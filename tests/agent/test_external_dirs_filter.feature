# Contract for per-entry filtering of skills.external_dirs.
# Issues: NousResearch/hermes-agent#100254, #129562.  Card: t_554b298f (spec t_dc5fe2ff).
# "EXTERNAL_DIR" denotes one fresh temporary skills directory created by the
# implementing pytest scenario in tests/agent/test_external_dirs_filter.py
# (one test per Scenario, matched by Scenario name).  The repository does not
# consume .feature files in CI (no pytest-bdd); this file freezes the contract,
# the six tests execute it.
Feature: Per-entry filtering of skills.external_dirs
  An external_dirs entry is either a plain string path (legacy: every valid
  skill of the directory is visible) or a mapping {path, include?, exclude?}
  whose include/exclude are fnmatch globs applied to the DECLARED skill name.
  Priority: skills.disabled always wins, then the entry's exclude, then its
  include; an empty or absent include admits everything the other two rules
  leave standing.

  Scenario: External dir entry as a plain string exposes all skills
    Given a config with skills.external_dirs containing a plain string path "EXTERNAL_DIR"
    And the directory "EXTERNAL_DIR" contains skills "ansible-core", "git-flow", "docker-build"
    When the skill catalog is resolved
    Then the visible skills are exactly "ansible-core", "docker-build" and "git-flow"

  Scenario: Include filter restricts to matching skill names
    Given a config with skills.external_dirs containing {path: "EXTERNAL_DIR", include: ["ansible-*"]}
    And the directory "EXTERNAL_DIR" contains skills "ansible-core", "ansible-vault", "git-flow"
    When the skill catalog is resolved
    Then the visible skills are exactly "ansible-core" and "ansible-vault"

  Scenario: Exclude filter hides matching skill names
    Given a config with skills.external_dirs containing {path: "EXTERNAL_DIR", exclude: ["git-*"]}
    And the directory "EXTERNAL_DIR" contains skills "ansible-core", "git-flow", "git-hooks"
    When the skill catalog is resolved
    Then the visible skills are exactly "ansible-core"

  Scenario: Exclude takes precedence over include when both match
    Given a config with skills.external_dirs containing {path: "EXTERNAL_DIR", include: ["git-*"], exclude: ["git-hooks"]}
    And the directory "EXTERNAL_DIR" contains skills "git-flow", "git-hooks", "ansible-core"
    When the skill catalog is resolved
    Then the visible skills are exactly "git-flow"

  Scenario: skills.disabled overrides include filter
    Given a config with skills.disabled containing "ansible-core"
    And skills.external_dirs containing {path: "EXTERNAL_DIR", include: ["ansible-*"]}
    And the directory "EXTERNAL_DIR" contains skills "ansible-core", "ansible-vault"
    When the skill catalog is resolved
    Then the visible skills are exactly "ansible-vault"

  Scenario: Two profiles sharing the same external dir see different skill lists
    Given profile A has a config with skills.external_dirs containing {path: "EXTERNAL_DIR", include: ["ansible-*"]}
    And profile B has a config with skills.external_dirs containing {path: "EXTERNAL_DIR", exclude: ["ansible-*"]}
    And the directory "EXTERNAL_DIR" contains skills "ansible-core", "git-flow", "docker-build"
    When profile A then profile B each resolve the skill catalog
    Then profile A sees exactly "ansible-core" and profile B sees exactly "docker-build" and "git-flow"
