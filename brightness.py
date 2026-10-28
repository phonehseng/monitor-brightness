#!/usr/bin/env python3
"""Change external monitor brightness over DDC/CI, using the monitorcontrol library.

Usage:
    pythonw brightness.py                    run in the system tray (click the icon for sliders)
    python brightness.py list                list detected monitors
    python brightness.py get [MON]           print brightness
    python brightness.py set VAL [MON]       set brightness to VAL (0-100), or change it by +N / -N
    python brightness.py install             add Start menu, desktop and Startup shortcuts
    python brightness.py autostart [on|off]  start the tray app when you sign in to Windows

MON is "all" (the default), a number from `list`, or part of a monitor's name.

While the tray app runs it also serves a small API on 127.0.0.1, which the
Stream Deck+ plugin uses to change brightness from a dial.
"""

from __future__ import annotations

import argparse
import ctypes
import faulthandler
import json
import logging
import logging.handlers
import math
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
from ctypes import wintypes
from dataclasses import dataclass
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    from monitorcontrol import get_monitors
except ImportError:
    sys.exit("monitorcontrol is not installed. Run:  py -m pip install -r requirements.txt")

log = logging.getLogger("brightness")

APP_NAME = "MonitorBrightness"
APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP_NAME  # log file and startup copy
API_HOST, API_PORT = "127.0.0.1", 47823
API_HEADER = "X-Monitor-Brightness"  # browsers can't add custom headers cross-site, so web pages can't call the API
LUMINANCE = 0x10                     # VCP code for brightness
RESYNC_AFTER = 10.0                  # seconds before a relative change re-reads the monitor first
IS_WINDOWS = sys.platform == "win32"


# --- Display detection (on Windows, names come from each monitor's EDID) -----------------------

if IS_WINDOWS:
    import winreg

    # A private user32 instance, so the argtypes set here can't clash with pystray's.
    _user32 = ctypes.WinDLL("user32", use_last_error=True)

    class _MONITORINFOEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]

    class _DISPLAY_DEVICEW(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32),
                    ("DeviceString", wintypes.WCHAR * 128), ("StateFlags", wintypes.DWORD),
                    ("DeviceID", wintypes.WCHAR * 128), ("DeviceKey", wintypes.WCHAR * 128)]

    _MONITORENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                          ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    _user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), _MONITORENUMPROC, wintypes.LPARAM]
    _user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MONITORINFOEXW)]
    _user32.EnumDisplayDevicesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(_DISPLAY_DEVICEW), wintypes.DWORD]
    _user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    _user32.MonitorFromPoint.restype = wintypes.HMONITOR
    _user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]


def _hmonitors():
    """(hmonitor, MONITORINFOEXW) for every active display, in Windows' enumeration order."""
    found = []

    def callback(hmonitor, _hdc, _rect, _data):
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if _user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            found.append((hmonitor, info))
        return True

    _user32.EnumDisplayMonitors(None, None, _MONITORENUMPROC(callback), 0)
    return found


def _edid_monitors(adapter):
    r"""(id, name) for each active monitor on an adapter such as '\\.\DISPLAY1'."""
    result, index = [], 0
    while True:
        device = _DISPLAY_DEVICEW()
        device.cb = ctypes.sizeof(device)
        if not _user32.EnumDisplayDevicesW(adapter, index, ctypes.byref(device), 1):  # EDD_GET_DEVICE_INTERFACE_NAME
            return result
        index += 1
        if device.StateFlags & 1:  # DISPLAY_DEVICE_ACTIVE
            result.append(_read_edid(device.DeviceID))


def _read_edid(interface):
    r"""Turn '\\?\DISPLAY#DELA1B2#5&2b3c&0&UID4352#{guid}' into (stable id, model name) via the EDID in the registry."""
    parts = interface.split("#")
    if len(parts) < 3:
        return None, None
    code, instance = parts[1], parts[2]
    try:
        key_path = rf"SYSTEM\CurrentControlSet\Enum\DISPLAY\{code}\{instance}\Device Parameters"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
            edid = bytes(winreg.QueryValueEx(key, "EDID")[0])
    except OSError:
        return f"{code}-{instance}", None
    name = None
    for offset in (54, 72, 90, 108):  # the four 18-byte descriptor blocks
        block = edid[offset:offset + 18]
        if len(block) == 18 and block[:3] == b"\0\0\0" and block[3] == 0xFC:  # 0xFC = monitor name
            name = block[5:].split(b"\n")[0].decode("ascii", "replace").strip() or None
            break
    serial = int.from_bytes(edid[12:16], "little") if len(edid) >= 16 else 0
    return (f"{code}-{serial:08X}" if serial else f"{code}-{instance}"), name


