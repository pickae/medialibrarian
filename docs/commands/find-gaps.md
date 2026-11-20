# `find-gaps`

Walks a folder tree and draws it the way `tree` does — except that each folder
holds what is **missing** from its numbered or dated run, not what is there.

    find-gaps [options] <inputDir>

| At a glance | |
| --- | --- |
| **Takes** | one folder, read at every depth |
| **Writes** | `logs/find-gaps/find-gaps-<folder>.tree`, one per folder given |
| **Input** | nothing is changed: it reads names only |
| **Reruns** | asks before replacing the report an earlier run left ([below](#reruns)) |
| **Network** | optional — when a web-searching model is configured and `findGapsLlm` is on, checks series and dated runs against real release lists ([below](#model-notes)) |

## What counts as a run

Each folder is looked at on its own, and only for the **kind of file it mostly
holds** — video, audio, image, comic, document, subtitle or archive. A cover
`.jpg`, a `.cue` or the `.srt` files beside a season are not part of the run, and
a book ripped partly to `.mp3` and partly to `.m4a` is still one run.

What the files are numbered by decides what is missing:

| Numbered by | Examples | Reported missing |
| --- | --- | --- |
| season and episode | `S01E01`, `s1e14`, `S02,E13`, `S01E01-E03`, `1x05` | every episode from 1 up to a season's highest, and every whole season from 1 up to the highest |
| a leading date | `20240105 …`, `2024-01-05 …` | the stretches with nothing in them that stand out for how often the run comes |
| a number | `1`, `01`, `001`, `Chapter 7`, `(03)` | every number from 1 up to the highest |

Numbers are read the way [`clean-folder-structure`](clean-folder-structure.md)
reads a numbered prefix, padding and all, so `3`, `03` and `003` are one number,
and text every name shares (`Chapter `) is looked past. A handful of numbers
that covers less than half of 1 to its highest — `1994`, `1999`, `2011` — is not
a run and reports nothing. A run that starts high but is dense between its own
ends (`51` to `90`) is missing everything before it as well — except years
(`1994` to `1999`) and one disc of disc-and-track numbers (`101` to `112`),
which report only the gaps inside them.

A run not starting at 1 is missing its start: `S01E03` onwards is missing
`S01E01-E02`, and a folder holding only season 3 is missing seasons 1 and 2 —
unless the folder is named for its season (`Season 3`, `Show S03 1080p`), in
which case the folder above it reports them. A single episode file is a run on
its own.

A dated run's cadence is the median distance between its dates:

| Cadence | Reported when nothing comes for |
| --- | --- |
| daily, or nearly (up to every 2 days) | 7 days or more |
| weekly (up to every 10 days) | 30 days or more |
| monthly (up to every 45 days) | 75 days or more — two months skipped, not one |
| slower | twice its cadence |

A run needs four distinct dates before its cadence is judged. The dated files in
a folder's year folders (`2023/`, `2024/`, as `clean-folder-structure -y` sorts
them) are one run with the folder's own, so a gap across New Year is found.

Sub-folders are a run of their own: `Season 1`, `Season 3` reports
`Season 2 (folder)`, a lone `Season 3` reports `Season 1 (folder)` and
`Season 2 (folder)`, and `01 …`, `03 …` reports `02 (folder)`.

## The report

```
/srv/media/lib
|-- Book/
|   |-- 03
|   `-- 06-08
|-- Podcast/
|   `-- 20231031-20231224 (nothing for 55 days, otherwise weekly)
`-- Show/
    |-- Season 2 (folder)
    `-- Season 1/
        `-- S01E03-E04

4 folder(s) with gaps, 5 missing, 1 date gap(s)
```

The first line is the folder given. Only folders with something missing at or
below them are drawn; a folder carries a trailing `/`, as `tree -F` marks one,
so it cannot be mistaken for a missing number. Consecutive missing numbers are
one line, as a range. Deterministic numbering alone cannot know where the end
of a run was meant to be — that is where optional model notes help.

## Model notes

When a language model that can search the web is set up (the `gaps-check` task
in [`docs/models.md`](../models.md)) and `findGapsLlm` is `on` (the default),
`find-gaps` sends a concise summary of the detected numbered, episodic and
dated runs (folder, series and episode names, number/date ranges and the
deterministic gaps already found) so the model can look up real episode lists
and release schedules.

The model pass is strictly report-only: it never modifies files and never
removes deterministic gaps from the tree above. Validated findings are
appended in a separate `Model notes` section — flagging suspected missing
trailing/final episodes, marking scheduled podcast breaks or hiatuses, noting
genuinely short seasons or missing specials, and confirming real gaps with a
source. Set `findGapsLlm=off` (or `-x findGapsLlm=off`) to skip this step and
keep the run completely offline.

## Reruns

The report is named after the folder given, so a second run over it would
replace the first. When the report is already there, the run
[asks before overwriting it](../output.md#overwriting-a-report).

---

**Options:** `find-gaps -h` lists every option and its default.
**Shared rules:** [file safety](../file-safety.md) ·
[what a run prints](../output.md) ·
[settings](../settings.md)
