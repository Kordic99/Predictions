# All data connections disconnected — 2026-11-10

This supersedes the earlier Livesport-only pause. Explicit owner approval is
required before any source, synchronization or schedule can be re-enabled.

- All five data workflows are disabled on GitHub and guarded with `if: false`.
  Schedule cron is removed. Codex oversight is PAUSED with non-restart instructions.
- Browser live fetches, polling, background refresh, manual refresh and GitHub
  submission are disconnected. CSP `connect-src 'none'` is a second safety layer.
- Python fetch boundaries reject ALL sources (including Transfermarkt, official
  schedule and club websites). The GitHub archive publisher also fails closed.
- The app reads a static `offline-saved-data.js` packaged from the existing five
  JSON files. `node scripts/build_offline_snapshot.cjs --check` verifies exact
  reproducibility; generation is local-only and does not alter source files.
- Existing predictions, immutable snapshots, rosters, Results, context and manual
  browser data remain available. Manual edits and prediction locks remain local.
  Export JSON is now required for backing up new edits; no GitHub confirmation is
  implied. Existing archived receipt/evaluation information is preserved.
- Ordinary page/JavaScript asset delivery on GitHub Pages remains enabled so the
  app is accessible. Source hyperlinks are references, not automatic requests.
  Clicking such a link intentionally opens its website; it is not an integration.
- This does NOT delete or privatize previously published data or Git history.
  Data-publication rights remain a separate unresolved issue.

Reload existing tabs (Ctrl+F5). Previously downloaded old HTML files cannot be
remotely disconnected. A local copy of the current app requires its sibling JS
files, including `offline-saved-data.js`.
