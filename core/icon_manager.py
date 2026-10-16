"""桌面图标管理器 - 基于 Win32 API 的图标位置操作"""
import struct
import ctypes
import threading
import time
from collections import deque
import win32api
import win32con
import win32gui
import win32process
import win32file
import logging
import json
import os
from pathlib import Path
from typing import Dict, Tuple, Optional, List, Iterable
from datetime import datetime

from utils.safeio import atomic_write_json
from utils.win32_safe import send_msg_timeout

logger = logging.getLogger("desktop_manager")


def _atomic_save_json(path: str, data: dict) -> bool:
    """原子写入 JSON：先写 .tmp 再 os.replace，避免写一半崩溃导致文件损坏。"""
    return atomic_write_json(path, data, indent=4)

# Win32 常量
LVM_GETITEMCOUNT = 0x1004
LVM_GETITEMTEXTW = 0x1073
LVM_GETITEMRECT = 0x100E   # wParam=索引, lParam=远程 RECT（left=LVIR_* 输入标记）
LVM_GETITEMPOSITION = 0x1010
LVM_SETITEMPOSITION = 0x100F
LVM_GETITEMSPACING = 0x1033   # ★R234：原写 0x1011 —— 那是 LVM_GETSTRINGWIDTHA，
                              # 对桌面 ListView 恒返回 0，get_item_spacing() 于是
                              # 永远吃 (95,125) 兜底（本机真值实测 75,98）
# ★R234（第 14~16 批根因）：**SDK 里根本没有 LVM_SETAUTOARRANGE**——历轮写的
# 0x1039 是 LVM_SUBITEMHITTEST（commctrl.h/pywin32 commctrl.py 同值），发出去
# 从来没关成过自动排列。自动排列挂在窗口样式位 LVS_AUTOARRANGE 上（Shell 的
# FFlags 位 0 就是它），改它走 SetWindowLong，见 get/set_auto_arrange()。
LVS_AUTOARRANGE = 0x0100

