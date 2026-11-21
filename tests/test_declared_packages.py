"""The Python packages the distribution declares, and the one refusal for them.

A package pyproject.toml declares can still be missing - an editable install
keeps the packages it was installed with - and a command must then refuse as it
starts, naming it, rather than crash with an ImportError in whichever child
process first reaches it, after the long part of a run. That holds only while
two things do: the table the check reads is the declared list, and no module
imports a declared package at its top, which would fail before any check runs.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib

import pytest

from medialib import commands
from medialib.lib import tooldeps
from tests import blackbox

pytestmark = pytest.mark.fs

_REPO = blackbox.REPO


def _declared():
    with open(_REPO / "pyproject.toml", "rb") as handle:
        return tomllib.load(handle)["project"]["dependencies"]


def test_the_table_is_the_declared_list():
    assert sorted(_declared()) == sorted(tooldeps.DECLARED_PACKAGES)


@pytest.fixture
def without_packages(tmp_path):
    """A directory that, first on PYTHONPATH, makes every declared package fail
    to import - in this process's children and in theirs."""
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    for name in tooldeps.DECLARED_PACKAGES:
        (shadow / (name + ".py")).write_text(
            "raise ImportError('shadowed by the test')\n", encoding="utf-8")
    return shadow


def _modules():
    """Every module of the package that runs on this interpreter. The helpers
    are started on interpreters of their own, with their own packages."""
    found = []
    for path in sorted((_REPO / "medialib").rglob("*.py")):
        relative = path.relative_to(_REPO).with_suffix("")
        if relative.parts[1] == "helpers":
            continue
        parts = relative.parts[:-1] if relative.name == "__init__" else relative.parts
        found.append(".".join(parts))
    return found


def test_every_module_imports_without_the_declared_packages(without_packages):
    modules = _modules()
    assert len(modules) > 50
    probe = ("import importlib, sys, traceback\n"
             "failed = []\n"
             "for name in sys.argv[1:]:\n"
             "    try:\n"
             "        importlib.import_module(name)\n"
             "    except Exception:\n"
             "        failed.append(name + ': ' + traceback.format_exc().splitlines()[-1])\n"
             "print('\\n'.join(failed))\n")
    ran = subprocess.run(
        [sys.executable, "-c", probe, *modules], cwd=str(_REPO),
        env={"PYTHONPATH": "%s:%s" % (without_packages, _REPO), "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, timeout=300)
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.strip() == ""


@pytest.mark.parametrize("command", sorted(commands.COMMANDS), ids=lambda n: n)
def test_every_command_refuses_before_anything_else(command, tmp_path,
                                                    without_packages):
    import os
    env = dict(os.environ)
    env["SKIP_TOOL_PREFLIGHT"] = ""
    env.pop("DECLARED_PACKAGES_FOUND", None)
    env["PYTHONPATH"] = str(without_packages)
    ran = blackbox.run(command, "--help", cwd=tmp_path, env=env, timeout=120)
    assert ran.returncode == 1, ran.stdout + ran.stderr
    assert "Traceback" not in ran.stderr
    assert "Python packages it needs are not installed" in ran.stderr
    for name in tooldeps.DECLARED_PACKAGES:
        assert "  %s  " % name in ran.stderr
