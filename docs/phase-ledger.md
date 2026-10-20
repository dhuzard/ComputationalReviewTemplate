# Phase ledger

`scripts/phase_ledger.py` is the pipeline's record of which phases are finished and the only
thing that decides whether the next one may start. It is deterministic Python (standard library,
3.9+): no network, no LLM, no `assert`, so `python -O` changes nothing. The orchestrator skill
(`skills/comprev-orchestrator.md`, "Phase Ledger") is the coordinator-facing summary; this page
is the reference.

## What it does and does not prove

It proves that, for each completed phase, a receipt was registered in order; that the named gate
file existed, parsed as one JSON object, said what its producer documents as success, and has not
changed since; and that validators and critics ran in different agent frames than the actors they
check.

It does **not** prove scientific correctness, and it cannot tell a truthful validator from a
forged gate file. The validators stay independent agents; the ledger only refuses to proceed
without their recorded, intact verdict.

## Phase ids

Strings, in this order (the orchestrator's Phase Index; `check_template_consistency.py` keeps the
two in step):

`1 1V 2 2V 3 3V 4 5 5V 6 7 7V 8 9 9V 10 11 12 13 14 14V 15 15V 16 17 17V 18 19 19V 20a 20 20V 21`

Anything else (`"5v"`, `22`, `"3_curation"`) raises `InvalidPhase`. Integers 1-21 are accepted
for the main phases. A phase can only be advanced when the previous id in this list is complete,
so *action done* (`N`) never unlocks the next action: `N+1` waits for `NV`.

## Usage

```python
import sys
sys.path.insert(0, 'scripts')
from phase_ledger import (init_ledger, load_ledger, restart, advance_phase, require_phase,
                          check_gate, reopen_phase, completed_phases)

init_ledger()                                   # once per run, explicit
restart()                                       # each new session: load + re-verify everything

advance_phase('2', actor_vid, actor_frame_ids)  # action receipt
advance_phase('2V', gate_vid, validator_frame_ids)   # reads gate_evidence_compliance.json
advance_phase('15V', report_vid, validator_frame_ids, report=validator_result)

require_phase('2V')                             # -> artifact id, or raises
audit_vid = check_gate('gate_figure_audit.json')   # -> pinned artifact id, or raises
```

Command line: `python scripts/phase_ledger.py init|status|verify [--root DIR]` (exit 1 on any
ledger error).

Every call either succeeds or raises a `LedgerError` subclass and leaves the file untouched:
`InvalidPhase`, `LedgerMissing`, `LedgerExists`, `LedgerCorrupt`, `LedgerLocked`, `StaleLedger`,
`PhaseBlocked`, `GateError`.

### Receipts

`advance_phase(phase, artifact_id, child_ids, *, report=None, allow_deferred=False, root=".",
expected_revision=None)` registers:

| Field | Rule |
|---|---|
| `artifact_id` | Concrete saved-artifact version id (`[A-Za-z0-9._:/@+=-]`, <=200 chars); template placeholders like `<saved_version_id>` are rejected. For gate-bearing phases it is the gate artifact's id; `check_gate` returns it. Phase `20`: the pushed commit sha. |
| `child_ids` | Non-empty list of unique agent frame ids. Must be disjoint from the paired actor's (`NV` vs `N`; `8` vs `7`; `12` vs `11`). |
| gate file | Where the phase has one: found in `provenance/` then the working directory (symlinks refused), size-capped, strict JSON (duplicate keys and `NaN` rejected), sha256 recorded. |
| `report` | Only for `1V`, `15V`, `17V`, `19V`, whose verdict is not in a named file. Its canonical digest is recorded. |
| outputs | `15` needs `citation_triples.json`, `17` needs `fix_requests.json`; non-empty, digest recorded. |

Phases `16`, `18`, `19` and the other actor phases have no deterministic output file in the
pipeline, so their receipt is `artifact_id` + `child_ids` only. That is a record that the action
was reported done, not evidence about its content.

### Gate contracts

