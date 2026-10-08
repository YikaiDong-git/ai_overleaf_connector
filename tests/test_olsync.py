"""End-to-end tests. A local bare git repository stands in for the Overleaf project and a
second clone plays the co-author editing on Overleaf; olsync is run as a real subprocess."""

import errno
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
OLSYNC = ROOT / "skills" / "overleaf-sync" / "scripts" / "olsync.py"
_spec = importlib.util.spec_from_file_location("olsync", OLSYNC)
olsync_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(olsync_module)

MAIN = rb"""\documentclass{article}
\usepackage{graphicx}
\graphicspath{{figures/}}
\begin{document}
\input{sec/intro}
\input{sec/results}
\includegraphics[width=\linewidth]{fig1}
\bibliography{refs}
\end{document}
"""
RESULTS = b"".join(b"Result line %d.\n" % i for i in range(1, 11))
FILES = {
    "main.tex": MAIN,
    "sec/intro.tex": b"Intro line 1.\nIntro line 2.\nIntro line 3.\n",
    "sec/results.tex": RESULTS,
    "refs.bib": b"@article{a,title={A}}\n",
    "figures/fig1.png": b"\x89PNG\r\n\x1a\n" + bytes(range(256)),
}

FAKE_BUILD = r'''import sys
from pathlib import Path
Path("main.log").write_text(
    "LaTeX Warning: Reference `fig:x' on page 1 undefined on input line 3.\n"
    "Output written on main.pdf (3 pages, 999 bytes).\n")
Path("main.pdf").write_bytes(b"%PDF-1.4\n")
sys.exit(1 if (Path(__file__).parent / "fail.flag").exists() else 0)
'''

LATER_COMMIT_HOOK = """#!/bin/sh
if [ -f ../later.flag ]; then
  rm ../later.flag
  c=$(GIT_AUTHOR_NAME=x GIT_AUTHOR_EMAIL=x@x GIT_COMMITTER_NAME=x GIT_COMMITTER_EMAIL=x@x \\
      git commit-tree "main^{tree}" -p main -m "later edit")
  git update-ref refs/heads/main "$c"
fi
"""


def replace_line(data: bytes, n: int, text: str) -> bytes:
    lines = data.split(b"\n")
    lines[n - 1] = text.encode()
    return b"\n".join(lines)


def rmtree(path):
    def retry(func, p, _exc):
        os.chmod(p, 0o700)
        func(p)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


