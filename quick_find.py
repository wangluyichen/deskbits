"""快速定位图标 - 全局热键呼出的小搜索框

替代桌面原生的"首字母快速定位"：加了前缀后首字母变了，按 v 找不到
VMware。这里按名字任意片段搜索（如输入 vm），回车直接跳到桌面选中该
图标并高亮几秒。Esc 关闭。支持上下键选择、Ctrl+A 全选复制结果。
"""
import tkinter as tk
from tkinter import ttk
from typing import Dict, List
from gui import ui_layout   # ★R228：界面字号等比缩放 helper

# 主题色引用：main_window 启动时 set_colors(COLORS) 注入**同一 dict 对象**
# （apply_ui_theme 原地换色自动跟随）。
# ★R221（NEW7-P0-1）：这份兜底表必须覆盖本模块每一处 COLORS 下标直取键。
# R215 把 token `border` 改名 `text_secondary` 时只补了 theme_panel 真身和
# rule/kids 两处兜底表，这里漏补 → 快速定位框一构建就 KeyError，热键整条功能
# 死掉，而且每次按热键在 super().__init__ 之后抛异常 → 泄漏一个置顶空窗。
COLORS = {
    'bg': '#282a36', 'fg': '#f8f8f2', 'primary': '#8be9fd',
    'secondary': '#50fa7b', 'surface': '#44475a', 'border': '#636d83',
    'text_secondary': '#bcc3d6',
}


def set_colors(colors: dict):
    global COLORS
    COLORS = colors


_finder_instance = None


def open_quick_finder(parent, icon_manager, category_manager=None):
    """打开（或唤起已有的）快速定位框。方框提醒在模块内自绘（R151）。"""
    global _finder_instance
    if _finder_instance is not None and _finder_instance.winfo_exists():
        _finder_instance.deiconify()   # ★ R142：最小化/被藏时拉回前台
        _finder_instance.lift()
        _finder_instance.focus_entry()
        return _finder_instance
    _finder_instance = QuickFinder(parent, icon_manager, category_manager)
    return _finder_instance


