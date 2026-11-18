"""Per-entry include/exclude filtering of ``skills.external_dirs`` (RED contract).

Regression targets: NousResearch/hermes-agent#100254, #129562.
Spec: kanban card t_dc5fe2ff ("spec-external-dirs-filter.md"); this file is
the frozen test contract for the implementation card t_0fc8b1b8.

The Gherkin contract lives beside this file
(``tests/agent/test_external_dirs_filter.feature``); each Scenario below maps
one-to-one onto one test, matched by scenario title:

1. plain string entry exposes every skill of the directory
   (backward compatibility - green before and after the implementation)
2. ``include`` restricts visibility to matching declared names
3. ``exclude`` hides matching declared names
4. ``exclude`` wins over ``include`` on the same entry
5. ``skills.disabled`` wins over ``include``
6. two profiles sharing one directory with different filters see different lists
plus one boundary case from the card's risk list: empty ``include``/``exclude``
lists admit everything (spec priority rules 3 and 4).

Behaviour is observed through the public catalog surface
``tools.skills_tool.find_all_skills()`` so the contract does not pre-invent the
implementation's internal API.  On today's code the dict form is stringified by
``_config_str_list``, resolves to a nonexistent directory and contributes zero
skills: tests 2-7 are RED; test 1 pins the unchanged legacy path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import hermes_yaml
from agent import skill_utils
from tools import skills_tool

SKILL_BODY = """---
name: {name}
description: Skill {name} pinned by the t_554b298f contract.
---

# {name}

