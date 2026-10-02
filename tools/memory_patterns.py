"""Task-pattern capture + consistency judge for memory entries (E3).

Captures a short ``task_pattern`` label (<= 6 words) for persistent memory
entries that describe a reusable procedure, groups entries by exact-normalised
label (deterministic, zero-LLM), and — for paraphrases the exact key cannot
join — asks an auxiliary LLM to decide ``merge`` / ``supersede`` / ``reject``
with a mandatory citation of the entry it matches.  No embeddings, no cosine:
the only LLM traffic is a one-shot :func:`agent.auxiliary_client.call_llm`
call on the ``auxiliary.memory_patterns`` config slot, so memory content never
leaves the auxiliary runtime (no third-party egress).

The module only writes the sidecar ledger ``memories/.memory_patterns.json``
(pattern groups + ``FOLDED_INTO`` records + audit decisions).  It never
touches ``MEMORY.md`` / ``USER.md`` — demoting entries out of the budget is
the curator uplift pass' job (E2), which reads this ledger and promotes a
pattern group to a *proposed* skill once it holds at least two folded
fragments.

Opt-in: ``memory.task_patterns.enabled`` (default ``false``).  Rollback is
``enabled: false`` — the sidecar simply stops growing.
"""

import json
import logging
import os
import re
import threading
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tools.memory_tool import get_memory_dir
from utils import atomic_write_text, is_truthy_value

logger = logging.getLogger(__name__)

AUX_TASK = "memory_patterns"
LEDGER_FILENAME = ".memory_patterns.json"
LEDGER_VERSION = 1
MAX_PATTERN_WORDS = 6
FOLDED_MAX_RECORDS = 500
DECISIONS_MAX_RECORDS = 200

DEFAULTS: Dict[str, Any] = {
    "capture": True,
    "judge": True,
    "max_judge_candidates": 12,
    "entry_excerpt_chars": 0,  # 0 = no truncation (entries are budget-bounded)
    "timeout_seconds": 20,
    "max_tokens": 220,
}

_WORK_SEMAPHORE = threading.BoundedSemaphore(2)

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")

CAPTURE_SYSTEM = """You are a memory librarian for an AI assistant. You receive one persistent memory entry.
Decide whether it describes a REUSABLE PROCEDURE: a repeatable how-to (commands to run,
steps to follow, a recipe, a pitfall with a workaround). Plain facts, preferences,
identities and environment notes are NOT procedures.
If it is a procedure, produce a task_pattern label of AT MOST 6 words (no trailing
punctuation, in the entry's language) that names the procedure.
Respond with ONLY minified JSON: {"reusable": true, "task_pattern": "label"} or
{"reusable": false, "task_pattern": ""}"""

JUDGE_SYSTEM = """You are a consistency judge for the persistent memory of an AI assistant.
You receive ONE new entry and a numbered list of EXISTING entries (the new entry is NOT
in the list). Classify the new entry against the existing ones:
- "merge": it states the SAME procedure as one existing entry, in different words.
- "supersede": it states the SAME procedure but is the newer, corrected or more complete
  version of one existing entry.
- "reject": it is a different topic or a different procedure than every existing entry.
For "merge" and "supersede" you MUST copy the full text of the matched existing entry
VERBATIM (character for character, whitespace may collapse) into "cites_entry". Copy it
from the EXISTING list only — never from the new entry.
Respond with ONLY minified JSON: {"verdict": "merge"|"supersede"|"reject", "cites_entry":
"verbatim text", "reason": "one short sentence"}"""


# ---------------------------------------------------------------------------
# Label normalisation (deterministic, zero-LLM)
# ---------------------------------------------------------------------------


def normalize_label(text: str) -> str:
    """Deterministic exact-key for a pattern label.

    Lowercase, NFKC-fold, strip punctuation, collapse whitespace, and strip a
    single trailing ``s`` from words of length >= 4 that do not end in ``ss``
    (light surface stemming: ``stems``/``stem`` collide, ``process`` is kept).
    Determinism is the contract; imperfect collisions simply fall through to
    the semantic judge.
    """
    t = unicodedata.normalize("NFKC", text or "").lower()
    t = _PUNCT_RE.sub(" ", t)
    t = _WS_RE.sub(" ", t).strip()
    words = []
    for word in t.split(" "):
        if len(word) >= 4 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        words.append(word)
    return " ".join(words)


