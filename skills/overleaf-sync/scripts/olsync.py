#!/usr/bin/env python3
"""olsync: two-way sync between a local LaTeX folder and an Overleaf project.

An AI coding agent (Claude Code, Codex, ...) or a person edits the paper in an ordinary
local folder; `olsync sync` merges those edits with whatever co-authors changed on
Overleaf and sends the result back through Overleaf's official git integration.

Requires Python 3.9+ (standard library only) and git.
Project page: https://github.com/YikaiDong-git/ai_overleaf_connector
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import errno
import fnmatch
import hashlib
import html
import json
import os
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

__version__ = "0.1.0"

STATE = ".olsync"
BASE_REF = "refs/olsync/base"

EXIT_OK, EXIT_ERROR, EXIT_GIT, EXIT_CONFLICT, EXIT_MOVED, EXIT_BUILD = 0, 1, 2, 3, 4, 5

DEFAULT_EXCLUDE = [
    "*.aux", "*.bbl", "*.bcf", "*.blg", "*.dvi", "*.fdb_latexmk", "*.fls", "*.glg", "*.glo",
    "*.gls", "*.idx", "*.ilg", "*.ind", "*.ist", "*.lof", "*.log", "*.lot", "*.nav", "*.out",
    "*.run.xml", "*.snm", "*.synctex", "*.synctex.gz", "*.synctex(busy)", "*.toc", "*.vrb",
    "*.xdv", "*.olsync-tmp", ".DS_Store", "Thumbs.db", "*~", "*.swp",
]
GRAPHIC_EXTS = [".pdf", ".png", ".jpg", ".jpeg", ".eps", ".ps", ".mps", ".svg",
                ".tif", ".tiff", ".gif", ".bmp"]
# Only these are merged line by line and compared with LF line endings; every other file is
# copied byte for byte.
TEXT_EXTS = {
    ".tex", ".ltx", ".bib", ".bst", ".bbx", ".cbx", ".lbx", ".cls", ".sty", ".clo", ".cfg",
    ".def", ".fd", ".dtx", ".ins", ".tikz", ".pgf", ".txt", ".md", ".rst", ".csv", ".tsv",
    ".dat", ".json", ".yml", ".yaml", ".xml", ".lua", ".py", ".r", ".m", ".jl", ".sh", ".mk",
}
TEXT_NAMES = {"latexmkrc", ".latexmkrc", "makefile", ".gitignore"}

LABELS = {
    "same": "in sync",
    "push": "push",
    "new-local": "push (new)",
    "deleted-local": "deleted locally",
    "pull": "pull",
    "new-overleaf": "pull (new)",
    "deleted-overleaf": "deleted on Overleaf",
    "merge": "merge",
    "conflict": "CONFLICT",
}

AUTH_HELP = """\
Overleaf did not accept the credentials, or none were available.
  1. In Overleaf open Account Settings > Git integration and generate a token.
  2. Give it to git in one of two ways:
       - set the environment variable OVERLEAF_TOKEN to the token, or
       - store it once in git's credential manager (username "git", password = the token).
