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


class TestTheProgressCounter:
    """One flat denominator for every line - the job total, counting chunks as
    jobs - and a counter that reaches it exactly once.

    It read "[45/4]" once: the chunk encoder kept a local total of a file's
    chunks and the progress line printed that, so a chunk's line carried the
    file's chunk count as its denominator. Cores and the split threshold are
    pinned so the job counts do not depend on the host's CPU count.
    """

    @pytest.fixture
    def convert(self, sandbox, tmp_path):
        sandbox.with_tool("ffmpeg", _FFMPEG)
        sandbox.with_tool("ffprobe", _FFPROBE)
        # The bitrate lookup decides nothing here - .m4a is always transcoded -
        # and no cover art or metadata is in play.
        sandbox.with_tool("jq", "echo 0")
        sandbox.with_tools("rsync", "mkvextract")
        sandbox.with_tool("convert", 'out="${!#}"; out="${out%\\>}"; : > "$out"')
        return sandbox

    def _lines(self, convert, tmp_path, names):
        inputs = tmp_path / "in"
        outputs = tmp_path / "out"
        inputs.mkdir()
        for name in names:
            (inputs / name).write_text("")
        done = convert.run("convert-audio", "-j", 4, "-s", 10, inputs, outputs)
        return [(int(n), int(total)) for n, total in re.findall(
            r"^\[(\d+)/(\d+)\] Converting:", done.stdout, re.M)]

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
        assert {total for _, total in lines} == {jobs}
        assert len(lines) == jobs
        assert max(n for n, _ in lines) == jobs
        assert not [n for n, total in lines if n > total]


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
        done = convert.run("convert-audio", "-j", 2, "-s", 0, "-c", "in", "out",
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

        assert convert.run("convert-audio", "-j", 2, "-s", 0, "-c", "in", "out",
                           cwd=relative).returncode == 0
        assert convert.run("convert-audio", "-j", 2, "-s", 0, "-c",
                           absolute / "in", absolute / "out",
                           cwd=tmp_path).returncode == 0
        assert blackbox.tree_of(relative / "out") == blackbox.tree_of(
            absolute / "out")
