"""Tests for scripts/citation_occurrences.py: deterministic extraction, exact coverage, verdicts.

Everything runs against real source/bibliography/evidence files (test/fixtures/citations) and
real rescans; nothing is mocked and no flag bypasses validation.
"""
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import citation_occurrences as co  # noqa: E402

FIX = ROOT / "test" / "fixtures" / "citations"
HEX = "0" * 64


# ------------------------------------------------------------------- helpers
def make_manuscript(dst):
    """Copy the fixture manuscript (content/, provenance/review_request.md, extra.bib) into dst."""
    shutil.copytree(str(FIX / "manuscript"), str(dst), dirs_exist_ok=True)


def read_json(root, rel):
    return json.loads(Path(root, rel).read_text(encoding="utf-8"))


def write_json(root, rel, obj):
    p = Path(root, rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def codes(diags):
    return sorted({d["code"] for d in diags})


def stage_extraction(root):
    """What Phase 15 does: snapshot, then extraction from an independent scan."""
    co.create_snapshot(root, supersede=True)
    scan = co.scan_sources(root)
    co.write_atomic(Path(root, co.EXTRACTION), co.json_text(scan.inventory()).encode("utf-8"))
    return scan


def stage_coverage(root):
    """What the 15V validator does: persist the coverage file; returns the report binding to it."""
    doc, sha = co.write_coverage(root)
    return {"gate": "pass", "phase": 15, "citation_coverage_sha256": sha,
            "manuscript_revision": doc["manuscript_revision"], "occurrence_count": doc["occurrence_count"]}


EVIDENCE_FOR = {"Smith2020": ("Smith2020.fulltext.txt", "fulltext"),
                "Jones2021": ("Jones2021.abstract.txt", "abstract_only"),
                "Müller2022": ("Mueller2022.fulltext.txt", "fulltext"),
                "Lee2019": ("Lee2019.fulltext.txt", "fulltext"),
                "Root2018": ("Root2018.fulltext.txt", "fulltext")}


def install_evidence(root):
    dst = Path(root, co.EVIDENCE_DIR)
    dst.mkdir(parents=True, exist_ok=True)
    for f in (FIX / "evidence").iterdir():
        shutil.copyfile(str(f), str(dst / f.name))


def verdict_for(root, occ, **over):
    name, depth = EVIDENCE_FOR[occ["citation_key"]]
    rel = "%s/%s" % (co.EVIDENCE_DIR, name)
    data = Path(root, rel).read_bytes()
    end = data.index(b"\n")
    ev = {"status": "available", "depth": depth, "citation_key": occ["citation_key"], "path": rel,
          "sha256": co.sha256_hex(data), "passage_span": [0, end]}
    if depth == "abstract_only":
        ev["fulltext_unavailable_reason"] = "publisher paywall; PMC and Europe PMC had no OA copy"
    v = {"occurrence_id": occ["occurrence_id"], "manuscript_revision": occ["manuscript_revision"],
         "cite_key": occ["citation_key"], "category": "VERIFIED", "failed_at_step": None,
         "verification_depth": depth, "supporting_passage": data[:end].decode("utf-8"), "details": None,
         "evidence": ev, "scientific_checks_attestation": {
             "protocol": co.PROTOCOL, "steps_completed": [1, 2, 3, 4, 5], "attested_by": "frame-litreview-1"}}
    v.update(over)
    return v


def stage_verification(root, scan=None):
    install_evidence(root)
    scan = scan or co.scan_sources(root)
    env = co.assemble_verification(scan, [verdict_for(root, o) for o in scan.occurrences])
    write_json(root, co.VERIFICATION, env)
    return env


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.root = self.tmp / "repo"
        self.root.mkdir()

    def manuscript(self):
        make_manuscript(self.root)
        return self.root

    def text(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))
        return p


# ------------------------------------------------------------------- scanner
def found(text):
    return [(o["directive_kind"], o["key_index"], o["citation_key"]) for o in co.scan_markdown(text)]


def failure(text):
    try:
        co.scan_markdown(text)
    except co._ScanFail as e:
        return e.code
    return None


class ScannerPositives(unittest.TestCase):
    def test_roles_keys_and_repeats(self):
        self.assertEqual(found("a {cite:p}`A` b {cite:t}`B`"), [("cite:p", 0, "A"), ("cite:t", 0, "B")])
        self.assertEqual(found("{cite:p}`A, B ,C`"), [("cite:p", 0, "A"), ("cite:p", 1, "B"), ("cite:p", 2, "C")])
        self.assertEqual(found("{cite:t}`A,A`"), [("cite:t", 0, "A"), ("cite:t", 1, "A")])
        self.assertEqual(len(found("{cite:p}`A` {cite:p}`A`")), 2)

    def test_byte_spans_are_utf8_half_open_and_cover_the_whole_role(self):
        text = "é—ü {cite:p}`Müller, K` ø {cite:t}`Z`"
        raw = text.encode("utf-8")
        occs = co.scan_markdown(text)
        self.assertEqual(len(occs), 3)
        for o in occs[:2]:
            self.assertEqual(raw[slice(*o["byte_span"])].decode("utf-8"), "{cite:p}`Müller, K`")
        self.assertEqual(raw[slice(*occs[2]["byte_span"])].decode("utf-8"), "{cite:t}`Z`")
        self.assertEqual(occs[0]["byte_span"][0], len("é—ü ".encode("utf-8")))
        self.assertNotEqual(occs[0]["byte_span"][0], len("é—ü "))

    def test_crlf_and_bare_cr_offsets_and_lines(self):
        for nl in ("\r\n", "\r", "\n"):
            with self.subTest(newline=repr(nl)):
                text = nl.join(["# ü", "", "x {cite:p}`A`", "y {cite:t}`B`", ""])
                raw = text.encode("utf-8")
                occs = co.scan_markdown(text)
                self.assertEqual([o["line_range"] for o in occs], [[3, 3], [4, 4]])
                self.assertEqual([raw[slice(*o["byte_span"])] for o in occs],
                                 [b"{cite:p}`A`", b"{cite:t}`B`"])

    def test_role_spanning_lines_has_a_line_range(self):
        occs = co.scan_markdown("intro\nsee {cite:p}`A,\n B` end\n")
        self.assertEqual([(o["citation_key"], o["line_range"]) for o in occs], [("A", [2, 3]), ("B", [2, 3])])
        self.assertEqual(occs[0]["byte_span"], occs[1]["byte_span"])
        self.assertEqual(occs[0]["context"], "see {cite:p}`A,\n B` end")

    def test_prose_constructs_are_scanned(self):
        text = "\n".join([
            "# Heading {cite:p}`H`", "", "- item {cite:p}`L`", "", "  continuation {cite:p}`LC`", "",
            "> quote {cite:p}`Q`", "", "| a | {cite:p}`T` |", "|---|---|", "",
            "[label {cite:t}`LL`](https://example.org/x_y)", "",
            "text <span title=\"a\">{cite:p}`Tag`</span>", "", "1. first {cite:p}`N`"])
        self.assertEqual([k for _, _, k in found(text)], ["H", "L", "LC", "Q", "T", "LL", "Tag", "N"])

    def test_prose_directives_nested_and_titles(self):
        text = "\n".join([
            ":::::{note}", "outer {cite:p}`O`", "::::{tip}", "inner {cite:p}`I`", "```python", "{cite:p}`CodeIn`",
            "```", "::::", "back {cite:p}`B`", ":::::", "",
            ":::{admonition} Title {cite:t}`Ti`", "body {cite:p}`Bd`", ":::", "",
            "```{note}", "backtick directive {cite:p}`Bt`", "```", "",
            ":::{figure} img_a.png", ":label: f", ":alt: alt text", "", "caption {cite:p}`Cap`", ":::"])
        self.assertEqual([k for _, _, k in found(text)], ["O", "I", "B", "Ti", "Bd", "Bt", "Cap"])

    def test_four_space_list_continuation_is_prose_not_code(self):
        text = "1. item\n\n    continuation {cite:p}`Cont`\n\n- x\n\n  - y\n\n        deep code {cite:p}`Deep`\n"
        self.assertEqual([k for _, _, k in found(text)], ["Cont"])

    def test_known_prose_directive_with_prf_prefix_and_unknown_without_citations(self):
        self.assertEqual([k for _, _, k in found(":::{prf:theorem} T\nbody {cite:p}`P`\n:::\n")], ["P"])
        self.assertEqual(found(":::{weird-widget}\nplain text only\n:::\n"), [])


class ScannerNegatives(unittest.TestCase):
    def none(self, text):
        self.assertEqual(found(text), [], text)

    def test_code_fences_of_every_kind(self):
        self.none("```python\n{cite:p}`A`\n```\n")
        self.none("~~~\n{cite:p}`A`\n~~~\n")
        self.none("````\n```\n{cite:p}`A`\n```\n````\n")
        self.none("```{code-cell} python\n{cite:p}`A`\n```\n")
        self.none(":::{code-block} text\n{cite:p}`A`\n:::\n")
        self.none("```{math}\n{cite:p}`A`\n```\n")
        self.none("> ```\n> {cite:p}`A`\n> ```\n")
        self.none("- item\n\n  ```\n  {cite:p}`A`\n  ```\n")

    def test_inline_code_runs(self):
        self.none("`{cite:p}`A``")
        self.none("``{cite:p}`A` ``")
        self.none("```{cite:p}`A` ``` ")
        self.assertEqual(found("`x` {cite:p}`A`"), [("cite:p", 0, "A")])

    def test_indented_code_html_comments_urls_refdefs_frontmatter(self):
        self.none("para\n\n    {cite:p}`A`\n")
        self.none("<!-- {cite:p}`A` -->")
        self.none("<!--\n\n{cite:p}`A`\n\n-->\n")
        self.none("```\ncode\n```\n<!-- a -->")
        self.assertEqual(found("<!-- c --> {cite:p}`A` <!-- d -->"), [("cite:p", 0, "A")])
        self.none("[x](https://example.org/{a}_{b}) <https://example.org/a_b>")
        self.none("[ref]: https://example.org/path \"Title\"\n")
        self.none("---\ntitle: x\nkeywords: [a, b]\n---\n")
        self.none("%% a comment line\n% another\n")
        self.none(r"\{cite:p}`A` and \@key and mail a@b.org")

    def test_unknown_citation_like_text_that_is_not_a_role(self):
        self.none("{ref}`sec-x` {numref}`fig-1` {math}`x^2` {eval}`1+1`")
        self.none("curly {braces} and `code` and <not a tag")

    def test_comment_closing_resumes_scanning(self):
        self.assertEqual(found("<!-- hide\n{cite:p}`A`\n--> {cite:p}`B`\n"), [("cite:p", 0, "B")])


class ScannerFailsClosed(unittest.TestCase):
    CASES = {
        "unsupported_cite_role": ["x {cite:ps}`A`", "{cite}`A`", "{cite:author}`A`", "{cite:empty}`A`"],
        "unterminated_role": ["x {cite:p}`A", "{cite:t}``A`", "{cite:p}``"],
        "empty_key": ["{cite:p}`A,,B`", "{cite:p}`A, `", "{cite:p}` `"],
        "invalid_key": ["{cite:p}`A B`", "{cite:p}`A{B}`"],
        "malformed_cite_role": ["see {cite:p} for", "{cite:t}"],
        "unterminated_fence": ["```\ncode", ":::{note}\ntext", "~~~~\n~~~", "::::{note}\n:::{tip}\n::::"],
        "unterminated_comment": ["a <!-- b", "<!--\nx\n\ny"],
        "unterminated_frontmatter": ["---\ntitle: x\n"],
        "citation_in_frontmatter": ["---\nabstract: see {cite:p}`A`\n---\n"],
        "unsupported_citation_syntax": ["see [@A]", "as @Smith2020 showed", "(@Smith2020)"],
        "unsupported_directive_citation": [":::{weird}\n{cite:p}`A`\n:::\n", ":::{mystery} {cite:p}`A`\n:::"],
        "citation_in_comment_line": ["% {cite:p}`A`\n"],
        "citation_in_reference_definition": ["[r]: https://x.org/{cite:p}`A`\n"],
        "ambiguous_link_destination": ["[t](https://x.org/{cite:p}`A`)"],
        "unsupported_include": [":::{include} https://example.org/x.md\n:::", "```{include} a.md\n:lines: 1-2\n```",
                                "```{include} a.md\n```"],
    }

    def test_each_construct_fails_with_its_code(self):
        for code, texts in self.CASES.items():
            for t in texts:
                with self.subTest(code=code, text=t):
                    self.assertEqual(failure(t), code)

    def test_failure_reports_a_line(self):
        try:
            co.scan_markdown("ok\n\nbad {cite:ps}`A`\n")
        except co._ScanFail as e:
            self.assertEqual(e.line, 3)
        else:
            self.fail("no failure")


