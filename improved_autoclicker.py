import ctypes
import json
import os
import queue
import random
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import keyboard
import pystray
from PIL import Image, ImageDraw, ImageTk
from pystray import Menu, MenuItem

try:
    import winreg
except ImportError:
    winreg = None

APP_NAME = "Improved AutoClicker"
APP_ID = "ImprovedAutoClicker"
CONFIG_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), APP_ID)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = APP_ID
DEFAULT_HOTKEY = "f6"

DEFAULT_CONFIG = {
    "interval_seconds": 0,
    "interval_milliseconds": 100,
    "hotkey": DEFAULT_HOTKEY,
    "randomize": False,
    "startup": False,
}

MIN_INTERVAL_MS = 1
MAX_INTERVAL_MS = 24 * 60 * 60 * 1000
JITTER_LOW = 0.8
JITTER_HIGH = 1.2

BG = "#000000"
SURFACE = "#1e1e1e"
BORDER = "#d3d3d3"
TEXT = "#ffffff"
MUTED = "#d3d3d3"

RESAMPLE = getattr(Image, "Resampling", Image).LANCZOS


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
        0, min(9999, _as_int(raw.get("interval_seconds"), config["interval_seconds"]))
    )
    config["interval_milliseconds"] = max(
        0,
        min(999, _as_int(raw.get("interval_milliseconds"), config["interval_milliseconds"])),
    )
    hotkey = raw.get("hotkey")
    if isinstance(hotkey, str) and hotkey.strip():
        config["hotkey"] = hotkey.strip().lower()
    config["randomize"] = raw.get("randomize") is True
    config["startup"] = raw.get("startup") is True
    return config


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            return normalize_config(json.load(handle))
    except FileNotFoundError:
        config = dict(DEFAULT_CONFIG)
        save_config(config)
        return config
    except (OSError, ValueError):
        return dict(DEFAULT_CONFIG)


def save_config(config):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        temporary = CONFIG_PATH + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2)
        os.replace(temporary, CONFIG_PATH)
        return True
    except OSError:
        return False


def startup_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    executable = sys.executable
    pythonw = os.path.join(os.path.dirname(executable), "pythonw.exe")
    launcher = pythonw if os.path.isfile(pythonw) else executable
    script = os.path.abspath(__file__)
    return f'"{launcher}" "{script}" --minimized'


def read_startup_command():
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, RUN_VALUE)
            return str(value)
    except OSError:
        return None


def set_startup(enabled):
    if winreg is None:
        return False
    try:
        if enabled:
            with winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
            ) as key:
                winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
                ) as key:
                    winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass
        return True
    except OSError:
        return False


def reconcile_startup(config):
    if config.get("startup") is True and read_startup_command() != startup_command():
        if not set_startup(True):
            config["startup"] = False
            save_config(config)
    return config


def normalize_hotkey(text):
    combo = str(text).strip().lower().replace(" ", "")
    if not combo:
        raise ValueError("empty hotkey")
    if "+" not in combo and len(combo) < 2:
        raise ValueError("single-character hotkeys are not allowed")
    keyboard.parse_hotkey(combo)
    return combo


def format_hotkey(combo):
    if not combo:
        return ""
    return "+".join(
        part.upper() if len(part) == 1 else part.capitalize() for part in combo.split("+")
    )


class ClickEngine:
    def __init__(self):
        self._lock = threading.Lock()
        self._interval = DEFAULT_CONFIG["interval_milliseconds"] / 1000.0
        self._randomize = False
        self._stop = threading.Event()
        self._thread = None

    @property
    def running(self):
        thread = self._thread
        return thread is not None and thread.is_alive()

    def configure(self, interval_seconds, randomize):
        with self._lock:
            self._interval = max(MIN_INTERVAL_MS / 1000.0, float(interval_seconds))
            self._randomize = bool(randomize)

    def start(self):
        if self.running:
            return True
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ClickEngine", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._thread = None

    def toggle(self):
        if self.running:
            self.stop()
            return False
        self.start()
        return True

    def _snapshot(self):
        with self._lock:
            return self._interval, self._randomize

    def _wait_until(self, deadline):
        stop = self._stop
        while True:
            if stop.is_set():
                return False
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return True
            if remaining > 0.002:
                stop.wait(remaining - 0.001)
            else:
                time.sleep(0)

    def _run(self):
        try:
            import pyautogui
        except Exception:
            self._stop.set()
            return

        pyautogui.PAUSE = 0
        pyautogui.FAILSAFE = False

        timer = None
        try:
            timer = ctypes.windll.winmm
            timer.timeBeginPeriod(1)
        except Exception:
            timer = None

        try:
            next_at = time.perf_counter()
            while not self._stop.is_set():
                interval, jitter = self._snapshot()
                if jitter:
                    started = time.perf_counter()
                    self._click(pyautogui)
                    delay = interval * random.uniform(JITTER_LOW, JITTER_HIGH)
                    if not self._wait_until(started + delay):
                        break
                else:
                    if not self._wait_until(next_at):
                        break
                    self._click(pyautogui)
                    next_at += interval
                    now = time.perf_counter()
                    if next_at < now:
                        next_at = now + interval
        finally:
            if timer is not None:
                try:
                    timer.timeEndPeriod(1)
                except Exception:
                    pass

    @staticmethod
    def _click(pyautogui):
        try:
            pyautogui.click()
        except Exception:
            pass


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
            if dwm.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(enabled), ctypes.sizeof(enabled)
            ) == 0:
                break
        caption = ctypes.c_int(0x00000000)
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption), ctypes.sizeof(caption))
        text_color = ctypes.c_int(0x00FFFFFF)
        dwm.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(text_color), ctypes.sizeof(text_color))
    except Exception:
        pass


