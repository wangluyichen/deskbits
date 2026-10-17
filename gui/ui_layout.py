# -*- coding: utf-8 -*-
"""★R216 UI 阶段 2：布局骨架 helper（间距常量 + 分组块 + 等宽按钮排 + 表单行）。

治的是"控件都在、但看着别扭"的根因：历手写 `pack(side=LEFT, padx=…)` 让一排
按钮宽度随文字长短不齐、上下行左右边缘对不上、标签列忽左忽右。

纪律：**严禁 import gui.main_window**；本模块只依赖 tkinter/ttk。
颜色一律走 ttk **样式名**（`Sec.<accent>.TLabel`、`TSeparator`），由
`theme_panel._setup_styles()` 在换肤时重配——实时切主题自动跟随，既不必往
`_replace_widget_colors` 的映射表加键，也不会漏色（阶段 1 的教训）。

用法速记（group body 内一律 pack，网格只发生在 form_block 返回的子容器里）：
    outer, body = group_block(parent, i18n.tr('外观', 'Appearance'), 'primary')
    outer.grid(row=0, column=0, sticky='nw', padx=(0, SP['l']))
    form = form_block(body)
    form_row(form, 0, i18n.tr('模式:', 'Mode:'), combo)
    form_row(form, 1, i18n.tr('颜色:', 'Color:'), combo2)
    action_bar(body, [('▶ …', cb, 'primary.TButton'), ('⏹ …', cb2, None)], cols=2)
"""
import tkinter as tk
from tkinter import ttk
import weakref

# 5 档间距基准值（"标准"档）。★R225 阶段 5：密度三档整体缩放的是这张表，
# 而不是回去改几百个调用点——调用点一律读 SP['k']，改的只有 SP 本身。
SP_BASE = {'xs': 4, 's': 8, 'm': 12, 'l': 16, 'xl': 24}
SP = dict(SP_BASE)

# 密度档 ⇒ 缩放因子（规范键=中文，显示层翻译在 settings_panel，同主题色口径）
DENSITY = {'紧凑': 0.8, '标准': 1.0, '宽松': 1.25}

# ★R298-E：apply_density 把当前因子记在这里，pad() 用它缩放 style 的 padding=。
# SP 只管 padx/pady 那一路；theme_panel/_setup_styles 里那 10 处按钮/卡片/页签
# `padding=(12, 6)` 类字面量以前免疫密度档 ⇒ 切"宽松"看到 SP 松了、行高了，
# 唯独按钮内边距不动，主视图仍"挤"。这批用 pad() 收口。
_density_factor = 1.0


def apply_density(name):
    """按密度档**原地**重算 SP ⇒ 因子。

    必须原地 update 而不是换一个新 dict：几百个调用点在构建期读 `SP['k']`，
    另有模块 `from gui.ui_layout import SP` 持有引用。未知档按标准处理。
    下限 1px：紧凑档下 4×0.8=3.2→3 仍够用，但不能出现 0 把控件黏一起。
    """
    global _density_factor
    # ★R318 K2：ui_density 来自 app_settings.json（「导入全部数据」整包 update 零校验、
    # 官方推荐手改）。旧写法对**不可哈希**脏值（数组/对象）不是落默认，而是
    # TypeError: cannot use 'list' as a dict key，命中点在 main_window:394
    # `_apply_display_prefs()`（主窗构建链、无 try）⇒ 一个脏键就让程序打不开。
    # 字符串脏值照旧安全回标准档，这里只挡"函数自己不炸"这一道。
    if not isinstance(name, str):
        name = ''
    f = DENSITY.get(name, 1.0)
    _density_factor = f
    for k, v in SP_BASE.items():
        SP[k] = max(1, round(v * f))
    return f


def pad(*args):
    """按当前密度档缩放 ttk `padding=` 参数；下限 1。

    用法：`padding=pad(12, 6)` / `padding=pad(12)`。单值回 int，多值回 tuple。
    与 SP 用同一 `_density_factor`——apply_density 一调两边同缩放（P5 一致性）。
    """
    f = _density_factor
    if len(args) == 1:
        return max(1, round(args[0] * f))
    return tuple(max(1, round(a * f)) for a in args)


