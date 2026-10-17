# -*- coding: utf-8 -*-
"""🧩 设置页面板（R142 自 gui/main_window.py 拆出，codeSplitter 第二刀）。

职责：📌 悬浮窗设置卡 + 🤖 公共分类显示矩阵卡 + 相关 handler + 显示层
helper（cr_*/fd_*，main_window 经模块别名继续供给管理页/启动回填）。
纪律：**严禁 import gui.main_window**（模块双身份，会把直跑脚本再执行
一遍）；gui 依赖一律走参数传入的 gui 对象属性。
"""
import logging
import sys          # ★R241：只用于 sys.modules.get('gui.main_window') 查 SELF_CHECK
                    # （查已加载模块，不 import——本模块纪律禁止导入 main_window）

import tkinter as tk
from tkinter import ttk, messagebox

from gui import icons   # ★R225 阶段5：图标档取值口径统一（MODES/默认档）
from gui import hotkey_panel   # ★R226（P2-2）：热键显示读真实绑定（单向依赖，无环）
# ★R280-A：渐变档位的**表**住在渲染侧（core/dock），下拉只读它做 values——
# 与 rule_panel 读 core/icon_overlay.ZONE_TIGHTNESS 同一个思路，口径只有一个出处。
# core/dock 不 import 任何 gui 模块（已核），这里导入不成环。
from core.dock import GRAD_DEFAULT_LEVEL, GRADIENT_LEVELS
from gui import ui_layout
from gui.ui_layout import SP
from utils import i18n

logger = logging.getLogger("desktop_manager")

# 主题色引用：build_cards 时传入主窗 COLORS 的**引用**（apply_ui_theme 原地
# 更新该 dict，主题切换后这里自动跟随）
_colors = {'bg': '#282a36', 'fg': '#f8f8f2', 'primary': '#8be9fd',
           'surface': '#44475a', 'border': '#636d83',
           'warning': '#ffb86c', 'muted': '#6272a4', 'secondary': '#bd93f9',
           # ★R221（NEW7-P1-3）：同 manage_panel——本模块有 _colors 下标直取
           # text_secondary，兜底表缺这一键（AST 门禁 1.1 实锤）
           'text_secondary': '#bcc3d6'}


def set_colors(colors: dict):
    """持有主窗 COLORS 的引用（非拷贝），随主题原地换色"""
    global _colors
    _colors = colors


# ---------------------------------------------------------------------
# 显示层 helper（R79e/R94/R129 范式：显示随语言、存储保持中文 canonical）
# ---------------------------------------------------------------------

def cr_disp(n: str) -> str:
    if n == '当前':
        return i18n.tr('当前', 'Current')
    if n == '全部':
        return i18n.tr('全部', 'All')
    return n


def cr_canon(v: str) -> str:
    if v == '当前' or (i18n.is_en() and v == 'Current'):
        return '当前'
    if v == '全部' or (i18n.is_en() and v == 'All'):
        return '全部'
    return v


# '标准' 是"界面字体"档位的规范键（横条字体用 '中'）——两个下拉共用同一张
# 显示表，反查表 fd_canon 里 'Standard'→'标准' 与 'Medium'→'中' 不冲突
_FONT_DISP = {'小': 'Small', '中': 'Medium', '标准': 'Standard',
              '大': 'Large', '超大': 'X-Large'}
_FONT_CANON = {v: k for k, v in _FONT_DISP.items()}


def fd_disp(n):
    if n in _FONT_DISP:
        return i18n.tr(n, _FONT_DISP[n])
    return n


def fd_canon(v):
    return _FONT_CANON.get(v, v)


# ★R225 阶段5：界面密度三档。规范键=中文（与 ui_layout.DENSITY 的键严格一致，
# 存储/取值都用它），显示层走 dp_disp/dp_canon——和主题色、字体档同一套口径。
_DENSITY_DISP = {'紧凑': 'Compact', '标准': 'Standard', '宽松': 'Relaxed'}
_DENSITY_CANON = {v: k for k, v in _DENSITY_DISP.items()}
UI_DENSITIES = ('紧凑', '标准', '宽松')


def dp_disp(n):
    if n in _DENSITY_DISP:
        return i18n.tr(n, _DENSITY_DISP[n])
    return n


def dp_canon(v):
    return _DENSITY_CANON.get(v, v)


# 图标风格：规范键直接用机器码（gui/icons.MODES），显示层双语
_ICONS_DISP = {'mdl2': ('图标字体', 'Icon Font'),
               'emoji': ('Emoji 图标', 'Emoji Icons')}


def ic_disp(k):
    zh, en = _ICONS_DISP.get(k, (k, k))
    return i18n.tr(zh, en)


def ic_canon(v):
    for k, (zh, en) in _ICONS_DISP.items():
        if v in (zh, en):
            return k
    return v


# 字号表（main_window 以类属性别名引用同一 dict，启动回填经 self. 取用）
DOCK_FONT_SIZES = {'小': 13, '中': 16, '大': 20, '超大': 26}
DOCK_FONT_NAMES = {v: k for k, v in DOCK_FONT_SIZES.items()}
UI_FONT_SIZES = {'小': 9, '标准': 11, '大': 13}
DEFAULT_UI_FONT = '标准'

# ★R280-A（用户 06 批 item1「④：不明显」）：渐变幅度做成三档。
# **档位表本体住在 core/dock.py**（渲染侧唯一读者），这里只补"显示叫什么名"——
# 下拉的 values 直接从 GRADIENT_LEVELS 的键生成，以后加一档不用改这里。
_GRAD_DISP = {'朴素': ('朴素', 'Flat'),
              '微渐变': ('微渐变', 'Subtle'),
              '明显': ('明显', 'Strong')}


def gd_disp(k):
    zh, en = _GRAD_DISP.get(k, (k, k))
    return i18n.tr(zh, en)


def gd_canon(v):
    for k, (zh, en) in _GRAD_DISP.items():
        if v in (zh, en):
            return k
    return v


# ---------------------------------------------------------------------
# 🤖 公共分类显示：三列互斥矩阵
# ---------------------------------------------------------------------

def make_sticky_chip(gui, parent, text, var, kind, name, row):
    """创建一个 sticky 网格单元（tk.Checkbutton）并返回。

    ★R228（用户 22 批 item8「公共图标分类显示，每列都没对齐、有点歪」）：
    原来 anchor=tk.CENTER——列宽是等分的（uniform='stky'），但每格内容
    （☐+分类名）在格内**居中**，分类名长短不一 ⇒ 每行的勾选框左边线各在
    不同位置，看上去三列都是锯齿。改成 anchor=tk.W 后每列共用一条左边线
    （与 form_row 的控件列同一口径）。
    """
    _col_fg = {'resident': _colors['primary'], 'exclusive': _colors['fg'],
               'exclusive_sys': _colors['warning']}
    cb = tk.Checkbutton(parent, text=text, variable=var,
                        anchor=tk.W, relief=tk.FLAT, bd=0,
                        highlightthickness=0,
                        fg=_col_fg[kind], bg=_colors['surface'],
                        activeforeground=_col_fg[kind],
                        activebackground=_colors['surface'],
                        selectcolor=_colors['surface'],
                        command=lambda: on_sticky_chip(gui, name, kind))
    cb.grid(row=row, column={'resident': 0, 'exclusive': 1,
                             'exclusive_sys': 2}[kind],
            sticky=tk.EW, padx=10, pady=1)
    return cb


