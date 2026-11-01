"""DPI 坐标系收口：进程感知声明 + "物理像素 ↔ Tk 窗口几何"换算

为什么需要这个模块（R219，2026-09-23 用户报 Bug1）：

桌面图标矩形来自 Explorer 的 SysListView32（LVM_GETITEMRECT），**恒为物理
像素**（Explorer 自身是 Per-Monitor-V2）；而 Tk 的窗口几何
（``Toplevel.geometry('+x+y')``）用的是**本进程 DPI 感知档位决定的坐标系**。
两条口径只有在"进程 = PMv2"或"缩放 = 100%"时才重合：

    125% + 进程被降级为 System/Unaware ⇒ Tk 侧宽 1536，图标矩形宽 1920，
    框按 1.25 倍偏，且离原点越远偏得越多——正是"查找桌面图标框不准"。
    100% 下两者 1:1，本机永远复现不出来（开发机 Win10/100%，测试机
    Win11/125%）。

进程感知档位是**一次性、先到先得、不可覆盖**的：任何一次
SetProcessDPIAware()/shcore.SetProcessDpiAwareness() 抢跑，之后
SetProcessDpiAwarenessContext(PMv2) 一律 ACCESS_DENIED(5)。所以声明必须
收口到唯一入口（本模块 declare_process_dpi_awareness），任何模块都不得
再自行调用旧接口——历史上 icon_manager 就自己调过一次 SetProcessDPIAware。

换算不依赖"进程一定是 PMv2"这个假设：to_tk_rect 用真实物理分辨率
（EnumDisplaySettings，不受 DPI 虚拟化影响）与 Tk 实际屏幕宽比较得出比例，
PMv2 下比例恒为 1（零改变），被降级时自动补偿。这样不同显示设置下都对得上。
"""
import ctypes
import logging

logger = logging.getLogger("desktop_manager")

_DPI_AWARENESS_CONTEXT_UNAWARE = -1
_DPI_AWARENESS_CONTEXT_SYSTEM_AWARE = -2
_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE = -3
_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4

_CTX_NAMES = {
    _DPI_AWARENESS_CONTEXT_UNAWARE: 'UNAWARE',
    _DPI_AWARENESS_CONTEXT_SYSTEM_AWARE: 'SYSTEM',
    _DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE: 'PMv1',
    _DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2: 'PMv2',
}


def _user32():
    return ctypes.windll.user32


# ★R336-a：本文件所有 ctypes 外函数的原型**进模块就声明**，不等各调用点各自补。
# ctypes 默认 restype 是 c_long——64 位下 HDC 是指针宽的句柄，取回来先被截成
# 32 位再传回去，Explorer 就认不出这个 DC（★R234 的学费正是这一条）。
# 本文件其余四处（SetProcessDpiAwarenessContext / GetThreadDpiAwarenessContext /
# AreDpiAwarenessContextsEqual / GetDpiForWindow）本来就显式声明过，这四枚是漏网的。
# （`shcore.SetProcessDpiAwareness` 没写进来：`ctypes.windll.shcore` 在 import
#   时就 LoadLibrary，老系统上会把整张模块炸掉——它只在该函数的 try 里用。）
_U32 = ctypes.windll.user32
_GDI = ctypes.windll.gdi32
_U32.GetDC.restype = ctypes.c_void_p
_U32.GetDC.argtypes = [ctypes.c_void_p]
_U32.ReleaseDC.restype = ctypes.c_int
_U32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_GDI.GetDeviceCaps.restype = ctypes.c_int
_GDI.GetDeviceCaps.argtypes = [ctypes.c_void_p, ctypes.c_int]
_U32.SetProcessDPIAware.restype = ctypes.c_bool
_U32.SetProcessDPIAware.argtypes = []


_already_declared = None


def declare_process_dpi_awareness(log=None) -> str:
    """把进程声明为 Per-Monitor-V2；老系统逐级降级。返回最终档位。

    **必须在创建任何窗口（Tk 主窗 / 单实例互斥窗 / overlay）之前调用**。
    可重复调用：档位先到先得，第二次起直接返回现状，不再刷日志
    （R219：IconManager 等多个入口都会调，此前会把"已声明"写成假的降级日志）。"""
    global _already_declared
    log = log or logger
    cur = effective_awareness()
    if cur == 'PMv2':
        return cur
    if _already_declared is not None and cur == _already_declared:
        return cur
    u = _user32()
    result = 'Unknown'
    try:
        fn = u.SetProcessDpiAwarenessContext
        fn.restype = ctypes.c_bool
        fn.argtypes = [ctypes.c_void_p]
        if fn(ctypes.c_void_p(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)):
            result = 'PMv2'
    except Exception as e:
        log.debug(f'[DPI] PMv2 声明不可用: {e}')
    if result != 'PMv2':
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
        except Exception as e:
            log.debug(f'[DPI] shcore 声明不可用: {e}')
        try:
            u.SetProcessDPIAware()
        except Exception as e:
            log.debug(f'[DPI] System 档声明失败: {e}')
    result = effective_awareness()
    _already_declared = result
    if result == 'PMv2':
        log.info('[DPI] 进程感知档位: Per-Monitor-V2（坐标口径与 Explorer 一致）')
    else:
        # 拿不到 PMv2 ⇒ 高缩放下所有"窗口/图标坐标匹配"都要靠 to_tk_rect 补偿，
        # 这是 Bug1 那类问题的现场线索，必须留一条响的
        log.warning(f'[DPI] 进程感知档位={result}（未达 PMv2，'
                    f'高 DPI 缩放下框位换算依赖 to_tk_rect 补偿）')
    return result