# ---------------------------------------------------------------------
# ★R303 页签内边距：设计档 14，缩放/大字号下窗口放不下整排页签时按档收
# ---------------------------------------------------------------------
# 病灶：10 枚页签的总宽 = Σ文字宽 + 2×内边距×10 + 页签栏边距，全随 `tk scaling`
# 和「界面字号」长大，而窗口宽是像素上限（★R215 把内边距从 12 加到 14 那次
# 没人量过"最坏组合下还放得下吗"）。win11 125% + 字号大实测：页签排 1064px、
# 可用正好 1064px ⇒ 最后一枚「关于/About」裁在窗口边上（用户截图）。
# 收内边距是**唯一不动文案、不动结构**就能让整排回来的杠杆，所以留一个可调档：
# theme_panel 的样式表和 main_window 的自适应都从这里取值，换肤后不会被刷回 14。
TAB_PAD_DESIGN = 14                  # ★R215 的设计档（不挤时的默认）
TAB_PAD_MIN = 6                      # 收到这里字就贴边了，不再收
_tab_pad_h = TAB_PAD_DESIGN


def tab_pad_h():
    """当前页签左右内边距（未过密度档的原始值）。"""
    return _tab_pad_h


def set_tab_pad_h(v):
    """记一档页签内边距（调用方负责随后重刷样式表）。"""
    global _tab_pad_h
    try:
        v = int(v)
    except (TypeError, ValueError):
        return _tab_pad_h
    _tab_pad_h = max(TAB_PAD_MIN, min(TAB_PAD_DESIGN, v))
    return _tab_pad_h


# ---------------------------------------------------------------------
# ★R228 界面字体：让"字体与槽位 → 界面字体"真的能换动全界面字号
# ---------------------------------------------------------------------
# 病灶（用户 22 批 item6「界面字体感觉换大换小没什么效果」）：全项目有 88 处
# 写死的 `('Microsoft YaHei UI', N)` 字号元组 + theme_panel/_setup_styles 里
# 9 处样式 font= 元组。元组是**值拷贝**，改 `gui.ui_font`（唯一那个命名字体）
# 只带动 4 个 Listbox，其它文字全部免疫 ⇒ 看起来"没效果"。
#
# 治法：字号一律经 app_font() 拿一个**命名字体别名**（'uf1' 这种）。命名字体
# 是引用，改一次 size 全界面实时跟着变——已用真 Tk 实测钉住（tk.Label 26→34px、
# ttk.Button 36→44px，见 tmp/bugflow/.../_font_probe_r228.txt）。
# 缩放按**等比**：基准档 11（='标准'），选"大"13 ⇒ 12px 的小标题变 14px，
# 层级关系不丢（比"全界面统一 13px"更符合原设计）。
UI_FONT_FAMILY = 'Microsoft YaHei UI'
UI_FONT_BASE = 11                    # '标准'档（= settings_panel.UI_FONT_SIZES['标准']）
_font_root = None
_ui_font_base = UI_FONT_BASE
_fonts = {}                          # (原始字号, 样式元组) -> 别名
_font_objs = {}                      # 别名 -> tkinter Font 对象（★见 app_font 注释）
_font_seq = 0
MIN_FONT = 6                         # 缩到 6 以下已经看不清，封底


