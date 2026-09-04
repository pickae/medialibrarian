"""Write a dv_level into a Matroska file's Dolby Vision configuration record.

Mechanism only - which level a file SHOULD declare is
:func:`medialib.lib.dolbyvision.expected_config_level`'s decision.

The level is six bits of the dvcC/dvvC block addition mapping, a container
element attached to the video track and nothing to do with the HEVC bitstream.
Correcting it is therefore a two-byte edit at a fixed offset near the front of
the file, not a remux: instant whatever the file's size, and it cannot touch a
single frame of video.

Two bytes still cannot simply be written, which is why this is a Python helper
and not four lines of dd: some muxers put a CRC-32 on the master element holding
the record (ffmpeg does, mkvmerge does not), and a patch that left it stale would
leave a knowingly corrupt checksum behind. So the walk down to the record keeps
the master elements it passed through, and every CRC-32 among them is recomputed
afterwards, innermost first - an outer element's CRC covers the inner one's, so
it has to be settled last.

:func:`correct_level` answers 0 on success and 1 when the file holds no single
configuration record to patch - which the caller reads as "leave this file
alone", not as a hard error.
"""

import sys
import zlib

# The record lives in the Tracks element, which every muxer writes before the
# first frame; reading a megabyte reaches it without pulling in a 20 GB film.
HEADER_BYTES = 1 << 20

MASTER_IDS = {
    b"\x18\x53\x80\x67",  # Segment
    b"\x16\x54\xae\x6b",  # Tracks
    b"\xae",              # TrackEntry
    b"\xe0",              # Video
    b"\x41\xe4",          # BlockAdditionMapping
}
BLOCK_ADD_ID_EXTRA_DATA = b"\x41\xed"
CRC32_ID = b"\xbf"


def read_id(buf, pos):
    b0 = buf[pos]
    for n, mask in ((1, 0x80), (2, 0x40), (3, 0x20), (4, 0x10)):
        if b0 & mask:
            return bytes(buf[pos:pos + n]), pos + n
    raise ValueError(f"not an EBML element id at {pos}")


def read_size(buf, pos):
    b0 = buf[pos]
    for n in range(1, 9):
        if b0 & (0x80 >> (n - 1)):
            val = b0 & (0xFF >> n)
            for i in range(1, n):
                val = (val << 8) | buf[pos + i]
            unknown = val == (1 << (7 * n)) - 1
            return (None if unknown else val), pos + n
    raise ValueError(f"not an EBML size at {pos}")


def find_config_record(buf):
    """(payloadStart, enclosingMasters) for the one dvcC/dvvC payload in the
    header, or None when there is not exactly one to be sure about."""
    found = []

    def walk(start, end, path):
        pos = start
        while pos < end:
            try:
                eid, p = read_id(buf, pos)
                size, p = read_size(buf, p)
            except (ValueError, IndexError):
                return                      # a truncated or unexpected element
            if size is None:                # unknown length: descend, no bound
                size = end - p
            cend = min(p + size, end)
            if eid == BLOCK_ADD_ID_EXTRA_DATA:
                # A DOVIDecoderConfigurationRecord opens with dv_version_major=1,
                # dv_version_minor=0. Other block addition types exist and must
                # not be mistaken for one.
                if cend - p >= 5 and buf[p:p + 2] == b"\x01\x00":
                    found.append((p, list(path)))
            elif eid in MASTER_IDS:
                path.append((p, cend))
                walk(p, cend, path)
                path.pop()
            pos = cend

    walk(0, len(buf), [])
    return found[0] if len(found) == 1 else None


def set_level(path, new_level):
    with open(path, "rb") as fh:
        buf = bytearray(fh.read(HEADER_BYTES))
    hit = find_config_record(buf)
    if hit is None:
        return False
    payloadStart, masters = hit

    # Bytes 2..3 pack dv_profile (7 bits), dv_level (6), then the rpu / el / bl
    # present flags; only the middle field changes.
    word = (buf[payloadStart + 2] << 8) | buf[payloadStart + 3]
    word = (word & ~(0x3F << 3)) | ((new_level & 0x3F) << 3)
    buf[payloadStart + 2] = word >> 8
    buf[payloadStart + 3] = word & 0xFF

    # Every edit is settled in the buffer first and only then written back, and
    # written back as the six or ten bytes that actually changed rather than as
    # the whole megabyte that was read. This is somebody's film: the smaller the
    # write, the less a crash halfway through it can cost.
    edits = [(payloadStart + 2, bytes(buf[payloadStart + 2:payloadStart + 4]))]

    for contentStart, contentEnd in reversed(masters):
        if buf[contentStart:contentStart + 1] != CRC32_ID:
            continue                        # this muxer wrote no CRC here
        size, valueAt = read_size(buf, contentStart + 1)
        if size != 4:
            continue
        crc = zlib.crc32(bytes(buf[valueAt + 4:contentEnd])) & 0xFFFFFFFF
        buf[valueAt:valueAt + 4] = crc.to_bytes(4, "little")
        edits.append((valueAt, bytes(buf[valueAt:valueAt + 4])))

    with open(path, "r+b") as fh:
        for offset, data in edits:
            fh.seek(offset)
            fh.write(data)
        fh.flush()
    return True


def correct_level(path, level) -> int:
    """Patch <path>'s record to <level>: 0 done, 1 nothing to patch or unreadable.

    Silent, and a status rather than an exception, because the caller runs this
    over every file it produces and reads the answer as advice - the level is
    re-probed afterwards either way.
    """
    try:
        return 0 if set_level(path, int(level)) else 1
    except (OSError, ValueError):
        return 1


def main(argv):
    if len(argv) != 3:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    try:
        level = int(argv[2])
    except ValueError:
        print(f"not a Dolby Vision level: {argv[2]}", file=sys.stderr)
        return 2
    if not 1 <= level <= 63:
        print(f"level out of range: {level}", file=sys.stderr)
        return 2
    try:
        if not set_level(argv[1], level):
            print("no single Dolby Vision configuration record found", file=sys.stderr)
            return 1
    except OSError as exc:
        print(f"{argv[1]}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
