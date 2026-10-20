#!/usr/bin/env python3
"""Deterministic citation-occurrence extraction and exact coverage checks.

    python scripts/citation_occurrences.py extract [--root DIR] [--out FILE|-]
    python scripts/citation_occurrences.py snapshot [--root DIR] [--supersede]
    python scripts/citation_occurrences.py coverage [--root DIR]
    python scripts/citation_occurrences.py validate-occurrences [--root DIR] [--file FILE]
    python scripts/citation_occurrences.py validate-verdicts [--root DIR] [--file FILE]
    python scripts/citation_occurrences.py audit-historical --snapshot DIR [--root DIR]

Standard library only, Python >= 3.9, no network, no LLM and no `assert` (so `python -O` cannot
weaken it). Every command prints one JSON diagnostics object
(`comprev.citation_diagnostics/1`: `ok`, `errors[{code, message, ...}]`) and exits 1 on failure.

What this proves, and what it does not. It proves *mechanical* facts: which `{cite:p}` /
`{cite:t}` occurrences exist in the manuscript sources (found by an independent rescan, never
taken from a submitted inventory), that a submitted inventory lists exactly those, that there is
exactly one verdict per occurrence, that a VERIFIED verdict points at local evidence text whose
hash and passage still match, and that nothing was edited since. It NEVER proves that a claim is
true or that a cited paper supports it: that is the independent LITREVIEW Phase 16 protocol
(`skills/comprev-verification.md`). Verdict attestations are recorded, not trusted as science.

v1 scope: see docs/citation-occurrences.md.
"""
import argparse
import bisect
import hashlib
import json
import os
import re
import shutil
import string
import sys
import tempfile
from collections import Counter
from pathlib import Path, PurePosixPath

SCANNER_VERSION = "comprev.citation_scanner/2"
OCC_SCHEMA = "comprev.citation_occurrences/1"
VERDICT_SCHEMA = "comprev.citation_verification/1"
COVERAGE_SCHEMA = "comprev.citation_coverage/1"
SNAPSHOT_SCHEMA = "comprev.citation_snapshot/1"
DIAG_SCHEMA = "comprev.citation_diagnostics/1"

EXTRACTION = "provenance/citation_triples.json"
SNAPSHOT_DIR = "provenance/citation_snapshot"
COVERAGE = "provenance/citation_coverage.json"
VERIFICATION = "provenance/citation_verification.json"
EVIDENCE_DIR = "provenance/citation_evidence"

MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_EVIDENCE_BYTES = 64 * 1024 * 1024
MAX_DIAGNOSTICS = 200

DIRECTIVE_KINDS = ("cite:p", "cite:t")
CATEGORIES = ("VERIFIED", "MINOR", "CHIMERIC", "HALLUCINATED", "MISATTRIBUTED")
DEPTHS = ("fulltext", "abstract_only")
EVIDENCE_STATUSES = ("available", "unavailable", "unverifiable")
PROTOCOL = "comprev-verification/phase-16"

OCC_REQUIRED = ("schema_version", "manuscript_revision", "occurrence_id", "relative_file",
                "byte_span", "line_range", "directive_kind", "key_index", "citation_key")
OCC_OPTIONAL = ("context", "bib_entry", "claimed_finding")
ENVELOPE_KEYS = ("schema_version", "scanner_version", "manuscript_revision", "sources",
                 "occurrence_count", "extraction_digest", "occurrences")
SOURCE_KEYS = ("path", "role", "bytes", "sha256")
SOURCE_ROLES = ("markdown", "bibliography", "include")
VERIFICATION_ENVELOPE_KEYS = ("schema_version", "manuscript_revision", "extraction_digest", "verdicts")
VERDICT_REQUIRED = ("occurrence_id", "manuscript_revision", "cite_key", "category", "failed_at_step",
                    "verification_depth", "supporting_passage", "details")
VERDICT_OPTIONAL = ("correct_metadata", "suggested_action", "evidence", "scientific_checks_attestation")
EVIDENCE_AVAILABLE_KEYS = ("status", "depth", "citation_key", "path", "sha256", "passage_span")
EVIDENCE_AVAILABLE_OPTIONAL = ("fulltext_unavailable_reason",)
EVIDENCE_UNAVAILABLE_KEYS = ("status", "citation_key", "reason")
ATTESTATION_KEYS = ("protocol", "steps_completed", "attested_by")

_HEX64 = re.compile(r"[0-9a-f]{64}")


# ---------------------------------------------------------------- diagnostics
class CitationError(Exception):
    """Carries a list of machine-readable diagnostics; every failure means 'do not proceed'."""

    def __init__(self, diagnostics):
        self.diagnostics = list(diagnostics)
        first = self.diagnostics[0] if self.diagnostics else {"code": "unknown", "message": ""}
        super().__init__("%s: %s" % (first["code"], first["message"]))


def diag(code, message, **where):
    d = {"code": code, "message": message}
    d.update({k: v for k, v in where.items() if v is not None})
    return d


def _fail(code, message, **where):
    raise CitationError([diag(code, message, **where)])


def report(command, diagnostics, **extra):
    diagnostics = list(diagnostics)
    shown = diagnostics[:MAX_DIAGNOSTICS]
    if len(diagnostics) > MAX_DIAGNOSTICS:
        shown.append(diag("diagnostics_truncated", "%d further diagnostics omitted"
                          % (len(diagnostics) - MAX_DIAGNOSTICS)))
    out = {"schema_version": DIAG_SCHEMA, "command": command, "ok": not diagnostics,
           "error_count": len(diagnostics), "errors": shown}
    out.update(extra)
    return out


class _ScanFail(Exception):
    def __init__(self, code, message, line=None):
        super().__init__(message)
        self.code, self.message, self.line = code, message, line