def init_fonts(root, base_size=None):
    """主窗 root 一创建就调用：绑定字体宿主 + 记基准档（顺序早于 _build_ui）。

    base_size 用整数磅值（Tk 里正数=磅，负数=像素；本项目历史全用正数，这里不换单位）。

    ★R228 第二坑（S3 回归抓到）：**命名字体属于某一个 Tcl 解释器**。门禁测试
    在同一进程里先建中文窗、destroy 后再建英文窗，而 _fonts/_font_objs 是模块级
    缓存 ⇒ 第二个实例拿到的是"上一个 root 的别名名"，那个字体已随 root 销毁被
    Tk 删掉 ⇒ 控件只能退回默认字体（实测英文档每枚按钮宽度和 11/13 档完全无关，
    就是悬空别名的症状）。所以**宿主换了就清空注册表**，让新 root 重建自己的别名。
    """
    global _font_root, _ui_font_base, _fonts, _font_objs, _font_seq
    if _font_root is not root:
        _fonts = {}
        _font_objs = {}            # 只丢引用，别去 configure/delete 已死的解释器
        _font_seq = 0
    _font_root = root
    try:
        px = int(base_size) if base_size else UI_FONT_BASE
    except (TypeError, ValueError):
        px = UI_FONT_BASE
    _ui_font_base = max(MIN_FONT, px)
    return _ui_font_base


def ui_font_base():
    """当前基准档（磅值），main_window 建 ui_font 时按它取值。"""
    return _ui_font_base


def ui_font_scale():
    """界面字体缩放因子 = 当前档 / '标准'档。"""
    return _ui_font_base / UI_FONT_BASE


def scaled_size(size):
    """把设计期字号按等比换算成实际字号（封底 MIN_FONT，避免 0/负数）。"""
    return max(MIN_FONT, int(round(size * ui_font_scale())))


def app_font(size, *styles):
    """⇒ 与 `('Microsoft YaHei UI', size[, 'bold'…])` 等价的字体规格。

    返回的是**命名字体别名**（字符串，Tk 任何接受 -font 的地方都能用：tk 控件、
    ttk 样式、Text tag、Menu），并按当前基准档等比取实际字号；同一
    (字号, 样式) 组合全进程只建一次。
    root 还没就绪（极早期/纯单测）时退回普通元组，行为与改造前一致。
    """
    global _font_seq
    try:
        size = int(size)
    except (TypeError, ValueError):
        return (UI_FONT_FAMILY, 11) + tuple(styles)
    key = (size, tuple(styles))
    name = _fonts.get(key)
    if name:
        return name
    if _font_root is None:
        return (UI_FONT_FAMILY, size) + tuple(styles)
    from tkinter import font as _tkfont      # 只在真有 root 时才拉 tkinter.font
    _font_seq += 1
    name = 'uf%d' % _font_seq
    opts = {'family': UI_FONT_FAMILY, 'size': scaled_size(size)}
    for s in styles:
        s = str(s).lower()
        if s == 'bold':
            opts['weight'] = 'bold'
        elif s in ('italic', 'oblique'):
            opts['slant'] = 'italic'
        elif s == 'underline':
            opts['underline'] = 1
        elif s == 'overstrike':
            opts['overstrike'] = 1
    try:
        font_obj = _tkfont.Font(root=_font_root, name=name, **opts)
    except Exception:
        return (UI_FONT_FAMILY, size) + tuple(styles)
    # ★R228 踩过的坑（不写这里下一个人一定再踩）：**必须持有 Font 对象**。
    # tkinter 的 Font.__del__ 会执行 `font delete <name>`——只把名字字符串存进
    # _fonts、对象当场被引用计数回收 ⇒ 别名变悬空引用（实测 nametofont('uf1')
    # 报 "named font uf1 does not already exist"，控件按 -font uf1 渲染时只能
    # 退回默认字体，整套缩放机制静默失效）。
    _font_objs[name] = font_obj
    _fonts[key] = name
    return name


def set_ui_font_base(px):
    """切"界面字体"档：原地重配所有已建别名 ⇒ 全界面（含已显示的 ttk 样式）实时变。

    返回生效后的基准档。别名不销毁，下次再切只改数值。
    """
    global _ui_font_base
    try:
        px = int(px)
    except (TypeError, ValueError):
        return _ui_font_base
    _ui_font_base = max(MIN_FONT, px)
    if _font_root is not None:
        from tkinter import font as _tkfont
        for key, name in list(_fonts.items()):
            try:
                obj = _font_objs.get(name)
                if obj is None:                     # 兼容：老进程遗留/手工建的别名
                    obj = _tkfont.nametofont(name, root=_font_root)
                    _font_objs[name] = obj
                obj.configure(size=scaled_size(key[0]))
            except Exception:
                continue
    return _ui_font_base


