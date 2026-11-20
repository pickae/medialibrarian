"""The chapter model's settings and its OpenAI-protocol request, with curl
replaced by a function that records what it was handed."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor

import pytest

from medialib.lib import llmclient

pytestmark = pytest.mark.pure

_LOCAL = {"llmUrl": "http://localhost:11434/v1/", "llmModel": "m"}


class TestSettings:
    def test_a_local_server_needs_its_address_and_model(self):
        llm = llmclient.from_environment(_LOCAL)
        assert (llm.api, llm.url, llm.model) == ("openai", "http://localhost:11434/v1", "m")
        assert llm.context_tokens == llmclient.DEFAULT_CONTEXT_TOKENS
        assert llm.jobs == llmclient.DEFAULT_JOBS

    def test_and_is_refused_without_an_address(self):
        with pytest.raises(llmclient.LlmError, match="llmUrl"):
            llmclient.from_environment({"llmModel": "m"})

    def test_a_local_server_may_leave_the_model_to_what_it_lists(self):
        llm = llmclient.from_environment({"llmUrl": _LOCAL["llmUrl"]})
        assert llm.model == ""

    def test_a_hosted_api_is_refused_without_a_model(self):
        with pytest.raises(llmclient.LlmError, match="llmModel"):
            llmclient.from_environment({"llmUrl": "https://8.8.8.8/v1"})

    def test_several_addresses_are_a_ladder_the_first_preferred(self):
        llm = llmclient.from_environment(dict(
            _LOCAL, llmUrl=" http://localhost:8081/v1/ , http://localhost:8080/v1",
            llmServe="serve-it --port 8081", llmServeMib="20000"))
        assert (llm.url, llm.fallbacks) == ("http://localhost:8081/v1",
                                           ("http://localhost:8080/v1",))
        assert (llm.serve, llm.serve_mib) == ("serve-it --port 8081", 20000)
        assert "started when needed" in llm.describe()
        assert "else http://localhost:8080/v1" in llm.describe()

    def test_claude_takes_one_address_and_no_command(self):
        for extra in ({"llmUrl": "https://a/v1,https://b/v1"},
                      {"llmServe": "serve-it"}):
            with pytest.raises(llmclient.LlmError):
                llmclient.from_environment(dict(extra, llmApi="anthropic"))

    def test_claude_needs_neither(self):
        llm = llmclient.from_environment({"llmApi": "Anthropic"})
        assert (llm.api, llm.url, llm.model) == ("anthropic", "",
                                                 llmclient.DEFAULT_ANTHROPIC_MODEL)

    def test_an_unknown_protocol_is_refused(self):
        with pytest.raises(llmclient.LlmError, match="llmApi"):
            llmclient.from_environment({"llmApi": "gemini"})

    @pytest.mark.parametrize("value", ["0", "lots", "-3"])
    def test_a_window_that_is_not_a_count_is_refused(self, value):
        with pytest.raises(llmclient.LlmError, match="llmContext"):
            llmclient.from_environment(dict(_LOCAL, llmContext=value))


class TestLocalOrHosted:
    @pytest.mark.parametrize("url", [
        "http://localhost:11434/v1", "http://127.0.0.1:8080/v1", "http://[::1]:8080/v1",
        "http://192.168.1.20:11434/v1", "http://10.0.0.5/v1", "http://100.101.5.7/v1",
        "http://gpubox:8080/v1", "http://gpubox.local/v1", "http://nas.lan/v1",
        "http://box.tail1234.ts.net/v1", "http://model.test/v1"])
    def test_a_server_on_this_machine_or_network_is_local(self, url):
        assert llmclient.is_local(url)

    def test_no_address_is_the_hosted_api(self):
        assert not llmclient.is_local("")

    def test_a_public_address_is_hosted(self):
        assert not llmclient.is_local("https://8.8.8.8/v1")

    def test_a_name_is_hosted_when_it_resolves_to_public_addresses(self, monkeypatch):
        monkeypatch.setattr(llmclient.socket, "getaddrinfo", lambda host, port: [
            (2, 1, 6, "", ("104.18.6.192", 0)), (10, 1, 6, "", ("2606:4700::1", 0, 0, 0))])
        assert not llmclient.is_local("https://api.openai.com/v1")

    def test_and_local_when_it_resolves_to_this_network(self, monkeypatch):
        monkeypatch.setattr(llmclient.socket, "getaddrinfo", lambda host, port: [
            (2, 1, 6, "", ("192.168.1.20", 0))])
        assert llmclient.is_local("http://llm.example.com/v1")

    def test_and_local_when_it_does_not_resolve(self, monkeypatch):
        def fail(host, port):
            raise OSError("no such host")

        monkeypatch.setattr(llmclient.socket, "getaddrinfo", fail)
        assert llmclient.is_local("http://llm.example.com/v1")

    def test_a_local_model_is_held_to_two_requests(self):
        llm = llmclient.from_environment(_LOCAL)
        assert llm.local and llm.jobs == llmclient.DEFAULT_JOBS

    def test_a_hosted_one_to_none(self):
        llm = llmclient.from_environment({"llmApi": "anthropic"})
        assert not llm.local and llm.jobs == 0

    def test_unless_the_setting_says_so(self):
        llm = llmclient.from_environment({"llmApi": "anthropic",
                                           "llmJobs": "4"})
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

        monkeypatch.setattr(llmclient, "_post_openai", post)
        llm = llmclient.Llm("openai", "http://x/v1", "m", jobs=jobs)
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
        assert llmclient.first_json(text) == value


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

        monkeypatch.setattr(llmclient.subprocess, "run", answer)
        monkeypatch.setattr(llmclient, "RETRY_WAIT_SECONDS", 0)
        return seen

    def test_the_messages_go_to_the_chat_endpoint(self, curl):
        llm = llmclient.from_environment(dict(_LOCAL, llmKey="sk-secret"))
        assert llm.ask("sys", "user") == '{"ok": true}'
        assert 'url = "http://localhost:11434/v1/chat/completions"' in curl["config"]
        assert curl["body"] == {"model": "m", "max_tokens": 32000 - 2, "messages": [
            {"role": "system", "content": "sys"}, {"role": "user", "content": "user"}]}

    def test_the_tokens_written_are_counted_when_the_server_reports_them(self, curl):
        tally = llmclient.Tally()
        llm = llmclient.from_environment(_LOCAL)
        curl["stdout"] = json.dumps({"choices": [{"message": {"content": "x"}}],
                                     "usage": {"completion_tokens": 42}}) + "\n200"
        llm.ask("s", "u", tally)
        del curl["stdout"]
        llm.ask("s", "u", tally)
        assert (tally.requests, tally.counted, tally.tokens) == (2, 1, 42)
        assert tally.seconds >= tally.counted_seconds >= 0

    def test_the_key_is_on_stdin_and_never_on_the_argv(self, curl):
        llmclient.from_environment(dict(_LOCAL, llmKey="sk-secret")).ask("s", "u")
        assert curl["argv"] == ["curl", "--config", "-"]
        assert 'header = "Authorization: Bearer sk-secret"' in curl["config"]

    def test_no_key_no_header(self, curl):
        llmclient.from_environment(_LOCAL).ask("s", "u")
        assert "Authorization" not in curl["config"]

    def test_an_http_error_names_the_servers_message(self, curl):
        curl["stdout"] = json.dumps({"error": {"message": "context too long"}}) + "\n400"
        with pytest.raises(llmclient.LlmError, match="HTTP 400: context too long"):
            llmclient.from_environment(_LOCAL).ask("s", "u")

    def test_the_limit_is_what_the_window_leaves_up_to_a_ceiling(self, curl):
        llm = llmclient.from_environment(dict(_LOCAL, llmContext="1000000"))
        llm.ask("s", "x" * 30000)
        assert curl["body"]["max_tokens"] == llmclient.MAX_OUTPUT_TOKENS
        llm = llmclient.from_environment(dict(_LOCAL, llmContext="20000"))
        llm.ask("s", "x" * 60000)
        assert curl["body"]["max_tokens"] == llmclient.MIN_OUTPUT_TOKENS

    def test_a_refused_limit_name_moves_on_to_the_next_and_stays_there(
            self, curl, monkeypatch):
        bodies = []
        refusals = iter([json.dumps({"error": {"message":
                         "Unsupported parameter: 'max_tokens'"}}) + "\n400",
                         json.dumps({"error": {"message":
                         "Unrecognized request argument: max_completion_tokens"}})
                         + "\n400"])
        original = llmclient.subprocess.run

        def answer(argv, input, capture_output):
            curl.pop("stdout", None)
            refusal = next(refusals, None)
            if refusal:
                curl["stdout"] = refusal
            done = original(argv, input=input, capture_output=capture_output)
            bodies.append(dict(curl["body"]))
            return done

        monkeypatch.setattr(llmclient.subprocess, "run", answer)
        llm = llmclient.from_environment(_LOCAL)
        assert llm.ask("s", "u") == '{"ok": true}'
        assert ["max_tokens" in b for b in bodies] == [True, False, False]
        assert ["max_completion_tokens" in b for b in bodies] == [False, True, False]
        llm.ask("s", "u")
        assert set(bodies[-1]) == {"model", "messages"}

    def test_an_option_is_merged_and_a_limit_given_there_wins(self, curl):
        llmclient.from_environment(dict(
            _LOCAL, llmOptions='{"reasoning_effort": "low", "max_tokens": 900}')
        ).ask("s", "u")
        assert curl["body"]["reasoning_effort"] == "low"
        assert curl["body"]["max_tokens"] == 900

    def test_a_thorough_model_reasons_over_what_the_options_say(self, curl):
        llm = llmclient.from_environment(dict(
            _LOCAL, llmOptions='{"reasoning_effort": "none", "top_k": 20}'))
        llm.following("Name the speakers.").ask("system", "prompt")
        assert curl["body"]["reasoning_effort"] == llmclient.THOROUGH_EFFORT
        assert curl["body"]["top_k"] == 20
        llm.ask("system", "prompt")
        assert curl["body"]["reasoning_effort"] == "none"

    def test_a_curl_failure_is_retried_then_reported(self, curl, monkeypatch):
        calls = []
        original = llmclient._ask_openai

        def counted(*args):
            calls.append(1)
            return original(*args)

        monkeypatch.setattr(llmclient, "_ask_openai", counted)
        curl["rc"] = 7
        with pytest.raises(llmclient.LlmError, match="refused"):
            llmclient.from_environment(_LOCAL).ask("s", "u")
        assert len(calls) == llmclient.RETRIES + 1


class TestOptions:
    def test_they_go_into_every_request(self, monkeypatch):
        seen = {}

        def post(llm, system, prompt, tried):
            seen["options"] = llm.options
            return "x", None

        monkeypatch.setattr(llmclient, "_post_openai", post)
        llm = llmclient.from_environment(dict(
            _LOCAL, llmOptions='{"reasoning_effort": "none"}'))
        llm.ask("s", "u")
        assert seen["options"] == {"reasoning_effort": "none"}

    @pytest.mark.parametrize("value,error", [
        ("{nope", "not JSON"), ("[1]", "JSON object")])
    def test_and_must_be_a_json_object(self, value, error):
        with pytest.raises(llmclient.LlmError, match=error):
            llmclient.from_environment(dict(_LOCAL, llmOptions=value))

    def test_and_are_for_the_openai_protocol_only(self):
        with pytest.raises(llmclient.LlmError, match="openai protocol only"):
            llmclient.from_environment({"llmApi": "anthropic",
                                         "llmOptions": '{"a": 1}'})



class TestFollowing:
    def test_blank_instructions_change_nothing(self):
        llm = llmclient.from_environment(_LOCAL)
        assert llm.following("  \n ") is llm

    def test_the_copy_shares_the_runs_request_limit(self):
        llm = llmclient.from_environment(_LOCAL)
        assert llm._slots is not None
        assert llm.following("Name them.")._slots is llm._slots

    def test_only_claude_may_search(self):
        local = llmclient.from_environment(_LOCAL).following(" Name them. ")
        claude = llmclient.from_environment({"llmApi": "anthropic"}).following("x")
        assert local.instructions == "Name them."
        assert (local.effort, local.search) == (llmclient.THOROUGH_EFFORT, False)
        assert (claude.effort, claude.search) == (llmclient.THOROUGH_EFFORT, True)
        assert "medium effort" in local.describe()
        assert "web search" in claude.describe()
        assert "effort" not in llmclient.from_environment(_LOCAL).describe()


class _Block:
    def __init__(self, type, text=""):
        self.type, self.text = type, text


class _Message:
    def __init__(self, stop_reason, *blocks, output_tokens=None, input_tokens=None):
        self.stop_reason, self.content = stop_reason, list(blocks)
        if output_tokens is not None or input_tokens is not None:
            self.usage = types.SimpleNamespace(
                output_tokens=output_tokens, input_tokens=input_tokens)


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
        llmclient.from_environment({"llmApi": "anthropic"}).ask("s", "p")
        request = claude["requests"][0]
        assert "tools" not in request
        assert request["output_config"] == {"effort": "medium"}

    def test_a_thorough_request_searches_and_a_paused_turn_is_resumed(self, claude):
        searched = _Block("server_tool_use")
        claude["replies"] += [_Message("pause_turn", _Block("text", "Looking. "), searched),
                              _Message("end_turn", _Block("text", '{"a": 1}'))]
        llm = llmclient.from_environment({"llmApi": "anthropic"}).following("x")
        assert llm.ask("s", "p") == '{"a": 1}'
        first, second = claude["requests"]
        assert first["tools"][0]["type"] == "web_search_20260209"
        assert second["messages"][0] == {"role": "user", "content": "p"}
        assert second["messages"][1]["role"] == "assistant"
        assert second["messages"][1]["content"][1] is searched

    def test_the_tokens_of_a_resumed_turn_add_up(self, claude):
        claude["replies"] += [_Message("pause_turn", _Block("text", "."), output_tokens=30),
                              _Message("end_turn", _Block("text", "{}"), output_tokens=12)]
        tally = llmclient.Tally()
        llmclient.from_environment({"llmApi": "anthropic"}).ask("s", "p", tally)
        assert (tally.requests, tally.tokens) == (1, 42)


class TestSeeingPictures:
    """Which models are shown pictures, and how a picture is sent."""

    def _llm(self, **settings):
        return llmclient.from_environment(settings)

    def test_a_picture_is_sent_as_its_kind(self, tmp_path):
        png = tmp_path / "a.png"
        png.write_bytes(b"\x89PNG....")
        assert llmclient._picture(str(png)) == ("image/png", "iVBORy4uLi4=")
        avif = tmp_path / "a.avif"
        avif.write_bytes(b"\0\0\0\x1cftypavif")
        with pytest.raises(llmclient.LlmError, match="not a JPEG"):
            llmclient._picture(str(avif))

    def test_pictures_are_not_sent_through_a_harness(self):
        llm = self._llm(llmApi="anthropic")
        llm.harness = llmclient.llmharness.Harness.OPENCODE
        with pytest.raises(llmclient.LlmError, match="not sent through"):
            llm.ask("S", "P", images=["x.jpg"])
        with pytest.raises(llmclient.LlmError, match="not sent through"):
            llm.ask_pictures("S", "B", "A", ["x.jpg"])

    def test_numbered_pictures_openai_payload_and_options(self, monkeypatch, tmp_path):
        one = tmp_path / "one.png"
        two = tmp_path / "two.png"
        one.write_bytes(b"\x89PNGone")
        two.write_bytes(b"\x89PNGtwo")
        sent = {}

        def fake_run(cmd, input=b"", capture_output=False):
            config = input.decode("utf-8")
            body_line = next(line for line in config.splitlines()
                             if line.startswith("data-binary = "))
            body_path = json.loads(body_line.split(" = ", 1)[1])[1:]
            with open(body_path, encoding="utf-8") as handle:
                sent.update(json.loads(handle.read()))
            reply = json.dumps({
                "choices": [{"message": {"content": '["A", "B"]'}}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 11},
            }) + "\n200"
            return types.SimpleNamespace(returncode=0,
                                         stdout=reply.encode("utf-8"),
                                         stderr=b"")

        monkeypatch.setattr(llmclient.subprocess, "run", fake_run)
        llm = self._llm(llmUrl="http://localhost:8082/v1", llmModel="vision-12b",
                        llmOptions='{"top_k": 1}')
        tally = llmclient.Tally()
        answer = llm.ask(
            "", "Read two images.", tally, images=[str(one), str(two)],
            numbered=True, options={"temperature": 0}, max_tokens=400)
        assert answer == '["A", "B"]'
        assert len(sent["messages"]) == 1
        assert sent["messages"][0]["role"] == "user"
        content = sent["messages"][0]["content"]
        assert [p["text"] for p in content if p["type"] == "text"] == [
            "Read two images.", "Image 1:", "Image 2:"]
        assert [p["type"] for p in content] == [
            "text", "text", "image_url", "text", "image_url"]
        assert sent["top_k"] == 1
        assert sent["temperature"] == 0
        assert sent["max_tokens"] == 400
        assert (tally.requests, tally.read, tally.tokens) == (1, 120, 11)

    def test_numbered_pictures_anthropic_payload(self, monkeypatch, tmp_path):
        one = tmp_path / "one.png"
        two = tmp_path / "two.png"
        one.write_bytes(b"\x89PNGone")
        two.write_bytes(b"\x89PNGtwo")
        seen = []

        class Stream:
            def __init__(self, **request):
                seen.append(request)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get_final_message(self):
                return _Message("end_turn", _Block("text", '["A", "B"]'),
                                input_tokens=90, output_tokens=9)

        module = types.ModuleType("anthropic")
        module.Anthropic = lambda **opts: types.SimpleNamespace(
            beta=types.SimpleNamespace(
                messages=types.SimpleNamespace(stream=lambda **req: Stream(**req))))
        module.APIStatusError = type("APIStatusError", (Exception,), {})
        module.APIConnectionError = type("APIConnectionError", (Exception,), {})
        monkeypatch.setitem(sys.modules, "anthropic", module)

        tally = llmclient.Tally()
        llm = self._llm(llmApi="anthropic")
        answer = llm.ask(
            "", "Read two images.", tally, images=[str(one), str(two)],
            numbered=True, max_tokens=400)
        assert answer == '["A", "B"]'
        assert seen[0]["max_tokens"] == 400
        content = seen[0]["messages"][0]["content"]
        assert [p["text"] for p in content if p["type"] == "text"] == [
            "Read two images.", "Image 1:", "Image 2:"]
        assert [p["type"] for p in content] == [
            "text", "text", "image", "text", "image"]
        assert (tally.requests, tally.read, tally.tokens) == (1, 90, 9)

    def test_ask_pictures_batches_reasks_and_falls_back_one_by_one(
            self, monkeypatch, tmp_path):
        pics = []
        for name in ("a", "b", "c"):
            p = tmp_path / (name + ".png")
            p.write_bytes(b"\x89PNG" + name.encode())
            pics.append(str(p))

        llm = self._llm(llmUrl="http://localhost:8082/v1", llmModel="vision")
        calls = []

        def fake_ask(system, prompt, tally=None, images=(), **kwargs):
            calls.append((prompt, tuple(images), kwargs))
            if len(images) > 1:
                return '["one", "two", "short"]'
            return "full\ntwo lines"

        monkeypatch.setattr(llm, "ask", fake_ask)
        out = llm.ask_pictures(
            "", "BATCH", "ALONE", pics, max_tokens_each=200,
            allow_empty=True, reask=lambda i, t: t == "short")
        assert out == ["one", "two", "full\ntwo lines"]
        assert len(calls) == 2
        assert calls[0][0] == "BATCH" and calls[0][2]["max_tokens"] == 600
        assert calls[1][0] == "ALONE" and calls[1][1] == (pics[2],)

        # Short / invalid array falls back to asking each picture alone.
        calls.clear()

        def bad_batch(system, prompt, tally=None, images=(), **kwargs):
            calls.append(tuple(images))
            if len(images) > 1:
                return '["only one"]'
            return "text:" + os.path.basename(images[0])

        monkeypatch.setattr(llm, "ask", bad_batch)
        out = llm.ask_pictures("", "BATCH", "ALONE", pics, allow_empty=True)
        assert out == ["text:a.png", "text:b.png", "text:c.png"]
        assert len(calls) == 4

    def test_json_strings_and_stray_thinking_tag(self):
        assert llmclient.json_strings(
            '```json\n["Bon\\ jour.", "Two\\\\nlines"]\n```', 2) \
            == ["Bon jour.", "Two\\nlines"]
        assert llmclient.json_strings('["only one"]', 2) is None
        assert llmclient.json_strings("not json", 1) is None
        assert llmclient._strip_thinking("Hmm.</think>- Oui ?\n- Non.") \
            == "- Oui ?\n- Non."
