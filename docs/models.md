# Language models

Several commands ask a language model for something: chapters from a
transcript (`generate-chapters`, `convert-audio -C`, `convert-video -C`,
`ingest-movies`, `ytdlp`), host-read ads in RSS and site podcasts (`ytdlp`),
a video's encoding profile from sampled frames (`convert-video -p auto`),
picking a book's front cover among unnamed folder images, searching for a
cover where nothing else found one (`concat-audio`, `convert-audio`), a
book's chapter titles where its file names only number them, or a joined
book's empty metadata tags (`concat-audio`), reading picture subtitles into
text (`ingest-movies`), the MusicBrainz release of an album beets skipped
(`ingest-music`), classifying recurring filename fragment candidates
(`find-fragment-candidates`), web-checked gap notes for numbered and dated
runs (`find-gaps`), report-only match suggestions for unmatched or ambiguous
Letterboxd entries (`plex-letterboxd-sync`), an e-book's language where its
metadata and text disagree or neither gives a confident answer
(`read-library`), classifying e-book images as book content versus
advertising (`ingest-books -d`), an advisory likely cause and next step for
unexplained errors left in a run's error log (`error-triage`, controlled by
`errorTriageLlm`), checking whether a small comic page has real content
worth keeping (`convert-comics`), or whether a borderline PDF is a comic book
(`convert-comics`, `content-census`). Each of those is a **task**, and each
task picks from the models you have, by what it needs and what it would
rather have.

## Your models

The simplest setup is one model, given with the `llm…`
[settings](settings.md) - `llmUrl`, `llmModel` and the rest. With more than
one, list them in the **roster file**, `models.toml` beside the settings file
(or wherever `llmModels` points). One table per model, in your order of
preference; [`models.example.toml`](../medialib/config/models.example.toml)
is a commented example:

```toml
[models.qwen-local]
url = "http://localhost:8081/v1"
model = "qwen3.6-35b-a3b"
serve = "llama-server -m /models/qwen3.6-35b-a3b.gguf --port 8081"
harness = "opencode"
harness_model = "local/qwen3.6-35b-a3b"

[models.vision-local]
url = "http://localhost:8082/v1"
model = "gemma3-12b"

[models.claude]
api = "anthropic"
model = "claude-opus-5-5"
traits = { cost_in = 5, cost_out = 25 }
```

A model takes the fields the single-model settings give (`api`, `url`,
`model`, `key`, `context`, `options`, `jobs`, `serve`, `serve_mib`,
`harness`, `harness_model`, `traits`), plus:

- `key_env` - the environment variable holding its key, so the file holds
  none. A file that holds a `key` must be readable by you alone (`chmod 600`).
- `tasks` - the tasks it may be used for, where not all.

A model set with the single-model settings joins the roster as `llm`, after
the file's.

## What a model is

What a task looks at is the model's **traits**:

| Trait | Meaning |
| --- | --- |
| `tools` | it calls functions (tool calling) |
| `web` | it can search the web: Claude can, and any other model through an agent harness (`harness`) |
| `vision` | it can be shown pictures |
| `reasoning` | it thinks before it answers |
| `images_out` | it makes pictures (no task asks for this yet) |
| `local` / `cloud` | it runs on this machine or its network, or is hosted. A local model shares one GPU, so its requests wait their turn (`jobs`, 2 at a time unless set); a cloud API is serverless, so every request goes the moment it is ready. Read off the address; declare `cloud` for a proxy on this machine in front of a hosted API |
| `moe` | a mixture of experts: only `active` of its `params` billion are read for each token, so it runs like a small model and knows like a large one |
| `small` / `medium` / `large` | its size: up to 15 billion parameters, up to 80, more |
| `fast` / `slow` | its speed: a local model reading up to 15 billion parameters a token is fast; a hosted one is fast when it is a small one |
| `cheap` | asking it costs next to nothing: every local model, and a hosted one writing a million tokens for $2 or less |

Each is found the surest way there is:

1. what you declared, under `traits` (or `llmTraits` for the single model);
2. else what a local server says of the model it holds - llama.cpp's
   `/props`, Ollama's `/api/show`;
3. else what the model's name says: `Qwen3-VL-30B-A3B` is a 30-billion-
   parameter mixture of experts with 3 billion active that sees pictures.

The names follow the open model catalogues, so an entry can be copied from
one: [models.dev](https://models.dev) (which opencode reads) has `tool_call`,
`reasoning`, `attachment` and `cost`; LiteLLM's model map has
`supports_function_calling`, `supports_vision` and `supports_reasoning`. Both
spellings are read. A declaration in full:

```toml
traits = { vision = true, tools = true, reasoning = false, moe = true,
           params = 30, active = 3, tok_s = 90, cost_in = 0, cost_out = 0,
           size = "medium", speed = "fast", cheap = true }
```

Run with `-v` to see which model each task took, and what it was taken for.

## Tasks

| Task | Needs | Prefers | Leans |
| --- | --- | --- | --- |
| `chapters` | - | reasoning | balanced |
| `chapters-instructed` (chapters with your `-P` instructions) | - | web, reasoning | precise |
| `cover-search` | web | reasoning | balanced |
| `cover-look` (picking among web results or unnamed folder images) | vision | - | light |
| `contents-search` | web | reasoning | balanced |
| `subtitle-ocr` | vision | local, cheap | light |
| `video-profile` (`convert-video -p auto`) | vision | - | light |
| `music-search` | web | reasoning | balanced |
| `podcast-ads` | - | reasoning | balanced |
| `fragment-classify` | - | reasoning, web | balanced |
| `gaps-check` | web | reasoning | balanced |
| `letterboxd-match` | - | reasoning, web | balanced |
| `book-language` | - | - | light |
| `book-image-check` | vision | - | light |
| `error-triage` (unexplained errors in a run's error log) | - | reasoning, local | balanced |
| `comic-page-check` | vision | - | light |
| `comic-pdf-judge` | vision | - | light |
| `book-tags` | web | reasoning | balanced |

`error-triage` prefers a local model because error logs carry library paths,
and is strictly advisory: it never retries commands, deletes files or changes
a run's exit code (turned off with `errorTriageLlm=off`).

A task's models, best first:

1. those without what it **needs** are left out;
2. those with more of what it **prefers** come first;
3. then its **lean**: `light` takes the cheapest, fastest, smallest first;
   `precise` the largest, then the ones that reason; `balanced` changes
   nothing;
4. where all that is even, your order decides.

A task that can do without what it prefers still runs on a model that lacks
it: chapters with instructions are written without web search when no model
can search. A task that needs something no model has is skipped (the cover
search, `convert-audio`'s per-track cover search whenever no vision model is
available for `cover-look`, the music release lookup, the comic empty-page
check, and `book-image-check`, which falls back to the fixed junk-name
substrings) or refused before any work (the chapter commands and
`convert-video -p auto`), saying which trait was missing.

Overriding it:

```toml
[tasks.chapters]
lean = "light"                        # small, cheap and fast first

[tasks.cover-search]
models = ["claude", "qwen-local"]     # this ladder, in this order
```

`llmLean` sets the lean of every task the file does not.
