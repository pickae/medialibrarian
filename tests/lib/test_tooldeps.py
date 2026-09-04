"""Tests for medialib.lib.tooldeps - the external-tool preflight.

What is pinned here: the two faces of the presence test (bash's PATH scan does not
test the executable bit, while a slash-path does), the exact byte-padded layout of
the refusal, the notes table and its generic fallback, and the module-import
check's fresh-process form.
"""

import io
import os

import pytest

from medialib.lib import tooldeps

pytestmark = pytest.mark.fs


@pytest.fixture
def pathdir(tmp_path, monkeypatch):
    """A scratch directory on PATH; the test builds whatever the case needs in
    it and the rest of the machine's PATH is out of the question."""
    d = tmp_path / "bin"
    d.mkdir()
    monkeypatch.setenv("PATH", str(d))
    return d


def _file(d, name, mode=0o755, content=b"x"):
    p = d / name
    p.write_bytes(content)
    os.chmod(p, mode)
    return p


# --- tool_present ---------------------------------------------------------------


class TestToolPresent:
    def test_a_plain_name_on_path(self, pathdir):
        _file(pathdir, "ffmpeg")
        assert tooldeps.tool_present("ffmpeg") is True

    def test_a_plain_name_not_on_path(self, pathdir):
        assert tooldeps.tool_present("definitelyNotAToolQqq") is False

    def test_the_executable_bit_is_not_required_on_path(self, pathdir):
        # bash's PATH scan does not test X_OK: a 644 file reads as present
        _file(pathdir, "noexec", mode=0o644)
        assert tooldeps.tool_present("noexec") is True

    def test_a_fifo_on_path_reads_as_present(self, pathdir):
        os.mkfifo(pathdir / "fifo1")
        assert tooldeps.tool_present("fifo1") is True

    def test_a_directory_on_path_reads_as_absent(self, pathdir):
        (pathdir / "dir1").mkdir()
        assert tooldeps.tool_present("dir1") is False

    def test_symlinks_are_followed(self, pathdir):
        _file(pathdir, "target")
        os.symlink(pathdir / "target", pathdir / "linktoreg")
        (pathdir / "adir").mkdir()
        os.symlink(pathdir / "adir", pathdir / "linktodir")
        os.symlink(pathdir / "nowhere", pathdir / "linkbroken")
        assert tooldeps.tool_present("linktoreg") is True
        assert tooldeps.tool_present("linktodir") is False
        assert tooldeps.tool_present("linkbroken") is False

    def test_a_name_in_a_later_path_entry(self, pathdir, tmp_path, monkeypatch):
        later = tmp_path / "later"
        later.mkdir()
        _file(later, "mkvmerge")
        monkeypatch.setenv("PATH", "{}:{}".format(pathdir, later))
        assert tooldeps.tool_present("mkvmerge") is True

    def test_alternatives_any_one_satisfies(self, pathdir):
        _file(pathdir, "present")
        assert tooldeps.tool_present("absentA|present|absentB") is True
        assert tooldeps.tool_present("present|absentA") is True
        assert tooldeps.tool_present("absentA|absentB") is False

    def test_an_empty_alternative_never_satisfies(self, pathdir):
        # the middle of "a||b" is the empty name, which no directory holds
        assert tooldeps.tool_present("a||b") is False
        assert tooldeps.tool_present("|a") is False
        assert tooldeps.tool_present("a|") is False
        _file(pathdir, "a")
        assert tooldeps.tool_present("a||b") is True
        assert tooldeps.tool_present("|a") is True
        assert tooldeps.tool_present("a|") is True

    def test_an_empty_spec_is_absent(self, pathdir):
        assert tooldeps.tool_present("") is False

    def test_a_name_with_a_space_and_one_leading_with_a_dash(self, pathdir):
        _file(pathdir, "my tool")
        assert tooldeps.tool_present("my tool") is True
        assert tooldeps.tool_present("-x") is False

    def test_a_slash_path_is_tested_directly(self, pathdir, tmp_path, monkeypatch):
        # a name with a slash is not looked up on PATH at all: it is checked
        # where it stands, relative to the working directory
        monkeypatch.chdir(tmp_path)
        inner = tmp_path / "sub"
        inner.mkdir()
        _file(inner, "exec2")
        _file(inner, "noexec2", mode=0o644)
        os.mkfifo(inner / "pipe2")
        assert tooldeps.tool_present("sub/exec2") is True
        assert tooldeps.tool_present("sub/noexec2") is False
        assert tooldeps.tool_present("sub/pipe2") is False
        assert tooldeps.tool_present("sub/nope") is False
        # the same name without the slash is a PATH lookup and misses
        assert tooldeps.tool_present("exec2") is False


