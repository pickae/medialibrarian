"""Tests for medialib.lib.archives - the answers a file's name gives about an
archive, and the unpacking of one.

What is pinned here:

  * the extractor dispatch, pinned to the exact argv the module hands the tools
    it still reaches for, with the tool's own exit status the answer - the wrong
    tool with the right tree left behind would otherwise pass;
  * the tar family, which reads through tarfile and so is pinned to the tree it
    lands on disk instead;
  * the descent and cleanup of extractArchiveAsFolder on shapes the corpus
    stages through a real extractor rather than directly, including the failure
    that must leave no temporary directory behind;
  * the host's 7-Zip binary under a PATH that is not this host's.
"""

import io
import os
import shutil
import stat
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest

from medialib.lib import archives

pytestmark = pytest.mark.fs


class TestExtension:
    def test_each_known_suffix(self):
        for ext in ("zip", "rar", "7z", "tar", "tar.gz", "tgz", "tar.bz2",
                    "tbz2", "tbz", "tar.xz", "txz", "tar.zst", "tzst"):
            assert archives.archive_extension_of(f"Some Book.{ext}") == ext

    def test_the_match_is_case_insensitive(self):
        assert archives.archive_extension_of("SOME BOOK.TAR.BZ2") == "tar.bz2"
        assert archives.archive_extension_of("Some Book.Tar.Gz") == "tar.gz"
        assert archives.archive_extension_of("Book.ZIP") == "zip"

    def test_the_shell_fold_of_the_dotted_i(self):
        # U+0130: the shell lowercases it to a plain "i" and a full Unicode
        # mapping does not, so this is the input that says which rule is in use.
        assert archives.archive_extension_of("Some Book.ZİP") == "zip"
        assert archives.archive_extension_of("Some Book.zİP") == "zip"
        # and the near miss that folds to nothing but "ip"
        assert archives.archive_extension_of("Book.İP") == ""

    def test_the_longest_match_wins(self, monkeypatch):
        # The live list has no suffix another live suffix ends in, so the rule
        # shows itself only on a list that does: the compound must not read as
        # its parts.
        monkeypatch.setattr(archives, "ARCHIVE_EXTENSIONS",
                            ("tar", "gz", "tar.gz"))
        assert archives.archive_extension_of("Book.tar.gz") == "tar.gz"
        assert archives.archive_extension_of("Book.gz") == "gz"
        assert archives.archive_extension_of("Book.tar") == "tar"

    def test_a_character_is_required_before_the_dot(self):
        assert archives.archive_extension_of(".zip") == ""
        assert archives.archive_extension_of(".tar.gz") == ""
        assert archives.archive_extension_of("zip") == ""

    def test_only_the_last_component_counts(self):
        assert archives.archive_extension_of("/srv/books.7z.d/Some Book.zip") == "zip"
        assert archives.archive_extension_of("books.tar.gz/Some Book.rar") == "rar"
        assert archives.archive_extension_of("a//b.zip") == "zip"
        assert archives.archive_extension_of("deep/dir.tgz/Book") == ""

    def test_a_suffix_followed_by_more_is_not_a_suffix(self):
        assert archives.archive_extension_of("Book.zipx") == ""
        assert archives.archive_extension_of("Book.tar.gz.old") == ""
        assert archives.archive_extension_of("Book.tar.gz2") == ""
        assert archives.archive_extension_of("Book.zip.") == ""

    def test_an_empty_name(self):
        assert archives.archive_extension_of("") == ""
        assert archives.archive_extension_of(" ") == ""


class TestIsArchive:
    def test_yes(self):
        assert archives.is_archive_file("Book.zip")
        assert archives.is_archive_file("Book.TAR.GZ")

    def test_no(self):
        assert not archives.is_archive_file("notes.txt")
        assert not archives.is_archive_file(".zip")
        assert not archives.is_archive_file("")


class TestBaseName:
    def test_the_suffix_comes_off(self):
        assert archives.archive_base_name("Some Book.tar.gz") == "Some Book"
        assert archives.archive_base_name("Some Book.ZIP") == "Some Book"
        assert archives.archive_base_name("Some Book.ZİP") == "Some Book"

    def test_only_the_last_component(self):
        assert archives.archive_base_name("/srv/books/Some Book.zip") == "Some Book"
        assert archives.archive_base_name("books.tar.gz/Book.rar") == "Book"

    def test_a_non_archive_comes_back_unchanged(self):
        assert archives.archive_base_name("notes.txt") == "notes.txt"
        assert archives.archive_base_name(".hidden") == ".hidden"
        assert archives.archive_base_name("Book.zipx") == "Book.zipx"

    def test_the_extension_of_the_shell_fold(self):
        # The strip runs on the name as spelled, the match on the fold - so the
        # length that comes off is the length of the known suffix, and the
        # capital letter stays where it was.
        assert archives.archive_base_name("Some Book.ZİP") == "Some Book"


