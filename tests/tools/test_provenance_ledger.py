"""Tests for tools/provenance_ledger.py — SQLite provenance backlinks + stale-on-retract.

Acceptance (task E1):
- AC1: a simulated memory→skill fold leaves a queryable link — ``SELECT * FROM
  provenance WHERE target_id = ...`` returns the row with ``source_type='memory'``.
- AC2: ``memory(action=remove|replace)`` on a linked source marks ``revoked_at`` + a
  queryable ``stale`` flag, never touches the target file, and is idempotent.
- AC3: a drifted memory file makes the store refuse the write (the ``_drift_error``
  guard) and retract nothing — no retraction is lost or faked while a write is lost.
Plus the curator-model failure paths: corruption → backup + recreate (never destroy the
artifact), backup failure → file left untouched, and the config gate (off by default,
relative paths rejected).
"""

import sqlite3
from pathlib import Path

import pytest

ENTRY = "Benchmarks sequential, never parallel (RTX 3500 12GB)"
SKILL = "skill_x"


def _stub_config(monkeypatch, ledger_path):
    """The test_skill_ledger idiom: stub the config loaders so the gate sees exactly one
    ``provenance`` section."""
    import hermes_cli.config as _cfg

    cfg = {"provenance": {"ledger_path": ledger_path}}
    monkeypatch.setattr(_cfg, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(_cfg, "load_config_readonly", lambda *a, **k: cfg)


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """Ledger enabled on a temp DB (schema created on first use)."""
    import hermes_cli.config as _cfg
    from tools import provenance_ledger as P

    db = tmp_path / "ledger" / "provenance.sqlite"
    _stub_config(monkeypatch, str(db))
    return {"P": P, "db": db, "cfg": _cfg}


@pytest.fixture
def store_env(tmp_path, monkeypatch):
    """Real MemoryStore with an isolated memory dir AND the ledger enabled — the E2E path
    through ``MemoryStore._mutate`` (the hook call site), no mocks on the store."""
    import hermes_cli.config as _cfg
    from tools import provenance_ledger as P
    from tools.memory_tool_store import MemoryStore

    mem_dir = tmp_path / "memories"
    db = tmp_path / "ledger" / "provenance.sqlite"
    monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: mem_dir)
    _stub_config(monkeypatch, str(db))
    return {"P": P, "db": db, "store": MemoryStore(), "mem_dir": mem_dir, "root": tmp_path}


class TestConfigGate:
    def test_off_without_config(self):
        from tools import provenance_ledger as P

        assert P.ledger_path() is None
        assert P.record("a", "memory", SKILL, "FOLDED_INTO") is None
        assert P.retract("a", "memory") == []
        assert P.retract_entries([ENTRY], "memory") == []
        assert P.rows_for_target(SKILL) == []
        assert P.stale_targets() == []
        assert P.clear_stale(SKILL) == 0

    def test_relative_path_rejected(self, tmp_path, monkeypatch):
        from tools import provenance_ledger as P

        _stub_config(monkeypatch, "relative/provenance.sqlite")
        assert P.ledger_path() is None

    def test_non_string_rejected(self, monkeypatch):
        from tools import provenance_ledger as P

        _stub_config(monkeypatch, 12345)
        assert P.ledger_path() is None

    def test_expanduser_path(self, monkeypatch):
        from tools import provenance_ledger as P

        _stub_config(monkeypatch, "~/provenance-test.sqlite")
        assert P.ledger_path() == Path.home() / "provenance-test.sqlite"


