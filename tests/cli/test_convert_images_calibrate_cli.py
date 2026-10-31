"""`convert-images -k` as a process: the calibration's plumbing, end to end.

Every tool is a stand-in, and they are rigged so the answer is known: both
encoders write a file whose size follows the quality asked for - the GPU's a
fifth larger at every quality - and the scorer gives each picture its quality
as its score. The GPU then needs the same quality number for the same score,
and every speed and preset is the same size as every other, so there is
nothing to rank by and every speed takes the anchor preset.
"""

from __future__ import annotations

import os

import pytest

from medialib.lib import encodercalibration

pytestmark = pytest.mark.stubbed

_IDENTIFY = 'echo "False 8 2000 1500 Undefined"'

# The output is the last argument, after a PNG24:/PNG48: prefix. An AVIF encode
# is ten bytes per point of -quality; anything else is a picture of no account.
_CONVERT = '''
out="${!#}"; out="${out#PNG24:}"; out="${out#PNG48:}"
quality=""; prev=""
for a in "$@"; do [ "$prev" = "-quality" ] && quality=$a; prev=$a; done
if [ -n "$quality" ]; then head -c $((quality * 10)) /dev/zero > "$out"
else echo picture > "$out"; fi
'''

_AV1GPU = '''
while [ $# -gt 0 ]; do
    case "$1" in --batch) list=$2 ;; --outdir) out=$2 ;; --quality) q=$2 ;; esac
    shift
done
while IFS= read -r input; do
    head -c $((q * 12)) /dev/zero > "$out/${input##*/}.avif"
done < "$list"
'''

# The decoded picture is named <road>_<setting>_q<quality>_<crop>.
_SCORER = '''
name="${3##*/}"; q="${name#*_q}"; q="${q%%_*}"
echo "Score: $q"
'''


@pytest.fixture
def calibrate(sandbox, tmp_path):
    sandbox.with_tool("identify", _IDENTIFY)
    sandbox.with_tool("convert", _CONVERT)
    sandbox.with_tool("av1gpu", _AV1GPU)
    sandbox.with_tool("ssimulacra2_rs", _SCORER)
    inputs = tmp_path / "in"
    inputs.mkdir()
    for name in ("one.jpg", "two.png"):
        (inputs / name).write_text(name)

    def run(*options, **environment):
        env = dict(os.environ, **{"imageGpuEncoder": "av1gpu", **environment})
        return sandbox.run("convert-images", "-k", *options, inputs,
                           tmp_path / "work", env=env)

    run.inputs = inputs
    run.work = tmp_path / "work"
    return run


def test_the_measurement_is_reported_and_saved(calibrate):
    done = calibrate()
    assert done.returncode == 0, done.stdout + done.stderr
    assert "Calibrating on 4 crops" in done.stdout + done.stderr
    assert "What the GPU is asked for" in done.stdout + done.stderr
    saved = encodercalibration.load()
    assert saved is not None
    assert set(saved.presets) == {2, 3, 4, 5, 6, 7, 8, 9}
    assert saved.gpu_settings(5, 60) == ("turbo", 60)
    assert saved.gpu_settings(9, 35) == ("turbo", 35)


def test_the_input_is_only_read_and_the_work_is_kept(calibrate):
    before = sorted(p.name for p in calibrate.inputs.iterdir())
    calibrate()
    assert sorted(p.name for p in calibrate.inputs.iterdir()) == before
    assert (calibrate.work / "scores.csv").read_text().startswith(
        "road,setting,quality,bits,pixels,ssimulacra2\n")


@pytest.mark.parametrize("option", [("-l", "60"), ("-s", "5"), ("-c",),
                                    ("-e", "webp")])
def test_an_option_it_measures_itself_is_refused(calibrate, option):
    done = calibrate(*option)
    assert done.returncode == 1
    assert "-k measures every speed and quality itself" in done.stderr
    assert encodercalibration.load() is None


def test_without_a_gpu_encoder_there_is_nothing_to_calibrate(calibrate):
    done = calibrate(imageGpuEncoder="")
    assert done.returncode == 1
    assert "no GPU encoder to calibrate" in done.stderr
    assert encodercalibration.load() is None
