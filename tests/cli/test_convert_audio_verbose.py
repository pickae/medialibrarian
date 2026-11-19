"""`convert-audio -v`: the steps a run used to prove only by leaving temp files
behind, now said instead.

The encoders are the stubs the other convert-audio cases use, so an output is
an empty file and nothing can be written into it; what is checked is what a run
says about the chapters a source carried, the cover it embedded, and where it
cut a long file - and that the same run, without -v, says none of it.
"""

from __future__ import annotations

import re

import pytest

from tests.cli.test_convert_audio_cli import _stubbed

pytestmark = pytest.mark.stubbed

# ffprobe answers a source whose name holds "chaptered" with two chapters of its
# own, and nothing else. A "long" file is the run's own duration to plan against;
# an .m4a states the rate that gets it transcoded, and no cover art is in play.
_FFPROBE = '''
last="${!#}"
for a in "$@"; do
  if [[ "$a" == "-show_chapters" ]]; then
    if [[ "$last" == *chaptered* ]]; then
      printf 'chapters.chapter.0.start_time="0.000000"\\n'
      printf 'chapters.chapter.1.start_time="60.000000"\\n'
    fi
    exit 0
  fi
  if [[ "$a" == *duration* ]]; then
    if [[ "$last" == *long* ]]; then echo 300.000; else echo 5.000; fi
    exit 0
  fi
done
if [[ "$*" == *"-print_format json"* ]]; then
  case "$last" in
    *.m4a|*.m4b)
      printf '{"format": {}, "streams": [{%s}]}' \\
        '"codec_name": "aac", "codec_type": "audio", "bit_rate": "128000", "channels": 2'
      ;;
  esac
  exit 0
fi
exit 0
'''


@pytest.fixture
def convert(sandbox):
    _stubbed(sandbox)
    sandbox.with_tool("ffprobe", _FFPROBE)
    return sandbox


def _make(inputs, name, size=1000):
    inputs.mkdir(exist_ok=True)
    with open(inputs / name, "wb") as handle:
        handle.truncate(size)


def _run(convert, tmp_path, *options, expect=0):
    done = convert.run("convert-audio", *options, tmp_path / "in",
                       tmp_path / "out")
    assert done.returncode == expect, done.stdout + done.stderr
    return done.stderr


class TestTheChaptersASourceCarried:
    """-v says what a source's chapters were, per file - the count, and that
    they were the source's own - or that there were none. -k used to prove it by
    leaving the OGM file beside the output; the line is the proof now."""

    def test_under_v_a_source_says_its_chapters(self, convert, tmp_path):
        _make(tmp_path / "in", "chaptered.m4a")
        _make(tmp_path / "in", "plain.m4a")
        said = _run(convert, tmp_path, "-j", "1", "-v")
        assert "chaptered.m4a: 2 chapter(s) - the source's own" in said, said
        assert "plain.m4a: no chapters - the source carried none" in said, said

    def test_without_v_the_chapters_are_not_said(self, convert, tmp_path):
        _make(tmp_path / "in", "chaptered.m4a")
        _make(tmp_path / "in", "plain.m4a")
        said = _run(convert, tmp_path, "-j", "1")
        assert "chaptered.m4a: 2 chapter(s)" not in said, said
        assert "plain.m4a: no chapters" not in said, said


class TestTheCoverAFileIsGiven:
    """-v says where a file's cover came from - a sidecar image, the source's
    own art, or a search - with its size and whether it was recompressed. A
    sidecar sharing the track's name is the cover, and the line says so."""

    def test_under_v_a_sidecar_cover_is_said(self, convert, tmp_path):
        inputs = tmp_path / "in"
        _make(inputs, "cover.m4a")
        (inputs / "cover.jpg").write_bytes(b"x" * 100)
        said = _run(convert, tmp_path, "-j", "1", "-v")
        assert ("cover.m4a: cover from a sidecar image, 100 B, "
                "kept at its own size") in said, said

    def test_without_v_the_cover_is_not_said(self, convert, tmp_path):
        inputs = tmp_path / "in"
        _make(inputs, "cover.m4a")
        (inputs / "cover.jpg").write_bytes(b"x" * 100)
        said = _run(convert, tmp_path, "-j", "1")
        assert "cover from a sidecar image" not in said, said


class TestASplitFile:
    """-v says how many chunks a long file was cut into, and where. The cut
    points are the silence the planner found; a run without -v only reports the
    join, not the cut."""

    def test_under_v_a_split_file_says_its_chunks_and_cuts(self, convert,
                                                           tmp_path):
        _make(tmp_path / "in", "long.m4a", size=20_000_000_000)
        said = _run(convert, tmp_path, "-j", "4", "-v")
        assert re.search(r"long\.m4a: cut into \d+ chunk\(s\), at \d",
                         said), said

    def test_without_v_the_split_is_not_said(self, convert, tmp_path):
        _make(tmp_path / "in", "long.m4a", size=20_000_000_000)
        said = _run(convert, tmp_path, "-j", "4")
        assert not re.search(r"long\.m4a: cut into \d+ chunk\(s\)", said), said
