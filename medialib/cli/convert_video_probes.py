"""What this ffmpeg build and this machine can do, for convert-video.

Does this encoder take that argument, does this GPU open, which render node
is the Intel one: each is answered by running the tool once on nothing and
reading its status, once at startup or per file where the answer depends on
the file. What a source is, is `convert_video_source.py`.

The small tool helpers the run's other modules share live here too, being
the lowest of them.
"""

import os
import re
import shutil
import subprocess

from medialib.cli import convert_video as rules
from medialib.cli import convert_video_source as videosource
from medialib.lib import enums

# The sysfs PCI vendor id of the iGPU maker this script decodes with. Matching on
# it is what keeps a mixed Intel-plus-NVIDIA box from mistaking the NVIDIA node for
# the iGPU.
INTEL_PCI_VENDOR = "0x8086"


def _run(argv: list, capture: bool = False):
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE if capture
                              else subprocess.DEVNULL)
    except OSError:
        class Failed:
            returncode = 127
            stderr = b""
        return Failed()


def _has_tool(name: str) -> bool:
    return shutil.which(name) is not None


def _sources(root: str) -> list:
    found = []
    for parent, dirs, names in os.walk(root):
        dirs.sort()
        for name in sorted(names):
            if enums.lower_extension_of(name) in rules.SOURCE_EXTENSIONS:
                found.append(os.path.relpath(os.path.join(parent, name), root))
    return found


def svt_chatter(line: str) -> bool:
    """True for a line libsvtav1 wrote about itself rather than about a failure -
    its banner, the configuration it resolved and its warnings. Its errors are left
    alone: they are the only thing it says that a caller needs."""
    return line.startswith(("Svt[info]:", "Svt[warn]:"))


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# --- what this build and this machine can do ----------------------------------

def ffmpeg_takes_args(binary: str, args: str) -> bool:
    """The one-frame encode the build check is made of: true when the build runs
    it AND does not report a parameter it had to ignore.

    Only one of the two ways a build can be too old is loud. An option ffmpeg
    itself does not know fails the encode; but a key inside -svtav1-params that the
    linked SVT-AV1 does not know is logged as "Error parsing option" and then
    IGNORED. A build that applies half the tuning looks exactly like one that
    applies all of it, which is why this reads the encoder's complaints rather than
    comparing version numbers.
    """
    done = _run([binary, "-hide_banner", "-nostdin", "-loglevel", "warning",
                 "-f", "lavfi", "-i", "color=c=black:s=256x256:r=5:d=1"]
                + args.split() + ["-frames:v", "1", "-f", "null", "-"],
                capture=True)
    if done.returncode != 0:
        return False
    return b"error parsing option" not in done.stderr.lower()


def ffmpeg_takes_profile(binary: str, args: str) -> bool:
    """True when that build accepts every argument the selected profile asks for.

    The tuning info is the one argument allowed to be negotiated rather than
    demanded: a row asks for uhq, and a build that does not know the value refuses
    the encode over it - but the run does not need uhq, it FALLS BACK to hq, so
    judging a build on the un-demoted row rejects it for a reason that never
    applies to the encode it would actually run.
    """
    if ffmpeg_takes_args(binary, args):
        return True
    wanted = " -tune %s " % rules.NVENC_TUNE_WANTED
    if wanted not in " %s " % args:
        return False
    return ffmpeg_takes_args(binary, args.replace(
        wanted, " -tune %s " % rules.NVENC_TUNE_FALLBACK))


def svt_av1_hdr_works(binary: str) -> bool:
    """True when that build's libsvtav1 is SVT-AV1-HDR: it parses a key only the
    fork has. A build without libsvtav1 at all fails the encode, and a mainline
    one logs the key as unparsed - both answer False."""
    return ffmpeg_takes_args(binary, rules.SVT_AV1_HDR_PROBE)


_SVT_AV1_RELEASE = re.compile(rb"Encoder Lib v(\d+)\.(\d+)\.(\d+)")


