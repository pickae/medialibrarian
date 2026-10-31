"""The white box for medialib/lib/av1gpu.py.

Three things are pinned. An image only goes to the card when its header says
the CPU road would have written the same picture - no alpha, no resize - and an
answer that cannot be read keeps it on the CPU. A chunk is measured in
megapixels, not images. And a chunk hands back exactly the outputs the encoder
wrote, under the right names, and none at all from an encoder that died
mid-chunk.

The encoder is a script standing in for av1gpu: it does what batch mode does to
the file system, which is all the module reads of it.
"""

from __future__ import annotations

import pytest

from medialib.lib import av1gpu


class TestTheSmallPreset:
    """Below 1 MP av1gpu uses a preset of its own: `base`, unless the call
    asked for something slower still."""

    pytestmark = pytest.mark.pure

    @pytest.mark.parametrize("preset,small", [
        ("slow", "slow"), ("base", "base"), ("fast", "base"),
        ("turbo", "base"), ("turbo3", "base")])
    def test_it_is_never_faster_than_the_call_s_own(self, preset, small):
        assert av1gpu.small_preset(preset) == small


class TestTheProbe:
    pytestmark = pytest.mark.pure

    @pytest.mark.parametrize("text,pixels", [
        ("False 8 1000 1500 Undefined", 1_500_000),
        ("Undefined 8 1000 1500 Undefined", 1_500_000),  # a JPEG
        ("True 8 1000 1500 Undefined", 0),        # transparency
        ("Blend 8 1000 1500 Undefined", 0),
        ("False 16 1000 1500 Undefined", 1_500_000),  # a 16-bit PNG
        ("False 32 1000 1500 Undefined", 0),      # a float TIFF's depth
        ("False 8 1000 1501 Undefined", 0),       # -m would shrink it
        ("False 8 10001 1500 Undefined", 0),      # so would the width cap
        ("False 8 1000 1500 Undefined\n", 1_500_000),
        ("", 0),                                  # unreadable: CPU
        ("False 8 1000 1500", 0),                 # no orientation
        ("False 8 wide tall Undefined", 0),
    ])
    def test_the_header_decides(self, text, pixels):
        assert av1gpu.takes_probe(text, 10000, 1500) == pixels

    def test_one_av1gpu_would_decline_is_not_offered(self):
        """8000 x 6000 is 48 MP, past its 35.6."""
        assert av1gpu.takes_probe("False 8 8000 6000 Undefined",
                                  10000, 10000) == 0
        assert av1gpu.takes_probe("False 8 5965 5968 Undefined",
                                  10000, 10000) > 0

    @pytest.mark.parametrize("orientation,jpeg,takes", [
        ("RightTop", True, True),       # av1gpu writes a JPEG's into the AVIF
        ("RightTop", False, False),     # but not a PNG's or a WebP's
        ("TopLeft", False, True),
        ("Undefined", False, True),
    ])
    def test_an_orientation_only_a_jpeg_carries_across(self, orientation,
                                                       jpeg, takes):
        text = "False 8 1000 1500 " + orientation
        assert bool(av1gpu.takes_probe(text, 10000, 1500, jpeg=jpeg)) is takes


class TestTheChunks:
    """Megapixels a call, not images: the card's cost per call is the same
    however many images it holds."""

    pytestmark = pytest.mark.pure

    def test_the_budget_closes_a_chunk(self):
        sized = [("a", 4), ("b", 4), ("c", 4), ("d", 1)]
        assert list(av1gpu.chunks(sized, budget=8)) == [["a", "b"], ["c", "d"]]

    def test_the_order_is_kept(self):
        sized = [(n, 1) for n in "abcdef"]
        assert sum(av1gpu.chunks(sized, budget=4), []) == list("abcdef")

    def test_one_larger_than_the_budget_is_a_chunk_of_its_own(self):
        sized = [("a", 1), ("huge", 50), ("b", 1)]
        assert list(av1gpu.chunks(sized, budget=10)) == [
            ["a"], ["huge"], ["b"]]

    def test_nothing_makes_no_chunk(self):
        assert list(av1gpu.chunks([])) == []

    def test_a_folder_of_camera_photos_is_a_few_calls(self):
        """The 873 photos the budget was measured on: 12.3 gigapixels."""
        sized = [(n, 14_100_000) for n in range(873)]
        assert len(list(av1gpu.chunks(sized))) == 1


class TestTheBinary:
    pytestmark = pytest.mark.fs

    def test_an_empty_setting_means_the_cpu(self, monkeypatch):
        monkeypatch.setenv(av1gpu.ENCODER_VARIABLE, "")
        assert av1gpu.binary() == ""

    def test_one_that_is_not_there_means_the_cpu(self, monkeypatch, tmp_path):
        monkeypatch.setenv(av1gpu.ENCODER_VARIABLE, str(tmp_path / "av1gpu"))
        assert av1gpu.binary() == ""

    def test_a_path_that_is_executable_is_used(self, monkeypatch, tmp_path):
        tool = tmp_path / "av1gpu"
        tool.write_text("#!/bin/sh\n")
        tool.chmod(0o755)
        monkeypatch.setenv(av1gpu.ENCODER_VARIABLE, str(tool))
        assert av1gpu.binary() == str(tool)

    def test_unset_it_is_looked_for_on_path(self, monkeypatch, tmp_path):
        tool = tmp_path / av1gpu.DEFAULT_BINARY
        tool.write_text("#!/bin/sh\n")
        tool.chmod(0o755)
        monkeypatch.delenv(av1gpu.ENCODER_VARIABLE)
        monkeypatch.setenv("PATH", str(tmp_path))
        assert av1gpu.binary() == av1gpu.DEFAULT_BINARY


