"""Tests d'intégration du model-quota guard."""

from __future__ import annotations

import sys
import os
import sqlite3

WORKTREE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, WORKTREE)
sys.path.insert(0, os.path.join(WORKTREE, "plugins"))
os.chdir(WORKTREE)


def _make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            assignee TEXT,
            status TEXT,
            model_override TEXT,
            worker_pid INTEGER
        )
    """)
    conn.commit()
    return conn


def test_helper_functions():
    from hermes_cli import kanban_db

    # _resolve_task_model: model_override prime sur profile
    model = kanban_db._resolve_task_model("architect", "custom-model")
    assert model == "custom-model", f"model_override doit primer: got {model}"

    model2 = kanban_db._resolve_task_model("hermes-optimizer", None)
    assert model2 is not None, "Doit retourner un modèle par défaut"

    # _count_in_flight_by_model: base vide
    conn = _make_conn()
    counts = kanban_db._count_in_flight_by_model(conn)
    assert counts == {}, f"Base vide → compte vides, got {counts}"

    # Tâche running avec worker_pid (comptée)
    conn.execute(
        "INSERT INTO tasks VALUES ('t1', 'architect', 'running', NULL, 12345)",
    )
    conn.commit()

    counts = kanban_db._count_in_flight_by_model(conn)
    assert len(counts) > 0, f"Doit compter la tâche running: {counts}"

    # Tâche running SANS worker_pid (non comptée)
    conn.execute(
        "INSERT INTO tasks VALUES ('t2', 'reviewer', 'running', NULL, NULL)",
    )
    conn.commit()

    counts_after = kanban_db._count_in_flight_by_model(conn)
    assert counts_after == counts, \
        "Sans worker_pid, le compte ne doit pas changer"

    print("✓ helper_functions")


def test_guard_short_circuits():
    """Aucun hook → court-circuit via _kanban_observer_consumed."""
    from hermes_cli import kanban_db

    conn = _make_conn()
    # Patch la référence dans le module
    orig = kanban_db._kanban_observer_consumed
    kanban_db._kanban_observer_consumed = lambda *a, **k: False
    try:
        reason = kanban_db._check_spawn_model_quota(
            conn, "t1", "architect", None,
            board="test-board", dry_run=False, _model_cache={},
        )
        assert reason is None, f"Court-circuit attendu: got {reason}"
    finally:
        kanban_db._kanban_observer_consumed = orig

    print("✓ guard_short_circuits")


def test_guard_swallows_hook_failure():
    """Une exception dans invoke_hook ne crash pas le guard."""
    from hermes_cli import kanban_db, lifecycle

    conn = _make_conn()
    saved = kanban_db._kanban_observer_consumed

    def mock_invoke(*a, **k):
        raise RuntimeError("boom!")

    orig_invoke = lifecycle.invoke_hook
    lifecycle.invoke_hook = mock_invoke
    kanban_db._kanban_observer_consumed = lambda *a, **k: True

    try:
        reason = kanban_db._check_spawn_model_quota(
            conn, "t1", "architect", None,
            board="test-board", dry_run=False, _model_cache={},
        )
        assert reason is None, f"Exception swallowed: got {reason}"
    finally:
        lifecycle.invoke_hook = orig_invoke
        kanban_db._kanban_observer_consumed = saved

    print("✓ guard_swallows_hook_failure")


def test_plugin_returns_quota_exceeded():
    """Le plugin retourne quota_exceeded quand le modèle est plein."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "kanban_model_quota",
        os.path.join(os.path.dirname(__file__), "__init__.py"),
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class FakeCtx:
        def __init__(self, cfg=None):
            self._cfg = cfg or {"enabled": True, "per_model_concurrency": 1}
        def register_hook(self, name, cb):
            self._cb = cb
        def get_plugin_config(self):
            return self._cfg

    ctx = FakeCtx()
    mod.register(ctx)
    cb = ctx._cb

    result = cb(
        ctx, "t1", "test-board", "architect", "qwen",
        "architect", False, {"qwen": 1},
    )
    assert "quota_exceeded" in result, f"Quota atteint → blocage: got {result}"

    result2 = cb(
        ctx, "t2", "test-board", "architect", "qwen",
        "architect", False, {"qwen": 0},
    )
    assert result2 is None, f"Quota libre → OK: got {result2}"

    print("✓ plugin_returns_quota_exceeded")


if __name__ == "__main__":
    test_helper_functions()
    test_guard_short_circuits()
    test_guard_swallows_hook_failure()
    test_plugin_returns_quota_exceeded()
    print("\n✅ 4/4 tests d'intégration passés")