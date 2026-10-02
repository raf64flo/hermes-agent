"""Wiring tests for the E2 memory → skill uplift in agent/curator.py.

The LLM fork is never spawned: ``_run_llm_review`` is monkeypatched. These tests pin
the card's acceptance criteria against the REAL curator code paths:
- AC1: ``consolidate=True`` → the prompt handed to the fork carries the uplift section
  (candidates + verbatim entries + expected slug + live-tree state + jev evidence);
  post-fork, a materialized + declared skill triggers the deterministic demotion.
- AC2: the demotion snapshots ``MEMORY.md``/``USER.md`` before the first removal
  (restore path surfaced in the demoted report), and the backlink is written before
  the fragment is gone.
- AC3: default (``consolidate`` off) and ``dry_run`` → zero fork, zero uplift section,
  zero demotion, zero side effects (behavior of today).
"""

from __future__ import annotations

import json

from tests.agent.test_curator import curator_env  # noqa: F401  (shared fixture)


E1_TEXT = "Lancer ruff check puis pygount avant de relire la doc"
E2_TEXT = "Relire la doc d'un repo après ruff et pygount"

_CANDIDATE = {"key": "k-audit", "label": "Auditer un repo", "target": "memory",
              "entries": [E1_TEXT, E2_TEXT], "canonical": E1_TEXT, "slug": "auditer-un-repo"}


def _stub_uplift(curator, candidates, jev=None):
    import tools.memory_uplift as mu

    monkey_jev = jev or {"ok": True, "mode": "observe", "summary": "1 relation(s)",
                         "relations": [{"a": "x", "b": "auditer-un-repo",
                                         "relation": "duplicate", "confidence": 0.9}]}
    curator._uplift_candidates = lambda: (candidates, monkey_jev)
    if jev is None and candidates:
        # Keep the real section renderer: it is the AC1 surface under test.
        curator._uplift_prompt_section = lambda cands, j: mu.render_uplift_section(
            cands, existing_slugs=set(), jev=j)


def _stub_report(curator, names):
    """Curated report stub: rows for exactly *names* (fresh agent-created skills)."""
    import tools.skill_usage as usage

    row = lambda n: {"name": n, "provenance": "agent", "state": "active", "pinned": False,
                     "activity_count": 1, "use_count": 1, "view_count": 1, "patch_count": 0,
                     "last_activity_at": "2026-10-01T00:00:00+00:00"}
    usage.curated_report = lambda: [row(n) for n in names]
    curator._safe_curated_report = lambda: [row(n) for n in names]


def _mem_file(curator_env):
    from tools import memory_tool
    d = memory_tool.get_memory_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "MEMORY.md").write_text(E1_TEXT + "\n§\n" + E2_TEXT, encoding="utf-8")
    return d


def _write_pattern_ledger(curator_env):
    """The E3 sidecar ledger with one eligible pattern (≥2 entries)."""
    from tools import memory_patterns as mp
    from tools import memory_uplift as mu

    ledger = {"version": 1, "patterns": [
        {"key": _CANDIDATE["key"], "label": _CANDIDATE["label"], "target": "memory",
         "canonical": _CANDIDATE["canonical"], "entries": list(_CANDIDATE["entries"]),
         "created": "2026-01-01T00:00:00Z"}], "folded": [], "decisions": []}
    path = mp.get_ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger), encoding="utf-8")
    return mu.eligible_patterns()  # real loader, real path — no stub on the read side


# ---------------------------------------------------------------------------
# AC3 — off / dry-run: zero side effects
# ---------------------------------------------------------------------------


class TestConsolidateOff:
    def test_default_run_never_touches_uplift(self, curator_env, monkeypatch):
        """consolidate off (the default): no fork at all, no uplift section, no
        demotion, the memory file is byte-identical (behavior of today)."""
        home, curator = curator_env["home"], curator_env["curator"]
        monkeypatch.setattr(curator, "_load_config", lambda: {})
        prompts, demotes, notes = [], [], []
        monkeypatch.setattr(curator, "_run_llm_review", lambda p: prompts.append(p) or curator._llm_meta("no change"))
        monkeypatch.setattr(curator, "_demote_validated_uplifts",
                            lambda *a, **k: demotes.append(a) or [])
        _mem_file(curator_env)
        _write_pattern_ledger(curator_env)

        result = curator.run_curator_review(synchronous=True, on_summary=notes.append)

        # `summary_so_far` is the auto-only half; "consolidation off" rides the final
        # summary surfaced via on_summary. Pin both, on the right fields.
        assert result["summary_so_far"] == "no changes"
        joined = " | ".join(notes)
        assert "consolidation off" in joined
        assert "auto: " in joined
        assert prompts == []  # no fork — the uplift rides ONLY the consolidate branch
        assert demotes == []
        from tools import memory_tool
        assert (memory_tool.get_memory_dir() / "MEMORY.md").read_text(encoding="utf-8") == E1_TEXT + "\n§\n" + E2_TEXT


