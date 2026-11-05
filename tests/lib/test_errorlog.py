"""The white box for medialib/lib/errorlog.py - the errors a run keeps under
`logs/error/`, one file per run.

The tools here are this interpreter, told what to print and how to exit.
"""

import errno
import io
import os
import re
import sys
from pathlib import Path

import pytest

from medialib.lib import errorlog, ramscratch, runlog, statusline, toolcapture
from tests import blackbox

REPO = Path(__file__).resolve().parents[2]


def tool(script: str) -> list:
    return [sys.executable, "-c", script]


@pytest.fixture(autouse=True)
def no_status_row(monkeypatch):
    monkeypatch.setattr(statusline.state, "row", "")


@pytest.fixture
def error_dir(tmp_path, monkeypatch):
    """The run's error log lands under the test's own directory."""
    monkeypatch.setenv("CLI_SCRIPT_DIR", str(tmp_path))
    monkeypatch.delenv("LOG_TIMESTAMPS", raising=False)
    return tmp_path / "logs" / errorlog.ERROR_DIR


def only_file(error_dir: Path) -> str:
    files = list(error_dir.iterdir())
    assert len(files) == 1, files
    return files[0].read_text(encoding="utf-8")


@pytest.mark.fs
class TestRecord:
    def test_nothing_is_written_until_an_error_arrives(self, error_dir):
        errorlog.start_run(["in", "out"])
        assert not error_dir.exists()

    def test_a_record_names_when_what_and_the_run(self, error_dir):
        errorlog.start_run(["-j", "2", "/in"])
        path = errorlog.record("tool-failed", "ffmpeg exited with status 1")
        text = only_file(error_dir)
        assert path == str(error_dir / os.listdir(error_dir)[0])
        assert re.search(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} [+-]\d{4}  "
                         r"ERROR  tool-failed$", text, re.M)
        assert "what:     ffmpeg exited with status 1" in text
        assert "run:      " in text and "-j 2 /in" in text
        assert "means:    %s" % errorlog.KINDS["tool-failed"].means in text

    def test_the_file_is_named_for_the_run_and_shared_by_its_records(
            self, error_dir):
        errorlog.start_run([])
        first = errorlog.record("usage", "one")
        second = errorlog.record("crash", "two")
        assert first == second
        assert re.match(r"\d{8}-\d{6}-.+-\d+\.log$", os.path.basename(first))
        assert only_file(error_dir).count("=" * 78) == 2

    def test_an_unknown_kind_is_kept_rather_than_lost(self, error_dir):
        errorlog.record("no-such-kind", "still said")
        assert "ERROR  error (no-such-kind)" in only_file(error_dir)

    def test_secrets_are_starred_out(self, error_dir, monkeypatch):
        monkeypatch.setenv("plexToken", "sekrit-value-123")
        errorlog.record("error", "GET /x?api_key=abc123 with sekrit-value-123")
        text = only_file(error_dir)
        assert "sekrit-value-123" not in text and "abc123" not in text

    def test_an_unwritable_home_costs_the_record_not_the_run(self, tmp_path,
                                                             monkeypatch):
        blocker = tmp_path / "file"
        blocker.write_text("")
        monkeypatch.setenv("CLI_SCRIPT_DIR", str(blocker))
        assert errorlog.record("error", "lost") == ""

    def test_where_in_the_commands_it_was_asked_from_is_kept(self, error_dir):
        # Compiled as if it sat in the package: only the commands' own frames
        # are kept, and a test's are not among them.
        caller = REPO / "medialib" / "cli" / "somewhere.py"
        code = compile('errorlog.record("error", "from here")', str(caller),
                       "exec")
        exec(code, {"errorlog": errorlog})
        text = only_file(error_dir)
        assert "called from (most recent call last):" in text
        assert 'somewhere.py", line 1' in text
        assert 'test_errorlog.py", line' not in text


