# -*- coding: utf-8 -*-
"""🎨 主题面板（R146 自 gui/main_window.py 拆出，第五刀）。

COLORS 真身在此（apply_ui_theme 原地更新；main_window 与
manage/settings/rule/hotkey 各面板经别名/注入持有**同一 dict 对象**）。
含 UI_THEMES 四套色板 + apply_ui_theme + 显示层（_theme_disp/_theme_canon）
+ ThemeMixin（_on_ui_theme_change/_apply_theme_live/_setup_styles）。
纪律：**严禁 import gui.main_window**。
"""
import logging

import tkinter as tk
from tkinter import ttk

from utils import i18n
# ★R228（用户 22 批 item6）：_setup_styles 里 9 处样式字号改走
# ui_layout.app_font() 的**命名字体别名**（原来是一堆元组拷贝，切"界面字号"
# 一点不动）。ui_layout 是叶子模块（只依赖 tkinter），不违反"严禁 import
# gui.main_window"的纪律，也不构成依赖环。
from gui import ui_layout

logger = logging.getLogger("desktop_manager")

# ★R215（UI 美化阶段1）：Treeview 行高 22→28——22 配 9pt 中文太挤，
# 信息密集页（下载记录/数据管理）"显廉价"的直观来源之一。仍为本文件
# 唯一真身（ui_style 懒 import 引用），R213 单点化成果不变。
TREE_ROWHEIGHT = 28

# ★R225 阶段 5：行高的密度基准值（28 是 R215 定下的"标准"档）
TREE_ROWHEIGHT_BASE = 28


def apply_density(factor):
    """★R225：按密度因子重算 Treeview 行高 ⇒ 新行高。

    与 ui_layout.apply_density（间距表）配成一对，两者都由
    main_window._apply_display_prefs 在 _setup_styles 之前落一次。
    下限 18：再矮 9pt 中文就切字。改的是本模块单点真身，
    ui_style.configure_tree 每次调用时懒 import 读最新值（R213 单点化）。
    """
    global TREE_ROWHEIGHT
    try:
        f = float(factor)
    except (TypeError, ValueError):
        f = 1.0
    TREE_ROWHEIGHT = max(18, round(TREE_ROWHEIGHT_BASE * f))
    return TREE_ROWHEIGHT


# ★ R79e：界面主题显示层翻译（UI_THEMES 以中文键构建调色板，存储保持中文）
_UI_THEME_EN = {'深色': 'Dark', '暗黑': 'Black', '浅色': 'Light', '护眼': 'Eye Care'}
_UI_THEME_INV = {v: k for k, v in _UI_THEME_EN.items()}


def _theme_disp(n: str) -> str:
    if n in _UI_THEME_EN:
        return i18n.tr(n, _UI_THEME_EN[n])
    return n


def _theme_canon(v: str) -> str:
    return _UI_THEME_INV.get(v, v)



# Dracula 配色方案（2026-09-11：secondary 绿从 #50fa7b 调暗为 #2ea043，避免荧光绿刺眼）
# ★R216：#2ea043 在卡底上只有 4.22:1，作为"小节标题"这种 10pt 加粗彩色文字
# 不达标（WCAG 4.5）→ 提到 #4dd05f（card_bg 7.12 / surface 4.58，仍远暗于
# #50fa7b 的荧光绿，不违背 2026-09-11 调暗的初衷）
COLORS = {
    'bg': '#282a36',
    'fg': '#f8f8f2',
    'primary': '#8be9fd',
    'secondary': '#4dd05f',
    'accent': '#ff79c1',
    'warning': '#ffb86c',
    'error': '#ff5555',
    'surface': '#44475a',
    'border': '#636d83',
    # ==== ★R215 新增语义 token（每套主题各自给定，不再拿 border 当文字色）====
    # text_secondary：次要文字（副标题/状态栏/提示）。此前全项目 24 处用
    #   COLORS['border'] 当 foreground——浅色主题下 #d0d7de 压 #f5f6f8
    #   对比度≈1.1:1，等于隐形。
    # card_bg/card_border：卡片底/描边（分组现在"看得见"）。
    #   ★ card_bg 必须与 **bg** 同值（不是 surface）：ttk 的 TLabel/TFrame/
    #   TCheckbutton 没有透明通道、全部统一画 COLORS['bg']，卡片内每一行
    #   文字/每一个 Frame 都是一块 bg 色。卡片底若另起一档（surface），卡内
    #   就会满屏"补丁"（深色主题实测：#44475a 卡里嵌 #282a36 标签块）——
    #   这正是"分了组但更乱"的根因。所以分组可见性只由 1px card_border 承担，
    #   surface 专门留给"凹陷控件"（输入框/下拉/表格底）。
    # divider：1px 分隔线。  hover：控件悬停底色。
    # primary_soft：primary 的低饱和版，专供**大面积**底色（页签选中/表格
    #   选中行）——荧光青 #8be9fd 大面积铺出来刺眼，小面积强调仍用 primary。
    'text_secondary': '#bcc3d6',
    'card_bg': '#282a36',
    'card_border': '#4d5163',
    'divider': '#383c4a',
    'hover': '#3d4152',
    'primary_soft': '#3f6074',
    # 家长模式专用色（此前 #bd93f9/#ff5555 直接写死在样式里，浅色/护眼
    # 主题下仍是深紫边+大红字，不随主题）
    'kid_border': '#bd93f9',
    'kid_title': '#ff5555',
    # ★R223：表格语义 tag 的专用文字色。此前 sys_panel 三张表把深色主题的
    # 十六进制写死在 tag_configure 里（warn=#ffd866 淡黄、running=#50fa7b 荧光绿），
    # 浅色/护眼下浅字压浅底对比度≈1.6:1＝看不见（用户截图实证）。深色/暗黑
    # 保持原值不动（用户已认可观感），浅色/护眼给深一档的同色系。
    'warn_text': '#ffd866',
    'ok_text': '#50fa7b',
}

