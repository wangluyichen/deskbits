# -*- coding: utf-8 -*-
"""⌨️ 全局快捷键面板（R140 自 gui/main_window.py 拆出，codeSplitter 一刀）。

职责：热键配置读写（config/hotkeys.json）+ 注册/轮询 + 面板 UI + 捕获对话框。
纪律：**严禁 import gui.main_window**（会把直跑脚本再执行一遍，见
dist-manager-module-identity-double-load）；主窗只留薄接线。
gui 依赖一律通过参数传入的 gui 对象属性（_hotkey_cfg/hotkey_mgr/dock/_log/root）。
"""
import json
import logging
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox

from gui import ui_layout
from gui.ui_layout import SP
from utils.app_paths import get_app_dir
from utils import i18n
from utils.hotkey_manager import (DEFAULT_HOTKEY_CONFIG, CONFLICT_RULES,
                                  MOD_NAMES, parse_binding, format_binding,
                                  vk_to_name, name_to_vk, check_conflicts,
                                  binding_is_bare_alt, to_ctrl_alt)

logger = logging.getLogger("desktop_manager")

# 主题色引用：build_card 时传入主窗 COLORS 的**引用**（apply_ui_theme 原地
# 更新该 dict，主题切换后这里自动跟随，无需重建）
_colors = {'bg': '#282a36', 'fg': '#f8f8f2', 'primary': '#8be9fd',
           'surface': '#44475a', 'border': '#636d83'}


def set_colors(colors: dict):
    """持有主窗 COLORS 的引用（非拷贝），随主题原地换色"""
    global _colors
    _colors = colors


# ---------------------------------------------------------------------
# 配置读写
# ---------------------------------------------------------------------

def config_path() -> Path:
    return get_app_dir() / 'config' / 'hotkeys.json'


# ★R285-N28：本会话的"热键配置读不动"闸门（单向闩，唯一出口是重启进程）。
# 只在这一份模块级变量里存，**绝不写进配置载荷**（disk 上的 JSON 键集始终＝
# DEFAULT_HOTKEY_CONFIG 的键集，门禁 N28-13 钉着）。
_load_failed = False


def load_config() -> dict:
    """读 config/hotkeys.json，坏档回默认并**留底**；留不住底就立禁写闸。

    ★R285-N28（P3）：★R283 只修了写侧"不谎报已保存"，读侧仍是
    `except Exception: pass`——文件一旦损坏（半截写、被别的程序截断），内存回到
    **出厂默认键位**，下一次改键的 `save_config` 就把默认值整份原子覆盖到盘上，
    用户自定义的键位静默消失、零痕迹。现在与 core/category_manager、
    core/download_tracker、usage_stats（★R264 Z10）同口径：
      · 损坏原件先 `set_aside_corrupt` 另存留底 ⇒ 留住了就放行（覆盖可回退）；
      · 留不住底 ⇒ `_load_failed` 立起 ⇒ 本会话拒写（★R285-N7 同一把单向闩，
        后面重读成功也不复位——内存此刻是默认值，放闸等于拿默认值偷覆盖）；
      · **文件不存在=首启**，照常放行、不立闸（★R231：否则首启变成永久拒写）。
    """
    global _load_failed
    cfg = {k: list(v) for k, v in DEFAULT_HOTKEY_CONFIG.items()}
    p = config_path()
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding='utf-8'))
            if not isinstance(data, dict):
                raise ValueError('顶层不是对象，是 %s' % type(data).__name__)
            for k in cfg:
                if isinstance(data.get(k), list):
                    cfg[k] = data[k]
    except Exception as e:
        from utils.safeio import set_aside_corrupt
        kept, _detail = set_aside_corrupt(p)
        if not kept:
            _load_failed = True
        logger.error('hotkeys.json 损坏，原件留底%s: %s'
                     % ('成功' if kept else '同样失败（可能被占用）', e))
    return cfg


