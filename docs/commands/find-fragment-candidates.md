# `find-fragment-candidates`

Reports the recurring fragments a library's names still carry — the release tags,
site names and encoder marks the individual cleaning pass would strip if it knew
about them — so they can be reviewed and added to `data/fragments.txt`.

    find-fragment-candidates [options] <folderOrTreeFile>

| At a glance | |
| --- | --- |
| **Takes** | a folder, or a tree file that already exists |
| **Writes** | `fragmentCandidates.txt` beside whichever it was given, or wherever `-o` says |
| **Input** | never touched: it reads nothing but names and writes nothing but its report |
| **Network** | none |

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
[missing tools](../requirements.md#missing-tools-are-refused-up-front)