Keep the token out of the project URL and out of files in the paper folder."""


class Fail(Exception):
    """An expected failure: printed without a traceback and mapped to an exit code."""

    def __init__(self, message: str, code: int = EXIT_ERROR):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- git plumbing

def git_env() -> dict:
    env = dict(os.environ)
    # Agents run without a terminal: fail fast instead of waiting for a prompt.
    env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", GIT_ASKPASS="",
               SSH_ASKPASS="", LC_ALL="C")
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    return env


def credential_args(remote_url: str) -> list:
    """Offer OVERLEAF_TOKEN to the project's own https host only, without putting the token on
    the command line or on disk."""
    token_host = re.match(r"(https://[^/@\s]+)", remote_url or "")
    if not (os.environ.get("OVERLEAF_TOKEN") and token_host):
        return []
    key = f"credential.{token_host.group(1)}.helper"
    helper = '!f() { test "$1" = get && echo username=git && echo "password=$OVERLEAF_TOKEN"; }; f'
    return ["-c", key + "=", "-c", key + "=" + helper]


def explain_git(args, stderr: str) -> str:
    text = stderr.strip() or "(no output)"
    msg = "git " + " ".join(str(a) for a in args[:2]) + " failed:\n  " + text.replace("\n", "\n  ")
    low = text.lower()
    auth_words = ("authentication failed", "could not read username", "could not read password",
                  "terminal prompts disabled", "invalid username or password")
    if any(w in low for w in auth_words) or re.search(r"\b40[13]\b", low):
        msg += "\n\n" + AUTH_HELP
    return msg


class Repo:
    """The bare git copy of the Overleaf project, kept in <folder>/.olsync/repo.git."""

    def __init__(self, gitdir: Path, remote_url: str = ""):
        self.gitdir = gitdir
        self.remote_url = remote_url

    def run(self, *args, data: bytes | None = None, check: bool = True,
            network: bool = False, env: dict | None = None):
        cmd = ["git"] + (credential_args(self.remote_url) if network else [])
        cmd += ["--git-dir", str(self.gitdir)] + [str(a) for a in args]
        full_env = git_env()
        if env:
            full_env.update(env)
        try:
            p = subprocess.run(cmd, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env=full_env)
        except FileNotFoundError:
            raise Fail("git was not found; install git and make sure it is on PATH.", EXIT_GIT)
        if check and p.returncode != 0:
            raise Fail(explain_git(args, p.stderr.decode("utf-8", "replace")), EXIT_GIT)
        return p

    def out(self, *args, **kw) -> str:
        return self.run(*args, **kw).stdout.decode("utf-8", "replace").strip()

    def resolve(self, rev: str):
        p = self.run("rev-parse", "-q", "--verify", rev + "^{commit}", check=False)
        return p.stdout.decode().strip() if p.returncode == 0 else None

    def listing(self, commit: str) -> dict:
        """Every entry of a commit, recursively: path -> (mode, type, sha)."""
        res = {}
        for ent in self.run("ls-tree", "-r", "-z", "--full-tree", commit).stdout.split(b"\0"):
            if ent:
                meta, path = ent.split(b"\t", 1)
                mode, kind, sha = meta.decode().split(" ")
                res[path.decode("utf-8")] = (mode, kind, sha)
        return res

    def tree(self, commit) -> dict:
        """Regular files of a commit: path -> blob sha."""
        if not commit:
            return {}
        return {p: s for p, (m, k, s) in self.listing(commit).items()
                if k == "blob" and m != "120000" and p != STATE and not p.startswith(STATE + "/")}

    def write_tree(self, entries: dict) -> str:
        """Build (nested) tree objects from a flat path -> (mode, type, sha) map."""
        lines, subdirs = [], {}
        for path, (mode, kind, sha) in entries.items():
            head, sep, rest = path.partition("/")
            if sep:
                subdirs.setdefault(head, {})[rest] = (mode, kind, sha)
            else:
                lines.append(f"{mode} {kind} {sha}\t{path}")
        for name, sub in subdirs.items():
            lines.append(f"040000 tree {self.write_tree(sub)}\t{name}")
        return self.out("mktree", "-z", data="".join(x + "\0" for x in lines).encode("utf-8"))

    def blob(self, sha: str) -> bytes:
        return self.run("cat-file", "blob", sha).stdout

    def commit_time(self, rev: str, path: str | None = None):
        s = self.out("log", "-1", "--format=%ct", rev, *(["--", path] if path else []))
        return int(s) if s else None


# ---------------------------------------------------------------- content helpers

def blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def is_text(path: str, data: bytes) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return ((posixpath.splitext(name)[1] in TEXT_EXTS or name in TEXT_NAMES)
            and b"\0" not in data)


def norm(path: str, data: bytes) -> bytes:
    """Text files compare and travel with LF line endings; everything else stays byte-exact."""
    return data.replace(b"\r\n", b"\n") if is_text(path, data) else data


def short(sha) -> str:
    return sha[:7] if sha else "none"


def fmt_time(ts) -> str:
    if ts is None:
        return "unknown"
    return dt.datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d %H:%M (UTC%z)")


def clean_rel(rel: str):
    """A safe path relative to the paper folder, or None."""
    rel = rel.strip().replace("\\", "/")
    if not rel or rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        return None
    rel = posixpath.normpath(rel)
    if rel in (".", "..") or rel.startswith("../"):
        return None
    return rel


def rmtree(path: Path):
    def retry(func, p, _exc):
        os.chmod(p, 0o700)
        func(p)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


# ---------------------------------------------------------------- one synced folder

class Ctx:
    def __init__(self, folder: Path):
        self.D = folder
        self.S = folder / STATE
        self.cfg = json.loads((self.S / "config.json").read_text(encoding="utf-8"))
        self.repo = Repo(self.S / "repo.git", self.cfg["remote"])
        self.branch = self.cfg["branch"]
        self.stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")  # names this run's trash folder
        self._blobs = {}

    def local_path(self, rel: str) -> Path:
        return self.D.joinpath(*rel.split("/"))

    def read_local(self, rel: str):
        p = self.local_path(rel)
        return p.read_bytes() if p.is_file() else None

    def blob(self, sha: str) -> bytes:
        if sha not in self._blobs:
            self._blobs[sha] = self.repo.blob(sha)
        return self._blobs[sha]

    def same_as(self, path: str, data, sha) -> bool:
        if data is None or sha is None:
            return data is None and sha is None
        if blob_id(data) == sha:
            return True
        return is_text(path, data) and norm(path, data) == norm(path, self.blob(sha))

    def excluded(self, rel: str) -> bool:
        if rel == STATE or rel.startswith(STATE + "/"):
            return True
        name = rel.rsplit("/", 1)[-1]
        return any(fnmatch.fnmatchcase(rel, pat) or fnmatch.fnmatchcase(name, pat)
                   for pat in self.cfg.get("exclude", []))

    def fetch(self) -> str:
        self.repo.run("fetch", "--quiet", "--prune", "origin", network=True)
        return self.remote_head()

    def remote_head(self) -> str:
        head = self.repo.resolve("refs/remotes/origin/" + self.branch)
        if head is None:
            raise Fail(f"Overleaf branch '{self.branch}' was not found in the fetched data.", EXIT_GIT)
        return head

    def base(self):
        return self.repo.resolve(BASE_REF)

    def set_base(self, sha: str):
        self.repo.run("update-ref", BASE_REF, sha)

    def load_state(self) -> dict:
        p = self.S / "state.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}

    def save_state(self, state: dict):
        (self.S / "state.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    def update_state(self, **values):
        state = self.load_state()
        state.update(values)
        self.save_state(state)


class Lock:
    def __init__(self, ctx: Ctx):
        self.path = ctx.S / "lock"

    def __enter__(self):
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise Fail(f"Another olsync run is using this folder ({self.path} exists). "
                       "If none is running, delete that file and retry.")
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return self

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)


def find_folder(arg) -> Path:
    if arg:
        folder = Path(arg).resolve()
        if not (folder / STATE / "config.json").is_file():
            raise Fail(f"{folder} is not connected to Overleaf yet. "
                       f"Run: olsync init <overleaf-project-url> --dir {arg}")
        return folder
    here = Path.cwd().resolve()
    for d in [here, *here.parents]:
        if (d / STATE / "config.json").is_file():
            return d
    hits = sorted({p.parent.parent for pat in ("*/.olsync/config.json", "*/*/.olsync/config.json")
                   for p in here.glob(pat)})
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise Fail("Several connected folders below here; choose one with --dir: "
                   + ", ".join(str(h) for h in hits))
    raise Fail("No folder connected to Overleaf was found here. "
               "Run: olsync init <overleaf-project-url> --dir <paper-folder>")


# ---------------------------------------------------------------- which files belong to the paper

COMMENT = re.compile(r"(?<!\\)%.*")
GRAPHICSPATH = re.compile(r"\\graphicspath\s*\{((?:\s*\{[^{}]*\})+)\s*\}")
REFCMD = re.compile(
    r"\\(input|include|subfile|includegraphics|includesvg|includepdf|lstinputlisting|verbatiminput"
    r"|inputminted|import|subimport|inputfrom|subinputfrom|includefrom|subincludefrom"
    r"|bibliography|addbibresource|addglobalbib|bibliographystyle|documentclass|usepackage"
    r"|RequirePackage)(?![A-Za-z@])\*?\s*(?:\[[^\]]*\]\s*)*((?:\{[^{}]*\}\s*)+)")
BRACED = re.compile(r"\{([^{}]*)\}")


def with_ext(name: str, ext: str) -> str:
    return name if name.lower().endswith(ext) else name + ext


def scan_references(ctx: Ctx, known: set):
    """Files the manuscript uses, found by following \\input, \\includegraphics, \\bibliography ...
    from the main file and from every tracked .tex file.
    Returns (found paths, {missing path: file that references it})."""

    def exists(rel):
        return rel in known or ctx.local_path(rel).is_file()

    found, missing, graphics, gpaths, seen = set(), {}, [], [""], set()
    queue = [ctx.cfg["main"]] + sorted(p for p in known if p.lower().endswith(".tex"))

    def add(rel, src, required=True, follow=False):
        rel = clean_rel(rel)
        if rel is None:
            return False
        if exists(rel):
            found.add(rel)
            if follow:
                queue.append(rel)
            return True
        if required:
            missing.setdefault(rel, src)
        return False

    def tex(arg, src):
        if arg.lower().endswith(".tex"):
            add(arg, src, follow=True)
        elif not add(arg + ".tex", src, required=False, follow=True):
            if not add(arg, src, required=False, follow=True):
                add(arg + ".tex", src)

    while queue:
        rel = queue.pop()
        if rel in seen:
            continue
        seen.add(rel)
        data = ctx.read_local(rel)
        if data is None:
            continue
        found.add(rel)
        text = COMMENT.sub("", data.decode("utf-8", "replace"))
        here = posixpath.dirname(rel)
        for m in GRAPHICSPATH.finditer(text):
            gpaths += [g.strip() for g in BRACED.findall(m.group(1)) if g.strip()]
        for m in REFCMD.finditer(text):
            cmd = m.group(1)
            args = [a.strip() for a in BRACED.findall(m.group(2))]
            if not args or any("#" in a or "\\" in a for a in args[:2]):
                continue
            if cmd in ("input", "include", "subfile"):
                tex(args[0], rel)
            elif cmd in ("import", "inputfrom", "includefrom") and len(args) > 1:
                tex(posixpath.join(args[0], args[1]), rel)
            elif cmd in ("subimport", "subinputfrom", "subincludefrom") and len(args) > 1:
                tex(posixpath.join(here, args[0], args[1]), rel)
            elif cmd in ("includegraphics", "includesvg"):
                graphics.append((args[0], rel, cmd == "includesvg"))
            elif cmd in ("includepdf", "lstinputlisting", "verbatiminput"):
                add(args[0], rel)
            elif cmd == "inputminted" and len(args) > 1:
                add(args[1], rel)
            elif cmd in ("bibliography", "addglobalbib"):
                for name in args[0].split(","):
                    if name.strip():
                        add(with_ext(name.strip(), ".bib"), rel)
            elif cmd == "addbibresource":
                add(args[0], rel)
            elif cmd == "bibliographystyle":
                add(with_ext(args[0], ".bst"), rel, required=False)
            elif cmd == "documentclass":
                add(with_ext(args[0], ".cls"), rel, required=False)
            elif cmd in ("usepackage", "RequirePackage"):
                for name in args[0].split(","):
                    if name.strip():
                        add(with_ext(name.strip(), ".sty"), rel, required=False)

    # \graphicspath can change within a document; every existing candidate is kept, so an
    # ambiguous name sends all its matches.
    for arg, src, svg in graphics:
        has_ext = posixpath.splitext(arg)[1].lower() in GRAPHIC_EXTS
        exts = [""] if has_ext else ([".svg"] if svg else GRAPHIC_EXTS)
        hits = [c for c in (posixpath.join(gp, arg + e) for gp in gpaths for e in exts)
                if add(c, src, required=False)]
        target = clean_rel(arg)
        if not hits and target:
            missing.setdefault(target if has_ext else target + " (image)", src)
    return found, missing


def include_matches(ctx: Ctx) -> set:
    res = set()
    for pat in ctx.cfg.get("include", []):
        for p in ctx.D.glob(pat):
            if p.is_file():
                res.add(p.relative_to(ctx.D).as_posix())
    return res


# ---------------------------------------------------------------- the three-way comparison

class Item:
    def __init__(self, path, local, base, remote):
        self.path, self.local, self.base, self.remote = path, local, base, remote
        self.state, self.detail = "same", ""
        self.merged = None   # merged text for a "merge" or a conflict resolved hunk by hunk
        self.side = None     # "local" or "overleaf" once a conflict is resolved
        self.local_time = self.remote_time = None

    def as_json(self) -> dict:
        d = {"path": self.path, "state": self.state}
        if self.detail:
            d["detail"] = self.detail
        if self.state == "conflict":
            d["local_edit_time"] = fmt_time(self.local_time)
            d["overleaf_edit_time"] = fmt_time(self.remote_time)
        return d


def merge3(ctx: Ctx, path: str, local: bytes, base: bytes, remote: bytes, favor=None):
    """git merge-file: returns (merged bytes, number of conflicting places)."""
    tmp = ctx.S / "tmp"
    tmp.mkdir(exist_ok=True)
    names = []
    for name, data in (("local", local), ("base", base), ("overleaf", remote)):
        (tmp / name).write_bytes(norm(path, data))
        names.append(str(tmp / name))
    args = ["merge-file", "-p", "-L", "local", "-L", "last-sync", "-L", "overleaf"]
    if favor:
        args.append("--ours" if favor == "local" else "--theirs")
    p = ctx.repo.run(*args, *names, check=False)
    if p.returncode < 0 or p.returncode > 127:
        raise Fail(explain_git(args, p.stderr.decode("utf-8", "replace")), EXIT_GIT)
    return p.stdout, p.returncode


def mergeable(ctx: Ctx, it: Item) -> bool:
    return (it.local is not None and it.remote is not None and it.base is not None
            and is_text(it.path, it.local) and is_text(it.path, ctx.blob(it.remote)))


def classify(ctx: Ctx, path, b, r, local, remote_rev) -> Item:
    it = Item(path, local, b, r)
    if ctx.same_as(path, local, r):
        return it
    if b == r:
        it.state = "deleted-local" if local is None else ("new-local" if r is None else "push")
        return it
    if ctx.same_as(path, local, b):
        it.state = "deleted-overleaf" if r is None else ("new-overleaf" if local is None else "pull")
        return it
    it.state = "conflict"
    it.local_time = ctx.local_path(path).stat().st_mtime if local is not None else None
    it.remote_time = ctx.repo.commit_time(remote_rev, path)
    if local is None:
        it.detail = "deleted locally, edited on Overleaf"
    elif r is None:
        it.detail = "edited locally, deleted on Overleaf"
    elif b is None:
        it.detail = "differs between the two sides, with no earlier sync to compare against"
    elif not mergeable(ctx, it):
        it.detail = "binary file changed on both sides"
    else:
        merged, n = merge3(ctx, path, local, ctx.blob(b), ctx.blob(r))
        if n == 0:
            it.state, it.merged = "merge", merged
            it.detail = "edited on both sides in different places"
            return it
        it.detail = f"both sides edited the same lines ({n} place{'s' if n != 1 else ''})"
    it.detail += (f"; local edit {fmt_time(it.local_time)},"
                  f" Overleaf edit {fmt_time(it.remote_time)}")
    return it


def make_plan(ctx: Ctx, base, remote):
    btree, rtree = ctx.repo.tree(base), ctx.repo.tree(remote)
    known = set(btree) | set(rtree)
    found, missing = scan_references(ctx, known)
    # Files already in the project are always tracked; exclude patterns only keep
    # local-only files (build output and the like) from being sent.
    local_only = {p for p in found | include_matches(ctx) if p not in known and not ctx.excluded(p)}
    items = [classify(ctx, p, btree.get(p), rtree.get(p), ctx.read_local(p), remote)
             for p in sorted(known | local_only)]
    missing = [(p, src) for p, src in sorted(missing.items()) if not ctx.excluded(p)]
    return items, missing


def next_step(remote, items) -> str:
    states = {i.state for i in items} - {"same"}
    if "conflict" in states:
        return ("Resolve the conflicts: edit the files so both sides' intent is kept and run sync "
                "again, or choose a side with `olsync sync --prefer local|overleaf|newer`.")
    if states == {"deleted-local"}:
        return ("Only local deletions are pending; `olsync sync --allow-delete` removes those files "
                "on Overleaf too.")
    if states:
        return f"Run `olsync sync --expect {short(remote)}` to apply these changes."
    return "Nothing to sync."


def show_plan(ctx: Ctx, base, remote, items, missing):
    pending = [i for i in items if i.state != "same"]
    print(f"Overleaf  {ctx.cfg['remote']}  [{ctx.branch}]")
    print(f"          latest {short(remote)} at {fmt_time(ctx.repo.commit_time(remote))};"
          f" last sync {short(base)}")
    print(f"Local     {ctx.D}")
    if pending or missing:
        print()
    for it in pending:
        print(f"  {LABELS[it.state]:<20} {it.path}" + (f"  ({it.detail})" if it.detail else ""))
    for path, src in missing:
        print(f"  {'missing':<20} {path}  (referenced in {src}; not found locally or on Overleaf)")
    print()
    print(f"{len(items) - len(pending)} of {len(items)} tracked files identical on both sides.")
    print(next_step(remote, items))


def status_json(ctx: Ctx, base, remote, items, missing) -> dict:
    return {
        "ok": True,
        "folder": str(ctx.D),
        "remote": ctx.cfg["remote"],
        "branch": ctx.branch,
        "overleaf_head": remote,
        "overleaf_head_time": fmt_time(ctx.repo.commit_time(remote)),
        "last_sync": base,
        "tracked": len(items),
        "identical": sum(i.state == "same" for i in items),
        "changes": [i.as_json() for i in items if i.state != "same"],
        "missing_references": [{"path": p, "referenced_in": s} for p, s in missing],
        "next": next_step(remote, items),
    }


# ---------------------------------------------------------------- checks before writing

def case_insensitive(ctx: Ctx) -> bool:
    probe = ctx.S / "Case-Probe.tmp"
    probe.write_bytes(b"")
    try:
        return (ctx.S / "case-probe.tmp").exists()
    finally:
        probe.unlink()


def path_collisions(ctx: Ctx, paths) -> list:
    """Pairs of distinct paths that this file system would store as one file."""
    if not case_insensitive(ctx):
        return []
    seen, pairs = {}, []
    for p in paths:
        key = unicodedata.normalize("NFC", p).casefold()
        if key in seen:
            pairs.append((seen[key], p))
        else:
            seen[key] = p
    return pairs


def is_link(path: Path) -> bool:
    return path.is_symlink() or getattr(os.path, "isjunction", lambda p: False)(path)


def write_obstacle(ctx: Ctx, rel: str):
    """Why writing or moving rel would be unsafe, or None: a symbolic link anywhere on its path,
    or a file where a folder belongs (and the reverse)."""
    cur = ctx.D
    for part in rel.split("/")[:-1]:
        cur = cur / part
        if is_link(cur):
            return "symbolic link"
        if cur.exists() and not cur.is_dir():
            return "file where Overleaf has a folder"
    path = ctx.local_path(rel)
    if is_link(path):
        return "symbolic link"
    if path.is_dir():
        return "folder where Overleaf has a file"
    return None


# ---------------------------------------------------------------- writing files

CHANGED = ("{rel} changed on disk during the sync, so olsync stopped there{kept}. "
           "Run `olsync sync` again.")


def current_bytes(ctx: Ctx, rel: str, expected) -> None:
    """Stop if the file changed on disk since the plan was made (an editor save, for example)."""
    path = ctx.local_path(rel)
    if (path.read_bytes() if path.is_file() else None) != expected:
        raise Fail(CHANGED.format(rel=rel, kept=""))


def place_new(src: str, dst: Path) -> bool:
    """Move src to dst only if dst does not exist; False if something is already there."""
    if os.name == "nt":
        try:
            os.rename(src, str(dst))  # refuses to replace an existing file on Windows
        except FileExistsError:
            return False
        return True
    try:
        os.link(src, str(dst))  # refuses to replace an existing file on POSIX
    except FileExistsError:
        return False
    except OSError as e:
        if e.errno not in (errno.EPERM, errno.EOPNOTSUPP, errno.ENOTSUP, errno.EMLINK, errno.EXDEV):
            raise
        # A file system without hard links: create exclusively, then copy.
        try:
            fd = os.open(str(dst), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
            shutil.copyfileobj(inp, out)
        shutil.copymode(src, str(dst))
    os.unlink(src)
    return True


def prune_empty(folder: Path, stop: Path):
    """Remove empty folders from `folder` up to, but not including, `stop`."""
    while folder != stop and stop in folder.parents and not any(folder.iterdir()):
        folder.rmdir()
        folder = folder.parent


def move_aside(ctx: Ctx, rel: str, origin: str) -> Path:
    dst = trash_path(ctx, ctx.stamp, origin, rel)
    try:
        os.replace(str(ctx.local_path(rel)), str(dst))
    except OSError as e:
        if isinstance(e, PermissionError) or e.errno == errno.EBUSY:
            raise Fail(f"{rel} is busy or locked by another program (an editor that is saving it, "
                       "a sync client, a virus scanner), so olsync left it as it is. Run sync again "
                       "in a moment.")
        raise
    return dst


def write_local(ctx: Ctx, rel: str, data: bytes, old):
    """Replace a local file without ever overwriting an edit made during the sync.

    The current file is moved aside and checked against the planned snapshot; the new content,
    prepared in a fresh temporary file, is then placed only if nothing new appeared at the path.
    Keeps a text file's CRLF style and the file's permissions, and reads the result back."""
    current_bytes(ctx, rel, old)
    if old is not None and norm(rel, old) == norm(rel, data):
        return
    if old is not None and is_text(rel, data) and b"\r\n" in old:
        data = norm(rel, data).replace(b"\n", b"\r\n")
    path = ctx.local_path(rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    if old is not None:
        mode = stat.S_IMODE(path.stat().st_mode)
    else:
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o666 & ~umask
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".",
                               suffix=".olsync-tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, mode)
        aside = None
        if old is not None:
            aside = move_aside(ctx, rel, "replaced")
            if aside.read_bytes() != old:
                back = place_new(str(aside), path)
                kept = "" if back else f"; your version is saved at {aside.relative_to(ctx.D)}"
                raise Fail(CHANGED.format(rel=rel, kept=kept))
        if not place_new(tmp, path):
            kept = f"; the version before it is saved at {aside.relative_to(ctx.D)}" if aside else ""
            raise Fail(CHANGED.format(rel=rel, kept=", leaving the newly saved file in place" + kept))
        if aside is not None:
            if aside.read_bytes() != old:
                raise Fail(CHANGED.format(rel=rel, kept="; the version written meanwhile is saved at "
                                          + str(aside.relative_to(ctx.D))))
            aside.unlink()
            prune_empty(aside.parent, ctx.S / "trash")
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)
    if path.read_bytes() != data:
        raise Fail(f"Writing {rel} did not stick (read-back differs); stopping.")


def trash_path(ctx: Ctx, stamp: str, origin: str, rel: str) -> Path:
    """A fresh path under .olsync/trash/<stamp>/<origin>/; existing copies are never replaced."""
    dst = ctx.S / "trash" / stamp / origin / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    n = 1
    while os.path.lexists(dst):
        dst = dst.with_name(f"{posixpath.basename(rel)}.{n}")
        n += 1
    return dst


def move_to_trash(ctx: Ctx, rel: str, old) -> str:
    current_bytes(ctx, rel, old)
    dst = move_aside(ctx, rel, "local")
    if dst.read_bytes() != old:
        back = place_new(str(dst), ctx.local_path(rel))
        raise Fail(CHANGED.format(rel=rel, kept="" if back else
                                  f"; your version is saved at {dst.relative_to(ctx.D)}"))
    return dst.relative_to(ctx.D).as_posix()


def keep_copy(ctx: Ctx, stamp: str, origin: str, rel: str, data: bytes) -> str:
    dst = trash_path(ctx, stamp, origin, rel)
    with open(dst, "xb") as f:
        f.write(data)
    return dst.relative_to(ctx.D).as_posix()


def identity_env(ctx: Ctx) -> dict:
    env = {}
    if not (os.environ.get("GIT_AUTHOR_NAME") or ctx.repo.out("config", "user.name", check=False)):
        env.update(GIT_AUTHOR_NAME="olsync", GIT_COMMITTER_NAME="olsync")
    if not (os.environ.get("GIT_AUTHOR_EMAIL") or ctx.repo.out("config", "user.email", check=False)):
        env.update(GIT_AUTHOR_EMAIL="olsync@localhost", GIT_COMMITTER_EMAIL="olsync@localhost")
    return env


def make_commit(ctx: Ctx, parent: str, outgoing, message: str) -> str:
    entries = ctx.repo.listing(parent)
    for it in outgoing:
        if it.local is None:
            entries.pop(it.path, None)
        else:
            sha = ctx.repo.out("hash-object", "-w", "--stdin", data=norm(it.path, it.local))
            mode = entries.get(it.path, ("100644",))[0]
            entries[it.path] = (mode if mode in ("100644", "100755") else "100644", "blob", sha)
    tree = ctx.repo.write_tree(entries)
    return ctx.repo.out("commit-tree", tree, "-p", parent, "-m", message, env=identity_env(ctx))


def push(ctx: Ctx, commit: str):
    p = ctx.repo.run("push", "--porcelain", "origin", f"{commit}:refs/heads/{ctx.branch}",
                     network=True, check=False)
    if p.returncode != 0:
        err = (p.stdout + p.stderr).decode("utf-8", "replace")
        if re.search(r"rejected|non-fast-forward|fetch first|stale info", err):
            raise Fail("Overleaf changed during this sync, so nothing was pushed. The Overleaf "
                       "changes pulled in this run are already in the local files; run "
                       "`olsync sync` again.", EXIT_MOVED)
        raise Fail(explain_git(("push",), err), EXIT_GIT)


# ---------------------------------------------------------------- build check

def pdf_pages(pdf: Path):
    tool = shutil.which("pdfinfo")
    if tool:
        p = subprocess.run([tool, str(pdf)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        m = re.search(rb"^Pages:\s+(\d+)", p.stdout, re.M)
        if m:
            return int(m.group(1))
    n = len(re.findall(rb"/Type\s*/Page[^s]", pdf.read_bytes()))
    return n or None


def run_build(ctx: Ctx) -> dict:
    cmd = ctx.cfg.get("build")
    if not cmd:
        raise Fail('No build command is configured. Add one to .olsync/config.json, for example '
                   '"build": "latexmk -pdf -interaction=nonstopmode main.tex".')
    stem = posixpath.splitext(ctx.cfg["main"])[0]
    log_path = ctx.local_path(ctx.cfg.get("log") or stem + ".log")
    pdf_path = log_path.with_suffix(".pdf")
    timeout = ctx.cfg.get("build_timeout", 900)
    t0 = time.time()
    try:
        p = subprocess.run(cmd, shell=True, cwd=str(ctx.D), stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise Fail(f"The build command ran longer than {timeout} s and was stopped.", EXIT_BUILD)
    out = p.stdout.decode("utf-8", "replace")
    log = ""
    if log_path.is_file() and log_path.stat().st_mtime >= t0 - 2:
        log = log_path.read_text(encoding="utf-8", errors="replace")
    # TeX wraps log lines at 79 characters; the joined copy keeps wrapped messages whole.
    text = log + "\n" + log.replace("\n", "") if log else out
    undefined = sorted(set(re.findall(r"(?:Reference|Citation) [`']([^'\s]+)' on page \S+ undefined",
                                      text)))
    glyphs = sorted(set(re.findall(r"Missing character: There is no .+? in font [^\s!]+",
                                   out + "\n" + log)))
    pages = re.findall(r"Output written on [^(]*\((\d+) pages?", text)
    fresh_pdf = pdf_path.is_file() and pdf_path.stat().st_mtime >= t0 - 2
    return {
        "ok": p.returncode == 0,
        "exit_code": p.returncode,
        "pages": int(pages[-1]) if pages else (pdf_pages(pdf_path) if fresh_pdf else None),
        "undefined": len(undefined),
        "undefined_keys": undefined[:10],
        "missing_glyphs": len(glyphs),
        "missing_glyph_lines": glyphs[:5],
        "seconds": round(time.time() - t0, 1),
        "output_tail": out.splitlines()[-25:] if p.returncode else [],
    }


def build_and_record(ctx: Ctx) -> dict:
    res = run_build(ctx)
    state = ctx.load_state()
    res["previous"] = state.get("last_build")
    if res["ok"]:
        state["last_build"] = {k: res[k] for k in ("pages", "undefined", "missing_glyphs")}
        ctx.save_state(state)
    return res


def build_lines(b: dict) -> list:
    if not b["ok"]:
        return [f"Build   FAILED (exit code {b['exit_code']}); last lines of output:"] + \
               ["        " + line for line in b["output_tail"]]
    prev = b.get("previous") or {}

    def part(key, label):
        v, w = b[key], prev.get(key)
        s = f"{'?' if v is None else v} {label}"
        return s + (f" (was {w})" if w is not None and w != v else "")
    lines = ["Build   ok: " + ", ".join([part("pages", "pages"),
                                         part("undefined", "undefined references/citations"),
                                         part("missing_glyphs", "missing characters")])]
    if b["undefined_keys"]:
        lines.append("        undefined: " + ", ".join(b["undefined_keys"]))
    lines += ["        " + g for g in b["missing_glyph_lines"]]
    return lines


# ---------------------------------------------------------------- review page

TOKEN = re.compile(r"\s+|\w+|[^\w\s]")
MAX_LINES = 60


def word_diff(a: str, b: str):
    ta, tb = TOKEN.findall(a), TOKEN.findall(b)
    ra, rb = [], []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ta, tb, autojunk=False).get_opcodes():
        sa, sb = html.escape("".join(ta[i1:i2])), html.escape("".join(tb[j1:j2]))
        if tag == "equal":
            ra.append(sa)
            rb.append(sb)
        else:
            if sa:
                ra.append(f"<del>{sa}</del>")
            if sb:
                rb.append(f"<ins>{sb}</ins>")
    return "".join(ra), "".join(rb)


def hunks(a: str, b: str):
    """Yield (line number in the new text, old html, new html) for each changed region."""
    al, bl = a.splitlines(), b.splitlines()
    for group in difflib.SequenceMatcher(None, al, bl, autojunk=False).get_grouped_opcodes(1):
        old, new, line = [], [], None
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                t = html.escape("\n".join(al[i1:i2]))
                old.append(t)
                new.append(t)
                continue
            line = line or j1 + 1
            if max(i2 - i1, j2 - j1) > MAX_LINES:
                if i2 > i1:
                    old.append("<del>" + html.escape("\n".join(al[i1:i1 + MAX_LINES]))
                               + f"\n... ({i2 - i1} lines)</del>")
                if j2 > j1:
                    new.append("<ins>" + html.escape("\n".join(bl[j1:j1 + MAX_LINES]))
                               + f"\n... ({j2 - j1} lines)</ins>")
                continue
            x, y = word_diff("\n".join(al[i1:i2]), "\n".join(bl[j1:j2]))
            if i2 > i1:
                old.append(x)
            if j2 > j1:
                new.append(y)
        yield line or 1, "\n".join(old), "\n".join(new)


REVIEW_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>olsync review</title>
<style>
body{font-family:system-ui,Segoe UI,Arial,sans-serif;max-width:1200px;margin:24px auto;padding:0 16px;color:#222;line-height:1.45}
h1{font-size:21px;margin-bottom:2px} .meta{color:#666;font-size:13px}
table{border-collapse:collapse;font-size:14px;margin:12px 0} td{border:1px solid #ddd;padding:4px 9px}
.item{border:1px solid #ddd;border-radius:6px;padding:10px 14px;margin:14px 0}
.item h3{margin:0 0 4px;font-size:15px} .loc{font-family:Consolas,monospace;font-size:12px;color:#555}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:8px 0}
.col{background:#fafafa;border:1px solid #eee;padding:6px 9px;min-width:0}
.col b{display:block;font-size:11px;color:#888;text-transform:uppercase;margin-bottom:3px}
pre{white-space:pre-wrap;word-wrap:break-word;margin:0;font-size:13px;font-family:Consolas,Menlo,monospace}
del{background:#ffd7d7} ins{background:#c8f5c8;text-decoration:none}
textarea{width:100%;height:36px;margin-top:6px;font-size:13px} #out{height:150px;font-family:monospace}
button{padding:6px 14px;font-size:14px;cursor:pointer}
</style></head><body>
<h1>Changes to review</h1>
<div class="meta">__META__</div>
<table>__SUMMARY__</table>
__CARDS__
<h2>Decisions</h2>
<button onclick="gen()">Generate</button> <button onclick="cp()">Copy</button>
<p><textarea id="out" readonly></textarea></p>
<script>
function gen(){const L=["olsync review __HEAD__ decisions:"];
document.querySelectorAll('.item').forEach(d=>{const r=d.querySelector('input:checked');
const c=d.querySelector('textarea').value.trim();
L.push(d.dataset.id+" "+d.dataset.label+": "+(r?r.value:"-")+(c?" | "+c:""));});
document.getElementById('out').value=L.join("\\n");}
function cp(){gen();const o=document.getElementById('out');o.select();navigator.clipboard.writeText(o.value);}
gen();
</script></body></html>
"""


