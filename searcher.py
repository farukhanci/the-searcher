"""
The Searcher — layer 4: search, and the tool itself.

    research(question) -> passages grouped by source URL

Everything between those two points is code. The planner names what it wants;
it does not choose URLs, open pages, decide what is relevant, or write files.

    python3 searcher.py "what CPU does the Kirin 9050 Pro have"
    python3 searcher.py --deep "..."        # 20 pages instead of 8
    python3 searcher.py --dry "..."         # do not write to the vault
"""

from __future__ import annotations

import asyncio
import time
import sys
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from check import CheckResult, check_pages
from fetch_clean import fetch_and_clean
from store import render, store

# ---------------------------------------------------------------- settings

SEARXNG = "http://localhost:8080/search"
SEARCH_TIMEOUT = 30.0

# When every engine on the local instance has been rate-limited, a public
# instance answers from a different address and is usually unaffected. This is
# the only outward-facing part of the searcher, it costs nothing, needs no key,
# and it runs ONLY when the local instance comes back empty.
# Scanned all 77 reachable public instances on searx.space; this was the only
# one still serving the JSON API. Most disable it to discourage abuse, so this
# list will need re-scanning when it stops answering.
FALLBACK_INSTANCES = [
    "https://search.mectov.my.id/search",
]
FALLBACK_TIMEOUT = 12.0     # a dead public instance must not stall a round

# A planner asks several near-identical questions inside one run, and each one
# is a fresh hit on the upstream engines. Holding results briefly cuts that
# volume without changing any answer.
CACHE_TTL = 900.0
CACHE_MAX = 200
SEARCH_PAUSE = 1.5     # general engines rate-limit hard; seven of eight were
                       # suspended after one day of testing without this

DEFAULT_PAGES = 8      # per category, so 16 in a mixed round
DEEP_PAGES = 16        # per category
PER_DOMAIN = 2          # one site should not be able to fill a whole round

# Sites that never carry article text. Dropped in code so the reader model is
# never spent on them; every one of these turned up in the test runs.
SKIP_DOMAINS = {
    "facebook.com", "instagram.com", "x.com", "twitter.com", "t.co",
    "youtube.com", "youtu.be", "tiktok.com", "pinterest.com",
    "linkedin.com", "threads.net",
}

# Cap on what goes back to the planner. The stored file always gets everything;
# this only bounds the context the planner has to carry round over round.
RETURN_TOKEN_BUDGET = 6000


@dataclass
class Research:
    question: str
    results: list[CheckResult]
    path: str | None = None       # where it was stored, if it was

    @property
    def passages(self) -> int:
        return sum(len(r.passages) for r in self.results)


# ---------------------------------------------------------------- search

def _domain(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}


def _cached(key: tuple[str, str]) -> list[dict] | None:
    got = _cache.get(key)
    if got and time.monotonic() - got[0] < CACHE_TTL:
        return got[1]
    return None


def _remember(key: tuple[str, str], results: list[dict]) -> None:
    if len(_cache) >= CACHE_MAX:
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:CACHE_MAX // 2]:
            del _cache[k]
    _cache[key] = (time.monotonic(), results)


async def _one_instance(client: httpx.AsyncClient, base: str, query: str,
                        categories: str, timeout: float) -> list[dict]:
    r = await client.get(base, timeout=timeout,
                         params={"q": query, "format": "json",
                                 "language": "en", "categories": categories})
    r.raise_for_status()
    return r.json().get("results", []) or []


async def _hits(client: httpx.AsyncClient, query: str,
                categories: str) -> list[dict]:
    """Local instance first; a public one only if the local one came back empty."""
    key = (query, categories)
    got = _cached(key)
    if got is not None:
        return got

    results: list[dict] = []
    try:
        results = await _one_instance(client, SEARXNG, query, categories,
                                      SEARCH_TIMEOUT)
    except (httpx.HTTPError, ValueError):
        results = []

    if not results:
        for base in FALLBACK_INSTANCES:
            try:
                results = await _one_instance(client, base, query, categories,
                                              FALLBACK_TIMEOUT)
            except (httpx.HTTPError, ValueError):
                continue
            if results:
                print(f"[note] local {categories} search was empty; "
                      f"{len(results)} results from {base.split('/')[2]}",
                      file=sys.stderr, flush=True)
                break

    _remember(key, results)
    return results


