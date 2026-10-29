"""A stand-in for Nebius Token Factory, for testing the live code path.

Speaks the OpenAI-compatible protocol the real service does -- /v1/models,
streamed /v1/chat/completions with reasoning deltas, inline <think> tags and
a final usage chunk -- and answers each agent's prompt with a plausible reply.
It cannot tell you whether Nemotron reasons well; it tells you whether every
byte of the integration around it works.

    python tests/mock_tokenfactory.py 8765
    # then NEBIUS_BASE_URL=http://127.0.0.1:8765/v1 TAVILY_URL=http://127.0.0.1:8765/search
"""

from __future__ import annotations

import json
import math
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODELS = ["nvidia/Nemotron-3-Ultra-550B-A55B", "nvidia/nemotron-3-super-120b-a12b",
          "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B", "meta-llama/Llama-3.3-70B-Instruct"]
CALLS: list[dict] = []
# Set by tests: "disconnect" drops the connection mid-answer; "garbage" puts a
# malformed chunk in the middle of the stream.
FAULT = ""


def reply_for(body: dict) -> tuple[str, str, bool]:
    """(thinking, answer, inline_think_tags)"""
    msgs = body["messages"]
    system = next((m["content"] for m in msgs if m["role"] == "system"), "")
    user = msgs[-1]["content"]
    if '{"ok": true}' in user:
        return "", '{"ok": true}', False
    if "chief architect" in system:
        if "compiler rejected" in user:
            plan = {"part_number": "TPO-PPG1", "name": "PulseFront", "tagline": "PPG front-end",
                    "signal_chain": "photodiode -> amplifier -> comparator",
                    "blocks": [{"id": "afe", "kind": "analog", "title": "PPG amplifier",
                                "spec": "op-amp with 60dB gain, 2MHz bandwidth, under 200uW, 2pF load",
                                "why": "PPG AC component is ~1 mV [1]"},
                               {"id": "beat", "kind": "digital", "title": "Beat detector",
                                "cell": "comparator1", "why": "threshold crossing"}]}
            return "Lower the gain to fit the envelope.", json.dumps(plan), True
        # First answer deliberately breaks the compiler, to exercise repair.
        plan = {"part_number": "TPO-PPG1", "name": "PulseFront", "blocks": [
            {"id": "afe", "kind": "analog", "title": "PPG amplifier",
             "spec": "op-amp with 120dB gain, 2MHz bandwidth, under 200uW, 2pF load"},
            {"id": "beat", "kind": "digital", "cell": "comparator1"}]}
        return "The PPG signal is small, so I want a lot of gain.", "```json\n" + json.dumps(plan) + "\n```", True
    if "red-team engineer" in system:
        hyps = [
            {"claim": "pm", "mechanism": "Fast corner raises GBW toward the output pole.",
             "fixed": {"corner": "ff"}, "axis": "cl_x", "toward": 5},
            {"claim": "gbw", "mechanism": "Slow cold devices lose gm.",
             "fixed": {"corner": "ss", "temp": -40}, "axis": "vdd", "toward": 1.5},
            {"claim": "gain", "mechanism": "Input pair leaves saturation near the rail.",
             "fixed": {}, "axis": "vcm", "toward": 0.9},
            {"claim": "pwr", "mechanism": "Hot fast silicon on a high supply.",
             "fixed": {"corner": "ff", "temp": 85}, "axis": "vdd", "toward": 2.0},
        ]
        if "JUST RE-SIZED" in user:
            hyps = [{"claim": "pm", "mechanism": "The repair grew Cc; check the slow-hot corner.",
                     "fixed": {"corner": "ss", "temp": 85}, "axis": "cl_x", "toward": 4}]
        return "Margins are thin on PM; attack the output pole.", json.dumps({"hypotheses": hyps}), False
    if "sizing a circuit against a" in system:
        vals = {}
        for name, lo, hi in re.findall(r"^\s+(\w+): .*\[([\d.e+-]+) \.\. ([\d.e+-]+)\]", user, re.M):
            lo, hi = float(lo), float(hi)
            vals[name] = math.sqrt(lo * hi) * (1.3 ** (len(CALLS) % 5 - 2))
        return "", json.dumps({"reasoning": "Raise ibias to lift gm; widen the input pair.",
                               "values": vals}), False
    if "commercial operational amplifiers" in user:
        urls = re.findall(r"URL: (\S+)", user)
        return "", json.dumps({"part": "OPA-TEST", "manufacturer": "Example Corp",
                               "source": urls[0] if urls else "x",
                               "specs": [{"name": "gbw", "text": "1.5 MHz"},
                                         {"name": "supply_current", "text": "8 µA"},
                                         {"name": "gain", "text": "140 dB"}]}), False
    if "plan web searches" in user:
        return "", '{"queries": ["PPG signal bandwidth amplitude", "PPG AFE power"]}', False
    if "Extract up to 6" in user:
        urls = re.findall(r"URL: (\S+)", user)
        facts = [{"claim": "PPG AC amplitude is about 1 mV", "source": urls[0] if urls else "x"},
                 {"claim": "invented citation", "source": "https://not-retrieved.example"}]
        return "", json.dumps({"facts": facts}), False
    if "must not appear" in user:
        return "", json.dumps({"overview": "A PPG front-end.", "features": ["Two-stage amplifier"],
                               "applications": ["wearables"]}), False
    if "front page of a datasheet" in user:
        # Two unsupported figures, to exercise the auditor's rewrite.
        return "", json.dumps({"overview": "A PPG front-end with 140 dB of gain.",
                               "features": ["60.0dB minimum gain", "3 nV noise"],
                               "applications": ["wearables"]}), False
    return "", "{}", False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            self._json({"data": [{"id": m} for m in MODELS]})
        else:
            self.send_error(404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path.rstrip("/").endswith("/search"):          # Tavily
            if "op amp datasheet" in body.get("query", ""):
                return self._json({"results": [{
                    "title": "OPA-TEST micropower op amp datasheet",
                    "url": "https://example.org/opa-test",
                    "content": "OPA-TEST: Gain bandwidth: 1.5 MHz. Quiescent current 8 µA."}]})
            return self._json({"results": [{
                "title": "PPG signal basics", "url": "https://example.org/ppg",
                "content": "The pulsatile AC part of a PPG signal is about 1 mV, 0.5-5 Hz."}]})
        if self.headers.get("Authorization") != "Bearer test-key":
            return self._json({"error": "bad key"}, 401)
        if body["model"] not in MODELS:
            return self._json({"error": f"model {body['model']} not found"}, 404)
        CALLS.append(body)
        think, answer, inline = reply_for(body)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def send(delta):
            chunk = {"choices": [{"delta": delta}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())

        if FAULT == "disconnect":
            send({"content": answer[:10]})
            return                              # HTTP/1.0: closing ends the stream
        if FAULT == "garbage":
            self.wfile.write(b"data: {not json}\n\n")
        if think and inline:
            text = f"<think>{think}</think>{answer}"
            for i in range(0, len(text), 7):          # split tags across chunks
                send({"content": text[i:i + 7]})
        else:
            if think:
                send({"reasoning_content": think})
            for i in range(0, len(answer), 40):
                send({"content": answer[i:i + 40]})
        usage = {"prompt_tokens": 100, "completion_tokens": len(answer) // 4}
        self.wfile.write(f"data: {json.dumps({'choices': [], 'usage': usage})}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")

    def _json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(port: int = 0) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    srv = serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8765)
    print(f"mock Token Factory on http://127.0.0.1:{srv.server_port}/v1")
    threading.Event().wait()
