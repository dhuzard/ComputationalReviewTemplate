"""Offline tests for the Python helpers the skills document.

The skill code blocks are extracted from the markdown and executed, so the
documented code (not a copy of its algorithm) is what is tested.
"""
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import frames_ledger  # noqa: E402
import skill_catalog  # noqa: E402

SKILLS = sorted((ROOT / "skills").glob("*.md"))
FENCE = re.compile(r"^```(\w*)[^\n]*\n(.*?)^```", re.M | re.S)


def python_blocks(path):
    return [m.group(2) for m in FENCE.finditer(path.read_text(encoding="utf-8")) if m.group(1) == "python"]


def block_containing(path, needle):
    found = [b for b in python_blocks(path) if needle in b]
    assert len(found) == 1, f"{path.name}: {len(found)} python blocks contain {needle!r}"
    return found[0]


class Cwd:
    def __init__(self, path): self.path = path
    def __enter__(self): self.old = os.getcwd(); os.chdir(self.path)
    def __exit__(self, *a): os.chdir(self.old)


class PipelineSkillRoles(unittest.TestCase):
    def test_every_skill_has_a_meaningful_role(self):
        rows = skill_catalog.skill_rows(ROOT / "skills")
        self.assertEqual(len(rows), 21)
        for name, role in rows:
            self.assertGreater(len(role), 8, name)
            self.assertNotIn("---", role, name)
            self.assertNotIn("Expert Review Pipeline", role, name)
            self.assertNotIn("|", role, name)

    def test_documented_methods_snippet_builds_the_table(self):
        code = block_containing(ROOT / "skills" / "comprev-dataml-phases.md", "methods_table")
        scope = {}
        with Cwd(ROOT):
            exec(code, scope)
        lines = scope["table"].splitlines()
        self.assertEqual(lines[:2], ["| Skill | Role |", "|---|---|"])
        self.assertEqual(len(lines), 2 + 21)
        self.assertNotIn("| `comprev-critic.md` | --- |", lines)
        self.assertTrue(all(not l.endswith("| --- |") for l in lines))
        self.assertIn("| `comprev-critic.md` | Section Critic Protocol |", lines)

    def test_role_parser_variants(self):
        role = skill_catalog.skill_role
        self.assertEqual(role('---\nname: x\ndescription: "Expert Review Pipeline v30 (T): Do a | thing. More."\n---\n# H\n'), "Do a | thing")
        self.assertEqual(role("---\nname: x\ndescription: >\n  Folded\n  text here. Next.\n---\n"), "Folded text here")
        self.assertEqual(role("---\nname: x\n---\n```\n# fenced\n```\n# Real heading\n"), "Real heading")
        self.assertEqual(role("# Plain heading\nbody"), "Plain heading")
        self.assertEqual(role("no heading", "stem"), "stem")
        self.assertEqual(skill_catalog.methods_table([("a.md", "x | y")]).splitlines()[-1], "| `a.md` | x \\| y |")


FRAMES = {"frames": [
    {"agent_name": "DATAML", "status": "complete", "created_at": "2026-01-01T00:00:00Z"},
    {"agent_name": "LITREVIEW", "status": "complete", "created_at": "2026-01-01T02:30:00Z"},
    {"agent_name": "LITREVIEW", "status": "failed", "created_at": "2026-01-01T01:00:00Z"},
]}


