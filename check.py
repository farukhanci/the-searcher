"""
The Searcher — layer 2: the checker.

One cleaned page, one question, one isolated model call with thinking off.
The model copies out the sentences that answer the question; this module then
verifies every line it returned actually appears in the page and throws away
anything that does not.

That verification is also the format repair. If the model writes "Here are the
relevant sentences:" or renumbers things, those lines fail to verify and vanish
on their own — so the prompt can stay short, which is what this model wants.

    python3 check.py "what CPU cores does it have" https://example.com/article
"""

from __future__ import annotations

import asyncio
import re
import sys
import unicodedata
from dataclasses import dataclass, field

import httpx

from fetch_clean import Page, fetch_and_clean

# ---------------------------------------------------------------- settings

from config import OLLAMA_URL, MODEL, NUM_CTX, NUM_GPU, TEMPERATURE, TOP_P

# One value for every checker call. Ollama reloads the model when num_ctx
# changes, so varying it per page would cost a reload each time. Measured:
# Q8 weights + q8_0 KV cache at 128k = 5.6 GB on a 6 GB card.
CALL_TIMEOUT = 300.0

# OpenBMB's documented no-think sampling. A lower temperature would paraphrase
# less, but going against a small model's recommended values tends to misbehave
# in other ways — and verification below catches paraphrase regardless, so this
# choice is low-stakes either way.

# Short on purpose. Measured on this model: longer prompts produced worse
# research, not better.
PROMPT = """You are given one web page and one question.

Copy out the sentences from the page that answer the question. Copy each one \
exactly as written - same words, same numbers, same units - one per line.

Copy a sentence that answers only part of the question too. A partial answer is worth having.

If the page says nothing about the question, output nothing at all."""

# Sentences that qualify a page's claims. Collected by code rather than asked
# for, because across two test runs the same model carried the caveat once and
# dropped it once.
CAVEAT_PATTERNS = [
    # the claim has not been checked
    r"\bunverified\b",
    r"\bindependent(?:ly)?\s+\w{0,12}\s*(?:audit|verif|confirm|replicat|review)",
    r"\b(?:no|not|never|without|nor)\b[^.]{0,60}\b(?:verif|confirm|audit)",
    r"\b(?:verif|confirm|audit)\w*\b[^.]{0,40}\b(?:no|not|never)\b",
    # the claim belongs to someone
    r"\b(?:Huawei|the company|the firm) claims?\b",
    r"\b(?:company|firm|maker|vendor|manufacturer|Huawei)[- ]sourced\b",
    r"\bremain\w*\s+\w+[- ]sourced\b",
    r"\bcame? from\b[^.]{0,40}\bitself\b",
    r"\breportedly\b",
    r"\ballegedly\b",
    r"\bsaid to be\b",
]
_CAVEAT_RE = re.compile("|".join(CAVEAT_PATTERNS), re.IGNORECASE)

MANY_PASSAGES = 15
MAX_CAVEATS = 5  # above this the model is copying, not selecting


# ---------------------------------------------------------------- result type

@dataclass
class Passage:
    text: str          # the source's wording, never the model's
    url: str
    caveat: bool = False


@dataclass
class CheckResult:
    url: str
    title: str = ""
    status: str = "[none]"        # [OK] · [note] · [none] · [STOP]
    passages: list[Passage] = field(default_factory=list)
    caveats: list[Passage] = field(default_factory=list)
    reason: str = ""
    dropped: int = 0              # lines the model returned that are not on the page
    raw_lines: int = 0
    raw: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.passages)


# ---------------------------------------------------------------- verification

_QUOTES = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "-", "\u2212": "-", "\u00a0": " ",
})


def _norm(s: str) -> str:
    """Fold away the differences a model introduces without changing meaning."""
    s = unicodedata.normalize("NFKC", s).translate(_QUOTES)
    s = re.sub(r"[*_`#>]", "", s)        # markdown the model may drop or add
    return re.sub(r"\s+", " ", s).strip().lower()


def _find_in_source(candidate: str, source: str, source_norm: str) -> str | None:
    """
    Return the SOURCE's own wording for `candidate`, or None if it is not there.

    Matching happens on the normalised text; what comes back is the original
    span, so a passage is always the page's words and not the model's.
    """
    cand = _norm(candidate)
    if len(cand) < 25:               # too short to be a claim; likely a fragment
        return None

    at = source_norm.find(cand)
    if at == -1:
        # The model shortens long sentences with "...". Treat what it gave as a
        # prefix, find that in the page, and return the page's whole sentence.
        stub = re.sub(r"(?:\.\.\.|…)\s*$", "", cand).strip()
        if len(stub) < len(cand) and len(stub) >= 25:
            at = source_norm.find(stub)
            if at == -1:
                return None
            tail = source_norm[at + len(stub):]
            stop = re.search(r"[.!?](?:\s|$)", tail)
            cand = stub + (tail[:stop.end()] if stop else tail[:400])
        else:
            return None

    # Walk the raw source, counting normalised characters, to find the span.
    seen, start, end = 0, None, None
    prev_space = True
    for i, ch in enumerate(source):
        n = _norm(ch)
        if not n:
            if ch.isspace() and not prev_space and seen:
                n, prev_space = " ", True
            else:
                continue
        else:
            prev_space = n.isspace()
        if seen == at and start is None:
            start = i
        seen += len(n)
        if start is not None and seen >= at + len(cand):
            end = i + 1
            break

    if start is None or end is None:
        return candidate.strip()      # matched but could not be located; rare
    return source[start:end].strip()