class IncludeTests(Tmp):
    def test_known_local_include_is_supported_and_recorded_as_a_dependency(self):
        make_manuscript(self.root)
        scan = co.scan_sources(self.root)
        roles = {s["path"]: s["role"] for s in scan.sources}
        self.assertEqual(roles["provenance/review_request.md"], "include")
        self.assertEqual(roles["content/Methods.md"], "markdown")
        self.assertEqual(roles["extra.bib"], "bibliography")

    def test_included_file_is_part_of_the_revision(self):
        make_manuscript(self.root)
        before = co.scan_sources(self.root).revision
        p = self.root / "provenance/review_request.md"
        p.write_text(p.read_text().replace("cell responses", "cell reactions"))
        self.assertNotEqual(co.scan_sources(self.root).revision, before)

    def test_include_failures(self):
        make_manuscript(self.root)
        m = self.root / "content/Methods.md"
        base = m.read_text()
        cases = {
            "include_marker_not_found": base.replace("## Verbatim user prompt", "## Missing marker"),
            "path_escape": base.replace("../provenance/review_request.md", "../../outside.md"),
            "unreadable_source": base.replace("../provenance/review_request.md", "../provenance/absent.md"),
            "unsupported_include": base.replace(":start-after:", ":unknown-option:"),
        }
        for code, text in cases.items():
            with self.subTest(code=code):
                m.write_text(text)
                with self.assertRaises(co.CitationError) as ctx:
                    co.scan_sources(self.root)
                self.assertIn(code, codes(ctx.exception.diagnostics))

    def test_citation_inside_an_included_region_is_refused_not_lost(self):
        make_manuscript(self.root)
        p = self.root / "provenance/review_request.md"
        p.write_text(p.read_text().replace("> Review cell responses.", "> Review {cite:p}`Smith2020`."))
        with self.assertRaises(co.CitationError) as ctx:
            co.scan_sources(self.root)
        self.assertEqual(codes(ctx.exception.diagnostics), ["citation_in_include"])

    def test_symlinked_include_is_refused(self):
        make_manuscript(self.root)
        p = self.root / "provenance/review_request.md"
        real = self.root / "provenance/real.md"
        real.write_bytes(p.read_bytes())
        p.unlink()
        os.symlink(str(real), str(p))
        with self.assertRaises(co.CitationError) as ctx:
            co.scan_sources(self.root)
        self.assertIn("symlink_refused", codes(ctx.exception.diagnostics))

    def test_literal_include_is_code_and_not_read(self):
        self.text("content/a.md", "```{include} ../nowhere.txt\n:literal:\n```\ntext\n")
        self.text("content/r.bib", "")
        self.assertEqual(co.scan_sources(self.root).occurrences, [])


# ------------------------------------------------------------- bibliography
class BibTests(unittest.TestCase):
    def test_entries_with_braces_quotes_strings_and_comments(self):
        text = open(str(FIX / "manuscript/content/references.bib"), encoding="utf-8").read()
        entries = co.parse_bib(text, "references.bib")
        self.assertEqual(set(entries), {"Jones2021", "Lee2019", "Smith2020", "Müller2022"})
        raw = text.encode("utf-8")
        e = entries["Müller2022"]
        self.assertEqual(raw[slice(*e["byte_span"])].decode("utf-8"), e["text"])
        self.assertTrue(e["text"].startswith("@article{Müller2022,") and e["text"].endswith("}"))

    def test_parenthesised_entries_and_at_signs_in_text(self):
        entries = co.parse_bib("mail me @ home\n@misc(Key1, title = {a (b) c})\n", "x.bib")
        self.assertEqual(list(entries), ["Key1"])

    def test_unsupported_and_duplicate_entries_are_rejected(self):
        for code, text in {
            "bib_parse_error": ["@article{A, title = {unbalanced}", "@article{A, title = \"x}\"}"],
            "invalid_bib_key": ["@article{, title = {x}}", "@article{a b, title = {x}}"],
            "duplicate_bib_key": ["@misc{A, x={1}}\n@misc{A, x={2}}"],
        }.items():
            for t in text:
                with self.subTest(code=code, text=t):
                    with self.assertRaises(co._ScanFail) as ctx:
                        co.parse_bib(t, "x.bib")
                    self.assertEqual(ctx.exception.code, code)

    def test_duplicate_key_across_files_and_unresolved_key(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(root), True)
        (root / "content").mkdir()
        (root / "content/a.bib").write_text("@misc{A, x={1}}\n")
        (root / "b.bib").write_text("@misc{A, x={2}}\n")
        with self.assertRaises(co.CitationError) as ctx:
            co.scan_sources(root)
        self.assertEqual(codes(ctx.exception.diagnostics), ["duplicate_bib_key"])
        (root / "b.bib").unlink()
        (root / "content/a.md").write_text("see {cite:p}`Missing`\n")
        with self.assertRaises(co.CitationError) as ctx:
            co.scan_sources(root)
        self.assertEqual(codes(ctx.exception.diagnostics), ["unresolved_bib_key"])


# ---------------------------------------------------------------- discovery
class DiscoveryTests(Tmp):
    def test_fixed_canonical_discovery(self):
        make_manuscript(self.root)
        self.text("content/sub/deep/c.md", "no citations\n")
        self.text("content/sub/more.bib", "@misc{Deep, x={1}}\n")
        self.text("content/notes.txt", "{cite:p}`Ignored`")
        self.text("content/widget.mjs", "// {cite:p}`Ignored`")
        self.text("elsewhere/x.md", "{cite:p}`Ignored`")
        self.text("README.md", "{cite:p}`Ignored`")
        paths = [s["path"] for s in co.scan_sources(self.root).sources]
        self.assertEqual(paths, sorted(paths))
        self.assertEqual(paths, ["content/01_introduction.md", "content/02_results.md", "content/Methods.md",
                                 "content/extra_refs.bib", "content/references.bib", "content/sub/deep/c.md",
                                 "content/sub/more.bib",
                                 "extra.bib", "provenance/review_request.md"])

    def test_revision_is_deterministic_and_covers_every_byte_including_bibliography(self):
        make_manuscript(self.root)
        base = co.scan_sources(self.root).revision
        other = self.tmp / "copy"
        make_manuscript(other)
        self.assertEqual(co.scan_sources(other).revision, base)
        bib = self.root / "extra.bib"
        bib.write_text(bib.read_text() + "\n")
        self.assertNotEqual(co.scan_sources(self.root).revision, base)

    def test_missing_source_root_and_hostile_files(self):
        with self.assertRaises(co.CitationError) as ctx:
            co.scan_sources(self.root)
        self.assertEqual(codes(ctx.exception.diagnostics), ["missing_source_root"])
        (self.root / "content").mkdir()
        cases = {
            "invalid_utf8": lambda: self.text("content/a.md", "x").write_bytes(b"caf\xe9 {cite:p}`A`"),
            "bom_not_allowed": lambda: (self.root / "content/a.md").write_bytes(b"\xef\xbb\xbfhello"),
            "symlink_refused": lambda: os.symlink(str(self.root / "elsewhere"), str(self.root / "content/a.md")),
        }
        (self.root / "elsewhere").write_text("x")
        for code, make in cases.items():
            with self.subTest(code=code):
                make()
                with self.assertRaises(co.CitationError) as ctx:
                    co.scan_sources(self.root)
                self.assertIn(code, codes(ctx.exception.diagnostics))
                (self.root / "content/a.md").unlink()

    def test_symlinked_directory_and_symlinked_content_root_are_refused(self):
        (self.root / "content").mkdir()
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "secret.md").write_text("{cite:p}`X`")
        os.symlink(str(outside), str(self.root / "content/linked"))
        with self.assertRaises(co.CitationError) as ctx:
            co.scan_sources(self.root)
        self.assertEqual(codes(ctx.exception.diagnostics), ["symlink_refused"])
        shutil.rmtree(str(self.root / "content"))
        os.symlink(str(outside), str(self.root / "content"))
        with self.assertRaises(co.CitationError) as ctx:
            co.scan_sources(self.root)
        self.assertEqual(codes(ctx.exception.diagnostics), ["missing_source_root"])

    @unittest.skipIf(not hasattr(os, "geteuid") or os.geteuid() == 0, "root can read anything")
    def test_unreadable_source_is_reported(self):
        (self.root / "content").mkdir()
        p = self.text("content/a.md", "text")
        os.chmod(str(p), 0)
        self.addCleanup(os.chmod, str(p), 0o644)
        with self.assertRaises(co.CitationError) as ctx:
            co.scan_sources(self.root)
        self.assertEqual(codes(ctx.exception.diagnostics), ["unreadable_source"])

    def test_one_failing_file_does_not_hide_another(self):
        (self.root / "content").mkdir()
        self.text("content/a.md", "{cite:ps}`A`")
        self.text("content/b.md", "```\nopen")
        with self.assertRaises(co.CitationError) as ctx:
            co.scan_sources(self.root)
        self.assertEqual(codes(ctx.exception.diagnostics), ["unsupported_cite_role", "unterminated_fence"])
        self.assertEqual({d["file"] for d in ctx.exception.diagnostics}, {"content/a.md", "content/b.md"})


