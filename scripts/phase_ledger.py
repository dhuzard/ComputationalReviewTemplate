#!/usr/bin/env python3
"""Canonical phase ledger and fail-closed prerequisite checks.

The coordinator records each finished phase here and asks here whether the next
one may start. Everything is deterministic: no network, no LLM, no `assert`
(so `python -O` cannot weaken it). Standard library only, Python >= 3.9.

    from phase_ledger import init_ledger, advance_phase, require_phase, check_gate

State is `provenance/phase_ledger.json`. It is created only by `init_ledger`;
`load_ledger` never creates or resets it. Every mutation takes an exclusive
lock, reloads the file, optionally compares `expected_revision` (stale-writer
protection), and replaces the file atomically.

Two things are kept apart:
  * action completion -- an actor/critic phase ran, recorded as a receipt
    (`artifact_id` + the agent `child_ids` + sha256 of any named output files);
  * validator approval -- the `NV` phase, recorded only when its verdict passes.
Phase N+1 requires the previous phase in `PHASE_ORDER`, so a missing `NV` blocks it.

What this can and cannot prove: it checks that a named gate file exists, parses
as one JSON object, is bound by sha256 to the receipt, and says what its
producer documents as success. It does not re-run any scientific check, and it
cannot tell a truthful validator from a forged file; independent validators stay
independent (child ids must differ from the actor's).
"""
import argparse
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
import time
from collections import namedtuple
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

try:  # POSIX advisory locks; elsewhere fall back to an exclusive lock file
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

SCHEMA = "comprev.phase_ledger/1"
LEDGER = "provenance/phase_ledger.json"
LOCK_TIMEOUT = 10.0
MAX_GATE_BYTES = 16 * 1024 * 1024


class LedgerError(Exception):
    """Base class: every failure here means 'do not proceed'."""


class InvalidPhase(LedgerError, ValueError):
    pass


class LedgerExists(LedgerError):
    pass


class LedgerMissing(LedgerError):
    pass


class LedgerCorrupt(LedgerError):
    pass


class LedgerLocked(LedgerError):
    pass


class StaleLedger(LedgerError):
    pass


class PhaseBlocked(LedgerError):
    pass


class GateError(LedgerError):
    pass


# ---------------------------------------------------------------- contracts
def _require(cond, msg):
    if not cond:
        raise GateError(msg)


def _s(v):
    return isinstance(v, str) and v.strip() != "" and not (v.startswith("<") and v.endswith(">"))


def _i(v):
    return type(v) is int


def _ids(v):
    return isinstance(v, list) and bool(v) and all(_s(x) for x in v)


def _vid_map(v):
    return isinstance(v, dict) and bool(v) and all(_s(k) and _s(x) for k, x in v.items())


def _check_scope(o):
    for k in ("clusters", "sections"):
        _require(isinstance(o.get(k), list) and o[k], f"`{k}` must be a non-empty list")
    _require(_s(o.get("title")), "`title` must be a non-empty string")
    _require(isinstance(o.get("evidence_parameters"), dict) and o["evidence_parameters"],
             "`evidence_parameters` must be a non-empty object")
    _require(_s(o.get("review_request_path")), "`review_request_path` missing")


def _check_citation_infra(o):
    for k in ("citation_key_map_vid", "author_name_table_vid"):
        _require(_s(o.get(k)), f"`{k}` is null or missing (blocks all downstream phases)")


def _check_scaffold(o):
    _require(_s(o.get("scaffold_artifact_id")), "`scaffold_artifact_id` missing")
    for k in ("sections_with_connections", "cross_refs_specific", "conflicts_represented",
              "figures_per_section_met"):
        _require(o.get(k) is True, f"`{k}` must be true")


def _check_figure_audit(o):
    _require(_i(o.get("redesign_remaining")) and o["redesign_remaining"] == 0,
             "`redesign_remaining` must be the integer 0")
    _require(isinstance(o.get("verdict_counts_post_resolution"), dict)
             and o["verdict_counts_post_resolution"], "`verdict_counts_post_resolution` missing")
    _require(_ids(o.get("critic_frame_ids")), "`critic_frame_ids` must list the critic frames")


def _check_critic(o):
    _require(_i(o.get("total_must_fix")) and o["total_must_fix"] == 0,
             "`total_must_fix` must be the integer 0")
    _require(o.get("coverage_review") is not None, "`coverage_review` missing")


SIX_PASSES = {"6a_transition", "6b_crossref", "6c_terminology", "6d_continuity",
              "6e_figures", "6f_hygiene"}


