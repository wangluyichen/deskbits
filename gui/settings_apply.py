# -*- coding: utf-8 -*-
"""★R240（#137/#138，用户 32 批 item2）：导入全部数据后，把新设置**推给运行中的对象**。

问题定性：「导出前统计功能是开的，导回来变成关的」「榜单字号也没跟包走」——
根因**不在导出**：`app_settings` 是整包导出的（只剔 `_` 前缀私有字段），导入也
整包落盘（`data_manage_page._import` 的 设置 节）。缺的是**落盘之后没人把新值
推给运行时**：
  · `StatsCaptureManager.enabled` 只在构造时读一次，`apply_startup()` 只在启动调一次；
  · `_StatsPanel._font_size` 只在面板构造时读一次；
  · 界面主题/字号/密度/图标风格/悬浮窗置顶各有自己的应用路径，导入都没走到。
所以这里把"导入后该热切的项"收在一处，导入分支只调 `apply_runtime(gui)` 一个入口。

三条纪律：
  1. **只读不回写**：不改 `_app_settings`、不调 `_save_app_settings()`——导入端
     已经落过盘，这里再存一遍会把导入侧的校验与回退（R71 退出恢复目标校验等）绕开；
  2. **每项独立 try**：一项应用失败不能让导入主流程中断，其余照常生效，失败项进日志；
  3. **不假装即时**：Tk 文案是构建期求值，语言/密度这类要重建整窗的项**明确归到
     "重启后生效"**（与 R73 语言切换、R225 显示档的同一口径），只把全局档
     就位并同步设置页控件的显示值——不同步控件会有更坏的后果：用户导入后点一下
     "✅ 应用"，控件里的旧值会把刚导入的设置覆盖回去（R229 sticky 网格踩过）。

★第 4 条是 `test_r233_import_safety` 的 R7/M4 + D5 三条判据把我第一版打回红之后补的：
  **包里没带的键一律不推**。导入端对 `app_settings` 是 `update()`（合并）而不是整表
  替换，包里没有 `ui_language` 时 `s.get('ui_language', '中文')` 会拿到**我写死的默认值**
  去 `i18n.set_language('中文')`——把用户当前的英文档强行改回中文，导入流程后半段所有
  弹窗（回滚框/完成框）当场露中文。写死默认值在这里不是"兜底"，是**替用户改设置**：
  包没说的事情，运行态就不该动。所以下面每一项都先判 `key in s`，不带就不推、
  也不进"已即时生效"清单（诚实记账）。

★R249（用户 26 批 02 补充 1「规则里面的设置不能导入导出，比如设置方框哪些」）
在模块尾部另起两条**不走 `_PUSHES`** 的入口——`apply_highlight(gui)`（规则页
高亮框选的数据在 `config/highlight.json`，压根不在 `app_settings` 里）和
`apply_dimmer(gui, 包里那份)`（桌面亮度要按**包体**判"带没带"，合并后的
`_app_settings` 判不出来）。两条的完整病根与判据见各自 docstring 和
`测试_ui/test_r249_rules_import_apply.py`。
"""
import logging

logger = logging.getLogger("desktop_manager")


def _push_stats(gui, s):
    """统计功能开关 + 统计页榜单字号（★R240 #137 本体）

    两项独立：包里只带其中一项时也只推那一项（别把另一项按默认值盖掉）。"""
    sc = getattr(gui, 'stats_capture', None)
    sp = getattr(gui, 'stats_panel', None)
    done = []
    if sc is not None and 'stats_capture_enabled' in s:
        if sc.apply_settings():
            done.append('统计开关')
        else:
            # 采集线程起不动（★ R256 去钩子化后已无"缺库"分支）：不谎报
            # "采集中"，如实提示
            gui._log('⚠ 导入的统计开关未能生效：鼠标采集线程启动失败'
                     '（重启程序再试，📊 统计页也可手动打开）')
        if sp is not None:
            sp.update_toggle_ui(sc.enabled)   # 按钮文案跟着真实状态走
    if sp is not None and 'stats_font_size' in s:
        sp.reload_settings()
        done.append('榜单字号')
    if not done:
        return None, False
    return '、'.join(done), False


