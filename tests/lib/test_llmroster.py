"""Tests for the roster of language models and the tasks that choose among
them - medialib.lib.llmroster.

What is pinned: the roster file and the single-model settings both give
models; a task leaves out a model without what it needs, puts first those
with more of what it prefers, and then orders by its lean; the user's own
order breaks every tie; a task or a model can be pinned in the file; a
mistake in the file is said, naming where it is.
"""

import os

import pytest

from medialib.lib import llmclient, llmroster, llmtraits, runcontext

pytestmark = pytest.mark.pure

# Every model's traits are said outright, so nothing is asked of a server and
# no name is guessed at.
_ROSTER = '''
[models.big-local]
url = "http://localhost:8081/v1"
model = "qwen3-vl-235b-a22b"
traits = { vision = true, reasoning = true, params = 235, active = 22, speed = "slow" }

[models.small-local]
url = "http://localhost:8082/v1"
model = "gemma3-4b"
traits = { vision = true, reasoning = false, params = 4 }

[models.claude]
api = "anthropic"
model = "claude-opus-5-5"
traits = { cost_out = 25 }
'''


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    llmroster.forget()
    llmtraits.forget()
    monkeypatch.setattr(llmtraits, "_server_json", lambda *a, **k: None)
    for name in llmclient.SETTING_FIELDS.values():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(llmroster.LEAN_VARIABLE, raising=False)
    monkeypatch.setenv(llmroster.MODELS_VARIABLE, str(tmp_path / "models.toml"))
    yield
    for name in llmclient.SETTING_FIELDS.values():
        os.environ.pop(name, None)


def _roster(tmp_path, text=_ROSTER):
    (tmp_path / "models.toml").write_text(text, encoding="utf-8")
    return llmroster.load()


def _names(ladder):
    return [llm.name for llm in ladder]


class TestTheLadder:
    def test_what_a_task_needs_leaves_the_rest_out(self, tmp_path):
        # Only Claude searches here: no harness is set up for the others.
        assert _names(_roster(tmp_path).ladder(llmroster.COVER_SEARCH)) == ["claude"]
        assert _names(_roster(tmp_path).ladder(llmroster.GAPS_CHECK)) == ["claude"]

    def test_a_light_task_takes_the_small_cheap_and_fast_first(self, tmp_path):
        assert _names(_roster(tmp_path).ladder(llmroster.COVER_LOOK)) == [
            "small-local", "big-local", "claude"]
        assert _names(_roster(tmp_path).ladder(llmroster.BOOK_LANGUAGE)) == [
            "small-local", "big-local", "claude"]
        assert _names(_roster(tmp_path).ladder(llmroster.BOOK_IMAGE_CHECK)) == [
            "small-local", "big-local", "claude"]
        assert _names(_roster(tmp_path).ladder(llmroster.TRACK_ROLE)) == [
            "small-local", "big-local", "claude"]

    def test_bonus_category_leans_light_with_no_required_traits(self, tmp_path):
        assert llmroster.BONUS_CATEGORY.requires == ()
        assert llmroster.BONUS_CATEGORY.prefers == ()
        assert llmroster.BONUS_CATEGORY.lean == "light"
        assert _names(_roster(tmp_path).ladder(llmroster.BONUS_CATEGORY)) == [
            "small-local", "big-local", "claude"]

    def test_what_a_task_prefers_comes_before_its_lean(self, tmp_path):
        # Chapters prefer reasoning; small-local has none.
        assert _names(_roster(tmp_path).ladder(llmroster.CHAPTERS)) == [
            "big-local", "claude", "small-local"]
        assert _names(_roster(tmp_path).ladder(llmroster.COMMENTARY_JUDGE)) == [
            "big-local", "claude", "small-local"]

    def test_curated_lists_prefers_local_reasoning_models(self, tmp_path):
        # big-local has both reasoning and local; small-local has local and
        # claude has reasoning (both match one preferred trait, roster order
        # decides ties under balanced lean).
        assert llmroster.TASKS["curated-lists"] is llmroster.CURATED_LISTS
        assert _names(_roster(tmp_path).ladder(llmroster.CURATED_LISTS)) == [
            "big-local", "small-local", "claude"]

    def test_a_precise_task_takes_the_large_first(self, tmp_path):
        task = llmroster.Task("t", "test", lean="precise")
        assert _names(_roster(tmp_path).ladder(task)) == [
            "big-local", "claude", "small-local"]

    def test_a_balanced_task_keeps_the_rosters_order(self, tmp_path):
        task = llmroster.Task("t", "test")
        assert _names(_roster(tmp_path).ladder(task)) == [
            "big-local", "small-local", "claude"]

    def test_letterboxd_match_prefers_reasoning_and_web_balanced(self, tmp_path):
        # Claude has both reasoning and web; big-local has reasoning; small-local has neither.
        assert _names(_roster(tmp_path).ladder(llmroster.LETTERBOXD_MATCH)) == [
            "claude", "big-local", "small-local"]

    def test_the_setting_leans_every_task(self, tmp_path, monkeypatch):
        monkeypatch.setenv(llmroster.LEAN_VARIABLE, "precise")
        assert _names(_roster(tmp_path).ladder(llmroster.COVER_LOOK)) == [
            "big-local", "claude", "small-local"]

    def test_the_file_leans_a_task_or_gives_its_ladder(self, tmp_path):
        roster = _roster(tmp_path, _ROSTER + '''
[tasks.cover-look]
lean = "precise"

[tasks.chapters]
models = ["small-local", "claude"]
''')
        assert _names(roster.ladder(llmroster.COVER_LOOK))[0] == "big-local"
        assert _names(roster.ladder(llmroster.CHAPTERS)) == ["small-local", "claude"]

    def test_a_model_kept_to_some_tasks(self, tmp_path):
        text = _ROSTER.replace('traits = { cost_out = 25 }',
                               'traits = { cost_out = 25 }\ntasks = ["cover-search"]')
        roster = _roster(tmp_path, text)
        assert "claude" not in _names(roster.ladder(llmroster.CHAPTERS))
        assert _names(roster.ladder(llmroster.COVER_SEARCH)) == ["claude"]

    def test_a_harness_lets_a_local_model_search(self, tmp_path, monkeypatch):
        monkeypatch.setattr(llmclient.llmharness, "installed",
                            lambda harnesses: harnesses[0] if harnesses else None)
        text = _ROSTER.replace('model = "gemma3-4b"', 'model = "gemma3-4b"\n'
                               'harness = "opencode"\nharness_model = "local/gemma3"')
        # It searches now; Claude, which also reasons, still comes first.
        assert _names(_roster(tmp_path, text).ladder(llmroster.COVER_SEARCH)) == [
            "claude", "small-local"]

    def test_commentary_names_prefers_reasoning_and_web_but_keeps_the_rest(self, tmp_path):
        # Claude has reasoning + web, big-local has reasoning, small-local has neither.
        assert _names(_roster(tmp_path).ladder(llmroster.COMMENTARY_NAMES)) == [
            "claude", "big-local", "small-local"]

    def test_fragment_classify_prefers_reasoning_and_web_with_balanced_lean(
            self, tmp_path):
        # Claude has both reasoning and web; big-local has reasoning only;
        # small-local has neither, and is still kept because nothing is required.
        assert llmroster.FRAGMENT_CLASSIFY.name in llmroster.TASKS
        assert llmroster.FRAGMENT_CLASSIFY.prefers == ("reasoning", "web")
        assert llmroster.FRAGMENT_CLASSIFY.lean == "balanced"
        assert _names(_roster(tmp_path).ladder(llmroster.FRAGMENT_CLASSIFY)) == [
            "claude", "big-local", "small-local"]


