"""图标高亮框模块 - 在桌面上用彩色边框圈住指定图标

实现方式：一个全屏透明 overlay 窗口（LWA_COLORKEY 黑色透明），
用 GDI 画矩形边框。WS_EX_TRANSPARENT 让鼠标点击穿透，不影响正常使用。
按图标"名称"记录高亮对象，每次重绘时重新解析索引，图标移动后框会跟着走。
"""
import sys
import struct
import ctypes
import logging
import threading
import queue
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import win32gui
import win32con
import win32api
import win32process

# 添加日志系统
sys.path.insert(0, str(Path(__file__).parent.parent / 'utils'))
from utils.logger import log_frame_debug, log_debug, log_error, print_frame_info, print_alignment_info

logger = logging.getLogger("desktop_manager")

LVM_GETITEMCOUNT = 0x1004
from utils.win32_safe import send_msg_timeout
LVM_GETITEMRECT = 0x100E     # lParam = 远程 RECT，RECT.left = LVIR_* 输入标记
LVM_GETITEMPOSITION = 0x1010
LVIR_BOUNDS = 0              # 整个图块（图标 + 文字标签）的精确矩形
MEM_COMMIT = 0x1000
MEM_RELEASE = 0x8000
PAGE_READWRITE = 0x04
WM_ERASEBKGND = 0x0014
WS_EX_NOACTIVATE = 0x08000000
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
# ★ P1-I 签名容差（像素）：坐标在此范围内的微抖不触发重绘。
# Explorer DPI 计算、窗口阴影偏移等常导致 1~2px 抖动，
# 精确比对会让签名不断跳变 → overlay destroy+create 闪烁。
_SIG_TOLERANCE = 4


def _sig_round(x):
    """★ P1-I 坐标容差舍入：把 x 向下取整到 _SIG_TOLERANCE 的倍数"""
    return (int(x) // _SIG_TOLERANCE) * _SIG_TOLERANCE


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [('cbSize', ctypes.c_uint), ('dwTime', ctypes.c_uint)]


# ★R334-a：本文件用到的 ctypes 原型集中钉住。两处是"现在就在错"的：
# ① SetThreadDpiAwarenessContext 的 restype/argtypes 过去只在
#    `_set_thread_pmv2` 里就地声明，`_restore_thread_dpi` 那发是另取一次
#    `ctypes.windll.user32`——同一个缓存对象所以侥幸生效，挪到模块级才是实话；
# ② GetTickCount 不声明时按 c_int 返回，开机 24.9 天后读成负数（下面那句
#    `& 0xFFFFFFFF` 正是给它打的补丁，声明成 c_uint 后补丁仍成立）。
# dwmapi 在极老系统上可能载不进，声明不能让模块导入跟着崩。
try:
    ctypes.windll.user32.GetLastInputInfo.restype = ctypes.c_int
    ctypes.windll.user32.GetLastInputInfo.argtypes = [ctypes.c_void_p]
    ctypes.windll.kernel32.GetTickCount.restype = ctypes.c_uint
    ctypes.windll.kernel32.GetTickCount.argtypes = []
    ctypes.windll.user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    ctypes.windll.user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    ctypes.windll.dwmapi.DwmGetWindowAttribute.restype = ctypes.c_int
    ctypes.windll.dwmapi.DwmGetWindowAttribute.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
    ctypes.windll.gdi32.RoundRect.restype = ctypes.c_int
    ctypes.windll.gdi32.RoundRect.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                              ctypes.c_int, ctypes.c_int,
                                              ctypes.c_int, ctypes.c_int,
                                              ctypes.c_int]
except Exception as _e:      # 缺导出/载不进 DLL：保持旧口径（默认 int），不留暗账
    logger.debug("ctypes 原型声明跳过: %s" % _e)


def _idle_seconds() -> float:
    """距最后一次键盘/鼠标输入的秒数（本机 user32 调用，不跨进程）。

    读不到就返回 0 —— 即"当作有人在用"，监视循环保持快节奏，宁多读不漏看。
    GetTickCount 约 49.7 天回绕，减法按无符号补回来。
    """
    try:
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        return ((ctypes.windll.kernel32.GetTickCount()
                 - info.dwTime) & 0xFFFFFFFF) / 1000.0
    except Exception:
        return 0.0


def _virtual_desktop_bounds() -> Tuple[int, int, int, int]:
    """虚拟桌面物理边界 (left, top, right, bottom)；越界坐标不允许写回缓存"""
    try:
        vx = win32api.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
        vy = win32api.GetSystemMetrics(77)   # SM_YVIRTUALSCREEN
        cw = win32api.GetSystemMetrics(78)   # SM_CXVIRTUALSCREEN
        ch = win32api.GetSystemMetrics(79)   # SM_CYVIRTUALSCREEN
        return (vx, vy, vx + cw, vy + ch)
    except Exception:
        return (0, 0, 1920, 1080)


PRESET_COLORS = {
    '蓝色': '#00BFFF', '绿色': '#00FF00', '红色': '#FF0000',
    '黄色': '#FFFF00', '橙色': '#FF8C00', '紫色': '#9932CC',
    '粉色': '#FF69B4', '青色': '#00FFFF', '白色': '#FFFFFF',
}

# 框形状（GDI 实现）与预设主题：一键切换 颜色/线宽/虚实/框大小/形状
FRAME_SHAPES = ['圆角立体', '圆角', '直角', '双线', '下划线', 'U形', '四角']
THEMES = {
    '经典蓝':   {'color': '#00BFFF', 'width': 2, 'dashed': False, 'pad': 1, 'shape': '圆角立体'},
    '暗黑':     {'color': '#565B69', 'width': 2, 'dashed': False, 'pad': 1, 'shape': '直角'},
    '护眼':     {'color': '#8BC34A', 'width': 2, 'dashed': False, 'pad': 1, 'shape': '圆角'},
    '少女':     {'color': '#FF7BAC', 'width': 2, 'dashed': False, 'pad': 2, 'shape': '双线'},
    '卡通':     {'color': '#FFC107', 'width': 3, 'dashed': False, 'pad': 2, 'shape': '圆角立体'},
    '霓虹绿':   {'color': '#39FF14', 'width': 3, 'dashed': False, 'pad': 1, 'shape': '圆角立体'},
    '荧光黄':   {'color': '#FFD700', 'width': 2, 'dashed': False, 'pad': 1, 'shape': '圆角'},
    '警示红':   {'color': '#FF3333', 'width': 3, 'dashed': False, 'pad': 2, 'shape': '直角'},
    '清新紫':   {'color': '#B388FF', 'width': 2, 'dashed': False, 'pad': 1, 'shape': '圆角'},
    '樱花粉':   {'color': '#FF69B4', 'width': 2, 'dashed': False, 'pad': 2, 'shape': '双线'},
    '低调虚线': {'color': '#CCCCCC', 'width': 1, 'dashed': True,  'pad': 1, 'shape': '直角'},
    '加粗橙':   {'color': '#FF8C00', 'width': 4, 'dashed': False, 'pad': 3, 'shape': '直角'},
}

# 显示方式：label ↔ 代码（2026-09-05 四种高亮方案；★R273 批29 B5 追加四档，
# 旧四档逐字不动——用户 item5"保留之前的样式，可以选。这样可选范围多"）
DISPLAY_MODES = {
    '方框': 'border',
    '色块垫底': 'block',
    '文字高亮': 'text_hl',
    '聚光灯': 'spotlight',
    '分区': 'zone',              # 同类相邻并成一团，一团一个包络+分类名小牌
    '色块纱环': 'block_ring',    # 四周垫子是抖动纱，中间真透明
    '荧光笔': 'marker',          # 色条只压文字下沿
    '角贴': 'badge',             # 右上角一枚分类色圆点
}

# ★R278-D（用户 09-30 点名）：方式 → 该方式**渲染路径真消费**的样式控件。
# 面板据此把用不到的下拉变灰——"点了没反应"会被用户读成"程序没设计好"。
# 这张表逐档对着 core/icon_overlay.py 的实时绘制分支量过（不是猜的）：
#   · border 走形状分支（icon_overlay.py:457-526）⇒ shape/width/style 全用；
#   · block / text_hl / block_ring 提前 return，但 return 前那支按
#     width/dashed 建的 pen 已被 SelectObject 且在 RoundRect/Rectangle 描边
#     （:349-353 → :369/:397/:414）⇒ 只用 width/style，形状本身不改画面；
#   · marker 换 NULL_PEN（:441）、badge 走自带 _dot pen（:328）⇒ 三味全不用；
#   · spotlight 不进 draw_rect（:543）⇒ 三味全不用；
#   · zone 也不进 draw_rect（:557），★R278-D 起 _draw_cluster 按 width/dashed
#     建笔 ⇒ 分区终于跟"样式/线宽"联动（用户点名的那条）。
# 不进表的：颜色、渐变色、模式（每档都读，恒不灰）；框大小 pad **没有任何一档
# 写它**——实时渲染侧零读者（IconOverlayWindow 上没有 pad 属性），所以恒灰。
# ★R279-① 新增 'merge'＝并团松紧：只有 zone 那一路会把它换算成余量倍率
# （icon_overlay._draw_zones → zone_clusters(tight=…)），其余七档恒灰。
DISPLAY_CAPS = {
    'border':      {'shape', 'width', 'style'},
    'block':       {'width', 'style'},
    'text_hl':     {'width', 'style'},
    'spotlight':   set(),
    # ★R282-4（用户 10-01 01 批 R010-3）：分区升为主功能——形状解锁（_draw_cluster
    # 按 self.shape 画多种分区框），并新增 zone_color（"分区配色"下拉，只有这一档读）。
    # 于是 形状/线宽/样式/配色 四味对分区都真联动；颜色/主题经"统一色"这一档也接进来。
    'zone':        {'width', 'style', 'merge', 'shape', 'zone_color', 'paint'},
    'block_ring':  {'width', 'style'},
    'marker':      set(),
    'badge':       set(),
}

# ★R288（批44，用户点名"把色块纱环/荧光笔/角贴也支持到分区里，分区更多更好看，
# 别丢原功能"）：分区内部再分「画法」。★R289 起这张表**八档**、★R293 起**十档**。
# ★R288/★R289 的旧口径是"只有默认档 包络框 读 形状/线宽/样式，其余档自带笔/自带图案
# ⇒ 那三味一律灰掉"；★R293-③（用户 ③-3"三味控件不能只管一档"）把它**逐档打开**：
# 每档认领哪些，取决于渲染侧真的读不读——表说吃，改那一味就必须改画面（闸 R293 的
# 正向/负向两路像素判据按这张表逐档反证），表说不吃，面板灰掉才是实话。
# 剩下的"不吃"都有物理原因，不是偷懒：
#   · 抖动/图案/实心块那几档没有笔 ⇒ 虚线（样式）无从谈起；
#   · 一条横带（荧光笔/斜纹带）与一枚角点没有"形状"可言；
#   · 角贴 借用逐图标那枚 `_dot`，动它等于同时动"角贴显示模式"那条链 ⇒ ★R293 挂账。
ZONE_PAINT_CAPS = {
    '包络框': {'shape', 'width', 'style'},
    '色块纱环': {'shape', 'width'},
    '荧光笔': {'width'},
    '角贴': set(),
    # ★R289（批45，用户点名"目前 4 个不多，弄点有创新的样式出来"）：新四档。
    # ★R293 起取景角/双线框/斜纹带/柔光晕也都认领了（自带笔的档吃线宽/形状/虚线，
    # 实心块那档只吃线宽）——认领集不再全空。
    '取景角': {'width'},
    '双线框': {'shape', 'width', 'style'},
    '斜纹带': {'width'},
    '柔光晕': {'shape', 'width'},
    # ★R293-②（批49，用户 ③-2"太抢眼"）：两档"细"预设＝双线框/柔光晕换一组更小的
    # 基准数（`DOUBLE_GAP_THIN` 等），认领集与各自的老档**逐字相同** ⇒ 切到细档不会
    # 让面板忽然多出/少掉一味（闸 R293-A3 钉这三档的联动集必须成对相等）。
    '细双线框': {'shape', 'width', 'style'},
    '细柔光晕': {'shape', 'width'},
}
# 画法这一档本身、以及"隔几格算一伙/配色"四档画法统统要读 ⇒ 只要方式是分区就不灰
_ZONE_ALWAYS_CAPS = {'merge', 'zone_color', 'paint'}


def zone_caps(paint: str) -> set:
    """方式=分区时真正可用的控件名单（按「画法」再收一层）。

    默认档（包络框）与 `DISPLAY_CAPS['zone']` 逐字相等——这条等价关系由
    test_r288 C 段钉住：它是"新增画法没把老功能灰掉"的唯一硬证据。"""
    return _ZONE_ALWAYS_CAPS | ZONE_PAINT_CAPS.get(paint, set())