def _push_theme(gui, s):
    """界面主题：走既有热切路径 `_apply_theme_live`（换肤不需重启）"""
    if 'ui_theme' not in s:
        return None, False
    theme = s['ui_theme']
    apply_live = getattr(gui, '_apply_theme_live', None)
    if not callable(apply_live):
        return None, False
    var = getattr(gui, 'ui_theme_var', None)
    if var is not None:
        from gui.theme_panel import _theme_disp
        var.set(_theme_disp(theme))       # 控件显示值同步（防"应用"回写旧值）
    apply_live(theme)
    return '界面主题', False


def _push_ui_font(gui, s):
    """界面字号：命名字体别名原地重配 ⇒ 全场实时（R228 口径）"""
    if 'ui_font_size' not in s:
        return None, False
    try:
        px = int(s['ui_font_size'])
    except Exception:
        # 包里是脏值（字符串/None）：宁可不推，也不拿默认字号盖掉用户现值
        logger.warning('导入的 ui_font_size 不是整数，跳过字号应用: %r',
                       s.get('ui_font_size'))
        return None, False
    from gui import ui_layout
    from gui.settings_panel import UI_FONT_SIZES, DEFAULT_UI_FONT, fd_disp
    ui_layout.set_ui_font_base(px)
    font = getattr(gui, 'ui_font', None)
    if font is not None:
        try:
            font.configure(size=px)
        except Exception:
            pass
    var = getattr(gui, 'ui_fontsize_var', None)
    if var is not None:
        canon = next((k for k, v in UI_FONT_SIZES.items() if v == px),
                     DEFAULT_UI_FONT)
        var.set(fd_disp(canon))
    reposition = getattr(gui, '_reposition_window', None)
    if callable(reposition):
        try:
            reposition()
        except Exception as e:
            logger.warning(f'导入后重算窗口尺寸失败: {e}')
    return '界面字号', False


def _push_display_prefs(gui, s):
    """界面密度 / 图标风格：全局档立即就位，已建好的主窗要重启才整窗生效（R225）"""
    has_d = 'ui_density' in s
    has_i = 'ui_icons' in s
    if not (has_d or has_i):
        return None, False
    from gui.settings_panel import dp_disp, ic_disp
    var_d = getattr(gui, 'ui_density_var', None)
    if has_d and var_d is not None:
        var_d.set(dp_disp(s['ui_density']))
    var_i = getattr(gui, 'ui_icons_var', None)
    if has_i and var_i is not None:
        var_i.set(ic_disp(s['ui_icons']))
    apply_prefs = getattr(gui, '_apply_display_prefs', None)
    if not callable(apply_prefs):
        return None, False
    apply_prefs()          # 该方法自己读 _app_settings（已合并），不接参数
    return '界面密度/图标风格', True


def _push_language(gui, s):
    """界面语言：Tk 文案构建期求值，热切做不到（R73 定案）⇒ 只切全局语言档 + 重启生效

    ★这条是 R7/M4 打回红的正主：没有 `ui_language` 键时**绝不能**碰
    `i18n.set_language`——把运行中的语言档改掉，会让本次导入流程后半段的
    所有弹窗当场换语言。"""
    if 'ui_language' not in s:
        return None, False
    from utils import i18n
    lang = s['ui_language']
    var = getattr(gui, 'ui_lang_var', None)
    if var is not None:
        try:
            var.set(lang)
        except Exception:
            pass
    i18n.set_language(lang)
    return '界面语言', True


