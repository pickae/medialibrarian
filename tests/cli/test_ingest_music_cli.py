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

    def run():
        # The suite turns the length check off below the media tier (stubs would
        # otherwise report every output as truncated); this case IS about a
        # truncated output, and its stubs report real lengths, so it asks for the
        # check back.
        done = sandbox.run("ingest-music", "-j", "2", download, library, opus,
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
