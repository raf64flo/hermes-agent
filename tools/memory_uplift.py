"""E2 — memory → skill uplift (proposer mode, jev-curator gate).

Pli d'entrées de mémoire éligibles (motif E3) dans un skill du *live tree*, puis
démotion déterministe des fragments sources hors du budget borné (2200/1375).

Rôle : « stop injecting the fragments alongside the procedure ».

Division du travail (voir le design, kanban t_70ab8f5a) :

* **Promotion** — faite par la fork aux-model existante du curator (``consolidate``).
  Ce module ne crée AUCUN skill : il prépare la section « uplift » du prompt
  (candidats + entrées verbatim + slug attendu) et la consultation read-only de
  ``jev-curator`` (C3). La fork propose/crée le skill via ``skill_manage`` dans le
  live tree (~/.hermes/skills) — JAMAIS ``~/local/git/ia/skills`` (C1).
* **Démotion** — déterministe, post-fork, uniquement sur candidats validés (skill
  réellement matérialisé). Lien retour E1 **d'abord** (``FOLDED_INTO``), puis
  ``store.remove``. Pire cas (AC2) : le fragment reste dans la mémoire ET le lien
  retour est écrit — jamais l'inverse.

Ce module n'a PAS d'effet de bord tant qu'on n'appelle pas ``demote`` (qui est
lancé par le wiring du curator, post-fork, sous la branche ``consolidate`` — AC3 :
``consolidate=False`` ⇒ rien de tout ça ne s'exécute, comportement du jour).
"""

from __future__ import annotations

import logging
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Un motif E3 n'est « éligible » à l'uplift que s'il regroupe au moins N entrées :
# c'est le trigger (a) de la carte (≥2 fragments du même motif).
MIN_ENTRIES_FOR_UPLIFT = 2

# Limite de caractères par entrée affichée verbatim dans le prompt uplift. On garde
# la phrase complète dans la pratique (les entrées mémoire sont courtes) mais on borne
# pour ne pas exploser le budget prompt si une entrée est pathologiquement longue.
_ENTRY_EXCERPT_CHARS = 600


# ---------------------------------------------------------------------------
# Slug dérivé du label du motif
# ---------------------------------------------------------------------------


def derive_slug(label: str) -> str:
    """Slug de skill (kebab-case) dérivé du *label* d'un motif E3.

    Le champ ``target`` d'un motif est le bucket mémoire (``memory``/``user``), PAS un
    slug — d'où la dérivation ici. Règles :
    - diacritiques translittérés vers la lettre de base (é→e) : les labels FR restent lisables
      (``Référencer`` → ``referencer``, pas ``r-f-rencer``) ;
    - si le label ne contient AUCUNE lettre latine de base (ex. ``éàç``) → ``""`` : pas de slug
      valable, le motif est laissé à un humain (c'est le skip de ``eligible_patterns``) ;
    - camelCase → kebab (``PyGount`` → ``py-gount``), borné à 64 ;
    - contrat du slug : ``^[a-z][a-z0-9-]*$`` (lettre initiale, pas de chiffre en tête).
    """
    raw = label or ""
    if not re.search(r"[a-zA-Z]", raw):
        # Purely diacritic / non-Latin label → no valid base skeleton → no slug (skip-guard).
        return ""
    t = unicodedata.normalize("NFKD", raw)
    t = "".join(c for c in t if not unicodedata.combining(c))  # drop diacritics (é→e)
    t = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", t)  # camelCase → kebab, before lowercasing
    t = re.sub(r"[^a-z0-9]+", "-", t.lower())
    t = re.sub(r"-{2,}", "-", t).strip("-")
    if not t:
        return ""
    if not t[0].isalpha():  # contrat : lettre initiale (pas de chiffre en tête)
        t = "skill-" + t
    return t[:64].rstrip("-")


# ---------------------------------------------------------------------------
# Candidats éligibles (lecture seule du ledger E3)
# ---------------------------------------------------------------------------


