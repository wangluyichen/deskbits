# -*- coding: utf-8 -*-
"""📍 规则页·当前分类快捷高亮（★ R65-Bug7，自 main_window 分文件）。

用户反馈：核对一个分类的高亮框，要切到"管理"页点选分类再回规则页
点"🖍️ 高亮选中分类"，太绕。本模块在规则页高亮框区加"当前分类"下拉框，
选中即高亮——与"🖍️ 高亮选中分类"同一语义：单分类核对，不含常驻叠加。"""
import logging
from tkinter import ttk, messagebox

from core.highlighter import PRESET_COLORS, _hex_to_rgb
# ★R204 GUIB-12：_hl_t 延迟导入（函数内）——rule_quickpick 在 main_window
# 中先于 rule_panel 加载，模块级导入会成环（test_r194_exit 实锤）

logger = logging.getLogger("desktop_manager")


def refresh_values(gui):
    """postcommand：每次展开下拉时重读分类名，新建/导入的分类立即可见"""
    try:
        names = [c.get('name', '') for c in
                 gui.category_manager.get_all_categories()]
        gui._hl_quickpick_combo.configure(values=names)
    except Exception as e:
        logger.error(f"当前分类下拉刷新失败: {e}")


def on_selected(gui):
    name = (gui.hl_quickpick_var.get() or '').strip()
    if name:
        highlight_category_by_name(gui, name)


def step(gui, delta):
    """★ R67：不展开下拉的快速循环切换（↑/↓ 方向键、鼠标中键）。

    下一个/上一个分类循环切换并立即高亮，方便对照桌面方框逐个核对。"""
    try:
        refresh_values(gui)
        vals = list(gui._hl_quickpick_combo['values'] or ())
        if not vals:
            return
        cur = (gui.hl_quickpick_var.get() or '').strip()
        try:
            i = vals.index(cur)
        except ValueError:
            i = 0 if delta > 0 else len(vals) - 1
        gui.hl_quickpick_var.set(vals[(i + delta) % len(vals)])
        on_selected(gui)
    except Exception as e:
        logger.error(f"快速切换分类失败: {e}")


def highlight_category_by_name(gui, cat_name):
    """按分类名高亮其全部图标（原 main_window._on_hl_category 逻辑迁入）"""
    # 渐变色模式：每个图标用自身所属分类色（多分类取第一个）
    if gui.highlighter._gradient_mode and gui.category_manager:
        color_map = {}
        icon_cats = {}
        for cat in gui.category_manager.categories:
            for icon in cat.get("icons", []):
                icon_cats.setdefault(icon, []).append(cat["name"])
        for icon_name in gui.category_manager.get_category_icons(cat_name):
            cats = icon_cats.get(icon_name, [])
            real_cats = [c for c in cats if c != '其他']
            if real_cats:
                color_map[icon_name] = _hex_to_rgb(
                    gui.category_manager.get_category_color(real_cats[0]))
            else:
                color_map[icon_name] = gui.highlighter._color
        gui.highlighter.set_color_map(color_map)
    names = gui.highlighter.highlight_category(gui.category_manager, cat_name)
    if not names:
        messagebox.showinfo('提示',
                            f'分类 [{cat_name}] 还没有图标。\n'
                            '请点击"🖱️ 拖拽分类"先把图标加入分类。',
                            parent=gui.root)
        return
    # ★ 语义（2026-09-12 用户拍板）：单分类核对工具，只圈该分类自己的
    # 图标；同步 UI 变量为分类颜色
    color = gui.category_manager.get_category_color(cat_name)
    from gui.rule_panel import _hl_t   # ★R204 GUIB-12：延迟导入防循环
    for name, value in PRESET_COLORS.items():
        if value.lower() == color.lower():
            gui.hl_color_var.set(_hl_t(f'{name} {value}'))
            break
    gui._save_highlight_settings()
    gui._log(f'🖍️ 已高亮 [{cat_name}] 的 {len(names)} 个图标')