# --------------------------------------------------------------- inventory
class InventoryTests(Tmp):
    def setUp(self):
        super().setUp()
        make_manuscript(self.root)
        self.scan = co.scan_sources(self.root)
        self.inv = copy.deepcopy(self.scan.inventory())

    def check(self, inv=None, root=None):
        return co.check_inventory(self.inv if inv is None else inv, co.scan_sources(root or self.root))

    def test_fixture_extraction_is_exact_and_complete(self):
        self.assertEqual(self.check(), [])
        self.assertEqual(self.inv["occurrence_count"], 11)
        self.assertEqual({o["citation_key"] for o in self.inv["occurrences"]},
                         {"Jones2021", "Lee2019", "Root2018", "Smith2020", "Müller2022"})
        multi = [o for o in self.inv["occurrences"] if o["line_range"] == [6, 6]]
        self.assertEqual([(o["directive_kind"], o["key_index"], o["citation_key"]) for o in multi],
                         [("cite:p", 0, "Smith2020"), ("cite:p", 1, "Jones2021"), ("cite:t", 0, "Smith2020")])
        for o in self.inv["occurrences"]:
            self.assertTrue(o["bib_entry"]["text"].startswith("@"))
            self.assertEqual(set(co.OCC_REQUIRED) - set(o), set())

    def test_ids_are_stable_unique_and_derive_from_revision_path_span_and_key_index(self):
        again = co.scan_sources(self.root)
        self.assertEqual([o["occurrence_id"] for o in again.occurrences],
                         [o["occurrence_id"] for o in self.scan.occurrences])
        self.assertEqual(len({o["occurrence_id"] for o in again.occurrences}), len(again.occurrences))
        o = self.scan.occurrences[1]
        self.assertEqual(o["occurrence_id"],
                         co.occurrence_id(o["manuscript_revision"], o["relative_file"], o["byte_span"], o["key_index"]))
        # the same role in a second file or at another offset gets another id
        self.assertNotEqual(co.occurrence_id(HEX, "a.md", [1, 5], 0), co.occurrence_id(HEX, "b.md", [1, 5], 0))
        self.assertNotEqual(co.occurrence_id(HEX, "a.md", [1, 5], 0), co.occurrence_id(HEX, "a.md", [1, 5], 1))
        self.assertNotEqual(co.occurrence_id(HEX, "a.md", [1, 5], 0), co.occurrence_id("1" * 64, "a.md", [1, 5], 0))

    def test_repeated_and_multi_key_roles_across_files_are_all_listed(self):
        per_file = {}
        for o in self.inv["occurrences"]:
            per_file.setdefault(o["relative_file"], []).append(o["citation_key"])
        self.assertEqual(per_file["content/02_results.md"], ["Lee2019", "Lee2019", "Root2018", "Jones2021"])
        self.assertEqual(per_file["content/Methods.md"], ["Smith2020"])

    def test_zero_citations_is_a_valid_explicit_inventory_but_a_missing_one_is_not(self):
        root = self.tmp / "zero"
        (root / "content").mkdir(parents=True)
        (root / "content/a.md").write_text("# Nothing cited\n")
        scan = co.scan_sources(root)
        self.assertEqual(scan.occurrences, [])
        self.assertEqual(co.check_inventory(scan.inventory(), scan), [])
        self.assertEqual(co.validate_occurrences(root)["errors"][0]["code"], "missing_file")
        co.write_atomic(root / co.EXTRACTION, co.json_text(scan.inventory()).encode())
        self.assertTrue(co.validate_occurrences(root)["ok"])
        # an empty inventory is not valid once a citation appears
        (root / "content/a.md").write_text("# Cited {cite:p}`K`\n")
        (root / "content/r.bib").write_text("@misc{K, title={t}}\n")
        self.assertEqual(codes(co.validate_occurrences(root)["errors"]), ["source_added", "source_modified",
                                                                           "stale_revision"])

    def test_stale_source_changes_are_detected_with_precise_codes(self):
        cases = {
            "added markdown": (lambda r: (r / "content/new.md").write_text("new\n"), "source_added"),
            "added bibliography": (lambda r: (r / "content/new.bib").write_text("@misc{N, x={1}}\n"), "source_added"),
            "deleted markdown": (lambda r: (r / "content/02_results.md").unlink(), "source_removed"),
            "deleted bibliography": (lambda r: (r / "content/extra_refs.bib").unlink(), "source_removed"),
            "modified markdown": (lambda r: (r / "content/01_introduction.md").write_text(
                (r / "content/01_introduction.md").read_text() + "\nmore\n"), "source_modified"),
            "modified bibliography": (lambda r: (r / "content/references.bib").write_text(
                (r / "content/references.bib").read_text().replace("Replication", "Replications")), "source_modified"),
            "modified include": (lambda r: (r / "provenance/review_request.md").write_text(
                (r / "provenance/review_request.md").read_text() + "x\n"), "source_modified"),
        }
        for name, (mutate, code) in cases.items():
            with self.subTest(name):
                root = self.tmp / ("case-" + name.replace(" ", "-"))
                make_manuscript(root)
                mutate(root)
                diags = co.check_inventory(copy.deepcopy(self.inv), co.scan_sources(root))
                self.assertIn("stale_revision", codes(diags))
                self.assertIn(code, codes(diags))

    def test_whitespace_only_edit_is_stale_even_when_no_occurrence_moves(self):
        p = self.root / "content/02_results.md"
        p.write_text(p.read_text() + "\n")
        self.assertIn("stale_revision", codes(self.check()))

    def test_omissions_masked_by_duplicates_are_detected(self):
        inv = copy.deepcopy(self.inv)
        inv["occurrences"][3] = copy.deepcopy(inv["occurrences"][2])  # same count, one missing, one doubled
        diags = self.check(inv)
        self.assertEqual({"duplicate_occurrence_id", "duplicate_occurrence", "missing_occurrence"}, set(codes(diags)))
        inv = copy.deepcopy(self.inv)
        forged = copy.deepcopy(inv["occurrences"][2])
        forged["occurrence_id"] = "a" * 64  # duplicate identity under a new id
        inv["occurrences"][3] = forged
        diags = self.check(inv)
        self.assertIn("duplicate_occurrence", codes(diags))
        self.assertIn("invalid_occurrence_id", codes(diags))
        self.assertIn("missing_occurrence", codes(diags))

    def test_missing_unknown_and_changed_records(self):
        inv = copy.deepcopy(self.inv)
        gone = inv["occurrences"].pop(4)
        inv["occurrence_count"] -= 1
        d = self.check(inv)
        self.assertIn("missing_occurrence", codes(d))
        self.assertEqual([x["occurrence_id"] for x in d if x["code"] == "missing_occurrence"], [gone["occurrence_id"]])
        inv = copy.deepcopy(self.inv)
        extra = copy.deepcopy(inv["occurrences"][0])
        extra["byte_span"] = [extra["byte_span"][0] + 1, extra["byte_span"][1] + 1]
        extra["occurrence_id"] = co.occurrence_id(extra["manuscript_revision"], extra["relative_file"],
                                                  extra["byte_span"], extra["key_index"])
        inv["occurrences"].append(extra)
        inv["occurrence_count"] += 1
        self.assertIn("unknown_occurrence", codes(self.check(inv)))
        for field, value in {"citation_key": "Lee2019", "relative_file": "content/02_results.md",
                             "directive_kind": "cite:t", "line_range": [99, 99], "context": "forged",
                             "bib_entry": {"file": "x", "byte_span": [0, 1], "text": "@x"}}.items():
            with self.subTest(field=field):
                inv = copy.deepcopy(self.inv)
                inv["occurrences"][0][field] = value
                d = self.check(inv)
                self.assertTrue({"occurrence_mismatch", "invalid_occurrence_id"} & set(codes(d)), d)

    def test_claimed_finding_is_advisory_and_never_compared(self):
        inv = copy.deepcopy(self.inv)
        inv["occurrences"][0]["claimed_finding"] = "Cells respond (LLM summary, not a mechanical fact)"
        self.assertEqual(self.check(inv), [])
        inv["occurrences"][0]["claimed_finding"] = 7
        self.assertEqual(codes(self.check(inv)), ["malformed_record"])

    def test_malformed_envelopes_and_records(self):
        def mutate(f):
            inv = copy.deepcopy(self.inv)
            f(inv)
            return inv

        cases = {
            "malformed_envelope": [lambda i: i.pop("sources"), lambda i: i.update(extra=1),
                                   lambda i: i.update(occurrences={}), lambda i: i.update(occurrence_count="11"),
                                   lambda i: i.update(manuscript_revision="XYZ"),
                                   lambda i: i["sources"][0].update(sha256="no")],
            "unsupported_schema_version": [lambda i: i.update(schema_version="comprev.citation_occurrences/2")],
            "malformed_record": [lambda i: i["occurrences"][0].pop("byte_span"),
                                 lambda i: i["occurrences"][0].update(extra=1),
                                 lambda i: i["occurrences"][0].update(byte_span=[True, 5]),
                                 lambda i: i["occurrences"][0].update(byte_span=[5, 5]),
                                 lambda i: i["occurrences"][0].update(byte_span=[0.0, 5]),
                                 lambda i: i["occurrences"][0].update(line_range=[0, 1]),
                                 lambda i: i["occurrences"][0].update(key_index=-1),
                                 lambda i: i["occurrences"][0].update(key_index=False),
                                 lambda i: i["occurrences"][0].update(directive_kind="cite:ps"),
                                 lambda i: i["occurrences"][0].update(citation_key=" "),
                                 lambda i: i["occurrences"][0].update(relative_file="../x.md"),
                                 lambda i: i["occurrences"][0].update(relative_file="/abs.md"),
                                 lambda i: i["occurrences"][0].update(occurrence_id="short"),
                                 lambda i: i["occurrences"].__setitem__(0, 5)],
            "count_mismatch": [lambda i: i.update(occurrence_count=3)],
        }
        for code, fs in cases.items():
            for n, f in enumerate(fs):
                with self.subTest(code=code, n=n):
                    self.assertIn(code, codes(self.check(mutate(f))))
        self.assertEqual(codes(co.check_inventory([], self.scan)), ["malformed_envelope"])

    def test_digest_tampering_is_caught(self):
        inv = copy.deepcopy(self.inv)
        inv["extraction_digest"] = "f" * 64
        self.assertEqual(codes(self.check(inv)), ["digest_mismatch"])
        inv = copy.deepcopy(self.inv)
        inv["scanner_version"] = "comprev.citation_scanner/0"
        self.assertIn("scanner_version_mismatch", codes(self.check(inv)))

    def test_json_level_failures_are_reported_not_raised(self):
        base = co.json_text(self.inv)
        cases = {
            "invalid_json": ["{", base.replace('"occurrence_count"', '"occurrence_count": 1, "occurrence_count"', 1),
                             base.replace('"occurrence_count": 11', '"occurrence_count": NaN')],
            "not_an_object": ["[]", "3"],
        }
        (self.root / "provenance").mkdir(exist_ok=True)
        for code, texts in cases.items():
            for t in texts:
                with self.subTest(code=code, text=t[:30]):
                    (self.root / co.EXTRACTION).write_text(t)
                    rep = co.validate_occurrences(self.root)
                    self.assertFalse(rep["ok"])
                    self.assertEqual(codes(rep["errors"]), [code])
        (self.root / co.EXTRACTION).write_bytes(b"\xef\xbb\xbf" + base.encode())
        self.assertEqual(codes(co.validate_occurrences(self.root)["errors"]), ["invalid_json"])
        (self.root / co.EXTRACTION).unlink()
        self.assertEqual(codes(co.validate_occurrences(self.root)["errors"]), ["missing_file"])

    def test_scan_failure_is_reported_by_the_public_validator(self):
        (self.root / "provenance").mkdir(exist_ok=True)
        write_json(self.root, co.EXTRACTION, self.inv)
        (self.root / "content/01_introduction.md").write_text("```\nopen\n")
        rep = co.validate_occurrences(self.root)
        self.assertEqual((rep["ok"], codes(rep["errors"])), (False, ["unterminated_fence"]))


