"""Tests for tools/memory_patterns.py — task-pattern capture + consistency judge (E3).

Covers the card's acceptance criteria:
- AC1: grouping writes a ``FOLDED_INTO`` record with the judge's verbatim
  citation (judge merge) and the exact-key deterministic path (zero extra LLM).
- AC2: false positives are rejected — a citation that does not verbatim-match
  an existing entry coerces the verdict to ``reject`` and no fold is written.
- AC3: no embedding primitives anywhere; the only LLM surface is the
  one-shot auxiliary call on the ``memory_patterns`` config slot.
Plus: safety guardrails (write-time strict scan before any merge), opt-in
default-off, the ``MemoryStore._mutate`` hook end-to-end against a temporary
``HERMES_HOME``, and ledger robustness.
"""

import json

import pytest

import hermes_yaml
from tools import memory_patterns as mp
from tools.memory_tool_store import MemoryStore


CFG = dict(mp.DEFAULTS, enabled=True)

E_EXISTING = (
    "Pour auditer un repo, lancer ruff check puis pygount avant de relire la doc"
)
E_PARAPHRASE = "Avant de relire la documentation d'un repo, exécuter d'abord ruff check puis pygount"
E_SUPERSEDED = "Procédure d'audit repo v2 : ruff check, pygount, puis relecture doc — version corrigée"
E_OTHER = "Le serveur Netdata tourne sur le port 19999 du nœud sud"


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _write_config(home, task_patterns):
    (home / "config.yaml").write_text(
        hermes_yaml.safe_dump({"memory": {"task_patterns": task_patterns}}),
        encoding="utf-8",
    )


def _fake_aux(judge_reply=None, capture_labels=None):
    def fake_aux(system, user, cfg):
        if system is mp.CAPTURE_SYSTEM:
            for entry, label in (capture_labels or {}).items():
                if entry in user:
                    return (
                        json.dumps({"reusable": True, "task_pattern": label}),
                        "aux-test",
                    )
            return (json.dumps({"reusable": False, "task_pattern": ""}), "aux-test")
        return judge_reply if judge_reply is not None else (None, None)

    return fake_aux


class _InlineThread:
    """Thread double that runs the worker inline (deterministic E2E)."""

    def __init__(self, target=None, args=(), **kwargs):
        self._target, self._args = target, args

    def start(self):
        assert self._target is not None
        self._target(*self._args)

    def join(self, *args):
        pass


# ---------------------------------------------------------------------------
# Deterministic label normalisation (zero-LLM)
# ---------------------------------------------------------------------------


def test_normalize_label_deterministic():
    assert mp.normalize_label("Git Stash Pop!") == mp.normalize_label("git  stash  pop")
    assert mp.normalize_label("Stems") == mp.normalize_label("stem")
    assert mp.normalize_label("process") == "process"  # no double-s break
    assert mp.normalize_label("ｒｅａｄ") == mp.normalize_label("read")  # NFKC fold
    assert mp.normalize_label("") == ""


def test_parse_json_object_tolerant():
    assert mp._parse_json_object('{"a": 1}') == {"a": 1}
    assert mp._parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert mp._parse_json_object('Here you go: {"a": 1} done') == {"a": 1}
    assert mp._parse_json_object("no json here") is None
    assert mp._parse_json_object('{"a": 1') is None
    assert mp._parse_json_object(None) is None


# ---------------------------------------------------------------------------
# Capture (LLM 1)
# ---------------------------------------------------------------------------


def test_capture_labels_reusable_procedure(monkeypatch):
    monkeypatch.setattr(
        mp,
        "_aux_call",
        lambda s, u, c: (
            json.dumps({"reusable": True, "task_pattern": "git stash pop"}),
            "aux-test",
        ),
    )
    info, model = mp.capture_task_pattern(
        "Utiliser git stash avant de basculer de branche", CFG
    )
    assert info == {
        "label": "git stash pop",
        "key": mp.normalize_label("git stash pop"),
    }
    assert model == "aux-test"


def test_capture_not_reusable(monkeypatch):
    monkeypatch.setattr(
        mp,
        "_aux_call",
        lambda s, u, c: ('{"reusable": false, "task_pattern": ""}', "aux-test"),
    )
    info, _ = mp.capture_task_pattern("Le bot est mute depuis mardi", CFG)
    assert info is None


