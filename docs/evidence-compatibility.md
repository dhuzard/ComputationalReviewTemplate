# Review compatibility report

Every assembled review writes `provenance/review_compatibility_report.json`.
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

The report is provenance, not a trust assessment. It must list the validator
and contract versions used to make each determination.