def _push_dock_topmost(gui, s):
    """悬浮窗置顶：dock 有实时接口（R226 P2-1 同源），不需要重启"""
    if 'dock_topmost' not in s:
        return None, False
    on = bool(s['dock_topmost'])
    var = getattr(gui, 'dock_topmost_var', None)
    if var is not None:
        try:
            var.set(on)
        except Exception:
            pass
    dock = getattr(gui, 'dock', None)
    if dock is None or not hasattr(dock, 'set_always_on_top'):
        return None, False
    dock.set_always_on_top(on)
    return '悬浮窗置顶', False


def _push_verbose_log(gui, s):
    """排障「全量日志」勾选：运行态 + 界面勾选框两处一起推（★R250 一5⑤）

    用户 42 批 ⑤：「再去看全量日志居然没勾上，之前导出的时候是勾上的」。
    这一项**不在**导出端丢（`verbose_log` 随 `app_settings` 整包走），
    是 ★R240 那一族"落盘之后没人应用"的另一枚：级别没人切、勾选框没人回填。
    复用 `log_page.apply_verbose_log`（与勾选框自己同一条路，不造第二套语义），
    `persist=False`：导入端已经存过盘，这里再存会绕开导入侧校验。"""
    if 'verbose_log' not in s:
        return None, False          # ★R240 纪律 4：包没说话 ⇒ 运行态不动
    from gui import log_page
    log_page.apply_verbose_log(gui, on=bool(s['verbose_log']),
                               persist=False, notify=False)
    return '全量日志（排障）', False


# 顺序＝重要性：统计（用户点名）→ 主题 → 字号 → 密度/图标 → 语言 → 置顶
# → 全量日志（★R250 一5⑤：排障证据链的开关，放在最后一条不影响判读）
_PUSHES = (_push_stats, _push_theme, _push_ui_font, _push_display_prefs,
           _push_language, _push_dock_topmost, _push_verbose_log)


def apply_runtime(gui):
    """把 `_app_settings` 现值推给运行中的对象。

    返回 `(即时生效项, 重启后生效项)` 两个中文名列表，供导入侧写日志/完成提示。
    单项异常只进日志（`logger.warning`），不向上抛——导入主流程必须继续。"""
    s = getattr(gui, '_app_settings', None)
    if not isinstance(s, dict):
        return [], []
    now, later = [], []
    for push in _PUSHES:
        try:
            label, deferred = push(gui, s)
        except Exception as e:
            logger.warning(f'导入后应用设置失败（{push.__name__}，重启后正常）: {e}')
            continue
        if not label:
            continue
        (later if deferred else now).append(label)
    return now, later


# =====================================================================
# ★R249（用户 26 批 02 补充 1「规则里面的设置不能导入导出，比如设置方框哪些」）
# 规则页的两样：高亮框选（另存 highlight.json）/ 桌面亮度（在 app_settings 里）
# =====================================================================

def apply_highlight(gui):
    """导入全部数据后，把刚落盘的 highlight.json 应用到运行中的对象。

    ★R249 补充1/D1：导入端的高亮节此前**只写文件**，既不走启动那条
    `_load_highlight_settings()`（gui/main_window.py:202 与 :321），也不叫
    `dock.recompute_highlight_only()` ⇒
      · 屏幕上那些"设置方框"勾选框还是旧状态、桌面上的框也没变，
        用户自然读成"规则里的设置没导进来"（本轮报告里说的"看不见"）；
      · 真正的丢数据面：运行中的 highlighter 仍是旧值，用户随手再点一下
        任一框，`_save_highlight_settings()`（gui/rule_panel.py:505）就把
        **旧内存**原样写回盘 ⇒ 刚导进来的高亮被静默抹掉。
    与 ★R233 R4「回滚后内存要跟着回灌」是同一病根，修法也同口径：
    **复用既有启动路径**，不新造一套应用逻辑——`_load_highlight_settings`
    自己会读文件、推给 highlighter、并回填规则页控件的显示值（不同步控件
    会有更坏的后果，见本模块开头第 3 条纪律）。
    调用方只在**写盘成功**后才调它：写失败时磁盘与内存都还是旧的，
    只把内存推新会造出"界面显示新值、磁盘还是旧值"的假生效。
    返回 True=已应用，False=这台没有可应用的对象（规则页方法缺失等）。
    """
    load = getattr(gui, '_load_highlight_settings', None)
    if not callable(load):
        logger.warning('导入后应用高亮设置跳过：主窗没有 _load_highlight_settings')
        return False
    load()
    dock = getattr(gui, 'dock', None)
    if dock is not None and hasattr(dock, 'recompute_highlight_only'):
        try:
            dock.recompute_highlight_only()   # 只重算框选，不重排图标（R65-Bug5）
        except Exception as e:
            logger.warning(f'导入后重算高亮框失败（重启后正常）: {e}')
    return True


