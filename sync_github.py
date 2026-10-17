# -*- coding: utf-8 -*-
"""一键同步到 GitHub（私有仓）——安全过滤镜像推送引擎。

用法：
    python sync_github.py            # 交互确认后推送
    python sync_github.py --yes      # 跳过确认（无人值守）
    环境变量 GH_REPO 可覆盖仓库地址（默认 wangluyichen/deskbits）。

与 提交到Gitee.bat 的分工：
    Gitee = 提交 + 推送（主仓库、完整内容，走 提交到Gitee.bat）；
    GitHub = 只同步**已提交**的内容（本引擎），且剥离 代码存档/ 与 测试_ui/。

六道安全门（任何一道不过就中止，绝不推送）：
  1. 永不给主仓库加 github 远端、永不从主仓库直推——只在临时镜像副本里操作，
     本地仓库与 Gitee 远端零改动；
  2. 镜像里 `git rm -r` 剥离 代码存档/、测试_ui/ 与一批本地文件（GitHub 只留
     产品代码）；★R327 起不再依赖 git-filter-repo（本机没装，且 [4.6] 反正
     要把整条历史丢掉，重写历史是白干的活）；
  3. 过滤后三重校验：两个目录在 master 树里必须消失 / core、gui、utils、
     README.md 还在（防过滤过猛）/ 提交数必须 > 0；
  4. 隐私门（与 提交到Gitee.bat 同口径）：data/ config/ icon_backups/
     build/ dist_onefile/ dist_onedir/ 不得出现在 master 树里（防 gitignore
     失效时隐私随历史上云）；
  5. 推送前打印将推内容（提交数/HEAD/体积）并要确认（--yes 跳过）；
  6. 推送后回读核对：ls-remote 的 master == 镜像 master。

★R327 追加的两道（因为仓库已经**公开**，元数据就是正文）：
  4.5 单提交化：公开仓只留**一条**快照提交（从剥离干净的树 `--orphan` 重切）。
      历轮 184 条提交的作者/提交者邮箱里有两个 QQ 号，而**按路径过滤历史的老办法
      只删路径不洗元数据** ⇒ 整条历史推上去＝公开泄露；从工作树重切一条快照，
      历史与元数据一起归零，比 mailmap 更硬（本仓现状就是单提交）。
  4.7 元数据闸：推之前逐条数——`rev-list --count` 必须是 1，且所有
      author/committer 邮箱必须等于 GH_EMAIL（noreply），多一条或多一个
      邮箱就中止。这条闸也是给以后的人兜底：谁改回直推历史，它当场红。
  4.8 快照树复核：单提交化**之后**再看一眼最终那一发的文件树——剥离项
      （含 `.gitignore`，用户 2026-10-16 点名不上传）与隐私目录一个不许在，
      core/gui/utils/README.md 一个不许少。

认证说明：本机 `github.com:443` 被墙（https 直推必 reset），所以默认地址走
**SSH**：`git@github.com:wangluyichen/deskbits.git` + `~/.ssh/config` 里
`Host github.com → HostName ssh.github.com, Port 443`（密钥 `id_ed25519_github`，
公钥要在 https://github.com/settings/keys 里先加上，否则推送被拒）。
`GH_REPO` 环境变量仍可覆盖成 https（挂代理的机器上用）。
"""
import os
import shutil
import subprocess
import sys
import tempfile

MAIN_REPO = os.path.dirname(os.path.abspath(__file__))
#: ★R327（本机 `github.com:443` 实测被墙，https 直推必 reset ⇒ 默认走 SSH-443；
#: GH_REPO 环境变量仍可覆盖）
GH_URL = os.environ.get('GH_REPO') or \
    'git@github.com:wangluyichen/deskbits.git'
#: ★R327：公开仓里提交身份**只能**是 GitHub noreply 邮箱。原先这里硬编码
#: 作者的 QQ 邮箱（README 换装那一发的提交作者），仓库一公开它就变成
#: 网页上的一行明文 ⇒ 换成 noreply，并被下面的元数据闸逐条核。
GH_NAME = os.environ.get('GH_NAME') or 'wangluyichen'
GH_EMAIL = os.environ.get('GH_EMAIL') or \
    'wangluyichen@users.noreply.github.com'