class AutoClickerApp:
    def __init__(self, root, config, engine):
        self.root = root
        self.config = dict(config)
        self.engine = engine

        self._queue = queue.Queue()
        self._closed = False
        self._after_id = None
        self._hotkey = ""
        self._hotkey_handle = None
        self._committing_hotkey = False
        self._hotkey_capturing = False
        self._last_toggle = 0.0
        self._last_valid_ms = DEFAULT_CONFIG["interval_milliseconds"]
        self._save_job = None
        self._toast = None
        self._toast_job = None
        self._running_state = False

        self.icon = None
        self._tray_thread = None
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

    def _configure_window(self):
        self.root.title(APP_NAME)
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.option_add("*tearOff", False)
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

        validator = (self.root.register(self._validate_interval_input), "%P")

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
            validatecommand=validator,
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
            validatecommand=validator,
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
            width=8,
            justify="center",
        )
        self.hotkey_entry.grid(row=0, column=1, sticky="ew", padx=(12, 0))

        self.randomize_var = tk.BooleanVar()
        self.randomize_check = ttk.Checkbutton(
            container,
            text="Enable Anti-Detection Randomization",
            variable=self.randomize_var,
            style="Switch.TCheckbutton",
            cursor="hand2",
            command=self._on_randomize_toggled,
        )
        self.randomize_check.grid(row=3, column=0, sticky="w", pady=(16, 0))

        self.startup_var = tk.BooleanVar()
        self.startup_check = ttk.Checkbutton(
            container,
            text="Launch on Windows Startup",
            variable=self.startup_var,
            style="Switch.TCheckbutton",
            cursor="hand2",
            command=self._on_startup_toggled,
        )
        self.startup_check.grid(row=4, column=0, sticky="w", pady=(10, 0))

        self.toggle_button = ttk.Button(
            container,
            text="Start",
            style="Flat.TButton",
            cursor="hand2",
            command=self._toggle_clicking,
        )
        self.toggle_button.grid(row=5, column=0, sticky="ew", pady=(18, 0))

        self.tray_button = ttk.Button(
            container,
            text="Minimize to Tray",
            style="Flat.TButton",
            cursor="hand2",
            command=self._minimize_to_tray,
        )
        self.tray_button.grid(row=6, column=0, sticky="ew", pady=(8, 0))

        self.seconds_var.trace_add("write", self._on_interval_changed)
        self.ms_var.trace_add("write", self._on_interval_changed)

        for entry in (self.seconds_entry, self.ms_entry):
            entry.bind("<Return>", self._commit_interval)
            entry.bind("<FocusOut>", self._commit_interval)
            entry.bind("<FocusIn>", lambda event: event.widget.select_range(0, "end"))

        self.hotkey_entry.bind("<FocusIn>", self._begin_hotkey_capture)
        self.hotkey_entry.bind("<Button-1>", self._begin_hotkey_capture)
        self.hotkey_entry.bind("<FocusOut>", self._cancel_hotkey_capture)

    def _finalize_window(self):
        self.root.update_idletasks()
        width = max(340, self.root.winfo_reqwidth())
        height = self.root.winfo_reqheight()
        x = max(0, (self.root.winfo_screenwidth() - width) // 2)
        y = max(0, (self.root.winfo_screenheight() - height) // 3)
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        apply_dark_title_bar(self.root)

    def _apply_config(self, config):
        seconds = _as_int(config.get("interval_seconds"), 0)
        millis = _as_int(config.get("interval_milliseconds"), 100)
        total = max(MIN_INTERVAL_MS, min(MAX_INTERVAL_MS, seconds * 1000 + millis))
        self._last_valid_ms = total
        whole_seconds, leftover_ms = divmod(total, 1000)

        self.seconds_var.set(str(whole_seconds))
        self.ms_var.set(str(leftover_ms))
        self.randomize_var.set(config.get("randomize") is True)
        self.startup_var.set(config.get("startup") is True)
        self.engine.configure(total / 1000.0, config.get("randomize") is True)

        self._bind_saved_hotkey(config.get("hotkey", DEFAULT_HOTKEY))

    def _persist(self):
        total = self._current_interval_ms()
        if total is None:
            total = self._last_valid_ms
        total = max(MIN_INTERVAL_MS, min(MAX_INTERVAL_MS, total))
        seconds, millis = divmod(total, 1000)
        self.config = {
            "interval_seconds": seconds,
            "interval_milliseconds": millis,
            "hotkey": self._hotkey or DEFAULT_HOTKEY,
            "randomize": bool(self.randomize_var.get()),
            "startup": bool(self.startup_var.get()),
        }
        save_config(self.config)

    def _schedule_save(self):
        if self._closed:
            return
        self._cancel_save()
        self._save_job = self.root.after(450, self._save_after_change)

    def _save_after_change(self):
        self._save_job = None
        if not self._closed:
            self._persist()

    def _cancel_save(self):
        if self._save_job is not None:
            try:
                self.root.after_cancel(self._save_job)
            except Exception:
                pass
            self._save_job = None

    @staticmethod
    def _validate_interval_input(proposed):
        if proposed == "":
            return True
        return proposed.isdigit() and len(proposed) <= 4

    def _current_interval_ms(self):
        try:
            seconds = int(self.seconds_var.get())
            millis = int(self.ms_var.get())
        except ValueError:
            return None
        return seconds * 1000 + millis

    def _on_interval_changed(self, *_args):
        total = self._current_interval_ms()
        if total is None:
            return
        total = max(MIN_INTERVAL_MS, min(MAX_INTERVAL_MS, total))
        self._last_valid_ms = total
        self.engine.configure(total / 1000.0, bool(self.randomize_var.get()))
        self._schedule_save()

    def _commit_interval(self, _event=None):
        total = self._current_interval_ms()
        if total is None:
            total = self._last_valid_ms
        total = max(MIN_INTERVAL_MS, min(MAX_INTERVAL_MS, total))
        self._last_valid_ms = total
        seconds, millis = divmod(total, 1000)
        self.seconds_var.set(str(seconds))
        self.ms_var.set(str(millis))
        self.engine.configure(total / 1000.0, bool(self.randomize_var.get()))
        self._cancel_save()
        self._persist()
        return "break"

    def _on_randomize_toggled(self):
        self.engine.configure(self._last_valid_ms / 1000.0, bool(self.randomize_var.get()))
        self._persist()

    def _on_startup_toggled(self):
        enabled = bool(self.startup_var.get())
        if not set_startup(enabled):
            self.startup_var.set(not enabled)
            messagebox.showerror(
                APP_NAME,
                "The Windows startup entry could not be updated.",
                parent=self.root,
            )
        self._persist()

    def _bind_hotkey(self, combo):
        previous = self._hotkey
        self._unbind_hotkey()
        try:
            self._hotkey_handle = keyboard.add_hotkey(
                combo, self._on_hotkey_press, trigger_on_release=True
            )
        except Exception:
            self._hotkey_handle = None
            if previous:
                try:
                    self._hotkey_handle = keyboard.add_hotkey(
                        previous, self._on_hotkey_press, trigger_on_release=True
                    )
                except Exception:
                    self._hotkey_handle = None
            return False
        self._hotkey = combo
        return True

    def _unbind_hotkey(self):
        handle = self._hotkey_handle
        if handle is None:
            return
        try:
            keyboard.remove_hotkey(handle)
        except Exception:
            try:
                keyboard.unhook_all_hotkeys()
            except Exception:
                pass
        self._hotkey_handle = None

    def _bind_saved_hotkey(self, combo):
        try:
            normalized = normalize_hotkey(combo)
        except ValueError:
            normalized = DEFAULT_HOTKEY
        if not self._bind_hotkey(normalized):
            self._bind_hotkey(DEFAULT_HOTKEY)
        self.hotkey_var.set(format_hotkey(self._hotkey or DEFAULT_HOTKEY))

    def _begin_hotkey_capture(self, _event=None):
        if self._closed or self._hotkey_capturing:
            return "break"
        self._hotkey_capturing = True
        self._unbind_hotkey()
        self.hotkey_var.set("")
        self.hotkey_entry.configure(state="readonly")
        threading.Thread(
            target=self._hotkey_capture_worker, name="HotkeyCapture", daemon=True
        ).start()
        return "break"

    def _hotkey_capture_worker(self):
        try:
            combo = keyboard.read_hotkey(suppress=False)
        except Exception:
            combo = None
        self._queue.put(("hotkey", combo))

    def _cancel_hotkey_capture(self, _event=None):
        if not self._hotkey_capturing or self._closed:
            return
        # Focus moved away before capture completed; restore display and rebind.
        # The capture thread may still be waiting for a keypress; it will post a
        # result that _finish_hotkey_capture will ignore once capture ends.
        self._hotkey_capturing = False
        try:
            self.hotkey_entry.configure(state="normal")
        except tk.TclError:
            return
        self.hotkey_var.set(format_hotkey(self._hotkey or DEFAULT_HOTKEY))
        if self._hotkey and self._hotkey_handle is None:
            self._bind_hotkey(self._hotkey)

    def _finish_hotkey_capture(self, combo):
        if not self._hotkey_capturing or self._closed:
            return
        self._hotkey_capturing = False
        try:
            self.hotkey_entry.configure(state="normal")
        except tk.TclError:
            return

        if not combo or str(combo).strip().lower() == "esc":
            self.hotkey_var.set(format_hotkey(self._hotkey or DEFAULT_HOTKEY))
            if self._hotkey and self._hotkey_handle is None:
                self._bind_hotkey(self._hotkey)
            return

        try:
            normalized = normalize_hotkey(combo)
        except ValueError:
            normalized = None

        if normalized is None or not self._bind_hotkey(normalized):
            self.hotkey_var.set(format_hotkey(self._hotkey or DEFAULT_HOTKEY))
            if self._hotkey and self._hotkey_handle is None:
                self._bind_hotkey(self._hotkey)
            messagebox.showerror(
                APP_NAME,
                "That hotkey could not be registered. Try a different key.",
                parent=self.root,
            )
            return

        self.hotkey_var.set(format_hotkey(self._hotkey))
        self._persist()
        self.root.focus_set()

    def _on_hotkey_press(self):
        if self._closed:
            return
        now = time.monotonic()
        if now - self._last_toggle < 0.3:
            return
        self._last_toggle = now
        running = self.engine.toggle()
        self._queue.put(("state", running))

    def _toggle_clicking(self):
        if self._closed:
            return
        now = time.monotonic()
        if now - self._last_toggle < 0.3:
            return
        self._last_toggle = now
        running = self.engine.toggle()
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
        self._tray_thread = threading.Thread(
            target=self.icon.run, name="TrayIcon", daemon=True
        )
        self._tray_thread.start()

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
                pass
        if notify and changed:
            self._show_notification(running)

    def _show_notification(self, enabled):
        self._destroy_notification()
        try:
            toast = tk.Toplevel(self.root)
            toast.overrideredirect(True)
            toast.configure(bg=BG)
            toast.attributes("-topmost", True)
            panel = tk.Frame(toast, bg=SURFACE, padx=20, pady=12)
            panel.pack(fill="both", expand=True)
            message = "Autoclicker Enabled" if enabled else "Autoclicker Disabled"
            tk.Label(
                panel,
                text=message,
                bg=SURFACE,
                fg=TEXT,
                font=("Segoe UI Semibold", 10),
            ).pack()
            toast.update_idletasks()
            width = max(200, toast.winfo_reqwidth())
            height = toast.winfo_reqheight()
            x = max(0, self.root.winfo_screenwidth() - width - 24)
            y = max(0, self.root.winfo_screenheight() - height - 72)
            toast.geometry(f"{width}x{height}+{x}+{y}")
            toast.deiconify()
            toast.lift()
            self._toast = toast
            self._toast_job = self.root.after(1500, self._destroy_notification)
        except tk.TclError:
            self._destroy_notification()

    def _fade_notification(self, toast, alpha):
        if toast is not self._toast:
            return
        try:
            if not toast.winfo_exists():
                return
            alpha -= 0.08
            if alpha <= 0:
                self._destroy_notification()
                return
            toast.attributes("-alpha", alpha)
            self._toast_job = self.root.after(
                32, lambda: self._fade_notification(toast, alpha)
            )
        except tk.TclError:
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
        self._minimize_to_tray()

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
        self.engine.stop()
        self._cancel_save()
        self._persist()
        self._destroy_notification()
        self._unbind_hotkey()
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
        self._closed = True
        self._cancel_save()
        self.engine.stop()
        self._destroy_notification()
        self._unbind_hotkey()
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


def main():
    enable_dpi_awareness()
    config = reconcile_startup(load_config())
    engine = ClickEngine()
    root = tk.Tk()
    start_minimized = "--minimized" in sys.argv
    if start_minimized:
        root.withdraw()
    app = AutoClickerApp(root, config, engine)
    try:
        root.mainloop()
    finally:
        app.dispose()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as error:
        try:
            messagebox.showerror(APP_NAME, f"Improved AutoClicker could not start.\n\n{error}")
        except Exception:
            pass
        raise
