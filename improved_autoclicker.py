import ctypes
import json
import logging
import os
import queue
import random
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import keyboard
import pystray
from PIL import Image, ImageDraw, ImageTk
from pystray import Menu, MenuItem

try:
    import winsound
except ImportError:
    winsound = None

try:
    import winreg
except ImportError:
    winreg = None

APP_NAME = "Improved AutoClicker"
APP_ID = "ImprovedAutoClicker"
CONFIG_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), APP_ID)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
LOG_PATH = os.path.join(CONFIG_DIR, "autoclicker.log")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = APP_ID
DEFAULT_HOTKEY = "f6"

DEFAULT_CONFIG = {
    "interval_seconds": 0,
    "interval_milliseconds": 100,
    "hotkey": DEFAULT_HOTKEY,
    "randomize": False,
}

MIN_INTERVAL_MS = 10
MAX_INTERVAL_SECONDS = 9999
MAX_INTERVAL_MS = MAX_INTERVAL_SECONDS * 1000 + 999
SECONDS_DIGITS = 4
MILLISECONDS_DIGITS = 3
MAX_CLICK_FAILURES = 5
HOTKEY_POLL_MS = 25
JITTER_LOW = 0.8
JITTER_HIGH = 1.2

MODIFIER_KEYS = {
    "ctrl",
    "left ctrl",
    "right ctrl",
    "shift",
    "left shift",
    "right shift",
    "alt",
    "left alt",
    "right alt",
    "alt gr",
    "windows",
    "left windows",
    "right windows",
    "cmd",
}

VK_GROUPS = {
    "ctrl": (0xA2, 0xA3),
    "left ctrl": (0xA2,),
    "right ctrl": (0xA3,),
    "shift": (0xA0, 0xA1),
    "left shift": (0xA0,),
    "right shift": (0xA1,),
    "alt": (0xA4, 0xA5),
    "left alt": (0xA4,),
    "right alt": (0xA5,),
    "alt gr": (0xA5,),
    "windows": (0x5B, 0x5C),
    "win": (0x5B, 0x5C),
    "cmd": (0x5B, 0x5C),
    "left windows": (0x5B,),
    "right windows": (0x5C,),
    "space": (0x20,),
    "tab": (0x09,),
    "enter": (0x0D,),
    "return": (0x0D,),
    "esc": (0x1B,),
    "escape": (0x1B,),
    "backspace": (0x08,),
    "delete": (0x2E,),
    "insert": (0x2D,),
    "home": (0x24,),
    "end": (0x23,),
    "page up": (0x21,),
    "page down": (0x22,),
    "up": (0x26,),
    "down": (0x28,),
    "left": (0x25,),
    "right": (0x27,),
    "print screen": (0x2C,),
    "pause": (0x13,),
    "caps lock": (0x14,),
    "minus": (0xBD,),
    "-": (0xBD,),
    "plus": (0xBB,),
    "+": (0xBB,),
    "=": (0xBB,),
    "comma": (0xBC,),
    ",": (0xBC,),
    "period": (0xBE,),
    ".": (0xBE,),
    "slash": (0xBF,),
    "/": (0xBF,),
    "backslash": (0xDC,),
    "\\": (0xDC,),
    "semicolon": (0xBA,),
    ";": (0xBA,),
    "quote": (0xDE,),
    "'": (0xDE,),
    "backquote": (0xC0,),
    "`": (0xC0,),
    "left bracket": (0xDB,),
    "[": (0xDB,),
    "right bracket": (0xDD,),
    "]": (0xDD,),
}


def combo_keys(combo):
    text = str(combo).strip().lower()
    keys = [part.strip() for part in text.split("+") if part.strip()]
    if text.endswith("+"):
        keys.append("+")
    return keys


def vk_group(name):
    group = VK_GROUPS.get(name)
    if group is not None:
        return group
    if len(name) == 1 and name.isascii():
        if "a" <= name <= "z" or "0" <= name <= "9":
            return (ord(name.upper()),)
    if len(name) > 1 and name[0] == "f" and name[1:].isascii() and name[1:].isdigit():
        number = int(name[1:])
        if 1 <= number <= 24:
            return (0x70 + number - 1,)
    return ()


def hotkey_vk_groups(combo):
    groups = []
    for name in combo_keys(combo):
        group = vk_group(name)
        if not group:
            raise ValueError(f"Unsupported key in hotkey: {name!r}")
        groups.append(tuple(group))
    if not groups:
        raise ValueError("The hotkey is empty")
    return tuple(groups)


def _vk_down(vk):
    return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)

BG = "#000000"
SURFACE = "#1e1e1e"
BORDER = "#d3d3d3"
TEXT = "#ffffff"
MUTED = "#d3d3d3"

RESAMPLE = getattr(Image, "Resampling", Image).LANCZOS

log = logging.getLogger(APP_ID)


def report_callback_exception(exception_type, error, traceback_object):
    log.error(
        "Unhandled interface error",
        exc_info=(exception_type, error, traceback_object),
    )