def _check_integration(o):
    passes = o.get("passes_completed")
    _require(isinstance(passes, list) and set(passes) == SIX_PASSES and len(passes) == 6,
             "`passes_completed` must list each of the six integration passes once")
    for k in ("filename_mismatches", "structural_issues"):
        _require(isinstance(o.get(k), list), f"`{k}` must be a list")


def _check_intro(o):
    for k in ("introduction_artifact_id", "conclusion_artifact_id", "abstract_artifact_id",
              "frontmatter_artifact_id"):
        _require(_s(o.get(k)), f"`{k}` missing")
    _require(o.get("novel_citations") == [], "`novel_citations` must be an empty list")
    _require(_i(o.get("master_citation_count")) and o["master_citation_count"] > 0,
             "`master_citation_count` must be a positive integer")


def _check_bookend(o):
    _require(_i(o.get("total_must_fix")) and o["total_must_fix"] == 0,
             "`total_must_fix` must be the integer 0")


def _check_methods(o):
    _require(_s(o.get("methods_artifact_id")), "`methods_artifact_id` missing")
    _require(_i(o.get("subsection_count")) and o["subsection_count"] == 8,
             "`subsection_count` must be 8")
    _require(o.get("reproducibility_statement_present") is True,
             "`reproducibility_statement_present` must be true")


def _check_myst(o):
    _require(isinstance(o.get("structural_results"), dict) and o["structural_results"],
             "`structural_results` must be a non-empty object")


def _check_final_vids(o):
    _check_myst(o)
    _require(_vid_map(o.get("section_md_vids_final")), "`section_md_vids_final` missing or empty")


def _check_refresh(o):
    _require(_i(o.get("forbidden_hits")) and o["forbidden_hits"] == 0,
             "`forbidden_hits` must be the integer 0")
    _require(o.get("figure_regenerated") is True, "`figure_regenerated` must be true")
    _require(_i(o.get("frame_count")) and o["frame_count"] > 0,
             "`frame_count` must be a positive integer")


Spec = namedtuple("Spec", "id gate verdict number check report_check outputs")
# verdict: "file" = the gate file carries the validator verdict; "report" = the
# validator's structured result is passed to advance_phase; None = no verdict.
# number: the `phase` value a validator-authored object must carry.


def _spec(pid, gate=None, verdict=None, number=None, check=None, report_check=None, outputs=()):
    return Spec(pid, gate, verdict, number, check, report_check, tuple(outputs))


PHASE_ORDER = ("1", "1V", "2", "2V", "3", "3V", "4", "5", "5V", "6", "7", "7V", "8", "9", "9V",
               "10", "11", "12", "13", "14", "14V", "15", "15V", "16", "17", "17V", "18", "19",
               "19V", "20a", "20", "20V", "21")

PHASES = {s.id: s for s in (
    _spec("1"),
    _spec("1V", "gate_scope.json", "report", 1, _check_scope),
    _spec("2"),
    _spec("2V", "gate_evidence_compliance.json", "file", 2),
    _spec("3"),
    _spec("3V", "gate_citation_infrastructure.json", "file", 3, _check_citation_infra),
    _spec("4", "gate_scaffold_approved.json", None, None, _check_scaffold),
    _spec("5"),
    _spec("5V", "gate_evidence_curated.json", "file", 5),
    _spec("6", "gate_figure_audit.json", None, None, _check_figure_audit),
    _spec("7"),
    _spec("7V", "gate_sections_drafted.json", "file", 7, _check_final_vids),
    _spec("8", "gate_critic_complete.json", None, None, _check_critic),
    _spec("9"),
    _spec("9V", "gate_bibliography.json", "file", 9),
    _spec("10", "gate_integration.json", None, None, _check_integration),
    _spec("11", "gate_intro_conclusion.json", None, None, _check_intro),
    _spec("12", "gate_bookend_critic.json", None, None, _check_bookend),
    _spec("13", "gate_methods.json", None, None, _check_methods),
    _spec("14"),
    _spec("14V", "gate_assembly.json", "file", 14, _check_final_vids),
    _spec("15", outputs=("citation_triples.json",)),
    _spec("15V", verdict="report", number=15),
    _spec("16"),
    _spec("17", outputs=("fix_requests.json",)),
    _spec("17V", verdict="report", number=17),
    _spec("18"),
    _spec("19"),
    _spec("19V", verdict="report", number=19, report_check=_check_final_vids),
    _spec("20a", "gate_phase_20a_methods_refresh.json", None, None, _check_refresh),
    _spec("20"),
    _spec("20V", "gate_repository_push.json", "file", 20, _check_myst),
    _spec("21", "gate_phase21_deploy_polish.json", "file", None),
)}
if list(PHASES) != list(PHASE_ORDER):  # import-time self-consistency
    raise RuntimeError("PHASES and PHASE_ORDER disagree")