def hermetic_env(tmp: Path) -> dict:
    gitconfig = tmp / "gitconfig"
    gitconfig.write_text("")
    env = dict(os.environ, GIT_CONFIG_GLOBAL=str(gitconfig), GIT_CONFIG_NOSYSTEM="1")
    for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
                "GIT_COMMITTER_EMAIL", "OVERLEAF_TOKEN", "GIT_DIR", "GIT_WORK_TREE"):
        env.pop(key, None)
    return env


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="olsync-test-"))
        self.env = hermetic_env(self.tmp)
        self.remote = self.tmp / "overleaf.git"
        self.git("init", "--bare", "-q", str(self.remote))
        self.work = self.tmp / "coauthor"
        self.git("init", "-q", "-b", "main", str(self.work))
        self.write(self.work, FILES)
        self.coauthor_commit()
        self.paper = self.tmp / "paper"

    def tearDown(self):
        rmtree(self.tmp)

    # -- helpers ---------------------------------------------------------------
    def git(self, *args, check=True):
        p = subprocess.run(["git", "-c", "user.name=Co Author", "-c", "user.email=co@example.org",
                            *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env)
        if check and p.returncode:
            raise AssertionError(p.stderr.decode())
        return p

    @staticmethod
    def write(root: Path, files: dict):
        for rel, data in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def coauthor_commit(self):
        self.git("-C", str(self.work), "add", "-A")
        self.git("-C", str(self.work), "commit", "-q", "-m", "edit on Overleaf")
        self.git("-C", str(self.work), "push", "-q", str(self.remote), "main:main")

    def coauthor_edit(self, files: dict, delete=()):
        self.git("-C", str(self.work), "pull", "-q", "--ff-only", str(self.remote), "main")
        self.write(self.work, files)
        for rel in delete:
            (self.work / rel).unlink()
        self.coauthor_commit()

    def remote_file(self, rel):
        p = self.git("--git-dir", str(self.remote), "cat-file", "blob", "main:" + rel, check=False)
        return p.stdout if p.returncode == 0 else None

    def remote_head(self):
        return self.git("--git-dir", str(self.remote), "rev-parse", "main").stdout.decode().strip()

    def olsync(self, *args, code=0, cwd=None):
        p = subprocess.run([sys.executable, str(OLSYNC), *args], cwd=str(cwd or self.tmp),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env)
        out, err = p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
        if p.returncode != code:
            self.fail(f"olsync {' '.join(args)} exited {p.returncode}, expected {code}\n{out}\n{err}")
        return out, err

    def connect(self, *extra):
        self.olsync("init", str(self.remote), "--dir", str(self.paper), *extra)
        self.olsync("sync", "--dir", str(self.paper))

    def sync(self, *extra, code=0):
        return self.olsync("sync", "--dir", str(self.paper), *extra, code=code)

    def status(self):
        return json.loads(self.olsync("status", "--dir", str(self.paper), "--json")[0])

    def states(self):
        return {c["path"]: c["state"] for c in self.status()["changes"]}

    def local(self, rel):
        path = self.paper / rel
        return path.read_bytes() if path.exists() else None

    def set_local(self, rel, data: bytes):
        self.write(self.paper, {rel: data})

    def kept(self, name):
        return sorted(p.relative_to(self.paper / ".olsync" / "trash").as_posix()
                      for p in (self.paper / ".olsync" / "trash").rglob(name))

    def build_command(self):
        script = self.tmp / "fake_build.py"
        script.write_text(FAKE_BUILD)
        argv = [sys.executable, str(script)]
        return subprocess.list2cmdline(argv) if os.name == "nt" else " ".join(map(shlex.quote, argv))


class SyncTests(Case):
    def test_first_sync_pulls_everything(self):
        self.olsync("init", str(self.remote), "--dir", str(self.paper))
        self.assertEqual(set(self.states().values()), {"new-overleaf"})
        self.sync()
        for rel, data in FILES.items():
            self.assertEqual(self.local(rel), data, rel)
        self.assertEqual(self.states(), {})

    def test_local_edit_is_pushed(self):
        self.connect()
        self.set_local("sec/intro.tex", b"Intro line 1.\nIntro line 2, revised.\nIntro line 3.\n")
        self.assertEqual(self.states(), {"sec/intro.tex": "push"})
        self.sync("-m", "Revise the introduction")
        self.assertEqual(self.remote_file("sec/intro.tex"), self.local("sec/intro.tex"))
        self.assertEqual(self.states(), {})

    def test_overleaf_edit_is_pulled(self):
        self.connect()
        self.coauthor_edit({"refs.bib": b"@article{a,title={A}}\n@article{b,title={B}}\n"})
        self.assertEqual(self.states(), {"refs.bib": "pull"})
        self.sync()
        self.assertIn(b"@article{b", self.local("refs.bib"))

    def test_edits_in_different_places_of_one_file_merge(self):
        self.connect()
        self.set_local("sec/results.tex", replace_line(RESULTS, 2, "Result line 2, local."))
        self.coauthor_edit({"sec/results.tex": replace_line(RESULTS, 9, "Result line 9, Overleaf.")})
        self.assertEqual(self.states(), {"sec/results.tex": "merge"})
        self.sync()
        merged = self.local("sec/results.tex")
        self.assertIn(b"line 2, local", merged)
        self.assertIn(b"line 9, Overleaf", merged)
        self.assertEqual(self.remote_file("sec/results.tex"), merged)

    def test_same_lines_edited_on_both_sides_stops(self):
        self.connect()
        mine = replace_line(RESULTS, 2, "Result line 2, local.")
        self.set_local("sec/results.tex", mine)
        self.coauthor_edit({"sec/results.tex": replace_line(RESULTS, 2, "Result line 2, Overleaf.")})
        head = self.remote_head()
        out, _ = self.sync(code=3)
        self.assertIn("CONFLICT", out)
        self.assertEqual(self.local("sec/results.tex"), mine)
        self.assertEqual(self.remote_head(), head)

    def test_prefer_overleaf_keeps_local_edits_elsewhere(self):
        self.connect()
        mine = replace_line(replace_line(RESULTS, 2, "Result line 2, local."), 9, "Result line 9, local.")
        self.set_local("sec/results.tex", mine)
        self.coauthor_edit({"sec/results.tex": replace_line(RESULTS, 2, "Result line 2, Overleaf.")})
        self.sync("--prefer", "overleaf")
        got = self.local("sec/results.tex")
        self.assertIn(b"line 2, Overleaf", got)
        self.assertNotIn(b"line 2, local", got)
        self.assertIn(b"line 9, local", got)
        self.assertEqual(self.remote_file("sec/results.tex"), got)

    def test_prefer_local(self):
        self.connect()
        self.set_local("sec/results.tex", replace_line(RESULTS, 2, "Result line 2, local."))
        self.coauthor_edit({"sec/results.tex": replace_line(replace_line(RESULTS, 2, "x"), 9, "y")})
        self.sync("--prefer", "local")
        got = self.local("sec/results.tex")
        self.assertIn(b"line 2, local", got)
        self.assertIn(b"\ny\n", got)
        self.assertEqual(self.remote_file("sec/results.tex"), got)

    def test_prefer_newer_follows_edit_times(self):
        self.connect()
        self.set_local("sec/results.tex", replace_line(RESULTS, 2, "older local edit"))
        past = time.time() - 3600
        os.utime(self.paper / "sec" / "results.tex", (past, past))
        self.coauthor_edit({"sec/results.tex": replace_line(RESULTS, 2, "newer Overleaf edit")})
        self.sync("--prefer", "newer")
        self.assertIn(b"newer Overleaf edit", self.local("sec/results.tex"))

        current = self.local("sec/results.tex")
        self.coauthor_edit({"sec/results.tex": replace_line(current, 3, "older Overleaf edit")})
        self.set_local("sec/results.tex", replace_line(current, 3, "newer local edit"))
        future = time.time() + 3600
        os.utime(self.paper / "sec" / "results.tex", (future, future))
        self.sync("--prefer", "newer")
        self.assertIn(b"newer local edit", self.remote_file("sec/results.tex"))

    def test_prefer_newer_cannot_date_a_deletion(self):
        self.connect()
        (self.paper / "sec" / "intro.tex").unlink()
        self.coauthor_edit({"sec/intro.tex": b"co-author edit\n"})
        head = self.remote_head()
        _, err = self.sync("--prefer", "newer", code=3)
        self.assertIn("deletion", err)
        self.assertIsNone(self.local("sec/intro.tex"))
        self.assertEqual(self.remote_head(), head)

    def test_prefer_newer_also_stops_for_an_overleaf_deletion(self):
        self.connect()
        self.set_local("sec/intro.tex", b"my late edit\n")
        self.coauthor_edit({}, delete=["sec/intro.tex"])
        head = self.remote_head()
        self.sync("--prefer", "newer", code=3)
        self.assertEqual(self.local("sec/intro.tex"), b"my late edit\n")
        self.assertEqual(self.remote_head(), head)

    def test_expect_stops_when_overleaf_moved(self):
        self.connect()
        head = self.status()["overleaf_head"]
        self.set_local("sec/intro.tex", b"changed\n")
        self.coauthor_edit({"refs.bib": b"@article{c,title={C}}\n"})
        self.sync("--expect", head[:7], code=4)
        self.assertEqual(self.remote_file("sec/intro.tex"), FILES["sec/intro.tex"])
        self.assertEqual(self.local("refs.bib"), FILES["refs.bib"])

    def test_new_figure_is_sent_and_scratch_files_stay_local(self):
        self.connect()
        self.set_local("sec/results.tex", RESULTS + b"\\includegraphics{fig2}\n")
        self.set_local("figures/fig2.pdf", b"%PDF-1.4 fake\n")
        self.set_local("notes.txt", b"private notes\n")
        self.set_local("analysis/plot.py", b"print(1)\n")
        self.assertEqual(self.states(), {"sec/results.tex": "push", "figures/fig2.pdf": "new-local"})
        self.sync()
        self.assertEqual(self.remote_file("figures/fig2.pdf"), b"%PDF-1.4 fake\n")
        self.assertIsNone(self.remote_file("notes.txt"))
        self.assertIsNone(self.remote_file("analysis/plot.py"))

    def test_ambiguous_image_name_sends_every_match(self):
        self.connect()
        self.set_local("sec/results.tex",
                       RESULTS + b"\\graphicspath{{a/}{b/}}\n\\includegraphics{plot}\n")
        self.set_local("a/plot.pdf", b"%PDF-1.4 a\n")
        self.set_local("b/plot.pdf", b"%PDF-1.4 b\n")
        self.sync()
        self.assertEqual(self.remote_file("a/plot.pdf"), b"%PDF-1.4 a\n")
        self.assertEqual(self.remote_file("b/plot.pdf"), b"%PDF-1.4 b\n")

    def test_binary_files_keep_their_exact_bytes(self):
        self.connect()
        pdf = (b"%PDF-1.4\r\n%\xe2\xe3\xcf\xd3\r\n1 0 obj\r\n<< /Type /Catalog >>\r\nendobj\r\n"
               b"xref\r\n0 2\r\n%%EOF\r\n")
        self.set_local("sec/results.tex", RESULTS + b"\\includegraphics{plot.pdf}\n")
        self.set_local("plot.pdf", pdf)
        self.sync()
        self.assertEqual(self.remote_file("plot.pdf"), pdf)
        eps = b"%!PS-Adobe-3.0 EPSF-3.0\r\n%%BoundingBox: 0 0 1 1\r\nshowpage\r\n"
        self.coauthor_edit({"figures/scan.eps": eps})
        self.sync()
        self.assertEqual(self.local("figures/scan.eps"), eps)

    def test_files_on_overleaf_are_synced_even_if_they_match_exclude(self):
        self.connect()
        bbl = b"\\begin{thebibliography}{1}\n\\end{thebibliography}\n"
        self.coauthor_edit({"main.bbl": bbl})
        self.assertEqual(self.states(), {"main.bbl": "new-overleaf"})
        self.sync()
        self.assertEqual(self.local("main.bbl"), bbl)

    def test_missing_reference_is_reported(self):
        self.connect()
        self.set_local("sec/intro.tex", FILES["sec/intro.tex"] + b"\\input{sec/appendix}\n")
        missing = self.status()["missing_references"]
        self.assertEqual(missing, [{"path": "sec/appendix.tex", "referenced_in": "sec/intro.tex"}])

    def test_local_deletion_needs_allow_delete(self):
        self.connect()
        (self.paper / "sec" / "intro.tex").unlink()
        self.sync()
        self.assertIsNotNone(self.remote_file("sec/intro.tex"))
        self.assertEqual(self.states(), {"sec/intro.tex": "deleted-local"})
        self.sync("--allow-delete")
        self.assertIsNone(self.remote_file("sec/intro.tex"))
        self.assertEqual(self.states(), {})

    def test_overleaf_deletion_moves_local_copy_to_trash(self):
        self.connect()
        self.coauthor_edit({}, delete=["refs.bib"])
        self.assertEqual(self.states(), {"refs.bib": "deleted-overleaf"})
        self.sync()
        self.assertIsNone(self.local("refs.bib"))
        kept = list((self.paper / ".olsync" / "trash").rglob("refs.bib"))
        self.assertEqual([p.read_bytes() for p in kept], [FILES["refs.bib"]])
        self.assertEqual(self.states(), {})

    def test_crlf_copy_counts_as_identical(self):
        self.connect()
        crlf = FILES["sec/intro.tex"].replace(b"\n", b"\r\n")
        self.set_local("sec/intro.tex", crlf)
        self.assertEqual(self.states(), {})
        self.set_local("sec/intro.tex", crlf.replace(b"line 2", b"line two"))
        self.sync()
        self.assertEqual(self.remote_file("sec/intro.tex"),
                         FILES["sec/intro.tex"].replace(b"line 2", b"line two"))
        self.assertIn(b"\r\n", self.local("sec/intro.tex"))

    def test_pull_only_leaves_local_changes_pending(self):
        self.connect()
        self.set_local("sec/intro.tex", b"draft\n")
        self.coauthor_edit({"refs.bib": b"@article{z,title={Z}}\n"})
        self.sync("--pull-only")
        self.assertEqual(self.local("refs.bib"), b"@article{z,title={Z}}\n")
        self.assertEqual(self.remote_file("sec/intro.tex"), FILES["sec/intro.tex"])
        self.assertEqual(self.states(), {"sec/intro.tex": "push"})

    def test_existing_local_copy_is_compared_before_first_sync(self):
        self.write(self.paper, FILES)
        self.set_local("sec/intro.tex", b"my own version\n")
        self.olsync("init", str(self.remote), "--dir", str(self.paper))
        self.assertEqual(self.states(), {"sec/intro.tex": "conflict"})
        self.sync(code=3)
        self.sync("--prefer", "local")
        self.assertEqual(self.remote_file("sec/intro.tex"), b"my own version\n")
        self.assertEqual(len(self.kept("intro.tex")), 1)
        self.assertIn("/overleaf/sec/intro.tex", self.kept("intro.tex")[0])

    def test_prefer_without_earlier_sync_saves_the_replaced_version(self):
        self.write(self.paper, FILES)
        self.set_local("sec/intro.tex", b"my own version\n")
        self.olsync("init", str(self.remote), "--dir", str(self.paper))
        self.sync("--prefer", "overleaf")
        self.assertEqual(self.local("sec/intro.tex"), FILES["sec/intro.tex"])
        kept = list((self.paper / ".olsync" / "trash").rglob("intro.tex"))
        self.assertEqual([p.read_bytes() for p in kept], [b"my own version\n"])

    def test_init_leaves_an_existing_state_folder_alone(self):
        precious = self.paper / ".olsync" / "trash" / "old" / "x.tex"
        precious.parent.mkdir(parents=True)
        precious.write_bytes(b"only copy\n")
        _, err = self.olsync("init", str(self.remote), "--dir", str(self.paper), code=1)
        self.assertIn("already exists", err)
        self.assertEqual(precious.read_bytes(), b"only copy\n")

    def test_token_in_url_is_refused(self):
        _, err = self.olsync("init", "https://git:secret@git.overleaf.com/0123456789abcdef01234567",
                             "--dir", str(self.paper), code=1)
        self.assertIn("token", err)
        self.assertFalse(self.paper.exists())

    def test_folder_is_found_from_parent_and_child(self):
        self.connect()
        self.olsync("status", cwd=self.tmp)
        self.olsync("status", cwd=self.paper / "sec")

    def test_review_page_shows_both_sides(self):
        self.connect()
        self.set_local("sec/intro.tex", FILES["sec/intro.tex"].replace(b"line 1", b"line one"))
        self.coauthor_edit({"refs.bib": b"@article{a,title={Alpha}}\n"})
        page_path = self.tmp / "review.html"
        self.olsync("review", "--dir", str(self.paper), "-o", str(page_path))
        page = page_path.read_text(encoding="utf-8")
        for needle in ("sec/intro.tex", "refs.bib", "<del>", "<ins>", "Local", "Overleaf"):
            self.assertIn(needle, page)

    def test_overleaf_edit_right_after_the_push_is_reported(self):
        self.connect()
        hook = self.remote / "hooks" / "post-receive"
        hook.write_text(LATER_COMMIT_HOOK, newline="\n")
        hook.chmod(0o755)
        (self.tmp / "later.flag").write_text("x")
        self.set_local("sec/intro.tex", b"edited\n")
        r = json.loads(self.sync("--json")[0])
        self.assertEqual(r["overleaf_changed_since"], self.remote_head())
        self.assertNotEqual(r["overleaf_after"], self.remote_head())

    def test_folder_linked_elsewhere_is_left_alone(self):
        self.connect()
        outside = self.tmp / "outside_figures"
        (self.paper / "figures").rename(outside)
        try:
            os.symlink(outside, self.paper / "figures", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symbolic links are not available here")
        self.coauthor_edit({"figures/fig1.png": b"\x89PNG\r\n\x1a\nnew image"})
        _, err = self.sync(code=1)
        self.assertIn("symbolic link", err)
        self.assertEqual((outside / "fig1.png").read_bytes(), FILES["figures/fig1.png"])

    def test_link_inside_the_folder_is_also_left_alone(self):
        self.connect()
        try:
            os.symlink(self.paper / "figures", self.paper / "alias", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symbolic links are not available here")
        self.coauthor_edit({"alias/extra.tex": b"through the link\n"})
        _, err = self.sync(code=1)
        self.assertIn("symbolic link", err)
        self.assertFalse((self.paper / "figures" / "extra.tex").exists())

    def test_folder_replaced_by_a_file_stops_cleanly(self):
        self.connect()
        self.git("-C", str(self.work), "pull", "-q", "--ff-only", str(self.remote), "main")
        self.git("-C", str(self.work), "rm", "-q", "figures/fig1.png")
        shutil.rmtree(self.work / "figures", ignore_errors=True)
        (self.work / "figures").write_bytes(b"now a file\n")
        self.coauthor_commit()
        _, err = self.sync(code=1)
        self.assertIn("folder where Overleaf has a file", err)
        self.assertEqual(self.local("figures/fig1.png"), FILES["figures/fig1.png"])

    def test_old_fixed_temp_name_is_left_alone(self):
        self.connect()
        self.set_local("sec/intro.tex.olsync-tmp", b"someone's file\n")
        self.coauthor_edit({"sec/intro.tex": b"co-author edit\n"})
        self.sync()
        self.assertEqual(self.local("sec/intro.tex"), b"co-author edit\n")
        self.assertEqual(self.local("sec/intro.tex.olsync-tmp"), b"someone's file\n")

    @unittest.skipIf(os.name == "nt", "file modes are a POSIX feature")
    def test_executable_bit_survives_a_pull(self):
        self.connect()
        self.coauthor_edit({"tools/run.sh": b"#!/bin/sh\necho one\n"})
        self.sync()
        script = self.paper / "tools" / "run.sh"
        script.chmod(0o755)
        self.coauthor_edit({"tools/run.sh": b"#!/bin/sh\necho two\n"})
        self.sync()
        self.assertEqual(script.read_bytes(), b"#!/bin/sh\necho two\n")
        self.assertTrue(os.access(script, os.X_OK))

    def test_names_differing_only_in_case(self):
        self.connect()
        env = dict(self.env, GIT_AUTHOR_NAME="x", GIT_AUTHOR_EMAIL="x@x",
                   GIT_COMMITTER_NAME="x", GIT_COMMITTER_EMAIL="x@x")
        repo = olsync_module.Repo(self.remote)
        entries = repo.listing("main")
        for name, text in (("sec/Notes.tex", b"upper\n"), ("sec/notes.tex", b"lower\n")):
            entries[name] = ("100644", "blob", repo.out("hash-object", "-w", "--stdin", data=text))
        commit = repo.out("commit-tree", repo.write_tree(entries), "-p", "main", "-m", "case",
                          env=env)
        repo.run("update-ref", "refs/heads/main", commit)
        probe = self.tmp / "CaseProbe"
        probe.write_text("x")
        if (self.tmp / "caseprobe").exists():
            _, err = self.sync(code=1)
            self.assertIn("letter case", err)
            self.assertFalse((self.paper / "sec" / "notes.tex").exists())
        else:
            self.sync()
            self.assertEqual(self.local("sec/Notes.tex"), b"upper\n")
            self.assertEqual(self.local("sec/notes.tex"), b"lower\n")


class BuildTests(Case):
    def test_failed_build_blocks_push_and_metrics_are_read(self):
        self.connect("--build", self.build_command())
        state = json.loads((self.paper / ".olsync" / "state.json").read_text())
        self.assertEqual(state["last_build"], {"pages": 3, "undefined": 1, "missing_glyphs": 0})

        (self.tmp / "fail.flag").write_text("x")
        self.set_local("sec/intro.tex", b"edited\n")
        self.sync(code=5)
        self.assertEqual(self.remote_file("sec/intro.tex"), FILES["sec/intro.tex"])
        (self.tmp / "fail.flag").unlink()
        out, _ = self.sync()
        self.assertIn("3 pages", out)
        self.assertEqual(self.remote_file("sec/intro.tex"), b"edited\n")
        self.assertIsNone(self.remote_file("main.log"))
        self.assertIsNone(self.remote_file("main.pdf"))

    def test_chosen_deletion_survives_a_failed_build(self):
        self.connect("--build", self.build_command())
        (self.paper / "sec" / "intro.tex").unlink()
        self.coauthor_edit({"sec/intro.tex": b"co-author edit\n"})
        (self.tmp / "fail.flag").write_text("x")
        self.sync("--prefer", "local", code=5)
        self.assertIsNotNone(self.remote_file("sec/intro.tex"))
        (self.tmp / "fail.flag").unlink()
        self.sync()
        self.assertIsNone(self.remote_file("sec/intro.tex"))


class UnitTests(unittest.TestCase):
    def test_token_is_offered_only_to_the_project_host(self):
        tmp = Path(tempfile.mkdtemp(prefix="olsync-cred-"))
        try:
            env = dict(hermetic_env(tmp), OVERLEAF_TOKEN="t0ken-value", GIT_TERMINAL_PROMPT="0",
                       GCM_INTERACTIVE="never", GIT_ASKPASS="", SSH_ASKPASS="")
            with mock.patch.dict(os.environ, {"OVERLEAF_TOKEN": "t0ken-value"}):
                args = olsync_module.credential_args("https://git.overleaf.com/0123456789abcdef01234567")
                self.assertEqual(olsync_module.credential_args("http://git.overleaf.com/0123"), [])
            self.assertNotIn("t0ken-value", " ".join(args))

            def fill(host):
                p = subprocess.run(["git", *args, "credential", "fill"],
                                   input=f"protocol=https\nhost={host}\n\n".encode(),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
                return p.stdout.decode()
            self.assertIn("password=t0ken-value", fill("git.overleaf.com"))
            self.assertNotIn("t0ken-value", fill("other.example.org"))
        finally:
            rmtree(tmp)

    @staticmethod
    def unit_ctx(tmp: Path):
        return SimpleNamespace(local_path=lambda rel: tmp.joinpath(*rel.split("/")),
                               S=tmp / ".olsync", D=tmp, stamp="run")

    def test_write_stops_when_the_file_changed_after_planning(self):
        tmp = Path(tempfile.mkdtemp(prefix="olsync-unit-"))
        try:
            target = tmp / "a.tex"
            target.write_bytes(b"saved by an editor meanwhile\n")
            with self.assertRaises(olsync_module.Fail):
                olsync_module.write_local(self.unit_ctx(tmp), "a.tex", b"pulled\n", b"original\n")
            self.assertEqual(target.read_bytes(), b"saved by an editor meanwhile\n")
        finally:
            rmtree(tmp)

    def test_save_just_before_the_swap_is_kept(self):
        tmp = Path(tempfile.mkdtemp(prefix="olsync-unit-"))
        try:
            target = tmp / "a.tex"
            target.write_bytes(b"original\n")
            real_replace = os.replace

            def editor_saves_first(src, dst):
                if Path(src) == target:
                    target.write_bytes(b"editor save\n")
                return real_replace(src, dst)
            with mock.patch.object(olsync_module.os, "replace", side_effect=editor_saves_first):
                with self.assertRaises(olsync_module.Fail):
                    olsync_module.write_local(self.unit_ctx(tmp), "a.tex", b"pulled\n", b"original\n")
            self.assertEqual(target.read_bytes(), b"editor save\n")
        finally:
            rmtree(tmp)

    def test_file_recreated_during_the_swap_is_kept(self):
        tmp = Path(tempfile.mkdtemp(prefix="olsync-unit-"))
        try:
            target = tmp / "a.tex"
            target.write_bytes(b"original\n")
            real_replace = os.replace

            def editor_saves_after(src, dst):
                result = real_replace(src, dst)
                if Path(src) == target:
                    target.write_bytes(b"editor save\n")
                return result
            with mock.patch.object(olsync_module.os, "replace", side_effect=editor_saves_after):
                with self.assertRaises(olsync_module.Fail):
                    olsync_module.write_local(self.unit_ctx(tmp), "a.tex", b"pulled\n", b"original\n")
            self.assertEqual(target.read_bytes(), b"editor save\n")
            saved = [p.read_bytes() for p in (tmp / ".olsync" / "trash").rglob("a.tex")]
            self.assertEqual(saved, [b"original\n"])
            self.assertEqual(list(tmp.glob("*.olsync-tmp")) + list(tmp.glob(".*.olsync-tmp")), [])
        finally:
            rmtree(tmp)

    def test_busy_file_stops_cleanly(self):
        tmp = Path(tempfile.mkdtemp(prefix="olsync-unit-"))
        try:
            target = tmp / "a.tex"
            target.write_bytes(b"original\n")
            busy = OSError(errno.EBUSY, "Device or resource busy")
            with mock.patch.object(olsync_module.os, "replace", side_effect=busy):
                with self.assertRaises(olsync_module.Fail) as caught:
                    olsync_module.write_local(self.unit_ctx(tmp), "a.tex", b"pulled\n", b"original\n")
            self.assertIn("busy", str(caught.exception))
            self.assertEqual(target.read_bytes(), b"original\n")
        finally:
            rmtree(tmp)

    def test_saved_copies_never_replace_each_other(self):
        tmp = Path(tempfile.mkdtemp(prefix="olsync-unit-"))
        try:
            ctx = SimpleNamespace(S=tmp / ".olsync", D=tmp)
            first = olsync_module.keep_copy(ctx, "stamp", "local", "sec/a.tex", b"one\n")
            second = olsync_module.keep_copy(ctx, "stamp", "local", "sec/a.tex", b"two\n")
            self.assertNotEqual(first, second)
            self.assertEqual((tmp / first).read_bytes(), b"one\n")
            self.assertEqual((tmp / second).read_bytes(), b"two\n")
        finally:
            rmtree(tmp)

    def test_only_known_text_formats_are_normalised(self):
        self.assertTrue(olsync_module.is_text("sec/a.tex", b"a\r\nb\r\n"))
        self.assertFalse(olsync_module.is_text("fig/a.pdf", b"%PDF-1.4\r\nno nul here\r\n"))
        self.assertFalse(olsync_module.is_text("a.tex", b"a\0b"))


if __name__ == "__main__":
    unittest.main()