# --- tool_note -------------------------------------------------------------------


class TestToolNote:
    def test_a_known_name_carries_its_role_and_hint(self):
        assert tooldeps.tool_note("ffmpeg") == "audio/video de- and encoding|apt install ffmpeg"

    def test_a_url_hint_is_carried_verbatim(self):
        assert tooldeps.tool_note("dovi_tool") == (
            "Dolby Vision RPU extraction and conversion"
            "|https://github.com/quietvoid/dovi_tool (release binary)")

    def test_one_package_names_that_package_for_every_binary(self):
        for tool in ("mkvmerge", "mkvpropedit", "mkvextract"):
            assert tooldeps.tool_note(tool).split("|", 1)[1] == "apt install mkvtoolnix"
        for tool in ("ffmpeg", "ffprobe"):
            assert tooldeps.tool_note(tool).split("|", 1)[1] == "apt install ffmpeg"
        for tool in ("pdftoppm", "pdfinfo", "pdfimages", "pdftotext"):
            assert tooldeps.tool_note(tool).split("|", 1)[1] == "apt install poppler-utils"

    def test_the_three_7z_names_share_their_package(self):
        for tool in ("7z", "7zz", "7za"):
            assert tooldeps.tool_note(tool).split("|", 1)[1] == "apt install p7zip-full"

    def test_an_unknown_name_falls_to_the_generic_line(self):
        assert tooldeps.tool_note("definitelyNotAToolQqq") == (
            'required by this script|check your package manager for "definitelyNotAToolQqq"')


# --- require_tools -----------------------------------------------------------------


