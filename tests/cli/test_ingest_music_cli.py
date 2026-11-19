"""`ingest-music`'s accounting: what it says it did, and that it says it once.

Four counted phases, and the invariant every one of them has to hold: one
"[n/total]" line per item, ONE denominator per phase - the number of items that
phase was handed - and a counter that reaches it exactly. Never overshooting it and
never stopping short, on every path through the phase including the skips. A re-run
over a finished library is all skips, and a phase that reports nothing for those
looks exactly like a stalled encoder.

The heavy tools are stubbed, so no codecs are needed; the real rsync, the real
de-duplication and the real cue parsing do the rest, so what is exercised is the
command's own accounting rather than a mock of it.
"""

from __future__ import annotations

import os
import re
import shutil

import pytest

from tests import blackbox, conftest
from tests.cli.test_no_mkvtoolnix_cli import _ORDINARY

pytestmark = pytest.mark.stubbed

_TOOLS = ("rsync", "flock", "jq", "xargs")

# Which file a call is about is the argument after -i, or the last one. Four call
# shapes are answered, matching the four the command makes: the JSON stream info,
# a tag read back, a bare duration, and the "duration=" line. A file that does not
# exist answers nothing, which is what makes the "no output yet" branch encode.
#
# The tag shape is what lets the provenance resume be exercised without codecs: the
# ffmpeg stub writes whatever `-metadata` it was handed into its fake output and
# this reads it back, so the round trip under test is the COMMAND's - record the
# source, find it again after the library was renamed - with the encoder's own
# tagging stubbed either side of it.
_FFPROBE = r"""
file=""; prev=""
for a in "$@"; do [[ "$prev" == "-i" ]] && file="$a"; prev="$a"; done
[[ -n "$file" ]] || file="${!#}"
[[ -e "$file" ]] || exit 0
json=0; entries=""; prev=""
for a in "$@"; do
  [[ "$a" == "json" ]] && json=1
  [[ "$prev" == "-show_entries" ]] && entries="$a"
  prev="$a"
done
if (( json )); then
  codec=flac
  case "$file" in *lossy*) codec=mp3 ;; esac
  printf '{"streams":[{"codec_name":"%s","sample_rate":"44100"}],' "$codec"
  printf '"format":{"duration":"600.000"}}\n'
  exit 0
fi
if [[ "$entries" == format_tags=* ]]; then
  wanted="${entries#format_tags=}"
  recorded="$(sed -n 2p -- "$file")"
  [[ "$recorded" == "$wanted="* ]] && printf '%s\n' "${recorded#*=}"
  exit 0
fi
[[ -n "$entries" ]] && { echo "600.000"; exit 0; }
printf 'duration=600.000\n'
"""

_FFMPEG = r"""
out="${!#}"; meta=""; prev=""
for a in "$@"; do [[ "$prev" == "-metadata" ]] && meta="$a"; prev="$a"; done
printf 'flac\n%s\n' "$meta" > "$out"
"""

_CONVERT = r'out="${!#}"; out="${out%\>}"; printf avif > "$out"'

_MKVMERGE = r"""
prev=""; for a in "$@"; do [[ "$prev" == "-o" ]] && printf mkv > "$a"; prev="$a"; done
"""

_CUE = """FILE "one.flac" WAVE
  TRACK 01 AUDIO
    TITLE "First"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Second"
    INDEX 01 01:30:00
"""

# Told apart by the distinctive words of their lines rather than by the "Skip ("
# they share, so a phase's skips count towards its own denominator and not another's.
_PHASES = [
    # Three files with a lossless extension: two real, one that only claims to be.
    ("encoding", 3, r"Encoding:|already ingested|not lossless"),
    # Both cover images, one to AVIF and one copied.
    ("covers", 2, r"Cover to AVIF|Cover copied"),
    # The one stray video.
    ("remuxing", 1, r"Remuxing to Matroska:"),
    # The one cue sheet. On a FIRST run the cues are pruned after the encoding and
    # the embedding rather than where the copy puts them - which is where every cue
    # in a release is an orphan, no flac having been encoded yet.
    ("chapters", 1, r"Chapters |no flac this cue|no chapter in the cue"
                    r"|flac reports no duration|chapters start past"),
]


def _counted(log: str, pattern: str) -> list[tuple[int, int]]:
    """The counted lines of one phase, as (n, total)."""
    found = []
    for line in log.splitlines():
        head = re.match(r"^\[(\d+)/(\d+)\]", line)
        if head and re.search(pattern, line):
            found.append((int(head.group(1)), int(head.group(2))))
    return found


def _stat(log: str, label: str) -> str | None:
    """The number the closing stats block gives for a row, or None when the block
    does not carry that row at all - which is how a zero-count category is left
    out rather than printed as a nought."""
    found = re.findall(r"^%s: *(\d*)" % re.escape(label), log, re.MULTILINE)
    return found[-1] if found else None


@pytest.fixture
def music(sandbox, tmp_path):
    """One release holding one of everything the phases count."""
    for tool in _TOOLS:
        if shutil.which(tool) is None:
            conftest.missing_host_tool(
                "the host has no %s: the command needs it to report what is "
                "asserted here" % tool)
    sandbox.with_tool("ffprobe", _FFPROBE)
    sandbox.with_tool("ffmpeg", _FFMPEG)
    sandbox.with_tool("convert", _CONVERT)
    sandbox.with_tool("mkvmerge", _MKVMERGE)
    # fdupes is stubbed OUT rather than stubbed: the real one would delete the
    # fixture's same-sized placeholders as duplicates of each other, and the counts
    # under test would then depend on which one it kept. The other three only have
    # to succeed; nothing here asserts on what they would have written.
    sandbox.with_tools("fdupes", "beet", "mkvpropedit", "mkvextract")

    download = tmp_path / "download"
    album = download / "Album"
    album.mkdir(parents=True)
    (album / "one.flac").write_text("one")
    (album / "two.flac").write_text("two")
    # The one whose extension lies about its codec.
    (album / "lossy.flac").write_text("lossy")
    (album / "clip.mp4").write_text("clip")
    # Over the cover threshold, so it is worth an AVIF - and one well under it.
    (album / "cover.jpg").write_bytes(b"\0" * 1200000)
    (album / "back.jpg").write_text("small")
    (album / "one.cue").write_text(_CUE)

    library = tmp_path / "library"
    opus = tmp_path / "libraryopus"

    def run():
        done = sandbox.run("ingest-music", "-j", "2", download, library, opus,
                           timeout=900)
        return done.stdout + done.stderr

    sandbox.library = library
    sandbox.ingest = run
    return sandbox


