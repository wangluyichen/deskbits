# -*- coding: utf-8 -*-
"""Win32 跨进程调用的安全封装。

范式来源：ZenDesktop（MIT）ui.rs 的跨进程 LVM 调用全部走
SendMessageTimeoutW(SMTO_ABORTIFHUNG)，注释原话"Explorer 挂死不能拖死 UI"；
openFrence 作者因跨进程 SendMessage 死锁整个禁用了该功能。
我们的规则：凡是发往 Explorer 桌面窗口（SysListView32/SHELLDLL_DefView/
Progman）的 SendMessage 一律走本模块，Explorer 假死时我们最多丢一次
调用，绝不被拖死。

★R286-A1（崩前"最后动作"留痕，= 挂账 O1 的方案②）：那一发若在 Explorer
侧 native abort，软件侧拦不住（★R282 #167 已判），拦不住不等于查不到——
所以发之前先把"在飞的是哪一发"落一行进 `data/win32_trail.log`，回来再补
一行"回来了 + 用时"。进程当场没，最后那行没配对的 `I|` 就是现场：
crash.log 只有栈，这里才有"当时正在问谁"。
三条边界，一条都不能越：
  · **只加证据**：返回值、超时、异常吞法一字未改（本函数对外的行为面）。
  · **有上限**：过 `TRAIL_CAP_BYTES` 就裁尾，常驻几天也不会把它养成新故障。
  · **绝不拖慢手感**：写失败、或单行写超 `TRAIL_SLOW_WRITE_MS`（网络盘、
    杀软拦文件）⇒ 本会话直接停用留痕。取证不值钱，客户体验值钱。
"""
import logging
import os
import threading
import time

import win32gui

from utils.app_paths import get_app_dir
# ★R319 M1：取证链要看"上一会话有没有走完自己的退出流程"，退出标记在那边。
# 取模块引用而不是 `from ... import FLAG_PATH`：后者把路径绑死在导入那一刻，
# 排障/门禁把它改指沙箱就拿不到了（★R311：无状态判据才是判据）。
from utils import clean_exit

logger = logging.getLogger("desktop_manager")

SMTO_ABORTIFHUNG = 0x0002
DEFAULT_TIMEOUT_MS = 300

# ---- 留痕（取证）侧的配置，全部可在测试里改常量 ----
TRAIL_NAME = 'win32_trail.log'
TRAIL_CAP_BYTES = 32768     # 到点裁尾（一次调用两行，约 300 发一轮换）
TRAIL_KEEP_LINES = 300
TRAIL_SLOW_WRITE_MS = 50    # 单行写盘超过它 ⇒ 停用留痕

#: 测试/排障注入点：置成绝对路径就用它，否则用 `<程序目录>/data/`
TRAIL_PATH_OVERRIDE = None

_trail_lock = threading.Lock()
_trail_off = False          # 写失败/写太慢 ⇒ 本会话停用（一次判定，不反复试）
_trail_warned = False
_trail_fh = None            # 常驻无缓冲追加句柄（理由见 _reopen）
_trail_written = 0          # 句柄视角的文件大小：发现"被人截空/删掉了"
_compact_off = False        # ★R319 M4：裁尾这条路走不通 ⇒ 本会话不再撞第二回
_compact_warned = False     # 同上：那条告警只说一次


def trail_path():
    if TRAIL_PATH_OVERRIDE:
        return TRAIL_PATH_OVERRIDE
    return os.path.join(str(get_app_dir()), 'data', TRAIL_NAME)


def _abbr(v):
    """把入参压成一截短 ASCII。留痕文件本身绝不能成为新的崩溃源，
    所以这里不许抛、也不许把几十 KB 的结构体 repr 灌进去。"""
    if v is None:
        return 'none'
    if isinstance(v, bool):
        return 'b%d' % int(v)
    if isinstance(v, int):
        return str(v) if -10 ** 12 < v < 10 ** 12 else '0x%x' % v
    return type(v).__name__[:16]


def _hexnum(v):
    """消息号按十六进制落（`0x1001` 比 `4097` 好对 SDK），其余原样走 _abbr。"""
    if isinstance(v, int) and not isinstance(v, bool):
        return '0x%x' % v
    return _abbr(v)


def _reopen(path):
    """打开/重开常驻句柄。

    为什么不开一次写一次：每次 `open()+flush()+close()` 实测 1.5ms/发
    （★R286 T12 第一次跑出这个数），跨进程调用本来就可能上百次连发，
    取证不能收这个税。`buffering=0` 的句柄每次 write 就是一发 WriteFile
    直进内核 ⇒ native abort 也追不回来，这正好是"崩前留痕"要的语义。"""
    global _trail_fh, _trail_written
    _close_fh()
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    _trail_fh = open(path, 'ab', buffering=0)
    try:
        _trail_written = os.path.getsize(path)
    except OSError:
        _trail_written = 0


def _close_fh():
    global _trail_fh
    fh = _trail_fh
    _trail_fh = None
    if fh is not None:
        try:
            fh.close()
        except Exception:
            pass



