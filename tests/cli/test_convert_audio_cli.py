"""`convert-audio` as a process: a folder of audio re-encoded to Opus.

Two claims that only a whole run can make. The progress counter has to count one
flat queue of jobs - whole files and individual chunks together - which a
per-file view of the code cannot check; and the paths on the command line belong
to the caller, not to the directory the run chdirs into.
"""

from __future__ import annotations

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

# A silencedetect probe reports one silence straddling the middle of its window,
# so a cut lands at every ideal boundary and the chunk count is the core count.
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
  c=$(awk -v s="$ss" -v l="$t" "BEGIN{printf \\"%.3f\\", s + l/2}")
  printf "silencedetect @ silence_start: %s\\n" \\
    "$(awk -v c="$c" "BEGIN{printf \\"%.3f\\", c-0.4}")" >&2
  printf "silencedetect @ silence_end: %s | silence_duration: 0.800\\n" \\
    "$(awk -v c="$c" "BEGIN{printf \\"%.3f\\", c+0.4}")" >&2
  exit 0
fi
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
                re.findall(r"^\[(\d+)(?:/(\d+))?\] [^:\n]+:", done.stdout,
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
        return re.sub(r"^\[\d+(?:/\d+)?\] ", "", done.stdout, flags=re.M)

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

    def test_and_its_footer_leaves_out_what_it_did_not_do(self, convert,
                                                          tmp_path):
        """Nothing was encoded and nothing split, so there is no audio to
        total, no speed to give and no re-join to time."""
        self._said(convert, tmp_path)
        said = self._said(convert, tmp_path)
        for row in ("Total duration:", "Real-time speedup:",
                    "Post-conversion:"):
            assert row not in said
        assert "Files:" in said


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
        assert "[chunk 1/4]" in first.stdout
        joined = outputs / "long.opus"
        before = (blackbox.tree_of(outputs), joined.stat().st_mtime_ns,
                  joined.read_bytes())

        again = convert.run("convert-audio", "-j", 4, inputs, outputs)
        assert again.returncode == 0, again.stdout + again.stderr
        assert "Up to date, skipping: long.m4a\n" in again.stdout
        assert "[chunk" not in again.stdout
        assert "Joining" not in again.stdout
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
