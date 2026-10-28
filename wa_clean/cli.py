"""wa-clean: find and remove WhatsApp media on macOS without losing chats.

Commands
  scan      Show how much media each chat uses, by type.
  clean     Pick media with filters. Previews by default; add --trash to
            move the matching files to the Trash.
  restore   Move files from a cleanup folder back into WhatsApp.
  gui       Open the same tools in your browser.

Examples
  wa-clean scan
  wa-clean clean --unlinked
  wa-clean clean --chat "Family" --type sticker
  wa-clean clean --older-than 6m --type video --larger-than 20MB
  wa-clean clean --unlinked --trash

Moved files go into one folder ("WhatsApp Cleanup <date>"), which is then put
in the Trash. To undo, use Put Back in Finder, then run `restore` on it.
"""

import argparse
import csv
import os
import sys
import time
from collections import defaultdict

from . import __version__
from .core import (CONTAINER, TYPE_ORDER, human, load_library, match_chat,
                   parse_age, parse_size, restore, restore_command, select,
                   stage_and_trash, whatsapp_running)

RUNNING_MSG = "WhatsApp is running. Quit it first (⌘Q), then run this again."


def print_problems(lib):
    for p in lib.problems:
        print("⚠️  %s" % p)
    if lib.problems:
        print()


def cmd_scan(args):
    lib = load_library(args.container)
    print_problems(lib)
    files, names = lib.files, lib.names
    if not files:
        print("No WhatsApp media found in %s" % args.container)
        return
    by_chat = defaultdict(lambda: defaultdict(int))
    for f in files:
        by_chat[f.chat][f.kind] += f.size
        by_chat[f.chat]["_total"] += f.size
        if not f.linked:
            by_chat[f.chat]["_unlinked"] += f.size

    total = sum(f.size for f in files)
    unlinked = sum(f.size for f in files if not f.linked)
    print("WhatsApp media: %s in %d files across %d chats"
          % (human(total), len(files), len(by_chat)))
    print("Not linked to any message (WhatsApp can't show these): %s\n"
          % human(unlinked))

    cols = ["photo", "video", "sticker", "audio", "document"]
    header = "%-34s %9s %9s " % ("Chat", "Total", "Unlinked")
    header += " ".join("%9s" % c.capitalize() for c in cols)
    print(header)
    print("-" * len(header))
    ranked = sorted(by_chat.items(), key=lambda kv: -kv[1]["_total"])
    for jid, sizes in ranked[:args.top]:
        name = names.get(jid, jid)
        if len(name) > 34:
            name = name[:33] + "…"
        row = "%-34s %9s %9s " % (name, human(sizes["_total"]),
                                  human(sizes["_unlinked"]))
        row += " ".join("%9s" % human(sizes[c]) for c in cols)
        print(row)
    if len(ranked) > args.top:
        rest = sum(s["_total"] for _, s in ranked[args.top:])
        print("… %d more chats, %s" % (len(ranked) - args.top, human(rest)))


def print_preview(chosen, names, show_files):
    total = sum(f.size for f in chosen)
    print("Matched %d files, %s\n" % (len(chosen), human(total)))
    if not chosen:
        return
    by_chat = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for f in chosen:
        by_chat[f.chat][f.kind][0] += 1
        by_chat[f.chat][f.kind][1] += f.size
    ranked = sorted(by_chat.items(),
                    key=lambda kv: -sum(v[1] for v in kv[1].values()))
    for jid, kinds in ranked[:15]:
        size = sum(v[1] for v in kinds.values())
        parts = ", ".join("%d %s" % (kinds[k][0], k)
                          for k in TYPE_ORDER if k in kinds)
        print("  %-40s %9s  %s" % (names.get(jid, jid)[:40], human(size), parts))
    if len(ranked) > 15:
        print("  … and %d more chats" % (len(ranked) - 15))

    if show_files:
        print("\nLargest files:")
    for f in sorted(chosen, key=lambda f: -f.size)[:show_files]:
        print("  %9s  %s  %-9s %s" % (
            human(f.size), time.strftime("%Y-%m-%d", time.localtime(f.date)),
            f.kind, names.get(f.chat, f.chat)))


def write_csv(path, chosen, names):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["chat", "type", "size", "date", "linked", "path"])
        for f in chosen:
            w.writerow([names.get(f.chat, f.chat), f.kind, f.size,
                        time.strftime("%Y-%m-%d", time.localtime(f.date)),
                        "yes" if f.linked else "no", f.path])


