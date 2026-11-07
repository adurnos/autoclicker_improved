# Improved AutoClicker

A compact Windows auto-clicker written in Python. It runs from a system tray icon, stores preferences outside the application folder, and uses a flat monochrome interface.

## Features

- Zero Directory Clutter: Preferences save to `%LOCALAPPDATA%\ImprovedAutoClicker\config.json`.
- Background Persistence: Closing the interface keeps the click loop and global hotkey active in the system tray.
- Windows Startup Registration: An optional checkbox adds or removes the app from the current user's Run key.
- Anti-Detection Timing: Optional randomization varies each click delay between 80 and 120 percent of the chosen interval.
- Toggle Notifications: A borderless overlay confirms when clicking starts or stops.
- Monochrome Interface: Black, dark gray, light gray, and white only.

## Requirements

- Windows 10 or Windows 11
- Python 3.9 or newer

## Installation

```bash
pip install pyautogui keyboard pystray pillow
```

## Usage

```bash
python improved_autoclicker.py
```

Press F6 to start or stop clicking. Enter a different key or combination in the hotkey field and press Enter to rebind it. Closing the window hides the app in the system tray. Use the tray menu to reopen the interface or exit the process.

## Compilation

```bash
pyinstaller --noconsole --onefile -n autoclicker_improved improved_autoclicker.py
```

The executable is written to `dist/autoclicker_improved.exe`.

## License

Distributed under the MIT License. See LICENSE for details.
