"""Find WhatsApp media on macOS and decide what is safe to remove.

Everything here is read-only except stage_and_trash() and restore().
"""

import argparse
import csv
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time

CONTAINER = os.path.expanduser(
    "~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared")
APPLE_EPOCH = 978307200  # 2001-01-01, used for WhatsApp message dates
MANIFEST = "manifest.csv"

TYPES = {
    "photo": {"jpg", "jpeg", "png", "heic", "gif"},
    "video": {"mp4", "mov", "m4v", "3gp"},
    "sticker": {"webp", "was"},
    "audio": {"opus", "aac", "m4a", "mp3", "ogg", "wav", "amr"},
    "document": {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt",
                 "zip", "rar", "apk", "csv", "vcf"},
    "thumbnail": {"thumb", "mmsthumb", "videothumb"},
}
EXT_TYPE = {ext: t for t, exts in TYPES.items() for ext in exts}
TYPE_ORDER = list(TYPES) + ["other"]

# Columns we rely on. If WhatsApp renames any of them we stop rather than
# guess, because names, dates and starred messages would silently go missing.
REQUIRED_COLUMNS = {
    "ZWACHATSESSION": {"ZCONTACTJID", "ZPARTNERNAME"},
    "ZWAMESSAGE": {"Z_PK", "ZMESSAGEDATE", "ZSTARRED"},
    "ZWAMEDIAITEM": {"ZMESSAGE", "ZMEDIALOCALPATH", "ZTHUMBNAILLOCALPATH"},
}
OPTIONAL_PATH_COLUMNS = ("ZXMPPTHUMBPATH",)

UUID_RE = re.compile(
    rb"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


class MediaFile(object):
    __slots__ = ("path", "rel", "chat", "kind", "size", "date", "linked",
                 "starred")

    def __init__(self, path, rel, chat, kind, size, date, linked, starred):
        self.path = path
        self.rel = rel
        self.chat = chat
        self.kind = kind
        self.size = size
        self.date = date
        self.linked = linked
        self.starred = starred


class Library(object):
    """The media files on disk, chat names, and anything that looked wrong.

    When `problems` is not empty the linked/starred information can't be
    trusted, so nothing should be moved.
    """

    def __init__(self, files, names, problems):
        self.files = files
        self.names = names
        self.problems = problems

    @property
    def safe(self):
        return not self.problems


# ---------------------------------------------------------------- helpers

def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1000:
            return "%.0f %s" % (n, unit) if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1000.0
    return "%.1f TB" % n


def parse_size(text):
    m = re.fullmatch(r"\s*([\d.]+)\s*([kmgt]?)b?\s*", text, re.I)
    if not m:
        raise argparse.ArgumentTypeError("size like 50MB, 1.5GB or 800KB")
    mult = {"": 1, "k": 1e3, "m": 1e6, "g": 1e9, "t": 1e12}[m.group(2).lower()]
    return int(float(m.group(1)) * mult)


def parse_age(text):
    m = re.fullmatch(r"\s*(\d+)\s*([dwmy])\s*", text, re.I)
    if not m:
        raise argparse.ArgumentTypeError("age like 30d, 8w, 6m or 1y")
    days = {"d": 1, "w": 7, "m": 30, "y": 365}[m.group(2).lower()]
    return int(m.group(1)) * days * 86400


def whatsapp_running():
    return subprocess.run(["pgrep", "-x", "WhatsApp"],
                          stdout=subprocess.DEVNULL).returncode == 0


def file_id(name):
    """The UUID part of a media file name, as bytes, or None."""
    stem = os.path.basename(name).split(".", 1)[0].lower().encode()
    return stem if UUID_RE.fullmatch(stem) else None


# ---------------------------------------------------------------- loading

def open_db(container):
    """Copy ChatStorage.sqlite (with its WAL) so we never touch the live DB."""
    src = os.path.join(container, "ChatStorage.sqlite")
    if not os.path.exists(src):
        return None, None
    tmp = tempfile.mkdtemp(prefix="wa_clean_")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(src + suffix):
            shutil.copy2(src + suffix, os.path.join(tmp, "db.sqlite" + suffix))
    return sqlite3.connect(os.path.join(tmp, "db.sqlite")), tmp


def check_schema(conn):
    """Return (missing "TABLE.COLUMN" names, optional path columns present)."""
    missing = []
    present = {}
    for table in set(REQUIRED_COLUMNS) | {"ZWAMEDIAITEM"}:
        present[table] = {row[1] for row in
                          conn.execute("PRAGMA table_info(%s)" % table)}
    for table, cols in sorted(REQUIRED_COLUMNS.items()):
        missing += ["%s.%s" % (table, c) for c in sorted(cols - present[table])]
    extra = [c for c in OPTIONAL_PATH_COLUMNS if c in present["ZWAMEDIAITEM"]]
    return missing, extra


def load_db(container):
    """Return (chat names by jid, {relative media path: (date, starred)},
    list of problems)."""
    names, media = {}, {}
    try:
        conn, tmp = open_db(container)
    except (OSError, sqlite3.Error) as e:
        return names, media, ["Couldn't read WhatsApp's chat database: %s" % e]
    if conn is None:
        return names, media, [
            "Couldn't find WhatsApp's chat database (ChatStorage.sqlite). "
            "Is the Mac App Store version of WhatsApp installed?"]
    try:
        missing, extra = check_schema(conn)
        if missing:
            return names, media, [
                "WhatsApp's database layout has changed (missing %s). "
                "This version of wa-clean can't tell which media is still in "
                "use, so it won't move anything." % ", ".join(missing)]
        for jid, name in conn.execute(
                "SELECT ZCONTACTJID, ZPARTNERNAME FROM ZWACHATSESSION"):
            if jid and name:
                names[jid] = name
        path_cols = ["m.ZMEDIALOCALPATH", "m.ZTHUMBNAILLOCALPATH"]
        path_cols += ["m." + c for c in extra]
        # LEFT JOIN: a media row without a message still owns its file.
        rows = conn.execute(
            "SELECT g.ZMESSAGEDATE, g.ZSTARRED, %s "
            "FROM ZWAMEDIAITEM m LEFT JOIN ZWAMESSAGE g ON m.ZMESSAGE = g.Z_PK"
            % ", ".join(path_cols))
        for row in rows:
            date, starred = row[0], row[1]
            info = ((date + APPLE_EPOCH) if date else None, bool(starred))
            for p in row[2:]:
                if p:
                    media[p.lstrip("/")] = info
    except sqlite3.Error as e:
        return names, media, ["Couldn't read WhatsApp's chat database: %s" % e]
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)
    return names, media, []


