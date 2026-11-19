"""The white box for medialib/lib/gpulane.py.

A book is handed back once every page of it has an output - from the card, or
from its fallback call for a page the card did not write - and an encoder that
cannot start sends every page to its fallback. The encoder is the same kind of
stand-in medialib/lib/av1gpu.py is tested with.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import traceback

import pytest

from medialib.lib import gpulane

pytestmark = pytest.mark.fs

# The stand-in encoder is a shell script, handed its pages as symlinks:
# Windows can start neither without privileges a test run does not have.
_STUB_RUNS = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the stand-in encoder is a shell script fed symlinks")


def _close(lane, seconds=120):
    """lane.close(), failed rather than waited for should it hang. The
    deadline is generous: a CPU encode is a process start-up, slow on a
    machine every worker of a parallel run is loading."""
    closer = threading.Thread(target=lane.close, daemon=True)
    closer.start()
    closer.join(seconds)
    if not closer.is_alive():
        return
    # Where each lane thread is stuck is what such a failure has to say.
    frames = sys._current_frames()
    stuck = "\n".join(
        "%s:\n%s" % (thread.name,
                     "".join(traceback.format_stack(frames[thread.ident])))
        for thread in lane.threads if thread.ident in frames)
    pytest.fail("the lane did not close in %d s:\n%s" % (seconds, stuck))

_ENCODER = r'''#!/usr/bin/env bash
trim=
while [ $# -gt 0 ]; do
    case "$1" in --batch) list=$2 ;; --outdir) out=$2 ;; --trim) trim=1 ;; esac; shift
done
echo "called${trim:+ trim}" >> "$(dirname "$0")/calls"
[ -n "$STUB_STATUS" ] && exit "$STUB_STATUS"
while IFS= read -r input; do
    target=$(readlink "$input")
    case "${target##*/}" in decline*) continue ;; esac
    if [ -n "$trim" ]; then
        case "${target##*/}" in
            blank*) echo "[av1gpu] skipped $input: blank (--skip-blank)" >&2; continue ;;
        esac
    fi
    echo "GPU${trim:+ trimmed}" > "$out/${input##*/}.avif"
done < "$list"
[ -n "$trim" ] && echo "[av1gpu] trim: 1 images trimmed, 0 skipped as blank" >&2
exit 0
'''


@pytest.fixture
def lane_run(tmp_path, monkeypatch):
    if sys.platform == "win32":
        pytest.skip("the stand-in encoder is a shell script fed symlinks")
    tool = tmp_path / "bin" / "av1gpu"
    tool.parent.mkdir()
    tool.write_text(_ENCODER)
    tool.chmod(0o755)
    root = tmp_path / "lane"
    root.mkdir()

    def run(books, resolve=None, **environment):
        for name, value in environment.items():
            monkeypatch.setenv(name, value)
        finished = []
        lock = threading.Lock()

        def finish(item):
            with lock:
                finished.append(item)

        lane = gpulane.Lane(str(tool), str(root), 60, "turbo", "444",
                            finish, 2, lambda line: None,
                            trim=["--trim", "--skip-blank", "99"],
                            resolve=resolve)
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
                    entry = {"output": str(output), "page": page,
                             "pixels": 1_000_000,
                             "cpu": ["sh", "-c", script, str(output)]}
                    # A page named cpu* is listed for the CPU alone, with the
                    # card's own word for why, when it is named for one.
                    if not page.startswith("cpu"):
                        entry["input"] = str(source)
                    elif "alpha" in page:
                        entry["why"] = "it carries an alpha channel"
                    # One named trim* or blank* is the card's to trim.
                    if page.startswith(("trim", "blank")):
                        entry["trim"] = True
                    manifest.write(json.dumps(entry) + "\n")
            open(f"{folder}/{gpulane.READY}", "w").close()
            lane.expect(folder, book)
        _close(lane)
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


def test_the_pages_the_card_trims_go_in_calls_of_their_own(lane_run, tmp_path):
    """One call is given one set of options: the trim's, or none."""
    finished, outputs, _called, lane = lane_run(
        {"one": ["trim1.jpg", "p1.jpg"], "two": ["trim2.jpg"]})
    assert finished == ["one", "two"]
    assert outputs == {"trim1.jpg": "GPU trimmed", "trim2.jpg": "GPU trimmed",
                       "p1.jpg": "GPU"}
    calls = (tmp_path / "bin" / "calls").read_text().splitlines()
    assert "called trim" in calls and "called" in calls
    assert lane.trimmed >= 1


def test_a_page_the_card_found_blank_finishes_its_book_without_output(
        lane_run, capfd):
    """Not a page for the CPU to try again: there is nothing on it - and the
    run says what the call found blank about it, in the call's own words."""
    finished, outputs, _called, lane = lane_run(
        {"one": ["trim1.jpg", "blank.jpg"]}, LOG_VERBOSITY="verbose")
    assert finished == ["one"]
    assert outputs == {"trim1.jpg": "GPU trimmed"}
    assert (lane.blank, lane.fallbacks, lane.cpu_pages) == (1, 0, 0)
    assert "one: blank.jpg - the card dropped it blank: --skip-blank" \
        in capfd.readouterr().err


