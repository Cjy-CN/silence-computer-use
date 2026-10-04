"""Decide whether an empty local credential can authorize this session.

One local interactive LogonUser check, no impersonation and no persisted token.
Domain/online identities or policy failures are inconclusive, not permission to
bypass authentication. This check never tries any non-empty password.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import time


@dataclass(frozen=True)
class EmptyCredential:
    accepted: bool = False
    status: str = "unknown"
    winerror: int = 0

    @property
    def try_without_card(self) -> bool:
        # 1327 is an account/policy restriction, NOT proof of a wrong password.
        # Try the SDK's normal empty authorization path without a useless card;
        # it must still authenticate and render, or startup stops.
        return self.accepted or self.winerror == 1327


class WindowsCredentialApi:
    def __init__(self):
        self.security = ctypes.WinDLL("secur32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        self.security.GetUserNameExW.argtypes = [
            ctypes.c_int, wintypes.LPWSTR, ctypes.POINTER(wintypes.ULONG)]
        self.security.GetUserNameExW.restype = wintypes.BOOL
        self.kernel.GetComputerNameW.argtypes = [
            wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.GetComputerNameW.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.advapi.LogonUserW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
            wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        self.advapi.LogonUserW.restype = wintypes.BOOL

    def identity(self) -> tuple[str, str, str]:
        user = ctypes.create_unicode_buffer(1024)
        size = wintypes.ULONG(len(user))
        if not self.security.GetUserNameExW(2, user, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        computer = ctypes.create_unicode_buffer(256)
        size = wintypes.DWORD(len(computer))
        if not self.kernel.GetComputerNameW(computer, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        domain, separator, account = user.value.partition("\\")
        if not separator:
            raise ValueError("current identity is not a SAM-compatible account")
        return domain, account, computer.value

    def try_empty(self, domain: str, account: str) -> tuple[bool, int]:
        token = wintypes.HANDLE()
        try:
            ok = bool(self.advapi.LogonUserW(
                account, domain, "", 2, 0, ctypes.byref(token)))
            error = 0 if ok else ctypes.get_last_error()
            return ok, error
        finally:
            if token.value:
                self.kernel.CloseHandle(token)


def probe_empty_credential(api=None) -> EmptyCredential:
    """True only when Windows accepts the current local account's empty secret."""
    try:
        api = api or WindowsCredentialApi()
        domain, account, computer = api.identity()
        if not account or domain.casefold() != computer.casefold():
            return EmptyCredential(status="nonlocal_identity")
        accepted, error = api.try_empty(domain, account)
        if accepted:
            return EmptyCredential(True, "empty_accepted")
        status = {1326: "password_required", 1327: "account_restriction"}.get(error, "unknown")
        return EmptyCredential(False, status, error)
    except (OSError, ValueError, AttributeError) as error:
        return EmptyCredential(winerror=getattr(error, "winerror", 0) or 0)


def authorize_session(host, ui, *, auto_empty: bool, timeout: float,
                      now=time.monotonic, sleep=time.sleep) -> dict:
    """Authorize once; a GUID is insufficient until a real frame is rendered.

Empty accounts never create/read a password card. Manual empty submissions are
allowed for inconclusive detection. Failed creation/rendering stops rather than
repeatedly prompting the user or guessing that a password was wrong.
"""
    if auto_empty:
        password = ""
    else:
        deadline = now() + timeout
        while now() < deadline:
            if ui.dialog.cancelled:
                return {"ok": False, "reason": "cancelled", "exit_code": 4}
            if ui.dialog.submitted:
                password = ui.dialog.read_password()
                ui.close_dialog()
                break
            sleep(0.1)
        else:
            return {"ok": False, "reason": "authorization_timeout", "exit_code": 3}
    try:
        session = host.create_session(password)
    finally:
        password = None  # Do not retain the credential in this frame.
    if not session.get("session_guid"):
        return {"ok": False, "reason": "session_creation_failed", "exit_code": 3}
    render = host.wait_for_render(timeout=35)
    if not render.get("rendered"):
        return {"ok": False, "reason": "session_render_failed", "render": render, "exit_code": 3}
    return {"ok": True, "session": session, "render": render, "automatic": auto_empty}