def save_config(gui):
    """把当前热键配置原子落盘。★R283：消费 atomic_write_json 返回值——
    写盘失败（文件被占用/只读）不再静默，落到日志页一行 ⚠，调用方据此
    不再谎报"已更新"（口径同 safeio 契约"返回值必须消费"、★R230b
    "except pass 是交付障碍"；本函数此前把 False 直接丢掉 = 改了键、重启却回默认）。

    ★R285-N28：本会话读不动 hotkeys.json 且原件留不住底 ⇒ **拒写**并给一行 ⚠——
    此时内存里是出厂默认键位，写下去就是拿默认值覆盖用户自定义。
    返回 True/False。"""
    if _load_failed:
        logger.error('快捷键配置未写盘：本会话读不动 hotkeys.json 且原件留不住底')
        try:
            gui._log('⌨️ ⚠ 快捷键配置未写入磁盘：本会话读不动 hotkeys.json'
                     '（原件也留不住底），为防用出厂默认键位覆盖你的自定义，已拒写'
                     '——重启程序后可恢复')
        except Exception:
            pass
        return False
    try:
        from utils.safeio import atomic_write_json
        ok = atomic_write_json(config_path(), gui._hotkey_cfg, indent=2)
    except Exception as e:
        logger.error(f'快捷键配置写盘异常: {e}')
        ok = False
    if not ok:
        logger.error('快捷键配置写入失败（文件可能被占用/只读），本次改动不会保留到下次启动')
        try:
            gui._log('⌨️ ⚠ 快捷键配置**未能写入磁盘**（文件被占用或只读？）：'
                     '本会话内已生效，但**重启后会回到改动前**——请关闭占用后重设')
        except Exception:
            pass
    return bool(ok)


# ---------------------------------------------------------------------
# ★R229（用户 01 批 item3）："只按 Alt"的组合体检与一键整改
# ---------------------------------------------------------------------

_ALT_KEYS = ('toggle_dock', 'show_all', 'quick_find', 'category')
_ALT_LABELS = {'toggle_dock': '悬浮窗开关', 'show_all': '显示全部',
               'quick_find': '快速定位', 'category': '切换分类 1~9'}
# 一次进程只提醒一次（apply_hotkeys 启动/改键都会走，别刷屏日志页）
_alt_warned = False


def alt_risky_bindings(cfg) -> list:
    """列出配置里"只按 Alt"的条目：[(配置键, 显示串)]；空表 = 无需整改。

    判据见 utils/hotkey_manager.binding_is_bare_alt（实测取证 forensic_keys.txt）。
    """
    out = []
    for k in _ALT_KEYS:
        parts = (cfg or {}).get(k) or []
        if parts and binding_is_bare_alt(parts):
            out.append((k, 'Alt+1~9' if k == 'category' else '+'.join(parts)))
    return out


# ---------------------------------------------------------------------
# 注册与轮询
# ---------------------------------------------------------------------

