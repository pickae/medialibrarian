"""The remux conversion (-r): each film that is a disc remux, converted by
convert-video and written beside it.

A remux is told by its name, which says so, or by its video, which only a disc
carries: a dual-layer Dolby Vision profile 7 HEVC 2160p stream at a generous
bitrate, any other HEVC 2160p one at a very generous bitrate, or an SDR H.264
1080p one at a very generous bitrate. The name is read before the cleaning
takes the word out of it, and the file is known again at the end by its inode,
which every rename keeps; the video is read from the original the improved
remux kept where there is one, since that remux turns profile 7 into 8.1.

The conversion is convert-video's, on its default video settings, with -k:
the finished video combined with every other track of the ingested film as it
is. It is written into the film's folder under the same name with the
:data:`plexnames.CONVERTED_EDITION` edition, so Plex offers the two as one film,
and every later phase and run leaves that copy alone the way it leaves the
"(old)" one.
"""

from __future__ import annotations

import os
import shutil

from medialib import commands
from medialib.cli import convert_video as videorules
from medialib.cli import ingest_movies as rules
from medialib.cli import ingest_movies_plex as plexphase
from medialib.lib import (
    adequacy,
    codecs,
    plexnames,
    ramscratch,
    resolutions,
    safety,
    videobitrate,
)

__all__ = ["convert_remuxes", "kept_original", "named_remuxes", "remux_reason"]

# The word a release names itself a remux with, in any case.
REMUX_WORD = "remux"

# How far above adequate a stream has to be to be a disc's rather than an
# encode's when nothing else about it says so: a Blu-ray spends 20-40 Mbit/s on
# a 1080p H.264 picture an encode gives 8-15 (adequate is 5), and a UHD disc
# 50-100 on a 2160p HEVC one a web release gives 15-40 (adequate is 11).
VERY_GENEROUS_FACTOR = 4

# The transfer characteristics that make a stream HDR, as ffprobe names them.
_HDR_TRANSFERS = ("smpte2084", "arib-std-b67")


def named_remuxes(root: str) -> set:
    """The films under ``root`` whose name, or a folder's on the way to them,
    says they are a remux - as (device, inode), which outlasts every rename
    the ingest makes until the film is converted."""
    found = set()
    for path in rules._files_below(
            root, matches=lambda name: name.lower().endswith(".mkv")):
        if REMUX_WORD not in os.path.relpath(path, root).lower():
            continue
        try:
            stat = os.stat(path)
        except OSError:
            continue
        found.add((stat.st_dev, stat.st_ino))
    return found


def kept_original(movie: str) -> str:
    """The "(old)" copy the improved remux kept of ``movie``, or "" when it has
    none.

    Matched by the name the copy WOULD be given rather than the name it has:
    it is the one file the tagging leaves alone, so it still carries the
    version name its film was imported under.
    """
    directory, name = os.path.split(movie)
    stem = os.path.splitext(name)[0]
    own = os.path.join(directory, stem + plexnames.OLD_SUFFIX)
    if os.path.isfile(own):
        return own

    base, tag = plexnames.untagged_base(os.path.basename(
        directory.rstrip("/")))
    for other in sorted(rules._names_in(directory)):
        if not other.endswith(plexnames.OLD_SUFFIX):
            continue
        kept = other[:-len(plexnames.OLD_SUFFIX)]
        # Guarded, or a stray "(old)" belonging to nothing would read as the
        # plain film's copy: an unreadable stem gives no edition and no part,
        # and that is exactly the plain film's name.
        if not kept.startswith(base):
            continue
        if plexnames.plex_stem(base, tag,
                               *plexnames.read_stem(base, kept)) == stem:
            return os.path.join(directory, other)
    return ""


