"""`convert-audio` as a process: a folder of audio re-encoded to Opus.

Two claims that only a whole run can make. The progress counter has to count one
flat queue of jobs - whole files and individual chunks together - which a
per-file view of the code cannot check; and the paths on the command line belong
to the caller, not to the directory the run chdirs into.
"""

from __future__ import annotations

import os
import re
import shutil

import pytest

from tests import blackbox

pytestmark = pytest.mark.stubbed

# ffprobe answers a long duration for an input whose name says "long", so a file
# deterministically splits, and a short one otherwise, so it stays whole.
_FFPROBE = '''
for a in "$@"; do [[ "$a" == "-show_chapters" ]] && exit 0; done
last="${!#}"
for a in "$@"; do
  if [[ "$a" == *duration* ]]; then
    if [[ "$last" == *long* ]]; then echo 300.000; else echo 5.000; fi
    exit 0
  fi
done
exit 0
'''

# A silencedetect probe reports one silence the way ffmpeg prints it, the start
# on one line and the end on the next. It sits a third of the nudge window past
# the middle of the probe window - the ideal boundary - so a cut found there can
# only have come from the silence and not from the arithmetic. The probe window
# is the ideal boundary give or take the nudge window and a 2s guard, which is
# how the nudge window is read back out of its length.
# Any other call that seeks is a chunk encode, and notes where it starts in a
# "seeks" file beside this stub.
# The awk program is single-quoted: macOS's bash 3.2 does not keep \" intact
# inside a "$(...)", so a double-quoted program there reached awk broken and
# the probe printed no times at all.
# Any other call creates its output - the last argument - except the "-f null -"
# sink, where "-" is stdout and not a file.
_FFMPEG = '''
args=("$@"); ss=""; t=""; sd=0; i=0
while [[ $i -lt ${#args[@]} ]]; do
  a="${args[$i]}"
  case "$a" in
    -ss) ss="${args[$((i+1))]}";;
    -t)  t="${args[$((i+1))]}";;
    *silencedetect*) sd=1;;
  esac
  i=$((i+1))
done
if [[ $sd -eq 1 ]]; then
  awk -v s="$ss" -v l="$t" 'BEGIN {
    c = s + l/2 + (l-4)/2/3
    p = "[Parsed_silencedetect_0 @ 0x55d5c8a0] "
    printf "%ssilence_start: %.3f\\n", p, c - 0.4
    printf "%ssilence_end: %.3f | silence_duration: 0.800\\n", p, c + 0.4
  }' >&2
  exit 0
fi
[[ -n "$ss" ]] && echo "$ss" >> "$(dirname "$0")/seeks"
last="${args[$((${#args[@]}-1))]}"
[[ "$last" != "-" ]] && : > "$last"
exit 0
'''


def _stubbed(sandbox):
    sandbox.with_tool("ffmpeg", _FFMPEG)
    sandbox.with_tool("ffprobe", _FFPROBE)
    # The bitrate lookup decides nothing here - .m4a is always transcoded - and
    # no cover art or metadata is in play.
    sandbox.with_tool("jq", "echo 0")
    sandbox.with_tools("rsync", "mkvextract")
    sandbox.with_tool("convert", 'out="${!#}"; out="${out%\\>}"; : > "$out"')
    return sandbox