class TestToolSpecs:
    def test_each_known_suffix(self):
        assert archives.archive_tool_specs("zip") == "unzip"
        assert archives.archive_tool_specs("rar") == "unrar"
        assert archives.archive_tool_specs("7z") == "7z|7zz|7za"
        assert archives.archive_tool_specs("tar.zst") == "tar zstd"
        assert archives.archive_tool_specs("tzst") == "tar zstd"
        for ext in ("tar", "tar.gz", "tgz", "tar.bz2", "tbz2", "tbz",
                    "tar.xz", "txz"):
            assert archives.archive_tool_specs(ext) == "tar"

    def test_the_lookup_is_case_sensitive(self):
        # A name match, but this is a table: the suffix in the case it is spelled
        # in answers nothing, the way a typo does.
        assert archives.archive_tool_specs("ZIP") == ""
        assert archives.archive_tool_specs("tar.GZ") == ""

    def test_every_extension_the_table_knows_has_an_extractor(self):
        """Enumerated from ARCHIVE_EXTENSIONS rather than by hand, so a suffix
        added without an extractor is caught here and not at extract time."""
        from medialib.lib.enums import ARCHIVE_EXTENSIONS
        for ext in ARCHIVE_EXTENSIONS:
            assert archives.archive_tool_specs(ext), ext

    def test_a_suffix_no_extractor_opens(self):
        assert archives.archive_tool_specs("gz") == ""
        assert archives.archive_tool_specs("mp3") == ""
        assert archives.archive_tool_specs("") == ""


class TestSevenZipCommand:
    def _fake_path(self, tmp_path, monkeypatch, names, number=1):
        bin_dir = tmp_path / f"bin{number}"
        bin_dir.mkdir()
        for name in names:
            exe = bin_dir / name
            exe.write_text("#!/bin/sh\nexit 0\n")
            exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
        monkeypatch.setenv("PATH", str(bin_dir))

    def test_the_first_of_the_three_on_the_path(self, tmp_path, monkeypatch):
        self._fake_path(tmp_path, monkeypatch, ["7z", "7zz", "7za"])
        assert archives.seven_zip_command() == "7z"

    def test_each_name_in_turn(self, tmp_path, monkeypatch):
        self._fake_path(tmp_path, monkeypatch, ["7zz", "7za"], 1)
        assert archives.seven_zip_command() == "7zz"
        self._fake_path(tmp_path, monkeypatch, ["7za"], 2)
        assert archives.seven_zip_command() == "7za"

    def test_none_present(self, tmp_path, monkeypatch):
        self._fake_path(tmp_path, monkeypatch, [])
        assert archives.seven_zip_command() == ""


class TestDirname:
    def test_pinned_against_dirname_1(self):
        for name, expected in (
            ("a/b", "a"),
            ("b", "."),
            ("a/b/", "a"),
            ("a//", "."),
            ("/", "/"),
            ("", "."),
            ("/abs/b", "/abs"),
            ("./x", "."),
            ("x/.", "x"),
            ("..", "."),
            (".", "."),
        ):
            assert archives._dirname(name) == expected, name


class TestShadowedByFolder:
    def test_the_pair(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "Some Book").mkdir()
        (tmp_path / "Some Book.zip").write_bytes(b"")
        assert archives.archive_shadowed_by_folder("Some Book.zip")

    def test_no_folder_of_that_name(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "Some Book.zip").write_bytes(b"")
        assert not archives.archive_shadowed_by_folder("Some Book.zip")

    def test_compared_as_spelled(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # The extractors take the name from the archive, so the unpacked copy
        # beside it is spelled exactly as the archive's base name is - a case-
        # folded match would keep an archive that is not a shadow.
        (tmp_path / "some book").mkdir()
        (tmp_path / "Some Book.zip").write_bytes(b"")
        assert not archives.archive_shadowed_by_folder("Some Book.zip")

    def test_one_folder_down(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "sub" / "Book").mkdir(parents=True)
        assert archives.archive_shadowed_by_folder("sub/Book.zip")

    def test_the_directory_part_is_not_the_name(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "books.7z.d" / "Book").mkdir(parents=True)
        assert archives.archive_shadowed_by_folder("books.7z.d/Book.zip")

    def test_a_non_archive_is_its_whole_name(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # The base name of a non-archive keeps its extension, so the folder that
        # shadows it is named with the dot still on it - and the file itself need
        # not exist, because nothing here opens it.
        (tmp_path / "sub" / "notes.txt").mkdir(parents=True)
        assert archives.archive_shadowed_by_folder("sub/notes.txt")

    def test_the_folded_suffix(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "Book").mkdir()
        (tmp_path / "Book.ZİP").write_bytes(b"")
        assert archives.archive_shadowed_by_folder("Book.ZİP")

    def test_a_trailing_slash_has_no_base(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # The base name of a name that ends in a slash is empty, so the answer
        # is no no matter what sits beside it.
        (tmp_path / "sub").mkdir()
        assert not archives.archive_shadowed_by_folder("sub/")


# --- the extractors -------------------------------------------------------------
# What is pinned: the exact argv each suffix dispatches to, and the status the
# tool returns becoming the status the function returns.


# The write mode each suffix of the tar family is packed with.
_TAR_WRITE_MODES = {
    "tar": "w", "tar.gz": "w:gz", "tgz": "w:gz",
    "tar.bz2": "w:bz2", "tbz2": "w:bz2", "tbz": "w:bz2",
    "tar.xz": "w:xz", "txz": "w:xz",
    "tar.zst": "w:zst", "tzst": "w:zst",
}


def _pack_tar(path, mode):
    """A two-entry tar: one file at the top level and one a folder down, so the
    layout the extraction has to reproduce is not flat."""
    with tarfile.open(str(path), mode) as archive:
        for name, body in (("notes.txt", b"top"), ("inner/page.txt", b"deep")):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))


def _pack_tar_zst(path, tmp_path, escaping=False):
    """A .tar.zst made the way a host without tarfile's zstd would have to read
    it: a plain tar, compressed by the zstd binary."""
    plain = tmp_path / "staging.tar"
    if escaping:
        with tarfile.open(str(plain), "w") as archive:
            info = tarfile.TarInfo("../escaped.txt")
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))
    else:
        _pack_tar(plain, "w")
    subprocess.run(["zstd", "-q", "-f", str(plain), "-o", str(path)],
                   check=True)
    plain.unlink()


