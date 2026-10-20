"""Tests for scripts/phase_ledger.py: canonical ids, fail-closed gates, durable state."""
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "test"))

import phase_ledger as pl  # noqa: E402
import citation_occurrences as co  # noqa: E402
import test_citation_occurrences as tco  # noqa: E402  (real manuscript/evidence fixtures; module only)

VALIDATOR = {"gate": "pass", "gate_passed": True, "structural_results": {"X": "pass"},
             "section_md_vids_final": {"01.md": "v1"}}


def _skill_phase21_checks():
    """check -> tier, extracted from the Binary Checks table of the deploy-polish skill."""
    text = (ROOT / "skills" / "comprev-deploy-polish.md").read_text()
    rows = re.findall(r"^\| `([A-Z0-9_]+)` \| ([AB]) \|", text, re.M)
    return dict(rows)


SKILL_CHECKS = _skill_phase21_checks()
TIER_B = sorted(n for n, t in SKILL_CHECKS.items() if t == "B")


# Shape documented in skills/comprev-deploy-polish.md ("Fix-List Output").
def _p21_checks(blocked=(), spelling="BLOCKED"):
    return {n: {"verdict": spelling if n in blocked else "PASS", "tier": t,
                "evidence": [] if n in blocked else [f"{n}: ok"]} for n, t in SKILL_CHECKS.items()}


PHASE21_PASS = {
    "gate": "phase_21_deploy_polish", "verdict": "PASS", "deploy_accessible": True,
    "input_source": "pages_artifact_tarball", "checks": _p21_checks(), "fix_list": [],
}
PHASE21_BLOCKED = dict(
    PHASE21_PASS, verdict="PASS_WITH_BLOCKED_TIER_B", deploy_accessible=False,
    checks=_p21_checks(TIER_B), manual_checklist_artifact_id="manual_phase21_checklist.md")
PHASE21_CONFIRMED = dict(
    PHASE21_BLOCKED, user_signoff={"decision": "proceed", "note": "verified in browser",
                                   "browser_confirmed_checks": TIER_B})


def gate_body(pid):
    """A minimal object that satisfies the documented contract of `pid`."""
    spec = pl.PHASES[pid]
    body = {"gate": "pass", "phase": spec.number} if spec.verdict else {}
    extra = {
        "1V": {"title": "T", "clusters": [{}], "sections": [{}], "evidence_parameters": {"a": 1},
               "review_request_path": "provenance/review_request.md"},
        "3V": {"citation_key_map_vid": "k1", "author_name_table_vid": "a1"},
        "4": {"scaffold_artifact_id": "s1", "sections_with_connections": True, "cross_refs_specific": True,
              "conflicts_represented": True, "figures_per_section_met": True},
        "6": {"redesign_remaining": 0, "verdict_counts_post_resolution": {"KEEP": 3},
              "critic_frame_ids": ["c1"]},
        "7V": VALIDATOR, "14V": VALIDATOR, "20V": {"structural_results": {"X": "pass"}},
        "8": {"total_must_fix": 0, "coverage_review": []},
        "10": {"passes_completed": sorted(pl.SIX_PASSES), "filename_mismatches": [], "structural_issues": []},
        "11": {"introduction_artifact_id": "i", "conclusion_artifact_id": "c", "abstract_artifact_id": "a",
               "frontmatter_artifact_id": "f", "novel_citations": [], "master_citation_count": 40},
        "12": {"total_must_fix": 0},
        "13": {"methods_artifact_id": "m", "subsection_count": 8, "reproducibility_statement_present": True},
        "20a": {"forbidden_hits": 0, "figure_regenerated": True, "frame_count": 12},
        "21": PHASE21_PASS,
    }.get(pid, {})
    body.update(extra)
    return body


def report_for(pid):
    r = {"gate": "pass", "phase": pl.PHASES[pid].number}
    if pid == "19V":
        r.update(VALIDATOR)
    return r


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = str(self.tmp)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.n = 0

    def write_gate(self, pid, body=None, where="provenance"):
        d = self.tmp / where if where else self.tmp
        d.mkdir(parents=True, exist_ok=True)
        (d / pl.PHASES[pid].gate).write_text(json.dumps(gate_body(pid) if body is None else body))

    def manuscript(self):
        if not (self.tmp / "content").exists():
            tco.make_manuscript(self.tmp)

    def edit(self, rel, old, new, required=True):
        p = self.tmp / rel
        text = p.read_text(encoding="utf-8")
        if required:
            self.assertIn(old, text)
        p.write_text(text.replace(old, new, 1), encoding="utf-8")

    def produce_citation_outputs(self, pid, kw):
        """What the DATAML/validator/LITREVIEW tooling writes for 15, 15V and 16 (real files, no mocks)."""
        self.manuscript()
        if pid == "15":
            tco.stage_extraction(self.root)
        elif pid == "15V":
            kw.setdefault("report", dict(report_for(pid), **tco.stage_coverage(self.root)))
        elif pid == "16":
            tco.stage_verification(self.root)

    def mutate_live_manuscript(self, pid):
        """Phase 19 applies approved repairs; 20a refreshes Methods. Both legitimately edit live files."""
        self.manuscript()
        if pid == "19":
            self.edit("content/01_introduction.md", "Cells respond to stimuli", "Cells reliably respond to stimuli",
                      required=False)
            self.edit("content/02_results.md", "{cite:p}`Root2018`", "{cite:p}`Smith2020`", required=False)
        elif pid == "20a":
            self.edit("content/Methods.md", "Methods cite", "Methods (refreshed with final counts) cite",
                      required=False)

    def advance(self, pid, **kw):
        """Produce whatever `pid` needs on disk, then advance it with fresh agent ids."""
        spec = pl.PHASES[pid]
        if spec.gate and "body" not in kw and not (self.tmp / "provenance" / spec.gate).exists():
            self.write_gate(pid)
        kw.pop("body", None)
        if pid in ("15", "15V", "16"):
            self.produce_citation_outputs(pid, kw)
        else:
            for out in spec.outputs:
                (self.tmp / "provenance").mkdir(exist_ok=True)
                (self.tmp / "provenance" / out).write_text('[{"t": 1}]')
        if pid in ("19", "20a"):
            self.mutate_live_manuscript(pid)
        if spec.verdict == "report":
            kw.setdefault("report", report_for(pid))
        self.n += 1
        art = "deadbeef1" if pid == "20" else f"vid-{pid}"
        return pl.advance_phase(pid, art, kw.pop("child_ids", [f"frame-{self.n}"]), root=self.root, **kw)

    def run_to(self, last):
        done = pl.load_ledger(self.root).completed
        for pid in pl.PHASE_ORDER[:pl.PHASE_ORDER.index(last) + 1]:
            if pid not in done:
                self.advance(pid)

    def state_path(self):
        return self.tmp / pl.LEDGER


class CanonicalOrder(unittest.TestCase):
    def test_phase_order_matches_the_orchestrator_index(self):
        text = (ROOT / "skills" / "comprev-orchestrator.md").read_text(encoding="utf-8")
        import re
        ids = re.findall(r"^\|\s*(\d+[A-Za-z]?V?)\s*\|[^|]*\|[^|]*\|", text, re.M)
        deduped = [i for n, i in enumerate(ids) if i not in ids[:n]]
        self.assertEqual(tuple(deduped), pl.PHASE_ORDER)
        self.assertEqual(len([p for p in pl.PHASE_ORDER if p.isdigit()]), 21)
        self.assertEqual({int(p.rstrip("aV")) for p in pl.PHASE_ORDER}, set(range(1, 22)))

    def test_gates_are_exactly_those_the_orchestrator_routes(self):
        import re
        text = (ROOT / "skills" / "comprev-orchestrator.md").read_text(encoding="utf-8")
        self.assertEqual(set(re.findall(r"gate_[a-z0-9_]+\.json", text)), set(pl.GATES))

    def test_resolve_phase(self):
        self.assertEqual(pl.resolve_phase(5), "5")
        self.assertEqual(pl.resolve_phase("20a"), "20a")
        for bad in ("5v", " 5", "22", 0, "", None, True, 5.0, "20A", "3_curation", ["5"]):
            with self.assertRaises(pl.InvalidPhase, msg=repr(bad)):
                pl.resolve_phase(bad)

    def test_legacy_aliases_are_explicit_and_land_on_canonical_ids(self):
        for old, new in pl.LEGACY_ALIASES.items():
            self.assertEqual(pl.resolve_phase(old, allow_legacy=True), new)
            self.assertIn(new, pl.PHASES)
        self.assertEqual(len(pl.LEGACY_ALIASES), 9)