def svt_av1_release(binary: str) -> tuple:
    """The SVT-AV1 release that build's libsvtav1 is built on, as (major, minor,
    patch), or () when it does not say - no libsvtav1, or a library too old to
    log its banner.

    Read from the banner the library itself logs, since ffmpeg's own version says
    nothing about it. SVT-AV1-HDR numbers its releases after the mainline one it
    is based on, so the two compare; the commits a nightly carries past its
    release are left out, because they are not comparable across the two.
    """
    done = _run([binary, "-hide_banner", "-nostdin", "-loglevel", "info",
                 "-f", "lavfi", "-i", "color=c=black:s=256x256:r=5:d=1",
                 "-c:v", "libsvtav1", "-frames:v", "1", "-f", "null", "-"],
                capture=True)
    found = _SVT_AV1_RELEASE.search(done.stderr or b"")
    return tuple(int(part) for part in found.groups()) if found else ()


class SvtBuildRank:
    """The rank convert-video's ffmpeg ladder orders SVT-AV1 builds by.

    The fork is worth more than a newer encoder only when the run uses what it
    does that mainline cannot: its PQ variance-boost curve, or av1Grain's film
    grain tune. Such a run ranks SVT-AV1-HDR first and the release second; any
    other ranks the newer release first, with the fork still winning a tie. Each
    build's answers are kept, for the startup summary and so the build that
    wins is not asked again.
    """

    def __init__(self, fork_use: str) -> None:
        # What the run would use the fork for, "" when nothing.
        self.fork_use = fork_use
        self.seen: dict = {}

    def facts(self, binary: str) -> tuple:
        """(is SVT-AV1-HDR, release) for that build."""
        if binary not in self.seen:
            self.seen[binary] = (svt_av1_hdr_works(binary),
                                 svt_av1_release(binary))
        return self.seen[binary]

    def __call__(self, binary: str) -> tuple:
        fork, release = self.facts(binary)
        return (fork, release) if self.fork_use else (release, fork)

    def note(self, chosen: str) -> str:
        """Why the build chosen is the SVT-AV1 one it is, when a build of the
        other kind was also able to run the profile; "" when there was no such
        choice."""
        fork, release = self.facts(chosen)
        others = [(binary, facts) for binary, facts in self.seen.items()
                  if facts[0] != fork]
        if not others:
            return ""
        other, (_other_fork, other_release) = others[0]
        if fork:
            if other_release > release:
                return ("chosen over the newer mainline SVT-AV1 %s in %s, "
                        "because this run uses %s"
                        % (_release_text(other_release), other,
                           self.fork_use))
            return ""
        return ("passed over (%s, SVT-AV1 %s): this run uses neither its PQ "
                "curve (no PQ source left to convert) nor av1Grain's film "
                "grain tune, and this build's SVT-AV1 %s is newer"
                % (other, _release_text(other_release),
                   _release_text(release)))


def _release_text(release: tuple) -> str:
    return ".".join(str(part) for part in release) if release else "of unknown release"


def pq_source_to_convert(settings) -> bool:
    """Whether any source this run still has to convert is PQ HDR - the one kind
    of file SVT-AV1-HDR encodes differently by itself.

    A source whose output already exists is passed over: it is usually finished,
    and a finished HDR file in the folder must not hold every new SDR one to the
    fork. Without an ffprobe on PATH to ask, the answer is yes, which keeps the
    fork where it was always preferred.
    """
    if not _has_tool("ffprobe"):
        return True
    for relative in _sources(settings.input_dir):
        output = rules.output_path_of(relative, settings.input_dir,
                                      settings.output_dir)
        if os.path.isfile(output):
            continue
        source = os.path.join(settings.input_dir, relative)
        if videosource.video_color_space(source)[2] == "smpte2084":
            return True
    return False


def nvenc_works(encoder: str) -> bool:
    """True when a one-frame test encode succeeds: GPU present, driver up, that
    codec's NVENC block available."""
    return _run(["ffmpeg", "-hide_banner", "-v", "error", "-f", "lavfi",
                 "-i", "color=c=black:s=256x256:r=5:d=1", "-c:v", encoder,
                 "-pix_fmt", "p010le", "-f", "null", "-"]).returncode == 0