GATES = {s.gate: s.id for s in PHASES.values() if s.gate}

# Identifiers used by earlier revisions of the skills, for translating old text only.
# They are never accepted implicitly (see resolve_phase).
LEGACY_ALIASES = {
    "2_scaffold": "4", "3_curation": "5V", "3b_figure_audit": "6", "4b_critic": "8",
    "5_bibliography": "9V", "6_integration": "10", "11_intro_conclusion": "11",
    "13_methods": "13", "14_github_push": "20",
}

# Whose child frames must differ: validators from their actor, critics from the writers.
INDEPENDENT_OF = {s: s[:-1] for s in PHASE_ORDER if s.endswith("V")}
INDEPENDENT_OF.update({"8": "7", "12": "11"})


def resolve_phase(phase, allow_legacy=False):
    """Return the canonical id for `phase`, or raise InvalidPhase."""
    if type(phase) is int:
        phase = str(phase)
    if isinstance(phase, str):
        if phase in PHASES:
            return phase
        if allow_legacy and phase in LEGACY_ALIASES:
            return LEGACY_ALIASES[phase]
    raise InvalidPhase(f"unknown phase id {phase!r}; valid ids: {', '.join(PHASE_ORDER)}")


def _previous(pid):
    i = PHASE_ORDER.index(pid)
    return PHASE_ORDER[i - 1] if i else None


# ------------------------------------------------------- verdict evaluation
def _signed_off(o):
    so = o.get("user_signoff")
    return isinstance(so, dict) and so.get("decision") == "proceed" and _s(so.get("note"))


def _verdict_problem(o, number, allow_deferred):
    """Return (problem, deferred). `problem` is None when the object approves."""
    if not isinstance(o, dict):
        return "verdict is not a JSON object", False
    if number is not None and not (_i(o.get("phase")) and o["phase"] == number):
        return f"`phase` must be the integer {number}, got {o.get('phase')!r}", False
    needs = o.get("user_signoff_required") is True or o.get("verdict") == "FAIL_REQUIRES_USER_SIGNOFF"
    signed = False
    if needs:
        if not _signed_off(o):
            return "needs `user_signoff` {decision: proceed, note} before it can pass", False
        signed = True
    deferred = False
    seen = False
    if "gate" in o:
        seen = True
        g = o["gate"]
        if g == "pass_with_deferred":
            if not (allow_deferred and _ids(o.get("deferred_checks")) and
                    isinstance(o.get("tool_probe"), dict) and o["tool_probe"]):
                return "pass_with_deferred needs allow_deferred plus deferred_checks and tool_probe", False
            deferred = True
        elif g != "pass" and not (signed and g == "fail"):
            return f"gate={g!r}", False
    if "gate_passed" in o:
        seen = True
        v = o["gate_passed"]
        if deferred:
            if v is True:
                return "gate_passed is true although checks were deferred", False
        elif v is not True and not (signed and v is False):
            return f"gate_passed={v!r}", False
    if "verdict" in o:
        seen = True
        v = o["verdict"]
        ok = (v in ("PASS", "pass") or (v == "FAIL_REQUIRES_USER_SIGNOFF" and signed)
              or (v == "PASS_WITH_BLOCKED_TIER_B" and _signed_off(o)))
        if not ok:
            return f"verdict={v!r}", False
    if not seen:
        return "no `gate`, `gate_passed` or `verdict` field", False
    if not signed:  # a pass that contradicts its own detail is not a pass
        for key in ("checks", "structural_results"):
            detail = o.get(key) if isinstance(o.get(key), dict) else {}
            for name, val in detail.items():
                bad = (isinstance(val, str) and val.lower() == "fail") or (
                    isinstance(val, dict) and (_i(val.get("fail")) and val["fail"] > 0
                                               or str(val.get("verdict", "")).upper() == "FAIL"))
                if bad:
                    return f"{key}.{name} failed although the verdict passes", False
        if isinstance(o.get("failures"), list) and o["failures"]:
            return "`failures` is non-empty although the verdict passes", False
    return None, deferred


