# `generate-chapters`

Gives spoken-word audio and video chapter marks — start times and titles — that a
language model works out from a transcript of what is said.

    generate-chapters [options] <inputDir> <outputDir>

| At a glance | |
| --- | --- |
| **Takes** | a folder of audio and video |
| **Writes** | per input, under `<outputDir>` in the same sub-folders: `<name>.chapters.txt` (OGM chapter rows) and `<name>.srt`, the transcript |
| **Input** | never modified, unless `-w` writes the chapters into it, or beside it ([below](#into-the-files-themselves--w)) |
| **Reruns** | a chapter file already present is kept (`-f` makes it again); a transcript already present is reused |
| **Network** | the language model's server, wherever the settings point: on this machine or a hosted API |

## Two phases

1. **Transcribing.** Every input without a transcript goes through
   [`transcribe-audio`](transcribe-audio.md), and its
   transcript is kept in `<outputDir>`. A transcript some other recogniser wrote
   is used as it is: `<name>.srt`, `.vtt`, `.tsv`, `.json` (whisper's or the
   OpenAI transcription API's) or `.lrc`, in `<outputDir>` or beside the input.
2. **Chaptering.** The model reads each transcript and answers which lines
   chapters start on. All transcription is done before the model is asked
   anything, so whisper and a model on the same GPU never compete for it.

The model never writes a time. Every line it is shown has an id, it answers with
ids, and the times are the transcript's own. Each mark is then moved into the
pause before the first word.

## How the model is asked

A chapter list is a division of the whole recording, so it is made in three passes:

- **The outline** reads the whole transcript at once, as passages of about 20
  seconds, and divides all of it. It writes a short summary too. A transcript too
  long for the model's window (`llmContext`) is first summarised part by
  part, and the outline is drawn from those summaries. That way it still sees the
  whole recording, at a coarser grain.
- **The second look** takes each chapter start again, line by line, in the few
  minutes around it, with the whole outline and the summary beside it. It moves
  the start to the line where the new topic really begins, corrects a title that
  does not fit what is said there, or drops a start that turns out not to be a
  change of topic.
- **The review** reads the finished list - each chapter's start and title, your
  instructions, and no transcript - and rewords titles that disagree with one
  another or with the instructions: a pattern kept to the end, numbers in order.
  The titles the second look changed follow the list with the outline's titles,
  so the review can choose between them or combine them. It cannot move, add or
  drop a chapter, and a title it does not name stays as it was. It is one short
  request, skipped for fewer than three chapters.

A recording made of parts, such as several episodes in a row each with its own
segments, comes out as one flat list. Each part gets its own chapter where it
starts, and the titles say which part they belong to.

`-l` steers how long a chapter usually is (8 minutes by default) and `-m` sets the
shortest one allowed (2 minutes). A chapter shorter than that is merged into the
one before it. `-m` takes fractions of a minute, so `-m 0.5` allows chapters of
thirty seconds - for a recording whose structure has short parts, such as an
episode's opening before its first segment.

## Your own instructions

`-P` adds your own text to every request, for chapters cut or titled differently
from the default:

    generate-chapters -P "Let the chapters follow the speakers, and title each
    with the speaker's name and topic. Search online to make sure each speaker
    is named correctly." talks/ chapters/

Where your text and the built-in guidance disagree, yours wins. The answer's
form and the shortest chapter (`-m`) still apply.

A run with `-P` makes the model think harder. Over the `openai` protocol it
sends `reasoning_effort` set to `medium`, replacing whatever `llmOptions`
says for that field. A server that switches thinking off some other way has to
have that setting removed by hand. Claude runs at `medium` effort anyway, and
with `-P` it may also search the web, up to five searches a request. Anthropic
bills those searches. A server behind the `openai` protocol cannot search on
its own, so it works from the transcript alone - unless you have an agent
harness: with `llmHarness=opencode` and `llmHarnessModel` naming
the model as `opencode models` lists it, `-P` requests go to that model through
opencode, which can fetch web pages. Each request is a throwaway session,
deleted once its answer is read, so opencode's own session list stays yours.
Opencode runs with its permissions auto-approved for these requests.

A transcript does not say who is speaking, so the model works speakers out from
what they say: introductions, names, how they address each other.

## Into the files themselves (`-w`)

With `-w`, each input also gets its chapters written into it, in place and
without being re-encoded. Whatever chapters it held are replaced, and its other
tags, cover and streams are kept:

| Input | How |
| --- | --- |
| `.opus`, `.flac`, `.ogg` | the chapters written as tags, the file otherwise untouched |
| `.mka`, `.mkv`, `.webm` | mkvpropedit, the file otherwise untouched (needs mkvtoolnix) |
| `.mp3`, `.m4a`, `.m4b`, `.mp4` and other containers | an ffmpeg stream copy with the chapters added, put over the input only once it reads back with every chapter and is as long as the input |

A format with no place for chapters, such as raw `.aac` or `.wav`, is left as
it was and gets the chapter file beside it instead, as `<name>.chapters.txt`,
and the run goes on with the rest.
[`convert-audio`](convert-audio.md#a-chapter-file-beside-a-source) writes that
file into what it encodes from the recording. An input that cannot take its
chapters for any other reason is named and fails the run.

The chapter file is what goes in, so a chapter file already made is written in
without asking the model again, and needs no model at all. You can make the
chapters first, check or correct the chapter files, then run again with `-w`.
An input that already holds exactly those chapters is left alone.

## Which model

Any model behind one of two protocols, chosen with the
[settings](../settings.md) `llmApi`, `llmUrl`, `llmModel`
and `llmKey`:

| `llmApi` | Reaches |
| --- | --- |
| `openai` (default) | any server speaking OpenAI's chat protocol: Ollama, llama.cpp, vLLM, LM Studio and other local servers, OpenAI, Groq, Mistral, OpenRouter, Gemini's compatibility endpoint… |
| `anthropic` | Claude, through the `anthropic` Python package (`pip install anthropic`) |

For example, a local Ollama:

    llmUrl=http://localhost:11434/v1
    llmModel=qwen3:32b
    llmContext=32000

`llmContext` should be the window the server really runs the model with,
which is often smaller than the model allows. Ollama, for one, uses a small
window unless it is told otherwise. A server that lists its window at
`/v1/models` (`max_model_len`, as vLLM does) is held to the smaller of the two,
and unset, `llmContext` is the window it lists.

### Several servers, one card

`llmUrl` may list several servers, the preferred first. That suits a
card that holds one model at a time, where the model you would rather chapter
with is often not the one loaded:

    llmUrl=http://localhost:8081/v1,http://localhost:8080/v1
    llmModel=qwen3.6-35b-a3b
    llmServe=/path/to/start-the-35b-server
    llmServeMib=26000

Each recording is chaptered by the first server that can be had right then:

- A server that answers is used, and left running when the run ends.
- The first server, when it does not answer, is started with `llmServe`.
  This happens only if nvidia-smi shows `llmServeMib` free. Its output
  goes to a log in the scratch folder. A server started this way is stopped
  once nothing has asked it anything for five minutes, and when the run ends.
- When the card is taken, the next server that answers is asked, with
  the model it lists when it does not list `llmModel`.

`llmJobs` and `llmOptions` apply to whichever server is asked.
With no server to be had, a recording gets no chapters and the run goes on.

Whether the model is local or hosted is read off `llmUrl`. A local one
is localhost, a private or Tailscale address, a name without a dot, or a name
ending in `.local` or `.lan`. A local model shares the GPU with whisper, so all
the transcripts are made first. Then the files are chaptered one at a time, two
requests at once (`llmJobs`). A hosted model (no `llmUrl` for
`anthropic`, or a public address) is never queued for. Each file is chaptered
as soon as its transcript is there, while the rest are still being
transcribed, and every request goes out as soon as it is ready. Set
`llmJobs` to cap that, for an API with a rate limit.

The model does not have to reason. On a three-hour recording of seven
episodes, 28 chapters in all, a mixture-of-experts model with thinking turned off
(`llmOptions={"reasoning_effort": "none"}`, where the server takes that
field) found every chapter in 8 seconds. A dense reasoning model on the same
card found the same chapters in 3½ minutes. Letting the small model think at a
low effort made it slower and its titles worse.

A run with no usable model is refused before anything is transcribed.

---

**Options:** `generate-chapters -h` lists every option and its default. **Shared
rules:** [file safety](../file-safety.md) ·
[stopping a run](../file-safety.md#stopping-a-run) ·
[missing tools](../requirements.md#missing-tools-are-refused-up-front) ·
[what a run prints](../output.md) ·
[settings](../settings.md)
