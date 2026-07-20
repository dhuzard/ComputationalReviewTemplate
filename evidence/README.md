# Evidence Directory

This directory contains the structured evidence packages that power the
interactive Evidence Explorer widget on the review site.

## Evidence-package contract

The canonical contract is version **1.0.0**. Its machine-readable schemas are
[`schema/package.schema.json`](schema/package.schema.json) and
[`schema/manifest.schema.json`](schema/manifest.schema.json). A canonical
package is named `evidence_section_NN.json` and includes a schema version,
explicit section ID/order/title, findings with source identifiers (and DOIs,
supporting passages, and locators when available), conflicts, figure data,
evidence gaps, replication information, and provenance metadata.

Use `manifest.json` for an explicit package list and ordering. If it is absent,
the explorer discovers these documented filenames and orders them by numeric
section order, never filesystem order:

```
evidence_section_02.json
evidence_section_03.json
...
evidence_section_NN.json
```

The explorer also has a deliberately bounded compatibility adapter for the
existing `section_NN_evidence_package.json`, `section_NN_evidence.json`, and
pre-versioned `evidence_section_NN.json` shapes. New reviews must produce the
canonical v1 format; historical variants are not an open-ended support promise.

An evidence directory can contain any number of sections. The build exposes one
of these statuses rather than silently rendering an indistinguishable empty
viewer: missing directory, invalid manifest, no compatible files, invalid
packages, partial loading, valid zero findings, or loaded.

## Canonical package example

Each newly generated per-section file must contain the following shape (the
JSON Schema is authoritative):

```json
{
  "evidence_package_schema_version": "1.0.0",
  "section": {"id": "mechanisms", "order": 2, "title": "Mechanisms"},
  "findings": [
    {
      "claim": "What the paper found",
      "sources": [{
        "source_id": "Author2026",
        "doi": "10.xxxx/example",
        "supporting_passages": [{"text": "Verbatim supporting passage", "locator": "Results, paragraph 2"}]
      }]
    }
  ],
  "conflicts": [
    {
      "paper_a_doi": "10.xxxx/...",
      "paper_b_doi": "10.xxxx/...",
      "nature_of_conflict": "Description",
      "resolution_status": "unresolved | partially_resolved | resolved"
    }
  ],
  "figure_data": [],
  "evidence_gaps": [],
  "replication": {"unreplicated_claims": []},
  "provenance": {"pipeline_version": "…", "generated_at": "ISO-8601 timestamp"}
}
```

An optional `manifest.json` has `schema_version: 1` and `packages` entries
with a local `file` and explicit numeric `order`. It is recommended where
section numbering is non-contiguous or file ordering should be declared rather
than inferred. Manifest filenames and manifest orders must each be unique, and
every listed file must exist inside the evidence directory. Loaded packages
must likewise have unique `section.order` values.

The JSON Schemas define each document's structural contract. The explorer's
dependency-free runtime validator mirrors those constraints and additionally
enforces cross-file invariants that JSON Schema cannot express: manifest entry
uniqueness, containment/existence of listed files, and unique package section
orders. A package that declares an unknown or future schema version is rejected
explicitly; it is never interpreted as a legacy package.

## How Files Are Generated

The pipeline's Phase 5 (Evidence Curation) builds per-section evidence
packages from the raw cluster evidence. Phase 14 (Assembly) should split
and copy these into this directory.

A combined `evidence_database.json` may also be generated, but the
evidence-explorer plugin does **not** read it directly — it requires
the individual per-section files.
