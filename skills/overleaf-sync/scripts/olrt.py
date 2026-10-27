"""Tracked edits on overleaf.com through its real-time editor protocol.

The git integration can only replace file contents, so Overleaf records a git push as plain edits. The editor
itself talks socket.io 0.9 to Overleaf; an edit sent there with a track-changes id lands as a tracked change,
exactly as typing with Review > Track changes on. This module speaks that protocol over HTTP long-polling
(standard library only), signed in with the browser session cookie `overleaf_session2`.

Positions and lengths follow JavaScript strings (UTF-16 code units), as Overleaf counts them.
"""
from __future__ import annotations

import difflib
import hashlib
import http.cookiejar
import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

COOKIE_HELP = """\
Tracked edits need your Overleaf browser session (git tokens do not reach the editor).
  1. Sign in to overleaf.com in a browser, open the developer tools > Application/Storage > Cookies.
  2. Copy the value of the cookie `overleaf_session2`.
  3. Either set the environment variable OVERLEAF_SESSION to it, or save it alone in ~/.overleaf_session
     (readable only by you). It expires after some days; repeat when olsync says so.
Or push plain (untracked) edits with `olsync sync --untracked`."""

# a LaTeX command is one token, so an edit never splits "\\end" into "\\" + "end"
TOKEN = re.compile(r"\\[A-Za-z]+|\s+|\w+|[^\w\s]")


class RTError(Exception):
    """A failure talking to the Overleaf editor connection."""


def beyond_bmp(text: str) -> list:
    """Characters above U+FFFF (emoji and the like). Overleaf stores none of them: its document updater turns
    every UTF-16 surrogate into U+FFFD, so an edit carrying one fails the hash check."""
    return sorted({ch for ch in text if ord(ch) > 0xFFFF})


