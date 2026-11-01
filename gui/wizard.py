# -*- coding: utf-8 -*-
"""🧭 首次使用向导（R160，R125 设计落地 + 用户定案"首步引导备份桌面图标"）。

- 首次启动自动弹出（app_settings 'wizard_done'），设置页有"重看向导"。
- 7 步可跳过；每步可"去操作"（切到对应页签并暂存进度）。
- 纪律：**严禁 import gui.main_window**；gui 依赖全走参数。
"""
import logging
import tkinter as tk
from tkinter import ttk

from utils import i18n
from gui import ui_layout   # ★R228：界面字体等比缩放 helper

logger = logging.getLogger("desktop_manager")

# 选项卡中文键 → 英文页签文字里的词（main_window 的页签是
# `f"📁 {i18n.tr('管理','Files')}"` 这种双语写法）。
# ★R221（NEW7-P1-6）：_goto 原来只按中文子串找页签——English 界面下页签文字
# 是 "📁 Files"，向导里 4 个"去某某选项卡"按钮**全部点了没反应**（异常又被
# except: pass 吞掉）。加了键就要与 main_window 同步，E.2 门禁会盯住 STEPS
# 里用到的每个键。
_TAB_ALIAS = {
    '管理': 'Files',
    '规则': 'Rules',
    '设置': 'Settings',
    '资源': 'Resources',
    '家长': 'Parental',
    '系统': 'System',
    '日志': 'Logs',
    '数据管理': 'Data',
    '统计': 'Stats',
    '关于': 'About',
}

