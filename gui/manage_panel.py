# -*- coding: utf-8 -*-
"""📁 管理页面板（R144 自 gui/main_window.py 拆出，第三刀·Mixin 方案）。

ManageMixin = 管理页全部操作方法（保存/恢复/显示隐藏/分类管理/布局管理/
悬浮条配色/拖拽入口/修缓存）+ _build_manage_tab（管理页 UI 构建），
由 DesktopManager 继承——**self. 语义零转换**，data_manage_page /
hotkey_panel 等外部 gui._xxx() 调用方自然兼容（_on_show_all/_on_clear_icon_data
无需薄委托）。
纪律：**严禁 import gui.main_window**（模块双身份）。
"""
import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

from typing import List, Optional

from drag_drop import open_drag_drop_panel   # 拖拽分类面板（实现在 drag_drop.py）
from gui import ui_layout
from gui.ui_layout import SP
from utils.app_paths import get_app_dir as _get_app_dir

APP_DIR = _get_app_dir()   # ★ R144：布局 zip 导出的默认目录基准
from utils import i18n

logger = logging.getLogger("desktop_manager")

# 主题色引用：main_window 启动时 set_colors(COLORS) 注入**同一 dict 对象**
# （apply_ui_theme 原地换色自动跟随）
COLORS = {'bg': '#282a36', 'fg': '#f8f8f2', 'primary': '#8be9fd',
          'surface': '#44475a', 'border': '#636d83', 'warning': '#ffb86c',
          'muted': '#6272a4', 'secondary': '#bd93f9', 'error': '#ff5555',
          # ★R221（NEW7-P1-3）：本文件 :1013/:1043 直取 COLORS['text_secondary']，
          # R215 改名时漏补这张兜底表——set_colors 注入前构建即 KeyError（与
          # quick_find/drag_drop 的 P0 同源，只是这里注入总在构建前发生所以没炸）
          'text_secondary': '#bcc3d6'}


def set_colors(colors: dict):
    global COLORS
    COLORS = colors


# 显示层 helper（实现在 settings_panel，R142 起是唯一真身）
from gui.settings_panel import cr_disp as _cr_disp, cr_canon as _cr_canon