class InitAndLoad(Env):
    def test_load_never_creates_or_resets(self):
        with self.assertRaises(pl.LedgerMissing):
            pl.load_ledger(self.root)
        with self.assertRaises(pl.LedgerMissing):
            pl.require_phase("1", root=self.root)
        self.assertFalse(self.state_path().exists())

    def test_init_creates_pending_state_and_refuses_to_overwrite(self):
        led = pl.init_ledger(self.root)
        self.assertEqual((led.revision, led.completed, led.next_phase), (0, [], "1"))
        self.assertEqual(len(led.run_id), 32)
        with self.assertRaises(pl.LedgerExists):
            pl.init_ledger(self.root)
        self.advance("1")
        with self.assertRaises(pl.LedgerExists):
            pl.init_ledger(self.root)
        self.assertEqual(pl.load_ledger(self.root).completed, ["1"])

    def test_init_does_not_replace_a_corrupt_file(self):
        self.state_path().parent.mkdir()
        self.state_path().write_text("{broken")
        with self.assertRaises(pl.LedgerExists):
            pl.init_ledger(self.root)
        self.assertEqual(self.state_path().read_text(), "{broken")

    def test_python_dash_O_does_not_weaken_enforcement(self):
        pl.init_ledger(self.root)
        code = (
            "import sys; sys.path.insert(0, %r); import phase_ledger as p\n"
            "print('debug' if __debug__ else 'optimized')\n"
            "try:\n    p.require_phase('1', root=%r)\nexcept p.PhaseBlocked:\n    print('blocked')\n"
            "try:\n    p.advance_phase('2', 'x', ['a'], root=%r)\nexcept p.PhaseBlocked:\n    print('blocked2')\n"
            "try:\n    p.check_gate('gate_scope.json', root=%r)\nexcept p.PhaseBlocked:\n    print('blocked3')\n"
        ) % (str(ROOT / "scripts"), self.root, self.root, self.root)
        out = subprocess.run([sys.executable, "-O", "-c", code], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.split(), ["optimized", "blocked", "blocked2", "blocked3"])


class SequenceAndPrerequisites(Env):
    def setUp(self):
        super().setUp()
        pl.init_ledger(self.root)

    def test_cannot_skip_a_phase(self):
        with self.assertRaisesRegex(pl.PhaseBlocked, "phase 1 is not complete"):
            self.advance("1V")
        self.advance("1")
        with self.assertRaisesRegex(pl.PhaseBlocked, "phase 1V is not complete"):
            self.advance("2")

    def test_action_completion_does_not_unlock_the_next_action_without_validator(self):
        self.run_to("2")
        with self.assertRaises(pl.PhaseBlocked):
            pl.require_phase("2V", root=self.root)
        with self.assertRaises(pl.PhaseBlocked):
            self.advance("3")
        self.advance("2V")
        self.advance("3")

    def test_missing_gate_blocks_the_validator_phase(self):
        self.run_to("2")
        with self.assertRaisesRegex(pl.GateError, "not found"):
            pl.advance_phase("2V", "vid", ["f9"], root=self.root)
        self.assertEqual(pl.load_ledger(self.root).completed, ["1", "1V", "2"])

    def test_full_pipeline_walks_to_completion(self):
        self.run_to("21")
        led = pl.restart(self.root)
        self.assertEqual(led.completed, list(pl.PHASE_ORDER))
        self.assertIsNone(led.next_phase)
        self.assertEqual(led.revision, len(pl.PHASE_ORDER))

    def test_require_phase_and_check_gate_return_the_pinned_artifact(self):
        self.run_to("6")
        self.assertEqual(pl.check_gate("gate_figure_audit.json", root=self.root), "vid-6")
        self.assertEqual(pl.require_phase("5V", root=self.root), "vid-5V")
        self.assertEqual(pl.check_gate("gate_evidence_curated.json", root=self.root), "vid-5V")
        with self.assertRaises(pl.PhaseBlocked):
            pl.check_gate("gate_sections_drafted.json", root=self.root)

    def test_check_gate_rejects_unknown_names_and_arbitrary_files(self):
        self.run_to("6")
        (self.tmp / "gate_other.json").write_text("{}")
        for name in ("gate_other.json", "../gate_figure_audit.json", "", None):
            with self.assertRaises(pl.GateError):
                pl.check_gate(name, root=self.root)

    def test_file_existence_alone_is_not_a_receipt(self):
        self.run_to("5V")
        self.write_gate("6")  # gate file present, phase 6 never registered
        with self.assertRaises(pl.PhaseBlocked):
            pl.check_gate("gate_figure_audit.json", root=self.root)

    def test_already_complete_phase_cannot_be_advanced_again(self):
        self.advance("1")
        with self.assertRaisesRegex(pl.PhaseBlocked, "already complete"):
            pl.advance_phase("1", "other", ["zz"], root=self.root)

    def test_receipt_inputs_are_validated(self):
        for art in (None, "", "<saved_version_id>", "has space", 5, "x" * 300):
            with self.assertRaises(pl.GateError, msg=repr(art)):
                pl.advance_phase("1", art, ["a"], root=self.root)
        for ids in (None, [], "a", ["a", "a"], [""], [None], ["<id>"]):
            with self.assertRaises(pl.GateError, msg=repr(ids)):
                pl.advance_phase("1", "vid", ids, root=self.root)
        with self.assertRaises(pl.InvalidPhase):
            pl.advance_phase("3_curation", "vid", ["a"], root=self.root)
        self.assertEqual(pl.load_ledger(self.root).completed, [])

    def test_validator_and_critics_must_use_different_frames_than_the_actor(self):
        self.advance("1", child_ids=["shared"])
        with self.assertRaisesRegex(pl.GateError, "different agent frames"):
            self.advance("1V", child_ids=["x", "shared"])
        self.advance("1V", child_ids=["other"])
        self.run_to("7")
        self.advance("7V")
        with self.assertRaisesRegex(pl.GateError, "different agent frames"):
            pl.advance_phase("8", "v8", [f"frame-{self.n - 1}"], root=self.root)  # 7's frame

    def test_phase_20_artifact_must_be_a_commit_sha(self):
        self.run_to("20a")
        with self.assertRaisesRegex(pl.GateError, "commit sha"):
            pl.advance_phase("20", "not-a-sha", ["p"], root=self.root)
        pl.advance_phase("20", "0123abc", ["p"], root=self.root)