STRIP_DIRS = ('代码存档', '测试_ui')
# 历史剥离：R210 只停止跟踪没洗历史，这些的旧版本仍躺在 git 历史里
# （工作进度/DEV/待办存档=开发日志含真机细节；Gitee bat=账号名+令牌流程说明）。
# 公开仓库前必须从**历史**里剥掉；win11_env/hidden_state 历史为 0，顺手保险。
# ★R327（用户点名「.gitignore 这个也不用上传了，完全是暴露自己的」）：本机的
# 忽略规则改放 `.git/info/exclude`（不入库），文件本身不再进任何远端；这里再列
# 一道是防它哪天被误 add 回去就顺着快照上公开仓。
# `BUGS.md`＝开发侧 bug 速查（引 DEV.md 纪律、逐轮编号、用户原话），2026-10-16
# 开源定案时点过名要剥；这一发把它重新钉进清单（`发布用_README_Gitee.md` **不剥**：
# 它是发布包 README 的数据源，剥了源码打包者就退回英文 README）。
STRIP_EXTRA = ('工作进度.md', 'DEV.md', '提交到Gitee.bat',
               '待办存档_20260920.md', '.gitignore', 'BUGS.md')
STRIP_GLOBS = ('工作进度*.md', '待办存档*.md', 'win11_env*', 'hidden_state.json')
MUST_HAVE = ('core', 'gui', 'utils', 'README.md')
PRIVACY = ('data', 'config', 'icon_backups', 'build',
           'dist_onefile', 'dist_onedir')

# GitHub 侧的默认 README（英文）。本地/Gitee 仍是中文 README.md 不动；
# 引擎在镜像里把中文版改名为 readme-中文.md，再写入这份英文版。
_README_EN = '''# DeskBits (拾碎) — a lazy-person's desktop toolbox for Windows

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
  *really moved* (not hidden) into `C:\\Users\\<you>\\桌面盒子deskbits\\<box name>`,
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
| `一键备份.bat` | zip the source and required content into `backup\\` |

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

All data stays in the app's local `config\\` and `data\\` directories. Before
handing your machine to someone else, "📦 Data management" wipes icon lists,
folder groups, statistics and logs in one click. Exports land in `backup\\`
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
'''

MIRROR = os.path.join(tempfile.gettempdir(), 'deskbits_github_mirror.git')

_GIT = 'git'
if shutil.which('git') is None:
    _GIT = r'C:\Program Files\Git\bin\git.exe'


def run(cmd, cwd=None, check=True):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                       encoding='utf-8', errors='replace')
    if check and p.returncode != 0:
        print('[FAIL] 命令失败 rc=%s: %s' % (p.returncode, ' '.join(cmd[:5])))
        if p.stderr:
            print(p.stderr.strip()[:800])
        raise SystemExit(1)
    return p


