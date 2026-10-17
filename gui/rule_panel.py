# -*- coding: utf-8 -*-
"""🎛️ 规则页面板（R145 自 gui/main_window.py 拆出，第四刀·Mixin 方案）。

RuleMixin = 🖍️ 高亮框 + 🌙 桌面亮度全部操作方法 + _build_rules_tab
（规则页 UI：排行榜调用行原顺序保留）+ 高亮框显示层（_hl_t/_hl_c，
main_window 留别名供给留守方法）。由 DesktopManager 继承——self. 语义
零转换，无外部调用方需要委托。
纪律：**严禁 import gui.main_window**（模块双身份）。
"""
import logging

import tkinter as tk
from tkinter import ttk, messagebox

import json

from core.highlighter import (DISPLAY_CAPS, DISPLAY_MODES, FRAME_SHAPES,
                              PRESET_COLORS, THEMES, ZONE_COLOR_MODES,
                              IconHighlighter, zone_caps)
# ★R279-①：并团档的档位表住在渲染侧（core/icon_overlay），面板只读它做下拉值——
# 与"方式能点哪些控件"读 DISPLAY_CAPS 同一个思路：口径只有一个出处，两边不会各写一份。
# ★R281-4：档位从倍率改成"中间空几格"。★R282-3：界面文案只讲"隔几格"、不再报 px
# （px 是写死的 100% 参照，换大/小图标就对不上，用户读成"这数没用"）；所以面板只借
# 档名/档序/"空几格"这几个数（zone_tight_table/level/resolve），像素换算仍只留在渲染侧。
# ★R288：分区「画法」档名同址同源（ZONE_STYLES 也住在渲染侧；★R289 起八档）。
from core.icon_overlay import (ZONE_STYLES, ZONE_STYLE_DEFAULT,
                               ZONE_TIGHTNESS, ZONE_TIGHT_DEFAULT,
                               zone_tight_level, zone_tight_resolve,
                               zone_tight_table)
from utils import i18n
from utils.app_paths import get_app_dir as _get_app_dir
from utils.safeio import atomic_write_json, set_aside_corrupt

APP_DIR = _get_app_dir()
from gui import rank_display
from gui import rule_quickpick
from gui import ui_layout
from gui.ui_layout import SP          # ★R216 阶段2：布局骨架 helper + 间距常量

logger = logging.getLogger("desktop_manager")

# 主题色引用：main_window 启动时 set_colors(COLORS) 注入**同一 dict 对象**
COLORS = {'bg': '#282a36', 'fg': '#f8f8f2', 'primary': '#8be9fd',
          'surface': '#44475a', 'border': '#636d83', 'warning': '#ffb86c',
          'muted': '#6272a4', 'secondary': '#bd93f9',
          # ★R215：兜底表补 text_secondary——本文件底部说明文字用它，此前
          # 只在 set_colors 注入后才存在（与 kids_panel 同款兜底）
          'text_secondary': '#bcc3d6'}


def set_colors(colors: dict):
    global COLORS
    COLORS = colors


def _safe_int(val, default=100, key=''):
    """从可被外部改写的设置里取整数：脏值回退默认并留一行日志。

    ★R264 审计 Z6：`dim_scope_levels` / `desktop_brightness` 都在「导入全部
    数据」的白名单里，导入端对 `app_settings` 是整包 `update()` 不做校验，
    而 `config/*.json` 本身也是官方推荐手改的文件——裸 `int()` 落在
    `_build_rules_tab`（主窗 `__init__` 构建链，无 try）上就是
    ValueError ⇒ 主窗建不起来 ⇒ 程序打不开。
    """
    try:
        return int(val)
    except (TypeError, ValueError):
        logger.warning('设置项 %s 不是整数（%r），已按默认值 %s 运行'
                       % (key or '?', val, default))
        return default


def _hl_num(data, key, default, caster):
    """按字段取一个数：脏值只丢它自己那一个字段，回默认并留一行日志。

    与 `_safe_int` 的分工：那一份收**裸值**、只会转 int，服务 `app_settings`
    那几处；这一份按 key 从 dict 里取、要能同时要小数（width）和整数（pad），
    口径不同所以另写，不复用。

    ★R313 F2：`config/highlight.json` 是官方推荐手改的文件，也在「导入全部
    数据」白名单里。旧写法在同一 try 里裸 `float(data.get('width', 2))` /
    `int(data.get('pad', 1))`——一个脏值抛 ValueError 就把**后面所有字段**
    （分类模式/并团松紧/分区配色/画法/三个勾选）一起拖回默认，且整段
    `except Exception: pass` 零留痕；随后用户任何一次设置操作就按这份默认
    值 _save 覆盖原件 ⇒ 原配置彻底丢失且无从归因。
    """
    val = data.get(key, default)
    try:
        return caster(val)
    except (TypeError, ValueError):
        logger.warning('高亮设置项 %s 不是数字（%r），已按默认值 %s 运行'
                       % (key, val, default))
        return default


_HEX_DIGITS = set('0123456789abcdefABCDEF')


def _hl_color(data, key, default):
    """按字段取一个十六进制颜色：脏值只丢它自己那一个字段，回默认并留一行日志。

    ★R318 K1（★R313 F2 的漏网兄弟）：那一批给 width/pad 配了 `_hl_num`，独独把
    `color` 留在裸取值上。而 `color` 的下游是
    `self.highlighter.set_color_hex(color)`——★R313 之后那一行已经在 try **之外**，
    底层 `_hex_to_rgb` 旧写法第一句就是 `(h or '#00BFFF').lstrip('#')`。于是
    `"color": 123` / `"color": ["#fff"]` 这类**真值但非字符串**（`""`/`null` 因 falsy
    反倒安全）一路 AttributeError 上抛，沿 `_load_highlight_settings()`
    （`gui/main_window.py:299`，主窗构建链、无 try）打断整条建窗链 ⇒ 程序打不开。
    这份档既是官方推荐手改、又在「导入全部数据」白名单里零校验，所以这是**必修**。
    """
    val = data.get(key, default)
    if isinstance(val, str):
        body = val.strip().lstrip('#')
        if len(body) == 6 and all(c in _HEX_DIGITS for c in body):
            return val
    logger.warning('高亮设置项 %s 不是十六进制颜色（%r），已按默认值 %s 运行'
                   % (key, val, default))
    return default


# _hl_t：中文 canonical → 当前语言显示；_hl_c：显示 → 中文 canonical。
_HL_EN = {
    # 模式（主题）
    '经典蓝': 'Classic Blue', '暗黑': 'Dark', '护眼': 'Eye Care', '少女': 'Sweet',
    '卡通': 'Cartoon', '霓虹绿': 'Neon Green', '荧光黄': 'Fluorescent Yellow',
    '警示红': 'Alert Red', '清新紫': 'Fresh Purple', '樱花粉': 'Sakura Pink',
    '低调虚线': 'Subtle Dashed', '加粗橙': 'Bold Orange', '自定义': 'Custom',
    # 形状
    '圆角立体': 'Rounded 3D', '圆角': 'Rounded', '直角': 'Square', '双线': 'Double',
    '下划线': 'Underline', 'U形': 'U-shape', '四角': 'Corners',
    # 样式
    '实线': 'Solid', '虚线': 'Dashed',
    # 方式
    '方框': 'Box', '色块垫底': 'Color Fill', '文字高亮': 'Text Highlight',
    '聚光灯': 'Spotlight',
    # ★R273 B5 新四档（旧四档逐字不动）
    '分区': 'Zone', '色块纱环': 'Tint Ring', '荧光笔': 'Marker', '角贴': 'Badge',
    # ★R281-4（用户 08 批 item4「直接按照图标隔开来计数好理解点」）：档名从形容词
    # 换成"中间空几格"——数得出来的才叫大白话。旧四行（紧/中/松/很松）拔掉：
    # 读旧存档时 `zone_tight_resolve` 先把旧码值折进新档，界面显示的永远是当前档名，
    # 留旧译名只会让 _hl_c 这张反查表多认四个没人再显示的词。
    '挨着': 'Touching', '隔1格': '1 icon apart',
    '隔2格': '2 icons apart', '隔3格': '3 icons apart',
    # ★R282-4：分区配色两档（'统一色' 时颜色/主题联动分区）
    '跟随分类色': 'By category', '统一色': 'Single color',
    # ★R288：分区「画法」默认档名（另外三档沿用上面"方式"那三个词）
    '包络框': 'Envelope',
    # ★R289（批45）：画法再扩的四档创新样式（English 档不许冒出中文档名，闸 K11e/K11f）
    '取景角': 'Crop Marks', '双线框': 'Double Frame',
    '斜纹带': 'Hatched Band', '柔光晕': 'Soft Glow',
    # ★R293-②（批49）：两档"细"预设。英文档名照老档加 Thin 前缀（闸 K11e 数键集，
    # 漏一个 English 界面就冒中文档名）
    '细双线框': 'Thin Double Frame', '细柔光晕': 'Thin Soft Glow',
    # 颜色名（拼进 '名字 #HEX' 标签）
    '蓝色': 'Blue', '绿色': 'Green', '红色': 'Red', '黄色': 'Yellow',
    '橙色': 'Orange', '紫色': 'Purple', '粉色': 'Pink', '青色': 'Cyan',
    '白色': 'White',
}
_HL_EN_INV = {v: k for k, v in _HL_EN.items()}

# ★R278-D：样式下拉 ↔ core.highlighter.DISPLAY_CAPS 的表键名对照。
# 放模块级（不是 RuleMixin 的类属性）是为了让门禁/桩具能直接 import 复用同一份，
# 免得"哪些控件归表管"这件事在两个文件里各写一遍。
# ★R279-②（产品负责人 09-30 04 批 item2「摘掉框大小」）：'pad' 这一行整条拔掉——
# 渲染侧没有任何一档读 pad（★R278-D 实测零读者），它恒灰＝一个永远点不动的下拉，
# 留着就是用户说的"设计了个没用的东西"。控件拔掉了，表里也就不再挂它。
CAP_WIDGETS = (('shape', 'hl_shape_combo'),
               ('width', 'hl_width_combo'),
               ('style', 'hl_style_combo'),
               # ★R279-①：新档位控件照同一张表管——灰/不灰由渲染侧说了算，
               # 且③那套"为什么灰"的悬停说明自动跟着覆盖到它
               ('merge', 'hl_merge_combo'),
               # ★R282-4：分区配色——只有"方式=分区"读它（highlighter._style_args），
               # 其余七档恒灰；照同一张表管，"为什么灰"的悬停说明自动跟上
               ('zone_color', 'hl_zonecolor_combo'),
               # ★R288：分区「画法」——同样只有"方式=分区"读它；它反过来还决定
               # 形状/线宽/样式三味在分区里能不能点（见 zone_caps）
               ('paint', 'hl_zpaint_combo'))


