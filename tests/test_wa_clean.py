"""Tests for wa-clean, run against a small fake WhatsApp folder.

    python3 -m unittest discover -s tests -v
"""

import argparse
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from argparse import Namespace
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wa_clean import cli, core  # noqa: E402

FRIEND, GROUP = "111@lid", "222@g.us"
LINKED = "aaaaaaaa-1111-1111-1111-111111111111"      # in ZMEDIALOCALPATH
ORPHAN_VIDEO = "bbbbbbbb-2222-2222-2222-222222222222"
ORPHAN_STICKER = "cccccccc-3333-3333-3333-333333333333"
STARRED = "dddddddd-4444-4444-4444-444444444444"     # starred message
THUMB_ONLY = "eeeeeeee-5555-5555-5555-555555555555"  # only in ZXMPPTHUMBPATH
IN_STATUS = "ffffffff-6666-6666-6666-666666666666"   # only in Status.sqlite
IN_LOG = "abcdefab-7777-7777-7777-777777777777"      # only in a log file
ODD_NAME = "holiday-video"                           # not an ID: keep

OLD = 700000000   # 2023-03-07 in WhatsApp's 2001-based dates
NEW = time.time() - core.APPLE_EPOCH - 86400


def rel(chat, name):
    return "Media/%s/%s/%s/%s" % (chat, name[0], name[1], name)


class FakeWhatsApp(object):
    """Builds a minimal WhatsApp Group Container in a temp folder."""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="wa_clean_test_")
        self.base = os.path.join(self.root, "Message")

    def add_file(self, chat, name, size):
        path = os.path.join(self.base, rel(chat, name))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"\0" * size)
        return path

    def build(self, schema_ok=True):
        files = [
            (FRIEND, LINKED + ".jpg", 2000000),
            (FRIEND, ORPHAN_VIDEO + ".mp4", 3000000),
            (GROUP, ORPHAN_STICKER + ".webp", 50000),
            (GROUP, STARRED + ".jpg", 900000),
            (GROUP, THUMB_ONLY + ".jpg", 400000),
            (GROUP, IN_STATUS + ".jpg", 300000),
            (GROUP, IN_LOG + ".jpg", 200000),
            (GROUP, ODD_NAME + ".mp4", 100000),
        ]
        for chat, name, size in files:
            self.add_file(chat, name, size)

        db = sqlite3.connect(os.path.join(self.root, "ChatStorage.sqlite"))
        starred_col = "ZSTARRED" if schema_ok else "ZRENAMED"
        db.executescript("""
            CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT, ZPARTNERNAME TEXT);
            CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY,
                ZMESSAGEDATE REAL, %s INTEGER);
            CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY,
                ZMESSAGE INTEGER, ZMEDIALOCALPATH TEXT,
                ZTHUMBNAILLOCALPATH TEXT, ZXMPPTHUMBPATH TEXT);
        """ % starred_col)
        db.executemany("INSERT INTO ZWACHATSESSION VALUES (?, ?, ?)",
                       [(1, FRIEND, "Test Friend"), (2, GROUP, "Test Group")])
        db.executemany("INSERT INTO ZWAMESSAGE VALUES (?, ?, ?)",
                       [(1, OLD, 0), (2, NEW, 1), (3, NEW, 0)])
        db.executemany("INSERT INTO ZWAMEDIAITEM VALUES (?, ?, ?, ?, ?)", [
            (1, 1, rel(FRIEND, LINKED + ".jpg"), None, None),
            (2, 2, rel(GROUP, STARRED + ".jpg"), None, None),
            (3, 3, None, None, rel(GROUP, THUMB_ONLY + ".thumb")),
        ])
        db.commit()
        db.close()

        os.makedirs(os.path.join(self.root, "statusDB"))
        status = sqlite3.connect(os.path.join(self.root, "statusDB",
                                              "Status.sqlite"))
        status.execute("CREATE TABLE media_content (file_path TEXT)")
        status.execute("INSERT INTO media_content VALUES (?)",
                       (rel(GROUP, IN_STATUS + ".jpg"),))
        status.commit()
        status.close()

        os.makedirs(os.path.join(self.root, "Logs"))
        with open(os.path.join(self.root, "Logs", "whatsapp.log"), "w") as fh:
            fh.write("orphaned/linking [1; Media/x/%s.jpg -> ...]\n" % IN_LOG)
        return self

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


def filters(**kw):
    base = dict(chat=None, exclude_chat=None, type=None, older_than=None,
                larger_than=None, unlinked=False, include_starred=False)
    base.update(kw)
    return Namespace(**base)


def by_id(lib):
    return {os.path.basename(f.path).split(".")[0]: f for f in lib.files}