def refresh_sticky_chip(gui, name: str, kind: str):
    """勾选态高亮：常驻=✓+主题青，只显示自己=✓+前景色，只显示自己+隐系统=✓+暖橙"""
    colors = {'resident': _colors['primary'], 'exclusive': _colors['fg'],
              'exclusive_sys': _colors['warning']}
    btn, var = None, None
    if kind == 'resident':
        btn = gui._resident_btns.get(name)
        var = gui._resident_vars.get(name)
    elif kind == 'exclusive':
        btn = gui._exclusive_btns.get(name)
        var = gui._exclusive_vars.get(name)
    elif kind == 'exclusive_sys':
        btn = gui._exsys_btns.get(name)
        var = gui._exsys_vars.get(name)
    if not btn or not var:
        return
    if var.get():
        btn.configure(fg=colors[kind], text=f'☑ {name}')
    else:
        btn.configure(fg=_colors['muted'], text=name)


def on_sticky_chip(gui, name: str, kind: str):
    """勾选：只改外观不落盘（轻快）；同一行三列互斥；按"应用"才生效"""
    others = {'resident': ('exclusive', 'exclusive_sys'),
              'exclusive': ('resident', 'exclusive_sys'),
              'exclusive_sys': ('resident', 'exclusive')}[kind]
    vars_map = {'resident': gui._resident_vars,
                'exclusive': gui._exclusive_vars,
                'exclusive_sys': gui._exsys_vars}
    for other in others:
        if other in vars_map and name in vars_map[other]:
            vars_map[other][name].set(False)
            refresh_sticky_chip(gui, name, other)
    refresh_sticky_chip(gui, name, kind)


def rebuild_sticky_grid(gui):
    """清空 sticky chips 容器并按当前 category_manager 状态重建。

    新建/重命名/删除/导入分类后调用，保证网格与分类列表同步。
    重命名场景下旧名 var/btn 会被新名覆盖，display_mode 由分类配置带出。
    """
    if not hasattr(gui, '_sticky_chips'):
        return
    chips = gui._sticky_chips
    # 销毁旧内容（表头/系统图标/各分类）
    for child in list(chips.winfo_children()):
        child.destroy()
    # 重置字典（重命名时旧 key 不再生成，自动消失）
    gui._resident_vars = {}
    gui._exclusive_vars = {}
    gui._exsys_vars = {}
    gui._resident_btns = {}
    gui._exclusive_btns = {}
    gui._exsys_btns = {}

    _col_fg = {'resident': _colors['primary'], 'exclusive': _colors['fg'],
               'exclusive_sys': _colors['warning']}
    _col_defs = [(i18n.tr('① 常驻（跟随所有分类）', '① Resident (follows all)'), 'resident'),
                 (i18n.tr('② 只显示自己', '② Only itself'), 'exclusive'),
                 (i18n.tr('③ 只显示自己+隐系统', '③ Only itself + hide sys'), 'exclusive_sys')]
    for _ci, (_cn, _ck) in enumerate(_col_defs):
        # ★R228（item8）：表头也左贴，和下面的勾选框共用同一条左边线
        # （原来 ttk.Label 默认居中 ⇒ 表头文字与勾选框对不上，整列看着更歪）
        ttk.Label(chips, text=_cn, foreground=_col_fg[_ck],
                  background=_colors['surface'], anchor='w',
                  font=ui_layout.app_font(9, 'bold')).grid(
            row=0, column=_ci, sticky=tk.EW, padx=10)

    # 系统图标行（row 1，仅常驻列）
    # ★R264 审计 Z12：旧写法"var 已存在就复用"（★ P1-N20）在导入这一路成了
    # 漏网——「导入全部数据」先 `update()` 了 `_app_settings`，再专门调本函数
    # 刷新网格（gui/data_manage_page.py:1146 注释写着"不重建的话用户点应用会
    # 把旧 var 写回去覆盖刚导入的值"），但 var 是复用的旧对象 ⇒ 勾态仍是旧值，
    # 那句预防正好没生效；用户随后点一次设置页「应用」（本文件 :276-278）就把
    # var 里那份旧值写回 `system_icons_resident` 落盘，导入的选择静默蒸发。
    # 口径与本函数自己的纪律统一：**重建＝一切按已落盘的那一份重搭**
    # （下面各分类行的 display_mode 也是每次从 category_manager 重读，
    #   从不复用旧 var）。
    _sys_var = tk.BooleanVar(value=bool(
        gui._app_settings.get('system_icons_resident', True)))
    gui._system_resident_var = _sys_var
    # ★R204 GUIB-11：系统图标 var 也注册进 _resident_vars——此前只进
    # _resident_btns，勾选态同步（refresh_sticky_chip 的 var 分支）永不生效
    gui._resident_vars['🖥 系统图标'] = _sys_var
    gui._resident_btns['🖥 系统图标'] = make_sticky_chip(
        gui, chips, i18n.tr('🖥 系统图标', '🖥 System Icons'), _sys_var,
        'resident', '🖥 系统图标', 1)
    # 系统图标初始 ✓ 同步（与下方分类一致）
    refresh_sticky_chip(gui, '🖥 系统图标', 'resident')

    # 各分类行（row 2+）
    for _i, _c in enumerate(gui.category_manager.get_all_categories()):
        _name = _c['name']
        _mode = _c.get('display_mode', 'none')
        _rv = tk.BooleanVar(value=_mode == 'resident')
        _ev = tk.BooleanVar(value=_mode == 'exclusive')
        _xv = tk.BooleanVar(value=_mode == 'exclusive_sys')
        gui._resident_vars[_name] = _rv
        gui._exclusive_vars[_name] = _ev
        gui._exsys_vars[_name] = _xv
        gui._resident_btns[_name] = make_sticky_chip(
            gui, chips, _name, _rv, 'resident', _name, _i + 2)
        gui._exclusive_btns[_name] = make_sticky_chip(
            gui, chips, _name, _ev, 'exclusive', _name, _i + 2)
        gui._exsys_btns[_name] = make_sticky_chip(
            gui, chips, _name, _xv, 'exclusive_sys', _name, _i + 2)
        # ★ P2-N20：三列互斥，只刷 display_mode 对应那一列
        # 未选中列默认外观已在 make_sticky_chip 里配好，无需重复 configure
        if _mode in ('resident', 'exclusive', 'exclusive_sys'):
            refresh_sticky_chip(gui, _name, _mode)


def on_sticky_apply(gui):
    """应用三列网格：每分类 display_mode + 系统图标常驻开关"""
    failed = []
    for n in list(gui._resident_vars.keys()):
        if n == '🖥 系统图标':
            # ★R204 GUIB-11：系统图标是全局开关不是分类（下方单独保存），
            # 不能走 set_display_mode
            continue
        if gui._resident_vars[n].get():
            mode = 'resident'
        elif gui._exsys_vars.get(n) and gui._exsys_vars[n].get():
            mode = 'exclusive_sys'
        elif gui._exclusive_vars.get(n) and gui._exclusive_vars[n].get():
            mode = 'exclusive'
        else:
            mode = 'none'
        if not gui.category_manager.set_display_mode(n, mode):
            failed.append(n)   # 分类已改名/删除时网格未重建
    if failed:
        gui._log(f"⚠ 以下分类应用失败（改名/删除后请重启程序刷新面板）: {failed}")
    sys_on = gui._system_resident_var.get()
    # ★R297-H：先把旧的总闸读进一份基线（默认 True，与 dock.py:371 那把初值一致），
    # 只有当用户这次点「应用」把 sys_on 真的翻了才走冲刷+摆位——不变也摆＝把用户
    # 手动拖好的位置刷掉，比不摆更糟。
    _prev_sys_on = bool(gui._app_settings.get('system_icons_resident', True))
    _sys_on_changed = bool(sys_on) != _prev_sys_on
    gui._app_settings['system_icons_resident'] = sys_on
    gui._save_app_settings()
    gui.dock.system_resident = sys_on
    # ★R297-D 反向同步：设置页「应用」把总闸改了，系统页那枚「一键显示系统图标」
    # 的回显必须跟着走（两页共用一把闸，谁改谁推对侧）——不推的话系统页照样亮着，
    # 用户下一次在那边点一下就把总闸又反着翻回去了。
    _sysic = getattr(gui, '_sysic_var', None)
    if _sysic is not None:
        _sysic.set(bool(sys_on))
    try:
        gui.dock.sync_slots(gui.category_manager.get_all_categories())
    except Exception as e:
        logger.error(f"重建悬浮条失败: {e}")
    gui._replay_active_slot()
    # ★R297-H 收尾链（照 _on_sysic_toggle 那条 sys_panel.py:3360-3375）：
    #   总闸真变了 ⇒ _replay_active_slot 会按新值把那五枚请回来/送走，Explorer 侧
    #   的显隐变化必须走一次 F5 冲刷让视图重建；冲刷会让 Explorer 按自己那份排序
    #   整屏重排（★R234），所以摆位排在冲刷**之后**。两条各自 try——失败只记一行日志，
    #   不许把整次「应用」炸掉。
    if _sys_on_changed:
        try:
            gui.dock._force_listview_refresh()
        except Exception as e:
            logger.warning(f'[系统图标·应用] F5 冲刷失败（显隐已写，等 Explorer 自己刷）: {e}')
        try:
            gui.dock.relayout_after_sysicon_change()
        except Exception as e:
            logger.warning(f'[系统图标·应用] 收尾摆位失败（显隐本身已生效）: {e}')
    resident = [n for n, v in gui._resident_vars.items() if v.get()]
    exclusive = [n for n, v in gui._exclusive_vars.items() if v.get()]
    exsys = [n for n, v in gui._exsys_vars.items() if v.get()]
    gui._update_lists()
    gui._log(f'🤖 公共分类显示已应用：常驻={resident}，只显示自己={exclusive}，'
             f'只显示自己+隐系统={exsys}，系统图标常驻={sys_on}')