class CommandLineTests(Tmp):
    def run_cli(self, *args, python=(sys.executable,)):
        p = subprocess.run(list(python) + [str(ROOT / "scripts/citation_occurrences.py")] + list(args),
                           capture_output=True, text=True)
        return p.returncode, (json.loads(p.stdout) if p.stdout.strip().startswith("{") else p.stdout), p.stderr

    def test_extract_validate_roundtrip_and_failure_exit_codes(self):
        make_manuscript(self.root)
        rc, out, err = self.run_cli("extract", "--root", str(self.root))
        self.assertEqual((rc, out["ok"], out["occurrence_count"]), (0, True, 11), err)
        self.assertEqual(out["schema_version"], co.DIAG_SCHEMA)
        rc, out, _ = self.run_cli("validate-occurrences", "--root", str(self.root))
        self.assertEqual((rc, out["ok"], out["errors"]), (0, True, []))
        p = self.root / "content/02_results.md"
        p.write_text(p.read_text() + "\n")
        for flags in ((), ("-O",)):
            rc, out, _ = self.run_cli("validate-occurrences", "--root", str(self.root),
                                      python=(sys.executable,) + flags)
            self.assertEqual(rc, 1, flags)
            self.assertFalse(out["ok"])
            self.assertIn("stale_revision", codes(out["errors"]))
            self.assertEqual(out["command"], "validate-occurrences")

    def test_extract_to_stdout_and_failing_extract(self):
        make_manuscript(self.root)
        rc, out, _ = self.run_cli("extract", "--root", str(self.root), "--out", "-")
        self.assertEqual(rc, 0)
        self.assertEqual(out["schema_version"], co.OCC_SCHEMA)
        (self.root / "content/bad.md").write_text("{cite:ps}`x`")
        rc, out, _ = self.run_cli("extract", "--root", str(self.root))
        self.assertEqual((rc, out["ok"], codes(out["errors"])), (1, False, ["unsupported_cite_role"]))

    def test_validate_verdicts_cli_and_no_bypass_flags(self):
        make_manuscript(self.root)
        stage_extraction(self.root)
        stage_verification(self.root)
        rc, out, err = self.run_cli("validate-verdicts", "--root", str(self.root))
        self.assertEqual((rc, out["ok"]), (0, True), err)
        for flag in ("--skip-live", "--historical", "--no-fresh", "--trust-inventory"):
            rc, _, err = self.run_cli("validate-verdicts", "--root", str(self.root), flag)
            self.assertEqual(rc, 2, flag)
        help_text = subprocess.run([sys.executable, str(ROOT / "scripts/citation_occurrences.py"),
                                    "validate-verdicts", "--help"], capture_output=True, text=True).stdout
        self.assertNotRegex(help_text, r"skip|bypass|trust|force|no-fresh")

    BAD = "x\n\n> - ```\n    > {cite:p}`Smith2020`\n> ```\n> ```\n"

    def snapshot_tree(self):
        return {str(p.relative_to(self.root)): (p.read_bytes() if p.is_file() else None)
                for p in sorted(self.root.rglob("*"))}

    def test_unsupported_container_layout_fails_every_command_without_writes(self):
        make_manuscript(self.root)
        stage_extraction(self.root)
        stage_coverage(self.root)
        stage_verification(self.root)
        self.text("content/zz_bad.md", self.BAD)
        before = self.snapshot_tree()
        for command in ("extract", "snapshot", "coverage", "validate-occurrences", "validate-verdicts"):
            for flags in ((), ("-O",)):
                with self.subTest(command=command, flags=flags):
                    args = [command, "--root", str(self.root)]
                    if command == "snapshot":
                        args.append("--supersede")
                    rc, out, err = self.run_cli(*args, python=(sys.executable,) + flags)
                    self.assertEqual(rc, 1, err)
                    self.assertIsInstance(out, dict, "diagnostics must be JSON on stdout, not a traceback")
                    self.assertNotIn("Traceback", err)
                    self.assertFalse(out["ok"])
                    self.assertEqual(codes(out["errors"]), ["unsupported_container_layout"])
                    self.assertEqual([(d["file"], d["line"]) for d in out["errors"]], [("content/zz_bad.md", 4)])
                    self.assertEqual(self.snapshot_tree(), before, "a failed validation must not write artifacts")

    def test_fresh_extract_on_the_blocker_writes_nothing(self):
        make_manuscript(self.root)
        self.text("content/zz_bad.md", self.BAD)
        rc, out, _ = self.run_cli("extract", "--root", str(self.root))
        self.assertEqual((rc, codes(out["errors"])), (1, ["unsupported_container_layout"]))
        self.assertFalse((self.root / co.EXTRACTION).exists())
        self.assertFalse((self.root / co.SNAPSHOT_DIR).exists())


# ---------------------------------------------------------------- verdicts
class VerdictTests(Tmp):
    def setUp(self):
        super().setUp()
        make_manuscript(self.root)
        stage_extraction(self.root)
        self.env = stage_verification(self.root)
        self.scan = co.scan_sources(self.root)

    def check(self, env=None):
        return co.check_verdicts(self.root, copy.deepcopy(self.env if env is None else env), self.scan)

    def mutated(self, f, index=0):
        env = copy.deepcopy(self.env)
        f(env["verdicts"][index])
        return env

    def test_complete_verification_with_real_evidence_passes(self):
        self.assertEqual(self.check(), [])
        self.assertTrue(co.validate_verdicts(self.root)["ok"])
        self.assertEqual(len(self.env["verdicts"]), 11)
        self.assertEqual({v["verification_depth"] for v in self.env["verdicts"]}, {"fulltext", "abstract_only"})

    def test_exactly_one_verdict_per_occurrence_missing_unknown_duplicate(self):
        env = copy.deepcopy(self.env)
        gone = env["verdicts"].pop(2)
        d = self.check(env)
        self.assertEqual(codes(d), ["missing_verdict"])
        self.assertEqual(d[0]["occurrence_id"], gone["occurrence_id"])
        env = copy.deepcopy(self.env)
        env["verdicts"][2]["occurrence_id"] = "b" * 64
        self.assertEqual(codes(self.check(env)), ["missing_verdict", "unknown_verdict_id"])
        env = copy.deepcopy(self.env)
        env["verdicts"].append(copy.deepcopy(env["verdicts"][2]))
        self.assertEqual(codes(self.check(env)), ["duplicate_verdict"])
        env = copy.deepcopy(self.env)  # a duplicate that conceals an omission keeps the count equal
        env["verdicts"][3] = copy.deepcopy(env["verdicts"][2])
        self.assertEqual(codes(self.check(env)), ["duplicate_verdict", "missing_verdict"])
        env = copy.deepcopy(self.env)
        env["verdicts"] = []
        self.assertEqual(codes(self.check(env)), ["missing_verdict"])

    def test_repeated_key_occurrences_each_need_their_own_verdict(self):
        lee = [o for o in self.scan.occurrences if o["citation_key"] == "Lee2019"]
        self.assertEqual(len(lee), 3)
        env = copy.deepcopy(self.env)
        env["verdicts"] = [v for v in env["verdicts"] if v["occurrence_id"] != lee[1]["occurrence_id"]]
        d = self.check(env)
        self.assertEqual([x["occurrence_id"] for x in d], [lee[1]["occurrence_id"]])

    def test_stale_revision_and_digest(self):
        env = copy.deepcopy(self.env)
        env["manuscript_revision"] = "c" * 64
        self.assertIn("stale_revision", codes(self.check(env)))
        env = copy.deepcopy(self.env)
        env["extraction_digest"] = "d" * 64
        self.assertEqual(codes(self.check(env)), ["extraction_digest_mismatch"])
        self.assertIn("stale_revision", codes(self.check(self.mutated(
            lambda v: v.update(manuscript_revision="e" * 64)))))

    def test_stale_source_after_verification_fails_the_public_validator(self):
        p = self.root / "content/02_results.md"
        p.write_text(p.read_text().replace("prose too", "prose, too"))
        rep = co.validate_verdicts(self.root)
        self.assertFalse(rep["ok"])
        self.assertIn("stale_revision", codes(rep["errors"]))

    def test_malformed_envelopes_and_verdicts(self):
        cases = {
            "malformed_envelope": [lambda e: e.pop("verdicts"), lambda e: e.update(x=1), lambda e: e.update(verdicts={}),
                                   lambda e: e.update(manuscript_revision="x")],
            "unsupported_schema_version": [lambda e: e.update(schema_version="comprev.citation_verification/2")],
        }
        for code, fs in cases.items():
            for f in fs:
                env = copy.deepcopy(self.env)
                f(env)
                self.assertIn(code, codes(self.check(env)))
        for n, f in enumerate([lambda v: v.pop("details"), lambda v: v.update(surprise=1),
                               lambda v: v.update(cite_key=7), lambda v: v.update(details=5),
                               lambda v: v.update(failed_at_step=3),
                               lambda v: v.update(verification_depth="deep")]):
            with self.subTest(n=n):
                self.assertTrue({"malformed_verdict", "verdict_key_mismatch"} & set(codes(self.check(
                    self.mutated(f)))), n)
        self.assertEqual(codes(self.check(self.mutated(lambda v: v.update(cite_key="Other")))), ["verdict_key_mismatch"])
        self.assertEqual(codes(self.check(self.mutated(lambda v: v.update(category="GREAT")))), ["unknown_category"])
        env = copy.deepcopy(self.env)
        env["verdicts"][0] = "not an object"
        self.assertIn("malformed_verdict", codes(self.check(env)))

    def test_verified_with_unavailable_or_unverifiable_evidence_fails(self):
        for status in ("unavailable", "unverifiable"):
            with self.subTest(status):
                d = self.check(self.mutated(lambda v: v.update(evidence={
                    "status": status, "citation_key": v["cite_key"], "reason": "paywalled"})))
                self.assertEqual(codes(d), ["evidence_not_available"])
        self.assertEqual(codes(self.check(self.mutated(lambda v: v.update(evidence={
            "status": "maybe", "citation_key": v["cite_key"]})))), ["unknown_evidence_status"])
        self.assertEqual(codes(self.check(self.mutated(lambda v: v.pop("evidence")))), ["evidence_not_available"])
        self.assertEqual(codes(self.check(self.mutated(lambda v: v.update(evidence=None)))), ["evidence_not_available"])
        self.assertEqual(codes(self.check(self.mutated(lambda v: v.update(evidence="x")))), ["malformed_evidence"])

    def test_metadata_only_is_never_valid(self):
        self.assertIn("metadata_only_not_valid", codes(self.check(self.mutated(
            lambda v: v.update(verification_depth="metadata_only")))))
        self.assertIn("metadata_only_not_valid", codes(self.check(self.mutated(
            lambda v: v["evidence"].update(depth="metadata_only")))))
        env = self.mutated(lambda v: v.update(category="HALLUCINATED", failed_at_step=1,
                                              verification_depth="metadata_only", details="no record"))
        self.assertIn("metadata_only_not_valid", codes(self.check(env)))

    def test_tampered_mismatched_or_missing_evidence_fails(self):
        v0 = self.env["verdicts"][0]
        ev_file = self.root / v0["evidence"]["path"]
        original = ev_file.read_bytes()
        ev_file.write_bytes(original + b"edited")
        self.assertEqual(codes(self.check()), ["evidence_hash_mismatch"])
        ev_file.write_bytes(original)
        self.assertEqual(self.check(), [])
        ev_file.unlink()
        self.assertEqual(codes(self.check()), ["evidence_missing"])
        ev_file.write_bytes(original)
        cases = {
            "evidence_hash_mismatch": lambda v: v["evidence"].update(sha256="a" * 64),
            "evidence_key_mismatch": lambda v: v["evidence"].update(citation_key="Lee2019"),
            "passage_mismatch": lambda v: v.update(supporting_passage="Cells respond robustly."),
            "passage_out_of_range": lambda v: v["evidence"].update(passage_span=[0, 10 ** 6]),
            "evidence_path_invalid": lambda v: v["evidence"].update(path="content/01_introduction.md"),
            "depth_mismatch": lambda v: v["evidence"].update(depth="abstract_only",
                                                            fulltext_unavailable_reason="x"),
            "fulltext_fallback_unexplained": lambda v: v["evidence"].update(depth="abstract_only", fulltext_unavailable_reason=" "),
            "malformed_evidence": lambda v: v["evidence"].update(extra="x"),
        }
        for code, f in cases.items():
            with self.subTest(code):
                self.assertIn(code, codes(self.check(self.mutated(f))))

    def test_evidence_path_traversal_and_symlinks_are_refused(self):
        outside = self.tmp / "outside.txt"
        outside.write_text("secret passage\n")
        for rel in ("provenance/citation_evidence/../../outside.txt", "/etc/passwd", "provenance/citation_evidence/"):
            env = self.mutated(lambda v: v["evidence"].update(path=rel))
            self.assertEqual(codes(self.check(env)), ["evidence_path_invalid"], rel)
        link = self.root / co.EVIDENCE_DIR / "linked.txt"
        os.symlink(str(outside), str(link))
        env = self.mutated(lambda v: v["evidence"].update(
            path=co.EVIDENCE_DIR + "/linked.txt", sha256=co.sha256_hex(outside.read_bytes()),
            passage_span=[0, 6]))
        self.assertIn("symlink_refused", codes(self.check(env)))

    def test_passage_that_splits_a_utf8_sequence_is_rejected(self):
        occ = next(o for o in self.scan.occurrences if o["citation_key"] == "Müller2022")
        v = next(x for x in self.env["verdicts"] if x["occurrence_id"] == occ["occurrence_id"])
        data = (self.root / v["evidence"]["path"]).read_bytes()
        cut = data.index("ü".encode("utf-8")) + 1
        env = copy.deepcopy(self.env)
        for x in env["verdicts"]:
            if x["occurrence_id"] == occ["occurrence_id"]:
                x["evidence"]["passage_span"] = [0, cut]
        self.assertEqual(codes(self.check(env)), ["passage_not_utf8"])

    def test_abstract_fallback_must_be_explicit(self):
        v = next(x for x in self.env["verdicts"] if x["verification_depth"] == "abstract_only")
        self.assertTrue(v["evidence"]["fulltext_unavailable_reason"])
        env = copy.deepcopy(self.env)
        for x in env["verdicts"]:
            if x["verification_depth"] == "abstract_only":
                del x["evidence"]["fulltext_unavailable_reason"]
        self.assertEqual(codes(self.check(env)), ["fulltext_fallback_unexplained"])

    def test_scientific_attestation_is_required_for_verified(self):
        self.assertEqual(codes(self.check(self.mutated(lambda v: v.pop("scientific_checks_attestation")))),
                         ["missing_attestation"])
        for bad in ({"protocol": "x", "steps_completed": [1, 2, 3, 4, 5], "attested_by": "a"},
                    {"protocol": co.PROTOCOL, "steps_completed": [1, 2, 3], "attested_by": "a"},
                    {"protocol": co.PROTOCOL, "steps_completed": [1, 2, 3, 4, 5], "attested_by": ""},
                    {"protocol": co.PROTOCOL, "steps_completed": [1, 2, 3, 4, 5], "attested_by": "a", "x": 1}):
            self.assertEqual(codes(self.check(self.mutated(lambda v: v.update(scientific_checks_attestation=bad)))),
                             ["missing_attestation"])

    def test_non_verified_verdicts(self):
        base = dict(category="MISATTRIBUTED", failed_at_step=5, supporting_passage=None,
                    details="Paper never reports this effect.")
        # with evidence (the audit trail may keep it), without evidence plus a manual-review note
        ok_with = self.mutated(lambda v: v.update(base, supporting_passage=None))
        self.assertEqual(self.check(ok_with), [])
        bare = dict(base, verification_depth=None, details="Full text and abstract unobtainable; manual review needed.")
        self.assertEqual(self.check(self.mutated(lambda v: (v.pop("evidence"), v.update(bare)))), [])
        self.assertEqual(self.check(self.mutated(lambda v: (v.update(bare, evidence={
            "status": "unverifiable", "citation_key": v["cite_key"], "reason": "no retrievable text"})))), [])
        no_note = dict(bare, details=" ")
        self.assertEqual(codes(self.check(self.mutated(lambda v: (v.pop("evidence"), v.update(no_note))))),
                         ["missing_explanation"])
        self.assertEqual(codes(self.check(self.mutated(lambda v: (v.pop("evidence"), v.update(
            dict(bare, verification_depth="fulltext")))))), ["depth_without_evidence"])
        self.assertIn("malformed_verdict", codes(self.check(self.mutated(lambda v: v.update(base, failed_at_step=None)))))
        self.assertIn("malformed_verdict", codes(self.check(self.mutated(lambda v: v.update(failed_at_step=2)))))

    def test_verified_requires_the_passage_to_be_present(self):
        self.assertIn("passage_mismatch", codes(self.check(self.mutated(lambda v: v.update(supporting_passage=None)))))
        self.assertIn("passage_mismatch", codes(self.check(self.mutated(lambda v: v.update(supporting_passage="")))))

    def test_public_validator_is_always_live_and_reads_the_files(self):
        self.assertTrue(co.validate_verdicts(self.root)["ok"])
        env = copy.deepcopy(self.env)
        env["verdicts"].pop()
        write_json(self.root, co.VERIFICATION, env)
        rep = co.validate_verdicts(self.root)
        self.assertEqual((rep["ok"], codes(rep["errors"])), (False, ["missing_verdict"]))
        (self.root / co.VERIFICATION).write_text("[")
        self.assertEqual(codes(co.validate_verdicts(self.root)["errors"]), ["invalid_json"])
        # the extraction file is validated too: stale extraction fails the verdict check
        write_json(self.root, co.VERIFICATION, self.env)
        inv = read_json(self.root, co.EXTRACTION)
        inv["occurrences"].pop()
        inv["occurrence_count"] -= 1
        write_json(self.root, co.EXTRACTION, inv)
        self.assertIn("missing_occurrence", codes(co.validate_verdicts(self.root)["errors"]))