def _compact(path):
    """留痕超上限就只保留尾部若干行（原地重写，判据"末段有没有未配对 I|"不受影响）。

    ★R319 M4：这条路以前会自己把自己弄瘫。`os.replace` 撞着活读者（打包日志正被
    我们自己读着、杀软扫描）就抛 PermissionError，而彼时常驻句柄已经放掉、
    `_reopen` 又没跑到，于是三件事一起坏：①`.tmp` 永久留在 data/（三份清理名单
    都不收 `.tmp`）；②下一行写进 `None` ⇒ 留痕自废；③异常顺着调用链抛给调用方，
    而且**每一发跨进程调用都要再撞一次**（等于把取证税加回手感）。
    现在：失败就擦掉临时文件、把句柄接回来、本会话不再试，并留一条 warning
    说清"哪份文件、为什么、此后长度暂不受限"。
    """
    global _compact_off, _compact_warned
    if _compact_off:
        return
    try:
        if os.path.getsize(path) <= TRAIL_CAP_BYTES:
            return
    except OSError:
        return                      # 还没这个文件：不必裁
    # 必须先把自家常驻句柄放掉：Windows 上 os.replace 撞着一个还开着的
    # 文件会直接 PermissionError（★R286 第一次跑绿就被这条咬住——表现是
    # "裁剪从来没发生 + 留痕被自己判成写不动而停用"）。
    _close_fh()
    tmp = '%s.%d.tmp' % (path, os.getpid())
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            tail = f.read().splitlines()[-TRAIL_KEEP_LINES:]
        with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
            f.write('\n'.join(tail) + '\n')
        os.replace(tmp, path)
    except Exception as e:
        _compact_off = True
        try:
            if os.path.exists(tmp):
                os.remove(tmp)      # 别在 data/ 里留下没人收的 .tmp
        except OSError:
            pass
        if not _compact_warned:
            _compact_warned = True
            # 只报文件名：绝对路径可能带用户名（★R319 M6 同口径）
            logger.warning('[取证] 跨进程留痕裁不了尾（%s 被占用？）⇒ 本会话不再裁剪，'
                           '期间留痕继续写当前文件、长度暂不受限: %s'
                           % (os.path.basename(path), e))
        _reopen(path)               # 句柄必须接回来：裁剪失败不配让整条取证链自废
    else:
        _reopen(path)               # 句柄指的是被换掉的那份，必须重开


def _before_write(path, incoming):
    """每次写之前顺手照两件事实（只花一次 stat）：
      · 文件被"清理诊断日志/清空个人数据/60 天自清"截空或删掉了 ⇒ 重开句柄，
        别把下一批证据写进一个孤儿文件里（那种写法不会报错，只会静默丢）。
      · 加这一行就超上限 ⇒ 先裁尾。
    """
    global _trail_written
    if _trail_fh is None:
        _reopen(path)
        return
    try:
        size = os.path.getsize(path)
    except OSError:
        size = -1
    if size < 0 or size < _trail_written:
        _reopen(path)
        return
    _trail_written = size
    if size + incoming > TRAIL_CAP_BYTES:
        _compact(path)



def _trail_line(kind, detail):
    """追加一行 `<kind>|<时间>|pid=..|tid=..|<detail>`，写完就在内核里。

    "就在内核里"是本函数的全部意义：进程被 native abort 掐掉时，还留在
    Python 缓冲里的字节一起没了，那行"在飞"就白写。返回 False=这行没落盘。"""
    global _trail_off, _trail_warned
    if _trail_off:
        return False
    # ★R286-S6 返修：算路径与拼行**也在**这张保护网里。它们写在 try 外面时，
    # `get_app_dir()` 一抛就顺着 `_trail_line` 穿透 `send_msg_timeout`——调用方
    # 拿到的是异常而不是本该的返回值，"只加证据、不改行为"就漏了这一角。
    path = ''
    wrote_ms = -1.0
    try:
        path = trail_path()
        line = ('%s|%s|pid=%d|tid=%d|%s\n'
                % (kind, time.strftime('%m-%d %H:%M:%S'), os.getpid(),
                   threading.get_native_id(), detail)).encode('utf-8')
        with _trail_lock:
            _before_write(path, len(line))
            # ★R319 M3：计时贴着 write 那一句。旧写法把"算路径 + 拼行 + 裁尾"全
            # 算进"单行写入用时"——网络盘/杀软光让 trail_path() 多花 60ms，就会被
            # 谎报成"这一行写盘慢"并把整会话留痕停用，告警还指着错的地方。
            t0 = time.perf_counter()
            _trail_fh.write(line)
            wrote_ms = (time.perf_counter() - t0) * 1000.0
    except Exception as e:
        # 写不动 ⇒ 停用而不是每次重试：这条链路是"顺手加的证"，
        # 不能变成每次跨进程调用都要踩一脚的坑（但必须留一行，别静默）
        _trail_off = True
        _close_fh()
        if not _trail_warned:
            _trail_warned = True
            # ★R319 M6：只报文件名。整条绝对路径会进 app.log，而 Nuitka onefile
            # 的缓存目录里带着用户名——GUI 上屏那侧历轮一直是相对口径，这里跟上。
            where = (os.path.basename(path) if path else '路径都没算出来')
            logger.warning(f'[取证] 跨进程留痕写不动，本会话停用（{where}）: {e}')
        return False
    if wrote_ms > TRAIL_SLOW_WRITE_MS:
        _trail_off = True
        _close_fh()
        if not _trail_warned:
            _trail_warned = True
            logger.warning(f'[取证] 跨进程留痕单行写入 {wrote_ms:.0f}ms'
                           f'（阈值 {TRAIL_SLOW_WRITE_MS}ms，磁盘在网络上或被拦截？），'
                           f'本会话停用留痕')
        return False
    return True