# ---------------------------------------------------------------------
# 📌 悬浮窗设置：handlers
# ---------------------------------------------------------------------

def on_tidy_mode_toggle(gui):
    """整理桌面模式：守护暂停，用户可随意大规模拖动图标"""
    v = bool(gui.tidy_mode_var.get())
    gui.icon_manager.desktop_tidy_mode = v
    state = '已开启：布局守护暂停，拖完请取消勾选并重新保存布局' if v else '已关闭：布局守护恢复'
    gui._log(f'🧹 整理桌面模式{state}')


def on_dock_maxslots_change(gui):
    """横条最多显示多少个分类槽"""
    try:
        v = gui.dock_maxslots_var.get()
        gui._app_settings['dock_max_slots'] = 0 if v == '全部' else int(v)
    except Exception:
        return
    gui._save_app_settings()
    gui._apply_app_settings()
    gui._log(f'📌 横条分类槽位上限: {v}')


def on_dock_font_change(gui):
    """横条字体大小：小/中/大/超大，立即生效并记忆"""
    px = DOCK_FONT_SIZES.get(fd_canon(gui.dock_font_var.get()), 16)
    gui._app_settings['dock_font_size'] = px
    gui._save_app_settings()
    gui.dock.set_font_size(px)
    gui.dock.sync_slots(gui.category_manager.get_all_categories())
    gui._log(f'📌 悬浮窗字体已设为 [{gui.dock_font_var.get()}]')


def on_dock_gradient_change(gui):
    """★R280-A：悬浮栏渐变幅度三档（朴素/微渐变/明显）。

    顺序照 ★R226：**先落盘再应用**——万一运行时那一下炸了，用户选的那一档
    也已经在盘上，重启还在；`set_gradient_level` 认不得的档名会回默认档，
    所以下面回显的是"真正生效"的那一档，界面不骗人。
    只重算渐变三色、不推整张主题（否则调个浓淡会把别的配色一起刷了）。"""
    want = gd_canon(gui.dock_gradient_var.get())
    gui._app_settings['dock_gradient_level'] = want
    gui._save_app_settings()
    try:
        eff = gui.dock.set_gradient_level(want)
    except Exception as e:
        logger.warning(f'[设置] 悬浮栏渐变档位应用失败: {e}')
        gui._log(f'📌 渐变档位没能立即生效（重启后正常）: {e}')
        return
    if eff != want:
        gui.dock_gradient_var.set(gd_disp(eff))
    gui._log(f'📌 悬浮栏渐变 = {gui.dock_gradient_var.get()}')


def on_apply_catlayout_toggle(gui):
    """复选项：切分类时是否自动套用同名布局（默认关，手动摆放图标时不被打扰）"""
    v = bool(gui.apply_catlayout_var.get())
    gui._app_settings['apply_cat_layout'] = v
    gui._save_app_settings()
    gui.dock.apply_cat_layout = v
    if v:
        gui._log('📂 已开启：切分类时自动套用同名布局的摆放')
    else:
        gui._log('📂 已关闭：切分类不再自动挪动图标位置')


def on_dock_keep_toggle(gui):
    """关闭主窗口后是否保留悬浮窗"""
    gui._app_settings['close_keep_dock'] = bool(gui.dock_keep_var.get())
    gui._app_settings['close_restore_enabled'] = bool(gui.close_restore_enabled_var.get())
    gui._app_settings['close_restore_layout'] = cr_canon(gui.close_restore_layout_var.get())
    gui._save_app_settings()
    state = '保留悬浮窗后台运行' if gui.dock_keep_var.get() else '一并退出'
    gui._log(f'⚙️ 关闭主窗口时：{state}（横条右端 ✕ 可随时退出）')


def on_close_restore_toggle(gui):
    """退出时恢复布局开关"""
    gui._app_settings['close_restore_enabled'] = bool(gui.close_restore_enabled_var.get())
    gui._app_settings['close_restore_layout'] = cr_canon(gui.close_restore_layout_var.get())
    gui._save_app_settings()
    state = '启用' if gui.close_restore_enabled_var.get() else '禁用'
    gui._log(f'⚙️ 退出时恢复布局已{state}')


def on_close_restore_combo_changed(gui, event=None):
    """退出恢复布局下拉框变更：立即保存"""
    gui._app_settings['close_restore_enabled'] = bool(gui.close_restore_enabled_var.get())
    gui._app_settings['close_restore_layout'] = cr_canon(gui.close_restore_layout_var.get())
    gui._save_app_settings()
    gui._log(f"⚙️ 退出恢复设置已保存: "
             f"{'启用' if gui._app_settings['close_restore_enabled'] else '禁用'}"
             f" / {gui._app_settings['close_restore_layout']}")


def on_apply_layout_toggle(gui):
    """★R241：「启动时自动套用布局」——先落盘，不当场挪桌面。

    这一档改的是**下次启动**的行为（由 `main_window._auto_apply_layout_on_launch`
    读这个键决定），所以勾上时不去动用户当前桌面：立刻摆位会让人以为
    "我只是开了个开关，图标怎么全跑了"。想现在就看到效果，用旁边
    「▶ 套用所选布局」。
    """
    on = bool(gui.apply_layout_var.get())
    gui._app_settings['apply_layout_on_launch'] = on
    gui._save_app_settings()
    gui._log('⚙️ 启动时自动套用布局已'
             + ('开启（下次启动生效）' if on else '关闭'))


def on_sort_clear_fix_now(gui):
    """「🧹 立即修复桌面排序」＝用户主动要修：读桌面 → 弹确认 → 点「是」才动鼠标。

    ★R243（用户 35 批 item3）：结果弹窗**只由 `main_window._sort_clear_notify`
    一处**负责（修好了 / 不用修 / 没修好 三种），这里不再自己弹，
    否则同一件事会弹两遍。不节流：用户自己按的按钮不该被"每会话一次 + 冷却"
    挡住；但确认框（点「是」才发鼠标）这道闸照旧要过。
    ★R245（用户 37 批 item-二-2）：这一枚和「❓ 功能说明」就是本功能的全部
    入口——原来的「桌面排序修复」复选已按用户口径删掉，检测改为常开。
    """
    fn = getattr(gui, 'sort_clear_manual_check', None)
    if not callable(fn):
        gui._log('🧹 暂时读不到桌面视图状态（主窗口还没就绪）')
        return
    try:
        fn()
    except Exception as e:
        gui._log('🧹 修复失败：%s' % type(e).__name__)


