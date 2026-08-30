"""Tests unitaires du plugin kanban_model_quota."""

from __future__ import annotations

import sys
import types
from pathlib import Path

# Ajouter le plugin au path
PLUGIN_DIR = Path(__file__).parent
WORKTREE = Path(__file__).parent.parent
HERMES_AGENT = WORKTREE / "hermes-agent"
sys.path.insert(0, str(HERMES_AGENT))
sys.path.insert(0, str(WORKTREE / "plugins"))

# Créer un module mock pour hermes_cli.plugins
plugins_mock = types.ModuleType("hermes_cli.plugins")
plugins_mock.get_plugin_manager.return_value.has_hook.return_value = False
sys.modules["hermes_cli.plugins"] = plugins_mock

# Mock hermes_cli.profiles
profiles_mock = types.ModuleType("hermes_cli.profiles")
profiles_mock.load_yaml = lambda path, default=None: {} if default is not None else None
sys.modules["hermes_cli.profiles"] = profiles_mock

# Mock hermes_cli.plugin_context
pc = types.ModuleType("hermes_cli.plugin_context")
pc.PluginContext = types.SimpleNamespace(
    get_plugin_config=lambda: {"type": {"enabled": True, "per_model_concurrency": 2}},
    register_hook=lambda *a, **k: None,
    logger=lambda *a, **k: None,
)
sys.modules["hermes_cli.plugin_context"] = pc


# Maintenant on peut importer le plugin
from kanban_model_quota import (  # noqa: E402
    register,
    _is_enabled,
    _get_max_concurrency,
    _get_error_count,
    _check_circuit_breaker,
    _spawn_model_guard_callback,
    config_schema,
)


class FakeCtx:
    def get_plugin_config(self):
        return {"type": {"enabled": True, "per_model_concurrency": 2}}


class DisabledCtx:
    def get_plugin_config(self):
        return {"type": {"enabled": False, "per_model_concurrency": 2}}


class CustomCtx:
    def get_plugin_config(self):
        return {"type": {"enabled": True, "per_model_concurrency": 5}}


def test_default_config():
    ctx = FakeCtx()
    assert _is_enabled(ctx) is True
    assert _get_max_concurrency(ctx) == 2


def test_config_override():
    ctx = CustomCtx()
    assert _is_enabled(ctx) is True
    assert _get_max_concurrency(ctx) == 5


def test_disabled_config():
    ctx = DisabledCtx()
    assert _is_enabled(ctx) is False


def test_quota_check_under_limit():
    ctx = FakeCtx()  # per_model_concurrency = 2
    # 1 en vol sur 2 max -> autorisé
    result = _spawn_model_guard_callback(
        ctx,
        "t_test_1",
        "test-fibo",
        "hermes-optimizer",
        "gpt-4o",
        "architect",
        False,
        {"gpt-4o": 1},
    )
    assert result is None


def test_quota_check_at_limit():
    ctx = FakeCtx()  # per_model_concurrency = 2
    # 2 en vol sur 2 max -> bloqué
    result = _spawn_model_guard_callback(
        ctx,
        "t_test_1",
        "test-fibo",
        "hermes-optimizer",
        "gpt-4o",
        "architect",
        False,
        {"gpt-4o": 2},
    )
    assert result is not None
    assert "quota_exceeded" in result


def test_quota_check_over_limit():
    ctx = FakeCtx()  # per_model_concurrency = 2
    # 10 en vol sur 2 max -> bloqué
    result = _spawn_model_guard_callback(
        ctx,
        "t_test_1",
        "test-fibo",
        "hermes-optimizer",
        "gpt-4o",
        "architect",
        False,
        {"gpt-4o": 10},
    )
    assert result is not None
    assert "quota_exceeded" in result


def test_none_model_ignored():
    """Quand model_override est None, le plugin retourne None (autorisé)."""
    ctx = FakeCtx()
    result = _spawn_model_guard_callback(
        ctx,
        "t_test_1",
        "test-fibo",
        "unknown",
        None,  # model=None
        "default",
        False,
        {},
    )
    assert result is None


if __name__ == "__main__":
    tests = [
        test_default_config,
        test_config_override,
        test_disabled_config,
        test_quota_check_under_limit,
        test_quota_check_at_limit,
        test_quota_check_over_limit,
        test_none_model_ignored,
    ]
    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  ✓ {test.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ✗ {test.__name__}: {e}")
            failed += 1
    print(f"\n✅ {passed}/{passed + failed} tests passés")
    sys.exit(1 if failed else 0)