def _hl_t(zh: str) -> str:
    """高亮下拉显示文案。颜色项形如 '蓝色 #00BFFF'，'自定义 #FF8800'。"""
    if not zh or not i18n.is_en():
        return zh
    if zh == '自定义...':
        return 'Custom...'
    if zh.startswith('自定义 #'):
        return 'Custom #' + zh[len('自定义 #'):]
    if ' #' in zh:      # 颜色 '蓝色 #00BFFF'
        name, hexv = zh.rsplit(' #', 1)
        return f'{_HL_EN.get(name, name)} #{hexv}'
    return _HL_EN.get(zh, zh)


def _hl_c(disp: str) -> str:
    """显示文案 → 中文 canonical（写回 highlighter / highlight.json 用）"""
    if not disp or not i18n.is_en():
        return disp
    if disp == 'Custom...':
        return '自定义...'
    if disp.startswith('Custom #'):
        return '自定义 #' + disp[len('Custom #'):]
    if ' #' in disp:
        name, hexv = disp.rsplit(' #', 1)
        return f'{_HL_EN_INV.get(name, name)} #{hexv}'
    return _HL_EN_INV.get(disp, disp)


# ★R279-③：置灰项的悬停说明（产品负责人 09-30 04 批 item3「可以」点头批上 N-8）。
# 原来只把用不到的下拉"灰掉"——那只说了"不能点"，没说"为什么不能点"，
# 用户仍然要猜（★R278-D 那条原话："选了没反应，以为是程序没设计好"）。
_CAP_LABELS = {'shape': ('形状', 'Shape'), 'width': ('线宽', 'Width'),
               'style': ('样式', 'Style'),
               # ★R279-①：新控件也要有"为什么灰"可说（③的门禁 C4 逐键核对）
               'merge': ('并团（隔几格算一伙）', 'Clustering (icons apart)'),
               # ★R282-4：分区配色也只有"方式=分区"用得到
               'zone_color': ('分区配色', 'Zone color'),
               # ★R288：画法同样只有"方式=分区"用得到
               'paint': ('画法', 'Zone paint')}


# ★R288：画法日志里那句人话（每档一撇，别让用户猜"角贴跟角贴档有啥区别"）
# ★R289：新四档补四撇（闸 K11d 钉"非空/单行/以；收尾"，K11c 钉键集==档表）
# ★R293：再补两撇"细"档；包络框那句改口——★R293-③ 起 形状/线宽/样式 不再是
# "只管这一档"，其余各档也按 ZONE_PAINT_CAPS 认领了（写死"就管这一档"会变成
# 一句当面骗人的话：用户选了柔光晕、把线宽拉到 6，画面真的会变）。
PAINT_HINTS = {'包络框': '一圈线框住整团，粗细方圆全跟 形状/线宽/样式 走；',
               '色块纱环': '整团套一圈磨砂色环，中间透出桌面；',
               '荧光笔': '每行图标底下来一条色带，团里几行就几条；',
               '角贴': '每个图标角上一枚点，团右上角再贴一枚名牌；',
               '取景角': '只在团四角各钉一枚实心括号，中间全透出桌面；',
               '双线框': '两圈同色线、中间留一条透明缝，比单线更钉；',
               '斜纹带': '每行图标底下来一条 45° 斜纹带，比荧光笔压得住；',
               '柔光晕': '沿团边向内三圈渐淡的光晕，中心透出桌面；',
               '细双线框': '还是两圈夹一条透明缝，但缝更窄、线更细一档；',
               '细柔光晕': '还是三圈渐淡光晕，但带更窄、颜色更淡一档；'}


def cap_tip_text(cap, mode_zh, paint_zh=None):
    """灰下拉的说明文案。中文变量只插中文模板、英文只插英文模板
    （★R230b：`tr(zh % x, en % x)` 会把中文尾巴带进 English 档）。

    ★R288：方式=分区时"为什么灰"多半不是方式的锅，是**画法**的锅（选了角贴，
    形状/线宽/样式自然没用）。这时把画法一起点名，讲的才是真原因。"""
    zh, en = _CAP_LABELS.get(cap, (cap, cap))
    if paint_zh:
        mode_zh = i18n.tr('%s·画法=%s' % (mode_zh, paint_zh),
                          '%s / paint=%s' % (_HL_EN.get(mode_zh, mode_zh),
                                             _HL_EN.get(paint_zh, paint_zh)))
    return i18n.tr(
        '「方式=%s」用不到「%s」：这一档它不改变画面' % (mode_zh, zh),
        '"%s" is not used by display mode "%s" — changing it does nothing here'
        % (en, _HL_EN.get(mode_zh, mode_zh)))


def zone_paint_of(owner) -> str:
    """读面板对象当前「画法」的中文 canonical（★R288）。

    控件还没建、或值认不得 ⇒ 一律回默认档 包络框：等价于修前那份联动表，
    既不会把下拉打死，也不会因为"多了一味"让既有桩具/构建早期炸在 _sync_mode_caps 里。
    做成模块级函数（不是 RuleMixin 方法）就是为了让 `_sync_mode_caps` 只依赖
    `self.hl_zpaint_var` 这一枚属性——那些不继承 RuleMixin 的门禁桩具才不至于
    因 `self._zone_paint_code()` 不存在而整段静默不刷（★R231 桩具红 ≠ 缺陷红）。"""
    var = getattr(owner, 'hl_zpaint_var', None)
    try:
        want = _hl_c(var.get()) if var is not None else None
    except Exception:
        want = None
    return want if want in ZONE_STYLES else ZONE_STYLE_DEFAULT


def attach_disable_tip(widget, text_fn):
    """给一枚按「方式」置灰的控件挂"为什么灰"的跟手小窗（★R279-③）。

    口径照 gui/main_window.py 的 `_attach_hover_tip`（★R262）：无边框 + topmost
    （overrideredirect 窗不抬顶会被主窗压在下面看不见）、离开/按下/销毁即收。
    本模块严禁 import main_window（模块双身份），所以这里自带一份。
    文案**悬停那一刻现算** ⇒ 切了「方式」说明跟着变；`state!='disabled'` 直接返回
    ⇒ 能用的控件一次都不弹，不打扰。"""
    tip = {}

    def _hide(_ev=None):
        w = tip.pop('w', None)
        if w is not None:
            try:
                w.destroy()
            except Exception:
                pass

    def _show(ev):
        _hide()
        try:
            if str(widget.cget('state')) != 'disabled':
                return
        except Exception:
            return
        try:
            txt = text_fn()
        except Exception as e:
            logger.warning(f'[规则] 置灰说明文案取不到: {e}')
            return
        if not txt:
            return
        try:
            w = tk.Toplevel(widget)
            w.wm_overrideredirect(True)
            w.attributes('-topmost', True)
            w.wm_geometry('+%d+%d' % (ev.x_root + 14, ev.y_root + 10))
            tk.Label(w, text=txt, bg=COLORS['surface'], fg=COLORS['fg'],
                     font=ui_layout.app_font(9), padx=8, pady=4,
                     wraplength=420, justify=tk.LEFT,
                     highlightbackground=COLORS['border'],
                     highlightthickness=1).pack()
            tip['w'] = w
        except Exception as e:
            logger.warning(f'[规则] 置灰说明弹不出来: {e}')

    widget.bind('<Enter>', _show, add='+')
    widget.bind('<Leave>', _hide, add='+')
    widget.bind('<Button-1>', _hide, add='+')
    widget.bind('<Destroy>', _hide, add='+')


# ★R280-B（用户 06 批 item3「界面无量化提示」）：③ 那套说明只管"为什么灰"，
# 能点的时候用户看到的还是一句形容词。★R281-4（用户 08 批 item4「太过于专业……
# 直接按照图标隔开来计数好理解点」）先把口径翻成"这档中间允许空几格"。
# ★R282-3（用户 10-01 01 批 R011-1）再砍掉像素：px 是写死的 100% 参照，换大/小图标
# 就对不上，用户读成"这数没用"；文案只讲"隔几格"（一格＝一个图标占的位置，与像素无关，
# 大小图标都成立），档名/档序/"空几格"借渲染侧 zone_tight_table/level/resolve，
# 像素换算仍只留在渲染侧、界面不重写公式。
def merge_hint_text(tight_zh):
    # ★R282-3（用户 10-01 01 批 R011-1）：旧文案把"相距多少px"摊了一整屏，用户原话
    # "px 这个数字对用户没用"，且 px 是写死的 100% 参照（ZONE_REF_CELL），换大/小图标
    # 就不准。改成只讲"隔几格"——一格＝一个图标占的位置，与图标像素大小无关，
    # 大图标小图标都成立（渲染侧本来就读每格真实 w/h，提示不必再报像素）。
    key = zone_tight_resolve(tight_zh)          # 旧档名先进来折叠，说明里说的就是生效档
    n = zone_tight_level(key)
    levels_zh = '／'.join(nm for nm, _k, _px in zone_tight_table())
    levels_en = ' / '.join(_HL_EN.get(nm, nm) for nm, _k, _px in zone_tight_table())
    how = (f'中间只要空出 1 个图标的位置就分成两伙' if n <= 0 else
           f'中间空 {n} 个图标的位置还算一伙，空 {n + 1} 个就分成两伙')
    en_how = ('icons merge only when nothing sits between them'
              if n <= 0 else
              f'up to {n} empty icon cell(s) may sit between two icons and '
              f'still merge')
    return i18n.tr(
        f'「并团」= 挨在一起的图标算一伙，一伙画一个分区框。\n'
        f'当前档「{key}」：{how}。\n'
        f'四档：{levels_zh}——档名里的数字＝中间最多空几个图标的位置。\n'
        f'（"格"＝一个图标占的位置，大图标小图标都按格算，不用管像素。）\n'
        '图标被切成一堆小框就往"隔1格/隔2格"挪，团得太散就往"挨着"挪；'
        '最松也只到空 3 格，不会圈出一个盖住半屏的大框。',
        f'Clustering = icons that sit together count as one group and get one '
        f'zone box.\nCurrent level "{_HL_EN.get(key, key)}": {en_how}.\n'
        f'Four levels: {levels_en} — the number in a level is how many empty '
        f'icon cells may sit between two icons.\n'
        f'(A "cell" is the space one icon occupies, so this holds for any icon '
        f'size — no pixels involved.)\n'
        'Too many small boxes? Move toward "1/2 apart". Groups too loose? Move '
        'toward "Touching". Even the widest level stops at 3 empty cells, so it '
        'cannot swallow half the screen.')


