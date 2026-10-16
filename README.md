# DeskBits (拾碎) — Windows desktop icon organizer

A desktop toolbox for Windows that keeps your desktop tidy: sort icons into
groups and switch between them in one click, mark the current group with
colored highlight frames, pack icons away into real folders ("boxes"), open
whole folder sets at once, remember icon layouts, and track usage — all fully
offline.

> Downloads, install instructions and the full user-facing README (English +
> 中文): see the [deskbits-release repository](https://github.com/wangluyichen/deskbits-release).

## Highlights

- **Groups & one-click switching** — the floating bar at the top of the screen
  switches the desktop to a single group instantly; hide-by-attribute and
  pack-into-box modes.
- **Desktop boxes** — icons are really moved into plain folders under your
  user directory and scattered back to their original grid spots on demand.
- **Highlight frames** — 8 display styles × 10 zone paint styles, spotlight
  mode, per-group colors.
- **Layout memory & backup** — save/restore full icon positions, export data
  as one JSON, back up desktop files to zip.
- **System toolbox** — plain-language event viewer, scheduled tasks,
  condition watch, one-click maintenance, lock screen, parent mode, and more.
- **Bilingual UI** (Chinese / English), four themes, adjustable font size,
  density and icon style.

## Run from source

1. Python 3.10+ (developed on 3.14), Windows 10/11.
2. `pip install -r requirements.txt` (pywin32, Pillow, psutil, pynput).
3. Double-click `启动.bat`, or run `pythonw launcher.pyw`.
   A ready-to-run venv can be created with `一键安装环境.bat`.

## Project layout

- `core/` — desktop engine: icon manager, floating dock, desktop boxes,
  visibility router, highlighter/overlay painting, download tracker.
- `gui/` — Tkinter pages (main window, panels, lock screen, wizard, styles).
- `utils/` — logging & rotation, i18n, safe atomic IO, hotkeys, diagnostics.
- root scripts — packaging (`打包_Nuitka.py`), backup, tests harness
  (`自动测试.py`).

Code comments and commit messages are mostly Chinese.

## Data & privacy

Everything is stored locally under the app's `config\` directory. No network
calls, no uploads. Personal data can be wiped in one click from the app.

## Disclaimer

For personal study, research and everyday use. Built on official public
Windows interfaces (mostly read-only). Provided "as is", without warranty —
use the built-in backups before risky operations.
