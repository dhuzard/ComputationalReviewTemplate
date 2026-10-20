---
name: comprev-triples-validator
description: "Expert Review Pipeline v30 (ComputationalReviewTemplate): Triples Validator — Binary Gate for Phases 15 and 17"
---

# Triples Validator — Binary Gate for Phases 15 and 17

**Purpose:** Validate citation occurrence extraction and fix preparation.
**Agent:** DATAML (deterministic counting + string matching)

## Phase 15 Checks: Citation Triples

The Phase 15 checks are mechanical and run by `scripts/citation_occurrences.py`; do not re-implement
them with ad-hoc greps. The validator **rescans the sources itself** and never trusts the submitted
inventory.

```bash
python scripts/citation_occurrences.py validate-occurrences   # live, fresh rescan; JSON diagnostics, exit 1 on any failure
python scripts/citation_occurrences.py coverage               # re-verifies snapshot + live, then writes provenance/citation_coverage.json
```

1. **EXACT_OCCURRENCE_COVERAGE**: the extraction equals an independent rescan of the live sources
   and of `provenance/citation_snapshot/`: same revision, same sources, same occurrence ids with
   identical `relative_file`, `byte_span`, `line_range`, `directive_kind`, `key_index`,
   `citation_key`; no missing, unknown, duplicated or altered record (duplicates are rejected
   before any set is built, so a duplicate cannot hide an omission). **pass/fail**
2. **SNAPSHOT_INTEGRITY**: every snapshot file matches its manifest hash, nothing is extra or
   missing, and an independent rescan of the snapshot reproduces the manifest (no omitted source). **pass/fail**
3. **KEY_IN_BIB**: every cited key resolves to exactly one `.bib` entry (duplicates and
   unsupported entries are failures). **pass/fail**
4. **SUPPORTED_SYNTAX**: the scanner met no unsupported citation syntax (unsupported roles,
   `[@key]`/`@key`, empty keys, unterminated roles/fences/comments, citations in front matter or in an
   include). **pass/fail**
5. **BATCH_SIZE**: Phase 16 batches have at most 18 occurrences. **pass/fail**

The validator `report` passed to `advance_phase('15V', ...)` MUST carry `phase: 15`, `gate: "pass"`
and the facts printed by the `coverage` command: `citation_coverage_sha256`, `manuscript_revision`,
`occurrence_count`. A report without them is refused even if every check above reads "pass".
This gate proves occurrence coverage only; it says nothing about whether any citation is correct.

## Phase 17 Checks: Fix Preparation

6. **FULL_COVERAGE**: Every non-VERIFIED triple has a fix request? **pass/fail**
7. **CONTEXT_EXISTS**: Every fix request's context is substring of target file? **pass/fail**
8. **CORRECT_METADATA**: METADATA_ERROR fixes match CrossRef? **pass/fail**

## Output Schema
```json
{"phase": 15|17, "gate": "pass|fail", "checks": {...}, "failures": [...]}
```