def _display_signature():
    """Cheap fingerprint of the current display layout, to notice hot-plugging."""
    if not IS_WINDOWS:
        return None
    return tuple((info.szDevice, info.rcMonitor.left, info.rcMonitor.top, info.rcMonitor.right, info.rcMonitor.bottom)
                 for _, info in _hmonitors())


def detect_displays():
    if IS_WINDOWS:
        try:
            return _detect_windows()
        except Exception:
            log.exception("Detailed display detection failed; using generic names")
    return [Display(monitor, f"display{n}", f"Display {n}") for n, monitor in enumerate(get_monitors(), 1)]


def _detect_windows():
    # Same enumeration monitorcontrol's get_monitors() does, but keeping track of which
    # display each handle belongs to so it can be named and sorted left to right.
    from monitorcontrol.monitorcontrol import Monitor
    from monitorcontrol.vcp.vcp_windows import WindowsVCP

    found = []
    for hmonitor, info in _hmonitors():
        edid = _edid_monitors(info.szDevice)
        handles = WindowsVCP._physical_monitors_from_hmonitor(wintypes.HMONITOR(hmonitor))
        for k, (handle, description) in enumerate(handles):
            ident, name = edid[k] if k < len(edid) else (None, None)
            found.append(((info.rcMonitor.left, info.rcMonitor.top, k), Monitor(WindowsVCP(handle, description)), ident, name))
    found.sort(key=lambda f: f[0])

    names = [name for _, _, _, name in found]
    displays, seen = [], set()
    for n, (_, monitor, ident, name) in enumerate(found, 1):
        ident = ident or f"display{n}"
        if ident in seen:
            ident = f"{ident}#{n}"
        seen.add(ident)
        if name is None:
            name = f"Display {n}"
        elif names.count(name) > 1:
            name = f"{name} ({n})"
        displays.append(Display(monitor, ident, name))
    return displays


# --- Brightness state ---------------------------------------------------------------------------

class Display:
    """One physical monitor. Changes are queued to a worker thread, so a fast-moving slider
    or dial only ever sends the latest value over the (slow) DDC/CI bus."""

    def __init__(self, monitor, ident, name):
        self.monitor, self.id, self.name = monitor, ident, name
        self.value = None      # brightness in percent; None if the monitor didn't answer
        self.maximum = 100     # the monitor's raw maximum for VCP 0x10
        self.checked = 0.0     # when `value` was last read or set
        self.requested = 0.0   # when a change was last requested
        self.failed = False    # last write failed (e.g. unplugged): rescan on next refresh
        self._ddc = threading.Lock()
        self._wake = threading.Condition()
        self._pending = None
        self._closed = False
        self._worker = None

    def read(self):
        """Read brightness from the monitor (blocks ~50 ms). Returns the percent, or None."""
        self.checked = time.monotonic()
        try:
            with self._ddc, self.monitor:
                current, maximum = self.monitor.vcp.get_vcp_feature(LUMINANCE)
        except Exception as exc:
            log.debug("%s: read failed: %s", self.name, exc)
            self.value = None
            return None
        self.maximum = maximum or 100
        self.value = round(current * 100 / self.maximum)
        return self.value

    def write(self, percent):
        """Send a brightness to the monitor right away (blocking)."""
        with self._ddc, self.monitor:
            self.monitor.set_luminance(round(percent * self.maximum / 100))

    def request(self, percent):
        """Queue a brightness change and return immediately."""
        percent = max(0, min(100, int(round(percent))))
        with self._wake:
            self.value = self._pending = percent
            self.checked = self.requested = time.monotonic()
            if self._worker is None:
                self._worker = threading.Thread(target=self._write_loop, name=f"ddc {self.name}", daemon=True)
                self._worker.start()
            self._wake.notify()
        return percent

    @property
    def busy(self):
        return self._pending is not None or time.monotonic() - self.requested < 1.5

    def close(self):
        with self._wake:
            self._closed = True
            self._wake.notify()

    def _write_loop(self):
        while True:
            with self._wake:
                self._wake.wait_for(lambda: self._pending is not None or self._closed)
                if self._closed:
                    return
                percent, self._pending = self._pending, None
            try:
                self.write(percent)
                self.failed = False
            except Exception as exc:
                self.failed = True
                log.warning("%s: setting brightness to %s%% failed: %s", self.name, percent, exc)
            time.sleep(0.03)  # give the monitor's DDC/CI controller a breather between commands


