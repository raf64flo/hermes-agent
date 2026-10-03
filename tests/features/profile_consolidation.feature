@e4 @profile-consolidation
Feature: Bounded one-pass profile consolidation (USER.md)
  As an INRAE researcher whose agent profile is otherwise curated ad hoc,
  I want the agent to edit my profile (USER.md) in a bounded one-pass rewrite —
  never append — with a backlog, a cooldown, versioning, and stale-on-retract,
  so that a stale fact is flagged (not silently lost) and every edit is reversible.

  Background:
    # Config: memory.profile_consolidation (OFF by default; opt-in).
    # The model EDITS (rewrites the whole file), it does not append.
    # Backlog: memories/.profile/backlog.json; archives: memories/.profile/archives/USER.md.<ts>.md.

  Scenario: AC1 — an edit only fires after cooldown with enough candidates, and archives the old state
    Given the profile consolidation is enabled with min candidates "5" and cooldown "24" hours
    And the user profile has 2 existing bullets
    And 6 facts are pending in the backlog
    And the cooldown has elapsed since the last edit
    When the agent consolidates the profile into 8 bullets under the char limit
    Then the new USER.md has at most 8 bullets
    And the new USER.md is at or under the char limit
    And a snapshot of the previous 2-bullet USER.md is archived before the write
    And the backlog is cleared of the consumed facts

  Scenario: AC1 (refusal) — no edit before the cooldown or with too few candidates
    Given the profile consolidation is enabled with min candidates "5" and cooldown "24" hours
    And only 3 facts are pending in the backlog
    When the agent tries to consolidate the profile
    Then no edit is made and the current USER.md is unchanged
    And the reason is insufficient candidates

  Scenario: AC1 (cap) — an edit over the char or bullet limit is refused, file untouched
    Given the profile consolidation is enabled with min candidates "5" and cooldown "24" hours
    And 5 facts are pending in the backlog and the cooldown has elapsed
    When the agent consolidates into content over the char limit
    Then the edit is refused
    And the current USER.md is unchanged and no archive is created for the refused write

  Scenario: AC2 — a retracted source flags the profile bullet stale, bypassing the cooldown
    Given the profile consolidation is enabled with cooldown "24" hours
    And the profile has a bullet that a memory fact informed (a provenance link exists)
    When that memory fact is retracted (E1 marks the link revoked + stale)
    And the agent marks the stale bullets (fast path, no cooldown)
    Then the informed bullet is prefixed with a STALE marker
    And no bullet is deleted
    And the fast path did not wait for the cooldown

  Scenario: AC3 — a rollback restores the previous profile state
    Given the profile was edited and the previous state was archived
    When the agent rolls back the profile
    Then the current USER.md equals the archived previous state
    And the pre-rollback state is itself archived so a failed rollback can be undone

  Scenario: default-off — consolidation is disabled unless opted in
    Given the profile consolidation is not enabled
    When the agent tries to consolidate the profile
    Then no edit is made and the reason is that consolidation is disabled