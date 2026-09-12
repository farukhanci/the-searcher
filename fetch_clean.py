"""
The Searcher — layer 1: fetch and clean.

Takes URLs, fetches them concurrently, strips the page down to its article body
with trafilatura, and reports what it got. Nothing here talks to a model.

Run it directly to measure extraction on real pages before wiring anything up:

    python3 fetch_clean.py https://example.com/a https://example.com/b
    python3 fetch_clean.py --show https://example.com/a     # print the text too
"""

from __future__ import annotations

import asyncio
import io
import re
import sys
from dataclasses import dataclass, field

import httpx
import trafilatura

try:
    from pypdf import PdfReader
except ImportError:      # PDFs simply stay unreadable if it is not installed
    PdfReader = None

# ---------------------------------------------------------------- settings

TIMEOUT = 30.0          # seconds, matches the Open WebUI default
CONCURRENCY = 6         # parallel fetches
MAX_HTML_BYTES = 8_000_000
# One 2-minute stall in a round came from parsing a single oversized PDF while
# every other page read in a second or two. A cap on the file, not on the text.
MAX_PDF_BYTES = 12_000_000
SHORT_TEXT_CHARS = 400  # below this, the extraction probably failed
MAX_PDF_PAGES = 60      # a thesis is not worth reading whole for one passage

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# trafilatura's precision mode. The independent evaluations that rank it highest
# rank it on precision — it leaves boilerplate out. Recall mode drags menus back in.
EXTRACT_OPTS = dict(
    output_format="markdown",   # headings survive; they help the reader model
    include_comments=False,
    include_tables=True,
    include_links=False,
    favor_precision=True,
    with_metadata=False,
    deduplicate=True,
)


# ---------------------------------------------------------------- result type

@dataclass
class Page:
    url: str
    status: str = "[none]"       # [OK] · [note] · [none] · [RETRY] · [STOP]
    title: str = ""
    text: str = ""
    reason: str = ""             # why it is not [OK]
    raw_chars: int = 0
    meta: dict = field(default_factory=dict)

    @property
    def chars(self) -> int:
        return len(self.text)

    @property
    def tokens(self) -> int:
        """Rough. English prose runs about four characters per token."""
        return self.chars // 4

    @property
    def ratio(self) -> float:
        """Share of the raw HTML that survived. Low means a lot was chrome."""
        return self.chars / self.raw_chars if self.raw_chars else 0.0

    @property
    def ok(self) -> bool:
        return self.status in ("[OK]", "[note]")


# ---------------------------------------------------------------- fetching

async def _fetch_one(client: httpx.AsyncClient, url: str, sem: asyncio.Semaphore) -> Page:
    page = Page(url=url)
    async with sem:
        try:
            r = await client.get(url)
        except httpx.TimeoutException:
            page.status, page.reason = "[RETRY]", f"timed out after {TIMEOUT:.0f}s"
            return page
        except httpx.HTTPError as e:
            page.status, page.reason = "[STOP]", f"{type(e).__name__}: {e}"
            return page

    if r.status_code >= 500:
        page.status, page.reason = "[RETRY]", f"HTTP {r.status_code}"
        return page
    if r.status_code >= 400:
        page.status, page.reason = "[STOP]", f"HTTP {r.status_code}"
        return page

    ctype = r.headers.get("content-type", "").lower()
    body = r.content
    if "application/pdf" in ctype or body[:5] == b"%PDF-":
        if len(body) > MAX_PDF_BYTES:
            page.raw_chars = len(body)
            page.status = "[STOP]"
            page.reason = f"PDF too large to parse ({len(body) // 1_000_000} MB)"
            return page
        return _read_pdf(page, body)

    html = r.text
    page.raw_chars = len(html)
    if page.raw_chars > MAX_HTML_BYTES:
        page.status, page.reason = "[STOP]", f"page too large ({page.raw_chars} chars)"
        return page

    return _clean(page, html)