class GateContracts(Env):
    """Each gate must exist, parse as an object, and show its documented success."""

    def setUp(self):
        super().setUp()
        pl.init_ledger(self.root)

    def reach(self, pid):
        self.run_to(pl.PHASE_ORDER[pl.PHASE_ORDER.index(pid) - 1])

    def expect_rejected(self, pid, body, pattern=None, raw=None):
        self.reach(pid)
        d = self.tmp / "provenance"
        d.mkdir(exist_ok=True)
        (d / pl.PHASES[pid].gate).write_text(raw if raw is not None else json.dumps(body))
        with self.assertRaisesRegex(pl.GateError, pattern or ""):
            pl.advance_phase(pid, "vid", ["unique-x"], root=self.root)
        self.assertNotIn(pid, pl.load_ledger(self.root).completed)

    def test_every_gate_rejects_the_standard_non_objects_and_empty_object(self):
        for pid in [p for p in pl.PHASE_ORDER if pl.PHASES[p].gate]:
            with self.subTest(pid=pid):
                for raw in ("", "not json", "[]", "null", '"pass"', "42", "{}", '{"a": 1, "a": 2}',
                            '{"gate": NaN}'):
                    self.setUp()
                    self.expect_rejected(pid, None, raw=raw)

    def test_failed_or_ambiguous_validator_verdicts_are_rejected(self):
        base = gate_body("2V")
        for patch in ({"gate": "fail"}, {"gate": "PASS"}, {"gate": True}, {"gate": None}, {"gate_passed": False},
                      {"gate": "pass", "gate_passed": False}, {"gate": "fail", "gate_passed": True},
                      {"phase": 3}, {"phase": "2"}, {"phase": True}, {"verdict": "FAIL_REQUIRES_FIX"},
                      {"failures": ["DOI_RESOLVES"]}, {"checks": {"DOI": "fail"}},
                      {"checks": {"DOI": {"pass": 3, "fail": 1}}}, {"gate": "pass_with_deferred"}):
            self.setUp()
            body = dict(base, **patch)
            self.expect_rejected("2V", body)

    def test_phase_field_is_required_for_validator_gates(self):
        body = gate_body("2V")
        del body["phase"]
        self.expect_rejected("2V", body, "phase")

    def test_gate_without_any_verdict_field_is_rejected(self):
        self.expect_rejected("2V", {"phase": 2, "total_findings": 10}, "no `gate`")

    def test_gate_passed_only_producer_is_accepted(self):
        self.reach("2V")
        self.write_gate("2V", {"phase": 2, "gate_passed": True})
        pl.advance_phase("2V", "vid", ["v"], root=self.root)

    def test_user_signoff_override(self):
        base = {"phase": 2, "gate": "fail", "verdict": "FAIL_REQUIRES_USER_SIGNOFF",
                "failures": ["BIBLIOGRAPHY_FLOOR_HARD"]}
        self.expect_rejected("2V", base, "user_signoff")
        for so in ({}, {"decision": "abort", "note": "n"}, {"decision": "proceed"},
                   {"decision": "proceed", "note": " "}, "proceed"):
            self.setUp()
            self.expect_rejected("2V", dict(base, user_signoff=so))
        self.setUp()
        self.reach("2V")
        self.write_gate("2V", dict(base, user_signoff={"decision": "proceed", "note": "accepted by user"}))
        pl.advance_phase("2V", "vid", ["v"], root=self.root)

    def test_deferred_checks_need_explicit_opt_in_and_evidence(self):
        deferred = {"phase": 2, "gate": "pass_with_deferred", "gate_passed": False,
                    "deferred_checks": ["MYST_BUILD"], "tool_probe": {"node": "command not found"}}
        self.expect_rejected("2V", deferred, "allow_deferred")
        for patch in ({"deferred_checks": []}, {"tool_probe": {}}, {"gate_passed": True}):
            self.setUp()
            self.reach("2V")
            self.write_gate("2V", dict(deferred, **patch))
            with self.assertRaises(pl.GateError):
                pl.advance_phase("2V", "vid", ["v"], root=self.root, allow_deferred=True)
        self.setUp()
        self.reach("2V")
        self.write_gate("2V", deferred)
        pl.advance_phase("2V", "vid", ["v"], root=self.root, allow_deferred=True)
        self.assertTrue(pl.load_ledger(self.root).phases["2V"]["deferred_accepted"])
        pl.require_phase("2V", root=self.root)  # re-verification honours the recorded acceptance

    def test_phase21_check_set_matches_skill_table(self):
        self.assertEqual(len(SKILL_CHECKS), 14)
        self.assertEqual(pl.PHASE21_CHECKS, SKILL_CHECKS)

    def test_phase21_documented_pass_forms(self):
        alt = dict(PHASE21_CONFIRMED, checks=_p21_checks(TIER_B, "BLOCKED_DEPLOY_INACCESSIBLE"))
        for body in (PHASE21_PASS, PHASE21_CONFIRMED, alt):
            self.setUp()
            self.reach("21")
            self.write_gate("21", body)
            pl.advance_phase("21", "vid", ["v"], root=self.root)
            pl.require_phase("21", root=self.root)

    def test_phase21_rejects_failures_and_contradictions(self):
        def mut(base, **kw):
            return dict(copy.deepcopy(base), **kw)

        def with_check(base, name, **kw):
            b = copy.deepcopy(base)
            b["checks"][name] = dict(b["checks"][name], **kw)
            return b

        def without(base, name):
            b = copy.deepcopy(base)
            del b["checks"][name]
            return b

        def with_extra(base):
            b = copy.deepcopy(base)
            b["checks"]["ARBITRARY_PASS"] = {"verdict": "PASS", "tier": "A", "evidence": ["x"]}
            return b

        cases = [
            {"verdict": "PASS"},  # legacy bare form: no gate name / checks
            mut(PHASE21_PASS, gate="pass"),
            mut(PHASE21_PASS, verdict="FAIL_REQUIRES_FIX", fix_list=[{"check": "X"}]),
            mut(PHASE21_PASS, verdict="BLOCKED_DEPLOY_INACCESSIBLE"),
            mut(PHASE21_PASS, verdict="FAIL_REQUIRES_USER_SIGNOFF"),
            mut(PHASE21_PASS, verdict=True),
            mut(PHASE21_PASS, checks={}),
            mut(PHASE21_PASS, checks=[]),
            mut(PHASE21_PASS, checks={"ARBITRARY_PASS": {"verdict": "PASS", "tier": "A", "evidence": ["x"]}}),
            mut(PHASE21_PASS, fix_list=[{"check": "X"}]),
            mut(PHASE21_PASS, failures=["x"]),
            mut(PHASE21_PASS, deploy_accessible=False),
            mut(PHASE21_PASS, deploy_accessible=None),
            {k: v for k, v in PHASE21_PASS.items() if k != "deploy_accessible"},
            mut(PHASE21_PASS, input_source="none"),
            mut(PHASE21_PASS, user_signoff_required=True),
            with_extra(PHASE21_PASS),
            with_check(PHASE21_PASS, "BUILD_GREEN_AT_HEAD", verdict="FAIL"),
            with_check(PHASE21_PASS, "PER_PAGE_HTTP_200", verdict="FAIL"),
            with_check(PHASE21_PASS, "PER_PAGE_HTTP_200", verdict="BLOCKED"),  # PASS with BLOCKED
            with_check(PHASE21_PASS, "BUILD_GREEN_AT_HEAD", verdict="BLOCKED"),
            with_check(PHASE21_PASS, "BUILD_GREEN_AT_HEAD", tier="B"),  # wrong tier
            with_check(PHASE21_PASS, "PER_PAGE_HTTP_200", tier="A"),
            with_check(PHASE21_PASS, "BUILD_GREEN_AT_HEAD", tier=None),
            with_check(PHASE21_PASS, "FIGURE_DROPDOWN_COMPLETE", evidence=[]),
            with_check(PHASE21_PASS, "FIGURE_DROPDOWN_COMPLETE", verdict="N/A"),
            mut(PHASE21_PASS, checks=dict(PHASE21_PASS["checks"], BUILD_GREEN_AT_HEAD="PASS")),
            mut(PHASE21_BLOCKED, deploy_accessible=True),
            mut(PHASE21_BLOCKED, manual_checklist_artifact_id=None),
            mut(PHASE21_BLOCKED, manual_checklist_artifact_id="  "),
            mut(PHASE21_BLOCKED, input_source="none"),
            mut(PHASE21_BLOCKED, checks=_p21_checks()),  # nothing actually blocked
            with_check(PHASE21_BLOCKED, "BUILD_GREEN_AT_HEAD", verdict="BLOCKED", evidence=[]),
            with_check(PHASE21_BLOCKED, "BUILD_GREEN_AT_HEAD", verdict="FAIL"),
            with_check(PHASE21_BLOCKED, "BUILD_GREEN_AT_HEAD", verdict="BLOCKED_DEPLOY_INACCESSIBLE"),
            with_extra(PHASE21_BLOCKED),
        ]
        cases += [mut(PHASE21_BLOCKED, checks=_p21_checks(TIER_B[:1])),  # a Tier B check passing
                  mut(PHASE21_BLOCKED, checks=_p21_checks(TIER_B[1:]))]
        cases += [without(PHASE21_PASS, n) for n in SKILL_CHECKS]
        cases += [without(PHASE21_CONFIRMED, n) for n in SKILL_CHECKS]
        for i, body in enumerate(cases):
            with self.subTest(i):
                self.setUp()
                self.expect_rejected("21", body)

    def test_phase21_blocked_fallback_needs_browser_confirmation(self):
        def so(**kw):
            return dict(PHASE21_BLOCKED, user_signoff=dict(
                {"decision": "proceed", "note": "ok", "browser_confirmed_checks": TIER_B}, **kw))

        cases = [
            PHASE21_BLOCKED,  # no receipt: run is not complete
            dict(PHASE21_BLOCKED, user_signoff="yes"),
            so(decision="abort"), so(note=""), so(note=None),
            so(browser_confirmed_checks=[]), so(browser_confirmed_checks=TIER_B[:1]),
            so(browser_confirmed_checks=TIER_B + ["BUILD_GREEN_AT_HEAD"]),
            so(browser_confirmed_checks=TIER_B + TIER_B[:1]),
            so(browser_confirmed_checks="PER_PAGE_HTTP_200"), so(browser_confirmed_checks=None),
        ]
        for i, body in enumerate(cases):
            with self.subTest(i):
                self.setUp()
                self.expect_rejected("21", body)

    def test_phase21_user_signoff_never_bypasses_failures(self):
        signoff = {"decision": "proceed", "note": "retry anyway", "browser_confirmed_checks": TIER_B}
        failed = copy.deepcopy(PHASE21_PASS)
        failed["checks"]["NO_FRONTMATTER_LEAK"] = {"verdict": "FAIL", "tier": "A", "evidence": ["leak"]}
        cases = [
            {"gate": "phase_21_deploy_polish", "verdict": "FAIL_REQUIRES_USER_SIGNOFF", "user_signoff": signoff},
            dict(failed, verdict="FAIL_REQUIRES_USER_SIGNOFF", user_signoff=signoff),
            dict(failed, user_signoff=signoff),
            dict(PHASE21_PASS, checks={}, verdict="FAIL_REQUIRES_USER_SIGNOFF", user_signoff=signoff),
            dict(PHASE21_PASS, user_signoff_required=True, user_signoff=signoff, checks={}),
        ]
        for i, body in enumerate(cases):
            with self.subTest(i):
                self.setUp()
                self.expect_rejected("21", body)

    def test_contract_fields_per_gate(self):
        cases = {
            "1V": [{"clusters": []}, {"sections": "x"}, {"title": ""}, {"evidence_parameters": {}},
                   {"review_request_path": None}],
            "3V": [{"citation_key_map_vid": None}, {"author_name_table_vid": ""}],
            "4": [{"cross_refs_specific": False}, {"figures_per_section_met": "yes"}, {"scaffold_artifact_id": None}],
            "6": [{"redesign_remaining": 2}, {"redesign_remaining": False}, {"redesign_remaining": "0"},
                  {"critic_frame_ids": []}, {"verdict_counts_post_resolution": {}}],
            "7V": [{"structural_results": {}}, {"section_md_vids_final": {}}, {"section_md_vids_final": {"a": 1}}],
            "8": [{"total_must_fix": 1}, {"total_must_fix": 0.0}, {"total_must_fix": True}, {"coverage_review": None}],
            "10": [{"passes_completed": ["6a_transition"]}, {"filename_mismatches": None},
                   {"passes_completed": sorted(pl.SIX_PASSES) + ["6a_transition"]}],
            "11": [{"novel_citations": ["x"]}, {"novel_citations": None}, {"master_citation_count": 0},
                   {"conclusion_artifact_id": None}],
            "12": [{"total_must_fix": 3}],
            "13": [{"subsection_count": 7}, {"reproducibility_statement_present": False}, {"methods_artifact_id": ""}],
            "14V": [{"structural_results": {}}, {"section_md_vids_final": {}}],
            "20a": [{"forbidden_hits": 1}, {"figure_regenerated": False}, {"frame_count": 0}],
            "20V": [{"structural_results": {}}],
        }
        for pid, patches in cases.items():
            for patch in patches:
                with self.subTest(pid=pid, patch=patch):
                    self.setUp()
                    self.expect_rejected(pid, dict(gate_body(pid), **patch))

    def test_missing_required_key_is_rejected_not_defaulted(self):
        for pid in ("1V", "3V", "4", "6", "8", "10", "11", "12", "13", "20a"):
            for key in [k for k in gate_body(pid) if k not in ("gate", "phase")]:
                with self.subTest(pid=pid, key=key):
                    self.setUp()
                    body = gate_body(pid)
                    del body[key]
                    self.expect_rejected(pid, body)

    def test_gate_in_provenance_wins_over_root_copy(self):
        self.reach("2V")
        self.write_gate("2V", {"phase": 2, "gate": "fail"}, where="")
        self.write_gate("2V")  # provenance/ copy passes
        pl.advance_phase("2V", "vid", ["v"], root=self.root)
        self.assertEqual(pl.load_ledger(self.root).phases["2V"]["gate_file"], "provenance/gate_evidence_compliance.json")

    def test_root_level_gate_is_found_when_provenance_has_none(self):
        self.reach("2V")
        self.write_gate("2V", where="")
        pl.advance_phase("2V", "vid", ["v"], root=self.root)

    def test_symlinked_gate_is_refused(self):
        self.reach("2V")
        real = self.tmp / "real.json"
        real.write_text(json.dumps(gate_body("2V")))
        (self.tmp / "provenance").mkdir(exist_ok=True)
        os.symlink(real, self.tmp / "provenance" / "gate_evidence_compliance.json")
        with self.assertRaisesRegex(pl.GateError, "symlink"):
            pl.advance_phase("2V", "vid", ["v"], root=self.root)

    def test_report_phases_need_a_passing_report_with_the_right_phase(self):
        self.run_to("15")
        for rep in (None, {}, {"gate": "fail", "phase": 15}, {"gate": "pass"}, {"gate": "pass", "phase": 17},
                    {"gate": "pass", "phase": 15, "failures": ["x"]}, "pass", ["pass"]):
            with self.assertRaises(pl.GateError, msg=repr(rep)):
                pl.advance_phase("15V", "vid", ["v"], report=rep, root=self.root)
        bound = dict(report_for("15V"), **tco.stage_coverage(self.root))
        pl.advance_phase("15V", "vid", ["v"], report=bound, root=self.root)
        self.assertEqual(len(pl.load_ledger(self.root).phases["15V"]["report_sha256"]), 64)

    def test_report_19v_must_carry_canonical_artifact_map(self):
        self.run_to("19")
        with self.assertRaises(pl.GateError):
            pl.advance_phase("19V", "vid", ["v"], report={"gate": "pass", "phase": 19}, root=self.root)
        pl.advance_phase("19V", "vid", ["v"], report=report_for("19V"), root=self.root)

    def test_report_is_rejected_where_the_verdict_lives_in_a_file(self):
        self.run_to("2")
        self.write_gate("2V")
        with self.assertRaisesRegex(pl.GateError, "takes no report"):
            pl.advance_phase("2V", "vid", ["v"], report={"gate": "pass"}, root=self.root)

    def test_1v_needs_both_a_scope_file_and_a_passing_report(self):
        self.advance("1")
        self.write_gate("1V")
        with self.assertRaises(pl.GateError):
            pl.advance_phase("1V", "vid", ["v"], root=self.root)
        pl.advance_phase("1V", "vid", ["v"], report=report_for("1V"), root=self.root)

    def test_output_receipts_for_phases_without_gates(self):
        self.run_to("14V")
        self.manuscript()
        with self.assertRaisesRegex(pl.GateError, "citation_snapshot not found"):
            pl.advance_phase("15", "vid", ["a"], root=self.root)
        tco.stage_extraction(self.root)
        (self.tmp / co.EXTRACTION).write_text("")
        with self.assertRaisesRegex(pl.GateError, "empty"):
            pl.advance_phase("15", "vid", ["a"], root=self.root)
        tco.stage_extraction(self.root)
        pl.advance_phase("15", "vid", ["a"], root=self.root)
        recorded = pl.load_ledger(self.root).phases["15"]["outputs"]
        self.assertEqual(sorted(recorded), ["provenance/citation_snapshot", "provenance/citation_triples.json"])
        self.run_to("17")
        self.assertEqual(list(pl.load_ledger(self.root).phases["17"]["outputs"]), ["provenance/fix_requests.json"])


