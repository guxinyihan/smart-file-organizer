# Audited upstream baseline

Recorded on 2026-10-01 for SmartSort, before replacing the upstream entry point.

## Provenance and environment

The source is [Shunlauk/file-organizer-python](https://github.com/Shunlauk/file-organizer-python), audited commit `64faf6652918fc5f01b830849f7b88c43e43d944`. The test fixtures were obtained with `git show` from that commit rather than from an edited working-tree file. Upstream attribution and the MIT license remain applicable to these snapshots.

| Snapshot | Original file | SHA-256 |
| --- | --- | --- |
| `tests/fixtures/upstream_script.txt` | `FileOrganizer.py` | `0830d61bbaa647ef7fd223d8ccb6689a174d2cec635959106a92a1b5e4d446ae` |
| `tests/fixtures/upstream_extensions.json` | `extension.json` | `1180637d2bdff8d875c34713fe046f2beb7b2e09b9556d4f1e394c6723b903c7` |

The original script requires Python 3.8 or newer: it uses the assignment expression in its hashing loop and `Path.unlink(missing_ok=True)`. SmartSort's new project metadata declares Python 3.11 or newer. This baseline was executed in the isolated project `.venv` using Python 3.14.7 on Windows; it does not claim validation of other Python versions or operating systems.

All original organization operations ran on copied scripts inside disposable temporary directories. Each sandbox contained separate `upstream-tool`, `selected`, and `working-directory` folders. Even demonstrations of destination escape, occupied-source overwrite, and changed-copy deletion stayed inside that one disposable sandbox. The installed project, ordinary user folders, and other five portfolio projects were never targets of these operations.

## Executed characterization tests

Command:

```text
python -m pytest tests/test_upstream_baseline.py -q
```

Observed result:

```text
..................                                                       [100%]
18 passed in 2.50s
```

These tests deliberately assert the unsafe **original behavior**. A passing upstream characterization test confirms a defect was reproduced; it is not a safety acceptance test for SmartSort. The original snapshots remain separate from SmartSort's production modules so these demonstrations continue to work after the entry point is refactored. A hash test prevents accidental fixture changes from silently altering the baseline.

| Upstream issue | Executed observation |
| --- | --- |
| Preview logging side effect | `--dry-mode` left source files unchanged but created a nonempty script-relative `organizer.log`. |
| First unknown extension omitted | A single unknown file prompted for a category but remained in place; the script reported `No File Found`. |
| Inconsistent config location | The unknown mapping was written under the process working directory, while the script-relative config remained unchanged. A second invocation prompted again. |
| Excluded trees not pruned | Preview included files below `.git`, `node_modules`, an ordinary hidden parent, and an existing `Documents` destination category. |
| Batch preview collisions | Two differently located `report.pdf` files were both previewed into the same previously nonexistent `Documents/report.pdf`. |
| Nested destination failure | A rule targeting `Documents/Nested` failed with exit 1 because parent directories were not created. |
| One failure aborts remaining items | A deterministic injected `PermissionError` on the second move aborted the batch, leaving the third source untouched. |
| Prior success lacks durable undo | The first move in that failing batch completed, but no `undo.json` was written. |
| Occupied-source move undo | After a source path was recreated with replacement content, undo replaced that content with the organized file. |
| Changed-copy undo deletion | After editing a copied destination, copy undo deleted it without checking its identity or content. |
| Destination escape | Both a parent-traversal category and an absolute category moved a file outside the selected root, but still inside the disposable test sandbox. |
| Import side effects | Importing the original module with `--help` in `sys.argv` printed CLI help, created a log, and exited before import completion. |
| Missing prerequisites | A missing target, missing script-relative config, and malformed JSON config each returned exit 1. |

The partial-failure test injects a named move failure and sorts discovery order in a disposable subprocess wrapper. It keeps the captured original script unchanged. This models a per-file operation failure without relying on administrator rights, platform-specific permissions, or an unreliable real-world fault.

The original README was also inspected directly with `git show`: its structure and usage sections refer to `organizer.py`, while the actual upstream entry point is `FileOrganizer.py`. Its claim of non-recursive organization also disagrees with the script's `rglob("*")` traversal. These are documented source mismatches, not additional executed test cases.

## Actual disposable CLI transcripts

Below, `<PYTHON>` denotes the isolated Python executable, `<SANDBOX>` replaces the generated temporary directory, and `<TIME>` replaces log timestamps. Path separators are normalized for readability; the application output and exit codes otherwise reflect actual runs. No real user paths are retained.

### Preview collision and logging

Setup: `selected/first/report.pdf` and `selected/second/report.pdf` contained different text, and `Documents` did not exist.

```text
<PYTHON> <SANDBOX>/upstream-tool/FileOrganizer.py <SANDBOX>/selected --dry-mode
exit: 0
stderr:
<TIME> | INFO | Will move <SANDBOX>/selected/first/report.pdf -> <SANDBOX>/selected/Documents/report.pdf
<TIME> | INFO | Will move <SANDBOX>/selected/second/report.pdf -> <SANDBOX>/selected/Documents/report.pdf
```

Both source files remained. No destination directory or undo record appeared. A persistent `upstream-tool/organizer.log` did appear, containing the preview messages.

### Unknown extension and misplaced configuration

Setup: `selected/sample.unknownbaseline` existed; standard upstream mappings were installed beside the copied script. Standard input provided `Others` followed by a newline.

```text
<PYTHON> <SANDBOX>/upstream-tool/FileOrganizer.py <SANDBOX>/selected
exit: 0
stdout:
Which folder would you like to add this extensions(default:Others) :
stderr:
<TIME> | INFO | Unknown Extension Found: .unknownbaseline
<TIME> | INFO | No File Found
<TIME> | INFO | Organization Completed
```

The source remained. `working-directory/extension.json` gained the mapping; `upstream-tool/extension.json` was unchanged. The next process still treated the same extension as unknown.

### Excluded directory descendant

Setup: `selected/.git/nested/payload.txt` existed.

```text
<PYTHON> <SANDBOX>/upstream-tool/FileOrganizer.py <SANDBOX>/selected --dry-mode
exit: 0
stderr:
<TIME> | INFO | Will move <SANDBOX>/selected/.git/nested/payload.txt -> <SANDBOX>/selected/Documents/payload.txt
```

### Nested category failure

Setup: a disposable config mapped `.txt` to `Documents/Nested`; `selected/notes.txt` existed.

```text
<PYTHON> <SANDBOX>/upstream-tool/FileOrganizer.py <SANDBOX>/selected
exit: 1
stderr:
<TIME> | ERROR | [WinError 3] 系统找不到指定的路径。: '<SANDBOX>/selected/Documents/Nested'
```

The source remained, no `Documents` parent was created, and no undo record appeared. The localized Windows error means the specified path could not be found.

### Missing prerequisites

Missing selected folder:

```text
<PYTHON> <SANDBOX>/upstream-tool/FileOrganizer.py <SANDBOX>/selected
exit: 1
stderr:
<TIME> | ERROR | folder is either doesn't exist or is not a directory
<TIME> | ERROR | Organization Stopped
```

Missing script-relative configuration:

```text
<PYTHON> <SANDBOX>/upstream-tool/FileOrganizer.py <SANDBOX>/selected --dry-mode
exit: 1
stderr:
<TIME> | ERROR | [Errno 2] No such file or directory: '<SANDBOX>/upstream-tool/extension.json'
```

Even these failed invocations created a script-relative log during module initialization. The malformed-config scenario was also executed by pytest and returned exit 1 with an error log.

## Safety acceptance boundary

SmartSort's separate regression and feature tests must assert the corrected behavior: import safety, preview purity, pruned traversal, validated destinations, reserved batch names, per-item results, persistent history, and conflict-safe undo. This report describes the initial upstream evidence only and does not itself certify that those replacements are complete.
