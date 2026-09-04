"""Tests for medialib.lib.versionsort.

Twelve call sites across six modules list a folder with ``find ... | sort -V``,
and the order that comes back decides which file becomes 01 - so the ordering is
a rule of the whole system and the port has to be exact.

The oracle is at the bottom: the `sort -V` this machine actually has. Exact
against WHAT is the interesting part - `sort` here is uutils coreutils, not GNU,
and the two disagree (GNU puts "1" before "01.mp3" and uutils the reverse).
So the comparison is against the sort the scripts really get - which means it
only says anything on a host whose sort is the one this rule was read off. The
oracle asks `sort --version` and SKIPS itself on any other, rather than
reporting a difference between two coreutils as a defect in this module; the
cases above it pin the behaviours themselves and run everywhere.
"""

import random
import shutil
import subprocess
import sys

import pytest

from medialib.lib.versionsort import version_sorted as order

pytestmark = pytest.mark.pure


class TestNumbersSortAsNumbers:
    def test_ten_comes_after_nine(self):
        assert order(["10.mp3", "9.mp3", "2.mp3"]) == ["2.mp3", "9.mp3", "10.mp3"]

    def test_padding_does_not_change_the_value(self):
        # same number, so the order between them is settled by bytes, but all
        # three still sort before 2
        assert order(["2.mp3", "1.mp3", "01.mp3", "001.mp3"])[-1] == "2.mp3"

    def test_a_longer_digit_run_is_a_bigger_number(self):
        assert order(["100.mp3", "99.mp3"]) == ["99.mp3", "100.mp3"]

    def test_numbers_inside_a_name(self):
        assert order(["a10", "a2", "a1"]) == ["a1", "a2", "a10"]


class TestTheSuffixIsHeldBack:
    """A trailing .ext group is set aside and the stems compared first, so a
    number keeps meaning a number even with an extension after it."""

    def test_the_stem_decides(self):
        assert order(["foo10.mp3", "foo9.mp3"]) == ["foo9.mp3", "foo10.mp3"]

    def test_a_dot_followed_by_a_digit_is_not_a_suffix(self):
        # ".3" does not qualify - a suffix group starts with a letter or "~"
        assert order(["a.3", "a.10"]) == ["a.3", "a.10"]


class TestTheTilde:
    def test_it_sorts_before_everything_including_the_end_of_a_name(self):
        assert order(["a", "a~", "ab"]) == ["a~", "a", "ab"]


class TestDots:
    def test_a_dotfile_sorts_before_an_ordinary_name(self):
        assert order(["b", ".a"]) == [".a", "b"]

    def test_the_current_directory_sorts_first(self):
        assert order(["..", "."]) == [".", ".."]


class TestNonAlphanumericsSortAfterLetters:
    def test_a_dash_comes_after_a_letter(self):
        assert order(["a-b", "aab"]) == ["aab", "a-b"]


class TestTheTieBreakThisMachineUses:
    """Once the stems tie, the order is byte order over the whole names. GNU runs
    another version comparison first and would answer the other way round; the
    sort installed here does not, and the scripts get the sort installed here."""

    def test_a_padded_name_with_a_suffix_beats_a_bare_one(self):
        assert order(["1", "01.mp3"]) == ["01.mp3", "1"]

    def test_and_a_bare_name_beats_its_own_suffixed_form(self):
        assert order(["1.mp3", "1"]) == ["1", "1.mp3"]


# --- the oracle ---------------------------------------------------------------

# The oracle runs `sort -V` with a POSIX PATH and LC_ALL in its environment,
# neither of which a Windows child can use.
_POSIX = sys.platform != "win32"
_ORACLE_PATH = "/usr/bin:/bin"


