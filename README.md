# DeskBits (拾碎) — a lazy-person's desktop toolbox for Windows

One small Windows tool that does two jobs. **First**, it keeps your desktop
tidy: sort icons into your own groups, switch the desktop to a single group
with one click, circle the current group with colored highlight frames, pack
icons away into real folders, remember whole layouts, and see what you actually
use. **Second**, it bundles the system chores most people have to dig through
Control Panel for — a built-in lock screen, a plain-language event viewer,
scheduled tasks, a process watcher that can trigger actions, and one-click
maintenance.

Everything runs locally. No account, no network calls, no telemetry.

## Downloads

- Source code (open): <https://github.com/wangluyichen/deskbits>
- Prebuilt zips: <https://github.com/wangluyichen/deskbits/releases>
- Gitee mirror (faster in mainland China): <https://gitee.com/wanke2022/deskbits-release>

Grab the latest **Releases** asset. Two flavours, same program:

| Asset | What it is |
|---|---|
| `…-single.zip` | one self-contained `.exe` — double-click and go |
| `…-dir.zip` | a folder — double-click the `.exe` of the same name inside; keep the folder together, don't pick files out of it |

Since 0.99-R043 the archive, its checksum file and the in-package document use
English names (`…-single.zip` / `…-dir.zip` / `…_SHA256.txt` / `README.txt`);
the document text itself is still Chinese.
Verify a download with the `…_SHA256.txt` sidecar shipped next to it.

## Release notes

Per-version notes are **not** duplicated here. The full series lives in
[`更新说明.md`](更新说明.md) in this repository, and each release zip carries a
short "what's new in this version" block at the top of its `README.txt`, plus a
"last three releases" note for copy-pasting into a release page.

## A minute with it

1. Open the **🖱️ Drag-to-group** tab and drag desktop icons onto a group on the right.
2. Move the mouse to the top edge of the screen — the floating bar appears; click a group name.
   The desktop instantly shows only that group.
3. Click **All** to bring every icon back, in its saved positions.
4. Try the **🧰 System** tab for the lock screen, event viewer and scheduled tasks;
   the **⚙️ Settings** tab for themes and behaviour.

## Features

### 🖼️ Desktop icon organizing (the core)

- **Your own groups** — create categories (Apps / Network / Work / English / …)
  and fill them by dragging icons or by name.
- **One-click switching** — the floating bar along the top edge of the screen
  swaps the desktop to a single group; everything else is hidden, then restored
  untouched.
- **The bar itself** — the background is a top-to-bottom gradient tinted by the
  active theme with a highlight line along the top edge, and you can dial the
  gradient in **three steps** (Settings → floating bar → "Bar gradient":
  flat / subtle / strong). "Strong" is the default; pick "flat" if you want it
  near-solid. Your choice is re-read on the next start. Hovering a slot lifts
  that slot one step brighter, so you can see which group you are about to
  click. The group dots and the capsule background follow the theme live.
- **Highlight frames** — circle the current group's icons with a colored border;
  7 shapes, adjustable colour, line width and dash pattern, colour-coded per group.
- **8 display modes** (Rules page, "Highlight" block; switchable at any time):
  plain box / tinted backing plate / highlighted labels / spotlight /
  **zone paint** / colour-block halo / highlighter stroke / corner badge.
  **Zone paint** merges icons that sit close together into one block and hangs a
  group nameplate on it; two icons merge too, and vertical runs merge as well —
  whether things merge is measured against your actual icon-cell spacing, so a
  wide desktop can't be swallowed by one giant frame.
- **How close counts as "one cluster"** — the "Merge" dropdown has four steps
  (**touching / gap of 1 / gap of 2 / gap of 3**); the name literally means "how
  many empty icon cells we still allow inside one cluster". The gap is measured
  in **icon-cell units**, so it follows your zoom setting instead of a fixed
  pixel value, and horizontal rows use the column width while vertical columns
  use the row height. The default ("touching") splits as soon as one cell is
  empty, and the widest step still only spans three empty cells. Hovering the
  control shows the pixel breakpoints for your current layout, so you can pick
  a step without guessing.
