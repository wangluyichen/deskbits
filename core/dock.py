"""悬浮窗模块 - 顶部快捷切换栏 (Win32 原生窗口 + tkinter 定时器)

功能：
- 鼠标移到屏幕顶部自动出现，移开后自动隐藏（轮询检测，不依赖 TrackMouseEvent）
- Alt+Ctrl+E 热键切换（由 gui.py 注册全局热键）
- 点击槽位：显示该分类的图标（若有保存布局则恢复其位置），其余图标移出屏幕
- 不抢焦点（WS_EX_NOACTIVATE + SW_SHOWNA），不打断游戏/办公
- 可设置不置顶（被其他窗口覆盖）
"""
import os
import json
import time
import glob
import queue
import logging
import ctypes
import threading
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional, Dict, Callable, Tuple

import win32gui
import win32con
import win32api
from core.category_manager import CategoryManager   # ★R280-D：只借 is_rank_group 一把判据
from core import visibility_router   # ★R305：收纳档（双属性 / 真搬进盒子）唯一出口
from core.icon_manager import (VIRTUAL_DESKTOP_ICONS,
                               LVM_SETITEMPOSITION, pack_lparam)
from utils.safeio import atomic_write_json
from utils.win32_safe import send_msg_timeout
from utils import i18n   # ★ R73 双语

logger = logging.getLogger("desktop_manager")

# ★ R256（用户 48 批一1/一3）：统计采集**去钩子化**——原先用 pynput 全局
# 低级鼠标钩子（WH_MOUSE_LL）采点击，主进程被任务管理器"结束进程树"杀掉时
# 钩子链要等 LowLevelHooksTimeout（≈2s）才被系统摘除 ⇒ 全系统鼠标冻结一下。
# 用户 A/B 实锤：关统计不卡、开统计基本都卡。现改为 GetAsyncKeyState 轮询
# （只读查询，与悬浮条自家点击检测 :930 同一套路），不挂任何钩子。

WS_EX_NOACTIVATE = 0x08000000

LVM_GETITEMPOSITION = 0x1010  # wParam=索引, lParam=远程 POINT 指针
DENIED_STREAK_LIMIT = 400     # ★R318 C：连续 ~50 秒（poll_ms=125）读不到光标 ⇒ 算反常
DENIED_LOG_WINDOW = 60.0      # ★R318 C：反常之后每 60 秒只点名一次，不许把日志刷爆

# ★ R78：dock 工具槽双语显示层。槽名（slot.name）是逻辑标识（_handle_click/
# _paint 激活配色/UTILITY_SLOTS 过滤都按它比较），必须保持中文 canonical；
# 仅绘制文本随语言切换。分类名是用户数据、⏱倒计时是符号，都原样返回。
# ★R262（用户 61 批 1）：槽位显示改名「一键目录」→「一键打开」，英文
# 'Folders'→'Open'。slot.name 仍是中文 canonical '一键目录'（点击派发/
# UTILITY_SLOTS/家长模式/保留名比较全按它走），只有画出来的文本变。
_DOCK_EN = {'全部': 'All', '窗': 'Main', '一键目录': 'Open', '退出': 'Exit'}
_DOCK_ZH = {'一键目录': '一键打开'}   # 中文显示层改名映射（canonical→显示）


def _dock_disp(name: str) -> str:
    """槽位显示文本：工具槽中文 canonical → 当前语言，其余原样"""
    if name in _DOCK_EN:
        return i18n.tr(_DOCK_ZH.get(name, name), _DOCK_EN[name])
    return name


def visibility_log_line(mode, hidden, shown, boxed_out, back_in,
                        stuck, skipped_system) -> str:
    """一次收纳的结果 → 一行日志（★R314，真机 0.99-R031 定级）。

    历轮这里把 `len(failed)` 当失败数打出去，可 `failed` 装的是**属性法"找不到本体"**那份
    名单——刚搬进盒的那批（文件已经不在桌面上了）和系统虚拟图标全在里头 ⇒ 真机每次
    切换都成功、日志却打出"失败 62"（62 = 搬进盒子 57 + 系统图标跳过 5）。
    口径改成路由器已经算好的两份：`stuck`＝既没进盒子也藏不了的那几枚，
    `skipped_system`＝虚拟系统图标跳过（★R311：跳过数由路由器直接数，调用方不做减法）。
    """
    _s = [str(x) for x in (stuck or [])]
    return (f"[{mode}档] 新隐藏 {hidden}，恢复 {shown}，搬进盒子 {boxed_out}，"
            f"散回 {back_in}，卡住 {len(_s)}，系统图标跳过 {int(skipped_system or 0)}"
            + (f": {_s[:5]}" if _s else ""))


def _left_button_down() -> bool:
    """鼠标左键此刻是否被按住（★R234 抽成函数：看护的"拖动避让"要能测）。

    只用 0x8000（异步按下态）——`& 0x0001` 是读后即清的，会吃掉别的模块的判据。
    """
    try:
        import ctypes
        return bool(ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000)
    except Exception:
        return False


def _virtual_desktop_bounds() -> Tuple[int, int, int, int]:
    """虚拟桌面物理边界 (left, top, right, bottom)，含 60px 容差由调用方处理"""
    try:
        vx = win32api.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
        vy = win32api.GetSystemMetrics(77)   # SM_YVIRTUALSCREEN
        cw = win32api.GetSystemMetrics(78)   # SM_CXVIRTUALSCREEN
        ch = win32api.GetSystemMetrics(79)   # SM_CYVIRTUALSCREEN
        return (vx, vy, vx + cw, vy + ch)
    except Exception:
        return (0, 0, 1920, 1080)


def _in_virtual_desktop(x: int, y: int, margin: int = 60) -> bool:
    vx0, vy0, vx1, vy1 = _virtual_desktop_bounds()
    return ((vx0 - margin) <= x <= (vx1 + margin)
            and (vy0 - margin) <= y <= (vy1 + margin))


# 悬浮横条兜底色（R,G,B）：★R218 阶段 3 起配色由 apply_theme 从主界面
# COLORS 派生，这里只剩"没换肤前"的两个兜底档（原 5 档分类激活色随
# "每格铺色"的画法一起退役，历史见 工作进度qoder.md ★R218）
_SLOT_COLOR_ACTIVE_CAT = (100, 50, 30)    # 分类激活兜底：暖橙色
_SLOT_COLOR_HOVER = (70, 70, 95)          # 悬停兜底：浅灰蓝

# 鼠标下是"真桌面"的窗口类（Progman/WorkerW/Shell/桌面 SysListView）
_DESKTOP_WINDOW_CLS = frozenset((
    'Progman', 'WorkerW', 'SHELLDLL_DefView', 'SysListView32',
    'Shell_TrayWnd', 'Shell_SecondaryTrayWnd',
))


def _mouse_on_desktop(x: int = None, y: int = None) -> bool:
    """鼠标当前是否悬停在真正的桌面（而非其他应用窗口）上。

    规则：鼠标下的窗口链中直接可见 WorkerW/Progman/SHELLDLL_DefView/SysListView32
    → 算桌面；其他任何应用类/有标题栏的类 → 不算。
    用于悬浮条：打开浏览器/文档时，鼠标移到屏幕顶部也不应浮现悬浮条。
    （2026-09-02 自备份版移植，用户确认该体验正确）
    ★ R25：支持传入事件坐标（统计采集用队列里的事件位置判定，
    不取实时光标——事件与处理之间光标可能已移动）。"""
    try:
        pt = (x, y) if x is not None and y is not None else win32gui.GetCursorPos()
        h = win32gui.WindowFromPoint(pt)
        while h:
            cls = win32gui.GetClassName(h)
            if cls in _DESKTOP_WINDOW_CLS:
                return True
            # 其他应用窗口（Chrome_WidgetWin_1, IEFrame, CabinetWClass, Tk...）→ 不是桌面
            try:
                parent = win32gui.GetParent(h)
            except Exception:
                parent = 0
            if not parent:
                break
            h = parent
        # 遍历完也没找到桌面类 → 不在桌面
        return False
    except Exception:
        return False


def note_cursor_denied(streak, now, last_named=0.0):
    """这一次读光标被拒 ⇒ 返回 (新的连续次数, 这一刻要不要点名)。

    ★R318 C（真机 0.99-R033 日志里"解锁之后横条不再自动浮现/自动收起"那一段）：
    `GetCursorPos` 在锁屏/UAC 安全桌面下被拒是**预期**，★R270-R7 因此把它降到
    `debug` + 60 秒限频（win11 真机 3 小时 23,850 行的教训）。但"预期"只在那段
    时间内成立：一直读不到就是横条的自动浮现/自动收起整条链停摆，而默认日志级别
    下这件事**一行都看不见**（debug 不算错误、error 只走"非拒绝"分支），真机报回来
    只能重猜。判据收成这一枚纯函数——纯算术，不建窗口、不碰 Tcl，更不碰 `_wndproc`
    （悬浮条红线），门禁可以直接真调它钉"什么时候点名、点名的间隔"。
    """
    streak = int(streak) + 1
    named = (streak >= DENIED_STREAK_LIMIT
             and float(now) - float(last_named) >= DENIED_LOG_WINDOW)
    return streak, named


def note_cursor_ok(streak):
    """重新读得到光标 ⇒ 返回 (要不要写那一行"已恢复", 刚结束的那段连续次数)。

    ★R329（2026-10-16 日志包 R044 复盘）：`note_cursor_denied` 一旦点名，句子写的是
    "解锁后本应自行恢复；若已解锁仍不恢复请把这段日志发来"——可恢复的那一刻**一行都不
    写**（旧代码只把计数归零）。于是用户把日志发来核对时，只能看到一长串"读不到"，
    永远证不了"后来恢复了没有"，这条链在日志上是**单向的**。判据同样收成一枚纯函数：
    只有曾经达到过反常阈值的那一段才值得报告，普通的一次锁屏不打扰日志。
    """
    s = int(streak)
    return s >= DENIED_STREAK_LIMIT, s


def _lum(rgb) -> float:
    """感知亮度（0~255），用于"暗底/亮底"分支"""
    r, g, b = rgb
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _mix(a, b, k: float) -> tuple:
    """把 b 按 k 混进 a（k=0 全 a / k=1 全 b）——悬浮条所有派生色走这里，
    保证"同一条底 + 差一档亮度"的关系稳定，不再各主题手写常量。"""
    return (int(a[0] * (1 - k) + b[0] * k),
            int(a[1] * (1 - k) + b[1] * k),
            int(a[2] * (1 - k) + b[2] * k))


# ★R279-④（产品负责人 09-30 04 批 item4「我看不出来区别」）：
# 渐变**画上去了**，但幅度比眼睛在这条高度上能分的还小 ⇒ "看了等于没看"。
# 算给你看（条高＝字号 16 那档 40px，见 `_calc_height`；亮度＝感知亮度 0~255。
# 量具 tmp/r279/probe_grad_spans2.txt：喂**真色板** gui/theme_panel.UI_THEMES
# 四套，走下面那个唯一派生出口现算，不是手挑一个 hex 蒙混）：
#   旧 0.10/0.12/0.30：深色档 顶 +20.9 / 底 −5.9 ⇒ 上下总跨度 **26.8 级**，
#     四套实测 25.0~30.2，摊到 40px＝每像素 0.62~0.76 级；
#     高光线 +63.0 级但只占 1px（一条线撑不起"厚度"）。
#   新 0.26/0.26/0.50：总跨度 **66.0~66.2 级**（四套都过 66，不是单套好看），
#     每像素 1.65~1.66 级；高光线 +105.8。门禁 GR1 钉住"跨度 ≥ 60"，
#     防以后又调回"画了等于没画"的微差。
#   注：亮底（浅色/护眼）往上抬白本来就抬不动（顶到 255 了），那两套的跨度
#     主要来自"往下压深"——这是物理，不是 bug。
# 三个系数收在**这一处**——`__init__` 的兜底值和 `palette_from_colors` 的派生式
# 都读它们，免得"改了一处漏一处"（那正好又是看不出来的那种 bug）。
# ★R280-A（用户 06 批 item1「④：不明显」）：跨度从 27 级抬到 66 级他仍判"看不出"，
# 所以把幅度做成**三档可调**，且默认档直接给到 ~132 级；中间那档 (0.26/0.26/0.50)
# 与 ★R279 逐字同值（门禁 L2b 钉住"微渐变＝上批现状，一格未变"），
# 嫌浓的人可以退到「微渐变」或「朴素」（朴素＝★R278 刚做渐变时那组旧系数）。
GRADIENT_LEVELS = {
    '朴素': (0.10, 0.12, 0.30),      # ★R278-B 原值：跨度 25~30 级，用户判"看不出来"
    '微渐变': (0.26, 0.26, 0.50),    # ★R279-④ 现值：跨度 66 级
    '明显': (0.52, 0.52, 0.85),      # 本批默认：跨度 132 级（深色板实测）
}
GRAD_DEFAULT_LEVEL = '明显'


def grad_level_k(name):
    """档位名 → (上浅, 下深, 高光线) 三个混色系数。
    认不得的名字回默认档：坏存档不该打死绘制（与 icon_overlay.zone_tight_resolve 同口径）。"""
    return GRADIENT_LEVELS.get(name, GRADIENT_LEVELS[GRAD_DEFAULT_LEVEL])


# ★R280-D（用户 06 批 item7）：排行组在悬浮栏走**临时区**（不占 max_slots 个用户槽），
# 临时区自己封顶 6 枚——超出只挤自己人，用户的分类一枚都不会被撤。
# 6 = 本机实测的视觉上限：横条再长就要把 ✕ 推出屏外（_position_window 按槽宽累加）。
RANK_SLOT_MAX = 6



# ---------------------------------------------------------------------
# ★R278-B 悬浮栏美化一期：竖向渐变底（纯 GDI，msimg32.GradientFill）
#   为什么不逐行 FillRect：条高 ~34px，逐行要每帧新建/删除 34 个画刷，
#   ★R204 CORE-22 刚把 GDI 对象泄漏治完，不能往回走。GradientFill 一次调用、
#   零画刷，且只碰 GDI ⇒ `_wndproc`/`_paint` 的纯 GDI 红线不动。
# ---------------------------------------------------------------------
_GRADIENT_FILL_RECT_V = 0x00000001


class _TRIVERTEX(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long),
                ("Red", ctypes.c_ushort), ("Green", ctypes.c_ushort),
                ("Blue", ctypes.c_ushort), ("Alpha", ctypes.c_ushort)]


class _GRADIENT_RECT(ctypes.Structure):
    _fields_ = [("UpperLeft", ctypes.c_ulong), ("LowerRight", ctypes.c_ulong)]


# argtypes 必须显式声明：HDC/结构体指针在 64 位下按默认 int 传会被截断
_GradientFill = ctypes.windll.msimg32.GradientFill
_GradientFill.argtypes = [ctypes.c_void_p, ctypes.POINTER(_TRIVERTEX),
                          ctypes.c_ulong, ctypes.POINTER(_GRADIENT_RECT),
                          ctypes.c_ulong, ctypes.c_ulong]
_GradientFill.restype = ctypes.c_int


def _gradient_v(hdc, x0, y0, x1, y1, c_top, c_bot) -> bool:
    """把 (x0,y0)-(x1,y1) 用 c_top→c_bot 竖向渐变铺满。True=已铺上。

    拿不到 msimg32 / hdc 失效 / 坐标退化 ⇒ 返回 False，**调用方必须退回纯色
    FillRect**：美化不能把绘制搞崩（★R204 CORE-7/CORE-22 的教训是画刷必删、
    异常路径也要能画出一帧）。"""
    if x1 - x0 < 2 or y1 - y0 < 2:
        return False
    try:
        vts = (_TRIVERTEX * 2)()
        vts[0].x, vts[0].y = x0, y0
        vts[0].Red = int(c_top[0]) * 256
        vts[0].Green = int(c_top[1]) * 256
        vts[0].Blue = int(c_top[2]) * 256
        vts[1].x, vts[1].y = x1, y1
        vts[1].Red = int(c_bot[0]) * 256
        vts[1].Green = int(c_bot[1]) * 256
        vts[1].Blue = int(c_bot[2]) * 256
        rect = _GRADIENT_RECT(0, 1)
        return _GradientFill(int(hdc), vts, 2, ctypes.byref(rect), 1,
                             _GRADIENT_FILL_RECT_V) != 0
    except Exception as e:
        logger.warning(f"[悬浮栏] 渐变填充不可用，退回纯色底: {e}")
        return False


def _contrast_text(rgb):
    """按背景亮度自动选字色：亮底配深字、暗底配浅字（R153 浅色主题
    悬浮栏文字看不清的修复——用户定案：字色自动对比+用户自定义优先）。
    """
    r, g, b = rgb
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    return (40, 40, 40) if lum > 150 else (235, 238, 245)


def _hex_to_rgb(h: str):
    h = (h or "#8be9fd").lstrip('#')
    if len(h) == 6:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    return (0x8b, 0xe9, 0xfd)


def _muted(r: int, g: int, b: int,
           br: int = 24, bg_: int = 24, bb: int = 34,
           k: float = 0.34) -> tuple:
    """★R218 阶段 3 起停用（悬浮条改整条单色底，不再"每格铺分类色"）。
    保留一个版本周期作回滚参照，历史：
    2026-09-02 此前整槽用分类原色填充，红/粉分类让横条一眼全是红 → 改低饱和
    调和；2026-09-10 参数顺序修正为 (r,g,b, br,bg_,bb, k)。"""
    return (int(br * (1 - k) + r * k),
            int(bg_ * (1 - k) + g * k),
            int(bb * (1 - k) + b * k))


_gdi32 = ctypes.windll.gdi32
DEFAULT_CHARSET = 1        # win32con.DEFAULT_CHARSET
CLEARTYPE_QUALITY = 4      # win32con.CLEARTYPE_QUALITY
FW_BOLD = 700


def _gdi_create_font(size: int, bold: bool = True, face: str = "Microsoft YaHei UI") -> int:
    """ctypes 创建字体（pywin32 无 win32gui.CreateFont）"""
    return _gdi32.CreateFontW(
        -size, 0, 0, 0, FW_BOLD if bold else 400,
        0, 0, 0, DEFAULT_CHARSET,
        0, 0, CLEARTYPE_QUALITY, 0, face)


@dataclass
class QuickSlot:
    """快捷槽配置"""
    name: str
    color: str = "#8be9fd"
    layout_path: Optional[str] = None

    def to_dict(self) -> dict:
        return {"name": self.name, "color": self.color, "layout_path": self.layout_path}

    @classmethod
    def from_dict(cls, data: dict) -> 'QuickSlot':
        return cls(
            name=data.get("name", "未命名"),
            color=data.get("color", "#8be9fd"),
            layout_path=data.get("layout_path"),
        )