# Apple Music files carry .m4a whether the codec is lossy AAC or lossless ALAC,
# so the codec is the whole story: the ffprobe stub answers alac for the track
# the case encodes and aac for the one it must copy, whatever the extension.
_FFPROBE_M4A = r"""
file=""; prev=""
for a in "$@"; do [[ "$prev" == "-i" ]] && file="$a"; prev="$a"; done
[[ -n "$file" ]] || file="${!#}"
[[ -e "$file" ]] || exit 0
json=0; entries=""; prev=""
for a in "$@"; do
  [[ "$a" == "json" ]] && json=1
  [[ "$prev" == "-show_entries" ]] && entries="$a"
  prev="$a"
done
case "$file" in *two*) codec=aac ;; *) codec=alac ;; esac
if (( json )); then
  printf '{"streams":[{"codec_name":"%s","sample_rate":"44100"}],' "$codec"
  printf '"format":{"duration":"600.000"}}\n'
  exit 0
fi
if [[ "$entries" == format_tags=* ]]; then
  wanted="${entries#format_tags=}"
  recorded="$(sed -n 2p -- "$file")"
  [[ "$recorded" == "$wanted="* ]] && printf '%s\n' "${recorded#*=}"
  exit 0
fi
[[ -n "$entries" ]] && { echo "600.000"; exit 0; }
printf 'duration=600.000\n'
"""


@pytest.fixture
def m4a_library(sandbox, tmp_path):
    """One release with an ALAC .m4a and a lossy .m4a side by side - the one
    extension, two different fates."""
    for tool in _TOOLS:
        if shutil.which(tool) is None:
            conftest.missing_host_tool(
                "the host has no %s: the command needs it to do the work "
                "asserted here" % tool)
    sandbox.with_tool("ffprobe", _FFPROBE_M4A)
    sandbox.with_tool("ffmpeg", _FFMPEG)
    sandbox.with_tool("convert", _CONVERT)
    sandbox.with_tool("mkvmerge", _MKVMERGE)
    sandbox.with_tools("fdupes", "beet", "mkvpropedit", "mkvextract")

    download = tmp_path / "download"
    album = download / "Album"
    album.mkdir(parents=True)
    (album / "one.m4a").write_text("alac")
    (album / "two.m4a").write_text("aac")

    library = tmp_path / "library"
    opus = tmp_path / "libraryopus"

    def run():
        done = sandbox.run("ingest-music", "-j", "2", download, library, opus,
                           timeout=900)
        return done.stdout + done.stderr

    sandbox.download = download
    sandbox.library = library
    sandbox.opus = opus
    sandbox.ingest = run
    return sandbox


# An encoder that stops half way still closes a valid file and exits 0, so the
# length is what settles it: the stub answers the source's full length for
# anything in the download, and half of it for the output the encoder "wrote"
# into the library - the shape of an encode that gave up part way through.
_FFPROBE_SHORT = r"""
file=""; prev=""
for a in "$@"; do [[ "$prev" == "-i" ]] && file="$a"; prev="$a"; done
[[ -n "$file" ]] || file="${!#}"
[[ -e "$file" ]] || exit 0
json=0; entries=""; prev=""
for a in "$@"; do
  [[ "$a" == "json" ]] && json=1
  [[ "$prev" == "-show_entries" ]] && entries="$a"
  prev="$a"
done
case "$file" in */library/*) dur=300.000 ;; *) dur=600.000 ;; esac
if (( json )); then
  printf '{"streams":[{"codec_name":"flac","sample_rate":"44100"}],'
  printf '"format":{"duration":"%s"}}\n' "$dur"
  exit 0
fi
if [[ "$entries" == format_tags=* ]]; then
  wanted="${entries#format_tags=}"
  recorded="$(sed -n 2p -- "$file")"
  [[ "$recorded" == "$wanted="* ]] && printf '%s\n' "${recorded#*=}"
  exit 0
fi
[[ -n "$entries" ]] && { echo "$dur"; exit 0; }
printf 'duration=%s\n' "$dur"
"""


@pytest.fixture
def short_encode(sandbox, tmp_path):
    """One lossless track whose encode comes out half as long as its source."""
    for tool in _TOOLS:
        if shutil.which(tool) is None:
            conftest.missing_host_tool(
                "the host has no %s: the command needs it to do the work "
                "asserted here" % tool)
    sandbox.with_tool("ffprobe", _FFPROBE_SHORT)
    sandbox.with_tool("ffmpeg", _FFMPEG)
    sandbox.with_tool("convert", _CONVERT)
    sandbox.with_tool("mkvmerge", _MKVMERGE)
    sandbox.with_tools("fdupes", "beet", "mkvpropedit", "mkvextract")

    download = tmp_path / "download"
    album = download / "Album"
    album.mkdir(parents=True)
    (album / "one.flac").write_text("full length")

    library = tmp_path / "library"
    opus = tmp_path / "libraryopus"

    def run(*options):
        # The suite turns the length check off below the media tier (stubs would
        # otherwise report every output as truncated); this case IS about a
        # truncated output, and its stubs report real lengths, so it asks for the
        # check back.
        done = sandbox.run("ingest-music", "-j", "2", *options, download,
                           library, opus,
                           env=dict(os.environ, SKIP_LENGTH_CHECK=""),
                           timeout=900)
        return done

    sandbox.download = download
    sandbox.library = library
    sandbox.opus = opus
    sandbox.ingest = run
    return sandbox


def _assert_phase(log, label, expected, pattern):
    lines = _counted(log, pattern)
    denominators = {total for _, total in lines}
    assert denominators == {expected}, \
        "%s: denominators %s, expected {%d}\n%s" % (label, denominators,
                                                    expected, log)
    counters = sorted(n for n, _ in lines)
    assert counters == list(range(1, expected + 1)), \
        "%s: counters %s\n%s" % (label, counters, log)


