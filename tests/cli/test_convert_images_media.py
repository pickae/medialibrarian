"""convert-images against a real ImageMagick, one format at a time.

The three formats -o writes disagree about everything an encoder call carries -
the bit depth, and the NAME and the DIRECTION of the effort setting - and none
of that can be checked by reading the argv back. ImageMagick answers an effort
value it does not accept in one of two ways, and both are silent: WebP refuses
the write, and JPEG XL clamps. With stderr discarded, either one reads as a run
in which nothing could be converted.

So the cases below hand the real tool every value the mapping can emit and
require a file back. `heic:speed=10` is exactly the bug this tier exists to
catch: it is one past the top of the range, it is refused rather than clamped,
and it leaves a zero-byte file - a whole tree of them, on a run that named it.

Skipped, not failed, where the build cannot write a format: a host without
libjxl is a normal host, and the point is to test what it CAN do.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from medialib.cli import convert_images as ci
from medialib.lib import imagemagick

# Asked once, at collection: an unreadable listing means there is no real
# ImageMagick to test against - including the Windows box where `convert` is
# the unrelated system32 utility, which answers nothing this can parse.
_MODES = imagemagick.format_modes()

pytestmark = [
    pytest.mark.media,
    pytest.mark.skipif(
        not _MODES,
        reason="tier D needs an ImageMagick whose -list format can be read"),
]

_WRITABLE = sorted(name for name in ci.FORMATS if "w" in _MODES.get(name, ""))


def _needs(image_format: str):
    """Skip a case for a format this build was not given the delegate for."""
    return pytest.mark.skipif(
        image_format not in _WRITABLE,
        reason="this ImageMagick cannot write %s (needs %s)"
               % (image_format, imagemagick.DELEGATES.get(image_format, "?")))


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    """One small real image, made by the tool that will re-encode it."""
    path = tmp_path_factory.mktemp("source") / "page.png"
    subprocess.run(imagemagick.convert_argv(
        ["-size", "96x72", "plasma:fractal", str(path)]), check=True)
    return path


def _encode(source, out, arguments) -> int:
    return subprocess.run(imagemagick.convert_argv(arguments),
                          stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode


def _identify(path, spec="%m %z"):
    done = subprocess.run(
        imagemagick.identify_argv(["-format", spec, str(path)]),
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if done.returncode != 0:
        return ""
    return done.stdout.decode("utf-8", "replace").strip()


def _run(image_format, speed=ci.DEFAULT_SPEED):
    """A Run whose options main() would have settled for these flags."""
    return ci.Run("/in", "/out", "/counters", {
        "crop": False,
        "format": image_format,
        "quality": 55,
        "effortLevel": ci.FORMATS[image_format].level(speed),
        "fuzzCommand": "10%",
        "maxResCommand": "%dx%d>" % (ci.UNBOUNDED_EDGE, ci.UNBOUNDED_EDGE),
    }, 1)


class TestEveryFormatReallyEncodes:
    @pytest.mark.parametrize("image_format", sorted(ci.FORMATS),
                             ids=sorted(ci.FORMATS))
    def test_the_default_call_produces_a_file_of_that_format(
            self, image_format, source, tmp_path, request):
        request.applymarker(_needs(image_format))
        out = tmp_path / ("page." + image_format)
        argv = _run(image_format)._encode_arguments(str(source), str(out))
        assert _encode(source, out, argv) == 0
        assert out.is_file() and out.stat().st_size > 0
        assert _identify(out, "%m").lower() == image_format

    @pytest.mark.parametrize("image_format", sorted(ci.FORMATS),
                             ids=sorted(ci.FORMATS))
    def test_the_depth_asked_for_is_the_depth_carried(self, image_format,
                                                      source, tmp_path,
                                                      request):
        """WebP has 8 bits and no more; the other two are asked for 10 so flat
        gradients do not band, and must come back with more than 8."""
        request.applymarker(_needs(image_format))
        out = tmp_path / ("depth." + image_format)
        argv = _run(image_format)._encode_arguments(str(source), str(out))
        assert _encode(source, out, argv) == 0
        depth = int(_identify(out, "%z") or 0)
        if ci.FORMATS[image_format].depth == 8:
            assert depth == 8
        else:
            assert depth > 8


class TestEverySpeedTheMappingCanEmit:
    """The regression guard for the range. Every -s the check now accepts,
    through the real encoder, for every format this build can write."""

    @pytest.mark.parametrize("speed", range(ci.MAX_SPEED + 1))
    @pytest.mark.parametrize("image_format", sorted(ci.FORMATS),
                             ids=sorted(ci.FORMATS))
    def test_the_encoder_accepts_it_and_writes_something(
            self, image_format, speed, source, tmp_path, request):
        request.applymarker(_needs(image_format))
        out = tmp_path / ("s%d.%s" % (speed, image_format))
        argv = _run(image_format, speed)._encode_arguments(str(source),
                                                           str(out))
        assert _encode(source, out, argv) == 0, argv
        assert out.is_file() and out.stat().st_size > 0, argv


class TestTheTopOfTheRangeIsWhereItIs:
    """Why MAX_SPEED is 9 and not 10, asked of the encoder rather than of a
    manual page - so a libheif that widens the range shows up as a failure
    here and the constant can be raised with evidence."""

    @pytest.mark.skipif("avif" not in _WRITABLE,
                        reason="this ImageMagick cannot write avif")
    def test_the_top_speed_encodes(self, source, tmp_path):
        out = tmp_path / "top.avif"
        assert _encode(source, out, [
            "-format", "avif", "-quality", "55",
            "-define", "heic:speed=%d" % ci.MAX_SPEED,
            str(source), str(out)]) == 0
        assert out.stat().st_size > 0

    @pytest.mark.skipif("avif" not in _WRITABLE,
                        reason="this ImageMagick cannot write avif")
    def test_one_past_the_top_does_not(self, source, tmp_path):
        """The failure the option check exists to prevent: not an exception,
        not a clamp - a zero-byte file and a silent non-zero exit."""
        out = tmp_path / "over.avif"
        code = _encode(source, out, [
            "-format", "avif", "-quality", "55",
            "-define", "heic:speed=%d" % (ci.MAX_SPEED + 1),
            str(source), str(out)])
        left_behind = out.stat().st_size if out.exists() else 0
        assert code != 0 or left_behind == 0


class TestTheDelegateProbeAgreesWithTheTool:
    def test_what_the_probe_calls_writable_really_writes(self, source,
                                                         tmp_path):
        """The probe is only worth having if its answer matches what happens,
        so the two are compared on this host's own build."""
        for image_format in sorted(ci.FORMATS):
            out = tmp_path / ("agree." + image_format)
            argv = _run(image_format)._encode_arguments(str(source), str(out))
            wrote = _encode(source, out, argv) == 0 and out.exists() \
                and out.stat().st_size > 0
            assert wrote == (image_format in _WRITABLE), image_format

    def test_a_format_the_probe_refuses_is_refused_before_any_work(self,
                                                                   capsys):
        """HEIC where it is listed r-- : readable, never writable, and the one
        case that proves presence in the listing is not the test."""
        if "w" in _MODES.get("heic", ""):
            pytest.skip("this build writes HEIC, so it is not the example")
        if "heic" not in _MODES:
            pytest.skip("this build does not know HEIC at all")
        assert imagemagick.require_format("convert-images", "heic") == 1
        assert "cannot write HEIC" in capsys.readouterr().err


def test_the_environment_really_is_the_one_being_claimed():
    """A guard on the tier itself: these cases mean nothing if they quietly
    ran against no tool at all."""
    assert _MODES, "collected without a readable -list format"
    assert os.environ.get("SKIP_TOOL_PREFLIGHT", "") == "" or _WRITABLE