class TestRequireTools:
    def test_all_present_is_silent_and_zero(self, pathdir):
        _file(pathdir, "bash")
        buf = io.StringIO()
        status = tooldeps.require_tools("some-command", ["bash", "x|bash"], file=buf)
        assert status == 0
        assert buf.getvalue() == ""

    def test_a_single_miss_is_phrased_in_the_singular(self, pathdir):
        buf = io.StringIO()
        status = tooldeps.require_tools("some-command", ["definitelyNotAToolQqq"], file=buf)
        assert status == 1
        assert buf.getvalue() == (
            "\n"
            "Cannot run some-command: it needs a tool this machine does not have.\n\n"
            "  definitelyNotAToolQqq  required by this script"
            '  check your package manager for "definitelyNotAToolQqq"\n'
            "\nInstall the above (or put it on PATH) and run again. Nothing was changed.\n")

    def test_every_miss_is_named_in_caller_order(self, pathdir):
        buf = io.StringIO()
        status = tooldeps.require_tools(
            "the conversion", ["alsoNotAToolZzz", "definitelyNotAToolQqq"], file=buf)
        assert status == 1
        assert buf.getvalue() == (
            "\n"
            "Cannot run the conversion: it needs 2 tools this machine does not have.\n\n"
            "  alsoNotAToolZzz        required by this script"
            '  check your package manager for "alsoNotAToolZzz"\n'
            "  definitelyNotAToolQqq  required by this script"
            '  check your package manager for "definitelyNotAToolQqq"\n'
            "\nInstall the above (or put it on PATH) and run again. Nothing was changed.\n")

    def test_a_present_tool_is_not_listed(self, pathdir):
        _file(pathdir, "bash")
        buf = io.StringIO()
        tooldeps.require_tools("s", ["bash", "definitelyNotAToolQqq"], file=buf)
        assert "bash" not in buf.getvalue()

    def test_an_alternatives_spec_is_reported_as_the_alternatives(self, pathdir):
        # the name is every candidate joined with " or ", and the note is the
        # first candidate's: an unknown first candidate gets the generic line,
        # a known one its table entry
        buf = io.StringIO()
        tooldeps.require_tools("s", ["absentA|absentB"], file=buf)
        assert "  absentA or absentB  required by this script" in buf.getvalue()
        buf = io.StringIO()
        tooldeps.require_tools("s", ["mkvmerge|absentB"], file=buf)
        assert "  mkvmerge or absentB  muxing Matroska (chapters, track flags)" \
            in buf.getvalue()

    def test_the_column_width_is_the_longest_missing_name(self, pathdir):
        buf = io.StringIO()
        # "definitelyNotAToolQqq" (21 bytes) is the widest name; "mkvmerge" (8)
        # is padded to it, and the role column pads to its own longest entry
        tooldeps.require_tools("s", ["mkvmerge", "definitelyNotAToolQqq"], file=buf)
        assert buf.getvalue() == (
            "\n"
            "Cannot run s: it needs 2 tools this machine does not have.\n\n"
            "  mkvmerge               muxing Matroska (chapters, track flags)"
            "  apt install mkvtoolnix\n"
            "  definitelyNotAToolQqq  required by this script"
            "                  check your package manager for \"definitelyNotAToolQqq\"\n"
            "\nInstall the above (or put it on PATH) and run again. Nothing was changed.\n")

    def test_the_padding_is_counted_in_bytes_not_characters(self, pathdir):
        # "Über" is 4 characters and 5 bytes; in a 7-byte field it takes 2 spaces,
        # where a character count would give 3
        buf = io.StringIO()
        tooldeps.require_tools("s", ["Über", "abcdefg"], file=buf)
        assert buf.getvalue() == (
            "\n"
            "Cannot run s: it needs 2 tools this machine does not have.\n\n"
            "  Über    required by this script  check your package manager for \"Über\"\n"
            "  abcdefg  required by this script  check your package manager for \"abcdefg\"\n"
            "\nInstall the above (or put it on PATH) and run again. Nothing was changed.\n")

    def test_the_what_is_quoted_back_verbatim(self, pathdir):
        buf = io.StringIO()
        tooldeps.require_tools("simulation mode (-s)", ["definitelyNotAToolQqq"], file=buf)
        assert buf.getvalue().split("\n")[1] == (
            "Cannot run simulation mode (-s): it needs a tool this machine does not have.")

    def test_the_message_goes_to_the_given_file_not_elsewhere(self, pathdir):
        buf = io.StringIO()
        tooldeps.require_tools("s", ["definitelyNotAToolQqq"], file=buf)
        assert "Cannot run s" in buf.getvalue()

    def test_skip_preflight_is_a_silent_zero(self, pathdir):
        buf = io.StringIO()
        status = tooldeps.require_tools(
            "s", ["definitelyNotAToolQqq", "alsoNotAToolZzz"],
            skip_preflight=True, file=buf)
        assert status == 0
        assert buf.getvalue() == ""


# --- require_python_module -----------------------------------------------------------


class TestRequirePythonModule:
    def test_an_importable_module_is_silent_and_zero(self):
        buf = io.StringIO()
        status = tooldeps.require_python_module("json", "some-command", file=buf)
        assert status == 0
        assert buf.getvalue() == ""

    def test_a_dotted_module_is_imported_as_given(self):
        buf = io.StringIO()
        assert tooldeps.require_python_module("json.encoder", "s", file=buf) == 0
        assert buf.getvalue() == ""

    def test_a_missing_module_is_refused_with_its_line(self):
        buf = io.StringIO()
        status = tooldeps.require_python_module(
            "notARealModuleQqq", "some-command", "does a thing", file=buf)
        assert status == 1
        assert buf.getvalue() == (
            "\n"
            'Cannot run some-command: the Python "notARealModuleQqq" '
            'package is not importable.\n\n'
            "  notARealModuleQqq  does a thing\n"
            "                     apt install python3-notARealModuleQqq  "
            "(or: pip install notARealModuleQqq)\n"
            "\nInstall it and run again. Nothing was changed.\n")

    def test_the_role_defaults(self):
        buf = io.StringIO()
        tooldeps.require_python_module("notARealModuleQqq", "s", file=buf)
        assert "  notARealModuleQqq  used by this script\n" in buf.getvalue()

    def test_the_indent_matches_the_module_length(self):
        # the line is two spaces, one space per character of the module, then two
        buf = io.StringIO()
        tooldeps.require_python_module("xyz", "s", "the tags", file=buf)
        assert "       apt install python3-xyz  (or: pip install xyz)\n" in buf.getvalue()

    def test_the_indent_is_counted_in_characters(self):
        # "Über" is 4 characters (5 bytes): the shell's ${module//?/ } gives 4 spaces
        buf = io.StringIO()
        tooldeps.require_python_module("Über", "s", file=buf)
        assert "    apt install python3-Über  (or: pip install Über)\n" in buf.getvalue()

    def test_a_module_with_a_space_is_refused(self):
        # the shell builds `import foo bar`, a syntax error: refused, and the
        # space is quoted back and counted for the indent
        buf = io.StringIO()
        status = tooldeps.require_python_module("foo bar", "s", file=buf)
        assert status == 1
        assert 'the Python "foo bar" package' in buf.getvalue()
        assert "    apt install python3-foo bar" in buf.getvalue()

    def test_skip_preflight_is_a_silent_zero(self):
        buf = io.StringIO()
        status = tooldeps.require_python_module(
            "notARealModuleQqq", "s", skip_preflight=True, file=buf)
        assert status == 0
        assert buf.getvalue() == ""

