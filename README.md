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

Under construction, built in order. **Steps 1-3 are complete**: the data model,
BibTeX parsing and normalization; the Crossref client with its disk cache and
rate limiting; and all six checks with the engine that runs them. 367 tests,
`mypy --strict` clean.

There is also a **web version** — the same checks behind a small ASGI app, so
a `.bib` can be dropped in a browser without installing anything. The terminal
report, `--fix` and the CLI itself are next.

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

### Network, cache and rate limiting

The Crossref client is built around three properties, in this order of
importance:

**Nothing crashes the run.** Every failure — a timeout, a DNS error, a 500, a
body that is not JSON, a JSON body of the wrong shape — becomes a
`NETWORK_ERROR` outcome for *one entry*. Thirty-nine good references are never
lost to one bad one.

**Everything is cached, including 404s.** A fabricated DOI is the most
interesting result the tool produces, and not caching it would mean re-fetching
it on every run. Negative caching is what makes `--offline` and fast re-runs
work at all. A 5xx, by contrast, is *never* cached — a transient failure must
not poison the cache for 30 days. The cache is a plain tree of JSON files:
readable, greppable, sharded so one bibliography does not make one huge
directory, written atomically so an interrupt cannot truncate a record, and
safe to delete at any time.

**The server is treated as a guest treats a host.** Concurrency is capped by a
semaphore (default 8), `Retry-After` is obeyed, and backoff is exponential
*with jitter* — without jitter, eight workers that all get a 429 retry in
lockstep and trip the limit again together. A contact email moves requests into
Crossref's polite pool; it is a courtesy, not a key, and everything works
without one.

Three outcomes are kept strictly apart, because conflating them is how a tool
like this lies to its user:

| Outcome | Meaning |
|---|---|
| `NOT_FOUND` | We asked, and the registry said no. This is what a fabricated DOI looks like. |
| `NETWORK_ERROR` | We asked and never got an answer. |
| `OFFLINE_MISS` | We never asked, because `--offline` was set and the cache had nothing. |

Only the first is a finding about the *citation*. The other two are findings
about the *run*, and are reported as such.

### The checks

Every rule in `checks.py` is a **pure function**: local metadata and an
authoritative record in, a tuple of `Problem` out. No HTTP, no state, no
ordering dependency between rules. Each can be exercised directly with
hand-built inputs, and a new rule cannot break an existing one. `engine.py`
does the orchestration and owns the one hard guarantee: an exception anywhere
in one entry costs *that entry* and nothing else — `asyncio.gather` runs with
`return_exceptions=True`, so even a bug in bibcheck itself degrades to a
`NETWORK_ERROR` on a single reference rather than a traceback.

Three judgment calls worth stating, because they are the difference between a
tool people use and one they mute:

- **A DOI that does not resolve is CRITICAL; an entry with no identifier that
  cannot be matched is only a WARN.** Plenty of real work is absent from
  Crossref. Treating "not found" as "fabricated" would cry wolf on every thesis
  and technical report.
- **Venue drift caps at WARN and drops to INFO when either side looks
  abbreviated.** `IEEE Trans. Pattern Anal. Mach. Intell.` versus the
  spelled-out name is house style, not an error.
- **A shared title alone is not a duplicate.** A conference paper and its
  extended journal version legitimately share a title; the year is part of the
  identity signature.

### Failure handling

Parsing never raises. A syntax error, a duplicate key and a duplicate field are
each reported as a distinct problem, and the last two still yield a usable entry
— bibtexparser hands back the recovered block, so an author does not lose a
reference to a typo. One broken entry costs one entry, not the run.

## The web version

The same checks, behind a small ASGI app in `web.py` — no web framework, so
the CLI never grows a dependency it does not need. The Python package is the
single source of truth, so the site cannot drift from the tool.

```sh
pip install -e ".[dev]" uvicorn
uvicorn app:app --reload        # then open http://127.0.0.1:8000
```

`POST /api/check` takes either a JSON envelope or a raw `.bib` body:

```sh
curl -X POST localhost:8000/api/check --data-binary @refs.bib
curl -X POST localhost:8000/api/check -H 'content-type: application/json' \
     -d '{"bib": "@article{...}", "tex": "\\cite{key}"}'
```

Serverless has no persistent disk and a hard execution deadline, so the cache
lives under `/tmp` and the work is bounded: body size, entry count and wall
clock are all capped up front, and exceeding a cap returns an explanation
rather than a platform timeout page.

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

The "zero network access" claim is enforced, not asserted: an autouse fixture
in `tests/conftest.py` monkeypatches `socket.connect` and `getaddrinfo` to
raise, so any test that reaches for a real socket fails loudly. Crossref
traffic is served from recorded JSON in `tests/data/crossref/` through respx.

## License

MIT
