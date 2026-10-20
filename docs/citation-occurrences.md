# Citation occurrences

`scripts/citation_occurrences.py` is the deterministic half of Phases 15, 15V and 16: it finds every
`{cite:p}` / `{cite:t}` key occurrence in the manuscript sources, checks that an inventory lists
exactly those, and checks that Phase 16 left exactly one well-formed verdict per occurrence. Standard
library only (Python >= 3.9), no network, no LLM, no `assert` (so `python -O` changes nothing).
`scripts/phase_ledger.py` calls it at admission and on every re-verification
([phase-ledger.md](phase-ledger.md)).

## What it proves, and what it never proves

It proves mechanical facts only: which occurrences exist (found by a fresh rescan, never read from a
submitted file), that an inventory/coverage file/verdict file matches that rescan exactly, that each
VERIFIED verdict points at local evidence text whose hash and passage still match, and that nothing
was edited since the revision was pinned.

It **never** proves that a claim is true or that a cited paper supports it. That is the independent
LITREVIEW protocol of `skills/comprev-verification.md` (Phase 16). A VERIFIED verdict's
`scientific_checks_attestation` is *recorded*, not trusted as science; evidence *availability* (a
mechanical fact) is kept separate from *support* (a judgment).

## v1 scope

**Sources (fixed, no configuration can exclude one):** `content/**/*.md`, bibliographies
`content/**/*.bib` and root `*.bib`. The one local `{include}` that `content/Methods.md` uses
(`provenance/review_request.md` with `:start-after:` / `:end-before:`) is supported; the included
file joins the revision, and a citation inside an included region is refused (`citation_in_include`)
rather than lost. Generated LaTeX mirrors are not scanned. A missing `content/` directory, a symlink
(file, directory or include), invalid UTF-8, a byte-order mark, an unreadable file **or an
unreadable directory** (a subtree that cannot be listed is `unreadable_source`, never silently
omitted; the same holds for the snapshot walk, `unreadable_snapshot`) or a path that leaves the
repository fails.

**Extracted:** `{cite:p}` and `{cite:t}` in active prose, including headings, lists, blockquotes,
tables, captions, link labels, footnotes, prose directive bodies (`note`, `warning`, `figure`,
`table`, `card`, `tab-item`, `dropdown`, `prf:*`, ... , colon or backtick fenced, nested) and prose
directive titles (`admonition`, ...). One record per key: `{cite:p}`A, B`` is two records,
`key_index` 0 and 1; repeated keys, also inside one role, are repeated records.

**Not extracted:** normal backtick/tilde fences and code directives (`code-cell`, `code-block`,
`math`, `raw`, `mermaid`, ...), inline code (any backtick run length), indented code, HTML comments
(multi-line, with blank lines), URL destinations and autolinks, link reference definitions, YAML
front matter, `\{cite:p}` escapes. When it is ambiguous whether text is code, it is scanned as prose
(a 4-space-indented paragraph inside a list item is prose), so the failure mode is an extra occurrence
that needs a verdict, never a silently lost one.