def on_sort_clear_help(gui):
    """「❓ 功能说明」：整段大白话只在这个弹窗里，页面上只留一行。

    ★R243：用户口径是"下面的解释太多了、术语太多，严重损害排版和美观"，
    "用户没这个时间去看的，只看效果"⇒ 页面上的说明全删，细节只留在这个弹窗。
    ★R245（用户 37 批）：常开化后没有"先勾开关"这一步了，口径改成
    "点这枚按钮 → 弹窗点『是』"；同时说明做完会**按布局一次性摆回**（A7）。
    ★R249（用户 26 批 一3a-1「还是啰嗦」）：弹窗里再砍两段——
    【它做什么】【它不管的】整段删掉，只留【什么时候用】【你要配合的】。
    "怎么修的"属于实现细节，用户不看效果之外的字；"自动排列那个开关我们不改"
    这件事在**真的因为它摆不住**时由确认框/失败提示当场说（core/sort_clear
    的 `_offer_text` 与失败分支），不预先科普。
    时长不写死：读 core/sort_clear 的节奏常量，改快了文案跟着变。
    """
    try:
        from core import sort_clear as sc
        secs = sc.drag_seconds_text()
    except Exception:
        secs = '3'
    try:
        messagebox.showinfo(
            i18n.tr('桌面排序修复 是干什么的', 'About this fix'),
            i18n.tr(
                '【什么时候用】桌面图标明明排好了，过一会儿或者一刷新就自己乱跑'
                '——直接点「🧹 立即修复桌面排序」，弹窗点「是」。\n\n'
                '【你要配合的】全过程约 %s 秒，这期间请不要动鼠标和键盘，'
                '让它自动完成。做完会当面告诉你「修好了」还是「没修好」。'
                '就算没修好，被拖过的图标也一定放回原位。'
                % secs,
                'Use it when icons you arranged keep scrambling on their own. '
                'Just press "Fix desktop sorting now", then Yes. '
                'Keep your hands off the mouse and keyboard for about %s '
                'seconds. You will be told straight away whether it worked; '
                'if it did not, the icon that was dragged still goes back.'
                % secs),
            parent=gui.root)
    except Exception as e:
        gui._log('🧹 说明弹窗没弹出来：%s' % type(e).__name__)


def on_auto_start_toggle(gui):
    """★R241：「随登录自启动」——先落盘再注册/删除任务，**失败必须回滚**。

    为什么不"只落盘、下次再说"：这一档是往系统里写任务计划程序条目，
    当场就能验成不成。若写失败还留着勾，用户会以为已经自启了（比不勾
    更糟），所以失败时把勾选和设置一起退回原值并明确报错。
    """
    on = bool(gui.auto_start_var.get())
    gui._app_settings['auto_start_logon'] = on
    gui._save_app_settings()
    # 测试态（--selfcheck）只记设置，绝不去真建系统任务
    _mw = sys.modules.get('gui.main_window')
    if _mw is not None and getattr(_mw, 'SELF_CHECK', False):
        gui._log('⚙️ [selfcheck] 随登录自启仅记录，未触碰系统任务')
        return
    try:
        from gui import sys_panel
        ok, msg = sys_panel._apply_selfstart(gui, on)
    except Exception as e:
        ok, msg = False, str(e)
    if not ok:
        gui._app_settings['auto_start_logon'] = not on
        gui._save_app_settings()
        gui.auto_start_var.set(not on)
        gui._log(f'⚙️ 随登录自启设置失败，已回滚: {msg}')
        messagebox.showerror(
            i18n.tr('没能改成功', 'Failed'),
            i18n.tr('登录自启任务%s失败，勾选已退回原状态。\n\n%s'
                    % ('创建' if on else '删除', msg),
                    'Failed to %s the logon task; the checkbox was rolled back.'
                    '\n\n%s' % ('create' if on else 'remove', msg)))
        return
    gui._log(f'⚙️ 随登录自启动已{("开启" if on else "关闭")}：{msg}')


def on_apply_selected_layout(gui):
    """套用所选布局：立即把桌面恢复成下拉框选中的布局。

    ★ 新增功能（2026-09-11）：手动即时应用，与"退出时自动恢复"解耦——
    不依赖关主窗/退出的触发时机，选中布局后点一下就切过去。
    选"当前"则无需套用。
    """
    name = cr_canon((gui.close_restore_layout_var.get() or '').strip())
    if not name or name in ('当前', '窗', '✕'):
        gui._log('请先在下拉框选择一个布局；"当前"表示保持现状，无需套用')
        return
    r = gui._apply_layout_by_name(name)
    if r is None:
        gui._log(f"✗ 找不到布局存档 {name}（icon_backups 下没有 {name}_*.json）")
    else:
        gui._log(f"✓ 已套用布局 [{name}]：匹配 {r.get('matched', '?')} 个图标")


def update_close_restore_combo(gui):
    """更新退出恢复布局下拉列表"""
    try:
        # 始终添加"当前"选项
        unique_names = ['当前']

        # 获取所有已保存的布局（排除以 _ 开头的）
        try:
            layouts = gui.icon_manager.list_layouts()
            layout_names = [lay.get('name', '') for lay in layouts]
        except Exception as e:
            logger.error(f"获取布局列表失败: {e}")
            layout_names = []

        # 添加有存档的分类（排除工具槽和临时存档）
        seen = set(unique_names)
        for name in layout_names:
            # 跳过临时/备份存档
            if name.startswith('_') or name == '当前':
                continue
            # 跳过工具槽
            if name in ('窗', '✕'):
                continue
            if name not in seen:
                unique_names.append(name)
                seen.add(name)

        # 清空当前值，然后设置选项列表（★ R79e：显示层翻译）
        gui.close_restore_layout_var.set('')
        # 直接操作 Combobox 组件设置选项
        gui.close_restore_layout_combo['values'] = [cr_disp(n) for n in unique_names]
        # 恢复上次选择（★ R72：含"当前"——此前刻意不恢复导致清空数据/
        # 布局后下拉显示空白，用户以为设置丢了）
        last = gui._app_settings.get('close_restore_layout', '')
        if last and last in unique_names:
            gui.close_restore_layout_var.set(cr_disp(last))
    except Exception as e:
        logger.error(f"更新退出恢复布局下拉框失败: {e}")


def on_dock_topmost_toggle(gui):
    """悬浮窗置顶开关（★R226 P2-1：以前只改运行时 flag，重启就回到"置顶"）"""
    on = bool(gui.dock_topmost_var.get())
    gui._app_settings['dock_topmost'] = on
    gui._save_app_settings()
    gui.dock.set_always_on_top(on)
    gui._log(f'📌 悬浮窗置顶已{"开启" if on else "关闭"}（关闭时会被其他窗口覆盖）')