def attach_info_tip(widget, text_fn):
    """给一枚**一直能用**的控件挂量化说明（★R280-B）。

    与 `attach_disable_tip` 互为补集：那一枚只在 disabled 时弹（讲"为什么灰"），
    这一枚只在能用时弹（讲"这一档中间空几格"）⇒ 同一控件两套文案，任何时候只弹一张，
    不打架也不重复。窗口口径照 ★R262/★R279-③ 那份（无边框 + topmost，
    离开/按下/销毁即收），本模块不 import main_window。"""
    tip = {}

    def _hide(_ev=None):
        w = tip.pop('w', None)
        if w is not None:
            try:
                w.destroy()
            except Exception:
                pass

    def _show(ev):
        _hide()
        try:
            if str(widget.cget('state')) == 'disabled':
                return          # 灰着的时候由③那张"为什么灰"来说话
        except Exception:
            return
        try:
            txt = text_fn()
        except Exception as e:
            logger.warning(f'[规则] 量化说明文案取不到: {e}')
            return
        if not txt:
            return
        try:
            w = tk.Toplevel(widget)
            w.wm_overrideredirect(True)
            w.attributes('-topmost', True)
            w.wm_geometry('+%d+%d' % (ev.x_root + 14, ev.y_root + 10))
            tk.Label(w, text=txt, bg=COLORS['surface'], fg=COLORS['fg'],
                     font=ui_layout.app_font(9), padx=8, pady=4,
                     wraplength=420, justify=tk.LEFT,
                     highlightbackground=COLORS['border'],
                     highlightthickness=1).pack()
            tip['w'] = w
        except Exception as e:
            logger.warning(f'[规则] 量化说明弹不出来: {e}')

    widget.bind('<Enter>', _show, add='+')
    widget.bind('<Leave>', _hide, add='+')
    widget.bind('<Button-1>', _hide, add='+')
    widget.bind('<Destroy>', _hide, add='+')


