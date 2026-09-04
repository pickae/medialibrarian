"""The white box for medialib/lib/booknarration.py.

What is pinned here: the exact argv each probe and each engine call is handed,
the two stderr refusals, the venv bootstrap branch, the PATH the engine call runs
under, and the file-level decisions - which of several m4b is the one, which
sitecustomize goes, which session directory is dropped.
"""

import os
import re
import shutil
import sys
from types import SimpleNamespace

import pytest

from medialib.lib import booknarration as bn
from tests import blackbox

pytestmark = pytest.mark.stubbed

_TOOLSTUB = blackbox.TOOLSTUB

_PROBE = 'import sys; print("%d.%d" % sys.version_info[:2])'
_SYS_CONFIG = "import sysconfig; print(sysconfig.get_paths()['purelib'])"

_NARRATION_PLUMBING = ("bash", "awk", "cat", "base64")

_NARRATION_ENV = (
    "narrationHome", "narrationPython", "narrationDevice", "narrationEnvReady",
    "narrationVoiceMap", "narrationLanguage", "narrationEngine",
    "narrationEngineLanguages", "narrationFormat", "narrationChannel",
    "narrationVerbose", "narrationVramPerBookGB", "narrationProgressTail",
    "narrationLosslessTolerance", "narrationVoiceFallbackNames",
    "narrationTorchcodecPairs", "narrationSoundfileFallbackMark",
    "narrationPythonMinMinor", "narrationPythonMaxMinor", "voiceSampleSeconds",
    "voiceSampleMaxSeconds", "voiceSampleMinSeconds", "voiceSampleSearchSeconds",
    "voiceSilenceNoise", "voiceSilenceMinDur", "voiceSampleMaxGap",
    "voiceSampleRate", "voiceSampleCodec", "PYTHONNOUSERSITE",
)


_table_line = blackbox.toolstub_table_line


@pytest.fixture()
def nb(tmp_path, monkeypatch):
    """A PATH holding only the stubs a test installs, the toolstub's knobs, and
    a clean narration environment. The venv python is NOT on this PATH: the
    module reaches it by the absolute path it was settled with."""
    bin_dir = tmp_path / "bin"
    out_dir = tmp_path / "stubout"
    state_dir = tmp_path / "stubstate"
    for d in (bin_dir, out_dir, state_dir):
        d.mkdir()
    for tool in _NARRATION_PLUMBING:
        (bin_dir / tool).symlink_to(shutil.which(tool))
    record = tmp_path / "calls"

    def install(name, path=None):
        target = path if path else bin_dir / name
        shutil.copyfile(_TOOLSTUB, str(target))
        os.chmod(str(target), 0o755)
        return str(target)

    def say(name, text):
        (out_dir / name).write_text(text)

    def rc(name, codes):
        (out_dir / (name + ".rc")).write_text(codes + "\n")

    def write(name, paths):
        (out_dir / (name + ".write")).write_text(" ".join(paths) + "\n")

    def table(name, lines):
        (out_dir / (name + ".table")).write_text("\n".join(lines) + "\n")

    def calls():
        if not record.exists():
            return []
        return [line.rstrip("\n").split("\t")[1:]
                for line in record.read_text().splitlines() if line]

    def clear():
        if record.exists():
            record.unlink()

    for name in _NARRATION_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("TOOLSTUB_LOG", str(record))
    monkeypatch.setenv("TOOLSTUB_OUT", str(out_dir))
    monkeypatch.setenv("TOOLSTUB_STATE", str(state_dir))
    return SimpleNamespace(install=install, say=say, rc=rc, write=write,
                           table=table, calls=calls, clear=clear,
                           bin_dir=bin_dir, out_dir=out_dir, tmp_path=tmp_path)


def _checkout(nb, venv_python="stub"):
    """A checkout with app.py and a python_env/bin/python that is what the test
    named: a copy of the toolstub, or a bash script that exits 0 for everything
    and answers the sysconfig probe with the given site."""
    checkout = nb.tmp_path / "checkout"
    (checkout / "python_env" / "bin").mkdir(parents=True)
    (checkout / "app.py").write_text("print('engine')\n", encoding="ascii")
    exe = checkout / "python_env" / "bin" / "python"
    if venv_python == "stub":
        nb.install("python", path=exe)
    else:
        site, = venv_python
        exe.write_text(
            "#!/bin/bash\n"
            'if [[ "${1:-}" == "-c" && "${2:-}" == *sysconfig* ]]; then\n'
            '    echo "%s"\n'
            "fi\nexit 0\n" % site,
            encoding="ascii")
        os.chmod(str(exe), 0o755)
    return str(checkout)


def _venv_env(nb, checkout):
    python = os.path.join(checkout, "python_env", "bin", "python")
    os.environ["narrationHome"] = checkout
    os.environ["narrationPython"] = python
    return python


# --- the engine's language table -----------------------------------------------


def test_the_table_is_split_on_semicolons_then_whitespace(nb, monkeypatch):
    monkeypatch.setenv(
        "narrationEngineLanguages", "xtts:ara,eng;other:deu,fra")
    assert bn.narration_supports_language("deu", "other") is True
    assert bn.narration_supports_language("deu") is False
    assert bn.narration_supports_language("eng") is True
    assert bn.narration_language_list("other") == "deu fra"


def test_the_shipped_xtts_row_is_what_a_refusal_quotes(nb):
    """The list is what the user is shown when a book is turned away, so it is
    pinned as text rather than derived from the same constant that builds it."""
    assert bn.narration_language_list("xtts") == (
        "ara ces deu eng fra hin hun ita jpn kor nld pol por rus spa tur zho")
    assert bn.narration_supports_language("deu", "xtts") is True
    assert bn.narration_supports_language("swe", "xtts") is False


def test_no_language_at_all_is_not_a_supported_one(nb):
    """A book whose language could not be read must not be narrated as though
    it were English."""
    assert bn.narration_supports_language("", "xtts") is False


def test_an_engine_with_no_row_is_not_second_guessed(nb):
    assert bn.narration_supports_language("zzz", "unknown") is True
    assert bn.narration_language_list("unknown") == ""


def test_the_engine_is_lowered_and_defaults_to_narrationEngine(nb, monkeypatch):
    monkeypatch.setenv("narrationEngine", "XTTS")
    assert bn.narration_supports_language("eng") is True
    assert bn.narration_supports_language("zzz") is False


# --- finding the base python ----------------------------------------------------


class TestBasePython:
    def test_pyenv_versions_come_before_path(self, nb):
        root = nb.tmp_path / "pyenv"
        py312 = root / "versions" / "3.12.1" / "bin" / "python3"
        py312.parent.mkdir(parents=True)
        nb.install("pyenv")
        nb.table("pyenv", [
            _table_line(["root"], 0, str(root)),
            _table_line(["versions", "--bare"], 0,
                        "3.10.4\n3.12.1\n3.9.7\n3.13.0\n"),
        ])
        nb.install("python3", path=py312)
        nb.table("python3", [_table_line(["-c", _PROBE], 0, "3.12")])
        assert bn.narration_base_python() == str(py312)
        assert nb.calls() == [
            ["pyenv", "root"], ["pyenv", "versions", "--bare"],
            ["python3", "-c", _PROBE],
        ]

    def test_the_version_filter_keeps_only_3_10_to_3_12(self, nb):
        root = nb.tmp_path / "pyenv"
        py310 = root / "versions" / "3.100.0" / "bin" / "python3"
        py310.parent.mkdir(parents=True)
        nb.install("pyenv")
        nb.table("pyenv", [
            _table_line(["root"], 0, str(root)),
            _table_line(["versions", "--bare"], 0, "3.100.0\n"),
        ])
        nb.install("python3", path=py310)
        nb.table("python3", [_table_line(["-c", _PROBE], 0, "3.100")])
        assert bn.narration_base_python() is None
        assert nb.calls() == [["pyenv", "root"], ["pyenv", "versions", "--bare"]]

    def test_without_pyenv_path_is_scanned_newest_first(self, nb):
        for minor, version in (("12", "3.12"), ("11", "3.11"),
                               ("10", "3.10")):
            nb.install("python3.%s" % minor)
            nb.table("python3.%s" % minor,
                     [_table_line(["-c", _PROBE], 0, version)])
        assert bn.narration_base_python() == str(nb.bin_dir / "python3.12")
        assert nb.calls() == [["python3.12", "-c", _PROBE]]

    def test_a_failing_probe_moves_on_to_the_next_newer_is_tried_first(self, nb):
        nb.install("python3.12")
        nb.table("python3.12", [_table_line(["-c", _PROBE], 1, "")])
        nb.install("python3.11")
        nb.table("python3.11", [_table_line(["-c", _PROBE], 0, "3.11")])
        assert bn.narration_base_python() == str(nb.bin_dir / "python3.11")

    def test_a_version_out_of_range_is_not_an_interpreter(self, nb):
        nb.install("python3.13")
        nb.table("python3.13", [_table_line(["-c", _PROBE], 0, "3.13")])
        nb.install("python3.9")
        nb.table("python3.9", [_table_line(["-c", _PROBE], 0, "3.9")])
        assert bn.narration_base_python() is None

    def test_pyenv_root_that_says_nothing_falls_back_to_path(self, nb):
        nb.install("pyenv")
        nb.table("pyenv", [_table_line(["root"], 1, "")])
        nb.install("python3.10")
        nb.table("python3.10", [_table_line(["-c", _PROBE], 0, "3.10")])
        assert bn.narration_base_python() == str(nb.bin_dir / "python3.10")
        assert nb.calls() == [["pyenv", "root"], ["python3.10", "-c", _PROBE]]

    def test_a_root_named_and_then_failed_is_still_the_root(self, nb):
        """The shell reads it as ``$(pyenv root 2>/dev/null || true)`` - the
        status is swallowed on purpose, so a pyenv that names its root and then
        exits non-zero has still named it. Requiring success here would walk
        past every interpreter that root holds, which is the whole reason pyenv
        is asked before PATH."""
        root = nb.tmp_path / "pyenv"
        py312 = root / "versions" / "3.12.1" / "bin" / "python3"
        py312.parent.mkdir(parents=True)
        nb.install("pyenv")
        nb.table("pyenv", [
            _table_line(["root"], 1, str(root)),
            _table_line(["versions", "--bare"], 0, "3.12.1\n"),
        ])
        nb.install("python3", path=py312)
        nb.table("python3", [_table_line(["-c", _PROBE], 0, "3.12")])
        nb.install("python3.10")
        nb.table("python3.10", [_table_line(["-c", _PROBE], 0, "3.10")])
        assert bn.narration_base_python() == str(py312)
        assert nb.calls()[:2] == [["pyenv", "root"],
                                  ["pyenv", "versions", "--bare"]]

    def test_a_root_with_leading_space_is_the_root_it_printed(self, nb):
        """Command substitution takes off trailing newlines and nothing else, so
        a probe's answer keeps whatever else it printed - and the readers split
        what they are handed on its first dot rather than trimming it."""
        nb.install("pyenv")
        nb.table("pyenv", [
            _table_line(["root"], 0, " /nowhere \n"),
            _table_line(["versions", "--bare"], 0, "3.12.1\n"),
        ])
        assert bn.narration_base_python() is None
        assert nb.calls() == [["pyenv", "root"], ["pyenv", "versions", "--bare"]]

    def test_a_directory_wearing_a_candidates_name_is_not_it(self, nb):
        (nb.bin_dir / "python3.12").mkdir()
        nb.install("python3.11")
        nb.table("python3.11", [_table_line(["-c", _PROBE], 0, "3.11")])
        assert bn.narration_base_python() == str(nb.bin_dir / "python3.11")

    def test_nothing_qualifying_is_no_python(self, nb):
        assert bn.narration_base_python() is None
        assert nb.calls() == []