def apply_dimmer(gui, _pkg):
    """按**导入包里那一份**应用桌面亮度（★R249 补充1/D2）

    为什么不并进 `_PUSHES`/`apply_runtime`：那里拿到的是 `update()` 之后的
    `_app_settings`，分不清"包带来的"和"本机原有的"。★R240 第 4 条纪律
    （包里没带的键一律不推）在这一路上得按包体原样判，所以导入端把包里那份
    `incoming` 显式传进来——包里没提亮度 ⇒ 蒙版一次都不碰。

    两件事都得做，只做第一件照样丢数据：
      1) 蒙版重铺：取值口径与**启动完全一致**（gui/main_window.py:253~264——
         scope 先照搬，`desktop_brightness` 只在"全部显示器"档当 fallback），
         免得导入和启动两套语义各说各话；
      2) 规则页回填：`_dim_levels` 是**构建期拷贝**（gui/rule_panel.py:833），
         不原地更新的话，用户下次拖滑条时 `_on_dim_change` 会拿这份陈旧拷贝
         写回 `dim_scope_levels`（gui/rule_panel.py:133）⇒ 刚导入的分屏记忆
         整份被盖掉。滑条数字与作用显示器下拉也要跟着回填（:96 那个刷新）。
    """
    if not isinstance(_pkg, dict):
        return False
    _keys = ('dim_scope_levels', 'desktop_brightness', 'desktop_hw_scope')
    if not any(k in _pkg for k in _keys):
        return False          # 包没说话 ⇒ 运行态不动（★R240 第 4 条纪律）
    d = getattr(gui, 'dimmer', None)
    if d is None or not hasattr(d, 'restore_levels'):
        logger.warning('导入后应用桌面亮度跳过：没有可用的 dimmer')
        return False
    if 'desktop_hw_scope' in _pkg:
        d.scope = _pkg['desktop_hw_scope']
    _lv = _pkg.get('dim_scope_levels')
    _lv = _lv if isinstance(_lv, dict) else {}
    try:
        _fb = int(_pkg.get('desktop_brightness', 100)) \
            if getattr(d, 'scope', None) is None else 100
    except (TypeError, ValueError):
        _fb = 100
    d.restore_levels(_lv, fallback=_fb)
    # 回填规则页：原地改这份 dict（滑条回写用的就是它，换对象等于没回填）
    _mem = getattr(gui, '_dim_levels', None)
    if isinstance(_mem, dict):
        _mem.clear()
        _mem.update(_lv)
        _key = 'all' if getattr(d, 'scope', None) is None else str(d.scope)
        var = getattr(gui, 'dim_var', None)
        if var is not None:
            try:
                var.set(int(_lv.get(_key, _fb)))
            except (TypeError, ValueError):
                pass
    ref = getattr(gui, '_dim_scope_refresh', None)
    if callable(ref):
        try:
            ref()
        except Exception as e:
            logger.warning(f'导入后回填亮度作用显示器失败: {e}')
    return True
