# The Searcher

Ask a question, get an answer built only from what the sources actually said.

The Searcher searches the web through a local SearXNG instance, downloads the
pages it finds, and has a local model read them. Every passage it keeps is
checked, word for word, against the page it came from — if the model
paraphrased, the passage is dropped before it reaches the answer. The result is
written to a markdown file alongside the URL each passage came from.

It runs on a 2.6B model on a 6 GB card. No API keys.

## Why it is built this way

A small model asked to "research this topic" will invent citations. The usual
answers are a bigger model or a stricter prompt. This is a third one: **give
each model call a job whose output code can check.**

The reading model is never asked to summarise or judge. It is asked to copy
sentences out of a page. That job has a mechanical test — is this sentence
actually on that page? — so a fabricated passage cannot survive it. Everything
that needs judgement lives in the code around the model: which pages to fetch,
what to keep, when to stop, what to write where.

Four decisions follow from that, each of which was tried the other way first:

**Every page is read in its own model context.** Not one context holding ten
pages. A long document degrades a small model's grip on the text, and pages
read together bleed into each other. Isolated calls cost more time and buy
accuracy that cannot be recovered later.

**Only passages cross between layers.** The reading model sees the whole page;
the planner never does. It receives 150–300 tokens per page instead of several
thousand, which is what lets a research run go several rounds without the
planner's context filling up.

**The code holds the structure, not the prompt.** Two rules — ask in English,
one call at a time — were written into the prompt, recited correctly by the
model in its own reasoning, and then broken in its behaviour. They are now
mechanical. A rule a model quotes but does not follow is only making the prompt
longer.

**Prompts are short.** Measured on this model, on the same question: no prompt
gave a full answer in 2 calls, a short prompt in 6, and a long structured
prompt produced 8 calls and an answer of three bare words. Small specialised
models get worse when you crowd them.

## How the decisions were made

Nothing here was designed on paper. Each rule came from a run that failed, and
several were reversed after a later run showed the diagnosis was wrong.

Worth naming, because they are the reason the rest of the file can be specific:

- Interleaving academic and general results was blamed twice for bad output and
  reverted twice. Both times the real cause was that seven of eight search
  engines had rate-limited the machine. The rule that came out of it is in the
  limits section: check the engines first.
- "Sixteen pages floods the planner with 80k tokens" was a misreading of a log
  line. That figure is page text going to the readers, one isolated call each;
  the planner sees a fiftieth of it. The revert was withdrawn.
- A freeze that looked like a network problem was diagnosed with `nvidia-smi`,
  `ss` and `top` — GPU idle, sockets in CLOSE-WAIT, 100% CPU in state R — and
  then located to the line with `PYTHONFAULTHANDLER` and `kill -ABRT`. Two
  patches had already been written against guesses and neither could have
  helped.

## What it does not do

**Verification protects the passages, not the answer.** Passages are checked
against their source, so fabrication there is structurally impossible. The
planner then writes a summary from those passages, and that step is unchecked —
it is where an invented citation once appeared in testing. The loop caught it
on a later round and said so, but nothing guarantees that.

**A faithful quote from a wrong page is still wrong.** Verification proves the
quote is accurate, not that the page is. Treat the output as sourced passages,
not as fact. Where sources disagree the answer says so rather than picking one.

**Thin results usually mean an engine problem.** The search engines behind
SearXNG rate-limit, and when several are suspended at once the output degrades
sharply while looking like a model failure. Check SearXNG before changing
anything else — three wrong diagnoses during development all had this cause.

## Requirements

