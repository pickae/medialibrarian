"""The command table, against `[project.scripts]`.

`[project.scripts]` is the authority on what runs what: it is what a user ends up
with on PATH. So the table is checked against it rather than trusted - a wrong row
would send one command's arguments to another command's `main`, which no other
test would notice because both would start successfully.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from medialib import commands
from tests import blackbox

pytestmark = pytest.mark.pure

_REPO = blackbox.REPO


def _console_scripts():
    with open(_REPO / "pyproject.toml", "rb") as handle:
        return tomllib.load(handle)["project"]["scripts"]


def test_there_are_eighteen_commands_to_check():
    """A comparison of two empty things satisfies every assertion below by
    checking nothing, so the count is spelled out."""
    assert len(_console_scripts()) == 18
    assert len(commands.COMMANDS) == 18


def test_the_installed_commands_are_the_table():
    """`[project.scripts]` is what a user ends up with on PATH, and the table is
    what one command uses to start another. Two spellings of one fact."""
    assert _console_scripts() == {
        name: module + ":cli" for name, module in commands.COMMANDS.items()}


@pytest.mark.parametrize("command", sorted(commands.COMMANDS), ids=lambda n: n)
def test_every_console_script_points_at_something_callable(command):
    module = __import__(commands.module_for(command), fromlist=["cli"])
    assert callable(module.cli)


def test_every_name_is_a_lower_case_kebab_word():
    for name in commands.COMMANDS:
        assert re.fullmatch(r"[a-z][a-z0-9]*(-[a-z0-9]+)*", name), name


def test_no_two_commands_share_a_module():
    modules = list(commands.COMMANDS.values())
    assert len(set(modules)) == len(modules)


def test_the_reverse_lookup_is_the_table_reversed():
    assert {m: c for c, m in commands.COMMANDS.items()} == commands.MODULES


def test_an_unknown_command_names_itself():
    with pytest.raises(KeyError, match="convert-vidoe"):
        commands.module_for("convert-vidoe")


class TestRunCommand:
    """What reaches the child: the resolved interpreter, the module, the argv,
    and the two environment values the shell shim used to set."""

    def _spawn(self, monkeypatch, *args, **kwargs):
        seen = {}

        def fake_run(argv, **rest):
            seen["argv"] = argv
            seen["env"] = rest.get("env")
            seen["rest"] = {k: v for k, v in rest.items() if k != "env"}
            return "result"

        monkeypatch.setattr(commands.subprocess, "run", fake_run)
        assert commands.run_command(*args, **kwargs) == "result"
        return seen

    def test_the_interpreter_is_the_resolved_one_not_the_word_python3(
            self, monkeypatch):
        """The python3 on PATH here is a pyenv shim costing ~195 ms a call to
        re-resolve an interpreter we are already running on."""
        import sys
        seen = self._spawn(monkeypatch, "convert-audio", ["in", "out"])
        assert seen["argv"][0] == sys.executable
        assert seen["argv"][1] == "-m"

    def test_the_module_is_the_one_the_table_names(self, monkeypatch):
        seen = self._spawn(monkeypatch, "convert-video", [])
        assert seen["argv"][2] == "medialib.cli.convert_video_run"

    def test_the_argv_follows_it_as_strings(self, monkeypatch):
        seen = self._spawn(monkeypatch, "convert-audio", ["in", "out", 4])
        assert seen["argv"][3:] == ["in", "out", "4"]

    def test_the_child_is_told_what_it_is_called(self, monkeypatch):
        """Its help page prints the name, so a page printed by a child has to say
        the child's name and not the parent's."""
        seen = self._spawn(monkeypatch, "convert-audio", [])
        assert seen["env"]["CLI_PROGRAM"] == "convert-audio"

    def test_the_script_directory_travels_when_there_is_one(self, monkeypatch):
        seen = self._spawn(monkeypatch, "convert-audio", [], script_dir="/repo")
        assert seen["env"]["CLI_SCRIPT_DIR"] == "/repo"

    def test_and_is_left_alone_when_there_is_not(self, monkeypatch):
        """Rather than set to the empty string, which reads as a directory."""
        monkeypatch.delenv("CLI_SCRIPT_DIR", raising=False)
        seen = self._spawn(monkeypatch, "convert-audio", [])
        assert "CLI_SCRIPT_DIR" not in seen["env"]

    @pytest.mark.skipif(sys.platform == "win32",
                     reason="a Windows environment keeps the OS's case for "
                            "names, so the exact-name lookup misses")
    def test_the_rest_of_the_environment_is_inherited(self, monkeypatch):
        """The scratch base and the pause state travel in it, and a child that
        did not inherit them would work somewhere else."""
        monkeypatch.setenv("ramScratchBase", "/somewhere")
        seen = self._spawn(monkeypatch, "convert-audio", [])
        assert seen["env"]["ramScratchBase"] == "/somewhere"

    def test_a_caller_may_hand_over_its_own_environment(self, monkeypatch):
        seen = self._spawn(monkeypatch, "convert-audio", [],
                           env={"PATH": "/only/this"})
        assert seen["env"]["PATH"] == "/only/this"
        assert seen["env"]["CLI_PROGRAM"] == "convert-audio"

    def test_every_other_keyword_reaches_subprocess_run(self, monkeypatch):
        seen = self._spawn(monkeypatch, "convert-audio", [], check=True)
        assert seen["rest"] == {"check": True}


class TestWhatACommandCallsItself:
    """The name on the help page and the directory beside the package, with
    nothing in the environment to supply either."""

    def test_the_package_root_is_the_directory_the_package_sits_in(self):
        assert Path(commands.package_root()) == _REPO

    def test_a_script_directory_in_the_environment_wins(self, monkeypatch):
        """A caller that hands one down is still obeyed - for one release, so a
        setup that sets it is not broken silently."""
        monkeypatch.setenv("CLI_SCRIPT_DIR", "/handed/down")
        assert commands.script_dir() == "/handed/down"

    def test_and_the_checkout_answers_while_there_is_one(self, monkeypatch):
        """The package sits at the checkout's root, where pyproject.toml is, so
        for every run from this repository the checkout is the answer."""
        monkeypatch.delenv("CLI_SCRIPT_DIR", raising=False)
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        assert commands.script_dir() == commands.package_root()

    @pytest.mark.skipif(sys.platform == "win32",
                     reason="`~` resolves through USERPROFILE on Windows, "
                            "not HOME")
    def test_and_the_user_data_directory_answers_when_there_is_no_checkout(
            self, monkeypatch, tmp_path):
        """A `pip install .` package root is site-packages, which has no
        pyproject.toml beside it: the user's data directory answers then, and is
        the one place the answer is always writable."""
        monkeypatch.setattr(
            commands, "package_root",
            lambda: str(tmp_path / "site-packages" / "medialib"))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("CLI_SCRIPT_DIR", raising=False)
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        assert commands.script_dir() == str(tmp_path / ".local" / "share"
                                            / "medialib")

    def test_an_XDG_data_home_is_obeyed_for_the_user_data_directory(
            self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            commands, "package_root",
            lambda: str(tmp_path / "site-packages" / "medialib"))
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        monkeypatch.delenv("CLI_SCRIPT_DIR", raising=False)
        assert commands.script_dir() == str(tmp_path / "xdg" / "medialib")

    def test_a_program_name_in_the_environment_wins(self, monkeypatch):
        monkeypatch.setenv("CLI_PROGRAM", "./convert-video")
        assert commands.program_name(
            "medialib.cli.convert_video_run") == "./convert-video"

    def test_and_the_command_name_answers_when_nobody_said(self, monkeypatch):
        monkeypatch.delenv("CLI_PROGRAM", raising=False)
        assert commands.program_name(
            "medialib.cli.convert_video_run") == "convert-video"

    def test_a_module_outside_the_table_gets_no_name_at_all(self, monkeypatch):
        """Rather than a plausible one: a help page is a recorded contract."""
        monkeypatch.delenv("CLI_PROGRAM", raising=False)
        with pytest.raises(KeyError):
            commands.program_name("medialib.lib.runlog")


@pytest.mark.fs
@pytest.mark.parametrize("command", sorted(commands.COMMANDS),
                         ids=lambda name: name)
def test_a_module_run_with_no_environment_names_itself(command, tmp_path):
    """One command at a time: no CLI_PROGRAM, no CLI_SCRIPT_DIR, and a working
    directory that is not the checkout - so nothing but the package itself can be
    answering.

    Both streams, and no claim about the status: `cue-to-chapters` answers -h by
    refusing, on stderr, and that is its recorded page.
    """
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(_REPO),
           "HOME": str(tmp_path)}
    done = subprocess.run(
        [sys.executable, "-m", commands.module_for(command), "-h"],
        cwd=tmp_path, env=env, capture_output=True, text=True)

    page = done.stdout + done.stderr
    usage = [line.strip() for line in page.splitlines()
             if line.strip().startswith(command + " ")
             or line.strip() == command]
    assert usage, page
    assert ".sh" not in page.split("Usage:")[-1].splitlines()[1], page
