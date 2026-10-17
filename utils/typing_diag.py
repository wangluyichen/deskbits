# -*- coding: utf-8 -*-
"""★R232（用户 06 批 item1）：「打字不上」诊断采样器 —— **只读、不修、可关**。

用户原话："切换分类，然后切换到全部，才出现打字不上的情况，你把程序最小化后，
就能正常打字了……不是每次都出现，想办法做个日志记录，下次遇到这种情况，找找原因？"

现场不可复现 ⇒ 按可复现门不进修复队列（★R229 的 `bounce_alt`/启动巡检已经到位），
本模块只负责**下次发生时留下能归因的证据**。要分清的两个假设：

  H1 修饰键被留在"按下"态（Alt 卡住 ⇒ 别的应用把每个键都当 Alt+键，
     记事本表现为敲不进字母、退格变 Alt+退格删词）。判据：**某修饰键连续
     按下超过 `HOLD_MS`**——正常按键几百毫秒内必抬，卡住的是不会自己抬的。
  H2 前台归属被隐形拿走（"最小化就好"强烈指向这条）。判据：**在事件窗内
     （切分类/开文件夹后 `EVENT_WINDOW_S` 秒）前台窗口发生跃变**——
     记录"从谁的窗口切到谁的窗口"，含 pid、类名、标题。

红线（★R232 定死，门禁 B3b/B3c/B7 钉住）：
  · **纯观察者**：不产生任何输入事件、不装消息钩子、不抢前台、不改焦点——
    注入修饰键的唯一出口仍是 `utils/keystate.bounce_alt()`，本模块不碰；
  · **采样线程不碰 Tk**（Tcl 跨线程会静默 native abort）：最小化态由主线程
    通过 `set_ui_state()` 写标志，线程只读标志 + `IsIconic` 交叉核对；
  · `core/dock.py` 的窗口过程保持纯 GDI，一个字都不动；
  · 日志频次三重封顶：只在**状态跃变/超阈值**时记 + 同一状态 `DEDUPE_S` 内
    不重复 + 每小时 `CAP_PER_HOUR` 条封顶。开关 `typing_diag_log` 可一键关。

输出走 `desktop_manager` logger ⇒ `data/app.log`（2MB×2 轮转，中文原样）。
"""
import ctypes
import logging
import os
import threading
import time

logger = logging.getLogger('desktop_manager')

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# ---- 阈值（都被门禁测试按名引用，改值要一起看 test_r232_typing_diag）----
POLL_MS = 250                 # 采样间隔：2 个 ctypes 读，开销可忽略
HOLD_MS = 1200                # 修饰键连续按下多久算"可疑卡住"
EVENT_WINDOW_S = 5.0          # 事件（切分类/开文件夹）后多久内的跃变才算线索
DEDUPE_S = 60.0               # 同一状态多久内不重复记
CAP_PER_HOUR = 20             # 每小时最多写多少条诊断记录
_MIN_INTERVAL = 0.2           # 事件窗内两次跃变的最小间隔（防同一帧重复）

MODIFIERS = (('Alt', 0x12), ('Ctrl', 0x11), ('Shift', 0x10))
VK_MENU, VK_LWIN, VK_RWIN = 0x12, 0x5B, 0x5C

_lock = threading.Lock()
_thread = None
_stop = None
_ui = {'minimized': False, 'hwnd': 0}
_ev = {'tag': '', 'at': None}
_st = {'prev': None, 'held_since': None, 'last_report': {}, 'win_start': 0.0,
       'in_window': 0}


def _down(vk):
    """该虚拟键此刻是否处于"按下"（只用 0x8000 位，理由见 utils/keystate）。"""
    try:
        return bool(user32.GetAsyncKeyState(vk) & 0x8000)
    except Exception:
        return False


def _text(buf, size, getter, hwnd):
    try:
        getter(hwnd, buf, size)
        return str(buf.value or '')
    except Exception:
        return ''


def _mods():
    return tuple(name for name, vk in MODIFIERS if _down(vk))