def verify(lines: list[str], page: Page) -> tuple[list[Passage], int]:
    """Keep only the returned lines that really appear in the page."""
    source_norm = _norm(page.text)
    kept: list[Passage] = []
    seen: set[str] = set()
    dropped = 0

    for line in lines:
        line = line.strip().lstrip("-•*0123456789. )").strip()
        if not line:
            continue
        found = _find_in_source(line, page.text, source_norm)
        if found is None:
            dropped += 1
            continue
        key = _norm(found)
        if key in seen:
            continue
        seen.add(key)
        kept.append(Passage(text=found, url=page.url,
                            caveat=bool(_CAVEAT_RE.search(found))))
    return kept, dropped


def collect_caveats(page: Page, already: list[Passage]) -> list[Passage]:
    """Pull the page's hedging sentences whether or not the model chose them."""
    have = {_norm(p.text) for p in already}
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+", page.text):
        s = sentence.strip()
        if not (25 < len(s) < 400) or _norm(s) in have:
            continue
        # a caveat is a sentence: it ends like one and has enough words
        if not s.endswith((".", "!", "?")) or len(s.split()) < 6:
            continue
        if _CAVEAT_RE.search(s):
            out.append(Passage(text=s, url=page.url, caveat=True))
    return out[:MAX_CAVEATS]


# ---------------------------------------------------------------- model call

async def _ask(client: httpx.AsyncClient, question: str, page_text: str) -> str:
    body = {
        "model": MODEL,
        "stream": False,
        "think": False,          # ignored by older Ollama; see _no_think below
        "options": {
            "num_ctx": NUM_CTX,
            "num_gpu": NUM_GPU,
            "temperature": TEMPERATURE,
            "top_p": TOP_P,
        },
        "messages": [
            {"role": "system", "content": PROMPT},
            {"role": "user",
             "content": f"QUESTION: {question}\n\nPAGE:\n{page_text}"},
        ],
    }
    r = await client.post(OLLAMA_URL, json=body)
    r.raise_for_status()
    return r.json().get("message", {}).get("content", "") or ""


def _no_think(text: str) -> str:
    """Strip a reasoning block if the runtime emitted one anyway."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


async def check_page(page: Page, question: str,
                     client: httpx.AsyncClient | None = None) -> CheckResult:
    """Read one page against one question. Isolated call, nothing carried over."""
    res = CheckResult(url=page.url, title=page.title)
    if not page.ok or not page.text:
        res.status, res.reason = "[STOP]", page.reason or "no text to read"
        return res

    own = client is None
    client = client or httpx.AsyncClient(timeout=CALL_TIMEOUT)
    try:
        raw = _no_think(await _ask(client, question, page.text))
    except httpx.HTTPError as e:
        res.status, res.reason = "[STOP]", f"model call failed: {e}"
        return res
    finally:
        if own:
            await client.aclose()

    lines = [l for l in raw.splitlines() if l.strip()]
    res.raw_lines = len(lines)
    res.raw = raw
    res.passages, res.dropped = verify(lines, page)
    res.caveats = collect_caveats(page, res.passages)

    if not res.passages:
        res.status = "[none]"
        res.reason = ("nothing on this page answers the question"
                      if not lines else
                      f"model returned {len(lines)} lines, none of them on the page")
    elif len(res.passages) > MANY_PASSAGES:
        res.status = "[note]"
        res.reason = f"{len(res.passages)} passages — copying rather than selecting"
    elif len(res.passages) == 1:
        res.status = "[note]"
        res.reason = "one passage only — may be word overlap rather than an answer"
    else:
        res.status = "[OK]"
    return res


async def check_pages(pages: list[Page], question: str) -> list[CheckResult]:
    """Sequential on purpose: one model is resident, calls cannot overlap."""
    out = []
    async with httpx.AsyncClient(timeout=CALL_TIMEOUT) as client:
        for p in pages:
            out.append(await check_page(p, question, client))
    return out


# ---------------------------------------------------------------- CLI

async def _run(question: str, urls: list[str]) -> None:
    pages = await fetch_and_clean(urls)
    for p in pages:
        if not p.ok:
            print(f"{p.status} fetch {p.url}\n    └─ {p.reason}\n")
    results = await check_pages([p for p in pages if p.ok], question)

    for r in results:
        head = f"{r.status} {r.title or r.url}"
        print(f"\n{head}\n{'-' * min(len(head), 78)}")
        if r.reason:
            print(f"  ({r.reason})")
        for p in r.passages:
            print(f"  • {p.text}")
        for c in r.caveats:
            print(f"  ! {c.text}")
        if r.dropped and "--debug" in sys.argv:
            print("  -- what the model actually returned --")
            for l in r.raw.splitlines():
                if l.strip():
                    print(f"    | {l.strip()[:200]}")
        if r.dropped:
            print(f"  [note] {r.dropped}/{r.raw_lines} returned lines "
                  f"were not on the page and were dropped")

    total = sum(len(r.passages) for r in results)
    drops = sum(r.dropped for r in results)
    print(f"\n{'=' * 78}\n{total} passages from "
          f"{sum(1 for r in results if r.ok)}/{len(results)} pages"
          f" · {drops} unverifiable lines dropped")


def main() -> int:
    if len([a for a in sys.argv[1:] if not a.startswith("--")]) < 2:
        print(__doc__)
        return 1
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    asyncio.run(_run(args[0], args[1:]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