class QuickFinder(tk.Toplevel):
    # ★R313 G4：结果列表**实际渲染**的行数上限（`_refresh_results` 按它截断）。
    # 上下键取模、越界判据都必须用它——按 `len(self._names)`（全部命中数）取模
    # 会 `selection_set(40)` 越界，而 Tk 对越界下标是**静默不选**，不是报错。
    MAX_ROWS = 40

    def __init__(self, parent, icon_manager, category_manager=None):
        super().__init__(parent)
        self.icon_manager = icon_manager
        self.category_manager = category_manager

        self.title('🔍 快速定位图标')
        self.configure(bg=COLORS['bg'])
        self.resizable(False, True)
        self.attributes('-topmost', True)
        # ★ R142：去掉 transient(parent)——主窗隐藏（只用悬浮框）时，挂在
        #   主窗下的子窗会被一并压后/藏起，用户报"不点主窗口压根看不见"。
        #   改为完全独立窗口：置顶 + <Map> 再补置顶 + 唤起已有实例时 deiconify。
        # 固定出现在主屏顶部居中（避开主界面常在的位置，也减少被遮挡概率）
        self.update_idletasks()
        try:
            sw = self.winfo_screenwidth()
        except Exception:
            sw = 1920
        self.geometry(f'460x360+{max(0, (sw - 460) // 2)}+90')
        # 显示拓扑变化后如被压到下面，重新映射时强制回置顶
        self.bind('<Map>', lambda e: (self.attributes('-topmost', True),
                                      self.lift()))

        ttk.Label(self, text='输入图标名的任意片段（如 vm、微信），回车跳到桌面选中',
                  foreground=COLORS['text_secondary'],
                  font=ui_layout.app_font(9)).pack(fill=tk.X, padx=10, pady=(8, 4))

        self.var = tk.StringVar()
        self.entry = ttk.Entry(self, textvariable=self.var, font=ui_layout.app_font(12))
        self.entry.pack(fill=tk.X, padx=10, pady=(0, 6))
        self.entry.focus_force()
        self.entry.bind('<KeyRelease>', self._on_key)
        self.entry.bind('<Return>', lambda e: self._jump())
        self.entry.bind('<Down>', lambda e: self._move(1))
        self.entry.bind('<Up>', lambda e: self._move(-1))
        self.entry.bind('<Escape>', lambda e: self.destroy())

        self.listbox = tk.Listbox(self, font=ui_layout.app_font(10),
                                  bg=COLORS['surface'], fg=COLORS['fg'],
                                  selectmode=tk.SINGLE, relief=tk.FLAT,
                                  highlightthickness=0, activestyle='none',
                                  exportselection=False)
        self.listbox.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 4))
        self.listbox.bind('<Double-Button-1>', lambda e: self._jump())
        self.listbox.bind('<Return>', lambda e: self._jump())

        self.hint_var = tk.StringVar(value='回车=跳到桌面选中并方框闪烁    Esc=关闭')
        ttk.Label(self, textvariable=self.hint_var,
                  foreground=COLORS['text_secondary'],
                  font=ui_layout.app_font(9)).pack(fill=tk.X, padx=10, pady=(0, 6))

        self._names: List[str] = []
        self._members: Dict[str, set] = {}
        self.protocol('WM_DELETE_WINDOW', self.destroy)
        self.bind('<Escape>', lambda e: self.destroy())
        self._refresh_results()

    def focus_entry(self):
        try:
            self.entry.focus_force()
            self.entry.select_range(0, tk.END)
        except Exception:
            pass

    # ------------------------------------------------------------------

    def _all_icons(self) -> List[str]:
        """当前可见的全部桌面图标名（按桌面顺序）"""
        try:
            return list(self.icon_manager.get_current_icon_positions().keys())
        except Exception:
            return []

    def _members_of(self, name: str) -> set:
        if name not in self._members:
            cats = set()
            if self.category_manager:
                try:
                    cats = {c['name'] for c in self.category_manager.get_all_categories()
                            if name in c.get('icons', [])}
                except Exception:
                    cats = set()
            self._members[name] = cats
        return self._members[name]

    def _refresh_results(self):
        kw = self.var.get().strip().lower()
        self._names = []
        self._members.clear()
        for name in self._all_icons():
            if not kw or kw in name.lower():
                self._names.append(name)
        self.listbox.delete(0, tk.END)
        for name in self._names[:self.MAX_ROWS]:
            members = sorted(self._members_of(name) - {'其他'})
            tag = f"  [{ '|'.join(members) }]" if members else ''
            self.listbox.insert(tk.END, f"{name}{tag}")
        if self._names:
            self.listbox.selection_clear(0, tk.END)
            self.listbox.selection_set(0)

    def _on_key(self, event):
        if event.keysym in ('Up', 'Down', 'Return', 'Escape'):
            return
        self._refresh_results()

    def _move(self, delta: int):
        sel = self.listbox.curselection()
        # ★R313 G4：取模按**实际渲染的行数**（列表框只装前 MAX_ROWS 条），
        # 不是命中总数。旧写法 `% len(self._names)`：桌面 >40 个图标时，
        # 在第 41 条以后按 Down、或在第一行按 Up，都会算出一个越界下标 ⇒
        # `selection_set(越界)` 在 Tk 里是**静默不选**（不报错），列表框
        # 因此"一个选中项都没有"，接着回车就把用户指的那条跳成了第 1 条。
        n = min(len(self._names), self.MAX_ROWS)
        if n <= 0:
            return
        i = (sel[0] + delta) % n if sel else 0
        self.listbox.selection_clear(0, tk.END)
        self.listbox.selection_set(i)
        self.listbox.see(i)

    def _jump(self):
        sel = self.listbox.curselection()
        if not self._names:
            return
        if not sel:
            # ★R313 G4：没有选中项就**什么都不做**。旧写法 `i = sel[0] if sel else 0`
            # 回落到第 0 条——用户看着列表里第 40 条按回车，桌面上被选中的却是
            # 另一个文件（接着双击就开错程序）。宁可没反应，也不能跳错。
            return
        name = self._names[sel[0]]
        # 桌面切到前台，选中该图标，方框闪 3 下（R142）
        ok = False
        rect = None
        try:
            prog = win32gui_FindWindow('Progman')
            if prog:
                import ctypes
                from utils import keystate
                u = ctypes.windll.user32
                # ★R229（用户 01 批 item3）：原来无条件"假按 Alt→置前台→抬起 Alt"，
                # down/up 之间没有 try/finally——中途抛异常就把全系统的 Alt 卡在
                # 按下态（实测取证 E3：不补 key-up 不会自动恢复），表现正是用户说的
                # "记事本打不进字、自动往回退"。现在：①先直接置前台，成功就不注入；
                # ②真要借 Alt 绕前台锁，也只走 keystate.bounce_alt()（finally 配对
                # + 事后巡检），任何异常都回得到静息态。
                if not u.SetForegroundWindow(prog):
                    with keystate.bounce_alt():
                        u.SetForegroundWindow(prog)
                import time
                time.sleep(0.2)   # ★R204 ROOT-18：0.4→0.2（主线程卡顿减半）
            ok = self.icon_manager.select_icon_by_name(name)
            if ok:
                # ★ R151：方框位置=此刻实时读回的矩形（R85 教条）——
                # 不再用 highlighter 缓存（Explorer 重排后名字→索引错位，
                # 框会画到别的图标上，用户实测）
                rect = self.icon_manager.get_icon_rect_by_name(name)
        except Exception as e:
            # ★R232（用户 06 批 item1）：原来是裸 `pass`。这条路正是"借 Alt 把
            #   桌面拽到前台"的那条，出错却一句不留 ⇒ 用户报的"打字不上"无从归因。
            #   现在留 pid + 当时的修饰键快照（不弹窗、不改"失败就提示重试"的行为）。
            import logging
            import os as _os
            try:
                from utils import keystate as _ks
                _mods = _ks.stuck_modifiers()
            except Exception:
                _mods = []
            logging.getLogger('desktop_manager').warning(
                f'[quick_find] 跳到桌面图标过程出错（已按失败处理）: {e!r}'
                f'｜pid={_os.getpid()}｜仍按下={_mods}')
        if ok:
            self.destroy()
            if rect:
                _flash_icon_rect(self.master, rect, name)
        else:
            # ★ R143：失败不再静默关窗——桌面忙时映射可能暂时缺名，
            # 提示+刷新结果让用户直接重试（此前静默无任何反馈）
            self.hint_var.set(f'✗ 未选中「{name}」（桌面忙或名字已变），已刷新结果，请再试')
            self._refresh_results()
            self.entry.focus_force()