def apply_hotkeys(gui):
    """按当前配置（重）注册全部全局快捷键。

    ★ R124 修复"瞬态占用被永久写死"：旧版注册失败即清空单键配置、
    把失败数字永久记入 category_skip（只增不减、永不重试）——而冲突
    往往是瞬态的（另一份本程序副本未退完、某软件临时占用），配置却
    从此瘫痪（实锤：本机 hotkeys.json 曾变成 skip=[1..9] 且双键全空，
    用户报"快捷键不能切分类"）。现改为：每次启动全量重试；失败的
    只在日志与 category_skip 记"本次不可用"，用户配置永不被篡改。
    """
    global _alt_warned
    cfg = gui._hotkey_cfg
    old_skip = {int(d) for d in cfg.get('category_skip', [])
                if str(d).isdigit()}
    dirty = False

    # 一次性修复历史瘫痪指纹：双键全空 + 数字全放弃 = 当年被误清
    if not cfg.get('toggle_dock') and not cfg.get('show_all') \
            and len(old_skip) >= 9:
        cfg['toggle_dock'] = list(DEFAULT_HOTKEY_CONFIG['toggle_dock'])
        cfg['show_all'] = list(DEFAULT_HOTKEY_CONFIG['show_all'])
        cfg['category_skip'] = []
        old_skip = set()
        dirty = True
        gui._log('⌨️ 检测到旧版"被占用即永久放弃"留下的瘫痪配置，'
                 '已恢复默认快捷键（Ctrl+Alt+E 悬浮窗 / Ctrl+Alt+0 '
                 '显示全部 / Ctrl+Alt+1~9 切分类）')

    bindings = []

    mods, vk = parse_binding(cfg.get('toggle_dock', []))
    if mods and vk:
        bindings.append({'id': 1, 'mods': mods, 'vk': vk,
                         'payload': 'toggle_dock', 'label': '悬浮窗开关'})

    mods, vk = parse_binding(cfg.get('show_all', []))
    if mods and vk:
        bindings.append({'id': 2, 'mods': mods, 'vk': vk,
                         'payload': 'show_all', 'label': '显示全部'})

    mods, vk = parse_binding(cfg.get('quick_find', []))
    if mods and vk:
        bindings.append({'id': 3, 'mods': mods, 'vk': vk,
                         'payload': 'quick_find', 'label': '快速定位图标'})

    mods, _vk = parse_binding(cfg.get('category', []))
    if mods:
        for i in range(9):  # 数字 1-9 对应顶部槽位顺序（每次全量重试）
            bindings.append({'id': 10 + i, 'mods': mods, 'vk': ord('1') + i,
                             'payload': ('cat', i), 'label': f'切换分类{i + 1}'})

    gui.hotkey_mgr.start(bindings)

    # 本次注册失败的：只记日志（配置原样保留，下次启动自动重试）
    failed = gui.hotkey_mgr.failed
    dropped_digits = []
    for f in failed:
        combo = format_binding(f['mods'], f['vk'])
        if f['id'] == 1:
            gui._log(f'⌨️ [{combo}] 本次启动被其他程序占用，悬浮窗开关'
                     '热键暂不可用（配置已保留，下次启动自动重试；'
                     '长期冲突可在快捷键面板换键）')
        elif f['id'] == 2:
            gui._log(f'⌨️ [{combo}] 本次启动被其他程序占用，"显示全部"'
                     '热键暂不可用（配置已保留，下次启动自动重试）')
        elif isinstance(f['id'], int) and 10 <= f['id'] <= 18:
            dropped_digits.append(f['id'] - 9)
    new_skip = sorted(set(dropped_digits))
    if dropped_digits:
        slots = [s.name for s in gui.dock.slots
                 if s.name not in gui.dock.UTILITY_SLOTS]
        desc = '、'.join(
            f"Ctrl+Alt+{d}" if not slots else f"Ctrl+Alt+{d}({slots[d - 1]})"
            for d in dropped_digits if 0 < d <= len(slots))
        gui._log(f'⌨️ {desc} 本次被占用，这些分类热键暂不可用'
                 '（其余照常，下次启动自动重试）')
    if new_skip != sorted(old_skip):
        cfg['category_skip'] = new_skip
        dirty = True
    if dirty:
        save_config(gui)
        update_list(gui)
    # ★R229（用户 01 批 item3）：配置里仍有"只按 Alt"的组合 ⇒ 启动提醒一次。
    # 只提醒不擅改：R124 的教训是"程序自作主张篡改用户键位"会把面板搞瘫痪，
    # 整改做成面板上的一个按钮，由用户点。
    risky = alt_risky_bindings(cfg)
    if risky and not _alt_warned:
        _alt_warned = True
        gui._log('⌨️ 当前热键是「%s」这类"只按 Alt"组合：它会抢走其他软件的菜单'
                 '快捷键（记事本 Alt+E=编辑、Alt+F=文件），被吞的那一下还会让那边'
                 '停在 Alt 菜单态——表现就是"打字没反应、退格自动删词"。'
                 '改法：设置→⌨️ 全局快捷键→🔧 一键改用 Ctrl+Alt'
                 % '、'.join(lbl for _k, lbl in risky))