class IntegrityAfterApproval(Env):
    def setUp(self):
        super().setUp()
        pl.init_ledger(self.root)
        self.run_to("6")

    def test_gate_edited_after_approval_is_detected(self):
        g = self.tmp / "provenance" / "gate_figure_audit.json"
        g.write_text(json.dumps(dict(gate_body("6"), note="edited")))
        with self.assertRaisesRegex(pl.GateError, "changed after"):
            pl.check_gate("gate_figure_audit.json", root=self.root)
        with self.assertRaisesRegex(pl.GateError, "changed after"):
            pl.restart(self.root)

    def test_gate_deleted_or_corrupted_after_approval_blocks_everything_after_it(self):
        g = self.tmp / "provenance" / "gate_scaffold_approved.json"
        g.unlink()
        with self.assertRaises(pl.GateError):
            pl.require_phase("6", root=self.root)
        g.write_text("{")
        with self.assertRaises(pl.GateError):
            pl.require_phase("5V", root=self.root)
        with self.assertRaises(pl.GateError):
            pl.restart(self.root)

    def test_gate_that_stopped_passing_is_detected_even_if_hash_were_rewritten(self):
        # simulate a ledger whose recorded hash matches a now-failing gate
        g = self.tmp / "provenance" / "gate_figure_audit.json"
        bad = dict(gate_body("6"), redesign_remaining=3)
        g.write_text(json.dumps(bad))
        st = json.loads(self.state_path().read_text())
        st["phases"]["6"]["gate_sha256"] = pl._sha256(g.read_bytes())
        self.state_path().write_text(json.dumps(st))
        with self.assertRaisesRegex(pl.GateError, "redesign_remaining"):
            pl.check_gate("gate_figure_audit.json", root=self.root)

    def test_output_changed_after_receipt_is_detected(self):
        self.run_to("14V")
        self.advance("15")
        pl.restart(self.root)
        out = self.tmp / "provenance" / "citation_triples.json"
        out.write_text(out.read_text() + " ")
        with self.assertRaisesRegex(pl.GateError, "changed after"):
            pl.restart(self.root)

    def _assert_advance_rejected_unchanged(self, pid, exc=pl.GateError):
        before = self.state_path().read_bytes()
        rev = pl.load_ledger(self.root).revision
        with self.assertRaises(exc):
            self.advance(pid)
        self.assertEqual(self.state_path().read_bytes(), before)
        self.assertEqual(pl.load_ledger(self.root).revision, rev)

    def test_advance_rejects_deleted_immediate_prerequisite_gate(self):
        (self.tmp / "provenance" / "gate_figure_audit.json").unlink()
        self._assert_advance_rejected_unchanged("7")

    def test_advance_rejects_deleted_non_immediate_prerequisite_gate(self):
        # the reproduced bypass: 1 and 1V complete, scope gate deleted, then phase 2
        shutil.rmtree(self.tmp)
        self.tmp.mkdir()
        pl.init_ledger(self.root)
        self.run_to("1V")
        (self.tmp / "provenance" / "gate_scope.json").unlink()
        self._assert_advance_rejected_unchanged("2")
        self.assertEqual(pl.load_ledger(self.root).completed, ["1", "1V"])

    def test_advance_rejects_modified_earlier_gate(self):
        g = self.tmp / "provenance" / "gate_scaffold_approved.json"
        g.write_text(json.dumps(dict(gate_body("4"), note="edited")))
        self._assert_advance_rejected_unchanged("7")

    def test_advance_rejects_modified_immediate_prerequisite_gate(self):
        g = self.tmp / "provenance" / "gate_figure_audit.json"
        g.write_text(json.dumps(dict(gate_body("6"), note="edited")))
        self._assert_advance_rejected_unchanged("7")

    def test_advance_rejects_missing_or_changed_prior_output(self):
        self.run_to("14V")
        self.advance("15")
        bound = dict(report_for("15V"), **tco.stage_coverage(self.root))
        out = self.tmp / "provenance" / "citation_triples.json"
        original = out.read_bytes()
        for tamper in (lambda: out.write_bytes(original + b" "), out.unlink):
            tamper()
            before = self.state_path().read_bytes()
            with self.assertRaises(pl.GateError):
                pl.advance_phase("15V", "vid-15V", ["frame-v"], report=bound, root=self.root)
            self.assertEqual(self.state_path().read_bytes(), before)
            out.write_bytes(original)
        pl.advance_phase("15V", "vid-15V", ["frame-v"], report=bound, root=self.root)

    def test_advance_succeeds_with_intact_prerequisites_and_after_restart(self):
        self.advance("7")
        pl.restart(self.root)
        self.advance("7V")
        self.assertEqual(pl.load_ledger(self.root).completed[-2:], ["7", "7V"])

    def test_reopen_resets_the_phase_and_everything_after_and_is_audited(self):
        self.run_to("8")
        led = pl.reopen_phase("6", "critic found a REDESIGN", root=self.root)
        self.assertEqual(led.completed, ["1", "1V", "2", "2V", "3", "3V", "4", "5", "5V"])
        self.assertEqual(led.state["events"][-1]["reason"], "critic found a REDESIGN")
        with self.assertRaises(pl.PhaseBlocked):
            pl.check_gate("gate_figure_audit.json", root=self.root)
        with self.assertRaisesRegex(pl.LedgerError, "reason"):
            pl.reopen_phase("6", " ", root=self.root)
        self.advance("6")  # same gate file still valid -> can be re-registered
        self.assertEqual(pl.load_ledger(self.root).revision, led.revision + 1)