def _sync_cache_into_layout(im, lay_path, cache, cat_names):
    """切分类后把位置缓存**回写**进那份布局存档 → `(写出条目数, 同格让位数)`。

    ★R319-K12：这是第三条布局写盘路径（另两条是 ★R299 盯过的"全部同步"和
    "手动保存"），旧写法在这里**直改 JSON**——`bd['icons'][name] = pos` 逐条抄
    缓存再 `atomic_write_json`，既不过同格去重、也不过占位剔除 ⇒ 缓存里那个真实
    读到的 `desktop.ini` 与挤在同一格的两枚被原样写回存档，下次回放又把占位文件
    摆回主屏（真机症状＝「全部」那份 79/78 交替，★R312）。现在与另两条同口径：
    先 `dedupe_layout_cells` 再 `_strip_placeholders`。
    清洗只作用于**写出去的那份**：`cache`（＝`_position_cache`）本体保持桌面
    真实读数——它同时是守护与读回验证的基准，抹平它会造成假位移。
    """
    import json as _j
    with open(lay_path, 'r', encoding='utf-8') as f:
        bd = _j.load(f)
    merged = dict(bd.get('icons') or {})
    for name, pos in dict(cache or {}).items():
        if not cat_names or name in cat_names:
            merged[name] = pos
    clean, moves = im.dedupe_layout_cells(merged)
    clean = im._strip_placeholders(clean, '存档(切分类回写)')
    bd['icons'] = clean
    bd['icon_count'] = len(clean)
    from datetime import datetime
    bd['timestamp'] = datetime.now().isoformat()
    atomic_write_json(lay_path, bd, indent=4)
    return len(clean), len(moves)