# ----------------------------------------------------------------- primitives
def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def canonical_bytes(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _int(v):
    return type(v) is int


def _hex(v):
    return isinstance(v, str) and _HEX64.fullmatch(v) is not None


def _nonempty(v):
    return isinstance(v, str) and v.strip() != ""


def occurrence_id(revision, rel, span, key_index):
    return sha256_hex(canonical_bytes({"schema_version": OCC_SCHEMA, "manuscript_revision": revision,
                                       "relative_file": rel, "byte_span": list(span), "key_index": key_index}))


def loads_strict(text):
    def pairs(items):
        d = {}
        for k, v in items:
            if k in d:
                raise ValueError("duplicate key %r" % k)
            d[k] = v
        return d

    def bad(c):
        raise ValueError("non-finite constant " + c)

    return json.loads(text, object_pairs_hook=pairs, parse_constant=bad)


def load_json_file(path, label):
    """Strict JSON object from `path`, or CitationError (invalid_json / missing_file)."""
    p = Path(path)
    if p.is_symlink():
        _fail("symlink_refused", "%s is a symlink" % label, file=str(label))
    try:
        if not p.is_file() or p.stat().st_size > MAX_JSON_BYTES:
            _fail("missing_file", "%s is missing or not a regular file of acceptable size" % label, file=str(label))
        raw = p.read_bytes()
    except OSError as e:
        _fail("unreadable_file", "%s: %s" % (label, e), file=str(label))
    return loads_bytes(raw, label), raw


def loads_bytes(raw, label):
    try:
        if raw.startswith(b"\xef\xbb\xbf"):
            raise ValueError("byte-order mark")
        obj = loads_strict(raw.decode("utf-8"))
    except (ValueError, RecursionError) as e:
        _fail("invalid_json", "%s is not strict JSON: %s" % (label, e), file=str(label))
    if not isinstance(obj, dict):
        _fail("not_an_object", "%s must contain one JSON object" % label, file=str(label))
    return obj


def json_text(obj):
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def write_atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
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


def _safe_rel(rel):
    """True for a normalised relative posix path that stays inside its root."""
    if not isinstance(rel, str) or not rel or "\\" in rel or "\x00" in rel:
        return False
    p = PurePosixPath(rel)
    return not p.is_absolute() and ".." not in p.parts and str(p) == rel and "." not in p.parts


def _walk_no_symlink(root, rel):
    """Return the Path of root/rel after checking that no component is a symlink."""
    cur = Path(root)
    for part in PurePosixPath(rel).parts:
        cur = cur / part
        if cur.is_symlink():
            raise _ScanFail("symlink_refused", "%s: %s is a symlink; refusing to follow it" % (rel, part))
    return cur


# ------------------------------------------------------------------ discovery
def _read_source(root, rel):
    """Bytes of root/rel under the source rules, or _ScanFail."""
    p = _walk_no_symlink(root, rel)
    try:
        if not p.is_file():
            raise _ScanFail("unreadable_source", "%s is not a regular file" % rel)
        if p.stat().st_size > MAX_SOURCE_BYTES:
            raise _ScanFail("source_too_large", "%s exceeds %d bytes" % (rel, MAX_SOURCE_BYTES))
        raw = p.read_bytes()
    except OSError as e:
        raise _ScanFail("unreadable_source", "%s: %s" % (rel, e))
    if raw.startswith(b"\xef\xbb\xbf"):
        raise _ScanFail("bom_not_allowed", "%s starts with a UTF-8 byte-order mark" % rel)
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise _ScanFail("invalid_utf8", "%s is not valid UTF-8: %s" % (rel, e))
    return raw


def discover(root):
    """Fixed, canonical discovery: content/**/*.md, content/**/*.bib and root *.bib.

    Returns (list of (rel, role), diagnostics). No configuration can exclude a path.
    """
    root = Path(root)
    found, errors = [], []
    content = root / "content"
    if content.is_symlink() or not content.is_dir():
        return [], [diag("missing_source_root", "%s has no readable content/ directory (symlinks are refused)" % root)]
    def walk_failed(err):
        try:
            where = Path(err.filename).relative_to(root).as_posix()
        except (TypeError, ValueError):
            where = str(err.filename or "content")
        errors.append(diag("unreadable_source", "cannot list %s: %s; the sources there would be silently "
                           "omitted" % (where, err.strerror or err), file=where))

    for dirpath, dirnames, filenames in os.walk(str(content), followlinks=False, onerror=walk_failed):
        dirnames.sort()
        for d in list(dirnames):
            if os.path.islink(os.path.join(dirpath, d)):
                rel = Path(dirpath, d).relative_to(root).as_posix()
                errors.append(diag("symlink_refused", "%s is a symlink; refusing to follow it" % rel, file=rel))
                dirnames.remove(d)
        for f in sorted(filenames):
            full = Path(dirpath, f)
            rel = full.relative_to(root).as_posix()
            suffix = full.suffix.lower()
            if suffix not in (".md", ".bib"):
                continue
            if full.is_symlink():
                errors.append(diag("symlink_refused", "%s is a symlink; refusing to follow it" % rel, file=rel))
                continue
            found.append((rel, "markdown" if suffix == ".md" else "bibliography"))
    try:
        for entry in sorted(os.listdir(str(root))):
            full = root / entry
            if entry.lower().endswith(".bib") and (full.is_symlink() or full.is_file()):
                if full.is_symlink():
                    errors.append(diag("symlink_refused", "%s is a symlink; refusing to follow it" % entry, file=entry))
                else:
                    found.append((entry, "bibliography"))
    except OSError as e:
        errors.append(diag("unreadable_source", "cannot list %s: %s" % (root, e)))
    return sorted(found), errors


# ----------------------------------------------------------- markdown scanner
_ASCII_PUNCT = set(string.punctuation)
_ROLE_RE = re.compile(r"\{([A-Za-z][A-Za-z0-9_:.+\-]*)\}")
_AUTOLINK_RE = re.compile(r"<[A-Za-z][A-Za-z0-9+.\-]{1,31}:[^\s<>]*>")
_TAG_RE = re.compile(r"</?[A-Za-z][^<>]*>")
_BARE_AT_RE = re.compile(r"(?<![\w@/.:\\\-])@[A-Za-z][\w:.\-]*")
_CITE_SYNTAX_RE = re.compile(r"\{cite[:}]|\[@")
_KEY_RE = re.compile(r"[\w][\w:.+/\-]*")
_FENCE_RE = re.compile(r"^(`{3,}|~{3,}|:{3,})(.*)$")
_BACKTICK_FENCE_RE = re.compile(r"^(`{3,})([^`]*)$")
_DIRECTIVE_RE = re.compile(r"^\{([^{}\s]+)\}[ \t]*(.*)$")
_REFDEF_RE = re.compile(r"^\[(?!\^)(?:[^\]\\]|\\.)+\]:[ \t]*\S")
_LIST_RE = re.compile(r"([-*+]|\d{1,9}[.)])([ \t]+|$)")
_HEADING_RE = re.compile(r"#{1,6}(?:[ \t]|$)")
_BREAK_RE = re.compile(r"([-*_])(?:[ \t]*\1){2,}$")
_SETEXT_RE = re.compile(r"(?:=+|-+)$")
_OPTION_RE = re.compile(r"^:([A-Za-z][\w\-]*):[ \t]*(.*)$")
_TERMINATOR_RE = re.compile(r"\r\n|\n|\r")

# Directive bodies are Markdown prose and are scanned.
ADMONITIONS = frozenset(["note", "warning", "tip", "important", "caution", "danger", "attention", "hint",
                         "error", "seealso", "admonition", "versionadded", "versionchanged", "deprecated"])
PROSE_DIRECTIVES = ADMONITIONS | frozenset([
    "figure", "image", "table", "list-table", "card", "grid", "grid-item", "grid-item-card", "tab-set",
    "tab-item", "dropdown", "aside", "sidebar", "topic", "epigraph", "pull-quote", "margin", "div",
    "container", "only", "glossary", "abstract", "proof", "theorem", "lemma", "definition", "corollary",
    "proposition", "remark", "example", "exercise", "solution"])
# Prose directives whose first-line argument is a title (prose) rather than a path.
ARGUMENT_IS_PROSE = frozenset(["admonition", "dropdown", "card", "tab-item", "grid-item-card", "topic",
                               "sidebar", "proof", "theorem", "lemma", "definition", "corollary",
                               "proposition", "remark", "example", "exercise"])
# Directive bodies that are code or data, never scanned.
CODE_DIRECTIVES = frozenset(["code", "code-block", "sourcecode", "code-cell", "literalinclude", "math",
                             "raw", "mermaid", "eval-rst", "bibliography", "toctree", "contents", "index",
                             "plot", "iframe", "embed"])
INCLUDE_OPTIONS = ("start-after", "end-before", "literal", "code", "lang", "language")


def _is_prose_directive(name):
    return name in PROSE_DIRECTIVES or name.startswith("prf:")


def _lines_of(text):
    """[(start, end)] char offsets of each line, terminator excluded."""
    out, pos = [], 0
    for m in _TERMINATOR_RE.finditer(text):
        out.append((pos, m.start()))
        pos = m.end()
    if pos < len(text):
        out.append((pos, len(text)))
    return out


class _Offsets:
    def __init__(self, text, lines):
        self.text = text
        self.starts = [s for s, _ in lines]
        self.bytes = []
        total = 0
        for i, (s, _) in enumerate(lines):
            self.bytes.append(total)
            nxt = lines[i + 1][0] if i + 1 < len(lines) else len(text)
            total += len(text[s:nxt].encode("utf-8"))

    def line_of(self, idx):
        return max(0, bisect.bisect_right(self.starts, idx) - 1)

    def byte_of(self, idx):
        li = self.line_of(idx)
        return self.bytes[li] + len(self.text[self.starts[li]:idx].encode("utf-8"))


def _skip_ws(raw, i, col):
    """Advance over spaces/tabs from raw[i] at absolute column `col`; tabs stop at multiples of 4."""
    n = len(raw)
    while i < n and raw[i] in " \t":
        col += (4 - col % 4) if raw[i] == "\t" else 1
        i += 1
    return i, col


def _quote_step(raw, j, c):
    """After a `>` at raw[j] (column c): (resume_index, resume_col, content_base_col)."""
    nxt = raw[j + 1:j + 2]
    if nxt == " ":
        return j + 2, c + 2, c + 2
    if nxt == "\t":
        return j + 1, c + 1, c + 2
    return j + 1, c + 1, c + 1


def _list_start(raw, j, c):
    """A list marker at raw[j] (column c): (content_col, resume_index, resume_col, may_interrupt) or None.

    Four or fewer columns after the marker put the content at the first non-blank column; five or more
    (or none) put it one column after the marker, so any further indentation is indented code.
    """
    m = _LIST_RE.match(raw, j)
    if not m:
        return None
    marker = m.group(1)
    if marker in ("-", "*", "+") and re.fullmatch(r"([-*+])(?:[ \t]*\1){2,}[ \t]*", raw[j:]):
        return None
    mend, mcol = j + len(marker), c + len(marker)
    j2, c2 = _skip_ws(raw, mend, mcol)
    if not raw[j2:].strip():
        return mcol + 1, len(raw), mcol + 1, False
    interrupts = marker in ("-", "*", "+") or int(marker[:-1]) == 1
    if c2 - mcol >= 5:
        return mcol + 1, mend, mcol, interrupts
    return c2, j2, c2, interrupts


def _has_cite_syntax(s):
    return _CITE_SYNTAX_RE.search(s) is not None


class _Inline:
    """Scans one paragraph (virtual text) for citation roles."""

    def __init__(self, virt, mapper):
        self.v, self.map = virt, mapper
        self.out, self.open_comment = [], False

    def run(self):
        v, n, i = self.v, len(self.v), 0
        while i < n:
            c = v[i]
            if c == "\\":
                i += 2 if i + 1 < n and v[i + 1] in _ASCII_PUNCT else 1
            elif c == "`":
                k = self._run_len(i)
                close = self._find_run(i + k, k)
                i = close[1] if close else i + k
            elif c == "{":
                i = self._role(i)
            elif c == "<":
                if v.startswith("<!--", i):
                    end = v.find("-->", i + 4)
                    if end < 0:
                        self.open_comment = True
                        return self
                    i = end + 3
                else:
                    m = _AUTOLINK_RE.match(v, i) or _TAG_RE.match(v, i)
                    i = m.end() if m and not _has_cite_syntax(m.group()) else i + 1
            elif c == "]" and i + 1 < n and v[i + 1] == "(":
                end = self._destination(i + 1)
                if end is None:
                    i += 1
                else:
                    if _has_cite_syntax(v[i + 1:end]):
                        raise _ScanFail("ambiguous_link_destination",
                                        "citation syntax inside a link destination", self.map.line(i))
                    i = end
            elif c == "[" and v.startswith("[@", i):
                raise _ScanFail("unsupported_citation_syntax",
                                "pandoc-style [@key] citations are not supported; use {cite:p}`key`",
                                self.map.line(i))
            elif c == "@":
                if _BARE_AT_RE.match(v, i):
                    raise _ScanFail("unsupported_citation_syntax",
                                    "bare @key citations are not supported; use {cite:t}`key` "
                                    "(escape a literal @ as \\@)", self.map.line(i))
                i += 1
            else:
                i += 1
        return self

    def _run_len(self, i):
        j = i
        while j < len(self.v) and self.v[j] == "`":
            j += 1
        return j - i

    def _find_run(self, start, k):
        """(run_start, run_end) of the first backtick run of exactly length k at/after start."""
        v, n, j = self.v, len(self.v), start
        while j < n:
            if v[j] == "`":
                m = self._run_len(j)
                if m == k:
                    return j, j + m
                j += m
            else:
                j += 1
        return None

    def _destination(self, i):
        v, n, depth, j = self.v, len(self.v), 0, i
        limit = min(n, i + 2000)
        while j < limit:
            c = v[j]
            if c == "\\" and j + 1 < n:
                j += 2
                continue
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    return j + 1
            j += 1
        return None

    def _role(self, i):
        v = self.v
        m = _ROLE_RE.match(v, i)
        if not m:
            return i + 1
        name, j = m.group(1), m.end()
        is_cite = name == "cite" or name.startswith("cite:")
        if j >= len(v) or v[j] != "`":
            if is_cite:
                raise _ScanFail("malformed_cite_role", "{%s} is not followed by a backtick-quoted key list"
                                % name, self.map.line(i))
            return j
        k = self._run_len(j)
        close = self._find_run(j + k, k)
        if not is_cite:
            return close[1] if close else j
        if name not in DIRECTIVE_KINDS:
            raise _ScanFail("unsupported_cite_role", "unsupported citation role {%s}; only {cite:p} and "
                            "{cite:t} are extracted" % name, self.map.line(i))
        if not close:
            raise _ScanFail("unterminated_role", "{%s} has no closing backtick" % name, self.map.line(i))
        body = v[j + k:close[0]]
        for idx, raw_key in enumerate(body.split(",")):
            key = raw_key.strip()
            if not key:
                raise _ScanFail("empty_key", "empty citation key in {%s}`%s`" % (name, body), self.map.line(i))
            if _KEY_RE.fullmatch(key) is None:
                raise _ScanFail("invalid_key", "invalid citation key %r" % key, self.map.line(i))
            self.out.append((name, idx, key, i, close[1]))
        return close[1]


class _Mapper:
    """Virtual paragraph index -> real character index / line number."""

    def __init__(self, offsets, pieces):
        self.off, self.virt_starts, self.real_starts, self.lens = offsets, [], [], []
        pos = 0
        for real_start, text in pieces:
            self.virt_starts.append(pos)
            self.real_starts.append(real_start)
            self.lens.append(len(text))
            pos += len(text) + 1

    def real(self, vi):
        k = max(0, bisect.bisect_right(self.virt_starts, vi) - 1)
        return self.real_starts[k] + min(vi - self.virt_starts[k], self.lens[k])

    def line(self, vi):
        return self.off.line_of(self.real(vi)) + 1


_MAX_INCLUDE_DEPTH = 1


class _Doc:
    def __init__(self, text, rel, root, deps, depth):
        self.text, self.rel, self.root, self.deps, self.depth = text, rel, root, deps, depth
        self.lines = _lines_of(text)
        self.off = _Offsets(text, self.lines)
        self.found = []

    # -- paragraph handling (a paragraph is one block inside one container path)
    def _scan_para(self, para):
        pieces = [(start, self.text[start:self.lines[li][1]]) for li, start in para]
        mapper = _Mapper(self.off, pieces)
        inline = _Inline("\n".join(t for _, t in pieces), mapper).run()
        return inline, mapper

    def _flush(self, para):
        if not para:
            return
        inline, mapper = self._scan_para(para)
        if inline.open_comment:
            raise _ScanFail("unterminated_comment", "HTML comment is never closed", para[0][0] + 1)
        for name, idx, key, vs, ve in inline.out:
            rs, re_ = mapper.real(vs), mapper.real(ve - 1) + 1
            l1, l2 = self.off.line_of(rs), self.off.line_of(re_ - 1)
            context = "\n".join(self.text[a:b] for a, b in self.lines[l1:l2 + 1])
            self.found.append({"directive_kind": name, "key_index": idx, "citation_key": key,
                               "byte_span": [self.off.byte_of(rs), self.off.byte_of(re_)],
                               "line_range": [l1 + 1, l2 + 1], "context": context})

    def _comment_open(self, para):
        """True if the paragraph so far ends inside an unclosed HTML comment (role errors are left to the
        final flush, which reports them with the complete paragraph)."""
        try:
            return bool(para) and self._scan_para(para)[0].open_comment
        except _ScanFail:
            return False

    # -- includes
    def _include(self, arg, option_lines, opener_line):
        opts = {}
        for ln in option_lines:
            s = ln.strip()
            if not s:
                continue
            m = _OPTION_RE.match(s)
            if not m:
                raise _ScanFail("unsupported_include", "unsupported {include} body line %r" % s, opener_line)
            name, val = m.group(1), m.group(2).strip()
            if name not in INCLUDE_OPTIONS:
                raise _ScanFail("unsupported_include", "unsupported {include} option :%s:" % name, opener_line)
            opts[name] = val
        if any(k in opts for k in ("literal", "code", "lang", "language")):
            return  # shown as code, never parsed
        if self.root is None or self.depth >= _MAX_INCLUDE_DEPTH:
            raise _ScanFail("unsupported_include", "nested or unresolvable {include}", opener_line)
        target = arg.strip()
        if not target or "://" in target or "\\" in target:
            raise _ScanFail("unsupported_include", "{include} needs a local relative path, got %r" % target,
                            opener_line)
        base = PurePosixPath(self.rel).parent
        norm = os.path.normpath(str(base / target)).replace(os.sep, "/")
        if not _safe_rel(norm):
            raise _ScanFail("path_escape", "{include} path %r leaves the repository" % target, opener_line)
        try:
            raw = _read_source(self.root, norm)
        except _ScanFail as e:
            raise _ScanFail(e.code, e.message, opener_line)
        region = raw.decode("utf-8")
        for key, pick in (("start-after", True), ("end-before", False)):
            marker = opts.get(key)
            if marker is None:
                continue
            at = region.find(marker)
            if at < 0 or not marker:
                raise _ScanFail("include_marker_not_found", "{include} %s marker %r not found in %s"
                                % (key, marker, norm), opener_line)
            region = region[at + len(marker):] if pick else region[:at]
        self.deps[norm] = raw
        try:
            inner = _Doc(region, norm, self.root, self.deps, self.depth + 1).scan(skip_frontmatter=False)
        except _ScanFail as e:
            raise _ScanFail("citation_in_include", "included %s: %s" % (norm, e.message), opener_line)
        if inner:
            raise _ScanFail("citation_in_include", "included %s contains citations; include them from "
                            "content/ instead" % norm, opener_line)

    # -- block pass
    @staticmethod
    def _is_closer(stripped, top):
        return re.fullmatch(re.escape(top["char"]) + "{%d,}" % top["len"], stripped) is not None

    @staticmethod
    def _match(raw, open_, limit=None):
        """Match the open containers (quote / list item) against one line.

        Returns (matched_count, index, column, content_base_col). A blank line matches list items only.
        """
        n, i, col, base, m = len(raw), 0, 0, 0, 0
        for ct in (open_ if limit is None else open_[:limit]):
            j, c = _skip_ws(raw, i, col)
            blank = not raw[j:].strip()
            if ct["t"] == "q":
                if blank or raw[j] != ">" or c - base >= 4:
                    break
                i, col, base = _quote_step(raw, j, c)
            else:
                if blank:
                    i, col = n, c
                elif c >= base + ct["rel"]:
                    # the required indentation is relative to the parent container's content column
                    base += ct["rel"]
                    while i < n and col < base and raw[i] in " \t":
                        col += (4 - col % 4) if raw[i] == "\t" else 1
                        i += 1
                else:
                    break
            m += 1
        return m, i, col, base

    @staticmethod
    def _open_new(raw, i, col, base, restricted):
        """Container starts (quote / list item) at the current position; restricted = a paragraph is open
        right here, which only a bullet or `1.` item may interrupt."""
        new, n = [], len(raw)
        while True:
            j, c = _skip_ws(raw, i, col)
            if j >= n or c - base >= 4:
                break
            if raw[j] == ">":
                i, col, base = _quote_step(raw, j, c)
                new.append({"t": "q"})
                continue
            ls = _list_start(raw, j, c)
            if ls is None or (restricted and not new and not ls[3]):
                break
            cc, i, col, _ = ls
            new.append({"t": "l", "rel": cc - base})
            base = cc
        return new, i, col, base

    def _close_entry(self, entry):
        if entry["kind"] == "include":
            self._include(entry["arg"], entry["lines"], entry["line"])

    def scan(self, skip_frontmatter=True):
        text, lines = self.text, self.lines
        li0 = 0
        if skip_frontmatter and lines and text[lines[0][0]:lines[0][1]].rstrip() == "---":
            end = next((i for i in range(1, len(lines))
                        if text[lines[i][0]:lines[i][1]].rstrip() in ("---", "...")), None)
            if end is None:
                raise _ScanFail("unterminated_frontmatter", "YAML front matter is never closed", 1)
            if _has_cite_syntax(text[lines[0][0]:lines[end][1]]):
                raise _ScanFail("citation_in_frontmatter", "front matter contains citation syntax, which "
                                "is not extracted", 1)
            li0 = end + 1
        stack, para, open_ = [], [], []
        st = {"depth": 0, "html": False}  # container depth the paragraph lives in; "<!--" seen in it

        def end_para():
            self._flush(para)
            del para[:]
            st["html"] = False

        def close_to(m):
            end_para()
            while stack and stack[-1]["depth"] > m:
                self._close_entry(stack.pop())
            del open_[m:]

        def add_line(li, start):
            if not para:
                st["depth"] = len(open_)
            para.append((li, start))
            st["html"] = st["html"] or "<!--" in text[start:lines[li][1]]

        for li in range(li0, len(lines)):
            s, e = lines[li]
            raw = text[s:e]
            if st["html"] and para and self._comment_open(para):
                # an unclosed HTML comment swallows lines, but only while its own containers continue
                m, i, _, _ = self._match(raw, open_, st["depth"])
                if m < st["depth"]:
                    raise _ScanFail("unterminated_comment", "an HTML comment is still open when its quote or "
                                    "list item ends", li + 1)
                para.append((li, s + i))
                continue
            m, i, col, base = self._match(raw, open_)
            if stack and stack[-1]["kind"] in ("code", "include", "unknown") and stack[-1]["depth"] > m:
                close_to(m)  # the container holding the fence ended: so did the fence
            top = stack[-1] if stack else None
            if top and top["kind"] in ("code", "include", "unknown"):
                # fence payload is opaque: only a same-container closer counts, nothing in it is parsed
                j, c = _skip_ws(raw, i, col)
                stripped = raw[j:].strip()
                if c - base < 4 and self._is_closer(stripped, top):
                    self._close_entry(stack.pop())
                elif top["kind"] == "include":
                    top["lines"].append(raw[i:])
                elif top["kind"] == "unknown" and _has_cite_syntax(raw[i:]):
                    raise _ScanFail("unsupported_directive_citation",
                                    "citation syntax inside the unsupported directive {%s}" % top["name"], li + 1)
                continue
            new, i2, col2, base2 = self._open_new(raw, i, col, base, bool(para) and m == len(open_))
            j, c = _skip_ws(raw, i2, col2)
            stripped = raw[j:].strip()
            ind, fm = c - base2, None
            para_alive = bool(para) and not new
            if not stripped:
                kind = "blank"
            elif ind >= 4:
                kind = "prose" if para_alive else "icode"
            else:
                fm = _FENCE_RE.match(stripped)
                if fm and fm.group(1)[0] == "`" and not _BACKTICK_FENCE_RE.match(stripped):
                    fm = None
                if top and top["kind"] == "prose" and not new and m >= top["depth"] and self._is_closer(stripped, top):
                    kind = "closer"
                elif fm:
                    kind = "fence"
                elif _HEADING_RE.match(stripped):
                    kind = "heading"
                elif _BREAK_RE.match(stripped) or (para_alive and _SETEXT_RE.match(stripped)):
                    kind = "break"
                elif _REFDEF_RE.match(stripped) and not any(c_["t"] == "l" for c_ in new):
                    kind = "refdef"
                elif stripped.startswith("%"):
                    kind = "percent"
                else:
                    kind = "prose"
            if m < len(open_):
                if kind == "prose" and not new and para and ind >= 4 and stripped.startswith(">"):
                    # an indented `>` under an unmatched list item: renderers disagree whether it is lazy text
                    raise _ScanFail("unsupported_container_layout", "indented quote marker under a list item whose "
                                    "continuation is ambiguous", li + 1)
                if kind == "prose" and not new and para:
                    add_line(li, s + i2)  # lazy paragraph continuation: the containers stay open
                    continue
                close_to(m)
            elif new:
                end_para()
            open_.extend(new)
            if kind == "prose":
                add_line(li, s + i2)
                continue
            end_para()
            if kind == "closer":
                stack.pop()
            elif kind == "fence":
                entry = self._open_fence(fm, li, s, raw)
                entry["depth"] = len(open_)
                stack.append(entry)
            elif kind == "heading":
                self._flush([(li, s + j)])
            elif kind in ("refdef", "percent"):
                if _has_cite_syntax(raw):
                    code, msg = (("citation_in_reference_definition", "citation syntax in a link reference "
                                  "definition") if kind == "refdef" else
                                 ("citation_in_comment_line", "a % comment line contains citation syntax; "
                                  "delete it or use an HTML comment"))
                    raise _ScanFail(code, msg, li + 1)
        end_para()
        if stack:
            raise _ScanFail("unterminated_fence", "fence or directive opened on line %d is never closed"
                            % stack[-1]["line"], stack[-1]["line"])
        return self.found

    def _open_fence(self, fm, li, s, raw):
        char, run, info = fm.group(1)[0], len(fm.group(1)), fm.group(2).strip()
        entry = {"char": char, "len": run, "line": li + 1, "name": None, "arg": "", "lines": []}
        dm = _DIRECTIVE_RE.match(info)
        if not dm:
            entry["kind"] = "prose" if char == ":" else "code"
            return entry
        name, arg = dm.group(1), dm.group(2)
        entry.update(name=name, arg=arg)
        if name == "include":
            entry["kind"] = "include"
        elif name in CODE_DIRECTIVES:
            entry["kind"] = "code"
        elif _is_prose_directive(name):
            entry["kind"] = "prose"
            if (name in ARGUMENT_IS_PROSE or name.startswith("prf:")) and arg.strip():
                self._flush([(li, s + raw.rfind(arg))])
            elif _has_cite_syntax(arg):
                raise _ScanFail("unsupported_citation_syntax",
                                "citation syntax in the argument of {%s}" % name, li + 1)
        else:
            entry["kind"] = "unknown"
            if _has_cite_syntax(arg):
                raise _ScanFail("unsupported_directive_citation",
                                "citation syntax inside the unsupported directive {%s}" % name, li + 1)
        return entry


def scan_markdown(text, rel="<memory>", root=None, deps=None):
    """Occurrences found in `text` (list of dicts), or raises _ScanFail. Pure; used by tests too."""
    return _Doc(text, rel, root, {} if deps is None else deps, 0).scan()


# ------------------------------------------------------------ bibliography
def parse_bib(text, rel):
    """{key: {file, entry_type, byte_span, text}} with a bounded brace/quote-aware parser."""
    entries, i, n = {}, 0, len(text)
    head = re.compile(r"@[ \t]*([A-Za-z]+)[ \t\r\n]*([{(])")
    last_c, last_b = 0, 0

    def byte_at(ci):
        nonlocal last_c, last_b
        if ci < last_c:
            last_c, last_b = 0, 0
        last_b += len(text[last_c:ci].encode("utf-8"))
        last_c = ci
        return last_b

    while True:
        i = text.find("@", i)
        if i < 0:
            return entries
        m = head.match(text, i)
        if not m:
            i += 1
            continue
        opener, depth, j, in_q, close = m.group(2), 0, m.end(), False, None
        while j < n:
            c = text[j]
            if c == "{":
                depth += 1
            elif c == "}":
                if depth == 0:
                    if in_q or opener != "{":
                        break
                    close = j
                    break
                depth -= 1
            elif c == '"' and depth == 0:
                in_q = not in_q
            elif c == ")" and opener == "(" and depth == 0 and not in_q:
                close = j
                break
            j += 1
        line = text.count("\n", 0, i) + 1
        if close is None:
            raise _ScanFail("bib_parse_error", "unbalanced braces/quotes in the entry starting here", line)
        etype = m.group(1).lower()
        if etype not in ("comment", "string", "preamble"):
            key = text[m.end():close].split(",", 1)[0].strip()
            if not key or re.search(r"[\s{}\",=#%'()]", key):
                raise _ScanFail("invalid_bib_key", "unsupported BibTeX key %r" % key, line)
            if key in entries:
                raise _ScanFail("duplicate_bib_key", "BibTeX key %r is defined twice in %s" % (key, rel), line)
            entries[key] = {"file": rel, "entry_type": etype,
                            "byte_span": [byte_at(i), byte_at(close + 1)], "text": text[i:close + 1]}
        i = close + 1


# ------------------------------------------------------------------- scanning
class Scan:
    """Result of one fresh, independent scan of a source tree."""

    def __init__(self, root, revision, sources, files, occurrences):
        self.root, self.revision, self.sources, self.files = Path(root), revision, sources, files
        self.occurrences = occurrences
        self.digest = sha256_hex(canonical_bytes(occurrences))

    def inventory(self):
        return {"schema_version": OCC_SCHEMA, "scanner_version": SCANNER_VERSION,
                "manuscript_revision": self.revision, "sources": self.sources,
                "occurrence_count": len(self.occurrences), "extraction_digest": self.digest,
                "occurrences": self.occurrences}


def _revision(files):
    h = hashlib.sha256()
    h.update(SCANNER_VERSION.encode("utf-8") + b"\n")
    for path in sorted(files):
        data = files[path]
        h.update(path.encode("utf-8") + b"\0" + str(len(data)).encode() + b"\0" + data + b"\n")
    return h.hexdigest()


def scan_sources(root):
    """Scan `root` from scratch; return a Scan or raise CitationError. Never trusts any inventory."""
    root = Path(root)
    found, errors = discover(root)
    if errors:
        raise CitationError(errors)
    files, roles, texts = {}, {}, {}
    for rel, role in found:
        try:
            raw = _read_source(root, rel)
        except _ScanFail as e:
            errors.append(diag(e.code, e.message, file=rel))
            continue
        files[rel], roles[rel] = raw, role
        texts[rel] = raw.decode("utf-8")
    if errors:
        raise CitationError(errors)
    bib, deps, raw_occ = {}, {}, []
    for rel in sorted(files):
        try:
            if roles[rel] == "bibliography":
                for key, ent in parse_bib(texts[rel], rel).items():
                    if key in bib:
                        raise _ScanFail("duplicate_bib_key", "BibTeX key %r is defined in both %s and %s"
                                        % (key, bib[key]["file"], rel))
                    bib[key] = ent
            else:
                raw_occ.extend((rel, o) for o in _Doc(texts[rel], rel, root, deps, 0).scan())
        except _ScanFail as e:
            errors.append(diag(e.code, e.message, file=rel, line=e.line))
    if errors:
        raise CitationError(errors)
    for rel, data in deps.items():
        if rel not in files:
            files[rel], roles[rel] = data, "include"
    revision = _revision(files)
    occurrences = []
    for rel, o in sorted(raw_occ, key=lambda x: (x[0], x[1]["byte_span"][0], x[1]["key_index"])):
        entry = bib.get(o["citation_key"])
        if entry is None:
            errors.append(diag("unresolved_bib_key", "citation key %r has no BibTeX entry" % o["citation_key"],
                               file=rel, line=o["line_range"][0]))
            continue
        rec = {"schema_version": OCC_SCHEMA, "manuscript_revision": revision,
               "occurrence_id": occurrence_id(revision, rel, o["byte_span"], o["key_index"]),
               "relative_file": rel, "byte_span": o["byte_span"], "line_range": o["line_range"],
               "directive_kind": o["directive_kind"], "key_index": o["key_index"],
               "citation_key": o["citation_key"], "context": o["context"],
               "bib_entry": {k: entry[k] for k in ("file", "byte_span", "text")}}
        occurrences.append(rec)
    if errors:
        raise CitationError(errors)
    sources = [{"path": p, "role": roles[p], "bytes": len(files[p]), "sha256": sha256_hex(files[p])}
               for p in sorted(files)]
    return Scan(root, revision, sources, files, occurrences)


# ----------------------------------------------------- inventory validation
def _validate_record_shape(rec, n):
    out = []
    where = {"index": n}
    if not isinstance(rec, dict):
        return [diag("malformed_record", "occurrence %d is not an object" % n, **where)]
    missing = [k for k in OCC_REQUIRED if k not in rec]
    extra = [k for k in rec if k not in OCC_REQUIRED and k not in OCC_OPTIONAL]
    if missing:
        out.append(diag("malformed_record", "occurrence %d lacks %s" % (n, ", ".join(missing)), **where))
    if extra:
        out.append(diag("malformed_record", "occurrence %d has unknown fields %s" % (n, ", ".join(sorted(extra))),
                        **where))
    if missing:
        return out

    def bad(field, why):
        out.append(diag("malformed_record", "occurrence %d: %s %s" % (n, field, why), **where))

    if rec["schema_version"] != OCC_SCHEMA:
        out.append(diag("unsupported_schema_version", "occurrence %d: schema_version %r != %r"
                        % (n, rec["schema_version"], OCC_SCHEMA), **where))
    for f in ("manuscript_revision", "occurrence_id"):
        if not _hex(rec[f]):
            bad(f, "must be 64 lowercase hex characters")
    if not _safe_rel(rec["relative_file"]):
        bad("relative_file", "must be a normalised relative posix path")
    bs, lr = rec["byte_span"], rec["line_range"]
    if not (isinstance(bs, list) and len(bs) == 2 and all(_int(x) for x in bs) and 0 <= bs[0] < bs[1]):
        bad("byte_span", "must be [start, end) integers with 0 <= start < end")
    if not (isinstance(lr, list) and len(lr) == 2 and all(_int(x) for x in lr) and 1 <= lr[0] <= lr[1]):
        bad("line_range", "must be [first, last] one-based integers")
    if rec["directive_kind"] not in DIRECTIVE_KINDS:
        bad("directive_kind", "must be one of " + ", ".join(DIRECTIVE_KINDS))
    if not (_int(rec["key_index"]) and rec["key_index"] >= 0):
        bad("key_index", "must be a non-negative integer")
    if not _nonempty(rec["citation_key"]):
        bad("citation_key", "must be a non-empty string")
    if "context" in rec and not isinstance(rec["context"], str):
        bad("context", "must be a string")
    if "claimed_finding" in rec and not isinstance(rec["claimed_finding"], str):
        bad("claimed_finding", "must be a string")
    if "bib_entry" in rec:
        be = rec["bib_entry"]
        if not (isinstance(be, dict) and set(be) == {"file", "byte_span", "text"} and isinstance(be["text"], str)
                and isinstance(be["file"], str) and isinstance(be["byte_span"], list) and len(be["byte_span"]) == 2
                and all(_int(x) and x >= 0 for x in be["byte_span"])):
            bad("bib_entry", "must be {file, byte_span: [start, end], text}")
    return out


def _core(rec):
    """The fields compared with the rescan: every required field, plus an optional one only if submitted."""
    keys = list(OCC_REQUIRED) + [k for k in ("context", "bib_entry") if k in rec]
    return {k: rec[k] for k in keys}


def _source_diffs(submitted, scan):
    out = []
    expected = {s["path"]: s for s in scan.sources}
    got = {s["path"]: s for s in submitted if isinstance(s, dict) and isinstance(s.get("path"), str)}
    for p in sorted(set(expected) - set(got)):
        out.append(diag("source_added", "%s exists now but is not in the inventory" % p, file=p))
    for p in sorted(set(got) - set(expected)):
        out.append(diag("source_removed", "%s is in the inventory but no longer exists" % p, file=p))
    for p in sorted(set(got) & set(expected)):
        if got[p] != expected[p]:
            out.append(diag("source_modified", "%s changed since the inventory was written" % p, file=p))
    return out


def check_inventory(data, scan, label="inventory"):
    """Diagnostics comparing a submitted extraction envelope with a fresh `scan` (empty = exact match)."""
    d = []
    if not isinstance(data, dict):
        return [diag("malformed_envelope", "%s must be an object" % label)]
    missing = [k for k in ENVELOPE_KEYS if k not in data]
    extra = [k for k in data if k not in ENVELOPE_KEYS]
    if missing or extra:
        return [diag("malformed_envelope", "%s keys: missing [%s] unknown [%s]"
                     % (label, ", ".join(missing), ", ".join(sorted(extra))))]
    if data["schema_version"] != OCC_SCHEMA:
        return [diag("unsupported_schema_version", "schema_version %r != %r" % (data["schema_version"], OCC_SCHEMA))]
    if data["scanner_version"] != SCANNER_VERSION:
        d.append(diag("scanner_version_mismatch", "scanner_version %r != %r" % (data["scanner_version"], SCANNER_VERSION)))
    occs, srcs = data["occurrences"], data["sources"]
    if not isinstance(occs, list) or not isinstance(srcs, list) or not _int(data["occurrence_count"]):
        return d + [diag("malformed_envelope", "sources/occurrences must be lists and occurrence_count an integer")]
    if not _hex(data["manuscript_revision"]) or not _hex(data["extraction_digest"]):
        d.append(diag("malformed_envelope", "manuscript_revision and extraction_digest must be 64 hex characters"))
    for n, s in enumerate(srcs):
        if not (isinstance(s, dict) and set(s) == set(SOURCE_KEYS) and isinstance(s["path"], str)
                and s["role"] in SOURCE_ROLES and _int(s["bytes"]) and _hex(s["sha256"])):
            d.append(diag("malformed_envelope", "sources[%d] must be {path, role, bytes, sha256}" % n))
    if data["occurrence_count"] != len(occs):
        d.append(diag("count_mismatch", "occurrence_count %s != %d listed occurrences"
                      % (data["occurrence_count"], len(occs))))
    shape = []
    for n, rec in enumerate(occs):
        shape.extend(_validate_record_shape(rec, n))
    d.extend(shape)
    if shape:
        return d
    ids = Counter(r["occurrence_id"] for r in occs)
    for oid, c in sorted(ids.items()):
        if c > 1:
            d.append(diag("duplicate_occurrence_id", "occurrence_id appears %d times" % c, occurrence_id=oid))
    idents = Counter((r["relative_file"], tuple(r["byte_span"]), r["key_index"]) for r in occs)
    for (f, span, ki), c in sorted(idents.items()):
        if c > 1:
            d.append(diag("duplicate_occurrence", "%s bytes %s key %d appears %d times" % (f, list(span), ki, c), file=f))
    for r in occs:
        want = occurrence_id(r["manuscript_revision"], r["relative_file"], r["byte_span"], r["key_index"])
        if r["occurrence_id"] != want:
            d.append(diag("invalid_occurrence_id", "occurrence_id does not derive from its revision/path/span/key",
                          occurrence_id=r["occurrence_id"], file=r["relative_file"]))
    stale = data["manuscript_revision"] != scan.revision or any(
        r["manuscript_revision"] != scan.revision for r in occs)
    if stale:
        d.append(diag("stale_revision", "inventory revision %s != current sources %s"
                      % (data["manuscript_revision"], scan.revision), details={
                          "submitted": data["manuscript_revision"], "current": scan.revision}))
        return d + _source_diffs(srcs, scan)
    if srcs != scan.sources:
        d.extend(_source_diffs(srcs, scan) or [diag("source_mismatch", "source list differs from the rescan")])
    expected = {o["occurrence_id"]: o for o in scan.occurrences}
    got = {}
    for r in occs:
        got.setdefault(r["occurrence_id"], r)
    for oid in sorted(set(expected) - set(got)):
        o = expected[oid]
        d.append(diag("missing_occurrence", "%s bytes %s key %r is in the sources but not in the inventory"
                      % (o["relative_file"], o["byte_span"], o["citation_key"]), occurrence_id=oid,
                      file=o["relative_file"], line=o["line_range"][0]))
    for oid in sorted(set(got) - set(expected)):
        r = got[oid]
        d.append(diag("unknown_occurrence", "inventory lists %s bytes %s key %r which the sources do not contain"
                      % (r["relative_file"], r["byte_span"], r["citation_key"]), occurrence_id=oid,
                      file=r["relative_file"]))
    for oid in sorted(set(got) & set(expected)):
        a, b = _core(got[oid]), expected[oid]
        bad = sorted(k for k in a if a[k] != b.get(k))
        if bad:
            d.append(diag("occurrence_mismatch", "fields differ from the rescan: " + ", ".join(bad),
                          occurrence_id=oid, file=b["relative_file"], line=b["line_range"][0]))
    if not d and data["extraction_digest"] != scan.digest:
        d.append(diag("digest_mismatch", "extraction_digest does not match the rescanned occurrences"))
    return d


def validate_occurrences(root=".", data=None, path=None):
    """Live check: compare an extraction file (default provenance/citation_triples.json) with a fresh scan."""
    cmd = "validate-occurrences"
    root = Path(root)
    try:
        scan = scan_sources(root)
        if data is None:
            data, _ = load_json_file(path or root / EXTRACTION, EXTRACTION)
        diags = check_inventory(data, scan)
    except CitationError as e:
        diags = e.diagnostics
        scan = None
    return report(cmd, diags, manuscript_revision=scan.revision if scan else None,
                  occurrence_count=len(scan.occurrences) if scan else None)


# ------------------------------------------------------------ verdict checks
def _read_evidence(root, rel):
    if not (_safe_rel(rel) and rel.startswith(EVIDENCE_DIR + "/")):
        raise _ScanFail("evidence_path_invalid", "evidence path %r must be a file under %s/" % (rel, EVIDENCE_DIR))
    p = _walk_no_symlink(root, rel)
    try:
        if not p.is_file():
            raise _ScanFail("evidence_missing", "evidence file %s does not exist" % rel)
        if p.stat().st_size > MAX_EVIDENCE_BYTES:
            raise _ScanFail("evidence_invalid", "evidence file %s is too large" % rel)
        return p.read_bytes()
    except OSError as e:
        raise _ScanFail("evidence_missing", "evidence file %s: %s" % (rel, e))


def _exact_keys(obj, required, optional=()):
    return isinstance(obj, dict) and all(k in obj for k in required) and all(
        k in required or k in optional for k in obj)


def _check_evidence(root, ev, occ, verdict, verified):
    """Diagnostics for one verdict's evidence object (mechanical availability/binding only)."""
    oid = verdict.get("occurrence_id")
    d = []

    def err(code, msg):
        d.append(diag(code, msg, occurrence_id=oid))

    if not isinstance(ev, dict):
        return [diag("malformed_evidence", "evidence must be an object", occurrence_id=oid)]
    status = ev.get("status")
    if status not in EVIDENCE_STATUSES:
        return [diag("unknown_evidence_status", "evidence.status %r is not one of %s"
                     % (status, ", ".join(EVIDENCE_STATUSES)), occurrence_id=oid)]
    if status != "available":
        if not _exact_keys(ev, EVIDENCE_UNAVAILABLE_KEYS):
            err("malformed_evidence", "a %s evidence record must be exactly {status, citation_key, reason}" % status)
        elif not _nonempty(ev["reason"]):
            err("malformed_evidence", "evidence.reason must explain why the text is %s" % status)
        if ev.get("citation_key") != occ["citation_key"]:
            err("evidence_key_mismatch", "evidence.citation_key %r != occurrence key %r"
                % (ev.get("citation_key"), occ["citation_key"]))
        if verified:
            err("evidence_not_available", "VERIFIED requires available evidence text, got %s" % status)
        return d
    if not _exact_keys(ev, EVIDENCE_AVAILABLE_KEYS, EVIDENCE_AVAILABLE_OPTIONAL):
        return d + [diag("malformed_evidence", "an available evidence record needs exactly {%s} (+ optional %s)"
                         % (", ".join(EVIDENCE_AVAILABLE_KEYS), ", ".join(EVIDENCE_AVAILABLE_OPTIONAL)),
                         occurrence_id=oid)]
    depth = ev["depth"]
    if depth == "metadata_only":
        err("metadata_only_not_valid", "metadata_only is never valid verification depth")
    elif depth not in DEPTHS:
        err("malformed_evidence", "evidence.depth %r must be fulltext or abstract_only" % (depth,))
    reason = ev.get("fulltext_unavailable_reason")
    if "fulltext_unavailable_reason" in ev and not isinstance(reason, str):
        err("malformed_evidence", "evidence.fulltext_unavailable_reason must be a string when present")
    elif depth == "abstract_only" and not _nonempty(reason):
        err("fulltext_fallback_unexplained", "abstract_only evidence must state why full text was unavailable "
            "(fulltext_unavailable_reason)")
    elif reason == "":
        err("malformed_evidence", "evidence.fulltext_unavailable_reason must not be empty when present")
    if ev["citation_key"] != occ["citation_key"]:
        err("evidence_key_mismatch", "evidence.citation_key %r != occurrence key %r"
            % (ev["citation_key"], occ["citation_key"]))
    if not _hex(ev["sha256"]):
        err("malformed_evidence", "evidence.sha256 must be 64 lowercase hex characters")
    span = ev["passage_span"]
    if not (isinstance(span, list) and len(span) == 2 and all(_int(x) for x in span) and 0 <= span[0] < span[1]):
        err("malformed_evidence", "evidence.passage_span must be [start, end) integers")
    if d:
        return d
    try:
        data = _read_evidence(root, ev["path"])
    except _ScanFail as e:
        return [diag(e.code, e.message, occurrence_id=oid)]
    if sha256_hex(data) != ev["sha256"]:
        return [diag("evidence_hash_mismatch", "%s no longer matches evidence.sha256" % ev["path"], occurrence_id=oid)]
    if span[1] > len(data):
        return [diag("passage_out_of_range", "passage_span %s exceeds the %d evidence bytes" % (span, len(data)),
                     occurrence_id=oid)]
    try:
        passage = data[span[0]:span[1]].decode("utf-8")
    except UnicodeDecodeError:
        return [diag("passage_not_utf8", "passage_span cuts through a UTF-8 sequence", occurrence_id=oid)]
    sp = verdict.get("supporting_passage")
    if sp is not None and sp != passage:
        err("passage_mismatch", "supporting_passage is not the verbatim evidence bytes at passage_span")
    if verified and sp is None:
        err("passage_mismatch", "VERIFIED requires supporting_passage")
    if depth != verdict.get("verification_depth"):
        err("depth_mismatch", "evidence.depth %r != verification_depth %r" % (depth, verdict.get("verification_depth")))
    return d


def _check_attestation(att, oid, code):
    ok = (_exact_keys(att, ATTESTATION_KEYS) and att["protocol"] == PROTOCOL
          and isinstance(att["steps_completed"], list) and all(_int(x) for x in att["steps_completed"])
          and att["steps_completed"] == [1, 2, 3, 4, 5] and _nonempty(att["attested_by"]))
    return [] if ok else [diag(code, "VERIFIED needs scientific_checks_attestation "
                               "{protocol: %s, steps_completed: [1..5], attested_by}" % PROTOCOL,
                               occurrence_id=oid)]


def _check_one_verdict(root, v, occ, revision):
    oid = v.get("occurrence_id")
    d = []
    missing = [k for k in VERDICT_REQUIRED if k not in v]
    extra = [k for k in v if k not in VERDICT_REQUIRED and k not in VERDICT_OPTIONAL]
    if missing or extra:
        return [diag("malformed_verdict", "verdict fields: missing [%s] unknown [%s]"
                     % (", ".join(missing), ", ".join(sorted(extra))), occurrence_id=oid)]
    if v["manuscript_revision"] != revision:
        d.append(diag("stale_revision", "verdict revision %s != current %s" % (v["manuscript_revision"], revision),
                      occurrence_id=oid))
    if v["cite_key"] != occ["citation_key"]:
        d.append(diag("verdict_key_mismatch", "cite_key %r != occurrence key %r" % (v["cite_key"], occ["citation_key"]),
                      occurrence_id=oid))
    cat = v["category"]
    if cat not in CATEGORIES:
        return d + [diag("unknown_category", "category %r is not one of %s" % (cat, ", ".join(CATEGORIES)),
                         occurrence_id=oid)]
    verified = cat == "VERIFIED"
    step = v["failed_at_step"]
    if verified != (step is None) or (step is not None and not (_int(step) and 1 <= step <= 5)):
        d.append(diag("malformed_verdict", "failed_at_step must be null for VERIFIED and an integer 1-5 otherwise",
                      occurrence_id=oid))
    depth = v["verification_depth"]
    if depth == "metadata_only":
        d.append(diag("metadata_only_not_valid", "metadata_only is never valid verification depth", occurrence_id=oid))
    elif depth not in DEPTHS and not (depth is None and not verified):
        d.append(diag("malformed_verdict", "verification_depth must be fulltext or abstract_only "
                      "(null only for a non-VERIFIED verdict that retrieved nothing)", occurrence_id=oid))
    for f in ("supporting_passage", "details", "suggested_action"):
        if f in v and v[f] is not None and not isinstance(v[f], str):
            d.append(diag("malformed_verdict", "%s must be a string or null" % f, occurrence_id=oid))
    if "correct_metadata" in v and v["correct_metadata"] is not None and not isinstance(v["correct_metadata"], dict):
        d.append(diag("malformed_verdict", "correct_metadata must be an object or null", occurrence_id=oid))
    ev = v.get("evidence")
    if ev is None:
        if verified:
            d.append(diag("evidence_not_available", "VERIFIED requires an available evidence record", occurrence_id=oid))
        elif depth is not None:
            d.append(diag("depth_without_evidence", "verification_depth is set but no evidence is referenced",
                          occurrence_id=oid))
        if not verified and not _nonempty(v["details"]):
            d.append(diag("missing_explanation", "a non-VERIFIED verdict without evidence needs `details` "
                          "explaining the manual review needed", occurrence_id=oid))
        if not verified and v["supporting_passage"] is not None:
            d.append(diag("passage_mismatch", "supporting_passage without evidence cannot be bound to text",
                          occurrence_id=oid))
    else:
        d.extend(_check_evidence(root, ev, occ, v, verified))
        if not verified and isinstance(ev, dict) and ev.get("status") == "available" and depth is None:
            d.append(diag("malformed_verdict", "evidence is available but verification_depth is null", occurrence_id=oid))
    if verified:
        d.extend(_check_attestation(v.get("scientific_checks_attestation"), oid, "missing_attestation"))
    elif v.get("scientific_checks_attestation") is not None:
        d.extend(_check_attestation(v["scientific_checks_attestation"], oid, "invalid_attestation"))
    return d


def check_verdicts(root, data, scan):
    """Diagnostics for a verification envelope against the occurrences of a fresh `scan`."""
    if not isinstance(data, dict):
        return [diag("malformed_envelope", "verification must be an object")]
    missing = [k for k in VERIFICATION_ENVELOPE_KEYS if k not in data]
    extra = [k for k in data if k not in VERIFICATION_ENVELOPE_KEYS]
    if missing or extra:
        return [diag("malformed_envelope", "verification keys: missing [%s] unknown [%s]"
                     % (", ".join(missing), ", ".join(sorted(extra))))]
    if data["schema_version"] != VERDICT_SCHEMA:
        return [diag("unsupported_schema_version", "schema_version %r != %r" % (data["schema_version"], VERDICT_SCHEMA))]
    d = []
    if not isinstance(data["verdicts"], list):
        return [diag("malformed_envelope", "verdicts must be a list")]
    if not _hex(data["manuscript_revision"]) or not _hex(data["extraction_digest"]):
        return [diag("malformed_envelope", "manuscript_revision and extraction_digest must be 64 hex characters")]
    if data["manuscript_revision"] != scan.revision:
        d.append(diag("stale_revision", "verification revision %s != current sources %s"
                      % (data["manuscript_revision"], scan.revision)))
    if data["extraction_digest"] != scan.digest:
        d.append(diag("extraction_digest_mismatch", "verification was written against a different extraction"))
    verdicts, expected = data["verdicts"], {o["occurrence_id"]: o for o in scan.occurrences}
    ids = []
    for n, v in enumerate(verdicts):
        if not isinstance(v, dict) or not _hex(v.get("occurrence_id")):
            d.append(diag("malformed_verdict", "verdict %d is not an object with a 64-hex occurrence_id" % n))
        else:
            ids.append(v["occurrence_id"])
    counts = Counter(ids)
    for oid, c in sorted(counts.items()):
        if c > 1:
            d.append(diag("duplicate_verdict", "%d verdicts for one occurrence" % c, occurrence_id=oid))
    for oid in sorted(set(counts) - set(expected)):
        d.append(diag("unknown_verdict_id", "verdict for an occurrence that does not exist", occurrence_id=oid))
    for oid in sorted(set(expected) - set(counts)):
        o = expected[oid]
        d.append(diag("missing_verdict", "no verdict for %s line %d key %r" % (o["relative_file"], o["line_range"][0],
                                                                              o["citation_key"]),
                      occurrence_id=oid, file=o["relative_file"], line=o["line_range"][0]))
    done = set()
    for v in verdicts:
        oid = v.get("occurrence_id") if isinstance(v, dict) else None
        if _hex(oid) and oid in expected and oid not in done:
            done.add(oid)
            d.extend(_check_one_verdict(root, v, expected[oid], scan.revision))
    return d


def validate_verdicts(root=".", data=None, path=None):
    """Live check: fresh scan, the extraction file, then the verification file (default paths)."""
    cmd = "validate-verdicts"
    root = Path(root)
    try:
        scan = scan_sources(root)
        diags = check_inventory(load_json_file(root / EXTRACTION, EXTRACTION)[0], scan, EXTRACTION)
        if data is None:
            data, _ = load_json_file(path or root / VERIFICATION, VERIFICATION)
        diags = diags + check_verdicts(root, data, scan)
    except CitationError as e:
        diags, scan = e.diagnostics, None
    return report(cmd, diags, manuscript_revision=scan.revision if scan else None,
                  occurrence_count=len(scan.occurrences) if scan else None)


def assemble_verification(scan_or_root, verdicts):
    """Wrap verdict records in the versioned envelope bound to the current extraction."""
    scan = scan_or_root if isinstance(scan_or_root, Scan) else scan_sources(scan_or_root)
    return {"schema_version": VERDICT_SCHEMA, "manuscript_revision": scan.revision,
            "extraction_digest": scan.digest, "verdicts": verdicts}


# ------------------------------------------------------------------ snapshot
def _snapshot_manifest(scan):
    return {"schema_version": SNAPSHOT_SCHEMA, "scanner_version": SCANNER_VERSION,
            "manuscript_revision": scan.revision, "source_count": len(scan.sources), "sources": scan.sources}


def create_snapshot(root=".", supersede=False):
    """Copy every scanned source into the immutable snapshot directory; return its manifest."""
    root = Path(root)
    scan = scan_sources(root)
    dest = root / SNAPSHOT_DIR
    if dest.exists() or dest.is_symlink():
        if not supersede:
            _fail("snapshot_exists", "%s already exists; it is immutable. After reopen_phase('15', ...) "
                  "re-run with --supersede to move it aside" % SNAPSHOT_DIR)
        n = 1
        while (dest.parent / ("citation_snapshot.superseded-%d" % n)).exists():
            n += 1
        os.rename(str(dest), str(dest.parent / ("citation_snapshot.superseded-%d" % n)))
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(dir=str(dest.parent), prefix=".citation_snapshot."))
    try:
        for rel, data in scan.files.items():
            target = tmp / "files" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            os.chmod(str(target), 0o444)
        manifest = _snapshot_manifest(scan)
        (tmp / "manifest.json").write_bytes(json_text(manifest).encode("utf-8"))
        os.chmod(str(tmp / "manifest.json"), 0o444)
        verify_snapshot(tmp)
        os.chmod(str(tmp), 0o755)
        os.rename(str(tmp), str(dest))
    except BaseException:
        shutil.rmtree(str(tmp), ignore_errors=True)
        raise
    return manifest