PHASE21_GATE = "phase_21_deploy_polish"
# The binary checks of skills/comprev-deploy-polish.md and their tiers; a test diffs this against the
# skill's table so the two cannot drift. Every one must be reported (an inapplicable conditional
# check such as FIGURE_DROPDOWN_COMPLETE is reported PASS with evidence saying so).
PHASE21_CHECKS = {
    "BUILD_GREEN_AT_HEAD": "A", "DEPLOY_ARTIFACT_AVAILABLE": "A", "PER_PAGE_HTTP_200": "B",
    "NO_FRONTMATTER_LEAK": "A", "FIGURE_DROPDOWN_COMPLETE": "A", "FIGURE_ASSETS_RESOLVE": "A",
    "INTERNAL_LINK_HEALTH": "A", "EXTERNAL_LINK_HEALTH": "B", "PLUGIN_DATA_BOUND": "A",
    "EVIDENCE_EXPLORER_STATUS_RENDERED": "A", "REVIEW_COMPATIBILITY_REPORT_VALID": "A",
    "DIRECTIVE_RENDERED_OK": "A", "FORBIDDEN_LEXICON_DEPLOYED": "A", "AUTHOR_IDENTITY_DEPLOYED": "A",
}
PHASE21_BLOCKED_SPELLINGS = ("BLOCKED", "BLOCKED_DEPLOY_INACCESSIBLE")  # both appear in the skill


def _phase21_confirmation_problem(o, blocked):
    """The skill's Coordinator Loop Discipline requires explicit browser confirmation that the
    blocked Tier B checks passed before the run is marked complete. Receipt: `user_signoff`
    {decision: "proceed", note, browser_confirmed_checks: [every blocked Tier B check name]}."""
    so = o.get("user_signoff")
    if not _signed_off(o):
        return "PASS_WITH_BLOCKED_TIER_B needs `user_signoff` {decision: proceed, note} (browser confirmation)"
    got = so.get("browser_confirmed_checks")
    if not (isinstance(got, list) and all(isinstance(x, str) for x in got) and set(got) == set(blocked)
            and len(got) == len(blocked)):
        return ("`user_signoff.browser_confirmed_checks` must list exactly the blocked Tier B checks: "
                + ", ".join(sorted(blocked)))
    return None


def _phase21_problem(o):
    """Phase 21 as skills/comprev-deploy-polish.md documents it: `gate` is the constant gate name
    and `verdict` carries the outcome. Every documented check must be reported with its tier."""
    if not isinstance(o, dict):
        return "verdict is not a JSON object"
    if o.get("gate") != PHASE21_GATE:
        return f"gate must be {PHASE21_GATE!r}, got {o.get('gate')!r}"
    v = o.get("verdict")
    if v not in ("PASS", "PASS_WITH_BLOCKED_TIER_B"):
        # FAIL_REQUIRES_USER_SIGNOFF only hands direction back to the user; it is never a pass.
        return f"verdict={v!r} does not complete Phase 21"
    checks = o.get("checks")
    if not isinstance(checks, dict):
        return "`checks` must be an object"
    missing = sorted(set(PHASE21_CHECKS) - set(checks))
    if missing:
        return "missing required checks: " + ", ".join(missing)
    unknown = sorted(set(checks) - set(PHASE21_CHECKS))
    if unknown:
        return "unknown checks: " + ", ".join(unknown)
    blocked = []
    for name, tier in PHASE21_CHECKS.items():
        c = checks[name]
        if not isinstance(c, dict):
            return f"checks.{name} must be an object"
        if c.get("tier") != tier:
            return f"checks.{name}.tier must be {tier!r}, got {c.get('tier')!r}"
        cv = c.get("verdict")
        if cv == "PASS":
            if not (isinstance(c.get("evidence"), list) and c["evidence"]):
                return f"checks.{name} PASS needs non-empty `evidence`"
        elif cv in PHASE21_BLOCKED_SPELLINGS and tier == "B":
            blocked.append(name)
        else:
            return f"checks.{name}.verdict={cv!r} is not a passing result for Tier {tier}"
    if o.get("fix_list") not in (None, []):
        return "`fix_list` is non-empty although the verdict passes"
    if isinstance(o.get("failures"), list) and o["failures"]:
        return "`failures` is non-empty although the verdict passes"
    if o.get("user_signoff_required") is True and not _signed_off(o):
        return "needs `user_signoff` {decision: proceed, note} before it can pass"
    if v == "PASS":
        if blocked or o.get("deploy_accessible") is not True:
            return "verdict PASS needs every check PASS and deploy_accessible=true"
        if o.get("input_source") not in ("pages_artifact_tarball", "live_url"):
            return "verdict PASS needs input_source pages_artifact_tarball or live_url"
        return None
    tier_b = [n for n, t in PHASE21_CHECKS.items() if t == "B"]
    if o.get("deploy_accessible") is not False or sorted(blocked) != sorted(tier_b):
        return "PASS_WITH_BLOCKED_TIER_B needs deploy_accessible=false and every Tier B check blocked"
    if o.get("input_source") != "pages_artifact_tarball":
        return "PASS_WITH_BLOCKED_TIER_B needs input_source pages_artifact_tarball (Tier A ran on it)"
    if not _s(o.get("manual_checklist_artifact_id")):
        return "PASS_WITH_BLOCKED_TIER_B needs `manual_checklist_artifact_id`"
    return _phase21_confirmation_problem(o, blocked)