class TestAnM4aThatIsLossless:
    """Apple Music files carry .m4a whether the codec is lossy AAC or lossless
    ALAC, so the extension alone cannot tell them apart. The lossless one is
    renamed up front and handed to the encoder; the lossy one is the ordinary
    case and is carried across as a copy, re-encoded by nothing."""

    def test_the_alac_m4a_is_encoded_not_copied(self, m4a_library):
        m4a_library.ingest()
        library = m4a_library.library
        assert (library / "Album" / "one.flac").is_file()
        assert not (library / "Album" / "one.m4a").exists()

    def test_the_lossy_m4a_is_copied_not_encoded(self, m4a_library):
        m4a_library.ingest()
        library = m4a_library.library
        assert (library / "Album" / "two.m4a").is_file()
        assert not (library / "Album" / "two.flac").exists()

    def test_the_alac_rename_is_announced(self, m4a_library):
        log = m4a_library.ingest()
        assert "Lossless m4a (ALAC) renamed for the encoder" in log, log


class TestAShortEncodeFailsTheRun:
    """An encoder that stops early still closes a valid file and exits 0, so the
    length is what settles it: an output shorter than its source is a failure the
    run reports, and - because a finished library could then hold a track cut off
    half way - it is the reason the whole run ends nonzero."""

    def test_the_run_ends_nonzero_when_an_encode_came_out_short(
            self, short_encode):
        done = short_encode.ingest()
        log = done.stdout + done.stderr
        assert done.returncode == 1, log
        assert "ffmpeg could not encode this track" in log, log

    def test_the_track_that_came_out_short_is_counted_failed_not_encoded(
            self, short_encode):
        done = short_encode.ingest()
        log = done.stdout + done.stderr
        assert re.search(r"^  failed: +1$", log, re.MULTILINE), log
        assert re.search(r"^  encoded: ", log, re.MULTILINE) is None, log


class TestAQuietRun:
    """`-q` leaves out the counted lines, the warnings and the closing stats -
    but a run that failed still fails, and still says which track it was."""

    @pytest.fixture
    def done(self, short_encode):
        return short_encode.ingest("-q")

    def test_it_still_ends_nonzero_naming_the_track(self, done):
        log = done.stdout + done.stderr
        assert done.returncode == 1, log
        assert "  Album/one.flac (input 0:10:00, output 0:05:00)" in log, log

    def test_the_progress_warnings_and_stats_are_left_out(self, done):
        log = done.stdout + done.stderr
        assert _counted(log, r".") == [], log
        assert "ffmpeg could not encode this track" not in log, log
        assert "Stats" not in log, log


class TestAnOutputNestedInTheInput:
    """The FLAC library and the Opus copies are written to output folders, and
    what the run looks for in the input is exactly what it writes to the output,
    so an output nested inside the input would be ingested again by the next run
    - and the cleanup of the input tree would reach into the finished output.
    Each of the three folders nested inside another is refused before the ingest
    folder is created, so a refused run leaves nothing behind.

    The refusal is settled from the paths alone, before any tool is reached for,
    so these cases need no stubs: a folder to stand in for the input, and the
    nested path typed as the output."""

    def test_a_library_inside_the_download_is_refused_and_leaves_nothing(
            self, sandbox, tmp_path):
        download = tmp_path / "download"
        album = download / "Album"
        album.mkdir(parents=True)
        (album / "one.flac").write_text("one")
        before = blackbox.tree_of(download)
        library = download / "library"
        done = sandbox.run("ingest-music", download, library)
        log = done.stdout + done.stderr
        assert done.returncode == 1, log
        assert "Refusing to write the output inside the input" in log, log
        assert "Nothing was changed." in log, log
        assert blackbox.tree_of(download) == before
        assert not library.exists()

    def test_an_opus_folder_inside_the_library_is_refused_too(self, sandbox,
                                                              tmp_path):
        download = tmp_path / "download"
        (download / "Album").mkdir(parents=True)
        library = tmp_path / "library"
        opus = library / "opus"
        done = sandbox.run("ingest-music", download, library, opus)
        log = done.stdout + done.stderr
        assert done.returncode == 1, log
        assert "Refusing to write the output inside the input" in log, log
        assert "Nothing was changed." in log, log
        assert not library.exists()


class TestTheCoverIsReadAsTheImageItsNameSays:
    """ImageMagick picks its decoder from a file's content unless told, so the
    cover's operand names the coder: a cover.jpg holding SVG is read as a
    JPEG, and fails as one, rather than drawn."""

    def test_the_cover_operand_names_its_coder(self, music, tmp_path):
        calls = tmp_path / "convert-calls"
        music.with_tool("convert", 'printf "%%s\\n" "$@" >> %s\n%s'
                        % (calls, _CONVERT))
        music.ingest()
        operands = calls.read_text().splitlines()
        assert [operand for operand in operands
                if operand.endswith("cover.jpg")
                and operand.startswith("jpeg:")], operands


