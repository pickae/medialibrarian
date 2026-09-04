"""Turning what the TTS engine produced into what the library keeps.

An audiobook comes out of here in TWO files, because a read book is expensive -
hours of a machine, once - and is wanted in two shapes no single file can be. One
to LISTEN to: a synthetic voice, mono, 36 kbps of Opus, transparent for that and
small enough to put a library on a phone. One to KEEP: the narration is not
reproducible, so what is thrown away here is thrown away for good, and the engine
already built the whole book losslessly on its way to the m4b.

The lossless one is finished the same way the m4b was - the same filter pass, the
same sample rate and channel count, read off the m4b itself rather than assumed,
so the FLAC is that file's audio and not a second opinion about it. Nothing is
re-timed, because the filters are all sample-for-sample, so the m4b's chapter
timeline is the FLAC's.

The Opus is not encoded here but by ``convert-audio``: it is the one place that
knows how to carry chapter marks and cover art through an Opus encode, and it
splits a long file into one chunk per core - which for an audiobook of eight hours
is the difference between four minutes and forty seconds.
"""

import os
import shutil
import subprocess

from medialib import commands
from medialib.lib import chapters, mutagentags
from medialib.lib.newestfile import newest_file

__all__ = ["audiobook_lossless", "audiobook_to_opus"]

# Upstream's own export chain (its lib/core.py, _export_audio), repeated here because
# it only applies it on the way into a format that cannot hold chapters:
# dynaudnorm levels a synthetic reading whose volume drifts between chapters and
# sentences, slowly enough not to pump on the pauses; afftdn takes the faint
# synthesis hiss out of the silences. Emptying it turns the step into a plain
# re-encode of the raw model output.
LOSSLESS_FILTERS = "dynaudnorm=f=150:g=15,afftdn=nf=-70"
# FLAC's default effort. Level 8 buys about 1% at several times the CPU, which on
# a whole library is hours spent on nothing.
LOSSLESS_COMPRESSION = "5"


def _config(name, default):
    return os.environ.get(name, default)