# --- the same refusal, addressed to a Mac ----------------------------------------


class TestTheMacosHints:
    """What a refusal tells a Mac user to run.

    The role half of every line is the same sentence everywhere - what the tool
    is FOR does not change with the host. What changes is the half after it,
    and an apt command on a machine that has no apt is not a smaller failure
    than no hint at all: it is a hint that cannot work, printed at the one
    moment the user is looking for one that can.
    """

    def test_the_role_is_the_same_sentence_and_only_the_hint_moves(self):
        role, hint = tooldeps.tool_note("ffmpeg", "darwin").split("|", 1)
        assert role == tooldeps.tool_note("ffmpeg", "linux").split("|", 1)[0]
        assert hint == "brew install ffmpeg"

    def test_every_tool_the_table_names_has_a_line_for_a_mac(self):
        """The structural half. A tool added to the apt table with nothing
        here would print `apt install ...` to a Mac, and nothing would notice
        until somebody was standing in front of that message."""
        missing = sorted(name for name in tooldeps._TOOL_NOTES
                         if name not in tooldeps._MACOS_HINTS)
        assert missing == []

    def test_and_no_line_here_names_a_tool_the_table_does_not(self):
        """The other direction: a hint for a tool nothing asks for is a hint
        that will never be printed, and a name that was renamed above."""
        extra = sorted(name for name in tooldeps._MACOS_HINTS
                       if name not in tooldeps._TOOL_NOTES)
        assert extra == []

    def test_the_two_formulae_that_are_not_named_after_their_binary(self):
        """7-Zip's formula is "sevenzip" and installs `7zz`; unrar was dropped
        from homebrew-core over its licence, so the tap is named in full. Both
        are the kind of thing a user cannot guess from the binary's name, which
        is the whole reason the hint exists."""
        for tool in ("7z", "7zz", "7za"):
            assert tooldeps.tool_note(tool, "darwin").split("|", 1)[1] == (
                "brew install sevenzip")
        assert "carlocab" in tooldeps.tool_note("unrar", "darwin")

    def test_what_ships_with_the_system_says_so_rather_than_naming_a_formula(
            self):
        for tool in ("curl", "unzip", "zip", "tar"):
            assert "ships with macOS" in tooldeps.tool_note(tool, "darwin")

    def test_an_unknown_name_falls_to_a_generic_line_a_mac_can_run(self):
        assert tooldeps.tool_note("definitelyNotAToolQqq", "darwin") == (
            'required by this script|brew search "definitelyNotAToolQqq"')

    def test_the_whole_refusal_is_addressed_to_the_host_it_prints_on(
            self, monkeypatch, pathdir):
        monkeypatch.setattr(tooldeps.hostos, "is_macos", lambda *_a: True)
        buf = io.StringIO()
        status = tooldeps.require_tools("s", ["mkvmerge"], file=buf)
        assert status == 1
        assert "brew install mkvtoolnix" in buf.getvalue()
        assert "apt install" not in buf.getvalue()

    def test_a_python_package_is_pip_alone_there(self, monkeypatch):
        """Homebrew carries almost none of these as formulae, and the distro
        package that is the first thing to reach for on Debian has no
        counterpart."""
        monkeypatch.setattr(tooldeps.hostos, "is_macos", lambda *_a: True)
        buf = io.StringIO()
        tooldeps.require_python_module("notARealModuleQqq", "s", file=buf)
        assert "pip install notARealModuleQqq" in buf.getvalue()
        assert "apt install" not in buf.getvalue()