class Hub:
    """The detected displays, plus a change counter that API clients can long-poll."""

    def __init__(self):
        self.displays = []
        self.instance = secrets.token_hex(4)  # lets clients notice a restart (version starts over)
        self.version = 0
        self.listeners = []                   # called from any thread after every change
        self._changed = threading.Condition()
        self._scan_lock = threading.RLock()
        self._signature = None

    def scan(self, force=False):
        """Re-detect monitors if the display layout changed. Returns True if it rescanned."""
        with self._scan_lock:
            signature = _display_signature()
            if not force and signature == self._signature:
                return False
            displays = detect_displays()
            for display in displays:
                display.read()
            old, self.displays, self._signature = self.displays, displays, signature
            for display in old:
                display.close()
            log.info("Monitors: %s", ", ".join(f"{d.name} ({d.id})" for d in displays) or "none")
        self.notify()
        return True

    def refresh(self):
        """Pick up hot-plugged monitors and changes made elsewhere (monitor buttons, other apps)."""
        with self._scan_lock:
            if self.scan(force=any(d.failed for d in self.displays)):
                return
            changed = False
            for display in self.displays:
                if not display.busy:
                    before = display.value
                    changed |= display.read() != before
        if changed:
            self.notify()

    def select(self, target="all"):
        """Displays matching "all", a 1-based number, an id, or part of a name."""
        target = str(target).strip()
        if target.lower() == "all":
            return list(self.displays)
        if target.isdigit():
            n = int(target)
            return self.displays[n - 1:n] if n > 0 else []
        exact = [d for d in self.displays if d.id == target]
        return exact or [d for d in self.displays if target.lower() in d.name.lower()]

    def set(self, target, percent):
        for display in self.select(target):
            display.request(percent)
        self.notify()

    def adjust(self, target, delta):
        for display in self.select(target):
            if not display.busy and time.monotonic() - display.checked > RESYNC_AFTER:
                display.read()  # the monitor's own buttons may have changed it since we last looked
            if display.value is not None:
                display.request(display.value + delta)
        self.notify()

    def state(self):
        return {"instance": self.instance, "version": self.version,
                "monitors": [{"id": d.id, "name": d.name, "brightness": d.value} for d in self.displays]}

    def notify(self):
        with self._changed:
            self.version += 1
            self._changed.notify_all()
        for listener in list(self.listeners):
            try:
                listener()
            except Exception:
                log.exception("listener failed")

    def wait(self, since, timeout):
        """Block until the version differs from `since` (or the timeout passes)."""
        with self._changed:
            self._changed.wait_for(lambda: self.version != since, timeout)


# --- Local API (used by the Stream Deck plugin) -------------------------------------------------

