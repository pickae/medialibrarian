"""Tests for what a language model is - medialib.lib.llmtraits.

What is pinned: a model's name says what it is where nothing else does; a
local server's word beats the name; the user's word beats both; and size,
speed and cost are worked out from what is known, the way a task's lean
reads them.
"""

import pytest

from medialib.lib import llmtraits

pytestmark = pytest.mark.pure


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    llmtraits.forget()
    monkeypatch.setattr(llmtraits, "_server_json", lambda *a, **k: None)


def _named(model, local=True, api="openai", said=None):
    return llmtraits.detect(api, "http://localhost:1/v1" if local else
                            "https://api.example.com/v1", model, "", local, said)


class TestTheName:
    def test_a_mixture_that_sees(self):
        traits = _named("Qwen3-VL-30B-A3B-Instruct")
        assert (traits.vision, traits.tools, traits.moe) == (True, True, True)
        assert (traits.params_b, traits.active_b) == (30.0, 3.0)
        # An instruct release of Qwen3 does not think first.
        assert traits.reasoning is False
        assert (traits.size, traits.speed, traits.cheap) == ("medium", "fast", True)

    def test_a_large_dense_model_is_slow(self):
        traits = _named("qwen3.8-27b")
        assert traits.moe is None and traits.reasoning
        assert (traits.size, traits.speed) == ("medium", "slow")

    def test_a_mixture_whose_name_hides_its_active_parameters(self):
        traits = _named("gpt-oss-120b")
        assert traits.moe and traits.active_b == 5.1 and traits.speed == "fast"
        assert traits.size == "large"

    @pytest.mark.parametrize("model, vision", [
        ("llava:13b", True), ("gemma3:27b", True), ("pixtral-12b", True),
        ("llama3.1:8b", False), ("devstral-small", False)])
    def test_seeing(self, model, vision):
        assert _named(model).vision is vision

    def test_a_maker_of_images(self):
        assert _named("gpt-image-1", local=False).images_out

    @pytest.mark.parametrize("model, size, speed, cheap", [
        ("claude-haiku-5", "small", "fast", True),
        ("gpt-5-mini", "small", "fast", True),
        ("gpt-5", "large", "slow", False)])
    def test_a_hosted_family(self, model, size, speed, cheap):
        traits = _named(model, local=False)
        assert (traits.size, traits.speed, traits.cheap) == (size, speed, cheap)

    def test_claude_sees_calls_tools_and_reasons(self):
        traits = _named("claude-opus-5-5", local=False, api="anthropic")
        assert traits.vision and traits.tools and traits.reasoning
        assert not traits.cheap


class TestTheServer:
    def _server(self, monkeypatch, answers):
        def server(url, key, body=None):
            return next((value for path, value in answers.items()
                         if url.endswith(path)), None)
        monkeypatch.setattr(llmtraits, "_server_json", server)

    def test_llama_cpp_says_what_it_holds(self, monkeypatch):
        self._server(monkeypatch, {"/props": {
            "modalities": {"vision": False},
            "chat_template_caps": {"supports_tools": True}}})
        traits = _named("some-vl-model")
        assert (traits.vision, traits.tools) == (False, True)

    def test_ollama_says_its_capabilities_size_and_experts(self, monkeypatch):
        self._server(monkeypatch, {"/api/show": {
            "capabilities": ["completion", "vision", "thinking"],
            "details": {"parameter_size": "30.5B"},
            "model_info": {"qwen3moe.expert_count": 128}}})
        traits = _named("my-own-name")
        assert (traits.vision, traits.tools, traits.reasoning) == (True, False, True)
        assert (traits.params_b, traits.moe) == (30.5, True)

    def test_a_hosted_api_is_not_asked(self, monkeypatch):
        monkeypatch.setattr(llmtraits, "_server_json",
                            lambda *a, **k: pytest.fail("a hosted API was asked"))
        assert _named("gpt-4o-mini", local=False).vision


class TestTheUsersWord:
    def test_it_beats_the_name_and_the_server(self):
        traits = _named("Qwen3-VL-30B-A3B", said=llmtraits.parse_traits(
            "-vision,reasoning,size=large,speed=slow,cost_out=0.5"))
        assert (traits.vision, traits.reasoning) == (False, True)
        assert (traits.size, traits.speed) == ("large", "slow")

    def test_the_catalogues_names_are_read(self):
        said = llmtraits.declared({"tool_call": True, "attachment": False,
                                   "dense": True, "cost_out": 30})
        assert said == {"tools": True, "vision": False, "moe": False,
                        "cost_out": 30.0}
        assert not _named("x", local=False, said=said).cheap

    @pytest.mark.parametrize("text, problem", [
        ("telepathy", "no trait"), ("size=huge", "size"),
        ("params=many", "number"), ("speed=warp", "speed")])
    def test_a_declaration_that_is_none(self, text, problem):
        with pytest.raises(llmtraits.TraitsError, match=problem):
            llmtraits.parse_traits(text)

    def test_the_description_says_it_in_a_few_words(self):
        assert _named("Qwen3-VL-30B-A3B-Instruct").describe() == (
            "local, medium, fast, cheap, MoE 30B-A3B, tools, vision")
