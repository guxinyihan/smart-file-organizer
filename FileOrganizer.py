"""Compatibility entry point for the MIT upstream file organizer.

Prefer 'python -m smartsort'. Importing this module never parses arguments or
touches files. Legacy flags are translated into the shared CLI.
"""
import argparse


def main(argv=None):
    import sys
    from smartsort.cli.main import main as cli_main

    args = list(sys.argv[1:] if argv is None else argv)
    commands = {"preview", "organize", "duplicates", "history", "undo", "rules", "stats", "watch", "gui", "recover"}
    if args and args[0] in commands:
        return cli_main(args)
    parser = argparse.ArgumentParser(description="Legacy adapter; prefer python -m smartsort")
    parser.add_argument("folder", nargs="?", default=".")
    parser.add_argument("--dry-mode", action="store_true")
    parser.add_argument("--copy", action="store_true")
    parser.add_argument("--include-hidden", action="store_true")
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parsed = parser.parse_args(args)
    translated = ["preview" if parsed.dry_mode else "organize", parsed.folder]
    for enabled, flag in ((parsed.copy, "--copy"), (parsed.include_hidden, "--include-hidden"),
                          (parsed.recursive, "--recursive")):
        if enabled:
            translated.append(flag)
    if parsed.yes and not parsed.dry_mode:
        translated.append("--yes")
    return cli_main(translated)


if __name__ == "__main__":
    raise SystemExit(main())
            