def _sort_flavor() -> str:
    """How the `sort` the oracle will run names itself: the first line of
    `sort --version`, which for either coreutils holds its project's name.

    Under the oracle's own PATH, so the answer describes the very binary the
    comparison is made against and not some other one earlier on this shell's
    PATH. Not asked on Windows at all: the `sort.exe` there takes no --version
    and would sit reading stdin instead of answering.
    """
    if not _POSIX or shutil.which("sort", path=_ORACLE_PATH) is None:
        return ""
    try:
        done = subprocess.run(["sort", "--version"], capture_output=True,
                              text=True, stdin=subprocess.DEVNULL,
                              env={"LC_ALL": "C", "PATH": _ORACLE_PATH})
    except OSError:
        return ""
    return done.stdout.split("\n", 1)[0]


_FLAVOR = _sort_flavor()

# The tie-break this module implements is uutils', and GNU's filevercmp answers
# the other way round (see the module's own docstring). A GNU host is therefore
# not a host this comparison can be made on, and the difference is between two
# coreutils rather than in this code, so it skips there rather than recording
# GNU's answer as if this module were meant to give it.
needs_uutils_sort = pytest.mark.skipif(
    "uutils" not in _FLAVOR,
    reason="the oracle is uutils `sort -V`; this host has %r"
           % (_FLAVOR or "no POSIX sort"))


def _real_sort(names: list[str]) -> list[str]:
    done = subprocess.run(["sort", "-V"], input="\n".join(names) + "\n",
                          capture_output=True, text=True,
                          env={"LC_ALL": "C", "PATH": _ORACLE_PATH})
    return done.stdout.split("\n")[:-1]


@needs_uutils_sort
@pytest.mark.parametrize("names", [
    pytest.param(["1.mp3", "2.mp3", "10.mp3", "11.mp3", "9.mp3"],
                 id="a plain numbered run"),
    pytest.param(["1.mp3", "01.mp3", "001.mp3", "2.mp3", "10.mp3", "100.mp3",
                  "0.mp3"], id="mixed zero padding"),
    pytest.param(["a1.mp3", "a10.mp3", "a2.mp3", "Kapitel 1.mp3",
                  "Kapitel 10.mp3"], id="numbers inside words"),
    pytest.param(["x.mp3", "x.MP3", "x.Mp3"], id="extensions differing in case"),
    pytest.param(["1", "2", "10", "01", "001"], id="names with no extension"),
    pytest.param([".", "..", ".hidden", ".hidden.mp3", "...", ".mp3"],
                 id="dotfiles and dots"),
    pytest.param(["a~", "a", "~", "~a", "a~b", "b~a"],
                 id="the tilde, which sorts before everything"),
    pytest.param(["a-1", "a_1", "a 1", "a+1", "a(1)", "a.1", "a'1"],
                 id="non-alphanumerics"),
    pytest.param(["1", "01.mp3", "1.mp3", "2"],
                 id="the case that tells uutils from GNU"),
    pytest.param(["\u00e9.mp3", "e.mp3", "z.mp3", "\u2013.mp3", "\u00dcber.mp3"],
                 id="accented and non-ascii names"),
    pytest.param(["foo.tar.gz", "foo.tar.9", "foo.tar", "foo"],
                 id="multi-part suffixes"),
])
def test_the_shapes_a_media_folder_actually_holds(names):
    assert order(names) == _real_sort(names)


ATOMS = ["a", "b", "z", "A", "Z", "1", "2", "9", "10", "01", "001", "0", "100",
         "~", "-", "_", ".", " ", "+", "#", "(", ")", "'", "\u00e9", "\u2013",
         "mp3", "jpg", "txt", "v", "rc", "alpha", "beta"]


@needs_uutils_sort
def test_a_generated_corpus_agrees_name_for_name():
    """The cases above are the ones somebody thought of."""
    rng = random.Random(20260820)
    for _ in range(300):
        names = list(dict.fromkeys(
            "".join(rng.choice(ATOMS) for _ in range(rng.randint(1, 6)))
            for _ in range(rng.randint(2, 8))))
        names = [name for name in names if name]
        if not names:
            continue
        assert order(names) == _real_sort(names), names
