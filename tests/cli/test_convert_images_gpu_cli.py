"""`convert-images` as a process, with a GPU encoder on PATH.

What is under test is the split: which images the card is given, that every one
it does not write is encoded on the CPU after it, that nothing is encoded twice,
and that the runs which cannot use the card never call it. ImageMagick and
av1gpu are both stand-ins that stamp each output with who wrote it.
"""

from __future__ import annotations

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
case "$*" in *"%A %z %w %h %[orientation]"*) ;; *) echo "1000 1500"; exit 0 ;; esac
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
echo "CPU ${src##*/}" > "$out"
'''

# Batch mode: each listed link becomes <outdir>/<link name>.avif naming the file
# it points at, except a target called decline*, which it skips. $STUB_STATUS
# makes it an encoder that cannot start: nothing written, a failing status.
_AV1GPU = '''
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


@pytest.fixture
def run(sandbox, tmp_path):
    sandbox.with_tool("identify", _IDENTIFY)
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
        "flat.avif": "GPU",
        "alpha.avif": "CPU",        # transparency
        "deep.avif": "CPU",         # 16 bits a sample
        "scan.avif": "CPU",         # a format av1gpu does not read
        "decline.avif": "CPU",      # given to it, and not written
    }
    assert called == 1


def test_the_report_gives_each_road_its_speed(run):
    _, _, output = run()
    assert "Converted 8 images" in output
    assert re.search(r"^GPU: 4 image\(s\), 6 MP in [0-9.]+ seconds = "
                     r"[0-9.]+ MP/s$", output, re.M)
    assert re.search(r"^CPU: 4 image\(s\), 6 MP in [0-9.]+ seconds = "
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


@pytest.mark.parametrize("options,environment", [
    (("-u", "444"), {}),
    (("-e", "webp"), {}),
    (("-c",), {}),
    ((), {"imageGpuEncoder": ""}),
], ids=["full-chroma", "webp", "crop", "turned-off"])
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