class TestTheProgressCounter:
    """One flat denominator for every line - the job total, counting chunks as
    jobs - and a counter that reaches it exactly once.

    It read "[45/4]" once: the chunk encoder kept a local total of a file's
    chunks and the progress line printed that, so a chunk's line carried the
    file's chunk count as its denominator. The denominator is absent for as long
    as the queue is still being planned - a line printed before the total is
    known carries the count alone - so a line's denominator, where it has one,
    is what this asserts on. The pool is pinned, and a long file is large
    enough that the run cuts it however many threads the host has, so the job
    counts do not depend on the host's CPU count.
    """

    @pytest.fixture
    def convert(self, sandbox):
        return _stubbed(sandbox)

    def _lines(self, convert, tmp_path, names):
        inputs = tmp_path / "in"
        outputs = tmp_path / "out"
        inputs.mkdir()
        for name in names:
            # Whether a file is cut is settled from its size, not a probe, so a
            # "long" one is twenty gigabytes - sparse, so it costs no disk - and
            # a short one is small enough never to be worth cutting.
            with open(inputs / name, "wb") as handle:
                handle.truncate(20_000_000_000 if name.startswith("long")
                                else 100_000)
        done = convert.run("convert-audio", "-j", 4, inputs, outputs)
        return [(int(n), int(total) if total else None) for n, total in
                re.findall(r"^\[(\d+)(?:/(\d+))?\] [^:\n]+:", done.stderr,
                           re.M)]

    @pytest.mark.parametrize("names,jobs", [
        # three short files stay whole
        (["a.mp3", "b.mp3", "c.mp3"], 3),
        # two long files at four chunks each, plus two whole files
        (["long1.m4a", "long2.m4a", "short1.mp3", "short2.mp3"], 10),
        # two long files and nothing whole
        (["long1.m4a", "long2.m4a"], 8),
    ], ids=["nothing chunked", "mixed", "all chunked"])
    def test_every_line_counts_the_same_queue(self, convert, tmp_path, names,
                                              jobs):
        lines = self._lines(convert, tmp_path, names)
        assert len(lines) == jobs
        assert max(n for n, _total in lines) == jobs
        denominators = {total for _n, total in lines if total is not None}
        assert denominators <= {jobs}
        assert not [n for n, total in lines
                    if total is not None and n > total]


class TestWhereALongFileIsCut:
    """Each cut lands in the silence the window probe found near it, not at
    the arithmetic boundary - so a chunk never starts mid-word."""

    def test_every_chunk_but_the_first_starts_in_a_silence(self, sandbox,
                                                           tmp_path):
        convert = _stubbed(sandbox)
        inputs = tmp_path / "in"
        inputs.mkdir()
        with open(inputs / "long.m4a", "wb") as handle:
            handle.truncate(20_000_000_000)
        done = convert.run("convert-audio", "-j", 4, inputs, tmp_path / "out")
        assert done.returncode == 0, done.stdout + done.stderr

        # 300 s over four cores: the ideal boundaries are 75 s apart and the
        # nudge window is half that, so each silence is 12.5 s past one.
        starts = sorted(float(line) for line in
                        (convert.bin / "seeks").read_text().split())
        assert starts == pytest.approx([0.0, 87.5, 162.5, 237.5], abs=0.01)


class TestWhatEachLineSays:
    """A counted line says what its job does to the file, so a re-run over a
    finished tree does not read as every file being converted again.

    The stubbed probe states no bitrate at all, which the run keeps as it would
    a small file - so an .mp3 here is one the run leaves alone, and an .m4a one
    it always transcodes.
    """

    @pytest.fixture
    def convert(self, sandbox):
        return _stubbed(sandbox)

    def _said(self, convert, tmp_path, *options):
        inputs = tmp_path / "in"
        inputs.mkdir(exist_ok=True)
        for name in ("book.m4a", "spoken.mp3"):
            (inputs / name).write_bytes(b"\0" * 1000)
        done = convert.run("convert-audio", "-j", 1, *options, inputs,
                           tmp_path / "out")
        assert done.returncode == 0, done.stdout + done.stderr
        return re.sub(r"^\[\d+(?:/\d+)?\] ", "", done.stderr, flags=re.M)

    def test_a_file_that_is_encoded_is_converting(self, convert, tmp_path):
        assert "\nConverting: book.m4a\n" in self._said(convert, tmp_path)

    def test_one_left_alone_says_so_and_why(self, convert, tmp_path):
        said = self._said(convert, tmp_path)
        assert "\nBitrate unknown, skipping: spoken.mp3\n" in said
        assert "Converting: spoken.mp3" not in said

    def test_under_c_the_same_file_is_copying(self, convert, tmp_path):
        said = self._said(convert, tmp_path, "-c")
        assert "\nBitrate unknown, copying: spoken.mp3\n" in said

    def test_a_rerun_over_its_own_output_is_up_to_date(self, convert,
                                                       tmp_path):
        self._said(convert, tmp_path)
        said = self._said(convert, tmp_path)
        assert "\nUp to date, skipping: book.m4a\n" in said
        assert "Converting:" not in said

    def _split_run(self, convert, tmp_path, *options):
        """A run over a tree with one file long enough that it is cut up."""
        inputs = tmp_path / "in"
        inputs.mkdir(exist_ok=True)
        (inputs / "book.m4a").write_bytes(b"\0" * 1000)
        with open(inputs / "long.m4a", "wb") as handle:
            handle.truncate(20_000_000_000)
        done = convert.run("convert-audio", "-j", 4, *options, inputs,
                           tmp_path / "out")
        assert done.returncode == 0, done.stdout + done.stderr
        convert.stdout = done.stdout
        return done.stderr

    def test_a_run_that_split_a_file_times_the_re_join(self, convert,
                                                       tmp_path):
        assert re.search(r"^Post-conversion: +\d", self._split_run(
            convert, tmp_path), re.M)

    def test_and_its_footer_leaves_out_what_it_did_not_do(self, convert,
                                                          tmp_path):
        """The tree was split once already, but this time nothing was encoded
        and nothing split, so there is no audio to total, no speed to give and
        no re-join to time."""
        self._split_run(convert, tmp_path)
        said = self._split_run(convert, tmp_path)
        assert "Up to date, skipping: long.m4a" in said
        for row in ("Total duration:", "Real-time speedup:",
                    "Post-conversion:"):
            assert row not in said
        assert "Files:" in said

    def test_under_q_a_split_run_that_went_well_prints_nothing(self, convert,
                                                               tmp_path):
        """Not the counted lines, the split and the re-join, nor the footer."""
        assert self._split_run(convert, tmp_path, "-q") == ""
        assert convert.stdout == ""
        assert (tmp_path / "out" / "long.opus").is_file()