class ApiServer(ThreadingHTTPServer):
    allow_reuse_address = False  # on Windows SO_REUSEADDR would let a second copy share the port
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class ApiHandler(BaseHTTPRequestHandler):
    """GET  /monitors[?refresh=1][&since=VERSION]   state; `since` long-polls until something changes
    POST /brightness {"monitor": "all"|id, "value": N} or {..., "delta": N}
    POST /show                                      open the tray flyout"""

    hub: Hub = None
    show = None

    def do_GET(self):
        if not self._allowed():
            return self._reply(403, {"error": "forbidden"})
        url = urlparse(self.path)
        if url.path != "/monitors":
            return self._reply(404, {"error": "not found"})
        query = parse_qs(url.query)
        try:
            since = int(query["since"][0]) if "since" in query else None
        except ValueError:
            return self._reply(400, {"error": "bad since"})
        if "refresh" in query:
            self.hub.refresh()
        if since is not None:
            self.hub.wait(since, timeout=50)
        self._reply(200, self.hub.state())

    def do_POST(self):
        if not self._allowed():
            return self._reply(403, {"error": "forbidden"})
        path = urlparse(self.path).path
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if path == "/brightness":
                target = body.get("monitor", "all")
                if "value" in body:
                    self.hub.set(target, int(body["value"]))
                elif "delta" in body:
                    self.hub.adjust(target, int(body["delta"]))
                else:
                    return self._reply(400, {"error": "need value or delta"})
                return self._reply(200, self.hub.state())
        except (ValueError, TypeError, AttributeError):
            return self._reply(400, {"error": "bad request"})
        if path == "/show" and self.show:
            self.show()
            return self._reply(200, {})
        self._reply(404, {"error": "not found"})

    def _allowed(self):
        host = self.headers.get("Host", "").rsplit(":", 1)[0]
        return self.headers.get(API_HEADER) == "1" and host in ("127.0.0.1", "localhost")

    def _reply(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        log.debug("api: " + fmt, *args)


def api_request(path, payload=None, timeout=2.0):
    """Call the running tray app's API (bypassing any system proxy)."""
    request = urllib.request.Request(
        f"http://{API_HOST}:{API_PORT}{path}",
        data=None if payload is None else json.dumps(payload).encode(),
        headers={API_HEADER: "1", "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        return json.load(response)


# --- Tray icon and slider flyout ----------------------------------------------------------------

@dataclass
class Theme:
    dark: bool
    bg: str
    fg: str
    dim: str
    track: str
    accent: str
    thumb: str
    thumb_border: str


def load_theme():
    """Match the taskbar's light/dark mode and the Windows accent color."""
    dark, palette = True, b""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            dark = winreg.QueryValueEx(key, "SystemUsesLightTheme")[0] == 0
    except OSError:
        pass
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\Accent") as key:
            palette = bytes(winreg.QueryValueEx(key, "AccentPalette")[0])
    except OSError:
        pass

    def accent(index, fallback, bg, plain):
        # AccentPalette holds 8 RGBA shades, lightest first. A black/gray accent would
        # barely show against the flyout, so fall back to a plain neutral fill then.
        if len(palette) < 32:
            return fallback
        color = "#%02x%02x%02x" % tuple(palette[index * 4:index * 4 + 3])
        return color if _contrast(color, bg) >= 3 else plain

    if dark:
        bg = "#2c2c2c"
        return Theme(True, bg, "#ffffff", "#c5c5c5", "#8a8a8a", accent(1, "#4cc2ff", bg, "#f0f0f0"), "#454545", "#3d3d3d")
    bg = "#f3f3f3"
    return Theme(False, bg, "#1a1a1a", "#5f5f5f", "#9a9a9a", accent(4, "#005fb8", bg, "#303030"), "#ffffff", "#d0d0d0")


def _contrast(a, b):
    """WCAG contrast ratio between two #rrggbb colors."""
    def luminance(color):
        channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        r, g, b = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def sun_image(size, color):
    """A brightness 'sun' glyph, drawn large and scaled down for smooth edges."""
    from PIL import Image, ImageDraw

    big = 256
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    c, w, r = big / 2, big * 0.09, big * 0.19
    draw.ellipse((c - r, c - r, c + r, c + r), outline=color, width=round(w))
    for k in range(8):
        a = k * math.pi / 4
        ends = [(c + big * f * math.cos(a), c + big * f * math.sin(a)) for f in (0.33, 0.45)]
        draw.line(ends, fill=color, width=round(w))
        for x, y in ends:  # round caps
            draw.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=color)
    return image.resize((size, size), Image.LANCZOS)


def thumb_image(theme, size, inner):
    """Slider thumb: a ring with an accent-colored dot, like Windows 11."""
    from PIL import Image, ImageDraw, ImageTk

    big = size * 4
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((0, 0, big - 1, big - 1), fill=theme.thumb_border)
    draw.ellipse((4, 4, big - 5, big - 5), fill=theme.thumb)
    r, c = big * inner / 2, big / 2
    draw.ellipse((c - r, c - r, c + r, c + r), fill=theme.accent)
    return ImageTk.PhotoImage(image.resize((size, size), Image.LANCZOS))


class Slider(tk.Canvas):
    """A 0-100 horizontal slider drawn on a canvas."""

    def __init__(self, master, theme, scale, thumbs, command):
        width, height = round(200 * scale), round(32 * scale)
        super().__init__(master, width=width, height=height, bg=theme.bg, highlightthickness=0, bd=0)
        self.command = command
        self.x0, self.x1, self.cy = round(10 * scale), width - round(10 * scale), height // 2
        line = dict(width=max(2, round(4 * scale)), capstyle="round")
        self.create_line(self.x0, self.cy, self.x1, self.cy, fill=theme.track, **line)
        self.fill = self.create_line(self.x0, self.cy, self.x0, self.cy, fill=theme.accent, **line)
        self.thumb = self.create_image(self.x0, self.cy, image=thumbs[0])
        self.value, self._wheel = 0, 0
        self.bind("<Button-1>", self._drag)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<Enter>", lambda e: self.itemconfigure(self.thumb, image=thumbs[1]))
        self.bind("<Leave>", lambda e: self.itemconfigure(self.thumb, image=thumbs[0]))

    def set(self, value):
        self.value = value
        x = self.x0 + (self.x1 - self.x0) * value / 100
        self.coords(self.fill, self.x0, self.cy, x, self.cy)
        self.coords(self.thumb, x, self.cy)

    def scroll(self, delta):
        self._wheel += delta
        steps = int(self._wheel / 60)  # one wheel notch (120) = 2%, like the Windows volume flyout
        self._wheel -= steps * 60
        self._change(self.value + steps)

    def _drag(self, event):
        self._change(round((event.x - self.x0) * 100 / (self.x1 - self.x0)))

    def _change(self, value):
        value = max(0, min(100, value))
        if value != self.value:
            self.set(value)
            self.command(value)


def _fmt(value):
    return "–" if value is None else str(value)


class TrayApp:
    """Tray icon; clicking it opens a flyout with a brightness slider per monitor."""

    def __init__(self, hub):
        import pystray

        self.hub = hub
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.report_callback_exception = lambda *exc: log.error("UI error", exc_info=exc)
        self.root.bind("<FocusOut>", lambda e: self.root.after(150, self._hide_if_unfocused))
        self.root.bind("<Escape>", lambda e: self.hide())
        self.root.bind_all("<MouseWheel>", self._wheel)
        self.scale = self.root.winfo_fpixels("1i") / 96
        self.theme = load_theme()
        self.visible = False
        self._hidden_at = 0.0
        self._watch_job = None
        self._rows = {}     # display id (or "all") -> (slider, value label)
        self._ids = None
        self._images = []   # keep PhotoImages alive

        menu = pystray.Menu(
            pystray.MenuItem("Show", lambda: self.call(self.toggle), default=True, visible=False),
            pystray.MenuItem("Refresh monitors", lambda: threading.Thread(target=hub.scan, args=(True,), daemon=True).start()),
            pystray.MenuItem("Start with Windows", self._toggle_autostart, checked=lambda item: autostart_enabled()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit),
        )
        self.icon = pystray.Icon(APP_NAME, self._tray_image(), "Monitor Brightness", menu)
        hub.listeners += [lambda: self.call(self._sync), self._update_tooltip]
        self._update_tooltip()

    def run(self):
        threading.Thread(target=self.icon.run, name="tray", daemon=True).start()
        self.root.mainloop()

    def call(self, fn, *args):
        """Run fn on the Tk thread; safe to call from the tray, API and DDC threads."""
        try:
            self.root.after(0, fn, *args)
        except RuntimeError:
            pass  # the UI is shutting down

    def toggle(self):
        if self.visible:
            self.hide()
        elif time.monotonic() - self._hidden_at > 0.4:  # this click is what just closed it
            self.show()

    def show(self):
        theme, was_dark = load_theme(), self.theme.dark
        self.theme = theme
        if theme.dark != was_dark:
            self.icon.icon = self._tray_image()
        self._build()
        self._place()
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        frame = int(self.root.wm_frame(), 16)
        _user32.SetForegroundWindow(frame)
        _round_corners(frame)
        self.visible = True
        if self._watch_job:
            self.root.after_cancel(self._watch_job)
        self._watch(frame)
        threading.Thread(target=self.hub.refresh, daemon=True).start()  # rows update when it finishes

    def hide(self):
        if self.visible:
            self.root.withdraw()
            self.visible = False
            self._hidden_at = time.monotonic()
        if self._watch_job:
            self.root.after_cancel(self._watch_job)
            self._watch_job = None

    def _watch(self, frame, misses=0):
        """Windows sometimes refuses to give us focus, and then FocusOut never fires.
        As a fallback, close once the pointer has been elsewhere for a few seconds."""
        x, y = self.root.winfo_pointerxy()
        inside = (0 <= x - self.root.winfo_rootx() < self.root.winfo_width()
                  and 0 <= y - self.root.winfo_rooty() < self.root.winfo_height())
        misses = 0 if inside or _user32.GetForegroundWindow() == frame else misses + 1
        if misses >= 3:
            self.hide()
        else:
            self._watch_job = self.root.after(1000, self._watch, frame, misses)

    def _hide_if_unfocused(self):
        try:
            focused = self.root.focus_get()
        except KeyError:
            focused = None
        if focused is None:
            self.hide()

    def _build(self):
        t, s = self.theme, self.scale
        for child in self.root.winfo_children():
            child.destroy()
        self._rows.clear()
        self._images.clear()
        self.root.configure(bg=t.bg)
        body = tk.Frame(self.root, bg=t.bg, padx=round(16 * s), pady=round(12 * s))
        body.pack()
        tk.Label(body, text="Brightness", bg=t.bg, fg=t.fg, font=("Segoe UI Semibold", 10)).grid(
            row=0, column=0, columnspan=3, sticky="w")

        sun = _photo(sun_image(round(18 * s), t.fg))
        thumbs = (thumb_image(t, round(20 * s), 0.55), thumb_image(t, round(20 * s), 0.7))
        self._images += [sun, *thumbs]

        displays = self.hub.displays
        entries = [(d.id, d.name) for d in displays]
        if len(displays) > 1:
            entries.insert(0, ("all", "All displays"))
        row = 1
        for key, name in entries:
            tk.Label(body, text=name, bg=t.bg, fg=t.dim, font=("Segoe UI", 9)).grid(
                row=row, column=0, columnspan=3, sticky="w", pady=(round(10 * s), 0))
            tk.Label(body, image=sun, bg=t.bg).grid(row=row + 1, column=0, padx=(0, round(6 * s)))
            slider = Slider(body, t, s, thumbs, partial(self.hub.set, key))
            slider.grid(row=row + 1, column=1)
            label = tk.Label(body, bg=t.bg, fg=t.fg, font=("Segoe UI", 10), width=3, anchor="e")
            label.grid(row=row + 1, column=2, padx=(round(6 * s), 0))
            self._rows[key] = (slider, label)
            row += 2
        if not displays:
            tk.Label(body, text="No DDC/CI monitors found", bg=t.bg, fg=t.dim, font=("Segoe UI", 9)).grid(
                row=row, column=0, pady=(round(10 * s), 0))
        self._ids = [d.id for d in displays]
        self._show_values()

    def _sync(self):
        """Bring the open flyout up to date with the hub (changes from the dial, a refresh, ...)."""
        if not self.visible:
            return
        if [d.id for d in self.hub.displays] != self._ids:
            self._build()
            self._place()
        else:
            self._show_values()

    def _show_values(self):
        values = {d.id: d.value for d in self.hub.displays}
        known = [v for v in values.values() if v is not None]
        values["all"] = round(sum(known) / len(known)) if known else None
        for key, (slider, label) in self._rows.items():
            if values[key] is not None and values[key] != slider.value:
                slider.set(values[key])
            label.config(text=_fmt(values[key]))

    def _place(self):
        self.root.update_idletasks()
        width, height = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        left, top, right, bottom = _work_area_at_cursor()
        margin = round(12 * self.scale)
        self.root.geometry(f"{width}x{height}+{right - width - margin}+{bottom - height - margin}")

    def _wheel(self, event):
        # Tk sends wheel events to the focused window, so route them to the slider under the pointer.
        widget = self.root.winfo_containing(event.x_root, event.y_root)
        if isinstance(widget, Slider):
            widget.scroll(event.delta)

    def _tray_image(self):
        return sun_image(64, "#ffffff" if self.theme.dark else "#000000")

    def _update_tooltip(self):
        lines = ["Monitor Brightness"] + [f"{d.name}: {'n/a' if d.value is None else f'{d.value}%'}"
                                          for d in self.hub.displays]
        title = "\n".join(lines)[:127]
        if self.icon.title != title:
            self.icon.title = title

    def _toggle_autostart(self, icon, item):
        set_autostart(not autostart_enabled())
        icon.update_menu()

    def _quit(self, icon, item):
        log.info("Quit from the tray menu")
        icon.stop()
        self.call(self.root.quit)


def _photo(image):
    from PIL import ImageTk
    return ImageTk.PhotoImage(image)


def _work_area_at_cursor():
    point = wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(point))
    info = _MONITORINFOEXW()
    info.cbSize = ctypes.sizeof(info)
    _user32.GetMonitorInfoW(_user32.MonitorFromPoint(point, 2), ctypes.byref(info))  # MONITOR_DEFAULTTONEAREST
    r = info.rcWork
    return r.left, r.top, r.right, r.bottom


def _round_corners(hwnd):
    try:
        preference = ctypes.c_int(2)  # DWMWCP_ROUND (Windows 11; ignored elsewhere)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(wintypes.HWND(hwnd), 33, ctypes.byref(preference), 4)
    except OSError:
        pass


SHORTCUT_NAME = "Monitor Brightness.lnk"
SHORTCUT_PLACES = {  # Windows known-folder IDs
    "start menu": "{A77F5D77-2E2B-44C3-A6A2-ABA601054A51}",
    "desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    "startup": "{B97D20BB-F46A-4C97-BA10-5E3608430854}",  # shortcuts here run at sign-in
}
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def shortcut_path(place):
    folder_id = ctypes.create_string_buffer(16)
    ctypes.oledll.ole32.CLSIDFromString(SHORTCUT_PLACES[place], folder_id)
    folder = ctypes.c_wchar_p()
    ctypes.oledll.shell32.SHGetKnownFolderPath(folder_id, 0, None, ctypes.byref(folder))
    try:
        return Path(folder.value) / SHORTCUT_NAME
    finally:
        ctypes.windll.ole32.CoTaskMemFree(folder)


def create_shortcuts(places):
    """Shortcuts that run this script's tray app (Windows Script Host writes the .lnk files)."""
    script = Path(__file__).resolve()
    icon = script.with_name("brightness.ico")
    links = [shortcut_path(place) for place in places]
    env = dict(os.environ, MB_LINKS="|".join(map(str, links)), MB_TARGET=str(_pythonw()), MB_ARGS=f'"{script}"',
               MB_DIR=str(script.parent), MB_ICON=f"{icon},0" if icon.exists() else "")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                    "$shell = New-Object -ComObject WScript.Shell; "
                    "foreach ($path in $env:MB_LINKS.Split('|')) { "
                    "$link = $shell.CreateShortcut($path); $link.TargetPath = $env:MB_TARGET; "
                    "$link.Arguments = $env:MB_ARGS; $link.WorkingDirectory = $env:MB_DIR; "
                    "if ($env:MB_ICON) { $link.IconLocation = $env:MB_ICON }; "
                    "$link.Description = 'Monitor brightness sliders in the system tray'; $link.Save() }"],
                   env=env, check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    return links


