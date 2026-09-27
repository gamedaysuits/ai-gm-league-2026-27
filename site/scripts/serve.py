#!/usr/bin/env python3
"""Local preview server that never lets the browser cache (handy while editing).

    python3 site/scripts/serve.py            # serves site/ at http://localhost:8765
    python3 site/scripts/serve.py 9000 dist  # serves site/dist/ on port 9000

`python3 -m http.server -d site 8765` works too; it just lets browsers cache JS/CSS between edits.
"""
import functools
import http.server
import sys
from pathlib import Path

SITE = Path(__file__).resolve().parents[1]


class NoCache(http.server.SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    root = SITE / sys.argv[2] if len(sys.argv) > 2 else SITE
    handler = functools.partial(NoCache, directory=str(root))
    with http.server.ThreadingHTTPServer(("", port), handler) as httpd:
        print(f"serving {root} at http://localhost:{port}")
        httpd.serve_forever()
