# `concat-audio`

Concatenates the audio in each input subfolder into one output file per
subfolder, with chapters and an embedded cover.

    concat-audio [options] <inputDir> <outputDir>

| At a glance | |
| --- | --- |
| **Takes** | one subfolder per book, holding only one of mp3, opus, aac, m4a (incl. xHE-AAC) or flac — in folders of any depth |
| **Writes** | one audiobook file per subfolder into `<outputDir>` |
| **Input** | untouched, unless pretreatment (`-p`) is opted into, which renames folders and files in place |
| **Network** | none |

## Default behavior

With no options, a run:

- joins each subfolder's audio in name order into one book, **without re-encoding** (FLAC is re-encoded to FLAC); AAC and m4a books come out as `.m4b`
- passes over a subfolder holding more than one audio format
- takes chapters from a cue sheet, else makes one per file, and embeds a cover ([below](#chapters-and-cover))
- **never overwrites an existing book**: that subfolder fails instead
- leaves the input alone (`-p`)

The naming of the files and subfolders should reflect the order they are to be
joined in.

## Chapters and cover

Each is taken from the first source that has one:

| | First | Then | Last |
| --- | --- | --- | --- |
| **Chapters** | a `.cue` sheet — the only one, or else the one beside the audio sharing its name, or else the largest | one chapter per individual file | — |
| **Cover** | an image file in the input folder | the first page of a PDF booklet (needs `pdftoppm`) | the art embedded in the audio files |

Without mkvtoolnix an MP3 book still concatenates, but unchaptered.

---

**Options:** `concat-audio -h` lists every option and its default. **Shared
rules:** [file safety](../file-safety.md) ·
[output never inside the input](../file-safety.md#the-output-folder-must-not-sit-inside-the-input) ·
[stopping a run](../file-safety.md#stopping-a-run) ·
[missing tools](../requirements.md#missing-tools-are-refused-up-front) ·
[which ffmpeg](../requirements.md#which-ffmpeg-a-run-uses)
