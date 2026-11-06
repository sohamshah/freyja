"""Whether this Mac can start a scheduled run right now.

Every run is a model call, so it needs the network. While a Mac sleeps it
wakes for a minute at a time (DarkWake: maintenance, Power Nap): the
scheduler's process runs and finds jobs due, but the network is down and
the Mac sleeps again within the minute. Each run started then failed
with "Connection error" (the Morning briefing and the Routing radar,
Oct 30 to Nov 2).

Both checks are local and take microseconds:

  · DarkWake: IOKit's system capabilities, the letters pmset prints for
    each wake ("CDNVA" for a full wake, "CDNP" for a DarkWake). A
    DarkWake has no graphics.
  · Offline: SystemConfiguration's reachability of the internet. It reads
    the routing state and sends no packet.

When a check cannot run (another OS, a missing symbol), it does not hold
runs back. A reading that is wrong holds a run back at most 30 minutes of
the Mac's awake time (service._RUNS_WAIT_AT_MOST_SECONDS).
"""

from __future__ import annotations

import ctypes
import logging
import platform
import socket
from functools import lru_cache
from typing import Any

logger = logging.getLogger("freyja.scheduler.machine")

# IOPMCapabilityBits (IOKit's IOPMLibPrivate.h).
_CAPABILITY_CPU = 0x1
_CAPABILITY_VIDEO = 0x2

# SCNetworkReachabilityFlags (SystemConfiguration's SCNetworkReachability.h).
_REACHABLE = 1 << 1
_CONNECTION_REQUIRED = 1 << 2
_CONNECTION_ON_TRAFFIC = 1 << 3
_INTERVENTION_REQUIRED = 1 << 4
_CONNECTION_ON_DEMAND = 1 << 5


def why_runs_wait() -> str | None:
    """Why a due run should not start yet, or None when it can."""
    if dark_wake_from(_system_capabilities()):
        return "the Mac is in DarkWake"
    flags = _internet_reachability()
    if flags is not None and not online_from(flags):
        return "the network is unreachable"
    return None


def dark_wake_from(capabilities: int) -> bool:
    """True when ``capabilities`` (0 if unknown) are those of a DarkWake:
    the CPU runs, graphics do not."""
    return bool(capabilities & _CAPABILITY_CPU) and not capabilities & _CAPABILITY_VIDEO


def online_from(flags: int) -> bool:
    """True when reachability ``flags`` say a connection to the internet
    can be made now, or will be made by the first packet sent."""
    if not flags & _REACHABLE:
        return False
    if not flags & _CONNECTION_REQUIRED:
        return True
    return bool(flags & (_CONNECTION_ON_TRAFFIC | _CONNECTION_ON_DEMAND)) and not (
        flags & _INTERVENTION_REQUIRED
    )


def _system_capabilities() -> int:
    libs = _libraries()
    if libs is None:
        return 0
    try:
        return int(libs["iokit"].IOPMConnectionGetSystemCapabilities())
    except Exception:  # noqa: BLE001
        return 0


class _SockaddrIn(ctypes.Structure):
    # macOS's struct sockaddr_in, which starts with its own length.
    _fields_ = [
        ("sin_len", ctypes.c_uint8),
        ("sin_family", ctypes.c_uint8),
        ("sin_port", ctypes.c_uint16),
        ("sin_addr", ctypes.c_uint32),
        ("sin_zero", ctypes.c_char * 8),
    ]


def _internet_reachability() -> int | None:
    """Reachability flags for 0.0.0.0, "the internet", or None if unknown."""
    libs = _libraries()
    if libs is None:
        return None
    sc, cf = libs["sc"], libs["cf"]
    try:
        address = _SockaddrIn(ctypes.sizeof(_SockaddrIn), socket.AF_INET, 0, 0, b"\0" * 8)
        target = sc.SCNetworkReachabilityCreateWithAddress(None, ctypes.byref(address))
        if not target:
            return None
        try:
            flags = ctypes.c_uint32(0)
            if not sc.SCNetworkReachabilityGetFlags(target, ctypes.byref(flags)):
                return None
            return int(flags.value)
        finally:
            cf.CFRelease(target)
    except Exception:  # noqa: BLE001
        return None


@lru_cache(maxsize=1)
def _libraries() -> dict[str, Any] | None:
    if platform.system() != "Darwin":
        return None
    try:
        iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
        iokit.IOPMConnectionGetSystemCapabilities.restype = ctypes.c_uint32
        iokit.IOPMConnectionGetSystemCapabilities.argtypes = []
        sc = ctypes.CDLL(
            "/System/Library/Frameworks/SystemConfiguration.framework/SystemConfiguration"
        )
        sc.SCNetworkReachabilityCreateWithAddress.restype = ctypes.c_void_p
        sc.SCNetworkReachabilityCreateWithAddress.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        sc.SCNetworkReachabilityGetFlags.restype = ctypes.c_bool
        sc.SCNetworkReachabilityGetFlags.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32),
        ]
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        return {"iokit": iokit, "sc": sc, "cf": cf}
    except (OSError, AttributeError) as exc:
        logger.info("DarkWake and network checks unavailable: %s", exc)
        return None
