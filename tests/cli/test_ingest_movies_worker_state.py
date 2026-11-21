"""What a worker process is handed survives the trip.

Under forkserver or spawn (the default from Python 3.14) a worker is started
from a pickled copy of what it is handed, and the whisper lane hands it the
run itself. The run carries the model usages, whose counters hold thread
locks, and with -C the chapter model, whose request slots are a semaphore;
those must not stop the copy.
"""

import functools
import pickle

import pytest

from medialib.cli import ingest_movies_run as run_module
from medialib.lib import aicommentary, aicommentarynames, llmclient, subtitleocr

pytestmark = pytest.mark.pure


def test_the_whisper_lane_s_work_pickles_with_the_run_s_model_usages():
    ocr = subtitleocr.Usage()
    ocr.tally.add(2.0, 40, read=100)
    ocr.spans.add(0.0, 2.0)
    state = run_module.Run(
        script_dir="", ram_root="", skips=None, fragments_file="",
        whisper={"jobs": 1}, whisper_said=[], ocr_usage=ocr,
        commentary_judge_usage=aicommentary.Usage(),
        commentary_names_usage=aicommentarynames.Usage(),
        extras_llm=llmclient.Llm(api="openai", url="http://127.0.0.1:1",
                                 model="m", jobs=2))
    work = functools.partial(run_module._transcribe_one, state, lane="whisper")

    handed = pickle.loads(pickle.dumps(work)).args[0]

    assert handed.ocr_usage.requests == 1
    assert handed.ocr_usage.seconds == 2.0
    # The copy counts on with a lock of its own.
    handed.ocr_usage.tally.add(1.0, 10)
    assert (handed.ocr_usage.requests, ocr.requests) == (2, 1)
