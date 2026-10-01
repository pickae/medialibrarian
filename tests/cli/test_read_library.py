"""The white box for medialib/cli/read_library.py.

test_read_library_cli.py drives whole runs against a stubbed checkout; what is
pinned here is the publish underneath them, on a path a stubbed run never takes:
a destination that refuses part of the rename.
"""

import errno
import os

import pytest

from medialib.cli import read_library

pytestmark = pytest.mark.fs


@pytest.fixture
def book(tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    source = scratch / "story.opus"
    source.write_bytes(b"the whole audiobook")
    return source, tmp_path / "out" / "opus" / "story.opus"


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
