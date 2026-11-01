# -*- coding: utf-8 -*-
"""📜 日志页（★ R43：自 gui/main_window.py 拆出）。"""
import io
import logging
import os
import shutil
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import ttk, messagebox

from gui import ui_layout
from gui.ui_layout import SP
from utils.app_paths import get_app_dir  # ★ R65-Bug1
from utils import i18n   # ★ R73 双语
APP_DIR = get_app_dir()
logger = logging.getLogger("desktop_manager")

LOG_RETAIN_DAYS = 60
# ★R249 补充2（用户："日志打包需要打包config不呢？"）：
#   · **config/ 不收**——里面没有一条日志，全是设置与用户内容
#     （`app_settings.json` 带机器路径、`parental.json` 家长控制名单、
#     `hidden_state.json`、`lock_screen_image.jpg` 个人图片）。一键包是发给
#     别人排查用的，顺手带上这些＝把隐私一起发出去。要查设置走
#     「数据管理 → 导出全部数据」那份 JSON。
#   · 但 `data/save_audit.log` **该收而此前漏收**：★R34 起它专门记"布局为什么
#     被覆盖"的结论（当初就是为了躲开 app.log 自清才单开一个文件），而
#     "桌面变乱"这类案子偏偏就要看它。
_LOG_GLOB_PATTERNS = ['app.log', 'app.log.1', 'app.log.2',
                      'switch.log', 'switch.log.1', 'switch.log.2',
                      'crash.log', 'stderr.log', 'launcher.log',
                      # ★R286-A1：跨进程"在飞"留痕（崩在哪一发的唯一现场），
                      # 与 crash.log 同性质 ⇒ 进包、也进 60 天自清
                      'win32_trail.log',
                      '_f5_out.txt', '_persist_out.txt']
#: **只进诊断包、不进 60 天自清**的那一份。为什么单列：`_LOG_GLOB_PATTERNS`
#: 同时被 `auto_clean_logs` 用（:63 那圈），把它加进去等于让"清理旧日志"
#: 顺手删掉唯一的长期审计——正是要查案的时候没证据了。
_LOG_BUNDLE_ONLY = ['save_audit.log']

# ★R226（NEW7-P2-3）：界面日志 Text 的行数上限。本程序是常驻守护进程，
# `_log` 每条都往 Text 里插且历史上从不删——连开几天后几万行吃内存、
# 滚动条拖不动、`see(END)` 也越来越慢。只裁**界面显示**，
# data/app.log 落盘链路一字不动（排查证据仍在全量日志里）。
LOG_VIEW_MAX = 2000


def trim_log_text(text, keep: int = LOG_VIEW_MAX) -> int:
    """把界面日志 Text 裁到最近 keep 行 ⇒ 裁掉的行数（未超限返回 0）。

    行号口径：Tk 的 `end-1c` 落在末尾那个空行上，内容行数 = 行号 - 1；
    删除区间 `[1.0, (last-keep+1).0)` 正好留最后 keep 整行。
    """
    try:
        last = int(text.index('end-1c').split('.')[0]) - 1
        if last <= keep:
            return 0
        text.delete('1.0', '%d.0' % (last - keep + 1))
        return last - keep
    except Exception:
        logger.exception('[log_page] 界面日志裁剪失败')
        return 0

def _clean_fail_note(where, name, e):
    """★R319-M10：清理旧日志删不动时留的一行（只进 app.log，不上屏）。

    为什么必须留：`win32_trail.log` 这类文件正被常驻句柄占着，`unlink` 天天失败
    是**常态**而不是意外；旧写法 `except Exception: pass` 把它吞成零痕迹，于是
    用户点完"清理旧日志"只看见"已清理 N 个"，那几个老的还在——下一轮排查照样
    被老证据淹，而且没人知道为什么没清掉（★R230b：兜底可以吞，必须留一行）。
    只写文件名不写全路径：这条会长期跑在启动路径上，把机器路径刷满日志没意义。
    """
    logger.warning('[日志清理] %s 里的 %s 没删掉（正被占用？）: %s'
                   % (where, name, e))


def auto_clean_logs(retain_days: int = LOG_RETAIN_DAYS):
    """清理 data/ 下超过 retain_days 天的日志文件"""
    data_dir = APP_DIR / 'data'
    if not data_dir.exists():
        return 0
    cutoff = datetime.now().timestamp() - retain_days * 86400
    cleaned = 0
    for pattern in _LOG_GLOB_PATTERNS:
        for f in data_dir.glob(pattern + '*'):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    cleaned += 1
            except Exception as e:
                _clean_fail_note('data/', f.name, e)
    return cleaned



def _sweep_expired_logs(retain_days: int = LOG_RETAIN_DAYS) -> int:
    """过期文件删除（★R250 把原来埋在 `on_cleanup_logs` 里的两圈挪成函数）。

    语义一个字不改：`data/` 走 `auto_clean_logs`（mtime 早于截止才删），
    `logs/` 同样按 mtime 扫。它管的是"很久没动的文件"，
    **不是**"用户点一下就想看到的清空"——那件事归 `clear_live_logs`。
    """
    n = auto_clean_logs(retain_days)
    n2 = 0
    import glob as _glob
    import time as _time
    cutoff = _time.time() - retain_days * 86400
    for f in _glob.glob(str(APP_DIR / 'logs' / '*')):
        try:
            if os.path.getmtime(f) < cutoff:
                os.remove(f)
                n2 += 1
        except Exception as e:
            _clean_fail_note('logs/', os.path.basename(f), e)
    return n + n2


#: ★R250 一1：手动"清理诊断日志"就地清空的活日志文件名。
#: `data/` 按这份名单点（**故意不含** `save_audit.log`——★R249 补充2 判过它是
#: 长期审计，"布局为什么被覆盖"的唯一证据链，清日志不许顺手把它抹了）；
#: `logs/` 里的都是程序自写的 `*.log` 调试件，整类一并算活日志。
_LIVE_CLEAN_TARGETS = ['app.log', 'switch.log', 'crash.log', 'stderr.log',
                       'launcher.log', 'win32_trail.log']
#: 轮转备份（`app.log.1`…）：老内容就是要甩掉的证据 ⇒ 直接删，不截零。
_LIVE_CLEAN_ROTATIONS = 3


def _live_log_files():
    """列出本次要处理的文件：`[(Path, 是否轮转件)]`。目录不存在 ⇒ 空表。"""
    pairs = []
    seen = set()

    def _add(p, rot=False):
        key = os.path.normcase(str(p))
        if key in seen or not os.path.isfile(str(p)):
            return
        seen.add(key)
        pairs.append((p, rot))

    for sub in ('data', 'logs'):
        d = APP_DIR / sub
        if not d.exists():
            continue
        bases = ([d / n for n in _LIVE_CLEAN_TARGETS] if sub == 'data'
                 else sorted(d.glob('*.log')))
        for p in bases:
            _add(p)
            for i in range(1, _LIVE_CLEAN_ROTATIONS + 1):
                _add(p.parent / ('%s.%d' % (p.name, i)), True)
    return pairs


