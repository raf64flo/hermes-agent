"""Tests for Matrix non-approval reaction forwarding (PR #43745 inbound half).

Re-integrated from the 2026-08-19 stash draft and ported to the current
adapter API (``build_source`` / ``handle_message`` inherited from
``gateway.platforms.base``). Covers:

* ``_forward_reaction_to_agent`` — DM / group-allowlist gating, event
  construction, handoff to ``handle_message``, best-effort veille deposit,
  and failure containment (a forward error must never break the reaction
  pipeline).
* ``_on_reaction`` call site — non-approval reactions reach the forwarder,
  the flag can disable it, and approval-consuming reactions do not forward.
* ``_deposit_veille_reaction`` — writes a JSONL line via
  ``veille.reactions.capture_forwarded_reaction`` (imported from the repo
  named by ``VEILLE_REPO``) and swallows any error.
"""

import asyncio
import json
import sys
import types
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


# ---------------------------------------------------------------------------
# Stub mautrix so plugins.platforms.matrix.adapter can be imported without the SDK.
# (same trick as test_matrix_approval_reaction_fail_closed.py)
# ---------------------------------------------------------------------------

def _stub_mautrix():
    stub = types.ModuleType("mautrix")
    for sub in ("mautrix.types", "mautrix.client", "mautrix.client.api",
                "mautrix.errors", "mautrix.crypto", "mautrix.util",
                "mautrix.util.config"):
        sys.modules.setdefault(sub, types.ModuleType(sub))
    sys.modules.setdefault("mautrix", stub)
    m = sys.modules["mautrix.types"]

    class EventType:
        ROOM_MESSAGE = "m.room.message"
        REACTION = "m.reaction"
        ROOM_ENCRYPTED = "m.room.encrypted"
        ROOM_NAME = "m.room.name"

    class PaginationDirection:
        BACKWARD = "b"
        FORWARD = "f"

    class PresenceState:
        ONLINE = "online"
        OFFLINE = "offline"
        UNAVAILABLE = "unavailable"

    class RoomCreatePreset:
        PRIVATE = "private_chat"
        PUBLIC = "public_chat"
        TRUSTED_PRIVATE = "trusted_private_chat"

    class TrustState:
        UNVERIFIED = 0
        VERIFIED = 1

    for attr in ("ContentURI", "EventID", "RoomID", "SyncToken", "UserID"):
        setattr(m, attr, str)
    m.EventType = EventType
    m.PaginationDirection = PaginationDirection
    m.PresenceState = PresenceState
    m.RoomCreatePreset = RoomCreatePreset
    m.TrustState = TrustState


_stub_mautrix()

from plugins.platforms.matrix.adapter import MatrixAdapter  # noqa: E402


ROOM = "!testroom:matrix.org"
OTHER_ROOM = "!other:matrix.org"
SENDER = "@alice:matrix.org"
REACTION_EVENT_ID = "$reaction-event-1"
TARGET_EVENT = "$target-event-1"
KEY = "\U0001f44d"  # 👍
EXPECTED_BODY = f"[reaction] reacted with {KEY} to the message above."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_adapter(forward_reactions=True, is_dm=False, allowed_rooms=()):
    """Construct a MatrixAdapter with only the state needed by the forward
    path (``_forward_reaction_to_agent`` / ``_on_reaction``)."""
    adapter = object.__new__(MatrixAdapter)
    adapter._forward_reactions = forward_reactions
    adapter._allowed_rooms = set(allowed_rooms)
    adapter._is_dm_room = AsyncMock(return_value=is_dm)
    adapter._get_display_name = AsyncMock(return_value="Alice")
    adapter.build_source = Mock(return_value=SimpleNamespace(
        chat_id=ROOM, chat_type="dm" if is_dm else "group",
        user_id=SENDER, user_name="Alice"))
    adapter.handle_message = AsyncMock()
    adapter._deposit_veille_reaction = Mock()
    return adapter


