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


@pytest.fixture(autouse=True)
def plain_lines(monkeypatch):
    monkeypatch.delenv("LOG_TIMESTAMPS", raising=False)


def test_a_successful_encode_says_nothing(capfd):
    run_module.run_quiet_encode(_encoder(CHATTER), "film.mkv")
    assert capfd.readouterr() == ("", "")


def test_a_failure_is_replayed_with_its_reason(capfd):
    run_module.run_quiet_encode(_encoder(CHATTER + TROUBLE, status=1),
                                "film.mkv (chunk 2 of 9)")
    err = capfd.readouterr().err
    assert err.startswith(
        "==> ERROR: film.mkv (chunk 2 of 9): ffmpeg exited with status 1\n")
    replayed = [line[len("    | "):] for line in err.splitlines()
                if line.startswith("    | ")]
    assert replayed == CHATTER + TROUBLE


def test_the_exit_status_is_the_encoder_own(capfd):
    assert run_module.run_quiet_encode(_encoder(CHATTER, status=69)) == 69
