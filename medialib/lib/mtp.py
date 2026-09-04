"""A phone folder reached over MTP.

A folder on a phone mounted over MTP has two spellings: the ``mtp://`` URI a
file manager puts on the clipboard, and the gvfs FUSE path that is the only
one of the two a directory. The port translates the first into the second,
and probes whether a mount will take a rename - the two judgements
``clean-folder-structure`` makes before it trusts a phone with a rename.
"""

import os
import shutil

_MOUNT_PREFIX = "mtp:host="
_HEX_DIGITS = b"0123456789abcdefABCDEF"


def mtp_mount_root() -> str:
    """Where gvfs puts its mounts: ``$CFS_GVFS_ROOT``, else
    ``$XDG_RUNTIME_DIR/gvfs``, else ``/run/user/<uid>/gvfs``.

    The first that is set and non-empty wins, the way the bash's nested
    ``:-`` defaults read it.
    """
    root = os.environ.get("CFS_GVFS_ROOT")
    if root:
        return root
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return os.path.join(xdg, "gvfs")
    # gvfs, and the /run/user/<uid> it lives under, are Linux's. macOS reaches
    # a phone through Android File Transfer, which is not a filesystem at all,
    # and Windows through its own shell namespace - neither is a directory this
    # can walk, and on Windows there is not even a uid to name one with. The
    # conventional path is still the answer: it does not exist there, the walk
    # below finds nothing, and the run falls back to adb - which is the same
    # thing it does for a phone that is simply not mounted.
    uid = os.getuid() if hasattr(os, "getuid") else 0
    return "/run/user/{}/gvfs".format(uid)


def percent_decode(name: str) -> str:
    """The bash's ``_percentDecode``: each ``%XX`` becomes the byte it names,
    everything else - including a backslash - stays.

    The bash does this in bytes, its strings being byte strings, so a
    multi-byte character spelled as escapes is one character in the answer,
    and the port decodes bytes the same way and reads them back in the
    filesystem's encoding. The bash's own doubling of backslashes and its
    ``printf %b`` cancel out: a doubled pair prints as one backslash, and the
    backslash the ``%XX``-to-``\\xXX`` rewrite inserts is always the one the
    ``%b`` pairs the escape with, so the composition is a plain percent
    decode. The corpus pins it with names that hold backslashes next to
    escapes.

    A ``%00`` cannot survive into the bash's value, because the answer is a
    command substitution and one drops null bytes; the port drops them the
    same way. No name on a phone ever holds one.
    """
    raw = name.encode("utf-8", "surrogateescape")
    out = bytearray()
    i = 0
    while i < len(raw):
        if (
            raw[i] == 0x25
            and i + 2 < len(raw)
            and raw[i + 1] in _HEX_DIGITS
            and raw[i + 2] in _HEX_DIGITS
        ):
            out.append(int(raw[i + 1 : i + 3], 16))
            i += 3
        else:
            out.append(raw[i])
            i += 1
    return out.decode("utf-8", "surrogateescape").replace("\x00", "")


def mtp_mount_can_rename(directory: str) -> bool:
    """The bash's ``mtpMountCanRename``: whether renames can be applied on
    this mount, answered by doing one - a scratch folder is created inside
    ``directory``, renamed and removed again.

    ``CFS_MTP_FORCE_ADB=1`` answers no without probing.
    """
    if os.environ.get("CFS_MTP_FORCE_ADB", "0") == "1":
        return False
    probe = os.path.join(directory, ".cfsRenameProbe.{}".format(os.getpid()))
    try:
        os.mkdir(probe)
    except OSError:
        return False
    try:
        os.rename(probe, probe + ".renamed")
    except OSError:
        _remove_quietly(probe)
        return False
    _remove_quietly(probe + ".renamed")
    return True


def _remove_quietly(path: str) -> None:
    """The bash's ``rmdir x 2>/dev/null || rm -rf x 2>/dev/null``."""
    try:
        os.rmdir(path)
    except OSError:
        shutil.rmtree(path, ignore_errors=True)


def resolve_mtp_uri(uri: str) -> tuple[str, str]:
    """The bash's ``resolveMtpUri``: the gvfs path of the folder a ``mtp://``
    URI names, or the one-line reason there is none.

    Returns ``(path, error)``: on success the path and an empty error, on
    failure an empty path and the reason the bash leaves in ``RET_MTP_ERROR``.
    """
    rest = uri[6:] if uri.startswith("mtp://") else uri
    host, slash, rel = rest.partition("/")
    host = percent_decode(host)
    rel = percent_decode(rel) if slash else ""

    root = mtp_mount_root()
    mounts: list[str] = []
    matched = ""
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        entries = []
    for entry in entries:
        if not entry.startswith(_MOUNT_PREFIX):
            continue
        mount = os.path.join(root, entry)
        if not os.path.isdir(mount):
            continue
        mounts.append(mount)
        if percent_decode(entry[len(_MOUNT_PREFIX):]) == host:
            matched = mount

    if not matched:
        if len(mounts) == 1:
            matched = mounts[0]
        elif not mounts:
            return "", (
                "no phone is mounted under {}. Open it once in the file manager"
                " - that is what creates the mount - then retry.".format(root)
            )
        else:
            return "", 'none of the phones mounted under {} is "{}".'.format(
                root, host
            )

    path = matched if not rel else matched + "/" + rel
    if not os.path.isdir(path):
        return "", "not a folder on the mounted phone: {}".format(path)
    return path, ""