class TestAFirstRun:
    """Everything is encoded and converted, and says so."""

    @pytest.fixture
    def log(self, music):
        return music.ingest()

    @pytest.mark.parametrize("label,expected,pattern", _PHASES,
                             ids=[case[0] for case in _PHASES])
    def test_a_phase_counts_every_item_it_was_handed_exactly_once(
            self, log, label, expected, pattern):
        """One denominator, and a counter that reaches it without overshooting -
        so nothing is lost, doubled, or counted against the wrong phase."""
        _assert_phase(log, label, expected, pattern)

    def test_the_track_that_is_not_lossless_is_named_as_such(self, log):
        assert log.count("Skip (not lossless, mp3): Album/lossy.flac") == 1, log

    def test_each_cover_is_reported_by_what_was_done_to_it(self, log):
        assert log.count("Cover to AVIF") == 1, log
        assert log.count("Cover copied as it is") == 1, log

    @pytest.mark.parametrize("label,expected", [
        ("Lossless tracks", "3"), ("  encoded", "2"),
        ("  not lossless", "1"), ("Cover images", "2"),
        ("  to AVIF", "1"), ("  copied unchanged", "1"),
        ("  remuxed to MKV", "1")])
    def test_the_stats_categories_add_up_to_what_was_found(self, log, label,
                                                           expected):
        assert _stat(log, label) == expected, log

    def test_the_encoded_audio_is_reported_as_a_real_time_speed_up(self, log):
        assert re.search(r"^Real-time speedup: ", log, re.MULTILINE), log

    def test_the_closing_block_is_printed_exactly_once(self, log):
        assert len(re.findall(r"^Lossless tracks: ", log, re.MULTILINE)) == 1

    def test_a_category_with_nothing_in_it_is_left_out(self, log):
        """So an ordinary run does not carry rows about work it never did."""
        assert _stat(log, "ALAC renamed") is None, log

    def test_the_cue_is_kept_rather_than_dropped(self, music, log):
        """What the embed COUNTED is `test_ingest_music.py`'s: the write is a
        function call, and real mutagen honestly refuses a stub-encoded flac."""
        assert _stat(log, "  dropped, no flac") is None, log
        assert (music.library / "Album" / "one.cue").is_file()


class TestTheSameInputAgain:
    """Every track is already in the library, and the run says that per track
    instead of falling silent for the length of an encode phase."""

    @pytest.fixture
    def run(self, music):
        music.ingest()
        return music, music.ingest()

    def test_the_encoding_phase_still_counts_every_track(self, run):
        _, log = run
        _assert_phase(log, "re-run encoding", 3, _PHASES[0][2])

    def test_nothing_is_encoded_twice_and_the_tracks_are_reported_up_to_date(
            self, run):
        _, log = run
        assert _stat(log, "  encoded") is None, log
        assert _stat(log, "  up to date") == "2", log

    def test_an_unchanged_input_leaves_the_library_unchanged(self, run):
        """Rather than growing a second copy of everything that is not a flac -
        the sidecars, the covers - under whatever name the download uses."""
        music, _ = run
        before = blackbox.tree_of(music.library)
        music.ingest()
        assert blackbox.tree_of(music.library) == before


class TestALibraryRenamedInBetween:
    """The folder and the files in it, which is what every run does to itself
    since it ends by cleaning the library's names. Nothing in the library is
    called what this command called it any more, and none of it may therefore be
    made a second time. Three things have to hold, and without all three the
    library doubles on every run:

      * the tracks are recognised by the download recorded inside each flac;
      * everything else is written to the folder those flacs are in, not to a
        folder named after the download;
      * a sidecar or cover the library already holds under a cleaned name is
        recognised by its CONTENT, a rip log carrying no tag to know it by.
    """

    @pytest.fixture
    def run(self, music, tmp_path):
        (tmp_path / "download" / "Album" / "rip.log").write_text("rip log")
        music.ingest()
        album = music.library / "Album"
        (album / "one.flac").rename(album / "renamed one.flac")
        (album / "two.flac").rename(album / "renamed two.flac")
        (album / "rip.log").rename(album / "Renamed Rip.log")
        album.rename(music.library / "Cleaned Album")
        return music, music.ingest()

    def test_it_is_still_recognised_track_for_track(self, run):
        _, log = run
        assert _stat(log, "  up to date") == "2", log
        assert _stat(log, "  encoded") is None, log

    def test_it_says_it_wrote_to_the_folder_the_library_uses(self, run):
        music, log = run
        assert _stat(log, "Folders re-homed") == "1", log
        assert not (music.library / "Album").exists()

    def test_nothing_is_made_a_second_time(self, run):
        """Counted rather than named: the run ends by cleaning the library's
        names, so what the tracks are CALLED afterwards is that cleaning's
        business and how many there are is this case's."""
        music, _ = run
        assert len(list(music.library.rglob("*.flac"))) == 2
        assert len(list(music.library.rglob("back.jpg"))) == 1
        assert len(list(music.library.rglob("*.avif"))) == 1

    def test_a_sidecar_renamed_in_the_library_is_not_copied_again(self, run):
        """The rip log carries no tag, so it is its bytes that say the library
        already has it - and the run says it found it there."""
        music, log = run
        assert len(list(music.library.rglob("*.log"))) == 1, log
        assert _stat(log, "Already in library") == "1", log

    def test_the_cue_the_rename_orphaned_is_dropped_and_said_to_be(self, run):
        """Its flac has a different name and there is more than one flac in the
        folder, so nothing can be paired - which is the case the pruning is for,
        and it is counted rather than silent."""
        _, log = run
        assert _stat(log, "  dropped, no flac") == "1", log

    def test_and_from_there_it_settles(self, run):
        """The run after a rename puts back what the rename orphaned - the cue
        sheet, whose flac is called what it was again once the names have been
        cleaned - and every run after that changes nothing at all."""
        music, _ = run
        music.ingest()
        settled = blackbox.tree_of(music.library)
        music.ingest()
        assert blackbox.tree_of(music.library) == settled


# --- -k resolution ------------------------------------------------------------
# The one part this command keeps: the source's bit depth and sample rate.
# The cases want the encoder's own call rather than the run's report, so the
# ffmpeg stand-in records every call it is handed, argument by argument.

# The ffprobe stub, with the source above the hearing threshold: the default
# run's -ar is then the threshold itself, which is what the kept run's encode
# must not carry. The stream also states a bitrate, because without one the
# Opus copy step would judge the library flac "unknown" and copy it instead of
# encoding it - and the encoder's call is what that step's cases assert on.
_FFPROBE_HIRES = r"""
file=""; prev=""
for a in "$@"; do [[ "$prev" == "-i" ]] && file="$a"; prev="$a"; done
[[ -n "$file" ]] || file="${!#}"
[[ -e "$file" ]] || exit 0
json=0; entries=""; prev=""
for a in "$@"; do
  [[ "$a" == "json" ]] && json=1
  [[ "$prev" == "-show_entries" ]] && entries="$a"
  prev="$a"
done
if (( json )); then
  printf '{"streams":[{"codec_name":"flac","codec_type":"audio",'
  printf '"sample_rate":"96000","bit_rate":"1000000"}],'
  printf '"format":{"duration":"600.000"}}\n'
  exit 0
fi
if [[ "$entries" == format_tags=* ]]; then
  wanted="${entries#format_tags=}"
  recorded="$(sed -n 2p -- "$file")"
  [[ "$recorded" == "$wanted="* ]] && printf '%s\n' "${recorded#*=}"
  exit 0
fi
[[ -n "$entries" ]] && { echo "600.000"; exit 0; }
printf 'duration=600.000\n'
"""


