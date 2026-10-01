# Development log

SmartSort development began on 2026-10-01, with accuracy, release review, and public GitHub publication continuing on 2026-10-02. This log distinguishes checks performed during development from consolidated release validation and records the verified publication below.

## Planning baseline and repository setup

Implementation used the existing `PHASE_1_ANALYSIS.md` and `DEVELOPMENT_PLAN.md`; the other five audited projects were outside the implementation scope. A real clone retained upstream history, including audited commit `64faf6652918fc5f01b830849f7b88c43e43d944`. The original repository is retained as `upstream`, and the upstream MIT attribution is preserved.

An isolated virtual environment and `pyproject.toml` were established. SmartSort declares Python 3.11 or newer, keeps its core on the standard library, and uses development `pytest` plus optional watch-mode `watchdog`.

## Upstream characterization before replacement

Original source/configuration snapshots were obtained with `git show` from the audited commit and copied into disposable test sandboxes. Characterization tests reproduced preview logging, unknown-extension omission, inconsistent config locations, unpruned excluded trees, batch preview collisions, nested-category failure, lost undo after partial failure, destructive upstream undo, path escape, and import side effects.

The original CLI was also run against disposable folders, including missing-target and missing-config scenarios. [BASELINE_REPORT.md](BASELINE_REPORT.md) records snapshot hashes, actual sanitized output, the executed baseline result, and the distinction between reproducing an unsafe original and verifying a corrected implementation.

This milestone was committed separately as `test: capture upstream filesystem regressions` before replacing the original entry point.

## Configuration, scanning, and duplicate foundation

Typed records and enums were introduced. Configuration loading became independent of the current working directory, retained legacy extension mappings, and added validated version-1 rules. Rule predicates include extension, filename glob, inclusive size bounds, exclusive modified/available-created date conditions, and first-match ordering.

Scanning now prunes excluded and destination trees before descending, honors top-level/recursive and hidden options, filters extensions, skips links/reparse paths, and reports per-path errors. Collection-wide duplicates use size grouping before SHA-256 hashing and handle differently named files and unreadable candidates.

Completed foundations were committed as separate rule/scanner and duplicate-report changes. Their regression tests use disposable directories; no personal folders were organization targets.

## Planner, executor, journal, and undo

The planner returns an immutable side-effect-free plan and reserves destination names across a batch. The executor revalidates the approved destinations, isolates ordinary per-item failures, and records intent and progress in SQLite. Exclusive filesystem transfers and advisory root locks support the data-safety model.

Multi-session history, identity-checked move/copy undo, cumulative successful-operation statistics, and read-only interrupted-session inspection were implemented. Persistence or ambiguous transfer failures stop further mutation and preserve uncertain files for manual review. Filesystem and SQLite changes are explicitly not described as one atomic transaction.

Targeted tests exercise collision revalidation, partial failure, cross-filesystem fallback, journal write failures, occupied-source undo, changed-copy undo, pending-stage inspection, and real statistics. Cases requiring unavailable link privileges report a skip rather than a fabricated pass.

## CLI, monitoring, and desktop

The CLI exposes preview, organize, duplicates, history, undo, recover, stats, rules, watch, and gui workflows. Mutating automation requires explicit `--yes`; unknown extensions are noninteractive. Importing the application does not parse arguments or organize files.

The optional monitor uses stability tracking, temporary-download exclusions, serial execution, destination-event suppression, bounded ordinary retries, and safe stop behavior. It is never enabled at startup and does not process pre-existing files simply because monitoring starts.

The desktop contains functional Organize, Duplicate files, Rules, History, and Settings & logs areas. It consumes its cached approved preview and uses shared services. Workers receive captured plain values; queue callbacks update Tk widgets on the UI thread. Large tables render in short batches, and closing allows current filesystem work to finish safely.

Real Tk tests exercised preview purity, approval refusal, exact cached-plan consumption, changed-input invalidation, rule validation/apply/load/save, selected-extra quarantine, history detail/undo/recovery, statistics/logs, folder/storage selection, errors, cancellation, and UI responsiveness during large-table rendering. A real watch workflow started through the desktop, organized a newly arriving stable file, retained an existing file, and stopped cleanly.

A desktop startup smoke check exited successfully without creating its supplied state directory. The local GUI checks ran on Windows with Python 3.14.7 and an available Tk display. No hosted operating-system matrix result is inferred from those checks.

## Review corrections and consolidated checks

