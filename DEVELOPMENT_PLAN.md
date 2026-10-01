# SmartSort implementation plan

Scope: SmartSort only. The six-project Phase 1 reports supplied by the user remain the planning baseline; the other five projects are not being implemented.

Upstream: https://github.com/Shunlauk/file-organizer-python
Audited and cloned commit: 64faf6652918fc5f01b830849f7b88c43e43d944.
All three upstream commits are preserved. Clone HEAD matched the audit; no new commits required reassessment. LICENSE retains Copyright (c) 2026 Love_Jangid.

Target Python: 3.11+; the upstream required 3.8+, while the extension uses modern standard-library typed records. Development runtime verified as 3.14.7 with Tk and SQLite available. Only pytest is required for development; watchdog is an optional monitoring extra.

1. Capture upstream defects with real subprocess runs in pytest temporary sandboxes.
2. Add import-safe models/package, prune scanner trees and validate ordered JSON rules; package original extension defaults.
3. Separate pure, collision-reserving plans from execution. Revalidate identities and destinations; report each file failure.
4. Journal intentions and created-file identity durably in SQLite before source cleanup; provide conservative reconciliation and safe multi-session undo.
5. Implement collection duplicate reports and explicit keep/skip/quarantine without automatic deletion.
6. Deliver shared-core non-interactive CLI and threaded Tkinter/ttk GUI with preview approval, rules, duplicates, history, settings/logs and actual statistics.
7. Add opt-in watchdog monitoring with stability windows, download exclusions, suppression and clean cancellation.
8. Verify meaningful regression/feature tests, disposable-folder workflows, actual GUI startup/screenshots, clean installation, license and secret/runtime-data hygiene.
9. Commit each completed logical milestone. Publish only when all release gates pass and an authenticated creation/push method exists; never replace an existing repository.
10. Write FINAL_REPORT.md after the complete local product is verified, with any publication blocker stated truthfully.

Safety design: SQLite cannot make filesystem actions atomic. Persist intents before exclusive destination creation and preserve ambiguous pending work for explicit recovery. Identity checks combine metadata/inode and content hashing; filesystem paths remain root-bounded, symlinks/reparse points are rejected. Preview performs reads only. Adversarial concurrent path substitution and unstable paused downloads require honest documented limitations.

Tests accompany changes. Milestones record their verification and remaining work in docs/DEVELOPMENT_LOG.md. Original upstream code remains recoverable in Git history and a licensed test fixture used only inside temporary sandboxes.
