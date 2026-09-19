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
from .export import to_bibtex
from .models import CrossCheck, Report, Status
from .parsing import ParsedBib, parse_bibtex_file
from .pdf import (
    PdfExtractionError,
    crosscheck_pdf,
    extract_text,
    find_reference_section,
    references_to_bib,
)
from .keys import suggest_key
from .lookup import cite
from .report import make_console, render_human, render_json, render_stats
from .stats import summarise
from .style import STYLES
from rich.console import Console

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
            "  bibcheck paper.pdf\n"
            "  bibcheck paper.pdf --export-bib refs.bib\n"
            "  bibcheck refs.bib --tex paper.tex\n"
            "  bibcheck refs.bib --json report.json\n"
            "  bibcheck refs.bib --fix fixed.bib\n"
            "  bibcheck refs.bib --fail-on critical\n"
            "  bibcheck refs.bib --stats --self-author He\n"
            "  bibcheck refs.bib --offline\n"
            "\n"
            "  bibcheck cite 10.1109/CVPR.2016.90\n"
            "  bibcheck cite arXiv:1706.03762\n"
            "  bibcheck cite \"attention is all you need\"\n"
        ),
    )
    parser.add_argument(
        "bibfile",
        nargs="?",
        type=Path,
        metavar="FILE",
        help="the .bib file to check, or a paper .pdf to read references out of",
    )
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
        "--export-bib",
        type=Path,
        metavar="PATH",
        help="write a .bib file built from the authoritative records (use with a PDF)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="allow --fix or --export-bib to overwrite an existing file",
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
        "--style",
        metavar="NAME",
        default="ieee",
        help="citation style to check formatting against: " + ", ".join(sorted(STYLES)),
    )
    parser.add_argument(
        "--no-style",
        action="store_true",
        help="skip the local style and formatting checks",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="also print bibliography health metrics (age, preprints, venues)",
    )
    parser.add_argument(
        "--self-author",
        action="append",
        metavar="SURNAME",
        help="count references by this author as self-citations (repeatable)",
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


def _run_cite(argv: Sequence[str]) -> int:
    """``bibcheck cite <doi|arxiv|title>`` -- fetch one entry and print it."""
    parser = argparse.ArgumentParser(
        prog="bibcheck cite",
        description="Look up a work and print a ready-to-paste BibTeX entry.",
    )
    parser.add_argument("query", nargs="+", help="a DOI, an arXiv id, or a title")
    parser.add_argument("--key", help="use this citation key instead of a generated one")
    parser.add_argument("--email", metavar="ADDRESS", help="contact email for Crossref")
    parser.add_argument("--offline", action="store_true", help="use only the cache")
    parser.add_argument("--cache-dir", type=Path, metavar="PATH")
    parser.add_argument("--no-color", action="store_true")
    args = parser.parse_args(argv)

    errors = make_console(file=sys.stderr, color=False if args.no_color else None)
    config = load_config(
        contact_email=args.email,
        cache_dir=args.cache_dir,
        offline=args.offline or None,
    )

    query = " ".join(args.query)
    result = asyncio.run(cite(query, config, key=args.key))

    if not result.found:
        errors.print(
            f"[red]not found:[/red] {result.detail or 'no match for that query'}"
        )
        return EXIT_FAILED

    record = result.record
    assert record is not None
    if record.match_confidence < 1.0:
        # A title search can land on the wrong paper, and pasting the wrong
        # entry is worse than pasting none.
        errors.print(
            f"matched by title with confidence {record.match_confidence:.2f} — "
            "check this is the work you meant",
        )
    print(result.bibtex)
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    # A subcommand rather than a file. Kept out of argparse subparsers so that
    # the original `bibcheck refs.bib` form stays exactly as it was.
    if arguments and arguments[0] == "cite":
        return _run_cite(arguments[1:])

    parser = build_parser()
    args = parser.parse_args(arguments)

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

    if args.style not in STYLES:
        errors.print(
            f"[red]error:[/red] unknown style {args.style!r}; "
            f"choose from {', '.join(sorted(STYLES))}"
        )
        return EXIT_USAGE

    if not args.bibfile.is_file():
        errors.print(f"[red]error:[/red] no such file: {args.bibfile}")
        return EXIT_USAGE

    started = time.monotonic()
    try:
        parsed, crosscheck = _load_input(args, errors)
    except PdfExtractionError as error:
        errors.print(f"[red]error:[/red] {error}")
        return EXIT_USAGE

    report = asyncio.run(
        check_bibliography(
            parsed,
            config,
            manuscript_path=args.tex,
            crosscheck=crosscheck,
            style=None if args.no_style else args.style,
        )
    )
    elapsed = time.monotonic() - started

    # When JSON owns stdout, the human report moves to stderr so that
    # `bibcheck refs.bib --json - > report.json` yields valid JSON.
    json_to_stdout = args.json is not None and str(args.json) == "-"
    console = (
        errors if json_to_stdout else make_console(color=_resolve_color(args))
    )
    render_human(report, console, elapsed=elapsed, show_ok=not args.quiet)

    if args.stats:
        render_stats(
            summarise(
                [entry.metadata for entry in report.entries if entry.metadata],
                self_authors=args.self_author or (),
            ),
            console,
        )

    if args.json is not None:
        _write_json(report, args.json)

    if args.export_bib is not None:
        if _write_export(report, args, errors) != EXIT_OK:
            return EXIT_USAGE

    if args.fix is not None:
        if _is_pdf(args.bibfile):
            errors.print(
                "[red]error:[/red] --fix edits a .bib file; for a PDF use "
                "--export-bib to write one instead"
            )
            return EXIT_USAGE
        if _write_fix(report, parsed, args, errors) != EXIT_OK:
            return EXIT_USAGE

    if failing and _triggered(report, failing):
        return EXIT_FAILED
    return EXIT_OK


def _is_pdf(path: Path) -> bool:
    """Detect a PDF by magic bytes, not by extension.

    A file named ``refs.bib`` that is actually a PDF should still work, and a
    ``.pdf`` that is really BibTeX should not be fed to the PDF reader.
    """
    if path.suffix.lower() == ".pdf":
        return True
    try:
        with path.open("rb") as handle:
            return handle.read(5) == b"%PDF-"
    except OSError:
        return False


def _load_input(
    args: argparse.Namespace, errors: Console
) -> tuple[ParsedBib, CrossCheck | None]:
    """Read either a .bib file or a paper PDF into checkable entries."""
    if not _is_pdf(args.bibfile):
        return parse_bibtex_file(args.bibfile), None

    text = extract_text(args.bibfile)
    parsed, references = references_to_bib(text, source=str(args.bibfile))
    body, _ = find_reference_section(text)

    if references:
        weak = [reference for reference in references if reference.confidence < 0.5]
        errors.print(
            f"read {len(references)} references from {args.bibfile.name}"
            + (f" ({len(weak)} with no identifier to check against)" if weak else ""),
            soft_wrap=True,
        )

    # --tex wins if given; otherwise the PDF's own body provides the crosscheck.
    if args.tex is not None:
        return parsed, None
    return parsed, crosscheck_pdf(body, [entry.key for entry in parsed.entries])


def _write_export(report: Report, args: argparse.Namespace, errors: Console) -> int:
    """Write a .bib built from what the registry says."""
    target: Path = args.export_bib
    if target.exists() and not args.force:
        errors.print(
            f"[red]error:[/red] {target} already exists (pass --force to overwrite)"
        )
        return EXIT_USAGE
    if target.resolve() == args.bibfile.resolve():
        errors.print("[red]error:[/red] --export-bib must not overwrite the input")
        return EXIT_USAGE

    text = to_bibtex(
        report,
        header=f"Generated by bibcheck {__version__} from {Path(args.bibfile).name}",
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    resolved = sum(1 for entry in report.entries if entry.resolved)
    errors.print(
        f"wrote {target} ({resolved} of {len(report.entries)} entries verified "
        "against Crossref)",
        soft_wrap=True,
    )
    return EXIT_OK


def _write_json(report: Report, path: Path) -> None:
    payload = render_json(report)
    if str(path) == "-":
        sys.stdout.write(payload + "\n")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload + "\n", encoding="utf-8")


def _write_fix(
    report: Report, parsed: ParsedBib, args: argparse.Namespace, errors: Console
) -> int:
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

    errors.print(
        f"\nwrote {args.fix} with {len(applied)} correction(s):", soft_wrap=True
    )
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