def render_review(ctx: Ctx, base, remote, items) -> str:
    cards, n = [], 0
    for it in items:
        if it.state == "same":
            continue
        sides = []
        if it.state in ("pull", "new-overleaf", "deleted-overleaf", "merge", "conflict"):
            sides.append(("Overleaf", ctx.blob(it.remote) if it.remote else b""))
        if it.state in ("push", "new-local", "deleted-local", "merge", "conflict"):
            sides.append(("Local", it.local or b""))
        before = ctx.blob(it.base) if it.base else b""
        for side, after in sides:
            if not (is_text(it.path, before or b"x") and is_text(it.path, after or b"x")):
                parts = [(None, "(binary file)", "(binary file, changed)")]
            else:
                parts = list(hunks(norm(it.path, before).decode("utf-8", "replace"),
                                   norm(it.path, after).decode("utf-8", "replace")))
            for line, old, new in parts:
                n += 1
                loc = f"{it.path}:{line}" if line else it.path
                label = html.escape(f"{side} {loc}", quote=True)
                cards.append(
                    f'<div class="item" data-id="{n}" data-label="{label}">'
                    f"<h3>{n} &middot; {side}: {html.escape(LABELS[it.state])}</h3>"
                    f'<div class="loc">{html.escape(loc)}</div><div class="cols">'
                    f'<div class="col"><b>Last synced version</b><pre>{old}</pre></div>'
                    f'<div class="col"><b>{side} now</b><pre>{new}</pre></div></div>'
                    f'<label><input type="radio" name="r{n}" value="accept" checked> Accept</label> '
                    f'<label><input type="radio" name="r{n}" value="revise"> Revise</label>'
                    f'<textarea placeholder="comment"></textarea></div>')
    counts = {}
    for it in items:
        counts[it.state] = counts.get(it.state, 0) + 1
    summary = "".join(f"<tr><td>{html.escape(LABELS[s])}</td><td>{c}</td></tr>"
                      for s, c in sorted(counts.items()))
    meta = (f"Overleaf {html.escape(ctx.cfg['remote'])} latest {short(remote)} "
            f"({html.escape(fmt_time(ctx.repo.commit_time(remote)))}); last sync {short(base)}; "
            f"folder {html.escape(str(ctx.D))}; generated "
            f"{html.escape(fmt_time(time.time()))}")
    page = REVIEW_PAGE.replace("__META__", meta).replace("__SUMMARY__", summary)
    page = page.replace("__HEAD__", short(remote))
    return page.replace("__CARDS__", "\n".join(cards) or "<p>No differences.</p>")


