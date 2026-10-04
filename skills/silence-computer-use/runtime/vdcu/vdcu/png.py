"""PNG encode/decode in pure standard library.

The backend must ship without Pillow (the whole point is that an arbitrary agent
host can run it), but it still needs to:

* turn the GDI capture buffer into a PNG the agent host can render,
* read those PNGs back to measure how much of a frame is non-black — the check
  that distinguishes "the virtual desktop really rendered" from "captured a
  black, never-composited desktop",
* downscale a frame for the live-view page.

Filter type 0 (None) is used for encoding.  All five filter types are supported
when decoding, because a frame may have been produced elsewhere.
"""

from __future__ import annotations

import struct
import zlib

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class PngError(ValueError):
    pass


# --------------------------------------------------------------------------- #
# Encoding
# --------------------------------------------------------------------------- #


def _chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def encode_rgb(rows: list[bytes], width: int, height: int, level: int = 6, filter_type: str = "none") -> bytes:
    """Encode tightly packed RGB scanlines (``width * 3`` bytes each) as PNG.

    Always filter type 0.  An attempt at Up filtering (differencing each row against
    the previous one) used big-integer arithmetic for speed, but integer subtraction
    lets borrows propagate *across* bytes, while the PNG filter is defined per byte
    modulo 256 — decoding gave ``0000fe`` where the input was ``0000ff``.  Doing it
    correctly needs byte-wise SWAR masking, and it is not worth it: photographic
    frames go through :mod:`vdcu.jpeg` (GDI+), which is both smaller and faster.
    Filtering stays available for callers that measure a benefit.
    """

    if len(rows) != height:
        raise PngError(f"expected {height} scanlines, got {len(rows)}")
    stride = width * 3
    use_up = filter_type == "up" and height > 1

    raw = bytearray()
    previous = bytes(stride)
    for index, row in enumerate(rows):
        if len(row) != stride:
            raise PngError(f"scanline {index} has {len(row)} bytes, expected {stride}")
        if use_up and index > 0:
            raw.append(2)  # filter type 2 (Up), byte-wise modulo 256
            raw.extend(bytes((value - previous[position]) & 0xFF for position, value in enumerate(row)))
        else:
            raw.append(0)  # filter type 0 (None)
            raw.extend(row)
        previous = row

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"".join(
        (
            PNG_SIGNATURE,
            _chunk(b"IHDR", header),
            _chunk(b"IDAT", zlib.compress(bytes(raw), level)),
            _chunk(b"IEND", b""),
        )
    )


def bgra_to_rgb_rows(bgra: bytes, width: int, height: int) -> list[bytes]:
    """Split a top-down BGRA buffer into RGB scanlines, dropping alpha."""

    stride = width * 4
    if len(bgra) < stride * height:
        raise PngError(
            f"buffer holds {len(bgra)} bytes, expected at least {stride * height}"
        )
    rows: list[bytes] = []
    for y in range(height):
        chunk = bgra[y * stride : (y + 1) * stride]
        rgb = bytearray(width * 3)
        rgb[0::3] = chunk[2::4]
        rgb[1::3] = chunk[1::4]
        rgb[2::3] = chunk[0::4]
        rows.append(bytes(rgb))
    return rows


def encode_bgra(bgra: bytes, width: int, height: int, level: int = 6, filter_type: str = "none") -> bytes:
    return encode_rgb(
        bgra_to_rgb_rows(bgra, width, height), width, height, level, filter_type
    )


