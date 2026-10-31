"""The white box for medialib/lib/gpulane.py.

A book is handed back once every page of it has an output - from the card, or
from its fallback call for a page the card did not write - and an encoder that
cannot start sends every page to its fallback. The encoder is the same kind of
stand-in medialib/lib/av1gpu.py is tested with.
"""

from __future__ import annotations

import json
import threading

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
                    entry = {"output": str(output), "pixels": 1_000_000,
                             "cpu": ["sh", "-c", 'echo CPU > "$0"',
                                     str(output)]}
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
