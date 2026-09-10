"""The white box for medialib/cli/convert_images.py.

`disambiguated_output` is the script's whole anti-clobber story: two source
images in one folder that differ only in extension (a.jpg and a.png) would both
map to a.avif, so when that happens the source extension is folded into the
output name and neither conversion can overwrite the other.

Two properties make it safe to call from inside each parallel worker, and both
are asserted below: it is DETERMINISTIC (no shared state, so every worker derives
the same answer) and STABLE across re-runs (so the resume check keeps
recognising an already-converted file).

The helper is Python, so these are asserted against it directly.

The other half is -o, the output format. AVIF, WebP and JPEG XL agree about
nothing - the bit depth each can carry, and the name AND the direction of each
one's encoder effort setting - so what is pinned below is that the one -s knob
still means "slower" in all three, that each format's number stays inside the
range its encoder accepts, and that the default -o avif call is byte for byte
the call this command made before there was an -o at all.
"""


import os

import pytest

from medialib.cli import convert_images as ci
from medialib.lib import clioptions, enums

pytestmark = pytest.mark.fs

OUT = "/out"


def _out(tmp_path, relative, extension="avif"):
    return ci.disambiguated_output(relative, extension, str(tmp_path), OUT)


def _touch(tmp_path, *names):
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")


class TestDisambiguatedOutput:
    def test_a_lone_stem_keeps_its_name(self, tmp_path):
        _touch(tmp_path, "lone/photo.jpg")
        assert _out(tmp_path, "lone/photo.jpg") == "/out/lone/photo.avif"

    def test_a_shared_stem_folds_the_source_extension_in(self, tmp_path):
        _touch(tmp_path, "clash/photo.jpg", "clash/photo.png")
        assert _out(tmp_path, "clash/photo.jpg") == "/out/clash/photo-jpg.avif"
        assert _out(tmp_path, "clash/photo.png") == "/out/clash/photo-png.avif"

    def test_the_two_colliding_sources_map_to_different_outputs(self, tmp_path):
        _touch(tmp_path, "clash/photo.jpg", "clash/photo.png")
        assert _out(tmp_path, "clash/photo.jpg") != \
            _out(tmp_path, "clash/photo.png")

    def test_the_same_input_is_answered_identically_twice(self, tmp_path):
        """Each parallel worker computes it independently, and a re-run has to
        recognise what the last one wrote."""
        _touch(tmp_path, "clash/photo.jpg", "clash/photo.png")
        assert _out(tmp_path, "clash/photo.jpg") == \
            _out(tmp_path, "clash/photo.jpg")

    def test_a_non_image_sibling_does_not_trigger_disambiguation(self,
                                                                 tmp_path):
        """Only the image types this script converts are counted, so a stray
        sidecar cannot rename the output."""
        _touch(tmp_path, "sidecar/photo.jpg", "sidecar/photo.txt",
               "sidecar/photo.xmp")
        assert _out(tmp_path, "sidecar/photo.jpg") == "/out/sidecar/photo.avif"

    def test_an_upper_case_sibling_still_triggers_it(self, tmp_path):
        """Extensions are lower-cased only later, so the count is
        case-tolerant here."""
        _touch(tmp_path, "upper/photo.jpg", "upper/photo.PNG")
        assert _out(tmp_path, "upper/photo.jpg") == "/out/upper/photo-jpg.avif"

    def test_the_upper_case_source_keeps_its_own_spelling(self, tmp_path):
        _touch(tmp_path, "upper/photo.jpg", "upper/photo.PNG")
        assert _out(tmp_path, "upper/photo.PNG") == "/out/upper/photo-PNG.avif"

    def test_an_avif_sibling_counts_towards_the_collision(self, tmp_path):
        """What keeps the answer stable when the conversion writes in place -
        the interaction between the disambiguation and the resume check."""
        _touch(tmp_path, "resumed/photo.jpg", "resumed/photo.avif")
        assert _out(tmp_path, "resumed/photo.jpg") == \
            "/out/resumed/photo-jpg.avif"

    def test_a_top_level_source_needs_no_directory_part(self, tmp_path):
        _touch(tmp_path, "top.jpg")
        assert _out(tmp_path, "top.jpg") == "/out/top.avif"

    def test_a_top_level_collision_is_disambiguated_too(self, tmp_path):
        _touch(tmp_path, "top.jpg", "top.png")
        assert _out(tmp_path, "top.jpg") == "/out/top-jpg.avif"

    def test_a_nested_path_is_mirrored_whole(self, tmp_path):
        _touch(tmp_path, "a/b/c/page.webp")
        assert _out(tmp_path, "a/b/c/page.webp") == "/out/a/b/c/page.avif"

    def test_only_the_last_dot_is_the_extension(self, tmp_path):
        _touch(tmp_path, "tricky/my photo.v2.jpg")
        assert _out(tmp_path, "tricky/my photo.v2.jpg") == \
            "/out/tricky/my photo.v2.avif"

    def test_the_output_extension_is_whatever_was_asked_for(self, tmp_path):
        """The same logic serves the reverse (avif -> jpeg) direction."""
        _touch(tmp_path, "rev/page.avif")
        assert _out(tmp_path, "rev/page.avif", "jpg") == "/out/rev/page.jpg"


