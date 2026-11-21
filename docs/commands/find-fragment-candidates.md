# `find-fragment-candidates`

Reports the recurring fragments a library's names still carry — the release tags,
site names and encoder marks the individual cleaning pass would strip if it knew
about them — so they can be reviewed and added to `data/fragments.txt`.

    find-fragment-candidates [options] <folderOrTreeFile>

| At a glance | |
| --- | --- |
| **Takes** | a folder, or a tree file that already exists |
| **Writes** | the report, for a folder its `.tree` ([below](#default-behavior)), and when a language model classifies the candidates a separate proposal diff under `logs/` ([below](#classifying-candidates-with-a-language-model)) |
| **Input** | nothing is renamed: it reads names only, and never edits `data/fragments.txt` directly |
| **Network** | none, unless the run is given `-A`, `fragmentCandidatesLlm` is `on` and a hosted or web-searching model is configured |

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

## Classifying candidates with a language model

When the run is given `-A` and a language model is configured (see
[`docs/models.md`](../models.md), task `fragment-classify`) and `fragmentCandidatesLlm` is `on` (the default), the
candidates not already in `data/fragments.txt` — together with sample
folder/file names carrying each token and the existing sections of
`data/fragments.txt` — are sent to the model.

The model sorts each candidate into:

- `release_group` — scene, P2P, ripper or encoder group tags
- `quality_or_source` — resolution, codec, source or rip tags
- `publisher` — recurring publisher, platform or site watermark tags
- `title_word` — real title, series, creator or ordinary words (never proposed)
- `unsure` — ambiguous tokens (never proposed)

`data/fragments.txt` is **never edited directly**. Any proposed additions are
written as a separate proposal and unified diff file under `logs/`
(`logs/find-fragment-candidates-proposals-<timestamp>.diff`) for you to review
and merge. Every model decision (proposed and excluded) and the proposal file
path are printed in the run's output. Invented tokens, tokens already in
`data/fragments.txt`, `title_word` and `unsure` entries are never proposed.

Set `fragmentCandidatesLlm=off` (or pass `-x fragmentCandidatesLlm=off`) to
write the candidate report without asking a model.

---

**Options:** `find-fragment-candidates -h` lists every option and its default.
**Shared rules:** [file safety](../file-safety.md) ·
[missing tools](../requirements.md#missing-tools-are-refused-up-front) ·
[what a run prints](../output.md) ·
[settings](../settings.md)