- Python 3.11+ (`asyncio.timeout`)
- [Ollama](https://ollama.com), with a model that supports tool calling
- A [SearXNG](https://docs.searxng.org) instance with the JSON format enabled
- ~6 GB of VRAM at the defaults; 4 GB is enough at a lower `SEARCHER_NUM_CTX`,
  and the measurements are below

On a bare Debian or Ubuntu, none of the commands below exist yet:

```bash
sudo apt install git python3 python3-venv python3-pip curl openssl docker.io
sudo usermod -aG docker $USER   # then log out and back in
```

Without the group change every `docker` command fails on a permission error
at the socket.

Default model is `hf.co/openbmb/MiniCPM5-2B-GGUF:Q8_0`. At `num_ctx` 128000
with a q8_0 KV cache it measures about 5.5 GB — but only with `num_gpu` set
high enough to force every layer onto the card. Without that, Ollama's own
estimate leaves part of the model on the CPU and the same settings report
6.2 GB with a 23/77 CPU/GPU split.

| `SEARCHER_NUM_CTX` | VRAM |
| --- | --- |
| 128000 | 5.5 GB |
| 50000 | 3.6 GB |

All measured on an RTX 4050 Mobile with the whole model on the card; check
`ollama ps` on yours.

**On a 4 GB card**, drop `SEARCHER_NUM_CTX` to 50000. Less context sounds
like it should cost pages, and it does not, because a page never arrives whole
and unbounded: `MAX_HTML_BYTES` and `MAX_PDF_PAGES` cut it before it reaches
the model, and the checker reads one page per call rather than a batch. 32000
was run earlier and behaved the same. A single cleaned page large enough to
fill 50k would have to get past the size caps first.

What the window is actually for is keeping the planner and the checker on the
same number, so Ollama does not reload the model between reading and planning.
Lower it in one place — `config.py` reads it once — and both halves move
together.

## Setup

### 1. The model

```bash
ollama pull hf.co/openbmb/MiniCPM5-2B-GGUF:Q8_0
```

### 2. SearXNG

The Searcher asks SearXNG for JSON, which is off by default.

Create `~/searxng/settings.yml` (`mkdir -p ~/searxng` first):

```yaml
use_default_settings: true

server:
  secret_key: "CHANGE_ME"        # replace - see below
  # SearXNG rate-limits what looks like a bot, and two parallel category
  # queries per round look like one. Left on, it answers 403 to its own
  # user - which is indistinguishable from the engines themselves
  # rate-limiting, and that is the harder problem to diagnose.
  limiter: false

search:
  formats:
    - html
    - json

outgoing:
  # Semantic Scholar was timing out with 200 ms to spare on the 5 s default.
  request_timeout: 10.0
  max_request_timeout: 15.0

engines:
  # API-based, no CAPTCHA, and between them they cover the social sciences and
  # humanities that arXiv and PubMed do not. Google Scholar is enabled by
  # default and mostly answers "unusual traffic"; there is no fix for that.
  - name: crossref
    disabled: false
  - name: openalex
    disabled: false
  # Slow, but worth waiting for now that the timeout allows it.
  - name: semantic scholar
    timeout: 10.0
```

Replace `CHANGE_ME` with output from `openssl rand -hex 32`. SearXNG does not
enforce this - it starts and searches fine with the placeholder - but the key
signs session data, so a published default is a published default.

Then run it:

```bash
docker run -d --name searxng --restart unless-stopped \
  -p 8080:8080 \
  -v ~/searxng:/etc/searxng \
  -v searxng-cache:/var/cache/searxng \
  searxng/searxng:latest
```

Check JSON works, and that the science engines answer:

```bash
curl -s "http://localhost:8080/search?q=test&format=json" | head -c 200
curl -s -G localhost:8080/search --data-urlencode "q=philosophy of education" \
  --data-urlencode "format=json" --data-urlencode "categories=science" \
  | python3 -c "import sys,json;print(len(json.load(sys.stdin)['results']))"
```

### 3. The Searcher

```bash
git clone https://github.com/farukhanci/the-searcher
cd the-searcher
python3 -m venv ~/.venvs/searcher
source ~/.venvs/searcher/bin/activate
pip install -r requirements.txt
```

## Running it

```bash
python3 plan.py "what is a chiplet"
```

As a service, reachable over HTTP:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/the-searcher.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now the-searcher
```

The unit assumes the repo at `~/the-searcher` and the venv at
`~/.venvs/searcher`. Edit it if yours differ. `loginctl enable-linger $USER`
keeps it running when you are not logged in - with `sudo` on a system without
polkit, which answers `Access denied` otherwise.

## HTTP API

Three endpoints on port 8199. The service above is one way to get them; the
other is to run the server yourself, which is what you want while changing
anything:

```bash
source ~/.venvs/searcher/bin/activate
uvicorn serve:app --host 127.0.0.1 --port 8199
```

`--host 0.0.0.0` only if something in a container has to reach it — the unit
uses it for exactly that reason and for no other.

`POST /ask` runs the full loop — search, read, verify, write up an answer.

```bash
curl -s localhost:8199/ask \
  -H 'content-type: application/json' \
  -d '{"question": "how does LogicFolding differ from chiplet packaging"}'
```

`POST /research` returns the verified passages from a single round instead of a
written answer, grouped under their source URLs. `deep` reads more pages.

```bash
curl -s localhost:8199/research \
  -H 'content-type: application/json' \
  -d '{"question": "what CPU cores does the Kirin 9050 Pro have", "deep": true}'
```

`GET /health` reports whether the service is up.

**Questions must be in English.** Another script comes back as `[RETRY]` with a
request to translate. This is the model's limit, not the sources': an English
question finds and reads pages in any language — Persian and Indonesian
journals turned up in testing. Ask in English, answer the user in theirs.

One run at a time; a second request queues rather than being refused.

## What happens inside a run

1. The planner writes a search question.
2. SearXNG is queried twice in parallel — `general` and `science` — and each
   category gets its own page budget rather than sharing one. A news topic
   wants every general hit; an academic one wants every paper. They are not
   competing for the same answer.
3. Pages are fetched concurrently. HTML goes through `trafilatura`; PDFs
   through `pypdf`, detected by content type or the `%PDF-` header so that
   extensionless download links are caught. On the test set, extraction kept
   1–6% of the raw HTML — the rest was navigation and script.
4. Each cleaned page is read in a fresh model context with thinking off.
5. Every returned line is matched against the page. Lines that are not there
   are dropped. Sentences the model shortened with "..." are recovered whole
   from the source.
6. Sentences that qualify the page's own claims ("according to the company",
   "not independently verified") are collected by pattern, whether or not the
   model chose them — across two runs the same model carried such a caveat once
   and dropped it once, so it is not left to the model.
7. Everything is written to a markdown file with frontmatter, an `approved:
   false` checkbox, and each passage quoted under its source URL.
8. The planner reads only the passages. If a round is thin it asks again.

## Configuration

Every value below has a working default; set the variable only to change it.

| Variable | Default | What it does |
| --- | --- | --- |
| `SEARCHER_OUTPUT` | `$SENTINEL_VAULT/sources` | Where finished research is written |
| `SENTINEL_VAULT` | `~/obsidian/Obsidian-1` | Vault root, used only for the default above |

Both path variables expand `~`. And that default vault is one particular
vault — mine. Running this next to the Sentinel means pointing both at the
same place: set `SENTINEL_VAULT` here to whatever `--vault` is there, or set
`SEARCHER_OUTPUT` directly. Left alone, research lands in a directory the
Sentinel is not reading, and nothing says so.
| `SEARCHER_MODEL` | `hf.co/openbmb/MiniCPM5-2B-GGUF:Q8_0` | Ollama model for both planning and reading |
| `SEARCHER_NUM_CTX` | `128000` | Context window. Lower it on a smaller card |
| `SEARCHER_NUM_GPU` | `256` | Layers on the GPU. High enough to mean "all of them" |
| `OLLAMA_URL` | `http://localhost:11434/api/chat` | Where Ollama is |
| `SEARXNG_URL` | `http://localhost:8080/search` | Where SearXNG is |

The two path variables accept `~`, quoted or not — `SEARCHER_OUTPUT="~/notes"`
and a systemd `Environment=` line both work. Before that, the tilde came
through literally and the research was filed in a directory named `~` beside
the code: the write succeeded and the run printed `[DONE]`, so the only sign
was research that was not in the vault. `test_store.py` guards it.

The planner and the reader share one `NUM_CTX` on purpose: Ollama reloads the
model whenever the context window changes, so different values cost a reload
every round.

Page budgets, the caveat limit, the round cap, the pause between searches and
the download size limits are **not** configurable. Each came out of a
measurement, and exposing them hands a tuning problem to someone who has not
made those measurements.

## Known limits

- **Page titles are empty.** The metadata pass was removed after trafilatura's
  `normalize_authors` was caught spinning at 100% CPU for minutes on a page
  whose author field held thousands of names. It runs on the event loop, so it
  blocked the whole round, and an asyncio timeout cannot interrupt it — that
  needs an await point and a C extension has none. Source files show the URL
  instead. Passages are unaffected; they come from a different call.
- **The planner's summary is not verified.** See above. The passages behind it
  are, and they are in the source file.
- **Publisher bot protection.** Some sites return 403 to any non-browser
  client. Nothing in the fetch layer works around this.
- **Journal index pages are read in full.** An author index — every author
  across every volume — is fetched and read like an article, costing time for
  nothing. A URL-pattern filter is the fix and has not been written.
- **The English check is script-based.** It measures how much of the question
  is Latin script, so Turkish or French passes and Cyrillic or Chinese is
  caught. Ask in English regardless.
- **One run at a time**, by design. Concurrent runs contend for the same GPU.


## Tests

```bash
python3 test_store.py
```

Five checks over the `sources` frontmatter field. It is a narrow suite: that
one field was written as a YAML list while the Sentinel's frontmatter reader
is flat by design, and the mismatch silently dropped every URL but the last
from 29 files in a real vault. The test fails if the list form comes back.

## Recorded runs

`runs/` holds unedited output from three real runs, with a note on what each
one demonstrates — including a working run and an empty one on the same topic
sixty-one seconds apart.

## License

MIT — see `LICENSE`.

The model is separate. MiniCPM5-2B is Apache 2.0 and comes with its own terms;
so do SearXNG and the search engines behind it. This license covers the code
in this repository, nothing it talks to.