class LinkedTests(unittest.TestCase):
    def setUp(self):
        self.wa = FakeWhatsApp().build()
        self.lib = core.load_library(self.wa.root)
        self.files = by_id(self.lib)

    def tearDown(self):
        self.wa.cleanup()

    def test_no_problems_on_a_healthy_folder(self):
        self.assertEqual(self.lib.problems, [])
        self.assertEqual(len(self.lib.files), 8)

    def test_only_true_orphans_are_unlinked(self):
        unlinked = {k for k, f in self.files.items() if not f.linked}
        self.assertEqual(unlinked, {ORPHAN_VIDEO, ORPHAN_STICKER})

    def test_ids_mentioned_elsewhere_count_as_linked(self):
        for fid in (THUMB_ONLY, IN_STATUS, IN_LOG):
            self.assertTrue(self.files[fid].linked, fid)

    def test_names_that_are_not_ids_are_kept(self):
        self.assertTrue(self.files[ODD_NAME].linked)

    def test_chat_names_and_starred(self):
        self.assertEqual(self.lib.names[FRIEND], "Test Friend")
        self.assertTrue(self.files[STARRED].starred)
        self.assertFalse(self.files[LINKED].starred)


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.wa = FakeWhatsApp().build()
        self.lib = core.load_library(self.wa.root)

    def tearDown(self):
        self.wa.cleanup()

    def pick(self, **kw):
        chosen = core.select(self.lib.files, self.lib.names, filters(**kw))
        return {os.path.basename(f.path).split(".")[0] for f in chosen}

    def test_starred_is_kept_unless_asked(self):
        self.assertNotIn(STARRED, self.pick())
        self.assertIn(STARRED, self.pick(include_starred=True))

    def test_type(self):
        self.assertEqual(self.pick(type=["sticker"]), {ORPHAN_STICKER})

    def test_chat_by_name_part_or_id(self):
        self.assertEqual(self.pick(chat=["friend"]), {LINKED, ORPHAN_VIDEO})
        self.assertEqual(self.pick(chat=[FRIEND]), {LINKED, ORPHAN_VIDEO})
        self.assertNotIn(LINKED, self.pick(exclude_chat=["friend"]))

    def test_size_and_age(self):
        self.assertEqual(self.pick(larger_than=2500000), {ORPHAN_VIDEO})
        self.assertEqual(self.pick(older_than=365 * 86400), {LINKED})

    def test_unlinked(self):
        self.assertEqual(self.pick(unlinked=True),
                         {ORPHAN_VIDEO, ORPHAN_STICKER})


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.wa = FakeWhatsApp()

    def tearDown(self):
        self.wa.cleanup()

    def assert_refuses_to_trash(self):
        args = cli.build_parser().parse_args(
            ["--container", self.wa.root, "clean", "--unlinked", "--trash",
             "--yes", "--force"])
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                cli.cmd_clean(args)
        self.assertIn("Not moving", str(cm.exception))
        self.assertEqual(len(core.load_library(self.wa.root).files),
                         len(self.wa_files))

    def test_missing_database(self):
        self.wa.build()
        os.remove(os.path.join(self.wa.root, "ChatStorage.sqlite"))
        self.wa_files = core.load_library(self.wa.root).files
        lib = core.load_library(self.wa.root)
        self.assertFalse(lib.safe)
        self.assertIn("Couldn't find", lib.problems[0])
        self.assert_refuses_to_trash()

    def test_changed_schema(self):
        self.wa.build(schema_ok=False)
        self.wa_files = core.load_library(self.wa.root).files
        lib = core.load_library(self.wa.root)
        self.assertIn("ZWAMESSAGE.ZSTARRED", lib.problems[0])
        self.assert_refuses_to_trash()

    def test_raw_scan_must_agree_with_database(self):
        ids = {"Media/x/%s.jpg" % LINKED: (None, False)}
        self.assertEqual(core.check_refs(ids, {LINKED.encode()}), [])
        self.assertTrue(core.check_refs(ids, set()))
        self.assertTrue(core.check_refs(ids, {b"something-else"}))


class TrashAndRestoreTests(unittest.TestCase):
    def setUp(self):
        self.wa = FakeWhatsApp().build()
        self.stage_root = tempfile.mkdtemp(prefix="wa_clean_stage_")

    def tearDown(self):
        self.wa.cleanup()
        shutil.rmtree(self.stage_root, ignore_errors=True)

    def test_round_trip(self):
        lib = core.load_library(self.wa.root)
        chosen = core.select(lib.files, lib.names, filters(unlinked=True))
        trashed = []
        r = core.stage_and_trash(chosen, lib.names, self.wa.root,
                                 stage_root=self.stage_root,
                                 trash=lambda p: trashed.append(p) or True,
                                 log=lambda m: None)
        self.assertEqual((r["moved"], r["failed"]), (2, 0))
        self.assertEqual(r["freed"], 3050000)
        self.assertEqual(trashed, [r["stage"]])
        self.assertTrue(os.path.exists(
            os.path.join(r["stage"], "HOW-TO-RESTORE.txt")))
        self.assertEqual(len(core.load_library(self.wa.root).files), 6)

        self.assertEqual(core.restore(r["stage"]), (2, 0))
        self.assertEqual(len(core.load_library(self.wa.root).files), 8)
        self.assertEqual(core.restore(r["stage"]), (0, 2))  # nothing left

    def test_restore_needs_manifest(self):
        with self.assertRaises(FileNotFoundError):
            core.restore(self.stage_root)


class ParseTests(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual(core.parse_size("50MB"), 50000000)
        self.assertEqual(core.parse_size("1.5g"), 1500000000)
        self.assertEqual(core.parse_size("800 KB"), 800000)

    def test_ages(self):
        self.assertEqual(core.parse_age("30d"), 30 * 86400)
        self.assertEqual(core.parse_age("6m"), 180 * 86400)

    def test_bad_input(self):
        for bad in ("lots", "5 parsecs"):
            with self.assertRaises(argparse.ArgumentTypeError):
                core.parse_size(bad)
        with self.assertRaises(argparse.ArgumentTypeError):
            core.parse_age("soon")


if __name__ == "__main__":
    unittest.main()
