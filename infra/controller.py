"""Local deployment adapter for the two official ownCloud control endpoints."""

import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import subprocess
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=2999)
    args = parser.parse_args()
    server = HTTPServer((args.host, args.port), Controller)
    print(f"TAC ownCloud controller listening on {args.host}:{args.port}", flush=True)
    server.serve_forever()


class Controller(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if self.path != "/api/healthcheck/owncloud":
            self.respond(404, {"message": "Unknown endpoint"})
            return
        try:
            request = Request("http://127.0.0.1:8092", method="HEAD")
            with urlopen(request, timeout=10) as response:
                status = 200 if response.status == 200 else 503
        except (HTTPError, URLError, TimeoutError):
            status = 503
        self.respond(status, {"message": "ownCloud reachable" if status == 200 else "ownCloud not ready"})

    def do_POST(self):
        if self.path != "/api/reset-owncloud":
            self.respond(404, {"message": "Unknown endpoint"})
            return
        compose = ["docker", "compose", "-f", str(Path(__file__).with_name("compose.yml")), "-p", "tac-eval"]
        # The upstream reset removes the container and creates it from its seeded image.
        # A fresh anonymous data volume prevents one trial's edits reaching the next.
        for arguments in (["stop", "owncloud"], ["rm", "-f", "-v", "owncloud"], ["up", "-d", "owncloud"]):
            completed = subprocess.run(compose + arguments, check=False)
            if completed.returncode:
                self.respond(503, {"message": "ownCloud reset failed"})
                return
        self.respond(202, {"message": "Reset ownCloud command initiated"})

    def respond(self, status, body):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)


if __name__ == "__main__":
    main()