Deterministic fixture body.
"""


def _clear_caches() -> None:
    """Drop every memo that could serve a stale catalog across config switches."""
    skill_utils._external_dirs_cache_clear()
    skills_tool.clear_skills_cache()


def _entry(path, include=None, exclude=None):
    """One ``external_dirs`` entry: plain string, or the new {path, include?, exclude?} form."""
    if include is None and exclude is None:
        return str(path)
    entry: dict[str, object] = {"path": str(path)}
    if include is not None:
        entry["include"] = list(include)
    if exclude is not None:
        entry["exclude"] = list(exclude)
    return entry


def _make_profile(tmp_path: Path, name: str = "profile") -> Path:
    """An isolated HERMES_HOME (config.yaml + empty local skills dir)."""
    home = tmp_path / name / ".hermes"
    (home / "skills").mkdir(parents=True)
    return home


def _write_external_dirs_config(home: Path, entries, disabled=()) -> None:
    skills_cfg: dict = {"external_dirs": list(entries)}
    if disabled:
        skills_cfg["disabled"] = list(disabled)
    (home / "config.yaml").write_text(
        hermes_yaml.safe_dump({"skills": skills_cfg}), encoding="utf-8"
    )


def _make_skill(root: Path, name: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(SKILL_BODY.format(name=name), encoding="utf-8")


def _visible_names(home: Path, monkeypatch) -> set[str]:
    """Point HERMES_HOME at *home*, invalidate every cache, return catalog names."""
    monkeypatch.setenv("HERMES_HOME", str(home))
    _clear_caches()
    return {s["name"] for s in skills_tool.find_all_skills()}


@pytest.fixture
def external_dirs_env(tmp_path, monkeypatch):
    """Isolated HERMES_HOME sharing one external skills dir with fresh caches."""
    home = _make_profile(tmp_path, "profile")
    external = tmp_path / "shared-skills"
    external.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)  # keep find_project_root() out of the real checkout
    _clear_caches()
    yield home, external
    _clear_caches()


def test_plain_string_entry_exposes_all_skills(external_dirs_env, monkeypatch):
    """Gherkin scenario 1: a string entry keeps listing every skill of the dir."""
    home, external = external_dirs_env
    for name in ("ansible-core", "git-flow", "docker-build"):
        _make_skill(external, name)
    _write_external_dirs_config(home, [_entry(external)])

    visible = _visible_names(home, monkeypatch)
    expected = {"ansible-core", "git-flow", "docker-build"}
    assert visible == expected, (
        f"plain-string entry must expose every skill, got {sorted(visible)}"
    )


def test_include_filter_restricts_to_matching_skill_names(
    external_dirs_env, monkeypatch
):
    """Gherkin scenario 2: include admits only names matching its globs."""
    home, external = external_dirs_env
    for name in ("ansible-core", "ansible-vault", "git-flow"):
        _make_skill(external, name)
    _write_external_dirs_config(home, [_entry(external, include=["ansible-*"])])

    visible = _visible_names(home, monkeypatch)
    expected = {"ansible-core", "ansible-vault"}
    assert visible == expected, (
        f"include ['ansible-*'] must admit exactly {sorted(expected)}, "
        f"got {sorted(visible)}"
    )


def test_exclude_filter_hides_matching_skill_names(external_dirs_env, monkeypatch):
    """Gherkin scenario 3: exclude removes every name matching its globs."""
    home, external = external_dirs_env
    for name in ("ansible-core", "git-flow", "git-hooks"):
        _make_skill(external, name)
    _write_external_dirs_config(home, [_entry(external, exclude=["git-*"])])

    visible = _visible_names(home, monkeypatch)
    assert visible == {"ansible-core"}, (
        f"exclude ['git-*'] must hide git-flow and git-hooks, got {sorted(visible)}"
    )


def test_exclude_takes_precedence_over_include(external_dirs_env, monkeypatch):
    """Gherkin scenario 4: a name hit by both lists is excluded (rule 2)."""
    home, external = external_dirs_env
    for name in ("git-flow", "git-hooks", "ansible-core"):
        _make_skill(external, name)
    _write_external_dirs_config(
        home, [_entry(external, include=["git-*"], exclude=["git-hooks"])]
    )

    visible = _visible_names(home, monkeypatch)
    assert visible == {"git-flow"}, (
        "exclude must win over include (git-hooks hidden) and a non-matching "
        f"name must stay out of an include-bounded entry (ansible-core hidden), "
        f"got {sorted(visible)}"
    )


def test_disabled_overrides_include_filter(external_dirs_env, monkeypatch):
    """Gherkin scenario 5: skills.disabled still wins over a matching include."""
    home, external = external_dirs_env
    for name in ("ansible-core", "ansible-vault"):
        _make_skill(external, name)
    _write_external_dirs_config(
        home, [_entry(external, include=["ansible-*"])], disabled=["ansible-core"]
    )

    visible = _visible_names(home, monkeypatch)
    assert visible == {"ansible-vault"}, (
        "skills.disabled ['ansible-core'] must not be resurrected by "
        f"include ['ansible-*'], got {sorted(visible)}"
    )


def test_two_profiles_sharing_dir_see_different_lists(
    external_dirs_env, tmp_path, monkeypatch
):
    """Gherkin scenario 6: per-entry filters partition one shared directory."""
    _home_a, external = external_dirs_env
    for name in ("ansible-core", "git-flow", "docker-build"):
        _make_skill(external, name)
    home_a = _make_profile(tmp_path, "profile_a")
    home_b = _make_profile(tmp_path, "profile_b")
    _write_external_dirs_config(home_a, [_entry(external, include=["ansible-*"])])
    _write_external_dirs_config(home_b, [_entry(external, exclude=["ansible-*"])])

    visible_a = _visible_names(home_a, monkeypatch)
    assert visible_a == {"ansible-core"}, (
        f"profile A (include ansible-*) must see only ansible-core, "
        f"got {sorted(visible_a)}"
    )
    visible_b = _visible_names(home_b, monkeypatch)
    assert visible_b == {"git-flow", "docker-build"}, (
        f"profile B (exclude ansible-*) must see git-flow and docker-build, "
        f"got {sorted(visible_b)}"
    )
    # Switching back must not serve a cross-profile cached catalog.
    visible_a_again = _visible_names(home_a, monkeypatch)
    assert visible_a_again == visible_a, (
        "the external-dirs cache must key on filters: re-resolving profile A "
        f"after profile B must not leak B's view, got {sorted(visible_a_again)}"
    )


def test_empty_include_and_exclude_lists_admit_everything(
    external_dirs_env, monkeypatch
):
    """Card risk list / spec rules 3-4: empty filter lists change nothing."""
    home, external = external_dirs_env
    for name in ("ansible-core", "git-flow", "docker-build"):
        _make_skill(external, name)
    _write_external_dirs_config(home, [_entry(external, include=[], exclude=[])])

    visible = _visible_names(home, monkeypatch)
    expected = {"ansible-core", "git-flow", "docker-build"}
    assert visible == expected, (
        f"empty include/exclude lists must admit everything, got {sorted(visible)}"
    )
