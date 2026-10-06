# `find-fragment-candidates`

Reports the recurring fragments a library's names still carry — the release tags,
site names and encoder marks the individual cleaning pass would strip if it knew
about them — so they can be reviewed and added to `data/fragments.txt`.

    find-fragment-candidates [options] <folderOrTreeFile>

| At a glance | |
| --- | --- |
| **Takes** | a folder, or a tree file that already exists |
| **Writes** | the report, and for a folder its `.tree` ([below](#default-behavior)) |
| **Input** | nothing is renamed: it reads names only |
| **Network** | none |

## Default behavior

With no options, a run:

- for a folder, **writes `<folder>.tree` into it** (needs `tree`), then reads that
- writes the report **into the folder** as `fragmentCandidates.txt`, or beside a tree file as `<stem>.fragmentCandidates.txt`, overwriting an older one (`-o`)
- reports only fragments at least 2 distinct names carry (`-m`)

| Given | What is parsed |
| --- | --- |
| a folder | a tree of that folder, generated first (needs `tree`) |
| a tree file | that file, directly |

`-m` is the prevalence floor: how many distinct names have to carry a candidate
before it is worth reporting. The default of 2 hides one-off title words, which
is most of what a first run would otherwise show; `-m 1` lists every candidate.

The fragments it finds are what [`clean-folder-structure`](clean-folder-structure.md#fragments)
and `ingest-movies` remove.

---

**Options:** `find-fragment-candidates -h` lists every option and its default.
**Shared rules:** [file safety](../file-safety.md) ·
[missing tools](../requirements.md#missing-tools-are-refused-up-front) ·
[what a run prints](../output.md)