def verify_snapshot(snap):
    """Check a snapshot directory's integrity and rescan it; return (manifest, scan_of_snapshot)."""
    snap = Path(snap)
    if snap.is_symlink() or not snap.is_dir():
        _fail("snapshot_missing", "%s is missing or not a directory" % snap)
    manifest, _ = load_json_file(snap / "manifest.json", "snapshot manifest")
    keys = {"schema_version", "scanner_version", "manuscript_revision", "source_count", "sources"}
    if set(manifest) != keys:
        _fail("snapshot_malformed", "snapshot manifest keys must be exactly " + ", ".join(sorted(keys)))
    if manifest["schema_version"] != SNAPSHOT_SCHEMA:
        _fail("unsupported_schema_version", "snapshot schema %r != %r" % (manifest["schema_version"], SNAPSHOT_SCHEMA))
    srcs = manifest["sources"]
    if not (isinstance(srcs, list) and manifest["source_count"] == len(srcs) and all(
            isinstance(s, dict) and set(s) == set(SOURCE_KEYS) and _safe_rel(s["path"]) and s["role"] in SOURCE_ROLES
            and _int(s["bytes"]) and _hex(s["sha256"]) for s in srcs)) or not _hex(manifest["manuscript_revision"]):
        _fail("snapshot_malformed", "snapshot manifest sources are malformed")
    if [s["path"] for s in srcs] != sorted(s["path"] for s in srcs) or len({s["path"] for s in srcs}) != len(srcs):
        _fail("snapshot_malformed", "snapshot manifest sources must be unique and sorted")
    files_root = snap / "files"
    on_disk = set()
    if files_root.is_symlink() or not files_root.is_dir():
        _fail("snapshot_missing", "%s/files is missing" % snap)
    walk_errors = []
    for dirpath, dirnames, filenames in os.walk(str(files_root), followlinks=False, onerror=walk_errors.append):
        for name in dirnames + filenames:
            full = Path(dirpath, name)
            if full.is_symlink():
                _fail("symlink_refused", "snapshot contains a symlink: %s" % full.relative_to(files_root).as_posix())
        for name in filenames:
            on_disk.add(Path(dirpath, name).relative_to(files_root).as_posix())
    if walk_errors:
        raise CitationError([diag("unreadable_snapshot", "cannot list %s: %s; the snapshot would be silently "
                                  "incomplete" % (err.filename, err.strerror or err)) for err in walk_errors])
    listed = {s["path"] for s in srcs}
    d = [diag("snapshot_file_unlisted", "%s is in the snapshot but not in its manifest" % p, file=p)
         for p in sorted(on_disk - listed)]
    d += [diag("snapshot_file_missing", "%s is in the manifest but missing from the snapshot" % p, file=p)
          for p in sorted(listed - on_disk)]
    if d:
        raise CitationError(d)
    for s in srcs:
        data = (files_root / s["path"]).read_bytes()
        if len(data) != s["bytes"] or sha256_hex(data) != s["sha256"]:
            d.append(diag("snapshot_file_modified", "%s no longer matches the manifest" % s["path"], file=s["path"]))
    if d:
        raise CitationError(d)
    scan = scan_sources(files_root)
    if scan.sources != srcs or scan.revision != manifest["manuscript_revision"]:
        raise CitationError([diag("snapshot_rescan_mismatch", "an independent rescan of the snapshot does not "
                                  "reproduce its manifest (sources omitted or revision differs)")]
                            + _source_diffs(srcs, scan))
    return manifest, scan