# --- the venv -------------------------------------------------------------------


class TestEnsureVenv:
    def test_a_prepared_env_is_echoed_without_any_call(self, nb):
        checkout = _checkout(nb)
        os.environ["narrationHome"] = checkout
        python = bn.narration_ensure_venv()
        assert python == os.path.join(checkout, "python_env", "bin", "python")
        assert nb.calls() == []

    def test_an_env_python_without_the_execute_bit_is_built(self, nb):
        checkout = _checkout(nb)
        exe = os.path.join(checkout, "python_env", "bin", "python")
        os.chmod(exe, 0o644)
        os.environ["narrationHome"] = checkout
        assert bn.narration_ensure_venv() is None

    def test_the_bootstrap_runs_venv_then_pip_and_echoes_the_path(self, nb):
        # The base python is a script, not the stub: the venv step has to
        # MATERIALISE an executable interpreter at the path only the module
        # knows, and a stub's write cannot chmod the file it makes. The script
        # answers the version probe, creates the interpreter when asked for the
        # venv, and the interpreter it creates records its own calls in the
        # stub's line format.
        checkout = nb.tmp_path / "checkout"
        checkout.mkdir()
        (checkout / "app.py").write_text("x\n", encoding="ascii")
        os.environ["narrationHome"] = str(checkout)
        base = nb.bin_dir / "python3.12"
        base.write_text(r"""#!/bin/bash
if [[ "${1:-}" == "-c" ]]; then
    echo "3.12"
    exit 0
fi
if [[ "${1:-}" == "-m" && "${2:-}" == "venv" ]]; then
    /bin/mkdir -p -- "$3/bin"
    cat > "$3/bin/python" <<'PYEOF'
#!/bin/bash
line=$'call\tpython'
for a in "$@"; do line+=$'\t'"$a"; done
printf '%s\n' "$line" >> "${TOOLSTUB_LOG}"
exit 0
PYEOF
    /bin/chmod +x -- "$3/bin/python"
    exit 0
fi
exit 1
""", encoding="ascii")
        os.chmod(str(base), 0o755)
        python = bn.narration_ensure_venv()
        assert python == str(checkout / "python_env" / "bin" / "python")
        assert os.access(python, os.X_OK)
        assert nb.calls() == [
            ["python", "-m", "pip", "install", "--quiet", "--upgrade",
             "pip", "setuptools", "wheel"],
        ]

    def test_the_bootstrap_stops_when_pip_fails(self, nb):
        checkout = nb.tmp_path / "checkout"
        checkout.mkdir()
        (checkout / "app.py").write_text("x\n", encoding="ascii")
        os.environ["narrationHome"] = str(checkout)
        nb.install("python3.12")
        nb.table("python3.12", [_table_line(["-c", _PROBE], 0, "3.12")])
        nb.rc("python3.12", "0 7")
        assert bn.narration_ensure_venv() is None

    def test_no_base_python_prints_the_refusal_and_returns_none(self, nb, capsys):
        os.environ["narrationHome"] = str(nb.tmp_path / "checkout")
        assert bn.narration_ensure_venv() is None
        assert capsys.readouterr().err == (
            "\n"
            "Cannot narrate: no Python 3.10-3.12 interpreter found on this "
            "machine.\n\n"
            "  python3.10-3.12  what ebook2audiobook runs on (not older, not "
            "newer)  apt install python3.12-venv  (or: "
            "https://github.com/pyenv/pyenv)\n"
            "\nInstall one and run again. Nothing was changed.\n")

    def test_the_refusal_uses_the_configured_range(self, nb, capsys, monkeypatch):
        monkeypatch.setenv("narrationPythonMinMinor", "9")
        monkeypatch.setenv("narrationPythonMaxMinor", "11")
        assert bn.narration_ensure_venv() is None
        err = capsys.readouterr().err
        assert "no Python 3.9-3.11 interpreter" in err
        assert "python3.9-3.11  what ebook2audiobook runs on" in err


# --- the torchcodec question ------------------------------------------------------


class TestPackageSeries:
    def test_the_script_is_read_from_stdin_against_a_real_python(
            self, tmp_path, monkeypatch):
        # The series is read by a SUBPROCESS, the way the module hands the venv
        # python its script on stdin - so the dist-info has to be visible the
        # way a subprocess sees things, through PYTHONPATH rather than through
        # this process' sys.path.
        site = tmp_path / "site"
        dist = site / "torchcodec-0.7.0+cu129.dist-info"
        dist.mkdir(parents=True)
        (dist / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: torchcodec\n"
            "Version: 0.7.0+cu129\n", encoding="ascii")
        monkeypatch.setenv("narrationPython", sys.executable)
        monkeypatch.setenv("PYTHONPATH", str(site))
        assert bn.narration_package_series("torchcodec") == "0.7"
        assert bn.narration_package_series("not-installed-here") is None

    def test_without_a_settled_python_there_is_no_series(self, nb):
        assert bn.narration_package_series("torch") is None


class TestEnsureTorchcodec:
    def _stub_python(self, nb, checkout):
        _venv_env(nb, checkout)
        return "python"

    def test_an_import_that_succeeds_is_the_whole_answer(self, nb):
        checkout = _checkout(nb)
        self._stub_python(nb, checkout)
        nb.table("python", [_table_line(["-c", "import torchcodec"], 0, "")])
        assert bn.narration_ensure_torchcodec() is True
        assert nb.calls() == [["python", "-c", "import torchcodec"]]

    def test_no_torchcodec_at_all_is_the_shim_answer(self, nb):
        checkout = _checkout(nb)
        self._stub_python(nb, checkout)
        nb.table("python", [
            _table_line(["-c", "import torchcodec"], 1, ""),
            _table_line(["-", "torchcodec"], 1, ""),
        ])
        assert bn.narration_ensure_torchcodec() is False
        assert nb.calls() == [["python", "-c", "import torchcodec"],
                              ["python", "-", "torchcodec"]]

    def test_a_mismatch_is_corrected_with_the_paired_series(self, nb):
        checkout = _checkout(nb)
        self._stub_python(nb, checkout)
        nb.table("python", [
            _table_line(["-c", "import torchcodec"], 1, ""),
            _table_line(["-", "torchcodec"], 0, "0.7"),
            _table_line(["-", "torch"], 0, "2.9"),
            _table_line(["-m", "pip", "install", "--quiet",
                         "--no-cache-dir", "--no-deps",
                         "torchcodec==0.9.*"], 0, ""),
        ])
        logs = []
        # The re-import after the install cannot succeed in the stub's world:
        # the table answers a repeated argv with its first matching line, and
        # that one is the failure. What is pinned is the dispatch - the
        # correction reached for, and the re-import it is followed by.
        assert bn.narration_ensure_torchcodec(logs.append) is False
        assert nb.calls() == [
            ["python", "-c", "import torchcodec"],
            ["python", "-", "torchcodec"],
            ["python", "-", "torch"],
            ["python", "-m", "pip", "install", "--quiet",
             "--no-cache-dir", "--no-deps", "torchcodec==0.9.*"],
            ["python", "-c", "import torchcodec"],
        ]
        assert logs == [
            "torchcodec 0.7 cannot load here (typically an FFmpeg newer than "
            "the versions it",
            "         shipped libraries for) - installing torchcodec 0.9, "
            "the one torch 2.9 is paired with.",
        ]

    def test_a_correction_that_still_does_not_load_is_false(self, nb):
        checkout = _checkout(nb)
        self._stub_python(nb, checkout)
        nb.table("python", [
            _table_line(["-c", "import torchcodec"], 1, ""),
            _table_line(["-", "torchcodec"], 0, "0.7"),
            _table_line(["-", "torch"], 0, "2.9"),
            _table_line(["-c", "import torchcodec"], 1, ""),
        ])
        assert bn.narration_ensure_torchcodec() is False

    def test_install_failure_is_false(self, nb):
        checkout = _checkout(nb)
        self._stub_python(nb, checkout)
        nb.table("python", [
            _table_line(["-c", "import torchcodec"], 1, ""),
            _table_line(["-", "torchcodec"], 0, "0.7"),
            _table_line(["-", "torch"], 0, "2.10"),
            _table_line(["-c", "import torchcodec"], 0, ""),
        ])
        nb.rc("python", "7")
        assert bn.narration_ensure_torchcodec() is False

    def test_the_paired_series_still_not_loading_is_not_a_mismatch(self, nb):
        checkout = _checkout(nb)
        self._stub_python(nb, checkout)
        nb.table("python", [
            _table_line(["-c", "import torchcodec"], 1, ""),
            _table_line(["-", "torchcodec"], 0, "0.9"),
            _table_line(["-", "torch"], 0, "2.9"),
        ])
        assert bn.narration_ensure_torchcodec() is False
        assert len(nb.calls()) == 3

    def test_a_torch_with_no_paired_row_is_left_alone(self, nb):
        checkout = _checkout(nb)
        self._stub_python(nb, checkout)
        nb.table("python", [
            _table_line(["-c", "import torchcodec"], 1, ""),
            _table_line(["-", "torchcodec"], 0, "0.7"),
            _table_line(["-", "torch"], 0, "2.12"),
        ])
        assert bn.narration_ensure_torchcodec() is False

    @pytest.mark.parametrize("torch,expected", [
        ("2.9", "0.9"), ("2.10", "0.10"), ("2.11", "0.11"),
    ])
    def test_the_pairs_table(self, nb, torch, expected):
        assert bn.narration_wanted_torchcodec(torch) == expected

    def test_a_torch_not_in_the_table_has_no_wanted_series(self, nb):
        assert bn.narration_wanted_torchcodec("2.8") is None
        assert bn.narration_wanted_torchcodec("2.12") is None