# 界面主题（2026-09-05）：每套都按"背景色×字体色"对比度成对设计，
# 浅色/护眼的文字统一换深色系，不能沿用深色主题的浅色文字
UI_THEMES = {
    '深色': dict(COLORS, muted='#9aa3b8', on_primary='#282a36',
                check_accent='#ff5555'),  # Dracula 原版；check_accent=勾选强调色
                                          # （用户实测：#ffb86c 橙、#f1fa8c 淡黄、
                                          #  #50fa7b 绿在深底小字号都看不清 → 定红）
    # 暗黑（2026-09-12 用户需求）：纯黑底 AMOLED 风，强调色沿用深色 Dracula
    # 那套（用户已喜欢）；surface/border 抬高一档让卡片/输入框与纯黑底分层
    '暗黑': {
        'bg': '#000000', 'fg': '#f2f2f0',
        'primary': '#8be9fd', 'secondary': '#2ea043',
        'accent': '#ff79c1', 'warning': '#ffb86c',
        'error': '#ff5555',
        'surface': '#17171c', 'border': '#3c3c46',
        'muted': '#9aa3b8', 'on_primary': '#0f0f14',
        'check_accent': '#ff5555',
        # ★R215 新 token（card_bg 与 bg 同值，理由见 COLORS 段注释：卡内
        # TLabel/TFrame 只能画 bg，另起一档就是满屏补丁）
        'text_secondary': '#a8b0c2', 'card_bg': '#000000',
        'card_border': '#5a5a68', 'divider': '#242430',
        'hover': '#22222c', 'primary_soft': '#2d4d57',
        'kid_border': '#bd93f9', 'kid_title': '#ff5555',
        'warn_text': '#ffd866', 'ok_text': '#50fa7b',
    },
    '浅色': {
        'bg': '#f5f6f8', 'fg': '#24292f',
        'primary': '#0969da', 'secondary': '#1a7f37',
        'accent': '#bf3989', 'warning': '#9a6700',
        'error': '#cf222e',
        'surface': '#ffffff', 'border': '#d0d7de',
        'muted': '#57606a', 'on_primary': '#ffffff',
        'check_accent': '#9a6700',
        # ★R215 新 token（浅色主题下次要文字必须真读得见——本轮对比度修复重点；
        # card_bg=bg 同"补丁"理由；hover 需比 bg 明显暗一档才有悬停反馈）
        'text_secondary': '#59636e', 'card_bg': '#f5f6f8',
        'card_border': '#d0d7de', 'divider': '#eaeef2',
        'hover': '#e3e8ee', 'primary_soft': '#ddf4ff',
        'kid_border': '#8250df', 'kid_title': '#a40e26',
        # ★R223：浅底配深字——淡黄/荧光绿在浅底上对比度≈1.6:1（用户截图实证看不清）
        'warn_text': '#9a6700', 'ok_text': '#1a7f37',
    },
    '护眼': {
        'bg': '#cce8cf', 'fg': '#233326',
        'primary': '#166534', 'secondary': '#3f6212',
        'accent': '#be185d', 'warning': '#92400e',
        'error': '#b91c1c',
        'surface': '#e3f2e6', 'border': '#9dbfa4',
        'muted': '#5a6b5e', 'on_primary': '#ffffff',
        'check_accent': '#92400e',
        'text_secondary': '#4f6353', 'card_bg': '#cce8cf',
        'card_border': '#7fa985', 'divider': '#b9d8bb',
        'hover': '#bcdcbc', 'primary_soft': '#a9d6ac',
        'kid_border': '#7c3aed', 'kid_title': '#9f1239',
        'warn_text': '#92400e', 'ok_text': '#3f6212',
    },
}



