"""A small browser GUI for wa-clean.

`wa-clean gui` opens your browser on a page served only to this Mac
(127.0.0.1). Press Ctrl+C in the terminal to stop it.
"""

import json
import mimetypes
import os
import secrets
import threading
import time
import webbrowser
from argparse import Namespace
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import core

HERE = os.path.dirname(os.path.abspath(__file__))
PREVIEW_FILES = 48
IMAGE_KINDS = {"photo", "sticker"}
mimetypes.add_type("image/webp", ".webp")


class State(object):
    """The scanned library, shared by every request."""

    def __init__(self, container, force):
        self.container = container
        self.force = force
        self.lock = threading.Lock()
        self.lib = None

    def load(self, refresh=False):
        with self.lock:
            if self.lib is None or refresh:
                self.lib = core.load_library(self.container)
            return self.lib


def chat_summary(files, names):
    chats = defaultdict(lambda: {"total": 0, "unlinked": 0, "count": 0})
    for f in files:
        c = chats[f.chat]
        c["total"] += f.size
        c["count"] += 1
        if not f.linked:
            c["unlinked"] += f.size
    out = [dict(jid=jid, name=names.get(jid, jid), **c)
           for jid, c in chats.items()]
    return sorted(out, key=lambda c: -c["total"])


def filters_to_args(body):
    days = body.get("older_than_days")
    return Namespace(
        chat=body.get("chats") or None,
        exclude_chat=None,
        type=body.get("types") or None,
        older_than=int(days) * 86400 if days else None,
        larger_than=int(body.get("larger_than") or 0) or None,
        unlinked=bool(body.get("unlinked")),
        include_starred=bool(body.get("include_starred")),
    )


def thumbnail_path(f):
    """The file to show for a media item, or None if there's nothing to show."""
    if f.kind in IMAGE_KINDS:
        return f.path
    stem = os.path.splitext(f.path)[0]
    for ext in (".thumb", ".videothumb", ".mmsthumb"):
        if os.path.exists(stem + ext):
            return stem + ext
    return None


def preview(files, names, chosen):
    index = {id(f): i for i, f in enumerate(files)}
    by_chat = defaultdict(lambda: {"size": 0, "count": 0})
    for f in chosen:
        by_chat[f.chat]["size"] += f.size
        by_chat[f.chat]["count"] += 1
    top_chats = sorted(by_chat.items(), key=lambda kv: -kv[1]["size"])[:8]
    largest = sorted(chosen, key=lambda f: -f.size)[:PREVIEW_FILES]
    return {
        "count": len(chosen),
        "size": sum(f.size for f in chosen),
        "chats": [dict(name=names.get(j, j), **v) for j, v in top_chats],
        "more_chats": max(0, len(by_chat) - len(top_chats)),
        "files": [{
            "i": index[id(f)], "chat": names.get(f.chat, f.chat),
            "kind": f.kind, "size": f.size, "linked": f.linked,
            "thumb": thumbnail_path(f) is not None,
            "date": time.strftime("%Y-%m-%d", time.localtime(f.date)),
        } for f in largest],
    }


class Handler(BaseHTTPRequestHandler):
    state = None
    token = None
    allowed_hosts = ()

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self):
        # Blocks DNS-rebinding: other sites can't reach us under their name.
        return self.headers.get("Host", "") in self.allowed_hosts

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        url = urlparse(self.path)
        if url.path == "/":
            with open(os.path.join(HERE, "gui.html"), "rb") as fh:
                page = fh.read().replace(b"__TOKEN__", self.token.encode())
            return self._send(200, page, "text/html; charset=utf-8")
        if url.path == "/file":
            q = parse_qs(url.query)
            if q.get("t", [""])[0] != self.token:
                return self._send(403, {"error": "bad token"})
            files = self.state.load().files
            try:
                f = files[int(q.get("i", ["-1"])[0])]
            except (ValueError, IndexError):
                return self._send(404, {"error": "no such file"})
            path = thumbnail_path(f)
            if not path or not os.path.exists(path):
                return self._send(404, {"error": "no preview"})
            ctype = mimetypes.guess_type(path)[0] or "image/jpeg"
            with open(path, "rb") as fh:
                return self._send(200, fh.read(), ctype)
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok() or self.headers.get("X-Token") != self.token:
            return self._send(403, {"error": "forbidden"})
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad json"})
        route = urlparse(self.path).path
        try:
            if route == "/api/scan":
                return self._send(200, self.scan(body.get("refresh")))
            if route == "/api/preview":
                lib = self.state.load()
                chosen = core.select(lib.files, lib.names,
                                     filters_to_args(body))
                return self._send(200, preview(lib.files, lib.names, chosen))
            if route == "/api/trash":
                return self._send(200, self.trash(body))
        except Exception as e:  # report to the page instead of a dead socket
            return self._send(500, {"error": str(e)})
        self._send(404, {"error": "not found"})

    def scan(self, refresh):
        lib = self.state.load(refresh=bool(refresh))
        return {
            "total": sum(f.size for f in lib.files),
            "unlinked": sum(f.size for f in lib.files if not f.linked),
            "files": len(lib.files),
            "chats": chat_summary(lib.files, lib.names),
            "running": core.whatsapp_running() and not self.state.force,
            "problems": lib.problems,
        }

    def trash(self, body):
        if core.whatsapp_running() and not self.state.force:
            return {"error": "WhatsApp is running. Quit it first (⌘Q)."}
        lib = self.state.load()
        if not lib.safe:
            return {"error": "Not moving anything: " + " ".join(lib.problems)}
        chosen = core.select(lib.files, lib.names, filters_to_args(body))
        if not chosen:
            return {"error": "Nothing matches these filters."}
        if body.get("expect_count") != len(chosen):
            return {"error": "The files changed since the preview. "
                             "Rescan and check again."}
        result = core.stage_and_trash(chosen, lib.names, self.state.container,
                                      log=lambda msg: None)
        result["restore"] = core.restore_command(result["stage"])
        self.state.load(refresh=True)
        return result


def serve(container, port, open_browser=True, force=False):
    Handler.state = State(container, force)
    Handler.token = secrets.token_urlsafe(24)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    port = server.server_address[1]
    Handler.allowed_hosts = ("127.0.0.1:%d" % port, "localhost:%d" % port)
    url = "http://127.0.0.1:%d/" % port
    print("wa-clean is running at %s" % url)
    print("Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