def start_poll(gui):
    """100ms 轮询热键事件队列（主线程执行回调，异常不能带崩主循环）"""
    # ★R221（NEW7-P0-6）：延迟导入，锁屏守卫要用 is_locked()
    from gui import lock_screen

    def poll():
        try:
            # ★R285-O2：热键线程死了 Windows 就不会再投 WM_HOTKEY，用户那边只
            # 表现为"按了没反应"，而重启程序才好——正是 R012 实测的会话内失效。
            # 这一行是看门狗：发现线程失联就按原绑定重注册，最多 3 次。
            rv = gui.hotkey_mgr.revive_if_dead()
            if rv == 'CAPPED':
                gui._log('⌨️ 全局热键自动恢复已达上限（3 次），本轮不再重试'
                         '——重启程序后可恢复')
            elif rv:
                n = getattr(gui.hotkey_mgr, '_revives', 1)
                # 原因只进 app.log（logger.error 那行带中文），日志页这行保持
                # 整句可译——★R230b：中文变量不许插进中英两份模板。
                gui._log(f'⌨️ ⚠ 全局热键曾失效，已自动恢复（第 {n} 次）')
            events = gui.hotkey_mgr.poll()
            # 全局热键由独立线程的低级钩子投递，锁屏画面上方照常触发。
            # 原来只有 show_all/cat 带家长闸，toggle_dock/quick_find 两道闸
            # 都没有 → 能在锁屏上拨出置顶窗、列出桌面图标，等于绕过锁屏。
            # 整批丢弃，一轮只提示一次（按住不放会连发，避免刷屏日志页）。
            if events and lock_screen.is_locked():
                gui._log('🔒 锁屏中，快捷键已忽略')
                events = ()
            for ev in events:
                p = ev['payload']
                if p == 'toggle_dock':
                    gui._toggle_dock()
                elif p == 'show_all':
                    if gui.parental.is_kids():
                        gui._log('🔒 家长模式中，"显示全部"已禁用')
                    else:
                        # ★ R136：与悬浮条 [全部] 完全同路（切槽位+套布局+
                        #   重算框）——原直调 _on_show_all 只恢复图标，
                        #   不切槽位不重算框 → 框留在上一分类（用户实测
                        #   "Ctrl+Alt+0 到全部了框不对"实锤）
                        if not gui._activate_slot_by_name('全部'):
                            gui._on_show_all()
                elif p == 'quick_find':
                    # ★R221（NEW7-P0-6）：家长模式进出都会关掉快速定位框
                    # （见 kids_panel），但热键是**唯一**入口且没有闸 →
                    # 孩子可按 Ctrl+Alt+F 重新唤出，列出并跳到被隐藏的图标。
                    if gui.parental.is_kids():
                        gui._log('🔒 家长模式中，"快速定位"已禁用')
                    else:
                        gui._open_quick_find()
                elif isinstance(p, tuple) and p[0] == 'cat':
                    idx = p[1]
                    # 家长模式：数字热键只允许 [小朋友]/[全部] 之外的分类不存在，
                    # 且绝不能触发 [退出]/[⏱]，直接整体禁用防绕过
                    if gui.dock.kids_mode:
                        gui._log('🔒 家长模式中，分类切换热键已禁用')
                        continue
                    # 只对分类槽位响应（跳过末尾工具槽 全部/窗/✕）
                    if idx < len(gui.dock.slots) and \
                            gui.dock.slots[idx].name not in gui.dock.UTILITY_SLOTS:
                        gui.dock._activate_slot(idx)
                    else:
                        gui._log(f'⚠ 快捷键：分类槽位 {idx + 1} 不存在')
        except Exception as e:
            logger.error(f"热键处理异常(已忽略): {e}")
        # after 挪进 try，退出瞬间 root 已销毁时不会抛未捕获 TclError
        try:
            gui.root.after(100, poll)
        except Exception as e:
            # ★R285-O2：这一条定时链一断，热键事件就永远没人取——正常退出时
            # 确实会走到这里（root 已销毁），但也可能是别的 TclError。留一行
            # warning 让两种情况能在 app.log 里分开，不再"静默失效"。
            logger.warning(f'热键轮询定时链结束（退出时属正常）: {type(e).__name__}: {e}')

    poll()