- **Ten paint styles inside zone paint** (Rules page, "Highlight" block):
  envelope (default, one ring around the whole cluster) / colour-block halo /
  highlighter / corner badge / crop marks (only the four corners) / double line
  (two rings with a genuinely transparent gap between them) / diagonal band /
  soft glow (three fading dithered bands inside the edge) / thin double line /
  thin soft glow. The last two are the same look one step quieter, for when the
  double line or the glow is too loud. Switching style only changes the picture —
  your colours and line widths stay as you set them.
- **Controls that don't apply are greyed out, and they tell you why** — each
  paint style declares which knobs it actually uses (envelope / double line /
  thin double line use shape + line width + dash; halos use shape + line width;
  highlighter and diagonal band use line width only; the corner badge uses none,
  because it draws a small colour dot instead). Hovering a greyed control says
  so in one sentence. Greying never overwrites your saved choice: switching back
  to "box" restores it.
  A legacy "frame size" option was dropped from the Rules page because no style
  consumed it; the stored value is kept for a future renderer.
- **Folder groups** — save a set of Explorer windows as a named group
  ("English", "Apps", …) and open the whole set from the bar, or have it open
  automatically when you switch to that group. On Windows 11 22H2+ the tabs
  merge into one window automatically; on Windows 10 they open as separate windows.
- **Layout memory** — save and restore the positions of all icons.
  Manual mode: switching groups or restoring a layout places everything;
  after you rearrange by hand, press Save.
- **System icons** — PC / Recycle Bin / Network / Control Panel / user folder
  can follow a group or stay always shown / always hidden. A group marked
  "exclude system icons" really takes those five off the desktop while it is
  active and puts them back exactly as they were (it does not decide for you
  that they should now be visible).
- **Ranking groups** — show only the top *N* most-used icons, or ranks *M…N*,
  and turn a range into a named, coloured group. Tick "resident" to pin it to
  the bar on every start; leave it unticked and it still appears on the bar for
  this session without writing anything to disk. Rebuilding a range replaces
  that group instead of erroring, and a **↻ Refresh** button re-reads the latest
  ranking into an existing group (all groups, or just one) without changing
  names or colours. Ranking groups land in a temporary tail section of the bar
  (capped at 6) so they can never evict one of your real categories. Every build
  attempt is logged, including refusals and the reason (empty list, reserved
  name, write failure). Two **⛔** escape hatches: un-pin residency, or delete
  group and category together — and neither path can touch a category you made
  by hand.
- **Desktop boxes** — select icons and press **📥 Pack into box**: the icons are
  *really moved* (not hidden) into `C:\Users\<you>\桌面盒子deskbits\<box name>`,
  so the desktop clears at once and the box is a normal folder you can browse.
  **📤 Scatter back** returns each icon along the manifest and re-places it per
  the saved layout — the desktop may still be rearranging at that moment, so the
  program waits for each icon to really show up (about 2.2 s at most) before
  writing positions, and an occupied slot falls back to the nearest free cell
  rather than claiming success it did not get. Name clashes, in-use files and
  cross-drive moves are named
  out loud instead of failing silently (Resources page → box panel).
  The card is a symmetrical three-column layout — pinnable desktop items on the
  left, three actions in the middle, box contents on the right — and a filter box
  narrows both tables at once, with "matched / total" and "selected" in the headers.
  **🔄 Auto-pack is on by default**: every icon returns to the box named after
  its own category, cross-category items each go their own way, missing boxes are
  created on the spot — and anything you have never classified is *not* guessed
  for you; a dialog reads those names back to you. Un-tick it to go back to
  batching into the box selected in the dropdown. Creating a category also
  creates its matching box, so you never type the name twice.
  Two more buttons finish the job: **🧹 Collect duplicate icons** sweeps
  `Name_20261011_152441`-style duplicates off the desktop into a
  **"收纳·重复的"** box (with a `清单.json` recording original names and
  sources; that box is write-only — it is never scattered back and never counted
  as "unclaimed"), and **Delete empty boxes** clears shells accumulated over
  many runs, touching only genuinely empty ones and naming each before removal.
  Icons left in a box because of a name clash are not counted as failures: the
  desktop copy is untouched, and the next scatter will succeed once you resolve it.
