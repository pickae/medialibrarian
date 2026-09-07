"""How this host spells an ImageMagick call.

The whole module is one question - is the version 6 name still there - and the
answer changes the argv every image conversion in the package hands over, so it
is worth cases of its own rather than being read off six call sites.

The lookup is a PATH lookup, so a case says what is on PATH by putting
something there: a directory of its own, ahead of everything.
"""

from __future__ import annotations

import subprocess

import pytest

from medialib.lib import imagemagick

pytestmark = pytest.mark.fs


@pytest.fixture
def only(tmp_path, monkeypatch):
    """Put exactly these names on PATH, and nothing else at all."""
    def install(*names):
        directory = tmp_path / "bin"
        directory.mkdir(exist_ok=True)
        for name in names:
            entry = directory / name
            entry.write_text("#!/bin/sh\n", encoding="ascii")
            entry.chmod(0o755)
            # Windows resolves a bare name through PATHEXT, and a file with no
            # suffix is not a command there whatever its mode bits say.
            (directory / (name + ".bat")).write_text("@echo off\n",
                                                     encoding="ascii")
        monkeypatch.setenv("PATH", str(directory))
        return directory

    return install


class TestWhereBothExist:
    def test_the_old_name_is_used_while_it_is_there(self, only):
        """Which is every ImageMagick 6, and every 7 that still installs its
        compatibility wrappers - so nothing about an existing install changes."""
        only("convert", "identify", "magick")
        assert imagemagick.convert_argv(["a.jpg", "a.avif"]) == [
            "convert", "a.jpg", "a.avif"]
        assert imagemagick.identify_argv(["-format", "%w"]) == [
            "identify", "-format", "%w"]


class TestVersionSevenOnly:
    def test_a_conversion_becomes_a_bare_magick(self, only):
        """`magick` with no operation IS convert - that is the v7 spelling, not
        an abbreviation of one."""
        only("magick")
        assert imagemagick.convert_argv(["a.jpg", "a.avif"]) == [
            "magick", "a.jpg", "a.avif"]

    def test_and_identify_keeps_its_name_as_the_operation(self, only):
        """Dropping the word would run the CONVERSION instead, which with
        `identify`'s own arguments writes a file where a measurement was
        wanted."""
        only("magick")
        assert imagemagick.identify_argv(["-format", "%w %h", "-"]) == [
            "magick", "identify", "-format", "%w %h", "-"]


class TestNeither:
    def test_the_call_is_spelled_the_way_the_refusal_names_it(self, only):
        """A host with no ImageMagick at all has already been refused by the
        preflight; if one gets here anyway, the failure should name the tool
        the install hint tells the user to get."""
        only()
        assert imagemagick.convert_argv(["a.jpg"]) == ["convert", "a.jpg"]
        assert imagemagick.identify_argv(["a.jpg"]) == ["identify", "a.jpg"]


class TestThePreflightSpecs:
    pytestmark = pytest.mark.pure

    def test_either_spelling_satisfies_the_preflight(self):
        from medialib.lib import tooldeps
        assert tooldeps.tool_note(
            imagemagick.CONVERT_SPEC.split("|")[0]).startswith(
                "image conversion")
        assert imagemagick.CONVERT_SPEC.split("|") == ["convert", "magick"]
        assert imagemagick.IDENTIFY_SPEC.split("|") == ["identify", "magick"]

    def test_the_v6_name_comes_first_so_a_refusal_names_it(self):
        """The install hints are written for `convert` and `identify`; a
        refusal that led with `magick` would name a binary neither hint
        mentions."""
        assert imagemagick.CONVERT_SPEC.startswith("convert")
        assert imagemagick.IDENTIFY_SPEC.startswith("identify")


class TestTheArgumentsAreNotTouched:
    def test_an_empty_argument_list_is_the_bare_command(self, only):
        only("convert")
        assert imagemagick.convert_argv([]) == ["convert"]

    def test_a_generator_is_taken_as_a_sequence(self, only):
        only("convert")
        assert imagemagick.convert_argv(str(n) for n in range(3)) == [
            "convert", "0", "1", "2"]


# One real `-list format` listing, trimmed to the interesting rows: the header
# that must not be read as a format, a name carrying the `*`, a format that can
# be read but NOT written (HEIC), and the three this package writes. Recorded
# from ImageMagick 7.1.2-30 rather than composed, because the whole point of
# the parse is that it survives what the tool really prints.
REAL_LISTING = """   Format  Mode  Description
-------------------------------------------------------------------------------
      3FR  r--   Hasselblad CFV/H3D39II Raw Format (0.22.2-Release)
        A* rw+   Raw alpha samples
     AVIF  rw+   AV1 Image File Format (1.23.1)
     HEIC  r--   High Efficiency Image Format (1.23.1)
      JXL* rw+   JPEG XL (ISO/IEC 18181) (libjxl 0.12.0)
     JPEG  rw-   Joint Photographic Experts Group JFIF format (9.0)
      PNG  rw-   Portable Network Graphics (1.6.44)
     WEBP* rw+   WebP Image Format (libwebp 1.6.0 [0210])
"""