# ---------------------------------------------------------------- commands

def normalize_remote(s: str) -> str:
    s = s.strip()
    m = re.match(r"https?://(?:www\.)?overleaf\.com/(?:project|read)/([0-9a-f]{10,})", s)
    if m:
        return "https://git.overleaf.com/" + m.group(1)
    if re.fullmatch(r"[0-9a-f]{24}", s):
        return "https://git.overleaf.com/" + s
    if re.match(r"https?://[^/@]+:[^/@]*@", s):
        raise Fail("The URL contains a password or token. Use the plain project URL and give the "
                   "token through OVERLEAF_TOKEN or git's credential manager.")
    return s


def detect_branch(repo: Repo) -> str:
    heads = [h for h in repo.out("for-each-ref", "--format=%(refname:strip=3)",
                                 "refs/remotes/origin").split() if h != "HEAD"]
    if len(heads) == 1:
        return heads[0]
    for name in ("main", "master"):
        if name in heads:
            return name
    if not heads:
        raise Fail("The Overleaf project returned no commits.", EXIT_GIT)
    raise Fail("The remote has several branches (" + ", ".join(heads) + "); Overleaf projects "
               "have one. Check the URL.", EXIT_GIT)


def detect_main(repo: Repo, remote: str, folder: Path) -> str:
    rtree = repo.tree(remote)
    names = {n for n in rtree if "/" not in n} | {p.name for p in folder.glob("*.tex")}
    cands = []
    for name in sorted(n for n in names if n.lower().endswith(".tex")):
        data = (folder / name).read_bytes() if (folder / name).is_file() else repo.blob(rtree[name])
        if re.search(rb"^[^%\n]*\\documentclass", data, re.M):
            cands.append(name)
    if len(cands) == 1:
        return cands[0]
    if "main.tex" in cands:
        return "main.tex"
    if not cands:
        raise Fail("No top-level .tex file with \\documentclass was found; pass --main FILE.")
    raise Fail("Several possible main files (" + ", ".join(cands) + "); pass --main FILE.")


