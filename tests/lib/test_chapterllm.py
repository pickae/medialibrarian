"""The chapter model's settings and its OpenAI-protocol request, with curl
replaced by a function that records what it was handed."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor

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

    def test_and_is_refused_without_an_address(self):
        with pytest.raises(chapterllm.LlmError, match="chapterLlmUrl"):
            chapterllm.from_environment({"chapterLlmModel": "m"})

    def test_a_local_server_may_leave_the_model_to_what_it_lists(self):
        llm = chapterllm.from_environment({"chapterLlmUrl": _LOCAL["chapterLlmUrl"]})
        assert llm.model == ""

    def test_a_hosted_api_is_refused_without_a_model(self):
        with pytest.raises(chapterllm.LlmError, match="chapterLlmModel"):
            chapterllm.from_environment({"chapterLlmUrl": "https://8.8.8.8/v1"})

    def test_several_addresses_are_a_ladder_the_first_preferred(self):
        llm = chapterllm.from_environment(dict(
            _LOCAL, chapterLlmUrl=" http://localhost:8081/v1/ , http://localhost:8080/v1",
            chapterLlmServe="serve-it --port 8081", chapterLlmServeMib="20000"))
        assert (llm.url, llm.fallbacks) == ("http://localhost:8081/v1",
                                           ("http://localhost:8080/v1",))
        assert (llm.serve, llm.serve_mib) == ("serve-it --port 8081", 20000)
        assert "started when needed" in llm.describe()
        assert "else http://localhost:8080/v1" in llm.describe()

    def test_claude_takes_one_address_and_no_command(self):
        for extra in ({"chapterLlmUrl": "https://a/v1,https://b/v1"},
                      {"chapterLlmServe": "serve-it"}):
            with pytest.raises(chapterllm.LlmError):
                chapterllm.from_environment(dict(extra, chapterLlmApi="anthropic"))

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


class TestLocalOrHosted:
    @pytest.mark.parametrize("url", [
        "http://localhost:11434/v1", "http://127.0.0.1:8080/v1", "http://[::1]:8080/v1",
        "http://192.168.1.20:11434/v1", "http://10.0.0.5/v1", "http://100.101.5.7/v1",
        "http://gpubox:8080/v1", "http://gpubox.local/v1", "http://nas.lan/v1",
        "http://box.tail1234.ts.net/v1", "http://model.test/v1"])
    def test_a_server_on_this_machine_or_network_is_local(self, url):
        assert chapterllm.is_local(url)

    def test_no_address_is_the_hosted_api(self):
        assert not chapterllm.is_local("")

    def test_a_public_address_is_hosted(self):
        assert not chapterllm.is_local("https://8.8.8.8/v1")

    def test_a_name_is_hosted_when_it_resolves_to_public_addresses(self, monkeypatch):
        monkeypatch.setattr(chapterllm.socket, "getaddrinfo", lambda host, port: [
            (2, 1, 6, "", ("104.18.6.192", 0)), (10, 1, 6, "", ("2606:4700::1", 0, 0, 0))])
        assert not chapterllm.is_local("https://api.openai.com/v1")

    def test_and_local_when_it_resolves_to_this_network(self, monkeypatch):
        monkeypatch.setattr(chapterllm.socket, "getaddrinfo", lambda host, port: [
            (2, 1, 6, "", ("192.168.1.20", 0))])
        assert chapterllm.is_local("http://llm.example.com/v1")

    def test_and_local_when_it_does_not_resolve(self, monkeypatch):
        def fail(host, port):
            raise OSError("no such host")

        monkeypatch.setattr(chapterllm.socket, "getaddrinfo", fail)
        assert chapterllm.is_local("http://llm.example.com/v1")

    def test_a_local_model_is_held_to_two_requests(self):
        llm = chapterllm.from_environment(_LOCAL)
        assert llm.local and llm.jobs == chapterllm.DEFAULT_JOBS

    def test_a_hosted_one_to_none(self):
        llm = chapterllm.from_environment({"chapterLlmApi": "anthropic"})
        assert not llm.local and llm.jobs == 0

    def test_unless_the_setting_says_so(self):
        llm = chapterllm.from_environment({"chapterLlmApi": "anthropic",
                                           "chapterLlmJobs": "4"})
        assert not llm.local and llm.jobs == 4

    @pytest.mark.parametrize("jobs, most", [(2, 2), (0, 6)])
    def test_requests_in_flight_never_pass_the_limit(self, monkeypatch, jobs, most):
        lock = threading.Lock()
        flying = [0, 0]
        release = threading.Barrier(most)

        def post(llm, system, prompt, tried):
            with lock:
                flying[0] += 1
                flying[1] = max(flying)
            release.wait(timeout=5)
            with lock:
                flying[0] -= 1
            return "x", None

        monkeypatch.setattr(chapterllm, "_post_openai", post)
        llm = chapterllm.Llm("openai", "http://x/v1", "m", jobs=jobs)
        with ThreadPoolExecutor(max_workers=6) as pool:
            assert list(pool.map(lambda _: llm.ask("s", "u"), range(6))) == ["x"] * 6
        assert flying[1] == most


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

    def test_the_tokens_written_are_counted_when_the_server_reports_them(self, curl):
        tally = chapterllm.Tally()
        llm = chapterllm.from_environment(_LOCAL)
        curl["stdout"] = json.dumps({"choices": [{"message": {"content": "x"}}],
                                     "usage": {"completion_tokens": 42}}) + "\n200"
        llm.ask("s", "u", tally)
        del curl["stdout"]
        llm.ask("s", "u", tally)
        assert (tally.requests, tally.counted, tally.tokens) == (2, 1, 42)
        assert tally.seconds >= tally.counted_seconds > 0

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

    def test_a_thorough_model_reasons_over_what_the_options_say(self, curl):
        llm = chapterllm.from_environment(dict(
            _LOCAL, chapterLlmOptions='{"reasoning_effort": "none", "top_k": 20}'))
        llm.following("Name the speakers.").ask("system", "prompt")
        assert curl["body"]["reasoning_effort"] == chapterllm.THOROUGH_EFFORT
        assert curl["body"]["top_k"] == 20
        llm.ask("system", "prompt")
        assert curl["body"]["reasoning_effort"] == "none"

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

        def post(llm, system, prompt, tried):
            seen["options"] = llm.options
            return "x", None

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



class TestFollowing:
    def test_blank_instructions_change_nothing(self):
        llm = chapterllm.from_environment(_LOCAL)
        assert llm.following("  \n ") is llm

    def test_the_copy_shares_the_runs_request_limit(self):
        llm = chapterllm.from_environment(_LOCAL)
        assert llm._slots is not None
        assert llm.following("Name them.")._slots is llm._slots

    def test_only_claude_may_search(self):
        local = chapterllm.from_environment(_LOCAL).following(" Name them. ")
        claude = chapterllm.from_environment({"chapterLlmApi": "anthropic"}).following("x")
        assert local.instructions == "Name them."
        assert (local.effort, local.search) == (chapterllm.THOROUGH_EFFORT, False)
        assert (claude.effort, claude.search) == (chapterllm.THOROUGH_EFFORT, True)
        assert "medium effort" in local.describe()
        assert "web search" in claude.describe()
        assert "effort" not in chapterllm.from_environment(_LOCAL).describe()


class _Block:
    def __init__(self, type, text=""):
        self.type, self.text = type, text


class _Message:
    def __init__(self, stop_reason, *blocks, output_tokens=None):
        self.stop_reason, self.content = stop_reason, list(blocks)
        if output_tokens is not None:
            self.usage = types.SimpleNamespace(output_tokens=output_tokens)


class TestClaude:
    @pytest.fixture
    def claude(self, monkeypatch):
        """A stand-in for the anthropic package: each request is recorded and
        answered with the next of ``replies``."""
        seen: dict = {"requests": [], "replies": []}

        class Stream:
            def __init__(self, **request):
                seen["requests"].append(request)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get_final_message(self):
                return seen["replies"].pop(0)

        class Messages:
            def stream(self, **request):
                return Stream(**request)

        class Client:
            def __init__(self, **options):
                self.beta = types.SimpleNamespace(messages=Messages())

        module = types.ModuleType("anthropic")
        module.Anthropic = Client
        module.APIStatusError = type("APIStatusError", (Exception,), {})
        module.APIConnectionError = type("APIConnectionError", (Exception,), {})
        monkeypatch.setitem(sys.modules, "anthropic", module)
        return seen

    def test_plain_requests_neither_search_nor_raise_the_effort(self, claude):
        claude["replies"].append(_Message("end_turn", _Block("text", "{}")))
        chapterllm.from_environment({"chapterLlmApi": "anthropic"}).ask("s", "p")
        request = claude["requests"][0]
        assert "tools" not in request
        assert request["output_config"] == {"effort": "medium"}

    def test_a_thorough_request_searches_and_a_paused_turn_is_resumed(self, claude):
        searched = _Block("server_tool_use")
        claude["replies"] += [_Message("pause_turn", _Block("text", "Looking. "), searched),
                              _Message("end_turn", _Block("text", '{"a": 1}'))]
        llm = chapterllm.from_environment({"chapterLlmApi": "anthropic"}).following("x")
        assert llm.ask("s", "p") == '{"a": 1}'
        first, second = claude["requests"]
        assert first["tools"][0]["type"] == "web_search_20260209"
        assert second["messages"][0] == {"role": "user", "content": "p"}
        assert second["messages"][1]["role"] == "assistant"
        assert second["messages"][1]["content"][1] is searched

    def test_the_tokens_of_a_resumed_turn_add_up(self, claude):
        claude["replies"] += [_Message("pause_turn", _Block("text", "."), output_tokens=30),
                              _Message("end_turn", _Block("text", "{}"), output_tokens=12)]
        tally = chapterllm.Tally()
        chapterllm.from_environment({"chapterLlmApi": "anthropic"}).ask("s", "p", tally)
        assert (tally.requests, tally.tokens) == (1, 42)