def _tarfile_without_zstd(monkeypatch):
    """tarfile as the 3.11 and 3.12 series have it: a .tar.zst opened by NAME is
    a format it does not know, while a stream handed to it still reads."""
    real_open = tarfile.open

    def no_zstd(*args, **kwargs):
        if args and isinstance(args[0], str):
            raise tarfile.ReadError("this tarfile has no zstd")
        return real_open(*args, **kwargs)

    monkeypatch.setattr(archives.tarfile, "open", no_zstd)


def _assert_unpacked(dest):
    assert (dest / "notes.txt").read_bytes() == b"top"
    assert (dest / "inner" / "page.txt").read_bytes() == b"deep"


# What each listing tool says about an archive of one ordinary file. The
# dispatch cases are about the EXTRACTION argv, and the listing before it is a
# different tool call with a different question - so it is answered here rather
# than left to fail and take the extraction with it.
_SAFE_LISTINGS = {
    "unrar": "        Name: notes.txt\n        Type: File\n",
    "7z": ("Listing archive: Book.7z\n\n----------\n"
           "Path = notes.txt\nAttributes = A -rw-r--r--\n\n"),
}


def _is_listing(command):
    if command[0] == "unrar" and len(command) > 1 and command[1] == "vt":
        return "unrar"
    if command[0] in ("7z", "7zz", "7za") and len(command) > 1 \
            and command[1] == "l":
        return "7z"
    return ""


def _record_calls(monkeypatch, rc=0, listings=None):
    """The tool calls, with the member listing answered rather than recorded.

    ``listings`` replaces what a listing tool says, which is how a case asks for
    an archive that is refused rather than unpacked.
    """
    calls = []
    answers = dict(_SAFE_LISTINGS)
    answers.update(listings or {})

    def fake_run(command, stdout=None, **_kwargs):
        tool = _is_listing(command)
        if tool:
            return SimpleNamespace(returncode=0,
                                   stdout=answers[tool].encode("utf-8"))
        calls.append((command, stdout))
        return SimpleNamespace(returncode=rc)

    monkeypatch.setattr(archives.subprocess, "run", fake_run)
    return calls


def _pack_zip(path, names=("notes.txt",)):
    """A real zip of ordinary members: what the extraction cases are handed, so
    the member check they now pass through has something true to read."""
    import zipfile
    with zipfile.ZipFile(str(path), "w") as packed:
        for name in names:
            packed.writestr(name, "x")


