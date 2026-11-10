# /// script
# requires-python = ">=3.10"
# dependencies = ["whisper-ctranslate2", "av<19", "nvidia-cublas-cu12"]
# ///
"""What a film's audio tracks say over the same few minutes: how much of each is
speech, the words whisper hears in it, and the language they are in.

    commentary_probe.py --model M --device D --compute_type C --threads T
                        --seconds S --at F [--at F ...] MOVIE STREAM [STREAM ...]

Each ``--at`` is a fraction of the film's running time a window of ``--seconds``
starts at; each STREAM an ffmpeg audio index ("0" for ``-map 0:a:0``). Prints
one JSON object keyed by STREAM: ``speech`` (the share of the windows the voice
detector hears someone talking in), ``text`` (whisper's transcript of the
windows, one line a window) and ``language`` (the language whisper hears most
often across them).

The dependencies are whisper_transcribe.py's, word for word, so ``pipx run``
reuses the environment it already built for that one. The model is loaded once
for every track, which is the reason this is a program of its own rather than a
whisper-ctranslate2 run per window.
"""

import argparse
import ctypes
import glob
import json
import os
import subprocess
import sys
from collections import Counter

RATE = 16000


def _load_cublas():
    try:
        import nvidia.cublas
    except ImportError:
        return
    for folder in nvidia.cublas.__path__:
        # the Lt library first: the other one needs it
        for name in ("libcublasLt.so.*", "libcublas.so.*"):
            for path in glob.glob(os.path.join(folder, "lib", name)):
                try:
                    ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL)
                except OSError:
                    pass


_load_cublas()

import numpy as np  # noqa: E402
from faster_whisper import WhisperModel  # noqa: E402
from faster_whisper.vad import VadOptions, get_speech_timestamps  # noqa: E402


def _duration(movie):
    done = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "default=nk=1:nw=1", movie],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL)
    try:
        return float(done.stdout.decode("utf-8", "replace").strip())
    except ValueError:
        return 0.0


def _window(movie, stream, start, seconds):
    """One window of one track, as the 16 kHz mono samples whisper wants."""
    done = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-ss", "%.3f" % start,
         "-t", str(seconds), "-i", movie, "-map", "0:a:%s" % stream,
         "-vn", "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL)
    return np.frombuffer(done.stdout, np.int16).astype(np.float32) / 32768.0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--compute_type", required=True)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--seconds", type=int, required=True)
    parser.add_argument("--at", type=float, action="append", required=True)
    parser.add_argument("movie")
    parser.add_argument("streams", nargs="+")
    args = parser.parse_args()

    duration = _duration(args.movie)
    if duration <= 0:
        sys.stderr.write("no running time read from %s\n" % args.movie)
        return 1
    starts = [max(0.0, min(duration - args.seconds, duration * at))
              for at in args.at]

    model = WhisperModel(args.model, device=args.device,
                         compute_type=args.compute_type,
                         cpu_threads=args.threads)
    vad = VadOptions()
    answer = {}
    for stream in args.streams:
        heard = total = 0.0
        lines: list = []
        languages: Counter = Counter()
        for start in starts:
            samples = _window(args.movie, stream, start, args.seconds)
            if not len(samples):
                continue
            total += len(samples) / RATE
            heard += sum(span["end"] - span["start"] for span in
                         get_speech_timestamps(samples, vad)) / RATE
            segments, info = model.transcribe(
                samples, beam_size=1, vad_filter=True,
                condition_on_previous_text=False)
            lines.append(" ".join(segment.text.strip()
                                  for segment in segments))
            if info.language:
                languages[info.language] += 1
        answer[stream] = {
            "speech": heard / total if total else 0.0,
            "text": "\n".join(lines),
            "language": languages.most_common(1)[0][0] if languages else "",
        }
    json.dump(answer, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
