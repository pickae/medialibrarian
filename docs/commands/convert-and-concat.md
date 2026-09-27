# `convert-and-concat`

Wrapper that ingests (`convert-audio`) and then concatenates
(`concat-audio`), keeping the intermediate tree entirely in RAM, so only
the finished books are ever written to disk — which is why the output folder is
given rather than derived: it usually lives on a different disk than the source.
`-e` picks the codec that tree is written in and is handed straight to
`convert-audio`: Opus by default, or xHE-AAC as `.m4a`, which the concatenating
phase then joins into an `.m4b`.

A book may arrive as an **archive instead of a folder**. Wherever the run expects a
folder of tracks — the input's subfolders, or theirs with `-s` — a `.zip`, `.rar`,
`.7z`, `.tar` or compressed tar lying there counts as one: it is unpacked into RAM
and ingested from there like a folder. The archive itself is never touched, and
only the extractors the input actually needs are required.

- The unpacked folder is named for the **one folder the archive holds at its
  root** — `dl-4471.zip` holding `Some Book/…` becomes `Some Book` — and for the
  archive file minus its suffix when the root holds anything else: several
  folders, files, or files beside a folder. A download renamed on its way here
  still carries the name it was packed under.
- An archive beside a **folder of that name** is left alone: that folder is
  taken to be it, already unpacked, and possibly corrected since.
- Of two archives claiming one name (a `.zip` and a `.rar` of one book), the first
  stands in for it and the other is passed over, so the two are never mixed into a
  single book.
- The redundant folder most archives carry (`Some Book.zip` holding `Some Book/…`)
  is dropped, so the tracks end up where they would have been unpacked by hand.
- An archive is read before it is unpacked, and one asking to write **outside its
  own folder** — a member spelled `/etc/…` or `../…`, or a link pointing out of
  the tree — is skipped whole rather than unpacked and tidied up afterwards. The
  same applies to `.cbz`/`.cbr`/`.cb7` in `convert-comics`.