Validator-authored files (`2V 3V 5V 7V 9V 14V 20V`, and the `report` of `1V 15V 17V 19V`) must
carry the matching integer `phase`, and at least one of `gate == "pass"`, `gate_passed is true`,
`verdict == "PASS"`; every verdict field present must agree. A pass is refused if any `checks` /
`structural_results` entry is `"fail"` (or a `{fail: n>0}` count) or `failures` is non-empty.

* `pass_with_deferred`: only with `allow_deferred=True`, non-empty `deferred_checks` and
  `tool_probe`, and `gate_passed` not true. The acceptance is stored and honoured on re-verification.
* `FAIL_REQUIRES_USER_SIGNOFF` / `user_signoff_required`: only with
  `user_signoff: {decision: "proceed", note: "..."}`.
* Phase 21 uses its documented producer format instead (see the table): `gate` is the constant
  `"phase_21_deploy_polish"` and `verdict` is the outcome.

Per-gate checks, taken from what each skill documents its producer writes:

| Gate | Phase | Required |
|---|---|---|
| `gate_scope.json` | 1V | non-empty `title`, `clusters`, `sections`, `evidence_parameters`; `review_request_path` (verdict from `report=`) |
| `gate_evidence_compliance.json`, `gate_evidence_curated.json`, `gate_bibliography.json` | 2V, 5V, 9V | validator verdict only |
| `gate_citation_infrastructure.json` | 3V | validator verdict; non-null `citation_key_map_vid`, `author_name_table_vid` |
| `gate_scaffold_approved.json` | 4 | `scaffold_artifact_id`; four booleans `true` |
| `gate_figure_audit.json` | 6 | `redesign_remaining == 0`, `verdict_counts_post_resolution`, `critic_frame_ids` |
| `gate_sections_drafted.json`, `gate_assembly.json` | 7V, 14V | validator verdict; non-empty `structural_results` and `section_md_vids_final` |
| `gate_critic_complete.json` | 8 | `total_must_fix == 0`, `coverage_review` |
| `gate_integration.json` | 10 | the six `6a`..`6f` passes once each; `filename_mismatches`, `structural_issues` lists |
| `gate_intro_conclusion.json` | 11 | four artifact ids; `novel_citations == []`; `master_citation_count > 0` |
| `gate_bookend_critic.json` | 12 | `total_must_fix == 0` |
| `gate_methods.json` | 13 | `methods_artifact_id`; `subsection_count == 8`; `reproducibility_statement_present` |
| `gate_phase_20a_methods_refresh.json` | 20a | `forbidden_hits == 0`; `figure_regenerated`; `frame_count > 0` |
| `gate_repository_push.json` | 20V | validator verdict; non-empty `structural_results` |
| `gate_phase21_deploy_polish.json` | 21 | `gate == "phase_21_deploy_polish"`; non-empty `checks` of `{tier A\|B, verdict PASS\|BLOCKED}`; empty `fix_list`; `verdict == "PASS"` (no blocked check, `deploy_accessible` not false) or `PASS_WITH_BLOCKED_TIER_B` (manual-checklist fallback: `deploy_accessible == false`, only Tier B checks blocked and at least one, non-empty `manual_checklist_artifact_id`; no sign-off in the gate, the coordinator collects the browser confirmation outside the ledger). `FAIL_REQUIRES_USER_SIGNOFF` passes only with `user_signoff`; `FAIL_REQUIRES_FIX` and `BLOCKED_DEPLOY_INACCESSIBLE` never pass |

Counts such as `total_must_fix` must be real integers (`0`, not `false`, `0.0` or `"0"`).

Two producers used to write the same file name. The actor summaries are now
`provenance/bibliography_summary.json` (Phase 9) and `provenance/push_summary.json` (Phase 20),
so `gate_bibliography.json` and `gate_repository_push.json` are written only by their validators.

## State file

