"""Authenticated read-only HTTP API for Orchestrator Control."""
from __future__ import annotations

import argparse
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from core.ops_status import build_snapshot

ORCH_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TOKEN_FILE = ORCH_ROOT / "secrets" / "ops_api_token"


def _token():
    value = os.environ.get("OPS_API_TOKEN", "").strip()
    if value:
        return value
    path = Path(os.environ.get("OPS_API_TOKEN_FILE", DEFAULT_TOKEN_FILE))
    return path.read_text(encoding="utf-8").strip()


def _allowed_origins():
    raw = os.environ.get("OPS_ALLOWED_ORIGINS", "")
    return {x.strip() for x in raw.split(",") if x.strip()}

class OpsHandler(BaseHTTPRequestHandler):
    server_version = "MyngleOps/0.2"

    def _cors(self):
        origin = self.headers.get("Origin", "")
        allowed = _allowed_origins()
        if origin and origin in allowed:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _send(self, status, payload):
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        supplied = self.headers.get("Authorization", "")
        expected = "Bearer " + _token()
        return hmac.compare_digest(supplied, expected)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/healthz":
            return self._send(200, {"ok": True, "service": "orchestrator-control"})
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        snapshot = build_snapshot()
        if path in ("/", "/api/ops"):
            return self._send(200, {"service": "orchestrator-control", "read_only": True,
                                    "generated_at": snapshot["generated_at"]})
        if path == "/api/ops/summary":
            return self._send(200, {"generated_at": snapshot["generated_at"], **snapshot["summary"]})
        if path == "/api/ops/runs":
            return self._send(200, {"generated_at": snapshot["generated_at"], "runs": snapshot["live"]})
        if path == "/api/ops/handoffs":
            return self._send(200, {"generated_at": snapshot["generated_at"], "handoffs": snapshot["handoffs"]})
        if path == "/api/ops/recent":
            return self._send(200, {"generated_at": snapshot["generated_at"], "runs": snapshot["recent"]})
        if path == "/api/ops/attention":
            return self._send(200, {"generated_at": snapshot["generated_at"], "runs": snapshot["attention"]})
        if path == "/api/ops/history":
            return self._send(200, {"generated_at": snapshot["generated_at"], "runs": snapshot["history"]})
        if path == "/api/ops/capacity":
            return self._send(200, {"generated_at": snapshot["generated_at"], **snapshot["capacity"]})
        if path.startswith("/api/ops/runs/"):
            run_id = path.split("/api/ops/runs/", 1)[1]
            candidates = snapshot["live"] + snapshot["history"]
            run = next((r for r in candidates if r["run_id"] == run_id), None)
            return self._send(200 if run else 404, run or {"error": "run not found"})
        return self._send(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        return

def main():
    parser = argparse.ArgumentParser(description="Authenticated read-only Orchestrator Control API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), OpsHandler)
    print(f"Orchestrator Control API listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
