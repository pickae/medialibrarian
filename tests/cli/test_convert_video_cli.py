"""`convert-video` as a process: what a run decides before it encodes anything.

The heavy tools are stand-ins, and the fixture is an empty .mkv - the exit status
is not what these cases are about. What the startup block REPORTS is: which
encoder tuning it settled on, how many NVENC engines it will parallelise across,
and whether that count was read from the card or guessed blind.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.stubbed

# Everything ordinary a run reaches for. Symlinked into the sandbox rather than
# inherited, because the host HAS nvidia-smi and GPUs - so the scenario can only
# be set by whether this PATH holds one.
_ORDINARY = ("bash", "env", "xargs", "find", "sort", "sed", "awk", "grep",
             "head", "tail", "wc", "cat", "cut", "tr", "uniq", "mktemp", "stat",
             "touch", "date", "basename", "dirname", "realpath", "mkdir",
             "rmdir", "rm", "mv", "cp", "ln", "md5sum", "timeout", "jq",
             "rsync", "nproc", "flock", "sleep")


class TestTheNvencEngineCount:
    """A *Nvenc profile parallelises across the card's NVENC engines, and the
    count is guessed from the GPU model - which needs nvidia-smi.

    Without it the run already degraded to a blind default of two, but nothing
    said the guess was blind rather than model-based: a three-engine card would
    have been under-parallelised without a word.
    """

    @pytest.fixture
    def video(self, sandbox, tmp_path):
        # ffmpeg succeeds at every probe and test encode (that is what the
        # nvenc-works probes are), ffprobe answers a duration.
        sandbox.with_tool("ffmpeg", "exit 0")
        sandbox.with_tool("ffprobe", "echo 1.0")
        sandbox.linking(*_ORDINARY).narrow()
        sandbox.inputs = tmp_path / "in"
        sandbox.inputs.mkdir()
        (sandbox.inputs / "clip.mkv").write_text("")
        return sandbox

    def _run(self, video, *options, count=0):
        outputs = video.work / ("out%d" % count)
        done = video.run("convert-video", *options, video.inputs, outputs,
                         timeout=180)
        return done.stdout + done.stderr

    def test_without_nvidia_smi_the_guess_is_named_as_a_blind_one(self, video):
        log = self._run(video, "-p", "hevcNvenc")
        assert log.count("nvidia-smi is unavailable") == 1
        assert log.count("blind guess of 2") == 1
        assert log.count("Use -e <count>") == 1

    def test_and_the_tuning_line_is_still_stated(self, video):
        """The warning adds to the summary rather than replacing it."""
        log = self._run(video, "-p", "hevcNvenc")
        assert log.count("NVENC tuning: -tune ") == 1

    def test_a_readable_model_is_read_rather_than_guessed_at(self, video):
        """The RTX 5090 is the one model the table knows by name: three
        engines."""
        video.with_tool("nvidia-smi", 'echo "NVIDIA GeForce RTX 5090"')
        log = self._run(video, "-p", "hevcNvenc", count=1)
        assert "nvidia-smi is unavailable" not in log
        assert log.count(
            'across 3 engine(s) (guessed for "NVIDIA GeForce RTX 5090"') == 1

    def test_an_explicit_count_needs_no_model_lookup_at_all(self, video):
        log = self._run(video, "-p", "hevcNvenc", "-e", 3, count=2)
        assert "nvidia-smi is unavailable" not in log
        assert log.count("across 3 engine(s) (from -e)") == 1


@pytest.fixture
def video_cli(sandbox, tmp_path):
    """The host's own PATH, because every claim below fires before the first file
    is touched - the fixture is an empty .mkv and no run reaches an encode."""
    sandbox.inputs = tmp_path / "in"
    sandbox.inputs.mkdir()
    (sandbox.inputs / "clip.mkv").write_text("")
    sandbox.counter = [0]

    def start(*options, **overrides):
        sandbox.counter[0] += 1
        outputs = sandbox.work / ("out%d" % sandbox.counter[0])
        done = sandbox.run("convert-video", *options, sandbox.inputs, outputs,
                           timeout=180, **overrides)
        return done.returncode, done.stdout + done.stderr

    sandbox.start = start
    return sandbox


class TestPairingTheAudioProfileWithItsBitrate:
    """-b pins ONE Opus bitrate on every track, which is exactly what the
    opusCustom profile means - so the two belong together, and the run says so
    rather than encoding at a bitrate nobody chose.

    The subtlety worth its own case: -a defaults to "opus", so a naive check
    cannot tell `-a opus -b 46` (a contradiction - the per-channel table AND a
    fixed bitrate) from a bare `-b 46` (no profile named, which can only mean
    opus at this bitrate). The first is refused, the second promotes the
    untouched default.
    """

    _ACCEPTED = ("Audio profile: opusCustom (46 kbit/s Opus applied to every "
                 "track, surround downmixed to stereo)")

    @pytest.mark.parametrize("options", [["-b", 46],
                                         ["-a", "opusCustom", "-b", 46]],
                             ids=["-b alone", "named outright"])
    def test_the_two_spellings_of_one_run_reach_the_same_place(self, video_cli,
                                                               options):
        _, log = video_cli.start(*options)
        assert self._ACCEPTED in log

    @pytest.mark.parametrize("options,message", [
        (["-a", "opus", "-b", 46],
         "The -b audio bitrate only applies to the opusCustom profile "
         "(got -a opus)."),
        (["-a", "passthrough", "-b", 46],
         "The -b audio bitrate only applies to the opusCustom profile "
         "(got -a passthrough)."),
        (["-a", "opusCustom"],
         "The opusCustom audio profile requires an audio bitrate: "
         "-b <kbit/s>."),
        # The value is validated after the promotion, not instead of it.
        (["-b", 0],
         'The -b audio bitrate must be a positive integer in kbit/s '
         '(got "0").'),
    ], ids=["-a opus with -b", "-a passthrough with -b",
            "opusCustom without -b", "-b that is not a bitrate"])
    def test_and_every_genuine_mistake_is_still_one(self, video_cli, options,
                                                    message):
        status, log = video_cli.start(*options)
        assert status == 1
        assert message in log


class TestFlagsOnlySomeProfilesCanUse:
    """-f and -g are both libsvtav1 settings. Accepting one for an x265 or NVENC
    profile and then ignoring it would encode at settings nobody asked for and
    never say so, so they are refused up front."""

    @pytest.mark.parametrize("options,message", [
        (["-p", "x265BluRay", "-f", 1],
         "The -f fast-decode level only applies to the AV1 software profiles "
         "(got -p x265BluRay, which encodes with libx265)."),
        (["-p", "av1Nvenc", "-f", 1],
         "The -f fast-decode level only applies to the AV1 software profiles "
         "(got -p av1Nvenc, which encodes with av1_nvenc)."),
        (["-f", 3], 'The -f fast-decode level must be 1 or 2 (got "3").'),
        (["-p", "x265Fast", "-g", 10],
         "The -g film grain level only applies to the AV1 software profiles "
         "(got -p x265Fast, which encodes with libx265)."),
        # Including a -g off: a profile that never synthesises grain has none to
        # turn off, so the flag can only be a misunderstanding of the profile.
        (["-p", "hevcNvenc", "-g", "off"],
         "The -g film grain level only applies to the AV1 software profiles "
         "(got -p hevcNvenc, which encodes with hevc_nvenc)."),
        (["-g", 51],
         "The -g film grain level must be off, or an integer between 0 and 50 "
         '- where 0 asks for the per-source probe (got "51").'),
        (["-g", "some"],
         "The -g film grain level must be off, or an integer between 0 and 50 "
         '- where 0 asks for the per-source probe (got "some").'),
        # Refused rather than taken as a synonym, so an invocation asking for
        # "auto" is told to say -g 0 instead of silently getting the default.
        (["-g", "auto"],
         "The -g film grain level must be off, or an integer between 0 and 50 "
         '- where 0 asks for the per-source probe (got "auto").'),
    ], ids=["-f on x265", "-f on nvenc", "-f out of range", "-g on x265",
            "-g off on nvenc", "-g above the scale", "-g a word",
            "-g auto"])
    def test_the_refusal_names_what_is_wrong(self, video_cli, options, message):
        status, log = video_cli.start(*options)
        assert status == 1
        assert message in log

    @pytest.mark.parametrize("options,summary", [
        # Leaving -g out is not refused for those profiles: it is what a run that
        # never mentioned grain looks like, and an x265 run has to start.
        (["-p", "x265Fast"],
         "Film grain: not available with libx265, none synthesised."),
        (["-p", "av1Grain"],
         "Film grain: measured per file and synthesised as measured, the "
         "av1Grain default"),
        (["-p", "av1Grain", "-g", 35],
         "Film grain: level 35 for every file (-g), regardless of source or "
         "profile"),
        (["-p", "av1Grain", "-g", 0],
         "Film grain: -g 0 - measured per file and synthesised as measured"),
        (["-p", "av1Fast"],
         "Film grain: none - the av1Fast default is 0 (-g 0 measures each "
         "source instead)."),
        # Animation opts out of the measurement rather than merely defaulting
        # low: a measurement is only worth taking where it would be acted on.
        (["-p", "av1Animation"],
         "Film grain: none - the av1Animation default is 0 (-g 0 measures each "
         "source instead)."),
    ], ids=["x265 without -g", "the grain default", "a level outright",
            "the probe outright", "an AV1 profile that defaults to none",
            "animation"])
    def test_and_the_summary_says_what_the_run_settled_on(self, video_cli,
                                                          options, summary):
        _, log = video_cli.start(*options)
        assert summary in log


class TestTheBitrateTest:
    """-t takes an OPTIONAL percentage, which getopts cannot express, so the flag
    claims the next word itself when it is a number. The three spellings it has
    to keep apart are here: no number, a number, and a number it must NOT eat -
    the input directory, which follows the options."""

    @pytest.mark.parametrize("options,summary", [
        (["-t"], "Bitrate test: on (-t)"),
        (["-t"], "would still be adequate on 50% less bitrate"),
        (["-t", 30], "would still be adequate on 30% less bitrate"),
        (["-t", 0], "would still be adequate on 0% less bitrate"),
        (["-p", "av1Grain"],
         "Bitrate test: off, every source is converted"),
    ], ids=["-t alone", "the default saving", "a percentage", "-t 0",
            "without -t"])
    def test_what_the_summary_reports(self, video_cli, options, summary):
        _, log = video_cli.start(*options)
        assert summary in log

    def test_a_saving_of_the_whole_bitrate_is_refused(self, video_cli):
        """A saving is a share of the source's bitrate, so 100% asks for an
        encode adequate at no bitrate at all - refused up front rather than
        skipping every file."""
        status, log = video_cli.start("-t", 100)
        assert status == 1
        assert ('The -t saving must be a whole percentage between 0 and 99 '
                '(got "100").') in log


class TestWhereTheQualityLevelComesFrom:
    """The resolution bias is otherwise invisible: the profile row and the
    summary both show the unbiased level, and a 2160p file is then encoded two
    levels away from it. The ladder in the line is generated from the bias table,
    so a summary that drifted from the tiers a run applies is caught here too."""

    @pytest.mark.parametrize("options,summary", [
        (["-p", "av1Grain"],
         "biased per file by the tier it is ENCODED at (4320p +3, 2160p +2, "
         "1440p +1, 1080p 0, 720p -1, SD -2, unknown 0; override with -q)"),
        (["-p", "av1Grain", "-q", 22],
         "Video quality: -crf 22 for every file (-q), so no resolution bias is "
         "applied."),
        (["-p", "av1Constrained"],
         "Video quality: none to set - av1Constrained targets an average "
         "bitrate, not a quality level."),
    ], ids=["the bias ladder", "-q turns it off", "a bitrate-capped profile"])
    def test_the_summary_states_it(self, video_cli, options, summary):
        _, log = video_cli.start(*options)
        assert summary in log


class TestWhichFfmpegTheRunUses:
    """The AV1 profiles need SVT-AV1 parameters, and the *Nvenc ones NVENC's uhq
    tuning, that a distribution's ffmpeg is often too old for - and a too-old
    libsvtav1 DROPS an unknown parameter rather than failing, so a build applying
    half the tuning looks like one applying all of it. Hence: search for a newer
    build, and say which one was used."""

    def test_the_run_says_which_one(self, video_cli):
        _, log = video_cli.start("-p", "av1Grain")
        assert "Using ffmpeg: " in log

    def test_one_placed_on_path_wins_and_is_not_second_guessed(self, video_cli):
        """That is what keeps every stubbed case in this suite valid: a run
        silently preferring the real encoder over the stub would exercise neither
        what the case set up nor what it asserts."""

        video_cli.with_tool("ffmpeg", "exit 0")
        video_cli.with_tool("ffprobe", "echo 1.0")
        _, log = video_cli.start("-p", "av1Grain")
        chosen = [line for line in log.splitlines() if "Using ffmpeg" in line]
        assert chosen and str(video_cli.bin / "ffmpeg") in chosen[0], chosen

    def test_an_override_pins_a_build_even_when_a_better_one_exists(
            self, video_cli):
        """An instruction, not a preference - otherwise there is no way to pin
        one at all."""
        import os as _os

        video_cli.with_tool("ffmpeg", "exit 0")
        video_cli.with_tool("ffprobe", "echo 1.0")
        pinned = str(video_cli.bin / "ffmpeg")
        _, log = video_cli.start("-p", "av1Grain",
                                 env=dict(_os.environ, ffmpegOverride=pinned))
        assert "Using ffmpeg: %s" % pinned in log

    def test_and_a_mistyped_one_is_refused_rather_than_searched_past(
            self, video_cli):
        """Falling back to a search would silently ignore what the caller asked
        for."""
        import os as _os

        status, log = video_cli.start(
            "-p", "av1Grain",
            env=dict(_os.environ, ffmpegOverride="/nonexistent/ffmpeg"))
        assert status == 1
        assert ('ffmpegOverride names "/nonexistent/ffmpeg", which is not an '
                "executable file.") in log


# Every ffmpeg call is logged with its arguments, because the central claim below
# is WHICH file the encodes read. The calls answer the way a run needs: a
# capability probe succeeds silently, an encoder help page admits to -dolbyvision
# so the run believes the encoder can code an RPU, an encode writes its output and
# its progress file, and the Dolby Vision extraction writes its elementary stream
# to the pipe dovi_tool reads.
_DV_FFMPEG = '''
printf '%s\\n' "$*" >> "$FFMPEG_LOG"
case "$*" in
*-h\\ encoder=*)
    printf -- '-dolbyvision <boolean>\\n'
    exit 0 ;;
esac
progress=""
prev=""
for a in "$@"; do
    [[ "$prev" == "-progress" ]] && progress="$a"
    prev="$a"
done
[[ -n "$progress" ]] && printf 'frame=100\\nout_time_us=60000000\\n' >> "$progress"
out="${!#}"
if [[ "$out" == */* ]]; then
    printf 'x' > "$out"
else
    printf 'hevc'
fi
exit 0
'''

# Answers per FILE, which is what lets one run see a dual-layer profile 7 source
# and a single-layer profile 8 intermediate: the prepared file is the one named
# dv81.mkv, and a finished conversion is the one under an output tree.
_DV_FFPROBE = '''
file="${!#}"
case "$*" in
*format=duration*) echo "60.000000" ;;
*stream=channels*) echo "2" ;;
*stream=r_frame_rate*) echo "24000/1001" ;;
*stream=width*) echo '{"streams":[{"width":1920,"height":1080,"field_order":"progressive","sample_aspect_ratio":"1:1"}]}' ;;
*stream=color_primaries*) echo "bt2020" ;;
*stream=color_transfer*) echo "smpte2084" ;;
*stream=color_space*) echo "bt2020nc" ;;
*stream=color_range*) echo "tv" ;;
*frame=side_data_list*) echo '{"frames":[{"side_data_list":[]}]}' ;;
*stream_side_data=*)
    case "$file" in
    *dv81.mkv | *"$OUT_MARKER"*)
        echo '{"streams":[{"side_data_list":[{"side_data_type":"DOVI configuration record","dv_profile":8,"rpu_present_flag":1,"el_present_flag":0}]}]}' ;;
    *)
        echo '{"streams":[{"side_data_list":[{"side_data_type":"DOVI configuration record","dv_profile":7,"rpu_present_flag":1,"el_present_flag":1}]}]}' ;;
    esac ;;
esac
exit 0
'''

# The RPU probe finds an RPU, and the conversion writes the profile 8.1 elementary
# stream it is asked for. The two return codes are what the scenarios switch on.
_DV_DOVI_TOOL = '''
printf '%s\\n' "$*" >> "$DOVI_LOG"
cat >/dev/null
out=""
prev=""
for a in "$@"; do
    [[ "$prev" == "-o" ]] && out="$a"
    prev="$a"
done
case "$*" in
*extract-rpu*)
    [[ "${DOVI_RPU_RC:-0}" -eq 0 ]] || exit "$DOVI_RPU_RC"
    [[ -n "$out" ]] && printf 'rpu' > "$out"
    exit 0 ;;
esac
[[ "${DOVI_CONVERT_RC:-0}" -eq 0 ]] || exit "$DOVI_CONVERT_RC"
[[ -n "$out" ]] && printf 'hevc' > "$out"
exit 0
'''

# The container the converted stream goes back into. Logged, because the frame
# rate it is handed is what keeps that stream's timing - and so every chunk
# boundary cut from it - right. It exits 1 on non-fatal warnings while still
# producing a valid file, which is a scenario of its own.
_DV_MKVMERGE = '''
printf '%s\\n' "$*" >> "$MKVMERGE_LOG"
[[ "${MKVMERGE_RC:-0}" -le 1 ]] || { printf 'stub mux failure\\n' >&2; exit "$MKVMERGE_RC"; }
prev=""
for a in "$@"; do
    [[ "$prev" == "-o" ]] && printf 'mkv' > "$a"
    prev="$a"
done
exit "${MKVMERGE_RC:-0}"
'''


class TestNormalisingDolbyVisionProfile7:
    """Profile 7 keeps its RPU in an enhancement layer no encoder can re-encode,
    so encoding such a file as it arrives loses its dynamic metadata. The run
    converts it first, into a video-only single-layer profile 8.1 intermediate
    that the encode reads INSTEAD of the source.

    What that makes worth a whole run is the wiring no unit case reaches: the
    intermediate is what every parallel chunk worker encodes from - each a process
    of its own, so the decision has to travel in the environment - while the audio
    and the subtitles still come from the ORIGINAL, the raw stream is given the
    source's frame rate on its way into a container, and the scratch is handed
    back afterwards.

    Nothing here is real Dolby Vision; that a profile 7 stream really converts,
    and that the result really is 8.1, is `tests/lib/test_dolbyvision.py`'s and
    the media tier's.
    """

    @pytest.fixture
    def dv(self, sandbox, tmp_path):
        import os as _os

        sandbox.with_tool("ffmpeg", _DV_FFMPEG)
        sandbox.with_tool("ffprobe", _DV_FFPROBE)
        sandbox.with_tool("dovi_tool", _DV_DOVI_TOOL)
        sandbox.with_tool("mkvmerge", _DV_MKVMERGE)

        inputs = tmp_path / "in"
        inputs.mkdir()
        (inputs / "clip.mkv").write_text("")
        logs = {name: tmp_path / (name.lower() + ".log")
                for name in ("FFMPEG_LOG", "DOVI_LOG", "MKVMERGE_LOG")}

        def convert(name, drop_dovi_tool=False, **scenario):
            for path in logs.values():
                path.write_text("")
            env = dict(_os.environ, OUT_MARKER="/scenario-",
                       **{key: str(path) for key, path in logs.items()},
                       **{key: str(value) for key, value in scenario.items()})
            path = sandbox.path
            if drop_dovi_tool:
                # The host has dovi_tool installed, so its absence is a PATH
                # holding every stub but that one, and no host directory that
                # carries it either.
                spare = tmp_path / "bin-no-dovi"
                spare.mkdir(exist_ok=True)
                for tool in sandbox.bin.iterdir():
                    target = spare / tool.name
                    if tool.name != "dovi_tool" and not target.exists():
                        target.symlink_to(tool.resolve())
                keep = [part for part in _os.environ["PATH"].split(_os.pathsep)
                        if not (Path(part) / "dovi_tool").exists()]
                path = _os.pathsep.join([str(spare)] + keep)
            # -j 32 so the source is cut into several chunks whatever the core
            # count: the chunk workers are separate processes, and what they
            # encode from is half of what this asserts. -g off keeps the film
            # grain probe, which has nothing to do with any of it, out of the run.
            done = sandbox.run(
                "convert-video", "-p", "av1Fast", "-j", 32, "-g", "off",
                inputs, tmp_path / ("scenario-" + name),
                env=env, path=path, timeout=600)
            return done.stdout + done.stderr

        sandbox.convert = convert
        sandbox.inputs = inputs
        sandbox.logs = logs
        sandbox.out_of = lambda name: (tmp_path / ("scenario-" + name))
        return sandbox

    @staticmethod
    def _video_encodes_from(dv, fragment):
        """How many of the run's VIDEO encodes - the calls carrying a -progress
        file - read a file whose path matches. The one question this turns on."""
        return len([line for line in
                    dv.logs["FFMPEG_LOG"].read_text().splitlines()
                    if "-progress" in line and fragment in line])

    @staticmethod
    def _scratch_leftovers(dv):
        """The converted stream and its container are a whole film's video, so a
        run over a folder of profile 7 films must not accumulate one per file.

        Every base a run may pick, not one of them: an assertion that looked in
        the wrong directory would be an empty list either way."""
        import os as _os
        bases = {_os.environ.get(name, "") for name in
                 ("ramBase", "ramScratchBase", "TMPDIR")}
        bases.add("/dev/shm")
        bases.add("/tmp")
        return sorted(str(path) for base in bases if base and Path(base).is_dir()
                      for path in Path(base).rglob("dv81.*"))

    @pytest.fixture
    def normalised(self, dv):
        return dv, dv.convert("normalised")

    def test_the_source_is_converted_with_dovi_tool(self, normalised):
        dv, _ = normalised
        dovi = dv.logs["DOVI_LOG"].read_text()
        assert "-m 2" in dovi
        assert "--discard" in dovi, "the enhancement layer is not discarded"

    def test_the_converted_stream_keeps_the_sources_exact_frame_rate(
            self, normalised):
        dv, _ = normalised
        assert "--default-duration 0:24000/1001fps" in dv.logs[
            "MKVMERGE_LOG"].read_text()

    def test_every_chunk_worker_encodes_from_the_intermediate(self,
                                                              normalised):
        """The workers are separate processes, so this is also the assertion that
        the decision reached them."""
        dv, _ = normalised
        assert self._video_encodes_from(dv, "dv81.mkv") >= 2
        assert self._video_encodes_from(
            dv, str(dv.inputs / "clip.mkv")) == 0

    def test_and_what_the_intermediate_does_not_have_still_comes_from_the_original(
            self, normalised):
        dv, _ = normalised
        ffmpeg = dv.logs["FFMPEG_LOG"].read_text()
        assert "-map 0:a:0" in ffmpeg, "the audio is not taken from the source"
        assert "-i %s" % (dv.inputs / "clip.mkv") in ffmpeg, (
            "the mux does not read the source for subtitles and chapters")

    def test_the_encode_is_told_to_carry_the_rpu_through(self, normalised):
        dv, _ = normalised
        assert "-dolbyvision 1" in dv.logs["FFMPEG_LOG"].read_text()

    def test_the_run_says_what_it_is_doing_and_produces_its_output(
            self, normalised):
        dv, log = normalised
        assert "normalising the dual-layer profile 7 source" in log
        assert (dv.out_of("normalised") / "clip.mkv").is_file()

    def test_the_scratch_is_handed_back_afterwards(self, normalised):
        dv, _ = normalised
        assert self._scratch_leftovers(dv) == []

    def test_a_mux_warning_still_leaves_the_intermediate_in_use(self, dv):
        """mkvmerge exits 1 on non-fatal warnings while still producing a valid
        file, so that is not a reason to throw the conversion away."""
        dv.convert("muxWarned", MKVMERGE_RC=1)
        assert self._video_encodes_from(dv, "dv81.mkv") >= 2

    def _fell_back(self, dv, name, log):
        """None of the declines may fail the FILE: the source still carries its
        own RPU, so a normalisation that cannot happen just means encoding it as
        the plain HDR10 it also is."""
        assert (dv.out_of(name) / "clip.mkv").is_file()
        assert self._video_encodes_from(dv, str(dv.inputs / "clip.mkv")) >= 2
        assert "-dolbyvision 1" not in dv.logs["FFMPEG_LOG"].read_text(), (
            "the RPU is claimed to survive an encode of the source")
        assert self._scratch_leftovers(dv) == []

    def test_a_failed_conversion_falls_back_and_is_reported(self, dv):
        log = dv.convert("convertFailed", DOVI_CONVERT_RC=1)
        self._fell_back(dv, "convertFailed", log)
        assert "encoding the source as plain HDR10 instead" in log

    def test_a_failed_mux_falls_back_and_says_so(self, dv):
        log = dv.convert("muxFailed", MKVMERGE_RC=2)
        self._fell_back(dv, "muxFailed", log)
        assert "could not be muxed" in log

    def test_a_container_claiming_profile_7_over_no_rpu_at_all(self, dv):
        """ffmpeg copies the configuration record while dropping the RPU, so
        there is nothing to convert - and it has to be recognised from the
        48-frame probe rather than by copying the whole stream out first."""
        log = dv.convert("noRpu", DOVI_RPU_RC=1)
        self._fell_back(dv, "noRpu", log)
        assert "carries no RPU" in log
        assert "-m 2" not in dv.logs["DOVI_LOG"].read_text(), (
            "a false claim cost a conversion")

    def test_without_dovi_tool_the_missing_tool_is_named(self, dv):
        log = dv.convert("noDoviTool", drop_dovi_tool=True)
        self._fell_back(dv, "noDoviTool", log)
        assert "without dovi_tool" in log