# What each encoder's own effort setting reads as at the two ends of -s, in that
# encoder's units: the value at the slowest -s first, the fastest second. AVIF
# counts a speed and the other two count an effort, which is why two of these
# pairs run backwards.
_ENDS = {"avif": (0, 9), "webp": (6, 0), "jxl": (9, 1)}

_SLOWEST_SPEED = 0
_FASTEST_SPEED = ci.MAX_SPEED


def _run(image_format="avif", speed=5, quality=60, max_res=ci.UNBOUNDED_EDGE):
    """A Run carrying the options main() would have settled for these flags.

    Only the encoder arguments are asked of it, so the directories and the
    counter directory are names rather than places.
    """
    options = {
        "crop": False,
        "skipStarved": False,
        "format": image_format,
        "quality": quality,
        "effortLevel": ci.FORMATS[image_format].level(speed),
        "fuzzCommand": "10%",
        "maxResCommand": "%dx%s>" % (ci.UNBOUNDED_EDGE, max_res),
    }
    return ci.Run("/in", OUT, "/counters", options, 1)


class TestTheFormatTable:
    """The three formats -o takes, and the -s knob translated into each one."""

    def test_avif_is_still_the_default(self):
        assert ci.DEFAULT_FORMAT == "avif"

    def test_the_default_is_a_format_the_command_can_write(self):
        assert ci.DEFAULT_FORMAT in ci.FORMATS

    def test_the_table_covers_the_central_list_and_nothing_else(self):
        """enums.IMAGE_CODECS is what the page offers and the check accepts, so
        a codec there with no entry here would be accepted and then crash."""
        assert sorted(ci.FORMATS) == sorted(enums.IMAGE_CODECS)

    def test_every_format_names_its_own_effort_define(self):
        """A define borrowed from another format is silently ignored by
        ImageMagick, so the three cannot share one."""
        efforts = [ci.FORMATS[name].effort for name in ci.FORMATS]
        assert len(set(efforts)) == len(efforts)

    @pytest.mark.parametrize("name", sorted(_ENDS))
    def test_the_slowest_speed_asks_for_the_slowest_encode(self, name):
        assert ci.FORMATS[name].level(_SLOWEST_SPEED) == _ENDS[name][0]

    @pytest.mark.parametrize("name", sorted(_ENDS))
    def test_the_fastest_speed_asks_for_the_fastest_encode(self, name):
        assert ci.FORMATS[name].level(_FASTEST_SPEED) == _ENDS[name][1]

    @pytest.mark.parametrize("name", sorted(_ENDS))
    def test_no_speed_lands_outside_the_encoder_s_own_range(self, name):
        """The whole -s range, not just its ends: a value ImageMagick does not
        accept would only surface as a per-image failure."""
        low, high = sorted(_ENDS[name])
        levels = [ci.FORMATS[name].level(speed)
                  for speed in range(_SLOWEST_SPEED, _FASTEST_SPEED + 1)]
        assert [level for level in levels if not low <= level <= high] == []

    @pytest.mark.parametrize("name", sorted(_ENDS))
    def test_the_translation_never_doubles_back(self, name):
        """Monotone across the whole range, so a -s the user turned DOWN never
        asks for a faster encode than the -s above it."""
        levels = [ci.FORMATS[name].level(speed)
                  for speed in range(_SLOWEST_SPEED, _FASTEST_SPEED + 1)]
        towards_fast = levels if _ENDS[name][0] < _ENDS[name][1] else \
            list(reversed(levels))
        assert towards_fast == sorted(towards_fast)