# ★R215：新 token 的兜底派生表——老主题字典/未来新主题缺键时不再 KeyError，
# 一律从该主题已有色推导（写死默认值反而会在某套主题下串色）。
_TOKEN_FALLBACK = {
    'text_secondary': ('muted',),
    'card_bg': ('surface',),
    'card_border': ('border',),
    'divider': ('border',),
    'hover': ('border',),
    'primary_soft': ('primary',),
    'kid_border': ('accent',),
    'kid_title': ('error',),
    'muted': ('border',),
    'on_primary': ('bg',),
    'check_accent': ('warning',),
    # ★R223：表格语义文字色缺键时退回同系 token（浅色=warning/secondary 已是深字）
    'warn_text': ('warning',),
    'ok_text': ('secondary',),
}


def apply_ui_theme(name: str):
    """把主题色板写进 COLORS（原地更新，别的模块若持有引用也能同步）"""
    # ★R318 K2：`name` 来自 app_settings.json 的 ui_theme——「导入全部数据」是整包
    # update() 零校验、那份档也官方推荐手改。旧写法 `UI_THEMES.get(name)` 对
    # **不可哈希**的脏值（数组/对象）不是落默认，而是 TypeError: cannot use 'list'
    # as a dict key；命中点在 `gui/main_window.py:385`——主窗构建链、这一句在 try
    # **之外** ⇒ 一个脏键就让程序打不开（launcher 重启 5 次后放弃）。留痕是必须的：
    # "回默认色板"只是表象，用户得能在 app.log 里看到到底哪个值被挡了。
    if not isinstance(name, str):
        logger.warning('界面主题档不是文字（%r）⇒ 本次按默认色板运行' % (name,))
        name = ''
    pal = dict(UI_THEMES.get(name) or UI_THEMES['深色'])
    # ★R215：缺键按上表推导兜底（旧实现只补 muted/on_primary 两个键，
    # 新增 token 一旦哪套主题漏写就是运行期 KeyError）
    for key, sources in _TOKEN_FALLBACK.items():
        if key in pal:
            continue
        for src in sources:
            if src in pal:
                pal[key] = pal[src]
                break
    COLORS.clear()
    COLORS.update(pal)
    # ★R222 阶段 4：ui_layout 不 import 主题（避免依赖环），色板由这里注入
    from gui import ui_layout as _ul
    _ul.set_default_colors(COLORS)