def _truncate_via_handler(path) -> bool:
    """有活 handler 正握着这个文件 ⇒ 先关流、截零、再把流交回去（True＝已处理）。

    为什么非要绕这一道：`app.log`/`switch.log` 是**本进程自己开着句柄**在写的。
    实测两条坑（Python 3.14 本机跑出来的，见 `测试_ui/test_r250_cleanup_logs.txt`）：
      · 只调 `stream.truncate(0)` ⇒ 流的写位置留在旧末尾，下一条日志越过一排
        空洞写进去，文件没真清干净；
      · 关流之后把 `handler.stream = None` 就完事 ⇒ **3.14 的
        `StreamHandler.emit` 不会自己重开**（`stream = self.stream;
        stream.write(...)` 直接炸进 `handleError`），日志通道就此哑掉。
    所以走"关流 → 磁盘上截零 → 交回 handler 自己 `_open()` 重开"这条，
    清空后写日志的通道必须仍然活着（判据 C2 钉的就是这一点）。
    """
    target = os.path.normcase(os.path.normpath(str(path)))
    hit = False
    try:
        loggers = [logging.root] + [l for l in
                                    logging.Logger.manager.loggerDict.values()
                                    if isinstance(l, logging.Logger)]
    except Exception:
        loggers = [logging.root]
    for lg in loggers:
        for h in list(getattr(lg, 'handlers', None) or []):
            bf = getattr(h, 'baseFilename', None)
            if not bf or os.path.normcase(os.path.normpath(str(bf))) != target:
                continue
            try:
                h.acquire()
                try:
                    st = getattr(h, 'stream', None)
                    if st is not None:
                        try:
                            st.flush()
                        except Exception:
                            pass
                        try:
                            st.close()
                        except Exception:
                            pass
                        h.stream = None
                    with io.open(str(path), 'w', encoding='utf-8'):
                        pass          # 磁盘上截零（此刻没人握着句柄）
                    # 关键一步：把流**交回去**（3.14 的 emit 不会自己重开）
                    try:
                        h.stream = h._open()
                    except Exception as _oe:
                        logger.debug('[clear_logs] %s 重开写流失败: %r'
                                     % (os.path.basename(str(path)), _oe))
                    hit = True
                finally:
                    h.release()
            except Exception as e:
                logger.debug('[clear_logs] 走 handler 清空 %s 失败: %r'
                             % (os.path.basename(str(path)), e))
    return hit


def clear_live_logs() -> dict:
    """★R250 一1：**现在**就把桌面上的诊断日志倒空（用户原话「清理诊断日志
    好像没用」的正解）。

    修前为什么"没用"：`on_cleanup_logs` 只会删 mtime 早于 60 天前的文件，
    而正在写的日志 mtime 恒等于"现在" ⇒ 一个都删不掉，且 `total=0` 时连一句
    反馈都不给（详见 `测试_ui/test_r250_cleanup_logs.py` 抬头取证）。

    口径：
      · 活日志（名单里的 `data/*.log` + `logs/*.log`）**就地截零**，
        优先走各文件自己的 logging handler ⇒ 清完程序还在写、日志通道不断；
      · 轮转备份（`app.log.1`…）**删除**——那本来就是要甩掉的历史；
      · `data/save_audit.log` 一个字节都不碰（长期审计，见 `_LIVE_CLEAN_TARGETS`）；
      · 被别的进程占着删不动的 ⇒ 记进 `errors` 如实报，不再 `except: pass`
        装成"没这个文件"（★R248 那轮的教训：静默失败＝用户只能猜）。
    返回 ``{'cleared','deleted','errors','freed'}``，全部可弹窗报数。
    """
    out = {"cleared": [], "deleted": [], "errors": [], "freed": 0}
    try:
        pairs = _live_log_files()
    except Exception as e:
        logger.debug('[clear_logs] 列文件失败: %r' % e)
        return out
    for p, rot in pairs:
        try:
            size = p.stat().st_size
        except Exception:
            continue
        try:
            if rot:
                p.unlink()
                out["deleted"].append((p.name, size))
                out["freed"] += size
                continue
            if not _truncate_via_handler(p):
                with io.open(str(p), 'w', encoding='utf-8'):
                    pass
            out["cleared"].append((p.name, size))
            out["freed"] += size
        except Exception as e:
            out["errors"].append((p.name, repr(e)))
    return out


def _fmt_size(n) -> str:
    """字节数转大白话（弹窗用，别给用户看 1048576 这种数）。"""
    try:
        n = int(n)
    except Exception:
        return '0 B'
    if n < 1024:
        return i18n.tr('%d 字节' % n, '%d B' % n)
    if n < 1024 * 1024:
        return i18n.tr('%.1f KB' % (n / 1024.0), '%.1f KB' % (n / 1024.0))
    return i18n.tr('%.1f MB' % (n / 1048576.0), '%.1f MB' % (n / 1048576.0))


def on_cleanup_logs(gui):
    """手动清理诊断日志（★R250 一1 重写）。

    三步，一步都不能省：
      ① 先问一句（点名清哪几份、审计日志不动、要发给人家先看一眼就先去打包）；
      ② 过期件照旧删（`_sweep_expired_logs`，历轮语义不变）+ 当场把活日志倒空
         （`clear_live_logs`）；
      ③ **无论清没清到都弹一次结果**——0 字节也要说"本来就是空的"。
         这条是这次的核心：修前 `if total:` 才弹窗，用户点了跟没点一样。
    """
    try:
        go = messagebox.askyesno(
            i18n.tr('清理诊断日志', 'Clear Diagnostic Logs'),
            i18n.tr('要把当前的诊断日志倒空吗？\n\n'
                    '会清掉：运行日志、分类切换日志、崩溃记录、启动器日志，'
                    '以及 logs/ 里的调试日志（更早的轮转备份一并删除）。\n'
                    '不会动：记录"布局为什么被覆盖"的长期审计。\n\n'
                    '如果是要把日志发给作者排查问题，'
                    '请先点「打包日志到桌面」，再回来清理。',
                    'Clear the current diagnostic logs?\n\n'
                    'Will be cleared: run log, category-switch log, crash log, '
                    'launcher log, and the debug logs under logs/ '
                    '(older rotated backups are removed as well).\n'
                    'Not touched: the long-term audit of layout overwrites.\n\n'
                    'Investigating a problem? Use "Package Logs to Desktop" '
                    'FIRST, then come back here.'),
            parent=gui.root)
        if not go:
            gui._log('🧹 清理诊断日志：用户取消，未做任何改动')
            return
        expired = _sweep_expired_logs(LOG_RETAIN_DAYS)
        r = clear_live_logs()
        cleared = r.get('cleared') or []
        deleted = r.get('deleted') or []
        errors = r.get('errors') or []
        freed = int(r.get('freed') or 0)
        n_files = len(cleared) + len(deleted)
        gui._log('🧹 清理诊断日志：就地清空 %d 个、删除轮转备份 %d 个、'
                 '过期文件 %d 个，释放 %s%s'
                 % (len(cleared), len(deleted), expired, _fmt_size(freed),
                    ('；%d 个被占用没清成' % len(errors)) if errors else ''))
        if n_files or expired:
            msg = i18n.tr('已清空 %d 个日志文件、删除 %d 个轮转备份'
                          '（过期文件 %d 个），共释放 %s。'
                          % (len(cleared), len(deleted), expired,
                             _fmt_size(freed)),
                          'Cleared %d log file(s), removed %d rotated backup(s)'
                          ' and %d expired file(s); freed %s.'
                          % (len(cleared), len(deleted), expired,
                             _fmt_size(freed)))
        else:
            # ★ 用户点完"什么也没发生"就是这一支：以前这里干脆不弹窗
            msg = i18n.tr('诊断日志本来就是空的，没有需要清理的内容。',
                          'The diagnostic logs were already empty — '
                          'nothing to clear.')
        if errors:
            msg += '\n' + i18n.tr(
                '另有 %d 个文件正被其他程序占用，这次没清掉：关掉同时开着的'
                '另一个拾碎（或重启后再试一次）。' % len(errors),
                '%d file(s) are held by another program and were skipped: '
                'close the other running DeskBits instance (or retry after '
                'a restart).' % len(errors))
        msg += '\n' + i18n.tr('记录"布局为什么被覆盖"的长期审计没有动。',
                              'The long-term layout audit was not touched.')
        messagebox.showinfo(i18n.tr('清理完成', 'Done'), msg, parent=gui.root)
    except Exception as e:
        logger.error(f"清理日志失败: {e}")
        messagebox.showerror(i18n.tr('错误', 'Error'),
                             i18n.tr('清理日志失败：%s' % e,
                                     'Failed to clear logs: %s' % e),
                             parent=gui.root)