# ★R282-4：分区配色两档。'跟随分类色'＝一团一色（旧行为，默认）；
# '统一色'＝全团用所选颜色 ⇒ "颜色/模式(主题)" 两个下拉对分区也生效
# （用户点名"分区跟模式、颜色都不能联动"）。只有 display_mode=='zone' 读它。
ZONE_COLOR_MODES = ('跟随分类色', '统一色')


def _hex_to_rgb(h: str) -> Tuple[int, int, int]:
    # ★R318 K1：非字符串先挡。`config/highlight.json` 是官方推荐手改、又在「导入
    # 全部数据」白名单里零校验，旧写法第一句 `(h or '#00BFFF').lstrip('#')` 对
    # 数字/数组直接 AttributeError，而调用点在**主窗构建链且无 try**
    # （gui/rule_panel._load_highlight_settings → gui/main_window.py:299）
    # ⇒ 一个脏 color 就让程序打不开（launcher 重启 5 次后放弃）。
    # 上层另有 `gui/rule_panel._hl_color` 做字段级取值与留痕，这一道是"函数自己不炸"
    # 的兜底（同文件 set_width 的 TypeError 兜底同源）。
    if not isinstance(h, str):
        return (0, 191, 255)
    h = (h or '#00BFFF').lstrip('#')
    if len(h) == 6:
        try:
            return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            pass
    return (0, 191, 255)


