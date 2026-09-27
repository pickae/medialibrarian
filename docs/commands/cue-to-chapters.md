# `cue-to-chapters`

Converts a `.cue` sheet into an OGM chapter file (`CHAPTERNN=` /
`CHAPTERNNNAME=`), which is what the audio pipelines feed to `mkvmerge`.

    cue-to-chapters <input.cue> <output.chapters>

| At a glance | |
| --- | --- |
| **Takes** | one `.cue` sheet |
| **Writes** | one chapter file |
| **Network** | none |

It takes those two file names and no options, so any other argument list prints
its usage and fails.
