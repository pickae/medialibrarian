"""How this host spells an ImageMagick call.

The whole module is one question - is the version 6 name still there - and the
answer changes the argv every image conversion in the package hands over, so it
is worth cases of its own rather than being read off six call sites.

The lookup is a PATH lookup, so a case says what is on PATH by putting
something there: a directory of its own, ahead of everything.
"""

from __future__ import annotations

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
