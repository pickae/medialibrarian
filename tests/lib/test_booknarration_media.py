"""Tier D for the voice sample the narration clones from.

Every question here is about the SAMPLES, so no stub can answer one: whether a
rewritten sample really came out mono at the model's rate, whether the slice
taken out of a long recording really holds speech rather than the silence that
happened to sit in the middle of it, and whether a purpose-made minute survives
uncut. So this tier encodes real audio and measures what comes back.

The over-long fixture is the one worth reading twice: 115 s of tone, 10 s of
silence, 115 s of tone. The midpoint of the recording sits inside the silence, so
"take the middle" without looking would hand the cloning ten seconds of nothing.
"""

import os
import shutil
import subprocess

import pytest

from medialib.lib import booknarration as bn

pytestmark = [
    pytest.mark.media,
    pytest.mark.skipif(shutil.which("ffmpeg") is None
                       or shutil.which("ffprobe") is None,
                       reason="tier D needs a real ffmpeg and ffprobe"),
]

RATE = "24000"
CODEC = "pcm_s16le"
WANTED = 45.0          # voiceSampleSeconds
MAX_WHOLE = 60.0       # voiceSampleMaxSeconds


def _ffmpeg(*args):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *map(str, args)],
                   check=True, stdin=subprocess.DEVNULL)


def _property(path, entry):
    done = subprocess.run(
        ["ffprobe", "-v", "quiet", "-select_streams", "a:0", "-show_entries",
         "stream=" + entry, "-of", "default=nk=1:nw=1", "--", str(path)],
        capture_output=True, text=True)
    return done.stdout.splitlines()[0] if done.stdout.strip() else ""


def _seconds(path):
    done = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "default=nk=1:nw=1", "--", str(path)],
        capture_output=True, text=True)
    return round(float(done.stdout.strip() or 0))


def _mean_volume(path):
    """Pure silence measures around -91 dB, tone around -20."""
    done = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af",
         "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL)
    for line in done.stderr.splitlines():
        if "mean_volume" in line:
            return float(line.split()[-2])
    raise AssertionError("ffmpeg reported no mean_volume for %s" % path)


@pytest.fixture(autouse=True)
def own_scratch(tmp_path, monkeypatch):
    """Keep the scratch out of the machine-wide /dev/shm, so what these assert
    is about the run and not about the host."""
    monkeypatch.setenv("ramBase", str(tmp_path))


@pytest.fixture
def tone(tmp_path):
    def make(name, seconds=8, channels=1, rate=RATE, codec=CODEC, freq=200):
        path = tmp_path / name
        codec_args = [] if codec is None else ["-c:a", codec]
        _ffmpeg("-f", "lavfi",
                "-i", "sine=frequency=%d:duration=%s" % (freq, seconds),
                "-ac", channels, "-ar", rate, *codec_args, path)
        return path
    return make


@pytest.fixture
def spliced(tmp_path):
    """Tone, silence, tone - concatenated, so the silence sits where the case
    wants it."""
    def make(name, first, gap, second):
        path = tmp_path / name
        _ffmpeg("-f", "lavfi", "-i", "sine=frequency=200:duration=%s" % first,
                "-f", "lavfi",
                "-i", "anullsrc=r=%s:cl=mono:duration=%s" % (RATE, gap),
                "-f", "lavfi", "-i", "sine=frequency=300:duration=%s" % second,
                "-filter_complex",
                "[0:a][1:a][2:a]concat=n=3:v=0:a=1[out]", "-map", "[out]",
                "-ac", "1", "-ar", RATE, "-c:a", CODEC, path)
        return path
    return make


class TestASampleThatIsAlreadyWhatCloningWants:
    def test_a_short_pcm_wav_is_handed_over_untouched(self, tone, tmp_path):
        short = tone("short.wav")
        assert bn.prepare_voice_sample(str(short), str(tmp_path)) == str(short)
        assert not (tmp_path / "voiceSample.wav").exists()


class TestASampleInTheWrongFormat:
    """Stereo, 44.1 kHz, lossy - the shape of a voice note or a podcast clip,
    which is what a user actually has to hand."""

    def test_it_is_rewritten_to_the_models_own_shape(self, tone, tmp_path):
        wrong = tone("short.mp3", channels=2, rate="44100", codec=None)
        made = bn.prepare_voice_sample(str(wrong), str(tmp_path))
        assert made == str(tmp_path / "voiceSample.wav")
        assert _property(made, "channels") == "1"
        assert _property(made, "codec_name") == CODEC
        assert _property(made, "sample_rate") == RATE
        assert _seconds(made) == 8