def cmd_init(args) -> int:
    folder = Path(args.dir).resolve()
    S = folder / STATE
    if S.exists():
        raise Fail(f"{S} already exists. To connect this folder again, move that folder away "
                   "first (it can hold copies of replaced files in its trash/ folder).")
    url = normalize_remote(args.remote)
    folder.mkdir(parents=True, exist_ok=True)
    S.mkdir()
    try:
        (S / ".gitignore").write_text("*\n", encoding="utf-8")
        p = subprocess.run(["git", "init", "--bare", "--quiet", str(S / "repo.git")],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=git_env())
        if p.returncode:
            raise Fail(explain_git(("init",), p.stderr.decode("utf-8", "replace")), EXIT_GIT)
        repo = Repo(S / "repo.git", url)
        repo.run("remote", "add", "origin", url)
        repo.run("fetch", "--quiet", "origin", network=True)
        branch = detect_branch(repo)
        remote = repo.resolve("refs/remotes/origin/" + branch)
        main = clean_rel(args.main) if args.main else detect_main(repo, remote, folder)
        if not main:
            raise Fail(f"--main must be a path inside {folder}.")
        stem = posixpath.splitext(main)[0]
        cfg = {
            "version": 1,
            "remote": url,
            "branch": branch,
            "main": main,
            "build": args.build,
            "build_timeout": 900,
            "log": None,
            "include": [],
            "exclude": DEFAULT_EXCLUDE + [stem + ".pdf"],
        }
        (S / "config.json").write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        ctx = Ctx(folder)
        items, missing = make_plan(ctx, None, remote)
    except BaseException:
        rmtree(S)  # created by this run, so nothing of the user's is inside
        raise
    print(f"Connected {folder} to {url} (branch {branch}, main file {main}).\n")
    show_plan(ctx, None, remote, items, missing)
    return EXIT_OK


