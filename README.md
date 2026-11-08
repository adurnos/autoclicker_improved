# Improved AutoClicker

A compact Windows auto-clicker written in Python. It runs from a system tray icon, stores preferences outside the application folder, and uses a flat monochrome interface.

## Features

- Zero Directory Clutter: Preferences save to `%LOCALAPPDATA%\ImprovedAutoClicker\config.json`.
- Background Persistence: "Minimize to Tray" hides the window while the click loop and global hotkey keep working.
- Global Hotkey: Start and stop clicking from any application. The hotkey works even while the window is hidden.
- Toggle Notifications: A borderless popup and a short system sound confirm when clicking starts or stops.
- Anti-Detection Timing: Optional randomization varies each click delay between 80 and 120 percent of the chosen interval.
- Monochrome Interface: Black, dark gray, light gray, and white only.

## Requirements

- Windows 10 or Windows 11
- Python 3.9 or newer, only to run the source or build the executable

The built executable needs no Python installation.

## Installation

```bash
pip install -r requirements.txt
```

## Usage

```bash
python improved_autoclicker.py
```

The app does not start with Windows. Open it yourself each time you want to use it.

- **Set the hotkey:** click the hotkey box, then press the combination you want (for example `Ctrl+Alt+K`). The box cannot be typed into; it listens for keys. Press `Esc` to cancel, or click elsewhere and the previous hotkey keeps working.
- **Start or stop clicking:** press the hotkey, or use the Start/Stop button.
- **Minimize to Tray:** hides the window and keeps clicking and the hotkey active. Reopen from the tray icon.
- **Close completely:** click the window's X. The click loop, hotkey and tray icon all shut down.

The minimum interval is 10 milliseconds.

The popup banner is click-through, announces **AUTOCLICKER ENABLED** or **AUTOCLICKER DISABLED**, shows the hotkey that stops it and plays a short two-tone sound (rising when it starts, falling when it stops).

### If the hotkey does not respond

The hotkey is detected by reading the physical key state 40 times a second instead of installing a global keyboard hook, so it keeps working when Windows blocks hooks, when the window is in the background, and when another program has focus.

- The app writes `Hotkey 'f6' is armed` and `Hotkey 'f6' pressed - clicking started` to the log. If the "armed" line is there but no "pressed" line appears, the keys are being swallowed before they reach the app — usually because an administrator-level program has focus. Run the auto-clicker as administrator as well.
- Laptop function keys often need `Fn`: on most laptops `F6` alone sends a media key rather than `F6`. Choose a combination such as `Ctrl+Alt+K` instead.
- The status line under the hotkey box reports when a combination could not be used at all.

## Logs

Startup problems, hotkey activity and clicking errors are written to `%LOCALAPPDATA%\ImprovedAutoClicker\autoclicker.log`.

## Building the executable

Run this from the project folder, in Windows with Python installed:

```bash
pip install -r requirements.txt pyinstaller
pyinstaller --noconsole --onefile -n autoclicker_improved improved_autoclicker.py
```

The executable is written to `dist\autoclicker_improved.exe`. It is self-contained, so it can be copied to any Windows computer and started by double-clicking; nothing needs to be installed there. `--noconsole` is required, otherwise a black console window opens behind the app.

- The first start of a one-file build takes a few seconds while it unpacks itself, so the window appears a moment after the double-click.
- Settings and logs stay in `%LOCALAPPDATA%\ImprovedAutoClicker\`, so replacing the executable never loses your configuration.
- The executable is unsigned, so Windows SmartScreen may warn about it on another computer. Choose **More info**, then **Run anyway**.
- `build/`, `dist/` and the generated `.spec` file are build leftovers and are already ignored by git.

## License

Distributed under the MIT License. See LICENSE for details.