class TestEncodeArguments:
    def test_the_default_run_is_the_avif_call_it_has_always_been(self):
        """The one case that must not have moved: -o defaults to avif, so a
        command line that predates the option converts exactly as it did."""
        assert _run()._encode_arguments("page.jpg", "/out/page.avif") == [
            "-format", "avif", "-depth", "10", "-quality", "60",
            "-define", "heic:speed=5", "page.jpg",
            "-resize", "10000x10000>", "/out/page.avif"]

    @pytest.mark.parametrize("name,depth", [("avif", "10"), ("webp", "8"),
                                            ("jxl", "10")])
    def test_each_format_is_written_at_the_depth_it_can_carry(self, name,
                                                              depth):
        argv = _run(name)._encode_arguments("page.jpg", "/out/page." + name)
        assert argv[argv.index("-depth") + 1] == depth

    @pytest.mark.parametrize("name", sorted(ci.FORMATS))
    def test_each_format_names_itself_to_the_encoder(self, name):
        argv = _run(name)._encode_arguments("page.jpg", "/out/page." + name)
        assert argv[argv.index("-format") + 1] == name

    @pytest.mark.parametrize("name,define", [("avif", "heic:speed=5"),
                                             ("webp", "webp:method=3"),
                                             ("jxl", "jxl:effort=5")])
    def test_the_speed_reaches_the_encoder_under_its_own_name(self, name,
                                                              define):
        argv = _run(name)._encode_arguments("page.jpg", "/out/page." + name)
        assert argv[argv.index("-define") + 1] == define

    def test_the_border_sits_between_the_source_and_the_resize(self):
        """Where the crop's restored margin has to go: an operation on the
        loaded image is applied after it is read and before it is scaled."""
        argv = _run("jxl")._encode_arguments(
            "miff:trimmed", "/out/page.jxl",
            ["-bordercolor", "white", "-border", "7"])
        assert argv.index("miff:trimmed") < argv.index("-bordercolor") \
            < argv.index("-resize")

    def test_the_output_path_is_the_last_word(self):
        """ImageMagick reads the last argument as the destination, so nothing
        the extra arguments add may follow it."""
        argv = _run("webp")._encode_arguments(
            "miff:trimmed", "/out/page.webp",
            ["-bordercolor", "white", "-border", "7"])
        assert argv[-1] == "/out/page.webp"


class TestTheFormatReachesTheOutputName:
    """disambiguated_output is called with whatever -o settled on, and the
    collision set has to know every one of those extensions."""

    @pytest.mark.parametrize("name", sorted(ci.FORMATS))
    def test_a_lone_stem_takes_the_chosen_extension(self, tmp_path, name):
        _touch(tmp_path, "lone/photo.jpg")
        assert _out(tmp_path, "lone/photo.jpg", name) == \
            os.path.join(OUT, "lone/photo." + name)

    @pytest.mark.parametrize("name", sorted(ci.FORMATS))
    def test_every_written_extension_counts_towards_a_collision(self, tmp_path,
                                                                name):
        """A source already in the target format is what the resume check reads
        back, so it has to keep the disambiguated name stable."""
        _touch(tmp_path, "resumed/photo.jpg", "resumed/photo." + name)
        assert _out(tmp_path, "resumed/photo.jpg", name) == \
            os.path.join(OUT, "resumed/photo-jpg." + name)

    def test_a_jxl_sibling_is_not_mistaken_for_a_sidecar(self, tmp_path):
        assert "jxl" in ci._SAME_STEM and "JXL" in ci._SAME_STEM