@pytest.mark.fs
class TestFiles:
    def test_a_scratch_file_names_the_hard_drive_file_it_came_from(
            self, error_dir, tmp_path, monkeypatch):
        scratch = tmp_path / "shm" / "run"
        scratch.mkdir(parents=True)
        monkeypatch.setattr(ramscratch._STATE, "ram_base", str(scratch))
        source = tmp_path / "library" / "Book" / "01.mp3"
        source.parent.mkdir(parents=True)
        source.write_text("")
        copy = scratch / "convertAudio.x"
        copy.mkdir()
        errorlog.note_origin(str(copy), str(source))
        errorlog.record("tool-failed", "boom",
                        tool_argv=["ffmpeg", "-i", str(copy / "01.wav"), "-y",
                                   str(tmp_path / "out.opus")])
        text = only_file(error_dir)
        assert "%s  [RAM scratch, made from %s, not there]" % (
            copy / "01.wav", source) in text
        assert "%s  [not there]" % (tmp_path / "out.opus") in text
        assert "tool:     ffmpeg -i" in text

    def test_the_origin_reaches_another_process_through_the_scratch(
            self, tmp_path, monkeypatch):
        scratch = tmp_path / "run"
        scratch.mkdir()
        monkeypatch.setattr(ramscratch._STATE, "ram_base", str(scratch))
        errorlog.note_origin(str(scratch / "book"), str(tmp_path / "b.cbz"))
        monkeypatch.setattr(errorlog, "_ORIGINS", {})
        assert errorlog._origin_of(str(scratch / "book" / "p1.png")) == str(
            tmp_path / "b.cbz")

    def test_the_item_in_hand_is_named(self, error_dir, tmp_path):
        source = tmp_path / "film.mkv"
        source.write_text("")
        with errorlog.working_on(str(source), item="film.mkv"):
            errorlog.record("tool-failed", "boom")
        errorlog.record("tool-failed", "after")
        text = only_file(error_dir)
        assert "item:     film.mkv" in text
        assert "input:    %s" % source in text
        assert text.count("item:") == 1

    def test_paths_are_found_in_key_value_words(self):
        assert errorlog._paths_in(["mkvmerge", "--output=/x/y.mkv",
                                   "file:/a/b", "-q"]) == ["/x/y.mkv", "/a/b"]


@pytest.mark.pure
class TestKinds:
    def test_an_exception_is_kept_as_what_it_says_about_the_machine(self):
        assert errorlog.crash_kind(OSError(errno.ENOSPC, "full")) == "disk-full"
        assert errorlog.crash_kind(PermissionError(errno.EACCES, "no")) == \
            "permission"
        assert errorlog.crash_kind(MemoryError()) == "out-of-memory"
        assert errorlog.crash_kind(ValueError("x")) == "crash"

    def test_every_crash_kind_is_a_kind(self):
        for kind in ("disk-full", "permission", "out-of-memory", "crash"):
            assert kind in errorlog.KINDS

    @pytest.mark.parametrize("line, key", [
        ("[mp3float @ 0x5] Header missing", "decode-error"),
        ("Error while decoding stream #0:0: Invalid data found when processing "
         "input", "decode-error"),
        ("moov atom not found", "invalid-data"),
        ("av_interleaved_write_frame(): No space left on device", "disk-full"),
        ("RuntimeError: CUDA out of memory. Tried to allocate", "out-of-memory"),
        ("ERROR: [youtube] x: Sign in to confirm you're not a bot", "blocked"),
        ("frame= 100 fps= 25 q=28.0 size= 1024kB", ""),
        ("Non-monotonous DTS in output stream 0:1", ""),
    ])
    def test_notable_lines(self, line, key):
        assert errorlog.notable_key(line) == key

    def test_every_kind_is_listed_in_the_docs(self):
        page = (REPO / "docs" / "output.md").read_text(encoding="utf-8")
        missing = [key for key in errorlog.KINDS if "`%s`" % key not in page]
        assert missing == []

    def test_every_kind_the_code_names_is_in_the_catalogue(self):
        named = set()
        pattern = re.compile(
            r'(?:errorlog\.record\(\s*|kind=)"([a-z-]+)"')
        for path in (REPO / "medialib").rglob("*.py"):
            named |= set(pattern.findall(path.read_text(encoding="utf-8")))
        assert named and named <= set(errorlog.KINDS), named - set(errorlog.KINDS)