# Batch mode as far as the file system sees it: every listed input becomes
# <outdir>/<its base name>.avif holding the name of the file the link points at,
# except one whose target is called "decline*", which is skipped. Exits with
# $STUB_STATUS, or kills itself with $STUB_SIGNAL after writing.
_ENCODER = r'''#!/usr/bin/env bash
while [ $# -gt 0 ]; do
    case "$1" in
        --batch) list=$2; shift 2 ;;
        --outdir) out=$2; shift 2 ;;
        *) echo "$1" >> "$(dirname "$0")/args"; shift ;;
    esac
done
echo "$GP_FALLBACK_WORKERS" > "$(dirname "$0")/threads"
while IFS= read -r input; do
    target=$(readlink "$input")
    case "${target##*/}" in decline*) continue ;; esac
    echo "GPU ${target##*/}" > "$out/${input##*/}.avif"
done < "$list"
[ -n "$STUB_SIGNAL" ] && kill -"$STUB_SIGNAL" $$
exit "${STUB_STATUS:-0}"
'''


class TestAChunk:
    pytestmark = pytest.mark.fs

    @pytest.fixture
    def chunk(self, tmp_path, monkeypatch):
        tool = tmp_path / "bin" / "av1gpu"
        tool.parent.mkdir()
        tool.write_text(_ENCODER)
        tool.chmod(0o755)
        sources = tmp_path / "in"
        outputs = tmp_path / "out"
        for name in ("a/cover.jpg", "b/cover.jpg", "decline.png"):
            (sources / name).parent.mkdir(parents=True, exist_ok=True)
            (sources / name).write_text(name)
        (outputs / "a").mkdir(parents=True)
        (outputs / "b").mkdir()
        items = [(str(sources / "a/cover.jpg"), str(outputs / "a/cover.avif")),
                 (str(sources / "b/cover.jpg"), str(outputs / "b/cover.avif")),
                 (str(sources / "decline.png"), str(outputs / "decline.avif"))]

        def encode(**environment):
            for name, value in environment.items():
                monkeypatch.setenv(name, value)
            workdir = tmp_path / "work"
            workdir.mkdir()
            return av1gpu.encode_chunk(str(tool), items, str(workdir), 60,
                                       "turbo")

        encode.outputs = outputs
        encode.args = tool.parent / "args"
        encode.threads = tool.parent / "threads"
        return encode

    def test_each_output_reaches_its_own_image_s_destination(self, chunk):
        """Two `cover.jpg` in two folders would land on each other under their
        own names; numbered, each comes back to where it belongs."""
        done, status = chunk()
        assert (done, status) == ([0, 1], 0)
        assert (chunk.outputs / "a/cover.avif").read_text() == "GPU cover.jpg\n"
        assert (chunk.outputs / "b/cover.avif").read_text() == "GPU cover.jpg\n"

    def test_what_it_skipped_is_not_claimed(self, chunk):
        done, _ = chunk()
        assert 2 not in done
        assert not (chunk.outputs / "decline.avif").exists()

    def test_no_partial_name_is_left_behind(self, chunk):
        chunk()
        assert not [p for p in chunk.outputs.rglob("*.part")]

    def test_an_encoder_that_failed_still_hands_back_what_it_wrote(self, chunk):
        """A failure it exits from by itself is reported after its threads
        have finished writing."""
        assert chunk(STUB_STATUS="1") == ([0, 1], 1)

    def test_one_killed_mid_chunk_hands_back_nothing(self, chunk):
        """Any of its outputs could be half written."""
        done, status = chunk(STUB_SIGNAL="KILL")
        assert done == [] and status < 0
        assert not (chunk.outputs / "a/cover.avif").exists()

    def test_the_quality_and_preset_are_passed_as_given(self, chunk):
        chunk()
        assert chunk.args.read_text().split() == [
            "--quality", "60", "--preset", "turbo", "--small-preset", "base",
            "--tiles", "2,2,2", "--yuv", "420", "--depth", "10"]

    def test_png_and_webp_are_decoded_on_more_threads_than_its_own(
            self, chunk, monkeypatch):
        monkeypatch.delenv(av1gpu.DECODE_THREADS_VARIABLE, raising=False)
        chunk()
        assert chunk.threads.read_text() == "8\n"

    def test_a_number_the_environment_names_is_kept(self, chunk):
        chunk(GP_FALLBACK_WORKERS="3")
        assert chunk.threads.read_text() == "3\n"