# ============ ★R248（用户 40 批 item5）：一键打包日志到桌面 ============
# 目的：别人（朋友/用户）遇到问题时，点一下就把日志拢成一个 zip 丢桌面，
# 连着"校验码 txt"一起发给作者。三点要求：① 一键到桌面；② 带校验码；
# ③ 若能低成本证明"这包确实出自 DeskBits"就做——做法很轻：包内塞一份
# 清单，抬头写死产品特征行（DESKBITS_DIAG_BUNDLE_V1）+ 版本号 + 每文件
# sha256 + 机器名。作者收到先看清单抬头就能认出来路，sha256 还能查篡改。
def _diag_desktop_dir():
    """拿"桌面"真实路径：OneDrive 重定向 / 非中文系统名都对，失败退回 ~/Desktop。"""
    try:
        import ctypes
        from ctypes import wintypes
        buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        # CSID_DESKTOPDIRECTORY=0x10（用户"桌面"文件夹）；0 是合并虚拟桌面，
        # 用 0x10 更稳（OneDrive 改名后仍指向真实目录）。
        if ctypes.windll.shell32.SHGetFolderPathW(
                None, 0x10, None, 0, buf) == 0 and buf.value:
            p = Path(buf.value)
            if p.exists():
                return p
    except Exception:
        logger.debug('[log_page] 桌面路径 API 失败，退回 ~/Desktop', exc_info=True)
    home = Path.home()
    for name in ('Desktop', '桌面'):
        p = home / name
        if p.exists():
            return p
    return home


def _diag_vm_dirs():
    """★R271（用户 `----2026-9-29 02----` item1「vmware 不能自动启动，你检查下
    日志」）：启动虚拟机的 bat 与它自己的运行日志所在目录。

    为什么必须进诊断包：★R241 专门让 bat 往同目录 `vm_xxx.log` 写"几点跑的、
    rc 几、重试了几次"，就是为了这类"有时候行有时候不行"的问题能有证据；
    但打包日志只扫 `data/` 与 `logs/` ⇒ 用户回传的 zip 里永远没有这份日志，
    证据链在自己手里断掉（本轮实测：13 个成员全是 app.log 家族，
    app.log 里零条任务执行痕迹——任务本来就是系统跑的，不会写进来）。
    两个目录都要收：新版固定目录 + 老版跟着程序目录的那份（升级前后的任务
    分别指向两处）。"""
    ds = []
    try:
        from gui.sys_panel import _vm_bat_dir
        ds.append(Path(_vm_bat_dir(create=False)))
    except Exception:
        logger.debug('[log_page] 取 scheduled_vms 目录失败', exc_info=True)
    legacy = APP_DIR / 'scheduled_vms'
    if legacy not in ds:
        ds.append(legacy)
    return ds


def _diag_collect_log_files():
    """收集日志正文：data/ 里（自清名单 + 只进包名单）命中的 + logs/ 全量
    + scheduled_vms/ 的 bat、运行日志与隐藏启动器（★R271 / ★R287-N22）。
    data/ 与 logs/ 只收该层文件；scheduled_vms/ 往里递归一层子目录（启动器住在
    hidden_launchers/ 里，不递归就取不到任务真身）。单个 >8 MB 跳过
    （防把巨型崩溃转储整个塞进去）。"""
    files = []
    seen = set()
    data_dir = APP_DIR / 'data'
    if data_dir.exists():
        for pat in list(_LOG_GLOB_PATTERNS) + list(_LOG_BUNDLE_ONLY):
            for f in data_dir.glob(pat + '*'):
                try:
                    if f.is_file() and f.stat().st_size <= 8 * 1024 * 1024:
                        key = str(f.resolve()).lower()
                        if key not in seen:
                            seen.add(key)
                            files.append(f)
                except OSError:
                    continue
    logs_dir = APP_DIR / 'logs'
    if logs_dir.exists():
        for f in logs_dir.glob('*'):
            try:
                if (f.is_file() and f.suffix.lower() in ('.log', '.txt')
                        and f.stat().st_size <= 8 * 1024 * 1024):
                    key = str(f.resolve()).lower()
                    if key not in seen:
                        seen.add(key)
                        files.append(f)
            except OSError:
                continue
    # ★R271：计划任务的启动 bat + bat 自己写的运行日志（★R287-N22 加上 .vbs）。
    # 目录里别的东西不动。bat 正文本身就是"任务到点到底会执行什么"的铁证；
    # hidden_launchers/*.vbs 是★R275 之后任务的**真正动作**——不收它，"vm 不启动"
    # 这类工单回传的包里就没有那份执行体（本轮实测：包里 0 个 .vbs）。
    # 子目录要递归：启动器就住在 scheduled_vms/hidden_launchers/ 里。
    for d in _diag_vm_dirs():
        try:
            if not d.exists():
                continue
            for f in d.rglob('*'):
                try:
                    if (f.is_file()
                            and f.suffix.lower() in ('.bat', '.log', '.vbs')
                            and f.stat().st_size <= 8 * 1024 * 1024):
                        key = str(f.resolve()).lower()
                        if key not in seen:
                            seen.add(key)
                            files.append(f)
                except OSError:
                    continue
        except OSError:
            continue
    return sorted(files)


def _diag_arc_name(f):
    """zip 里的归档名（按来源目录分栏，别把 bat 混进 data/）。"""
    pn = f.parent.name.lower()
    if pn == 'scheduled_vms':
        return 'vms/' + f.name
    if pn == 'hidden_launchers':
        # ★R287-N22：启动器住在 scheduled_vms/hidden_launchers/——归到 vms 下面
        # 单独一层，别掉进 data/（同名文件会互相顶掉，取证时看不出来少了一枚）
        return 'vms/hidden_launchers/' + f.name
    if 'logs' in pn:
        return 'logs/' + f.name
    return 'data/' + f.name