# ------------------------------------------------------------------ coverage
def _coverage_doc(scan, extraction_raw, manifest_raw):
    return {"schema_version": COVERAGE_SCHEMA, "scanner_version": SCANNER_VERSION,
            "manuscript_revision": scan.revision, "extraction_digest": scan.digest,
            "extraction_sha256": sha256_hex(extraction_raw), "snapshot_manifest_sha256": sha256_hex(manifest_raw),
            "source_count": len(scan.sources), "occurrence_count": len(scan.occurrences),
            "key_count": len({o["citation_key"] for o in scan.occurrences}),
            "sources": [{"path": s["path"], "sha256": s["sha256"]} for s in scan.sources],
            "mechanical_checks": ["snapshot_integrity", "snapshot_rescan_reproduces_manifest",
                                  "extraction_equals_snapshot_rescan", "live_rescan_equals_snapshot_at_admission"],
            "scope": "mechanical occurrence coverage only; scientific correctness is not established here"}


def check_stage(root=".", live=True, snapshot=None):
    """Phase 15 contract. Returns (scan_of_snapshot, extraction_raw, manifest_raw); raises CitationError.

    live=True additionally requires the current manuscript to equal the snapshot (independent
    discovery on both sides). live=False is the historical audit of a sealed revision: it validates
    the pinned snapshot and extraction only and says nothing about the current manuscript.
    """
    root = Path(root)
    snap = Path(snapshot) if snapshot else root / SNAPSHOT_DIR
    manifest, snap_scan = verify_snapshot(snap)
    manifest_raw = (snap / "manifest.json").read_bytes()
    data, extraction_raw = load_json_file(root / EXTRACTION, EXTRACTION)
    diags = check_inventory(data, snap_scan, EXTRACTION + " vs snapshot")
    if diags:
        raise CitationError(diags)
    if live:
        live_scan = scan_sources(root)
        if live_scan.revision != snap_scan.revision or live_scan.sources != snap_scan.sources:
            raise CitationError([diag("stale_snapshot", "the manuscript changed after the Phase 15 snapshot "
                                      "(live %s != snapshot %s); reopen_phase('15', ...) to re-extract"
                                      % (live_scan.revision, snap_scan.revision))]
                                + _source_diffs(snap_scan.sources, live_scan))
        diags = check_inventory(data, live_scan, EXTRACTION + " vs live sources")
        if diags:
            raise CitationError(diags)
    return snap_scan, extraction_raw, manifest_raw


