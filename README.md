# bibcheck

Verify the integrity of every reference in a BibTeX file — before a reviewer does.

LLM-assisted writing produces plausible-looking citations that do not exist, have
the wrong year, list the wrong venue, or point at retracted work. Checking 40+
references by hand is the bottleneck. `bibcheck` automates it.

```
bibcheck refs.bib                    # human-readable report
bibcheck refs.bib --tex paper.tex    # also cross-check cited/uncited keys
bibcheck refs.bib --json report.json # machine-readable
bibcheck refs.bib --fix fixed.bib    # corrected metadata, written to a NEW file
bibcheck refs.bib --fail-on critical # nonzero exit for CI
bibcheck refs.bib --offline          # cache only, no network
```

## Status

Under construction, built in order. **Step 1 is complete**: the data model,
BibTeX parsing and the normalization layer, with 163 tests and `mypy --strict`
clean. The Crossref client, the six checks, the reporting layer and `--fix` are
next.

## Checks

| # | Check | Code | Severity |
|---|-------|------|----------|
| 1 | DOI resolves against Crossref | `doi_not_found` | CRITICAL |
| 1 | No DOI — resolved by title+author+year | `not_found_by_title`, `low_confidence_match` | CRITICAL / WARN |
| 2 | Title, author, year, venue, volume, pages drift | `*_mismatch` | WARN |
| 3 | Retracted / withdrawn / expression of concern | `retracted`, `withdrawn`, `expression_of_concern` | CRITICAL |
| 4 | arXiv preprint superseded by a published version | `preprint_superseded` | WARN |
| 5 | Same work cited under two keys | `duplicate_work` | WARN |
| 6 | Bib entry never cited / `\cite` key with no entry | `uncited_entry`, `undefined_citation` | INFO / CRITICAL |
| — | Malformed entry, duplicate key, bad DOI syntax | `malformed_entry`, `duplicate_key`, `malformed_doi` | WARN |

## Design

### The data model

Three shapes, and the relationship between them is the whole design.

**`BibEntry`** is what the file literally says: raw field strings, the original
field order and spelling, the source line. It is never mutated — `--fix` builds
a new file from it, which is what makes "preserve entry keys and field order"
and "never modify the input in place" structural rather than a promise.

**`Metadata`** is the comparable shape, produced *both* from a local `BibEntry`
and from an authoritative `WorkRecord`. Drift checking is therefore a
field-by-field comparison of two values of the same type, not a pile of special
cases. Every field is optional, and `None` means *this source does not say* —
which is different from a disagreement and is never reported as drift.

**`Problem` → `EntryReport` → `Report`** carry the findings. A check emits
`Problem`s with a `Severity`; an entry's `Status` is **derived** from them:

```
network_failed          → NETWORK_ERROR   (nothing else is trustworthy)
any CRITICAL problem    → CRITICAL
not resolved            → UNRESOLVED      (we did not check, ≠ we found nothing)
any WARN problem        → WARN
otherwise               → OK
```

Severity lives in exactly one place, so a new check cannot invent its own notion
of how bad its findings are. `UNRESOLVED` and `NETWORK_ERROR` are outcomes of
*looking*, not degrees of badness: an entry that could not be checked is
reported honestly as unchecked rather than quietly called OK.

### Normalization

The rule: **a value is compared through a key, never directly** — and the two
directions are deliberately different functions.

`latex_to_unicode` is *semantic*. `Caf\'{e}` → `Café`, not `Cafe`. This is what
the report prints and what `--fix` writes back. Accents are resolved by mapping
the LaTeX command to a Unicode **combining mark** and running NFC, so all four
spellings (`\'e`, `\'{e}`, `{\'e}`, `{\'{e}}`) converge without a table of
hundreds of pairs. An unrecognised macro is left visible rather than silently
deleted — it shows up in the report instead of corrupting a title.

`title_key` / `venue_key` are *aggressive*, and used only for equality tests:
ASCII-fold, casefold, `&` → `and`, drop punctuation and protective braces,
collapse hyphens, drop articles. A difference that survives that is a real one.

Never display folded text; never compare unfolded text.

Author names get their own treatment, because they are the noisiest field in any
bibliography. Comparison runs on **surname + first given initial**, ASCII-folded:
`J. Smith` matches `John Smith`, `A. Smith` does not, and `Smith` vs `Schmidt` is
caught. A given name present on only one side is not evidence of a mismatch, and
a truncated local list matches a prefix of the full one (the `et al.` case).

Other fields get the same treatment: `10--20`, `10-20` and `1234--56` (the elided
form of `1234--1256`) all reduce to a comparable `PageRange`; a value that will
not parse yields `None`, meaning *cannot compare*, never a false mismatch.

### Failure handling

Parsing never raises. A syntax error, a duplicate key and a duplicate field are
each reported as a distinct problem, and the last two still yield a usable entry
— bibtexparser hands back the recovered block, so an author does not lose a
reference to a typo. One broken entry costs one entry, not the run.

## Install

```sh
pip install -e ".[dev]"
```

Python 3.11+. No API keys are required for the default path.

## Development

```sh
pytest          # the suite runs with zero network access
mypy            # strict
```

## License

MIT
