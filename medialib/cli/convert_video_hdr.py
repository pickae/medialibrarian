"""convert-video's HDR side-work: the Dolby Vision preparation and the HDR10+
round trip.

Both happen around the encode rather than in it. A Dolby Vision source is
normalised first - profile 7 to 8.1, or its false claim dropped - into an
intermediate the encode reads instead of the source; HDR10+ metadata is
lifted off the source before the encode and written back into the finished
video after it, and a round trip that fails keeps HDR10 rather than the
file.
"""

import os
import shutil
import subprocess

from medialib.cli import convert_video as rules
from medialib.cli import convert_video_probes as probes
from medialib.cli import convert_video_source as videosource
from medialib.lib import codecs, ramscratch, toolcapture
from medialib.lib.runlog import log

# --- the Dolby Vision preparation ---------------------------------------------

def normalise_dolby_vision(relative: str, settings, output_dir: str) -> tuple:
    """Bring a dual-layer profile 7 source to single-layer 8.1, so its RPU can
    ride through the re-encode after all, and answer with the video-only file to
    encode INSTEAD of the source, plus the scratch holding it.

    Profile 7 carries its RPU in an enhancement layer no encoder here can
    re-encode; 8.1 is the single-layer form of the very same Dolby Vision, and
    reaching it needs no re-encode at all. Doing it here is what keeps a conversion
    from having to be preceded by an ingest: a profile 7 source that is not
    normalised first loses its dynamic metadata, and whether the user thought of
    that is not what should decide it.

    Every failure is a fallback to plain HDR10 - the source is never touched, and
    nothing here can fail the conversion of a file.
    """
    from medialib.lib import dolbyvision
    source = os.path.join(settings.input_dir, relative)

    missing = []
    if not probes._has_tool("dovi_tool"):
        missing.append("dovi_tool")
    if not probes._has_tool("mkvmerge"):
        missing.append("mkvmerge (mkvtoolnix)")
    if missing:
        log("Dolby Vision profile 7 cannot be normalised to 8.1 without %s - "
            "install it to keep Dolby Vision on dual-layer sources: %s"
            % (" and ".join(missing), relative))
        return "", ""

    fps = videosource.video_frame_rate(source)
    if not fps:
        log("Dolby Vision profile 7 cannot be normalised to 8.1: ffprobe "
            "reports no usable frame rate, and the converted stream has to be "
            "given one: %s" % relative)
        return "", ""

    # The container's claim is not proof of an RPU: a file remuxed by a tool that
    # copied the configuration record while dropping the RPU still reports profile
    # 7 with nothing behind it. Asked first, from a few frames, because the
    # alternative is copying a whole 4K video stream out of the file to find out.
    if not dolbyvision.stream_has_rpu(source):
        log("Dolby Vision profile 7 is claimed by the container but the video "
            "carries no RPU, so there is nothing to normalise: %s" % relative)
        return "", ""

    # Two files exist at once - mkvmerge reads the converted stream while writing
    # the Matroska around it - and a video stream is most of a film, so the need is
    # twice the SOURCE's size: an upper bound rather than an estimate, which is the
    # direction a capacity check has to err in.
    try:
        need = str(os.path.getsize(source) * 2)
    except OSError:
        need = ""
    scratch, on_disk, status = ramscratch.ram_scratch_dir_for(
        need, "convertVideoDv", output_dir)
    if status != 0:
        log("Dolby Vision profile 7 cannot be normalised to 8.1: no scratch "
            "directory could be created: %s" % relative)
        return "", ""
    # Registered as well as handed back by the caller: the release is the ordinary
    # path, the registration is what covers an interrupt landing before it.
    ramscratch.add_exit_cleanup([scratch])
    log("Dolby Vision: normalising the dual-layer profile 7 source to "
        "single-layer profile 8.1 first, so the encode can carry its RPU (the "
        "video itself is not re-encoded): %s" % relative)
    if on_disk:
        log("Dolby Vision: too large for the RAM scratch, preparing it on disk "
            "beside the output instead: %s" % relative)

    hevc = os.path.join(scratch, "dv81.hevc")
    prepared = os.path.join(scratch, "dv81.mkv")
    if dolbyvision.convert_to_profile81(source, hevc, log=log) != 0:
        log("Dolby Vision: the profile 7 -> 8.1 conversion failed, encoding the "
            "source as plain HDR10 instead: %s" % relative)
        ramscratch.release_exit_cleanup([scratch])
        return "", ""

    # --default-duration is not decoration: the raw stream has no timing, and
    # without it mkvmerge falls back to 25 fps - which would misplace every chunk
    # boundary cut from the intermediate.
    done = toolcapture.watched(
        ["mkvmerge", "--quiet", "-o", prepared, "--default-duration",
         "0:%sfps" % fps, hevc], announce=False, on_missing=127,
        skipped="encoding as plain HDR10 instead", stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    # The converted stream has served its purpose the moment it is inside the
    # Matroska; free it before the encode starts rather than holding both.
    probes._remove(hevc)
    if not toolcapture.warnings_at_most(done.returncode):
        log("Dolby Vision: the profile 8.1 stream could not be muxed (%s), "
            "encoding the source as plain HDR10 instead: %s"
            % (done.stderr.decode("utf-8", "replace").strip() or "no output",
               relative))
        ramscratch.release_exit_cleanup([scratch])
        return "", ""

    # Verified before a frame is encoded from it, the same way the source was
    # classified. Anything else is thrown away and the source encoded as it is -
    # which still has its RPU, so nothing is lost that was not lost already.
    profile, enhancement = videosource.dolby_vision_profile(prepared)
    reason = ""
    if not profile:
        reason = "the result signals no Dolby Vision at all"
    elif enhancement == "1":
        reason = "the result is still dual-layer (profile %s)" % profile
    if reason:
        log("Dolby Vision: %s, encoding the source as plain HDR10 instead: %s"
            % (reason, relative))
        ramscratch.release_exit_cleanup([scratch])
        return "", ""

    log("Dolby Vision: the source now reads as single-layer profile %s, and is "
        "encoded from that: %s" % (profile, relative))
    return prepared, scratch


def dolby_vision_probe(relative: str, settings) -> tuple:
    """True when this file can really be encoded with its RPU carried through,
    decided by actually running the encoder for one frame with the exact arguments
    the real encode would use.

    That covers every way it can fail - a profile ffmpeg cannot map onto the
    output codec, colour signalling it cannot classify, a build without the
    feature - in one check, and it costs a single frame. On failure the encoder's
    first complaint comes back so the caller can report why.
    """
    source = rules.video_source_for(relative, settings.input_dir,
                                    settings.dolby_vision_source)
    probing = rules.Settings(**dict(settings.__dict__, dolby_vision_mode="1"))
    args = rules.build_video_args(
        rules.profile_args(rules.VIDEO_PROFILES, settings.video_profile),
        source, probing)
    argv = (["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
             "-nostats"] + settings.decode_accel.split()
            + ["-i", source, "-an", "-sn", "-map", "0:v:0", "-frames:v", "1"]
            + args.split() + ["-f", "null", "-"])
    done = probes._run(argv, capture=True)
    if done.returncode == 0:
        return True, ""
    for line in done.stderr.decode("utf-8", "replace").splitlines():
        if line.strip() and not probes.svt_chatter(line):
            return False, line
    return False, ""


def dolby_vision_mode_for(relative: str, profile: str, enhancement: str,
                          settings) -> str:
    """The mode to encode this file with, as the "1" / "0" / "" value the argument
    builder takes, with the reason logged.

    The two cases that cannot work are recognised from the source and the encoder
    rather than attempted; anything else is settled by actually encoding a frame.
    Note the difference between "" and "0": empty leaves ffmpeg's auto default
    alone, which is right when there is no RPU to carry, while 0 actively switches
    DV off for a DV source we have decided against - otherwise auto would enable it
    by itself and fail an encode already known not to work.
    """
    if not profile:
        return ""
    if not settings.dv_encoder_support:
        log("Dolby Vision profile %s present, but %s cannot code an RPU - "
            "keeping HDR10 only: %s" % (profile, settings.encoder, relative))
        return ""
    if enhancement == "1":
        log("Dolby Vision profile %s is dual-layer, which cannot be re-encoded, "
            "and could not be normalised to single-layer profile 8.1 here (see "
            "above) - keeping HDR10 only: %s" % (profile, relative))
        return "0"
    worked, reason = dolby_vision_probe(relative, settings)
    if worked:
        log("Dolby Vision: carrying the profile %s RPU through the encode: %s"
            % (profile, relative))
        return "1"
    log("Dolby Vision profile %s cannot be encoded here - keeping HDR10 only "
        "(%s): %s" % (profile, reason or "no reason given", relative))
    return "0"


# --- the HDR10+ round trip ----------------------------------------------------

def prepare_hdr10plus(relative: str, source: str, directory: str, settings,
                      reframed: bool) -> str:
    """The source's HDR10+ metadata, read out to a JSON in <directory> so it can
    be written back into the encode, or "" for a file that does not keep it.

    No encoder here carries HDR10+ through an encode, so it is kept AROUND the
    encode instead, which works the same with every ffmpeg the run can settle
    on. Read from the ORIGINAL even when the video is encoded from a Dolby
    Vision intermediate: the profile 8.1 conversion drops HDR10+ from the stream
    it writes, and the original still has every frame of it. ``reframed`` is
    true when the encode is cropped or scaled - which the metadata survives
    unless it describes processing windows placed in the frame it was graded
    in.

    Every reason not to keep it is said, and none of them fails the file: what
    comes out instead is the plain HDR10 the source carries alongside.
    """
    from medialib.lib import hdr10plus
    if not hdr10plus.stream_has_hdr10plus(source):
        return ""
    codec = codecs.encoder_codec(settings.encoder)
    if codec != "hevc":
        log("HDR10+: the source carries it, but %s writes %s, which it cannot "
            "be written back into yet - keeping HDR10 only: %s"
            % (settings.encoder, codec.upper() or "a codec", relative))
        return ""
    source_codec = videosource._probe([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=codec_name",
        "-of", "default=nk=1:nw=1", source]).strip()
    if source_codec != "hevc":
        log("HDR10+: the source carries it in %s, and it can only be read out "
            "of HEVC - keeping HDR10 only: %s"
            % (source_codec.upper() or "an unknown codec", relative))
        return ""
    missing = [name for name in ("hdr10plus_tool", "mkvmerge")
               if not probes._has_tool(name)]
    if missing:
        log("WARNING: HDR10+: the source carries it and %s could keep it, but "
            "that needs %s - install it to keep HDR10+. Keeping HDR10 only: %s"
            % (settings.encoder, " and ".join(
                "mkvmerge (mkvtoolnix)" if name == "mkvmerge" else name
                for name in missing), relative))
        return ""
    if not videosource.video_frame_rate(source):
        log("HDR10+: ffprobe reports no usable frame rate, and the stream it is "
            "written back into has to be given one - keeping HDR10 only: %s"
            % relative)
        return ""

    metadata = os.path.join(directory, "hdr10plus.json")
    log("HDR10+: reading the source's dynamic metadata, to write it back into "
        "the encode: %s" % relative)
    if hdr10plus.extract_metadata(source, metadata, log=log) != 0:
        log("HDR10+: the metadata could not be read out of the source, keeping "
            "HDR10 only: %s" % relative)
        return ""
    if reframed and hdr10plus.metadata_windows(metadata) > 1:
        probes._remove(metadata)
        log("HDR10+: the metadata places processing windows in the frame it "
            "was graded in, and this encode is cropped or scaled - keeping "
            "HDR10 only: %s" % relative)
        return ""
    return metadata


def restore_hdr10plus(relative: str, directory: str, metadata: str,
                      settings) -> bool:
    """The source's HDR10+ <metadata> written into the finished video-only
    intermediate, in place, without re-encoding it. True when the intermediate
    now carries it.

    The rewritten video is checked before it replaces anything - as long as
    the encode, and signalling HDR10+ - so every failure leaves the plain HDR10
    encode exactly as it was.
    """
    from medialib.lib import hdr10plus
    video = os.path.join(directory, "video.mkv")
    source = os.path.join(settings.input_dir, relative)

    # The raw stream the metadata is written into, and the Matroska mkvmerge
    # writes around it, are each about the size of the encode, and the first is
    # gone before the second is finished - so twice the encode is an upper
    # bound.
    try:
        need = str(os.path.getsize(video) * 2)
    except OSError:
        need = ""
    scratch, _on_disk, status = ramscratch.ram_scratch_dir_for(
        need, "convertVideoHdr10plus", settings.output_dir)
    if status != 0:
        log("HDR10+: no scratch directory could be created, keeping HDR10 "
            "only: %s" % relative)
        return False
    ramscratch.add_exit_cleanup([scratch])

    log("HDR10+: writing the source's dynamic metadata back into the encode "
        "(the video is not re-encoded): %s" % relative)
    injected = os.path.join(scratch, "video.mkv")
    reason = ""
    if hdr10plus.inject_metadata(video, metadata, injected,
                                 videosource.video_frame_rate(source), scratch,
                                 log=log) != 0:
        reason = "it could not be written into the stream"
    elif (videosource._media_duration(injected)
          < videosource._media_duration(video) - 1):
        reason = "the rewritten video came out shorter than the encode"
    elif not hdr10plus.stream_has_hdr10plus(injected):
        reason = "the rewritten video does not signal it"
    else:
        # Moved in beside the encode first and only then renamed over it, so
        # there is no moment at which neither is there.
        staged = video + ".hdr10plus"
        try:
            shutil.move(injected, staged)
            os.replace(staged, video)
        except OSError as error:
            probes._remove(staged)
            reason = "the rewritten video could not replace the encode (%s)" \
                % error
    ramscratch.release_exit_cleanup([scratch])
    if reason:
        log("HDR10+: %s, keeping HDR10 only: %s" % (reason, relative))
        return False
    return True
