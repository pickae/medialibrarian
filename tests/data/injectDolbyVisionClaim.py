#!/usr/bin/env python3
"""Write a Dolby Vision configuration record into a Matroska video track that has
none, so the container CLAIMS Dolby Vision over a video carrying no RPU.

That is the one Dolby Vision shape no encoder can produce, and so the one the
generated-media tier could not reach before: ffmpeg makes such a file by copying
the dvcC of a real profile 7 source onto a re-encoded stream, and an
already-ingested library is full of the result. Written from scratch here, so the
fixture needs no real film.

usage: injectDolbyVisionClaim.py <in.mkv> <out.mkv> [profile] [level] [compatId]

The default - version 1.0, profile 7, level 6, BL+EL+RPU all flagged present,
compatibility id 6 (Blu-ray) - is byte for byte the record real false-claim files
carry.

SeekHead and Cues are overwritten with EBML Void of exactly their own size rather
than updated: growing the Tracks element moves everything after it and their
stored positions would then be wrong. Remuxing the result with mkvmerge rebuilds
both, and that pass is what turns this into an ordinary file.
"""

import sys

SEGMENT = b"\x18\x53\x80\x67"
SEEK_HEAD = b"\x11\x4d\x9b\x74"
CUES = b"\x1c\x53\xbb\x6b"
TRACKS = b"\x16\x54\xae\x6b"
TRACK_ENTRY = b"\xae"
VOID = b"\xec"
MAX_BLOCK_ADDITION_ID = b"\x55\xee"
BLOCK_ADDITION_MAPPING = b"\x41\xe4"
BLOCK_ADD_ID_VALUE = b"\x41\xf0"
BLOCK_ADD_ID_NAME = b"\x41\xa4"
BLOCK_ADD_ID_TYPE = b"\x41\xe7"
BLOCK_ADD_ID_EXTRA_DATA = b"\x41\xed"


def vint_width(first):
    if first == 0:
        raise ValueError("invalid EBML vint")
    return 8 - first.bit_length() + 1


def read_size(buf, pos):
    """The EBML size at buf[pos:] as (value, width); value None when unknown."""
    width = vint_width(buf[pos])
    value = buf[pos] & (0xFF >> width)
    for byte in buf[pos + 1:pos + width]:
        value = (value << 8) | byte
    unknown = (1 << (7 * width)) - 1
    return (None if value == unknown else value), width


def write_size(value, width=None):
    """value as an EBML size vint, in `width` bytes when one is demanded."""
    for candidate in ([width] if width else range(1, 9)):
        if value < (1 << (7 * candidate)) - 1:
            return (value | (1 << (7 * candidate))).to_bytes(candidate, "big")
    raise ValueError("size %d does not fit in %s bytes" % (value, width))


def element(eid, payload):
    return eid + write_size(len(payload)) + payload


def uint(value):
    return value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")


class Span:
    """One EBML element: where its id, size vint and payload sit in the buffer."""

    def __init__(self, eid, start, size_pos, size_width, size):
        self.id = eid
        self.start = start
        self.size_pos = size_pos
        self.size_width = size_width
        self.body = size_pos + size_width
        self.size = size

    @property
    def stop(self):
        return self.body + (self.size or 0)


def children(buf, start, end):
    pos = start
    while pos < end - 1:
        id_width = vint_width(buf[pos])
        size, size_width = read_size(buf, pos + id_width)
        span = Span(bytes(buf[pos:pos + id_width]), pos,
                    pos + id_width, size_width, size)
        yield span
        pos = end if size is None else span.stop


def find(buf, start, end, wanted):
    for span in children(buf, start, end):
        if span.id == wanted:
            return span
    raise LookupError("no %s element in this Matroska file" % wanted.hex())


def dovi_record(profile, level, compat_id):
    """A 24-byte DOVIDecoderConfigurationRecord: 7 bits of profile, 6 of level,
    then the rpu/el/bl present flags, then the base-layer compatibility id."""
    packed = (profile << 9) | (level << 3) | 0b111
    return bytes([1, 0, packed >> 8, packed & 0xFF, compat_id << 4]) + bytes(19)


def blank(buf, span):
    """Replace one element with a Void of exactly the same total size."""
    total = span.stop - span.start
    for width in range(1, 9):
        payload = total - 1 - width
        if payload >= 0 and payload < (1 << (7 * width)) - 1:
            buf[span.start:span.stop] = (VOID + write_size(payload, width)
                                         + bytes(payload))
            return
    raise ValueError("cannot void %d bytes" % total)


def resize(buf, span, payload_len):
    """Rewrite one element's size vint. Returns how many bytes it grew by."""
    new = write_size(payload_len)
    buf[span.size_pos:span.body] = new
    return len(new) - span.size_width


def main():
    if not 3 <= len(sys.argv) <= 6:
        sys.exit(__doc__)
    src, dst = sys.argv[1], sys.argv[2]
    profile = int(sys.argv[3]) if len(sys.argv) > 3 else 7
    level = int(sys.argv[4]) if len(sys.argv) > 4 else 6
    compat_id = int(sys.argv[5]) if len(sys.argv) > 5 else 6

    with open(src, "rb") as handle:
        buf = bytearray(handle.read())

    segment = find(buf, 0, len(buf), SEGMENT)
    seg_end = segment.stop if segment.size else len(buf)

    for span in list(children(buf, segment.body, seg_end)):
        if span.id in (SEEK_HEAD, CUES):
            blank(buf, span)

    tracks = find(buf, segment.body, seg_end, TRACKS)
    entry = find(buf, tracks.body, tracks.stop, TRACK_ENTRY)

    added = element(MAX_BLOCK_ADDITION_ID, uint(1)) \
        + element(BLOCK_ADDITION_MAPPING,
                  element(BLOCK_ADD_ID_VALUE, uint(1))
                  + element(BLOCK_ADD_ID_NAME, b"Dolby Vision configuration")
                  + element(BLOCK_ADD_ID_TYPE, uint(0x64766343))  # 'dvcC'
                  + element(BLOCK_ADD_ID_EXTRA_DATA,
                            dovi_record(profile, level, compat_id)))

    # Innermost first: every rewritten size vint may itself need another byte,
    # which the next element out has to count as part of its own payload.
    buf[entry.stop:entry.stop] = added
    grown = len(added)
    grown += resize(buf, entry, entry.size + grown)
    grown += resize(buf, tracks, tracks.size + grown)
    if segment.size is not None:
        resize(buf, segment, segment.size + grown)

    with open(dst, "wb") as handle:
        handle.write(bytes(buf))


if __name__ == "__main__":
    main()
