"""The settings registry against the code that reads settings, and against the
page that lists them.

A setting nobody listed is one no page tells a user about and no settings file
can be warned about, and a listed one nothing reads is a page sending a user
to set something that does nothing. So the registry is checked from both
sides: every name the code reads from the environment that is spelled like a
setting has to be in it, and everything in it has to be read somewhere.
"""

from __future__ import annotations

import ast
import re

import pytest

from medialib import commands
from medialib.lib import settings
from tests import blackbox

pytestmark = pytest.mark.pure

_PACKAGE = blackbox.REPO / "medialib"

# Spelled like a setting: lower case first. The variables the commands hand
# one another are upper case, and are not settings.
_SETTING_SPELLING = re.compile(r"[a-z][A-Za-z0-9]*$")

# The calls that read an environment variable by the name they are given:
# the environment's own, and the small readers modules keep for their knobs.
_READERS = {"get", "getenv", "setdefault", "pop", "_config", "_knob", "_env",
            "_flag", "setenv", "delenv"}


def _names_read(tree: ast.AST) -> set:
    """Every name spelled like a setting that ``tree`` reads from, or hands
    to, the environment - by a call, by a subscript, through a constant whose
    name says it holds a variable's name, or in a provider's login."""
    found = set()

    def literal(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and _SETTING_SPELLING.match(node.value):
            found.add(node.value)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else \
                func.id if isinstance(func, ast.Name) else ""
            if called in _READERS and node.args and _is_environment_read(func):
                literal(node.args[0])
            if called == "Provider" and len(node.args) == 3:
                for element in getattr(node.args[2], "elts", ()):
                    literal(element)
        elif isinstance(node, ast.Subscript) and _environ(node.value):
            literal(node.slice)
        elif isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name)
                and target.id.endswith(("_VARIABLE", "_KEY", "LOGINS"))
                for target in node.targets):
            # A table of logins is keyed by provider: only its values are
            # variables.
            held = node.value.values if isinstance(node.value, ast.Dict) \
                else [node.value]
            for value in held:
                for inner in ast.walk(value):
                    literal(inner)
    return found


def _environ(node) -> bool:
    """Whether ``node`` is an environment: os.environ, environ, env."""
    name = node.attr if isinstance(node, ast.Attribute) else \
        node.id if isinstance(node, ast.Name) else ""
    return name in ("environ", "env", "environment")


def _is_environment_read(func) -> bool:
    """A method of an environment, or one of the module-level readers."""
    if isinstance(func, ast.Attribute):
        return _environ(func.value) or func.attr == "getenv"
    return isinstance(func, ast.Name)


def _every_name_read() -> dict:
    """Each setting-spelled name the package reads, with the files that do."""
    where: dict = {}
    for path in sorted(_PACKAGE.rglob("*.py")):
        for name in _names_read(ast.parse(path.read_text(encoding="utf-8"))):
            where.setdefault(name, []).append(str(path.relative_to(blackbox.REPO)))
    return where


def test_there_is_a_registry_to_check():
    assert len(settings.SETTINGS) > 20


def test_every_setting_the_code_reads_is_listed():
    read = _every_name_read()
    unlisted = {name: files for name, files in read.items()
                if name not in settings.BY_NAME and name not in settings.INTERNAL}
    assert unlisted == {}


def test_every_listed_setting_is_read_somewhere():
    read = _every_name_read()
    assert [name for name in settings.BY_NAME if name not in read] == []


def test_each_setting_is_listed_once_and_the_internal_ones_not_at_all():
    names = [setting.name for setting in settings.SETTINGS]
    assert len(names) == len(set(names))
    assert set(names) & settings.INTERNAL == set()


def test_the_commands_a_setting_names_are_commands():
    named = {command for setting in settings.SETTINGS for command in setting.commands}
    assert named - set(commands.COMMANDS) == set()
    assert all(setting.commands for setting in settings.SETTINGS)


def test_the_page_is_the_registry():
    page = (blackbox.REPO / "docs" / "settings.md").read_text(encoding="utf-8")
    assert page == settings.page(), \
        "docs/settings.md is out of date: write settings.page() to it"


def test_every_command_reads_its_settings_before_anything_else():
    """settings.load() as cli()'s first statement - the docstring aside - so
    nothing a command does, its help page included, runs without them."""
    late = []
    for module in sorted(set(commands.COMMANDS.values())):
        path = blackbox.REPO / (module.replace(".", "/") + ".py")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        cli = next(node for node in tree.body
                   if isinstance(node, ast.FunctionDef) and node.name == "cli")
        body = [statement for statement in cli.body
                if not (isinstance(statement, ast.Expr)
                        and isinstance(statement.value, ast.Constant))]
        first = body[0]
        if not (isinstance(first, ast.Expr) and isinstance(first.value, ast.Call)
                and ast.unparse(first.value) == "settings.load()"):
            late.append(module)
    assert late == []


def test_the_subtitle_helper_reads_the_logins_the_command_checks():
    """The helper runs in subliminal's own environment and cannot import the
    package, so it keeps its own copy of the login names - which is only
    safe while the two copies agree."""
    from medialib.lib import subtitlefiles
    helper = (_PACKAGE / "helpers" / "subliminal_download.py").read_text(encoding="utf-8")
    tree = ast.parse(helper)
    values = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id in ("LOGINS", "SUBDL_KEY"):
            values[node.targets[0].id] = ast.literal_eval(node.value)
    providers = {provider.name: tuple(provider.login)
                 for provider in subtitlefiles.PROVIDERS}
    assert {key: tuple(login) for key, login in values["LOGINS"].items()} == \
        {key: login for key, login in providers.items() if len(login) == 2}
    assert (values["SUBDL_KEY"],) == providers["subdl"]
