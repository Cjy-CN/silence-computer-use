"""ctypes bindings for the bundled shadow-session SDK.

Keep signed native components and the interpreter unchanged. Use the bundled
engine pack or explicitly configured runtime paths; never bypass authentication.
"""

from __future__ import annotations

import ctypes
import glob
import os
import re
import time
from ctypes import wintypes

# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #

_ENV_APP_DIR = "VDCU_SHADOW_APP_DIR"
_ENV_SANDBOX_PY = "VDCU_SHADOW_SANDBOX_PYTHON"

# The self-contained engine pack (``vdcu engine-pack``): vendor binaries copied
# unchanged from the user's own installation.  Checked first so ``--engine shadow``
# works on a machine without the bundled runtime installed at all.
_PACK_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "engine"
)

def find_app_dir() -> str:
    """Directory holding the native SDK and session backend."""
    override = os.environ.get(_ENV_APP_DIR)
    if override:
        if os.path.isfile(os.path.join(override, "pip_sdk.dll")):
            return override
        raise FileNotFoundError(f"{_ENV_APP_DIR} has no pip_sdk.dll")
    bundled = os.path.join(_PACK_ROOT, "app")
    if os.path.isfile(os.path.join(bundled, "pip_sdk.dll")):
        return bundled
    raise FileNotFoundError("Restore engine/app or set VDCU_SHADOW_APP_DIR")


def find_sandbox_python() -> str:
    """Locate the signed interpreter accepted by the session SDK."""
    override = os.environ.get(_ENV_SANDBOX_PY)
    if override:
        if os.path.isfile(override):
            return override
        raise FileNotFoundError(f"{_ENV_SANDBOX_PY} is not a file")
    bundled = os.path.join(_PACK_ROOT, "python", "python.exe")
    if os.path.isfile(bundled):
        return bundled
    raise FileNotFoundError("Restore engine/python or set VDCU_SHADOW_SANDBOX_PYTHON")


def find_pip_dll() -> str:
    return os.path.join(find_app_dir(), "pip_sdk.dll")


def find_cursor_path() -> str:
    path = os.path.join(find_app_dir(), "pip", "cursor-blue-white.png")
    return path if os.path.isfile(path) else ""


# --------------------------------------------------------------------------- #
# Raw bindings
# --------------------------------------------------------------------------- #

# Result status as printed by the SDK log: {category=N, code=N, success=bool}.
# The C return value is treated as an opaque int; the SDK's own log is the oracle
# for semantics, and :class:`PipSdk` exposes ``read_log_tail`` for exactly that.
STATUS_OK = 0


