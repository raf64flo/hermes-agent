"""Tests for tools/profile_consolidation.py — bounded one-pass USER.md editing.

Acceptance (task E4 / backlog §E4):
- AC1: after cooldown + ≥ min_candidates, an edit produces a USER.md ≤ cap and an
  archived snapshot of the previous version; under-threshold or pre-cooldown
  attempts refuse and leave the file untouched.
- AC2: a retracted source (E1 provenance revoked + stale) flags the informed
  profile bullet [STALE] in <1 pass, bypassing the cooldown, without deleting it.
- AC3: a rollback restores the previous profile state (and archives the
  pre-rollback state so a failed rollback can be undone).
Plus the config gate (off by default, opt-in) and the hard user_profile_enabled gate.
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

DELIM = "\n§\n"


def _bullets(*texts) -> str:
    return DELIM.join(texts)


def _iso_offset(now: float, hours: float) -> str:
    return datetime.fromtimestamp(now - hours * 3600, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _stub_config(monkeypatch, ledger_path, pc_overrides=None, memory_overrides=None):
    """Stub the config loaders: one memory section (profile writes on by default)
    + one provenance section. ``memory_overrides`` can e.g. force
    ``user_profile_enabled: False`` to exercise the hard consent gate."""
    import hermes_cli.config as _cfg

    pc = {
        "enabled": True, "min_candidates": 5, "cooldown_hours": 24,
        "max_bullets": 12, "char_limit": 1500,
    }
    if pc_overrides:
        pc.update(pc_overrides)
    memory = {"profile_consolidation": pc}
    if memory_overrides:
        memory.update(memory_overrides)
    cfg = {"memory": memory,
           "provenance": {"ledger_path": ledger_path}}
    monkeypatch.setattr(_cfg, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(_cfg, "load_config_readonly", lambda *a, **k: cfg)


def _write_user_md(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated profile dir + ledger; a ProfileConsolidator pointed at it."""
    import hermes_cli.config as _cfg
    from tools import memory_tool
    from tools.profile_consolidation import ProfileConsolidator, load_profile_consolidation_config

    mem_dir = tmp_path / "memories"
    mem_dir.mkdir(parents=True)
    user_md = mem_dir / "USER.md"
    state_dir = mem_dir / ".profile"
    db = tmp_path / "ledger" / "provenance.sqlite"

    monkeypatch.setattr(memory_tool, "get_memory_dir", lambda: mem_dir)
    _stub_config(monkeypatch, str(db))
    config = load_profile_consolidation_config()
    tc = ProfileConsolidator(user_md_path=user_md, state_dir=state_dir, config=config)
    return {
        "tc": tc, "user_md": user_md, "state_dir": state_dir,
        "db": db, "mem_dir": mem_dir, "root": tmp_path,
    }


def _set_last_edit(tc, now: float, hours_ago: float):
    """Set the backlog last_edit_at (read-modify-write; the returned dict is ephemeral)."""
    bl = tc._backlog()
    bl["last_edit_at"] = _iso_offset(now, hours_ago)
    tc._save_backlog(bl)


def _queue(tc, n=5, prefix="fact"):
    """Queue n distinct backlog facts; return their ids."""
    ids = []
    for i in range(n):
        res = tc.add_candidate(f"{prefix} {i}: a persistent fact about the user")
        assert res["success"], res
        ids.append(res["id"])
    return ids


# ── config gate ─────────────────────────────────────────────────────────────
class TestConfigGate:
    def test_off_by_default(self, monkeypatch):
        from tools.profile_consolidation import load_profile_consolidation_config
        import hermes_cli.config as _cfg
        monkeypatch.setattr(_cfg, "load_config_readonly", lambda *a, **k: {})
        assert load_profile_consolidation_config()["enabled"] is False

    def test_consolidate_refused_when_disabled(self, env, monkeypatch):
        import hermes_cli.config as _cfg
        _stub_config(monkeypatch, str(env["db"]), {"enabled": False})
        from tools.profile_consolidation import ProfileConsolidator, load_profile_consolidation_config
        tc = ProfileConsolidator(env["user_md"], env["state_dir"],
                                 load_profile_consolidation_config())
        res = tc.consolidate(_bullets("a", "b"))
        assert res["success"] is False
        assert "disabled" in res["error"]
        assert not env["user_md"].exists()


