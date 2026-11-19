# `clean-folder-structure-adb`

Cleans the names in a folder on an Android phone over `adb`, the way
[`clean-folder-structure`](clean-folder-structure.md) cleans a local tree, for a
phone whose MTP mount refuses renames.

    clean-folder-structure-adb [options] <inputPath>

| At a glance | |
| --- | --- |
| **Takes** | a folder on the phone: its on-device path (`/sdcard/…`), or the `/run/user/…/gvfs/mtp:host=…` path of its MTP mount |
| **Writes** | renames on the phone; with `-p`, nothing |
| **Input** | renamed in place — that is the job |
| **Reruns** | a name already in its target form is left alone |
| **Network** | none; the phone is reached over USB |

`clean-folder-structure` runs this by itself when it meets such a mount, so it
is only needed by name to work from an on-device path.

## Default behavior

With no options, a run:

- needs `adb`, exactly one attached phone, and USB debugging switched on
- **renames folders and files on the phone**, at every level below the folder given, with an `adb shell mv`: no file content is read, rewritten or sent over the cable
- removes the fragments in `data/fragments.txt` in the medialib data directory (`-f`)
- removes sub-folders the cleaning left empty
- skips any rename whose target already exists, with a warning
- fixes no dates, makes no year folders and numbers nothing (`-d`, `-y`, `-n`)

## How it works

The phone's tree is listed once and mirrored locally, and the mirror is cleaned
by `clean-folder-structure` itself, so the names come out exactly as they would
on a local disk. Only the renames that cleaning made are then replayed on the
phone, in sorted order, each printed as it goes.

| Option | Effect |
| --- | --- |
| `-d`, `-y`, `-n`, `-f <file>` | as for [`clean-folder-structure`](clean-folder-structure.md#optional-passes) |
| `-p` | prints the planned renames and touches nothing on the phone |

An MTP path is translated to the on-device one by its storage's label: a label
that names the SD card is only looked for on the removable volumes, and one that
names internal storage only there, so a folder on one is never mistaken for a
folder of the same name on the other.

Stopping a run partway leaves the renames already replayed in place on the
phone; the closing line says how many went through of how many were planned.

---

**Options:** `clean-folder-structure-adb -h` lists every option and its default.
**Shared rules:** [file safety](../file-safety.md) ·
[stopping a run](../file-safety.md#stopping-a-run) ·
[missing tools](../requirements.md#missing-tools-are-refused-up-front) ·
[what a run prints](../output.md) ·
[settings](../settings.md)