def win32gui_FindWindow(cls):
    import win32gui
    return win32gui.FindWindow(cls, None)


def _flash_icon_rect(root, rect, name):
    """专属提醒（R151）：4 条边拼方框圈住目标图标 + 上方"▼ 名字"标签，
    闪 3 次后常显 1.8s 自动消失。独立顶层窗（overrideredirect+置顶），
    与 highlighter 完全解耦——框位=实时读回的图标矩形（R85 教条）。

    坐标口径（★R219 收口）：rect 来自 Explorer 的 LVM_GETITEMRECT，**恒为物理
    像素**；Tk 的 geometry 用的是本进程感知档位所决定的坐标系。两者只有在
    "进程=PMv2"或"缩放=100%"时重合，125% + 进程被降级时框会按 1.25 倍偏
    （用户实测"查找桌面图标框不准"）。故一律过 dpi_geom.to_tk_rect 换算，
    PMv2/100% 下比例恒为 1、零行为改变。"""
    try:
        from core.dpi_geom import to_tk_rect
        l, tp, r, b = to_tk_rect([int(v) for v in rect],
                                 root.winfo_screenwidth())
    except Exception:
        return

    def _strip(x, y, w, h):
        wtop = tk.Toplevel(root)
        wtop.overrideredirect(True)
        wtop.attributes('-topmost', True)
        wtop.configure(bg=COLORS['primary'])
        wtop.geometry('%dx%d+%d+%d' % (max(2, w), max(2, h), x, y))
        return wtop

    pad = 6
    th = 3
    L, T, R, B = l - pad, tp - pad, r + pad, b + pad
    wins = [
        _strip(L, T, R - L, th),            # 上
        _strip(L, B - th, R - L, th),       # 下
        _strip(L, T, th, B - T),            # 左
        _strip(R - th, T, th, B - T),       # 右
    ]
    lbl = tk.Toplevel(root)
    lbl.overrideredirect(True)
    lbl.attributes('-topmost', True)
    lbl.configure(bg=COLORS['bg'])
    tk.Label(lbl, text='▼ ' + name, fg=COLORS['primary'], bg=COLORS['bg'],
             font=ui_layout.app_font(10, 'bold')).pack(padx=6, pady=2)
    lbl.update_idletasks()
    lw = lbl.winfo_reqwidth()
    lbl.geometry('+%d+%d' % (max(0, (L + R) // 2 - lw // 2), max(0, T - 36)))
    wins.append(lbl)

    def _show(vis):
        for w in wins:
            if not w.winfo_exists():
                return
            (w.deiconify if vis else w.withdraw)()

    seq = [(True, 350), (False, 180), (True, 350), (False, 180),
           (True, 350), (False, 180), (True, 1800)]
    delay = 0
    for vis, ms in seq:
        delay += ms
        root.after(delay, lambda v=vis: _show(v))
    root.after(delay + 150, lambda: [w.destroy() for w in wins
                                     if w.winfo_exists()])