# ---------------------------------------------------------------------
# 面板
# ---------------------------------------------------------------------

def binding_str(gui, action: str) -> str:
    parts = gui._hotkey_cfg.get(action, [])
    if action == 'category':
        base = '+'.join(parts) + '+1..9' if parts else '未设置'
        skip = [int(d) for d in gui._hotkey_cfg.get('category_skip', [])
                if str(d).isdigit()]
        if skip:
            base += i18n.tr(f'（已放弃: {",".join(str(d) for d in sorted(skip))}）',
                            f' (abandoned: {",".join(str(d) for d in sorted(skip))})')
        return base
    return '+'.join(parts) if parts else i18n.tr('未设置（可双击设置）', 'Not set (double-click to set)')


def sync_hotkey_label(gui):
    """把设置页"热键:"标签刷成悬浮窗开/关的真实绑定（★R226 P2-2）。

    设置页未构建（无该 var）时静默返回——热键页与设置页同属一个主窗，
    两边谁后建都不需要对方先就位。
    """
    var = getattr(gui, 'hotkey_label_var', None)
    if var is None:
        return
    try:
        var.set(binding_str(gui, 'toggle_dock'))
    except Exception as e:
        logger.error(f"同步设置页热键标签失败: {e}")


def update_list(gui):
    # ★R226（P2-2）：设置页"热键:"一栏跟着真实绑定走（改键/恢复默认都要刷）
    sync_hotkey_label(gui)
    if not hasattr(gui, 'hotkey_list'):
        return
    gui.hotkey_list.delete(0, tk.END)
    gui.hotkey_list.insert(tk.END, f"{i18n.tr('悬浮窗 开/关', 'Floating Bar On/Off')}:        {binding_str(gui, 'toggle_dock')}")
    gui.hotkey_list.insert(tk.END, f"{i18n.tr('显示全部图标', 'Show All Icons')}:       {binding_str(gui, 'show_all')}")
    gui.hotkey_list.insert(tk.END, f"{i18n.tr('快速定位图标', 'Quick Find Icons')}:        {binding_str(gui, 'quick_find')}")
    dock = getattr(gui, 'dock', None)
    slots = [s.name for s in dock.slots
             if s.name not in dock.UTILITY_SLOTS][:9] if dock else []
    mapping = ' '.join(f'{i + 1}={n}' for i, n in enumerate(slots))
    gui.hotkey_list.insert(tk.END, f"{i18n.tr('切换分类(按顶部顺序)', 'Switch Category (top-bar order)')}: {binding_str(gui, 'category')}")
    gui.hotkey_list.insert(tk.END, f"  {i18n.tr('当前对应', 'Mapping')}: {mapping}")