- **Desktop backup** — zip the desktop folder, so a lost icon is one click away.

### 🧰 System tools (the "System" page)

- **🔒 Built-in lock screen** — a full-screen lock with a big clock and four font
  sizes. The wallpaper dropdown picks one of the **four built-in theme images**
  (dark / darker / light / eye-care), your own imported picture, or none.
  Separate password with security-question recovery; "Sign out" is hidden while
  locked so it can't be used to bypass it; parent-mode timeouts open the same lock.
- **🖥️ Desktop system icons (one checkbox)** — toggles PC / Recycle Bin / user
  folder / Control Panel / Network without hunting through
  Personalisation → Themes → Desktop icon settings. The tick honestly reflects
  current state, it is the same switch as the "system icons stay resident"
  setting (both echo each other), and it re-tidies the layout afterwards, so
  clicking it no longer leaves your desktop messy until the next group switch.
- **👀 Condition watch** — poll a process every 5 s for "appeared / disappeared"
  and run an action, e.g. shut the host down after a virtual machine exits.
  Two examples plus two practical templates, editable, each with its own
  enable/disable switch, and a pop-up when it fires. Rules travel with
  export/import of all data (imported rules start disabled).
  When the action is **shutdown or restart**, you are asked once more — a click
  cancels the countdown, and the log reports the outcome truthfully (cancelled /
  not cancelled with the return code / result unreadable, please check). If
  nobody clicks within 30 s, the original plan runs.
- **📜 Event viewer in plain language** — translates the Windows system log into
  human sentences (31 mappings: boot/shutdown, crashes, service failures, disk,
  drivers, updates …), marks failures in red, double-click for details, one click
  to generate a filtered query, and "🌐 Native viewer" opens the same filter as a
  custom view in the real Event Viewer.
- **📅 Scheduled tasks** — list / run / enable / disable / delete Windows
  scheduled tasks, grouped by action type, with plain-language notes and creation
  times. Includes a creator (timed shutdown/restart, launch a program, run a
  command, **run after a delay** (one-shot), **start a VM** — VMware/VirtualBox,
  multi-select), seven ready templates and an editor. Destructive task types are
  red-marked and ask for confirmation.
- **⚡ One-click maintenance** — clean temp files, empty Recycle Bin (with a count
  dialog), flush DNS, restart Explorer, rebuild the icon cache, clear the
  clipboard, clear "recently used", disk cleanup. Each reports success or failure
  in a dialog and in the log.

### 📥 Everything else

- **Download records** — remembers where browser downloads went, by reading your
  local Chrome/Edge download history plus a watch on your download folders
  (records stay on this machine); ignore an entry, un-ignore it, or group by folder.
- **Parent mode** — the desktop keeps only the children's groups, the taskbar
  hides (across all monitors), and the timeout opens the lock screen.
  Password + security-question recovery.
- **Usage ranking** — three leaderboards, switchable off in one click (packaged
  builds start with it off). Rows that no longer match reality (icon renamed or
  deleted, folder moved, category removed) carry a `⚠` at the line start, and
  hovering explains which case it is. **It only warns: no statistic is rewritten
  and no record is deleted.** System icons like PC and Recycle Bin are never flagged.
- **First-run wizard** — seven steps, each naming the tab where the thing lives,
  with jump-there buttons; "don't show again" is available, Settings has a
  "show wizard at start" switch and a "🧭 Re-run wizard" button, and the wizard
  itself can import/export all data.