def test_capture_rejects_labels_over_six_words(monkeypatch):
    reply = json.dumps({
        "reusable": True,
        "task_pattern": "seven words in this label here today",
    })
    monkeypatch.setattr(mp, "_aux_call", lambda s, u, c: (reply, "aux-test"))
    info, _ = mp.capture_task_pattern("une longue procédure", CFG)
    assert info is None  # deterministic rejection, no second LLM call


def test_capture_aux_failure_is_none(monkeypatch):
    monkeypatch.setattr(mp, "_aux_call", lambda s, u, c: (None, None))
    info, _ = mp.capture_task_pattern("une procédure", CFG)
    assert info is None


# ---------------------------------------------------------------------------
# Exact-key grouping (AC1, deterministic path)
# ---------------------------------------------------------------------------


def test_exact_group_writes_folded_into(tmp_path, monkeypatch):
    lp = tmp_path / "ledger.json"
    # Same label for both entries -> same exact key -> zero-judge grouping.
    monkeypatch.setattr(
        mp,
        "_aux_call",
        lambda s, u, c: (
            json.dumps({"reusable": True, "task_pattern": "repo audit routine"}),
            "aux-test",
        ),
    )
    audit1 = mp.process_entry_sync("memory", E_EXISTING, [], CFG, ledger_path=lp)
    audit2 = mp.process_entry_sync(
        "memory", E_PARAPHRASE, [E_EXISTING], CFG, ledger_path=lp
    )
    assert audit1["decision"] == "new_pattern"
    assert audit2["decision"] == "grouped_exact"
    ledger = mp.load_ledger(lp)
    assert len(ledger["patterns"]) == 1
    pattern = ledger["patterns"][0]
    assert pattern["entries"] == [E_EXISTING, E_PARAPHRASE]
    assert pattern["canonical"] == E_EXISTING  # first entry stays canonical
    folds = [f for f in ledger["folded"] if f["type"] == "FOLDED_INTO"]
    assert len(folds) == 1
    assert folds[0]["by"] == "exact"
    assert folds[0]["from_entry"] == E_PARAPHRASE
    assert folds[0]["into_entry"] == E_EXISTING
    decisions = {d["decision"] for d in ledger["decisions"]}
    assert {"new_pattern", "grouped_exact"} <= decisions


def test_already_grouped_entry_is_not_reprocessed(tmp_path, monkeypatch):
    lp = tmp_path / "ledger.json"
    calls = []

    def fake_aux(system, user, cfg):
        calls.append(system)
        return (
            json.dumps({"reusable": True, "task_pattern": "repo audit routine"}),
            "aux-test",
        )

    monkeypatch.setattr(mp, "_aux_call", fake_aux)
    mp.process_entry_sync("memory", E_EXISTING, [], CFG, ledger_path=lp)
    audit = mp.process_entry_sync("memory", E_EXISTING, [], CFG, ledger_path=lp)
    assert audit["decision"] == "already_grouped"
    # The second run did not call the capture LLM again.
    assert calls == [mp.CAPTURE_SYSTEM]


# ---------------------------------------------------------------------------
# Semantic judge (AC1 / AC2)
# ---------------------------------------------------------------------------


def test_judge_merge_with_verbatim_citation(tmp_path, monkeypatch):
    lp = tmp_path / "ledger.json"
    monkeypatch.setattr(
        mp,
        "_aux_call",
        _fake_aux(
            judge_reply=(
                json.dumps({
                    "verdict": "merge",
                    "cites_entry": E_EXISTING,
                    "reason": "même procédure",
                }),
                "aux-test",
            ),
            capture_labels={
                E_EXISTING: "audit repo routine",
                E_PARAPHRASE: "audit repo doc recheck",
            },
        ),
    )
    mp.process_entry_sync(
        "memory", E_EXISTING, [], CFG, ledger_path=lp
    )  # seeds its own group
    audit = mp.process_entry_sync(
        "memory", E_PARAPHRASE, [E_EXISTING], CFG, ledger_path=lp
    )
    assert audit["decision"] == "folded_merge"
    ledger = mp.load_ledger(lp)
    fold = [f for f in ledger["folded"] if f["by"] == "judge"][0]
    assert fold["type"] == "FOLDED_INTO"
    assert fold["from_entry"] == E_PARAPHRASE
    assert fold["into_entry"] == E_EXISTING
    assert fold["citation"] == E_EXISTING
    assert fold["judge_model"] == "aux-test"
    pattern = [p for p in ledger["patterns"] if E_PARAPHRASE in p["entries"]][0]
    assert pattern["canonical"] == E_EXISTING  # merge keeps the existing canonical