@pytest.fixture
def keep_music(sandbox, tmp_path):
    """One 96 kHz lossless track, the whole release, with the encoder's calls
    recorded: the -k cases are about what the ffmpeg is told, and the Opus
    case is about what it is not told differently."""
    for tool in _TOOLS:
        if shutil.which(tool) is None:
            conftest.missing_host_tool(
                "the host has no %s: the command needs it to report what is "
                "asserted here" % tool)
    calls = sandbox.work / "ffmpeg-calls"
    sandbox.with_tool("ffprobe", _FFPROBE_HIRES)
    sandbox.with_tool("ffmpeg", _FFMPEG_CALLS(str(calls)))
    sandbox.with_tool("convert", _CONVERT)
    sandbox.with_tool("mkvmerge", _MKVMERGE)
    sandbox.with_tools("fdupes", "beet", "mkvpropedit", "mkvextract")

    download = tmp_path / "download"
    album = download / "Album"
    album.mkdir(parents=True)
    (album / "one.flac").write_text("one")

    library = tmp_path / "library"
    opus = tmp_path / "libraryopus"

    def run(*options, positionals=None):
        done = sandbox.run("ingest-music", "-j", "2", *options,
                           *(positionals or (download, library, opus)),
                           timeout=900)
        return done

    sandbox.ffmpeg_calls = calls
    sandbox.download = download
    sandbox.library = library
    sandbox.opus = opus
    sandbox.ingest = run
    return sandbox


def _FFMPEG_CALLS(calls_file: str) -> str:
    """The ffmpeg stand-in, with every call it gets recorded, argument by
    argument, ahead of what it does with it."""
    return (
        "printf '%s\\n' \"$*\" >> " + calls_file + "\n"
        + "out=\"${!#}\"; meta=\"\"; prev=\"\"\n"
        + "for a in \"$@\"; do [[ \"$prev\" == \"-metadata\" ]] && "
        + "meta=\"$a\"; prev=\"$a\"; done\n"
        + "printf 'flac\\n%s\\n' \"$meta\" > \"$out\"\n")


def _recorded_calls(calls_file) -> list[list[str]]:
    """The ffmpeg calls the stand-in recorded, one list of arguments a line."""
    if not calls_file.exists():
        return []
    return [line.split(" ") for line in calls_file.read_text().splitlines()
            if line.strip()]


def _encodes(calls: list[list[str]], suffix: str) -> list[list[str]]:
    """The recorded calls that ENCODE a file ending in ``suffix``: an output
    that is the last argument and an audio codec in the call, so a probe about
    such a file is not one of them."""
    return [argv for argv in calls
            if len(argv) > 1 and argv[-1].endswith(suffix)
            and "-c:a" in argv]


class TestTheKeptResolution:
    """-k resolution keeps the source's bit depth and sample rate: the track
    is still re-encoded to FLAC - that is what makes the formats uniform - but
    -sample_fmt and -ar are left out of the call, and the Opus copies made from
    the finished library afterwards are not affected."""

    def test_the_default_encode_is_sixteen_bit_capped_at_the_threshold(
            self, keep_music):
        done = keep_music.ingest()
        assert done.returncode == 0, done.stdout + done.stderr
        argv = _encodes(_recorded_calls(keep_music.ffmpeg_calls),
                        ".flac")[0]
        assert argv[argv.index("-sample_fmt") + 1] == "s16", argv
        # The source is 96 kHz: the default run's rate is the threshold, not
        # the source's.
        assert argv[argv.index("-ar") + 1] == "48000", argv

    def test_the_part_leaves_the_sample_arguments_out_of_the_call(
            self, keep_music):
        done = keep_music.ingest("-k", "resolution")
        assert done.returncode == 0, done.stdout + done.stderr
        argv = _encodes(_recorded_calls(keep_music.ffmpeg_calls),
                        ".flac")[0]
        assert "-sample_fmt" not in argv, argv
        assert "-ar" not in argv, argv

    def test_a_bare_k_keeps_every_part(self, keep_music):
        """There is one part today, so the bare flag and the named one are the
        same run - and they stay the same when the later parts land."""
        done = keep_music.ingest("-k")
        assert done.returncode == 0, done.stdout + done.stderr
        argv = _encodes(_recorded_calls(keep_music.ffmpeg_calls),
                        ".flac")[0]
        assert "-sample_fmt" not in argv, argv
        assert "-ar" not in argv, argv

    def test_the_track_is_still_reencoded_to_flac(self, keep_music):
        """What the part takes away is the normalisation, not the re-encode:
        the call is the same FLAC encode with two arguments less."""
        done = keep_music.ingest("-k", "resolution")
        assert done.returncode == 0, done.stdout + done.stderr
        argv = _encodes(_recorded_calls(keep_music.ffmpeg_calls),
                        ".flac")[0]
        assert argv[argv.index("-c:a") + 1] == "flac", argv
        assert argv[argv.index("-f") + 1] == "flac", argv

    def test_the_opus_call_is_unchanged_by_the_part(self, keep_music):
        """The Opus copies are made from the finished library by the convert-
        audio child, which the run hands the same arguments in both modes -
        so the encoder's call is the one the modes cannot move. The copies are
        deleted between the runs, because a second run over the first's
        finished copies would skip them as up to date and hand the case one
        call rather than two."""
        keep_music.ingest()
        calls = _recorded_calls(keep_music.ffmpeg_calls)
        default_opus = _encodes(calls, ".opus")[0]
        shutil.rmtree(keep_music.opus)
        keep_music.ingest("-k", "resolution")
        calls = _recorded_calls(keep_music.ffmpeg_calls)
        kept_opus = _encodes(calls, ".opus")[-1]
        assert default_opus == kept_opus
        assert kept_opus[kept_opus.index("-c:a") + 1] == "libopus"
        assert kept_opus[kept_opus.index("-b:a") + 1] == "120k"