def _probe_field(path, entry):
    """One ffprobe field of the first audio stream, or "" - the shell's
    ``ffprobe ... | head -n1``, whose status is the pipeline's and so is never
    what decides."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "quiet", "-select_streams", "a:0",
             "-show_entries", "stream=" + entry, "-of", "default=nk=1:nw=1",
             path],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL)
    except (OSError, ValueError):
        return ""
    lines = proc.stdout.decode("utf-8", "replace").split("\n")
    return lines[0] if lines else ""


def _run_quiet(argv):
    """A call the shell runs with everything redirected away: only its status is
    asked of it."""
    try:
        proc = subprocess.run(list(argv), stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL,
                              stdin=subprocess.DEVNULL)
    except (OSError, ValueError):
        return 1
    return proc.returncode


def _nonempty(path):
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


def audiobook_lossless(master, tagged, work_dir, script_dir=None,
                       python_bin=None):
    """The engine's raw lossless master turned into the file the library keeps.
    Returns its path, or None when nothing came out - which leaves the caller to
    keep the m4b instead.

    <tagged> is the finished audiobook the engine exported FROM this very master:
    the file that has the chapter marks and the cover art, and the file whose
    sound this one has to match. Both metadata steps are optional and silent - a
    book with no chapters and no cover simply yields a plain FLAC, and neither a
    missing cover nor an unwritable tag is worth failing a book that has been
    read for the last four hours.
    """
    if script_dir is None:
        script_dir = commands.script_dir()
    if not (os.path.isfile(master) and _nonempty(master)):
        return None
    out_dir = os.path.join(work_dir, "lossless")
    shutil.rmtree(out_dir, ignore_errors=True)
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError:
        return None

    name = master.rpartition("/")[2]
    dot = name.rfind(".")
    finished = os.path.join(out_dir, (name[:dot] if dot >= 0 else name)
                            + ".flac")

    # What the audiobook was exported as. Missing answers are simply not
    # asserted: ffmpeg then keeps whatever the master has, which is the next best
    # thing.
    rate = _probe_field(tagged, "sample_rate")
    channels = _probe_field(tagged, "channels")

    args = []
    filters = _config("audiobookLosslessFilters", LOSSLESS_FILTERS)
    if filters:
        args += ["-af", filters]
    if rate.isdigit():
        args += ["-ar", rate]
    if channels.isdigit():
        args += ["-ac", channels]

    if _run_quiet(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                   "-y", "-i", master, "-map", "0:a:0", "-map_metadata", "-1"]
                  + args
                  + ["-c:a", "flac", "-compression_level",
                     _config("audiobookLosslessCompression",
                             LOSSLESS_COMPRESSION), finished]) != 0:
        return None
    if not _nonempty(finished):
        return None

    chapters.attach_chapters(tagged, finished, out_dir, script_dir)

    cover = os.path.join(out_dir, "bookCover.jpg")
    _run_quiet(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-y", "-i", tagged, "-an", "-c:v", "copy", cover])
    if _nonempty(cover):
        mutagentags.embed_cover(finished, cover)
    try:
        os.remove(cover)
    except OSError:
        pass

    return finished


def _python_bin():
    """The interpreter the shell's ``pythonRun`` resolves, which is what runs
    the helper script below - so a stub on PATH stays in charge of it."""
    from medialib.lib import runlog
    return runlog.python_bin()


def audiobook_to_opus(audiobook, work_dir, bitrate, jobs, log_file,
                      script_dir=None):
    """One finished audiobook encoded to Opus; returns the file that came out, or
    None - which fails that book rather than publishing half of it.

    The work is done by ``convert-audio`` over a directory holding this one
    book, so it needs no per-file mode of its own.

    The child's environment is stripped of SAFETY_LOG and ABORT_FLAG. Both are
    inherited by design when one of these scripts wraps another in the same
    shell - but this one is a separate process INSIDE a parallel worker, and the
    tail of convert-audio removes the two files it was given on the way out,
    which would take the whole run's skip log and interrupt flag with it. The
    child makes its own instead; a Ctrl+C still reaches it, because it sits in
    the same process group as everything else in the run.
    """
    if script_dir is None:
        script_dir = commands.script_dir()
    if not (os.path.isfile(audiobook) and _nonempty(audiobook)):
        return None

    # A directory of its own for the input, so what the encoder is pointed at is
    # this one book and not whatever else the workspace holds. Linked rather than
    # copied - the source is already in the RAM workspace.
    in_dir = os.path.join(work_dir, "toOpus")
    out_dir = os.path.join(work_dir, "opus")
    for directory in (in_dir, out_dir):
        shutil.rmtree(directory, ignore_errors=True)
    try:
        for directory in (in_dir, out_dir):
            os.makedirs(directory, exist_ok=True)
    except OSError:
        return None
    target = os.path.join(in_dir, audiobook.rpartition("/")[2])
    try:
        os.link(audiobook, target)
    except OSError:
        try:
            shutil.copyfile(audiobook, target)
        except OSError:
            return None

    child = dict(os.environ)
    child.pop("SAFETY_LOG", None)
    child.pop("ABORT_FLAG", None)
    try:
        with open(log_file, "ab") as handle:
            commands.run_command("convert-audio",
                                 ["-m", "-b", bitrate, "-j", jobs,
                                  in_dir, out_dir],
                                 script_dir=script_dir,
                                 stdin=subprocess.DEVNULL, stdout=handle,
                                 stderr=subprocess.STDOUT, env=child)
    except (OSError, ValueError):
        # `|| true`: whatever the encoder did, what decides is the file it left.
        pass

    produced = newest_file(out_dir, "opus")
    shutil.rmtree(in_dir, ignore_errors=True)
    if not produced or not _nonempty(produced):
        return None
    return produced