def eligible_patterns(ledger: Optional[Dict[str, Any]] = None,
                      min_entries: int = MIN_ENTRIES_FOR_UPLIFT) -> List[Dict[str, Any]]:
    """Motifs E3 éligibles à l'uplift : ≥ ``min_entries`` entrées groupées.

    ``ledger`` None → on charge le sidecar (``.memory_patterns.json``) via E3. Retourne
    une liste de dicos ``{key, label, target, entries, canonical, slug}`` triée par
    label pour une détermination stable du prompt. Lecture seule : ne modifie rien.
    """
    if ledger is None:
        from tools import memory_patterns

        ledger = memory_patterns.load_ledger()
    out: List[Dict[str, Any]] = []
    for p in (ledger or {}).get("patterns", []) or []:
        if not isinstance(p, dict):
            continue
        entries = [e for e in (p.get("entries") or []) if isinstance(e, str) and e.strip()]
        if len(entries) < min_entries:
            continue
        label = str(p.get("label") or "").strip()
        # Garde fail-closed (AC E2, audit OMH 2026-10-02 reco 2b) : une entrée sans
        # motif n'est PAS candidate à la promotion/démotion — pas de label = pas de
        # motif = pas de candidat, vérifié ICI à la sélection, AVANT tout jugement
        # modèle. Jamais « le modèle décide dans le doute ». Nommée
        # `unexplained_entries_are_not_candidates` pour traçabilité.
        if not label:
            continue  # unexplained_entries_are_not_candidates
        slug = derive_slug(label)
        if not slug:
            continue
        out.append({
            "key": p.get("key") or "",
            "label": label,
            "target": p.get("target") or "memory",
            "entries": entries,
            "canonical": p.get("canonical") or (entries[0] if entries else ""),
            "slug": slug,
        })
    out.sort(key=lambda c: (c["label"].lower(), c["slug"]))
    return out


# ---------------------------------------------------------------------------
# Consultation read-only de jev-curator (C3) — fail-open
# ---------------------------------------------------------------------------


def _load_jev_service() -> Any:
    """Import the ``jev-curator`` plugin (in the profile's Hermes home, NOT the repo)
    and build its read-only service. Returns the service, or ``None`` when the plugin
    is absent / unimportable (hyphenated dir) / its ``build_service`` fails.

    Never raises: the consultation is fail-open by design (C3 — pass through the
    existing gate; its absence must not block the uplift pass).
    """
    try:
        import importlib.util
        import sys

        from hermes_constants import get_hermes_home

        pkg_dir = get_hermes_home() / "plugins" / "jev-curator"
        init = pkg_dir / "__init__.py"
        if not init.is_file():
            return None
        name = "jev_curator_pkg"
        if name in sys.modules:
            mod = sys.modules[name]
        else:
            spec = importlib.util.spec_from_file_location(
                name, str(init), submodule_search_locations=[str(pkg_dir)])
            if spec is None or spec.loader is None:
                return None
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod
            spec.loader.exec_module(mod)
        service = mod.service.build_service(None)
        return service
    except Exception as exc:  # noqa: BLE001
        logger.debug("memory uplift: jev-curator service unavailable: %s", exc)
        return None