def u16(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def doc_hash(text: str) -> str:
    # document-updater: sha1("blob " + JS length + "\0" + UTF-8 content)
    return hashlib.sha1(b"blob %d\0" % u16(text) + text.encode("utf-8")).hexdigest()


def word_ops(src: str, dst: str) -> list:
    """ShareJS text ops turning src into dst, one delete/insert pair per changed run of words.

    Runs separated by a single whitespace token merge, so "a b" -> "x y" reads as one replacement.
    Components go from the end of the text backwards, so each position refers to the original text."""
    s, d = TOKEN.findall(src), TOKEN.findall(dst)
    so, do = [0], [0]
    for t in s:
        so.append(so[-1] + u16(t))
    for t in d:
        do.append(do[-1] + u16(t))
    runs = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, s, d, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if runs and i1 - runs[-1][1] == 1 and s[i1 - 1].isspace():
            runs[-1][1], runs[-1][3] = i2, j2
        else:
            runs.append([i1, i2, j1, j2])
    comps = []
    for i1, i2, j1, j2 in reversed(runs):
        if i2 > i1:
            comps.append({"p": so[i1], "d": "".join(s[i1:i2])})
        if j2 > j1:
            comps.append({"p": so[i1], "i": "".join(d[j1:j2])})
    return comps


def apply_ops(text: str, comps: list) -> str:
    """Apply ShareJS text ops (UTF-16 positions); a delete must match the text it removes."""
    b = text.encode("utf-16-le")
    for c in comps:
        p = 2 * c["p"]
        if "d" in c:
            d = c["d"].encode("utf-16-le")
            if b[p:p + len(d)] != d:
                raise RTError(f"delete does not match the text at {c['p']}")
            b = b[:p] + b[p + len(d):]
        else:
            b = b[:p] + c["i"].encode("utf-16-le") + b[p:]
    return b.decode("utf-16-le")


def frames(body: str) -> list:
    """Split a socket.io 0.9 polling payload: one message, or several framed as \\ufffd<len>\\ufffd<msg>."""
    if not body.startswith("�"):
        return [body] if body else []
    out, b = [], body.encode("utf-16-le")
    sep = "�".encode("utf-16-le")
    while b:
        if not b.startswith(sep):
            raise RTError("malformed socket.io frame")
        end = b.index(sep, 2)
        n = int(b[2:end].decode("utf-16-le"))
        out.append(b[end + 2:end + 2 + 2 * n].decode("utf-16-le"))
        b = b[end + 2 + 2 * n:]
    return out


class Project:
    """One editor connection to an overleaf.com project."""

    def __init__(self, project_id: str, cookie: str, host: str = "www.overleaf.com", timeout: int = 60):
        self.pid, self.host, self.timeout, self.n, self.queue = project_id, host, timeout, 0, []

        def opener():
            jar = http.cookiejar.CookieJar()
            jar.set_cookie(http.cookiejar.Cookie(
                0, "overleaf_session2", cookie, None, False, "." + host.split(".", 1)[1], True, True, "/",
                True, True, None, False, None, None, {}))
            return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

        # Separate cookie jars: a web page answer sets its own load-balancer cookie (GCLB), which would
        # send the editor connection's next poll to a server that never saw its handshake.
        self.http, self.web = opener(), opener()
        q = urllib.parse.urlencode({"projectId": project_id})
        sid = self._req(f"https://{host}/socket.io/1/?{q}&t={int(time.time() * 1000)}").split(":")[0]
        self.url = f"https://{host}/socket.io/1/xhr-polling/{sid}?{q}"
        info = self.wait_event("joinProjectResponse")["args"][0]
        self.project, self.permissions = info["project"], info.get("permissionsLevel")

    def _req(self, url: str, data: bytes | None = None, headers: dict | None = None, web: bool = False) -> str:
        req = urllib.request.Request(url, data=data, headers=headers or {})
        try:
            with (self.web if web else self.http).open(req, timeout=self.timeout) as r:
                if "/login" in r.geturl():
                    raise RTError("Overleaf asked to sign in again: the session cookie expired.\n" + COOKIE_HELP)
                return r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise RTError(f"Overleaf refused the session ({e.code}) for {url.split('?')[0]}.\n"
                              + COOKIE_HELP) from e
            raise RTError(f"Overleaf answered {e.code} for {url.split('?')[0]}") from e

    def _send(self, msg: str):
        self._req(f"{self.url}&t={int(time.time() * 1000)}", msg.encode("utf-8"),
                  {"Content-Type": "text/plain;charset=UTF-8"})

    def recv(self) -> str:
        while True:
            while not self.queue:
                self.queue = frames(self._req(f"{self.url}&t={int(time.time() * 1000)}"))
            m = self.queue.pop(0)
            if m.startswith("2::"):
                self._send("2::")
            elif m.startswith(("7:", "0:")):
                raise RTError(f"Overleaf closed the editor connection ({m[:100]}); it does so when it "
                              "rejects an edit, e.g. one whose result fails its hash check")
            elif m.startswith("5:") and '"otUpdateError"' in m:
                raise RTError(f"Overleaf rejected an edit: {m[:500]}")
            elif not m.startswith(("1::", "8::")):
                return m

    def wait_event(self, name: str) -> dict:
        while True:
            m = self.recv()
            if m.startswith("5:"):
                ev = json.loads(m.split(":", 3)[3])
                if ev["name"] == name:
                    return ev

    def call(self, name: str, *args):
        self.n += 1
        self._send(f"5:{self.n}+::" + json.dumps({"name": name, "args": list(args)}))
        while True:
            m = self.recv()
            if m.startswith("6:::"):
                # "6:::<n>+<json args>", or a bare "6:::<n>" when the reply has no arguments
                ack, _, payload = m[4:].partition("+")
                if ack == str(self.n):
                    return json.loads(payload) if payload else []

    def doc_ids(self) -> dict:
        def walk(folder, prefix=""):
            out = {prefix + d["name"]: d["_id"] for d in folder.get("docs", [])}
            for f in folder.get("folders", []):
                out.update(walk(f, prefix + f["name"] + "/"))
            return out
        return walk(self.project["rootFolder"][0])

    def read(self, doc_id: str):
        """(text, version, ranges) of a doc. Lines and the texts in ranges arrive UTF-8 escaped as Latin-1."""
        err, lines, version, _ops, ranges = self.call("joinDoc", doc_id, {"encodeRanges": True})[:5]
        if err:
            raise RTError(f"could not open doc {doc_id}: {err}")
        self.call("leaveDoc", doc_id)
        dec = lambda s: s.encode("latin-1").decode("utf-8")  # noqa: E731
        ranges = ranges or {}
        for c in ranges.get("changes", []) + ranges.get("comments", []):
            for k in ("i", "d", "c"):
                if k in c["op"]:
                    c["op"][k] = dec(c["op"][k])
        return "\n".join(dec(x) for x in lines), version, ranges

    def edit(self, doc_id: str, old: str, new: str, tracked: bool = True) -> int:
        """Turn the doc's text from `old` into `new` with word-level ops; returns the number of op components.

        Stops if the doc no longer holds `old` (a co-author typed meanwhile). Overleaf checks the hash of the
        resulting text, so the edit applies exactly or not at all."""
        err, lines, version = self.call("joinDoc", doc_id, {"encodeRanges": True})[:3]
        if err:
            raise RTError(f"could not open doc {doc_id}: {err}")
        if "\n".join(x.encode("latin-1").decode("utf-8") for x in lines) != old:
            self.call("leaveDoc", doc_id)
            raise RTError(f"doc {doc_id} changed on Overleaf while syncing; nothing was sent for it")
        comps = word_ops(old, new)
        if comps:
            update = {"doc": doc_id, "op": comps, "v": version, "hash": doc_hash(new)}
            if tracked:
                update["meta"] = {"tc": secrets.token_hex(9)}  # id seed, as the editor generates
            res = self.call("applyOtUpdate", doc_id, update)
            if res and res[0]:
                raise RTError(f"Overleaf rejected the edit to doc {doc_id}: {res[0]}")
        self.call("leaveDoc", doc_id)
        return len(comps)

    def _post(self, path: str, body: dict):
        page = self._req(f"https://{self.host}/project/{self.pid}", web=True)
        tok = re.search(r'name="ol-csrfToken" content="([^"]+)"', page)
        if not tok:
            raise RTError("could not read Overleaf's CSRF token from the project page")
        self._req(f"https://{self.host}/project/{self.pid}/{path}", json.dumps(body).encode(),
                  {"Content-Type": "application/json", "X-Csrf-Token": tok.group(1)}, web=True)

    def track_changes_on(self):
        """Track changes on for the owner and every member (overleaf.com refuses the project-wide form)."""
        users = [self.project["owner"]["_id"]] + [m["_id"] for m in self.project.get("members", [])]
        self._post("track_changes", {"on_for": {u: True for u in users}})

    def close(self):
        self._send("0::")