# ★ R86：布局存取专用诊断日志（logs/layout_debug.log，2MB×2 轮转）。
# 2026-9-16 恢复布局"假成功"事故教训：消息已发送≠已生效，位置类操作
# 必须读回验证并留痕（用户定案写进开发规范：不许靠猜）。
try:
    from logging.handlers import RotatingFileHandler as _RFH
    from utils.app_paths import get_app_dir as _gad
    _layout_log_dir = os.path.join(_gad(), "logs")
    os.makedirs(_layout_log_dir, exist_ok=True)
    _layout_logger = logging.getLogger("layout_debug")
    _lfh = _RFH(os.path.join(_layout_log_dir, "layout_debug.log"),
                maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    _lfh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _layout_logger.addHandler(_lfh)
    _layout_logger.setLevel(logging.INFO)
    _layout_logger.propagate = False
except Exception:
    _layout_logger = logging.getLogger("layout_debug")

WM_SETREDRAW = 0x000B
MEM_COMMIT = 0x1000
MEM_RELEASE = 0x8000
PAGE_READWRITE = 0x04
LVIF_TEXT = 0x0001

# 虚拟系统图标（此电脑/回收站等）的注册表显隐——文件属性对它们无效。
# CLSID 表与"双注册表键"写法照抄 Dasktop（MIT）catalog.rs / visibility.rs：
# 值=1 隐藏 / 0 显示 / 值缺失=该图标的系统默认状态。
VIRTUAL_ICON_REGISTRY_PATHS = (
    r"Software\Microsoft\Windows\CurrentVersion\Explorer\HideDesktopIcons\NewStartPanel",
    r"Software\Microsoft\Windows\CurrentVersion\Explorer\HideDesktopIcons\ClassicStartMenu",
)
VIRTUAL_DESKTOP_ICONS = {
    "此电脑": "{20D04FE0-3AEA-1069-A2D8-08002B30309D}",
    "回收站": "{645FF040-5081-101B-9F08-00AA002F954E}",
    "网络": "{F02C1A0D-BE21-4350-88B0-7367FC96EF3C}",
    "控制面板": "{5399E694-6CE5-4D6C-8FCE-1D8870FDCBA0}",
    "用户文件夹": "{59031A47-3F72-44A7-89C5-5595FE6B30EE}",
    # ★ 历史遗留别名：P2-C 之前用来固定匹配"用户文件夹"。
    # 现在由 IconManager.get_user_folder_icon_name() 动态解析账户名；
    # 本键保留仅为向后兼容（某些外部工具/脚本可能按 'Administrator' 查 CLSID）。
    # 副作用：桌面若存在同名普通图标会被 drag_drop 误过滤——概率极低，暂不改。
    "Administrator": "{59031A47-3F72-44A7-89C5-5595FE6B30EE}",
}
SHCNE_ASSOCCHANGED = 0x08000000
SHCNF_IDLIST = 0x0000


class _LVITEMW(ctypes.Structure):
    """Win32 LVITEMW 结构体（64位正确对齐）"""
    _fields_ = [
        ("mask", ctypes.c_uint),
        ("iItem", ctypes.c_int),
        ("iSubItem", ctypes.c_int),
        ("state", ctypes.c_uint),
        ("stateMask", ctypes.c_uint),
        ("pszText", ctypes.c_void_p),
        ("cchTextMax", ctypes.c_int),
        ("iImage", ctypes.c_int),
        ("lParam", ctypes.c_void_p),
    ]


def pack_lparam(x: int, y: int) -> int:
    """打包坐标到 LPARAM"""
    return ((y & 0xFFFF) << 16) | (x & 0xFFFF)


# --------------------------- 文件隐藏机制 ---------------------------
# 桌面图标本质是 Desktop 文件夹里的文件：设置 FILE_ATTRIBUTE_HIDDEN +
# FILE_ATTRIBUTE_SYSTEM（双属性）并发 shell 通知后，Explorer 会把图标原地
# 隐藏；清除属性后原地恢复。与显示器布局无关、位置零移动。
# 必须双属性：用户习惯开着"显示隐藏文件"（Hidden=1），单 HIDDEN 属性的
# 图标会以半透明露出；SYSTEM 属性使其落入"受保护的操作系统文件"范畴，
# 默认 ShowSuperHidden=0 下彻底不可见（实测通过）。
# 实测教训：多显示器下 ListView 会把一切越界坐标钳回主屏可见区，
# "移到屏幕外"的老办法在双屏机器上彻底失效，故分类切换改用本机制。

SHCNE_UPDATEITEM = 0x00002000
SHCNF_PATHW = 0x0005

_DESKTOP_FOLDER_GUIDS = (
    '{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}',  # 用户桌面
    '{C4AA340D-F20F-4863-AFEF-F87EF2E230BA}',  # 公共桌面
)


def _desktop_dirs() -> List[str]:
    """用户桌面 + 公共桌面。

    实测注意：本机 SHGetKnownFolderPath 对 FOLDERID_PublicDesktop 返回
    0x80070002，所以必须带注册表兜底（HKLM Common Desktop）。"""
    class _GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                    ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    def parse(s: str) -> _GUID:
        body = s.strip('{}')
        g = _GUID()
        g.Data1 = int(body[:8], 16)
        g.Data2 = int(body[9:13], 16)
        g.Data3 = int(body[14:18], 16)
        raw = bytes.fromhex(body[19:].replace('-', ''))
        for i in range(8):
            g.Data4[i] = raw[i]
        return g

    dirs: List[str] = []
    for gs in _DESKTOP_FOLDER_GUIDS:
        try:
            out = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(
                    ctypes.byref(parse(gs)), 0, None, ctypes.byref(out)) == 0:
                dirs.append(out.value)
                ctypes.windll.ole32.CoTaskMemFree(out)
        except Exception:
            pass

    # 注册表兜底（expandvars 展开 %PUBLIC% 等）
    try:
        import winreg
        for root, sub, val in (
                (winreg.HKEY_CURRENT_USER,
                 r'SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders',
                 'Desktop'),
                (winreg.HKEY_LOCAL_MACHINE,
                 r'SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders',
                 'Common Desktop')):
            try:
                with winreg.OpenKey(root, sub) as k:
                    p = os.path.expandvars(winreg.QueryValueEx(k, val)[0])
                if p:
                    dirs.append(p)
            except OSError:
                pass
    except Exception:
        pass

    uniq: List[str] = []
    for d in dirs:
        if d and os.path.isdir(d) and d.lower() not in {x.lower() for x in uniq}:
            uniq.append(d)
    if not uniq:
        uniq = [os.path.expanduser(r'~\Desktop'), r'C:\Users\Public\Desktop']
    return uniq


# ── ★R247（#109/#131 由挂账升级为必修）：桌面**同名图标**不再互相覆盖 ─────────
#   病灶：位置表是"名字为键"的 dict。用户桌面 + 公共桌面各一个同名图标（★R204
#   在本机 AutoClaw 两处实锤；win11 回传存档里 desktop.ini 也成对）在**读取那
#   一步**就折叠成一条——后读的顶掉先读的（旧 `icons[name] = (x, y)`），于是
#   存档里永远只剩一个坐标，另一个图标的位置信息连"进过存档"的机会都没有
#   ⇒ 保存/恢复/看护全按这一条走 ⇒ 同名两个图标必然有一个摆不回原位。
#   ★R204 当年只改了"len(icons)<count 被误判残缺、整桌读数弃用"这一半，
#   坐标折叠本身一直没动。
#   修法（**附加字段，不改 icons 的键形态**）：读取时把被顶掉的那些收进
#   _dup_cache（与 _position_cache 平行），存档多写 dup_cells={名字:[[x,y],…]}
#   —— 与 ★R245 icon_size / ★R246 view_state 同一惯例：只增字段，老版本读不到
#   就忽略，跨机导入不丢数据；恢复时主表套完位，再用**就近配对**把多出来的
#   坐标按 ListView **索引**写回（set_icon_position），绕开"按名字"这条天生
#   装不下的路。
#   为什么不改成 `名字#2` 这种键：按名匹配的地方有 ~11 处（filter_ghost_names /
#   avoid_newcomer_cells / plan_write_order / _hidden / check_guard /
#   select_icon_by_name …），而且**老版本会把 `#2` 当幽灵剪掉** ⇒ 跨机导入直接
#   丢数据。
def _fold_live_items(items):
    """把实时读到的 [(名字, ListView 索引, (x, y)), …] 折叠成两张表。

    返回 (主表 名字→坐标, 附加表 名字→[被顶掉的坐标…])。
    **"谁占主位"完全沿用既有语义**（后读到的覆盖先读到的），这样历轮所有按名字
    取坐标的调用方（分类显隐、看护、让位、去重…）行为一个字都不变；本函数只做
    一件事：**把原本要被丢掉的那条坐标留下来**。纯函数，不碰窗口/注册表/COM。
    """
    icons, dups = {}, {}
    for it in items or []:
        try:
            name, xy = it[0], it[2]
        except Exception:
            continue
        if not name:
            continue
        if name in icons:
            dups.setdefault(name, []).append(icons[name])
        icons[name] = xy
    return icons, dups


def _pair_dup_cells(name, targets, live):
    """同名"多出来的坐标" ↔ 现场同名的几个图标，按**就近**一一配对。

    `targets`：这个同名图标要摆的坐标 [(x, y), …]；
    `live`：现场 (名字, 索引, 坐标) 里名字对得上的那几条。
    返回 [(索引, 目标坐标), …]，条数 = min(目标数, 现场数)：
      · 目标比现场多 ⇒ 多出来的**丢弃**（绝不把坐标塞给别的图标）；
      · 目标比现场少 ⇒ 只配这一条（少说少做）；
      · 贪心取最近：每轮挑"剩余目标 × 剩余现场"里距离最小的一对——同名一般
        只有 2~3 个，代价可忽略，且不会出现交叉配对。
    纯函数：是否"已在位所以不必写"由调用层按容差判，这里不改语义。
    """
    lv = [it for it in (live or []) if it and it[0] == name and it[2]]
    tg = []
    for p in targets or []:
        try:
            tg.append((int(p[0]), int(p[1])))
        except Exception:
            continue
    out, used_l, used_t = [], set(), set()
    for _ in range(min(len(tg), len(lv))):
        best = None
        for ti, t in enumerate(tg):
            if ti in used_t:
                continue
            for li, it in enumerate(lv):
                if li in used_l:
                    continue
                d = (t[0] - int(it[2][0])) ** 2 + (t[1] - int(it[2][1])) ** 2
                if best is None or d < best[0]:
                    best = (d, ti, li, t)
        if best is None:
            break
        out.append((lv[best[2]][1], best[3]))
        used_l.add(best[2])
        used_t.add(best[1])
    return out


def ensure_desktop_view_visible() -> bool:
    """★ R102 黑桌面防线 / ★R254 提为模块级：确保桌面图标窗口可见。

    背景（2026-09-16 win11 事故 + 2026-09-26 用户 45 批 item2）：管理页「隐藏」
    把 explorer 的 SHELLDLL_DefView 整层 SW_HIDE——那是**别人家的窗口**，不随
    本进程死亡消失。此前复明调用全在"程序活着"的路上（启动自愈/悬浮条「全部」/
    规则页），退出与崩溃路径一处都没有 ⇒ 点隐藏后关程序屏幕一片黑。
    ★R254 起三层防线：①启动自愈（下次开软件自动复明，原有）；②_main_window
    退出 `_teardown` 早段；③崩溃钩子 app_bootstrap._sys_hook/_thread_hook。
    本函数体因此提到模块级：**崩态只 import 这个函数就能用，不构造 IconManager**
    （__init__ 有 mkdir/DPI 副作用，异常路径里禁碰）。只做 SW_SHOW（对已可见
    窗口零副作用，幂等可反复调）。查找逻辑与 rescue_desktop_icons.py 同一套：
    Progman 直子 + WorkerW 链（壁纸引擎拓扑）。"""
    try:
        found_any = False
        progman = win32gui.FindWindow("Progman", None)
        if progman:
            shell = win32gui.FindWindowEx(progman, 0,
                                          "SHELLDLL_DefView", None)
            if shell:
                found_any = True
                if not win32gui.IsWindowVisible(shell):
                    win32gui.ShowWindow(shell, win32con.SW_SHOW)
                    logger.warning("[防线] 桌面图标窗口处于隐藏态，已拉回显示"
                                   "（此前被自动隐藏/异常路径藏掉）")
        w = win32gui.FindWindowEx(0, 0, "WorkerW", None)
        while w:
            shell = win32gui.FindWindowEx(w, 0, "SHELLDLL_DefView", None)
            if shell:
                found_any = True
                if not win32gui.IsWindowVisible(shell):
                    win32gui.ShowWindow(shell, win32con.SW_SHOW)
                    logger.warning("[防线] 桌面图标窗口(WorkerW)处于隐藏态，已拉回显示")
            w = win32gui.FindWindowEx(0, w, "WorkerW", None)
        # ★R255（46 批 item④-1）状态写点③：复明成功即证明"隐藏态已结束"，
        # 顺手清哨兵状态（崩溃钩子/启动自愈都经此，哨兵见 false 收工）。
        # 崩态下这是赚的不是债：写盘失败只吞异常保链路，不上抛。
        if found_any:
            try:
                from core import desktop_sentinel
                desktop_sentinel.set_hidden(False)
            except Exception:
                pass
        return found_any
    except Exception as e:
        logger.error(f"确保桌面图标窗口可见失败: {e}")
        return False


def _screen_stamp_from_archive(data) -> dict:
    """★R287-N25：从布局存档里取"屏幕指纹"，只认 `_screen_stamp()` 写出来的那个形状。

    可信形状 = `{"main": [w, h], "virtual": [x, y, w, h]}`（值必须是**非空列表**）。
    其余一律当"这份存档没记屏"⇒ 返回 `{}`，**绝不抛**：存档可能被人手改过、
    从别的机器导过来、或是老版本写的。读侧此前直接拿 `data` 里的 `screen` 原值
    就往下用，再 `list(_stamp.get('virtual') or [])`，遇到 `virtual` 是个数字就
    `TypeError: 'int' object is not iterable` —— 而那一行正坐在 restore 的
    屏外检查与去重之间，一个**只用于留痕**的诊断字段能把整条恢复打死。

    口径：诊断可以缺席，功能不能被打断。脏指纹宁可不报，不可报错。
    """
    if not isinstance(data, dict):
        return {}
    raw = data.get('screen')
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key in ('main', 'virtual'):
        if key not in raw:
            continue
        val = raw[key]
        if not isinstance(val, (list, tuple)) or not val:
            return {}          # 一半可信的指纹不比没有更可解释，整套作废
        out[key] = list(val)
    return out


class IconManager:
    """桌面图标管理器"""

    # ★R316（P1）：hwnd 的默认值必须是**类级**属性，不能只在 __init__ 里赋。
    #   __init__ 第 394 行就调 _load_initial_positions()，而实例赋值原先排在 410 行
    #   （晚 16 行）⇒ 那条链 _get_current_icon_map → _read_icon_map_from_listview →
    #   get_icon_count 读 self._hwnd_listview 时撞 AttributeError，又被 ★R193 的
    #   `except Exception: return 0` 当成"Explorer 读不到"吞掉 ⇒ 启动期实时读数恒空
    #   ⇒ 存档里每一枚都被判"不在当前桌面"（假警告）+ 白耗 2.5 s 轮询预算。
    _hwnd_listview: int = 0

    def __init__(self, backup_dir: str = None, state_dir: str = None):
        # 进程 DPI 感知：坐标与 Explorer 内部一致（否则 DPI 缩放下位置会错位）
        # ★R219：原来这里直接调旧接口 SetProcessDPIAware()，那是 **System**
        #   档；档位先到先得且不可覆盖，一旦它抢在 app_bootstrap 的 PMv2 声明
        #   之前跑过（诊断脚本单独构造 IconManager 就是这种路径），进程就被
        #   永久钉在 System 档，125% 缩放下表现为"框不准"。改走统一入口，
        #   PMv2 优先、已声明时静默失败不影响。
        try:
            from core.dpi_geom import declare_process_dpi_awareness
            declare_process_dpi_awareness()
        except Exception:
            pass
        self.backup_dir = backup_dir or str(Path(__file__).parent / "icon_backups")
        Path(self.backup_dir).mkdir(parents=True, exist_ok=True)
        # 隐藏状态文件目录（gui 传 config 目录）
        self.state_dir = state_dir or self.backup_dir
        Path(self.state_dir).mkdir(parents=True, exist_ok=True)
        self._hidden_state_path = os.path.join(self.state_dir, "hidden_state.json")
        self._hidden: Dict[str, dict] = {}   # {图标名: {path, was_hidden}}
        self._load_hidden_state()
        # 实时"正被本程序隐藏"集合（highlighter 过滤框用；与 _hidden 的区别：
        # unhide_all 恢复属性后 _hidden 仍保留记录供 auto_backup 参考）。
        # 初始化按文件实际属性核实，避免磁盘上的陈旧记录造成误判
        self._currently_hidden: set = set()
        for _n, _i in self._hidden.items():
            try:
                if (not _i.get('was_hidden') and os.path.exists(_i['path'])
                        and (win32api.GetFileAttributes(_i['path'])
                             & win32file.FILE_ATTRIBUTE_HIDDEN)):
                    self._currently_hidden.add(_n)
            except Exception:
                pass
        # 虚拟系统图标（此电脑/回收站等）接管记录：注册表法隐藏前的原始值
        # 范式来源：Dasktop（MIT）shell_icon_visibility_records——只恢复自己
        # 隐藏过的，绝不覆盖用户自己设置的隐藏状态
        self._virtual_state_path = os.path.join(self.state_dir, "virtual_icon_state.json")
        self._virtual_managed: Dict[str, dict] = {}   # {图标名: {clsid, prev}}
        self._load_virtual_state()
        self._position_cache: Dict[str, Tuple[int, int]] = {}  # 名称→位置缓存（实时读数优先，备份只兜底）
        # ★R247：同名图标"被顶掉的那些"坐标（名字→[(x,y),…]），与位置缓存平行。
        #   主表按名字天生装不下两条，所以多出来的一条单独存这儿，进存档时写
        #   附加字段 dup_cells、恢复时按索引补回去。
        self._dup_cache: Dict[str, list] = {}
        self._live_items: list = []   # ★R247 最近一次实时读数（未折叠，带索引）
        # 启动时从最新备份加载位置到缓存兜底
        self._load_initial_positions()
        # 布局守护（防远程重连/刷新回退）：RDP 每次断开/重连都会改变显示拓扑，
        # Explorer 随即换用新拓扑下保存的旧布局（实测三个图标工具全部中招）。
        # 检测方式（跳变检测）：对比连续两次轮询的位置快照，图标位置在 2 秒内
        # 瞬间大面积跳变（≥25% 且 ≥5 个、位移>32px）→ 判定为系统回退，
        # 拉回守护的布局一次；用户手动逐个拖动是渐进变化，绝不会触发。
        self.guard_enabled = False
        self._guard_path: Optional[str] = None
        self._guard_last_apply = 0.0
        self._guard_snapshot: Optional[Dict[str, Tuple[int, int]]] = None
        self._prev_moved = 0            # 上轮有位移的图标数（=0 视为静止）
        self._guard_msgs: deque = deque()
        self._guard_suppress_until = 0.0   # 程序套用布局期间抑制守护（防误判回退）
        self.saved_layout_name: str = ''   # 最近一次保存/恢复的布局名（gui 可读取用于提示）
        self._guard_notify_cb = None       # 守护恢复后回调，用于通知 highlighter 刷新
        # ★ 实时读数覆盖备份播种的过时值（备份里的位置可能早已过时；
        #   2026-09-02 实测 GETITEMPOSITION 实时读 81 个仅需 ~10ms）
        try:
            live = self._read_all_positions_live()
            if live:
                self._position_cache.update(live)
                # ★R300-F1：失败分支立过牌子 ⇒ 这一行自己说清前一句作废，
                #   别让读日志的人看见两条互相打脸的话。牌子用完即清。
                if getattr(self, '_startup_cache_kept_empty', False):
                    self._startup_cache_kept_empty = False
                    logger.info(
                        f"启动位置缓存已用实时读数覆盖（{len(live)} 个）——"
                        "此前那句「实时读数也失败，保留空缓存」作废："
                        "当时桌面视图还没就绪，现在读到了")
                else:
                    logger.info(f"启动位置缓存已用实时读数覆盖（{len(live)} 个）")
        except Exception as e:
            logger.error(f"启动实时位置读取失败（沿用备份值）: {e}")

    def get_user_folder_icon_name(self) -> str:
        """桌面"用户文件夹"图标的显示名 = 当前账户名（如 Administrator）。

        ★ P2-C：桌面"用户文件夹"的显示名是账户名，而非固定的 'Administrator'。
        代码里硬编码 'Administrator' 在换机/不同账户时失效——此处动态解析。
        ★ P2-C 修正（避免 CWD 陷阱）：USERPROFILE 为空/缺失时**不能**用
        Path('')（那会 resolve 到当前工作目录，返回项目目录名而非用户名）——
        而是仅在非空时才取目录 basename，空则回退 getpass.getuser()（标准库
        跨平台读 USERNAME/USER/LOGNAME）。最后兜底固定别名 'Administrator'
        （保证此电脑/回收站等静态 CLSID 路径仍可用）。"""
        try:
            up = os.environ.get('USERPROFILE', '')
            if up:
                name = Path(up).name
                if name:
                    return name
        except Exception:
            pass
        # 备用：getpass.getuser() 跨平台读当前用户名
        try:
            import getpass
            name = getpass.getuser()
            if name:
                return name
        except Exception:
            pass
        return 'Administrator'

    @property
    def hwnd_listview(self) -> int:
        """懒加载：动态查找最新有效的 ListView 句柄"""
        if self._hwnd_listview and win32gui.IsWindow(self._hwnd_listview):
            return self._hwnd_listview
        self._hwnd_listview = self._get_desktop_listview_hwnd()
        return self._hwnd_listview

    def _get_desktop_listview_hwnd(self) -> int:
        """获取桌面 ListView 窗口句柄"""
        hwnd_progman = win32gui.FindWindow("Progman", None)
        hwnd_shell = win32gui.FindWindowEx(hwnd_progman, 0, "SHELLDLL_DefView", None)
        hwnd_listview = win32gui.FindWindowEx(hwnd_shell, 0, "SysListView32", None)

        if not hwnd_listview:
            def enum_callback(hwnd, lParam):
                hwnd_shell = win32gui.FindWindowEx(hwnd, 0, "SHELLDLL_DefView", None)
                if hwnd_shell:
                    lv = win32gui.FindWindowEx(hwnd_shell, 0, "SysListView32", None)
                    if lv:
                        lParam.append(lv)
                return True

            hwnds = []
            win32gui.EnumWindows(enum_callback, hwnds)
            if hwnds:
                hwnd_listview = hwnds[0]

        if not hwnd_listview:
            raise RuntimeError("无法找到桌面图标控件")
        return hwnd_listview
    
    def get_icon_count(self) -> int:
        """获取图标数量（Explorer 假死/超时/找不到控件时返回 0，调用方按空桌面兜底）"""
        # ★ R193（Qoder CORE-8）：hwnd_listview 属性在找不到 ListView 时抛
        # RuntimeError——Explorer 重启空窗期会炸而非按文档返回 0，并成为
        # CORE-1/2 黑桌面链的引信。降级函数必须兑现降级语义。
        try:
            return send_msg_timeout(self.hwnd_listview, LVM_GETITEMCOUNT, 0, 0) or 0
        except Exception:
            return 0

    def get_item_spacing(self) -> Tuple[int, int]:
        """桌面图标的网格间距 (cx, cy)——排列图标时按此对齐，避免吸附后重叠"""
        try:
            res = send_msg_timeout(self.hwnd_listview, LVM_GETITEMSPACING, 0, 0) or 0
            cx, cy = res & 0xFFFF, (res >> 16) & 0xFFFF
            if cx <= 0 or cy <= 0:
                return (95, 125)
            return (cx, cy)
        except Exception:
            return (95, 125)

    # ── ★R234：自动排列（真机制）─────────────────────────────────────
    def get_auto_arrange(self) -> Optional[bool]:
        """桌面"自动排列图标"此刻是否开着（读 SysListView32 的样式位）。

        返回 None = 读不到（控件没了/跨进程失败），调用方按"未知"处理，
        不可把它当成"关着"继续写坐标。"""
        try:
            st = win32gui.GetWindowLong(self.hwnd_listview, win32con.GWL_STYLE)
            return bool(st & LVS_AUTOARRANGE)
        except Exception:
            return None

    def set_auto_arrange(self, on: bool) -> bool:
        """开/关自动排列，**读回确认**后返回是否达到目标态。

        2026-09-24 实测（run/probe28.txt）：样式位跨进程可写、Explorer 1s 内
        不会自己改回去；开着它时 LVM_SETITEMPOSITION 直接返回 0（一格都挪不动），
        关掉后返回 1 且真的落位——所以"恢复布局没生效/看护拉扯"的第一件事就是查这里。
        这里刻意用 GetWindowLong/SetWindowLong 而不是 SendMessage：跨进程发消息
        会被挂死的 Explorer 拖住（见 utils/win32_safe.py 的立规理由）。
        """
        try:
            lv = self.hwnd_listview
            st = win32gui.GetWindowLong(lv, win32con.GWL_STYLE)
            new = (st | LVS_AUTOARRANGE) if on else (st & ~LVS_AUTOARRANGE)
            if new != st:
                win32gui.SetWindowLong(lv, win32con.GWL_STYLE, new)
                win32gui.InvalidateRect(lv, None, True)
            back = win32gui.GetWindowLong(lv, win32con.GWL_STYLE) & LVS_AUTOARRANGE
            return bool(back) == bool(on)
        except Exception as e:
            logger.warning(f"set_auto_arrange({on}) 失败: {e}")
            return False

    def ensure_auto_arrange_off(self) -> str:
        """位置类操作前的闸门：确保自动排列关着。返回 off/cleared/failed/unknown。

        自动排列开着时任何 SETITEMPOSITION 都是白费劲（实测返回 0），历轮靠
        发 0x1039"关它"其实是发了条命中测试消息，所以现象是"套了位、看起来
        成功、刷新又乱"。这里改成真开关，并把结果如实回传给调用方留痕。"""
        state = self.get_auto_arrange()
        if state is False:
            return 'off'
        if state is None:
            return 'unknown'
        return 'cleared' if self.set_auto_arrange(False) else 'failed'

    def auto_arrange_block_reason(self) -> str:
        """写坐标前的一句话诊断（空串=没有阻塞），供各调用方直接进日志。"""
        state = self.get_auto_arrange()
        if state is True:
            return '桌面开着"自动排列图标"，位置由系统管，写进去也会被拒'
        if state is None:
            return '读不到桌面视图样式位（控件可能刚被重建）'
        return ''

    # ── ★R246 B5-①/③：视图状态埋点 + 偏差归因（**纯只读**，收口 #179）────
    # 为什么放在这里而不是各调用点各写一遍：存/取布局、守护、清排序兜底都要
    # 回答同一个问题——"这一刻到底是谁在管桌面图标"。出口只有一个，日志与
    # 存档读到的才是同一份数（★R246 版本标注那条"同源"口径同理）。
    def _view_stamp(self) -> dict:
        """读一次"当时的活动排序列 + 自动排列"，给存档/日志用（读不到 ⇒ ``{}``）。

        通道全在 `core/sort_clear.archive_stamp` 里封着：COM `SortColumns`（权威）
        + 注册表 bag 镜像（只补 autoarrange 那一位）。这里**不写注册表、不发消息、
        不碰鼠标**，任何异常都吞成空 dict ⇒ 调用方"干脆不写这个键"，
        比写个 null 干净（回放时"存档没这一项"是老存档的正常情况）。
        """
        try:
            from core import sort_clear as sc
            return sc.archive_stamp()
        except Exception as e:
            logger.debug("[view_stamp] desk view state unavailable: %s",
                         type(e).__name__)
            return {}

    def drift_report(self) -> dict:
        """写后回读不符时的**归因**：{'cause': 代号, 'text': 给用户看的那句}。

        两路互校：窗口样式位（`get_auto_arrange`，写坐标真正的闸门）优先，
        读不到再退回注册表镜像；活动排序列走 COM。四种代号见
        `sort_clear.CAUSE_*`——**"读不到"单独一档，绝不当成"被忽略"**，
        否则日志会把用户往错的方向支走（★R242 win11 那份日志就吃过这个亏）。
        """
        cause, col = 'unknown', None
        try:
            from core import sort_clear as sc
            st = sc.read_sort_state() or {}
            col = st.get('sort_column')
            cause = sc.drift_cause(st, autoarrange=self.get_auto_arrange())
            return {'cause': cause, 'text': sc.drift_text(cause, col)}
        except Exception as e:
            logger.debug("[drift_report] unavailable: %s", type(e).__name__)
            return {'cause': cause,
                    'text': u'读不到桌面视图状态，偏差原因未知（不当成"没问题"）'}

    # ── ★R245 A2：图标尺寸（用户 37 批 一-1「加图标尺寸」＝挂账 #176）────
    # 为什么尺寸必须跟布局一起存：**图标尺寸一变，格子间距就跟着变**
    # （★R244 X3 实测：小/中/大档下同一组坐标落点完全不同）。以前存档只记
    # 坐标，等于默认"桌面永远同一档尺寸"——在 32px 下摆好的布局套到 48px
    # 桌面上整体错格，用户看到的还是"乱了"。
    # 通道用 COM（桌面窗口 Document.IconSize 可读可写）：★R239/R244 实证
    # QI IFolderView2 在本机直接报"不支持此接口"，而这条通（同 sort_clear）。
    ICON_SIZE_MIN = 16
    ICON_SIZE_MAX = 256

    def _shell_desktop_doc(self):
        """桌面窗口的 COM Document（读/写 IconSize 用）；拿不到返回 None，不抛。"""
        try:
            import pythoncom
            from win32com.client import dynamic
            try:
                pythoncom.CoInitialize()        # 已初始化过也算 ok
            except Exception:
                pass
            app = dynamic.Dispatch("Shell.Application")
            return app.Windows().FindWindowSW(None, "", 8, 0, 1).Document
        except Exception as e:
            logger.debug("COM 桌面 Document 不可用: %s", type(e).__name__)
            return None

    def get_icon_size(self) -> Optional[int]:
        """当前桌面图标尺寸（像素）。**读不到返回 None**——调用方按"未知"处理，
        不许当成 0 或某个默认值写进存档（写错比不写更糟）。"""
        doc = self._shell_desktop_doc()
        try:
            px = int(getattr(doc, "IconSize", 0) or 0)
            if px > 0:
                return px
        except Exception:
            pass
        # 退路：注册表 bag 的 IconSize（Explorer 自己写的镜像，这里只读）
        try:
            from core import desk_view_state as _dvs
            st = _dvs.read_desk_view_state()
            if st.get("ok"):
                px = int(st.get("icon_size") or 0)
                if px > 0:
                    return px
        except Exception:
            pass
        return None

    def set_icon_size(self, px: int) -> bool:
        """设桌面图标尺寸，**写完用 `get_icon_size()` 回读核对**才返回 True。

        为什么不能"发了就算成功"（★R244 的同族教训）：Explorer 正在刷新、
        或自动排列开着时，属性写进去会被吃掉而读数不变——回读不一致＝没改成。
        为什么读要走 `get_icon_size()` 而不是自己 grab `doc.IconSize`：那是
        **第二条读数路**，COM 对象失效时它会异常或给出旧值，而 `get_icon_size`
        还带注册表 bag 退路。写和读必须同一把尺子，否则"回读通过"是假的。
        """
        try:
            want = int(px)
        except (TypeError, ValueError):
            logger.warning("set_icon_size 拒绝非数字尺寸: %r" % (px,))
            return False
        if not (self.ICON_SIZE_MIN <= want <= self.ICON_SIZE_MAX):
            logger.warning("set_icon_size 拒绝越界尺寸: %r" % (px,))
            return False
        if self.get_icon_size() == want:
            return True                     # 已经是这一档，不用重排
        doc = self._shell_desktop_doc()
        if doc is None:
            return False
        try:
            doc.IconSize = want
        except Exception as e:
            logger.warning("set_icon_size(%d) 写入失败: %s"
                           % (want, type(e).__name__))
            return False
        for _ in range(6):
            if self.get_icon_size() == want:
                # 尺寸变⇒格子间距变⇒旧坐标缓存全部作废：不丢的话，看护和
                # 统计会拿"上一档尺寸"的位置跟实时位置比对（★R234 同族）
                try:
                    self._position_cache.clear()
                except Exception:
                    pass
                try:
                    win32gui.InvalidateRect(self.hwnd_listview, None, True)
                except Exception:
                    pass
                logger.info("桌面图标尺寸已设为 %dpx（回读核对通过）" % want)
                return True
            time.sleep(0.05)
        logger.warning("set_icon_size(%d) 回读不符，未确认生效" % want)
        return False

    def _apply_archived_icon_size(self, data: dict) -> dict:
        """把存档里记的尺寸回放到位（restore 的**第一步**：先尺寸后坐标）。

        返回 ``{"ok", "changed", "from", "to", "err"}``。三条兼容规矩：
          · 老存档没这一项 / 值为 null ⇒ 一根手指都不动（ok=True, changed=False）；
          · 尺寸本来就一样 ⇒ 不写（少一次无谓重排）；
          · 值离谱或不是数 ⇒ 拒绝写并如实带 err，绝不外抛。
        """
        res = {"ok": True, "changed": False, "from": None, "to": None, "err": ""}
        raw = data.get("icon_size") if isinstance(data, dict) else None
        if raw is None:
            res["err"] = "no-icon-size"
            return res
        try:
            want = int(raw)
        except (TypeError, ValueError):
            res.update({"ok": False, "err": "bad-icon-size"})
            logger.warning("存档里的 icon_size 不是数字，跳过回放: %r" % (raw,))
            return res
        res["to"] = want
        if not (self.ICON_SIZE_MIN <= want <= self.ICON_SIZE_MAX):
            res.update({"ok": False, "err": "out-of-range"})
            logger.warning("存档 icon_size=%s 超出桌面允许区间，跳过回放" % want)
            return res
        cur = self.get_icon_size()
        res["from"] = cur
        if cur is None:
            res.update({"ok": False, "err": "current-unknown"})
            logger.warning("读不到当前桌面图标尺寸，本次不回放尺寸（坐标照旧套）")
            return res
        if cur == want:
            res["err"] = "already-same"
            return res
        if not self.set_icon_size(want):
            res.update({"ok": False, "err": "set-failed"})
            return res
        res["changed"] = True
        return res

    def icon_at_point(self, x: int, y: int) -> Optional[str]:
        """★ B18（S 段）：屏幕坐标 → 命中的桌面图标名（网格命中，零跨进程开销）。

        用 _position_cache（图标左上角，监视线程 2s 实时刷新）+ 网格间距近似
        图标单元格。统计用途精度足够；未命中返回 None。"""
        try:
            if not self._position_cache:
                return None
            cx, cy = self.get_item_spacing()
            for name, (px, py) in self._position_cache.items():
                if px <= x < px + cx and py <= y < py + cy:
                    return name
        except Exception:
            pass
        return None
    
    def _read_all_positions_live(self) -> Dict[str, Tuple[int, int]]:
        """跨进程实时读取全部图标的 (名称 → 位置)。

        2026-09-02 实测推翻旧结论"Progman 不响应 LVM_GETITEMPOSITION"——
        旧探测读取方式有误（见记忆 08-31 验证条目）。实时读数是唯一可信的
        当前位置来源；成功时会同步更新 _position_cache。

        ★R247：循环改为**先收 (名字, 索引, 坐标) 全量、最后再折叠**——同名图标
        （用户桌面 + 公共桌面各一个）在旧写法下于 `icons[name]=…` 那行互相顶掉，
        第二条坐标当场消失。现在被顶掉的那条进 `_dup_cache`，索引留在
        `_live_items`，恢复时才有办法按索引补回去。"""
        icons = {}
        # ★R247：本轮读到的每一条（含同名），未折叠
        items: List[Tuple[str, int, Tuple[int, int]]] = []
        # ★ R193（Qoder CORE-8）：属性访问此前在 try 外——找不到控件时抛
        # RuntimeError 而非按文档返回 {}
        try:
            pid = win32process.GetWindowThreadProcessId(self.hwnd_listview)[1]
        except Exception:
            return icons
        process_handle = None
        remote_memory = None

        try:
            process_handle = win32api.OpenProcess(
                win32con.PROCESS_ALL_ACCESS, False, pid
            )
            remote_memory = win32process.VirtualAllocEx(
                process_handle, 0, 8192, MEM_COMMIT, PAGE_READWRITE
            )

            count = self.get_icon_count()
            text_buffer = remote_memory + ctypes.sizeof(_LVITEMW)
            # ★R204 真实桌面新案：残缺判定改为"失败项计数"——此前用
            # len(icons)<count，用户桌面+公共桌面存在同名图标（本机
            # AutoClaw 两处实锤）时字典天然少 1 项，被误判残缺→读数
            # 永久弃用→所有调用方全部降级（切换/归位/保存连锁失灵）
            _failed = 0

            for i in range(count):
                # 获取位置
                if send_msg_timeout(
                    self.hwnd_listview, LVM_GETITEMPOSITION, i, remote_memory
                ) is None:
                    _failed += count - i   # 本项及之后全部未读到
                    break  # Explorer 假死：后续调用也会超时，直接放弃本轮
                point_data = win32process.ReadProcessMemory(
                    process_handle, remote_memory, 8
                )
                x, y = struct.unpack("ii", point_data)

                # 获取名称（使用 ctypes LVITEMW 结构体）
                lv = _LVITEMW()
                lv.mask = LVIF_TEXT
                lv.iItem = i
                lv.iSubItem = 0
                lv.pszText = text_buffer
                lv.cchTextMax = 512
                ptr = ctypes.addressof(lv)
                win32process.WriteProcessMemory(
                    process_handle, remote_memory,
                    ctypes.string_at(ptr, ctypes.sizeof(_LVITEMW))
                )
                _name_ok = False
                for _retry in range(4):
                    if send_msg_timeout(
                        self.hwnd_listview, LVM_GETITEMTEXTW, i, remote_memory
                    ) is not None:
                        _name_ok = True
                        break
                    time.sleep(0.05)
                if not _name_ok:
                    # 2026-09-03 修复：超时重试 4 次——此前直接跳过导致
                    # Explorer 忙时部分图标从映射里消失，切换集合出错
                    _failed += 1
                    continue
                text_raw = win32process.ReadProcessMemory(
                    process_handle, text_buffer, 1024
                )
                name = text_raw.decode("utf-16-le", errors="replace").split("\0", 1)[0]
                if name:
                    # ★R247：原来这里直接 `icons[name] = (x, y)`，同名图标后读的
                    # 顶掉先读的 ⇒ 第二条坐标在"读"这一步就丢了。改成先收全量。
                    items.append((name, i, (x, y)))
                else:
                    _failed += 1

            # ★R247：折叠成主表 + 附加表（"谁占主位"的语义与旧写法一致）
            icons, dups = _fold_live_items(items)

            # ★ 残缺读数防扩散（2026-09-05 审查修复）：有失败项 = 中途
            #   超时跳过过项，此时结果不可信——返回空让调用方沿用旧缓存，
            #   防止残缺数据污染 _position_cache / 布局文件
            #   ★R204：改按 _failed 计数——同名图标（用户+公共桌面各一个
            #   AutoClaw 实锤）让 len(icons)<count 恒真，整桌读数被永久
            #   误判残缺弃用
            if count and _failed:
                logger.warning(
                    f"实时位置读取残缺（{len(icons)}/{count}，"
                    f"失败 {_failed} 项），本次结果弃用")
                # ★R247：整轮读数作废 ⇒ 附加表/索引表一并作废，
                #   绝不能留半截数据被下一次 save_layout 抄进存档
                self._dup_cache = {}
                self._live_items = []
                return {}

            # ★ R193（Qoder CORE-9）：缓存改在残缺判定通过后整体 update——
            # 原来循环里逐项写缓存，残缺 return {} 时缓存已被污染且撤不回
            if icons:
                self._position_cache.update(icons)
                # ★R247：同一次读数的两份产物一起落地（主表 + 同名附加表）
                self._dup_cache = dups
                self._live_items = list(items)
                if dups:
                    # 只在真有同名时留一行痕迹（本函数按秒轮询，别刷屏）
                    logger.debug("[dup] 桌面同名图标 %d 组：%s"
                                 % (len(dups), list(dups)[:6]))
                    _layout_logger.info(
                        f"[dup] live 同名组={len(dups)} {list(dups)[:6]}")

            return icons

        except Exception as e:
            logger.error(f"获取图标位置失败: {e}")
            return {}
        finally:
            if remote_memory and remote_memory != 0 and process_handle:
                win32process.VirtualFreeEx(process_handle, remote_memory, 0, MEM_RELEASE)
            if process_handle:
                win32api.CloseHandle(process_handle)
    
    def get_current_icon_positions(self) -> Dict[str, Tuple[int, int]]:
        """获取当前所有图标的位置。

        2026-09-02 起改为实时读数优先：旧"缓存优先"会被 _load_initial_positions
        播种的旧备份数据污染，导致恢复/位置校正的判断基准失灵（实测图标
        恢复后漂移却校不正）。实时读不到时才回退缓存。"""
        live = self._read_all_positions_live()
        if live:
            return live
        if self._position_cache:
            return dict(self._position_cache)
        return {}

    def get_live_icon_items(self) -> List[Tuple[str, int, Tuple[int, int]]]:
        """★R247：现场 (名字, ListView 索引, 坐标) **未折叠**全量列表。

        位置表按名字折叠后，"桌面上这个名字其实站着两个图标"根本看不出来；
        要补同名图标多出来的那条坐标，必须知道每一条站在哪个索引上
        （按名字写只有第一条能写进去），所以单独提供这条不折叠的读数。
        """
        try:
            self._read_all_positions_live()
        except Exception as e:
            logger.debug("[dup] 未折叠读数失败: %s" % e)
        return list(getattr(self, '_live_items', []) or [])

    def _layout_source(self, filepath: str) -> str:
        """读布局文件的来源标记：manual=用户手动保存 / auto=程序自动写 /
        manual(缺字段)=旧版文件——一律当手动备份保护，绝不清理"""
        try:
            with open(filepath, "r", encoding="utf-8") as fh:
                return json.load(fh).get("source", "manual")
        except Exception:
            return "manual"

    def _prune_layout_backups(self, name: str, keep: int = 5):
        """同名布局滚动清理。

        规则（2026-09-05 用户明确要求：手动备份绝不能删）：
        - 内部备份（_家长模式前 等 _ 开头）：全部程序生成，保留最近 keep 份
        - 用户布局：只清理 source=auto 的自动备份（保最近 keep 份）；
          source=manual 及旧版无标记文件一律保留"""
        try:
            internal = name.startswith('_')
            prefix = name + '_'
            files = sorted([f for f in os.listdir(self.backup_dir)
                            if f.startswith(prefix) and f.endswith('.json')],
                           reverse=True)          # 时间戳文件名，新在前
            if internal:
                victims = files[keep:]
            else:
                autos = [f for f in files
                         if self._layout_source(os.path.join(self.backup_dir, f)) == 'auto']
                victims = autos[keep:]
            for old in victims:
                try:
                    os.remove(os.path.join(self.backup_dir, old))
                    logger.info(f"清理自动布局备份: {old}")
                except Exception as e:
                    logger.warning(f"清理 {old} 失败: {e}")
        except Exception as e:
            logger.warning(f"布局备份清理失败: {e}")

    def pick_layout(self, name: str):
        """同名布局优选：优先用户手动保存（manual/旧版无标记），取其中最新；
        没有手动备份才回退到最新的自动备份。返回布局 dict（含 path）或 None"""
        cands = [l for l in self.list_layouts() if l.get('name') == name]
        if not cands:
            return None
        manual = [l for l in cands if l.get('source', 'manual') != 'auto']
        return max(manual or cands, key=lambda l: l.get('timestamp', ''))

    # ── ★R235：布局存档清洗（幽灵 / 同格重叠 / 屏外）──────────────────
    # 用户 21/22 批（win11 第 3 份日志）：写入链路已经通了（移动=75、落位=75），
    # 但桌面还是"看着乱"，根因在**存档本身不干净**：
    #   ① 存档 82 条 / 桌面 75 个 ⇒ 7 个早已删掉、改名的"幽灵"。它们在
    #      restore_layout 结尾被 `cache.update(saved_icons)` 灌回缓存，下一次
    #      "保存全部"(sync_full_cache) 又原样写进新存档 ⇒ 脏数据自我复制，
    #      并且 [verify] 永远报"偏差=7"（挂账 #101 一并收口）。
    #   ② 存档里两对名字写的是同一格（win11：(112,637) 与 (570,767)）
    #      ⇒ 套位后图标叠在一起（挂账 #107）。
    #   ③ 双屏机器（主屏 1920x1080 / 虚拟屏 3840x1200）按主屏判屏外
    #      ⇒ 副屏图标全被误报"跑去分屏"，真屏外反而没人管。
    def _virtual_screen_rect(self):
        """当前虚拟屏 (x, y, w, h)——双屏时覆盖两块屏，读不到时退回主屏。"""
        try:
            vx = win32api.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
            vy = win32api.GetSystemMetrics(77)
            vw = win32api.GetSystemMetrics(78)
            vh = win32api.GetSystemMetrics(79)
            if vw > 0 and vh > 0:
                return (vx, vy, vw, vh)
        except Exception:
            pass
        try:
            return (0, 0, win32api.GetSystemMetrics(win32con.SM_CXSCREEN),
                    win32api.GetSystemMetrics(win32con.SM_CYSCREEN))
        except Exception:
            return (0, 0, 1920, 1080)

    def _screen_stamp(self) -> dict:
        """存档里的屏幕指纹：事后可判断这份布局是在哪套屏上抄的。"""
        vx, vy, vw, vh = self._virtual_screen_rect()
        try:
            mw = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
            mh = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)
        except Exception:
            mw = mh = 0
        return {"main": [mw, mh], "virtual": [vx, vy, vw, vh]}

    def _boxed_names(self):
        """★R306：此刻正躺在本系统「收纳盒」里的图标显示名 → (名字集合, 读不动?)。

        为什么必须有这一条（win11 R024 两份真机日志的 A/B 差集）：盒子档切分类时，
        那些"这次不该显示"的图标是**真的搬进了** `桌面盒子deskbits\\收纳·<组>`——
        不在桌面 ListView、不在 `_hidden`、桌面目录里也没有同名文件 ⇒ ★R235 那三条
        幽灵判据全中。盒子档每切一次就在 `layout_debug.log` 里报
        `幽灵=45~71` 并把它们从 `_position_cache` 剪掉（隐藏档恒 `幽灵=0`），
        主布局记忆就这么一轮一轮被啃掉、缺席图标存档的那一格也不再保留 ⇒
        用户读成"除了全部以外都很乱"。它们不是被删掉的图标，是我们自己收起来的。

        只认 `hidden_box.json` **登记过**的盒：用户手工建的盒里的同名文件照旧按幽灵
        处理（★R305 同一条红线——那条链一枚都不许碰用户的盒）。
        """
        try:
            from core import desktop_box as _db
            from core import visibility_router as _vr
            _own, l_fail = _vr.load_ledger()
            if l_fail:
                return set(), True
            _m, m_fail = _db.load_manifest()
            if m_fail:
                return set(), True
            return set(_vr.boxed_members()), False
        except Exception as e:
            logger.warning("[ghost] 收纳盒成员读不动，这一轮一个都不剪: %s", e)
            return set(), True

    def filter_ghost_names(self, icons: dict, live_names=None):
        """挑出"桌面上已经没有、也不是本程序藏着"的名字。

        返回 (保留 dict, 幽灵名单)。判幽灵要三条同时成立，宁保守不误删：
        不在 ListView 当前名单里 + 不在 _hidden（本程序隐藏的图标本来就不在
        ListView 里）+ 桌面目录磁盘上也没有同名文件（Explorer  momentarily
        读不到时不至于把真图标当幽灵抹掉）。live_names 读不到（空）⇒ 一律不剪。
        ★R299 N2：另有一批**不可能被删除**的名字直接豁免（五枚系统图标 +
        用户文件夹的动态显示名）——它们被隐藏时三条判据全中，坐标记忆会被抹。
        ★R306：再加第四批豁免＝本系统收纳盒里的图标（盒子档切分类的那批）。
        """
        if live_names is None:
            try:
                live_names = set(self._read_icon_map_from_listview().keys())
            except Exception:
                live_names = set()
        else:
            live_names = set(live_names)
        if not live_names:
            return dict(icons), []
        ours = set(self._hidden.keys()) if getattr(self, '_hidden', None) else set()
        # ★R299 N2：五枚系统图标（此电脑/回收站/网络/控制面板/用户文件夹）被
        #   "隐藏"时本来就不在 ListView、桌面目录里也没有同名文件 ⇒ 三条判据全中，
        #   位置记忆被一遍遍抹掉（win11 挂机日志里这条"🧹 桌面上已经不存在了"打了
        #   99 次，名单正是那五枚）。它们不可能被删除，一律豁免；名单口径与悬浮条
        #   框选用的系统图标集合同源：静态 CLSID 表 + 动态解析的账户显示名。
        try:
            ours |= set(VIRTUAL_DESKTOP_ICONS.keys()) | {self.get_user_folder_icon_name()}
        except Exception:
            ours |= set(VIRTUAL_DESKTOP_ICONS.keys())
        try:
            on_disk = set(self._name_to_path_map().keys())
        except Exception:
            on_disk = set()
        cand = [n for n in icons
                if n not in live_names and n not in ours and n not in on_disk]
        if cand:
            # ★R306：只有真要剪什么的时候才去问盒子——隐藏档常态（cand 为空）
            # 一发读数都不发（门禁 K8 钉这条零额外开销）。
            boxed, bfail = self._boxed_names()
            if bfail:
                logger.warning("🧹 这一轮一个图标都不剪：收纳盒里都有谁认不出来"
                               "（多半是 `data/hidden_box.json` 读不动）——"
                               "宁可留着可疑的坐标记忆，也不冒"
                               "「把搬进盒子的图标当删掉的抹掉」这个险")
                return dict(icons), []
            ours |= boxed
            cand = [n for n in cand if n not in ours]
        kept = {n: p for n, p in icons.items() if n not in set(cand)}
        return kept, cand

    def prune_ghost_positions(self, reason: str = '', live_names=None) -> list:
        """★R235：把幽灵从 _position_cache 里剔掉（斩断"存档→缓存→新存档"闭环）。

        ★R247：同名图标的附加坐标（_dup_cache）跟着一起剪——只剪主表的话，
        幽灵名字下面的那条附加坐标会被下一次 save_layout 原样抄进新存档，
        ★R235 那条复刻闭环换个字段再来一遍。
        """
        kept, ghosts = self.filter_ghost_names(self._position_cache, live_names)
        if ghosts:
            self._position_cache = kept
            try:
                dcache = getattr(self, '_dup_cache', None)
                if dcache:
                    for g in ghosts:
                        dcache.pop(g, None)
            except Exception as e:
                logger.debug("[dup] 附加表剪幽灵失败: %s" % e)
            logger.info("🧹 已剔除 %d 个桌面上已不存在的图标记录（%s）：%s"
                        % (len(ghosts), reason or '位置缓存', ghosts[:8]))
            _layout_logger.info(f"[prune] {reason} 剔除幽灵={len(ghosts)} {ghosts[:10]}")
        return ghosts

    def _dup_cells_for_save(self, names) -> dict:
        """★R247：本次存档要写的同名附加坐标（只留"主表里真有这个名字"的）。

        坐标一律规整成 [[x, y], …]（JSON 里 tuple/list 没区别，但统一成 list
        读起来更直白）。**没有同名就返回空 dict ⇒ 调用方干脆不写这个键**，
        与 ★R245 icon_size / ★R246 view_state 同一惯例：老版本读不到就忽略。
        """
        out = {}
        try:
            dcache = getattr(self, '_dup_cache', None) or {}
            if not dcache:
                return out
            for name, cells in dcache.items():
                if self._is_placeholder_name(name):
                    continue      # ★R312：主表已不认领占位文件，附加表也不许带走
                if name not in names:
                    continue      # 主表都没有这个名字 ⇒ 幽灵，不带走
                pts = []
                for p in cells or []:
                    try:
                        pts.append([int(p[0]), int(p[1])])
                    except Exception:
                        continue
                if pts:
                    out[name] = pts
        except Exception as e:
            logger.debug("[dup] 附加表整理失败: %s" % e)
        return out

    def dedupe_layout_cells(self, icons: dict, view=None, reserved=None):
        """存档内同一格挤了两个及以上名字 ⇒ 后来者顺延到最近的空格。

        返回 (清洗后 dict, [(名字, 原格, 新格), ...])。同组里第一个保持不动
        （存档顺序即用户当初的登记顺序，动"后来者"最少惊扰用户）。
        ★R236：`reserved` 传"桌面上新放的图标当前占的格"（像素坐标集合），
        顺延挑空格时同样要绕开——否则拆了一格又叠到别人身上。
        ★R237：算格子要带**现场样本**标定格点原点，否则顺延写出去的坐标
        落在屏原点的假格点上，Explorer 仍旧就近吸附（等于白让位）。
        """
        seen, moves = set(), []
        dup = [n for n in icons if tuple(icons[n]) in seen or seen.add(tuple(icons[n]))]
        if not dup:
            return dict(icons), []
        geom = self._grid_geometry(view, list(icons.values()) + list(reserved or ()))
        # 占用格用**格子序号**算：存档里的像素值可能没完全对齐网格，
        # 直接比像素会把"同一格差几像素"的两条当成两格（★R236 一并收口）。
        busy = {self._cell_index(p, geom) for p in icons.values()}
        busy |= {self._cell_index(p, geom) for p in (reserved or ())}

        fixed = dict(icons)
        taken = set()
        for name in dup:
            old = tuple(fixed[name])
            new_cell = self._nearest_free_cell(self._cell_index(old, geom),
                                               busy | taken, geom)
            if new_cell is None:      # 网格全占满（理论不可能）：原地往下找空
                cx, cy = geom[0], geom[1]
                step = 1
                while (self._cell_index((old[0], old[1] + step * cy), geom)
                       in busy | taken):
                    step += 1
                new_cell = self._cell_index((old[0], old[1] + step * cy), geom)
            taken.add(new_cell)
            fixed[name] = self._cell_origin(new_cell, geom)
            moves.append((name, old, fixed[name]))
        logger.warning("🧹 布局里有 %d 个图标和别人挤在同一格，已顺延：%s"
                       % (len(moves), [m[0] for m in moves[:6]]))
        _layout_logger.info(f"[dedupe] 同格顺延={len(moves)} {moves[:6]}")
        return fixed, moves

    # ── ★R236：桌面上"存档里根本没有的新图标"占的格子要当保留格 ──────────────
    #   用户 24 批 item2：保存布局后又往桌面丢新文件（常态，"不是谁都记得
    #   随时保存"）。存档给 c 记的那格 (150,0) 此刻正站着一个新图标，
    #   恢复时照抄存档把 c 写回 (150,0) ⇒ **两个图标叠在同一格**，
    #   用户读成的还是"程序一恢复就变乱"。★R235 的 dedupe 只看存档内部，
    #   看不见"存档里没有的人"，所以这里补第三条清洗。
    # ★R237：真实格点**不在**虚拟屏原点上。win11 那台机器实测格点是
    #   (17+n*95, 2+m*127)——历轮拿屏原点 (0,0) 当格点算，让位/拆同格写出去
    #   的坐标（如 1615、950）根本不是合法格点，Explorer 一律就近吸附
    #   （1632 / 967），"吸到哪一格"由不得我们（日志 #115）。修法：**格子序号
    #   语义不变**，只用现场读数标定原点偏移，写出去的像素落在真格点上。
    #   三条保守线：样本 <4 个、或者余数乱散（一致性 ≤60%）⇒ 退回屏原点，
    #   绝不硬猜一个偏移出来（宁可用旧行为，也不引入新的一次位移）。
    @staticmethod
    def _median(vals):
        v = sorted(vals)
        if not v:
            return 0
        n = len(v)
        return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) // 2

    def _calibrate_origin(self, points, cx, cy, vx, vy,
                          min_n: int = 4, spread: int = 8,
                          coherence: float = 0.6):
        """从现场坐标反推格点原点；不收敛则退回屏原点 (vx, vy)。

        做法：每个坐标对格距取**带符号余数**（差几像素也算同一格），全体
        余数取中位数即候选偏移；只有"多数点都贴着这个偏移"（严格 > 一致性
        门限）才认账。列优先还是行优先不影响——两轴各自标定。
        """
        pts = [p for p in points
               if isinstance(p, (list, tuple)) and len(p) == 2]

        def _axis(idx, step, base):
            vals = []
            for p in pts:
                r = (int(p[idx]) - int(base)) % step
                if r > step // 2:
                    r -= step
                vals.append(r)
            if len(vals) < min_n:
                return 0
            med = self._median(vals)
            hits = sum(1 for r in vals if abs(r - med) <= spread)
            return med if hits > coherence * len(vals) else 0

        try:
            return (int(vx) + _axis(0, cx, vx), int(vy) + _axis(1, cy, vy))
        except Exception:
            return (int(vx), int(vy))

    def _grid_geometry(self, view=None, samples=None):
        """网格几何 (cx, cy, ox, oy, vw, vh)。

        ox/oy 默认是视图原点；传 `samples`（坐标 dict 或坐标序列）时返回
        **标定后**的格点原点（★R237），让"算出来的格子"和"Explorer 真正
        摆得下的格子"是同一批。
        """
        try:
            cx, cy = self.get_item_spacing()
        except Exception:
            cx, cy = 75, 95
        cx, cy = max(1, int(cx)), max(1, int(cy))
        vx, vy, vw, vh = view or self._virtual_screen_rect()
        ox, oy = int(vx), int(vy)
        if samples:
            try:
                pts = samples.values() if isinstance(samples, dict) else samples
                ox, oy = self._calibrate_origin(pts, cx, cy, ox, oy)
            except Exception:
                ox, oy = int(vx), int(vy)
        return (cx, cy, ox, oy, int(vw), int(vh))

    def _cell_index(self, pos, geom=None):
        """像素 → 格子序号（四舍五入 ⇒ 实时读数差几像素仍认成同一格）。"""
        cx, cy, vx, vy, vw, vh = geom or self._grid_geometry()
        try:
            return (int(round((pos[0] - vx) / float(cx))),
                    int(round((pos[1] - vy) / float(cy))))
        except Exception:
            return (0, 0)

    def _cell_origin(self, cell, geom=None):
        """格子序号 → 该格左上角像素。"""
        cx, cy, vx, vy, vw, vh = geom or self._grid_geometry()
        return (vx + cell[0] * cx, vy + cell[1] * cy)

    def _nearest_free_cell(self, cell, busy, geom=None):
        """从 `cell` 往外一圈圈找最近的空格（比"从头找第一个空格"更贴近原位）。

        越界/被占都不选；整张屏都找不到才返回 None（调用方保持原样不乱搬）。
        """
        cx, cy, vx, vy, vw, vh = geom or self._grid_geometry()
        cols = max(1, int(vw // cx))
        rows = max(1, int(vh // cy))
        span = max(cols, rows) + 2
        for r in range(1, span):
            best = None
            for dc in range(-r, r + 1):
                for dr in range(-r, r + 1):
                    if max(abs(dc), abs(dr)) != r:
                        continue
                    c, rr = cell[0] + dc, cell[1] + dr
                    if not (0 <= c < cols and 0 <= rr < rows):
                        continue
                    if (c, rr) in busy:
                        continue
                    d = (dc * cx) ** 2 + (dr * cy) ** 2      # 同圈内取欧氏最近
                    if best is None or d < best[0]:
                        best = (d, (c, rr))
            if best:
                return best[1]
        return None

    # ── ★R309（#361）：桌面占位文件自己让位，别让真图标让位 ─────────────────
    #   病灶（真机 R026 日志 29 条 🧭）：用户打开"显示隐藏文件"后 `desktop.ini`
    #   进了 Explorer 的 ListView。它既不在存档、也不在那五枚系统图标里 ⇒
    #   `avoid_newcomer_cells` 把它当"用户新放的图标"，每轮摆布局都把站在这格上
    #   的**真图标**（此电脑 / HashKit / J荐片高清影音）顶到旁边空格。
    #   ③"新放的图标永远不动"那条保守线是给**用户的东西**定的，系统占位文件
    #   不是用户放的（收纳盒那边 `NEVER_BOX` 早就不让它进盒子）。
    #   用户 42 批原话："直接固定在某个其他图标没有的地方…放在空的地方，省的起冲突"。
    PLACEHOLDER_DESKTOP_NAMES = ('desktop.ini', 'thumbs.db')

    @classmethod
    def _is_placeholder_name(cls, name):
        """桌面占位文件（`desktop.ini`/`Thumbs.db`…）：系统自己写的，不是用户的东西。"""
        return (name or '').lower() in cls.PLACEHOLDER_DESKTOP_NAMES

    @classmethod
    def _strip_placeholders(cls, icons, where=''):
        """★R312（用户 68 批 item1）：存档/回放目标一律**不认领**桌面占位文件。

        为什么排到这一批：`save_layout` 把 ListView/缓存的读数原样抄进存档，用户开着
        "显示隐藏的文件"时 `desktop.ini` 就跟着进了档 ⇒ 回放按那条坐标把它摆回主屏。
        真机 R029 两枚排行组正好差在这一格上（`tmp/bugflow/20261008_1655/`）：
          16:49:51 [save] name=排行 1-20 名 n=17
          16:51:04 [restore] 排行 1-20 名_…json 匹配=17/17 占位挪位=0   ← 摆回 (112,891)＝看得见
          16:51:10 [park] 占位文件挪位=1 [('desktop.ini',(112,891)→(3627,1018))]
          16:51:10 [restore] 排行 21-40 名_…json 匹配=14/14 占位挪位=1  ← 15-1：摘掉才 14＝看不见
        「全部」那份更难看：16:44~16:50 之间 `79/79 占位挪位=0` 与 `78/78 占位挪位=1`
        交替 10+ 次＝每轮先摆回去、再请走。

        清洗只作用于**写出去/要回放的那份 dict**，`_position_cache` 保持桌面真实读数
        （★R299 N1 同口径：它是守护与读回验证的基准，抹平它会造成假位移）。
        剔除要留痕，不许静默少一枚。
        """
        src = icons or {}
        out = {k: v for k, v in src.items() if not cls._is_placeholder_name(k)}
        if len(out) != len(src):
            _layout_logger.info("[strip] %s 剔除占位文件=%s"
                                % (where or '?',
                                   sorted(set(src) - set(out))[:6]))
        return out

    @staticmethod
    def _free_cell_out_of_the_way(busy, cols, rows):
        """从右往左找空列，**同一列里从下往上**取第一个空格（锚在底边）。

        桌面图标从左上角往下排，**最右**那几列常年是空的 ⇒ 占位文件放那儿既不挡
        人。★R311 改从下往上扫：底边那一格谁也排不到（要挤到它得整屏图标），
        所以落点跟"这一类有几个图标"无关＝用户要的"固定"；旧写法从上往下扫，
        第 0 格一旦被这一类的存档占住就往上跳一格，切分类时上下来回跑
        （真机 R029 点名：desktop.ini 有时在 1-20、有时在 21-40）。
        右边留一格不贴屏幕边，免得名字被裁掉。
        """
        for c in range(max(0, cols - 2), -1, -1):
            for r in range(rows - 1, -1, -1):
                if (c, r) not in busy:
                    return (c, r)
        return None

    def placeholder_park_plan(self, icons, live_pos, view=None):
        """纯计算（不碰窗口）：占位文件该挪去哪。返回 `[(名字, 目标格), ...]`。

        "没人站"的口径是**两边都算**：现场别人所在的格 + 存档里大家将要落的格，
        免得把占位文件塞进某个真图标的存档位，反过来又触发让位。

        ★R312（用户 68 批 item1"跟其他分类一样，看不见也行"）：判据从 ★R310 的
        "脚下这格有人要才请走"改成**摆布局就一定请走**。旧判据留着就有真机那组
        症状——排行 1-20 那份存档认领了它的坐标，而它脚下那格没人抢 ⇒ 一条计划
        都不给 ⇒ 下一句回放把它摆回主屏 (112,891)＝用户看得见的那枚。
        目标格还是 ★R311 那个固定锚点（最右富余列的**底行**，与本次存档占了哪几格
        无关 ⇒ 不再上下跳）；**已经站在那一格就不给计划**——真机日志里每轮
        `占位文件挪位=1` 那一发，正是往同一格重复写。
        """
        if not live_pos:
            return []
        mine = sorted(n for n in live_pos if self._is_placeholder_name(n))
        if not mine:
            return []
        _pts = list((icons or {}).values()) + list(live_pos.values())
        geom = self._grid_geometry(view, _pts)
        busy = {self._cell_index(p, geom) for n, p in live_pos.items()
                if n not in mine}
        busy |= {self._cell_index(p, geom) for p in (icons or {}).values()}
        cx, cy, vx, vy, vw, vh = geom
        cols = max(1, int(vw // cx))
        rows = max(1, int(vh // cy))
        out, taken = [], set()
        for name in mine:
            own = self._cell_index(live_pos[name], geom)
            cell = self._free_cell_out_of_the_way(busy | taken, cols, rows)
            if cell is None:
                break               # 整屏满了（理论不可能）：宁可不搬
            if cell == own:
                continue            # 已经站在要给它的那格 ⇒ 一发不写（幂等）
            out.append((name, cell))
            taken.add(cell)
        return out

    def park_placeholder_icons(self, icons, live_pos, view=None):
        """把桌面占位文件挪进空格，并把它们从存档目标里摘掉（布局不该管它们）。

        返回 (调整后存档 dict, [(名字, 旧坐标, 新坐标), ...])。四条保守线只管**写**：
        ① `live_pos` 读不到（空）⇒ 一发不写；
        ② 现场没有占位文件 ⇒ 一个都不动（绝不做"顺手整理"）；
        ③ 自动排列闸门关不掉 ⇒ 不写（写了也不落地，白刷一次桌面）；
        ④ 写完**逐条读回**，不落地就不认账。
        ★R312：**摘账不受这四条约束**——四条提前返回以前都把 `icons` 原样交回，
        占位文件仍留在 `saved_icons` 里，下面拆同格/避新人/套位照样摆它（真机
        `匹配=17/17 占位挪位=0` 那一轮就是这么把 desktop.ini 摆回主屏的）。现在
        每条返回都过 `_strip_placeholders`；它没被挪走时留在现场，就按"驻场新人"
        被 `avoid_newcomer_cells` 兜住（别人给它让位），比"既不认账又继续摆它"更稳。
        """
        plan = self.placeholder_park_plan(icons, live_pos, view)
        if not plan:
            return self._strip_placeholders(icons, '回放'), []
        try:
            gate = self.ensure_auto_arrange_off()
        except Exception:
            gate = 'unknown'
        if gate != 'off':
            logger.debug("[park] 自动排列闸门=%s ⇒ 占位文件本轮不挪（写了也不落地）"
                         % gate)
            return self._strip_placeholders(icons, '回放(闸门未关)'), []
        _pts = list((icons or {}).values()) + list(live_pos.values())
        geom = self._grid_geometry(view, _pts)
        try:
            idx_map = self._get_current_icon_map() or {}
        except Exception:
            idx_map = {}
        wrote = []
        for name, cell in plan:
            idx = idx_map.get(name)
            if idx is None:
                continue            # 名字读得到、索引拿不到：不猜
            xy = self._cell_origin(cell, geom)
            if self.set_icon_position(idx, xy[0], xy[1]):
                wrote.append((name, tuple(live_pos.get(name) or xy), xy))
        if not wrote:
            return self._strip_placeholders(icons, '回放(未写成)'), []
        try:
            reread = dict(self._read_all_positions_live() or {})
        except Exception:
            reread = {}
        hits = []
        for name, old, xy in wrote:
            got = reread.get(name)
            if (got is None
                    or self._cell_index(got, geom) != self._cell_index(xy, geom)):
                logger.warning("🧭 桌面占位文件「%s」挪位后读回不落地（要 %s，读到 %s），"
                               "本轮照旧让存档图标给它让位" % (name, xy, got))
                continue
            hits.append((name, old, xy))
        # 布局不再认领占位文件的坐标（它归"空格"管，一认就要跟真图标抢格子）
        fixed = self._strip_placeholders(icons, '回放')
        if hits:
            logger.info("🧭 已把桌面占位文件 %s 挪到没人站的空格 %s，真图标各回各格"
                        % ([h[0] for h in hits[:6]], [h[2] for h in hits[:6]]))
            _layout_logger.info("[park] 占位文件挪位=%d %s" % (len(hits), hits[:6]))
        return fixed, hits

    def avoid_newcomer_cells(self, icons: dict, live_pos: dict, view=None):
        """存档目标格正被"存档里没有的名字"（用户新放的图标）占着 ⇒ 让**存档图标**让位。

        返回 (调整后 dict, [(名字, 原格, 新格), ...])。三条保守线：
        ① `live_pos` 空（实时坐标读不到）⇒ 原样返回，不猜、不乱搬；
        ② 没有新人占格 ⇒ 一个都不动（这套逻辑绝不做"顺手整理"）；
        ③ 只挪"要落在新人格子上"的那一个存档图标，**新放的图标永远不动**。
        """
        if not icons or not live_pos:
            return dict(icons), []
        _pts = list(icons.values()) + list(live_pos.values())
        geom = self._grid_geometry(view, _pts)
        mine = set(icons.keys())
        held = {self._cell_index(p, geom) for n, p in live_pos.items() if n not in mine}
        if not held:
            return dict(icons), []
        busy = {self._cell_index(p, geom) for p in icons.values()} | held
        fixed, hits, taken = dict(icons), [], set()
        for name, pos in icons.items():
            cell = self._cell_index(pos, geom)
            if cell not in held:
                continue
            target = self._nearest_free_cell(cell, busy | taken, geom)
            if target is None:
                continue          # 整屏满了（理论不可能）：宁可不搬
            fixed[name] = self._cell_origin(target, geom)
            taken.add(target)
            hits.append((name, tuple(pos), fixed[name]))
        if hits:
            # ★R300-F5：占位的名字从前一律写成"**新放的图标**"，把此电脑/回收站/
            #   网络这类系统图标也算进去（win11 真机一条日志点出五个"新放的图标"，
            #   读的人以为桌面冒出了五个新东西）。分类沿用现成的豁免口径。
            _sys_names = set(VIRTUAL_DESKTOP_ICONS.keys()) | {self.get_user_folder_icon_name()}
            _who = [n for n in live_pos if n not in mine]
            _ph = [n for n in _who if self._is_placeholder_name(n)]
            _sys = [n for n in _who if n in _sys_names]
            _new = [n for n in _who
                    if n not in _sys_names and n not in _ph]
            _bits = []
            if _sys:
                _bits.append('系统图标 %s' % (_sys[:6],))
            if _ph:
                # ★R309：`desktop.ini` 这类系统占位文件单独点名——它多半已经被
                #   `park_placeholder_icons` 请去空格了，走到这条分支说明那一挪
                #   没落地，写"新放的图标"会让人以为桌面冒出了新东西。
                _bits.append('桌面占位文件 %s' % (_ph[:6],))
            if _new:
                _bits.append('新放的图标 %s' % (_new[:6],))
            logger.warning("🧭 布局里有 %d 个图标要落的位置被占着（%s），"
                           "已让它们挪到旁边空格：%s"
                           % (len(hits), '；'.join(_bits) or '来源未点名',
                              [h[0] for h in hits[:6]]))
            _layout_logger.info(
                f"[conflict] 新人占格顺延={len(hits)} 系统图标={_sys[:8]} "
                f"占位文件={_ph[:8]} 新放的图标={_new[:8]} {hits[:6]}")
        return fixed, hits


    # ── ★R237：写入顺序要"碰撞安全" ─────────────────────────────────────────
    #   `LVM_SETITEMPOSITION` 把图标写进一个**此刻有人站着**的格子时，Explorer
    #   不报错、也不叠加，而是顺手塞到最近的空格 ⇒ 一次恢复里只要有一个人
    #   的落点被"还没搬走的人"占着，就会连锁顺延出一整列（win11 第 5 份日志
    #   9 条偏差全部 +127 = 一行高，补发后仍剩 4 条 = 用户眼前的"乱"）。
    #   历轮靠 [verify2] 补发一次兜住，但补发窗口只有一趟、且第二遍全量写紧跟
    #   在 Explorer 自己重排之后 ⇒ 兜不住。正解是**按顺序写**：先写"落点此刻
    #   就是空的"那个，逐轮把人质解开。解不开的真死循环（两图标互换格子）
    #   诚实标进 bumped——它注定要靠读回补发收敛，日志/界面能点名说清。
    def plan_write_order(self, targets: dict, live_pos: dict, view=None):
        """给出碰撞安全的写入顺序，返回 (顺序 list, 注定被撞 list)。

        三条口径（与 ★R236 同族的保守线）：
        ① `live_pos` 读不到（空）⇒ 原样顺序、不猜（行为等同历轮，绝不多动）；
        ② 站着不动的人（存档里没有的名字，如用户新放的图标）占的格**永远算占位**，
           宁可标 bumped 也不假装能落位；
        ③ 返回的是 targets 的全排列——一个都不丢、一个都不重。

        ★R250 item4 补第 ④ 条：**存档里有、现场缺席**的人（切分类时被我们自己
        藏起来的那批）此刻不站任何格子 ⇒ 既不参与排程（不当障碍、不进死锁分支），
        也不丢——原样接在顺序末尾，③ 的全排列性质保持不变。
        """
        names = list(targets.keys())
        if not targets or not live_pos:
            return names, []
        _pts = list(targets.values()) + list(live_pos.values())
        geom = self._grid_geometry(view, _pts)
        want = {n: self._cell_index(p, geom) for n, p in targets.items()}
        cur = {n: self._cell_index(p, geom) for n, p in live_pos.items()
               if n in targets}
        # 不参与搬动的人：它的格子从头到尾都是障碍
        static = {self._cell_index(p, geom) for n, p in live_pos.items()
                  if n not in targets}
        # ★R250 item4（用户 42 批 ④「自动还是有问题」的硬伤）：`cur` 只装得下
        #   "现场真有"的人，而 `remaining` 是**存档全表**——切分类时被我们藏起来的
        #   那批图标（win11 实测：存档 76 条 / 桌面此刻只有 36 条）在下面那句
        #   `cur[m]` 上当场 KeyError ⇒ restore_layout 在**写入循环之前**就整轮抛掉，
        #   一个图标都没摆（win11 12:06:38 日志：`匹配 36/76` 紧接
        #   `恢复布局失败: 'J荐片高清影音'`，且**没有** `已移动 N 个图标` 那一行）。
        #   缺席的人不站格子、也不必搬：从排程里摘出来（不当障碍、不进死锁分支），
        #   最后原样接在 order 末尾 ⇒ 口径③「全排列一个不丢、一个不重」不受影响，
        #   真正写坐标那圈本来就有 `idx is None ⇒ continue` 兜着。
        present = [n for n in names if n in cur]
        absent = [n for n in names if n not in cur]
        if absent:
            logger.debug("[order] 存档里 %d 个图标此刻不在桌面上（多半被分类藏着），"
                         "不参与排程：%s" % (len(absent), absent[:8]))
        remaining, order, bumped = list(present), [], []
        written = set()                      # 已写出去的人占的格
        while remaining:
            ready = [n for n in remaining
                     if want[n] not in static
                     and want[n] not in written
                     and want[n] not in {cur[m] for m in remaining if m != n}]
            if not ready:
                # 死锁（真·互换/环）：挑一个写明"注定被撞"，其余人质照常解
                n = remaining[0]
                bumped.append(n)
                order.append(n)
                written.add(want[n])
                remaining.remove(n)
                continue
            for n in ready:
                order.append(n)
                written.add(want[n])
                remaining.remove(n)
        if bumped:
            logger.warning("🔀 %d 个图标的落点被'互换格子'式的死锁占着，"
                           "只能靠读回补发收敛：%s" % (len(bumped), bumped[:8]))
        return order + absent, bumped

    def wait_positions_settle(self, timeout: float = 1.2, interval: float = 0.1,
                             need: int = 2) -> bool:
        """等 Explorer 把图标位置落定（连续 `need` 次读数完全相同）。

        ★R237：`_show_listview()` 之后 Explorer 会自己重排一轮，R84 的显示态
        补套若立刻开写，就是**和它抢格子**（这正是"第 2 遍全量写被撞 9 次"的
        来路）。补套前等它落定即可。到 `timeout` 仍不稳 ⇒ 返回 False 照样往下走
        ——绝不能卡住 UI 线程。
        """
        deadline = time.time() + float(timeout)
        try:
            prev = dict(self._read_all_positions_live() or {})
        except Exception:
            return False
        stable = 0
        while time.time() < deadline:
            time.sleep(max(0.01, float(interval)))
            try:
                cur = dict(self._read_all_positions_live() or {})
            except Exception:
                return False
            if cur and cur == prev:
                stable += 1
                if stable >= max(1, int(need) - 1):
                    return True
            else:
                stable = 0
            prev = cur
        return False

    def offscreen_names(self, icons: dict, view=None) -> list:
        """★R235：按**虚拟屏**判屏外（旧口径只按主屏，双屏机器上副屏全中枪）。"""
        vx, vy, vw, vh = view or self._virtual_screen_rect()
        return [n for n, (x, y) in icons.items()
                if x < vx or y < vy or x >= vx + vw or y >= vy + vh]

    def classify_positions(self, target: dict, live: dict, tol: int = 4):
        """读回验证分类：落位 / 真偏差 / 桌面上没有（缺席）。

        旧实现把"缺席"算进偏差（挂账 #101）⇒ 日志永远红着 7 个，真偏差被淹没。
        """
        ok, dev, absent = [], [], []
        for n, (tx, ty) in target.items():
            p = live.get(n)
            if p is None:
                absent.append(n)
            elif abs(p[0] - tx) <= tol and abs(p[1] - ty) <= tol:
                ok.append(n)
            else:
                dev.append(n)
        return ok, dev, absent

    def save_layout(self, name: str, sync_full_cache: bool = False,
                    source: str = 'manual') -> dict:
        """保存当前布局。

        sync_full_cache=True: 仅把 _position_cache（含全部可见/隐藏图标）同步到备份文件，
        不重写缓存——由调用方自己决定是否 restore_layout。
        用于 show_all / 切换分类前先"写缓存到文件"的场景，避免 save_layout 只读可见图标
        把缓存里已保存的隐藏图标位置覆盖掉。
        """
        if sync_full_cache:
            # 只写缓存，不读 ListView，不覆盖缓存
            if not self._position_cache:
                return {"success": False, "error": "缓存为空"}
            # ★R235：写盘前先剪幽灵——这条路径以前是"缓存里有啥就抄啥"，
            #   于是 restore 灌回来的已删除图标名被一遍遍复刻进新存档
            #   （win11 实测：桌面 75 个，存档永远 82 条）。
            _ghosts = self.prune_ghost_positions('save:全部同步')
            if not self._position_cache:
                return {"success": False, "error": "缓存清洗后为空"}
            # ★R299 N1：写盘前按格子去重。以前 dedupe 只在**回放时**做，同格
            #   坐标照样被原样抄进新存档 ⇒ "存档→缓存→新存档"一路复刻，win11
            #   挂机日志每恢复一次就打一条"🧹 布局里有 N 个图标挤在同一格"。
            #   清洗只作用于**写出去的那份**：_position_cache 保持桌面真实读数
            #   （它同时是守护/读回验证的基准，抹平它会造成假位移）。
            _clean, _moves = self.dedupe_layout_cells(dict(self._position_cache))
            # ★R312 item1：存档不认领桌面占位文件（「全部」那份 79/78 交替就是它引起的）
            _clean = self._strip_placeholders(_clean, '存档(全部同步)')
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{name}_{timestamp}.json"
            filepath = os.path.join(self.backup_dir, filename)
            data = {
                "timestamp": datetime.now().isoformat(),
                "name": name,
                "source": source,
                "screen": self._screen_stamp(),
                "icon_count": len(_clean),
                "icons": _clean,
            }
            # ★R245 A2：尺寸跟着存档（读不到＝None ⇒ 干脆不写这个键，
            #   比写个 null 好：回放时"没这一项"是老存档的正常情况）
            _isz = self.get_icon_size()
            if _isz is not None:
                data["icon_size"] = _isz
            # ★R246 B5-①：**当初谁在管桌面**跟着存档（纯只读）。读不到 ⇒ 整个键
            #   都不写。为什么值这个钱：★R244 #179 之后判读一份存档，光看坐标
            #   分不出"用户摆的"还是"排完序抄的"，win11 回传那份只能靠猜。
            _vs = self._view_stamp()
            if _vs:
                data["view_state"] = _vs
            # ★R247（#109/#131）：**同名图标多出来的坐标**跟着存档。主表按名字
            #   只装得下一条，另一条以前在这一步就没了 ⇒ 恢复时有一个同名图标
            #   摆不回原位。没有同名 ⇒ 整个键都不写（老版本/老存档互不影响）。
            _dc = self._dup_cells_for_save(self._position_cache)
            if _dc:
                data["dup_cells"] = _dc
                logger.info("📦 这份布局里有 %d 组同名图标，多出来的坐标一并记进 "
                            "dup_cells（%s）" % (len(_dc), list(_dc)[:4]))
            try:
                import collections as _cl
                from core import sort_clear as _sc
                _dups = [p for p, c in _cl.Counter(
                    tuple(v) for v in self._position_cache.values()).items() if c > 1]
                # ★R299 N1：同格的事实在此只留痕，不再另发一条 WARNING——
                #   dedupe_layout_cells 自己已经把顺延的名单点出来了
                _layout_logger.info(
                    f"[save] name={name} full=True n={len(_clean)} "
                    f"screen={data['screen']} {_sc.stamp_text(_vs)} "
                    f"剔除幽灵={len(_ghosts)} {_ghosts[:8]} "
                    f"重复坐标={len(_dups)} {_dups[:4]} 写盘前顺延={len(_moves)}")
            except Exception:
                pass
            try:
                if _atomic_save_json(filepath, data):
                    logger.info(f"已同步完整缓存到布局: {filename}, 共 {len(self._position_cache)} 个图标")
                    self.saved_layout_name = name
                    self._prune_layout_backups(name)
                    return {"success": True, "count": len(self._position_cache), "file": filename}
                return {"success": False, "error": "写入失败"}
            except Exception as e:
                logger.error(f"保存布局失败: {e}")
                return {"success": False, "error": str(e)}

        icons = self.get_current_icon_positions()
        # ★R246 B5-①：读数只取一次，日志与存档用同一份（两条路径同源）
        _vs = self._view_stamp()
        # ★ R86：保存留痕（屏幕环境 + 异常坐标检测：重复/屏外）
        try:
            _sx = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
            _sy = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)
            _vw = win32api.GetSystemMetrics(78)
            _vh = win32api.GetSystemMetrics(79)
            import collections as _cl
            from core import sort_clear as _sc
            _dups = [p for p, c in _cl.Counter(
                tuple(v) for v in icons.values()).items() if c > 1]
            _off = [n for n, (x, y) in icons.items()
                    if x < 0 or y < 0 or x >= _vw or y >= _vh]
            # ★R234：留痕带上"自动排列"状态。开着自动排列时读回来的坐标是
            #   系统打包后的网格位，不是用户摆放位——存进布局文件就成了
            #   "污染布局"（下次恢复把网格当原样搬回去）。有这一列才能事后判断
            #   某份布局是在哪种状态下抄的（win11 那台机器的历史布局需按此筛）。
            _aa = self.get_auto_arrange()
            _layout_logger.info(
                f"[save] name={name} full={sync_full_cache} n={len(icons)} "
                f"主屏={_sx}x{_sy} 虚拟屏={_vw}x{_vh} "
                f"autoarrange={_aa} {_sc.stamp_text(_vs)} "
                f"重复坐标={len(_dups)} {_dups[:4]} 屏外={len(_off)} {_off[:5]}")
            if _aa and source == 'manual':
                logger.warning(
                    "保存布局 [%s] 时桌面开着\"自动排列图标\"：存进去的是系统打包后的"
                    "网格位置，不是用户摆放位置（布局可能污染）" % name)
        except Exception:
            pass
        # 安全检查：ListView 读回来的图标名如果为空（Explorer 刷新延迟导致），
        # 直接放弃保存，避免用残缺数据覆盖 _position_cache。
        # 调用方（如 auto_backup）应重试或改用 sync_full_cache 模式。
        if not icons:
            logger.warning("save_layout: ListView 返回 0 个图标（可能 Explorer 正在刷新），跳过保存")
            return {"success": False, "error": "ListView 无图标"}
        # 同时更新缓存（确保 save 后缓存与文件一致）
        self._position_cache.clear()
        self._position_cache.update(icons)
        # ★R299 N1：与"保存全部"同口径——写出去的那份先按格子去重，斩断
        #   "存档→缓存→新存档"的同格复刻；缓存保持本次桌面的真实读数。
        _clean, _moves = self.dedupe_layout_cells(dict(icons))
        # ★R312 item1：与"保存全部"同口径——两条写盘路径只堵一条＝排行组好了、
        #   「全部」照旧每轮把它摆回存档记的那格。缓存不动（保持真实读数）。
        _clean = self._strip_placeholders(_clean, '存档(手动)')

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{name}_{timestamp}.json"
        filepath = os.path.join(self.backup_dir, filename)
        
        data = {
            "timestamp": datetime.now().isoformat(),
            "name": name,
            "source": source,
            "screen": self._screen_stamp(),
            "icon_count": len(_clean),
            "icons": _clean
        }
        # ★R245 A2：两条写盘路径都带尺寸（只带一条＝"有时记有时不记"，
        #   回放时更没法判读）；读不到就不写这个键。
        _isz = self.get_icon_size()
        if _isz is not None:
            data["icon_size"] = _isz
        # ★R246 B5-①：同上，两条路径都带"当初谁在管桌面"（读不到 ⇒ 不写这个键）
        if _vs:
            data["view_state"] = _vs
        # ★R247：同上，两条写盘路径都带同名附加坐标（没同名 ⇒ 不写这个键）。
        #   只带一条＝"有时记有时不记"，恢复时更没法判读。
        _dc = self._dup_cells_for_save(icons)
        if _dc:
            data["dup_cells"] = _dc
            logger.info("📦 这份布局里有 %d 组同名图标，多出来的坐标一并记进 "
                        "dup_cells（%s）" % (len(_dc), list(_dc)[:4]))

        try:
            if _atomic_save_json(filepath, data):
                logger.info(f"已保存布局: {filename}, 共 {len(icons)} 个图标")
                self.saved_layout_name = name
                self._prune_layout_backups(name)
                return {"success": True, "count": len(icons), "file": filename}
            return {"success": False, "error": "写入失败"}
        except Exception as e:
            logger.error(f"保存布局失败: {e}")
            return {"success": False, "error": str(e)}
    
    def _apply_dup_cells(self, dup_cells, live_items=None) -> int:
        """★R247：把同名图标"多出来的那些"坐标补回原位（**按索引写**，不动别人）。

        主表 `icons` 按名字只能带一条坐标 ⇒ 桌面上站着两个同名图标时，恢复
        永远只有一个回得了原位（#109/#131 用户看到的"有一个就是摆不对"）。
        这里读附加字段 `dup_cells={名字: [[x,y],…]}`：对每个名字取**现场同名**
        的那几条 (名字, 索引, 坐标)，就近配对后按**索引**写回。三条硬口径：
          · 名字在桌面上找不到 ⇒ 丢弃并留痕，**绝不把坐标塞给别的图标**；
          · 已在目标位（≤2px）⇒ 不重复写，省一次无谓重排；
          · 老存档没这个字段 ⇒ 一次写入都不许多发（零改动）。
        返回实际补位条数（可判读，不许静默）。
        """
        if not dup_cells:
            return 0
        try:
            items = (list(live_items) if live_items is not None
                     else list(self.get_live_icon_items() or []))
        except Exception as e:
            logger.debug("[dup] 现场读数失败: %s" % e)
            items = []
        n = skipped = ph = 0
        for name, cells in (dup_cells or {}).items():
            # ★R313 A1：★R312 只挡了**写侧**（`_dup_cells_for_save`），可 R030 之前
            #   存的档里 `dup_cells` 仍带着 desktop.ini 那一枚（本文件 :193 注释
            #   自述"win11 回传存档里 desktop.ini 也成对"）——这条按索引补位的出口
            #   一旦读到它，就把占位文件摆回主屏可见格，用户点名的症状在旧存档上
            #   原样复发，日志还写成"补位 N 个"。判据与写侧同一个名字口径。
            if self._is_placeholder_name(name):
                ph += 1
                continue
            try:
                targets = [(int(p[0]), int(p[1])) for p in cells or []
                           if isinstance(p, (list, tuple)) and len(p) == 2]
            except Exception:
                targets = []
            if not targets:
                continue
            live = [it for it in items if it and it[0] == name]
            if not live:
                logger.info("🧹 同名补位：桌面上找不到「%s」这个图标，"
                            "存档里 %d 条附加坐标本次不写" % (name, len(targets)))
                continue
            for idx, (tx, ty) in _pair_dup_cells(name, targets, live):
                cur = next((it[2] for it in live if it[1] == idx), None)
                if cur is not None and abs(int(cur[0]) - tx) <= 2 \
                        and abs(int(cur[1]) - ty) <= 2:
                    skipped += 1
                    continue
                try:
                    if self.set_icon_position(int(idx), tx, ty):
                        n += 1
                except Exception as e:
                    logger.debug("[dup] 补位失败 icon=%s err=%s" % (name, e))
        if ph:
            logger.info("🧹 同名补位：跳过 %d 个桌面占位文件（desktop.ini/Thumbs.db——"
                        "系统自己写的，不是用户摆的东西，旧存档里的附加坐标本次不写）"
                        % ph)
            _layout_logger.info("[dup] 跳过占位文件=%d（★R313 A1 读侧出口）" % ph)
        if n or skipped:
            logger.info("🧹 同名图标补位：按索引写了 %d 个、%d 个本来就在位"
                        "（不靠名字，所以两个同名能各回各的格子）" % (n, skipped))
            _layout_logger.info("[dup] 补位=%d 在位=%d 名字=%s"
                                % (n, skipped, list(dup_cells)[:6]))
        return n

    def restore_layout(self, filepath: str) -> dict:
        """恢复指定布局

        关键：每次恢复前强制重新从 ListView 读取图标索引映射，
        不依赖可能过时的 _position_cache。

        ★ R66c：恢复期间冻结高亮采集线程的"实时坐标→缓存"写回
        （_pos_sync_freeze_until）——否则采集可能读到搬运途中的坐标
        污染缓存，随后的 realign 校正再把图标搬回旧位（用户实测"点
        全部后部分图标还在分屏"；管理页恢复无 realign 所以正常）。
        """
        self._pos_sync_freeze_until = time.time() + 4.0
        if not os.path.exists(filepath):
            return {"success": False, "error": "文件不存在"}

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)

            saved_icons = data.get("icons", {})
            if not saved_icons:
                return {"success": False, "error": "布局文件为空"}
            # 坐标一律规整成 int 二元组（外部导入/手改过的存档可能带 float）
            saved_icons = {n: (int(p[0]), int(p[1])) for n, p in saved_icons.items()
                           if isinstance(p, (list, tuple)) and len(p) == 2}

            logger.info(f"[restore_layout] 开始恢复 {filepath}, 备份中有 {len(saved_icons)} 个图标")
            # ★R238（挂账 #123 收口）：把桌面视图的排序接管状态一并记进日志。
            #   只读 HKCU\...\Shell\Bags\1\Desktop 的 Sort/FFlags，不写、不发消息。
            #   为什么必须记：视图一旦被"排序方式"钉住，本函数写进去的坐标
            #   当场生效但用户一刷新就被打回（win10 实测 16/20 → 5/20），
            #   以前日志里完全没有这个字段，导致"刷新变乱"无从判读。
            try:
                from core import desk_view_state as _dvs
                logger.info("[restore_layout] %s",
                            _dvs.summarize(_dvs.read_desk_view_state()))
            except Exception as _e:
                logger.debug("[restore_layout] desk view state skipped: %s", _e)
            # ★R246 B5-①/③：把"**存档当时**"和"**此刻**"两份视图状态并排留痕。
            #   判读"套完还是乱"只有两条线索：是当初抄进来的就是乱的（存档侧），
            #   还是这台机器此刻又在接管（现值侧）。老存档没这一项＝正常，
            #   `stamp_from_archive` 给 {} ⇒ 日志里就是 archive=view=unknown，
            #   **绝不拿"此刻"冒充"当初"**（#179 那份存档没法判读就是这个亏）。
            _vs_arch, _vs_now = {}, {}
            try:
                from core import sort_clear as _sc
                _vs_arch = _sc.stamp_from_archive(data)
                _vs_now = self._view_stamp()
                logger.info("[restore_layout] archive=%s now=%s",
                            _sc.stamp_text(_vs_arch), _sc.stamp_text(_vs_now))
                if _vs_arch.get('sorted'):
                    logger.warning("这份布局记录时桌面正按「%s」排序：存进去的就是"
                                   "排完序的位置，套回后一刷新会被打回"
                                   "（不是本次恢复失败）" % _vs_arch.get('sort_column'))
                elif _vs_arch.get('autoarrange'):
                    logger.warning("这份布局记录时桌面开着「自动排列图标」：存进去的"
                                   "是系统网格位，套回后会被重新打包"
                                   "（不是本次恢复失败）")
            except Exception as _ve2:
                logger.debug("[restore_layout] view stamp skipped: %s", _ve2)

            # ★R250 ④（用户 42 批：「后来我点"全部"这个选项卡也不好使了，最后还是
            #   点菜单里那个「立即修复桌面排序」才好使」）：**每次摆布局之前**都先
            #   安静地把活动排序清掉，不再只挂在"切分类"那一个入口（★R249 挂账 #208
            #   当初就写着"以后要是发现别的入口也常摆完又被打回，从这条扩"——现在
            #   就是那个"以后"）。win11 12:09:07 那次"显示全部"补套的读数就是证据：
            #   `匹配 76/76`＋`已移动 76 个图标` 位置全写进去了，但 `now=view=
            #   sort:prop:system.itemnamedisplay` 排序还开着 ⇒ 一刷新又被打回。
            #   · 只走免鼠标官方接口，一次鼠标都不发、不弹框（★R249 同一条红线）；
            #   · 没排序/读不到 ⇒ 一个字节都不写（`silent_clear_for_layout` 自己管）；
            #   · 切分类那条路前面 gui 已注入过一次（dock.py:1494），这里再问一次
            #     只是"已经清过⇒读不到排序⇒空转"，不会重复写视图；
            #   · 任何异常吞成不试，摆布局照旧。
            try:
                from core import sort_clear as _sc0
                _sc0.silent_clear_for_layout(where='摆布局')
            except Exception as _sce:
                logger.debug("[restore_layout] 静默清排序跳过: %s" % _sce)

            # ★R245 A2：**先尺寸后坐标**。图标尺寸一换，格子间距就跟着变，
            #   尺寸的读写必须排在套位之前，否则整份布局会按旧间距错格。
            #   老存档没有这一项＝正常情况，静默跳过（绝不擅自改用户尺寸）。
            try:
                _szr = self._apply_archived_icon_size(data)
                if _szr.get('changed'):
                    logger.info("🔎 已按存档把桌面图标尺寸从 %s 调成 %s，"
                                "再套坐标（★R245 A2）"
                                % (_szr.get('from'), _szr.get('to')))
                    time.sleep(0.6)     # 给 Explorer 一次按新尺寸重排的时间
                elif _szr.get('err') not in ('no-icon-size', 'already-same'):
                    logger.info("🔎 图标尺寸未回放（%s，存档值=%s）"
                                % (_szr.get('err'), data.get('icon_size')))
            except Exception as _sze:
                logger.debug("[restore_layout] icon size skipped: %s", _sze)

            # ★ 强制重新从 ListView 读取图标索引，不依赖缓存
            current_map = self._read_icon_map_from_listview()
            logger.info(f"[restore_layout] 当前桌面有 {len(current_map)} 个图标")

            # ── ★R235/★R236 四步清洗：剪幽灵 → 读实时坐标 → 拆同格 → 避新人 ──
            saved_icons, ghosts = self.filter_ghost_names(
                saved_icons, live_names=set(current_map.keys()))
            if ghosts:
                logger.info("🧹 这份布局里有 %d 个图标桌面上已经不存在了，"
                            "本次不套位、也不再抄回缓存：%s"
                            % (len(ghosts), ghosts[:8]))
            # ★R236：恢复前先读一遍实时坐标——"存档里根本没记的新图标"
            #   此刻站的那格要当保留格，既不往里套位也不往里顺延。
            _view = self._virtual_screen_rect()
            try:
                _live_before = dict(self._read_all_positions_live() or {})
            except Exception:
                _live_before = {}
            # ★R309（#361）：先请桌面占位文件（desktop.ini / Thumbs.db，只在"显示
            #   隐藏文件"开着时才进 ListView）让开。真机 R026 日志里 29 条 🧭 全是
            #   它当"新人"把 此电脑/HashKit/J荐片高清影音 顶到旁边空格。挪完了
            #   **重读一遍实时坐标**——下面的拆同格/避新人/排写入顺序全都吃这份读数，
            #   不重读就等于拿"占位文件还站在真图标家里"的旧账去摆桌。
            saved_icons, _park = self.park_placeholder_icons(
                saved_icons, _live_before, view=_view)
            if _park:
                try:
                    _live_reread = dict(self._read_all_positions_live() or {})
                except Exception:
                    _live_reread = {}
                if _live_reread:
                    _live_before = _live_reread
            _newcomer_cells = {tuple(p) for n, p in _live_before.items()
                               if n not in saved_icons}
            saved_icons, dup_moves = self.dedupe_layout_cells(
                saved_icons, view=_view, reserved=_newcomer_cells)
            saved_icons, conflicts = self.avoid_newcomer_cells(
                saved_icons, _live_before, view=_view)
            # 屏外：按虚拟屏判（双屏机器上副屏是正常位，旧口径按主屏算全是误报）
            _off = self.offscreen_names(saved_icons, view=_view)
            _stamp = _screen_stamp_from_archive(data)
            if _off:
                logger.warning(
                    f"[布局检查] {os.path.basename(filepath)} 有 "
                    f"{len(_off)}/{len(saved_icons)} 个图标坐标在虚拟屏"
                    f"（{_view[2]}x{_view[3]}）之外（如 {_off[:3]}）——"
                    '多半是这套屏和存档那套屏不一样（换过显示器/远程桌面）。'
                    '若非有意安排，恢复后点"保存"覆盖一次即可根治')
            elif _stamp and list(_stamp.get('virtual') or []) not in ([], list(_view)):
                _layout_logger.info(
                    f"[restore] 存档屏幕指纹={_stamp.get('virtual')} "
                    f"当前虚拟屏={list(_view)}（不同≠异常，仅留痕）")

            # 统计匹配情况（★R235 教训：这里以前只打 unmatched[:5]，
            #  取证时因此少数过 2 个幽灵——名单一律打全，超过 12 个才截断并标注总数）
            matched = sum(1 for name in saved_icons if name in current_map)
            unmatched = [name for name in saved_icons if name not in current_map]
            logger.info(f"[restore_layout] 匹配 {matched}/{len(saved_icons)}, "
                        f"未匹配 {len(unmatched)} 个: "
                        f"{unmatched if len(unmatched) <= 12 else unmatched[:12] + ['…']}")

            # 移动到保存的位置
            # 注意：不跳过"已在位置"的图标——实时读数自 2026-09-02 起已可信
            # （旧结论"Progman 不响应 LVM_GETITEMPOSITION"是读取方式错了），
            # 但恢复是低频动作，强制重设全部图标更稳。
            moved = 0
            # ★R234：见 apply_positions 同处注释——自动排列开着 ⇒ 位置写不进，
            # 恢复必须先把这道闸门落地（历轮发的 0x1039 是命中测试，从没生效）。
            _aa = self.ensure_auto_arrange_off()
            if _aa != 'off':
                logger.warning(f"[restore_layout] 自动排列闸门={_aa}"
                               f"｜{self.auto_arrange_block_reason()}")
                _layout_logger.info(f"[restore] 自动排列闸门={_aa}")
            send_msg_timeout(self.hwnd_listview, WM_SETREDRAW, 0, 0)
            # ★R237：写入顺序 = 碰撞安全顺序（先搬"落点此刻是空的"那个）。
            #   旧写法按存档字典序遍历，等于把整列人排成"依次去站别人正站着的
            #   格子"⇒ Explorer 逐个就近顺延，一次恢复歪掉 9 个（win11 第 5 份日志）。
            _order, _bumped = self.plan_write_order(saved_icons, _live_before,
                                                    view=_view)
            _layout_logger.info(
                f"[order] 碰撞安全顺序={len(_order)} 注定被撞={len(_bumped)} "
                f"{_bumped[:8]} 实时样本={len(_live_before)}")
            dup_laid = 0
            try:
                for name in _order:
                    x, y = saved_icons[name]
                    idx = current_map.get(name)
                    if idx is None:
                        continue
                    # ★ 移除"已在位置则跳过"的判断，强制移动所有图标
                    lparam = pack_lparam(x, y)
                    if send_msg_timeout(
                        self.hwnd_listview, LVM_SETITEMPOSITION, idx, lparam
                    ) is None:
                        logger.warning("[restore_layout] SETITEMPOSITION 超时，中止本轮恢复")
                        break
                    self._position_cache[name] = (x, y)
                    moved += 1
                # ★R247（#109/#131）：主表套完之后才补同名——顺序不能反：主表
                #   先把"占主位"的那个摆回存档坐标，剩下的同名才就近认领自己
                #   的格子（反过来会拿旧位置配对，等于白补一趟）。
                #   读现场必须**重新读**：上面那一轮写入已经改变了每个索引站在
                #   哪儿，用 _live_before 配对会配错人。
                try:
                    dup_laid = self._apply_dup_cells(
                        data.get("dup_cells") or {},
                        live_items=self.get_live_icon_items())
                except Exception as _dce:
                    logger.debug("[restore_layout] dup cells skipped: %s" % _dce)
            finally:
                # ★R190（Qoder CORE-3）：任何异常都必须解冻，防 ListView 永久冻结
                send_msg_timeout(self.hwnd_listview, WM_SETREDRAW, 1, 0)
                win32gui.InvalidateRect(self.hwnd_listview, None, True)

            logger.info(f"[restore_layout] 已移动 {moved} 个图标到保存位置")
            _layout_logger.info(
                f"[restore] {os.path.basename(filepath)} 匹配={matched}/{len(saved_icons)} "
                f"移动={moved} 幽灵={len(ghosts)} 同格={len(dup_moves)} "
                f"避新人={len(conflicts)} 占位挪位={len(_park)} 同名补位={dup_laid}")

            # ★ R86：读回验证——消息已发送≠已生效。经远程缓冲读回真实
            # 坐标逐图标比对（±4px），偏差项补发一遍并复检，全部留痕。
            # ★R235：缺席（桌面上没有）单列，不再混进"偏差"（挂账 #101）
            try:
                _live = self._read_all_positions_live()
                _ok, _failed_names, _absent = self.classify_positions(saved_icons, _live)
                _layout_logger.info(
                    f"[verify] 目标={len(saved_icons)} 落位={len(_ok)} "
                    f"偏差={len(_failed_names)} 缺席={len(_absent)} {_absent[:6]} "
                    f"首批偏差={[(n, saved_icons[n], _live.get(n)) for n in _failed_names[:6]]}")
                if _failed_names:
                    # ★R246 B5-③：偏差先**归因**再补发（openFrence 那条写法）。
                    #   用户看到"图标又乱了"第一反应是存档坏了，历轮沟通一半时间
                    #   花在解释"是系统在接管"上——这一句把方向说在前面。
                    _rp = self.drift_report()
                    _layout_logger.info(f"[verify] 归因={_rp['cause']} {_rp['text']}")
                    logger.warning("套完布局有 %d 个图标没落到存档位置——%s"
                                   % (len(_failed_names), _rp['text']))
                    _cmap2 = self._read_icon_map_from_listview()
                    for _n in _failed_names:
                        _idx = _cmap2.get(_n)
                        if _idx is not None:
                            _tx, _ty = saved_icons[_n]
                            send_msg_timeout(self.hwnd_listview,
                                             LVM_SETITEMPOSITION, _idx,
                                             pack_lparam(_tx, _ty))
                    _live2 = self._read_all_positions_live()
                    _ok2, _still, _ = self.classify_positions(saved_icons, _live2)
                    _layout_logger.info(
                        f"[verify2] 补发后仍偏差={len(_still)} 归因={_rp['cause']} "
                        f"明细={[(n, saved_icons[n], _live2.get(n)) for n in _still[:10]]}")
            except Exception as _ve:
                _layout_logger.info(f"[verify] 读回验证异常: {_ve}")
            # 布局守护：以该布局为基准，防远程重连导致的显示拓扑回退
            # ★R235：基准传清洗后的集合，不再原样 copyfile 脏存档
            #（否则守护把两个图标拉回同一格、或按幽灵名单空转）
            self.arm_guard(filepath, icons=saved_icons)
            # 恢复时从文件名里提取布局名（文件名格式：<name>_<timestamp>.json）
            try:
                import re as _re
                fname = os.path.basename(filepath)
                m = _re.match(r'^(.+?)_\d{8}_\d{6}\.json$', fname)
                self.saved_layout_name = m.group(1) if m else fname
            except Exception:
                pass
            # 更新位置缓存：用刚恢复的位置覆盖旧缓存
            # ★R235：只回写"桌面上真有"的名字——旧写法把整份存档（含幽灵）
            #   灌回缓存，随后的 save_layout(sync_full_cache) 再原样抄进新存档，
            #   于是 win11 那台机器桌面 75 个图标、存档永远 82 条。
            self._position_cache.update({n: p for n, p in saved_icons.items()
                                         if n in current_map})
            self.prune_ghost_positions('restore:套位后', live_names=set(current_map.keys()))
            return {"success": True, "restored": moved, "matched": matched,
                    "ghosts": ghosts, "dups": dup_moves, "offscreen": _off,
                    "conflicts": conflicts, "bumped": _bumped,
                    # ★R247：同名图标按索引补回的条数（0＝没同名/老存档没这项）
                    "dup_laid": dup_laid}

        except Exception as e:
            logger.error(f"恢复布局失败: {e}")
            return {"success": False, "error": str(e)}

    def wait_for_stable(self, expected_count: int, max_retries: int = 5, interval: float = 0.3) -> bool:
        """等待 Explorer 稳定：轮询图标索引映射，直到匹配率稳定

        Args:
            expected_count: 预期的图标总数（来自备份文件）
            max_retries: 最大重试次数
            interval: 每次检查的间隔（秒）

        Returns:
            是否在限次内达到稳定状态
        """
        import time as _time
        for i in range(max_retries):
            current_map = self._read_icon_map_from_listview()
            actual_count = len(current_map)
            logger.info(f"[wait_for_stable] 第{i+1}次检查: 当前{actual_count}个图标 (预期{expected_count})")

            if actual_count == expected_count:
                logger.info(f"[wait_for_stable] 已稳定！")
                return True

            if i < max_retries - 1:
                _time.sleep(interval)

        logger.warning(f"[wait_for_stable] 未达到稳定，最终{actual_count}个图标")
        return False
    
    def suppress_guard(self, seconds: float = 8.0):
        """程序套用布局时临时抑制守护（大批移动会被误判为系统回退）"""
        self._guard_suppress_until = time.time() + seconds
        self._guard_snapshot = None   # 抑制结束后以新位置重建基线

    def rebase_guard(self):
        """★R272：以"现在的位置"为守护新基线，不拉回、不抑制。

        监视层睡死期间守护也不巡查（`check_guard` 属于重采层，含全表跨进程读）。
        醒来那一拍若拿休眠前的旧快照去比，用户在休眠期间自己挪的图标会被判成
        "瞬间大面积跳变（≥25% 且 ≥5 个）"⇒ 守护把它们**拽回旧位**，等于程序
        替用户撤销他刚做的整理。★R234 定案：用户摆过要让位，宁可漏纠不可错纠。
        与 `suppress_guard` 的区别：这条不设抑制期，下一拍立刻按新基线继续守护。
        """
        self._guard_snapshot = None

    def arm_guard(self, filepath: str, layout_name: str = '', icons: dict = None):
        """设置布局守护基准（仅当守护开启时生效，gui 从设置注入开关）。

        ★ R66c：基准改存独立 live 文件（_guard_live.json），不再把用户
        保存的布局文件直接当基准——此前"用户整理完毕"会把拖动后的位置
        合并回被武装的布局文件：临时把图标挪去副屏测试，保存的布局就被
        静默改写，之后每次套用都把图标搬去分屏（2026-09-15 用户实测）。
        live 文件跟随整理演进（守护拉回仍用最新摆放，防拓扑回退不丢位置）；
        用户保存的布局文件只由"保存/保存全部"显式写入。

        ★R235：icons 传入时（restore_layout 已剪幽灵+拆同格的集合）直接写
        live 基准，不再 copyfile 原存档——否则守护会把两个图标拉回同一格。"""
        if not self.guard_enabled:
            return
        try:
            live = os.path.join(os.path.dirname(filepath), '_guard_live.json')
            if icons is not None:
                data = {"timestamp": datetime.now().isoformat(),
                        "name": layout_name or os.path.basename(filepath),
                        "source": "guard",
                        "screen": self._screen_stamp(),
                        "icon_count": len(icons), "icons": dict(icons)}
                if _atomic_save_json(live, data):
                    self._guard_path = live
                else:
                    raise OSError('_guard_live.json 写入失败')
            elif os.path.abspath(filepath) != os.path.abspath(live):
                import shutil as _sh
                _sh.copyfile(filepath, live)
                self._guard_path = live
            else:
                self._guard_path = live
        except Exception as e:
            logger.error(f"守护 live 基准初始化失败（退回原文件）: {e}")
            self._guard_path = filepath
        self._guard_snapshot = None   # 首次 check 时建立快照
        if layout_name:
            self.saved_layout_name = layout_name

    def disarm_guard(self):
        self._guard_path = None
        self._guard_snapshot = None

    def check_guard(self):
        """布局守护检测（由后台监视线程每 2 秒调用一次，可安全跨进程）。

        状态机（用户拖图标是常规操作，必须自动避让）：
        - 上轮静止 + 本轮瞬间大面积跳变（≥25% 且 ≥5 个、位移>32px）
          = 刷新/拓扑切换回退 → 拉回基准布局；
        - 上轮有移动（用户正在整理）→ 暂停守护 60 秒并跟随用户进度，
          停止拖动后下一轮自动重新武装。"""
        msgs = []
        if not self.guard_enabled or not self._guard_path:
            return msgs
        # "整理桌面"模式：用户要大规模拖图标，守护完全闭嘴（2026-09-03 用户定案）
        if getattr(self, 'desktop_tidy_mode', False):
            self._guard_snapshot = None   # 退出模式后重建基线
            return msgs
        # ★ 程序正在套用布局（或刚套用完）：大批移动是程序所为，不是系统回退，
        #   抑制期间守护直接放行（2026-09-03 修复：守护与切换机制互相打架）
        if time.time() < self._guard_suppress_until:
            self._guard_snapshot = None   # 抑制结束后重建快照基线
            return msgs
        if not os.path.exists(self._guard_path):
            self._guard_path = None
            self._prev_moved = 0
            return msgs
        try:
            with open(self._guard_path, 'r', encoding='utf-8') as f:
                baseline = json.load(f).get('icons') or {}
            current = self.get_current_icon_positions()
            if not current or not baseline:
                return msgs
            if self._guard_snapshot is None:
                self._guard_snapshot = dict(current)
                self._prev_moved = 0
                return msgs

            moved = 0
            big = 0
            for n, (x, y) in current.items():
                s = self._guard_snapshot.get(n)
                if not s:
                    continue
                dx, dy = abs(x - s[0]), abs(y - s[1])
                if dx > 32 or dy > 32:
                    big += 1
                    moved += 1
                elif dx > 1 or dy > 1:
                    moved += 1
            total = len(current)

            if self._prev_moved == 0:
                # 上轮静止：本轮的瞬间大面积跳变才是"系统回退"
                if total and big >= max(5, int(total * 0.25))                         and time.time() - self._guard_last_apply > 5:
                    # ★ R66 熔断器：10 分钟内自动拉回满 3 次仍被改写，说明
                    # 有另一机制在反复写位置——继续拉回就是用户看到的"图标
                    # 反复拉扯"。此时守护自动让位（disarm），把桌面留给最后
                    # 写入方；重新保存/套用布局会自动重新武装。拉扯由此在
                    # 机制上不可能持续，且不影响正常单次拉回功能。
                    if not hasattr(self, '_guard_pulls'):
                        self._guard_pulls = []
                    _now = time.time()
                    self._guard_pulls = [t for t in self._guard_pulls
                                         if _now - t < 600]
                    self._guard_pulls.append(_now)
                    if len(self._guard_pulls) >= 3:
                        self.disarm_guard()
                        msg = ('🛡 守护熔断：10 分钟内已自动拉回 3 次图标仍被'
                               '改写，疑似与其他机制冲突——已自动停用布局守护，'
                               '防止图标反复拉扯。重新保存/套用布局会自动重新武装。')
                        msgs.append(msg)
                        self._guard_msgs.append(msg)
                        logger.warning(msg)
                        return msgs
                    target = {n: (int(x), int(y)) for n, (x, y) in baseline.items()
                              if n in current}
                    self.apply_positions(target)
                    self._guard_snapshot = {n: (int(x), int(y))
                                            for n, (x, y) in target.items()}
                    self._guard_last_apply = time.time()
                    self._prev_moved = 0
                    self._guard_notify_cb and self._guard_notify_cb()
                    msg = (f'🛡 检测到 {big}/{total} 个图标位置瞬间跳变'
                           f'（刷新/显示拓扑切换回退），已自动恢复保存的布局')
                    msgs.append(msg)
                    self._guard_msgs.append(msg)
                    logger.info(msg)
                    # ★R238：跳变现场顺手记一次视图排序状态（只读），
                    #   用来区分"Explorer 刷新时重排"与"别的东西在写坐标"。
                    try:
                        from core import desk_view_state as _dvs
                        logger.info("[guard] %s",
                                    _dvs.summarize(_dvs.read_desk_view_state()))
                    except Exception as _e:
                        logger.debug("[guard] desk view state skipped: %s", _e)
                else:
                    # 上轮静止 + 本轮有小幅移动（用户正在整理）→ 记入 _prev_moved，
                    # 下轮进入"跟随"分支；此前恒写 0 导致跟随分支永不可达（审查修复）
                    self._prev_moved = moved
                    self._guard_snapshot = dict(current)
            else:
                # 上轮有移动 → 本轮静止表示用户刚停止拖拽，更新备份后重新武装
                if moved == 0:
                    self._update_guard_backup(current)
                    logger.info("📦 用户整理完毕，已自动更新布局备份")
                self._guard_snapshot = dict(current)
                self._prev_moved = moved
        except Exception as e:
            logger.error(f"布局守护检测失败: {e}")
        return msgs

    def _update_guard_backup(self, current: Dict[str, Tuple[int, int]]):
        """将守护基准布局更新为当前图标位置（用户整理完成后调用）。

        ★ 原地合并是设计语义：布局文件跟随用户的整理持续演进，
        这样切分类时套用的才是最新摆放（审核曾试图改为"另存auto文件"，
        但那会让套用永远停在旧的手动快照上、整理结果被回滚——已回退）。
        防污染的正确做法是在程序化过渡（家长模式进出等）处压制守护，
        见 utils/parental_mode.py 的 suppress_guard 调用。"""
        if not self._guard_path:
            return
        try:
            with open(self._guard_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            # ★ 合并而非整体替换（2026-09-05 审查修复）：current 只含可见图标，
            #   整体替换会把隐藏图标的布局位置全部抹掉
            icons = data.get('icons') or {}
            for n, pos in current.items():
                if n in icons:
                    icons[n] = (int(pos[0]), int(pos[1]))
            data['icons'] = icons
            data['timestamp'] = datetime.now().isoformat()
            data['icon_count'] = len(icons)
            if _atomic_save_json(self._guard_path, data):
                self._guard_last_apply = time.time()
        except Exception as e:
            logger.error(f"自动更新布局备份失败: {e}")

    # ── ★R247：批量摆位的"基准从哪来"（修 no-baseline 死路）────────────────
    def _relay_baseline_candidates(self, extra_path: Optional[str] = None) -> list:
        """列出能当基准用的布局存档，按优先级给 [(path, 来源标记), …]，可能为空。

        为什么必须有这一段：★R245 A7 的 `relayout_from_baseline()` 原本**只认
        `self._guard_path`**，而 `_guard_path` 只在 `arm_guard()` 里赋值，
        `arm_guard()` 开头就是 `if not self.guard_enabled: return`；
        `guard_enabled` 三处全部硬置 False（本文件 __init__、
        gui/main_window.py、gui/manage_panel.py，★R83 起没有任何一处把它设回 True）
        ⇒ 四条入口（主动按钮 / 守护 / 拖动成功支 / 免鼠标 view-api 成功支）
        **每一次都撞 no-baseline**，"清完排序整桌摆回"这一步在真机上从没成功过
        （用户 2026-09-25 22:09 日志：排序清了、紧接着就是 no-baseline）。
        数据其实一直在，只是没人去读：恢复布局成功时主窗口会把这份存档路径记进
        `app_settings['last_guard_layout']`。

        口径（**宁可承认拿不到，也不猜一份布局往用户桌面上写**）：
          1) `guard`   —— 守护基准 `_guard_path`：有守护就用守护的，语义与历轮一致
          2) `last-layout` —— 调用方传进来的 `extra_path`（上次恢复/套用那份）
          3) `latest-all` —— `icon_backups/` 里文件名时间戳最新的「全部_*.json」
        时间戳在文件名里 ⇒ 排序取新（★R239 ⑲：判"存档新不新"不能看文件系统日期）。
        """
        out = []

        def _push(p, tag):
            try:
                if p and os.path.isfile(p):
                    if not any(str(t[0]).lower() == str(p).lower() for t in out):
                        out.append((p, tag))
            except Exception:
                pass

        _push(getattr(self, '_guard_path', None), 'guard')
        _push(extra_path, 'last-layout')
        try:
            files = sorted([f for f in os.listdir(self.backup_dir)
                            if f.startswith('全部_') and f.endswith('.json')],
                           reverse=True)
        except Exception:
            files = []
        for f in files[:3]:
            _push(os.path.join(self.backup_dir, f), 'latest-all')
        return out

    def relayout_from_baseline(self, baseline_path: Optional[str] = None) -> dict:
        """按**基准布局**一次性把整个桌面摆回原位（★R245 A7，★R247 修基准来源）。

        用户 37 批插话原话："干脆快速把图标通过程序的方式，把所有的图标摆成
        布局的位置，这样重启都不乱了。也不用拖回去了"。为什么这条对：
        旧写法清完排序只把**被拖的那个图标**拖回原格，其余图标还留在系统
        重排后的位置 ⇒ 现象是"修好了但桌面还是乱的"。本机实测
        （run/r245/r245_a7.txt / r245_a73.txt）：拖 1 下清排序 → 这里一次性
        批量摆位（2.25s + 1.02s）→ 刷一次 F5 与基准差 0/26 → **再重启
        资源管理器仍 0 偏离**，"重启都不乱了"成立（win10 本机；win11 待验收）。

        返回 ``{"ok", "moved", "total", "err", "baseline", "baseline_src"}``。
        **拿不到基准就老实说拿不到**（ok=False）：调用方 core/sort_clear 会自动
        退回"逐个拖回原位"的老路，绝不空手把桌面留在中间态。
        """
        res = {"ok": False, "moved": 0, "total": 0, "err": "",
               "baseline": "", "baseline_src": ""}
        cands = self._relay_baseline_candidates(baseline_path)
        if not cands:
            res["err"] = "no-baseline"
            return res
        # 逐份试：**读不出、或读出来没有一条合法坐标**都算这一份不合格，
        # 顺延下一份（★R247 判据 A6/A8；一口卡在最新那份坏档上＝白放弃）。
        path = None
        target = {}
        errs = []
        for p, tag in cands:
            try:
                with open(p, "r", encoding="utf-8") as f:
                    icons = json.load(f).get("icons") or {}
                t = {n: (int(v[0]), int(v[1])) for n, v in icons.items()
                     if isinstance(v, (list, tuple)) and len(v) == 2}
                # ★R313 A1 第三条出口：基准来自 `_guard_path`/`last_guard_layout`/
                #   最新 `全部_*.json`，R030 之前的老档里 desktop.ini 还在 `icons` 中
                #   ⇒「🧹 立即修复桌面排序」和拖完图标那趟批量摆位会把它直接
                #   apply_positions 到主屏（★R312 只改了存档写入端，这条读端没进范围）。
                t = self._strip_placeholders(t, '基准摆位')
                if not t:
                    errs.append('%s:empty' % os.path.basename(p))
                    continue
                target = t
                path, res["baseline"], res["baseline_src"] = p, os.path.basename(p), tag
                break
            except Exception as e:
                errs.append('%s:%s' % (os.path.basename(p), type(e).__name__))
        if path is None:
            # 全是空档 ⇒ empty-baseline；有读坏的 ⇒ 把第一份的错带出来（便于判读）
            res["err"] = ('bad-baseline:%s' % '; '.join(errs[:3])) if errs \
                else 'empty-baseline'
            logger.warning("批量摆位：基准布局全都不可用（%s），本次不写桌面"
                           % '; '.join(errs[:3]))
            return res
        res["total"] = len(target)
        try:
            # 基准里可能带着桌面上已不存在的名字（删掉的快捷方式）——
            # apply_positions 按实时索引表自然跳过，这里只留痕便于判读。
            live_names = set((self.get_current_icon_positions() or {}).keys())
            ghosts = [n for n in target if n not in live_names]
            res["moved"] = int(self.apply_positions(target) or 0)
            res["ok"] = True
            _layout_logger.info(
                "[relayout] 基准=%s 目标=%d 实摆=%d 跳过不存在的=%d %s"
                % (os.path.basename(path), len(target), res["moved"],
                   len(ghosts), ghosts[:6]))
        except Exception as e:
            res["err"] = "apply-failed:%s" % type(e).__name__
            logger.warning("批量按基准摆位失败：%s" % res["err"])
        return res

    def drain_guard_msgs(self) -> List[str]:
        msgs = list(self._guard_msgs)
        self._guard_msgs.clear()
        return msgs

    def _read_item_rect_by_index(self, idx: int):
        """读回 ListView 第 idx 项的整块矩形（LVIR_BOUNDS，含文字标签）。

        供快速定位方框实时定位用（R151）：框位必须来自"此刻"的读数。"""
        try:
            pid = win32process.GetWindowThreadProcessId(self.hwnd_listview)[1]
            h = win32api.OpenProcess(win32con.PROCESS_ALL_ACCESS, False, pid)
        except Exception:
            return None
        remote = None
        try:
            remote = win32process.VirtualAllocEx(h, 0, 8192, 0x1000, 0x04)
            win32process.WriteProcessMemory(
                h, remote, struct.pack("<4l", 0, 0, 0, 0))   # left=LVIR_BOUNDS
            if send_msg_timeout(self.hwnd_listview, LVM_GETITEMRECT, idx,
                                remote) is None:
                return None   # 读超时：缓冲不可信（审查修复）
            data = win32process.ReadProcessMemory(h, remote, 16)
            l, tp, r, b = struct.unpack("<4l", data)
            if r <= l or b <= tp:
                return None
            return (l, tp, r, b)
        except Exception:
            return None
        finally:
            if remote:
                try:
                    win32process.VirtualFreeEx(h, remote, 0, 0x8000)
                except Exception:
                    pass
            try:
                win32api.CloseHandle(h)
            except Exception:
                pass

    def get_icon_rect_by_name(self, name: str, retries: int = 3):
        """按名字实时读回图标矩形（快速定位方框用，R151）。

        ★ R85 教条落地：highlighter 的 _rect_cache 是上次采集快照，
        Explorer 重排后名字→索引错位，框会画到别的图标上（用户实测
        "框到别的图标上去了"）。这里每次都现读现验，重试 3 次。"""
        for attempt in range(1, retries + 1):
            try:
                idx = self._get_current_icon_map().get(name)
            except Exception:
                idx = None
            if idx is not None:
                rect = self._read_item_rect_by_index(idx)
                if rect:
                    return rect
            logger.warning(f"[快速定位] 第{attempt}次未读到「{name}」矩形，稍候重试")
            time.sleep(0.25)
        logger.error(f"[快速定位] 「{name}」矩形三次读取失败，方框提醒降级跳过")
        return None

    def _read_item_text(self, idx: int):
        """读回 ListView 第 idx 项的显示名（选中验证用，R85 读回教条）。"""
        try:
            pid = win32process.GetWindowThreadProcessId(self.hwnd_listview)[1]
            h = win32api.OpenProcess(win32con.PROCESS_ALL_ACCESS, False, pid)
        except Exception:
            return None
        remote = None
        try:
            remote = win32process.VirtualAllocEx(h, 0, 8192, 0x1000, 0x04)
            lv = _LVITEMW()
            lv.mask = 0x1   # LVIF_TEXT
            lv.iItem = idx
            lv.iSubItem = 0
            lv.pszText = remote + ctypes.sizeof(_LVITEMW)
            lv.cchTextMax = 512
            win32process.WriteProcessMemory(
                h, remote, ctypes.string_at(ctypes.addressof(lv),
                                            ctypes.sizeof(_LVITEMW)))
            if send_msg_timeout(self.hwnd_listview, LVM_GETITEMTEXTW, idx,
                                remote) is None:
                return None   # 读超时：无法验证（按消息成功计）
            raw = win32process.ReadProcessMemory(
                h, remote + ctypes.sizeof(_LVITEMW), 1024)
            return raw.decode("utf-16-le", errors="replace").split("\0", 1)[0]
        except Exception:
            return None
        finally:
            if remote:
                try:
                    win32process.VirtualFreeEx(h, remote, 0, 0x8000)
                except Exception:
                    pass
            try:
                win32api.CloseHandle(h)
            except Exception:
                pass

    def select_icon_by_name(self, name: str) -> bool:
        """在桌面上选中并聚焦指定图标（快速定位用）。

        ★ R143 准确性三件套（用户实测"搜 zcode 双击跳成控制面板/不准确
        时候多"）：①映射缺失重试——Explorer 忙时个别名字 GETITEMTEXT
        读超时会被 _read_icon_map_from_listview 跳过，映射没目标≠桌面没有；
        ②读回验证（R85 教条）——选中后把该索引名字读回比对，不符判失败；
        ③全程 [快速定位] 日志，失败可见不再静默。"""
        idx = None
        for attempt in (1, 2, 3):
            try:
                idx = self._get_current_icon_map().get(name)
            except Exception:
                idx = None
            if idx is not None:
                break
            logger.warning(f"[快速定位] 第{attempt}次映射未找到「{name}」"
                           "（桌面忙时个别名字读超时会被跳过），稍候重试")
            time.sleep(0.25)
        if idx is None:
            logger.error(f"[快速定位] 三次映射均未找到「{name}」，放弃选中")
            return False
        LVIS_SELECTED, LVIS_FOCUSED = 0x2, 0x1
        pid = win32process.GetWindowThreadProcessId(self.hwnd_listview)[1]
        h = None
        buf = None
        buf_clear = None
        try:
            h = win32api.OpenProcess(win32con.PROCESS_ALL_ACCESS, False, pid)
            buf = win32process.VirtualAllocEx(h, 0, 512, 0x1000, 0x04)
            buf_clear = win32process.VirtualAllocEx(h, 0, 512, 0x1000, 0x04)

            def _fill(remote, state):
                lvi = _LVITEMW()
                lvi.mask = 0x8          # LVIF_STATE
                lvi.state = state
                lvi.stateMask = LVIS_SELECTED | LVIS_FOCUSED
                win32process.WriteProcessMemory(
                    h, remote, ctypes.string_at(ctypes.addressof(lvi),
                                                ctypes.sizeof(lvi)))

            # ★ R142 修复"快速定位后桌面图标全被选中"：清选择与设选中必须
            #   两份缓冲——旧实现共用同一份（state=SELECTED 配 wParam=-1
            #   等于把全部项都"选中"而非清除，用户实测复现）
            _fill(buf_clear, 0)                    # 清除全部项选中/焦点（wParam=-1）
            _fill(buf, LVIS_SELECTED | LVIS_FOCUSED)
            ok1 = send_msg_timeout(self.hwnd_listview, 0x102B, -1, buf_clear)  # LVM_SETITEMSTATE
            ok2 = send_msg_timeout(self.hwnd_listview, 0x102B, idx, buf)
            ok = ok1 is not None and ok2 is not None
            # ★ 读回验证：消息发出≠选中生效（R85 教条）
            got = self._read_item_text(idx) if ok else None
            if got is not None and got != name:
                logger.error(f"[快速定位] 读回不符 idx={idx}: "
                             f"读回={got!r} 目标={name!r}")
                return False
            logger.info(f"[快速定位] 选中 idx={idx}「{name}」"
                        f"{'（读回一致）' if got is not None else '（读回超时按消息成功计）'}")
            return ok
        except Exception as e:
            logger.error(f"选中图标失败 {name}: {e}")
            return False
        finally:
            for b in (buf, buf_clear):
                if b and h:
                    try:
                        win32process.VirtualFreeEx(h, b, 0, 0x8000)
                    except Exception:
                        pass
            if h:
                try:
                    win32api.CloseHandle(h)
                except Exception:
                    pass

    def _get_visible_icon_map(self) -> Dict[str, int]:
        """获取当前**可见**图标名称到索引的映射。

        关键：Progman 的 SysListView32 不响应文件隐藏属性，LVM_GETITEMCOUNT
        和 LVM_GETITEMTEXTW 始终返回全部图标（含已隐藏文件的）。因此必须结合
        文件系统状态过滤，只返回真正可见的图标。
        """
        raw_map = self._get_current_icon_map()
        name_to_path = self._name_to_path_map()
        visible_map: Dict[str, int] = {}
        for name, idx in raw_map.items():
            p = name_to_path.get(name)
            if p and self._is_hidden(p):
                continue
            visible_map[name] = idx
        return visible_map

    def _read_icon_map_from_listview(self) -> Dict[str, int]:
        """强制从 ListView 读取所有图标名称到索引的映射（不依赖缓存）。

        这是 restore_layout 的正确实现：每次恢复前都重新读取 ListView，
        确保索引映射与当前桌面状态一致。
        """
        icon_map = {}
        count = self.get_icon_count()
        if count == 0:
            return icon_map

        # ★ R193（Qoder CORE-8）：同上，属性访问进降级保护
        try:
            pid = win32process.GetWindowThreadProcessId(self.hwnd_listview)[1]
        except Exception:
            return icon_map
        process_handle = None
        remote_memory = None

        try:
            process_handle = win32api.OpenProcess(
                win32con.PROCESS_ALL_ACCESS, False, pid
            )
            remote_memory = win32process.VirtualAllocEx(
                process_handle, 0, 8192, MEM_COMMIT, PAGE_READWRITE
            )
            text_buffer = remote_memory + ctypes.sizeof(_LVITEMW)

            for i in range(count):
                lv = _LVITEMW()
                lv.mask = LVIF_TEXT
                lv.iItem = i
                lv.iSubItem = 0
                lv.pszText = text_buffer
                lv.cchTextMax = 512
                ptr = ctypes.addressof(lv)
                win32process.WriteProcessMemory(
                    process_handle, remote_memory,
                    ctypes.string_at(ptr, ctypes.sizeof(_LVITEMW))
                )
                _name_ok = False
                for _retry in range(4):
                    if send_msg_timeout(
                        self.hwnd_listview, LVM_GETITEMTEXTW, i, remote_memory
                    ) is not None:
                        _name_ok = True
                        break
                    time.sleep(0.05)
                if not _name_ok:
                    # 2026-09-03 修复：超时重试 4 次——此前直接跳过导致
                    # Explorer 忙时部分图标从映射里消失，切换集合出错
                    continue
                text_raw = win32process.ReadProcessMemory(
                    process_handle, text_buffer, 1024
                )
                name = text_raw.decode("utf-16-le", errors="replace").split("\0", 1)[0]
                if name:
                    icon_map[name] = i

            # ★ R24 降噪：本行随监视线程每 2 秒触发一次（app.log 曾被刷到
            # 1.8MB），降为 DEBUG——排查时把 root logger 调到 DEBUG 即可见
            logger.debug(f"[_read_icon_map_from_listview] 读取到 {len(icon_map)} 个图标")
            return icon_map
        except Exception as e:
            logger.error(f"读取图标映射失败: {e}")
            return {}
        finally:
            if remote_memory and remote_memory != 0 and process_handle:
                win32process.VirtualFreeEx(process_handle, remote_memory, 0, MEM_RELEASE)
            if process_handle:
                win32api.CloseHandle(process_handle)

    def _get_current_icon_map(self) -> Dict[str, int]:
        """获取当前图标名称到索引的映射（**含隐藏文件**，仅作参考，不用于可见性判断）。

        2026-09-10 简化：之前有缓存优先 + 内联降级两条路径，都做同样的事
        （遍历 ListView 读全部 name→idx），但内联代码和另一个 batch 函数
        与 _read_icon_map_from_listview 完全重复。统一收口到后者。"""
        return self._read_icon_map_from_listview()

    def delete_layout(self, filepath: str) -> dict:
        """删除一个保存的布局文件"""
        try:
            if not os.path.exists(filepath):
                return {"success": False, "error": "文件不存在"}
            os.remove(filepath)
            logger.info(f"已删除布局: {filepath}")
            return {"success": True}
        except Exception as e:
            logger.error(f"删除布局失败: {e}")
            return {"success": False, "error": str(e)}

    def rename_layout(self, filepath: str, new_name: str) -> dict:
        """重命名布局：更新文件内 name 字段并按新名字重命名文件。
        ★R190（Qoder CORE-4）：名字先过 sanitize；写盘成功才删旧文件。"""
        try:
            if not os.path.exists(filepath):
                return {"success": False, "error": "文件不存在"}
            safe = self.sanitize_layout_name(new_name)
            if not safe:
                return {"success": False, "error": "名字不合法"}
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["name"] = safe
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            new_path = os.path.join(os.path.dirname(filepath), f"{safe}_{ts}.json")
            if os.path.abspath(new_path) == os.path.abspath(filepath):
                return {"success": False, "error": "新名字与原名相同"}
            if not _atomic_save_json(new_path, data):
                return {"success": False, "error": "写入失败，原布局保留"}
            os.remove(filepath)
            logger.info(f"布局已重命名: {safe}")
            return {"success": True, "path": new_path}
        except Exception as e:
            logger.error(f"重命名布局失败: {e}")
            return {"success": False, "error": str(e)}

    @staticmethod
    def sanitize_layout_name(name: str) -> str:
        """统一布局名称清洗规则（导入入口 / add_layout 两处共用，杜绝漂移）：
        非白名单字符→'_'，去掉首尾点（防 '.'/'..' 路径穿越特征残留），限长 64。"""
        import re as _re
        return _re.sub(r'[^\w\u4e00-\u9fff \-()]', '_',
                       str(name).strip()).strip('.').strip()[:64]

    def _remove_same_name_layouts(self, name: str, exclude: str = None) -> int:
        """删掉布局目录里与 `name` 同名的旧存档，返回删除份数。

        ★R233（用户 08 批 item4，★R230 挂账 3）：本方法**只允许在写入新档成功
        之后**调用。旧实现把同名清理放在 `add_layout` 开头（校验 icons、写盘
        之前），于是"这一份导入失败"和"本地旧档已删"同时发生：坏 zip / 只读盘 /
        校验不过，用户每重试一次就多丢一批自己攒的布局（修前实测：目录被清空）。
        `exclude` 传刚写入的那个文件路径——新档内部的 name 字段与旧档同名，
        不排掉就会把自己删了。
        """
        removed = 0
        try:
            for fn in os.listdir(self.backup_dir):
                if not fn.endswith('.json'):
                    continue
                fp = os.path.join(self.backup_dir, fn)
                if exclude and os.path.abspath(fp) == os.path.abspath(exclude):
                    continue
                try:
                    with open(fp, 'r', encoding='utf-8') as f:
                        _n = json.load(f).get('name')
                    if _n == name:
                        os.remove(fp)
                        removed += 1
                except Exception:
                    continue   # 单个损坏文件不拦整体
        except Exception as e:
            logger.warning(f"[布局] 同名清理扫描失败(忽略): {e}")
        if removed:
            logger.info(f"[布局] 同名覆盖：已移除旧 [{name}] {removed} 份")
        return removed

    def add_layout(self, data: dict, keep_ts: bool = False,
                   replace_same: bool = False) -> dict:
        """导入一个布局 dict 到布局目录（zip 还原用）。

        校验 name + icons 结构，写入唯一文件名（避免覆盖同名旧备份），
        复用 _atomic_save_json + _prune_layout_backups。
        ★ 纵深防御：name 再净化一次（调用方 _on_import_layouts 已清洗，
        这里兜底，防止将来他人直接调本方法直传外部 name 造成路径穿越）。
        ★ R34 keep_ts=True：导入场景保留布局"原保存时间"（文件名+timestamp
        都用源时间戳）——此前一律用导入时刻，布局列表显示的时间误导用户
        （用户拍板：显示上次保存的时间）。仅接受**过去**的时间戳，未来值
        一律回退当前时间（P1-4 防未来时间戳破坏 pick_layout 排序语义不变）。
        ★ R37 replace_same=True：导入时删除本地**同名**（按文件内 name 字段
        匹配）旧存档——否则重复导入同一份全部数据时布局成对重复（用户
        实测）。语义与文件夹组导入一致：同名覆盖、其他保留。
        ★R233：同名覆盖的执行时机从"开头"改到"新档写成功之后"（见
        `_remove_same_name_layouts`）——语义没变，丢数据的面没了。
        """
        try:
            name = self.sanitize_layout_name(data.get('name', ''))
            icons = data.get('icons')
            if not name:
                return {"success": False, "error": "布局缺少 name 字段"}
            # ★ P1-2 残留：不只校验 config 是 list，还要长度=2 + 元素是数字；
            #   否则 [1] / [1,2,3] / ['a','b'] 会穿透校验，在 restore_layout
            #   的 `for name, (x, y) in saved_icons.items()` 处解包/位运算报错，
            #   整轮恢复被 try 吞掉。宽松策略：保留合法条目，避免"脏文件废整份布局"。
            if not isinstance(icons, dict):
                return {"success": False, "error": f"布局 [{name}] 的 icons 不是 dict"}
            clean_icons: dict = {}
            for k, v in icons.items():
                if (isinstance(k, str)
                        and isinstance(v, list) and len(v) == 2
                        and all(isinstance(c, (int, float)) for c in v)):
                    clean_icons[k] = [int(v[0]), int(v[1])]
            if not clean_icons:
                return {"success": False, "error": f"布局 [{name}] 无有效图标位置"}
            icons = clean_icons
            # ★R313 A1 第四条出口：外部存档（zip 还原/换机导入）里可能带着
            #   R030 之前抄进去的桌面占位文件——导入即落盘，下次套用照样摆回来。
            #   与写侧同一口径：不认领，剔除并留痕。
            icons = self._strip_placeholders(icons, '导入外部布局')
            if not icons:
                return {"success": False,
                        "error": f"布局 [{name}] 只有桌面占位文件，没有可恢复的位置"}
            # ★R300-F6：★R299 给"保存布局/另存"两个写入口都过了同格清洗，但导入
            #   外部布局是**第三个写入者**——外面那份 JSON 里同一格挤两个名字时
            #   照样原样落盘，下次套用照旧叠图标。清洗接在这里（同组里第一个不动、
            #   后来者顺延，dedupe_layout_cells 自己会留一条 WARNING 点名谁被顺延）。
            icons, _dup_moves = self.dedupe_layout_cells(dict(icons))
            # ★ R34：解析源时间戳（keep_ts 且为合法的过去时间才采用）
            ts_dt = None
            if keep_ts:
                try:
                    cand = datetime.fromisoformat(str(data.get('timestamp') or ''))
                    if cand <= datetime.now():
                        ts_dt = cand
                except Exception:
                    ts_dt = None
            now = datetime.now()
            ts = (ts_dt or now).strftime("%Y%m%d_%H%M%S")
            filename = f"{name}_{ts}.json"
            filepath = os.path.join(self.backup_dir, filename)
            # 同秒重复导入 → 加序号后缀，绝不覆盖已有文件
            seq = 2
            while os.path.exists(filepath):
                filepath = os.path.join(self.backup_dir, f"{name}_{ts}_{seq}.json")
                seq += 1
            payload = {
                # ★ P1-4：不接受外部**未来**时间戳——list_layouts 按它排序、
                #   pick_layout 用 max(..., key=timestamp) 取"最新"，未来时间
                #   会让该布局永远排最前，破坏套用语义。R34 起 keep_ts 导入
                #   接受合法的**过去**源时间戳（布局列表显示原保存时间）。
                "timestamp": (ts_dt or now).isoformat(),
                "name": name,
                "source": "manual",   # 导入的备份按手动保护，不参与自动清理
                "icon_count": len(icons),
                "icons": icons,
            }
            # ★R264 审计 Z1：旧写法在这里只重建 5 个键，把保存侧
            # （:1374-1398 / :1476-1492）写出的 screen / icon_size / view_state /
            # dup_cells **四个键全剥掉** ⇒ 换机「导入全部数据」再套用时：
            #   · dup_cells 没了 ⇒ 同名图标"多出来的那一条"坐标永久丢失
            #     （★R247 修的那个"有一个就是摆不对"，在导入链上原地复发）；
            #   · icon_size 没了 ⇒ 非默认尺寸档不再回放（★R245）；
            #   · view_state / screen 没了 ⇒ 判读时"当初谁在管桌面"变成不知道。
            # 与本文件 :199-200 自己写明的惯例（"只增字段，跨机导入不丢数据"）
            # 直接矛盾。现在透传，外部数据逐键按各自读侧口径校验，脏的就当
            # "没记"（读侧本来就按"老存档没这一项"处理，不会因此报错）。
            _isz = data.get('icon_size')
            if _isz is not None:
                try:
                    _isz = int(_isz)
                except (TypeError, ValueError):
                    _isz = None
                if _isz is not None and not (
                        self.ICON_SIZE_MIN <= _isz <= self.ICON_SIZE_MAX):
                    _isz = None
                if _isz is None:
                    logger.warning('[布局导入] [%s] icon_size 是脏值/越界（%r），'
                                   '本项不透传' % (name, data.get('icon_size')))
                else:
                    payload['icon_size'] = _isz
            _dc_raw = data.get('dup_cells')
            if _dc_raw is not None:
                _dc = {}
                if isinstance(_dc_raw, dict):
                    for _n, _cells in _dc_raw.items():
                        if not isinstance(_n, str) or not isinstance(_cells, list):
                            continue
                        _pts = [[int(c[0]), int(c[1])] for c in _cells
                                if (isinstance(c, (list, tuple)) and len(c) == 2
                                    and all(isinstance(v, (int, float))
                                            and not isinstance(v, bool)
                                            for v in c))]
                        if _pts:
                            _dc[_n] = _pts
                else:
                    logger.warning('[布局导入] [%s] dup_cells 不是对象（%s），'
                                   '本项不透传' % (name, type(_dc_raw).__name__))
                if _dc:
                    payload['dup_cells'] = _dc
            for _k in ('screen', 'view_state'):
                _v = data.get(_k)
                if _v is None:
                    continue
                if isinstance(_v, dict):
                    payload[_k] = _v
                else:
                    logger.warning('[布局导入] [%s] %s 不是对象（%s），本项不透传'
                                   % (name, _k, type(_v).__name__))
            if _atomic_save_json(filepath, payload):
                # ★R233：同名清理挪到这里——**新档已经落盘成功**才动旧档，
                #   校验失败/写盘失败/异常都碰不到用户原来那份布局。
                if replace_same:
                    self._remove_same_name_layouts(name, exclude=filepath)
                self._prune_layout_backups(name)
                logger.info(f"已导入布局: {os.path.basename(filepath)}, 共 {len(icons)} 个图标")
                return {"success": True, "name": name,
                        "count": len(icons), "file": os.path.basename(filepath)}
            return {"success": False, "error": f"布局 [{name}] 写入失败"}
        except Exception as e:
            logger.error(f"导入布局失败: {e}")
            return {"success": False, "error": str(e)}

    def list_layouts(self, include_internal: bool = False) -> List[dict]:
        """列出所有保存的布局（按布局内 timestamp 排序，保证"最新"语义正确）。

        include_internal=False（默认）时隐藏内部备份（文件名以 _ 开头，
        如 _家长模式前），它们只供程序内部使用，不该出现在用户布局列表里。"""
        layouts = []
        if not os.path.exists(self.backup_dir):
            return layouts

        for f in os.listdir(self.backup_dir):
            if f.endswith(".json"):
                if not include_internal and f.startswith("_"):
                    continue
                filepath = os.path.join(self.backup_dir, f)
                try:
                    with open(filepath, "r", encoding="utf-8") as fh:
                        data = json.load(fh)
                    layouts.append({
                        "name": data.get("name", f),
                        "path": filepath,
                        # ★R313 E5（P1）：timestamp **在收集端就归一成 str**。
                        #   旧写法 `data.get("timestamp", "")` 对"键存在但值是 null"
                        #   给出的是 None，而下面的 `layouts.sort(...)` 在逐文件 try
                        #   **之外** ⇒ None 与 str 一比就 TypeError ⇒
                        #   `_update_lists`（`__init__` 一路无 try）炸掉 ⇒
                        #   主窗构造失败、launcher 重启循环后放弃＝"程序彻底打不开"。
                        #   混型档确实存在：core/dock.py 的 ★R204 CORE-19 注释自述
                        #   "timestamp 混型（null/数字）时 str() 归一"——那把 clamp
                        #   只护了 dock 自己，没护这条全目录列表。
                        "timestamp": str(data.get("timestamp") or ""),
                        "source": data.get("source", "manual"),
                        "icons": data.get("icon_count", 0)
                    })
                except Exception:
                    pass

        layouts.sort(key=lambda l: l.get("timestamp", ""))
        return layouts
    
    def get_all_icon_positions(self) -> Dict[str, Tuple[int, int]]:
        """获取所有图标位置（用于分类显示）"""
        return self.get_current_icon_positions()
    
    def set_icon_position(self, index: int, x: int, y: int) -> bool:
        """设置指定索引的图标位置"""
        try:
            lparam = pack_lparam(x, y)
            send_msg_timeout(
                self.hwnd_listview, LVM_SETITEMPOSITION, index, lparam
            )
            # 无法通过索引查名称，所以缓存无法按名称更新
            # 但位置确实已写入 ListView
            return True
        except Exception as e:
            logger.error(f"设置位置失败: {e}")
            return False

    def apply_positions(self, target: Dict[str, Tuple[int, int]]) -> int:
        """
        批量把图标移动到目标位置，已在目标位置的自动跳过（读取很快，移动很慢，
        跳过无变化图标可让二次切换近乎瞬时）。返回实际移动数。
        """
        current = self.get_current_icon_positions()
        name_to_idx = self._get_current_icon_map()
        lv = self.hwnd_listview
        # ★R234：自动排列开着时 LVM_SETITEMPOSITION 会被 Explorer 直接拒（实测
        # ret=0、图标一格不动），套位前先把它关掉；关不掉也必须留痕，否则又是
        # 一轮"消息发了=成功"的假成功（R86 同族）。
        _aa = self.ensure_auto_arrange_off()
        if _aa != 'off':
            logger.warning(f"[apply_positions] 自动排列闸门={_aa}"
                           f"｜{self.auto_arrange_block_reason()}")
        moved = 0
        # ★R246 B5-②：冻结重绘这件事本身要**留痕**（以前只 try/finally，
        #   真失败过没人知道）。顺带记一次耗时——整桌批量摆位是 ★R245 A7
        #   "拖完立马写布局"那条路的时间成本，有数才有得优化。
        _t0 = time.time()
        _fz = send_msg_timeout(lv, WM_SETREDRAW, 0, 0)
        _un = None
        try:
            for name, (tx, ty) in target.items():
                idx = name_to_idx.get(name)
                if idx is None:
                    continue
                cur = current.get(name)
                if cur and abs(cur[0] - tx) <= 2 and abs(cur[1] - ty) <= 2:
                    continue  # 已在目标位置
                if send_msg_timeout(
                    lv, LVM_SETITEMPOSITION, idx, pack_lparam(int(tx), int(ty))
                ) is None:
                    logger.warning("[restore] SETITEMPOSITION 超时，中止本轮移动")
                    break
                self._position_cache[name] = (int(tx), int(ty))
                moved += 1
        finally:
            _un = send_msg_timeout(lv, WM_SETREDRAW, 1, 0)
            win32gui.InvalidateRect(lv, None, True)
            try:
                _layout_logger.info(
                    f"[apply] 目标={len(target)} 实动={moved} "
                    f"secs=%.2f freeze=%s unfreeze=%s" % (
                        time.time() - _t0,
                        'ok' if _fz is not None else 'FAILED',
                        'ok' if _un is not None else 'FAILED'))
            except Exception as _fe:
                logger.debug("[apply_positions] timing log skipped: %s", _fe)
            # ★R252（挂账 #105 口径 B 里"顺手补"的那半）：套布局补一条事后读回。
            #   `restore_layout` 从 ★R190 起就有 [verify]，`apply_positions` 没有 ⇒
            #   "自动排列关不掉、照写了"那一次到底写进去几个，日志里看不出来，
            #   用户只能读成"摆了没反应"，我们也无从归因（#105 挂账的原始诉求）。
            #   · **只在闸门不干净时读**（`_aa != 'off'`）：切分类是热路径，
            #     关得掉（绝大多数时候）就不多加一次全表跨进程读，不拖慢摆位；
            #   · 只读回、**不改行为、不补发**（口径 B＝照写留痕；补发仍归
            #     `restore_layout` 那条低频路）；
            #   · 任何异常吞掉，读回失败不能把切分类带崩。
            try:
                if moved and _aa != 'off':
                    _lv2 = self._read_all_positions_live()
                    _ok2, _bad2, _ab2 = self.classify_positions(target, _lv2)
                    _layout_logger.info(
                        f"[verify-apply] 闸门={_aa} 目标={len(target)} "
                        f"落位={len(_ok2)} 偏差={len(_bad2)} 缺席={len(_ab2)} "
                        f"首批偏差={[(n, target[n], _lv2.get(n)) for n in _bad2[:6]]}")
            except Exception as _ve2:
                _layout_logger.info(f"[verify-apply] 读回异常: {_ve2}")
        return moved

    def apply_positions_settled(self, target, *, wait_ms=2200, poll_ms=120,
                                tol=4, read_map=None, read_pos=None,
                                move_one=None, geom=None, gate=None,
                                sleep=time.sleep, clock=time.monotonic):
        """★R304（批60 U1）「刚落回桌面的图标」专用摆位：先等、再摆、再读回。

        真机 R023 日志实锤（`[apply] 目标=1 实动=0` 连着三条）：`move_back` 在
        `os.rename` 之后**立刻**调 `apply_positions`，那枚图标此刻还没被 Explorer
        加进 ListView（刷新的 `SHChangeNotify` 又甩在后台线程里排队），于是走进
        `idx is None → continue` 这条**静默跳过**——一枚都没摆，界面却报"已按原
        坐标摆好"。三条规矩：
          ① 名册里缺席的名字**有界轮询等待**，等不到就点名 missing、一发都不写；
          ② 记录那格已被"别人"占着 ⇒ `_nearest_free_cell` 改投**离原格最近**的空格
             （绝不叠到别人身上，也绝不动别人），整屏都满就点名 no_room 不写；
          ③ 写完**读回核对**：返回值只证明"消息发出去了"，落没落看读数（★R234）。
        读名册/读坐标/写坐标三个原语可注入 ⇒ 门禁能在假桌面上跑这条真函数体，
        一次都不碰用户桌面。
        → {'target','moved','already','adjusted','missing','no_room','unverified','secs'}
        """
        _rd_map = read_map or self._get_current_icon_map
        _rd_pos = read_pos or self.get_current_icon_positions
        _gate = gate if gate is not None else self.ensure_auto_arrange_off
        if move_one is None:
            def _move(name, idx, x, y):
                return send_msg_timeout(
                    self.hwnd_listview, LVM_SETITEMPOSITION, idx,
                    pack_lparam(int(x), int(y))) is not None
        else:
            _move = move_one
        res = {'target': len(target), 'moved': 0, 'already': 0, 'adjusted': 0,
               'missing': [], 'no_room': [], 'unverified': []}
        _t0 = clock()
        if not target:
            res['secs'] = 0.0
            return res
        names = set(target.keys())
        deadline = _t0 + max(0.0, wait_ms / 1000.0)
        imap = {}
        while True:
            try:
                imap = _rd_map() or {}
            except Exception as e:
                logger.warning('[apply-settled] 读名册失败（当没这枚处理）: %s', e)
                imap = {}
            if all(n in imap for n in target) or clock() >= deadline:
                break
            sleep(poll_ms / 1000.0)
        try:
            pos = _rd_pos() or {}
        except Exception as e:
            logger.warning('[apply-settled] 读实时坐标失败，只用目标格: %s', e)
            pos = {}
        if geom is None:
            try:
                geom = self._grid_geometry(
                    None, list(target.values()) + list(pos.values()))
            except Exception as e:
                logger.warning('[apply-settled] 网格几何读不到，用默认格距: %s', e)
                geom = self._grid_geometry()
        try:
            _aa = _gate() or 'off'
        except Exception as e:
            _aa = 'error'
            logger.warning('[apply-settled] 自动排列闸门检查异常（照写并留痕）: %s', e)
        if _aa != 'off':
            logger.warning('[apply-settled] 自动排列闸门=%s｜关不掉时 LVM_SETITEMPOSITION '
                           '可能被 Explorer 拒（★R234），本轮照写并以读回为准', _aa)
        busy = set()
        for n, p in pos.items():
            if n in names:
                continue
            try:
                busy.add(self._cell_index(p, geom))
            except Exception:
                pass
        written = []
        for name, (tx, ty) in target.items():
            if name not in imap:
                res['missing'].append(name)
                continue
            cur = pos.get(name)
            try:
                if cur and abs(cur[0] - tx) <= 2 and abs(cur[1] - ty) <= 2:
                    res['already'] += 1
                    busy.add(self._cell_index((tx, ty), geom))
                    continue
                cell = self._cell_index((tx, ty), geom)
            except Exception as e:
                logger.warning('[apply-settled] %s 格子换算失败，按原坐标写: %s', name, e)
                cell = None
            if cell is not None and cell in busy:
                nb = self._nearest_free_cell(cell, busy, geom)
                if nb is None:
                    res['no_room'].append(name)
                    continue
                tx, ty = self._cell_origin(nb, geom)
                res['adjusted'] += 1
                cell = nb
            if cell is not None:
                busy.add(cell)
            if _move(name, imap[name], int(tx), int(ty)):
                res['moved'] += 1
                written.append((name, (int(tx), int(ty))))
                try:
                    self._position_cache[name] = (int(tx), int(ty))
                except Exception as _ce:
                    logger.debug('[apply-settled] 位置缓存没写进去: %s', _ce)
            else:
                res['unverified'].append(name)
        if res['missing']:
            logger.warning('[apply-settled] %d 枚等 %dms 仍没出现在桌面视图里，'
                           '这一枚一枚都没摆（多半是 Explorer 还没刷新）：%s',
                           len(res['missing']), wait_ms, res['missing'][:6])
        if res['no_room']:
            logger.warning('[apply-settled] %d 枚的原格被人占着、且全屏找不到空格，'
                           '为不叠图标本轮没摆：%s',
                           len(res['no_room']), res['no_room'][:6])
        if written:
            try:
                pos2 = _rd_pos() or {}
            except Exception as e:
                logger.warning('[apply-settled] 读回失败，%d 枚落位未知: %s',
                               len(written), e)
                pos2 = {}
            for name, (tx, ty) in written:
                p2 = pos2.get(name)
                if p2 is None or abs(p2[0] - tx) > tol or abs(p2[1] - ty) > tol:
                    if name not in res['unverified']:
                        res['unverified'].append(name)
            if res['unverified']:
                logger.warning('[apply-settled] %d 枚写完读回不符（写了没落）：%s',
                               len(res['unverified']), res['unverified'][:6])
        res['secs'] = max(0.0, clock() - _t0)
        try:
            _layout_logger.info(
                '[apply-settled] 目标=%d 实动=%d 已在=%d 改格=%d 缺席=%d 无空位=%d '
                '读回不符=%d secs=%.2f' % (
                    res['target'], res['moved'], res['already'], res['adjusted'],
                    len(res['missing']), len(res['no_room']),
                    len(res['unverified']), res['secs']))
        except Exception as _le:
            logger.debug('[apply-settled] 簿记日志跳过: %s', _le)
        return res
    
    def show_icons(self):
        """显示所有桌面图标"""
        try:
            hwnd_progman = win32gui.FindWindow("Progman", None)
            hwnd_shell = win32gui.FindWindowEx(hwnd_progman, 0, "SHELLDLL_DefView", None)
            if hwnd_shell:
                win32gui.ShowWindow(hwnd_shell, win32con.SW_SHOW)
                # ★R255（46 批 item④-1）状态写点②：复明成功=隐藏态已结束，
                # 哨兵看到 hidden=false 自己收工退出。
                try:
                    from core import desktop_sentinel
                    desktop_sentinel.set_hidden(False)
                except Exception as _sde:
                    logger.warning(f"哨兵状态清除异常（不影响显示本身）: {_sde}")
                return True
        except Exception as e:
            logger.error(f"显示图标失败: {e}")
        return False
    
    def hide_icons(self):
        """隐藏所有桌面图标"""
        try:
            hwnd_progman = win32gui.FindWindow("Progman", None)
            hwnd_shell = win32gui.FindWindowEx(hwnd_progman, 0, "SHELLDLL_DefView", None)
            if hwnd_shell:
                win32gui.ShowWindow(hwnd_shell, win32con.SW_HIDE)
                # ★R255（46 批 item④-1）：整层隐藏生效瞬间拉起"影子哨兵"——
                # 主进程哪怕被任务管理器强杀/连树一起杀，它也会把桌面拉亮。
                # 拉不起不算罪（下次启动自愈仍兜底），但必须留痕（★R230b）。
                try:
                    from core import desktop_sentinel
                    if not desktop_sentinel.on_hide():
                        logger.warning("桌面哨兵拉起失败：强杀场景暂靠下次启动自愈")
                except Exception as _sde:
                    logger.warning(f"桌面哨兵接线异常（不影响隐藏本身）: {_sde}")
                return True
        except Exception as e:
            logger.error(f"隐藏图标失败: {e}")
        return False
    
    def is_icons_visible(self) -> bool:
        """检查图标是否可见"""
        try:
            hwnd_progman = win32gui.FindWindow("Progman", None)
            hwnd_shell = win32gui.FindWindowEx(hwnd_progman, 0, "SHELLDLL_DefView", None)
            if hwnd_shell:
                return win32gui.IsWindowVisible(hwnd_shell)
        except:
            pass
        return False

    def ensure_desktop_view_visible(self) -> bool:
        """★ R254：实现体已提为模块级函数（本方法签名与三个既有调用点一字未动，
        崩态/退出路径可直接 import 模块级那份，不经 IconManager 构造）。"""
        return ensure_desktop_view_visible()

    # -------------------------------------------------------------------
    # 按名字隐藏/恢复（文件属性法，分类切换的核心机制）
    # -------------------------------------------------------------------

    @property
    def hidden_names(self) -> set:
        """当前被本程序隐藏（属性仍为 HIDDEN）的图标名（高亮框绘制时跳过它们）。

        2026-09-02 语义修正：旧实现按 was_hidden=True 过滤，但本程序隐藏的
        图标记录的是 was_hidden=False，导致过滤永远为空、highlighter 在
        分类模式下仍给已隐藏图标画框（"多余的框"）。改由 _currently_hidden
        维护实时集合。"""
        return set(self._currently_hidden)

    def _visible_icon_cache_key(self) -> str:
        """返回可见性状态的指纹，供 dock._get_icon_map 缓存失效使用。
        当 _hidden 字典变化（隐藏/恢复图标）时返回值改变，强制重新读取映射。
        使用 sorted tuple 而非 hash()，确保跨进程稳定（hash() 在 Python 3 中随机化）。"""
        return tuple(sorted(self._hidden.keys()))

    def _is_hidden(self, path: str) -> bool:
        try:
            return bool(win32api.GetFileAttributes(path) & win32file.FILE_ATTRIBUTE_HIDDEN)
        except Exception:
            return False

    def _notify_shell(self, path: str):
        """让 Explorer 立即刷新该桌面项（同步，适合少量调用）"""
        try:
            ctypes.windll.shell32.SHChangeNotify(
                SHCNE_UPDATEITEM, SHCNF_PATHW, ctypes.c_wchar_p(path), 0)
        except Exception as e:
            logger.error(f"shell 通知失败 {path}: {e}")

    def _notify_shell_async(self, paths):
        """批量 shell 通知放到后台线程。

        实测教训：主线程（Tk 事件循环里）连发几十上百个 SHChangeNotify 会触发
        原生 abort（code=3 无声崩溃，stderr 无输出，独立进程跑同样代码不崩），
        所以批量通知必须脱离主线程。"""
        paths = list(paths)
        if not paths:
            return

        def work():
            for p in paths:
                try:
                    ctypes.windll.shell32.SHChangeNotify(
                        SHCNE_UPDATEITEM, SHCNF_PATHW, ctypes.c_wchar_p(p), 0)
                except Exception:
                    pass

        threading.Thread(target=work, daemon=True, name='ShellNotify').start()

    def _notify_shell_sync(self, paths):
        """同步批量 shell 通知——★R319 K14 把话说明白：**只有 <=10 枚那一支真的同步**，
        更多枚转交异步那支、本函数不等（旧第一行那句承诺说过头了）。

        适用于需要立即看到效果的场景（如切换分类）。
        少量图标时使用（<=10个），大量图标时使用异步版本。"""
        paths = list(paths)
        if not paths:
            return
        # 先发每个图标的单独通知
        if len(paths) <= 10:
            for p in paths:
                try:
                    ctypes.windll.shell32.SHChangeNotify(
                        SHCNE_UPDATEITEM, SHCNF_PATHW, ctypes.c_wchar_p(p), 0)
                except Exception:
                    pass
        else:
            # 大量图标：转交异步那一支。★R319 K14 改口：旧注释在这里承诺过"会稍等"，
            # 实际这条分支只起线程、**不等**（函数名叫 sync 也只对 <=10 那一支成立）。
            # 真加等待会拖慢切换手感，那个取舍还没定 ⇒ 本批先把话说对，行为不动。
            self._notify_shell_async(paths)

        # ★R283-1：收尾刷新改 Desktop 作用域 UPDATEDIR，不再发全局 ASSOCCHANGED。
        # 旧写法 SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_FLUSH) 向**全部外壳订阅者/
        # 扩展**广播"关联变了"并同步等待处理完——任一扩展/Explorer 正忙即僵死
        # （用户报"explorer 卡住·文件夹组关不掉，只能重启 explorer"）。ASSOCCHANGED 是
        # 全表关联重扫，与"桌面图标显隐"无关且是"全部→分类 屏幕空白偏长"的元凶
        # （dock.py:1616 已点名弃用）。UPDATEDIR 只重扫桌面文件夹本身，正是我们改动的范围；
        # 两个调用方（dock.py:1603 / rank_display.py:362）随后还会各发一次 F5，桌面必然刷新。
        # 与 dock.py:2031"补发 UPDATEDIR 强刷新"同款已验证范式。
        for d in _desktop_dirs():
            try:
                buf = ctypes.create_unicode_buffer(
                    d if d.endswith(os.sep) else d + os.sep)
                ctypes.windll.shell32.SHChangeNotify(
                    0x00001000,              # SHCNE_UPDATEDIR
                    SHCNF_PATHW | 0x00001000,  # SHCNF_PATHW | SHCNF_FLUSH
                    ctypes.cast(buf, ctypes.c_void_p), 0)
            except Exception:
                pass

    def _load_hidden_state(self):
        try:
            if os.path.exists(self._hidden_state_path):
                with open(self._hidden_state_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._hidden = data
                else:
                    # ★R207 NEW3-C7：顶层不是 dict=损坏——改 .bad 留底防覆盖
                    logger.error("hidden_state.json 顶层不是 dict，已留底 .bad")
                    self._quarantine_bad_ledger()
        except json.JSONDecodeError as e:
            # ★R207 NEW3-C7：解析失败不能与"首次使用"混同——直接清空
            # _hidden 会让下一次保存把整份旧账覆盖掉。改名 .bad 留底
            # （可手动捞回），本次按空账起步（启动自愈会把桌面 H|S
            # 双属性图标扫回来）。
            logger.error(f"hidden_state.json 解析失败（尝试改名留底）: {e}")
            self._quarantine_bad_ledger()
        except Exception as e:
            # ★R210（NEW4-C1）：非解析类读失败（杀软/备份瞬时占用、权限、
            # 编码）同权留底+禁写闸——原"空账三不管"会让本会话首次落盘
            # 覆盖整份旧账（与 condition_monitor 同轮已修版双标）
            logger.error(f"读取隐藏状态失败（尝试改名留底并按空账起步）: {e}")
            self._quarantine_bad_ledger()

    def _quarantine_bad_ledger(self):
        """损坏账本改名 .json.bad 留底（CORE-18 分类账同款范式）"""
        self._hidden = {}
        try:
            if os.path.exists(self._hidden_state_path):
                # ★R213（NEW5-C4）：.bad 已存在时不裸覆盖——上一次损坏
                # 的留底可能还能手动捞回，第二次 os.replace 会把前一份
                # 唯一证据静默抹掉。.bad 存在则加时间戳后缀另存。
                bad = self._hidden_state_path + '.bad'
                if os.path.exists(bad):
                    import time as _t
                    bad = (self._hidden_state_path + '.bad.'
                           + _t.strftime('%Y%m%d_%H%M%S'))
                os.replace(self._hidden_state_path, bad)
                logger.warning(f"损坏的隐藏账本已留底 → {bad}")
        except OSError as e:
            logger.error(f"隐藏账本留底失败（本会话禁写防覆盖）: {e}")
            self._ledger_load_failed = True

    def _load_initial_positions(self):
        """从最新备份文件加载图标位置到缓存，避免首次读取时 LVM_GETITEMPOSITION 失败返回全零。

        关键：加载后验证备份中的图标是否仍在当前桌面上。如果桌面图标被 Windows
        重置或大量变化，自动重新保存当前布局作为基准。"""
        try:
            layouts = self.list_layouts()
            if not layouts:
                return
            # 取最新的布局（按 timestamp 排序，最后一个是最新的）
            latest = layouts[-1]
            filepath = latest['path']
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            icons = data.get('icons', {})
            if not icons:
                return

            self._position_cache.clear()
            self._position_cache.update(icons)
            logger.info(f"已加载初始位置缓存: {len(icons)} 个图标（来自 {os.path.basename(filepath)}）")

            # 验证：检查备份中的图标是否还在当前桌面上
            # 注意：用 _get_current_icon_map() 而非 _get_visible_icon_map()，
            # 因为 Progman ListView 不响应文件隐藏属性，可见映射会遗漏已隐藏图标。
            try:
                current_map = self._get_current_icon_map()
                backup_names = set(icons.keys())
                current_names = set(current_map.keys())
                missing = backup_names - current_names
                extra = current_names - backup_names
                if missing and not extra:
                    # 只有备份有、当前没有 → 可能 Explorer 刷新延迟，等一会再试
                    # ★R242（§二 D3）：原文案承诺"将用当前桌面重新保存"，可这条
                    #   路 ★P1-3 起就**不再回写布局文件**了（只把本进程的基准缓存
                    #   换成实时读数），下一句又写"已用实时读数覆盖"——两条对着说，
                    #   判读日志的人据此会以为存档被改过。口径统一：只说缓存。
                    logger.warning(
                        f"位置缓存验证: {len(missing)} 个备份图标不在当前桌面 "
                        f"(如: {', '.join(list(missing)[:5])})，"
                        "本会话基准改用实时读数（布局文件不动）")
                    # ★ P1-3：不再内嵌 save_layout 回写——它会二次触发
                    #   _read_all_positions_live + _prune_layout_backups，
                    #   且 __init__ 启动路径上同步阻塞。改成清缓存 + 用实时
                    #   读数覆盖；重读仍失败则保留空缓存，由首次 save_layout 兜底。
                    self._position_cache.clear()
                    live = self._read_all_positions_live()
                    if not live:
                        # ★R277 → ★R299 N3（win11 那份 26.4 h 挂机日志里
                        #   "保留空缓存"共 5 次，★R299-S6 逐行复核订正过这里的旧"4 次"）：
                        # ★R277 补的那一次重读等的是**固定 0.15 s**，可开机后
                        # Explorer 桌面 ListView 重建完成的耗时并不固定 ⇒ 等满
                        # 还是读空，日志照样声明"实时读数也失败"，几百毫秒后
                        # __init__ 又读出 76 个（自相矛盾没根治）。改成**有界轮询**：
                        # 每 0.25 s 再读一次，读到就用；界满才说失败。
                        # 仍只在失败分支里等，正常路径零开销。
                        # ★R314（真机 0.99-R031 两个会话 2/2 复现）：上界 2.0 s 差一点
                        # 点——失败那句打在 +2.004 s，紧接着 +2.023 s __init__ 那一读就
                        # 读出 79 个（又是"先说读不到、再说读到了"）。放宽到 2.5 s：
                        # 够吃掉实测的 20 ms 边界，仍守在既有闸的 3 s 帽内（不会把启动卡死）。
                        _deadline = time.time() + 2.5
                        while not live and time.time() < _deadline:
                            time.sleep(0.25)
                            live = self._read_all_positions_live()
                    if live:
                        self._position_cache.update(live)
                        logger.info(
                            f"位置缓存基准已换成实时读数（{len(live)} 个），"
                            "布局文件未改动")
                    else:
                        logger.warning(
                            "位置缓存验证不通过且实时读数也失败，保留空缓存")
                        # ★R300-F1：轮询到 2 s 界仍读空 ⇒ 立个牌子。__init__ 后面
                        # 那次覆盖如果读到了，会把这行说成"前一句作废"，两条日志
                        # 不再互相打脸（win11 真机 32 ms 内先说读不到、再说读到 78 个）。
                        self._startup_cache_kept_empty = True
                elif extra:
                    # ★R242：missing 与 extra 同时存在时过去只报"多 N 个"，
                    #   读日志的人以为集合只差在多出来的那几个上。两数都报。
                    logger.info(
                        f"位置缓存验证: 当前桌面多 {len(extra)} 个图标"
                        + (f"、存档另有 {len(missing)} 个不在桌面" if missing else '')
                        + "，保留缓存")
                else:
                    logger.info("位置缓存验证通过：图标集合一致")
            except Exception as e:
                logger.debug(f"位置验证跳过: {e}")
        except Exception as e:
            logger.error(f"加载初始位置失败: {e}")

    def _save_hidden_state(self) -> bool:
        """★ R193（Qoder CORE-10）：返回写盘结果——此前丢弃返回值，写盘
        失败时内存已 pop 的恢复条目与磁盘分叉，重启按旧账把用户已见的
        图标重新藏掉（"图标不见了、软件不认账"）。★R207 NEW3-C7：损坏
        账本留底失败时本会话禁写（防整份旧账被空账覆盖）。"""
        if getattr(self, '_ledger_load_failed', False):
            logger.error("隐藏账本曾损坏且留底失败——本会话禁写防覆盖"
                         "（请手动处理 hidden_state.json 后重启）")
            return False
        ok = atomic_write_json(self._hidden_state_path, self._hidden, indent=2)
        if not ok:
            logger.error("隐藏状态写盘失败——内存与磁盘将分叉，"
                         "请检查磁盘空间/权限")
        return ok

    # -------------------------------------------------------------------
    # 虚拟系统图标（此电脑/回收站/网络/控制面板）注册表显隐
    # -------------------------------------------------------------------

    def _load_virtual_state(self):
        try:
            if os.path.exists(self._virtual_state_path):
                with open(self._virtual_state_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._virtual_managed = data
        except Exception as e:
            logger.error(f"读取虚拟图标接管状态失败: {e}")
            self._virtual_managed = {}

    def _save_virtual_state(self):
        # ★R284-4（P1）：这份状态记的是"本程序接管/隐藏了哪些虚拟图标"，丢了它
        #   退出/全部恢复时就找不到该还原的那批。旧写法既没 try 也没消费返回值，
        #   写失败彻底静默。补 try/except + 消费返回值留痕。
        try:
            ok = atomic_write_json(self._virtual_state_path,
                                   self._virtual_managed, indent=2)
        except Exception as e:
            logger.error(f"保存虚拟图标接管状态失败: {e}")
            return
        if not ok:
            logger.error("虚拟图标接管状态未落盘（文件可能被占用/只读）："
                         "下次退出/恢复可能找不到被隐藏的虚拟图标")

    def _virtual_query_raw(self, clsid: str) -> Optional[int]:
        """读该 CLSID 的注册表 DWORD；值缺失返回 None（系统默认状态）"""
        try:
            key = win32api.RegOpenKey(win32con.HKEY_CURRENT_USER,
                                      VIRTUAL_ICON_REGISTRY_PATHS[0])
            try:
                val, _ = win32api.RegQueryValueEx(key, clsid)
                return int(val)
            finally:
                win32api.RegCloseKey(key)
        except Exception:
            return None

    def _virtual_set_hidden(self, clsid: str, hidden: bool):
        """双注册表键写入，覆盖 Win10/11 与兼容模式差异"""
        for reg_path in VIRTUAL_ICON_REGISTRY_PATHS:
            key = win32api.RegCreateKey(win32con.HKEY_CURRENT_USER, reg_path)
            try:
                win32api.RegSetValueEx(key, clsid, 0, win32con.REG_DWORD,
                                       1 if hidden else 0)
            finally:
                win32api.RegCloseKey(key)

    def _virtual_delete_value(self, clsid: str):
        """删除值以恢复'值缺失'的原始状态（值缺失时部分图标默认不可见）"""
        for reg_path in VIRTUAL_ICON_REGISTRY_PATHS:
            try:
                key = win32api.RegOpenKey(win32con.HKEY_CURRENT_USER, reg_path,
                                          0, win32con.KEY_SET_VALUE)
                try:
                    win32api.RegDeleteValue(key, clsid)
                finally:
                    win32api.RegCloseKey(key)
            except Exception:
                pass

    def notify_virtual_change(self):
        """SHCNE_ASSOCCHANGED 强制 Explorer 按新注册表状态重建桌面（Dasktop 同款）"""
        try:
            ctypes.windll.shell32.SHChangeNotify(
                SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None)
        except Exception as e:
            logger.error(f"刷新桌面通知失败: {e}")

    def hide_virtual_icons(self, names: Iterable[str]) -> int:
        """隐藏指定虚拟系统图标，记录原始注册表值供恢复。返回本次新隐藏数。

        同一 CLSID 只接管一次（Administrator/用户文件夹 双别名同 CLSID，
        重复接管会把别名2的"当前值=已隐藏"记成原始值，恢复时互相污染）。"""
        n = 0
        managed_clsids = {info.get('clsid') for info in self._virtual_managed.values()}
        for name in names:
            clsid = VIRTUAL_DESKTOP_ICONS.get(name)
            if not clsid or name in self._virtual_managed:
                continue
            if clsid in managed_clsids:
                continue   # 该 CLSID 已由另一别名接管
            managed_clsids.add(clsid)
            prev = self._virtual_query_raw(clsid)
            try:
                self._virtual_set_hidden(clsid, True)
            except Exception as e:
                logger.error(f"隐藏虚拟图标失败 {name}: {e}")
                continue
            self._virtual_managed[name] = {'clsid': clsid, 'prev': prev}
            n += 1
        if n:
            self._save_virtual_state()
        return n

    def show_virtual_icons(self, names: Iterable[str]) -> int:
        """恢复由本程序隐藏的虚拟图标到原始注册表状态。返回恢复数。

        ★R297-C（用户 10-05 08:5x win11 真机"隐藏了系统图标，切几个分类又长回来"）：
        "还原"的判据是**接管那一刻记下的原值**，不是"一律显示"。旧写法 prev 只用来
        区分"值缺失就删值"，非缺失一律写"显示"⇒ 用户自己隐藏过（prev=1）的图标，
        被某个 exclusive_sys 分类接管一次再切走，就被这次"还原"请回桌面了——用户
        读作"程序偷偷改了我的设置"。定案口径：其他分组受系统页那枚开关约束，
        所以我们只把状态放回用户自己定下的那一格，不猜他要显示还是隐藏。"""
        n = 0
        for name in list(names):
            info = self._virtual_managed.get(name)
            if not info:
                continue
            prev = info.get('prev')
            try:
                if prev is None:
                    self._virtual_delete_value(info['clsid'])
                else:
                    # prev=0 → 显示；prev=1 → 仍然隐藏（原值就是 1，说明是用户自己
                    # 隐藏的，不是我们接管造成的，还原不该把它翻成显示）
                    self._virtual_set_hidden(info['clsid'], bool(prev))
            except Exception as e:
                logger.error(f"恢复虚拟图标失败 {name}: {e}")
                continue
            self._virtual_managed.pop(name, None)
            n += 1
        if n:
            self._save_virtual_state()
        return n

    def restore_managed_virtual_icons(self) -> int:
        """恢复所有被本程序隐藏的虚拟图标（退出/全部/启动自愈用）"""
        return self.show_virtual_icons(list(self._virtual_managed.keys()))

    def _virtual_unique_clsids(self):
        """五项系统图标的 (名字, CLSID)，按 CLSID 去重。

        表里 'Administrator' 与 '用户文件夹' 是同一枚 CLSID 的历史别名
        （icon_manager:81 注释），不去重会把同一枚图标写两遍、并且"用户文件夹"
        会被别名抢掉名字，日志里读成 'Administrator'。"""
        seen = {}
        for name, clsid in VIRTUAL_DESKTOP_ICONS.items():
            seen.setdefault(clsid, name)
        return list(seen.items())

    def virtual_icons_all_shown(self) -> bool:
        """五项系统图标是不是**都**在显示——系统页那枚复选框的初始态。

        读 NewStartPanel 那一支就够（写入是双键一起写，见 _virtual_set_hidden）。
        值=0 显示 / 值=1 隐藏 / **值缺失＝该图标的系统默认**，而默认按图标不同：
        回收站出厂就在桌面上，此电脑/网络/控制面板/用户文件夹在 Win10/11 默认
        **不**显示（这正是"桌面图标设置"那个小窗存在的原因）⇒ 缺失不能一律当"显示"，
        否则新机器上这枚复选框会谎报"已显示"。整支读不动（注册表被拦）同样回 False：
        初始不勾、用户点下去才是显式动作，比"看着勾上其实没显示"诚实。"""
        for clsid, name in self._virtual_unique_clsids():
            val = self._virtual_query_raw(clsid)
            if val is None:
                if name != '回收站':
                    return False
            elif val:
                return False
        return True

    def set_virtual_icons_shown(self, shown: bool):
        """★R292-②「一键显示系统图标」：五项一次性全显示 / 全隐藏。

        返回 (写成的枚数, 失败的图标名列表)。与 hide/show_virtual_icons 的分工要
        分清：那两枚是**分类切换**用的"接管—还原"（记下 prev，退出或切回时按 prev
        还原），这一枚给的是用户点名的**最终态**、不是接管，所以写完就把接管账清掉
        ——留着 prev 的话，下一次退出恢复会按旧值把用户刚点出来的结果又改回去
        （观感＝"我明明勾了，它自己又没了"）。
        单个 CLSID 写失败只记一行、继续写其余：部分成功要如实报，不许整体谎报。
        HKCU 不需要管理员权限（同 lock_screen._reg_write_nologoff 的先例）。"""
        ok = 0
        failed = []
        for clsid, name in self._virtual_unique_clsids():
            try:
                self._virtual_set_hidden(clsid, not shown)
                ok += 1
            except Exception as e:
                logger.error(f"设置系统图标显隐失败 {name}: {e}")
                failed.append(name)
        if self._virtual_managed:
            self._virtual_managed = {}
            self._save_virtual_state()
        logger.info(f"[系统图标] 一键{'显示' if shown else '隐藏'}: 写成={ok} "
                    f"失败={failed}（接管账已清，退出不再回灌旧值）")
        return ok, failed

    def _realign_restored(self, restored, tries=5, pause=0.5):
        """★ R195b：恢复后归位。目标=隐藏瞬间的位置快照（账本 'pos' 或
        _pos_snapshot）——R195 首版用 _position_cache 当目标，缓存整桌
        过期时把 42 个图标往旧坐标搬=反向乱跑（win11 日志"3 轮后仍未
        到位 42 个"实锤），已废弃。

        两步：①恢复者搬回自己的快照位；②被挤走的其他图标（快照有、
        现在可见但不在位）一并归位。Explorer 重现条目有 2s+ 延迟
        （win11 实测"期望 47 实际 36"），重现不齐先等再搬。
        restored: [(name, info_dict)]。"""
        if not restored:
            return
        snap = getattr(self, '_pos_snapshot', None) or {}
        # ★R204 NEW-C4：快照新鲜度——超 60s 的旧快照不再当"被挤走"判定
        # 基准（其间用户可能手动挪过全桌，第②步会把它们搬回旧坐标）
        snap_ts = getattr(self, '_pos_snapshot_ts', 0)
        snap_fresh = bool(snap) and (time.time() - snap_ts <= 60.0)
        targets = {}
        for name, info in restored:
            t = (info or {}).get('pos') or snap.get(name)
            if t:
                targets[name] = tuple(t)
        if not targets:
            return
        restored_names = set(targets)
        for i in range(tries):
            # ★R204 NEW-C5 复盘：首读前必须保留 0.7s 等待——R204 曾改成
            # "先读后等"，实测（R195b 大规模压测 11 个位移）Explorer 的
            # 重排落在归位之后，读得太早=把重排前的位置当基线。该等待
            # 是吸收 Explorer 重排的，不是冗余。
            time.sleep(pause if i else 0.7)   # 首轮多等 Explorer 重现条目
            try:
                live = self._read_all_positions_live()
            except Exception as e:
                logger.error(f'【恢复】位置读回失败: {e}')
                return
            if any(n not in live for n in targets) and i < tries - 1:
                continue   # 还有条目没重现完，等下一轮
            # ① 恢复者归位
            # ★R204 NEW-C2：搬移全程 try 隔离——此处抛异常会打断调用方的
            #  unhide_all 尾部（restore_virtual/_currently_hidden 被跳过）
            try:
                self._move_icons_to({n: t for n, t in targets.items() if n in live})
            except Exception as e:
                logger.error(f'【恢复】归位恢复者失败(不影响主流程): {e}')
            # ② 被挤走的其他图标归位（快照里有、现在可见但不在位）
            # ★R204 NEW-C4：仅新鲜快照参与判定，过期快照整步跳过
            try:
                others = {}
                if snap_fresh:
                    live = self._read_all_positions_live()
                    others = {n: p for n, p in snap.items()
                              if n not in restored_names and n in live
                              and (abs(live[n][0] - p[0]) > 2
                                   or abs(live[n][1] - p[1]) > 2)}
                elif snap:
                    logger.info('【恢复】位置快照已过期(>60s)，跳过被挤走图标归位')
                if others:
                    moved = self._move_icons_to(others)
                    logger.info(f'【恢复】归位被挤走图标 {moved} 个')
            except Exception as e:
                logger.error(f'【恢复】归位被挤走图标失败(不影响主流程): {e}')
            # ③ ★R204 读回校验（R85 教条）：批量恢复时 Explorer 的重排
            # 可能落在归位之后、把刚搬好的再挤一串（R195b 大规模压测
            # 实锤 8~11 个链式位移）。校验不齐→进入下一轮重搬，齐了
            # 才提前收工。
            if i < tries - 1:
                time.sleep(0.4)
                verify = self._read_all_positions_live()
                if not verify:
                    # ★R207 NEW3-C4：空读数=读失败，不可判定——不得虚报
                    # "校验通过"，也不再重试（重试也读不到）
                    logger.error('【恢复】读回校验读数为空（读失败），'
                                 '归位结果未知，停止重试')
                    return
                # ★R210（NEW4-C3）：缺项也算不在位——读数非空但缺目标条目
                # （隐藏→重现迟滞真实存在）时旧写法 `n in verify and …`
                # 把缺项过滤掉=虚报"校验通过"
                still_off = [n for n, t in targets.items()
                             if n not in verify
                             or abs(verify[n][0] - t[0]) > 2
                             or abs(verify[n][1] - t[1]) > 2]
                if not still_off:
                    # ★R207：延迟复核——Explorer 的重排可能落在上一次
                    # 校验之后（R195b 大规模压测二轮实测：校验通过后又
                    # 漂走 6 个尾部图标）。通过后再睡一拍复核一次，漂了
                    # 就补搬一轮（有界，只此一次，不进外层循环）。
                    time.sleep(0.8)
                    try:
                        again = self._read_all_positions_live()
                        if not again:
                            # ★R210（NEW4-C3）：空读数必须留痕，不按
                            # "无漂移"静默放行
                            logger.error('【恢复】延迟复核读数为空（读失败），'
                                         '归位结果未知')
                        else:
                            late = [n for n, t in targets.items()
                                    if n not in again
                                    or abs(again[n][0] - t[0]) > 2
                                    or abs(again[n][1] - t[1]) > 2]
                            if late:
                                logger.info(
                                    f'【恢复】延迟复核 {len(late)} 个被'
                                    '重排挤走/未收录，补搬一轮')
                                self._move_icons_to(
                                    {n: targets[n] for n in late})
                                time.sleep(0.5)
                                final = self._read_all_positions_live()
                                # ★R213（NEW5-C3）：final 空读数专哨——
                                # 空 {} 时 left=全部 targets，会被误判成
                                # "补搬后仍全体不在位/Explorer 未收敛"，
                                # 实为读取失败（与上面 again 空读数同源，
                                # 上轮只装了 again 这一半）
                                if not final:
                                    logger.error('【恢复】补搬后终读数为空'
                                                 '（读失败），归位结果未知')
                                    return
                                left = [n for n, t in targets.items()
                                        if n not in final
                                        or abs(final[n][0] - t[0]) > 2
                                        or abs(final[n][1] - t[1]) > 2]
                                if left:
                                    logger.warning(
                                        f'【恢复】补搬后仍有 {len(left)} 个'
                                        '不在位（Explorer 重排未收敛，'
                                        '留待下次归位）')
                    except Exception as e:
                        logger.error(
                            f'【恢复】延迟复核失败(不影响主流程): {e}')
                    logger.info(f'【恢复】归位读回校验通过（第 {i + 1} 轮）')
                    return
                logger.info(f'【恢复】读回校验 {len(still_off)} 个仍不在位，'
                            '再搬一轮')
            else:
                return

    def _move_icons_to(self, targets, exclude=()) -> int:
        """★ R195b：按 name→(x,y) 搬图标（读回比对，在位跳过，界外跳过）。

        realign_icons 的通用化——目标位置由调用方给（快照/缓存），不再
        绑死 _position_cache。返回实际搬动数。
        ★ R204 NEW-C2/C3：句柄获取全程入 try（拿不到 ListView/进程句柄
        返回 0 不抛，R190 兜底链不被炸穿）；移动成功后回写 _position_cache
        （dock:1706 realign 与 save_layout 不再吃到过期位置二次乱搬）。"""
        try:
            lv = self.hwnd_listview
        except Exception as e:
            logger.debug(f'_move_icons_to: ListView 句柄不可用: {e}')
            return 0
        if not lv or not targets:
            return 0
        # ★R234：同一道闸门——自动排列开着 ⇒ 写了也白写（实测 ret=0）。
        _aa = self.ensure_auto_arrange_off()
        if _aa != 'off':
            logger.warning(f"[_move_icons_to] 自动排列闸门={_aa}"
                           f"｜{self.auto_arrange_block_reason()}")
        try:
            vx = win32api.GetSystemMetrics(76)
            vy = win32api.GetSystemMetrics(77)
            vx1 = vx + win32api.GetSystemMetrics(78)
            vy1 = vy + win32api.GetSystemMetrics(79)
        except Exception:
            vx, vy, vx1, vy1 = 0, 0, 1920, 1080
        try:
            pid = win32process.GetWindowThreadProcessId(lv)[1]
            # ★ B6（R24）：VirtualAllocEx 失败时释放已打开的进程句柄（防泄漏）
            h = win32api.OpenProcess(win32con.PROCESS_ALL_ACCESS, False, pid)
        except Exception as e:
            logger.debug(f'_move_icons_to: 打开 Explorer 进程失败: {e}')
            return 0
        try:
            mem = win32process.VirtualAllocEx(h, 0, 64, MEM_COMMIT, PAGE_READWRITE)
        except Exception:
            try:
                win32api.CloseHandle(h)
            except Exception:
                pass
            raise
        if not mem:
            # ★R204 CORE-25 同型：分配失败早退，否则 ReadProcessMemory 全败
            # 被吞、按"全部在位"静默跳过（框/归位双盲）
            try:
                win32api.CloseHandle(h)
            except Exception:
                pass
            logger.debug('_move_icons_to: VirtualAllocEx 返回 0')
            return 0
        moved = 0
        try:
            name_to_idx = self._get_current_icon_map()
            # ★R237：realign/快照归位与 restore 共用同一套"碰撞安全"写入顺序
            #   （旧写法按 targets 字典序 ⇒ 同样会引发 Explorer 连锁顺延）
            _excl = set(exclude or ())
            _todo = {k: v for k, v in targets.items()
                     if k not in _excl and v}
            try:
                _live_now = dict(self._read_all_positions_live() or {})
            except Exception:
                _live_now = {}
            _order, _bumped = self.plan_write_order(_todo, _live_now)
            if _bumped:
                _layout_logger.info(
                    f"[order] _move_icons_to 注定被撞={len(_bumped)} {_bumped[:8]}")
            for name in _order:
                pos = _todo.get(name)
                idx = name_to_idx.get(name)
                if idx is None or not pos:
                    continue
                if not ((vx - 60) <= pos[0] <= (vx1 + 60)
                        and (vy - 60) <= pos[1] <= (vy1 + 60)):
                    continue
                if send_msg_timeout(
                    self.hwnd_listview, LVM_GETITEMPOSITION, idx, mem
                ) is None:
                    continue
                live = struct.unpack('ii',
                                     win32process.ReadProcessMemory(h, mem, 8))
                if abs(live[0] - pos[0]) <= 2 and abs(live[1] - pos[1]) <= 2:
                    continue
                lparam = pack_lparam(int(pos[0]), int(pos[1]))
                if send_msg_timeout(
                    self.hwnd_listview, LVM_SETITEMPOSITION, idx, lparam
                ) is None:
                    continue
                moved += 1
                # ★R204 NEW-C3：移动成功即回写缓存——这是唯一知道"新真实位置"
                # 的时刻；丢回写会让 realign/save_layout 按旧缓存再搬
                try:
                    self._position_cache[name] = (int(pos[0]), int(pos[1]))
                except Exception:
                    pass
            if moved:
                win32gui.InvalidateRect(self.hwnd_listview, None, True)
            return moved
        finally:
            win32process.VirtualFreeEx(h, mem, 0, MEM_RELEASE)
            win32api.CloseHandle(h)

    def realign_icons(self, names: Iterable[str]) -> int:
        """把 Explorer 放错位的重现图标搬回记忆位置（2026-09-02 新增）。

        属性法隐藏/恢复时，Explorer 偶发不按原位重现图标，而是丢到别的
        空网格位（回归实测 3 个图标集体漂到一列）。本方法对"实时位置 ≠
        记忆位置"的图标逐个搬回；记忆位置来自 _position_cache（隐藏前的
        最后一次实时读数，天然与网格吻合）。界外坐标一律跳过。
        ★ R195b：核心搬移抽到 _move_icons_to（目标可由调用方指定，供
        快照归位复用），本方法=以 _position_cache 为目标的兼容入口。"""
        targets = {n: self._position_cache[n] for n in names
                   if self._position_cache.get(n)}
        moved = self._move_icons_to(targets)
        if moved:
            logger.info(f"[校正] {moved} 个重现图标被 Explorer 放错位，已搬回原位")
        return moved

    def _name_to_path_map(self) -> Dict[str, str]:
        """桌面显示名 → 文件路径。
        Explorer 隐扩展名的是 `.lnk` 与 `.url`（★R311 注册表实测 NeverShowExt，
        口径只写在收纳盒模块的 display_name 一处），对 .txt/.bat 显示全名，
        两种键都登记（先登记全名，主名不覆盖已有的全名键）。
        ★R204 CORE-32：主名冲突记为歧义跳过——桌面同时有 报告.docx 与
        文件夹"报告"（或 x.lnk 与 x.txt）时，主名键先到先得会把属性
        改到用户没归类的那个上（"没动的文件夹突然消失"）。"""
        mapping: Dict[str, str] = {}
        stem_paths: Dict[str, set] = {}
        for d in _desktop_dirs():
            try:
                entries = os.listdir(d)
            except OSError as e:
                logger.error(f"列出桌面目录失败 {d}: {e}")
                continue
            for fn in entries:
                p = os.path.join(d, fn)
                if fn not in mapping:
                    mapping[fn] = p
                stem = os.path.splitext(fn)[0]
                if stem and stem != fn:
                    stem_paths.setdefault(stem, set()).add(p)
        for stem, ps in stem_paths.items():
            if stem in mapping:
                continue   # 全名键优先（维持原优先级）
            if len(ps) == 1:
                mapping[stem] = next(iter(ps))
            else:
                logger.debug(f"主名歧义跳过属性映射: {stem}")
        return mapping

    @staticmethod
    def _set_hidden_bits(path: str, hide: bool) -> bool:
        """★ R130：隐藏/恢复只增删 HIDDEN|SYSTEM 位，保留只读等其他属性。

        旧实现隐藏时写 H|S、恢复时写 NORMAL——两个方向都会把用户文件
        原有的只读/压缩等属性位抹掉。
        ★R210（NEW4-C7）：返回真实结果——调用方（回滚计数）不再恒报
        全成功。"""
        try:
            attrs = win32api.GetFileAttributes(path)
        except Exception as e:
            # ★R213（NEW5-C2）：读属性失败不能再"伪造 NORMAL"往下走——
            # 那会用 SetFileAttributes 把用户原有 READONLY/ARCHIVE 等位
            # 一起抹掉（NEW4-C7 的"保留其他位"契约在失败侧破口），且
            # 上层拿到 True 记成成功。与 _clear_system_bit 口径对称：
            # 读不到真实属性=无法安全增删位=直接 False，交调用方计 failed。
            logger.warning(f"读取文件属性失败 {path}: {e}")
            return False
        if attrs == -1:   # GetFileAttributes 失败返回 -1
            # ★R213（NEW5-C2）：同上——-1 是失败码，当属性用会写坏文件
            logger.warning(f"读取文件属性返回 -1（失败）{path}")
            return False
        mask = win32file.FILE_ATTRIBUTE_HIDDEN | win32file.FILE_ATTRIBUTE_SYSTEM
        new_attrs = (attrs | mask) if hide else (attrs & ~mask)
        if new_attrs != attrs:
            try:
                win32api.SetFileAttributes(path, new_attrs)
            except Exception as e:
                logger.warning(f"设置文件属性失败 {path}: {e}")
                return False
        return True

    @staticmethod
    def _clear_system_bit(path: str) -> bool:
        """★ R193（Qoder CORE-11）：只清 SYSTEM 指纹位，保留 HIDDEN。

        was_hidden=True（程序接管前用户自己就藏着）的文件恢复时必须走
        这里——_set_hidden_bits(hide=False) 会把用户自己的隐藏属性一起
        抹掉，用户隐藏的文件从此永久显形。
        ★R210（NEW4-C7）：返回真实结果。"""
        try:
            attrs = win32api.GetFileAttributes(path)
        except Exception:
            return False
        if attrs == -1:
            return False
        new_attrs = attrs & ~win32file.FILE_ATTRIBUTE_SYSTEM
        if new_attrs != attrs:
            try:
                win32api.SetFileAttributes(path, new_attrs)
            except Exception as e:
                logger.warning(f"清除 SYSTEM 位失败 {path}: {e}")
                return False
        return True

    def _set_file_hidden(self, path: str, hidden: bool):
        """切换文件的隐藏状态。

        用 HIDDEN|SYSTEM 双属性：用户开着"显示隐藏文件"时单 HIDDEN 属性
        的图标会以半透明露出，加 SYSTEM 属性后才彻底不可见（见模块头注释）。
        ★ R130：改走位运算增删，不抹掉用户原有属性。"""
        self._set_hidden_bits(path, hide=bool(hidden))
        self._notify_shell(path)

    def apply_visibility(self, visible_names: Iterable[str]) -> dict:
        """可见性同步：visible_names 之外的全部隐藏，其中被本程序隐藏过的恢复可见。

        返回 {'hidden': 新隐藏数, 'shown': 恢复数, 'failed': [无法隐藏的名字]}
        （无法隐藏 = 桌面文件里找不到对应文件，如 此电脑/回收站 等系统虚拟图标）
        属性修改在主线程完成（快且安全）；shell 通知走后台线程。
        """
        visible = set(visible_names)
        current_names = set(self._get_visible_icon_map().keys())
        name_map = self._name_to_path_map()

        hidden_cnt, shown_cnt, failed = 0, 0, []
        newly_hidden_names: List[str] = []  # 本次新隐藏的图标名（用于验证）
        newly_hidden_undo: Dict[str, str] = {}   # ★R207 NEW3-C2：name→回滚方式
        notify_paths: List[str] = []

        # 1) 恢复：之前被我们隐藏、现在应可见的
        _popped = []   # ★ R193（CORE-10）：写盘失败时回滚 pop（见保存处）
        restored_pairs = []   # ★ R195b：真正翻转了可见性的 (name, info)
        for name in list(self._hidden.keys()):
            if name in visible:
                info = self._hidden.pop(name)
                _popped.append((name, info))
                try:
                    if os.path.exists(info['path']):
                        if info.get('was_hidden'):
                            # ★ R193（CORE-11）：用户原本就藏着——只清我们的
                            # SYSTEM 指纹，HIDDEN 还给用户
                            # ★R213（NEW5-C1）：消费返回值——旧版假成功会
                            # 把条目当已恢复、用户文件仍藏着
                            if not self._clear_system_bit(info['path']):
                                raise OSError('清除 SYSTEM 指纹位失败')
                            notify_paths.append(info['path'])
                        else:
                            # ★ R194b（用户 22 批"手动隐藏文件切回全部属性被
                            # 清+顶掉图标"）：账本说"是我们的"但属性只剩
                            # 纯 HIDDEN（SYSTEM 指纹没了=用户手动改过/旧版
                            # 账错）→ 以指纹为准，别动他的属性。旧账在此
                            # 一并作废（已 pop）。
                            try:
                                _at = win32api.GetFileAttributes(info['path'])
                            except Exception:
                                _at = -1
                            _H = win32file.FILE_ATTRIBUTE_HIDDEN
                            _S = win32file.FILE_ATTRIBUTE_SYSTEM
                            if _at != -1 and (_at & _H) and not (_at & _S):
                                logger.info(
                                    f'【恢复】{name} 已是纯 HIDDEN（用户接管），'
                                    '保留其属性不清除')
                            elif _at != -1 and (_at & _H):
                                # ★R213（NEW5-C1）：消费返回值，失败保持跟踪重试
                                if not self._set_hidden_bits(info['path'],
                                                             hide=False):
                                    raise OSError('清除 HIDDEN 位失败')
                                notify_paths.append(info['path'])
                                restored_pairs.append((name, info))
                            # 已可见：无操作
                    # ★R204 NEW-C6：shown 只数真正翻转的条目——此前无操作/
                    # 用户接管/文件缺失也 +1，dock 的 expected 虚高导致
                    # _wait_listview_count 每轮等满 2s 超时+假数据
                except Exception as e:
                    logger.error(f"恢复图标失败 {name}: {e}")
                    self._hidden[name] = info  # 失败保持跟踪，下次重试
                    _popped.pop()
        shown_cnt = len(restored_pairs)

        # 2) 隐藏：应可见名单之外的
        # ★ R195b：隐藏前抓全桌面位置快照（恢复归位的唯一真值来源——
        # _position_cache 可能整桌过期，R195 首版拿它当目标曾把 42 个
        # 图标反向搬乱，win11 日志实锤）。有即将被隐藏的才读一次。
        _pos_snapshot = getattr(self, '_pos_snapshot', None) or {}
        if any(n not in visible and name_map.get(n)
               and os.path.exists(name_map[n]) for n in current_names):
            # ★R207 NEW3-C3：_read_all_positions_live 失败返回 {} 从不抛——
            # 空读数不得覆盖旧快照、不得刷新鲜 ts（否则恢复归位失去真值
            # 来源，"恢复后又乱跑"静默回归）
            _snap_new = self._read_all_positions_live()
            if _snap_new:
                self._pos_snapshot = dict(_snap_new)
                _pos_snapshot = self._pos_snapshot
                # ★R204 NEW-C4：快照带时间戳——过期快照不参与"被挤走"判定
                self._pos_snapshot_ts = time.time()
            else:
                logger.error('【隐藏】位置快照读数为空（读失败），沿用旧快照')
        for name in current_names:
            if name in visible:
                continue
            p = name_map.get(name)
            if not p or not os.path.exists(p):
                failed.append(name)
                continue
            try:
                if self._is_hidden(p):
                    # ★ R193b（用户 19 批实锤"手动隐藏的文件切分类还会显示"）：
                    # 已是隐藏状态的文件只接管**自家旧账**（老版本只设了
                    # HIDDEN 的自家文件，补 SYSTEM 指纹）——不在账上的=
                    # 用户自己藏的，完全不碰（原实现一律升级接管，切回
                    # "全部"恢复时把用户隐藏属性搞乱）
                    if name not in self._hidden:
                        continue
                    # 旧版本只设了 HIDDEN（用户开着显示隐藏文件会半透明
                    # 露出），升级为 HIDDEN|SYSTEM 双属性
                    try:
                        attrs = win32api.GetFileAttributes(p)
                        if not attrs & win32file.FILE_ATTRIBUTE_SYSTEM:
                            # ★R210（NEW4-C6）：走位运算助手，不再整字节
                            # 覆盖（保住 READONLY/ARCHIVE 等其他属性位）
                            if not self._set_hidden_bits(p, hide=True):
                                raise OSError('设置隐藏属性失败')
                            notify_paths.append(p)
                            self._hidden[name] = {
                                'path': p, 'was_hidden': False,
                                'pos': _pos_snapshot.get(name)}
                            hidden_cnt += 1
                            newly_hidden_names.append(name)  # 记录本次新隐藏的
                            # ★R207 NEW3-C2：升级分支回滚=只摘补上的 SYSTEM
                            newly_hidden_undo[name] = 'downgrade'
                    except Exception as e2:
                        logger.error(f"升级隐藏属性失败 {name}: {e2}")
                        failed.append(name)
                    continue
                # ★R210（NEW4-C6）：同上——位运算保其他属性位
                if not self._set_hidden_bits(p, hide=True):
                    raise OSError('设置隐藏属性失败')
                notify_paths.append(p)
                self._hidden[name] = {'path': p, 'was_hidden': False,
                                      'pos': _pos_snapshot.get(name)}
                hidden_cnt += 1
                newly_hidden_names.append(name)  # 记录本次新隐藏的
                # ★R207 NEW3-C2：普通隐藏回滚=整组清 H|S（恢复可见）
                newly_hidden_undo[name] = 'unhide'
            except Exception as e:
                logger.error(f"隐藏图标失败 {name}: {e}")
                failed.append(name)

        save_ok = True
        # ★R204 NEW-C6 后续校正：落盘条件按"账本是否变更"判定（恢复分支
        # pop 即便没有属性翻转也是账本变更，必须落盘）——原 hidden_cnt or
        # shown_cnt 在 shown_cnt 改为只数翻转后，会漏掉"仅 pop 无翻转"
        # 的场景造成内存/磁盘分叉
        if hidden_cnt or _popped:
            # ★ R193（CORE-10）：写盘失败回滚 pop——内存记账跟磁盘一致，
            # 不然重启按旧账把用户已见图标重新藏掉
            save_ok = self._save_hidden_state()
            if not save_ok:
                if _popped:
                    self._hidden.update(_popped)
                    logger.error("隐藏状态写盘失败，已回滚本次恢复登记"
                                 "（下次切换会重试写盘）")
                # ★R207 NEW3-C2（P1）：隐藏方向回滚必须**撤销属性**——
                # R204 只 pop 账本，文件还带着 H|S（物理已隐藏）→
                # 无账孤儿图标（切换/全部/账本三条路都救不回）。两方向
                # 不对称：恢复方向账本 pop 即可，隐藏方向必须恢复原属性。
                if newly_hidden_names:
                    _undone = 0
                    for _n in newly_hidden_names:
                        _info = self._hidden.pop(_n, None)
                        _p = (_info or {}).get('path') \
                            or name_map.get(_n) or ''
                        try:
                            _ok = False
                            if newly_hidden_undo.get(_n) == 'downgrade':
                                # 只摘补的 SYSTEM
                                _ok = self._clear_system_bit(_p)
                            else:
                                _ok = self._set_hidden_bits(_p, hide=False)
                            # ★R210（NEW4-C7）：按真实结果计数（助手此前
                            # 内部吞异常=恒报全成功）
                            if _ok:
                                _undone += 1
                            else:
                                logger.error(
                                    f"隐藏回滚撤销属性失败 {_n}（写失败）")
                        except Exception as _e:
                            logger.error(f"隐藏回滚撤销属性失败 {_n}: {_e}")
                    logger.error(f"隐藏状态写盘失败，已撤销 {len(newly_hidden_names)}"
                                 f" 条新隐藏（其中属性回滚成功 {_undone} 条，"
                                 "桌面恢复切换前状态；写盘问题解决后重试）")
        # ★ 关键：批量通知必须等待完成，否则 Explorer 可能还没刷新
        self._notify_shell_sync(notify_paths)
        # ★ R195：恢复的图标按记忆位置搬回+读回校验（Explorer 给重现
        # 条目随机找格=用户实测"顶掉别的图标/乱跑"）
        if restored_pairs:
            self._realign_restored(restored_pairs)
        # 实时隐藏集合 = _hidden 里**文件属性仍为 HIDDEN** 的条目
        # 不能假设 _hidden 全在隐藏状态——外部工具/用户可能手动改过文件属性
        self._currently_hidden = {
            name for name, info in self._hidden.items()
            if self._is_hidden(info.get('path', ''))
        }
        return {'hidden': hidden_cnt, 'shown': shown_cnt, 'failed': failed,
                'newly_hidden': newly_hidden_names, 'save_ok': save_ok}

    def unhide_all(self) -> int:
        """恢复所有被本程序隐藏的图标（含虚拟系统图标）。返回恢复数。

        不调用 _hidden.clear()：保留 _hidden 状态供后续 auto_backup 等流程参考。
        文件图标只改文件属性（FILE_ATTRIBUTE_NORMAL），不碰 _hidden 字典。
        """
        shown = 0
        notify_paths: List[str] = []
        restored_pairs = []   # ★ R195b：真正翻转了可见性的 (name, info)
        try:
            for name, info in list(self._hidden.items()):
                try:
                    if os.path.exists(info['path']):
                        # ★ R193/R194b：was_hidden=True 只清 SYSTEM 指纹位；
                        # was_hidden=False 先验指纹——纯 HIDDEN=用户接管，别动
                        if info.get('was_hidden'):
                            # ★R213（NEW5-C1）：消费返回值——假成功会把
                            # 未翻转的条目计入恢复
                            if not self._clear_system_bit(info['path']):
                                raise OSError('清除 SYSTEM 指纹位失败')
                            notify_paths.append(info['path'])
                        else:
                            try:
                                _at = win32api.GetFileAttributes(info['path'])
                            except Exception:
                                _at = -1
                            _H = win32file.FILE_ATTRIBUTE_HIDDEN
                            if _at != -1 and (_at & _H) and \
                                    not (_at & win32file.FILE_ATTRIBUTE_SYSTEM):
                                logger.info(
                                    f'【恢复全部】{name} 已是纯 HIDDEN'
                                    '（用户接管），保留其属性')
                            elif _at != -1 and (_at & _H):
                                # ★R213（NEW5-C1）：消费返回值
                                if not self._set_hidden_bits(info['path'],
                                                             hide=False):
                                    raise OSError('清除 HIDDEN 位失败')
                                notify_paths.append(info['path'])
                                restored_pairs.append((name, info))
                        # ★R207 NEW3-C9：shown 只数真翻转（同 NEW-C6 口径）
                except Exception as e:
                    logger.error(f"恢复图标失败 {name}: {e}")
            # 不清空 _hidden，让 auto_backup 仍能拿到完整图标列表参考
            # ★R204 NEW-C7：写盘失败至少留痕（本路径不 pop 无需回滚）
            if not self._save_hidden_state():
                logger.error("【恢复全部】隐藏账本写盘失败（保持内存态，重启后重试）")
            if notify_paths:
                self._notify_shell_async(notify_paths)
            # ★ R195：恢复的图标按记忆位置搬回+读回校验（同 apply_visibility）
            if restored_pairs:
                self._realign_restored(restored_pairs)
        finally:
            # ★R204 NEW-C2：尾部收尾进 finally——归位链抛异常不再跳过
            # 虚拟图标恢复与 _currently_hidden 收敛
            # 虚拟系统图标一并恢复（退出/全部时桌面必须回到全量状态）
            try:
                shown = len(restored_pairs)   # ★R207 NEW3-C9：只数真翻转
                shown += self.restore_managed_virtual_icons()
            except Exception as e:
                logger.error(f"【恢复全部】虚拟图标恢复失败: {e}")
            self._currently_hidden = set()
        return shown

    def recover_hidden_on_startup(self):
        """启动自愈：扫描桌面目录里带 HIDDEN|SYSTEM 双属性的文件/快捷方式，
        把它们恢复可见（无论 _hidden 字典里有没有登记）。

        只靠 _hidden 字典不够：
        - 退出选"保持当前"（close_restore_layout='当前'）时磁盘残留 HIDDEN|SYSTEM，
          但 _hidden 可能没有这 4 个条目的登记（老版本 apply_visibility 漏登记 /
          外部工具改属性 / 上次启动就带着老状态退出）。
        - 启动时 recover 只看 _hidden，直接跳过，磁盘状态永远不清。

        扫描兜底能兜住上述所有场景——凡是桌面上 HIDDEN|SYSTEM 双属性的图标都恢复，
        让桌面回到全量可见状态，随后分类逻辑（家长模式恢复、切分类）按需隐藏。

        判断用"双属性"而非"仅 HIDDEN"：
        - 程序隐藏 = HIDDEN|SYSTEM 双属性
        - 用户手动隐藏 = 只有 HIDDEN（Windows UI 不能设 SYSTEM）
        - 纯 HIDDEN 的文件不动，避免误恢复用户手动隐藏的东西。"""
        recovered_paths = []  # 收集所有恢复的路径，统一发 shell 通知
        recovered_files = []  # 只用于日志汇总

        HIDDEN = win32file.FILE_ATTRIBUTE_HIDDEN
        SYSTEM = win32file.FILE_ATTRIBUTE_SYSTEM

        # 1) 先处理 _hidden 里登记过的条目
        for name, info in list(self._hidden.items()):
            p = info.get('path', '')
            if not os.path.exists(p):
                # 路径暂时不存在（OneDrive 同步中 / 网络盘未挂载），保留登记
                # 等文件回来时下次启动再处理。只删真正能确认已不存在的。
                logger.warning(f'启动自愈跳过（路径不存在）: {name} → {p}')
                continue
            try:
                attrs = win32api.GetFileAttributes(p)
            except Exception as e:
                logger.error(f'启动自愈读属性失败 {name}: {e}')
                continue
            # 双属性 HIDDEN|SYSTEM = 程序隐藏的指纹
            # 纯 HIDDEN（用户手动设的）不动
            if (attrs & HIDDEN) and (attrs & SYSTEM):
                try:
                    if info.get('was_hidden'):
                        # ★ R193（CORE-11）：用户原本就藏着——只清指纹位，
                        # HIDDEN 还给用户（原恒 False 分支从不生效）
                        # ★R213（NEW5-C1）：消费返回值，失败不再计入已恢复
                        #（属性未翻转的文件留给桌面扫描兜底重试）
                        if not self._clear_system_bit(p):
                            raise OSError('清除 SYSTEM 指纹位失败')
                    else:
                        if not self._set_hidden_bits(p, hide=False):
                            raise OSError('清除 HIDDEN 位失败')
                    recovered_paths.append(p)
                    recovered_files.append(name)
                except Exception as e:
                    logger.error(f'启动自愈恢复失败 {name}: {e}')
                # 无论恢复成功还是 was_hidden=True，双属性条目都清掉
                self._hidden.pop(name, None)
            elif attrs & HIDDEN:
                # ★ R194b（用户 22 批）：纯 HIDDEN（无 SYSTEM）=用户已接管
                # （我们的指纹没了）——剔除登记。旧实现保留登记，恢复路径
                # 会按旧账清掉用户隐藏属性（账与实际脱节的根源）
                logger.info(f'启动自愈：{name} 纯 HIDDEN（用户接管），剔除登记')
                self._hidden.pop(name, None)
            else:
                # 文件已可见，僵尸记录清掉
                self._hidden.pop(name, None)

        # 2) 扫描桌面目录兜底：凡是 HIDDEN|SYSTEM 双属性的文件/文件夹都恢复
        # （只匹配"程序隐藏"指纹；用户手动设 HIDDEN 的不动）
        # ★R312（顺手收 ★R311 挂账第 1 条）：以前是 `{'desktop.ini'}` 这张
        #   大写敏感的字面表 ⇒ `Thumbs.db` 的 HIDDEN|SYSTEM 会被这道兜底扫描一起清掉
        #   （用户开着"显示隐藏文件"时桌面上就冒出一枚系统占位文件）。判据与全仓
        #   同一处 `_is_placeholder_name`（小写归一，desktop.ini / Thumbs.db 都算）。
        # ★R311：桌面显示名口径只许一处实现（Explorer 隐 `.lnk`/`.url`）。这里原来
        # 自己按后缀判 ⇒ 启动自愈把 `.url` 登记成 `foo.url`，而分类名单给的是 `foo`，
        # 两本账对不上。
        from core.desktop_box import display_name as _disp_name
        for d in _desktop_dirs():
            if not os.path.isdir(d):
                continue
            try:
                for fn in os.listdir(d):
                    if self._is_placeholder_name(fn):
                        continue
                    path = os.path.join(d, fn)
                    if not os.path.exists(path):
                        continue
                    try:
                        attrs = win32api.GetFileAttributes(path)
                    except Exception:
                        continue
                    # 双属性 = 程序隐藏的可靠指纹
                    if (attrs & HIDDEN) and (attrs & SYSTEM):
                        try:
                            # ★R213（NEW5-C1）：消费返回值，失败不计入已恢复
                            if not self._set_hidden_bits(path, hide=False):
                                raise OSError('清除 HIDDEN|SYSTEM 位失败')
                            recovered_paths.append(path)
                            # 显示名口径只有一处实现（见收纳盒模块的 display_name）
                            display_name = _disp_name(fn)
                            recovered_files.append(display_name)
                            # 同步登到 _hidden，持久化到 hidden_state.json，
                            # 下次切换分类时能正确跟踪
                            if display_name not in self._hidden:
                                self._hidden[display_name] = {
                                    'path': path, 'was_hidden': False}
                        except Exception as e:
                            logger.error(f'启动自愈恢复 {fn} 失败: {e}')
            except Exception as e:
                logger.error(f'启动自愈扫描 {d} 失败: {e}')

        # 无论 _hidden 是否为空都写盘：恢复成功后字典可能变空，
        # 不写盘会导致 hidden_state.json 残留旧条目，下次启动重复处理
        self._save_hidden_state()

        # 同步 _currently_hidden：否则 highlighter 会误认为这些已恢复的
        # 图标还隐藏着，跳过画框 → 用户看到"图标回来了但空白无框"
        self._currently_hidden = {
            name for name, info in self._hidden.items()
            if self._is_hidden(info.get('path', ''))
        }

        if recovered_paths:
            # 统一发 shell 通知让 Explorer 立即刷新（否则用户得按 F5 才看见）
            self._notify_shell_sync(recovered_paths)
            preview = ', '.join(recovered_files[:8])
            extra = f'...（+{len(recovered_files)-8} 更多）' if len(recovered_files) > 8 else ''
            logger.info(f'启动自愈：已恢复 {len(recovered_files)} 个隐藏图标 '
                        f'[字典+磁盘扫描]: {preview}{extra}')
        else:
            logger.info('启动自愈：无遗留隐藏图标')