class TestTheWordAfterTheFlag:
    """-k claims the word after it only when that word is a part, the way -t
    claims a number: a part is claimed and the folder is left, a folder is
    left and the run is a bare -k, and a clustered flag claims nothing at
    all - because the word the option sits in is not a part."""

    def test_a_folder_that_is_not_the_part_stays_a_folder(self, keep_music):
        """The bare -k keeps every part, and the download directory is still
        the first positional: the run has its three folders and takes
        nothing away."""
        done = keep_music.ingest("-k")
        assert done.returncode == 0, done.stdout + done.stderr
        argv = _encodes(_recorded_calls(keep_music.ffmpeg_calls),
                        ".flac")[0]
        assert "-ar" not in argv, argv
        assert (keep_music.library / "Album" / "one.flac").is_file()

    def test_the_named_part_is_claimed_and_the_folder_is_left(self,
                                                              keep_music):
        done = keep_music.ingest("-k", "resolution")
        assert done.returncode == 0, done.stdout + done.stderr
        argv = _encodes(_recorded_calls(keep_music.ffmpeg_calls),
                        ".flac")[0]
        assert "-ar" not in argv, argv
        assert (keep_music.library / "Album" / "one.flac").is_file()

    def test_the_long_form_says_the_same(self, keep_music):
        done = keep_music.ingest("--keep=resolution")
        assert done.returncode == 0, done.stdout + done.stderr
        argv = _encodes(_recorded_calls(keep_music.ffmpeg_calls),
                        ".flac")[0]
        assert "-ar" not in argv, argv
        assert (keep_music.library / "Album" / "one.flac").is_file()

    def test_a_clustered_flag_keeps_the_folder_and_the_verbosity(
            self, keep_music):
        """-kv: the k claims nothing, because the cluster's own word is not a
        part, and the v is the verbosity - so the run is a bare -k, said
        more."""
        done = keep_music.ingest("-kv")
        assert done.returncode == 0, done.stdout + done.stderr
        argv = _encodes(_recorded_calls(keep_music.ffmpeg_calls),
                        ".flac")[0]
        assert "-ar" not in argv, argv
        assert (keep_music.library / "Album" / "one.flac").is_file()

    def test_a_download_folder_literally_named_resolution(self, keep_music):
        """The one name the flag would eat: written ./resolution, the word is
        not the part, so the folder is the first positional after all."""
        download = keep_music.work / "resolution"
        (download / "Album").mkdir(parents=True)
        (download / "Album" / "one.flac").write_text("one")
        done = keep_music.ingest("-k",
                                 positionals=("./resolution",
                                              str(keep_music.library),
                                              str(keep_music.opus)))
        assert done.returncode == 0, done.stdout + done.stderr
        assert (keep_music.library / "Album" / "one.flac").is_file()

    def test_a_download_folder_literally_named_downloads(self, keep_music):
        """The second part name the flag would eat, the same way: written
        ./downloads, the folder is the first positional after all."""
        download = keep_music.work / "downloads"
        (download / "Album").mkdir(parents=True)
        (download / "Album" / "one.flac").write_text("one")
        done = keep_music.ingest("-k",
                                 positionals=("./downloads",
                                              str(keep_music.library),
                                              str(keep_music.opus)))
        assert done.returncode == 0, done.stdout + done.stderr
        assert (keep_music.library / "Album" / "one.flac").is_file()

    def test_and_the_bare_name_is_the_part_not_the_folder(self, keep_music):
        """The same folder written resolution, with no ./ to keep it from
        being the part: the word goes to -k, the run's first positional is
        the library - which does not exist yet, and the refusal names it."""
        download = keep_music.work / "resolution"
        (download / "Album").mkdir(parents=True)
        (download / "Album" / "one.flac").write_text("one")
        done = keep_music.ingest("-k", "resolution",
                                 positionals=(str(keep_music.library),
                                              str(keep_music.opus)))
        log = done.stdout + done.stderr
        assert done.returncode == 1, log
        assert 'Directory "%s" does not exist' % str(keep_music.library) \
            in log, log