Independent code review identified and reproduced unsafe runtime log aliases, stale duplicate selections falling back to ordinary sorting, duplicate previews losing already computed content hashes, and a desktop Close race during monitor startup. Focused regressions now verify these corrections, including exclusive log rotation and a monitor that is stopped before UI adoption. Actual screenshot inspection also exposed clipped summaries at the default window size; layout and real Tk visibility checks were corrected before recapturing the two screenshots.

The latest full Windows/Python 3.14.7 run completed with **195 passed, 6 skipped in 20.44 seconds**, after the final visible-view desktop test correction. All six skips require file symlink creation privileges unavailable in this environment; real Windows junction and hard-link scenarios passed. The suite includes 18 upstream characterization tests and 28 GUI tests; all 28 GUI tests also passed in the focused run. No artificial coverage target or additional lint/type checker was introduced.

Independent disposable CLI checks confirmed five moves followed by hash-verified restoration, four copies with one duplicate skipped followed by undo, one quarantined duplicate followed by undo, three persistent sessions, and ten cumulative successful operations. CLI help, the four-rule configuration example, import purity, and GUI startup without state creation were checked. Hosted validation results are recorded below.

## Documentation and release evidence

README and architecture documentation describe the implemented interfaces, rule semantics, contained destinations, pure preview, journal ordering, identity-checked undo, duplicate report observations, and monitoring behavior. They also record current limits: creation time can be unavailable, recovery is read-only, byte-copy metadata is not fully preserved, stability is heuristic, and OS races cannot be eliminated by application checks.

Actual screenshots were captured from the working desktop for the README. Consolidated automated results, clean-install checks, disposable CLI workflows, Git state, and the verified publication outcome are recorded in [FINAL_REPORT.md](../FINAL_REPORT.md).

The wheel-only installation passed fifteen checks from an external temporary working directory, followed by five checks after rebuilding for the final quarantine mode constraint. The installed organizer's source hash matched the current source and wheel. No third-party core dependency or watchdog was installed in that environment; GUI startup and a clear missing-watch error were verified.

Pre-publication review found a clean Git tree, preserved audited upstream ancestry and license, and no tracked runtime databases/logs or personal paths. Token-pattern checks covered current source and full Git patch history. Initial sandbox credential checks did not provide usable publication access, so the first report correctly recorded that publication had not occurred at that point.

After the user renewed GitHub authorization, system credential access verified the `guxinyihan` account and its repository/workflow permissions. The requested repository name was available. [guxinyihan/smart-file-organizer](https://github.com/guxinyihan/smart-file-organizer) was created publicly with the requested description and topics. A transient connection reset interrupted the first push; the retry succeeded without changing history. GitHub's commit API confirmed `main` matched local revision `a39bf4b6913f14f7a60806d9bfb346858720c302`. The new `origin` points to that personal repository and the preserved original remote remains `upstream`. No credential was printed or existing repository overwritten. The first push triggered [hosted validation](https://github.com/guxinyihan/smart-file-organizer/actions/runs/36902742131).

## Hosted platform checks and final publication evidence

The initial macOS Python 3.11 job stalled in the desktop history test. A diagnostic run with unbuffered verbose output and a sixty-second traceback timer located the main thread inside native Tk `update()` while the test updated an unmapped history text widget. Both incomplete runs were stopped. The test now displays the window and selects History, then Settings before their respective actions, retaining every original assertion. The focused Windows desktop suite passed all 28 cases, followed by the full local result above.

[Run 36907043412](https://github.com/guxinyihan/smart-file-organizer/actions/runs/36907043412) completed with all six jobs successful at `4a141676d9b145705267d0ca2c536cc5b5c28936`. Each Windows version passed 201 tests; each macOS version passed 197 with four Windows-specific skips; each Linux version passed 170 with 27 no-display skips and four Windows-specific skips. Installation, CLI help, and packaged rules also passed in every job. [FINAL_REPORT.md](../FINAL_REPORT.md#12-automated-tests) records exact durations and limitations. A ten-minute job limit and detailed timeout diagnostics remain configured.

Direct Git HTTPS uploads remained intermittently unavailable after the initial pushes. Subsequent publication used the official Git Database API through the existing GitHub CLI authentication. Every returned blob/tree/commit SHA matched local objects, the remote parent was rechecked, and the branch update was a non-forced fast-forward. The source/test revision was verified publicly before this final documentation update.
