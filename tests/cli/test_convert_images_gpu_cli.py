"""`convert-images` as a process, with a GPU encoder on PATH.

What is under test is the split: which images the card is given, that every one
it does not write is encoded on the CPU after it, that nothing is encoded twice,
and that the runs which cannot use the card never call it. ImageMagick and
av1gpu are both stand-ins that stamp each output with who wrote it.
"""

from __future__ import annotations

import json
import os
import re

import pytest

pytestmark = pytest.mark.stubbed

# The GPU's header probe: transparency for any file named alpha*, 16 bits for
# deep*, and one size for all of them. Any other question - the size the speed
# is counted in - gets that size alone. The starved check, which asks it too,
# is stood down by -a.
_IDENTIFY = '''
for a in "$@"; do last="$a"; done
case "$*" in
    *"%@"*) case "${last##*/}" in *margin*) echo "900x1400+50+50" ;;
            *) echo "1000x1500+0+0" ;; esac; exit 0 ;;
    *"%A %z %w %h %[orientation]"*) ;;
    *) echo "1000 1500"; exit 0 ;;
esac
case "${last##*/}" in
    alpha*) echo "True 8 1000 1500 Undefined" ;;
    deep*) echo "False 16 1000 1500 Undefined" ;;
    *) echo "False 8 1000 1500 Undefined" ;;
esac
'''

_CONVERT = '''
src=""
for a in "$@"; do [[ -f "$a" ]] && { src="$a"; break; }; done
out="${!#}"
[ "$out" = "info:" ] && {
    printf "srgb(255,255,255) 100\\n%.0s" 1 2 3 4; exit 0; }
echo "CPU ${src##*/}" > "$out"
'''

# Batch mode: each listed link becomes <outdir>/<link name>.avif naming the file
# it points at, except a target called decline*, which it skips. $STUB_STATUS
# makes it an encoder that cannot start: nothing written, a failing status.
_AV1GPU = '''
echo "$*" >> "$(dirname "$0")/args"
while [ $# -gt 0 ]; do
    case "$1" in --batch) list=$2 ;; --outdir) out=$2 ;; esac; shift
done
echo called >> "$(dirname "$0")/calls"
[ -n "$STUB_STATUS" ] && exit "$STUB_STATUS"
while IFS= read -r input; do
    target=$(readlink "$input")
    case "${target##*/}" in decline*) continue ;; esac
    echo "GPU ${target##*/}" > "$out/${input##*/}.avif"
done < "$list"
'''

_PAGES = ("a/cover.jpg", "b/cover.jpg", "photo.jpeg", "flat.png",
          "alpha.png", "deep.png", "scan.bmp", "decline.jpg")


# The lossless crop: the geometry asked for, and the source copied through.
_JPEGTRAN = '''
echo "$*" >> "$(dirname "$0")/crops"
while [ $# -gt 1 ]; do
    case "$1" in -outfile) out=$2; shift ;; esac; shift
done
cp "$1" "$out"
'''


@pytest.fixture
def run(sandbox, tmp_path):
    sandbox.with_tool("identify", _IDENTIFY)
    sandbox.with_tool("jpegtran", _JPEGTRAN)
    sandbox.with_tool("convert", _CONVERT)
    sandbox.with_tool("av1gpu", _AV1GPU)
    sandbox.with_tool("fdupes", "exit 0")
    inputs, outputs = tmp_path / "in", tmp_path / "out"
    for name in _PAGES:
        (inputs / name).parent.mkdir(parents=True, exist_ok=True)
        (inputs / name).write_text(name)

    def convert(*options, **environment):
        env = dict(os.environ, **{"imageGpuEncoder": "av1gpu", **environment})
        calls = sandbox.bin / "calls"
        calls.unlink(missing_ok=True)
        done = sandbox.run("convert-images", "-a", *options, inputs, outputs,
                           env=env)
        assert done.returncode == 0, done.stdout + done.stderr
        written = {str(p.relative_to(outputs)): p.read_text().split()[0]
                   for p in outputs.rglob("*.avif")}
        called = len(calls.read_text().split()) if calls.exists() else 0
        return written, called, done.stdout + done.stderr

    return convert


def test_what_the_card_can_do_it_does_and_the_cpu_does_the_rest(run):
    written, called, _ = run()
    assert written == {
        "a/cover.avif": "GPU", "b/cover.avif": "GPU", "photo.avif": "GPU",
        "flat.avif": "GPU", "deep.avif": "GPU",
        "alpha.avif": "CPU",        # transparency
        "scan.avif": "CPU",         # a format av1gpu does not read
        "decline.avif": "CPU",      # given to it, and not written
    }
    assert called == 1


def test_the_report_gives_each_road_its_speed(run):
    _, _, output = run()
    assert "Converted 8 images" in output
    assert re.search(r"^GPU: 5 image\(s\), 8 MP in [0-9.]+ seconds = "
                     r"[0-9.]+ MP/s$", output, re.M)
    assert re.search(r"^CPU: 3 image\(s\), 4 MP in [0-9.]+ seconds = "
                     r"[0-9.]+ MP/s$", output, re.M)


def test_a_run_without_the_card_reports_the_cpu_alone(run):
    _, _, output = run(imageGpuEncoder="")
    assert "CPU: 8 image(s), 12 MP" in output
    assert "GPU:" not in output


def test_an_encoder_that_cannot_start_leaves_it_all_to_the_cpu(run):
    written, called, output = run(STUB_STATUS="1")
    assert set(written.values()) == {"CPU"} and len(written) == len(_PAGES)
    assert called == 1
    assert "encoded on the CPU" in output