def _diag_target_exists(target):
    """任务动作里"要跑的东西"现在在不在 → `(判据, PATH 解析出的实址)`。

    ★R319-M7：旧写法一律 `os.path.isfile(target)`——那是拿**我们自己进程的 CWD**
    去判一个由任务计划程序解释的路径。`EXE=` 允许写裸名（`shutdown.exe`、
    `notepad.exe`），schtasks 交给系统按 `%PATH%` 找 ⇒ 系统自带的命令常年被我们
    报成死链，诊断包那一栏于是把"任务指向的东西早没了"这个最常见死因**反证**成
    "没问题"（用户 33 批挂账原话：EXISTS 栏恒假/恒真都算说谎）。
    口径分三种：绝对路径照旧问磁盘；裸名走 `shutil.which`（找到才算在，并把实址
    一并交给证据链，方便看出解析到哪）；**相对带目录**没有基准目录（`WD=` 常常
    是空串）⇒ 老实写 unknown，不硬说 False。
    """
    try:
        if os.path.isabs(target):
            return ('True' if os.path.isfile(target) else 'False'), ''
        if not os.path.dirname(target):
            found = shutil.which(target)
            return ('True' if found else 'False'), (found or '')
    except (OSError, ValueError) as e:
        logger.warning('[诊断包] 任务动作判在失败（按 unknown）: %s (%s)'
                       % (os.path.basename(target), e))
        return 'unknown', ''
    return 'unknown', ''


def _diag_action_exists_lines(lines):
    """把快照里每条 `ACTION=` **真正要跑的那个文件**判一次在不在。

    ★R287-N21：旧写法判的是 `EXE=` 那一段——★R275 之后我们的任务动作是
    `wscript.exe "…\\hidden_launchers\\x.vbs"`，`EXE=` 永远是系统自带的
    wscript.exe ⇒ EXISTS **恒真**，于是"任务指向的东西早就不在了"这个最常见
    的死因，被这一栏反过来证明成"没问题"（`_tr_exe` 当时导进来就是为解这一层，
    但从没被调用）。现在走 sys_panel 的同一个解包口径 `_tr_exe`，外壳不进证据链。
    """
    out = []
    try:
        from gui.sys_panel import _tr_exe
    except Exception as e:
        return ['action check failed: import _tr_exe: %s: %s'
                % (type(e).__name__, e)]
    for l in lines:
        if not l.startswith('ACTION='):
            continue
        for seg in l[len('ACTION='):].split(' ; '):
            if not seg.startswith('EXE='):
                continue
            _rest = seg[len('EXE='):]
            _exe, _has_arg, _tail = _rest.partition(' ARG=')
            _arg = _tail.partition(' WD=')[0] if _has_arg else ''
            _exe = _exe.strip()
            if not _exe:
                continue
            try:
                target = _tr_exe(('%s %s' % (_exe, _arg)).strip()) or _exe
            except Exception as e:
                out.append('UNWRAP FAILED: %s: %s (%s)'
                           % (type(e).__name__, e, _exe))
                continue
            note = '' if target == _exe else '  <- 隐藏启动器解包'
            verdict, resolved = _diag_target_exists(target)
            if verdict == 'unknown':
                out.append('EXISTS=unknown  %s%s（相对路径，没有基准目录判不了）'
                           % (target, note))
            elif resolved:
                out.append('EXISTS=True  %s%s  → %s' % (target, note, resolved))
            else:
                out.append('EXISTS=%s  %s%s' % (verdict, target, note))
    return out


def _diag_ascii(s):
    """诊断包要在别的代码页机器上也读得出来 ⇒ 非 ASCII 一律替成 `?`。"""
    try:
        out = str(s).encode('ascii', 'replace').decode('ascii')
    except Exception:
        out = '?'
    return ' '.join(out.split())


def _diag_shutdown_probes():
    """★R332-b：关机/重启**被系统拒了**才跑的现场探针（全程只读）。

    §卅六 三·6 记的就是这件事：R045 包里 `DeskBits_delay_shutdown_212406` 的
    "上次结果"=2147946720（0x800710E0「操作员或系统管理员拒绝了请求」），
    ★R331-c 只把码翻成人话——**为什么被拒**一个样本判不了，只能猜，而猜不算修。
    这几行是把"下一次同类码回传时想知道的东西"当场抓下来：系统原文、组策略有
    没有禁关机、这个令牌到底有没有 `SeShutdownPrivilege`、进程提没提权。
    任何一环读不动一律写 `unknown`，**不许拿 0 冒充"读到了且没有"**。
    全程只读：查注册表用 OpenKey/QueryValueEx，查令牌用 OpenProcessToken/
    GetTokenInformation——不建、不改、不删、不运行任何东西。"""
    import ctypes
    import ctypes.wintypes as wt
    import winreg

    out = []
    # 1) 组策略：Explorer 的 NoShutdown / HideShutDown（HKLM + HKCU）
    pol = []
    for root, rname in ((winreg.HKEY_LOCAL_MACHINE, 'HKLM'),
                        (winreg.HKEY_CURRENT_USER, 'HKCU')):
        for val in ('NoShutdown', 'HideShutDown'):
            try:
                k = winreg.OpenKey(
                    root, r'SOFTWARE\Microsoft\Windows\CurrentVersion'
                        r'\Policies\Explorer')
                try:
                    v, _t = winreg.QueryValueEx(k, val)
                    pol.append('%s_%s=%s' % (rname, val, v))
                except OSError:
                    pol.append('%s_%s=absent' % (rname, val))
                finally:
                    winreg.CloseKey(k)
            except Exception:
                pol.append('%s_%s=unknown' % (rname, val))
    out.append('POLICY ' + ' '.join(pol))

    # 2) 令牌：是否提权 + 有没有"关闭系统"权限（present / enabled / absent）
    tok = []
    failed = False
    _step = 'start'
    try:
        k32 = ctypes.windll.kernel32
        adv = ctypes.windll.advapi32
        # ★R204 NEW-C10 同款纪律：原型必须逐个钉死。缺省时 GetCurrentProcess()
        # 的返回值按 c_int 收，64 位下 -1 这个伪句柄被截成 0xFFFFFFFF ⇒
        # OpenProcessToken 拿到无效句柄，探针会"读不到"却没人知道为什么。
        k32.GetCurrentProcess.restype = wt.HANDLE
        adv.OpenProcessToken.argtypes = [wt.HANDLE, ctypes.c_ulong,
                                         ctypes.POINTER(wt.HANDLE)]
        adv.OpenProcessToken.restype = wt.BOOL
        adv.GetTokenInformation.argtypes = [wt.HANDLE, ctypes.c_int,
                                            ctypes.c_void_p, ctypes.c_ulong,
                                            ctypes.POINTER(ctypes.c_ulong)]
        adv.GetTokenInformation.restype = wt.BOOL
        adv.LookupPrivilegeValueW.argtypes = [ctypes.c_wchar_p,
                                              ctypes.c_wchar_p, ctypes.c_void_p]
        adv.LookupPrivilegeValueW.restype = wt.BOOL

        _step = 'OpenProcessToken'
        h = wt.HANDLE()
        if not adv.OpenProcessToken(k32.GetCurrentProcess(), 0x0008,
                                    ctypes.byref(h)):
            raise OSError(_step)

        class _ELEV(ctypes.Structure):
            _fields_ = [('TokenIsElevated', ctypes.c_ulong)]

        class _LUID(ctypes.Structure):
            _fields_ = [('LowPart', ctypes.c_ulong),
                        ('HighPart', ctypes.c_long)]

        class _LA(ctypes.Structure):
            _fields_ = [('Luid', _LUID), ('Attributes', ctypes.c_ulong)]

        class _TP(ctypes.Structure):
            _fields_ = [('PrivilegeCount', ctypes.c_ulong),
                        ('Privileges', _LA * 512)]

        got = ctypes.c_ulong(0)
        _elev = _ELEV()
        _step = 'TokenElevation'
        if not adv.GetTokenInformation(h, 20, ctypes.byref(_elev),
                                       ctypes.sizeof(_elev), ctypes.byref(got)):
            raise OSError(_step)
        tok.append('elevated=%d' % (1 if _elev.TokenIsElevated else 0))

        tp = _TP()
        _step = 'TokenPrivileges'
        if not adv.GetTokenInformation(h, 3, ctypes.byref(tp),
                                       ctypes.sizeof(tp), ctypes.byref(got)):
            raise OSError(_step)

        _step = 'LookupPrivilegeValue'
        _want = _LUID()
        if not adv.LookupPrivilegeValueW(None, 'SeShutdownPrivilege',
                                         ctypes.byref(_want)):
            tok.append('SeShutdownPrivilege=unknown')
        else:
            hit = None
            for i in range(tp.PrivilegeCount):
                p = tp.Privileges[i]
                if (p.Luid.LowPart == _want.LowPart and
                        p.Luid.HighPart == _want.HighPart):
                    hit = p
                    break
            if hit is None:
                tok.append('SeShutdownPrivilege=absent')
            else:
                tok.append('SeShutdownPrivilege=%s' % (
                    'enabled' if hit.Attributes & 0x00000002 else 'present'))
        _step = 'CloseHandle'
        # CloseHandle 是 kernel32 的导出，advapi32 里没有 ⇒ 拿 adv 去取会
        # AttributeError（实测），句柄没关掉还冤枉成"权限读不到"。
        k32.CloseHandle(h)
    except Exception as e:
        failed = True
        tok.append('stopped_at=%s(%s)' % (_step, type(e).__name__))
    # 读到了几栏就说几栏：一律冠 `unknown` 会把"权限明明读出来了、只是句柄
    # 没关掉"冒充成"什么都没读到"。
    out.append(('TOKEN ' + ('partial ' if tok else 'unknown ')
                + ' '.join(tok)) if failed else 'TOKEN ' + ' '.join(tok))
    return out


