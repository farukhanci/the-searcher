"""
The Searcher — the server.

Two endpoints, one for each caller:

  /ask       the Sentinel's. One question in, a short answer out, plus the
             paths of the files the material was filed in. The Sentinel never
             carries the raw passages — that is what keeps its context free.

  /research  the raw layer. Search, read, verify, store, and hand back every
             passage. Useful on its own and used internally by /ask.

Deliberately separate from the Sentinel's own server: the Sentinel is the
vault's control layer, the searcher is a consumer that only writes files into
it. Keeping them apart means the searcher's dependencies stay out of the
Sentinel's install, and a failure here cannot take the vault tools down.

    uvicorn serve:app --host 0.0.0.0 --port 8199
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI
from pydantic import BaseModel, Field

from plan import ask
from searcher import DEEP_PAGES, DEFAULT_PAGES, for_planner, research

app = FastAPI(
    title="The Searcher",
    version="1.1.0",
    description="Web research. Ask a question; the answer comes back with the "
                "passages behind it filed in the vault.",
)

# One run at a time. Both endpoints drive a single resident model, so two
# concurrent runs would fight over it and thrash the GPU.
_lock = asyncio.Lock()


def _non_english(q: str) -> bool:
    """True if the question is mostly not Latin script."""
    letters = [c for c in q if c.isalpha()]
    if not letters:
        return False
    latin = sum(1 for c in letters if c.isascii())
    return latin / len(letters) < 0.7
_BUSY = ("[STOP] another research run is still going. Calling again will not start it sooner. Tell the user to ask again in a minute.")


class Ask(BaseModel):
    question: str = Field(
        ...,
        description="What you want to find out, as ONE PLAIN ENGLISH QUESTION. "
                    "Always English, whatever language the conversation is in - "
                    "the model behind this reads English pages and answers in "
                    "English. Translate the user's question before sending it.",
        examples=["how does LogicFolding differ from chiplet packaging"],
    )


class Raw(BaseModel):
    question: str = Field(
        ...,
        description="What to find out, as one plain English question. Write it "
                    "the way you would type it into a search box.",
        examples=["what CPU cores does the Kirin 9050 Pro have"],
    )
    deep: bool = Field(
        False,
        description=f"Read {DEEP_PAGES} pages instead of {DEFAULT_PAGES}. "
                    "Slower. Use it when one round has already come back thin.",
    )


@app.post("/ask", operation_id="research", response_model=str)
async def ask_endpoint(body: Ask) -> str:
    """
    Get the answer to a question from the web.

    What this returns is the answer to the question you asked. Not notes on it,
    not a starting point, not one source among others - the answer. A separate
    model has already searched, read several pages, compared them and written
    it up. Nothing about it is pending.

    So: read it, and tell the user what it says. There is no second step.

    Ask in English, always - whatever language the conversation or the sources
    are in. The model behind this only works in English; a question in another
    language comes back as broken text. It reads foreign-language pages fine on
    an English question. Say the answer to the user in their own language.

    One call at a time. Wait for it to return before making another. A second
    call while one is running is refused, and the two fight over the same GPU.
    """
    if _non_english(body.question):
        return ("[RETRY] the question must be in English. Translate it and "
                "send it again - an English question finds pages in any "
                "language.")
    async with _lock:  # queue rather than refuse; runs are short
        import time
        t0 = time.monotonic()
        ans = await ask(body.question)
        print(f"[ask] {time.monotonic() - t0:.1f}s total, "
              f"{ans.rounds} rounds: {body.question[:70]}",
              file=__import__("sys").stderr, flush=True)
        return ans.for_sentinel()


@app.post("/research", operation_id="research_raw", response_model=str)
async def research_endpoint(body: Raw) -> str:
    """
    Search the web and return the passages themselves, grouped by source URL.

    Every passage is copied word for word from the page it came from and has
    been checked against that page. Returns `[none]` when no page answered.
    """
    if _lock.locked():
        return _BUSY
    async with _lock:
        return for_planner(await research(body.question, deep=body.deep))


@app.get("/health", operation_id="health")
async def health() -> dict:
    return {"status": "ok"}