# (图标, (标题中, 标题英), (正文中, 正文英), 动作按钮 [((按钮中, 按钮英),
#  选项卡关键字), ...] or None)
# ★ R183（用户 15 批"叙述要跟软件的具体选项卡对上，要准确"）：正文里
# 一律写明「哪个选项卡」；选项卡名以 main_window 实际 nb.add 文字为准
# （📁 管理 / 🎛️ 规则 / 🧩 设置 / 📂 资源 / 👶 家长 / 🧰 系统 /
#   📜 日志 / ℹ️ 关于/About / 📦 数据管理）。按钮文字同步带选项卡名。
# ★R228（用户 22 批 item1「英文版本下，向导的说明部分还是中文」）：
# STEPS 改为存**双语源**（每段文案 = (中文, 英文)），渲染时经 step_content()
# 现选语言。为什么不在模块级直接写 i18n.tr(...)：本模块 import 期就会求值，
# 那时语言还没从 app_settings 读进来（R183 同类坑）——tr 必须推到渲染时。
# 英文文案里的选项卡/按钮/槽位名与软件 English 界面实际文字一致
# （Files/Rules/Settings/Resources/Parental/System/Logs/Data/Stats/About；
#  悬浮条见 core/dock.py 的 All / Main / Folders）。
STEPS = [
    ('👋',
     ('欢迎使用拾碎-懒人桌面工具箱', 'Welcome to DeskBits'),
     ('桌面图标分类管理：点悬浮条上的分类名，桌面立刻只显示那一类图标，'
      '其余全部隐藏。\n\n'
      '全程数据只保存在本机，不上传任何东西。本向导 1 分钟带你上手，'
      '随时可以跳过。',
      'Sort your desktop icons into categories: click a category name on the '
      'dock bar and the desktop instantly shows only that category — every '
      'other icon is hidden.\n\n'
      'All data stays on this PC; nothing is uploaded anywhere. This wizard '
      'takes about a minute and you can skip it at any time.'),
     None),
    ('💾',
     ('第一步：先备份桌面图标', 'Step 1: Back up the desktop first'),
     ('整理前先留底，怎么折腾都能一键还原（两个选项卡各一处，建议都做）：\n\n'
      '① 「📁 管理」选项卡 → 点"💾 保存全部"：保存图标位置（布局存档）\n'
      '② 「📦 数据管理」选项卡 → 点"📦 导出桌面图标（文件备份）"：'
      '连桌面文件一起打包成 zip\n\n'
      '两个都是 10 秒钟的事。',
      'Leave a restore point before tidying up, so one click can put '
      'everything back (one in each tab — doing both is recommended):\n\n'
      '① "📁 Files" tab → "💾 Save All": stores icon positions (layout '
      'archive)\n'
      '② "📦 Data" tab → "📦 Export Desktop Icons": zips the desktop files '
      'themselves\n\n'
      'Each one takes about ten seconds.'),
     [(('📁 去「管理」选项卡', '📁 Go to Files tab'), '管理'),
      (('📦 去「数据管理」选项卡', '📦 Go to Data tab'), '数据管理')]),
    ('📌',
     ('认识悬浮条', 'Meet the dock bar'),
     ('把鼠标移到屏幕顶部，悬浮条自动出现：\n\n'
      '· 点分类名 → 桌面只显示该分类\n'
      '· 点 [全部] → 所有图标按保存的位置就位\n'
      '· 右端 [窗] 呼出主窗口，[✕] 完全退出\n'
      '· "一键打开" 一键打开文件夹组/高频文件夹/桌面图标',
      'Move the mouse to the top edge of the screen and the dock bar slides '
      'out:\n\n'
      '· Click a category name → the desktop shows only that category\n'
      '· Click [All] → every icon returns to its saved position\n'
      '· [Main] at the right end opens this window, [✕] quits the app\n'
      '· [Folders] opens the current folder group with one click'),
     None),
    ('📂',
     ('新建分类、拖拽归类', 'New categories and drag-to-sort'),
     ('「📁 管理」选项卡左侧"➕ 新建"建分类（比如：工作、娱乐、学习）；\n'
      '然后点"🖱️ 拖拽分类"，把桌面图标拖到分类上就完成归类。\n\n'
      '分类颜色可在"🎨 改颜色"里换，悬浮条按分类色显示。',
      'Click "➕ New" on the left of the "📁 Files" tab to create categories '
      '(Work, Fun, Study…);\n'
      'then click "🖱️ Drag & Sort" and drop desktop icons onto a category.\n\n'
      '"🎨 Color" changes a category color — the dock bar follows it.'),
     [(('📁 去「管理」选项卡', '📁 Go to Files tab'), '管理')]),
    ('⌨️',
     ('全局快捷键', 'Global hotkeys'),
     ('· Ctrl+Alt+E  显示/隐藏悬浮条\n'
      '· Ctrl+Alt+0  显示全部图标（方框圈定）\n'
      '· Ctrl+Alt+1~9  切换到对应分类\n'
      '· Ctrl+Alt+F  快速定位图标（按名字搜索）\n\n'
      '改键：「🧩 设置」选项卡 → "⌨️ 全局快捷键"，双击条目即可改。',
      '· Ctrl+Alt+E  show / hide the dock bar\n'
      '· Ctrl+Alt+0  show every icon (framed)\n'
      '· Ctrl+Alt+1~9  switch to that category\n'
      '· Ctrl+Alt+F  quick find — search an icon by name\n\n'
      'To rebind: "🧩 Settings" tab → "⌨️ Global Hotkeys", then double-click '
      'an entry.'),
     [(('🧩 去「设置」选项卡', '🧩 Go to Settings tab'), '设置')]),
    ('✨',
     ('还有这些好用', 'Also handy'),
     ('· 「📊 统计」选项卡：图标/文件夹/分类三份榜单，统计开关也在这；\n'
      '  "排行榜显示"（桌面只显示最常用的前 N 名）的开关在「🎛️ 规则」'
      '选项卡，榜单数据来自「📊 统计」\n'
      '· 按屏调光（每台显示器各记各的亮度）→「🎛️ 规则」选项卡\n'
      '· 下载记录：浏览器下载到哪了，一查便知 →「📂 资源」选项卡\n'
      '· 家长模式：桌面只留儿童分类，到时自动锁屏 →「👶 家长」选项卡\n'
      '· 「🧰 系统」选项卡=实用工具集中营：一键锁屏（自定义画面与文字）、\n'
      '  事件查看器（系统日志翻译成大白话）、计划任务（定时关机/开软件）、\n'
      '  条件监视（盯某进程出现/消失自动执行动作）、\n'
      '  一键维护（清回收站/清临时文件等 8 个键，点了就弹结果）',
      '· "📊 Stats" tab: three leaderboards (icons / folders / categories) '
      'and the stats switch;\n'
      '  the "🏆 Rank Display" switch (desktop shows only the most used top N '
      'icons) is in the "🎛️ Rules" tab and reads from "📊 Stats"\n'
      '· Per-monitor dimming (each display keeps its own brightness) → '
      '"🎛️ Rules" tab\n'
      '· Download log: where did the browser put it → "📂 Resources" tab\n'
      '· Parental mode: the desktop keeps only kids categories, then locks '
      'the screen on time → "👶 Parental" tab\n'
      '· "🧰 System" tab = the toolbox: lock screen (custom picture and '
      'text),\n'
      '  Event Viewer (system log in plain words), Scheduled tasks (shutdown '
      'or launch on time),\n'
      '  Condition watch (run an action when a process appears or exits),\n'
      '  One-click maintenance (8 buttons: recycle bin, temp files and more '
      '— each reports back)'),
     None),
    ('🎉',
     ('完成，开始使用', 'Done — start using it'),
     ('点悬浮条分类名试试吧。\n\n'
      '· 本向导随时重看：「🧩 设置」选项卡 → "🧭 重看向导"\n'
      '· 用得顺手的话，欢迎到 Gitee 点个 Star / 提意见\n'
      '· 问题反馈：发布仓 Issues',
      'Try clicking a category name on the dock bar.\n\n'
      '· Watch this wizard again: "🧩 Settings" tab → "🧭 Replay Wizard"\n'
      '· If it helps you, a Star (or a suggestion) on Gitee is appreciated\n'
      '· Bugs and ideas: Issues on the release repo'),
     None),
]


