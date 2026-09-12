"""Shared settings. Environment variables override the defaults below.

The defaults are what runs on a 6 GB card: Q8 weights plus q8_0 KV cache at
128k measures 5.6 GB. On a smaller card, lower SEARCHER_NUM_CTX; on a larger
one it can be raised. Both the planner and the checker read NUM_CTX from here
on purpose - Ollama reloads the model whenever the window changes, so the two
must stay equal.
"""

import os

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/chat")
MODEL = os.environ.get("SEARCHER_MODEL", "hf.co/openbmb/MiniCPM5-2B-GGUF:Q8_0")

NUM_CTX = int(os.environ.get("SEARCHER_NUM_CTX", 128000))
NUM_GPU = int(os.environ.get("SEARCHER_NUM_GPU", 256))

# OpenBMB's documented no-think sampling.
TEMPERATURE = 1.0
TOP_P = 0.95

SEARXNG = os.environ.get("SEARXNG_URL", "http://localhost:8080/search")