def _read_pdf(page: Page, data: bytes) -> Page:
    """
    Extract a PDF's text.

    Journals in this vault's fields - DergiPark above all - publish an abstract
    and a reference list on the article page and put the body in a PDF. Without
    this the reader model sees only a citation list and answers from it.
    """
    page.raw_chars = len(data)
    if PdfReader is None:
        page.status = "[STOP]"
        page.reason = "PDF, and pypdf is not installed"
        return page
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = reader.pages[:MAX_PDF_PAGES]
        text = "\n\n".join(p.extract_text() or "" for p in pages).strip()
    except Exception as e:                      # encrypted, damaged, exotic
        page.status = "[STOP]"
        page.reason = f"PDF could not be read: {type(e).__name__}"
        return page

    # A scanned PDF yields a handful of stray characters and needs OCR, which
    # is out of scope; report it as itself rather than as an empty page.
    if len(text) < SHORT_TEXT_CHARS:
        page.status = "[note]"
        page.reason = (f"PDF gave only {len(text)} chars - scanned images "
                       "rather than text")
        page.text = text
        return page

    page.text = re.sub(r"\n{3,}", "\n\n", text)
    try:
        if reader.metadata and reader.metadata.title:
            page.title = str(reader.metadata.title).strip()
    except Exception:
        pass
    page.status = "[OK]"
    return page


def _clean(page: Page, html: str) -> Page:
    """Strip a fetched page to its article body. Sets status on the page."""
    text = trafilatura.extract(html, url=page.url, **EXTRACT_OPTS)

    if not text or not text.strip():
        page.status = "[none]"
        page.reason = "nothing extractable — not an article, or JS-rendered"
        return page

    page.text = text.strip()

    md = trafilatura.extract_metadata(html, default_url=page.url)
    if md:
        page.title = md.title or ""
        page.meta = {
            "author": md.author,
            "date": md.date,
            "sitename": md.sitename,
        }

    if page.chars < SHORT_TEXT_CHARS:
        page.status = "[note]"
        page.reason = f"only {page.chars} chars — paywall, stub, or a listing page"
    else:
        page.status = "[OK]"

    return page


async def fetch_and_clean(urls: list[str]) -> list[Page]:
    """Fetch every URL concurrently and return one Page each, input order kept."""
    sem = asyncio.Semaphore(CONCURRENCY)
    limits = httpx.Limits(max_connections=CONCURRENCY)
    async with httpx.AsyncClient(
        timeout=TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
        limits=limits,
        verify=True,
    ) as client:
        return list(await asyncio.gather(*(_fetch_one(client, u, sem) for u in urls)))


def clean_html(html: str, url: str = "") -> Page:
    """Clean HTML you already have. Useful for tests without network."""
    page = Page(url=url, raw_chars=len(html))
    return _clean(page, html)


# ---------------------------------------------------------------- CLI

def _report(pages: list[Page], show: bool) -> None:
    print(f"{'status':8} {'chars':>7} {'~tok':>6} {'kept':>6}  url")
    print("-" * 78)
    for p in pages:
        kept = f"{p.ratio:.0%}" if p.raw_chars else "-"
        print(f"{p.status:8} {p.chars:>7} {p.tokens:>6} {kept:>6}  {p.url[:60]}")
        if p.reason:
            print(f"{'':8} └─ {p.reason}")

    good = [p for p in pages if p.ok]
    print("-" * 78)
    if good:
        total = sum(p.tokens for p in good)
        print(f"{len(good)}/{len(pages)} usable · {total} tokens total "
              f"· {total // len(good)} avg · {max(p.tokens for p in good)} max")
    else:
        print(f"0/{len(pages)} usable")

    if show:
        for p in good:
            print(f"\n{'=' * 78}\n{p.title or p.url}\n{'=' * 78}\n{p.text}")


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--show"]
    if not args:
        print(__doc__)
        return 1
    pages = asyncio.run(fetch_and_clean(args))
    _report(pages, show="--show" in sys.argv[1:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