def consult_jev(candidates: List[Dict[str, Any]], service: Any = None) -> Dict[str, Any]:
    """Consultation *read-only* du gate ``jev-curator`` (mode observe par défaut).

    Retourne un objet compact ``{"ok", "mode", "summary", "relations"}`` à injecter
    dans le prompt uplift, pour que la fork voit le paysage de relations EXISTANT
    (doublons / sous-ensembles / complémentarité) avant de choisir créer-vs-plier.

    C3 : on passe par le gate existant, on n'en crée pas un second. ``scan()`` en mode
    observe produit des jugements sans aucune mutation (``apply`` reste refusé hors
    mode apply). ``service`` injectable (tests / découplage) ; ``None`` → on importe le
    plugin. Tout échec (pas de route, pas de config, plugin absent) → ``{"ok": False,
    ...}`` et la passe continue SANS preuve jev : la consultation est un apport, pas
    une porte d'entrée bloquante.
    """
    if not candidates:
        return {"ok": False, "mode": "", "summary": "no uplift candidates", "relations": []}
    if service is None:
        service = _load_jev_service()
    if service is None:
        return {"ok": False, "mode": "",
                "summary": "jev-curator unavailable; no relation evidence (fail-open)",
                "relations": []}
    try:
        scan = service.scan()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "mode": str(getattr(getattr(service, "settings", None), "mode", "") or ""),
                "summary": f"jev-curator scan failed ({type(exc).__name__}) — continuing without evidence",
                "relations": []}
    judgments = scan.get("judgments", []) or []
    # On n'injecte que les relations SIGNALÉES (pas unrelated / pas enough evidence)
    # pour tenir le prompt court.
    interesting = [j for j in judgments if isinstance(j, dict)
                   and j.get("relation") in {"duplicate", "subset", "same_class", "complementary", "conflict"}]
    rels = []
    for j in interesting[:20]:
        rels.append({
            "a": j.get("a") or j.get("skill_a") or "",
            "b": j.get("b") or j.get("skill_b") or "",
            "relation": j.get("relation", ""),
            "confidence": j.get("confidence"),
        })
    return {
        "ok": True,
        "mode": str(getattr(getattr(service, "settings", None), "mode", "") or ""),
        "summary": f"jev-curator ({len(rels)} relation(s) of note across {len(judgments)} judged pair(s))",
        "relations": rels,
    }


# ---------------------------------------------------------------------------
# Section « uplift » du prompt de la fork
# ---------------------------------------------------------------------------