# ── AC1 ─────────────────────────────────────────────────────────────────────
class TestAc1:
    def test_edit_after_cooldown_with_enough_candidates(self, env):
        """AC1: cooldown elapsed + ≥ min_candidates → bounded edit + archived snapshot."""
        tc, user_md = env["tc"], env["user_md"]
        old = _bullets("bullet one", "bullet two")
        _write_user_md(user_md, old)
        ids = _queue(tc, 6)

        now = time.time()
        # last edit 48h ago → cooldown (24h) elapsed
        _set_last_edit(tc, now, 48)

        new = _bullets(*[f"bullet {i}" for i in range(8)])
        res = tc.consolidate(new, consumed_ids=ids)
        assert res["success"], res
        assert res["char_count"] == len(new)
        assert res["bullet_count"] == 8
        assert res["archived"], "a snapshot must exist before the write"

        # the file now holds the new content, and an archive holds the OLD one
        assert user_md.read_text(encoding="utf-8") == new
        archives = sorted((env["state_dir"] / "archives").glob("USER.md.*.md"))
        assert len(archives) == 1
        assert archives[0].read_text(encoding="utf-8") == old

        # backlog cleared of consumed facts
        assert tc._backlog()["candidates"] == []
        assert tc._backlog()["last_edit_at"] is not None

    def test_refused_with_insufficient_candidates(self, env):
        tc, user_md = env["tc"], env["user_md"]
        old = _bullets("only", "these")
        _write_user_md(user_md, old)
        _queue(tc, 3)  # 3 < 5
        now = time.time()
        _set_last_edit(tc, now, 48)

        res = tc.consolidate(_bullets("a", "b", "c"))
        assert res["success"] is False
        assert "candidates" in res["error"]
        assert user_md.read_text(encoding="utf-8") == old
        assert not (env["state_dir"] / "archives").exists()  # no snapshot for a refused write

    def test_refused_before_cooldown(self, env):
        tc, user_md = env["tc"], env["user_md"]
        old = _bullets("old state")
        _write_user_md(user_md, old)
        _queue(tc, 6)
        now = time.time()
        _set_last_edit(tc, now, 1)  # 1h ago < 24h

        res = tc.consolidate(_bullets("fresh"))
        assert res["success"] is False
        assert "Cooldown" in res["error"]
        assert user_md.read_text(encoding="utf-8") == old
        # status reports the block
        st = tc.status(now=now)
        assert st["cooldown_ready"] is False
        assert st["due"] is False

    def test_refused_over_char_limit(self, env):
        tc, user_md = env["tc"], env["user_md"]
        _write_user_md(user_md, _bullets("old"))
        _queue(tc, 5)
        now = time.time()
        _set_last_edit(tc, now, 48)

        big = _bullets(*[f"very long bullet {i}" * 30 for i in range(3)])
        assert len(big) > 1500
        res = tc.consolidate(big)
        assert res["success"] is False
        assert "char limit" in res["error"]
        assert user_md.read_text(encoding="utf-8") == _bullets("old")

    def test_refused_over_bullet_limit(self, env):
        tc, user_md = env["tc"], env["user_md"]
        _write_user_md(user_md, _bullets("old"))
        _queue(tc, 5)
        now = time.time()
        _set_last_edit(tc, now, 48)

        too_many = _bullets(*[f"b{i}" for i in range(13)])  # 13 > 12
        res = tc.consolidate(too_many)
        assert res["success"] is False
        assert "bullet limit" in res["error"]

    def test_add_candidate_dedup(self, env):
        tc = env["tc"]
        r1 = tc.add_candidate("a persistent fact about the user")
        r2 = tc.add_candidate("a persistent fact about the user")
        assert r1["added"] is True
        assert r2["added"] is False
        assert r2["pending_count"] == 1


# ── AC2 ─────────────────────────────────────────────────────────────────────
class TestAc2:
    def test_mark_stale_flags_informed_bullet_without_cooldown(self, env):
        """AC2: revoked source → [STALE] on the informed bullet, one pass, no cooldown."""
        from tools.provenance_ledger import record, retract, entry_source_id
        tc, user_md = env["tc"], env["user_md"]
        bullet = "Benchmarks sequential, never parallel (RTX 3500 12GB)"
        _write_user_md(user_md, _bullets(bullet, "another fact"))

        # a memory source informed this profile bullet (E1 link)
        src = entry_source_id(bullet)
        row = record(src, "memory", "user_profile", "INFORMED_BY", source_text=bullet)
        assert row is not None

        # the memory write is retracted → E1 marks the link revoked + stale
        affected = retract(src, "memory")
        assert "user_profile" in affected

        # the COOLDOWN IS ACTIVE (last edit 1h ago) — mark_stale must bypass it anyway
        now = time.time()
        _set_last_edit(tc, now, 1)

        res = tc.mark_stale()
        assert res["success"], res
        assert res["marked"] == 1
        assert res["revoked_sources"] == 1

        content = user_md.read_text(encoding="utf-8")
        assert f"[STALE] {bullet}" in content
        assert "another fact" in content  # never deleted on the fly
        # one pass: a second call finds nothing new to mark
        assert tc.mark_stale()["marked"] == 0