def _diag_result_msg(code):
    """★R332-b：把"上次结果"那个数交给系统自己翻译成英文原文（ASCII，进包可读）。
    读不到就写 `unknown`——不拿我们自己写的中文冒充系统原文。"""
    import ctypes
    try:
        buf = ctypes.create_unicode_buffer(512)
        n = ctypes.windll.kernel32.FormatMessageW(
            0x00001000 | 0x00000200, None, int(code) & 0xFFFFFFFF,
            0x0409, buf, 512, None)
        if not n:
            return 'unknown'
        return _diag_ascii(buf.value) or 'unknown'
    except Exception:
        return 'unknown'


# ★R332-b 复评扫出来的口径错：「上次结果不为 0」≠「这次关机被系统拒了」。
# 267009=正在运行、267010=已停用、267011=尚未运行（任务建好从没跑过＝最常见形态），
# 按旧写法每台建过延时关机任务、还没到点的机器，诊断包都会凭空多一段"关机失败"
# 并跑一遍探针——正好把"正常包不涨"的承诺说反。尺子跟界面"标橙"用同一把
# （gui/sys_panel.py 的 `n not in (0, 267009, 267010, 267011)`），别两处各定一套。
_TASK_STATE_CODES = (267009, 267010, 267011)


def _diag_shutdown_refusal_lines(lines):
    """★R332-b：只在"动作是 shutdown.exe /s|/r **且**上次结果真是失败"时补一段探针。
    认不出这类记录 ⇒ 一行都不加（正常诊断包不涨）。"""
    recs = []
    cur = None
    for l in lines:
        if l.startswith('NAME='):
            if cur is not None:
                recs.append(cur)
            cur = {'name': l[5:].strip(), 'action': '', 'result': None}
        elif cur is None:
            continue
        elif l.startswith('ACTION='):
            cur['action'] = l[7:].lower()
        elif l.startswith('LAST='):
            i = l.find('RESULT=')
            if i >= 0:
                tok = l[i + 7:].split()
                try:
                    cur['result'] = int(tok[0]) if tok else None
                except ValueError:
                    cur['result'] = None
    if cur is not None:
        recs.append(cur)
    bad = [r for r in recs
           if 'shutdown.exe' in r['action']
           and ('/s' in r['action'] or '/r' in r['action'])
           and r['result'] not in (None, 0)
           and r['result'] not in _TASK_STATE_CODES]
    if not bad:
        return []
    out = ['', '== shutdown refusal probe (read-only; a DeskBits shutdown/'
               'reboot task returned a failure Last Task Result; 0 and the '
               'task-state codes 267009/267010/267011 are excluded) ==']
    out.append('SHUTDOWN_TASK_FAILED=%d' % len(bad))
    for r in bad[:4]:
        out.append('FAILED_TASK=%s RESULT=%d MSG=%s'
                   % (_diag_ascii(r['name']), r['result'],
                      _diag_result_msg(r['result'])))
    out += _diag_shutdown_probes()
    return out


def _diag_tasks_snapshot():
    """★R271：只读抓一份 DeskBits_* 计划任务的现场快照（进诊断包）。

    要的就是这几栏——本轮判"vmware 为什么不自动启动"缺的正是它们：
    触发器类型（开机/登录）、`Principal.LogonType`（Interactive 的任务在
    开机那一刻没有用户会话 ⇒ 起不来）、`StartWhenAvailable`（错过补跑，
    不开就永远不会补）、电池两条、动作的绝对路径 + **该路径现在还在不在**、
    上次运行时间与返回码。全程只读（Get-ScheduledTask / Get-ScheduledTaskInfo），
    不建、不改、不删、不运行任何任务。抓不到就写清"抓不到"，不拿空文件冒充。"""
    import subprocess
    ps = (
        "$ErrorActionPreference='SilentlyContinue'; "
        "Get-ScheduledTask | Where-Object { $_.TaskName -like 'DeskBits*' } | "
        "ForEach-Object { "
        "$i = $_ | Get-ScheduledTaskInfo; "
        "$a = ($_.Actions | ForEach-Object { "
        "'EXE=' + $_.Execute + ' ARG=' + $_.Arguments + "
        "' WD=' + $_.WorkingDirectory }) -join ' ; '; "
        "$t = ($_.Triggers | ForEach-Object { $_.CimClass.CimClassName "
        "+ ' delay=' + $_.Delay + ' enabled=' + $_.Enabled }) -join ' ; '; "
        "'---'; 'NAME=' + $_.TaskPath + $_.TaskName; "
        "'STATE=' + $_.State; "
        "'PRINCIPAL=' + $_.Principal.UserId + ' logon=' "
        "+ $_.Principal.LogonType + ' runlevel=' + $_.Principal.RunLevel; "
        "'TRIGGER=' + $t; "
        "'SETTINGS StartWhenAvailable=' + $_.Settings.StartWhenAvailable "
        "+ ' DisallowStartIfOnBatteries=' "
        "+ $_.Settings.DisallowStartIfOnBatteries "
        "+ ' StopIfGoingOnBatteries=' + $_.Settings.StopIfGoingOnBatteries "
        "+ ' ExecutionTimeLimit=' + $_.Settings.ExecutionTimeLimit; "
        "'ACTION=' + $a; "
        "'LAST=' + $i.LastRunTime + ' RESULT=' + $i.LastTaskResult "
        "+ ' NEXT=' + $i.NextRunTime }")
    lines = ['DESKBITS_TASKS_SNAPSHOT_V1',
             'generated: %s' % datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
             'scope    : TaskName -like DeskBits*  (read-only query)',
             '']
    _rc = None
    try:
        p = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-Command', ps],
            capture_output=True, timeout=60, stdin=subprocess.DEVNULL,
            creationflags=0x08000000)
        txt = ''
        for enc in ('utf-8', 'gbk'):     # ★R230c：重定向落盘/管道可能是 GBK
            try:
                txt = p.stdout.decode(enc)
                break
            except Exception:
                txt = p.stdout.decode('utf-8', errors='replace')
        _rc = p.returncode
        lines.append('rc=%d' % p.returncode)
        if p.returncode != 0:
            err = p.stderr.decode('gbk', errors='replace').strip()
            lines.append('STDERR: %s' % (err or '(empty)'))
        lines += [l for l in txt.splitlines() if l.strip()]
    except Exception as e:
        lines.append('SNAPSHOT FAILED: %s: %s' % (type(e).__name__, e))
    # ★R331（未完成清单 §卅五 第 5 条）：零任务时原来只落 `rc=0` + 两行表头，
    # 一条记录都没有 ⇒ 用户回传的诊断包里"这台机器确实没建过任务"和"查询崩了但
    # 没报错"长得一模一样，正好打在本函数 docstring 自己写的承诺上（"抓不到就写清
    # '抓不到'，不拿空文件冒充"）。三枚标记各管一件事：查到几条 / 确实为零 / 没查成。
    _n_rec = sum(1 for l in lines if l.startswith('NAME='))
    if _n_rec:
        lines.append('TASK_RECORDS=%d' % _n_rec)
    elif _rc == 0:
        lines.append('NO_DESKBITS_TASKS=0  (query completed with rc=0 and this '
                     'machine has no DeskBits* task -- NOT a capture failure)')
    else:
        lines.append('TASKS_UNCONFIRMED=1  (query did not complete: rc=%s; the '
                     'empty listing below does NOT mean "no tasks")' % (_rc,))
    # 动作路径现在还在不在——这一栏是把"静默什么都不做"变成可见的关键
    lines.append('')
    lines.append('== action file existence ==')
    lines += _diag_action_exists_lines(lines)
    # ★R332-b：只有"关机/重启动作 + 上次结果不为 0"才多这一段（正常包不涨）
    lines += _diag_shutdown_refusal_lines(lines)
    return '\n'.join(lines) + '\n'


