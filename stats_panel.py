"""📊 统计页签（★ S 段）：图标打开榜 / 文件夹打开榜 / 分类切换榜。

独立文件实现，main_window 只需 import + build 一行接入；
面板对象返回给 gui，页签选中 / 导入全部数据 后调 refresh() 刷新。
文件夹路径默认只显示"盘符…末级文件夹"，双击条目展开完整路径（R15 用户要求）。
"""
import json
import logging
import os
import queue
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import ttk, messagebox, filedialog

from gui import icons   # ★R225 阶段5：页签图标档统一出口（不依赖 main_window）

from utils.safeio import atomic_write_json
from utils import i18n   # ★ R73 双语
from gui import ui_layout   # ★R228：界面字号等比缩放 helper

logger = logging.getLogger("desktop_manager")

# ★R281-7（用户 09-30 08 批 item7：「统计页三张榜单要不要标 ⚠ —— 这个肯定要」）
# ⚠ 前缀与设置页那两列同一个记号、同一个含义：**这一项现在还在不在**。
STALE_MARK = '⚠ '

# ★R286-A2（挂账 N2，用户 2026-10-01 点头"按你建议的来"）：
# 这两个榜的"现场对不上"体检要碰磁盘（桌面上列一遍目录、逐个路径 isdir）。
# 以前它是**主线程同步做的**——映射到已断的网络盘/UNC 时一发 isdir 能停几十秒，
# 整个界面跟着冻（用户报的"统计页卡"就是这个）。现在挪到后台单探针线程，
# 主线程只画缓存，所以：
AUDIT_DRAIN_MS = 250          # 主线程取后台结果的轮询间隔（只在有探针在飞时排）
AUDIT_PROBE_TIMEOUT_SEC = 8.0  # 探针超过它 ⇒ 判"卡死"：旧结果作废、坑腾出来重探
AUDIT_REPROBE_SEC = 20.0       # 名单没变时最快 20 秒复查一次（refresh 是高频动作）


def audit_board(kind, names, live_cats=None):
    """把★R266 那套"现场对不上"体检挪到统计页三张榜单上。

    语义只有一句：榜单是**历史事实**（它真被打开过），⚠ 只补一句"可现在
    桌面上/磁盘上/分类表里已经没有它了"。**一个统计数字都不改，一条记录都不删**
    ——猜错就是静默毁用户的数据（口径照 `audit_desktop_names` 的 docstring）。
    虚拟图标（此电脑/回收站那类）由 `audit_desktop_names` 自己放行，不会被标。

    返回 {榜单里的名字: 说明串}，只含对不上的那些。体检本身炸了 ⇒ 返回 {}，
    并留一行 warning（★R230b：兜底可以吞异常，但不许静默——静默了下一轮只能重猜）。
    """
    out = {}
    try:
        if kind == 'icon':
            from core import explorer_tabs
            out = {k: v for k, v in
                   explorer_tabs.audit_desktop_names(names).items() if v}
        elif kind == 'folder':
            from core import explorer_tabs
            out = {k: v for k, v in
                   explorer_tabs.audit_folder_paths(names).items() if v}
        elif kind == 'category':
            # 分类名单读不到 / 读出来是空的 ⇒ **一律不标**。
            # ★R230 那条教训：`category_manager._load_failed` 为真时内存里是
            # 出厂默认值，拿它当真相源会把用户真的分类整榜标成 ⚠（比不标更毒）。
            cats = {str(c).casefold() for c in (live_cats or []) if c}
            if not cats:
                return {}
            for n in (names or []):
                if str(n).casefold() not in cats:
                    out[n] = i18n.tr(
                        f'分类「{n}」现在已经不在了（删掉或改名了）。'
                        f'这条切换次数是历史事实，数字照旧、记录不删。',
                        f'Category "{n}" no longer exists (deleted or renamed). '
                        f'The switch count is history — the number and the '
                        f'record are both kept as they are.')
    except Exception as e:
        logger.warning(f'[统计] 榜单 ⚠ 体检没做成（这次不标）: {e}')
        return {}
    return out


def build_stats_tab(notebook, usage_stats, click_tracker, colors, root, log,
                    on_toggle=None, is_on=None, settings=None, save_settings=None,
                    category_names=None, tab_text=None):
    """构建统计页签，返回面板对象（含 refresh()）。

    ★ R30：on_toggle=切换统计采集开关的回调（gui 注入）；
    is_on=查询当前开关状态的回调。两者任一缺失则不显示开关按钮。
    ★ R32：settings/save_settings=持久化榜单字号用（gui 注入）。
    ★R281-7：category_names=取"现在还有哪些分类"的回调（gui 注入；缺失或
    返回空 ⇒ 分类榜这次不标 ⚠，图标/文件夹两榜不受影响）。
    ★R304-2：tab_text=gui 那份 `_tab_text(key, zh, en)`（缺失则退回自己拼图标）。
    页签标题**必须**从同一个口径出来：第④级降级是在 `_tab_text` 登记的那份
    「整串→纯文字」表里来回换写法，这枚页签要是绕过登记，摘图标就永远轮不到它
    （125%+大字窄窗口下它会一直顶着图标前缀，正是本轮红树上抓到的）。"""
    panel = _StatsPanel(notebook, usage_stats, click_tracker, colors, root, log,
                        on_toggle=on_toggle, is_on=is_on,
                        settings=settings, save_settings=save_settings,
                        category_names=category_names, tab_text=tab_text)
    return panel


