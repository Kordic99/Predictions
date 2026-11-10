# Livesport collection pause — 2026-11-10

**Superseded by [the all-connections pause](web-connections-pause.md): schedule
updates and GitHub archive synchronization are now paused too.**

Paused by the owner pending clarification of source permissions. This is not a
data deletion or a legal determination.

- Disabled on GitHub: update-player-stats.yml, update-match-context.yml and
  update-rosters.yml (the Friday audit depends on Livesport).
- Removed their cron triggers and set their jobs to `if: ${{ false }}` so merely
  re-enabling a workflow cannot resume collection.
- Paused the Codex heartbeat `hodinov-kontrola-chance-ligy`; it must not dispatch
  the disabled Friday job as a recovery attempt.
- Python Livesport network boundaries and collector CLI entry points fail before
  requests or writes. There is no environment override.
- Browser automatic results timers, manual result checks, low-level requests and
  career-update request generation are paused. The page labels saved data as
  potentially outdated. Reload existing browser tabs to receive this version;
  old downloaded HTML copies are not remotely changed.
- Existing JSON files, player data, Results, Predictions, Accuracy, Accuracy New
  and immutable prediction archive are retained. Saved sibling JSON files may
  still be read; that does not contact Livesport.
- Manual prediction locking, append-only archive synchronization and JSON
  backup/import remain available. The official Chance Liga schedule updater is
  independent and remains enabled.

The existing public data has NOT been removed or made private. Any change to
publication or deletion needs a separate owner decision. Do not silently resume
collection or produce Friday predictions from stale replacement inputs. Resume
only with explicit approval after the source-permission question is resolved.