class TestAnOutputPathThatIsAFile:
    """An output path naming an existing file is refused before anything runs;
    it once reached the output folder's makedirs and died in a traceback."""

    def test_it_is_refused_and_nothing_is_touched(self, sandbox, tmp_path):
        convert = _stubbed(sandbox)
        inputs, output = tmp_path / "in", tmp_path / "out.opus"
        (inputs / "sub").mkdir(parents=True)
        (inputs / "track.m4a").write_bytes(b"\0" * 1000)
        (inputs / "sub" / "track.mp3").write_bytes(b"\1" * 1000)
        output.write_bytes(b"not a folder")

        def snapshot():
            return [(name, (inputs / name).is_file()
                     and (inputs / name).read_bytes())
                    for name in blackbox.tree_of(inputs)]
        before = snapshot()

        done = convert.run("convert-audio", inputs, output)
        assert done.returncode == 1, done.stdout + done.stderr
        assert "Traceback" not in done.stderr
        errors = [line for line in done.stderr.splitlines()
                  if line.startswith("Error:")]
        assert errors == ['Error: the output folder "%s" is not a folder.'
                          % output]
        assert snapshot() == before
        assert output.read_bytes() == b"not a folder"


class TestRelativePaths:
    """The run chdirs into its input folder, so a relative path from the command
    line once resolved against that folder rather than the caller's and no output
    tree was built at all."""

    @pytest.fixture
    def convert(self, sandbox):
        if not shutil.which("rsync"):
            pytest.fail("rsync is missing: the image copy is a step under test")
        if not shutil.which("jq"):
            pytest.fail("jq is missing: the bitrate probe decides what -c copies")
        return sandbox.with_media_stubs()

    def _fixture(self, root):
        """One file of each kind the run treats differently, in the root and in a
        sub-folder, so the mirrored tree is exercised too: an .m4a is always
        transcoded, an .mp3 below the threshold is copied verbatim by -c, and a
        .jpg that is nobody's sidecar is copied by the image pass."""
        (root / "sub").mkdir(parents=True)
        for name in ("track.m4a", "sub/track.m4a", "sub/spoken.mp3",
                     "sub/cover.jpg"):
            (root / name).write_text("")
        return root

    def test_a_relative_pair_is_read_from_the_callers_directory(self, convert,
                                                                tmp_path):
        parent = tmp_path / "parent"
        self._fixture(parent / "in")
        done = convert.run("convert-audio", "-j", 2, "-c", "in", "out",
                           cwd=parent)
        assert done.returncode == 0, done.stderr

        assert (parent / "out" / "sub").is_dir()
        assert (parent / "out" / "track.opus").is_file()
        assert (parent / "out" / "sub" / "track.opus").is_file()
        assert (parent / "out" / "sub" / "spoken.mp3").is_file()
        assert (parent / "out" / "sub" / "cover.jpg").is_file()
        # The input folder is the caller's, not one inside itself.
        assert not (parent / "in" / "in").exists()

    def test_and_gives_the_same_tree_as_the_absolute_spelling(self, convert,
                                                              tmp_path):
        relative = tmp_path / "relative"
        absolute = tmp_path / "absolute"
        self._fixture(relative / "in")
        self._fixture(absolute / "in")

        assert convert.run("convert-audio", "-j", 2, "-c", "in", "out",
                           cwd=relative).returncode == 0
        assert convert.run("convert-audio", "-j", 2, "-c",
                           absolute / "in", absolute / "out",
                           cwd=tmp_path).returncode == 0
        assert blackbox.tree_of(relative / "out") == blackbox.tree_of(
            absolute / "out")