class TestKeptDownloads:
    """-k downloads: the run works on a copy of the download folder: the
    de-duplication, the disc split and the renames happen on it, and the
    folder comes out of the run as it went in. The measures the part keeps out
    of the folder are the real ones - the real fdupes, not the stand-in - so
    what the part is is what they do not do to it."""

    @pytest.fixture
    def kept(self, sandbox, tmp_path):
        """The -k resolution cases' release plus a two-disc rip and a pair of
        identical files, the two things the folder is otherwise changed by."""
        for tool in _TOOLS + ("fdupes",):
            if shutil.which(tool) is None:
                conftest.missing_host_tool(
                    "the host has no %s: the command needs it to report what "
                    "is asserted here" % tool)
        calls = sandbox.work / "ffmpeg-calls"
        sandbox.with_tool("ffprobe", _FFPROBE_HIRES)
        sandbox.with_tool("ffmpeg", _FFMPEG_CALLS(str(calls)))
        sandbox.with_tool("convert", _CONVERT)
        sandbox.with_tool("mkvmerge", _MKVMERGE)
        sandbox.with_tools("beet", "mkvpropedit", "mkvextract")

        download = tmp_path / "download"
        album = download / "Album"
        album.mkdir(parents=True)
        (album / "one.flac").write_bytes(b"one")
        (album / "one-copy.flac").write_bytes(b"one")
        rip = download / "Rip"
        rip.mkdir()
        for stem in ("disc1", "disc2"):
            (rip / (stem + ".flac")).write_bytes(("rip " + stem).encode())
            (rip / (stem + ".cue")).write_text('FILE "%s.flac" WAVE\n' % stem)

        library = tmp_path / "library"
        opus = tmp_path / "libraryopus"

        def run(*options, positionals=None):
            done = sandbox.run("ingest-music", "-j", "2", *options,
                               *(positionals or (download, library, opus)),
                               timeout=900)
            return done

        sandbox.ffmpeg_calls = calls
        sandbox.download = download
        sandbox.library = library
        sandbox.opus = opus
        sandbox.ingest = run
        return sandbox

    def _tree(self, folder):
        return blackbox.tree_of(folder)

    def test_the_kept_folder_is_the_one_it_went_in_as(self, kept):
        before = self._tree(kept.download)
        done = kept.ingest("-k", "downloads")
        assert done.returncode == 0, done.stdout + done.stderr
        assert self._tree(kept.download) == before

    def test_the_default_run_is_the_changing_one(self, kept):
        """The contrast: the same folder without the part is de-duplicated and
        split in place, which is what the part keeps out of it."""
        done = kept.ingest()
        assert done.returncode == 0, done.stdout + done.stderr
        after = self._tree(kept.download)
        assert "Rip/disc1/disc1.flac" in after, after
        assert "Rip/disc1.flac" not in after, after
        assert sum(name.startswith("Album/") and name.endswith(".flac")
                   for name in after) == 1, after

    def test_the_split_happens_in_the_library_not_the_folder(self, kept):
        done = kept.ingest("-k", "downloads")
        assert done.returncode == 0, done.stdout + done.stderr
        # The subfolders the split made are name-cleaned to their disc numbers
        # before the assertion can name them.
        assert (kept.library / "Rip" / "1" / "disc1.flac").is_file()
        assert (kept.library / "Rip" / "2" / "disc2.flac").is_file()
        # The de-duplication happened on the copy the run worked on: the
        # library holds the one track the duplicate came down to.
        assert sum(name.startswith("Album/") and name.endswith(".flac")
                   for name in self._tree(kept.library)) == 1

    def test_a_bare_k_keeps_every_part(self, kept):
        """The folder here, and the source's resolution in the encode the
        resolution part is about."""
        before = self._tree(kept.download)
        done = kept.ingest("-k")
        assert done.returncode == 0, done.stdout + done.stderr
        assert self._tree(kept.download) == before
        argv = _encodes(_recorded_calls(kept.ffmpeg_calls), ".flac")[0]
        assert "-ar" not in argv, argv

    def test_the_multi_disc_folder_is_refused_when_the_copy_cannot_be_made(
            self, kept, tmp_path):
        """The split happens in the scratch copy of the tree: when the copy
        cannot be made, the folders it would split are refused rather than
        ingested flat, and the download is left as it was."""
        marker = tmp_path / "rsync-first"
        kept.with_tool("rsync",
                       'if [ ! -f "%s" ]; then touch "%s"; exit 1; fi\n'
                       'exec %s "$@"' % (marker, marker,
                                         shutil.which("rsync")))
        before = self._tree(kept.download)
        done = kept.ingest("-k", "downloads")
        log = done.stdout + done.stderr
        assert done.returncode == 0, log
        assert 'Refused "Rip"' in log, log
        assert "Folders refused" in log, log
        assert self._tree(kept.download) == before
        assert (kept.library / "Album" / "one.flac").is_file()
        assert not (kept.library / "Rip").exists()


class TestAnUnknownPart:
    """A part the run will not keep is refused with the page, like a bad
    option. The part the value names is checked the one place that knows
    the command's parts; what reaches it that is not one of them says so
    and changes nothing."""

    def test_the_long_form_refuses_a_part_the_command_does_not_keep(
            self, sandbox):
        done = sandbox.run("ingest-music", "--keep=covers")
        log = done.stdout + done.stderr
        assert done.returncode == 1, log
        assert 'Option --keep does not take "covers"' in log, log
        assert "Usage:" in log, log
        assert "Nothing was changed." in log

    def test_and_a_list_with_an_unknown_part_in_it(self, sandbox):
        """The value is a comma list of parts, and one not among them is the
        refusal: the page is printed and nothing is changed."""
        done = sandbox.run("ingest-music", "--keep=resolution,covers")
        log = done.stdout + done.stderr
        assert done.returncode == 1, log
        assert 'Option --keep does not take "resolution,covers"' in log, log
        assert "Nothing was changed." in log


class TestAReRunInTheOtherMode:
    """A track ingested with the source's resolution kept is recognised, when
    the next run is in the default mode, by the INGESTSOURCE tag the encoder
    wrote into it - and the tag is the download the track came from, which
    does not change with the mode the run is in. The library is renamed in
    between, because the name is the check the tag exists to replace: a
    recognition that survived the rename is the tag's, not the name's."""

    @pytest.mark.parametrize("first,second", [
        (["-k", "resolution"], []),
        ([], ["-k", "resolution"]),
    ], ids=["kept then default", "default then kept"])
    def test_the_other_mode_recognises_what_this_one_ingested(
            self, keep_music, first, second):
        done = keep_music.ingest(*first)
        assert done.returncode == 0, done.stdout + done.stderr
        album = keep_music.library / "Album"
        (album / "one.flac").rename(album / "renamed one.flac")
        album.rename(keep_music.library / "Cleaned Album")
        done = keep_music.ingest(*second)
        log = done.stdout + done.stderr
        assert done.returncode == 0, log
        assert _stat(log, "  up to date") == "1", log
        assert _stat(log, "  encoded") is None, log
        assert len(list(keep_music.library.rglob("*.flac"))) == 1


# The beets stand-in, with every call it gets recorded, argument by argument,
# so the config a run hands it is asserted from the record rather than from a
# stub that says nothing.
def _BEET_CALLS(log_file: str) -> str:
    return "printf '%s\\n' \"$*\" >> " + log_file + "\n"


def _beet_calls(log_file) -> list[list[str]]:
    """The beets calls the stand-in recorded, one list of arguments a line."""
    if not log_file.exists():
        return []
    return [line.split(" ") for line in log_file.read_text().splitlines()
            if line.strip()]