def on_edit(gui):
    sel = gui.hotkey_list.curselection()
    actions = ['toggle_dock', 'show_all', 'quick_find', 'category']
    if not sel or sel[0] >= len(actions):
        messagebox.showwarning(
            i18n.tr('提示', 'Info'),
            i18n.tr('请先选中要修改的快捷键行（前 4 行，双击该行也可以）。',
                    'Select a hotkey row first (top 4 rows, or double-click it).'),
            parent=gui.root)
        return
    action = actions[sel[0]]
    current = format_binding(*parse_binding(gui._hotkey_cfg.get(action, [])))
    dlg = HotkeyCaptureDialog(gui.root, current,
                              mods_only=(action == 'category'))
    gui.root.wait_window(dlg)
    if not dlg.result:
        return
    mods, vk = dlg.result
    parts = [name for name, bit in MOD_NAMES if mods & bit]
    if vk and action != 'category':
        parts.append(vk_to_name(vk))
    gui._hotkey_cfg[action] = parts
    if action == 'category':
        # 换了修饰键组合后，之前因冲突放弃的数字键重新尝试注册
        gui._hotkey_cfg['category_skip'] = []
    _hk_ok = save_config(gui)
    apply_hotkeys(gui)
    update_list(gui)
    if _hk_ok:
        gui._log(f'⌨️ 快捷键已更新: {binding_str(gui, action)}')
    # ★ R129：新键被占即时弹窗反馈（R124 起配置不再被清空，只靠这里提示）
    _id_map = {'toggle_dock': 1, 'show_all': 2, 'quick_find': 3}
    failed_ids = {f['id'] for f in gui.hotkey_mgr.failed}
    hit = (_id_map.get(action) in failed_ids
           or (action == 'category'
               and any(isinstance(i, int) and 10 <= i <= 18
                       for i in failed_ids)))
    if hit:
        messagebox.showwarning(
            i18n.tr('快捷键被占用', 'Hotkey Taken'),
            i18n.tr('新快捷键已被其他程序占用，本次未生效。\n配置已保留'
                    '（下次启动自动重试），建议换一个组合。',
                    'The new hotkey is taken by another app and is not '
                    'active now.\nIt is kept and retried next start; '
                    'consider another combo.'),
            parent=gui.root)


def on_fix_alt_hotkeys(gui):
    """🔧 一键把"只按 Alt"的热键改成 Ctrl+Alt+同主键。

    ★R229（用户 01 批 item3）：裸 Alt 组合会抢走其他软件的菜单助记键，是
    "记事本打不进字/退格删词"的直接来源之一。改法**必须由用户点**——
    R124 的教训：程序自作主张篡改键位会把面板弄瘫（那次是注册失败即清空）。
    """
    risky = alt_risky_bindings(gui._hotkey_cfg)
    if not risky:
        gui._log('⌨️ 当前没有"只按 Alt"的热键组合，无需修改')
        return
    changed = []
    for k, lbl in risky:
        gui._hotkey_cfg[k] = to_ctrl_alt(gui._hotkey_cfg.get(k) or [])
        changed.append(f'{lbl} → {"+".join(gui._hotkey_cfg[k])}')
    # 换了修饰键 ⇒ 之前因冲突放弃的数字键重新全量试注册
    gui._hotkey_cfg['category_skip'] = []
    _hk_ok = save_config(gui)
    apply_hotkeys(gui)
    update_list(gui)
    btn = getattr(gui, '_fix_alt_btn', None)
    if btn is not None:
        try:
            btn.pack_forget()
        except Exception:
            pass
    if _hk_ok:
        gui._log('🔧 已改：%s。其他软件（记事本/浏览器/Office）的菜单快捷键不再被'
                 '抢走；想换别的键，双击面板里那一行即可' % '、'.join(changed))


def on_reset(gui):
    gui._hotkey_cfg = {k: list(v) for k, v in DEFAULT_HOTKEY_CONFIG.items()}
    _hk_ok = save_config(gui)
    apply_hotkeys(gui)
    update_list(gui)
    if _hk_ok:
        gui._log('⌨️ 已恢复默认快捷键方案')


def on_show_help():
    lines = ['【本软件默认方案】Ctrl+Alt+数字，数字键位在主流软件中最空闲：',
             '  · Ctrl+Alt+E 悬浮窗开关 / Ctrl+Alt+0 显示全部 / Ctrl+Alt+1~9 切分类', '',
             '【强烈建议避开】']
    for _tag, desc in CONFLICT_RULES:
        lines.append(f'  ✗ {desc}')
    lines += ['', '【小提示】若注册后提示"被其他程序占用"，说明该组合已被',
              '某个后台软件注册（如截图工具、输入法），换一个即可。']
    messagebox.showinfo('快捷键冲突提醒', '\n'.join(lines))


