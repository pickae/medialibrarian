"""Every command has a recorded CLI contract, and every recording has a command.

A page changes only when a decision says so, and what proves it per command is
the recorded contract in tests/data/cliContract. The comparison itself is a
mapping somebody maintains by hand, so what is checked here is the mapping: a
command added without a recording is silently uncovered, and a
recording left behind after a command goes is a fixture nothing runs.
"""

import importlib
import os
import re

import pytest

import medialib.cli
from medialib import commands
from medialib.lib import clioptions
from tests import blackbox

pytestmark = pytest.mark.fs

_FIXTURES = str(blackbox.DATA / "cliContract")


def _scripts():
    """The recording directory each command is entitled to, which since item 6.4
    is simply its own name."""
    return sorted(commands.COMMANDS)


def _recorded():
    return sorted(name for name in os.listdir(_FIXTURES)
                  if os.path.isdir(os.path.join(_FIXTURES, name)))


def test_there_are_commands_to_check_at_all():
    """A comparison of two empty sets satisfies every assertion below by
    checking nothing."""
    assert len(_scripts()) == 18


def test_every_command_has_a_recorded_contract():
    assert set(_scripts()) <= set(_recorded()), \
        "no recording: %s" % sorted(set(_scripts()) - set(_recorded()))


def test_every_recording_still_has_its_command():
    assert set(_recorded()) <= set(_scripts()), \
        "no command: %s" % sorted(set(_recorded()) - set(_scripts()))


@pytest.mark.parametrize("script", _scripts())
def test_the_no_argument_refusal_is_recorded_for_each(script):
    """The one scenario every command has, whether or not it parses options."""
    for part in ("noargs.out", "noargs.err", "noargs.rc"):
        assert os.path.isfile(os.path.join(_FIXTURES, script, part)), part


# --- the long forms ----------------------------------------------------------
# Every documented option letter has a name in full as well. A
# letter added without one is a gap nothing else notices: the page renders, the
# letter works, and only somebody reaching for the long form finds out.

def _cli_modules():
    """Every CLI module that declares a spec, found through the package rather
    than by listing this file's own directory - which is not where they are."""
    out = []
    directory = os.path.dirname(os.path.abspath(medialib.cli.__file__))
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".py") or name.startswith("test_"):
            continue
        if name.endswith("_run.py") or name == "__init__.py":
            continue
        module = importlib.import_module("medialib.cli." + name[:-3])
        if hasattr(module, "spec"):
            out.append((name[:-3], module.spec("x")))
    return out


def test_there_are_specs_to_check_at_all():
    assert len(_cli_modules()) >= 17


def test_every_documented_option_has_a_long_form():
    missing = []
    for stem, declaration in _cli_modules():
        longs = dict(clioptions.long_pairs(declaration))
        for line in declaration.options.split("\n"):
            letter = clioptions._entry_letter(line)
            if letter is None or not clioptions.spec_field(line, 3):
                continue
            if letter not in longs:
                missing.append(f"{stem} -{letter}")
    assert not missing, "no long form: %s" % missing


def test_no_long_form_names_a_letter_the_script_does_not_have():
    stray = []
    for stem, declaration in _cli_modules():
        letters = {clioptions._entry_letter(line)
                   for line in declaration.options.split("\n")}
        for letter, name in clioptions.long_pairs(declaration):
            if letter not in letters:
                stray.append(f"{stem} --{name} (-{letter})")
    assert not stray, "no such option: %s" % stray


def test_a_long_name_is_a_lower_case_kebab_word():
    bad = []
    for stem, declaration in _cli_modules():
        for _letter, name in clioptions.long_pairs(declaration):
            if not re.fullmatch(r"[a-z][a-z0-9]*(-[a-z0-9]+)*", name):
                bad.append(f"{stem} --{name}")
    assert not bad, "not a kebab-case name: %s" % bad


def test_one_letter_never_gets_two_names_or_one_name_two_letters():
    for stem, declaration in _cli_modules():
        pairs = clioptions.long_pairs(declaration)
        letters = [letter for letter, _ in pairs]
        names = [name for _, name in pairs]
        assert len(set(letters)) == len(letters), f"{stem}: letter twice"
        assert len(set(names)) == len(names), f"{stem}: name twice"
