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

Either way the whole of it is kept in `logs/tool-failures/`, and the block ends
with that file's path. Keys, tokens and passwords are starred out first, in
the block and in the file.

A tool whose exit status cannot be trusted — whisper exits 0 after running out
of GPU memory — is judged by what it made instead, and replayed the same way
when that falls short.

## Where it goes

Results go to standard output: a report's name, a query's table, the calls a
preview would make. Everything else — progress, warnings, errors, a failed
tool's replay — goes to standard error, beside the status row it shares the
screen with. So `command > results.txt` keeps the results and still shows the
run.
