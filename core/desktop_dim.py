"""桌面亮度调节——软件蒙版（唯一路径，R69 起硬件 DDC 已移除）。

★ R69（2026-09-15 用户定案：只用软件调节，不再浪费时间）：硬件 DDC/CI
路径整体移除。实测证据（win11，详见 DEV.md 开发日记）：哪怕一次只读
探测（GetMonitorBrightness，不发任何写），两分钟后同一进程即以
0xC0000005 访问冲突崩溃（launcher.log code=3221225477），当天更两次
蓝屏——该机器上 DDC 链路（显卡驱动内核 I2C 栈）本身就是毒源，读也不行。
硬件代码原样存档于 代码存档/亮度硬件DDC_不可用_20260915/（含 README，
列为不可用代码）。任何情况下不要把硬件路径接回来。

软件蒙版：半透明黑色窗口盖屏幕（点击穿透），即时、安全、全平台可用；
★ R98（2026-09-16 用户定案）：每台显示器各记各的亮度、蒙版互相独立——
调某一台只动那一台，其他屏保持现状；scope=None 统一调全部。
与聚光灯/高亮框的叠加：无论哪种情况，聚光灯高亮都比背景亮一档。
"""
import logging
import ctypes
from ctypes import wintypes, POINTER, WINFUNCTYPE

import win32gui
import win32con
import win32api

logger = logging.getLogger("desktop_manager")

_MONITORENUMPROC = WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR,
                               wintypes.HDC, POINTER(wintypes.RECT),
                               wintypes.LPARAM)
_monitor_proc = None   # 模块级保活，防回调被 GC

# ★R334-a：本文件两发 ctypes 调用钉住原型。
# ① PatBlt 的句柄参数——过去靠 icon_overlay 恰好声明过同一枚才没出事，
#    单独导入本模块（只调暗、不挂高亮框）时它是裸的；
# ② EnumDisplayMonitors 的 HDC/LPRECT/LPARAM 全是指针，未声明时参数尺寸
#    没人保证（回调那枚是 ctypes 认得 CData 才侥幸传对）。
_gdi32 = ctypes.windll.gdi32
_gdi32.PatBlt.restype = ctypes.c_int
_gdi32.PatBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int, ctypes.c_int, ctypes.c_uint]
_u32 = ctypes.windll.user32
_u32.EnumDisplayMonitors.restype = wintypes.BOOL
_u32.EnumDisplayMonitors.argtypes = [wintypes.HMONITOR, wintypes.HDC,
                                     _MONITORENUMPROC, ctypes.c_void_p]


def _enum_monitor_rects():
    """[(hMonitor, (x, y, w, h)), ...]（纯用户态枚举，无任何 I2C 事务）

    ★R204 CORE-26：失败/空结果返回 None 以区别于"真的没有显示器"——
    此前空列表被 _sw_cleanup 当成"所有屏都没了"，把在册蒙版全部
    SW_HIDE 并清空 _visible（"调暗的屏幕自己亮回来了"）。"""
    out = []

    def cb(hmon, hdc, lprect, data):
        r = lprect.contents
        out.append((hmon, (r.left, r.top, r.right - r.left,
                           r.bottom - r.top)))
        return True

    global _monitor_proc
    _monitor_proc = _MONITORENUMPROC(cb)   # 调用期间保活引用（防 GC）
    try:
        ctypes.windll.user32.EnumDisplayMonitors(
            None, None, _monitor_proc, None)
    except Exception as e:
        logger.error(f"枚举显示器失败: {e}")
        return None
    return out or None


def list_monitors():
    """枚举显示器做人读标签，供"作用显示器"下拉框选择。

    序号顺序与 set_level 的 scope 一致（None=全部 / int=只盖该台）。
    纯用户态枚举 + GetMonitorInfo，UI 线程可安全调用。"""
    out = []

    def cb(hmon, hdc, lprect, data):
        r = lprect.contents
        primary = False
        try:
            info = win32api.GetMonitorInfo(hmon)
            primary = bool(info.get('Flags', 0) & 1)   # MONITORINFOF_PRIMARY
        except Exception:
            pass
        out.append((r.left, r.top, r.right - r.left, r.bottom - r.top, primary))
        return True

    global _monitor_proc
    _monitor_proc = _MONITORENUMPROC(cb)
    try:
        ctypes.windll.user32.EnumDisplayMonitors(None, None, _monitor_proc, None)
    except Exception as e:
        logger.error(f"枚举显示器失败: {e}")
    labels = []
    for i, (x, y, w, h, primary) in enumerate(out):
        tag = '主屏' if primary else '副屏'
        labels.append(f"显示器{i + 1}（{tag} {w}×{h} @{x},{y}）")
    return labels