def cmd_status(args) -> int:
    ctx = Ctx(find_folder(args.dir))
    remote = ctx.remote_head() if args.no_fetch else ctx.fetch()
    base = ctx.base()
    items, missing = make_plan(ctx, base, remote)
    if args.json:
        print(json.dumps(status_json(ctx, base, remote, items, missing), indent=2))
    else:
        show_plan(ctx, base, remote, items, missing)
    return EXIT_OK


def cmd_sync(args) -> int:
    ctx = Ctx(find_folder(args.dir))
    with Lock(ctx):
        return run_sync(ctx, args)


def apply_local_change(ctx: Ctx, it: Item, result: dict, chosen_deletes: set):
    if it.state in ("pull", "new-overleaf"):
        write_local(ctx, it.path, ctx.blob(it.remote), it.local)
        result["pulled"].append(it.path)
    elif it.state == "deleted-overleaf":
        result["moved_to_trash"].append(move_to_trash(ctx, it.path, it.local))
    elif it.state == "merge":
        write_local(ctx, it.path, it.merged, it.local)
        result["merged"].append(it.path)
    elif it.state == "conflict":
        if it.merged is not None:
            write_local(ctx, it.path, it.merged, it.local)
            how = "overlapping lines"
        elif it.side == "overleaf":
            if it.remote is None:
                result["moved_to_trash"].append(move_to_trash(ctx, it.path, it.local))
            else:
                if it.local is not None:
                    result["kept_copies"].append(keep_copy(ctx, ctx.stamp, "local", it.path, it.local))
                write_local(ctx, it.path, ctx.blob(it.remote), it.local)
            how = "whole file"
        else:
            if it.remote is not None:
                result["kept_copies"].append(
                    keep_copy(ctx, ctx.stamp, "overleaf", it.path, ctx.blob(it.remote)))
            if it.local is None:
                chosen_deletes.add(it.path)
            how = "whole file"
        result["resolved"].append(f"{it.path} (kept {it.side}, {how})")


