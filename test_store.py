"""Run: python3 test_store.py

Covers the `sources` field only: everything else `render()` writes was never
part of the frontmatter-parsing bug this guards against.

The Sentinel's frontmatter reader (sentinel/text.py split_frontmatter) is a
flat `key: value` reader, not a YAML parser - deliberately, because every
field IT owns is a scalar. `sources` was the one field this store wrote as a
YAML list (`sources:` header + `  - url` lines). Partitioned on ":" like
every other line, `  - https://a` and `  - https://b` both became key
`- https`, so the second write silently overwrote the first and every URL
but the last, per scheme, vanished from frontmatter. Measured on the real
vault: 29 files, 28 with a stray `- https` key and 7 with `- http`.

The fix, in `render()`, is to write `sources` the same way
sentinel/concepts.py already does: one scalar line, URLs comma-joined.
"""

from __future__ import annotations

import sys

from check import CheckResult, Passage
from store import render

PASS: list[str] = []
FAIL: list[str] = []


def ok(name, cond, detail=""):
    (PASS if cond else FAIL).append(f"{name}{' - ' + detail if detail and not cond else ''}")


# Two results sharing a scheme - exactly the shape that collapsed under the
# old writer, since both URLs would partition to the same bogus key.
RESULTS = [
    CheckResult(
        url="https://a.example.com/one",
        title="Page A",
        passages=[Passage(text="A says something.", url="https://a.example.com/one")],
    ),
    CheckResult(
        url="https://b.example.com/two",
        title="Page B",
        passages=[Passage(text="B says something else.", url="https://b.example.com/two")],
    ),
    CheckResult(
        url="http://c.example.com/three",
        title="Page C",
        passages=[Passage(text="C too.", url="http://c.example.com/three")],
    ),
]

doc = render("three sources", RESULTS)
front, _, _ = doc.partition("\n---\n")
front_lines = front.split("\n")

ok("no bare 'sources:' list header",
   "sources:" not in front_lines, front)
ok("no YAML list item lines under sources",
   not any(line.strip().startswith("- http") for line in front_lines), front)

sources_lines = [l for l in front_lines if l.startswith("sources: ")]
ok("exactly one 'sources: ' scalar line", len(sources_lines) == 1, front)

if sources_lines:
    value = sources_lines[0][len("sources: "):].strip('"')
    urls = [u.strip() for u in value.split(",")]
    ok("all three URLs survive in the one line",
       urls == [r.url for r in RESULTS], value)

# --- no results found: no sources field at all, not an empty list --------
empty_doc = render("nothing found", [CheckResult(url="https://x.example.com")])
ok("no 'sources' field when nothing was found",
   "sources" not in empty_doc.split("\n---\n")[0])

print(f"\n{len(PASS)} passed, {len(FAIL)} failed\n")
for f in FAIL:
    print("  FAIL  " + f)
sys.exit(1 if FAIL else 0)