def test_judge_supersede_swaps_canonical(tmp_path, monkeypatch):
    lp = tmp_path / "ledger.json"
    monkeypatch.setattr(
        mp,
        "_aux_call",
        _fake_aux(
            judge_reply=(
                json.dumps({
                    "verdict": "supersede",
                    "cites_entry": E_EXISTING,
                    "reason": "v2 corrigée",
                }),
                "aux-test",
            ),
            capture_labels={
                E_EXISTING: "audit repo routine",
                E_SUPERSEDED: "audit repo v2",
            },
        ),
    )
    mp.process_entry_sync("memory", E_EXISTING, [], CFG, ledger_path=lp)
    audit = mp.process_entry_sync(
        "memory", E_SUPERSEDED, [E_EXISTING], CFG, ledger_path=lp
    )
    assert audit["decision"] == "folded_supersede"
    ledger = mp.load_ledger(lp)
    fold = [f for f in ledger["folded"] if f["by"] == "judge"][0]
    # Supersede: the old wording folds into the new one.
    assert fold["from_entry"] == E_EXISTING
    assert fold["into_entry"] == E_SUPERSEDED
    pattern = [p for p in ledger["patterns"] if E_SUPERSEDED in p["entries"]][0]
    assert pattern["canonical"] == E_SUPERSEDED
    assert set(pattern["entries"]) == {E_EXISTING, E_SUPERSEDED}


def test_judge_invalid_citation_is_rejected(tmp_path, monkeypatch):
    """AC2: a paraphrased (non-verbatim) citation coerces the verdict to reject."""
    lp = tmp_path / "ledger.json"
    monkeypatch.setattr(
        mp,
        "_aux_call",
        _fake_aux(
            judge_reply=(
                json.dumps({
                    "verdict": "merge",
                    "cites_entry": "lancer ruff puis pygount avant doc",
                    "reason": "à peu près la même chose",
                }),
                "aux-test",
            ),
            capture_labels={
                E_EXISTING: "audit repo routine",
                E_PARAPHRASE: "audit repo doc recheck",
            },
        ),
    )
    mp.process_entry_sync("memory", E_EXISTING, [], CFG, ledger_path=lp)
    audit = mp.process_entry_sync(
        "memory", E_PARAPHRASE, [E_EXISTING], CFG, ledger_path=lp
    )
    assert audit["decision"] == "rejected"
    ledger = mp.load_ledger(lp)
    assert not [f for f in ledger["folded"] if f["type"] == "FOLDED_INTO"]
    # The new entry seeds its own pattern group instead of merging.
    own = [p for p in ledger["patterns"] if E_PARAPHRASE in p["entries"]]
    assert len(own) == 1 and own[0]["entries"] == [E_PARAPHRASE]


def test_judge_rejects_different_topic(tmp_path, monkeypatch):
    lp = tmp_path / "ledger.json"
    monkeypatch.setattr(
        mp,
        "_aux_call",
        _fake_aux(
            judge_reply=(
                json.dumps({
                    "verdict": "reject",
                    "cites_entry": "",
                    "reason": "autre sujet",
                }),
                "aux-test",
            ),
            capture_labels={
                E_EXISTING: "audit repo routine",
                E_OTHER: "port netdata sud",
            },
        ),
    )
    mp.process_entry_sync("memory", E_EXISTING, [], CFG, ledger_path=lp)
    audit = mp.process_entry_sync("memory", E_OTHER, [E_EXISTING], CFG, ledger_path=lp)
    assert audit["decision"] == "rejected"
    assert not [f for f in mp.load_ledger(lp)["folded"] if f["type"] == "FOLDED_INTO"]


def test_judge_unparsable_output_rejected(tmp_path, monkeypatch):
    lp = tmp_path / "ledger.json"
    monkeypatch.setattr(
        mp,
        "_aux_call",
        _fake_aux(
            judge_reply=(
                "Je ne peux pas trancher, les deux entrées se ressemblent.",
                "aux-test",
            ),
            capture_labels={
                E_EXISTING: "audit repo routine",
                E_PARAPHRASE: "audit repo doc recheck",
            },
        ),
    )
    mp.process_entry_sync("memory", E_EXISTING, [], CFG, ledger_path=lp)
    audit = mp.process_entry_sync(
        "memory", E_PARAPHRASE, [E_EXISTING], CFG, ledger_path=lp
    )
    assert audit["decision"] == "rejected"
    assert not [f for f in mp.load_ledger(lp)["folded"] if f["type"] == "FOLDED_INTO"]