class TestExtractArchive:
    """Each tool's own arguments, and the "--" before the archive that every one
    of them wants: without it a file named "-dash.zip" is read as options, and
    unzip answers "-d option used more than once" rather than unpacking it."""

    def test_zip(self, tmp_path, monkeypatch):
        calls = _record_calls(monkeypatch)
        dest = tmp_path / "out"
        dest.mkdir()
        _pack_zip(tmp_path / "Book.zip")
        monkeypatch.chdir(tmp_path)
        assert archives.extract_archive("Book.zip", "out") == 0
        assert calls == [(["unzip", "-qq", "-o", "-d", "out", "--", "Book.zip"],
                          archives.subprocess.DEVNULL)]

    def test_rar_keeps_the_archive_layout(self, tmp_path, monkeypatch):
        calls = _record_calls(monkeypatch)
        dest = tmp_path / "out"
        dest.mkdir()
        monkeypatch.chdir(tmp_path)
        assert archives.extract_archive("Book.rar", "out") == 0
        # x, not e: the trailing slash is what makes unrar read the destination
        # as a folder
        assert calls == [(["unrar", "x", "-o+", "-idq", "--", "Book.rar",
                           "out/"], archives.subprocess.DEVNULL)]

    def test_seven_zip_dispatches_to_the_host_binary(self, tmp_path, monkeypatch):
        calls = _record_calls(monkeypatch)
        monkeypatch.setattr(archives, "seven_zip_command", lambda: "7zz")
        dest = tmp_path / "out"
        dest.mkdir()
        monkeypatch.chdir(tmp_path)
        assert archives.extract_archive("Book.7z", "out") == 0
        assert calls == [(["7zz", "x", "-y", "-oout", "--", "Book.7z"],
                          archives.subprocess.DEVNULL)]

    def test_seven_zip_without_a_binary(self, tmp_path, monkeypatch):
        calls = _record_calls(monkeypatch)
        monkeypatch.setattr(archives, "seven_zip_command", lambda: "")
        dest = tmp_path / "out"
        dest.mkdir()
        monkeypatch.chdir(tmp_path)
        assert archives.extract_archive("Book.7z", "out") == 1
        assert calls == []

    @pytest.mark.parametrize("ext", ["tar", "tar.gz", "tgz", "tar.bz2",
                                     "tbz2", "tbz", "tar.xz", "txz"])
    def test_the_tar_family_is_one_reader(self, tmp_path, monkeypatch, ext):
        # the compression is read off the file itself, so the suffix only
        # chooses the name and never the handling: each of them lands the same
        # tree, and none of them reaches for a tool
        calls = _record_calls(monkeypatch)
        dest = tmp_path / "out"
        dest.mkdir()
        _pack_tar(tmp_path / f"Book.{ext}", _TAR_WRITE_MODES[ext])
        monkeypatch.chdir(tmp_path)
        assert archives.extract_archive(f"Book.{ext}", "out") == 0
        _assert_unpacked(dest)
        assert calls == []

    @pytest.mark.parametrize("ext", ["tar.zst", "tzst"])
    def test_zstd_is_read_by_an_interpreter_that_has_it(self, tmp_path,
                                                        monkeypatch, ext):
        if "zst" not in tarfile.TarFile.OPEN_METH:
            pytest.skip("tarfile learned zstd in Python 3.14")
        calls = _record_calls(monkeypatch)
        dest = tmp_path / "out"
        dest.mkdir()
        _pack_tar(tmp_path / f"Book.{ext}", _TAR_WRITE_MODES[ext])
        monkeypatch.chdir(tmp_path)
        assert archives.extract_archive(f"Book.{ext}", "out") == 0
        _assert_unpacked(dest)
        assert calls == []

    @pytest.mark.parametrize("ext", ["tar.zst", "tzst"])
    def test_a_zstd_tarfile_cannot_open_is_decompressed_and_still_filtered(
            self, tmp_path, monkeypatch, ext):
        """The interpreter whose tarfile has no zstd hands the host the
        COMPRESSION and nothing else.

        What lands is still read by tarfile, through the same filter every other
        tar in the family goes through - so which members may be written is this
        package's answer on every interpreter, and not the host tar's.
        """
        if not shutil.which("zstd"):
            pytest.skip("the fallback is the zstd binary")
        dest = tmp_path / "out"
        dest.mkdir()
        _pack_tar_zst(tmp_path / f"Book.{ext}", tmp_path)
        _tarfile_without_zstd(monkeypatch)
        monkeypatch.chdir(tmp_path)

        assert archives.extract_archive(f"Book.{ext}", "out") == 0
        _assert_unpacked(dest)

    @pytest.mark.parametrize("ext", ["tar.zst", "tzst"])
    def test_the_zstd_fallback_refuses_a_member_reaching_outside(
            self, tmp_path, monkeypatch, ext):
        if not shutil.which("zstd"):
            pytest.skip("the fallback is the zstd binary")
        dest = tmp_path / "out"
        dest.mkdir()
        _pack_tar_zst(tmp_path / f"Book.{ext}", tmp_path, escaping=True)
        _tarfile_without_zstd(monkeypatch)
        monkeypatch.chdir(tmp_path)

        assert archives.extract_archive(f"Book.{ext}", "out") == 1
        assert not (tmp_path / "escaped.txt").exists()

    def test_a_zstd_that_is_not_a_tar_at_all_is_a_status_one(self, tmp_path,
                                                             monkeypatch):
        dest = tmp_path / "out"
        dest.mkdir()
        (tmp_path / "Book.tar.zst").write_bytes(b"not a tar at all")
        monkeypatch.chdir(tmp_path)
        assert archives.extract_archive("Book.tar.zst", "out") == 1
        assert list(dest.iterdir()) == []

    def test_a_tar_that_is_not_one_is_a_status_one(self, tmp_path, monkeypatch):
        # and the rest of the family has nothing to fall back to, so an
        # unopenable file is the failure rather than a tool call
        calls = _record_calls(monkeypatch)
        dest = tmp_path / "out"
        dest.mkdir()
        (tmp_path / "Book.tar.gz").write_bytes(b"not a tar at all")
        monkeypatch.chdir(tmp_path)
        assert archives.extract_archive("Book.tar.gz", "out") == 1
        assert calls == []

    def test_a_member_reaching_outside_the_destination_is_refused(
            self, tmp_path, monkeypatch):
        # the tar filter is the tool's own guard: a member spelled through ..
        # fails the extraction rather than being written next to it
        dest = tmp_path / "out"
        dest.mkdir()
        with tarfile.open(str(tmp_path / "Book.tar"), "w") as archive:
            info = tarfile.TarInfo("../escaped.txt")
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))
        monkeypatch.chdir(tmp_path)
        assert archives.extract_archive("Book.tar", "out") == 1
        assert not (tmp_path / "escaped.txt").exists()

    def test_a_suffix_none_of_them_open(self, tmp_path, monkeypatch):
        calls = _record_calls(monkeypatch)
        dest = tmp_path / "out"
        dest.mkdir()
        monkeypatch.chdir(tmp_path)
        assert archives.extract_archive("notes.txt", "out") == 1
        assert calls == []

    def test_a_destination_that_is_not_a_folder(self, tmp_path, monkeypatch):
        calls = _record_calls(monkeypatch)
        monkeypatch.chdir(tmp_path)
        assert archives.extract_archive("Book.zip", "no-such") == 1
        assert calls == []

    def test_the_tools_own_status_is_the_answer(self, tmp_path, monkeypatch):
        # An empty zip is the tool's own verdict, and the verdict is passed
        # through, not smoothed over
        calls = _record_calls(monkeypatch, rc=9)
        dest = tmp_path / "out"
        dest.mkdir()
        _pack_zip(tmp_path / "Book.zip")
        monkeypatch.chdir(tmp_path)
        assert archives.extract_archive("Book.zip", "out") == 9
        assert len(calls) == 1


