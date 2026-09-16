"""Run: python3 test_store.py

Covers two bugs, both of which wrote a correct file to a wrong place or a
wrong file to the right place, and neither of which raised anything.

--- the `sources` field ---------------------------------------------------

Everything else `render()` writes was never part of the frontmatter-parsing
bug this guards against.

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

--- `~` in a path setting -------------------------------------------------

`SENTINEL_VAULT` and `SEARCHER_OUTPUT` name directories, and a shell expands
`~` only when it is unquoted. Quoted in a shell, set in a systemd
`Environment=` line or read from an .env file, the tilde arrives here
literally, and `Path("~/notes")` is a relative path whose first component is
a directory named `~`. mkdir made it, the write succeeded, the run reported
`[DONE]` - and the research was in `./~/notes` beside the code instead of in
the home directory. The fix is config.env_path, which expanduser()s every
setting that names a place on disk.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import store
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

# --- `~` in a path setting expands to the home directory ----------------
# store reads both settings at import, so the reload is what re-reads them.
HOME = Path.home()
os.environ["SENTINEL_VAULT"] = "~/kasa"
os.environ["SEARCHER_OUTPUT"] = "~/notlar"
store = importlib.reload(store)

ok("SEARCHER_OUTPUT=~/notlar lands under the home directory",
   store.SOURCES == HOME / "notlar", str(store.SOURCES))
ok("SENTINEL_VAULT=~/kasa lands under the home directory",
   store.VAULT == HOME / "kasa", str(store.VAULT))

# The default for SEARCHER_OUTPUT is built from VAULT, so an unexpanded vault
# would carry the literal `~` into the sources directory too.
del os.environ["SEARCHER_OUTPUT"]
store = importlib.reload(store)
ok("a `~` vault carries no literal tilde into the default sources directory",
   store.SOURCES == HOME / "kasa" / "sources" and "~" not in store.SOURCES.parts,
   str(store.SOURCES))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed\n")
for f in FAIL:
    print("  FAIL  " + f)
sys.exit(1 if FAIL else 0)
