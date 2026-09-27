# `clean-folder-structure`

Applies the shared name cleaners in place across a nested folder tree. As a final
tidy-up it removes any sub-folders left empty (or already empty), keeping the
input root itself. It can also number the plurality filetype in each folder, sort
`YYYYMMDD` files into yearly subfolders, and simulate a whole run in a sandbox —
writing `before.tree` and `after.tree` into the input folder for risk-free review
without touching anything.

A phone mounted over MTP is handled automatically, and needs nothing set up on
the phone. Most mounts rename in place at metadata speed — an MTP rename sets an
object's name property, so no file content moves — and the folder is then cleaned
like any other. A mount that refuses renames is cleaned locally instead, with
only the resulting renames replayed on the device over `adb`, which does require
USB debugging. Which of the two applies is settled by trying a rename, not
assumed. The path may be given in either spelling the file manager offers — the
`mtp://…/SD%20card/…` URI it copies, or the `/run/user/…/gvfs/mtp:host=…` path
that URI stands for.

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

The fragments to remove are read from `data/fragments.txt` (one per line), or from
a file named with `-f`. Without either, names are cleaned without any fragment
removal.
[`find-fragment-candidates`](find-fragment-candidates.md) surfaces the recurring
leftovers in a library worth adding to that list.

Fragment removal and affix stripping are **word/number aware**: nothing is
removed when doing so would slice through the middle of a word or a number, so
`must be better` is never mangled by a `tt` fragment.