def test_full_chroma_is_asked_of_the_card_as_full_chroma(run, sandbox):
    written, called, _ = run("-u", "444")
    assert written["photo.avif"] == "GPU" and called == 1
    assert "--yuv 444 --depth 10" in (sandbox.bin / "args").read_text()


@pytest.mark.parametrize("options,environment", [
    (("-e", "webp"), {}),
    (("-c",), {}),
    ((), {"imageGpuEncoder": ""}),
], ids=["webp", "crop", "turned-off"])
def test_a_run_the_card_cannot_serve_never_asks_it(run, options, environment):
    written, called, _ = run(*options, **environment)
    assert called == 0
    assert set(written.values()) <= {"CPU"}


def test_a_picture_m_would_shrink_stays_on_the_cpu(run):
    written, called, _ = run("-m", "1000")
    assert set(written.values()) == {"CPU"}
    assert called == 0


def test_a_rerun_finds_the_card_s_work_already_done(run):
    run()
    written, called, output = run()
    assert called == 0
    assert "already done: 8" in output


def test_a_relative_input_and_output_are_where_the_caller_meant(sandbox,
                                                                tmp_path):
    """Both are resolved before the run moves into the input folder; resolved
    after it, they pointed inside it and every image "converted" to nowhere."""
    sandbox.with_tool("identify", _IDENTIFY)
    sandbox.with_tool("convert", _CONVERT)
    sandbox.with_tool("fdupes", "exit 0")
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "photo.jpg").write_text("photo")
    done = sandbox.run("convert-images", "-a", "in", "out", cwd=tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert (tmp_path / "out" / "photo.avif").read_text() == "CPU photo.jpg\n"


def test_under_a_lane_the_pages_are_sorted_and_none_encoded(run, tmp_path):
    """The run that set the lane encodes: this one never calls the card nor
    ImageMagick's encoder. Each page the card can encode as it is - a PNG
    or WebP as well as a JPEG - is listed with its input for the card;
    everything else with the CPU call alone."""
    lane = tmp_path / "lane"
    lane.mkdir()
    written, called, _ = run("-c", imageGpuLane=str(lane))
    assert called == 0 and written == {}
    lines = [json.loads(line)
             for line in (lane / "manifest").read_text().splitlines()]
    entries = {os.path.relpath(entry["output"], tmp_path / "out"): entry
               for entry in lines}
    assert sorted(name for name, entry in entries.items()
                  if "input" in entry) == [
        "a/cover.avif", "b/cover.avif", "decline.avif", "deep.avif",
        "flat.avif", "photo.avif"]
    assert sorted(name for name, entry in entries.items()
                  if "input" not in entry) == ["alpha.avif", "scan.avif"]
    assert entries["flat.avif"]["input"] == str(tmp_path / "in/flat.png")
    assert entries["photo.avif"]["input"] == str(tmp_path / "in/photo.jpeg")
    assert entries["photo.avif"]["pixels"] == 1_500_000
    assert entries["photo.avif"]["cpu"][-1] == entries["photo.avif"]["output"]


def test_under_a_lane_a_page_taller_than_m_is_the_cpu_s(run, tmp_path):
    lane = tmp_path / "lane"
    lane.mkdir()
    written, _, _ = run("-c", "-m", "1000", imageGpuLane=str(lane))
    lines = [json.loads(line)
             for line in (lane / "manifest").read_text().splitlines()]
    assert written == {} and len(lines) == 8
    assert not [line for line in lines if "input" in line]


def test_under_a_lane_a_trimmed_jpeg_is_cropped_losslessly_for_the_card(
        run, sandbox, tmp_path):
    """The trim's box, widened by the margin the CPU's border would give
    back, is cut out of the JPEG by jpegtran, and that crop is what the card
    is given."""
    lane = tmp_path / "lane"
    lane.mkdir()
    (tmp_path / "in" / "margin.jpg").write_text("margin.jpg")
    run("-c", imageGpuLane=str(lane))
    assert "-crop 920x1420+40+40" in (sandbox.bin / "crops").read_text()
    entry = next(entry for entry in (
        json.loads(line) for line in (lane / "manifest").read_text().splitlines())
        if entry["output"].endswith("/margin.avif"))
    assert entry["input"].startswith(str(lane)) and entry["input"].endswith(
        ".jpg")
    with open(entry["input"]) as cropped:
        assert cropped.read() == "margin.jpg"


def test_under_a_lane_a_trimmed_png_is_cut_by_imagemagick_for_the_card(
        run, sandbox, tmp_path):
    """A PNG has no lossless crop of its own: ImageMagick cuts it exactly as
    the CPU's trim would, into a PNG in the lane, and that is what the card
    is given."""
    lane = tmp_path / "lane"
    lane.mkdir()
    (tmp_path / "in" / "margin.png").write_text("margin.png")
    (tmp_path / "in" / "alpha-margin.png").write_text("alpha-margin.png")
    run("-c", imageGpuLane=str(lane))
    entries = {os.path.basename(entry["output"]): entry for entry in (
        json.loads(line) for line in (lane / "manifest").read_text().splitlines())}
    entry = entries["margin.avif"]
    assert entry["input"].startswith(str(lane)) and entry["input"].endswith(
        ".png")
    with open(entry["input"]) as cropped:
        assert cropped.read() == "CPU margin.png\n"
    # One the card would not take uncut is not cut for it.
    assert "input" not in entries["alpha-margin.avif"]
    assert "-crop" in " ".join(entries["alpha-margin.avif"]["cpu"])
