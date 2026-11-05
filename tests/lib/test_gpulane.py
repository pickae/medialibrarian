"""The white box for medialib/lib/gpulane.py.

A book is handed back once every page of it has an output - from the card, or
from its fallback call for a page the card did not write - and an encoder that
cannot start sends every page to its fallback. The encoder is the same kind of
stand-in medialib/lib/av1gpu.py is tested with.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from medialib.lib import gpulane

pytestmark = pytest.mark.fs

_ENCODER = r'''#!/usr/bin/env bash
while [ $# -gt 0 ]; do
    case "$1" in --batch) list=$2 ;; --outdir) out=$2 ;; esac; shift
done
echo called >> "$(dirname "$0")/calls"
[ -n "$STUB_STATUS" ] && exit "$STUB_STATUS"
while IFS= read -r input; do
    target=$(readlink "$input")
    case "${target##*/}" in decline*) continue ;; esac
    echo "GPU" > "$out/${input##*/}.avif"
done < "$list"
'''


@pytest.fixture
def lane_run(tmp_path, monkeypatch):
    tool = tmp_path / "bin" / "av1gpu"
    tool.parent.mkdir()
    tool.write_text(_ENCODER)
    tool.chmod(0o755)
    root = tmp_path / "lane"
    root.mkdir()

    def run(books, **environment):
        for name, value in environment.items():
            monkeypatch.setenv(name, value)
        finished = []
        lock = threading.Lock()

        def finish(item):
            with lock:
                finished.append(item)

        lane = gpulane.Lane(str(tool), str(root), 60, "turbo", "444",
                            finish, 2, lambda line: None)
        for book, pages in books.items():
            folder = lane.folder()
            with open(f"{folder}/{gpulane.MANIFEST}", "w") as manifest:
                for page in pages:
                    source = tmp_path / page
                    source.write_text(page)
                    output = tmp_path / (page + ".avif")
                    # One named cpunone* is encoded by a CPU encoder that
                    # exits 0 and writes nothing.
                    script = ("exit 0" if page.startswith("cpunone")
                              else 'echo CPU > "$0"')
                    entry = {"output": str(output), "pixels": 1_000_000,
                             "cpu": ["sh", "-c", script, str(output)]}
                    # A page named cpu* is listed for the CPU alone.
                    if not page.startswith("cpu"):
                        entry["input"] = str(source)
                    manifest.write(json.dumps(entry) + "\n")
            open(f"{folder}/{gpulane.READY}", "w").close()
            lane.expect(folder, book)
        lane.close()
        outputs = {path.name[:-len(".avif")]: path.read_text().strip()
                   for path in tmp_path.glob("*.avif")}
        calls = tool.parent / "calls"
        called = len(calls.read_text().split()) if calls.exists() else 0
        return sorted(finished), outputs, called, lane

    return run


def test_every_book_comes_back_once_all_its_pages_have_outputs(lane_run):
    finished, outputs, called, lane = lane_run(
        {"one": ["p1.jpg", "decline.jpg"], "two": ["p2.jpg"]})
    assert finished == ["one", "two"]
    assert outputs == {"p1.jpg": "GPU", "p2.jpg": "GPU", "decline.jpg": "CPU"}
    assert 1 <= called <= gpulane.CALLS
    assert (lane.pages, lane.fallbacks) == (2, 1)


def test_an_encoder_that_cannot_start_sends_it_all_to_the_cpu(lane_run):
    finished, outputs, called, _ = lane_run(
        {"one": ["p1.jpg"], "two": ["p2.jpg", "p3.jpg"]}, STUB_STATUS="1")
    assert finished == ["one", "two"]
    assert set(outputs.values()) == {"CPU"} and len(outputs) == 3
    assert called >= 1


def test_a_book_with_nothing_for_the_card_comes_back_at_once(lane_run):
    finished, outputs, called, _ = lane_run({"empty": []})
    assert finished == ["empty"] and not outputs and called == 0


def test_a_page_for_the_cpu_alone_is_encoded_there(lane_run, tmp_path):
    """A manifest line without an input for the card is the CPU's from the
    start."""
    finished, outputs, _called, lane = lane_run(
        {"one": ["p1.jpg", "cpu_big.png"]})
    assert finished == ["one"]
    assert outputs == {"p1.jpg": "GPU", "cpu_big.png": "CPU"}
    assert lane.cpu_pages == 1


def test_both_sides_are_timed_for_their_throughput(lane_run):
    """The closing report's MP/s is pixels over the time a side was working:
    the card's calls, and the CPU's encoders."""
    _finished, _outputs, _called, lane = lane_run(
        {"one": ["p1.jpg", "cpu_big.png", "cpu_other.png"]})
    assert lane.busy > 0 and lane.pixels == 1_000_000
    assert lane.cpu_busy > 0 and lane.cpu_pixels == 2_000_000
    assert lane._cpu_running == 0


def test_a_page_the_cpu_wrote_nothing_for_is_missing_from_its_book(
        lane_run, capfd):
    """Its book still comes back, so the run can pack the rest - and say how
    many of its pages are missing, which the lane keeps by book folder."""
    finished, outputs, _called, lane = lane_run(
        {"one": ["p1.jpg", "cpunone.png"]})
    assert finished == ["one"]
    assert outputs == {"p1.jpg": "GPU"}
    assert list(lane.missing.values()) == [1]
    assert "exited 0 but wrote no image" in capfd.readouterr().err


def test_a_page_is_encoded_while_its_book_is_still_being_sorted(tmp_path):
    """The lane takes each page as its line is written, not when the book's
    run exits; the book itself comes back only after READY. A line still being
    written is left for the next look."""
    tool = tmp_path / "av1gpu"
    tool.write_text(_ENCODER)
    tool.chmod(0o755)
    root = tmp_path / "lane"
    root.mkdir()
    finished = []
    lane = gpulane.Lane(str(tool), str(root), 60, "turbo", "444",
                        finished.append, 1, lambda line: None)
    folder = lane.folder()
    lane.expect(folder, "book")

    def line(page):
        source, output = tmp_path / page, tmp_path / (page + ".avif")
        source.write_text(page)
        return json.dumps({"input": str(source), "output": str(output),
                           "pixels": 1_000_000,
                           "cpu": ["sh", "-c", 'echo CPU > "$0"', str(output)]})

    def wait_for(condition):
        deadline = time.time() + 10
        while not condition() and time.time() < deadline:
            time.sleep(0.05)
        return condition()

    second = line("p2.jpg")
    with open(f"{folder}/{gpulane.MANIFEST}", "w") as manifest:
        manifest.write(line("p1.jpg") + "\n" + second[:10])
    assert wait_for(lambda: (tmp_path / "p1.jpg.avif").exists())
    assert not (tmp_path / "p2.jpg.avif").exists() and finished == []
    with open(f"{folder}/{gpulane.MANIFEST}", "a") as manifest:
        manifest.write(second[10:] + "\n")
    assert wait_for(lambda: (tmp_path / "p2.jpg.avif").exists())
    assert finished == []
    open(f"{folder}/{gpulane.READY}", "w").close()
    lane.close()
    assert finished == ["book"]
    assert (tmp_path / "p2.jpg.avif").read_text().strip() == "GPU"
