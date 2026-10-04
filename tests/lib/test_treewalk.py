"""Tests for medialib.lib.treewalk - every entry below a directory, in find's
order, without following a link and without stopping at a folder it cannot
list."""

import os
import sys

import pytest

from medialib.lib import treewalk


def _walk(top):
    return [os.path.relpath(entry.path, top).replace(os.sep, "/")
            for entry in treewalk.entries_below(str(top))]


def test_a_subdirectory_is_descended_into_where_it_stands(tmp_path):
    """One entry per folder, so readdir order cannot hide the answer: the
    folder's own contents come straight after it."""
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "film.mkv").write_text("")
    assert _walk(tmp_path) == ["a", "a/b", "a/b/film.mkv"]


def test_every_kind_of_entry_is_yielded(tmp_path):
    (tmp_path / "folder").mkdir()
    (tmp_path / "file").write_text("")
    assert sorted(_walk(tmp_path)) == ["file", "folder"]


def test_a_top_that_cannot_be_listed_yields_nothing(tmp_path):
    assert _walk(tmp_path / "missing") == []


@pytest.mark.skipif(sys.platform == "win32",
                    reason="making a symlink needs a privilege the CI "
                           "account does not have")
def test_a_link_is_yielded_but_not_followed(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "inside").write_text("")
    os.symlink(tmp_path / "real", tmp_path / "link")
    assert sorted(_walk(tmp_path)) == ["link", "real", "real/inside"]


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0,
                    reason="a folder its owner cannot list is POSIX's, and "
                           "root lists it anyway")
def test_a_folder_it_cannot_list_is_yielded_and_passed_over(tmp_path):
    """lost+found on an ext4 drive is root's alone."""
    closed = tmp_path / "lost+found"
    closed.mkdir()
    (closed / "hidden").write_text("")
    (tmp_path / "film.mkv").write_text("")
    closed.chmod(0o000)
    try:
        assert sorted(_walk(tmp_path)) == ["film.mkv", "lost+found"]
    finally:
        closed.chmod(0o700)


def test_the_entry_just_handed_over_may_be_removed(tmp_path):
    for name in ("a", "b", "c"):
        (tmp_path / name).write_text("")
    seen = []
    for entry in treewalk.entries_below(str(tmp_path)):
        seen.append(entry.name)
        os.unlink(entry.path)
    assert sorted(seen) == ["a", "b", "c"]


@pytest.mark.skipif(sys.platform == "win32",
                    reason="a path that deep is past Windows' MAX_PATH")
def test_a_tree_deeper_than_the_recursion_limit(tmp_path, monkeypatch):
    depth = sys.getrecursionlimit() + 100
    monkeypatch.chdir(tmp_path)
    # not os.makedirs, which recurses once per level itself
    deep = "d"
    os.mkdir(deep)
    for _ in range(depth - 1):
        deep += "/d"
        os.mkdir(deep)
    try:
        assert sum(1 for _ in treewalk.entries_below(".")) == depth
    finally:
        # bottom up, since a recursive rmtree is what this is guarding against
        while deep:
            os.rmdir(deep)
            deep = os.path.dirname(deep)