# ================= 任务快照的后台缓存（★R333-d） =================
# 实测 `_diag_tasks_snapshot()` 单次 **7.90 秒**（PowerShell 冷启，上限
# `timeout=60`），旧写法把它压在那枚按钮的同步链里 ⇒ 点「📦 打包日志到桌面」
# 主线程连界面带悬浮条一起冻死最长一分钟——而这恰恰是用户排障时要点的一枚
# （★R230 界面假死同族）。这一份只是取证快照，**晚几分钟生成不影响诊断**：
# 快照第一行本来就写着自己的生成时刻，取缓存不撒谎。所以改成开机后台预热 +
# 点击时取缓存，慢探针一律离开主线程。
_SNAP_TTL = 600.0
_snap = {'text': None, 'ts': 0.0}
_snap_lock = threading.Lock()


def _snap_refresh():
    """后台线程里跑慢探针，成功才记账（失败留一行 warning，不许静默）。"""
    try:
        text = _diag_tasks_snapshot()
    except Exception as e:
        logger.warning('[诊断包] 后台任务快照没跑成（%s）⇒ 下次点「打包日志」'
                       '会当场补一次', e)
        return
    with _snap_lock:
        _snap['text'] = text
        _snap['ts'] = time.time()


def prewarm_tasks_snapshot(force=False):
    """起一个 daemon 线程去补快照；缓存还热且没点名要新的就不重复起。"""
    with _snap_lock:
        fresh = _snap['text'] is not None and (time.time() - _snap['ts']) < _SNAP_TTL
    if fresh and not force:
        return False
    threading.Thread(target=_snap_refresh, daemon=True,
                     name='TasksSnapshotPrewarm').start()
    return True


def tasks_snapshot_text():
    """给诊断包用的那份文本：**有热的就用，绝不为主线程等探针**。
    取过一次就顺手在后台补一份，下次点击仍是热的；只有冷启动第一下（缓存
    从没成功过）才当场同步跑一次——那一下本来也躲不掉，但至少日志留痕。"""
    with _snap_lock:
        text, ts = _snap['text'], _snap['ts']
    if text is not None and (time.time() - ts) < _SNAP_TTL:
        prewarm_tasks_snapshot(force=True)
        return text
    if text is None:
        logger.warning('[诊断包] 任务快照还没预热好（启动线程没跑成或还没跑完），'
                       '这一次当场跑（界面可能卡几秒）——之后就取缓存了')
    return _diag_tasks_snapshot()


def _diag_version():
    """版本号只从 version.txt 读（单一来源，和标题栏一致）；读不到给 unknown。"""
    try:
        v = (APP_DIR / 'version.txt').read_text(encoding='utf-8').strip()
        return v or 'unknown'
    except Exception:
        return 'unknown'


def _diag_box_state():
    """★R320（用户 P0「图标丢失」）：把**两本盒账本 + 盒根清单**收进诊断包。

    这次 win11 那份日志包里两本账一枚都没有、盒根里躺着什么也看不见，所以只能靠
    用户口述+复现猜。账本原件照抄（读不到就写一行"没有这份账本"——**缺席本身就是
    证据**：换了新版本目录 ⇒ exe 旁边的账本是空的），盒根清单走
    `visibility_router.audit_box_root()`（以 os.listdir 为准，逐只盒标登记/未登记、
    可认领/没被认领）。返回 [(归档名, bytes)]，**本身不抛**。
    """
    out = []
    try:
        from core import desktop_box as _db
        from core import visibility_router as _vr
        for arc, p in (('账本_hidden_box.json', _vr.ledger_path()),
                       ('账本_desktop_box.json', _db.manifest_path())):
            try:
                with io.open(p, 'rb') as fh:
                    out.append((arc, fh.read()))
            except FileNotFoundError:
                out.append((arc, (u'（这份账本不存在：%s）\n'
                                  u'（换了新版本目录就会这样——盒子根在家目录里活着，'
                                  u'账本却写在 exe 旁边，于是盒里的图标"盘上有、账上没写"）'
                                  % p).encode('utf-8')))
            except Exception as e:
                out.append((arc, (u'（这份账本读不动：%s）' % e).encode('utf-8')))
        aud = _vr.audit_box_root()
        lines = [u'盒根清单（★R320 取证，判据=os.listdir，不读账本自述）',
                 u'盒根：%s' % aud.get('root'),
                 u'账本读不动：%s' % bool(aud.get('ledger_failed')),
                 u'可认领（分类名单点过名）：%d 枚' % int(aud.get('claimable_total') or 0),
                 u'没被认领：%d 枚' % int(aud.get('stray_total') or 0), u'']
        for b in (aud.get('boxes') or []):
            lines.append(u'[%.4s] %s ｜ 登记=%s 本系统的盒=%s ｜ 里面 %d 枚'
                         % (u'可散回' if b.get('registered') else
                            (u'待认领' if b.get('ours') else u'不相干'),
                            b.get('box'), bool(b.get('registered')),
                            bool(b.get('ours')), len(b.get('entries') or [])))
            for f in (b.get('entries') or []):
                lines.append(u'   %s %s' % (u'↩' if f in (b.get('claimable') or [])
                                            else u'·', f))
        blob = ('\n'.join(lines) + u'\n').encode('utf-8')
        out.append(('盒根清单.txt', blob))
    except Exception as e:
        logger.warning('[诊断包] 盒状态收集失败（只少这一份，其余照打包）: %s', e)
    return out


