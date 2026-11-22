# ruff: noqa: E501
"""Tests for asking a model through an agent harness - medialib.lib.llmharness.

A stub ``opencode`` stands in for the real one: ``run`` records its argument
list and the task file it was given and answers in opencode's JSON events;
``session delete`` records the id it is asked to delete. What is pinned: the
request reaches the harness whole, the last message's text is the answer, the
session is deleted whether the answer came, failed or timed out, and a model
must be named before a harness is used.
"""

import json
import os

import pytest

from medialib.lib import llmclient, llmharness

pytestmark = pytest.mark.stubbed

_STUB = r"""#!/bin/sh
log="$STUB_LOG"
if [ "$1" = session ] && [ "$2" = export ]; then
  echo "export $3" >> "$log"
  [ -n "$STUB_NO_EXPORT" ] && exit 1
  echo "{\"info\": {\"id\": \"$3\"}, \"key\": \"$ANTHROPIC_API_KEY\"}"
  exit 0
fi
if [ "$1" = session ]; then
  echo "delete $3" >> "$log"
  exit "${STUB_DELETE_STATUS:-0}"
fi
echo "run $*" >> "$log"

while [ $# -gt 0 ]; do
  [ "$1" = --file ] && cp "$2" "$STUB_LOG.task"
  shift
done
case "$STUB_MODE" in
  fail) echo '{"type":"step_start","sessionID":"ses_1"}'; echo boom >&2; exit 3;;
  hang) echo '{"type":"step_start","sessionID":"ses_1"}'; sleep 30;;
  empty) echo '{"type":"step_start","sessionID":"ses_1"}'; exit 0;;
  refused) echo '{"type":"step_start","sessionID":"ses_1"}'
    echo '{"type":"error","sessionID":"ses_1","error":{"type":"provider.transport","message":"ECONNREFUSED: Unable to connect."}}'
    exit 1;;
  full) echo '{"type":"step_start","sessionID":"ses_1"}'
    echo '{"type":"error","sessionID":"ses_1","error":{"type":"provider.rate-limit","message":"inference request queue is full","status":429}}'
    exit 1;;
  broken) echo '{"type":"step_start","sessionID":"ses_1"}'
    echo '{"type":"error","sessionID":"ses_1","error":{"type":"tool","message":"the tool broke"}}'
    exit 1;;
esac
echo '{"type":"text","sessionID":"ses_1","part":{"messageID":"m1","type":"text","text":"let me search"}}'
echo '{"type":"tool_use","sessionID":"ses_1","part":{"messageID":"m1","type":"tool"}}'
echo '{"type":"text","sessionID":"ses_1","part":{"messageID":"m2","type":"text","text":"{\"chapters\": "}}'
echo '{"type":"text","sessionID":"ses_1","part":{"messageID":"m2","type":"text","text":"[]}"}}'
"""