class TestWhatAnArchiveIsAllowedToAskFor:
    """The policy itself, on the member's own spelling.

    It is tarfile's "tar" filter, written out: nothing absolute, nothing through
    "..", no link that lands outside the tree, and no device or fifo. The three
    unpackers that have no filter are held to it by asking the archive what is
    in it before starting them.
    """

    @pytest.mark.parametrize("name", [
        "/etc/passwd", "//etc/passwd", "C:/Windows/system32/x.dll",
        "../escaped.txt", "a/../../escaped.txt", "a/b/../../../escaped.txt",
    ])
    def test_a_path_that_leaves_the_destination(self, name):
        assert archives.unsafe_member(name)

    @pytest.mark.parametrize("name", [
        "notes.txt", "a/b/page.jpg", "./cover.jpg",
        "..hidden.txt", "a/..b/c.txt", "a:b.txt", "Vol 1: The Start/01.jpg",
    ])
    def test_a_path_that_stays_inside_it(self, name):
        assert archives.unsafe_member(name) == ""

    def test_a_dotted_component_is_refused_even_where_it_cancels_out(self):
        """"a/../b" lands inside the destination and is still refused: a member
        spelled that way is not how anything packs a book, and a rule about the
        SPELLING is one that cannot be walked around with a longer path."""
        assert archives.unsafe_member("a/../b/page.jpg")

    def test_a_link_is_judged_where_it_would_land(self):
        assert archives.unsafe_member("alias.jpg", "link", "cover.jpg") == ""
        assert archives.unsafe_member("sub/alias.jpg", "link",
                                      "../cover.jpg") == ""
        assert archives.unsafe_member("alias", "link", "../../etc")
        assert archives.unsafe_member("alias", "link", "/etc/passwd")

    def test_a_link_whose_target_is_unknown_is_not_unpacked(self):
        """unrar and 7-Zip do not print a link's target, so a link in one of
        them is a link this cannot check - and an unchecked link is the exact
        shape the check exists for."""
        assert archives.unsafe_member("alias", "link", "")

    def test_a_device_or_a_fifo_is_not_content(self):
        assert archives.unsafe_member("dev/null", "special")

    def test_a_folder_is_a_folder(self):
        assert archives.unsafe_member("chapter one/", "dir") == ""


def _rar_listing(*members):
    """What ``unrar vt`` prints, as (name, type) pairs."""
    return "".join("        Name: %s\n        Type: %s\n" % pair
                   for pair in members)


def _seven_zip_listing(*members):
    """What ``7z l -slt`` prints, as (name, attributes) pairs."""
    return ("Listing archive: Book.7z\n\n--\nPath = Book.7z\nType = 7z\n"
            "\n----------\n"
            + "".join("Path = %s\nAttributes = %s\n\n" % pair
                      for pair in members))