def nvenc_tune_works(encoder: str, tune: str) -> bool:
    """True when that encoder will really accept that tuning info.

    Asked by encoding a frame rather than by reading the option's documented
    range, because the answer depends on the SDK the build was compiled against,
    the driver and the GPU generation at once - and only an actual encode has all
    three in play.
    """
    return _run(["ffmpeg", "-hide_banner", "-v", "error", "-f", "lavfi",
                 "-i", "color=c=black:s=256x256:r=5:d=1", "-c:v", encoder,
                 "-pix_fmt", "p010le", "-preset", "p7", "-tune", tune,
                 "-f", "null", "-"]).returncode == 0


def vaapi_works(node: str) -> bool:
    """True when a VA display can actually be opened on that node - the true gate
    for using it as a decode device.

    A functional probe rather than a guess from sysfs, so it reports what the
    machine can do right now. Only the DEVICE is exercised, not a particular
    filter, to avoid rejecting a working iGPU over an unrelated missing one.
    """
    return _run(["ffmpeg", "-hide_banner", "-v", "error", "-init_hw_device",
                 "vaapi=va:" + node, "-f", "lavfi",
                 "-i", "color=c=black:s=64x64:r=1:d=0.1", "-f", "null",
                 "-"]).returncode == 0


def videotoolbox_works() -> bool:
    """True when a VideoToolbox device can be opened - the macOS answer to the
    question :func:`vaapi_works` asks on Linux.

    Every Mac has the hardware, Intel and Apple Silicon alike, so what this is
    really testing is whether the ffmpeg on PATH was built with it: a
    Homebrew build is, and a static build fetched from elsewhere may not be.
    The probe is the same shape as the VAAPI one - open the device, decode
    nothing - so it rejects a build that cannot use the hardware without
    rejecting one that merely lacks some unrelated filter.
    """
    return _run(["ffmpeg", "-hide_banner", "-v", "error", "-init_hw_device",
                 "videotoolbox=vt", "-f", "lavfi",
                 "-i", "color=c=black:s=64x64:r=1:d=0.1", "-f", "null",
                 "-"]).returncode == 0


def intel_render_node() -> str:
    """The Intel render node to decode on, or "".

    The sysfs PCI vendor id is preferred. When the vendor files are absent (some
    virtualised hosts) it falls back to the first node a VA display can be opened
    on.
    """
    try:
        nodes = sorted(name for name in os.listdir("/dev/dri")
                       if name.startswith("renderD"))
    except OSError:
        return ""

    def vendor_of(name):
        try:
            with open("/sys/class/drm/%s/device/vendor" % name) as handle:
                return handle.read().strip()
        except OSError:
            return ""

    return pick_render_node(nodes, vendor_of, vaapi_works)


def pick_render_node(nodes: list, vendor_of, works) -> str:
    """The node out of <nodes> (``renderD*`` names, in order) to decode on, or "".

    An Intel node a VA display opens on comes first. Only then a node with no
    vendor to read, the first one that opens; a node whose vendor is known and is
    not Intel is never taken, whatever VA-API driver may open on it.
    """
    for name in nodes:
        node = "/dev/dri/" + name
        if vendor_of(name) == INTEL_PCI_VENDOR and works(node):
            return node
    for name in nodes:
        node = "/dev/dri/" + name
        if vendor_of(name):
            continue
        if works(node):
            return node
    return ""


def encoder_takes_master_display(encoder: str) -> bool:
    """Whether this nvenc build has the HDR static-metadata options; an older one
    keeps colour signalling only."""
    try:
        done = subprocess.run(["ffmpeg", "-hide_banner", "-h",
                               "encoder=" + encoder],
                              stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL)
    except OSError:
        return False
    return b"-master_display" in done.stdout


def encoder_takes_dolby_vision(encoder: str) -> bool:
    """Whether the SELECTED encoder can accept a source's RPU and write it back.

    Asked of the encoder rather than assumed from an ffmpeg version: only one that
    exposes the option can do it (libx265 and libsvtav1 do; the NVENC encoders have
    no RPU support at all), and an ffmpeg too old to offer it then simply behaves
    as it did before rather than erroring on an unknown argument.
    """
    try:
        done = subprocess.run(["ffmpeg", "-hide_banner", "-h",
                               "encoder=" + encoder],
                              stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL)
    except OSError:
        return False
    return b"-dolbyvision" in done.stdout