def _file_problem(spec, o, allow_deferred):
    if spec.id == "21":
        return _phase21_problem(o), False
    return _verdict_problem(o, spec.number, allow_deferred)


# ------------------------------------------------------------ file helpers
def _strict_loads(text):
    def pairs(items):
        d = {}
        for k, v in items:
            if k in d:
                raise ValueError(f"duplicate key {k!r}")
            d[k] = v
        return d

    def bad(c):
        raise ValueError(f"non-finite constant {c}")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=bad)


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _find(root, rel):
    """`provenance/<rel>` wins over `<root>/<rel>`; returns (relative posix path, Path) or None."""
    for base in ("provenance", ""):
        p = Path(root, base, rel)
        if p.is_symlink():
            raise GateError(f"{p} is a symlink; refusing to follow it")
        if p.exists():
            return (f"{base}/{rel}" if base else rel), p
    return None


def _read_gate(root, name):
    found = _find(root, name)
    if not found:
        raise GateError(f"gate {name} not found under {root}/provenance or {root}")
    rel, p = found
    if not p.is_file() or p.stat().st_size > MAX_GATE_BYTES:
        raise GateError(f"{rel} is not a regular file of acceptable size")
    raw = p.read_bytes()
    try:
        obj = _strict_loads(raw.decode("utf-8"))
    except (ValueError, RecursionError) as e:
        raise GateError(f"{rel} is not valid JSON: {e}")
    if not isinstance(obj, dict):
        raise GateError(f"{rel} must contain one JSON object")
    return rel, obj, _sha256(raw)


def _hash_path(p):
    if p.is_symlink():
        raise GateError(f"{p} is a symlink; refusing to follow it")
    if p.is_file():
        data = p.read_bytes()
        _require(data, f"required output {p} is empty")
        return _sha256(data)
    files = sorted(f for f in p.rglob("*") if f.is_file() and not f.is_symlink()) if p.is_dir() else []
    _require(files, f"required output {p} is missing or contains no files")
    h = hashlib.sha256()
    for f in files:
        h.update(f.relative_to(p).as_posix().encode() + b"\0" + _sha256(f.read_bytes()).encode() + b"\n")
    return h.hexdigest()


def _hash_output(root, rel):
    found = _find(root, rel)
    if not found:
        raise GateError(f"required output {rel} not found")
    return found[0], _hash_path(found[1])


def _artifact_ok(v):
    return isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+=-]{0,199}", v) is not None


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------- state I/O
def _empty_entry():
    return {"status": "pending", "artifact_id": None, "child_ids": [], "completed_at": None,
            "gate_file": None, "gate_sha256": None, "report_sha256": None,
            "outputs": {}, "deferred_accepted": False}


ENTRY_KEYS = set(_empty_entry())
STATE_KEYS = {"schema", "run_id", "revision", "created_at", "updated_at", "phases", "events"}
EVENT_KEYS = {"rev", "op", "phase", "at", "reason"}
_HEX64 = re.compile(r"[0-9a-f]{64}")


def _bad(msg):
    raise LedgerCorrupt(msg)


