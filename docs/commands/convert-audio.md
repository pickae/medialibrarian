# `convert-audio`

Transcodes spoken-word audio to Opus at a low bitrate — or to AAC or xHE-AAC —
carrying over chapters and cover art.

    convert-audio [options] <inputDir> <outputDir>

| At a glance | |
| --- | --- |
| **Takes** | a folder of audio, and of video whose soundtrack is wanted |
| **Writes** | the converted files under `<outputDir>`; with `-c`, the rest copied verbatim beside them |
| **Input** | file extensions lower-cased and `.jpeg` renamed to `.jpg`; nothing else is changed |
| **Reruns** | an up-to-date output is skipped |
| **Network** | none, except the language model's server under `-C` |

## Default behavior

With no options, a run:

- writes **Opus at 46 kbps**, keeping the source's channel count (`-e`, `-b`, `-m`, `-a`)
- re-encodes only what is at or above 90 kbps (50 kbps in a mono run; `-T`), or in a format not worth keeping, and **leaves smaller files out of the output** ([below](#what-gets-re-encoded); `-c` copies them)
- **splits files longer than 10000 s** into chunks that encode in parallel (`-s`)
- carries chapters and cover art over, and copies images across ([below](#cover-art))
- writes a transcript found beside a source into its output ([below](#transcripts))
- **lower-cases file extensions in the input**, and renames `.jpeg` to `.jpg`
- runs one encoder per CPU thread (`-j`)

## What gets re-encoded

What gets re-encoded is decided per file:

| Input | What happens |
| --- | --- |
| at or above the re-encode threshold (90 kbps, 50 kbps in a mono run, or what `-T` names) | re-encoded |
| `mka`, a container `concat-audio` cannot join - a book of them copied through `convert-and-concat` would fail at the join | re-encoded |
| a video | its audio stream is extracted and converted like any other input |
| a video whose soundtrack is *already* a small enough Opus | stream-copied out of its container rather than encoded a second time into the same thing |
| anything else | small enough already: copied verbatim with `-c`, otherwise left alone |

**Long files** are split at quiet points into chunks that encode in parallel and
are transparently re-joined, with the original's metadata re-attached. Whether a
run splits anything, and which files, it decides for itself from the file sizes:
it splits only when that is expected to finish the whole run sooner than
encoding every file whole, and prints what it expects to gain when it does.

## The re-encode threshold (`-T`)

The threshold is tuned to the 46 kbps Opus default - 90 kbps, or 50 kbps in a
mono run - and is wrong for a run that encodes at another rate: a `-b 120` run
judged against 90 re-encodes what is already at the rate it would encode at.
`-T` moves it for the whole run, judging every file of it against the value,
stereo and mono alike, and the `-g` folder verdicts read against it as well.

## Channels and bitrate

| Mode | Channels | Bitrate |
| --- | --- | --- |
| default | as the source, but at most stereo — a surround source is downmixed | 46 kbps |
| `-m` | forced mono | 32 kbps |
| `-s` (surround) | the source's own count, up to 7.1 | 46 kbps scaled up with the channel count (106 for 5.1) |
| `-b <kbps>` | — | that bitrate |
| `-a` (adaptive) | the source's own count, decided per file, capped at stereo unless `-s` | the slightly higher spoken-word figure for that channel count, allowing for sound-effects. Cannot be combined with `-m` or `-b`, and turns long-file splitting off |

The output is meant for a phone, so stereo is the ceiling unless `-s` asks for
more. With `-s`, Opus carries surround in only one fixed layout per channel
count, so a source in any other layout (the 5.1(side) that EAC3 decodes to, or
7.1(wide)) is re-labelled onto it. A 2.1 or 3.1 source is still downmixed to
stereo rather than have its LFE played from a full-range speaker, and one wider
than 7.1 is folded down to 7.1.

The table is the Opus one: `-e aac` runs at 64 kbps by default and 48 kbps in a
forced mono run, and an AAC figure is not scaled up with the channel count the
Opus one is — a `-s` run of it stays at the rate the codec needs of speech.

## AAC (`-e aac`)

AAC-LC in a `.m4a`, which ffmpeg encodes itself, so there is no external
encoder to set up. Its chapters are a track in the `.m4a` rather than a tag,
exactly as for xHE-AAC, and a book of its outputs through
[`convert-and-concat`](convert-and-concat.md) joins into one `.m4b` the same
way. Long files split and re-join as they do for any other codec.

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
  states its length in 32 bits — so a book past that is always split, chunking
  being the only way to encode it at all.

Long files split here as they do for Opus, and a re-joined book is its source's
length to the sample.

### Setting up xHE-AAC

No distribution packages exhale, so it has to be built from its
[repository](https://gitlab.com/ecodis/exhale). A run that asks for this codec
without it says where to build it from and offers `-e opus` instead.

## Transcripts

A transcript beside a source, named after it (`<name>.srt`, `.vtt` or `.lrc`,
or with a language code in front: `<name>.en.srt`), is written into the output.
An Opus or FLAC file takes it as timed lyrics (LRC in the `LYRICS` comment),
which audio players show in step with the playback. Ogg has no subtitle stream
ffmpeg can write, so lyrics are how a transcript fits in an Opus file without
switching it to Matroska. An `.m4a` takes it as a subtitle track. An output
that carries a transcript already is left as it is.

## Chapters for what has none (`-C`)

With `-C`, every output whose source has no chapters gets some, worked out by a
language model from a transcript of the speech, exactly as
[`generate-chapters`](generate-chapters.md) does it and with the same
settings. The transcript is the sidecar where there is one. Otherwise the
outputs are transcribed by `transcribe-audio` once all the encoding is done, and
that transcript goes into the output with the chapters. A source with chapters
of its own keeps them and is not sent to the model. A rerun tries again only for
outputs that still have none. Without a usable model, `-C` is refused before
anything is encoded.

`-P "<text>"` adds your own instructions for the model, as
[`generate-chapters -P`](generate-chapters.md#your-own-instructions) does; it needs `-C`.

## Some folders of the input (`-O`)

`-O <folder>` converts only what is under `<inputDir>/<folder>`, into the
place a run over all of `<inputDir>` would put it. It may be given more than
once.

## Cover art

A converted file keeps the cover its source carries. An image beside it with
the same name (`track.jpg` / `track.webp`) replaces that cover. A folder image
such as `cover.jpg` or `folder.jpg` is copied across but **not** embedded: an
album's tracks would each carry their own copy of it, and for music
`ingest-music` already leaves the folder image players read.

A track with no cover is looked for **online** only when it is the odd one out:

| Searched | Not searched |
| --- | --- |
| at least 30 minutes long, or named with a leading `yyyymmdd` date or a trailing `[YouTube id]` | shorter, undated and without an id (album-style tracks) |
| no image in its folder | a folder image is there |
| most of the other tracks in its folder carry a cover, or it is alone | most of them carry none (the album case) |

The search works the way [concat-audio's](concat-audio.md#chapters-and-cover)
does: a dated name or 30–200 minutes reads as a podcast, longer as an
audiobook, and an answer is taken only when it is the same title. One
difference: 30–200 minutes encoded in **stereo** (not `-m`, and not a mono
source under `-a`) reads as a full-cast drama, and is looked for where
audiobooks are. The
`coverSearch` [setting](../settings.md) switches it off.

## Every conversion is measured

Every conversion is measured afterwards: an output that is not as long as its input is removed and named, and
the run ends non-zero — an encoder that stops early otherwise leaves a playable
file and a zero exit status.

---

**Options:** `convert-audio -h` lists every option and its default. **Shared
rules:** [file safety](../file-safety.md) ·
[output never inside the input](../file-safety.md#the-output-folder-must-not-sit-inside-the-input) ·
[stopping a run](../file-safety.md#stopping-a-run) ·
[missing tools](../requirements.md#missing-tools-are-refused-up-front) ·
[which ffmpeg](../requirements.md#which-ffmpeg-a-run-uses) ·
[what a run prints](../output.md) ·
[settings](../settings.md)