class FrameLedgerBridge(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "provenance").mkdir()
        (self.tmp / frames_ledger.LEDGER).write_text(json.dumps(FRAMES))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_load_and_summarize(self):
        with Cwd(self.tmp):
            frames = frames_ledger.load_frames()
        s = frames_ledger.summarize(frames)
        self.assertEqual(s["total"], 3)
        self.assertEqual(dict(s["agent_counts"]), {"DATAML": 1, "LITREVIEW": 2})
        self.assertEqual(dict(s["status_counts"]), {"complete": 2, "failed": 1})
        self.assertAlmostEqual(s["elapsed_h"], 2.5)
        self.assertAlmostEqual(frames_ledger.summarize(frames, "2026-01-01T05:00:00Z")["elapsed_h"], 5.0)

    def test_missing_or_malformed_ledger_fails_loudly(self):
        with self.assertRaisesRegex(FileNotFoundError, "serialise"):
            frames_ledger.load_frames(self.tmp / "nope.json")
        bad = self.tmp / "bad.json"
        for payload in ('[]', '{"frames": 3}', '{}'):
            bad.write_text(payload)
            with self.assertRaises(ValueError):
                frames_ledger.load_frames(bad)
        with self.assertRaises(ValueError):
            frames_ledger.summarize([])

    def test_documented_phase_13_snippet_reads_the_bridge_file(self):
        code = block_containing(ROOT / "skills" / "comprev-dataml-phases.md", "snapshot_phase")
        scope = {
            "json": json, "MODEL_ID": "test-model", "PROJECT_ID": "p",
            "datetime": __import__("datetime").datetime, "timezone": __import__("datetime").timezone,
            "phase_ledger": {"1": {"status": "complete"}}, "first_frame_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        }
        (self.tmp / "provenance" / "pipeline_version.json").write_text('{"pipeline_version": 30}')
        with Cwd(self.tmp):
            exec(code, scope)
        self.assertEqual(scope["methods_data"]["total_frames"], 3)

    def test_documented_phase_20a_snippet_reads_the_bridge_file(self):
        code = block_containing(ROOT / "skills" / "comprev-dataml-phases.md", "m6_pattern")
        # Run only the ledger-consuming head; the rest renders Methods.md from gate files.
        head = code[:code.index("methods = pathlib.Path('content/Methods.md')")]
        scope = {"MODEL_ID": "test-model"}
        with Cwd(self.tmp):
            exec(head, scope)
        self.assertEqual(sum(scope["agent_counts"].values()), 3)
        self.assertAlmostEqual(scope["elapsed_h"], 2.5)


class NoHostFramesInPython(unittest.TestCase):
    def test_python_blocks_never_call_host_frames(self):
        # Executable Python cannot call the repl-only host.frames; prose may mention it.
        offenders = [f"{p.name}: {line.strip()}" for p in SKILLS for b in python_blocks(p)
                     for line in b.splitlines() if re.search(r"\bhost\.frames\s*\(", line)]
        self.assertEqual(offenders, [])

    def test_bridge_is_documented_where_python_consumes_it(self):
        orch = (ROOT / "skills" / "comprev-orchestrator.md").read_text(encoding="utf-8")
        self.assertIn("Frame ledger bridge", orch)
        self.assertIn("provenance/frames_ledger.json", orch)
        self.assertIn("frames_ledger", (ROOT / "skills" / "comprev-dataml-phases.md").read_text(encoding="utf-8"))


class ValidatorFrameCountBridge(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "provenance").mkdir()
        (self.tmp / frames_ledger.LEDGER).write_text(json.dumps(FRAMES))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_methods_ledger_fresh_frame_count_check(self):
        validator = (ROOT / "skills" / "comprev-myst-validator.md").read_text(encoding="utf-8")
        check = validator.split("**Pipeline Execution frame count", 1)[1].split("    3.", 1)[0]
        self.assertIn("provenance/frames_ledger.json", check)
        self.assertIn("len(load_frames())", check)
        self.assertNotIn("host.frames(", check)
        preserve = (ROOT / "skills" / "comprev-dataml-phases.md").read_text(encoding="utf-8")
        preserve = preserve.split("4. **Preserve** all scaffold-owned", 1)[1].split("5. **Verify file signatures**", 1)[0]
        for helper in ("scripts/frames_ledger.py", "scripts/skill_catalog.py"):
            self.assertIn(f"`{helper}`", preserve)
        with Cwd(self.tmp):
            frames = frames_ledger.load_frames()
        frame_count = len(frames)
        self.assertEqual(frame_count, 3)
        for tolerance in [-1, 0, 1]:
            declared_count = frame_count + tolerance
            self.assertLessEqual(abs(declared_count - frame_count), 1,
                f"validator tolerance check: {declared_count} frames declared, {frame_count} in ledger, ±1 range is {frame_count-1} to {frame_count+1}")


if __name__ == "__main__":
    unittest.main()
