# /// script
# requires-python = ">=3.10"
# dependencies = ["whisper-ctranslate2", "av<19", "nvidia-cublas-cu12"]
# ///
"""whisper-ctranslate2, run with a PyAV it can still open audio with and the
CUDA libraries its ctranslate2 was built against.

    whisper_transcribe.py <whisper-ctranslate2's own arguments>

faster-whisper opens every file it is handed with an argument PyAV 19 no longer
takes, so with the newest PyAV - which is what an unpinned install resolves to -
every transcription fails before it starts. Pinning PyAV below 19 here, where
the dependencies are declared, keeps ``pipx run`` building an environment that
works; the pin can go once faster-whisper stops passing that argument.

ctranslate2 4 is built against CUDA 12 and loads its cuBLAS by that version's
name, which a host that has moved on to a newer CUDA no longer has - and then
the GPU cannot run whisper at all. NVIDIA's own wheel of that cuBLAS is
declared here and loaded before ctranslate2 asks for it, so the GPU works
whichever CUDA the host has installed.
"""

import ctypes
import glob
import os
import sys


def _load_cublas():
    try:
        import nvidia.cublas
    except ImportError:
        return
    for folder in nvidia.cublas.__path__:
        # the Lt library first: the other one needs it
        for name in ("libcublasLt.so.*", "libcublas.so.*"):
            for path in glob.glob(os.path.join(folder, "lib", name)):
                try:
                    ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL)
                except OSError:
                    pass


_load_cublas()

from whisper_ctranslate2.whisper_ctranslate2 import main  # noqa: E402

if __name__ == "__main__":
    sys.argv[0] = "whisper-ctranslate2"
    sys.exit(main())
