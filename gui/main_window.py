#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
拾碎-懒人桌面工具箱（DeskBits）- 桌面图标管理工具
功能：点击统计、分类管理、顶部悬浮窗（自动隐藏已退役 R107）
"""

import sys
import os
import json
import logging
import re
import subprocess
import logging.handlers
import threading
import queue            # ★ P1-3：后台线程→UI 线程投递队列（Tk after 非线程安全）
import faulthandler
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, messagebox, simpledialog
from pathlib import Path
from datetime import datetime
import time
from typing import Optional, List, Dict

logger = logging.getLogger("desktop_manager")
import win32gui
import win32con
import win32api
import ctypes
from ctypes import wintypes

# 添加模块路径
# 打包模式(frozen)：APP_DIR = exe 所在目录；源码模式：项目根目录
# ★ R174 C5：先按旧口径算 APP_DIR 并插好 sys.path（直跑时 sys.path[0]
# 是 gui/，提前 import utils 会炸——07:59 六连崩根因），再取 app_paths
# 统一口径（Nuitka onefile 下修正为外层 exe 目录）
if getattr(sys, 'frozen', False):
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent.parent  # 项目根目录（gui的父目录）
sys.path.insert(0, str(APP_DIR))
try:
    from utils.app_paths import get_app_dir as _get_app_dir
    APP_DIR = _get_app_dir()
except Exception:
    pass   # sys.path 未就绪的极端环境退回旧口径，不拦启动

# ★ R98（2026-09-16 日志翻倍案根因）：本文件有两种身份——启动器直跑时是
# '__main__'，被 `from gui.main_window import X`（如 R90 固化序列、拖拽
# 取色弹窗）时是 'gui.main_window'。两个身份各执行一遍模块级代码 =
# 日志 handler 双挂（app.log/switch.log 每行翻倍，2026-09-16 win11 实测）
# + 模块级单例翻份。脚本身份抢先把自己注册成包内名字，让后续 import
# 全部命中同一份模块，文件只执行一次。
# （必须放在 sys.path.insert 之后：脚本直跑时 sys.path[0] 是 gui/，
#   不加项目根就找不到 gui 包。）
if __name__ == '__main__' and 'gui.main_window' not in sys.modules:
    try:
        import gui  # noqa: F401  确保父包先就位
        sys.modules.setdefault('gui.main_window', sys.modules[__name__])
    except Exception:
        pass

# ★R213（NEW5-A2）：--selfcheck 探针模式（自动测试 t_startup 专用）。
# 窗口与全部页签照常构建——那才是被测面；但一切"对用户机器的真实
# 副作用"跳过：监视引擎线程、桌面暗色蒙版恢复、全局热键注册、注销
# 守卫启动清扫（碰注册表）、隐藏图标启动恢复（改桌面文件属性）。
# 此前探针在用户机上真跑这些副作用，测试机=用户机时是隐患。
SELF_CHECK = '--selfcheck' in sys.argv


def box_startup_reclaim_ok(argv=None, frozen=None, self_check=None):
    """★R320：启动那趟"盒子对账"**只在真启动那一把跑**，测试进程一律不跑。

    为什么要有这一条闸：`_startup_box_reclaim` 会往**真桌面**搬文件，而它是挂在
    `after(6400)` 上的——`测试_ui/` 里十几套回归会真建主窗、进程常常活过 6 秒，
    `--selfcheck` 只挡得住带旗标的那几套（★R213 的同一条隐患：测试机=用户机）。
    认的三个真入口：打包 exe（`sys.frozen` / 引导器传的 `--gui`）、
    源码 `pythonw gui/main_window.py`（启动器源码模式与 启动.bat 都是这一条）。
    """
    a = list(sys.argv) if argv is None else list(argv)
    fz = bool(getattr(sys, 'frozen', False)) if frozen is None else bool(frozen)
    sc = bool(SELF_CHECK) if self_check is None else bool(self_check)
    if sc:
        return False
    if fz or '--gui' in a:
        return True
    head = os.path.basename(str(a[0] if a else '')).lower()
    return head in ('main_window.py', 'main_window.exe')


# ★R249 一2③（用户 26 批 02）：程序自动修桌面排序的那 0.7 秒里，用户常常以为
# "图标自己乱了"而去抓鼠标；看见鼠标被别的东西带着动，大多数人会松手让程序做。
# 所以口径定成：**连续三次**都想去动鼠标，才弹一次大白话说明（不然每次都弹＝新噪音）。
# 阈值只写这一处，弹完归零（判据见 测试_ui/test_r249_silent_sort_fix.py S8~S12）。
INTERFERE_TALK_AT = 3

from core.icon_manager import IconManager
from click_counter import ClickTracker
from core.category_manager import CategoryManager
from core.dock import init_dock
from core.desktop_dim import DesktopDimmer
from core.download_tracker import DownloadTracker   # ★ R112：下载目录记忆
from core.highlighter import (IconHighlighter, PRESET_COLORS, THEMES,
                         FRAME_SHAPES, DISPLAY_MODES, _hex_to_rgb)
import drag_drop   # ★R221：取模块引用做 set_colors 注入（下方 :288 附近）
from drag_drop import open_drag_drop_panel, get_panel
from utils.safeio import atomic_write_json
from utils import i18n   # ★ R73：双语支持（中文/English）
from utils import log_i18n   # ★R229（item2 方案 A）：日志英文显示层
from utils.hotkey_manager import HotkeyManager
from utils.parental_mode import ParentalMode
import quick_find   # ★R221：同上
from quick_find import open_quick_finder
from usage_stats import UsageStats          # ★ S 段/B18：使用统计（数据层）
from stats_capture import StatsCaptureManager  # ★ R31：统计采集开关（新功能分文件）
from stats_panel import build_stats_tab     # ★ S 段/B18：统计页签（展示层）
from gui.about_page import build_about_tab, show_help_window  # ★ R33：关于页（分文件）
from gui import hotkey_panel  # ★ R140：热键面板（分文件，codeSplitter 一刀）
from gui import settings_panel  # ★ R142：设置页面板（第二刀：悬浮窗设置+公共分类显示）
from gui import manage_panel    # ★ R144：管理页面板（第三刀，Mixin）
ask_category_color = manage_panel.ask_category_color   # drag_drop 局部 from-import 兼容
from gui import rule_panel    # ★ R145：规则页面板（第四刀，Mixin）——须在 rank_display/rule_quickpick 之后
from gui import theme_panel   # ★ R146：主题面板（第五刀）
from gui import kids_panel    # ★ R147：家长模式面板（第六刀，Mixin）
from gui import sys_panel     # ★ R159：系统相关（锁屏/事件/计划任务）
from gui import ui_layout     # ★R216 阶段2b：布局骨架 helper（资源页按钮排）
from gui.ui_layout import SP
from gui import icons        # ★R225 阶段5：页签图标字体层（MDL2/emoji 两档）
from gui import lock_screen   # ★ R166：锁屏画面与守卫
from gui import desktop_box_panel   # ★R301（批57）：📦 桌面盒子卡（资源页）
from gui import wizard        # ★ R160：首次使用向导
from gui.data_manage_page import build_data_manage_tab  # ★ R43：数据管理页（分文件）
from gui.log_page import build_log_tab  # ★ R43：日志页（分文件）
from gui import rule_quickpick  # ★ R65-Bug7：规则页当前分类快捷下拉
from gui import rank_display    # ★ R106：排行榜显示（按统计榜显示名次区间）
from gui import download_panel  # ★ R112：下载记录卡片（资源页）


# ★ R77：高亮区五下拉双语显示层（模式/颜色/形状/样式/方式）。
# 存储与 highlighter 内部匹配一律用中文 canonical 值（兼容旧 highlight.json
# 和 core/highlighter.py 的主题值），仅下拉框显示文案随语言切换。
# ★ R145 规则页拆分（第四刀）：高亮框显示层迁 gui/rule_panel
_hl_t = rule_panel._hl_t
_hl_c = rule_panel._hl_c
# ★ R142 设置页拆分（第二刀）：显示层 helper 迁至 gui/settings_panel；
# 模块级别名保持管理页流程/启动回填等既有调用点零改动
_cr_disp = settings_panel.cr_disp
_cr_canon = settings_panel.cr_canon
_fd_disp = settings_panel.fd_disp
_fd_canon = settings_panel.fd_canon
# ★R280-A：悬浮栏渐变三档的显示层（表本体在 core/dock.GRADIENT_LEVELS）
_gd_disp = settings_panel.gd_disp
_gd_canon = settings_panel.gd_canon


# ★ R146 主题拆分（第五刀）：显示层迁 gui/theme_panel
_UI_THEME_EN = theme_panel._UI_THEME_EN
_UI_THEME_INV = theme_panel._UI_THEME_INV
_theme_disp = theme_panel._theme_disp
_theme_canon = theme_panel._theme_canon
# 日志自动清理：启动时删除超过保留天数的日志文件（默认 60 天）
# ---------------------------------------------------------------------------


from gui.log_page import auto_clean_logs as _auto_clean_logs  # ★ R43 拆分
from gui.log_page import trim_log_text   # ★R226（P2-3）：界面日志封顶


# ★ P0-3 残余：_auto_clean_logs 也是**顶层裸调**，且其 `data_dir.glob(...)`
# 在函数内层 try 之外——若 data/ 目录存在但不可读（受限 ACL / 父目录只读），
# Path.glob 会抛 PermissionError → 顶层层裸调直接让模块导入失败，
# 症状与 P0-3 完全一样（--gui 弹 gui_fatal.log 退出）。与 switch handler、
# _setup_diagnostics 同源，一并包 try，绝不拦截启动。
try:
    _auto_clean_logs()
except Exception:
    pass

# ★ R146 主题拆分：COLORS 真身在 gui/theme_panel（同一 dict 对象）
COLORS = theme_panel.COLORS
UI_THEMES = theme_panel.UI_THEMES
apply_ui_theme = theme_panel.apply_ui_theme


def _group_list_width(names, minimum=10, maximum=30):
    """组多选列表的字符宽：按最长组名折算，CJK 记 2 个半角、其余记 1，
    再留 2 个半角余量（选中条/滚动条），并夹在 [minimum, maximum] 内。

    ★R259（用户 51 批 二1b）：原先列表 `fill=X expand=True` 把"程序""电脑"
    这种两字组名拉成整行宽，界面显得空。
    """
    def _half_width(s):
        return sum(2 if ord(c) > 0x2E80 else 1 for c in str(s))
    longest = max([_half_width(n) for n in names] or [0])
    return max(minimum, min(maximum, longest + 2))


def _group_list_height(n, minimum=2, maximum=6):
    """组多选列表的行数：按组数收着给，夹在 [minimum, maximum]。

    ★R261（用户 52 批）：原先写死 `height=4`，两三个组时底下空两行显假。
    保底 2 行（0 行的列表看着像坏了），封顶 6 行（组多也不把卡撑爆——
    纵向滚动条用户没点名，暂不加）。
    """
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 0
    return max(minimum, min(maximum, n))


def _top_k_display(value):
    """配置里的 top_k ⇒ 下拉框显示值：数字原样，其余（含 'all'/缺键）⇒「全部」。

    ★R261 修的是个显示 bug：落盘存 `'all'`、下拉候选写的是「全部」，
    于是老配置直接在下拉框里露出英文 `all`（用户截图点名"那个 all 真的丑"）。
    """
    s = str(value if value is not None else '').strip()
    if s.isdigit():
        return s
    return i18n.tr('全部', 'all')


def _disp_clip(text, cap=22):
    """榜单/组名的显示截断：CJK 记 2、半角记 1，超 cap 折断加 '…'。

    ★R262（用户 62 批"部分 ui 按钮看不全"）：榜单里出现过 571px 的巨名
    （超长文件夹名、日志 zip 名），而三列各只分到 ~330px ⇒ 溢出裁切。
    只截**显示**——勾选变量键与落盘配置仍是全名，打开行为不受影响。
    """
    s = str(text)
    w = 0
    for i, ch in enumerate(s):
        w += 2 if ord(ch) > 0x2E80 else 1
        if w > cap - 1:
            return s[:i] + '…'
    return s


def _settings_int(raw, default, key=''):
    """从 `_app_settings` 取整数的唯一口径：脏值回退默认并留日志。

    ★R264 审计 Z2/Z6：`config/app_settings.json` 是**外部输入**——①官方
    推荐用户手改（app_bootstrap.py:62-64），②「导入全部数据」对 app_settings
    是整包 `update()`、不做内容校验（gui/data_manage_page.py）。裸 `int()`
    落在**主窗构建链**上时，一个 `"八成"`/`None` 就 ValueError ⇒ 主窗建不起来
    ⇒ launcher 2 秒重启、300 秒 5 次后放弃，程序彻底打不开（只能手工改配置救活）。
    所以启动路径的取数必须"脏值不炸"，且如实记一行日志（`except: pass` 那种
    静默兜底会让下一轮排查重新靠猜，★R230b）。
    """
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning('设置项 %s 不是整数（%r），已按默认值 %s 运行'
                       % (key or '?', raw, default))
        return default


class DesktopManager(manage_panel.ManageMixin, rule_panel.RuleMixin,
                    theme_panel.ThemeMixin, kids_panel.KidsMixin):   # ★ R144~R147
    def __init__(self):
        self.lang = 'zh'

        # ★R286-A1（挂账 O1 方案②）：上一会话有没有停在"问桌面没返回"的那一发。
        # 必须早于本会话任何一次跨进程调用——自己一旦写了留痕，上一会话那条
        # "在飞"就不再是文件末段，现场会被自己冲掉。这里只取证，上屏排在
        # _build_ui 之后（日志页还没建，先建后说）。
        try:
            from utils.win32_safe import consume_unfinished
            self._trail_stale = consume_unfinished()
        except Exception as e:
            self._trail_stale = None
            logger.warning(f'[取证] 上次会话现场读取失败（不影响使用）: {e}')

        # 初始化管理器（IconManager 里会开 DPI 感知，必须先于横幅日志）
        config_dir = APP_DIR / 'config'
        self.icon_manager = IconManager(str(APP_DIR / 'icon_backups'),
                                        str(config_dir))

        # 启动横幅：以后排查"自动退出/图标跑位"从这里看起
        try:
            ver = (APP_DIR / 'version.txt').read_text(encoding='utf-8').strip() or '0.1'
        except Exception:
            ver = '0.1'
        # ★R246（用户 38 批 item3）：留一份给标题栏用——同一版本号改了很久时，
        # 用户/win11 那台只看标题栏就能判定手上跑的是哪一次构建（0.98-R246）。
        self.app_version = ver
        logger.info(f"===== 程序启动 v{ver} pid={os.getpid()} =====")
        # ★ R185（TRAE #5）：虚拟屏 metrics 用 win32con 命名常量（76~79）
        logger.info(
            f"屏幕: 主屏 {win32api.GetSystemMetrics(win32con.SM_CXSCREEN)}x"
            f"{win32api.GetSystemMetrics(win32con.SM_CYSCREEN)}, "
            f"虚拟屏 origin=({win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN)},"
            f"{win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN)}) "
            f"size={win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN)}x"
            f"{win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN)}")
        self.click_tracker = ClickTracker(str(APP_DIR / 'data'))
        self.usage_stats = UsageStats(str(APP_DIR / 'data'))   # ★ S 段/B18
        # ★ R112：下载目录记忆——Recent 回填 + Downloads/桌面监听（daemon）
        self.download_tracker = DownloadTracker(str(APP_DIR / 'data'),
                                                log=self._log)
        download_panel.attach_tracker(self, self.download_tracker)
        self.download_tracker.start()   # ★ R142：Recent 回填改主线程调度，见启动回填块

        # ★ R166：条件监视引擎（进程出现/消失 → 关机/重启/命令；
        # 典型用法=监视虚拟机进程，VM 停了自动关主机）
        from core.condition_monitor import ConditionMonitor
        self.monitor = ConditionMonitor(str(APP_DIR / 'config'), log=self._log)
        if not SELF_CHECK:
            self.monitor.start()
        # ★R214：应用设置读取+语言就位必须早于 CategoryManager——出厂
        # 分类名在播种/迁移时就要按当前语言落（用户冒烟：English 下默认
        # 分类仍中文，须手动"恢复默认"才对）。R73"构建界面前就位"进一步
        # 前移到"构建数据前"；下方原读取点改为复用，不再二次读盘
        self._app_settings = self._load_app_settings()
        i18n.set_language(self._app_settings.get('ui_language', '中文'))
        self.category_manager = CategoryManager(str(config_dir))
        self.highlighter = IconHighlighter(self.icon_manager)
        self._load_highlight_settings()
        # ★ P1-3：后台线程 → UI 线程投递队列。所有后台线程（重建缓存、
        # 打开文件夹组）回调到 UI 时不再直接 root.after（Tk 官方不保证线程
        # 安全），统一 put 到本队列，主线程 _drain_ui_queue 50ms 轮询消费。
        self._ui_queue: "queue.Queue" = queue.Queue()

        # 创建主窗口（必须先创建，再初始化 dock）
        self.root = tk.Tk()

        self.highlighter._tk_root = self.root  # 用于延迟刷新
        # ★R246：标题栏带上版本构建标注（`v0.98-R246`）。同一版本改很多批时，
        # 用户看一眼标题栏就知道自己跑的是哪一次构建（38 批 item3 的原话诉求）。
        # 只改标题字符串：单实例用的是互斥锁名字、不靠窗口标题（见 app_bootstrap），
        # 所以加后缀不会让"唤醒已有实例"失效。
        # ★R308 批64（用户"实在不行，把『分类-布局-悬浮条』这几个字移动到窗体那一格，
        # v0.99-R024 后面，这样就省出来空间给『收纳：属性隐藏』"）：那行装饰说明
        # 从头部行搬进标题栏。标题栏文字不参与 `_chrome_need_width()` 的量尺
        # ⇒ 顶部两簇需求 1206 → 1061，正好落回 1080 的预算里（不再触发降级、
        # 收纳档下拉也不会被挤）。
        self.root.title('%s  v%s  ·  %s' % (
            i18n.tr('🖥️ 拾碎-懒人桌面工具箱', '🖥️ DeskBits'), self.app_version,
            i18n.tr('分类 · 布局 · 悬浮条', 'Categories · Layout · Bar')))
        self.root.resizable(True, True)
        self.root.configure(bg=COLORS['bg'])
        # Tk 回调异常只记日志不退出（历史上"点一下就闪退"的根源之一）
        self.root.report_callback_exception = self._on_tk_callback_exception

        # 初始化悬浮窗（传入 root 用于定时器），并按分类配置同步顶部槽位
        self.dock = init_dock(
            self.icon_manager, self.category_manager,
            self.click_tracker, str(config_dir), tk_root=self.root
        )
        # ★R304-2（真机 R023 日志 L444→L470）：槽位上限必须**先于**第一次 sync_slots
        #   落到 dock。旧顺序这一发用的是 dock 自带的出厂默认 12，等
        #   `_apply_app_settings` 把用户存的 10 推进来再同步一次——同一次启动于是报了
        #   **两份不一样**的"没放下"名单（先 ['公共'] 后 ['工具','AI','公共']）；
        #   ★R300-F3 那道去重按名单比，名单变了就当新情况又说一遍，看着像"震荡"。
        #   设置字典这里已在 :296 读过盘（★R214 语言就位的同一份），不再二次读。
        self.dock.max_slots = _settings_int(
            self._app_settings.get('dock_max_slots', 12), 12, 'dock_max_slots')
        self.dock.sync_slots(self.category_manager.get_all_categories())


        self.root.attributes('-topmost', False)

        # 桌面亮度（夜间护眼）：按保存的亮度值恢复全桌面暗色蒙版
        # ★ R69：硬件 DDC 已整体移除（存档于 代码存档/亮度硬件DDC_不可用_
        # 20260915/），软件蒙版是唯一路径；post_fn/mode_changed_cb 保留，
        # 供界面回显"软件蒙版/无蒙版"终态。
        self.dimmer = DesktopDimmer(str(APP_DIR / 'config'))
        self.dimmer.post_fn = lambda cb: self.root.after(0, cb)
        # ★ 界面配置收敛读取：全程只读一次盘，挂到 self._app_settings 后，
        #   后续（dimmer 恢复 / 主题 / 字体 / 应用设置）全部复用同一 dict，
        #   避免 4 次盘读 + JSON 解析。
        #   注意：dimmer 恢复需 desktop_brightness，主题需 ui_theme，字体需
        #   ui_font_size——都在这一次读进来的 dict 里。
        self._app_settings = self._load_app_settings()
        # ★R266（批21 item1）：程序图标（默认 images/app_icons/01_B_拾碎成序）。
        # 设不上就沿用系统默认那一枚，只留一行日志——绝不因为图标把启动路径炸掉。
        self._apply_app_icon()
        # ★ R73：语言状态要在构建任何界面文字之前就位（标签页/按钮文案
        # 都是构建时求值），切换语言后重启程序完全生效。
        # ★R214：实际执行点已提前到 CategoryManager 构造之前（出厂分类
        # 名播种同样按语言求值），此处只复用不再重复读盘/set。
        i18n.set_language(self._app_settings.get('ui_language', '中文'))
        _saved = self._app_settings
        # ★ R65-Bug6 / R69：蒙版作用显示器（None=全部 / 序号=只盖该台）。
        # 设置键沿用旧名 desktop_hw_scope，用户已保存的选择不丢。
        self.dimmer.scope = _saved.get('desktop_hw_scope', None)
        if SELF_CHECK:
            logger.info('[selfcheck] 跳过桌面暗色蒙版恢复')
        else:
            try:
                # ★ R98：启动按每台显示器"自己的记忆"铺蒙版（无专属记忆退"全部"
                #   记忆，再无退 fallback——旧数据 scope=None 时以
                #   desktop_brightness 兜底，保持旧版升级行为不变）
                _dim_fb = int(_saved.get('desktop_brightness', 100)) \
                    if self.dimmer.scope is None else 100
                self.dimmer.restore_levels(
                    _saved.get('dim_scope_levels', {}), fallback=_dim_fb)
            except Exception as e:
                logger.error(f"恢复桌面亮度失败: {e}")

        # 界面主题：必须在 _setup_styles/_build_ui 之前应用，
        # 否则 ttk 样式和控件仍按默认深色创建；悬浮条同步换肤。
        _saved_theme = self._app_settings.get('ui_theme', '深色')
        apply_ui_theme(_saved_theme)
        self.root.configure(bg=COLORS['bg'])
        try:
            self.dock.apply_theme(_saved_theme, COLORS)
        except Exception:
            pass

        # ★R225 阶段5：图标风格 + 界面密度——和主题同一时序约束，必须早于
        # _setup_styles（Treeview rowheight）与 _build_ui（SP/页签文字）。
        self._apply_display_prefs()

        # ★R228（用户 22 批 item6）：界面字号交给 ui_layout 的**命名字体别名**
        # 机制统一管——必须早于 _setup_styles / _build_ui（两处都在构建期调
        # ui_layout.app_font 取别名），否则别名没宿主只能退回元组、又变回"改不动"。
        # ★R264 审计 Z2：这行在**主窗构建链**上且无 try，脏 ui_font_size 会让
        # 整个程序打不开（见 _settings_int）。同段 :324/:805/:840 早包了 try，
        # 唯独启动路径这两处裸奔——统一走读取端类型收敛。
        self.ui_font_px = _settings_int(
            self._app_settings.get('ui_font_size', 11), 11, 'ui_font_size')
        ui_layout.init_fonts(self.root, self.ui_font_px)

        # 界面字体：命名字体，改大小全界面即时生效（列表统一使用）
        self.ui_font = tkfont.Font(family='Microsoft YaHei UI',
                                   size=self.ui_font_px)

        # ★ R31：统计采集管理器（stats_capture.py，新功能分文件）——
        # 开关状态/三路记录入口/启停都收敛在独立模块，gui 只做接线。
        # 须在 _build_ui 之前就绪（统计页构建时读开关状态做按钮初始文案）
        self.stats_capture = StatsCaptureManager(
            self.dock, self.usage_stats, self._app_settings,
            self._save_app_settings, self._log,
            lambda: self._ui_queue.put(self._refresh_stats_if_visible))

        # 设置样式
        self._setup_styles()


        # ★ R140：热键配置须先于 _build_ui 加载——面板构建时 update_list 要读
        self._hotkey_cfg = hotkey_panel.load_config()

        # 构建 UI
        manage_panel.set_colors(COLORS)   # ★ R144：Mixin 主题色引用（须在 _build_ui 前）
        rule_panel.set_colors(COLORS)     # ★ R145
        kids_panel.set_colors(COLORS)     # ★ R147
        # ★R221（NEW7-P0-1/2）：quick_find / drag_drop 也各有一份私有兜底色板，
        # R215 改名 text_secondary 时漏补 → 快速定位框、拖拽分类面板一构建就
        # KeyError（两处主功能全死）。兜底表已补齐，这里再接上真身：这两个窗口
        # 从此跟随当前主题（原来硬编码深色，浅色主题下仍是黑窗）。
        quick_find.set_colors(COLORS)
        drag_drop.set_colors(COLORS)
        self._build_ui()

        # ★R286-A1：日志页建好了，把"上次会话停在未返回的那一发"上屏。
        # 原始那行（hwnd/msg/参数形状）已经在 consume_unfinished() 里按
        # WARNING 落进 app.log，这里只给用户看得懂的一句 + 文件位置。
        if getattr(self, '_trail_stale', None):
            self._log('⚠ 上次运行结束在一条未返回的桌面调用上'
                      '（进程被系统掐断，通常是 Explorer 侧的问题）'
                      '——详情看 data/win32_trail.log')

        # 高亮设置重载：控件此时才存在，首次加载在 _build_ui 前会被跳过
        try:
            self._load_highlight_settings()
        except Exception as e:
            logger.error(f'高亮设置重载失败: {e}')

        # 窗口尺寸自适应内容：保证所有按钮完整显示（不再写死 900x800 截断按钮）
        self._reposition_window()
        # ★R304-2：紧接着把"这台机生效的缩放 + 顶部两排的尺寸账"记成一行
        self._log_scale_diagnostics()

        # ★R315（批71，挂账 #272）：勾选框不再补文字前缀——原生指示器回到样式层
        # （gui/theme_panel.py 的"色块档"：勾选=整块 primary，笔画与块同色），
        # manage_panel._mark_checkboxes 那套 ☑/☐ 已随本批退役。
        # ★R251 item4（用户 43 批）：单选钮那一族仍靠「● / ○」文字前缀
        # （选中态的颜色在 theme_panel 的样式表里）。
        self._mark_radios(self.root)

        # 把主窗口句柄告诉高亮框（判定桌面是否被自己遮挡时排除自己）
        try:
            self.highlighter.own_tk_hwnd = int(
                win32gui.GetParent(self.root.winfo_id()))
        except Exception:
            pass

        # 悬浮窗日志回调（_build_ui 之后 log_text 才存在）并启动轮询
        self.dock._log_cb = self._log
        # ★R221（NEW7-P1-1）：--selfcheck 此前只挡热键，不挡这五处"真桌面副作用"
        # ——dock.start() 会在**用户桌面**上弹一条真悬浮栏、apply_startup 挂全局
        # 鼠标钩子、ensure_desktop_view_visible 会改 Explorer 窗口状态、
        # start_monitor/_start_show_event_poll 起后台线程。UI 套件是带
        # --selfcheck 跑的，于是"跑一遍测试=桌面上多一条横条+一套钩子"。
        if SELF_CHECK:
            logger.info('[selfcheck] 跳过悬浮栏启动/统计钩子/桌面自愈/监视线程')
        else:
            self.dock.start()
            # ★ B18/R17：全局鼠标监听（图标打开统计）——★ R31 启停收敛到
            # stats_capture.apply_startup（按持久化开关决定，exe 默认关）
            self.stats_capture.apply_startup()
            # ★ R102 黑桌面防线：启动自愈——若桌面图标窗口曾被 SW_HIDE
            # （自动隐藏已退役，见 代码存档/自动隐藏_整层SW_HIDE版_R107退役_*；
            #  防线保留：异常路径仍可能藏窗口），启动时无条件确保可见
            self.icon_manager.ensure_desktop_view_visible()
            # ★R255（46 批 item④-2）：打包态把软件快捷方式放桌面——极端情况
            # （强杀后黑桌面被亮回）下给用户一个手动入口。
            # ★R281（用户 08 批 item1）：记号从"建过没有"换成"建给哪个 exe"，
            # 所以**升级会原地刷新**那枚快捷方式（旧布尔记号让它冻在 R007 上，
            # 用户读成"不建快捷方式了"）；用户自己删掉的仍然不重塞（桌面主权）。
            # ★R280-C：建链已挪进子进程（本行所在的主线程**不再调 COM**——
            # R008 那条原生 AV 就是在主线程里 Dispatch('WScript.Shell') 崩的，
            # Python 的 try/except 拦不住原生崩溃）。这里的 try/except 只兜
            # "起子进程/落盘记号"这类可捕获异常；失败只留痕不影响启动（★R230b）。
            try:
                from utils.desktop_shortcut import ensure_desktop_shortcut
                ensure_desktop_shortcut(self)
            except Exception as _sce:
                logger.warning(f"桌面快捷方式接线异常（不影响启动）: {_sce}")
        self._drain_ui_queue()                 # ★ P1-3：启动 UI 队列消费

        # 全局快捷键（独立线程，必须 _build_ui 之后以便输出日志）
        # ★ R140：逻辑在 gui/hotkey_panel.py，这里只做接线
        # （_hotkey_cfg 已在 _build_ui 前加载）
        self.hotkey_mgr = HotkeyManager()
        if SELF_CHECK:
            # ★R213（NEW5-A2）：只画列表，不注册全局热键、不起轮询线程
            hotkey_panel.update_list(self)
            logger.info('[selfcheck] 跳过热键注册/轮询')
        else:
            hotkey_panel.apply_hotkeys(self)
            hotkey_panel.update_list(self)
            hotkey_panel.start_poll(self)

        # 应用设置（关窗保留横条等）
        # ★ 收敛读取：_build_ui 期间已把运行时状态写入 self._app_settings 的内存键
        #   （如 ui_tab / ui_font_size / cat_pane_width 等），这里若重读盘会覆盖它们
        #   ——原"重读"逻辑其实是潜在 bug。直接复用同一 dict，保留内存改动。
        self._apply_app_settings()
        # 主窗口默认显示（用户要求：启动就要看到窗口；关闭窗口是否保留横条
        # 由"保留横条后台运行"设置决定，关窗后隐藏，横条[窗]/双击启动可唤出）

        # 图标框位置变化检测循环（后台监视线程读数据，UI 线程只应用+绘制）
        # ★R221（NEW7-P1-1）：同 dock/统计——--selfcheck 下不起这些会真动
        # 桌面（画框/藏图标）的监视线程，也不抢单实例唤醒事件。
        if SELF_CHECK:
            logger.info('[selfcheck] 跳过框监视线程/单实例轮询')
        else:
            self._start_frame_refresh_loop()
            self.highlighter.start_monitor(2.0)

            # 单实例唤醒事件轮询（第二次双击启动.bat → 弹出本实例主窗口）
            self._start_show_event_poll()

        # 悬浮窗工具按钮回调：显示主窗口 / 退出程序
        self.dock.on_show_window = self._show_main_window
        self.dock.on_exit_app = self._exit_from_dock
        # 可见性变化（分类切换隐藏/恢复）→ 强制高亮框重绘 + 同步主窗口分类选中
        self.dock.on_visibility_changed = self._on_dock_cat_switched
        # ★ B13（根因修复）：dock.highlighter 从未被注入——dock.py 里写明
        #   "由 gui.py 注入"但 gui 漏了，dock.highlighter 一直是 None，
        #   悬浮条切分类的高亮分支（highlight_names）自始从未执行
        #   （app.log 零条"已高亮"为证），分类切换永远不画框，全靠
        #   "框住全部"模式掩盖；"只框分类"也因此无效。
        self.dock.highlighter = self.highlighter
        # ★ S 段/B18：统计采集回调（新增注入——已记入注入对账清单 R13）
        # ★ R31：注入直接指向 stats_capture（stats_capture.py），gui 不再承载记录逻辑
        self.dock.on_desktop_icon_open = self.stats_capture.record_icon_open
        from core import explorer_tabs as _et_mod
        _et_mod.on_folder_opened = self.stats_capture.record_folder_open
        # 切换完成 → 写 switch.log
        self.highlighter.category_manager = self.category_manager
        self.dock.on_switch_done = self._log_switch
        # 悬浮条 一键打开：打开当前组；分类切换联动同名组
        self.dock.on_open_folders = self._on_open_explorer_folders
        # ★R262 批11 联动：菜单顶部"⚡ 按自动打开设置"→ 按三列计划一键执行
        self.dock.on_run_auto_open = lambda: self._startup_open_folders(manual=True)
        self.dock.on_switch_link_folders = self._on_switch_link_folders

        # 家长模式
        self.parental = ParentalMode(
            self.dock, self.icon_manager, self.category_manager,
            str(config_dir), log=self._log)
        self.parental.on_exit_request = self._on_kids_exit_request
        self.parental.on_kids_end = self._on_kids_end
        # ★ R160：家长到时锁屏 → 自带全屏锁屏画面（不再用系统锁屏）；
        # 向导在 UI 全就位后 2 秒弹出（wizard_done 未设置时）
        self.parental.lock_cb = lambda: sys_panel.lock_now(self)
        # ★ R166：锁屏守卫崩溃自恢复（上次进程被杀没恢复"注销"入口时）
        if SELF_CHECK:
            logger.info('[selfcheck] 跳过守卫/隐藏图标启动恢复')
        else:
            lock_screen.recover_guard_on_startup(self)
        wizard.maybe_show(self, COLORS)


        self.dock.on_kids_show_all = self.parental._switch_to_kids

        # 若家长模式仍在生效期（防止孩子重启程序绕过），自动恢复
        # （注意：recover 在 resume 之前，先把上次遗留的隐藏图标恢复，再按需重新隐藏）
        # ★R213（NEW5-A2）：selfcheck 下不改桌面图标属性
        if not SELF_CHECK:
            try:
                self.icon_manager.recover_hidden_on_startup()
            except Exception as e:
                logger.error(f"启动恢复隐藏图标失败: {e}")

        # 若用户开启了"显示受保护的操作系统文件"，收起的图标会半透明露出
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                               r'Software\Microsoft\Windows\CurrentVersion'
                               r'\Explorer\Advanced')
            ssh, _ = winreg.QueryValueEx(k, 'ShowSuperHidden')
            if ssh:
                self._log('⚠ 检测到你开启了"显示受保护的操作系统文件"，'
                          '被分类收起的图标会以半透明显示；'
                          '建议在文件夹选项里关闭它，收起的图标才彻底不可见')
        except Exception:
            pass
        try:
            if self.parental.resume_if_active():
                self.root.after(800, self.root.withdraw)
                self._kids_resumed = True   # 防绕过：跳过"启动恢复上次分类"
        except Exception as e:
            logger.error(f"恢复家长模式失败: {e}")

        # 加载数据
        self._load_data()

        # 启动续守护：上次守护的布局若还在，自动恢复并继续守护
        # （配合"切分类套用同名布局"，实现每个分类桌面整理一次、长期保持）
        # ★ 2026-09-02：若退出时选择"保持当前分类"，启动后直接恢复上次分类，
        #   不再强套旧守护布局（用户实测"退出重进三次图标乱"的根源之一）
        try:
            last_slot = self._app_settings.get('last_active_slot', '')
            restore_layout = (self._app_settings.get('close_restore_layout', '') or '当前')
            restore_enabled = bool(self._app_settings.get('close_restore_enabled'))
            _kids = getattr(self, '_kids_resumed', False)
            # ★ 家长模式恢复中绝不恢复上次分类——否则会把非儿童图标重新
            #   显示出来，孩子重启程序即可绕过（2026-09-05 实测复现已修）
            # ★ R71：上次分类是空分类时不自动恢复——recover 刚把图标全部
            # 恢复可见，再激活空分类等于开机就清空桌面（用户以为图标丢失）。
            # 目标没内容就保持全可见，只记日志。
            if (not _kids and restore_enabled and restore_layout == '当前'
                    and last_slot
                    and not self._close_restore_target_has_content(last_slot)):
                logger.info(f"[启动] 上次分类 [{last_slot}] 是空分类且无布局"
                            "存档，跳过自动恢复（防桌面全空）")
                return_skip_guard = True
            elif (not _kids and restore_enabled and restore_layout == '当前'
                    and last_slot):
                logger.info(f"[启动] 退出恢复=当前：启动后恢复上次分类 [{last_slot}]")
                # ★R312 item3（真机 R029 启动日志 17:10:31）：这条链里 dock 会照例
                #   套一次布局（[全部]＝core/dock.py:2349-2363；普通分类＝开了
                #   "切分类套用同名布局"才套，:1797-1801）。摆没摆要在这里先记下来，
                #   1800ms 那次"启动自动套用"（★R241）读到"已经摆过"就必须收手——
                #   修前它看不见这件事，把一份**毫不相干的旧守护布局**（那台机器上
                #   是「排行 21-40 名」）盖在刚排好的桌面上 ⇒ 用户读成"启动后还是乱，
                #   点一下全部才排好"。
                self._startup_chain_laid = self._startup_chain_will_lay(last_slot)
                # 开机恢复分类不联动开文件夹（用户要求移除"开机自动打开"）：
                # 3 秒静默窗盖住 恢复(0.9s)+联动(1.7s) 两次触发
                import time as _t0
                self._link_suppress_until = _t0.time() + 3.0
                # ★R313 E1（P1）：★R213/★R221 那批把监视线程/蒙版/热键/钩子都挡了，
                #   可这四条"真动桌面"的排程留在 __init__ 里没配旗标——`自动测试.py`
                #   的 t_startup 让探针活 8 秒 > 900ms，于是**每跑一次测试，用户桌面上
                #   就真被摆一次布局**（测试机＝用户机时是隐患，且与上面"探针跳过一切
                #   真实副作用"的注释自相矛盾）。
                if SELF_CHECK:
                    logger.info('[selfcheck] 跳过启动恢复上次分类（探针不摆真桌面）')
                else:
                    self.root.after(900, lambda: self._reactivate_last_slot(last_slot))
                return_skip_guard = True
            elif (not _kids and restore_enabled and restore_layout != '当前'
                  and not self._close_restore_target_has_content(restore_layout)):
                # ★ R71：指定的恢复目标是空分类且无同名布局存档 → 跳过
                logger.info(f"[启动] 退出恢复目标 [{restore_layout}] 为空分类"
                            "且无布局存档，跳过自动套用（防桌面全空）")
                return_skip_guard = True
            elif (not _kids and restore_enabled and restore_layout != '当前'):
                # ★ 方案B：指定了具体布局（如"工作"），启动后自动套用该布局。
                #   修复：此前启动恢复逻辑只处理了 '当前'，指定布局时被忽略，
                #   只有关闭那一刻套用、重启后不恢复 → 布局"过夜又变回"。
                logger.info(f"[启动] 退出恢复=指定布局：启动后套用布局 [{restore_layout}]")
                import time as _t0
                self._link_suppress_until = _t0.time() + 3.0
                if SELF_CHECK:
                    logger.info('[selfcheck] 跳过启动套用指定布局（探针不摆真桌面）')
                else:
                    self.root.after(900, lambda nm=restore_layout: self._apply_layout_by_name(nm))
                return_skip_guard = True
            else:
                return_skip_guard = False
        except Exception as e:
            logger.error(f"[启动] 读取上次分类失败: {e}")
            return_skip_guard = False
        try:
            last = self._app_settings.get('last_guard_layout', '')
            if return_skip_guard:
                logger.info("[启动] 跳过旧守护布局强套（改为恢复上次分类）")
            elif SELF_CHECK:
                # ★R313 E1：这条是直接 `restore_layout(last)`——不是排程晚一点动，
                # 是**探针一起来就按盘上那份旧守护摆一次桌面**（还带 verify 补发）。
                logger.info('[selfcheck] 跳过启动续守护布局恢复（探针不摆真桌面）')
            elif self._app_settings.get('layout_guard', True) and last                     and os.path.exists(last):
                # ★ R100：单次恢复——内置 [verify]/[verify2] 读回验证+偏差补发，
                # 原"双次恢复+智能等待"第二遍冗余
                r = self.icon_manager.restore_layout(last)
                matched1 = r.get('matched', 0)
                logger.info(f"[启动续守护] 恢复匹配: {matched1}")

                if r.get('success'):
                    logger.info(f"启动续守护：已恢复上次的布局 {last}")
        except Exception as e:
            logger.error(f"启动续守护失败: {e}")

        # ★R241（用户 33 批 item2）：可选"启动时自动套用布局"（默认关）。
        # 排在 1800ms——晚于分类恢复的 900ms，显隐先落定再摆位置；先摆再
        # 切显隐会被后面的显示/隐藏改动打乱。开关只在勾选时排程，
        # 没勾就不给 root 挂任何多余回调。
        try:
            if bool(self._app_settings.get('apply_layout_on_launch', False)):
                import time as _t1
                self._link_suppress_until = _t1.time() + 3.0
                self.root.after(1800, self._auto_apply_layout_on_launch)
        except Exception as e:
            logger.warning(f'[启动] 自动套用布局未排程: {e}')

        # 启动定时刷新
        self._refresh_timer = None
        self._start_refresh_timer()

        # 绑定关闭事件
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)

        # ★R232（用户 06 批 item1）：「打字不上」诊断采样器（**只读观察**，
        # 不注入按键、不抢前台；开关 app_settings['typing_diag_log']，默认开）。
        # 采样线程不许碰 Tcl ⇒ 窗口可见性由主线程在这里回灌。
        try:
            from utils import typing_diag
            typing_diag.set_ui_state(minimized=(self.root.state() != 'normal'))
            typing_diag.start(self._app_settings)
            self.root.bind('<Map>', self._on_win_visibility, add='+')
            self.root.bind('<Unmap>', self._on_win_visibility, add='+')
        except Exception as e:
            logger.warning(f"[启动] 打字诊断采样未启用（不影响功能）: {e}")

        # 按当前字号重新算一次窗口尺寸（上面续守护 restore 会改图标位置、
        # 但不会改 UI；字号切换时会主动调，这里保证初始启动就对）
        try:
            self._reposition_window()
        except Exception:
            pass

    def _on_win_visibility(self, event=None):
        """★R232：把"主窗口此刻是不是被最小化/隐藏"回灌给诊断采样器。

        采样线程不能碰 Tcl，所以状态由主线程写标志（`Map`/`Unmap` + 托盘
        withdraw 都会走到这里）。这是分清 H1（键卡住）与 H2（前台被拿走）
        的关键背景——用户报的现象正是"把程序最小化后就能正常打字"。
        """
        try:
            from utils import typing_diag
            typing_diag.set_ui_state(minimized=(self.root.state() != 'normal'))
        except Exception:
            pass

    def _diag_event(self, tag: str):
        """★R232：给诊断采样器打"事件锚点"（此后 5 秒内的前台跃变才是线索）。

        没有锚点，日志里就只有"用户日常切窗口"的噪声；有了锚点，下次复现时
        能直接回答"切分类那一刻，前台到底归了谁"。异常一律吞：埋点不许带走功能。
        """
        try:
            from utils import typing_diag
            typing_diag.mark_event(tag)
        except Exception:
            pass

    # ---------------- ★R303：缩放/大字号下顶部两排的自适应 ----------------
    # 病灶（用户 59 批截图 + 探针 r303_u9 实测）：Windows 缩放把 `tk scaling` 从
    # 1.333 抬到 1.667 ⇒ 磅值字体在像素上长 25%，而窗口宽是**像素**上限
    # `min(1080, sw-160)`——内容涨、窗口不涨。125% 叠加「界面字号=大」时标题栏要
    # 1301px 而窗口只有 1080 ⇒ pack 把最后到的「悬浮窗」压到 1px（截图里右上角那
    # 个空框）；页签排要 1064px 而可用正好 1064 ⇒ 最后一枚「关于/About」裁在边框。
    # 历史：★R250 只把**编辑弹窗**改成"按内容量尺寸"，主窗顶部从未管过——
    # ★R262 账本 §六 挂着的"125% 缩放第二块机"就是这条，不是"修好又坏"。
    _HDR_PAD = 32          # header 的 padding 左右 12+12，再加 8 余量
    _tab_compact = False   # 第④级降级（页签只显文字）当前是否生效

    def _chrome_need_width(self):
        """⇒ 标题栏两簇**同排**完整显示所需的窗口宽（0=控件还没建好）。

        按子件 reqwidth 现算、不读 header 自己的：右簇挪到第二行后
        `header.winfo_reqwidth()` 会变成"最宽那一行"，拿它当需求会越量越小。"""
        left = getattr(self, '_hdr_left', None)
        ctrl = getattr(self, '_hdr_ctrl', None)
        if left is None or ctrl is None:
            return 0
        try:
            lw = left.winfo_reqwidth()
            # ★R308：这里**不再**补那行装饰副标题的宽度——它已经搬进窗口标题
            # （`self.root.title`），头部行没有它了。旧写法要补是因为
            # `_hdr_sub` 平时会被第①级降级 `pack_forget()` 掉，此刻不在 pack 树上
            # ⇒ 左簇 reqwidth 天生少算 145px（★R305 那把"同一尺子"的来龙去脉）。
            return lw + ctrl.winfo_reqwidth() + self._HDR_PAD
        except Exception:
            return 0

    def _tab_margin(self, i, default):
        """TNotebook 的 tabmargins 某一边的像素值（读不到就回 default）。"""
        try:
            mg = ttk.Style(self.notebook).lookup('TNotebook', 'tabmargins')
            return int(mg[i]) if mg and len(mg) > i else default
        except Exception:
            return default

    def _tabs_have_gap(self):
        """页签区右端是否还留着空白 ⇒ 整排没被挤的判据。

        两条都是 u12/u14 探针实测纠正过来的，别再改回去：
        ① **问点要问在页签区内侧那一端**。ttk 放不下时不是"裁掉留个缺口"，
          而是把每一枚**压缩**（125%+大：tab0 自然 100px，窗口 1309/1000/860/700
          时分别是 100/88/75/61），压完的整排正好**顶满**页签区右端——末枚右沿
          恒等于 `宽 - 右 tabmargins - 1`，四档分毫不差。贴着窗口右沿问（旧的
          `w-2`）落在 12px 的 tabmargins 里，永远回空串 ⇒ 判据恒真、第三级
          降级一次都没跑（u12 里 pad 全程 14 就是这么来的）。
        ② **一条 y 会漏读整枚页签**。页签形状上沿有斜切，`tabmargins[1]+4` 这一刀
          上第 3 枚「资源」x=312..397 整段回空串（y≥18 才探得到，u14）；所以
          纵向扫三刀，任一命中即算"顶满"。
        """
        nb = getattr(self, 'notebook', None)
        if nb is None:
            return True
        try:
            w = nb.winfo_width()
            if w <= 2:
                return True
            edge = w - self._tab_margin(2, 12) - 1
            if edge <= 1:
                return True
            top = self._tab_margin(1, 8)
            for dy in (4, 12, 20):
                y = top + dy
                for x in (edge, edge - 3, edge - 7):
                    if x > 1 and nb.tk.call(str(nb), 'identify', 'tab', x, y) != '':
                        return False
            return True
        except Exception:
            return True

    def _apply_tab_pad(self):
        ttk.Style(self.notebook).configure(
            'TNotebook.Tab', padding=ui_layout.pad(ui_layout.tab_pad_h(), 8))

    def _set_tabs_compact(self, on):
        """第④级降级开关：页签文字在「图标+词」与「只有词」之间整排来回切。

        只在 `_tab_text` 那份登记表里换，绝不自己拼图标（口径见 `_tab_text`）。
        `icons.is_tab_text` 本来就认"无图标"这一形（★R230），统计页那两处比较
        不会因为这里摘了前缀而脱钩。幂等：已经是目标态就直接返回。
        """
        on = bool(on)
        if on == self._tab_compact:
            return 0
        nb = getattr(self, 'notebook', None)
        if nb is None:
            return 0
        forms = getattr(self, '_tab_forms', {})
        back = {v: k for k, v in forms.items()}
        moved = 0
        for idx in range(nb.index('end')):
            try:
                cur = str(nb.tab(idx, 'text'))
            except Exception:
                continue
            nxt = (forms if on else back).get(cur, cur)
            if nxt != cur:
                try:
                    nb.tab(idx, text=nxt)
                    moved += 1
                except Exception:
                    pass
        self._tab_compact = on
        try:
            nb.update_idletasks()
        except Exception:
            pass
        return moved

    def _log_scale_diagnostics(self):
        """★R304-2：启动期记一行"这台机实际生效的缩放 + 顶部两排的尺寸账"。

        为什么非要有：win11 那台机报"按钮被压扁/页签被裁"，本机 100% 缩放
        复现不了，而旧日志**一个字的缩放都没记**——排查只能靠用户口述"我开着
        125%"。`tk scaling` 是 Windows 缩放的直接读数（1.333≈100%、1.667≈125%、
        2.0≈150%），配上窗口宽与标题栏需求宽，一行就能分清"是内容涨了"
        还是"窗口没跟上"。
        """
        try:
            sc = self.root.winfo_fpixels('1i') / 72.0
            wide = self.root.winfo_width()
            if wide <= 1:            # 还没映射，winfo_width 回 1 ⇒ 读请求几何
                wide = int(str(self.root.winfo_geometry()).split('x')[0])
            nb = getattr(self, 'notebook', None)
            # 这一整句写成一条字面量、不拆两行：★R232 的 B8b 逐条扫源码里的字符串
            # 字面量去过词典，拆行只会扫到前半句，词典里那条整句规则永远匹配不上它
            # ——报出来的是假红，不是真的漏翻。
            logger.info('界面缩放诊断: tk scaling=%.3f 约=%d%% 窗口宽=%d 标题栏需求=%d 页签=%d枚(内边距%d) 字号=%d',
                        sc, int(round(sc * 100.0 * 72.0 / 96.0)), wide,
                        self._chrome_need_width(),
                        nb.index('end') if nb is not None else 0,
                        ui_layout.tab_pad_h(), self.ui_font_px)
        except Exception as e:
            logger.warning(f'界面缩放诊断没记上: {e}')

    def _on_root_configure(self, event=None):
        if event is not None and event.widget is not self.root:
            return
        pending = getattr(self, '_refit_after', None)
        if pending:
            try:
                self.root.after_cancel(pending)
            except Exception:
                pass
        try:
            self._refit_after = self.root.after(150, self._refit_chrome)
        except Exception:
            self._refit_after = None

    def _refit_chrome(self):
        """按当前窗口宽重排顶部两排（★R303）。

        三级降级，100% 标准档一条都不触发（★R308 实测：标题栏需求 1061 < 1080、
        页签排 848 < 1064）：
        ① 右簇挪第二行（动作按钮永远完整可点）
        → ② 页签内边距从 14 按 2px 一档收到 6
        → ③ 收到头仍挤就把页签图标摘掉只留文字（探针 r306_tab4_probe：125%+大字
           900px 那一档省 170px，约 740px 以上都能重新腾出留白）。
        （原第①级"装饰副标题让位"随 ★R308 退役——那行字搬进窗口标题了，
          头部行不再有为它腾地方的问题。）"""
        self._refit_after = None
        if getattr(self, '_refitting', False):
            return
        nb = getattr(self, 'notebook', None)
        if nb is None or getattr(self, '_hdr_ctrl', None) is None:
            return
        try:
            avail = self.root.winfo_width()
            if avail <= 1:
                return
            self._refitting = True
            ctrl = self._hdr_ctrl
            # ★R308：装饰副标题搬进窗口标题后，头部行只有**一份**需求——
            # ★R305/★R306 那对"两把尺子"（full 含装饰 / lean 不含）连同第①级
            # 降级（让副标题让位）一起退役。留着它会在 `_hdr_sub` 不存在时
            # 抛 AttributeError，被外层 try 吞成"这一轮整条自适应没跑"。
            need = self._chrome_need_width()
            # ① 挤就把右簇挪到第二行：宁可高一行，不可把按钮压成 1px
            #    （grid 的列跨度不写回 1 会留在上一次的 2，看着没事、其实占位错了）
            if need <= avail:
                if self._hdr_wrapped:
                    ctrl.grid(row=0, column=1, columnspan=1, sticky='e')
                    self._hdr_wrapped = False
            elif not self._hdr_wrapped:
                ctrl.grid(row=1, column=0, columnspan=2, sticky='e')
                self._hdr_wrapped = True
            # ② 页签排：窗口宽回来了就先放回设计档，再一档一档收到放得下
            #    （★R304-2：第③级同理——先放回「图标+词」，够不够宽让循环去判）。
            #    `had_compact` 是"这一轮是不是从降级态回来的"：拖着窗口在窄处微调时
            #    每 150ms 就重排一次，装回去→放不下→再摘掉这条链会每次都走一遍，
            #    日志只该在**真的跨过那道坎**时说一次（★R232 提示反复出现同款病）。
            had_compact = self._tab_compact
            if had_compact:
                self._set_tabs_compact(False)
            if ui_layout.tab_pad_h() < ui_layout.TAB_PAD_DESIGN:
                ui_layout.set_tab_pad_h(ui_layout.TAB_PAD_DESIGN)
                self._apply_tab_pad()
                nb.update_idletasks()
            while not self._tabs_have_gap():
                cur = ui_layout.tab_pad_h()
                if cur <= ui_layout.TAB_PAD_MIN:
                    if not self._tab_compact:
                        # ③ 内边距已收到头：摘掉页签图标只留文字，一枚省 8~17px
                        moved = self._set_tabs_compact(True)
                        if not had_compact:
                            logger.warning('页签内边距收到最紧一档(%d)还挤，再降一级：'
                                           '页签只显文字（摘掉 %d 枚图标前缀），'
                                           '窗口宽 %d、共 %d 枚',
                                           ui_layout.TAB_PAD_MIN, moved, avail,
                                           nb.index('end'))
                        continue
                    logger.warning('页签排在最紧一档(%d)仍放不下：窗口宽 %d 装不下 '
                                   '%d 枚页签（缩放/字号叠加的物理极限）',
                                   ui_layout.TAB_PAD_MIN, avail, nb.index('end'))
                    break
                ui_layout.set_tab_pad_h(cur - 2)
                self._apply_tab_pad()
                nb.update_idletasks()
        except Exception as e:
            logger.warning(f'顶部自适应重排失败(已忽略): {e}')
        finally:
            self._refitting = False

    def _reposition_window(self):
        """重算主窗口几何（字号切换 / 首启都会调用）。

        2026-09-02 选项卡布局后内容不再决定窗口高度，按屏幕比例取
        居中尺寸，上限 980x720 保证一屏放下、不溢出。
        ★R303：宽度改成"至少放得下标题栏两簇"——缩放/大字号下内容涨了
        窗口跟着涨（仍以屏为准），否则 pack 会把右簇最后那枚按钮压成 1px。
        ★R306 的 `_chrome_width_budget()`（需求扣掉装饰副标题）随 ★R308 退役：
        那行字搬进窗口标题后，`_chrome_need_width()` 量到的就是**真需求**，
        不再存在"为一句装饰把窗口开宽"的空间。"""
        try:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            budget = self._chrome_need_width()
            w = min(max(1080, budget), max(880, sw - 160))   # 优先 1080，小屏留 160px 边
            h = min(860, max(640, sh - 120))
            x = max(0, (sw - w) // 2)
            y = max(0, (sh - h) // 2)
            # ★ minsize 不能超过算出的 w/h，否则 ≤1024 屏窗口被撑出屏幕
            self.root.minsize(min(w, 1080), min(h, 680))
            self.root.geometry(f'{w}x{h}+{x}+{y}')
            self.root.update_idletasks()
            self._refit_chrome()
        except Exception as e:
            logger.error(f"重算窗口尺寸失败(已忽略): {e}")

    # ★R305（批61 U1/U6）：「收纳方式」开关——双属性隐藏 / 装进盒子收纳。
    # 用户 2026-10-07 点名："开关不放悬浮条，放主窗口的显眼位置，因为这个比较重要。"
    # 这一档只管**下一次**切分类/排行榜显示/家长模式怎么落地；回调里一个文件
    # 操作都没有（M3），搬不搬由 `core/visibility_router.py` 在切换那一刻决定——
    # 当场批量搬文件会让"改个设置"变成"未知耗时的桌面大手术"。
    # 选项词只留四个汉字：宽度是这里唯一的成本（见 _build_pack_mode_switch 的预算）。
    _PACK_MODE_OPTS = (('属性隐藏', 'Hide attrs', 'hide'),
                       ('盒子收纳', 'Pack boxes', 'box'))

    def _build_pack_mode_switch(self, parent):
        """顶部左簇尾巴上的收纳档下拉（紧挨应用名，两档都摆得下）。

        ★R303 那道尺寸闸管着这一簇（100%+标准：窗口宽必须还是 1080）。标题栏需求
        = 左簇 + 右簇 + 32，实测不带这个开关时是 940（100%+标准）；★R308 把装饰
        副标题搬进窗口标题后，1080 下含开关的需求是 1061（旧写法 1206，超预算 ⇒
        下拉一直被挤）。所以词按最短档写、下拉宽度按当前语言的选项**现算**，
        不写死 13（写死 13 实测把需求顶到 1084/1507，两条判据当场红）。
        """
        cur = self._app_settings.get('pack_mode', 'hide')
        labels, self._pack_mode_by_label = [], {}
        for zh, en, mode in self._PACK_MODE_OPTS:
            lb = i18n.tr(zh, en)
            labels.append(lb)
            self._pack_mode_by_label[lb] = mode
            self._pack_mode_by_label.setdefault(zh, mode)
            self._pack_mode_by_label.setdefault(en, mode)
        pack_lbl = ttk.Label(parent, text=i18n.tr('收纳:', 'Mode:'),
                             font=ui_layout.app_font(9),
                             foreground=COLORS['text_secondary'])
        pack_lbl.pack(side=tk.LEFT, padx=(12, 2))
        self._pack_mode_var = tk.StringVar(
            value=next((zh if i18n.get_language() != 'English' else en
                        for zh, en, m in self._PACK_MODE_OPTS if m == cur),
                       self._PACK_MODE_OPTS[0][0]))
        width = max(sum(2 if ord(c) > 0x2E80 else 1 for c in lb)
                    for lb in labels) + 1
        self._pack_mode_combo = ttk.Combobox(parent, textvariable=self._pack_mode_var,
                                             width=width, state='readonly',
                                             values=labels)
        self._pack_mode_combo.pack(side=tk.LEFT)
        self._pack_mode_combo.bind('<<ComboboxSelected>>',
                                   self._on_pack_mode_change)

    def _box_backup_hint(self, mode):
        """★R309 #363：切到盒子收纳时提醒备份桌面图标——**每次程序运行只提醒一次**。

        旗标挂在实例上、不写盘：重启后自然又能提醒一次（用户口径"程序启动状态就
        提醒一次，然后就不再提醒"）。备份本身交给数据页那枚既有出口，这里不另造格式。
        """
        if mode != 'box' or getattr(self, '_pack_box_backup_asked', False):
            return
        self._pack_box_backup_asked = True
        try:
            ask = messagebox.askyesno(
                i18n.tr('切到盒子收纳', 'Switch to box packing'),
                i18n.tr('盒子收纳会真的把图标搬进桌面盒子文件夹，不是只翻隐藏属性。\n'
                        '建议先备份一次桌面图标：万一盒里的东西被误删，还能整份退回来。\n\n'
                        '现在备份吗？',
                        'Box packing really moves icons into the DeskBits box '
                        'folder instead of just hiding them.\nBack up the desktop '
                        'icons once first: if a box is ever deleted by mistake, '
                        'you can restore the whole desktop.\n\nBack up now?'),
                parent=getattr(self, 'root', None))
        except Exception as e:
            logger.warning('[收纳档] 备份提醒没能弹出来（切档照做）: %s', e)
            return
        if ask:
            try:
                from gui import data_manage_page as _dmp
                _dmp.on_export_desktop_icons(self)
            except Exception as e:
                logger.warning('[收纳档] 备份这一步没走成（切档照做）: %s', e)
                self._log('🗂 ' + i18n.tr(
                    '备份没能发起（原因见日志），收纳方式已经切好了。',
                    'The backup could not be started (see the log); the packing '
                    'mode is switched.'))
        else:
            self._log('🗂 ' + i18n.tr(
                '好，本次运行不再提醒备份（数据页随时可以手动备份）。',
                'OK - no more prompts this run. The Data page can back up '
                'any time.'))

    def _on_pack_mode_change(self, event=None):
        """改档：先落盘 → 再留日志（★R226 口径）。不搬任何文件。"""
        mode = self._pack_mode_by_label.get(self._pack_mode_var.get(), 'hide')
        self._app_settings['pack_mode'] = mode
        self._save_app_settings()
        zh, en = ((u'双属性隐藏（图标原地不动，只翻隐藏属性）',
                   u'hidden by file attributes (icons never move)')
                  if mode == 'hide' else
                  (u'装进盒子收纳（不该显示的真的搬进 桌面盒子deskbits\\收纳·<组>）',
                   u'packed into boxes (off-group icons really move to 桌面盒子deskbits)'))
        self._log('🗂 ' + i18n.tr(
            f'收纳方式已切换为：{zh}。下一次切换分类 / 排行榜显示 / 家长模式时按这一档落地。',
            f'Packing mode: {en}. It applies on the next category switch, '
            f'rank display or kids mode.'))
        logger.info('[收纳档] pack_mode=%s（下一次切换起生效，本步零文件操作）', mode)
        self._warn_boxes_before_hide_mode(mode)
        self._box_backup_hint(mode)

    def _warn_boxes_before_hide_mode(self, mode):
        """★R320（S5c）：从盒子档切到隐藏档那一下，先问"盒里这些要不要搬回"。

        为什么要有这一问（win11 R037 日志 07:13:45→07:17:42 实锤）：hide 档**一枚都不
        碰盒**（"逐字节转发"那条红线本批不动），所以同一堆图标会被分成两处管——桌上一批
        挂着隐藏属性、盒里一批原地不动，用户点什么都没收回全，读成"图标丢了"。
        切档这一步本身仍是零文件操作；这里只**问一句**，点了才动。
        """
        if mode != 'hide' or SELF_CHECK:
            return
        try:
            from core import visibility_router as _vr
            au = _vr.audit_box_root()
        except Exception as e:
            # 兜底可以吞异常，但必须留痕（★R230b），且不许把切档带倒
            logger.warning('[收纳档] 切隐藏档前没核对成盒根（照切，只少了提醒）: %s', e)
            return
        ours = [b for b in (au.get('boxes') or []) if b.get('ours')]
        n_in = sum(len(b.get('entries') or []) for b in ours)
        n_unreg = sum(len(b.get('entries') or []) for b in ours
                      if not b.get('registered'))
        # 清单读不动那一支必须排在数枚数**之前**：`audit_box_root` 在 ledger_failed 时
        # 只报读不动、一盒都不列（保守口径），先数就会先 return ⇒ 这一句永远走不到。
        if au.get('ledger_failed'):
            self._log('🗂 ' + i18n.tr(
                '收纳清单读不动——这一档下盒子里的东西不会自动回来，切到隐藏档前'
                '先去资源页的盒子面板看一眼。',
                'The box ledger cannot be read - nothing in the boxes will come '
                'back on its own. Check the box panel on the Resources page first.'))
            return
        if not n_in:
            return
        try:
            go = messagebox.askyesno(
                i18n.tr('切到隐藏收纳', 'Switch to hidden packing'),
                i18n.tr('盒子里还放着 %d 枚图标%s。\n\n隐藏档只翻隐藏属性，不会去盒子里拿东西'
                        '——现在切过去，这些不会回到桌面。\n\n先全部搬回桌面吗？'
                        % (n_in, ('（其中 %d 枚是换了程序目录后没登记上的）' % n_unreg
                                  if n_unreg else '')),
                        'The boxes still hold %d icons%s.\n\nHidden packing only '
                        'toggles file attributes - it never reaches into the boxes, '
                        'so those icons will not reappear.\n\nMove them all back to '
                        'the desktop first?'
                        % (n_in, (' (%d of them unregistered after a version switch)'
                                  % n_unreg) if n_unreg else '')),
                parent=getattr(self, 'root', None))
        except Exception as e:
            logger.warning('[收纳档] 切隐藏档的提醒没能弹出来（照切）: %s', e)
            return
        if go:
            # 走现成那条链（`release_all_for`：先认领没登记的、再散回登记的，并把
            # "还剩几枚拿不准的"报进界面日志）——不在这里另抄一份 try + 文案。
            try:
                _vr.release_all_for(self.icon_manager, source='mode-switch',
                                    log=self._log)
            except Exception as e:
                logger.error('[隐藏盒] 切档前的散回没走成（切档照做）: %s', e)
        else:
            self._log('🗂 ' + i18n.tr(
                '好，盒里 %d 枚先留着——想拿回来在资源页的盒子面板点「搬回未认领的」，'
                '或直接点「显示全部」。' % n_in,
                'Keeping those %d icons in the boxes - use the box panel on the '
                'Resources page, or "Show all".' % n_in))

    def _release_boxes(self, source: str = '') -> int:
        """把本系统搬进盒里的图标散回桌面（「显示全部」语义的另一半）。

        ★R305 真机实撞（P1）：box 档切过分类以后图标是真的不在桌面上，而退出恢复
        这类路径只会 `unhide_all()`（属性法），看不见盒里的东西 ⇒ 桌面看着像被清空。
        位置交给紧随其后的布局/摆位，这里不传 apply_layout（理由见 dock 那条注释）。
        """
        n = 0
        try:
            from core import visibility_router as _vr
            r = _vr.release_all_boxes(self.icon_manager, source=source)
            n = int(r.get('total') or 0)
            if r.get('reason'):
                self._log('⚠ ' + str(r['reason']))
            if n:
                self._log(f'📦 已从盒里搬回 {n} 枚（{len(r.get("released") or {})} 只盒）')
        except Exception as e:
            logger.error(f'[隐藏盒] 散回失败（来源={source}，后续步骤照走）: {e}')
        return n

    def _startup_box_reclaim(self):
        """★R320：启动后对账一次盒根，把"盘上有、账本没登记"的旧盒图标**能证明的**先找回家。

        为什么排在启动：真机 win11 R037 那起"图标丢失"就是这么走的——换了新版本目录
        （两本账写在 exe 旁边，跟着目录一起是空的），盒根却在家目录里活着，于是
        「显示全部」散回 0 枚、切分类报"在盘上但没登记 ⇒ 一枚不搬"，用户只能自己拖。
        这一趟只搬**显示名在分类名单里**的那几枚（`claim_stray_boxes` 的判据），
        证明不了的原地留着，只在界面日志点一句"还有 N 枚没被认领"。
        **本身不抛**：对账失败不许把启动链带倒（★R230b：兜底可以吞异常，但必须留痕）。
        """
        # 二次确认：排 `after` 是一回事，真跑之前再问一遍入口（★R320 入口闸）。
        if not box_startup_reclaim_ok():
            logger.info('[selfcheck] 跳过启动盒根对账（探针/测试进程不动真桌面）')
            return
        try:
            from core import visibility_router as _vr
            r = _vr.claim_stray_boxes(source='startup')
            n = int(r.get('total') or 0)
            left = sum(len(v) for v in (r.get('left') or {}).values())
            # ★R321 J3 补漏（独立验收）：撞名留在盒里的那几枚以前只进 app.log
            # （`claim_stray_boxes` 里那句 warning），这一条入口界面上一个字没有 ⇒
            # 用户看见的还是"图标没回来"。跟「显示全部」用同一句文案（词典已建）。
            _nc = len(r.get('conflicts') or [])
            if r.get('reason'):
                self._log(u'⚠ ' + str(log_i18n.translate(r['reason'])))
            if n:
                self._log('📦 启动自查：从没登记的盒子里找回 %d 枚图标到桌面'
                          '（换过版本目录就会这样，以后每次启动都替你查一遍）' % n)
                logger.info('[隐藏盒] 启动自查：认领=%d 枚 动过的盒=%d 只',
                            n, len(r.get('claimed') or {}))
            elif left:
                self._log('📦 启动自查：盒里还有 %d 枚没被认领（分类名单里没有它们的'
                          '名字）——资源页的盒子面板可以一键搬回' % left)
            if _nc:
                # 独立一句（不并进上面那支）：找回了几枚也可能同时有撞名的，
                # 两件事都要说。
                self._log(u'📦 还有 %d 枚留在盒里没动（桌面上已经有同名的）'
                          u'——要搬请在资源页的盒子面板点对那只盒' % _nc)
        except Exception as e:
            logger.error('[隐藏盒] 启动自查失败（不影响其它功能）: %s', e)
            # ★R320 S5b（独立验收 D5）：这一支以前只躺 app.log——简报第 2 条承诺的是
            # "不再静默"，而对账**整个没跑成**恰恰是最该出声的一格（用户看见的是
            # "图标还是没回来"，界面上却一句没有）。只补一句，不改启动链。
            self._log(u'⚠ ' + i18n.tr(
                '启动自查没跑成（盒子里的图标这趟没替你核对）——资源页的盒子面板可以'
                '自己点一次「搬回未认领的」',
                'The startup box check did not run, so nothing in the boxes was '
                'verified this time - the box panel on the Resources page can '
                'reclaim them on demand'))

        # ★R323 L4（用户点名：启动自查发现早期让路名残留时提醒，**只提醒不动手**）：
        # 历轮"改名让路"留下过两代回收目录（`~让路回收_<时刻>` / `~让路_<月日>-<第几把>`），
        # `~` 前缀 ⇒ 它们**不在**盒子面板的可见名单里（`list_boxes` 读不到），于是里面
        # 那几枚永远没人去看。这一趟只数一遍、界面点一句，一个文件不搬、一个目录不删：
        # 动手由用户在盒子面板点「清理重复的图标」（★R324 N1 前的旧名读不懂）——
        # ★R323 L8 那条链会把它们一并收进可见的「收纳·重复的」盒。
        try:
            from core import desktop_box as _db
            _left = _db.yield_leftover_dirs()
            if _left:
                self._log(u'🧹 ' + i18n.tr(
                    u'启动自查：盒根下还留着 %d 个早期的"让路"回收目录'
                    u'（历轮改名留下的，里面一枚没删）。这一趟没动它们——要收拾，'
                    u'点资源页盒子面板的「清理重复的图标」，会一并收进'
                    u'「收纳·重复的」' % len(_left),
                    u'Startup check: %d old "yield" backup folder(s) are still under '
                    u'the box root (left by earlier renames, nothing inside was '
                    u'deleted). Nothing was touched this run - press "Tidy" in the '
                    u'box panel on the Resources page to collect them into the '
                    u'duplicate box' % len(_left)))
                logger.info('[隐藏盒] 启动自查：早期让路目录残留 %d 个（只提醒，没动手）',
                            len(_left))
        except Exception as e:
            logger.warning('[隐藏盒] 启动自查：让路残留没查成（不影响其它功能）: %s', e)

    # ----------------------- 全局快捷键 -----------------------
    def _on_tk_callback_exception(self, exc_type, exc_value, exc_tb):
        """Tk 回调异常：只记日志，绝不退出主循环"""
        logger.error(f"Tk 回调异常(已忽略): {exc_type.__name__}: {exc_value}",
                     exc_info=(exc_type, exc_value, exc_tb))

    # ★ R140：热键逻辑整体拆至 gui/hotkey_panel.py（codeSplitter 流程一刀）。
    # 保留薄委托：数据管理页"导入数据"仍按旧名调用本方法
    def _save_hotkey_config(self):
        hotkey_panel.save_config(self)

    def _drain_ui_queue(self):
        """主线程消费后台线程投递的 UI 任务（P1-3）。

        Tk 官方不保证 after 线程安全（CPython 靠 GIL 侥幸可用，但某些版本
        /平台会抛 "main thread is not in main loop" 或静默丢事件）。所有
        "后台线程 → UI" 的回传统一走 self._ui_queue：后台只 put(callable)，
        主线程 50ms 轮询消费。
        一次性消费积压（避免多次唤醒事件循环）；单个 cb 异常不阻断后续。
        （★ R140 拆分事故补充：本方法曾随热键块误删——切除范围必须
        逐方法核对，不能整段连切。）"""
        try:
            while True:
                try:
                    cb = self._ui_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    cb()
                except Exception as e:
                    logger.error(f"UI 队列任务异常(已忽略): {e}", exc_info=True)
        except Exception as e:
            logger.error(f"UI 队列消费异常(已忽略): {e}")
        try:
            self.root.after(50, self._drain_ui_queue)
        except Exception:
            pass  # 退出瞬间 root 已销毁

    # ----------------------- 应用设置 -----------------------

    def _app_settings_path(self) -> Path:
        return APP_DIR / 'config' / 'app_settings.json'

    def _load_app_settings(self) -> dict:
        s = {'close_keep_dock': True, 'dock_max_slots': 12, 'system_icons_resident': True,
             'cat_pane_width': 170, 'dock_font_size': 16, 'apply_cat_layout': True,
             'layout_guard': True, 'close_restore_layout': '当前',
             'close_restore_enabled': True, 'desktop_brightness': 100,
            'box_auto_pack': True,
             'icon_watch_window_sec': 180}
        # ★R264 审计 Z13（P2）：旧写法 `except Exception: pass` ⇒ 手改配置漏个
        # 逗号（app_bootstrap 官方推荐的操作）就把全部设置静默重置成出厂值，
        # **零日志**；更糟的是启动后几秒内就有高频 `_save_app_settings()`
        # （亮度/守护/分类宽度…）把这份"内存里的默认值"原子写回盘 ⇒
        # 用户原配置彻底湮灭，且没有任何一份留底可查。
        # 与 core/category_manager、core/download_tracker、usage_stats 同口径：
        # 损坏原件先另存留底，留不住底就立本会话禁写闸；文件不存在=首启，放行。
        # ★R285-N7：这把闸是**单向闩**——本会话内一旦立起（读不动且留底失败），
        # 后面任何一次重读都不许把它悄悄放掉——所以这里**不再有**那句无条件
        # `self._settings_load_failed = False`。旧写法开头就复位，而 manage_panel
        # 构建期为取 cat_pane_width 又调了本函数一次：启动时配置被
        # 临时占用（闸门立、内存=出厂默认）⇒ 构建管理页时占用释放、重读成功 ⇒
        # 闸门被复位而内存还是默认值 ⇒ 此后任意一次设置写盘就用出厂默认整份覆盖
        # 用户真配置——正是 Z13 要防的那次丢失。放闸的唯一出口是**重启进程**
        # （读侧判据自身照常：文件不存在=首启放行，★R231）。
        try:
            if self._app_settings_path().exists():
                data = json.loads(self._app_settings_path().read_text(encoding='utf-8'))
                if isinstance(data, dict):
                    s.update(data)
                else:
                    self._corrupt_settings_keep_and_gate(
                        ValueError('顶层不是对象，是 %s' % type(data).__name__))
        except Exception as e:
            self._corrupt_settings_keep_and_gate(e)
        # ★ 迁移：旧默认值 close_restore_enabled=False / close_restore_layout=''
        #   会导致启动时全部图标显示、丢失上次分类。统一升级为"保持当前分类"，
        #   让启动默认载入上次退出的桌面状态。
        # 用 _migrated_close_restore 标记确保只迁移一次——用户若主动关闭该功能，
        # 下次启动不会被强制重新打开。
        if not s.get('_migrated_close_restore'):
            if not s.get('close_restore_enabled') and not s.get('close_restore_layout'):
                s['close_restore_enabled'] = True
                s['close_restore_layout'] = '当前'
            s['_migrated_close_restore'] = True
        # ★ #1 迁移：旧默认 close_keep_dock=False —— 主窗口一关，悬浮条跟着退出。
        #   用户要求"关闭主界面应保留悬浮窗，只有点悬浮窗的 ✕ 才退出"，
        #   一次性升级为 True。用 _migrated_keep_dock 标记确保只迁移一次——
        #   用户若主动改回"关闭即退出"，下次启动不会被强制打开。
        if not s.get('_migrated_keep_dock'):
            if not s.get('close_keep_dock'):
                s['close_keep_dock'] = True
            s['_migrated_keep_dock'] = True
        return s

    def _corrupt_settings_keep_and_gate(self, e):
        """app_settings.json 损坏：先另存留底，留不住底就立本会话禁写闸（★R264 Z13）"""
        from utils.safeio import set_aside_corrupt
        kept, detail = set_aside_corrupt(self._app_settings_path())
        if not kept:
            self._settings_load_failed = True
        logger.error('app_settings.json 读不动，按出厂默认运行；原件留底%s: %s'
                     % (('成功（%s）' % os.path.basename(detail)) if kept
                        else '同样失败（可能被占用）', e))

    def _apply_display_prefs(self):
        """★R225 阶段5：落"图标风格 + 界面密度"两档 ⇒ (图标档, 密度因子)。

        须在 _setup_styles/_build_ui **之前**调用：行高在 ttk 样式表里、SP 在
        页签与各面板构建时被读。设置页改档位后会再调一次本方法，让之后新建的
        窗口/对话框立即按新档构建（主窗已建好的部分仍要重启，见 settings_panel）。
        """
        s = self._app_settings
        mode = icons.set_mode(s.get('ui_icons', icons.DEFAULT_MODE))
        density = s.get('ui_density', '标准')
        f = ui_layout.apply_density(density)
        theme_panel.apply_density(f)
        return mode, f

    def _save_app_settings(self):
        """写 app_settings.json，并把**到底写上没有**回报给调用方（★R287-N26）。

        返回 True=盘上这份就是内存这份；False=没写进去（拒写闸立着 / 异常 /
        磁盘或权限失败）。历轮它三种坏出口全 `return`（None）、好出口不返回，
        调用点无从判断 ⇒ "导入全部数据"里设置项写不进盘，完成弹窗照样只报
        "成功"（★R284 那一批同款病，别的文件已经收口，这一处漏了）。
        """
        # ★R264 审计 Z13：本会话的 app_settings.json 若是"读不动且留底也失败"
        # 的状态，内存里那份是出厂默认 ⇒ 继续写就是用默认值整份顶掉用户配置
        # （★R231 口径：闸门要独立成立，不能指望上游自觉）。
        if getattr(self, '_settings_load_failed', False):
            now = time.time()
            if now - getattr(self, '_settings_refuse_ts', 0) > 60:
                self._settings_refuse_ts = now
                logger.warning('app_settings 本次未写盘：原件读不动且留底失败，'
                               '继续写会用出厂默认覆盖用户配置')
                self._log('⚠ 设置未写盘（config/app_settings.json 读不动且留底失败，'
                          '已拒写以防覆盖）：本次改动重启后不生效，'
                          '请先关闭占用该文件的程序再重启')
            return False
        # ★R204 GUIA-14：写盘失败留限频告警（R200 路径记忆等已依赖本写入，
        # 静默丢失无线索）
        ok = False
        try:
            ok = atomic_write_json(self._app_settings_path(),
                                   self._app_settings, indent=2)
        except Exception as e:
            logger.error(f"app_settings 写盘异常: {e}")
            return False
        if not ok:
            now = time.time()
            if now - getattr(self, '_settings_warn_ts', 0) > 60:
                self._settings_warn_ts = now
                logger.error("app_settings 写盘失败（设置仅存内存）")
                self._log('⚠ 设置写盘失败（磁盘/权限？），本次改动重启后可能丢失')
            return False
        return True

    def _apply_app_settings(self):
        # ★ #3：注册亮度终态回调。必须在这里（_build_ui 之后）——__init__ 早期
        #   （L371）就会调 dimmer.set_level，那时 dim_mode_var 还没创建，
        #   提前注册会导致回调里 AttributeError 闪退（历史日志里出现过）。
        try:
            self.dimmer.mode_changed_cb = self._on_dim_mode_changed
        except Exception as e:
            # ★R313 E4：亮度终态回调没挂上＝调亮度时灯不跟着变，旧写法零留痕
            logger.warning(f"[设置] 亮度回调注册失败（调亮度可能不联动）: {e!r}")
        # ★R304 S6：这里原来是一发裸 `int(...)` + `except: pass`——脏值静默跳过，
        # 和 __init__:331 那份 `_settings_int`（脏值回退默认 + 留日志，★R264 Z2 口径）
        # 分叉。`self.dock` 在 :321 就建好了，本行晚于它，不需要再包 try。
        self.dock.max_slots = _settings_int(
            self._app_settings.get('dock_max_slots', 12), 12, 'dock_max_slots')
        self.dock.system_resident = bool(
            self._app_settings.get('system_icons_resident', True))
        self.dock.apply_cat_layout = bool(self._app_settings.get('apply_cat_layout', True))
        # ★R249 一2①：切分类摆布局之前，先把桌面上压着的活动排序安静清掉
        # （免鼠标接口、不弹框；和上面那枚复选同一个注入点，随设置一起生效）
        self.dock.silent_sort_fix = self._sort_silent_fix
        # ★ R83：布局守护退役（用户定案 2026-09-16）。手动模式——点悬浮窗/
        # 恢复布局即排好，拖动后用户手动保存。守护原用途（拓扑变化自动
        # 拉回）在持久化位置表背景下反复与用户操作互相拉扯，弊大于利。
        # 代码保留（icon_manager.check_guard 等），仅强制停用，可回退。
        self.icon_manager.guard_enabled = False
        # ★ R92：layout_guard 开关改为控制"恢复后看护"
        self.icon_manager.watch_enabled = bool(self._app_settings.get('layout_guard', True))
        # ★R272（批25，用户 2026-09-29 01 批 item1「我觉得三分钟是可以修改的」）：
        # 密集监视窗口长度可配。夹取/回落与留痕都在 highlighter.set_watch_window
        # 里做（app_settings 会随"导入全部数据"从外部 JSON 进来，属外部输入）。
        try:
            _ww = self.highlighter.set_watch_window(
                self._app_settings.get('icon_watch_window_sec', 180))
            self._app_settings['icon_watch_window_sec'] = _ww
            # ★R240 #137 同一病根：导入全部数据只落盘不回头刷界面 ⇒ 用户看到的
            # 还是旧值。这里把下拉框一起推平（规则页没建时静默跳过）。
            _ww_var = getattr(self, 'watch_window_var', None)
            if _ww_var is not None:
                _ww_var.set('%g' % (_ww / 60.0))
        except Exception as e:
            logger.warning(f"应用图标监视时长失败（用缺省 180 秒）: {e}")
        try:
            # ★ R153：只在用户设过自定义色时应用；否则走自动对比档
            if self._app_settings.get('dock_text_color'):
                self.dock.set_text_colors(
                    self._app_settings['dock_text_color'], False)
            if self._app_settings.get('dock_active_text_color'):
                self.dock.set_text_colors(
                    self._app_settings['dock_active_text_color'], True)
        except Exception as e:
            # ★R313 E4：自定义字色没应用＝悬浮栏文字还是默认色，用户只会说"设置没生效"
            logger.warning(f"[设置] 悬浮栏文字色未应用（继续用默认色）: {e!r}")
        self.icon_manager.desktop_tidy_mode = False   # 整理桌面模式不持久化，每次启动默认关
        # ★ B5（R24）：守护恢复回调在监视线程触发，原直接注入 notify_changed
        # 会让它在监视线程里调 root.after（与 P1-3 线程约定冲突）——
        # 改经 _ui_queue 转 UI 线程执行
        # ★R242 #140：同一个回调再挂一层"清排序兜底"的触发点（守护刚把图标
        # 拉回原位＝桌面带着活动排序的最强信号）。仍走 _ui_queue，弹窗和
        # 鼠标注入只发生在 UI 线程；core/dock.py 里不留任何引用（红线）。
        self.icon_manager._guard_notify_cb = self._on_guard_restored
        try:
            # ★R264 Z2：走 _settings_int——原 `except: pass` 会让脏值静默跳过
            # 字号应用，下面的回填链也一并断（★R222 挂账 Z22 的同一条链）
            self.dock.set_font_size(
                _settings_int(self._app_settings.get('dock_font_size', 16),
                              16, 'dock_font_size'))
        except Exception as e:
            logger.warning(f"应用悬浮条字号失败（重启后正常）: {e}")
        # ★R280-A（用户 06 批 item1）：悬浮栏渐变浓淡三档。没存过就不动
        # （dock 自己带的 GRAD_DEFAULT_LEVEL 就是默认档，这里不另抄一个默认值），
        # 存了坏值也不打死启动：set_gradient_level 认不得就回默认档。
        try:
            _gl = self._app_settings.get('dock_gradient_level')
            if _gl:
                self.dock.set_gradient_level(_gl)
        except Exception as e:
            logger.warning(f"应用悬浮栏渐变档位失败（重启后正常）: {e}")
        # 重建槽位（数量限制生效）+ 同步 UI
        self.dock.sync_slots(self.category_manager.get_all_categories())
        try:
            self.dock_keep_var.set(bool(self._app_settings.get('close_keep_dock')))
            self.dock_maxslots_var.set(str(self.dock.max_slots))
            self.dock_font_var.set(_fd_disp(
                self._DOCK_FONT_NAMES.get(
                    _settings_int(self._app_settings.get('dock_font_size', 16),
                                  16, 'dock_font_size'), '中')))
            # ★R280-A：渐变档位的框回显读 **dock 当前生效的那一档**、不读盘——
            # 上面 set_gradient_level 已把坏档名回落成默认档，跟着盘走会显示一个
            # 屏幕上根本看不到的档（同上面字号那两行的分工）。
            _gv = getattr(self, 'dock_gradient_var', None)
            if _gv is not None:
                _gv.set(_gd_disp(getattr(self.dock, 'grad_level', '')))
            self.apply_catlayout_var.set(
                bool(self._app_settings.get('apply_cat_layout', True)))
            self.layout_guard_var.set(
                bool(self._app_settings.get('layout_guard', True)))
            self.close_restore_enabled_var.set(
                bool(self._app_settings.get('close_restore_enabled', False)))
            # ★R241：两个新复选回填（勾法与"退出时恢复"同族，放同一段）
            self.apply_layout_var.set(
                bool(self._app_settings.get('apply_layout_on_launch', False)))
            self.auto_start_var.set(
                bool(self._app_settings.get('auto_start_logon', False)))
            # ★R245（用户 37 批 item-二-2）：原来这里回填「桌面排序修复」复选，
            #   该复选已按用户口径删除（检测改为常开），不再有可回填的控件。
            self._update_close_restore_combo()
            # 恢复上次选择的布局
            last_layout = self._app_settings.get('close_restore_layout', '')
            if last_layout:
                self.close_restore_layout_var.set(_cr_disp(last_layout))
            # ★R222 U4：下拉里存的是显示文案（fd_disp），启动回填也得过一遍
            # 翻译，否则 English 下框里显示"标准"、且不在 values 里
            self.ui_fontsize_var.set(_fd_disp(next(
                (k for k, v in self._UI_FONT_SIZES.items()
                 if v == _settings_int(
                     self._app_settings.get('ui_font_size', 11), 11,
                     'ui_font_size')),
                self._DEFAULT_UI_FONT)))
            hotkey_panel.update_list(self)
            # ★ R142：Recent .lnk 回填改主线程调度——后台线程跑 WScript.Shell
            #   COM 曾原生 AV 整进程闪退（win11 crash.log 实锤）；此时 root
            #   已就绪，延迟 6s 避开启动高峰
            self.root.after(6000, self.download_tracker.scan_recent_lnk)
            # ★R320（用户 P0「图标丢失」）：启动就替用户查一次盒根——旧写法要等用户
            #   点某一枚分类才发现"图标躺在盒里散不回"，而那一刀正落在用户最想用的
            #   功能上。这一趟只搬"分类名单里点过名"的，剩下的报进界面日志。
            #   这一趟只搬"分类名单里点过名"的，剩下的报进界面日志。
            #   **入口闸**（`box_startup_reclaim_ok`）：探针态与回归测试进程都不排，
            #   因为 `after` 会在测试进程活着过 6 秒时往真桌面搬文件。排在 6s 那发
            #   之后、避开启动高峰同一条理由。
            if box_startup_reclaim_ok():
                self.root.after(6400, self._startup_box_reclaim)
            # ★R332-c（§卅六 三·5）：老版本虚拟机启动脚本的**只读**探测——从不打开
            #   计划任务页的用户，坏脚本会一直坏着（★R331-b 的自愈只在点刷新那趟跑）。
            #   启动这一趟只数不写；真数到了才问一句，点【是】才修。入口闸同上：
            #   探针态与回归测试进程一律不排——`after` 会在测试进程活着过 7 秒时
            #   往真目录写文件，那是绝对不该发生的。
            if box_startup_reclaim_ok():
                self.root.after(7200,
                                lambda: sys_panel.startup_vm_bat_check(self))
            # 启动时按设置字号重算窗口（否则首次启动用默认字号量出来的尺寸和
            # 用户上次选的字号对不上）
            try:
                self._reposition_window()
            except Exception as e:
                # ★R313 E4：按字号重算窗口失败＝首启尺寸对不上旧写法零留痕
                logger.warning(f"[设置] 窗口按字号重算失败（尺寸可能对不上）: {e!r}")
        except Exception as e:
            # ★R313 E4（★R230b 口径：兜底可以吞异常，但必须留一行痕）：这一整段是
            # 11 个控件回填 + 热键列表刷新 + 窗口重算，旧写法 `except: pass` 让
            # 任何一环抛异常都"后半段静默不跑 + app.log 零痕迹"，用户读成
            # "设置没生效"而下轮无从归因。
            logger.warning('[设置] 界面回填中断（其后控件/热键列表可能没刷新，'
                           '重启后按盘上设置重建）: %r' % (e,))

        # ★R258（用户 50 批 二1）：启动自动打开（组/高频/图标三列）——默认关，勾了才排程；
        # 延时按档位（Explorer 冷启动头几秒不稳，也盖过 3 秒联动静默窗）。
        # 没勾就不给 root 挂任何多余回调（★R241 同款纪律）。
        try:
            from core import explorer_tabs as _et_so
            _so_cfg = _et_so.sanitize_startup_open(
                (_et_so.load_state(APP_DIR / 'config')
                 or {}).get('startup_open'))
            if _so_cfg['enabled']:
                # ★R313 E2：排程走出口——探针态不排（★R313 E1）＋同一会话只排一发
                self._schedule_startup_open(_so_cfg['delay_s'])
        except Exception as e:
            logger.warning(f'[启动] 自动打开设置未排程: {e}')

    def _schedule_startup_open(self, delay_s: int) -> bool:
        """把"启动自动打开组/高频/图标"排成一发**整会话只排一次**的回调。

        ★R313 E2（真 bug，用户可感知）：`_apply_app_settings` 不只启动时跑——
        改槽位上限（settings_panel:366）、导入全部数据/回滚
        （data_manage_page:942/1223）都会重进它。旧写法在那里裸
        `self.root.after(delay_s * 1000, self._startup_open_folders)` ⇒
        用户勾了自动打开后，运行中改一次设置，十几秒后程序"像开机一样"成批
        弹出勾选的窗口，而且每改一次再叠一发（时序不可归因）。
        探针态（`--selfcheck`）一律不排：测试不许开真窗口（★R221 纪律）。
        返回是否真的排了（门禁按这个判"只排一次"）。
        """
        if SELF_CHECK:
            logger.info('[selfcheck] 跳过启动自动打开排程（探针不开真窗口）')
            return False
        if getattr(self, '_startup_open_scheduled', False):
            logger.debug('[启动] 自动打开本会话已排过，跳过重复排程')
            return False
        self._startup_open_scheduled = True
        self.root.after(int(delay_s) * 1000, self._startup_open_folders)
        return True

    # ----------------------- 资源管理器文件夹组（多组命名） -----------------------

    def _refresh_dm_groups_label(self):
        """数据管理页"当前文件夹组"一览（只读展示，管理在资源管理页）"""
        var = getattr(self, 'dm_groups_var', None)
        if var is None:
            return
        try:
            from core import explorer_tabs
            st = explorer_tabs.load_state(APP_DIR / 'config')
            groups = explorer_tabs.sorted_groups(st.get('groups', []))
            if not groups:
                var.set('当前文件夹组：（无——在 📂 资源 页"新建组"或"存为组"创建）')
                return
            active = st.get('active_group', '')
            parts = [f"{'▶ ' if g['name'] == active else ''}"
                     f"{g['name']}({len(g.get('folders', []))}个)" for g in groups]
            var.set('当前文件夹组: ' + '  ·  '.join(parts) + '　—— 在 📂 资源 页管理')
        except Exception as e:
            logger.error(f"刷新文件夹组一览失败: {e}")

    def _refresh_explorer_list(self):
        """重建组条（一排）并填充当前选中组的文件夹列表；
        同步刷新数据管理页的"当前文件夹组"一览（2026-09-05）"""
        try:
            self._refresh_dm_groups_label()
        except Exception:
            pass
        groups, active = [], ''
        try:
            from core import explorer_tabs
            state = explorer_tabs.load_state(APP_DIR / 'config')
            groups = state.get('groups', [])
            active = state.get('active_group') or (
                groups[0]['name'] if groups else '')
            state['active_group'] = active
            self.explorer_as_tabs_var.set(bool(state.get('as_tabs', True)))
            try:
                self.explorer_link_var.set(bool(state.get('link_switch', True)))
            except Exception:
                pass
            # 当前组的"悬浮框自动打开"勾选状态随选中组同步
            # ★R232（用户 06 批 item2）：缺字段按"关"显示，和 core 侧兜底同口径
            try:
                cur = next((g for g in groups if g['name'] == active), None)
                self.explorer_auto_group_var.set(
                    bool(cur.get('auto_open', False)) if cur else False)
                self.explorer_auto_group_name_var.set(
                    f'[{active}]' if active else '（未选中组）')
            except Exception:
                pass
        except Exception as e:
            logger.error(f"读取文件夹组失败: {e}")

        # 清空旧组条并重建（单击选中 / 双击打开）
        for w in self.explorer_chips_frame.winfo_children():
            w.destroy()
        for g in groups:
            name = g['name']
            sel = (name == active)
            btn = tk.Button(self.explorer_chips_frame, text=name,
                            font=ui_layout.app_font(10),
                            padx=10, pady=3, bd=0, cursor='hand2',
                            # ★R215：写死的 #3a6ea5 蓝选中态 → 主题 primary，
                            # 护眼/浅色主题下不再"这一块蓝是哪来的"
                            bg=COLORS['primary'] if sel else COLORS['surface'],
                            fg=COLORS['on_primary'] if sel else COLORS['muted'],
                            activebackground=COLORS['primary'],
                            activeforeground=COLORS['on_primary'],
                            command=lambda nn=name: self._on_explorer_group_chip(nn))
            btn.bind('<Double-Button-1>',
                     lambda e, nn=name: self._on_explorer_group_open(nn))
            btn.pack(side=tk.LEFT, padx=(0, 6))

        # 当前组的文件夹列表
        self.explorer_list.delete(0, tk.END)
        for g in groups:
            if g['name'] == active:
                for f in g.get('folders', []):
                    self.explorer_list.insert(tk.END, f)
                break
        ui_layout.zebra_listbox(self.explorer_list, COLORS)
        self._repaint_explorer_list_toggle()
        # ★R262 批14（item4）：组的增删改会牵动「自动打开设置」卡第 1 列的
        # 勾选行（新建的组要能勾、删掉的组不能留幽灵行）——同一次刷新里对齐
        self._resync_startup_open_vars()

    def _repaint_explorer_list_toggle(self):
        """★R262 批14（用户 14 批 item2）：组文件夹列表默认只露 3 行，
        超过 3 项才出「显示全部 ▾ / 收起 ▴」切换；≤3 项隐藏切换、自然高。
        展开也封顶 6 行——组再长也不把资源页撑爆，剩余走滚动条。"""
        try:
            n = self.explorer_list.size()
            if n <= 3:
                self._exp_list_collapsed = True
                self.explorer_list.configure(height=max(1, n))
                self.explorer_list_toggle.configure(text='')
                return
            if self._exp_list_collapsed:
                self.explorer_list.configure(height=3)
                self.explorer_list_toggle.configure(
                    text=i18n.tr('显示全部 ▾（共 {} 项）'
                                 .format(n), 'show all ▾ ({} items)'
                                 .format(n)))
            else:
                self.explorer_list.configure(height=min(n, 6))
                self.explorer_list_toggle.configure(
                    text=i18n.tr('收起 ▴', 'collapse ▴'))
        except Exception as e:
            logger.error(f'刷新组列表折叠切换失败: {e}')

    def _toggle_explorer_list(self):
        """★R262 批14：点切换展开/收起当前组文件夹列表"""
        self._exp_list_collapsed = not self._exp_list_collapsed
        self._repaint_explorer_list_toggle()

    # ---------- ★R258：🚀 启动自动打开（用户 50 批 二1，方案见 run/r257/方案_B1） ----------

    def _build_startup_open_card(self, tab_explorer):
        """★R262 资源页「🚀 自动打开设置」卡：三列 grid（按组/高频文件夹/桌面图标）。

        用户 56 批定稿：三列等宽等高；第2/3列「自动开前K」与「手动勾选」
        radio 二选一（另一侧置灰）；勾选列表默认只露前 3、可展开；全部下拉
        支持手动输入数字（sanitize 区间兜底）；字体统一 ui_font。
        """
        from core import explorer_tabs
        import os as _os
        _so = {}
        _groups_ui = []
        _top_folders = []
        _top_icons = []
        try:
            _st = explorer_tabs.load_state(APP_DIR / 'config')
            _so = _st.get('startup_open') or {}
            _groups_ui = [g.get('name') for g in
                          explorer_tabs.sorted_groups(_st.get('groups', []))
                          if g.get('name')]
            if hasattr(self, 'usage_stats'):
                _top_folders = [f[0] for f in self.usage_stats.top_folders(20)]
                _top_icons = [ic[0] for ic in self.usage_stats.top_icons(20)]
        except Exception as e:
            logger.error(f'启动自动打开初值读取失败（按默认关显示）: {e}')

        def _short(p):
            """榜单同款缩略：长路径收成 盘符+省略号+末级目录，行宽整齐。"""
            try:
                drive, tail = _os.path.splitdrive(p.rstrip('\\'))
                if drive:
                    last = _os.path.basename(tail) or tail
                    return drive + chr(92) + chr(8230) + chr(92) + last
            except Exception:
                pass
            return p

        def _editable_combo(parent, var, values, width):
            """可手动输入的下拉：候选值 + 手输都走保存（sanitize 兜底）。"""
            cb = ttk.Combobox(parent, textvariable=var, values=values,
                              width=width, state='normal',
                              style='Startup.TCombobox')
            cb.bind('<<ComboboxSelected>>',
                    lambda e: self._save_startup_open_settings())
            cb.bind('<Return>', lambda e: self._save_startup_open_settings())
            cb.bind('<FocusOut>', lambda e: self._save_startup_open_settings())
            return cb

        so_frame = ttk.LabelFrame(
            tab_explorer, text=i18n.tr('🚀 自动打开设置',
                                       '🚀 Auto-open settings'),
            padding='10', style='Card.TLabelframe')
        so_frame.pack(fill=tk.X, pady=(6, 0))

        # ---------- 顶部共用：总开关 + 延时 + 两枚上限（全可手输） ----------
        top_grid = ui_layout.form_block(so_frame)
        self.expl_startup_enabled_var = tk.BooleanVar(
            value=bool(_so.get('enabled', False)))
        _on_cb = ttk.Checkbutton(
            top_grid, text=i18n.tr('自动打开', 'Auto-open'),
            variable=self.expl_startup_enabled_var,
            style='Startup.Accent.TCheckbutton',
            command=self._save_startup_open_settings)
        self.expl_startup_delay_var = tk.StringVar(
            value=str(_so.get('delay_s', 15)))
        _delay_cb = _editable_combo(top_grid, self.expl_startup_delay_var,
                                    ['5', '10', '15', '30'], 4)
        ui_layout.form_row(
            top_grid, 0, i18n.tr('启动', 'Start'), _on_cb,
            ttk.Label(top_grid, text=i18n.tr('延时', 'Delay'),
                      style='Startup.TLabel'),
            _delay_cb,
            ttk.Label(top_grid, text=i18n.tr('秒', 's'), style='Startup.TLabel'))

        self.expl_max_folders_var = tk.StringVar(
            value=str(_so.get('max_folders_limit', 10)))
        _mf_cb = _editable_combo(top_grid, self.expl_max_folders_var,
                                 ['5', '10', '15', '20', '30'], 4)
        self.expl_max_icons_var = tk.StringVar(
            value=str(_so.get('max_icons_limit', 5)))
        _mi_cb = _editable_combo(top_grid, self.expl_max_icons_var,
                                 ['3', '5', '8', '10', '15'], 4)
        ui_layout.form_row(
            top_grid, 1, i18n.tr('上限', 'Limit'),
            ttk.Label(top_grid, text=i18n.tr('文件夹最多', 'Max folders'),
                      style='Startup.TLabel'), _mf_cb,
            ttk.Label(top_grid, text=i18n.tr('图标最多', 'Max icons'),
                      style='Startup.TLabel'), _mi_cb)

        # ---------- 三列：grid uniform ⇒ 等宽；行 0 sticky nsew ⇒ 等高 ----------
        columns_frame = ttk.Frame(so_frame)
        columns_frame.pack(fill=tk.X, pady=(10, 0))
        for _c in range(3):
            columns_frame.grid_columnconfigure(_c, weight=1, uniform='so3col')
        columns_frame.grid_rowconfigure(0, weight=1)

        # ===== 第1列：按组打开 =====
        col1 = ttk.LabelFrame(columns_frame,
                              text=i18n.tr('按组打开', 'By group'), padding='8',
                              style='Startup.TLabelframe')
        col1.grid(row=0, column=0, sticky='nsew', padx=(0, 6))
        self.expl_groups_enabled_var = tk.BooleanVar(
            value=bool(_so.get('groups_enabled', False)))
        ttk.Checkbutton(
            col1, text=i18n.tr('启用', 'Enable'),
            variable=self.expl_groups_enabled_var,
            style='Startup.Accent.TCheckbutton',
            command=self._save_startup_open_settings).grid(
            row=0, column=0, sticky='w', pady=(0, 6))
        # ★R262 批14（item4）：组勾选行放进独立子框——组数变了（新建组/
        # 删除组/导入全部数据）只需重建这个子框，不会牵动下面的分隔线、
        # 「🏆 最常用组」与「组内开前 K」那三行的行号。
        self.expl_group_check_vars = {}
        self._expl_group_box = ttk.Frame(col1)
        self._expl_group_box.grid(row=1, column=0, sticky='w')
        self._rebuild_group_check_rows(_groups_ui, _so.get('groups') or [])
        _r = 2
        ttk.Separator(col1, orient=tk.HORIZONTAL).grid(
            row=_r, column=0, sticky='ew', pady=6)
        _r += 1
        self.expl_startup_recent_var = tk.BooleanVar(
            value=bool(_so.get('smart_recent', False)))
        ttk.Checkbutton(
            col1, text=i18n.tr('🏆 最常用组', 'Most-used group'),
            variable=self.expl_startup_recent_var,
            style='Startup.Accent.TCheckbutton',
            command=self._save_startup_open_settings).grid(
            row=_r, column=0, sticky='w', pady=(0, 6))
        _r += 1
        _krow = ttk.Frame(col1)
        _krow.grid(row=_r, column=0, sticky='w')
        self.expl_startup_k_var = tk.StringVar(
            value=_top_k_display(_so.get('top_k', 'all')))
        ttk.Label(_krow, text=i18n.tr('组内开前', 'First'),
                  style='Startup.TLabel').pack(side=tk.LEFT)
        _editable_combo(_krow, self.expl_startup_k_var,
                        ['3', '5', '8', i18n.tr('全部', 'all')],
                        5).pack(side=tk.LEFT, padx=4)

        # ===== 第2/3列共用构建器：启用 + (auto|manual radio) + 折叠勾选列表 =====
        def _rank_column(col_idx, title, en_var, mode_var, k_var, items,
                         var_map, k_values, unit, is_folder):
            frame = ttk.LabelFrame(columns_frame, text=title, padding='8',
                                   style='Startup.TLabelframe')
            frame.grid(row=0, column=col_idx, sticky='nsew',
                       padx=(6, 0) if col_idx == 2 else 6)
            ttk.Checkbutton(
                frame, text=i18n.tr('启用', 'Enable'), variable=en_var,
                style='Startup.Accent.TCheckbutton',
                command=self._save_startup_open_settings).grid(
                row=0, column=0, sticky='w', pady=(0, 6))

            _arow = ttk.Frame(frame)
            _arow.grid(row=1, column=0, sticky='w', pady=1)
            ttk.Radiobutton(_arow, text=i18n.tr('自动 开前', 'Auto top'),
                            variable=mode_var, value='auto',
                            style='Startup.TRadiobutton',
                            command=self._save_startup_open_settings).pack(
                side=tk.LEFT)
            k_cb = _editable_combo(_arow, k_var, k_values, 4)
            k_cb.pack(side=tk.LEFT, padx=4)
            ttk.Label(_arow, text=unit, style='Startup.TLabel').pack(side=tk.LEFT)

            _mrow = ttk.Frame(frame)
            _mrow.grid(row=2, column=0, sticky='w', pady=1)
            ttk.Radiobutton(_mrow, text=i18n.tr('手动勾选', 'Manual'),
                            variable=mode_var, value='manual',
                            style='Startup.TRadiobutton',
                            command=self._save_startup_open_settings).pack(
                side=tk.LEFT)
            state = {'expanded': False}
            toggle = ttk.Label(_mrow, text='', cursor='hand2',
                               style='Startup.TLabel')
            toggle.configure(foreground=COLORS['primary'])
            toggle.pack(side=tk.RIGHT)

            box = ttk.Frame(frame)
            box.grid(row=3, column=0, sticky='w')
            cbs = []
            holder = {'items': list(items), 'bound': None, 'stale': None}
            _stale_kind = 'folders' if is_folder else 'icons'

            def _repaint_toggle():
                if len(cbs) <= 3:
                    toggle.configure(text='')
                    for cb in cbs:
                        cb.grid()
                    return
                # ★R264 批17 修的是"勾着的项被折叠藏住"，但"露到最后一个勾选
                # 项为止"这一刀太狠：勾一枚排在第 12 行的 ⇒ 前 12 行全摊开，
                # 卡片被顶得老长（★R308 批64 用户"折叠功能给改的不对了"）。
                # 折叠只允许藏**没勾**的行——露「前 3 行 ∪ 所有勾着的行」，
                # 跳着露不连续没关系：grid 的空行会自动收拢。
                _show = {i for i, _it in enumerate(holder['items'])
                         if var_map.get(_it) is not None and var_map[_it].get()}
                _show.update(range(3))
                if state['expanded']:
                    toggle.configure(text=i18n.tr('收起 ▴', 'collapse'))
                    for cb in cbs:
                        cb.grid()
                else:
                    toggle.configure(text=i18n.tr(
                        '展开 ▾ ({})'.format(len(cbs)), 'expand'))
                    for i, cb in enumerate(cbs):
                        if i in _show:
                            cb.grid()
                        else:
                            cb.grid_remove()

            def _apply_mode(*_a):
                manual = mode_var.get() == 'manual'
                k_cb.configure(state='normal' if not manual else 'disabled')
                # ★R262 批15（用户 15 批 一1）：勾框在 auto 下**不再置灰**——
                # 置灰会让 _on_rank_manual_check（点勾即跳手动）永远点不到，
                # 用户看着一排灰框"手动勾选不能勾"。模式由 radio 决定，
                # 行只是名单，任何时候都能勾。
                for cb in cbs:
                    cb.configure(state='normal')

            def _rebuild_rows():
                """★R262 批14（item4）：榜单随导入/使用统计变化而重建。
                勾选态从 var_map 里那份 BooleanVar 取（新面孔默认不勾）。
                名单没变、且每行绑的仍是字典里那份变量 ⇒ 直接返回——
                刷新是高频动作，别每次拆了重搭。
                ★R266（批21 item3）：失效标注变了也要重建（名单没变但
                "桌面上还在不在"变了，早退会让 ⚠ 一直不出现）。"""
                _stale = dict((getattr(self, '_expl_stale_marks', None)
                               or {}).get(_stale_kind) or {})
                bound = [var_map.get(it) for it in holder['items']]
                if (holder.get('built') == holder['items']
                        and holder.get('bound') == bound
                        and holder.get('stale') == _stale
                        and all(b is not None for b in bound)):
                    # ★R264 批17：名单没变**不等于现场没变**——导入只换勾选
                    # 不换榜单时，这里早退会让"露到勾选项为止"的折叠规则
                    # 不重算（勾着的项继续藏在第 4 行以后）。早退前补一次
                    # 重排，控件不拆重搭、只改露出量。
                    _repaint_toggle()
                    return
                holder['built'] = list(holder['items'])
                holder['stale'] = _stale
                for _w in box.winfo_children():
                    _w.destroy()
                cbs[:] = []
                for i, item in enumerate(holder['items']):
                    var = var_map.get(item)
                    if var is None:
                        var = tk.BooleanVar(value=False)
                        var_map[item] = var
                    note = _stale.get(item)
                    # ★R266（批21）：⚠ 前缀自己也要占那 22 半角的显示预算——
                    # 不预留就把★R262 的截断帽顶穿（T7 实测 26 半角超标），
                    # 三列卡片又会横向溢出。带标注的行按 20 截，前缀补回 2。
                    disp = _disp_clip(_short(item) if is_folder else item,
                                      20 if note else 22)
                    if note:
                        # ⚠ 前缀＝"记着它在、现场却对不上"（改名/删除/挪走）
                        disp = '⚠ ' + disp
                    cb = ttk.Checkbutton(box, text=disp, variable=var,
                                         style='Startup.TCheckbutton',
                                         command=lambda:
                                         self._on_rank_manual_check(mode_var))
                    cb.grid(row=i, column=0, sticky='w', pady=1)
                    if note:
                        self._attach_hover_tip(cb, note)
                    elif disp != item:      # 被缩略/截断过 ⇒ 悬停浮显完整路径
                        self._attach_hover_tip(cb, item)
                    cbs.append(cb)
                holder['bound'] = [var_map.get(it) for it in holder['items']]
                # ★R269（批23 item2）当年在这里补挂 ☑/☐ 文字前缀，是因为主窗只在
                # _build_ui 收尾扫一次、事后重建的行"一个勾都看不见"。★R315 起
                # 勾选态由样式层的原生指示器承担（每个控件出生即带），这一刀不再需要。
                _repaint_toggle()
                _apply_mode()

            _rebuild_rows()
            toggle.bind('<Button-1>',
                        lambda e, s=state, rp=_repaint_toggle: (
                            s.update(expanded=not s['expanded']), rp()))
            mode_var.trace_add('write', _apply_mode)
            return {'cbs': cbs, 'k_cb': k_cb, 'mode': mode_var,
                    'rebuild': _rebuild_rows, 'holder': holder}

        self.expl_folders_enabled_var = tk.BooleanVar(
            value=bool(_so.get('folders_enabled', False)))
        self.expl_folders_mode_var = tk.StringVar(
            value=(_so.get('folders_mode')
                   if _so.get('folders_mode') in ('auto', 'manual') else 'auto'))
        self.expl_top_folders_k_var = tk.StringVar(
            value=str(_so.get('top_folders_k', 3)))
        # ★R262 批15（用户 15 批 一2/3）：已勾选项即使掉出榜单前 20，也要
        # 留在行里并显示为勾着——否则建卡时看不见、下一次保存又把它从盘上
        # 抹掉，用户读到的就是"勾了没生效 / 打开的对不上"。
        _top_folders = list(_top_folders) + [
            p for p in (_so.get('selected_folders') or [])
            if p not in _top_folders]
        _top_icons = list(_top_icons) + [
            n for n in (_so.get('selected_desktop') or [])
            if n not in _top_icons]
        self.expl_selected_folder_vars = {
            p: tk.BooleanVar(value=(p in (_so.get('selected_folders') or [])))
            for p in _top_folders}
        self._expl_folders_col = _rank_column(
            1, i18n.tr('跨组高频文件夹', 'Top folders'),
            self.expl_folders_enabled_var, self.expl_folders_mode_var,
            self.expl_top_folders_k_var, _top_folders,
            self.expl_selected_folder_vars,
            ['3', '5', '8', i18n.tr('全部', 'all')],
            i18n.tr('个', ''), True)

        self.expl_desktop_enabled_var = tk.BooleanVar(
            value=bool(_so.get('desktop_enabled', False)))
        self.expl_desktop_mode_var = tk.StringVar(
            value=(_so.get('desktop_mode')
                   if _so.get('desktop_mode') in ('auto', 'manual') else 'auto'))
        self.expl_top_desktop_k_var = tk.StringVar(
            value=str(_so.get('top_desktop_k', 3)))
        self.expl_selected_icon_vars = {
            n: tk.BooleanVar(value=(n in (_so.get('selected_desktop') or [])))
            for n in _top_icons}
        self._expl_desktop_col = _rank_column(
            2, i18n.tr('打开桌面图标程序', 'Open desktop apps'),
            self.expl_desktop_enabled_var, self.expl_desktop_mode_var,
            self.expl_top_desktop_k_var, _top_icons,
            self.expl_selected_icon_vars,
            ['3', '5', '8', i18n.tr('全部', 'all')],
            i18n.tr('个', ''), False)

    def _rebuild_group_check_rows(self, names, checked):
        """★R262 批14（item4）：重建「按组打开」列的组勾选行。
        checked=None 表示保留各组当前勾选态；否则按名单重置。
        名单没变时只更新勾选态，不拆控件（刷新是高频动作）。"""
        box = getattr(self, '_expl_group_box', None)
        if box is None:
            return
        keep = self.expl_group_check_vars
        if keep.keys() == set(names) and checked is not None:
            for n in names:
                keep[n].set(n in checked)
            return
        for w in box.winfo_children():
            w.destroy()
        new = {}
        for i, name in enumerate(names):
            v = keep.get(name)
            if v is None:
                v = tk.BooleanVar(value=False)
            if checked is not None:
                v.set(name in checked)
            new[name] = v
            ttk.Checkbutton(box, text=_disp_clip(name, 16), variable=v,
                            style='Startup.TCheckbutton',
                            command=self._save_startup_open_settings).grid(
                row=i, column=0, sticky='w', pady=1)
        self.expl_group_check_vars = new
        # ★R269（批23 item2）当年在这里补挂 ☑/☐ 前缀；★R315 起勾选态在样式层，
        # 事后重建的行出生就带指示器，这一刀不再需要。

    def _refresh_stale_marks(self):
        """★R266（批21 item3）：给"记着但现场对不上"的项做体检并留标注。

        用户原话："图标有时候存在改名了，或者图标排行榜里面它在，但实际图标
        已经不在桌面了，所以这种情况要提示下，当然也得考虑其他情况。"

        口径：**只标不删、只标不改名**——统计记录和勾选都是用户自己的数据，
        程序猜"它改名叫 X 了"然后自动改，猜错就是静默毁数据。界面上给
        ⚠ 前缀 + 悬停说明，日志里留一条汇总，动不动由用户决定。
        """
        marks = {'folders': {}, 'icons': {}}
        try:
            from core import explorer_tabs
            marks['icons'] = {k: v for k, v in explorer_tabs.audit_desktop_names(
                list(getattr(self, 'expl_selected_icon_vars', {}))).items() if v}
            marks['folders'] = {
                k: v for k, v in explorer_tabs.audit_folder_paths(
                    list(getattr(self, 'expl_selected_folder_vars', {}))
                ).items() if v}
        except Exception as e:
            logger.warning(f'自动打开设置：失效体检没做成（这次不标 ⚠）: {e}')
        self._expl_stale_marks = marks
        _n = len(marks['icons']) + len(marks['folders'])
        if _n:
            logger.info(
                f'自动打开设置：{_n} 项现场对不上（图标改名/删除/路径失效），'
                f'已在界面上标 ⚠：'
                + '；'.join(list(marks['icons'].values())[:3]
                            + list(marks['folders'].values())[:3]))
        return marks

    def _stats_live_category_names(self):
        """★R281-7（用户 09-30 08 批 item7「统计页三张榜单要不要标 ⚠ 这个肯定要」）：
        给统计页**分类切换榜**当"现在还有哪些分类"的真相源。

        读不动就返回 `[]`（＝这一榜这次不标）——`category_manager.categories` 里若是
        **出厂默认**，拿它去比对会把用户真在用的分类整榜标成 ⚠，比不标更毒（★R230
        导出空备份那条同款教训）。★R281-S6④：这条契约原本只挡住了 `_load_failed`
        那一路，而**最常见**的损坏形态（原件成功另存成 .bak ⇒ 内存换成出厂默认、
        `_load_failed` 仍是 False）从闸门口漏过去，整榜照样误标 ⇒ 两个标记都要看：
        `_load_failed`＝禁写盘，`_fallback_defaults`＝这份名单不可信。"""
        try:
            cm = getattr(self, 'category_manager', None)
            if (cm is None or getattr(cm, '_load_failed', False)
                    or getattr(cm, '_fallback_defaults', False)):
                return []
            return [c.get('name') for c in cm.get_all_categories()
                    if isinstance(c, dict) and c.get('name')]
        except Exception as e:
            logger.warning(f'[统计] 分类名单取不到（分类榜这次不标 ⚠）: {e}')
            return []

    def _resync_startup_open_vars(self):
        """★R262 批14（用户 14 批 item4）：把「自动打开设置」卡上的控件
        与盘上配置**重新对齐**——导入全部数据 / 新建组 / 删除组 / 使用统计
        变化之后都要调一次。

        为什么必须调：这张卡的初值只在建卡时读一次，而任何一次勾选改动都会
        `load_state` 后**用界面上这些变量整份覆盖** startup_open。导入进来的
        设置若不同步进界面，用户随手点一下卡上的开关就会把刚导入的配置
        静默回退成导入前的样子（数据"导入了又没了"）。"""
        if not hasattr(self, 'expl_startup_enabled_var'):
            return          # 卡还没建（首启/降级）——无需同步
        try:
            from core import explorer_tabs
            state = explorer_tabs.load_state(APP_DIR / 'config')
            so = explorer_tabs.sanitize_startup_open(state.get('startup_open'))
            self.expl_startup_enabled_var.set(bool(so['enabled']))
            self.expl_startup_delay_var.set(str(so['delay_s']))
            self.expl_max_folders_var.set(str(so['max_folders_limit']))
            self.expl_max_icons_var.set(str(so['max_icons_limit']))
            self.expl_groups_enabled_var.set(bool(so['groups_enabled']))
            self.expl_startup_recent_var.set(bool(so['smart_recent']))
            self.expl_startup_k_var.set(_top_k_display(so['top_k']))
            self.expl_folders_enabled_var.set(bool(so['folders_enabled']))
            self.expl_folders_mode_var.set(so['folders_mode'])
            self.expl_top_folders_k_var.set(_top_k_display(so['top_folders_k']))
            self.expl_desktop_enabled_var.set(bool(so['desktop_enabled']))
            self.expl_desktop_mode_var.set(so['desktop_mode'])
            self.expl_top_desktop_k_var.set(
                _top_k_display(so['top_desktop_k']))
            names = [g.get('name') for g in
                     explorer_tabs.sorted_groups(state.get('groups', []))
                     if g.get('name')]
            self._rebuild_group_check_rows(names, so['groups'])
            touched = []
            for col, var_map, items, picked in (
                    (getattr(self, '_expl_folders_col', None),
                     self.expl_selected_folder_vars,
                     [f[0] for f in self.usage_stats.top_folders(20)]
                     if hasattr(self, 'usage_stats') else [],
                     so['selected_folders']),
                    (getattr(self, '_expl_desktop_col', None),
                     self.expl_selected_icon_vars,
                     [ic[0] for ic in self.usage_stats.top_icons(20)]
                     if hasattr(self, 'usage_stats') else [],
                     so['selected_desktop'])):
                if col is None:
                    continue
                # ★R262 批15（用户 15 批 一1/2/3）：**就地更新**这份字典，
                # 不再 clear() 后新建——clear 之后控件还抱着旧变量，点勾写进
                # 孤儿、保存读新字典 ⇒ 界面显示勾着、盘上却没这条（用户读到
                # 的"打开的和勾选的对不上"）。同列名走同一条路（对齐
                # _rebuild_group_check_rows 的写法）。
                # 名单 = 榜单 ∪ 已勾选项：某项掉出前 20 也不能把它悄悄丢掉，
                # 否则下一次保存就把它从盘上抹了（只进不出＝白勾）。
                items = list(items)
                for it in picked:
                    if it not in items:
                        items.append(it)
                for it in items:
                    v = var_map.get(it)
                    if v is None:
                        var_map[it] = tk.BooleanVar(value=(it in picked))
                    else:
                        v.set(it in picked)
                for it in [k for k in var_map if k not in items]:
                    del var_map[it]
                col['holder']['items'] = items
                touched.append(col)
            # ★R266（批21 item3）：失效体检放在名单对齐**之后**——audit 读的就是
            # 这两列的行名，先体检后补名单 ⇒ 导入进来的新面孔第一轮永远不标 ⚠
            # （实测：第一次 resync 行文本干净，第二次才挂 ⚠）。
            self._refresh_stale_marks()
            for col in touched:
                col['rebuild']()
        except Exception as e:
            logger.error(f'同步自动打开设置卡失败（界面可能与配置不一致）: {e}')

    def _attach_hover_tip(self, widget, full_text):
        """★R262（用户 12 批补充）：截断过的行悬停浮显完整路径。

        无边框小窗跟随鼠标出现、离开即销毁；tk 原生控件（非 ttk）画
        主题色 Label。只在显示文本被缩略/截断时挂，短名不打扰。"""
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
                w = tk.Toplevel(widget)
                w.wm_overrideredirect(True)
                # overrideredirect 窗不会自动抬到 topmost 主窗之上（实测被
                # 压在下面看不见）——显式置顶 + lift。
                w.attributes('-topmost', True)
                w.wm_geometry('+%d+%d' % (ev.x_root + 14, ev.y_root + 10))
                tk.Label(w, text=full_text, bg=COLORS['surface'],
                         fg=COLORS['fg'], font=ui_layout.app_font(9),
                         padx=8, pady=4, wraplength=520, justify=tk.LEFT,
                         highlightbackground=COLORS['border'],
                         highlightthickness=1).pack()
                tip['w'] = w
            except Exception:
                pass

        widget.bind('<Enter>', _show)
        widget.bind('<Leave>', _hide)
        widget.bind('<Destroy>', _hide)

    def _apply_app_icon(self):
        """★R266（批21 item1）：把选定的 ico 设成窗口/任务栏图标。

        Tk 在 Windows 只认 .ico；`iconbitmap(default=...)` 之后所有窗口
        （对话框/子窗）都跟着换。取不到文件就什么都不做——沿用系统默认那一枚，
        绝不因为图标问题影响启动（设置页的「🎨 程序图标」卡是用户换图的入口）。
        """
        try:
            from core import app_icon
            p = app_icon.resolve(APP_DIR, self._app_settings)
            if p is None:
                logger.warning('程序图标：images/app_icons 里没有可用的 ico，'
                               '窗口沿用系统默认图标')
                return False
            if not app_icon.apply_to_window(self.root, p):
                logger.warning(f'程序图标设不上（沿用系统默认）: {p.name}')
                return False
            self._app_icon_path = p
            return True
        except Exception as e:
            logger.warning(f'程序图标应用失败（不影响使用）: {e}')
            return False

    def _on_rank_manual_check(self, mode_var):
        """★R262（用户 56 批 三3）：勾了手动项 ⇒ 自动切到 manual 模式
        （radio 跳过去、K 下拉随之置灰）；取消勾选不反向切回 auto
        （用户可能还想再勾回来，界面保持 manual 空选状态）。"""
        mode_var.set('manual')
        self._save_startup_open_settings()
    def _keep_hidden_ticks(self, prev_list, var_map, what, alive=None):
        """★R266（批21 item2）：界面上**压根没有这一行**的盘上勾选，不许被冲掉。

        卡片只在 `_resync_startup_open_vars()` 跑成功时才跟着盘刷新；导入/回滚
        那两条路把异常吞成一条 warning（win11 实测形状：跨组高频的勾找不见了，
        重启又回来）⇒ 这时界面字典里**少了**盘上勾着的那几项。用户随后点卡上
        任何一个开关都会"以界面为准"整份覆盖盘，那几项就从盘上被抹掉、**重启也
        回不来**。口径：看得见的项目以界面为准（勾了写、取消删），看不见的项目
        原样保留——"框里没这一行"不等于"用户取消了"。

        `alive` 给定时（组列）只保留**组还在**的那几项——用户真删了组，
        它的勾就该跟着走，不能复活成死名字。
        """
        seen = set(var_map)
        hidden = [p for p in (prev_list or [])
                  if isinstance(p, str) and p and p not in seen
                  and (alive is None or p in alive)]
        if hidden:
            logger.warning(
                f'自动打开设置：{what}有 {len(hidden)} 项在界面上没有对应行'
                f'（名单未刷新或已掉出榜单前 20），本次保存按原样保留，不覆盖')
        return hidden

    def _save_startup_open_settings(self):
        """★R262 把卡上三列设置落盘（先落盘再生效；生效点在启动钩子）。

        数字字段（延时/K/上限）原样传字符串，sanitize 统一区间消毒——
        下拉候选只是提示，手动输入同样合法。
        勾选顺序语义：按界面上的显示序收集。
        """
        try:
            from core import explorer_tabs
            state = explorer_tabs.load_state(APP_DIR / 'config')
            _so_prev = state.get('startup_open') or {}
            prev = _so_prev.get('groups') or []
            cur = [g for g, v in self.expl_group_check_vars.items() if v.get()]
            curset = set(cur)
            ordered = [g for g in prev if g in curset]
            ordered += [g for g in cur if g not in ordered]
            ordered += self._keep_hidden_ticks(
                prev, self.expl_group_check_vars, '按组打开',
                alive={g.get('name') for g in (state.get('groups') or [])
                       if isinstance(g, dict)})
            _keep_f = self._keep_hidden_ticks(
                _so_prev.get('selected_folders'),
                self.expl_selected_folder_vars, '跨组高频文件夹')
            _keep_d = self._keep_hidden_ticks(
                _so_prev.get('selected_desktop'),
                self.expl_selected_icon_vars, '桌面图标')
            raw = {'enabled': bool(self.expl_startup_enabled_var.get()),
                   'delay_s': self.expl_startup_delay_var.get(),
                   'groups': ordered,
                   'smart_recent': bool(self.expl_startup_recent_var.get()),
                   'top_k': self.expl_startup_k_var.get(),
                   'groups_enabled': bool(self.expl_groups_enabled_var.get()),
                   'folders_enabled': bool(self.expl_folders_enabled_var.get()),
                   'folders_mode': self.expl_folders_mode_var.get(),
                   'top_folders_k': self.expl_top_folders_k_var.get(),
                   'selected_folders': [
                       p for p, v in self.expl_selected_folder_vars.items()
                       if v.get()] + _keep_f,
                   'max_folders_limit': self.expl_max_folders_var.get(),
                   'desktop_enabled': bool(
                       self.expl_desktop_enabled_var.get()),
                   'desktop_mode': self.expl_desktop_mode_var.get(),
                   'top_desktop_k': self.expl_top_desktop_k_var.get(),
                   'selected_desktop': [
                       n for n, v in self.expl_selected_icon_vars.items()
                       if v.get()] + _keep_d,
                   'max_icons_limit': self.expl_max_icons_var.get()}
            clean = explorer_tabs.sanitize_startup_open(raw)
            state['startup_open'] = clean
            explorer_tabs.save_state(APP_DIR / 'config', state)
            # 手输被消毒折回时把框里的值同步回真值，界面不骗人
            self.expl_startup_delay_var.set(str(clean['delay_s']))
            self.expl_max_folders_var.set(str(clean['max_folders_limit']))
            self.expl_max_icons_var.set(str(clean['max_icons_limit']))
            self.expl_top_folders_k_var.set(_top_k_display(clean['top_folders_k']))
            self.expl_top_desktop_k_var.set(_top_k_display(clean['top_desktop_k']))
            self.expl_startup_k_var.set(_top_k_display(clean['top_k']))
        except Exception as e:
            logger.error(f'保存启动自动打开设置失败: {e}')
    def _startup_open_folders(self, manual: bool = False):
        """★R258 启动钩子：按 startup_open 计划逐组打开（后台线程）。

        这是用户**显式**勾出来的动作，不走 _link_suppress 静默窗（那窗防的
        是联动路径双开）；open_folders 同步阻塞 3~5 秒 ⇒ 挪后台线程（★P1-2
        同款），UI/日志回传走 _ui_queue。只记日志不弹框。

        ★R262（用户 61 批）manual=True：悬浮条「一键打开」手动触发——
        用户点击本身就是意愿，总开关只约束"开机自动"，这里强制放行；
        三列各自的启用开关仍是计划口径（勾了哪列开哪列）。
        """
        def _work():
            try:
                from core import explorer_tabs
                state = explorer_tabs.load_state(APP_DIR / 'config')
                if manual:
                    so = dict(state.get('startup_open') or {})
                    so['enabled'] = True
                    state = dict(state, startup_open=so)
                plans, warns = explorer_tabs.resolve_startup_plan(
                    state, usage_stats=self.usage_stats)
                for w in warns:
                    self._ui_queue.put(lambda t=w: self._log(t))
                if not plans:
                    if not warns:
                        self._ui_queue.put(lambda: self._log(
                            '🚀 启动自动打开：没有勾选任何组（或组都空着），'
                            '本次跳过' if not manual else
                            '🚀 一键打开：三列还没勾选任何启用（去资源页'
                            '「自动打开设置」卡配置），本次跳过'))
                    return
                as_tabs = bool(state.get('as_tabs', True))
                for p in plans:
                    group_type = p['group']
                    if group_type == 'desktop':
                        # ★R262（用户 61 批）：桌面图标列接通真打开——
                        # 按名命中桌面目录文件（.lnk=双击语义）/shell 虚拟
                        # 图标；命不中的名字进日志不静默。
                        icon_names = p['folders']
                        try:
                            opened, missed = explorer_tabs.open_desktop_icons(
                                icon_names)
                        except Exception as e:
                            logger.error(f'桌面图标打开失败: {e}')
                            continue
                        _tag = '一键打开' if manual else '启动自动打开'
                        if missed:
                            # ★R266（批21 item3）：现场对不上 ⇒ 顺手刷一次
                            # 失效标注（设置页那两列会挂上 ⚠），不静默
                            self._ui_queue.put(self._resync_startup_open_vars)
                        self._ui_queue.put(
                            lambda _o=len(opened), _tt=icon_names,
                            _m=missed, _tag=_tag: self._log(
                                f'🚀 {_tag} [桌面图标]: {_o}/{len(_tt)} 个'
                                + (f'（没找到: {", ".join(_m[:3])}'
                                   f'{"..." if len(_m) > 3 else ""}）'
                                   if _m else '')))
                    else:
                        # 第1列/第2列：文件夹组或高频文件夹
                        # ★R264 批17（用户 B2c）new_window=True：每一组各自一个
                        # 资源管理器窗口，不让 Win11 把第2列的高频文件夹并进
                        # 第1列那组的选项卡条里
                        try:
                            n = explorer_tabs.open_folders(
                                p['folders'], as_tabs=as_tabs,
                                new_window=True) or 0
                        except Exception as e:
                            logger.error(
                                f'启动自动打开组 [{group_type}] 失败: {e}')
                            continue
                        if group_type != 'folders':
                            try:
                                explorer_tabs.touch_group(
                                    APP_DIR / 'config', group_type)
                            except Exception:
                                pass
                        _nn, _tt = n, p
                        self._ui_queue.put(lambda _nn=_nn, _tt=_tt: self._log(
                            f'🚀 启动自动打开 [{_tt["group"]}]: '
                            f'{_nn}/{len(_tt["folders"])} 个文件夹'))
            except Exception as e:
                logger.error(f'启动自动打开未执行: {e}')
        import threading
        threading.Thread(target=_work, daemon=True).start()

    def _on_explorer_group_chip(self, name: str):
        """单击组条：选中为当前组（记忆），悬浮条一键打开即打开它"""
        from core import explorer_tabs
        explorer_tabs.set_active_group(APP_DIR / 'config', name)
        self._refresh_explorer_list()
        self._log(f'📂 当前文件夹组: [{name}]（悬浮条一键打开即打开它）')

    def _on_explorer_group_open(self, name: str):
        """双击组条：直接打开该组的全部文件夹"""
        from core import explorer_tabs
        explorer_tabs.set_active_group(APP_DIR / 'config', name)
        for g in explorer_tabs.load_state(APP_DIR / 'config').get('groups', []):
            if g['name'] == name:
                try:
                    opened = explorer_tabs.open_folders(
                        g.get('folders', []),
                        as_tabs=bool(explorer_tabs.load_state(
                            APP_DIR / 'config').get('as_tabs', True)))
                    self._log(f'📂 已直接打开 [{name}]: {opened}/{len(g.get("folders", []))} 个文件夹')
                except Exception as e:
                    logger.error(f'双击打开组失败: {e}')
                break

    def _on_open_category_folders(self):
        """主界面一键打开分类的文件夹组：选中分类有同名组打开之；
        没有则回退打开当前激活组（2026-09-05 用户需求）"""
        names = self._selected_category_names()
        if not names:
            messagebox.showwarning('提示', '请先在左侧选择一个分类', parent=self.root)
            return
        from core import explorer_tabs
        state = explorer_tabs.load_state(APP_DIR / 'config')
        groups = {g['name']: g for g in state.get('groups', [])}
        cat = names[0]
        if cat in groups and groups[cat].get('folders'):
            explorer_tabs.set_active_group(APP_DIR / 'config', cat)
            folders = groups[cat]['folders']
            src_desc = f'同名文件夹组 [{cat}]'
        else:
            folders = explorer_tabs.get_active_folders(state)
            src_desc = f'当前组 [{state.get("active_group") or "无"}]'
        if not folders:
            self._log(f'📂 [{cat}] 没有文件夹组，当前组也没有文件夹')
            return
        try:
            opened = explorer_tabs.open_folders(
                folders, as_tabs=bool(state.get('as_tabs', True)))
            self._log(f'📂 已打开 {src_desc}: {opened}/{len(folders)} 个文件夹')
        except Exception as e:
            logger.error(f'打开分类文件夹失败: {e}')
            messagebox.showerror('错误', f'打开失败: {e}', parent=self.root)

    def _show_explorer_tabs_info(self):
        """★ R72：文件夹组打开方式说明（替代原先的自动弹窗，想看再看）"""
        messagebox.showinfo(
            '文件夹组打开方式说明',
            '打开文件夹组时，程序会优先把多个文件夹合并成一个\n'
            '文件资源管理器窗口的多个选项卡（不占任务栏）。\n\n'
            '● Windows 11 22H2 及以上：支持选项卡合并，\n'
            '   一组文件夹 = 一个窗口里的多个标签页。\n'
            '● Windows 10 / 旧版 Windows 11：系统不支持选项卡控制，\n'
            '   自动退化为"多窗口"方式——每个文件夹开一个独立窗口，\n'
            '   功能不受影响，只是窗口数量多一些。\n\n'
            '无论哪种方式，组的保存/切换/一键打开都完全一样。\n'
            '降级发生时不会打扰你，日志里会记一条 ⚠ 提示。',
            parent=self.root)

    def _on_save_explorer_group(self):
        """把当前打开的资源管理器窗口保存为一个组（输入组名，同名覆盖）"""
        from core import explorer_tabs
        folders = explorer_tabs.list_open_folders()
        if not folders:
            messagebox.showinfo('提示', '当前没有打开的资源管理器窗口', parent=self.root)
            return
        state = explorer_tabs.load_state(APP_DIR / 'config')
        default = state.get('active_group') or ''
        name = simpledialog.askstring(
            '存为文件夹组',
            f'把当前打开的 {len(folders)} 个文件夹存为组，组名:',
            initialvalue=default, parent=self.root)
        if not name:
            return
        name = name.strip()
        if not name:
            self._log('⚠ 组名不能为空')
            return
        groups = state.get('groups', [])
        for g in groups:
            if g['name'] == name:
                g['folders'] = folders      # 同名覆盖
                break
        else:
            # ★R232（用户 06 批 item2）：新建组默认**不**勾"点分类自动打开"，
            # 显式落 False，不靠 load_state 的兜底碰运气
            groups.append({'name': name, 'folders': folders,
                           'auto_open': False})
        state['groups'] = groups
        state['active_group'] = name
        state['as_tabs'] = bool(self.explorer_as_tabs_var.get())
        explorer_tabs.touch_group(APP_DIR / 'config', name)
        explorer_tabs.save_state(APP_DIR / 'config', state)
        self._refresh_explorer_list()
        self._log(f'💾 已保存文件夹组 [{name}]（{len(folders)} 个文件夹）')

    def _on_create_explorer_group(self):
        """★R262（用户 12 批 1）：新建空文件夹组。

        此前只有"存为组（当前窗口）"——没开任何资源管理器窗口就根本建不了组。
        新建后组是空的，用旁边"➕ 添加文件夹"往里放路径即可。"""
        from core import explorer_tabs
        state = explorer_tabs.load_state(APP_DIR / 'config')
        name = simpledialog.askstring('新建文件夹组', '组名:', parent=self.root)
        if not name:
            return
        name = name.strip()
        if not name:
            self._log('⚠ 组名不能为空')
            return
        groups = state.get('groups', [])
        if any(g.get('name') == name for g in groups):
            messagebox.showinfo('提示', f'组 [{name}] 已存在，直接给它"添加文件夹"即可',
                                parent=self.root)
            return
        groups.append({'name': name, 'folders': [], 'auto_open': False})
        state['groups'] = groups
        state['active_group'] = name
        explorer_tabs.touch_group(APP_DIR / 'config', name)
        explorer_tabs.save_state(APP_DIR / 'config', state)
        self._refresh_explorer_list()
        self._log(f'➕ 已新建文件夹组 [{name}]：点"➕ 添加文件夹"往里放路径')

    def _on_delete_explorer_group(self):
        """删除当前选中的组"""
        from core import explorer_tabs
        name = explorer_tabs.load_state(APP_DIR / 'config').get('active_group', '')
        if not name:
            messagebox.showwarning('提示', '当前没有选中的组', parent=self.root)
            return
        if not messagebox.askyesno('确认', f'删除文件夹组 [{name}]？', parent=self.root):
            return
        from core import explorer_tabs
        state = explorer_tabs.load_state(APP_DIR / 'config')
        state['groups'] = [g for g in state.get('groups', []) if g['name'] != name]
        state['active_group'] = (state['groups'][0]['name']
                                 if state['groups'] else '')
        explorer_tabs.save_state(APP_DIR / 'config', state)
        self._refresh_explorer_list()
        self._log(f'🗑️ 已删除文件夹组 [{name}]')

    def _on_open_explorer_folders(self, explicit: bool = False):
        """打开当前组的文件夹（悬浮条一键打开/资源管理页/切分类联动共用）

        explicit=True：用户显式操作（子菜单点选/单组直开），
        不受启动静默窗限制——静默窗只为防止联动路径双开。
        open_folders 是同步阻塞流程：先把状态提示画上屏，再延迟执行，
        否则"正在打开…"在打开完成前根本看不见（2026-09-05 用户要求提醒）"""
        import time as _t2
        if not explicit and _t2.time() < getattr(self, '_link_suppress_until', 0):
            return   # 启动恢复期：联动静默，防止双开
        self._diag_event('open_folders')     # ★R232：开文件夹=最可能的前台易主点
        try:
            self.explorer_open_status_var.set(
                '⏳ 正在打开文件夹…（逐个合并选项卡，需要几秒钟）'
                '——期间请不要点击桌面或其他窗口，全部打开后即可操作')
            self.root.update_idletasks()
        except Exception:
            pass
        self.root.after(150, lambda: self._do_open_explorer_folders(explicit))

    def _do_open_explorer_folders(self, explicit: bool = False):
        """实际执行打开（由 _on_open_explorer_folders 延迟调度）

        ★ P1-2：open_folders 是 Win11 选项卡同步阻塞调用（3~5 秒），
        旧版在主线程跑会冻 UI。这里把阻塞部分挪到后台线程，
        UI 提示/弹框通过 root.after(0, ...) 回主线程更新。
        """
        def _work():
            try:
                from core import explorer_tabs
                state = explorer_tabs.load_state(APP_DIR / 'config')
                folders = explorer_tabs.get_active_folders(state)
                if not folders:
                    self._ui_queue.put(lambda: self._set_open_status(
                        '当前组没有文件夹：先"存为组"或添加文件夹'))
                    self._ui_queue.put(lambda: self._log(
                        '📂 当前组没有文件夹：先"存为组"或添加文件夹'))
                    return
                as_tabs = bool(state.get('as_tabs', True))

                def _on_degrade(reason: str):
                    # ★ R72：不再弹窗（每次会话弹一次+系统提示音太打扰，
                    # 用户拍板去掉）——降级只记日志；想了解差异看
                    # 📂 资源 页的"📖 说明"按钮，点开才看。
                    try:
                        self._log(f'⚠ 选项卡{reason}，已改为多窗口方式打开'
                                  '文件夹组（详见 📂 资源 页"📖 说明"）')
                    except Exception:
                        pass

                opened = explorer_tabs.open_folders(
                    folders, as_tabs=as_tabs, on_degrade=_on_degrade)
                try:
                    active = state.get('active_group')
                    if active:
                        explorer_tabs.touch_group(APP_DIR / 'config', active)
                except Exception:
                    pass
                _n_folders = len(folders)
                self._ui_queue.put(lambda: self._log(
                    f'📂 已打开 {opened}/{_n_folders} 个文件夹'))
                if opened < _n_folders:
                    # ★ B10：组内有失效路径（文件夹被改名/删除）时，open_folders
                    # 按设计吞掉单个失败继续开其余的——把"部分失败"显性化，
                    # 否则用户不知道有目录没开（2026-09-12 用户实测反馈）
                    _miss = _n_folders - opened
                    self._ui_queue.put(lambda: self._set_open_status(
                        f'⚠ 已打开 {opened}/{_n_folders} 个'
                        f'（{_miss} 个路径失效，详见运行日志）'))
                else:
                    self._ui_queue.put(lambda: self._set_open_status(
                        f'✅ 已打开 {opened}/{_n_folders} 个文件夹，可以正常操作了'))
                self._ui_queue.put(lambda: self.root.after(
                    8000, lambda: self._set_open_status('')))
            except Exception as e:
                err = str(e)
                logger.error(f'打开文件夹组失败: {e}')
                self._ui_queue.put(lambda: self._set_open_status(
                    '❌ 打开文件夹组失败，详见运行日志'))
                # ★ B1：lambda 经 _ui_queue 延迟到 except 块外执行，直接引用 e
                #   会因块尾 del e 抛 NameError（Python 3.14 实测），错误弹窗
                #   静默丢失——先固化为字符串再进闭包
                self._ui_queue.put(lambda: messagebox.showerror(
                    '错误', f'打开文件夹组失败: {err}', parent=self.root))
        threading.Thread(target=_work, daemon=True).start()

    def _set_open_status(self, text: str):
        """资源管理页的打开状态行（打开过程/结果提示）"""
        try:
            self.explorer_open_status_var.set(text)
        except Exception:
            pass

    def _on_add_explorer_folder(self):
        """浏览选择一个文件夹加入当前组"""
        from tkinter import filedialog
        path = filedialog.askdirectory(parent=self.root)
        if not path:
            return
        # ★R262 批14（用户 14 批 问题1）：askdirectory 返回正斜杠
        # （D:/a/b），与 COM 枚举/手输的反斜杠口径不一 ⇒ 入组前统一 normpath
        self._append_paths_to_active_group([path], '浏览选择')

    def _append_paths_to_active_group(self, paths, src_label):
        """★R262 批14（用户 14 批 3）：把若干路径**追加**进当前组——
        不清空原有成员（"存为组"是整组覆盖，已建好组时想"顺带加一个"
        就不用重存整组）。统一 normpath 反斜杠口径；已在组内/路径失效跳过并汇报。
        返回新增数（-1 = 当前没有组）。"""
        from core import explorer_tabs
        state = explorer_tabs.load_state(APP_DIR / 'config')
        active = state.get('active_group')
        for g in state.get('groups', []):
            if g['name'] != active:
                continue
            have = g.setdefault('folders', [])
            # ★R262 S6-F-A：`load_state` 的消毒**故意保留**非字符串成员
            # （`core/explorer_tabs.py:402`，不让一次手改坏掉的 JSON 毁掉整组），
            # 所以这里不能对既有成员无条件 normpath——遇到 None/数字会 TypeError，
            # 三个"加文件夹"入口当场瘫。非字符串成员按"已在组内"处理，不参与比较。
            norm_have = [os.path.normpath(x).lower() for x in have
                         if isinstance(x, str) and x]
            added, dup, bad = [], [], []
            for raw in paths:
                p = os.path.normpath(raw.strip()) if raw.strip() else ''
                if not p:
                    continue
                if not os.path.isdir(p):
                    bad.append(p)
                elif p.lower() in norm_have:
                    dup.append(p)
                else:
                    have.append(p)
                    norm_have.append(p.lower())
                    added.append(p)
            if added:
                explorer_tabs.save_state(APP_DIR / 'config', state)
                self._refresh_explorer_list()
            msg = f'📂 {src_label} → 组 [{active}]：新增 {len(added)} 个'
            if dup:
                msg += f'，已在组内 {len(dup)} 个'
            if bad:
                msg += f'，无效 {len(bad)} 个（不存在或不是文件夹）'
            self._log(msg)
            return len(added)
        self._log('⚠ 当前没有组：先"➕ 新建组"或"💾 存为组"')
        return -1

    def _on_add_to_group_dialog(self):
        """★R262 批14（用户 14 批 3）：「添加到当前组」——一个入口两种来路：
        ① 把当前打开的资源管理器窗口追加进来；② 粘贴地址（每行一个 / ; 分隔），
        点"📋 粘贴"直接从剪贴板补入，比走系统浏览框快。"""
        from core import explorer_tabs
        state = explorer_tabs.load_state(APP_DIR / 'config')
        active = state.get('active_group')
        if not active or not any(g['name'] == active
                                 for g in state.get('groups', [])):
            self._log('⚠ 当前没有组：先"➕ 新建组"或"💾 存为组"')
            return
        dlg = tk.Toplevel(self.root)
        # tr(zh, en) 两个位置参数都必填，少传 en 直接 TypeError（点按钮就崩）
        dlg.title(i18n.tr('添加到当前组 [{}]', 'Add to current group [{}]'
                         ).format(active))
        dlg.transient(self.root)
        dlg.resizable(False, False)
        ttk.Label(dlg, text=i18n.tr(
            '粘贴文件夹地址（每行一个，也可用 ; 分隔多个）：',
            'Paste folder paths (one per line, or ; separated):'),
            font=ui_layout.app_font(9)).pack(anchor=tk.W, padx=10, pady=(10, 2))
        txt = tk.Text(dlg, width=62, height=5, font=self.ui_font,
                      bg=COLORS['surface'], fg=COLORS['fg'],
                      insertbackground=COLORS['fg'], relief=tk.FLAT)
        txt.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)

        def _paste():
            try:
                clip = self.root.clipboard_get().strip()
            except Exception:
                clip = ''
            if clip:
                base = txt.get('1.0', 'end').rstrip()
                txt.delete('1.0', 'end')
                txt.insert('1.0', (base + '\n' if base else '') + clip)

        def _close():
            dlg.destroy()

        def _from_open_windows():
            folders = explorer_tabs.list_open_folders()
            if not folders:
                self._log('📂 当前没有打开的资源管理器窗口')
                return
            self._append_paths_to_active_group(folders, '当前打开的窗口')
            _close()

        def _from_paste():
            raw = txt.get('1.0', 'end')
            paths = [s.strip() for s in
                     raw.replace('；', ';').replace('\n', ';').split(';')
                     if s.strip()]
            if not paths:
                self._log('⚠ 粘贴框里没有可识别的路径')
                _close()
                return
            self._append_paths_to_active_group(paths, '粘贴地址')
            _close()

        row = ttk.Frame(dlg)
        row.pack(fill=tk.X, padx=10, pady=(2, 10))
        ttk.Button(row, text=i18n.tr('📋 粘贴', '📋 Paste'),
                   command=_paste).pack(side=tk.LEFT)
        ttk.Button(row, text=i18n.tr('➕ 当前打开的窗口',
                                     '➕ Open Windows'),
                   command=_from_open_windows).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(row, text=i18n.tr('✔ 加入', '✔ Add'),
                   command=_from_paste).pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(row, text=i18n.tr('取消', 'Cancel'),
                   command=_close).pack(side=tk.RIGHT)
        dlg.bind('<Escape>', lambda e: _close())
        try:
            dlg.geometry('+{}+{}'.format(self.root.winfo_rootx() + 120,
                                         self.root.winfo_rooty() + 160))
        except Exception:
            pass
        txt.focus_set()

    def _on_delete_explorer_folder(self):
        """删除当前组内选中的文件夹（支持多选）"""
        sel = list(self.explorer_list.curselection())
        if not sel:
            messagebox.showwarning('提示', '请先选择要删除的文件夹（可多选）',
                                   parent=self.root)
            return
        from core import explorer_tabs
        state = explorer_tabs.load_state(APP_DIR / 'config')
        active = state.get('active_group')
        for g in state.get('groups', []):
            if g['name'] == active:
                for i in sorted(sel, reverse=True):
                    if 0 <= i < len(g['folders']):
                        g['folders'].pop(i)
                break
        explorer_tabs.save_state(APP_DIR / 'config', state)
        self._refresh_explorer_list()
        self._log(f'🗑️ 已从组 [{active}] 删除 {len(sel)} 个文件夹记录')

    def _on_move_explorer_folder(self, delta: int):
        """上移/下移选中文件夹（调整打开顺序，单选）"""
        sel = self.explorer_list.curselection()
        if not sel or len(sel) != 1:
            messagebox.showwarning('提示', '请先选中一个（只能单选调整顺序）',
                                   parent=self.root)
            return
        i = sel[0]
        from core import explorer_tabs
        state = explorer_tabs.load_state(APP_DIR / 'config')
        active = state.get('active_group')
        for g in state.get('groups', []):
            if g['name'] == active:
                folders = g['folders']
                j = i + delta
                if 0 <= j < len(folders):
                    folders[i], folders[j] = folders[j], folders[i]
                    explorer_tabs.save_state(APP_DIR / 'config', state)
                    self._refresh_explorer_list()
                    self.explorer_list.selection_set(j)
                    self.explorer_list.see(j)
                return

    def _on_explorer_list_double_click(self, event=None):
        """双击列表项：直接打开该文件夹"""
        sel = self.explorer_list.curselection()
        if not sel:
            return
        from core import explorer_tabs
        state = explorer_tabs.load_state(APP_DIR / 'config')
        folders = explorer_tabs.get_active_folders(state)
        if 0 <= sel[0] < len(folders):
            p = folders[sel[0]]
            # ★R233（用户 08 批 item4，★R230 挂账 1）：这里曾直接 os.startfile，
            #   等于绕过了 ★R230 给 open_folders 入口加的 _safe_folders 闸。
            #   组里的 folders 整份可以来自「📦 数据管理 → 导入全部数据」，
            #   填的是 .exe / 他人 UNC 时，双击这一下就是运行程序或带凭据外联。
            #   判据与 open_folders 共用 explorer_tabs.is_openable（口径只留一份）。
            if not explorer_tabs.is_openable(p):
                logger.warning(f'[文件夹组] 双击打开被拒绝（不是现存目录）: {p!r}')
                messagebox.showwarning(
                    i18n.tr('无法打开', 'Cannot Open'),
                    i18n.tr('这个条目不是一个真实存在的文件夹，已拒绝打开。\n'
                            '（文件夹组可能来自导入的备份文件，'
                            '请先核对组里记录的路径）',
                            'This entry is not an existing folder, so opening it '
                            'was refused.\n(The group may come from an imported '
                            'backup file - check the paths recorded in it.)'),
                    parent=self.root)
                self._log('⛔ 已拒绝打开一个不是文件夹的条目（详见日志）')
                return
            try:
                os.startfile(p)
                explorer_tabs.notify_folder_open(p)   # ★ B18 统计
            except Exception as e:
                messagebox.showerror(i18n.tr('错误', 'Error'),
                                     f'{i18n.tr("打开失败", "Open failed")}: {e}',
                                     parent=self.root)

    def _on_explorer_group_auto_open_toggle(self):
        """当前组"点分类自动打开"开关（分类级，写进该组的 auto_open）"""
        from core import explorer_tabs
        state = explorer_tabs.load_state(APP_DIR / 'config')
        active = state.get('active_group')
        if not active:
            self._log('⚠ 尚未选中文件夹组')
            return
        for g in state.get('groups', []):
            if g['name'] == active:
                g['auto_open'] = bool(self.explorer_auto_group_var.get())
                explorer_tabs.save_state(APP_DIR / 'config', state)
                self._log('🔗 组 [' + active + '] 点分类自动打开: '
                          + ('开' if g['auto_open'] else '关'))
                return

    def _on_explorer_link_toggle(self):
        """联动开关：切分类时同时打开同名文件夹组"""
        from core import explorer_tabs
        state = explorer_tabs.load_state(APP_DIR / 'config')
        state['link_switch'] = bool(self.explorer_link_var.get())
        explorer_tabs.save_state(APP_DIR / 'config', state)
        self._log('🔗 分类联动文件夹组: '
                  + ('开（切分类时同名文件夹组一并打开）' if state['link_switch'] else '关'))

    def _on_switch_link_folders(self, name: str):
        """悬浮条切分类后调用：同名文件夹组勾了"点分类自动打开"则打开"""
        self._diag_event(f'link:{name}')      # ★R232：联动开文件夹是前台跃变高发点
        try:
            from core import explorer_tabs
            state = explorer_tabs.load_state(APP_DIR / 'config')
            for g in state.get('groups', []):
                if g['name'] == name and g.get('folders'):
                    # 分类级开关（2026-09-05 从全局 link_switch 改为每组 auto_open）
                    # ★R232（用户 06 批 item2）：默认关——没显式勾选过的组不再跟着开
                    if not g.get('auto_open', False):
                        return
                    explorer_tabs.set_active_group(APP_DIR / 'config', name)
                    # 延迟 800ms：等桌面切换完成再弹窗口
                    self.root.after(800, self._on_open_explorer_folders)
                    self._log(f'🔗 联动: 已同时打开 [{name}] 文件夹组')
                    return
        except Exception as e:
            logger.error(f"分类联动文件夹组失败: {e}")

    def _on_ui_language_change(self, event=None):
        """★ R73：切换界面语言（中文/English）。

        Tkinter 界面文案是构建时求值，热切换需重造全部控件（风险高收益
        低），按"重启生效"设计：此处只落盘+双语提示。"""
        lang = self.ui_lang_var.get()
        self._app_settings['ui_language'] = lang
        self._save_app_settings()
        i18n.set_language(lang)
        self._log(f'🌐 {i18n.tr("语言已切换为", "Language switched to")}'
                  f' [{lang}]')
        # ★ R79：一键重启（用户反馈：主流软件切语言即时生效——Tk 文案构建时
        # 求值做不到热切换，自动重启是最接近的体验）
        if messagebox.askyesno(
                i18n.tr('语言已切换', 'Language switched'),
                i18n.tr('设置已保存。重启程序后界面语言完全生效——现在自动重启吗？',
                        'Settings saved. A restart fully applies the new '
                        'language — restart now?'),
                parent=self.root):
            self._restart_for_language()

    def _restart_for_language(self, reason='语言'):
        """★ R79：保存并自动重启进程（语言切换用）。

        ★R225 阶段5：界面密度/图标风格也走这条接力（reason 传"显示设置"）——
        名字保留 for_language 是因为它是全工程唯一一处"等旧实例退出再起新进程"
        的实现，改名会牵动 R79d/R150 的回归断言，不划算。

        关键时序：等本实例完全退出（单实例互斥体消失）后再拉起新进程，
        否则新进程会被单实例保护当成"重复启动"——唤醒旧窗然后自己退出。"""

        def _spawn_new():
            # ★ R79d：立即带 --relaunch-wait 旗标拉起，接力等待由新进程做
            # （旧实现在本进程内等互斥体 = 结构性死锁，30 秒后才启动）
            # ★ R174 C5：get_executable——Nuitka onefile 下取外层 exe
            try:
                from utils.app_paths import get_executable
                if getattr(sys, 'frozen', False):
                    subprocess.Popen([get_executable(), '--relaunch-wait'],
                                     cwd=str(APP_DIR))
                else:
                    subprocess.Popen([sys.executable] + sys.argv
                                     + ['--relaunch-wait'], cwd=str(APP_DIR))
            except Exception as e:
                logger.error(f"语言切换自动重启失败: {e}")

        _spawn_timer = threading.Timer(1.0, _spawn_new)
        _spawn_timer.daemon = False   # 非守护：1 秒后拉起，之后进程可退出
        _spawn_timer.start()
        self._log(f'🔄 即将重启以应用新{reason}…')
        # ★ R150 修复：必须 force_exit 完整退出——用户开了"保留悬浮框"时
        # _on_close 只隐藏主窗不退出（进程握着互斥体），接力新进程等 60s
        # 超时后必然失败（实测"点 Y 无法自动拉起"）。force_exit 走 ✕ 同款
        # 完整退出流程（含退出恢复布局），互斥体正常释放。
        self.root.after(200, lambda: self._on_close(force_exit=True))

    def _replace_widget_colors(self, widget, old_colors: dict):
        """递归遍历控件，按属性→颜色键的显式映射替换颜色。

        ★ 不用"旧色值反查键"的方案——深色主题下 bg 和 on_primary
        都是 #282a36，反查表会丢失 bg，导致 Frame 底色被误设为白。
        这里明确声明每个属性对应哪个颜色键，语义无歧义。"""
        # 属性 → 可能的颜色键（按优先级，匹配第一个值相等的键）
        # ★R215（UI 美化阶段1）：补 R215 新 token 与 tk 侧实际用到的键——
        # 漏一个键＝该键的 tk 控件在**实时切主题**时不跟着换色（ttk 样式表
        # 会重跑，tk 控件只靠这张表）。text_secondary/on_primary/muted 是
        # 文字色（fg），border 是 tk 按钮底/分隔底（bg）。
        # 顺序按"专用键在前、通用键在后"排：每套主题里这些值都互不相等
        # （新 token 设计时已保证，撞值会让先匹配的键赢，语义就错了）。
        attr_key_map = {
            'bg':                  ('bg', 'surface', 'border'),
            'background':          ('bg', 'surface', 'border'),
            'fg':                  ('fg', 'text_secondary', 'muted',
                                    'on_primary'),
            'foreground':          ('fg', 'primary', 'secondary', 'accent',
                                    'warning', 'error', 'text_secondary',
                                    'muted', 'on_primary'),
            'insertbackground':    ('fg', 'primary'),
            'highlightbackground': ('border',),
            'highlightcolor':      ('primary',),
            'activebackground':    ('primary', 'surface'),
            'troughcolor':         ('bg', 'surface'),
            'selectbackground':    ('primary',),
            'selectforeground':    ('on_primary', 'bg'),
            'fieldbackground':     ('surface',),
        }
        for attr, keys in attr_key_map.items():
            try:
                cur = widget.cget(attr)
            except Exception:
                continue
            # ★ R46：Python 3.13+ tkinter 颜色选项 cget 返回 color object
            # （repr 形如 <color object: '#f8f8f2'>），与旧色板字符串比较永不相等
            # → 递归刷整轮空转：切换主题后控件保持旧色（浅色/护眼下白字白底）。
            # 统一转字符串并提取色值后再比对。
            cur = str(cur)
            if cur.startswith('<color object'):
                _mm = re.search(r"'([^']*)'", cur)
                cur = _mm.group(1) if _mm else ''
            if not cur:
                continue
            for key in keys:
                if old_colors.get(key) == cur:
                    new = COLORS.get(key)
                    if new and new != cur:
                        try:
                            widget.configure(**{attr: new})
                        except Exception:
                            pass
                    break
        for child in widget.winfo_children():
            self._replace_widget_colors(child, old_colors)

    def _on_explorer_tabs_toggle(self):
        """选项卡方式开关持久化"""
        from core import explorer_tabs
        state = explorer_tabs.load_state(APP_DIR / 'config')
        state['as_tabs'] = bool(self.explorer_as_tabs_var.get())
        explorer_tabs.save_state(APP_DIR / 'config', state)

    def _replay_active_slot(self):
        """立即重放当前激活的分类/全部（开关类设置马上生效）"""
        try:
            active = getattr(self.dock, '_active_slot_name', '')
            if active == '全部':
                self.dock._show_all_icons()
            elif active:
                for _idx, _s in enumerate(self.dock.slots):
                    if _s.name == active:
                        self.dock._do_activate_slot(_idx)
                        break
        except Exception as e:
            logger.error(f"重放当前分类失败: {e}")

    # ---- R142 设置页拆分：实现迁 gui/settings_panel，此处留薄委托与
    #      字号表别名（启动回填用 self._DOCK_FONT_NAMES/self._UI_FONT_SIZES
    #      引用类属性，须与 settings_panel 是同一 dict 对象）----
    _DOCK_FONT_SIZES = settings_panel.DOCK_FONT_SIZES
    _DOCK_FONT_NAMES = settings_panel.DOCK_FONT_NAMES
    _UI_FONT_SIZES = settings_panel.UI_FONT_SIZES
    _DEFAULT_UI_FONT = settings_panel.DEFAULT_UI_FONT

    def _rebuild_sticky_grid(self):
        """薄委托（R142 迁 settings_panel）：data_manage 导入/分类操作按旧名调用"""
        settings_panel.rebuild_sticky_grid(self)

    def _refresh_sticky_chip(self, name: str, kind: str):
        """薄委托：_on_toggle_cat_sticky 的勾选同步用"""
        settings_panel.refresh_sticky_chip(self, name, kind)

    def _update_close_restore_combo(self):
        """薄委托：启动回填与下拉 postcommand 用"""
        settings_panel.update_close_restore_combo(self)

    def _close_restore_target_has_content(self, name: str) -> bool:
        """★ R71：退出/启动恢复目标是否有"内容"——分类带图标，或存在同名
        布局存档。

        空分类（如清空数据后的 AI）没有图标也没有存档，把它设为退出恢复
        目标=退出把桌面清空、启动再清一遍——第一次用的人会以为图标全丢
        （2026-09-15 用户实测）。有内容才允许作为恢复目标。"""
        try:
            if self.category_manager.get_category_icons(name):
                return True
        except Exception:
            pass
        try:
            return any((APP_DIR / 'icon_backups').glob(f"{name}_*.json"))
        except Exception:
            return False

    def _activate_slot_by_name(self, slot_name: str) -> bool:
        """把桌面切到指定槽位/分类的显隐状态。

        '全部' 工具槽走 _handle_click（恢复全部图标全套逻辑），普通分类走
        _do_activate_slot。仅负责显隐切换，不负责布局摆放。找不到对应槽位
        返回 False（调用方决定兜底）。与 _reactivate_last_slot 同一套语义。
        """
        slots = list(getattr(self.dock, 'slots', []) or [])
        if slot_name == '全部':
            for idx, s in enumerate(slots):
                if s.name == '全部':
                    self.dock._handle_click(idx)
                    return True
            try:
                self.dock._show_all_icons()
                return True
            except Exception as e:
                logger.error(f"[布局] 显示全部图标失败: {e}")
                return False
        for idx, s in enumerate(slots):
            if s.name == slot_name:
                self.dock._do_activate_slot(idx)
                return True
        logger.warning(f"[布局] 未找到槽位/分类 [{slot_name}]，无法切换显隐")
        return False

    def _apply_layout_by_name(self, name: str):
        """按布局名把桌面恢复成对应存档（取最新一份 <name>_*.json）。

        供多处复用：手动套用按钮 / 常驻隐藏（方案A）/ 退出恢复 / 启动恢复，
        保证各触发路径行为一致。

        ★ 2026-09-11 修复：此前只 restore_layout 摆位置、不切显隐，导致命名
        布局（如"工作"）恢复后桌面仍是"全部"的可见集。现在先
        _activate_slot_by_name 把桌面切到该布局对应的分类/全部显隐，再
        restore_layout 摆位置。找到并恢复成功返回 restore_layout 的 dict；
        找不到存档/失败返回 None（此时显隐可能已切换成功，仅位置未套用）。
        """
        name = (name or '').strip()
        if not name or name in ('当前', '窗', '✕'):
            return None
        activated = self._activate_slot_by_name(name)
        cands = sorted((APP_DIR / 'icon_backups').glob(f"{name}_*.json"))
        if not cands:
            logger.warning(f"按名恢复布局 [{name}] 失败：icon_backups 下无 {name}_*.json"
                           f"{'' if activated else '（且未找到对应槽位）'}")
            return None
        try:
            r = self.icon_manager.restore_layout(str(cands[-1]))
            # ★ B1 顺带（N-2~N-4 观测缺口）：restore_layout 只打"开始恢复"，
            #   按钮/退出/启动三路径都无从判断成败——补一条结果日志
            logger.info(f"[布局] 按名套用 [{name}] 完成: success={r.get('success')} "
                        f"恢复={r.get('restored', 0)} 匹配={r.get('matched', 0)}")
            return r
        except Exception as e:
            logger.error(f"恢复布局 [{name}] 失败: {e}")
            return None

    def _show_main_window(self):
        """从横条"窗"按钮呼出主窗口"""
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        # ★R285-O1：藏着期间定时链不读桌面（省掉每 2 秒一次跨进程调用），
        # 唤回时立刻补一次，免得用户先看到旧数字
        try:
            self._update_stats(force=True)
        except Exception as e:
            logger.error(f"唤回主窗后刷新统计失败: {e}")

    def _on_dock_cat_switched(self):
        """dock 切换分类后：同步主窗口左侧分类选中 + 右侧图标明细 + 高亮框刷新"""
        try:
            self.highlighter.notify_changed()
        except Exception as e:
            logger.error(f"notify_changed 异常: {e}")
        # ★ R136：切换后框名单诊断落 app.log（"切到娱乐没框"排查——
        #   名单为空=该分类成员与桌面名对不上；非空=框画了但坐标漂移）
        try:
            _slot = getattr(self.dock, '_active_slot_name', '')
            _names = self.dock._compute_frame_names(_slot)
            _head = '、'.join(sorted(_names)[:6])
            logger.info(f"[框] 切到 [{_slot}] 后框名单 {len(_names)} 个"
                        f"{('：' + _head + ('…' if len(_names) > 6 else '')) if _names else '（空——不画框）'}")
        except Exception as _e:
            logger.error(f"[框] 切换后名单计算失败: {_e}")
        if not hasattr(self, 'category_list') or not hasattr(self, 'cat_detail'):
            return
        # 找到 dock 当前激活的分类名
        active_name = getattr(self.dock, '_active_slot_name', None)
        logger.info(f"_on_dock_cat_switched: active_name={active_name} kids_mode={getattr(self.dock, 'kids_mode', False)}")
        if not active_name:
            return
        # 在 category_list 里找对应行并选中
        for i in range(self.category_list.size()):
            item = self.category_list.get(i)
            name = item.rsplit(' (', 1)[0]
            if name == active_name:
                self.category_list.selection_clear(0, tk.END)
                self.category_list.selection_set(i)
                self.category_list.see(i)
                break
        self._on_main_cat_select()

    def _startup_chain_will_lay(self, slot_name: str) -> bool:
        """★R312 item3：预判"启动恢复上次分类"那条链**会不会**真的摆一次位置。

        只给 1800ms 的"启动自动套用布局"（★R241）当闸门用：链里已经摆过，
        那一趟就必须收手，否则会把一份和当前分类不相干的旧守护布局盖在刚排好的
        桌面上（真机 R029 启动日志 17:10:31 实测：刚按「全部」摆完，
        308ms 后被「排行 21-40 名」盖掉 ⇒ 用户读成"启动后还是乱，点一下全部才排好"）。

        口径照 dock 自己那两条，不另起一套选布局逻辑：
          · [全部]＝core/dock.py:2349-2363，有「全部」存档就摆；
          · 普通分类＝:1697-1701 + :1850-1905，只在"切分类套用同名布局"勾选时摆，
            同名存档优先、没有则回退「全部」；
          · 排行组不参与布局（★R279-⑦），只有它自己存过同名布局才摆（★R282-1）——
            是不是排行组问 dock，不在这里判名单。
        判不准（dock/图标管理器还没建好、任何异常）一律回 False＝照旧让 ★R241
        那趟生效：宁可多摆一次，也不要把用户勾出来的功能判死。

        ★R313 E3（真 bug）：旧预判只看"有没有同名布局存档"，从不看那个**槽位
        还在不在悬浮窗上**。分类后来被取消"顶"（in_dock=False）或被删时，
        真链 `_reactivate_last_slot` 在 slots 里找不到槽位 → 只打一句
        "不在悬浮窗中，跳过恢复"（一个位置都没摆），而 1800ms 的 ★R241 那趟
        又因为预判说"链已摆过"整趟收手 ⇒ 两边都不摆，用户勾的功能静默不生效。
        现在：dock 报告了非空槽位名单时，槽位不在名单里就判"没摆"；
        名单为空（悬浮栏还没建/旧桩）按"判不准"走原口径，不误闸。
        """
        try:
            dock = getattr(self, 'dock', None)
            im = getattr(self, 'icon_manager', None)
            if dock is None or im is None:
                return False
            slot_names = {getattr(s, 'name', None)
                          for s in (getattr(dock, 'slots', None) or [])}
            if slot_names and slot_name not in slot_names:
                return False
            if slot_name == '全部':
                return bool(im.pick_layout('全部'))
            if not getattr(dock, 'apply_cat_layout', False):
                return False
            if im.pick_layout(slot_name):
                return True
            try:
                if dock._cat_is_rank_group(slot_name):
                    return False
            except Exception:
                pass
            return bool(im.pick_layout('全部'))
        except Exception as e:
            logger.debug(f'[启动] 预判恢复链是否摆位失败（按"没摆"处理）: {e}')
            return False

    def _reactivate_last_slot(self, slot_name: str):
        """退出恢复=当前 模式：启动后重新套用上次激活的分类。

        ★ P1-3 修复：'全部' 是工具槽而非分类槽。此前与普通分类走同一
        _do_activate_slot 路径，而 _do_activate_slot 对 '全部' 调
        get_category_icons('全部') 得到空集 → 若无常驻分类则 visible 为空
        → 触发"空分类"分支 _hide_listview() → 启动后桌面图标全部消失
        （有常驻分类时则只剩常驻图标，同样不是"全部"）。

        修复：'全部' 特判走 dock._handle_click(全部槽 idx)，与用户手点
        [全部] 完全同路径（_show_all_icons / _active_slot_name /
        on_switch_done / repaint 全套）。此处不直调 _show_all_icons，
        是因为必须同步 dock._active_slot_name —— _replay_active_slot
        依赖它判断当前激活项，直调会让它停留在 None，导致后续设置
        切换时重放失效。
        """
        try:
            slots = list(getattr(self.dock, 'slots', []) or [])
            # 特判 '全部'：工具槽走 click 路径（含恢复全部图标全套逻辑）
            if slot_name == '全部':
                for idx, s in enumerate(slots):
                    if s.name == '全部':
                        self.dock._handle_click(idx)
                        self._log('📂 已恢复上次状态 [全部]')
                        return
                # 找不到 [全部] 槽（slots 重排 / 异常配置）→ 退化直调
                logger.warning("[启动] 未找到 [全部] 槽，退化直调 _show_all_icons")
                self.dock._show_all_icons()
                self._log('📂 已恢复上次状态 [全部]（退化直调）')
                return
            # 普通分类槽：原逻辑
            for idx, s in enumerate(slots):
                if s.name == slot_name:
                    self.dock._do_activate_slot(idx)
                    self._log(f"📂 已恢复上次分类 [{slot_name}]")
                    return
            logger.warning(f"[启动] 上次分类 [{slot_name}] 不在悬浮窗中，跳过恢复")
        except Exception as e:
            logger.error(f"[启动] 恢复上次分类失败: {e}")

    def _log_switch(self, slot_name: str, result: dict, applied_layout: bool):
        """记录分类切换到 switch.log（独立文件，方便事后逐条排查）"""
        self._diag_event(f'switch:{slot_name}')   # ★R232：切分类=诊断事件锚点
        try:
            # 记录最近激活的分类：退出恢复=当前 模式下，启动后据此恢复
            self._app_settings['last_active_slot'] = slot_name
            self._save_app_settings()
        except Exception:
            pass
        try:
            ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            hidden = result.get('hidden', 0) if result else 0
            shown = result.get('shown', 0) if result else 0
            # ★R314（真机 0.99-R031）：这里此前打的是 `失败={len(failed)}`，而 box 档的
            # `failed` 里装着**刚搬进盒子的那批**和系统虚拟图标 ⇒ 一次成功的切换被打成
            # "失败=62"（62 = 搬进盒子 57 + 系统图标跳过 5）。改读 dock 分解好的两份。
            stuck = len((result or {}).get('stuck') or [])
            skip = int((result or {}).get('skipped_system') or 0)
            kids = getattr(self.dock, 'kids_mode', False)
            # ★R319-K15：「全部」那一发的 `failed` 是**真失败**（dock 只在整条
            # 恢复链抛异常时往里塞话），旧写法一个都不读 ⇒ 点"全部"什么都没出来，
            # switch.log 仍记成 0/0/0/0 的"成功样"，事后翻日志看不出这次没成。
            # box 档的 `failed` 装的是"刚搬进盒子的名单"（★R314 就是被这个混用
            # 打成"失败=62"的假警报），所以只有「全部」按失败记账。
            _fl = list((result or {}).get('failed') or [])
            fail_part = ''
            if slot_name == '全部':
                fail_part = ' 失败=%d' % len(_fl)
                if _fl:
                    fail_part += ' 原因=%s' % ' '.join(
                        str(x) for x in _fl)[:120]
            # ★R323 L3（用户「那一发打真值（现在念的是旧数）」）：「全部」这一发过去
            # 只带 4 个数，收纳档永远是 `hide`、布局永远是"未套用"、散回那一步的分解
            # 整份丢掉 ⇒ 事后翻日志看不出这次到底动了什么。dock 现在把真值一路递到这，
            # 这里逐格念出来（只动「全部」那一发，别档一行字节不变）。
            _box_part = (f"  收纳: 档={result.get('mode', 'hide') if result else 'hide'}"
                         f" 搬进盒子={result.get('boxed', 0) if result else 0}"
                         f" 散回={result.get('back', 0) if result else 0}")
            _two = ''
            if slot_name == '全部':
                _g = lambda k: int((result or {}).get(k, 0) or 0)
                _box_part += (' 留在盒里=%d 搬不回=%d 已不在盒里=%d 让路改名=%d'
                              ' 没被认领=%d 动过的盒=%d'
                              % (_g('kept_in_box'), _g('blocked'), _g('gone'),
                                 _g('renamed'), _g('stray'), _g('boxes_touched')))
                _two = ('  两步: 先从盒子搬回 %d 枚，再清 %d 枚隐藏属性\n'
                        % (_g('back'), shown))
            lines = [
                f"━━━ 切换 [{slot_name}] ━━━",
                f"  时间: {ts}",
                f"  图标: 显示={shown} 隐藏={hidden} 卡住={stuck}"
                f" 系统跳过={skip}{fail_part}",
                _box_part,
                _two,
                f"  布局: {'已套用' if applied_layout else '未套用'}",
                f"  模式: kids={kids}",
                "",
            ]
            _switch_logger.info(''.join(lines))
        except Exception as e:
            logger.debug(f"切换日志写入失败(已忽略): {e}")

    def _exit_from_dock(self):
        """横条 ✕：退出整个程序。

        ★ #1 连带修复：必须强制退出，不受"关闭保留悬浮窗"影响。
        主窗口的 X 走同一个 _on_close，但那个要按 close_keep_dock 决定
        "隐藏"还是"退出"；横条 ✕ 的语义是"真正退出"（用户明确要求
        "只有点悬浮窗的 ✕ 才退出"）。此前两者无区分，close_keep_dock
        改为默认 True 后，点横条 ✕ 会退化成只隐藏主窗口、退不出去。
        """
        self.root.after(0, lambda: self._on_close(force_exit=True))

    # ----------------------- 高亮框 -----------------------

    def _start_frame_refresh_loop(self):
        """2 秒一次：应用后台监视线程的图标数据（有变化才重绘）+ 层级校准。
        所有跨进程读取都在监视线程里，UI 线程零跨进程调用（健壮性关键）。"""
        def tick():
            try:
                item = self.highlighter.poll_monitor()
                if item:
                    self.highlighter.apply_monitor_data(item)
                self.highlighter.maintain_zorder()
                for m in self.icon_manager.drain_guard_msgs():
                    self._log(m)
            except Exception as e:
                logger.error(f"图标框刷新出错(已忽略): {e}")
            try:
                self.root.after(2000, tick)   # ★ P1-E 包进 try：退出瞬间 TclError 会静默终止链
            except Exception as e:
                logger.error(f"刷新定时器调度失败: {e}")

        tick()

    def _start_show_event_poll(self):
        """每秒检查单实例唤醒事件（第二次启动 = 弹出主窗口）"""
        k32 = ctypes.windll.kernel32
        SYNCHRONIZE = 0x00100000
        self._show_evt = k32.OpenEventW(SYNCHRONIZE, False, _SINGLETON_SHOW_EVENT)

        def check():
            try:
                if self._show_evt and \
                        k32.WaitForSingleObject(self._show_evt, 0) == 0:
                    self._show_main_window()
            except Exception as e:
                logger.error(f"单实例唤醒检查异常(已忽略): {e}")
            try:
                self.root.after(1000, check)
            except Exception:
                pass

        check()

    # ----------------------- 分类选择（多选） -----------------------

    def _tab_text(self, key, zh, en=None):
        """★R225 阶段5：页签文字 = 图标字符 + 空格 + 双语词。

        词是逻辑 ID（向导 _goto 子串匹配、ui_tab 恢复、测试断言都按词找），
        图标档只换前面那个字符。统计页的比较串也从这里出，保证
        "构建"与"比较"永远同源（icons 模块头约束 2）。

        ★R304-2：顺手登记「整串 ↔ 纯文字」两种写法（`_tab_forms`），第④级降级
        只在这份登记表里来回换，不现算图标——图标档（mdl2/emoji）在构建期就定了，
        这里要是自己拼，换档未重启那一窗口期就会把页签改成屏幕上看不到的样子。
        """
        label = i18n.tr(zh, en) if en is not None else zh
        text = icons.tab_text(key, label)
        self.__dict__.setdefault('_tab_forms', {})[text] = label
        return text

    def _make_scroll_tab(self, label: str, fill_viewport: bool = False):
        """创建带纵向滚动条的页签，返回内容容器。

        使用 canvas + window item 标准模式：
        - inner 尺寸变化时更新 scrollregion
        - canvas 尺寸变化时让 inner 宽度跟随
        - 鼠标滚轮在 Enter 作用域内生效

        ★R312 item2（用户 68 批：「win11 缩放到 125%，窗口下面部分显示不全，
        有下拉单的窗口看起来没这个问题」）：多一个 `fill_viewport`——页签里是
        `fill=BOTH expand=True` 的子树（管理页的 PanedWindow、关于页的卡片）时，
        光把它塞进 canvas 会让子树从"撑满视口"退化成"只拿请求高"，窗口一矮就
        挤、一高又空。这里把 inner 的高度也钉成 `max(内容请求高, 视口高)`：
        内容比视口高 ⇒ 照常滚到底；视口比内容高 ⇒ inner 铺满，expand 子树
        拿到和以前一样的高度（M3：不许变成 1px 或 0 高）。
        """
        outer = ttk.Frame(self.notebook, padding='8 6')

        canvas = tk.Canvas(outer, highlightthickness=0,
                           bg=COLORS['bg'], bd=0)
        vs = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=canvas.yview)
        inner = ttk.Frame(canvas)
        canvas.configure(yscrollcommand=vs.set)
        vs.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        win = canvas.create_window((0, 0), window=inner, anchor=tk.NW,
                                   tags='inner')

        # ★ R155：scrollregion 高度取 max(内容, 视口)——此前用 bbox('all')，
        # 内容比视口矮时 Tk 允许把内容拖出区顶，露出上方黑底（用户实测
        # "规则页往上拉出黑的地方"）
        def _sync_scroll(_e=None):
            canvas.configure(scrollregion=(
                0, 0, max(1, canvas.winfo_width()),
                max(inner.winfo_reqheight(), canvas.winfo_height())))
            if fill_viewport:
                # ★R312 item2：把 inner 的高度也钉住（同上：取 max，两头都不亏）
                canvas.itemconfigure(
                    win, height=max(inner.winfo_reqheight(),
                                    canvas.winfo_height()))
        inner.bind('<Configure>', _sync_scroll)
        canvas.bind('<Configure>', lambda e: (canvas.itemconfigure(
            win, width=e.width), _sync_scroll()))

        def _wheel(e):
            # R123：滚轮落在自带滚动的控件上时优先滚控件（内容撑满则回落滚页面）
            try:
                w = canvas.winfo_containing(e.x_root, e.y_root)
                while w is not None and w.winfo_class() in (
                        'Treeview', 'Listbox', 'Text'):
                    try:
                        lo, hi = w.yview()
                    except Exception:
                        lo, hi = 0.0, 1.0
                    if not (lo <= 0.0 and hi >= 1.0):
                        return
                    w = getattr(w, 'master', None)
                if w is not None and w.winfo_class() == 'TCombobox':
                    return
            except Exception:
                pass
            canvas.yview_scroll(int(-e.delta / 120), 'units')
        canvas.bind('<Enter>', lambda e: canvas.bind_all('<MouseWheel>', _wheel))
        canvas.bind('<Leave>', lambda e: canvas.unbind_all('<MouseWheel>'))
        self.notebook.add(outer, text=label)
        # ★R312 item2：滚动页签返回的是**内容容器**（inner），而"这一枚页签"
        # 在 notebook 里是 outer。页签重排（R15 那条 `nb.insert('end', …)`）
        # 拿 inner 去 insert 会 TclError 且被那层的 try 吞掉 ⇒ 顺序静默失效。
        # 所以把 outer 挂在 inner 上带出去，重排处按 `_tab_page` 取。
        inner._tab_page = outer
        return inner

    def _build_ui(self):
        """构建 UI 界面（2026-09-02 重构：单页选项卡布局，一屏放下、层次分明）

        选项卡：📁 管理(分类+布局) | 🎛️ 显示规则(规则+排行榜+高亮框)
        | 🧩 悬浮设置(悬浮窗+快捷键) | 👶 家长模式 | 📜 日志 | ℹ️ 关于
        统计与运行状态常驻底部状态栏。"""
        self.root.geometry('1080x800')   # 与 minsize 对齐，避免启动瞬间尺寸跳变
        # minsize 不写死——由 _reposition_window 根据屏幕尺寸动态设置，
        # 否则 ≤1024 屏会被撑出屏幕右侧

        # ====== 顶部标题栏（紧凑单行）======
        header = ttk.Frame(self.root, padding='12 8 12 2')
        header.pack(fill=tk.X)
        # ★R303：左右两簇改 grid。原来是同排 `pack(side=LEFT)` + `pack(side=RIGHT)`
        #   抢宽——win11 125% 缩放叠加「界面字号=大」时实测「悬浮窗」按钮被压到
        #   1px 宽（用户截图里右上角那个空框就是它）。grid 把"放不下就把右簇挪到
        #   第二行"变成一行代码的事，100% 标准档下两簇照旧同一行、视觉不变。
        hdr_left = ttk.Frame(header)
        hdr_left.grid(row=0, column=0, sticky='w')
        header.columnconfigure(1, weight=1)
        # ★R229（item5）：标题栏这四处图标字符改走 icons 层——"图标风格"设置
        # 从此真的管到标题栏（MDL2 档＝单色系统图标，Emoji 档＝原彩色表情）。
        # 词（锁屏/悬浮窗/语言…）仍是 i18n 双语字面量，一个字没动。
        ttk.Label(hdr_left, text=self._tab_text('app', '拾碎-懒人桌面工具箱', 'DeskBits'),
                  font=ui_layout.app_font(16, 'bold'),
                  foreground=COLORS['primary']).pack(side=tk.LEFT)
        # ★R308：这里原来还有一行装饰 `分类 · 布局 · 悬浮条`（133px + 12px 间距）。
        # 用户 64 批点名把它挪进窗口标题（"v0.99-R024 后面"），头部行就此腾出 145px
        # 给「收纳: 属性隐藏」——见 `self.root.title`，两边只能有一处，别加回来。
        # ====== ★R305（用户点名）：收纳档开关放主窗口顶部，不放悬浮条 ======
        self._build_pack_mode_switch(hdr_left)
        # ====== ★R218 阶段 3：标题栏右侧控件簇 ======
        # 原来 6 个控件各自 pack(side=RIGHT)，间距忽 4 忽 10、悬浮窗是 ghost 而
        # 锁屏是 secondary＝同排不同档看着乱。收进一个右对齐的 cluster frame，
        # 内部统一 side=LEFT + 固定 padx；下拉（主题/语言）是一档、动作钮
        # （锁屏/悬浮窗）统一 secondary 一档，中间一道分隔线把两类分开。
        ctrl = ttk.Frame(header)
        ctrl.grid(row=0, column=1, sticky='e')
        self._hdr_left, self._hdr_ctrl = hdr_left, ctrl
        self._hdr_wrapped = False
        self.ui_lang_var = tk.StringVar(
            value=self._app_settings.get('ui_language', '中文'))
        _lang_combo = ttk.Combobox(ctrl, textvariable=self.ui_lang_var,
                                   width=9, state='readonly',
                                   values=['中文', 'English'])
        ttk.Label(ctrl, text=self._tab_text('lang', '语言/Language:')).pack(
            side=tk.LEFT, padx=(0, 4))
        _lang_combo.pack(side=tk.LEFT)
        _lang_combo.bind('<<ComboboxSelected>>', self._on_ui_language_change)
        # 界面主题（2026-09-05 用户反馈：原来埋在悬浮框设置里找不到）
        self.ui_theme_var = tk.StringVar(
            value=_theme_disp(self._app_settings.get('ui_theme', '深色')))
        _theme_combo = ttk.Combobox(ctrl, textvariable=self.ui_theme_var,
                                    width=9, state='readonly',
                                    values=[_theme_disp(k) for k in
                                            ('深色', '暗黑', '浅色', '护眼')])
        ttk.Label(ctrl, text=i18n.tr('主题:', 'Theme:')).pack(side=tk.LEFT, padx=(12, 4))
        _theme_combo.pack(side=tk.LEFT)
        _theme_combo.bind('<<ComboboxSelected>>', self._on_ui_theme_change)
        # 分隔线：下拉（选择）↔ 动作钮（执行）
        ttk.Separator(ctrl, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=12)
        # ★ R159：锁屏入口。★R215：不写 width＝按内容自然撑开，中英都不裁字。
        ttk.Button(ctrl, text=self._tab_text('lock', '锁屏', 'Lock'),
                   style='secondary.TButton',
                   command=lambda: sys_panel.lock_now(self)).pack(side=tk.LEFT,
                                                                  padx=(0, 8))
        ttk.Button(ctrl, text=self._tab_text('dock', '悬浮窗', 'Floating Bar'),
                   style='secondary.TButton',
                   command=self._toggle_dock).pack(side=tk.LEFT)

        # ====== 底部状态栏（常显：统计 + 运行状态）======
        # ★R298-B（美化阶段 2-4 挂账收口）：整段从 run-on pack 换成
        #   grid 三列（左统计 / 1px 分隔线 / 右状态），两侧列宽用 uniform
        #   保证分隔线在窗口拉宽时不会贴边；数字那一档从 YaHei UI（非
        #   tabular）换 Consolas——9→10、199→200 时不再抖宽度（用户 21 批
        #   "总图标 从 8 变 9 时后面的整簇跟着横移"同族）。
        #   注：`top_var` 显示"分类名 (计数)"，可能含中文 ⇒ 那一档保持
        #   app_font（Consolas 中文会掉字形），只三个纯数字档走等宽。
        status_frame = ttk.Frame(self.root, padding='10 2 12 8')
        status_frame.pack(side=tk.BOTTOM, fill=tk.X)
        status_frame.columnconfigure(0, weight=1, uniform='sbcol')
        status_frame.columnconfigure(1, weight=0)
        status_frame.columnconfigure(2, weight=1, uniform='sbcol')

        stats_box = ttk.Frame(status_frame)
        stats_box.grid(row=0, column=0, sticky='w')
        ttk.Separator(status_frame, orient='vertical').grid(
            row=0, column=1, sticky='ns', padx=(0, ui_layout.SP['m']))
        right_box = ttk.Frame(status_frame)
        right_box.grid(row=0, column=2, sticky='e')

        self.total_var = tk.StringVar(value='0')
        self.clicked_var = tk.StringVar(value='0')
        self.visible_var = tk.StringVar(value='0')
        self.top_var = tk.StringVar(value=i18n.tr('无数据', 'No data'))

        _num_font = ('Consolas', 9, 'bold')

        def _stat(prefix, var, color, numeric=False):
            ttk.Label(stats_box, text=prefix, foreground=COLORS['text_secondary'],
                      font=ui_layout.app_font(9)).pack(side=tk.LEFT, padx=(0, 2))
            ttk.Label(stats_box, textvariable=var, foreground=color,
                      font=_num_font if numeric else ui_layout.app_font(9, 'bold')
                      ).pack(side=tk.LEFT, padx=(0, 14))
        _stat(i18n.tr('总图标', 'Icons'), self.total_var, COLORS['primary'], True)
        _stat(i18n.tr('已点击', 'Clicked'), self.clicked_var, COLORS['secondary'], True)
        _stat(i18n.tr('可见', 'Visible'), self.visible_var, COLORS['warning'], True)
        _stat(i18n.tr('热门', 'Top'), self.top_var, COLORS['accent'])

        self.active_var = tk.StringVar(value='')
        ttk.Label(right_box, textvariable=self.active_var,
                  font=ui_layout.app_font(9),
                  foreground=COLORS['accent']).pack(side=tk.RIGHT)

        self.status_var = tk.StringVar(value=i18n.tr('就绪', 'Ready'))
        ttk.Label(right_box, textvariable=self.status_var,
                  font=ui_layout.app_font(9),
                  foreground=COLORS['text_secondary']).pack(side=tk.RIGHT, padx=(0, 14))

        # ====== 主体：选项卡 ======
        self.notebook = nb = ttk.Notebook(self.root)
        nb.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 2))
        try:
            nb.enable_traversal()
        except Exception:
            pass

        # ★R215（UI 美化阶段1）：页签文字去掉手工前后空格——宽度现在由
        # TNotebook.Tab 的 padding=(14,8) 统一给（R215 样式表），手工空格
        # 在中英切换时还会多撑出一截。（读页签的代码都走 .strip()/子串匹配，
        # 去空格不影响 main_window:1825/1859 与 wizard:195 的定位逻辑）
        # ★R312 item2（用户 68 批）：125% 缩放下实测这页**请求 859 高 / 视口 599**
        #   ⇒ 底下 260px 在窗口外（"窗口下面的部分显示不全"）。改走滚动页签，
        #   fill_viewport 让页内 PanedWindow 照旧撑满视口（量具见
        #   测试_ui/test_r312_scroll_tabs.py）。
        tab_manage = self._make_scroll_tab(
            self._tab_text('manage', '管理', 'Files'), fill_viewport=True)
        tab_rules = self._make_scroll_tab(self._tab_text('rules', '规则', 'Rules'))
        tab_dockset = self._make_scroll_tab(
            self._tab_text('settings', '设置', 'Settings'))
        # R123：资源页改滚动页签——下载卡底部按钮在默认窗口高度下被挡
        tab_explorer = self._make_scroll_tab(
            self._tab_text('resources', '资源', 'Resources'))
        tab_kids = ttk.Frame(nb, padding='8 6')
        nb.add(tab_kids, text=self._tab_text('parental', '家长', 'Parental'))
        # ★ R163：内容超出窗口高度，改滚动页签（用户 -15/2）
        # ★ R174：用户 -20/2"系统相关选项卡改成系统"
        tab_sys = self._make_scroll_tab(self._tab_text('system', '系统', 'System'))
        tab_log = ttk.Frame(nb, padding='8 6')
        nb.add(tab_log, text=self._tab_text('logs', '日志', 'Logs'))
        # ★ R73：关于标签永久双语（两种语言的用户都能找到）——双语字面
        #   '关于/About' 原样保留，图标字符仍走 _tab_text（★R225）。
        # ★R312 item2：125% 下实测**请求 848 / 视口 599** ⇒ 底下那截（更新记录
        #   末尾、外链按钮行）在窗口外。同管理页：滚动页签 + fill_viewport
        #   （关于卡片是 `fill=BOTH expand=True`，不撑就退化成请求高）。
        tab_about = self._make_scroll_tab(
            self._tab_text('about', '关于/About'), fill_viewport=True)

        nb.bind('<<NotebookTabChanged>>', self._on_tab_changed)

        # 📁 管理页 UI（R144 拆至 gui/manage_panel，Mixin._build_manage_tab）
        self._build_manage_tab(tab_manage)
        # 🎛️ 规则页 UI（R145 拆至 gui/rule_panel，RuleMixin._build_rules_tab）
        self._build_rules_tab(tab_rules)
        # ============ 🧩 悬浮设置：悬浮窗 + 公共分类显示（R142 拆至 gui/settings_panel）============
        settings_panel.build_cards(tab_dockset, self, COLORS)

        # ---- 全局快捷键 ----
        # ★ R140：面板拆至 gui/hotkey_panel.py（名单/改键/恢复默认/冲突提醒）
        hotkey_panel.build_card(tab_dockset, self, COLORS)

        # ============ 📂 资源管理：文件夹组（一排组条 + 打开 + 管理在下方）============
        exp_frame = ttk.LabelFrame(tab_explorer, text=i18n.tr('📂 文件夹组', '📂 Folder Groups'),
                                   padding='10', style='Card.TLabelframe')
        exp_frame.pack(fill=tk.X, pady=(0, 6))
        # ★R259（用户 51 批 二1a）：「🚀 启动自动打开」紧跟「📂 文件夹组」，
        # 不再被下载记录卡隔开——它管的就是文件夹组，同主题必须挨在一起。
        # ★R258（用户 50 批 二1）：启动软件后延时若干秒自动打开勾选的文件夹组
        # （旧"开机自动打开"死开关的正式替代，**默认关**；当年被用户撤过的行为
        # 不自动复活，开不开由用户在这里重新点）。勾选顺序＝打开顺序；
        # 🏆 最常用组自动取"最近使用排名第 1"；"组内只开前 K 个"按组内列表序截前 K。
        try:
            self._build_startup_open_card(tab_explorer)
        except Exception as e:
            logger.error(f'启动自动打开卡构建失败(不影响其余界面): {e}',
                         exc_info=True)
            self._log(f'⚠ 启动自动打开卡加载失败，资源页该块已跳过：{e}')

        # ★R301（批57）：📦 桌面盒子卡——排在自动打开设置之后、下载记录之前
        # （下载卡 fill=BOTH expand=True，排在它后面会被挤到底部看不见）
        try:
            desktop_box_panel.build_box_card(tab_explorer, self, COLORS)
        except Exception as e:
            logger.error(f'桌面盒子卡构建失败(不影响其余界面): {e}', exc_info=True)
            self._log(f'⚠ 桌面盒子卡加载失败，资源页该块已跳过：{e}')
            try:
                ttk.Label(tab_explorer, text=(
                    i18n.tr('⚠ 桌面盒子卡加载失败，其余功能不受影响。',
                            '⚠ Desktop box card failed to load; '
                            'other functions are unaffected.') +
                    f'\n{e}'),
                    foreground=COLORS['text_secondary']).pack(
                    padx=20, pady=10, anchor=tk.W)
            except Exception:
                pass

        # ★ R112：下载记录卡（设计稿入口=资源页，与文件夹组同主题）
        try:
            download_panel.build_download_card(tab_explorer, self, COLORS)
        except Exception as e:
            logger.error(f"下载记录卡构建失败(不影响其余界面): {e}")
            # ★R221（NEW7-P1-2）：降级要"看得见"。原先异常只进 app.log，
            # 资源页就那么空一块——用户以为功能本来就没有（本轮"功能死了但
            # 界面毫无反应"口径的同族）。日志页 + 页内占位各留一条。
            self._log(f'⚠ 下载记录卡加载失败，资源页该块已跳过：{e}')
            try:
                ttk.Label(tab_explorer, text=(
                    i18n.tr('⚠ 下载记录卡加载失败，其余功能不受影响。',
                            '⚠ Download history failed to load; '
                            'other functions are unaffected.') +
                    f'\n{e}'),
                    foreground=COLORS['text_secondary']).pack(
                    padx=20, pady=10, anchor=tk.W)
            except Exception:
                pass

        # 第 1 行：组条（一排）+ 打开按钮在最右
        # 单击组条 = 选中为当前组（悬浮条"一键打开"即打开它）；双击组条 = 直接打开
        strip_row = ttk.Frame(exp_frame)
        strip_row.pack(fill=tk.X, pady=(0, 4))
        # ★ R72：选项卡/多窗口说明按钮（用户要求：别用弹窗打扰，想看再看）
        ttk.Button(strip_row, text=i18n.tr('📖 说明', '📖 Info'),
                   command=self._show_explorer_tabs_info).pack(side=tk.LEFT,
                                                               padx=(0, 8))
        self.explorer_chips_frame = ttk.Frame(strip_row)
        self.explorer_chips_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._open_group_btn = ttk.Button(strip_row, text=i18n.tr('📂 打开', '📂 Open'),
                                          command=self._on_open_explorer_folders,
                                          style='primary.TButton')
        self._open_group_btn.pack(side=tk.RIGHT, padx=(8, 0))

        # 打开状态行（2026-09-05 用户要求：打开文件夹需要时间，要有文字提醒）
        self.explorer_open_status_var = tk.StringVar(value='')
        ttk.Label(strip_row, textvariable=self.explorer_open_status_var,
                  foreground=COLORS['warning'],
                  font=ui_layout.app_font(9)).pack(side=tk.RIGHT, padx=(0, 8))

        # 第 2 行：管理按钮（其他按钮都在这行）
        # ---- 组级 + 条目级操作：合成一条 action_bar ----
        # ★R216 阶段 2b：原先"组级一行 / 条目级一行"各自 pack(LEFT, padx) ⇒
        # 六枚按钮三种宽度、上下行竖边全错开。同一容器同一条 bar 走 grid，
        # 每列宽度取该列最宽按钮 ⇒ 上下两行左右边缘对齐。
        # ★R262 批14（用户 14 批 3）：+「添加到当前组」（追加当前窗口 / 粘贴
        # 地址，都不清空原成员）共 8 枚、cols=4 正好 2 行（用户点名"刚好 8 个
        # 凑成 2 行"）。"存为组"是整组覆盖，往已有组顺带加点不必再重存整组。
        mgmt_row = ttk.Frame(exp_frame)
        mgmt_row.pack(fill=tk.X, pady=(0, 4))
        ui_layout.action_bar(mgmt_row, [
            (i18n.tr('➕ 新建组', '➕ New Group'),
             self._on_create_explorer_group, 'primary.TButton'),
            (i18n.tr('💾 存为组（当前窗口）', '💾 Save Group (current windows)'),
             self._on_save_explorer_group, 'secondary.TButton'),
            (i18n.tr('🗑️ 删除当前组', '🗑️ Delete Current Group'),
             self._on_delete_explorer_group, 'secondary.TButton'),
            (i18n.tr('➕ 添加到当前组', '➕ Add To Current Group'),
             self._on_add_to_group_dialog, 'secondary.TButton'),
            (i18n.tr('➕ 添加文件夹', '➕ Add Folder'),
             self._on_add_explorer_folder, 'secondary.TButton'),
            (i18n.tr('⬆️ 上移', '⬆️ Up'),
             lambda: self._on_move_explorer_folder(-1), 'secondary.TButton'),
            (i18n.tr('⬇️ 下移', '⬇️ Down'),
             lambda: self._on_move_explorer_folder(1), 'secondary.TButton'),
            (i18n.tr('🗑️ 删除选中', '🗑️ Delete Selected'),
             self._on_delete_explorer_folder, 'secondary.TButton')],
            cols=4, pad=SP['s'])

        # 每组选项行（2026-09-05 用户要求：分类里只留这一个开关；
        # "开机自动打开"已按用户要求移除）
        auto_row = ttk.Frame(exp_frame)
        auto_row.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(auto_row, text=i18n.tr('🔗 悬浮框自动打开同名文件夹:', '🔗 Auto-open same-name folder:')).pack(side=tk.LEFT)
        self.explorer_auto_group_name_var = tk.StringVar(value=i18n.tr('（未选中组）', '(no group selected)'))
        ttk.Label(auto_row, textvariable=self.explorer_auto_group_name_var,
                  foreground=COLORS['primary'],
                  font=ui_layout.app_font(9, 'bold')).pack(side=tk.LEFT, padx=(6, 8))
        # ★R232（用户 06 批 item2）：①初始值改 False（原来建窗时先闪一下"已勾"）；
        # ②挂 Accent.TCheckbutton——前景走主题 token check_accent + 加粗，
        #   换肤时 _setup_styles 重跑自动跟色，不写死 hex（用户："颜色上做点文章，
        #   注意适配主题颜色，以免看不清"）。勾选态由样式层的原生指示器表达
        #   （★R315 色块档：勾选=整块 primary、笔画与块同色 ⇒ 看不见叉；
        #   ★R222 那层 ☑/☐ 文字前缀已随本批退役，所以不再需要补挂）。
        self.explorer_auto_group_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(auto_row,
                        text=i18n.tr('开启（点该分类时自动打开本组）', 'On (auto-open when switching to the category)'),
                        variable=self.explorer_auto_group_var,
                        style='Accent.TCheckbutton',
                        command=self._on_explorer_group_auto_open_toggle
                        ).pack(side=tk.LEFT)

        # 第 3 行：全局开关——以选项卡方式打开
        self.explorer_as_tabs_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(exp_frame, text=i18n.tr('以选项卡方式打开（需 Win11 22H2+；不支持时自动改多窗口并提示）', 'Open as tabs (needs Win11 22H2+; falls back to multiple windows)'),
                        variable=self.explorer_as_tabs_var,
                        command=self._on_explorer_tabs_toggle).pack(anchor=tk.W, pady=(0, 4))

        # 第 4 行：当前组的文件夹列表（双击打开单个）
        # ★R216 阶段 2b：列表单独包一行容器——原先 Listbox pack(LEFT) 后紧跟
        # pack(TOP) 的说明标签，落进了 LEFT 剩下的右半区（说明浮在列表右边、
        # 列表被挤窄），现在列表铺满整宽、说明回到列表下方
        # ★R262 批14（用户 14 批 item2）：默认只露 3 行，超过 3 项出
        # "显示全部 ▾/收起 ▴"切换（榜单列同款交互）；展开也封顶 6 行防撑爆
        _exp_list_row = ttk.Frame(exp_frame)
        _exp_list_row.pack(fill=tk.X)
        self.explorer_list = tk.Listbox(_exp_list_row, height=3,
                                        font=self.ui_font,
                                        bg=COLORS['surface'],
                                        fg=COLORS['fg'],
                                        selectmode=tk.EXTENDED,
                                        relief=tk.FLAT,
                                        highlightthickness=0)
        exp_scroll = ttk.Scrollbar(_exp_list_row, orient=tk.VERTICAL,
                                   command=self.explorer_list.yview)
        self.explorer_list.configure(yscrollcommand=exp_scroll.set)
        self.explorer_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        exp_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.explorer_list.bind('<Double-Button-1>', self._on_explorer_list_double_click)
        self._exp_list_collapsed = True   # 折叠态跨组刷新保持（会话级，不持久化）
        self.explorer_list_toggle = ttk.Label(
            exp_frame, text='', cursor='hand2',
            foreground=COLORS['primary'], font=ui_layout.app_font(9, 'bold'))
        self.explorer_list_toggle.pack(anchor=tk.E, pady=(2, 0))
        self.explorer_list_toggle.bind(
            '<Button-1>', lambda e: self._toggle_explorer_list())

        # ★R222 U1：这段说明此前是裸中文，English 模式下资源页只有它是中文
        ttk.Label(exp_frame,
                  text=i18n.tr(
                      '单击组条选中 · 双击组条直接打开 · 悬浮条"一键打开"打开当前选中组\n'
                      '⏳ 打开整组文件夹需要几秒钟（逐个合并成选项卡），'
                      '期间请勿点击桌面或其他窗口，全部打开后即可正常操作',
                      'Single-click a group to select · double-click to open it · '
                      'use the dock\'s "Open Folder" for the selected group\n'
                      '⏳ Opening a whole group takes a few seconds (tabs are merged '
                      'one by one). Do not click the desktop or other windows until '
                      'it finishes.'),
                  foreground=COLORS['text_secondary'],
                  font=ui_layout.app_font(9)).pack(anchor=tk.W, pady=(4, 0))

        self._refresh_explorer_list()

        # 👶 家长页 UI（R147 拆至 gui/kids_panel，KidsMixin._build_kids_tab）
        self._build_kids_tab(tab_kids)
        # 🧰 系统相关（R159：锁屏已上线；事件查看器/计划任务排期中）
        # ★R204 GUIA-7：调用点包 try（照 download_panel 降级模式）——
        #   系统页任一环节抛异常不再炸整个 __init__（双击打不开事故族）
        try:
            sys_panel.build_sys_tab(tab_sys, self, COLORS)
        except Exception as e:
            logger.error(f"系统相关页构建失败（已降级为提示页）: {e}", exc_info=True)
            # ★R221（NEW7-P1-2）：降级同样要打到日志页（原来只有 app.log，
            # 用户看到的是"系统页一片空白"）
            try:
                self._log(f'⚠ 系统相关页加载失败，已降级为提示页：{e}')
            except Exception:
                pass
            try:
                ttk.Label(tab_sys, text=(
                    '⚠ 系统相关页加载失败，其余功能不受影响。\n'
                    f'详情见日志: {e}'),
                    foreground=COLORS['text_secondary']).pack(padx=20, pady=20)
            except Exception:
                pass
        build_log_tab(tab_log, self, COLORS)
        # ============ 📦 数据管理 ============
        # ★ 用 add() 追加到末尾，不 insert 到中间——避免 tab 恢复逻辑
        #   （在 insert 之前执行）匹配不到"数据管理"标签，导致记忆失效
        # ★R312 item2：这页 125% 下**请求 680 / 视口 599** ⇒ 底下 81px 看不见
        #   （用户没点名，是同一把尺量出来的；页内没有 expand 子树 ⇒ 不用
        #   fill_viewport，内容矮于视口时与旧版逐像素一致）
        tab_dm = self._make_scroll_tab(self._tab_text('data', '数据管理', 'Data'))
        build_data_manage_tab(tab_dm, self, COLORS)
        # ============ 📊 统计（★ S 段/B18）============
        # ★ R31：开关状态在 stats_capture.StatsCaptureManager（默认值=源码
        # 运行开 / PyInstaller 打包关），这里只接线
        self.stats_panel = build_stats_tab(
            nb, self.usage_stats, self.click_tracker, COLORS, self.root,
            self._log, on_toggle=self._on_toggle_stats_capture,
            is_on=lambda: self.stats_capture.enabled,
            settings=self._app_settings,
            save_settings=self._save_app_settings,
            # ★R281-7（用户 08 批 item7）：分类榜的"现在还有哪些分类"真相源
            category_names=self._stats_live_category_names,
            # ★R304-2：标题也走 `_tab_text`，这枚页签才会进那张「整串→纯文字」
            # 登记表——第④级降级才摘得掉它的图标前缀（旧写法绕过登记，窄窗口下
            # 满排页签里就它一枚顶着图标，红树 T4c 抓到的正是这一枚）
            tab_text=self._tab_text)
        # ★ R15：页签顺序（用户定案）：……统计 → 数据管理 → 关于
        # ★ R20 修复：ttk.Notebook 没有 move 方法（旧代码 nb.move 抛
        #   'Notebook' object has no attribute 'move' 且被 try 吞掉，顺序从未
        #   生效）——insert 已存在的子组件即为"移动"，这是 ttk 的正确 API
        try:
            # ★R312 item2：这两枚现在是滚动页签，手上那个是**内容容器**，
            #   要 insert 的是它外面那一层页（`_tab_page`）——直接拿 inner 会
            #   TclError 并被这个 try 吞掉 ⇒ R15 那套顺序静默失效。
            nb.insert('end', getattr(tab_dm, '_tab_page', tab_dm))
            nb.insert('end', getattr(tab_about, '_tab_page', tab_about))
        except Exception as e:
            logger.error(f"调整页签顺序失败(已忽略): {e}")

        # ============ ℹ️ 关于 ============
        # ★ R33：关于页拆分到 gui/about_page.py（"新功能分文件"规范的首个
        # 拆分试点，用户拍板）——纯展示+外链按钮，main_window 只留一行接线。
        # ★ R34：传 get_color 回调实时取色——修复浅色/护眼主题下关于页
        # 文字几乎看不见（此前构建时取值被定死）
        build_about_tab(
            tab_about, lambda k, d=None: COLORS.get(k, d), APP_DIR, self._log,
            on_help=lambda: show_help_window(
                self.root, lambda k, d=None: COLORS.get(k, d), APP_DIR))

        # ★ 所有 tab 都建完（8 个）后再恢复上次选中的 tab
        #   不能放在 7 个 tab 之后——数据管理页是最后 add 的，放早了匹配不到
        try:
            # ★ __init__ 收敛后不再重读盘，复用已加载的 _app_settings
            # ★ R73 捉虫：ui_tab 改存**索引**——旧版按标签文字存，界面切
            # English 后文字变了，"记住上次标签页"就静默失效
            _saved_tab = self._app_settings.get('ui_tab', 0)
            if isinstance(_saved_tab, str) and _saved_tab:
                # 旧版文字记忆的兼容回退（英文界面下匹配不到就走默认）
                for _i in range(nb.index('end')):
                    if nb.tab(_i, 'text').strip() == _saved_tab:
                        nb.select(_i)
                        break
            elif isinstance(_saved_tab, int):
                try:
                    nb.select(_saved_tab)
                except Exception:
                    pass
        except Exception:
            pass

        # ★R303：窗口宽一变就重排顶部两排（150ms 去抖：拖窗口时别一档一档抖）
        try:
            self.root.bind('<Configure>', self._on_root_configure, add='+')
        except Exception as e:
            logger.warning(f'顶部自适应绑定失败(已忽略): {e}')

    def _refresh_ui(self):
        """刷新 UI"""
        self._update_lists()
        self._update_stats()

    def _notify_highlighter_changed(self):
        """★ B5（R24）：守护恢复通知（icon_manager 监视线程触发）→
        经 _ui_queue 转 UI 线程执行 notify_changed——其内部的
        root.after 需在 Tk 线程调用（P1-3 线程约定）。"""
        self._ui_queue.put(self.highlighter.notify_changed)

    # ---------------- ★R242 #140 → ★R245：自动清排序（常开） ----------------
    # 三条硬规矩（判据在 测试_ui/test_r245_always_on.py）：
    #   ① 检测**常开**（用户 37 批 item-二-2："那个复选项不用要了，常开就行了"
    #      ——这是对 ★R242 红线①"默认关"的用户改判）；但常开只等于"会问一句"，
    #      绝不等于自动动手：下面 ②③ 一条没松。
    #   ② 只有"视图内存里确实有活动排序"才提，读不到就当不知道；
    #   ③ 必须用户在确认框点"是"才发鼠标，且每会话主动提 ≤1 次 + 冷却。

    def _on_guard_restored(self):
        """守护刚把图标拉回原位（监视线程触发）＝桌面有活动排序的最强信号。

        这里**只往 UI 队列投一个回调**：监视线程里弹窗会直接崩，动鼠标更不行。
        判定、确认框、拖动全在 `_maybe_offer_sort_clear`（UI 线程）里做。
        """
        self._notify_highlighter_changed()
        try:
            self._ui_queue.put(lambda: self._maybe_offer_sort_clear('guard'))
        except Exception as e:
            logger.debug("[sort_clear] enqueue skipped: %s", e)

    def _sort_clear_ask(self, text):
        """确认框：拿不到窗口/任何异常 ⇒ False（默认永远是"不动手"）。"""
        try:
            return bool(messagebox.askyesno(
                i18n.tr('帮你修一下？', 'Fix it for you?'), text,
                parent=self.root))
        except Exception as e:
            logger.debug("[sort_clear] ask failed: %s", e)
            return False

    def _sort_clear_log(self, *a):
        try:
            self._log(' '.join(str(x) for x in a))
        except Exception:
            pass

    def _sort_clear_notify(self, res, trigger='guard'):
        """把结果说人话并弹一次（用户 35 批 item3："成功提示成功，失败提示失败"）。

        ★R243 的直接动因：用户修完再点按钮"发现点不了了"——其实按钮是好的，
        只是第二次桌面已无活动排序，程序判断"不用修"就只写日志不响，观感等于坏。
        所以口径定死：
          · **手动按钮**＝三种结果（修好了 / 没修好 / 不用修）一律弹；
          · **守护自动触发**＝只有真拖过才弹，没得修或用户点"否"就安静
            （不然每次拉回图标都弹窗，等于新噪音）。
        ★R245 A7：成功还分两种说法——真按基准布局一次性摆回了（relaid）才说
        "已按布局摆回"；没拿到基准布局就老实说"被拖的那个图标已放回原位"。
        自检态（SELF_CHECK）绝不弹窗，否则 `自动测试.py` 会卡在一个模态框里。
        """
        if SELF_CHECK or not res:
            return
        if res.get('user_said_no'):
            return                     # 用户自己点的"否"，不必再教育一遍
        # ★R246 #198：免鼠标那条一次鼠标都没发，但桌面**真被修过了** ⇒ 和"拖过"
        # 同等对待：守护触发时也要当面说一声，不然用户只看到图标自己动了。
        attempted = bool(res.get('attempts')) or res.get('via') == 'view-api'
        if trigger != 'manual' and not attempted:
            return
        reason = res.get('reason') or ''
        ok = bool(res.get('ok'))
        st = res.get('state') or {}
        # ★R249 一3a（用户：「弹出的对话框里面，没必要出现排序=一串英文」）：
        #   这一行原来写的是 `排序＝prop:system.itemnamedisplay`——那是 COM
        #   `SortColumns` 的原样读数，对人没有意义。列名一律过
        #   `sort_clear.sort_column_words`，认不出来就老实承认"没说是按哪一列"；
        #   原始串仍然每轮写在日志页那行 `sort=…` 里（一个字没改，排查用）。
        try:
            from core.sort_clear import sort_column_words as _scw
            _cw, _ce = _scw(st.get('sort_column')) or ('', '')
        except Exception:
            _cw, _ce = '', ''
        if _cw:
            _sz, _se = u'现在按「%s」排着' % _cw, 'is sorted by \u201c%s\u201d' % _ce
        elif st.get('sort_column'):
            _sz = u'被系统自己排着（它没说是按哪一列）'
            _se = ('is sorted by the system itself (it wouldn\'t say '
                   'which column)')
        else:
            _sz, _se = u'没有活动排序', 'has no active sort'
        _az, _ae = (u'开着', 'on') if st.get('autoarrange') \
            else (u'已关', 'off')
        read_line = i18n.tr(
            '程序读到的桌面：%s；自动排列图标%s。' % (_sz, _az),
            'What the app read: the desktop %s; auto-arrange is %s.' % (_se, _ae))
        adv = res.get('advisory') or ''
        if ok:
            title = i18n.tr('修好了', 'Fixed')
            if res.get('via') == 'view-api' and not res.get('relaid'):
                # ★R246 #198：免鼠标那条一个图标都没拖过 ⇒ 下面那句"被拖的那个
                # 已放回原位"就成了假话，单独走这一支老实说"没帮你搬动图标"。
                msg = i18n.tr(
                    '桌面的活动排序已经清掉（这次没占用鼠标：程序直接叫系统把'
                    '排序撤掉了）。\n图标停在它们现在的位置，没有帮你搬动。',
                    'The active desktop sort is cleared (no mouse was used: the '
                    'app asked the system to drop the sort directly).\nIcons '
                    'stay where they are — nothing was moved for you.')
            elif res.get('relaid'):
                # ★R245 A7：桌面已按基准布局整体摆回（用户："也不用拖回去了"）
                msg = i18n.tr(
                    '桌面的活动排序已经清掉，整个桌面已按你存好的布局'
                    '一次性摆回原位。\n现在再刷新桌面，图标应该不会再乱跑了。',
                    'The active desktop sort is cleared and the whole desktop '
                    'has been laid back out from your saved layout in one go.\n'
                    'Refreshing the desktop should no longer scramble the '
                    'icons.')
            else:
                # 没拿到基准布局 ⇒ 只敢说"被拖的那个放回原位"（不许吹整个桌面）
                msg = i18n.tr(
                    '桌面的活动排序已经清掉，被拖的那个图标已经放回原位。\n'
                    '现在再刷新桌面，图标应该不会再乱跑了。',
                    'The active desktop sort is cleared and the icon that was '
                    'dragged is back in its place.\nRefreshing the desktop '
                    'should no longer scramble the icons.')
            show = messagebox.showinfo
        elif reason == 'no-active-sort':
            title = i18n.tr('不用修', 'Nothing to fix')
            msg = i18n.tr(
                '读了一遍桌面，现在没有「排序方式＝某某」这种活动排序，'
                '所以没什么要修的。\n如果图标还在乱跑，多半是「自动排列图标」或'
                '布局存档本身的问题，日志页能看到细节。',
                'The desktop has no active "Sort by" right now, so there is '
                'nothing to fix.\nIf icons still move, the cause is '
                'auto-arrange or the layout file itself; see the log page.')
            show = messagebox.showinfo
        elif reason == 'no-target':
            title = i18n.tr('没修好', 'Not fixed')
            msg = i18n.tr(
                '没能找到可以拖的图标或空位置（桌面刚刷新完时偶尔会这样）。\n'
                '过一两秒再点一次就行。',
                'No draggable icon or empty spot was found (this can happen '
                'right after the desktop refreshes).\nTry again in a second.')
            show = messagebox.showwarning
        elif reason == 'state-unknown':
            title = i18n.tr('读不到桌面状态', 'Cannot read the desktop')
            msg = i18n.tr(
                '这一次没能读到桌面的排序状态（桌面正在刷新时偶尔会这样），'
                '所以什么都没动。\n过一两秒再点一次就行；要是每次都读不到，'
                '麻烦把日志页那几行「桌面视图检查」发我。',
                'The desktop sort state could not be read this time (this '
                'happens while the desktop is refreshing), so nothing was '
                'touched.\nTry again in a second; if it keeps failing, please '
                'send me the "desk view check" lines from the log page.')
            show = messagebox.showwarning
        elif reason.startswith('raised:') or reason.startswith('没修好'):
            title = i18n.tr('没修好', 'Not fixed')
            # ★R249 一3a：异常类名是**取证要用的**（用户截图发回来一眼认得出），
            #   但整串 `raised:WindowsError` 怼在正文里读起来像坏了半句话，
            #   所以只留名字、并标清它是"错误代码"。
            _code = (reason.split(':')[-1] or '?')
            msg = i18n.tr(
                '中途出了一下错，已经停手了（错误代码：%s）。\n被拖过的图标会放回原位，'
                '过一两秒再点一次试试。' % _code,
                'Something went wrong halfway and it stopped (error code: %s).'
                '\nAny icon that was dragged is put back; try again in a '
                'second.' % _code)
            show = messagebox.showwarning
        else:
            title = i18n.tr('没修好', 'Not fixed')
            # ★R249 一3a：这里原来把内部码（`still-sorted` 这类）当"原因"打印，
            #   用户读到的是一串英文单词。内部码留在日志（`attempt_clear` 那行
            #   「没修好：拖了 N 次…（原因：…）」照旧写），弹窗只说结论。
            msg = i18n.tr(
                '拖了 %d 次，桌面的活动排序还在。\n'
                '被拖过的图标都已经放回原位，桌面没有因为这次操作变乱。\n'
                '过一两秒再点一次试试。' % len(res.get('attempts') or []),
                'Dragged %d times but the active sort is still there.\nEvery '
                'icon that was dragged has been put back, so this run did not '
                'make the desktop messier.\nTry again in a second.' % (
                    len(res.get('attempts') or []),))
            show = messagebox.showwarning
        if st:
            msg += '\n' + read_line
        if adv:
            msg += '\n' + adv
        try:
            show(title, msg, parent=self.root)
        except Exception as e:
            logger.debug("[sort_clear] notify failed: %s", e)

    def _sort_clear_relay(self, moved) -> bool:
        """★R245 A7：清排序成功那一支用"一次性按基准布局摆回"顶替逐个拖回。

        用户 37 批插话原话："干脆快速把图标通过程序的方式，把所有的图标摆成
        布局的位置，这样重启都不乱了。也不用拖回去了，拖回去了反正都是乱的"。
        本机实测支持这个判断（run/r245/r245_a7.txt、r245_a73.txt）：
        拖 1 下清排序 → 批量摆位 → F5 与基准差 0/26 → 重启资源管理器仍 0 偏离。
        ⚠ 病中（排序还没清掉）只批量摆位＝**白摆**（写完差 0、一刷新又差 20），
        所以这条路只挂在 core/sort_clear 的成功分支里（判据 A7-4）。

        `moved` 是"拖过的图标＋原坐标"，本函数不用它（整桌都摆），留着是为了
        让接口能自证"要摆回去也摆得回"，**并且用来决定文案**：★R246 的 view-api
        那一路 `moved` 恒空（一个图标都没挪），再说"改把拖过的图标放回原位"就是假话。
        **拿不到基准布局就返回 False** ⇒ sort_clear 自动退回逐个拖回原位，
        绝不把桌面留在中间态。

        ★R247：基准来源加了回退链（见 `icon_manager._relay_baseline_candidates`），
        主窗这边负责把**用户自己存/恢复过的那一份**（`last_guard_layout`）递过去。
        此前只认守护基准、而守护自 ★R83 起被硬关掉 ⇒ 四条入口每次都是
        `no-baseline`（用户 22:09 日志实证），这一步等于从没做。
        """
        im = getattr(self, 'icon_manager', None)
        if im is None:
            return False
        # "刚才到底拖没拖过图标"决定失败文案（免鼠标支什么都没挪）
        tail = u'，改把拖过的图标放回原位' if moved else \
            u'（这一路本来没挪动任何图标，桌面保持现状）'
        try:
            last_path = ''
            try:
                last_path = (getattr(self, '_app_settings', None) or {}).get(
                    'last_guard_layout', '') or ''
            except Exception as e:
                logger.debug('[sort_clear] last_guard_layout 取不到: %s' % e)
            # 整桌批量移动是**程序所为**：先压住守护 8 秒，否则看护会把这一批
            # 当成"系统回退"再拉一遍，两边打架（★R234 同族，见 suppress_guard）
            im.suppress_guard(8.0)
            res = im.relayout_from_baseline(baseline_path=last_path) or {}
        except Exception as e:
            self._sort_clear_log(u'🧹 批量摆位没做成（%s）%s'
                                 % (type(e).__name__, tail))
            return False
        if res.get('ok'):
            self._sort_clear_log(u'🧹 已一次性按基准布局摆回 %s/%s 个图标（基准=%s·%s）'
                                 % (res.get('moved'), res.get('total'),
                                    res.get('baseline') or '?',
                                    res.get('baseline_src') or '?'))
            return True
        self._sort_clear_log(u'🧹 没有可用的基准布局（%s）%s'
                             % (res.get('err') or '?', tail))
        return False

    def _maybe_offer_sort_clear(self, trigger='guard', throttle=True):
        """查桌面视图 → 该提示才提示 → 用户点「是」才拖（活都在 core/sort_clear）。

        `throttle=False` 只给设置页那枚「🧹 立即修复桌面排序」用：用户自己按的
        按钮不该被"每会话一次 + 冷却"挡住；但**确认框（点「是」才发鼠标）这道
        闸照旧要过**。★R245：检测已常开，这里没有"开关"这一道了。
        """
        if SELF_CHECK or getattr(self, '_sort_clear_busy', False):
            return None                       # 自检态绝不打扰，也不碰真桌面
        try:
            from core import sort_clear as sc
        except Exception as e:
            logger.debug("[sort_clear] import failed: %s", e)
            return None
        now = time.time()
        if throttle and not sc.allow_offer(
                now=now,
                last_offer_ts=getattr(self, '_sort_clear_last_ts', 0.0),
                offers_this_session=getattr(self, '_sort_clear_offers', 0)):
            logger.debug("[sort_clear] throttled (trigger=%s)", trigger)
            return None
        self._sort_clear_busy = True
        # ★R246 #198（用户 38 批 item2b「不可以程序化的方式吗？不可以更快吗？」）：
        # 先给那条**一根手指都不占**的官方接口清法一次机会（core/folder_view，
        # 实测 4/4 轮、~0 秒）；它不行才落到要抢鼠标的合成拖动。延迟导入 ⇒
        # 导入主窗不等于碰 COM，拿不到模块也只是不试这条路（下面 view_clear=None）。
        try:
            from core import folder_view as _fv
            _view_clear = _fv.clear_active_sort
        except Exception as e:
            logger.debug("[sort_clear] view api unavailable: %s", e)
            _view_clear = None
        try:
            res = sc.offer_and_clear(
                settings=self._app_settings,
                icon_manager=self.icon_manager,
                ask=self._sort_clear_ask,
                apply_layout=self._sort_clear_relay,
                view_clear=_view_clear,
                button_down=sc.left_button_down,
                log=self._sort_clear_log) or {}
        except Exception as e:
            logger.error("[sort_clear] failed: %s: %s", type(e).__name__, e)
            res = {}
        finally:
            self._sort_clear_busy = False
        if res.get('offered') or res.get('via') == 'view-api':
            # 只有真弹过确认框才占用这一次额度（读不到桌面/没得修都不算）；
            # ★R246 #198：免鼠标那条不弹框，但它**真把桌面动过了** ⇒ 同样占额度，
            # 不然每轮守护检查都可能悄悄整桌摆一遍，那是新噪音不是修好了。
            self._sort_clear_offers = getattr(self, '_sort_clear_offers', 0) + 1
            self._sort_clear_last_ts = time.time()
        try:
            from core import desk_view_state as dvs
            self._sort_clear_log('🧹 桌面视图检查(%s): %s' % (
                trigger, dvs.summarize(dvs.read_desk_view_state())))
        except Exception:
            pass
        adv = res.get('advisory') or ''
        if adv:
            self._sort_clear_log('🧹 ' + adv)
        if res.get('ok'):
            # 细节（整桌摆回 / 只放回拖过的）由 `_sort_clear_relay` 那行给，
            # 这里只报结论，两行日志各说各的、不重复。
            # ★R246 #198：走免鼠标那条时把"没占用鼠标"写在结论里——用户之所以
            # 只看到图标自己动了，得有一行话说清楚这次动它的是接口不是拖动。
            self._sort_clear_log(
                '🧹 已清掉桌面的活动排序（没占用鼠标）'
                if res.get('via') == 'view-api' else '🧹 已清掉桌面的活动排序')
        # ★R243：日志用户不一定会翻，结果必须当面说一次（判据见 r243 门禁 N1~N4）。
        try:
            self._sort_clear_notify(res, trigger)
        except Exception as e:
            logger.debug("[sort_clear] notify raised: %s", e)
        # ★R249 一2③：用户"想去动鼠标"这件事他自己说不清，只有程序量得出来——
        # 连续攒到 INTERFERE_TALK_AT 次才当面说一句"刚才是程序在动手"（判据 S8~S16）。
        try:
            self._sort_interfere_note(res, trigger)
        except Exception as e:
            logger.debug("[sort_clear] interfere note raised: %s", e)
        return res

    def sort_clear_manual_check(self):
        """设置页「🧹 立即修复桌面排序」：手动、不节流，其余闸口一律照旧。"""
        return self._maybe_offer_sort_clear('manual', throttle=False)

    def _sort_silent_fix(self):
        """★R249 一2①②：切分类摆布局**之前**，安静地把桌面的活动排序清掉。

        用户 26 批 02 定的三件事：「①按建议来 ②不啃声」，风险那条他自己答了
        （「你一点分类，那肯定自动复原……想自己摆图标就手动保存下布局，本来就是
        这样设计的」）⇒ 挂在这里是**原设计**，不是新行为。

        "不啃声"＝这一条路**只走免鼠标官方接口**：一次鼠标都不发、一个框都不弹、
        清不掉就算了（要抢鼠标的那条仍归上面的确认框管）。活都在
        `core/sort_clear.silent_clear_for_layout`，这里只负责把接口接进来。
        延迟导入 ⇒ 导入主窗不等于碰 COM；拿不到也只是不试这条路。
        """
        try:
            from core import sort_clear as sc
            from core import folder_view as _fv
            return sc.silent_clear_for_layout(
                view_clear=_fv.clear_active_sort,
                log=self._sort_clear_log) or {}
        except Exception as e:
            logger.debug("[sort_clear] silent fix unavailable: %s: %s",
                         type(e).__name__, e)
            return {}

    def _sort_interfere_note(self, res, trigger=''):
        """★R249 一2③：连续三次"程序在修的时候用户想去动鼠标"，才弹一次大白话。

        动因（用户原话）：「程序自动修复的时候，你不知道程序在修复，看见图标乱了
        就想去点一下；然后看见鼠标动了，大多数人就放手让程序做。如果连续三次都想
        去动鼠标，就弹对话框——因为有的人压根没注意到主界面上有『修复桌面图标』
        这个选项。」所以：
          · 只在**真发过鼠标**的那条路计数（免接口那条一根手指都没占，无从干涉）；
          · 用户点"否"、或一次都没拖 ⇒ 不计数也**不清零**（那一段根本没发生）；
          · 中间出现一次干净的 ⇒ 清零（"连续"必须真连续）；
          · 到 `INTERFERE_TALK_AT` 次 ⇒ 弹一次、随即归零，不许每轮都弹；
          · 计数是设置：**先落盘再动界面**（★R226 纪律），自检态绝不弹（★R243 同因）。
        """
        if SELF_CHECK or not res:
            return
        try:
            if res.get('user_said_no') or res.get('via') == 'view-api':
                return
            if not (res.get('attempts') or []):
                return
            st = self._app_settings
            cur = int(st.get('sort_interfere_streak') or 0)
            if not res.get('interfered'):
                if cur:
                    st['sort_interfere_streak'] = 0
                    self._save_app_settings()
                    self._log(
                        '🧹 这一次没人碰鼠标，"想去动鼠标"的连续计数归零')
                return
            cur += 1
            st['sort_interfere_streak'] = cur
            self._save_app_settings()             # ★R226：先落盘
            self._log(
                '🧹 修桌面排序的时候检测到你想去动鼠标（连续第 %d 次）' % cur)
            if cur < INTERFERE_TALK_AT:
                return
            st['sort_interfere_streak'] = 0
            self._save_app_settings()
            messagebox.showinfo(
                i18n.tr('修桌面图标的时候', 'While the desktop is being fixed'),
                i18n.tr(
                    '连着三次，程序在自动整理桌面排序的那一下，你都伸手去抓了鼠标。\n\n'
                    '那是程序在动手，不是桌面坏了：它要把图标按你存好的布局排回去，'
                    '一般一秒就好。这会儿请松手，让它做完，别跟它抢。\n\n'
                    '想自己摆图标：等它停手之后再拖，摆好了点「保存布局」就行。',
                    'Three times in a row you reached for the mouse while the '
                    'app was tidying the desktop sort.\n\n'
                    'That was the app working, not the desktop breaking: it is '
                    'putting the icons back where you saved them, which takes '
                    'about a second. Please let go and let it finish.\n\n'
                    'Want to arrange icons yourself? Wait until it stops, drag '
                    'them into place, then hit \u201cSave layout\u201d.'),
                parent=self.root)
            self._log(
                '🧹 已当面说明过"刚才是程序在动鼠标"，连续计数归零')
        except Exception as e:
            logger.debug("[sort_clear] interfere note failed: %s: %s",
                         type(e).__name__, e)

    def _on_toggle_stats_capture(self):
        """★ R31：统计开关按钮（薄 UI 层）——启停/持久化/日志都在
        stats_capture.StatsCaptureManager，这里只做弹窗指引与界面同步"""
        ok, err = self.stats_capture.toggle()
        if not ok:
            messagebox.showerror('无法开启统计', err, parent=self.root)
        self.stats_panel.update_toggle_ui(self.stats_capture.enabled)

    def _stats_tab_selected(self):
        """当前选中的是不是统计页（★R230：比较跨图标档，见 icons.is_tab_text）"""
        return icons.is_tab_text(
            str(self.notebook.tab(self.notebook.select(), 'text')),
            'stats', i18n.tr('统计', 'Stats'))

    def _refresh_stats_if_visible(self):
        """统计页正打开时刷新榜单（供 _ui_queue 调用）"""
        try:
            # ★ R73：统计页标签已双语，比较值同步走 tr
            # ★R225：比较串改走 _tab_text——图标档一切换 emoji 字面就变了，
            # 再写死 '📊 统计' 会静默失去"切到统计页刷新榜单"
            # ★R230：进一步走 is_tab_text，图标档"即时切 / 页签重启才重排"的
            # 中间窗口期也不再脱钩
            if self._stats_tab_selected():
                self.stats_panel.refresh()
        except Exception:
            pass

    def _on_tab_changed(self, event=None):
        """记住上次选中的选项卡。

        ★ R73 捉虫：改存**索引**——旧版按标签文字存，双语切换后文字变了，
        "记住上次标签页"会静默失效。索引 + 启动时越界保护（restore 处
        try/except）。将来增删 tab 由 restore 的越界保护兜底。"""
        try:
            self._app_settings['ui_tab'] = self.notebook.index(
                self.notebook.select())
            self._save_app_settings()
            # ★ S 段：切到统计页时重算榜单（★R230：比较跨图标档）
            if self._stats_tab_selected():
                self.stats_panel.refresh()
        except Exception:
            pass

    def _update_lists(self):
        """更新所有列表"""
        # 更新分类列表（[顶]=在顶部悬浮窗显示, [驻]=常驻桌面）
        self.category_list.delete(0, tk.END)
        categories = self.category_manager.get_all_categories()
        for cat in categories:
            icon_count = len(cat.get('icons', []))
            flags = ''
            if cat.get('in_dock', True):
                flags += f" [{i18n.tr('顶', 'Top')}]"
            if cat.get('sticky', False):
                flags += f" [{i18n.tr('驻', 'Res')}]"
            self.category_list.insert(tk.END, f"{cat['name']} ({icon_count}){flags}")

        # 更新布局列表
        self.layout_list.delete(0, tk.END)
        layouts = self.icon_manager.list_layouts()
        for l in layouts:
            ts = (l.get('timestamp', '') or '')[:19].replace('T', ' ')
            # 紧凑时间：09-04 08:39（保证窄栏也显示全）
            ts = ts[5:16] if len(ts) >= 16 else ts
            self.layout_list.insert(
                tk.END, f"{_cr_disp(l['name'])} ({l['icons']} "
                        f"{i18n.tr('图标', 'icons')}, {ts})")

        # ★R218 阶段 3：分类/布局列表上隔行底色（斑马纹），随主题重刷
        ui_layout.zebra_listbox(self.category_list, COLORS)
        ui_layout.zebra_listbox(self.layout_list, COLORS)

        # 同步分类明细子列表
        self._on_main_cat_select()

    def _update_stats(self, force: bool = False):
        """更新统计信息（健壮：Win32 调用失败不抛出）"""
        # ★R285-O1（P2，崩溃暴露面）：这个定时链每 2 秒就往 Explorer 发一次
        # LVM_GETITEMCOUNT——R010 与 R012 两份 crash.log 的现场都落在
        # _update_stats→get_icon_count→send_msg_timeout 上（native abort，
        # ★R282 #167 已判"软件侧拦不住"）。拦不住不等于不加码：主窗藏着的时候
        # 这两个跨进程读只是给**看不见的标签**刷新，那就别读；唤回主窗时补一次。
        # 说清楚边界：这**不保证**不崩（崩因在外部注入的 DLL/Explorer 侧），
        # 只是把"横条后台运行"这种常态下的主线程跨进程读整段去掉。
        if not force and not self.root.winfo_viewable():
            return
        try:
            total = self.icon_manager.get_icon_count()
            visible = 1 if self.icon_manager.is_icons_visible() else 0
        except Exception as e:
            logger.error(f"读取桌面状态失败: {e}")
            total, visible = 0, 0
        clicked = len([c for c in self.click_tracker.get_all_clicks().values() if c > 0])

        self.total_var.set(str(total))
        self.clicked_var.set(str(clicked))
        self.visible_var.set(str(visible))

        # 获取热门图标
        top = self.click_tracker.get_top_clicked(1)
        if top:
            name = list(top.keys())[0]
            count = list(top.values())[0]
            self.top_var.set(f"{name[:8]}... ({count})")
        else:
            # ★ R185（TRAE #3）：走 i18n（英文界面此前显示中文"无数据"）
            self.top_var.set(i18n.tr('无数据', 'No data'))

    def _load_data(self):
        """加载数据"""
        self._update_lists()
        # force：构建期窗口还没 mapped，winfo_viewable 会是 False，
        # 不 force 的话首屏统计标签会空两秒（★R285-O1 的边界）
        self._update_stats(force=True)

    def _start_refresh_timer(self):
        """启动定时刷新（必须捕获所有异常：after 回调抛异常会终止主循环）"""
        def refresh():
            try:
                self._update_stats()
                if self.parental.is_kids():
                    self._update_kids_status()
            except Exception as e:
                logger.error(f"刷新统计出错(已忽略): {e}")
            # ★ 与 P1-N7 热键轮询 / 单实例轮询同款：after 也挪进 try，
            # 退出瞬间 root 已销毁时不会抛未捕获 TclError 静默终止定时链
            try:
                self._refresh_timer = self.root.after(2000, refresh)
            except Exception:
                pass

        refresh()

    def _log(self, message: str):
        """记录日志（GUI 日志页 + 文件 app.log）。

        ★ R133：此前只写 GUI 日志页——主窗口隐藏时的一切提示（快捷键
        被拒、修复说明等）在 app.log 里完全不可见，远程排障无据可查。
        ★ R191（Qoder GUIA-1 总闸）：本方法被十几个后台线程直接调用，
        Tk 控件只许主线程碰——非主线程一律回投主线程执行，文件日志
        线程内先落一份（防止主线程忙碌/已销毁时丢信息）。
        ★R318 B：回投的那一份改走 `_show_log`（只上屏、不再落盘）。旧写法
        回投 `self._log` 本身 ⇒ 后台线程每一条都进 app.log **两遍**（线程内
        一遍、主线程再一遍），真机日志里成对重复的行就是这么来的：一行时间戳
        差几毫秒、内容逐字相同，读日志的人先要怀疑是不是发生了两次事件。"""
        if threading.current_thread() is not threading.main_thread():
            try:
                logger.info(message)
            except Exception:
                pass
            # ★R204 NEW-A1：跨线程回投统一走 _ui_queue（同文件 docstring
            # 明文纪律）——root.after 从工作线程调用本身是跨线程触 Tcl
            # （R142 闪退同族），_ui_queue 是纯 queue.Queue 无此问题
            try:
                self._ui_queue.put(lambda m=message: self._show_log(m))
            except Exception:
                pass   # 队列没了（退出中）：文件日志已落，GUI 行放弃
            return
        self._show_log(message)
        try:
            logger.info(message)
        except Exception:
            pass

    def _show_log(self, message: str):
        """只把一行打到界面日志页（**不碰文件**）——上屏这一路与落盘分开。

        ★R318 B 的另一半：主线程 `_log` 走"上屏 + 落盘"，后台线程自己在
        那一侧落盘、回投时只欠一次上屏。两路共用这一段，English 档改写
        （★R229 方案 A）与界面封顶也只有一处。
        """
        timestamp = datetime.now().strftime('%H:%M:%S')
        # ★R229（用户 01 批 item2，方案 A）：English 模式下日志**上屏前**按
        # 短语典改写（utils/log_i18n.py）。源码里的中文原文一个字不动，
        # app.log / switch.log 仍写中文——远程排障、全文搜索、历轮门禁断言
        # 全部按中文口径，改动面只有这一行。
        shown = log_i18n.translate(message)
        try:
            self.log_text.insert(tk.END, f'[{timestamp}] {shown}\n')
            # ★R226（P2-3）：界面日志封顶只裁 Text，data/app.log 落盘链路不动
            trim_log_text(self.log_text)
            self.log_text.see(tk.END)
            self.status_var.set(shown[:50])
        except Exception:
            pass   # 日志页未建好/已销毁：文件日志仍要落

    def _auto_apply_layout_on_launch(self):
        """★R241（用户 33 批 item2「重启电脑后桌面图标是乱的，需要开一下
        软件才能变正确」）：可选的"启动时自动套用布局"。

        为什么原来不行：设置里"退出时恢复布局 = 当前"时，启动链只走
        `_reactivate_last_slot`——那是恢复**显隐**（哪个分类的图标可见），
        一步都不摆**位置**。位置只有两个来源：命名布局分支的
        `_apply_layout_by_name`、旧守护留下的 `last_guard_layout`；用户选
        "当前"两条都不触发 ⇒ 桌面保持重启前 Explorer 自己排的样子。

        口径：
          · 默认关（`apply_layout_on_launch`）——自动挪图标是会动手用户
            桌面的动作，必须用户自己勾；
          · "退出恢复=指定布局"那条链已经在摆位置 ⇒ 这里直接返回。同一
            布局连写两遍会撞 Explorer 的碰撞顺延（★R237 实测），再来一遍
            不是保险，是制造错位；
          · ★R312："退出恢复=当前"且 900ms 那条恢复链自己已经摆过位 ⇒ 同样
            返回（判据见 `_startup_chain_will_lay`）；没摆过（没勾"切分类套同名
            布局"、或压根没有存档）这趟照旧生效——正是 ★R241 用户勾它要的效果；
          · 目标顺序：上次记住的布局文件 → 上次分类的同名存档；都没有
            就只记日志，绝不猜一个来摆；
          · SELF_CHECK 直接返回：测试态不许碰真桌面（R221 纪律）。
        """
        if SELF_CHECK:
            return
        try:
            st = self._app_settings
            if not bool(st.get('apply_layout_on_launch', False)):
                return
            restore_layout = (st.get('close_restore_layout', '') or '当前')
            if bool(st.get('close_restore_enabled')) and restore_layout != '当前':
                logger.info('[启动] 命名布局那条链已经在套用，跳过重复摆位')
                return
            # ★R312 item3：900ms 的"恢复上次分类"那条链自己就会摆位（见
            # `_startup_chain_will_lay`）——已经摆过还来这趟，就是把一份不相干的
            # 旧守护布局盖在刚排好的桌面上（真机 R029 17:10:31 实测盖掉「全部」）。
            if bool(st.get('close_restore_enabled')) and restore_layout == '当前' \
                    and getattr(self, '_startup_chain_laid', False):
                logger.info('[启动] 恢复分类那条链已经摆过位置（上次分类「%s」），'
                            '跳过重复摆位' % (st.get('last_active_slot', '') or '?'))
                return
            last = st.get('last_guard_layout', '')
            if last and os.path.exists(last):
                r = self.icon_manager.restore_layout(last)
                logger.info(f"[启动] 自动套用上次布局: 匹配="
                            f"{r.get('matched', 0)} 成功={r.get('success')}")
                return
            slot = st.get('last_active_slot', '')
            if slot and slot not in ('全部', '窗', '✕', '当前'):
                r = self._apply_layout_by_name(slot)
                logger.info(f"[启动] 自动套用分类同名布局 [{slot}]: "
                            f"{'成功' if r else '无存档/未生效'}")
                return
            logger.info('[启动] 已开"启动时自动套用布局"，但没有任何可用存档'
                        '（先在想要的桌面上保存一次布局，或点一次分类）')
        except Exception as e:
            logger.error(f"[启动] 自动套用布局失败: {e}")

    def _remember_guard_layout(self, path: str):
        """记住守护的布局路径，程序重启后自动恢复并继续守护"""
        try:
            self._app_settings['last_guard_layout'] = path
            self._save_app_settings()
        except Exception:
            pass

    def _finish_restore(self, layout_path, first_result):
        """_on_restore 的收尾：布防+提示（R100 起不再二次恢复）"""
        try:
            if first_result.get('success'):
                self._remember_guard_layout(layout_path)
                mgr = self.icon_manager
                hint = (f"（已激活守护：布局 [{mgr.saved_layout_name}]）"
                        if mgr.guard_enabled and mgr.saved_layout_name else '')
                self._log(f'✓ 已恢复 {first_result["restored"]} 个图标 {hint}')
                # 通知高亮框刷新（位置变了，签名会变）
                try:
                    self.highlighter.notify_changed()
                except Exception:
                    pass
            else:
                self._log(f'✗ 错误: {first_result.get("error", "")}')
        except Exception as e:
            logger.error(f"恢复布局收尾失败: {e}")
            self._log(f'✗ 恢复收尾出错: {e}')
        finally:
            try:
                self.status_var.set('')
            except Exception:
                pass

    # ----------------------- 数据管理（导出/导入/清空） -----------------------

    def _on_drag_panel_changed(self):
        """★ #2：拖拽面板改动了分类/归属后回调——立刻刷新主界面。

        此前面板是独立 Toplevel，保存后只改 category_manager 数据、不通知
        主窗口，导致左栏分类的图标数目要重启程序才更新（用户反馈）。
        回调链路：面板 _commit_pending / 分类增删改 → 本方法。
        """
        try:
            self._update_lists()
            self._rebuild_sticky_grid()
        except Exception as e:
            logger.error(f"拖拽面板回调：刷新分类列表失败: {e}")
        try:
            # 分类可能被新建/删除 → 悬浮条槽位同步
            self.dock.sync_slots(self.category_manager.get_all_categories())
        except Exception as e:
            logger.error(f"拖拽面板回调：同步悬浮条槽位失败: {e}")

    def _refresh_drag_panel(self):
        """同步刷新拖拽面板"""
        from drag_drop import get_panel
        panel = get_panel()
        if panel and panel.winfo_exists():
            panel._refresh_icon_list()
            panel._refresh_category_list()

    def _layout_matches_target(self, name: str) -> bool:
        """★ R167：当前桌面图标位置是否已与目标布局一致（±2px）。

        用于关主窗"退出时恢复布局"按需执行——没改动就不做恢复+F5，
        消除无谓卡顿。任何异常按"不一致"处理（走原恢复路径，安全侧）。"""
        try:
            lay = next((l for l in self.icon_manager.list_layouts()
                        if l.get('name') == name), None)
            if not lay:
                return False
            data = json.loads(Path(lay['path']).read_text(encoding='utf-8'))
            saved = data.get('icons') or {}
            if not saved:
                return False
            live = self.icon_manager._read_all_positions_live()
            if not live or set(live.keys()) != set(saved.keys()):
                return False
            for n, xy in saved.items():
                lx, ly = live.get(n, (None, None))
                if lx is None or abs(lx - xy[0]) > 2 or abs(ly - xy[1]) > 2:
                    return False
            return True
        except Exception as e:
            logger.warning(f'布局一致性检查失败（按需恢复）: {e}')
            return False

    def _on_close(self, force_exit: bool = False):
        """关闭窗口：可选择保留悬浮窗后台运行。

        force_exit=True → 无条件走退出流程（横条 ✕ 调用，见 _exit_from_dock）。
        默认 False → 主窗口 X，按 close_keep_dock 决定"隐藏后台运行"还是"退出"。
        """
        # ★ P2-N26 + P2-N30：重入 guard——用户重复点关闭 / 弹框后再点关闭时
        # 直接吞掉，避免重复 withdraw + 注册多个 after(超时) + 重复走清理流程
        # 导致 _close_timeout_id 被覆盖、旧 after 无法 cancel、连弹多个弹框
        if getattr(self, '_closing', False):
            return
        self._closing = True
        keep = bool(self._app_settings.get('close_keep_dock'))
        if keep and not force_exit and self.root.winfo_viewable():
            # ★ 方案A：主窗 X（常驻隐藏前）若设置了"退出时恢复指定布局"，
            #   且指向具体布局（非"当前"），先套用该布局再隐藏。
            #   修复：此前这段只 hidden 就 return，"退出时恢复指定布局"被常驻
            #   分支抢先拦截，从不执行——只有点悬浮窗 ✕ 真退出才生效。
            try:
                if (self._app_settings.get('close_restore_enabled')
                        and (self._app_settings.get('close_restore_layout', '') or '当前') != '当前'):
                    _restore_name = self._app_settings.get('close_restore_layout', '')
                    # ★ R167：桌面已与目标布局一致（±2px）就跳过恢复——
                    # 此前没改动也执行整套恢复+F5（用户实测"点关闭卡一下"）
                    if self._layout_matches_target(_restore_name):
                        self._log('🫥 桌面与目标布局一致，跳过恢复（无卡顿）')
                    else:
                        self._apply_layout_by_name(_restore_name)
            except Exception as e:
                logger.error(f"常驻隐藏前套用布局失败: {e}")
            # 最小化到横条：程序继续运行（快捷键/横条全部可用）
            # 注意：此分支是"后台运行"不是退出，重置 guard 让下次还能切回
            self._closing = False
            self.root.withdraw()
            self._log('🫥 主窗口已隐藏，悬浮窗继续运行（横条"窗"可呼出，"✕"退出）')
            return

        # ★ P1-1：先 withdraw 主窗口，让用户立即看到关闭响应
        # 后续退出流程含 time.sleep(0.8)（F5 刷新等待）+ realign_icons，
        # 都是 Explorer 同步操作无法避让。窗口先隐藏后，用户感知不到这段冻结，
        # 程序在不可见状态下做完清理再 destroy。dock 也会在 destroy 阶段一起销毁。
        try:
            self.root.withdraw()
        except Exception:
            pass

        # ★ P2-N28 + P2-N29 + P1-N22：超时兜底
        # 若 destroy 未执行（某步异常被吞 / dock.destroy 阻塞），恢复窗口让用户看到状态，
        # 避免"窗口消失但进程还在、主窗口呼不出"。destroy 正常执行时 mainloop 退出，此回调不触发。
        # ★ P2-N29：从 3s 拉到 6s——给清理步骤留余量。
        #   实测最慢约 1.5s（_force_listview_refresh + sleep(0.8) + realign_icons），
        #   一旦 explorer 卡顿多花 2s 就会撞上 3s 误触，6s 更稳。
        # ★ P1-N22 覆盖场景说明（避免后续维护者误以为万能）：
        #   此兜底依赖 Tk 事件循环——after 回调只在事件队列空闲时才触发。
        #   ① 能救：_on_close 已跑完、destroy 因某原因未执行（异常被吞 / destroy 返回但 Tk 没退）
        #      → 主线程回到事件循环 → after 触发恢复窗口。
        #   ② 救不了：_on_close 里某步同步阻塞超过 6s（如 dock.destroy 卡死、unhide_all 超时）
        #      → 主线程一直卡在 _on_close 里，after 永远轮不到触发，兜底形同虚设。
        #   当前所有同步步骤实测 < 2s，6s 余量充足；若后续新增耗时步骤需同步审视。
        def _close_timeout():
            try:
                if self.root.winfo_exists():
                    logger.error("【退出】6 秒超时未完成 destroy，恢复窗口让用户看到状态")
                    # ★ P1-N23：重置 _closing guard，让用户能重试点关闭按钮
                    # 否则 _closing 一直为 True，用户再点关闭被静默吞掉 → "关不掉"
                    # ★ P2-N31：同步清掉已触发的 _close_timeout_id（旧 id 失效但留坏味道）
                    self._closing = False
                    self._close_timeout_id = None
                    self.root.deiconify()
                    messagebox.showerror(
                        '退出失败',
                        '程序退出流程超时，请再次点击关闭按钮。\n'
                        '若反复出现请查看 data/app.log 排查。',
                        parent=self.root)
            except Exception:
                pass
        self._close_timeout_id = self.root.after(6000, _close_timeout)

        logger.info("=" * 60)
        logger.info("【退出调试】开始退出流程")
        logger.info("=" * 60)

        # ★R270-R3：统计落盘已合并成 15 秒窗口（点图标的热路径不再每次整份
        # 重写 JSON），所以退出开头就得把窗口期内的账冲掉。放在最前面而不是
        # 收尾，是因为下面全是 Explorer 同步操作（F5 等待 + realign）——一旦
        # 卡住撞上 6 秒兜底甚至被杀，账就永远丢了。
        try:
            _fl = self.usage_stats.flush()
            logger.info("【退出】统计账落盘：" +
                        ("有未保存数据，已写入" if _fl else "无待落盘数据"))
        except Exception as e:
            logger.error(f"【退出】统计账落盘失败(已忽略): {e}")

        try:
            # 步骤 1：按"退出时恢复指定布局"设置决定退出行为
            # （2026-09-02 修复：此前该设置从未被读取，退出永远全量显示）
            restore_enabled = bool(self._app_settings.get('close_restore_enabled'))
            restore_name = (self._app_settings.get('close_restore_layout', '') or '当前')
            logger.info(f"【退出】退出恢复设置: 启用={restore_enabled}, 布局={restore_name}")
            if restore_enabled and restore_name == '当前':
                # 保持当前分类：可见性与图标位置都不动（下次启动自动恢复该分类）
                logger.info("【退出】保持当前分类状态（不恢复全量）")
            else:
                try:
                    # ★ 2026-09-11 修复：命名布局（含"全部"/"工作"）须先切到对应
                    #   分类显隐再摆位。此前只 unhide_all+摆位 → 恢复"工作"后桌面
                    #   仍是"全部"的可见集。现在统一走 _activate_slot_by_name
                    #   （切显隐 + 套用同名布局摆放）。仅保留 unhide_all 兜底，
                    #   用于该布局无存档且无对应槽位（选了已删分类）的极罕场景。
                    #   ★ R185（TRAE #2）：去掉 != '全部' 短路——
                    #   _activate_slot_by_name 本就特判"全部"（恢复全部图标），
                    #   此前"全部"直接跳过切显隐，退出时被隐藏的图标保持隐藏。
                    if not self._activate_slot_by_name(restore_name):
                        logger.warning(f"【退出】布局 {restore_name} 无对应槽位，回退全量显示")
                        # ★R305：回退分支也得先把盒里的散回来（主路径经 _show_all_icons
                        #   已经带上了，这条绕不过去的是因为它绕过了那条链）
                        self._release_boxes('exit-fallback')
                        self.icon_manager.unhide_all()
                    try:
                        self.icon_manager.notify_virtual_change()
                    except Exception:
                        pass
                    # F5 冲刷让桌面条目立即更新
                    try:
                        self.dock._force_listview_refresh()
                        time.sleep(0.8)
                    except Exception:
                        pass
                    # 位置校正：Explorer 偶发把重现图标放到别的空网格位
                    try:
                        fixed = self.icon_manager.realign_icons(
                            list(self.icon_manager._hidden.keys()))
                        if fixed:
                            logger.info(f"【退出】位置校正: 搬回 {fixed} 个")
                    except Exception:
                        pass
                    # 套用同名布局摆放（activate 已切显隐；无存档则跳过位置）
                    cands = sorted((APP_DIR / 'icon_backups').glob(
                        f"{restore_name}_*.json"))
                    if cands:
                        r = self.icon_manager.restore_layout(str(cands[-1]))
                        logger.info(f"【退出】已套用布局 {restore_name}: "
                                    f"匹配 {r.get('matched', '?')}")
                    else:
                        logger.warning(f"【退出】找不到布局存档 {restore_name}")
                except Exception as e:
                    logger.error(f"【退出】恢复图标可见性失败: {e}", exc_info=True)

            # ★ R71 退出防恐慌：恢复目标（或当前激活的分类）是空分类时，
            # 桌面会被清空后退出——第一次用的人再启动看到"图标全没了"会慌
            # （2026-09-15 用户实测）。退出前发现一个可见图标都不剩，
            # 就自动显示全部图标，绝不留空桌面。
            try:
                _total = self.icon_manager.get_icon_count()
                _hidden_n = len(self.icon_manager._currently_hidden)
                if _total and _hidden_n >= _total:
                    self._release_boxes('exit-empty')
                    self.icon_manager.unhide_all()
                    try:
                        self.icon_manager.notify_virtual_change()
                    except Exception:
                        pass
                    logger.info('【退出】检测到恢复目标分类为空（桌面将被清空），'
                                '已自动显示全部图标（防恐慌保护）')
            except Exception as e:
                logger.error(f'【退出】空桌面保护检查失败: {e}')

            # ★ R185（TRAE #1）：删除退出调试段——硬编码开发者图标名单
            # （ZCode/TRAE/python…）每次退出往日志写十来行噪音，生产无意义

            logger.info("【退出】退出流程完成")
            logger.info("=" * 60)

        except Exception as e:
            logger.error(f"【退出调试】退出清理异常: {e}", exc_info=True)
        # ★ P0-2（上轮遗漏）+ P2-F：尾段清理裸调 → 任一步抛异常后续不执行
        #   → clean_exit.flag 不写 → launcher 视为崩溃重启；
        #   root.destroy() 不执行 → 进程假死，6s 后 _close_timeout 恢复窗口
        #   但用户再点关闭仍失败。统一抽 _teardown()，每步独立 try + logger，
        #   finally 里写 flag + destroy() 确保一定执行。
        try:
            self._teardown()
        finally:
            # ★ P1-3：删除原 SetEvent 死代码——destroy 前给 event 发信号无人接收
            # （本实例即将退出，新实例启动时是首个实例不会走 OpenEventW 分支）
            # 若需要"下次启动自动弹窗"，应写 settings 标记让 main() 启动时读取
            # ★ P1-N9：写 clean_exit.flag，让守护器据此判断"用户主动退出"
            # 而不再依赖固定 5 秒阈值（onefile 冷启动 3~6s 会误判为干净退出）
            # ★R207 NEW3-A1（P1）：统一走 write_clean_exit_flag——全工程
            # 唯一写入口（另一处=app_bootstrap 第二实例唤醒退出）
            try:
                from utils.clean_exit import write_clean_exit_flag
                write_clean_exit_flag()
            except Exception:
                pass
            # root.destroy() 必须执行——否则 mainloop 不退、进程假死
            try:
                self.root.destroy()
            except Exception as e:
                # ★ R194（用户 21 批"退出后 python 进程不退"实锤：win11
                # 日志 root.destroy 失败: can't delete Tcl command）——
                # Py3.14 的 Tk 下 destroy 半途抛 TclError 被吞掉后，
                # mainloop 永不返回 → 进程隐身常驻（launcher 跟着吊住，
                # 用户"删文件夹提示被占用"）。重试一次；仍失败则硬退出
                # ——清理与 clean_exit.flag 都已完成，硬退出安全。
                logger.error(f"【退出】root.destroy 失败: {e}", exc_info=True)
                try:
                    self.root.destroy()
                    logger.info("【退出】destroy 重试成功，正常退出")
                except Exception as e2:
                    logger.error(f"【退出】destroy 重试仍失败，强制退出: {e2}")
                    # ★R204 NEW-A3 纪律位：os._exit 跳过 atexit/finally/GC，
                    # 只允许出现在这条"destroy 重试仍失败"的最后兜底链上
                    # ——新增退出路径严禁随手 os._exit，必须先完成
                    # _teardown + clean_exit.flag 再考虑。
                    try:
                        sys.stdout.flush()
                        sys.stderr.flush()
                    except Exception:
                        pass
                    os._exit(0)

    def _teardown(self):
        """退出清理：每步独立 try，避免单点异常导致 flag 不写 / destroy 不执行。

        被调用点：仅 _on_close（force_exit / 普通退出共用一条路径）。
        设计原则：① 不抛异常出去（调用方用 finally 写 flag + destroy）；
        ② 每步都记日志，方便定位"哪步开始出问题"；③ 顺序按"对用户感知
        影响从弱到强"——定时器/监控先停，再销毁可见 UI（dock），最后才
        亮度恢复（独立异步，不影响主线程退出）。
        """
        # 0. 条件监视停止（★R204 NEW-A2：此前漏停——os._exit 兜底/destroy
        #    重试的最坏 6s 窗口内，监视线程仍可能命中"进程消失→关机"规则，
        #    叠加 CORE-33=程序都关了电脑还被关机）
        try:
            _mon = getattr(self, 'monitor', None)
            if _mon is not None:
                _mon.stop()
        except Exception as e:
            logger.error(f"【退出】monitor.stop 失败: {e}", exc_info=True)
        # 0.5 ★R254（用户 45 批 item2）：隐藏态退出必须把桌面图标窗口 SW_SHOW
        #    回来——「🙈 隐藏」藏的是 explorer 的 SHELLDLL_DefView，那是别人家的
        #    窗口，不随本进程死亡复原；此前退出全程没人拉回 ⇒ 关程序黑桌面。
        #    置于 dock.destroy 之前：后面步骤再卡死，桌面此时已复明。
        #    幂等：对已可见窗口零副作用（常驻隐藏分支不走这里，用户主动隐藏的
        #    "后台运行"态照旧尊重，dock「全部」仍可恢复）。
        try:
            self.icon_manager.ensure_desktop_view_visible()
        except Exception as e:
            logger.error(f"【退出】ensure_desktop_view_visible 失败: {e}",
                         exc_info=True)
        # 1. 高亮监控停止（注册了 SetWinEventHook，必须解钩避免回调到已销毁对象）
        try:
            self.highlighter.stop_monitor()
        except Exception as e:
            logger.error(f"【退出】highlighter.stop_monitor 失败: {e}", exc_info=True)
        # ★ R185（TRAE #6）：单实例唤醒事件句柄用完即关（此前泄漏到进程退出）
        try:
            _evt = getattr(self, '_show_evt', None)
            if _evt:
                ctypes.windll.kernel32.CloseHandle(_evt)
                self._show_evt = None
        except Exception as e:
            logger.error(f"【退出】CloseHandle 失败(忽略): {e}")

        # 2. 取消主刷新定时器（after_cancel 对 invalid id 抛 TclError，需 try + 清 None）
        # ★ P2-F：原 `if self._refresh_timer:` 块里 after_cancel 无 try，
        # 退出瞬间 _refresh_timer 可能已被消费 → after_cancel 抛 TclError
        # → 后续 dock.destroy / root.destroy 全部跳过 → 进程假死。
        if getattr(self, '_refresh_timer', None):
            try:
                self.root.after_cancel(self._refresh_timer)
            except Exception:
                pass
            self._refresh_timer = None

        # 3. 取消防抖切换（dock._debounce_timer 是 dock 内部状态，可能不存在）
        if getattr(self.dock, '_debounce_timer', None):
            try:
                self.root.after_cancel(self.dock._debounce_timer)
            except Exception:
                pass
            self.dock._debounce_timer = None

        # 5. 横条销毁（最大不确定源——含若干 Win32 hook + 子窗口销毁，
        #    一旦抛异常会把 flag + destroy 一起带崩，故单独 try）
        try:
            self.dock.destroy()
        except Exception as e:
            logger.error(f"【退出】dock.destroy 失败: {e}", exc_info=True)

        # 6. 高亮清空（rect 列表 + 可能的画框窗口）
        try:
            self.highlighter.clear()
        except Exception as e:
            logger.error(f"【退出】highlighter.clear 失败: {e}", exc_info=True)

        # 7. 热键停止（注册过的 RegisterHotKey 在此 UnregisterHotKey）
        try:
            self.hotkey_mgr.stop()
        except Exception as e:
            logger.error(f"【退出】hotkey_mgr.stop 失败: {e}", exc_info=True)

        # 7.5 下载监听停止（★ R112 daemon 线程收尾）
        try:
            self.download_tracker.stop()
        except Exception as e:
            logger.error(f"【退出】download_tracker.stop 失败: {e}", exc_info=True)

        # 8. 亮度恢复（P1-2：退出前恢复亮度；★ R98 起按屏独立，
        #    set_level(100) 只会收"作用屏"一台，退出必须全部收起）
        try:
            self.dimmer.restore_now()
        except Exception as e:
            logger.error(f"【退出】dimmer.restore_now 失败: {e}", exc_info=True)

        # 9. 取消 P2-N28 超时兜底回调（destroy 即将执行，不需要恢复窗口）
        try:
            if getattr(self, '_close_timeout_id', None):
                self.root.after_cancel(self._close_timeout_id)
                self._close_timeout_id = None
        except Exception:
            pass

        # 10. 清退出 guard，幂等。正常路径 destroy 后就结束，此步无感；
        #    但若未来在 _teardown 之后、root.destroy() 之前插入代码并抛异常，
        #    _closing 会一直 True → 用户再点关闭被吞掉 → "关不掉"。
        try:
            self._closing = False
        except Exception:
            pass

    def mainloop(self):
        """运行主循环"""
        self.root.mainloop()




# ★ R149：启动/退出集群迁 gui/app_bootstrap（诊断/单实例/DPI/main）。
# 类体内 _start_show_event_poll 仍引用单实例事件名（唤醒监听）——留常量别名
from gui.app_bootstrap import _SINGLETON_SHOW_EVENT as _SINGLETON_SHOW_EVENT
from gui.app_bootstrap import _SINGLETON_MUTEX as _SINGLETON_MUTEX
# ★ R162b/c：切换日志器真身在 app_bootstrap（第七刀迁入）。注意
# from-import 只绑定被引名字、不绑定模块名——必须直接 import 目标。
from gui.app_bootstrap import _switch_logger as _switch_logger

# main 薄壳兼容旧入口（launcher.pyw 历史指向 gui.main_window:main 的调用
# 与直跑 python gui/main_window.py 场景），实现单点在 app_bootstrap。
def main():
    from gui.app_bootstrap import main as _boot_main
    _boot_main()


if __name__ == '__main__':
    main()
