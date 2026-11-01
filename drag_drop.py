"""拖拽分类面板 - 把桌面图标拖到分类里，支持首字母前缀规则"""
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, messagebox, simpledialog
from typing import List, Dict, Optional
import sys
import json
import os
import logging
from pathlib import Path

# 添加模块路径
from utils.app_paths import get_app_dir  # ★ R65-Bug1：frozen 时 __file__ 指向 _MEIPASS
APP_DIR = get_app_dir()
sys.path.insert(0, str(APP_DIR))

from core.classifier import (get_all_desktop_icons, get_suggested_categories,
                        classify_icon, PrefixRules)
from core.category_manager import CategoryManager
from core.icon_manager import VIRTUAL_DESKTOP_ICONS
from utils import i18n   # ★ R73 双语
from gui import ui_layout   # ★R228：界面字号等比缩放 helper

# 与 main_window 共用同一 logger（落盘 data/app.log）
logger = logging.getLogger("desktop_manager")

# 主题色引用：main_window 启动时 set_colors(COLORS) 注入**同一 dict 对象**
# （apply_ui_theme 原地换色自动跟随）。
# ★R221（NEW7-P0-2）：兜底表必须覆盖本模块每一处 COLORS 下标直取键。R215 把
# token `border` 改名 `text_secondary` 时只补了 theme_panel 真身，这里漏补 →
# DragDropPanel/PrefixRuleDialog 一构建就 KeyError，"🖱 拖拽分类"整页功能死掉；
# 且 `_panel_instance` 赋值在 :1373 到不了，每次点击都泄漏一个 1160x840 空窗。
COLORS = {
    'bg': '#282a36',
    'fg': '#f8f8f2',
    'primary': '#8be9fd',
    'secondary': '#50fa7b',
    'surface': '#44475a',
    'border': '#636d83',
    'text_secondary': '#bcc3d6',
    # ★R224：待保存计数 Label 改取 warning（R223 扫尾），兜底表同步补键
    'warning': '#ffb86c',
}


def set_colors(colors: dict):
    global COLORS
    COLORS = colors


class PrefixRuleDialog(tk.Toplevel):
    """首字母前缀规则编辑器：图标名以某前缀开头 → 自动归入对应分类"""

    def __init__(self, parent, prefix_rules: PrefixRules, on_changed=None):
        super().__init__(parent)
        self.prefix_rules = prefix_rules
        self.on_changed = on_changed or (lambda: None)

        self.title('🔤 首字母前缀规则')
        self.geometry('420x380')
        self.configure(bg=COLORS['bg'])
        self.transient(parent)
        # ★R221（NEW7-P0-3）：grab_set 挪到 __init__ 末尾。原来它在 UI 构建
        # 之前执行，下面任何一行抛异常（本轮就是 COLORS['text_secondary']
        # KeyError）都会留下一个"空对话框持有应用级模态抓"的窗——实测
        # grab status = 'local'，用户看到的是整个程序像卡死。

        ttk.Label(self, text=i18n.tr('图标名以指定前缀开头，即自动归入对应分类（不区分大小写）\n例如 w → 网络：W网络工具2 会被分到网络',
                                     'Icons starting with a prefix go to that category '
                                     'automatically (case-insensitive).\ne.g. w → 网络: '
                                     '"W网络工具2" goes to 网络'),
                  foreground=COLORS['text_secondary'],
                  font=ui_layout.app_font(9)).pack(anchor=tk.W, padx=10, pady=(10, 5))

        # 规则列表
        self.rule_list = tk.Listbox(self, font=('Consolas', 11),
                                     bg=COLORS['surface'], fg=COLORS['fg'],
                                     relief=tk.FLAT, highlightthickness=0)
        self.rule_list.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        # 编辑行
        edit = ttk.Frame(self)
        edit.pack(fill=tk.X, padx=10, pady=5)
        ttk.Label(edit, text=i18n.tr('前缀:', 'Prefix:')).pack(side=tk.LEFT)
        self.prefix_var = tk.StringVar()
        ttk.Entry(edit, textvariable=self.prefix_var, width=8).pack(side=tk.LEFT, padx=(3, 12))
        ttk.Label(edit, text=i18n.tr('分类:', 'Category:')).pack(side=tk.LEFT)
        self.cat_var = tk.StringVar()
        self.cat_combo = ttk.Combobox(edit, textvariable=self.cat_var, width=10,
                                       state='readonly')
        self.cat_combo.pack(side=tk.LEFT, padx=3)

        btns = ttk.Frame(self)
        btns.pack(fill=tk.X, padx=10, pady=(0, 10))
        ttk.Button(btns, text=i18n.tr('➕ 添加/更新', '➕ Add/Update'), command=self._on_add).pack(side=tk.LEFT, padx=2)
        ttk.Button(btns, text=i18n.tr('🗑️ 删除选中', '🗑️ Delete Selected'), command=self._on_delete).pack(side=tk.LEFT, padx=2)
        ttk.Button(btns, text=i18n.tr('关闭', 'Close'), command=self.destroy).pack(side=tk.RIGHT, padx=2)

        self.rule_list.bind('<<ListboxSelect>>', self._on_select)
        self._refresh_cats()
        self._refresh()
        self.grab_set()   # ★R221：UI 建完才允许抓（见 __init__ 开头注释）

    def _refresh_cats(self):
        cats = [c['name'] for c in self._categories()]
        self.cat_combo['values'] = cats
        if cats and not self.cat_var.get():
            self.cat_var.set(cats[0])

    def _categories(self):
        # 借调用方传入的 category_manager 不方便，直接从 rules 值 + 常用分类合成
        cm = getattr(self.prefix_rules, '_cm', None)
        if cm:
            return cm.get_all_categories()
        return [{'name': n} for n in ['程序', '网络', '视频', '英语', 'AI', '学习', '其他']]

    def _refresh(self):
        # ★R222 P4-3：早期写法是 f'{p!r:<6} → {c}'，!r 给前缀套上 Python 引号，
        # 用户看到 "'a'  →  AI"；且下方 _on_select/_on_delete 靠反解这段文本取
        # 前缀（strip("'")），一旦前缀本身含引号或箭头就会错删。现在行文本只做
        # 展示，操作一律走 self._rule_rows 下标，空列表补一行占位提示。
        self.rule_list.delete(0, tk.END)
        rows = sorted(self.prefix_rules.all().items())
        self._rule_rows = rows
        if not rows:
            self.rule_list.insert(
                tk.END, i18n.tr('（还没有规则：在下方填前缀、选分类，点「➕ 添加/更新」）',
                                '(no rules yet: type a prefix, pick a category, '
                                'then click ➕ Add/Update)'))
            # 注：Listbox 的 itemconfigure 不支持 -state（Tk 会报
            # unknown option），占位行只降色；不可选中由 _selected_rule()
            # 的"下标越界即 None"兜住。
            self.rule_list.itemconfig(0, foreground=COLORS['text_secondary'])
            return
        for p, c in rows:
            self.rule_list.insert(tk.END, f'{p:<6} → {c}')

    def _selected_rule(self):
        """当前选中的 (前缀, 分类)；占位行/越界返回 None。"""
        sel = self.rule_list.curselection()
        if not sel or not (0 <= sel[0] < len(getattr(self, '_rule_rows', ()))):
            return None
        return self._rule_rows[sel[0]]

    def _on_select(self, event=None):
        row = self._selected_rule()
        if row is None:
            return
        self.prefix_var.set(row[0])
        self.cat_var.set(row[1])

    def _on_add(self):
        prefix = self.prefix_var.get().strip()
        cat = self.cat_var.get().strip()
        if not prefix or not cat:
            messagebox.showwarning('提示', '请填写前缀并选择分类', parent=self)
            return
        saved = self.prefix_rules.set_rule(prefix, cat)
        self._refresh()
        self.on_changed()
        if saved:
            self.prefix_var.set('')
        else:
            # ★R313 S6 回补 P-D：`set_rule` 现在会把"没落盘"报出来。旧写法不看
            # 返回值，改完照刷 ⇒ 用户读成"存好了"，重启就回到改动前（★R284-2 同族）。
            messagebox.showwarning('提示', '这条规则没写进文件（可能被占用或只读），'
                                          '重启后会回到改动前', parent=self)

    def _on_delete(self):
        row = self._selected_rule()
        if row is None:
            messagebox.showwarning('提示', '请先选择一条规则', parent=self)
            return
        removed = self.prefix_rules.delete_rule(row[0])
        self._refresh()
        self.on_changed()
        if not removed:
            messagebox.showwarning('提示', '这条删除没写进文件（可能被占用或只读），'
                                          '重启后会回到改动前', parent=self)