def test_judge_disabled_seeds_new_pattern(tmp_path, monkeypatch):
    lp = tmp_path / "ledger.json"
    monkeypatch.setattr(
        mp,
        "_aux_call",
        _fake_aux(
            capture_labels={
                E_EXISTING: "audit repo routine",
                E_SUPERSEDED: "audit repo v2",
            }
        ),
    )
    cfg = dict(CFG, judge=False)
    mp.process_entry_sync("memory", E_EXISTING, [], cfg, ledger_path=lp)
    audit = mp.process_entry_sync(
        "memory", E_SUPERSEDED, [E_EXISTING], cfg, ledger_path=lp
    )
    assert audit["decision"] == "new_pattern"
    ledger = mp.load_ledger(lp)
    assert len(ledger["patterns"]) == 2
    assert not [f for f in ledger["folded"] if f["type"] == "FOLDED_INTO"]


def test_no_candidates_skips_judge_and_seeds(tmp_path, monkeypatch):
    lp = tmp_path / "ledger.json"
    calls = []

    def fake_aux(system, user, cfg):
        calls.append(system)
        return (
            json.dumps({"reusable": True, "task_pattern": "audit repo v2"}),
            "aux-test",
        )

    monkeypatch.setattr(mp, "_aux_call", fake_aux)
    audit = mp.process_entry_sync("memory", E_SUPERSEDED, [], CFG, ledger_path=lp)
    assert audit["decision"] == "new_pattern"
    assert calls == [mp.CAPTURE_SYSTEM]  # judge never invoked


# ---------------------------------------------------------------------------
# Safety guardrail (strict write-time scan before any merge)
# ---------------------------------------------------------------------------


def test_safety_screen_blocks_processing_before_llm(tmp_path, monkeypatch):
    lp = tmp_path / "ledger.json"
    calls = []

    def fake_aux(system, user, cfg):
        calls.append(system)
        return ("{}", "aux-test")

    monkeypatch.setattr(mp, "_aux_call", fake_aux)
    dirty = "Entrée\u200bcontaminée"
    audit = mp.process_entry_sync("memory", dirty, [], CFG, ledger_path=lp)
    assert audit["decision"] == "safety_screen"
    assert calls == []  # guardrail fires before any LLM traffic
    ledger = mp.load_ledger(lp)
    assert ledger["patterns"] == [] and ledger["folded"] == []
    assert any(d["decision"] == "safety_screen" for d in ledger["decisions"])


def test_safety_screen_blocks_merge_into_flagged_entry(tmp_path, monkeypatch):
    """A candidate flagged by the strict scan can never be folded into."""
    lp = tmp_path / "ledger.json"
    dirty = "Ancienne\u200bentrée\u0020contaminée"
    monkeypatch.setattr(
        mp,
        "_aux_call",
        _fake_aux(
            judge_reply=(
                json.dumps({
                    "verdict": "merge",
                    "cites_entry": dirty,
                    "reason": "même chose",
                }),
                "aux-test",
            ),
            capture_labels={E_SUPERSEDED: "audit repo v2"},
        ),
    )
    audit = mp.process_entry_sync("memory", E_SUPERSEDED, [dirty], CFG, ledger_path=lp)
    assert audit["decision"] == "safety_screen_blocked_fold"
    ledger = mp.load_ledger(lp)
    assert not [f for f in ledger["folded"] if f["type"] == "FOLDED_INTO"]
    assert any(
        d["decision"] == "safety_screen_blocked_fold" for d in ledger["decisions"]
    )


# ---------------------------------------------------------------------------
# Opt-in (disabled by default)
# ---------------------------------------------------------------------------


def test_disabled_by_default_without_config(hermes_home):
    cfg = mp.get_task_patterns_cfg()
    assert cfg["enabled"] is False
    assert mp.maybe_schedule_capture("memory", [], ["nouvelle entrée"]) is False
    assert not mp.get_ledger_path().exists()