class ThemeMixin:
    def _on_ui_theme_change(self, event=None):
        """界面主题切换：实时生效，无需重启。

        实现思路：快照旧 COLORS → apply_ui_theme 更新 COLORS →
        重跑 _setup_styles（ttk 样式全部重配）→ 递归遍历所有控件，
        按"属性→颜色键"的显式映射替换颜色值。"""
        theme = _theme_canon(self.ui_theme_var.get())
        self._app_settings['ui_theme'] = theme
        self._save_app_settings()
        self._apply_theme_live(theme)
        self._log(f'🎨 主题已切换为 [{theme}]')

    def _apply_theme_live(self, theme: str):
        """实时应用主题（不重启进程）"""
        old_colors = dict(COLORS)               # 快照旧色板用于映射
        apply_ui_theme(theme)                    # 更新 COLORS
        self._setup_styles()                     # ttk 样式全部重配
        try:
            self.dock.apply_theme(theme, COLORS)   # 悬浮条换肤
        except Exception as e:
            logger.warning(f'悬浮条换肤失败（不影响其余换肤）: {e}')
        # 递归替换所有 tk/ttk 控件的颜色属性
        self._replace_widget_colors(self.root, old_colors)
        # ★R218 阶段 3：Listbox 的隔行底色是**逐行 itemconfig**，不在上面的
        # 属性映射表覆盖范围内 → 换肤后按新 COLORS 重刷一次，避免奇偶行留旧色。
        from gui import ui_layout
        for attr in ('explorer_list', 'category_list', 'layout_list', 'cat_detail'):
            lb = getattr(self, attr, None)
            if lb is not None:
                try:
                    ui_layout.zebra_listbox(lb, COLORS)
                except Exception as e:
                    logger.warning(f'列表隔行底色重刷失败（{attr}）: {e}')
        # ★R302：上面那张硬编码名单会漏掉"后来才加的表"（盒子卡就是），漏一条
        # ＝那张表的行底冻在建卡时的色板上。注册表兜住全部 zebra_listbox 调用方，
        # 名单保留是为了★R218 那批既有闸的语义不变。
        try:
            ui_layout.refresh_zebra_listboxes(COLORS)
        except Exception as e:
            logger.warning(f'全部列表斑马纹重刷失败: {e}')
        # ★R222 阶段 4：Treeview 斑马纹同理是逐行 tag 底色，也要按新色重刷
        try:
            ui_layout.refresh_zebra_trees(COLORS)
        except Exception as e:
            logger.warning(f'表格斑马纹重刷失败: {e}')
        # ★R223：系统页三张表的语义 tag 前景色（warn/err/running…）也是逐树
        # tag_configure，换肤不重刷就会"浅色底配深色主题的淡黄字"＝隐形
        try:
            from gui import sys_panel
            sys_panel.refresh_semantic_tags(COLORS)
        except Exception as e:
            logger.warning(f'语义标签色重刷失败: {e}')

    def _setup_styles(self):
        """设置 ttk 样式"""
        style = ttk.Style()
        style.theme_use('clam')

        style.configure('TFrame', background=COLORS['bg'])
        style.configure('TLabel', background=COLORS['bg'], foreground=COLORS['fg'])
        # ★ R109：补 fieldbackground/insertbackground——缺了它们深色主题下
        # 输入框字段仍是系统默认白底黑字（用户实测"输入的字体颜色不好看，
        # 跟框的颜色估计一样"）
        # ★R215：lightcolor/darkcolor 由 surface 换 card_border——原来输入框
        # 与卡片同色＝没有边框感，一排输入框"浮"在页面上对不齐；换成描边色
        # 后每个输入域有明确边界（聚焦时由 ui_style 覆盖成 primary）
        style.configure('TEntry',
                        fieldbackground=COLORS['surface'],
                        background=COLORS['surface'],
                        foreground=COLORS['fg'],
                        insertbackground=COLORS['fg'],
                        bordercolor=COLORS['card_border'],
                        lightcolor=COLORS['card_border'],
                        darkcolor=COLORS['card_border'])

        # ==== ★R215 按钮三档层级 ====
        # 现状：全项目 ~120 个 ttk.Button 只有 ~54 个写了 style，其余裸按钮
        # 与 secondary 视觉几乎同权重 → 一眼看不出"该点哪个"。
        # 定案（**只改样式表，不动 66 处裸按钮调用点**）：
        #   实底＝primary/accent/warning/error（主操作/强调/警示/危险）
        #   描边＝secondary（常规操作）——原来是整块深绿实底，一屏好几个彩色
        #         实心块＝噪音，降成描边后主操作才立得住
        #   ghost＝默认 TButton（裸按钮），由 ui_style.apply 统一渲染：
        #         无描边、hover 才提亮
        for color in ['primary', 'accent', 'warning', 'error']:
            style.configure(f'{color}.TButton',
                            background=COLORS[color],
                            foreground=COLORS['on_primary'],
                            font=ui_layout.app_font(10, 'bold'),
                            borderwidth=0, relief='flat',
                            padding=ui_layout.pad(12, 6))
        style.configure('secondary.TButton',
                        background=COLORS['card_bg'],
                        foreground=COLORS['fg'],
                        font=ui_layout.app_font(10),
                        borderwidth=1, relief='solid',
                        bordercolor=COLORS['card_border'],
                        # clam 的 Button 元素用 light/dark 画边，只设
                        # bordercolor 时 1px 边可能仍是默认立体色＝描边看不见
                        lightcolor=COLORS['card_border'],
                        darkcolor=COLORS['card_border'],
                        padding=ui_layout.pad(12, 6))
        style.map('secondary.TButton',
                  background=[('disabled', COLORS['bg']),
                              ('active', COLORS['hover'])],
                  foreground=[('disabled', COLORS['muted'])])
        # ★R251（07 批 item2e「计划任务的添加按钮不明显，容易误会没点到」）：
        # 主操作里再分一档"更大一号"——计划任务的 ➕ 创建 用它。配色仍走
        # primary / on_primary（跟主题实时变），只是字号 10→11、内边距翻倍。
        # hover/pressed 的 map 在 gui/ui_style.apply 里随四档实底一并补
        # （★R221：不配 map 会命中 ghost 档＝按下去先褪色再弹回）。
        style.configure('BigPrimary.TButton',
                        background=COLORS['primary'],
                        foreground=COLORS['on_primary'],
                        font=ui_layout.app_font(11, 'bold'),
                        borderwidth=0, relief='flat',
                        padding=ui_layout.pad(22, 10))

        # ★ R115：Treeview（下载记录卡）随主题——此前默认白底黑字，
        # 深色主题下刺眼（用户反馈"要跟主题颜色配合"）
        # ★R215：① 表头底色 bg→card_border，原来表头与表格外页面同色＝
        #   表头不立起来；② 选中行底 primary（荧光青）→primary_soft 低饱和版，
        #   大面积铺 primary 太刺眼，字色改用 fg（soft 色在深浅主题下都与 fg
        #   成对）；③ rowheight 22→28 见本文件顶部常量
        style.configure('Treeview',
                        background=COLORS['surface'],
                        foreground=COLORS['fg'],
                        fieldbackground=COLORS['surface'],
                        bordercolor=COLORS['card_border'],
                        rowheight=TREE_ROWHEIGHT)
        style.configure('Treeview.Heading',
                        background=COLORS['card_border'],
                        foreground=COLORS['fg'],
                        font=ui_layout.app_font(10, 'bold'),
                        borderwidth=0, relief='flat',
                        padding=ui_layout.pad(10, 6))
        style.map('Treeview',
                  background=[('selected', COLORS['primary_soft'])],
                  foreground=[('selected', COLORS['fg'])])

        # 卡片（分组框）：★R215 补 1px 描边（clam 下 light/dark 同色＝平边）。
        # 此前 borderwidth=0 ＝ "分了组但看不出来"，页面读起来是一整坨控件；
        # 底色走 card_bg（＝页面底，见文件头 token 注释），分组只靠描边，
        # 卡内 TLabel/TFrame 不会再露出色块补丁。
        # 注：多数调用点自带 padding='10'（控件级优先），此处 padding 只对
        # 没写的生效。
        style.configure('Card.TLabelframe',
                       background=COLORS['card_bg'],
                       borderwidth=1, relief='solid',
                       bordercolor=COLORS['card_border'],
                       lightcolor=COLORS['card_border'],
                       darkcolor=COLORS['card_border'],
                       padding=ui_layout.pad(12))

        # ★R315（批71，挂账 #272）：**原生指示器回到布局里**，画法为"色块档"。
        # ★R222 当年把指示器从布局里摘掉、改文字前缀 ☑/☐，是因为 clam 的勾笔画
        # 实测像个 ✗（有歧义）。本轮三支探针定案（取证 D:/Qoder-workplace/tmp/
        # bugflow/20261010_0740/）：
        #   · _r315_probe_variants.txt + _r315_shots/*_V2|V4*.png：原生勾的笔画在
        #     四主题下**都是叉**（红叉、深叉都读成 ✗）⇒ 不能留笔画，只能把笔画
        #     画成与块**同色**（看不见笔画），勾选态由"整块填 primary"表达；
        #   · _r315_margin.txt：indicatormargin 0~4 对宽度**没有影响**（恒 90），
        #     宽度全由描边色开不开决定 ⇒ margin=0 + card_border 描边与前缀画法
        #     **逐像素同宽**（106），48 枚复选框与 13 枚尺寸闸零重冻。
        # 配套退役：manage_panel._mark_checkboxes 那套文字前缀（同批删除）。
        _cb_ind = dict(indicatorbackground=COLORS['surface'],
                       indicatorforeground=COLORS['surface'],
                       indicatormargin=0,
                       upperbordercolor=COLORS['card_border'],
                       lowerbordercolor=COLORS['card_border'])
        _cb_ind_map = dict(
            indicatorbackground=[('selected', COLORS['primary']),
                                 ('active', COLORS['hover']),
                                 ('disabled', COLORS['card_bg'])],
            indicatorforeground=[('selected', COLORS['primary']),
                                 ('disabled', COLORS['muted'])])
        style.layout('TCheckbutton', [
            ('Checkbutton.padding', {'sticky': 'nswe', 'children': [
                ('Checkbutton.focus', {'side': 'left', 'sticky': 'nswe',
                                       'children': [
                    ('Checkbutton.indicator', {'side': 'left', 'sticky': ''}),
                    ('Checkbutton.label', {'sticky': 'nswe'})]})]})])
        style.configure('TCheckbutton', **_cb_ind)
        style.map('TCheckbutton', **_cb_ind_map)
        # 强调色勾选项（如"保留悬浮框"）：色板字段 check_accent 按主题给定——
        # 深色/暗黑 #50fa7b（鲜绿；用户实测 #ffb86c 橙、#f1fa8c 淡黄在深底
        # 小字号都看不清） / 浅色 #9a6700 / 护眼 #92400e。
        # ★ 加粗是关键：小字号彩色文字发虚主要是笔画细，加粗比换色治本。
        # _setup_styles 随主题切换重跑，天然跟随主题。
        # 派生样式（Accent / Startup.*）在下面 Startup 段之后**显式**再钉一遍
        # 同一套指示器色键——实测派生样式确实继承基样式的 map（_r315_gauge_probe
        # .txt），但★R222 的口径是"派生样式别赌继承"，显式钉住更耐改。
        style.configure('Accent.TCheckbutton',
                        foreground=COLORS.get('check_accent', COLORS['warning']),
                        font=ui_layout.app_font(10, 'bold'))
        style.configure('Card.TLabelframe.Label',
                       background=COLORS['card_bg'],
                       foreground=COLORS['primary'],
                       font=ui_layout.app_font(11, 'bold'))

        # ==== ★R216 阶段2：小节标题 + 分隔线（gui/ui_layout.group_block 消费）====
        # 分区不靠底色（阶段 1 实证：clam 下 TLabel/TFrame 永远画页面底，
        # 底色分区会在卡内露出色块补丁），改由"彩色小节标题 + 1px 分隔线"
        # 承担——颜色走样式名，_setup_styles 随换肤重跑 ⇒ 天然实时跟随主题，
        # 不必往 _replace_widget_colors 的映射表加键。
        for _sec in ('primary', 'accent', 'warning', 'error', 'secondary',
                     'text_secondary'):
            style.configure(f'Sec.{_sec}.TLabel',
                            background=COLORS['card_bg'],
                            foreground=COLORS[_sec],
                            font=ui_layout.app_font(10, 'bold'))
        style.configure('TSeparator', background=COLORS['divider'],
                        relief='solid', borderwidth=0)

        # 家长模式：醒目标题样式（大红 + 更大字体）
        # ★R215：#bd93f9/#ff5555 两处硬编码 hex 收进色板 token——原写法在
        # 浅色/护眼主题下仍是深紫边+大红字，与本文件"颜色一律取 COLORS、
        # 不写 hex 字面量"的纪律相违（唯一残留的两处）
        style.configure('Kid.TLabelframe',
                       background=COLORS['card_bg'],
                       borderwidth=1,
                       relief='solid',
                       bordercolor=COLORS['kid_border'],
                       lightcolor=COLORS['kid_border'],
                       darkcolor=COLORS['kid_border'],
                       padding=ui_layout.pad(12))
        style.configure('Kid.TLabelframe.Label',
                       background=COLORS['card_bg'],
                       foreground=COLORS['kid_title'],
                       font=ui_layout.app_font(12, 'bold'))

        # ★ R174：默认 TLabelframe/TRadiobutton/TSpinbox/TCombobox 随主题——
        # 此前只配了 Card./Kid. 具名样式，系统页"创建计划任务"内层的
        # 触发/动作分组框用默认样式，深色主题下仍是系统灰白（用户 -20/6
        # "创建计划任务没跟随主题"）。统一给默认样式配主题色，全项目生效。
        # ★R215：默认分组框与 Card 同款（描边 + 标题 11pt），全项目分组观感一致。
        # lightcolor/darkcolor 也一并压成 card_border——clam 的 LabelFrame 元素
        # 用这两个颜色画"立体"边，只设 bordercolor 时仍是上亮下暗的 3D 凸边。
        style.configure('TLabelframe', background=COLORS['card_bg'],
                        borderwidth=1, relief='solid',
                        bordercolor=COLORS['card_border'],
                        lightcolor=COLORS['card_border'],
                        darkcolor=COLORS['card_border'],
                        padding=ui_layout.pad(12))
        style.configure('TLabelframe.Label', background=COLORS['card_bg'],
                        foreground=COLORS['primary'],
                        font=ui_layout.app_font(11, 'bold'))
        # ★R215：单选/勾选的底色从 surface 改 card_bg（＝页面/卡片底）。
        # 原写法在 bg 底上是一块块 surface 补丁（R174 只解决了"系统灰白"，
        # 留下了"与卡片不同色"）。同时补 TCheckbutton——它此前根本没配
        # background，深色主题下走 clam 默认灰底（全项目 20+ 个勾选框）。
        style.configure('TRadiobutton', background=COLORS['card_bg'],
                        foreground=COLORS['fg'],
                        focuscolor=COLORS['card_bg'])
        # ★R251 item4（用户 43 批「单选这几项，没选中的前面没有小圈圈，看不见。
        # 勾选状态颜色区分一下」）：选中态用 primary 上色，跟未选中（fg）分开。
        # 圆圈本体仍靠 `manage_panel._mark_radios` 的文字前缀（● / ○）——勾选框
        # 那一族同类问题已由 ★R315 用"色块档"解决（原生笔画读成 ✗ ⇒ 把笔画配成
        # 与块同色），单选钮的实心/空心圆要另做一档，本批未动（挂账）。
        # 状态表按"专用在前、通用在后"排：disabled 要先于 selected 命中。
        style.map('TRadiobutton',
                  background=[('active', COLORS['hover']),
                              ('disabled', COLORS['card_bg'])],
                  foreground=[('disabled', COLORS['muted']),
                              ('selected', COLORS['primary'])])
        style.configure('TCheckbutton', background=COLORS['card_bg'],
                        foreground=COLORS['fg'],
                        focuscolor=COLORS['card_bg'])
        style.map('TCheckbutton',
                  background=[('active', COLORS['hover']),
                              ('disabled', COLORS['card_bg'])],
                  foreground=[('disabled', COLORS['muted'])])
        style.configure('TSpinbox', fieldbackground=COLORS['surface'],
                        background=COLORS['surface'],
                        foreground=COLORS['fg'],
                        insertbackground=COLORS['fg'],
                        buttonbackground=COLORS['surface'],
                        arrowcolor=COLORS['fg'],
                        bordercolor=COLORS['card_border'],
                        lightcolor=COLORS['card_border'],
                        darkcolor=COLORS['card_border'])
        style.configure('TCombobox', fieldbackground=COLORS['surface'],
                        background=COLORS['surface'],
                        foreground=COLORS['fg'],
                        arrowcolor=COLORS['fg'],
                        insertbackground=COLORS['fg'],
                        bordercolor=COLORS['card_border'],
                        lightcolor=COLORS['card_border'],
                        darkcolor=COLORS['card_border'])
        style.map('TCombobox',
                  fieldbackground=[('readonly', COLORS['surface']),
                                   ('disabled', COLORS['bg'])],
                  foreground=[('readonly', COLORS['fg']),
                              ('disabled', COLORS['muted'])],
                  arrowcolor=[('disabled', COLORS['muted'])],
                  bordercolor=[('focus', COLORS['primary'])])

        # 选项卡：随主题配色（clam 默认灰不搭）
        # ★R215：① padding (12,5)→(14,8)——原来页签又扁又挤，是"显廉价"来源；
        #   ② 选中底 primary（荧光青整块翻色）→primary_soft 低饱和，字色配 fg；
        #   ③ 未选中底 surface 与页签栏 bg 分层
        style.configure('TNotebook', background=COLORS['bg'], borderwidth=0,
                        tabmargins=(12, 8, 12, 0))
        style.configure('TNotebook.Tab', background=COLORS['surface'],
                        foreground=COLORS['text_secondary'],
                        font=ui_layout.app_font(10),
                        borderwidth=0,
                        # ★R303：14 是设计档（★R215），但主窗在缩放/大字号下会
                        # 把它收到 6 档来保住整排页签不被裁——所以这里必须读
                        # ui_layout 的那一档，写死 14 会让换肤把自适应刷回去。
                        padding=ui_layout.pad(ui_layout.tab_pad_h(), 8))
        style.map('TNotebook.Tab',
                  background=[('selected', COLORS['primary_soft']),
                              ('active', COLORS['hover'])],
                  foreground=[('selected', COLORS['fg']),
                              ('active', COLORS['fg'])],
                  # ★R218 阶段 3：选中页签加 2px primary 描边当"活动指示"。
                  # 本机 clam 无 'border' 元素类型（自定义 layout 走不通），
                  # 而 Notebook.Tab 认 borderwidth/bordercolor → 用 map 零风险实现，
                  # 颜色取 primary 随主题变，比整块翻色更清爽。
                  borderwidth=[('selected', 2)],
                  bordercolor=[('selected', COLORS['primary'])],
                  expand=[('selected', (0, 0, 0, 0))])

        # ★R262：「自动打开文件夹」卡专用样式（Startup.*）——ttk 控件构造时
        # 不认 font= 选项（实测 TclError: unknown option "-font"），字体只能
        # 走样式；这里显式钉成界面字号，保证卡内标签/勾选/单选/下拉与
        # LabelFrame 标题同档（用户 59 批「上下的字体还不一样」）。
        _so_f = ui_layout.app_font(int(getattr(self, 'ui_font_px', 11)))
        style.configure('Startup.TLabel', background=COLORS['card_bg'],
                        foreground=COLORS['fg'], font=_so_f)
        style.configure('Startup.TCheckbutton', background=COLORS['card_bg'],
                        foreground=COLORS['fg'], font=_so_f,
                        focuscolor=COLORS['card_bg'])
        style.configure('Startup.Accent.TCheckbutton',
                        background=COLORS['card_bg'],
                        foreground=COLORS.get('check_accent', COLORS['warning']),
                        font=ui_layout.app_font(
                            int(getattr(self, 'ui_font_px', 11)), 'bold'),
                        focuscolor=COLORS['card_bg'])
        style.map('Startup.TCheckbutton',
                  background=[('active', COLORS['hover']),
                              ('disabled', COLORS['card_bg'])],
                  foreground=[('disabled', COLORS['muted'])])
        style.map('Startup.Accent.TCheckbutton',
                  background=[('active', COLORS['hover']),
                              ('disabled', COLORS['card_bg'])],
                  foreground=[('disabled', COLORS['muted'])])
        # ★R315：上面三份派生勾选样式各自**显式**钉一遍指示器色键（同一份
        # _cb_ind / _cb_ind_map），理由见 TCheckbutton 那段注释——不赌继承。
        # 注意只传这五个键：style.map 是逐选项覆写，不会把各自的 background/
        # foreground 那两份既有 map 吃掉。
        for _cb_style in ('Accent.TCheckbutton', 'Startup.TCheckbutton',
                          'Startup.Accent.TCheckbutton'):
            style.configure(_cb_style, **_cb_ind)
            style.map(_cb_style, **_cb_ind_map)
        style.configure('Startup.TRadiobutton', background=COLORS['card_bg'],
                        foreground=COLORS['fg'], font=_so_f,
                        focuscolor=COLORS['card_bg'])
        style.map('Startup.TRadiobutton',
                  background=[('active', COLORS['hover']),
                              ('disabled', COLORS['card_bg'])],
                  # ★R264 批17（用户 17 批 B2b）："选了另一个，被选中的字还是黑的，
                  # 只有下拉变灰" ⇒ 未选中的那一项**文字**也一起降色。ttk 控件不认
                  # 组件级 -foreground（★R222 实测 TclError），只能走样式 map；
                  # 这里用 `!selected` 而不是 `state(['disabled'])`——单选框必须保持
                  # 可点（置灰≠禁用，用户随时能切回另一档）。
                  foreground=[('selected', COLORS['primary']),
                              ('!selected', COLORS['muted']),
                              ('disabled', COLORS['muted'])])
        style.configure('Startup.TCombobox', font=_so_f)
        style.configure('Startup.TLabelframe', background=COLORS['card_bg'],
                        borderwidth=1, relief='solid',
                        bordercolor=COLORS['card_border'],
                        lightcolor=COLORS['card_border'],
                        darkcolor=COLORS['card_border'],
                        padding=ui_layout.pad(10))
        style.configure('Startup.TLabelframe.Label', background=COLORS['card_bg'],
                        foreground=COLORS['primary'],
                        font=ui_layout.app_font(
                            int(getattr(self, 'ui_font_px', 11)), 'bold'))

        # ★R211（简单美化）：样式增强层——默认按钮扁平化+hover/pressed
        # 微交互、滚动条扁平化、进度条/输入框聚焦反馈、Treeview 行高
        # （theme_panel.TREE_ROWHEIGHT 单点常量）。
        # 只引用 COLORS（同一原地更新引用），随本函数重跑自动跟随主题。
        # ★R215：本层在 _setup_styles **最后**执行＝默认 TButton（裸按钮，
        # 全项目约 66 处不写 style）的终态由它决定＝ghost 档，因此不必去
        # 逐处补 style= 就能拿到三档层级。
        try:
            from gui import ui_style
            ui_style.apply(style, COLORS)
        except Exception as e:
            logger.warning(f'ui_style 应用失败（不影响功能）: {e}')