def _interleave(a: list[dict], b: list[dict]) -> list[dict]:
    """One from each in turn, so neither list can crowd the other out."""
    out = []
    for i in range(max(len(a), len(b))):
        if i < len(a):
            out.append(a[i])
        if i < len(b):
            out.append(b[i])
    return out


async def search(query: str, want: int = DEFAULT_PAGES) -> list[str]:
    """
    Ask SearXNG and return URLs worth fetching, best first.

    Two categories are queried and interleaved: `general` for the open web and
    `science` for the academic engines (arXiv, Semantic Scholar, Crossref,
    PubMed, Scholar). Interleaved rather than concatenated because a paper
    should not have to outrank eight news articles to be read - and when the
    science side comes back empty, this degrades to a plain general search
    with no decision made anywhere.
    """
    async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT) as client:
        general, science = await asyncio.gather(
            _hits(client, query, "general"),
            _hits(client, query, "science"),
        )
    await asyncio.sleep(SEARCH_PAUSE)
    # Each category gets its own full budget rather than sharing one. A shared
    # eight gave a news topic only four general pages where it used to have
    # eight; an academic topic needs its papers just as much. They do not
    # compete for the same answer, so they do not share a budget.
    hits = _interleave(general[:want], science[:want])

    urls: list[str] = []
    seen: set[str] = set()
    per_domain: dict[str, int] = {}

    for hit in hits:
        url = (hit.get("url") or "").strip()
        if not url.startswith("http") or url in seen:
            continue
        dom = _domain(url)
        if any(dom == d or dom.endswith("." + d) for d in SKIP_DOMAINS):
            continue
        if per_domain.get(dom, 0) >= PER_DOMAIN:
            continue
        seen.add(url)
        per_domain[dom] = per_domain.get(dom, 0) + 1
        urls.append(url)
        if len(urls) >= want * 2:
            break
    return urls


# ---------------------------------------------------------------- the tool

def for_planner(res: Research) -> str:
    """
    What the planner sees. Passages grouped under their source URL, so a fact
    can never be separated from where it came from.
    """
    found = [r for r in res.results if r.passages]
    if not found:
        tried = len(res.results)
        return (f"[none] nothing usable for: {res.question}\n"
                f"searched and read {tried} "
                f"{'page' if tried == 1 else 'pages'}")

    out = [f"[OK] {res.passages} passages from {len(found)} pages"]
    budget = RETURN_TOKEN_BUDGET
    truncated = 0

    for r in found:
        block = [f"\n## {r.title or r.url}", f"SOURCE: {r.url}"]
        for p in r.passages:
            block.append(f"- {p.text}")
        for c in r.caveats:
            block.append(f"- (caveat) {c.text}")
        cost = sum(len(line) for line in block) // 4
        if cost > budget:
            truncated += 1
            continue
        budget -= cost
        out += block

    if truncated:
        out.append(f"\n[note] {truncated} more pages were stored but left out "
                   f"here to keep this reply bounded")
    return "\n".join(out)


async def research(question: str, deep: bool = False,
                   dry: bool = False, log=None) -> Research:
    """Search, read, verify, store. The planner calls this and nothing else."""
    say = log or (lambda *_: None)
    want = DEEP_PAGES if deep else DEFAULT_PAGES

    urls = await search(question, want)
    say(f"search: {len(urls)} urls")
    if not urls:
        return Research(question, [])

    pages = await fetch_and_clean(urls)
    good = [p for p in pages if p.ok]
    say(f"fetch : {len(good)}/{len(pages)} usable "
        f"({sum(p.tokens for p in good)} tokens of text)")

    results: list[CheckResult] = []
    for i, page in enumerate(good, 1):
        say(f"read  : {i}/{len(good)} {page.url[:60]}")
        results += await check_pages([page], question)
    results += [CheckResult(url=p.url, status=p.status, reason=p.reason)
                for p in pages if not p.ok]

    res = Research(question, results)
    if not dry:
        try:
            res.path = str(store(question, results))
            say(f"stored: {res.path}")
        except OSError as e:
            say(f"[note] could not store: {e}")
    return res


# ---------------------------------------------------------------- CLI

def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 1
    question = " ".join(args)
    flags = sys.argv[1:]

    def say(msg: str) -> None:
        print(msg, file=sys.stderr)

    res = asyncio.run(research(question,
                               deep="--deep" in flags,
                               dry="--dry" in flags,
                               log=say))
    print()
    if "--file" in flags:
        print(render(question, res.results))
    else:
        print(for_planner(res))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
