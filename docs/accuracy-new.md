# Accuracy New — prospective factual-context experiment

Version `2026.11-chronological-last5-v2` / context `2026.11-facts-v1`.

## What is compared

Every **new** manual or Friday automatic lock contains one baseline forecast and
one `snapshot.calculation.contextVariant`. Both share the exact locking timestamp,
fixture, player inputs and last-five league matches. They are one append-only archive
record, not competing duplicated predictions. Existing archived probabilities are
never recalculated, and old records without the second snapshot cannot be used in
the paired evaluation. Existing Accuracy remains the baseline; Accuracy New shows
both variants, paired Brier/log loss, double-chance and 1X2 accuracy, exact scores,
pending/excluded counts and the evidence used. Negative New-minus-baseline Brier or
log loss is better, but a small sample is not evidence of improvement.

## Time and form

The sequential model uses completed, **dated league matches known at the cutoff**,
ordered by actual kickoff rather than round number. Each team's recent-form state
retains its own last five matches. Midweek fixtures and postponed lower-round matches
are treated by date. Cups/Europe do not enter league form. They only enter workload.
The cutoff, last five IDs/dates/results/sources and both forecast outputs are frozen.
Rounds remain labels, not a data-availability filter.

Legacy historical Results without dates remain in the app, but cannot safely be
ordered and are excluded from this model's sequential training/form. They are not
assigned invented dates. Current roster/value inputs are observed at calculation
time; this implementation deliberately refuses historical recalculation using a
cutoff before the current page's input observation. A real historical backtest would
require original timestamped roster/value/context snapshots, not today's data.
If fewer than five eligible matches exist, the model reports the smaller count.
The current-season official schedule supplies missing fixture times when identity
matches; unknown times use a conservative end-of-day cutoff. Explicitly future
observations, updates, results and publication times are excluded.
For an unmarked recent score, completed-event evidence is required during the first
24 hours after kickoff, so an in-progress score cannot silently enter league form.
Legacy dated Results older than that are treated as the app's final-result records;
explicit live/postponed/abandoned statuses are always excluded.

## Free factual inputs

`scripts/update_prediction_evidence.py` reads public Livesport team results and
fixtures (all competitions), then official club websites linked by Chance Liga.
It extracts dated article titles/URLs via structured metadata, RSS/Atom and explicit
article time elements. No paid API, language-model service or secret is needed.
It does not bypass logins or claim complete news/social coverage. Official social
links are listed **without claiming their posts have been reviewed**. Missing,
undated or inaccessible pages are explicitly reported; no old feed is silently
relabelled as freshly checked. Original fetched/publication timestamps are retained.
Date-only article publication is conservatively treated as end of that Prague day.

The only **additional numeric effect in v1** is a conservative workload prior:

* Less than 72 hours between the previous match's finish and the target kickoff:
  multiply that team's expected goals by 0.97.
* At least three preceding competitive fixtures in the seven days before the
  target kickoff: another 0.02 reduction, capped at **0.95 overall**.
* Future intervening fixtures already scheduled at the cutoff can inform projected
  workload; their results are never assumed. Scheduled match duration is an explicit
  two-hour estimate. Actual completed times are used for past matches.
* Full results **and** fixture-feed coverage, checked within 48 hours of locking,
  is required. Partial, future-dated or stale evidence gives no correction. A lack
  of records is not interpreted as confirmed health or an automatic rest bonus.

These coefficients are experimental assumptions to evaluate prospectively, not
statistically proven improvements. The probabilities are recalculated from adjusted
goal rates with the same score matrix; these are **not percentage-point adjustments**.

Confirmed absences already affect the expected XI in both models; no second injury
penalty is applied. Stale/future lineup evidence falls back to a labelled squad proxy.
Uncertain injuries, rotation speculation, betting tips, headlines, sentiment and raw
European wins/losses have **zero standalone numeric effect**. A quality-of-European-
performance adjustment needs reliable opponent-strength/performance inputs; v1
does not invent them from a scoreline. Articles are linked as informational only,
including explicit zero effect in the frozen audit.

## Friday and persistence

The existing Friday 08:00 Europe/Prague job refreshes official schedule, all rosters,
values and Livesport context, then gathers public evidence. It predicts Friday
through the following Thursday (including midweek rounds). A short-lived receipt
hash-binds all model modules and input feeds. The new evidence file is part of the
allowed publication scope and the retained audit artifact. Both forecast variants
are backed up using the existing immutable GitHub archive and compare-and-swap
publication. Browser startup must finish loading inputs and successfully load the
published schedule before a new manual lock is allowed.

Optional news-source failures do not abort an otherwise valid forecast, but remain
visible as coverage gaps with zero speculative effect. Required schedule/roster/
strict lineup-refresh failures still stop the Friday production job.

Tests: `npm run test:archive`; `python -m unittest discover -s tests -p 'test_*.py'`.
No tests or preview run publish synthetic predictions or alter historical records.
