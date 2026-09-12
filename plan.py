"""
The Searcher — layer 5: the planner.

The Sentinel asks one question and gets back a short answer plus the paths of
the files the material was stored in. It never sees the raw passages; that is
the point, and it is what keeps the main conversation's context free.

The loop is here rather than in a chat UI so the budget is enforced by code:
the model may call `research` a few times, and then it must answer with what
it has.

    python3 plan.py "how does LogicFolding differ from chiplet packaging"
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from dataclasses import dataclass, field

import httpx

from searcher import for_planner, research

from config import OLLAMA_URL, MODEL, NUM_CTX, NUM_GPU, TEMPERATURE, TOP_P

# Same window the checker uses. Keeping them equal means Ollama never reloads
# the model between planning and reading. Raise it if the planner ever runs out
# of room, and accept a reload per round in exchange.

MAX_ROUNDS = 10         # a stop, not a target - the model decides when it is done
CALL_TIMEOUT = 900.0

PROMPT = """You research a question using the research tool, then answer it.

Work in English throughout - the queries you send and the answer you write.

Call research with one plain English question. It returns passages copied word \
for word from the pages that answered, each under its source URL.

If a round comes back thin or leaves part of the question open, call it again \
with a differently worded question. You have a few calls, not many.

Then answer in a short paragraph or two. Say what the sources say. Where they \
disagree, say that they disagree. Do not add anything they do not say.

[none] means the web did not answer. Say so plainly."""

TOOLS = [{
    "type": "function",
    "function": {
        "name": "research",
        "description": "Search the web and read what comes back. Returns "
                       "passages copied verbatim from the pages, each with "
                       "its source URL.",
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "One plain English question, written the "
                                   "way you would type it into a search box.",
                },
                "deep": {
                    "type": "boolean",
                    "description": "Read 20 pages instead of 8. Slower. Use it "
                                   "when a round has already come back thin.",
                },
            },
            "required": ["question"],
        },
    },
}]


@dataclass
class Answer:
    question: str
    text: str = ""
    paths: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    rounds: int = 0

    NOTHING = ("[none] the web has no answer to this. That is the finding, and "
               "it is the whole of what research produced. Tell the user the "
               "web does not answer it.")

    def for_sentinel(self) -> str:
        """What the Sentinel receives. Short, with a way back to the material."""
        if not self.text:
            return self.NOTHING
        # The paths deliberately do not travel with the answer. Naming a file
        # is an invitation to open it, and every run that saw one tried to -
        # then treated the failure as a reason to research further. The files
        # are in the vault under sources/ where the user can find them.
        return "[OK] " + self.text.strip()


def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


async def _chat(client: httpx.AsyncClient, messages: list[dict],
                tools: list | None) -> dict:
    body = {
        "model": MODEL,
        "stream": False,
        "messages": messages,
        "options": {"num_ctx": NUM_CTX, "num_gpu": NUM_GPU,
                    "temperature": TEMPERATURE, "top_p": TOP_P},
    }
    if tools:
        body["tools"] = tools
    r = await client.post(OLLAMA_URL, json=body)
    r.raise_for_status()
    return r.json().get("message", {}) or {}


async def ask(question: str, log=None) -> Answer:
    """Run the planner's loop and return its answer with the stored paths."""
    # Default to stderr so a server run leaves the same trace a CLI run does;
    # without it there is no way to tell a slow read from a slow planner.
    say = log or (lambda m: print(m, file=sys.stderr, flush=True))
    ans = Answer(question=question)
    messages = [{"role": "system", "content": PROMPT},
                {"role": "user", "content": question}]

    async with httpx.AsyncClient(timeout=CALL_TIMEOUT) as client:
        for _ in range(MAX_ROUNDS):
            msg = await _chat(client, messages, TOOLS)
            think = (msg.get("thinking") or "").strip()
            if think:
                say(f"[think] {think[:600]}")
            think = (msg.get("thinking") or "").strip()
            if think:
                say(f"[think] {think[:600]}")
            think = (msg.get("thinking") or "").strip()
            if think:
                say(f"[think] {think[:600]}")
            calls = msg.get("tool_calls") or []
            messages.append({k: v for k, v in msg.items() if k != "thinking"})

            if not calls:
                text = _strip_think(msg.get("content", ""))
                # Ollama's parser sometimes misses this model's tool-call
                # syntax and hands it back as prose. That is not an answer,
                # and passing it on puts raw markup in front of the Sentinel.
                if re.search(r"<function\s+name=|<param\s+name=", text):
                    say("[note] unparsed tool call in the reply — retrying")
                    messages.append({"role": "user", "content":
                                     "Answer in plain prose. Do not write a "
                                     "function call."})
                    continue
                ans.text = text
                return ans

            for call in calls:
                fn = call.get("function", {})
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {"question": args}
                q = (args.get("question") or question).strip()
                deep = bool(args.get("deep"))

                ans.rounds += 1
                ans.queries.append(q)
                say(f"round {ans.rounds}: {q}" + (" (deep)" if deep else ""))

                res = await research(q, deep=deep, log=say)
                if res.path:
                    ans.paths.append(res.path)
                messages.append({"role": "tool", "name": "research",
                                 "content": for_planner(res)})

        # Budget spent. One more turn, no tools: answer with what is in hand.
        say("budget spent — asking for the answer")
        messages.append({"role": "user",
                         "content": "Answer now with what you have."})
        msg = await _chat(client, messages, None)
        ans.text = _strip_think(msg.get("content", ""))
    return ans


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 1
    ans = asyncio.run(ask(" ".join(args),
                          log=lambda m: print(m, file=sys.stderr)))
    print()
    print(ans.for_sentinel())
    print(f"\n[{ans.rounds} research calls: "
          + " | ".join(ans.queries) + "]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
