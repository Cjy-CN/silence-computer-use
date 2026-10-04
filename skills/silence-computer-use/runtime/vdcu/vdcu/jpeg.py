"""JPEG encoding through the GDI+ that ships with Windows.

Why this exists: a full-resolution desktop frame of a photographic wallpaper is
~3 MB as PNG (PNG is lossless, so a photo costs 2–4 bytes per pixel), which is too
heavy to stream into a live preview or into a conversation.  JPEG at quality 85 is
roughly a tenth of that, and GDI+ does the encoding in C — no Pillow, no external
dependency, exactly like the rest of this package.

The flat GDI+ API is used through :mod:`ctypes`: create a 32-bit bitmap over the
capture buffer, ask GDI+ for its JPEG encoder CLSID, set the quality parameter, and
save to a temporary file (GDI+'s file-based entry point is far simpler than building
an ``IStream`` over an ``HGLOBAL``, and the file never leaves ``%TEMP%``).
"""

from __future__ import annotations

import ctypes
import os
import struct
import tempfile
import threading

from ctypes import wintypes

_JPEG_MIME = "image/jpeg"
# Measured on this machine: GdipCreateBitmapFromScan0 rejects PixelFormat32bppRGB
# (0x22009) with status 2 (InvalidParameter) when the caller supplies scan0 — for both
# a 2×2 and a 1536×864 buffer, with stride 0 or the true stride.  32bppARGB and
# 32bppPARGB are accepted, so ARGB it is, and the alpha byte is forced opaque because
# GDI screen captures leave it at zero.
_PIXEL_FORMAT_32BPP_ARGB = 0x0026200A
_ENCODER_QUALITY = "{1D5BE4B5-FA4A-452D-9CDD-5DB35105E7EB}"
_PARAM_TYPE_LONG = 4

_lock = threading.Lock()
_token = ctypes.c_void_p()
_ready = False


class JpegError(RuntimeError):
    pass


class GUID(ctypes.Structure):
    _fields_ = (
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    )

    @classmethod
    def parse(cls, text: str) -> "GUID":
        cleaned = text.strip().strip("{}")
        parts = cleaned.split("-")
        guid = cls()
        guid.Data1 = int(parts[0], 16)
        guid.Data2 = int(parts[1], 16)
        guid.Data3 = int(parts[2], 16)
        tail = bytes.fromhex(parts[3] + parts[4])
        for index, value in enumerate(tail):
            guid.Data4[index] = value
        return guid


class GdiplusStartupInput(ctypes.Structure):
    _fields_ = (
        ("GdiplusVersion", ctypes.c_uint),
        ("DebugEventCallback", ctypes.c_void_p),
        ("SuppressBackgroundThread", ctypes.c_int),
        ("SuppressExternalCodecs", ctypes.c_int),
    )


class ImageCodecInfo(ctypes.Structure):
    _fields_ = (
        ("Clsid", GUID),
        ("FormatID", GUID),
        ("CodecName", wintypes.LPWSTR),
        ("DllName", wintypes.LPWSTR),
        ("FormatDescription", wintypes.LPWSTR),
        ("FilenameExtension", wintypes.LPWSTR),
        ("MimeType", wintypes.LPWSTR),
        ("Flags", wintypes.DWORD),
        ("Version", wintypes.DWORD),
        ("SigCount", wintypes.DWORD),
        ("SigSize", wintypes.DWORD),
        ("SigPattern", ctypes.c_void_p),
        ("SigMask", ctypes.c_void_p),
    )


class EncoderParameter(ctypes.Structure):
    _fields_ = (
        ("Guid", GUID),
        ("NumberOfValues", ctypes.c_ulong),
        ("Type", ctypes.c_ulong),
        ("Value", ctypes.c_void_p),
    )


class EncoderParameters(ctypes.Structure):
    _fields_ = (("Count", ctypes.c_uint), ("Parameter", EncoderParameter * 1))