class TestTheRunLeavesNothingInTheTempFolder:
    """The list of renames held back to avoid an overwrite is kept in a temp
    file of the run's own, and the run takes it away again - including after
    reporting a skip from it. One left per run piles up in /tmp for good."""

    @pytest.fixture
    def run(self, sandbox, tmp_path, tmp_path_factory):
        convert = _stubbed(sandbox)
        # The worker pool's forkserver puts its socket in TMPDIR, and a socket
        # path has to fit in about 100 bytes: tmp_path, named after the test,
        # can be too deep for that.
        temp = tmp_path_factory.mktemp("t")
        inputs = tmp_path / "in"
        inputs.mkdir()
        (inputs / "track.mp3").write_bytes(b"\0" * 100_000)
        # A cover already holds the name the .jpeg would be renamed to.
        (inputs / "cover.jpeg").write_text("jpeg")
        (inputs / "cover.jpg").write_text("jpg")
        done = convert.run("convert-audio", "-j", 2, inputs, tmp_path / "out",
                           env=dict(os.environ, TMPDIR=str(temp)))
        assert done.returncode == 0, done.stdout + done.stderr
        return temp, done.stdout + done.stderr

    def test_the_skip_is_still_reported(self, run):
        _, log = run
        assert "Safety: skipped 1 rename(s) to avoid overwrite" in log

    def test_the_temp_folder_is_empty_afterwards(self, run):
        temp, _ = run
        assert list(temp.iterdir()) == []


class TestARerunOverASplitFile:
    """A long file the first run cut up and joined is up to date the second
    time: nothing is planned, cut or written again."""

    def test_it_is_skipped_whole_and_its_output_is_untouched(self, sandbox,
                                                             tmp_path):
        convert = _stubbed(sandbox)
        inputs, outputs = tmp_path / "in", tmp_path / "out"
        inputs.mkdir()
        with open(inputs / "long.m4a", "wb") as handle:
            handle.truncate(20_000_000_000)
        first = convert.run("convert-audio", "-j", 4, inputs, outputs)
        assert first.returncode == 0, first.stdout + first.stderr
        assert "[chunk 1/4]" in first.stderr
        joined = outputs / "long.opus"
        before = (blackbox.tree_of(outputs), joined.stat().st_mtime_ns,
                  joined.read_bytes())

        again = convert.run("convert-audio", "-j", 4, inputs, outputs)
        assert again.returncode == 0, again.stdout + again.stderr
        assert "Up to date, skipping: long.m4a\n" in again.stderr
        assert "[chunk" not in again.stderr
        assert "Joining" not in again.stderr
        assert (blackbox.tree_of(outputs), joined.stat().st_mtime_ns,
                joined.read_bytes()) == before


# Twenty hours of mono at 44.1 kHz: past the thirteen and a half one WAVE can
# carry, so xHE-AAC can only take it in pieces.
_FFPROBE_TWENTY_HOURS = '''
for a in "$@"; do [[ "$a" == "-show_chapters" ]] && exit 0; done
for a in "$@"; do
  case "$a" in
    *sample_rate*) echo 44100; exit 0;;
    *channels*) echo 1; exit 0;;
    *duration*) echo 72000.000; exit 0;;
  esac
done
exit 0
'''


