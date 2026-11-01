# -*- coding: utf-8 -*-
"""★R225 界面美化阶段 5：图标字体层（Segoe MDL2 Assets ↔ Emoji 两档）。

★R229（用户 01 批 item5）：作用域从"只有页签"扩到**页签 + 标题栏控件簇**
（应用名 🖥️ / 语言 🌐 / 锁屏 🔒 / 悬浮窗 📌）——设置项叫"图标风格"却只管
页签，切到单色 MDL2 后标题栏仍是彩色 emoji，用户读作"设置没生效"。
悬浮条（core/dock.py）仍**不在**作用域内，理由见下面硬约束 3。

为什么单独一层：页签文字历史上是 `f"📁 {tr('管理','Files')}"` 手写的，emoji 在
Win10 上渲染成彩色位图、和 9pt 中文字基线不齐（"看着像贴上去的"）。这一层把
"图标字符"与"文字词"解耦，图标可整体切换、词保持原样。

三条硬约束（都是历轮踩过的坑，别绕）：
  1. **词是逻辑 ID**：gui/wizard.py::_goto 用词子串匹配页签、_on_tab_changed
     双语恢复、多处测试按词断言——只换图标字符，词一个字都不动。
  2. **比较串必须同源**：统计页两处 `== i18n.tr('📊 统计', …)` 精确比较改走
     `self._tab_text('stats', …)`；stats_panel 用同一 `tab_text`。否则换图标档
     后"切到统计页刷新榜单"静默失效（R73 双语标签同款坑）。
     ★R230 补：同源还不够，得**跨档**也同源——图标档是即时生效、页签文字是
     重启才重排，所以比较一律走 `is_tab_text()`（两档都认），不再现算 tab_text。
  3. **只作用于 Tk 页签**：dock 悬浮条槽位名是逻辑 ID 且 `_wndproc` 是纯 GDI
     （字体对象固定，调 Tcl = 静默 native abort），图标风格不覆盖悬浮条。

MODE 是模块级状态（与 theme_panel.COLORS / drag_drop.COLORS 同一套"真身 +
set_xxx 注入"的既有约定）：main_window 启动时 `_apply_display_prefs` 落一次，
之后各处 `tab_text(key, label)` 不再传档，保证比较串天然一致。
"""

# 十枚 MDL2 码位对照 Microsoft 官方 "Segoe MDL2 Assets icons" 字形表选取，
# 全部落在私用区 E000–F8FF（Win10 自带该字体；Tk/GDI 对字符串里的 PUA 字符
# 逐字符回退，中英混排实测无豆腐，见 2026-09-23 探针截图）。
MDL2 = {
    'manage':    '\ue8b7',   # Folder          📁
    'rules':     '\ue71c',   # Filter          🎛️
    'settings':  '\ue713',   # Setting         🧩
    'resources': '\ue8f1',   # Library         📂
    'parental':  '\ue77b',   # Contact         👶
    'system':    '\ue90f',   # Repair          🧰
    'logs':      '\ue8fd',   # BulletedList    📜
    'stats':     '\ue9d2',   # AreaChart       📊
    'data':      '\ue74e',   # Save            📦
    'about':     '\ue946',   # Info            ℹ️
    # ★R229（用户 01 批 item5）：标题栏控件簇也吃这一档。原先"图标风格"只管
    # 页签，选了单色 MDL2 后标题栏那几枚彩色 emoji 仍是彩的——同一窗口两种
    # 图标语言，用户读作"设置没生效"。四个码位逐个截图核过（见工作进度
    # ★R229 一/二节），不是猜的：E7F4 显示器 / E72E 锁 / E718 图钉 / E774 地球。
    'app':       '\ue7f4',   # Desktop         🖥️
    'lock':      '\ue72e',   # Lock            🔒
    'dock':      '\ue718',   # Pinned          📌
    'lang':      '\ue774',   # Globe           🌐
}

# 现状档（原 emoji 一字不动）：既是用户可切的回退档，也让"没装 MDL2 字体的
# 机器"有退路。
EMOJI = {
    'manage': '📁', 'rules': '🎛️', 'settings': '🧩', 'resources': '📂',
    'parental': '👶', 'system': '🧰', 'logs': '📜', 'stats': '📊',
    'data': '📦', 'about': 'ℹ️',
    'app': '🖥️', 'lock': '🔒', 'dock': '📌', 'lang': '🌐',
}

MODES = ('mdl2', 'emoji')
DEFAULT_MODE = 'mdl2'

MODE = DEFAULT_MODE


def set_mode(mode):
    """落图标档；非法值退回默认档（app_settings 被手工改坏时不白屏）。"""
    global MODE
    MODE = mode if mode in MODES else DEFAULT_MODE
    return MODE


def get_mode():
    return MODE


def glyph(key, mode=None):
    """取某页的图标字符；未知页返回空串（新页签忘了登记也不会崩）。"""
    table = EMOJI if (mode or MODE) == 'emoji' else MDL2
    return table.get(key, '')


def tab_text(key, label, mode=None):
    """页签文字 = 图标 + 空格 + 已翻译的词（宽度由 TNotebook.Tab padding 给，
    R215 口径，不再手工加前后空格）。"""
    g = glyph(key, mode)
    return f'{g} {label}' if g else label


def is_tab_text(text, key, label):
    """判断一段页签文字属不属于某页——**忽略图标档**。

    ★R230（第 13 批顺手修）：模块头约束 2 说的是"比较串别再写死 emoji 字面，
    要走 tab_text"。但走 tab_text 只挡住了"跨语言/跨版本"，没挡住**跨档**：
    设置页改"图标风格"会当场 `set_mode`（gui/_apply_display_prefs），而已建好的
    主窗页签文字是构建期求值、要重启才重排（settings_panel 明写在弹窗里）。
    于是"重启弹窗选 No"之后：页签上仍是旧档图标，`tab_text()` 现算的是新档图标
    ⇒ 统计页那两处精确比较恒 False ⇒ "切到统计页刷新榜单 / 新纪录到达刷新"
    静默失效，直到下次重启才恢复。用户读作"统计页不刷新了"。

    口径：把两档 + 无图标三种形态都算出来比对，任何一侧是哪档都不再脱钩。
    词本身仍由调用方负责翻译（R73 双语标签口径不变）。
    """
    t = (text or '').strip()
    if not t:
        return False
    return t in {label, tab_text(key, label, 'mdl2'), tab_text(key, label, 'emoji')}