def _validate_state(st):
    if not isinstance(st, dict) or set(st) != STATE_KEYS:
        _bad("state must be an object with exactly: " + ", ".join(sorted(STATE_KEYS)))
    if st["schema"] != SCHEMA:
        _bad(f"unsupported schema {st['schema']!r} (expected {SCHEMA})")
    if not (isinstance(st["run_id"], str) and re.fullmatch(r"[0-9a-f]{32}", st["run_id"])):
        _bad("run_id must be 32 hex chars")
    if not (_i(st["revision"]) and st["revision"] >= 0):
        _bad("revision must be a non-negative integer")
    for k in ("created_at", "updated_at"):
        try:
            datetime.fromisoformat(st[k])
        except (TypeError, ValueError):
            _bad(f"{k} must be an ISO-8601 timestamp")
    if not isinstance(st["phases"], dict) or set(st["phases"]) != set(PHASE_ORDER):
        _bad("phases must contain exactly the canonical phase ids")
    gap = None
    for pid in PHASE_ORDER:
        e, spec = st["phases"][pid], PHASES[pid]
        if not isinstance(e, dict) or set(e) != ENTRY_KEYS:
            _bad(f"phase {pid}: entry must have exactly: " + ", ".join(sorted(ENTRY_KEYS)))
        if e["status"] == "pending":
            if e != _empty_entry():
                _bad(f"phase {pid}: pending entry carries completion data")
            gap = gap or pid
            continue
        if e["status"] != "complete":
            _bad(f"phase {pid}: status must be pending or complete")
        if gap:
            _bad(f"phase {pid} is complete but earlier phase {gap} is not")
        if not _artifact_ok(e["artifact_id"]):
            _bad(f"phase {pid}: bad artifact_id")
        if not (isinstance(e["child_ids"], list) and e["child_ids"] and
                all(_artifact_ok(c) for c in e["child_ids"]) and len(set(e["child_ids"])) == len(e["child_ids"])):
            _bad(f"phase {pid}: child_ids must be a non-empty list of unique ids")
        try:
            datetime.fromisoformat(e["completed_at"])
        except (TypeError, ValueError):
            _bad(f"phase {pid}: bad completed_at")
        if spec.gate:
            if not (isinstance(e["gate_file"], str) and e["gate_file"].endswith(spec.gate)):
                _bad(f"phase {pid}: gate_file must be {spec.gate}")
            if not (isinstance(e["gate_sha256"], str) and _HEX64.fullmatch(e["gate_sha256"])):
                _bad(f"phase {pid}: bad gate_sha256")
        elif e["gate_file"] is not None or e["gate_sha256"] is not None:
            _bad(f"phase {pid}: has no gate but records one")
        if (spec.verdict == "report") != (e["report_sha256"] is not None) or (
                e["report_sha256"] is not None and not (
                isinstance(e["report_sha256"], str) and _HEX64.fullmatch(e["report_sha256"]))):
            _bad(f"phase {pid}: report_sha256 does not match the phase contract")
        if not isinstance(e["outputs"], dict) or ".." in "".join(e["outputs"]) or {Path(k).name for k in e["outputs"]} != {
                Path(o).name for o in spec.outputs} or not all(
                isinstance(v, str) and _HEX64.fullmatch(v) for v in e["outputs"].values()):
            _bad(f"phase {pid}: outputs do not match the phase contract")
        if type(e["deferred_accepted"]) is not bool:
            _bad(f"phase {pid}: deferred_accepted must be boolean")
        if pid == "20" and not re.fullmatch(r"[0-9a-f]{7,64}", e["artifact_id"]):
            _bad("phase 20: artifact_id must be the pushed commit sha")
    ev = st["events"]
    if not isinstance(ev, list) or len(ev) != st["revision"] + 1:
        _bad("events must hold exactly one entry per revision")
    for n, x in enumerate(ev):
        if not isinstance(x, dict) or set(x) != EVENT_KEYS or x["rev"] != n or not _i(x["rev"]) \
                or x["op"] not in ("init", "advance", "reopen") \
                or not (x["phase"] is None or (isinstance(x["phase"], str) and x["phase"] in PHASES)) \
                or not (x["reason"] is None or isinstance(x["reason"], str)) or not isinstance(x["at"], str):
            _bad(f"event {n} is malformed")


def _path(root):
    return Path(root, LEDGER)


def _read_state(root):
    p = _path(root)
    if not p.exists():
        raise LedgerMissing(f"{p} does not exist; call init_ledger() explicitly to start a run")
    try:
        st = _strict_loads(p.read_bytes().decode("utf-8"))
    except (OSError, ValueError, RecursionError) as e:
        raise LedgerCorrupt(f"{p} is unreadable or not valid JSON ({e}); it was NOT reset")
    _validate_state(st)
    return st


def _fsync_dir(d):
    try:
        fd = os.open(str(d), os.O_RDONLY)
    except OSError:  # pragma: no cover - e.g. Windows
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover
        pass
    finally:
        os.close(fd)


def _write_atomic(path, text):
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    _fsync_dir(path.parent)


@contextmanager
def _lock(root):
    p = _path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    lock_path = str(p) + ".lock"
    deadline = time.monotonic() + LOCK_TIMEOUT
    if fcntl:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise LedgerLocked(f"{lock_path} is held by another writer")
                    time.sleep(0.02)
            yield
        finally:
            os.close(fd)
    else:  # pragma: no cover - a crashed holder leaves the file; remove it by hand
        while True:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                break
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise LedgerLocked(f"{lock_path} exists; remove it if no writer is running")
                time.sleep(0.02)
        try:
            yield
        finally:
            os.close(fd)
            os.unlink(lock_path)