# --- the soundfile shim -----------------------------------------------------------


class TestSoundfileShim:
    def _site(self, nb, checkout, exists=True):
        site = nb.tmp_path / "site"
        if exists:
            site.mkdir()
        nb.table("python", [_table_line(["-c", _SYS_CONFIG], 0, str(site))])
        return str(site)

    def test_install_writes_the_sitecustomize(self, nb):
        checkout = _checkout(nb)
        _venv_env(nb, checkout)
        site = self._site(nb, checkout)
        assert bn.narration_install_soundfile_fallback() == 0
        with open(os.path.join(site, "sitecustomize.py"),
                  encoding="utf-8") as written:
            text = written.read()
        assert text == bn._SITECUSTOMIZE
        assert "_load_with_soundfile_fallback" in text

    def test_install_without_a_site_directory_is_a_noop(self, nb):
        checkout = _checkout(nb)
        _venv_env(nb, checkout)
        self._site(nb, checkout, exists=False)
        assert bn.narration_install_soundfile_fallback() == 0

    def test_install_without_a_settled_python_is_a_noop(self, nb):
        assert bn.narration_install_soundfile_fallback() == 0

    def test_remove_takes_only_the_repo_s_own_file(self, nb):
        checkout = _checkout(nb)
        _venv_env(nb, checkout)
        site = self._site(nb, checkout)
        target = os.path.join(site, "sitecustomize.py")
        with open(target, "w", encoding="utf-8") as f:
            f.write("not ours\n")
        assert bn.narration_remove_soundfile_fallback() == 0
        assert os.path.isfile(target)

    def test_remove_finds_the_mark_anywhere_in_the_file(self, nb):
        checkout = _checkout(nb)
        _venv_env(nb, checkout)
        site = self._site(nb, checkout)
        target = os.path.join(site, "sitecustomize.py")
        with open(target, "w", encoding="utf-8") as f:
            f.write("an earlier version's head\n")
            f.write("    def _load_with_soundfile_fallback(uri):\n")
        assert bn.narration_remove_soundfile_fallback() == 0
        assert not os.path.exists(target)

    def test_remove_without_the_file_is_a_noop(self, nb):
        checkout = _checkout(nb)
        _venv_env(nb, checkout)
        self._site(nb, checkout)
        assert bn.narration_remove_soundfile_fallback() == 0


# --- the engine's device detection ---------------------------------------------------


# detect_device() as v26.8.20 leaves it: the helper defined in the branch that
# does not need it, and called from the one that does.
_FAULTED = '''class DeviceInstaller:
    def check_device_info(self):
        def version_classify(version_str, version_range):
            return (0, (), (), ())

        if self.rocm:

            def _normalize_version(v):
                return (int(v), 0, 0)

            return _normalize_version('5.7')
        else:
            return _normalize_version('12.9')
'''


class TestDeviceDetectionFix:
    def _installer(self, nb, text):
        checkout = nb.tmp_path / "ck"
        (checkout / "lib" / "classes").mkdir(parents=True)
        target = checkout / "lib" / "classes" / "device_installer.py"
        target.write_text(text, encoding="utf-8")
        os.environ["narrationHome"] = str(checkout)
        return target

    def _detect_on_cuda(self, text):
        """Run the branch that only ever CALLED the helper - the one the engine
        dies in while the definition sits in the other."""
        namespace = {"re": re}
        exec(compile(text, "device_installer.py", "exec"), namespace)
        installer = namespace["DeviceInstaller"]()
        installer.rocm = False
        return installer.check_device_info()

    def test_the_helper_lands_where_both_branches_can_see_it(self, nb):
        target = self._installer(nb, _FAULTED)
        # The fault the fix is for, reproduced: without this the case below
        # would pass against a checkout that never had anything wrong with it.
        with pytest.raises(UnboundLocalError):
            self._detect_on_cuda(_FAULTED)

        assert bn.narration_fix_device_detection() == 0
        text = target.read_text(encoding="utf-8")
        assert re.search(r"^ {8}def _normalize_version\(", text, re.M)
        assert self._detect_on_cuda(text) == (12, 9, 0)

    def test_a_checkout_upstream_already_fixed_is_left_alone(self, nb):
        target = self._installer(nb, _FAULTED)
        assert bn.narration_fix_device_detection() == 0
        once = target.read_text(encoding="utf-8")
        assert bn.narration_fix_device_detection() == 0
        assert target.read_text(encoding="utf-8") == once

    def test_a_checkout_without_the_helper_at_all_is_left_alone(self, nb):
        target = self._installer(nb, "    def check_device_info(self):\n"
                                     "        return 0\n")
        before = target.read_text(encoding="utf-8")
        assert bn.narration_fix_device_detection() == 0
        assert target.read_text(encoding="utf-8") == before

    def test_without_the_anchor_nothing_is_guessed(self, nb):
        # The insertion point is named, not searched for: a detect_device() this
        # fix does not recognise is not patched at a place picked at random.
        faulted = _FAULTED.replace("def version_classify(", "def classify(")
        target = self._installer(nb, faulted)
        assert bn.narration_fix_device_detection() == 0
        assert target.read_text(encoding="utf-8") == faulted

    def test_without_a_home_there_is_nothing_to_patch(self, nb, monkeypatch):
        monkeypatch.delenv("narrationHome", raising=False)
        assert bn.narration_fix_device_detection() == 0


# --- the emptied unidic -------------------------------------------------------------


class TestEmptyUnidic:
    def _site(self, nb, checkout):
        site = nb.tmp_path / "site"
        site.mkdir()
        nb.table("python", [_table_line(["-c", _SYS_CONFIG], 0, str(site))])
        return site

    def test_an_empty_unidic_goes(self, nb):
        checkout = _checkout(nb)
        _venv_env(nb, checkout)
        site = self._site(nb, checkout)
        (site / "unidic").mkdir()
        assert bn.narration_drop_empty_unidic() == 0
        assert not (site / "unidic").exists()

    def test_a_unidic_with_its_dictionary_stays(self, nb):
        checkout = _checkout(nb)
        _venv_env(nb, checkout)
        site = self._site(nb, checkout)
        (site / "unidic" / "dicdir").mkdir(parents=True)
        assert bn.narration_drop_empty_unidic() == 0
        assert (site / "unidic" / "dicdir").exists()

    def test_no_unidic_at_all_is_a_noop(self, nb):
        checkout = _checkout(nb)
        _venv_env(nb, checkout)
        self._site(nb, checkout)
        assert bn.narration_drop_empty_unidic() == 0

    def test_without_a_settled_python_is_a_noop(self, nb):
        assert bn.narration_drop_empty_unidic() == 0


# --- settling the run ---------------------------------------------------------------