# ------------------------------------------------- snapshot / coverage / audit
class SnapshotTests(Tmp):
    def setUp(self):
        super().setUp()
        make_manuscript(self.root)
        stage_extraction(self.root)
        self.snap = self.root / co.SNAPSHOT_DIR

    def stage(self, live=True):
        return co.check_stage(self.root, live=live)

    def expect(self, code, live=True):
        with self.assertRaises(co.CitationError) as ctx:
            self.stage(live)
        self.assertIn(code, codes(ctx.exception.diagnostics), ctx.exception.diagnostics)

    def test_snapshot_is_complete_and_matches_the_live_tree(self):
        manifest, scan = co.verify_snapshot(self.snap)
        live = co.scan_sources(self.root)
        self.assertEqual((manifest["manuscript_revision"], scan.sources), (live.revision, live.sources))
        self.assertEqual(manifest["source_count"], 7)
        self.stage(True)

    def test_snapshot_is_immutable_and_can_only_be_superseded_explicitly(self):
        with self.assertRaises(co.CitationError) as ctx:
            co.create_snapshot(self.root)
        self.assertEqual(codes(ctx.exception.diagnostics), ["snapshot_exists"])
        co.create_snapshot(self.root, supersede=True)
        self.assertTrue((self.root / "provenance/citation_snapshot.superseded-1").is_dir())
        self.stage(True)

    def test_snapshot_tampering_is_detected(self):
        f = self.snap / "files/content/01_introduction.md"
        os.chmod(str(f), 0o644)
        original = f.read_text()
        f.write_text(original + "\n")
        self.expect("snapshot_file_modified", live=False)
        f.write_text(original.replace("Cells", "Cella"))  # same size: the hash decides, not the length
        self.assertEqual(len(f.read_bytes()), len(original.encode()))
        self.expect("snapshot_file_modified", live=False)

    def test_snapshot_extra_missing_symlinked_and_omitted_sources(self):
        (self.snap / "files/content/injected.md").write_text("{cite:p}`Smith2020`\n")
        self.expect("snapshot_file_unlisted", live=False)
        (self.snap / "files/content/injected.md").unlink()
        os.chmod(str(self.snap / "files/content/02_results.md"), 0o644)
        saved = (self.snap / "files/content/02_results.md").read_bytes()
        (self.snap / "files/content/02_results.md").unlink()
        self.expect("snapshot_file_missing", live=False)
        # omit the source from the manifest AND the files: an independent rescan cannot reproduce it
        m = read_json(self.snap, "manifest.json")
        m["sources"] = [s for s in m["sources"] if s["path"] != "content/02_results.md"]
        m["source_count"] = len(m["sources"])
        os.chmod(str(self.snap / "manifest.json"), 0o644)
        write_json(self.snap, "manifest.json", m)
        self.expect("snapshot_rescan_mismatch", live=False)
        # the pinned extraction still lists the omitted source's occurrences
        (self.snap / "files/content/02_results.md").write_bytes(saved)
        write_json(self.snap, "manifest.json", {**m, "sources": read_json(self.snap, "manifest.json")["sources"]})
        os.symlink(str(self.root / "extra.bib"), str(self.snap / "files/link.bib"))
        self.expect("symlink_refused", live=False)

    def test_snapshot_directory_missing_or_symlinked(self):
        shutil.move(str(self.snap), str(self.tmp / "moved"))
        self.expect("snapshot_missing", live=False)
        os.symlink(str(self.tmp / "moved"), str(self.snap))
        self.expect("snapshot_missing", live=False)

    def test_live_edit_after_snapshot_is_stale_live_but_fine_historically(self):
        p = self.root / "content/01_introduction.md"
        p.write_text(p.read_text().replace("Cells respond", "Neurons respond"))
        self.expect("stale_snapshot", live=True)
        self.stage(live=False)  # historical: the pinned snapshot and extraction still validate
        q = self.root / "content/new_section.md"
        q.write_text("# New {cite:p}`Smith2020`\n")
        self.expect("source_added", live=True)
        self.stage(live=False)

    def test_extraction_must_equal_the_snapshot_rescan_in_both_modes(self):
        inv = read_json(self.root, co.EXTRACTION)
        inv["occurrences"].pop(0)
        inv["occurrence_count"] -= 1
        write_json(self.root, co.EXTRACTION, inv)
        self.expect("missing_occurrence", live=True)
        self.expect("missing_occurrence", live=False)

    def test_omitted_source_between_live_and_snapshot_is_caught(self):
        # a source file created after Phase 15 never reaches the snapshot
        (self.root / "content/late.bib").write_text("@misc{Late, x={1}}\n")
        self.expect("stale_snapshot", live=True)

    def test_coverage_is_deterministic_persisted_and_recomputed(self):
        rep = stage_coverage(self.root)
        doc = read_json(self.root, co.COVERAGE)
        self.assertEqual((doc["schema_version"], doc["occurrence_count"], doc["source_count"]),
                         (co.COVERAGE_SCHEMA, 11, 7))
        self.assertFalse("scientific correctness is established" in doc["scope"])
        co.check_coverage(self.root, live=True, report_obj=rep)
        again = co.write_coverage(self.root)
        self.assertEqual(again[1], rep["citation_coverage_sha256"])

    def test_coverage_forgery_and_unbound_reports_fail(self):
        rep = stage_coverage(self.root)
        with self.assertRaises(co.CitationError) as ctx:
            co.check_coverage(self.root, live=True, report_obj={"gate": "pass", "phase": 15})
        self.assertEqual(codes(ctx.exception.diagnostics), ["report_not_bound"])
        for field, value in {"citation_coverage_sha256": "0" * 64, "manuscript_revision": "1" * 64,
                             "occurrence_count": 10, }.items():
            with self.subTest(field), self.assertRaises(co.CitationError) as ctx:
                co.check_coverage(self.root, live=True, report_obj=dict(rep, **{field: value}))
            self.assertEqual(codes(ctx.exception.diagnostics), ["report_not_bound"])
        with self.assertRaises(co.CitationError) as ctx:
            co.check_coverage(self.root, live=True, report_obj=dict(rep, occurrence_count=True))
        doc = read_json(self.root, co.COVERAGE)
        doc["occurrence_count"] = 3
        write_json(self.root, co.COVERAGE, doc)
        with self.assertRaises(co.CitationError) as ctx:
            co.check_coverage(self.root, live=True)
        self.assertEqual(codes(ctx.exception.diagnostics), ["coverage_mismatch"])
        (self.root / co.COVERAGE).unlink()
        with self.assertRaises(co.CitationError) as ctx:
            co.check_coverage(self.root, live=True)
        self.assertEqual(codes(ctx.exception.diagnostics), ["coverage_missing"])

    def test_coverage_cannot_be_written_for_a_stale_snapshot(self):
        (self.root / "content/extra_note.md").write_text("changed\n")
        with self.assertRaises(co.CitationError):
            co.write_coverage(self.root)

    def test_historical_audit_validates_the_pinned_revision_only(self):
        stage_coverage(self.root)
        stage_verification(self.root)
        self.assertTrue(co.audit_historical(self.root)["ok"])
        p = self.root / "content/02_results.md"
        p.write_text(p.read_text().replace("A caption", "Altered") + "\nrepaired in Phase 19\n")
        self.assertFalse(co.validate_verdicts(self.root)["ok"])  # the public validator is live
        rep = co.audit_historical(self.root)
        self.assertTrue(rep["ok"], rep)
        self.assertEqual(rep["mode"], "historical")
        self.assertIn("current manuscript is not compared", rep["note"])
        ev = self.root / co.EVIDENCE_DIR / "Smith2020.fulltext.txt"
        ev.write_bytes(ev.read_bytes() + b"x")
        self.assertIn("evidence_hash_mismatch", codes(co.audit_historical(self.root)["errors"]))
        ev.write_bytes(ev.read_bytes()[:-1])
        f = self.snap / "files/content/Methods.md"
        os.chmod(str(f), 0o644)
        f.write_text("tampered\n")
        self.assertIn("snapshot_file_modified", codes(co.audit_historical(self.root)["errors"]))

    def test_cli_snapshot_coverage_and_audit(self):
        root = self.tmp / "cli"
        make_manuscript(root)
        run = lambda *a: subprocess.run([sys.executable, str(ROOT / "scripts/citation_occurrences.py")] + list(a),  # noqa: E731
                                        capture_output=True, text=True)
        self.assertEqual(run("snapshot", "--root", str(root)).returncode, 0)
        self.assertEqual(run("snapshot", "--root", str(root)).returncode, 1)
        self.assertEqual(run("extract", "--root", str(root)).returncode, 0)
        cov = run("coverage", "--root", str(root))
        self.assertEqual(cov.returncode, 0, cov.stderr)
        self.assertEqual(len(json.loads(cov.stdout)["citation_coverage_sha256"]), 64)
        aud = run("audit-historical", "--root", str(root))
        self.assertEqual(aud.returncode, 0, aud.stdout)
        self.assertEqual(json.loads(aud.stdout)["mode"], "historical")