`provenance/phase_ledger.json`, schema `comprev.phase_ledger/1`: `run_id`, `revision`,
timestamps, one entry per canonical phase (`status`, `artifact_id`, `child_ids`, `completed_at`,
`gate_file`, `gate_sha256`, `report_sha256`, `outputs`, `deferred_accepted`) and an `events` list
with one row per revision. Loading is strict: unknown or missing keys, wrong types, a completed
phase after a pending one, a pending entry carrying data, or `events` not matching `revision`
raise `LedgerCorrupt`.

* **Initialise vs load.** Only `init_ledger` creates the file, and it refuses if *anything* is
  already there (including a corrupt file). Loading a missing file raises `LedgerMissing`. Nothing
  ever resets state implicitly.
* **Atomic writes.** Temp file in the same directory, `fsync`, `os.replace`, directory `fsync`.
  A crash or error before the replace leaves the previous file intact; a stray
  `phase_ledger.json.*.tmp` is ignored and may be deleted.
* **Concurrency.** Mutations hold an exclusive lock (`phase_ledger.json.lock`, `flock`; an
  exclusive-create lock file on platforms without `fcntl`, which a crashed holder leaves behind -
  delete it only when no writer is running), reload, then write. Pass
  `expected_revision=ledger.revision` to get `StaleLedger` instead of overwriting a newer state.
  Waiting longer than `LOCK_TIMEOUT` (10 s) raises `LedgerLocked`.
* **Integrity.** `require_phase`, `check_gate` and `restart` re-read every gate and output up to
  the phase asked for. A gate edited, replaced or deleted after approval, or one that no longer
  passes its contract, raises `GateError`. `report` digests (1V, 15V, 17V, 19V) cannot be
  re-derived from disk; only the recorded digest is kept.

## Recovery policy

| Situation | Action |
|---|---|
| New session | `restart()`. It must succeed before any delegation. |
| `LedgerMissing` | Start of a run: `init_ledger()`. Mid-run: restore `provenance/phase_ledger.json` from the last saved artifact; do not re-initialise. |
| `LedgerCorrupt` | Stop. The file is untouched. Restore the last saved artifact copy, or keep the damaged file for inspection and rebuild by re-registering phases in order (below). Never edit the JSON by hand: the revision/event checks will reject it. |
| `StaleLedger` | Another writer advanced the ledger. `load_ledger()`, look at what changed, retry. |
| `GateError` after approval ("changed after ... approved") | A gate or output was modified. Find out why. To redo the phase and everything after it: `reopen_phase('<id>', 'reason')`, regenerate, advance again. |
| A phase must be redone (critic found a defect, fix pass) | `reopen_phase(id, reason)` resets `id` and every later phase to pending and records the reason. |
| Stale `.lock` on a platform without `fcntl` | Delete it once you have confirmed no writer is running. |

## Migration from the pre-ledger state

Earlier runs kept `phase_ledger = {'phase_1': {...}}` in memory, wrote it to a bare
`phase_ledger.json`, and used ids such as `3_curation`. Those files are rejected as
`LedgerCorrupt` (wrong schema) and are **not** auto-converted: a legacy `complete` meant "the
action was reported done", which is weaker than a validated gate.

To continue such a run: move the old file aside, `init_ledger()`, then re-register each finished
phase **in order** with `advance_phase`, using the saved gate artifacts and frame ids. Gates are
validated as they are registered, so a run whose gates do not meet the contracts above stops at
the first one that does not. `resolve_phase(old_id, allow_legacy=True)` translates old ids:

| Legacy | Canonical | | Legacy | Canonical |
|---|---|---|---|---|
| `2_scaffold` | `4` | | `6_integration` | `10` |
| `3_curation` | `5V` | | `11_intro_conclusion` | `11` |
| `3b_figure_audit` | `6` | | `13_methods` | `13` |
| `4b_critic` | `8` | | `14_github_push` | `20` |
| `5_bibliography` | `9V` | | | |

## Tests

`python -m unittest discover -s test -p 'test_phase_ledger.py'` (also run with `python -O` in
CI). Covers canonical order against the orchestrator index, invalid ids, every gate contract,
missing/failed/malformed gates, tampering after approval, malformed and truncated state,
restart across processes, interrupted and concurrent writes, and the skills' own ledger calls.