def font_alias_table():
    """★测试用：当前登记的 (字号, 样式) → 别名 表（只读副本）。"""
    return dict(_fonts)


def font_obj(name):
    """★测试用：取别名对应的 Font 对象（持有引用防 delete，也便于量实际磅值）。"""
    return _font_objs.get(name)

# 小节标题的强调色档，对应 theme_panel 里的 Sec.<k>.TLabel
# ★R216：只用"4 套主题下压 card_bg 都 ≥4.5:1"的颜色——muted 在护眼下 4.33
# 不达标已剔除；深色 secondary 同步提亮到 #4dd05f 才够格入选。
SEC_ACCENTS = ('primary', 'accent', 'warning', 'error', 'secondary',
               'text_secondary')


def group_block(parent, title, accent='primary'):
    """彩色小节标题 + 1px 分隔线 + 内容容器 ⇒ (outer, body)。

    分组靠"标题色 + 下划线"立起来，不靠底色——阶段 1 已实证 clam 主题下
    TLabel/TFrame 永远画页面底色，用底色分区会在卡内露出色块补丁。
    """
    if accent not in SEC_ACCENTS:
        accent = 'text_secondary'
    outer = ttk.Frame(parent)
    ttk.Label(outer, text=title, style=f'Sec.{accent}.TLabel').pack(anchor='w')
    ttk.Separator(outer, orient=tk.HORIZONTAL).pack(
        fill=tk.X, pady=(SP['xs'], 0))
    body = ttk.Frame(outer)
    body.pack(fill=tk.X, pady=(SP['s'], 0))
    return outer, body


def _blend_hex(c1, c2, t):
    """两 hex 颜色线性混合（斑马纹底色用，随主题变化）；非 hex 输入退回 c1。"""
    try:
        a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
        b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
        return '#' + ''.join(
            f'{round(x + (y - x) * t):02x}' for x, y in zip(a, b))
    except (ValueError, IndexError, TypeError):
        return c1


def zebra_listbox(lb, colors=None, base_key='surface'):
    """★R218 阶段 3：给 tk.Listbox 上隔行底色（斑马纹），提升长列表可读性。

    奇数行铺 `blend(base, fg, 0.04)`——与 sys_panel 的 Treeview 斑马纹同一配方
    （4% 混色，深浅主题都不刺眼），偶数行保持控件自身底色。颜色由调用方传入
    当前 COLORS（本模块不 import 主题，避免依赖环）；省略时取
    `set_default_colors()` 注入的当前色板。

    ★R302：逐行 `itemconfig(background=…)` **不在**换肤的属性映射表覆盖范围内
    （那张表只能改控件级 bg/fg，改不到某一行），所以只靠"建卡那一刻刷一次"就会
    把行底**冻在旧色板**上——浅底配浅字＝看不见（用户报"主题除了浅色都不能看见"）。
    本函数顺手把这张表登记进 `_ZEBRA_LISTBOXES`，换肤后由
    `refresh_zebra_listboxes()` 按新色重刷（与 Treeview 的 `refresh_zebra_trees`
    同一套路）。
    依赖：只读 lb.size()/itemconfig()，对空列表安全。"""
    c = colors or _DEFAULT_COLORS
    base = c.get(base_key) or c.get('bg') or '#000000'
    stripe = _blend_hex(base, c.get('fg', '#ffffff'), 0.04)
    try:
        n = lb.size()
    except Exception:
        return
    for i in range(n):
        bg = stripe if (i % 2) else base
        try:
            lb.itemconfig(i, background=bg)
        except Exception:
            break
    if not any(r() is lb for r, _k in _ZEBRA_LISTBOXES):
        _ZEBRA_LISTBOXES.append((weakref.ref(lb), base_key))