class DesktopDock:
    """桌面悬浮窗 - 顶部快捷切换栏"""

    _class_registered = False
    _by_hwnd: Dict[int, "DesktopDock"] = {}

    def __init__(self, icon_manager, category_manager, click_tracker,
                 config_dir: str = None, tk_root=None, log: Callable[[str], None] = None):
        self.icon_manager = icon_manager
        self.category_manager = category_manager
        self.click_tracker = click_tracker
        self.config_dir = config_dir or str(Path(__file__).parent / "config")
        self.tk_root = tk_root
        self._log_cb = log or (lambda m: logger.info(m))
        self.highlighter = None  # 由 gui.py 注入
        self._hidden_positions_cache = {}  # 保存被移到屏幕外的图标位置

        # 行为配置
        self.font_size = 16
        self.text_color = (235, 238, 245)   # 未选中文字色（可自定义）
        self.active_text_color = (255, 255, 255)  # 选中/激活文字色（可自定义）
        # ★ R153：用户自定义文字色（管理页两按钮；None=自动对比档）
        self._custom_text = None
        self._custom_active = None           # 横条字体（可调：小13/中16/大20）
        # ★R218 阶段 3：条高＝字号 + 24（文字区 + 3px 分类色条 + 1px 边线 +
        # 上下留白）。原 38/`font+20` 两处不一致，且 16px 字挤在 36px 里发闷。
        self.dock_height = self._calc_height(16)
        # 底部圆角半径（顶部贴屏边保持直角）：SetWindowRgn 裁出"悬挂卡片"
        self.corner_radius = 9
        # ★R218：悬浮条配色（由 apply_theme 从主界面 COLORS 派生；这里是
        # 深色兜底值，保证未换肤前也自洽）
        self._bg = (24, 24, 34)        # 条底：整条一个色，不再一格一色
        self._text = (235, 238, 245)   # 常规文字
        self._primary = (139, 233, 253)  # 激活文字（跟主界面强调色）
        self._hover = (70, 70, 95)     # 悬停胶囊底
        self._base = (24, 24, 34)      # 兼容旧字段：分类色调和基色
        self._active_fill = _mix(self._bg, self._primary, 0.24)
        self._sep = _mix(self._bg, self._text, 0.16)   # 槽间 1px 分隔线
        self._edge = _mix(self._bg, self._text, 0.30)  # 底部 1px 边线
        # ★R278-B：渐变底两端 + 顶部 1px 高光线（未换肤前的深色兜底值，
        # 与 apply_theme 的派生式同一条公式 ⇒ 首帧与换肤后不会跳色）
        # ★R280-A：幅度按档位取（默认「明显」），gui 侧读设置后 set_gradient_level 覆写
        self.grad_level = GRAD_DEFAULT_LEVEL
        _kt, _kb, _kh = grad_level_k(self.grad_level)
        self._grad_top = _mix(self._bg, (255, 255, 255), _kt)
        self._grad_bot = _mix(self._bg, (0, 0, 0), _kb)
        self._hilite = _mix(self._bg, (255, 255, 255), _kh)
        # ★ R78：槽宽自适应——按显示文本 GDI 实测宽 + 左右内边距，长分类名
        # 自动加宽（旧版固定 78px 会把 Computer/Entertainment 裁成
        # ompute/ntertainm，用户实测 2026-09-16）；总宽超屏再等比压缩。
        self.slot_pad = 16             # 每槽文字左右内边距（px，R218 14→16）
        self.slot_min_w = 44           # 单槽最小宽度（✕/Main 等短槽）
        self.slot_max_text = 160      # 单槽文字最大宽，超出加省略号截断
        self._text_w_cache = {}       # (font_size, text) -> px 实测宽缓存
        self.max_slots = 12           # 分类槽位最多显示多少个（工具槽不计入）
        self.enabled = True            # 总开关（gui 可关闭）
        # ★ P1-F HFONT 缓存：_paint 每 120ms 一次，此前每帧 CreateFont/DeleteObject
        #   会累积 GDI 句柄碎片；按 (font_size, bold, face) 三元组缓存，
        #   font_size 变更时由 set_font_size 调 _clear_hfont_cache 失效。
        self._hfont_cache = {}
        self.auto_show_top = True      # 鼠标移到屏幕顶部自动出现
        self.always_on_top = True      # False = 允许被其他窗口覆盖
        self.hide_delay_ms = 500       # 鼠标离开后延迟隐藏
        self.poll_ms = 120             # 鼠标位置轮询间隔

        # 末尾工具槽：全部=恢复全部图标，窗=显示主窗口，✕=退出程序
        self.UTILITY_SLOTS = ('全部', '窗', '一键目录', '✕')
        self.on_exit_app = None        # gui 注入：点 ✕ 退出整个程序
        self.on_show_window = None     # gui 注入：点 窗 显示主窗口
        self.system_resident = True    # 系统图标常驻（网格勾选）：True=各分类都显示
        self.on_open_folders = None    # gui 注入：点 一键目录 打开当前文件夹组
        self.on_run_auto_open = None   # gui 注入：★R262 批11 联动——按"自动打开设置"三列一键执行
        self.on_switch_link_folders = None   # gui 注入：切分类后联动同名文件夹组
        self.apply_cat_layout = False  # 复选项：切分类时是否自动套用同名布局（默认关，
                                       # 手动摆放图标时不被打扰；gui 可开）
        # ★R249 一2①：gui 注入「切分类摆布局**之前**安静清掉桌面活动排序」的回调
        # （免鼠标官方接口那条，一根手指都不占；不注入 ⇒ 一切照旧，不 AttributeError）
        self.silent_sort_fix = None
        # gui 注入：可见性变化后强制高亮框重绘（隐藏前后条目位置不变，
        # 位置签名比对不出来，必须显式通知）
        self.on_visibility_changed = None
        # gui 注入：切换完成后的回调（写 switch.log）
        self.on_switch_done = None
        # ★ B18（S 段）：桌面图标双击打开采集——gui 注入回调（usage_stats）
        self.on_desktop_icon_open = None
        # ★ R30：统计采集总开关（gui 按持久化设置初始化）——关闭时不记
        # 分类切换/图标打开，全局鼠标监听也不启动
        self.stats_enabled = True
        self._click_state = {}                 # ★ R25：点击状态机（down 位置/上次完成点击）
        # ★ R256：轮询线程（替代历轮 pynput 监听器；不挂任何全局钩子）
        self._mouse_poll_thread = None
        self._mouse_poll_stop = None
        # ★ B26（R25）：钩子回调只入队，重活（跨进程/窗口链检查）在工作线程——
        # LL 钩子回调超时（≈300ms）会被 Windows 静默摘除，双击事件丢失
        # （实测根因：第二击恰逢 Explorer 忙于启动程序）
        self._mouse_events = queue.Queue()
        self._mouse_worker_stop = threading.Event()   # ★ R130：bool→Event（可等待）
        self._mouse_worker = None

        # 防抖：快速连点时合并为一次切换，避免竞态和资源浪费（800ms 窗口）
        self._debounce_timer: Optional[int] = None
        self._debounce_delay = 200  # ms（从 800 减少，提升响应速度）
        self._processing = False  # 标记是否正在执行切换

        # 家长模式（儿童桌面）
        self.kids_mode = False
        self.kids_category = '小朋友'
        self.on_kids_exit = None       # parental 注入：点 [退出] 弹密码框
        self.on_kids_show_all = None   # parental 注入：儿童模式点 [全部] 只恢复儿童图标

        # 状态（注意：_pending_save_slots 必须先于 _load_slots 初始化，
        # 否则迁移标记会被这里的默认值覆盖、迁移永远不落盘）
        self._pending_save_slots = False
        # ★R281-2a（用户 08 批 item2a）：排行组「本次上栏」的登记表——**只在进程内**，
        # 全文件只有 __init__/add_session_rank_slot/drop_session_rank_slot 三处给它赋值
        # （闸：测试_ui/test_r281_session_rank.py X3），sync_slots 只读不写（X4）。
        # 为什么不写进 docks.json：那张盘存的是"用户自己摆的槽"，排行组是分类表的
        # 派生物（★R280-S6 已把它整类排除在盘外）；而"本次上栏"要的正是**这次显示、
        # 下次不算** ⇒ 进程内一张列表就是它全部的寿命，重启自然回到没登记的状态。
        self._session_rank: List[str] = []
        self.slots: List[QuickSlot] = self._load_slots()
        self.hwnd = None
        self.is_visible = False
        self.hovered_slot = -1
        self._active_slot = -1          # 当前激活（已点击选中）的槽位索引
        self._active_slot_name = None   # 当前激活槽位的名称（供 gui 同步用）
        self._outside_since = None
        self._hide_after_id = None
        self._poll_after_id = None
        self._polling = False
        self._btn_down = False      # 鼠标左键按下状态（点击检测用）
        self._press_slot = None     # 按下时所在槽位

        # 缓存：图标名→索引（图标增删或 Explorer 重启才失效）、槽位布局
        self._icon_map_cache = None
        self._icon_map_key = None
        self._layout_cache = {}
        self._auto_saved = False       # 本次会话是否已自动备份全量布局

        self._register_class()
        self._create_window()
        if self._pending_save_slots:
            self._save_slots()
            self._pending_save_slots = False

    # -------------------------------------------------------------------------
    # 日志
    # -------------------------------------------------------------------------

    def _log(self, msg: str):
        try:
            self._log_cb(msg)
        except Exception:
            logger.info(msg)

    # -------------------------------------------------------------------------
    # 定时器（优先用 tkinter after，跨线程安全问题少）
    # -------------------------------------------------------------------------

    def _after(self, ms: int, fn):
        if self.tk_root is not None:
            try:
                return self.tk_root.after(ms, fn)
            except Exception:
                pass
        t = threading.Timer(ms / 1000.0, fn)
        t.daemon = True
        t.start()
        return t

    def _after_cancel(self, tid):
        if tid is None:
            return
        # ★R204 CORE-20：_after 在 tk_root.after 抛异常时会回退 threading.Timer，
        # 此处必须按对象实际类型取消——按 tk_root 有无分支会把 Timer 交给
        # after_cancel（必失败被吞），到点后在工作线程执行含 Tcl 的回调。
        if isinstance(tid, threading.Timer):
            try:
                tid.cancel()
            except Exception:
                pass
            return
        if self.tk_root is not None:
            try:
                self.tk_root.after_cancel(tid)
            except Exception:
                pass
            return
        try:
            tid.cancel()
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # 窗口类注册与创建
    # -------------------------------------------------------------------------

    @classmethod
    def _register_class(cls):
        if cls._class_registered:
            return
        wc = win32gui.WNDCLASS()
        wc.hInstance = win32gui.GetModuleHandle(None)
        wc.lpszClassName = "DesktopDockClass"
        wc.style = win32con.CS_HREDRAW | win32con.CS_VREDRAW
        wc.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
        wc.hbrBackground = win32con.COLOR_WINDOW + 1
        wc.lpfnWndProc = DesktopDock._wndproc
        try:
            win32gui.RegisterClass(wc)
        except win32gui.error:
            pass  # 已注册
        cls._class_registered = True

    def _create_window(self):
        if self.hwnd and win32gui.IsWindow(self.hwnd):
            return
        w = self._dock_width()
        h = self.dock_height
        ex_style = (win32con.WS_EX_TOOLWINDOW | win32con.WS_EX_LAYERED |
                    WS_EX_NOACTIVATE)
        if self.always_on_top:
            ex_style |= win32con.WS_EX_TOPMOST
        self.hwnd = win32gui.CreateWindowEx(
            ex_style, "DesktopDockClass", "DesktopDock",
            win32con.WS_POPUP,
            0, 0, w, h,
            0, 0, win32gui.GetModuleHandle(None), None
        )
        if self.hwnd:
            # ★R218：225→242。旧值配合"每格一色"的色块还能糊过去，改成整条
            # 单色底后，半透明会让文字发灰（ClearType 在分层窗口上被整体
            # 拉低 α 时最明显）；242 仍留一点"透出桌面"的轻量感。
            win32gui.SetLayeredWindowAttributes(self.hwnd, 0, 242, win32con.LWA_ALPHA)
            win32gui.ShowWindow(self.hwnd, win32con.SW_HIDE)
            DesktopDock._by_hwnd[self.hwnd] = self

    # -------------------------------------------------------------------------
    # 消息处理（静态 WNDPROC，按 hwnd 分发到实例）
    # -------------------------------------------------------------------------

    @classmethod
    def _wndproc(cls, hwnd, msg, wparam, lparam):
        # 崩溃教训（2026-08-29）：本进程的窗口过程由 Tk 主线程的消息泵派发，
        # 在 WNDPROC 里调用任何 Tcl API（连 tk_root.after 都算）都可能触发
        # Tcl 原生 abort（code=3 无声闪退）。所以这里只保留纯 GDI 绘制，
        # 点击/悬停全部改由 _poll 的鼠标轮询检测（纯 Tcl 定时器上下文）。
        inst = cls._by_hwnd.get(hwnd)
        if inst:
            try:
                if msg == win32con.WM_PAINT:
                    inst._paint(hwnd)
                    return 0
                if msg == win32con.WM_DESTROY:
                    cls._by_hwnd.pop(hwnd, None)
                    inst.hwnd = None
                    inst.is_visible = False
                    return 0
            except Exception as e:
                logger.error(f"dock wndproc error: {e}")
                return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _paint(self, hwnd):
        hdc, ps = win32gui.BeginPaint(hwnd)
        # ★R204 CORE-22：画刷/字体全程 try/finally——异常路径也必须删本帧
        # 画刷、还原字体，否则每帧泄漏 GDI 对象（与 CORE-7 叠加加速耗尽）
        old_font = None
        old_pen = None
        try:
            rect = win32gui.GetClientRect(hwnd)
            width, height = rect[2], rect[3]
            texts, ws = self._slot_layout()
            n = max(len(texts), 1)

            # ★R218 阶段 3（用户："悬浮栏差点意思"）：整条改成**单一底色**。
            # 旧画法每槽铺一格"分类色→_muted 低饱和→深色再压暗"的色块，
            # 4 套主题实测都是一条彩虹马赛克（浅色主题最明显：一排蜡笔格），
            # 文字还逐格自动对比 ⇒ 深浅不一。现在分类身份只留在文字下方的
            # 3px 短色条，悬停/激活用圆角胶囊，槽间 1px 分隔线，底边 1px 收口。
            bg = getattr(self, '_bg', None) or (24, 24, 34)
            chip_h = 3                     # 分类色条高
            body_h = max(self.font_size + 8, height - chip_h - 1)   # 文字区高
            # ★R278-B（悬浮栏美化一期，用户 `----2026-9-30 03----`）：整条底从
            # 单色 FillRect 换成**上浅下深竖向微渐变** + 顶部 1px 高光线。
            # 渐变铺不上（msimg32 拿不到/hdc 失效）就退回 ★R218 的纯色底 ⇒
            # 美化永远压不垮绘制。
            if not self._paint_backdrop(hdc, width, height):
                bbrush = win32gui.CreateSolidBrush(win32api.RGB(*bg))
                try:
                    win32gui.FillRect(hdc, (0, 0, width, height), bbrush)
                finally:
                    win32gui.DeleteObject(bbrush)

            hfont_key = (self.font_size, True, "Microsoft YaHei UI")
            hfont = self._hfont_cache.get(hfont_key)
            if hfont is None:
                hfont = _gdi_create_font(*hfont_key)
                self._hfont_cache[hfont_key] = hfont
            old_font = win32gui.SelectObject(hdc, hfont)
            win32gui.SetBkMode(hdc, win32con.TRANSPARENT)
            # RoundRect 用当前画笔 ⇒ 全程挂 NULL_PEN（胶囊只填不描）
            old_pen = win32gui.SelectObject(
                hdc, win32gui.GetStockObject(win32con.NULL_PEN))

            _x = 0
            for i, slot in enumerate(self.slots):
                x0 = _x
                x1 = width if i == n - 1 else x0 + ws[i]
                sw = max(x1 - x0, 1)
                _x = x1
                cr, cg, cb = _hex_to_rgb(slot.color)
                _t = texts[i] if i < len(texts) else slot.name
                tw, th = win32gui.GetTextExtentPoint32(hdc, _t)

                # ① 槽间分隔线（上下各留白，不与圆角/色条打架）
                if i > 0:
                    sep = getattr(self, '_sep', None) or _mix(bg, (255, 255, 255), .16)
                    sb = win32gui.CreateSolidBrush(win32api.RGB(*sep))
                    try:
                        win32gui.FillRect(hdc, (x0, 7, x0 + 1, body_h - 6), sb)
                    finally:
                        win32gui.DeleteObject(sb)

                # ② 胶囊底 + 文字色：激活=强调色文字（用户自定义优先，R153）
                hover_top = None
                if i == self._active_slot:
                    fill = getattr(self, '_active_fill', None) or _SLOT_COLOR_ACTIVE_CAT
                    _tc = (getattr(self, '_custom_active', None)
                           or getattr(self, '_primary', None)
                           or _contrast_text(fill))
                elif i == self.hovered_slot:
                    # ★R280-G（N-10②，用户 06 批 item4「修」）：悬停那格从"平涂一档"
                    # 改成**抬亮一档 + 顶边 1px 反光**，鼠标离开自动回原样。
                    # 不新增定时器：hovered_slot 本来就由 _poll 每 120ms 更新（:1054+），
                    # 这里只多算两个色 ⇒ `_paint`/`_wndproc` 的纯 GDI 红线不动。
                    _hv = getattr(self, '_hover', None) or _SLOT_COLOR_HOVER
                    fill = _mix(_hv, (255, 255, 255), 0.35)
                    hover_top = _mix(fill, (255, 255, 255), 0.45)
                    _tc = (getattr(self, '_custom_text', None)
                           or _contrast_text(fill))
                else:
                    fill = None
                    _tc = (getattr(self, '_custom_text', None)
                           or getattr(self, '_text', None)
                           or _contrast_text(bg))
                if fill:
                    pbr = win32gui.CreateSolidBrush(win32api.RGB(*fill))
                    try:
                        ob = win32gui.SelectObject(hdc, pbr)
                        try:
                            win32gui.RoundRect(hdc, x0 + 3, 3, x1 - 3, body_h - 2,
                                               12, 12)
                        finally:
                            win32gui.SelectObject(hdc, ob)
                    finally:
                        win32gui.DeleteObject(pbr)
                    if hover_top is not None:
                        # 反光只画在胶囊**内部**（左右各进 8px、上下留圆角），
                        # 渐变填充会糊出圆角，所以这里用 1px FillRect 而不是 GradientFill
                        hbr = win32gui.CreateSolidBrush(win32api.RGB(*hover_top))
                        try:
                            win32gui.FillRect(hdc, (x0 + 11, 4, max(x1 - 11, x0 + 12), 5), hbr)
                        finally:
                            win32gui.DeleteObject(hbr)
                win32gui.SetTextColor(hdc, win32api.RGB(*_tc))
                win32gui.ExtTextOut(hdc, x0 + (sw - tw) // 2,
                                    (body_h - th) // 2, 0, None, _t)

                # ③ 分类身份色＝文字正下方 3px 短条（宽度跟文字走，不铺满格）
                cw = min(max(tw, 18), 72)
                cx0 = x0 + (sw - cw) // 2
                cbr = win32gui.CreateSolidBrush(win32api.RGB(cr, cg, cb))
                try:
                    win32gui.FillRect(hdc, (cx0, height - 1 - chip_h,
                                            cx0 + cw, height - 1), cbr)
                finally:
                    win32gui.DeleteObject(cbr)

            # ④ 底边 1px：与桌面之间有明确收口
            edge = getattr(self, '_edge', None) or _mix(bg, (255, 255, 255), .3)
            ebr = win32gui.CreateSolidBrush(win32api.RGB(*edge))
            try:
                win32gui.FillRect(hdc, (0, height - 1, width, height), ebr)
            finally:
                win32gui.DeleteObject(ebr)
        finally:
            if old_pen is not None:
                try:
                    win32gui.SelectObject(hdc, old_pen)
                except Exception:
                    pass
            if old_font is not None:
                try:
                    win32gui.SelectObject(hdc, old_font)   # 还原原始字体；hfont 留在缓存里复用
                except Exception:
                    pass
            win32gui.EndPaint(hwnd, ps)

    def _paint_backdrop(self, hdc, width, height) -> bool:
        """★R278-B：铺"上浅下深"渐变底 + 顶部 1px 高光线。True=已铺满。

        单独成方法是为了**无头取证**：测试可以在内存 DC 上调它，再 GetPixel 量
        顶行/底行的亮度差，不必真起悬浮条（起窗口要注册窗口类+消息循环，代价大）。
        只调 GDI（msimg32 + FillRect + 画刷 try/finally），不碰 Tcl。
        """
        top = getattr(self, '_grad_top', None)
        bot = getattr(self, '_grad_bot', None)
        if not top or not bot:
            return False
        if not _gradient_v(hdc, 0, 0, width, height, top, bot):
            return False
        hl = getattr(self, '_hilite', None)
        if hl:
            hb = win32gui.CreateSolidBrush(win32api.RGB(*hl))
            try:
                win32gui.FillRect(hdc, (0, 0, width, 1), hb)
            finally:
                win32gui.DeleteObject(hb)
        return True

    def _slot_at_point(self, x: int, y: int) -> int:
        """屏幕坐标 → 槽位索引（不在横条内返回 -1）

        ★ R79c：槽宽自适应（R78）后必须按 _slot_layout 的变宽分格换算——
        旧等分公式与实际绘制边界错位：点"全部"落到别的槽（偶尔才准）、
        Main/Folders 互相窜（2026-09-16 用户实测）。"""
        n = len(self.slots)
        if n == 0 or not self.hwnd:
            return -1
        r = win32gui.GetWindowRect(self.hwnd)
        if not (r[0] <= x < r[2] and r[1] <= y < r[3]):
            return -1
        _, ws = self._slot_layout()
        offset = x - r[0]
        acc = 0
        for i, w in enumerate(ws):
            acc += w
            if offset < acc:
                return i
        return n - 1

    def _activate_group(self, name, cfg):
        """子菜单选中组 → 记录最近使用 + 置为当前组 + 打开（显式打开）"""
        try:
            from core import explorer_tabs as _et
            _et.touch_group(cfg, name)
            _et.set_active_group(cfg, name)
        except Exception as e:
            # ★R204 CORE-38：组丢失/写盘失败不再静默吞（无现场线索）
            logger.warning(f"文件夹组记录更新失败 [{name}]: {e}")
        if self.on_open_folders:
            try:
                self.on_open_folders(True)   # explicit=True：绕过启动静默窗
            except Exception as e:
                logger.error(f"打开文件夹组 [{name}] 失败: {e}")

    def _run_auto_open(self):
        """★R262 批11 联动：一键打开走资源页「自动打开设置」三列计划（组/高频/图标）。

        回调在 gui 侧 `_startup_open_folders(manual=True)`：手动触发视为打开意图，
        总开关只 govern 开机自启；列级启用开关仍决定这次到底开哪几列。"""
        if not self.on_run_auto_open:
            self._log('⚡ 一键打开未接入自动打开设置（主界面异常？）')
            return
        try:
            self.on_run_auto_open()
        except Exception as e:
            logger.error(f"一键打开（按自动设置）失败: {e}")

    def _show_folder_group_menu(self, groups, auto_on=False):
        """一键目录：Tk 菜单列出文件夹组，选中由菜单命令自行调度打开。

        ★ 不能用"返回组名"模式：tk_popup 在菜单收起瞬间就返回，而 command
        事件此时往往还排在 Tcl 队列里，同步 destroy 会把待执行的回调一并
        销毁——表现为点了子菜单毫无反应且无日志（2026-09-05 用户实测复现，
        独立 Tk 实验证实 command 在 popup 返回后才入队执行）。
        故动作在 command 里经 after(0) 自行调度；destroy 也延迟 200ms。"""
        import tkinter as tk
        if self.tk_root is None:
            # ★ P1-6 无 Tk：退化为直接打开当前组，避免抛异常
            if self.on_open_folders:
                try:
                    self.on_open_folders(True)
                except Exception as e:
                    logger.error(f"打开文件夹组失败（无 Tk 回退）: {e}")
            return None
        cfg = getattr(self, 'config_dir', None) or 'config'
        # ★R218：菜单色跟随悬浮条派生调色板（apply_theme 后 _bg/_text 已是当前主题值）
        menu_bg = '#%02x%02x%02x' % self._bg
        menu_fg = '#%02x%02x%02x' % self._text
        menu_hi = '#%02x%02x%02x' % self._primary
        menu = tk.Menu(self.tk_root, tearoff=0,
                       font=('Microsoft YaHei UI', 10),
                       bg=menu_bg, fg=menu_fg,
                       activebackground=menu_hi, activeforeground=menu_bg)

        def _pick(nn):
            self._after(0, lambda nn=nn: self._activate_group(nn, cfg))

        # ★R262 批11 联动：置顶一条"按自动打开设置"——勾了哪列开哪列，
        # 与开机自动打开同一份配置、同一条解析管线（resolve_startup_plan）。
        if auto_on:
            menu.add_command(
                label=i18n.tr('  ⚡ 按自动打开设置  ', '  ⚡ Run auto-open  '),
                command=lambda: self._after(0, self._run_auto_open))
            menu.add_separator()
        MAX_SHOWN = 8
        for g in groups[:MAX_SHOWN]:
            n = g.get('name', '')
            cnt = len(g.get('folders', []))
            menu.add_command(
                label=i18n.tr(f"  📂 {n}  ({cnt}个)  ", f"  📂 {n}  ({cnt})  "),
                command=lambda nn=n: _pick(nn))
        if len(groups) > MAX_SHOWN:
            sub = tk.Menu(menu, tearoff=0,
                          font=('Microsoft YaHei UI', 10),
                          bg=menu_bg, fg=menu_fg,
                          activebackground=menu_hi, activeforeground=menu_bg)
            for g in groups[MAX_SHOWN:]:
                n = g.get('name', '')
                cnt = len(g.get('folders', []))
                sub.add_command(
                    label=i18n.tr(f"  📂 {n}  ({cnt}个)  ", f"  📂 {n}  ({cnt})  "),
                    command=lambda nn=n: _pick(nn))
            menu.add_cascade(label=i18n.tr('  更多…', '  More…'), menu=sub)
        menu.add_separator()
        menu.add_command(label=i18n.tr('  取消', '  Cancel'), command=lambda: None)
        try:
            x, y = win32api.GetCursorPos()
            menu.tk_popup(x, y + 8)
        finally:
            menu.grab_release()
            try:
                self._after(200, menu.destroy)   # 延迟销毁：放行排队的 command
            except Exception:
                pass
        return None

    def _handle_click(self, idx: int):
        """槽位点击处理（运行在 Tcl 定时器上下文，可安全调用任何 UI）"""
        if not (0 <= idx < len(self.slots)):
            return
        name = self.slots[idx].name
        if self.kids_mode:
            if name.startswith('⏱'):
                return
            if name == '退出':
                if self.on_kids_exit:
                    try:
                        self.on_kids_exit()
                    except Exception as e:
                        logger.error(f"退出家长模式回调异常: {e}")
                return
            # ★ 家长模式：禁止切换分类，只允许退出
            if name not in ('全部', '窗', '✕'):
                self._log('👶 家长模式：请先退出后再切换分类')
                return
        # ★ 一键目录槽（显示名"一键打开"）：弹出文件夹组菜单（最近使用优先；组多时冷门收进"更多"）
        if name == '一键目录':
            auto_on = False
            try:
                from core import explorer_tabs
                cfg = getattr(self, 'config_dir', None) or 'config'
                st = explorer_tabs.load_state(cfg)
                groups = explorer_tabs.sorted_groups(st.get('groups', []))
                # ★R262 批13（用户 13 批 1）：菜单项**忽略**卡里的"自动打开"总开关——
                # 那个开关只管开机要不要自动执行；人手动点"一键打开"本身就是意图，
                # 只要三列里有任何一列启用，就给留位（列里勾了什么就一键开什么）。
                so = explorer_tabs.sanitize_startup_open(st.get('startup_open'))
                auto_on = bool(so['groups_enabled'] or
                               so['folders_enabled'] or so['desktop_enabled'])
            except Exception:
                groups = []
            if not groups and not auto_on:
                # ★ #5：空组（未配置任何文件夹组）→ 弹提示引导，不再弹空菜单
                self._log('📂 尚未保存文件夹组：先在主界面"📂 资源"页"新建组/存为组"')
                return
            # ★ #5（用户澄清后拍板）：不再对"仅一个组"做特殊处理——
            #  原优化"单组直开"已回退，现在单组也走统一菜单，交互保持一致。
            #  菜单选中后由 _activate_group 自行调度（tk_popup 收起即返回，
            #  不能依赖返回值——command 可能尚未执行）
            self._show_folder_group_menu(groups, auto_on)
            return
        if name == '全部':
            _all_result = None
            try:
                if self.kids_mode and self.on_kids_show_all:
                    self.on_kids_show_all()  # 儿童模式：只恢复儿童图标
                else:
                    _all_result = self._show_all_icons()
                    self._log('✓ 已显示全部图标')
            except Exception as e:
                logger.error(f"显示全部失败: {e}")
                # ★R287-N20：以前这条分支只进 logger——用户点了"全部"什么都没出来，
                # 横条这边一个字不说，切换日志（switch.log）还记成 0/0/0 的"成功样"。
                # 兜底可以吞异常，静默不行（★R230b），且失败的计数不能冒充成功。
                self._log(f'⚠ 显示全部失败：{e}')
                _all_result = {'hidden': 0, 'shown': 0, 'failed': ['显示全部失败: %s' % e]}
            self._active_slot = idx
            self._active_slot_name = '全部'
            # ★ R15：点"全部"也计入分类切换榜（此前缺失——用户发现）
            try:
                if self.stats_enabled:
                    self.click_tracker.record_click('全部')
            except Exception:
                pass
            # ★ 点"全部"也要记录：否则退出时在"全部"，重启却恢复成
            #   上一次的分类（2026-09-05 用户实测反馈）
            if self.on_switch_done:
                try:
                    r = _all_result or {}
                    # ★R323 L3（用户「那一发打真值（现在念的是旧数）」）：旧写法只带
                    # hidden/shown/failed/back 四项，于是 switch.log 那一发永远是
                    # 「档=hide 搬进盒子=0 布局: 未套用」——收纳档、布局、散回那一步的
                    # 六格分解全在 dock 里被丢掉，念的是缺省的旧数。整份回执直接递过去。
                    info = dict(r)
                    info.setdefault('hidden', 0)
                    info.setdefault('shown', 0)
                    info.setdefault('failed', [])
                    info.setdefault('back', 0)
                    # ★R319-K15：收纳分解要一路传到切换日志
                    self.on_switch_done('全部', info,
                                        bool(r.get('layout_applied')))
                except Exception as e:
                    logger.warning('[切换日志] 「全部」那一发回执没递出去: %s', e)
            # 先 repaint 让颜色变化可见，再延迟隐藏（避免 paint 被 hide 截断）
            self._repaint_if_visible()
            if self.is_visible:
                # ★R285-N19：这个 id 以前是扔掉的——`_hide_after_id` 全文件只有
                # `= None` 一处赋值，于是 :1058/:1077/:1120/:2722 那四处 cancel
                # 恒对 None，destroy 之后到点的 pending 仍会触发 hide_dock。
                self._hide_after_id = self._after(self.poll_ms, self.hide_dock)
            return
        if name == '窗':
            if self.on_show_window:
                try:
                    self.on_show_window()
                except Exception as e:
                    logger.error(f"显示主窗口失败: {e}")
            return
        if name == '✕':
            if self.on_exit_app:
                try:
                    self.on_exit_app()
                except Exception as e:
                    logger.error(f"退出程序失败: {e}")
            return
        # 分类槽（隔离异常：点击横条绝不能带崩程序）
        try:
            self._activate_slot(idx)
        except Exception as e:
            logger.error(f"切换分类失败: {e}")
            self._log(f'⚠ 切换分类出错: {e}')

    # -------------------------------------------------------------------------
    # 显示 / 隐藏 / 轮询
    # -------------------------------------------------------------------------

    @staticmethod
    def _calc_height(font_size: int) -> int:
        """条高唯一事实源（构造与 set_font_size 共用，避免两处公式漂移）"""
        return max(34, int(font_size) + 24)

    def _apply_shape(self):
        """★R218 阶段 3：底部圆角。区域顶边推到窗口外 ⇒ 上沿直角（贴屏边）、
        下沿圆角，悬浮条从"一条马赛克矩形"变成"悬挂卡片"。
        SetWindowRgn 之后区域归窗口所有，**不要** DeleteObject（二次释放会崩）。"""
        if not self.hwnd:
            return
        try:
            r = max(0, int(self.corner_radius))
            l, t, rr, b = win32gui.GetWindowRect(self.hwnd)
            w, h = rr - l, b - t
            if w <= 0 or h <= 0:
                return
            hrgn = win32gui.CreateRoundRectRgn(0, -r, w + 1, h + 1, r * 2, r * 2)
            if hrgn:
                win32gui.SetWindowRgn(self.hwnd, hrgn, True)
        except Exception as e:
            logger.debug(f"悬浮条圆角设置失败（不影响功能）: {e}")

    def _position_window(self):
        if not self.hwnd:
            return
        screen_w = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
        w = self._dock_width()
        h = self.dock_height
        x = max(0, (screen_w - w) // 2)
        insert = win32con.HWND_TOPMOST if self.always_on_top else win32con.HWND_NOTOPMOST
        win32gui.SetWindowPos(self.hwnd, insert, x, 0, w, h,
                              win32con.SWP_NOACTIVATE)
        self._apply_shape()      # ★R218：宽/高变了，圆角区域要跟着重裁

    def _measure_text(self, text: str) -> int:
        """GDI 实测文本宽度（与 _paint 同字体），按 (字号, 文本) 缓存"""
        key = (self.font_size, text)
        w = self._text_w_cache.get(key)
        if w is not None:
            return w
        try:
            hdc = win32gui.GetDC(0)
            try:
                hfont_key = (self.font_size, True, "Microsoft YaHei UI")
                hfont = self._hfont_cache.get(hfont_key)
                if hfont is None:
                    hfont = _gdi_create_font(*hfont_key)
                    self._hfont_cache[hfont_key] = hfont
                old = win32gui.SelectObject(hdc, hfont)
                try:
                    w = win32gui.GetTextExtentPoint32(hdc, text)[0]
                finally:
                    win32gui.SelectObject(hdc, old)
            finally:
                win32gui.ReleaseDC(0, hdc)
        except Exception:
            w = len(text) * self.font_size + 8   # 无 DC 兜底估算
        if len(self._text_w_cache) > 512:        # 分类反复重命名也不撑爆
            self._text_w_cache.clear()
        self._text_w_cache[key] = w
        return w

    def _ellipsis(self, text: str) -> str:
        """超单槽上限的文字加省略号截断（避免个别超长名把整条撑满）"""
        if self._measure_text(text) <= self.slot_max_text:
            return text
        while text and self._measure_text(text + '…') > self.slot_max_text:
            text = text[:-1]
        return text + '…'

    def _slot_layout(self):
        """每槽最终显示文本与宽度（翻译/截断/压缩后的唯一事实源）。

        _dock_width（SetWindowPos 定窗宽）与 _paint（分格绘制）共用同一份
        结果，保证窗口总宽和槽位分格永远一致。"""
        texts = [self._ellipsis(_dock_disp(s.name)) for s in self.slots] or ['']
        ws = [max(self._measure_text(t) + self.slot_pad * 2, self.slot_min_w)
              for t in texts]
        total = sum(ws)
        try:
            avail = win32api.GetSystemMetrics(win32con.SM_CXSCREEN) - 40
        except Exception:
            avail = 0
        if avail > 200 and total > avail:
            k = avail / total
            ws = [max(int(w * k), 24) for w in ws]
        self._dump_layout_diag(texts, ws)
        return texts, ws

    def _dump_layout_diag(self, texts, ws):
        """★R218：把槽位宽度写进 config/_dock_layout.json，供诊断工具 _dock_click.py
        定位非等宽槽位的点击坐标。内容不变则不写盘；仅诊断用途，异常吞掉不影响绘制。"""
        try:
            payload = {'texts': list(texts), 'ws': list(ws)}
            if getattr(self, '_layout_diag_cache', None) == payload:
                return
            import os
            path = os.path.join(self.config_dir, '_dock_layout.json')
            atomic_write_json(path, payload)
            self._layout_diag_cache = payload
        except Exception:
            pass

    def _dock_width(self) -> int:
        """紧凑宽度 = 各槽自适应宽之和（R78：随文字长度伸缩）"""
        return max(sum(self._slot_layout()[1]), 1)

    def show_dock(self):
        """立即显示悬浮窗"""
        if not self.enabled:
            self._log("悬浮窗已禁用，请在设置中开启")
            return
        self._after_cancel(self._hide_after_id)
        self._hide_after_id = None
        self._outside_since = None
        self._create_window()
        # ★R204 CORE-21：创建失败（hwnd=0）时不能继续 ShowWindow/InvalidateRect
        if not self.hwnd:
            logger.error("悬浮窗创建失败（hwnd=0），放弃显示")
            self.is_visible = False
            return
        self._position_window()
        win32gui.ShowWindow(self.hwnd, win32con.SW_SHOWNA)
        self.is_visible = True
        self.hovered_slot = -1
        # 不重置 _active_slot：悬浮窗靠鼠标靠近顶部自动出现时保留选中状态
        win32gui.InvalidateRect(self.hwnd, None, True)
        self._start_poll()

    def hide_dock(self):
        """立即隐藏悬浮窗"""
        self._after_cancel(self._hide_after_id)
        self._hide_after_id = None
        self._do_hide()

    def toggle_dock(self):
        if self.is_visible:
            self.hide_dock()
        else:
            self.show_dock()

    def _do_hide(self):
        if self.hwnd and self.is_visible:
            win32gui.ShowWindow(self.hwnd, win32con.SW_HIDE)
        self.is_visible = False
        self._outside_since = None
        self.hovered_slot = -1

    def _start_poll(self):
        if self._polling:
            return
        self._polling = True
        self._poll()

    def _poll(self):
        """轮询：鼠标位置（顶部出现/离开隐藏）+ 悬停高亮 + 点击检测。
        全部运行在 Tcl 定时器上下文——绝不依赖窗口过程。"""
        if not self._polling:
            return
        try:
            if not self.enabled:
                if self.is_visible:
                    self._do_hide()
            else:
                pt = win32gui.GetCursorPos()
                # ★R318 C：读得到就把连续被拒的计数归零——不归零的话，用户每天
                # 锁屏几次累加起来的"连续次数"会在某一刻假报成"一直读不到"。
                # ★R329：归零前把那一段的次数取出来，达到过反常阈值的补一行"已恢复"
                # ——被拒点名那句承诺了"解锁后本应自行恢复"，可恢复的一刻旧代码零痕迹，
                # 用户照着日志根本判不出"恢复了"还是"还在停"。
                _den = getattr(self, '_cursor_denied', 0)
                self._cursor_denied = 0
                if note_cursor_ok(_den)[0]:
                    logger.info(
                        f"dock 读光标已恢复（此前连续 {_den} 次被拒，"
                        f"约 {_den * self.poll_ms / 1000.0:.0f} 秒）⇒ 横条的自动浮现/"
                        f"自动收起重新接上")
                # ★ P1-D 改用 0x8000（当前是否按下）+ 状态机，不再用 0x0001（自上次查询按下过）
                # 0x0001 是全局的——用户在桌面别处点一下再移到 dock 上会误触。
                # 现在改成：在 dock 内按下 → 记录 _btn_down=True；按钮释放时若鼠标仍在 dock → 触发 click。
                btn_pressed = bool(win32api.GetAsyncKeyState(0x01) & 0x8000)
                if self.is_visible and self.hwnd:
                    r = win32gui.GetWindowRect(self.hwnd)
                    inside = r[0] <= pt[0] < r[2] and r[1] <= pt[1] < r[3]
                    if inside:
                        self._outside_since = None
                        self._after_cancel(self._hide_after_id)
                        self._hide_after_id = None
                        # 悬停高亮
                        idx = self._slot_at_point(pt[0], pt[1])
                        if idx != self.hovered_slot:
                            self.hovered_slot = idx
                            win32gui.InvalidateRect(self.hwnd, None, True)
                        # ★ 点击检测：两阶段
                        #   1) 在 dock 内按下 → 记录 _btn_down + 当前 idx
                        #   2) 之前按下过 + 现在释放 + 鼠标仍在 dock → 触发 click
                        if btn_pressed and idx >= 0:
                            self._btn_down = True
                            self._press_slot = idx
                        elif self._btn_down and not btn_pressed:
                            # 按钮刚释放：若鼠标仍在 dock 内（任何槽位或空白）→ 触发记录的槽位
                            self._btn_down = False
                            click_idx = getattr(self, '_press_slot', -1)
                            self._press_slot = -1
                            if click_idx >= 0:
                                self.hovered_slot = -1
                                self._handle_click(click_idx)
                    else:
                        # 鼠标离开 dock：丢弃按下状态（避免别处点击残留）
                        self._btn_down = False
                        self._press_slot = -1
                        if self.hovered_slot != -1:
                            self.hovered_slot = -1
                            win32gui.InvalidateRect(self.hwnd, None, True)
                        if self._outside_since is None:
                            self._outside_since = time.time()
                        elif time.time() - self._outside_since >= self.hide_delay_ms / 1000.0:
                            self._do_hide()
                elif self.auto_show_top:
                    # ★ 只有鼠标下是"真桌面"才自动浮现（开网页/文档时不再打扰）
                    if pt[1] <= 1 and _mouse_on_desktop():
                        self.show_dock()
        except Exception as e:
            # 降噪（R174 精确化，win11 18.7h 审计：121 条/天全是本类）：
            # 锁屏/UAC 安全桌面激活时 GetCursorPos 被正常拒绝（WinError 5），
            # 属预期 → debug 静默；其余错误保留 ERROR 且 60 秒限频。
            msg = str(e)
            now = time.time()
            if '拒绝访问' in msg or 'Access is denied' in msg.lower():
                # ★R318 C：先数"连续第几次"。达阈值（默认 ~50 秒）就按 error 点名
                # 一次、之后每 60 秒至多一次——历轮只有下面那条 debug，默认日志
                # 级别下整条链停摆是零痕迹的。判据本身是纯函数（`note_cursor_denied`），
                # 由 测试_ui/test_r318_cancel_power_log_once.py H 组真调钉住。
                self._cursor_denied, _named = note_cursor_denied(
                    getattr(self, '_cursor_denied', 0), now,
                    getattr(self, '_cursor_denied_named_at', 0.0))
                if _named:
                    self._cursor_denied_named_at = now
                    logger.error(
                        f"dock 连续 {self._cursor_denied} 次读不到光标位置"
                        f"（系统多半仍在锁屏/UAC 安全桌面）⇒ 横条的自动浮现/"
                        f"自动收起这一路是停的，解锁后本应自行恢复；若已解锁"
                        f"仍不恢复请把这段日志发来（{e}）")
                # ★R270-R7：这一条在锁屏/UAC 期间每 poll_ms(125ms) 命中一次——
                # win11 真机 3 小时写了 23,850 行 app.log（用户日志包实测）。
                # 降频到 60 秒一条，并把这段时间累计的条数带出来，既留着
                # "是不是安全桌面"的线索，又不把日志刷满、不把写盘耗在热路径上。
                self._poll_denied_n = getattr(self, '_poll_denied_n', 0) + 1
                if now - getattr(self, '_last_poll_denied_log', 0) > 60:
                    self._last_poll_denied_log = now
                    _n = self._poll_denied_n
                    self._poll_denied_n = 0
                    logger.debug(
                        f"dock poll denied (安全桌面?) x{_n}: {e}")
            elif now - getattr(self, '_last_poll_err_log', 0) > 60:
                self._last_poll_err_log = now
                logger.error(f"dock poll error: {e}")
        self._poll_after_id = self._after(self.poll_ms, self._poll)

    # -------------------------------------------------------------------------
    # ★ B18/R17 桌面图标打开采集；★ R256 去钩子化：只读轮询判点击
    #   （GetAsyncKeyState+GetCursorPos，与悬浮条自家点击检测同源，
    #    不挂任何全局钩子 ⇒ 进程被树杀不会让系统鼠标卡 1-2 秒）
    # -------------------------------------------------------------------------

    _MOUSE_POLL_SEC = 0.03   # 轮询步长（测试用实例属性 _mouse_poll_sec 覆盖）
    # ★R270-R4（用户 24 批口径："要在保障软件流畅的情况下，才保障统计的准确性"）：
    #   没人碰鼠标时把步长放大 4 倍（33 次/秒醒 → 8 次/秒），一出现按键边沿
    #   就回到快节奏并维持 1.5 秒——用户真在点的采样密度一点没降，空闲时省掉
    #   3/4 的线程唤醒。倍数/窗口写死在表达式里（本函数体被 ★R256 的
    #   test_r256 W 段 exec 出来单测，多一个模块级或 self 属性就会 NameError/
    #   AttributeError 假红）。

    def start_icon_capture(self):
        """启动鼠标轮询线程 + 事件工作线程（★ R256：无钩子、无外部库依赖）。

        ★ R30：返回 True=采集就绪 / False=线程起不动（异常），供统计开关
        按钮反馈真实状态。去钩子化后不再有"缺库降级"分支。"""
        try:
            # ★ R130 幂等：线程还活着就直接报就绪，不叠第二个
            t = getattr(self, '_mouse_poll_thread', None)
            if t is not None and t.is_alive():
                return True
            # ★ R130：先确保旧工作线程真正收工——快速 关→开 时旧线程可能
            #   还睡在队列 get 上，停机标志被重置后会"复活"成双线程消费
            #   同一队列，点击统计错乱
            self._join_mouse_worker()
            self._mouse_poll_stop = threading.Event()
            self._mouse_poll_thread = threading.Thread(
                target=self._mouse_poll_worker, args=(self._mouse_poll_stop,),
                daemon=True, name='MouseStatPoll')
            self._mouse_poll_thread.start()
            # ★ B26：事件处理工作线程（轮询只入队，重活在这里做）
            self._mouse_worker_stop = threading.Event()
            self._mouse_worker = threading.Thread(
                target=self._mouse_event_worker, daemon=True,
                name='MouseStatWorker')
            self._mouse_worker.start()
            logger.info('[统计采集] 鼠标轮询已启动（只读查询，无钩子）')
            return True
        except Exception as e:
            logger.error(f'启动统计采集线程失败: {e}')
            return False

    def stop_icon_capture(self):
        # ★ R256：先收轮询线程（置停→join→清引用），再走 R130 收工作线程
        pe = getattr(self, '_mouse_poll_stop', None)
        if isinstance(pe, threading.Event):
            pe.set()
        t = getattr(self, '_mouse_poll_thread', None)
        if t is not None and t.is_alive():
            t.join(timeout=1.0)
        self._mouse_poll_thread = None
        self._mouse_poll_stop = None
        # ★ R130：置停事件并等旧线程退出（线程每 0.5s 醒一次，最多等 1s）
        if isinstance(getattr(self, '_mouse_worker_stop', None),
                      threading.Event):
            self._mouse_worker_stop.set()
        self._join_mouse_worker()

    def _join_mouse_worker(self):
        """等旧统计工作线程退出（最多 1s），引用置空防僵尸复用"""
        w = getattr(self, '_mouse_worker', None)
        if w is not None and w.is_alive():
            if isinstance(getattr(self, '_mouse_worker_stop', None),
                          threading.Event):
                self._mouse_worker_stop.set()
            w.join(timeout=1.0)
        self._mouse_worker = None

    def _mouse_event_worker(self):
        """★ B26：事件处理工作线程——从队列取点击事件，做桌面判定/图标命中/
        双击配对（含跨进程调用，慢也不影响钩子回调）。"""
        # ★ R130：入口即锁定本线程的停机事件——重启后实例属性会换成新
        #   Event，旧线程若持有属性引用会"复活"；锁本地引用则必然退出
        stop_evt = self._mouse_worker_stop
        while not stop_evt.is_set():
            try:
                x, y, btn, pressed = self._mouse_events.get(timeout=0.5)
            except Exception:
                continue
            try:
                self._process_mouse_event(x, y, btn, pressed)
            except Exception as e:
                logger.debug(f'统计处理异常(已忽略): {e}')

    def _mouse_poll_worker(self, stop_evt):
        """★ R256：鼠标点击的**只读轮询**采集——每 ~30ms 查一次左右键
        （GetAsyncKeyState 高位=当下按下，低位=自上次查询以来按过），
        配合 GetCursorPos 出坐标，投进与旧钩子路**同一条队列**，下游
        状态机（_process_mouse_event 的按下/抬起/双击配对/拖动判定）一字不改。

        两个 API 都是纯读查询：不挂全局钩子（进程被树杀不会有摘钩延迟，
        就是用户报的"结束任务鼠标卡一下"的根因），也不注入任何输入。
        已知边界（P3 挂账）：整段按下+抬起恰好完全落在两轮之间的快点，
        靠低位合成一对同坐标按下/抬起兜住；若个别点击连低位都没采到
        （被自家悬浮条轮询先读走），统计会漏一击——只影响计数，不影响功能。"""
        gaks = win32api.GetAsyncKeyState
        gcp = win32api.GetCursorPos
        interval = getattr(self, '_mouse_poll_sec', self._MOUSE_POLL_SEC)
        idle_interval = interval * 4          # ★R270-R4 空闲步长
        active_until = 0.0                    # 最后一次按键边沿的"保持快采样"截止
        names = {0x01: 'Button.left', 0x02: 'Button.right'}
        down = {0x01: False, 0x02: False}
        while not stop_evt.is_set():
            step = interval if time.monotonic() < active_until else idle_interval
            try:
                for vk in (0x01, 0x02):
                    # pywin32 返回有符号 short（0x8000 时是负数），先归一成
                    # 无符号 16 位再判位
                    st = gaks(vk) & 0xFFFF
                    cur = bool(st & 0x8000)
                    was = down[vk]
                    if cur and not was:
                        x, y = gcp()
                        self._mouse_events.put((x, y, names[vk], True))
                        down[vk] = True
                        active_until = time.monotonic() + 1.5
                    elif was and not cur:
                        x, y = gcp()
                        self._mouse_events.put((x, y, names[vk], False))
                        down[vk] = False
                        active_until = time.monotonic() + 1.5
                    elif (not was) and (not cur) and (st & 0x0001):
                        # 超快点：按下+抬起整个落在两轮之间，只剩"按过"低位
                        # ⇒ 合成同坐标一对（位移 0，天然过拖动判定）
                        x, y = gcp()
                        self._mouse_events.put((x, y, names[vk], True))
                        self._mouse_events.put((x, y, names[vk], False))
                        active_until = time.monotonic() + 1.5
            except Exception as e:
                logger.debug(f'统计轮询异常(已忽略): {e}')
            stop_evt.wait(step)

    @staticmethod
    def _dbl_sec() -> float:
        """系统双击间隔（秒）——Windows 默认 500ms（GetDoubleClickTime）。
        ★ R25 第二次补回（外部编辑再次回退丢失）——双击配对调用点在
        _process_mouse_event，缺失即 AttributeError → 图标榜无记录。"""
        try:
            return ctypes.windll.user32.GetDoubleClickTime() / 1000.0
        except Exception:
            return 0.5

    def _process_mouse_event(self, x, y, btn, pressed):
        """事件处理（工作线程）：点击状态机（参考 PeekDesktop MouseClickTracker）。

        口径（R22/R25 用户拍板）：
        - 左键完整点击（按下→释放且位移 ≤ SM_CXDRAG）命中图标 → 记为一次点击
        - 两次这样的点击命中同一图标、间隔 ≤ 系统双击时间、位移 ≤ 4px → 记一次打开
        - 右键按下命中图标 → 直接记一次使用
        - 左键单击只算选中 → 不计"""
        try:
            if not self.on_desktop_icon_open or not self.stats_enabled:
                return   # ★ R30：统计关闭时不处理残留事件
            if not _mouse_on_desktop(x, y):
                if pressed:
                    _cls = ''
                    try:
                        _h = win32gui.WindowFromPoint((x, y))
                        _cls = win32gui.GetClassName(_h) if _h else ''
                    except Exception:
                        pass
                    # ★ R143 降噪：每次点击都记一行，INFO 下刷屏（本机长跑
                    #   实锤：最近 400 行近半是它）；降 DEBUG 不落盘
                    logger.debug(f'[统计采集] 非桌面点击@({x},{y}) 窗口类={_cls!r}')
                self._click_state = {}
                return
            st = self._click_state
            if btn == 'Button.right':
                if pressed:
                    name = self.icon_manager.icon_at_point(x, y)
                    if name:
                        logger.debug(f'[统计采集] 右键命中 → 记录 {name}')
                        self.on_desktop_icon_open(name)
                    st['prev'] = None
                return
            if btn != 'Button.left':
                return
            if pressed:
                st['down'] = (x, y)
                return
            # ── 左键释放：构成一次完整点击（拖动则取消）──
            down = st.pop('down', None)
            if not down:
                return
            _drag = max(win32api.GetSystemMetrics(68), win32api.GetSystemMetrics(69))
            if abs(x - down[0]) > _drag or abs(y - down[1]) > _drag:
                return   # 拖动，不算点击
            name = self.icon_manager.icon_at_point(x, y)
            if not name:
                if st.get('prev'):
                    logger.info(f'[统计采集] 第二次点击未命中图标'
                                f'（先前={st["prev"]["name"]}），配对中断')
                st['prev'] = None
                return
            now = time.time()
            prev = st.get('prev')
            dbl = self._dbl_sec()
            if (prev and prev['name'] == name
                    and now - prev['tick'] <= dbl
                    and abs(x - prev['x']) <= 4 and abs(y - prev['y']) <= 4):
                st['prev'] = None
                logger.debug(f'[统计采集] 双击确认 → 记录 {name}')
                self.on_desktop_icon_open(name)
            else:
                st['prev'] = {'name': name, 'tick': now, 'x': x, 'y': y}
                logger.debug(f'[统计采集] 首击命中 {name}（等待双击窗口）')
        except Exception as e:
            logger.error(f'桌面图标点击采集异常: {e}', exc_info=True)

    def start(self):
        """启动轮询（gui 初始化完成后调用）"""
        self._start_poll()

    # -------------------------------------------------------------------------
    # 槽位激活：切换分类
    # -------------------------------------------------------------------------

    def _get_icon_map(self) -> Dict[str, int]:
        """图标名→索引映射（带缓存：数量或句柄不变时直接复用，二次切换零读取）

        注意：必须用可见图标映射，因为 Progman ListView 不响应文件隐藏属性，
        _get_current_icon_map 会返回已隐藏图标的索引，导致可见性判断出错。"""
        try:
            hwnd = self.icon_manager.hwnd_listview
            count = self.icon_manager.get_icon_count()
        except Exception:
            return {}
        key = (hwnd, count, self.icon_manager._visible_icon_cache_key())
        if self._icon_map_cache is not None and self._icon_map_key == key:
            return self._icon_map_cache
        try:
            self._icon_map_cache = self.icon_manager._get_visible_icon_map()
            self._icon_map_key = key
        except Exception as e:
            logger.error(f"读取图标索引失败: {e}")
            return {}
        return self._icon_map_cache

    def invalidate_cache(self):
        """图标数量变化后调用（gui 增删桌面文件时可触发）"""
        self._icon_map_cache = None
        self._icon_map_key = None
        self._layout_cache.clear()

    def _auto_backup(self):
        """首次切换分类前，自动备份当前全部图标位置（仅一次）

        只在还没有任何自动备份时保存：跨会话也保留"最初的干净布局"，
        避免上次隐藏图标后退出、这次把隐藏状态当初始布局存下来。
        """
        if self._auto_saved:
            return
        backup_dir = getattr(self.icon_manager, 'backup_dir', '')
        if glob.glob(os.path.join(backup_dir, '_自动备份_*.json')):
            self._auto_saved = True  # 已有历史备份，不覆盖
            return
        try:
            r = self.icon_manager.save_layout("_自动备份", source="auto")
            if r.get("success"):
                self._auto_saved = True
                self._log("💾 已自动备份当前桌面布局（可用\"恢复布局\"找回）")
        except Exception as e:
            logger.error(f"自动备份失败: {e}")

    def _hl_names_with_sys(self, names):
        """★ B15：'框住系统图标'开启时，把系统虚拟图标名追加进高亮名单。
        关闭（默认）时原样返回——系统图标不框（用户定案 2026-09-12）。"""
        if getattr(self.highlighter, 'frame_sys', False):
            names = list(names) + list(VIRTUAL_DESKTOP_ICONS.keys())
            try:
                names.append(self.icon_manager.get_user_folder_icon_name())
            except Exception:
                pass
        return names

    def _activate_slot(self, index: int):
        """防抖入口：快速连点时合并为一次切换，避免竞态和资源浪费。"""
        if not (0 <= index < len(self.slots)):
            return
        slot = self.slots[index]

        # 取消上一次待执行的切换
        was_pending = self._debounce_timer is not None
        self._after_cancel(self._debounce_timer)
        self._debounce_timer = None
        # 以当前点击为准重新计时（用 _after 统一走降级路径：tk_root=None 时用 Timer）
        self._debounce_timer = self._after(
            self._debounce_delay, lambda i=index: self._do_activate_slot(i))
        if was_pending:
            logger.info(f"⏱ 防抖合并: [{slot.name}] 丢弃了上一次未完成的切换")

    def _do_activate_slot(self, index: int):
        """防抖到期后执行的实际切换逻辑（★R190 兜底：异常也必须恢复
        列表显示——同 _show_all_icons）。★R204 NEW-C1：impl 的"空分类
        隐藏"是功能意图不是异常态，置 _keep_lv_hidden 后兜底放行。"""
        self._keep_lv_hidden = False
        try:
            self._do_activate_slot_impl(index)
        finally:
            if getattr(self, '_keep_lv_hidden', False):
                logger.info("[兜底] 空分类按设计隐藏，跳过强制显示")
            else:
                try:
                    self._show_listview()
                except Exception as e:
                    logger.error(f"显示 ListView 失败(兜底): {e}")

    def _do_activate_slot_impl(self, index: int):
        """防抖到期后执行的实际切换逻辑"""
        self._debounce_timer = None
        if not (0 <= index < len(self.slots)):
            return

        # ★ P1-3 附加防护：工具槽（全部/窗/一键目录/✕）不是分类，不能走
        #   分类切换逻辑——它们的动作由 _handle_click 独立处理。此前误调
        #   （如启动恢复 '全部'）会让 get_category_icons 返回空集，触发下方
        #   "空分类"分支 _hide_listview()，表现为桌面图标全部消失。
        #   这是双保险：主修复已让启动恢复走 _handle_click，此处防止将来
        #   别处再误调。
        if self.slots[index].name in self.UTILITY_SLOTS:
            logger.warning(
                f"⚠ 工具槽 [{self.slots[index].name}] 被误调 _do_activate_slot，"
                f"已拒绝（应走 _handle_click）")
            return

        # ★ 防止重入：如果已经在处理，直接返回（不执行新请求）
        if self._processing:
            logger.warning(f"⚠ 切换执行中，忽略新点击 [{self.slots[index].name}]")
            return

        self._processing = True
        try:
            self._active_slot = index
            slot = self.slots[index]
            self._active_slot_name = slot.name
            try:
                if self.stats_enabled:
                    self.click_tracker.record_click(slot.name)
            except Exception:
                pass

            logger.info(f"=== 开始切换分类 [{slot.name}] (#{index}) ===")
            self._repaint_if_visible()
            logger.info(f"槽位激活: [{slot.name}] kids_mode={self.kids_mode}")

            cat_icons = self.category_manager.get_category_icons(slot.name)
            im = self.icon_manager
            # ★ 样式位诊断：自动排列(LVS_AUTOARRANGE=0x100)若被系统/用户
            # 重新打开，会造成整屏重排——日志里第一时间能看到
            try:
                _lv0 = im.hwnd_listview
                if _lv0 and win32gui.IsWindow(_lv0):
                    _style = win32gui.GetWindowLong(_lv0, win32con.GWL_STYLE)
                    logger.debug(f"[诊断] ListView 样式={_style:#x} "
                                f"自动排列={'开 ⚠' if _style & 0x100 else '关'} "
                                f"NOSCROLL={'有' if _style & 0x2000 else '无'}")
            except Exception:
                pass
            name_to_idx = self._get_icon_map()
            if not name_to_idx:
                self._log("⚠ 未读取到桌面图标")
                return
            logger.info(f"当前桌面可见图标数: {len(name_to_idx)}, 分类 [{slot.name}] 应有 {len(cat_icons)} 个")

            # ── 属性法切换（2026-09-02 起，替代"移出屏幕"方案）──
            # 屏幕外停泊已被探针判死：本机桌面 ListView 带 LVS_NOSCROLL(0x2000)，
            # 虚拟桌面外坐标在解冻重绘瞬间被钳回网格（_probe_layout*.py 实证）。
            # 新流程：整体隐藏 ListView（瞬时清屏）→ 批量改文件属性（图标位置
            # 全程不动）→ F5 冲刷（条目内容实测 ~0.05s 更新）→ 轮询数量到位
            # → 重新显示 + 高亮。图标不动则框读实时坐标永远准。

            # 第一步：计算要显示的图标列表
            visible = set(cat_icons)
            sticky_kept = 0
            sticky = set()
            # 常驻叠加规则（2026-09-03 定案）：
            # - 家长模式 / "只显示自己"的分类（默认小朋友）→ 不叠加任何常驻
            # - sticky 分类被点击时只显示自己（互不叠加）
            # - 普通分类显示自身 + 所有常驻分类的图标
            try:
                _mode = self.category_manager.get_display_mode(slot.name)
                if self.kids_mode or _mode in ('exclusive', 'exclusive_sys'):
                    sticky = set()          # 只显示自己：不叠加任何常驻
                elif _mode == 'resident':
                    sticky = self.category_manager.get_sticky_icon_names(slot.name) - set(cat_icons)
                else:
                    sticky = self.category_manager.get_sticky_icon_names() - set(cat_icons)
                visible |= sticky
                sticky_kept = len(sticky & set(name_to_idx))
            except Exception:
                sticky = set()
            logger.info(f"分类 [{slot.name}] 可见性: {len(visible)} 个图标, 常驻保留 {sticky_kept} 个")

            if not visible:
                # 空分类且无常驻可显示：隐藏整个 ListView，清空 overlay
                # （2026-09-05 修复：原逻辑只看分类自身图标，空分类会连
                #   常驻 AI/公共 和系统图标一起藏掉——点股票桌面全空）
                self._hide_listview()
                if self.highlighter:
                    self.highlighter.clear()
                self._keep_lv_hidden = True   # ★R204 NEW-C1：告知兜底这是功能意图
                self._log(f"[{slot.name}] 空分类，已隐藏全部图标")
                self.hide_dock()
                return

            # ★ 超容量提醒：分类图标多于桌面网格时，多出的会排在屏幕外看不到
            cap = self._desktop_capacity()
            if len(visible) > cap:
                warn = (f'⚠ 桌面网格约可摆放 {cap} 个图标（含副屏），'
                        f'分类 [{slot.name}] 需要 {len(visible)} 个——'
                        '多出的会排在屏幕外看不到')
                self._log(warn)
                logger.warning(warn)

            count_before = im.get_icon_count()

            # 第二步：整体隐藏 ListView——属性刷新期间不显示过期画面
            self._hide_listview()

            # 第三步：按当前「收纳档」落地（★R305）——
            #   hide 档 = 批量改文件属性（HIDDEN|SYSTEM，图标一枚不动）；
            #   box  档 = 这次不该显示的**真的搬进**「收纳·<分类>」盒子，该看的若还在
            #             盒里就按搬走时记的原坐标散回。两条档都以属性法收尾，
            #             所以 hidden/shown/failed/save_ok 这几个键两种档都读得到。
            t0 = time.time()
            # ★R321 H 组：`log=self._log` 把"留在盒里没动"那几枚接到界面日志上——
            #   这一条链的入口除了点悬浮条，还有启动「退出恢复=当前」自己走回来的
            #   (`gui/main_window.py:677 → _reactivate_last_slot`)，没人点的时候更要出声。
            result = visibility_router.apply_group_visibility(
                im, visible, group=slot.name, source='dock',
                apply_layout=getattr(im, 'apply_positions_settled', None),
                positions_provider=getattr(im, '_read_all_positions_live', None),
                log=self._log)
            boxed_out = len(result.get('hidden_now') or [])
            back_in = len(result.get('shown_now') or [])
            back_names = {str(x.get('name')) for x in (result.get('shown_now') or [])}
            failed = result.get('failed') or []
            # ★R310（#366 N-1b）：`failed` 里混着两种东西——虚拟系统图标（本来就不归
            #   收纳管）和**桌面上没有本体**的残影（既搬不进盒子也藏不了）。历轮一律报成
            #   "N 个系统图标跳过"，用户既不知道是哪一枚、也不知道该干什么。
            #   分解由路由器给（★R311：跳过数直接数虚拟名，调用方不做减法；
            #   hide 档没这两个键时按旧算法回退——★R303/★R304 逐字节转发那条红线不动）。
            # ★R314：这一对**先算再打印**，日志那句、界面那句、发给 gui 的回执共用一把尺子。
            _stuck = list(result.get('stuck') or [])
            _skip = result.get('skipped_system')
            _virt_skip = (max(0, len(failed) - len(_stuck))
                          if _skip is None else int(_skip))
            logger.info(visibility_log_line(
                result.get('mode', 'hide'), result.get('hidden', 0),
                result.get('shown', 0), boxed_out, back_in, _stuck, _virt_skip))
            # 这一组没能走盒子（撞名/账本读不动）⇒ 必须点名，别让用户以为在盒子里
            if result.get('reason'):
                self._log('⚠ ' + str(result['reason']))
            # ★R204 NEW-C7：写盘失败必须让用户看见（此前 save_ok 零消费者）
            if result.get('save_ok') is False:
                self._log('⚠ 隐藏状态写盘失败，本次切换未记账——下次切换会重试')
                logger.error('[属性法] hidden_state 写盘失败（save_ok=False）')

            # 第三步半：虚拟系统图标（此电脑/回收站/网络/控制面板）——
            # 文件属性对它们无效，走注册表 HideDesktopIcons（Dasktop MIT 范式）
            # 2026-09-02 实测：F5 冲刷即可让注册表变更 0.05s 生效，这里不再发
            # ASSOCCHANGED 重型刷新（它是"全部→分类"屏幕空白偏长的元凶）
            # ★ "系统"槽开关：关=四个图标在任何分类都隐藏（覆盖分类归属）
            virt_hidden = virt_shown = 0
            try:
                # 系统图标显隐（三列网格"系统"行）：常驻勾选=各分类都显示；
                # "只显示自己+隐系统"的分类点开时强制隐藏
                # ★R297-A：建排行组时那枚「不包含系统图标」同样要落到桌面上——
                #   历轮它只剔组的名单，桌面显隐从不看它，所以用户勾了没反应。
                #   两个判据是"或"：任一成立就隐，都不成立才常驻。
                _mode = self.category_manager.get_display_mode(slot.name)
                try:
                    _no_sys = bool(
                        self.category_manager.get_exclude_system(slot.name))
                except Exception as e:
                    # 这格读不动只按"未勾"处理，别让分类表被占用把 exclusive_sys
                    # 那条历轮判据一起吃掉（★R230b：吞异常必须留痕）
                    _no_sys = False
                    logger.error(f"[属性法] exclude_system 这格读不动"
                                 f"（按未勾处理，显隐走旧判据）: {e}")
                if (self.system_resident and _mode != 'exclusive_sys'
                        and not _no_sys):
                    virt_shown = im.show_virtual_icons(set(VIRTUAL_DESKTOP_ICONS.keys()))
                else:
                    virt_hidden = im.hide_virtual_icons(set(VIRTUAL_DESKTOP_ICONS.keys()))
            except Exception as e:
                logger.error(f"[属性法] 虚拟图标处理失败（不影响主流程）: {e}")

            # 第四步：F5 冲刷 + 轮询条目数到位
            expected = (count_before - result.get('hidden', 0) + result.get('shown', 0)
                        - virt_hidden + virt_shown)
            if boxed_out or back_in:
                # box 档：真搬走的那些**从不在属性法的 hidden 计数里**（文件已经不在
                # 桌面上了），散回来的同理不在 shown 里。少算这两笔，期望值就会虚高
                # ⇒ 每轮等满 2 秒超时（★R204 NEW-C6 那个坑的盒子版）。
                expected = expected - boxed_out + back_in
            self._force_listview_refresh()
            settled = self._wait_listview_count(expected, timeout=2.0)
            logger.info(f"[属性法] 条目数{'已到位' if settled else ' ⚠ 超时(照常显示)'}: "
                        f"期望 {expected}，实际 {im.get_icon_count()}，"
                        f"总耗时 {time.time() - t0:.2f}s")

            # 第四步半：位置校正——Explorer 偶发把重现图标丢到别的空网格位，
            # 与记忆位置不符的搬回原位（best-effort，失败不影响主流程）
            # ★R305：从盒子散回的那几枚已经按盒子记的原坐标摆过一遍了
            # （`apply_positions_settled`），别再拿分类缓存位置覆盖一次，否则"回原位"
            # 这件事有两个主人，后写的赢。
            shown_names = [n for n in visible
                           if n not in name_to_idx and n not in back_names]
            if shown_names:
                try:
                    time.sleep(0.2)
                    im.realign_icons(shown_names)
                except Exception as e:
                    logger.error(f"[校正] 失败（不影响主流程）: {e}")

            # 第四步六：套用布局——有同名布局用同名，没有则回退"全部"布局
            # （2026-09-02 修复：重写切换流程时丢失了此调用，导致切换后图标
            #   停留在杂乱位置；用户要求无布局分类默认按"全部"排列显示）
            if self.apply_cat_layout:
                try:
                    self._apply_category_layout(slot.name)
                except Exception as e:
                    logger.error(f"套用布局失败（不影响主流程）: {e}")

            # 第五步：显示 ListView + 高亮当前分类图标
            self._show_listview()
            if self.highlighter:
                # ★ R137：名单+重采+重绘+8s 漂移窗收口到统一出口
                # refresh_frames_after_switch（悬浮条点击/热键切换共用本函数，
                # 语义与规则页重算、[全部] 视图一致）
                self.refresh_frames_after_switch(slot.name)
                logger.debug(f"[诊断] 已高亮（sys_only={bool(getattr(self.highlighter, 'sys_only', False))}）")

            msg = (f"✓ 已切换到 [{slot.name}]：显示 {len(visible)} 个图标")
            if sticky_kept:
                msg += f"，常驻保留 {sticky_kept} 个"
            if boxed_out:
                msg += f"，{boxed_out} 个收进盒子"
            if back_in:
                msg += f"，{back_in} 个从盒子放回"
            if result.get('hidden'):
                msg += f"，隐藏 {result['hidden']} 个"
            # ★R310（#366 N-1b）/★R311/★R314 那对口径在上面已算好：
            #   虚拟的照旧只报数，残影逐名点名。
            if _virt_skip:
                msg += f"，{_virt_skip} 个系统图标跳过"
            if _stuck:
                msg += ('，%d 个没能收走（桌面上没有它的文件）：%s'
                        % (len(_stuck), '、'.join(str(n) for n in _stuck[:5])))
            # 搬不进盒子的那几枚（同名撞车/没权限）同样点名；`(整组)` 是"这一组整体
            # 没走盒子"的降级说明，上面 `result['reason']` 那条已经说过，不重复报。
            _fb = [x for x in (result.get('attr_fallback') or [])
                   if str(x.get('name')) != '(整组)']
            if _fb:
                msg += ('，%d 个没能收进盒子：%s'
                        % (len(_fb), '；'.join('%s（%s）' % (x.get('name'),
                                                            x.get('reason'))
                                               for x in _fb[:5])))
            msg += f"（{time.time() - t0:.1f}s）"
            logger.info(f"=== 切换完成 [{slot.name}]: {msg} ===")
            self._log(msg)

            # 统一在这里通知一次（触发 highlighter 重绘 + GUI 同步）
            self._fire_visibility_changed()
            # ★ 分类联动文件夹组：若存在与分类同名的组且开关开启，gui 侧会打开
            if self.on_switch_link_folders:
                try:
                    self.on_switch_link_folders(slot.name)
                except Exception as e:
                    logger.error(f"分类联动文件夹组失败: {e}")
            # 通知 gui 写切换日志
            if self.on_switch_done:
                try:
                    # 2026-09-03 修复：传真实 result（此前传空字典，
                    # switch.log 里"显示=0 隐藏=0"永远是假数据）
                    self.on_switch_done(
                        slot.name,
                        {'hidden': result.get('hidden', 0),
                         'shown': result.get('shown', 0),
                         'failed': result.get('failed', []),
                         # ★R305：box 档那两笔真搬移也进日志，否则 switch.log 里
                        #  「显示=0 隐藏=0」对走了盒子的切换又是一份假数据
                         'mode': result.get('mode', 'hide'),
                         'boxed': boxed_out, 'back': back_in,
                         # ★R314：把已经分解好的两份一起交给 gui——switch.log 那边
                         # 此前只拿到 `failed`（刚进盒的+系统跳过全在里头），照着打成
                         # 「失败=62」，一次成功的切换在日志里像丢了 62 枚图标
                         'stuck': _stuck, 'skipped_system': _virt_skip},
                        True)
                except Exception:
                    pass
        finally:
            logger.info(f"=== _processing 重置为 False [{slot.name}] ===")
            self._processing = False
        self.hide_dock()

    def _cat_is_rank_group(self, cat_name: str) -> bool:
        """★R279-⑦：这个槽位名对应的分类是不是「排行组」（判据只有一个地方写，
        见 core/category_manager.py::is_rank_group）。

        读不到分类管理器/表里有脏条目 ⇒ 一律按"不是"处理：宁可像旧版那样套布局，
        也别把一个正常的分类判成"不动位置"。"""
        try:
            for c in (self.category_manager.get_all_categories() or []):
                if isinstance(c, dict) and c.get('name') == cat_name:
                    return CategoryManager.is_rank_group(c)
        except Exception as e:
            logger.debug(f'[dock] 判排行组失败 [{cat_name}]: {e}')
        return False

    def _apply_category_layout(self, cat_name: str, sync_only: bool = False,
                               force: bool = False) -> bool:
        """存在与分类同名的布局文件时，切换分类自动套用其图标位置。
        同名布局不存在时，回退到"全部"布局（用户只要摆好"全部"，其他分类自然有稳定基准）。
        仅在用户开启"切分类套用同名布局"时生效（默认关：手动摆放图标时不被打扰）

        sync_only=True: 只把当前缓存位置写入备份文件，不执行 restore_layout。
        用于在可见性切换前先写缓存，避免 save_layout 重读 ListView 污染缓存。
        force=True: 无视那条偏好直接套——只给"是我们自己把桌面弄乱的"收尾路径用
        （★R297-B 系统图标一键显隐之后的补摆位），用户手动摆位时仍然不受打扰。
        """
        if not force and not getattr(self, 'apply_cat_layout', False):
            return False
        # ★R279-⑦（产品负责人 09-30 04 批点名"排行组要和保存布局协调"）：
        # 排行组**不参与布局**——既不套同名布局，也不回写。两个理由都是硬的：
        #   ① 它的名单每次建组都在变（榜单在动），给它存一份同名布局＝攒一份
        #      很快就过期的位置表，而且每次保存都写一个新时间戳文件，越用越乱；
        #   ② 更糟的是下面那条"无同名布局→回退到『全部』布局"的老路：
        #      点一下排行组的槽就把**整个桌面**重摆一遍，用户完全没要求过这件事。
        # 排行组的职责就一句：显示哪些图标。位置交给用户和「全部」布局。
        if self._cat_is_rank_group(cat_name):
            # ★R282-1（用户 10-01 01 批 R010-1 / R011-2）：排行组**自己手动存过**同名
            # 布局时，切回来要套用那份位置——旧写法一律 return False，用户实测"存了
            # 排行组布局，切走再切回还是跟随『全部』"。能取到是因为 manage_panel._on_save
            # 选中分类时正是以分类名存布局（pick_layout(cat_name) 命中同名那份）。
            # 仍然严守 ★R279-⑦ 的两条硬理由，只放开"读"、不放开"写"：
            #   · **绝不回退「全部」**——点一下排行组不该把整个桌面重摆（无同名布局照旧跳过）；
            #   · **不回写**（不进下面 _is_own 的同步块）——榜单名单在动，回写只会攒一堆
            #     很快过期的时间戳文件。位置以用户手动存的那份为准，只读套用。
            _rk_lay = None
            try:
                _rk_lay = self.icon_manager.pick_layout(cat_name)
            except Exception as e:
                logger.debug(f'[dock] 排行组取同名布局失败 [{cat_name}]: {e}')
            if not _rk_lay or _rk_lay.get('name') != cat_name:
                logger.info(f'[dock] 排行组 [{cat_name}] 无同名布局，不参与布局（临时快照，只切显示）')
                return False
            if sync_only:
                # 排行组不回写缓存（名单在动）：sync_only 这条路对它是 no-op（与旧版一致）
                return False
            try:
                im = self.icon_manager
                im.suppress_guard(10.0)
                # ★R249 一2①：摆布局前先安静清掉桌面"排序方式"，否则写完一刷新被打回
                if callable(getattr(self, 'silent_sort_fix', None)):
                    try:
                        self.silent_sort_fix()
                    except Exception as e:
                        logger.debug('[dock] 切排行组前静默清排序失败（不影响摆布局）: %s', e)
                im.restore_layout(_rk_lay['path'])
                im.arm_guard(_rk_lay['path'], _rk_lay.get('name', ''))
                logger.info(f'[dock] 排行组 [{cat_name}] 套用其手动保存的同名布局 → 守护基准={cat_name}')
                return True
            except Exception as e:
                logger.error(f'[dock] 排行组套用同名布局失败 [{cat_name}]: {e}')
                return False
        try:
            # 2026-09-03 修复：取最新一份（按布局内 timestamp），旧代码取最旧导致
            # 点"全部"套用的是几天前的摆放
            # 2026-09-05 优选：同名布局优先用户手动保存的（pick_layout），
            # 自动备份只在没有手动备份时兜底
            target_lay = self.icon_manager.pick_layout(cat_name)
            if target_lay is None:
                # 无同名布局 → 回退到"全部"（同样优选手动）
                target_lay = self.icon_manager.pick_layout('全部')
            if target_lay:
                im = self.icon_manager
                import json as _json, os as _os
                _is_own = target_lay.get('name') == cat_name
                # 回退到"全部"布局时严禁同步——2026-09-03 事故根因：
                # 回退同步把切换期间的临时位置写进"全部"布局，越用越乱
                if not sync_only:
                    im.suppress_guard(10.0)
                    # ★R249 一2①（用户 26 批 02：「①按建议来 ②不啃声」）：摆布局
                    # **之前**先把桌面上压着的"排序方式"安静清掉。为什么必须在之前：
                    # 活动排序还在时系统认为自己负责摆图标，我们写完的位置一刷新就被
                    # 打回（实测：写完差 0、刷新后又差 20）。这个回调由 gui 注入，
                    # 走免鼠标接口——**一次鼠标都不发、不弹框**，清不掉就照旧摆布局；
                    # 没注入（None）时这段整个跳过，行为与旧版逐字一致。
                    if callable(getattr(self, 'silent_sort_fix', None)):
                        try:
                            self.silent_sort_fix()
                        except Exception as e:
                            logger.debug(
                                '[dock] 切分类前静默清排序失败（不影响摆布局）: %s', e)
                    # ★ R100：一次到位——restore_layout 内置读回验证+偏差补发
                    # （R86 [verify]/[verify2]），旧的"sleep 0.5 后二次校正"
                    # 第二遍恢复已冗余，还让每次切分类多闪一遍（用户反馈
                    # "分类框多次刷新桌面"，2026-09-16 日志实锤 0.5s 内跑两遍）
                    im.restore_layout(target_lay['path'])
                    im.arm_guard(target_lay['path'], target_lay.get('name', ''))
                if _is_own:
                    # ★ 同步挪到恢复之后（2026-09-05 修复：此前在恢复前同步，
                    #   把图标被 Explorer 临时塞的位置写进布局，坏位置自我延续；
                    #   恢复后缓存=布局位置，同步变为无差写，只有用户手动挪动
                    #   过的图标才会真正更新布局）
                    try:
                        _cat_names = set(
                            self.category_manager.get_category_icons(cat_name)
                            or [])
                        _sync_cache_into_layout(im, target_lay['path'],
                                                im._position_cache, _cat_names)
                    except Exception as e:
                        logger.error(f"同步布局备份失败 [{cat_name}]: {e}")
                logger.info(f"应用分类布局 [{cat_name}]{' (仅同步缓存)' if sync_only else ''}"
                            f" → 守护基准={target_lay.get('name', '')}")
                return True
        except Exception as e:
            logger.error(f"应用分类布局失败 [{cat_name}]: {e}")
        return False

    def relayout_after_sysicon_change(self) -> bool:
        """★R297-B（用户 2026-10-05 08:4x win11 真机）：一键显示/隐藏系统图标的收尾摆位。

        那个开关要发一次 F5 冲刷才让注册表显隐生效，而 F5 会让 Explorer 按它自己
        那份"排序方式"整屏重排一遍（★R234：坐标层打不赢视图层）——用户看到的就是
        "点一下桌面变乱，再点一次悬浮栏的分类才好"。历轮只有**切分类**那条路在冲刷
        后接了"清排序+摆回布局+武装守护"，开关这条路一条都没接。这里就是把既有的
        _apply_category_layout 那条链补接上，不另写第二份摆位算法。

        F5 是异步的：冲刷那一刻桌面条目数还在动（五枚注册表图标正长出来/缩回去），
        摆早了是摆给过渡画面看 ⇒ 先等读数连持稳三次（上限 ~1.2s）再动手。
        返回"是否真的摆了"，没得摆（无视图名/无布局基准/排行组没存过布局）都算 False。
        """
        view = getattr(self, '_active_slot_name', None)
        if not view:
            logger.info('[系统图标] 收尾摆位跳过：没有当前视图名（刚启动/工具槽没记名）')
            return False
        im = self.icon_manager
        self._hide_listview()
        try:
            deadline = time.time() + 1.2
            last = None
            stable = 0
            while time.time() < deadline:
                try:
                    now = im.get_icon_count()
                except Exception as e:
                    logger.debug('[系统图标] 等待期间读条目数失败（直接摆）: %s', e)
                    break
                if now == last:
                    stable += 1
                    if stable >= 2:          # 首读 + 连续两次相同 = 连持稳三次
                        break
                else:
                    stable = 0
                    last = now
                time.sleep(0.1)
            # force=True：桌面是**我们**用 F5 弄乱的。那条偏好管的是"用户手动摆位时
            # 别打扰"，不是"程序弄乱了别收拾"，所以这条路绕过它、但仍只摆一次。
            ok = bool(self._apply_category_layout(view, force=True))
            if not ok:
                logger.info(f'[系统图标] 收尾摆位未执行 [{view}]'
                            f'（没有可用布局基准，或当前视图是没存过布局的排行组）')
            return ok
        except Exception as e:
            logger.error(f'[系统图标] 收尾摆位异常（显隐本身已生效）: {e}')
            return False
        finally:
            # 摆位成不成都得把桌面显示回来——留在隐藏态＝图标全空，那是 P0
            self._show_listview()

    def _compute_frame_names(self, active_slot):
        """★ R66 统一框选语义（用户 2026-09-15 定案，所有视图一致）：

        - 只框系统图标(sys_only)：只框系统，最高优先，其余一概不框
        - 框住系统图标(frame_sys)：在当前基础名单上"追加"系统图标
        - 框住分类和常驻(frame_all)：基础名单=分类图标+常驻图标
        - 只框分类(cat_only)：基础名单=仅分类图标（不含常驻，与上者联动互斥）
        - 四态全关 = 一个框都不画（默认动作）
        active_slot: 悬浮窗当前槽位名；'全部'/空 视为全部视图。
        档案：项目根 框选语义_定案_20260915.md"""
        hl = self.highlighter
        im = self.icon_manager
        cm = self.category_manager
        sys_names = set(VIRTUAL_DESKTOP_ICONS.keys())
        try:
            sys_names.add(im.get_user_folder_icon_name())
        except Exception:
            pass
        if getattr(hl, 'sys_only', False):
            return list(sys_names)
        base = set()
        if getattr(hl, 'frame_all', False) or getattr(hl, 'cat_only', False):
            if active_slot and active_slot != '全部':
                base = set(cm.get_category_icons(active_slot) or [])
                if not getattr(hl, 'cat_only', False):
                    # 常驻叠加口径与 _do_activate_slot 一致
                    try:
                        _mode = cm.get_display_mode(active_slot)
                        if self.kids_mode or _mode in ('exclusive', 'exclusive_sys'):
                            sticky = set()
                        elif _mode == 'resident':
                            sticky = cm.get_sticky_icon_names(active_slot) - base
                        else:
                            sticky = cm.get_sticky_icon_names() - base
                    except Exception:
                        sticky = set()
                    base |= sticky
            else:
                for _c in cm.get_all_categories():
                    base |= set(_c.get('icons') or [])
                if getattr(hl, 'cat_only', False):
                    base -= set(cm.get_sticky_icon_names())
                else:
                    base |= set(cm.get_sticky_icon_names())
        # ★ R67（用户实测反馈）：系统虚拟图标即使被加进过某分类，也不属于
        # "分类和常驻"的框选范围——需要框系统请勾"框住系统图标"（追加型）
        # 或"只框系统图标"（独占）。
        base -= sys_names
        if getattr(hl, 'frame_sys', False):
            base |= sys_names
        # ★R237：旧写法这里 `return None` ⇒ 调用方 len(None)/sorted(None) 直接
        # 抛异常（win11 19:33 app.log 两条 [框] ERROR 就是它）。"没有可框的图标"
        # 是一个**空名单**，不是"算不出来"，语义上就该返回 []。
        if not base:
            return []
        # 非系统名字只保留当前真实可见的（隐藏中的不框；系统名直接保留）
        try:
            vis = set(im._get_visible_icon_map().keys())
            named = {n for n in base if n in sys_names or n in vis}
            if named:
                base = named
        except Exception:
            pass
        return list(base)

    def refresh_frames_after_switch(self, slot_name=None):
        """★ R137 统一框刷新出口（悬浮条点击 / 热键切换 / [全部] / 规则页
        重算 四路共用，"方框准确圈中图标"的唯一口径）。

        名单计算（_compute_frame_names，R66 语义）→ highlight_names/clear →
        立即重采+重绘（R105：名单套旧缓存会把框画在"上次的框位置"）→
        **开 8s 重采窗口**兜 Explorer 迟到重排——快速连切时图标落位可超时
        2 秒+，此前规则页重算漏开这个窗口，框画完图标还在挪，看着就是
        "没框/框错"（用户 -08 实测）。"""
        if not self.highlighter:
            return
        shown_slot = slot_name if slot_name is not None \
            else getattr(self, '_active_slot_name', None)
        try:
            _hl = self._compute_frame_names(
                slot_name if slot_name is not None
                else getattr(self, '_active_slot_name', None))
            if _hl:
                self.highlighter.highlight_names(_hl)
            else:
                self.highlighter.clear()
            if self.highlighter.collect_now():
                self.highlighter.force_redraw()
            self.highlighter.arm_recollect(8.0)
            # ★R237：`len(_hl)` 在名单为空时是 len(None) ⇒ 整段 except 吞掉并
            # 打 ERROR（框其实已经撤了，日志却一片红）。空名单照实报 0。
            logger.info(f"[框] 统一刷新 [{shown_slot}]: 框 {len(_hl or [])} 个")
        except Exception as e:
            logger.error(f"[框] 统一刷新失败(不影响主流程): {e}")

    def recompute_highlight_only(self):
        """★ R65-Bug5：框选开关变化只重算高亮名单+重绘，不走整套显隐切换。

        此前四个框选复选框都调 _replay_active_slot → 点一下就
        隐藏/F5/搬图标/显示一整轮（2026-09-15 用户实测：桌面在两种布局
        间反复拉扯、还打扰打字——TRAE 工作区 06:59 日志 13 秒拉 4 轮）。
        框选开关只影响画框名单，不影响图标显隐，故走轻量路径。
        名单口径统一走 _compute_frame_names（R66 语义）。
        ★ R137：重采/重绘/8s 漂移窗全走统一出口 refresh_frames_after_switch
        （旧实现漏开 8s 窗口：快速连切后图标还在挪，重算的框随即过期）。"""
        if not self.highlighter:
            return
        try:
            self.highlighter.notify_changed()
        except Exception as e:
            logger.error(f"notify_changed 异常: {e}")
        self.refresh_frames_after_switch()

    def _fire_visibility_changed(self):
        """通知高亮框等：图标可见性变了（位置没变，签名比对不出来）"""
        if self.on_visibility_changed:
            try:
                self.on_visibility_changed()
            except Exception as e:
                logger.error(f"可见性变化回调异常: {e}")

    def _hide_listview(self):
        """隐藏整个 ListView（混合方案：ShowWindow 方式）"""
        try:
            im = self.icon_manager
            lv = im.hwnd_listview
            if lv and win32gui.IsWindow(lv):
                win32gui.ShowWindow(lv, win32con.SW_HIDE)
                logger.debug(f"[诊断] 已隐藏 ListView: {lv}")
        except Exception as e:
            logger.error(f"隐藏 ListView 失败: {e}")

    def _show_listview(self):
        """显示整个 ListView（混合方案：ShowWindow 方式）"""
        try:
            im = self.icon_manager
            # ★ R102 黑桌面防线：父窗口 SHELLDLL_DefView 被藏时，只 SHOW
            # 子 ListView 无效——先无条件确保父窗口可见（对已可见零副作用）
            try:
                im.ensure_desktop_view_visible()
            except Exception:
                pass
            lv = im.hwnd_listview
            if lv and win32gui.IsWindow(lv):
                win32gui.ShowWindow(lv, win32con.SW_SHOW)
                logger.debug(f"[诊断] 已显示 ListView: {lv}")
        except Exception as e:
            logger.error(f"显示 ListView 失败: {e}")

    def _desktop_capacity(self) -> int:
        """估算桌面可摆放的图标总数：虚拟屏(含副屏) ÷ 网格间距，扣任务栏。

        用于"图标多于桌面网格"的提醒（2026-09-05 用户需求）。"""
        try:
            cx, cy = self.icon_manager.get_item_spacing()
        except Exception:
            cx, cy = 95, 125
        cx = max(60, cx)
        cy = max(80, cy)
        try:
            vw = win32api.GetSystemMetrics(78)
            vh = win32api.GetSystemMetrics(79)
        except Exception:
            vw, vh = 1920, 1040
        cols = max(1, vw // cx)
        rows = max(1, (vh - 60) // cy)   # 扣任务栏高度
        return cols * rows

    def _force_listview_refresh(self):
        """F5 键击冲刷桌面 ListView 条目内容。

        2026-09-02 探针实测：改完文件属性后，逐文件 SHChangeNotify 基本不
        触发内容更新，而 F5 键击让条目列表 ~0.05s 内完成增删（位置不动）。"""
        try:
            im = self.icon_manager
            lv = im.hwnd_listview
            if lv and win32gui.IsWindow(lv):
                win32gui.PostMessage(lv, win32con.WM_KEYDOWN, 0x74, 0)  # VK_F5
                time.sleep(0.05)
                win32gui.PostMessage(lv, win32con.WM_KEYUP, 0x74, 0)
                logger.debug("[诊断] 已发送 F5 冲刷桌面")
        except Exception as e:
            logger.error(f"F5 冲刷失败: {e}")

    def _wait_listview_count(self, expected: int, timeout: float = 6.0) -> bool:
        """轮询等待 ListView 条目数到位；过半程未到位则补发 UPDATEDIR 强刷新"""
        deadline = time.time() + timeout
        nudged = False
        while time.time() < deadline:
            try:
                if self.icon_manager.get_icon_count() == expected:
                    return True
            except Exception:
                pass
            if not nudged and time.time() > deadline - timeout * 0.5:
                nudged = True
                try:
                    import ctypes
                    buf = ctypes.create_unicode_buffer(
                        os.path.join(os.path.expanduser('~'), 'Desktop') + os.sep)
                    ctypes.windll.shell32.SHChangeNotify(
                        0x1000,              # SHCNE_UPDATEDIR
                        0x5 | 0x1000,        # SHCNF_PATHW | SHCNF_FLUSH
                        ctypes.cast(buf, ctypes.c_void_p), None)
                    logger.debug("[诊断] 条目未到位，补发 UPDATEDIR 强刷新")
                except Exception:
                    pass
            time.sleep(0.05)
        return False

    def _watch_repair(self, im, bad, cmap):
        """逐个把漂移图标写回目标坐标，按**消息返回值**记账 → `(归位, 未找到, 没落位)`。

        ★R319-M5：这一段旧写法内联在 `_watch_poll` 里，`send_msg_timeout` 的返回值
        丢掉不看、发完就 `fixed += 1`——而那个返回值是"这条 LVM_SETITEMPOSITION
        写进去没有"的唯一凭据（★R234：索引失效／视图刚重建／跨进程超时 ⇒ 返回 0
        或 None）。于是"一发没写"也被计进 `归位=N`，日志宣称"N 个图标漂移已自动
        归位"、桌面却一动不动：看护看着在干活，实际只会谎报（★R234 那族
        "消息发出≠生效"的假成功）。
        上屏那一行只在**真归位过**的时候出；"没落位"只进 [watch] 那行——
        这条链每 1.5 秒跑一轮，把白发的发数刷到日志页上就成了噪音。
        """
        fixed, missed, unverified = 0, [], []
        for n, (tx, ty) in bad.items():
            idx = cmap.get(n)
            if idx is None:
                missed.append(n)
                continue
            ret = send_msg_timeout(im.hwnd_listview, LVM_SETITEMPOSITION, idx,
                                   pack_lparam(tx, ty))
            if ret:
                fixed += 1
            else:
                unverified.append(n)
        try:
            from core.icon_manager import _layout_logger
            _layout_logger.info(
                f"[watch] 漂移={len(bad)} 归位={fixed} 未找到={missed[:5]}"
                f" 没落位={len(unverified)}")
        except Exception:
            pass
        if fixed:
            self._log(f'👀 看护：{fixed} 个图标漂移已自动归位')
        return fixed, missed, unverified

    def _watch_poll(self):
        """★ R88：恢复/大整理后的定向看护轮询（仅"全部"视图生效）。

        Explorer 持久化流更新前的窗口期里，F5 会把个别图标按旧流重导。
        每 3s 读回实际位置与布局目标比对，漂移 >4px 的静默归位；
        切去其他分类即失效；10 分钟后自动结束。全程写 layout_debug.log。"""
        if not getattr(self.icon_manager, 'watch_enabled', True):
            return
        if time.time() > getattr(self, '_restore_watch_until', 0):
            try:
                from core.icon_manager import _layout_logger
                _layout_logger.info("[watch] 看护期结束，停止轮询")
            except Exception:
                pass
            return
        try:
            _as = getattr(self, '_active_slot_name', '')
            if _as != '全部':
                try:
                    from core.icon_manager import _layout_logger
                    _layout_logger.info(f"[watch] 视图={_as!r} 非全部，停止轮询")
                except Exception:
                    pass
                return   # 用户切去其他分类：看护失效，不再轮询
            # ★ R89：心跳自证存活（用户实测无 [watch] 记录，需分辨
            # "没跑"还是"跑了没漂移"）
            # ★ R91：拖动避让——左键按住时跳过本轮纠偏（用户正在拖图标）
            if _left_button_down():
                # ★R270-R5：id 必须握在手里，否则这条链谁也取消不掉
                self._watch_after_id = self._after(1500, self._watch_poll)
                return
            self._watch_hb = getattr(self, '_watch_hb', 0) + 1
            if self._watch_hb % 5 == 1:
                try:
                    from core.icon_manager import _layout_logger
                    _layout_logger.info(f"[watch] 心跳#{self._watch_hb} 存活")
                except Exception:
                    pass
            im = self.icon_manager
            ref = getattr(self, '_restore_watch_ref', {}) or {}
            if ref:
                live = im._read_all_positions_live()
                bad = {n: t for n, t in ref.items()
                       if n in live and (abs(live[n][0] - t[0]) > 4
                                         or abs(live[n][1] - t[1]) > 4)}
                # ★R234（用户 14 批 item1 + 16 批 item1）：让位判据只管"个别图标"。
                #   一轮里成片漂移 = Explorer 自己把整屏重排（刷新/新文件枚举/
                #   自动排列都会这样），不是用户在摆图标——旧写法会给每个被卷到的
                #   图标记一次 _watch_hits，刷新两轮就把全部图标判成"手动整理"，
                #   看护全线放弃（用户看到的"越刷越没人管"）。成片轮不记让位，
                #   只连续重排到第 3 轮才整体收兵（有界，绝不无限拉扯）。
                _reflow = len(bad) >= max(3, (len(ref) + 3) // 4)
                if _reflow:
                    self._watch_reflow = getattr(self, '_watch_reflow', 0) + 1
                    _round = self._watch_reflow
                    if _round >= 3:
                        ref.clear()
                        self._watch_reflow = 0
                        self._log('👀 看护收兵：桌面视图连续 3 轮整屏重排，'
                                  '位置交给系统管（不再拉扯）')
                        return
                    try:
                        from core.icon_manager import _layout_logger
                        _layout_logger.info(
                            f"[watch] 视图重排 {len(bad)} 个（第 {_round} 轮，"
                            f"不计让位）")
                    except Exception:
                        pass
                else:
                    self._watch_reflow = 0
                    # ★ R91：同图标第二次漂移=用户主动摆放，看护让位
                    for n in list(bad):
                        if getattr(self, '_watch_hits', {}).get(n, 0) >= 1:
                            ref.pop(n, None)
                            bad.pop(n, None)
                            self._log(f'👀 看护让位：[{n}] 判定为手动整理，随你摆')
                        else:
                            self._watch_hits[n] = 1
                if bad:
                    # ★R234：自动排列开着时 SETITEMPOSITION 被 Explorer 直接拒
                    # （实测 ret=0，一格不动）——先真关掉；关不掉就不写，
                    # 只提示一次，避免"每 1.5s 白写一遍"的拉扯。
                    # ★R234-S6：`is not False` 而不是直接判真值——
                    # get_auto_arrange() 返回 None 表示"读不到样式位"
                    # （控件重建/跨进程失败），按 icon_manager 自立的规矩
                    # "None 当未知"，未知就不该继续白写。
                    if im.get_auto_arrange() is not False:
                        if not im.set_auto_arrange(False):
                            if not getattr(self, '_watch_aa_warned', False):
                                self._watch_aa_warned = True
                                self._log('👀 桌面开着"自动排列图标"，'
                                          '位置由系统管，看护暂停纠偏')
                            bad = {}
                    if bad:
                        cmap = im._read_icon_map_from_listview()
                        self._watch_repair(im, bad, cmap)
        except Exception as e:
            logger.error(f"看护轮询异常: {e}")
        try:
            # ★R270-R5：看护链的唯一调度点——记下 id，arm 时才取消得掉
            self._watch_after_id = self._after(1500, self._watch_poll)
        except Exception:
            pass

    def _show_all_icons(self):
        """恢复全部图标（★R190 兜底：无论中途怎么异常，列表必须重新
        显示——R102 黑桌面事故的最后一道闸）。★R204 NEW-C12：把 impl
        的统计结果透传给调用方（此前恒 None→switch.log 恒记 0/0/0）。"""
        try:
            return self._show_all_icons_impl()
        finally:
            try:
                self._show_listview()
            except Exception as e:
                logger.error(f"显示 ListView 失败(兜底): {e}")

    def _show_all_icons_impl(self):
        """恢复全部图标：先隐藏列表 → 清属性/摆位全部在幕后完成 → 再显示"""
        im = self.icon_manager
        _quanbu = []      # ★R204 CORE-19：函数开头初始化，防 NameError
        _latest = None

        # 第〇步：隐藏整个列表（2026-09-04 修复"点全部图标先挤成一团再排列"：
        # Explorer 把隐藏图标加回时会先塞默认网格位，必须在幕后完成摆位）
        self._hide_listview()

        # 第〇步半（★R305 真机实撞，P1）：先把**本系统搬进盒子里的**散回来，再清属性。
        #   历轮这一条只会 `unhide_all()`（属性法），它看不见盒里的东西 ⇒ 盒档切过一次
        #   分类再点"全部"，桌面只剩 3 枚（2026-10-07 真机日志：搬走=27 之后
        #   「切换 [全部] … 散回=0」）。放在 count_before 之前是为了让下面的期望值
        #   自洽：带隐藏属性回来的那批由 unhide_all 计入 shown，没带的已在 count_before 里。
        _rel = 0
        # ★R323 L3（用户「switch.log 里"全部"那一发打真值」）：散回那一步的**分解**
        # 也要一路带到日志。以前只有 `total` 被读走，其余六格（留在盒里 / 搬不回 /
        # 已不在盒里 / 让路改名 / 没被认领 / 动过的盒）在 dock 里就地丢弃，
        # switch.log 那一发只能念一份"什么都没有"的旧数。
        _rel_x = {'kept': 0, 'blocked': 0, 'gone': 0, 'renamed': 0,
                  'stray': 0, 'boxes': 0}
        _lay_ok = False      # ★R323 L3：「全部」布局**真的**套上了才算，别把"试过"写成"套好"
        try:
            # 这里**不**传 apply_layout：ListView 此刻是 SW_HIDE，★R304 实测那条
            # "等视图列全再摆位"在隐藏态会等满超时还摆不上（每只盒白等 2.2 秒）；
            # 而这条链第二步本来就要套「全部」布局 + realign，位置由它统一管。
            _rr = visibility_router.release_all_boxes(im, source='all')
            _rel = int(_rr.get('total') or 0)
            _rel_x = {
                'kept': len(_rr.get('kept_in_box') or []),
                'blocked': len(_rr.get('blocked') or []),
                'gone': len(_rr.get('gone') or []),
                'renamed': len(_rr.get('renamed') or []),
                # ★R325 F3（报告 P12）：`stray` 是 `{盒: [名目]}`，旧写法 `len(...)`
                # 数的是**几只盒**，而 app.log 同一趟念的是 `sum(len(v))`＝**几枚**
                # （1 只盒 7 枚 ⇒ 两份日志 1 vs 7 对不上）。switch.log 那格跟着改枚数。
                'stray': sum(len(v) for v in (_rr.get('stray') or {}).values()),
                'boxes': len(_rr.get('released') or {})}
            if _rr.get('reason'):
                self._log('⚠ ' + str(_rr['reason']))
            if _rel:
                self._log(f'📦 显示全部：先从 {len(_rr.get("released") or {})} 只盒子'
                          f'搬回 {_rel} 枚，再恢复属性')
            if _rr.get('conflicts'):
                # ★R321 J3：这一条链（点悬浮条「全部」）就是 win11 R038 连开 40 发改名
                # 的现场；现在一枚都不碰桌面那一枚，但留在盒里几枚要说给人听。
                self._log(u'📦 还有 %d 枚留在盒里没动（桌面上已经有同名的）'
                          u'——要搬请在资源页的盒子面板点对那只盒'
                          % len(_rr['conflicts']))
        except Exception as e:
            # 散回没做成也不能挡住"清属性"这一步，但必须留痕（★R230b：静默兜底=交付障碍）
            logger.error(f'[隐藏盒] 显示全部散回失败（属性法照走）: {e}')

        # 第一步：恢复被本程序隐藏的文件属性（图标位置全程不动）+ 虚拟图标
        count_before = im.get_icon_count()
        shown = im.unhide_all()
        self._force_listview_refresh()
        self._wait_listview_count(count_before + shown, timeout=2.0)
        total_all = count_before + shown
        cap = self._desktop_capacity()
        if total_all > cap:
            warn = (f'⚠ 桌面网格约可摆放 {cap} 个图标（含副屏），'
                    f'当前全部图标 {total_all} 个——多出的会排在屏幕外看不到')
            self._log(warn)
            logger.warning(warn)

        # 第二步：套用"全部"布局的摆放（用户要求：点全部=回到整理好的布局）
        # ★ 2026-09-03 关键：ListView 还在隐藏状态就完成摆放（移动不可见），
        #   全部到位才显示——修"点全部刚开始乱"。★ R100：二次恢复已删
        #   （restore_layout 内置 [verify]/[verify2] 读回验证+偏差补发，
        #   外层"二次校正"冗余且多闪一遍）
        try:
            _quanbu = [im.pick_layout('全部')] if im.pick_layout('全部') else []
            if _quanbu:
                # ★R204 CORE-19：timestamp 混型（null/数字）时 str() 归一
                _latest = max(_quanbu, key=lambda l: str(l.get('timestamp') or ''))
                im.suppress_guard(10.0)
                im.restore_layout(_latest['path'])
                im.arm_guard(_latest['path'], '全部')
                _lay_ok = True
                logger.info(f"[全部] 已套用全部布局并重新布防"
                            f"（{os.path.basename(_latest['path'])}）")
        except Exception as e:
            logger.error(f"套用全部布局失败（不影响主流程）: {e}")
        try:
            time.sleep(0.2)
            im.realign_icons(list(im._position_cache.keys()))
        except Exception as e:
            logger.error(f"[校正] 失败（不影响主流程）: {e}")

        # 第三步：全部到位后才显示 ListView
        try:
            self._show_listview()
        except Exception as e:
            logger.error(f"显示 ListView 失败: {e}")

        # ★ R84：显示后补一遍套位。隐藏态（SW_HIDE）下 LVM_SETITEMPOSITION
        # 不触发 ShellView 的"用户挪动"簿记——位置永远进不了 Explorer 的
        # 持久化记忆，用户之后每次 F5 都被旧流重导（2026-09-16 实测：点All
        # 好看、F5 又乱、闲置久了 Explorer 自己存盘才正常）。显示态补套，
        # 让簿记真正发生并持久化。
        try:
            if _quanbu and _latest:
                im.suppress_guard(10.0)
                # ★R237：显示 ListView 之后 Explorer 自己还要重排一轮（win11
                # 第 5 份日志：这一遍补套紧跟重排 ⇒ 9 个被就近顺延、补发后仍
                # 歪 4 个 = 用户眼前的"乱"）。先等它把位置落定再开写，别和它
                # 抢格子；等不到也只是到点返回，绝不卡界面线程。
                try:
                    _settled = im.wait_positions_settle(timeout=1.2, interval=0.1)
                    logger.info(f"[R84] 显示态补套前等 Explorer 落定={_settled}")
                except Exception:
                    pass
                # ★ R157：冻结重绘再补套——第二遍 restore_layout 是给
                # Explorer 记簿记的（隐藏态挪动不进持久化记忆），但图标
                # 会肉眼可见地再动一次（"双跑抖动"）。WM_SETREDRAW 冻结
                # ListView → 补套（含读回验证）→ 解冻+重绘：簿记照发生，
                # 用户看到的是一次到位。
                _lv = im.hwnd_listview
                _frozen = False
                try:
                    if _lv and win32gui.IsWindow(_lv):
                        send_msg_timeout(_lv, 0x000B, 0, 0)  # WM_SETREDRAW False
                        _frozen = True
                except Exception:
                    pass
                try:
                    im.restore_layout(_latest['path'])
                    im.arm_guard(_latest['path'], '全部')
                    logger.info("[R84] 显示后补套全部布局（冻结重绘，簿记照发生）")
                finally:
                    if _frozen:
                        try:
                            send_msg_timeout(_lv, 0x000B, 1, 0)  # WM_SETREDRAW True
                            win32gui.InvalidateRect(_lv, None, True)
                        except Exception:
                            pass
        except Exception as e:
            logger.error(f"显示后补套失败（不影响主流程）: {e}")

        # ★ R88/R91：定向看护武装——仅在"恢复桌面图标"后的 30 分钟窗口内
        # 点 All 才启用（平时整理完全自由，不受任何限制）。窗口内停留
        # "全部"视图时每 1.5s 校验，漂移归位；同图标二次漂移视为用户
        # 主动摆放，看护让位。
        try:
            if _quanbu and getattr(self.icon_manager, 'watch_enabled', True) \
                    and time.time() < getattr(self, '_restore_watch_pending', 0):
                self._restore_watch_until = time.time() + 600.0
                self._restore_watch_ref = dict(im._position_cache)
                self._watch_hb = 0
                self._watch_hits = {}
                # ★R234：成片重排的计数与"自动排列"提示都要按会话清零
                self._watch_reflow = 0
                self._watch_aa_warned = False
                # ★R270-R5：窗口期内每次点「全部」都会重跑这一段。旧写法直接
                # 调 _watch_poll()，而已在跑的那条链没人持有 id、取消不掉 ⇒
                # 连点 k 次 = k 条链并发，每条每 1.5s 全量跨进程重读桌面图标
                # 位置（长时间运行的负载放大器，用户报的"鼠标变卡"就吃在这）。
                # 取消动作只走 _after_cancel，桩具 FakeDock 没这个属性就跳过。
                _cancel = getattr(self, '_after_cancel', None)
                _pending = getattr(self, '_watch_after_id', None)
                if _cancel is not None and _pending is not None:
                    try:
                        _cancel(_pending)
                    except Exception:
                        pass
                self._watch_after_id = None
                self._watch_poll()
                logger.info("[R91] 布局看护已武装（恢复后窗口期，10 分钟，仅全部视图）")
        except Exception:
            pass

        # 第三步：设置 [全部] 视图的高亮名单
        # ★ R65-Bug4（用户拍板 2026-09-15）："框住分类和常驻"=只框
        #   全部分类成员∪常驻成员——此前"含未分类全框"把没进分类的图标
        #   也框上，用户实测"框住了桌面所有图标"。"框住系统图标"=追加系统
        #   图标；"只框分类"=剔除常驻成员；三态全关 → 无框。
        #   sys_only（只框系统图标）仍最高优先。
        # ★ R137：名单+重采+重绘+8s 漂移窗收口到统一出口（Ctrl+Alt+0 热键
        #   经 _activate_slot_by_name → _handle_click 也走这里）
        if self.highlighter:
            self.refresh_frames_after_switch('全部')

        self._log(f"✓ 已显示全部图标（恢复 {shown} 个）")
        self._auto_saved = True
        self._fire_visibility_changed()
        # ★R204 NEW-C12：透传统计给壳/调用方（switch.log 与提示不再恒 0）
        # ★R319-K15：`back`（从盒子里散回几枚）此前只在日志里念过、没进这份
        # 回执 ⇒ `on_switch_done` 拿不到，switch.log 的"散回="恒为 0，而 app.log
        # 同一次写着散回=58（★R318 那晚真机实锤）。数在源头就该带上。
        # ★R323 L3（用户「那一发打真值（现在念的是旧数）」）：把这一发的**全部真值**
        # 一次带全——收纳档、布局到底套没套上、散回那一步的六格分解。`shown` 是
        # **属性法**清掉隐藏属性的枚数，跟"从盒里搬回 N 枚"是两步不同的事，
        # 日志里必须分得开（用户口径「先搬回 N 枚、再清 M 枚」）。
        try:
            _mode = visibility_router.current_mode()
        except Exception as e:
            logger.warning('[隐藏盒] 收纳档读数失败（切换日志按 hide 记）: %s', e)
            _mode = visibility_router.MODE_HIDE
        return {'hidden': 0, 'shown': shown, 'failed': [], 'back': _rel,
                'mode': _mode, 'boxed': 0, 'layout_applied': _lay_ok,
                'kept_in_box': _rel_x['kept'], 'blocked': _rel_x['blocked'],
                'gone': _rel_x['gone'], 'renamed': _rel_x['renamed'],
                'stray': _rel_x['stray'], 'boxes_touched': _rel_x['boxes']}

    def _repaint_if_visible(self):
        if self.hwnd and self.is_visible:
            win32gui.InvalidateRect(self.hwnd, None, True)

    def sync_slots(self, categories: List[dict]):
        """按分类配置重建顶部槽位（只显示 in_dock=True 的分类，前 max_slots 个 + 工具槽）

        重建即退出家长模式的临时槽位（gui 在模式结束/分类变动时调用）。
        排序规则：非 sticky 分类在前，sticky 分类（AI/公共）靠后，
        ★R280-D 排行组临时区再靠后（**不占** max_slots 个固定槽），全部在最末。
        """
        self.kids_mode = False
        all_slots = [
            QuickSlot(name=c.get('name', ''), color=c.get('color', '#8be9fd'))
            for c in categories if c.get('in_dock', True)
        ]
        all_slots = [s for s in all_slots if s.name and s.name not in self.UTILITY_SLOTS]
        # ★R280-D（用户 06 批 item7 真机实锤）：排行组的身份证是分类里的 `rank_source`
        # 键（★R279-⑤ 同一条判据，不靠名字猜），它单独走尾部临时区、不吃用户的槽。
        # 旧写法由 gui 侧数"标了 in_dock 的枚数"判槽满，与这里的截断规则**不是同一件事**：
        # 真机 13 枚标记 vs cap=10 ⇒ 排行组永远降级，"撤一枚再勾"撤完 12>10 还是不行。
        rank_names = {c.get('name') for c in categories
                      if CategoryManager.is_rank_group(c)}
        rank_slots = [s for s in all_slots if s.name in rank_names]
        all_slots = [s for s in all_slots if s.name not in rank_names]
        # ★R281-2a：把进程内登记的「本次上栏」组并进同一个尾部临时区。
        # 两条口径要紧：①判据仍只有 `rank_names`（排行组身份证）——登记过但不是一枚
        # 排行组、或分类表里根本没有的名字，一律上不了栏（S8/S9），免得造出空槽
        # （空分类会把整个桌面 ListView 藏掉，★R204 那条）；②同名只留一枚（S6/S12），
        # 因为「↻ 刷新组」每轮回灌、勾常驻时也会 drop，两边都可能撞上。
        # 图标名单不需要在这里另想办法：点它的时候走的是 `get_category_icons`，
        # 那条本来就读得到 in_dock=False 的组（X2 钉住"只有一条取名单的路"）。
        _seen = {s.name for s in rank_slots}
        for _nm in (getattr(self, '_session_rank', None) or []):
            if _nm in rank_names and _nm not in _seen:
                _seen.add(_nm)
                rank_slots.append(QuickSlot(
                    name=_nm, color=self.category_manager.get_category_color(_nm)))
        # 非 sticky 在前，sticky 分类（用户自定义，默认 AI/公共）靠后
        normal = [s for s in all_slots if not self.category_manager.is_sticky(s.name)]
        sticky = [s for s in all_slots if self.category_manager.is_sticky(s.name)]
        cat_slots = normal + sticky
        if self.max_slots > 0 and len(cat_slots) > self.max_slots:
            # 被截断了：只取前 max_slots 个，同时记日志说明谁被挤掉了
            excluded = [s.name for s in cat_slots[self.max_slots:]]
            # ★R300-F3：这句原先写 logger.debug——用户机的默认档位是 INFO，
            #   DEBUG 根本不落盘 ⇒ 分类被悄悄挤掉、日志里查无此事（win11 真机
            #   13 个 in_dock 对 12/10 槽，两次挤掉的都含「公共」而无人知晓）。
            #   升 WARNING，并按挤掉的名单去重：同一份结果反复 sync 只说一次
            #   （★R232 那条"提示反复出现"的同款病）。
            _drop_sig = tuple(excluded)
            if getattr(self, '_slot_drop_logged', None) != _drop_sig:
                self._slot_drop_logged = _drop_sig
                logger.warning(
                    f'悬浮条槽位不够，本轮有 {len(excluded)} 个分类没放下：'
                    f'{excluded[:5]}'
                    f'{"...（更多）" if len(excluded) > 5 else ""}'
                    '（想让它们都显示：设置→悬浮条→把槽位上限调大，或选「全部」）')
            cat_slots = cat_slots[:self.max_slots]
        if len(rank_slots) > RANK_SLOT_MAX:
            # 临时区自己封顶，超出只挤自己人——**绝不**因为排行组建得多就撤用户的分类
            dropped = [s.name for s in rank_slots[RANK_SLOT_MAX:]]
            logger.debug(
                f'sync_slots: 排行组临时区 {len(rank_slots)} 枚超过上限 {RANK_SLOT_MAX}，'
                f'挤掉 {dropped[:5]}{"...（更多）" if len(dropped) > 5 else ""}（用户槽未动）')
            rank_slots = rank_slots[:RANK_SLOT_MAX]
        self.slots = cat_slots + rank_slots + self._make_utility_slots()
        self._save_slots()
        if self.is_visible:
            self._position_window()
        self._repaint_if_visible()

    def add_session_rank_slot(self, name: str) -> bool:
        """★R281-2a（用户 08 批 item2a）：登记一枚排行组「本次上栏」。

        用户口径原话：「不点常驻，就是悬浮栏显示，但是下次重启系统自动消失」。
        旧写法里这一档**根本不存在**——尾部临时区只接 `in_dock=True` 的组，
        不勾常驻就彻底不上栏（真机日志 L218→L225 中间零条 dock 记录）。

        这里只**登记**，不判"能不能显示"：真正的准入在 `sync_slots`（按分类表的
        排行组身份证 + 表里确有这一组），所以调用方拿到的 True 意思是"记下了"，
        不是"栏上一定有"。名字归一化后为空 ⇒ 不记。
        """
        name = (name or '').strip()
        if not name:
            return False
        if name in self._session_rank:
            return True
        self._session_rank.append(name)
        logger.info(f'[dock] 排行组 [{name}] 登记为本次上栏（不落盘，重启即消）')
        return True

    def drop_session_rank_slot(self, name: str) -> bool:
        """撤掉「本次上栏」登记（勾了常驻 / 取消常驻又反悔 / 整组删掉时调）。
        返回 True=确实撤掉了一条，False=本来就没登记。"""
        name = (name or '').strip()
        if not name or name not in self._session_rank:
            return False
        self._session_rank = [x for x in self._session_rank if x != name]
        logger.info(f'[dock] 撤掉排行组 [{name}] 的本次上栏登记')
        return True

    def _make_utility_slots(self) -> List[QuickSlot]:
        return [
            QuickSlot(name='全部', color='#f8f8f2'),
            QuickSlot(name='窗', color='#6272a3'),
            QuickSlot(name='一键目录', color='#50fa7b'),
            QuickSlot(name='✕', color='#ff5555'),
        ]

    # ----------------------- 家长模式（儿童桌面） -----------------------

    def set_kids_slots(self, cat_name: str, color: str = '#ff5555'):
        """进入家长模式：横条只留 [分类] [⏱倒计时] [退出]
        （2026-09-05 用户要求去掉"全部"——儿童模式不需要）"""
        self.kids_mode = True
        self.kids_category = cat_name
        self.slots = [
            QuickSlot(name=cat_name, color=color),
            QuickSlot(name='⏱--', color='#ffb86c'),
            QuickSlot(name='退出', color='#ff5555'),
        ]
        if self.is_visible:
            self._position_window()
        self._repaint_if_visible()

    def update_kids_time(self, text: str):
        """更新横条上的倒计时文字"""
        for s in self.slots:
            if s.name.startswith('⏱'):
                if s.name != text:
                    s.name = text
                    self._position_window()   # ★ R78 文字宽度变了要重定窗宽
                    self._repaint_if_visible()
                return

    def add_slot(self, name: str, color: str = "#8be9fd"):
        self.slots.append(QuickSlot(name=name, color=color))
        self._save_slots()
        if self.is_visible:
            self._position_window()
        self._repaint_if_visible()

    def rename_slot(self, index: int, new_name: str):
        if 0 <= index < len(self.slots):
            self.slots[index].name = new_name
            self._save_slots()
            self._repaint_if_visible()

    def delete_slot(self, index: int):
        if 0 <= index < len(self.slots):
            self.slots.pop(index)
            self._save_slots()
            if self.is_visible:
                self._position_window()
            self._repaint_if_visible()

    def set_slot_icons(self, index: int, layout_path: str):
        if 0 <= index < len(self.slots):
            self.slots[index].layout_path = layout_path
            self._save_slots()

    def set_enabled(self, v: bool):
        self.enabled = bool(v)
        if not self.enabled:
            self.hide_dock()

    def set_always_on_top(self, v: bool):
        self.always_on_top = bool(v)
        if self.is_visible:
            self._position_window()

    def set_auto_show_top(self, v: bool):
        self.auto_show_top = bool(v)

    def set_text_colors(self, hex_text, active: bool):
        """设置悬浮条文字颜色（未选中/选中），hex 形如 #RRGGBB。

        ★ R153：传 None=清除自定义、恢复自动对比档（随主题/槽位底色
        自动选深浅字）。"""
        if hex_text is None:
            if active:
                self._custom_active = None
            else:
                self._custom_text = None
            try:
                self._repaint_if_visible()
            except Exception:
                pass
            return
        try:
            r = int(hex_text[1:3], 16)
            g = int(hex_text[3:5], 16)
            b = int(hex_text[5:7], 16)
        except Exception:
            return
        if active:
            self.active_text_color = (r, g, b)
            self._custom_active = (r, g, b)
        else:
            self.text_color = (r, g, b)
            self._custom_text = (r, g, b)

    # 悬浮条兜底色板（★R218：正常路径由 apply_theme(colors=COLORS) 从主界面
    # 调色板派生，这里只在拿不到 COLORS 时兜底，故不再手写 hover/base）
    THEME_PALETTES = {
        '深色': {'bg': (24, 24, 34), 'text': (235, 238, 245), 'primary': (139, 233, 253)},
        '暗黑': {'bg': (10, 10, 14), 'text': (235, 238, 245), 'primary': (139, 233, 253)},
        '浅色': {'bg': (244, 245, 247), 'text': (36, 41, 47), 'primary': (9, 105, 214)},
        '护眼': {'bg': (232, 230, 220), 'text': (59, 58, 51), 'primary': (107, 142, 35)},
    }

    @classmethod
    def palette_from_colors(cls, colors: dict, level: str = None) -> dict:
        """★R218 阶段 3：从主界面 COLORS（hex 字符串字典）派生悬浮条配色。

        以前悬浮条自带一套手写 THEME_PALETTES，与主界面色板**各改各的**，
        换肤时条子的 hover/底色跟主窗对不上（用户"悬浮栏差点意思"的一部分）。
        现在派生关系明确：底=COLORS['bg']，文字=fg，激活文字=primary，
        悬停/分隔/边线都由底色朝文字/边框混一档得到 ⇒ 主界面改色板，条子自动跟上。
        单独暴露成 classmethod：无头测试可直接断言派生结果，不必真起窗口。"""
        bg = _hex_to_rgb(colors.get('bg', '#282a36'))
        fg = _hex_to_rgb(colors.get('fg', '#f8f8f2'))
        pri = _hex_to_rgb(colors.get('primary', '#8be9fd'))
        bord = _hex_to_rgb(colors.get('card_border')
                           or colors.get('border') or '#636d83')
        # 亮底往深里混、暗底往浅里混，悬停才始终"看得见抬了一档"
        hov_k = 0.10 if _lum(bg) < 128 else 0.13
        # ★R278-B：暗底的高光线往白里抬，亮底往文字色压（近白底再抬白＝看不见，
        # 亮主题的顶部那条线本来就该是"接缝"而不是"反光"）
        dark_bg = _lum(bg) < 128
        return {
            'bg': bg, 'text': fg, 'primary': pri, 'base': bg,
            'hover': _mix(bg, fg, hov_k),
            'active_fill': _mix(bg, pri, 0.24),
            'sep': _mix(bg, bord, 0.55),
            'edge': _mix(bg, bord, 0.85),
            # 渐变底两端：上浅下深各一档，条子才有厚度（★R218 的纯色底
            # 用户读成"贴上去的纸板"）。全部 _mix 派生 ⇒ 换肤自动跟随。
            # ★R279-④：系数读模块级那三个常量（旧值 0.10/0.12/0.30 跨度只有 27 级，
            # 用户实测"看不出来区别"）
            # ★R280-A：改成按档位取（level 不传＝默认档「明显」），三档跨度
            # 48/66/132 级量具在 测试_ui/test_r280_gradient_levels.py（L3b）
            'grad_top': _mix(bg, (255, 255, 255), grad_level_k(level)[0]),
            'grad_bot': _mix(bg, (0, 0, 0), grad_level_k(level)[1]),
            'hilite': (_mix(bg, (255, 255, 255), grad_level_k(level)[2]) if dark_bg
                       else _mix(bg, fg, 0.22)),
        }

    def apply_theme(self, theme: str, colors: dict = None):
        """应用悬浮条主题（底/文字/悬停/激活/分隔/边线/渐变底+高光线）并立即重绘。

        colors 传主界面 COLORS 时按它派生；不传则退到 THEME_PALETTES 兜底。"""
        pal = None
        # ★R280-A：grad_level 走 getattr——`__init__` 会设它，但历轮门禁/桩具里有
        # 不少"不跑 __init__ 直接 __new__ 一枚出来只调 apply_theme"的实例
        # （test_r279_dock_gradient_span 就是这么造的）。写死 self.grad_level
        # 会把那种实例炸成 AttributeError ⇒ 派生失败退化到"配色整个不对"，
        # 是测量工具在骗人，不是产品坏了。兜底档 = 模块默认档，与 __init__ 同源。
        _lvl = getattr(self, 'grad_level', GRAD_DEFAULT_LEVEL)
        if colors:
            try:
                pal = self.palette_from_colors(colors, level=_lvl)
            except Exception as e:
                logger.warning(f"悬浮条配色派生失败，用兜底色板: {e}")
                pal = None
        if pal is None:
            base = self.THEME_PALETTES.get(theme) or self.THEME_PALETTES['深色']
            pal = self.palette_from_colors({
                'bg': '#%02x%02x%02x' % base['bg'],
                'fg': '#%02x%02x%02x' % base['text'],
                'primary': '#%02x%02x%02x' % base['primary']},
                level=_lvl)
        self._bg = pal['bg']
        self._text = pal['text']
        self._primary = pal['primary']
        self._hover = pal['hover']
        self._base = pal['base']
        self._active_fill = pal['active_fill']
        self._sep = pal['sep']
        self._edge = pal['edge']
        # ★R278-B：渐变底两端 + 顶部高光线（两条 pal 分支都出自
        # palette_from_colors ⇒ 这三个键必然在场）
        self._grad_top = pal['grad_top']
        self._grad_bot = pal['grad_bot']
        self._hilite = pal['hilite']
        self.text_color = self._text
        try:
            self._repaint_if_visible()
        except Exception:
            pass

    def set_gradient_level(self, name):
        """★R280-A：换渐变幅度档（'朴素'/'微渐变'/'明显'），只重算三个派生色并即时重绘。

        换档不改底色/文字色，所以不走 apply_theme（那会顺带把 pal 的其它键重刷一遍，
        用户在设置页动一下渐变档就把别的配色推倒，属于意外副作用）。
        认不得的档名回默认档，返回值＝真正生效的那一档（gui 侧据此回显，别显示一个没生效的）。"""
        if name not in GRADIENT_LEVELS:
            name = GRAD_DEFAULT_LEVEL
        self.grad_level = name
        _kt, _kb, _kh = grad_level_k(name)
        dark_bg = _lum(self._bg) < 128
        self._grad_top = _mix(self._bg, (255, 255, 255), _kt)
        self._grad_bot = _mix(self._bg, (0, 0, 0), _kb)
        # 亮底往白里抬抬不动（顶到 255），所以亮主题的高光线仍走"往文字色压"那条公式
        self._hilite = (_mix(self._bg, (255, 255, 255), _kh) if dark_bg
                        else _mix(self._bg, self._text, 0.22))
        self._repaint_if_visible()
        return name

    def set_font_size(self, size):
        """设置横条字体大小（小13/中16/大20/超大26），条高随动，立即重绘"""
        try:
            self.font_size = max(10, min(28, int(size)))
        except (TypeError, ValueError):
            return
        self._clear_hfont_cache()   # ★ P1-F font_size 变了，字体句柄要重建
        self.dock_height = self._calc_height(self.font_size)
        if self.is_visible:
            self._position_window()
        self._repaint_if_visible()

    def _clear_hfont_cache(self):
        """★ P1-F 释放缓存的 HFONT（GDI 句柄必须显式 DeleteObject）。
        set_font_size 改字号、destroy 退出时调。"""
        for hfont in self._hfont_cache.values():
            try:
                win32gui.DeleteObject(hfont)
            except Exception:
                pass
        self._hfont_cache.clear()

    # -------------------------------------------------------------------------
    # 数据持久化
    # -------------------------------------------------------------------------

    def _load_slots(self) -> List[QuickSlot]:
        slots_file = os.path.join(self.config_dir, "docks.json")
        slots = None
        if os.path.exists(slots_file):
            try:
                with open(slots_file, "r", encoding="utf-8") as f:
                    slots = [QuickSlot.from_dict(s) for s in json.load(f)]
            except Exception as e:
                logger.error(f"加载快捷槽配置失败: {e}")

        if slots is None:
            slots = [
                QuickSlot(name="英语", color="#bd93f9"),
                
                QuickSlot(name="程序", color="#8be9fd"),
                QuickSlot(name="网络", color="#50fa7b"),
                QuickSlot(name="视频", color="#ffb86c"),
                QuickSlot(name="学习", color="#50fa7b"),
                QuickSlot(name="小朋友", color="#ff5555"),
            ]

        # 旧配置迁移：确保"网络"槽位存在
        names = [s.name for s in slots]
        if "网络" not in names:
            idx = names.index("程序") + 1 if "程序" in names else len(slots)
            slots.insert(idx, QuickSlot(name="网络", color="#50fa7b"))
            self._pending_save_slots = True

        # 过滤工具槽 + 排行组临时区（两者都不持久化），按 max_slots 截断后追加
        # ★R280-S6（独立验收命中）：这里以前只剔工具槽，于是 `_load_slots` 变成
        # **第二套"槽满"口径**——它把排行组也算进 max_slots 那把尺里截一刀。
        # gui/rank_display 侧的守卫已整体摘除、判定收成 sync_slots 一处，但导入/回滚
        # 那两条路是直接 `dock.slots = dock._load_slots()`（data_manage_page :933/:1271），
        # 不跟 sync_slots ⇒ 排行组被尾巴截掉，正是用户 item7 那句"有组无栏"的同一种分裂
        # （重启才自愈）。排行组和工具槽一样是**运行时派生**的（sync_slots 每次都按
        # 分类表重建），所以读盘时按同一把判据（`_cat_is_rank_group`，不靠名字猜）剔掉。
        slots = [s for s in slots if s.name not in self.UTILITY_SLOTS
                 and not self._cat_is_rank_group(s.name)]
        if self.max_slots > 0:
            slots = slots[:self.max_slots]
        slots = slots + self._make_utility_slots()
        return slots

    def _save_slots(self):
        if self.kids_mode:
            return  # 儿童模式槽位是临时的，退出时由 sync_slots 重建
        try:
            # 工具槽（全部/窗/✕）是运行时自动追加的，不持久化；★R280-S6：排行组同理
            # ——它是分类表 `rank_source` 的派生物，写进 docks.json 就等于让「数据 →
            # 全部导入/导出」那节把组名搬到别的机器上（那边根本没有这条分类，条上会
            # 多一枚点不动的幽灵胶囊），与 ★R279"排行组是临时功能、不跨机搬"同一条口径。
            persist = [s.to_dict() for s in self.slots
                       if s.name not in self.UTILITY_SLOTS
                       and not self._cat_is_rank_group(s.name)]
            _ok = atomic_write_json(os.path.join(self.config_dir, "docks.json"),
                                    persist, indent=4)
            # ★R283：消费返回值——atomic_write_json 写失败只返回 False 不抛，
            # 旧写法丢掉它 = 用户拖的槽位顺序重启悄悄回退，还"保存成功"零留痕
            # （口径同 safeio 契约"返回值必须消费"、★R213 B11）。
            if not _ok:
                logger.error("保存快捷槽配置未落盘（atomic_write_json 返回 False，"
                             "docks.json 可能被占用/只读），本次槽位改动不会保留")
        except Exception as e:
            logger.error(f"保存快捷槽配置失败: {e}")

    def destroy(self):
        """销毁悬浮窗（退出）。

        属性法下图标位置永不变动，当前分类保持显示即"退出时恢复当前分类"
        （v0.3.45 定案的行为）；隐藏记录在 _hidden_state 里持久化，
        下次启动 recover_hidden_on_startup 可自愈。"""
        self._polling = False
        self.stop_icon_capture()   # ★ B18/R17：停全局鼠标监听
        self._after_cancel(self._poll_after_id)
        self._after_cancel(self._hide_after_id)
        self._after_cancel(self._debounce_timer)   # ★ P2-2 防悬挂 debounce
        self._clear_hfont_cache()                  # ★ P1-F 释放缓存的 HFONT 句柄
        if self.hwnd and win32gui.IsWindow(self.hwnd):
            try:
                win32gui.DestroyWindow(self.hwnd)
            except Exception:
                pass
        self.hwnd = None
        self.is_visible = False


# -------------------------------------------------------------------------
# 全局实例
# -------------------------------------------------------------------------

_dock_instance = None


def get_dock() -> Optional[DesktopDock]:
    return _dock_instance


def init_dock(icon_manager, category_manager, click_tracker,
              config_dir=None, tk_root=None, log=None) -> DesktopDock:
    global _dock_instance
    _dock_instance = DesktopDock(icon_manager, category_manager, click_tracker,
                                 config_dir, tk_root=tk_root, log=log)
    return _dock_instance