class TestAnOverLongSampleWithTheSilenceInTheMiddle:
    @pytest.fixture
    def long_sample(self, spliced):
        path = spliced("long.wav", 115, 10, 115)
        assert _seconds(path) == 240
        return path

    def test_the_window_is_the_wanted_length(self, long_sample):
        start, length = bn.voice_sample_window(str(long_sample), 240).split()
        assert round(float(length)) == WANTED

    def test_and_it_avoids_the_silence(self, long_sample):
        start, length = (float(x) for x in
                         bn.voice_sample_window(str(long_sample), 240).split())
        assert start + length <= 115 or start >= 125

    def test_the_slice_holds_speech_and_not_room_tone(self, long_sample,
                                                      tmp_path):
        cut = bn.prepare_voice_sample(str(long_sample), str(tmp_path))
        assert cut == str(tmp_path / "voiceSample.wav")
        assert _seconds(cut) == WANTED
        assert _mean_volume(cut) > -60


class TestASampleOfUpToAMinuteIsHandedOverWhole:
    """A purpose-made sample is exactly this: someone reading for a minute,
    pauses and all. Cutting into one only throws away what its maker chose."""

    def test_even_with_a_long_silence_sitting_in_the_middle(self, spliced,
                                                            tmp_path):
        whole = spliced("whole.wav", 24, 6, 25)
        assert _seconds(whole) == 55
        assert _seconds(whole) < MAX_WHOLE
        assert bn.prepare_voice_sample(str(whole), str(tmp_path)) == str(whole)
        assert not (tmp_path / "voiceSample.wav").exists()


class TestThePausesOfOrdinarySpeechStayInsideTheSlice:
    """Three minutes of someone talking with a breath every ten seconds. Every
    breath is a "silence"; none is long enough to end the stretch, so the slice
    is a full-length one that contains them - not the ten-second fragment
    between two of them."""

    @pytest.fixture
    def spoken(self, tmp_path):
        path = tmp_path / "spoken.wav"
        _ffmpeg("-f", "lavfi", "-i", "sine=frequency=220:duration=180",
                "-af", "volume=enable='lt(mod(t,10),0.6)':volume=0",
                "-ac", "1", "-ar", RATE, "-c:a", CODEC, path)
        return path

    def test_the_window_is_still_a_full_one(self, spoken):
        _start, length = bn.voice_sample_window(str(spoken), 180).split()
        assert round(float(length)) == WANTED

    def test_and_it_really_comes_out_that_long_with_the_talking_in_it(
            self, spoken, tmp_path):
        made = bn.prepare_voice_sample(str(spoken), str(tmp_path))
        assert _seconds(made) == WANTED
        assert _mean_volume(made) > -60


class TestNothingReadable:
    def test_a_file_with_no_audio_in_it_is_refused(self, tmp_path):
        (tmp_path / "notaudio.wav").write_text("not audio at all")
        assert bn.prepare_voice_sample(str(tmp_path / "notaudio.wav"),
                                       str(tmp_path)) is None

    def test_and_so_is_one_that_is_not_there(self, tmp_path):
        assert bn.prepare_voice_sample(str(tmp_path / "missing.wav"),
                                       str(tmp_path)) is None


class TestOneVoicePerLanguage:
    """A directory of samples is prepared once, into one entry per language, and
    every entry is a file this run made on its own tmpfs - a user's own path may
    hold a tab, which is what the map is separated by."""

    @pytest.fixture
    def prepared(self, tone, tmp_path, monkeypatch):
        voices = tmp_path / "voices"
        voices.mkdir()
        tone("voices/deu.wav")
        tone("voices/french.mp3", channels=2, rate="44100", codec=None,
             freq=300)
        tone("voices/default.wav", freq=400)
        (voices / "broken.eng.wav").write_text("not audio")
        samples = tmp_path / "samples"
        samples.mkdir()
        made = bn.prepare_voice_samples(str(voices), str(samples))
        monkeypatch.setenv("narrationVoiceMap", made or "")
        return samples, made

    def test_one_entry_per_usable_sample(self, prepared):
        _samples, made = prepared
        assert len([line for line in (made or "").splitlines() if line]) == 3

    def test_the_german_book_gets_the_german_sample(self, prepared):
        samples, _made = prepared
        german = bn.voice_sample_for("deu")
        assert german == str(samples / "voices" / "deu" / "voiceSample.wav")
        assert _property(german, "codec_name") == CODEC

    def test_the_french_one_was_transcoded_on_its_way_in(self, prepared):
        samples, _made = prepared
        french = bn.voice_sample_for("fra")
        assert french == str(samples / "voices" / "fra" / "voiceSample.wav")
        assert _property(french, "channels") == "1"
        assert _property(french, "sample_rate") == RATE

    def test_a_language_with_no_sample_falls_back(self, prepared):
        samples, _made = prepared
        assert bn.voice_sample_for("ita") == \
            str(samples / "voices" / "-" / "voiceSample.wav")

    def test_a_sample_with_no_audio_in_it_is_left_out(self, prepared):
        samples, _made = prepared
        assert not os.path.exists(str(samples / "voices" / "eng"))