# --------------------------------------------------------------------------- #
# Decoding
# --------------------------------------------------------------------------- #


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def decode(png: bytes) -> tuple[int, int, bytes]:
    """Decode an 8-bit RGB/RGBA PNG into ``(width, height, rgb_bytes)``.

    Only what this package produces or consumes: 8 bits per channel, colour
    types 2 (RGB) and 6 (RGBA), no interlacing.  Anything else raises
    :class:`PngError` rather than returning a plausible-looking wrong answer.
    """

    if not png.startswith(PNG_SIGNATURE):
        raise PngError("not a PNG (bad signature)")

    pos = len(PNG_SIGNATURE)
    width = height = depth = colour = interlace = 0
    idat = bytearray()
    while pos + 8 <= len(png):
        length = struct.unpack(">I", png[pos : pos + 4])[0]
        tag = png[pos + 4 : pos + 8]
        payload = png[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if tag == b"IHDR":
            width, height, depth, colour, _comp, _filt, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
        elif tag == b"IDAT":
            idat.extend(payload)
        elif tag == b"IEND":
            break

    if width <= 0 or height <= 0:
        raise PngError("missing or invalid IHDR")
    if depth != 8:
        raise PngError(f"unsupported bit depth {depth}")
    if colour not in (2, 6):
        raise PngError(f"unsupported colour type {colour}")
    if interlace:
        raise PngError("interlaced PNGs are not supported")

    channels = 3 if colour == 2 else 4
    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    if len(raw) < (stride + 1) * height:
        raise PngError(f"inflated data is short: {len(raw)} bytes")

    out = bytearray(height * stride)
    previous = bytes(stride)
    offset = 0
    for y in range(height):
        filter_type = raw[offset]
        line = bytearray(raw[offset + 1 : offset + 1 + stride])
        offset += stride + 1
        if filter_type == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif filter_type == 2:
            for i in range(stride):
                line[i] = (line[i] + previous[i]) & 0xFF
        elif filter_type == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif filter_type == 4:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                up_left = previous[i - channels] if i >= channels else 0
                line[i] = (line[i] + _paeth(left, previous[i], up_left)) & 0xFF
        elif filter_type != 0:
            raise PngError(f"unknown filter type {filter_type} on row {y}")
        out[y * stride : (y + 1) * stride] = line
        previous = bytes(line)

    if channels == 4:
        rgb = bytearray(width * height * 3)
        for y in range(height):
            row = out[y * stride : (y + 1) * stride]
            target = y * width * 3
            end = target + width * 3
            # The strided slice must stop at the row's end: ``target::3`` runs to the
            # end of the whole image and then a one-row assignment raises
            # "attempt to assign bytes of size <width> to extended slice of size
            # <width*height>" (observed on the engine's RGBA snapshots — 3-channel
            # PNGs never take this branch, which is why self-testing missed it).
            rgb[target + 0 : end : 3] = row[0::4]
            rgb[target + 1 : end : 3] = row[1::4]
            rgb[target + 2 : end : 3] = row[2::4]
        return width, height, bytes(rgb)
    return width, height, bytes(out)


def dimensions(png: bytes) -> tuple[int, int]:
    """Read width/height from IHDR without decoding pixels."""

    if not png.startswith(PNG_SIGNATURE) or len(png) < 24:
        raise PngError("not a PNG")
    if png[12:16] != b"IHDR":
        raise PngError("IHDR is not the first chunk")
    width, height = struct.unpack(">II", png[16:24])
    return width, height


# --------------------------------------------------------------------------- #
# Analysis and scaling
# --------------------------------------------------------------------------- #


def non_blank_ratio(png: bytes, *, threshold: int = 12, step: int = 8) -> float:
    """Fraction of sampled pixels brighter than ``threshold`` in any channel.

    A desktop that exists but was never switched to composites to solid black,
    so this is the measurement that proves a capture is real.
    """

    width, height, rgb = decode(png)
    lit = total = 0
    row_bytes = width * 3
    for y in range(0, height, step):
        base = y * row_bytes
        for x in range(0, row_bytes, 3 * step):
            total += 1
            pixel = rgb[base + x : base + x + 3]
            if len(pixel) == 3 and (
                pixel[0] > threshold or pixel[1] > threshold or pixel[2] > threshold
            ):
                lit += 1
    return (lit / total) if total else 0.0


def downscale(png: bytes, max_width: int) -> bytes:
    """Nearest-neighbour downscale, for the live-view page.

    Nearest neighbour is deliberate: it is a handful of slice operations instead
    of a resampling loop, and a live preview does not need antialiasing.
    """

    width, height, rgb = decode(png)
    if width <= max_width:
        return png
    factor = max(1, (width + max_width - 1) // max_width)
    new_width = max(1, width // factor)
    new_height = max(1, height // factor)
    rows: list[bytes] = []
    for y in range(new_height):
        source = (y * factor) * width * 3
        row = bytearray(new_width * 3)
        for x in range(new_width):
            offset = source + (x * factor) * 3
            row[x * 3 : x * 3 + 3] = rgb[offset : offset + 3]
        rows.append(bytes(row))
    return encode_rgb(rows, new_width, new_height, level=1)
