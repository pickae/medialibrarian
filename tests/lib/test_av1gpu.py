"""The white box for medialib/lib/av1gpu.py.

Three things are pinned. An image only goes to the card when its header says
the card can write the picture the CPU road would - no alpha, within its size,
any resize asked of the card - and an answer that cannot be read keeps it on
the CPU. A chunk is measured in
megapixels, not images. And a chunk hands back exactly the outputs the encoder
wrote, under the right names, and none at all from an encoder that died
mid-chunk.

The encoder is a script standing in for av1gpu: it does what batch mode does to
the file system, which is all the module reads of it.
"""

from __future__ import annotations

import pytest

from medialib.lib import av1gpu, errorlog


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

    @pytest.mark.parametrize("text,size", [
        ("False 8 1000 1500 Undefined", (1000, 1500)),
        ("Undefined 8 1000 1500 Undefined", (1000, 1500)),  # a JPEG
        ("True 8 1000 1500 Undefined", None),     # transparency
        ("Blend 8 1000 1500 Undefined", None),
        ("False 16 1000 1500 Undefined", (1000, 1500)),  # a 16-bit PNG
        ("False 32 1000 1500 Undefined", None),   # a float TIFF's depth
        ("False 8 30000 1500 Undefined", (30000, 1500)),  # the card shrinks it
        ("False 8 1000 1500 Undefined\n", (1000, 1500)),
        ("", None),                               # unreadable: CPU
        ("False 8 1000 1500", None),              # no orientation
        ("False 8 wide tall Undefined", None),
        ("False 8 0 1500 Undefined", None),
    ])
    def test_the_header_decides(self, text, size):
        probed = av1gpu.takes_probe(text)
        assert (probed and probed[:2]) == size

    def test_up_to_a_hundred_megapixels_are_offered(self):
        """Not av1gpu's own 35.6: it is asked for 100."""
        assert av1gpu.takes_probe("False 8 8000 6000 Undefined")
        assert av1gpu.takes_probe("False 8 10000 10000 Undefined")
        assert av1gpu.takes_probe("False 8 10000 10001 Undefined") is None

    @pytest.mark.parametrize("orientation,jpeg,takes", [
        ("RightTop", True, True),       # av1gpu writes a JPEG's into the AVIF
        ("RightTop", False, False),     # but not a PNG's or a WebP's
        ("TopLeft", False, True),
        ("Undefined", False, True),
    ])
    def test_an_orientation_only_a_jpeg_carries_across(self, orientation,
                                                       jpeg, takes):
        text = "False 8 1000 1500 " + orientation
        assert bool(av1gpu.takes_probe(text, jpeg=jpeg)) is takes


class TestTheShrink:
    """The card's -m: ImageMagick's size, and its box turned where av1gpu
    turns it."""

    pytestmark = pytest.mark.pure

    @pytest.mark.parametrize("size,cap,fitted", [
        ((2000, 3000), 1500, (1000, 1500)),
        ((2035, 2961), 2960, (2034, 2960)),
        ((1000, 1500), 1500, (1000, 1500)),     # never enlarged
        ((1000, 1500), 0, (1000, 1500)),        # no cap
    ])
    def test_the_size_is_imagemagick_s(self, size, cap, fitted):
        assert av1gpu.fitted_size(*size, cap) == fitted

    @pytest.mark.parametrize("orientation,box", [
        ("Undefined", "x2960"), ("TopLeft", "x2960"),
        ("RightTop", "2960x"), ("LeftBottom", "2960x")])
    def test_the_cap_is_on_the_stored_rows(self, orientation, box):
        """av1gpu's box is the displayed size; ImageMagick's -resize caps the
        stored rows, which a quarter turn displays as the width."""
        assert av1gpu.max_size(2960, orientation) == box


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


# What --help of an av1gpu that crops and resizes says, as far as it is read.
_HELP = '#!/bin/sh\necho "  --max-input-mp MP       largest source decoded"\n'


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
        tool.write_text(_HELP)
        tool.chmod(0o755)
        monkeypatch.setenv(av1gpu.ENCODER_VARIABLE, str(tool))
        assert av1gpu.binary() == str(tool)

    def test_unset_it_is_looked_for_on_path(self, monkeypatch, tmp_path):
        tool = tmp_path / av1gpu.DEFAULT_BINARY
        tool.write_text(_HELP)
        tool.chmod(0o755)
        monkeypatch.delenv(av1gpu.ENCODER_VARIABLE)
        monkeypatch.setenv("PATH", str(tmp_path))
        assert av1gpu.binary() == av1gpu.DEFAULT_BINARY

    def test_whether_it_trims_is_read_from_its_help(self, tmp_path):
        """--trim with the conditions convert-images sets is what makes the
        card decide a -c trim; an av1gpu without it is given the box."""
        old, new = tmp_path / "old", tmp_path / "new"
        old.write_text(_HELP)
        new.write_text(_HELP + 'echo "  --trim-lightness L      trim only when"\n')
        for tool in (old, new):
            tool.chmod(0o755)
        assert (av1gpu.trims(str(old)), av1gpu.trims(str(new))) == (
            False, True)
        assert av1gpu.transforms(str(new))

    def test_one_that_cannot_crop_or_resize_means_the_cpu(self, monkeypatch,
                                                          tmp_path):
        """An av1gpu older than 0.7.0, which is said once."""
        tool = tmp_path / "av1gpu"
        tool.write_text("#!/bin/sh\necho '  --batch LIST|DIR'\n")
        tool.chmod(0o755)
        monkeypatch.setenv(av1gpu.ENCODER_VARIABLE, str(tool))
        said = []
        assert av1gpu.binary(said.append) == ""
        assert len(said) == 1 and "0.7.0" in said[0]