# ---------------------------------------------------------------------------
# AC1 — consolidate on: the prompt carries the uplift section
# ---------------------------------------------------------------------------


class TestPromptCarriesUplift:
    def test_uplift_section_injected(self, curator_env, monkeypatch):
        home, curator = curator_env["home"], curator_env["curator"]
        monkeypatch.setattr(curator, "_load_config", lambda: {})
        _mem_file(curator_env)
        cands = _write_pattern_ledger(curator_env)
        assert cands, "fixture must yield one eligible candidate"
        _stub_uplift(curator, cands)
        _stub_report(curator, [])

        prompts = []
        monkeypatch.setattr(curator, "_run_llm_review",
                            lambda p: prompts.append(p) or curator._llm_meta("ok"))
        monkeypatch.setattr(curator, "_demote_validated_uplifts", lambda *a, **k: [])
        # No candidate skill and no uplift → the pass would skip the fork; force the
        # candidate list to render a row so the prompt path runs.
        summary, meta, uplifts = curator._consolidation_pass("auto: ", "no changes", False, set())

        assert summary.startswith("auto: ")
        assert prompts, "the fork must have been handed a prompt"
        section = prompts[0]
        assert "## Memory → skill uplift" in section
        assert "`auditer-un-repo`" in section
        assert "> Lancer ruff check puis pygount avant de relire la doc" in section
        assert "> Relire la doc d'un repo après ruff et pygount" in section
        assert "not present (create it)" in section
        assert "duplicate" in section and "observe" in section  # jev evidence block
        assert uplifts == []  # the stub demotion ran (kept-all → no demoted entry)


# ---------------------------------------------------------------------------
# AC1 + AC2 — post-fork demotion of a validated candidate
# ---------------------------------------------------------------------------


