"""Which kind of host this is.

Three answers and a rule for everything else, which is the whole module - but
it is the switch under every platform decision in the package, so the rule gets
cases rather than being read off the source.
"""

from __future__ import annotations

import sys

import pytest

from medialib.lib import hostos

pytestmark = pytest.mark.pure


class TestHostKind:
    @pytest.mark.parametrize("platform,want", [
        ("linux", "linux"),
        ("darwin", "macos"),
        ("win32", "windows"),
        ("cygwin", "windows"),
    ])
    def test_the_three_platforms_this_package_knows(self, platform, want):
        assert hostos.host_kind(platform) == want

    @pytest.mark.parametrize("platform", ["freebsd14", "openbsd7", "sunos5",
                                          "aix", "emscripten"])
    def test_everything_else_reads_as_linux(self, platform):
        """Not because it IS Linux: every decision keyed on this asks whether
        the POSIX shape holds, and on those it does."""
        assert hostos.host_kind(platform) == "linux"

    def test_with_nothing_said_it_answers_for_this_host(self):
        assert hostos.host_kind() == hostos.host_kind(sys.platform)


class TestThePredicates:
    def test_macos(self):
        assert hostos.is_macos("darwin") is True
        assert hostos.is_macos("linux") is False

    def test_windows(self):
        assert hostos.is_windows("win32") is True
        assert hostos.is_windows("darwin") is False

    def test_posix_is_the_two_that_are_not_windows(self):
        assert hostos.is_posix("linux") is True
        assert hostos.is_posix("darwin") is True
        assert hostos.is_posix("win32") is False
