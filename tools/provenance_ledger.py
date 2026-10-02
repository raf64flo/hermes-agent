"""Provenance ledger — SQLite backlinks from memory / user-profile / session sources to the
skills and profile fields they informed, so a source retraction propagates as a ``stale``
flag instead of a silent loss.

A row answers "this memory entry (or user-profile bullet, or session) informed this
skill / profile field via *relation*":

- E2 (memory→skill promotion) records a ``FOLDED_INTO`` link when it folds an entry into
  a skill.
- E4 (profile edits) records an ``INFORMED_BY`` link when an entry shaped a profile bullet.
- The memory store retracts links (stale-on-retract): the ``MemoryStore._mutate`` hook in
  ``tools/memory_tool_store.py`` calls :func:`retract_entries` for every entry a
  remove/replace write takes away, and the target's ``stale`` flag becomes the re-review
  queue a human (or E2/E4) clears with :func:`clear_stale`.

Two established patterns are reused, deliberately:

- ``agent/curator_backup.py`` (atomic-write model): a corrupted ledger file is moved aside
  as ``<file>.bak.<ts>.sqlite`` before the schema is recreated, so the corrupted artifact
  is never destroyed; no public call below ever raises.
- ``tools/skill_ledger.py`` (fail-open telemetry): config-gated and resolved at call time
  so one process can serve many profiles; every disabled/failed path returns an empty
  result. The ledger is an audit + advisory signal — never a gate on a memory write.

Storage: ``provenance.ledger_path`` in the profile's ``config.yaml`` (an absolute path or
a ``~``-expanded path). Scope is per profile: each profile's config points at its own
file, and the file's single ``provenance`` table is that profile's ledger. The path is
the operator's choice (e.g. inside the versioned skills repo, not under the memory dir).

``source_id`` / ``target_id`` conventions:

- memory / user sources: :func:`entry_source_id` — sha256 of the stripped entry text.
- session sources: the session id.
- skill targets: the skill slug.
- profile-field targets: :func:`entry_source_id` of the field / bullet text.

``revoked_at`` is the audit record — set once, never cleared. ``stale`` is the advisory
flag set together with it; a reviewer clears it with :func:`clear_stale` after checking
the target. Clearing never touches the target file itself (no automatic rewrite).
"""

import hashlib
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("tools.provenance_ledger")