class TestInit:
    def test_a_missing_checkout_is_refused_before_anything_is_touched(
            self, nb, capsys):
        checkout = nb.tmp_path / "checkout"
        checkout.mkdir()
        rc = bn.init_book_narration(str(checkout))
        assert rc == 1
        assert capsys.readouterr().err == (
            "\n"
            'Cannot narrate: no ebook2audiobook checkout at "%s".\n\n'
            % str(checkout)
            + "  ebook2audiobook  the text-to-speech engine that reads the "
              "books  git clone "
              "https://github.com/DrewThomasson/ebook2audiobook\n"
            + "\nClone it (or name an existing checkout) and run again. "
              "Nothing was changed.\n")
        assert "narrationHome" not in os.environ
        assert "narrationPython" not in os.environ

    def test_the_default_checkout_is_the_home_one(self, nb, capsys,
                                                  monkeypatch):
        monkeypatch.setenv("HOME", str(nb.tmp_path))
        rc = bn.init_book_narration()
        assert rc == 1
        assert ('at "%s/ebook2audiobook"' % nb.tmp_path) \
            in capsys.readouterr().err

    def test_the_home_is_settled_logically_not_physically(self, nb):
        real = nb.tmp_path / "real"
        (real / "python_env" / "bin").mkdir(parents=True)
        (real / "app.py").write_text("x\n", encoding="ascii")
        (real / "python_env" / "bin" / "python").write_text(
            "#!/bin/bash\nexit 0\n", encoding="ascii")
        os.chmod(str(real / "python_env" / "bin" / "python"), 0o755)
        link = nb.tmp_path / "link"
        os.symlink(str(real), str(link))
        rc = bn.init_book_narration(str(link))
        assert rc == 0
        assert os.environ["narrationHome"] == str(nb.tmp_path / "link")

    def test_a_prepared_checkout_settles_everything(self, nb):
        site = nb.tmp_path / "site"
        site.mkdir()
        checkout = _checkout(nb, venv_python=(str(site),))
        rc = bn.init_book_narration(checkout)
        assert rc == 0
        assert os.environ["narrationHome"] == checkout
        assert os.environ["narrationPython"] == os.path.join(
            checkout, "python_env", "bin", "python")
        assert os.environ["PYTHONNOUSERSITE"] == "1"
        assert os.environ["narrationDevice"] == "cpu"
        assert os.environ["narrationEnvReady"] == "1"

    def test_a_gpu_host_settles_cuda(self, nb):
        checkout = _checkout(nb)
        nb.install("nvidia-smi")
        nb.rc("nvidia-smi", "0")
        # The first python call - the torchcodec import - succeeds; the rest
        # fall through to the stub's default and the sysconfig answer is not
        # needed, so the fallback stays a no-op. The two sysconfig calls are the
        # venv being asked where it keeps its packages: once for the shim, once
        # for the emptied unidic.
        nb.rc("python", "0")
        rc = bn.init_book_narration(checkout)
        assert rc == 0
        assert os.environ["narrationDevice"] == "cuda"
        assert nb.calls() == [
            ["python", "-c", "import torchcodec"],
            ["python", "-c", _SYS_CONFIG],
            ["python", "-c", _SYS_CONFIG],
            ["nvidia-smi", "-L"],
            ["python", "-c", "import torch"],
        ]

    def test_the_device_argument_wins_over_the_probe(self, nb):
        checkout = _checkout(nb)
        nb.install("nvidia-smi")
        nb.rc("nvidia-smi", "0")
        nb.rc("python", "0")
        rc = bn.init_book_narration(checkout, device="cpu")
        assert rc == 0
        assert os.environ["narrationDevice"] == "cpu"
        assert nb.calls() == [
            ["python", "-c", "import torchcodec"],
            ["python", "-c", _SYS_CONFIG],
            ["python", "-c", _SYS_CONFIG],
            ["python", "-c", "import torch"],
        ]

    def test_an_unpopulated_env_is_ready_zero(self, nb):
        checkout = nb.tmp_path / "checkout"
        (checkout / "python_env" / "bin").mkdir(parents=True)
        (checkout / "app.py").write_text("x\n", encoding="ascii")
        exe = checkout / "python_env" / "bin" / "python"
        exe.write_text(
            "#!/bin/bash\n"
            'if [[ "${1:-}" == "-c" && "${2:-}" == *torchcodec* ]]; then\n'
            '    exit 1\n'
            "fi\nexit 1\n", encoding="ascii")
        os.chmod(str(exe), 0o755)
        rc = bn.init_book_narration(checkout)
        assert rc == 0
        assert os.environ["narrationEnvReady"] == "0"

    def test_no_python_at_all_refuses_the_run(self, nb, capsys):
        checkout = nb.tmp_path / "checkout"
        checkout.mkdir()
        (checkout / "app.py").write_text("x\n", encoding="ascii")
        rc = bn.init_book_narration(checkout)
        assert rc == 1
        assert "no Python 3.10-3.12 interpreter found" \
            in capsys.readouterr().err
        assert "narrationPython" not in os.environ


# --- the narration itself -------------------------------------------------------------


class TestNarrateBook:
    def _env(self, nb, checkout, device="cuda", fmt="m4b", channel="mono",
             engine="xtts"):
        os.environ["narrationHome"] = checkout
        os.environ["narrationPython"] = os.path.join(
            checkout, "python_env", "bin", "python")
        os.environ["narrationDevice"] = device
        os.environ["narrationFormat"] = fmt
        os.environ["narrationChannel"] = channel
        os.environ["narrationEngine"] = engine
        return checkout

    def _argv(self, checkout, book, outdir, device="cuda", fmt="m4b",
              channel="mono", engine="xtts", voice="", language=""):
        argv = ["python", "app.py", "--headless", "--device", device,
                "--tts_engine", engine, "--output_format", fmt,
                "--output_channel", channel, "--ebook", book,
                "--output_dir", outdir]
        if voice:
            argv += ["--voice", voice]
        if language:
            argv += ["--language", language]
        return argv

    def test_a_success_is_the_newest_file_it_left(self, nb):
        checkout = _checkout(nb)
        self._env(nb, checkout)
        outdir = str(nb.tmp_path / "out")
        book = str(nb.tmp_path / "book.epub")
        # The name may not hold a space: the stub's write list is a
        # word-separated list of destinations, one word per call.
        m4b = os.path.join(outdir, "MyBook.m4b")
        nb.rc("python", "0")
        nb.write("python", [m4b])
        nb.say("python", "engine console output")
        got = bn.narrate_book(book, outdir)
        assert got == m4b
        assert nb.calls() == [self._argv(checkout, book, outdir)]
        with open(os.path.join(outdir, "narration.log"),
                  encoding="ascii") as written:
            assert written.read() == "engine console output"

    def test_the_book_is_resolved_to_an_absolute_path(self, nb, monkeypatch):
        checkout = _checkout(nb)
        self._env(nb, checkout)
        work = nb.tmp_path / "work"
        work.mkdir()
        (work / "book.epub").write_text("x\n", encoding="ascii")
        monkeypatch.chdir(work)
        outdir = str(work / "out")
        m4b = os.path.join(outdir, "B.m4b")
        nb.rc("python", "0")
        nb.say("python", "x")
        nb.write("python", [m4b])
        got = bn.narrate_book("book.epub", outdir)
        assert got == m4b
        assert nb.calls() == [self._argv(checkout, str(work / "book.epub"),
                                         outdir)]

    def test_voice_and_language_are_optional_flags(self, nb):
        checkout = _checkout(nb)
        self._env(nb, checkout)
        outdir = str(nb.tmp_path / "out")
        book = str(nb.tmp_path / "book.epub")
        m4b = os.path.join(outdir, "B.m4b")
        nb.rc("python", "0")
        nb.say("python", "x")
        nb.write("python", [m4b])
        got = bn.narrate_book(book, outdir, voice="v.wav", language="deu")
        assert got == m4b
        assert nb.calls() == [self._argv(checkout, book, outdir,
                                         voice="v.wav", language="deu")]

    def test_an_empty_language_falls_back_to_narrationLanguage(self, nb):
        checkout = _checkout(nb)
        self._env(nb, checkout)
        os.environ["narrationLanguage"] = "fra"
        outdir = str(nb.tmp_path / "out")
        book = str(nb.tmp_path / "book.epub")
        m4b = os.path.join(outdir, "B.m4b")
        nb.rc("python", "0")
        nb.write("python", [m4b])
        bn.narrate_book(book, outdir)
        assert nb.calls() == [self._argv(checkout, book, outdir,
                                         language="fra")]

    def test_with_neither_language_set_the_flag_is_absent(self, nb):
        checkout = _checkout(nb)
        self._env(nb, checkout)
        outdir = str(nb.tmp_path / "out")
        book = str(nb.tmp_path / "book.epub")
        m4b = os.path.join(outdir, "B.m4b")
        nb.rc("python", "0")
        nb.write("python", [m4b])
        bn.narrate_book(book, outdir)
        assert nb.calls() == [self._argv(checkout, book, outdir)]

    def test_an_unset_device_is_an_empty_argument(self, nb, monkeypatch):
        checkout = _checkout(nb)
        self._env(nb, checkout)
        monkeypatch.delenv("narrationDevice")
        outdir = str(nb.tmp_path / "out")
        book = str(nb.tmp_path / "book.epub")
        m4b = os.path.join(outdir, "B.m4b")
        nb.rc("python", "0")
        nb.write("python", [m4b])
        bn.narrate_book(book, outdir)
        assert nb.calls() == [self._argv(checkout, book, outdir, device="")]

    def test_the_engine_configuration_reaches_the_argv(self, nb):
        checkout = _checkout(nb)
        self._env(nb, checkout, device="cpu", fmt="m4a", channel="stereo",
                  engine="bark")
        outdir = str(nb.tmp_path / "out")
        book = str(nb.tmp_path / "book.epub")
        nb.rc("python", "0")
        nb.say("python", "x")
        nb.write("python", [os.path.join(outdir, "B.m4a")])
        got = bn.narrate_book(book, outdir)
        assert got == os.path.join(outdir, "B.m4a")
        assert nb.calls() == [self._argv(checkout, book, outdir, device="cpu",
                                         fmt="m4a", channel="stereo",
                                         engine="bark")]

    def test_a_failed_conversion_is_nothing_even_with_a_leftover_file(self, nb):
        checkout = _checkout(nb)
        self._env(nb, checkout)
        outdir = str(nb.tmp_path / "out")
        book = str(nb.tmp_path / "book.epub")
        m4b = os.path.join(outdir, "B.m4b")
        nb.rc("python", "7")
        nb.write("python", [m4b])
        assert bn.narrate_book(book, outdir) is None

    def test_a_zero_exit_that_left_nothing_is_nothing(self, nb):
        checkout = _checkout(nb)
        self._env(nb, checkout)
        outdir = str(nb.tmp_path / "out")
        book = str(nb.tmp_path / "book.epub")
        nb.rc("python", "0")
        assert bn.narrate_book(book, outdir) is None

    def test_a_zero_exit_that_left_an_empty_file_is_nothing(self, nb):
        checkout = _checkout(nb)
        self._env(nb, checkout)
        outdir = str(nb.tmp_path / "out")
        book = str(nb.tmp_path / "book.epub")
        nb.rc("python", "0")
        nb.write("python", [os.path.join(outdir, "B.m4b")])
        assert bn.narrate_book(book, outdir) is None

    def test_the_newest_file_wins_and_the_extension_is_case_blind(self, nb):
        checkout = _checkout(nb)
        self._env(nb, checkout)
        outdir = nb.tmp_path / "out"
        outdir.mkdir()
        book = str(nb.tmp_path / "book.epub")
        old = outdir / "Old.m4b"
        new = outdir / "New.M4B"
        other = outdir / "notes.txt"
        old.write_text("old\n", encoding="ascii")
        new.write_text("new\n", encoding="ascii")
        other.write_text("x\n", encoding="ascii")
        os.utime(str(old), (1000, 1000))
        os.utime(str(new), (2000, 2000))
        os.utime(str(other), (3000, 3000))
        nb.rc("python", "0")
        got = bn.narrate_book(book, str(outdir))
        assert got == str(new)

    def test_the_newest_file_is_chosen_before_it_is_weighted(self, nb):
        # a newer EMPTY file loses the whole answer, it is not skipped for
        checkout = _checkout(nb)
        self._env(nb, checkout)
        outdir = nb.tmp_path / "out"
        outdir.mkdir()
        book = str(nb.tmp_path / "book.epub")
        old = outdir / "Old.m4b"
        new = outdir / "New.m4b"
        old.write_text("old\n", encoding="ascii")
        new.write_text("", encoding="ascii")
        os.utime(str(old), (1000, 1000))
        os.utime(str(new), (2000, 2000))
        nb.rc("python", "0")
        assert bn.narrate_book(book, str(outdir)) is None

    def test_an_mtime_tie_goes_to_the_larger_path(self, nb):
        checkout = _checkout(nb)
        self._env(nb, checkout)
        outdir = nb.tmp_path / "out"
        outdir.mkdir()
        book = str(nb.tmp_path / "book.epub")
        for name in ("B.m4b", "A.m4b"):
            f = outdir / name
            f.write_text("x\n", encoding="ascii")
            os.utime(str(f), (1700000000.123, 1700000000.123))
        nb.rc("python", "0")
        got = bn.narrate_book(book, str(outdir))
        assert got == str(outdir / "B.m4b")

    def test_a_book_in_a_directory_of_its_own_is_still_found(self, nb):
        """The shell asks find, which recurses. app.py names the audiobook after
        the book's own metadata, so where it puts it is the engine's business -
        and a look at the top level only would report a finished book as
        unproduced and the whole conversion as failed."""
        checkout = _checkout(nb)
        self._env(nb, checkout)
        outdir = nb.tmp_path / "out"
        (outdir / "Author" / "Title").mkdir(parents=True)
        deep = outdir / "Author" / "Title" / "Book.m4b"
        deep.write_text("x\n", encoding="ascii")
        nb.rc("python", "0")
        got = bn.narrate_book(str(nb.tmp_path / "book.epub"), str(outdir))
        assert got == str(deep)

    def test_and_a_nearer_newer_one_still_wins(self, nb):
        checkout = _checkout(nb)
        self._env(nb, checkout)
        outdir = nb.tmp_path / "out"
        (outdir / "sub").mkdir(parents=True)
        deep = outdir / "sub" / "Old.m4b"
        deep.write_text("x\n", encoding="ascii")
        os.utime(str(deep), (1600000000, 1600000000))
        near = outdir / "New.m4b"
        near.write_text("x\n", encoding="ascii")
        os.utime(str(near), (1700000000, 1700000000))
        nb.rc("python", "0")
        got = bn.narrate_book(str(nb.tmp_path / "book.epub"), str(outdir))
        assert got == str(near)

    def test_verbose_runs_mirror_the_engine_to_stderr_and_keep_the_log(self, nb,
                                                                       capfd):
        checkout = _checkout(nb)
        self._env(nb, checkout)
        os.environ["narrationVerbose"] = "1"
        outdir = str(nb.tmp_path / "out")
        book = str(nb.tmp_path / "book.epub")
        m4b = os.path.join(outdir, "B.m4b")
        nb.rc("python", "0")
        nb.write("python", [m4b])
        nb.say("python", "verbose engine line")
        got = bn.narrate_book(book, outdir)
        assert got == m4b
        captured = capfd.readouterr()
        assert captured.err == "verbose engine line"
        with open(os.path.join(outdir, "narration.log"),
                  encoding="ascii") as written:
            assert written.read() == "verbose engine line"

    def test_a_quiet_run_says_nothing_on_stderr(self, nb, capfd):
        checkout = _checkout(nb)
        self._env(nb, checkout)
        outdir = str(nb.tmp_path / "out")
        book = str(nb.tmp_path / "book.epub")
        m4b = os.path.join(outdir, "B.m4b")
        nb.rc("python", "0")
        nb.write("python", [m4b])
        nb.say("python", "quiet engine line")
        got = bn.narrate_book(book, outdir)
        assert got == m4b
        assert capfd.readouterr().err == ""

    def test_the_engine_runs_from_the_checkout_with_its_bin_first(self, nb):
        checkout = nb.tmp_path / "checkout"
        (checkout / "python_env" / "bin").mkdir(parents=True)
        (checkout / "app.py").write_text("x\n", encoding="ascii")
        probe = nb.tmp_path / "probe"
        exe = checkout / "python_env" / "bin" / "python"
        exe.write_text(
            "#!/bin/bash\n"
            'echo "$PATH" > %s\n'
            "pwd >> %s\n"
            "printf 'x' > \"${@: -1}/B.m4b\"\n"
            "exit 0\n" % (probe, probe),
            encoding="ascii")
        os.chmod(str(exe), 0o755)
        os.environ["narrationHome"] = str(checkout)
        os.environ["narrationPython"] = str(exe)
        os.environ["narrationDevice"] = "cpu"
        outdir = str(nb.tmp_path / "out")
        got = bn.narrate_book(str(nb.tmp_path / "book.epub"), outdir)
        assert got is not None
        lines = probe.read_text().splitlines()
        assert lines[0] == str(checkout / "python_env" / "bin") + os.pathsep \
            + os.environ["PATH"]
        assert lines[1] == str(checkout)

    def test_without_a_settled_env_nothing_is_attempted(self, nb):
        outdir = str(nb.tmp_path / "out")
        assert bn.narrate_book(str(nb.tmp_path / "b.epub"), outdir) is None
        assert nb.calls() == []
        assert not os.path.exists(os.path.join(outdir, "narration.log"))