def autostart_enabled():
    return shortcut_path("startup").exists()


def set_autostart(enabled):
    """Start the tray app at sign-in, via a shortcut in your Startup folder."""
    if enabled:
        create_shortcuts(["startup"])
    else:
        shortcut_path("startup").unlink(missing_ok=True)


def _migrate_autostart():
    """Earlier versions started from a Run registry entry pointing at a copy of the script in
    %LOCALAPPDATA%. Swap that for the Startup shortcut, so the app doesn't start twice."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_ALL_ACCESS) as key:
            winreg.QueryValueEx(key, APP_NAME)
            winreg.DeleteValue(key, APP_NAME)
    except OSError:
        return  # nothing to migrate
    set_autostart(True)
    old_copy = APP_DIR / "brightness.py"
    if old_copy.resolve() != Path(__file__).resolve():
        old_copy.unlink(missing_ok=True)
    log.info("Moved autostart from the registry to the Startup folder")


def _pythonw():
    """pythonw.exe runs the tray app without a console window."""
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return pythonw if pythonw.exists() else Path(sys.executable)


def _tray_running():
    try:
        api_request("/monitors", timeout=1)
        return True
    except OSError:
        return False


def run_tray():
    if not IS_WINDOWS:
        sys.exit("The tray app is Windows-only; use the list/get/set commands instead.")
    APP_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(APP_DIR / "brightness.log", maxBytes=256_000, backupCount=1, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[handler])
    try:
        _migrate_autostart()
    except Exception:
        log.exception("Couldn't move autostart to the Startup folder")
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor DPI aware: crisp text
    except OSError:
        pass

    try:
        server = ApiServer((API_HOST, API_PORT), ApiHandler)
    except OSError:
        # Port taken: most likely we're already running, so pop up that copy's flyout instead.
        _user32.AllowSetForegroundWindow(0xFFFFFFFF)  # ASFW_ANY
        try:
            api_request("/show")
        except OSError:
            log.error("Port %s is in use by another program", API_PORT)
        return

    _log_crashes()
    log.info("Started (pid %s) from %s", os.getpid(), Path(__file__).resolve())
    hub = Hub()
    hub.scan(force=True)
    app = TrayApp(hub)
    ApiHandler.hub = hub
    ApiHandler.show = staticmethod(lambda: app.call(app.show))
    # A long poll interval keeps the idle server thread asleep; requests still wake it instantly.
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 3600}, name="api", daemon=True).start()
    app.run()
    log.info("Exited")


def _log_crashes():
    """pythonw has no console, so a fatal error would otherwise vanish. Python errors (from any
    thread) go to brightness.log; native crashes and anything printed to stderr go to crash.log."""
    path = APP_DIR / "crash.log"
    big = path.exists() and path.stat().st_size > 1_000_000
    stream = open(path, "w" if big else "a", encoding="utf-8", buffering=1)
    stream.write(f"--- started {time.strftime('%Y-%m-%d %H:%M:%S')} (pid {os.getpid()})\n")
    if sys.stderr is None:
        sys.stderr = stream  # e.g. "Exception ignored in ..." messages and warnings
    faulthandler.enable(stream)  # access violations, aborts: dumps every thread's Python stack

    def thread_hook(args):
        if not issubclass(args.exc_type, SystemExit):
            name = args.thread.name if args.thread else "?"
            log.critical("Uncaught error in thread %s", name, exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = lambda *exc: log.critical("Uncaught error", exc_info=exc)
    threading.excepthook = thread_hook


# --- Command line -------------------------------------------------------------------------------

def _pick(hub, target):
    displays = hub.select(target)
    if not displays:
        names = ", ".join(f"{n}={d.name}" for n, d in enumerate(hub.displays, 1)) or "none detected"
        sys.exit(f"No monitor matches {target!r} (monitors: {names})")
    return displays


def cli_list(hub):
    if not hub.displays:
        print("No monitors detected.")
        return
    width = max(len(d.name) for d in hub.displays)
    print(f"#  {'Monitor':<{width}}  Brightness  ID")
    for n, d in enumerate(hub.displays, 1):
        value = "no DDC/CI" if d.value is None else f"{d.value}%"
        print(f"{n}  {d.name:<{width}}  {value:<10}  {d.id}")


def cli_get(hub, target):
    for d in _pick(hub, target):
        print(f"{d.name}: {'no DDC/CI' if d.value is None else f'{d.value}%'}")


def cli_set(hub, value, target):
    try:
        amount = int(value)
    except ValueError:
        sys.exit(f"Brightness must be a number like 60, +10 or -10 (got {value!r})")
    relative = value.strip()[0] in "+-"
    failures = 0
    for d in _pick(hub, target):
        if relative and d.value is None:
            print(f"{d.name}: can't read current brightness (no DDC/CI?)")
            failures += 1
            continue
        new = max(0, min(100, d.value + amount if relative else amount))
        try:
            d.write(new)
        except Exception as exc:
            print(f"{d.name}: failed ({exc})")
            failures += 1
            continue
        print(f"{d.name}: {_fmt(d.value)}% -> {new}%")
    return 1 if failures else 0


def cli_install(remove=False):
    if not IS_WINDOWS:
        sys.exit("Shortcuts are Windows-only.")
    if remove:
        for place in SHORTCUT_PLACES:
            shortcut_path(place).unlink(missing_ok=True)
        print("Removed the Start menu, desktop and Startup shortcuts.")
        return 0
    links = create_shortcuts(SHORTCUT_PLACES)
    for link in links:
        print(f"Created {link}")
    return _after_setup(links)


def cli_autostart(state):
    if not IS_WINDOWS:
        sys.exit("Autostart is Windows-only.")
    if state:
        set_autostart(state == "on")
    link = shortcut_path("startup")
    print(f"Autostart is on: {link}" if link.exists() else "Autostart is off.")
    return _after_setup([link]) if state == "on" else 0


def _after_setup(links):
    """Check Windows can see the new shortcuts, then start the tray app if it isn't running."""
    if any("\\packages\\" in str(link.resolve()).lower() for link in links):
        # Terminals inside another app's container (like the Claude desktop app's) can have
        # their AppData writes redirected into that app's storage under %LOCALAPPDATA%\Packages.
        print("\nWarning: this terminal runs inside another app's container, which keeps these shortcuts\n"
              "private. Run the command again from Windows Terminal (Start menu > Terminal).")
        return 1
    if _tray_running():
        print("The tray app is already running.")
    else:
        # Opening the shortcut through Explorer starts the app on its own, like double-clicking
        # it, instead of tying it to this terminal (which may close it when the terminal closes).
        subprocess.Popen(["explorer.exe", str(links[0])])
        print("Started the tray app.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     epilog='MON is "all" (default), a number from `list`, or part of a monitor name.')
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("tray", help="run in the system tray (the default)")
    commands.add_parser("list", help="list detected monitors")
    get = commands.add_parser("get", help="print brightness")
    get.add_argument("monitor", nargs="?", default="all")
    set_ = commands.add_parser("set", help="set brightness")
    set_.add_argument("value", help="0-100, or +N / -N to change it")
    set_.add_argument("monitor", nargs="?", default="all")
    commands.add_parser("install", help="add Start menu, desktop and Startup shortcuts, then start the tray app")
    commands.add_parser("uninstall", help="remove those shortcuts")
    auto = commands.add_parser("autostart", help="start the tray app when you sign in to Windows")
    auto.add_argument("state", nargs="?", choices=["on", "off"], help="leave out to show the current setting")
    args = parser.parse_args(argv)

    if args.command in (None, "tray"):
        return run_tray()
    if args.command in ("install", "uninstall"):
        return cli_install(remove=args.command == "uninstall")
    if args.command == "autostart":
        return cli_autostart(args.state)
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    hub = Hub()
    hub.scan(force=True)
    if args.command == "list":
        return cli_list(hub)
    if args.command == "get":
        return cli_get(hub, args.monitor)
    return cli_set(hub, args.value, args.monitor)


if __name__ == "__main__":
    sys.exit(main())