def on_ui_font_change(gui):
    """界面字号：小/标准/大 ⇒ 全界面文字等比缩放（★R228 起真能全场生效）。

    ★R228（用户 22 批 item6「感觉换大换小没什么效果」）：原来只 `gui.ui_font`
    重配 size，而全项目 79 处字号是**元组拷贝**（`('Microsoft YaHei UI', 9)`）、
    ttk 样式里 9 处也是元组拷贝 ⇒ 只有 4 个 Listbox 会动。现改为：字号统一经
    `ui_layout.app_font()` 拿命名字体别名，切档＝原地重配这些别名，tk 控件和
    ttk 样式都按引用实时变（实测 tk.Label 26→34px、ttk.Button 36→44px）。
    顺序仍按仓内纪律：先落盘，再推运行时。
    """
    # ★R222 U4：var 存显示文案（English 下是 Small/Standard/Large），
    # 取字号前必须 fd_canon 反查回规范键，否则 English 下永远落到默认 11
    _canon = fd_canon(gui.ui_fontsize_var.get())
    px = UI_FONT_SIZES.get(_canon, 11)
    gui._app_settings['ui_font_size'] = px
    gui._save_app_settings()
    ui_layout.set_ui_font_base(px)     # ★R228：全场等比（命名字体别名重配）
    gui.ui_font.configure(size=px)     # 列表用的那个字体对象（历史口径）
    # 重算窗口尺寸以适配新字号
    gui._reposition_window()
    gui._log(f'🔤 界面字号已设为 [{gui.ui_fontsize_var.get()}]（{px}）')


def on_display_pref_change(gui):
    """★R225 阶段5：界面密度 / 图标风格——落盘 + 全局档立即就位 + 问一次重启。

    能热切的只有"全局档"：icons.MODE、ui_layout.SP、theme_panel.TREE_ROWHEIGHT
    （gui._apply_display_prefs 重算一次），所以此后新建的窗口/对话框/向导立即
    按新档排布。已经建好的主窗改不动——padx 与页签文字都是构建期求值，热改会
    牵动 ui_tab 恢复与向导定位（R73/R215 的老账），故复用 R79d 语言切换那条
    一键重启接力，重启后整窗按新档重建。
    """
    dens = dp_canon(gui.ui_density_var.get())
    ic = ic_canon(gui.ui_icons_var.get())
    if (gui._app_settings.get('ui_density', '标准') == dens
            and gui._app_settings.get('ui_icons', icons.DEFAULT_MODE) == ic):
        return
    gui._app_settings['ui_density'] = dens
    gui._app_settings['ui_icons'] = ic
    gui._save_app_settings()
    gui._apply_display_prefs()
    gui._log(f'🎨 界面显示已设为 [{dens} / {ic}]')
    if messagebox.askyesno(
            i18n.tr('界面显示', 'Interface'),
            i18n.tr('设置已保存。当前窗口按原样显示，重启后整窗生效——现在自动重启吗？',
                    'Saved. The current window keeps its look until restart — '
                    'restart now?'),
            parent=gui.root):
        gui._restart_for_language('显示设置')   # 日志文案用规范中文（日志不分双语）


# ---------------------------------------------------------------------
# UI：悬浮窗卡 + 公共分类卡 + 🧭 向导小节（调用方按顺序 pack；⌨️ 热键卡在
#      hotkey_panel.build_card）
# ---------------------------------------------------------------------