# --- the progress ------------------------------------------------------------------


class TestProgress:
    def _log(self, nb, text):
        path = nb.tmp_path / "narration.log"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_nothing_said_yet_is_nothing(self, nb):
        assert bn.narration_progress(self._log(nb, "")) is None
        assert bn.narration_progress(str(nb.tmp_path / "nope")) is None

    def test_the_last_redraw_wins(self, nb):
        path = self._log(nb,
                         "\r  5%|  | 5/998\n\r 50.5%|## | 504/998\n")
        assert bn.narration_progress(path) == "50"

    def test_the_bar_shape_is_read_to_the_first_percent_pipe(self, nb):
        path = self._log(nb, "Export:  87%|##### | 87/99\n")
        assert bn.narration_progress(path) == "87"

    def test_a_percent_inside_spoken_text_is_not_progress(self, nb):
        path = self._log(nb, "she said 50% of it was done\n")
        assert bn.narration_progress(path) is None

    def test_a_later_shape_overwrites_an_earlier_one(self, nb):
        path = self._log(nb, "12.34%: 12/99\nExport:  45%|## |\n")
        assert bn.narration_progress(path) == "45"

    def test_a_bar_with_no_number_does_not_overwrite(self, nb):
        path = self._log(nb, "45%|## |\nExport:  %|## | 0/99\n")
        assert bn.narration_progress(path) == "45"

    def test_only_the_tail_is_read(self, nb):
        old = "12%|  | 12/99\n"
        padding = "x" * 70000
        path = self._log(nb, old + padding + "\n88%|##### | 88/99\n")
        assert bn.narration_progress(path) == "88"
        monkeypatch_env = "narrationProgressTail"
        os.environ[monkeypatch_env] = "20"
        try:
            assert bn.narration_progress(path) == "88"
        finally:
            del os.environ[monkeypatch_env]

    def test_a_tail_that_is_not_a_number_reads_nothing(self, nb, monkeypatch):
        path = self._log(nb, "45%|## |\n")
        monkeypatch.setenv("narrationProgressTail", "abc")
        assert bn.narration_progress(path) is None

    def test_the_number_is_whole(self, nb):
        path = self._log(nb, "99.9%: 99/99\n")
        assert bn.narration_progress(path) == "99"


# --- the lossless master -------------------------------------------------------------


