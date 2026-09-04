"""No message in this package names a command by a name of its own.

A run prints its own name in three places - the usage head, the tool refusal and
the missing-module refusal - and every one of them has to say what the user
typed. A name written down in the source is one that outlives a rename, and the
refusal a user reads is then about a command they did not run.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from medialib import commands
from tests import blackbox

pytestmark = pytest.mark.pure

_REPO = blackbox.REPO

# The argument each one names the run in, by position.
_SUBJECT = {"require_tools": 0, "require_python_module": 1}

# A subject that names the RUN rather than the command is right and stays:
# "simulation mode (-s)" and "the tidy-up (-c) of the video tables" are what a
# refusal about one mode of a command should say. What may not be written down is
# the command's own name, in either spelling.
_A_COMMAND = re.compile(r"\b(%s)\b|\.sh\b" % "|".join(
    sorted(commands.COMMANDS) + [module.rsplit(".", 1)[-1]
                                 for module in commands.COMMANDS.values()]))


def _modules():
    return [path for path in _REPO.glob("medialib/**/*.py")
            if not path.name.startswith("test_") and path.name != "conftest.py"]


def _literal_subjects(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        index = _SUBJECT.get(name) if isinstance(name, str) else None
        if index is None or len(node.args) <= index:
            continue
        subject = node.args[index]
        text = _joined(subject)
        if text is not None and _A_COMMAND.search(text):
            yield node.lineno, name, text


def _joined(node):
    """The text of a string literal, including one written as adjacent parts."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return None                       # a template, which is the right shape
    return None


def test_no_refusal_names_a_command_it_was_given_a_name_for():
    found = ["%s:%d %s(%r)" % (path.relative_to(_REPO), line, name, value)
             for path in _modules()
             for line, name, value in _literal_subjects(path)]
    assert found == []


def test_the_walk_reads_the_calls_it_is_about():
    """A parser that found no such call would pass the assertion above without
    checking one."""
    calls = sum(1 for path in _modules()
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                if isinstance(node, ast.Call)
                and (getattr(node.func, "attr", None) in _SUBJECT))
    assert calls >= 15
