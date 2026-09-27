# `transcribe-audio`

Transcribes a folder of audio and video with whisper, one transcript per input,
in a mirrored output folder.

    transcribe-audio [options] <inputDir> <outputDir>

| At a glance | |
| --- | --- |
| **Takes** | a folder of audio and video |
| **Writes** | one transcript per input under `<outputDir>`: the same sub-folder structure, a new extension |
| **Input** | never modified |
| **Reruns** | a transcript already present in the output is left in place, so only what is new is done |
| **Network** | none, beyond whisper fetching a model the first time it is used |

| Input | Transcribed from | Becomes |
| --- | --- | --- |
| `<input>/a/track.mp3` | the audio as it is | `<output>/a/track.txt` |
| `<input>/b/movie.mkv` | its **first audio track only** — the video stream and any further audio tracks are ignored | `<output>/b/movie.txt` |
| a video with no audio track | — | skipped, with a warning |

The whisper model is settled for the host once, up front: the GPU when whisper can
really run on it (the biggest model the free VRAM holds), otherwise the CPU. The
format (`-f`: `txt`, `srt`, `vtt` or `tsv`) defaults to `txt`, and `-j` sets how
many transcriptions run at a time.

---

**Options:** `transcribe-audio -h` lists every option and its default. **Shared
rules:** [file safety](../file-safety.md) ·
[stopping a run](../file-safety.md#stopping-a-run) ·
[missing tools](../requirements.md#missing-tools-are-refused-up-front) ·
[which ffmpeg](../requirements.md#which-ffmpeg-a-run-uses)
