# Spec — Compteurs `↑input ↓output` dans la barre de statut du classic CLI

Date : 2026-09-11
Statut : PRÊTE pour décomposition architecte (process SFEIR kanban)
Owner : architecte → developer → gate humain
Base : branche `feat/token-counters-statusbar` (commit `b47f438ae5` + refactor S1-S5 non commité dans le working tree)

## 1. Problème / contexte

La feature "compteurs de tokens ↑input ↓output + TPS streaming temps réel" a été implémentée
**TUI-only** (`ui-tui/`). Vérifié sur la branche `feat/token-counters-statusbar` :
- `hermes --tui` : la feature est visible après rebuild de `ui-tui/dist/entry.js`.
- `hermes` (classic CLI) : **NON visible** — `git status hermes_cli/` est vide, et
  `_status_bar_segments` (`hermes_cli/cli_status_bar_mixin.py:977-1046`) ne rend jamais
  `↑input ↓output`. Les données existent pourtant : `session_input_tokens` /
  `session_output_tokens` sont dans le snapshot (ligne 268-269).

L'utilisateur veut la feature visible en réel sur **classic** ET **TUI** (desktop suivi à part).

## 2. Objectif

1. Afficher `↑{in} ↓{out}` dans la barre de statut du classic CLI, aligné avec le TUI, en
   réutilisant le format numeric existant `format_token_count_compact` (cli.py:142).
2. Ne PAS toucher au rendu TUI (déjà fait) ni au backend.
3. Enregistrer que `stream_tps` LIVE (rolling 2s) **ne peut pas** être calculé côté classic
   (pas de `message.delta` individuels ; le classic voit les appels terminés). Le classic
   garde le segment `tps` = `avg_tps` existant. Périmètre : compteurs tokens statiques
   `↑in ↓out`, PAS de `stream_tps` live en classic.

## 3. Approche — à valider par l'architecte

Dans `hermes_cli/cli_status_bar_mixin.py` :

1. **Nouveau segment compteurs tokens** dans `_status_bar_segments()` : après le contexte
   (`context_pct`), render `↑{in} ↓{out}` quand le snapshot a des tokens distincts zéro.
   - Utiliser `format_token_count_compact(snapshot["session_input_tokens"])` /
     `format_token_count_compact(snapshot["session_output_tokens"])`.
   - Glyphes `↑` / `↓` cohérents avec le TUI (`↑` = input quittant le PC, `↓` = output
     du LLM vers le poste).
   - Style `_DIM` (comme les autres read-outs context/latency).
   - Auto-masquage : si `input == 0 AND output == 0`, pas de segment (éviter `↑0 ↓0`).
     Si l'un des deux est non nul, afficher les deux (ex. `↑1.2K ↓0` ok, ou ne montrer que
     le non-nul → TRANCHE : afficher les deux dès qu'au moins un est > 0, plus lisible).
2. **Nouveau champ `tokens`** dans la doc `_get_status_bar_field_set` (ligne 957-959) pour
   permettre de masquer via `display.status_bar.fields`. Gate par `_ok("tokens")`.
3. **Largeur** : n'afficher ce segment que dans le tier `wide` (≥76 cols), cohérent avec
   d'autres read-outs denses. Vérifier la contrainte de largeur existante
   (`_status_bar_segments` reçoit `width` ; tiers `narrow`/`wide`).

`avg_tps` (rolling appels terminés) **inchangé** — signal différent du compteur cumulé.

## 4. Invariants / à ne pas casser

- `format_token_count_compact` réutilisé tel quel (pas de nouvelle fonction de format).
- Les segments existants (model, context, cache_hit, latency, tps, compressions, bg_*)
  restent inchangés dans leur ordre/visibilité.
- `_get_status_bar` (PlainText + prompt_toolkit) dessine depuis la même `_status_bar_segments`
  → un seul endroit à modifier (DRY déjà en place, commentaire ligne 980).
- Test : la barre classic passe par `_build_status_bar_text` → ajouter un test unitaire
  Python sur `_status_bar_segments` (voir ci-dessous).
- Le `stream_tps` live n'est PAS ajouté au classic — documenter la limite dans la doc.

## 5. Décomposition attendue (sous-tâches TDD)

1. **C1 — Segment tokens dans `_status_bar_segments`** : render `↑{in} ↓{out}` gated
   `_ok("tokens")` + tier wide, avec auto-masquage. Test d'abord (RED) sur la sortie de
   `_status_bar_segments` avec un snapshot factice, puis implémentation (GREEN).
2. **C2 — Champ `tokens` documenté** dans la liste des fields + test que le field_set le
   contrôle bien.
3. **C3 — Vérif suites** : suite Python CLI (`scripts/run_tests.sh tests/hermes_cli/`)
   verte ; les tests existants de la barre ne régressent pas.

## 6. Vérification finale

```bash
# Suite Python classic (dans hermes-agent, venv activé)
scripts/run_tests.sh tests/hermes_cli/test_cli_status_bar* 2>&1   # verts
scripts/run_tests.sh tests/hermes_cli/ 2>&1                        # pas de régression
# Lancer la barre classic
hermes   # avec terminal ≥76 cols → segment `↑1.2K ↓5.7K` doit apparaître après 1re réponse
```

## 7. Gate humain avant PR

- Revue du diff (un seul fichier de prod : `hermes_cli/cli_status_bar_mixin.py` + tests).
- Carte parent d'intégration `t_18346c61` (verifier) reste en gate humain.
- Ne pas pousser/merger sans accord.

## 8. Limite documentée (importante)

`stream_tps` live (rolling 2s, deltas pendant le streaming) est **TUI-only** : seul le TUI
reçoit `message.delta` individuellement. Le classic CLI et le desktop reçoivent `session.usage`
par snapshots et ne peuvent pas calculer ce débit instantané. En classic, le débit affiché reste
`avg_tps` (le segment `tps` existant). Cette limite doit être explicite dans le corps de la carte
et la doc, pour éviter une fausse attente.