class DragDropPanel(tk.Toplevel):
    """拖拽分类面板 — 按住桌面图标拖到右侧分类上松手即可归类"""

    DRAG_THRESHOLD = 8  # 触发拖拽的移动像素

    def __init__(self, parent, icon_manager, category_manager: CategoryManager,
                 highlighter=None, on_changed=None):
        super().__init__(parent)
        self.icon_manager = icon_manager
        self.category_manager = category_manager
        self.highlighter = highlighter
        # ★ #2：分类/归属写入后回调主窗口刷新分类图标数
        #   （主窗口注入 _on_drag_panel_changed）。未注入时退化为空操作。
        self.on_changed = on_changed or (lambda: None)
        self.parent = parent
        self.prefix_rules = PrefixRules(str(APP_DIR / 'config'))
        self.prefix_rules._cm = category_manager  # 供规则对话框读取分类列表

        self.title('🖱️ 图标拖拽分类')
        # 默认开大一点，保证左侧图标列表与底部按钮尽量全部显示
        self.geometry('1160x840')
        # ★ R72：左栏加宽到 640 后同步抬高最小宽度（860 时右侧分类区
        # 只剩 ~180px，两列列表挤成一团）
        self.minsize(1024, 680)
        self.configure(bg=COLORS['bg'])
        self.resizable(True, True)
        # 界面字体跟随主界面设置（小9/标准11/大13）
        try:
            with open(os.path.join(str(APP_DIR), 'config', 'app_settings.json'),
                      encoding='utf-8') as f:
                _size = int(json.load(f).get('ui_font_size', 11))
        except Exception:
            _size = 11
        self.ui_font = tkfont.Font(family='Microsoft YaHei UI', size=_size)
        # ★R215（UI 美化阶段1）：小字号下限 8→9——中文 8pt 在深色主题下发虚，
        # 且"小"档（_size=9）时 max(8, 7)＝7pt 根本读不清
        self.small_font = tkfont.Font(family='Microsoft YaHei UI',
                                      size=max(9, _size - 2))

        # 状态
        self._icons_cache: List[Dict[str, object]] = []   # 图标列表缓存
        self._drag_active = False
        self._drag_pressed = False
        self._drag_start = (0, 0)
        self._drag_names: List[str] = []
        self._ghost = None
        self._drop_target_idx = -1
        # 待保存的归属变更：{图标名: 目标分类集合}（点"保存"才真正写入，
        # 支持一个图标同属多个分类）
        self._pending: Dict[str, set] = {}
        self._icon_filter = ''          # 搜索过滤词
        self._display_idx: List[int] = []  # 当前显示行 → _icons_cache 索引映射

        # 定位显示：默认放主窗右侧，尽量不遮挡主窗口主体；
        # 屏幕空间不足时退化为"右下偏移"叠放（主窗左缘仍可见）。
        self.update_idletasks()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        px, py = parent.winfo_x(), parent.winfo_y()
        sw = self.winfo_screenwidth()
        try:
            x = px + pw + 10
            if x + 1160 > sw:
                x = px + 50            # 右侧放不下 → 右下偏移，避免盖死主窗
        except Exception:
            x = px + 50
        y = max(0, py - 10)
        self.geometry(f'+{x}+{y}')

        self._build_ui()
        self._refresh_icon_list()
        self._refresh_category_list()
        self.protocol('WM_DELETE_WINDOW', self._on_close)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _sync_btn_styles(self):
        """★R222 P4-5：克隆本面板专用的两档按钮样式（带 small_font 字号）。

        ttk.Button **不接受** `-font` 选项——传了就 `TclError: unknown option
        "-font"`，整个拖拽面板构造失败（本轮实测：6 项 UI 测试当场红）。
        字号只能挂在样式上，故把 secondary/error.TButton 的现有配置整份拷到
        一个按字号命名的变体再覆写 font。<Map> 时重刷一次，换肤（_setup_styles
        重跑基样式）后再打开面板不会停在旧色板。
        """
        st = ttk.Style(self)
        sz = self.small_font.actual('size')
        self._btn_styles = {}
        for base in ('secondary.TButton', 'error.TButton'):
            name = 'DD%d.%s' % (sz, base)
            try:
                opts = dict(st.configure(base) or {})
                opts.pop('font', None)
                st.configure(name, font=self.small_font, **opts)
            except Exception:
                logger.exception('[drag_drop] 按钮样式变体配置失败 %s', name)
                name = base
            self._btn_styles[base] = name

    def _btn_style(self, base):
        return getattr(self, '_btn_styles', {}).get(base, base)

    def _build_ui(self):
        # ★ 工具栏三列 grid：列0=左侧按钮组(固定) / 列1=状态文本(吸收余量，
        #   空间不足时优先被压缩) / 列2="还原/保存更改"(固定)。
        #   此前 tb_left 先 pack 且 expand：status_var/pending_var 文本一变长
        #   （如点"清空图标"后写入长提示），pack 按声明顺序预留宽度就把后
        #   声明的 tb_right 挤压变位。grid 固定列不随文本变化，两按钮恒定。
        toolbar = ttk.Frame(self, padding='8')
        toolbar.pack(fill=tk.X)
        toolbar.grid_columnconfigure(0, weight=0)
        toolbar.grid_columnconfigure(1, weight=1)
        toolbar.grid_columnconfigure(2, weight=0)

        btns_left = ttk.Frame(toolbar)
        btns_left.grid(row=0, column=0, sticky='w')

        ttk.Button(btns_left, text=i18n.tr('🔄 刷新', '🔄 Refresh'),
                   command=self._refresh_all, width=8).pack(side=tk.LEFT, padx=3)
        ttk.Button(btns_left, text=i18n.tr('✨ 智能自动分类', '✨ Auto Sort'),
                   command=self._on_auto_classify,
                   width=15).pack(side=tk.LEFT, padx=3)
        ttk.Button(btns_left, text=i18n.tr('🔤 首字母规则', '🔤 Initial Rules'),
                   command=self._on_prefix_rules,
                   width=13).pack(side=tk.LEFT, padx=3)

        # 筛选：只显示未分类图标（★ 用 tk.Checkbutton：ttk 在深色主题下勾选标记不可见）
        self.filter_none_var = tk.BooleanVar(value=False)
        tk.Checkbutton(btns_left, text=i18n.tr('仅显示未分类', 'Uncategorized only'),
                       variable=self.filter_none_var,
                       command=self._refresh_icon_list,
                       bg=COLORS['bg'], fg=COLORS['fg'],
                       selectcolor=COLORS['surface'],
                       activebackground=COLORS['bg'],
                       activeforeground=COLORS['fg'],
                       font=ui_layout.app_font(9)).pack(side=tk.LEFT, padx=(15, 0))

        # 状态文本区：待保存计数放前（持久状态），提示文本放后（瞬态）——
        # 列宽不足时 pack 后声明者先被裁切，牺牲瞬态提示保住计数
        status_zone = ttk.Frame(toolbar)
        status_zone.grid(row=0, column=1, sticky='ew')

        self.pending_var = tk.StringVar(value='')
        ttk.Label(status_zone, textvariable=self.pending_var,
                  foreground=COLORS['warning'],
                  font=ui_layout.app_font(9, 'bold')).pack(side=tk.LEFT, padx=(15, 0))

        self.status_var = tk.StringVar(value=i18n.tr('就绪', 'Ready'))
        ttk.Label(status_zone, textvariable=self.status_var,
                  foreground=COLORS['primary'],
                  font=ui_layout.app_font(9)).pack(side=tk.LEFT, padx=(15, 0))

        btns_right = ttk.Frame(toolbar)
        btns_right.grid(row=0, column=2, sticky='e')

        ttk.Button(btns_right, text=i18n.tr('↩️ 还原', '↩️ Revert'),
                   command=self._discard_pending).pack(side=tk.LEFT, padx=3)
        ttk.Button(btns_right, text=i18n.tr('💾 保存更改', '💾 Save Changes'),
                   command=self._commit_pending).pack(side=tk.LEFT, padx=3)

        # 三层级竖向滚动条：桌面图标(默认粗) → 分类(约80%) → 最右图标区(约50%)，
        # 从左到右逐渐变细，表达"子菜单"层级递进。仅作用于本 Toplevel。
        # ★ 修复"改崩/啥都不显示"：自定义滚动条样式必须先从系统
        #   Vertical.TScrollbar 继承 layout，否则 style.configure 只建了"空样式"、
        #   没有布局，ttk.Scrollbar 生成时会抛
        #   "Layout Vertical.Register.TScrollbar not found"，中断 _build_ui，
        #   导致左侧图标列表、右侧分类内容全部渲染不出来。
        #   注册失败时退化为系统默认滚动条，绝不阻断界面构建。
        # ★ 滚动条按列分级：桌面图标=20px(100%) → 分类=18px(90%) → 该分类图标=14px(70%)。
        #   用 tk.Scrollbar(width=) 直接控制像素宽度（ttk.Scrollbar 不支持 width）。
        #   按钮小窗格的滚动条与正下方列表用同款宽度。
        _SB_W_BASE = 20    # 桌面图标（基准 100%）
        _SB_W_MID = 18     # 分类（90%）
        _SB_W_THIN = 14    # 该分类图标（70%）

        main = ttk.Frame(self, padding='8')
        main.pack(fill=tk.BOTH, expand=True)
        # ★ R72c（用户定案）：三列全程按比例缩放，比例=桌面图标:分类:该分类
        # 图标 = 2:1:2（两侧等宽显示图标名、中间最小）——最大化也不走样。
        # 右侧"📂 分类"框跨中+右两列（外层 1:2），其内部再按 1:2 分两列，
        # 合成效果即 2:1:2。
        main.grid_columnconfigure(0, weight=2)
        main.grid_columnconfigure(1, weight=1)
        main.grid_columnconfigure(2, weight=2)
        main.grid_rowconfigure(0, weight=1)

        # 左：桌面图标列表
        left = tk.LabelFrame(main, text=i18n.tr('🖥️ 桌面图标（按住拖到右侧分类）', '🖥️ Desktop Icons (drag onto a category)'),
                              padx=8, pady=8, bg=COLORS['surface'], fg=COLORS['fg'])
        # 列宽由外层 grid 权重按比例分配，不再定死像素
        left.grid(row=0, column=0, sticky='nsew', padx=(0, 5))

        self.filter_var = tk.StringVar()
        filter_entry = ttk.Entry(left, textvariable=self.filter_var)
        filter_entry.pack(fill=tk.X, padx=5, pady=(2, 0))
        filter_entry.bind('<KeyRelease>', lambda e: self._refresh_icon_list())
        ttk.Label(left, text=i18n.tr('🔎 搜索:', '🔎 Search:'), foreground=COLORS['text_secondary'],
                  font=ui_layout.app_font(9)).pack(anchor=tk.W, padx=6)

        icon_row = ttk.Frame(left)
        icon_row.pack(fill=tk.BOTH, expand=True, padx=5, pady=(4, 5))

        self.icon_list = tk.Listbox(icon_row,
                                     font=self.ui_font,
                                     bg=COLORS['surface'],
                                     fg=COLORS['fg'],
                                     selectmode=tk.EXTENDED,
                                     relief=tk.FLAT,
                                     highlightthickness=0,
                                     exportselection=False,
                                     activestyle='none',
                                     # ★ R72：请求宽按 48 字符封顶——此前由
                                     # 最长图标名撑宽；★ R72c 权重布局下
                                     # 请求宽=列最小宽，封顶值须低于最小
                                     # 窗口的按比例宽度，比例才不被顶住
                                     width=48)
        hscroll = ttk.Scrollbar(icon_row, orient=tk.HORIZONTAL,
                                 command=self.icon_list.xview)
        vscroll = tk.Scrollbar(icon_row, orient=tk.VERTICAL, width=_SB_W_BASE,
                                command=self.icon_list.yview)
        self.icon_list.configure(xscrollcommand=hscroll.set,
                                  yscrollcommand=vscroll.set)
        self.icon_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vscroll.pack(side=tk.RIGHT, fill=tk.Y)
        hscroll.pack(side=tk.BOTTOM, fill=tk.X)

        self.icon_list.bind('<Double-Button-1>', self._on_icon_double_click)
        self.icon_list.bind('<<ListboxSelect>>', self._on_icon_select)
        # 真拖拽支持
        self.icon_list.bind('<Button-1>', self._on_drag_press)
        self.icon_list.bind('<B1-Motion>', self._on_drag_motion)
        self.icon_list.bind('<ButtonRelease-1>', self._on_drag_release)

        # 右：分类区域（田字两列四格，grid 均匀两行两列）
        #   顶行两格对齐 → "新建分类" 与 "移出分类" 上对齐
        #   底行两格对齐 → 分类列表 与 "该分类中的图标" 上对齐
        #   ★ R72c：跨中+右两列（外层权重 1:2），内部两列也 1:2，
        #   合成"分类列最窄、该分类图标列与左栏等宽"
        right = tk.LabelFrame(main, text=i18n.tr('📂 分类', '📂 Categories'),
                               padx=6, pady=6, bg=COLORS['surface'], fg=COLORS['fg'])
        right.grid(row=0, column=1, columnspan=2, sticky='nsew', padx=(5, 0))
        # ★ R72b：右区内两列 1:2——分类列表列最窄（中间最小），该分类
        # 图标列拿双倍宽（要显示完整图标名，与左栏桌面图标列基本等宽）
        right.grid_columnconfigure(0, weight=1)
        right.grid_columnconfigure(1, weight=2)
        right.grid_rowconfigure(0, weight=0)      # 按钮区紧凑
        right.grid_rowconfigure(1, weight=0)      # 分隔条
        right.grid_rowconfigure(2, weight=1)      # 列表区撑满

        # 可滚动按钮窗格：窗格高度固定为 visible×单按钮高（与窗口大小无关），
        # 内容超出才出滚动条（见 _fit_pane）。
        def _pane(parent, sb_width):
            wrap = tk.Frame(parent, bg=COLORS['surface'])
            cv = tk.Canvas(wrap, bg=COLORS['surface'], highlightthickness=0)
            cv.bind('<MouseWheel>', lambda e: cv.yview_scroll(-int(e.delta // 120), 'units'))
            dv = tk.Scrollbar(wrap, orient=tk.VERTICAL, width=sb_width,
                              command=cv.yview, bg=COLORS['surface'])
            inner = tk.Frame(cv, bg=COLORS['surface'])
            _win = cv.create_window((0, 0), window=inner, anchor='nw')
            inner.bind('<Configure>', lambda e: cv.configure(scrollregion=cv.bbox('all')))
            cv.bind('<Configure>', lambda e: cv.itemconfigure(_win, width=e.width))
            cv.configure(yscrollcommand=dv.set)
            cv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            # 滚动条初始不挂：显隐完全由 _fit_pane 依据"内容 vs 窗格高度"决定
            return wrap, inner, cv, dv

        def _fit_pane(cv, inner, dv, visible=5, tag=''):
            """固定窗格高度 = visible×单按钮高；内容超出才出滚动条。

            ★ 判据只依赖"内容高度 vs 窗格高度"，与窗口大小/最大化无关——
              修复"默认窗口不显下拉条、最大化反而出现"的几何脱节。
            ★ 自愈复测时机：构建同步 1 次 + 250/700ms 兜底 + <Map> + 窗格/内容
              尺寸变化（去抖 150ms）。此前只测两次且之后永不重估，状态可能
              停在映射前的错误测量值上（用户实测：默认不显、最大化才显）。
            ★ 显隐判据用 winfo_manager()（是否仍在 pack 管理中）——
              winfo_ismapped() 在窗口映射前恒 0，曾令 pack_forget 被跳过，
              滚动条"不该显常显"。每次判定结果落日志，便于实机取证。"""
            state = {'timer': None}

            def _cancel():
                if state['timer'] is not None:
                    try:
                        self.after_cancel(state['timer'])
                    except Exception:
                        pass
                    state['timer'] = None

            def _apply():
                state['timer'] = None
                try:
                    kids = inner.winfo_children()
                    if not kids:
                        cv.configure(height=100)
                        if dv.winfo_manager():
                            dv.pack_forget()
                        return
                    inner.update_idletasks()
                    total = inner.winfo_reqheight()
                    per = max(1, total / len(kids))
                    h = int(round(per * visible))
                    # 若被外层 grid 压缩，实际可用高度可能小于 h：
                    # 以较小者判溢出，保证被裁内容始终可通过滚动条到达
                    avail = cv.winfo_height() if cv.winfo_ismapped() else h
                    avail = max(1, min(h, avail)) if avail > 0 else h
                    needs = total > avail + 2
                    cv.configure(height=h)
                    cv.configure(scrollregion=(0, 0, inner.winfo_reqwidth(), total))
                    if needs:
                        if not dv.winfo_manager():
                            dv.pack(side=tk.RIGHT, fill=tk.Y)
                        dv.set(0.0, min(1.0, avail / total))
                    else:
                        if dv.winfo_manager():
                            dv.pack_forget()
                    logger.info(f'[drag_drop] 窗格[{tag}] 内容高={total} '
                                f'窗格高={h} 实际可用={avail} 出滚动条={needs}')
                except Exception:
                    logger.exception(f'[drag_drop] 窗格[{tag}] 测量失败')

            def _run_debounced():
                state['timer'] = None
                _apply()

            def _debounced(delay=150):
                _cancel()
                state['timer'] = self.after(delay, _run_debounced)

            _apply()                      # 同步首测（映射前，可能不准，后续复测纠正）
            self.after(250, _apply)       # 映射后复测
            self.after(700, _apply)       # 兜底（字体/主题慢加载场景）
            # add='+'：不覆盖 _pane 里已绑的宽度同步 / scrollregion 更新
            cv.bind('<Map>', lambda e: _debounced(120), add='+')
            cv.bind('<Configure>', lambda e: _debounced(150), add='+')
            inner.bind('<Configure>', lambda e: _debounced(150), add='+')

        # ── 左上格：分类操作按钮（可上下滚动，90%滚动条，与下方分类列表同宽）──
        self._sync_btn_styles()
        self.bind('<Map>', lambda e: self._sync_btn_styles(), add='+')
        cat_pane, cat_inner, cat_cv, cat_dv = _pane(right, _SB_W_MID)
        cat_pane.grid(row=0, column=0, sticky='nsew', padx=(0, 4), pady=2)
        for _t, _c, _st in (
                (i18n.tr('➕ 新建分类', '➕ New Category'), self._on_new_category,
                 'secondary.TButton'),
                (i18n.tr('✏️ 重命名分类', '✏️ Rename Category'), self._on_rename_category,
                 'secondary.TButton'),
                (i18n.tr('🗑️ 删除分类', '🗑️ Delete Category'), self._on_delete_category,
                 'error.TButton'),
                (i18n.tr('🎨 改颜色', '🎨 Color'), self._on_recolor_category,
                 'secondary.TButton'),
            ):
            # ★R222 P4-5：整排 tk.Button 灰/绿实底 → 与主窗 R215 三档层级脱节。
            # 改用同一套 ttk 样式（常规=描边 secondary，破坏=实底 error）；
            # 字号走样式变体（ttk.Button 不吃 -font，见 _sync_btn_styles）。
            ttk.Button(cat_inner, text=_t, command=_c,
                       style=self._btn_style(_st)).pack(fill=tk.X, pady=2)
        if self.highlighter:
            for _t, _c in (
                    (i18n.tr('🖍️ 高亮该分类', '🖍️ Highlight Category'), self._on_highlight_category),
                    (i18n.tr('🚫 清除高亮', '🚫 Clear Highlight'), self._on_clear_highlight),
                ):
                ttk.Button(cat_inner, text=_t, command=_c,
                           style=self._btn_style('secondary.TButton')
                           ).pack(fill=tk.X, pady=2)
        # visible=6：★ R79 新增"🎨 改颜色"后共 6 个按钮全部显示、无滚动条
        # （用户拍板 2026-09-12：按钮全显示；再超宽 _fit_pane 自动出滚动条）
        _fit_pane(cat_cv, cat_inner, cat_dv, visible=6, tag='分类操作')

        # ── 横向分隔条：按钮区与列表区之间 ──
        ttk.Separator(right, orient=tk.HORIZONTAL).grid(
            row=1, column=0, columnspan=2, sticky='ew', padx=2, pady=2)

        # ── 左下格：分类列表（分类名 "程序/网络/..."）──
        cat_area = ttk.Frame(right)
        cat_area.grid(row=2, column=0, sticky='nsew', padx=(2, 0), pady=2)
        self.cat_list = tk.Listbox(cat_area,
                                    font=self.ui_font,
                                    bg=COLORS['surface'],
                                    fg=COLORS['fg'],
                                    selectmode=tk.SINGLE,
                                    relief=tk.FLAT,
                                    highlightthickness=0,
                                    width=16,
                                    height=5,
                                    exportselection=False)
        cscroll = tk.Scrollbar(cat_area, orient=tk.VERTICAL, width=_SB_W_MID,
                               command=self.cat_list.yview, bg=COLORS['surface'])
        self.cat_list.configure(yscrollcommand=cscroll.set)
        self.cat_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        cscroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.cat_list.bind('<<ListboxSelect>>', self._on_category_select)

        # ── 右上格：图标操作按钮（可上下滚动，70%滚动条，与下方图标列表同宽）──
        icon_pane, icon_inner, icon_cv, icon_dv = _pane(right, _SB_W_THIN)
        icon_pane.grid(row=0, column=1, sticky='nsew', padx=(4, 0), pady=2)
        for _t, _c, _st in (
                (i18n.tr('➖ 移出分类', '➖ Remove from Category'),
                 self._on_remove_from_category, 'secondary.TButton'),
                (i18n.tr('📤 移动到', '📤 Move to'),
                 self._on_move_to_category, 'secondary.TButton'),
                (i18n.tr('🧹 清空图标', '🧹 Clear Icons'),
                 self._on_clear_category, 'error.TButton'),
                (i18n.tr('☑️ 全选图标', '☑️ Select All Icons'),
                 self._on_select_all_icons, 'secondary.TButton'),
                (i18n.tr('☐ 取消全选', '☐ Deselect All'),
                 self._on_deselect_all_icons, 'secondary.TButton'),
            ):
            # ★R222 P4-5：5 枚全绿实底 → 一排彩色实心块＝噪音，看不出哪个是危险操作。
            # 中性动作走描边 secondary，破坏性（清空）走实底 error；同时干掉写死的
            # 深绿/砖红十六进制色（浅色主题下对比度不足、也不跟主题切换）与 i18n 缺失。
            ttk.Button(icon_inner, text=_t, command=_c,
                       style=self._btn_style(_st)).pack(fill=tk.X, pady=2)
        # visible=5：与左窗格同口径，第 6 个按钮自动出滚动条
        _fit_pane(icon_cv, icon_inner, icon_dv, visible=5, tag='图标操作')
        # 旧"以左列高度强设右列"的同步块已删：两窗格现在用同一确定性公式
        # （同字体同 pady → 单按钮等高），且映射前强设会覆盖 _fit_pane 的
        # 正确测量；若左列按钮数少于右列（如无高亮功能），强设会裁掉右列按钮。

        # ── 右下格：该分类中的图标（与左侧分类列表上对齐）──
        icon_area = ttk.Frame(right)
        icon_area.grid(row=2, column=1, sticky='nsew', padx=(4, 0), pady=2)
        cat_label = ttk.Label(icon_area, text=i18n.tr('该分类中的图标:', 'Icons in this category:'),
                               foreground=COLORS['primary'],
                               font=self.small_font)
        cat_label.pack(anchor=tk.W, pady=(0, 2))
        self.cat_icon_list = tk.Listbox(icon_area,
                                         font=self.ui_font,
                                         bg=COLORS['surface'],
                                         fg=COLORS['fg'],
                                         selectmode=tk.EXTENDED,
                                         relief=tk.FLAT,
                                         highlightthickness=0,
                                         width=16,
                                         height=5,
                                         exportselection=False)
        cc_scroll = tk.Scrollbar(icon_area, orient=tk.VERTICAL, width=_SB_W_THIN,
                                 command=self.cat_icon_list.yview, bg=COLORS['surface'])
        self.cat_icon_list.configure(yscrollcommand=cc_scroll.set)
        self.cat_icon_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        cc_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        hint = ttk.Frame(self, padding='8')
        hint.pack(fill=tk.X)
        ttk.Label(hint,
                  text=i18n.tr('💡 按住左侧图标拖到右侧分类松手即可加入（可属多个分类）；', '💡 Drag an icon from the left onto a category to assign it (multiple categories allowed);')
                       + i18n.tr('选中左侧图标时，右侧红底显示它所属的分类；', 'select an icon to see its categories highlighted red on the right;')
                       + i18n.tr('改动后点"💾 保存更改"生效，双击图标快速归类', 'click "💾 Save Changes" to apply; double-click for quick assign'),
                  foreground=COLORS['text_secondary'],
                  font=self.small_font).pack(fill=tk.X)

    # ------------------------------------------------------------------
    # 数据刷新
    # ------------------------------------------------------------------

    def _refresh_icon_list(self):
        # 记住当前滚动位置（拖拽后重建列表时恢复，避免跳变）
        try:
            yview = self.icon_list.yview()
        except tk.TclError:
            yview = None
        self.icon_list.delete(0, tk.END)
        self._icons_cache = get_all_desktop_icons(self.icon_manager, self.prefix_rules)
        # ★ #7a：剔除系统图标（此电脑/回收站/网络/控制面板/用户文件夹）——
        #   它们的显隐由"公共分类显示 → 🖥 系统图标"三列网格独立控制，
        #   列在"未分类/待分类"里会让用户误以为需要手动归类。
        #   ★ P2-C：VIRTUAL_DESKTOP_ICONS 里 " Administrator" 是静态别名，
        #   桌面"用户文件夹"的实际显示名 = 当前账户名（换机不同账户会变）。
        #   故过滤集 = 静态系统图标名 ∪ {当前账户名}，换机仍能正确剔除。
        blocked = set(VIRTUAL_DESKTOP_ICONS)
        blocked.add(self.icon_manager.get_user_folder_icon_name())
        self._icons_cache = [ic for ic in self._icons_cache
                             if ic['name'] not in blocked]
        kw = (self.filter_var.get() if hasattr(self, 'filter_var') else '').strip().lower()
        show_none_only = bool(self.filter_none_var.get())
        self._display_idx = []
        row = 0
        for i, icon in enumerate(self._icons_cache):
            if kw and kw not in icon['name'].lower():
                continue
            # tag：列出真实归属；"其他"也视为已分类（与未分类统计口径一致）
            saved = set(self._icon_members(icon['name']))
            pending_extras = self._pending.get(icon['name'], set()) - saved
            all_cats = sorted(saved | pending_extras)
            is_none = not all_cats
            # 仅显示未分类：跳过有分类的图标
            if show_none_only and not is_none:
                continue
            tag = f" [·{'|'.join(all_cats)}·]" if all_cats else ' [未分类]'
            entry = f"{icon['name']}{tag}"
            self.icon_list.insert(tk.END, entry)
            # 未分类项整行浅红底色，便于识别
            if is_none:
                try:
                    self.icon_list.itemconfigure(row, bg='#3d1f1f')
                except tk.TclError:
                    pass
            # 有分类的条目：用第一个分类的颜色作为前景
            # tag 部分用亮白圆点·作视觉分隔，让括号内容与文字产生亮度差
            if all_cats:
                first_cat = all_cats[0]
                color = self.category_manager.get_category_color(first_cat)
                try:
                    self.icon_list.itemconfigure(row, fg=color)
                except tk.TclError:
                    pass
            self._display_idx.append(i)
            row += 1
        self.status_var.set(f'共 {len(self._icons_cache)} 个图标'
                            + (f'，匹配 {row} 个' if kw else ''))
        if show_none_only:
            self.status_var.set(f'📭 未分类：{row} 个'
                                + (f'，匹配 {row} 个' if kw else ''))
        # 恢复滚动位置
        if yview is not None:
            try:
                self.icon_list.yview_moveto(yview[0])
            except tk.TclError:
                pass
        self._apply_member_highlights()
        self._update_dirty_ui()

    # ------------------------------------------------------------------
    # 归属关系（支持多分类 + 待保存机制）
    # ------------------------------------------------------------------

    def _icon_members(self, name: str) -> set:
        """图标当前实际所属的分类集合"""
        return {c['name'] for c in self.category_manager.get_all_categories()
                if name in c.get('icons', [])}

    def _effective_members(self, name: str) -> set:
        """含未保存更改的所属分类（左侧标签/右侧高亮都用它）"""
        if name in self._pending:
            return set(self._pending[name])
        return self._icon_members(name)

    def _set_pending_membership(self, name: str, cats: set):
        """记录目标归属（与现状相同则撤销记录）"""
        if cats == self._icon_members(name):
            self._pending.pop(name, None)
        else:
            self._pending[name] = set(cats)
        self._refresh_icon_list()
        self._refresh_category_list()

    def _commit_pending(self) -> tuple:
        """保存：把待保存的归属变更写入分类配置。

        ★ P1-2：逐图标独立 try，收集失败清单——
        原实现任一图标抛异常即中断全部，且 _pending 未清造成"界面显示已保存
        但实际没落盘"的假象。现每图标隔离，成功项落盘、失败项保留 pending
        供用户重试，任何失败都不阻断整体保存。

        ★ P1-1/P1-2 交互残留：返回 (成功数, 失败名列表)。
        调用方（尤其 _on_close）可据此决定是否提示用户 / 阻止关闭，
        否则用户点"保存并关闭"后失败项静默丢失。
        工具栏按钮 command=self._commit_pending 忽略返回值，无影响。
        """
        if not self._pending:
            self.status_var.set('没有需要保存的更改')
            return 0, []
        n = 0
        failed = []
        for name, cats in list(self._pending.items()):
            try:
                current = self._icon_members(name)
                # ★R191（Qoder ROOT-4）：API 返回 False=分类缺失或落盘失败，
                # 不再无视返回值谎报成功
                ok = True
                for c in current - cats:
                    if not self.category_manager.remove_icon_from_category(c, name):
                        ok = False
                for c in cats - current:
                    if not self.category_manager.add_icon_to_category(c, name):
                        ok = False
                if ok:
                    n += 1
                else:
                    failed.append(name)
                    logger.error(f"保存图标 [{name}] 归属失败（分类缺失或落盘失败）")
            except Exception as e:
                # ★ P3：error 不打堆栈——批量失败时 logger.exception 会刷屏
                # （每项 15~20 行 traceback），文件日志迅速膨胀；
                # 需要定位时单开 logger.debug(..., exc_info=True)。
                logger.error(f"保存图标 [{name}] 归属失败: {e}")
                logger.debug("保存失败详情", exc_info=True)
                failed.append(name)
        if failed:
            # 只保留失败的项；成功项已落盘，不再重复提交
            self._pending = {nm: cs for nm, cs in self._pending.items()
                             if nm in failed}
            self.status_var.set(f'⚠ 有 {len(failed)} 项保存失败，已保留待重试')
        else:
            self._pending.clear()
            self.status_var.set(f'✓ 已保存 {n} 个图标的归属更改')
        self._refresh_all()
        # ★ P3：全部失败（n=0）时磁盘未变、主窗口数据视图也未变，
        # 刷新主窗口纯属浪费（_update_lists + _rebuild_sticky_grid +
        # dock.sync_slots 都是重活）。n>0 才通知。
        if n > 0:
            self.on_changed()   # ★ #2：通知主窗口刷新分类图标数
        return n, failed

    def _discard_pending(self):
        if not self._pending:
            messagebox.showinfo('提示', '没有未保存的更改', parent=self)
            return
        n = len(self._pending)
        self._pending.clear()
        self._refresh_all()
        self.status_var.set(f'已还原 {n} 项未保存的更改')

    def _update_dirty_ui(self):
        if self._pending:
            self.pending_var.set(f'● 待保存 {len(self._pending)} 项')
        else:
            self.pending_var.set('')

    def _on_icon_select(self, event=None):
        """选中左侧图标 → 右侧用红底标出它所属的分类"""
        self._apply_member_highlights()
        names = self._selected_icon_names()
        if names:
            members = self._effective_members(names[0])
            members.discard('其他')
            extra = f' 等{len(names)}个' if len(names) > 1 else ''
            self.status_var.set(
                f'[{names[0]}]{extra} 属于: {"/".join(sorted(members)) or "未归类"}')

    def _apply_member_highlights(self):
        """右侧分类列表：选中图标所属的分类标红底"""
        size = self.cat_list.size()
        for i in range(size):
            try:
                self.cat_list.itemconfigure(i, bg=COLORS['surface'])
            except tk.TclError:
                pass
        names = self._selected_icon_names()
        if not names:
            return
        members = set()
        for n in names:
            members |= self._effective_members(n)
        for i in range(size):
            cat_name = self.cat_list.get(i).rsplit(' (', 1)[0]  # P2-B：分类名含(时只切末个 (count)
            if cat_name in members:
                try:
                    self.cat_list.itemconfigure(i, bg='#b71c1c')
                except tk.TclError:
                    pass

    def _refresh_category_list(self):
        selected = self._selected_category()
        self.cat_list.delete(0, tk.END)
        cats = self.category_manager.get_all_categories()
        for cat in cats:
            flags = ''
            if cat.get('sticky', False):
                flags += ' 🤖'
            self.cat_list.insert(tk.END, f"{cat['name']} ({len(cat.get('icons', []))}){flags}")
        # 追加"未分类"虚拟分类（统计当前无任何归属的图标数）
        unassigned = sum(1 for n in self._icons_cache
                         if not {c['name'] for c in self.category_manager.get_all_categories()
                                 if n['name'] in c.get('icons', [])})
        if unassigned > 0 or self._pending:
            pending_unassigned = sum(1 for n, cats in self._pending.items()
                                     if not cats)
            total_unassigned = unassigned + pending_unassigned
            self.cat_list.insert(tk.END, f"📭 未分类 ({total_unassigned})")
        # 恢复选中
        if selected:
            for i, cat in enumerate(cats):
                if cat['name'] == selected:
                    self.cat_list.selection_set(i)
                    break
            # 若选中的是虚拟分类也恢复
            if not any(cat['name'] == selected for cat in cats):
                for i in range(self.cat_list.size()):
                    if self.cat_list.get(i).startswith('📭 未分类'):
                        self.cat_list.selection_set(i)
                        break
        self._on_category_select()
        self._apply_member_highlights()

    def _refresh_all(self):
        self._refresh_icon_list()
        self._refresh_category_list()

    def _selected_category(self) -> Optional[str]:
        sel = self.cat_list.curselection()
        if not sel:
            return None
        text = self.cat_list.get(sel[0])
        # 虚拟分类行以 "📭 未分类" 开头
        if text.startswith('📭 未分类'):
            return '📭未分类'
        return text.rsplit(' (', 1)[0]  # P2-B：分类名含 ( 时只切末个 (count)

    def _selected_icon_names(self) -> List[str]:
        return [self._icons_cache[self._display_idx[i]]['name']
                for i in self.icon_list.curselection()
                if i < len(self._display_idx)]

    # ------------------------------------------------------------------
    # 真拖拽：按住图标 → 拖到分类 → 松手
    # ------------------------------------------------------------------

    def _on_drag_press(self, event):
        self._drag_pressed = True
        self._drag_active = False
        self._drag_start = (event.x, event.y)
        self._drag_names = []
        # 按在已选中的项上 → 冻结当前多选（EXTENDED 模式的类绑定会在
        # 拖动中重置选择集，导致"拖多个只剩一个"，必须逐事件恢复）
        idx = self.icon_list.nearest(event.y)
        if 0 <= idx < len(self._display_idx) and idx in self.icon_list.curselection():
            self._keep_selection = list(self.icon_list.curselection())
        else:
            self._keep_selection = None

    def _on_drag_motion(self, event):
        if not self._drag_pressed:
            return
        if not self._drag_active:
            dx = abs(event.x - self._drag_start[0])
            dy = abs(event.y - self._drag_start[1])
            if dx < self.DRAG_THRESHOLD and dy < self.DRAG_THRESHOLD:
                return 'break'
            # 冻结按下时刻的选中集（拖多个时不再被类绑定清掉）
            if self._keep_selection is None:
                self._keep_selection = list(self.icon_list.curselection())
            names = [self._icons_cache[self._display_idx[i]]['name']
                     for i in self._keep_selection
                     if i < len(self._display_idx)]
            if not names:
                return 'break'
            self._drag_active = True
            self._drag_names = names
            self._show_ghost()
        # 每个拖动事件都恢复冻结的选择（对抗列表框类绑定的拖选）
        if self._keep_selection:
            cur = list(self.icon_list.curselection())
            if cur != self._keep_selection:
                self.icon_list.selection_clear(0, tk.END)
                for i in self._keep_selection:
                    self.icon_list.selection_set(i)

        self._move_ghost()
        self._update_drop_highlight()
        return 'break'

    def _on_drag_release(self, event):
        was_active = self._drag_active
        names = list(self._drag_names)
        self._drag_pressed = False
        self._drag_active = False
        self._drag_names = []
        self._hide_ghost()
        self._clear_drop_highlight()
        if not was_active or not names:
            return
        # ★ R72：拖拽结束把横向视图滑回开头——Tk Listbox 自带 autoscan
        # 会在按住拖动时把长列表横向滑走，图标名开头跑出视野（用户实测
        # "左边的图标窗口自动往右边滑动，显示不全"）
        try:
            self.icon_list.xview_moveto(0.0)
        except Exception:
            pass

        target_idx = self._cat_index_at(event.x_root, event.y_root)
        if target_idx is None:
            return 'break'
        # ★R204 ROOT-6：与 _selected_category 同口径识别虚拟行——此前
        # rsplit 得到 '📭 未分类'（带空格）与哨兵 '📭未分类'（无空格）不
        # 相等，被当真实分类写入 _pending，提交时必失败且待保存清不掉。
        # 语义定为"拖到未分类行=清空全部归属"（_commit_pending 空集分支）。
        _row = self.cat_list.get(target_idx)
        if _row.startswith('📭 未分类'):
            for name in names:
                self._pending[name] = set()
            moved = len(names)
            self._refresh_all()
            self.status_var.set(i18n.tr(
                f'📦 待保存：已移出全部分类 {moved} 个图标（点"保存更改"生效）',
                f'📦 Pending: cleared categories for {moved} icon(s) '
                '(click "💾 Save Changes" to apply)'))
            return 'break'
        cat_name = _row.rsplit(' (', 1)[0]  # P2-B
        self._assign_icons_to_category(names, cat_name)
        return 'break'

    def _show_ghost(self):
        n = len(self._drag_names)
        self._ghost = tk.Toplevel(self)
        self._ghost.overrideredirect(True)
        self._ghost.attributes('-topmost', True)
        lbl = tk.Label(self._ghost,
                       text=f'📦 {n} 个图标',
                       bg='#50fa7b', fg='#282a36',
                       font=ui_layout.app_font(10, 'bold'),
                       padx=10, pady=4)
        lbl.pack()
        self._move_ghost()

    def _move_ghost(self):
        if self._ghost:
            try:
                x = self.winfo_pointerx() + 12
                y = self.winfo_pointery() + 12
                self._ghost.geometry(f'+{x}+{y}')
            except tk.TclError:
                pass

    def _hide_ghost(self):
        if self._ghost:
            try:
                self._ghost.destroy()
            except tk.TclError:
                pass
            self._ghost = None

    def _cat_index_at(self, x_root, y_root) -> Optional[int]:
        """屏幕坐标 → 分类列表项索引（不在列表内返回 None）"""
        rx = self.cat_list.winfo_rootx()
        ry = self.cat_list.winfo_rooty()
        w = self.cat_list.winfo_width()
        h = self.cat_list.winfo_height()
        if not (rx <= x_root < rx + w and ry <= y_root < ry + h):
            return None
        idx = self.cat_list.index(f'@{x_root - rx},{y_root - ry}')
        return idx if 0 <= idx < self.cat_list.size() else None

    def _update_drop_highlight(self):
        idx = self._cat_index_at(self.winfo_pointerx(), self.winfo_pointery())
        if idx == self._drop_target_idx:
            return
        self._clear_drop_highlight()
        if idx is not None:
            self.cat_list.itemconfigure(idx, bg='#3d6b4f')
            self._drop_target_idx = idx

    def _clear_drop_highlight(self):
        if 0 <= self._drop_target_idx < self.cat_list.size():
            try:
                self.cat_list.itemconfigure(self._drop_target_idx, bg=COLORS['surface'])
            except tk.TclError:
                pass
        self._drop_target_idx = -1

    def _assign_icons_to_category(self, names: List[str], cat_name: str):
        """把图标加入分类（保留已属的其他分类，支持多分类归属；
        变更先记入待保存，点"保存更改"才写入）"""
        if not names:
            return
        added = 0
        for name in names:
            members = self._effective_members(name)
            if cat_name in members:
                continue
            members.add(cat_name)
            self._pending[name] = members
            added += 1
        if added:
            self._refresh_all()
        self.status_var.set(f'📦 待保存：{added} 个图标加入 [{cat_name}]'
                            f'（点右上"保存更改"生效）')

    # ------------------------------------------------------------------
    # 事件处理
    # ------------------------------------------------------------------

    def _on_icon_double_click(self, event=None):
        sel = self.icon_list.curselection()
        if not sel:
            return
        idx = sel[0]
        if idx >= len(self._display_idx):
            return
        icon = self._icons_cache[self._display_idx[idx]]
        cat = icon['category']
        if messagebox.askyesno('快速归类', f'将 "{icon["name"]}" 归类到 [{cat}]？',
                               parent=self):   # P1-8
            existing = [c['name'] for c in self.category_manager.get_all_categories()]
            if cat not in existing:
                self.category_manager.add_category(cat)
                # ★R302：顺手建同名盒子（只做加法，失败只在日志留痕）
                from gui import desktop_box_panel
                desktop_box_panel.auto_box_for_category(cat)
            self._assign_icons_to_category([icon['name']], cat)

    def _category_display_icons(self, cat_name: str) -> List[str]:
        """分类图标（含待保存的增删），拖完立刻可见"""
        # 虚拟"未分类"分类：显示当前无任何真实归属的图标
        if cat_name == '📭未分类':
            result = []
            for icon in self._icons_cache:
                members = self._effective_members(icon['name'])
                real_cats = members - {'📭未分类'}
                # 也考虑 pending 中是否还在未分类
                pending_cats = self._pending.get(icon['name'], set())
                has_real = bool(real_cats) or bool(pending_cats)
                if not has_real:
                    result.append(icon['name'])
            return result
        icons = list(self.category_manager.get_category_icons(cat_name))
        for name, cats in self._pending.items():
            if cat_name in cats and name not in icons:
                icons.append(name)
            elif cat_name not in cats and name in icons:
                icons.remove(name)
        return icons

    def _on_category_select(self, event=None):
        cat_name = self._selected_category()
        self.cat_icon_list.delete(0, tk.END)
        if not cat_name:
            return
        for name in self._category_display_icons(cat_name):
            self.cat_icon_list.insert(tk.END, name)

    def _on_clear_category(self):
        """一键清空选中的分类（走待保存机制，点"保存更改"才写入）"""
        cat_name = self._selected_category()
        if not cat_name:
            messagebox.showwarning('提示', '请先在右侧选择一个分类', parent=self)
            return
        # 虚拟"未分类"不能清空
        if cat_name == '📭未分类':
            messagebox.showinfo('提示', '「未分类」是虚拟分类，无法清空')
            return
        names = self.category_manager.get_category_icons(cat_name)
        if not names:
            messagebox.showinfo('提示', f'分类 [{cat_name}] 本来就是空的',
                                parent=self)
            return
        preview = '、'.join(names[:10]) + ('…' if len(names) > 10 else '')
        if not messagebox.askyesno(
                '确认清空',
                f'将清空分类 [{cat_name}] 里的全部 {len(names)} 个图标：'
                + chr(10) + chr(10) + preview + chr(10) + chr(10)
                + '确定吗？（点"保存更改"后才会真正写入）',
                parent=self):
            return
        n = 0
        for name in names:
            members = self._effective_members(name)
            members.discard(cat_name)
            self._pending[name] = members
            n += 1
        self._refresh_all()
        self.status_var.set(f'📦 待保存：已清空 [{cat_name}] 的 {n} 个图标'
                            f'（点"保存更改"生效）')

    def _on_remove_from_category(self):
        cat_name = self._selected_category()
        icon_sel = self.cat_icon_list.curselection()
        if not cat_name or not icon_sel:
            messagebox.showwarning('提示', '请先选择分类和其中的图标', parent=self)
            return
        if cat_name == '📭未分类':
            messagebox.showinfo('提示', '「未分类」是虚拟分类，图标本身就没有归属')
            return
        names = [self.cat_icon_list.get(i) for i in icon_sel]
        preview = chr(10).join(f'  · {n}' for n in names[:15])
        if len(names) > 15:
            preview += chr(10) + f'  … 等共 {len(names)} 个'
        if not messagebox.askyesno(
                '确认移除',
                f'将把 {len(names)} 个图标移出 [{cat_name}]：' + chr(10) + chr(10)
                + preview + chr(10) + chr(10)
                + '确定吗？（点"保存更改"后才会真正写入）',
                parent=self):
            return
        for name in names:
            members = self._effective_members(name)
            members.discard(cat_name)
            self._pending[name] = members
        self._refresh_all()
        self.status_var.set(f'📦 待保存：{len(names)} 个图标移出 [{cat_name}]'
                            f'（点"保存更改"生效）')

    def _on_select_all_icons(self):
        """全选"该分类中的图标"列表的所有图标"""
        n = self.cat_icon_list.size()
        if n > 0:
            self.cat_icon_list.selection_set(0, n - 1)

    def _on_deselect_all_icons(self):
        """取消全选"该分类中的图标"列表"""
        self.cat_icon_list.selection_clear(0, tk.END)

    def _on_move_to_category(self):
        """把右侧列表中选中的图标从当前分类移除并移动到另一个分类（一步完成）"""
        cat_name = self._selected_category()
        if not cat_name:
            messagebox.showwarning('提示', '请先在右侧选择一个分类', parent=self)
            return
        # 虚拟"未分类"不允许移动
        if cat_name == '📭未分类':
            messagebox.showinfo('提示', '「未分类」是虚拟分类，无法移动', parent=self)
            return
        icon_sel = self.cat_icon_list.curselection()
        if not icon_sel:
            messagebox.showwarning('提示', '请先在右侧列表中选中要移动的图标',
                                   parent=self)
            return
        names = [self.cat_icon_list.get(i) for i in icon_sel]
        preview = chr(10).join(f'  · {n}' for n in names[:10])
        if len(names) > 10:
            preview += chr(10) + f'  … 等共 {len(names)} 个'
        # 列出所有真实分类供选择
        real_cats = [c['name'] for c in self.category_manager.get_all_categories()]
        cat_choices = '\n'.join(f'  {i+1}. {c}' for i, c in enumerate(real_cats))
        target = simpledialog.askstring(
            i18n.tr('移动到分类', 'Move to Category'),
            i18n.tr(f'将 {len(names)} 个图标从 [{cat_name}] 移动到哪个分类？\n\n'
            f'可用分类：\n{cat_choices}\n\n请输入分类名称：',
                    f'Move {len(names)} icon(s) from [{cat_name}] to which category?\n\n'
                    f'Available categories:\n{cat_choices}\n\nEnter category name:'),
            parent=self)
        if not target:
            return
        target = target.strip()
        if not target:
            return
        # 检查目标分类是否存在
        existing_names = {c['name'] for c in self.category_manager.get_all_categories()}
        if target not in existing_names:
            messagebox.showwarning(i18n.tr('提示', 'Notice'),
                                   i18n.tr(f'分类 [{target}] 不存在',
                                           f'Category [{target}] does not exist'), parent=self)
            return
        if target == cat_name:
            messagebox.showinfo(i18n.tr('提示', 'Notice'),
                                i18n.tr('目标分类与当前分类相同，无需移动',
                                        'Target equals the current category — nothing to move'), parent=self)
            return
        if not messagebox.askyesno(
                i18n.tr('确认移动', 'Confirm Move'),
                i18n.tr(f'将把 {len(names)} 个图标从 [{cat_name}] 移动到 [{target}]：'
                + chr(10) + chr(10)
                + preview + chr(10) + chr(10)
                + '确定吗？（点"保存更改"后才会真正写入）',
                        f'Will move {len(names)} icon(s) from [{cat_name}] to [{target}]'
                        + chr(10) + chr(10)
                        + preview + chr(10) + chr(10)
                        + 'Are you sure? (nothing is written until you click "💾 Save Changes")'),
                parent=self):
            return
        moved = 0
        for name in names:
            members = self._effective_members(name)
            members.discard(cat_name)
            members.add(target)
            self._pending[name] = members
            moved += 1
        self._refresh_all()
        self.status_var.set(i18n.tr(f'📦 待保存：已移动 {moved} 个图标 [{cat_name}] → [{target}]'
                            f'（点"保存更改"生效）',
                            f'📦 Pending: moved {moved} icon(s) [{cat_name}] → [{target}]'
                            f' (click "💾 Save Changes" to apply)'))

    def _on_auto_classify(self):
        if not self._icons_cache:
            messagebox.showinfo(i18n.tr('提示', 'Notice'),
                                i18n.tr('桌面没有图标', 'No icons on the desktop'),
                                parent=self)   # P1-8 残留
            return
        suggested = get_suggested_categories(
            self.icon_manager, self.category_manager, self.prefix_rules)
        lines = [i18n.tr(f'智能分类预览（共 {len(self._icons_cache)} 个图标）：',
                         f'Smart classify preview ({len(self._icons_cache)} icons in total):')]
        for cat, names in sorted(suggested.items(), key=lambda x: -len(x[1])):
            lines.append(i18n.tr(f'  [{cat}] {len(names)} 个',
                                 f'  [{cat}] {len(names)} icon(s)'))
        preview = '\n'.join(lines)
        if not messagebox.askyesno(i18n.tr('智能分类', 'Smart Classify'),
                                   i18n.tr(f'{preview}\n\n确定应用？(已归类图标保持不变)',
                                           f'{preview}\n\nApply? (already-assigned icons stay unchanged)'),
                                   parent=self):   # P1-8 残留
            return
        # 只给"未归类"的图标分配（已手动归类的保持不变）
        # ★R204 CORE-17 接线：批量 API 一次落盘——此前仍走 add_icon_to_category
        # 逐图标全量 JSON 原子写+inspect.stack，200 图标卡 UI 数秒~数十秒
        assigned = {n for c in self.category_manager.get_all_categories()
                    for n in c.get('icons', [])}
        assignments = {}
        for cat, names in suggested.items():
            if cat == '其他':
                continue
            todo = [n for n in names if n not in assigned]
            if todo:
                assignments[cat] = todo
        if assignments:
            if not self.category_manager.add_icons_to_category_batch(assignments):
                messagebox.showerror(
                    i18n.tr('智能分类', 'Smart Classify'),
                    i18n.tr('分类数据写盘失败，本次智能分类未保存',
                            'Failed to save category data; smart classify was not applied'),
                    parent=self)
                return
        self._refresh_all()
        self.status_var.set(i18n.tr(f'✓ 智能分类完成：{len(suggested)} 个分类',
                                    f'✓ Smart classify done: {len(suggested)} categories'))
        self.on_changed()   # ★ #2：智能分类直接写盘 → 通知主窗口刷新

    def _on_prefix_rules(self):
        PrefixRuleDialog(self, self.prefix_rules, on_changed=self._refresh_all)

    def _on_highlight_category(self):
        if not self.highlighter:
            return
        cat_name = self._selected_category()
        if not cat_name:
            messagebox.showwarning(i18n.tr('提示', 'Notice'),
                                   i18n.tr('请先选择一个分类', 'Select a category first'),
                                   parent=self)   # P1-8
            return
        names = self.highlighter.highlight_category(self.category_manager, cat_name)
        if not names:
            messagebox.showinfo(i18n.tr('提示', 'Notice'),
                                i18n.tr(f'分类 [{cat_name}] 还没有图标',
                                        f'Category [{cat_name}] has no icons yet'),
                                parent=self)   # P1-8 补全
            return
        self.status_var.set(i18n.tr(f'🖍️ 已高亮 [{cat_name}] 的 {len(names)} 个图标',
                                    f'🖍️ Highlighted {len(names)} icon(s) of [{cat_name}]'))

    def _on_clear_highlight(self):
        if self.highlighter:
            self.highlighter.clear()
            self.status_var.set('🚫 已清除所有高亮框')

    def _on_new_category(self):
        name = simpledialog.askstring(i18n.tr('新建分类', 'New Category'),
                                      i18n.tr('请输入分类名称:', 'Enter category name:'),
                                      parent=self)
        if not name:
            return
        # ★ R79：与主窗口同款色板选色（旧版直接默认色 → 渐变色全同色）
        color = None
        try:
            # ★R204 ROOT-16：改从 gui.manage_panel 导入（函数真正定义处）
            # ——import gui.main_window 违反 R99 模块双身份禁令
            from gui.manage_panel import ask_category_color
            color = ask_category_color(self)
        except Exception:
            color = None
        self.category_manager.add_category(name, color or '#8be9fd')
        # ★R302：顺手建同名盒子（只做加法，失败只在日志留痕）
        from gui import desktop_box_panel
        desktop_box_panel.auto_box_for_category(name)
        self._refresh_category_list()
        self.on_changed()   # ★ #2：新分类 → 同步主窗口列表 + 悬浮条槽位

    def _on_rename_category(self):
        cat_name = self._selected_category()
        if not cat_name:
            messagebox.showwarning(i18n.tr('提示', 'Notice'),
                                   i18n.tr('请先选择一个分类', 'Select a category first'),
                                   parent=self)   # P1-8
            return
        new_name = simpledialog.askstring(i18n.tr('重命名分类', 'Rename Category'),
                                           i18n.tr('请输入新名称:', 'Enter new name:'),
                                           initialvalue=cat_name, parent=self)
        if not new_name or new_name == cat_name:
            return
        self.category_manager.rename_category(cat_name, new_name)
        self._refresh_all()
        self.on_changed()   # ★ #2：重命名 → 同步主窗口 + 悬浮条

    def _on_recolor_category(self):
        """★ R79：改选中分类颜色（色板 → set_color → 刷新）"""
        cat_name = self._selected_category()
        if not cat_name:
            messagebox.showwarning(i18n.tr('提示', 'Notice'),
                                   i18n.tr('请先选择一个分类', 'Select a category first'),
                                   parent=self)   # P1-8
            return
        color = None
        try:
            # ★R204 ROOT-16：改从 gui.manage_panel 导入（函数真正定义处）
            # ——import gui.main_window 违反 R99 模块双身份禁令
            from gui.manage_panel import ask_category_color
            color = ask_category_color(self)
        except Exception:
            color = None
        if not color:
            return
        self.category_manager.set_color(cat_name, color)
        self._refresh_all()
        self.on_changed()   # ★ #2：改色 → 同步主窗口 + 悬浮条

    def _on_delete_category(self):
        cat_name = self._selected_category()
        if not cat_name:
            messagebox.showwarning(i18n.tr('提示', 'Notice'),
                                   i18n.tr('请先选择一个分类', 'Select a category first'),
                                   parent=self)   # P1-8
            return
        if messagebox.askyesno(i18n.tr('确认删除', 'Confirm Delete'),
                               i18n.tr(f'确认删除分类 "{cat_name}"？\n其中的图标不会被删除。',
                                       f'Delete category "{cat_name}"?\nThe icons inside will NOT be deleted.')):
            self.category_manager.delete_category(cat_name)
            self._refresh_all()
            self.on_changed()   # ★ #2：删除分类 → 同步主窗口 + 悬浮条

    def _on_close(self):
        """关闭面板。

        ★ P1-1：原实现 _commit_pending 抛出时 _hide_ghost / destroy 不执行
        → 面板关不掉，用户只能强杀进程。现保证任何情况下都能关。

        ★ P1-1/P1-2 交互残留：_commit_pending 现在返回 (n_ok, failed)。
        若用户选"保存并关闭"但部分项失败，弹框告知具体哪些项失败并让用户
        决定"仍关闭"还是"留下面板先修"——避免失败项静默丢失、用户无感知。
        """
        if self._pending:
            ans = messagebox.askyesnocancel(
                '未保存的更改',
                f'有 {len(self._pending)} 项分类归属更改还未保存。' + chr(10) + chr(10)
                + '是：保存并关闭；否：放弃更改并关闭；取消：留在本面板',
                parent=self)
            if ans is None:
                return   # 取消：留在本面板
            if ans:
                try:
                    n_ok, failed = self._commit_pending()   # P1-2 内部已逐条容错
                except Exception as e:
                    logger.error(f"提交待保存变更失败: {e}")
                    logger.debug("提交失败详情", exc_info=True)
                    n_ok, failed = 0, list(self._pending.keys())
                if failed:
                    preview = chr(10).join(f'  · {nm}' for nm in failed[:10])
                    if len(failed) > 10:
                        preview += chr(10) + f'  … 等共 {len(failed)} 项'
                    # 关键：用户点"否" → 留在面板让用户先修；点"是"才关闭
                    if not messagebox.askyesno(
                            '部分保存失败',
                            f'已保存 {n_ok} 项，但有 {len(failed)} 项失败'
                            f'（可能是分类已被删除 / 重命名）：'
                            + chr(10) + chr(10) + preview + chr(10) + chr(10)
                            + '仍要关闭吗？未保存的更改将丢失。',
                            parent=self):
                        return   # 留下面板，让用户在 UI 里继续处理
        # ★ P1-1：finally 语义——隐藏 ghost 和 destroy 各自独立 try，
        # 任一步失败都不能阻止面板销毁。
        try:
            self._hide_ghost()
        except Exception as e:
            logger.error(f"隐藏拖拽占位失败: {e}")
        try:
            self.destroy()
        except Exception as e:
            logger.error(f"销毁拖拽面板失败: {e}")

    def refresh(self):
        self._refresh_all()


# 全局面板实例
_panel_instance = None


def get_panel():
    return _panel_instance


def open_drag_drop_panel(parent, icon_manager, category_manager,
                         highlighter=None, on_changed=None):
    global _panel_instance
    if _panel_instance and _panel_instance.winfo_exists():
        if highlighter is not None:
            _panel_instance.highlighter = highlighter
        # ★ #2：面板是复用单例，每次打开都刷新回调（主窗口可能已换实例）
        _panel_instance.on_changed = on_changed or (lambda: None)
        _panel_instance.lift()
        _panel_instance.focus()
        _panel_instance._refresh_all()
        return _panel_instance

    _panel_instance = DragDropPanel(parent, icon_manager, category_manager,
                                    highlighter=highlighter, on_changed=on_changed)
    return _panel_instance