class DesktopDimmer:
    """桌面亮度调节器（★ R69 起：纯软件蒙版）。

    level: 30~100，100=正常亮度。
    mode: 'sw'=软件蒙版 / 'none'=未启用。
    scope: None=全部显示器；int=只盖该序号屏（分屏调光，R67c 用户定案）。
    post_fn: gui 注入的线程投递函数（保留：模式回调经它回 UI 线程）。
    mode_changed_cb: gui 注入的 fn(mode)，终态变化时通知界面显示。"""

    def __init__(self, config_dir=None):
        self.level = 100
        self.mode = 'none'
        # ★ 原 hw_scope 改名 scope：R69 起只作用于软件蒙版（按屏分区）
        self.scope = None
        self.post_fn = None          # gui 注入：fn(callback)
        self.mode_changed_cb = None  # gui 注入：fn(mode: str)
        self._sw_windows = {}        # rect字符串 -> hwnd
        self._visible = set()        # 当前有蒙版可见的 rect 字符串（R98）
        self._class_registered = False

    def _notify_mode(self, direct: bool = False):
        """把当前 mode 通知 gui。

        direct=True 表示调用方已在主线程（本类所有入口都是 UI 线程调用），
        直接同步调 cb；未注入回调时跳过。"""
        cb = self.mode_changed_cb
        if not cb:
            return
        try:
            if direct or not self.post_fn:
                cb(self.mode)
            else:
                self.post_fn(lambda: cb(self.mode))
        except Exception:
            pass

    # -- 软件蒙版窗口（即时·安全·唯一路径） --

    @staticmethod
    def _wndproc(hwnd, msg, w_param, l_param):
        if msg == 0x0014:      # WM_ERASEBKGND：w_param = DC，涂纯黑
            try:
                hdc = w_param
                l, t, r, b = win32gui.GetClientRect(hwnd)
                ctypes.windll.gdi32.PatBlt(hdc, 0, 0, r - l, b - t,
                                           0x00000042)   # BLACKNESS
            except Exception:
                pass
            return 1
        return win32gui.DefWindowProc(hwnd, msg, w_param, l_param)

    def _sw_window(self, x, y, w, h):
        """创建/复用一个点击穿透的黑色蒙版窗口（按显示器矩形）"""
        key = f'{x},{y},{w},{h}'
        hwnd = self._sw_windows.get(key)
        if hwnd and win32gui.IsWindow(hwnd):
            return hwnd
        className = "DesktopDimClass"
        if not self._class_registered:
            wc = win32gui.WNDCLASS()
            wc.hInstance = win32api.GetModuleHandle(None)
            wc.lpszClassName = className
            wc.style = win32con.CS_HREDRAW | win32con.CS_VREDRAW
            wc.lpfnWndProc = self._wndproc
            wc.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
            try:
                win32gui.RegisterClass(wc)
                self._class_registered = True
            except win32gui.error:
                pass
        hwnd = win32gui.CreateWindowEx(
            win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT |
            win32con.WS_EX_TOOLWINDOW | win32con.WS_EX_NOACTIVATE,
            className, None, win32con.WS_POPUP,
            x, y, w, h, None, None, win32api.GetModuleHandle(None), None)
        if hwnd:
            try:
                hdc = win32gui.GetDC(hwnd)
                ctypes.windll.gdi32.PatBlt(hdc, 0, 0, w, h, 0x00000042)
                win32gui.ReleaseDC(hwnd, hdc)
            except Exception as e:
                logger.error(f"蒙版涂黑失败: {e}")
            win32gui.SetLayeredWindowAttributes(
                hwnd, 0, 0, win32con.LWA_ALPHA)
        self._sw_windows[key] = hwnd
        return hwnd

    @staticmethod
    def _alpha_for(level: int) -> int:
        return min(130, int((100 - level) * 1.6))

    def _sw_set_rect(self, rect, level):
        """★ R98：为单个显示器矩形设置/收起蒙版——只动这一台，
        其他屏的蒙版原样保留（旧 _sw_apply 会隐藏不在名单内的屏，
        导致切作用显示器时另一台的调光被"顺手关掉"=回 100 假象）。"""
        key = f'{rect[0]},{rect[1]},{rect[2]},{rect[3]}'
        if level >= 100:
            hwnd = self._sw_windows.get(key)
            if hwnd and win32gui.IsWindow(hwnd):
                try:
                    win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
                except Exception:
                    pass
            self._visible.discard(key)
            return
        hwnd = self._sw_window(*rect)
        if not hwnd:
            return
        win32gui.SetLayeredWindowAttributes(
            hwnd, 0, self._alpha_for(level), win32con.LWA_ALPHA)
        win32gui.ShowWindow(hwnd, win32con.SW_SHOWNA)
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE |
                              win32con.SWP_NOACTIVATE)
        self._visible.add(key)

    def _sw_cleanup(self):
        """显示器拓扑变化后（拔插/改分辨率），收起已不存在的矩形的蒙版。
        ★R204 CORE-26：枚举失败（None）视为失败而非"全没了"，保留现状。"""
        _all = _enum_monitor_rects()
        if not _all:
            logger.error("[亮度] 显示器枚举失败，跳过蒙版清理（保留现状）")
            return
        alive = {f'{x},{y},{w},{h}' for _h, (x, y, w, h) in _all}
        for key, hwnd in list(self._sw_windows.items()):
            if key not in alive:
                # ★R204 CORE-27：不再存活的矩形 DestroyWindow（旧 SW_HIDE
                # 窗口永久滞留=分辨率/插拔后累积）
                try:
                    win32gui.DestroyWindow(hwnd)
                except Exception:
                    try:
                        win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
                    except Exception:
                        pass
                self._sw_windows.pop(key, None)
                self._visible.discard(key)

    def _mode_by_visible(self):
        return 'sw' if self._visible else 'none'

    def _hide_sw(self):
        self._visible.clear()
        for hwnd in self._sw_windows.values():
            try:
                win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
            except Exception:
                pass

    # -- 主入口（UI 线程直接调用：纯用户态操作，无阻塞风险） --

    def set_level(self, level: int):
        """应用亮度。★ R98 起按屏独立（用户定案）：

        scope=None → 统一调节全部显示器；
        scope=int  → 只动这一台的蒙版，其他屏保持各自现状不变
        （旧实现会把不在作用范围内的屏"顺手藏掉"，表现成另一台
        亮度自动回 100——用户 2026-09-16 实测报告的根因）。"""
        level = max(30, min(100, int(level)))
        _all = _enum_monitor_rects()
        if not _all:
            # ★R207 NEW3-C14：枚举失败时不接受新 level（赋值挪到守卫后），
            # 否则滑条显示已调、蒙版没动=设置假生效
            # ★R204 CORE-26：枚举失败不决策，保现状（scope 越界静默不做的
            # 上游即此处）
            logger.error("[亮度] 显示器枚举失败，本次不调节（保留现状）")
            return
        self.level = level   # ★R207 NEW3-C14：确认可执行才记账
        _rects = [r for _h, r in _all]
        _scope = self.scope
        if _scope is None:
            for r in _rects:
                self._sw_set_rect(r, level)
        else:
            if 0 <= _scope < len(_rects):
                self._sw_set_rect(_rects[_scope], level)
        # ★R333-c2（P1）：清理原来只挂在"指定某一台"那一支 ⇒ 统一调节（滑条默认
        # 走的就是这条）永远不收起已消失矩形的蒙版：拔屏/改分辨率后那层 topmost
        # 半透明窗留在桌上（多半盖住现存屏的一条边、或跑到新虚拟桌面边界外），
        # 而且 `_visible` 里那枚死键还留着 ⇒ 下面两句读数一起偏（日志报
        # "蒙版可见 3/2 台"、`_mode_by_visible` 把已回 100 的机器算成仍在调光）。
        # 两条分支之后都要清一次；`_sw_cleanup` 自己只动"不在存活名单里"的那些，
        # 幂等、且枚举失败时保现状（★R204 CORE-26）。
        self._sw_cleanup()
        self.mode = self._mode_by_visible()
        logger.info(f"[亮度] 软件蒙版: level={level} 蒙版可见 "
                    f"{len(self._visible)}/{len(_rects)} 台（scope={_scope}）")
        self._notify_mode(direct=True)   # 终态：软件蒙版已铺好/已收起

    def restore_levels(self, levels: dict, fallback: int = 100):
        """★ R98：启动恢复——按每台显示器"自己的记忆"铺蒙版。

        levels 形如 {'all': 80, '0': 65, '1': 90}；某台没有专属记忆
        时退到 'all'，再没有退到 fallback。只铺 <100 的台。"""
        levels = levels if isinstance(levels, dict) else {}
        _all_lv = levels.get('all', fallback)
        _all = _enum_monitor_rects()
        if not _all:
            # ★R204 CORE-26：枚举失败不铺蒙版
            logger.error("[亮度] 显示器枚举失败，启动恢复跳过")
            return
        _rects = [r for _h, r in _all]
        for i, r in enumerate(_rects):
            try:
                lv = max(30, min(100, int(levels.get(str(i), _all_lv))))
            except (TypeError, ValueError):
                lv = 100
            self._sw_set_rect(r, lv)
        self.mode = self._mode_by_visible()
        logger.info(f"[亮度] 启动恢复: 记忆={levels} 蒙版可见 "
                    f"{len(self._visible)}/{len(_rects)} 台")
        self._notify_mode(direct=True)

    def restore_now(self):
        """程序退出/主题重启前：隐藏全部蒙版窗口（R69 起无硬件需恢复）"""
        self._hide_sw()
