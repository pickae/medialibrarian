"""Tests for medialib.lib.overwrite - asking before a report is replaced."""

import io

import pytest

from medialib.lib import overwrite

pytestmark = pytest.mark.fs


def _ask(monkeypatch, capsys, paths, answer):
    monkeypatch.setattr("sys.stdin", io.StringIO(answer))
    allowed = overwrite.confirm_overwrite(paths)
    return allowed, capsys.readouterr().err


def test_nothing_there_yet_is_not_asked_about(tmp_path, monkeypatch, capsys):
    allowed, err = _ask(monkeypatch, capsys, [str(tmp_path / "new.txt")], "")
    assert allowed
    assert err == ""


@pytest.mark.parametrize("answer", ["y\n", "Y\n", "yes\n", " YES \n"])
def test_yes_overwrites(tmp_path, monkeypatch, capsys, answer):
    earlier = tmp_path / "earlier.txt"
    earlier.write_text("x")
    allowed, err = _ask(monkeypatch, capsys, [str(earlier)], answer)
    assert allowed
    assert str(earlier) in err
    assert "Overwrite it? [y/N]" in err


@pytest.mark.parametrize("answer", ["n\n", "\n", "yep\n"])
def test_anything_else_keeps_the_earlier_one(tmp_path, monkeypatch, capsys,
                                             answer):
    earlier = tmp_path / "earlier.txt"
    earlier.write_text("x")
    allowed, err = _ask(monkeypatch, capsys, [str(earlier)], answer)
    assert not allowed
    assert "Nothing was overwritten." in err


def test_no_answer_at_all_is_a_no_and_says_how_to_say_yes(
        tmp_path, monkeypatch, capsys):
    earlier = tmp_path / "earlier.txt"
    earlier.write_text("x")
    allowed, err = _ask(monkeypatch, capsys, [str(earlier)], "")
    assert not allowed
    assert "No answer on standard input" in err
    assert 'Pipe "y" in' in err


def test_several_are_asked_about_once_and_only_the_ones_there(
        tmp_path, monkeypatch, capsys):
    one, two = tmp_path / "one.txt", tmp_path / "two.txt"
    one.write_text("x")
    two.write_text("x")
    missing = tmp_path / "missing.txt"
    allowed, err = _ask(monkeypatch, capsys,
                        [str(one), str(missing), str(two), str(one)], "y\n")
    assert allowed
    assert err.count("Overwrite") == 1
    assert "Overwrite them?" in err
    assert str(missing) not in err
    assert err.count(str(one)) == 1