def render_uplift_section(candidates: List[Dict[str, Any]],
                          existing_slugs: Optional[set] = None,
                          jev: Optional[Dict[str, Any]] = None) -> str:
    """Section de prompt pour la fork : un bloc par candidat avec entrées verbatim +
    slug attendu + état du skill cible + (optionnel) preuve jev.

    ``existing_slugs`` = slugs de skills déjà présents dans le live tree (permet à la
    fork de choisir patch-existant vs créer-nouveau, cas (b)/(c) de la carte).
    Retourne ``""`` s'il n'y a aucun candidat.
    """
    if not candidates:
        return ""
    existing = {s for s in (existing_slugs or set())}
    lines = [
        "## Memory → skill uplift (PROPOSE, do not write to the versioned house tree)\n",
        "The E3 task-pattern ledger found the memory groups below. Each group is a reusable",
        "PROCEDURE that keeps re-injecting its fragments into the bounded memory budget",
        "(2200/1375 chars). Promote each ONE to a skill in the LIVE tree and I will",
        "deterministically fold the source entries out of memory (backlink first, then remove).\n",
        "Rules:",
        "  • Use ``skill_manage`` to CREATE the skill with the EXACT slug given, or PATCH an",
        "    existing skill if its slug already exists (a single strong fragment may justify",
        "    a patch of a living skill).",
        "  • Write the procedure into the skill body / a references file — the fragments must",
        "    be recoverable from the skill, or the memory fold loses information.",
        "  • Write to the LIVE skill tree ONLY. Never target the versioned house skills dir.",
        "  • In the structured summary block, add an ``uplifts:`` list — one entry per",
        "    promoted group: `- slug: <slug>` / `key: <pattern-key>` / `reason: <why>`.\n",
    ]
    for i, c in enumerate(candidates, 1):
        state = "EXISTS in live tree (patch it)" if c["slug"] in existing else "not present (create it)"
        lines.append(f"### Uplift {i}: `{c['slug']}`  (pattern key `{c['key']}`, target bucket `{c['target']}`, {state})")
        lines.append(f"Label: {c['label']}")
        lines.append("Source entries (verbatim — fold these into the skill):")
        for e in c["entries"]:
            excerpt = e if len(e) <= _ENTRY_EXCERPT_CHARS else e[:_ENTRY_EXCERPT_CHARS].rstrip() + " …"
            lines.append(f'  > {excerpt}')
        lines.append("")
    if jev and jev.get("ok"):
        lines.append(f"### jev-curator relation evidence (read-only, mode {jev.get('mode') or '?'})")
        if jev.get("relations"):
            for r in jev["relations"]:
                lines.append(f"  • {r['a']} ↔ {r['b']}: {r['relation']}"
                             + (f" (conf {r['confidence']})" if r.get("confidence") is not None else ""))
        else:
            lines.append("  (no notable relations among existing skills)")
        lines.append("")
    lines.append("Add the ``uplifts:`` list to the required ```yaml structured block at the end.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Démotion déterministe : lien E1 d'abord, puis retrait
# ---------------------------------------------------------------------------


def write_fold_links(entries: List[str], target: str, slug: str) -> int:
    """Écrire le lien retour E1 (``FOLDED_INTO``) pour chaque entrée source.

    Renvoie le nombre de liens écrits (0 si le ledger est off / indisponible — fail-open :
    la démission continue, le pire cas reste « fragment conservé + lien »).
    """
    from tools.provenance_ledger import entry_source_id, record

    written = 0
    for text in entries:
        if not text or not text.strip():
            continue
        row = record(entry_source_id(text), target, slug, "FOLDED_INTO", source_text=text)
        if row is not None:
            written += 1
    return written


def demote(pattern: Dict[str, Any], slug: str, store: Any) -> Dict[str, Any]:
    """Démotion d'un motif validé : lien E1 d'abord, puis retrait des entrées sources.

    ``pattern`` = un candidat de :func:`eligible_patterns` (doit porter ``target``,
    ``entries``, ``key``). ``store`` = un ``MemoryStore`` (``load_on_disk_store()`` ou
    l'instance live). Ordre critique : le lien retour est écrit AVANT tout retrait,
    donc même si un ``remove`` échoue (entrée déjà absente, ambiguïté, drift), le
    fragment reste récupérable et le lien existe — pire cas AC2 = fragment conservé + lien.

    Retourne un rapport ``{slug, key, target, link_written, removed, kept,
    chars_before, chars_after, budget_dropped}``.
    """
    target = pattern.get("target") or "memory"
    entries = [e for e in (pattern.get("entries") or []) if isinstance(e, str) and e.strip()]
    report: Dict[str, Any] = {
        "slug": slug, "key": pattern.get("key", ""), "target": target,
        "link_written": 0, "removed": [], "kept": [],
        "chars_before": 0, "chars_after": 0, "budget_dropped": 0,
    }
    if not entries:
        return report
    # 1. Lien retour E1 d'abord (jamais d'abord le retrait).
    report["link_written"] = write_fold_links(entries, target, slug)
    # 2. Retrait déterministe, entrée par entrée.
    report["chars_before"] = int(store._char_count(target))
    for e in entries:
        try:
            res = store.remove(target, e)
        except Exception as exc:  # noqa: BLE001 — un retrait en échec ne doit pas tuer la passe
            logger.debug("memory uplift: remove failed for %r: %s", e[:60], exc)
            report["kept"].append(e)
            continue
        if isinstance(res, dict) and res.get("success"):
            report["removed"].append(e)
        else:
            # Pire cas AC2 : le fragment reste, mais le lien est déjà écrit.
            report["kept"].append(e)
    report["chars_after"] = int(store._char_count(target))
    report["budget_dropped"] = max(0, report["chars_before"] - report["chars_after"])
    return report


# ---------------------------------------------------------------------------
# Validation post-fork : un candidat n'est démis que si le skill est matérialisé
# ---------------------------------------------------------------------------


def validated_uplifts(candidates: List[Dict[str, Any]],
                      materialized_slugs: set) -> List[Dict[str, Any]]:
    """Filtre les candidats dont le slug a été RÉELLEMENT matérialisé par la fork.

    ``materialized_slugs`` = slugs présents dans le live tree POST-fork (et/ou déclarés
    par la fork dans ``uplifts``). C'est la garde anti-démemoi-aveugle : on ne retire
    jamais un fragment mémoire si le skill cible n'existe pas.
    """
    materialized = {s for s in (materialized_slugs or set()) if s}
    return [c for c in candidates if c.get("slug") in materialized]