def build_cards(parent, gui, colors):
    """构建设置页"📌 悬浮窗设置"+"🤖 公共分类显示"+"🧭 向导"（R142 自 main_window 迁入）

    ★R216 阶段 2b（只动布局，不动任何行为/回调/变量名）：
    · 卡内按功能区切 4 个彩色小节（显示与热键 / 字体与槽位 / 行为开关 /
      退出时恢复布局），2×2 网格 ⇒ 卡片不再是一串不相干的 run-on pack；
    · 标签+控件一律 form_row 网格 ⇒ 上下行标签右对齐、控件左对齐；
    · 按钮一律 action_bar ⇒ 等宽、左右边缘对齐；
    · 颜色只走 ttk 样式名（Sec.*.TLabel）⇒ 实时换肤自动跟随，
      不依赖 main_window._replace_widget_colors 的旧值映射。
    """
    set_colors(colors)

    # ============ 📌 悬浮窗设置 ============
    dock_frame = ttk.LabelFrame(parent, text=i18n.tr('📌 悬浮窗设置', '📌 Floating Bar Settings'),
                                padding='10', style='Card.TLabelframe')
    dock_frame.pack(fill=tk.X, pady=(0, 6))

    dock_inner = ttk.Frame(dock_frame)
    dock_inner.pack(fill=tk.X)
    dock_inner.columnconfigure(0, weight=1, uniform='dockcol')
    dock_inner.columnconfigure(1, weight=1, uniform='dockcol')

    # ---- 小节①：🎯 显示与热键（primary）----
    g_a, body_a = ui_layout.group_block(
        dock_inner, i18n.tr('🎯 显示与热键', '🎯 Show & Hotkey'), 'primary')
    g_a.grid(row=0, column=0, sticky='nsew', padx=(0, SP['l']), pady=(0, SP['m']))
    fa = ui_layout.form_block(body_a)
    # ★R226（P2-2）：热键显示读真实绑定。原来写死 'Alt+Ctrl+E'，既不会随改键
    # 更新，顺序也和默认 Ctrl+Alt+E 对不上。改键后由 hotkey_panel 同步刷新。
    gui.hotkey_label_var = tk.StringVar(
        value=hotkey_panel.binding_str(gui, 'toggle_dock'))
    ui_layout.form_row(fa, 0, i18n.tr('热键:', 'Hotkey:'),
                       ttk.Label(fa, textvariable=gui.hotkey_label_var,
                                 style='Sec.primary.TLabel'))
    # ★R226（P2-1）：置顶开关持久化——初值取记忆，缺省保持旧行为（置顶）
    gui.dock_topmost_var = tk.BooleanVar(
        value=bool(gui._app_settings.get('dock_topmost', True)))
    ui_layout.form_row(fa, 1, '', ttk.Checkbutton(
        fa, text=i18n.tr('始终置顶', 'Always on Top'),
        variable=gui.dock_topmost_var,
        command=lambda: on_dock_topmost_toggle(gui)))
    # dock 侧 always_on_top 恒以 True 起步，记忆为"关"时启动即推一次
    if not gui.dock_topmost_var.get():
        gui.dock.set_always_on_top(False)
    ui_layout.action_bar(body_a, [
        (i18n.tr('📌 显示/隐藏悬浮窗', '📌 Show/Hide Bar'), gui._toggle_dock,
         'secondary.TButton')])

    # ---- 小节②：🔤 字体与槽位（accent）----
    g_b, body_b = ui_layout.group_block(
        dock_inner, i18n.tr('🔤 字体与槽位', '🔤 Font & Slots'), 'accent')
    g_b.grid(row=0, column=1, sticky='nsew', pady=(0, SP['m']))
    fb = ui_layout.form_block(body_b)
    gui.dock_maxslots_var = tk.StringVar(value='12')
    maxslots_combo = ttk.Combobox(fb, textvariable=gui.dock_maxslots_var,
                                  width=8, state='readonly',
                                  values=['6', '8', '10', '12', '14', '16', '全部'])
    maxslots_combo.bind('<<ComboboxSelected>>', lambda e: on_dock_maxslots_change(gui))
    ui_layout.form_row(fb, 0, i18n.tr('槽位:', 'Slots:'), maxslots_combo)
    gui.dock_font_var = tk.StringVar(value=fd_disp('中'))
    font_combo = ttk.Combobox(fb, textvariable=gui.dock_font_var,
                              width=8, state='readonly',
                              values=[fd_disp(v) for v in ('小', '中', '大', '超大')])
    font_combo.bind('<<ComboboxSelected>>', lambda e: on_dock_font_change(gui))
    ui_layout.form_row(fb, 1, i18n.tr('横条字体:', 'Bar Font:'), font_combo)
    # ★R222 U4：下拉值此前是裸中文（"横条字体"早就走 fd_disp 了，这行漏了）。
    # var 里存**显示文案**，取值时用 fd_canon 反查回规范键（小/标准/大），
    # 与横条字体同一套口径。
    gui.ui_fontsize_var = tk.StringVar(value=fd_disp(DEFAULT_UI_FONT))
    uifont_combo = ttk.Combobox(fb, textvariable=gui.ui_fontsize_var,
                                width=8, state='readonly',
                                values=[fd_disp(v) for v in UI_FONT_SIZES])
    uifont_combo.bind('<<ComboboxSelected>>', lambda e: on_ui_font_change(gui))
    # ★R228（用户 22 批 item6「"界面字体"指什么？」）：这一档管的只是**字号**
    # （字体族固定雅黑），名字却叫"字体"，和上面"横条字体"混在一起看不出区别
    # ⇒ 改名"界面字号"，并补一行说清它管哪片、和横条字体怎么分工。
    ui_layout.form_row(fb, 2, i18n.tr('界面字号:', 'UI Text Size:'),
                       uifont_combo)
    # ★R280-A（用户 06 批 item1「④：不明显」）：渐变浓淡做成三档、就放在同一张
    # 悬浮栏卡里。档位表读 core/dock.GRADIENT_LEVELS（渲染侧唯一出处），
    # 这里不另抄一份名字——以后加一档，下拉自己多一格。
    gui.dock_gradient_var = tk.StringVar(value=gd_disp(GRAD_DEFAULT_LEVEL))
    grad_combo = ttk.Combobox(fb, textvariable=gui.dock_gradient_var,
                              width=8, state='readonly',
                              values=[gd_disp(v) for v in GRADIENT_LEVELS])
    grad_combo.bind('<<ComboboxSelected>>',
                    lambda e: on_dock_gradient_change(gui))
    ui_layout.form_row(fb, 3, i18n.tr('悬浮栏渐变:', 'Dock shading:'),
                       grad_combo)
    ttk.Label(body_b, text=i18n.tr(
        '小/标准/大 = 主窗口所有文字的大小（页签、按钮、列表、说明一起等比缩放，'
        '改档立即生效）；只管大小，字体族仍是微软雅黑。\n'
        '"横条字体"只管屏幕顶部那条悬浮栏，两者互不影响。\n'
        '"悬浮栏渐变"= 那条悬浮栏底色上浅下深的浓淡：默认「明显」，'
        '嫌重可以退到「微渐变」或「朴素」，只改浓淡、不动别的配色。',
        'Small / Standard / Large = text size across the main window (tabs, '
        'buttons, lists and notes scale together, applied instantly); the font '
        'family itself stays Microsoft YaHei UI.\n'
        '"Bar Font" only sizes the floating dock bar at the top of the screen.\n'
        '"Dock shading" = how strongly that bar fades from light to dark. '
        'Default is "Strong"; pick "Subtle" or "Flat" if it feels too heavy. '
        'It only changes the fade, no other colour.'),
        foreground=_colors['text_secondary'], wraplength=420, justify='left',
        font=ui_layout.app_font(9)).pack(anchor=tk.W, pady=(SP['xs'], 0))

    # ---- 小节③：🧲 行为开关（warning）----
    g_c, body_c = ui_layout.group_block(
        dock_inner, i18n.tr('🧲 行为开关', '🧲 Behaviour'), 'warning')
    g_c.grid(row=1, column=0, sticky='nsew', padx=(0, SP['l']))
    fc = ui_layout.form_block(body_c)
    gui.apply_catlayout_var = tk.BooleanVar(value=True)
    ui_layout.form_row(fc, 0, '', ttk.Checkbutton(
        fc, text=i18n.tr('📂 切分类套用同名布局',
                         '📂 Apply same-name layout on switch'),
        variable=gui.apply_catlayout_var,
        command=lambda: on_apply_catlayout_toggle(gui)))
    gui.dock_keep_var = tk.BooleanVar(value=False)
    # ★ 用户拍板（2026-09-12）：文案"保留横条后台"→"保留悬浮框"，并用
    #   强调色（Accent.TCheckbutton，随主题切换换色）区别于普通勾选项
    ui_layout.form_row(fc, 1, i18n.tr('关主窗口:', 'Main window close:'),
                       ttk.Checkbutton(
                           fc, text=i18n.tr('保留悬浮框', 'Keep floating bar'),
                           style='Accent.TCheckbutton',
                           variable=gui.dock_keep_var,
                           command=lambda: on_dock_keep_toggle(gui)))
    gui.tidy_mode_var = tk.BooleanVar(value=False)
    # ★R216：文案拆成"短勾选项 + 下行小字说明"——ttk.Checkbutton 在 Tk 8.6
    # 没有 -wraplength（整句长文案会把小节撑破），长解释改走 ttk.Label（支持
    # wraplength），与原意一致
    ui_layout.form_row(fc, 2, '', ttk.Checkbutton(
        fc, text=i18n.tr('🧹 整理桌面模式', '🧹 Tidy Mode'),
        variable=gui.tidy_mode_var,
        command=lambda: on_tidy_mode_toggle(gui)))
    ttk.Label(body_c, text=i18n.tr(
        '大规模拖动图标时勾上，布局守护暂停；拖完取消并重新"保存布局"',
        'Check before mass-dragging icons (layout guard pauses); '
        'uncheck and re-"Save Layout" when done'),
        foreground=_colors['text_secondary'], wraplength=420, justify='left',
        font=ui_layout.app_font(9)).pack(anchor=tk.W, pady=(SP['xs'], 0))
    # 界面主题入口已移至标题栏（所有页签可见）

    # ---- 小节④：💾 退出时恢复布局（secondary）----
    g_d, body_d = ui_layout.group_block(
        dock_inner, i18n.tr('💾 退出时恢复布局', '💾 Restore Layout On Exit'),
        'secondary')
    g_d.grid(row=1, column=1, sticky='nsew')
    fd = ui_layout.form_block(body_d)
    gui.close_restore_enabled_var = tk.BooleanVar(value=False)
    ui_layout.form_row(fd, 0, '', ttk.Checkbutton(
        fd, text=i18n.tr('退出时恢复指定布局', 'Restore layout on exit'),
        variable=gui.close_restore_enabled_var,
        command=lambda: on_close_restore_toggle(gui)))
    gui.close_restore_layout_var = tk.StringVar(value='')
    # ★ 改选择立即保存（2026-09-02 修复：此前只在勾选开关时保存，
    #   单改下拉框的值重启后会丢——用户报"设置不能保存"）
    gui.close_restore_layout_combo = ttk.Combobox(
        fd, textvariable=gui.close_restore_layout_var,
        width=26, state='readonly',
        postcommand=lambda: update_close_restore_combo(gui))
    gui.close_restore_layout_combo.bind(
        '<<ComboboxSelected>>', lambda e: on_close_restore_combo_changed(gui))
    ui_layout.form_row(fd, 1, i18n.tr('布局:', 'Layout:'),
                       gui.close_restore_layout_combo)
    # ★R241：两枚"用户自主决定"的复选（默认全关，不擅自改用户机器）。
    #   ① 启动时自动套用布局——解决"重启后图标乱、要开一下软件才对"：
    #      退出恢复选"当前"时只恢复显隐不摆位置，这里补一次摆位。
    #   ② 随登录自启动——走任务计划程序（可原生延时、列表可见可删），
    #      不走注册表 Run，勾/取消都当场注册/删除并回读核对。
    gui.apply_layout_var = tk.BooleanVar(
        value=bool(gui._app_settings.get('apply_layout_on_launch', False)))
    ui_layout.form_row(fd, 2, '', ttk.Checkbutton(
        fd, text=i18n.tr('启动时自动套用布局（软件一开就自己挪图标位置）',
                         'Auto-apply layout at startup'),
        variable=gui.apply_layout_var,
        command=lambda: on_apply_layout_toggle(gui)))
    gui.auto_start_var = tk.BooleanVar(
        value=bool(gui._app_settings.get('auto_start_logon', False)))
    ui_layout.form_row(fd, 3, '', ttk.Checkbutton(
        fd, text=i18n.tr('随登录自启动本软件', 'Start DeskBits at logon'),
        variable=gui.auto_start_var,
        command=lambda: on_auto_start_toggle(gui)))
    # ★R245（用户 37 批 item-二-3）：这两枚复选**不再写页面说明**——用户原话
    #   "启动自动套用和登陆自启动软件不需要说明"。行为本身一个字没改，
    #   只是把六行灰字删掉（口径：默认都不勾、改了下次启动生效，
    #   自启动走任务计划程序且可取消，都写在 main_window 的注释里）。
    # ★ 立即应用所选布局：手动把桌面切到下拉框选中的布局，直观可控
    #   与"退出时自动恢复"解耦——不依赖关主窗/退出的触发时机
    ui_layout.action_bar(body_d, [
        (i18n.tr('▶ 套用所选布局', '▶ Apply Selected Layout'),
         lambda: on_apply_selected_layout(gui), 'secondary.TButton')], cols=1)
    # ★R242 #140 落地、★R243 改版、★R245（用户 37 批 item-二-2）常开化：
    #   原「🧹 桌面排序修复」复选**按用户口径删除**——检测改为常开，
    #   这一枚「🧹 立即修复桌面排序」+「❓ 功能说明」就是全部入口。
    #   红线一条没松：必须用户点「是」才注入鼠标、每会话主动提≤1 次 + 冷却、
    #   成功判据＝COM 读回为空、绝不写别的进程内存、绝不进悬浮条回调。
    #   ★R243 教训保留：这里不要再往 body_d 里插 form_block——grid 行永远
    #   排在后 pack 的按钮之上，会把"说明"和"按钮"拆开（旧 item3 的观感）。
    # 两枚按钮同一功能的一套（一个动手、一个解释），所以并排；
    # 上面「▶ 套用所选布局」语义无关，单独一排。
    ui_layout.action_bar(body_d, [
        (i18n.tr('🧹 立即修复桌面排序', 'Fix desktop sorting now'),
         lambda: on_sort_clear_fix_now(gui), 'secondary.TButton'),
        (i18n.tr('❓ 功能说明', 'What does this do?'),
         lambda: on_sort_clear_help(gui), 'secondary.TButton')], cols=2)

    # ---- 🖥 界面显示：🎚 密度 + 🔣 页签图标（★R225 美化阶段 5）----
    # 单开一张卡而不是塞进"字体与槽位"：这两档改完要重启，与卡内其余
    # "立即生效"的项混在一起会被误认为无效（用户反馈口径）。
    disp_frame = ttk.LabelFrame(
        parent, text=i18n.tr('🖥 界面显示（这两档改完需重启生效）',
                             '🖥 Interface (these two apply after restart)'),
        padding='10', style='Card.TLabelframe')
    disp_frame.pack(fill=tk.X, pady=(0, 6))
    df = ui_layout.form_block(disp_frame)
    _dens0 = gui._app_settings.get('ui_density', '标准')
    gui.ui_density_var = tk.StringVar(value=dp_disp(_dens0))
    density_combo = ttk.Combobox(df, textvariable=gui.ui_density_var,
                                 width=8, state='readonly',
                                 values=[dp_disp(v) for v in UI_DENSITIES])
    density_combo.bind('<<ComboboxSelected>>',
                       lambda e: on_display_pref_change(gui))
    ui_layout.form_row(df, 0, i18n.tr('界面密度:', 'Density:'), density_combo)
    _ic0 = gui._app_settings.get('ui_icons', icons.DEFAULT_MODE)
    gui.ui_icons_var = tk.StringVar(value=ic_disp(_ic0))
    icons_combo = ttk.Combobox(df, textvariable=gui.ui_icons_var,
                               width=12, state='readonly',
                               values=[ic_disp(v) for v in icons.MODES])
    icons_combo.bind('<<ComboboxSelected>>',
                     lambda e: on_display_pref_change(gui))
    ui_layout.form_row(df, 1, i18n.tr('图标风格:', 'Icons:'), icons_combo)
    ttk.Label(disp_frame, text=i18n.tr(
        '密度＝控件间距与表格行高一起缩放（紧凑 0.8×／标准 1×／宽松 1.25×）；'
        '图标风格＝页签前与标题栏那几枚图标，"图标字体"用系统 Segoe MDL2，'
        '与中文字基线更齐，"Emoji 图标"是原来的彩色表情（悬浮条不受影响）。',
        'Density scales padding and table row height (0.8x / 1x / 1.25x); '
        'Icons switches the leading glyph on the tabs and the title bar — '
        'system Segoe MDL2 aligns better with Chinese text, Emoji keeps the '
        'original colour icons (the floating bar is unaffected).'),
        foreground=_colors['text_secondary'], wraplength=520, justify='left',
        font=ui_layout.app_font(9)).pack(anchor=tk.W, pady=(SP['xs'], 0))

    # ---- 🎨 程序图标（★R266 批21 item1：默认 B，六套全部随包、都能换）----
    _build_icon_card(parent, gui)

    # ---- 🤖 公共分类显示：常驻 / 只显示自己 / 只显示自己+隐系统（三列网格）----
    sticky_frame = ttk.LabelFrame(parent, text=i18n.tr('🤖 公共分类显示（每行三选一或不选；勾选后点"✅ 应用"生效）', '🤖 Category Display Matrix (one per row or none; click "✅ Apply" to take effect)'),
                                  padding='10', style='Card.TLabelframe')
    sticky_frame.pack(fill=tk.X, pady=(0, 6))

    # 列色：每列统一颜色（青/白/橙），列色即身份
    # 表头与勾选项放同一个 grid（row 0=表头），列宽自动对齐
    # 三色取自主题键（深色主题与旧硬编码值一致，浅色/护眼自动换深色）
    chips = ttk.Frame(sticky_frame)
    chips.pack(fill=tk.X, pady=(2, 4))
    for _ci in range(3):
        chips.columnconfigure(_ci, weight=1, uniform='stky')
    # 保存容器引用 + 初始化空字典，统一交给 rebuild_sticky_grid 填充
    gui._sticky_chips = chips
    gui._resident_vars = {}
    gui._resident_btns = {}
    gui._exclusive_vars = {}
    gui._exclusive_btns = {}
    gui._exsys_vars = {}
    gui._exsys_btns = {}
    rebuild_sticky_grid(gui)

    ui_layout.action_bar(sticky_frame, [
        (i18n.tr('✅ 应用（勾选后点这里生效）', '✅ Apply (click to take effect)'),
         lambda: on_sticky_apply(gui), 'primary.TButton')])
    # ★ R183（用户 15 批"以后不再显示向导应该是个复选项"）：设置页直接
    # 给复选框。勾上=启动自动弹向导（同时清 wizard_done/wizard_disabled）；
    # 取消勾选=不再自动弹。"🧭 重看向导"按钮保持随时可手动打开。
    _wiz_var = tk.BooleanVar(
        value=not bool(gui._app_settings.get('wizard_disabled')))

    def _toggle_wizard():
        if _wiz_var.get():
            gui._app_settings.pop('wizard_disabled', None)
            gui._app_settings.pop('wizard_done', None)
        else:
            gui._app_settings['wizard_disabled'] = True
        gui._save_app_settings()

    ttk.Label(sticky_frame,
              text=i18n.tr('每行最多勾一列；"只显示自己+隐系统"=点该分类时连系统图标都隐藏。'
                           '三列都不选=普通分类。勾选过程不生效，按"✅ 应用"才切换。',
                           'One column per row max; "Only itself + hide sys" also hides '
                           'system icons when switching to that category. No selection = '
                           'normal category. Changes apply only after clicking "✅ Apply".'),
              foreground=_colors['text_secondary'],
              font=ui_layout.app_font(9)).pack(anchor=tk.W, pady=(SP['xs'], 0))

    # ---- 🧭 向导（独立小节，不再零散挂在页面底部）----
    g_w, body_w = ui_layout.group_block(
        parent, i18n.tr('🧭 启动向导', '🧭 Startup Wizard'), 'text_secondary')
    g_w.pack(fill=tk.X, pady=(0, 6))
    ttk.Checkbutton(body_w, text=i18n.tr('启动时显示向导',
                                         'Show wizard on startup'),
                    variable=_wiz_var, command=_toggle_wizard
                    ).pack(anchor=tk.W)
    # ★ R160：重看向导入口（R125 定案=设置页）
    ui_layout.action_bar(body_w, [
        (i18n.tr('🧭 重看向导', '🧭 Replay Wizard'),
         lambda: __import__('gui.wizard', fromlist=['x']).show(gui, colors),
         'secondary.TButton')])