def build_card(parent, gui, colors):
    """构建"⌨️ 全局快捷键"卡片（设置页）。返回 frame"""
    set_colors(colors)
    frame = ttk.LabelFrame(parent, text=i18n.tr('⌨️ 全局快捷键', '⌨️ Global Hotkeys'),
                           padding='10', style='Card.TLabelframe')
    frame.pack(fill=tk.X)

    gui.hotkey_list = tk.Listbox(frame, height=5,
                                 font=('Consolas', 10),
                                 bg=colors['surface'],
                                 fg=colors['fg'],
                                 selectmode=tk.SINGLE,
                                 relief=tk.FLAT,
                                 highlightthickness=0)
    gui.hotkey_list.pack(fill=tk.X, padx=5, pady=(0, 5))
    gui.hotkey_list.bind('<Double-Button-1>', lambda e: on_edit(gui))

    btns = ttk.Frame(frame)
    btns.pack(fill=tk.X)
    # ★R216 阶段 2b：三个按钮走 action_bar 等宽分列（原先 padx=2 的手工作坊排
    # 让长短不一的三枚按钮边缘对不齐），说明文字下移一行、走 text_secondary
    ui_layout.action_bar(btns, [
        (i18n.tr('✏️ 修改选中（双击也可）', '✏️ Edit Selected (or double-click)'),
         lambda: on_edit(gui), 'primary.TButton'),
        (i18n.tr('🔄 恢复默认', '🔄 Reset Defaults'), lambda: on_reset(gui),
         'secondary.TButton'),
        (i18n.tr('⚠️ 冲突提醒', '⚠️ Conflict Warnings'), on_show_help,
         'secondary.TButton')],
        cols=3, pad=SP['s'])
    ttk.Label(btns, text=i18n.tr('默认方案: Ctrl+Alt+数字（主流软件最空闲的组合）',
                                 'Default: Ctrl+Alt+number (least conflicting combo)'),
              foreground=colors['text_secondary'], wraplength=980, justify='left',
              font=ui_layout.app_font(9)).pack(anchor=tk.W, pady=(SP['xs'], 0))
    # ★R229（用户 01 批 item3）：配置里有"只按 Alt"的组合时给一键整改入口。
    # 只在真有风险时出现（否则白占一行、也不知道按了干什么）；改完就地收起。
    gui._fix_alt_btn = None
    if alt_risky_bindings(gui._hotkey_cfg):
        gui._fix_alt_btn = ttk.Button(
            btns, text=i18n.tr('🔧 一键改用 Ctrl+Alt',
                               '🔧 Use Ctrl+Alt instead'),
            style='secondary.TButton',
            command=lambda: on_fix_alt_hotkeys(gui))
        gui._fix_alt_btn.pack(anchor=tk.W, pady=(SP['s'], 0))
        ttk.Label(btns, text=i18n.tr(
            '现在的 Alt+… 会抢走其他软件的菜单快捷键（记事本 Alt+E＝编辑），'
            '点上面按钮换成 Ctrl+Alt 版即可。',
            'Alt+… currently steals other apps\' menu keys (Notepad Alt+E = Edit).'),
            foreground=colors['text_secondary'], wraplength=980, justify='left',
            font=ui_layout.app_font(9)).pack(anchor=tk.W, pady=(SP['xs'], 0))
    update_list(gui)
    return frame