class TestAFileTooLongForOneXheAacEncode:
    """-a turns the chunking off, so a book past the WAVE ceiling reaches the
    whole-file encode - which refuses it rather than writing its first
    thirteen hours and calling that the book."""

    def test_the_run_fails_and_writes_no_m4a(self, sandbox, tmp_path):
        convert = _stubbed(sandbox)
        convert.with_tool("ffprobe", _FFPROBE_TWENTY_HOURS)
        # Present, so the run gets as far as asking it; it must never run.
        convert.with_tool("exhale", ': > "$(dirname "$0")/exhale-ran"')
        inputs, outputs = tmp_path / "in", tmp_path / "out"
        inputs.mkdir()
        (inputs / "book.m4a").write_bytes(b"\0" * 1000)

        done = convert.run("convert-audio", "-j", 1, "-e", "xheaac", "-a",
                           inputs, outputs)
        assert done.returncode == 1, done.stdout + done.stderr
        assert "too long for exhale to encode whole" in done.stderr
        assert not list(outputs.rglob("*.m4a"))
        assert not (convert.bin / "exhale-ran").exists()

    def test_under_q_the_error_is_all_that_is_printed(self, sandbox,
                                                      tmp_path):
        convert = _stubbed(sandbox)
        convert.with_tool("ffprobe", _FFPROBE_TWENTY_HOURS)
        convert.with_tool("exhale", ': > "$(dirname "$0")/exhale-ran"')
        inputs = tmp_path / "in"
        inputs.mkdir()
        (inputs / "book.m4a").write_bytes(b"\0" * 1000)

        done = convert.run("convert-audio", "-q", "-j", 1, "-e", "xheaac",
                           "-a", inputs, tmp_path / "out")
        assert done.returncode == 1, done.stdout + done.stderr
        assert "ERROR: too long for exhale to encode whole" in done.stderr
        assert done.stdout == "", done.stdout


class TestAdaptiveRefusesTheGlobals:
    """-a decides the channel and the bitrate per file, so a global -m or -b
    would contradict it: refused at the option line, before a file is looked
    at and before the output folder is made."""

    @pytest.fixture
    def convert(self, sandbox, tmp_path):
        convert = _stubbed(sandbox)
        convert.inputs = tmp_path / "in"
        convert.inputs.mkdir()
        (convert.inputs / "track.m4a").write_bytes(b"\0" * 1000)
        convert.outputs = tmp_path / "out"
        return convert

    def _refused(self, convert, *options):
        before = blackbox.tree_of(convert.inputs)
        done = convert.run("convert-audio", *options, convert.inputs,
                           convert.outputs)
        assert done.returncode == 1, done.stdout + done.stderr
        assert "error: -a (adaptive) cannot be combined with -m or -b." \
            in done.stderr
        assert blackbox.tree_of(convert.inputs) == before
        assert not convert.outputs.exists()

    def test_a_global_mono_is_refused(self, convert):
        self._refused(convert, "-a", "-m")

    def test_a_global_bitrate_is_refused(self, convert):
        self._refused(convert, "-a", "-b", "64")


class TestXheAacWithoutItsEncoder:
    """xHE-AAC is the one codec ffmpeg cannot produce at all: the run needs
    the separate exhale binary, and a machine without it is refused up front,
    naming the tool and the way out - rather than failing per file."""

    def test_the_run_refuses_naming_the_encoder_and_the_way_out(self, sandbox,
                                                                tmp_path):
        convert = _stubbed(sandbox).narrow()
        inputs, outputs = tmp_path / "in", tmp_path / "out"
        inputs.mkdir()
        (inputs / "track.m4a").write_bytes(b"\0" * 1000)
        before = blackbox.tree_of(inputs)
        done = convert.run("convert-audio", "-e", "xheaac", inputs, outputs,
                           env=dict(os.environ, SKIP_TOOL_PREFLIGHT=""))
        assert done.returncode == 1, done.stdout + done.stderr
        assert "Cannot run convert-audio (-e xheaac): it needs an xHE-AAC " \
            "encoder, and this machine has none." in done.stderr
        assert "exhale" in done.stderr
        assert "(-e opus)" in done.stderr
        assert blackbox.tree_of(inputs) == before
        assert not outputs.exists()