class TestAnArchiveThatAsksForTooMuchIsNotUnpacked:
    """The three unpackers with no filter of their own, each asked what is in
    the archive before it is started - and not started at all when the answer
    is a member that would be written outside the destination."""

    def _dest(self, tmp_path, monkeypatch):
        dest = tmp_path / "out"
        dest.mkdir()
        monkeypatch.chdir(tmp_path)
        return dest

    @pytest.mark.parametrize("member", ["../escaped.txt", "/etc/passwd"])
    def test_a_zip_whose_member_escapes(self, tmp_path, monkeypatch, member):
        calls = _record_calls(monkeypatch)
        self._dest(tmp_path, monkeypatch)
        _pack_zip(tmp_path / "Book.zip", ("page.jpg", member))

        assert archives.extract_archive("Book.zip", "out") == 1
        assert calls == [], "the unpacker must not be started at all"

    @pytest.mark.skipif(sys.platform == "win32",
                        reason="making a symlink needs a privilege the CI "
                               "account does not have")
    def test_a_zip_carrying_a_link_out_of_the_tree(self, tmp_path,
                                                   monkeypatch):
        import zipfile
        calls = _record_calls(monkeypatch)
        self._dest(tmp_path, monkeypatch)
        with zipfile.ZipFile(str(tmp_path / "Book.zip"), "w") as packed:
            packed.writestr("page.jpg", "x")
            link = zipfile.ZipInfo("escape")
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            packed.writestr(link, "../../../etc")

        assert archives.extract_archive("Book.zip", "out") == 1
        assert calls == []

    def test_a_zip_whose_link_stays_inside_is_unpacked(self, tmp_path,
                                                       monkeypatch):
        """And the link itself is removed afterwards, which is what the prune
        has always been for: a book is files in folders."""
        import zipfile
        calls = _record_calls(monkeypatch)
        self._dest(tmp_path, monkeypatch)
        with zipfile.ZipFile(str(tmp_path / "Book.zip"), "w") as packed:
            packed.writestr("cover.jpg", "x")
            link = zipfile.ZipInfo("alias.jpg")
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            packed.writestr(link, "cover.jpg")

        assert archives.extract_archive("Book.zip", "out") == 0
        assert len(calls) == 1

    def test_a_zip_that_cannot_be_read_at_all(self, tmp_path, monkeypatch):
        calls = _record_calls(monkeypatch)
        self._dest(tmp_path, monkeypatch)
        (tmp_path / "Book.zip").write_bytes(b"not a zip")

        assert archives.extract_archive("Book.zip", "out") == 1
        assert calls == []

    @pytest.mark.parametrize("listing", [
        _rar_listing(("../escaped.txt", "File")),
        _rar_listing(("escape", "Symbolic link")),
        _rar_listing(("/etc/passwd", "File")),
        "",
    ])
    def test_a_rar_whose_listing_says_so(self, tmp_path, monkeypatch, listing):
        calls = _record_calls(monkeypatch, listings={"unrar": listing})
        self._dest(tmp_path, monkeypatch)

        assert archives.extract_archive("Book.rar", "out") == 1
        assert calls == []

    def test_a_rar_of_ordinary_members_is_unpacked(self, tmp_path,
                                                   monkeypatch):
        calls = _record_calls(monkeypatch, listings={
            "unrar": _rar_listing(("Disc 1", "Directory"),
                                  ("Disc 1/01.mp3", "File"))})
        self._dest(tmp_path, monkeypatch)

        assert archives.extract_archive("Book.rar", "out") == 0
        assert calls[0][0][:2] == ["unrar", "x"]

    @pytest.mark.parametrize("listing", [
        _seven_zip_listing(("../escaped.txt", "A -rw-r--r--")),
        _seven_zip_listing(("escape", "A lrwxrwxrwx")),
        _seven_zip_listing(("dev/null", "A crw-rw-rw-")),
        "",
    ])
    def test_a_7z_whose_listing_says_so(self, tmp_path, monkeypatch, listing):
        calls = _record_calls(monkeypatch, listings={"7z": listing})
        monkeypatch.setattr(archives, "seven_zip_command", lambda: "7z")
        self._dest(tmp_path, monkeypatch)

        assert archives.extract_archive("Book.7z", "out") == 1
        assert calls == []

    def test_a_7z_of_ordinary_members_is_unpacked(self, tmp_path, monkeypatch):
        calls = _record_calls(monkeypatch, listings={
            "7z": _seven_zip_listing(("chapter one", "D drwxr-xr-x"),
                                     ("chapter one/01.jpg", "A -rw-r--r--"))})
        monkeypatch.setattr(archives, "seven_zip_command", lambda: "7z")
        self._dest(tmp_path, monkeypatch)

        assert archives.extract_archive("Book.7z", "out") == 0
        assert calls[0][0][:2] == ["7z", "x"]

    def test_the_listing_is_asked_of_the_tool_that_knows(self, tmp_path,
                                                         monkeypatch):
        """The argv of the question itself: a listing that names the archive
        after "--" cannot be read as options, exactly as the extraction is."""
        asked = []

        def fake_run(command, stdout=None, **_kwargs):
            asked.append(command)
            return SimpleNamespace(returncode=0,
                                   stdout=_SAFE_LISTINGS["unrar"].encode())

        monkeypatch.setattr(archives.subprocess, "run", fake_run)
        self._dest(tmp_path, monkeypatch)
        archives.extract_archive("Book.rar", "out")

        assert asked[0] == ["unrar", "vt", "-idq", "--", "Book.rar"]

    def test_a_refused_archive_leaves_the_destination_empty(self, tmp_path,
                                                            monkeypatch):
        dest = self._dest(tmp_path, monkeypatch)
        _pack_zip(tmp_path / "Book.zip", ("page.jpg", "../escaped.txt"))

        assert archives.extract_archive("Book.zip", "out") == 1
        assert list(dest.iterdir()) == []
        assert not (tmp_path / "escaped.txt").exists()

    def test_and_leaves_no_folder_behind_when_it_was_the_book(self, tmp_path,
                                                              monkeypatch):
        """extract_archive_as_folder unpacks into a temporary sibling first, so
        a refusal there must not leave one - the parent holds what it held."""
        monkeypatch.chdir(tmp_path)
        _pack_zip(tmp_path / "Book.zip", ("page.jpg", "../escaped.txt"))

        assert archives.extract_archive_as_folder("Book.zip", "Book") == 1
        assert sorted(os.listdir(str(tmp_path))) == ["Book.zip"]