def move_to_trash(chosen, names, container):
    r = stage_and_trash(chosen, names, container)
    print("Moved %d files%s." % (
        r["moved"], " (%d failed)" % r["failed"] if r["failed"] else ""))
    if r["trashed"]:
        print("Put \"%s\" in the Trash. Empty the Trash to free the space."
              % os.path.basename(r["stage"]))
        print("To undo: Put Back in Finder, then run:\n  %s"
              % restore_command(r["stage"]))
    else:
        print("Couldn't move the folder to the Trash. The files are in:\n  %s\n"
              "Drag it to the Trash yourself, or restore with:\n  %s"
              % (r["stage"], restore_command(r["stage"])))


def cmd_clean(args):
    if args.trash and whatsapp_running() and not args.force:
        sys.exit(RUNNING_MSG)
    lib = load_library(args.container)
    print_problems(lib)
    if args.trash and not lib.safe:
        sys.exit("Not moving anything because of the problem above.")
    files, names = lib.files, lib.names
    if args.chat:
        known = {f.chat for f in files}
        for p in args.chat:
            if not any(match_chat(j, names, [p]) for j in known):
                print("Note: no chat matches %r" % p)
    chosen = select(files, names, args)
    print_preview(chosen, names, args.show)
    if args.csv:
        write_csv(args.csv, chosen, names)
        print("\nWrote the full list to %s" % args.csv)
    if not chosen:
        return
    if not args.trash:
        print("\nPreview only. Nothing was moved. Add --trash to move these "
              "files to the Trash.")
        return
    if not args.yes:
        answer = input("\nMove %d files (%s) to the Trash? [y/N] "
                       % (len(chosen), human(sum(f.size for f in chosen))))
        if answer.strip().lower() not in ("y", "yes"):
            print("Cancelled. Nothing was moved.")
            return
    move_to_trash(chosen, names, args.container)


def cmd_restore(args):
    if whatsapp_running() and not args.force:
        sys.exit(RUNNING_MSG)
    stage = os.path.abspath(os.path.expanduser(args.folder))
    try:
        restored, skipped = restore(stage)
    except FileNotFoundError as e:
        sys.exit(str(e))
    print("Restored %d files%s." % (
        restored, " (%d skipped: missing or already there)" % skipped
        if skipped else ""))


def cmd_gui(args):
    from .gui import serve
    serve(args.container, args.port, not args.no_browser, args.force)


def build_parser():
    p = argparse.ArgumentParser(
        prog="wa-clean",
        description="Find and remove WhatsApp media on macOS.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("\n", 2)[2])
    p.add_argument("--version", action="version",
                   version="wa-clean %s" % __version__)
    p.add_argument("--container", default=CONTAINER, help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="show media size per chat")
    s.add_argument("--top", type=int, default=20, help="chats to list (20)")
    s.set_defaults(func=cmd_scan)

    c = sub.add_parser("clean", help="select media; preview or move to Trash")
    c.add_argument("--chat", action="append", metavar="NAME",
                   help="only chats whose name contains NAME, or an exact "
                        "chat ID (repeatable)")
    c.add_argument("--exclude-chat", action="append", metavar="NAME",
                   help="skip these chats (repeatable)")
    c.add_argument("--type", action="append", choices=TYPE_ORDER,
                   help="only this kind of media (repeatable)")
    c.add_argument("--older-than", type=parse_age, metavar="AGE",
                   help="only media older than AGE: 30d, 8w, 6m, 1y")
    c.add_argument("--larger-than", type=parse_size, metavar="SIZE",
                   help="only files at least SIZE: 800KB, 50MB, 1GB")
    c.add_argument("--unlinked", action="store_true",
                   help="only files no message points to")
    c.add_argument("--include-starred", action="store_true",
                   help="also remove media from starred messages")
    c.add_argument("--show", type=int, default=10, metavar="N",
                   help="largest files to list in the preview (10)")
    c.add_argument("--csv", metavar="FILE", help="write every match to a CSV")
    c.add_argument("--trash", action="store_true",
                   help="actually move the matches to the Trash")
    c.add_argument("--yes", action="store_true", help="don't ask to confirm")
    c.add_argument("--force", action="store_true",
                   help="run even while WhatsApp is open")
    c.set_defaults(func=cmd_clean)

    r = sub.add_parser("restore", help="put files from a cleanup back")
    r.add_argument("folder", help="the \"WhatsApp Cleanup …\" folder")
    r.add_argument("--force", action="store_true",
                   help="run even while WhatsApp is open")
    r.set_defaults(func=cmd_restore)

    g = sub.add_parser("gui", help="open the browser interface")
    g.add_argument("--port", type=int, default=8770, help="port (8770)")
    g.add_argument("--no-browser", action="store_true",
                   help="don't open the browser")
    g.add_argument("--force", action="store_true", help=argparse.SUPPRESS)
    g.set_defaults(func=cmd_gui)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
