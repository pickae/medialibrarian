# Test tiers (what needs real media, and what does not)

The suite keeps to **four tiers** along one axis: how much of the world is real.
They are pytest markers, not directories, so a test that is mostly pure with one
disk assertion does not have to pick a side, and the tree mirrors the code under
test instead.

| Marker | Disk | File contents | External tools |
| --- | --- | --- | --- |
| `pure` | no | — | none |
| `fs` | real directories | empty (`touch`ed) | none |
| `stubbed` | real directories | real, small | a recording stub on `PATH` |
| `media` | real directories | real media | the real heavy tools |

**Operationally there is one gate: real media or not.** `pure` / `fs` /
`stubbed` are authoring distinctions — nobody runs "only the tests that avoid
disk" — and all three are in the default run. `media` encodes video and needs a
machine's worth of tools, so it is opt-in and never runs by accident.

The `media` fixture is *generated, not committed*: ffmpeg synthesises a real
HDR10 clip (10-bit, PQ, BT.2020, mastering-display + MaxCLL) in a fraction of a
second, and 24 hand-written container bytes make a clip *claim* Dolby Vision it
does not have. The one committed fixture is the subtitle pairing in
`tests/data/subtitleSync/`, where the cue timings are real but every line of text is replaced by `line N` — timings are all ffsubsync looks at, and
synthetic ones alias.

## Running the suite

```bash
pytest                    # everything but media
pytest -n auto            # the same, across every core - what CI runs
pytest -m media           # the real-tool tier: ffmpeg, mkvmerge, dovi_tool, ...
pytest -m 'fs or pure'    # one tier, when narrowing something down
```

`-m 'not media'` is the default, set in `pyproject.toml`, so a plain `pytest`
can never start encoding video.

Two rules keep the suite safe to run concurrently, and neither is left to
review: `tests/conftest.py` points every environment knob that names a place to
work at the test's own directory, and `tests/test_self_containment.py` walks the
syntax tree of every test file and fails one that creates something under a
shared root.

`-n auto` needs `pytest-xdist`, and it is worth it: most of the wall clock is
starting child processes — the ~1460 cases under `tests/cli` each start a
command — which is the shape that parallelises best. It is what all three test
jobs in CI run, and the macOS one is where it counts, that being the host where
starting a process is dearest.

## A green run is a whole run

A runner counts what it discovers, so "all passed" does not by itself say the
suite ran. Two questions are asked of a whole-suite run: every `test_*.py` under
`tests/` has to hand over at least one case, and the total has a floor under it
for the files that are gone from disk entirely.

`tests/conftest.py` asks them at collection time and *refuses* — the run stops
with an `ERROR:` naming what is missing, before anything executes. Under `-n` it
cannot: that hook runs in the workers and never on the controller, and a worker
that raises there disagrees with its siblings about what was collected, which
xdist reports as an `INTERNALERROR` naming an unrelated case. The controller
cannot stand in, because the ids xdist offers it have already been through `-m`,
so the media-only files would read as missing ones.

So `tests/test_suite_is_whole.py` asks the same two questions as a *case*,
sharing conftest's wording. That holds however the run was started; what it
gives up is stopping first, which is why the refusal stays where it can be had.
Serially both are live and the refusal simply gets there first.

All of it is skipped when you ask for part of the suite by path
(`pytest tests/lib`), and none of it is affected by `-k` or `-m`, which narrow
what runs rather than what is collected.

## Where things are

```
tests/
  conftest.py      the isolation every test gets
  blackbox.py      starting a command as a child, and stubbing the heavy tools
  test_*.py        what is about the package as a whole
  cli/  lib/       one module per module under test
  data/            what the tests READ: the 180 recorded pages under
                   cliContract/, the cue, name-cleaning and subtitle fixtures,
                   the shared tool stub, and the Dolby Vision claim injector
```

The package ships without any of it: `medialib/` is the wheel, `tests/` is the
repository.

## Host prerequisites

The suite assumes the Linux environment the commands themselves target — GNU
coreutils/findutils, a tmpfs at `/dev/shm`, and a UTF-8 locale (pinned to
`C.UTF-8` where available). On top of that:

- the default run needs only small tools: `tree`, `jq`, `zip`/`unzip`, `iconv`,
  `md5sum`, `realpath`, `xargs`, plus the `flock` the commands under test use for
  their own progress counters. No real media and no heavy external tool.
- `-m media` needs the real ones: `ffmpeg`/`ffprobe` built with `libx265`,
  `mkvmerge`, `mediainfo`, `dovi_tool`, `ffsubsync`, `mutagen`. **`mkvmerge`
  cannot be substituted:** a raw HEVC elementary stream carries no timestamps,
  so ffmpeg's Matroska muxer refuses it and writes a corrupt file, with or
  without `-r` / `-fflags +genpts`.

A missing prerequisite fails the test that needs it rather than being skipped
quietly, so a green run always means the suite really ran.

### Off Linux

**macOS** is a POSIX host, so the whole default run is expected to work there —
bash stubs, symlinks, signals and all — and CI runs it (`macos` in
`.github/workflows/ci.yml`, reporting rather than gating until it has been green
once). What it does *not* have is glibc, and three cases turn on that: the
`C.UTF-8` locale, which `conftest.py` assembles from `LC_COLLATE=C` and a UTF-8
`LC_CTYPE` instead; `iconv`, which is GNU libiconv there and spells accents out
rather than dropping them, so `normalize_title` folds in Python (one case holds
the two paths to the same answers); and the codepoints that make `iconv` *give
up*, which two cases ask this host about before asserting on.

It has no `/proc` either, and the package never assumed one — `pausecontrol`
reads it where there is one and falls back to `kill -0` and `pgrep -P`. Five
*cases* did assume one, and they are the reason to say so here rather than treat
it as settled: three read `/proc/<pid>` to ask whether an encoder was stopped or
released, which off Linux answered "no such process" for a live one — so a case
asserting the encoders were *gone* passed without testing anything. They now
take the same two rungs the package does (`ps -o stat=` for the state letter,
which is `T` for stopped on both; signal 0 for liveness, where a refusal counts
as alive). The other two wanted a pid with nothing behind it and read
`/proc/sys/kernel/pid_max` to build one; they reap a child of their own instead,
which is the property they were after on any POSIX host.

**Windows** runs the `pure` tier and `test_ramscratch.py`, which is what CI
runs there. The rest fails for reasons that belong to the platform and not to
the code: the stubbed tier's tools are `#!/usr/bin/env bash` scripts, several
fixtures need a symlink (which needs Developer Mode or an elevated shell), and
a great many recorded values are paths spelled with `/`. Those are known and
not worth chasing — WSL2 is the answer there.

## Still needs real media (not synthesisable)

What a generated or anonymised fixture cannot drive, listed so the gap is
explicit rather than assumed:

- a genuine **dual-layer Dolby Vision profile 7 RPU**. No encoder produces one,
  so that it really converts to profile 8.1 and plays as DV still rests on
  manual verification. The *other* Dolby Vision outcome — a container claiming
  DV over a video with no RPU — needs no RPU to reproduce and is asserted in the
  media tier;
- the **AVIF encode** quality of the comic and image conversions;
- **whisper** transcript quality, and how well **ffsubsync** aligns against real
  *audio* (its VAD path). That is model output, which no fixture can stand in
  for. ffsubsync's *interface* — which flags it has, that it reports a refused
  alignment as `low-quality alignment` while still exiting 0, and that
  `--log-dir-path` writes that record unwrapped — is asserted against the real
  binary in the media tier, on subtitle fixtures that need no video at all.
