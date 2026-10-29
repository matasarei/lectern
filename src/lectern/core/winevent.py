"""A named Windows event: how Lectern's Windows tray app asks `lectern serve` to stop.

Windows has no Ctrl+C to send a process with no console. The tray app used to close a pipe on
`serve`'s standard input instead, but while a thread waits in a read on that pipe, Windows holds
the handle: a DLL that looks at the standard handles as it loads (numpy's OpenBLAS) then waits
for the read, and the first model load froze the server until the pipe closed
(docs/acceptance/m4.md, Windows results). An event touches no standard handle.

Windows only (`ctypes.WinDLL`); `serve` refuses `--stop-event` elsewhere.
"""

from __future__ import annotations

import ctypes
from typing import Any

INFINITE = 0xFFFFFFFF
WAIT_OBJECT_0 = 0


def _kernel32() -> Any:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.CreateEventW.argtypes = (
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    )
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.SetEvent.argtypes = (wintypes.HANDLE,)
    kernel32.SetEvent.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _open(kernel32: Any, name: str) -> int:
    # The tray app's own event when it made it first, else a new one by that name: manual
    # reset, so a set event stays set for whoever looks; not set to begin with.
    handle = kernel32.CreateEventW(None, True, False, name)
    if not handle:
        raise OSError(ctypes.get_last_error(), f"couldn't open the event {name!r}")
    return handle


def wait_for(name: str) -> None:
    """Return once the named event is set. Blocks the calling thread (the GIL is released)."""
    kernel32 = _kernel32()
    handle = _open(kernel32, name)
    try:
        if kernel32.WaitForSingleObject(handle, INFINITE) != WAIT_OBJECT_0:
            raise OSError(ctypes.get_last_error(), f"couldn't wait for the event {name!r}")
    finally:
        kernel32.CloseHandle(handle)


def set_event(name: str) -> None:
    """Set the named event: what the tray app does to stop Lectern."""
    kernel32 = _kernel32()
    handle = _open(kernel32, name)
    try:
        if not kernel32.SetEvent(handle):
            raise OSError(ctypes.get_last_error(), f"couldn't set the event {name!r}")
    finally:
        kernel32.CloseHandle(handle)