class IconHighlighter:
    """在桌面图标上绘制可自定义颜色/风格的边框"""

    # ★R270-R1（用户 24 批口径："要在保障软件流畅的情况下，才保障统计的准确性"）
    # 监视线程每轮都要往 Explorer 桌面线程发上百次同步跨进程读，长时间挂着
    # 就是在持续压用户输入链的响应。没人碰键盘鼠标时桌面上不会有东西变化，
    # 把节拍放大到 _IDLE_FACTOR 倍；一有输入下一轮立刻回到 _mon_interval。
    _IDLE_AFTER_SEC = 8.0
    _IDLE_FACTOR = 4.0
    # ★R270-R2：图标总数没变时，名称→索引映射最多每这么多秒真读一次全表
    _NAME_MAP_STALE_SEC = 6.0

    # ★R272（批25，用户 `----2026-9-29 01----`）：把"一个节拍管所有事"拆成两层。
    # 心跳层永远醒着、只调本地 user32；重采层窗口内慢爬、窗口外平方快递减到睡死。
    # 实测依据 D:/Qoder-workplace/tmp/r270/probe_heartbeat.txt：一整拍心跳
    # （2×FindWindowEx + 2×GetWindowLong + GetAsyncKeyState + GetCursorPos）
    # = 15.86 µs、跨进程往返 0 次；而一次 SendMessage(LVM_GETITEMCOUNT) 跨进程
    # = 465.32 µs ≈ 29 整拍心跳 ⇒ 贵的从来不是"醒着"，是"跨进程读图标"。
    # 所以心跳敢从 8 s 提到 2 s（43,200 拍/24h × 15.86 µs = 0.69 s CPU/24h）。
    _PROBE_BEAT_SEC = 2.0     # 心跳层节拍
    _BASE_SEC = 2.0           # 密集窗口起点节拍（= _mon_interval 缺省）
    _WIN_END_SEC = 6.0        # 密集窗口末尾节拍（"递减的慢点"就是线性爬到这条）
    _WIN_SEC_DEFAULT = 180.0  # 密集窗口长度缺省（用户点名"持续监视3分钟"）
    _WIN_SEC_MIN = 30.0       # 可配下限
    _WIN_SEC_MAX = 600.0      # 可配上限
    _DECAY_K = 40.0           # 窗口外平方递减系数：2 + 40k² ⇒ 42/162/362/642
    _DORMANT_SEC = 600.0      # 递减算出来 ≥ 这条 ⇒ 彻底睡死（不再有兜底巡视）
    _STABLE_ROUNDS = 2        # 连续几轮签名一致才算"图标稳定"

    _class_registered = False
    _by_hwnd: Dict[int, "IconHighlighter"] = {}

    def __init__(self, icon_manager=None):
        self.icon_manager = icon_manager
        self._names: List[str] = []
        self._color: Tuple[int, int, int] = (0, 191, 255)
        self._width: int = 2
        self._dashed: bool = False
        self._display_mode: str = 'border'  # 8 档码值见 DISPLAY_MODES（★R273 新增 zone/block_ring/marker/badge）
        # ★R279-①：并团档（只有 display_mode=='zone' 会读；档位表在 icon_overlay.ZONE_TIGHTNESS）
        # ★R281-4：默认档跟着表改名（'中'→'挨着'，算式没变，还是 ★R278-C 那一档）
        self._zone_tightness: str = '挨着'
        # ★R282-4：分区配色（只有 display_mode=='zone' 读；'统一色' 时颜色/主题联动分区）
        self._zone_color_mode: str = '跟随分类色'
        # ★R288：分区「画法」（包络框/色块纱环/荧光笔/角贴；同样只有 zone 那一路读，
        # 默认档 = 修前唯一的画法 ⇒ 老配置/没这个键的存档进来画面不变）
        self._zone_style: str = '包络框'
        self._pad: int = 1          # 框大小：向外扩展的像素数
        self._shape: str = '圆角立体'   # 圆角立体/圆角/直角/双线
        self._bottom_align: bool = True  # 同列框底边对齐（图标文字行数不同导致底边参差）
        self._frame_all: bool = False   # 框住全部图标（低资源：位置没变不重绘）
        self._gradient_mode: bool = False  # 渐变色模式：按分类各自颜色画框
        self._color_map: Dict[str, Tuple[int, int, int]] = {}  # {icon_name: (r,g,b)}
        self._last_sig = None           # 上次绘制的位置签名
        self._recollect_until = 0.0     # ★ R105 重采窗口截止时刻
        self._last_raw = None           # ★ R105 上次绘制的实测原始矩形
        self._overlay = None
        # ★ 新增：基于 ZenDesktop 技术的新 overlay（UpdateLayeredWindow）
        self._zendesktop_overlay = None
        # 绘制缓存：跨进程读取在后台监视线程完成后，WM_PAINT 只读这些
        self._rect_cache: Dict[int, Tuple[int, int, int, int]] = {}
        self._idx_name_cache: Dict[int, str] = {}
        self._name_idx_cache: Dict[str, int] = {}
        self._hidden_snapshot: set = set()
        # 后台监视线程状态
        self._mon_stop = True
        self._mon_interval = 2.0
        self._mon_queue: 'queue.Queue' = queue.Queue()
        self._force_refresh = False
        # ★R272：重采层三态状态机（密集窗口内 / 窗口外递减 / 睡死）。
        # _recollect_due 由 _monitor_step 每拍写一次，_monitor_loop 读它决定
        # "这一拍到底读不读图标"——睡死态恒 False，一拍跨进程都不发。
        self._win_start = time.monotonic()
        self._win_sec_cfg = None      # None=用缺省；设置页经 set_watch_window 注入
        self._decay_k = 0
        self._stable = 0
        self._dormant = False
        self._recollect_due = True
        self._recollect_next_at = None   # ★R272-S6：窗口外重采 deadline
        self._probe_last = None       # 上一拍心跳快照（句柄/样式位）
        self._lbtn_down = False       # 上一拍左键按下态（用于抓"抬起"那条边）
        # ★R270-R2：名称→索引映射的跨进程全表读缓存，形状 (图标总数, 读取时刻,
        # 映射)。None=还没读过；_name_map_dirty=True 下次必须真读（见 invalidate）
        self._name_map_cache = None
        self._name_map_dirty = False
        self._delayed_refresh_id = None   # ★R270-R6：待跑的那一次延迟重绘
        self._kids_mode = False  # 家长模式下强制刷新，不等签名变化
        self._tk_root = None     # P2-4 gui 通过 ._tk_root = root 注入，供 notify_changed 延迟刷新用
        # ★ B12：分类模式只框分类自己的图标（常驻可见但不框）。
        # False = 切分类框 分类+常驻（跟"公共分类显示"矩阵，旧行为）；
        # True  = 只框该分类的图标，用于一眼看清"这个分类到底有哪些"。
        # gui 经设置页复选框注入并持久化（highlight.json: cat_only）。
        self.cat_only = False
        # ★ B15：框住系统图标——勾选后系统虚拟图标（此电脑/回收站等）进高亮名单。
        # 名单计算在 dock/main_window 侧，此开关仅作状态位（gui 持久化
        # highlight.json: frame_sys）。默认关：系统图标不框（用户定案）。
        self.frame_sys = False
        # ★ B16：只框系统图标——第三种框选范围（与 框住全部/只框分类 互斥，
        # 选中时 gui 侧把其他框选开关置灰）。True 时名单=系统虚拟图标，
        # 计算优先级最高（dock 侧分支先判本开关，frame_sys/cat_only 均忽略）。
        # gui 持久化 highlight.json: sys_only。
        self.sys_only = False

    # ---------------------------------------------------------------------
    # 设置
    # ---------------------------------------------------------------------

    @property
    def names(self) -> List[str]:
        return self._names.copy()

    def set_color_hex(self, hexstr: str):
        r, g, b = _hex_to_rgb(hexstr)
        if (r, g, b) == (0, 0, 0):  # 纯黑会被 colorkey 吃掉，改用近黑
            r, g, b = 1, 1, 1
        self._color = (r, g, b)
        self._refresh()

    def set_width(self, w):
        try:
            v = float(w)
        except (TypeError, ValueError):
            v = 1.0
        # GDI 最小实线宽度为 1px；0.5 是视觉"更细"的选项，落地为 1px
        self._width = max(1, min(8, int(max(0.5, v))))
        self._refresh()

    def set_dashed(self, dashed: bool):
        self._dashed = bool(dashed)
        self._refresh()

    def set_display_mode(self, mode: str):
        """显示方式：合法值直接由 DISPLAY_MODES 派生。

        ★R273：原先这里手写一份四值元组，新增档位会被**静默丢弃**（面板选了、
        桌面无反应、日志无痕）——白名单与表两处各写各的迟早对不上，改成一处。"""
        if mode in DISPLAY_MODES.values():
            self._display_mode = mode
            self._refresh()

    @property
    def display_mode(self) -> str:
        return self._display_mode

    def set_zone_tightness(self, name: str):
        """★R279-①：并团档位（码值表在 icon_overlay；★R281-4 起档名是
        挨着/隔1格/隔2格/隔3格，旧档名紧/中/松/很松 经 ZONE_TIGHT_LEGACY 折叠）。

        与 `set_display_mode` 同款口径：**认档才动**，认不得就整条跳过
        （不改值、不重绘、不留半个状态）——坏存档/坏导入不该把分区画框打死。
        折叠表只认历史那四个名字，别的怪值一律不碰（所以 `zone_tight_resolve`
        那种"认不得回默认档"的口径在这里不能用：它会把坏值静默改成"挨着"，
        等于用户选了个怪值、画面却自己跳档）。
        只有"分区"那一路真读它（`icon_overlay._draw_zones`），其余档位改了
        也只是存着，等切到分区才见效。"""
        from core.icon_overlay import (ZONE_TIGHT_LEGACY, ZONE_TIGHTNESS)
        key = ZONE_TIGHT_LEGACY.get(name, name)
        if key not in ZONE_TIGHTNESS or key == self._zone_tightness:
            return
        self._zone_tightness = key
        self._refresh()

    @property
    def zone_tightness(self) -> str:
        return self._zone_tightness

    def set_zone_color_mode(self, mode: str):
        """★R282-4：分区配色。'跟随分类色'＝一团一色（旧行为）；'统一色'＝全团用
        所选颜色 ⇒ "颜色/模式(主题)" 两个下拉对分区也生效（用户点名要联动）。

        与 `set_zone_tightness` 同款口径：**认档才动**，认不得就整条跳过
        （不改值、不重绘）——坏存档/坏导入不该把分区配色打成半状态。
        只有"分区"那一路真读它（`_style_args`），其余档位改了也只是存着。"""
        if mode in ZONE_COLOR_MODES and mode != self._zone_color_mode:
            self._zone_color_mode = mode
            self._refresh()

    @property
    def zone_color_mode(self) -> str:
        return self._zone_color_mode

    def set_zone_style(self, name: str):
        """★R288：分区「画法」。码值表在 `icon_overlay.ZONE_STYLES`
        （包络框=修前唯一的画法 / 色块纱环 / 荧光笔 / 角贴；★R289 起再追加
        取景角 / 双线框 / 斜纹带 / 柔光晕 四档创新样式；★R293 起追加
        细双线框 / 细柔光晕 两档"细"预设）。

        与 `set_zone_tightness` 同款口径：**认档才动**（不改值、不重绘、不留半个
        状态）——坏存档/坏导入不许把分区画框打成空白。渲染侧
        （`_draw_cluster`）另有一道同款兜底，属性被喂坏值时按默认档画。"""
        from core.icon_overlay import ZONE_STYLES
        if name in ZONE_STYLES and name != self._zone_style:
            self._zone_style = name
            self._refresh()

    @property
    def zone_style(self) -> str:
        return self._zone_style

    def set_pad(self, pad: int):
        """框大小：向外扩展的像素（0-20）"""
        try:
            self._pad = max(0, min(20, int(pad)))
        except (TypeError, ValueError):
            self._pad = 1
        self._refresh()

    def set_shape(self, shape: str):
        """框形状：圆角立体/圆角/直角/双线"""
        if shape in FRAME_SHAPES:
            self._shape = shape
            self._refresh()

    @property
    def shape(self) -> str:
        return self._shape

    def set_bottom_align(self, enabled: bool):
        """同列框底边对齐开关"""
        self._bottom_align = bool(enabled)
        self._last_sig = None
        self._refresh()

    @property
    def bottom_align(self) -> bool:
        return self._bottom_align

    def set_frame_all(self, enabled: bool):
        """复选项：框住桌面上全部图标（默认开；位置没变不重绘，几乎不占资源）"""
        self._frame_all = bool(enabled)
        self._last_sig = None  # 强制下次重绘
        self._refresh()

    def set_gradient_mode(self, enabled: bool):
        """渐变色模式：高亮时每个图标用自身分类色（需配合 category_manager）"""
        self._gradient_mode = bool(enabled)
        self._last_sig = None
        self._refresh()

    def set_color_map(self, color_map: Dict[str, Tuple[int, int, int]]):
        """手动设置每个图标名对应的颜色（来自 highlight_category gradient 模式）"""
        self._color_map = dict(color_map)
        self._last_sig = None
        self._refresh()

    @property
    def frame_all(self) -> bool:
        return self._frame_all

    def apply_theme(self, theme_name: str) -> Optional[dict]:
        """一键应用预设主题，返回主题配置；未知主题返回 None"""
        theme = THEMES.get(theme_name)
        if not theme:
            return None
        r, g, b = _hex_to_rgb(theme['color'])
        if (r, g, b) == (0, 0, 0):
            r, g, b = 1, 1, 1
        self._color = (r, g, b)
        self._width = int(theme['width'])
        self._dashed = bool(theme['dashed'])
        self._pad = int(theme['pad'])
        self._shape = theme.get('shape', '圆角立体')
        self._refresh()
        return theme

    # ---------------------------------------------------------------------
    # 高亮 API（按名称）
    # ---------------------------------------------------------------------

    def highlight_names(self, names: List[str]):
        self._names = list(dict.fromkeys(names))
        self._refresh()

    def highlight_category(self, category_manager, cat_name: str) -> List[str]:
        """高亮一个分类的全部图标，边框用分类自己的颜色"""
        names = category_manager.get_category_icons(cat_name)
        if names:
            try:
                self.set_color_hex(category_manager.get_category_color(cat_name))
            except Exception:
                pass
            self.highlight_names(names)
        return names

    def clear(self):
        self._names = []
        self._refresh()

    # ---------------------------------------------------------------------
    # Overlay 窗口
    # ---------------------------------------------------------------------

    def _listview_rect(self) -> Tuple[int, int, int, int]:
        """桌面物理矩形。直接读取 SysListView32 窗口矩形，避免 DPI 虚拟化问题。"""
        # 先尝试找到桌面图标窗口
        progman = win32gui.FindWindow('Progman', 'Program Manager')
        if progman:
            # 查找 SHELLDLL_DefView 和 SysListView32
            def_view = win32gui.FindWindowEx(progman, 0, 'SHELLDLL_DefView', None)
            if def_view:
                listview = win32gui.FindWindowEx(def_view, 0, 'SysListView32', None)
                if listview:
                    try:
                        # 使用 PMv2 上下文获取物理坐标
                        old_ctx = self._set_thread_pmv2()
                        try:
                            rect = win32gui.GetWindowRect(listview)
                            return rect
                        finally:
                            self._restore_thread_dpi(old_ctx)
                    except Exception:
                        pass

        # 降级：使用虚拟屏幕度量
        try:
            old_ctx = self._set_thread_pmv2()
            try:
                return (win32api.GetSystemMetrics(76),   # SM_XVIRTUALSCREEN
                        win32api.GetSystemMetrics(77),   # SM_YVIRTUALSCREEN
                        win32api.GetSystemMetrics(76)
                        + win32api.GetSystemMetrics(78),  # X + CX
                        win32api.GetSystemMetrics(77)
                        + win32api.GetSystemMetrics(79))  # Y + CY
            finally:
                self._restore_thread_dpi(old_ctx)
        except Exception:
            return (0, 0,
                    win32api.GetSystemMetrics(win32con.SM_CXSCREEN),
                    win32api.GetSystemMetrics(win32con.SM_CYSCREEN))

    def _get_insert_after(self) -> int:
        """（保留备用）桌面根窗口锚点。实测本机 SetWindowPos 不允许插到
        Progman 之后（返回 False），现改用 maintain_zorder 动态校准层级。"""
        lv = self._get_listview()
        try:
            if lv:
                shell = win32gui.GetParent(lv)
                root = win32gui.GetAncestor(shell, 2)  # GA_ROOT
                if root:
                    return root
        except Exception:
            pass
        progman = win32gui.FindWindow('Progman', None)
        return progman or win32con.HWND_BOTTOM

    def _set_thread_pmv2(self):
        """临时把当前线程切成 Per-Monitor V2：overlay 窗口按物理像素定位，
        双屏/异构 DPI 下边框才能与图标逐像素对齐（旧上下文会被 DWM 缩放错位）。"""
        try:
            ctx = ctypes.windll.user32.SetThreadDpiAwarenessContext
            ctx.restype = ctypes.c_void_p
            ctx.argtypes = [ctypes.c_void_p]
            old = ctx(ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2))
            if old:
                return old
        except Exception:
            pass
        return None

    def _restore_thread_dpi(self, old):
        if not old:
            return
        try:
            ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(old))
        except Exception:
            pass

    def _ensure_overlay(self):
        """旧 WM_PAINT overlay 已退役（2026-09-04）。

        渲染全部由 zendesktop overlay（icon_overlay.py）承担。本函数只负责
        销毁历史遗留的旧窗口，不再创建——避免其 WM_PAINT 用过期缓存画框。"""
        try:
            if self._overlay and win32gui.IsWindow(self._overlay):
                win32gui.DestroyWindow(self._overlay)
                IconHighlighter._by_hwnd.pop(self._overlay, None)
                self._overlay = None
        except Exception as e:
            logger.error(f"旧 overlay 销毁失败(已忽略): {e}")

    def _build_category_color_map(self) -> dict:
        """图标名 → 所属分类颜色（多分类取第一个非"其他"）。
        渐变色/分类色模式的颜色来源，每次实时构建不缓存。"""
        color_map = {}
        cm = getattr(self, 'category_manager', None)
        if not cm:
            return color_map
        try:
            for cat in cm.get_all_categories():
                try:
                    r, g, b = _hex_to_rgb(cat.get('color', '#8be9fd'))
                except Exception:
                    continue
                for icon in cat.get('icons', []):
                    if icon not in color_map:
                        color_map[icon] = (r, g, b)
        except Exception as e:
            logger.error(f"构建分类色失败: {e}")
        return color_map

    def _build_category_label_map(self) -> dict:
        """图标名 → 所属分类名（★R273 'zone' 档包络上沿的小牌文字）。

        与 _build_category_color_map 同口径：多分类取第一个，实时构建不缓存。"""
        label_map = {}
        cm = getattr(self, 'category_manager', None)
        if not cm:
            return label_map
        try:
            for cat in cm.get_all_categories():
                name = cat.get('name') or ''
                for icon in cat.get('icons', []):
                    if name and icon not in label_map:
                        label_map[icon] = name
        except Exception as e:
            logger.error(f"构建分类名失败: {e}")
        return label_map

    def _style_args(self, overlay_rects):
        """按当前显示方式备出 (colors, labels) 两份逐图标样式表。

        ★R273：'zone' 档天生按分类分团，所以**渐变色没开时也要拿分类色**，
        否则整屏一个色＝一个巨型框，失去分团意义；labels 只有 zone 用得上。
        ★R282-4：分区新增"配色"档——选'统一色'时不再取分类色（colors 空 ⇒
        渲染侧每团回落 self.color），于是"颜色/模式(主题)"两个下拉对分区也联动；
        默认仍是'跟随分类色'（一团一色，旧行为逐字不丢）。"""
        zone = self._display_mode == 'zone'
        per_color = self._gradient_mode or (zone and self._zone_color_mode != '统一色')
        if not (per_color or zone):
            return None, None
        cmap = self._build_category_color_map() if per_color else {}
        lmap = self._build_category_label_map() if zone else {}
        colors, labels = {}, ({} if lmap else None)
        for idx in overlay_rects:
            n = self._idx_name_cache.get(idx)
            if not n:
                continue
            if n in cmap:
                colors[idx] = cmap[n]
            if labels is not None and n in lmap:
                labels[idx] = lmap[n]
        return (colors or None), labels

    def _tk_screen_width(self) -> int:
        """本进程坐标系里的主屏宽——即 Win32 定位/Tk geometry 所用的口径。"""
        try:
            return int(win32api.GetSystemMetrics(0))
        except Exception:
            return 0

    def _screen_rects(self) -> Dict[int, Tuple[int, int, int, int]]:
        """把 _rect_cache（物理像素，Explorer 侧读数）换算到本进程坐标系。

        ★R219（Bug1 同族）：进程=PMv2 或缩放=100% 时比例恒为 1，逐字节等价
        于旧行为；只有进程被降级为 System/Unaware（老包、manifest 缺位、
        早期 SetProcessDPIAware 抢跑）且缩放≠100% 时才补偿。不补偿的话框
        按缩放倍数偏、离原点越远偏得越多（Win11 125% 用户实测）。
        仅换算**绘制口径**；_position_cache 仍是 Explorer 口径，不能一起换算
        （它要喂给 LVM_SETITEMPOSITION）。"""
        from core.dpi_geom import to_tk_rect
        w = self._tk_screen_width()
        if not w:
            return dict(self._rect_cache)
        return {idx: to_tk_rect(r, w) for idx, r in self._rect_cache.items()}

    def _refresh_zendesktop(self):
        """使用新的 ZenDesktop overlay 直接绘制（不依赖 WM_PAINT）"""
        from core.icon_overlay import IconOverlayWindow

        if not self._rect_cache:
            return

        rects = self._screen_rects()      # ★R219 绘制口径换算

        lv = self._get_listview()
        if not lv:
            return

        # 获取 ListView 的真实位置（可能跨多个显示器）
        # ★R204 CORE-24：句柄被外部销毁时 GetWindowRect 抛异常→sig 滞留
        # 框长期错位（同 R130 给 :527 打的补丁，这两处漏了）
        try:
            lv_rect = win32gui.GetWindowRect(lv)
        except Exception as e:
            logger.debug(f'GetWindowRect 失败(重取句柄): {e}')
            lv = self._get_listview()
            if not lv:
                return
            try:
                lv_rect = win32gui.GetWindowRect(lv)
            except Exception:
                return
        overlay_x, overlay_y, overlay_r, overlay_b = lv_rect
        overlay_w = overlay_r - overlay_x
        overlay_h = overlay_b - overlay_y

        # 计算需要的最大尺寸（考虑所有图标）
        # ★ B2：_read_item_rect 返回 LTRB (l,t,r,b)，右/底边界就是 r[2]/r[3]；
        #   旧式 r[0]+r[2]（左+右）把宽度算成 ~2×，overlay 白开一倍大的窗
        max_x = max(r[2] for r in rects.values()) + 10
        max_y = max(r[3] for r in rects.values()) + 10

        # 确保 overlay 覆盖所有图标
        final_w = max(overlay_w, max_x - overlay_x)
        final_h = max(overlay_h, max_y - overlay_y)

        # 获取或创建 overlay
        if not self._zendesktop_overlay:
            self._zendesktop_overlay = IconOverlayWindow()
        # ★ 每次刷新都同步颜色/线宽/形状/虚线（2026-09-04 修复：颜色换
        #   不了——旧代码只在创建时拷贝一次，之后 set_color_hex 传不进来）
        self._zendesktop_overlay.color = self._color
        self._zendesktop_overlay.pen_width = self._width
        self._zendesktop_overlay.shape = self._shape
        self._zendesktop_overlay.dashed = self._dashed
        self._zendesktop_overlay.display_mode = self._display_mode
        # ★R279-①：并团松紧也走这条同步链——四处站点（两条刷新路径 × 新建/重建）漏一处，
        #   就是"改了档位要等 overlay 重建才见效"那种查半天的坑
        self._zendesktop_overlay.zone_tightness = self._zone_tightness
        # ★R288：分区画法同一条链（四处站点漏一处＝"换了画法要等 overlay 重建才见效"）
        self._zendesktop_overlay.zone_style = self._zone_style

        # 确保窗口大小正确
        if (not self._zendesktop_overlay.hwnd or
                self._zendesktop_overlay.width != final_w or
                self._zendesktop_overlay.height != final_h or
                self._zendesktop_overlay.hwnd and win32gui.GetWindowRect(self._zendesktop_overlay.hwnd) != (overlay_x, overlay_y, overlay_x + final_w, overlay_y + final_h)):
            self._zendesktop_overlay.destroy()
            self._zendesktop_overlay.create(overlay_x, overlay_y, final_w, final_h)

        # 转换 rects 格式，排除隐藏的
        overlay_rects = {}
        for idx, r in rects.items():
            name = self._idx_name_cache.get(idx)
            if name and name in self._hidden_snapshot:
                continue
            # 转换为相对于 overlay 的坐标
            overlay_rects[idx] = (r[0] - overlay_x, r[1] - overlay_y, r[2] - r[0], r[3] - r[1])

        if overlay_rects:
            # 分类色模式：每个图标用所属分类的颜色（"全部"视图一目了然）
            _colors, _labels = self._style_args(overlay_rects)
            _ok = self._zendesktop_overlay.draw_icons(overlay_rects, _colors,
                                                      _labels)
            if _ok is False:
                # ULW 被会话状态拒绝：销毁重建 overlay 窗口再试一次
                # （新窗口表面归零，可绕开被污染的合成状态）
                try:
                    self._zendesktop_overlay.destroy()
                    self._zendesktop_overlay = IconOverlayWindow()
                    self._zendesktop_overlay.color = self._color
                    self._zendesktop_overlay.pen_width = self._width
                    self._zendesktop_overlay.shape = self._shape
                    self._zendesktop_overlay.dashed = self._dashed
                    self._zendesktop_overlay.display_mode = self._display_mode
                    self._zendesktop_overlay.zone_tightness = self._zone_tightness
                    self._zendesktop_overlay.zone_style = self._zone_style
                    self._zendesktop_overlay.create(overlay_x, overlay_y,
                                                    final_w, final_h)
                    _ok = self._zendesktop_overlay.draw_icons(
                        overlay_rects, _colors, _labels)
                except Exception as e:
                    logger.error(f"重建 overlay 失败: {e}")
            self._zendesktop_overlay.show()
            # 数据面留痕：本次实际画框的名字与矩形（框准性排查用）
            try:
                _names = sorted(
                    self._idx_name_cache.get(idx, f'idx{idx}')
                    for idx in overlay_rects)
                log_frame_debug("画框数据", count=len(overlay_rects),
                                 names=','.join(_names[:90]))
            except Exception:
                pass
        else:
            self._zendesktop_overlay.hide()

    def _refresh_zendesktop_names(self):
        """使用新 overlay 高亮指定名称的图标（分类模式）"""
        from core.icon_overlay import IconOverlayWindow

        if not self._rect_cache or not self._names:
            return

        rects = self._screen_rects()      # ★R219 绘制口径换算

        lv = self._get_listview()
        if not lv:
            return

        # 获取 ListView 位置
        # ★R204 CORE-24：同上，包 try 防句柄失效
        try:
            lv_rect = win32gui.GetWindowRect(lv)
        except Exception as e:
            logger.debug(f'GetWindowRect 失败(重取句柄): {e}')
            lv = self._get_listview()
            if not lv:
                return
            try:
                lv_rect = win32gui.GetWindowRect(lv)
            except Exception:
                return
        overlay_x, overlay_y = lv_rect[0], lv_rect[1]

        # 计算需要的最大尺寸（★ B2 同第一处：LTRB 直接取 r[2]/r[3]）
        max_x = max(r[2] for r in rects.values()) + 10
        max_y = max(r[3] for r in rects.values()) + 10
        final_w = max(lv_rect[2] - lv_rect[0], max_x - overlay_x)
        final_h = max(lv_rect[3] - lv_rect[1], max_y - overlay_y)

        # 获取或创建 overlay
        if not self._zendesktop_overlay:
            self._zendesktop_overlay = IconOverlayWindow()
        # ★ 每次刷新都同步颜色/线宽/形状/虚线（2026-09-04 修复：颜色换
        #   不了——旧代码只在创建时拷贝一次，之后 set_color_hex 传不进来）
        self._zendesktop_overlay.color = self._color
        self._zendesktop_overlay.pen_width = self._width
        self._zendesktop_overlay.shape = self._shape
        self._zendesktop_overlay.dashed = self._dashed
        self._zendesktop_overlay.display_mode = self._display_mode
        self._zendesktop_overlay.zone_tightness = self._zone_tightness
        self._zendesktop_overlay.zone_style = self._zone_style   # ★R288

        # 确保窗口大小与位置正确（分辨率/任务栏变化时原点会变）
        # ★ R130：GetWindowRect 包 try——句柄被外部销毁等罕见场景下不再
        #   让整个 _refresh 抛异常（叠加 sig 滞留会导致框长期不更新）
        try:
            cur_rect = win32gui.GetWindowRect(self._zendesktop_overlay.hwnd)
        except Exception:
            cur_rect = None
        if (not self._zendesktop_overlay.hwnd or
                self._zendesktop_overlay.width != final_w or
                self._zendesktop_overlay.height != final_h or
                cur_rect !=
                (overlay_x, overlay_y,
                 overlay_x + final_w, overlay_y + final_h)):
            self._zendesktop_overlay.destroy()
            self._zendesktop_overlay.create(overlay_x, overlay_y, final_w, final_h)

        # 只绘制指定名称的图标
        overlay_rects = {}
        for name in self._names:
            idx = self._name_idx_cache.get(name)
            if idx is None:
                continue
            r = rects.get(idx)
            if not r:
                continue
            overlay_rects[idx] = (r[0] - overlay_x, r[1] - overlay_y, r[2] - r[0], r[3] - r[1])

        if overlay_rects:
            _colors, _labels = self._style_args(overlay_rects)
            _ok = self._zendesktop_overlay.draw_icons(overlay_rects, _colors,
                                                      _labels)
            if _ok is False:
                # ULW 被会话状态拒绝：销毁重建 overlay 窗口再试一次
                # （新窗口表面归零，可绕开被污染的合成状态）
                try:
                    self._zendesktop_overlay.destroy()
                    self._zendesktop_overlay = IconOverlayWindow()
                    self._zendesktop_overlay.color = self._color
                    self._zendesktop_overlay.pen_width = self._width
                    self._zendesktop_overlay.shape = self._shape
                    self._zendesktop_overlay.dashed = self._dashed
                    self._zendesktop_overlay.display_mode = self._display_mode
                    self._zendesktop_overlay.zone_tightness = self._zone_tightness
                    self._zendesktop_overlay.zone_style = self._zone_style
                    self._zendesktop_overlay.create(overlay_x, overlay_y,
                                                    final_w, final_h)
                    _ok = self._zendesktop_overlay.draw_icons(
                        overlay_rects, _colors, _labels)
                except Exception as e:
                    logger.error(f"重建 overlay 失败: {e}")
            self._zendesktop_overlay.show()
            logger.debug(f"_refresh_zendesktop_names: {len(overlay_rects)} 个框, 高亮: {self._names}")
        else:
            self._zendesktop_overlay.hide()

    def _refresh(self):
        if not self._names and not self._frame_all and not getattr(self, '_kids_mode', False):
            if self._overlay and win32gui.IsWindow(self._overlay):
                win32gui.ShowWindow(self._overlay, win32con.SW_HIDE)
            if self._zendesktop_overlay:
                self._zendesktop_overlay.hide()
            return
        # ★ 分类高亮模式优先：切到分类后只画 _names 的框。
        # （2026-09-02 修复：此前 frame_all 分支优先，切换后仍把切换前缓存的
        #   全部矩形画出来——包括已隐藏图标旧位置上的"多余的框"）
        if self._names and self._rect_cache:
            self._refresh_zendesktop_names()
            return
        # ★ R65-Bug4：frame_all 全框兜底退役——"框住分类和常驻"的名单
        # 由 dock 按分类∪常驻精确计算（含未分类全框是旧语义，用户已否决）。
        # 名单为空时宁可无框，也不再全量画框。
        # 缓存未就绪（names/frame_all 有设定但矩形缓存为空）→ 先隐藏，
        # 等监视线程补齐数据后下一轮再画。旧 WM_PAINT overlay 已退役
        # （2026-09-04：它曾按前台显隐并画出过期缓存，造成"焦点在桌面时框多"）
        if self._zendesktop_overlay:
            self._zendesktop_overlay.hide()

    def maintain_zorder(self):
        """旧 overlay 退役守卫（每 2 秒由 gui 轮询调用）。

        2026-09-04：旧 WM_PAINT overlay 退役。此前本函数会在"桌面为前台"时
        把旧 overlay 显示出来，而它画的是过期缓存（隐藏图标的旧网格位），
        叠在新 overlay 的正确框上——用户表现为"焦点在桌面时框变多"。
        显隐职责已全部由 zendesktop overlay（_refresh 系）承担，
        本函数只确保旧窗口永远不再出现。"""
        try:
            if self._overlay and win32gui.IsWindow(self._overlay):
                if win32gui.IsWindowVisible(self._overlay):
                    win32gui.ShowWindow(self._overlay, win32con.SW_HIDE)
        except Exception as e:
            logger.error(f"旧 overlay 隐藏失败(已忽略): {e}")

    def _desktop_exposed(self) -> bool:
        """主屏图标区是否没有可见的普通应用窗口遮挡（最小化/工具窗口/副屏窗口不算）。"""
        own = getattr(self, 'own_tk_hwnd', 0)
        cx = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
        cy = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)
        skip_cls = ('Progman', 'WorkerW', 'Shell_TrayWnd', 'Shell_SecondaryTrayWnd',
                    'IconHighlightOverlayClass', 'DesktopDockClass',
                    'MSCTFIME UI', 'IME', 'Default IME', 'tooltips_class32',
                    'WindowsDashboard', 'ApplicationFrameWindow')
        found = {'covered': False}

        def cb(h, _):
            if not win32gui.IsWindowVisible(h) or win32gui.IsIconic(h):
                return True
            if h == own:
                return True  # 自己的主界面不算遮挡
            cls = win32gui.GetClassName(h)
            if cls in skip_cls:
                return True
            ex = win32gui.GetWindowLong(h, win32con.GWL_EXSTYLE)
            if ex & (win32con.WS_EX_TOOLWINDOW | 0x08000000):  # TOOLWINDOW|NOACTIVATE
                return True
            try:
                cloaked = ctypes.c_uint(0)
                ctypes.windll.dwmapi.DwmGetWindowAttribute(
                    h, 14, ctypes.byref(cloaked), 4)  # DWMWA_CLOAKED
                if cloaked.value:
                    return True  # 被 DWM 遮蔽（如 设置/输入体验 等 UWP 后台窗口）
            except Exception:
                pass
            if not win32gui.GetWindowText(h):
                return True
            try:
                l, t, r, b = win32gui.GetWindowRect(h)
                if r <= 0 or b <= 0 or l >= cx or t >= cy:
                    return True  # 与主屏图标区无交集（如在副屏）
            except Exception:
                return True
            found['covered'] = True
            return False

        try:
            win32gui.EnumWindows(cb, None)
        except Exception:
            pass
        return not found['covered']

    # ---------------------------------------------------------------------
    # 绘制
    # ---------------------------------------------------------------------

    @staticmethod
    def _calc_inset(l: int, t: int, r: int, b: int) -> Tuple[int, int, int, int]:
        """动态计算内缩矩形：根据图块尺寸按比例内缩，
        保证框既紧贴图块又不与相邻框相连。
        - 水平内缩 = max(2, 图块宽度 * 2%)  → 小图标多留点空间防连
        - 垂直内缩 = max(1, 图块高度 * 1.5%) → 文字标签底部留白
        这样长标签图标的框不会从右边/底边吞掉文字。"""
        w, h = r - l, b - t
        inset_x = max(2, int(w * 0.02))
        inset_y = max(1, int(h * 0.015))
        return (l + inset_x, t + inset_y, r - inset_x, b - inset_y)

    def _draw_frame(self, hdc, l: int, t: int, r: int, b: int, pen: int,
                    skip_edges: frozenset = frozenset()):
        """按形状绘制独立边框。内缩量随图块尺寸动态计算，
        小图标留足够间隙防相连，大图标（长标签）不吞掉文字。

        skip_edges：应跳过的边集合，元素为 'top'/'bottom'/'left'/'right'。
        用于与相邻框共享公共边时避免线条重复绘制。"""
        gdi32 = ctypes.windll.gdi32
        l, t, r, b = self._calc_inset(l, t, r, b)
        shape = self._shape

        if shape == '直角':
            win32gui.SelectObject(hdc, pen)
            # 逐边绘制，跳过共享边
            if 'top' not in skip_edges:
                win32gui.MoveToEx(hdc, l, t)
                win32gui.LineTo(hdc, r, t)
            if 'bottom' not in skip_edges:
                win32gui.MoveToEx(hdc, l, b)
                win32gui.LineTo(hdc, r, b)
            if 'left' not in skip_edges:
                win32gui.MoveToEx(hdc, l, t)
                win32gui.LineTo(hdc, l, b)
            if 'right' not in skip_edges:
                win32gui.MoveToEx(hdc, r, t)
                win32gui.LineTo(hdc, r, b)
            return

        if shape == '下划线':
            win32gui.SelectObject(hdc, pen)
            if 'bottom' not in skip_edges:
                win32gui.MoveToEx(hdc, l, b)
                win32gui.LineTo(hdc, r, b)
            return

        if shape == 'U形':
            win32gui.SelectObject(hdc, pen)
            win32gui.MoveToEx(hdc, l, t)
            win32gui.LineTo(hdc, l, b)
            if 'bottom' not in skip_edges:
                win32gui.LineTo(hdc, r, b)
            win32gui.LineTo(hdc, r, t)
            return

        if shape == '四角':
            win32gui.SelectObject(hdc, pen)
            arm = max(8, min(22, (r - l) // 5, (b - t) // 5))
            win32gui.MoveToEx(hdc, l, t + arm)
            win32gui.LineTo(hdc, l, t)
            win32gui.LineTo(hdc, l + arm, t)
            win32gui.MoveToEx(hdc, r - arm, t)
            win32gui.LineTo(hdc, r, t)
            win32gui.LineTo(hdc, r, t + arm)
            win32gui.MoveToEx(hdc, r, b - arm)
            win32gui.LineTo(hdc, r, b)
            win32gui.LineTo(hdc, r - arm, b)
            win32gui.MoveToEx(hdc, l + arm, b)
            win32gui.LineTo(hdc, l, b)
            win32gui.LineTo(hdc, l, b - arm)
            return

        if shape == '双线':
            win32gui.SelectObject(hdc, pen)
            gdi32.RoundRect(hdc, l, t, r, b, 18, 18)
            inset2 = 3 + self._width
            gdi32.RoundRect(hdc, l + inset2, t + inset2, r - inset2, b - inset2, 12, 12)
            return

        if shape == '圆角':
            win32gui.SelectObject(hdc, pen)
            gdi32.RoundRect(hdc, l, t, r, b, 14, 14)
            return

        # 默认：圆角立体（右下角深色投影，偏移 2 在内缩范围内；
        # 避免纯黑被 colorkey 透明化）
        sr = max(3, int(self._color[0] * 0.35))
        sg = max(3, int(self._color[1] * 0.35))
        sb = max(3, int(self._color[2] * 0.35))
        shadow_pen = win32gui.CreatePen(
            win32con.PS_SOLID, max(1, self._width - 1),
            (sb << 16) | (sg << 8) | sr)
        old = win32gui.SelectObject(hdc, shadow_pen)
        # ★R335-a（批88，挂账 §四十 item1）：`RoundRect` 抛异常时旧句柄没还原、
        # shadow_pen 也永不删除——高亮每帧都走这一支，一次失败就漏一枚 GDI 句柄，
        # 攒到桌面画不出框才看得见。与 ★R334-a 那批 ctypes 原型同一口径：
        # **建了就要在 finally 里删**，成功路径也不例外。
        try:
            gdi32.RoundRect(hdc, l + 2, t + 2, r + 2, b + 2, 14, 14)
        finally:
            win32gui.SelectObject(hdc, old)
            win32gui.DeleteObject(shadow_pen)
        win32gui.SelectObject(hdc, pen)
        gdi32.RoundRect(hdc, l, t, r, b, 14, 14)

    def _create_pen_pair(self, hdc, colorref: int, is_dashed: bool):
        """创建主边框 pen + 投影用 solid pen，返回 (pen, solid_pen, old_solid)。

        old_solid 是 solid_pen 被选入 hdc 前的旧对象句柄，供 cleanup 还原。
        """
        gdi32 = ctypes.windll.gdi32
        pw = max(1, self._width)
        style = win32con.PS_DASH if is_dashed else win32con.PS_SOLID
        pen = win32gui.CreatePen(style, pw, colorref)
        # 投影 pen：右下角深色（与 _draw_frame 立体效果一致）
        sr = max(3, int(self._color[0] * 0.35))
        sg = max(3, int(self._color[1] * 0.35))
        sb = max(3, int(self._color[2] * 0.35))
        solid_pen = win32gui.CreatePen(win32con.PS_SOLID, max(1, pw - 1),
                                        (sb << 16) | (sg << 8) | sr)
        old_solid = win32gui.SelectObject(hdc, solid_pen)
        return pen, solid_pen, old_solid

    @staticmethod
    def _cleanup_pen_pair(hdc, pen, solid_pen, old_solid):
        """还原 solid_pen 的选中状态并删除两个 pen。"""
        win32gui.SelectObject(hdc, old_solid)
        win32gui.DeleteObject(pen)
        win32gui.DeleteObject(solid_pen)

    @staticmethod
    def _align_columns_bottom(rects: Dict[int, Tuple[int, int, int, int]]
                              ) -> Dict[int, Tuple[int, int, int, int]]:
        """同一竖列的框底边对齐到该列最深者（图标文字行数不同导致底边参差）。

        ★ 修改：只对齐垂直紧贴的图标（间隙 = 0），有间隙的不处理。
        这样每个图标保持独立，只有真正相邻的才共享底边。
        """
        # 按左边缘分组列
        cols: List[dict] = []   # {'left': x, 'icons': [(idx, l, t, r, b)]}
        for idx, (l, t, r, b) in rects.items():
            placed = False
            for col in cols:
                if abs(col['left'] - l) <= 24:
                    col['icons'].append((idx, l, t, r, b))
                    placed = True
                    break
            if not placed:
                cols.append({'left': l, 'icons': [(idx, l, t, r, b)]})

        # 对每列进行底边对齐
        out: Dict[int, Tuple[int, int, int, int]] = dict(rects)
        for col in cols:
            icons = col['icons']
            if len(icons) < 2:
                continue

            # 按顶部坐标排序
            icons.sort(key=lambda x: x[2])

            # 找出垂直紧贴的组（间隙 = 0 视为紧贴）
            groups: List[list] = [[icons[0]]]
            for i in range(1, len(icons)):
                prev_b = groups[-1][-1][4]  # 上一组最后一个图标的底边
                curr_t = icons[i][2]         # 当前图标的顶边
                gap = curr_t - prev_b
                if gap == 0:  # 只有紧贴才分组
                    groups[-1].append(icons[i])
                else:
                    groups.append([icons[i]])

            # 对每个紧贴组进行底边对齐
            for group in groups:
                if len(group) >= 2:
                    max_b = max(b for _, _, _, _, b in group)
                    for idx, l, t, r, _ in group:
                        out[idx] = (l, t, r, max_b)

        return out

    @staticmethod
    def _align_rows_bottom(rects: Dict[int, Tuple[int, int, int, int]]
                           ) -> Dict[int, Tuple[int, int, int, int]]:
        """同一横排的框底边对齐到该排最深者（图标文字行数不同导致底边参差）。

        ★ 新增：对水平相邻的图标（间隙 < 5px）进行底边对齐。
        这样同一行的图标即使高度不同，底边也会对齐。
        """
        # 按顶边缘分组行
        rows: List[dict] = []   # {'top': y, 'icons': [(idx, l, t, r, b)]}
        for idx, (l, t, r, b) in rects.items():
            placed = False
            for row in rows:
                if abs(row['top'] - t) <= 5:  # 顶边相近视为同一行
                    row['icons'].append((idx, l, t, r, b))
                    placed = True
                    break
            if not placed:
                rows.append({'top': t, 'icons': [(idx, l, t, r, b)]})

        # 对每行进行底边对齐
        out: Dict[int, Tuple[int, int, int, int]] = dict(rects)
        for row in rows:
            icons = row['icons']
            if len(icons) < 2:
                continue

            # 按左边缘排序
            icons.sort(key=lambda x: x[1])

            # 找出水平相邻的组（间隙 < 5px 视为相邻）
            groups: List[list] = [[icons[0]]]
            for i in range(1, len(icons)):
                prev_r = groups[-1][-1][3]  # 上一组最后一个图标的右边
                curr_l = icons[i][1]         # 当前图标的左边
                gap = curr_l - prev_r
                if gap < 5:  # 间隙小于5px视为相邻
                    groups[-1].append(icons[i])
                else:
                    groups.append([icons[i]])

            # 对每个相邻组进行底边对齐
            for group in groups:
                if len(group) >= 2:
                    max_b = max(b for _, _, _, _, b in group)
                    for idx, l, t, r, _ in group:
                        out[idx] = (l, t, r, max_b)

        return out

    @staticmethod
    def _build_shared_edge_mask(rects: Dict[int, Tuple[int, int, int, int]],
                                visible: set) -> set:
        """构建「应跳过的边」集合，避免相邻框的公共边被重复绘制。

        ★ 修改：只有真正紧贴的图标才共享边（gap=0），有间隙的不共享。
        规则：
        - 底边共享：rect A 的底边 == rect B 的顶边（精确相等），且水平重叠 ≥1px
        - 竖边共享：rect A 的右边 == rect B 的左边（精确相等），且垂直重叠 ≥1px
        这样有间隙的图标不会共享边，每个框都是独立的。
        """
        items = [(idx, r) for idx, r in rects.items() if idx in visible]
        skip: set = set()
        for i, (idx_a, ra) in enumerate(items):
            la, ta, ra_r, ba = ra
            for idx_b, rb in items[i + 1:]:
                lb, tb, rb_r, bb = rb
                # 底边共享：A 的下边 == B 的上边（精确相等），水平范围有交集
                if ba == tb and max(0, min(ra_r, rb_r) - max(la, lb)) >= 1:
                    skip.add((idx_a, 'bottom'))
                    skip.add((idx_b, 'top'))
                # 竖边共享：A 的右边 == B 的左边（精确相等），垂直范围有交集
                if ra_r == lb and max(0, min(ba, bb) - max(ta, tb)) >= 1:
                    skip.add((idx_a, 'right'))
                    skip.add((idx_b, 'left'))
        return skip

    def _paint(self, hwnd):
        """纯 GDI 绘制，只读缓存。

        健壮性教训：此前在 WM_PAINT（Tk 消息泵派发）里做 ~600 次跨进程
        SendMessage（读矩形+名字），属于"消息处理内重活"，与 dock 点击闪退
        同类风险。现在跨进程读取全部在 2 秒轮询/refresh（Tcl 定时器上下文）
        中完成并写入缓存，WM_PAINT 零跨进程调用。"""
        hdc, ps = win32gui.BeginPaint(hwnd)
        try:
            null_brush = win32gui.GetStockObject(win32con.NULL_BRUSH)
            old_brush = win32gui.SelectObject(hdc, null_brush)

            is_dashed = self._dashed and self._width <= 1
            ov_l, ov_t, ov_r, ov_b = win32gui.GetWindowRect(hwnd)
            rects = self._rect_cache
            idx_name = self._idx_name_cache

            log_frame_debug("_paint 开始",
                             frame_all=self._frame_all,
                             names_count=len(self._names),
                             rects_count=len(rects),
                             overlay_rect=f"({ov_l},{ov_t},{ov_r},{ov_b})")

            # 模式一：框住全部图标（★ R65-Bug4：名单优先——有名单时只按
            # 名单画，不再叠加全框，否则"框住分类和常驻"一开就是全部图标）
            if self._frame_all and not self._names:
                pen, solid_pen, old_solid = self._create_pen_pair(hdc, self._colorref(), is_dashed)
                try:
                    old_pen = win32gui.SelectObject(hdc, pen)
                    # 收集可见图标（排除被隐藏的）
                    visible = {idx for idx, _ in rects.items()
                               if idx_name.get(idx) not in self._hidden_snapshot}
                    if visible:
                        shared = self._build_shared_edge_mask(rects, visible)
                        log_frame_debug("共享边检测",
                                         visible=len(visible),
                                         shared_edges=len(shared))
                    else:
                        shared = set()
                        log_frame_debug("无可见图标，跳过绘制")

                    drawn_count = 0
                    skipped_count = 0
                    for idx, (l, t, r, b) in rects.items():
                        name = idx_name.get(idx)
                        if name is not None and name in self._hidden_snapshot:
                            skipped_count += 1
                            continue
                        if r <= ov_l + 4 or b <= ov_t + 4 \
                                or l >= ov_r - 4 or t >= ov_b - 4:
                            skipped_count += 1
                            continue
                        # 只有 '直角' 形状才使用逐边绘制（跳过共享边）
                        # 其他形状（双线/圆角/立体）直接用完整矩形，不受共享边影响
                        if self._shape == '直角':
                            self._draw_frame(hdc, l, t, r, b, pen,
                                             skip_edges=shared.get(idx, frozenset()))
                        else:
                            self._draw_frame(hdc, l, t, r, b, pen)
                        drawn_count += 1

                    log_frame_debug("_paint 完成",
                                     drawn=drawn_count,
                                     skipped=skipped_count,
                                     shape=self._shape)
                finally:
                    win32gui.SelectObject(hdc, old_pen)
                    self._cleanup_pen_pair(hdc, pen, solid_pen, old_solid)

            # 模式二：高亮指定名称（可叠加，支持渐变色）
            if self._names:
                for name in self._names:
                    color = self._color_map.get(name, self._color)
                    colorref = (color[2] << 16) | (color[1] << 8) | color[0]
                    pen, solid_pen, old_solid = self._create_pen_pair(hdc, colorref, is_dashed)
                    try:
                        old_pen = win32gui.SelectObject(hdc, pen)
                        idx = self._name_idx_cache.get(name)
                        rect = self._rect_cache.get(idx) if idx is not None else None
                        if not rect:
                            continue
                        l, t, r, b = rect
                        self._draw_frame(hdc, l, t, r, b, pen)
                    finally:
                        win32gui.SelectObject(hdc, old_pen)
                        self._cleanup_pen_pair(hdc, pen, solid_pen, old_solid)

            win32gui.SelectObject(hdc, old_brush)
        finally:
            win32gui.EndPaint(hwnd, ps)



    # ---------------------------------------------------------------------
    # 低资源刷新：位置签名比对，没变就不重绘
    # ---------------------------------------------------------------------

    def _read_position(self, lv, proc, buf, idx):
        if send_msg_timeout(lv, LVM_GETITEMPOSITION, idx, buf) is None:
            return (0, 0)   # 超时：远端缓冲不可信（审查修复）
        data = win32process.ReadProcessMemory(proc, buf, 8)
        x, y = struct.unpack("ii", data)
        return x, y

    def _read_item_rect(self, lv, proc, buf, idx,
                        flag: int = 1) -> Tuple[int, int, int, int]:
        """LVM_GETITEMRECT：flag=1(LVIR_ICON) 图标图片的紧贴矩形，
        flag=0(LVIR_BOUNDS) 含文字标签的整块矩形。
        旧实现用"位置 + 假设尺寸"推算，边框不贴图标且参差，实测弃用。"""
        win32process.WriteProcessMemory(proc, buf, struct.pack("<4l", flag, 0, 0, 0))
        if send_msg_timeout(lv, LVM_GETITEMRECT, idx, buf) is None:
            return (0, 0, 0, 0)   # 超时：远端缓冲不可信（审查修复）
        l, t, r, b = struct.unpack("<4l", win32process.ReadProcessMemory(proc, buf, 16))
        return (l, t, r, b)

    def refresh_if_changed(self) -> bool:
        """（兼容保留）手动触发一次数据重建。常规刷新走后台监视线程。"""
        return self.collect_now()

    def collect_now(self) -> bool:
        """在调用线程立即重建绘制数据（生成方框按钮用，一次性线程亦可）。"""
        if not self._frame_all and not self._names and not self._kids_mode:
            return False
        try:
            data = self._collect_paint_data()
        except Exception as e:
            logger.error(f"读取图标数据失败: {e}")
            return False
        # ★ R105：与监控路径同口径——底部对齐后再入缓存（此前 collect_now
        # 入的是未对齐原始矩形，即时重绘与 2s 后监控路径画的不一致）
        try:
            if self._bottom_align:
                rects = self._align_columns_bottom(data[0])
                rects = self._align_rows_bottom(rects)
                data = (rects, data[1], data[2], data[3])
        except Exception:
            pass
        self._apply_paint_data(data)
        return True

    def force_redraw(self):
        """★ B11：UI 线程立即按当前缓存重绘一次。

        collect_now 只重建数据不触发重绘（重绘靠监视线程下一轮 2s），
        "生成方框"按钮需要当场可见，故补这个入口。"""
        self._refresh()

    # ---------------------------------------------------------------------
    # 后台监视线程：所有跨进程读取都在这里，绝不占用 UI 线程
    # ---------------------------------------------------------------------

    def start_monitor(self, interval: float = 2.0):
        # ★R204 CORE-23：防重入——重复 start 会产生两个监视循环（双份
        # 跨进程读 + 双份 check_guard 拉回=图标被反复拉扯）；对齐 dock
        # start_icon_capture 的 R130 范式
        t0 = getattr(self, '_mon_thread', None)
        if t0 is not None and t0.is_alive():
            if not self._mon_stop:
                self._mon_interval = interval
                return   # ★真重入：监视已在跑
            # ★R207 NEW3-C13：线程在退场途中（stop 已置位）——等它退出，
            # 否则下面把 _mon_stop 清了它也不看，旧线程带着新队列继续跑
            t0.join(timeout=3.0)
            # ★R210（NEW4-C8）：超时后必须复查——旧线程还活着就清标志
            # 重启=双监视复活（双份跨进程读+双份拉回）。宁可不重启。
            if t0.is_alive():
                logger.error('高亮监视旧线程 3s 未退场，本轮不重启'
                             '（防双监视）')
                return
        self._mon_interval = interval
        self._mon_stop = False
        self._force_refresh = True
        self._mon_queue: 'queue.Queue' = queue.Queue()
        t = threading.Thread(target=self._monitor_loop, daemon=True,
                             name='IconMonitor')
        self._mon_thread = t
        t.start()

    def stop_monitor(self):
        self._mon_stop = True
        # ★R204 CORE-23：stop 等 join——否则仍有一整轮在跑（含跨进程读）
        # ★R270-R1：空闲降频的那一拍是按 _mon_interval 切片睡的（见 _sleep_step），
        # 所以"最长多久醒来看 _mon_stop"没变，join 预算不用跟着放大。
        t = getattr(self, '_mon_thread', None)
        if t is not None and t.is_alive()                 and t is not threading.current_thread():
            t.join(timeout=self._mon_interval + 1.0)

    def _sleep_step(self, seconds: float):
        """睡满 seconds，但按 _mon_interval 切片——降频不许把 stop() 拖长。"""
        deadline = time.monotonic() + seconds
        while not self._mon_stop and time.monotonic() < deadline:
            time.sleep(min(self._mon_interval,
                           max(0.02, deadline - time.monotonic())))

    # ------------------------------------------------------------------
    # ★R272（批25）：两个纯函数 + 三态状态机（密集窗口 / 窗口外递减 / 睡死）
    # ------------------------------------------------------------------

    @classmethod
    def _win_interval(cls, elapsed: float, win_sec: float) -> float:
        """密集窗口内的节拍：从 _BASE_SEC 线性慢爬到 _WIN_END_SEC（纯函数）。

        用户 2026-09-29 01 批 item1「三分钟也可以递减，不过递减的慢点。这样
        负载更小」⇒ 180 s 窗口内 90 拍/2,790 次跨进程降到 45 拍/1,395 次(-50%)。
        慢爬不损失体验的原因：爬升只发生在"图标没在动"的时候——签名一变、或
        用户点/拖一下，窗口起点立刻重置、下一拍回到 _BASE_SEC。
        """
        if win_sec <= 0:
            return cls._BASE_SEC
        r = min(1.0, max(0.0, elapsed) / float(win_sec))
        return cls._BASE_SEC + (cls._WIN_END_SEC - cls._BASE_SEC) * r

    @classmethod
    def _decay_interval(cls, k: int) -> float:
        """窗口外的平方快递减（纯函数）：k=1→42s、2→162s、3→362s、4→642s。"""
        return cls._BASE_SEC + cls._DECAY_K * k * k

    def _win_sec(self) -> float:
        """当前密集窗口长度（秒）。唯一来源是 set_watch_window 注入的值。"""
        v = self._win_sec_cfg
        return float(self._WIN_SEC_DEFAULT) if v is None else float(v)

    @classmethod
    def clamp_watch_window(cls, seconds):
        """纯函数：把任意输入夹成"真正生效"的窗口秒数。

        返回 `(秒数, 原因)`，原因 `''` = 原样可用、`'clamped'` = 超出值域被夹、
        其余（`'unreadable'/'illegal'`）= 回落缺省。做成纯函数是为了让调用方
        能**先算出终值再落盘**（★R226 顺序纪律：先落盘再应用），不必为了拿
        终值先把运行时状态改掉。
        """
        try:
            v = float(seconds)
        except (TypeError, ValueError):
            return float(cls._WIN_SEC_DEFAULT), 'unreadable'
        if v != v or v <= 0:      # NaN / 0 / 负数
            return float(cls._WIN_SEC_DEFAULT), 'illegal'
        c = min(cls._WIN_SEC_MAX, max(cls._WIN_SEC_MIN, v))
        return c, ('' if c == v else 'clamped')

    def set_watch_window(self, seconds) -> float:
        """★R272：注入"图标监视时长"，返回**真正生效**的秒数。

        三条不可用路径各留一行 warning（★R230b 口径：兜底可以吞异常，但必须
        留痕，否则用户报"我改了没生效"时下一轮只能重猜）。app_settings 会随
        "导入全部数据"从外部 JSON 进来，属外部输入 ⇒ 夹取不能省。
        """
        eff, why = self.clamp_watch_window(seconds)
        if why:
            logger.warning(
                '图标监视时长 %r 不可用（%s），%s为 %g 秒（允许 %g~%g 秒）'
                % (seconds, why,
                   '已夹取' if why == 'clamped' else '回落缺省',
                   eff, self._WIN_SEC_MIN, self._WIN_SEC_MAX))
        self._win_sec_cfg = eff
        return eff

    def _idle_factor(self) -> float:
        """★R270-R1 的空闲降频在 ★R272 里只管**心跳层**这一档。

        密集窗口内不许降频（那一档要的正是"扳机一到 ≤2 s 贴准"）；而永远醒着的
        心跳在人离开时也没必要 2 s 一拍。读不到空闲时长按 1.0（宁多醒不漏看）。
        """
        try:
            if _idle_seconds() >= self._IDLE_AFTER_SEC:
                return float(self._IDLE_FACTOR)
        except Exception as e:
            logger.debug(f"空闲时长读取失败，按快节奏: {e}")
        return 1.0

    def _arm_window(self):
        """扳机：重进密集窗口（窗口起点=现在、递减档归零、退出睡死）。"""
        self._win_start = time.monotonic()
        self._decay_k = 0
        self._stable = 0
        self._recollect_next_at = None   # 重进窗口 ⇒ 旧的窗口外 deadline 作废
        if self._dormant:
            self._dormant = False
            # 休眠期攒下的位置变化按"新基线"收下：绝不拿休眠前的旧快照去比，
            # 否则用户睡着那阵子自己挪的图标会被判成"瞬间大面积跳变"、被守护
            # 拽回旧位（★R234 定案：用户摆过要让位，宁可漏纠不可错纠）。
            im = self.icon_manager
            if im is not None and hasattr(im, 'rebase_guard'):
                try:
                    im.rebase_guard()
                except Exception as e:
                    logger.debug(f'守护基线重建失败(已忽略): {e}')
            logger.info('图标监视已从休眠唤醒，重进密集窗口')

    def _advance_decay(self):
        """这一拍"图标没动"⇒ 推进递减，够远就睡死（唯一改 _decay_k 的地方）。"""
        if self._force_refresh or time.time() < self._recollect_until:
            return            # 重采窗口期内不参与递减判定
        # ★R272-S6 修法1：还在密集窗口里就不推进 k。窗口内的"递减得慢点"由
        # _win_interval 那 2→6 s 爬坡承担（用户 01 批 item1 要的就是这一档）；
        # 旧写法每拍都加 k，180 s 窗口里几十拍会把 k 顶到 4（2+40k²=642 ≥
        # _DORMANT_SEC）⇒ 十几秒就睡死，可配窗口长度成了死旋钮。
        if time.monotonic() - self._win_start < self._win_sec():
            return
        if self._decay_k == 0:
            # 用户口径「后来客户不动图标了，就是图标稳定了，就递减呗」⇒
            # 进递减之前要连续 _STABLE_ROUNDS 轮一致；进去之后每轮升一档
            # （要的是"快速递减"，不是每一档都再等两轮）。
            self._stable += 1
            if self._stable < self._STABLE_ROUNDS:
                return
            self._stable = 0
        self._decay_k += 1
        iv = self._decay_interval(self._decay_k)
        if iv >= self._DORMANT_SEC:
            # ★用户 2026-09-29 01 批 item2「半个小时动的图标不用管，需要自己去
            # 点下刷新或者，切图标就行」⇒ 不要兜底巡视，直接睡死。
            self._dormant = True
            logger.info('图标监视已休眠（递减到 %ds ≥ %ds）；'
                        '点刷新/切分类/拖图标即唤醒'
                        % (int(iv), int(self._DORMANT_SEC)))

    def _note_stability(self, sig):
        """重采层跑完一拍回报签名，由它决定"重新密集"还是"继续退"。"""
        if sig is None:
            return            # 读不到 = 不动状态（防"一次读失败就狂重采"，也防误睡死）
        if self._force_refresh or time.time() < self._recollect_until:
            return
        if sig != self._last_sig:
            self._arm_window()
            return
        self._advance_decay()
        if not self._dormant:
            # ★R272-S6：窗口外"这一拍没动"⇒ 下一档重采排在递减后的那个间隔之后。
            # 只有睡死态由 _monitor_step 按心跳节拍走，所以这里不动 deadline。
            self._recollect_next_at = (time.monotonic()
                                        + self._decay_interval(self._decay_k))

    def _probe_beat(self) -> bool:
        """★R272 心跳层：只调本地 user32，**一次跨进程都不发**。

        返回 True = 这一拍探到扳机（桌面视图重建 / 桌面点按抬起）。
        读不到一律返回 False——没证据不下结论：宁可漏一次扳机（下一拍还会再看，
        且"我们自己动作"和"用户刷新"那两条路照样唤醒），也不许自己乱叫造成
        无谓重采。实测 40 拍被动观察误判 0/40（tmp/r270/probe_heartbeat_c.txt）。
        """
        fired = False
        try:
            progman = win32gui.FindWindow('Progman', None)
            shell = (win32gui.FindWindowEx(progman, 0, 'SHELLDLL_DefView', None)
                     if progman else 0)
            lv = (win32gui.FindWindowEx(shell, 0, 'SysListView32', None)
                  if shell else 0)
            style = win32gui.GetWindowLong(lv, win32con.GWL_STYLE) if lv else 0
            snap = (shell, lv, style)
            prev, self._probe_last = self._probe_last, snap
            # 视图重建：右键刷新/F5 会让 Explorer 重建 DefView→SysListView32
            # （句柄变），或改"自动排列/排序方式"（样式位 LVS_AUTOARRANGE 变）。
            if prev is not None and prev != snap and snap[1]:
                fired = True
            # 桌面点按：左键出现"按下→抬起"的边，且抬起时光标在桌面矩形内。
            # 拖动必带点按 ⇒ "用户自己挪图标"这条被覆盖，不依赖他去按刷新。
            down = bool(win32api.GetAsyncKeyState(win32con.VK_LBUTTON) & 0x8000)
            if down:
                self._lbtn_down = True
            elif self._lbtn_down:
                self._lbtn_down = False
                if lv and self._cursor_on_desktop(lv):
                    fired = True
        except Exception as e:
            logger.debug(f'心跳探测失败(已忽略，本拍不触发): {e}')
            return False
        return fired

    @staticmethod
    def _cursor_on_desktop(lv) -> bool:
        """光标是否落在桌面 ListView 的屏幕矩形内（纯本地 API，判不了就 False）。"""
        try:
            x, y = win32api.GetCursorPos()
            l, t, r, b = win32gui.GetWindowRect(lv)
            return l <= x <= r and t <= y <= b
        except Exception:
            return False

    def _monitor_step(self):
        """本轮该睡多久 + 这一拍要不要重采（**唯一决策点**）。

        副作用：写 `self._recollect_due`，`_monitor_loop` 读它决定"这一拍到底
        读不读图标"——睡死态恒 False，一拍跨进程都不发（守护 check_guard 也
        属于重采层，同样不跑；醒来第一拍会重建基线，见 _arm_window）。

        这个调用点在 _monitor_loop 的 try 外面（就在 sleep 前面），所以它自己
        必须吞掉一切异常：抛出去线程就死了，框从此不再跟随桌面——比降频失败
        严重得多。出任何岔子一律回落"快节奏 + 要重采"（宁多读不漏看）。
        """
        base = self._mon_interval
        self._recollect_due = True
        try:
            if self._probe_beat():
                self._arm_window()
                self._force_refresh = True
            # 重采窗口（点一次悬浮栏就要框准）和显式要求重绘时不降频
            if self._force_refresh or time.time() < self._recollect_until:
                self._arm_window()
                return base
            if self._dormant:
                self._recollect_due = False
                return self._PROBE_BEAT_SEC * self._idle_factor()
            elapsed = time.monotonic() - self._win_start
            w = self._win_sec()
            if elapsed < w:
                return self._win_interval(elapsed, w)
            # ★R272-S6 修法2：窗口外**心跳照旧每拍都醒**（本地 user32，实测
            # 15.86 µs/拍），只有"重采"按平方递减用 deadline 卡住。旧写法把
            # max(心跳, 递减档) 当睡眠时长 ⇒ 退到 362 s 那一档时，用户在桌面
            # 点一下要等 6 分钟才被看见，与"心跳层永远醒着"（本文件 ★R272
            # 设计注释）和交付口径"点一下左键即唤醒"相反。
            hb = self._PROBE_BEAT_SEC * self._idle_factor()
            now = time.monotonic()
            if self._recollect_next_at is None or now >= self._recollect_next_at:
                # 时刻先清空：下一档排在哪由 _note_stability 在这一拍重采**之后**
                # 再定（那时才知道 k 升到了第几档）。
                self._recollect_next_at = None
                return hb
            self._recollect_due = False
            return min(hb, self._recollect_next_at - now)
        except Exception as e:
            logger.debug(f"节拍决策失败，回落快节奏: {e}")
        self._recollect_due = True
        return base

    def _monitor_loop(self):
        while not self._mon_stop:
            self._sleep_step(self._monitor_step())
            if self._mon_stop:
                return
            # ★R272 睡死态：这一拍只跑了心跳（零跨进程），图标一次都不读
            if not self._recollect_due:
                continue
            try:
                # 布局守护检测也在后台线程（含跨进程，绝不占 UI 线程）
                if self.icon_manager and hasattr(self.icon_manager, 'check_guard'):
                    self.icon_manager.check_guard()
                    # 守护可能刚移动了图标，等 ListView 刷新一下再读位置
                    time.sleep(0.15)
                if not self._frame_all and not self._names and not self._kids_mode:
                    # 没东西可框 ⇒ 这一拍按"稳定"推进递减，别永远卡在窗口里。
                    # ★R285-N3（P2）：这一枪必须在这里消费掉——下面两个清除点都在
                    # 采集路径里，而默认无框配置（R66 定案）压根走不到那儿 ⇒
                    # `_force_refresh` 永不清除：`_monitor_step` 每拍走"重采窗口"
                    # 分支返回 base（2 s），`_advance_decay`/`_note_stability` 开头
                    # 又都 `if self._force_refresh: return` ⇒ ★R270/★R272 的平方递减
                    # 与睡死**在默认配置下全部失效**（就是那批"用久了鼠标变卡"的病）。
                    # 必须清在 `_advance_decay()` **之前**，否则这一拍仍被它挡回去。
                    self._force_refresh = False
                    self._advance_decay()
                    continue
                sig = self._positions_signature()
                self._note_stability(sig)
                # ★ R105 重采窗口：签名基于位置缓存，看不见 Explorer 迟到的
                # 图标重排；窗口期内强制重采，实测坐标有漂移才重绘
                if (sig == self._last_sig and not self._force_refresh
                        and time.time() >= self._recollect_until):
                    continue
                data = self._collect_paint_data()
                if (sig == self._last_sig and not self._force_refresh
                        and data[0] == getattr(self, '_last_raw', None)):
                    self._force_refresh = False
                    continue   # 窗口期内无漂移：跳过重绘（防闪烁）
                self._last_raw = data[0]
                self._mon_queue.put((sig, data))
                self._force_refresh = False
                logger.debug(f"监视线程: sig更新, rects={len(data[0]) if data else 0}")
            except Exception as e:
                logger.error(f"图标监视线程异常(已忽略): {e}")

    def poll_monitor(self):
        """UI 线程每 2 秒调用：取走监视线程的最新数据（无则 None）"""
        data = None
        try:
            while True:
                data = self._mon_queue.get_nowait()
        except queue.Empty:
            pass
        return data

    def apply_monitor_data(self, item):
        """把监视线程的数据应用到缓存并触发重绘（UI 线程调用，无跨进程）"""
        sig, data = item
        rects, idx_name, name_idx, hidden = data

        log_frame_debug("开始应用数据",
                         total_icons=len(rects),
                         frame_all=self._frame_all,
                         names_count=len(self._names))

        # ★ 应用底部对齐：同一列的框底边对齐到最深者
        original_rects = dict(rects)
        if self._bottom_align:
            rects = self._align_columns_bottom(rects)
            print_alignment_info(original_rects, rects, "列")
            original_rects = dict(rects)

            rects = self._align_rows_bottom(rects)
            print_alignment_info(original_rects, rects, "行")

        self._rect_cache = rects
        self._idx_name_cache = idx_name
        self._name_idx_cache = name_idx
        self._hidden_snapshot = hidden

        log_frame_debug("数据应用完成",
                         rects=len(rects),
                         names=len(self._names),
                         frame_all=self._frame_all)
        self._refresh()
        # ★ R130：刷新成功后才记签名——原顺序先记后刷，_refresh 一旦抛
        #   异常，监视线程会因"签名未变"永远跳过补发，框滞留旧画面
        self._last_sig = sig

    # ★ 别名：供 collect_now 调用（直接接受 4 值元组）
    def _apply_paint_data(self, data):
        rects, idx_name, name_idx, hidden = data
        self._rect_cache = rects
        self._idx_name_cache = idx_name
        self._name_idx_cache = name_idx
        self._hidden_snapshot = hidden

    def _collect_paint_data(self):
        """构建绘制数据：图块矩形 + 名字映射 + 隐藏名单。

        关键原则：矩形数必须等于全量图标数，与 ListView 可见性无关。
        高亮框通过 _hidden_snapshot 过滤隐藏图标，而不是通过减少矩形数量。
        如果按 count（ListView 可见数）截断循环，索引和名字对应关系会错位——
        例如 ListView 只可见24个时，range(24) 拿到的名字是全量顺序的前24个，
        而这些名字对应的位置却是「系统工具」分类的图标，导致框标在错误位置。
        """
        lv = self._get_listview()
        if not lv:
            return ({}, {}, {}, set())
        proc, buf = self._open_proc()
        try:
            try:
                count = send_msg_timeout(lv, LVM_GETITEMCOUNT, 0, 0) or 0
            except Exception:
                count = 0
            icon_rects: Dict[int, Tuple[int, int, int, int]] = {}
            # ★R270-R2：把本轮已读到的 count 当复用锚点传进去
            name_to_idx = self._get_name_index_map(count)
            # 先建 idx->name，避免后面两次重复遍历
            idx_to_name = {i: n for n, i in name_to_idx.items()}

            # 优先使用 icon_manager 的位置缓存
            pos_cache = {}
            if self.icon_manager and hasattr(self.icon_manager, '_position_cache'):
                pos_cache = self.icon_manager._position_cache

            # ★ 核心修复：遍历全量索引，而不是 count
            # 这样每个图标名都能拿到正确的位置，不依赖当前 ListView 可见状态
            # ★ R130：上界取 max(索引)+1 而非 len——索引读取超时被跳过时
            #   映射有跳号，len 会让尾部图标拿不到框（违背本函数契约）
            total_icons = (max(name_to_idx.values()) + 1) if name_to_idx else count
            # ★R219（Bug1 同族）：下面的尺寸阈值是按 100% 拟的像素值，图标格
            #   尺寸随显示器缩放同步放大（175% 起两行标签的格子宽度就超 200），
            #   超阈值会被误判成"非法读数"整批降级到"缓存位置+固定框"，
            #   框自然对不上。阈值按 window_scale 同比例放宽，100% 下不变。
            from core.dpi_geom import window_scale
            _sc = window_scale(lv)
            _wmax, _hmax = int(200 * _sc), int(150 * _sc)
            cell_w, cell_h = 80, 68   # 降级框尺寸：跟随最近一次实测（R219）
            for idx in range(total_icons):
                name = idx_to_name.get(idx)
                if name is None:
                    continue
                # ★ 优先从 ListView 实时读取（含文字），避免固定尺寸导致偏差
                try:
                    r = self._read_item_rect(lv, proc, buf, idx, flag=0)
                    w, h_val = r[2] - r[0], r[3] - r[1]
                    if 40 <= w <= _wmax and 30 <= h_val <= _hmax:
                        icon_rects[idx] = r
                        cell_w, cell_h = w, h_val
                        continue
                except Exception:
                    pass
                # 降级：用缓存位置 + 最近实测尺寸
                pos = pos_cache.get(name)
                if pos:
                    x, y = pos
                    icon_rects[idx] = (x, y, x + cell_w, y + cell_h)
            rects = icon_rects
            log_frame_debug("读取图标数据完成",
                             count=len(rects),
                             total_expected=total_icons)

            # ★ 同步更新 icon_manager 缓存（降级读取时也要写入）
            # ★ 界外坐标禁止写回：屏幕外/被钳位图标的实时坐标一旦写进
            # _position_cache，下一次切换的"恢复原位"就会拿到界外坐标
            # （2026-09-02 修复：这是"切回来位置不对/框不准"的污染源）
            if self.icon_manager:
                # ★ R66c：restore_layout 套布局期间冻结写回——搬运途中的
                # 坐标一旦写进缓存，随后的 realign 会把图标搬回旧位
                if getattr(self.icon_manager,
                           '_pos_sync_freeze_until', 0) > time.time():
                    pass   # 冻结期：缓存保持刚套用的布局坐标
                else:
                    vx0, vy0, vx1, vy1 = _virtual_desktop_bounds()
                    for idx, rect in rects.items():
                        name = idx_to_name.get(idx)
                        if name and rect:
                            x, y = rect[0], rect[1]
                            if ((vx0 - 60) <= x <= (vx1 + 60)
                                    and (vy0 - 60) <= y <= (vy1 + 60)):
                                self.icon_manager._position_cache[name] = (x, y)
        finally:
            self._close_proc(proc, buf)

        # idx_name 反向映射（已在上层建好，这里直接用）
        idx_name = idx_to_name
        hidden = set()
        if self.icon_manager:
            try:
                hidden = self.icon_manager.hidden_names
            except Exception:
                hidden = set()

        log_frame_debug("_collect_paint_data 完成",
                         rects=len(rects),
                         names=len(idx_name),
                         hidden=len(hidden))
        return (rects, idx_name, name_to_idx, hidden)

    def _build_paint_cache(self):
        """（兼容保留）同步构建缓存——现由 _collect_paint_data + apply 取代。
        不做列底对齐：每个图标框保持独立。"""
        rects, idx_name, name_idx, hidden = self._collect_paint_data()
        # 不做 _align_columns_bottom：避免同列框底边拉伸相连
        self._rect_cache = rects
        self._idx_name_cache = idx_name
        self._name_idx_cache = name_idx
        self._hidden_snapshot = hidden

    def notify_changed(self):
        """外部通知图标已变动（如分类切换后/守护恢复布局），强制下次轮询重建缓存并立即重绘"""
        self._last_sig = None
        self._force_refresh = True
        self.invalidate_name_map()   # ★R270-R2：图标真的动过，映射不能复用
        # ★ 延迟重绘：等待 Explorer 完成图标位置稳定（避免圈住空白）
        # 使用 Tcl 定时器（如果在 Tk 线程中）或后台线程
        if hasattr(self, '_tk_root') and self._tk_root:
            # ★R270-R6：旧写法每次通知都新开一个 800ms 定时、没人取消上一个 ⇒
            # 连点悬浮栏会在一小段窗口里堆出 N 个"延迟重绘"，每一个都逼监视线程
            # 整表重采。只保留最后那一个。
            _prev = getattr(self, '_delayed_refresh_id', None)
            if _prev is not None:
                try:
                    self._tk_root.after_cancel(_prev)
                except Exception as e:
                    logger.debug(f"取消旧延迟重绘定时失败(已忽略): {e}")
            try:
                self._delayed_refresh_id = self._tk_root.after(
                    800, self._delayed_refresh)  # 增加到 800ms
            except Exception:
                self._delayed_refresh_id = None
                self._delayed_refresh()
        else:
            # 兜底：直接重绘
            self._delayed_refresh()

    def arm_recollect(self, seconds: float = 8.0):
        """★ R105：切换分类/套布局后开一段"重采窗口"（用户 2026-09-16 定案：
        点一次悬浮栏就要框准，不许靠点第二次）。

        背景：highlight_names 在切换瞬间用的是上一视图的 _rect_cache
        （框画在"上次的框位置"）；且签名基于位置缓存，Explorer 切换后
        晚几秒把图标重排到位（或排回持久化旧位）对它不可见——旧框一直
        挂到下次点击。窗口期内监控循环每轮强制重采，实测坐标与上次绘制
        不同才重绘（无漂移则不闪）。"""
        self._recollect_until = time.time() + seconds
        self._last_sig = None
        self._force_refresh = True
        # ★R272：这一路是最硬的扳机（切分类/套布局/悬浮条「⚡ 按自动打开设置」）
        # ——保持现状即时全量重采、不许降频，只顺手把密集窗口起点重置、退出睡死。
        self._arm_window()
        self.invalidate_name_map()   # ★R270-R2：视图换了，映射按新表重读

    def _delayed_refresh(self):
        """延迟后的刷新，确保图标位置已稳定
        ★R204 CORE-36：置 _force_refresh 让监视线程下一轮立即重采+重绘；
        旧实现只 InvalidateRect 已退役的 overlay，实际要再等 2s"""
        self._delayed_refresh_id = None   # ★R270-R6：这一发已经落地
        self._force_refresh = True

    def _positions_signature(self):
        """轻量位置签名：全部图标(框住模式)或高亮名单的位置快照。
        使用 icon_manager 的位置缓存，避免 LVM_GETITEMPOSITION 失效问题。

        关键：使用可见图标数量（过滤掉隐藏文件），避免签名在可见性变化时不变。"""
        lv = self._get_listview()
        if not lv:
            return None

        # 使用原始图标数量作为签名基数（与 _get_name_index_map 保持一致）
        count = send_msg_timeout(lv, LVM_GETITEMCOUNT, 0, 0) or 0

        # 优先使用位置缓存
        pos_cache = {}
        if self.icon_manager and hasattr(self.icon_manager, '_position_cache'):
            pos_cache = self.icon_manager._position_cache

        if self._frame_all and pos_cache:
            # 框住全部模式：用缓存位置构造签名
            name_to_idx = self._get_name_index_map(count)
            # 构建反向映射 idx->name
            idx_to_name = {i: n for n, i in name_to_idx.items()}
            pts = []
            for idx in range(count):
                name = idx_to_name.get(idx)
                pos = pos_cache.get(name) if name else None
                if pos:
                    pts.append((_sig_round(pos[0]), _sig_round(pos[1])))
                else:
                    pts.append((0, 0))
            return (count, tuple(pts))
        elif self._names and pos_cache:
            # 高亮指定名称模式
            name_to_idx = self._get_name_index_map(count)
            pts = []
            for name in self._names:
                idx = name_to_idx.get(name)
                pos = pos_cache.get(name) if idx is not None else None
                if pos:
                    pts.append((_sig_round(pos[0]), _sig_round(pos[1])))
                else:
                    pts.append(None)
            return (count, tuple(pts))
        else:
            # 降级：尝试直接读取（已知在 Progman 上会失败）
            proc = self._open_proc()
            try:
                buf = proc[1]
                if self._frame_all:
                    pts = []
                    for idx in range(count):
                        x, y = self._read_position(lv, proc[0], buf, idx)
                        pts.append((_sig_round(x), _sig_round(y)))
                    return (count, tuple(pts))
                else:
                    name_to_idx = self._get_name_index_map(count)
                    pts = []
                    for name in self._names:
                        idx = name_to_idx.get(name)
                        if idx is None:
                            pts.append(None)
                        else:
                            x, y = self._read_position(lv, proc[0], buf, idx)
                            pts.append((_sig_round(x), _sig_round(y)))
                    return (count, tuple(pts))
            finally:
                self._close_proc(proc[0], proc[1])

    def _colorref(self) -> int:
        r, g, b = self._color
        return (b << 16) | (g << 8) | r

    def _get_listview(self) -> Optional[int]:
        if self.icon_manager:
            try:
                return self.icon_manager.hwnd_listview
            except Exception:
                return None
        hwnd_progman = win32gui.FindWindow("Progman", None)
        shell = win32gui.FindWindowEx(hwnd_progman, 0, "SHELLDLL_DefView", None)
        lv = win32gui.FindWindowEx(shell, 0, "SysListView32", None)
        return lv or None

    def _get_name_index_map(self, count: Optional[int] = None) -> Dict[str, int]:
        """名称→索引映射（★R270-R2：整表跨进程读加一层缓存）。

        这一读是监视循环里最贵的一项：每个图标都要 1 次 WriteProcessMemory +
        1 次同步 SendMessageTimeout + 1 次 ReadProcessMemory。本机实测 87 个
        图标 ⇒ 每 2 秒 87 发，而且同一轮里 _positions_signature 与
        _collect_paint_data 各读一遍。缓存把"一周期两遍"合成"至多一遍"，
        并在图标总数没变时最多每 _NAME_MAP_STALE_SEC 读一次。

        count（调用方本轮已读到的 LVM_GETITEMCOUNT）是复用锚点：不传就不敢
        复用，直接真读。代价（用户 24 批已认可"精度让位流畅"）：图标被外部
        改名/重排而总数不变时，框最长晚 _NAME_MAP_STALE_SEC + 一拍才跟上；
        分类切换、套布局、守护恢复都走 arm_recollect()/notify_changed()
        立刻作废，用户可感知的三条路径不受影响。
        """
        if not self.icon_manager:
            return {}
        cached = getattr(self, '_name_map_cache', None)
        now = time.monotonic()
        if (cached is not None and count is not None
                and not getattr(self, '_name_map_dirty', False)
                and count == cached[0]
                and now - cached[1] < self._NAME_MAP_STALE_SEC):
            return cached[2]
        try:
            # 必须用原始全量映射（含隐藏文件），因为 Progman SysListView32 不响应
            # 文件隐藏属性——被隐藏的文件在 ListView 中仍存在，索引不变。
            # 若用可见映射会导致索引错位，框住错误的图标。
            mapping = self.icon_manager._get_current_icon_map()
        except Exception as e:
            logger.error(f"读取图标索引失败: {e}")
            return {}
        self._name_map_cache = (
            count if count is not None else len(mapping), now, mapping)
        self._name_map_dirty = False
        return mapping

    def invalidate_name_map(self):
        """★R270-R2：作废映射缓存，下一次读取必须真读全表。"""
        self._name_map_dirty = True

    def _open_proc(self):
        # ★ B6：VirtualAllocEx 失败时必须释放已打开的进程句柄，
        #   否则 Explorer 忙/权限受限时反复失败会缓慢泄漏句柄
        lv = self._get_listview()
        pid = win32process.GetWindowThreadProcessId(lv)[1]
        proc = None
        try:
            proc = win32api.OpenProcess(win32con.PROCESS_ALL_ACCESS, False, pid)
            buf = win32process.VirtualAllocEx(proc, 0, 512, MEM_COMMIT, PAGE_READWRITE)
            # ★R204 CORE-25：buf=0 不拦住，后续 WriteProcessMemory 全败被吞，
            # 全部矩形走"缓存+固定 80×68"降级且日志零线索
            if not buf:
                raise OSError('VirtualAllocEx 返回 0')
            return proc, buf
        except Exception:
            if proc:
                try:
                    win32api.CloseHandle(proc)
                except Exception:
                    pass
            raise

    def _close_proc(self, proc, buf):
        try:
            win32process.VirtualFreeEx(proc, buf, 0, MEM_RELEASE)
        except Exception:
            pass
        try:
            win32api.CloseHandle(proc)
        except Exception:
            pass

    def destroy(self):
        """销毁全部 overlay 窗口（退出时调用）"""
        try:
            if self._zendesktop_overlay:
                self._zendesktop_overlay.destroy()
                self._zendesktop_overlay = None
        except Exception:
            pass
        if self._overlay and win32gui.IsWindow(self._overlay):
            try:
                win32gui.DestroyWindow(self._overlay)
            except Exception:
                pass
        self._overlay = None