class TestIdsAndSchema:
    def test_entry_source_id_normalizes(self):
        from tools import provenance_ledger as P

        assert P.entry_source_id("  abc ") == P.entry_source_id("abc")
        assert P.entry_source_id("abc") != P.entry_source_id("abd")
        assert len(P.entry_source_id("abc")) == 64

    def test_ac1_select_star_by_target(self, ledger):
        """AC1, verbatim against the file: the fold leaves a queryable link."""
        P, db = ledger["P"], ledger["db"]
        sid = P.entry_source_id(ENTRY)
        row = P.record(sid, "memory", SKILL, "FOLDED_INTO", source_text=ENTRY)
        assert row is not None
        assert row["source_type"] == "memory"
        assert row["target_id"] == SKILL
        assert row["relation"] == "FOLDED_INTO"
        assert row["revoked_at"] is None
        assert row["stale"] == 0
        assert row["source_text"] == ENTRY

        conn = sqlite3.connect(str(db))
        try:
            rows = conn.execute(
                "SELECT * FROM provenance WHERE target_id = ?", (SKILL,)).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1
        assert rows[0][1] == "memory"   # source_type column
        assert rows[0][3] == "FOLDED_INTO"  # relation column

    def test_record_idempotent_on_link_key(self, ledger):
        P = ledger["P"]
        sid = P.entry_source_id(ENTRY)
        P.record(sid, "memory", SKILL, "FOLDED_INTO", source_text=ENTRY)
        P.record(sid, "memory", SKILL, "FOLDED_INTO")
        assert len(P.rows_for_target(SKILL)) == 1

    def test_record_revives_revoked_link(self, ledger):
        P = ledger["P"]
        sid = P.entry_source_id(ENTRY)
        P.record(sid, "memory", SKILL, "FOLDED_INTO", source_text=ENTRY)
        assert P.retract(sid, "memory") == [SKILL]
        # source re-added / re-folded → the same link comes back alive, no duplicate row
        P.record(sid, "memory", SKILL, "FOLDED_INTO", source_text=ENTRY)
        rows = P.rows_for_target(SKILL)
        assert len(rows) == 1
        assert rows[0]["revoked_at"] is None
        assert rows[0]["stale"] == 0

    def test_bad_enums_rejected(self, ledger):
        P = ledger["P"]
        assert P.record("a", "bogus", SKILL, "FOLDED_INTO") is None
        assert P.record("a", "memory", SKILL, "BOGUS_RELATION") is None
        assert P.rows_for_target(SKILL) == []
        assert P.retract("a", "bogus") == []

    def test_stale_targets_groups_by_target(self, ledger):
        P = ledger["P"]
        P.record("a1", "memory", "t1", "FOLDED_INTO")
        P.record("a2", "memory", "t2", "INFORMED_BY")
        P.record("a3", "user", "t2", "DERIVED_FROM")
        P.retract("a1", "memory")
        assert [t["target_id"] for t in P.stale_targets()] == ["t1"]
        P.retract("a2", "memory")
        P.retract("a3", "user")
        by_target = {t["target_id"]: t for t in P.stale_targets()}
        assert set(by_target) == {"t1", "t2"}
        assert by_target["t2"]["links"] == 2


class TestStaleOnRetract:
    def test_ac2_remove_marks_revoked_and_stale(self, store_env):
        """AC2: remove on a linked source retracts the link, flags the target stale, and
        never touches the target file."""
        P, store, root = store_env["P"], store_env["store"], store_env["root"]
        skill_file = root / "skill_x.md"
        skill_file.write_text("---\nname: skill_x\n---\n\nBody.\n", encoding="utf-8")
        before = skill_file.read_bytes()

        assert store.add("memory", ENTRY)["success"]
        P.record(P.entry_source_id(ENTRY), "memory", SKILL, "FOLDED_INTO", source_text=ENTRY)
        assert store.remove("memory", ENTRY)["success"]
        assert ENTRY not in (store_env["mem_dir"] / "MEMORY.md").read_text(encoding="utf-8")

        rows = P.rows_for_target(SKILL)
        assert len(rows) == 1
        assert rows[0]["revoked_at"] is not None
        assert rows[0]["stale"] == 1
        assert any(t["target_id"] == SKILL for t in P.stale_targets())
        assert skill_file.read_bytes() == before  # the skill is never overwritten

    def test_ac2_replace_also_retracts(self, store_env):
        P, store = store_env["P"], store_env["store"]
        assert store.add("memory", ENTRY)["success"]
        P.record(P.entry_source_id(ENTRY), "memory", SKILL, "FOLDED_INTO", source_text=ENTRY)
        assert store.replace("memory", ENTRY, "Compressed: " + ENTRY[:20])["success"]
        rows = P.rows_for_target(SKILL)
        assert rows[0]["revoked_at"] is not None
        assert rows[0]["stale"] == 1

    def test_ac2_retract_idempotent(self, store_env):
        P, store = store_env["P"], store_env["store"]
        sid = P.entry_source_id(ENTRY)
        assert store.add("memory", ENTRY)["success"]
        P.record(sid, "memory", SKILL, "FOLDED_INTO", source_text=ENTRY)
        assert store.remove("memory", ENTRY)["success"]
        first_revoked_at = P.rows_for_target(SKILL)[0]["revoked_at"]

        # Same entry re-added, removed again: the link is already revoked → nothing
        # changes (idempotent, ``revoked_at IS NULL`` guard).
        assert store.add("memory", ENTRY)["success"]
        assert store.remove("memory", ENTRY)["success"]
        rows = P.rows_for_target(SKILL)
        assert len(rows) == 1
        assert rows[0]["revoked_at"] == first_revoked_at
        assert rows[0]["stale"] == 1
        assert P.retract(sid, "memory") == []  # third pass: no live link left

    def test_add_retracts_nothing(self, store_env):
        P, store = store_env["P"], store_env["store"]
        assert store.add("memory", ENTRY)["success"]
        assert store.add("memory", "Another unrelated entry")["success"]
        assert P.rows_for_target(SKILL) == []
        assert P.stale_targets() == []

    def test_clear_stale_preserves_revoked_at(self, store_env):
        P, store = store_env["P"], store_env["store"]
        sid = P.entry_source_id(ENTRY)
        assert store.add("memory", ENTRY)["success"]
        P.record(sid, "memory", SKILL, "FOLDED_INTO", source_text=ENTRY)
        assert store.remove("memory", ENTRY)["success"]
        revoked_at = P.rows_for_target(SKILL)[0]["revoked_at"]

        assert P.clear_stale(SKILL) == 1
        row = P.rows_for_target(SKILL)[0]
        assert row["stale"] == 0
        assert row["revoked_at"] == revoked_at  # the audit record survives
        assert P.stale_targets() == []
        assert P.clear_stale(SKILL) == 0  # already clear