def effective_awareness() -> str:
    """当前线程实际生效的感知档位：'PMv2'/'PMv1'/'System'/'UNAWARE'/'Unknown'。

    ★ 不能用 GetAwarenessFromDpiAwarenessContext——它返回 DPI_AWARENESS 枚举
    （0/1/2），V1 与 V2 都是 2，天生分不开（历史上误判过一次"进程只是 PMv1"）。
    必须拿上下文句柄与已知常量做 AreDpiAwarenessContextsEqual。"""
    u = _user32()
    try:
        get = u.GetThreadDpiAwarenessContext
        get.restype = ctypes.c_void_p
        ctx = get()
        eq = u.AreDpiAwarenessContextsEqual
        eq.restype = ctypes.c_bool
        eq.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        for neg, name in _CTX_NAMES.items():
            if eq(ctypes.c_void_p(ctx), ctypes.c_void_p(neg)):
                return name
        return 'Unknown'
    except Exception as e:
        logger.debug(f'[DPI] 档位查询失败: {e}')
        return 'Unknown'


def physical_screen_size():
    """主显示器**真实物理**分辨率 (w, h)；失败返回 None。

    刻意不用 GetSystemMetrics(0)：那是不感知 DPI 虚拟化的进程眼里的"屏幕"，
    与被虚拟化的 Tk 坐标同源，拿它俩互比永远得到比例 1（历史上
    quick_find 的高 DPI 兜底就是这么写成死代码的）。
    GetDeviceCaps 的 DESKTOPHORZRES(118)/DESKTOPVERTRES(117) 是 MSDN 明确的
    "物理屏幕像素宽高"，不受 DPI 虚拟化影响；HORZRES(4) 则会随虚拟化变，
    所以这里必须用 117/118 而不是 4/5。"""
    u = _user32()
    gdi = ctypes.windll.gdi32
    hdc = u.GetDC(0)
    if not hdc:
        return None
    try:
        w = gdi.GetDeviceCaps(hdc, 118)   # DESKTOPHORZRES
        h = gdi.GetDeviceCaps(hdc, 117)   # DESKTOPVERTRES
        if w > 0 and h > 0:
            return (int(w), int(h))
        return None
    except Exception as e:
        logger.debug(f'[DPI] 物理分辨率读取失败: {e}')
        return None
    finally:
        u.ReleaseDC(0, hdc)


def window_scale(hwnd) -> float:
    """窗口所在显示器的缩放倍数（96dpi → 1.0；125% → 1.25）。失败返回 1.0。

    用于给"按 100% 拟定的像素阈值"做同比例放宽——图标格尺寸、框线粗细这类
    常量在高 DPI 下会整体放大，写死像素值等于假设了 100%。"""
    try:
        u = _user32()
        fn = u.GetDpiForWindow
        fn.restype = ctypes.c_uint
        fn.argtypes = [ctypes.c_void_p]
        dpi = fn(ctypes.c_void_p(int(hwnd) if hwnd else None))
        if 96 <= dpi <= 480:
            return float(dpi) / 96.0
    except Exception as e:
        logger.debug(f'[DPI] GetDpiForWindow 失败: {e}')
    return 1.0


def tk_scale_ratio(tk_screen_w, phys_screen_w=None):
    """Tk 坐标系 1 单位 = 多少物理像素（k）。无法判定时返回 1.0（不换算）。

    tk_screen_w 取 ``root.winfo_screenwidth()``。k<1 说明本进程被 DPI 虚拟化
    了（125% → 0.8），物理像素矩形要乘以 k 才能喂给 Tk 的 geometry。
    phys_screen_w 供测试注入（默认取本机真实物理宽）。"""
    try:
        if phys_screen_w is None:
            phys = physical_screen_size()
            if not phys:
                return 1.0
            phys_screen_w = phys[0]
        if not phys_screen_w or not tk_screen_w or tk_screen_w <= 0:
            return 1.0
        k = float(tk_screen_w) / float(phys_screen_w)
        # 2% 以内视为口径一致：避免缩放分辨率/取整抖动引起无谓位移
        if abs(k - 1.0) < 0.02:
            return 1.0
        if not (0.25 <= k <= 4.0):
            return 1.0
        return k
    except Exception:
        return 1.0


def to_tk_rect(rect, tk_screen_w, phys_screen_w=None):
    """把物理像素矩形换算到 Tk 窗口几何坐标系；PMv2/100% 下原样返回。"""
    k = tk_scale_ratio(tk_screen_w, phys_screen_w)
    l, t, r, b = rect
    if k == 1.0:
        return (int(l), int(t), int(r), int(b))
    return (int(round(l * k)), int(round(t * k)),
            int(round(r * k)), int(round(b * k)))