@pytest.fixture()
def opencode(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "opencode"
    stub.write_text(_STUB)
    os.chmod(str(stub), 0o755)
    log = tmp_path / "log"
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("STUB_LOG", str(log))
    monkeypatch.delenv("STUB_MODE", raising=False)
    monkeypatch.delenv("STUB_DELETE_STATUS", raising=False)
    monkeypatch.delenv("STUB_NO_EXPORT", raising=False)
    monkeypatch.setenv("TMPDIR", str(tmp_path))

    class Stub:
        def lines(self):
            return log.read_text().splitlines() if log.exists() else []

        def task(self):
            return open(str(log) + ".task", encoding="utf-8").read()
    return Stub()


def _ask(timeout=20):
    return llmharness.ask(llmharness.Harness.OPENCODE, "ninfer/qwen", "SYSTEM",
                          "PROMPT", timeout)


class TestAsking:
    def test_the_last_message_is_the_answer_and_the_session_is_deleted(self, opencode):
        assert _ask() == '{"chapters": []}'
        run, export, delete = opencode.lines()
        assert "--model ninfer/qwen" in run and "--archived" in run
        assert (export, delete) == ("export ses_1", "delete ses_1")

    def test_the_task_file_is_given_by_absolute_path(self, opencode):
        # opencode's background service resolves a relative one against its own
        # directory, and finds nothing.
        _ask()
        argv = opencode.lines()[0].split()
        assert argv[argv.index("--file") + 1].startswith("/")

    def test_the_whole_request_is_in_the_task_file(self, opencode):
        _ask()
        assert "SYSTEM" in opencode.task() and "PROMPT" in opencode.task()

    def test_no_directory_is_left_behind(self, opencode, tmp_path):
        _ask()
        assert not [entry for entry in os.listdir(tmp_path)
                    if entry.startswith("llmharness.")]

    def test_a_failure_still_deletes_the_session(self, opencode, monkeypatch):
        monkeypatch.setenv("STUB_MODE", "fail")
        with pytest.raises(llmharness.HarnessError, match="boom"):
            _ask()
        assert opencode.lines()[-1] == "delete ses_1"

    def test_a_timeout_still_deletes_the_session(self, opencode, monkeypatch):
        monkeypatch.setenv("STUB_MODE", "hang")
        with pytest.raises(llmharness.HarnessError, match="did not answer"):
            _ask(timeout=1)
        assert opencode.lines()[-1] == "delete ses_1"

    @pytest.mark.parametrize("mode,said", [("refused", "ECONNREFUSED"),
                                           ("full", "queue is full")])
    def test_a_server_that_refused_the_session_is_named_unavailable(
            self, opencode, monkeypatch, mode, said):
        monkeypatch.setenv("STUB_MODE", mode)
        with pytest.raises(llmharness.HarnessUnavailable, match=said):
            _ask()
        assert opencode.lines()[-1] == "delete ses_1"

    def test_any_other_error_the_session_names_is_said(self, opencode, monkeypatch):
        monkeypatch.setenv("STUB_MODE", "broken")
        with pytest.raises(llmharness.HarnessError, match="the tool broke") as raised:
            _ask()
        assert not isinstance(raised.value, llmharness.HarnessUnavailable)

    def test_no_text_is_no_answer(self, opencode, monkeypatch):
        monkeypatch.setenv("STUB_MODE", "empty")
        with pytest.raises(llmharness.HarnessError, match="no text"):
            _ask()

    def test_a_delete_that_fails_is_said_not_raised(self, opencode, monkeypatch,
                                                    capsys):
        monkeypatch.setenv("STUB_DELETE_STATUS", "1")
        assert _ask()
        assert "Could not delete" in capsys.readouterr().err


class TestTheSetting:
    def test_names_are_parsed_in_order(self):
        assert llmharness.parse(" OpenCode, ,opencode") == (llmharness.Harness.OPENCODE,)
        assert llmharness.parse("") == ()

    def test_an_unknown_name_is_refused(self):
        with pytest.raises(llmharness.HarnessError, match="takes opencode"):
            llmharness.parse("emacs")

    def test_a_model_is_required(self):
        with pytest.raises(llmclient.LlmError, match="llmHarnessModel"):
            llmclient.from_environment({
                "llmUrl": "http://localhost:8080/v1",
                "llmHarness": "opencode"})


class TestThroughTheModel:
    def _llm(self):
        return llmclient.from_environment({
            "llmUrl": "http://localhost:8080/v1",
            "llmHarness": "opencode",
            "llmHarnessModel": "ninfer/qwen"})

    def test_instructions_go_through_the_harness(self, opencode):
        llm = self._llm()
        following = llm.following("check the names online")
        assert following.harness is llmharness.Harness.OPENCODE
        assert following.ask("s", "p") == '{"chapters": []}'
        assert "through opencode" in following.describe()

    def test_without_instructions_the_server_is_asked_alone(self, opencode):
        assert self._llm().following("  ").harness is None

    def test_claude_searches_by_itself(self, opencode):
        llm = llmclient.from_environment({
            "llmApi": "anthropic", "llmHarness": "opencode",
            "llmHarnessModel": "x"})
        assert llm.following("check").harness is None

    def test_json_in_the_answer_is_readable(self, opencode):
        assert llmclient.first_json(_ask()) == {"chapters": []}
        assert json.loads(_ask()) == {"chapters": []}


class TestTheLog:
    def test_the_session_is_kept_in_the_runs_folder_before_it_is_deleted(
            self, opencode):
        _ask()
        assert opencode.lines()[-2:] == ["export ses_1", "delete ses_1"]
        folder = llmharness.session_dir()
        assert os.path.dirname(folder).endswith(os.path.join("logs", "llm"))
        (name,) = os.listdir(folder)
        assert name.endswith("-opencode-ses_1.json")
        with open(os.path.join(folder, name), encoding="utf-8") as handle:
            assert json.load(handle)["info"]["id"] == "ses_1"

    def test_the_folder_is_named_like_the_runs_other_logs(self, opencode):
        from medialib.lib import errorlog
        stem = os.path.splitext(os.path.basename(errorlog.current_file()))[0]
        assert os.path.basename(llmharness.session_dir()) == stem

    def test_secrets_are_starred_out(self, opencode, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-0123456789abcdef0123")
        _ask()
        folder = llmharness.session_dir()
        (name,) = os.listdir(folder)
        with open(os.path.join(folder, name), encoding="utf-8") as handle:
            assert "sk-ant-0123456789abcdef0123" not in handle.read()

    def test_an_export_that_fails_still_deletes_the_session(self, opencode,
                                                            monkeypatch):
        monkeypatch.setenv("STUB_NO_EXPORT", "1")
        assert _ask()
        assert opencode.lines()[-1] == "delete ses_1"
        assert not os.path.isdir(llmharness.session_dir())


class TestSearching:
    """A model asked to look something up: Claude searches itself, a server
    behind the openai protocol only through a harness."""

    def test_claude_searches(self):
        llm = llmclient.from_environment({"llmApi": "anthropic"})
        found = llm.searching("cover")
        assert found is not None and found.search and found.purpose == "cover"
        assert found.effort == llmclient.THOROUGH_EFFORT

    def test_a_server_without_a_harness_cannot(self):
        llm = llmclient.from_environment({"llmUrl": "http://localhost:8080/v1"})
        assert llm.searching("cover") is None

    def test_a_server_with_a_harness_searches_through_it(self, opencode):
        llm = llmclient.from_environment({
            "llmUrl": "http://localhost:8080/v1",
            "llmHarness": "opencode", "llmHarnessModel": "ninfer/qwen"})
        found = llm.searching("cover")
        assert found is not None and found.harness is llmharness.Harness.OPENCODE
        found.ask("SYSTEM", "PROMPT")
        assert "--title medialib cover request" in opencode.lines()[0]