class TestLosslessMaster:
    def _log(self, nb, text):
        path = nb.tmp_path / "lm.log"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def _flac(self, nb, rel, size=1):
        path = nb.tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        return str(path)

    def test_the_newest_named_master_wins(self, nb):
        first = self._flac(nb, "a/First.flac")
        last = self._flac(nb, "b/Last.flac")
        path = self._log(nb,
                         "Completed -> %s\nCompleted -> %s\n" % (first, last))
        assert bn.narration_lossless_master(path) == last

    def test_a_completed_after_a_slash_is_not_a_completion(self, nb):
        flac = self._flac(nb, "x.flac")
        path = self._log(nb, "see /a/Completed -> %s\n" % flac)
        assert bn.narration_lossless_master(path) is None

    def test_two_completeds_in_the_leading_run_name_one_master(self, nb):
        flac = self._flac(nb, "m.flac")
        path = self._log(nb, "Completed phase Completed -> %s\n" % flac)
        assert bn.narration_lossless_master(path) == flac

    def test_the_extension_is_case_blind(self, nb):
        flac = self._flac(nb, "M.FLAC")
        path = self._log(nb, "Completed -> %s\n" % flac)
        assert bn.narration_lossless_master(path) == flac

    def test_a_chapter_piece_is_not_the_master(self, nb):
        flac = self._flac(nb, "w/chapters/c1.flac")
        path = self._log(nb, "Completed -> %s\n" % flac)
        assert bn.narration_lossless_master(path) is None

    def test_a_missing_or_empty_master_is_walked_over(self, nb):
        absent = str(nb.tmp_path / "absent.flac")
        empty = self._flac(nb, "empty.flac", size=0)
        real = self._flac(nb, "real.flac")
        path = self._log(nb, "\n".join(
            "Completed -> %s" % p for p in (absent, empty, real)))
        assert bn.narration_lossless_master(path) == real

    def test_the_master_must_be_as_long_as_the_audiobook(self, nb, monkeypatch):
        flac = self._flac(nb, "m.flac")
        path = self._log(nb, "Completed -> %s\n" % flac)
        ab = str(nb.tmp_path / "book.m4b")
        durations = {"a": "100.0", "m": "101.0"}
        monkeypatch.setattr(bn, "_media_duration",
                            lambda p: durations["m"]
                            if p == flac else durations["a"])
        assert bn.narration_lossless_master(path, ab) == flac
        durations["m"] = "103.0"
        assert bn.narration_lossless_master(path, ab) is None

    def test_the_tolerance_edge_is_inclusive(self, nb, monkeypatch):
        flac = self._flac(nb, "m.flac")
        path = self._log(nb, "Completed -> %s\n" % flac)
        ab = str(nb.tmp_path / "book.m4b")
        monkeypatch.setattr(bn, "_media_duration",
                            lambda p: "102.0" if p == flac else "100.0")
        assert bn.narration_lossless_master(path, ab) == flac

    def test_an_audiobook_whose_length_is_not_a_number_leaves_no_master(
            self, nb, monkeypatch):
        """The gate is awk's ``r > 0`` over the raw reference, compared as text
        when it is not a number - "N/A" > "0" holds - so the tolerance check IS
        asked for. Its own arithmetic then divides by that zero, which is fatal
        in awk, and the shell's ``|| continue`` reads the death as "not this
        candidate": every candidate is walked past."""
        flac = self._flac(nb, "m.flac")
        path = self._log(nb, "Completed -> %s\n" % flac)
        ab = str(nb.tmp_path / "book.m4b")
        monkeypatch.setattr(bn, "_media_duration",
                            lambda p: "100.0" if p == flac else "N/A")
        assert bn.narration_lossless_master(path, ab) is None

    def test_a_reference_of_zero_asks_for_no_check_at_all(self, nb,
                                                          monkeypatch):
        flac = self._flac(nb, "m.flac")
        path = self._log(nb, "Completed -> %s\n" % flac)
        ab = str(nb.tmp_path / "book.m4b")
        monkeypatch.setattr(bn, "_media_duration",
                            lambda p: "100.0" if p == flac else "0")
        assert bn.narration_lossless_master(path, ab) == flac

    def test_without_an_audiobook_the_duration_is_not_checked(self, nb,
                                                               monkeypatch):
        flac = self._flac(nb, "m.flac")
        path = self._log(nb, "Completed -> %s\n" % flac)

        def boom(p):
            raise AssertionError("ffprobe must not be asked")

        monkeypatch.setattr(bn, "_media_duration", boom)
        assert bn.narration_lossless_master(path) == flac


# --- the session directory --------------------------------------------------------------


class TestSession:
    HOME = "/ck"

    def _log(self, nb, text):
        os.environ["narrationHome"] = self.HOME
        path = nb.tmp_path / "s.log"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_the_id_ends_at_the_first_character_the_engine_would_not_use(self, nb):
        # A comma: a period would not do, because the engine does use one in
        # an id and the boundary is the first character it does not.
        path = self._log(nb, "working in /ck/tmp/proc-abc123_X, more\n")
        assert bn.narration_session_dir(path) == "/ck/tmp/proc-abc123_X"

    def test_the_first_named_session_wins(self, nb):
        path = self._log(nb, "/ck/tmp/proc-one\n/ck/tmp/proc-two\n")
        assert bn.narration_session_dir(path) == "/ck/tmp/proc-one"

    def test_no_session_is_nothing(self, nb):
        assert bn.narration_session_dir(self._log(nb, "nothing\n")) is None

    def test_without_a_home_there_is_no_prefix(self, nb, monkeypatch):
        path = nb.tmp_path / "s.log"
        path.write_text("/ck/tmp/proc-x\n", encoding="utf-8")
        monkeypatch.delenv("narrationHome", raising=False)
        assert bn.narration_session_dir(str(path)) is None

    def test_a_home_with_a_trailing_slash_makes_one_slash(self, nb):
        os.environ["narrationHome"] = "/ck/"
        path = nb.tmp_path / "s.log"
        path.write_text("/ck/tmp/proc-x\n", encoding="utf-8")
        assert bn.narration_session_dir(str(path)) == "/ck/tmp/proc-x"

    def test_drop_removes_the_tree_and_says_nothing(self, nb):
        home = nb.tmp_path / "ck"
        session = home / "tmp" / "proc-gone" / "deep"
        session.mkdir(parents=True)
        (session / "f").write_text("x\n", encoding="ascii")
        os.environ["narrationHome"] = str(home)
        path = nb.tmp_path / "s.log"
        path.write_text(str(session.parent) + "\n", encoding="utf-8")
        assert bn.narration_drop_session(str(path)) == 0
        assert not session.parent.exists()

    def test_drop_refuses_outside_the_checkouts_tmp(self, nb):
        home = nb.tmp_path / "ck"
        other = nb.tmp_path / "elsewhere" / "proc-x"
        other.mkdir(parents=True)
        os.environ["narrationHome"] = str(home)
        path = nb.tmp_path / "s.log"
        path.write_text(str(other) + "\n", encoding="utf-8")
        assert bn.narration_drop_session(str(path)) == 0
        assert other.exists()

    def test_drop_of_a_missing_session_is_silence(self, nb):
        os.environ["narrationHome"] = str(nb.tmp_path / "ck")
        path = nb.tmp_path / "s.log"
        path.write_text("nothing\n", encoding="utf-8")
        assert bn.narration_drop_session(str(path)) == 0

    def _three_folders(self, nb, session="s1"):
        """A checkout holding all three of the engine's per-session folders, and
        the log that names the work one. Returns them in removal order."""
        home = nb.tmp_path / "ck"
        work = home / "tmp" / ("proc-" + session)
        voice = home / "voices" / "__sessions" / ("voice-" + session)
        model = home / "models" / "__sessions" / ("model-" + session)
        for directory in (work, voice / "eng", model):
            directory.mkdir(parents=True)
        (voice / "eng" / "voiceSample.wav").write_text("x\n", encoding="ascii")
        os.environ["narrationHome"] = str(home)
        path = nb.tmp_path / "s.log"
        path.write_text(str(work) + "\n", encoding="utf-8")
        return str(path), work, voice, model

    def test_drop_takes_the_voice_and_model_folders_with_the_work_one(self, nb):
        log, work, voice, model = self._three_folders(nb)
        assert bn.narration_drop_session(log) == 0
        assert not work.exists()
        assert not voice.exists()
        assert not model.exists()

    def test_drop_clears_the_voice_sample_when_the_work_folder_is_already_gone(self, nb):
        log, work, voice, model = self._three_folders(nb)
        shutil.rmtree(work)
        assert bn.narration_drop_session(log) == 0
        assert not voice.exists()
        assert not model.exists()

    def test_drop_leaves_another_books_session_alone(self, nb):
        # Books are read side by side (-j), each under its own id: one finishing
        # must not take the voice sample another is still reading from.
        log, _work, voice, _model = self._three_folders(nb, "s1")
        other = voice.parent / "voice-s2" / "eng"
        other.mkdir(parents=True)
        assert bn.narration_drop_session(log) == 0
        assert other.exists()

    def test_drop_outside_the_checkout_touches_no_voice_folder(self, nb):
        # The refusal is decided before any path is built, so the sibling folders
        # of a session that is not ours are never even named.
        home = nb.tmp_path / "ck"
        voice = home / "voices" / "__sessions" / "voice-x"
        voice.mkdir(parents=True)
        other = nb.tmp_path / "elsewhere" / "proc-x"
        other.mkdir(parents=True)
        os.environ["narrationHome"] = str(home)
        path = nb.tmp_path / "s.log"
        path.write_text(str(other) + "\n", encoding="utf-8")
        assert bn.narration_drop_session(str(path)) == 0
        assert voice.exists()


# --- the voice sample window --------------------------------------------------------------


class TestMediaDuration:
    """``ffprobe ... 2>/dev/null || echo 0`` - and the ``|| echo 0`` APPENDS. A
    probe that prints something and then fails hands back what it printed AND
    the zero, on one stream, which is not the "0" a reader of the status alone
    would expect."""

    def _probe(self, nb, path, rc, out):
        nb.install("ffprobe")
        nb.table("ffprobe", [
            _table_line(["-v", "quiet", "-show_entries", "format=duration",
                         "-of", "default=nk=1:nw=1", path], rc, out)])

    def test_a_probe_that_answers_is_its_answer(self, nb):
        path = str(nb.tmp_path / "a.m4b")
        self._probe(nb, path, 0, "3600.5\n")
        assert bn._media_duration(path) == "3600.5"

    def test_a_probe_that_says_nothing_and_fails_is_zero(self, nb):
        path = str(nb.tmp_path / "a.m4b")
        self._probe(nb, path, 7, "")
        assert bn._media_duration(path) == "0"

    def test_a_probe_that_answers_and_then_fails_is_both(self, nb):
        # no newline of its own, so the appended zero lands against it - and
        # "3600.0" plus "0" is a number, just not that file's duration
        path = str(nb.tmp_path / "a.m4b")
        self._probe(nb, path, 7, "3600.0")
        assert bn._media_duration(path) == "3600.00"

    def test_and_a_whole_line_that_fails_is_two_lines(self, nb):
        path = str(nb.tmp_path / "a.m4b")
        self._probe(nb, path, 7, "abc\n")
        assert bn._media_duration(path) == "abc\n0"

    def test_a_tool_that_is_not_there_is_zero(self, nb):
        assert bn._media_duration(str(nb.tmp_path / "a.m4b")) == "0"


