"""The scout: find out what the product actually needs before designing it.

A product idea ("a hearing aid front-end") carries requirements nobody stated:
the band of interest, the signal amplitude, the battery budget. The scout
searches the web with Tavily, and Nemotron Nano reads the results and pulls
out the facts that matter for an analog front-end -- each one kept with the
URL it came from, so the architect's targets can be traced to a source.

Without a TAVILY_API_KEY the scout says so and the architect falls back on its
own knowledge, labelled as such.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from . import llm

TAVILY_URL = "https://api.tavily.com/search"


@dataclass
class Fact:
    claim: str
    source: str
    title: str = ""


@dataclass
class Findings:
    queries: list[str] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    note: str = ""

    def brief(self) -> str:
        if not self.facts:
            return "(no web research available)"
        return "\n".join(
            f"[{i + 1}] {f.claim}  (source: {f.source})"
            for i, f in enumerate(self.facts)
        )

    def to_dict(self) -> dict:
        return {
            "queries": self.queries,
            "facts": [f.__dict__ for f in self.facts],
            "sources": self.sources,
            "note": self.note,
        }


def tavily_key() -> str:
    return os.environ.get("TAVILY_API_KEY", "").strip()


def search(query: str, max_results: int = 5) -> list[dict]:
    body = json.dumps({
        "query": query,
        "search_depth": "advanced",
        "max_results": max_results,
        "include_answer": False,
    }).encode()
    req = urllib.request.Request(
        TAVILY_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {tavily_key()}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r).get("results", [])


QUERY_PROMPT = """You plan web searches for an analog IC designer.
Product idea: {idea}

Write two short web search queries that would find the electrical \
requirements of this product's sensor/audio/signal front-end: signal \
bandwidth, signal amplitude, required amplifier gain, power budget, and \
typical load. Reply with JSON: {{"queries": ["...", "..."]}}"""

EXTRACT_PROMPT = """Product idea: {idea}

Below are web search results. Extract up to 6 concrete, numeric facts that \
matter for designing this product's analog front-end and its decision logic \
(signal bandwidth, signal amplitude, gain needed, power or battery budget, \
sample rate, load). Use ONLY what the sources say -- do not add numbers from \
memory. Each fact must name the URL it came from.

Reply with JSON: {{"facts": [{{"claim": "...", "source": "https://..."}}]}}

RESULTS:
{results}"""


def scout(idea: str, *, ledger: llm.Ledger, emit) -> Findings:
    f = Findings()
    if not tavily_key():
        f.note = "TAVILY_API_KEY not set; architect will rely on its own knowledge"
        emit({"type": "log", "agent": "scout", "msg": f.note})
        return f
    if not llm.online():
        f.queries = [f"{idea} analog front end signal bandwidth amplifier gain power"]
    else:
        try:
            answer, _ = llm.chat(
                "nano",
                [{"role": "user", "content": QUERY_PROMPT.format(idea=idea)}],
                ledger=ledger, json_mode=True, reasoning=False, max_tokens=300,
                temperature=0.3,
            )
            f.queries = [q for q in llm.extract_json(answer).get("queries", []) if isinstance(q, str)][:2]
        except (llm.LLMError, ValueError):
            f.queries = []
        if not f.queries:
            f.queries = [f"{idea} analog front end signal bandwidth amplifier gain power"]

    results: list[dict] = []
    for q in f.queries:
        emit({"type": "log", "agent": "scout", "msg": f"Tavily search: {q}"})
        try:
            hits = search(q)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            emit({"type": "log", "agent": "scout", "msg": f"search failed: {exc}"})
            continue
        for h in hits:
            if any(r["url"] == h.get("url") for r in results):
                continue
            results.append({
                "title": h.get("title", ""),
                "url": h.get("url", ""),
                "content": (h.get("content") or "")[:900],
            })
    f.sources = [{"title": r["title"], "url": r["url"]} for r in results]
    emit({"type": "sources", "sources": f.sources})

    if not results:
        f.note = "web search returned nothing usable"
        return f
    if not llm.online():
        f.facts = [Fact(r["content"][:200], r["url"], r["title"]) for r in results[:3]]
        return f

    blob = "\n\n".join(
        f"URL: {r['url']}\nTITLE: {r['title']}\n{r['content']}" for r in results[:8]
    )
    try:
        answer, _ = llm.chat(
            "nano",
            [{"role": "user", "content": EXTRACT_PROMPT.format(idea=idea, results=blob)}],
            ledger=ledger, json_mode=True, reasoning=False, max_tokens=900,
            temperature=0.2,
            on_delta=lambda k, t: emit({"type": "token", "agent": "scout", "kind": k, "text": t}),
        )
        known = {r["url"]: r["title"] for r in results}
        for item in llm.extract_json(answer).get("facts", []):
            src = str(item.get("source", ""))
            # A fact whose source is not one we actually retrieved is dropped:
            # the model may not invent citations.
            if src in known and item.get("claim"):
                f.facts.append(Fact(str(item["claim"]), src, known[src]))
    except (llm.LLMError, ValueError) as exc:
        f.note = f"fact extraction failed: {exc}"
    return f