def _make_full_adapter(forward_reactions=True, **kw):
    """Adapter good enough to drive the real ``_on_reaction`` end-to-end:
    real self-sender / dedupe gates, stubbed prompt handlers."""
    adapter = _make_adapter(forward_reactions=forward_reactions, **kw)
    adapter._user_id = "@bot:matrix.org"
    adapter._processed_events = deque(maxlen=512)
    adapter._processed_events_set = set()
    adapter._handle_approval_reaction = AsyncMock(return_value=False)
    adapter._handle_model_picker_reaction = AsyncMock(return_value=False)
    adapter._handle_choice_picker_reaction = AsyncMock(return_value=False)
    return adapter


def _make_reaction_event(key=KEY, reacts_to=TARGET_EVENT):
    """Minimal Matrix reaction event as ``_on_reaction`` receives it."""
    return SimpleNamespace(
        sender=SENDER,
        event_id=REACTION_EVENT_ID,
        room_id=ROOM,
        content={"m.relates_to": {"event_id": reacts_to, "key": key}},
    )


def _forward(adapter):
    return asyncio.run(
        adapter._forward_reaction_to_agent(
            ROOM, SENDER, REACTION_EVENT_ID, TARGET_EVENT, KEY))


# ---------------------------------------------------------------------------
# _forward_reaction_to_agent
# ---------------------------------------------------------------------------

class TestForwardReactionToAgent:
    """Gating, event construction and failure containment."""

    def test_forwarded_in_dm(self):
        adapter = _make_adapter(is_dm=True)
        _forward(adapter)

        assert adapter.handle_message.await_count == 1
        event = adapter.handle_message.await_args.args[0]
        assert event.text == EXPECTED_BODY
        assert event.source.chat_id == ROOM
        assert event.source.chat_type == "dm"
        assert event.source.user_id == SENDER
        assert event.message_id == REACTION_EVENT_ID
        assert event.reply_to_message_id == TARGET_EVENT
        adapter._deposit_veille_reaction.assert_called_once_with(
            ROOM, SENDER, KEY, TARGET_EVENT)

    def test_forwarded_in_allowlisted_group_room(self):
        adapter = _make_adapter(is_dm=False, allowed_rooms={ROOM})
        _forward(adapter)
        assert adapter.handle_message.await_count == 1
        assert adapter.handle_message.await_args.args[0].source.chat_type == "group"

    def test_dropped_in_non_allowlisted_group_room(self):
        adapter = _make_adapter(is_dm=False, allowed_rooms={OTHER_ROOM})
        _forward(adapter)
        adapter.handle_message.assert_not_awaited()
        adapter._deposit_veille_reaction.assert_not_called()

    def test_group_room_forwarded_when_allowlist_empty(self):
        # Empty allowlist = unrestricted (mirrors the room-gate semantics).
        adapter = _make_adapter(is_dm=False, allowed_rooms=set())
        _forward(adapter)
        assert adapter.handle_message.await_count == 1

    def test_handle_message_failure_swallowed(self):
        adapter = _make_adapter(is_dm=True)
        adapter.handle_message = AsyncMock(side_effect=RuntimeError("boom"))
        _forward(adapter)  # must not raise
        # Deposit happens before the handoff, so it is unaffected.
        adapter._deposit_veille_reaction.assert_called_once_with(
            ROOM, SENDER, KEY, TARGET_EVENT)


# ---------------------------------------------------------------------------
# _on_reaction call site
# ---------------------------------------------------------------------------

