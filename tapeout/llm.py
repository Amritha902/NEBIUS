"""Nebius Token Factory client, with the three Nemotron tiers as roles.

Token Factory speaks the OpenAI chat-completions protocol, so this is a small
urllib client rather than another dependency. It streams, because the UI shows
every agent thinking in real time, and it records token usage per model so the
datasheet can say exactly what each tier cost.

Three tiers, picked the way the hackathon brief suggests:

  ULTRA  -- serious reasoning: the architect that turns a product idea into a
            chip plan, and repairs that plan when the compiler rejects it.
  SUPER  -- the closed-loop sizing agent: reads simulator output, proposes
            transistor sizes, ~10-20 calls per block.
  NANO   -- fast everyday calls: pulling numbers out of web research, writing
            the datasheet prose.

Model ids move, so they are discovered from GET /v1/models at startup and can
be pinned with TAPEOUT_MODEL_ULTRA / _SUPER / _NANO.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

BASE_URL = os.environ.get(
    "NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1"
).rstrip("/")

# Fallbacks if discovery cannot run. Discovery wins when it finds a match.
DEFAULTS = {
    "ultra": "nvidia/nemotron-3-ultra-550b-a55b",
    "super": "nvidia/nemotron-3-super-120b-a12b",
    "nano": "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
}

# Rough list price per million tokens (input, output), for the cost line on
# the datasheet. Indicative only; Token Factory's console is the source of truth.
PRICE = {
    "ultra": (1.00, 3.00),
    "super": (0.30, 0.90),
    "nano": (0.06, 0.24),
}

THINK = re.compile(r"<think>(.*?)</think>", re.S)


def api_key() -> str:
    return os.environ.get("NEBIUS_API_KEY", "").strip()


def online() -> bool:
    return bool(api_key()) and os.environ.get("TAPEOUT_OFFLINE", "") != "1"


@dataclass
class Usage:
    calls: int = 0
    prompt: int = 0
    completion: int = 0
    seconds: float = 0.0


@dataclass
class Ledger:
    """Per-tier usage for one tape-out run."""

    by_tier: dict[str, Usage] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, tier: str, prompt: int, completion: int, seconds: float) -> None:
        with self.lock:
            u = self.by_tier.setdefault(tier, Usage())
            u.calls += 1
            u.prompt += prompt
            u.completion += completion
            u.seconds += seconds

    def summary(self) -> list[dict]:
        rows = []
        for tier in ("ultra", "super", "nano"):
            u = self.by_tier.get(tier)
            if not u:
                continue
            pin, pout = PRICE[tier]
            rows.append({
                "tier": tier,
                "model": MODELS.get(tier, ""),
                "calls": u.calls,
                "prompt_tokens": u.prompt,
                "completion_tokens": u.completion,
                "seconds": round(u.seconds, 1),
                "usd": round((u.prompt * pin + u.completion * pout) / 1e6, 5),
            })
        return rows


MODELS: dict[str, str] = {}
_discovered = False
_discover_lock = threading.Lock()


def discover() -> dict[str, str]:
    """Resolve each tier to a model id this key can actually call."""
    global _discovered
    with _discover_lock:
        if _discovered:
            return MODELS
        ids: list[str] = []
        if online():
            try:
                req = urllib.request.Request(
                    f"{BASE_URL}/models",
                    headers={"Authorization": f"Bearer {api_key()}"},
                )
                with urllib.request.urlopen(req, timeout=15) as r:
                    ids = [m["id"] for m in json.load(r).get("data", [])]
            except (urllib.error.URLError, OSError, ValueError, KeyError):
                ids = []

        for tier in ("ultra", "super", "nano"):
            pinned = os.environ.get(f"TAPEOUT_MODEL_{tier.upper()}")
            if pinned:
                MODELS[tier] = pinned
                continue
            MODELS[tier] = _pick(ids, tier) or DEFAULTS[tier]

        # A key without Ultra access still gets a working architect.
        if ids and MODELS["ultra"] not in ids and MODELS["super"] in ids:
            MODELS["ultra"] = MODELS["super"]
        _discovered = True
        return MODELS


def _pick(ids: list[str], tier: str) -> str | None:
    cands = [
        i for i in ids
        if "nemotron" in i.lower() and tier in i.lower()
        and not any(x in i.lower() for x in ("omni", "vl", "embed", "rerank", "safety", "guard"))
    ]
    if not cands:
        return None
    # Prefer the newest generation: "nemotron-3" over older Nano v2 etc.
    cands.sort(key=lambda i: ("-3" in i.lower() or "3-" in i.lower(), len(i)), reverse=True)
    return cands[0]


class LLMError(RuntimeError):
    pass


def chat(
    tier: str,
    messages: list[dict],
    *,
    ledger: Ledger | None = None,
    on_delta: Callable[[str, str], None] | None = None,
    max_tokens: int = 2048,
    temperature: float = 0.6,
    json_mode: bool = False,
    reasoning: bool = True,
    timeout: float = 180.0,
) -> tuple[str, str]:
    """One streamed completion. Returns (answer, thinking).

    `on_delta(kind, text)` is called as tokens arrive, kind being "think" or
    "text". Thinking is collected from either `reasoning_content` deltas or
    inline <think> tags, whichever the model's chat template uses.
    """
    if not online():
        raise LLMError("NEBIUS_API_KEY is not set")
    model = discover()[tier]

    body: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if not reasoning:
        # Nemotron chat templates honour this switch; others ignore it.
        body["chat_template_kwargs"] = {"enable_thinking": False}

    data = json.dumps(body).encode()
    t0 = time.perf_counter()
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            return _stream(data, tier, ledger, on_delta, timeout, t0)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            last_err = LLMError(f"{model}: HTTP {exc.code} {detail}")
            # response_format is not supported everywhere; drop it and retry.
            if exc.code == 400 and json_mode and "response_format" in body:
                body.pop("response_format")
                data = json.dumps(body).encode()
                continue
            if exc.code == 400 and "chat_template_kwargs" in body:
                body.pop("chat_template_kwargs")
                data = json.dumps(body).encode()
                continue
            if exc.code not in (429, 500, 502, 503, 504):
                break
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last_err = LLMError(f"{model}: {exc}")
        time.sleep(1.5 * (attempt + 1))
    raise last_err or LLMError("request failed")


def _stream(data, tier, ledger, on_delta, timeout, t0) -> tuple[str, str]:
    req = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=data,
        headers={
            "Authorization": f"Bearer {api_key()}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
    )
    text, think = [], []
    usage = {}
    in_tag = False
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except ValueError:
                continue
            if chunk.get("usage"):
                usage = chunk["usage"]
            for choice in chunk.get("choices", []):
                delta = choice.get("delta") or {}
                r = delta.get("reasoning_content") or delta.get("reasoning")
                if r:
                    think.append(r)
                    if on_delta:
                        on_delta("think", r)
                c = delta.get("content")
                if not c:
                    continue
                # Inline <think> tags, split across chunks.
                while c:
                    if in_tag:
                        end = c.find("</think>")
                        part, c = (c, "") if end < 0 else (c[:end], c[end + 8:])
                        if end >= 0:
                            in_tag = False
                        if part:
                            think.append(part)
                            if on_delta:
                                on_delta("think", part)
                    else:
                        start = c.find("<think>")
                        part, c = (c, "") if start < 0 else (c[:start], c[start + 7:])
                        if start >= 0:
                            in_tag = True
                        if part:
                            text.append(part)
                            if on_delta:
                                on_delta("text", part)

    answer = THINK.sub("", "".join(text)).strip()
    if ledger is not None:
        ledger.add(
            tier,
            int(usage.get("prompt_tokens", 0)),
            int(usage.get("completion_tokens", 0)),
            time.perf_counter() - t0,
        )
    return answer, "".join(think)


def extract_json(text: str) -> dict:
    """The outermost JSON object in a reply, tolerating fences and prose."""
    from bias.agent import _extract_json

    text = THINK.sub("", text)
    blob = _extract_json(text)
    if blob is None:
        raise ValueError(f"no JSON object in reply: {text[:200]!r}")
    return json.loads(blob)
