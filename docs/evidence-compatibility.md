# Review compatibility report

Every assembled review writes `provenance/review_compatibility_report.json`,
which must validate against
`provenance/review_compatibility_report.schema.json`.
The report is descriptive: it tells readers and validators which reusable
capabilities are present without treating optional extensions as requirements.

| Level | Meaning | Minimum evidence |
|---|---|---|
| `article_renderable` | The review is structurally renderable as a MyST article/site. | Successful build and resolved project files. |
| `citation_aware` | Citations resolve against the declared bibliography. | Citation and bibliography validation result. |
| `evidence_package_aware` | A declared Evidence Database has packages the explorer can load through the versioned contract or one listed compatibility adapter. | Loader diagnostics and package schema version(s). |
| `claim_graph_aware` | An extension supplied a claim graph that is discoverable as an external capability. | Extension name, path, and validation status reported verbatim. |

`claim_graph_aware` is optional. The generic template must report its presence
without assigning scores, interpreting graph semantics, or requiring a claim
graph for article, citation, or evidence-package compatibility.

Use `pass`, `fail`, `not_provided`, or `not_applicable` for each level. A
review with `evidence_database.availability: not_provided` can remain article-
and citation-compatible; it is not evidence-package-aware.

An article or citation `pass` or `fail` must name at least one validation result
in `evidence`. An evidence-package `pass` additionally requires
`availability: available`, at least one package schema version, non-empty
`discovered` and `loaded` loader diagnostics, and no rejected packages.
`not_provided` is an explicit empty state: its availability is `not_provided`,
and its versions and all loader diagnostic arrays are empty.

The root `claim_graph_aware` field is optional. When a claim graph passes, each
reported extension includes its name, path, and the extension validator's
verbatim, non-empty `validation_status`. The generic validator checks only that
this descriptive record is present; it does not interpret graph semantics.

The report is provenance, not a trust assessment. It must list the validator
and contract versions used to make each determination.

## Generate and validate

The zero-dependency helper accepts phase determinations without generated
metadata, adds `schema_version` and `generated_at`, validates the result, and
writes the report:

```bash
node scripts/review-compatibility-report.mjs generate phase-determinations.json provenance/review_compatibility_report.json
```

Phases 14 and 19 generate or refresh the report after their build, citation,
and loader checks. Phases 20 and 21 validate the exact artifact they push or
deploy:

```bash
node scripts/review-compatibility-report.mjs validate provenance/review_compatibility_report.json
```

Both commands exit non-zero and print all detected contract errors when the
report is malformed or its status contradicts its evidence.
