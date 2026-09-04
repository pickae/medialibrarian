"""The suite answers the same on any host, because it does not inherit a locale.

Two parts of one decide results here: LC_CTYPE, which is why an accented name
survives being walked character-wise, and LC_COLLATE, which is the order the
recorded `tree` fixtures are in. Neither belongs to the code under test, so
`conftest.py` pins them - the reasoning is written there.

On a glibc host both come from one name, C.UTF-8. macOS has no such name and
gets the same combination assembled from its two halves, so the cases here are
in two kinds: the ones about the COMBINATION, which hold whichever rung the
host took, and the ones about the single name, which are skipped where there is
none.

A pin is invisible while it holds, which is why it gets a case of its own rather
than being noticed by whatever breaks first.
"""

from __future__ import annotations

import codecs
import locale
import os
import subprocess
import sys

import pytest

from tests.conftest import PINNED_LOCALE

pytestmark = pytest.mark.pure

_HAS_C_UTF8 = PINNED_LOCALE == "C.UTF-8"

needs_c_utf8 = pytest.mark.skipif(
    not _HAS_C_UTF8, reason="this host has no C.UTF-8, so the pin is assembled")

needs_a_pin = pytest.mark.skipif(
    PINNED_LOCALE is None,
    reason="this host offers neither spelling, so it keeps its own locale")


# --- the combination, whichever way this host spells it -------------------------


@needs_a_pin
def test_characters_are_handled_as_utf8():
    """The half the name cleaners need: a multibyte name walked character-wise
    rather than mangled a byte at a time.

    Read off the locale the process is IN rather than off
    ``getpreferredencoding``, which on Windows answers about the code page and
    not about what was set here.
    """
    name = locale.setlocale(locale.LC_CTYPE)
    assert codecs.lookup(name.rsplit(".", 1)[-1]).name == "utf-8"


@needs_a_pin
def test_and_sorting_is_by_bytes():
    """The half the recorded `tree` fixtures need. In byte order "a16z" sorts
    after "Die ..."; a dictionary order puts it right after "36", and every
    recorded sibling list would have to be re-recorded."""
    assert locale.strxfrm("a16z") > locale.strxfrm("Die Zeit")


@needs_a_pin
def test_a_child_inherits_both():
    """The transliteration is an `iconv` subprocess, so the pin has to be in
    the environment and not only in this process. Whichever variables carry it,
    what the child must not find is an LC_ALL saying something else."""
    done = subprocess.run(
        [sys.executable, "-c",
         "import os;print(repr({k: v for k, v in os.environ.items() "
         "if k.startswith('LC_')}))"],
        capture_output=True, text=True, check=True)
    inherited = eval(done.stdout)          # noqa: S307 - our own repr, one line
    wanted = {k: v for k, v in os.environ.items() if k.startswith("LC_")}
    assert inherited == wanted


@needs_a_pin
def test_the_folding_the_pin_exists_for():
    """An accent folds to ASCII rather than being dropped - the thing the pin is
    for, in one line."""
    from medialib.lib import tmdblookup
    assert tmdblookup.normalize_title("Amélie") == "amelie"


# --- the one-name spelling ------------------------------------------------------


@needs_c_utf8
def test_the_run_is_pinned_whatever_the_host_was_started_with():
    assert os.environ["LC_ALL"] == "C.UTF-8"


@needs_c_utf8
def test_and_the_process_itself_is_too():
    """`locale.strxfrm` and the ctypes `iswalnum` read this one, not the
    variable."""
    assert locale.setlocale(locale.LC_CTYPE) == "C.UTF-8"


@needs_c_utf8
def test_a_child_inherits_it():
    done = subprocess.run([sys.executable, "-c",
                           "import os; print(os.environ.get('LC_ALL'))"],
                          capture_output=True, text=True, check=True)
    assert done.stdout.strip() == "C.UTF-8"


# --- the assembled spelling -----------------------------------------------------


@pytest.mark.skipif(PINNED_LOCALE != "",
                    reason="this host had the one-name spelling")
def test_the_assembled_pin_drops_lc_all():     # pragma: no cover - host data
    """An inherited LC_ALL overrides both halves in a child, which would undo
    exactly what was just assembled."""
    assert "LC_ALL" not in os.environ
    assert os.environ["LC_COLLATE"] == "C"
