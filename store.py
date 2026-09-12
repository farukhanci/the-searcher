"""
The Searcher — layer 3: the store.

Writes one file per research call into the vault's `sources/`. Everything here
is written by code: the model never chooses a path, a filename or a format.

Nothing in a stored file is the model's own words. Passages are the page's
wording, recovered from the source during verification; the contents list and
the caveats are produced here.

    python3 store.py "what CPU does the chip have" https://example.com/a
    python3 store.py --dry "..." https://example.com/a     # print, don't write
"""

from __future__ import annotations

import asyncio
import re
import sys
from datetime import date, datetime
from pathlib import Path

from check import CheckResult, check_pages
from fetch_clean import fetch_and_clean

VAULT = Path.home() / "obsidian" / "Obsidian-1"
SOURCES = VAULT / "sources"


# ---------------------------------------------------------------- naming

def slug(text: str, words: int = 8) -> str:
    keep = re.sub(r"[^a-z0-9\s-]", "", text.lower()).split()[:words]
    return "-".join(keep) or "query"


def new_path(query: str, root: Path = SOURCES) -> Path:
    """`sources/2026-09-09-what-cpu-does-the-chip-have.md`, never overwriting."""
    stem = f"{date.today().isoformat()}-{slug(query)}"
    path = root / f"{stem}.md"
    n = 2
    while path.exists():
        path = root / f"{stem}-{n}.md"
        n += 1
    return path


# ---------------------------------------------------------------- rendering

def _yaml_quote(s: str) -> str:
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _quote_block(text: str) -> str:
    """Markdown blockquote, so a passage can never be mistaken for prose."""
    return "\n".join("> " + line if line.strip() else ">"
                     for line in text.strip().splitlines())


def render(query: str, results: list[CheckResult]) -> str:
    """Build the whole file. Every line here comes from code or from a page."""
    found = [r for r in results if r.passages]
    empty = [r for r in results if not r.passages]
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")

    out: list[str] = ["---",
                      "type: source",
                      "origin: derived",
                      "approved: false",
                      f"query: {_yaml_quote(query)}",
                      f"collected: {stamp}",
                      "collected_by: the-searcher"]
    if found:
        out.append("sources:")
        out += [f"  - {r.url}" for r in found]
    out += ["---", "",
            f"# {query}",
            "",
            "Collected automatically. Passages are verbatim from the pages listed "
            "below and were checked against them; nothing here has been read or "
            "approved yet.",
            ""]

    # Contents — code-generated, so it cannot misdescribe what follows.
    out.append("## Contents")
    out.append("")
    for i, r in enumerate(found, 1):
        title = r.title or r.url
        out.append(f"{i}. **{title}** — {len(r.passages)} passages"
                   + (f", {len(r.caveats)} caveats" if r.caveats else ""))
    if empty:
        out.append(f"{len(found) + 1}. *Nothing found on {len(empty)} other "
                   f"{'page' if len(empty) == 1 else 'pages'}*")
    out.append("")

    for r in found:
        out += ["---", "", f"## {r.title or r.url}", "", f"<{r.url}>", ""]
        if r.status == "[note]" and r.reason:
            out += [f"*{r.reason}*", ""]
        for p in r.passages:
            out += [_quote_block(p.text), ""]
        if r.caveats:
            out += ["**What the page qualifies**", ""]
            for c in r.caveats:
                out += [_quote_block(c.text), ""]

    if empty:
        out += ["---", "", "## Nothing found", ""]
        for r in empty:
            why = r.reason or "no passages"
            out.append(f"- <{r.url}> — {why}")
        out.append("")

    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------- writing

def store(query: str, results: list[CheckResult],
          root: Path = SOURCES) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = new_path(query, root)
    path.write_text(render(query, results), encoding="utf-8")

    # Verify the write the way the vault's own tools do: read it back.
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        raise IOError(f"wrote {path} but it came back empty")
    return path


# ---------------------------------------------------------------- CLI

async def _run(query: str, urls: list[str], dry: bool) -> None:
    pages = await fetch_and_clean(urls)
    for p in pages:
        if not p.ok:
            print(f"{p.status} {p.url}  ({p.reason})")
    results = await check_pages([p for p in pages if p.ok], query)
    results += [CheckResult(url=p.url, status=p.status, reason=p.reason)
                for p in pages if not p.ok]

    text = render(query, results)
    n = sum(len(r.passages) for r in results)
    c = sum(len(r.caveats) for r in results)

    if dry:
        print(text)
    else:
        path = store(query, results)
        print(f"[DONE] {path}")
    print(f"       {n} passages · {c} caveats · "
          f"{sum(1 for r in results if r.passages)}/{len(results)} pages "
          f"· ~{len(text) // 4} tokens")


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) < 2:
        print(__doc__)
        return 1
    asyncio.run(_run(args[0], args[1:], dry="--dry" in sys.argv[1:]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
