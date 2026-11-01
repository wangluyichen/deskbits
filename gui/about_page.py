# -*- coding: utf-8 -*-
"""ℹ️ 关于页 + 帮助窗口（★ R33：自 gui/main_window.py 拆出——分文件规范首个试点）。

纯展示模块，零业务逻辑：
- build_about_tab(tab_about, ...)   构建"关于"页（版本/作者/按钮/声明/二维码/链接）
- show_help_window(parent, ...)     详细帮助窗口（功能/用法/快捷键/新功能详解）
- open_readme(app_dir, log)         用系统默认程序打开 README.md

依赖注入：get_color(key, default)=实时取主窗口当前主题色（每次构建控件时
求值——主题切换后再打开关于页/帮助，颜色一定是当前主题的，浅色/护眼正确）、
app_dir=项目根 Path、log=gui 日志回调。本模块不得反向 import gui.main_window。
"""
import logging
import os
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from utils import i18n   # ★ R73：双语支持
from gui import ui_layout   # ★R228：界面字体等比缩放 helper

logger = logging.getLogger("desktop_manager")


def _read_version(app_dir: Path) -> str:
    version = '0.1'
    try:
        version = (Path(app_dir) / 'version.txt').read_text(
            encoding='utf-8').strip() or '0.1'
    except Exception:
        pass
    return version


# ---- ★ R192b 文字可读性适配（用户 17 批"详细帮助字看不清楚"）----
# 病根：dim 文字用 muted（深色主题 #9aa3b8 压 #44475a 对比度仅 ~2.6:1）。
# 按 WCAG 对比度把前景色自动调到可读——深底向亮调、浅底向暗调，
# 任何主题（含将来新增）自动对上，不再逐处手工挑色。

