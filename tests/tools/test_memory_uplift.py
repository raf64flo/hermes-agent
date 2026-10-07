"""Tests for tools/memory_uplift.py — E2 memory → skill uplift (proposer mode).

Covers the card's acceptance criteria:
- AC1: the consolidation prompt carries the uplift candidates (verbatim entries +
  expected slug + the live-tree EXISTS state) — the fork PROPOSES, the module never
  creates a skill itself (C1: proposer mode, live tree only).
- AC2: demotion is the deterministic post-fork half — the E1 backlink is written
  BEFORE any removal, a failed removal keeps the fragment (worst case = fragment
  kept + backlink), and the budget char count drops.
- AC3: the whole pass rides the ``consolidate`` branch (covered by the wiring tests
  in tests/agent/test_curator_uplift.py: default off ⇒ zero LLM fork, zero demotion,
  zero side effects).
Plus: slug derivation, the ≥2-entries trigger, the fail-open jev-curator
consultation (C3 — read-only, absence never blocks), and the curated-report wiring.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools import memory_uplift as mu
from tools import provenance_ledger as P
from tools.memory_tool_store import ENTRY_DELIMITER, MemoryStore


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mem_dir(tmp_path, monkeypatch):
    """Isolated memories dir with an existing MEMORY.md — the demotion reads/writes
    through the real store, no mocks on the write path."""
    d = tmp_path / "memories"
    d.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: d)
    return d


def _write_memory(mem_dir: Path, entries):
    (mem_dir / "MEMORY.md").write_text(ENTRY_DELIMITER.join(entries), encoding="utf-8")


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """Provenance ledger enabled on a temp DB (the test_provenance_ledger idiom)."""
    import hermes_cli.config as _cfg

    db = tmp_path / "ledger" / "provenance.sqlite"
    cfg = {"provenance": {"ledger_path": str(db)}}
    monkeypatch.setattr(_cfg, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(_cfg, "load_config_readonly", lambda *a, **k: cfg)
    return {"db": db}


# ---------------------------------------------------------------------------
# derive_slug — the slug is derived from the pattern label (target is the bucket)
# ---------------------------------------------------------------------------


class TestDeriveSlug:
    def test_accented_label_deaccented(self):
        # FR labels are the feature's domain: accents transliterate (é→e), not dropped.
        assert mu.derive_slug("Référencer les sources") == "referencer-les-sources"

    def test_mixed_case_and_punct(self):
        # camelCase splits to kebab; punctuation → dashes; lower-cased.
        assert mu.derive_slug("Ruff + PyGount BEFORE doc") == "ruff-py-gount-before-doc"

    def test_leading_digit_gets_prefix(self):
        # Contract: letter first (^[a-z][a-z0-9-]*$) — a leading digit is prefixed, not allowed.
        assert mu.derive_slug("3 ways to test") == "skill-3-ways-to-test"

    def test_no_latin_letter_skipped(self):
        # No plain Latin letter (pure diacritics) → no confident slug → "" (this is the
        # skip-guard eligible_patterns relies on; mirrors the k-no-slug fixture label éàç).
        assert mu.derive_slug("éàçü") == ""

    def test_empty(self):
        assert mu.derive_slug("") == ""

    def test_whitespace_only_empty(self):
        assert mu.derive_slug("   ") == ""

    def test_bounded_length(self):
        assert len(mu.derive_slug("a" * 200)) <= 64


# ---------------------------------------------------------------------------
# eligible_patterns — the ≥2-entries trigger, read-only
# ---------------------------------------------------------------------------


LEDGER = {
    "version": 1,
    "patterns": [
        {"key": "k-audit", "label": "Auditer un repo", "target": "memory",
         "canonical": "E1", "entries": ["E1", "E2"], "created": "2026-01-01T00:00:00Z"},
        {"key": "k-single", "label": "Solo entry", "target": "user",
         "canonical": "E3", "entries": ["E3"], "created": "2026-01-01T00:00:00Z"},
        {"key": "k-no-slug", "label": "éàç", "target": "memory",
         "canonical": "E4", "entries": ["E4", "E5"], "created": "2026-01-01T00:00:00Z"},
    ],
    "folded": [],
    "decisions": [],
}


class TestEligiblePatterns:
    def test_two_entry_trigger_only(self):
        out = mu.eligible_patterns(LEDGER)
        assert [c["key"] for c in out] == ["k-audit"]
        assert out[0]["slug"] == "auditer-un-repo"
        assert out[0]["target"] == "memory"
        assert out[0]["entries"] == ["E1", "E2"]

    def test_min_entries_override(self):
        assert mu.eligible_patterns(LEDGER, min_entries=1) and len(mu.eligible_patterns(LEDGER, min_entries=1)) == 2

    def test_empty_and_none_ledger(self):
        assert mu.eligible_patterns({}) == []
        assert mu.eligible_patterns(None) == []  # load_ledger() on an empty home → empty


class TestEvictionSansMotifNonCandidate:
    """AC fail-closed (audit OMH 2026-10-02 reco 2b) : une entrée sans motif n'est
    PAS candidate à la promotion/démotion — décidé déterministiquement à la
    sélection des candidats, AVANT tout jugement modèle. Pas de motif = pas de
    candidat, jamais « le modèle décide dans le doute ». Sans LLM."""

    def test_groupe_sans_motif_non_candidate(self):
        # k-x et k-y portent ≥2 entrées (déclencheur) mais AUCUN label de motif →
        # exclus ; seul k-ok, qui a un vrai motif, est candidat.
        ledger = {
            "version": 1,
            "patterns": [
                {"key": "k-x", "label": "", "canonical": "X", "entries": ["E1", "E2"]},
                {"key": "k-y", "canonical": "Y", "entries": ["E3", "E4"]},  # pas de clé label
                {"key": "k-ok", "label": "Vraie procédure", "canonical": "Z",
                 "entries": ["E5", "E6"]},
            ],
            "folded": [], "decisions": [],
        }
        out = mu.eligible_patterns(ledger)
        assert [c["key"] for c in out] == ["k-ok"]
        # Tout candidat porte un vrai motif (déterministe, sans modèle).
        assert all(c.get("label") for c in out)

    def test_entrees_sans_motif_jamais_emises(self):
        # Des entrées sans motif (hors de tout groupe étiqueté) ne remontent jamais
        # comme candidat : la sélection ne part que des groupes du ledger.
        assert mu.eligible_patterns({"version": 1, "patterns": [], "folded": [], "decisions": []}) == []
        # Un groupe sans aucune clé label est droppé par le garde fail-closed.
        out = mu.eligible_patterns(
            {"version": 1, "patterns": [{"entries": ["E1", "E2"]}],
             "folded": [], "decisions": []})
        assert out == []


# ---------------------------------------------------------------------------
# render_uplift_section — the prompt the fork sees (AC1 input)
# ---------------------------------------------------------------------------


def _cand(**over):
    base = {"key": "k-audit", "label": "Auditer un repo", "target": "memory",
            "entries": ["Lancer ruff check puis pygount", "Relire la doc ensuite"],
            "canonical": "Lancer ruff check puis pygount", "slug": "auditer-un-repo"}
    base.update(over)
    return base


class TestRenderUpliftSection:
    def test_no_candidates_no_section(self):
        assert mu.render_uplift_section([]) == ""

    def test_verbatim_entries_and_slug(self):
        section = mu.render_uplift_section([_cand()], existing_slugs={"other-skill"})
        assert "## Memory → skill uplift" in section
        assert "`auditer-un-repo`" in section
        assert "not present (create it)" in section
        assert "> Lancer ruff check puis pygount" in section  # verbatim
        assert "> Relire la doc ensuite" in section
        assert "LIVE tree" in section

    def test_existing_slug_gets_patch_instruction(self):
        section = mu.render_uplift_section([_cand()], existing_slugs={"auditer-un-repo"})
        assert "EXISTS in live tree (patch it)" in section

    def test_jev_evidence_rendered_when_ok(self):
        jev = {"ok": True, "mode": "observe", "summary": "s",
               "relations": [{"a": "a", "b": "auditer-un-repo", "relation": "duplicate", "confidence": 0.9}]}
        section = mu.render_uplift_section([_cand()], existing_slugs=set(), jev=jev)
        assert "jev-curator relation evidence" in section
        assert "a ↔ auditer-un-repo: duplicate" in section

    def test_jev_unavailable_not_rendered(self):
        section = mu.render_uplift_section([_cand()], existing_slugs=set(),
                                           jev={"ok": False, "mode": "", "summary": "unavailable", "relations": []})
        assert "jev-curator relation evidence" not in section


# ---------------------------------------------------------------------------
# consult_jev — C3 read-only, fail-open
# ---------------------------------------------------------------------------


class _FakeSettings:
    mode = "observe"


class _FakeService:
    def __init__(self, scan_result=None, exc=None):
        self.settings = _FakeSettings()
        self._scan_result = scan_result
        self._exc = exc

    def scan(self):
        if self._exc is not None:
            raise self._exc
        return self._scan_result


class TestConsultJev:
    def test_empty_candidates_short_circuit(self):
        r = mu.consult_jev([])
        assert r["ok"] is False and r["relations"] == []

    def test_service_unavailable_fail_open(self):
        r = mu.consult_jev([_cand()], service=None)  # no plugin in test env
        assert r["ok"] is False and "unavailable" in r["summary"] and r["relations"] == []

    def test_scan_exception_fail_open(self):
        r = mu.consult_jev([_cand()], service=_FakeService(exc=RuntimeError("no route")))
        assert r["ok"] is False and "scan failed" in r["summary"]

    def test_only_signal_relations_injected(self):
        scan = {"judgments": [
            {"a": "x", "b": "y", "relation": "duplicate", "confidence": 0.8},
            {"a": "m", "b": "n", "relation": "unrelated", "confidence": 0.4},
        ]}
        r = mu.consult_jev([_cand()], service=_FakeService(scan_result=scan))
        assert r["ok"] is True and r["mode"] == "observe"
        assert len(r["relations"]) == 1 and r["relations"][0]["a"] == "x"


# ---------------------------------------------------------------------------
# write_fold_links + demote — AC2 (backlink first, failed removal keeps fragment)
# ---------------------------------------------------------------------------


E1_TEXT = "Lancer ruff check puis pygount avant de relire la doc"
E2_TEXT = "Relire la doc d'un repo après ruff et pygount"
KEEPER = "Le serveur Netdata tourne sur le port 19999"


class TestDemote:
    def test_full_demotion(self, mem_dir, ledger):
        _write_memory(mem_dir, [E1_TEXT, E2_TEXT, KEEPER])
        store = MemoryStore()
        store.load_from_disk()
        pattern = {"key": "k-audit", "target": "memory", "entries": [E1_TEXT, E2_TEXT]}
        rep = mu.demote(pattern, "auditer-un-repo", store)
        assert rep["link_written"] == 2
        assert rep["removed"] == [E1_TEXT, E2_TEXT]
        assert rep["kept"] == []
        assert rep["chars_after"] < rep["chars_before"]
        assert rep["budget_dropped"] == rep["chars_before"] - rep["chars_after"]
        # The fragment is really gone from disk; the keeper survives.
        on_disk = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
        assert E1_TEXT not in on_disk and E2_TEXT not in on_disk and KEEPER in on_disk
        # The backlink rows exist, queryable, target = the slug.
        rows = P.rows_for_target("auditer-un-repo")
        assert {r["source_type"] for r in rows} == {"memory"}
        assert {r["relation"] for r in rows} == {"FOLDED_INTO"}
        assert {r["source_text"] for r in rows} == {E1_TEXT, E2_TEXT}

    def test_user_bucket_target(self, mem_dir, ledger):
        (mem_dir / "USER.md").write_text(ENTRY_DELIMITER.join([E1_TEXT, E2_TEXT]), encoding="utf-8")
        store = MemoryStore()
        store.load_from_disk()
        rep = mu.demote({"key": "k", "target": "user", "entries": [E1_TEXT, E2_TEXT]},
                        "user-proc", store)
        assert rep["removed"] == [E1_TEXT, E2_TEXT]
        assert {r["source_type"] for r in P.rows_for_target("user-proc")} == {"user"}

    def test_failed_removal_keeps_fragment_but_link_written(self, mem_dir, ledger):
        """AC2 worst case: the entry is NOT in the file (already gone / ambiguous /
        drift) → the removal fails, the fragment is reported kept, and the backlink
        is STILL written (it goes out before any removal)."""
        _write_memory(mem_dir, [KEEPER])  # neither E1 nor E2 present
        store = MemoryStore()
        store.load_from_disk()
        rep = mu.demote({"key": "k", "target": "memory", "entries": [E1_TEXT, E2_TEXT]},
                        "auditer-un-repo", store)
        assert rep["removed"] == []
        assert rep["kept"] == [E1_TEXT, E2_TEXT]
        assert rep["link_written"] == 2
        assert P.rows_for_target("auditer-un-repo")  # the backlink exists despite the failure

    def test_ledger_off_still_demotes(self, mem_dir):
        """Fail-open: no provenance config → 0 links written, but the removal still
        happens (the demotion is not gated on the ledger)."""
        _write_memory(mem_dir, [E1_TEXT, KEEPER])
        store = MemoryStore()
        store.load_from_disk()
        rep = mu.demote({"key": "k", "target": "memory", "entries": [E1_TEXT]}, "s-x", store)
        assert rep["link_written"] == 0
        assert rep["removed"] == [E1_TEXT]

    def test_empty_entries_noop(self, mem_dir, ledger):
        store = MemoryStore()
        rep = mu.demote({"key": "k", "target": "memory", "entries": []}, "s-y", store)
        assert rep == {"slug": "s-y", "key": "k", "target": "memory", "link_written": 0,
                       "removed": [], "kept": [], "chars_before": 0, "chars_after": 0,
                       "budget_dropped": 0}


# ---------------------------------------------------------------------------
# validated_uplifts — the anti-blind-fold guard
# ---------------------------------------------------------------------------


class TestValidatedUplifts:
    def test_only_materialized_slugs_pass(self):
        cands = [_cand(), _cand(key="k2", slug="autre-skill", label="Autre")]
        out = mu.validated_uplifts(cands, {"auditer-un-repo"})
        assert [c["key"] for c in out] == ["k-audit"]

    def test_empty_materialized(self):
        assert mu.validated_uplifts([_cand()], set()) == []