def test_the_run_says_each_page_s_road_as_the_manifest_lists_it(
        lane_run, capfd):
    """Which side a page was sorted onto, and why the card declined it - from
    the page's own run, which is where the header read happened - and the
    figures a trim verdict was reached on, whichever side reached it."""
    asked = []

    def resolve(entry):
        asked.append(entry["page"])
        return (["sh", "-c", 'echo CPU trimmed > "$0"', entry["output"]],
                "trimmed",
                "trimmed 1000x1500 to 900x1400 - the corners read "
                "srgb(255,255,255) at 100, 10 px of margin given back")

    finished, outputs, _called, lane = lane_run(
        {"one": ["p1.jpg", "cpu_alpha.png", "trim3.jpg"]}, resolve=resolve,
        STUB_STATUS="1", LOG_VERBOSITY="verbose")
    assert finished == ["one"]
    assert sorted(asked) == ["trim3.jpg"]
    err = capfd.readouterr().err
    assert "one: p1.jpg - to the card - 1.0 MP" in err
    assert "one: cpu_alpha.png - to the CPU - it carries an alpha channel" \
        in err
    # trim3 was sorted to the card; the card died, and the CPU decided its
    # trim, on the figures it measured.
    assert "one: trim3.jpg - to the card - 1.0 MP" in err
    assert "one: trim3.jpg - trimmed 1000x1500 to 900x1400" in err


def test_a_page_its_own_run_found_blank_is_done_the_moment_it_is_listed(
        tmp_path, monkeypatch, capfd):
    """A page the run's own trim found blank is listed, not encoded: its book
    is not held for an encode that will not come, and the run says what it was
    found blank on."""
    if sys.platform == "win32":
        pytest.skip("the stand-in encoder is a shell script fed symlinks")
    tool = tmp_path / "bin" / "av1gpu"
    tool.parent.mkdir()
    tool.write_text(_ENCODER)
    tool.chmod(0o755)
    monkeypatch.setenv("LOG_VERBOSITY", "verbose")
    root = tmp_path / "lane"
    root.mkdir()
    finished = []
    lane = gpulane.Lane(str(tool), str(root), 60, "turbo", "444",
                        finished.append, 2, lambda line: None)
    folder = lane.folder()
    source = tmp_path / "p1.jpg"
    source.write_text("p1.jpg")
    manifest = (json.dumps({"input": str(source),
                            "output": str(tmp_path / "p1.jpg.avif"),
                            "page": "p1.jpg", "pixels": 1_000_000,
                            "cpu": ["sh", "-c", 'echo CPU > "$0"',
                                    str(tmp_path / "p1.jpg.avif")]})
                + "\n"
                + json.dumps({"output": str(tmp_path / "blank1.jpg.avif"),
                              "page": "blank1.jpg", "blank": True,
                              "note": ("blank - the trim took 99.5% of the "
                                       "width and 0.0% of the height, past "
                                       "the 99% that leaves a page")})
                + "\n")
    with open(f"{folder}/{gpulane.MANIFEST}", "w") as handle:
        handle.write(manifest)
    open(f"{folder}/{gpulane.READY}", "w").close()
    lane.expect(folder, "one")
    _close(lane)
    assert finished == ["one"]
    assert (tmp_path / "p1.jpg.avif").read_text().strip() == "GPU"
    assert not (tmp_path / "blank1.jpg.avif").exists()
    assert "one: blank1.jpg - blank - the trim took 99.5% of the width" \
        in capfd.readouterr().err


def test_the_cpu_decides_the_trim_of_a_card_page_it_takes(lane_run):
    """When the card cannot start, a page it was to trim is resolved on the
    CPU: the call it answers is run, and a blank one is done without."""
    asked = []

    def resolve(entry):
        asked.append(entry["output"].rpartition("/")[2])
        if "blank" in entry["output"]:
            return None, "blank", "blank - the trim took everything"
        return (["sh", "-c", 'echo CPU trimmed > "$0"', entry["output"]],
                "trimmed", "trimmed 1000x1500 to 900x1400")

    finished, outputs, _called, lane = lane_run(
        {"one": ["trim1.jpg", "blank.jpg", "p1.jpg"]}, resolve=resolve,
        STUB_STATUS="1")
    assert finished == ["one"]
    assert outputs == {"trim1.jpg": "CPU trimmed", "p1.jpg": "CPU"}
    assert sorted(asked) == ["blank.jpg.avif", "trim1.jpg.avif"]
    assert (lane.trimmed, lane.blank) == (1, 1)


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


@_STUB_RUNS
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
    _close(lane)
    assert finished == ["book"]
    assert (tmp_path / "p2.jpg.avif").read_text().strip() == "GPU"