def _lum(hex_color):
    """WCAG 相对亮度"""
    h = hex_color.lstrip('#')
    def lin(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(int(h[i:i + 2], 16)) for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(c1, c2):
    l1, l2 = sorted((_lum(c1), _lum(c2)), reverse=True)
    return (l1 + 0.05) / (l2 + 0.05)


def _readable(fg_hex, bg_hex, target=4.5):
    """fg 在 bg 上对比度不足 target 时，朝锚点色（深底→白/浅底→黑）
    逐档混色直到达标；原色已达标则原样返回。"""
    fg_hex, bg_hex = fg_hex or '#000000', bg_hex or '#ffffff'
    if _contrast(fg_hex, bg_hex) >= target:
        return fg_hex
    anchor = '#ffffff' if _lum(bg_hex) < 0.5 else '#000000'
    a, b = fg_hex.lstrip('#'), anchor.lstrip('#')
    for t in range(1, 11):
        cand = '#' + ''.join(
            f'{round(int(a[i:i + 2], 16) * (1 - t / 10) + int(b[i:i + 2], 16) * t / 10):02x}'
            for i in (0, 2, 4))
        if _contrast(cand, bg_hex) >= target:
            return cand
    return anchor


def open_readme(app_dir, log=None):
    """用系统默认程序打开 README.md"""
    try:
        os.startfile(str(Path(app_dir) / 'README.md'))
    except Exception as e:
        logger.error(f'打开 README 失败: {e}')
        if callable(log):
            log(f'✗ 打开 README 失败: {e}')


def build_about_tab(tab_about, get_color, app_dir, log=None, on_help=None):
    """构建"关于"页（main_window._build_ui 调一行接入）。

    on_help=打开详细帮助的回调（gui 传 show_help_window 的包装）。"""
    app_dir = Path(app_dir)
    fg = get_color('fg', '#f8f8f2')
    border = get_color('border', '#636d83')
    # ★ R40：次要文字用 muted（浅色主题 border=#d0d7de 在白底上几乎隐形——
    # 用户实测"护眼/浅色下几乎看不见"的漏网处）；边框线仍用 border
    muted = get_color('muted', '#9aa3b8')

    about_frame = ttk.LabelFrame(
        tab_about,
        text=i18n.tr('🖥️ 拾碎-懒人桌面工具箱',
                     '🖥️ DeskBits'),
        padding='16 12', style='Card.TLabelframe')
    about_frame.pack(fill=tk.BOTH, expand=True)

    ttk.Label(about_frame,
              text=i18n.tr(f'版本 v{_read_version(app_dir)}',
                           f'Version v{_read_version(app_dir)}'),
              foreground=get_color('secondary', '#2ea043'),
              font=ui_layout.app_font(11, 'bold')).pack(anchor=tk.W, pady=(0, 2))
    ttk.Label(about_frame,
              text=i18n.tr('作者：浴火重生(networker)-DaLian',
                           'Author: 浴火重生(networker)-DaLian'),
              foreground=muted,
              font=ui_layout.app_font(9)).pack(anchor=tk.W, pady=(0, 10))
    ttk.Label(about_frame,
              text=i18n.tr(
                  '一句话用法：图标拖进分类 → 顶部悬浮条一键切换分类 → [全部] 恢复。\n'
                  '常用快捷键：Ctrl+Alt+E 悬浮窗开关 · Ctrl+Alt+0 显示全部 · '
                  'Ctrl+Alt+F 快速定位 · Ctrl+Alt+1~9 切分类',
                  'Quick start: drag icons into categories → switch with the '
                  'floating bar → [全部] restores all.\n'
                  'Hotkeys: Ctrl+Alt+E toggle floating bar · Ctrl+Alt+0 show '
                  'all icons · Ctrl+Alt+F quick find · Ctrl+Alt+1~9 switch '
                  'categories'),
              foreground=fg,
              font=ui_layout.app_font(10),
              justify=tk.LEFT).pack(anchor=tk.W, pady=(0, 12))
    ttk.Button(about_frame,
               text=i18n.tr('📖 打开详细帮助（功能/用法/更新记录）',
                            '📖 Open Help (Features / Usage / Changelog)'),
               command=(on_help or (lambda: None)),
               style='primary.TButton').pack(anchor=tk.W, pady=(0, 4))

    ttk.Button(about_frame,
               text=i18n.tr('📄 打开 README（项目说明/版本记录）',
                            '📄 Open README (Docs / Changelog)'),
               command=lambda: open_readme(app_dir, log),
               style='secondary.TButton').pack(anchor=tk.W, pady=(0, 4))

    # ---- ★ R30：声明与隐私（参考市面常用软件的免责范式）----
    # ★ R32：字号 8→9、颜色 border→fg（用户反馈字太小/主题下看不清）
    decl = ttk.LabelFrame(
        about_frame,
        text=i18n.tr('📌 声明与隐私', '📌 Disclaimer & Privacy'),
        padding='10 8', style='Card.TLabelframe')
    decl.pack(fill=tk.X, pady=(10, 0))
    ttk.Label(decl, justify=tk.LEFT,
              text=i18n.tr(
                  '【免责声明】本软件按"现状"免费提供，仅供个人学习、效率整理与测试使用。'
                  '因使用不当、误操作或环境差异造成的任何数据丢失、图标布局错乱或其他损失，'
                  '作者不承担相关责任——分发或大改动前请先用"📤 导出全部数据"备份，重要文件请另行备份。\n'
                  '【隐私声明】本软件完全离线运行：不联网、不上传任何数据，不收集密码与个人信息。'
                  '"统计"功能仅在本机记录图标的点击次数（data\\usage_stats.json 等本地文件），'
                  '仅用于点击率排行与显示规则优化；可在"📊 统计"页一键关闭采集或清零，'
                  '分发前可用"清空全部个人数据"一键抹除。\n'
                  '【杀毒软件提示】开启统计功能后，个别杀毒软件可能提示"全局鼠标监听"——'
                  '这是统计桌面图标双击/右键次数的正常工作方式；不需要统计时关闭该功能即可，'
                  '关闭后程序不再安装任何鼠标钩子。',
                  '[Disclaimer] This software is provided free of charge "as is", '
                  'for personal study, productivity and testing only. The author is '
                  'not liable for any data loss, icon-layout disorder or other damage '
                  'caused by misuse, misoperation or environment differences — back up '
                  'with "导出全部数据 (Export All Data)" before distributing or making '
                  'major changes, and keep separate backups of important files.\n'
                  '[Privacy] This software runs fully offline: no network access, no '
                  'data uploads, no passwords or personal information collected. The '
                  '"Statistics" feature only counts icon clicks locally (data\\'
                  'usage_stats.json etc.) for ranking and display-rule tuning; you can '
                  'turn it off or reset it on the 📊 Stats page, and wipe everything '
                  'with "清空全部个人数据 (Clear All Personal Data)" before '
                  'distribution.\n'
                  '[Antivirus note] With statistics enabled, some antivirus software '
                  'may flag the "global mouse listener" — that is how double-click/'
                  'right-click counting normally works. Turn the feature off if not '
                  'needed; no mouse hooks are installed while it is off.'),
              foreground=fg, wraplength=980,
              font=ui_layout.app_font(10)).pack(anchor=tk.W)

    # ---- 二维码（打赏 / 加好友）+ 主页链接 ----
    qr_row = ttk.Frame(about_frame)
    qr_row.pack(fill=tk.X, pady=(8, 0))

    root_win = tab_about.winfo_toplevel()   # 弹窗的 transient 父窗口

    def _qr_panel(path, label):
        panel = tk.Frame(qr_row, bg=get_color('surface', '#44475a'),
                         highlightthickness=1,
                         highlightbackground=border)
        panel.pack(side=tk.LEFT, padx=(0, 16), ipadx=8, ipady=4)
        tk.Label(panel, text=label, fg=fg, bg=get_color('surface', '#44475a'),
                 font=ui_layout.app_font(10, 'bold')).pack(pady=(2, 4))
        try:
            from PIL import Image, ImageTk
            img = Image.open(path)
            img.thumbnail((110, 110))
            photo = ImageTk.PhotoImage(img)
            lbl = tk.Label(panel, image=photo, bg='#ffffff', pady=4,
                           cursor='hand2')
            lbl.image = photo   # 保住引用防 GC
            lbl.pack(padx=4, pady=(0, 4))

            def _zoom(ev=None, _path=path, _label=label):
                """点击放大 3 倍查看（扫得清）"""
                win = tk.Toplevel(root_win)
                win.title(_label)
                win.configure(bg='#ffffff')
                win.transient(root_win)
                big = Image.open(_path)
                big = big.resize((big.width * 3, big.height * 3),
                                 Image.LANCZOS)
                if big.width > 360:
                    big.thumbnail((360, 360))
                ph = ImageTk.PhotoImage(big)
                tk.Label(win, image=ph, bg='#ffffff').pack(padx=10, pady=10)
                win.image = ph
                tk.Label(win, text=i18n.tr('点击图片关闭', 'Click to close'),
                         fg='#57606a', bg='#ffffff').pack(pady=(0, 8))
                win.bind('<Button-1>', lambda e: win.destroy())

            lbl.bind('<Button-1>', _zoom)
        except Exception as e:
            tk.Label(panel, text=i18n.tr('（图片缺失）', '(image missing)'),
                     fg=muted, bg=get_color('surface', '#44475a'),
                     font=ui_layout.app_font(9)).pack(pady=(0, 6))
            logger.error(f"关于页二维码加载失败: {e}")

    _qr_panel(app_dir / 'images' / 'qrcode_tip.png',
              i18n.tr('打赏', 'Donate'))
    _qr_panel(app_dir / 'images' / 'qrcode_friend.png',
              i18n.tr('加好友', 'Add Friend'))

    def _open_url(url):
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception as e:
            logger.error(f"打开链接失败 {url}: {e}")

    links_col = ttk.Frame(qr_row)
    links_col.pack(side=tk.LEFT, anchor=tk.N, padx=(4, 0), pady=(6, 0))
    ttk.Label(links_col,
              text=i18n.tr('主页与最新版下载：',
                           'Homepages & latest version:'),
              foreground=muted,
              font=ui_layout.app_font(9)).pack(anchor=tk.W, pady=(0, 2))
    for _text, _url in [
        ('B站主页', 'https://space.bilibili.com/404813020?spm_id_from=333.1007.0.0'),
        ('知乎主页', 'https://www.zhihu.com/people/wang-lu-yi-chen-66'),
        ('Gitee 主页', 'https://gitee.com/wanke2022'),
        ('Gitee 下载页', 'https://gitee.com/wanke2022/deskbits-release/releases'),
        # ★R209d（用户 13 批）：姊妹软件互推——剑听 IELTS 那边也已链回本软件
        ('剑听 IELTS', 'https://gitee.com/networker2026/jian-ting'),
        # ★R293（用户 2026-10-04 点名"在帮助关于里加上链接"）：软件合集（网盘分享页），
        # 做法与上面几枚一致——同一份表、同一只 `_open_url`，不新增第二条开窗路径。
        ('软件合集', 'https://pan.baidu.com/s/5vqUN3PgY6Ze_ByHrxMsK7A'),
        # ★R325（用户 2026-10-14 点名"github 那个发布仓可以加到帮助关于里面"）；
        # 2026-10-16 源码仓已开源并改用本仓 Releases 发布，故指向源码仓的 Releases 页。
        ('GitHub 下载页', 'https://github.com/wangluyichen/deskbits/releases'),
    ]:
        _lbl = ttk.Label(links_col, text=_text,
                         foreground=get_color('primary', '#8be9fd'),
                         cursor='hand2',
                         font=ui_layout.app_font(10, 'underline'))
        _lbl.pack(anchor=tk.W, pady=2)
        _lbl.bind('<Button-1>', lambda e, u=_url: _open_url(u))


def show_help_window(parent, get_color, app_dir):
    """详细帮助窗口：功能介绍、用法、快捷键、新功能详解、版本更新、打赏/加好友"""
    app_dir = Path(app_dir)
    fg = get_color('fg', '#f8f8f2')
    surface = get_color('surface', '#44475a')
    border = get_color('border', '#636d83')
    muted = get_color('muted', '#9aa3b8')   # ★ R40：次要文字用 muted（浅色可见）

    win = tk.Toplevel(parent)
    win.title(i18n.tr('❓ 帮助 / 关于', '❓ Help / About'))
    win.geometry('760x830')
    win.configure(bg=get_color('bg', '#282a36'))
    win.transient(parent)
    # ★ R192b（用户 17 批"窗口显示不全"）：高 DPI 缩放下定死 830 高会把
    # 底部二维码/关闭按钮挤出屏。构建完按内容自适应，超屏封顶。
    def _fit_to_content():
        win.update_idletasks()
        w = max(760, win.winfo_reqwidth())
        h = min(win.winfo_reqheight(), win.winfo_screenheight() - 60)
        win.geometry(f'{w}x{h}')

    version = _read_version(app_dir)

    head = ttk.Frame(win, padding='12 10')
    head.pack(fill=tk.X)
    ttk.Label(head,
              text=i18n.tr('🖥️ 拾碎-懒人桌面工具箱',
                           '🖥️ DeskBits'),
              font=ui_layout.app_font(16, 'bold'),
              foreground=get_color('primary', '#8be9fd')).pack(side=tk.LEFT)
    info = ttk.Frame(head)
    info.pack(side=tk.LEFT, padx=(15, 0))
    ttk.Label(info, text=i18n.tr(f'版本 v{version}', f'Version v{version}'),
              foreground=get_color('secondary', '#2ea043'),
              font=ui_layout.app_font(10, 'bold')).pack(anchor=tk.W)
    ttk.Label(info,
              text=i18n.tr('作者：浴火重生(networker)-DaLian',
                           'Author: 浴火重生(networker)-DaLian'),
              foreground=muted,
              font=ui_layout.app_font(9)).pack(anchor=tk.W)

    # 正文：功能 / 用法 / 更新记录（富文本）
    # ★ R32：新增多段新功能详解后内容超出一屏——补滚动条（原先没有）
    body_wrap = ttk.Frame(win)
    body_wrap.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 6))
    body = tk.Text(body_wrap, wrap='word', font=ui_layout.app_font(10),
                   bg=surface, fg=fg,
                   relief=tk.FLAT, padx=14, pady=10, height=15)
    body_scroll = ttk.Scrollbar(body_wrap, orient=tk.VERTICAL, command=body.yview)
    body.configure(yscrollcommand=body_scroll.set)
    body.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    body_scroll.pack(side=tk.RIGHT, fill=tk.Y)

    body.tag_configure('h1', font=ui_layout.app_font(12, 'bold'),
                       foreground=get_color('primary', '#8be9fd'),
                       spacing1=8, spacing3=4)
    # ★ R192b：h2/dim/kbd 全部经 _readable 保证在当前主题 surface 上可读
    # （dim 原 muted 在深底对比度 ~2.6:1="字看不清楚"的病根）
    body.tag_configure('h2', font=ui_layout.app_font(10, 'bold'),
                       foreground=_readable(get_color('secondary', '#2ea043'),
                                            surface, 4.0), spacing1=6)
    body.tag_configure('dim', foreground=_readable(
        get_color('muted', '#9aa3b8'), surface, 4.5))
    body.tag_configure('kbd', font=('Consolas', 10, 'bold'),
                       foreground=_readable(get_color('warning', '#f1fa8c'),
                                            surface, 4.5))

    def h1(t):
        body.insert(tk.END, t + '\n', 'h1')

    def h2(t):
        body.insert(tk.END, '· ' + t + '\n', 'h2')

    def line(t='', tag=''):
        body.insert(tk.END, t + '\n', tag or ())

    # ★ R73：英文模式下提示详细正文暂为中文（批二接入）
    if i18n.is_en():
        line('Note: the detailed help text below is Chinese-only in this '
             'version. English version coming in a later update.', 'dim')
        line('')
    h1('■ 主要功能')
    h2('分类切换桌面：把图标归类（程序/网络/英语/AI/公共/工作…），一键只显示某个分类，其余自动隐藏')
    h2('顶部悬浮横条：鼠标移到屏幕顶部自动出现/隐藏；点分类即切换，[全部]恢复全部图标、[窗]呼出主界面、[✕]退出程序')
    h2('首字母前缀自动归类：G=工作、GY=公共、GZ=工作、W/WL=网络、Y=英语、YL=娱乐、A=AI、L=临时（可在"拖拽分类→首字母规则"里自定义）')
    h2('图标高亮框：用彩色边框圈住一个分类的图标，多种主题，颜色/线宽/样式可调')
    h2('文件夹组一键打开：把常用资源管理器窗口保存为命名组，悬浮条"一键打开"或切分类时一键打开；Win11 22H2+ 自动合并为单窗口选项卡')
    h2('布局记忆 + 布局守护：保存/恢复整套图标位置；系统刷新后由布局守护自动拉回原摆放')
    h2('家长模式：桌面只留儿童分类图标，密码保护 + 密保找回，到时自动锁屏')
    h2('桌面亮度调节：硬件背光（实验性）或软件蒙版，降低亮度省电护眼、即时生效')
    h2('主题实时切换：深色 / 暗黑 / 浅色 / 护眼四套主题，切换即时生效、无需重启')
    h2('锁屏（自定义图片+独立密码）/ 事件查看器（大白话）/ 计划任务 / '
       '条件监视 / 一键维护 / 下载管理')
    h2('系统图标显隐 / 点击统计 / 排行榜显示 / 全局快捷键 / 布局与数据导入导出')

    h1('■ 快速上手')
    line('1. 点"🖱️ 拖拽分类"打开面板：按住图标（可多选）拖到右侧分类上松手', 'dim')
    line('2. 或先设好首字母规则，点"✨ 智能自动分类"一键归类', 'dim')
    line('3. 鼠标移到屏幕顶部 → 点横条上的分类名 → 桌面立刻切换', 'dim')
    line('4. 点横条 [全部] 恢复所有图标；Ctrl+Alt+0 同效', 'dim')
    line('5. 主界面"💾 保存布局"记住当前摆放，"↩️ 恢复布局"找回（支持删除/重命名）', 'dim')
    line('6. 快捷键可在"⌨️ 全局快捷键"面板自定义（Ctrl+Alt+1~9 切分类）', 'dim')
    line('7. Ctrl+Alt+F 打开快速定位，输入关键词即可跳到目标图标', 'dim')

    h1('■ 常用快捷键（默认）')
    line('Ctrl+Alt+E  悬浮窗 开/关', 'kbd')
    line('Ctrl+Alt+0  显示全部图标', 'kbd')
    line('Ctrl+Alt+F  快速定位图标（输入关键词，回车跳到目标）', 'kbd')
    line('Ctrl+Alt+1~9  切换分类（按横条槽位顺序）', 'kbd')

    # ★ R32：新功能详解（用户反馈详细帮助缺新功能/新用法）
    h1('■ 统计排行（📊 统计页）')
    h2('三份榜单：图标打开 Top / 文件夹打开 Top / 分类切换 Top，前三名带奖牌标记')
    h2('口径：桌面双击图标=打开；右键点在图标上也计入（近似）；'
       '文件夹组/资源页打开计入文件夹榜；点悬浮条切换计入分类榜')
    h2('实时上榜：统计页开着时，每次点击立刻刷新榜单')
    h2('统计开关（按钮行最右）：关闭后程序不再监视鼠标、数据保留不再新增；'
       '重启保持；分发版（exe）默认关闭')
    h2('榜单字号：按钮行 A− / A+ 可调（7~16），自动记忆')
    h2('数据只存本机 data\\usage_stats.json / data\\clicks.json，不联网不上传；'
       '🧹 清零归零，📤 导出 / 📥 导入 做 JSON 备份')
    h2('与规则联动：显示规则的 top/bottom_clicked 按分类点击数执行；'
       '统计关闭时规则基于历史数据（执行前会提醒）')

    h1('■ 数据管理（📦 数据管理页）')
    h2('导出/导入全部数据：分类+规则+前缀规则+设置+快捷键+高亮+文件夹组'
       '+布局存档+使用统计+分类点击，换机迁移/备份一步到位')
    h2('单独导出/导入：分类（JSON）、文件夹组（JSON）、布局打包（ZIP）、'
       '图标分类清单（TXT，导入按合并语义）')
    h2('清空三兄弟：清空图标分类（保留分类框架）/ 清空文件夹组 / '
       '清空全部个人数据（分发前抹痕迹，写盘后自动复核）')
    h2('导入有格式校验：选错文件会给明确提示，不会写坏现有数据')

    h1('■ 图标高亮框（🖍️ 规则页）')
    h2('三种框选范围：框住全部（分类+常驻）/ 只框分类 / 框住系统图标 / '
       '只框系统图标（后两类互斥，选中自动置灰另一项）')
    h2('形状主题：圆角立体 / 下划线 / 方框等，颜色/线宽/虚线/底部对齐可调')

    h1('■ 系统工具（🧰 系统页）')
    h2('事件查看器（大白话）：系统日志翻译成人话，按开关机/异常崩溃/服务故障/'
       '磁盘/驱动等一键筛选；可生成 Windows 事件查看器同口径过滤视图；'
       '双击看详情+同类事件+过滤脚本；错误红/警告黄与原生一致')
    h2('计划任务：图形化创建 定时/登录/开机/延时 任务（关机、重启、开软件、'
       '跑脚本、打开虚拟机）；列表带下次运行时间与动作大白话，支持编辑/启停/'
       '删除；自带懒人模板库')
    h2('条件监视：盯住某个进程，出现/消失时自动执行关机、重启、开软件、跑命令'
       '（典型：虚拟机关机后自动关主机）；随"导出/导入全部数据"迁移')
    h2('一键维护：清空回收站/临时文件/DNS 缓存等八项常用维护一键完成，'
       '成功失败都有弹窗回报')

    h1('■ 锁屏与安全（🔒）')
    h2('自带锁屏：每块显示器全屏覆盖，自定义图片+大时钟，解锁界面固定在主屏')
    h2('解锁方式优先级：锁屏独立密码（重启后自动清除，每次重设）→ 家长密码 → '
       '没密码时按住按钮 3 秒；忘记密码走家长密保找回')
    h2('锁定期间自动隐藏系统"注销"入口，解锁自动恢复；输入框自动聚焦，'
       '打字即校验，输对立即解锁')

    h1('■ 下载管理（⬇️ 下载页）')
    h2('自动记录浏览器下载文件（按路径去重、按目录统计大小）；误清理可"撤回"')
    h2('忽略规则/黑名单：防止 OneDrive 等同步盘把记录"复活"；'
       '随"导出/导入全部数据"迁移')

    h1('■ 首次使用向导（🧭）')
    h2('首次启动自动弹出：先备份桌面图标，再讲清每个功能在哪个页签、一键直达；'
       '以后可在 设置页"启动向导"随时重看')

    h1('■ 退出与恢复')
    h2('退出时恢复指定布局：可选"当前"（保持现状）或任一已存布局；'
       '保存布局后下拉框即时更新')
    h2('关窗保留悬浮框：主窗 X 隐藏后台运行，悬浮条[窗]呼出、[✕]真正退出')

    h1('■ 版本更新记录')
    # ★ P2-N6：版本号从 version.txt 动态读，避免与实际版本脱节
    line(f'v{version}  当前版本', 'dim')
    # ★ P1-3：不再逐条列举历史版本（易与实际脱节），改指向 README
    line('各版本功能更新记录见项目 README.md（打包版见随包"使用说明.txt"）', 'dim')

    body.configure(state='disabled')

    # 底部：二维码（加好友 + 打赏）
    # ★R204 NEW-B6：_fit_to_content 挪到底部组件全部 pack 完之后再调用
    # （此前先测量后放二维码，高 DPI 下量到的高度不含底部，仍被挤出）
    qr_frame = ttk.Frame(win, padding='6 2 12 10')
    qr_frame.pack(fill=tk.X)
    try:
        from PIL import Image, ImageTk
        has_pil = True
    except Exception:
        has_pil = False

    def add_qr(path, label):
        panel = tk.Frame(qr_frame, bg=surface,
                         highlightthickness=1,
                         highlightbackground=border)
        panel.pack(side=tk.LEFT, padx=8, pady=2, ipadx=6, ipady=4)
        # 文字说明放在图片上方、加亮加粗，即使图片缺失也能看到说明
        tk.Label(panel, text=label, fg=fg, bg=surface,
                 font=ui_layout.app_font(11, 'bold')).pack(pady=(2, 4))
        if not Path(path).exists():
            tk.Label(panel, text=i18n.tr('（图片缺失）', '(image missing)'),
                     fg=muted, bg=surface,
                     font=ui_layout.app_font(9)).pack(pady=(0, 6))
            return
        try:
            if has_pil:
                img = Image.open(path)
                img.thumbnail((150, 150))
                photo = ImageTk.PhotoImage(img)
            else:
                # ★R204 GUIB-9：必须 file= 关键字——位置参数被当成 -data
                # 必抛异常，无 PIL 环境下赞助二维码永不显示
                photo = tk.PhotoImage(file=str(path))
            lbl = tk.Label(panel, image=photo, bg='#ffffff', pady=4)
            lbl.image = photo
            lbl.pack()
        except Exception as e:
            logger.error(f"加载二维码失败: {e}")

    add_qr(app_dir / 'images' / 'qrcode_friend.png',
           i18n.tr('微信扫码加好友', 'Scan to add friend (WeChat)'))
    add_qr(app_dir / 'images' / 'qrcode_tip.png',
           i18n.tr('请作者喝杯咖啡 (打赏)', 'Buy the author a coffee (Donate)'))

    ttk.Button(win, text=i18n.tr('关闭', 'Close'),
               command=win.destroy).pack(pady=(0, 10))
    _fit_to_content()   # ★ R192b+R204 NEW-B6：底部齐了再按实际高度自适应