def ask_category_color(parent):
    """★ R79：分类颜色色板选择器（替代旧版手输 hex 的 askstring）。

    旧版默认值固定 #8be9fd，连续新建分类一路回车 → 全部同色，渐变色
    （按分类色）模式看起来"失效"（2026-09-16 用户实测）。
    色板点选 + 系统调色板自定义双通道。取消返回 None。"""
    dlg = tk.Toplevel(parent)
    dlg.title(i18n.tr('选择分类颜色', 'Pick a Category Color'))
    dlg.resizable(False, False)
    dlg.configure(bg=COLORS['bg'])
    dlg.transient(parent)
    ttk.Label(dlg,
              text=i18n.tr('点选分类颜色（悬浮条色条 / 渐变色框都用它）：',
                           'Pick a color for this category (dock bar / gradient frames):'),
              background=COLORS['bg'], foreground=COLORS['fg']
              ).pack(padx=14, pady=(12, 8))
    palette = ['#8be9fd', '#50fa7b', '#ff79c1', '#ffb86c', '#bd93f9', '#ff5555',
               '#f1fa8c', '#a78bfa', '#2196f3', '#ffb6c1', '#00ced1', '#8bc34a']
    holder = {'color': None}
    grid = ttk.Frame(dlg)
    grid.pack(padx=14)
    for i, c in enumerate(palette):
        def _pick(cc=c):
            holder['color'] = cc
            dlg.destroy()
        tk.Button(grid, bg=c, fg='#111111', text='██', relief=tk.FLAT, bd=0,
                  activebackground=c, command=_pick, cursor='hand2',
                  font=ui_layout.app_font(9, 'bold')
                  ).grid(row=i // 6, column=i % 6, padx=3, pady=3, sticky='nsew')
    row = ttk.Frame(dlg)
    row.pack(pady=(8, 12))

    def _custom():
        from tkinter import colorchooser
        _rgb, hexstr = colorchooser.askcolor(
            color='#8be9fd', title=i18n.tr('自定义颜色', 'Custom Color'),
            parent=dlg)
        if hexstr:
            holder['color'] = hexstr
            dlg.destroy()

    ttk.Button(row, text=i18n.tr('🎨 自定义…', '🎨 Custom…'),
               command=_custom).pack(side=tk.LEFT, padx=5)
    ttk.Button(row, text=i18n.tr('取消', 'Cancel'),
               command=dlg.destroy).pack(side=tk.LEFT, padx=5)
    dlg.update_idletasks()
    try:
        w, h = dlg.winfo_width(), dlg.winfo_height()
        x = parent.winfo_x() + (parent.winfo_width() - w) // 2
        y = parent.winfo_y() + (parent.winfo_height() - h) // 3
        dlg.geometry(f'+{max(x, 0)}+{max(y, 0)}')
    except Exception:
        pass
    dlg.grab_set()
    parent.wait_window(dlg)
    return holder['color']




class ManageMixin:
    def _on_cat_sash_release(self, event=None):
        """拖动分类面板分隔条结束：记住左栏宽度（下次启动恢复）"""
        try:
            x = self.cat_pane.sash_coord(0)[0]
            self._app_settings['cat_pane_width'] = int(x)
            self._save_app_settings()
        except Exception:
            pass

    def _on_cleanup_stale_icons(self):
        """清理所有分类中桌面上已不存在的图标名（改名/删文件后的死条目）

        ★R215（NEW6-G1）：本功能的"真值"是桌面**实时**读数，而
        icon_manager.get_current_icon_positions() 在 Explorer 重启/桌面繁忙/
        句柄失效时会返回空 dict 或残缺集（源码里就有 return {} 的路径）。
        旧实现拿它直接当"桌面存在的图标"：空集＝所有分类条目全被判失效，
        并且**边判边逐条落盘**＝一次点击毁掉全部归类关系、不可逆。
        现在：① 先只算不删（dry-run 计划表），② 空读数直接中止，
        ③ 失效占比 >50% 视为读数异常中止（正常"改名/删文件"是零星几个），
        ④ 弹确认展示将清理的总数与明细，确认后才落盘。"""
        try:
            desktop_names = set(self.icon_manager.get_current_icon_positions().keys())
        except Exception as e:
            messagebox.showerror('错误', f'读取桌面图标失败: {e}', parent=self.root)
            return
        if not desktop_names:
            messagebox.showwarning(
                i18n.tr('清理失效', 'Clean Stale'),
                i18n.tr('桌面图标读数失败（0 个），已中止清理。\n'
                        '请稍后重试（Explorer 正在重启时会出现）。',
                        'Desktop reading returned 0 icons — cleanup aborted.\n'
                        'Try again later (happens while Explorer restarts).'),
                parent=self.root)
            return
        cats = self.category_manager.get_all_categories()
        total_named = sum(len(c.get('icons') or []) for c in cats)
        plan = []                       # [(分类名, [失效图标名...])]
        for cat in cats:
            stale = [n for n in cat.get('icons', []) if n not in desktop_names]
            if stale:
                plan.append((cat['name'], stale))
        total_removed = sum(len(s) for _, s in plan)
        if not total_removed:
            messagebox.showinfo(i18n.tr('清理失效', 'Clean Stale'),
                                i18n.tr('所有分类都很干净，没有失效的图标名',
                                        'All categories are clean, no stale names'),
                                parent=self.root)
            return
        if total_named and total_removed * 2 > total_named:
            messagebox.showwarning(
                i18n.tr('清理失效', 'Clean Stale'),
                i18n.tr(f'桌面读数异常：{total_removed}/{total_named} 个条目'
                        f'在桌面上都找不到（占比超过 50%），已中止。\n'
                        f'若桌面图标确实被大量删除，请确认后再清一次。',
                        f'Suspicious reading: {total_removed}/{total_named} entries '
                        f'missing on desktop (>50%), aborted.\n'
                        f'If you really removed that many icons, run it again.'),
                parent=self.root)
            return
        detail_parts = [f"[{n}] {len(s)} 个" for n, s in plan]
        detail = '; '.join(detail_parts)
        preview = detail if len(plan) <= 6 else \
            '; '.join(detail_parts[:6]) + \
            f' …{i18n.tr("等共", "etc, total")} {len(plan)} ' + \
            i18n.tr('个分类', 'categories')
        if not messagebox.askyesno(
                i18n.tr('确认清理失效', 'Confirm Clean Stale'),
                i18n.tr(f'将从 {len(plan)} 个分类中清理 {total_removed} 个'
                        f'桌面上已不存在的图标名：\n{preview}\n\n'
                        f'（只清理分类记录里的死条目，不删除桌面文件）\n确认继续？',
                        f'Remove {total_removed} stale icon names from '
                        f'{len(plan)} categories:\n{preview}\n\n'
                        f'(Only dead entries in category records; desktop files '
                        f'are untouched.)\nContinue?'),
                parent=self.root):
            return
        for name, stale in plan:
            for n in stale:
                self.category_manager.remove_icon_from_category(name, n)
        self._update_lists()
        # 日志前缀哨兵 '🧹 已清理' 保持原样（不改写既有 _log 文案口径）
        self._log(f'🧹 已清理 {total_removed} 个失效图标名: {detail}')

    def _on_delete_category(self):
        """删除分类（支持多选批量）"""
        names = self._selected_category_names()
        if not names:
            messagebox.showwarning('提示', '请先选择分类', parent=self.root)
            return

        preview = '、'.join(names) if len(names) <= 6 else \
            '、'.join(names[:6]) + f' 等共 {len(names)} 个'
        if not messagebox.askyesno('确认删除',
                                   f'确认删除分类：{preview}？\n（其中的图标不会被删除）',
                                   parent=self.root):
            return
        deleted = 0
        for name in names:
            if self.category_manager.delete_category(name):
                deleted += 1
        if deleted:
            self.dock.sync_slots(self.category_manager.get_all_categories())
            self._log(f'✓ 已删除 {deleted} 个分类: {preview}')
            self._update_lists()
            self._rebuild_sticky_grid()
        if deleted < len(names):
            # ★ R130：部分未删成（不存在或写盘失败）——不再静默
            messagebox.showwarning(
                i18n.tr('提示', 'Notice'),
                i18n.tr(f'{len(names) - deleted} 个分类未删除成功'
                        '（不存在或写入磁盘未生效，详见运行日志）。',
                        f'{len(names) - deleted} category(s) were not deleted '
                        '(missing or disk write failed, see log).'),
                parent=self.root)

    def _on_delete_layout(self):
        """删除选中的布局（支持 Ctrl/Shift 多选批量删除）"""
        sel = list(self.layout_list.curselection())
        if not sel:
            messagebox.showwarning('提示', '请先选择布局（可 Ctrl/Shift 多选）', parent=self.root)
            return
        layouts = self.icon_manager.list_layouts()
        names = [layouts[i]['name'] for i in sel if 0 <= i < len(layouts)]
        if not names:
            return
        preview = '、'.join(_cr_disp(n) for n in names[:8]) + ('…' if len(names) > 8 else '')
        if not messagebox.askyesno(
                '确认删除',
                f'确认删除选中的 {len(names)} 个布局？\n{preview}',
                parent=self.root):
            return
        ok = 0
        for i in sorted(sel, reverse=True):
            if 0 <= i < len(layouts):
                r = self.icon_manager.delete_layout(layouts[i]['path'])
                if r.get('success'):
                    ok += 1
                else:
                    logger.error(f"删除布局失败: {r.get('error', '')}")
        self._log(f'🗑️ 已删除 {ok}/{len(names)} 个布局')
        self._update_lists()

    def _on_export_icon_list(self, *args, **kwargs):
        """★ R43 拆分薄壳——实现在 gui/data_manage_page"""
        from gui import data_manage_page as _p
        return _p.on_export_icon_list(self, *args, **kwargs)
    def _on_export_layouts(self, *args, **kwargs):
        """★ R43 拆分薄壳——实现在 gui/data_manage_page"""
        from gui import data_manage_page as _p
        return _p.on_export_layouts(self, *args, **kwargs)
    def _on_hide(self):
        """隐藏所有图标"""
        # ★ R135：同 _on_show_all——移除可见性闸门，悬浮窗模式照常执行
        if not self.root.winfo_viewable():
            self._log('⚠ 主窗口隐藏中（悬浮窗模式），照常执行')
        self.icon_manager.hide_icons()
        self._log('✓ 已隐藏所有图标')
        # ★ R254（45 批 item2）：告诉用户这不是"回不来"——退出/崩溃都会自动复明
        self._log('ℹ 退出或崩溃后会自动恢复桌面显示')
        self._update_stats()
        self._refresh_drag_panel()

    def _on_import_icon_list(self, *args, **kwargs):
        """★ R43 拆分薄壳——实现在 gui/data_manage_page"""
        from gui import data_manage_page as _p
        return _p.on_import_icon_list(self, *args, **kwargs)
    def _on_import_layouts(self, *args, **kwargs):
        """★ R43 拆分薄壳——实现在 gui/data_manage_page"""
        from gui import data_manage_page as _p
        return _p.on_import_layouts(self, *args, **kwargs)
    def _on_layout_guard_toggle(self):
        """布局守护：RDP 重连/显示拓扑切换导致大面积回退时，自动拉回守护布局"""
        v = bool(self.layout_guard_var.get())
        self._app_settings['layout_guard'] = v
        self._save_app_settings()
        # ★ R92：开关现在控制"恢复后定向看护"（守护本体 R83 已退役）
        self.icon_manager.guard_enabled = False
        # ★ R92：layout_guard 开关改为控制"恢复后看护"
        self.icon_manager.watch_enabled = bool(self._app_settings.get('layout_guard', True))
        self.icon_manager.watch_enabled = v
        try:
            if not v:
                self.dock._restore_watch_pending = 0
        except Exception:
            pass
        if v:
            mgr = self.icon_manager
            hint = (f"，守护基准：布局 [{mgr.saved_layout_name}]"
                    if mgr._guard_path else '')
            self._log(f'🛡 布局守护已开启：恢复布局后如遇远程重连/刷新回退，'
                      f'将自动拉回（手动摆放一两个图标不会触发）{hint}')
        else:
            self.icon_manager.disarm_guard()
            self._log('🛡 布局守护已关闭')

    def _on_main_cat_select(self, event=None):
        """主界面选中分类 → 右侧显示该分类的图标（多选时显示最后点选的）"""
        if not hasattr(self, 'cat_detail'):
            return
        self.cat_detail.delete(0, tk.END)
        name = self._active_category_name()
        if not name:
            return
        self.cat_detail_label.config(text=i18n.tr(f'[{name}] 中的图标:', f'Icons in [{name}]:'))
        for icon_name in self.category_manager.get_category_icons(name):
            self.cat_detail.insert(tk.END, icon_name)
        ui_layout.zebra_listbox(self.cat_detail, COLORS)

    def _on_new_category(self):
        """新建分类（★R204 GUIB-15：入口统一 strip——此前查重用 strip 值、
        创建用原值，可造出 "foo " 与 "foo" 并存）"""
        name = simpledialog.askstring(i18n.tr('新建分类', 'New Category'),
                                      i18n.tr('请输入分类名称:', 'Enter category name:'),
                                      parent=self.root)
        if name:
            name = name.strip()
        if not name:
            return

        colors = ['#8be9fd', '#50fa7b', '#ff79c1', '#ffb86c', '#bd93f9', '#ff5555']
        # ★ R79：色板点选替代手输 hex（旧默认全 #8be9fd → 渐变色模式全同色）
        color = ask_category_color(self.root)

        if self.category_manager.add_category(name, color or '#8be9fd'):
            # 顶部悬浮窗按分类配置重建
            self.dock.sync_slots(self.category_manager.get_all_categories())
            self._log(f'✓ 已创建分类: {name}（点"📌 顶部"可移出悬浮窗）')
            self._update_lists()
            self._rebuild_sticky_grid()
            # ★R302：建了分类顺手建一个**同名盒子**（省得再取名）。只做加法，
            # 盒子已存在/名字不合法/建盒失败都只在日志留一行，不影响建分类。
            from gui import desktop_box_panel
            desktop_box_panel.auto_box_for_category(name, self)
        elif any(c['name'] == name.strip()
                 for c in self.category_manager.get_all_categories()):
            messagebox.showerror('错误', '分类已存在', parent=self.root)
        else:
            # ★ R130：非重名 = 保留名或写盘失败——后者最伤（当次能用重启丢）
            messagebox.showerror(
                i18n.tr('错误', 'Error'),
                i18n.tr('创建分类未能写入磁盘（当次会话可用，重启后会消失）。\n'
                        '也可能是用了系统保留名（全部/窗/一键打开/✕）。\n'
                        '请检查配置文件是否只读/被占用/被杀软拦截。',
                        'Failed to save the new category (usable this session '
                        'only, lost on restart).\nReserved names (All/Main '
                        'Folders/Exit) are also rejected.\nCheck if the config '
                        'file is read-only/locked/blocked by antivirus.'),
                parent=self.root)

    def _on_open_drag_drop(self):
        """打开拖拽分类面板"""
        open_drag_drop_panel(
            self.root, self.icon_manager, self.category_manager,
            self.highlighter, on_changed=self._on_drag_panel_changed
        )
        self._log('🖱️ 已打开拖拽分类面板')

    def _on_rebuild_icon_cache(self):
        """重建桌面图标缓存：删除 Windows Explorer 图标缓存文件并重启 Explorer"""
        if not messagebox.askyesno(
                '重建图标缓存',
                '将删除桌面图标缓存并重启资源管理器，图标可能会短暂消失后重新加载。\n\n'
                '这可以修复图标变白、显示错误等问题。\n\n'
                '确定继续吗？',
                parent=self.root):
            return
        self._log('🔄 正在重建图标缓存…')
        # ★ P0-3 + P1-1：整个流程挪到后台线程
        # ① 不再阻塞 UI 线程（旧版 time.sleep(1.0) 在主线程冻屏）
        # ② taskkill 后用 returncode 判断；重启 explorer 用 Popen 不走 shell
        # ③ 轮询 Progman 窗口 5 秒内未起就弹框提示用户手动重启
        def _work():
            try:
                import subprocess
                import ctypes as _ct
                local = os.environ.get('LOCALAPPDATA', '')
                cache_dir = os.path.join(local, 'Microsoft', 'Windows', 'Explorer')
                legacy_db = os.path.join(local, 'IconCache.db')   # 老式单体缓存
                targets = [legacy_db]
                if os.path.isdir(cache_dir):
                    targets += [os.path.join(cache_dir, f) for f in os.listdir(cache_dir)
                                if f.startswith('iconcache_') or f.startswith('thumbcache_')]
                # ① 先杀 Explorer（解锁全部缓存文件）
                # ★R316（顺手，P2）：打包版跑在 pythonw 下没有控制台，subprocess 不挂
                #   CREATE_NO_WINDOW 就会闪一个黑框（口径同 sys_panel / data_manage_page）。
                tk_res = subprocess.run(['taskkill', '/F', '/IM', 'explorer.exe'],
                                         capture_output=True, timeout=5,
                                         creationflags=0x08000000)
                tk_rc = getattr(tk_res, 'returncode', -1)
                logger.info(f"taskkill explorer 返回 {tk_rc}")
                # 无论 taskkill 是否成功（可能本就没 explorer 在跑），都尝试删缓存
                time.sleep(1.0)  # 等句柄释放（后台线程，不冻 UI）
                # ② 删缓存（含老式 IconCache.db）
                deleted = 0
                for pth in targets:
                    try:
                        if os.path.exists(pth):
                            os.remove(pth)
                            deleted += 1
                    except Exception as _e:
                        logger.warning(f"删除缓存失败 {pth}: {_e}")
                # ★ P2-N24 + P2-N27：taskkill 失败 + 缓存一个都没删掉 = 缓存仍被锁定
                # 不报"成功删除"，直接提示用户关闭所有资源管理器窗口后重试
                # ★ P2-N27：不再重启 explorer——taskkill rc≠0 = "想杀没杀掉"
                # = explorer 还活着 = 桌面还在，无需恢复；强行 Popen 可能开出两个
                # explorer.exe 进程（旧 shell + 新 shell 抢桌面，任务栏闪烁、双托盘）
                if tk_rc != 0 and deleted == 0 and targets:
                    self._ui_queue.put(lambda: messagebox.showwarning(
                        '缓存未删除',
                        '资源管理器未能终止（可能被其他程序占用），缓存文件仍被锁定。\n'
                        '请关闭所有资源管理器窗口后重试，或重启电脑后再点此按钮。',
                        parent=self.root))
                    self._log(f'⚠ taskkill 返回 {tk_rc}，0 个缓存被删除（文件被锁定）')
                    return
                # ③ 重启 Explorer：用 Popen 直接拉，不走 shell（旧版 shell=True 的 start 命令）
                explorer_ok = False
                try:
                    subprocess.Popen(['explorer.exe'])
                    explorer_ok = True
                except Exception as e:
                    logger.error(f"Popen explorer.exe 失败: {e}")
                # ④ 轮询 Progman 窗口 5 秒内是否起来
                if explorer_ok:
                    progman_up = False
                    for _ in range(50):
                        try:
                            if _ct.windll.user32.FindWindowW('Progman', None):
                                progman_up = True
                                break
                        except Exception:
                            pass
                        time.sleep(0.1)
                    if not progman_up:
                        # 5 秒未起，提示用户手动重启
                        self._ui_queue.put(lambda: messagebox.showwarning(
                            '资源管理器未重启',
                            '已删除图标缓存，但资源管理器在 5 秒内未自动重启。\n'
                            '请按 Win+R 输入 explorer.exe 手动启动，或重启电脑。',
                            parent=self.root))
                        self._log('⚠ 资源管理器未自动重启，请手动启动 explorer.exe')
                        return
                else:
                    self._ui_queue.put(lambda: messagebox.showerror(
                        '重启资源管理器失败',
                        '已删除图标缓存，但资源管理器自动重启失败。\n'
                        '请按 Win+R 输入 explorer.exe 手动启动。',
                        parent=self.root))
                    self._log('✗ 资源管理器重启失败，请手动启动 explorer.exe')
                    return
                self._log(f'✓ 已删除 {deleted} 个缓存文件并重启资源管理器，图标缓存已重建')
                # 等待 Explorer 重启后刷新面板与列表（回到主线程）
                # ★ P1-3：先经队列回主线程，再到主线程里排 4000ms 延迟任务
                self._ui_queue.put(lambda: self.root.after(4000, self._refresh_drag_panel))
                self._ui_queue.put(lambda: self.root.after(4000, self._update_lists))
            except Exception as e:
                err = str(e)
                logger.error(f"重建图标缓存失败: {e}")
                # ★ B1：同第一处——e 在 except 块尾被 del，延迟执行的 lambda
                #   必须引用固化后的字符串
                self._ui_queue.put(lambda: self._log(f'✗ 重建失败: {err}'))
        threading.Thread(target=_work, daemon=True).start()

    def _on_recolor_category(self):
        """★ R79：改选中分类颜色（色板选色 → set_color → 同步悬浮条/列表）。

        背景：旧版新建分类手输 hex 默认全 #8be9fd，用户 5 个分类同色后
        渐变色模式"看起来失效"——除了新建时选色，还必须有事后改色入口。"""
        old_name = self._active_category_name()
        if not old_name:
            messagebox.showwarning(i18n.tr('提示', 'Notice'),
                                   i18n.tr('请先选择一个分类', 'Select a category first'),
                                   parent=self.root)
            return
        color = ask_category_color(self.root)
        if not color:
            return
        if self.category_manager.set_color(old_name, color):
            # 同步更新悬浮窗与列表（底部色条/渐变色框 2s 内换色）
            self.dock.sync_slots(self.category_manager.get_all_categories())
            self._update_lists()
            self._log(f'🎨 分类 [{old_name}] 颜色已改为 {color}')
        else:
            messagebox.showerror(i18n.tr('错误', 'Error'),
                                 i18n.tr('分类不存在，颜色未修改',
                                         'Category not found — color unchanged'),
                                 parent=self.root)

    def _on_remove_icon_from_category(self):
        """把选中的图标从分类中移除（先整体预览，确认后才保存）"""
        cat_sel = self.category_list.curselection()
        detail_sel = self.cat_detail.curselection()
        if not cat_sel or not detail_sel:
            messagebox.showwarning('提示', '请先选择分类和其中的图标',
                                   parent=self.root)
            return
        # 与 cat_detail 显示口径一致（_on_main_cat_select 用最后点选项）
        cat_name = self._active_category_name()
        names = [self.cat_detail.get(i) for i in detail_sel]
        preview = '\n'.join(f'  · {n}' for n in names[:15])
        if len(names) > 15:
            preview += f'\n  … 等共 {len(names)} 个'
        if not messagebox.askyesno(
                '确认移除',
                f'将把 {len(names)} 个图标移出 [{cat_name}]：\n\n{preview}\n\n确定保存更改吗？',
                parent=self.root):
            return
        for icon_name in names:
            self.category_manager.remove_icon_from_category(cat_name, icon_name)
        self._log(f'➖ 已从 [{cat_name}] 移除 {len(names)} 个图标')
        self._update_lists()
        self._on_main_cat_select()

    def _on_rename_category(self):
        """重命名分类（取最后点选的分类）"""
        old_name = self._active_category_name()
        if not old_name:
            messagebox.showwarning(i18n.tr('提示', 'Notice'),
                                   i18n.tr('请先选择一个分类', 'Select a category first'),
                                   parent=self.root)
            return

        new_name = simpledialog.askstring(i18n.tr('重命名分类', 'Rename Category'),
                                           i18n.tr('请输入新名称:', 'Enter new name:'),
                                           initialvalue=old_name, parent=self.root)
        if not new_name or new_name == old_name:
            return

        if self.category_manager.rename_category(old_name, new_name):
            # 同步更新悬浮窗
            self.dock.sync_slots(self.category_manager.get_all_categories())
            self._log(f'✓ 已重命名为: {new_name}')
            self._update_lists()
            self._rebuild_sticky_grid()
        else:
            # ★ R130：区分"重名/保留名"与"写盘失败"
            if any(c['name'] == new_name.strip()
                   for c in self.category_manager.get_all_categories()):
                messagebox.showerror('错误', '重命名失败：目标名称已存在',
                                     parent=self.root)
            else:
                messagebox.showerror(
                    i18n.tr('错误', 'Error'),
                    i18n.tr('重命名未能写入磁盘（当次会话可用，重启后会消失）。\n'
                            '也可能是保留了专用名（全部/窗/一键打开/✕）。',
                            'Rename was not saved to disk (usable this session '
                            'only).\nReserved names are also rejected.'),
                    parent=self.root)

    def _on_rename_layout(self):
        """重命名选中的布局"""
        sel = self.layout_list.curselection()
        if not sel:
            messagebox.showwarning('提示', '请先选择一个布局', parent=self.root)
            return
        layouts = self.icon_manager.list_layouts()
        if sel[0] >= len(layouts):
            return
        lay = layouts[sel[0]]
        new_name = simpledialog.askstring(i18n.tr('重命名布局', 'Rename Layout'),
                                           i18n.tr('请输入新名称:', 'Enter new name:'),
                                           initialvalue=_cr_disp(lay['name']),
                                           parent=self.root)
        if not new_name:
            return
        # ★ R79f：EN 下把建议名 "All" 原样确认 = canonical「全部」（视为未改名）
        new_name = _cr_canon(new_name.strip())
        if new_name == lay['name']:
            return
        r = self.icon_manager.rename_layout(lay['path'], new_name)
        if r['success']:
            self._log(f'✏️ 布局已重命名: {new_name}')
            self._update_lists()
        else:
            messagebox.showerror('错误', r.get('error', '重命名失败'), parent=self.root)


    def _on_reset_dock_colors(self):
        """恢复悬浮条文字默认色"""
        self._app_settings.pop('dock_text_color', None)
        self._app_settings.pop('dock_active_text_color', None)
        self._save_app_settings()
        # ★ R153：恢复默认=清自定义回自动对比档（非写死旧默认值）
        self.dock.set_text_colors(None, False)
        self.dock.set_text_colors(None, True)
        try:
            self.dock._repaint_if_visible()
        except Exception:
            pass
        self._log('🎨 悬浮条文字色已恢复默认')

    def _on_restore(self):
        """恢复布局"""
        sel = self.layout_list.curselection()
        if not sel:
            messagebox.showwarning('提示', '请先选择一个布局', parent=self.root)
            return

        layouts = self.icon_manager.list_layouts()
        if sel[0] >= len(layouts):
            return

        self.status_var.set('正在恢复...')
        self.root.update_idletasks()  # ★ P1-1/P2-N4：update() 改 update_idletasks() 防重入

        # ★ P1-1：旧版 time.sleep(0.5) 在主线程冻屏，改 after 分步收尾
        # ★ R100：只恢复一次——restore_layout 内置 [verify]/[verify2]
        # 读回验证+偏差补发，500ms 后的第二次恢复冗余（多闪一遍）
        result = self.icon_manager.restore_layout(layouts[sel[0]]['path'])
        self.root.after(
            500,
            lambda: self._finish_restore(layouts[sel[0]]['path'], result))

    def _on_save(self):
        """保存布局（选中分类时预填分类名 = 保存该分类的专属布局，切换分类自动套用）"""
        initial = datetime.now().strftime('%Y%m%d_%H%M%S')
        hint = i18n.tr('请输入布局名称（与分类同名即成为该分类的专属布局）:',
                       "Enter a layout name (same name as a category makes it "
                       "that category's dedicated layout):")
        sel = self.category_list.curselection() if hasattr(self, 'category_list') else None
        if sel:
            # ★ P2-N2：与 _active_category_name 口径一致（用 sel[-1] 而非 sel[0]）
            # ★ P2-N3：分类名含 "(" 时 split(' (')[0] 会误截断，改 rsplit(' (', 1)[0]
            initial = self.category_list.get(sel[-1]).rsplit(' (', 1)[0]
            hint = i18n.tr(f'保存为分类 [{initial}] 的专属布局？可改名另存:',
                           f'Save as the dedicated layout for [{initial}]? '
                           'Rename it to save as a copy:')
        name = simpledialog.askstring(i18n.tr('保存布局', 'Save Layout'), hint,
                                       initialvalue=initial, parent=self.root)
        if not name:
            return

        self.status_var.set('正在保存...')
        # ★ P2-N4：update() 会重入事件循环（双击可能触发两次），改 update_idletasks()
        self.root.update_idletasks()

        result = self.icon_manager.save_layout(name)
        if result['success']:
            extra = '（切换到该分类时将自动套用）' if name in {
                c['name'] for c in self.category_manager.get_all_categories()} else ''
            self._log(f'✓ 已保存布局 [{name}] {result["count"]} 个图标 {extra}')
            # 保存即守护：以这份布局为基准，刷新/拓扑切换导致的瞬间回退自动拉回
            try:
                path = str(APP_DIR / 'icon_backups' / result['file'])
                self.icon_manager.arm_guard(path, name)
                self._remember_guard_layout(path)
                if self.icon_manager.guard_enabled:
                    self._log(f'🛡 布局守护已以此布局 [{name}] 为基准'
                              '（遇到瞬间大面积回退会自动恢复）')
            except Exception as e:
                logger.error(f"布局守护布防失败: {e}")
            self._update_lists()
        else:
            self._log(f'✗ 错误: {result.get("error", "")}')

    def _on_save_all_icons(self):
        """保存全部图标位置（含被当前分类隐藏的图标）。

        此前只有重启后（全量可见）才能保存完整布局；一旦切了分类，
        保存就只能拿到当前分类的图标——用户要求提供"保存全部"。
        位置来源：可见图标实时读数并入缓存 + 缓存中隐藏图标隐藏前的位置
        （_read_all_positions_live 实测 81 个仅 ~10ms）。"""
        im = self.icon_manager
        name = simpledialog.askstring(
            i18n.tr('保存全部图标', 'Save All Icons'),
            i18n.tr('保存【全部图标】的当前位置为布局，名称:',
                    'Save the current positions of ALL icons as a layout. Name:'),
            initialvalue=_cr_disp('全部'), parent=self.root)
        if not name:
            return
        # ★ R79f：EN 建议名 All 落盘仍为 canonical「全部」——悬浮条 All 槽
        # 与退出恢复联动都按 pick_layout('全部') 匹配，存成 "All" 会断联动
        name = _cr_canon(name.strip())

        self.status_var.set('正在保存全部图标...')
        # ★ P2-N4：update() 会重入事件循环，改 update_idletasks()
        self.root.update_idletasks()

        try:
            live = im._read_all_positions_live()
            if live:
                im._position_cache.update(live)
        except Exception as e:
            logger.error(f"保存全部: 实时读数失败（沿用缓存）: {e}")

        result = im.save_layout(name, sync_full_cache=True)
        if result.get('success'):
            self._log(f"✓ 已保存全部布局 [{name}] "
                      f"{result.get('count', '?')} 个图标（含被分类隐藏的图标）")
            try:
                path = str(APP_DIR / 'icon_backups' / result['file'])
                self.icon_manager.arm_guard(path, name)
                self._remember_guard_layout(path)
                if self.icon_manager.guard_enabled:
                    self._log(f'🛡 布局守护已以此全量布局 [{name}] 为基准')
            except Exception as e:
                logger.error(f"布局守护布防失败: {e}")
            self._update_lists()
            self.status_var.set('✓ 全部布局已保存')
        else:
            self._log(f"✗ 保存全部失败: {result.get('error', '')}")
            self.status_var.set('保存失败')

    def _on_toggle_cat_dock(self):
        """切换分类是否显示在顶部悬浮窗（支持多选批量）"""
        names = self._selected_category_names()
        if not names:
            messagebox.showwarning('提示', '请先在左侧选择分类', parent=self.root)
            return
        cats = self.category_manager.get_all_categories()
        new_val = not next((c for c in cats if c['name'] == names[0]),
                           {}).get('in_dock', True)
        for name in names:
            self.category_manager.set_in_dock(name, new_val)
        self.dock.sync_slots(self.category_manager.get_all_categories())
        state = '显示在顶部' if new_val else '从顶部移除'
        self._log(f'📌 [{ "、".join(names) }] 已{state}')
        self._update_lists()

    def _on_toggle_cat_sticky(self):
        """切换分类是否常驻桌面（支持多选批量）"""
        names = self._selected_category_names()
        if not names:
            messagebox.showwarning('提示', '请先在左侧选择分类', parent=self.root)
            return
        cats = self.category_manager.get_all_categories()
        new_val = not next((c for c in cats if c['name'] == names[0]),
                           {}).get('sticky', False)
        for name in names:
            self.category_manager.set_sticky(name, new_val)
            if new_val and hasattr(self, '_exclusive_vars') and name in self._exclusive_vars:
                # 常驻与"只显示自己"互斥：走 display_mode（set_sticky 内部已清另一种）
                self._exclusive_vars[name].set(False)
                self._refresh_sticky_chip(name, 'exclusive')
            if new_val and hasattr(self, '_exsys_vars') and name in self._exsys_vars:
                self._exsys_vars[name].set(False)
                self._refresh_sticky_chip(name, 'exclusive_sys')
            if hasattr(self, '_resident_vars') and name in self._resident_vars:
                self._resident_vars[name].set(new_val)
                self._refresh_sticky_chip(name, 'resident')
        state = '常驻桌面（切换分类时保持可见）' if new_val else '取消常驻'
        self._log(f'🤖 [{ "、".join(names) }] 已{state}')
        self._update_lists()
        self._replay_active_slot()

    def _pick_dock_color(self, active: bool):
        """选悬浮条文字颜色（未选中/选中）"""
        from tkinter import colorchooser
        key = 'dock_active_text_color' if active else 'dock_text_color'
        cur = self._app_settings.get(key, '#ffffff' if active else '#e8eaf2')
        rgb, hexs = colorchooser.askcolor(color=cur, title='选择悬浮条文字颜色',
                                          parent=self.root)
        if not hexs:
            return
        hexs = hexs.upper()
        self._app_settings[key] = hexs
        self._save_app_settings()
        try:
            self.dock.set_text_colors(hexs, active)
        except Exception:
            pass
        try:
            self.dock._repaint_if_visible()
        except Exception:
            pass
        self._log(f'🎨 悬浮条文字色已更新: {key}={hexs}')

    def _show_layout_menu(self, event=None):
        """布局列表右键菜单"""
        try:
            self.layout_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.layout_menu.grab_release()


    def _selected_category_names(self) -> List[str]:
        """所有选中的分类名（支持 Ctrl 多选 / Shift 选段 / Ctrl+A）"""
        return [self.category_list.get(i).rsplit(' (', 1)[0]
                for i in self.category_list.curselection()]

    def _active_category_name(self) -> Optional[str]:
        """最后点选的分类（重命名/查看等单分类操作用）"""
        sel = self.category_list.curselection()
        if not sel:
            return None
        i = sel[-1]  # 取最后选中的项（EXTENDED 模式下鼠标点的是最后一个）
        if i < 0 or i >= self.category_list.size():
            return None
        return self.category_list.get(i).rsplit(' (', 1)[0]

    def _selected_category_name(self) -> Optional[str]:
        """兼容旧调用：取最后点选的分类"""
        name = self._active_category_name()
        if not name:
            messagebox.showwarning('提示', '请先在左侧选择一个分类', parent=self.root)
        return name

    def _mark_radios(self, widget):
        """★R251 item4（用户 43 批）：给所有 ttk.Radiobutton 递归挂上
        **看得见的圆圈前缀**——选中「● 」、未选中「○ 」。

        用户原话：「什么时候做（触发），做什么（动作）这几项，选择的时候，前面
        加一个小圆圈，现在没选中的前面没有小圈圈，看不见。勾选状态颜色区分一下。」

        为什么又是文字前缀：clam 主题下 ttk 的原生单选圆点跟勾选框那个"像 ✗ 的
        勾"是同一族问题（★R222 定案）。勾选框那一半已由 ★R315 用"色块档"接回
        原生指示器（笔画配成与块同色 ⇒ 看不见叉），**单选钮这半边还靠文字**——
        实心/空心圆要另做一档，已挂账。
        颜色那半边不在这里做，走样式表的 `('selected', primary)`
        （gui/theme_panel.py 的 `style.map('TRadiobutton', ...)`）——**跟着主题
        实时换**，不必往 `_replace_widget_colors` 的映射表加键。

        本方法的四条口径是历轮从勾选框那套抄来的，那套现已退役，口径留下：
          · 递归先深入子控件，再处理自己（父容器整块重建时也能覆盖到）；
          · **先剥已有前缀**再拼，本方法会随换肤/重建反复调用，不剥就叠成
            「● ○ ● 每天…」；
          · 单选钮的 variable 是**同组共用**的一个 StringVar ⇒ 判"选没选中"
            必须比 `var.get() == 本钮的 value`，不能只看 var 真假；
          · 挂 `trace_add('write')` ⇒ 点选/程序改值都会即时重画前缀。
        """
        rad_on, rad_off = '● ', '○ '
        for child in widget.winfo_children():
            self._mark_radios(child)
        if not isinstance(widget, ttk.Radiobutton):
            return
        varname = str(widget.cget('variable'))
        if not varname:
            return
        try:
            var = tk.StringVar(self.root, name=varname)
        except Exception:
            return
        base = str(widget.cget('text'))
        while base.startswith(rad_on) or base.startswith(rad_off):
            base = base[2:]
        want = str(widget.cget('value'))

        def upd(*_):
            try:
                picked = (var.get() == want)
                widget.configure(text=(rad_on if picked else rad_off) + base)
            except Exception:
                pass

        var.trace_add('write', upd)
        upd()

    def _on_export_all_data(self, *args, **kwargs):
        """★ R43 拆分薄壳——实现在 gui/data_manage_page"""
        from gui import data_manage_page as _p
        return _p.on_export_all_data(self, *args, **kwargs)
    def _collect_layout_archives(self) -> list:
        """★ S 段/B18：读取全部布局存档内容（导出全部数据用，此前漏掉）。
        include_internal=True：内部备份（_家长模式前 等）一并带走，备份才完整。"""
        layouts = []
        for lay in self.icon_manager.list_layouts(include_internal=True):
            try:
                with open(lay['path'], 'r', encoding='utf-8') as fh:
                    layouts.append(json.load(fh))
            except Exception as e:
                logger.warning(f"导出布局存档读取失败 {lay.get('path')}: {e}")
        return layouts

    def _read_prefix_rules(self) -> dict:
        """读前缀规则文件（真实存储在 config/prefix_rules.json，
        归 classifier 模块所有——2026-09-05 修复导出恒为空的问题）"""
        pr = APP_DIR / 'config' / 'prefix_rules.json'
        try:
            if pr.exists():
                return json.loads(pr.read_text(encoding='utf-8'))
        except Exception as e:
            logger.error(f'读取前缀规则失败: {e}')
        return {}

    def _on_import_all_data(self, *args, **kwargs):
        """★ R43 拆分薄壳——实现在 gui/data_manage_page"""
        from gui import data_manage_page as _p
        return _p.on_import_all_data(self, *args, **kwargs)
    def _on_export_categories(self, *args, **kwargs):
        """★ R43 拆分薄壳——实现在 gui/data_manage_page"""
        from gui import data_manage_page as _p
        return _p.on_export_categories(self, *args, **kwargs)
    def _on_import_categories(self, *args, **kwargs):
        """★ R43 拆分薄壳——实现在 gui/data_manage_page"""
        from gui import data_manage_page as _p
        return _p.on_import_categories(self, *args, **kwargs)
    def _on_export_desktop_icons(self, *args, **kwargs):
        """★ R43 拆分薄壳——实现在 gui/data_manage_page"""
        from gui import data_manage_page as _p
        return _p.on_export_desktop_icons(self, *args, **kwargs)
    def _on_restore_desktop_icons(self, *args, **kwargs):
        """★ R43 拆分薄壳——实现在 gui/data_manage_page"""
        from gui import data_manage_page as _p
        return _p.on_restore_desktop_icons(self, *args, **kwargs)
    def _on_export_explorer_groups(self, *args, **kwargs):
        """★ R43 拆分薄壳——实现在 gui/data_manage_page"""
        from gui import data_manage_page as _p
        return _p.on_export_explorer_groups(self, *args, **kwargs)
    def _on_import_explorer_groups(self, *args, **kwargs):
        """★ R43 拆分薄壳——实现在 gui/data_manage_page"""
        from gui import data_manage_page as _p
        return _p.on_import_explorer_groups(self, *args, **kwargs)
    def _on_clear_folder_groups(self, *args, **kwargs):
        """★ R43 拆分薄壳——实现在 gui/data_manage_page"""
        from gui import data_manage_page as _p
        return _p.on_clear_folder_groups(self, *args, **kwargs)
    def _on_clear_all_privacy(self, *args, **kwargs):
        """★ R43 拆分薄壳——实现在 gui/data_manage_page"""
        from gui import data_manage_page as _p
        return _p.on_clear_all_privacy(self, *args, **kwargs)
    def _on_cleanup_logs(self, *args, **kwargs):
        """★ R43 拆分薄壳——实现在 gui/log_page"""
        from gui import log_page as _p
        return _p.on_cleanup_logs(self, *args, **kwargs)
    def _build_icon_manifest_text(self, *args, **kwargs):
        """★ R43 拆分薄壳——实现在 gui/data_manage_page"""
        from gui import data_manage_page as _p
        return _p.build_icon_manifest_text(self, *args, **kwargs)
    def _on_clear_icon_data(self, *args, **kwargs):
        """★ R43 拆分薄壳——实现在 gui/data_manage_page"""
        from gui import data_manage_page as _p
        return _p.on_clear_icon_data(self, *args, **kwargs)
    def _import_layouts_batch(self, *args, **kwargs):
        """★ R43 拆分薄壳——实现在 gui/data_manage_page"""
        from gui import data_manage_page as _p
        return _p.import_layouts_batch(self, *args, **kwargs)
    def _on_show_all(self):
        """显示所有图标：文件属性原地恢复（位置从未动过，无需布局回滚）"""
        # ★ R135：彻底移除"主窗口可见才允许"的旧闸门（P1-N8）——桌面图
        # 标操作是纯 Win32 调用，与 Tk 窗口可见性无关；用户的主动用法
        # 就是只留悬浮窗（主窗口隐藏），闸门反而让快捷键/按钮形同虚设。
        # R133 的"自动呼出主窗口"也被否（弹窗打扰，方向整反了）。
        if not self.root.winfo_viewable():
            self._log('⚠ 主窗口隐藏中（悬浮窗模式），照常执行')
        self.icon_manager.show_icons()
        # ★R305（真机 2026-10-07 实撞的同一类缺口）：收纳档切过分类以后，"这次不该显示"
        #   的图标是真的在盒子里、不在桌面上，而下面这句 unhide_all 只翻属性位 ⇒ 点完
        #   「显示所有图标」桌面照样只剩几枚。所以先散盒，再清属性。
        from core import visibility_router as _vr
        _vr.release_all_for(self.icon_manager, 'all-btn', self._log)
        try:
            n = self.icon_manager.unhide_all()
            self._log('✓ 已显示所有图标' + (f'（恢复 {n} 个）' if n else ''))
        except Exception as e:
            self._log(f'✗ 恢复图标出错: {e}')
            logger.error(f"显示全部图标失败: {e}")
        self._update_stats()
        # 同步刷新拖拽面板
        self._refresh_drag_panel()

    def _build_manage_tab(self, tab_manage):
        """管理页 UI（R144 自 _build_ui 原样迁入，tab_manage 为页签容器）"""
        # ============ 📁 管理：操作按钮 + 图标分类 + 已保存布局 ============
        # 操作按钮单行横排（2026-09-05 用户反馈：按钮要横着排满一行，
        # 别拆成多行显得乱；"打开分类文件夹"已删——资源管理页保留同功能）
        btn_frame = ttk.Frame(tab_manage)
        btn_frame.pack(fill=tk.X, pady=(0, 6))

        # ---- 第 1 行：保存 / 恢复（全 primary 蓝色系）----
        # ---- 第 2 行：显示控制 / 工具（secondary 中性色系，隐藏用 error）----
        # ★R216 阶段 2b：两排改走同一张 12 分格（4+4+4 与 3+3+3+3）⇒ 上下排
        # 的竖边终于对齐（原先各排自己 expand 等分，3 枚对 4 枚必然错开）
        btn_row1 = ttk.Frame(btn_frame)
        btn_row1.pack(fill=tk.X, pady=(0, 2))
        (self.save_btn, self.save_all_btn,
         self.restore_btn) = ui_layout.action_bar(btn_row1, [
            (i18n.tr('💾 保存', '💾 Save'), self._on_save, 'primary.TButton'),
            (i18n.tr('💾 保存全部', '💾 Save All'), self._on_save_all_icons,
             'primary.TButton'),
            (i18n.tr('↩️ 恢复', '↩️ Restore'), self._on_restore,
             'primary.TButton')], spans=(4, 4, 4), align='fill', pad=SP['xs'])

        btn_row2 = ttk.Frame(btn_frame)
        btn_row2.pack(fill=tk.X)
        (self.show_all_btn, self.hide_btn, self.drag_btn,
         self.rebuild_cache_btn) = ui_layout.action_bar(btn_row2, [
            (i18n.tr('👁️ 全部', '👁️ Show All'), self._on_show_all,
             'secondary.TButton'),
            (i18n.tr('🙈 隐藏', '🙈 Hide'), self._on_hide, 'error.TButton'),
            (i18n.tr('🖱️ 拖拽分类', '🖱️ Drag & Sort'), self._on_open_drag_drop,
             'secondary.TButton'),
            (i18n.tr('🔧 修复图标缓存', '🔧 Fix Icon Cache'),
             self._on_rebuild_icon_cache, 'secondary.TButton')],
            spans=(3, 3, 3, 3), align='fill', pad=SP['xs'])

        # ★R216 阶段 2b：标签+三枚按钮走 form_row 网格 ⇒ 与上方两行按钮列对齐
        # （原先 pack(side=LEFT, padx=…) 让按钮长短不齐）
        color_row = ttk.Frame(tab_manage)
        color_row.pack(fill=tk.X, pady=(0, 6))
        _cf = ui_layout.form_block(color_row)
        ui_layout.form_row(_cf, 0,
                           i18n.tr('🎨 悬浮条文字:', '🎨 Bar Text:'),
                           ttk.Button(_cf, text=i18n.tr('未选中文字色',
                                                        'Normal Text Color'),
                                      command=lambda: self._pick_dock_color(False),
                                      style='secondary.TButton'),
                           ttk.Button(_cf, text=i18n.tr('选中文字色',
                                                        'Selected Text Color'),
                                      command=lambda: self._pick_dock_color(True),
                                      style='secondary.TButton'),
                           ttk.Button(_cf, text=i18n.tr('↺ 恢复默认', '↺ Reset'),
                                      command=self._on_reset_dock_colors,
                                      style='secondary.TButton'))

        # 左右分栏：左=图标分类（含明细），右=已保存布局
        manage_pane = ttk.PanedWindow(tab_manage, orient=tk.HORIZONTAL)
        manage_pane.pack(fill=tk.BOTH, expand=True)
        left_frame = ttk.Frame(manage_pane)
        manage_pane.add(left_frame, weight=3)
        right_frame = ttk.Frame(manage_pane)
        manage_pane.add(right_frame, weight=2)

        cat_frame = ttk.LabelFrame(left_frame, text=i18n.tr('📂 图标分类', '📂 Icon Categories'),
                                   padding='10', style='Card.TLabelframe')
        cat_frame.pack(fill=tk.BOTH, expand=True)

        self.cat_pane = tk.PanedWindow(
            cat_frame, orient=tk.HORIZONTAL, sashwidth=6, sashrelief=tk.FLAT,
            bd=0, bg=COLORS['border'], opaqueresize=True)
        self.cat_pane.pack(fill=tk.BOTH, expand=True, pady=(0, 5))

        # ---- 左栏：分类列表（支持 Windows 标准 Ctrl 多选 / Shift 选段 / Ctrl+A 全选）----
        cat_left = ttk.Frame(self.cat_pane)
        self.category_list = tk.Listbox(cat_left,
                                        font=self.ui_font,
                                        bg=COLORS['surface'],
                                        fg=COLORS['fg'],
                                        selectmode=tk.EXTENDED,
                                        relief=tk.FLAT,
                                        highlightthickness=0,
                                        exportselection=False)
        scrollbar = ttk.Scrollbar(cat_left, orient=tk.VERTICAL,
                                  command=self.category_list.yview)
        self.category_list.configure(yscrollcommand=scrollbar.set)

        self.category_list.pack(side=tk.LEFT, fill=tk.BOTH,
                                expand=True, padx=(0, 2), pady=2)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y, pady=2)
        self.cat_pane.add(cat_left, width=170, minsize=120, stretch='never')

        # ---- 右栏：选中分类的图标明细（点分类即显示，可多选移除）----
        cat_right = ttk.Frame(self.cat_pane)
        self.cat_detail_label = ttk.Label(cat_right, text=i18n.tr('选中分类查看图标:', 'Select a category to view icons:'),
                                          foreground=COLORS['text_secondary'],
                                          font=ui_layout.app_font(9))
        self.cat_detail_label.pack(anchor=tk.W)
        detail_row = ttk.Frame(cat_right)
        detail_row.pack(fill=tk.BOTH, expand=True)
        self.cat_detail = tk.Listbox(detail_row,
                                     font=self.ui_font,
                                     bg=COLORS['surface'],
                                     fg=COLORS['fg'],
                                     selectmode=tk.EXTENDED,
                                     relief=tk.FLAT,
                                     highlightthickness=0,
                                     exportselection=False)
        detail_scroll = ttk.Scrollbar(detail_row, orient=tk.VERTICAL,
                                      command=self.cat_detail.yview)
        self.cat_detail.configure(yscrollcommand=detail_scroll.set)
        self.cat_detail.pack(side=tk.LEFT, fill=tk.BOTH, expand=True,
                             padx=(0, 2), pady=(2, 0))
        detail_scroll.pack(side=tk.RIGHT, fill=tk.Y, pady=(2, 0))
        detail_btns = ttk.Frame(cat_right)
        detail_btns.pack(fill=tk.X, pady=(SP['xs'], 0))
        # ★R216 阶段 2b：按钮铺满明细栏宽度，提示小字下移一行（原先按钮和
        # 长提示挤同一排，提示把按钮挤得忽长忽短）
        ui_layout.action_bar(detail_btns, [
            (i18n.tr('➖ 移除选中图标出分类', '➖ Remove Selected From Category'),
             self._on_remove_icon_from_category, 'secondary.TButton')],
            cols=1, align='fill')
        ttk.Label(detail_btns,
                  text=i18n.tr('提示: 拖动中间分隔条可调左栏宽度',
                               'Tip: drag the divider to resize the left column'),
                  foreground=COLORS['text_secondary'],
                  font=ui_layout.app_font(9)).pack(anchor=tk.W,
                                                       pady=(SP['xs'], 0))

        self.cat_pane.add(cat_right, minsize=200, stretch='always')

        # 恢复上次的左栏宽度；拖拽结束时记住
        # ★R285-N7：读**内存里那一份**（启动时已读过一次盘），不再在构建期重新
        # 读盘——旧写法会连带把 Z13 那把设置禁写闸中途复位（详见 main_window
        # ._load_app_settings 的同号注释）。
        try:
            saved_w = int(dict(self._app_settings).get('cat_pane_width', 170))
            self.cat_pane.sash_place(0, max(120, saved_w), 0)
        except Exception:
            pass
        self.cat_pane.bind('<ButtonRelease-1>', self._on_cat_sash_release)
        self.category_list.bind('<<ListboxSelect>>', self._on_main_cat_select)

        # 分类操作按钮（★R216 阶段 2b：三排 action_bar ⇒ 每排按钮等宽铺满、
        # 上下排左右边缘对齐；原先 pack(expand) 让"新建/重命名/删除/改颜色"
        # 一排不等分、和第二三排对不齐）
        cat_btn_frame = ttk.Frame(cat_frame)
        cat_btn_frame.pack(fill=tk.X, pady=(0, 2))
        ui_layout.action_bar(cat_btn_frame, [
            (i18n.tr('➕ 新建', '➕ New'), self._on_new_category,
             'secondary.TButton'),
            (i18n.tr('✏️ 重命名', '✏️ Rename'), self._on_rename_category,
             'secondary.TButton'),
            (i18n.tr('🗑️ 删除分类', '🗑️ Delete Category'), self._on_delete_category,
             'secondary.TButton'),
            (i18n.tr('🎨 改颜色', '🎨 Color'), self._on_recolor_category,
             'secondary.TButton')],
            spans=(3, 3, 3, 3), align='fill', pad=SP['xs'])
        ui_layout.action_bar(cat_btn_frame, [
            (i18n.tr('📌 顶部', '📌 On Top'), self._on_toggle_cat_dock,
             'secondary.TButton'),
            (i18n.tr('🤖 常驻', '🤖 Resident'), self._on_toggle_cat_sticky,
             'secondary.TButton'),
            (i18n.tr('🧹 清理失效', '🧹 Purge Broken'), self._on_cleanup_stale_icons,
             'secondary.TButton')],
            spans=(4, 4, 4), align='fill', pad=SP['xs'])
        ui_layout.action_bar(cat_btn_frame, [
            (i18n.tr('📤 导出', '📤 Export'), self._on_export_icon_list,
             'secondary.TButton'),
            # ★ R19：导入清单（"📤 导出"的逆操作）——按 txt 清单合并分类归属
            (i18n.tr('📥 导入清单', '📥 Import List'), self._on_import_icon_list,
             'secondary.TButton'),
            (i18n.tr('🧹 清空', '🧹 Clear'), self._on_clear_icon_data,
             'error.TButton')],
            spans=(4, 4, 4), align='fill', pad=SP['xs'])

        # 布局区（右栏）：操作按钮在列表上方（2026-09-04 用户反馈：
        # 按钮在下方显示不全，且列表项看不到保存时间）
        layout_frame = ttk.LabelFrame(right_frame, text=i18n.tr('💾 已保存的布局', '💾 Saved Layouts'),
                                      padding='10', style='Card.TLabelframe')
        layout_frame.pack(fill=tk.BOTH, expand=True)

        layout_btns = ttk.Frame(layout_frame)
        layout_btns.pack(fill=tk.X, pady=(0, 2))
        # ★R216 阶段 2b：两排 action_bar（每排等宽铺满、上下对齐）；
        # "🛡 布局守护"是开关不是动作，改到列表下方单独一行，别和按钮混排
        ui_layout.action_bar(layout_btns, [
            (i18n.tr('↩️ 恢复选中', '↩️ Restore Selected'), self._on_restore,
             'secondary.TButton'),
            (i18n.tr('✏️ 重命名', '✏️ Rename'), self._on_rename_layout,
             'secondary.TButton'),
            (i18n.tr('🗑️ 删除', '🗑️ Delete'), self._on_delete_layout,
             'secondary.TButton')],
            spans=(4, 4, 4), align='fill', pad=SP['xs'])
        ui_layout.action_bar(layout_btns, [
            (i18n.tr('📦 导出zip', '📦 Export ZIP'), self._on_export_layouts,
             'secondary.TButton'),
            (i18n.tr('📥 导入zip', '📥 Import ZIP'), self._on_import_layouts,
             'secondary.TButton')],
            spans=(6, 6), align='fill', pad=SP['xs'])
        # ★R216 阶段 2b：布局守护是"开关"不是"动作"，从按钮排里摘出来单独
        # 一行（原先它和 导出zip/导入zip 混在同一排，看着像第三个动作按钮）
        self.layout_guard_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(layout_btns, text=i18n.tr('🛡 布局守护', '🛡 Layout Guard'),
                        variable=self.layout_guard_var,
                        command=self._on_layout_guard_toggle).pack(
            anchor=tk.W, pady=(SP['xs'], 0))

        self.layout_list = tk.Listbox(layout_frame,
                                      font=self.ui_font,
                                      bg=COLORS['surface'],
                                      fg=COLORS['fg'],
                                      selectmode=tk.EXTENDED,
                                      relief=tk.FLAT,
                                      highlightthickness=0,
                                      exportselection=False)
        layout_scrollbar = ttk.Scrollbar(layout_frame, orient=tk.VERTICAL,
                                         command=self.layout_list.yview)
        self.layout_list.configure(yscrollcommand=layout_scrollbar.set)

        self.layout_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5, pady=5)
        layout_scrollbar.pack(side=tk.RIGHT, fill=tk.Y, pady=5)

        self.layout_list.bind('<Double-Button-1>', lambda e: self._on_restore())

        # 右键菜单：恢复 / 重命名 / 删除（支持多选批量删除）
        self.layout_menu = tk.Menu(layout_frame, tearoff=0)
        self.layout_menu.add_command(label=i18n.tr('↩️ 恢复选中', '↩️ Restore Selected'), command=self._on_restore)
        self.layout_menu.add_command(label=i18n.tr('✏️ 重命名', '✏️ Rename'), command=self._on_rename_layout)
        self.layout_menu.add_command(label=i18n.tr('🗑️ 删除选中', '🗑️ Delete Selected'), command=self._on_delete_layout)
        self.layout_list.bind('<Button-3>', self._show_layout_menu)

        self.cat_pane.bind('<ButtonRelease-1>', self._on_cat_sash_release)
        self.category_list.bind('<<ListboxSelect>>', self._on_main_cat_select)

        # 分类操作按钮（两行，全显示；第二行含导出/清空）
