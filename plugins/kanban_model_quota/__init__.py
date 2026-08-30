"""Plugin de contrôle par quota de modèles pour le dispatch kanban.

Ce plugin s'enregistre au hook `kanban_spawn_model_guard` pour empêcher
le spawn de tâches quand les quotas par modèle sont atteints.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("hermes.plugins.kanban_model_quota")

# État global du circuit-breaker par modèle
_circuit_breaker: dict[str, dict[str, Any]] = {}
# Cache pour les métriques récentes
_metric_cache: dict[str, Any] = {}


def _load_config(ctx: Any) -> dict[str, Any]:
    """Charger la configuration du plugin depuis config.yaml."""
    try:
        return ctx.get_plugin_config() or {}
    except Exception as exc:
        logger.debug("Impossible de charger la configuration: %s", exc)
        return {}


def _is_enabled(ctx: Any) -> bool:
    """Vérifier si le plugin est activé."""
    config = _load_config(ctx)
    return config.get("enabled", True)


def _get_max_concurrency(ctx: Any) -> int:
    """Récupérer le quota de concurrence par modèle."""
    config = _load_config(ctx)
    return int(config.get("per_model_concurrency", 1))


def _check_circuit_breaker(
    ctx: Any, model: str, error_count: int
) -> Optional[str]:
    """Vérifier l'état du circuit-breaker pour un modèle donné.

    Retourne un motif de blocage si le circuit est ouvert, sinon None.
    """
    config = _load_config(ctx)
    cb_config = config.get("circuit_breaker", {})
    cb_enabled = cb_config.get("enabled", True)

    if not cb_enabled:
        return None

    error_threshold = int(cb_config.get("error_threshold", 5))
    recovery_time = int(cb_config.get("recovery_time", 300))

    # Vérifier ou initialiser le circuit-breaker pour ce modèle
    if model not in _circuit_breaker:
        _circuit_breaker[model] = {
            "error_count": 0,
            "last_error_time": 0,
            "state": "closed",  # closed, open, half-open
        }

    cb_state = _circuit_breaker[model]

    # Gérer le cycle de vie du circuit-breaker
    if cb_state["state"] == "open":
        elapsed = time.time() - cb_state["last_error_time"]
        if elapsed >= recovery_time:
            # Passage à half-open
            cb_state["state"] = "half-open"
            logger.info("Circuit breaker %s: half-open après %ds", model, elapsed)
        else:
            # Circuit ouvert, blocage
            remaining = int(recovery_time - elapsed)
            return f"circuit_breaker: modèle {model} en timeout ({remaining}s restantes)"

    # Compter les erreurs
    if error_count >= error_threshold:
        cb_state["error_count"] = error_count
        if cb_state["state"] == "closed":
            cb_state["state"] = "open"
            cb_state["last_error_time"] = time.time()
            logger.warning(
                "Circuit breaker %s: open (erreurs: %d/%d)",
                model, error_count, error_threshold,
            )
            return f"circuit_breaker: modèle {model} en erreur ({error_count} erreurs)"

    # Réinitialiser le compteur si le circuit est fermé
    if cb_state["state"] == "half-open":
        cb_state["state"] = "closed"
        cb_state["error_count"] = 0
        logger.info("Circuit breaker %s: closed après half-open", model)

    return None


def _get_error_count(ctx: Any, task_id: str) -> int:
    """Récupérer le nombre d'erreurs consécutives pour une tâche."""
    try:
        from hermes_cli.config import load_config_readonly
        config = load_config_readonly() or {}
        # Cette fonction est un placeholder - la logique réelle
        # sera implémentée en accédant à la base kanban
        return int(config.get(f"tasks.{task_id}.consecutive_failures", 0))
    except Exception:
        return 0


def _spawn_model_guard_callback(
    ctx: Any,
    task_id: str,
    board: Optional[str],
    assignee: str,
    model: Optional[str],
    profile_name: str,
    dry_run: bool,
    in_flight_by_model: dict[str, int],
) -> Optional[str]:
    """Callback pour le hook kanban_spawn_model_guard.

    Vérifie les quotas par modèle et le circuit-breaker.
    """
    if not _is_enabled(ctx):
        return None

    if not model:
        return None

    max_concurrency = _get_max_concurrency(ctx)
    current = in_flight_by_model.get(model, 0)

    logger.debug(
        "Guard model_quota: task=%s model=%s in_flight=%d max=%s",
        task_id, model, current, max_concurrency,
    )

    # Vérifier le quota
    if current >= max_concurrency:
        error_count = _get_error_count(ctx, task_id)
        block_reason = _check_circuit_breaker(ctx, model, error_count)
        if block_reason:
            return block_reason
        return f"quota_exceeded: modèle {model} à concurrence maximale ({current}/{max_concurrency})"

    # Vérifier le circuit-breaker même en dessous du quota
    error_count = _get_error_count(ctx, task_id)
    return _check_circuit_breaker(ctx, model, error_count)


def register(ctx: Any) -> None:
    """Enregistrer le plugin et ses hooks."""
    ctx.register_hook("kanban_spawn_model_guard", _spawn_model_guard_callback)
    logger.info("Plugin kanban_model_quota enregistré")