class TestOnReactionForwarding:
    """Non-approval reactions reach the forwarder from _on_reaction."""

    def test_non_approval_reaction_forwarded(self):
        adapter = _make_full_adapter(is_dm=True)
        asyncio.run(adapter._on_reaction(_make_reaction_event()))
        assert adapter.handle_message.await_count == 1

    def test_forward_flag_off_no_forward(self):
        adapter = _make_full_adapter(forward_reactions=False, is_dm=True)
        asyncio.run(adapter._on_reaction(_make_reaction_event()))
        adapter.handle_message.assert_not_awaited()
        adapter._deposit_veille_reaction.assert_not_called()

    def test_approval_consuming_reaction_not_forwarded(self):
        adapter = _make_full_adapter(is_dm=True)
        adapter._handle_approval_reaction = AsyncMock(return_value=True)
        asyncio.run(adapter._on_reaction(_make_reaction_event()))
        adapter.handle_message.assert_not_awaited()
        adapter._handle_model_picker_reaction.assert_not_awaited()
        adapter._handle_choice_picker_reaction.assert_not_awaited()

    def test_self_reaction_not_forwarded(self):
        adapter = _make_full_adapter(is_dm=True)
        asyncio.run(adapter._on_reaction(_make_reaction_event().__class__(
            sender=adapter._user_id,
            event_id=REACTION_EVENT_ID,
            room_id=ROOM,
            content={"m.relates_to": {"event_id": TARGET_EVENT, "key": KEY}},
        )))
        adapter.handle_message.assert_not_awaited()


# ---------------------------------------------------------------------------
# _deposit_veille_reaction
# ---------------------------------------------------------------------------

class TestDepositVeilleReaction:
    """Best-effort deposit into the veille repo (VEILLE_REPO)."""

    @pytest.fixture()
    def stub_repo(self, tmp_path, monkeypatch):
        """A fake veille repo whose capture_forwarded_reaction appends JSONL."""
        repo = tmp_path / "veille-repo"
        pkg = repo / "veille"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        out_file = tmp_path / "reactions.jsonl"
        (pkg / "reactions.py").write_text(
            "import json\n"
            "import os\n"
            "\n"
            "\n"
            "def capture_forwarded_reaction(sender=None, emoji=None,\n"
            "                               target_event_id=None, room_id=None,\n"
            "                               **kwargs):\n"
            "    path = os.environ['STUB_REACTIONS_PATH']\n"
            "    with open(path, 'a', encoding='utf-8') as fh:\n"
            "        fh.write(json.dumps({'sender': sender, 'emoji': emoji,\n"
            "                             'target_event_id': target_event_id,\n"
            "                             'room_id': room_id}) + '\\n')\n"
            "    return path\n"
        )
        monkeypatch.setenv("VEILLE_REPO", str(repo))
        monkeypatch.setenv("STUB_REACTIONS_PATH", str(out_file))
        for name in ("veille", "veille.reactions"):
            sys.modules.pop(name, None)
        yield repo, out_file
        for name in ("veille.reactions", "veille"):
            sys.modules.pop(name, None)
        repo_str = str(repo)
        while repo_str in sys.path:
            sys.path.remove(repo_str)

    def test_deposit_writes_jsonl_line(self, stub_repo):
        adapter = object.__new__(MatrixAdapter)
        _repo, out_file = stub_repo
        adapter._deposit_veille_reaction(ROOM, SENDER, KEY, TARGET_EVENT)

        lines = out_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0]) == {
            "sender": SENDER, "emoji": KEY,
            "target_event_id": TARGET_EVENT, "room_id": ROOM,
        }

    def test_deposit_missing_repo_never_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VEILLE_REPO", str(tmp_path / "does-not-exist"))
        for name in ("veille", "veille.reactions"):
            sys.modules.pop(name, None)
        try:
            adapter = object.__new__(MatrixAdapter)
            adapter._deposit_veille_reaction(ROOM, SENDER, KEY, TARGET_EVENT)
        finally:
            for name in ("veille.reactions", "veille"):
                sys.modules.pop(name, None)
            missing = str(tmp_path / "does-not-exist")
            while missing in sys.path:
                sys.path.remove(missing)
        # No exception = test passes; the error is logged as a warning.