SOURCE_TYPES = ("memory", "user", "session")
RELATIONS = ("DERIVED_FROM", "INFORMED_BY", "FOLDED_INTO")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS provenance (
    source_id   TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK (source_type IN ('memory', 'user', 'session')),
    target_id   TEXT NOT NULL,
    relation    TEXT NOT NULL CHECK (relation IN ('DERIVED_FROM', 'INFORMED_BY', 'FOLDED_INTO')),
    created_at  TEXT NOT NULL,
    revoked_at  TEXT,
    source_text TEXT,
    stale       INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_provenance_link
    ON provenance (source_id, source_type, target_id, relation);
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def entry_source_id(text: str) -> str:
    """Stable id of a memory / user-profile entry (or profile-field text): sha256 of the
    stripped text, so surrounding whitespace never splits one source into two ids."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def ledger_path() -> Optional[Path]:
    """``provenance.ledger_path`` from the active profile's config, or ``None`` (off).

    ``None`` also when the configured path is empty / not a string (the default is an empty
    string: ledger OFF) or not absolute after ``~`` expansion: a relative path would resolve
    against the process CWD and differ per caller. Resolved at call time (never a module
    constant) so a multiplexing process binds each call's profile scope.
    """
    try:
        from hermes_cli.config import cfg_get, load_config_readonly
        raw = cfg_get(load_config_readonly(), "provenance", "ledger_path", default=None)
    except Exception:
        return None
    if not isinstance(raw, str) or not raw.strip():
        return None
    path = Path(os.path.expanduser(raw.strip()))
    if not path.is_absolute():
        return None
    return path.resolve()


def _backup_corrupt(path: Path, reason: Exception) -> bool:
    """Move a corrupt ledger aside as ``<name>.bak.<ts>.sqlite`` (curator backup model):
    preserve the corrupted artifact for forensics; ``True`` when the move landed."""
    bak = path.with_name(path.name + f".bak.{int(time.time())}.sqlite")
    try:
        path.replace(bak)
        for suffix in ("-wal", "-shm"):
            side = path.with_name(path.name + suffix)
            if side.exists():
                side.unlink()
    except OSError as e:
        logger.warning("Provenance ledger %s corrupt (%s) and the backup failed (%s); "
                       "leaving the file untouched", path, reason, e)
        return False
    logger.warning("Provenance ledger %s corrupt (%s); moved to %s and the schema was "
                   "recreated (prior rows are unrecoverable)", path, reason, bak)
    return True


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def _connect(path: Path) -> Optional[sqlite3.Connection]:
    """Open *path* with the schema ready, or ``None`` when the ledger is unusable.

    A file that fails ``integrity_check`` — or that makes ``connect`` / the first
    statement raise ("file is not a database") — is backed up (or left untouched if
    even the backup fails) and the schema recreated: the caller always gets a working
    ledger or a clean fail-open ``None``, never an exception.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = None
    try:
        conn = _open(path)
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise sqlite3.DatabaseError("integrity check failed")
    except (sqlite3.DatabaseError, sqlite3.OperationalError) as e:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        if not _backup_corrupt(path, e):
            return None
        try:
            conn = _open(path)
            conn.executescript(_SCHEMA)
            return conn
        except sqlite3.Error as e2:
            logger.warning("Provenance ledger %s unusable (%s); call skipped", path, e2)
            return None
    conn.executescript(_SCHEMA)
    return conn


def record(source_id: str, source_type: str, target_id: str, relation: str,
           *, source_text: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Register (or revive) a provenance link; the stored row as a dict, or ``None`` when
    the ledger is off / unusable / the enums are invalid. Re-recording a revoked link
    revives it (the source was re-added or re-folded): ``revoked_at`` and ``stale``
    reset. Idempotent on the (source_id, source_type, target_id, relation) key."""
    if source_type not in SOURCE_TYPES or relation not in RELATIONS:
        logger.warning("Provenance record ignored: bad source_type=%r or relation=%r",
                       source_type, relation)
        return None
    path = ledger_path()
    if path is None:
        return None
    conn = _connect(path)
    if conn is None:
        return None
    try:
        conn.execute(
            """INSERT INTO provenance (source_id, source_type, target_id, relation,
                                       created_at, source_text)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT (source_id, source_type, target_id, relation) DO UPDATE SET
                   revoked_at = NULL,
                   stale = 0,
                   source_text = COALESCE(excluded.source_text, provenance.source_text)""",
            (source_id, source_type, target_id, relation, _now(), source_text))
        conn.commit()
        row = conn.execute(
            "SELECT * FROM provenance WHERE source_id = ? AND source_type = ? "
            "AND target_id = ? AND relation = ?",
            (source_id, source_type, target_id, relation)).fetchone()
        return dict(row)
    except sqlite3.Error as e:
        logger.warning("Provenance record failed (%s): %s", path, e)
        return None
    finally:
        conn.close()


def retract(source_id: str, source_type: str) -> List[str]:
    """Mark every unrevoked link of *source_id* revoked (``revoked_at``) and stale.
    Returns the distinct affected ``target_id``s; ``[]`` when the ledger is off,
    unusable, or the source had no live link. Idempotent: an already-revoked link is
    left byte-identical (the ``revoked_at IS NULL`` guard), so a second call returns
    ``[]``."""
    if source_type not in SOURCE_TYPES:
        return []
    path = ledger_path()
    if path is None:
        return []
    conn = _connect(path)
    if conn is None:
        return []
    try:
        now = _now()
        cur = conn.execute(
            "UPDATE provenance SET revoked_at = ?, stale = 1 "
            "WHERE source_id = ? AND source_type = ? AND revoked_at IS NULL",
            (now, source_id, source_type))
        conn.commit()
        if not cur.rowcount:
            return []
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT target_id FROM provenance "
            "WHERE source_id = ? AND source_type = ? AND revoked_at = ?",
            (source_id, source_type, now))]
    except sqlite3.Error as e:
        logger.warning("Provenance retract failed (%s): %s", path, e)
        return []
    finally:
        conn.close()


def retract_entries(entries: List[str], source_type: str) -> List[str]:
    """The memory-store hook entry point: :func:`retract` over the stable id of each
    entry text a write removed or replaced away. Union of the affected targets,
    deduplicated, in first-hit order."""
    affected: List[str] = []
    for entry in entries:
        if not entry or not entry.strip():
            continue
        for target in retract(entry_source_id(entry), source_type):
            if target not in affected:
                affected.append(target)
    return affected


def rows_for_target(target_id: str) -> List[Dict[str, Any]]:
    """Every link pointing at *target_id* (``SELECT *`` shape), oldest first; ``[]`` when
    the ledger is off / unusable."""
    path = ledger_path()
    if path is None:
        return []
    conn = _connect(path)
    if conn is None:
        return []
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM provenance WHERE target_id = ? ORDER BY created_at",
            (target_id,))]
    except sqlite3.Error as e:
        logger.warning("Provenance rows_for_target failed (%s): %s", path, e)
        return []
    finally:
        conn.close()


def stale_targets() -> List[Dict[str, Any]]:
    """Targets whose provenance was retracted and not yet cleared — the re-review queue:
    one row per target with ``target_id``, ``links`` (count) and ``last_revoked``."""
    path = ledger_path()
    if path is None:
        return []
    conn = _connect(path)
    if conn is None:
        return []
    try:
        return [dict(r) for r in conn.execute(
            "SELECT target_id, COUNT(*) AS links, MAX(revoked_at) AS last_revoked "
            "FROM provenance WHERE stale = 1 GROUP BY target_id ORDER BY last_revoked")]
    except sqlite3.Error as e:
        logger.warning("Provenance stale_targets failed (%s): %s", path, e)
        return []
    finally:
        conn.close()


def clear_stale(target_id: str) -> int:
    """Clear the advisory flag for a reviewed target (its reviewer confirmed the target
    still holds). ``revoked_at`` is deliberately preserved — the audit record survives.
    Returns the number of rows cleared (0 when the ledger is off / unusable)."""
    path = ledger_path()
    if path is None:
        return 0
    conn = _connect(path)
    if conn is None:
        return 0
    try:
        cur = conn.execute(
            "UPDATE provenance SET stale = 0 WHERE target_id = ? AND stale = 1", (target_id,))
        conn.commit()
        return cur.rowcount
    except sqlite3.Error as e:
        logger.warning("Provenance clear_stale failed (%s): %s", path, e)
        return 0
    finally:
        conn.close()