def _instance_count():
    """同名 exe 正在跑几份。只在**要落一条记录时**才查（Toolhelp 快照不便宜）。

    实测取证（2026-09-24 09:55）：用户机器上就有 2 份 0.98 同时开着——
    双实例对"热键注册/前台归属/写盘"都是重要背景，所以进证据字段。
    """
    try:
        me = os.path.basename(getattr(__import__('sys'), 'executable', '') or '')

        class _PE(ctypes.Structure):
            _fields_ = [('dwSize', ctypes.c_ulong), ('cntUsage', ctypes.c_ulong),
                        ('th32ProcessID', ctypes.c_ulong),
                        ('th32DefaultHeapID', ctypes.c_size_t),
                        ('th32ModuleID', ctypes.c_ulong), ('cntThreads', ctypes.c_ulong),
                        ('th32ParentProcessID', ctypes.c_ulong),
                        ('pcPriClassBase', ctypes.c_long), ('dwFlags', ctypes.c_ulong),
                        ('szExeFile', ctypes.c_wchar * 260)]

        snap = kernel32.CreateToolhelp32Snapshot(2, 0)
        if snap == -1 or not snap:
            return 0
        n, ent = 0, _PE()
        ent.dwSize = ctypes.sizeof(_PE)
        try:
            ok = kernel32.Process32FirstW(snap, ctypes.byref(ent))
            while ok:
                if me and str(ent.szExeFile).lower() == me.lower():
                    n += 1
                ent.dwSize = ctypes.sizeof(_PE)
                ok = kernel32.Process32NextW(snap, ctypes.byref(ent))
        finally:
            kernel32.CloseHandle(snap)
        return n
    except Exception:
        return 0


def snapshot():
    """当前这一次"谁在前台 + 修饰键 + 本程序状态"的只读快照。"""
    hwnd = user32.GetForegroundWindow() or 0
    pid = ctypes.c_ulong(0)
    if hwnd:
        try:
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        except Exception:
            pid = ctypes.c_ulong(0)
    cls = _text(ctypes.create_unicode_buffer(256), 256,
                user32.GetClassNameW, hwnd) if hwnd else ''
    title = _text(ctypes.create_unicode_buffer(512), 512,
                  user32.GetWindowTextW, hwnd) if hwnd else ''
    our_hwnd = _ui.get('hwnd') or 0
    iconic = None
    if our_hwnd:
        try:
            iconic = bool(user32.IsIconic(our_hwnd))     # 交叉核对主线程标志
        except Exception:
            iconic = None
    return {'ts': time.time(), 'hwnd': hwnd, 'fg_pid': int(pid.value or 0),
            'fg_class': cls or '?', 'fg_title': title[:60], 'mods': _mods(),
            'our_pid': os.getpid(), 'our_minimized': bool(_ui.get('minimized')),
            'iconic': iconic}


def _ui_word(cur):
    """本程序在这条记录里的状态词（ASCII 取值，English 档不混拼中文）。"""
    if cur.get('fg_pid') and cur.get('fg_pid') == cur.get('our_pid'):
        return 'fg'
    return 'min' if cur.get('our_minimized') else 'bg'


def _owner_word(cur):
    return '本程序' if (cur.get('fg_pid') and
                        cur.get('fg_pid') == cur.get('our_pid')) else '别的应用'


def report(prev, cur, now, held_since=None, event_tag='', event_at=None,
           last_report=None, reports_in_window=0):
    """纯函数：给定前后两次快照 ⇒ 该记哪几条（返回文本表，不碰线程/日志）。

    拆成纯函数是为了**能脱离采样线程做判据**（历轮教训：判据绑线程＝测不到）。
    """
    out = []
    last_report = last_report if last_report is not None else {}
    mods = tuple(cur.get('mods') or ())
    # H1：修饰键持续按下超阈值
    if mods and held_since is not None:
        dur_ms = (now - held_since) * 1000.0
        if dur_ms >= HOLD_MS:
            key = 'held:' + '+'.join(sorted(mods))
            if now - last_report.get(key, -1e18) >= DEDUPE_S:
                out.append(
                    f'⌨ 诊断｜修饰键持续按下 {"+".join(mods)} {dur_ms:.0f}ms'
                    f'｜前台={cur.get("fg_class")} pid={cur.get("fg_pid")}'
                    f'｜前台归属={_owner_word(cur)}'
                    f'｜本程序={_ui_word(cur)} 实例={cur.get("our_instances")}')
                last_report[key] = now
    # H2：事件窗内的前台跃变
    if (event_tag and event_at is not None and prev
            and (now - event_at) <= EVENT_WINDOW_S
            and cur.get('hwnd')
            and (prev.get('hwnd'), prev.get('fg_pid')) !=
                (cur.get('hwnd'), cur.get('fg_pid'))
            and now - last_report.get('ev', -1e18) >= _MIN_INTERVAL):
        out.append(
            f'🔎 诊断｜{event_tag} 后前台切换：'
            f'{prev.get("fg_class")}(pid={prev.get("fg_pid")}) → '
            f'{cur.get("fg_class")}(pid={cur.get("fg_pid")})'
            f'｜前台归属={_owner_word(cur)}'
            f'｜本程序={_ui_word(cur)} 实例={cur.get("our_instances")}')
        last_report['ev'] = now
    if not out:
        return []
    # 每小时封顶：到量后只留一条"封顶"提示，不再逐条刷
    if reports_in_window >= CAP_PER_HOUR:
        # ★R232（S6 验收 P2）：旧写法只 `last_report.get('capped')` 读、从不写
        #   ⇒ 死守卫，同小时内每换一种按键形态（Alt→Ctrl）都会再提示一次。
        #   用**时间戳**而不是布尔：last_report 只在 start() 清空，写死 True 会
        #   把提示永久静默（下一小时该提醒时反而不提醒）。
        if now - last_report.get('capped', -1e18) < 3600.0:
            return []
        last_report['capped'] = now
        return ['📛 诊断记录已达每小时上限，本小时同类记录不再写入']
    return out


