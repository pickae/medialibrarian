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
"""


import pytest

from medialib.cli import convert_images as ci

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