# ==================== ★R266（批21 item1）🎨 程序图标 ====================
# 用户口径："图标选B和F，然后存档……客户可以右键修改图标，直接就定位图标文件，
# 到时候可以改。默认B，其他的图标都存里面吧，都可以改，B，F排第一第二。"
# 落点：六套 .ico 全随包（images/app_icons/，文件名前缀 01_B、02_F＝显示序），
# 卡上给缩略图选择器 + 两枚按钮（定位当前图标文件 / 恢复默认）。
# exe 文件自身的图标资源是编译期嵌死的，运行时改不了——所以这一节改的是
# **窗口/任务栏**图标，并把 ico 文件"定位"到用户眼前，让他走 Windows 自带的
# "右键快捷方式→属性→更改图标"去换快捷方式那一枚。

def _app_dir():
    from utils.app_paths import get_app_dir
    return get_app_dir()


def _refresh_icon_cards(gui):
    """把六张缩略图上的"当前选中"高亮刷成盘上的真值。"""
    from core import app_icon
    chips = getattr(gui, '_icon_chips', None)
    if not chips:
        return
    try:
        cur = app_icon.resolve(_app_dir(), gui._app_settings)
        cur_name = cur.name if cur is not None else ''
    except Exception:
        cur_name = ''
    for stem, chip, cell in chips:
        on = (stem == cur_name)
        try:
            chip.configure(
                highlightbackground=_colors['primary'] if on
                else _colors['border'],
                highlightthickness=2 if on else 1)
            cell.configure(bg=_colors['surface'] if on else _colors['bg'])
        except Exception:
            pass