# ── AC3 ─────────────────────────────────────────────────────────────────────
class TestAc3:
    def test_rollback_restores_previous_state(self, env):
        """AC3: rollback restores the archived previous state."""
        tc, user_md = env["tc"], env["user_md"]
        old = _bullets("the original profile", "with these facts")
        _write_user_md(user_md, old)
        _queue(tc, 6)
        now = time.time()
        _set_last_edit(tc, now, 48)

        new = _bullets("rewritten profile", "newer set")
        res = tc.consolidate(new)  # consumes all 6 pending candidates
        assert res["success"], res
        assert res["entries_consolidated"] == 6
        assert user_md.read_text(encoding="utf-8") == new

        back = tc.rollback()
        assert back["success"], back
        assert user_md.read_text(encoding="utf-8") == old
        # the pre-rollback state was itself archived (a failed rollback is undoable)
        archives = sorted((env["state_dir"] / "archives").glob("USER.md.*.md"))
        assert len(archives) >= 2
        assert any(a.read_text(encoding="utf-8") == new for a in archives)

    def test_rollback_without_archives_refused(self, env):
        tc = env["tc"]
        res = tc.rollback()
        assert res["success"] is False
        assert "No archives" in res["error"]


# ── hard gate: user_profile_enabled (spec §Consent, hard write gate) ────────
# The user_profile_enabled gate holds for EVERY write to USER.md — consolidate,
# mark_stale and rollback alike. It is independent of the consolidation
# opt-in flag and of the cooldown/threshold bypasses.
class TestUserProfileGate:
    def test_mark_stale_refused_when_profile_writes_disabled(self, env, monkeypatch):
        """AC2 fix: mark_stale bypasses cooldown/threshold, but NOT the consent gate.

        When memory.user_profile_enabled=false, mark_stale must refuse even when
        the consolidation opt-in is ON and a provenance row is revoked+stale.
        """
        from tools.provenance_ledger import record, retract, entry_source_id
        _stub_config(monkeypatch, str(env["db"]), memory_overrides={"user_profile_enabled": False})
        from tools.profile_consolidation import ProfileConsolidator, load_profile_consolidation_config
        tc = ProfileConsolidator(env["user_md"], env["state_dir"],
                                 load_profile_consolidation_config())
        bullet = "Benchmarks sequential, never parallel (RTX 3500 12GB)"
        _write_user_md(env["user_md"], _bullets(bullet, "another fact"))

        # a revoked+stale provenance row exists — the fast path WOULD fire...
        src = entry_source_id(bullet)
        assert record(src, "memory", "user_profile", "INFORMED_BY", source_text=bullet) is not None
        assert "user_profile" in retract(src, "memory")
        # ...and the cooldown is even active.

        res = tc.mark_stale()
        assert res["success"] is False
        assert "user_profile_enabled" in res["error"]
        # USER.md untouched, no [STALE] marker, no archive created
        assert env["user_md"].read_text(encoding="utf-8") == _bullets(bullet, "another fact")
        assert "[STALE]" not in env["user_md"].read_text(encoding="utf-8")
        assert not (env["state_dir"] / "archives").exists()

    def test_rollback_refused_when_profile_writes_disabled(self, env, monkeypatch):
        """AC3 fix: rollback is a safety net (no consolidation gate) but a write
        nonetheless — it must refuse when profile writes are disabled."""
        _stub_config(monkeypatch, str(env["db"]), memory_overrides={"user_profile_enabled": False})
        from tools.profile_consolidation import ProfileConsolidator, load_profile_consolidation_config
        tc = ProfileConsolidator(env["user_md"], env["state_dir"],
                                 load_profile_consolidation_config())
        old = _bullets("the original profile")
        _write_user_md(env["user_md"], old)
        # an archive exists so the only thing that can block is the gate
        env["tc"]._archive_current()

        res = tc.rollback()
        assert res["success"] is False
        assert "user_profile_enabled" in res["error"]
        # USER.md untouched
        assert env["user_md"].read_text(encoding="utf-8") == old