def step_content(idx: int):
    """★R228：按**当前语言**取第 idx 步文案 ⇒ (图标, 标题, 正文, 按钮列表)。

    语言在渲染时才解析（原因见 STEPS 上方注释）；选项卡关键字是逻辑 ID，
    保持中文规范值不翻译（与 core/dock.py 槽位名同一纪律）。
    """
    icon, title, body, acts = STEPS[idx]
    return (icon, i18n.tr(title[0], title[1]),
            i18n.tr(body[0], body[1]),
            [(i18n.tr(pair[0], pair[1]), key) for pair, key in (acts or [])])


def maybe_show(gui, colors=None):
    """首次启动自动弹（延迟 2s 等主窗就位）。
    ★ R176：用户勾了"不再显示向导"（wizard_disabled）也跳过。"""
    if gui._app_settings.get('wizard_done') \
            or gui._app_settings.get('wizard_disabled'):
        return
    gui.root.after(2000, lambda: show(gui, colors))


def show(gui, colors=None):
    """打开/重看向导（设置页入口调用）"""
    for w in gui.root.winfo_children():
        if isinstance(w, WizardDialog) and w.winfo_exists():
            w.lift()
            w.focus_force()
            return
    WizardDialog(gui.root, gui, colors)


class WizardDialog(tk.Toplevel):
    def __init__(self, parent, gui, colors=None):
        super().__init__(parent)
        self.gui = gui
        self._colors = colors or {'bg': '#282a36', 'fg': '#f8f8f2',
                                  'primary': '#8be9fd',
                                  'muted': '#6272a4'}
        self.title(i18n.tr('使用向导', 'Getting Started'))
        self.geometry('640x430')
        self.configure(bg=self._colors['bg'])
        self.resizable(False, False)
        self.transient(parent)
        self.attributes('-topmost', True)
        # ★ R161：不再 grab_set——"去操作"步骤要切主窗页签让用户实际操作，
        # 模态会锁死主窗（用户实测点不进主窗口）。改非模态+保持置顶；
        # 点"去操作"后自动取消置顶让位，任务栏可召回。
        self.step = 0
        # ★R204 GUIB-10：点 ✕ 也走 _finish——否则勾了"不再显示"后直接
        # 关窗，该设置不落盘
        self.protocol('WM_DELETE_WINDOW', self._finish)
        self.body = ttk.Frame(self)
        self.body.pack(fill=tk.BOTH, expand=True, padx=22, pady=(16, 0))
        # ★ R176（用户 -06/7）：底部工具行=不再显示 + 导入/导出全部数据
        tools = ttk.Frame(self)
        tools.pack(fill=tk.X, padx=22, pady=(0, 0))
        self._never_var = tk.BooleanVar(
            value=bool(gui._app_settings.get('wizard_disabled')))
        ttk.Checkbutton(
            tools, text=i18n.tr('以后不再显示向导（设置页可重看）',
                                "Don't show again (see Settings page)"),
            variable=self._never_var).pack(side=tk.LEFT)
        ttk.Button(tools, text=i18n.tr('📤 导出全部数据', '📤 Export All'),
                   command=self._export_data).pack(side=tk.RIGHT)
        ttk.Button(tools, text=i18n.tr('📥 导入全部数据', '📥 Import All'),
                   command=self._import_data).pack(side=tk.RIGHT,
                                                   padx=(0, 6))
        nav = ttk.Frame(self)
        nav.pack(fill=tk.X, padx=22, pady=12)
        ttk.Button(nav, text=i18n.tr('⏭ 跳过向导', '⏭ Skip'),
                   command=self._finish).pack(side=tk.LEFT)
        ttk.Button(nav, text=i18n.tr('◀ 上一步', '◀ Back'),
                   command=self._prev).pack(side=tk.RIGHT, padx=(6, 0))
        self.next_btn = ttk.Button(nav, text=i18n.tr('下一步 ▶', 'Next ▶'),
                                   command=self._next, style='primary.TButton')
        self.next_btn.pack(side=tk.RIGHT)
        # ★R245 A3-2 曾在这里借主窗的 `_mark_checkboxes` 给「以后不再显示向导」
        # 补 ☑/☐ 文字前缀（向导是启动后才造的 Toplevel，不在主窗那次递归里）。
        # ★R315（批71，挂账 #272）：勾选态回到样式层的原生指示器（"色块档"），
        # 控件出生即带 ⇒ 这一刀连同那个助手一起退役。
        self._render()
        self.lift()
        self.focus_force()

    def _render(self):
        for c in self.body.winfo_children():
            c.destroy()
        # ★R228（item1）：文案经 step_content() 按当前语言解析（原来是直接把
        # 中文 STEPS 塞进 Label，English 界面整段向导仍是中文）
        icon, title, text, actions = step_content(self.step)
        ttk.Label(self.body, text=icon, font=('Segoe UI Emoji', 30),
                  background=self._colors['bg']).pack(pady=(0, 4))
        ttk.Label(self.body, text=title,
                  font=ui_layout.app_font(15, 'bold'),
                  foreground=self._colors['primary']
                  ).pack()
        ttk.Label(self.body, text=text, justify=tk.LEFT,
                  font=ui_layout.app_font(11),
                  foreground=self._colors['fg'],
                  wraplength=560).pack(pady=(10, 8))
        if actions:
            row = ttk.Frame(self.body)
            row.pack(pady=(0, 6))
            for btn_text, tab_key in actions:
                ttk.Button(row, text=btn_text,
                           command=lambda k=tab_key: self._goto(k)
                           ).pack(side=tk.LEFT, padx=4)
        dots = ' '.join('●' if i == self.step else '○'
                        for i in range(len(STEPS)))
        ttk.Label(self.body, text=dots,
                  foreground=self._colors['muted'],
                  font=ui_layout.app_font(9)).pack(side=tk.BOTTOM, pady=4)
        self.next_btn.configure(
            text=i18n.tr('完成 ✔', 'Done ✔')
            if self.step == len(STEPS) - 1 else i18n.tr('下一步 ▶', 'Next ▶'))
        # ★ R183（用户 15 批"向导断了/窗口显示不全"）：640x430 定死高度
        # 在长文步骤/高 DPI 下把底部工具行（不再显示复选框/导入导出）和
        # 导航行（下一步）挤出窗外。改为每步按内容实际需求自适应高度，
        # 首次居中显示，超出屏幕再封顶。
        self.update_idletasks()
        w = max(640, self.winfo_reqwidth())
        h = self.winfo_reqheight()
        max_h = self.winfo_screenheight() - 60
        if h > max_h:
            h = max_h
        if not getattr(self, '_sized_once', False):
            self._sized_once = True
            x = max(0, (self.winfo_screenwidth() - w) // 2)
            y = max(20, (self.winfo_screenheight() - h) // 2 - 24)
            self.geometry(f'{w}x{h}+{x}+{y}')
        else:
            self.geometry(f'{w}x{h}')

    def _goto(self, tab_key):
        """★ R161：切到对应页签 + 主窗置前 + 向导让位（不关闭）——
        用户操作完直接点"下一步"继续（此前 _finish 关了向导，用户实测
        "点了备份向导就终止了"）。"""
        try:
            nb = self.gui.notebook
            words = [tab_key, _TAB_ALIAS.get(tab_key, '')]
            hit = None
            for i in range(nb.index('end')):
                text = nb.tab(i, 'text')
                if any(w and w in text for w in words):
                    hit = i
                    break
            if hit is None:
                # ★R221：找不到不再无声吞（原来是 `except: pass` + 静默不跳）
                logger.warning(f'向导跳页：没有页签匹配 {tab_key}（双语键 {_TAB_ALIAS.get(tab_key)}）')
            else:
                nb.select(hit)
            self.gui.root.deiconify()
            self.gui.root.lift()
            self.gui.root.focus_force()
        except Exception as e:
            logger.warning(f'向导跳页异常: {e}')
        try:
            self.attributes('-topmost', False)   # 让位：主窗在前可操作
            self.lift()
        except Exception:
            pass

    def _next(self):
        if self.step >= len(STEPS) - 1:
            self._finish()
        else:
            self.step += 1
            self._render()

    def _prev(self):
        if self.step > 0:
            self.step -= 1
            self._render()

    def _finish(self):
        try:
            self.gui._app_settings['wizard_done'] = True
            # ★ R176：勾了"不再显示"则持久化，下次启动不再自动弹
            if self._never_var.get():
                self.gui._app_settings['wizard_disabled'] = True
            self.gui._save_app_settings()
        except Exception as e:
            # ★R333-e（P1，★R230b「兜底可以吞异常，不许沉默」）：这一句静默的代价
            # 是**下次开机向导又弹一遍**（`wizard_done` 没落盘），而 app.log 里零痕迹，
            # 用户只能读成"我明明点过完成"。留着窗照常关，但必须上一行。
            logger.warning('[向导] 完成时的设置没能保存（%r）⇒ 下次启动可能还会弹向导；'
                           '勾「不再显示」也一样无效，要永久关掉请在设置里改', e)
        self.destroy()

    def _export_data(self):
        """★ R176：向导内直接导出全部数据（复用数据管理页的现成链路）"""
        try:
            from gui import data_manage_page
            data_manage_page.on_export_all_data(self.gui)
        except Exception as e:
            from tkinter import messagebox
            messagebox.showerror(i18n.tr('错误', 'Error'), str(e),
                                 parent=self)

    def _import_data(self):
        """★ R176：向导内直接导入全部数据"""
        try:
            from gui import data_manage_page
            data_manage_page.on_import_all_data(self.gui)
        except Exception as e:
            from tkinter import messagebox
            messagebox.showerror(i18n.tr('错误', 'Error'), str(e),
                                 parent=self)