# ----------------------------------------------------------------- schemas
def schema_errors(inst, schema, root, path="$"):
    """A small JSON-Schema subset checker: just enough to keep the schema files honest."""
    if "$ref" in schema:
        ref = root
        for part in schema["$ref"][2:].split("/"):
            ref = ref[part]
        return schema_errors(inst, ref, root, path)
    if "oneOf" in schema:
        ok = [s for s in schema["oneOf"] if not schema_errors(inst, s, root, path)]
        return [] if len(ok) == 1 else ["%s: matches %d of oneOf" % (path, len(ok))]
    err = []
    for sub in schema.get("allOf", []):
        err += schema_errors(inst, sub, root, path)
    if "if" in schema and not schema_errors(inst, schema["if"], root, path):
        err += schema_errors(inst, schema.get("then", {}), root, path)
    if "const" in schema and inst != schema["const"]:
        err.append("%s: const" % path)
    if "enum" in schema and inst not in schema["enum"]:
        err.append("%s: enum" % path)
    t = schema.get("type")
    if t:
        names = t if isinstance(t, list) else [t]
        py = {"object": dict, "array": list, "string": str, "integer": int, "null": type(None), "boolean": bool}
        if not any(isinstance(inst, py[n]) and (n == "boolean" or not isinstance(inst, bool) or n == "boolean")
                   for n in names):
            return err + ["%s: type %s" % (path, names)]
    if isinstance(inst, dict):
        for k in schema.get("required", []):
            if k not in inst:
                err.append("%s: missing %s" % (path, k))
        props = schema.get("properties", {})
        for k, v in inst.items():
            if k in props:
                err += schema_errors(v, props[k], root, "%s.%s" % (path, k))
            elif schema.get("additionalProperties") is False:
                err.append("%s: unknown %s" % (path, k))
    if isinstance(inst, list):
        if len(inst) < schema.get("minItems", 0) or len(inst) > schema.get("maxItems", 10 ** 9):
            err.append("%s: item count" % path)
        if "items" in schema:
            for i, v in enumerate(inst):
                err += schema_errors(v, schema["items"], root, "%s[%d]" % (path, i))
    if isinstance(inst, str):
        if len(inst) < schema.get("minLength", 0):
            err.append("%s: minLength" % path)
        if "pattern" in schema and not re.search(schema["pattern"], inst):
            err.append("%s: pattern" % path)
    if isinstance(inst, int) and not isinstance(inst, bool):
        if inst < schema.get("minimum", -10 ** 18) or inst > schema.get("maximum", 10 ** 18):
            err.append("%s: range" % path)
    return err


class SchemaMatchesRuntime(Tmp):
    def setUp(self):
        super().setUp()
        self.occ = json.loads((ROOT / "provenance/citation_occurrences.schema.json").read_text())
        self.ver = json.loads((ROOT / "provenance/citation_verdicts.schema.json").read_text())
        make_manuscript(self.root)
        stage_extraction(self.root)
        self.inv = read_json(self.root, co.EXTRACTION)
        self.env = stage_verification(self.root)

    def test_constants_and_required_fields_agree_with_the_runtime(self):
        s = self.occ
        self.assertEqual(s["properties"]["schema_version"]["const"], co.OCC_SCHEMA)
        self.assertEqual(s["properties"]["scanner_version"]["const"], co.SCANNER_VERSION)
        self.assertEqual(s["$id"], co.OCC_SCHEMA)
        self.assertEqual(set(s["required"]), set(co.ENVELOPE_KEYS))
        self.assertEqual(set(s["properties"]), set(co.ENVELOPE_KEYS))
        occ = s["$defs"]["occurrence"]
        self.assertEqual(set(occ["required"]), set(co.OCC_REQUIRED))
        self.assertEqual(set(occ["properties"]), set(co.OCC_REQUIRED) | set(co.OCC_OPTIONAL))
        self.assertEqual(occ["properties"]["directive_kind"]["enum"], list(co.DIRECTIVE_KINDS))
        self.assertEqual(s["properties"]["sources"]["items"]["properties"]["role"]["enum"], list(co.SOURCE_ROLES))
        v = self.ver
        self.assertEqual(v["properties"]["schema_version"]["const"], co.VERDICT_SCHEMA)
        self.assertEqual(v["$id"], co.VERDICT_SCHEMA)
        self.assertEqual(set(v["required"]), set(co.VERIFICATION_ENVELOPE_KEYS))
        vd = v["$defs"]["verdict"]
        self.assertEqual(set(vd["required"]), set(co.VERDICT_REQUIRED))
        self.assertEqual(set(vd["properties"]), set(co.VERDICT_REQUIRED) | set(co.VERDICT_OPTIONAL))
        self.assertEqual(vd["properties"]["category"]["enum"], list(co.CATEGORIES))
        ea = v["$defs"]["evidence_available"]
        self.assertEqual(set(ea["required"]), set(co.EVIDENCE_AVAILABLE_KEYS))
        self.assertEqual(set(ea["properties"]), set(co.EVIDENCE_AVAILABLE_KEYS) | set(co.EVIDENCE_AVAILABLE_OPTIONAL))
        self.assertEqual(ea["properties"]["depth"]["enum"], list(co.DEPTHS))
        self.assertEqual(set(v["$defs"]["evidence_unavailable"]["required"]), set(co.EVIDENCE_UNAVAILABLE_KEYS))
        self.assertEqual(set(v["$defs"]["evidence_unavailable"]["properties"]["status"]["enum"]),
                         set(co.EVIDENCE_STATUSES) - {"available"})
        att = v["$defs"]["attestation"]
        self.assertEqual(set(att["required"]), set(co.ATTESTATION_KEYS))
        self.assertEqual(att["properties"]["protocol"]["const"], co.PROTOCOL)
        self.assertEqual(ea["properties"]["path"]["pattern"], "^%s/.+" % co.EVIDENCE_DIR)

    def test_valid_documents_satisfy_both_schema_and_runtime(self):
        self.assertEqual(schema_errors(self.inv, self.occ, self.occ), [])
        self.assertEqual(schema_errors(self.env, self.ver, self.ver), [])
        self.assertTrue(co.validate_occurrences(self.root)["ok"])
        self.assertTrue(co.validate_verdicts(self.root)["ok"])

    def test_documents_the_runtime_rejects_are_rejected_by_the_schema_too(self):
        def occ(f):
            inv = copy.deepcopy(self.inv)
            f(inv)
            return inv

        for f in (lambda i: i.update(extra=1), lambda i: i["occurrences"][0].update(extra=1),
                  lambda i: i["occurrences"][0].pop("byte_span"), lambda i: i["occurrences"][0].update(byte_span=[1]),
                  lambda i: i["occurrences"][0].update(key_index=-1),
                  lambda i: i["occurrences"][0].update(key_index=True),
                  lambda i: i["occurrences"][0].update(directive_kind="cite:ps"),
                  lambda i: i["occurrences"][0].update(relative_file="../x"),
                  lambda i: i["occurrences"][0].update(occurrence_id="abc"),
                  lambda i: i.update(schema_version="x"), lambda i: i["occurrences"][0].update(line_range=[0, 1])):
            bad = occ(f)
            self.assertNotEqual(schema_errors(bad, self.occ, self.occ), [])
            self.assertNotEqual(co.check_inventory(bad, co.scan_sources(self.root)), [])

        def ver(f):
            env = copy.deepcopy(self.env)
            f(env["verdicts"][0])
            return env

        for f in (lambda v: v.update(extra=1), lambda v: v.pop("details"), lambda v: v.update(category="GREAT"),
                  lambda v: v["evidence"].update(depth="metadata_only"),
                  lambda v: v["evidence"].update(path="elsewhere/x.txt"),
                  lambda v: v["evidence"].update(status="maybe"),
                  lambda v: v["scientific_checks_attestation"].update(steps_completed=[1, 2]),
                  lambda v: v.update(verification_depth="metadata_only")):
            bad = ver(f)
            self.assertNotEqual(schema_errors(bad, self.ver, self.ver), [])
            self.assertNotEqual(co.check_verdicts(self.root, bad, co.scan_sources(self.root)), [])



