# PR #6767 visual and browser evidence

## Scope and sources

Chromium, 1440×1000 and 390×1000, default dark appearance, English (UK).
The real WebUI server, HTML, CSS, task rendering, run-output loader and completion
poller were used with an isolated empty state directory and an inert Agent root.
Cron GET responses are browser fixtures containing two sample jobs and one output;
Pause/Resume POSTs are intercepted and change only the fixture; other cron writes
are rejected. No real jobs, provider calls or production state are used.
This establishes browser rendering and refresh behaviour, not backend scheduling.

`before-*`: master `3179f55609d2483d6489ec707b88ad186716e6cd` panels/CSS.
`review-before-*`: reviewed PR head `9eb8f947c8f47e5fa04b5b4126e88cdf173d9900`
panels/CSS, served in the same current HTML shell to isolate this regression.
`after-*`: fixed implementation in this evidence commit, including the follow-up
that preserves a pending explicit detail refresh when a background poll supersedes it.

## Scenario and observed results

Open Morning summary and click its run header to load sample output. Seed its
unread state so the output highlight is visible. Deliver an unrelated completion
for Sample backup check through the real completion-poll code. Compare the list
and output after the asynchronous work settles.

On master the badge records the completion but the list does not refresh; its
output highlight is green. On the reviewed PR head the new red list dots appear,
but the open output is rebuilt: the run closes and loaded content disappears.
On the fixed head the red dots appear, matching the desktop rail badge, and the
selected run remains open with exactly the same loaded text at both widths.
No uncaught browser errors were observed in any of the six runs.

The follow-up browser checks deliberately hold the list refresh from Pause/Resume,
let a newer completion poll finish first, then release the held request. At both
widths Pause displays the paused state and Resume control; Resume displays the
active state and Pause control. `action-paused-*` and `action-active-*` screenshots
and the `action_races` measurements record these results. An explicit user action
retains the existing detail-refresh behaviour; ordinary completion-only polls
still preserve the loaded output. Subsequent polls do not repeat a completed
explicit detail refresh. Node regressions also cover both completion orders,
form/selection changes and recovery after a failed latest request.

## Images

| State | Desktop list | Desktop output | Phone drawer | Phone output |
| --- | --- | --- | --- | --- |
| Master | [list](before-1440-list.png) | [output](before-1440-output.png) | [drawer](before-390-list.png) | [output](before-390-output.png) |
| Reviewed PR | [list](review-before-1440-list.png) | [collapsed output](review-before-1440-output.png) | [drawer](review-before-390-list.png) | [collapsed output](review-before-390-output.png) |
| Fixed | [list](after-1440-list.png) | [preserved output](after-1440-output.png) | [drawer](after-390-list.png) | [preserved output](after-390-output.png) |

## UX inspection and limitations

The phone drawer and phone detail were checked separately using the actual
hamburger button. Sample job names truncate in the compact list but retain their
full title attribute; the detail heading and output remain legible. The red dots
and output border use the same unread colour rather than success green. No new
control or layout change was introduced. The isolated gateway warning is expected
because this fixture intentionally has no running Agent gateway.

The desktop document-wide `scrollWidth > innerWidth` probe is true on all three
versions; it is false at phone width. This unchanged measurement is recorded,
not treated as proof that the whole application is overflow-free. The inspected
task list/output screenshots show no new clipping from this PR. This check covers
only the default dark appearance, not all skins, browsers, or real scheduled runs.
Final visual acceptance belongs to the maintainer; these screenshots are evidence,
not a claim of maintainer approval.

## Reproduce

With the repository's Python environment plus Playwright and Chromium installed:

```sh
python docs/pr-media/6767/capture.py
```

`capture.py` starts and stops its own loopback-only server, allocates temporary
fixture state, captures the 16 PNGs, and writes exact browser measurements to
`results.json`. It asserts the reviewed-head collapse and the fixed-head
preservation at both widths. The `after` assets come from the current checkout;
rerunning on changed implementation will produce new evidence.

Related automated verification: 46 tests passed across
`test_cron_refresh_button_835.py`, `test_issue6767_cron_refresh_races.py`,
`test_issue3460_cron_session_unread.py`, and `test_issue1140_cron_badge_per_job.py`.
The refresh-button static signature test now accepts the optional `options`
parameter while retaining `animate` as the first parameter and preserving all
existing button restoration/accessibility checks.