class TestVoiceSampleWindow:
    def _window(self, nb, duration, silence=""):
        nb.install("ffmpeg")
        nb.say("ffmpeg", silence)
        return bn.voice_sample_window(str(nb.tmp_path / "src.wav"), duration)

    def test_the_probe_only_decodes_the_middle(self, nb):
        self._window(nb, "1000")
        assert nb.calls() == [[
            "ffmpeg",
            "-nostdin", "-hide_banner", "-copyts",
            "-ss", "410.000", "-t", "180.000",
            "-i", str(nb.tmp_path / "src.wav"),
            "-af", "silencedetect=noise=-30dB:d=0.3",
            "-f", "null", "-",
        ]]

    def test_a_short_file_probes_from_zero(self, nb):
        self._window(nb, "100")
        call = nb.calls()[0]
        assert call[call.index("-ss") + 1] == "0.000"
        assert call[call.index("-t") + 1] == "100.000"

    def test_a_file_of_exactly_the_search_length_probes_whole(self, nb):
        self._window(nb, "180")
        call = nb.calls()[0]
        assert call[call.index("-ss") + 1] == "0.000"
        assert call[call.index("-t") + 1] == "180.000"

    def test_a_duration_that_is_not_a_number_does_not_clamp_the_window(self, nb):
        """The clamp is awk's ``if (st + len > d)``, and d is the raw duration:
        a duration that does not read as a number is compared as TEXT against
        the window's own length, and "180" loses to "abc" - so the whole search
        window is probed rather than none of it."""
        self._window(nb, "abc\n0")
        call = nb.calls()[0]
        assert call[call.index("-ss") + 1] == "0.000"
        assert call[call.index("-t") + 1] == "180.000"

    def test_a_duration_that_is_a_number_still_clamps(self, nb):
        self._window(nb, "90")
        call = nb.calls()[0]
        assert call[call.index("-t") + 1] == "90.000"

    def test_the_configuration_reaches_the_probe(self, nb, monkeypatch):
        monkeypatch.setenv("voiceSilenceNoise", "-40dB")
        monkeypatch.setenv("voiceSilenceMinDur", "0.5")
        self._window(nb, "1000")
        call = nb.calls()[0]
        assert call[call.index("-af") + 1] == "silencedetect=noise=-40dB:d=0.5"

    def test_no_silence_is_one_stretch_of_the_middle(self, nb):
        assert self._window(nb, "3600") == "1777.500 45.000"

    def test_a_long_gap_splits_the_stretches(self, nb):
        silence = ("[silencedetect] silence_start: 1750\n"
                   "[silencedetect] silence_end: 1800 | silence_duration: 50\n")
        assert self._window(nb, "3600", silence) == "1822.500 45.000"

    def test_a_slice_between_two_gaps_keeps_both_nudges(self, nb):
        silence = ("[silencedetect] silence_start: 5.5\n"
                   "[silencedetect] silence_end: 6.5\n"
                   "[silencedetect] silence_start: 13.5\n"
                   "[silencedetect] silence_end: 14.5\n")
        os.environ["voiceSampleSearchSeconds"] = "10"
        os.environ["voiceSampleSeconds"] = "6"
        try:
            assert self._window(nb, "20", silence) == "7.000 6.000"
        finally:
            del os.environ["voiceSampleSearchSeconds"]
            del os.environ["voiceSampleSeconds"]

    def test_stretches_too_short_give_the_plain_middle(self, nb, monkeypatch):
        monkeypatch.setenv("voiceSampleSearchSeconds", "10")
        monkeypatch.setenv("voiceSampleSeconds", "6")
        monkeypatch.setenv("voiceSampleMinSeconds", "5")
        # The gap runs from before the window starts: it is skipped rather
        # than split on, so the whole window is one stretch that cannot hold
        # the slice - the plain middle of the window, centred.
        silence = ("[silencedetect] silence_start: 2\n"
                   "[silencedetect] silence_end: 8\n")
        assert self._window(nb, "20", silence) == "7.000 6.000"


# --- preparing the voice sample ----------------------------------------------------------


class TestPrepareVoiceSample:
    def _probes(self, nb, src, duration, codec):
        nb.install("ffprobe")
        nb.table("ffprobe", [
            _table_line(["-v", "quiet", "-show_entries", "format=duration",
                         "-of", "default=nk=1:nw=1", src], 0, duration),
            _table_line(["-v", "quiet", "-select_streams", "a:0",
                         "-show_entries", "stream=codec_name", "-of",
                         "default=nk=1:nw=1", src], 0, codec),
        ])

    def test_a_short_pcm_wav_is_passed_through_untouched(self, nb):
        src = nb.tmp_path / "voice.wav"
        src.write_bytes(b"audio")
        self._probes(nb, str(src), "10.000000", "pcm_s16le")
        assert bn.prepare_voice_sample(str(src), str(nb.tmp_path / "s")) == str(src)
        assert nb.calls() == [
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=nk=1:nw=1", str(src)],
            ["ffprobe", "-v", "quiet", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name", "-of",
             "default=nk=1:nw=1", str(src)],
        ]

    def test_a_short_other_codec_is_transcoded_without_a_cut(self, nb):
        src = nb.tmp_path / "voice.mp3"
        src.write_bytes(b"audio")
        self._probes(nb, str(src), "10.000000", "mp3")
        nb.install("ffmpeg")
        sample = str(nb.tmp_path / "s" / "voiceSample.wav")
        nb.rc("ffmpeg", "0")
        nb.say("ffmpeg", "x")
        nb.write("ffmpeg", [sample])
        os.makedirs(os.path.dirname(sample), exist_ok=True)
        assert bn.prepare_voice_sample(str(src),
                                        str(nb.tmp_path / "s")) == sample
        assert nb.calls()[-1] == [
            "ffmpeg",
            "-y", "-nostdin", "-loglevel", "error",
            "-i", str(src), "-map", "0:a:0", "-ac", "1", "-ar", "24000",
            "-c:a", "pcm_s16le", sample,
        ]

    def test_an_over_long_sample_is_cut_at_the_window(self, nb):
        src = nb.tmp_path / "voice.m4a"
        src.write_bytes(b"audio")
        self._probes(nb, str(src), "3600.000000", "aac")
        nb.install("ffmpeg")
        silence = ("[silencedetect] silence_start: 1750\n"
                   "[silencedetect] silence_end: 1800\n")
        nb.say("ffmpeg", silence)
        sample = str(nb.tmp_path / "s" / "voiceSample.wav")
        # Two ffmpeg calls: the window probe, then the transcode - the rc
        # list is consumed in call order.
        nb.rc("ffmpeg", "0 0")
        nb.write("ffmpeg", [sample])
        os.makedirs(os.path.dirname(sample), exist_ok=True)
        logs = []
        assert bn.prepare_voice_sample(
            str(src), str(nb.tmp_path / "s"), logs.append) == sample
        transcode = [c for c in nb.calls()
                     if len(c) > 1 and c[1] == "-y"]
        assert transcode == [[
            "ffmpeg",
            "-y", "-nostdin", "-loglevel", "error",
            "-ss", "1822.500", "-t", "45.000",
            "-i", str(src), "-map", "0:a:0", "-ac", "1", "-ar", "24000",
            "-c:a", "pcm_s16le", sample,
        ]]
        assert logs == ["Voice sample is 1:00:00 long: taking 0:45 of speech "
                        "from 30:23"]

    def test_a_duration_that_is_not_a_number_is_an_over_long_file(self, nb):
        """awk compares two strnums as TEXT when either is not a number, and
        "N/A" > "60" holds - so a file ffprobe could not read the length of takes
        the CUT branch, not the pass-through one. A port that coerced both sides
        would call it a file of no length and hand the whole thing over."""
        src = nb.tmp_path / "voice.wav"
        src.write_bytes(b"audio")
        self._probes(nb, str(src), "N/A", "pcm_s16le")
        nb.install("ffmpeg")
        sample = str(nb.tmp_path / "s" / "voiceSample.wav")
        # non-empty, because the stub writes its canned output into the file the
        # write list names and a zero-byte sample is no sample
        nb.say("ffmpeg", "no silence here\n")
        nb.rc("ffmpeg", "0 0")
        nb.write("ffmpeg", ["-", sample])
        os.makedirs(os.path.dirname(sample), exist_ok=True)
        assert bn.prepare_voice_sample(str(src), str(nb.tmp_path / "s")) == sample
        # the silence probe ran, which is what "over-long" means here
        assert nb.calls()[2][:4] == ["ffmpeg", "-nostdin", "-hide_banner",
                                     "-copyts"]

    def test_exactly_the_ceiling_is_not_long(self, nb):
        src = nb.tmp_path / "voice.wav"
        src.write_bytes(b"audio")
        self._probes(nb, str(src), "60.000000", "pcm_s16le")
        assert bn.prepare_voice_sample(str(src), str(nb.tmp_path / "s")) == str(src)

    def test_no_codec_is_no_sample(self, nb):
        src = nb.tmp_path / "voice.bin"
        src.write_bytes(b"audio")
        self._probes(nb, str(src), "10.000000", "")
        assert bn.prepare_voice_sample(str(src), str(nb.tmp_path / "s")) is None

    def test_a_failed_transcode_is_no_sample(self, nb):
        src = nb.tmp_path / "voice.mp3"
        src.write_bytes(b"audio")
        self._probes(nb, str(src), "10.000000", "mp3")
        nb.install("ffmpeg")
        nb.rc("ffmpeg", "7")
        os.makedirs(str(nb.tmp_path / "s"), exist_ok=True)
        assert bn.prepare_voice_sample(str(src), str(nb.tmp_path / "s")) is None

    def test_a_missing_file_is_no_sample(self, nb):
        assert bn.prepare_voice_sample(str(nb.tmp_path / "nope"),
                                       str(nb.tmp_path / "s")) is None


# --- naming the languages ---------------------------------------------------------------


class TestVoiceSampleLanguage:
    @pytest.mark.parametrize("name,expected", [
        ("deu.wav", "deu"),
        ("DEU.MP3", "deu"),
        ("german.m4a", "deu"),
        ("narrator_de.wav", "deu"),
        ("voice sample fra.flac", "fra"),
        ("de.", "deu"),
        ("default.mp3", "-"),
        ("ANY.wav", "-"),
        ("Fallback", "-"),
        ("other", "-"),
        ("xyzzy.wav", ""),
        ("notes.deu.flac", "deu"),
        (".hidden", ""),
        ("english.deu.ogg", "deu"),
    ])
    def test_the_name_says_one_language(self, nb, name, expected):
        assert bn.voice_sample_language("/voices/" + name) == expected

    def test_the_fallback_names_are_configured(self, nb, monkeypatch):
        monkeypatch.setenv("narrationVoiceFallbackNames", "base")
        assert bn.voice_sample_language("base.mp3") == "-"
        assert bn.voice_sample_language("default.mp3") == ""