@pytest.fixture
def listing(monkeypatch):
    """Answer `-list format` with this text and this exit code."""
    def install(stdout=REAL_LISTING, returncode=0):
        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(
                argv, returncode, stdout.encode("utf-8"), b"")
        monkeypatch.setattr(imagemagick.subprocess, "run", fake_run)

    return install


class TestFormatModes:
    """What a BUILD can do, as against what the binary is called."""

    def test_every_row_is_read_with_its_mode(self, listing):
        listing()
        modes = imagemagick.format_modes()
        assert modes["avif"] == "rw+"
        assert modes["heic"] == "r--"
        assert modes["jpeg"] == "rw-"

    def test_a_name_carrying_the_star_keeps_only_the_name(self, listing):
        """`WEBP*` is the format WEBP; the star is a flag of the listing."""
        listing()
        assert imagemagick.format_modes()["webp"] == "rw+"
        assert "webp*" not in imagemagick.format_modes()

    def test_the_header_row_is_not_a_format(self, listing):
        listing()
        assert "format" not in imagemagick.format_modes()

    def test_a_failed_listing_says_nothing_rather_than_nothing_works(
            self, listing):
        """The distinction the callers depend on: "cannot tell" is empty, and
        empty is never read as "this build writes nothing"."""
        listing(stdout=REAL_LISTING, returncode=1)
        assert imagemagick.format_modes() == {}

    def test_an_unreadable_listing_is_also_just_empty(self, listing):
        listing(stdout="something else entirely\n")
        assert imagemagick.format_modes() == {}

    def test_a_missing_binary_is_empty_and_not_an_exception(self, monkeypatch):
        def explode(argv, **kwargs):
            raise OSError(2, "No such file or directory")
        monkeypatch.setattr(imagemagick.subprocess, "run", explode)
        assert imagemagick.format_modes() == {}


class TestRequireFormat:
    """The refusal that turns a whole failed run into one message up front."""

    def test_a_writable_format_is_allowed_through_silently(self, listing,
                                                           capsys):
        listing()
        assert imagemagick.require_format("convert-images", "avif") == 0
        assert capsys.readouterr().err == ""

    @pytest.mark.parametrize("image_format,delegate",
                             [("webp", "libwebp"), ("jxl", "libjxl"),
                              ("avif", "libheif")])
    def test_a_format_the_build_lacks_is_refused_naming_its_delegate(
            self, listing, capsys, image_format, delegate):
        """The delegate and not the program: somebody hitting this already has
        ImageMagick, so "install ImageMagick" is no help to them."""
        listing(stdout="\n".join(
            line for line in REAL_LISTING.splitlines()
            if not line.strip().lower().startswith(image_format)) + "\n")
        assert imagemagick.require_format("convert-images", image_format) == 1
        refusal = capsys.readouterr().err
        assert image_format.upper() in refusal
        assert delegate in refusal
        assert "Nothing was changed." in refusal

    def test_a_readable_but_unwritable_format_is_refused_for_writing(
            self, listing, capsys):
        """HEIC is the real case: present in every libheif build and still
        never writable, so presence alone cannot be the test."""
        listing()
        assert imagemagick.require_format("convert-images", "heic") == 1
        assert "cannot write HEIC" in capsys.readouterr().err

    def test_that_same_format_is_fine_to_read(self, listing, capsys):
        """Which is what -r asks about, and why the direction is a parameter."""
        listing()
        assert imagemagick.require_format("convert-images", "heic",
                                          writing=False) == 0
        assert capsys.readouterr().err == ""

    def test_an_unwritable_format_is_refused_for_reading_by_name(
            self, listing, capsys):
        listing(stdout="\n".join(
            line for line in REAL_LISTING.splitlines()
            if not line.strip().lower().startswith("jxl")) + "\n")
        assert imagemagick.require_format("convert-images", "jxl",
                                          writing=False) == 1
        assert "cannot read JXL" in capsys.readouterr().err

    def test_skipping_the_preflight_skips_this_too(self, listing, capsys):
        """The tool preflight and this check are one decision: a run told not
        to probe its tools must not probe their delegates either, or every
        stubbed test would refuse."""
        listing(stdout="")
        assert imagemagick.require_format("convert-images", "webp",
                                          skip_preflight=True) == 0
        assert capsys.readouterr().err == ""

    def test_a_listing_that_cannot_be_read_does_not_refuse(self, listing,
                                                           capsys):
        listing(returncode=1)
        assert imagemagick.require_format("convert-images", "webp") == 0
        assert capsys.readouterr().err == ""

    def test_every_format_the_package_writes_has_a_delegate_to_name(self):
        from medialib.lib import enums
        for codec in enums.IMAGE_CODECS:
            assert codec in imagemagick.DELEGATES