class TestPostForkDemotion:
    def test_materialized_and_declared_candidate_is_demoted(self, curator_env, monkeypatch):
        home, curator = curator_env["home"], curator_env["curator"]
        monkeypatch.setattr(curator, "_load_config", lambda: {})
        # Enable the provenance ledger on a temp DB (the `ledger`-fixture idiom in
        # test_memory_uplift.py): without it record() fails open → link_written == 0.
        # The AC2 assertion is that the backlink is REAL and queryable via rows_for_target.
        import hermes_cli.config as _cfg
        db = home / "provenance.sqlite"
        _prov_cfg = {"provenance": {"ledger_path": str(db)}}
        monkeypatch.setattr(_cfg, "load_config", lambda *a, **k: _prov_cfg)
        monkeypatch.setattr(_cfg, "load_config_readonly", lambda *a, **k: _prov_cfg)
        mem_dir = _mem_file(curator_env)
        _write_pattern_ledger(curator_env)
        cands = [{**_CANDIDATE}]
        _stub_uplift(curator, cands)
        # The fork "created" the skill: it appears in the post-fork report and is
        # declared in the structured summary.
        _stub_report(curator, ["auditer-un-repo"])
        llm_meta = curator._llm_meta("ok")
        llm_meta["final"] = (
            "consolidated the audit fragments\n```yaml\n"
            "consolidations: []\nprunings: []\n"
            "uplifts:\n  - slug: auditer-un-repo\n    key: k-audit\n    reason: reusable audit procedure\n"
            "```\n")

        results = curator._demote_validated_uplifts(cands, before_names=set(), llm_meta=llm_meta)

        assert len(results) == 1
        rep = results[0]
        assert rep["status"] == "demoted"
        assert rep["removed"] == [E1_TEXT, E2_TEXT]
        assert rep["link_written"] == 2
        # AC2 — the memory file was snapshotted BEFORE the removal, and the path is
        # surfaced in the report.
        assert rep.get("memory_snapshot"), "the demoted report must name its restore point"
        import pathlib
        snap = pathlib.Path(rep["memory_snapshot"])
        assert snap.is_dir()
        mem_copy = list(snap.glob("MEMORY.md.*"))
        assert mem_copy and mem_copy[0].read_text(encoding="utf-8") == E1_TEXT + "\n§\n" + E2_TEXT
        # The backlink is queryable and the fragment is really gone from the budget.
        from tools import provenance_ledger as P
        import hermes_cli.config as _cfg
        rows = P.rows_for_target("auditer-un-repo")
        assert {r["relation"] for r in rows} == {"FOLDED_INTO"}
        on_disk = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
        assert E1_TEXT not in on_disk and E2_TEXT not in on_disk

    def test_unmaterialized_candidate_keeps_memory(self, curator_env, monkeypatch):
        """The anti-blind-fold guard: the fork declared the slug but the skill does
        not exist post-fork → nothing is removed, nothing is snapshotted."""
        home, curator = curator_env["home"], curator_env["curator"]
        monkeypatch.setattr(curator, "_load_config", lambda: {})
        mem_dir = _mem_file(curator_env)
        _write_pattern_ledger(curator_env)
        _stub_uplift(curator, [{**_CANDIDATE}])
        _stub_report(curator, [])  # the skill was never materialized
        llm_meta = curator._llm_meta("ok")
        llm_meta["final"] = (
            "```yaml\nuplifts:\n  - slug: auditer-un-repo\n    key: k-audit\n    reason: r\n```\n")

        results = curator._demote_validated_uplifts([{**_CANDIDATE}], before_names=set(), llm_meta=llm_meta)

        assert results[0]["status"].startswith("kept")
        assert results[0]["kept"] == [E1_TEXT, E2_TEXT]
        assert "memory_snapshot" not in results[0]
        on_disk = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
        assert E1_TEXT in on_disk and E2_TEXT in on_disk  # memory untouched
        from agent import curator_backup
        snaps = list((curator_backup._skills_dir() / ".curator_backups" / ".memory_snapshots").glob("*")) if (
            curator_backup._skills_dir() / ".curator_backups" / ".memory_snapshots").is_dir() else []
        assert snaps == []  # lazy: no demotion approved → no snapshot taken

    def test_patch_case_needs_declaration(self, curator_env, monkeypatch):
        """Pre-existing skill (in before_names) + no `uplifts:` declaration → kept.
        A create/patch merely named X is not enough evidence X contains the procedure."""
        home, curator = curator_env["home"], curator_env["curator"]
        monkeypatch.setattr(curator, "_load_config", lambda: {})
        _mem_file(curator_env)
        _write_pattern_ledger(curator_env)
        _stub_uplift(curator, [{**_CANDIDATE}])
        _stub_report(curator, ["auditer-un-repo"])  # exists post-fork AND pre-fork
        results = curator._demote_validated_uplifts(
            [{**_CANDIDATE}], before_names={"auditer-un-repo"}, llm_meta=curator._llm_meta("ok"))
        assert results[0]["status"].startswith("kept")


# ---------------------------------------------------------------------------
# AC2 — the memory snapshot helper itself
# ---------------------------------------------------------------------------


class TestMemorySnapshot:
    def test_copies_existing_files_and_ignores_missing(self, curator_env, monkeypatch):
        home, curator = curator_env["home"], curator_env["curator"]
        mem_dir = _mem_file(curator_env)
        (mem_dir / "USER.md").write_text("user fact", encoding="utf-8")

        snap = curator._memory_snapshot()

        assert snap and snap["files"] == ["MEMORY.md", "USER.md"]
        path = __import__("pathlib").Path(snap["path"])
        assert (path / f"MEMORY.md.{path.name}").read_text(encoding="utf-8") == E1_TEXT + "\n§\n" + E2_TEXT
        assert (path / f"USER.md.{path.name}").read_text(encoding="utf-8") == "user fact"
        assert path.parent.name == ".memory_snapshots"

    def test_no_memory_dir_returns_none(self, curator_env, monkeypatch):
        home, curator = curator_env["home"], curator_env["curator"]
        # Fresh tmp home: no memories/ dir at all.
        assert curator._memory_snapshot() is None

    def test_retention_keeps_five(self, curator_env, monkeypatch):
        home, curator = curator_env["home"], curator_env["curator"]
        _mem_file(curator_env)
        import time
        from agent import curator_backup
        base = curator_backup._skills_dir() / ".curator_backups" / ".memory_snapshots"
        for i in range(7):  # 7 pre-existing old snapshots
            old = base / f"2020-01-01T00:00:{i:02d}"
            old.mkdir(parents=True, exist_ok=True)
        snap = curator._memory_snapshot()
        assert snap
        remaining = sorted(p.name for p in base.iterdir())
        assert len(remaining) == 5
        assert snap["path"].split("/")[-1] in remaining