def _load():
    global _ready
    with _lock:
        if _ready:
            return
        gdiplus = ctypes.WinDLL("gdiplus")
        gdiplus.GdiplusStartup.restype = ctypes.c_int
        gdiplus.GdiplusStartup.argtypes = [
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(GdiplusStartupInput), ctypes.c_void_p
        ]
        gdiplus.GdiplusShutdown.argtypes = [ctypes.c_void_p]
        gdiplus.GdipCreateBitmapFromScan0.restype = ctypes.c_int
        gdiplus.GdipCreateBitmapFromScan0.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
        ]
        gdiplus.GdipSaveImageToFile.restype = ctypes.c_int
        gdiplus.GdipSaveImageToFile.argtypes = [
            ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(GUID), ctypes.c_void_p
        ]
        gdiplus.GdipDisposeImage.argtypes = [ctypes.c_void_p]
        gdiplus.GdipGetImageEncodersSize.restype = ctypes.c_int
        gdiplus.GdipGetImageEncodersSize.argtypes = [
            ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)
        ]
        gdiplus.GdipGetImageEncoders.restype = ctypes.c_int
        gdiplus.GdipGetImageEncoders.argtypes = [
            ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p
        ]

        startup = GdiplusStartupInput()
        startup.GdiplusVersion = 1
        status = gdiplus.GdiplusStartup(
            ctypes.byref(_token), ctypes.byref(startup), None
        )
        if status != 0:
            raise JpegError(f"GdiplusStartup failed with status {status}")
        gdiplus_ref = gdiplus
        _load.gdiplus = gdiplus_ref  # type: ignore[attr-defined]
        _ready = True


def jpeg_encoder_clsid() -> GUID:
    _load()
    gdiplus = _load.gdiplus  # type: ignore[attr-defined]
    count = ctypes.c_uint()
    size = ctypes.c_uint()
    if gdiplus.GdipGetImageEncodersSize(ctypes.byref(count), ctypes.byref(size)) != 0 or not count.value:
        raise JpegError("GDI+ reported no image encoders")
    buffer = ctypes.create_string_buffer(size.value)
    if gdiplus.GdipGetImageEncoders(count.value, size.value, buffer) != 0:
        raise JpegError("GdipGetImageEncoders failed")
    array = ctypes.cast(buffer, ctypes.POINTER(ImageCodecInfo))
    for index in range(count.value):
        mime = array[index].MimeType
        if mime and mime.lower() == _JPEG_MIME:
            return array[index].Clsid
    raise JpegError("no JPEG encoder is registered with GDI+")


def encode_bgra(bgra: bytes, width: int, height: int, *, quality: int = 85) -> bytes:
    """Encode a top-down BGRA buffer as a JPEG image."""

    if len(bgra) < width * height * 4:
        raise JpegError(f"buffer holds {len(bgra)} bytes, expected {width * height * 4}")
    _load()
    gdiplus = _load.gdiplus  # type: ignore[attr-defined]

    pixels = bytearray(bgra[: width * height * 4])
    pixels[3::4] = b"\xff" * (width * height)  # GDI capture leaves alpha at zero

    bitmap = ctypes.c_void_p()
    source = ctypes.create_string_buffer(bytes(pixels), len(pixels))
    status = gdiplus.GdipCreateBitmapFromScan0(
        width, height, width * 4, _PIXEL_FORMAT_32BPP_ARGB, source, ctypes.byref(bitmap)
    )
    if status != 0 or not bitmap:
        raise JpegError(f"GdipCreateBitmapFromScan0 failed with status {status}")

    parameters = EncoderParameters()
    parameters.Count = 1
    parameters.Parameter[0].Guid = GUID.parse(_ENCODER_QUALITY)
    parameters.Parameter[0].NumberOfValues = 1
    parameters.Parameter[0].Type = _PARAM_TYPE_LONG
    quality_value = ctypes.c_ulong(max(1, min(100, int(quality))))
    parameters.Parameter[0].Value = ctypes.cast(
        ctypes.byref(quality_value), ctypes.c_void_p
    )

    handle, path = tempfile.mkstemp(suffix=".jpg", prefix="vdcu-")
    os.close(handle)
    try:
        clsid = jpeg_encoder_clsid()
        status = gdiplus.GdipSaveImageToFile(
            bitmap, path, ctypes.byref(clsid), ctypes.byref(parameters)
        )
        if status != 0:
            raise JpegError(f"GdipSaveImageToFile failed with status {status}")
        with open(path, "rb") as result:
            return result.read()
    finally:
        gdiplus.GdipDisposeImage(bitmap)
        try:
            os.remove(path)
        except OSError:
            pass


def is_available() -> bool:
    try:
        _load()
        jpeg_encoder_clsid()
        return True
    except (JpegError, OSError):
        return False


def dimensions(jpeg: bytes) -> tuple[int, int]:
    """Read width/height from a JPEG's SOF marker."""

    if not jpeg.startswith(b"\xff\xd8"):
        raise JpegError("not a JPEG")
    position = 2
    while position + 9 < len(jpeg):
        if jpeg[position] != 0xFF:
            position += 1
            continue
        marker = jpeg[position + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            position += 2
            continue
        length = struct.unpack(">H", jpeg[position + 2 : position + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB):
            height, width = struct.unpack(">HH", jpeg[position + 5 : position + 9])
            return width, height
        position += 2 + length
    raise JpegError("no SOF marker found")
