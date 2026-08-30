"""Tests unitaires du plugin kanban_model_quota."""

from __future__ import annotations

import sys
import types
from pathlib import Path

# Ajouter le plugin au path
PLUGIN_DIR = Path(__file__).parent
sys.path.insert(0, str(PLUGIN_DIR.parent.parent / "plugins"))


class FakeCtx:
    """Contexte factice pour les hooks Hermes."""

    def __init__(self, config: dict | None = None):
        self._config = config or {}
        self._hooks: dict[str, list] = {}

    def register_hook(self, name: str, callback):
        self._hooks.setdefault(name, []).append(callback)

    def get_plugin_config(self):
        return self._config


# Charger le plugin avec le contexte factice
import importlib.util
spec = importlib.util.spec_from_file_location(
    "kanban_model_quota",
    str(PLUGIN_DIR / "__init__.py"),
)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)

# Vérifier que le plugin s'enregistre correctement
ctx = FakeCtx(config={
    "enabled": True,
    "per_model_concurrency": 2,
    "circuit_breaker": {
        "enabled": True,
        "error_threshold": 5,
        "recovery_time_seconds": 300,
    },
})
plugin.register(ctx)

assert len(ctx._hooks.get("kanban_spawn_model_guard", [])) == 1, "Le plugin doit s'enregistrer au hook"
callback = ctx._hooks["kanban_spawn_model_guard"][0]


def test_plugin_enabled_allows_under_quota():
    """Quota non atteint → autorisation."""
    result = callback(
        ctx,
        task_id="t_test1",
        board="test-fibo",
        assignee="hermes-optimizer",
        model="qwen-3-6-35b-a3b",
        profile_name="hermes-optimizer",
        dry_run=True,
        in_flight_by_model={"qwen-3-6-35b-a3b": 1},  # max=2
    )
    assert result is None, f"Devrait autoriser, got: {result}"
    print("✓ test_plugin_enabled_allows_under_quota")


def test_plugin_enabled_blocks_over_quota():
    """Quota atteint → blocage."""
    result = callback(
        ctx,
        task_id="t_test2",
        board="test-fibo",
        assignee="hermes-optimizer",
        model="qwen-3-6-35b-a3b",
        profile_name="hermes-optimizer",
        dry_run=False,
        in_flight_by_model={"qwen-3-6-35b-a3b": 2},  # max=2
    )
    assert result is not None, "Devrait bloquer quand quota atteint"
    assert "quota_exceeded" in result, f"Motif attendu: quota_exceeded, got: {result}"
    print("✓ test_plugin_enabled_blocks_over_quota")


def test_plugin_disabled_allows_everything():
    """Plugin désactivé → toujours autoriser."""
    ctx_disabled = FakeCtx(config={"enabled": False})
    plugin.register(ctx_disabled)
    cb = ctx_disabled._hooks["kanban_spawn_model_guard"][0]
    result = cb(
        ctx_disabled,
        task_id="t_test3",
        board="test-fibo",
        assignee="hermes-optimizer",
        model="qwen-3-6-35b-a3b",
        profile_name="hermes-optimizer",
        dry_run=False,
        in_flight_by_model={"qwen-3-6-35b-a3b": 999},
    )
    assert result is None, f"Devrait autoriser plugin désactivé, got: {result}"
    print("✓ test_plugin_disabled_allows_everything")


def test_plugin_no_model_allows():
    """Pas de modèle → autorisation."""
    result = callback(
        ctx,
        task_id="t_test4",
        board="test-fibo",
        assignee="hermes-optimizer",
        model=None,
        profile_name="hermes-optimizer",
        dry_run=False,
        in_flight_by_model={},
    )
    assert result is None, f"Devrait autoriser sans modèle, got: {result}"
    print("✓ test_plugin_no_model_allows")


def test_plugin_different_models_independent():
    """Modèles différents ont des quotas indépendants."""
    result = callback(
        ctx,
        task_id="t_test5",
        board="test-fibo",
        assignee="architect",
        model="anthropic-albert/deepseek-v4-flash",
        profile_name="architect",
        dry_run=False,
        in_flight_by_model={
            "qwen-3-6-35b-a3b": 2,
            "anthropic-albert/deepseek-v4-flash": 1,  # max=2, OK
        },
    )
    assert result is None, f"Devrait autoriser modèle indépendant, got: {result}"
    print("✓ test_plugin_different_models_independent")


# Exécuter les tests
if __name__ == "__main__":
    test_plugin_enabled_allows_under_quota()
    test_plugin_enabled_blocks_over_quota()
    test_plugin_disabled_allows_everything()
    test_plugin_no_model_allows()
    test_plugin_different_models_independent()
    print("\n✅ 5/5 tests unitaires passés")