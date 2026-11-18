"""The chapter model's settings and its OpenAI-protocol request, with curl
replaced by a function that records what it was handed."""

from __future__ import annotations

import json
import subprocess

import pytest

from medialib.lib import chapterllm

pytestmark = pytest.mark.pure

_LOCAL = {"chapterLlmUrl": "http://localhost:11434/v1/", "chapterLlmModel": "m"}


class TestSettings:
    def test_a_local_server_needs_its_address_and_model(self):
        llm = chapterllm.from_environment(_LOCAL)
        assert (llm.api, llm.url, llm.model) == ("openai", "http://localhost:11434/v1", "m")
        assert llm.context_tokens == chapterllm.DEFAULT_CONTEXT_TOKENS
        assert llm.jobs == chapterllm.DEFAULT_JOBS

    @pytest.mark.parametrize("missing", ["chapterLlmUrl", "chapterLlmModel"])
    def test_and_is_refused_without_either(self, missing):
        environ = dict(_LOCAL)
        del environ[missing]
        with pytest.raises(chapterllm.LlmError, match=missing):
            chapterllm.from_environment(environ)

    def test_claude_needs_neither(self):
        llm = chapterllm.from_environment({"chapterLlmApi": "Anthropic"})
        assert (llm.api, llm.url, llm.model) == ("anthropic", "",
                                                 chapterllm.DEFAULT_ANTHROPIC_MODEL)

    def test_an_unknown_protocol_is_refused(self):
        with pytest.raises(chapterllm.LlmError, match="chapterLlmApi"):
            chapterllm.from_environment({"chapterLlmApi": "gemini"})

    @pytest.mark.parametrize("value", ["0", "lots", "-3"])
    def test_a_window_that_is_not_a_count_is_refused(self, value):
        with pytest.raises(chapterllm.LlmError, match="chapterLlmContext"):
            chapterllm.from_environment(dict(_LOCAL, chapterLlmContext=value))


class TestFirstJson:
    @pytest.mark.parametrize("text,value", [
        ('{"a": 1}', {"a": 1}),
        ('Here you go:\n```json\n{"a": [1, 2]}\n```\nDone.', {"a": [1, 2]}),
        ('Not {this} but [1, 2]', [1, 2]),
        ("nothing at all", None),
    ])
    def test_found_wherever_it_is(self, text, value):
        assert chapterllm.first_json(text) == value


