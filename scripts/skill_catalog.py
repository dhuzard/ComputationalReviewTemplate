#!/usr/bin/env python3
"""Role summaries for the skill files, for the Methods "Pipeline Skills" table.

    python scripts/skill_catalog.py            # print the generated table

Every skill carries YAML frontmatter, so the first line of a file is `---`, not
a description. The role is the first sentence of the frontmatter `description`
(minus the "Expert Review Pipeline vN (...):" prefix). A file without a usable
description falls back to its first Markdown heading outside code fences, then
to its file stem. Standard library only.
"""
import json
import re
import sys
from pathlib import Path

PREFIX = re.compile(r"^Expert Review Pipeline v\d+ \([^)]*\):\s*")


def frontmatter(text):
    """Return (fields, body). Handles quoted, plain and folded/literal values."""
    m = re.match(r"\A---[ \t]*\r?\n(.*?\r?\n)?---[ \t]*(?:\r?\n|\Z)", text, re.S)
    if not m:
        return {}, text
    fields, key = {}, None
    for line in (m.group(1) or "").splitlines():
        top = re.match(r"^([A-Za-z_][\w-]*):[ \t]*(.*)$", line)
        if top:
            key, value = top.group(1), top.group(2).strip()
            fields[key] = "" if value in (">", "|", ">-", "|-") else value
        elif key and line.startswith((" ", "\t")):
            fields[key] = (fields[key] + " " + line.strip()).strip()
    for key, value in fields.items():
        if value[:1] == '"':
            try:
                fields[key] = json.loads(value)
                continue
            except ValueError:
                pass
        fields[key] = value.strip("\"'")
    return fields, text[m.end():]


def first_heading(body):
    fenced = False
    for line in body.splitlines():
        if re.match(r"^\s*(```|~~~)", line):
            fenced = not fenced
        elif not fenced:
            m = re.match(r"^#{1,6}\s+(\S.*?)\s*#*\s*$", line)
            if m:
                return m.group(1)
    return None


def skill_role(text, fallback=""):
    fields, body = frontmatter(text)
    description = PREFIX.sub("", " ".join(fields.get("description", "").split()))
    if description:
        return re.split(r"(?<=[^.\s])\.(?:\s|$)", description, maxsplit=1)[0].strip()
    return first_heading(body) or fallback


def skill_rows(skill_dir="skills"):
    return [(p.name, skill_role(p.read_text(encoding="utf-8"), p.stem))
            for p in sorted(Path(skill_dir).glob("comprev-*.md"))]


def methods_table(rows):
    cell = lambda s: s.replace("|", "\\|")
    table = "| Skill | Role |\n|---|---|\n"
    return table + "\n".join(f"| `{n}` | {cell(r)} |" for n, r in rows)


if __name__ == "__main__":
    sys.exit(print(methods_table(skill_rows(sys.argv[1] if len(sys.argv) > 1 else "skills"))) or 0)