def on_package_logs(gui):
    """把日志打包成一个 zip 放桌面，附一份校验码 txt。全程兜异常、必弹结果。"""
    import zipfile
    import hashlib
    import platform
    import getpass
    zip_path = None      # 本次自己建的那个 zip（异常时按它清理半截包）
    made = False         # 真开过 zip 文件了才允许删
    done = False         # 走到"校验码已落地"才算成
    try:
        files = _diag_collect_log_files()
        if not files:
            messagebox.showinfo(
                i18n.tr('打包日志', 'Package logs'),
                i18n.tr('没找到可打包的日志文件（data/ 与 logs/ 都是空的）',
                        'No log files were found to package (data/ and logs/ '
                        'are both empty).'),
                parent=gui.root)
            return
        ver = _diag_version()
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        desk = _diag_desktop_dir()
        base = 'DeskBits_日志_%s_%s' % (ver, stamp)
        zip_path = desk / (base + '.zip')
        chk_path = desk / (base + '_校验码.txt')

        def _sha256(path):
            h = hashlib.sha256()
            with open(path, 'rb') as fh:
                for chunk in iter(lambda: fh.read(65536), b''):
                    h.update(chunk)
            return h.hexdigest()

        # 清单**最后**写：只列真进了 zip 的那些（★R248 S6 自查：读不开的文件
        # 会被跳过，清单里再留着它的 sha256 就是假证据）。
        head_lines = [
            'DESKBITS_DIAG_BUNDLE_V1',
            'product  : DeskBits（拾碎·懒人桌面工具箱）',
            'version  : %s' % ver,
            'built    : %s' % stamp,
            'machine  : %s' % getpass.getuser(),
            'host     : %s' % platform.node(),
            'os       : %s' % platform.platform(),
        ]
        packed = []     # (sha256, size, arc) —— 与 zip 内容一一对应
        skipped = []    # 收集到了但没塞进去的（正被占用 / 已被删 / 读失败）
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            made = True
            for f in files:
                try:
                    digest = _sha256(f)
                    size = f.stat().st_size
                except OSError as e:
                    skipped.append('%s（%s）' % (f.name, type(e).__name__))
                    continue
                arc = _diag_arc_name(f)
                try:
                    zf.write(f, arc)
                except OSError as e:
                    # 读得出来却写不进去：同样只当没打进，绝不写进清单
                    skipped.append('%s（%s）' % (f.name, type(e).__name__))
                    continue
                packed.append((digest, size, arc))
            # ★R271：计划任务现场快照（只读）。app.log 里永远不会有"任务到点
            # 跑没跑"的记录——任务是系统跑的，证据只在任务自己的属性和
            # bat 的运行日志里。这份快照就是把那一栏补上。
            # ★R333-d：探针本身 7.9 秒 ⇒ 这里取的是**后台预热好的那份**，
            # 主线程不再等它（快照第一行写着自己的生成时刻，不是假新）。
            try:
                snap = tasks_snapshot_text().encode('utf-8')
                zf.writestr('tasks_snapshot.txt', snap)
                packed.append((hashlib.sha256(snap).hexdigest(), len(snap),
                               'tasks_snapshot.txt'))
            except Exception as e:
                skipped.append('tasks_snapshot.txt（%s）' % type(e).__name__)
                logger.debug('[log_page] 任务快照生成失败', exc_info=True)
            # ★R320：两本盒账本 + 盒根清单（原件按字节抄，缺席也留一行说明）
            for _arc, _blob in _diag_box_state():
                try:
                    zf.writestr(_arc, _blob)
                    packed.append((hashlib.sha256(_blob).hexdigest(), len(_blob),
                                   _arc))
                except Exception as e:
                    skipped.append('%s（%s）' % (_arc, type(e).__name__))
            body = head_lines + ['files    : %d' % len(packed), '',
                                 'sha256  size  name']
            body += ['%s  %d  %s' % row for row in packed]
            if skipped:
                body += ['', 'skipped  : %d' % len(skipped)]
                body += ['  - %s' % s for s in skipped]
            zf.writestr('_来源与校验_README.txt', '\n'.join(body) + '\n')
        # 桌面旁再放一份 zip 自身的 sha256，方便对方只发文件、作者先验包没坏。
        zip_digest = _sha256(zip_path)
        head = ('这串是同名 zip 的 SHA-256，用来确认压缩包没被改坏。\n'
                'This SHA-256 is for the zip of the same name.\n\n')
        chk_path.write_text(
            head + '%s  %s\n' % (zip_digest, zip_path.name),
            encoding='utf-8')
        done = True
        # 日志行用中文（★R229 口径：源码中文，English 由 log_i18n 上屏前改写）。
        gui._log('📦 日志已打包到桌面：%s（%d 个文件）'
                 % (zip_path.name, len(packed)))
        gui._log('📦 校验码：%s' % chk_path.name)
        _miss_zh = _en = ''
        if skipped:
            # 说人话：正被别的程序占着的日志（例：VMware 抓着它的 .log 不放）
            # 会走到这里。**不藏**——不然对方以为发全了，作者也白等一轮。
            _miss_zh = ('\n\n注意：有 %d 个日志没能打包（多半正被别的程序占用'
                        '或已被删除）：%s' % (len(skipped), '、'.join(skipped)))
            _en = ('\n\nNote: %d log file(s) were not included (most likely '
                   'locked by another program or already deleted): %s'
                   % (len(skipped), ', '.join(skipped)))
            gui._log('📦 有 %d 个日志没能打包（可能被其他程序占用或已删除）：%s'
                     % (len(skipped), '、'.join(skipped)))
        messagebox.showinfo(
            i18n.tr('打包完成', 'Done'),
            i18n.tr('已打包到桌面：\n%s\n%s\n\n把 .zip 发给作者即可'
                    '（校验码 txt 可一并发，用来证明包没被改坏）。\n'
                    '共 %d 个日志文件。%s'
                    % (zip_path.name, chk_path.name, len(packed), _miss_zh),
                    'Saved to the desktop:\n%s\n%s\n\nSend the .zip to the '
                    'author (the checksum txt proves the archive was not '
                    'tampered with). %d log files included.%s'
                    % (zip_path.name, chk_path.name, len(packed), _en)),
            parent=gui.root)
    except Exception as e:
        # 半截包不许留在桌面上骗人：只删**本次自己刚建的那个**路径。
        if made and not done:
            try:
                zip_path.unlink()
            except Exception:
                logger.debug('[log_page] 半截 zip 清理失败', exc_info=True)
        logger.error(f'打包日志失败: {e}', exc_info=True)
        messagebox.showerror(
            i18n.tr('打包失败', 'Failed'),
            i18n.tr('打包日志失败：%s' % e, 'Failed to package logs: %s' % e),
            parent=gui.root)


