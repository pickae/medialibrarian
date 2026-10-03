"""The white box for medialib/cli/read_library.py.

test_read_library_cli.py drives whole runs against a stubbed checkout; what is
pinned here is the publish underneath them, on the paths a stubbed run never
takes - a scratch on another filesystem than the output, and a destination that
refuses part of the rename.
"""

import errno
import os

import pytest

from medialib.cli import read_library

pytestmark = pytest.mark.fs


def _cross_device(*_args, **_kwargs):
    raise OSError(errno.EXDEV, "Invalid cross-device link")


@pytest.fixture
def book(tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    source = scratch / "story.opus"
    source.write_bytes(b"the whole audiobook")
    return source, tmp_path / "out" / "opus" / "story.opus"


class TestPublishingFromAScratchOnAnotherFilesystem:
    """The production path: the scratch is a tmpfs and the output a disk, so the
    move out of RAM is a copy."""

    @pytest.fixture
    def published(self, book, monkeypatch):
        source, target = book
        monkeypatch.setattr(os, "replace", _cross_device)
        monkeypatch.setattr(os, "rename", _cross_device)
        assert read_library.emit_output(str(source), str(target))
        return source, target

    def test_the_audiobook_arrives_whole(self, published):
        _, target = published
        assert target.read_bytes() == b"the whole audiobook"

    def test_it_is_moved_rather_than_copied(self, published):
        source, _ = published
        assert not source.exists()

    def test_no_staging_file_is_left_beside_it(self, published):
        _, target = published
        assert sorted(p.name for p in target.parent.iterdir()) == ["story.opus"]


class TestADestinationThatKeepsTheStagingName:
    """The link to the final name worked and removing the staging name did not.
    The book is published; linking it again would publish it twice."""

    def test_the_book_is_published_under_one_name(self, book, monkeypatch):
        source, target = book
        real_unlink = os.unlink

        def unlink(path, *args, **kwargs):
            if os.path.basename(path).startswith(".readLibrary."):
                raise OSError(errno.EACCES, "Permission denied")
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(os, "unlink", unlink)
        monkeypatch.setattr(os, "remove", unlink)
        assert read_library.emit_output(str(source), str(target))
        audio = sorted(p.name for p in target.parent.iterdir()
                       if not p.name.startswith("."))
        assert audio == ["story.opus"]
        assert target.read_bytes() == b"the whole audiobook"


class TestTheActiveBooksOrder:
    """The books on the status row come out in numeric order - 2, 9, 10 - not the
    string order that would put 10 before 2."""

    def test_the_books_are_shown_in_numeric_order(self, tmp_path):
        counter_dir = tmp_path / "counters"
        active = counter_dir / "active"
        active.mkdir(parents=True)
        (active / "2").write_text("=copying")
        (active / "9").write_text("=publishing")
        (active / "10").write_text("=done")
        run = read_library.Run(
            counters=read_library.Counters(str(counter_dir), 10))
        assert run.active_book_progress() == \
            "  [2] copying  [9] publishing  [10] done"
