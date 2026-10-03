"""Profile consolidation — bounded one-pass editing of USER.md with backlog,
cooldown, versioning, and stale-on-retract (E4).

Design (arbitrated by the orchestrator, see task t_e17cfb4d):
- Config: ``memory.profile_consolidation`` (OFF by default, opt-in).
- Core: ``ProfileConsolidator`` (pure, testable without a live agent).
- The model EDITS (rewrites USER.md as a whole), it does not append.
- Backlog: ``memories/.profile/backlog.json`` (dedup by ``entry_source_id``).
- Archives: ``memories/.profile/archives/USER.md.<tsZ>.md`` (via ``atomic_write_text``).
- Stale-on-retract: ``mark_stale`` bypasses cooldown/threshold, appends
  ``[STALE]`` to the matching bullet — never deletes on the fly.
- Rollback: archive the current state, then restore the latest archive.
- Provenance: after each consolidation, record source→target rows via
  ``tools.provenance_ledger.record``; on retraction, E1 marks rows revoked
  and E4's ``mark_stale`` surfaces the change in USER.md.
"""

import json
import logging
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hermes_constants import get_hermes_home
from utils import atomic_write_text
from tools.registry import no_cache_check_fn

logger = logging.getLogger(__name__)

# ── defaults ────────────────────────────────────────────────────────────────
DEFAULTS = {
    "enabled": False,
    "min_candidates": 5,
    "cooldown_hours": 24,
    "max_bullets": 12,
    "char_limit": 1500,
}

# Canonical target_id for the USER.md profile in the provenance ledger.
PROFILE_TARGET_ID = "user_profile"

STALE_MARKER = "[STALE]"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts_filename(ts: float) -> str:
    """Timestamp string for archive filenames: 20261003T120000Z123456 (microseconds)."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%dT%H%M%S%f")[:-3] + "Z"


def _parse_iso(s: str) -> float:
    """Parse a YYYY-MM-DDTHH:MM:SSZ string to epoch seconds (0 on failure)."""
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        return 0.0


def load_profile_consolidation_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Normalized ``memory.profile_consolidation`` section merged with DEFAULTS.
    Returns an empty ``enabled=False`` dict when the section is absent or malformed."""
    if config is None:
        try:
            from hermes_cli.config import load_config_readonly
            config = load_config_readonly()
        except Exception:
            logger.debug("Could not read profile_consolidation config", exc_info=True)
            return dict(DEFAULTS)
    section = config.get("memory") if isinstance(config, dict) else None
    if not isinstance(section, dict):
        return dict(DEFAULTS)
    pc = section.get("profile_consolidation")
    if not isinstance(pc, dict):
        return dict(DEFAULTS)
    merged = dict(DEFAULTS)
    for k in DEFAULTS:
        v = pc.get(k)
        if v is not None:
            merged[k] = v
    return merged


def get_user_profile_path() -> Path:
    """Resolve the USER.md path (same as MemoryStore's user target)."""
    from tools import memory_tool
    return memory_tool.get_memory_dir() / "USER.md"


def get_state_dir() -> Path:
    """Resolve the state dir: ``memories/.profile/``."""
    from tools import memory_tool
    return memory_tool.get_memory_dir() / ".profile"


# ── file-lock (shared with MemoryStore) ─────────────────────────────────────
@contextmanager
def _profile_file_lock(path: Path):
    """Exclusive lock on the USER.md .lock file — same pattern as MemoryStore._file_lock."""
    import os
    from tools import memory_tool as _mt
    fcntl, msvcrt = _mt.fcntl, _mt.msvcrt
    lock_path = path.with_suffix(path.suffix + ".lock")
    from hermes_constants import mkdir_under_hermes_home
    mkdir_under_hermes_home(lock_path.parent)
    if fcntl is None and msvcrt is None:
        yield
        return
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(lock_path), flags)
    try:
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_EX)
        elif msvcrt:
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        yield
    finally:
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_UN)
        elif msvcrt:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        os.close(fd)