class TestDriftGuard:
    def test_ac3_drift_refuses_write_and_retracts_nothing(self, store_env):
        """AC3: the drift guard refuses the write, so no retraction is lost or faked."""
        P, store, mem_dir = store_env["P"], store_env["store"], store_env["mem_dir"]
        sid = P.entry_source_id(ENTRY)
        assert store.add("memory", ENTRY)["success"]
        P.record(sid, "memory", SKILL, "FOLDED_INTO", source_text=ENTRY)

        # An external writer leaves content that would not round-trip (one entry over the
        # whole-file limit — exactly the signal _detect_external_drift looks for).
        mem_file = mem_dir / "MEMORY.md"
        mem_file.write_text("e" * 3000, encoding="utf-8")

        res = store.remove("memory", "e")
        assert res["success"] is False
        assert "drift_backup" in res
        assert Path(res["drift_backup"]).exists()

        rows = P.rows_for_target(SKILL)
        assert rows[0]["revoked_at"] is None
        assert rows[0]["stale"] == 0
        assert P.stale_targets() == []


class TestCorruption:
    def test_corrupt_file_backed_up_and_recreated(self, ledger):
        P, db = ledger["P"], ledger["db"]
        assert P.record("a1", "memory", "t1", "DERIVED_FROM") is not None
        db.write_bytes(b"X" * 160)  # garbage over a page header: not a database

        row = P.record("a2", "memory", "t1", "INFORMED_BY", source_text="x")
        assert row is not None  # fail-open: the caller never saw an exception
        assert db.exists()
        baks = list(db.parent.glob("provenance.sqlite.bak.*.sqlite"))
        assert len(baks) == 1  # the corrupt artifact was preserved, not destroyed
        assert [r["source_id"] for r in P.rows_for_target("t1")] == ["a2"]

    def test_backup_failure_leaves_file_untouched(self, ledger, monkeypatch):
        """When even the backup fails, the corrupt file must NOT be destroyed."""
        P, db = ledger["P"], ledger["db"]
        assert P.record("a0", "memory", "t0", "DERIVED_FROM") is not None  # valid file first
        garbage = b"X" * 160
        db.write_bytes(garbage)

        def _boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(Path, "replace", _boom)
        assert P.record("a1", "memory", "t1", "DERIVED_FROM") is None  # fail-open
        assert db.read_bytes() == garbage  # untouched, no recreation

    def test_retract_on_empty_ledger(self, ledger):
        P = ledger["P"]
        assert P.retract("a1", "memory") == []
        assert P.rows_for_target("t1") == []