class TestTheStarvedSkip:
    """The -a decision, which is what this command does INSTEAD of converting.

    The verdict itself belongs to imagebitrate and is pinned there; what is
    pinned here is that the flag reaches the decision, that a skipped image
    leaves no output, and that the two exemptions - -a and -r - are exemptions.
    """

    def _run(self, tmp_path, verdict, skip_starved=True):
        """A Run over one image, with the model's answer stood in for: the
        conversion path is what is under test, not the arithmetic."""
        counters = tmp_path / "counters"
        counters.mkdir()
        for name in ("current", "converted", "trimmed", "blank", "starved",
                     "alreadyDone", "notFound"):
            (counters / name).write_text("0")
        options = {
            "crop": False,
            "skipStarved": skip_starved,
            "format": "avif",
            "quality": 60,
            "effortLevel": 5,
            "fuzzCommand": "10%",
            "maxResCommand": "10000x10000>",
        }
        state = ci.Run(str(tmp_path), str(tmp_path / "out"), str(counters),
                       options, 1)
        state.starved = lambda relative: verdict
        state._convert = lambda arguments: converted.append(arguments) or 0
        converted = []
        return state, converted

    def test_a_starved_image_is_not_converted(self, tmp_path, monkeypatch):
        (tmp_path / "page.jpg").write_bytes(b"x")
        (tmp_path / "out").mkdir()
        monkeypatch.chdir(tmp_path)
        state, converted = self._run(tmp_path, verdict=True)
        state.transcode("page.jpg")
        assert converted == []
        assert state.counter("starved") == 1
        assert not (tmp_path / "out" / "page.avif").exists()

    def test_one_that_is_not_starved_is(self, tmp_path, monkeypatch):
        (tmp_path / "page.jpg").write_bytes(b"x")
        (tmp_path / "out").mkdir()
        monkeypatch.chdir(tmp_path)
        state, converted = self._run(tmp_path, verdict=False)
        state.transcode("page.jpg")
        assert len(converted) == 1
        assert state.counter("converted") == 1

    def test_and_neither_is_asked_when_the_run_was_told_to_convert_everything(
            self, tmp_path, monkeypatch):
        """-a: the question is not asked at all, so an image the model would
        have refused is converted with the rest."""
        (tmp_path / "page.jpg").write_bytes(b"x")
        (tmp_path / "out").mkdir()
        monkeypatch.chdir(tmp_path)
        state, converted = self._run(tmp_path, verdict=True,
                                     skip_starved=False)
        state.transcode("page.jpg")
        assert len(converted) == 1
        assert state.counter("starved") == 0

    def test_an_image_that_could_not_be_measured_is_converted(self, tmp_path,
                                                              monkeypatch):
        """The same answer convert-video's -t gives an unreadable source: the
        check exists to avoid pointless work, and refusing a conversion over a
        missing measurement would lose one worth doing."""
        image = tmp_path / "page.jpg"
        image.write_bytes(b"not an image at all")
        monkeypatch.chdir(tmp_path)
        assert ci.Run(str(tmp_path), str(tmp_path / "out"), "/counters",
                      {}, 1).starved("page.jpg") is False


class TestTheOptionsAreSettledUpFront:
    """Both new checks refuse the run before an image is touched, because
    neither mistake would show up until deep inside it."""

    @staticmethod
    def _parse(*argv):
        return clioptions.parse(ci.spec("convert-images"), list(argv))

    @pytest.mark.parametrize("name", sorted(enums.IMAGE_CODECS))
    def test_each_offered_format_is_accepted(self, name):
        assert self._parse("-o", name, "in", "out").values["outputFormat"] \
            == name

    def test_a_format_no_encoder_here_writes_is_refused(self):
        with pytest.raises(clioptions.UsageError) as refusal:
            self._parse("-o", "gif", "in", "out")
        assert ", ".join(enums.IMAGE_CODECS) in refusal.value.message

    def test_the_page_advertises_exactly_what_the_check_accepts(self):
        """Both are generated from the one list, and this is what says so."""
        for codec in enums.IMAGE_CODECS:
            assert codec in ci.OPT_SPEC

    def test_the_long_form_is_the_same_option(self):
        assert self._parse("--format=jxl", "in", "out").values["outputFormat"] \
            == "jxl"

    def test_the_starved_skip_is_on_unless_a_flag_turns_it_off(self):
        assert "a" not in self._parse("in", "out").given
        assert "a" in self._parse("-a", "in", "out").given
        assert "a" in self._parse("--always", "in", "out").given

    def test_a_speed_that_is_not_a_number_is_refused(self):
        """Every format's effort setting is derived from -s arithmetically, so
        a non-number would be a traceback rather than a refusal."""
        with pytest.raises(clioptions.UsageError):
            self._parse("-s", "fast", "in", "out")

    @pytest.mark.parametrize("speed", ["-1", "10", "11"])
    def test_a_speed_outside_the_range_is_refused(self, speed):
        """10 is the one that matters. The AVIF encoder REFUSES it rather than
        clamping - it writes a zero-byte file and says "Invalid parameter
        value" - so a run that accepted it would produce a tree of empty
        images with stderr silenced."""
        with pytest.raises(clioptions.UsageError):
            self._parse("-s", speed, "in", "out")

    def test_the_top_of_the_range_is_accepted(self):
        assert self._parse("-s", str(ci.MAX_SPEED), "in",
                           "out").values["speedPreset"] == str(ci.MAX_SPEED)

    def test_no_format_given_leaves_the_default_to_the_run(self):
        assert self._parse("in", "out").values["outputFormat"] == ""