class _StatsPanel:
    """统计页面板：三列榜单 + 刷新/导出/导入/清零/统计开关/字体调节"""

    def __init__(self, notebook, usage_stats, click_tracker, colors, root, log,
                 on_toggle=None, is_on=None, settings=None, save_settings=None,
                 category_names=None, tab_text=None):
        self.usage_stats = usage_stats
        self.click_tracker = click_tracker
        self.colors = colors or {}
        self.root = root
        self._log = log or (lambda m: None)
        self._on_toggle_cb = on_toggle
        self._is_on_cb = is_on
        self._settings = settings if isinstance(settings, dict) else None
        self._save_settings = save_settings
        self._category_names_cb = category_names if callable(category_names) else None
        self._tab_text_cb = tab_text if callable(tab_text) else None
        # ★R281-7：{榜别: {行号: 说明}}——⚠ 那一行鼠标停上去要能说清"哪种对不上"
        self._stale_tips = {'icon': {}, 'folder': {}, 'category': {}}
        self._stale_logged = None   # 只在**标注集变了**的时候写日志（刷新是高频动作）
        self.TOP_N = 50
        # ★ R32：榜单字号（7~16，持久化 app_settings['stats_font_size']）
        try:
            self._font_size = int((self._settings or {}).get('stats_font_size', 9))
        except Exception:
            self._font_size = 9
        self._font_size = max(7, min(16, self._font_size))
        # ★ R27 防御初始化（refresh 前的双击展开等交互不再踩未定义属性）
        self._folder_lines = []
        self._folder_expanded = set()
        # ★R286-A2：后台体检的状态位**必须在 self.refresh() 之前**建好——
        # refresh 是这一堆属性的第一个读者，晚一行就是 AttributeError。
        self._audit_marks = {'icon': {}, 'folder': {}}   # 主线程只读这份缓存
        self._audit_gen = 0            # 世代号：过期结果靠它作废
        self._audit_sig = None         # 最近一次交给后台的名单
        self._audit_probe_busy = False
        self._audit_probe_started = 0.0
        self._audit_last_done = 0.0    # 最近一次拿到结果的时刻（REPROBE 节流用）
        self._audit_queue = queue.Queue()
        self._audit_drain_after = None

        outer = ttk.Frame(notebook, padding='8 6')
        # ★R225 阶段5：图标档统一走 gui/icons（R215 已去手工前后空格，
        # 宽度由 TNotebook.Tab padding 给）——main_window 的比较串取同一构造
        # ★R304-2：gui 给了 `_tab_text` 就走它（顺带进那张登记表），第④级降级
        # 才摘得掉这一枚的图标前缀；没给才退回自己拼。
        if self._tab_text_cb is not None:
            _title = self._tab_text_cb('stats', '统计', 'Stats')
        else:
            _title = icons.tab_text('stats', i18n.tr('统计', 'Stats'))
        notebook.add(outer, text=_title)

        btns = ttk.Frame(outer)
        btns.pack(fill=tk.X, pady=(0, 6))
        # ★R216 阶段 2b：这一排原先 5 枚全是"裸按钮"（ghost＝无边框），
        # 看着像一行浮动文字、不像工具栏。按三档权重重排：数据动作描边
        # （secondary）、破坏性动作用 error、纯帮助入口保持 ghost。
        ttk.Button(btns, text=i18n.tr('🔄 刷新', '🔄 Refresh'),
                   command=self.refresh,
                   style='secondary.TButton').pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btns, text=i18n.tr('📤 导出', '📤 Export'),
                   command=self._on_export,
                   style='secondary.TButton').pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btns, text=i18n.tr('📥 导入', '📥 Import'),
                   command=self._on_import,
                   style='secondary.TButton').pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btns, text=i18n.tr('🧹 清零', '🧹 Reset'),
                   command=self._on_reset,
                   style='error.TButton').pack(side=tk.LEFT)

        # ★ R32：右侧从最右往左 pack（先 pack 的在最右）：
        #   开关按钮(最右) → 状态短字 → 📖说明 → A− → A+
        # 此前一行口径小字常驻按钮行左侧，把默认窗口的右侧按钮挤出
        # 可视区（Win11 实测），口径说明移进"📖 说明"弹窗
        if callable(self._on_toggle_cb):
            self.btn_toggle_stats = ttk.Button(
                btns, text='', command=self._on_toggle,
                style='secondary.TButton')
            self.btn_toggle_stats.pack(side=tk.RIGHT)
            self.lbl_stats_state = ttk.Label(
                btns, text='', foreground=self.colors.get('fg', '#f8f8f2'),
                font=ui_layout.app_font(9))
            self.lbl_stats_state.pack(side=tk.RIGHT, padx=(0, 6))
        ttk.Button(btns, text=i18n.tr('📖 说明', '📖 Info'),
                   command=self._show_help).pack(side=tk.RIGHT, padx=(0, 6))
        ttk.Button(btns, text='A−', width=3,
                   command=lambda: self._adjust_font(-1)).pack(side=tk.RIGHT, padx=(0, 4))
        ttk.Button(btns, text='A+', width=3,
                   command=lambda: self._adjust_font(1)).pack(side=tk.RIGHT)

        # ★ R256：原"pynput 缺失"黄条（R29）随去钩子化退役——采集改只读
        # 轮询后没有任何外部库依赖，不会再出现"缺库不工作"的状态。

        grid = ttk.Frame(outer)
        grid.pack(fill=tk.BOTH, expand=True)
        # ★ R15：文件夹路径长 → 列宽加倍；分类名短 → 窄列（用户反馈）
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=2)
        grid.columnconfigure(2, weight=1)
        grid.rowconfigure(0, weight=1)

        self.icon_frame, self.icon_list = self._make_list(
            grid, 0, i18n.tr('🖱️ 图标打开 Top', '🖱️ Icon Opens Top'))
        self.folder_frame, self.folder_list = self._make_list(
            grid, 1, i18n.tr('📂 文件夹打开 Top（双击展开完整路径）',
                             '📂 Folder Opens Top (double-click for full path)'))
        self.folder_list.bind('<Double-Button-1>', self._on_folder_dbl)
        self.cat_frame, self.cat_list = self._make_list(
            grid, 2, i18n.tr('🧭 分类切换 Top', '🧭 Category Switches Top'))

        # ★R281-7：⚠ 行要能悬停看"是哪种对不上"（只挂一次，说明随 refresh 换）
        for _lb, _kind in ((self.icon_list, 'icon'),
                           (self.folder_list, 'folder'),
                           (self.cat_list, 'category')):
            self._bind_row_tips(_lb, _kind)

        self._apply_font()   # ★ R32：应用持久化字号
        if callable(self._on_toggle_cb):
            self.update_toggle_ui(
                self._is_on_cb() if callable(self._is_on_cb) else True)

        self.refresh()

    # ------------------------------------------------------------------

    def _make_list(self, grid, col, title):
        frame = ttk.LabelFrame(grid, text=title, padding='6',
                               style='Card.TLabelframe')
        frame.grid(row=0, column=col, sticky='nsew',
                   padx=(0 if col == 0 else 4, 0), pady=2)
        lb = tk.Listbox(frame, font=ui_layout.app_font(9),
                        bg=self.colors.get('surface', '#44475a'),
                        fg=self.colors.get('fg', '#f8f8f2'),
                        relief=tk.FLAT, highlightthickness=0,
                        activestyle='none', exportselection=False)
        sb = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=lb.yview)
        lb.configure(yscrollcommand=sb.set)
        lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        return frame, lb

    def _bind_row_tips(self, lb, kind):
        """★R281-7：给一枚榜单 Listbox 挂"按行"的悬停说明。

        Listbox 没有 per-item 提示，只能 `<Motion>` 现算最近行（`nearest`）。
        没有说明的行**不弹**（弹个空框比不弹更吵）；同一行内挪动不重建窗口，
        否则鼠标每动一个像素就 destroy+Toplevel 一次，看着像闪。
        窗口口径照 gui/rule_panel.attach_info_tip 那份（无边框 + topmost，
        离开/按下/销毁即收），本模块不 import main_window。"""
        tip = {}

        def _hide(_ev=None):
            tip['i'] = None
            w = tip.pop('w', None)
            if w is not None:
                try:
                    w.destroy()
                except Exception as e:
                    # ★R287-N14：悬浮说明窗已经不存在（鼠标早已移开/窗已关），
                    # 销毁失败不伤操作，但留一行才能解释"说明窗偶尔没关"
                    logger.warning(f"[统计] 提示窗销毁失败（多半已自动关闭）: {e}")

        def _mv(ev):
            try:
                i = lb.nearest(ev.y)
            except Exception:
                return
            txt = (self._stale_tips.get(kind) or {}).get(i)
            if not txt:
                _hide()
                return
            if tip.get('i') == i and tip.get('w') is not None:
                return
            _hide()
            try:
                w = tk.Toplevel(lb)
                w.wm_overrideredirect(True)
                w.attributes('-topmost', True)
                w.wm_geometry('+%d+%d' % (ev.x_root + 14, ev.y_root + 10))
                tk.Label(w, text=txt,
                         bg=self.colors.get('surface', '#44475a'),
                         fg=self.colors.get('fg', '#f8f8f2'),
                         font=ui_layout.app_font(9), padx=8, pady=4,
                         wraplength=420, justify=tk.LEFT,
                         highlightbackground=self.colors.get('border', '#6272a4'),
                         highlightthickness=1).pack()
                tip['w'] = w
                tip['i'] = i
            except Exception as e:
                logger.warning(f'[统计] ⚠ 说明弹不出来: {e}')

        lb.bind('<Motion>', _mv, add='+')
        lb.bind('<Leave>', _hide, add='+')
        lb.bind('<Button-1>', _hide, add='+')
        lb.bind('<Destroy>', _hide, add='+')

    @staticmethod
    def _fmt_rank(i: int, count: int, name: str, note: str = None) -> str:
        """榜单条目格式：前三名奖牌 + 计数前置（★ R27 美化）

        ★R281-7：`note` 非空 ⇒ 行首挂 `⚠ `（＝"现在现场对不上了"，说明靠悬停看）。
        数字与名次一个字不动——榜单是历史事实，标注只补一句现场状态。"""
        medal = {1: '🥇', 2: '🥈', 3: '🥉'}.get(i)
        rank = medal if medal else f'{i:>2}.'
        return f'{STALE_MARK if note else ""}{rank} ×{count}  {name}'

    def refresh(self):
        """重填三份榜单（页签选中/导入/清零后调用）。

        ★ R27 美化：前三名奖牌标记、Frame 标题带总数、空状态给引导提示；
        文件夹路径默认缩略显示（双击条目展开）。
        ★ R29：刷新前先从文件重读（文件为唯一真相源——多来源写入/
        长时间运行不丢数据）。
        ★R281-7（用户 08 批 item7）：三榜各做一次"现场对不上"体检 ⇒ 行首 ⚠
        ＋悬停说明。**只标不改**：数字、名次、记录一个都不动。
        ★R286-A2（挂账 N2）：图标/文件夹两榜的体检碰磁盘 ⇒ 挪后台，这里
        **只读缓存**。缓存里没有 ⇒ 这一轮不标 ⚠（"还没测"绝不画成"已经没了"）；
        结果回来后再 refresh 一次订正。分类榜的名单是内存里的，留在主线程内联。"""
        try:
            self.usage_stats.reload()
            icon_rows = self.usage_stats.top_icons(self.TOP_N)
            icon_total = len(self.usage_stats.icon_opens)
            icon_names = [n for n, _c, _t in icon_rows]
            folder_rows = self.usage_stats.top_folders(self.TOP_N)
            folder_names = [n for n, _c, _t in folder_rows]
            # 碰磁盘的两榜：这一轮只排一轮后台探针，自己一次 isdir 都不做
            self._request_audit(icon_names, folder_names)
            icon_marks = self._audit_marks.get('icon') or {}
            self.icon_frame.configure(
                text=i18n.tr(f'🖱️ 图标打开 Top {len(icon_rows)} · 共 {icon_total} 项',
                             f'🖱️ Icon Opens Top {len(icon_rows)} · {icon_total} total'))
            self._stale_tips['icon'] = {}
            _icon_lines = []
            for i, (n, c, _t) in enumerate(icon_rows, 1):
                _note = icon_marks.get(n)
                if _note:
                    self._stale_tips['icon'][i - 1] = _note
                _icon_lines.append(self._fmt_rank(i, c, n, _note))
            self._fill(self.icon_list, _icon_lines,
                       empty=i18n.tr('（暂无数据 · 双击桌面图标开始统计）',
                                     '(no data yet · double-click desktop icons)'))

            folder_total = len(self.usage_stats.folder_opens)
            folder_marks = self._audit_marks.get('folder') or {}
            self.folder_frame.configure(
                text=i18n.tr(f'📂 文件夹打开 Top {len(folder_rows)} · 共 {folder_total} 项',
                             f'📂 Folder Opens Top {len(folder_rows)} · {folder_total} total'))
            self._folder_lines = []
            self._folder_expanded = set()
            self._stale_tips['folder'] = {}
            for i, (n, c, _t) in enumerate(folder_rows, 1):
                short = self._short_path(n)
                _note = folder_marks.get(n)
                if _note:
                    self._stale_tips['folder'][i - 1] = _note
                # 缩略行与展开行都带 ⚠（双击展开时不许把标注抖掉）
                self._folder_lines.append((self._fmt_rank(i, c, short, _note),
                                           self._fmt_rank(i, c, n, _note)))
            self._fill(self.folder_list, [s for s, _f in self._folder_lines],
                       empty=i18n.tr('（暂无数据 · 通过文件夹组/资源页打开文件夹）',
                                     '(no data yet · open folders via folder groups)'))

            cats = sorted(self.click_tracker.get_all_clicks().items(),
                          key=lambda kv: (-kv[1], kv[0]))[:self.TOP_N]
            cat_marks = audit_board('category', [k for k, _v in cats],
                                    live_cats=self._live_category_names())
            self.cat_frame.configure(
                text=i18n.tr(f'🧭 分类切换 Top {len(cats)} · 共 {len(self.click_tracker.clicks)} 项',
                             f'🧭 Category Switches Top {len(cats)} · {len(self.click_tracker.clicks)} total'))
            self._stale_tips['category'] = {}
            _cat_lines = []
            for i, (k, v) in enumerate(cats, 1):
                _note = cat_marks.get(k)
                if _note:
                    self._stale_tips['category'][i - 1] = _note
                _cat_lines.append(self._fmt_rank(i, v, k, _note))
            self._fill(self.cat_list, _cat_lines,
                       empty=i18n.tr('（暂无数据 · 使用悬浮条切换分类）',
                                     '(no data yet · switch categories on the bar)'))

            self._log_stale(icon_marks, folder_marks, cat_marks)
        except Exception as e:
            logger.error(f"统计刷新失败: {e}")

    # ------------------ ★R286-A2 后台体检（单探针 + 超时 + 缓存） ------------------

    def _request_audit(self, icon_names, folder_names):
        """把碰磁盘的两榜体检排给后台线程；主线程这一轮只做一次名单比较。

        三道闸（缺一个就会出问题）：
          · 同一时刻只许**一个**探针在飞——两份结果抢同一份缓存，画出来忽有忽无；
          · 名单没变且距上次拿到结果不到 `AUDIT_REPROBE_SEC` ⇒ 不重探
            （统计页开着时每次点击都 refresh，这是高频动作）；
          · 探针超龄 ⇒ 判"卡死"：换世代号重探，旧线程变孤儿，它那份结果回来
            时世代不符直接丢（不然一个卡在网络盘上的线程能永久占着坑，
            标注从此再也不更新）。"""
        now = time.monotonic()
        if self._audit_probe_busy and \
                now - self._audit_probe_started > AUDIT_PROBE_TIMEOUT_SEC:
            logger.warning('[统计] 榜单体检探针 %ss 没回话（多半是磁盘/网络卡路住），'
                           '这一份作废，重新起一轮' % AUDIT_PROBE_TIMEOUT_SEC)
            self._audit_probe_busy = False
        if self._audit_probe_busy:
            return
        sig = (tuple(icon_names), tuple(folder_names))
        if sig == self._audit_sig and now - self._audit_last_done < AUDIT_REPROBE_SEC:
            return
        self._audit_gen += 1
        gen = self._audit_gen
        self._audit_sig = sig
        self._audit_probe_busy = True
        self._audit_probe_started = now
        try:
            threading.Thread(target=self._audit_probe, args=(gen, sig),
                             daemon=True, name='stats-audit').start()
        except Exception as e:
            # 起线程失败 ⇒ 只是这一轮没有 ⚠，不能连累榜单本身
            self._audit_probe_busy = False
            logger.warning(f'[统计] 榜单体检探针起不来（这次不标 ⚠）: {e}')
            return
        self._start_drain()

    def _audit_probe(self, gen, sig):
        """后台线程体：**不碰 Tk、不碰 self 上任何控件**（★R204 口径）。
        结果只往 queue 里丢一份，由主线程 after 轮询取。"""
        icon_names, folder_names = sig
        marks = {'icon': {}, 'folder': {}}
        marks['icon'] = audit_board('icon', icon_names)
        marks['folder'] = audit_board('folder', folder_names)
        self._audit_queue.put((gen, marks))

    def _start_drain(self):
        """排一次主线程轮询（已排过就不叠加，不然 after 会越排越多）。"""
        if self._audit_drain_after is not None:
            return
        try:
            self._audit_drain_after = self.root.after(AUDIT_DRAIN_MS,
                                                      self._drain_audit)
        except Exception as e:
            self._audit_drain_after = None
            logger.warning(f'[统计] 体检结果回投没排上（界面不受影响，下次刷新再试）: {e}')

    def _drain_audit(self):
        """主线程取后台结果：过期世代丢弃、拿到新的一份就重画一次。"""
        self._audit_drain_after = None
        fresh = None
        while True:
            try:
                gen, marks = self._audit_queue.get_nowait()
            except queue.Empty:
                break
            if gen == self._audit_gen:
                fresh = marks          # 同世代有多份取最后到的那份
        if fresh is None:
            if self._audit_probe_busy:
                self._start_drain()    # 还在飞：继续等，别把坑等死了
            return
        self._audit_probe_busy = False
        self._audit_last_done = time.monotonic()
        self._audit_marks = fresh
        try:
            self.refresh()
        except Exception as e:
            logger.warning(f'[统计] 体检结果回投后重画失败（榜单照旧可用）: {e}')

    def _live_category_names(self):
        """★R281-7：分类榜的"现在还有哪些分类"。gui 没注入回调 ⇒ [] ⇒ 这一榜不标。"""
        if self._category_names_cb is None:
            return []
        try:
            names = self._category_names_cb()
            return list(names) if names else []
        except Exception as e:
            logger.warning(f'[统计] 分类名单取不到（分类榜这次不标 ⚠）: {e}')
            return []

    def _log_stale(self, icon_marks, folder_marks, cat_marks):
        """一条汇总日志，且**只在标注集变了**的时候写。

        为什么必须去重：统计页开着时每次点击都会 refresh 一次，按次写日志
        一分钟就能刷出几十行同样的话（★R232 那条"提示反复出现"的同款病）。

        ★R300-F2：图标/文件夹两榜的体检碰磁盘、在后台线程跑。后台还没回话时
        这一轮手里只有分类榜那半份结果 ⇒ 先不写（写了就是"5 项"这种半成品
        数字，一秒后结果回来又写"24 项"，一次启动两条互相矛盾）。"""
        if getattr(self, '_audit_probe_busy', False):
            return
        sig = (tuple(sorted(icon_marks)), tuple(sorted(folder_marks)),
               tuple(sorted(cat_marks)))
        if sig == self._stale_logged:
            return
        self._stale_logged = sig
        n = len(icon_marks) + len(folder_marks) + len(cat_marks)
        if not n:
            return
        logger.info(
            f'[统计] 三张榜单有 {n} 项现场对不上（图标 {len(icon_marks)}／'
            f'文件夹 {len(folder_marks)}／分类 {len(cat_marks)}），已标 ⚠；'
            f'统计数字与记录一条没动。例：'
            + '；'.join(list(icon_marks.values())[:1]
                        + list(folder_marks.values())[:1]
                        + list(cat_marks.values())[:1]))

    def _on_toggle(self):
        """★ R30：开关按钮 → 调 gui 注入的切换回调（启停钩子+持久化在 gui 侧）"""
        try:
            self._on_toggle_cb()
        except Exception as e:
            logger.error(f'切换统计开关失败: {e}')

    def update_toggle_ui(self, on: bool):
        """★ R30/R32：同步开关按钮/状态文案（gui 切换完成后调用）。

        状态字用前景色并精简——此前 muted 小字在部分主题下看不清。"""
        try:
            if hasattr(self, 'btn_toggle_stats'):
                self.btn_toggle_stats.configure(
                    text=i18n.tr('⏸ 关闭统计', '⏸ Stop Stats') if on
                    else i18n.tr('▶ 打开统计', '▶ Start Stats'))
                self.lbl_stats_state.configure(
                    text=i18n.tr('统计采集中', 'Collecting') if on
                    else i18n.tr('统计已关闭', 'Statistics off'),
                    foreground=self.colors.get('fg', '#f8f8f2'))
        except Exception as e:
            logger.error(f'刷新统计开关状态失败: {e}')

    # ----------------------- ★ R32 字体调节 -----------------------

    def _adjust_font(self, delta: int):
        """A−/A+：调整三榜单字号（7~16），持久化到 app_settings"""
        self._font_size = max(7, min(16, self._font_size + delta))
        self._apply_font()
        if self._settings is not None:
            self._settings['stats_font_size'] = self._font_size
            try:
                if callable(self._save_settings):
                    self._save_settings()
            except Exception as e:
                logger.error(f'保存榜单字号失败: {e}')

    def reload_settings(self):
        """★R240（#137）：从 app_settings 重读榜单字号并立刻应用。

        字号原本只在面板构造时读一次（R32），「导入全部数据」把
        `stats_font_size` 落盘后没人回来重读 ⇒ 界面还是旧字号，用户以为
        "字号没跟着导出包走"。这里只做"读+夹范围+应用"，**不回写、不保存**
        （导入端已经存过盘，重复保存会把导入侧的校验绕开）。

        缺键回默认 9（旧导出包没这个键）；非数字/越界一律夹回 7~16，
        不让手改坏的 JSON 把榜单炸掉。"""
        try:
            size = int((self._settings or {}).get('stats_font_size', 9))
        except Exception:
            size = 9
        self._font_size = max(7, min(16, size))
        self._apply_font()

    def _apply_font(self):
        """把当前字号应用到三个榜单 Listbox"""
        for lb in (self.icon_list, self.folder_list, self.cat_list):
            try:
                lb.configure(font=('Microsoft YaHei UI', self._font_size))
            except Exception as e:
                # 字体缺失/控件已销毁 ⇒ 字号这一档没跟上，必须留痕（用户改字号
                # "没反应"这类报告只有这一行能判到底是没应用还是应用了没重画）
                logger.warning(f"[统计] 榜单字号应用失败: {e}")

    # ----------------------- ★ R32 说明弹窗 -----------------------

    def _show_help(self):
        """📖 说明：统计口径/开关/字体/数据存放——替代按钮行常驻小字"""
        win = tk.Toplevel(self.root)
        win.title(i18n.tr('📊 统计功能说明', '📊 Statistics Help'))
        win.geometry('560x420')
        win.transient(self.root)
        body = tk.Text(win, wrap='word', font=ui_layout.app_font(10),
                       bg=self.colors.get('surface', '#44475a'),
                       fg=self.colors.get('fg', '#f8f8f2'),
                       relief=tk.FLAT, padx=14, pady=10)
        body.pack(fill=tk.BOTH, expand=True)
        body.tag_configure('h', font=ui_layout.app_font(10, 'bold'),
                           foreground=self.colors.get('primary', '#8be9fd'),
                           spacing1=8, spacing3=2)

        def h(t):
            body.insert(tk.END, t + '\n', 'h')

        def line(t=''):
            body.insert(tk.END, t + '\n')

        h(i18n.tr('【统计口径】', '[What is counted]'))
        line(i18n.tr(
            '· 图标打开榜：桌面图标"双击打开"计 1 次；右键点在图标上也计入'
            '（近似口径，右键后是否真打开无法区分）',
            '· Icon opens: a desktop icon counts on double-click; right-click '
            'on an icon also counts (approximate — cannot tell if it opened)'))
        line(i18n.tr('· 文件夹打开榜：通过文件夹组 / 资源页打开文件夹',
                     '· Folder opens: folders opened via folder groups / Resources page'))
        line(i18n.tr('· 分类切换榜：点击顶部悬浮条切换分类（含 [全部]）',
                     '· Category switches: clicking the floating bar (incl. [全部/All])'))
        line(i18n.tr('· 榜单实时刷新：统计页开着时，每次记录立刻上榜',
                     '· Live refresh: while this page is open, every record appears instantly'))
        h(i18n.tr('【行首的 ⚠ 是什么意思】', '[What the ⚠ prefix means]'))
        line(i18n.tr('· 只是告诉你"这一项现在现场对不上了"：桌面图标被改名/删掉、'
                     '文件夹被挪走、分类被删掉或改名',
                     '· It only says "this entry no longer matches reality": the desktop '
                     'icon was renamed/deleted, the folder moved away, or the category '
                     'was deleted/renamed'))
        line(i18n.tr('· 鼠标停在 ⚠ 那一行上，会写清是哪一种（例如"像是改名叫「××」"）',
                     '· Hover the ⚠ row to see which case it is (e.g. "looks renamed to X")'))
        line(i18n.tr('· **统计数字一个都没改、记录一条都没删**：榜单记的是历史'
                     '（它当时真被打开过），东西挪回来 ⚠ 会自己摘掉',
                     '· **No count is changed and no record is deleted**: the board is '
                     'history (it really was opened). Move the item back and the ⚠ '
                     'goes away by itself'))
        line(i18n.tr('· 此电脑/回收站这类系统图标不会被标 ⚠（它们本来就不在桌面上）',
                     '· System entries like This PC / Recycle Bin are never marked '
                     '(they are not desktop files to begin with)'))
        h(i18n.tr('【统计开关】（按钮行最右）', '[Stats toggle] (rightmost button)'))
        line(i18n.tr('· 关闭后程序不再监视鼠标，三榜数据保留但不再新增；重启后保持',
                     '· Off: no mouse monitoring; existing data is kept and paused'))
        line(i18n.tr('· ★ 采集方式已改为"只读轮询"（不挂全局钩子、不依赖任何库）：'
                     '结束本程序进程不会再让鼠标卡一下',
                     '· Capture now uses read-only polling (no global hook, no '
                     'extra library): killing this app can no longer freeze the mouse'))
        line(i18n.tr('· 分发版（exe）默认关闭：杀毒软件可能对"鼠标活动统计"弹窗，'
                     '属统计功能的正常工作方式，不想要可不开',
                     '· Off by default in distributed builds: antivirus may flag the '
                     '"mouse activity stats" — normal for this feature'))
        h(i18n.tr('【榜单字体】', '[List font]'))
        line(i18n.tr('· A− / A+ 调整字号（7~16），自动记忆，重启保持',
                     '· A− / A+ changes font size (7~16), remembered across restarts'))
        h(i18n.tr('【数据在哪】', '[Where data lives]'))
        line(i18n.tr('· 全部只存本机：data\\usage_stats.json（图标/文件夹）、'
                     'data\\clicks.json（分类切换），不联网、不上传任何数据',
                     '· Local only: data\\usage_stats.json (icons/folders), '
                     'data\\clicks.json (switches) — never uploaded'))
        h(i18n.tr('【清零 / 导出】', '[Reset / Export]'))
        line(i18n.tr('· 🧹 清零 = 三榜归零；📤 导出 / 📥 导入 = 榜单数据 JSON 备份'
                     '（导入为合并，不清空现有）',
                     '· 🧹 Reset = clear all three; 📤 Export / 📥 Import = JSON '
                     'backup (import merges, does not clear)'))
        body.configure(state='disabled')
        ttk.Button(win, text=i18n.tr('关闭', 'Close'),
                   command=win.destroy).pack(pady=(0, 8))

    # ★ R256：`_pynput_ok`（R29）随去钩子化删除——轮询采集无外部库可缺。

    @staticmethod
    def _short_path(p: str) -> str:
        """★ R15：长路径缩略为 '盘符:\…\末级文件夹'（双击条目展开完整路径）"""
        try:
            p = p.rstrip('\\')
            drive, tail = os.path.splitdrive(p)
            if not drive:
                return p
            last = os.path.basename(tail) or tail
            return f'{drive}\\…\\{last}'
        except Exception:
            return p

    def _on_folder_dbl(self, event=None):
        """★ R15：双击文件夹条目 → 短路径 ↔ 完整路径 切换"""
        try:
            sel = self.folder_list.curselection()
            if not sel or sel[0] >= len(self._folder_lines):
                return
            i = sel[0]
            short, full = self._folder_lines[i]
            expanded = i in self._folder_expanded
            self.folder_list.delete(i)
            self.folder_list.insert(i, short if expanded else full)
            if expanded:
                self._folder_expanded.discard(i)
            else:
                self._folder_expanded.add(i)
        except Exception as e:
            logger.debug(f'路径展开切换失败: {e}')

    @staticmethod
    def _fill(lb, lines, empty='（暂无数据）'):
        lb.delete(0, tk.END)
        for line in lines:
            lb.insert(tk.END, line)
        if not lines:
            lb.insert(tk.END, empty)

    # ------------------------------------------------------------------

    def _remember_dir(self, key, path):
        """★ R200：记录对话框最后使用目录（settings 持久化）"""
        try:
            d = os.path.dirname(str(path or ''))
            if d and isinstance(self._settings, dict) and os.path.isdir(d):
                self._settings[key] = d
                if callable(self._save_settings):
                    self._save_settings()
        except Exception as e:
            # ★R287-N14：这条是"下次打开对话框还记不记得上次目录"的写盘路径，
            # 写失败只影响便利不影响数据，但静默就等于永远查不出来
            logger.warning(f"[统计] 记住上次目录失败（key={key}）: {e}")

    def _last_dir(self, key):
        """★ R200：默认目录=上次使用目录优先，无/失效回退空（系统记忆）
        ★R204 NEW-A5：设置被改坏成非字符串（dict/list）时 isdir 抛
        TypeError——非 str 一律视为无记录"""
        d = (self._settings or {}).get(key)
        if isinstance(d, str) and d and os.path.isdir(d):
            return d
        return ''

    def _on_export(self):
        """单独导出使用统计（图标打开 + 文件夹打开）"""
        path = filedialog.asksaveasfilename(
            parent=self.root, defaultextension='.json',
            initialdir=self._last_dir('last_export_dir'),
            initialfile=f'使用统计_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json',
            filetypes=[('JSON', '*.json')])
        if not path:
            return
        self._remember_dir('last_export_dir', path)
        if atomic_write_json(path, self.usage_stats.export_dict(),
                             indent=2, ensure_ascii=False):
            self._log(f'📤 已导出使用统计 → {path}')
        else:
            # ★ R27：写失败（磁盘满/只读）不再静默
            messagebox.showerror(
                i18n.tr('错误', 'Error'),
                i18n.tr(f'导出失败（文件写入被拒绝）: {path}',
                        f'Export failed (write was rejected): {path}'),
                parent=self.root)

    def _on_import(self):
        path = filedialog.askopenfilename(
            parent=self.root,
            initialdir=self._last_dir('last_import_dir'),
            title=i18n.tr('选择使用统计备份 (json)', 'Select stats backup (json)'),
            filetypes=[('JSON', '*.json')])
        if not path:
            return
        self._remember_dir('last_import_dir', path)
        # ★ R42：统一格式识别（拿错文件给指路提示）
        from io_formats import sniff, mismatch_message, KIND_STATS
        kind = sniff(path)
        if kind and kind != KIND_STATS:
            messagebox.showerror(
                i18n.tr('文件不匹配', 'File mismatch'),
                mismatch_message(KIND_STATS, kind),
                parent=self.root)
            return
        try:
            data = json.loads(Path(path).read_text(encoding='utf-8'))
            n = self.usage_stats.import_dict(data)
            self.refresh()
            self._log(f'📥 已导入使用统计（{n} 条）')
        except Exception as e:
            logger.error(f'导入使用统计失败: {e}')
            messagebox.showerror(i18n.tr('错误', 'Error'),
                                 i18n.tr(f'导入失败: {e}', f'Import failed: {e}'),
                                 parent=self.root)

    def _on_reset(self):
        if not messagebox.askyesno(
                i18n.tr('清零统计', 'Reset Statistics'),
                i18n.tr('将清零：图标打开榜、文件夹打开榜、分类切换榜。\n'
                        '此操作不可恢复，需要留底请先"📤 导出"。确定？',
                        'This clears: icon opens, folder opens and category '
                        'switches.\nThis cannot be undone — export first if '
                        'needed. Continue?'),
                parent=self.root):
            return
        self.usage_stats.reset()
        self.click_tracker.reset_clicks()
        self.refresh()
        self._log('🧹 使用统计已清零')
