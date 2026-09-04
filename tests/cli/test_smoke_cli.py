"""The cheap check every command gets, whether or not its pipeline can be run
here: it answers `--help`, it refuses an empty command line, and the options it
acts on are the options it declares.

**No parse check belongs here.** `ruff check` and `mypy` run on every commit and
neither passes on a module that does not parse, and `pytest` imports every module
besides. A third answer to that question would only be one more thing to keep in
step.

The option-declaration check is the bulk of the file because nothing else asks it.
An option a command ACTS on but never DECLARES is unreachable and silently so: the
flag is refused, the feature stays off, and the run otherwise looks normal. No
linter sees it, and neither does the help page - the page is rendered FROM the
declaration, so a documented-but-undeclared option cannot exist and the danger is
entirely in the other direction. `convert-images` carried exactly that for `-r`,
its avif-to-jpeg direction: implemented, documented, undeclared, unreachable.
"""

from __future__ import annotations

import importlib
import re

import pytest

from medialib import commands

pytestmark = pytest.mark.fs

_COMMANDS = sorted(commands.COMMANDS)

# Every command refuses an empty command line with exit 1, and every one but this
# answers it with its usage page. `clean-folder-structure-adb` answers with the
# refusal alone ("expected exactly one path; got 0"). It is the internal helper
# the README does not document, so it is the one place the inconsistency costs
# least - recorded rather than changed here: a page changes only when a decision
# says so.
_NO_USAGE_ON_EMPTY = {"clean-folder-structure-adb"}

# `cue-to-chapters` parses no options at all - it takes two paths - so `-h` is
# just an argument it cannot use: it prints the page and exits 1 rather than 0.
# The page IS answered, which is what the rule asks for; the status is the wart,
# and changing it is a decision about a recorded page rather than something to
# ride along with an unrelated change.
_HELP_EXITS_ONE = {"cue-to-chapters"}


def _spec_of(command: str):
    """The command's declaration, wherever it lives.

    Four commands are the RUN half of a pair and the declaration sits in the
    other half - `ingest-movies` is `ingest_movies_run`, whose spec is in
    `ingest_movies`. Asking for the attribute rather than assuming the name
    means a later rename cannot make this quietly check nothing.
    """
    name = commands.module_for(command)
    candidates = [name]
    if name.endswith("_run"):
        candidates.append(name[: -len("_run")])
    for candidate in candidates:
        module = importlib.import_module(candidate)
        if hasattr(module, "spec"):
            return module.spec(command)
    raise AssertionError("no spec() reachable for %s (tried %s)"
                         % (command, ", ".join(candidates)))


def _declared(spec) -> list[str]:
    """The option letters the declaration table carries, in order.

    One row per option, the letter first - and the help page is rendered from
    this, so a documented-but-undeclared option is impossible by construction.
    The question is the other direction.
    """
    return re.findall(r"^\s*([a-zA-Z])\s*\|", spec.options, re.MULTILINE)


# How `flags` spells a letter: the KIND comes first, so the letter is what
# follows the prefix - and `optionalArg` carries a regex after it. Read the way
# `clioptions` reads it, because a different reading here would check something
# the parser never sees.
_FLAG_KINDS = ("arg:", "repeat:", "optionalArg:")


def _acted_on(spec) -> set[str]:
    """The letters something acts on: an assignment, a flag, a validated value,
    or a long form that has to resolve to one of them."""
    letters = set()
    for table in (spec.vars, spec.long):
        for entry in table.split():
            letters.add(entry.split(":", 1)[0])
    for entry in spec.flags.split():
        for kind in _FLAG_KINDS:
            if entry.startswith(kind) and len(entry) > len(kind):
                letters.add(entry[len(kind)])
                break
    letters.update(_declared_in(spec.checks))
    # `h:help` is in every long table, and `-h` is answered by the parser rather
    # than by a row of any command's own declaration.
    return letters - {"h"}


def _declared_in(table: str) -> list[str]:
    return re.findall(r"^\s*([a-zA-Z])\s*\|", table, re.MULTILINE)


class TestEveryCommandAnswersItsHelp:
    """The page comes out, and asking for it is not an error - both before any
    media or scratch directory is touched, which is what makes running all
    eighteen here free."""

    @pytest.mark.parametrize("command", _COMMANDS)
    @pytest.mark.parametrize("flag", ["-h", "--help"])
    def test_the_page_is_printed(self, sandbox, command, flag):
        """Both spellings, because every letter has a long form."""
        done = sandbox.run(command, flag)
        assert "Usage:" in done.stdout + done.stderr

    @pytest.mark.parametrize("command", _COMMANDS)
    @pytest.mark.parametrize("flag", ["-h", "--help"])
    def test_asking_for_it_is_not_an_error(self, sandbox, command, flag):
        done = sandbox.run(command, flag)
        expected = 1 if command in _HELP_EXITS_ONE else 0
        assert done.returncode == expected, done.stdout + done.stderr


class TestAnEmptyCommandLineIsRefused:
    """The other half of the same contract: called with nothing, say so and exit
    1 rather than running on an empty argument list. Side-effect free for the
    same reason `-h` is - the check happens before any work."""

    @pytest.mark.parametrize("command", _COMMANDS)
    def test_it_exits_one(self, sandbox, command):
        done = sandbox.run(command)
        assert done.returncode == 1, done.stdout + done.stderr

    @pytest.mark.parametrize("command", _COMMANDS)
    def test_it_says_something_about_what_it_wanted(self, sandbox, command):
        done = sandbox.run(command)
        log = done.stdout + done.stderr
        if command in _NO_USAGE_ON_EMPTY:
            assert "expected exactly one path" in log, log
        else:
            assert "Usage:" in log, log


class TestTheDeclarationAndTheCodeAgree:
    """Asked of the declaration each command carries rather than of the parse
    loop, which is shared and has its own cases."""

    @pytest.mark.parametrize("command", _COMMANDS)
    def test_every_acted_on_option_is_declared(self, command):
        """A letter that is assigned, flagged, checked or given a long form and
        is NOT in the table is the `convert-images -r` bug in its general form:
        something acts on an option the parser will refuse."""
        spec = _spec_of(command)
        missing = sorted(_acted_on(spec) - set(_declared(spec)))
        assert missing == [], \
            "acted on but absent from the declaration: %s" % (
                " ".join("-" + letter for letter in missing))

    @pytest.mark.parametrize("command", _COMMANDS)
    def test_no_option_letter_is_declared_twice(self, command):
        """The option string takes the FIRST row of a repeated letter and drops
        the rest, while the help page renders both - so a duplicate reads as two
        documented options of which only one can ever be parsed, which is what a
        copied-and-edited row produces."""
        declared = _declared(_spec_of(command))
        duplicated = sorted({letter for letter in declared
                             if declared.count(letter) > 1})
        assert duplicated == [], \
            "declared more than once: %s" % " ".join(duplicated)
