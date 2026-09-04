"""What the encoder library is allowed to say while an encode runs.

libsvtav1 writes straight to stderr, ignoring ffmpeg's -loglevel, once per
encoder instance - so a chunked AV1 encode repeats its banner and resolved
configuration once per chunk, over the top of the status row. Every video
encode goes through ``run_quiet_encode``, which is where that is dropped; what
is pinned here is that the drop is narrow - anything the library says about a
FAILURE, and everything ffmpeg itself says, still reaches the console.
"""

import sys

import pytest

from medialib.cli import convert_video_run as run_module

pytestmark = pytest.mark.stubbed

# Real lines, so the prefixes are the library's own rather than a guess at them.
CHATTER = [
    "Svt[info]: -------------------------------------------",
    "Svt[info]: SVT [version]:\tSVT-AV1 Encoder Lib v2.1.0",
    "Svt[warn]: Failed to set thread priority",
]
TROUBLE = [
    "Svt[error]: Instance 1: Invalid film grain denoise value",
    "[libsvtav1 @ 0x55f] Error parsing option 'tune' with value '9'.",
]


def _encoder(lines, status=0):
    """A stand-in encoder: it says those lines on stderr and exits."""
    script = "import sys\n" + "".join(
        "sys.stderr.write(%r)\n" % (line + "\n") for line in lines
    ) + "sys.exit(%d)\n" % status
    return [sys.executable, "-c", script]


def test_the_library_chatter_is_dropped(capfd):
    run_module.run_quiet_encode(_encoder(CHATTER))
    assert capfd.readouterr().err == ""


def test_a_failure_is_not(capfd):
    run_module.run_quiet_encode(_encoder(CHATTER + TROUBLE, status=1))
    assert capfd.readouterr().err.splitlines() == TROUBLE


def test_the_exit_status_is_the_encoder_own(capfd):
    assert run_module.run_quiet_encode(_encoder(CHATTER, status=69)) == 69
