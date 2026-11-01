# -*- coding: utf-8 -*-
"""📥 下载记录卡片（R112 V1，设计稿见 DEV.md"下载目录记忆·功能设计"）。

放"📂 资源"标签页（与文件夹组同属"文件在哪"主题）。V1 保持简单：
树表 + 今天/昨天/本周/更早 分组行 + 三个动作（打开所在文件夹/忽略/
双击打开文件）。数据由 core.download_tracker 提供。
"""
import logging
import os
import subprocess
import tkinter as tk

from gui import ui_layout
from gui.ui_layout import SP
from utils import i18n   # ★ R163b：R160 加的三个模块函数用到，此前漏导入
from tkinter import messagebox, ttk

logger = logging.getLogger("desktop_manager")


def _fmt_size(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ''
    if n < 1024:
        return f'{n} B'
    for unit in ('KB', 'MB', 'GB', 'TB'):   # ★R204 GUIB-14：≥1TB 不再显示 1500.0 GB
        n /= 1024.0
        if n < 1024:
            return f'{n:.1f} {unit}'
    return f'{n:.1f} PB'


def fold_by_dir(items, min_run=2):
    """把记录按目录归拢成段（R119 语义修正：不要求相邻——用户实测
    Downloads 与 D 盘交错排列时"连续"永远凑不齐；改为组内同目录
    全归拢，≥min_run 折叠，其余平铺。目录按首次出现顺序。）
    返回 [('fold', [r...]) | ('one', [r]), ...]"""
    order = []           # 目录首次出现顺序
    buckets = {}
    for r in items:
        d = r.get('dir', '')
        if d not in buckets:
            buckets[d] = []
            order.append(d)
        buckets[d].append(r)
    segs = []
    for d in order:
        run = buckets[d]
        segs.append(('fold' if len(run) >= min_run else 'one', run))
    return segs


def build_download_card(parent, gui, colors):
    """在"📂 资源"标签页构建"📥 下载记录"卡片，返回 frame"""
    from utils import i18n

    frame = ttk.LabelFrame(
        parent, text=i18n.tr('📥 下载记录（下载到哪了一目了然）',
                             '📥 Download Memory (where did it go)'),
        padding='10', style='Card.TLabelframe')
    frame.pack(fill=tk.BOTH, expand=True, pady=(0, 6))

    # 头行：摘要 + 打开下载目录/刷新
    head = ttk.Frame(frame)
    head.pack(fill=tk.X, pady=(0, 4))
    gui._dl_summary = ttk.Label(head, text='', foreground=colors['secondary'])
    gui._dl_summary.pack(side=tk.LEFT)
    ttk.Button(head, text=i18n.tr('📂 打开下载目录', '📂 Open Downloads'),
               command=lambda: _open_downloads_dir(gui),
               style='secondary.TButton').pack(side=tk.RIGHT)
    ttk.Button(head, text=i18n.tr('🔄 刷新', '🔄 Refresh'),
               command=lambda: refresh_sync(gui),
               style='secondary.TButton').pack(side=tk.RIGHT, padx=(0, 6))

    # 树表：文件 / 目录 / 大小 / 时间
    cols = ('name', 'dir', 'size', 'ts')
    tree = ttk.Treeview(frame, columns=cols, show='headings', height=10)
    for cid, text, w in (('name', i18n.tr('文件', 'File'), 220),
                         ('dir', i18n.tr('所在目录', 'Folder'), 260),
                         ('size', i18n.tr('大小', 'Size'), 80),
                         ('ts', i18n.tr('时间', 'Time'), 130)):
        tree.heading(cid, text=text)
        tree.column(cid, width=w, anchor=tk.W)
    tree.pack(fill=tk.BOTH, expand=True)
    gui._dl_tree = tree

    # 动作行
    # ★R216 阶段 2b：原 6 枚 pack(LEFT, padx=2) 各自按文字取宽 ⇒ 一排长短
    # 不齐、和上方树表左右边缘也对不上。改一条 12 分格 action_bar（每枚占
    # 2 格＝等宽铺满卡片宽），主操作实底、其余描边，层级一眼可辨。
    btns = ttk.Frame(frame)
    btns.pack(fill=tk.X, pady=(4, 0))
    ui_layout.action_bar(btns, [
        (i18n.tr('📂 打开所在文件夹', '📂 Show in Folder'),
         lambda: _show_selected(gui), 'primary.TButton'),
        (i18n.tr('✕ 忽略此条', '✕ Forget'),
         lambda: _forget_selected(gui), 'secondary.TButton'),
        # ★ R174 撤回入口（用户 -20/1：忽略此条不能反悔）
        (i18n.tr('↩ 撤回忽略', '↩ Undo Forget'),
         lambda: _undo_last_ignore(gui), 'secondary.TButton'),
        # ★ R160 迭代：手动添加 / 忽略规则 / 按目录统计
        (i18n.tr('➕ 手动添加', '➕ Add Manually'),
         lambda: _manual_add(gui), 'secondary.TButton'),
        (i18n.tr('🚫 忽略规则', '🚫 Ignore Rules'),
         lambda: _ignore_dialog(gui, colors), 'secondary.TButton'),
        (i18n.tr('📊 按目录统计', '📊 By Folder'),
         lambda: _stats_by_dir(gui, colors), 'secondary.TButton')],
        spans=(2, 2, 2, 2, 2, 2), align='fill', pad=SP['xs'])
    ttk.Label(btns, text=i18n.tr('双击行 = 打开文件 · 右键 = 更多操作',
                                 'Double-click = open · right-click = more'),
              foreground=colors['text_secondary'],
              font=ui_layout.app_font(9)).pack(anchor=tk.E)

    # R121 知情提示（用户定案）：读浏览器历史属敏感行为，分发对象应知情；
    # 强调仅本地读取、绝不上传
    ttk.Label(frame, text=i18n.tr(
        'ℹ 记录来自本机浏览器下载历史（Chrome/Edge）与下载目录监控，'
        '仅保存在本机，不上传网络。',
        "ℹ Records come from this PC's browser history (Chrome/Edge) and "
        "Downloads monitoring. Stored locally only, never uploaded."),
        foreground=colors['muted'], font=ui_layout.app_font(9),
        wraplength=690, justify=tk.LEFT).pack(fill=tk.X, pady=(4, 0))

    tree.bind('<Double-1>', lambda e: _open_selected(gui))
    tree.bind('<Button-1>', _on_tree_click)
    _attach_context_menu(tree, gui)   # ★ A5 右键菜单（取消忽略快捷入口）
    refresh(gui)
    return frame


def attach_tracker(gui, tracker):
    """主窗把 tracker 交给面板（iid→记录 映射初始化；refresh 会重建）"""
    tracker.records_by_path = {}
    gui.download_tracker = tracker


# ---------------------------------------------------------------------
# 动作
# ---------------------------------------------------------------------

def _on_tree_click(event):
    """单击折叠段父行（col 前缀）→ 展开/收起"""
    tree = event.widget
    try:
        iid = tree.identify_row(event.y)
    except Exception:
        return
    if iid and iid.startswith('col'):
        try:
            tree.item(iid, open=not tree.item(iid, 'open'))
        except Exception:
            pass


def _manual_add(gui):
    """手动添加下载记录（不受忽略规则限制；文件须存在）"""
    from tkinter import filedialog
    from pathlib import Path as _P
    ps = filedialog.askopenfilenames(parent=gui.root,
                                     title=i18n.tr('选择要记录的文件（可多选）',
                                                   'Pick files to record (multi)'))
    n = 0
    for p in ps:
        try:
            if gui.download_tracker.add_or_update(_P(p), src='manual'):
                n += 1
        except Exception as _e:
            logger.warning(f'手动添加失败 {p}: {_e}')
    refresh(gui)
    gui._log(f'➕ 手动添加下载记录 {n} 条')


def _ignore_dialog(gui, colors):
    """忽略规则管理：命中"文件名/路径子串"的新下载不再记录"""
    from tkinter import simpledialog
    tr = gui.download_tracker
    win = tk.Toplevel(gui.root)
    win.title(i18n.tr('下载记录忽略规则', 'Download Ignore Rules'))
    win.geometry('500x340')
    win.configure(bg=colors['bg'])
    win.transient(gui.root)
    win.grab_set()
    ttk.Label(win, text=i18n.tr(
        '规则=文件名或路径包含的子串（不区分大小写）。命中的新下载不再记录；'
        '手动添加不受限。例：临时、D:\缓存',
        'A rule is a substring of filename/path (case-insensitive). Matching '
        'new downloads are skipped; manual adds bypass it.'),
        foreground=colors['text_secondary'], wraplength=460,
        justify=tk.LEFT).pack(padx=12, pady=(10, 6), anchor=tk.W)
    lb = tk.Listbox(win, bg=colors['surface'], fg=colors['fg'],
                    relief=tk.FLAT, highlightthickness=0, height=8)
    lb.pack(fill=tk.BOTH, expand=True, padx=12)

    def _reload():
        lb.delete(0, tk.END)
        for s in tr.list_ignore():
            lb.insert(tk.END, s)
        # ★R222 阶段 4：与主窗列表同一口径的隔行底色
        ui_layout.zebra_listbox(lb, colors)

    _reload()
    var = tk.StringVar()
    sel_rec = _selected_record(gui)
    if sel_rec:
        var.set(sel_rec.get('dir', ''))
    row = ttk.Frame(win)
    row.pack(fill=tk.X, padx=12, pady=6)
    entry = ttk.Entry(row, textvariable=var)
    entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
    # ★ R176 修复：原 var.bind（StringVar 无 bind 方法）→ 点"忽略规则"
    # 按钮即 Tk 回调异常（用户"001.jpg 不能忽略"根因，日志 06:24 实锤）
    entry.bind('<Return>', lambda e: _add())

    def _add():
        s = var.get().strip()
        if s:
            tr.add_ignore(s)
            var.set('')
            _reload()

    ttk.Button(row, text=i18n.tr('添加', 'Add'), command=_add).pack(
        side=tk.LEFT, padx=(6, 0))

    def _del():
        sel = lb.curselection()
        if sel:
            tr.remove_ignore(lb.get(sel[0]))
            _reload()

    ttk.Button(win, text=i18n.tr('删除选中规则', 'Delete Selected'),
               command=_del).pack(anchor=tk.W, padx=12, pady=(0, 10))


def _stats_by_dir(gui, colors):
    """按目录统计：条数与合计大小，降序"""
    from collections import Counter
    recs = gui.download_tracker.records
    cnt = Counter(r.get('dir', '?') for r in recs)
    size = Counter()
    for r in recs:
        try:
            size[r.get('dir', '?')] += int(r.get('size') or 0)
        except Exception:
            pass
    win = tk.Toplevel(gui.root)
    win.title(i18n.tr('按目录统计', 'Stats by Folder'))
    win.geometry('640x380')
    win.configure(bg=colors['bg'])
    win.transient(gui.root)
    cols = ('dir', 'n', 'mb', 'pct')
    tree = ttk.Treeview(win, columns=cols, show='headings', height=14)
    for c, w, txt in (('dir', 360, '目录'), ('n', 60, '条数'),
                      ('mb', 100, '合计大小'), ('pct', 90, '占比(按条数)')):
        tree.heading(c, text=txt)
        tree.column(c, width=w, anchor=tk.W)
    tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
    total = sum(cnt.values()) or 1
    for d, n in cnt.most_common():
        d_disp = str(d).rstrip('\\/') or d
        mb = size[d] / 1048576
        # ★ R161 修复"统计看起来不正常"：大小只统计磁盘有文件的记录
        # （浏览器历史记录 size 常为 0，此前混进合计显示 0.0 假数据）；
        # 百分比列明确标注=按条数；目录尾斜杠归一。
        tree.insert('', 'end', values=(
            d_disp, n,
            f'{mb:.1f} MB' if size[d] else '—',
            f'{n * 100 // total}%'))
    ttk.Label(win, text=i18n.tr(f'共 {len(recs)} 条记录；"合计大小"只统计文件仍在磁盘的记录',
                                f'{len(recs)} records; size covers existing files only'),
              foreground=colors['muted']).pack(anchor=tk.W, padx=10,
                                                   pady=(0, 8))
    # ★R222 阶段 4：扁平表上隔行底色（下载卡是分组树，行有层级，铺纹会错位→不上）
    ui_layout.zebra_treeview(tree)


def _selected_record(gui):
    tree = gui._dl_tree
    sel = tree.selection()
    if not sel or sel[0].startswith(('grp', 'col')):
        return None
    return gui.download_tracker.records_by_path.get(sel[0])


def _open_dir(gui, d):
    """打开目录；目录也没了给提示（R123）"""
    from utils import i18n
    if d and os.path.isdir(d):
        try:
            subprocess.Popen(['explorer', d])
        except Exception as e:
            logger.warning(f"[下载记忆] 打开目录失败: {e}")
    else:
        messagebox.showinfo(
            i18n.tr('提示', 'Info'),
            i18n.tr('该文件夹已不存在（可能被移动、改名或删除）。',
                    'This folder no longer exists (moved, renamed or deleted).'),
            parent=gui.root)


def _show_selected(gui):
    # R122：选中折叠段父行（📁 N 个文件 → 目录）→ 直接打开该目录
    sel = gui._dl_tree.selection()
    if sel:
        d = getattr(gui, '_dl_fold_dirs', {}).get(sel[0])
        if d:
            _open_dir(gui, d)
            return
    rec = _selected_record(gui)
    if not rec:
        return
    # R123：文件可能已被剪切/移动/删除——先核对再打开
    if not os.path.exists(rec['path']):
        from utils import i18n
        if messagebox.askyesno(
                i18n.tr('文件不在了', 'File not found'),
                i18n.tr('该文件已不在当前文件夹（可能被剪切、移动、改名或删除）。\n仍要打开所在文件夹吗？',
                        'This file is no longer in its folder (moved, renamed or deleted).\nOpen the folder anyway?'),
                parent=gui.root):
            _open_dir(gui, rec.get('dir', ''))
        return
    try:
        subprocess.Popen(['explorer', '/select,', rec['path']])
    except Exception as e:
        logger.warning(f"[下载记忆] 打开所在文件夹失败: {e}")


# ★R264 审计 Z4（用户 18 批点头口径）：下载记录的 path 可能由「导入全部数据」
# 从别人给的备份包里带进来（导入端只保证"是 dict、路径去重"），而这里
# os.startfile ＝ 资源管理器双击语义，exe/bat/lnk 会被**真的运行**。
# 界面上只显示 name/dir 两列，伪造记录和真实下载看不出差别 ⇒ 可执行/脚本类
# 扩展名先弹确认，其余照旧直开（不牺牲"我自己下载了个安装包想双击运行"）。
_EXEC_EXTS = frozenset((
    '.exe', '.bat', '.cmd', '.com', '.msi', '.msp', '.lnk', '.scr',
    '.vbs', '.vbe', '.js', '.jse', '.wsf', '.wsh', '.ps1', '.psm1',
    '.hta', '.cpl', '.jar', '.reg',
    # ★R285-N8（P2）：补齐三类同样能"直接起程序"的旧格式——
    # .msc=管理单元（mmc 可带任意 snap-in）、.pif=旧式程序项（可指定任意 exe
    # 与命令行）、.scf=资源管理器命令（shell 命令文件，`Command=2` 之类可指向
    # 本地程序）。导入的备份包里塞这三个就绕过确认，与 .exe 同级风险。
    '.msc', '.pif', '.scf'))


def is_executable_record(path) -> bool:
    """这条下载记录双击会不会"执行代码"（判定口径见 _EXEC_EXTS）"""
    try:
        return os.path.splitext(str(path or ''))[1].lower() in _EXEC_EXTS
    except Exception:
        return False


def _open_selected(gui):
    rec = _selected_record(gui)
    if not rec:
        return
    # R123：双击打开也先核对文件还在不在
    if not os.path.exists(rec['path']):
        messagebox.showinfo(
            i18n.tr('文件不在了', 'File not found'),
            i18n.tr('该文件已不在原位置（可能被剪切、移动、改名或删除）。',
                    'This file is no longer there (moved, renamed or deleted).'),
            parent=gui.root)
        return
    # ★R264 Z4：可执行/脚本类 ⇒ 先问一句，把"要跑的就是这个文件"说明白
    if is_executable_record(rec['path']):
        if not messagebox.askyesno(
                i18n.tr('确认运行这个文件？', 'Run this file?'),
                i18n.tr('这是一个可执行程序/脚本：\n%s\n%s\n\n'
                        '双击会真的运行它。下载记录可能来自别人给的备份包，'
                        '不确定来源就别运行。',
                        'This is an executable/script:\n%s\n%s\n\n'
                        'Opening it will run it. Download records can come '
                        'from backups other people gave you - don\'t run it '
                        'if you are not sure of the source.')
                % (rec.get('name') or '', rec['path']),
                parent=gui.root):
            logger.info('[下载记忆] 用户取消了可执行文件的运行: %s'
                        % rec['path'])
            return
    try:
        os.startfile(rec['path'])
    except Exception as e:
        logger.warning(f"[下载记忆] 打开文件失败: {e}")


def _forget_selected(gui):
    tree = gui._dl_tree
    sel = tree.selection()
    if not sel:
        return
    # ★ R176（用户 -06/8）：折叠段父行=整目录一起忽略（全部入撤回栈）
    d = getattr(gui, '_dl_fold_dirs', {}).get(sel[0])
    if d:
        hits = [dict(r) for r in gui.download_tracker.records
                if r.get('dir', '') == d]
        for r in hits:
            gui.download_tracker.push_undo(r)
            gui.download_tracker.forget(r['path'])   # ★ R177 黑名单防复活
            gui.download_tracker.remove(r['path'])
        refresh(gui)
        gui._log(f'✕ 已忽略 [{d}] 下 {len(hits)} 条记录'
                 f'（点"↩ 撤回忽略"可找回）')
        return
    rec = _selected_record(gui)
    if not rec:
        return
    # ★ R175：入持久化撤回栈（用户 -20/5-1：原内存栈重启即丢，
    # "提示没有记录"——现在落盘 data/download_undo.json，重启不丢）
    # ★ R177：同时进黑名单——否则浏览器历史同步会把这条反复加回
    # （用户 001.jpg"点忽略刷新又出现"真凶 src=chrome）
    gui.download_tracker.push_undo(rec)
    gui.download_tracker.forget(rec['path'])
    gui.download_tracker.remove(rec['path'])
    refresh(gui)
    gui._log(f'✕ 已忽略此条（点"↩ 撤回忽略"可找回）：{rec.get("name", "")}')


def _undo_last_ignore(gui):
    """★ R175 撤回最近一次"忽略此条"（持久化栈，重启后仍可撤回）"""
    rec = gui.download_tracker.pop_undo()
    if not rec:
        messagebox.showinfo(
            i18n.tr('提示', 'Info'),
            i18n.tr('没有可撤回的记录（每次"忽略此条"都会入撤回栈）。',
                    'Nothing to undo.'),
            parent=gui.root)
        return
    gui.download_tracker.restore_record(rec)
    refresh(gui)
    gui._log(f'↩ 已撤回忽略：{rec.get("name", "")}')


def _attach_context_menu(tree, gui):
    """★ A5 右键菜单：打开/忽略/取消忽略（目录维度快捷入口，
    不用再进"忽略规则"对话框翻找）"""
    menu = tk.Menu(tree, tearoff=0)

    def _dir_of():
        """当前右键行对应目录：折叠段父行或记录行"""
        sel = tree.selection()
        if not sel:
            return None
        if sel[0] in getattr(gui, '_dl_fold_dirs', {}):
            return gui._dl_fold_dirs[sel[0]]
        rec = _selected_record(gui)
        return rec.get('dir', '') if rec else None

    def _do_ignore_dir():
        d = _dir_of()
        if d:
            gui.download_tracker.add_ignore(d)
            refresh(gui)
            gui._log(f'🚫 已忽略目录（新下载不再记录）：{d}')

    def _do_unignore_dir():
        d = _dir_of()
        if not d:
            return
        low = d.lower()
        hits = [s for s in gui.download_tracker.list_ignore()
                if s.lower() in low]
        if not hits:
            messagebox.showinfo(
                i18n.tr('提示', 'Info'),
                i18n.tr('该目录当前没有匹配的忽略规则。',
                        'No ignore rule matches this folder.'),
                parent=gui.root)
            return
        for s in hits:
            gui.download_tracker.remove_ignore(s)
        refresh(gui)
        gui._log(f'♻ 已取消忽略规则 {len(hits)} 条：{d}')

    menu.add_command(label=i18n.tr('📂 打开所在文件夹', '📂 Show in Folder'),
                     command=lambda: _show_selected(gui))
    menu.add_command(label=i18n.tr('✕ 忽略此条（删除记录）',
                                   '✕ Forget this entry'),
                     command=lambda: _forget_selected(gui))
    menu.add_command(label=i18n.tr('↩ 撤回上次忽略', '↩ Undo last forget'),
                     command=lambda: _undo_last_ignore(gui))
    menu.add_separator()
    menu.add_command(label=i18n.tr('🚫 忽略此目录', '🚫 Ignore this folder'),
                     command=_do_ignore_dir)
    menu.add_command(label=i18n.tr('♻ 取消忽略此目录',
                                   '♻ Unignore this folder'),
                     command=_do_unignore_dir)

    def _popup(event):
        iid = tree.identify_row(event.y)
        if not iid or iid.startswith('grp'):
            return
        tree.selection_set(iid)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    tree.bind('<Button-3>', _popup)


def _open_downloads_dir(gui):
    try:
        from core.download_tracker import _user_downloads_dir
        subprocess.Popen(['explorer', str(_user_downloads_dir())])
    except Exception as e:
        logger.warning(f"[下载记忆] 打开下载目录失败: {e}")


# ---------------------------------------------------------------------
# 刷新
# ---------------------------------------------------------------------

def refresh_sync(gui):
    """刷新按钮：先重建当前记录，再后台同步浏览器历史/Recent 后重绘"""
    import threading as _th
    refresh(gui)

    def _work():
        # ★R204 NEW-C8：跨线程回投统一走 _ui_queue（root.after 从工作线程
        # 调用本身是跨线程触 Tcl，R142 闪退同族）
        try:
            gui.download_tracker.scan_browser_history()
            # ★R190（Qoder CORE-6）：WScript.Shell COM 必须主线程
            # （R142 修了启动回填路，手动刷新这条漏了）
            gui._ui_queue.put(gui.download_tracker.scan_recent_lnk)
        except Exception as e:
            logger.warning(f"[下载记忆] 手动刷新同步失败: {e}")
        try:
            gui._ui_queue.put(lambda: refresh(gui))
        except Exception:
            pass
    _th.Thread(target=_work, daemon=True).start()


def refresh(gui):
    """重拉记录并重建树表（今天/昨天/本周/更早 分组 + 同目录折叠）"""
    from utils import i18n
    tree = gui._dl_tree
    tracker = gui.download_tracker
    tree.delete(*tree.get_children())
    tracker.records_by_path = {}
    fold_dirs = gui._dl_fold_dirs = {}   # R122：折叠段父行 iid → 目录（供"打开所在文件夹"）
    glabels = {'今天': i18n.tr('今天', 'Today'),
               '昨天': i18n.tr('昨天', 'Yesterday'),
               '本周': i18n.tr('本周', 'This week'),
               '更早': i18n.tr('更早', 'Earlier')}
    groups = tracker.grouped()
    total = sum(len(items) for _g, items in groups)
    n_dirs = len({r.get('dir', '') for r in tracker.records})
    try:
        gui._dl_summary.config(text=i18n.tr(
            f'共 {total} 条记录 · {n_dirs} 个目录',
            f'{total} records · {n_dirs} folders'))
    except Exception:
        pass
    i = 0
    for gname, items in groups:
        i += 1
        tree.insert('', tk.END, iid=f'grp{i}', open=True,
                    values=(f'── {glabels.get(gname, gname)} ({len(items)}) ──',
                            '', '', ''))
        for kind, run in fold_by_dir(items):
            if kind == 'fold':
                i += 1
                pid = f'col{i}'
                d = run[0].get('dir', '')
                fold_dirs[pid] = d
                total_size = sum(r.get('size', 0) or 0 for r in run)
                tree.insert('', tk.END, iid=pid, open=False, values=(
                    i18n.tr(f'📁 {len(run)} 个文件 → {d}',
                            f'📁 {len(run)} files → {d}'),
                    i18n.tr('（单击展开/收起）', '(click to expand/collapse)'),
                    _fmt_size(total_size), ''))
                for r in run:
                    i += 1
                    tracker.records_by_path[f'r{i}'] = r
                    tree.insert(pid, tk.END, iid=f'r{i}', values=(
                        '    ' + r.get('name', ''), r.get('dir', ''),
                        _fmt_size(r.get('size')),
                        str(r.get('ts', '')).replace('T', ' ')))
            else:
                for r in run:
                    i += 1
                    tracker.records_by_path[f'r{i}'] = r
                    tree.insert('', tk.END, iid=f'r{i}', values=(
                        r.get('name', ''), r.get('dir', ''),
                        _fmt_size(r.get('size')),
                        str(r.get('ts', '')).replace('T', ' ')))


def _toggle_group(gui, iid):
    """单击折叠段父行：展开/收起"""
    tree = gui._dl_tree
    try:
        tree.item(iid, open=not tree.item(iid, 'open'))
    except Exception:
        pass