_ZEBRA_TAG = '_zebra'
_ZEBRA_TREES = []          # weakref 列表：换主题时按新色板重刷
_ZEBRA_LISTBOXES = []      # ★R302：[(weakref(Listbox), base_key)]，同上
_DEFAULT_COLORS = {}       # 构建期没传 colors 时的兜底（apply_ui_theme 注入）


def set_default_colors(colors):
    """★R222：主题模块把当前色板注入本模块（保持"不 import 主题"的依赖方向）。"""
    global _DEFAULT_COLORS
    _DEFAULT_COLORS = dict(colors or {})


def zebra_treeview(tree, colors=None, rows=None):
    """★R222 阶段 4：给 ttk.Treeview 上隔行底色（只铺奇数行）。

    与 zebra_listbox 同一配方（bg×fg 按 4% 混合，深浅主题都不刺眼）。
    只动 `background`：Treeview 的语义 tag（danger/off/err/running…）清一色只设
    `foreground`，两者不冲突，且条纹 tag 放在 tags 元组**最前**（后面的 tag 优先级
    更高），语义色永远赢。

    colors 省略时用 `set_default_colors()` 注入的当前色板；调用一次即把该树登记进
    注册表，换主题后由 `refresh_zebra_trees()` 按新色重刷。
    依赖：只读 get_children()/item()，空表安全。行序变化（排序）后再调一次即可。
    """
    c = colors or _DEFAULT_COLORS
    base = c.get('bg') or c.get('surface') or '#000000'
    stripe = _blend_hex(base, c.get('fg', '#ffffff'), 0.04)
    try:
        tree.tag_configure(_ZEBRA_TAG, background=stripe)
    except Exception:
        return
    ids = list(tree.get_children()) if rows is None else list(rows)
    for i, iid in enumerate(ids):
        try:
            cur = [t for t in (tree.item(iid, 'tags') or ()) if t != _ZEBRA_TAG]
            new = (_ZEBRA_TAG,) + tuple(cur) if i % 2 else tuple(cur)
            tree.item(iid, tags=new)
        except Exception:
            continue
    if not any(r() is tree for r in _ZEBRA_TREES):
        _ZEBRA_TREES.append(weakref.ref(tree))


def refresh_zebra_trees(colors=None):
    """换主题后重刷所有登记过的 Treeview（逐行 tag 底色不在属性映射表里）。"""
    alive = []
    for r in list(_ZEBRA_TREES):
        t = r()
        if t is None:
            continue
        alive.append(r)
        try:
            zebra_treeview(t, colors)
        except Exception:
            pass
    _ZEBRA_TREES[:] = alive


def refresh_zebra_listboxes(colors=None):
    """★R302：换主题后重刷所有登记过的 tk.Listbox 的逐行底色。

    与 `refresh_zebra_trees` 同一套路：逐行 `itemconfig` 不在换肤的属性映射表里，
    不重刷就会留旧色板（"浅底配浅字"＝看不见）。已销毁的表顺手从注册表摘掉，
    所以这条可以每切换一次主题调一次，不越跑越长。
    """
    alive = []
    for ref, key in list(_ZEBRA_LISTBOXES):
        lb = ref()
        if lb is None:
            continue
        alive.append((ref, key))
        try:
            zebra_listbox(lb, colors, key)
        except Exception:
            pass
    _ZEBRA_LISTBOXES[:] = alive


def form_block(parent):
    """在 pack 流里开一块"网格区"：返回一个已 pack 的子容器，供 form_row 用。

    为什么要这一层：Tkinter 不允许同一个 master 里混用 pack 和 grid。
    group_block 的 body 走 pack（放 form_block / action_bar），grid 只发生在
    form_block 返回的子容器内部 ⇒ 结构上不可能踩到混用坑。
    """
    frame = ttk.Frame(parent)
    frame.pack(fill=tk.X)
    return frame