- **Also** — global hotkeys, quick locate, data import/export (backup path
  configurable), four themes switched live, adjustable font size / density /
  icon style, bilingual interface (Chinese / English).
- **🎨 App icon** — six icon sets on the Settings page, one click to change
  (applies to the window and taskbar immediately, survives restart; default is
  "B 拾碎成序"); "📂 Open icon folder" reveals the current `.ico` so you can point
  a desktop shortcut at it; "↺ Restore default icon" puts it back.

## Running it

**Packaged build (what most users want)** — unzip and double-click the `.exe`.
No Python needed: the single-file package is just that one executable; in the
single-directory package double-click the same-named `.exe` inside and leave the
folder intact.

**From source (developers)** — Python 3.10+ (developed on 3.14) on Windows 10/11:

```
pip install -r requirements.txt      # pywin32, Pillow, psutil, pynput
```

| Entry point | What it does |
|---|---|
| `启动.bat` | daily entry — a guardian relaunches the app if it dies |
| `自动测试.bat` | one-shot self-test of 11 feature areas; run this first when something misbehaves |
| `打包_Nuitka_单文件.bat` / `打包_Nuitka_单目录.bat` | produce release packages (zip + checksum) |
| `一键安装环境.bat` | create a ready-to-run virtual environment |
| `一键备份.bat` | zip the source and required content into `backup\` |

Chinese file names above are the real file names in the repository. Code
comments and commit messages are mostly Chinese.

**Project layout**

- `core/` — desktop engine: icon manager, floating dock, desktop boxes,
  visibility router, highlight/overlay painting, download tracker.
- `gui/` — Tkinter pages: main window, panels, lock screen, wizard, styles.
- `utils/` — logging and rotation, i18n, atomic file IO, hotkeys, diagnostics.
- repository root — packaging, backup and the test harness (`自动测试.py`).

## First launch: Windows SmartScreen

The build is not code-signed, so on some machines Windows shows
"Windows protected your PC" the first time. It is the generic warning for any
unsigned new program, not a verdict on this file: choose
**More info → Run anyway**, or right-click the `.exe` → Properties → tick
**Unblock**. Confirm integrity with the checksum file shipped next to the
download (`certutil -hashfile <file> SHA256`).

## Themes

Dark / darker / light / eye-care, switchable at any time — **applied live, no restart**.

## Reporting problems

Open an **Issue** on this repository with: Windows version, what you did, what
you expected, what happened — a screenshot helps a lot. If a feature "did
nothing", the in-app log page is the most useful attachment.

## Data & privacy

All data stays in the app's local `config\` and `data\` directories. Before
handing your machine to someone else, "📦 Data management" wipes icon lists,
folder groups, statistics and logs in one click. Exports land in `backup\`
by default (path configurable on the same page). There is no upload path in
this program. See [`SECURITY.md`](SECURITY.md) for how to report a vulnerability.

## Scope & disclaimer

- Intended for **personal study, research and everyday productivity**. Don't use
  it commercially or for anything against your local law.
- Every feature goes through official, publicly documented Windows interfaces;
  it is read-mostly, **does not inject code, does not install hooks, does not
  upload data**. Automation (scheduled tasks, condition watch) should only be
  used on machines you're entitled to administer, and you own the actions you
  create there (shutdowns, deletions).
- The software is provided "as is", **without warranty of any kind**. The author
  accepts no liability for direct or indirect loss (including data or time lost)
  from use or misuse. Use the built-in backup features before risky operations.
- The name, icons and documentation belong to the author. Please don't repackage
  them verbatim or publish them under your own name.

## Acknowledgements

Ideas and interaction patterns were borrowed from several excellent open-source
desktop-organizing projects (MIT and similarly permissive licences). Thanks to
the open-source community for sharing.

---

> I'm not a professional programmer — I only made this as a tool for my own
> use. The code is rough in places, so please go easy on me.
