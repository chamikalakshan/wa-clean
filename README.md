# wa-clean

**Find and safely remove WhatsApp media on macOS, without losing your chats.**

WhatsApp for Mac can quietly fill your disk with tens of gigabytes of photos,
videos and stickers. A lot of it is not even visible in the app: the Mac app
re-downloads media and keeps the old copies, which no message points to any
more. wa-clean finds those copies, shows you where your space went chat by
chat, and moves what you pick to the Trash, so you can always put it back.

- **Scan:** media size per chat, split into photos, videos, stickers, audio and documents
- **Unlinked copies:** files no message or status refers to, which WhatsApp can't show anyway
- **Filters:** by chat, type, age and size; starred messages are always kept
- **Safe by default:** previews first, moves to the Trash (never deletes), and can restore
- **Browser GUI** with thumbnails, or a command-line tool
- **No dependencies:** uses only the Python 3 that comes with macOS

> wa-clean is an independent project. It is not affiliated with, endorsed by or
> sponsored by WhatsApp or Meta. "WhatsApp" is a trademark of WhatsApp LLC.

## Install

You need macOS and the Mac App Store version of WhatsApp. Python 3.9 or newer
is already on your Mac (run `xcode-select --install` if `python3` is missing).

With [pipx](https://pipx.pypa.io):

```bash
pipx install git+https://github.com/chamikalakshan/wa-clean.git
```

Or run it straight from a clone:

```bash
git clone https://github.com/chamikalakshan/wa-clean.git
cd wa-clean
python3 -m wa_clean scan
```

(If you run from a clone, use `python3 -m wa_clean` wherever this README says `wa-clean`.)

## Use it

**Quit WhatsApp first** (⌘Q). wa-clean only reads while previewing, but it
refuses to move files while WhatsApp is open.

### Browser GUI

```bash
wa-clean gui
```

Your browser opens on a page that only your Mac can reach (127.0.0.1). Pick
filters on the left, check the preview and thumbnails, then press **Move to Trash**.

### Command line

```bash
wa-clean scan                                   # where is the space going?
wa-clean clean --unlinked                       # preview unlinked copies
wa-clean clean --unlinked --trash               # move them to the Trash
wa-clean clean --chat "Family" --type sticker   # one chat, one type
wa-clean clean --older-than 6m --type video --larger-than 20MB
wa-clean clean --unlinked --csv matches.csv     # full list for a spreadsheet
```

`clean` options:

| Option | Meaning |
|---|---|
| `--chat NAME` | Only chats whose name contains NAME, or an exact chat ID. Repeatable. |
| `--exclude-chat NAME` | Skip these chats. Repeatable. |
| `--type TYPE` | `photo`, `video`, `sticker`, `audio`, `document`, `thumbnail` or `other`. Repeatable. |
| `--older-than AGE` | `30d`, `8w`, `6m`, `1y` |
| `--larger-than SIZE` | `800KB`, `50MB`, `1GB` |
| `--unlinked` | Only files no message points to |
| `--include-starred` | Also remove media from starred messages (kept by default) |
| `--trash` | Actually move the matches. Without it, `clean` only previews. |

### Freeing the space, and undoing

Moved files go into a single folder, `WhatsApp Cleanup <date>`, which goes in
the Trash. **Empty the Trash** to get the space back.

Changed your mind? Drag that folder out of the Trash (or use Put Back), quit
WhatsApp, and run:

```bash
wa-clean restore ~/"WhatsApp Cleanup 2026-10-26 12.30.09"
```

The folder also contains `HOW-TO-RESTORE.txt` with the exact command.

## How "unlinked" is decided

WhatsApp names every media file after a random ID, like
`3f2a…-….jpg`. A file counts as **unlinked** only when its ID appears
nowhere in any of WhatsApp's databases or logs:

1. wa-clean copies `ChatStorage.sqlite` (it never opens the live file) and
   reads every media path, thumbnail path and the message each belongs to.
2. It then scans the raw bytes of **every** WhatsApp database and log for
   file IDs. This catches references in columns wa-clean doesn't know
   about, status updates, link previews and WhatsApp's own relinking jobs.
   Over-matching only makes it keep more, never remove more.
3. Files whose names aren't IDs are always kept.

### Safety checks

wa-clean refuses to move anything, in the CLI and the GUI, if:

- WhatsApp's chat database is missing or can't be read
- the database layout changed (for example after a WhatsApp update renamed a column)
- the raw scan misses media that the database itself lists, which would mean the
  unlinked check can't be trusted
- WhatsApp is running

It still lets you scan and preview, and tells you why moving is turned off.

**Back up first.** wa-clean only moves files to the Trash, but a Time Machine
backup before your first cleanup is cheap insurance.

## Compatibility

Tested with the native WhatsApp app from the Mac App Store on macOS 26, with
Python 3.9 (the system Python). The older Electron-based WhatsApp Desktop keeps
media elsewhere and isn't supported.

If wa-clean reports that WhatsApp's database layout has changed, please
[open an issue](https://github.com/chamikalakshan/wa-clean/issues) with your
WhatsApp and macOS versions.

## Development

```bash
python3 -m unittest discover -s tests -v
```

The tests build a small fake WhatsApp folder, so they never touch your real
data. They cover the unlinked check, filters, starred messages, the safety
checks, and moving to the Trash and back.

## License

[MIT](LICENSE)