def action_bar(parent, buttons, cols=None, align='left', equal=True, pad=None,
               spans=None):
    """等宽按钮排（自带一行容器）⇒ 按钮列表。

    buttons: `[(text, command), (text, command, style), ...]`；style 为 None
    时是 ghost（默认 TButton）。
    cols: 每行几个，默认全部一行；多行时上下行**左右边缘对齐**——列宽走
    `uniform` 统一成该列最宽按钮的自然宽度。
    align: 'left' 右侧留弹空列 / 'right' 左侧留 / 'fill' 按钮铺满整宽。
    equal: False 时各列按自身内容取宽（不再等宽）。
    spans: ★R216 阶段 2b——按"12 进制分格"排一行：`spans=(4,4,4)` 与
    `spans=(3,3,3,3)` 两排叠在一起时，3 枚与 4 枚按钮的**竖边照样对齐**
    （管理页顶部"保存/保存全部/恢复"与"全部/隐藏/拖拽/修复缓存"两排的
    老毛病：各排独立 expand 等分 ⇒ 上下行列线错开）。给了 spans 就单行排，
    长度须与 buttons 一致。
    """
    out = []
    n = len(buttons)
    if not n:
        return out
    bar = ttk.Frame(parent)
    bar.pack(fill=tk.X, pady=(SP['xs'], 0))
    cols = max(1, cols or n)
    pad = SP['s'] if pad is None else pad
    if spans:
        assert len(spans) == n, 'spans 与 buttons 数量不一致'
        off = 0
        for c in range(sum(spans)):
            bar.grid_columnconfigure(
                c, weight=1 if align == 'fill' else 0,
                uniform='barunit' if align == 'fill' else None)
        for i, spec in enumerate(buttons):
            style = spec[2] if len(spec) > 2 else None
            b = ttk.Button(bar, text=spec[0], command=spec[1])
            if style:
                b.configure(style=style)
            b.grid(row=0, column=off, columnspan=spans[i], sticky='ew',
                   padx=(0, 0) if i == n - 1 else (0, pad), pady=SP['xs'])
            off += spans[i]
            out.append(b)
        return out
    first = 1 if align == 'right' else 0
    if align == 'right':
        bar.grid_columnconfigure(0, weight=1)
    elif align == 'left':
        bar.grid_columnconfigure(cols, weight=1)
    for i, spec in enumerate(buttons):
        r, c = divmod(i, cols)
        style = spec[2] if len(spec) > 2 else None
        b = ttk.Button(bar, text=spec[0], command=spec[1])
        if style:
            b.configure(style=style)
        bar.grid_columnconfigure(
            c + first, uniform='bar' if equal else None,
            weight=1 if align == 'fill' else 0)
        b.grid(row=r, column=c + first,
               sticky='ew' if (equal or align == 'fill') else 'w',
               padx=(pad, 0) if align == 'right' else (0, pad),
               pady=SP['xs'])
        out.append(b)
    return out


def form_row(parent, row, label, *widgets, fill=False):
    """表单行：标签列右对齐 + 控件列左对齐 ⇒ 多行自然对齐成网格。

    label 传 '' 时留空标签列（复选框行用）。多个 widget 依次进第 1、2、3…
    列，同列各行同宽 ⇒ "显示前 [N 名][__]" 与 "显示第 [M 名][__][~ N 名][__]"
    两行能逐列对齐（用户 2026-09-23 图1 要的就是这个）。
    fill=True 时第 1 控件列吃满剩余宽度。
    """
    if label:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky='e',
                                           padx=(0, SP['s']), pady=SP['xs'])
    for i, w in enumerate(widgets):
        col = i + 1
        stretch = fill and i == 0
        parent.grid_columnconfigure(col, weight=1 if stretch else 0)
        w.grid(row=row, column=col, pady=SP['xs'],
               sticky='ew' if stretch else 'w',
               padx=(0, SP['s']) if i < len(widgets) - 1 else 0)
    return widgets[0] if widgets else None
