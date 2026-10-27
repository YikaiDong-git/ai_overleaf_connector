"""Offline tests for tracked pushes: the ShareJS op builder, socket.io frame parsing, Overleaf's text hash,
and when olsync chooses tracked pushes. The network side is exercised by hand against overleaf.com."""

import hashlib
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "overleaf-sync" / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


olrt = load("olrt")
olsync = load("olsync")


class WordOps(unittest.TestCase):
    CASES = [
        ("", ""),
        ("", "new text\n"),
        ("old text\n", ""),
        ("The quick brown fox.", "The slow brown fox."),
        ("a b c d", "x y c z"),
        ("line one\nline two\n", "line one\n\nline 2, revised\n"),
        ("Café \U0001F600 risk-haplotype", "Cafe \U0001F600\U0001F600 risk haplotype ünï"),
        ("\\textbf{Insight 1:} each chromosome", "\\textbf{Challenge 1:} the chromosome"),
    ]

    def test_ops_turn_src_into_dst(self):
        for src, dst in self.CASES:
            with self.subTest(src=src, dst=dst):
                self.assertEqual(olrt.apply_ops(src, olrt.word_ops(src, dst)), dst)

    def test_no_ops_for_equal_text(self):
        self.assertEqual(olrt.word_ops("same text\n", "same text\n"), [])

    def test_positions_count_utf16_units(self):
        ops = olrt.word_ops("\U0001F600 a", "\U0001F600 b")
        self.assertEqual(ops, [{"p": 3, "d": "a"}, {"p": 3, "i": "b"}])

    def test_one_space_between_changes_merges(self):
        ops = olrt.word_ops("keep a b keep", "keep x y keep")
        self.assertEqual(ops, [{"p": 5, "d": "a b"}, {"p": 5, "i": "x y"}])

    def test_beyond_bmp(self):
        self.assertEqual(olrt.beyond_bmp("Café – ok"), [])
        self.assertEqual(olrt.beyond_bmp("a \U0001F600 b \U0001F600 \U0001D400"), ["\U0001D400", "\U0001F600"])

    def test_mismatched_delete_fails(self):
        with self.assertRaises(olrt.RTError):
            olrt.apply_ops("abc", [{"p": 0, "d": "x"}])


class Protocol(unittest.TestCase):
    def test_frames(self):
        self.assertEqual(olrt.frames("1::"), ["1::"])
        self.assertEqual(olrt.frames(""), [])
        body = "�3�1::�5�2::\U0001F600"
        self.assertEqual(olrt.frames(body), ["1::", "2::\U0001F600"])

    def test_doc_hash_matches_git_blob_for_ascii(self):
        text = "hello\nworld\n"
        git = hashlib.sha1(b"blob %d\0" % len(text.encode()) + text.encode()).hexdigest()
        self.assertEqual(olrt.doc_hash(text), git)

    def test_doc_hash_uses_js_length(self):
        text = "\U0001F600"
        self.assertEqual(olrt.doc_hash(text),
                         hashlib.sha1(b"blob 2\0" + text.encode("utf-8")).hexdigest())


class TrackedMode(unittest.TestCase):
    def ctx(self, remote):
        return SimpleNamespace(cfg={"remote": remote})

    def test_overleaf_remote_is_tracked_by_default(self):
        ctx = self.ctx("https://git.overleaf.com/6ad3e7b84b3da0759802a3f9")
        self.assertTrue(olsync.tracked_mode(ctx, SimpleNamespace(untracked=False)))
        self.assertEqual(olsync.overleaf_project(ctx), ("6ad3e7b84b3da0759802a3f9", "www.overleaf.com"))

    def test_untracked_flag(self):
        ctx = self.ctx("https://git.overleaf.com/6ad3e7b84b3da0759802a3f9")
        self.assertFalse(olsync.tracked_mode(ctx, SimpleNamespace(untracked=True)))

    def test_other_git_servers_push_plain(self):
        for remote in ("/tmp/remote.git", "https://git.example.org/paper.git"):
            self.assertFalse(olsync.tracked_mode(self.ctx(remote), SimpleNamespace(untracked=False)))

    def test_session_cookie_sources(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(os.environ, {"OVERLEAF_SESSION": ""}), \
                    mock.patch.object(Path, "home", return_value=Path(home)):
                self.assertIsNone(olsync.session_cookie())
                (Path(home) / ".overleaf_session").write_text("abc\n")
                self.assertEqual(olsync.session_cookie(), "abc")
            with mock.patch.dict(os.environ, {"OVERLEAF_SESSION": " xyz "}):
                self.assertEqual(olsync.session_cookie(), "xyz")


if __name__ == "__main__":
    unittest.main()