# ── core class ──────────────────────────────────────────────────────────────
class ProfileConsolidator:
    """Bounded one-pass editor for USER.md. All mutating ops hold the file lock.

    Parameters
    ----------
    user_md_path : Path
        Absolute path to USER.md.
    state_dir : Path
        Directory for backlog.json and archives/ (typically ``memories/.profile``).
    config : dict
        Normalized profile_consolidation config (see ``load_profile_consolidation_config``).
    """

    def __init__(self, user_md_path: Path, state_dir: Path, config: Dict[str, Any]):
        self.user_md_path = Path(user_md_path)
        self.state_dir = Path(state_dir)
        self.archives_dir = self.state_dir / "archives"
        self.backlog_path = self.state_dir / "backlog.json"
        self.cfg = config

    # ── helpers ─────────────────────────────────────────────────────────────

    def _backlog(self) -> Dict[str, Any]:
        """Load backlog JSON (empty dict on missing/corrupt)."""
        if not self.backlog_path.exists():
            return {"candidates": [], "last_edit_at": None}
        try:
            data = json.loads(self.backlog_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {"candidates": [], "last_edit_at": None}
            data.setdefault("candidates", [])
            data.setdefault("last_edit_at", None)
            return data
        except (json.JSONDecodeError, OSError):
            return {"candidates": [], "last_edit_at": None}

    def _save_backlog(self, data: Dict[str, Any]) -> None:
        """Atomic write of the backlog JSON."""
        from hermes_constants import mkdir_under_hermes_home
        mkdir_under_hermes_home(self.backlog_path.parent)
        atomic_write_text(self.backlog_path, json.dumps(data, indent=2, ensure_ascii=False),
                          tmp_prefix=".profile_")

    def _read_user_md(self) -> str:
        """Current USER.md content ('' when missing)."""
        if not self.user_md_path.exists():
            return ""
        try:
            return self.user_md_path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def _write_user_md(self, content: str) -> None:
        """Atomic write of USER.md (0o600, same as the memory store)."""
        from hermes_constants import mkdir_under_hermes_home
        mkdir_under_hermes_home(self.user_md_path.parent)
        atomic_write_text(self.user_md_path, content, mode=0o600, tmp_prefix=".mem_")

    def _archive_current(self, ts: Optional[float] = None) -> str:
        """Snapshot the current USER.md to archives/. Returns the archive filename.
        No-op (returns empty string) when USER.md is missing or empty."""
        content = self._read_user_md()
        if not content.strip():
            return ""
        ts = ts if ts is not None else time.time()
        from hermes_constants import mkdir_under_hermes_home
        mkdir_under_hermes_home(self.archives_dir)
        fname = f"USER.md.{_ts_filename(ts)}.md"
        atomic_write_text(self.archives_dir / fname, content, mode=0o600, tmp_prefix=".archive_")
        return fname

    @staticmethod
    def _count_bullets(content: str) -> int:
        """Count §-delimited entries (same format as MemoryStore)."""
        if not content.strip():
            return 0
        return len([e for e in content.split("\n§\n") if e.strip()])

    def _check_cooldown(self, now: Optional[float] = None) -> Tuple[bool, str]:
        """Return (ready, reason). ready=True when cooldown has elapsed since last edit."""
        now = now if now is not None else time.time()
        backlog = self._backlog()
        last_edit = backlog.get("last_edit_at")
        if not last_edit:
            return True, "no prior edit"
        cooldown_secs = float(self.cfg.get("cooldown_hours", 24)) * 3600
        elapsed = now - _parse_iso(last_edit)
        if elapsed >= cooldown_secs:
            return True, f"cooldown elapsed ({elapsed:.0f}s ≥ {cooldown_secs:.0f}s)"
        remaining = cooldown_secs - elapsed
        return False, f"cooldown active: {remaining:.0f}s remaining"

    def _gate_errors(self) -> Optional[Dict[str, Any]]:
        """Return an error dict if the hard gates block an edit, else None."""
        if not self.cfg.get("enabled", False):
            return {"success": False, "error": (
                "Profile consolidation is disabled (memory.profile_consolidation.enabled=false). "
                "Enable it in config.yaml to use this tool.")}
        # Hard gate: user_profile must be enabled in the memory store
        try:
            from tools.memory_tool import get_builtin_memory_store_flags
            _, user_on = get_builtin_memory_store_flags()
            if not user_on:
                return {"success": False, "error": (
                    "User profile writes are disabled (memory.user_profile_enabled=false). "
                    "Re-enable it in config.yaml first.")}
        except Exception:
            pass  # config load failed — don't block on a soft error
        return None

    # ── actions ─────────────────────────────────────────────────────────────

    def status(self, now: Optional[float] = None) -> Dict[str, Any]:
        """Current state: enabled, pending count, cooldown, usage, due flag."""
        now = now if now is not None else time.time()
        content = self._read_user_md()
        char_count = len(content)
        bullet_count = self._count_bullets(content)
        backlog = self._backlog()
        pending = len(backlog.get("candidates", []))
        cooldown_ready, cooldown_reason = self._check_cooldown(now)
        min_cands = int(self.cfg.get("min_candidates", 5))
        due = self.cfg.get("enabled", False) and cooldown_ready and pending >= min_cands
        result: Dict[str, Any] = {
            "success": True,
            "enabled": bool(self.cfg.get("enabled", False)),
            "pending_count": pending,
            "min_candidates": min_cands,
            "cooldown_ready": cooldown_ready,
            "cooldown_reason": cooldown_reason,
            "due": due,
            "char_count": char_count,
            "char_limit": int(self.cfg.get("char_limit", 1500)),
            "bullet_count": bullet_count,
            "max_bullets": int(self.cfg.get("max_bullets", 12)),
            "last_edit_at": backlog.get("last_edit_at"),
            "user_md_path": str(self.user_md_path),
        }
        return result

    def add_candidate(self, entry_text: str, source: str = "memory") -> Dict[str, Any]:
        """Add a fact to the backlog (dedup by entry_source_id). No cooldown gate."""
        gate = self._gate_errors()
        if gate:
            return gate
        from tools.provenance_ledger import entry_source_id
        eid = entry_source_id(entry_text)
        backlog = self._backlog()
        existing_ids = {c.get("id") for c in backlog.get("candidates", [])}
        if eid in existing_ids:
            return {"success": True, "added": False, "reason": "already in backlog",
                    "pending_count": len(backlog.get("candidates", []))}
        backlog["candidates"].append({
            "id": eid,
            "text": entry_text.strip(),
            "source": source,
            "added_at": _now_iso(),
        })
        self._save_backlog(backlog)
        return {"success": True, "added": True, "id": eid,
                "pending_count": len(backlog["candidates"])}

    def consolidate(self, new_content: str, consumed_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        """Bounded one-pass edit of USER.md.

        Gates: enabled, user_profile_enabled, cooldown elapsed, ≥ min_candidates.
        Caps: char_limit, max_bullets.
        Before writing: archive the current state.
        After writing: record provenance rows for consumed (or all) backlog entries.
        """
        gate = self._gate_errors()
        if gate:
            return gate

        now = time.time()
        cooldown_ready, cooldown_reason = self._check_cooldown(now)
        if not cooldown_ready:
            return {"success": False, "error": f"Cooldown not elapsed: {cooldown_reason}"}

        backlog = self._backlog()
        candidates = backlog.get("candidates", [])
        min_cands = int(self.cfg.get("min_candidates", 5))
        if len(candidates) < min_cands:
            return {"success": False, "error": (
                f"Not enough pending candidates: {len(candidates)}/{min_cands}. "
                f"Use add_candidate to queue more facts.")}

        # Validate the new content
        char_count = len(new_content)
        char_limit = int(self.cfg.get("char_limit", 1500))
        if char_count > char_limit:
            return {"success": False, "error": (
                f"New content exceeds char limit: {char_count}/{char_limit}. "
                f"Trim the content and try again.")}
        bullet_count = self._count_bullets(new_content)
        max_bullets = int(self.cfg.get("max_bullets", 12))
        if bullet_count > max_bullets:
            return {"success": False, "error": (
                f"New content exceeds bullet limit: {bullet_count}/{max_bullets}. "
                f"Merge or remove bullets and try again.")}

        # Determine which entries were consumed
        if consumed_ids is None:
            consumed = list(candidates)
        else:
            consumed = [c for c in candidates if c.get("id") in set(consumed_ids)]
            if not consumed:
                return {"success": False, "error": "None of the consumed_ids matched the backlog."}

        # Hold the lock, archive, write, update backlog
        with _profile_file_lock(self.user_md_path):
            archive_fname = self._archive_current(now)
            self._write_user_md(new_content)
            backlog["candidates"] = [c for c in candidates if c.get("id") not in set(c["id"] for c in consumed)]
            backlog["last_edit_at"] = _now_iso()
            self._save_backlog(backlog)

        # Record provenance (fail-open: a ledger error never undoes the write)
        provenance_records = 0
        try:
            from tools.provenance_ledger import record, ledger_path
            if ledger_path() is not None:
                for c in consumed:
                    row = record(c["id"], c.get("source", "memory"), PROFILE_TARGET_ID,
                                 "INFORMED_BY", source_text=c.get("text", ""))
                    if row is not None:
                        provenance_records += 1
        except Exception:
            logger.warning("Provenance recording skipped (consolidation unaffected)", exc_info=True)

        return {
            "success": True,
            "archived": archive_fname or None,
            "char_count": char_count,
            "bullet_count": bullet_count,
            "entries_consolidated": len(consumed),
            "provenance_records": provenance_records,
            "remaining_candidates": len(backlog["candidates"]),
        }

    def mark_stale(self, target_id: str = PROFILE_TARGET_ID) -> Dict[str, Any]:
        """Mark stale bullets in USER.md for revoked provenance rows.

        Bypasses cooldown and min_candidates threshold (fast path).
        Appends [STALE] to the matching bullet — never deletes.
        One pass, idempotent.
        """
        # No gate: mark_stale is a fast path that works even when consolidation is disabled.
        # (The provenance ledger must be on for this to do anything useful.)
        from tools.provenance_ledger import rows_for_target, ledger_path
        if ledger_path() is None:
            return {"success": True, "marked": 0, "reason": "provenance ledger not configured"}

        rows = rows_for_target(target_id)
        revoked = [r for r in rows if r.get("revoked_at") is not None]
        if not revoked:
            return {"success": True, "marked": 0, "reason": "no revoked sources for target"}

        content = self._read_user_md()
        if not content.strip():
            return {"success": True, "marked": 0, "reason": "USER.md is empty"}

        bullets = content.split("\n§\n") if "\n§\n" in content else [content]
        marked = 0
        for row in revoked:
            src_text = row.get("source_text", "").strip()
            if not src_text:
                continue
            # Find the bullet that contains the source text (fuzzy: first 40 chars)
            needle = src_text[:40]
            for i, bullet in enumerate(bullets):
                if bullet.strip() and needle in bullet and not bullet.strip().startswith(STALE_MARKER):
                    bullets[i] = f"{STALE_MARKER} {bullet.strip()}"
                    marked += 1
                    break

        if marked:
            new_content = "\n§\n".join(bullets)
            with _profile_file_lock(self.user_md_path):
                self._archive_current()
                self._write_user_md(new_content)

        return {"success": True, "marked": marked, "revoked_sources": len(revoked)}

    def rollback(self) -> Dict[str, Any]:
        """Restore the most recent archive to USER.md.

        Before restoring: archive the current state (so a failed rollback can be undone).
        Returns the archive filename that was restored.
        """
        if not self.archives_dir.exists():
            return {"success": False, "error": "No archives directory found."}
        archives = sorted(self.archives_dir.glob("USER.md.*.md"))
        if not archives:
            return {"success": False, "error": "No archives available to rollback to."}
        latest = archives[-1]
        try:
            content = latest.read_text(encoding="utf-8")
        except OSError as e:
            return {"success": False, "error": f"Cannot read archive {latest.name}: {e}"}

        with _profile_file_lock(self.user_md_path):
            # Archive current state before overwriting
            self._archive_current()
            self._write_user_md(content)

        return {"success": True, "restored_from": latest.name,
                "char_count": len(content), "bullet_count": self._count_bullets(content)}

    def list_archives(self) -> Dict[str, Any]:
        """List available archives (newest first)."""
        if not self.archives_dir.exists():
            return {"success": True, "archives": []}
        archives = sorted(self.archives_dir.glob("USER.md.*.md"), reverse=True)
        result = []
        for a in archives:
            try:
                content = a.read_text(encoding="utf-8")
                result.append({
                    "filename": a.name,
                    "char_count": len(content),
                    "bullet_count": self._count_bullets(content),
                })
            except OSError:
                result.append({"filename": a.name, "error": "unreadable"})
        return {"success": True, "archives": result}


# ── tool handler ────────────────────────────────────────────────────────────

def _get_consolidator() -> ProfileConsolidator:
    """Build a ProfileConsolidator from the current config."""
    config = load_profile_consolidation_config()
    return ProfileConsolidator(
        user_md_path=get_user_profile_path(),
        state_dir=get_state_dir(),
        config=config,
    )


def profile_consolidation_tool(
    action: str = "status",
    entry_text: Optional[str] = None,
    source: Optional[str] = None,
    new_content: Optional[str] = None,
    consumed_ids: Optional[List[str]] = None,
    target_id: Optional[str] = None,
    **_: Any,
) -> str:
    """Handle a profile_consolidation tool call."""
    tc = _get_consolidator()
    try:
        if action == "status":
            result = tc.status()
        elif action == "add_candidate":
            if not entry_text:
                return json.dumps({"success": False, "error": "entry_text is required for add_candidate"})
            result = tc.add_candidate(entry_text, source or "memory")
        elif action == "consolidate":
            if not new_content:
                return json.dumps({"success": False, "error": "new_content is required for consolidate"})
            result = tc.consolidate(new_content, consumed_ids)
        elif action == "mark_stale":
            result = tc.mark_stale(target_id or PROFILE_TARGET_ID)
        elif action == "rollback":
            result = tc.rollback()
        elif action == "list_archives":
            result = tc.list_archives()
        else:
            return json.dumps({"success": False, "error": f"Unknown action '{action}'"})
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        logger.exception("profile_consolidation action %s failed", action)
        return json.dumps({"success": False, "error": str(e)})


PROFILE_CONSOLIDATION_SCHEMA: Dict[str, Any] = {
    "name": "profile_consolidation",
    "description": (
        "Bounded one-pass editing of the user profile (USER.md). "
        "The model EDITS the profile — it rewrites it as a whole, not append. "
        "Gates: opt-in (memory.profile_consolidation.enabled), ≥ min_candidates pending, "
        "cooldown elapsed. Caps: char_limit, max_bullets. "
        "Before each edit the current state is archived (rollback possible). "
        "mark_stale is a fast path that bypasses cooldown/threshold: when a memory "
        "entry is retracted (E1 provenance), the matching profile bullet gets [STALE] "
        "without waiting. Never deletes on the fly — only marks."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["status", "add_candidate", "consolidate", "mark_stale", "rollback", "list_archives"],
                "description": (
                    "status: current state (pending count, cooldown, usage, due flag). "
                    "add_candidate: queue a fact for the next consolidation. "
                    "consolidate: bounded one-pass edit (the model writes the full new content). "
                    "mark_stale: mark [STALE] on bullets whose source memory was retracted (fast path, no cooldown). "
                    "rollback: restore the most recent archive. "
                    "list_archives: list available archives (newest first)."
                ),
            },
            "entry_text": {
                "type": "string",
                "description": "For add_candidate: the fact to queue in the backlog.",
            },
            "source": {
                "type": "string",
                "enum": ["memory", "user", "session"],
                "description": "For add_candidate: source type of the fact (default: memory).",
            },
            "new_content": {
                "type": "string",
                "description": (
                    "For consolidate: the full new USER.md content (the model's edit). "
                    "§-delimited bullets, must be ≤ char_limit and ≤ max_bullets."
                ),
            },
            "consumed_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "For consolidate: backlog entry IDs (sha256) that informed this edit. "
                    "Omit to consume all pending candidates."
                ),
            },
            "target_id": {
                "type": "string",
                "description": "For mark_stale: the provenance target_id (default: user_profile).",
            },
        },
        "required": ["action"],
    },
}


@no_cache_check_fn
def check_profile_consolidation_requirements() -> bool:
    """Available when profile consolidation is enabled in config."""
    config = load_profile_consolidation_config()
    return bool(config.get("enabled", False))


from tools.registry import registry  # noqa: E402  (registration at import time)

registry.register(
    name="profile_consolidation",
    toolset="memory",
    schema=PROFILE_CONSOLIDATION_SCHEMA,
    handler=profile_consolidation_tool,
    check_fn=check_profile_consolidation_requirements,
    emoji="📋",
)