def apply_verbose_log(gui, on=None, persist: bool = False,
                      notify: bool = True) -> bool:
    """★R250 一5⑤：把「🔊 全量日志（排障）」这一项**两处一起**落地。

    用户 42 批 ⑤ 原话：「记得支持日志的选项支持导入导出，刚才我实验了半天，
    再去看全量日志居然没勾上，之前导出的时候是勾上的。」
    定性（`run/r250/范围_r250.md` item5⑤）：导出端**没毛病**——`verbose_log`
    就在 `app_settings` 里，整包跟着走（`data_manage_page:726` 只剔 `_` 前缀）；
    坏在导入端"落盘之后没人应用"这一族（★R240 #137 同一病根的另一枚）：
      ① **运行态**：root logger 与 `app.log` handler 的级别没人按新值切
        ⇒ 设置里是 True，DEBUG 却不落盘（用户"实验了半天"没证据）；
      ② **界面态**：那枚勾选框的值挂在日志页构建期的局部 `verbose_var` 上，
        导入流程不会回头刷新它 ⇒ 用户看到的还是"没勾上"。
    两处必须一起推：只做①，用户点一下勾选框就被旧控件值覆盖回去
    （★R240 纪律 3 里 sticky 网格踩过的坑）；只做②，看着勾上了其实没生效。

    口径：
      · `on=None` ⇒ 以 `_app_settings['verbose_log']` 现值为准（导入路径就这么用）；
      · `persist=True` 才回写设置并落盘（勾选框自己用；导入端已存过盘，
        这里再存会把导入侧的校验绕开 ⇒ 默认 **False**）；
      · 日志页还没构建（没有 `gui._verbose_log_var`）⇒ 只推运行态，不报错；
      · 源码日志保持中文（★R229 方案 A），弹窗文案才走 `i18n.tr`。
    返回最终状态（True=全量日志开）。
    """
    s = getattr(gui, '_app_settings', None)
    if on is None:
        on = bool((s or {}).get('verbose_log'))
    on = bool(on)
    if persist and isinstance(s, dict):
        s['verbose_log'] = on
        try:
            gui._save_app_settings()
        except Exception as e:
            logger.debug('[log_page] verbose_log 落盘失败: %r' % e)
    try:
        logging.root.setLevel(logging.DEBUG if on else logging.INFO)
        for _h in list(logging.root.handlers):
            if str(getattr(_h, 'baseFilename', '')).lower().endswith('app.log'):
                _h.setLevel(logging.DEBUG if on else logging.INFO)
    except Exception as e:
        logger.debug('[log_page] verbose_log 应用运行态失败: %r' % e)
    var = getattr(gui, '_verbose_log_var', None)
    if var is not None:
        try:
            var.set(on)
        except Exception as e:
            logger.debug('[log_page] 全量日志勾选框回填失败: %r' % e)
    if notify:
        try:
            gui._log('🔊 全量日志已' + ('开启：DEBUG 明细落盘 app.log（复现问题后'
                                        '请关闭并回传 app.log）' if on else '关闭'))
        except Exception:
            pass
    return on


def build_log_tab(tab_log, gui, colors):
        # ============ 📜 日志 ============
        log_frame = ttk.LabelFrame(tab_log, text=i18n.tr('📋 运行日志', '📋 Run Log'),
                                   padding='10', style='Card.TLabelframe')
        log_frame.pack(fill=tk.BOTH, expand=True)

        gui.log_text = tk.Text(log_frame, height=6,
                                font=('Consolas', 9),
                                bg=colors['surface'],
                                fg=colors['fg'],
                                relief=tk.FLAT,
                                highlightthickness=0)
        log_scrollbar = ttk.Scrollbar(log_frame, orient=tk.VERTICAL,
                                      command=gui.log_text.yview)
        gui.log_text.configure(yscrollcommand=log_scrollbar.set)

        gui.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5, pady=5)
        log_scrollbar.pack(side=tk.RIGHT, fill=tk.Y, pady=5)

        log_btns = ttk.Frame(tab_log)
        log_btns.pack(fill=tk.X, pady=(SP['s'], 0))

        # ★ R156：排障全量日志开关搬进界面（R154 版要改配置文件，exe 用户
        # 用不了）——勾选即写 app_settings 并实时生效，无需重启；用户报错
        # 时让用户勾上它复现，证据齐全，平时保持关。
        # ★R250 一5⑤：应用逻辑抽成模块级 `apply_verbose_log`（导入端也要走同
        # 一条路，不再两份实现），勾选框的 var 挂到 `gui._verbose_log_var`
        # ⇒ 导入完成后能回填，用户不会再看成"没勾上"。
        def _toggle_verbose():
            apply_verbose_log(gui, on=bool(verbose_var.get()),
                              persist=True, notify=True)

        verbose_var = tk.BooleanVar(value=bool(gui._app_settings.get('verbose_log')))
        gui._verbose_log_var = verbose_var     # ★R250 一5⑤：给导入路径留回填入口
        # ★R216 阶段 2b：两个操作走同一条 action_bar 右对齐、等宽（原先一枚
        # pack(RIGHT) 一枚 pack(RIGHT, padx) ⇒ 宽度随文字、和左边的路径长句
        # 挤在同一排）；路径说明下移独立一行，不再被按钮截断
        ttk.Checkbutton(log_btns, text=i18n.tr('🔊 全量日志（排障）',
                                               '🔊 Verbose log'),
                        variable=verbose_var, command=_toggle_verbose
                        ).pack(side=tk.RIGHT)
        ui_layout.action_bar(log_btns, [
            (i18n.tr('📦 打包日志到桌面', '📦 Package Logs to Desktop'),
             lambda: on_package_logs(gui), 'secondary.TButton'),
            (i18n.tr('🧹 清理诊断日志', '🧹 Clear Diagnostic Logs'),
             gui._on_cleanup_logs, 'secondary.TButton')],
            align='right', equal=False)
        # ★R336-f1：这行原来还挂着「logs/frame_debug.log（方框）」。真机 R050 日志包里
        # 那三份 `logs/*.log` 恒 0 字节，成因分两种（实测剔掉 utils/logger.py 自身与
        # 注释行：`s5_r336f1_facade_counts.txt`）：
        # `log_error`/`log_debug`/`log_warning`/`log_frame_action`/`print_frame_info`
        # 五个便捷函数在产品树内**零调用者** ⇒ `debug.log`/`error.log` 空；而
        # `log_frame_debug` 有 9 处调用（全在 `core/highlighter.py`，另有
        # `print_alignment_info` 2 处转调）却一律走 DEBUG，被 `utils/logger.py:72`
        # 的 INFO 文件门槛 + `:86 propagate=False` 挡在落盘之外 ⇒ `frame_debug.log` 空。
        # 指给用户一个"永远空"的证据文件，等于让他白打包、
        # 也让我们白读一遍（本批体检真为此花了一轮）。方框相关的行**确实有**，
        # 但都在 `data/app.log`（`[框] …` 那些），照实写。
        ttk.Label(log_btns,
                  text=i18n.tr('详细日志: data/app.log（含方框/收纳/监视全部行）'
                               ' · data/switch.log（切换） · data/crash.log（崩溃）',
                               'Detailed logs: data/app.log (frames, boxes, monitor — '
                               'everything) · data/switch.log (switches)'
                               ' · data/crash.log (crashes)'),
                  foreground=colors['text_secondary'], wraplength=980,
                  justify='left',
                  font=ui_layout.app_font(9)).pack(side=tk.LEFT,
                                                       pady=(SP['xs'], 0))