class HotkeyCaptureDialog(tk.Toplevel):
    """捕获用户按下的快捷键组合"""

    MOD_KEYSYMS = {'control_l', 'control_r', 'alt_l', 'alt_r',
                   'shift_l', 'shift_r', 'win_l', 'win_r',
                   'meta_l', 'meta_r'}

    def __init__(self, parent, current: str, mods_only: bool = False):
        super().__init__(parent)
        self.result = None       # (mods, vk) 或 None
        self.mods_only = mods_only
        self.title('修改快捷键')
        self.geometry('400x180')   # ★ R142：加大——"切换分类"方案的提示文本曾被窗口挡住
        # ★ P2-N14：跟随主题色（_colors 为主窗 COLORS 的引用）
        self.configure(bg=_colors['bg'])
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        hint = ('请按住修饰键（当前修改方案只需按修饰键，数字自动加）'
                if mods_only else '请按下新的快捷键组合')
        tk.Label(self, text=f'当前: {current or "未设置"}',
                 fg=_colors['fg'], bg=_colors['bg'],
                 font=ui_layout.app_font(10)).pack(pady=(18, 4))
        self.wait_var = tk.StringVar(value=f'⌨️ {hint}…（Esc 取消）')
        tk.Label(self, textvariable=self.wait_var,
                 fg=_colors['primary'], bg=_colors['bg'],
                 wraplength=370, justify='left',
                 font=ui_layout.app_font(10, 'bold')).pack(pady=8, padx=8)

        self.bind('<KeyPress>', self._on_key)
        self.focus_force()

    def _on_key(self, event):
        keysym = (event.keysym or '').lower()
        if keysym == 'escape':
            self.destroy()
            return
        if keysym in self.MOD_KEYSYMS:
            self.wait_var.set(f'已按住修饰键，再按下主键…（{keysym}）')
            return

        # tkinter state 掩码：Control=0x4, Shift=0x1, Alt=0x20000
        # ★ P2-N17：删 0x0008 兜底——在部分 Tk 环境它是 Mod1/Meta，
        #   与 Alt 混用会导致 Shift+数字 也被误判为带 Alt。只保留 0x20000
        #   （Win11 实测 Alt 在 state 里稳定为 0x20000）。
        mods = 0
        if event.state & 0x0004:
            mods |= 0x0002  # MOD_CONTROL
        if event.state & 0x0001:
            mods |= 0x0004  # MOD_SHIFT
        if event.state & 0x20000:
            mods |= 0x0001  # MOD_ALT

        vk = name_to_vk(event.keysym)
        if vk is None:
            self.wait_var.set(f'✗ 不支持按键 "{event.keysym}"，请换一个（Esc 取消）')
            return
        if self.mods_only:
            if not mods:
                self.wait_var.set('✗ 请至少按一个修饰键（Ctrl/Alt/Shift），Esc 取消')
                return
            # ★R229（用户 01 批 item3）：分类方案只录修饰键，check_conflicts
            # 走不到（没有主键），"只按 Alt"必须在这里单独拦一道——Alt+1~9
            # 抢的是所有软件的 Alt+数字 菜单键。
            if mods == 0x0001:   # 只有 MOD_ALT
                if messagebox.askyesno(
                        '检测到潜在冲突',
                        '只按 Alt（=Alt+1~9）会抢走所有软件的 Alt+数字 菜单键'
                        '（浏览器、Office、输入法都在用），被吞的那一下还会让目标'
                        '窗口停在 Alt 菜单态，出现"打字没反应、退格删词"。\n\n'
                        '改用 Ctrl+Alt 吗？（选"否"仍按 Alt 生效）',
                        parent=self):
                    mods |= 0x0002   # 叠上 MOD_CONTROL
            self.result = (mods, 0)
            self.destroy()
            return
        if not mods or not vk:
            self.wait_var.set('✗ 需要修饰键+主键组合（如 Ctrl+Alt+1），Esc 取消')
            return

        # 冲突提醒（允许强制使用）
        warnings = check_conflicts(mods, vk)
        if warnings:
            text = format_binding(mods, vk) + '\n\n⚠️ ' + '\n⚠️ '.join(warnings) + \
                   '\n\n仍要使用吗？'
            if not messagebox.askyesno('检测到潜在冲突', text, parent=self):
                self.wait_var.set('已取消，请再按其他组合（Esc 退出）')
                return
        self.result = (mods, vk)
        self.destroy()
