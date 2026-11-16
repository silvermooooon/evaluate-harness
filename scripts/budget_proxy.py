"""Meter the two harnesses at the same Responses API boundary."""

import argparse
import json
import re
import shlex
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from pricing import estimate_cost


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--budget", type=float, default=20)
    parser.add_argument("--direct", action="store_true", help="Bypass the env-file HTTP proxy for this evaluation process only")
    args = parser.parse_args()
    config = load_env(Path(args.env_file))
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    server.api_key = config["OPENAI_API_KEY"]
    server.upstream = config.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    server.client = httpx.Client(
        proxy=None if args.direct else config.get("HTTPS_PROXY") or config.get("https_proxy"),
        timeout=httpx.Timeout(300, connect=30), trust_env=False,
    )
    server.ledger = Path(args.ledger)
    server.ledger.parent.mkdir(parents=True, exist_ok=True)
    server.lock = threading.Lock()
    server.budget = args.budget
    server.reserved = 0.0
    server.calls = {}
    server.spent = {}
    if server.ledger.exists():
        for line in server.ledger.read_text().splitlines():
            row = json.loads(line)
            run = row["run_id"]
            charge = row["budget_charge_usd"]
            server.reserved += charge
            server.spent[run] = server.spent.get(run, 0) + charge
            server.calls[run] = server.calls.get(run, 0) + 1
    print(json.dumps({"status": "ready", "port": args.port, "budget_usd": args.budget,
                      "transport": "direct" if args.direct else "existing_env_proxy"}), flush=True)
    server.serve_forever()


def load_env(path):
    config = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.removeprefix("export ").split("=", 1)
        values = shlex.split(value, comments=True)
        config[name.strip()] = values[0] if values else ""
    return config


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path != "/health":
            return self.reply(404, {"error": "unknown_route"})
        self.reply(200, {"status": "ok", "budget_charge_usd": self.server.reserved,
                         "calls": self.server.calls, "per_run_charge": self.server.spent})

    def do_POST(self):
        match = re.fullmatch(r"/([a-zA-Z0-9_-]+)/v1/responses", self.path)
        length = int(self.headers.get("Content-Length", "0"))
        if not match or not 0 < length <= 500000:
            return self.reply(400, {"error": "invalid_route_or_request_size"})
        run = match.group(1)
        try:
            body = json.loads(self.rfile.read(length))
            if body.get("model") != "gpt-6-luna":
                raise ValueError("model")
            if any(t.get("type") != "function" for t in body.get("tools", [])):
                raise ValueError("tool")
        except (ValueError, TypeError, AttributeError):
            return self.reply(400, {"error": "only_gpt_6_luna_and_function_tools_are_enabled"})
        body["reasoning"] = {**body.get("reasoning", {}), "effort": "medium"}
        body["max_output_tokens"] = 8192
        body["service_tier"] = "default"
        body["store"] = False
        payload = json.dumps(body).encode()
        # Byte count bounds text tokens. Reserve the highest standard input rate
        # (12h writes with long context), retaining the reserve if usage is missing.
        reservation = len(payload) * 0.50 / 1_000_000 + 8192 * 1.00 / 1_000_000
        with self.server.lock:
            calls = self.server.calls.get(run, 0)
            spent = self.server.spent.get(run, 0)
            if calls >= 60 or spent + reservation > 1.5 or self.server.reserved + reservation > self.server.budget:
                return self.reply(429, {"error": {"message": "Evaluation request or cost budget exhausted", "type": "budget_exhausted"}})
            self.server.calls[run] = calls + 1
            self.server.reserved += reservation
            self.server.spent[run] = spent + reservation
        started = time.monotonic()
        record = {"run_id": run, "call": calls + 1, "started_unix": time.time(),
                  "model": "gpt-6-luna", "reasoning_effort": "medium", "max_output_tokens": 8192,
                  "request_bytes": len(payload), "stream": bool(body.get("stream")),
                  "tools": [t.get("name") for t in body.get("tools", [])],
                  "budget_charge_usd": reservation, "usage_complete": False}
        try:
            self.forward(payload, record)
        except Exception as exc:
            record["transport_error"] = type(exc).__name__
            if not getattr(self, "response_started", False):
                self.reply(502, {"error": "upstream_transport_error", "class": type(exc).__name__})
        finally:
            record["elapsed_seconds"] = time.monotonic() - started
            with self.server.lock:
                refund = reservation - record["budget_charge_usd"]
                self.server.reserved -= refund
                self.server.spent[run] -= refund
                with self.server.ledger.open("a") as output:
                    output.write(json.dumps(record) + "\n")
            print(json.dumps({k: record.get(k) for k in ["run_id", "call", "http_status", "usage_complete", "estimated_cost_usd", "transport_error"]}), flush=True)

    def forward(self, payload, record):
        headers = {"Authorization": "Bearer " + self.server.api_key, "Content-Type": "application/json"}
        with self.server.client.stream("POST", self.server.upstream + "/responses", content=payload, headers=headers) as response:
            record["http_status"] = response.status_code
            record["request_id"] = response.headers.get("x-request-id")
            self.send_response(response.status_code)
            self.send_header("Content-Type", response.headers.get("content-type", "application/json"))
            self.send_header("Connection", "close")
            self.end_headers()
            self.response_started = True
            self.close_connection = True
            chunks = []
            connected = True
            for chunk in response.iter_bytes():
                chunks.append(chunk)
                if connected:
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        connected = False
                        record["client_disconnected"] = True
            raw = b"".join(chunks)
            objects = []
            if record["stream"]:
                for line in raw.splitlines():
                    if line.startswith(b"data: ") and line != b"data: [DONE]":
                        objects.append(json.loads(line[6:]))
            else:
                objects.append(json.loads(raw))
            for obj in objects:
                value = obj.get("response", obj)
                if value.get("usage"):
                    usage = value["usage"]
                    tokens_in, tokens_out = usage["input_tokens"], usage["output_tokens"]
                    record.update(usage=usage, usage_complete=True, resolved_model=value.get("model"),
                                  response_id=value.get("id"), response_status=value.get("status"),
                                  budget_charge_usd=(tokens_in*0.50+tokens_out*1.00)/1_000_000)
                    record["estimated_cost_usd"] = estimate_cost(record)
                if "error" in obj:
                    error = obj["error"] or {}
                    record["api_error_type"] = error.get("type")
                    record["api_error_code"] = error.get("code")

    def reply(self, status, data):
        raw = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


if __name__ == "__main__":
    main()