**Blocks and containers.** Inline code and roles are matched inside one block only: a heading, a
list item (each item, including siblings), a block quote, a thematic break or a setext underline ends
the paragraph, so a stray backtick cannot reach across it, while soft line breaks inside one
paragraph still pair code spans as CommonMark does (`` `a\n{cite:p}`A` `` is one code span and is
correctly not a citation). Block quotes and list items are tracked as a container stack with
CommonMark's indentation rules (tabs stop at multiples of four; a list marker followed by five or more
columns, e.g. ``-     {cite:p}`A` ``, starts indented code, one to four columns is prose). A fence belongs
to the container that opened it: it closes only on a same-container closing fence (indented fewer than
four columns past the container) or when that quote/list item ends, and its payload is never parsed
for quote or list markers. A lazy continuation line keeps a paragraph open. An HTML comment that is
still open when its quote or list item ends is refused (`unterminated_comment`) instead of guessed.

**Refused (fail closed, JSON diagnostic with file and line):** other cite roles (`{cite:ps}`,
`{cite:author}`, ...), `{cite:p}` without a backtick key list, `[@key]` and bare `@key`, empty or
malformed keys, an unterminated role, fence, comment or front matter, citation syntax in front
matter, a `%` comment line, link destination or reference definition that contains citation
syntax, an unknown directive whose
body contains citation syntax, an unsupported `{include}` form, a key with no `.bib` entry, and
duplicate or unparseable `.bib` entries (the `.bib` parser is a bounded brace/quote-aware reader for
`@type{key, ...}` entries; `@string`, `@preamble` and `@comment` are skipped).

## Identity

`manuscript_revision` is the sha256 of the scanner version plus, for every source and include in
sorted path order, its path, length and raw bytes (bibliographies included). Any byte change anywhere
makes a new revision. `occurrence_id` is the sha256 of the canonical JSON of
`{schema_version, manuscript_revision, relative_file, byte_span, key_index}`; `byte_span` is the
zero-based half-open UTF-8 byte range of the whole role (CRLF, bare CR and non-ASCII text
included), `line_range` the one-based inclusive lines it touches.

## Scanner version and regeneration

The scanner version is `comprev.citation_scanner/3` and is part of the revision hash. Version 2 fixed
list-item continuation inside block quotes: the indentation a list item needs is stored and compared
*relative to its parent container's content column* (tabs still stop at physical multiples of four),
so a quote's leading spaces or `>` marker can no longer be mistaken for list indentation.

Version 3 closes a false-negative family. A non-blank line that is indented four or more columns past
the content column of the containers it *does* match, while failing to match all of its open containers
(quotes / list items), is where CommonMark renderers and this scanner could silently disagree about
whether a fence, a paragraph or inline code continues. Version 2 could scan such input as zero
citations while a renderer showed a paragraph containing a citation (for example
`> - ` + a code fence, then an over-indented `> {cite:p}` line). The scanner now refuses it before any
fence or paragraph state is closed, whether or not the line cites anything, with the diagnostic
`unsupported_container_layout` (file and one-based line). This is intentionally fail-closed: the
unsupported indented unmatched continuation is rejected, not interpreted. Repair the source (re-indent
the line to match its containers, or add a blank line) and rescan. Same-container code payload stays
opaque as before. This is an explicit refusal of a family of layouts, not an implementation of the whole
of CommonMark, and it says nothing about scientific correctness.

Because the revision hash changes, every artifact produced by version 1 or 2 (snapshot, extraction,
coverage, verdicts) is deliberately reported stale (`scanner_version_mismatch` / `stale_snapshot`) and is
never upgraded in place. To regenerate: reopen Phase 15 (or restart) so a new snapshot and extraction are
written, then redo 15V and 16 against the new revision; version-2 artifacts require this explicit
reopen and reverification. Data schema versions are unchanged (same shape).

Evidence for the refusal rule is bounded, not a proof: during development the scanner was compared with
the `markdown_it` CommonMark parser (a developer-only, optional test oracle; no runtime dependency) over
a deterministic container/prefix matrix and seeded random inputs, with no case where the scanner
succeeded and disagreed on the citation count; many inputs were refused. The test suite also pins a fixed
set of regression layouts that run without any markdown parser.

## Files

| File | Written by | Schema |
|---|---|---|
| `provenance/citation_snapshot/` (`manifest.json`, `files/...`) | `snapshot` (Phase 15) | `comprev.citation_snapshot/1` |
| `provenance/citation_triples.json` | `extract` (Phase 15) | [`provenance/citation_occurrences.schema.json`](../provenance/citation_occurrences.schema.json) |
| `provenance/citation_coverage.json` | `coverage` (Phase 15V) | `comprev.citation_coverage/1` (deterministic; recomputed, never trusted) |
| `provenance/citation_verification.json` | the coordinator, from Phase 16 batch results | [`provenance/citation_verdicts.schema.json`](../provenance/citation_verdicts.schema.json) |
| `provenance/citation_evidence/*` | Phase 16 agents | saved retrieved text, referenced by sha256 |

The extraction envelope is `{schema_version, scanner_version, manuscript_revision, sources[],
occurrence_count, extraction_digest, occurrences[]}`; each occurrence has exactly the nine core fields
`schema_version, manuscript_revision, occurrence_id, relative_file, byte_span, line_range,
directive_kind, key_index, citation_key`, plus optional `context` (verbatim lines) and `bib_entry`
(exact entry text and location), both optional and compared against the rescan only when present
(every required field is always compared exactly; an omitted `context`/`bib_entry` is valid), and an optional
advisory `claimed_finding` (never a mechanical fact, never compared). The schema files describe
structure and are kept in step with the runtime by tests; equality with the manuscript is checked at
runtime by rescanning. Zero citations is valid but must be an explicit, complete inventory
(`occurrences: []`); a missing file is a failure.

The verification envelope is `{schema_version, manuscript_revision, extraction_digest, verdicts[]}`.
Each verdict keeps the Phase 16 fields (`cite_key`, `category`, `failed_at_step`,
`verification_depth`, `supporting_passage`, `details`, `correct_metadata`, `suggested_action`) and adds
`occurrence_id`, `manuscript_revision`, `evidence` and `scientific_checks_attestation`.

* `VERIFIED` needs `failed_at_step: null`, `verification_depth` `fulltext` or `abstract_only`,
  `evidence.status: "available"` with the same depth, a `path` under `provenance/citation_evidence/`,
  the file's `sha256`, and `supporting_passage` equal to the file bytes at `passage_span`
  (UTF-8 byte offsets); abstract fallback needs `fulltext_unavailable_reason`, which must be a non-empty string whenever it is
  present, at any depth (the schema enforces both); the attestation must be
  `{protocol: "comprev-verification/phase-16", steps_completed: [1,2,3,4,5], attested_by}`.
* `unavailable` / `unverifiable` evidence, an unknown status, `metadata_only` and a missing,
  tampered, mismatched or out-of-range evidence file can never be VERIFIED. A non-VERIFIED verdict may
  omit evidence if `details` explains the manual review needed.
* A verdict whose `occurrence_id` is not a 64-hex string (`[]`, `{}`, `true`, `null`, a number, any
  other string) is a `malformed_verdict` diagnostic, never a crash; any unexpected internal error is
  reported as an `internal_error` JSON diagnostic with exit 1.
* Unknown fields, wrong JSON types (a `true` where an integer is required), duplicate JSON keys,
  `NaN`, a byte-order mark and an unsupported `schema_version` are rejected.

## Commands

All print one `comprev.citation_diagnostics/1` JSON object (`ok`, `error_count`,
`errors[{code, message, file?, line?, occurrence_id?}]`) and exit 1 on failure (2 on usage errors).

| Command | Does |
|---|---|
| `extract [--out FILE\|-]` | scan and write `provenance/citation_triples.json` |
| `snapshot [--supersede]` | copy every scanned source into the immutable snapshot; `--supersede` moves an old one aside (after `reopen_phase('15', ...)`) |
| `coverage` | verify snapshot + extraction + live equality, write `citation_coverage.json`, print its sha256 |
| `validate-occurrences [--file F]` | **live**: rescan, compare the inventory exactly |
| `validate-verdicts [--file F]` | **live**: rescan, check the extraction, then every verdict and its evidence |
| `audit-historical [--snapshot DIR]` | explicit audit of a pinned snapshot's extraction, coverage and verdicts; compares nothing with the current manuscript |

There is no flag that skips the rescan, trusts an inventory or relaxes a check: the public validators
are always live. Python API: `scan_sources`, `check_inventory`, `check_verdicts`, `validate_occurrences`,
`validate_verdicts`, `create_snapshot`, `verify_snapshot`, `write_coverage`, `audit_historical`,
`assemble_verification`.

## Failure codes (selection)

Structure: `invalid_json`, `not_an_object`, `missing_file`, `malformed_envelope`, `malformed_record`,
`malformed_verdict`, `malformed_evidence`, `unsupported_schema_version`, `count_mismatch`.
Staleness: `stale_revision`, `stale_snapshot`, `source_added`, `source_removed`, `source_modified`,
`extraction_digest_mismatch`, `digest_mismatch`. Identity: `duplicate_occurrence_id`,
`duplicate_occurrence`, `invalid_occurrence_id`, `missing_occurrence`, `unknown_occurrence`,
`occurrence_mismatch`. Verdicts: `missing_verdict`, `unknown_verdict_id`, `duplicate_verdict`,
`unknown_category`, `verdict_key_mismatch`, `missing_explanation`, `depth_without_evidence`,
`metadata_only_not_valid`, `missing_attestation`, `invalid_attestation`. Evidence:
`evidence_not_available`, `unknown_evidence_status`, `evidence_missing`, `evidence_hash_mismatch`,
`evidence_key_mismatch`, `evidence_path_invalid`, `passage_mismatch`, `passage_out_of_range`,
`passage_not_utf8`, `depth_mismatch`, `fulltext_fallback_unexplained`. Snapshot/coverage:
`snapshot_exists`, `snapshot_missing`, `snapshot_malformed`, `snapshot_file_modified`,
`snapshot_file_unlisted`, `snapshot_file_missing`, `snapshot_rescan_mismatch`, `coverage_missing`,
`coverage_mismatch`, `report_not_bound`. Sources: `missing_source_root`, `symlink_refused`,
`invalid_utf8`, `bom_not_allowed`, `unreadable_source`, `source_too_large`, `path_escape`,
`unreadable_snapshot`, `internal_error`, `bib_parse_error`, `invalid_bib_key`, `duplicate_bib_key`, `unresolved_bib_key`, and the scanner
refusals `unsupported_cite_role`, `malformed_cite_role`, `unterminated_role`, `empty_key`,
`invalid_key`, `unterminated_fence`, `unterminated_comment`, `unterminated_frontmatter`,
`citation_in_frontmatter`, `citation_in_include`, `citation_in_comment_line`,
`citation_in_reference_definition`, `ambiguous_link_destination`, `unsupported_citation_syntax`,
`unsupported_directive_citation`, `unsupported_include`, `include_marker_not_found`,
`unsupported_container_layout`.

## Limitations

* Container layouts renderers disagree on are refused rather than guessed: an indented (4+ columns)
  `>` line under a list item whose continuation does not match raises `unsupported_container_layout`.
  A development-only comparison against `markdown_it` over quote/list/indent/tab combinations found no
  silent disagreement; it is optional in CI (skipped if `markdown_it` is absent).
* Verdict `evidence` and `scientific_checks_attestation` may be `null` or absent on a non-VERIFIED
  verdict (nothing retrieved, nothing attested); the schema and the runtime agree. A VERIFIED verdict
  must carry non-null available, correctly hashed evidence and the five-step attestation.

* The scanner is a conservative line/container/inline state machine, not a full CommonMark/MyST parser. A
  construct it cannot classify is either refused or scanned as prose; it never guesses "code" to make
  an occurrence disappear.
* HTML blocks, math, and `csv-table`-style directives are not interpreted: a role inside them is
  scanned as prose (an extra record) or, for unknown directives, refused.
* No generalised receipt store, patch model or Methods-text accounting: that is Phase 16-20 follow-up
  design and is deliberately not implemented here.
