"""The ``bibcheck`` command line interface."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import Final, Sequence

from . import __version__
from .cache import DiskCache
from .config import Config, load_config
from .engine import check_bibliography
from .fixer import apply_fixes, plan_fixes, write_fixed
from .models import Report, Status
from .parsing import parse_bibtex_file
from .report import make_console, render_human, render_json

__all__ = ["main", "build_parser"]

EXIT_OK: Final[int] = 0
EXIT_FAILED: Final[int] = 1
EXIT_USAGE: Final[int] = 2

#: Values accepted by ``--fail-on``. ``any`` means "anything that is not OK".
_FAIL_ON: Final[dict[str, tuple[Status, ...]]] = {
    "critical": (Status.CRITICAL,),
    "warn": (Status.CRITICAL, Status.WARN),
    "unresolved": (Status.UNRESOLVED,),
    "error": (Status.NETWORK_ERROR,),
    "any": (Status.CRITICAL, Status.WARN, Status.UNRESOLVED, Status.NETWORK_ERROR),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bibcheck",
        description="Verify the integrity of every reference in a BibTeX file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  bibcheck refs.bib\n"
            "  bibcheck refs.bib --tex paper.tex\n"
            "  bibcheck refs.bib --json report.json\n"
            "  bibcheck refs.bib --fix fixed.bib\n"
            "  bibcheck refs.bib --fail-on critical\n"
            "  bibcheck refs.bib --offline\n"
        ),
    )
    parser.add_argument("bibfile", nargs="?", type=Path, help="the .bib file to check")
    parser.add_argument(
        "--tex",
        type=Path,
        metavar="PATH",
        help="a .tex or .md manuscript, to report uncited entries and undefined keys",
    )
    parser.add_argument(
        "--json",
        type=Path,
        metavar="PATH",
        help="write a machine-readable report ('-' for stdout)",
    )
    parser.add_argument(
        "--fix",
        type=Path,
        metavar="PATH",
        help="write corrected metadata to a NEW file (never edits the input)",
    )
    parser.add_argument(
        "--force", action="store_true", help="allow --fix to overwrite an existing file"
    )
    parser.add_argument(
        "--fail-on",
        metavar="LEVEL",
        help=(
            "exit nonzero when any entry matches: "
            + ", ".join(sorted(_FAIL_ON))
            + " (comma-separated)"
        ),
    )
    parser.add_argument(
        "--offline", action="store_true", help="use only the cache; make no network requests"
    )
    parser.add_argument("--email", metavar="ADDRESS", help="contact email for Crossref's polite pool")
    parser.add_argument("--concurrency", type=int, metavar="N", help="concurrent requests (default 8)")
    parser.add_argument("--cache-dir", type=Path, metavar="PATH", help="where to cache responses")
    parser.add_argument(
        "--cache-ttl", type=int, metavar="DAYS", help="how long cached responses stay fresh (default 30)"
    )
    parser.add_argument("--clear-cache", action="store_true", help="delete the cache and exit")
    parser.add_argument("--no-color", action="store_true", help="never colourise output")
    parser.add_argument("--color", action="store_true", help="colourise even when not a terminal")
    parser.add_argument("--quiet", action="store_true", help="only print entries that need attention")
    parser.add_argument("--version", action="version", version=f"bibcheck {__version__}")
    return parser


def _resolve_color(args: argparse.Namespace) -> bool | None:
    if args.no_color:
        return False
    if args.color:
        return True
    return None  # Let rich decide from the environment.


def _failing_statuses(raw: str | None) -> tuple[Status, ...] | None:
    if not raw:
        return None
    wanted: set[Status] = set()
    for part in raw.split(","):
        name = part.strip().lower().replace("-", "_")
        if name not in _FAIL_ON:
            raise ValueError(
                f"unknown --fail-on level {part.strip()!r}; "
                f"choose from {', '.join(sorted(_FAIL_ON))}"
            )
        wanted.update(_FAIL_ON[name])
    return tuple(wanted)


def _build_config(args: argparse.Namespace) -> Config:
    return load_config(
        contact_email=args.email,
        concurrency=args.concurrency,
        cache_dir=args.cache_dir,
        cache_ttl_days=args.cache_ttl,
        offline=args.offline or None,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # bibtexparser logs syntax errors to stderr itself; we report them as
    # problems instead, so silence the duplicate.
    logging.getLogger("bibtexparser").setLevel(logging.CRITICAL)

    errors = make_console(file=sys.stderr, color=_resolve_color(args))
    config = _build_config(args)

    if args.clear_cache:
        removed = DiskCache(config.cache_dir, ttl_days=config.cache_ttl_days).clear()
        # This is the command's result rather than a diagnostic, so it belongs
        # on stdout. Printed plainly: rich would wrap the path mid-directory.
        print(f"removed {removed} cached responses from {config.cache_dir}")
        return EXIT_OK

    if args.bibfile is None:
        parser.print_help()
        return EXIT_USAGE

    try:
        failing = _failing_statuses(args.fail_on)
    except ValueError as error:
        errors.print(f"[red]error:[/red] {error}")
        return EXIT_USAGE

    if not args.bibfile.is_file():
        errors.print(f"[red]error:[/red] no such file: {args.bibfile}")
        return EXIT_USAGE

    started = time.monotonic()
    parsed = parse_bibtex_file(args.bibfile)
    report = asyncio.run(
        check_bibliography(parsed, config, manuscript_path=args.tex)
    )
    elapsed = time.monotonic() - started

    # When JSON owns stdout, the human report moves to stderr so that
    # `bibcheck refs.bib --json - > report.json` yields valid JSON.
    json_to_stdout = args.json is not None and str(args.json) == "-"
    console = (
        errors if json_to_stdout else make_console(color=_resolve_color(args))
    )
    render_human(report, console, elapsed=elapsed, show_ok=not args.quiet)

    if args.json is not None:
        _write_json(report, args.json, errors)

    if args.fix is not None:
        if _write_fix(report, parsed, args, errors) != EXIT_OK:
            return EXIT_USAGE

    if failing and _triggered(report, failing):
        return EXIT_FAILED
    return EXIT_OK


def _write_json(report: Report, path: Path, errors: object) -> None:
    payload = render_json(report)
    if str(path) == "-":
        sys.stdout.write(payload + "\n")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload + "\n", encoding="utf-8")


def _write_fix(
    report: Report, parsed: object, args: argparse.Namespace, errors: object
) -> int:
    from rich.console import Console

    from .parsing import ParsedBib

    assert isinstance(parsed, ParsedBib)
    assert isinstance(errors, Console)

    planned = plan_fixes(report)
    if not planned:
        errors.print("nothing could be corrected with confidence; no file written")
        return EXIT_OK

    source = args.bibfile.read_text(encoding="utf-8", errors="replace")
    fixed, applied = apply_fixes(source, parsed, planned)

    try:
        write_fixed(args.bibfile, args.fix, fixed, force=args.force)
    except (ValueError, FileExistsError, OSError) as error:
        errors.print(f"[red]error:[/red] {error}")
        return EXIT_USAGE

    errors.print(f"\nwrote {args.fix} with {len(applied)} correction(s):")
    for fix in applied:
        errors.print(f"  {fix.describe()}")
    skipped = len(planned) - len(applied)
    if skipped:
        errors.print(f"  ({skipped} could not be located in the source and were skipped)")
    return EXIT_OK


def _triggered(report: Report, failing: Sequence[Status]) -> bool:
    counts = report.counts
    return any(counts[status] for status in failing)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