# --- the voice map ------------------------------------------------------------------------


class TestPrepareVoiceSamples:
    def _probes(self, nb, src, duration, codec):
        nb.install("ffprobe")
        lines = [
            _table_line(["-v", "quiet", "-show_entries", "format=duration",
                         "-of", "default=nk=1:nw=1", src], 0, duration),
            _table_line(["-v", "quiet", "-select_streams", "a:0",
                         "-show_entries", "stream=codec_name", "-of",
                         "default=nk=1:nw=1", src], 0, codec),
        ]
        # A test may probe more than one file: the table accumulates, because
        # each line answers only the call of exactly its own argv.
        table = nb.out_dir / "ffprobe.table"
        old = table.read_text().splitlines() if table.exists() else []
        nb.table("ffprobe", old + lines)

    def test_a_single_file_is_the_fallback(self, nb):
        src = nb.tmp_path / "given" / "voice.wav"
        src.parent.mkdir()
        src.write_bytes(b"audio")
        self._probes(nb, str(src), "10.000000", "pcm_s16le")
        scratch = str(nb.tmp_path / "scratch")
        os.makedirs(scratch, exist_ok=True)
        got = bn.prepare_voice_samples(str(src), scratch)
        expected = os.path.join(scratch, "voices", "fallback",
                                "voiceSample.wav")
        assert got == "-\t" + expected
        with open(expected, "rb") as handle:
            assert handle.read() == b"audio"

    def test_a_single_file_that_cannot_be_read_is_no_map(self, nb):
        src = nb.tmp_path / "given" / "broken.bin"
        src.parent.mkdir()
        src.write_bytes(b"audio")
        self._probes(nb, str(src), "10.000000", "")
        scratch = str(nb.tmp_path / "scratch")
        os.makedirs(scratch, exist_ok=True)
        assert bn.prepare_voice_samples(str(src), scratch) is None

    def test_a_directory_is_one_voice_per_language(self, nb):
        voices = nb.tmp_path / "voices"
        voices.mkdir()
        deu = voices / "deu.wav"
        deu.write_bytes(b"deu-audio")
        default = voices / "default.mp3"
        default.write_bytes(b"def-audio")
        notes = voices / "notes.txt"
        notes.write_text("no language here\n", encoding="ascii")
        self._probes(nb, str(deu), "10.000000", "pcm_s16le")
        self._probes(nb, str(default), "10.000000", "mp3")
        nb.install("ffmpeg")
        os.makedirs(str(nb.tmp_path / "scratch"), exist_ok=True)
        nb.rc("ffmpeg", "0")
        nb.say("ffmpeg", "x")
        # The fallback name is language "-", and the module makes the
        # directory the transcode writes into: the stub's write needs it to
        # exist.
        nb.write("ffmpeg", [
            str(nb.tmp_path / "scratch" / "voices" / "-"
                / "voiceSample.wav"),
        ])
        logs = []
        got = bn.prepare_voice_samples(str(voices),
                                        str(nb.tmp_path / "scratch"),
                                        logs.append)
        # The map is in the walk's order: find -maxdepth 1 | sort, so
        # default.mp3 comes before deu.wav.
        expected = "\n".join([
            "-\t" + str(nb.tmp_path / "scratch" / "voices" / "-"
                          / "voiceSample.wav"),
            "deu\t" + str(nb.tmp_path / "scratch" / "voices" / "deu"
                          / "voiceSample.wav"),
        ])
        assert got == expected
        with open(nb.tmp_path / "scratch" / "voices" / "deu"
                  / "voiceSample.wav", "rb") as sample:
            assert sample.read() == b"deu-audio"
        assert logs == ['Voice sample "notes.txt" names no language - '
                        'ignored. Name it after the',
                        '         language it speaks (deu.wav, german.m4a, '
                        'de.mp3), or "default".']

    def test_a_second_sample_for_the_same_voice_is_ignored(self, nb):
        voices = nb.tmp_path / "voices"
        voices.mkdir()
        first = voices / "de_1.wav"
        first.write_bytes(b"one")
        second = voices / "de_2.wav"
        second.write_bytes(b"two")
        self._probes(nb, str(first), "10.000000", "pcm_s16le")
        self._probes(nb, str(second), "10.000000", "pcm_s16le")
        os.makedirs(str(nb.tmp_path / "scratch"), exist_ok=True)
        logs = []
        got = bn.prepare_voice_samples(str(voices),
                                       str(nb.tmp_path / "scratch"),
                                       logs.append)
        assert got.count("deu\t") == 1
        assert logs == ['Voice sample "de_2.wav" is a second sample for the '
                        'same voice - ignored.']

    def test_a_sample_no_audio_could_be_read_from_clears_its_own_dir(self, nb):
        voices = nb.tmp_path / "voices"
        voices.mkdir()
        good = voices / "deu.wav"
        good.write_bytes(b"good")
        bad = voices / "fra.bin"
        bad.write_bytes(b"bad")
        self._probes(nb, str(good), "10.000000", "pcm_s16le")
        self._probes(nb, str(bad), "10.000000", "")
        os.makedirs(str(nb.tmp_path / "scratch"), exist_ok=True)
        logs = []
        got = bn.prepare_voice_samples(str(voices),
                                       str(nb.tmp_path / "scratch"),
                                       logs.append)
        assert "fra\t" not in (got or "")
        assert not (nb.tmp_path / "scratch" / "voices" / "fra").exists()
        assert 'No audio could be read from the voice sample "fra.bin" - ' \
            "ignored." in logs

    def test_an_empty_directory_is_no_map(self, nb):
        empty = nb.tmp_path / "empty"
        empty.mkdir()
        os.makedirs(str(nb.tmp_path / "scratch"), exist_ok=True)
        assert bn.prepare_voice_samples(str(empty),
                                        str(nb.tmp_path / "scratch")) is None

    def test_nothing_given_is_no_map(self, nb):
        assert bn.prepare_voice_samples(str(nb.tmp_path / "nowhere"),
                                        str(nb.tmp_path / "scratch")) is None


class TestVoiceSampleFor:
    MAP = "deu\t/a/deu.wav\nfra\t/b/fra.wav\n-\t/c/fallback.wav"

    def _map(self, nb, value=MAP):
        os.environ["narrationVoiceMap"] = value

    def test_its_own_language_wins(self, nb):
        self._map(nb)
        assert bn.voice_sample_for("deu") == "/a/deu.wav"

    def test_without_one_the_fallback_is_the_answer(self, nb):
        self._map(nb)
        assert bn.voice_sample_for("spa") == "/c/fallback.wav"
        assert bn.voice_sample_for(None) == "/c/fallback.wav"
        assert bn.voice_sample_for("") == "/c/fallback.wav"

    def test_without_a_map_there_is_no_voice(self, nb):
        assert bn.voice_sample_for("deu") == ""

    def test_a_path_may_hold_a_tab(self, nb):
        self._map(nb, "deu\t/a\tb.wav\n-\t/c.wav")
        assert bn.voice_sample_for("deu") == "/a\tb.wav"

    def test_a_later_fallback_entry_wins(self, nb):
        self._map(nb, "-\t/first.wav\n-\t/second.wav")
        assert bn.voice_sample_for("deu") == "/second.wav"

    def test_a_line_without_a_tab_names_no_path(self, nb):
        self._map(nb, "deu\n-\t/c.wav")
        assert bn.voice_sample_for("deu") == "/c.wav"


# --- the nvidia question ------------------------------------------------------------------


class TestNvidia:
    def test_has_nvidia_asks_for_a_listing(self, nb):
        nb.install("nvidia-smi")
        nb.rc("nvidia-smi", "0")
        assert bn.narration_has_nvidia() is True
        assert nb.calls() == [["nvidia-smi", "-L"]]

    def test_a_broken_driver_answers_false(self, nb):
        nb.install("nvidia-smi")
        nb.rc("nvidia-smi", "1")
        assert bn.narration_has_nvidia() is False

    def test_without_the_tool_there_is_no_gpu(self, nb):
        assert bn.narration_has_nvidia() is False
        assert nb.calls() == []

    def test_the_free_vram_is_the_first_line_cleaned(self, nb):
        nb.install("nvidia-smi")
        nb.table("nvidia-smi", [
            _table_line(["-L"], 0, "GPU 0: RTX\n"),
            _table_line(["--query-gpu=memory.free",
                         "--format=csv,noheader,nounits"], 0,
                        "  24576\n1234\n"),
        ])
        assert bn.narration_free_vram() == "24576"

    def test_a_non_numeric_answer_is_no_vram(self, nb):
        nb.install("nvidia-smi")
        nb.table("nvidia-smi", [
            _table_line(["-L"], 0, "GPU 0: RTX\n"),
            _table_line(["--query-gpu=memory.free",
                         "--format=csv,noheader,nounits"], 0, "N/A\n"),
        ])
        assert bn.narration_free_vram() is None

    @pytest.mark.parametrize("free,per,expected", [
        ("24576", "5", "4"),
        ("24576", "8", "3"),
        ("4096", "5", "1"),
        ("24576", "0", ""),
    ])
    def test_the_budget_is_never_zero(self, nb, free, per, expected):
        nb.install("nvidia-smi")
        nb.table("nvidia-smi", [
            _table_line(["-L"], 0, "GPU 0: RTX\n"),
            _table_line(["--query-gpu=memory.free",
                         "--format=csv,noheader,nounits"], 0, free + "\n"),
        ])
        os.environ["narrationVramPerBookGB"] = per
        assert bn.narration_job_budget("cuda") == expected

    def test_a_non_cuda_device_is_one(self, nb):
        assert bn.narration_job_budget("cpu") == "1"
        assert bn.narration_job_budget("") == "1"