class Ledger:
    """Read-only snapshot of a validated ledger."""

    def __init__(self, root, state):
        self.root, self.state = Path(root), state

    revision = property(lambda self: self.state["revision"])
    run_id = property(lambda self: self.state["run_id"])
    phases = property(lambda self: self.state["phases"])
    completed = property(lambda self: [p for p in PHASE_ORDER if self.phases[p]["status"] == "complete"])
    next_phase = property(lambda self: next((p for p in PHASE_ORDER if self.phases[p]["status"] != "complete"), None))


def _mutate(root, expected_revision, fn):
    with _lock(root):
        st = _read_state(root)
        if expected_revision is not None and st["revision"] != expected_revision:
            raise StaleLedger(f"ledger is at revision {st['revision']}, caller holds {expected_revision}; reload")
        op, phase, reason = fn(st)
        st["revision"] += 1
        st["updated_at"] = _now()
        st["events"].append({"rev": st["revision"], "op": op, "phase": phase, "at": st["updated_at"], "reason": reason})
        _validate_state(st)
        _write_atomic(_path(root), json.dumps(st, indent=2, sort_keys=True) + "\n")
        return Ledger(root, st)


# ------------------------------------------------------------- public API
def init_ledger(root="."):
    """Create a fresh ledger. Refuses if any ledger file exists, valid or not."""
    with _lock(root):
        p = _path(root)
        if p.exists() or p.is_symlink():
            raise LedgerExists(f"{p} already exists; use load_ledger()/restart(), or move it aside deliberately")
        now = _now()
        st = {"schema": SCHEMA, "run_id": secrets.token_hex(16), "revision": 0, "created_at": now,
              "updated_at": now, "phases": {pid: _empty_entry() for pid in PHASE_ORDER},
              "events": [{"rev": 0, "op": "init", "phase": None, "at": now, "reason": None}]}
        _validate_state(st)
        _write_atomic(p, json.dumps(st, indent=2, sort_keys=True) + "\n")
        return Ledger(root, st)


def load_ledger(root="."):
    """Load and strictly validate; never creates or resets state."""
    return Ledger(root, _read_state(root))


def _evaluate_gate(spec, root, report, allow_deferred):
    """Validate the gate file (and report) for `spec`; return the facts to record."""
    out = {"gate_file": None, "gate_sha256": None, "report_sha256": None, "deferred_accepted": False}
    if spec.gate:
        rel, obj, sha = _read_gate(root, spec.gate)
        out["gate_file"], out["gate_sha256"] = rel, sha
        if spec.verdict == "file":
            problem, deferred = _file_problem(spec, obj, allow_deferred)
            _require(problem is None, f"{rel}: {problem}")
            out["deferred_accepted"] = deferred
        if spec.check:
            spec.check(obj)
    if spec.verdict == "report":
        _require(isinstance(report, dict), f"phase {spec.id} needs the validator's `report` object")
        problem, deferred = _verdict_problem(report, spec.number, allow_deferred)
        _require(problem is None, f"phase {spec.id} validator report: {problem}")
        if spec.report_check:
            spec.report_check(report)
        out["report_sha256"] = _sha256(_canonical(report))
        out["deferred_accepted"] = deferred
    return out