def _norm_ws(text: str) -> str:
    return _WS_RE.sub(" ", text or "").strip()


def _display(entry: str, cfg: Dict[str, Any]) -> str:
    limit = cfg.get("entry_excerpt_chars") or 0
    if limit and len(entry) > limit:
        return entry[:limit].rstrip() + " …"
    return entry


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def get_task_patterns_cfg() -> Dict[str, Any]:
    """Resolve the ``memory.task_patterns`` section (defaults off)."""
    cfg: Dict[str, Any] = dict(DEFAULTS)
    cfg["enabled"] = False
    try:
        from hermes_cli.config import load_config_readonly

        root = load_config_readonly() or {}
    except Exception:
        return cfg
    section = (root.get("memory") or {}).get("task_patterns")
    if not isinstance(section, dict):
        return cfg
    for key in DEFAULTS:
        if key in section:
            cfg[key] = section[key]
    cfg["enabled"] = is_truthy_value(section.get("enabled"), default=False)
    cfg["capture"] = is_truthy_value(section.get("capture", True), default=True)
    cfg["judge"] = is_truthy_value(section.get("judge", True), default=True)
    return cfg


# ---------------------------------------------------------------------------
# Sidecar ledger (memories/.memory_patterns.json)
# ---------------------------------------------------------------------------


def get_ledger_path() -> Path:
    return get_memory_dir() / LEDGER_FILENAME


def _empty_ledger() -> Dict[str, Any]:
    return {"version": LEDGER_VERSION, "patterns": [], "folded": [], "decisions": []}


def load_ledger(path: Optional[Path] = None) -> Dict[str, Any]:
    path = path or get_ledger_path()
    if not path.exists():
        return _empty_ledger()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("memory patterns: unreadable ledger %s; starting fresh", path)
        return _empty_ledger()
    if not isinstance(data, dict):
        return _empty_ledger()
    ledger = _empty_ledger()
    for key in ("patterns", "folded", "decisions"):
        if isinstance(data.get(key), list):
            ledger[key] = [x for x in data[key] if isinstance(x, dict)]
    return ledger


def save_ledger(ledger: Dict[str, Any], path: Optional[Path] = None) -> None:
    path = path or get_ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path, json.dumps(ledger, ensure_ascii=False, indent=2), tmp_prefix=".mp_"
    )