class TestTheSources:
    def test_the_single_model_settings_come_after_the_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("llmUrl", "http://localhost:9/v1")
        monkeypatch.setenv("llmModel", "m")
        assert _names(_roster(tmp_path).models) == [
            "big-local", "small-local", "claude", "llm"]

    def test_no_file_and_no_settings_is_no_model(self):
        roster = llmroster.load()
        assert roster.models == []
        assert "no language model is set up" in roster.why_none(llmroster.CHAPTERS)

    def test_none_able_says_what_the_task_takes(self, tmp_path):
        roster = _roster(tmp_path, _ROSTER.split("[models.claude]")[0])
        assert "web search" in roster.why_none(llmroster.COVER_SEARCH)

    @pytest.mark.parametrize("text, problem", [
        ('[models.a]\nurl = "http://localhost:1/v1"\ncolour = "blue"', "no field colour"),
        ('[models.a]\nurl = "http://localhost:1/v1"\ntasks = ["knitting"]', "knitting"),
        ('[tasks.chapters]\nlean = "heavy"', "lean"),
        ('[tasks.chapters]\nmodels = ["nobody"]', "nobody"),
        ('[models.a]\nurl = "http://localhost:1/v1"\ntraits = { size = "huge" }', "size"),
        ('[other]\nx = 1', "takes \\[models"),
        ('not toml', "not TOML"),
    ])
    def test_a_mistake_in_the_file_is_named(self, tmp_path, text, problem):
        with pytest.raises(llmroster.RosterError, match=problem):
            _roster(tmp_path, text)

    @pytest.mark.skipif(os.name == "nt", reason="Windows keeps no mode bits")
    def test_a_key_in_a_file_others_can_read_is_refused(self, tmp_path):
        path = tmp_path / "models.toml"
        path.write_text('[models.a]\nurl = "http://localhost:1/v1"\nkey = "s3cret"\n')
        os.chmod(str(path), 0o644)
        with pytest.raises(llmroster.RosterError, match="chmod 600"):
            llmroster.load()

    def test_a_key_can_come_from_a_variable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MY_TEST_KEY", "from-the-environment")
        roster = _roster(tmp_path, '[models.a]\nurl = "http://localhost:1/v1"\n'
                                   'key_env = "MY_TEST_KEY"\n')
        assert roster.models[0].key == "from-the-environment"


