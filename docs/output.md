# What a run prints

Every command says the same kinds of things in the same places, and takes the
same two options for how much of it to say.

| | `-q`, `--quiet` | default | `-v`, `--verbose` |
| --- | --- | --- | --- |
| **Progress** — one line per step or item, the live status row, the closing figures | — | ✓ | ✓ |
| **Warnings** | — | ✓ | ✓ |
| **Why** — the reason behind each choice: skipped, picked this profile, retried | — | — | ✓ |
| **Errors**, and the files a run left alone to keep them safe | ✓ | ✓ | ✓ |
| **Results** — a report, a query's table, a preview of what would change | ✓ | ✓ | ✓ |

`-v` and `-q` together are refused. A command that starts another one hands its
level down, so a child says as much as its parent.

## The tools a command runs

ffmpeg, whisper, yt-dlp and the rest never print while they work: several run at
once, and their lines would land in each other's. What each one says is kept,
and a tool that succeeds is never heard from.

A tool that fails is replayed under the error, as one block:

- **default** — its last lines
- **`-v`** — the command that was run, its first lines and many more of its last;
  the tool itself is also asked to say more (ffmpeg at `-loglevel verbose`,
  yt-dlp with `--verbose`)
- **`-q`** — the error line alone

Either way the whole of it is kept in the run's error log (below), and the
block ends with that file's path. When the tool's own lines name a likely
cause — a full disk, memory running out, a site refusing the request — or it
was killed outright, the way the out-of-memory killer does, the block says so
in a `likely cause:` line. Keys, tokens and passwords are starred out first, in
the block and in the file.

A tool whose exit status cannot be trusted — whisper exits 0 after running out
of GPU memory — is judged by what it made instead, and replayed the same way
when that falls short.

A tool that succeeds is not heard from, with one exception: one that says it
met a damaged input and carried on anyway. ffmpeg exits 0 after decoding
broken frames (`Error while decoding stream`, `Header missing` in an mp3,
`moov atom not found`), or after writing an empty output, and the result may
have gaps or glitches that nothing else would catch. That earns one
`WARNING:`, the first such line and how many more there were, and the lines are
kept in the error log. The rest of what ffmpeg warns about is left out: most of
it is harmless, and repeating it would bury what is not.

## The error log

Every error a run reports is also kept, in `logs/error/`, one file per run,
named for when it started, the command and its process id:
`logs/error/20261105-140211-convert-audio-4242.log`. The file is only made when
the first error arrives, so a clean run leaves nothing there. The workers a run
fans out to and the commands it starts write into the same file.

The console stays short — one line, or a tool's last lines — and the file holds
the long form, one record per error:

- when it happened, to the millisecond and with the UTC offset;
- what kind of error it is, and what that kind means;
- the command line the run was started with, its start time and folder, and
  which process the error came from;
- the item being worked on and the input file it stands for, on the hard drive;
- every file involved, each marked as there or not, and those in the RAM
  scratch with the hard-drive file they were made from — an archive unpacked
  into RAM, a track copied there — so the culprit can still be found after the
  scratch is gone;
- for a third-party tool: its command line, how it ended, the likely cause, the
  place in the commands it was run from, and everything it printed that was
  kept;
- for a crash: the Python traceback. The console prints one line for a crash
  and where the traceback was kept; `-v` prints the traceback too, and so does
  any run whose error log could not be written.

Nothing is ever deleted from `logs/error/` by the commands.

These are the kinds of error a run keeps, and each is also said on the console:

| Kind | Severity | When |
| --- | --- | --- |
| `usage` | error | the command line was refused: an unknown option, a bad value, a missing argument |
| `input-missing` | error | the input folder or file does not exist |
| `input-unusable` | error | the input folder is empty or holds nothing the command works on |
| `output-inside-input` | error | the output folder sits inside the input folder |
| `config` | error | the settings file cannot be read, or holds a secret and is not private |
| `tool-missing` | error | a tool the command needs is not installed or not on PATH |
| `tool-failed` | error | a third-party tool exited non-zero or was killed |
| `tool-not-started` | error | a third-party tool could not be started at all |
| `tool-bad-output` | error | a third-party tool exited 0 but what it made falls short |
| `tool-skipped` | warning | a tool failed and the command skipped that step, not the item |
| `tool-warning` | warning | a tool succeeded but reported a damaged input or an empty output |
| `output-check` | error | an output was made but failed the command's own check, such as its length |
| `worker-died` | error | a worker process ended abnormally and its item was not finished |
| `disk-full` | error | a write failed because the disk or the RAM scratch is full |
| `out-of-memory` | error | the machine or the GPU ran out of memory |
| `permission` | error | a file could not be read or written: permission denied, read-only filesystem |
| `crash` | error | an unexpected Python exception stopped the command |
| `error` | error | any other error a command reports |

Help pages, a run with no arguments, and an interrupt are not errors, and
leave nothing in the log.

## Where it goes

Results go to standard output: a report's name, a query's table, the calls a
preview would make. Everything else — progress, warnings, errors, a failed
tool's replay — goes to standard error, beside the status row it shares the
screen with. So `command > results.txt` keeps the results and still shows the
run.