@contextmanager
def _ledger_locked(path: Optional[Path] = None):
    """Exclusive advisory lock for ledger read-modify-write (cross-worker)."""
    path = path or get_ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    if os.name == "nt":
        import msvcrt

        with open(lock_path, "a+", encoding="utf-8") as fh:
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        with open(lock_path, "a+", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _apply(ledger_path: Optional[Path], mutate) -> None:
    """Load the ledger, apply ``mutate(ledger)``, save — under the ledger lock."""
    with _ledger_locked(ledger_path):
        ledger = load_ledger(ledger_path)
        mutate(ledger)
        save_ledger(ledger, ledger_path)


def _record_decision(
    ledger: Dict[str, Any],
    entry: str,
    decision: str,
    reason: str = "",
    judge_model: Optional[str] = None,
) -> None:
    record = {
        "entry": _clip(entry, 200),
        "decision": decision,
        "reason": reason[:300],
        "at": time.time(),
    }
    if judge_model:
        record["judge_model"] = judge_model
    ledger["decisions"].append(record)
    del ledger["decisions"][:-DECISIONS_MAX_RECORDS]


def _add_folded(
    ledger: Dict[str, Any],
    from_entry: str,
    into_entry: str,
    into_key: str,
    by: str,
    verdict: str,
    citation: Optional[str] = None,
    judge_model: Optional[str] = None,
    reason: Optional[str] = None,
) -> None:
    record: Dict[str, Any] = {
        "type": "FOLDED_INTO",
        "from_entry": from_entry,
        "into_entry": into_entry,
        "into_key": into_key,
        "by": by,
        "verdict": verdict,
        "at": time.time(),
    }
    if citation is not None:
        record["citation"] = citation
    if judge_model:
        record["judge_model"] = judge_model
    if reason:
        record["reason"] = reason[:300]
    ledger["folded"].append(record)
    del ledger["folded"][:-FOLDED_MAX_RECORDS]


def _find_pattern(ledger: Dict[str, Any], key: str) -> Optional[Dict[str, Any]]:
    for pattern in ledger["patterns"]:
        if pattern.get("key") == key:
            return pattern
    return None


def _create_pattern(
    ledger: Dict[str, Any], key: str, label: str, canonical: str, target: str
) -> Dict[str, Any]:
    pattern = {
        "key": key,
        "label": label,
        "target": target,
        "entries": [canonical],
        "canonical": canonical,
        "created": time.time(),
        "updated": time.time(),
    }
    ledger["patterns"].append(pattern)
    return pattern


# ---------------------------------------------------------------------------
# Safety guardrail (reuse of the write-time strict scan)
# ---------------------------------------------------------------------------


def safety_screen(texts: List[str]) -> Optional[str]:
    """Return the first threat message among ``texts`` (strict scope), else None.

    Reuses ``_scan_memory_content`` — the same strict-scope ``threat_patterns``
    scan the store applies at write time — so the judge can never fold a
    flagged entry into a group (and never fold *into* a flagged one).
    """
    from tools.memory_tool_store import _scan_memory_content

    for text in texts:
        if text:
            message = _scan_memory_content(text)
            if message:
                return message
    return None


# ---------------------------------------------------------------------------
# Auxiliary LLM calls (only egress: the auxiliary runtime, never third parties)
# ---------------------------------------------------------------------------


def _aux_call(
    system: str, user: str, cfg: Dict[str, Any]
) -> Tuple[Optional[str], Optional[str]]:
    """One-shot aux call; returns (content, model) or (None, None) on any failure."""
    try:
        from agent.auxiliary_client import call_llm, extract_content_or_reasoning

        response = call_llm(
            task=AUX_TASK,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            max_tokens=int(cfg.get("max_tokens") or 220),
            timeout=float(cfg.get("timeout_seconds") or 20),
        )
    except Exception as exc:
        logger.debug("memory patterns: aux call failed: %s", exc)
        return None, None
    content = extract_content_or_reasoning(response)
    if not content:
        return None, None
    model: Optional[str] = None
    if isinstance(response, dict):
        model = response.get("model")
    else:
        model = getattr(response, "model", None)
    if not isinstance(model, str) or not model:
        model = None
    return content, model


def _parse_json_object(text: Optional[str]) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t).strip()
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    start, end = t.find("{"), t.rfind("}")
    if 0 <= start < end:
        try:
            obj = json.loads(t[start : end + 1])
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def capture_task_pattern(
    entry: str, cfg: Dict[str, Any]
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Label one entry.  Returns ({"label", "key"} or None, model)."""
    content, model = _aux_call(CAPTURE_SYSTEM, f'Entry:\n"""\n{entry}\n"""', cfg)
    obj = _parse_json_object(content)
    if not obj or not obj.get("reusable"):
        return None, model
    label = str(obj.get("task_pattern") or "").strip()
    if not label:
        return None, model
    if len(label.split()) > MAX_PATTERN_WORDS:
        # Over-long labels are rejected deterministically (no second LLM call).
        logger.debug("memory patterns: label over %d words, dropped", MAX_PATTERN_WORDS)
        return None, model
    key = normalize_label(label)
    if not key:
        return None, model
    return {"label": label, "key": key}, model


def judge_consistency(
    new_entry: str, candidates: List[str], cfg: Dict[str, Any]
) -> Dict[str, Any]:
    """Semantic judge (merge/supersede/reject) with mandatory verbatim citation.

    A citation that does not verbatim-match one of the candidates (the exact
    text the model was shown) coerces the verdict to ``reject`` — the safe
    default, and the AC2 false-positive guard.
    """
    result: Dict[str, Any] = {
        "verdict": "reject",
        "cites_entry": "",
        "reason": "",
        "matched": None,
        "model": None,
    }
    lines = [
        f'[{i}] """\n{_display(cand, cfg)}\n"""' for i, cand in enumerate(candidates, 1)
    ]
    user = (
        f'NEW ENTRY:\n"""\n{_display(new_entry, cfg)}\n"""\n\n'
        f"EXISTING ENTRIES:\n" + "\n\n".join(lines)
    )
    content, model = _aux_call(JUDGE_SYSTEM, user, cfg)
    result["model"] = model
    obj = _parse_json_object(content)
    if not obj:
        result["reason"] = "judge returned unparsable output"
        return result
    verdict = str(obj.get("verdict") or "reject").lower()
    if verdict not in ("merge", "supersede", "reject"):
        verdict = "reject"
    result["verdict"] = verdict
    result["reason"] = str(obj.get("reason") or "")[:300]
    if verdict == "reject":
        return result
    norm_cites = _norm_ws(str(obj.get("cites_entry") or ""))
    if norm_cites:
        for cand in candidates:
            if norm_cites == _norm_ws(_display(cand, cfg)):
                result["cites_entry"] = str(obj.get("cites_entry") or "")
                result["matched"] = {"entry": cand}
                return result
    # AC2: a merge/supersede verdict whose citation is missing or does not
    # verbatim-match a candidate is a false positive — coerce to the safe
    # default so no fold can ever be written from an unverified citation.
    result["verdict"] = "reject"
    result["reason"] = "merge/supersede without a verbatim citation (coerced to reject)"
    return result


# ---------------------------------------------------------------------------
# Entry processing (worker body — synchronous, ledger-only writes)
# ---------------------------------------------------------------------------


def process_entry_sync(
    target: str,
    entry: str,
    previous_entries: List[str],
    cfg: Dict[str, Any],
    ledger_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Capture + exact-group + judge one newly added/changed memory entry.

    ``previous_entries`` is the pre-change entry list of the target; it is the
    candidate population for the judge (for a replace, it still contains the
    old wording, which is exactly the supersede case).  Only the sidecar
    ledger is written.  Returns an audit dict
    (``{"entry", "decision", "reason"}``).
    """
    audit: Dict[str, Any] = {
        "entry": _clip(entry, 200),
        "decision": "skipped",
        "reason": "",
    }
    candidates = [e for e in previous_entries if e.strip() and e != entry]

    def done(decision: str, reason: str = "") -> Dict[str, Any]:
        audit["decision"], audit["reason"] = decision, reason
        return audit

    # Guardrail: an entry the write-time scan would have flagged is never
    # processed or folded (even if it entered memory via an older version).
    threat = safety_screen([entry])
    if threat:

        def screen_mutate(ledger: Dict[str, Any]) -> None:
            _record_decision(ledger, entry, "safety_screen", threat)

        _apply(ledger_path, screen_mutate)
        return done("safety_screen", threat)

    if not cfg.get("capture"):

        def disabled_mutate(ledger: Dict[str, Any]) -> None:
            _record_decision(
                ledger, entry, "capture_disabled", "capture is disabled in config"
            )

        _apply(ledger_path, disabled_mutate)
        return done("capture_disabled", "capture is disabled in config")

    # Already grouped in a previous run?  No second capture, no double fold.
    with _ledger_locked(ledger_path):
        ledger = load_ledger(ledger_path)
    if any(entry in p.get("entries", []) for p in ledger["patterns"]):
        return done("already_grouped")

    label_info, capture_model = capture_task_pattern(entry, cfg)
    if label_info is None:

        def no_label_mutate(ledger: Dict[str, Any]) -> None:
            _record_decision(
                ledger,
                entry,
                "not_reusable_or_capture_error",
                "no label produced",
                capture_model,
            )

        _apply(ledger_path, no_label_mutate)
        return done("not_reusable_or_capture_error", "no label produced")

    key, label = label_info["key"], label_info["label"]

    # Exact key hit: deterministic grouping, zero extra LLM.
    with _ledger_locked(ledger_path):
        ledger = load_ledger(ledger_path)
        has_exact_group = _find_pattern(ledger, key) is not None
    if has_exact_group:

        def exact_mutate(ledger: Dict[str, Any]) -> None:
            pattern = _find_pattern(ledger, key)
            if pattern is None or entry in pattern.get("entries", []):
                return
            pattern["entries"].append(entry)
            pattern["updated"] = time.time()
            canonical = pattern.get("canonical") or pattern["entries"][0]
            _add_folded(ledger, entry, canonical, key, by="exact", verdict="merge")
            _record_decision(ledger, entry, "grouped_exact", f"pattern '{label}'")

        _apply(ledger_path, exact_mutate)
        return done("grouped_exact", f"pattern '{label}'")

    # No exact hit → semantic judge (only when enabled and candidates exist).
    if cfg.get("judge") and candidates:
        limit = int(cfg.get("max_judge_candidates") or 12)
        judge = judge_consistency(entry, candidates[:limit], cfg)

        if judge["verdict"] == "reject":

            def reject_mutate(ledger: Dict[str, Any]) -> None:
                if _find_pattern(ledger, key) is None:
                    _create_pattern(ledger, key, label, entry, target)
                _record_decision(
                    ledger, entry, "rejected", judge["reason"], judge["model"]
                )

            _apply(ledger_path, reject_mutate)
            return done("rejected", judge["reason"])

        matched_entry = judge["matched"]["entry"]
        # Guardrail: re-screen the pair before writing any FOLDED_INTO.
        pair_threat = safety_screen([entry, matched_entry])
        if pair_threat:

            def blocked_mutate(ledger: Dict[str, Any]) -> None:
                _record_decision(
                    ledger,
                    entry,
                    "safety_screen_blocked_fold",
                    pair_threat,
                    judge["model"],
                )

            _apply(ledger_path, blocked_mutate)
            return done("safety_screen_blocked_fold", pair_threat)

        def fold_mutate(ledger: Dict[str, Any]) -> None:
            group = None
            for p in ledger["patterns"]:
                if matched_entry in p.get("entries", []):
                    group = p
                    break
            if group is None:
                group = _create_pattern(ledger, key, label, matched_entry, target)
                group["entries"].append(entry)
            elif entry not in group["entries"]:
                group["entries"].append(entry)
            group["updated"] = time.time()
            if judge["verdict"] == "supersede":
                group["canonical"] = entry  # new wording takes over
            if judge["verdict"] == "merge":
                from_entry, into_entry = entry, matched_entry
            else:
                from_entry, into_entry = matched_entry, entry
            _add_folded(
                ledger,
                from_entry,
                into_entry,
                group["key"],
                by="judge",
                verdict=judge["verdict"],
                citation=judge.get("cites_entry"),
                judge_model=judge["model"],
                reason=judge["reason"],
            )
            _record_decision(
                ledger,
                entry,
                f"folded_{judge['verdict']}",
                judge["reason"],
                judge["model"],
            )

        _apply(ledger_path, fold_mutate)
        return done(f"folded_{judge['verdict']}", judge["reason"])

    # Judge disabled or no candidates: seed a fresh pattern group.
    def seed_mutate(ledger: Dict[str, Any]) -> None:
        if _find_pattern(ledger, key) is None:
            _create_pattern(ledger, key, label, entry, target)
        _record_decision(ledger, entry, "new_pattern", f"pattern '{label}'")

    _apply(ledger_path, seed_mutate)
    return done("new_pattern")


# ---------------------------------------------------------------------------
# Scheduling (store hook surface — fire and forget, never raises)
# ---------------------------------------------------------------------------


def maybe_schedule_capture(
    target: str, previous_entries: List[str], current_entries: List[str]
) -> bool:
    """Spawn a bounded background worker for newly added/changed entries.

    Returns True when a worker was spawned.  The worker is a daemon thread,
    capped at 2 concurrent workers (BoundedSemaphore); overflow is dropped
    with a debug log — pattern capture is advisory and must never delay or
    fail a memory write.
    """
    added = [e for e in current_entries if e not in previous_entries]
    if not added:
        return False
    cfg = get_task_patterns_cfg()
    if not cfg.get("enabled"):
        return False
    if not _WORK_SEMAPHORE.acquire(blocking=False):
        logger.debug(
            "memory patterns: work queue full; dropping capture for %d entr%s",
            len(added),
            "y" if len(added) == 1 else "ies",
        )
        return False
    thread = threading.Thread(
        target=_run_capture_worker,
        args=(target, list(added), list(previous_entries), cfg),
        name="memory-patterns",
        daemon=True,
    )
    thread.start()
    return True


def _run_capture_worker(
    target: str, added: List[str], previous_entries: List[str], cfg: Dict[str, Any]
) -> None:
    try:
        for entry in added:
            try:
                audit = process_entry_sync(target, entry, previous_entries, cfg)
                logger.debug(
                    "memory patterns: %s (%s)", audit["decision"], audit["entry"]
                )
            except Exception:
                logger.exception(
                    "memory patterns: worker failed for one entry; continuing"
                )
    finally:
        _WORK_SEMAPHORE.release()