def build_coverage(root=".", live=True, snapshot=None):
    snap_scan, extraction_raw, manifest_raw = check_stage(root, live, snapshot)
    return _coverage_doc(snap_scan, extraction_raw, manifest_raw)


def write_coverage(root="."):
    doc = build_coverage(root, live=True)
    data = json_text(doc).encode("utf-8")
    write_atomic(Path(root) / COVERAGE, data)
    return doc, sha256_hex(data)


def check_coverage(root=".", live=True, report_obj=None, snapshot=None):
    """Phase 15V contract: coverage file equals a recomputation; an optional report binds to it."""
    root = Path(root)
    expected = json_text(build_coverage(root, live, snapshot)).encode("utf-8")
    p = root / COVERAGE
    if p.is_symlink() or not p.is_file():
        _fail("coverage_missing", "%s is missing; run `citation_occurrences.py coverage`" % COVERAGE)
    actual = p.read_bytes()
    if actual != expected:
        _fail("coverage_mismatch", "%s does not equal an independent recomputation" % COVERAGE)
    if report_obj is not None:
        doc = loads_strict(expected.decode("utf-8"))
        want = {"citation_coverage_sha256": sha256_hex(actual), "manuscript_revision": doc["manuscript_revision"],
                "occurrence_count": doc["occurrence_count"]}
        bad = [k for k, v in want.items() if report_obj.get(k) != v or (k == "occurrence_count" and not _int(report_obj.get(k)))]
        if bad:
            _fail("report_not_bound", "the validator report must carry the persisted coverage facts; "
                  "mismatched or missing: " + ", ".join(bad))