def _fake_extraction(monkeypatch, layout, rc=0):
    """An extractArchive that writes the given layout into its destination.

    ``layout`` maps a relative path to "file" or "link:<target>": the shapes a
    real extractor would leave, without the tool.
    """

    def fake_extract(file, dest):
        for rel, kind in layout.items():
            path = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if kind.startswith("link:"):
                os.symlink(kind[len("link:"):], path)
            else:
                with open(path, "wb") as handle:
                    handle.write(b"")
        return rc

    monkeypatch.setattr(archives, "extract_archive", fake_extract)


class TestWhatIsLeftBehindIsFilesAndFolders:
    """An archive is content, and content is files in folders.

    The one that matters is the symlink: unzip and 7-Zip drop "../" from an
    ordinary member's path, but write a symlink MEMBER exactly as asked, and a
    tree with a link out of it is one every later step can be walked out of.
    """

    @pytest.mark.skipif(sys.platform == "win32",
                        reason="making a symlink needs a privilege the CI "
                               "account does not have")
    def test_a_link_out_of_the_tree_is_removed_and_its_target_is_not(
            self, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_text("not this run's file")
        dest = tmp_path / "out"
        (dest / "inner").mkdir(parents=True)
        (dest / "inner" / "page.txt").write_text("content")
        (dest / "escape").symlink_to(tmp_path)
        (dest / "inner" / "victim").symlink_to(outside)

        assert archives.prune_irregular(str(dest)) == 2
        assert not os.path.lexists(str(dest / "escape"))
        assert not os.path.lexists(str(dest / "inner" / "victim"))
        # unlink never follows: what went is the link, never what it pointed at
        assert outside.read_text() == "not this run's file"
        assert (dest / "inner" / "page.txt").read_text() == "content"

    @pytest.mark.skipif(not hasattr(os, "mkfifo"),
                        reason="a fifo is a POSIX file type")
    def test_a_fifo_goes_too(self, tmp_path):
        os.mkfifo(str(tmp_path / "pipe"))
        assert archives.prune_irregular(str(tmp_path)) == 1
        assert not (tmp_path / "pipe").exists()

    def test_an_ordinary_tree_is_left_exactly_as_it_is(self, tmp_path):
        (tmp_path / "a" / "b").mkdir(parents=True)
        (tmp_path / "a" / "b" / "page.txt").write_text("x")
        assert archives.prune_irregular(str(tmp_path)) == 0
        assert (tmp_path / "a" / "b" / "page.txt").read_text() == "x"

    @pytest.mark.skipif(sys.platform == "win32",
                        reason="making a symlink needs a privilege the CI "
                               "account does not have")
    def test_an_extraction_that_carried_one_still_succeeds_without_it(
            self, tmp_path, monkeypatch):
        """A link pointing INSIDE is one the tar filter allows, so it is the
        case that reaches the prune. The archive is otherwise good content and
        is not turned into a failure over it."""
        dest = tmp_path / "out"
        dest.mkdir()
        with tarfile.open(str(tmp_path / "Book.tar"), "w") as archive:
            info = tarfile.TarInfo("page.txt")
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))
            link = tarfile.TarInfo("alias.txt")
            link.type = tarfile.SYMTYPE
            link.linkname = "page.txt"
            archive.addfile(link)
        monkeypatch.chdir(tmp_path)

        assert archives.extract_archive("Book.tar", "out") == 0
        assert (dest / "page.txt").read_bytes() == b"x"
        assert not os.path.lexists(str(dest / "alias.txt"))


