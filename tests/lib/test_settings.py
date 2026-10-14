"""Tests for medialib.lib.settings - where a command finds its settings, and
what it makes of the file they are written in.

The file's lines are a shell profile's, copied, so its values are pinned
against what bash itself makes of the same line, wherever there is a bash to
ask."""

import io
import os
import shutil
import subprocess

import pytest

from medialib.lib import settings

pytestmark = pytest.mark.stubbed

_HOME = os.path.expanduser("~")

# What each value written after "name=" means, as bash reads it.
_VALUES = [
    ("plain", "plain"),
    ("pa#ss", "pa#ss"),                     # a # inside a word is the word
    ("abc # a comment", "abc"),             # and after a space, a comment
    ("'pa ss'", "pa ss"),
    ('"pa ss"', "pa ss"),
    ("'it'\\''s'", "it's"),
    ("'$HOME'", "$HOME"),                   # single quotes are literal
    ('"$HOME/x"', _HOME + "/x"),
    ("${HOME}y", _HOME + "y"),
    ("~/a", _HOME + "/a"),
    ("a~/b", "a~/b"),
    ("a\\ b", "a b"),
    ('"q\\"q"', 'q"q'),
    ("p$", "p$"),
    ("$NOT_SET_ANYWHERE_X", ""),
    ("", ""),
]


@pytest.mark.parametrize("text,value", _VALUES)
def test_a_value_reads_as_bash_reads_it(text, value):
    assert settings.shell_value(text, os.environ) == value
    bash = shutil.which("bash")
    if bash:
        done = subprocess.run([bash, "--norc", "--noprofile", "-c",
                               'x=%s\nprintf %%s "$x"' % text],
                              capture_output=True, text=True,
                              env={"HOME": _HOME, "PATH": os.environ.get("PATH", "")})
        assert done.stdout == value


@pytest.mark.parametrize("text", ["a b", "'open", '"open'])
def test_a_value_bash_would_not_take_as_one_word_is_refused(text):
    with pytest.raises(ValueError):
        settings.shell_value(text, {})


def _setting(name, secret=False):
    return settings.Setting(name, "about", ("ingest-movies",), secret=secret)


@pytest.fixture
def registry(monkeypatch):
    """A registry of two settings, one of them a secret."""
    known = (_setting("someSetting"), _setting("someKey", secret=True))
    monkeypatch.setattr(settings, "SETTINGS", known)
    monkeypatch.setattr(settings, "BY_NAME", {one.name: one for one in known})
    for one in known:
        monkeypatch.delenv(one.name, raising=False)


def _file(tmp_path, monkeypatch, text, mode=0o600):
    path = tmp_path / "settings"
    path.write_text(text)
    path.chmod(mode)
    monkeypatch.setenv(settings.FILE_VARIABLE, str(path))
    return path


def test_the_file_is_read_under_the_environment(registry, tmp_path, monkeypatch):
    _file(tmp_path, monkeypatch, "# mine\nexport someSetting='from file'\nsomeKey=k\n")
    monkeypatch.setenv("someKey", "from environment")
    settings.load(io.StringIO())
    assert os.environ["someSetting"] == "from file"
    assert os.environ["someKey"] == "from environment"


def test_no_file_is_nothing_to_do(registry, tmp_path, monkeypatch):
    monkeypatch.setenv(settings.FILE_VARIABLE, str(tmp_path / "missing"))
    settings.load(io.StringIO())
    assert "someSetting" not in os.environ


@pytest.mark.skipif(os.name == "nt", reason="no permission bits to read")
def test_a_secret_others_can_read_stops_the_run(registry, tmp_path, monkeypatch):
    _file(tmp_path, monkeypatch, "someKey=k\n", mode=0o644)
    said = io.StringIO()
    with pytest.raises(SystemExit):
        settings.load(said)
    assert "chmod 600" in said.getvalue() and "someKey" in said.getvalue()
    assert "someKey" not in os.environ


@pytest.mark.skipif(os.name == "nt", reason="no permission bits to read")
def test_a_file_without_secrets_may_be_readable(registry, tmp_path, monkeypatch):
    _file(tmp_path, monkeypatch, "someSetting=x\n", mode=0o644)
    settings.load(io.StringIO())
    assert os.environ["someSetting"] == "x"


def test_a_line_that_is_no_assignment_stops_the_run(registry, tmp_path, monkeypatch):
    _file(tmp_path, monkeypatch, "someSetting=x\nnonsense\n")
    said = io.StringIO()
    with pytest.raises(SystemExit):
        settings.load(said)
    assert "line 2" in said.getvalue()


def test_a_name_nothing_reads_is_warned_about_with_the_likeliest(
        registry, tmp_path, monkeypatch):
    _file(tmp_path, monkeypatch, "someSettin=x\n")
    said = io.StringIO()
    settings.load(said)
    assert "WARNING" in said.getvalue() and "did you mean someSetting?" in said.getvalue()


def test_a_later_line_may_use_an_earlier_one(registry, tmp_path, monkeypatch):
    _file(tmp_path, monkeypatch, "someSetting=/a\nsomeKey=\"$someSetting/b\"\n")
    settings.load(io.StringIO())
    assert os.environ["someKey"] == "/a/b"


def test_the_file_is_under_the_config_home(monkeypatch, tmp_path):
    monkeypatch.delenv(settings.FILE_VARIABLE, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert settings.settings_file() == str(tmp_path / "medialib" / "settings")
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert settings.settings_file() == os.path.join(_HOME, ".config", "medialib",
                                                    "settings")