def run_sync(ctx: Ctx, args) -> int:
    remote = ctx.fetch()
    if args.expect and ctx.repo.resolve(args.expect) != remote:
        raise Fail(f"Overleaf is now at {short(remote)}, not {args.expect}: it changed after "
                   "you checked. Nothing was changed; run `olsync status` again.", EXIT_MOVED)
    base = ctx.base()
    items, missing = make_plan(ctx, base, remote)

    clashes = path_collisions(ctx, [i.path for i in items])
    if clashes:
        raise Fail("These paths differ only in letter case or Unicode form, which this file system "
                   "stores as one file: " + "; ".join(f"{a} / {b}" for a, b in clashes)
                   + ". Rename one of each pair on Overleaf, then sync again. Nothing was changed.")

    conflicts = [i for i in items if i.state == "conflict"]
    if conflicts and not args.prefer:
        msg = (f"{len(conflicts)} file(s) changed on both sides in the same place; nothing was "
               "changed. Edit them by hand and sync again, or choose a side with "
               "--prefer local|overleaf|newer.")
        if args.json:
            d = status_json(ctx, base, remote, items, missing)
            d.update(ok=False, exit_code=EXIT_CONFLICT, error=msg)
            print(json.dumps(d, indent=2))
        else:
            show_plan(ctx, base, remote, items, missing)
        print(f"olsync: {msg}", file=sys.stderr)
        return EXIT_CONFLICT
    if args.prefer == "newer":
        deletions = [i.path for i in conflicts if i.local is None or i.remote is None]
        if deletions:
            raise Fail("An edit against a deletion needs an explicit choice (" + ", ".join(deletions)
                       + "); use --prefer local or --prefer overleaf. Nothing was changed.",
                       EXIT_CONFLICT)
    for it in conflicts:
        side = args.prefer
        if side == "newer":
            side = "local" if it.local_time > it.remote_time else "overleaf"
        it.side = side
        if mergeable(ctx, it):
            it.merged, _ = merge3(ctx, it.path, it.local, ctx.blob(it.base), ctx.blob(it.remote),
                                  favor=side)

    writes = [i.path for i in items if i.state in ("pull", "new-overleaf", "merge", "deleted-overleaf")
              or (i.state == "conflict" and (i.merged is not None or i.side == "overleaf"))]
    obstacles = [(p, why) for p in writes for why in [write_obstacle(ctx, p)] if why]
    if obstacles:
        raise Fail("olsync writes only regular files inside the folder, and these targets are in the "
                   "way: " + "; ".join(f"{p} ({why})" for p, why in obstacles)
                   + ". Update or move them by hand (`olsync review` shows the Overleaf versions), "
                   "then sync again. Nothing was changed.")

    if args.dry_run:
        if args.json:
            print(json.dumps(status_json(ctx, base, remote, items, missing), indent=2))
        else:
            show_plan(ctx, base, remote, items, missing)
            for it in conflicts:
                how = "line by line" if it.merged is not None else "whole file"
                print(f"  would keep the {it.side} side of {it.path} ({how})")
            print("Dry run: nothing was changed.")
        return EXIT_OK

    # Deletions chosen with --prefer local survive a failed build or a rejected push.
    chosen_deletes = set(ctx.load_state().get("chosen_deletes", []))
    result = {"ok": True, "overleaf_before": remote, "overleaf_after": remote,
              "pulled": [], "merged": [], "resolved": [], "moved_to_trash": [], "kept_copies": [],
              "pushed": [], "deleted_on_overleaf": [], "not_pushed": [], "build": None}
    for it in items:
        try:
            apply_local_change(ctx, it, result, chosen_deletes)
        except OSError as e:
            # The base has not moved yet, so the next run re-plans from a consistent state.
            raise Fail(f"Could not update {it.path} locally ({e}). Files listed before it were "
                       "updated, nothing after it; run `olsync sync` again.")
    # Every Overleaf change is now in the local files, so Overleaf's head becomes the base.
    ctx.set_base(remote)
    ctx.update_state(chosen_deletes=sorted(chosen_deletes))

    outgoing_states = ("push", "new-local", "deleted-local")
    pending, _ = make_plan(ctx, remote, remote)
    if args.pull_only:
        result["not_pushed"] = [i.path for i in pending if i.state in outgoing_states]
    else:
        changed_here = any(result[k] for k in ("pulled", "merged", "resolved", "moved_to_trash"))
        will_push = any(i.state in outgoing_states for i in pending)
        if ctx.cfg.get("build") and not args.no_build and (changed_here or will_push):
            result["build"] = build_and_record(ctx)
            if not result["build"]["ok"]:
                result["not_pushed"] = [i.path for i in pending if i.state in outgoing_states]
                msg = ("The local build failed, so nothing was pushed (Overleaf changes were "
                       "pulled). Fix the build, or push anyway with --no-build.")
                result.update(ok=False, exit_code=EXIT_BUILD, error=msg)
                report_sync(ctx, result, args.json)
                print(f"olsync: {msg}", file=sys.stderr)
                return EXIT_BUILD
        final_plan, _ = make_plan(ctx, remote, remote)
        outgoing = [i for i in final_plan if i.state in ("push", "new-local")
                    or (i.state == "deleted-local"
                        and (args.allow_delete or i.path in chosen_deletes))]
        result["not_pushed"] = [i.path for i in final_plan
                                if i.state == "deleted-local" and i not in outgoing]
        if outgoing:
            names = [i.path for i in outgoing]
            message = args.message or ("Update from local folder: " + ", ".join(names[:5])
                                       + (f" and {len(names) - 5} more" if len(names) > 5 else ""))
            commit = make_commit(ctx, remote, outgoing, message)
            push(ctx, commit)
            ctx.set_base(commit)
            result["overleaf_after"] = commit
            result["pushed"] = [i.path for i in outgoing if i.local is not None]
            result["deleted_on_overleaf"] = [i.path for i in outgoing if i.local is None]
        ctx.update_state(chosen_deletes=[])

    # Fetch again and re-derive the state from scratch.
    final = result["overleaf_after"]
    refetch = ctx.repo.run("fetch", "--quiet", "--prune", "origin", network=True, check=False)
    if refetch.returncode != 0:
        result["refetch_error"] = refetch.stderr.decode("utf-8", "replace").strip()
    elif ctx.remote_head() != final:
        result["overleaf_changed_since"] = ctx.remote_head()
    check, _ = make_plan(ctx, final, final)
    unexpected = [i.path for i in check if i.state != "same" and i.path not in result["not_pushed"]]
    result["check"] = {"tracked": len(check), "identical": sum(i.state == "same" for i in check),
                       "unexpected_differences": unexpected}
    if unexpected:
        msg = ("After syncing, these files still differ from Overleaf: " + ", ".join(unexpected)
               + ". Run `olsync status` to inspect.")
        result.update(ok=False, exit_code=EXIT_ERROR, error=msg)
    report_sync(ctx, result, args.json)
    if unexpected:
        print(f"olsync: {result['error']}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


def report_sync(ctx: Ctx, r: dict, as_json: bool):
    if as_json:
        print(json.dumps(r, indent=2))
        return
    before, after = r["overleaf_before"], r["overleaf_after"]
    moved = f"{short(before)} -> {short(after)}" if after != before else short(after)
    print(f"Overleaf  {ctx.cfg['remote']}  {moved}")
    rows = [("pulled", r["pulled"]), ("merged", r["merged"]), ("conflict", r["resolved"]),
            ("to trash", r["moved_to_trash"]), ("saved copy", r["kept_copies"]),
            ("pushed", r["pushed"]), ("deleted", r["deleted_on_overleaf"]),
            ("not pushed", r["not_pushed"])]
    for label, paths in rows:
        for p in paths:
            print(f"  {label:<11} {p}")
    if not any(paths for _, paths in rows):
        print("  already in sync")
    if r["build"]:
        for line in build_lines(r["build"]):
            print(line)
    if "check" in r:
        c = r["check"]
        print(f"Check     {c['identical']} of {c['tracked']} tracked files identical to Overleaf "
              f"{short(after)}")
    if r.get("overleaf_changed_since"):
        print(f"Note      Overleaf has moved on to {short(r['overleaf_changed_since'])} since "
              "(someone edited it); run sync again to bring that in.")
    if r.get("refetch_error"):
        print("Note      The sync finished, but the final re-fetch from Overleaf failed: "
              + r["refetch_error"])


def cmd_review(args) -> int:
    ctx = Ctx(find_folder(args.dir))
    remote = ctx.remote_head() if args.no_fetch else ctx.fetch()
    base = ctx.base()
    items, _ = make_plan(ctx, base, remote)
    out = Path(args.output).resolve() if args.output else ctx.S / "review.html"
    out.write_text(render_review(ctx, base, remote, items), encoding="utf-8")
    pending = sum(i.state != "same" for i in items)
    print(f"{pending} changed file(s). Review page: {out}")
    return EXIT_OK


def cmd_build(args) -> int:
    ctx = Ctx(find_folder(args.dir))
    b = build_and_record(ctx)
    if args.json:
        print(json.dumps(b, indent=2))
    else:
        for line in build_lines(b):
            print(line)
    return EXIT_OK if b["ok"] else EXIT_BUILD


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    ap = argparse.ArgumentParser(
        prog="olsync",
        description="Two-way sync between a local LaTeX folder and an Overleaf project, "
                    "through Overleaf's git integration.")
    ap.add_argument("--version", action="version", version=f"olsync {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="connect a local folder to an Overleaf project")
    p.add_argument("remote", help="Overleaf project URL, git URL, or project id")
    p.add_argument("--dir", default=".", help="the paper folder (default: current folder)")
    p.add_argument("--main", help="main .tex file, relative to the folder (default: detected)")
    p.add_argument("--build", help='local build command, e.g. "latexmk -pdf main.tex"')
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("status", help="show what differs between the folder and Overleaf")
    p.add_argument("--dir", help="the paper folder (default: found from the current folder)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--no-fetch", action="store_true", help="use the last fetched Overleaf state")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("sync", help="pull Overleaf changes, merge, and push local changes")
    p.add_argument("--dir", help="the paper folder (default: found from the current folder)")
    p.add_argument("--expect", metavar="SHA",
                   help="stop unless Overleaf is still at this commit (from `status`)")
    p.add_argument("--prefer", choices=["local", "overleaf", "newer"],
                   help="where both sides edited the same lines, keep this side")
    p.add_argument("--allow-delete", action="store_true",
                   help="also delete on Overleaf the files deleted locally")
    p.add_argument("--pull-only", action="store_true", help="bring Overleaf changes in, push nothing")
    p.add_argument("--no-build", action="store_true", help="skip the configured build check")
    p.add_argument("-m", "--message", help="description stored with the pushed commit")
    p.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("review", help="write an HTML page showing every pending change")
    p.add_argument("--dir", help="the paper folder (default: found from the current folder)")
    p.add_argument("-o", "--output", help="output file (default: <folder>/.olsync/review.html)")
    p.add_argument("--no-fetch", action="store_true", help="use the last fetched Overleaf state")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("build", help="run the configured build command and report on it")
    p.add_argument("--dir", help="the paper folder (default: found from the current folder)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_build)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except Fail as e:
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "exit_code": e.code, "error": str(e)}, indent=2))
        print(f"olsync: {e}", file=sys.stderr)
        return e.code


if __name__ == "__main__":
    sys.exit(main())