def setup_logging():
    log.setLevel(logging.INFO)
    if log.handlers:
        return
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        if os.path.isfile(LOG_PATH) and os.path.getsize(LOG_PATH) > 512 * 1024:
            os.remove(LOG_PATH)
        handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        )
        log.addHandler(handler)
    except OSError:
        log.addHandler(logging.NullHandler())


def _as_int(value, fallback):
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def normalize_config(raw):
    if not isinstance(raw, dict):
        raw = {}
    config = dict(DEFAULT_CONFIG)
    config["interval_seconds"] = max(
        0,
        min(
            MAX_INTERVAL_SECONDS,
            _as_int(raw.get("interval_seconds"), config["interval_seconds"]),
        ),
    )
    config["interval_milliseconds"] = max(
        0,
        min(
            999,
            _as_int(
                raw.get("interval_milliseconds"),
                config["interval_milliseconds"],
            ),
        ),
    )
    total = config["interval_seconds"] * 1000 + config["interval_milliseconds"]
    total = max(MIN_INTERVAL_MS, min(MAX_INTERVAL_MS, total))
    config["interval_seconds"], config["interval_milliseconds"] = divmod(total, 1000)
    hotkey = raw.get("hotkey")
    if isinstance(hotkey, str) and hotkey.strip():
        config["hotkey"] = hotkey.strip().lower()
    config["randomize"] = raw.get("randomize") is True
    return config


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            return normalize_config(json.load(handle))
    except FileNotFoundError:
        config = dict(DEFAULT_CONFIG)
        save_config(config)
        return config
    except (OSError, ValueError) as error:
        log.warning("Could not read the saved settings: %s", error)
        return dict(DEFAULT_CONFIG)


def save_config(config):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        temporary = CONFIG_PATH + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2)
        os.replace(temporary, CONFIG_PATH)
        return True
    except OSError as error:
        log.warning("Could not save the settings: %s", error)
        return False


def remove_legacy_startup():
    if winreg is None:
        return
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE
        ) as key:
            try:
                winreg.QueryValueEx(key, RUN_VALUE)
            except FileNotFoundError:
                return
            winreg.DeleteValue(key, RUN_VALUE)
            log.info("Removed the old Windows startup entry")
    except FileNotFoundError:
        return
    except OSError as error:
        log.warning("Could not remove the old startup entry: %s", error)


def normalize_hotkey(text):
    combo = "+".join(part.strip() for part in str(text).strip().lower().split("+"))
    parts = [part for part in combo.split("+") if part]
    if not parts:
        raise ValueError("empty hotkey")
    if len(parts) == 1 and len(parts[0]) == 1:
        raise ValueError("single-character hotkeys are not allowed")
    if all(part in MODIFIER_KEYS for part in parts):
        raise ValueError("a hotkey needs a key that is not a modifier")
    keyboard.parse_hotkey(combo)
    hotkey_vk_groups(combo)
    return combo


def format_hotkey(combo):
    if not combo:
        return ""
    parts = []
    for part in combo.split("+"):
        if len(part) == 1:
            parts.append(part.upper())
        else:
            parts.append(" ".join(word.capitalize() for word in part.split()))
    return "+".join(parts)


def capture_hotkey(cancel):
    pressed = []
    result = []
    finished = threading.Event()

    def on_event(event):
        if cancel.is_set() or finished.is_set():
            return
        name = event.name
        if name == "esc":
            finished.set()
            return
        if event.event_type == keyboard.KEY_DOWN:
            if name not in pressed:
                pressed.append(name)
            return
        if event.event_type != keyboard.KEY_UP:
            return
        held = [key for key in pressed if key != name]
        pressed[:] = held
        try:
            combo = keyboard.get_hotkey_name(held + [name])
        except Exception:
            log.debug("Could not read the captured keys", exc_info=True)
            return
        parts = [part for part in combo.split("+") if part]
        if parts and all(part in MODIFIER_KEYS for part in parts):
            return
        result.append(combo)
        finished.set()

    handle = keyboard.hook(on_event)
    try:
        while not finished.is_set():
            if cancel.wait(0.05):
                return None
        return result[0] if result else None
    finally:
        try:
            keyboard.unhook(handle)
        except Exception:
            log.debug("Could not remove the capture hook", exc_info=True)