def on_app_icon_pick(gui, stem, name=''):
    """选一套：先落盘再应用（★R226 顺序纪律）。"""
    from core import app_icon
    try:
        gui._app_settings[app_icon.SETTING_KEY] = stem
        gui._save_app_settings()
        gui._apply_app_icon()
        _refresh_icon_cards(gui)
        gui._log(f'🎨 程序图标已切到「{name or stem}」（窗口/任务栏立即生效）')
    except Exception as e:
        logger.warning(f'切换程序图标失败: {e}')


def on_app_icon_reset(gui):
    from core import app_icon
    try:
        gui._app_settings.pop(app_icon.SETTING_KEY, None)
        gui._save_app_settings()
        gui._apply_app_icon()
        _refresh_icon_cards(gui)
        gui._log('🎨 程序图标已恢复默认（01_B 拾碎成序）')
    except Exception as e:
        logger.warning(f'恢复默认程序图标失败: {e}')


def on_app_icon_reveal(gui):
    """★ 用户点名的"直接就定位图标文件"：开资源管理器并把当前 ico 选中。"""
    from core import app_icon
    try:
        cur = app_icon.resolve(_app_dir(), gui._app_settings)
        if cur is None:
            messagebox.showwarning(
                i18n.tr('没有图标文件', 'No Icon File'),
                i18n.tr('没找到随包的图标文件夹 images/app_icons，'
                        '请确认安装包解压完整。',
                        'The bundled icon folder images/app_icons is missing - '
                        'check that the package was extracted completely.'),
                parent=gui.root)
            return
        if not app_icon.reveal_in_explorer(cur):
            messagebox.showwarning(
                i18n.tr('打不开文件夹', 'Cannot Open Folder'),
                i18n.tr('资源管理器没能打开这个路径：' + str(cur),
                        'Explorer could not open this path: ' + str(cur)),
                parent=gui.root)
            return
        gui._log(f'🎨 已定位图标文件：{cur}')
    except Exception as e:
        logger.warning(f'定位图标文件失败: {e}')


def _build_icon_card(parent, gui):
    """🎨 程序图标卡：六套缩略图（B 第一、F 第二）+ 定位/恢复默认。"""
    from core import app_icon
    frame = ttk.LabelFrame(
        parent, text=i18n.tr('🎨 程序图标（默认：B 拾碎成序）',
                             '🎨 Program Icon (default: B)'),
        padding='10', style='Card.TLabelframe')
    frame.pack(fill=tk.X, pady=(0, 6))
    row = ttk.Frame(frame)
    row.pack(fill=tk.X)
    items = app_icon.list_icons(_app_dir())
    gui._icon_chips = []
    gui._icon_imgs = []
    if not items:
        ttk.Label(row, text=i18n.tr(
            '这台机器上没找到随包图标文件夹 images/app_icons，'
            '程序图标沿用打包进 exe 的那一枚。',
            'The bundled icon folder images/app_icons is missing here - the '
            'app keeps the icon compiled into the exe.'),
            foreground=_colors['text_secondary'],
            font=ui_layout.app_font(9)).pack(anchor=tk.W)
        return
    px = 44
    for it in items:
        cell = tk.Frame(row, bg=_colors['bg'])
        cell.pack(side=tk.LEFT, padx=(0, 10))
        img = None
        try:
            from PIL import Image, ImageTk
            src = str(it['png'] if it['png'] is not None else it['ico'])
            pim = Image.open(src).convert('RGBA').resize((px, px),
                                                         Image.LANCZOS)
            img = ImageTk.PhotoImage(pim)
            gui._icon_imgs.append(img)
        except Exception as e:
            logger.warning(f'图标缩略图生成失败（退回文件名）: {it["stem"]} {e}')
        chip = tk.Label(cell, image=img if img else '',
                        width=px, height=px, bd=0, bg=_colors['bg'],
                        cursor='hand2', text='' if img else it['name'][:6])
        chip.pack()
        gui._icon_chips.append((it['stem'], chip, cell))
        lb = tk.Label(cell, text=it['name'], bg=_colors['bg'],
                      fg=_colors['fg'], font=ui_layout.app_font(9))
        lb.pack()
        for w in (chip, lb):
            w.bind('<Button-1>',
                   lambda e, s=it['stem'], n=it['name']: on_app_icon_pick(
                       gui, s, n))
    ttk.Label(frame, text=i18n.tr(
        '点缩略图就换：窗口和任务栏立即生效，下次启动还是它。' + chr(10) +
        '想换「桌面快捷方式」那一枚：右键快捷方式 → 属性 → 更改图标，'
        '按下面按钮定位到 ico 文件选中即可。',
        'Click a thumbnail to switch - the window and taskbar update at once '
        'and stay after restart.' + chr(10) +
        'For the desktop shortcut icon: right-click it, Properties, Change '
        'Icon, and use the button below to land on the .ico file.'),
        foreground=_colors['text_secondary'], wraplength=560, justify='left',
        font=ui_layout.app_font(9)).pack(anchor=tk.W, pady=(SP['xs'], SP['s']))
    ui_layout.action_bar(frame, [
        (i18n.tr('📂 打开图标文件夹（选中当前图标）', '📂 Open Icon Folder'),
         lambda: on_app_icon_reveal(gui), 'secondary.TButton'),
        (i18n.tr('↺ 恢复默认图标', '↺ Reset Icon'),
         lambda: on_app_icon_reset(gui), 'secondary.TButton')], cols=2)
    _refresh_icon_cards(gui)