class TestTheRequest:
    @pytest.fixture
    def curl(self, monkeypatch):
        seen: dict = {}

        def answer(argv, input, capture_output):
            seen["argv"] = argv
            seen["config"] = input.decode("utf-8")
            body_file = [line for line in seen["config"].splitlines()
                         if line.startswith("data-binary")][0].split('"@', 1)[1][:-1]
            with open(body_file, encoding="utf-8") as handle:
                seen["body"] = json.load(handle)
            stdout = seen.get("stdout", json.dumps({"choices": [{"message": {
                "content": "<think>hmm</think>\n{\"ok\": true}"}}]}) + "\n200")
            return subprocess.CompletedProcess(argv, seen.get("rc", 0),
                                               stdout.encode(), b"refused")

        monkeypatch.setattr(chapterllm.subprocess, "run", answer)
        monkeypatch.setattr(chapterllm, "RETRY_WAIT_SECONDS", 0)
        return seen

    def test_the_messages_go_to_the_chat_endpoint(self, curl):
        llm = chapterllm.from_environment(dict(_LOCAL, chapterLlmKey="sk-secret"))
        assert llm.ask("sys", "user") == '{"ok": true}'
        assert 'url = "http://localhost:11434/v1/chat/completions"' in curl["config"]
        assert curl["body"] == {"model": "m", "max_tokens": 32000 - 2, "messages": [
            {"role": "system", "content": "sys"}, {"role": "user", "content": "user"}]}

    def test_the_key_is_on_stdin_and_never_on_the_argv(self, curl):
        chapterllm.from_environment(dict(_LOCAL, chapterLlmKey="sk-secret")).ask("s", "u")
        assert curl["argv"] == ["curl", "--config", "-"]
        assert 'header = "Authorization: Bearer sk-secret"' in curl["config"]

    def test_no_key_no_header(self, curl):
        chapterllm.from_environment(_LOCAL).ask("s", "u")
        assert "Authorization" not in curl["config"]

    def test_an_http_error_names_the_servers_message(self, curl):
        curl["stdout"] = json.dumps({"error": {"message": "context too long"}}) + "\n400"
        with pytest.raises(chapterllm.LlmError, match="HTTP 400: context too long"):
            chapterllm.from_environment(_LOCAL).ask("s", "u")

    def test_the_limit_is_what_the_window_leaves_up_to_a_ceiling(self, curl):
        llm = chapterllm.from_environment(dict(_LOCAL, chapterLlmContext="1000000"))
        llm.ask("s", "x" * 30000)
        assert curl["body"]["max_tokens"] == chapterllm.MAX_OUTPUT_TOKENS
        llm = chapterllm.from_environment(dict(_LOCAL, chapterLlmContext="20000"))
        llm.ask("s", "x" * 60000)
        assert curl["body"]["max_tokens"] == chapterllm.MIN_OUTPUT_TOKENS

    def test_a_refused_limit_name_moves_on_to_the_next_and_stays_there(
            self, curl, monkeypatch):
        bodies = []
        refusals = iter([json.dumps({"error": {"message":
                         "Unsupported parameter: 'max_tokens'"}}) + "\n400",
                         json.dumps({"error": {"message":
                         "Unrecognized request argument: max_completion_tokens"}})
                         + "\n400"])
        original = chapterllm.subprocess.run

        def answer(argv, input, capture_output):
            curl.pop("stdout", None)
            refusal = next(refusals, None)
            if refusal:
                curl["stdout"] = refusal
            done = original(argv, input=input, capture_output=capture_output)
            bodies.append(dict(curl["body"]))
            return done

        monkeypatch.setattr(chapterllm.subprocess, "run", answer)
        llm = chapterllm.from_environment(_LOCAL)
        assert llm.ask("s", "u") == '{"ok": true}'
        assert ["max_tokens" in b for b in bodies] == [True, False, False]
        assert ["max_completion_tokens" in b for b in bodies] == [False, True, False]
        llm.ask("s", "u")
        assert set(bodies[-1]) == {"model", "messages"}

    def test_an_option_is_merged_and_a_limit_given_there_wins(self, curl):
        chapterllm.from_environment(dict(
            _LOCAL, chapterLlmOptions='{"reasoning_effort": "low", "max_tokens": 900}')
        ).ask("s", "u")
        assert curl["body"]["reasoning_effort"] == "low"
        assert curl["body"]["max_tokens"] == 900

    def test_a_curl_failure_is_retried_then_reported(self, curl, monkeypatch):
        calls = []
        original = chapterllm._ask_openai

        def counted(*args):
            calls.append(1)
            return original(*args)

        monkeypatch.setattr(chapterllm, "_ask_openai", counted)
        curl["rc"] = 7
        with pytest.raises(chapterllm.LlmError, match="refused"):
            chapterllm.from_environment(_LOCAL).ask("s", "u")
        assert len(calls) == chapterllm.RETRIES + 1


class TestOptions:
    def test_they_go_into_every_request(self, monkeypatch):
        seen = {}

        def post(llm, system, prompt):
            seen["options"] = llm.options
            return "x"

        monkeypatch.setattr(chapterllm, "_post_openai", post)
        llm = chapterllm.from_environment(dict(
            _LOCAL, chapterLlmOptions='{"reasoning_effort": "none"}'))
        llm.ask("s", "u")
        assert seen["options"] == {"reasoning_effort": "none"}

    @pytest.mark.parametrize("value,error", [
        ("{nope", "not JSON"), ("[1]", "JSON object")])
    def test_and_must_be_a_json_object(self, value, error):
        with pytest.raises(chapterllm.LlmError, match=error):
            chapterllm.from_environment(dict(_LOCAL, chapterLlmOptions=value))

    def test_and_are_for_the_openai_protocol_only(self):
        with pytest.raises(chapterllm.LlmError, match="openai protocol only"):
            chapterllm.from_environment({"chapterLlmApi": "anthropic",
                                         "chapterLlmOptions": '{"a": 1}'})