class TestExtractAsFolder:
    def test_the_redundant_top_level_is_dropped(self, tmp_path, monkeypatch):
        _fake_extraction(monkeypatch, {"Book/01.mp3": "file",
                                       "Book/02.mp3": "file"})
        monkeypatch.chdir(tmp_path)
        assert archives.extract_archive_as_folder("Book.zip", "Book") == 0
        entries = sorted(os.listdir("Book"))
        assert entries == ["01.mp3", "02.mp3"]

    def test_the_walk_goes_to_the_end_of_the_chain(self, tmp_path, monkeypatch):
        _fake_extraction(monkeypatch, {"A/level 1/level 2/track.mp3": "file"})
        monkeypatch.chdir(tmp_path)
        assert archives.extract_archive_as_folder("Book.zip", "Book") == 0
        assert sorted(os.listdir("Book")) == ["track.mp3"]

    def test_files_at_the_top_are_renamed_as_they_are(self, tmp_path, monkeypatch):
        _fake_extraction(monkeypatch, {"01.mp3": "file", "02.jpg": "file"})
        monkeypatch.chdir(tmp_path)
        assert archives.extract_archive_as_folder("Book.zip", "Book") == 0
        assert sorted(os.listdir("Book")) == ["01.mp3", "02.jpg"]

    def test_several_top_levels_stop_the_walk(self, tmp_path, monkeypatch):
        _fake_extraction(monkeypatch, {"Book/01.mp3": "file", "Extras/cue.txt": "file"})
        monkeypatch.chdir(tmp_path)
        assert archives.extract_archive_as_folder("Book.zip", "Book") == 0
        assert sorted(os.listdir("Book")) == ["Book", "Extras"]

    def test_a_link_is_the_one_entry_and_is_left_where_it_is(self, tmp_path, monkeypatch):
        # The walk follows a folder it can stand in: a link is one entry that is
        # not a folder it can stand in, so the raw comes out as it is.
        _fake_extraction(monkeypatch, {"Book/link": "link:gone.txt"})
        monkeypatch.chdir(tmp_path)
        assert archives.extract_archive_as_folder("Book.zip", "Book") == 0
        assert os.path.islink(os.path.join("Book", "link"))

    def test_an_empty_archive_is_an_empty_folder(self, tmp_path, monkeypatch):
        _fake_extraction(monkeypatch, {})
        monkeypatch.chdir(tmp_path)
        assert archives.extract_archive_as_folder("Book.7z", "Book") == 0
        assert os.path.isdir("Book")
        assert os.listdir("Book") == []

    def test_a_destination_that_exists(self, tmp_path, monkeypatch):
        _fake_extraction(monkeypatch, {"Book/01.mp3": "file"})
        (tmp_path / "Book").mkdir()
        monkeypatch.chdir(tmp_path)
        assert archives.extract_archive_as_folder("Book.zip", "Book") == 1
        assert os.listdir("Book") == []

    def test_a_trailing_slash_is_stripped(self, tmp_path, monkeypatch):
        _fake_extraction(monkeypatch, {"01.mp3": "file"})
        monkeypatch.chdir(tmp_path)
        assert archives.extract_archive_as_folder("Book.zip", "Book/") == 0
        assert sorted(os.listdir("Book")) == ["01.mp3"]

    def test_the_parent_is_created_when_missing(self, tmp_path, monkeypatch):
        _fake_extraction(monkeypatch, {"01.mp3": "file"})
        monkeypatch.chdir(tmp_path)
        assert archives.extract_archive_as_folder("Book.zip", "box/Book") == 0
        assert sorted(os.listdir(os.path.join("box", "Book"))) == ["01.mp3"]

    def test_a_failed_extraction_leaves_nothing(self, tmp_path, monkeypatch):
        _fake_extraction(monkeypatch, {"Book/01.mp3": "file"}, rc=2)
        monkeypatch.chdir(tmp_path)
        assert archives.extract_archive_as_folder("Book.zip", "Book") == 1
        assert not os.path.exists("Book")
        # the temporary sibling is the thing a careless implementation leaves:
        # the parent holds exactly what it held before
        assert os.listdir(tmp_path) == []

    def test_a_failed_rename_leaves_nothing_either(self, tmp_path, monkeypatch):
        _fake_extraction(monkeypatch, {"Book/01.mp3": "file"})

        def no_rename(source, destination):
            raise OSError("cross-device")

        monkeypatch.setattr(os, "rename", no_rename)
        monkeypatch.chdir(tmp_path)
        assert archives.extract_archive_as_folder("Book.zip", "Book") == 1
        assert not os.path.exists("Book")
        assert os.listdir(tmp_path) == []