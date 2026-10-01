# Development log

SmartSort development began on 2026-10-01, with accuracy and release review continuing on 2026-10-02. This log distinguishes implemented behavior and checks performed during development from later consolidated release validation. It does not claim that hosted CI or GitHub publication succeeded.

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

The latest full Windows/Python 3.14.7 run completed with **195 passed, 6 skipped in 20.32 seconds**. All six skips require file symlink creation privileges unavailable in this environment; real Windows junction and hard-link scenarios passed. The suite includes 18 upstream characterization tests and 28 GUI tests. No artificial coverage target or additional lint/type checker was introduced.

Independent disposable CLI checks confirmed five moves followed by hash-verified restoration, four copies with one duplicate skipped followed by undo, one quarantined duplicate followed by undo, three persistent sessions, and ten cumulative successful operations. CLI help, the four-rule configuration example, import purity, and GUI startup without state creation were checked. The configured hosted matrix remains unexecuted locally.

## Documentation and release evidence

README and architecture documentation describe the implemented interfaces, rule semantics, contained destinations, pure preview, journal ordering, identity-checked undo, duplicate report observations, and monitoring behavior. They also record current limits: creation time can be unavailable, recovery is read-only, byte-copy metadata is not fully preserved, stability is heuristic, and OS races cannot be eliminated by application checks.

Actual screenshots are captured from the working desktop for the README. Consolidated automated results, clean-install checks, disposable CLI workflows, Git state, and publication outcome belong in the final release report after those gates are completed. This log deliberately does not predict their totals or declare publication complete.
