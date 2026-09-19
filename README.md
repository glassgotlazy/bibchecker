# bibcheck

Verify the integrity of every reference in a BibTeX file — before a reviewer does.

LLM-assisted writing produces plausible-looking citations that do not exist, have
the wrong year, list the wrong venue, or point at retracted work. Checking 40+
references by hand is the bottleneck. `bibcheck` automates it.

```
bibcheck refs.bib                      # human-readable report
bibcheck paper.pdf                     # read the references out of the PDF itself
bibcheck paper.pdf --export-bib refs.bib   # ...and write a .bib from them
bibcheck refs.bib --tex paper.tex      # also cross-check cited/uncited keys
bibcheck refs.bib --json report.json   # machine-readable
bibcheck refs.bib --fix fixed.bib      # corrected metadata, written to a NEW file
bibcheck refs.bib --fail-on critical   # nonzero exit for CI
bibcheck refs.bib --stats              # bibliography health metrics
bibcheck refs.bib --offline            # cache only, no network

bibcheck cite 10.1109/CVPR.2016.90     # fetch one entry, ready to paste
bibcheck cite "attention is all you need"
```

## Status

Feature-complete against the original brief, plus PDF input, style checks,
bibliography metrics and a lookup command. **574 tests**, `mypy --strict`
clean, and the suite passes with no network access at all.

One caveat worth stating plainly: the recorded Crossref fixtures were written
against Crossref's documented schema rather than captured from live calls, so
the response mapping in `crossref_parse.py` deserves one check against the real
API — particularly which side of a retraction Crossref populates
(`updated-by` on the article versus `update-to` on the notice). Both are
handled; which one appears in practice is unverified.

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
| — | A reference read from a PDF that could not be parsed | `malformed_entry` | WARN |
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

## What a run looks like

```
  bibcheck  refs.bib  ·  4 entries · crossref · 0.4s

  ✓  he2016resnet
  ⚠  drifted2021     year bib 2021  crossref 2019
                     defined in the bibliography but never cited
  ✕  fabricated2021  DOI does not resolve  10.9999/jac.2021.99999
  ✕  wakefield1998   this work has been RETRACTED  10.1016/s0140-6736(97)11096-0
  ! unparseable entry (starting at '@article{broken2020,')

  ✕  1 cited but missing from the .bib: ghost2020

  2 critical   1 warn   1 ok
```

Colour carries meaning and nothing else does, and it turns itself off
automatically when stdout is not a terminal — piping to a file or a CI log
yields clean text with no flag needed.

## Reading a PDF

Often there is no `.bib` to hand — a co-author sent a PDF, or the submission is
the only artefact left. Point bibcheck at the paper itself:

```sh
bibcheck paper.pdf
```

```
read 6 references from paper.pdf (2 with no identifier to check against)

  bibcheck  paper.pdf  ·  6 entries · crossref · 1.8s

  ✓  ref1
  ✓  ref2
  ⚠  ref3   preprint superseded  → 10.18653/v1/2021.acl-1.1
  ✕  ref4   this work has been RETRACTED  10.1016/s0140-6736(97)11096-0
  ✕  ref5   DOI does not resolve  10.9999/jac.2021.99999
  ?  ref6   no DOI, and no confident match found by title

  2 critical   1 warn   1 unresolved   2 ok
```

Keys are the reference's own number, so `ref4` sends you straight to `[4]` on
the page. The PDF is both the bibliography *and* the manuscript, so the
cited/uncited crosscheck runs with no `--tex` needed.

Then turn it into a real bibliography, built from the **authoritative** records
rather than from whatever survived extraction:

```sh
bibcheck paper.pdf --export-bib refs.bib
```

Anything that did not resolve is still written out, marked
`% unverified: this entry could not be resolved, and is as-read` — nothing
silently disappears from a bibliography.

### What to expect from extraction

PDF text is positioned glyphs, not sentences, so column order, line breaks and
hyphenation all have to be undone by guesswork. The module is built around that
rather than pretending otherwise:

- Every reference keeps its **raw extracted text**, so you can see what was
  actually read off the page.
- A reference that cannot be parsed is **reported, never dropped** — a missing
  reference would be a silent lie about how many were checked.
- Each parse carries a confidence, and a reference carrying a DOI is visibly
  stronger than one assembled from guesswork. A DOI or arXiv id makes the rest
  of the parse irrelevant, because the registry supplies the truth.

Numbered styles (`[1]`, `1.`) split most reliably; author-year styles fall back
to blank-line separation. A scanned PDF with no text layer is reported as
needing OCR rather than returning nothing.

## Style and formatting checks

These run locally, need no network, and catch the class of problem a lookup
never will: a reference can be entirely factually correct and still render
badly or read inconsistently.

| Check | Catches |
|---|---|
| Case protection | `title = {BERT: ...}` → IEEEtran typesets "Bert". Acronyms and internal capitals that a style file will flatten, with the braced fix. |
| Style fields | Fields the chosen style requires (`--style ieee`, `acm`) and ones it merely expects. |
| Page sanity | A range running backwards, or an implausible span. |
| Inconsistent venue | `IEEE Trans. Pattern Anal.` in one entry and the spelled-out name in another. |
| Inconsistent author | The same person as `K. He` in one entry and `Kaiming He` in another. |
| URL hygiene | A URL-only citation with no access date. |