def main():
    yes = '--yes' in sys.argv[1:]

    print('=' * 52)
    print('  同步到 GitHub（私有镜像，剥离 代码存档/测试_ui）')
    print('=' * 52)

    # 前置：主仓库必须是个 git 仓库且有 master
    run([_GIT, 'rev-parse', '--git-dir'], cwd=MAIN_REPO)
    run([_GIT, 'rev-parse', '--verify', 'master'], cwd=MAIN_REPO)

    # [1] 重建临时镜像（每次全新过滤，绝不复用旧副本）
    mirror = MIRROR
    if os.path.exists(MIRROR):
        shutil.rmtree(MIRROR, ignore_errors=True)
        if os.path.exists(MIRROR):
            # 旧副本被占用删不掉（多半是上次被强杀的 git 进程还抓着）
            # ⇒ 换一个全新目录继续，不因残留垃圾中断本次同步
            import tempfile as _tf
            mirror = os.path.join(
                _tf.mkdtemp(prefix='deskbits_gh_sync_'), 'mirror.git')
            print('[提示] 旧镜像被占用删不掉，改用新目录：%s' % mirror)
    print('[1/5] 克隆临时镜像（本地操作，不碰网络）...')
    # 普通克隆（带工作区）而非 --mirror 裸仓库：后面 git mv 换 README 需要工作区
    run([_GIT, 'clone', '--quiet', MAIN_REPO, mirror])

    # [2] 快照剥离：把不公开的内容从**镜像的工作树+索引**里删掉。
    # ★R327 改判：原先这一步跑 `git filter-repo`（本机没装这个模块，脚本当场
    # 死于 No module named git_filter_repo），而 [4.6] 本来就要把整条历史丢掉、
    # 只留一条快照 ⇒ 重写历史是白干的活。删文件这一步 plain git 就够：
    # `git rm -r` 同时动索引与工作树，[4.6] 的 `git add -A` 才不会把它捡回来。
    print('[2/5] 快照剥离：代码存档/ 测试_ui/ + 不入库的本地文件...')
    _unwanted = list(STRIP_DIRS) + list(STRIP_EXTRA) + list(STRIP_GLOBS)
    run([_GIT, 'rm', '-r', '-q', '--ignore-unmatch', '--force'] + _unwanted,
        cwd=mirror)
    # 兜底：万一有同名文件因为各种原因还躺在工作树里（手动往镜像塞过东西），
    # 直接从盘上抹掉——[4.6] 是从工作树重切的，盘上有什么就上什么。
    import fnmatch
    for _name in os.listdir(mirror):
        if _name in ('.git',):
            continue
        if _name in STRIP_DIRS or _name in STRIP_EXTRA \
                or any(fnmatch.fnmatch(_name, g) for g in STRIP_GLOBS):
            _p2 = os.path.join(mirror, _name)
            shutil.rmtree(_p2) if os.path.isdir(_p2) else os.remove(_p2)
            print('[提示] 工作树残留 %s 已删除。' % _name)

    # [3] 过滤结果三重校验
    # ★R327：`git rm` 动的是**索引+工作树**，master 那几条老提交当然还带着
    # 代码存档/ ——所以这里量的是"接下来 [4.6] 要拿去重切快照的那份索引"，
    # 不是老提交的树（老提交反正整条丢掉）。真正的"上云前最后一眼"在 [4.8]。
    _idx = run([_GIT, '-c', 'core.quotePath=false', 'ls-files'],
               cwd=mirror).stdout.split('\n')
    tree = sorted({ln.split('/')[0] for ln in _idx if ln.strip()})
    bad = [d for d in STRIP_DIRS if d in tree]
    if bad:
        print('[FAIL] 过滤没生效，以下目录仍在 master 树里，绝不推送: %s' % bad)
        raise SystemExit(1)
    miss = [d for d in MUST_HAVE if d not in tree]
    if miss:
        print('[FAIL] 过滤过猛，关键内容缺失: %s' % miss)
        raise SystemExit(1)
    commits = int(run([_GIT, 'rev-list', '--count', 'master'],
                      cwd=mirror).stdout.strip())
    if commits <= 0:
        print('[FAIL] 过滤后提交数为 0，绝不推送。')
        raise SystemExit(1)

    # [4] 隐私门（与 Gitee 脚本同口径）
    leak = [d for d in PRIVACY if d in tree]
    if leak:
        print('[FAIL] 检测到隐私/产物目录在提交历史里: %s' % leak)
        print('       疑似 .gitignore 失效被提交过，先排查再同步。')
        raise SystemExit(1)

    # [4.5] GitHub 默认 README 换英文（中文版保留为 readme-中文.md）
    # 本地仓库与 Gitee 一字不动；只在镜像里追加一个换装提交。
    print('[4.5] 镜像内换装默认 README：英文为 README.md，中文保留为 readme-中文.md')
    if 'readme-中文.md' not in tree:
        run([_GIT, 'mv', 'README.md', 'readme-中文.md'], cwd=mirror)
        with open(os.path.join(mirror, 'README.md'), 'w',
                  encoding='utf-8', newline='\n') as fh:
            fh.write(_README_EN)
        run([_GIT, 'add', '-A'], cwd=mirror)
        run([_GIT, '-c', 'user.name=%s' % GH_NAME,
             '-c', 'user.email=%s' % GH_EMAIL,
             'commit', '-q', '--no-verify',
             '-m', 'README: English as default; Chinese kept as readme-中文.md'],
            cwd=mirror)
    else:
        print('[提示] 中文版已在位（readme-中文.md），跳过换装。')

    # [4.6] ★R327 单提交化：从剥离干净的树重切**一条**快照提交。
    # 为什么不是 mailmap、也不是"按路径过滤历史"：后者只删路径不改元数据，
    # 历轮 184 条提交的 author/committer 邮箱（两个 QQ 号）会原样跟着上云；
    # 公开仓里提交元数据就是正文（GitHub 提交页直接显示成网页文本）。
    # ⇒ 历史整条丢掉，只留快照。
    with open(os.path.join(MAIN_REPO, 'version.txt'), encoding='utf-8') as _vf:
        _ver = _vf.read().strip()
    print('[4.6] 单提交化：重切一条 %s 快照提交（历史与元数据一起归零）' % _ver)
    run([_GIT, 'checkout', '-q', '--orphan', 'gh_snapshot'], cwd=mirror)
    run([_GIT, 'add', '-A'], cwd=mirror)
    run([_GIT, '-c', 'user.name=%s' % GH_NAME, '-c', 'user.email=%s' % GH_EMAIL,
         'commit', '-q', '--no-verify', '--no-gpg-sign',
         '-m', 'DeskBits %s source (clean single-commit snapshot)' % _ver],
        cwd=mirror)
    run([_GIT, 'branch', '-f', 'master', 'gh_snapshot'], cwd=mirror)
    run([_GIT, 'checkout', '-q', 'master'], cwd=mirror)
    run([_GIT, 'branch', '-D', 'gh_snapshot'], cwd=mirror)

    # [4.7] ★R327 元数据闸：必须是**一条**提交，且所有 author/committer 邮箱
    # 全等于 GH_EMAIL。以后谁把这一段改回直推历史，这一闸当场红。
    _n = int(run([_GIT, 'rev-list', '--count', 'master'],
                 cwd=mirror).stdout.strip())
    if _n != 1:
        print('[FAIL] 单提交化没生效：镜像里有 %d 条提交，绝不推送。' % _n)
        raise SystemExit(1)
    _mails = set(m for m in run([_GIT, 'log', '--format=%ae%n%ce', 'master'],
                                cwd=mirror).stdout.split() if m.strip())
    if _mails != {GH_EMAIL}:
        print('[FAIL] 提交元数据里出现白名单外的邮箱，绝不推送: %s'
              % sorted(x.encode('ascii', 'backslashreplace').decode()
                       for x in _mails))
        raise SystemExit(1)
    print('[4.7] 元数据闸通过：1 条提交，作者邮箱只有 %s' % GH_EMAIL)

    # [4.8] ★R327 快照树复核（上云前最后一眼）：单提交化是从**工作树**重切的，
    # 所以这一步查的是"最终那一发到底带了什么"——剥离项/隐私目录一个不许在，
    # 产品主干一个不许少（少了就是过滤过猛，推上去等于推一份跑不起来的代码）。
    _tree = run([_GIT, '-c', 'core.quotePath=false', 'ls-tree',
                 '--name-only', 'master'], cwd=mirror).stdout.split()
    _leak = [d for d in STRIP_DIRS + STRIP_EXTRA + PRIVACY if d in _tree]
    if _leak:
        print('[FAIL] 快照树里仍有剥离项/隐私目录，绝不推送: %s'
              % [x.encode('ascii', 'backslashreplace').decode() for x in _leak])
        raise SystemExit(1)
    _miss = [d for d in MUST_HAVE if d not in _tree]
    if _miss:
        print('[FAIL] 快照树缺产品主干（过滤过猛），绝不推送: %s' % _miss)
        raise SystemExit(1)
    print('[4.8] 快照树复核通过：%d 个顶层条目，无剥离项/隐私目录，主干齐全'
          % len(_tree))

    head = run([_GIT, 'rev-parse', '--short', 'master'],
               cwd=mirror).stdout.strip()
    size = run([_GIT, 'count-objects', '-vH'], cwd=mirror).stdout
    pack = [ln.split(': ')[1] for ln in size.splitlines()
            if ln.startswith('size-pack')]
    print('[3/5] 校验通过：%d 个提交，HEAD=%s，包体积 %s' %
          (_n, head, pack[0] if pack else '?'))
    print('[4/5] 目标：%s （master，强制覆盖远端同名分支）' % GH_URL)
    if not yes:
        ans = input('确认推送？(y/N): ').strip().lower()
        if ans != 'y':
            print('已取消。镜像保留在 %s 供检查。' % mirror)
            raise SystemExit(0)

    # [5] 推送（首次会弹浏览器 GitHub 授权，凭据管理器记住后免输）
    print('[5/5] 推送中...')
    p = run([_GIT, 'push', '--force', GH_URL,
             'refs/heads/master:refs/heads/master'], cwd=mirror, check=False)
    if p.returncode != 0:
        print('[FAIL] 推送失败。常见原因：')
        print('  1) 首次推送弹的浏览器授权窗口没完成（重跑一次再点它）')
        print('  2) 网络不通 GitHub（挂代理后重试）')
        if p.stderr:
            print(p.stderr.strip()[:800])
        print('镜像保留在 %s 供诊断。' % mirror)
        raise SystemExit(1)

    # [6] 回读核对
    remote_sha = run([_GIT, 'ls-remote', GH_URL, 'refs/heads/master'],
                     cwd=mirror).stdout.split()[0]
    local_sha = run([_GIT, 'rev-parse', 'master'], cwd=mirror).stdout.strip()
    if remote_sha != local_sha:
        print('[FAIL] 推送后回读不一致（远端 %s != 镜像 %s），请检查 GitHub 页面。'
              % (remote_sha[:10], local_sha[:10]))
        raise SystemExit(1)

    shutil.rmtree(mirror if mirror == MIRROR else os.path.dirname(mirror),
                  ignore_errors=True)
    print('[OK] 同步完成，远端 master = %s（已剥离两个目录）。' % local_sha[:10])


if __name__ == '__main__':
    main()