# ------------------------------------------------- block / container repairs
class ScannerBlockRepairs(unittest.TestCase):
    """Inline matching is block-local; fences belong to the container that opened them."""

    CASES = [
        # (text, expected keys): a stray backtick must not reach across a block boundary
        ("`literal\n# Claim {cite:p}`A`\n", ["A"]),
        ("- `literal\n- Claim {cite:p}`A`\n", ["A"]),
        ("1. `literal\n2. Claim {cite:p}`A`\n", ["A"]),
        ("> `literal\n> # Claim {cite:p}`A`\n", ["A"]),
        ("> `literal\n\nClaim {cite:p}`A`\n", ["A"]),
        ("`literal\n---\nClaim {cite:p}`A`\n", ["A"]),
        ("`literal\n***\n{cite:p}`A`\n", ["A"]),
        ("Title `x\n=====\n{cite:p}`A`\n", ["A"]),
        ("`one\n\n{cite:p}`A`\n", ["A"]),
        # soft breaks inside ONE paragraph still pair code spans (and then hide what they enclose)
        ("`code\nspan` {cite:p}`A`\n", ["A"]),
        ("`open\n{cite:p}`A`\n", []),
        ("see {cite:p}`A,\nB` end\n", ["A", "B"]),
        ("> quoted {cite:p}`A,\n> B`\n", ["A", "B"]),
        ("> quoted\nlazy {cite:p}`A`\n", ["A"]),
        # fences are scoped to the container that opened them
        ("> ```\n> sample\n\nClaim {cite:p}`A`\n\n```text\nnormal\n```\n", ["A"]),
        ("```text\n> ```\n{cite:p}`A`\n> ```\n```\n", []),
        ("```text\n- ```\n{cite:p}`A`\n```\n", []),
        ("```text\n> quote {cite:p}`A`\n> > deeper\n```\n", []),
        ("> > ```\n> > x\n> Claim {cite:p}`A`\n", ["A"]),
        ("> ```\n> ```\n{cite:p}`A`\n", ["A"]),
        ("> ```\n> {cite:p}`B`\n> ```\n{cite:p}`A`\n", ["A"]),
        ("- ```\n  x\n- Claim {cite:p}`A`\n", ["A"]),
        ("- ```\n{cite:p}`A`\n", ["A"]),
        ("- item\n\n  ```\n  {cite:p}`B`\n  ```\n\nafter {cite:p}`A`\n", ["A"]),
        ("1. item\n\n   ~~~\n   {cite:p}`B`\n   ~~~\n\nafter {cite:p}`A`\n", ["A"]),
        ("- a\n  > ```\n  > {cite:p}`B`\n  > ```\n  {cite:p}`A`\n", ["A"]),
        ("> - ```\n>   {cite:p}`B`\n>   ```\n> {cite:p}`A`\n", ["A"]),
        (">\t```\n>\tcode {cite:p}`B`\n>\t```\n{cite:p}`A`\n", ["A"]),
        ("> ```\n>\tcode {cite:p}`B`\n> ```\n{cite:p}`A`\n", ["A"]),
        ("> ````\n> ```\n> {cite:p}`B`\n> ```\n> ````\n{cite:p}`A`\n", ["A"]),
        ("> ```\n> {cite:p}`B`\n\n> ```\n> {cite:p}`C`\n> ```\n{cite:p}`A`\n", ["A"]),
        # a closer indented four columns past its container is code, not a closer
        ("```\n    ```\n{cite:p}`A`\n```\n", []),
        # list item first-line indentation: >= 5 columns after the marker is indented code
        ("-     {cite:p}`A`\n", []),
        ("-      {cite:p}`A`\n", []),
        ("1.     {cite:p}`A`\n", []),
        ("-\t\t{cite:p}`A`\n", []),
        ("- a\n  -     {cite:p}`B`\n  - {cite:p}`A`\n", ["A"]),
        ("- a\n\n      {cite:p}`B`\n", []),
        ("> -     {cite:p}`B`\n> - {cite:p}`A`\n", ["A"]),
        # one to four spaces are ordinary list prose
        ("- {cite:p}`A`\n", ["A"]),
        ("-  {cite:p}`A`\n", ["A"]),
        ("-   {cite:p}`A`\n", ["A"]),
        ("-    {cite:p}`A`\n", ["A"]),
        ("1.    {cite:p}`A`\n", ["A"]),
        ("-\t{cite:p}`A`\n", ["A"]),
        ("- a\n\n     {cite:p}`A`\n", ["A"]),
        ("- a\n    - {cite:p}`A`\n", ["A"]),
        ("- a\n\n  - b\n\n        {cite:p}`B`\n\n    {cite:p}`A`\n", ["A"]),
    ]

    def test_block_local_inline_matching_and_container_scoped_fences(self):
        for text, want in self.CASES:
            with self.subTest(text=text):
                self.assertEqual([k for _, _, k in found(text)], want)

    def test_a_visible_cite_is_never_zero_or_an_explicit_failure(self):
        for text in ("`literal\n# Claim {cite:p}`A`\n", "- `literal\n- Claim {cite:p}`A`\n"):
            self.assertEqual(len(co.scan_markdown(text)), 1)

    def test_roles_in_lists_quotes_headings_keep_exact_byte_spans_and_lines(self):
        text = "é `x\n# Claim ü {cite:p}`A`\n"
        occ = co.scan_markdown(text)[0]
        raw = text.encode("utf-8")
        self.assertEqual(raw[slice(*occ["byte_span"])], "{cite:p}`A`".encode("utf-8"))
        self.assertEqual(occ["line_range"], [2, 2])
        self.assertEqual(occ["context"], "# Claim ü {cite:p}`A`")

    def test_comments_do_not_cross_container_boundaries_silently(self):
        self.assertEqual(found("<!--\n> {cite:p}`B`\n- c\n-->\n{cite:p}`A`\n"), [("cite:p", 0, "A")])
        self.assertEqual(failure("> <!-- open\n\n{cite:p}`A`\n"), "unterminated_comment")
        self.assertEqual(failure("- <!-- open\nnot indented -->\n"), "unterminated_comment")

    def test_directives_close_with_their_container_and_still_fail_closed(self):
        self.assertEqual(found("> :::{note}\n> inner {cite:p}`B`\n> :::\n{cite:p}`A`\n"),
                         [("cite:p", 0, "B"), ("cite:p", 0, "A")])
        self.assertEqual(failure("> ```{include} x.md\n\n{cite:p}`A`\n"), "unsupported_include")
        self.assertEqual(found("> :::{weird}\n> plain\n\n{cite:p}`A`\n"), [("cite:p", 0, "A")])
        self.assertEqual(failure("> :::{weird}\n> {cite:p}`B`\n"), "unsupported_directive_citation")


# ------------------------------------------------- fail-closed traversal
def _unreadable(case, path):
    """chmod 000 `path` (restored on cleanup); True if the OS then really refuses to list/read it."""
    os.chmod(str(path), 0)
    case.addCleanup(os.chmod, str(path), 0o755)
    try:
        if path.is_dir():
            os.listdir(str(path))
        else:
            path.read_bytes()
    except OSError:
        return True
    return False


class TraversalFailsClosed(Tmp):
    def tree(self):
        self.text("content/a.md", "Readable {cite:p}`A`.\n")
        self.text("content/a.bib", "@misc{A, title={A}}\n")
        self.text("content/private/hidden.md", "Hidden {cite:p}`A`.\n")
        self.text("content/private/deep/hidden.bib", "@misc{Z, title={Z}}\n")

    def refused(self, fn):
        with self.assertRaises(co.CitationError) as ctx:
            fn()
        self.assertEqual(codes(ctx.exception.diagnostics), ["unreadable_source"])
        self.assertTrue(any("private" in d.get("file", "") for d in ctx.exception.diagnostics))
        return ctx.exception

    def assert_nothing_certified(self, cli=True):
        self.refused(lambda: co.scan_sources(self.root))
        self.refused(lambda: co.create_snapshot(self.root))
        self.assertFalse((self.root / co.SNAPSHOT_DIR).exists())
        with self.assertRaises(co.CitationError):  # no snapshot exists, so there is nothing to cover
            co.write_coverage(self.root)
        self.assertFalse((self.root / co.COVERAGE).exists())
        for cmd in ("extract", "snapshot", "validate-occurrences") if cli else ():
            out = subprocess.run([sys.executable, str(ROOT / "scripts/citation_occurrences.py"), cmd, "--root",
                                  str(self.root)], capture_output=True, text=True)
            self.assertEqual(out.returncode, 1, cmd)
            self.assertEqual(codes(json.loads(out.stdout)["errors"]), ["unreadable_source"], cmd)
        self.assertFalse((self.root / co.EXTRACTION).exists())

    def test_unreadable_subtree_is_refused_for_sources_and_bibliographies_real_permissions(self):
        self.tree()
        if not _unreadable(self, self.root / "content/private"):
            self.skipTest("this user can read a mode-000 directory (root or an ACL-less filesystem)")
        self.assert_nothing_certified()

    def test_unreadable_subtree_is_refused_even_when_permissions_cannot_be_dropped(self):
        self.tree()
        real = os.scandir
        blocked = str(self.root / "content/private")

        def scandir(path=".", *a, **k):
            if os.fspath(path) == blocked:
                raise PermissionError(13, "Permission denied", blocked)
            return real(path, *a, **k)

        with mock.patch.object(os, "scandir", scandir):
            self.assert_nothing_certified(cli=False)
            self.assertFalse(co.validate_occurrences(self.root)["ok"])

    def test_an_earlier_valid_snapshot_does_not_make_a_later_subset_acceptable(self):
        self.tree()
        shutil.rmtree(str(self.root / "content/private"))
        stage_extraction(self.root)
        self.text("content/private/hidden.md", "Hidden {cite:p}`A`.\n")
        real = os.scandir
        blocked = str(self.root / "content/private")

        def scandir(path=".", *a, **k):
            if os.fspath(path) == blocked:
                raise PermissionError(13, "Permission denied", blocked)
            return real(path, *a, **k)

        with mock.patch.object(os, "scandir", scandir):
            with self.assertRaises(co.CitationError) as ctx:
                co.check_stage(self.root, live=True)
            self.assertEqual(codes(ctx.exception.diagnostics), ["unreadable_source"])
            self.assertFalse(co.validate_occurrences(self.root)["ok"])

    def test_historical_snapshot_walk_fails_closed(self):
        self.tree()
        shutil.rmtree(str(self.root / "content/private"))
        stage_extraction(self.root)
        sub = self.root / co.SNAPSHOT_DIR / "files/content"
        real = os.scandir

        def scandir(path=".", *a, **k):
            if os.fspath(path) == str(sub):
                raise PermissionError(13, "Permission denied", str(sub))
            return real(path, *a, **k)

        with mock.patch.object(os, "scandir", scandir):
            with self.assertRaises(co.CitationError) as ctx:
                co.verify_snapshot(self.root / co.SNAPSHOT_DIR)
            self.assertEqual(codes(ctx.exception.diagnostics), ["unreadable_snapshot"])
            rep = co.audit_historical(self.root)
            self.assertFalse(rep["ok"])
            self.assertEqual(codes(rep["errors"]), ["unreadable_snapshot"])

    def test_historical_snapshot_walk_fails_closed_real_permissions(self):
        self.tree()
        shutil.rmtree(str(self.root / "content/private"))
        stage_extraction(self.root)
        sub = self.root / co.SNAPSHOT_DIR / "files/content"
        if not _unreadable(self, sub):
            self.skipTest("this user can read a mode-000 directory")
        rep = co.audit_historical(self.root)
        self.assertFalse(rep["ok"])
        self.assertTrue({"unreadable_snapshot", "snapshot_file_missing"} & set(codes(rep["errors"])))


# ------------------------------------------- malformed verdict identifiers
class VerdictIdentifierShapes(Tmp):
    BAD = [[], {}, True, False, None, 5, 1.5, "", "abc", "A" * 64, ["a" * 64], {"a": 1}, [[]]]

    def setUp(self):
        super().setUp()
        make_manuscript(self.root)
        stage_extraction(self.root)
        self.env = stage_verification(self.root)
        self.scan = co.scan_sources(self.root)

    def test_any_non_identifier_value_is_a_structured_diagnostic_never_a_crash(self):
        for bad in self.BAD:
            with self.subTest(occurrence_id=bad):
                env = copy.deepcopy(self.env)
                env["verdicts"][0]["occurrence_id"] = bad
                d = co.check_verdicts(self.root, env, self.scan)
                self.assertIn("malformed_verdict", codes(d))
                self.assertIn("missing_verdict", codes(d))
                json.dumps(d)

    def test_cli_prints_json_and_exits_one_for_each_bad_identifier(self):
        for bad in ([], {}, True, None, 5, "abc"):
            with self.subTest(occurrence_id=bad):
                env = copy.deepcopy(self.env)
                env["verdicts"][0]["occurrence_id"] = bad
                write_json(self.root, co.VERIFICATION, env)
                out = subprocess.run([sys.executable, str(ROOT / "scripts/citation_occurrences.py"),
                                      "validate-verdicts", "--root", str(self.root)], capture_output=True, text=True)
                self.assertEqual(out.returncode, 1, out.stderr)
                self.assertEqual(out.stderr, "")
                rep = json.loads(out.stdout)
                self.assertFalse(rep["ok"])
                self.assertIn("malformed_verdict", codes(rep["errors"]))

    def test_other_non_dict_verdict_entries_are_malformed_not_crashes(self):
        for bad in ([], "x", None, 3):
            env = copy.deepcopy(self.env)
            env["verdicts"].append(bad)
            self.assertIn("malformed_verdict", codes(co.check_verdicts(self.root, env, self.scan)))

    def test_unexpected_internal_errors_still_yield_json_and_exit_one(self):
        with mock.patch.object(co, "validate_verdicts", side_effect=RuntimeError("boom")):
            with mock.patch("sys.stdout") as out:
                rc = co.main(["validate-verdicts", "--root", str(self.root)])
        self.assertEqual(rc, 1)
        self.assertIn("internal_error", "".join(c.args[0] for c in out.write.call_args_list))


