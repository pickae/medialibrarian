# `transcribe-audio`

Transcribes an input folder of audio and video with whisper, writing one
transcript per input into a mirrored output folder (the same sub-folder structure,
a new extension): `<input>/a/track.mp3` becomes `<output>/a/track.txt`. Audio
files are transcribed as they are; a video's speech is taken from its **first audio
track only** — the video stream and any further audio tracks are ignored, and a
video with no audio track is skipped with a warning.

The whisper model is settled for the host once, up front: the GPU when whisper can
really run on it (the biggest model the free VRAM holds), otherwise the CPU. A
transcript already present in the output is left in place, so a re-run only does
what is new. The format (`-f`) defaults to `txt`, and `-j` sets how many
transcriptions run at a time.