def advance_phase(phase, artifact_id, child_ids=None, *, report=None, allow_deferred=False,
                  root=".", expected_revision=None):
    """Record `phase` complete, after checking its prerequisite, receipt and gate.

    artifact_id: the saved version id of the gate artifact (or, for phases without
    a gate, of the main output; for phase 20, the pushed commit sha).
    child_ids: the agent frames that did the work (validators/critics must differ
    from the actor/writers). report: the validator's structured result, required
    only for phases whose verdict is not in a named file (1V, 15V, 17V, 19V).
    """
    pid = resolve_phase(phase)
    spec = PHASES[pid]
    if not _artifact_ok(artifact_id):
        raise GateError(f"artifact_id {artifact_id!r} is not a concrete artifact id (placeholder?)")
    ids = list(child_ids) if isinstance(child_ids, (list, tuple)) else []
    if not (ids and all(_artifact_ok(c) for c in ids) and len(set(ids)) == len(ids)):
        raise GateError("child_ids must be a non-empty list of unique agent frame ids")
    if pid == "20" and not re.fullmatch(r"[0-9a-f]{7,64}", artifact_id):
        raise GateError("phase 20 artifact_id must be the pushed commit sha")
    if report is not None and spec.verdict != "report":
        raise GateError(f"phase {pid} takes no report; its verdict lives in {spec.gate or 'no file'}")

    def apply(st):
        e = st["phases"][pid]
        if e["status"] == "complete":
            raise PhaseBlocked(f"phase {pid} is already complete; reopen_phase() to redo it")
        prev = _previous(pid)
        if prev and st["phases"][prev]["status"] != "complete":
            raise PhaseBlocked(f"phase {pid} blocked: phase {prev} is not complete")
        # Every earlier receipt must still match the disk, not just the previous status.
        for p in PHASE_ORDER[:PHASE_ORDER.index(pid)]:
            if st["phases"][p]["status"] == "complete":
                _verify_entry(root, p, st["phases"][p])
        other = INDEPENDENT_OF.get(pid)
        if other and set(ids) & set(st["phases"][other]["child_ids"]):
            raise GateError(f"phase {pid} must run in different agent frames than phase {other}")
        facts = _evaluate_gate(spec, root, report, allow_deferred)
        outputs = {}
        for rel in spec.outputs:
            shown, sha = _hash_output(root, rel)
            outputs[shown] = sha
        e.update(status="complete", artifact_id=artifact_id, child_ids=ids, completed_at=_now(),
                 outputs=outputs, **facts)
        return "advance", pid, None

    return _mutate(root, expected_revision, apply)


def reopen_phase(phase, reason, *, root=".", expected_revision=None):
    """Reset `phase` and every later phase to pending, recording why."""
    pid = resolve_phase(phase)
    if not (isinstance(reason, str) and reason.strip()):
        raise LedgerError("reopen_phase needs a non-empty reason")

    def apply(st):
        for p in PHASE_ORDER[PHASE_ORDER.index(pid):]:
            st["phases"][p] = _empty_entry()
        return "reopen", pid, reason

    return _mutate(root, expected_revision, apply)


def _verify_entry(root, pid, e):
    spec = PHASES[pid]
    if spec.gate:
        rel, obj, sha = _read_gate(root, spec.gate)
        if sha != e["gate_sha256"]:
            raise GateError(f"{rel} changed after phase {pid} was approved; reopen_phase('{pid}', ...) to redo it")
        if spec.verdict == "file":
            problem, deferred = _file_problem(spec, obj, e["deferred_accepted"])
            _require(problem is None and deferred == e["deferred_accepted"], f"{rel}: {problem or 'deferral changed'}")
        if spec.check:
            spec.check(obj)
    for rel, sha in e["outputs"].items():
        if _hash_path(Path(root, rel)) != sha:
            raise GateError(f"output {rel} changed after phase {pid} completed")


def restart(root="."):
    """Load the ledger and re-verify every completed phase against the files on disk."""
    ledger = load_ledger(root)
    for pid in ledger.completed:
        _verify_entry(root, pid, ledger.phases[pid])
    return ledger


def require_phase(phase, root="."):
    """Return the artifact id of `phase` if it and everything before it is complete and still intact."""
    pid = resolve_phase(phase)
    ledger = load_ledger(root)
    if ledger.phases[pid]["status"] != "complete":
        raise PhaseBlocked(f"BLOCKED: phase {pid} is not complete (next is {ledger.next_phase})")
    for p in PHASE_ORDER[:PHASE_ORDER.index(pid) + 1]:
        _verify_entry(root, p, ledger.phases[p])
    return ledger.phases[pid]["artifact_id"]


def check_gate(gate_name, root="."):
    """Return the pinned artifact id registered for `gate_name`, re-validating the file on disk."""
    if gate_name not in GATES:
        raise GateError(f"unknown gate {gate_name!r}; known: {', '.join(sorted(GATES))}")
    return require_phase(GATES[gate_name], root)


def completed_phases(root="."):
    """Canonical ids of completed phases (no gate re-verification)."""
    return load_ledger(root).completed


# --------------------------------------------------------------------- CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description="Inspect or initialise the phase ledger.")
    ap.add_argument("command", choices=("init", "status", "verify"))
    ap.add_argument("--root", default=".")
    a = ap.parse_args(argv)
    try:
        ledger = init_ledger(a.root) if a.command == "init" else (
            restart(a.root) if a.command == "verify" else load_ledger(a.root))
    except LedgerError as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(f"run {ledger.run_id} revision {ledger.revision}; completed: "
          f"{', '.join(ledger.completed) or 'none'}; next: {ledger.next_phase or 'done'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
