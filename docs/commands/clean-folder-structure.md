# `clean-folder-structure`

Applies the shared name cleaners in place across a nested folder tree, then
removes any sub-folders left empty (or already empty), keeping the input root
itself.

    clean-folder-structure [options] <inputDir>

| At a glance | |
| --- | --- |
| **Takes** | a folder tree, or a phone mounted over MTP |
| **Writes** | renames in place; with `-p`, only `before.tree` and `after.tree` |
| **Input** | renamed in place — that is the job |
| **Reruns** | a name already in its target form is left alone |
| **Network** | none |

## Default behavior

With no options, a run:

- **renames folders and files in place**, at every level below the folder given
- removes the configured fragments ([below](#fragments); `-f`)
- **renames the cover image in each folder to `folder.<ext>`**: the largest one with `folder`, `front` or `cover` in its name
- **removes sub-folders left empty**, keeping the folder given
- refuses any rename that would overwrite something
- fixes no dates, makes no year folders and numbers nothing (`-d`, `-y`, `-n`)

## Optional passes

| Option | Adds |
| --- | --- |
| `-d` | normalises a leading `YYYY-MM-DD`-style date into the compact `YYYYMMDD ` form first |
| `-y` | sorts `YYYYMMDD` files into yearly subfolders |
| `-n` | numbers the plurality filetype in each folder instead of cleaning file names |
| `-f <file>` | reads the fragments to remove from `<file>` ([below](#fragments)) |
| `-p` | simulates the whole run in a sandbox and writes `before.tree` and `after.tree` into the input folder for risk-free review, without touching anything. Combines with the others |

## How names are cleaned

Renaming itself is done in three phases, reused by the comic and audio pipelines
too:

1. **Individual** — clean each name on its own, normalise separators and
   punctuation, remove configured fragments, and split off a leading date/number
   prefix.
2. **Collective** — remove the longest leading/trailing affix a group of sibling
   names has in common, without cutting through a word or number and without
   unbalancing brackets.
3. **Hidden-prefix recovery** — restore prefixes the collective pass would
   otherwise have hidden.

Fragment removal and affix stripping are **word/number aware**: nothing is
removed when doing so would slice through the middle of a word or a number, so
`must be better` is never mangled by a `tt` fragment.

### Fragments

The fragments to remove are read from `data/fragments.txt` (one per line), or from
a file named with `-f`. Without either, names are cleaned without any fragment
removal. [`find-fragment-candidates`](find-fragment-candidates.md) surfaces the
recurring leftovers in a library worth adding to that list.

## Phones over MTP

A phone mounted over MTP is handled automatically, and needs nothing set up on
the phone. Which of the two ways applies is settled by trying a rename, not
assumed:

| The mount | How it is cleaned |
| --- | --- |
| renames in place (most) | at metadata speed — an MTP rename sets an object's name property, so no file content moves — and the folder is then cleaned like any other |
| refuses renames | cleaned locally instead, with only the resulting renames replayed on the device over `adb`, which does require USB debugging |

The path may be given in either spelling the file manager offers — the
`mtp://…/SD%20card/…` URI it copies, or the `/run/user/…/gvfs/mtp:host=…` path
that URI stands for.

---

**Options:** `clean-folder-structure -h` lists every option and its default.
**Shared rules:** [file safety](../file-safety.md) ·
[stopping a run](../file-safety.md#stopping-a-run) ·
[missing tools](../requirements.md#missing-tools-are-refused-up-front) ·
[what a run prints](../output.md)
