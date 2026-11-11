
# readwise -> goodlinks import

start time: 2026-07-02
end time: 2026-07-02

## overview

import the readwise export (`readwise/`) into goodlinks via the local REST API.

findings from the data (see conversation):

- readwise export has 51 web articles (plus 34 kindle books + 1 tweet, which
  have no URLs and are out of scope for goodlinks' URL-based model).
- 43 of the 51 already exist in goodlinks (matched by normalized URL); none of
  them are highlighted in goodlinks.
- 8 do not exist in goodlinks.
- **the goodlinks API has no highlight-creation endpoint** (`POST /highlights`
  and `POST /links/{id}/highlights` both 404; docs confirm highlights are
  UI-only). so merging readwise highlights into goodlinks is not possible via
  the API.

decisions (from user):

- skip highlights entirely.
- for the 8 missing articles: add via `POST /links`, tag `readwise`, and
  preserve the original readwise date (earliest "Highlighted at" from the CSV)
  as `addedAt`.

## stage 1: add_link client method
**goal**: `GoodLinksClient.add_link()` wrapping `POST /links`.
**success criteria**: one network call, raises on HTTP error, returns parsed JSON.
**status**: complete (shipped in 839f1f8)

## stage 2: readwise export parser + cmd_readwise_import
**goal**: parse `Readwise.zip` Articles/*.md for url/title, and the CSV for the
earliest highlight date per article; add any article missing from goodlinks.
**success criteria**: `--dry-run` lists exactly the 8 missing articles with the
url, resolved title, resolved date, and tag; `--json` emits machine-readable
output; existing-in-goodlinks articles are skipped.
**status**: complete (shipped in 839f1f8)

## stage 3: wire subparser + run
**goal**: register `readwise-import` subparser; verify dry-run; run for real.
**success criteria**: 8 links created in goodlinks, each tagged `readwise` with
the preserved date; re-running dry-run then reports 0 to add (idempotent).
**status**: complete (shipped in 839f1f8)

## commit log