def _loop(stop_ev):
    global _st
    while not stop_ev.is_set():
        stop_ev.wait(POLL_MS / 1000.0)
        if stop_ev.is_set():
            return
        try:
            now = time.time()
            cur = snapshot()
            prev = _st['prev']
            mods = cur.get('mods') or ()
            prev_mods = (prev or {}).get('mods') or ()
            # 修饰键"从哪一秒开始一直按着"：组合变化即重新计时，抬起即清零
            if not mods:
                _st['held_since'] = None
            elif mods != prev_mods or _st['held_since'] is None:
                _st['held_since'] = now
            ev_open = bool(_ev['at'] and now - _ev['at'] <= EVENT_WINDOW_S)
            ev_moved = bool(prev and ev_open and cur.get('hwnd')
                            and (prev.get('hwnd'), prev.get('fg_pid')) !=
                                (cur.get('hwnd'), cur.get('fg_pid')))
            held_over = bool(mods and _st['held_since'] is not None
                             and (now - _st['held_since']) * 1000.0 >= HOLD_MS)
            # 实例数只有"这一帧可能落记录"时才查（Toolhelp 快照不便宜）
            if held_over or ev_moved:
                cur['our_instances'] = _instance_count()
            with _lock:
                if now - _st['win_start'] >= 3600.0:
                    _st['win_start'], _st['in_window'] = now, 0
                lines = report(prev, cur, now, held_since=_st['held_since'],
                               event_tag=_ev['tag'] if ev_open else '',
                               event_at=_ev['at'],
                               last_report=_st['last_report'],
                               reports_in_window=_st['in_window'])
                _st['prev'] = cur
                for ln in lines:
                    _st['in_window'] += 1
                    logger.info(ln)
        except Exception as e:
            # 采样器自己出错不能带走任何功能，但要在 app.log 留一句
            logger.info(f'⌨ 诊断采样器自身出错（已跳过本轮）: {e}')


def start(settings=None):
    """按 `app_settings['typing_diag_log']` 拉起采样线程（默认开）。"""
    global _thread, _stop
    enabled = True
    if settings is not None:
        enabled = bool(settings.get('typing_diag_log', True))
    if not enabled:
        logger.info('⌨ 诊断采样未启用（设置里可开：typing_diag_log）')
        return False
    with _lock:
        if _thread is not None and _thread.is_alive():
            return True
        _stop = threading.Event()
        _st['prev'] = _st['held_since'] = None
        _st['last_report'] = {}
        _st['win_start'], _st['in_window'] = time.time(), 0
        _thread = threading.Thread(target=_loop, args=(_stop,), daemon=True,
                                   name='typing-diag')
        _thread.start()
    logger.info(f'⌨ 诊断采样已启动｜pid={os.getpid()}｜'
                f'阈值={HOLD_MS}ms 事件窗={EVENT_WINDOW_S}s')
    return True


def stop():
    global _thread, _stop
    with _lock:
        if _stop is not None:
            _stop.set()
        th = _thread
        _thread = None
    if th is not None and th.is_alive():
        th.join(timeout=1.0)
    return True


def is_running():
    th = _thread
    return bool(th is not None and th.is_alive())


def mark_event(tag):
    """主线程在"可疑动作"处打锚点：没有锚点就分不清 H1/H2（事件窗 5s）。"""
    _ev['tag'] = str(tag or '')
    _ev['at'] = time.time()


def set_ui_state(minimized=None, hwnd=None):
    """主线程回灌窗口状态（线程不碰 Tk，只读这里的标志 + IsIconic 核对）。"""
    if minimized is not None:
        _ui['minimized'] = bool(minimized)
    if hwnd is not None:
        _ui['hwnd'] = int(hwnd or 0)
