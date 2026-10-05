"""Tests for medialib.lib.subtitleads - the adverts taken out by subcleaner.

What is pinned here: the cues counted as removed, a missing subcleaner leaving
the subtitle as it was, and a language subcleaner will not clean being said
once a run.
"""

import os

import pytest

from medialib.lib import subtitleads

_TWO_CUES = ("1\n00:00:01,000 --> 00:00:03,000\nSynced by someone\n\n"
             "2\n00:01:00,000 --> 00:01:02,000\nHello.\n")


@pytest.fixture()
def bin_dir(tmp_path, monkeypatch):
    path = tmp_path / "bin"
    path.mkdir()
    monkeypatch.setenv("PATH", str(path))
    monkeypatch.setattr(subtitleads, "_said_unprofiled", set())
    return path


def _subcleaner(bin_dir, script):
    tool = bin_dir / "subcleaner"
    tool.write_text("#!/bin/bash\n" + script + "\n")
    os.chmod(str(tool), 0o755)


def _srt(tmp_path):
    srt = tmp_path / "Movie.en.srt"
    srt.write_text(_TWO_CUES)
    return srt


def test_without_subcleaner_nothing_is_touched(bin_dir, tmp_path):
    srt = _srt(tmp_path)
    logs = []
    assert subtitleads.clean(str(srt), "en", logs.append) == 0
    assert srt.read_text() == _TWO_CUES
    assert logs == []


def test_the_cues_it_took_out_are_counted(bin_dir, tmp_path):
    _subcleaner(bin_dir, "/usr/bin/printf '1\\n00:01:00,000 --> "
                         "00:01:02,000\\nHello.\\n' > \"$1\"")
    srt = _srt(tmp_path)
    assert subtitleads.clean(str(srt), "en", [].append) == 1


def test_it_is_handed_the_whole_path_and_the_language(bin_dir, tmp_path,
                                                      monkeypatch):
    report = tmp_path / "argv"
    _subcleaner(bin_dir, '/usr/bin/printf "%s\\n" "$@" > ' + str(report))
    srt = _srt(tmp_path)
    monkeypatch.chdir(tmp_path)
    subtitleads.clean("Movie.en.srt", "nl", [].append)
    assert report.read_text().splitlines() == [
        str(srt), "--language", "nl", "--silent", "--no-log"]


def test_a_language_it_will_not_clean_is_said_once(bin_dir, tmp_path):
    _subcleaner(bin_dir, "echo \" WARNING: language 'de' have no regex "
                         "profile associated with it.\"")
    srt = _srt(tmp_path)
    logs = []
    for _ in range(2):
        assert subtitleads.clean(str(srt), "de", logs.append) == 0
    assert logs == ["WARNING: not cleaned of adverts, subcleaner has no word "
                    "list for this language - set require_language_profile "
                    "= false in its subcleaner.conf to clean it by the words "
                    "every language shares"]
