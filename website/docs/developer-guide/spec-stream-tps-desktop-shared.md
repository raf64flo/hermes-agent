# Spec — Extension du `stream_tps` + compteurs ↑↓ au Desktop + refactor Helper partagé

Date : 2026-09-10
Statut : PRÊTE pour décomposition architecte (process SFEIR kanban)
Owner : architecte → developer → gate humain

## 1. Problème / contexte

La feature "compteurs de tokens ↑input ↓output + TPS streaming en temps réel" a été
implémentée **TUI-only** (branche `feat/token-counters-statusbar`, commit `b47f438ae5`)
et mergée comme base. Elle n'existe que dans l'UI Ink (`ui-tui/src/components/appChrome.tsx`
rend les ↑↓ et le `stream_tps`) et le calcul vit uniquement dans `ui-tui/src/app/turnController.ts`.

Pendant que la branche TUI est en relecture (gate humain), il faut **étendre la feature
au Desktop** (`apps/desktop`), qui est une surface séparée sur le même backend
(`tui_gateway`). Le desktop n'affiche que l'ancien `avg_tps` ; pas de compteurs ↑↓ ni de
`stream_tps` live.

## 2. Objectif

1. Affichage desktop : compteurs `↑input ↓output` + `↓ N t/s` (stream_tps live) dans la
   barre de statut du desktop, alignés sur le rendu TUI.
2. **DRY** : extraire l'algorithme de calcul `stream_tps` (rolling 2s window + throttle 1s
   + min 3 samples) dans un **helper partagé** `@hermes/shared`, importé par le TUI ET le
   desktop — une seule implémentation, plus de duplication.

## 3. APPROCHE — à valider par l'architecte

- **Helper partagé** : nouveau fichier `apps/shared/src/stream-tps.ts` exposant
  `createStreamTpsCounter()` (fermé) et/ou une fonction pure `accumulateStreamTps(counter, now)`.
  Le TUI et le desktop utilisent la MÊME instance de counter par "stream" (par session).
- **TUI (refactor)** : `ui-tui/src/app/turnController.ts` remplace sa logique inline
  (`_deltaTimestamps`, `_lastTpsEmit`, lignes 699-721) par le helper partagé. Le champ privé
  et le reset en début de turn restent. Les 12 tests TUI existants doivent rester verts.
- **Desktop — types** : ajouter `stream_tps?: number` à
  `apps/desktop/src/types/hermes.ts` (`UsageStats`).
- **Desktop — calcul** : dans le reducer
  `apps/desktop/src/app/session/hooks/use-message-stream/gateway-event/message-stream.ts`
  (branche `message.delta`, ligne ~154-160), instancier un counter par session et appeler le
  helper à chaque delta ; pousser `stream_tps` dans le store `$currentUsage`
  (via `setCurrentUsage`).
- **Desktop — rendu** : dans `apps/desktop/src/lib/statusbar.tsx`, ajouter
  `streamTpsLabel()` (lit `usage.stream_tps`, format `↓ N t/s`) et `tokenCountersLabel()`
  (lit `usage.input`/`usage.output`, format `↑ X ↓ Y`). Les brancher dans
  `apps/desktop/src/app/shell/hooks/use-statusbar-items.tsx` à côté du `tokensPerSecondLabel`
  existant (ligne ~287).

## 4. Contraites / invariants (ne pas casser)

- Le commentaire `appChrome.tsx:628` décrit le desktop comme le "miroir du classic CLI bar".
  Respecter le pattern existant (les read-outs s'auto-masquent si absent/0).
- `avg_tps` (rolling sur appels terminés) reste inchangé — c'est un signal différent du
  `stream_tps` (live pendant streaming).
- Le helper partagé doit être pur/testable (pas d'import React/Ink).
- Les tests : la suite TUI (1786) + la suite desktop doivent rester vertes.
- Ne pas toucher au comportement du backend (`tui_gateway`) : le `stream_tps` reste calculé
  côté client (TUI + desktop), pas côté serveur. Le serveur n'envoie pas les deltas
  individuels, donc un calcul serveur serait faux (ligne `_start_usage_ticker` server.py ~3007
  saute les ticks quand "counters frozen").

## 5. Décomposition attendue de l'architecte (en sous-tâches TDD)

L'architecte découpe en sous-tâches implémentables, chacune avec un test d'abord (RED→GREEN) :

1. **S1 — Helper partagé** `apps/shared/src/stream-tps.ts` + test `stream-tps.test.ts`
   (vitest). Contrat : `accumulateStreamTps` retourne le taux (rolling 2s, throttle ~1s,
   min 3 samples) ou `undefined`.
2. **S2 — Refactor TUI** : `turnController.ts` utilise le helper ; les 12 tests
   `turnControllerStreamTps.test.ts` restent verts SANS changement d'assertion.
3. **S3 — Desktop types** : `stream_tps` dans `UsageStats` (types/hermes.ts).
4. **S4 — Desktop calcul** : reducer `message-stream.ts` alimente le counter + pousse
   `stream_tps` via `setCurrentUsage` ; test du reducer couvrant le flux delta.
5. **S5 — Desktop rendu** : `streamTpsLabel()` + `tokenCountersLabel()` dans statusbar.tsx +
   branchement dans use-statusbar-items.tsx + tests statusbar.

Sous-tâches déclarant leurs fichiers touchés (principe de disjonction — pas de collision).

## 6. Vérification finale

```bash
# TUI (suite complète doit rester verte)
cd ui-tui && npx vitest run          # 170 files / 1786 tests green
cd ui-tui && npx tsc --noEmit -p tsconfig.json   # exit 0
# Desktop
cd apps/desktop && npx vitest run     # suite desktop green
cd apps/desktop && npx tsc --noEmit -p tsconfig.json  # exit 0 (via workspace root?)
# Shared
cd apps/shared && npx vitest run src/stream-tps.test.ts  # green
```

## 7. Gate humain avant PR

- Revue du diff consolidé (helper + TUI refactor + desktop) par un humain.
- La branche de feature `feat/token-counters-statusbar` sert de base.
- Ne pas pousser/merger sans accord explicite (+ PR upstream sur forge.inrae.fr ou autre remote).