def test_opt_in_flag_false_stays_off(hermes_home):
    _write_config(hermes_home, {"enabled": False})
    assert mp.maybe_schedule_capture("memory", [], ["nouvelle entrée"]) is False
    assert not mp.get_ledger_path().exists()


def test_no_added_entries_spawns_nothing(hermes_home):
    _write_config(hermes_home, {"enabled": True})
    assert mp.maybe_schedule_capture("memory", ["a"], ["a"]) is False


def test_worker_overflow_dropped(monkeypatch, hermes_home):
    _write_config(hermes_home, {"enabled": True})

    class _Full:
        def acquire(self, blocking=True):
            return False

    monkeypatch.setattr(mp, "_WORK_SEMAPHORE", _Full())
    assert mp.maybe_schedule_capture("memory", [], ["nouvelle entrée"]) is False


# ---------------------------------------------------------------------------
# AC3: no embeddings; judge runs on the auxiliary.* slot only
# ---------------------------------------------------------------------------


def test_no_embedding_imports_in_module():
    """AC3: no embedding primitives — the module must not import any vector or
    embedding library; the only LLM surface is the auxiliary-slot call."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(mp))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    for banned in (
        "numpy",
        "scipy",
        "sklearn",
        "sentence_transformers",
        "faiss",
        "torch",
    ):
        assert not any(banned in name.lower() for name in imported)


def test_aux_call_uses_memory_patterns_slot_and_real_extract(monkeypatch):
    import agent.auxiliary_client as ac

    calls = []

    class _Msg:
        content = json.dumps({"reusable": True, "task_pattern": "abc def"})

    class _Choice:
        message = _Msg()

    class _Resp:
        model = "aux-slot-model"
        choices = [_Choice()]

    def fake_call_llm(task=None, messages=None, **kwargs):
        calls.append((task, messages))
        return _Resp()

    monkeypatch.setattr(ac, "call_llm", fake_call_llm)
    info, model = mp.capture_task_pattern("une procédure", CFG)
    assert len(calls) == 1
    task, messages = calls[0]
    assert task == "memory_patterns"
    assert [m["role"] for m in messages] == ["system", "user"]
    assert info == {"label": "abc def", "key": "abc def"}
    assert model == "aux-slot-model"


# ---------------------------------------------------------------------------
# End-to-end: MemoryStore._mutate hook (real path, temp HERMES_HOME)
# ---------------------------------------------------------------------------


def test_store_hook_groups_added_entries_and_leaves_memory_files_untouched(
    hermes_home, monkeypatch
):
    _write_config(hermes_home, {"enabled": True})
    monkeypatch.setattr(mp.threading, "Thread", _InlineThread)
    monkeypatch.setattr(
        mp,
        "_aux_call",
        lambda s, u, c: (
            json.dumps({"reusable": True, "task_pattern": "repo audit routine"}),
            "aux-test",
        ),
    )
    store = MemoryStore()
    assert store.add("memory", E_EXISTING)["success"]
    assert store.add("memory", E_PARAPHRASE)["success"]
    # The memory files contain exactly the two entries — the feature never
    # rewrites them.
    assert store._read_file(mp.get_memory_dir() / "MEMORY.md") == [
        E_EXISTING,
        E_PARAPHRASE,
    ]
    # The sidecar ledger captured the deterministic group.
    ledger = mp.load_ledger(mp.get_ledger_path())
    assert len(ledger["patterns"]) == 1
    assert ledger["patterns"][0]["entries"] == [E_EXISTING, E_PARAPHRASE]
    assert ledger["patterns"][0]["target"] == "memory"
    fold = [f for f in ledger["folded"] if f["type"] == "FOLDED_INTO"][0]
    assert fold["by"] == "exact"
    assert fold["from_entry"] == E_PARAPHRASE and fold["into_entry"] == E_EXISTING


def test_store_hook_replace_feeds_old_wording_to_judge(hermes_home, monkeypatch):
    """A replace keeps the old wording in `previous`, so supersede can fold it."""
    _write_config(hermes_home, {"enabled": True})
    monkeypatch.setattr(mp.threading, "Thread", _InlineThread)
    seen = {}

    def fake_aux(system, user, cfg):
        if system is mp.CAPTURE_SYSTEM:
            if "version corrigée" in user:
                return (
                    json.dumps({"reusable": True, "task_pattern": "audit repo v2"}),
                    "aux-test",
                )
            return (
                json.dumps({"reusable": True, "task_pattern": "audit repo routine"}),
                "aux-test",
            )
        seen["user"] = user
        assert E_EXISTING in user  # the old wording is a visible candidate
        return (
            json.dumps({
                "verdict": "supersede",
                "cites_entry": E_EXISTING,
                "reason": "v2",
            }),
            "aux-test",
        )

    monkeypatch.setattr(mp, "_aux_call", fake_aux)
    store = MemoryStore()
    assert store.add("memory", E_EXISTING)["success"]
    assert store.replace("memory", E_EXISTING, E_SUPERSEDED)["success"]
    # Both wordings stay in memory (E2 does the demotion) — the ledger records
    # the supersede with citation.
    assert store._read_file(mp.get_memory_dir() / "MEMORY.md") == [E_SUPERSEDED]
    ledger = mp.load_ledger(mp.get_ledger_path())
    fold = [f for f in ledger["folded"] if f["by"] == "judge"][0]
    assert fold["verdict"] == "supersede"
    assert fold["from_entry"] == E_EXISTING and fold["into_entry"] == E_SUPERSEDED
    assert fold["citation"] == E_EXISTING
    pattern = [p for p in ledger["patterns"] if E_SUPERSEDED in p["entries"]][0]
    assert pattern["canonical"] == E_SUPERSEDED


def test_store_write_unaffected_when_disabled(hermes_home):
    _write_config(hermes_home, {"enabled": False})
    store = MemoryStore()
    assert store.add("memory", "une entrée ordinaire")["success"]
    assert not mp.get_ledger_path().exists()


def test_store_write_robust_to_patterns_module_failure(hermes_home, monkeypatch):
    """The capture side channel can never break a memory write."""
    _write_config(hermes_home, {"enabled": True})

    def boom(*args, **kwargs):
        raise RuntimeError("capture exploded")

    monkeypatch.setattr(mp, "maybe_schedule_capture", boom)
    store = MemoryStore()
    assert store.add("memory", "une entrée")["success"]
    assert store._read_file(mp.get_memory_dir() / "MEMORY.md") == ["une entrée"]


# ---------------------------------------------------------------------------
# Ledger robustness
# ---------------------------------------------------------------------------


def test_ledger_recovers_from_corruption(tmp_path):
    lp = tmp_path / "ledger.json"
    lp.write_text("{{{not json", encoding="utf-8")
    assert mp.load_ledger(lp) == mp._empty_ledger()


def test_ledger_round_trip(tmp_path):
    lp = tmp_path / "ledger.json"
    ledger = mp._empty_ledger()
    mp._create_pattern(ledger, "repo audit", "Repo audit", E_EXISTING, "memory")
    mp._add_folded(
        ledger,
        E_PARAPHRASE,
        E_EXISTING,
        "repo audit",
        by="judge",
        verdict="merge",
        citation=E_EXISTING,
        judge_model="aux-test",
    )
    mp.save_ledger(ledger, lp)
    reloaded = mp.load_ledger(lp)
    assert reloaded["patterns"][0]["label"] == "Repo audit"
    assert reloaded["folded"][0]["citation"] == E_EXISTING


def test_decision_log_is_capped(tmp_path):
    ledger = mp._empty_ledger()
    for i in range(mp.DECISIONS_MAX_RECORDS + 50):
        mp._record_decision(ledger, f"entrée {i}", "rejected")
    assert len(ledger["decisions"]) == mp.DECISIONS_MAX_RECORDS
    assert ledger["decisions"][-1]["entry"].endswith("49")


def test_capture_disabled_in_config_still_processes_nothing(tmp_path):
    """capture=false: entries are never labeled and never grouped."""
    lp = tmp_path / "ledger.json"
    calls = []

    def fake_aux(system, user, cfg):
        calls.append(system)
        return ("{}", "aux-test")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(mp, "_aux_call", fake_aux)
    try:
        audit = mp.process_entry_sync(
            "memory", E_EXISTING, [], dict(CFG, capture=False), ledger_path=lp
        )
    finally:
        monkeypatch.undo()
    assert audit["decision"] == "capture_disabled"
    assert calls == []
    ledger = mp.load_ledger(lp)
    assert ledger["patterns"] == [] and not [
        f for f in ledger["folded"] if f["type"] == "FOLDED_INTO"
    ]