# ------------------------------------------------ schema / runtime parity
class ContainerRelativeIndentation(unittest.TestCase):
    """List continuation is measured from the parent container's content column, never the physical column."""

    CASES = [
        # (text, expected keys): known renderings, so CI needs no markdown parser
        ("> - ```\n  > {cite:p}`A`\n> ```\n> ```\n", ["A"]),
        ("  > - ```\n>   {cite:p}`A`\n> ```\n> ```\n", []),
        ("> - ```\n>   {cite:p}`A`\n>   ```\n", []),
        ("> - x\n>   {cite:p}`A`\n", ["A"]),
        ("> - ```\n>  {cite:p}`A`\n> ```\n> ```\n", ["A"]),
        ("> > - ```\n> >   {cite:p}`A`\n> >   ```\n", []),
        ("> 1. ```\n>    {cite:p}`A`\n>    ```\n", []),
        ("> 1. ```\n>   {cite:p}`A`\n> ```\n> ```\n", ["A"]),
        ("- > ```\n  > {cite:p}`A`\n  > ```\n", []),
        ("-\t```\n\t{cite:p}`A`\n\t```\n", []),
        (">\t- ```\n>\t  {cite:p}`A`\n>\t  ```\n", []),
    ]

    def test_known_renderings(self):
        for text, keys in self.CASES:
            with self.subTest(text=text):
                self.assertEqual([k for _, _, k in found(text)], keys)

    def test_ambiguous_indented_quote_under_an_unmatched_list_item_is_unsupported(self):
        self.assertEqual(failure("-    > x\n    > {cite:p}`A`\n"), "unsupported_container_layout")

    def test_scanner_version_two_changes_the_revision_of_identical_sources(self):
        self.assertEqual(co.SCANNER_VERSION, "comprev.citation_scanner/3")
        with tempfile.TemporaryDirectory() as d:
            make_manuscript(d)
            r2 = co.scan_sources(d).revision
            with mock.patch.object(co, "SCANNER_VERSION", "comprev.citation_scanner/1"):
                self.assertNotEqual(co.scan_sources(d).revision, r2)

    def test_agrees_with_a_reference_parser_over_a_container_matrix(self):
        try:
            from markdown_it import MarkdownIt
        except ImportError:
            self.skipTest("markdown_it is a development-time oracle only")
        md = MarkdownIt("commonmark")
        key = re.compile(r"\{cite:p\}`A`")
        unsupported = checked = 0
        for text in MATRIX_TEXTS():
            expected = sum(len(key.findall(t.content)) for t in md.parse(text) if t.type == "inline")
            try:
                got = len(co.scan_markdown(text))
            except co._ScanFail:
                unsupported += 1  # explicit refusal is always allowed; silent disagreement is not
                continue
            checked += 1
            self.assertEqual(got, expected, repr(text))
        self.assertGreater(checked, 500)
        self.assertGreater(unsupported, 0)


MATRIX_OUTER = ["> ", ">", "  > ", "- ", "-  ", "-   ", "-    ", "1. ", "10. ", "1.  ", "> - ", "> -  ", "  > - ",
                "> > ", "- > ", "> 1. ", "- - ", "\t", ">\t", "-\t", "> - - ", "- 1. ", "   - ", "    "]
MATRIX_PRE = ["", " ", "  ", "   ", "    ", "     ", "      ", "        ", "\t", " \t", "> ", ">  ", ">   ", ">    ",
              ">\t", "> > ", "  - ", "\t> ", "    > ", "> \t"]


def MATRIX_TEXTS():
    """Fence payload / closer prefix matrix (the cite and a cite-free ambiguous line vary independently)."""
    for o in MATRIX_OUTER:
        for pf in MATRIX_PRE:
            for pf2 in MATRIX_PRE:
                yield o + "```\n" + pf + "{cite:p}`A`\n" + pf2 + "```\n"
    for o in MATRIX_OUTER[:20]:
        for pf in MATRIX_PRE:
            for pf2 in MATRIX_PRE[::3]:
                # a cite-free ambiguous line first; the cite sits in a later, otherwise ordinary line
                yield o + "x\n" + pf + "ambiguous\n" + "\n" + pf2 + "{cite:p}`A`\n"
                yield o + "```\n" + pf + "ambiguous\n" + "> ```\n" + "{cite:p}`A`\n"


class IndentedUnmatchedContinuationRefusal(unittest.TestCase):
    """Fixed regression family: an indented non-blank line that matches only some of its open containers is
    refused before any fence or paragraph state is lost (hardcoded, so CI needs no markdown parser)."""

    REFUSED = [
        "> - ```\n    > {cite:p}`A`\n> ```\n> ```\n",           # previously scanned 0, renders a paragraph
        "> > normal payload\n    ```\n> > {cite:p}`A`\n    ```\n",  # multi-line inline code across the gap
        "> - ```\n    ambiguous with no cite\n> ```\n> {cite:p}`A`\n",  # cite-free ambiguous line
        "> - x\n\t{cite:p}`A`\n",
        "> - ```\n\t> {cite:p}`A`\n> ```\n> ```\n",
        ">\t- ```\n    > {cite:p}`A`\n>\t```\n",
        "> > - ```\n      ambiguous\n> > ```\n> > {cite:p}`A`\n",
        "> 1. ```\n     ambiguous\n> ```\n{cite:p}`A`\n",
        "- > ```\n      ambiguous\n  > ```\n\n{cite:p}`A`\n",
        "> a\n    ambiguous\n\n{cite:p}`A`\n",
    ]

    def test_refused_with_the_right_line(self):
        for text in self.REFUSED:
            with self.subTest(text=text):
                with self.assertRaises(co._ScanFail) as ctx:
                    co.scan_markdown(text)
                self.assertEqual(ctx.exception.code, "unsupported_container_layout")
                self.assertEqual(text.splitlines()[ctx.exception.line - 1].strip() != "", True)

    def test_normal_same_container_layouts_stay_supported(self):
        for text, n in [("> - ```\n>   {cite:p}`A`\n>   ```\n", 0), ("> x {cite:p}`A`\n", 1),
                        ("- a\n  {cite:p}`A`\n", 1), ("- a\n    - b {cite:p}`A`\n", 1),
                        ("> > a\n> > {cite:p}`A`\n", 1), ("```\n    {cite:p}`A`\n```\n", 0),
                        ("> ```\n>     {cite:p}`A`\n> ```\n", 0), ("    {cite:p}`A`\n", 0),
                        ("1. a\n\n   {cite:p}`A`\n", 1), ("> a\n> b `x\n> {cite:p}`A` y`\n", 0)]:
            with self.subTest(text=text):
                self.assertEqual(len(co.scan_markdown(text)), n)


class NullableOptionalSchema(Tmp):
    """Optional evidence / attestation: runtime and schema agree on null, absent and VERIFIED."""

    def setUp(self):
        super().setUp()
        self.ver = json.loads((ROOT / "provenance/citation_verdicts.schema.json").read_text())
        make_manuscript(self.root)
        stage_extraction(self.root)
        self.env = stage_verification(self.root)
        self.scan = co.scan_sources(self.root)

    def both(self, env):
        return co.check_verdicts(self.root, env, self.scan), schema_errors(env, self.ver, self.ver)

    def non_verified(self):
        env = copy.deepcopy(self.env)
        v = env["verdicts"][0]
        v.update(category="MISATTRIBUTED", failed_at_step=2, verification_depth=None, supporting_passage=None,
                 details="no retrievable text; manual review needed")
        v.pop("evidence", None)
        v.pop("scientific_checks_attestation", None)
        return env, v

    def test_non_verified_with_null_or_absent_optionals_is_valid_in_both(self):
        for ev in ("absent", None):
            for at in ("absent", None):
                with self.subTest(evidence=ev, attestation=at):
                    env, v = self.non_verified()
                    if ev == "absent" and at == "absent":
                        pass
                    else:
                        v["evidence"], v["scientific_checks_attestation"] = None, None
                    self.assertEqual(self.both(env), ([], []))

    def test_verified_requires_non_null_evidence_and_attestation_in_both(self):
        for field in ("evidence", "scientific_checks_attestation"):
            for how in ("null", "absent"):
                with self.subTest(field=field, how=how):
                    env = copy.deepcopy(self.env)
                    v = next(x for x in env["verdicts"] if x["category"] == "VERIFIED")
                    if how == "null":
                        v[field] = None
                    else:
                        v.pop(field)
                    runtime, schema = self.both(env)
                    self.assertNotEqual(runtime, [])
                    self.assertNotEqual(schema, [])

    def test_unchanged_verified_fixture_still_passes_both(self):
        self.assertEqual(self.both(self.env), ([], []))


class OptionalFieldParity(Tmp):
    def setUp(self):
        super().setUp()
        self.occ = json.loads((ROOT / "provenance/citation_occurrences.schema.json").read_text())
        self.ver = json.loads((ROOT / "provenance/citation_verdicts.schema.json").read_text())
        make_manuscript(self.root)
        stage_extraction(self.root)
        self.inv = read_json(self.root, co.EXTRACTION)
        self.env = stage_verification(self.root)
        self.scan = co.scan_sources(self.root)

    def inventory_verdicts(self, f, first_only=False):
        inv = copy.deepcopy(self.inv)
        for r in inv["occurrences"][:1] if first_only else inv["occurrences"]:
            f(r)
        runtime = co.check_inventory(inv, self.scan)
        schema = schema_errors(inv, self.occ, self.occ)
        return runtime, schema

    def test_optional_context_and_bib_entry_may_be_omitted_in_both(self):
        for drop in (("context",), ("bib_entry",), ("context", "bib_entry"), ("claimed_finding",)):
            with self.subTest(drop=drop):
                runtime, schema = self.inventory_verdicts(lambda r: [r.pop(k, None) for k in drop])
                self.assertEqual((runtime, schema), ([], []))

    def test_required_core_fields_still_compare_exactly(self):
        for field, value in (("line_range", [1, 1]), ("citation_key", "Nope"), ("directive_kind", "cite:t"),
                             ("byte_span", [0, 1])):
            with self.subTest(field=field):
                runtime, _ = self.inventory_verdicts(lambda r: r.update({field: value}), first_only=True)
                self.assertNotEqual(runtime, [])

    def test_optional_fields_that_are_present_must_equal_the_authentic_content(self):
        runtime, schema = self.inventory_verdicts(lambda r: r.update(context=r["context"] + " forged"))
        self.assertIn("occurrence_mismatch", codes(runtime))
        self.assertEqual(schema, [])  # shape is valid; only the rescan can know it is false

        def forge(r):
            r["bib_entry"] = dict(r["bib_entry"], text="@misc{Z, title={forged}}")

        runtime, schema = self.inventory_verdicts(forge)
        self.assertIn("occurrence_mismatch", codes(runtime))

    def test_optional_field_types_are_enforced_by_runtime_and_schema_alike(self):
        for field, value in (("context", 5), ("context", []), ("claimed_finding", 5), ("bib_entry", []),
                             ("bib_entry", {"file": "x"}), ("bib_entry", {"file": "x", "byte_span": [1], "text": "t"}),
                             ("bib_entry", {"file": "x", "byte_span": [-1, 2], "text": "t"})):
            with self.subTest(field=field, value=value):
                runtime, schema = self.inventory_verdicts(lambda r: r.update({field: value}))
                self.assertNotEqual(runtime, [])
                self.assertNotEqual(schema, [])

    def evidence_env(self, depth, f):
        env = copy.deepcopy(self.env)
        v = next(x for x in env["verdicts"] if x["verification_depth"] == depth)
        f(v["evidence"])
        return env

    def test_fulltext_unavailable_reason_type_is_enforced_at_every_depth(self):
        for depth in ("fulltext", "abstract_only"):
            for bad in ([], {}, 5, None, True, ""):
                with self.subTest(depth=depth, reason=bad):
                    env = self.evidence_env(depth, lambda ev: ev.update(fulltext_unavailable_reason=bad))
                    self.assertNotEqual(co.check_verdicts(self.root, env, self.scan), [])
                    self.assertNotEqual(schema_errors(env, self.ver, self.ver), [])
            with self.subTest(depth=depth, reason="ok"):
                env = self.evidence_env(depth, lambda ev: ev.update(fulltext_unavailable_reason="paywalled; no OA copy"))
                self.assertEqual(co.check_verdicts(self.root, env, self.scan), [])
                self.assertEqual(schema_errors(env, self.ver, self.ver), [])

    def test_fulltext_without_a_reason_and_abstract_with_one_both_remain_valid(self):
        self.assertEqual(co.check_verdicts(self.root, self.env, self.scan), [])
        self.assertEqual(schema_errors(self.env, self.ver, self.ver), [])
        env = self.evidence_env("abstract_only", lambda ev: ev.pop("fulltext_unavailable_reason"))
        self.assertEqual(codes(co.check_verdicts(self.root, env, self.scan)), ["fulltext_fallback_unexplained"])
        self.assertNotEqual(schema_errors(env, self.ver, self.ver), [])


if __name__ == "__main__":
    unittest.main()
