# Prediction history

`data/predictions/2026-27.json` is the append-only public archive. It is independent
of `index.html`, player feeds and browser storage. Each record retains its original
lock, creation time, probabilities, model version and calculation/lineup snapshot
(when the original export contained one), plus official fixture ID, archive receipt,
source, eligibility and SHA-256 content ID. Original exports are never uploaded in
full. Recovered records retain their source filename and checksum, not local paths.

## Manual locking only

Only clicking Save to Accuracy locks a new prediction. Neither opening the page,
refreshing lineups nor approaching kickoff creates a snapshot. The former browser
timer and hourly server generator were disabled at the user's request on 2026-10-26.
The historical records were not changed.

GitHub Actions **Archive predictions** validates history after relevant pushes and
accepts authenticated submissions of manually locked snapshots. An empty dispatch
only validates existing data. The compatibility CLI command `generate` is also a
read-only validation, so an older queued workflow cannot create a prediction.

Unlocked forecasts may use refreshed inputs. Manual locking captures the available
calculation, lineup and source context at that moment. Later input refreshes cannot
change the locked prediction. News/social/odds enrichment is not implemented by this
change; any future research feed must follow this same manual-lock boundary.

## Browser and manual changes

On loading the app, the archive restores missing Accuracy and Predictions rows.
Imports merge history. A separate compressed local journal retains distinct locks
and draft match inputs even if the large player snapshot cannot be saved. It is not
deleted to free quota. A server copy, not browser storage, is the durable guarantee.
The browser's Clear action does not clear this history.

Local/manual forecasts are explicitly
shown as **not confirmed on GitHub** until their identical snapshot appears in the
archive. In Accuracy, **Zálohovat lokální predikce** accepts a fine-grained token for
Kordic99/Predictions with **Actions: write**. The credential is held only in memory,
cleared after submission/dialog close, and never exported or stored. Each dispatch
is confirmed by reading the actual published archive, not by an HTTP dispatch success.
Browser requests cannot choose a code ref, file path or command. The workflow treats
submission JSON as data, validates the official fixture and snapshot integrity,
and archives only prediction records. Manual submissions received after kickoff are
preserved but excluded from evaluation, regardless of a claimed earlier client time.

## Recovery and evaluation

40 P2 snapshots recovered from the three supplied exports; two were originally
created after kickoff and are excluded. The seven July records have no full original
input snapshot; their probabilities/time are preserved without inventing inputs.
Older missing rounds cannot be recreated honestly. Legacy recovery trusts the
original export timestamps and is labelled separately from server-timestamped data.

An alternate version is retained as its own record. Accuracy chooses the earliest
eligible snapshot for each fixture; integrity and time checks prevent a later result
from rewriting it. No one-click delete exists for immutable history. Deliberate
corrections require an explicit audited change, never silent replacement.

Publication uses the GitHub Contents API's expected blob SHA, retries concurrent
changes and merges the remote archive before retrying. It writes only the archive;
schedule/player commits are retained. Tokens never appear in code or logs.
The workflow also runs on application/archive pushes and compares the archive with
the pre-push revision. A removed or rewritten record fails CI. Git history remains
the recovery source even if someone edits files outside the app; this is a CI alert,
not a claim that administrators are prevented from changing the repository.

## Verification / operations

```
npm ci --ignore-scripts
npx playwright install --with-deps chromium
node --test tests/prediction_archive.test.cjs
node scripts/archive_predictions.cjs validate
```

Recovery is a deliberate operator-only local command with explicit export paths:
`node scripts/archive_predictions.cjs recover <export.json> ...`.
Do not use it to bless forecasts made after results became known.

The compact journal and cloud archive coexist with existing full-data JSON exports;
no results, player data or manual inputs are replaced by an archive download.
