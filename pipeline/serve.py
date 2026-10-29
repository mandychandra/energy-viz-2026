#!/usr/bin/env python3
"""Local web server for the map with HTTP Range support (PMTiles needs it;
python -m http.server does not do ranges).  Run from the project root:

    python3 pipeline/serve.py          # then open http://localhost:8000
"""
import os
import re
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000


class RangeHandler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def send_head(self):
        path = self.translate_path(self.path)
        rng = re.fullmatch(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
        if not rng or os.path.isdir(path) or not os.path.exists(path):
            return super().send_head()
        size = os.path.getsize(path)
        start = int(rng.group(1))
        end = min(int(rng.group(2)) if rng.group(2) else size - 1, size - 1)
        if start > end:
            self.send_error(416)
            return None
        f = open(path, "rb")
        f.seek(start)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self._range_len = end - start + 1
        return f

    def copyfile(self, source, outputfile):
        n = getattr(self, "_range_len", None)
        if n is None:
            return super().copyfile(source, outputfile)
        while n > 0:
            chunk = source.read(min(n, 1 << 16))
            if not chunk:
                break
            outputfile.write(chunk)
            n -= len(chunk)
        self._range_len = None

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, fmt, *args):  # quieter log: skip tile requests
        if ".pmtiles" not in self.path:
            super().log_message(fmt, *args)


if __name__ == "__main__":
    print(f"Serving {ROOT} at http://localhost:{PORT}  (Ctrl-C to stop)")
    ThreadingHTTPServer(("", PORT), RangeHandler).serve_forever()
