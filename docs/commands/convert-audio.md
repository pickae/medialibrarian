# `convert-audio`

Transcodes spoken-word audio to Opus at a low bitrate — or to xHE-AAC — carrying
over chapters and cover art.

    convert-audio [options] <inputDir> <outputDir>

| At a glance | |
| --- | --- |
| **Takes** | a folder of audio, and of video whose soundtrack is wanted |
| **Writes** | the converted files under `<outputDir>`; with `-c`, the rest copied verbatim beside them |
| **Input** | file extensions lower-cased and `.jpeg` renamed to `.jpg`; nothing else is changed |
| **Reruns** | an up-to-date output is skipped |
| **Network** | none |

## Default behavior

With no options, a run:

- writes **Opus at 46 kbps**, keeping the source's channel count (`-e`, `-b`, `-m`, `-a`)
- re-encodes only what is at or above 90 kbps, or in a format not worth keeping, and **leaves smaller files out of the output** ([below](#what-gets-re-encoded); `-c` copies them)
- **splits files longer than 10000 s** into chunks that encode in parallel (`-s`)
- carries chapters and cover art over, and copies images across
- **lower-cases file extensions in the input**, and renames `.jpeg` to `.jpg`
- runs one encoder per CPU thread (`-j`)

## What gets re-encoded

What gets re-encoded is decided per file:

| Input | What happens |
| --- | --- |
| above the bitrate threshold | re-encoded |
| a format the output should not keep at all (`m4a`/`m4b`/`mka`) | re-encoded |
| a video | its audio stream is extracted and converted like any other input |
| a video whose soundtrack is *already* a small enough Opus | stream-copied out of its container rather than encoded a second time into the same thing |
| anything else | small enough already: copied verbatim with `-c`, otherwise left alone |

**Long files** are split at quiet points into chunks that encode in parallel and
are transparently re-joined, with the original's metadata re-attached; `-s` sets
how long counts as long, and `0` turns it off.

## Channels and bitrate

| Mode | Channels | Bitrate |
| --- | --- | --- |
| default | as the source | 46 kbps |
| `-m` | forced mono | 32 kbps |
| `-b <kbps>` | — | that bitrate |
| `-a` (adaptive) | the source's own count, decided per file | the slightly higher spoken-word figure for that channel count, allowing for sound-effects. Cannot be combined with `-m` or `-b`, and turns long-file splitting off |

## xHE-AAC (`-e xheaac`)

xHE-AAC is the one output codec ffmpeg cannot produce — it decodes the codec and
has no encoder for it — so the audio goes out over a pipe to
[exhale](https://gitlab.com/ecodis/exhale), which reads WAVE on stdin and writes
a finished `.m4a`.

Two consequences worth knowing before using it:

- **A bitrate lands on the nearest preset.** exhale takes a *preset* about 12
  kbps apart rather than a bitrate, so a `-b` lands on the nearest rung and the
  run prints which — `-b 46` really encodes at 48.
- **Long books are always split.** One pass through the pipe cannot carry more
  than about 13½ hours of mono at 44.1 kHz, or half that in stereo, because WAVE
  states its length in 32 bits — so a book past that is split whatever `-s` says,
  chunking being the only way to encode it at all.

Long files split here as they do for Opus, and a re-joined book is its source's
length to the sample.

### Setting up xHE-AAC

No distribution packages exhale, so it has to be built from its
[repository](https://gitlab.com/ecodis/exhale). A run that asks for this codec
without it says where to build it from and offers `-e opus` instead.

## Every conversion is measured

Every conversion is measured afterwards, here and in the other commands that
transcode: an output that is not as long as its input is removed and named, and
the run ends non-zero — an encoder that stops early otherwise leaves a playable
file and a zero exit status.

---

**Options:** `convert-audio -h` lists every option and its default. **Shared
rules:** [file safety](../file-safety.md) ·
[output never inside the input](../file-safety.md#the-output-folder-must-not-sit-inside-the-input) ·
[stopping a run](../file-safety.md#stopping-a-run) ·
[missing tools](../requirements.md#missing-tools-are-refused-up-front) ·
[which ffmpeg](../requirements.md#which-ffmpeg-a-run-uses)