def remux_reason(named: bool, codec: str, width: str, height: str, fps: str,
                 kbit: str, dv_profile: str, transfer: str) -> str:
    """Why a film with this video is a remux, or "" when it is not one.

    ``named`` is its name saying so. The rest describe the first video stream
    as the disc had it: ``kbit`` its own bitrate, ``dv_profile`` its Dolby
    Vision profile ("" for none), ``transfer`` its transfer characteristics.
    """
    if named:
        return "its name says it is a remux"
    family = codecs.family_of(codec)
    tier = resolutions.tier_of(width, height)
    adequate = videobitrate.adequate_video_bitrate(codec, width, height, fps,
                                                   "")
    if (dv_profile == "7" and family == "hevc" and tier == "2160p"
            and adequacy.verdict(kbit, adequate) == adequacy.GENEROUS):
        return ("Dolby Vision profile 7 in 2160p HEVC at %s kbit/s"
                % kbit)
    # HDR10 alone, or profile 8.1 with no "(old)" profile 7 left to read:
    # nothing but the bitrate tells it from a web release.
    if (family == "hevc" and tier == "2160p"
            and adequacy.verdict(kbit, adequate, VERY_GENEROUS_FACTOR)
            == adequacy.GENEROUS):
        return "2160p HEVC at %s kbit/s" % kbit
    if (not dv_profile and transfer not in _HDR_TRANSFERS and family == "h264"
            and tier == "1080p"
            and adequacy.verdict(kbit, adequate, VERY_GENEROUS_FACTOR)
            == adequacy.GENEROUS):
        return "SDR 1080p H.264 at %s kbit/s" % kbit
    return ""


def _reason_for(movie: str, named: set) -> str:
    """:func:`remux_reason` for a film on disk, read from the original its
    improved remux kept where there is one."""
    original = kept_original(movie) or movie
    identities = set()
    for path in (movie, original):
        try:
            stat = os.stat(path)
        except OSError:
            continue
        identities.add((stat.st_dev, stat.st_ino))
    if identities & named:
        return remux_reason(True, "", "", "", "", "", "", "")
    stats = videobitrate.video_bitrate_stats(original).split(" ")
    codec, fps, kbit = (stats + ["", "", ""])[:3]
    width, height, _order, _sar = videorules.video_dimensions(original)
    dv_profile, _el = videorules.dolby_vision_profile(original)
    _space, _range, transfer = videorules.video_color_space(original)
    return remux_reason(False, codec, width, height, fps, kbit, dv_profile,
                        transfer)


def remuxes_in(root: str, named: set, log) -> list:
    """(film, where its converted copy goes, why it is a remux) for every
    film under ``root`` that is a remux. Each part of a split film is a film
    of its own here, and its copy keeps the stacking token last, so the
    copies stack as the parts do."""
    found = []
    for movie in plexphase.films(root):
        directory, name = os.path.split(movie)
        stem = os.path.splitext(name)[0]
        reason = _reason_for(movie, named)
        if not reason:
            continue
        target = os.path.join(directory,
                              plexnames.converted_stem(stem) + ".mkv")
        found.append((movie, target, reason))
    return found


def convert_remuxes(root: str, named: set, ram_root: str, script_dir: str,
                    log) -> int:
    """Every remux under ``root`` converted by ONE convert-video run, its copy
    written beside it. convert-video's own status, 0 when there was nothing to
    convert.

    convert-video mirrors its input tree under its output, so its input is a
    tree of links in the scratch, each where its film's folder is under
    ``root`` and named as the copy is to be: ``root`` is then the output, and
    every copy lands in its film's folder under its own name. A copy already
    as long as its film is convert-video's to skip, so a rerun converts only
    what an earlier one did not finish.
    """
    remuxes = remuxes_in(root, named, log)
    if not remuxes:
        log("  No remux to convert")
        return 0
    for movie, _target, reason in remuxes:
        log("  A remux (%s): ./%s" % (reason, os.path.relpath(movie, root)))

    links = os.path.join(ram_root, "remuxLinks")
    shutil.rmtree(links, ignore_errors=True)
    for movie, target, _reason in remuxes:
        link = os.path.join(links, os.path.relpath(target, root))
        os.makedirs(os.path.dirname(link), exist_ok=True)
        os.symlink(os.path.abspath(movie), link)
    ramscratch.add_exit_cleanup([links])
    try:
        status = commands.run_command(
            "convert-video", ["-k", links, root],
            script_dir=script_dir).returncode
    finally:
        shutil.rmtree(links, ignore_errors=True)
        ramscratch.release_exit_cleanup([links])
    if status != 0 and not safety.abort_requested():
        log("  WARNING: convert-video ended with status %d; what it finished "
            "is kept beside its film" % status)
    return status