def send_msg_timeout(hwnd, msg, wparam, lparam,
                     timeout_ms: int = DEFAULT_TIMEOUT_MS):
    """防挂死的 SendMessage。返回消息 LRESULT；目标挂死/超时/句柄无效返回 None。

    正常情况下与 win32gui.SendMessage 返回值一致（pywin32 把 SendMessageTimeout
    的结果包装为 (成功标志, LRESULT) 元组，失败抛 error 异常）。"""
    _trail_line('I', 'hwnd=%s|msg=%s|wp=%s|lp=%s|to=%s'
                % (_abbr(hwnd), _hexnum(msg), _abbr(wparam), _abbr(lparam),
                   _abbr(timeout_ms)))
    t0 = time.perf_counter()
    try:
        res = win32gui.SendMessageTimeout(
            hwnd, msg, wparam, lparam, SMTO_ABORTIFHUNG, timeout_ms)
        if isinstance(res, tuple):
            res = res[1]
    except Exception:
        _trail_line('O', 'ms=%d|ret=exc'
                    % int((time.perf_counter() - t0) * 1000))
        return None
    _trail_line('O', 'ms=%d|ret=%s'
                % (int((time.perf_counter() - t0) * 1000), _abbr(res)))
    return res


def _last_exit_was_clean(path):
    """上一会话有没有走完自己的退出流程（★R319 M1）。

    `consume_unfinished` 旧写法只看"末段有没有未配对的 I 行"，于是"点 X 退出
    （或条件命中关机）时正好有一发跨进程调用在飞"这种**正常收尾**，下次启动会被
    诬报成"上次会话停在未配对的跨进程调用"——日志页那条红字是假的，还会把用户往
    "程序被系统掐死"的方向带。

    判据做成无状态的（★R311 口径：靠上游老实传标记的防护不算防护）：磁盘上
    `clean_exit.flag` 的写入时刻不早于留痕最后一行 ⇒ 上一会话走到了自己的退出
    代码，那条未配对的 I 只是退出被硬切的一发，不是崩溃现场。
    任何一边拿不到（文件不在/读不动）一律判"不干净"——真崩溃不许被兜底静默。
    """
    try:
        trail_ts = os.path.getmtime(path)
        flag_ts = os.path.getmtime(str(clean_exit.FLAG_PATH))
    except OSError:
        return False
    return flag_ts >= trail_ts


def consume_unfinished():
    """启动时调一次：上一会话有"在飞没回来"的调用 ⇒ 返回那些原始行（最多 3 条）。

    判据取**最后一个 pid 那一段**：更早会话留下的 `I|` 若后面已经有了别的
    会话活动，就不是本次要说的现场。读罢补一行 `N|`（消费标记）——否则本会话
    若在第一次跨进程调用之前就退出，下次启动会把同一条重复报出来。
    没有可报的现场（文件不存在 / 都配对了 / 上一会话正常退出）返回 None。"""
    path = trail_path()
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            lines = [l for l in f.read().splitlines() if l.strip()]
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning(f'[取证] 跨进程留痕读不动（这次不报上次现场）: {e}')
        return None
    if not lines:
        return None
    try:
        last_pid = lines[-1].split('pid=', 1)[1].split('|', 1)[0]
    except Exception:
        return None
    block = [l for l in lines if ('pid=' + last_pid + '|') in l]
    open_by_tid = {}
    for l in block:
        parts = l.split('|')
        if len(parts) < 4:
            continue
        kind, tid = parts[0], parts[3]
        if kind == 'I':
            open_by_tid[tid] = l
        elif kind == 'O':
            open_by_tid.pop(tid, None)
    stale = [open_by_tid[t] for t in sorted(open_by_tid)][:3]
    if not stale:
        return None
    if _last_exit_was_clean(path):
        # 正常退出撞上的，不是现场：照样消费掉（补 N|，免得下次启动重复念），
        # 但只记一行 info 说清"为什么没报"，不往用户眼前递一条假故障。
        _trail_line('N', 'consumed=%d clean_exit=1' % len(stale))
        logger.info('[取证] 上次会话有 %d 发跨进程调用没配对，但退出标记比留痕新'
                    '（正常退出撞上的）⇒ 不记为异常现场' % len(stale))
        return None
    _trail_line('N', 'consumed=%d' % len(stale))
    logger.warning('[取证] 上次会话停在未配对的跨进程调用: %s' % ' ;; '.join(stale))
    return stale