class HotkeyPoller:
    def __init__(self, on_fire):
        self._on_fire = on_fire
        self._groups = ()
        self._armed = True
        self._suspended = False
        self._lock = threading.Lock()

    def is_pressed(self):
        groups = self._groups
        if not groups:
            return False
        return all(any(_vk_down(vk) for vk in group) for group in groups)

    def set_combo(self, combo):
        try:
            groups = hotkey_vk_groups(combo)
        except Exception as error:
            log.warning("Cannot watch the hotkey %r: %s", combo, error)
            return False
        with self._lock:
            self._groups = groups
            self._armed = True
        try:
            if self.is_pressed():
                self._armed = False
        except Exception:
            log.debug("Could not read the initial key state", exc_info=True)
        log.info("Hotkey %r is armed (virtual keys %s)", combo, groups)
        return True

    def clear(self):
        with self._lock:
            self._groups = ()
            self._armed = True

    def set_suspended(self, value):
        self._suspended = bool(value)
        if not value:
            self._armed = True

    def poll(self):
        if self._suspended:
            return
        if not self._groups:
            return
        try:
            pressed = self.is_pressed()
        except Exception:
            log.exception("Could not check the keyboard state")
            return
        with self._lock:
            if pressed:
                fire = self._armed
                self._armed = False
            else:
                self._armed = True
                fire = False
        if not fire:
            return
        try:
            self._on_fire()
        except Exception:
            log.exception("The hotkey action failed")


class ClickEngine:
    def __init__(self):
        self._state_lock = threading.Lock()
        self._config_lock = threading.Lock()
        self._interval = DEFAULT_CONFIG["interval_milliseconds"] / 1000.0
        self._randomize = False
        self._stop = threading.Event()
        self._thread = None
        self.report = None

    @property
    def running(self):
        thread = self._thread
        return thread is not None and thread.is_alive()

    def configure(self, interval_seconds, randomize):
        with self._config_lock:
            self._interval = min(
                MAX_INTERVAL_MS / 1000.0,
                max(MIN_INTERVAL_MS / 1000.0, float(interval_seconds)),
            )
            self._randomize = bool(randomize)

    def stop(self):
        with self._state_lock:
            self._stop.set()
            self._thread = None

    def toggle(self):
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                self._stop.set()
                self._thread = None
                return False
            stop = threading.Event()
            self._stop = stop
            self._thread = threading.Thread(
                target=self._run, args=(stop,), name="ClickEngine", daemon=True
            )
            self._thread.start()
            return True

    def _snapshot(self):
        with self._config_lock:
            return self._interval, self._randomize

    def _report(self, message):
        log.error("%s", message.replace("\n", " "))
        report = self.report
        if report is None:
            return
        try:
            report(message)
        except Exception:
            log.debug("Could not deliver the engine message", exc_info=True)

    def _run(self, stop):
        try:
            import pyautogui
        except Exception as error:
            self._report(
                "Clicking is unavailable because pyautogui could not be loaded.\n\n"
                f"{error}"
            )
            return

        pyautogui.PAUSE = 0
        pyautogui.FAILSAFE = False

        timer = None
        try:
            timer = ctypes.windll.winmm
            timer.timeBeginPeriod(1)
        except Exception:
            timer = None

        failures = 0
        try:
            next_at = time.perf_counter()
            while not stop.is_set():
                interval, jitter = self._snapshot()
                if jitter:
                    started = time.perf_counter()
                    clicked = self._click(pyautogui)
                    delay = interval * random.uniform(JITTER_LOW, JITTER_HIGH)
                    if not self._wait_until(stop, started + delay):
                        break
                else:
                    if not self._wait_until(stop, next_at):
                        break
                    clicked = self._click(pyautogui)
                    next_at += interval
                    now = time.perf_counter()
                    if next_at < now:
                        next_at = now + interval

                failures = 0 if clicked else failures + 1
                if failures >= MAX_CLICK_FAILURES:
                    self._report(
                        "Clicking stopped because the mouse could not be controlled."
                    )
                    break
        finally:
            if timer is not None:
                try:
                    timer.timeEndPeriod(1)
                except Exception:
                    pass

    @staticmethod
    def _wait_until(stop, deadline):
        while not stop.is_set():
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return True
            stop.wait(remaining)
        return False

    @staticmethod
    def _click(pyautogui):
        try:
            pyautogui.click()
            return True
        except Exception:
            log.warning("A click could not be performed", exc_info=True)
            return False


def build_icon_image(size=64, active=False):
    canvas = 256
    background = (255, 255, 255, 255) if active else (30, 30, 30, 255)
    foreground = (0, 0, 0, 255) if active else (255, 255, 255, 255)
    image = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((8, 8, canvas - 9, canvas - 9), radius=56, fill=background)
    arrow = [(94, 58), (94, 190), (121, 163), (142, 204), (166, 193), (145, 152), (184, 152)]
    draw.polygon(arrow, fill=foreground)
    return image.resize((size, size), RESAMPLE)


def enable_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def apply_dark_title_bar(root):
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
        dwm = ctypes.windll.dwmapi
        enabled = ctypes.c_int(1)
        for attribute in (20, 19):
            if (
                dwm.DwmSetWindowAttribute(
                    hwnd, attribute, ctypes.byref(enabled), ctypes.sizeof(enabled)
                )
                == 0
            ):
                break
        caption = ctypes.c_int(0x00000000)
        dwm.DwmSetWindowAttribute(
            hwnd, 35, ctypes.byref(caption), ctypes.sizeof(caption)
        )
        text_color = ctypes.c_int(0x00FFFFFF)
        dwm.DwmSetWindowAttribute(
            hwnd, 36, ctypes.byref(text_color), ctypes.sizeof(text_color)
        )
    except Exception:
        log.debug("The dark title bar is unavailable", exc_info=True)


GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
LWA_ALPHA = 0x00000002


def make_click_through(window):
    try:
        window.update_idletasks()
        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(window.winfo_id()) or window.winfo_id()
        user32.GetWindowLongW.argtypes = (ctypes.c_void_p, ctypes.c_int)
        user32.GetWindowLongW.restype = ctypes.c_long
        user32.SetWindowLongW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_long)
        user32.SetWindowLongW.restype = ctypes.c_long
        user32.SetLayeredWindowAttributes.argtypes = (
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_ubyte,
            ctypes.c_uint32,
        )
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT)
        user32.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)
        return True
    except Exception:
        log.debug("Click-through notifications are unavailable", exc_info=True)
        return False


def play_toggle_sound(enabled):
    if winsound is None:
        return
    notes = ((784, 90), (1175, 130)) if enabled else ((988, 90), (587, 150))

    def play():
        try:
            for frequency, duration in notes:
                winsound.Beep(frequency, duration)
            return
        except Exception:
            log.debug("The tone could not be played", exc_info=True)
        try:
            sound = winsound.MB_ICONASTERISK if enabled else winsound.MB_ICONHAND
            winsound.MessageBeep(sound)
        except Exception:
            log.debug("The backup notification sound failed", exc_info=True)

    threading.Thread(target=play, name="ToggleSound", daemon=True).start()