**Case protection is the one that earns its keep.** A bare acronym in a title
resolves perfectly against Crossref, drifts from nothing, and passes every
other check — and then the style file lower-cases it, and the author finds out
at proof stage. Nothing else in the pipeline will ever tell them.

```
⚠  devlin2019   BERT will be lower-cased by most style files; wrap each in
                braces to protect it   fix {BERT}: Pre-training of Transformers
```

Note the label: local suggestions are shown under **`fix`**, never under
`crossref`. A value bibcheck proposed and a value a registry stated are
different kinds of claim, and the report never blurs them.

`--fix` applies these, and unlike registry corrections it applies them to *any*
entry — brace protection adds braces around text already there and a backwards
range has only one reading, so neither prejudges a decision the author still
has to make about a CRITICAL entry.

Turn them off with `--no-style`.

## Bibliography metrics

```sh
bibcheck refs.bib --stats --self-author He
```

```
  bibliography

      references  40
      with a DOI  31  (78%)
           years  1998–2024   median 2019
  older than 10y  6  (15%)
       preprints  14  (35%)
  self-citations  9  (23%)

      top venues
                   6  IEEE Transactions on Pattern Analysis and Machine…
                   4  Advances in Neural Information Processing Systems

  · 14 of 40 references are preprints (35%); check whether any have since
    been published
```

Not errors — *shape*. A reference list can be entirely correct and still draw a
reviewer's comment: thirty preprints out of forty, or a median year of 2011 in
a fast-moving field. These are the numbers a reader forms an impression from,
and the author almost never counts them. The notes at the bottom are
deliberately rare: a note that fires on every bibliography would be ignored on
every bibliography.

## `bibcheck cite`

The writing-time companion. Mid-paragraph you have a DOI, an arXiv id, or just
a remembered title, and you want the entry now:

```sh
bibcheck cite 10.1109/CVPR.2016.90 >> refs.bib
bibcheck cite "attention is all you need"
bibcheck cite arXiv:2004.05150
```

```bibtex
@inproceedings{he2016residual,
  title     = {Deep Residual Learning for Image Recognition},
  author    = {He, Kaiming and Zhang, Xiangyu and Ren, Shaoqing and Sun, Jian},
  booktitle = {2016 IEEE Conference on Computer Vision and Pattern Recognition (CVPR)},
  pages     = {770--778},
  year      = {2016},
  doi       = {10.1109/cvpr.2016.90}
}
```

Only BibTeX goes to stdout, so `>> refs.bib` appends something valid. A
title-matched result prints its confidence to *stderr* — pasting the wrong
entry is worse than pasting none. Keys follow the usual `surnameYEARword`
convention, skipping words like "deep" and "learning" that identify nothing;
`--key` overrides.

## Exit codes

Findings alone never fail a build; `--fail-on` is opt-in.

| Code | Meaning |
|---|---|
| `0` | ran successfully, and `--fail-on` was not triggered |
| `1` | `--fail-on` was triggered |
| `2` | usage error — no such file, a bad flag, or `--fix` refusing to clobber |

`--fail-on` accepts `critical`, `warn`, `unresolved`, `error` or `any`, comma
separated. `warn` includes critical; `critical` alone is the sensible default
for CI.

## `--fix`

Corrections go to a **new** file, always:

```sh
bibcheck refs.bib --fix fixed.bib && diff refs.bib fixed.bib
```

```diff
16c16
<   year    = {2021},
---
>   year    = {2019},
```

That one-line diff is the whole design. Rather than re-emitting entries from
the parsed model — which would reformat the file — corrections are applied as
surgical replacements on the original text. Comments, indentation, `=`
alignment, delimiter style, trailing commas and field order all come through
untouched, because nothing deliberately changed is ever rewritten.

What it will correct: `year`, `volume`, `pages`, and adding a missing `doi`
when the title match is near-certain. What it will **not** touch:

- **Title and author.** A flagged mismatch there may mean the entry is the
  wrong work entirely — a judgement call, not a typo.
- **Venue.** Rewriting `IEEE Trans. Pattern Anal.` to the spelled-out name
  would undo a deliberate house style.
- **Anything on a CRITICAL entry.** A retracted paper needs a decision about
  whether to cite it at all; quietly correcting its page numbers would be
  absurd.
- **Anything resolved by a fuzzy title match** rather than by DOI.

It refuses to write over its input, and refuses to clobber an existing file
without `--force`.

## Continuous integration

A ready-to-copy workflow is in [`examples/bibcheck.yml`](examples/bibcheck.yml):

```yaml
- run: pip install git+https://github.com/glassgotlazy/bibchecker
- run: bibcheck paper/refs.bib --tex paper/main.tex --fail-on critical
```

It caches Crossref responses between runs, so a re-run costs no requests, and
it runs weekly on a schedule as well as on push — a bibliography that was clean
in March can pick up a retraction in June without anyone touching the repo.

## The web version

The same checks, behind a small ASGI app in `web.py` — no web framework, so
the CLI never grows a dependency it does not need. The Python package is the
single source of truth, so the site cannot drift from the tool.

```sh
pip install -e ".[dev]" uvicorn
uvicorn app:app --reload        # then open http://127.0.0.1:8000
```

`POST /api/check` takes a JSON envelope, a raw `.bib` body, or a PDF — which is
recognised by its magic bytes, so no multipart handling is needed on either
side:

```sh
curl -X POST localhost:8000/api/check --data-binary @refs.bib
curl -X POST localhost:8000/api/check --data-binary @paper.pdf
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