def check_verification(root=".", live=True, snapshot=None):
    """Phase 16 contract: verdict and evidence coverage of the pinned extraction.

    live=True (admission) also requires live == snapshot; live=False audits the sealed revision.
    """
    root = Path(root)
    snap_scan, _, _ = check_stage(root, live, snapshot)
    data, _ = load_json_file(root / VERIFICATION, VERIFICATION)
    diags = check_verdicts(root, data, snap_scan)
    if diags:
        raise CitationError(diags)


def verify_contract(contract, root=".", live=True, report_obj=None):
    if contract == "extract":
        check_stage(root, live)
    elif contract == "coverage":
        check_coverage(root, live, report_obj)
    elif contract == "verdicts":
        check_verification(root, live)
    else:
        _fail("unknown_contract", "unknown citation contract %r" % contract)


def audit_historical(root=".", snapshot=None):
    """Explicit historical audit of a pinned snapshot. Says nothing about the current manuscript."""
    root = Path(root)
    try:
        check_stage(root, live=False, snapshot=snapshot)
        diags = []
        if (root / COVERAGE).exists():
            check_coverage(root, live=False, snapshot=snapshot)
        if (root / VERIFICATION).exists():
            check_verification(root, live=False, snapshot=snapshot)
    except CitationError as e:
        diags = e.diagnostics
    return report("audit-historical", diags, mode="historical",
                  note="validates the pinned snapshot only; the current manuscript is not compared")