class RuleMixin:
    def _dim_scope_refresh(self):
        """★ R65-Bug6 / R69：刷新"作用显示器"下拉（每次展开时重枚举）。

        纯用户态枚举（EnumDisplayMonitors+GetMonitorInfo），无 I2C 事务。"""
        try:
            from core.desktop_dim import list_monitors
            vals = ['全部显示器'] + (list_monitors() or [])
        except Exception:
            vals = ['全部显示器']
        self._dim_scope_labels = vals
        self.dim_scope_combo.configure(values=vals)
        _saved = self._app_settings.get('desktop_hw_scope', None)
        if (not isinstance(_saved, int) or _saved < 0
                or 1 + _saved >= len(vals)):
            self.dim_scope_var.set('全部显示器')
        else:
            self.dim_scope_var.set(vals[1 + _saved])

    def _on_dim_change(self, value=None):
        """桌面亮度滑条：软件蒙版实时应用（拖动中安全无阻塞）

        ★ R98（用户定案）：单屏模式下只动这一台（其他屏蒙版原样）；
        "全部显示器"模式下拖动 = 统一调节，数字同时记到每台显示器名下。"""
        try:
            v = int(self.dim_var.get())
            self.dimmer.set_level(v)
            self._app_settings['desktop_brightness'] = v   # ★ P1-3 写内存，松手时落盘
            # ★ R74：记入当前作用显示器的专属记忆（松手时随设置落盘）
            self._dim_levels[self._dim_scope_key()] = v
            if getattr(self.dimmer, 'scope', None) is None:
                # ★ R98：全部模式调节数字 → 每台显示器都按这个数记忆
                try:
                    from core.desktop_dim import list_monitors
                    for _i in range(len(list_monitors() or [])):
                        self._dim_levels[str(_i)] = v
                except Exception:
                    pass
            self._app_settings['dim_scope_levels'] = self._dim_levels
        except Exception as e:
            logger.error(f"调节桌面亮度失败: {e}")

    def _on_dim_scope_change(self):
        """★ R65-Bug6 / R69：蒙版作用显示器（None=全部 / 序号=只盖该台）。

        ★ R98（2026-09-16 用户定案）：每台显示器各记各的亮度——
        · 选具体显示器：滑条显示它自己记住的值（无记忆退"全部"记忆，再无=100），
          并按该值只重铺这一台的蒙版，其他屏原样保留；
        · 切"全部显示器"：什么都不调节（各屏维持现状），滑条显示"全部"记忆值；
          拖动滑条才统一调节全部显示器（数字同时记入每台）。"""
        v = self.dim_scope_var.get()
        vals = getattr(self, '_dim_scope_labels', ['全部显示器'])
        # ★R204 GUIB-7：显示器热插拔后旧选项可能不在刷新后的 labels 里，
        # 裸 index() 会 ValueError 未捕获；index 失败回退"全部显示器"
        try:
            idx = None if v == '全部显示器' else max(0, vals.index(v) - 1)
        except ValueError:
            idx = None
            # ★R207 NEW3-B11：旧屏名已失效，下拉同步回"全部显示器"
            try:
                self.dim_scope_var.set(vals[0])
            except Exception:
                pass
        self._app_settings['desktop_hw_scope'] = idx
        self.dimmer.scope = idx
        # ★R264 Z6：本段原有的局部 `def _safe_int` 已提到模块级（与本文件
        # :833 那处构建期取值共用一份口径），这里直接复用
        if idx is None:
            # ★ R98：切"全部"不调节任何一台，各屏蒙版维持现状
            _lv = _safe_int(self._dim_levels.get('all', self.dim_var.get()))
            self.dim_var.set(_lv)
            self._app_settings['dim_scope_levels'] = self._dim_levels
            self._save_app_settings()
            self._log(i18n.tr(
                f'🌗 亮度作用: {v}（各屏维持各自现状，拖动滑条统一调节）',
                f'🌗 Dim scope: {v} (each monitor keeps its own; drag to set all)'))
            return
        # 选具体显示器：显示它自己的记忆值，只动这一台
        _lv = _safe_int(self._dim_levels.get(str(idx),
                                             self._dim_levels.get('all', 100)))
        self.dim_var.set(_lv)
        self._app_settings['desktop_brightness'] = _lv
        self._app_settings['dim_scope_levels'] = self._dim_levels
        self._save_app_settings()
        self._log('🌗 软件蒙版作用: ' + v + i18n.tr('', f' (level {_lv})'))
        # 立即按这一台自己的记忆重铺/收起它的蒙版（其他屏不动）
        try:
            self.dimmer.set_level(_lv)
        except Exception as e:
            logger.error(f"重铺蒙版失败: {e}")

    def _on_hl_align_toggle(self):
        """复选项：同列框底边对齐"""
        v = bool(self.hl_align_var.get())
        self.highlighter.set_bottom_align(v)
        self._save_highlight_settings()
        self._log('📐 已开启同列底边对齐' if v else '📐 已关闭底边对齐')

    def _on_hl_cat_only_toggle(self):
        """★ B12/B13 复选项：只框分类（不含常驻）。与"框住分类和常驻"互斥。"""
        v = bool(self.hl_cat_only_var.get())
        if v and self.hl_frame_all_var.get():
            # 互斥：开"只框分类"时自动取消"框住分类和常驻"
            self.hl_frame_all_var.set(False)
            self.highlighter.set_frame_all(False)
            self._log('🔲 "框住分类和常驻"已自动取消（与只框分类互斥）')
        self.highlighter.cat_only = v
        self._save_highlight_settings()
        self._log('🎯 已开启：切分类只框该分类的图标（常驻可见但不框）' if v
                  else '已关闭：切分类框 分类+常驻（跟"公共分类显示"矩阵）')
        # 立即生效：重放当前视图（★ B15：[全部] 也重放，名单按新口径计算）
        try:
            self.dock.recompute_highlight_only()   # ★ R65-Bug5：只重算框选
        except Exception as e:
            logger.error(f"重算高亮失败: {e}")

    def _on_watch_window_change(self):
        """★R272（批25，用户 2026-09-29 01 批 item1「我觉得三分钟是可以修改的」）。

        先落盘再应用（★R226 顺序纪律）：先用纯函数 `clamp_watch_window` 算出
        "真正会生效"的终值 → 写 `_app_settings` → `_save_app_settings()` →
        再推给运行时 → 最后回写下拉框。回写这一步不能省：用户选的值若被夹取，
        界面必须显示夹取后的那档，否则等于"界面骗人"。
        """
        try:
            sec = float(self.watch_window_var.get()) * 60.0
        except (TypeError, ValueError):
            sec = None
        eff, _why = IconHighlighter.clamp_watch_window(sec)
        self._app_settings['icon_watch_window_sec'] = eff
        self._save_app_settings()
        self.highlighter.set_watch_window(eff)
        self.watch_window_var.set('%g' % (eff / 60.0))
        self._log('⏱️ 图标监视时长 = %g 分钟（即时生效）' % (eff / 60.0))

    def _on_hl_category(self):
        """高亮选中分类的全部图标。

        ★ R67（用户定案）：只跟"📍 当前分类"下拉绑定——不再关联管理页
        列表选中或悬浮窗激活槽位（R66 的回退逻辑已按用户要求取消）。"""
        cat_name = (self.hl_quickpick_var.get() or '').strip()
        if not cat_name:
            messagebox.showwarning(
                '提示', '请先在上方"📍 当前分类"下拉选择一个分类',
                parent=self.root)
            return
        rule_quickpick.highlight_category_by_name(self, cat_name)

    def _on_hl_clear(self):
        """清除全部高亮框

        ★ B11：只清 _names 时，"🔲 框住分类和常驻"（默认开）会在 _refresh 的
        frame_all 分支立刻把全部框重画出来——表现为"点清除反而生成方框"
        （用户实测 2026-09-12）。清除语义 = 所有框都消失，故同步取消
        "框住分类和常驻"（复选框跟着取消勾选并落盘，勾回即恢复全框）。"""
        self.highlighter.clear()
        if self.hl_frame_all_var.get():
            self.hl_frame_all_var.set(False)
            self.highlighter.set_frame_all(False)
            self._save_highlight_settings()
        self._log('🚫 已清除所有高亮框（"框住分类和常驻"已一并取消，'
                  '勾回即恢复全框）')

    def _on_hl_color_selected(self, event=None):
        """颜色下拉：选"自定义..."弹出 Windows 原生选色对话框（可点色板）"""
        if _hl_c(self.hl_color_var.get()) == '自定义...':
            from tkinter import colorchooser
            rgb, hexstr = colorchooser.askcolor(
                color='#00BFFF',
                title=i18n.tr('选择方框颜色', 'Choose frame color'), parent=self.root)
            if not hexstr:      # 用户取消
                self.hl_color_var.set(_hl_t('蓝色 #00BFFF'))
                return
            hexstr = hexstr.replace('#', '').upper()
            self.hl_color_var.set(_hl_t(f'自定义 #{hexstr}'))
        self._on_hl_style_change()

    def _on_hl_display_mode_change(self):
        """显示方式变化 → 应用并保存（方框/色块垫底/文字高亮/聚光灯）

        ★ 语义（2026-09-12 用户拍板）：方式切换**沿用当前高亮名单**——
        悬浮条切分类的名单 = 分类+常驻+系统（跟"公共分类显示"矩阵）；
        "高亮选中分类"的名单 = 单分类（核对归类用的验证工具）。
        两种意图都原样保留，此处不改写名单。"""
        self.highlighter.set_display_mode(
            DISPLAY_MODES.get(_hl_c(self.hl_dmode_var.get()), 'border'))
        self._sync_mode_caps()
        self._save_highlight_settings()
        self._log(f'🖍️ 高亮方式: {self.hl_dmode_var.get()}')

    def _on_hl_merge_change(self):
        """★R279-①：并团松紧档位变化。

        顺序照 ★R226：**先落盘再应用**（进程内 `_refresh` 万一炸了，用户选的
        那一档也已经在盘上，重启还在）；`set_zone_tightness` 认档才动，
        所以下面回显的是"真正生效"的那一档，界面不骗人。"""
        want = _hl_c(self.hl_merge_var.get())
        self._save_highlight_settings()
        self.highlighter.set_zone_tightness(want)
        eff = self.highlighter.zone_tightness
        if eff != want:
            self.hl_merge_var.set(_hl_t(eff))
        # ★R282-3（用户 10-01 01 批 R011-1）：回显也去掉 px，只说"空几格"——
        # 一格＝一个图标的位置，与图标像素大小无关（px 是写死的 100% 参照，换图标大小就不准）。
        self._log(f'🧩 并团 = {self.hl_merge_var.get()}'
                  f'（中间空 {zone_tight_level(eff)} 格以内还算一伙，'
                  f'空 {zone_tight_level(eff) + 1} 格就分成两伙。'
                  f'只有"方式=分区"用得到，即时生效）')

    def _on_hl_zonecolor_change(self):
        """★R282-4：分区配色档位变化。顺序照 ★R226：**先落盘再应用**；
        `set_zone_color_mode` 认档才动，回显的是"真正生效"那一档，界面不骗人。

        '统一色' ⇒ 每个分区都改用上面"颜色/模式(主题)"选的那一个色（于是这两个
        下拉对分区也联动）；'跟随分类色' ⇒ 一团一色（各分区用自己分类的颜色，
        旧行为）。只有"方式=分区"用得到，即时生效。"""
        want = _hl_c(self.hl_zonecolor_var.get())
        self._save_highlight_settings()
        self.highlighter.set_zone_color_mode(want)
        eff = self.highlighter.zone_color_mode
        if eff != want:
            self.hl_zonecolor_var.set(_hl_t(eff))
        if eff == '统一色':
            self._log('🎨 分区配色 = 统一色（每个分区都用上面"颜色/模式"选的那一个色；'
                      '只有"方式=分区"用得到，即时生效）')
        else:
            self._log('🎨 分区配色 = 跟随分类色（一团一色，各分区用自己分类的颜色；'
                      '只有"方式=分区"用得到，即时生效）')

    def _on_hl_zpaint_change(self):
        """★R288：分区「画法」档位变化。顺序照 ★R226：**先落盘再应用**；
        `set_zone_style` 认档才动 ⇒ 回显的是"真正生效"那一档，界面不骗人。

        包络框＝修前唯一的画法（老用户升上来画面不动）；色块纱环/荧光笔/角贴
        是把"线条与显示"里那三种样式搬进分区。换画法还要立刻重刷置灰——
        ★R293-③ 起置灰按 `ZONE_PAINT_CAPS` **逐档**算：认领了哪味就亮哪味，
        没认领的（例如抖动填充那几档的"样式"——没有笔，虚线无从谈起）才灰。"""
        want = _hl_c(self.hl_zpaint_var.get())
        self._save_highlight_settings()
        self.highlighter.set_zone_style(want)
        eff = self.highlighter.zone_style
        if eff != want:
            self.hl_zpaint_var.set(_hl_t(eff))
        self._sync_mode_caps()
        self._log(f'🖌 分区画法 = {self.hl_zpaint_var.get()}'
                  f'（{PAINT_HINTS.get(eff, "")}只有"方式=分区"用得到，即时生效）')

    def _on_hl_frame_all_toggle(self):
        """复选项：框住分类和常驻 开/关（★ B13：与"只框分类"互斥；
        ★ B15 起 [全部] 视图按矩阵口径计算名单，系统图标默认不框）"""
        v = bool(self.hl_frame_all_var.get())
        if v and self.hl_cat_only_var.get():
            # 互斥：开"框住分类和常驻"时自动取消"只框分类"
            self.hl_cat_only_var.set(False)
            self.highlighter.cat_only = False
            self._log('🎯 "只框分类"已自动取消（与框住分类和常驻互斥）')
        self.highlighter.set_frame_all(v)
        self._save_highlight_settings()
        self._log('🔲 已框住分类和常驻（主题可在左侧切换）' if v
                  else '⬜ 已取消框住分类和常驻')
        # ★ B15：立即重放当前视图（分类或 [全部]），新口径马上可见
        try:
            self.dock.recompute_highlight_only()   # ★ R65-Bug5：只重算框选
        except Exception as e:
            logger.error(f"重算高亮失败: {e}")

    def _on_hl_frame_sys_toggle(self):
        """★ B15 复选项：框住系统图标（勾选后系统虚拟图标进高亮名单）"""
        v = bool(self.hl_frame_sys_var.get())
        self.highlighter.frame_sys = v
        self._save_highlight_settings()
        self._log('🖥️ 已开启：系统图标也画框' if v
                  else '🖥️ 已关闭：系统图标不画框')
        try:
            self.dock.recompute_highlight_only()   # ★ R65-Bug5：只重算框选
        except Exception as e:
            logger.error(f"重算高亮失败: {e}")

    def _on_hl_gradient_toggle(self):
        """复选项：渐变色模式 开/关"""
        v = bool(self.hl_gradient_var.get())
        self.highlighter.set_gradient_mode(v)
        self.highlighter.notify_changed()   # ★ R79：立即重绘（旧版等下一轮巡检才变色）
        self._save_highlight_settings()
        self._log('🎨 渐变色已开启（高亮时各图标用分类色）' if v
                  else '🎨 渐变色已关闭（统一单色）')
    # （★ B15）"📌 方框常显"已删除：其依赖的 pin/pinned_until 机制自旧
    # overlay 退役后零读者（框本来就常显），属死功能，连同 4 处 frames_always
    # 读取点一并清理。

    def _on_hl_rebuild(self):
        """手动重建方框：立即重建数据并当场重画

        ★ B11：旧实现 collect_now 只重建数据缓存、缺立即重绘——框已在屏上时
        看不出变化，框丢失时点了"没反应"要干等监视线程下一轮（2s）。
        （★ B15：原 pin(30s) 为死调用，已随 pin 机制一并清理）

        ★ R67（用户定案）：与"📍 当前分类"下拉绑定——有选中分类时重建
        该分类的方框；未选时维持旧语义（仅按当前名单重绘）。"""
        cat_name = ((self.hl_quickpick_var.get() or '').strip()
                    if hasattr(self, 'hl_quickpick_var') else '')
        if cat_name:
            rule_quickpick.highlight_category_by_name(self, cat_name)
            return
        self.highlighter.notify_changed()
        changed = self.highlighter.collect_now()
        # ★ B11：按新缓存立即重绘（原来缺这一步）
        self.highlighter.force_redraw()
        if changed:
            self._log('🔄 已重新生成方框（30 秒内保持显示）')
        else:
            self._log('⚠ 当前没有可绘制的内容：请勾选"🔲 框住分类和常驻"，'
                      '或先切分类 / 点"🖍️ 高亮选中分类"')

    def _on_hl_shape_change(self):
        """形状变化 → 应用并保存"""
        self.highlighter.set_shape(_hl_c(self.hl_shape_var.get()))
        try:
            self.hl_theme_var.set(_hl_t('自定义'))
        except Exception:
            pass
        self._save_highlight_settings()

    # ★ R69：硬件 DDC 已移除，模式只有"软件蒙版/无蒙版"两种
    _DIM_MODE_TEXT = {
        'sw': '软件蒙版',
        'none': '',
    }

    def _on_hl_style_change(self):
        """颜色/线宽/样式/框大小变化 → 应用到高亮器"""
        self.highlighter.set_color_hex(self._hl_parse_color())
        self.highlighter.set_width(self.hl_width_var.get())
        self.highlighter.set_dashed(_hl_c(self.hl_style_var.get()) == '虚线')
        self.highlighter.set_pad(self.hl_pad_var.get())
        try:
            self.hl_theme_var.set(_hl_t('自定义'))
        except Exception:
            pass
        self._save_highlight_settings()

    def _on_hl_sys_only_toggle(self):
        """★ B16 复选项：只框系统图标（与 框住分类和常驻/只框分类/含系统图标 互斥，
        选中时三者置灰不可选，取消后恢复）"""
        v = bool(self.hl_sys_only_var.get())
        self.highlighter.sys_only = v
        self._sync_frame_scope_states()
        self._save_highlight_settings()
        self._log('🖥️ 已开启：只框系统图标（其他框选开关已置灰）' if v
                  else '已关闭：恢复 框住分类和常驻/只框分类/含系统图标 可选')
        try:
            self.dock.recompute_highlight_only()   # ★ R65-Bug5：只重算框选
        except Exception as e:
            logger.error(f"重算高亮失败: {e}")

    def _on_hl_theme_change(self):
        """一键切换高亮主题"""
        theme = self.highlighter.apply_theme(_hl_c(self.hl_theme_var.get()))
        if not theme:
            return
        # 同步颜色/线宽/样式/框大小显示
        for name, value in PRESET_COLORS.items():
            if value.lower() == theme['color'].lower():
                self.hl_color_var.set(_hl_t(f'{name} {value}'))
                break
        self.hl_width_var.set(str(theme['width']))
        self.hl_style_var.set(_hl_t('虚线' if theme['dashed'] else '实线'))
        self.hl_pad_var.set(str(theme['pad']))
        self.hl_shape_var.set(_hl_t(theme.get('shape', '圆角立体')))
        self._save_highlight_settings()
        self._log(f'🎨 已切换主题 [{self.hl_theme_var.get()}]')

    def _sync_frame_scope_states(self):
        """★ B16："只框系统图标"选中时，其余三个框选开关置灰（逻辑互斥）；
        取消后恢复可选。供构建/加载/切换三处调用。"""
        dis = tk.DISABLED if self.hl_sys_only_var.get() else tk.NORMAL
        for cb in (self.hl_frame_all_cb, self.hl_cat_only_cb, self.hl_frame_sys_cb):
            try:
                cb.configure(state=dis)
            except Exception:
                pass

    # ★R278-D：下拉"能不能点"改由渲染侧那张表推导（见 CAP_WIDGETS）
    def _sync_mode_caps(self):
        """按当前"方式"启用/置灰它真消费的样式下拉（★R278-D）。

        旧版只管"形状"一格，其余用不到的下拉照样能点、点了桌面无变化 ⇒
        用户读成"程序没设计好"（原话）。现在：
          · 名单来自 core.highlighter.DISPLAY_CAPS（逐档对着 icon_overlay
            的实时绘制分支量过，不是这里猜的）；
          · 颜色/渐变色/模式每档都读 ⇒ 不在表里出现，也就永远不会被这段置灰；
          · 框大小 pad 没有任何一档写它（实时渲染零读者）⇒ 恒灰，
            等哪天真接上再把 'pad' 加进对应档位。
        置灰只动 state，不改值也不落盘 ⇒ 切回"方框"时用户原来选的照旧。

        ★R288：方式=分区时名单再按「画法」收一层（`core.highlighter.zone_caps`）。
        ★R293-③ 起这一层是**逐档认领**而不是"非包络框全灰"：双线框/细双线框吃满三味，
        纱环/光晕吃形状+线宽，色带/斜纹/取景角只吃线宽，只有 角贴 仍三味全灰
        （它借逐图标那枚角点画，动它等于同时动"角贴显示模式"那条链 ⇒ 本轮挂账）。
        画法读不到（控件还没建、
        或值认不得）一律按默认档 包络框 算，等价于修前那份表，不会把下拉打死。"""
        try:
            mode = DISPLAY_MODES.get(_hl_c(self.hl_dmode_var.get()), 'border')
            caps = DISPLAY_CAPS.get(mode)
            if mode == 'zone' and caps is not None:
                caps = zone_caps(zone_paint_of(self))
        except Exception:
            return
        if caps is None:      # 表没兜住的新码值：什么都不动，别把整排下拉打死
            return
        for cap, attr in CAP_WIDGETS:
            w = getattr(self, attr, None)
            if w is None:     # 对侧页面/控件还没建 ⇒ 静默（构建与加载两头都调）
                continue
            try:
                w.configure(state='readonly' if cap in caps else 'disabled')
            except Exception:
                pass

    def _on_dim_mode_changed(self, mode: str):
        """亮度路径终态回调（Dimmer 经 post_fn 投递回主线程）。

        ★ R69：软件蒙版是唯一路径，只回显 'sw'（已铺蒙版）/ 'none'（无蒙版）。"""
        # 防御：__init__ 早期 dim_mode_var 尚未创建（历史日志里 AttributeError 过）
        if not hasattr(self, 'dim_mode_var'):
            return
        try:
            self.dim_mode_var.set(self._DIM_MODE_TEXT.get(mode, ''))
        except Exception as e:
            logger.error(f"亮度终态刷新失败: {e}")

    def _hl_parse_color(self) -> str:
        """从 '蓝色 #00BFFF' 取 hex"""
        parts = self.hl_color_var.get().split()
        return parts[-1] if parts else '#00BFFF'

    def _load_highlight_settings(self):
        """加载并应用高亮设置"""
        settings_file = APP_DIR / 'config' / 'highlight.json'
        # ★R313 S6 回补 P-C：这份档读不动**且留底也搬不动**时才置真（下面 _save 认它）。
        self._hl_load_failed = False
        color, width, dashed, pad = '#00BFFF', 2, False, 1
        frame_all = False   # ★ R66 默认无框（用户定案：默认动作一个框都不画）
        shape, bottom_align = '圆角立体', True
        gradient_mode = False
        # ★R282-4（用户 10-01 01 批 R010-3"方式默认选分区"）：fresh config（没有
        # highlight.json 或档值非法）时兜底默认改成 zone。存过档的老用户照旧读回
        # 自己那一档（下面 `data.get('display_mode') in DISPLAY_MODES.values()` 认档），
        # 所以这只改"新装/重置"的第一眼，不动任何人的既有选择。
        display_mode = 'zone'
        cat_only = False   # ★ B12：分类模式只框分类自己的图标（不含常驻）
        frame_sys = False  # ★ B15：框住系统图标（默认关，用户定案）
        sys_only = False   # ★ B16：只框系统图标（与上两者互斥，选中时其余置灰）
        merge = ZONE_TIGHT_DEFAULT   # ★R279-①：并团松紧（旧存档没这个键⇒默认档）
        # ★R282-4：分区配色（旧存档没这个键⇒默认'跟随分类色'，与旧行为一致）
        zone_color_mode = ZONE_COLOR_MODES[0]
        # ★R288：分区画法（旧存档没这个键⇒默认'包络框'＝修前唯一的画法）
        zone_style = ZONE_STYLE_DEFAULT
        try:
            if settings_file.exists():
                data = json.loads(settings_file.read_text(encoding='utf-8'))
                # ★R318 K1：color 与 width/pad 同一口径逐字段安全取值（旧写法原样
                # 收，脏值在下面 try 之外炸掉整条建窗链）。
                color = _hl_color(data, 'color', color)
                # ★R313 F2：逐字段安全取值——旧写法那两行裸转换，一个脏值就把
                # 后面所有字段（分类模式/并团/配色/画法/勾选）一起拖回默认。
                width = _hl_num(data, 'width', 2, float)
                dashed = bool(data.get('dashed', False))
                pad = _hl_num(data, 'pad', 1, int)
                # ★R285-N15：兜底默认必须与上面 `frame_all = False`（R66 定案
                # "默认一个框都不画"）一致。旧写法 `data.get('frame_all', True)`
                # 让**缺这个键的老档**（旧版本配置/手工裁剪）启动即画全框，
                # 并被下一次 _save 原样写回 ⇒ 默认值被永久翻转。
                frame_all = bool(data.get('frame_all', False))
                if data.get('shape') in FRAME_SHAPES:
                    shape = data['shape']
                bottom_align = bool(data.get('bottom_align', True))
                gradient_mode = bool(data.get('gradient_mode', False))
                if data.get('display_mode') in DISPLAY_MODES.values():
                    display_mode = data['display_mode']
                # ★R279-①：认档才收（和 display_mode/shape 同一口径）——
                # 手改坏/导入来的怪值不但不应用，也不能被下面 _save 原样写回去。
                # ★R281-4：旧档名（紧/中/松/很松）经 `zone_tight_resolve` 折进新档，
                # 所以老存档里选过"很松"的人升级后看到的还是"隔1格"那档，不是默认档。
                if data.get('zone_tightness') is not None:
                    merge = zone_tight_resolve(data['zone_tightness'])
                # ★R282-4：分区配色同样认档才收（怪值不应用、也不被 _save 写回）
                if data.get('zone_color_mode') in ZONE_COLOR_MODES:
                    zone_color_mode = data['zone_color_mode']
                # ★R288：画法同样认档才收（旧档没这个键/怪值都留默认档）
                if data.get('zone_style') in ZONE_STYLES:
                    zone_style = data['zone_style']
                cat_only = bool(data.get('cat_only', False))
                frame_sys = bool(data.get('frame_sys', False))
                sys_only = bool(data.get('sys_only', False))
        except Exception as e:
            # ★R313 F2：旧写法 `except Exception: pass` 一行都不留。"回默认"只是
            # 表象——真正丢的是原件：下面任何一次设置操作都会 _save 把这份默认值
            # 写回覆盖 highlight.json。留痕 + 先另存留底再放行（★R231 口径：用户
            # 主动操作不能被死闸锁死，但原件得还在盘上）。
            logger.warning('[高亮] 设置读不动（%r）⇒ 本次按出厂默认运行；'
                           '原文件先另存留底，可从留底找回选项: %s'
                           % (e, settings_file))
            if settings_file.exists():
                _kept, _why = set_aside_corrupt(str(settings_file))
                if not _kept:
                    # ★R313 S6 回补 P-C：`utils/safeio.py` 的明文契约是"搬不动⇒
                    # 调用方必须继续保持禁写"。旧写法把返回值当语句丢了，于是这份
                    # 读不动的原件照样被下面的 _save 用出厂默认覆盖，连留底都没有。
                    self._hl_load_failed = True
                    logger.warning('[高亮] 原件也没能另存留底（%s）⇒ 本轮拒写 '
                                   'highlight.json，它保留在盘上待人工恢复' % (_why,))
        self.highlighter.set_color_hex(color)
        self.highlighter.set_width(width)
        self.highlighter.set_dashed(dashed)
        self.highlighter.set_pad(pad)
        self.highlighter.set_shape(shape)
        self.highlighter.set_bottom_align(bottom_align)
        self.highlighter.set_frame_all(frame_all)
        self.highlighter.set_gradient_mode(gradient_mode)
        self._gradient_restore = gradient_mode   # ★ R79：UI 构建时回填勾选框
        self.highlighter.set_display_mode(display_mode)
        self.highlighter.set_zone_tightness(merge)   # ★R279-①
        self.highlighter.set_zone_color_mode(zone_color_mode)   # ★R282-4
        self.highlighter.set_zone_style(zone_style)             # ★R288
        self.highlighter.cat_only = cat_only
        self.highlighter.frame_sys = frame_sys
        self.highlighter.sys_only = sys_only
        # 更新 UI 变量（控件可能还没创建，静默失败）
        for name, value in PRESET_COLORS.items():
            if value.lower() == color.lower():
                try:
                    self.hl_color_var.set(_hl_t(f'{name} {value}'))
                except Exception:
                    pass
                break
        else:
            try:
                self.hl_color_var.set(_hl_t(f'自定义 {color}'))
            except Exception:
                pass
        try:
            self.hl_width_var.set(str(width))
            self.hl_style_var.set(_hl_t('虚线' if dashed else '实线'))
            self.hl_pad_var.set(str(pad))
            self.hl_frame_all_var.set(frame_all)
            self.hl_shape_var.set(_hl_t(shape))
            self.hl_align_var.set(bottom_align)
            self.hl_cat_only_var.set(cat_only)
            self.hl_frame_sys_var.set(frame_sys)
            self.hl_sys_only_var.set(sys_only)
            # ★ B16：持久化恢复"只框系统图标"时，其余开关同步置灰
            self._sync_frame_scope_states()
            self.hl_dmode_var.set(_hl_t(
                {v: k for k, v in DISPLAY_MODES.items()}.get(display_mode, '方框')))
            # ★R288：画法回填排在置灰刷新**之前**——非默认画法要把形状/线宽/样式
            # 一起灰掉，晚一步就是"存档里选的是角贴、界面却给了三个能点的样式下拉"。
            # 单独包一层 try：控件还没建也不能把下面的刷新与两段回填带走（★R279）。
            try:
                self.hl_zpaint_var.set(_hl_t(zone_style))
            except Exception:
                pass
            # ★R278-D：构建时那次按默认"方框"算的，回填完必须按存档档位重刷一次
            self._sync_mode_caps()
            # ★R279-①：新档位的回填**排在最后**——这一段是一个 try 包到底的，
            # 新项插在中间的话，哪天少一枚控件就是"方式回显 + 置灰刷新"整段半途炸掉
            # （本批 pad_gone 的 L1c 就是这么抓到过一次，不是巧合）
            self.hl_merge_var.set(_hl_t(merge))
            # ★R282-4：分区配色回填同样排在最后（与并团同一口径）
            self.hl_zonecolor_var.set(_hl_t(zone_color_mode))
        except Exception:
            pass

    def _save_highlight_settings(self):
        # ★R313 S6 回补 P-C：读不动且**留底也没搬动** ⇒ 本轮一发都不许写。
        # 内存里这份是出厂默认，写下去就把用户手改的 highlight.json 覆盖了，
        # 而且盘上那份还是读不动的原样（★R231 同款：留底失败必须保持禁写）。
        if getattr(self, '_hl_load_failed', False):
            logger.warning('[高亮] 本轮拒写设置：盘上那份读不动且留底失败'
                           '（内存是出厂默认，写下去会覆盖用户手改的选项）')
            try:
                messagebox.showwarning(
                    i18n.tr('高亮设置', 'Highlight settings'),
                    i18n.tr('高亮设置本轮没有保存：那份文件读不动、也没能另存留底'
                            '（可能被别的程序占用）。请先关闭占用它的程序再重启，'
                            '原件还在盘上。',
                            'Highlight settings were not saved this session: '
                            'that file could not be read and could not be set '
                            'aside (it may be locked by another program). Close '
                            'it and restart - the original file is still there.'),
                    parent=self.root)
            except Exception:
                pass
            return
        # ★R207 NEW3-B1：两段式——先取值（解析失败=输入无效，非"写盘失败"），
        # 再落盘并**消费返回值**（atomic_write_json 失败返回 False 不抛，
        # R204 的 except 接不到它=写失败依旧静默）
        try:
            payload = {
                'color': self._hl_parse_color(),
                'width': float(self.hl_width_var.get()),
                'dashed': _hl_c(self.hl_style_var.get()) == '虚线',
                'pad': int(self.hl_pad_var.get()),
                'frame_all': bool(self.hl_frame_all_var.get()),
                'shape': _hl_c(self.hl_shape_var.get()),
                'bottom_align': bool(self.hl_align_var.get()),
                'gradient_mode': bool(getattr(self, 'hl_gradient_var', None) and self.hl_gradient_var.get()),
                # ★ B12：分类模式只圈分类自己的图标（不含常驻）
                'cat_only': bool(getattr(self, 'hl_cat_only_var', None) and self.hl_cat_only_var.get()),
                # ★ B15：框住系统图标
                'frame_sys': bool(getattr(self, 'hl_frame_sys_var', None) and self.hl_frame_sys_var.get()),
                # ★ B16：只框系统图标（与其余框选开关互斥）
                'sys_only': bool(getattr(self, 'hl_sys_only_var', None) and self.hl_sys_only_var.get()),
                'display_mode': DISPLAY_MODES.get(
                    _hl_c(self.hl_dmode_var.get())
                    if getattr(self, 'hl_dmode_var', None) else 'border', 'border'),
                # ★R279-①：只写认得的档位名（控件还没建/被手改成怪值都回默认档，
                # 免得一次坏写把 highlight.json 污染成"下次也读不出来"）
                'zone_tightness': (
                    _hl_c(self.hl_merge_var.get())
                    if getattr(self, 'hl_merge_var', None)
                    and _hl_c(self.hl_merge_var.get()) in ZONE_TIGHTNESS
                    else ZONE_TIGHT_DEFAULT),
                # ★R282-4：分区配色——只写认得的档（控件还没建/被手改成怪值都回
                # 默认'跟随分类色'，免得一次坏写把 highlight.json 污染成读不出来）
                'zone_color_mode': (
                    _hl_c(self.hl_zonecolor_var.get())
                    if getattr(self, 'hl_zonecolor_var', None)
                    and _hl_c(self.hl_zonecolor_var.get()) in ZONE_COLOR_MODES
                    else ZONE_COLOR_MODES[0]),
                # ★R288：分区画法——同款"认档才写、怪值回默认"（一次坏写不许把
                # highlight.json 污染成下次读不出来）
                'zone_style': (
                    _hl_c(self.hl_zpaint_var.get())
                    if getattr(self, 'hl_zpaint_var', None)
                    and _hl_c(self.hl_zpaint_var.get()) in ZONE_STYLES
                    else ZONE_STYLE_DEFAULT),
            }
        except Exception as e:
            logger.error(f"高亮设置取值失败（输入无效）: {e}")
            try:
                messagebox.showwarning(
                    i18n.tr('高亮设置', 'Highlight settings'),
                    i18n.tr(f'设置项取值失败，未保存：{e}',
                            f'Invalid setting value, not saved: {e}'),
                    parent=self.root)
            except Exception:
                pass
            return
        if not atomic_write_json(APP_DIR / 'config' / 'highlight.json',
                                 payload, indent=4, ensure_ascii=False):
            logger.error("高亮设置写盘失败")
            try:
                messagebox.showwarning(
                    i18n.tr('高亮设置', 'Highlight settings'),
                    i18n.tr('设置写盘失败，可能未保存成功',
                            'Failed to write settings; changes may not be saved'),
                    parent=self.root)
            except Exception:
                pass

    def _dim_scope_key(self):
        """★ R74：当前"作用显示器"的亮度记忆键——all / 0 / 1 ..."""
        idx = getattr(self.dimmer, 'scope', None)
        return 'all' if idx is None else str(idx)

    def _on_rule_restore_all(self):
        """★ R49：撤销"执行规则"的筛选显示——恢复全部图标。

        与悬浮条 [全部] 同路径（_activate_slot_by_name('全部') →
        dock._handle_click 全套），并同步规则帧状态行。"""
        try:
            if self._activate_slot_by_name('全部'):
                self.active_var.set('当前: 全部')
                self._log('⏹ 已恢复全部图标显示（规则筛选已撤销）')
                return
        except Exception as e:
            logger.warning(f'[规则] 恢复全部走槽位路径失败: {e}')
        try:
            # ★ R102 黑桌面防线：恢复全部时顺带确保图标窗口可见
            # （自动隐藏把窗口 SW_HIDE 后，文件级 unhide_all 视觉上无效）
            self.icon_manager.ensure_desktop_view_visible()
            # ★R305：走槽位那一路（上面）已经由 dock 负责散盒；这条兜底路只会翻属性，
            #   收纳档下盒里的图标看不见 ⇒ 先散回再清属性（同「显示全部」缺口）。
            from core import visibility_router as _vr
            _vr.release_all_for(self.icon_manager, 'rule-all', self._log)
            self.icon_manager.unhide_all()
            self.dock._force_listview_refresh()
            self.active_var.set('当前: 全部')
            # ★ R111：兜底路径也要重算框——否则排行榜/规则时代的旧框残留
            try:
                _hl = self.dock._compute_frame_names('全部')
                if _hl and self.highlighter:
                    self.highlighter.highlight_names(_hl)
                elif self.highlighter:
                    self.highlighter.clear()
            except Exception as e:
                logger.warning(f'[规则] 兜底路径重算框失败: {e}')
            self._log('⏹ 已恢复全部图标显示（unhide_all 兜底）')
        except Exception as e:
            logger.error(f'[规则] 恢复全部失败: {e}')
            messagebox.showerror('错误', f'恢复全部失败: {e}', parent=self.root)

    def _on_rules_help(self):
        """❓ 排行榜显示说明弹窗（原 R49 显示规则帮助；R107/R108 两次退役后收编）。"""
        colors = COLORS
        win = tk.Toplevel(self.root)
        win.title('❓ 排行榜显示 说明')
        win.geometry('620x520')
        win.transient(self.root)
        body = tk.Text(win, wrap='word', font=ui_layout.app_font(10),
                       bg=colors['surface'], fg=colors['fg'],
                       relief=tk.FLAT, padx=14, pady=10)
        body.pack(fill=tk.BOTH, expand=True)
        body.tag_configure('h', font=ui_layout.app_font(11, 'bold'),
                           foreground=colors['primary'], spacing1=10, spacing3=4)
        body.tag_configure('warn', foreground=colors['warning'])

        def h(t):
            body.insert(tk.END, t + '\n', 'h')

        def line(t='', tag=None):
            body.insert(tk.END, t + '\n', tag)


        h('🏆 排行榜显示')
        line('按统计榜显示名次区间的图标，其余隐藏（文件只藏不删，随时可还原）。')
        line()
        line('· 排行依据：图标打开榜（双击打开的次数）/ 分类切换榜（悬浮条')
        line('  分类点击频率，"全部"不计入）。')
        line('· 两行输入共用【▶ 应用显示】：填"前 N 名"或"第 M~N 名"；一行')
        line('  有输入另一行自动清空，两行都填按"前 N 名"执行。')
        line('· 名次=排行榜位置：同次数按名字先后排定，M=N 即只显示第 N')
        line('  位的那个图标。')
        line('· 撤销：点【⏹ 恢复全部】（或悬浮条 [全部]）。')
        line('· 数据来自统计功能：统计关闭时用历史数据，会弹窗确认；榜为空时')
        line('  先到"📊 统计"页开启统计并用一段时间。')
        line('· 框跟随"🖍️ 图标高亮框"四开关：全关不画框；框住分类和常驻/只框')
        line('  分类=框榜上显示的图标；框住系统图标=追加系统图标。')
        line('· 图标榜严格按排名显示，系统图标不强制加入；分类榜的系统图标')
        line('  跟随"常驻"设置。')
        line()
        h('🖍️ 图标高亮框（四态简述）')
        line('四态全关=不画框（默认）；框住分类和常驻=分类∪常驻；只框分类=')
        line('不含常驻；框住系统图标=追加系统图标；只框系统图标=只框系统。')
        body.configure(state='disabled')
        ttk.Button(win, text=i18n.tr('关闭', 'Close'), command=win.destroy).pack(pady=(0, 8))


    def _build_rules_tab(self, tab_rules):
        """规则页 UI（R145 自 _build_ui 原样迁入；视觉顺序在 before= 注释里）"""
        # ---- 🏆 排行榜显示（★ R106 V1，实现在 gui/rank_display）----
        # 视觉顺序：高亮框 → 排行榜显示 → 桌面亮度
        # （显示规则 R108 退役——与排行榜重复；自动隐藏 R107 退役）
        _rank_frame = None
        try:
            _rank_frame = rank_display.build_rank_block(self, tab_rules, COLORS)
        except Exception as e:
            logger.error(f"排行榜显示块构建失败(不影响其余界面): {e}")

        # ---- 图标高亮框 ----
        # ★ 用户拍板（2026-09-12）：高亮框排第一、显示规则第二。用 pack 的
        #   before= 插队实现，避免搬动整块代码——创建顺序与视觉顺序不再一致，
        #   视觉顺序以此处 before= 为准：高亮框 → 显示规则 → 排行榜显示 → 桌面亮度
        #   （自动隐藏已退役 R107，代码存档留作将来锁屏功能参考）
        hl_frame = ttk.LabelFrame(tab_rules, text=i18n.tr('🖍️ 图标高亮框', '🖍️ Icon Highlight'),
                                  padding='10', style='Card.TLabelframe')
        if _rank_frame is not None:
            hl_frame.pack(fill=tk.X, pady=(0, 6), before=_rank_frame)
        else:
            hl_frame.pack(fill=tk.X, pady=(0, 6))

        # ★R216 阶段2（用户 2026-09-23 图1 三条：①"每行功能是不一样的，但整
        # 体排列总觉得不舒服"②"是不是应该做个颜色不一样的分开"③卡片右侧一大
        # 块空白）：原来 4 行 run-on pack 把不相干控件横着排一行、只往左铺 ⇒
        # 右半页空、上下行边缘对不上、看不出哪组管什么。改成 2×2 彩色功能区
        # （外观/线条/框选范围/操作），组内 form_row 对齐、按钮 action_bar 等宽分档。
        hl_inner = ttk.Frame(hl_frame)
        hl_inner.pack(fill=tk.X)
        hl_inner.columnconfigure(0, weight=1, uniform='hlcol')
        hl_inner.columnconfigure(1, weight=1, uniform='hlcol')

        # ---- 组①： 外观（primary）----
        g_a, body_a = ui_layout.group_block(
            hl_inner, i18n.tr('🎨 外观', '🎨 Appearance'), 'primary')
        g_a.grid(row=0, column=0, sticky='nw',
                 padx=(0, SP['l']), pady=(0, SP['m']))
        fa = ui_layout.form_block(body_a)
        self.hl_theme_var = tk.StringVar(value=_hl_t('经典蓝'))
        theme_combo = ttk.Combobox(fa, textvariable=self.hl_theme_var,
                                   width=12, state='readonly',
                                   values=[_hl_t(k) for k in list(THEMES.keys()) + ['自定义']])
        theme_combo.bind('<<ComboboxSelected>>', lambda e: self._on_hl_theme_change())
        ui_layout.form_row(fa, 0, i18n.tr('模式:', 'Mode:'), theme_combo)
        self.hl_color_var = tk.StringVar(value=_hl_t('蓝色 #00BFFF'))
        color_combo = ttk.Combobox(fa, textvariable=self.hl_color_var,
                                   width=12, state='readonly',
                                   values=[_hl_t(f'{n} {c}') for n, c in PRESET_COLORS.items()]
                                          + [_hl_t('自定义...')])
        color_combo.bind('<<ComboboxSelected>>', self._on_hl_color_selected)
        ui_layout.form_row(fa, 1, i18n.tr('颜色:', 'Color:'), color_combo)
        # ★R282-4：分区配色——只有"方式=分区"用得到（挂 CAP_WIDGETS 表自动灰/不灰，
        # 灰时③那枚悬停说明会讲"为什么灰"）。'跟随分类色'＝一团一色（旧行为）；
        # '统一色'＝全团用上面选的"颜色/模式(主题)" ⇒ 这两个下拉对分区也联动
        # （用户点名"分区跟模式、颜色都不能联动"）。
        self.hl_zonecolor_var = tk.StringVar(value=_hl_t('跟随分类色'))
        self.hl_zonecolor_combo = ttk.Combobox(
            fa, textvariable=self.hl_zonecolor_var, width=12, state='readonly',
            values=[_hl_t(k) for k in ZONE_COLOR_MODES])
        self.hl_zonecolor_combo.bind(
            '<<ComboboxSelected>>', lambda e: self._on_hl_zonecolor_change())
        ui_layout.form_row(fa, 2, i18n.tr('分区配色:', 'Zone color:'),
                           self.hl_zonecolor_combo)
        self.hl_shape_var = tk.StringVar(value=_hl_t('圆角立体'))
        self.hl_shape_combo = ttk.Combobox(fa, textvariable=self.hl_shape_var,
                                           width=12, state='readonly',
                                           values=[_hl_t(s) for s in FRAME_SHAPES])
        self.hl_shape_combo.bind('<<ComboboxSelected>>',
                                 lambda e: self._on_hl_shape_change())
        ui_layout.form_row(fa, 3, i18n.tr('形状:', 'Shape:'), self.hl_shape_combo)
        # ★R278-D：这一排下拉的可用性改到线条组建完再统一刷（见组②末尾）
        # ★ B15：渐变色是颜色/样式类开关，留在外观组内（★R216：从行尾挪进组内，
        # 不再跟在下拉框后面挤成一串）
        self.hl_gradient_var = tk.BooleanVar(
            value=getattr(self, '_gradient_restore', False))   # ★ R79 回填
        ui_layout.form_row(fa, 4, '', ttk.Checkbutton(
            fa, text=i18n.tr('🎨 渐变色（按分类色）',
                             '🎨 Gradient (by category color)'),
            variable=self.hl_gradient_var,
            command=self._on_hl_gradient_toggle))

        # ---- 组②：📏 线条与显示（accent）----
        g_b, body_b = ui_layout.group_block(
            hl_inner, i18n.tr('📏 线条与显示', '📏 Line & Display'), 'accent')
        g_b.grid(row=0, column=1, sticky='nw', pady=(0, SP['m']))
        fb = ui_layout.form_block(body_b)
        self.hl_width_var = tk.StringVar(value='2')
        self.hl_width_combo = ttk.Combobox(fb, textvariable=self.hl_width_var,
                                           width=12, state='readonly',
                                           values=['0.5', '1', '1.5', '2', '3',
                                                   '4', '5'])
        self.hl_width_combo.bind('<<ComboboxSelected>>',
                                 lambda e: self._on_hl_style_change())
        ui_layout.form_row(fb, 0, i18n.tr('线宽:', 'Width:'), self.hl_width_combo)
        self.hl_style_var = tk.StringVar(value=_hl_t('实线'))
        self.hl_style_combo = ttk.Combobox(fb, textvariable=self.hl_style_var,
                                           width=12, state='readonly',
                                           values=[_hl_t('实线'), _hl_t('虚线')])
        self.hl_style_combo.bind('<<ComboboxSelected>>',
                                 lambda e: self._on_hl_style_change())
        ui_layout.form_row(fb, 1, i18n.tr('样式:', 'Style:'), self.hl_style_combo)
        # ★R279-②：「框大小」下拉已摘掉（渲染侧零读者 ⇒ 恒灰的死控件）。
        # hl_pad_var 这份**值**留着：老配置读得进来、主题预设照旧回填、
        # 存档键照旧写回（不改用户没在改的东西），只是界面上不再暴露它。
        self.hl_pad_var = tk.StringVar(value='1')
        self.hl_dmode_var = tk.StringVar(value=_hl_t('方框'))
        self.hl_dmode_combo = dmode_combo = ttk.Combobox(
            fb, textvariable=self.hl_dmode_var, width=12,
            state='readonly',
            values=[_hl_t(k) for k in DISPLAY_MODES.keys()])
        dmode_combo.bind('<<ComboboxSelected>>',
                         lambda e: self._on_hl_display_mode_change())
        ui_layout.form_row(fb, 2, i18n.tr('方式:', 'Display:'), dmode_combo)
        # ★R288（批44，用户点名"把色块纱环/荧光笔/角贴也支持到分区里，分区更多
        # 更好看"）：分区内部再选「画法」。默认档 包络框＝修前唯一的画法 ⇒ 老配置
        # 升上来画面一格不动。这一档只有"方式=分区"读，所以照样挂 CAP_WIDGETS 表，
        # 并且它反过来决定 形状/线宽/样式 三味在分区里点不点得动（见 zone_caps）。
        self.hl_zpaint_var = tk.StringVar(value=_hl_t(ZONE_STYLE_DEFAULT))
        self.hl_zpaint_combo = ttk.Combobox(
            fb, textvariable=self.hl_zpaint_var, width=12, state='readonly',
            values=[_hl_t(k) for k in ZONE_STYLES])
        self.hl_zpaint_combo.bind('<<ComboboxSelected>>',
                                  lambda e: self._on_hl_zpaint_change())
        ui_layout.form_row(fb, 3, i18n.tr('画法:', 'Paint:'),
                           self.hl_zpaint_combo)
        # ★R279-①（批35 item1）：并团松紧——"隔多远还算一伙"交给用户定。
        # 只有"方式=分区"用得上 ⇒ 挂在 CAP_WIDGETS 表里按方式自动灰/不灰，
        # 灰的时候③那枚悬停说明会告诉为什么点不动。
        # ★R288：行号 3→4——「画法」插在"方式"后面，并团往后挪一行
        self.hl_merge_var = tk.StringVar(value=_hl_t(ZONE_TIGHT_DEFAULT))
        self.hl_merge_combo = ttk.Combobox(
            fb, textvariable=self.hl_merge_var, width=12, state='readonly',
            values=[_hl_t(k) for k in ZONE_TIGHTNESS.keys()])
        self.hl_merge_combo.bind('<<ComboboxSelected>>',
                                 lambda e: self._on_hl_merge_change())
        ui_layout.form_row(fb, 4, i18n.tr('并团:', 'Cluster:'),
                           self.hl_merge_combo)
        # ★R278-D：三味下拉 + 形状都在场了，按默认档位刷一次可用性
        self._sync_mode_caps()

        # ★R288：这五味都是"按方式置灰"的，各挂一枚说明"为什么灰"
        #（表驱动一处挂齐；文案悬停时现算，切方式/切画法自动跟着变）
        # ★R279-③：原口径同上。画法这层是这批补的——分区里灰掉形状，凶手往往是
        # 画法而不是方式，说明里点名点错人等于没说。
        def _cap_tip_for(cap):
            mode_zh = _hl_c(self.hl_dmode_var.get())
            paint = zone_paint_of(self)
            return cap_tip_text(
                cap, mode_zh,
                paint if mode_zh == '分区' and paint != ZONE_STYLE_DEFAULT
                else None)

        for _cap, _attr in CAP_WIDGETS:
            _w = getattr(self, _attr, None)
            if _w is not None:
                attach_disable_tip(_w, lambda c=_cap: _cap_tip_for(c))
        # ★R280-B：「并团」这一枚**能用时**也要有话说——灰着讲"为什么灰"，
        # 能点讲"这一档是几像素、我该往哪挪"。档位悬停时现算 ⇒ 换了档跟着变。
        _merge_w = getattr(self, 'hl_merge_combo', None)
        if _merge_w is not None:
            attach_info_tip(_merge_w, lambda: merge_hint_text(
                _hl_c(self.hl_merge_var.get())))
        # ★ B15：同列底边对齐（原第四行随"方框常显"一起精简并入线条组）
        # ★R279-②：行号 4→3——「框大小」摘掉后留一行空 grid 就是看得见的空洞
        # ★R279-①：行号 3→4——「并团」这一行插在"方式"后面，对齐照旧排最后
        # ★R288：行号 4→5——「画法」插在"方式"后面，对齐仍排最后
        self.hl_align_var = tk.BooleanVar(value=True)
        ui_layout.form_row(fb, 5, '', ttk.Checkbutton(
            fb, text=i18n.tr('📐 同列底边对齐', '📐 Align Bottom in Column'),
            variable=self.hl_align_var,
            command=self._on_hl_align_toggle))

        # ---- 组③：🔲 框选范围（warning）----
        g_c, body_c = ui_layout.group_block(
            hl_inner, i18n.tr('🔲 框选范围', '🔲 Frame Scope'), 'warning')
        g_c.grid(row=1, column=0, sticky='nw', padx=(0, SP['l']))
        fc = ui_layout.form_block(body_c)
        self.hl_frame_all_var = tk.BooleanVar(value=False)   # ★ R66 默认无框（用户定案）
        self.hl_frame_all_cb = ttk.Checkbutton(
            fc, text=i18n.tr('🔲 框住分类和常驻', '🔲 Frame Category + Resident'),
            variable=self.hl_frame_all_var,
            command=self._on_hl_frame_all_toggle)
        ui_layout.form_row(fc, 0, '', self.hl_frame_all_cb)
        # ★ B12/B13：只框分类——与"框住分类和常驻"联动互斥（语义见框选语义档案）
        self.hl_cat_only_var = tk.BooleanVar(value=False)
        self.hl_cat_only_cb = ttk.Checkbutton(
            fc, text=i18n.tr('🎯 只框分类（不含常驻）', '🎯 Category Only (no resident)'),
            variable=self.hl_cat_only_var,
            command=self._on_hl_cat_only_toggle)
        ui_layout.form_row(fc, 1, '', self.hl_cat_only_cb)
        # ★ B15：框住系统图标——追加型：在当前基础名单上多加框系统图标
        self.hl_frame_sys_var = tk.BooleanVar(value=False)
        self.hl_frame_sys_cb = ttk.Checkbutton(
            fc, text=i18n.tr('🖥️ 框住系统图标', '🖥️ Add System Icons'),
            variable=self.hl_frame_sys_var,
            command=self._on_hl_frame_sys_toggle)
        ui_layout.form_row(fc, 2, '', self.hl_frame_sys_cb)
        # ★ B16：只框系统图标——独占：只框系统，其余一概不框（选中时上面三个置灰）
        self.hl_sys_only_var = tk.BooleanVar(value=False)
        self.hl_sys_only_cb = ttk.Checkbutton(
            fc, text=i18n.tr('🖥️ 只框系统图标', '🖥️ System Icons Only'),
            variable=self.hl_sys_only_var,
            command=self._on_hl_sys_only_toggle)
        ui_layout.form_row(fc, 3, '', self.hl_sys_only_cb)
        self._sync_frame_scope_states()

        # ---- 组④：🖍️ 高亮操作（secondary）----
        # ★ R66：下拉放最前免切管理页；★R216：三个按钮从"各自宽度贴左"换成
        # 等宽一排并按语义分档（主操作实底 / 常规描边 / 清除警示）
        g_d, body_d = ui_layout.group_block(
            hl_inner, i18n.tr('🖍️ 高亮操作', '🖍️ Highlight Actions'), 'secondary')
        g_d.grid(row=1, column=1, sticky='nw')
        fd = ui_layout.form_block(body_d)
        self.hl_quickpick_var = tk.StringVar()
        self._hl_quickpick_combo = ttk.Combobox(
            fd, textvariable=self.hl_quickpick_var,
            state='readonly', width=22,
            postcommand=lambda: rule_quickpick.refresh_values(self))
        ui_layout.form_row(fd, 0, i18n.tr('📍 当前分类:', '📍 Category:'),
                           self._hl_quickpick_combo)
        self._hl_quickpick_combo.bind(
            '<<ComboboxSelected>>',
            lambda e: rule_quickpick.on_selected(self))

        # ★ R67：↑/↓ 方向键、鼠标中键——不展开下拉直接循环切换并即时上框
        def _qp_step(delta):
            rule_quickpick.step(self, delta)
            return 'break'
        self._hl_quickpick_combo.bind('<Up>', lambda e: _qp_step(-1))
        self._hl_quickpick_combo.bind('<Down>', lambda e: _qp_step(1))
        self._hl_quickpick_combo.bind('<Button-2>', lambda e: _qp_step(1))
        # ★R272（批25，用户 2026-09-29 01 批 item1「我觉得三分钟是可以修改的，
        # 而且三分钟也可以递减，不过递减的慢点。这样负载更小」）：
        # 扳机命中后"密集盯"多久可配。窗口内节拍 2→6 秒线性慢爬（负载 -50%），
        # 窗口外平方快递减到睡死；调短负载立刻下来（3 分→0.5 分约 -83%）。
        # 用只读下拉而不是输入框：值域就是 [_WIN_SEC_MIN, _WIN_SEC_MAX]，
        # 免得用户填个 0 或 999 还要靠夹取兜（set_watch_window 仍留了夹取，
        # 因为 app_settings 会随"导入全部数据"从外部 JSON 进来）。
        try:
            _ww_cur = '%g' % (float(self._app_settings.get(
                'icon_watch_window_sec', 180)) / 60.0)
        except (TypeError, ValueError):
            _ww_cur = ''          # 盘上是脏值 ⇒ 落到下面的缺省 '3'
        self.watch_window_var = tk.StringVar(
            value=_ww_cur if _ww_cur in ('0.5', '1', '2', '3', '5', '10') else '3')
        ww_combo = ttk.Combobox(fd, textvariable=self.watch_window_var,
                                width=8, state='readonly',
                                values=['0.5', '1', '2', '3', '5', '10'])
        ww_combo.bind('<<ComboboxSelected>>',
                      lambda e: self._on_watch_window_change())
        ui_layout.form_row(fd, 1, i18n.tr('⏱️ 监视时长(分):', '⏱️ Watch (min):'),
                           ww_combo)
        ui_layout.action_bar(body_d, [
            (i18n.tr('🖍️ 高亮选中分类', '🖍️ Highlight Category'),
             self._on_hl_category, 'primary.TButton'),
            (i18n.tr('🔄 生成方框', '🔄 Draw Frames'),
             self._on_hl_rebuild, 'secondary.TButton'),
            (i18n.tr('🚫 清除高亮', '🚫 Clear Highlight'),
             self._on_hl_clear, 'warning.TButton'),
        ], cols=3, pad=SP['xs'],
            # ★R217：本小节在右栏（可用宽 ≈503px）。等宽档 uniform 会把三列
            # 都拉到最宽那枚的自然宽（English "Highlight Category" 194px ⇒
            # 3×194=594 > 503 ⇒ "Clear Highlight" 被卡片右缘裁掉）。英文文案
            # 天然比中文长，这里改按各自内容取宽（194+132+150+8=484 ≤503），
            # 两语都不裁。
            equal=False)

        ttk.Label(hl_frame, text=i18n.tr('框只在桌面显示，被窗口盖住自动隐藏',
                                         'Frames show on the desktop only; auto-hidden behind windows'),
                  foreground=COLORS['text_secondary'],
                  font=ui_layout.app_font(9)).pack(anchor=tk.W, pady=(SP['xs'], 0))

        # ---- 桌面亮度（2026-09-05 用户需求：整体调暗适合晚上不刺眼）----
        dim_frame = ttk.LabelFrame(tab_rules, text=i18n.tr('🌙 桌面亮度（整体调暗·夜间护眼）', '🌙 Display Brightness (dim for night)'),
                                   padding='10', style='Card.TLabelframe')
        dim_frame.pack(fill=tk.X)
        dim_row = ttk.Frame(dim_frame)
        dim_row.pack(fill=tk.X)
        ttk.Label(dim_row, text=i18n.tr('亮度:', 'Brightness:')).pack(side=tk.LEFT, padx=(0, 6))
        # ★ R74→R98：每个显示器各自记忆亮度数字——调某一台只动那一台，
        #   其他屏蒙版原样保留；记忆持久化在 app_settings.json 的
        #   dim_scope_levels 键（{'all': 80, '0': 65, '1': 90}），
        #   desktop_brightness 保持兼容（=最近一次应用的值）。
        # ★R264 审计 Z6：这两行在**主窗构建链**（_build_rules_tab ← __init__）
        # 上且无 try：包里写坏 dim_scope_levels（列表/字符串）⇒ dict() TypeError；
        # 写坏分值（{"all": "八成"}）⇒ int() ValueError ⇒ 主窗建不起来、程序打不开。
        # 与本文件 :160 那族取值同口径（脏值回退默认 + 留日志），不再裸奔。
        _raw_levels = self._app_settings.get('dim_scope_levels', {})
        if not isinstance(_raw_levels, dict):
            logger.warning('设置 dim_scope_levels 不是对象（%r），已按空记忆运行'
                           % (_raw_levels,))
            _raw_levels = {}
        self._dim_levels = dict(_raw_levels)
        _dim_key = 'all' if getattr(self.dimmer, 'scope', None) is None \
            else str(self.dimmer.scope)
        _dim_lv = _safe_int(self._dim_levels.get(
            _dim_key, self._app_settings.get('desktop_brightness', 100)),
            100, 'desktop_brightness')
        self.dim_var = tk.IntVar(value=_dim_lv)
        self.dim_scale = tk.Scale(
            dim_row, from_=30, to=100, orient=tk.HORIZONTAL, showvalue=True,
            variable=self.dim_var, length=280, resolution=1,
            command=lambda v: self._on_dim_change(v),
            bg=COLORS['surface'], fg=COLORS['fg'],
            highlightthickness=0, troughcolor=COLORS['bg'],
            activebackground=COLORS['primary'])
        self.dim_scale.pack(side=tk.LEFT, padx=(0, 10))
        self.dim_scale.bind('<ButtonRelease-1>',
                            lambda e: self._save_app_settings())
        # ★ R65-Bug6 / R69：作用显示器选择（软件蒙版按屏分区=分屏调光；
        # 硬件 DDC 已移除，设置键沿用 desktop_hw_scope 保持兼容）
        ttk.Label(dim_row, text=i18n.tr('作用显示器:', 'Monitor:')).pack(side=tk.LEFT, padx=(10, 0))
        self.dim_scope_var = tk.StringVar(value='全部显示器')
        self.dim_scope_combo = ttk.Combobox(
            dim_row, textvariable=self.dim_scope_var, state='readonly',
            width=28, postcommand=self._dim_scope_refresh)
        self.dim_scope_combo.pack(side=tk.LEFT, padx=(6, 0))
        self.dim_scope_combo.bind(
            '<<ComboboxSelected>>', lambda e: self._on_dim_scope_change())
        try:
            self._dim_scope_refresh()
        except Exception as e:
            logger.error(f"显示器列表刷新失败: {e}")
        self.dim_mode_var = tk.StringVar(value='')
        ttk.Label(dim_row, textvariable=self.dim_mode_var,
                  foreground=COLORS['secondary'],
                  font=ui_layout.app_font(9, 'bold')).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Label(dim_row, text=i18n.tr('（100=正常亮度，数值越小桌面越暗）', '(100 = normal; lower = dimmer)'),
                  foreground=COLORS['text_secondary'],
                  font=ui_layout.app_font(9)).pack(side=tk.LEFT)
        ttk.Label(dim_frame,
                  text=i18n.tr('★ 每台显示器各记各的亮度：调某一台只动那一台，'
                               '其他屏保持不变；"全部显示器"不主动改动，拖动滑条'
                               '才统一调节全部。★ 硬件背光调节已于 R69 移除'
                               '（实测在本机触发系统级死机，详见开发日记），软件蒙版'
                               '为唯一路径。与聚光灯同用时，高亮图标始终比背景亮一档。',
                               '★ Each monitor remembers its own brightness: '
                               'adjusting one leaves the others untouched; '
                               '"All monitors" changes nothing until you drag '
                               'the slider, which sets every monitor. Hardware '
                               'backlight was removed in R69 '
                               '(it triggered system crashes on this machine — see '
                               'dev log); the software mask is the only path. With the '
                               'spotlight on, highlighted icons stay one step brighter.',
                               ),
                  foreground=COLORS['text_secondary'],
                  font=ui_layout.app_font(9)).pack(anchor=tk.W, pady=(4, 0))