def load_refs(container):
    """Every file ID mentioned anywhere in WhatsApp's databases.

    Media paths show up in many columns and in other databases (status,
    link previews, metadata blobs), so instead of trusting a list of columns
    we scan the raw bytes of every database file. WhatsApp's logs are
    included too: its own "orphaned/linking" task names files it may still
    relink. Over-matching only makes us keep more, never remove more.
    """
    refs = set()
    media_dir = os.path.join(container, "Message")
    for dirpath, _, filenames in os.walk(container):
        if dirpath.startswith(media_dir):
            continue
        for fn in filenames:
            if ".sqlite" in fn or fn.endswith((".db", ".db-wal", ".plist",
                                                ".log")):
                try:
                    with open(os.path.join(dirpath, fn), "rb") as fh:
                        refs.update(m.lower() for m in
                                    UUID_RE.findall(fh.read()))
                except OSError:
                    pass
    return refs


def check_refs(media, refs):
    """Sanity-check the raw ID scan against the paths the database lists.

    Every path in the database must also turn up in the raw scan. If many
    don't, the scan is broken (encrypted database, new file format) and
    "unlinked" would be wrong for everything.
    """
    ids = {file_id(p) for p in media} - {None}
    if not ids:
        return []
    if not refs:
        return ["Found no file IDs in WhatsApp's databases, so wa-clean can't "
                "tell which media is still in use."]
    missing = len(ids - refs)
    if missing > len(ids) * 0.01:
        return ["%d of %d media files listed in WhatsApp's database weren't "
                "found when scanning its files, so the unlinked check isn't "
                "reliable here." % (missing, len(ids))]
    return []


def load_library(container):
    names, media, problems = load_db(container)
    refs = load_refs(container)
    if not problems:
        problems = check_refs(media, refs)
    base = os.path.join(container, "Message")
    root = os.path.join(base, "Media")
    files = []
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            if fn.startswith("."):
                continue
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, base)          # Media/<jid>/a/b/x.jpg
            parts = rel.split(os.sep)
            chat = parts[1] if len(parts) > 2 else "(loose files)"
            ext = fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
            try:
                st = os.lstat(path)
            except OSError:
                continue
            info = media.get(rel)
            date = info[0] if info and info[0] else st.st_mtime
            # Unlinked only when the name is a file ID that no database
            # mentions; anything else (odd names included) counts as linked.
            fid = file_id(fn)
            linked = info is not None or fid is None or fid in refs
            files.append(MediaFile(path, rel, chat, EXT_TYPE.get(ext, "other"),
                                   st.st_size, date, linked,
                                   bool(info and info[1])))
    return Library(files, names, problems)