class StateValidation(Env):
    def setUp(self):
        super().setUp()
        pl.init_ledger(self.root)
        self.run_to("3")

    def corrupt(self, mutate):
        st = json.loads(self.state_path().read_text())
        mutate(st)
        self.state_path().write_text(json.dumps(st))

    def test_malformed_state_is_rejected_and_never_reset(self):
        original = self.state_path().read_text()
        for text in ("", "{", "[]", "null", '{"a": 1}', original[: len(original) // 2], original + "x",
                     original.replace('"revision": 5', '"revision": 5, "revision": 6'), "\xff", "NaN"):
            self.state_path().write_text(text)
            for call in (lambda: pl.load_ledger(self.root), lambda: pl.require_phase("1", root=self.root),
                         lambda: pl.advance_phase("3V", "v", ["z"], root=self.root),
                         lambda: pl.restart(self.root)):
                with self.assertRaises(pl.LedgerCorrupt, msg=repr(text[:20])):
                    call()
            self.assertEqual(self.state_path().read_text(), text)  # untouched

    def test_binary_garbage_is_corrupt_not_a_crash(self):
        self.state_path().write_bytes(b"\xff\xfe\x00\x01")
        with self.assertRaises(pl.LedgerCorrupt):
            pl.load_ledger(self.root)

    def test_each_structural_violation_is_detected(self):
        mutations = {
            "wrong schema": lambda s: s.update(schema="other/1"),
            "extra key": lambda s: s.update(extra=1),
            "missing key": lambda s: s.pop("events"),
            "bad run id": lambda s: s.update(run_id="xyz"),
            "negative revision": lambda s: s.update(revision=-1),
            "bool revision": lambda s: s.update(revision=True),
            "bad timestamp": lambda s: s.update(created_at="yesterday"),
            "unknown phase": lambda s: s["phases"].update({"5_bibliography": {}}),
            "missing phase": lambda s: s["phases"].pop("21"),
            "legacy key": lambda s: s["phases"].update({"phase_1": s["phases"].pop("1")}),
            "bad status": lambda s: s["phases"]["1"].update(status="done"),
            "extra entry key": lambda s: s["phases"]["1"].update(x=1),
            "complete after pending": lambda s: s["phases"]["5"].update(copy.deepcopy(s["phases"]["3"])),
            "pending with data": lambda s: s["phases"]["7"].update(artifact_id="x"),
            "placeholder artifact": lambda s: s["phases"]["1"].update(artifact_id="<saved_version_id>"),
            "no child ids": lambda s: s["phases"]["1"].update(child_ids=[]),
            "dup child ids": lambda s: s["phases"]["1"].update(child_ids=["a", "a"]),
            "bad completion time": lambda s: s["phases"]["1"].update(completed_at=None),
            "gate hash dropped": lambda s: s["phases"]["2V"].update(gate_sha256=None),
            "gate hash malformed": lambda s: s["phases"]["2V"].update(gate_sha256="abc"),
            "gate on gateless phase": lambda s: s["phases"]["1"].update(gate_file="gate_scope.json"),
            "wrong gate file": lambda s: s["phases"]["2V"].update(gate_file="provenance/gate_scope.json"),
            "report hash on file phase": lambda s: s["phases"]["2V"].update(report_sha256="0" * 64),
            "report hash missing": lambda s: s["phases"]["1V"].update(report_sha256=None),
            "deferred not bool": lambda s: s["phases"]["1"].update(deferred_accepted=1),
            "events truncated": lambda s: s["events"].pop(),
            "events reordered": lambda s: s["events"].reverse(),
            "event bad op": lambda s: s["events"][1].update(op="hack"),
            "event bad phase": lambda s: s["events"][1].update(phase="99"),
            "event phase list": lambda s: s["events"][1].update(phase=[]),
            "event phase dict": lambda s: s["events"][1].update(phase={}),
            "event phase number": lambda s: s["events"][1].update(phase=5),
            "event phase list of ids": lambda s: s["events"][1].update(phase=["1"]),
            "event extra key": lambda s: s["events"][1].update(x=1),
        }
        base = self.state_path().read_text()
        for name, mutate in mutations.items():
            with self.subTest(name):
                self.state_path().write_text(base)
                self.corrupt(mutate)
                with self.assertRaises(pl.LedgerCorrupt):
                    pl.load_ledger(self.root)

    def test_restart_preserves_progress_across_processes(self):
        before = pl.load_ledger(self.root)
        code = ("import sys; sys.path.insert(0, %r); import phase_ledger as p; "
                "l = p.restart(%r); print(','.join(l.completed), l.next_phase, l.revision, l.run_id)"
                % (str(ROOT / "scripts"), self.root))
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        done, nxt, rev, run = out.stdout.split()
        self.assertEqual(done.split(","), before.completed)
        self.assertEqual((nxt, int(rev), run), ("3V", before.revision, before.run_id))

    def test_cli_fails_cleanly_on_malformed_event_phase(self):
        base = self.state_path().read_text()
        cli = [sys.executable, str(ROOT / "scripts" / "phase_ledger.py"), "--root", self.root]
        for bad in ([], {}, 5, ["1"]):
            st = json.loads(base)
            st["events"][1]["phase"] = bad
            self.state_path().write_text(json.dumps(st))
            out = subprocess.run(cli + ["verify"], capture_output=True, text=True)
            self.assertEqual(out.returncode, 1, repr(bad))
            self.assertNotIn("Traceback", out.stderr, repr(bad))

    def test_cli_init_status_verify(self):
        other = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, other, True)
        cli = [sys.executable, str(ROOT / "scripts" / "phase_ledger.py"), "--root", str(other)]
        self.assertEqual(subprocess.run(cli + ["status"], capture_output=True).returncode, 1)
        self.assertEqual(subprocess.run(cli + ["init"], capture_output=True).returncode, 0)
        self.assertEqual(subprocess.run(cli + ["init"], capture_output=True).returncode, 1)
        ok = subprocess.run(cli + ["verify"], capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0)
        self.assertIn("next: 1", ok.stdout)
        (other / pl.LEDGER).write_text("{")
        self.assertEqual(subprocess.run(cli + ["verify"], capture_output=True).returncode, 1)