# ------------------------------------------------------------------------ CLI
def _emit(rep):
    sys.stdout.write(json.dumps(rep, indent=2, sort_keys=True, ensure_ascii=True) + "\n")
    return 0 if rep["ok"] else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Deterministic citation extraction and coverage checks.")
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("extract", "snapshot", "coverage", "validate-occurrences", "validate-verdicts", "audit-historical"):
        p = sub.add_parser(name)
        p.add_argument("--root", default=".")
        if name == "extract":
            p.add_argument("--out", default=None, help="output file (default provenance/citation_triples.json; - for stdout)")
        if name == "snapshot":
            p.add_argument("--supersede", action="store_true",
                           help="move an existing snapshot aside (only after reopen_phase('15', ...))")
        if name in ("validate-occurrences", "validate-verdicts"):
            p.add_argument("--file", default=None)
        if name == "audit-historical":
            p.add_argument("--snapshot", default=None, help="the pinned snapshot directory")
    a = ap.parse_args(argv)
    root = Path(a.root)
    try:
        if a.command == "extract":
            scan = scan_sources(root)
            text = json_text(scan.inventory())
            if a.out == "-":
                sys.stdout.write(text)
                return 0
            write_atomic(Path(a.out) if a.out else root / EXTRACTION, text.encode("utf-8"))
            return _emit(report("extract", [], manuscript_revision=scan.revision,
                                occurrence_count=len(scan.occurrences), written=a.out or EXTRACTION))
        if a.command == "snapshot":
            m = create_snapshot(root, a.supersede)
            return _emit(report("snapshot", [], manuscript_revision=m["manuscript_revision"],
                                source_count=m["source_count"], written=SNAPSHOT_DIR))
        if a.command == "coverage":
            doc, sha = write_coverage(root)
            return _emit(report("coverage", [], manuscript_revision=doc["manuscript_revision"],
                                occurrence_count=doc["occurrence_count"], citation_coverage_sha256=sha,
                                written=COVERAGE))
        if a.command == "validate-occurrences":
            return _emit(validate_occurrences(root, path=a.file))
        if a.command == "validate-verdicts":
            return _emit(validate_verdicts(root, path=a.file))
        return _emit(audit_historical(root, a.snapshot))
    except CitationError as e:
        return _emit(report(a.command, e.diagnostics))
    except Exception as e:  # fail closed with a diagnostic, never a bare traceback on an empty stdout
        return _emit(report(a.command, [diag("internal_error", "%s: %s" % (type(e).__name__, e))]))


if __name__ == "__main__":
    sys.exit(main())