class TestTheBeetsConfig:
    """The tagging phase's config: the shipped file by default, another with
    -c or the beetsConfig setting - the option winning where both name one -
    and none at all with -o, which is a run beets never runs."""

    def test_the_shipped_file_is_the_config_a_run_is_not_given(self,
                                                               keep_music):
        log = keep_music.work / "beet-calls"
        keep_music.with_tool("beet", _BEET_CALLS(str(log)))
        done = keep_music.ingest()
        assert done.returncode == 0, done.stdout + done.stderr
        calls = _beet_calls(log)
        assert len(calls) == 1, calls
        config = calls[0][calls[0].index("-c") + 1]
        assert config.endswith("medialib/config/beets.yaml"), config
        assert os.path.isfile(config), config

    def test_the_config_the_option_names_is_the_one_beets_runs_with(self,
                                                                    keep_music):
        log = keep_music.work / "beet-calls"
        keep_music.with_tool("beet", _BEET_CALLS(str(log)))
        config = keep_music.work / "another.yaml"
        config.write_text("directory: .\n")
        done = keep_music.ingest("-c", str(config))
        assert done.returncode == 0, done.stdout + done.stderr
        calls = _beet_calls(log)
        assert len(calls) == 1, calls
        assert calls[0][calls[0].index("-c") + 1] == str(config), calls

    def test_the_setting_the_option_does_not_name_is_the_one_beets_runs_with(
            self, keep_music, tmp_path):
        log = keep_music.work / "beet-calls"
        keep_music.with_tool("beet", _BEET_CALLS(str(log)))
        config = tmp_path / "the-setting.yaml"
        config.write_text("directory: .\n")
        settings_file = tmp_path / "settings"
        settings_file.write_text("beetsConfig=%s\n" % config)
        done = keep_music.run("ingest-music", "-j", "2",
                              str(keep_music.download), str(keep_music.library),
                              str(keep_music.opus),
                              env=dict(os.environ,
                                       CLI_SETTINGS_FILE=str(settings_file)),
                              timeout=900)
        assert done.returncode == 0, done.stdout + done.stderr
        calls = _beet_calls(log)
        assert len(calls) == 1, calls
        assert calls[0][calls[0].index("-c") + 1] == str(config), calls

    def test_the_option_wins_over_the_setting(self, keep_music, tmp_path):
        log = keep_music.work / "beet-calls"
        keep_music.with_tool("beet", _BEET_CALLS(str(log)))
        from_the_option = keep_music.work / "the-option.yaml"
        from_the_option.write_text("directory: .\n")
        from_the_setting = tmp_path / "the-setting.yaml"
        from_the_setting.write_text("directory: .\n")
        settings_file = tmp_path / "settings"
        settings_file.write_text("beetsConfig=%s\n" % from_the_setting)
        done = keep_music.run("ingest-music", "-j", "2", "-c",
                              str(from_the_option),
                              str(keep_music.download), str(keep_music.library),
                              str(keep_music.opus),
                              env=dict(os.environ,
                                       CLI_SETTINGS_FILE=str(settings_file)),
                              timeout=900)
        assert done.returncode == 0, done.stdout + done.stderr
        calls = _beet_calls(log)
        assert len(calls) == 1, calls
        assert calls[0][calls[0].index("-c") + 1] == str(from_the_option), calls

    def test_a_config_that_is_not_there_is_refused_before_any_work(
            self, keep_music):
        missing = keep_music.work / "not-there.yaml"
        done = keep_music.ingest("-c", str(missing))
        assert done.returncode == 1, done.stdout + done.stderr
        assert ('The beets config "%s" does not exist.' % missing) in done.stderr
        assert "-c, --beets-config" in done.stderr
        assert not keep_music.ffmpeg_calls.exists(), "the run did work"

    def test_an_offline_run_never_runs_beet(self, keep_music):
        """The config is not even asked for: an offline run handed a config
        that is not there still runs, and beets is never started."""
        log = keep_music.work / "beet-calls"
        keep_music.with_tool("beet", _BEET_CALLS(str(log)))
        done = keep_music.ingest("-o", "-c",
                                 str(keep_music.work / "not-there.yaml"))
        assert done.returncode == 0, done.stdout + done.stderr
        assert not log.exists(), "beet ran on an offline run"


@pytest.fixture
def no_beet(sandbox, tmp_path):
    """A PATH that finds no beets, and every other tool the preflight asks for.

    Built as an allow-list rather than by dropping directories from the host's
    PATH, the way the no-mkvtoolnix cases build theirs: on this host beets
    lives in the same directory as the coreutils, so dropping it would take
    those with it.
    """
    sandbox.with_tool("ffprobe", _FFPROBE_HIRES)
    sandbox.with_tool("ffmpeg", _FFMPEG_CALLS(str(tmp_path / "ffmpeg-calls")))
    sandbox.with_tool("convert", _CONVERT)
    sandbox.with_tool("mkvmerge", _MKVMERGE)
    sandbox.with_tools("fdupes", "mkvpropedit", "mkvextract")
    sandbox.linking(*_ORDINARY).narrow()
    return sandbox


class TestOfflineNeedsNoBeet:
    """-o is a run beets never runs, so a machine without it can ingest: the
    preflight asks for the tool only when the tagging phase will run."""

    def test_the_path_really_finds_no_beet(self, no_beet):
        import shutil
        assert shutil.which("beet", path=no_beet.path) is None

    def test_an_online_run_is_refused_on_the_missing_beet(self, no_beet,
                                                          tmp_path):
        download = tmp_path / "download"
        download.mkdir()
        done = no_beet.run("ingest-music", str(download),
                           str(tmp_path / "library"),
                           str(tmp_path / "libraryopus"))
        assert done.returncode == 1, done.stdout + done.stderr
        assert "Cannot run ingest-music: it needs a tool" in done.stderr
        assert "beet" in done.stderr
        assert "Nothing to do" not in done.stderr

    def test_an_offline_run_gets_past_the_preflight(self, no_beet, tmp_path):
        """Past the preflight, and to the input check the online run never
        reaches: the empty folder is the same in both, and only the beets
        question differs between them."""
        download = tmp_path / "download"
        download.mkdir()
        done = no_beet.run("ingest-music", "-o", str(download),
                           str(tmp_path / "library"),
                           str(tmp_path / "libraryopus"))
        assert done.returncode == 1, done.stdout + done.stderr
        assert 'Nothing to do: "%s" is empty.' % download in done.stderr
