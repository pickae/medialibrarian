# `convert-audio`

Transcodes spoken-word audio to Opus at a low bitrate, optionally forcing mono,
and carries over chapters and cover art. `-e` picks the codec: Opus by default,
or xHE-AAC written as `.m4a` (see below). **Video files** are ingested too: their
audio stream is extracted and converted like any other input — and a video whose
soundtrack is *already* a small enough Opus is stream-copied out of its container
rather than encoded a second time into the same thing. **Long files** are
split at quiet points into chunks that encode in parallel and are transparently
re-joined, with the original's metadata re-attached. **Adaptive mode** decides
channels and bitrate per file: the output keeps the source's channel count and
gets the slightly higher bitrates allowing for sound-effects.

What gets re-encoded is decided per file: anything above the bitrate threshold,
anything in a format the output should not keep at all (`m4a`/`m4b`/`mka`), and
any video whose audio has to come out of its container. Everything else is small
enough already and is copied verbatim (`-c`) or left alone.

**xHE-AAC** (`-e xheaac`) is the one output codec ffmpeg cannot produce — it
decodes the codec and has no encoder for it — so the audio goes out over a pipe
to [exhale](https://gitlab.com/ecodis/exhale), which reads WAVE on stdin and
writes a finished `.m4a`. No distribution packages it, so a run that asks for
this codec without it says where to build it from and offers `-e opus` instead.

Two consequences worth knowing before using it. exhale takes a *preset* about 12
kbps apart rather than a bitrate, so a `-b` lands on the nearest rung and the run
prints which — `-b 46` really encodes at 48. And one pass through the pipe cannot
carry more than about 13½ hours of mono at 44.1 kHz, or half that in stereo,
because WAVE states its length in 32 bits — so a book past that is split whatever
`-s` says, chunking being the only way to encode it at all.

Long files split here as they do for Opus, and a re-joined book is its source's
length to the sample.

Every conversion is measured afterwards, here and in the other commands that
transcode: an output that is not as long as its input is removed and named, and
the run ends non-zero — an encoder that stops early otherwise leaves a playable
file and a zero exit status.