# ---------------------------------------------------------------- selecting

def match_chat(jid, names, patterns):
    name = names.get(jid, "").lower()
    for p in patterns:
        p = p.lower()
        if p == jid.lower() or (name and p in name):
            return True
    return False


def select(files, names, args):
    now = time.time()
    chosen = []
    for f in files:
        if args.chat and not match_chat(f.chat, names, args.chat):
            continue
        if args.exclude_chat and match_chat(f.chat, names, args.exclude_chat):
            continue
        if args.type and f.kind not in args.type:
            continue
        if args.older_than and now - f.date < args.older_than:
            continue
        if args.larger_than and f.size < args.larger_than:
            continue
        if args.unlinked and f.linked:
            continue
        if f.starred and not args.include_starred:
            continue
        chosen.append(f)
    return chosen


# ---------------------------------------------------------------- moving

def trash_path(path):
    if os.path.exists("/usr/bin/trash"):
        cmd = ["/usr/bin/trash", path]
    else:
        script = 'tell application "Finder" to delete POSIX file "%s"' % path
        cmd = ["osascript", "-e", script]
    return subprocess.run(cmd, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def restore_command(stage):
    if shutil.which("wa-clean"):
        return "wa-clean restore \"%s\"" % stage
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return "PYTHONPATH=\"%s\" python3 -m wa_clean restore \"%s\"" % (
        pkg_parent, stage)


def stage_and_trash(chosen, names, container, stage_root="~", trash=trash_path,
                    log=print):
    """Move files into a cleanup folder, then put that folder in the Trash.

    Returns {"stage", "moved", "failed", "freed", "trashed"}.
    """
    stamp = time.strftime("%Y-%m-%d %H.%M.%S")
    stage = os.path.join(os.path.expanduser(stage_root),
                         "WhatsApp Cleanup %s" % stamp)
    os.makedirs(stage)
    base = os.path.join(container, "Message")
    moved, failed, freed = 0, 0, 0
    with open(os.path.join(stage, MANIFEST), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["relative_path", "original_path", "chat", "type", "size"])
        for i, f in enumerate(chosen, 1):
            dest = os.path.join(stage, f.rel)
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                os.rename(f.path, dest)      # same volume: instant, no copy
            except OSError as e:
                failed += 1
                if failed <= 5:
                    log("  could not move %s: %s" % (f.path, e))
                continue
            w.writerow([f.rel, f.path, names.get(f.chat, f.chat), f.kind,
                        f.size])
            moved += 1
            freed += f.size
            if i % 5000 == 0:
                log("  moved %d of %d…" % (i, len(chosen)))
    with open(os.path.join(stage, "container.txt"), "w") as fh:
        fh.write(base + "\n")
    with open(os.path.join(stage, "HOW-TO-RESTORE.txt"), "w") as fh:
        fh.write("These files were moved here by wa-clean.\n\n"
                 "To put them back: drag this folder out of the Trash (or use\n"
                 "Put Back), quit WhatsApp, then run in Terminal:\n\n  %s\n"
                 % restore_command(stage))
    return {"stage": stage, "moved": moved, "failed": failed, "freed": freed,
            "trashed": trash(stage)}


def restore(stage):
    """Move files from a cleanup folder back. Returns (restored, skipped)."""
    manifest = os.path.join(stage, MANIFEST)
    if not os.path.exists(manifest):
        raise FileNotFoundError(
            "No %s in %s. Put the folder back from the Trash first."
            % (MANIFEST, stage))
    with open(os.path.join(stage, "container.txt")) as fh:
        base = fh.read().strip()
    restored, skipped = 0, 0
    with open(manifest, newline="") as fh:
        for row in csv.DictReader(fh):
            src = os.path.join(stage, row["relative_path"])
            dest = os.path.join(base, row["relative_path"])
            if not os.path.exists(src) or os.path.exists(dest):
                skipped += 1
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            os.rename(src, dest)
            restored += 1
    return restored, skipped