# Batch mode as far as the file system sees it: every listed input (the line up
# to its first TAB) becomes
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
cp "$list" "$(dirname "$0")/list"
while IFS= read -r line; do
    input=${line%%$'\t'*}
    target=$(readlink "$input")
    case "${target##*/}" in
        decline*) echo "skipped ${target##*/}: not decodable" >&2; continue ;;
        blank*) echo "[av1gpu] skipped $input: blank (--trim would remove more than 99% of its width or height, --skip-blank)" >&2
                continue ;;
    esac
    echo "GPU ${target##*/}" > "$out/${input##*/}.avif"
done < "$list"
echo "batch finished"
grep -q -- --trim "$(dirname "$0")/args" 2>/dev/null && echo "[av1gpu] trim: 2 images trimmed, 1 skipped as blank" >&2
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
                 (str(sources / "b/cover.jpg"), str(outputs / "b/cover.avif"),
                  "900x1400+40+40", "x1000"),
                 (str(sources / "decline.png"), str(outputs / "decline.avif"))]

        def encode(extra=(), report=None, **environment):
            for name, value in environment.items():
                monkeypatch.setenv(name, value)
            workdir = tmp_path / "work"
            workdir.mkdir()
            return av1gpu.encode_chunk(str(tool), items, str(workdir), 60,
                                       "turbo", extra=extra, report=report)

        encode.items = items
        encode.outputs = outputs
        encode.args = tool.parent / "args"
        encode.list = tool.parent / "list"
        encode.threads = tool.parent / "threads"
        return encode

    def test_each_output_reaches_its_own_image_s_destination(self, chunk):
        """Two `cover.jpg` in two folders would land on each other under their
        own names; numbered, each comes back to where it belongs."""
        done, status = chunk()
        assert (done, status) == ([0, 1], 0)
        assert (chunk.outputs / "a/cover.avif").read_text() == "GPU cover.jpg\n"
        assert (chunk.outputs / "b/cover.avif").read_text() == "GPU cover.jpg\n"

    def test_a_blank_page_is_reported_and_is_no_failure(
            self, chunk, tmp_path, capfd):
        """What --skip-blank kept from the encode comes back in the report,
        with the call's count of trims, and is not warned about as an image
        left unwritten: there is nothing to write."""
        (tmp_path / "in" / "blank.jpg").write_text("blank")
        chunk.items.append((str(tmp_path / "in" / "blank.jpg"),
                            str(chunk.outputs / "blank.avif")))
        report: dict = {}
        done, status = chunk(extra=["--trim", "--skip-blank", "99"],
                             report=report)
        assert (done, status) == ([0, 1], 0)
        assert report == {"blank": [3], "trimmed": 2}
        assert "--trim" in chunk.args.read_text().split()
        err = capfd.readouterr().err
        assert "left 1 of 4 image(s) unwritten" in err   # decline.png only

    def test_without_the_trim_options_none_are_passed(self, chunk):
        chunk()
        assert "--trim" not in chunk.args.read_text().split()

    def test_what_it_skipped_is_not_claimed(self, chunk):
        done, _ = chunk()
        assert 2 not in done
        assert not (chunk.outputs / "decline.avif").exists()

    def test_what_it_skipped_is_kept_and_named_by_its_book(
            self, chunk, tmp_path, monkeypatch, capfd):
        """A page left unwritten is warned about and kept in the error log,
        named by the comic it was unpacked from, with what av1gpu said."""
        monkeypatch.setenv("CLI_SCRIPT_DIR", str(tmp_path / "home"))
        monkeypatch.setattr(errorlog, "_ORIGINS", {})
        errorlog.note_origin(str(tmp_path / "in"), str(tmp_path / "Book.cbz"))
        chunk()
        err = capfd.readouterr().err
        assert "WARNING: av1gpu left 1 of 3 image(s) unwritten" in err
        assert "of Book.cbz - they go to the CPU" in err
        (kept,) = (tmp_path / "home" / "logs" / errorlog.ERROR_DIR).iterdir()
        text = kept.read_text(encoding="utf-8")
        assert "WARNING  tool-skipped" in text
        assert "made from %s" % (tmp_path / "Book.cbz") in text
        assert "  | skipped decline.png: not decodable" in text
        assert "  | batch finished" in text     # its stdout too

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
            "--tiles", "2,2,2", "--yuv", "420", "--depth", "10",
            "--max-mp", "100", "--max-input-mp", "100"]

    def test_the_crop_and_shrink_ride_on_their_image_s_line(self, chunk):
        chunk()
        lines = [line.split("\t") for line in
                 chunk.list.read_text().splitlines()]
        assert [line[1:] for line in lines] == [
            [], ["crop=900x1400+40+40", "max-size=x1000"], []]
        assert lines[1][0].endswith("/000001.jpg")

    def test_png_and_webp_are_decoded_on_more_threads_than_its_own(
            self, chunk, monkeypatch):
        monkeypatch.delenv(av1gpu.DECODE_THREADS_VARIABLE, raising=False)
        chunk()
        assert chunk.threads.read_text() == "8\n"

    def test_a_number_the_environment_names_is_kept(self, chunk):
        chunk(GP_FALLBACK_WORKERS="3")
        assert chunk.threads.read_text() == "3\n"