class WritesAndConcurrency(Env):
    def setUp(self):
        super().setUp()
        pl.init_ledger(self.root)

    def test_interrupted_write_leaves_previous_state_and_no_temp_files(self):
        self.advance("1")
        before = self.state_path().read_bytes()
        with mock.patch.object(pl.os, "replace", side_effect=OSError("disk gone")):
            with self.assertRaises(OSError):
                self.advance("1V")
        self.assertEqual(self.state_path().read_bytes(), before)
        self.assertEqual([p.name for p in self.state_path().parent.glob("*.tmp")], [])
        self.assertEqual(pl.load_ledger(self.root).completed, ["1"])
        self.advance("1V")

    def test_crash_mid_write_is_harmless_because_replace_is_atomic(self):
        self.advance("1")
        before = self.state_path().read_bytes()
        stray = self.state_path().parent / "phase_ledger.json.abc123.tmp"
        stray.write_text('{"half": ')  # what a killed writer leaves behind
        self.assertEqual(pl.load_ledger(self.root).completed, ["1"])
        self.advance("1V")
        self.assertNotEqual(self.state_path().read_bytes(), before)
        self.assertEqual(pl.restart(self.root).completed, ["1", "1V"])

    def test_failed_validation_does_not_touch_the_file(self):
        self.advance("1")
        before = self.state_path().read_bytes()
        for _ in range(3):
            with self.assertRaises(pl.LedgerError):
                pl.advance_phase("2", "v", ["x"], root=self.root)  # blocked: 1V missing
        self.assertEqual(self.state_path().read_bytes(), before)

    def test_stale_writer_is_rejected(self):
        a = pl.load_ledger(self.root)
        self.advance("1")  # someone else writes
        self.write_gate("1V")
        with self.assertRaisesRegex(pl.StaleLedger, "reload"):
            pl.advance_phase("1V", "v", ["q"], report=report_for("1V"), root=self.root,
                             expected_revision=a.revision)
        with self.assertRaises(pl.StaleLedger):
            pl.reopen_phase("1", "why", root=self.root, expected_revision=a.revision)
        fresh = pl.load_ledger(self.root)
        pl.advance_phase("1V", "v", ["q"], report=report_for("1V"), root=self.root,
                         expected_revision=fresh.revision)

    def test_concurrent_writers_with_same_revision_only_one_wins(self):
        rev = pl.load_ledger(self.root).revision
        results = []

        def go(n):
            try:
                pl.advance_phase("1", f"vid-{n}", [f"f-{n}"], root=self.root, expected_revision=rev)
                results.append("ok")
            except pl.LedgerError as e:
                results.append(type(e).__name__)

        threads = [threading.Thread(target=go, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count("ok"), 1, results)
        self.assertLessEqual(set(results) - {"ok"}, {"StaleLedger"})
        led = pl.load_ledger(self.root)
        self.assertEqual((led.completed, led.revision), (["1"], 1))

    def test_lock_held_by_another_writer_times_out_without_corrupting(self):
        before = self.state_path().read_bytes()
        with pl._lock(self.root):
            with mock.patch.object(pl, "LOCK_TIMEOUT", 0.1):
                if pl.fcntl is None:
                    self.skipTest("lock-file fallback is not re-entrant to test here")
                with self.assertRaises(pl.LedgerLocked):
                    pl.advance_phase("1", "v", ["f"], root=self.root)
        self.assertEqual(self.state_path().read_bytes(), before)
        self.advance("1")

    def test_every_write_bumps_revision_and_appends_an_event(self):
        self.run_to("2")
        led = pl.load_ledger(self.root)
        self.assertEqual(led.revision, 3)
        self.assertEqual([e["op"] for e in led.state["events"]], ["init", "advance", "advance", "advance"])
        self.assertEqual([e["phase"] for e in led.state["events"]], [None, "1", "1V", "2"])

    def test_state_file_is_stable_deterministic_json(self):
        self.advance("1")
        text = self.state_path().read_text()
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(json.loads(text), pl.load_ledger(self.root).state)


class CitationContract(Env):
    """Phases 15, 15V and 16 bind the ledger to independent rescans of the manuscript sources."""

    def setUp(self):
        super().setUp()
        pl.init_ledger(self.root)
        self.run_to("14V")

    def codes(self, ctx):
        return sorted({d["code"] for d in ctx.exception.diagnostics})

    def unchanged(self, fn, *exc):
        before, rev = self.state_path().read_bytes(), pl.load_ledger(self.root).revision
        with self.assertRaises(exc or (pl.GateError,)) as ctx:
            fn()
        self.assertEqual(self.state_path().read_bytes(), before)
        self.assertEqual(pl.load_ledger(self.root).revision, rev)
        return ctx

    def to_sealed(self, last="16"):
        self.run_to(last)

    # -- Phase 15
    def test_phase15_needs_a_real_snapshot_and_a_matching_extraction(self):
        self.manuscript()
        self.unchanged(lambda: pl.advance_phase("15", "v", ["a"], root=self.root))
        tco.stage_extraction(self.root)
        for junk in ('[{"t": 1}]', "{}", '{"schema_version": "comprev.citation_occurrences/1"}'):
            (self.tmp / co.EXTRACTION).write_text(junk)
            ctx = self.unchanged(lambda: pl.advance_phase("15", "v", ["a"], root=self.root), pl.CitationGateError)
            self.assertTrue(ctx.exception.diagnostics, junk)
        tco.stage_extraction(self.root)
        inv = tco.read_json(self.root, co.EXTRACTION)
        inv["occurrences"].pop()
        inv["occurrence_count"] -= 1
        tco.write_json(self.root, co.EXTRACTION, inv)
        ctx = self.unchanged(lambda: pl.advance_phase("15", "v", ["a"], root=self.root), pl.CitationGateError)
        self.assertEqual(self.codes(ctx), ["missing_occurrence"])

    def test_phase15_rejects_a_manuscript_that_changed_after_the_snapshot(self):
        self.manuscript()
        tco.stage_extraction(self.root)
        self.edit("content/01_introduction.md", "Cells respond", "Neurons respond")
        ctx = self.unchanged(lambda: pl.advance_phase("15", "v", ["a"], root=self.root), pl.CitationGateError)
        self.assertIn("stale_snapshot", self.codes(ctx))
        self.assertIn("source_modified", self.codes(ctx))
        (self.tmp / "content/late.md").write_text("late\n")
        ctx = self.unchanged(lambda: pl.advance_phase("15", "v", ["a"], root=self.root), pl.CitationGateError)
        self.assertIn("source_added", self.codes(ctx))

    def test_snapshot_that_omits_a_source_is_rejected(self):
        self.manuscript()
        tco.stage_extraction(self.root)
        snap = self.tmp / co.SNAPSHOT_DIR
        m = tco.read_json(snap, "manifest.json")
        m["sources"] = [x for x in m["sources"] if x["path"] != "content/references.bib"]
        m["source_count"] = len(m["sources"])
        (snap / "files/content/references.bib").chmod(0o644)
        (snap / "files/content/references.bib").unlink()
        (snap / "manifest.json").chmod(0o644)
        tco.write_json(snap, "manifest.json", m)
        ctx = self.unchanged(lambda: pl.advance_phase("15", "v", ["a"], root=self.root), pl.CitationGateError)
        self.assertTrue({"snapshot_rescan_mismatch", "unresolved_bib_key"} & set(self.codes(ctx)))

    def test_failure_codes_are_machine_readable_on_the_exception_and_in_the_cli(self):
        self.manuscript()
        tco.stage_extraction(self.root)
        self.advance("15")
        self.edit("content/Methods.md", "Methods cite", "Methods differently cite")
        with self.assertRaises(pl.CitationGateError) as ctx:
            pl.restart(self.root)
        self.assertIn("stale_snapshot", [d["code"] for d in ctx.exception.diagnostics])
        out = subprocess.run([sys.executable, str(ROOT / "scripts/phase_ledger.py"), "verify", "--root", self.root],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 1)
        rep = json.loads(out.stdout)
        self.assertEqual((rep["schema_version"], rep["ok"]), (co.DIAG_SCHEMA, False))
        self.assertIn("stale_snapshot", [d["code"] for d in rep["errors"]])

    # -- Phase 15V
    def test_a_synthetic_validator_pass_cannot_bypass_the_coverage_check(self):
        self.advance("15")
        synthetic = {"gate": "pass", "phase": 15}
        ctx = self.unchanged(lambda: pl.advance_phase("15V", "v", ["x"], report=synthetic, root=self.root))
        self.assertIn("required output citation_coverage.json not found", str(ctx.exception))
        bound = dict(synthetic, **tco.stage_coverage(self.root))
        for field, value in (("citation_coverage_sha256", "0" * 64), ("manuscript_revision", "1" * 64),
                             ("occurrence_count", 2)):
            ctx = self.unchanged(lambda: pl.advance_phase("15V", "v", ["x"], report=dict(bound, **{field: value}),
                                                          root=self.root), pl.CitationGateError)
            self.assertEqual(self.codes(ctx), ["report_not_bound"])
        ctx = self.unchanged(lambda: pl.advance_phase("15V", "v", ["x"], report=synthetic, root=self.root),
                             pl.CitationGateError)
        self.assertEqual(self.codes(ctx), ["report_not_bound"])
        led = pl.advance_phase("15V", "v", ["x"], report=bound, root=self.root)
        self.assertEqual(sorted(led.phases["15V"]["outputs"]), ["provenance/citation_coverage.json"])

    def test_forged_or_stale_coverage_is_rejected(self):
        self.advance("15")
        bound = dict(report_for("15V"), **tco.stage_coverage(self.root))
        doc = tco.read_json(self.root, co.COVERAGE)
        doc["occurrence_count"] = 99
        tco.write_json(self.root, co.COVERAGE, doc)
        ctx = self.unchanged(lambda: pl.advance_phase("15V", "v", ["x"], report=bound, root=self.root),
                             pl.CitationGateError)
        self.assertEqual(self.codes(ctx), ["coverage_mismatch"])
        bound = dict(report_for("15V"), **tco.stage_coverage(self.root))
        self.edit("content/Methods.md", "Methods cite", "Methods edited cite")
        ctx = self.unchanged(lambda: pl.advance_phase("15V", "v", ["x"], report=bound, root=self.root),
                             pl.CitationGateError)
        self.assertIn("stale_snapshot", self.codes(ctx))

    # -- Phase 16
    def test_phase16_requires_exact_verdicts_with_real_evidence(self):
        self.run_to("15V")
        self.manuscript()
        env = tco.stage_verification(self.root)
        good = json.loads(json.dumps(env))

        def attempt():
            return self.unchanged(lambda: pl.advance_phase("16", "v", ["lit-1"], root=self.root),
                                  pl.CitationGateError)

        (self.tmp / co.VERIFICATION).unlink()
        self.assertIn("required output citation_verification.json not found",
                      str(self.unchanged(lambda: pl.advance_phase("16", "v", ["lit-1"], root=self.root)).exception))
        broken = json.loads(json.dumps(good))
        broken["verdicts"].pop()
        tco.write_json(self.root, co.VERIFICATION, broken)
        self.assertEqual(self.codes(attempt()), ["missing_verdict"])
        broken = json.loads(json.dumps(good))
        broken["verdicts"][0]["evidence"] = {"status": "unavailable", "citation_key": broken["verdicts"][0]["cite_key"],
                                             "reason": "paywalled"}
        tco.write_json(self.root, co.VERIFICATION, broken)
        self.assertEqual(self.codes(attempt()), ["evidence_not_available"])
        tco.write_json(self.root, co.VERIFICATION, good)
        ev = self.tmp / good["verdicts"][0]["evidence"]["path"]
        ev.write_bytes(ev.read_bytes() + b"!")
        self.assertEqual(self.codes(attempt()), ["evidence_hash_mismatch"])
        ev.write_bytes(ev.read_bytes()[:-1])
        led = pl.advance_phase("16", "v", ["lit-1"], root=self.root)
        self.assertEqual(sorted(led.phases["16"]["outputs"]), ["provenance/citation_verification.json"])

    def test_phase16_admission_requires_the_manuscript_to_still_equal_the_snapshot(self):
        self.run_to("15V")
        tco.stage_verification(self.root)
        self.edit("content/01_introduction.md", "Cells respond", "Neurons respond")
        ctx = self.unchanged(lambda: pl.advance_phase("16", "v", ["lit-1"], root=self.root), pl.CitationGateError)
        self.assertIn("stale_snapshot", self.codes(ctx))

    def test_phase16_frames_are_independent_of_15_and_15V(self):
        self.run_to("15V")
        tco.stage_verification(self.root)
        led = pl.load_ledger(self.root)
        for other in ("15", "15V"):
            reused = led.phases[other]["child_ids"][0]
            self.unchanged(lambda: pl.advance_phase("16", "v", [reused, "lit-9"], root=self.root))
        pl.advance_phase("16", "v", ["lit-9"], root=self.root)

    # -- live freshness before 16 is recorded
    def test_before_16_restart_require_and_advance_enforce_live_freshness(self):
        self.run_to("15V")
        pl.restart(self.root)
        original = (self.tmp / "content/Methods.md").read_text(encoding="utf-8")
        self.edit("content/Methods.md", "Methods cite", "Methods quietly changed cite")
        for fn in (lambda: pl.restart(self.root), lambda: pl.require_phase("15", self.root),
                   lambda: pl.require_phase("15V", self.root), lambda: self.advance("16")):
            with self.assertRaises(pl.CitationGateError) as ctx:
                fn()
            self.assertIn("stale_snapshot", self.codes(ctx))
        (self.tmp / "content/Methods.md").write_text(original, encoding="utf-8")
        pl.restart(self.root)  # restored bytes are accepted again
        self.assertFalse(pl.citation_seal_state(self.root)["sealed"])
        self.assertEqual(pl.citation_seal_state(self.root)["mode"], "live")

    def test_added_deleted_or_modified_sources_and_bibliography_block_live_phases(self):
        self.run_to("15")
        bound = dict(report_for("15V"), **tco.stage_coverage(self.root))
        cases = {
            "new markdown": lambda: (self.tmp / "content/new.md").write_text("new\n"),
            "new bibliography": lambda: (self.tmp / "extra2.bib").write_text("@misc{N, x={1}}\n"),
            "deleted markdown": lambda: (self.tmp / "content/02_results.md").unlink(),
            "modified bibliography": lambda: self.edit("extra.bib", "root-level", "Root-level"),
        }
        saved = {p: p.read_bytes() for p in (self.tmp / "content").iterdir()}
        saved[self.tmp / "extra.bib"] = (self.tmp / "extra.bib").read_bytes()
        for name, mutate in cases.items():
            with self.subTest(name):
                mutate()
                with self.assertRaises(pl.GateError):
                    pl.restart(self.root)
                with self.assertRaises(pl.GateError):
                    pl.advance_phase("15V", "vid-15V", ["frame-v"], report=bound, root=self.root)
                for extra in (self.tmp / "content/new.md", self.tmp / "extra2.bib"):
                    extra.unlink() if extra.exists() else None
                for path, data in saved.items():
                    path.write_bytes(data)
                pl.restart(self.root)

    def test_snapshot_extraction_and_coverage_tamper_is_detected_on_restart(self):
        self.run_to("15V")
        snap = self.tmp / co.SNAPSHOT_DIR
        cases = {
            "snapshot file": snap / "files/content/Methods.md",
            "snapshot manifest": snap / "manifest.json",
            "extraction": self.tmp / co.EXTRACTION,
            "coverage": self.tmp / co.COVERAGE,
        }
        for name, path in cases.items():
            with self.subTest(name):
                original = path.read_bytes()
                path.chmod(0o644)
                path.write_bytes(original + b" ")
                with self.assertRaisesRegex(pl.GateError, "changed after"):
                    pl.restart(self.root)
                path.write_bytes(original)
                pl.restart(self.root)
        extra = snap / "files/content/planted.md"
        extra.write_text("planted\n")
        with self.assertRaisesRegex(pl.GateError, "changed after"):
            pl.restart(self.root)

    def test_hash_pinned_tamper_cannot_be_hidden_by_rewriting_the_ledger_hash(self):
        # even if the recorded output digest were rewritten, the contract re-derives everything
        self.run_to("15V")
        inv = tco.read_json(self.root, co.EXTRACTION)
        inv["occurrences"].pop()
        inv["occurrence_count"] -= 1
        tco.write_json(self.root, co.EXTRACTION, inv)
        st = json.loads(self.state_path().read_text())
        st["phases"]["15"]["outputs"]["provenance/citation_triples.json"] = pl._hash_path(self.tmp / co.EXTRACTION)
        self.state_path().write_text(json.dumps(st))
        with self.assertRaises(pl.CitationGateError) as ctx:
            pl.restart(self.root)
        self.assertIn("missing_occurrence", self.codes(ctx))

    # -- sealing
    def test_a_failed_16_does_not_seal_the_revision(self):
        self.run_to("15V")
        tco.stage_verification(self.root)
        self.edit("content/Methods.md", "Methods cite", "Methods drifted cite")
        self.unchanged(lambda: pl.advance_phase("16", "v", ["lit-1"], root=self.root), pl.CitationGateError)
        self.assertFalse(pl.citation_seal_state(self.root)["sealed"])
        with self.assertRaises(pl.CitationGateError):
            pl.restart(self.root)  # still live: nothing was recorded
        self.assertEqual(pl.load_ledger(self.root).next_phase, "16")

    def test_only_a_recorded_16_seals_and_then_live_edits_are_allowed(self):
        self.run_to("16")
        state = pl.citation_seal_state(self.root)
        self.assertEqual((state["sealed"], state["mode"]), (True, "historical"))
        self.assertEqual(len(state["snapshot_output"]), 64)
        original = (self.tmp / "content/01_introduction.md").read_bytes()
        self.edit("content/01_introduction.md", "Cells respond", "Neurons respond")
        pl.restart(self.root)
        self.assertEqual(pl.require_phase("16", self.root), "vid-16")
        self.assertEqual(pl.require_phase("15", self.root), "vid-15")
        self.advance("17")
        (self.tmp / "content/01_introduction.md").write_bytes(original)
        pl.restart(self.root)

    def test_a_planted_seal_file_does_not_seal_anything(self):
        self.run_to("15V")
        (self.tmp / "provenance/citation_sealed.json").write_text('{"sealed": true}')
        self.edit("content/Methods.md", "Methods cite", "Methods drifted cite")
        with self.assertRaises(pl.CitationGateError):
            pl.restart(self.root)

    def test_sealed_receipts_still_catch_snapshot_extraction_verdict_and_evidence_tamper(self):
        self.run_to("16")
        self.edit("content/01_introduction.md", "Cells respond", "Neurons respond")  # legitimate repair
        pl.restart(self.root)
        cases = {
            "snapshot": self.tmp / co.SNAPSHOT_DIR / "files/content/references.bib",
            "extraction": self.tmp / co.EXTRACTION,
            "coverage": self.tmp / co.COVERAGE,
            "verification": self.tmp / co.VERIFICATION,
        }
        for name, path in cases.items():
            with self.subTest(name):
                original = path.read_bytes()
                path.chmod(0o644)
                path.write_bytes(original + b" ")
                with self.assertRaisesRegex(pl.GateError, "changed after"):
                    pl.restart(self.root)
                with self.assertRaises(pl.GateError):
                    pl.require_phase("16", self.root)
                path.write_bytes(original)
        ev = self.tmp / co.EVIDENCE_DIR / "Smith2020.fulltext.txt"
        original = ev.read_bytes()
        ev.write_bytes(b"X" + original[1:])
        with self.assertRaises(pl.CitationGateError) as ctx:
            pl.restart(self.root)
        self.assertEqual(self.codes(ctx), ["evidence_hash_mismatch"])
        ev.write_bytes(original)
        pl.restart(self.root)

    def test_pinned_extraction_edit_with_rehashed_ledger_is_still_rejected_when_sealed(self):
        self.run_to("16")
        inv = tco.read_json(self.root, co.EXTRACTION)
        inv["occurrences"][0]["citation_key"] = "Lee2019"
        tco.write_json(self.root, co.EXTRACTION, inv)
        st = json.loads(self.state_path().read_text())
        st["phases"]["15"]["outputs"]["provenance/citation_triples.json"] = pl._hash_path(self.tmp / co.EXTRACTION)
        self.state_path().write_text(json.dumps(st))
        with self.assertRaises(pl.CitationGateError):
            pl.restart(self.root)

    # -- reopen / re-seal
    def test_reopen_unseals_and_live_freshness_applies_again(self):
        self.run_to("17")
        self.edit("content/01_introduction.md", "Cells respond", "Neurons respond")
        pl.restart(self.root)  # sealed: fine
        led = pl.reopen_phase("16", "verdict redo", root=self.root)
        self.assertEqual(led.completed[-1], "15V")
        self.assertFalse(pl.citation_seal_state(self.root)["sealed"])
        with self.assertRaises(pl.CitationGateError):
            pl.restart(self.root)  # live again, and the live manuscript drifted from the Phase 15 snapshot

    def test_reopening_15_clears_downstream_and_the_run_can_reseal_a_new_revision(self):
        self.run_to("19")
        self.assertIn("Cells reliably respond", (self.tmp / "content/01_introduction.md").read_text())
        led = pl.reopen_phase("15", "re-extract after repairs", root=self.root)
        self.assertEqual(led.completed[-1], "14V")
        for pid in ("15", "15V", "16", "17", "17V", "18", "19", "19V"):
            self.assertEqual(led.phases[pid]["status"], "pending", pid)
        old = tco.read_json(self.root, co.EXTRACTION)["manuscript_revision"]
        self.advance("15")  # re-snapshot supersedes the old directory
        self.assertTrue((self.tmp / "provenance/citation_snapshot.superseded-1").is_dir())
        self.assertNotEqual(tco.read_json(self.root, co.EXTRACTION)["manuscript_revision"], old)
        self.run_to("16")
        self.assertTrue(pl.citation_seal_state(self.root)["sealed"])
        self.run_to("21")
        self.assertEqual(pl.restart(self.root).completed, list(pl.PHASE_ORDER))

    def test_without_a_recorded_16_stale_state_cannot_be_resealed_without_reopening_15(self):
        self.run_to("15V")
        self.edit("content/Methods.md", "Methods cite", "Methods drifted cite")
        self.unchanged(lambda: self.advance("16"), pl.CitationGateError, co.CitationError)
        pl.reopen_phase("15", "manuscript changed", root=self.root)
        self.run_to("16")
        pl.restart(self.root)

    # -- realistic pipeline
    def test_realistic_pipeline_phase19_repairs_and_20a_methods_refresh_through_21(self):
        self.run_to("16")
        before = tco.read_json(self.root, co.EXTRACTION)["manuscript_revision"]
        self.run_to("19")  # advance("19") applies an approved repair to live markdown, including a cite key
        self.assertNotEqual(co.scan_sources(self.root).revision, before)
        pl.restart(self.root)
        self.run_to("20a")  # refreshes Methods.md
        self.run_to("21")
        led = pl.restart(self.root)
        self.assertEqual(led.completed, list(pl.PHASE_ORDER))
        # the pinned (historical) revision is still the one that was verified ...
        self.assertEqual(tco.read_json(self.root, co.EXTRACTION)["manuscript_revision"], before)
        self.assertTrue(co.audit_historical(self.root)["ok"])
        # ... and nothing claims it verifies the edited manuscript: the live public validators fail
        self.assertFalse(co.validate_occurrences(self.root)["ok"])
        self.assertFalse(co.validate_verdicts(self.root)["ok"])
        # a restart in another process (as a new session would) agrees
        code = ("import sys; sys.path.insert(0, %r); import phase_ledger as p; print(len(p.restart(%r).completed))"
                % (str(ROOT / "scripts"), self.root))
        out = subprocess.run([sys.executable, "-O", "-c", code], capture_output=True, text=True)
        self.assertEqual((out.returncode, out.stdout.strip()), (0, str(len(pl.PHASE_ORDER))), out.stderr)

    def test_optimized_interpreter_enforces_the_same_citation_contract(self):
        self.run_to("15V")
        self.edit("content/Methods.md", "Methods cite", "Methods drifted cite")
        code = ("import sys; sys.path.insert(0, %r); import phase_ledger as p\n"
                "try:\n    p.restart(%r)\nexcept p.CitationGateError as e:\n"
                "    print(sorted({d['code'] for d in e.diagnostics}))\n    sys.exit(3)\n"
                % (str(ROOT / "scripts"), self.root))
        out = subprocess.run([sys.executable, "-O", "-c", code], capture_output=True, text=True)
        self.assertEqual(out.returncode, 3, out.stderr)
        self.assertIn("stale_snapshot", out.stdout)

    # -- old state and shape
    def test_old_receipts_without_the_citation_contract_fail_closed_with_guidance(self):
        self.run_to("16")
        base = json.loads(self.state_path().read_text())
        for pid, outputs in (("15", {"provenance/citation_triples.json": "0" * 64}), ("15V", {}), ("16", {})):
            with self.subTest(pid):
                st = json.loads(json.dumps(base))
                st["phases"][pid]["outputs"] = outputs
                self.state_path().write_text(json.dumps(st))
                raw = self.state_path().read_bytes()
                with self.assertRaisesRegex(pl.LedgerCorrupt, "predates the citation contract"):
                    pl.load_ledger(self.root)
                with self.assertRaises(pl.LedgerCorrupt):
                    pl.restart(self.root)
                self.assertEqual(self.state_path().read_bytes(), raw)  # never reset silently
        self.state_path().write_text(json.dumps(base))

    def test_specs_declare_the_contracts(self):
        self.assertEqual({p: pl.PHASES[p].contract for p in pl.CITATION_PHASES},
                         {"15": "extract", "15V": "coverage", "16": "verdicts"})
        self.assertEqual(pl.PHASES["15"].outputs, ("citation_snapshot", "citation_triples.json"))
        self.assertEqual(pl.PHASES["15V"].outputs, ("citation_coverage.json",))
        self.assertEqual(pl.PHASES["16"].outputs, ("citation_verification.json",))

    def test_zero_citation_manuscript_completes_with_explicit_empty_inventory(self):
        (self.tmp / "content").mkdir()
        (self.tmp / "content/a.md").write_text("# Nothing is cited here\n")
        self.advance("15")
        self.assertEqual(tco.read_json(self.root, co.EXTRACTION)["occurrence_count"], 0)
        bound = dict(report_for("15V"), **tco.stage_coverage(self.root))
        pl.advance_phase("15V", "vid-15V", ["frame-v"], report=bound, root=self.root)
        tco.write_json(self.root, co.VERIFICATION, co.assemble_verification(self.root, []))
        pl.advance_phase("16", "vid-16", ["frame-l"], root=self.root)
        self.assertEqual(pl.restart(self.root).next_phase, "17")


class DocumentedSkillUsage(unittest.TestCase):
    def test_every_skill_call_uses_canonical_ids_known_gates_and_an_import(self):
        import re
        fence = re.compile(r"^```python[^\n]*\n(.*?)^```", re.M | re.S)
        calls = 0
        for path in sorted((ROOT / "skills").glob("*.md")):
            for block in fence.findall(path.read_text(encoding="utf-8")):
                for fn, arg in re.findall(r"\b(require_phase|advance_phase|check_gate)\(\s*['\"]([^'\"]+)['\"]", block):
                    calls += 1
                    if fn == "check_gate":
                        self.assertIn(arg, pl.GATES, path.name)
                    else:
                        self.assertIn(arg, pl.PHASES, f"{path.name}: {fn}({arg!r})")
                    self.assertIn("from phase_ledger import", block, f"{path.name} calls {fn} without importing it")
        self.assertGreaterEqual(calls, 8)

    def test_orchestrator_has_no_assert_based_ledger_or_undefined_helpers(self):
        text = (ROOT / "skills" / "comprev-orchestrator.md").read_text(encoding="utf-8")
        self.assertNotIn("phase_ledger = {", text)
        self.assertNotIn("assert phase_ledger", text)
        self.assertNotIn("def require_phase", text)
        self.assertIn("from phase_ledger import", text)


if __name__ == "__main__":
    unittest.main()