class TestAskingForOne:
    def test_require_refuses_and_says_why(self, capsys):
        assert llmroster.require("x", llmroster.COVER_SEARCH) is None
        assert "no language model is set up" in capsys.readouterr().err

    def test_require_gives_the_first_of_the_ladder(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SKIP_TOOL_PREFLIGHT", "1")
        _roster(tmp_path)
        assert llmroster.require("x", llmroster.CHAPTERS).name == "big-local"

    def test_optional_says_nothing_and_gives_the_ladder(self, tmp_path, capsys):
        _roster(tmp_path)
        ladder, why = llmroster.optional(llmroster.COVER_LOOK)
        assert ladder and why == ""
        assert capsys.readouterr().err == ""

    def test_optional_gives_none_without_a(self, tmp_path, monkeypatch, capsys):
        _roster(tmp_path)
        monkeypatch.delenv(runcontext.AI_VARIABLE)
        ladder, why = llmroster.optional(llmroster.COVER_LOOK)
        assert ladder == [] and "-A" in why
        assert capsys.readouterr().err == ""

    def test_optional_reads_a_given_environment(self, tmp_path, monkeypatch):
        _roster(tmp_path)
        assert llmroster.optional(llmroster.COVER_LOOK, environ={})[0] == []

    def test_require_does_not_ask_for_a(self, tmp_path, monkeypatch):
        """generate-chapters is nothing but a model's work, and the commands
        whose -A asks for chapters have checked it already."""
        monkeypatch.setenv("SKIP_TOOL_PREFLIGHT", "1")
        monkeypatch.delenv(runcontext.AI_VARIABLE)
        _roster(tmp_path)
        assert llmroster.require("x", llmroster.CHAPTERS).name == "big-local"

    def test_the_chapter_task_follows_the_instructions(self):
        assert llmroster.chapters_task("") is llmroster.CHAPTERS
        assert llmroster.chapters_task("Name the guests") is llmroster.CHAPTERS_INSTRUCTED


class TestTheQueue:
    """A local model's requests queue for its GPU; a cloud API's never wait.
    Which one a model is, is its local trait - the address, or what the
    user declared."""

    @pytest.mark.parametrize("entry, jobs", [
        ({"url": "http://localhost:8080/v1", "model": "m"}, 2),
        ({"url": "https://api.openai.com/v1", "model": "gpt-5"}, 0),
        ({"api": "anthropic"}, 0),
        # A proxy on this machine in front of a hosted API.
        ({"url": "http://localhost:4000/v1", "model": "m", "traits": "cloud"}, 0),
        ({"url": "http://localhost:4000/v1", "model": "m",
          "traits": {"local": False}}, 0),
        # A server elsewhere the user knows to be theirs.
        ({"url": "https://gpu.example.com/v1", "model": "m",
          "traits": {"local": True}}, 2),
        # What the jobs setting says, it says either way.
        ({"url": "https://api.openai.com/v1", "model": "gpt-5", "jobs": "4"}, 4),
    ])
    def test_it_follows_the_local_trait(self, monkeypatch, entry, jobs):
        monkeypatch.setattr(llmclient.socket, "getaddrinfo",
                            lambda host, port: [(0, 0, 0, "", ("93.184.216.34", 0))])
        llm = llmclient.build(entry, "m")
        assert llm.jobs == jobs
        assert (llm._slots is None) == (jobs == 0)
        assert llm.traits.local == (jobs == 2)

    def test_a_model_made_by_hand_is_local_as_its_address_says(self):
        assert llmclient.Llm(api="openai", url="http://localhost:1/v1", model="m").local
        assert not llmclient.Llm(api="openai", url="", model="m").local


class TestSubtitleOcrTask:
    """SUBTITLE_OCR requires vision, prefers local and cheap, and leans light."""

    def test_selects_local_cheap_light_vision_first(self, tmp_path):
        roster = _roster(tmp_path, '''
[models.text-only]
url = "http://localhost:8080/v1"
model = "qwen3-8b"
traits = { vision = false, params = 8 }

[models.cloud-vision]
api = "anthropic"
model = "claude-opus-5-5"
traits = { vision = true, cost_out = 25, size = "large" }

[models.big-local-vision]
url = "http://localhost:8081/v1"
model = "qwen3-vl-235b"
traits = { vision = true, params = 235, speed = "slow" }

[models.small-local-vision]
url = "http://localhost:8082/v1"
model = "gemma3-4b"
traits = { vision = true, params = 4, speed = "fast" }
''')
        assert _names(roster.ladder(llmroster.SUBTITLE_OCR)) == [
            "small-local-vision", "big-local-vision", "cloud-vision"]