@pytest.mark.stubbed
class TestToolRuns:
    def test_a_failure_says_its_likely_cause(self, error_dir, capfd):
        script = ("import sys; print('write: No space left on device'); "
                  "sys.exit(1)")
        result = toolcapture.run(tool(script), "a.flac")
        err = capfd.readouterr().err
        assert "    likely cause: the disk or the RAM scratch is full\n" in err
        text = only_file(error_dir)
        assert "ERROR  tool-failed" in text
        assert "cause:    the disk or the RAM scratch is full" in text
        assert "  | write: No space left on device" in text
        assert result.log_path == str(next(error_dir.iterdir()))

    def test_a_skip_is_kept_as_a_warning(self, error_dir):
        toolcapture.run(tool("raise SystemExit(2)"), warning="no subtitles")
        assert "WARNING  tool-skipped" in only_file(error_dir)

    def test_a_tool_that_cannot_start_is_its_own_kind(self, error_dir):
        toolcapture.run([str(error_dir / "no-such-tool")])
        assert "ERROR  tool-not-started" in only_file(error_dir)

    def test_a_success_that_reports_damage_is_warned_about(self, error_dir,
                                                           capfd):
        script = ("print('Stream mapping'); "
                  "[print('[mp3float @ 0x1] Header missing') for _ in range(3)]")
        result = toolcapture.run(tool(script), "01.mp3")
        assert result.ok
        err = capfd.readouterr().err
        assert "WARNING: 01.mp3: " in err and "damaged frames" in err
        assert "    | [mp3float @ 0x1] Header missing\n" in err
        assert "    ... and 2 more line(s) like it or worse\n" in err
        assert "WARNING  tool-warning" in only_file(error_dir)

    def test_quiet_keeps_the_damage_without_saying_it(self, error_dir, capfd,
                                                      monkeypatch):
        monkeypatch.setenv("LOG_VERBOSITY", "quiet")
        toolcapture.run(tool("print('moov atom not found')"))
        assert capfd.readouterr().err == ""
        assert "tool-warning" in only_file(error_dir)

    def test_a_clean_success_keeps_nothing(self, error_dir, capfd):
        toolcapture.run(tool("print('frame=1 Non-monotonous DTS')"))
        assert capfd.readouterr().err == ""
        assert not error_dir.exists()


@pytest.mark.fs
class TestConsole:
    def test_an_error_line_is_kept(self, error_dir, capfd, tmp_path):
        runlog.error("it broke", kind="output-check", files=[str(tmp_path)])
        assert capfd.readouterr().err == "==> ERROR: it broke\n"
        text = only_file(error_dir)
        assert "ERROR  output-check" in text and "what:     it broke" in text

    def test_a_line_written_into_a_report_is_not(self, error_dir):
        runlog.error("part of a block", stream=io.StringIO())
        assert not error_dir.exists()

    def test_a_crash_is_one_line_and_the_traceback_is_kept(self, error_dir,
                                                           capfd):
        try:
            raise OSError(errno.ENOSPC, "No space left on device", "/out/x")
        except OSError as exc:
            path = errorlog.record_crash(exc)
        err = capfd.readouterr().err
        assert err.startswith("==> ERROR: OSError: [Errno 28]")
        assert "Traceback" not in err
        assert "the traceback is kept in %s" % path in err
        text = only_file(error_dir)
        assert "ERROR  disk-full" in text
        assert "traceback (most recent call last):" in text
        assert "/out/x" in text or os.path.abspath("/out/x") in text

    def test_verbose_prints_the_traceback_too(self, error_dir, capfd,
                                              monkeypatch):
        monkeypatch.setenv("LOG_VERBOSITY", "verbose")
        try:
            raise ValueError("bad")
        except ValueError as exc:
            errorlog.record_crash(exc)
        assert "Traceback (most recent call last):" in capfd.readouterr().err

    def test_a_worker_that_raises_is_kept_and_ends_with_status_1(
            self, error_dir, capfd):
        def body():
            raise RuntimeError("inside")

        with pytest.raises(SystemExit) as stopped:
            errorlog.run_in_worker(body, (), "film.mkv")
        assert stopped.value.code == 1
        text = only_file(error_dir)
        assert "item:     film.mkv" in text and "RuntimeError: inside" in text


@pytest.mark.fs
class TestCommands:
    def test_a_missing_input_folder_is_kept(self, tmp_path):
        script_dir = tmp_path / "scriptDir"
        env = dict(os.environ, CLI_SCRIPT_DIR=str(script_dir))
        done = blackbox.run("find-gaps", str(tmp_path / "nowhere"),
                            cwd=tmp_path, env=env)
        assert done.returncode == 1
        text = only_file(script_dir / "logs" / errorlog.ERROR_DIR)
        assert "ERROR  input-missing" in text
        assert "find-gaps" in text and "nowhere" in text

    def test_an_unknown_option_is_kept(self, tmp_path):
        script_dir = tmp_path / "scriptDir"
        env = dict(os.environ, CLI_SCRIPT_DIR=str(script_dir))
        done = blackbox.run("find-gaps", "--no-such-option", "x",
                            cwd=tmp_path, env=env)
        assert done.returncode == 1
        assert "ERROR  usage" in only_file(
            script_dir / "logs" / errorlog.ERROR_DIR)

    def test_help_keeps_nothing(self, tmp_path):
        script_dir = tmp_path / "scriptDir"
        env = dict(os.environ, CLI_SCRIPT_DIR=str(script_dir))
        assert blackbox.run("find-gaps", "-h", cwd=tmp_path,
                            env=env).returncode == 0
        assert not (script_dir / "logs" / errorlog.ERROR_DIR).exists()