class AutoClickerApp:
    def __init__(self, root, config, engine):
        self.root = root
        self.config = dict(config)
        self.engine = engine

        self._queue = queue.Queue()
        self._closed = False
        self._after_id = None
        self._hotkey = ""
        self._hotkey_capturing = False
        self._hotkey_job = None
        self._hotkey_poller = HotkeyPoller(self._on_hotkey_press)
        self._capture_cancel = None
        self._last_toggle = 0.0
        self._toggle_lock = threading.Lock()
        self._last_valid_ms = DEFAULT_CONFIG["interval_milliseconds"]
        self._toast = None
        self._toast_job = None
        self._running_state = False

        self.icon = None
        self._tray_icons = {}

        self._configure_window()
        self._build_styles()
        self._build_ui()
        self._finalize_window()
        self._apply_config(self.config)
        self._build_tray()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._apply_running_state(False)
        self._after_id = self.root.after(80, self._pump_queue)
        self._hotkey_job = self.root.after(HOTKEY_POLL_MS, self._poll_hotkey)

    def _configure_window(self):
        self.root.title(APP_NAME)
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.option_add("*tearOff", False)
        self.root.report_callback_exception = report_callback_exception
        self._window_icon = ImageTk.PhotoImage(build_icon_image(64))
        self.root.iconphoto(True, self._window_icon)

    def _build_styles(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("Root.TFrame", background=BG)
        style.configure(
            "Section.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9, "bold")
        )
        style.configure("Field.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Body.TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 10))

        style.configure(
            "Field.TEntry",
            fieldbackground=SURFACE,
            foreground=TEXT,
            insertcolor=TEXT,
            bordercolor=SURFACE,
            lightcolor=SURFACE,
            darkcolor=SURFACE,
            selectbackground=BORDER,
            selectforeground=BG,
            padding=(8, 7),
            relief="flat",
        )
        style.map(
            "Field.TEntry",
            bordercolor=[("focus", TEXT)],
            lightcolor=[("focus", TEXT)],
            darkcolor=[("focus", TEXT)],
        )

        style.configure(
            "Switch.TCheckbutton",
            background=BG,
            foreground=TEXT,
            focuscolor=BG,
            focusthickness=0,
            indicatorbackground=SURFACE,
            indicatorforeground=TEXT,
            upperbordercolor=BORDER,
            lowerbordercolor=BORDER,
            indicatorsize=14,
            indicatormargin=(0, 0, 9, 0),
            padding=(0, 2),
            font=("Segoe UI", 10),
        )
        style.map(
            "Switch.TCheckbutton",
            background=[("active", BG)],
            foreground=[("disabled", MUTED)],
            indicatorbackground=[("selected", TEXT)],
            indicatorforeground=[("selected", BG)],
            upperbordercolor=[("selected", TEXT)],
            lowerbordercolor=[("selected", TEXT)],
        )

        style.configure(
            "Flat.TButton",
            background=TEXT,
            foreground=BG,
            bordercolor=TEXT,
            lightcolor=TEXT,
            darkcolor=TEXT,
            focuscolor=TEXT,
            focusthickness=0,
            relief="flat",
            borderwidth=0,
            padding=(12, 10),
            font=("Segoe UI", 10, "bold"),
        )
        style.map(
            "Flat.TButton",
            background=[("active", TEXT), ("pressed", TEXT)],
            foreground=[("active", BG), ("pressed", BG)],
            bordercolor=[("active", TEXT), ("pressed", TEXT)],
            lightcolor=[("active", TEXT), ("pressed", TEXT)],
            darkcolor=[("active", TEXT), ("pressed", TEXT)],
        )

    def _build_ui(self):
        container = ttk.Frame(self.root, style="Root.TFrame", padding=(20, 18))
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)

        ttk.Label(container, text="CLICK INTERVAL", style="Section.TLabel").grid(
            row=0, column=0, sticky="w"
        )

        fields = ttk.Frame(container, style="Root.TFrame")
        fields.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        fields.columnconfigure(0, weight=1, uniform="interval")
        fields.columnconfigure(1, weight=1, uniform="interval")

        seconds_validator = (
            self.root.register(self._make_interval_validator(SECONDS_DIGITS)),
            "%P",
        )
        milliseconds_validator = (
            self.root.register(self._make_interval_validator(MILLISECONDS_DIGITS)),
            "%P",
        )

        seconds_box = ttk.Frame(fields, style="Root.TFrame")
        seconds_box.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Label(seconds_box, text="Seconds", style="Field.TLabel").pack(anchor="w")
        self.seconds_var = tk.StringVar()
        self.seconds_entry = ttk.Entry(
            seconds_box,
            textvariable=self.seconds_var,
            style="Field.TEntry",
            width=6,
            justify="center",
            validate="key",
            validatecommand=seconds_validator,
        )
        self.seconds_entry.pack(fill="x", pady=(4, 0))

        ms_box = ttk.Frame(fields, style="Root.TFrame")
        ms_box.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        ttk.Label(ms_box, text="Milliseconds", style="Field.TLabel").pack(anchor="w")
        self.ms_var = tk.StringVar()
        self.ms_entry = ttk.Entry(
            ms_box,
            textvariable=self.ms_var,
            style="Field.TEntry",
            width=6,
            justify="center",
            validate="key",
            validatecommand=milliseconds_validator,
        )
        self.ms_entry.pack(fill="x", pady=(4, 0))

        hotkey_row = ttk.Frame(container, style="Root.TFrame")
        hotkey_row.grid(row=2, column=0, sticky="ew", pady=(16, 0))
        hotkey_row.columnconfigure(1, weight=1)
        ttk.Label(hotkey_row, text="Hotkey (Start/Stop):", style="Body.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.hotkey_var = tk.StringVar()
        self.hotkey_entry = ttk.Entry(
            hotkey_row,
            textvariable=self.hotkey_var,
            style="Field.TEntry",
            width=10,
            justify="center",
        )
        self.hotkey_entry.grid(row=0, column=1, sticky="ew", padx=(12, 0))

        self.status_var = tk.StringVar()
        self.status_label = ttk.Label(
            container,
            textvariable=self.status_var,
            style="Field.TLabel",
            justify="left",
            wraplength=280,
        )
        self.status_label.grid(row=3, column=0, sticky="w", pady=(6, 0))

        self.randomize_var = tk.BooleanVar()
        self.randomize_check = ttk.Checkbutton(
            container,
            text="Enable Anti-Detection Randomization",
            variable=self.randomize_var,
            style="Switch.TCheckbutton",
            cursor="hand2",
            command=self._on_randomize_toggled,
        )
        self.randomize_check.grid(row=4, column=0, sticky="w", pady=(14, 0))

        self.toggle_button = ttk.Button(
            container,
            text="Start",
            style="Flat.TButton",
            cursor="hand2",
            command=self._toggle_clicking,
        )
        self.toggle_button.grid(row=5, column=0, sticky="ew", pady=(16, 0))

        self.tray_button = ttk.Button(
            container,
            text="Minimize to Tray",
            style="Flat.TButton",
            cursor="hand2",
            command=self._minimize_to_tray,
        )
        self.tray_button.grid(row=6, column=0, sticky="ew", pady=(8, 0))

        for entry in (self.seconds_entry, self.ms_entry):
            entry.bind("<Return>", self._commit_interval)
            entry.bind("<FocusOut>", self._commit_interval)
            entry.bind("<FocusIn>", lambda event: event.widget.select_range(0, "end"))

        self.hotkey_entry.bind("<FocusIn>", self._begin_hotkey_capture)
        self.hotkey_entry.bind("<Button-1>", self._focus_hotkey_entry)
        self.hotkey_entry.bind("<FocusOut>", self._cancel_hotkey_capture)

    def _finalize_window(self):
        self.root.update_idletasks()
        width = max(350, self.root.winfo_reqwidth())
        height = self.root.winfo_reqheight()
        x = max(0, (self.root.winfo_screenwidth() - width) // 2)
        y = max(0, (self.root.winfo_screenheight() - height) // 3)
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        apply_dark_title_bar(self.root)

    def _apply_config(self, config):
        total = (
            _as_int(config.get("interval_seconds"), 0) * 1000
            + _as_int(config.get("interval_milliseconds"), DEFAULT_CONFIG["interval_milliseconds"])
        )
        total = max(MIN_INTERVAL_MS, min(MAX_INTERVAL_MS, total))
        self._last_valid_ms = total
        seconds, millis = divmod(total, 1000)

        self.seconds_var.set(str(seconds))
        self.ms_var.set(str(millis))
        randomize = config.get("randomize") is True
        self.randomize_var.set(randomize)
        self.engine.configure(total / 1000.0, randomize)
        self._bind_saved_hotkey(config.get("hotkey", DEFAULT_HOTKEY))

    def _set_status(self, text, error=False):
        try:
            self.status_var.set(text)
            self.status_label.configure(foreground=TEXT if error else MUTED)
        except tk.TclError:
            pass

    def _current_interval_ms(self):
        try:
            seconds = int(self.seconds_var.get())
            millis = int(self.ms_var.get())
        except ValueError:
            return None
        return seconds * 1000 + millis

    def _effective_interval_ms(self):
        total = self._current_interval_ms()
        if total is None:
            total = self._last_valid_ms
        return max(MIN_INTERVAL_MS, min(MAX_INTERVAL_MS, total))

    def _persist(self):
        total = self._effective_interval_ms()
        seconds, millis = divmod(total, 1000)
        self.config = {
            "interval_seconds": seconds,
            "interval_milliseconds": millis,
            "hotkey": self._hotkey or DEFAULT_HOTKEY,
            "randomize": bool(self.randomize_var.get()),
        }
        if not save_config(self.config):
            self._set_status("Your settings could not be saved to disk.", error=True)

    @staticmethod
    def _make_interval_validator(max_digits):
        def validate(proposed):
            if proposed == "":
                return True
            return proposed.isascii() and proposed.isdigit() and len(proposed) <= max_digits

        return validate

    def _commit_interval(self, _event=None):
        total = self._effective_interval_ms()
        self._last_valid_ms = total
        seconds, millis = divmod(total, 1000)
        self.seconds_var.set(str(seconds))
        self.ms_var.set(str(millis))
        self.engine.configure(total / 1000.0, bool(self.randomize_var.get()))
        self._persist()
        return "break"

    def _on_randomize_toggled(self):
        total = self._effective_interval_ms()
        self._last_valid_ms = total
        self.engine.configure(total / 1000.0, bool(self.randomize_var.get()))
        self._persist()

    def _bind_hotkey(self, combo):
        if not self._hotkey_poller.set_combo(combo):
            return False
        self._hotkey = combo
        return True

    def _unbind_hotkey(self):
        job = self._hotkey_job
        self._hotkey_job = None
        if job is not None:
            try:
                self.root.after_cancel(job)
            except Exception:
                pass
        self._hotkey_poller.clear()

    def _hotkey_entry_has_focus(self):
        try:
            return self.root.focus_get() is self.hotkey_entry
        except (KeyError, tk.TclError):
            return False

    def _poll_hotkey(self):
        self._hotkey_job = None
        if self._closed:
            return
        if self._hotkey_capturing and not self._hotkey_entry_has_focus():
            self._cancel_hotkey_capture()
        try:
            self._hotkey_poller.poll()
        except Exception:
            log.exception("The hotkey check failed")
        if self._closed:
            return
        try:
            self._hotkey_job = self.root.after(HOTKEY_POLL_MS, self._poll_hotkey)
        except tk.TclError:
            self._hotkey_job = None

    def _bind_saved_hotkey(self, combo):
        try:
            normalized = normalize_hotkey(combo)
        except Exception:
            normalized = DEFAULT_HOTKEY
        bound = self._bind_hotkey(normalized)
        if not bound and normalized != DEFAULT_HOTKEY:
            bound = self._bind_hotkey(DEFAULT_HOTKEY)
        if bound:
            self.hotkey_var.set(format_hotkey(self._hotkey))
            self._set_status(f"Hotkey {format_hotkey(self._hotkey)} is ready.")
        else:
            self.hotkey_var.set(format_hotkey(DEFAULT_HOTKEY))
            self._set_status(
                "The hotkey could not be set up. Click the hotkey box and choose "
                "another combination.",
                error=True,
            )

    def _focus_hotkey_entry(self, _event=None):
        try:
            self.hotkey_entry.focus_set()
        except tk.TclError:
            return
        self._begin_hotkey_capture()

    def _begin_hotkey_capture(self, _event=None):
        if self._closed or self._hotkey_capturing:
            return "break"
        self._hotkey_capturing = True
        self._hotkey_poller.set_suspended(True)
        cancel = threading.Event()
        self._capture_cancel = cancel
        try:
            self.hotkey_entry.configure(state="readonly")
        except tk.TclError:
            self._hotkey_capturing = False
            self._capture_cancel = None
            self._hotkey_poller.set_suspended(False)
            return "break"
        self.hotkey_var.set("Press keys...")
        self._set_status("Press the combination you want, or Esc to cancel.")
        threading.Thread(
            target=self._capture_worker, args=(cancel,), name="HotkeyCapture", daemon=True
        ).start()
        return "break"

    def _capture_worker(self, cancel):
        try:
            combo = capture_hotkey(cancel)
        except Exception:
            log.exception("The hotkey capture failed")
            combo = None
        self._queue.put(("hotkey", combo))

    def _cancel_hotkey_capture(self, _event=None):
        if not self._hotkey_capturing or self._closed:
            return
        self._hotkey_capturing = False
        self._hotkey_poller.set_suspended(False)
        cancel = self._capture_cancel
        self._capture_cancel = None
        if cancel is not None:
            cancel.set()
        try:
            self.hotkey_entry.configure(state="normal")
        except tk.TclError:
            return
        self.hotkey_var.set(format_hotkey(self._hotkey or DEFAULT_HOTKEY))
        self._set_status(
            f"Hotkey {format_hotkey(self._hotkey)} is ready." if self._hotkey else ""
        )

    def _finish_hotkey_capture(self, combo):
        if not self._hotkey_capturing or self._closed:
            return
        self._hotkey_capturing = False
        self._hotkey_poller.set_suspended(False)
        self._capture_cancel = None
        try:
            self.hotkey_entry.configure(state="normal")
        except tk.TclError:
            return

        if not combo:
            self.hotkey_var.set(format_hotkey(self._hotkey or DEFAULT_HOTKEY))
            self._set_status("Hotkey change cancelled.")
            return

        try:
            normalized = normalize_hotkey(combo)
        except Exception:
            normalized = None

        if normalized is None or not self._bind_hotkey(normalized):
            self.hotkey_var.set(format_hotkey(self._hotkey or DEFAULT_HOTKEY))
            self._set_status(
                "That key cannot be used. Try a combination such as Ctrl+Alt+K.",
                error=True,
            )
            messagebox.showerror(
                APP_NAME,
                "That key combination cannot be used as a hotkey.\n\n"
                "Use at least one key that is not a modifier (for example Ctrl+Alt+K).",
                parent=self.root,
            )
            return

        self.hotkey_var.set(format_hotkey(self._hotkey))
        self._set_status(f"Hotkey {format_hotkey(self._hotkey)} is ready.")
        self._persist()
        self.root.focus_set()

    def _request_toggle(self):
        if self._closed:
            return None
        with self._toggle_lock:
            now = time.monotonic()
            if now - self._last_toggle < 0.3:
                return None
            self._last_toggle = now
            return self.engine.toggle()

    def _on_hotkey_press(self):
        running = self._request_toggle()
        if running is None:
            return
        log.info(
            "Hotkey %r pressed - clicking %s",
            self._hotkey,
            "started" if running else "stopped",
        )
        try:
            self._queue.put(("state", running))
        except Exception:
            log.debug("Could not queue the hotkey state", exc_info=True)

    def _toggle_clicking(self):
        self._commit_interval()
        running = self._request_toggle()
        if running is None:
            return
        self._apply_running_state(running, notify=True)

    def _build_tray(self):
        self._tray_icons = {
            False: build_icon_image(64, active=False),
            True: build_icon_image(64, active=True),
        }
        menu = Menu(
            MenuItem("Open Interface", self._tray_open, default=True),
            MenuItem("Exit Completely", self._tray_exit),
        )
        self.icon = pystray.Icon(APP_ID, self._tray_icons[False], APP_NAME, menu)
        threading.Thread(target=self.icon.run, name="TrayIcon", daemon=True).start()

    def _tray_open(self, _icon, _item):
        self._queue.put("open")

    def _tray_exit(self, icon, _item):
        self.engine.stop()
        self._queue.put("exit")
        icon.stop()

    def _apply_running_state(self, running, notify=False):
        running = bool(running)
        changed = running != self._running_state
        self._running_state = running
        try:
            self.root.title(f"{APP_NAME} - Clicking" if running else APP_NAME)
        except tk.TclError:
            return
        toggle = getattr(self, "toggle_button", None)
        if toggle is not None:
            try:
                toggle.configure(text="Stop" if running else "Start")
            except tk.TclError:
                pass
        icon = self.icon
        if icon is not None:
            try:
                icon.icon = self._tray_icons[running]
                icon.title = f"{APP_NAME} - Clicking" if running else APP_NAME
            except Exception:
                log.debug("Could not update the tray icon", exc_info=True)
        if notify and changed:
            self._show_notification(running)

    def _show_notification(self, enabled):
        self._destroy_notification()
        play_toggle_sound(enabled)
        title = "AUTOCLICKER ENABLED" if enabled else "AUTOCLICKER DISABLED"
        hotkey = format_hotkey(self._hotkey or DEFAULT_HOTKEY)
        hint = f"Press {hotkey} to stop" if enabled else f"Press {hotkey} to start"
        log.info("Notification: %s", title.title())
        try:
            toast = tk.Toplevel(self.root)
            toast.overrideredirect(True)
            toast.configure(bg=BORDER)
            toast.attributes("-topmost", True)
            panel = tk.Frame(toast, bg=SURFACE, padx=32, pady=18)
            panel.pack(fill="both", expand=True, padx=1, pady=1)
            tk.Frame(panel, bg=TEXT, height=4).pack(fill="x")
            tk.Label(
                panel,
                text=title,
                bg=SURFACE,
                fg=TEXT,
                font=("Segoe UI Semibold", 15),
            ).pack(pady=(14, 0))
            tk.Label(
                panel,
                text=hint,
                bg=SURFACE,
                fg=MUTED,
                font=("Segoe UI", 10),
            ).pack(pady=(6, 0))
            toast.update_idletasks()
            width = max(340, toast.winfo_reqwidth())
            height = toast.winfo_reqheight()
            margin = 28
            x = max(0, self.root.winfo_screenwidth() - width - margin)
            y = max(0, self.root.winfo_screenheight() - height - 96)
            pointer_x, pointer_y = self.root.winfo_pointerxy()
            if x <= pointer_x <= x + width and y <= pointer_y <= y + height:
                x = margin
            toast.geometry(f"{width}x{height}+{x}+{y}")
            toast.deiconify()
            toast.lift()
            make_click_through(toast)
            self._toast = toast
            self._toast_job = self.root.after(2400, self._destroy_notification)
        except tk.TclError:
            log.debug("Could not show the notification", exc_info=True)
            self._destroy_notification()

    def _destroy_notification(self):
        job = self._toast_job
        self._toast_job = None
        if job is not None:
            try:
                self.root.after_cancel(job)
            except Exception:
                pass
        toast = self._toast
        self._toast = None
        if toast is not None:
            try:
                toast.destroy()
            except Exception:
                pass

    def _restore_window(self):
        self.root.deiconify()
        try:
            self.root.state("normal")
        except tk.TclError:
            pass
        self.root.lift()
        try:
            self.root.attributes("-topmost", True)
            self.root.after(150, self._drop_topmost)
        except tk.TclError:
            pass
        self.root.after(20, self.root.focus_force)

    def _drop_topmost(self):
        try:
            self.root.attributes("-topmost", False)
        except tk.TclError:
            pass

    def _minimize_to_tray(self):
        self._commit_interval()
        self.root.withdraw()

    def _on_close(self):
        self._shutdown()

    def report_engine_problem(self, message):
        self._queue.put(("error", message))

    def _handle_engine_problem(self, message):
        self._apply_running_state(False)
        first_line = message.splitlines()[0] if message else "Clicking stopped."
        self._set_status(first_line, error=True)
        try:
            messagebox.showerror(APP_NAME, message, parent=self.root)
        except tk.TclError:
            pass

    def _pump_queue(self):
        if self._closed:
            return
        try:
            while True:
                item = self._queue.get_nowait()
                if isinstance(item, tuple):
                    if item[0] == "state":
                        self._apply_running_state(bool(item[1]), notify=True)
                    elif item[0] == "hotkey":
                        self._finish_hotkey_capture(item[1])
                    elif item[0] == "error":
                        self._handle_engine_problem(str(item[1]))
                    continue
                if item == "open":
                    self._restore_window()
                elif item == "exit":
                    self._shutdown()
                    return
        except queue.Empty:
            pass
        if not self._closed:
            self._after_id = self.root.after(80, self._pump_queue)

    def _shutdown(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._commit_interval()
        except tk.TclError:
            pass
        self.engine.stop()
        self._destroy_notification()
        self._unbind_hotkey()
        cancel = self._capture_cancel
        self._capture_cancel = None
        if cancel is not None:
            cancel.set()
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        try:
            self.root.quit()
        except tk.TclError:
            pass

    def dispose(self):
        self._shutdown()
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass


def end_process_if_needed():
    lingering = [
        thread
        for thread in threading.enumerate()
        if thread is not threading.current_thread()
        and thread.is_alive()
        and not thread.daemon
    ]
    if not lingering:
        return
    log.info("Forcing exit; lingering threads: %s", [t.name for t in lingering])
    logging.shutdown()
    os._exit(0)


def main():
    setup_logging()
    enable_dpi_awareness()
    remove_legacy_startup()
    config = load_config()
    engine = ClickEngine()
    root = tk.Tk()
    app = AutoClickerApp(root, config, engine)
    engine.report = app.report_engine_problem
    try:
        root.mainloop()
    finally:
        app.dispose()
    end_process_if_needed()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as error:
        log.exception("The application could not start")
        try:
            messagebox.showerror(
                APP_NAME, f"Improved AutoClicker could not start.\n\n{error}"
            )
        except Exception:
            pass
        raise