def test_the_cpu_never_takes_the_card_s_pages(lane_run, monkeypatch):
    """However much the card has waiting - here five chunks' worth - its pages
    are its own: an ImageMagick encode of one took the CPU about as long as
    the card takes for a book, for no gain in the run's time."""
    monkeypatch.setattr(gpulane, "CHUNK_MEGAPIXELS", 1)
    pages = ["p%d.jpg" % n for n in range(5)]
    finished, outputs, _called, lane = lane_run({"one": pages})
    assert finished == ["one"]
    assert outputs == {page: "GPU" for page in pages}
    assert lane.cpu_pages == 0


def test_a_book_is_unpacked_for_the_cpu_only_once_the_card_failed(tmp_path):
    lane = gpulane.Lane("", str(tmp_path), 60, "turbo", "444",
                        lambda item: None, 2, lambda line: None)
    try:
        lane._ram_free = lambda: gpulane.RAM_RESERVE + 1
        assert lane.has_room()          # no card: the CPU is the only side
        lane.broken = False
        lane.pending_pixels = gpulane.QUEUED_CHUNKS * lane.budget
        assert not lane.has_room()      # the card has enough; the CPU waits
    finally:
        lane.broken = True
        _close(lane)


def test_the_reserve_scales_to_a_machine_with_less_ram_than_it_keeps_free(
        tmp_path):
    """The reserve is headroom the lane asks to keep free of unpacked books. A
    machine with less RAM than the full reserve could never keep it free - it
    would wait to unpack a book it could never hold - so on such a machine the
    reserve is a share of its RAM, which is always less than the RAM that is
    realistically free. A machine with the full reserve to spare keeps it, and
    so does one whose RAM the lane cannot read."""
    lane = gpulane.Lane("", str(tmp_path), 60, "turbo", "444",
                        lambda item: None, 2, lambda line: None)
    try:
        lane._total_ram_bytes = lambda: 256 * 1024 ** 3
        assert lane._ram_reserve() == gpulane.RAM_RESERVE
        lane._total_ram_bytes = lambda: 8 * 1024 ** 3
        assert (lane._ram_reserve() == 8 * 1024 ** 3
                // gpulane.RAM_RESERVE_SHARE)
        lane._total_ram_bytes = lambda: 0
        assert lane._ram_reserve() == gpulane.RAM_RESERVE
    finally:
        lane.broken = True
        _close(lane)


def _cpu_book(lane, tmp_path, pages):
    """A book of <pages> for the card, each with a CPU encode that writes
    "CPU" to its output - one any platform can run."""
    folder = lane.folder()
    with open(f"{folder}/{gpulane.MANIFEST}", "w") as manifest:
        for page in pages:
            source, output = tmp_path / page, tmp_path / (page + ".avif")
            source.write_text(page)
            manifest.write(json.dumps({
                "input": str(source), "output": str(output),
                "pixels": 1_000_000,
                "cpu": [sys.executable, "-c",
                        "import sys; open(sys.argv[1], 'w').write('CPU')",
                        str(output)]}) + "\n")
    open(f"{folder}/{gpulane.READY}", "w").close()
    return folder


def test_an_encoder_that_cannot_be_run_at_all_sends_it_all_to_the_cpu(
        tmp_path):
    """Not one that starts and fails, but one whose call raises - here, it is
    not there. Its chunk was once left running forever, and close() waited
    for it."""
    finished: list = []
    said: list[str] = []
    root = tmp_path / "lane"
    root.mkdir()
    lane = gpulane.Lane(str(tmp_path / "no-such-encoder"), str(root), 60,
                        "turbo", "444", finished.append, 2, said.append)
    lane.expect(_cpu_book(lane, tmp_path, ["p1.jpg", "p2.jpg"]), "one")
    _close(lane)
    assert finished == ["one"]
    assert lane.broken and not lane.failed
    assert lane.cpu_pages == 2 and not lane.missing
    assert {path.name: path.read_text() for path in tmp_path.glob("*.avif")} \
        == {"p1.jpg.avif": "CPU", "p2.jpg.avif": "CPU"}
    assert any("could not be run" in line for line in said)


def test_a_lane_thread_that_dies_does_not_hang_the_close(tmp_path,
                                                          monkeypatch):
    """Whatever a dead thread held is given up on, said, and the lane still
    closes: the watcher here, with its book never collected."""
    said: list[str] = []

    def collect(_self, _book):
        raise RuntimeError("boom")

    monkeypatch.setattr(gpulane.Lane, "_collect", collect)
    root = tmp_path / "lane"
    root.mkdir()
    lane = gpulane.Lane(str(tmp_path / "no-such-encoder"), str(root), 60,
                        "turbo", "444", lambda item: None, 2, said.append)
    lane.expect(_cpu_book(lane, tmp_path, ["p1.jpg"]), "one")
    _close(lane, seconds=10)
    assert lane.failed
    assert any("lane-watch stopped" in line for line in said)
    assert lane.wait_for_room() == ""
