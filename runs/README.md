# Recorded runs

Unedited output files from real runs, kept as evidence for claims made in the
main README. Nothing here has been trimmed or tidied — a file that had been
cleaned up would not be worth keeping.

Each file is what the system writes to `SEARCHER_OUTPUT`: frontmatter with the
query and the source URLs, then the verified passages quoted under the page
each came from.

## The files

**`2026-09-09-kirin-9050-pro-logicfolding-architecture.md`** — collected
15:05:08. A working run on a news topic: 18 verified passages across several
pages, reconciling how different outlets name the same part.

**`2026-09-09-kirin-9050-pro-huawei-flagship-chip-specifications-cpu.md`** —
collected 15:06:09. The same subject, sixty-one seconds later, and empty. The
search engines had begun rate-limiting the machine after the first query.

Read those two together. They are the same system on the same topic a minute
apart, and they are the evidence for two claims at once: a run that finds
nothing writes nothing rather than filling the gap, and thin output usually
means the engines are exhausted rather than the model failing. Every Kirin
query for the next twenty-five minutes returned zero or one passage. Two hours
later, partially recovered, the same subject produced 21.

**`2026-09-09-philosophy-for-children-quran-children-religious-education.md`**
— collected 12:50:01, before the engines were exhausted. An academic run: 17
passages carrying real volume, issue and page citations, including journals
published in Persian and Indonesian, found on an English question.
