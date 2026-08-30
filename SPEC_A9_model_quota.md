# SPEC — Contrôle par quota de modèles (A9)

## Résumé

Implémentation d'un garde-fou (guard) sur le dispatch kanban qui empêche le spawn
de tâches quand les quotas par modèle sont atteints. Ce mécanisme garantit qu'aucune
tâche ne passe en `running` sans PID valide quand le quota est saturé, et qu'aucune
tâche spawnée ne dépasse la concurrence maximale par modèle.

## Architecture

```
┌─────────────────────────────────────────────────┐
│  Dispatcher kanban_db._dispatch_once()           │
│                                                  │
│  ┌────────────────────────────────────────────┐  │
│  │ check_respawn_guard()                      │  │
│  │ (native — auth/token/lock)                 │  │
│  └────────────────────────────────────────────┘  │
│         ↓                                        │
│  ┌────────────────────────────────────────────┐  │
│  │ _check_spawn_model_quota()                 │  │
│  │ ├── _kanban_observer_consumed()            │  │
│  │ │   └── plugins.has_hook('kanban_…')       │  │
│  │ ├── _resolve_task_model()                  │  │
│  │ ├── _count_in_flight_by_model()            │  │
│  │ └── invoke_hook('kanban_spawn_model_guard')│  │
│  │        └── plugin kanban_model_quota       │  │
│  │             → _spawn_model_guard_callback() │  │
│  └────────────────────────────────────────────┘  │
│         ↓                                        │
│   Si return != None → tâche SKIP, restée ready   │
│   Si return == None → spawn normal               │
└─────────────────────────────────────────────────┘
```

### Composants

| Élément | Rôle |
|---|---|
| `kanban_spawn_model_guard` (hook) | Point d'extension dans `plugins.py` — tout plugin peut s'y enregistrer |
| `_check_spawn_model_quota()` | Fonction core dans `kanban_db.py` — short-circuite si aucun plugin ne souscrit |
| `_resolve_task_model()` | Résout le modèle effectif d'une tâche (model_override → assignee profile) |
| `_count_in_flight_by_model()` | Compte les tâches `running` avec un PID valide, par modèle |
| `_spawn_model_cache` | Cache par tick pour mutualiser les lectures profile |

### Flux

1. Pour chaque tâche candidate (ready → running ou review → running)
2. `check_respawn_guard()` vérifie les blocages natifs (auth, lock, erreurs)
3. `_check_spawn_model_quota()` est appelé si le guard natif a passé
   - Si aucun plugin ne souscrit au hook → court-circuite à `None` (autorisé)
   - Sinon, invoque `invoke_hook('kanban_spawn_model_guard', ...)`
   - Le plugin `kanban_model_quota` répond avec un motif de blocage si le quota est saturé
4. Si le guard renvoie un motif non-None → la tâche est skipée (reste en ready/review)
5. Sinon, la tâche passe en running (spawn normal)

## Fichiers modifiés

### Core (kanban_db.py + plugins.py)

| Fichier | Ligne(s) | Changement |
|---|---|---|
| `hermes_cli/plugins.py` | 354 | Ajout de `kanban_spawn_model_guard` à `VALID_HOOKS` |
| `hermes_cli/kanban_db.py` | ~9656 | Ajout de `_count_in_flight_by_model()` |
| `hermes_cli/kanban_db.py` | ~9684 | Ajout de `_resolve_task_model()` |
| `hermes_cli/kanban_db.py` | ~9684 | Ajout de `_check_spawn_model_quota()` |
| `hermes_cli/kanban_db.py` | ~10223 | Ajout de `_spawn_model_cache: dict = {}` (per-tick) |
| `hermes_cli/kanban_db.py` | ~10350 | Ajout du guard call après respawn guard (ready lane) |
| `hermes_cli/kanban_db.py` | ~10490 | Ajout du guard call après respawn guard (review lane) |
| `hermes_cli/kanban_db.py` | ~10159 | SELECT ready inclut `model_override` |
| `hermes_cli/kanban_db.py` | ~10167 | SELECT review inclut `model_override` |

### Plugin (kanban_model_quota)

| Fichier | Description |
|---|---|
| `plugins/kanban_model_quota/plugin.yaml` | Déclaration du plugin et de son hook |
| `plugins/kanban_model_quota/__init__.py` | Moteur quota + circuit-breaker |

### Tests

| Fichier | Description |
|---|---|
| `plugins/kanban_model_quota/test_plugin.py` | Tests unitaires du moteur |
| `plugins/kanban_model_quota/test_integration.py` | Tests d'intégration avec mock dispatch |

## Configuration

```yaml
plugins:
  entries:
    kanban_model_quota:
      enabled: true
      per_model_concurrency: 2    # concurrence max par modèle
      error_threshold: 3           # erreurs avant circuit-breaker
      recovery_time_seconds: 300   # durée du circuit-breaker
```

## Comportement

| Scénario | Résultat |
|---|---|
| Aucun plugin ne souscrit au hook | Court-circuite à `None` → autorisé (backward compatible) |
| `model_override` est `None` | Ignoré par le plugin → autorisé |
| Tâches en vol < per_model_concurrency | Autorisé (guard retourne None) |
| Tâches en vol >= per_model_concurrency | Bloqué → motif `quota_exceeded: modèle X à concurrence maximale (N/M)` |
| Circuit-breaker actif | Bloqué → motif `circuit_breaker: modèle X est en circuit-breaker (N erreurs récentes)` |

## Tests

### Unitaires (`test_plugin.py`) — 5/5 passing

- `test_default_config`: valeurs par défaut du plugin
- `test_config_override`: surcharge par config
- `test_quota_check_under_limit`: autorisé en dessous du quota
- `test_quota_check_at_limit`: bloqué au quota
- `test_quota_check_over_limit`: bloqué au-dessus du quota

### Intégration (`test_integration.py`) — 4/4 passing

- `test_helper_functions`: `_resolve_task_model`, `_count_in_flight_by_model`
- `test_guard_short_circuits`: court-circuite quand aucun hook
- `test_guard_swallows_hook_failure`: swallow des exceptions hook
- `test_plugin_returns_quota_exceeded`: le plugin retourne le motif de blocage

### Intégration board test-fibo — Passing

- Test A (0 en vol): ✅ autorisé
- Test B (1 en vol, quota=1): ✅ bloqué (1>=1)
- Test C (2 en vol, quota=2): ✅ bloqué (2>=2)
- Test D (10 en vol, quota=1): ✅ bloqué (10>=1)
- Test E (model=None): ✅ ignoré

## Gouvernance

- Implémentation uniquement sur branche de feature `wt/t_92708cec`
- `main` (production) n'est pas touchée
- Aucun push vers le remote
- Validation humaine requise avant merge

## Commits

| Commit | Message |
|---|---|
| `70b2aa127` | feat(A9): model-quota guard for kanban dispatch |
| `aa2628753` | feat(A9): plugin kanban_model_quota pour contrôle par quota de modèles |
| `3bc35bfaf` | test(A9): tests unitaires et d'intégration du plugin kanban_model_quota |