class PipSdk:
    """Thin ctypes wrapper over the 31 exports we actually need.

    Every call is padded with writable buffers beyond its evidenced parameters.
    The SDK has out-parameters the log does not print (calling one of them with the
    registers unset produced "access violation writing 0x4E" — a garbage pointer),
    and extra arguments are harmless on x64 while missing ones crash.  Padding with
    valid memory is the difference between a probe that survives and one that dies
    at the first unknown signature.
    """

    #: Writable slots handed to every call's tail registers/stack.
    PAD_SLOTS = 4

    def __init__(self, dll_path: str | None = None):
        self.dll_path = dll_path or find_pip_dll()
        self.dll = ctypes.WinDLL(self.dll_path)
        self._pad = [(ctypes.c_int * 16)() for _ in range(self.PAD_SLOTS)]
        self._pad_ptrs = [ctypes.byref(block) for block in self._pad]
        self._declare()

    def _tail(self) -> list:
        return list(self._pad_ptrs)

    # -- prototypes ------------------------------------------------------ #

    def _declare(self) -> None:
        dll = self.dll
        tail = [ctypes.c_void_p] * self.PAD_SLOTS

        dll.PipSdk_GetVersion.restype = ctypes.c_char_p
        dll.PipSdk_GetVersion.argtypes = []

        # Initialize(pip_module_dir, log_path_base, dump_path_base, vm_cache_dir,
        #            sdk_version, auto_destroy_shadow_session_when_exit)
        dll.PipSdk_Initialize.restype = ctypes.c_int
        dll.PipSdk_Initialize.argtypes = [
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int,
        ] + tail
        dll.PipSdk_Uninitialize.restype = ctypes.c_int
        dll.PipSdk_Uninitialize.argtypes = [] + tail

        dll.PipSdk_SetVirtualCursorPath.restype = ctypes.c_int
        dll.PipSdk_SetVirtualCursorPath.argtypes = [ctypes.c_char_p] + tail

        dll.PipSdk_IsSystemEnvironmentInitialized.restype = ctypes.c_int
        dll.PipSdk_IsSystemEnvironmentInitialized.argtypes = [] + tail
        dll.PipSdk_InitializeSystemEnvironment.restype = ctypes.c_int
        dll.PipSdk_InitializeSystemEnvironment.argtypes = [] + tail

        # VerifyCurrentUserPassword(password, ...out): the engine's own credential
        # check ("Shadow session credential precheck ... has_password=no" is what it
        # reports without this).  Out-slots beyond the evidenced parameter are padded.
        dll.PipSdk_VerifyCurrentUserPassword.restype = ctypes.c_int
        dll.PipSdk_VerifyCurrentUserPassword.argtypes = [ctypes.c_char_p] + tail

        # CreateShadowSession(parent_window_handle, x, y, width, height,
        #                     display_width, display_height, visible,
        #                     password, prompt)
        dll.PipSdk_CreateShadowSession.restype = ctypes.c_int
        dll.PipSdk_CreateShadowSession.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_char_p, ctypes.c_int,
        ] + tail
        dll.PipSdk_DestroyShadowSession.restype = ctypes.c_int
        dll.PipSdk_DestroyShadowSession.argtypes = [] + tail
        dll.PipSdk_SetShadowSessionRect.restype = ctypes.c_int
        dll.PipSdk_SetShadowSessionRect.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int
        ] + tail
        dll.PipSdk_SetShadowSessionVisible.restype = ctypes.c_int
        dll.PipSdk_SetShadowSessionVisible.argtypes = [ctypes.c_int] + tail
        # Six parameters, not two: the log oracle printed
        # {x, y, width, height, display_width, display_height} for this call — the
        # view rect plus the session display, the same shape as CreateShadowSession.
        dll.PipSdk_SetShadowSessionDisplaySettings.restype = ctypes.c_int
        dll.PipSdk_SetShadowSessionDisplaySettings.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int
        ] + tail

        # DumpHybridPrograms(out_json): one output pointer — the session's running
        # programs as JSON.  Fails without a session ("main not connected").
        dll.PipSdk_DumpHybridPrograms.restype = ctypes.c_int
        dll.PipSdk_DumpHybridPrograms.argtypes = [ctypes.POINTER(ctypes.c_void_p)] + tail

        # TakeSnapshot(out_buffer, out_size, max_pixels_count)
        dll.PipSdk_TakeSnapshot.restype = ctypes.c_int
        dll.PipSdk_TakeSnapshot.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_int
        ] + tail
        dll.PipSdk_FreeMemory.restype = None
        dll.PipSdk_FreeMemory.argtypes = [ctypes.c_void_p]

        # ---- input, clipboard, process -------------------------------- #
        # These signatures were read out of the SDK's own log, which prints what
        # it received for every call ("enter with args {name=value}").  Calling
        # each one with distinctive buffers in every slot made the log show the
        # field names and which value landed where — see
        # the native SDK. Coordinates are doubles.
        dll.PipSdk_SendMouseMoveEvent.restype = ctypes.c_int
        dll.PipSdk_SendMouseMoveEvent.argtypes = [ctypes.c_double, ctypes.c_double] + tail

        dll.PipSdk_SendMouseClickEvent.restype = ctypes.c_int
        dll.PipSdk_SendMouseClickEvent.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_double, ctypes.c_double
        ] + tail

        dll.PipSdk_SendMouseWheelEvent.restype = ctypes.c_int
        dll.PipSdk_SendMouseWheelEvent.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_double, ctypes.c_double
        ] + tail

        dll.PipSdk_SendMouseSingleEvent.restype = ctypes.c_int
        dll.PipSdk_SendMouseSingleEvent.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_double, ctypes.c_double
        ] + tail

        dll.PipSdk_SendKeyboardEvent.restype = ctypes.c_int
        dll.PipSdk_SendKeyboardEvent.argtypes = [ctypes.c_int, ctypes.c_int] + tail

        # SendKeyStroke(keys, size): a chord as an array pointer plus its length.
        # Argument ORDER is settled by the wire, not by the earlier probe's guess:
        # passing (size, ptr) made the SDK log "size=<our pointer value>"
        # (pip_sdk_20261003.log, 13:32:00 — size=1948154525112 is a heap address),
        # while the vendor's own call logs {size=2}.  The SDK reads the pointer
        # first and the length second.
        dll.PipSdk_SendKeyStroke.restype = ctypes.c_int
        dll.PipSdk_SendKeyStroke.argtypes = [ctypes.c_void_p, ctypes.c_size_t] + tail

        dll.PipSdk_SendText.restype = ctypes.c_int
        dll.PipSdk_SendText.argtypes = [ctypes.c_char_p] + tail

        dll.PipSdk_SetClipboardText.restype = ctypes.c_int
        dll.PipSdk_SetClipboardText.argtypes = [ctypes.c_char_p] + tail

        dll.PipSdk_GetClipboardText.restype = ctypes.c_int
        dll.PipSdk_GetClipboardText.argtypes = [ctypes.POINTER(ctypes.c_void_p)] + tail

        # CreateProcess(application_name, command_line, current_directory,
        #               run_as_admin, create_in_vm) — the log's "has_command_line"
        # wording is just its null-check on the optional strings.
        dll.PipSdk_CreateProcess.restype = ctypes.c_int
        dll.PipSdk_CreateProcess.argtypes = [
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_int
        ] + tail

        dll.PipSdk_QueryHybridPrograms.restype = ctypes.c_int
        dll.PipSdk_QueryHybridPrograms.argtypes = [
            ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_void_p),
        ] + tail

    # -- small helpers --------------------------------------------------- #

    def version(self) -> str:
        raw = self.dll.PipSdk_GetVersion()
        return raw.decode("ascii", "replace") if raw else ""

    def read_log_tail(self, log_path_base: str, lines: int = 12) -> list[str]:
        """The SDK's own log — the ground truth for what each call did."""

        today = time.strftime("%Y%m%d")
        pattern = os.path.join(log_path_base, "pip_sdk", f"pip_sdk_{today}.log")
        files = sorted(glob.glob(pattern)) or sorted(
            glob.glob(os.path.join(log_path_base, "pip_sdk", "pip_sdk_*.log"))
        )
        if not files:
            return []
        with open(files[-1], "r", encoding="utf-8", errors="replace") as handle:
            return [line.rstrip() for line in handle.readlines()[-lines:]]

    # -- calls ----------------------------------------------------------- #

    def initialize(
        self,
        *,
        pip_module_dir: str,
        log_path_base: str,
        dump_path_base: str,
        vm_cache_dir: str,
        auto_destroy_on_exit: int = 1,
    ) -> int:
        version = self.version().encode("ascii") or b"0.0.0.78"
        return self.dll.PipSdk_Initialize(
            pip_module_dir.encode("utf-8"),
            log_path_base.encode("utf-8"),
            dump_path_base.encode("utf-8"),
            vm_cache_dir.encode("utf-8"),
            version,
            int(auto_destroy_on_exit),
            *self._tail(),
        )

    def uninitialize(self) -> int:
        return self.dll.PipSdk_Uninitialize(*self._tail())

    def set_virtual_cursor_path(self, path: str) -> int:
        return self.dll.PipSdk_SetVirtualCursorPath(path.encode("utf-8"), *self._tail())

    def is_system_environment_initialized(self) -> int:
        return self.dll.PipSdk_IsSystemEnvironmentInitialized(*self._tail())

    def initialize_system_environment(self) -> int:
        return self.dll.PipSdk_InitializeSystemEnvironment(*self._tail())

    def verify_current_user_password(self, password: str) -> int:
        return self.dll.PipSdk_VerifyCurrentUserPassword(password.encode("utf-8"), *self._tail())

    # -- input into the shadow session --------------------------------- #

    def send_mouse_move(self, x: float, y: float) -> int:
        return self.dll.PipSdk_SendMouseMoveEvent(float(x), float(y), *self._tail())

    def send_mouse_click(self, button: int, modifier: int, action: int, x: float, y: float) -> int:
        return self.dll.PipSdk_SendMouseClickEvent(
            int(button), int(modifier), int(action), float(x), float(y), *self._tail()
        )

    def send_mouse_wheel(self, wheel_type: int, modifier: int, wheel: int, x: float, y: float) -> int:
        return self.dll.PipSdk_SendMouseWheelEvent(
            int(wheel_type), int(modifier), int(wheel), float(x), float(y), *self._tail()
        )

    def send_mouse_single(self, button: int, modifier: int, action: int, x: float, y: float) -> int:
        return self.dll.PipSdk_SendMouseSingleEvent(
            int(button), int(modifier), int(action), float(x), float(y), *self._tail()
        )

    def send_keyboard_event(self, virtual_key: int, is_down: int) -> int:
        return self.dll.PipSdk_SendKeyboardEvent(int(virtual_key), int(is_down), *self._tail())

    def send_key_stroke(self, keys: list[int]) -> int:
        """A chord: array pointer first, then the length — the wire order the
        vendor's own ``SendKeyStroke(size=2)`` calls use (see the class note)."""

        if not keys:
            return self.dll.PipSdk_SendKeyStroke(None, 0, *self._tail())
        array = (ctypes.c_int * len(keys))(*[int(key) for key in keys])
        return self.dll.PipSdk_SendKeyStroke(ctypes.byref(array), len(keys), *self._tail())

    def send_text(self, text: str) -> int:
        return self.dll.PipSdk_SendText(text.encode("utf-8"), *self._tail())

    def set_clipboard(self, text: str) -> int:
        return self.dll.PipSdk_SetClipboardText(text.encode("utf-8"), *self._tail())

    def get_clipboard(self) -> tuple[int, str]:
        holder = ctypes.c_void_p()
        status = self.dll.PipSdk_GetClipboardText(ctypes.byref(holder), *self._tail())
        if not holder.value:
            return status, ""
        try:
            text = ctypes.cast(holder, ctypes.c_char_p).value or b""
            return status, text.decode("utf-8", errors="replace")
        finally:
            self.dll.PipSdk_FreeMemory(holder)

    def create_process(
        self, application: str, command_line: str | None = None,
        current_directory: str | None = None, *, run_as_admin: int = 0,
        create_in_vm: int = 0,
    ) -> int:
        return self.dll.PipSdk_CreateProcess(
            application.encode("utf-8"),
            command_line.encode("utf-8") if command_line else None,
            current_directory.encode("utf-8") if current_directory else None,
            int(run_as_admin), int(create_in_vm), *self._tail(),
        )

    def query_hybrid_programs(
        self, query_text: str | None = None, max_count: int = 20,
        query_high_score: int = 0, config_json: str | None = "{}",
    ) -> tuple[int, str]:
        """Search the engine's program catalogue.
        SDK requirements:
        ``query_text`` must be non-empty and ``config_json`` must be non-null — an
        empty query or a null config returns ``{category=4, code=1}`` with no
        payload.  The result is a JSON array of
        ``{file_path, name, score, unique_info}`` where ``unique_info`` is the AUMID
        for packaged apps.  It works without a shadow session.
        """

        holder = ctypes.c_void_p()
        status = self.dll.PipSdk_QueryHybridPrograms(
            (query_text or "").encode("utf-8") or None,
            int(max_count), int(query_high_score),
            config_json.encode("utf-8") if config_json is not None else None,
            ctypes.byref(holder), *self._tail(),
        )
        if not holder.value:
            return status, ""
        try:
            text = ctypes.cast(holder, ctypes.c_char_p).value or b""
            return status, text.decode("utf-8", errors="replace")
        finally:
            self.dll.PipSdk_FreeMemory(holder)

    def create_shadow_session(
        self,
        parent_hwnd: int,
        rect: tuple[int, int, int, int] = (0, 0, 1024, 640),
        display: tuple[int, int] = (1920, 1080),
        *,
        visible: int = 1,
        password: str | None = None,
        prompt: int = 1,
    ) -> int:
        x, y, width, height = rect
        return self.dll.PipSdk_CreateShadowSession(
            ctypes.c_void_p(int(parent_hwnd)),
            int(x), int(y), int(width), int(height),
            int(display[0]), int(display[1]), int(visible),
            password.encode("utf-8") if password is not None else None,
            int(prompt),
            *self._tail(),
        )

    def destroy_shadow_session(self) -> int:
        return self.dll.PipSdk_DestroyShadowSession(*self._tail())

    def set_shadow_session_rect(self, x: int, y: int, width: int, height: int) -> int:
        return self.dll.PipSdk_SetShadowSessionRect(
            int(x), int(y), int(width), int(height), *self._tail()
        )

    def set_shadow_session_visible(self, visible: int) -> int:
        return self.dll.PipSdk_SetShadowSessionVisible(int(visible), *self._tail())

    def set_shadow_session_display_settings(
        self, x: int, y: int, width: int, height: int,
        display_width: int, display_height: int,
    ) -> int:
        return self.dll.PipSdk_SetShadowSessionDisplaySettings(
            int(x), int(y), int(width), int(height),
            int(display_width), int(display_height), *self._tail(),
        )

    def dump_hybrid_programs(self) -> tuple[int, str]:
        """The session's running programs as JSON (needs a session)."""

        holder = ctypes.c_void_p()
        status = self.dll.PipSdk_DumpHybridPrograms(ctypes.byref(holder), *self._tail())
        if not holder.value:
            return status, ""
        try:
            text = ctypes.cast(holder, ctypes.c_char_p).value or b""
            return status, text.decode("utf-8", errors="replace")
        finally:
            self.dll.PipSdk_FreeMemory(holder)

    def take_snapshot(self, max_pixels: int = 1920 * 1080) -> tuple[int, bytes, str]:
        """Return ``(status, data, layout)``.

        The ABI is measured, not guessed: ``TakeSnapshot(uint8_t** out_buffer,
        size_t* out_size, int max_pixels_count)``.  The SDK allocates the image and
        the caller frees it with ``PipSdk_FreeMemory``; the payload is a PNG with the
        virtual cursor drawn on it ("Failed to overlay virtual cursor onto snapshot
        png").

        Where exactly the PNG starts inside the allocation is *not* assumed: the
        first caller-allocated probe showed a heap pointer in the first 8 bytes with
        the rest untouched, so the copy scans for the image magic (``\\x89PNG`` or a
        JPEG SOI) and slices from there.  If no magic is found the raw head comes
        back in ``layout`` as hex — a failure then says what it actually got instead
        of "0 bytes".
        """

        holder = ctypes.c_void_p()
        size = ctypes.c_size_t(0)
        status = self.dll.PipSdk_TakeSnapshot(
            ctypes.byref(holder), ctypes.byref(size), int(max_pixels), *self._tail()
        )
        if holder.value and size.value:
            block = (ctypes.c_ubyte * size.value).from_address(holder.value)
            raw = bytes(block)
            self.dll.PipSdk_FreeMemory(holder)
            return status, _slice_image(raw), _describe_head(raw)

        # Defensive fallback: a caller-allocated reading of the same call, in case a
        # future build changes the convention.
        capacity = max_pixels * 4
        buffer = (ctypes.c_ubyte * capacity)()
        size2 = ctypes.c_size_t(0)
        status = self.dll.PipSdk_TakeSnapshot(
            ctypes.cast(buffer, ctypes.c_void_p), ctypes.byref(size2), int(max_pixels),
            *self._tail(),
        )
        if size2.value and size2.value <= capacity:
            raw = bytes(buffer[: size2.value])
            return status, _slice_image(raw), "caller_buffer " + _describe_head(raw)
        return status, b"", "none"


def _slice_image(raw: bytes) -> bytes:
    """The image bytes inside a snapshot allocation: find the magic, slice from it.

    Handles an allocation with a header in front of the PNG (observed: the first 8
    bytes were a heap pointer) and a JPEG payload just as well.  Unrecognised
    payloads come back unchanged so the caller can report what they are.
    """

    for magic in (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"):
        start = raw.find(magic)
        if start >= 0:
            return raw[start:]
    return raw


def _describe_head(raw: bytes) -> str:
    """A one-line description of a snapshot payload's first bytes, for diagnostics."""

    head = raw[:16]
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    return f"unknown(head={head.hex()}, len={len(raw)})"


# --------------------------------------------------------------------------- #
# Runtime directories (the SDK writes its log/dump/cache here)
# --------------------------------------------------------------------------- #


def runtime_dirs(root: str) -> dict[str, str]:
    dirs = {
        "log": os.path.join(root, "log"),
        "dump": os.path.join(root, "dump"),
        "cache": os.path.join(root, "cache"),
    }
    for path in dirs.values():
        os.makedirs(path, exist_ok=True)
    return dirs
