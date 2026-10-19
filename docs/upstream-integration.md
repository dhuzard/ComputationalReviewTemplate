# Upstream integration: AllenNeuralDynamics v30 into the fork

This records the merge of the upstream template into
`dhuzard/ComputationalReviewTemplate`. It is a real merge (both histories are
ancestors of the merge commit), not a squash or cherry-pick.

| Ref | Commit |
|---|---|
| Fork `main` (first parent) | `f205552d91c213313c8753ed8a851c7acc4b7f57` — Node 24 Actions conformity (#6) |
| Upstream `main` (second parent) | `8cc838383453d192211413e3ff96dc1ebf3e227a` — AllenNeuralDynamics#16, template self-consistency |
| Merge base | `2ecd112d1c9a33ab37560548e53379a1b24c9466` |

## What upstream brought

- **Pipeline generation 30** with `skills/manifest.json` (sha256 and size per
  skill), unversioned skill filenames (`comprev-orchestrator-v29.md` →
  `comprev-orchestrator.md`), YAML frontmatter on all 21 skills, and the new
  `comprev-writing-style` skill.
- **Claude Science runtime**: no-install skill loading, `host.*` API.
- **Deferred-check semantics**: `pass_with_deferred`, `deferred_checks`,
  `tool_probe`, and the re-test rule (failure modes 18 and 19).
- **Cross-package coherence** checks 11–13 in `comprev-curation-validator`.
- **Plugin data-binding** checks at 14V (`PLUGIN_PAYLOAD_MATCHES_SOURCE`,
  `DATA_PLUGIN_NEGATIVE_TEST`) and the `:sections:` directive option.
- **Prose fidelity** (`scripts/check_prose_fidelity.py`), **template
  consistency** (`scripts/check_template_consistency.py`) and **skill
  manifest** (`scripts/build_skill_manifest.py`) tooling, with the
  `plugins.yml` and `skills.yml` workflows.
- **Authorship**: the released remote AuthorshipExtractor plugin replaces the
  local `plugins/authorship-plugin.mjs`, `content/authorship-widget.mjs` and
  `content/authorship-widget.css`; `content/authors.yml` gains a human author.
- README setup, versioning and concept-DOI citation guidance.

## Conflict resolutions

1. **`plugins/evidence-explorer-plugin.mjs`** — the fork loader is the
   foundation: versioned v1 contract, `manifest.json` with containment checks,
   documented filename patterns, the bounded legacy adapter, diagnostics, and
   explicit `:availability:`. From upstream it takes the `:sections:` option
   (→ `sectionCount`), the `transform(opts, utils)` interface with
   `utils.fileWarn`, and a named `EvidenceLoadError`. Upstream's placeholder
   for an empty directory is **not** adopted: an un-instantiated template is
   declared with `:availability: not_provided` (kept in
   `content/evidence_database.md`); a directory declared available that is
   missing, empty, unmatched or entirely invalid still fails the build. New
   `evidenceLoadWarnings()` emits advisory warnings for a declared-count
   mismatch, interior gaps (filename discovery only — a manifest may be
   non-contiguous), partial loading and zero findings. Findings still come
   only from `findings[]`. All eight upstream package fixtures load through the
   existing legacy adapter; no new adapter was needed.
2. **`evidence/README.md`** — the JSON Schema stays authoritative. Upstream's
   01-based numbering (Introduction included) is recorded as a pipeline
   convention that does not restrict the loader.
3. **`README.md`** — upstream's concept-DOI citation is adopted. The fork's
   compatibility-reporting and golden-fixture sections are kept, and the site
   infrastructure list now names the pinned `mystmd@1.10.1`, the
   committed-figure deployment, all four workflows, the evidence schemas and
   the compatibility helper.
4. **`skills/comprev-deploy-polish.md`** — upstream's equality-based
   `PLUGIN_DATA_BOUND` compares against the loader result
   (manifest-selected or pattern-discovered, adapted packages) instead of a
   raw `evidence_section_*.json` glob. Valid zero findings pass, and
   `not_provided` binds the empty payload. The fork's
   `EVIDENCE_EXPLORER_STATUS_RENDERED` is kept, and
   `REVIEW_COMPATIBILITY_REPORT_VALID` is now defined, because the Phase 21
   orchestrator row requires it.
5. **`skills/comprev-orchestrator.md`** — takes the rename and the upstream
   v30 content. Keeps the fork's Phase 5/5V/14V/19V/20V/21 rows, upstream's
   Phase 6/7 rows (`comprev-writing-style`), and failure modes 18–19.

## Semantic fixes outside the conflict markers

- `comprev-myst-validator`: #26 compares against the loader selection and
  covers `loaded_zero_findings` and `not_provided`; #27 tests the declared
  empty state rather than an inferred one, and also runs the node test file;
  #25 counts distinct `sources[]` (DOI when present, else `source_id`).
  New #28 `REVIEW_COMPATIBILITY_REPORT_VALID` defines the check that the
  orchestrator requires at 14V/19V/20V.
- `comprev-curation-validator`: checks 1, 3 and 11–13 key papers by
  `findings[].sources[]` (DOI when present, else `source_id`), never fail a
  finding for lacking an optional DOI, and never infer attributes the packages
  do not record.
- `comprev-dataml-phases` Phase 20: the broad `git rm -r content/ evidence/
  provenance/ scripts/` is replaced by explicit removal of generated files. An
  enumerated list of scaffold-owned paths is preserved (scripts, schemas,
  tests, workflows, docs, plugins, widgets), and a staged-deletion check runs
  before commit. Phase 14 recommends `:sections: N`.
- `.github/workflows/plugins.yml` and `skills.yml` now use
  `actions/checkout@v7`, `actions/setup-node@v7` and
  `actions/setup-python@v6`, matching the fork's Node 24 action runtimes. The
  test toolchain intentionally stays on `node-version: '20'`.
- `skills/manifest.json` was regenerated after all skill edits; only sha256 and
  byte fields changed. The generation-29 reference-run values that upstream
  kept on purpose (`provenance/pipeline_metrics.json`) are untouched.

## Replacements and deletions

| Removed | Replaced by | Rationale |
|---|---|---|
| `skills/comprev-orchestrator-v29.md` | `skills/comprev-orchestrator.md` | Upstream rename; the version now lives only in the manifest. Fork rows retained. |
| `plugins/authorship-plugin.mjs`, `content/authorship-widget.{mjs,css}` | Remote AuthorshipExtractor release in `myst.yml` | Upstream replacement. The rendered widget was verified (see below). `:authors: ./authors.yml` kept. |

No other fork file was deleted.

## Preserved fork capabilities

Preserved fork capabilities:

- evidence schema v1, the manifest, the adapters and the containment
  boundaries
- all loader statuses and diagnostics, and explicit availability
- the evidence widget
- the review-compatibility schema, template, helper, docs and provenance
  reporting
- claim-graph neutrality
- the MyST golden fixtures and tests, including both empty `.gitkeep`
  fixtures
- the `requirements.txt` and `content/references.bib` customisations
- pinned `mystmd@1.10.1`
- the committed-figure deployment
- the `deploy.yml` and `validate.yml` workflows

## Validation (2026-10-19, merge worktree, uncommitted index)

| Command | Result |
|---|---|
| `python3 scripts/build_skill_manifest.py` / `--check` | pass — 21 skills, pipeline v30 |
| `python3 scripts/check_template_consistency.py` | pass — 12/12 |
| `python3 scripts/check_prose_fidelity.py --self-test` | pass — 16/16 |
| `node scripts/test_evidence_explorer.mjs` | pass — 14/14 |
| `node --test test/*.test.mjs` (pinned `mystmd` 1.10.1 on `PATH`) | pass — 27/27, including the rendered golden fixture build |
| `myst build --html` (`mystmd` 1.10.1, `BASE_URL=/ComputationalReviewTemplate`) | pass — the remote AuthorshipExtractor release was fetched and loaded |
| `node scripts/review-compatibility-report.mjs validate provenance/review_compatibility_report.template.json` | pass |

The rendered output was checked directly:

- The authorship anywidget binds both authors (`Claude`, `Jérôme Lecoq`), and
  its `.mjs`/`.css` assets are present in `_build/html/build/`.
- The Evidence Database anywidget binds `evidence_status: not_provided` with
  four empty arrays, and its widget asset is present.

As a negative control, the new evidence test script fails when run against
either pre-merge plugin: 4 cases fail against the fork plugin and 11 against
the upstream plugin. It therefore exercises the reconciliation and is not a
weakened copy of the upstream assertions.

## Independent review findings and repairs

An independent review of the merge found three defects. All three are repaired
in the working tree on top of the merge; none changes an evidence schema, the
loader's statuses or diagnostics, availability, adapters, the widget or
provenance.

1. **Methods "Pipeline Skills" roles (regression).** `comprev-dataml-phases`
   built each role from `splitlines()[0]`; every skill now begins with YAML
   frontmatter, so every role became `---`. The snippet now calls
   `scripts/skill_catalog.py`, which takes the first sentence of the
   frontmatter `description` (falling back to the first heading outside code
   fences, then the file stem) and escapes `|`.
2. **Gap warning hang (correctness).** `evidenceLoadWarnings` walked every
   integer between the lowest and highest discovered order, so orders at or
   above 2^53 never terminated and huge safe gaps were costly. It now compares
   adjacent sorted orders with BigInt arithmetic, lists at most 20 missing
   orders and reports the exact remainder (`... and N more`). Orders beyond
   `Number.MAX_SAFE_INTEGER` cannot be enumerated exactly; they are excluded
   from gap analysis and named in a separate advisory. Warnings stay advisory,
   and filename and manifest contracts are unchanged.
3. **`host.frames` in Python (test gap).** The orchestrator places `host.frames`
   in the `repl` tool while the Phase 13 and 20a examples called it from
   Python. The orchestrator now documents a **Frame ledger bridge**: the
   coordinator calls `host.frames` in the `repl`, writes the unmodified result
   to `provenance/frames_ledger.json` with the existing write-then-
   `save_artifacts` mechanism, and Python reads it through
   `scripts/frames_ledger.py` (`load_frames`, `summarize`). No new host API is
   assumed. Also resolved: the duplicated `deploy.yml` recipe in
   `comprev-deploy-polish` (old action versions, unpinned `mystmd`) is replaced
   by a pointer to the checked-in workflow, which is authoritative.

Regression coverage: `test/test_python_helpers.py` (run by
`test/python-helpers.test.mjs`) extracts and executes the documented Python
blocks from the skill files (Methods table, Phase 13 snapshot, Phase 20a ledger
head) against fixtures, and fails if any fenced `python` block in `skills/`
calls `host.frames(`. `test/evidence-explorer-plugin.test.mjs` adds a
5-second-timeout case for orders at 2^53, 2^53+2, 2^53-1 and mixed safe and
unsafe orders; the existing `03, 04, ... 09` assertion is unchanged.

Retained risks:

- The bridge is untested against the real Claude Science runtime, which is not
  installed here; tests cover the Python consumer and the file contract only.
  Whether the `repl` can write the file directly or needs a Python or bash cell
  to do it is left to the coordinator, as for gate JSON.
- A stale `frames_ledger.json` cannot be detected from Python; re-serialising
  at each refresh is a coordinator duty.
- `comprev-myst-validator` (outside this repair's scope) still describes check
  #20 as `len(host.frames(...)['frames'])` in prose. It is a semantic
  description, not an example the bridge test can execute.
- Orders beyond 2^53 are reported but never gap-checked.

## Risks

- **Network-dependent build.** `myst.yml` loads
  `.../AuthorshipExtractor/releases/latest/download/authorship-plugin.mjs`. If
  GitHub is unreachable the build fails, and there is no local fallback by
  design. `latest` is not pinned, so a future upstream release can change the
  widget without a commit here.
- The fork's simpler `normalizeConflict` is kept; upstream did not change it
  in this range. Conflict variants with object-shaped sides or `papers_*` keys
  render less information than the base normalizer did.
- `figures/notebooks/fig_methods_pipeline.ipynb` was relabelled for v30
  upstream, but the committed PNG was not regenerated by either side.
  Deployment renders the committed PNG.
- The template ships no real review. The compatibility report template is an
  example; it does not claim that a review was completed.

## Scope

Nothing was pushed, published or opened as a pull request. `main`, other
branches, remotes and existing pull requests were not touched. Raw logs, the
pre-merge stage files and the build output are kept outside the repository in
`/tmp/comprev-sync-run-1a16c84c-evidence/`.
