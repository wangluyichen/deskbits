# -*- coding: utf-8 -*-
"""🏆 排行榜显示（R106 V1 简化版）——按统计榜显示排名区间的图标，其余隐藏。

失败前作教训（用户 2026-09-17 定案"一步一步来，先简单实现"）：R50/R51
智能筛选一口气做 双栏+预设+预览+定时收敛+排行榜分类生成+按天统计升级
（净增 ~400 行、替换整个规则区），失败归档于
代码存档/R51智能筛选材料_20260915/。本版砍到最小：

· 榜源二选一：图标打开榜（usage_stats.json 的 icon_opens）/ 分类切换榜
  （clicks.json，剔除"全部"工具槽）；
· 两行输入共用一个执行按钮：行1=显示前 N 名（填了优先生效），
  行2=显示第 M~N 名；整数校验，M≤N；
· 撤销="⏹ 恢复全部"按钮（R108 自退役的显示规则区迁入，同一
  _on_rule_restore_all 路径，悬浮条[全部]也可）；
· 统计提醒双层：常驻小字 + 应用时统计关闭弹窗确认（R30/R31 模板）。

不做（留给迭代）：预设、预览行、定时收敛、排行榜分类生成、按天统计。
参考：Nova Launcher 按使用频率排序抽屉 / 腾讯桌面整理分区（R50 设计稿
附注）；显示机制复用 icon_manager.apply_visibility（R49 规则同款）。
"""
import json
import logging

import tkinter as tk
from tkinter import ttk
from tkinter import messagebox

from utils.app_paths import get_app_dir
from gui import ui_layout
from gui.ui_layout import SP
from pathlib import Path

logger = logging.getLogger("desktop_manager")


# ---------------------------------------------------------------------
# 榜源读取
# ---------------------------------------------------------------------

def _load_icon_opens(gui=None) -> dict:
    """图标打开榜 → {'图标名': 次数}。

    ★R287-N11：优先读**内存活账**（gui.usage_stats）。旧写法只读
    usage_stats.json，而点击热路径有 15 秒合并写窗口（★R270-R3）——统计页开着、
    刚双击完图标就 15 秒内到规则页点"▶ 应用显示"或建组，两榜会各数一份，
    边界名次互换。活账是文件的上游，读它就没有时差；没有活账（gui 未注入/
    读失败）才退回读文件。"""
    us = getattr(gui, 'usage_stats', None) if gui is not None else None
    if us is not None:
        try:
            with us._lock:
                opens = dict(us.icon_opens)
            out = {}
            for name, info in opens.items():
                out[name] = (int(info.get('count', 0)) if isinstance(info, dict)
                             else int(info))
            return out
        except Exception as e:
            logger.warning(f"[排行榜] 读内存活账失败，退回读文件: {e}")
    # 榜源兜底：usage_stats.json → {'图标名': {'count': n, ...}}
    try:
        p = Path(get_app_dir()) / 'data' / 'usage_stats.json'
        with open(p, encoding='utf-8') as f:
            j = json.load(f)
        opens = j.get('icon_opens', {}) if isinstance(j, dict) else {}
        out = {}
        for name, info in opens.items():
            if isinstance(info, dict):
                out[name] = int(info.get('count', 0))
            else:
                out[name] = int(info)   # 兼容旧结构（直接是数字）
        return out
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.error(f"[排行榜] 读取图标打开榜失败: {e}")
        return {}


def _load_category_switches(gui) -> dict:
    """分类切换榜：clicks.json（悬浮条分类使用频率），剔除'全部'工具槽"""
    try:
        counts = dict(gui.click_tracker.get_all_clicks() or {})
    except Exception as e:
        logger.error(f"[排行榜] 读取分类切换榜失败: {e}")
        return {}
    counts.pop('全部', None)
    return counts


def _ui_log(gui, msg):
    """往主窗日志栏写一行。控件没了（主窗已关/未建）不该把整条操作带崩，
    ★R287-N14：但兜底可以吞异常，静默不行（★R230b 口径）——留一行才能复盘。"""
    try:
        gui._log(msg)
    except Exception as e:
        logger.warning(f"[排行榜] 日志栏写入失败（操作本身已生效）: {e}")


def _ranked(desc_dict: dict):
    """{名字: 次数} → [(名字, 次数)] 降序；同次数按名字序（稳定可复现）"""
    return sorted(desc_dict.items(), key=lambda x: (-x[1], x[0]))


# ---------------------------------------------------------------------
# 选取逻辑（纯函数，沙箱可测）
# ---------------------------------------------------------------------

def pick_range(ranked, top_n=None, m=None, n=None):
    """按两行参数从榜单 [(名字, 次数)] 选取区间。

    ★ R114 定案（用户）：不搞并列，严格按排行榜位置——名次=排序后的
    位置，同次数按名字先后排定（结果每次一致、名次永远可区分）。
    M=6、N=6 即只显示排行榜第 6 位的图标。

    行1（前 top_n）填了优先生效；行2取第 M~N 位（1 基，含端点）。
    返回 (selected_names, 描述文本)；参数不合法抛 ValueError。"""
    from utils import i18n
    if not ranked:
        raise ValueError(i18n.tr('榜单为空', 'Ranking is empty'))
    if top_n is not None:
        if top_n < 1:
            raise ValueError(i18n.tr('N 必须是正整数', 'N must be a positive integer'))
        sel = ranked[:top_n]
        return [x[0] for x in sel], f'前 {top_n} 名'
    if (m is None) != (n is None):
        raise ValueError(i18n.tr('第二行的 M 和 N 要成对填写',
                                 'M and N in row 2 must be filled together'))
    if m is not None and n is not None:
        if m < 1 or n < m:
            raise ValueError(i18n.tr('要求 1 ≤ M ≤ N', 'Requires 1 ≤ M ≤ N'))
        sel = ranked[m - 1:n]
        if not sel:
            raise ValueError(i18n.tr(
                f'区间超出榜单长度（当前共 {len(ranked)} 名）',
                f'Range beyond ranking length ({len(ranked)} entries)'))
        desc = f'第 {m} 名' if m == n else f'第 {m}~{n} 名'
        return [x[0] for x in sel], desc
    raise ValueError(i18n.tr('请先填写其中一行（前 N 名，或 M~N 名）',
                             'Fill one row first (Top N, or ranks M~N)'))


# ---------------------------------------------------------------------
# UI + 应用
# ---------------------------------------------------------------------

def build_rank_block(gui, parent, colors):
    """在规则选项卡里构建"排行榜显示"块（视觉位置：显示规则→本块→自动隐藏）。

    colors 由主窗传入（主窗持有 COLORS，本模块不反向 import 主窗）；
    ★R278-A 同时挂到 gui._rank_colors，供排行组小窗建窗取色。"""
    from utils import i18n

    gui._rank_colors = colors
    frame = ttk.LabelFrame(
        parent, text=i18n.tr('🏆 排行榜显示（按统计榜显示名次区间，其余隐藏）',
                             '🏆 Rank Display (show a rank range by stats, hide the rest)'),
        padding='10', style='Card.TLabelframe')
    frame.pack(fill=tk.X, pady=(0, 6))

    # 行0：榜源 + 常驻统计提醒
    row0 = ttk.Frame(frame)
    row0.pack(fill=tk.X, pady=(0, 4))
    ttk.Label(row0, text=i18n.tr('排行依据:', 'Rank by:')).pack(side=tk.LEFT,
                                                                padx=(0, 6))
    gui._rank_dim = tk.StringVar(value='icon')
    ttk.Radiobutton(row0, text=i18n.tr('图标打开榜', 'Icon opens'),
                    value='icon', variable=gui._rank_dim).pack(side=tk.LEFT)
    ttk.Radiobutton(row0, text=i18n.tr('分类切换榜', 'Category switches'),
                    value='category', variable=gui._rank_dim).pack(
        side=tk.LEFT, padx=(8, 0))
    ttk.Label(row0, text=i18n.tr('（数据来自统计功能，关闭时用历史数据）',
                                 '(from stats; historical data while stats are off)'),
              foreground=colors['text_secondary'],
              font=ui_layout.app_font(9)).pack(side=tk.RIGHT)

    # ★R216 阶段2（用户 2026-09-23 图1：M 后面加"名"、N 后面加"名"就对齐了）：
    # 原来行1 标签是"N 名"、行2 是"M"/"~ N"，宽度不同 ⇒ 两个输入框上下错开。
    # 现在两行进同一个 grid 区（form_block），同列号 ⇒ 逐列天然对齐；
    # 标签补齐"名"（EN 侧顺手去掉中文"名"字，原来 EN 也显示"N 名"）。
    form = ui_layout.form_block(frame)

    def _mk_row(row, txt_zh, txt_en, keys):
        cells = []
        vars = {}
        for lbl_zh, lbl_en, key in keys:
            v = tk.StringVar()
            cells.append(ttk.Label(form, text=i18n.tr(lbl_zh, lbl_en)))
            e = ttk.Entry(form, textvariable=v, width=6)
            cells.append(e)
            vars[key] = v
        ui_layout.form_row(form, row, i18n.tr(txt_zh, txt_en), *cells)
        return vars

    # 行1：显示前 N 名
    gui._rank_top = _mk_row(0, '显示前', 'Show top', [('N 名', 'N', 'n')])
    # 行2：显示第 M ~ N 名
    gui._rank_range = _mk_row(1, '显示第', 'Show ranks',
                              [('M 名', 'M', 'm'), ('~ N 名', '~ N', 'n2')])

    # ★ R109 两行互斥（用户定案 2026-09-17）：一行有输入就自动清空另一行，
    # 用动作告诉用户"这两个不能同时填"——比口头提示可靠
    _top = gui._rank_top['n']
    _m = gui._rank_range['m']
    _n2 = gui._rank_range['n2']
    _mutex = {'on': False}

    def _clear(*vars_):
        if _mutex['on']:
            return
        _mutex['on'] = True
        try:
            for v in vars_:
                v.set('')
        finally:
            _mutex['on'] = False

    _top.trace_add('write', lambda *a: _clear(_m, _n2) if _top.get() else None)
    _m.trace_add('write', lambda *a: _clear(_top, _n2) if _m.get() else None)
    _n2.trace_add('write', lambda *a: _clear(_top) if _n2.get() else None)

    # 行3：执行 + 恢复全部（★ R108 自退役的"显示规则"区迁入）+ 说明
    # ★R216：按钮分档——主操作实底、常规描边、说明 ghost 靠右（原来三个
    # 裸按钮同权重，看不出点哪个）
    row3 = ttk.Frame(frame)
    row3.pack(fill=tk.X, pady=(SP['xs'], 0))
    ttk.Button(row3, text=i18n.tr('❓ 说明', '❓ Help'),
               command=gui._on_rules_help).pack(side=tk.RIGHT)
    # ★R281-2b（用户 08 批 item2b「没有后悔的控件……要手动去管理里面的图标分类去删」）：
    # 后悔药放 row3 **右侧**、跟 ❓说明 并排——不进下面那把 action_bar，所以左侧四枚的
    # cols=4 分配一个字都没动 ⇒ 卡片宽度闸/高度闸本轮零改动（B1/B2 钉住这件事）。
    ttk.Button(row3, text=i18n.tr('⛔ 撤组', '⛔ Undo'),
               command=lambda: ask_rank_group_undo(gui)).pack(side=tk.RIGHT, padx=(0, 6))
    _rank_left = ttk.Frame(row3)
    _rank_left.pack(side=tk.LEFT, fill=tk.X, expand=True)
    # ★R278-A：第三枚「🏆 建组上栏」——把榜单区间固化成一个普通分类并常驻
    # 悬浮栏（快照伪分类法，设计稿 run/r257/方案_B3_排行组进悬浮栏.md）。
    # 仍是同一行三列（cols 2→3），不新增行 ⇒ 卡片高度不变（尺寸闸不破）。
    ui_layout.action_bar(_rank_left, [
        (i18n.tr('▶ 应用显示', '▶ Apply'),
         lambda: apply_rank_display(gui), 'primary.TButton'),
        (i18n.tr('⏹ 恢复全部', '⏹ Restore All'),
         gui._on_rule_restore_all, 'secondary.TButton'),
        (i18n.tr('🏆 建组上栏', '🏆 Make Group'),
         lambda: create_rank_group(gui), 'secondary.TButton'),
        # ★R280-E（N-9②，用户 06 批 item4「修」）：建组是**快照**，桌面/榜单变了组不动。
        # 这枚 ↻ 按各组存的 rank_source（榜源+区间）重取名单回灌，不重开小窗、不改组名。
        (i18n.tr('↻ 刷新组', '↻ Refresh'),
         lambda: refresh_rank_groups(gui), 'secondary.TButton'),
    ], cols=4, pad=SP['s'])
    return frame


def _parse_int(v, label):
    v = (v or '').strip()
    if not v:
        return None
    try:
        n = int(v)
    except ValueError:
        from utils import i18n
        raise ValueError(i18n.tr(
            f'{label} 必须是整数（收到 "{v}"）',
            f'{label} must be an integer (got "{v}")'))
    return n


def apply_rank_display(gui):
    """执行：按榜源+两行参数选名次区间 → apply_visibility 显示子集"""
    from utils import i18n
    from core.icon_manager import VIRTUAL_DESKTOP_ICONS

    # 1) 参数解析（行1优先，两行都填时行2忽略并提示）
    try:
        top_n = _parse_int(gui._rank_top['n'].get(), 'N')
        m = _parse_int(gui._rank_range['m'].get(), 'M')
        n2 = _parse_int(gui._rank_range['n2'].get(), 'N')
    except ValueError as e:
        messagebox.showwarning(i18n.tr('参数有误', 'Invalid input'), str(e),
                               parent=gui.root)
        return
    if top_n is not None and (m is not None or n2 is not None):
        gui._log(i18n.tr('两行都填了，按"前 N 名"执行（行2已忽略）',
                         'Both rows filled; using "Top N" (row 2 ignored)'))

    # 2) 榜源
    dim = gui._rank_dim.get()
    logger.info(f"[排行榜] 应用开始: dim={dim} top_n={top_n} m={m} n2={n2}")
    if dim == 'icon':
        ranked = _ranked(_load_icon_opens(gui))
        empty_hint = i18n.tr(
            '还没有图标打开记录。\n请先到"📊 统计"页打开统计功能，双击打开几个图标后再来。',
            'No icon-open records yet.\nEnable stats on the "📊 Stats" page and open a few icons first.')
    else:
        ranked = _ranked(_load_category_switches(gui))
        empty_hint = i18n.tr(
            '还没有分类切换记录。\n请先到"📊 统计"页打开统计功能，点几次悬浮条分类后再来。',
            'No category-switch records yet.\nEnable stats on the "📊 Stats" page and click a few categories first.')
    if not ranked:
        logger.warning("[排行榜] 榜为空（无统计记录）→ 弹指路提示")
        messagebox.showinfo(i18n.tr('提示', 'Info'), empty_hint, parent=gui.root)
        return
    logger.info(f"[排行榜] 榜大小={len(ranked)} 前几名={ranked[:5]}")

    # 3) 统计关闭提醒（R30/R31 模板：用户要求"记得提醒打开统计功能"）
    if not gui.stats_capture.enabled:
        if not messagebox.askyesno(
                i18n.tr('统计功能已关闭', 'Stats are off'),
                i18n.tr('统计功能当前关闭，榜单停留在历史值。\n'
                        '如需按最新数据执行，请先到"📊 统计"页打开统计功能并用一段时间。\n\n'
                        '仍按历史数据执行吗？',
                        'Stats are off; the ranking reflects historical data.\n'
                        'Enable stats on the "📊 Stats" page for fresh ranking.\n\n'
                        'Proceed with historical data anyway?'),
                parent=gui.root):
            gui._log(i18n.tr('⏹ 已取消：统计功能未开启（可到"📊 统计"页开启后再用）',
                             '⏹ Cancelled: stats are off (enable on "📊 Stats" first)'))
            return

    # 4) 选取
    try:
        selected, desc = pick_range(ranked, top_n=top_n, m=m, n=n2)
    except ValueError as e:
        logger.warning(f"[排行榜] 参数不合法: {e}")
        messagebox.showwarning(i18n.tr('参数有误', 'Invalid input'), str(e),
                               parent=gui.root)
        return
    logger.info(f"[排行榜] 选中 {desc} 共 {len(selected)} 项: {selected[:8]}")

    # 5) 构建可见集（口径与 R49 规则执行一致）
    visible = set()
    rank_detail_parts = []
    counts = dict(ranked)   # ★ R109：selected 是名字列表，次数按名查
    if dim == 'icon':
        visible |= set(selected)
        rank_detail_parts = [f'{nm}({counts.get(nm, 0)})' for nm in selected]
    else:
        # ★ R109 别名容错：恢复出厂后分类是英文名，旧点击记录却是中文名
        # （R96.2 同类教训）——canonical 名与 name_en 都收进查找表
        alias = {}
        for c in gui.category_manager.get_all_categories():
            alias[c.get('name', '')] = c
            if c.get('name_en'):
                alias[c['name_en']] = c
        for nm in selected:
            c = alias.get(nm)
            if c:
                visible |= set(c.get('icons', []))
                rank_detail_parts.append(f'{nm}({counts.get(nm, 0)}次)')
            else:
                rank_detail_parts.append(f'{nm}({i18n.tr("已不存在", "gone")})')
    if not visible:
        messagebox.showinfo(i18n.tr('提示', 'Info'),
                            i18n.tr('选中名次没有对应任何桌面图标。',
                                    'The selected ranks match no desktop icons.'),
                            parent=gui.root)
        return
    # ★ R109 图标榜命中预检：榜名在当前桌面一个都对不上（图标已改名/
    # 移除/统计名与桌面名不一致）→ 明确提示并取消，不给"没反应"的错觉
    if dim == 'icon':
        try:
            # ★ R111：用全量名->路径表（含被分类视图隐藏的图标）——
            # _get_visible_icon_map 会滤掉隐藏文件，分类视图下误报"找不到"
            desktop_names = set(gui.icon_manager._name_to_path_map().keys())
            hit = [nm for nm in selected if nm in desktop_names]
        except Exception:
            hit = selected
        if not hit:
            messagebox.showinfo(
                i18n.tr('提示', 'Info'),
                i18n.tr('榜上的图标名在当前桌面一个都找不到（图标可能已改名或移除）。\n'
                        '请确认桌面图标与统计记录一致后再试。',
                        'None of the ranked icon names match the current desktop\n'
                        '(renamed or removed?). Align them and try again.'),
                parent=gui.root)
            return
    # ★ R110（用户定案）：图标榜=严格按排名显示，系统图标不强制加入
    # （点得少就不显示）；分类榜保留系统图标跟随"常驻"设置（分类语义）
    if dim == 'category':
        if gui._app_settings.get('system_icons_resident', True):
            visible |= set(VIRTUAL_DESKTOP_ICONS.keys())
        visible.add(gui.icon_manager.get_user_folder_icon_name())

    # 6) 应用 + 刷新 + 高亮（R105 三件套：单次点击框准）
    logger.info(f"[排行榜] 可见集 {len(visible)} 个 → 收纳档落地")
    im = gui.icon_manager
    gui._log(i18n.tr(
        f'▶ 排行榜显示 [{desc}]: 显示 {len(visible)} 个图标，其余隐藏…',
        f'▶ Rank display [{desc}]: showing {len(visible)} icons, hiding the rest…'))
    # ★R305：这条链也走路由器——hide 档与旧版逐字节相同；box 档把不该看的真搬进
    #   「收纳·排行榜显示」盒。组名固定这一枚，免得每次换榜参数就换一个盒。
    from core import visibility_router
    # ★R321 H 组：这一条也是批量链（用户点的是"按榜单显示这一组"，不是"倒空某只盒"）
    #   ⇒ 路由器里散回已改成不碰桌面上那一枚，留在盒里的几枚经 `log` 报进界面日志。
    result = visibility_router.apply_group_visibility(
        im, visible, group='排行榜显示', source='rank',
        settings=getattr(gui, '_app_settings', None),
        apply_layout=getattr(im, 'apply_positions_settled', None),
        positions_provider=getattr(im, '_read_all_positions_live', None),
        log=gui._log)
    if result.get('reason'):
        gui._log('⚠ ' + str(result['reason']))
    try:
        gui.dock._force_listview_refresh()
        gui.dock._wait_listview_count(im.get_icon_count(), timeout=2.0)
    except Exception as e:
        # ★R287-N14：图标可见性已经改了，这里只是让列表视图立刻跟上——
        # 刷不动不影响结果，但"刷不动"必须留一行，否则下次报"显示没刷新"无从查起
        logger.warning(f"[排行榜] 应用显示后刷新列表视图失败（图标已切换）: {e}")
    if gui.highlighter:
        try:
            # ★ R110（用户定案）：框跟随高亮框区四态规则——
            #   只框系统=框系统图标；框住分类和常驻/只框分类=框榜上显示的
            #   图标（此视图的"分类"即榜上名次）；四态全关=不画框
            hl = gui.highlighter
            sys_names = set(VIRTUAL_DESKTOP_ICONS.keys()) | {
                gui.icon_manager.get_user_folder_icon_name()}
            if getattr(hl, 'sys_only', False):
                frame_names = sys_names
            else:
                # R66 语义：base=榜上显示的图标（分类榜剔掉刚加的系统图标），
                # 框住分类和常驻/只框分类 → 框 base；框住系统图标 → 追加系统；
                # 四态全关 → 一个框都不画（2026-09-15 框选语义定案）
                base = set(selected) if dim == 'icon' else (visible - sys_names)
                frame_names = set()
                if getattr(hl, 'frame_all', False) or getattr(hl, 'cat_only', False):
                    frame_names |= base
                if getattr(hl, 'frame_sys', False):
                    frame_names |= sys_names
            if frame_names:
                hl.highlight_names(list(frame_names))
                hl.collect_now()
                hl.force_redraw()
                hl.arm_recollect(8.0)
            else:
                hl.clear()
        except Exception as e:
            logger.error(f"[排行榜] 高亮失败: {e}")
    failed = len(result.get('failed', []))
    logger.info(f"[排行榜] apply_visibility 完成: 隐藏{result.get('hidden', 0)} "
                f"恢复{result.get('shown', 0)} 失败{failed}")
    gui._log('✓ ' + i18n.tr(
        f"排行榜显示已执行：{desc} → 共 {len(visible)} 个图标，"
        f"隐藏 {result.get('hidden', 0)}，失败 {failed}。明细: "
        + '、'.join(rank_detail_parts[:8]),
        f"Rank display done: {desc} → {len(visible)} icons, "
        f"hidden {result.get('hidden', 0)}, failed {failed}."))


# ---------------------------------------------------------------------
# ★R278-A 排行组进悬浮栏（快照伪分类法，一期：快照分类 + 常驻上栏）
#   设计稿 run/r257/方案_B3_排行组进悬浮栏.md；用户 `----2026-9-30 03----` 点头。
#   核心判断：排行组不发明新槽类型，而是**变成一个普通分类**——显示/隐藏、框、
#   布局、热键、导入导出、管理页编辑全部机制零改动直接可用；名单来源是榜单快照，
#   另带一个可选键 rank_source（没这键就是普通分类，向后兼容）。
# ---------------------------------------------------------------------

GROUP_PALETTE = ['#8be9fd', '#50fa7b', '#ff79c1', '#ffb86c', '#bd93f9',
                 '#ff5555', '#f1fa8c', '#a78bfa', '#2196f3', '#ffb6c1',
                 '#00ced1', '#8bc34a']   # 与 manage_panel.ask_category_color 同色板


def rank_board(gui, dim):
    """按榜源取降序榜 [(名, 次)]——和「▶ 应用显示」同一口径，别另起炉灶。"""
    if dim == 'icon':
        return _ranked(_load_icon_opens(gui))
    return _ranked(_load_category_switches(gui))


def rank_snapshot(gui, dim, m, n):
    """第 m..n 名的名单 + 落到桌面图标的名单（快照取数口，纯读不写盘）。

    图标榜：榜名＝桌面图标显示名，直接进分类；
    分类榜：取这些分类的 icons 并集（★R109 同款别名容错：canonical 与 name_en 都认）。
    返回 (ranked_names, icon_names, desc)；参数不合法/空榜抛 ValueError。"""
    from utils import i18n
    ranked = rank_board(gui, dim)
    if not ranked:
        raise ValueError(i18n.tr(
            '榜单为空：请先到"📊 统计"页打开统计功能并用一段时间',
            'Ranking is empty: enable stats on the "📊 Stats" page first'))
    names, desc = pick_range(ranked, m=m, n=n)
    if dim == 'icon':
        return names, list(names), desc
    alias = {}
    for c in gui.category_manager.get_all_categories():
        alias[c.get('name', '')] = c
        if c.get('name_en'):
            alias[c['name_en']] = c
    icons = []
    for nm in names:
        c = alias.get(nm)
        for ic in (c or {}).get('icons', []):
            if ic not in icons:
                icons.append(ic)
    return names, icons, desc


def _system_icon_names(gui):
    """系统虚拟图标的名字集合（此电脑/回收站/网络/控制面板/用户文件夹）。

    ★R292-①：判据照抄 drag_drop.py:627-644 那一份口径，不另立第二套——
    `VIRTUAL_DESKTOP_ICONS` 的键 ∪ **动态**解析出的"用户文件夹"显示名
    （那枚跟着账户名变，常量表里的 'Administrator' 只是历史别名，见 icon_manager:81）。
    没有 icon_manager（老桩具/单测）⇒ 只回常量表，别把整条建组链路挡死。"""
    from core.icon_manager import VIRTUAL_DESKTOP_ICONS
    names = set(VIRTUAL_DESKTOP_ICONS.keys())
    fn = getattr(getattr(gui, 'icon_manager', None),
                 'get_user_folder_icon_name', None)
    if callable(fn):
        try:
            got = fn()
            if got:
                names.add(got)
        except Exception as e:
            logger.warning(f'[排行组] 用户文件夹显示名读不动（只按常量表剔系统图标）: {e}')
    return names


def _prune_gone(gui, icons):
    """★R279-⑦（产品负责人 09-30 04 批追加）：桌面上**已经没了**的图标不进组。

    原话：「如果桌面图标不在了，就不应该把这个图标放在这个布局中，因为图标不在，
    放在上面没有意义」。榜单源（usage_stats.json / clicks.json）从不核对图标是否
    还在（只做类型/非负校验，见 usage_stats.py:_clean），删过改过名的老名次会
    一直占位并被 `rank_snapshot` 原样灌进组里 ⇒ 组里躺着一排点了没反应的死名字。

    判据**不另立第四套**：直接复用产品自己的保守口径
    `core/icon_manager.py:959 filter_ghost_names`（要"不在 ListView + 不是本程序
    藏着的 + 桌面目录磁盘上也没有同名文件"三条同时成立才算幽灵；实时名单读不到
    时它一律不剪）。这条很要紧——"读不动"绝不能用当成"全都不在了"（★R234 口径）。

    返回 (留下的名单, 被剪掉的名字)。没有 icon_manager（老桩具/单测）⇒ 一律不剪。"""
    fn = getattr(getattr(gui, 'icon_manager', None), 'filter_ghost_names', None)
    if not callable(fn):
        return list(icons), []
    try:
        kept, ghosts = fn({n: 0 for n in icons})
        return [n for n in kept], list(ghosts or [])
    except Exception as e:
        logger.warning(f'[排行组] 幽灵判定读不动（这一趟不剪名单）: {e}')
        return list(icons), []


def _push_dock_state(gui, name, mode):
    """把「常驻 / 本次 / 已删」这一档推给悬浮栏：先摆平 dock 侧的本次登记，再整表重建槽位。

    mode：'pin'=常驻（撤掉同名的本次登记，走分类表那条持久通道）、
          'unpin'=本次上栏（登记，★R281-2a）、'gone'=整组删了（撤登记）。
    dock 没接线、或没有这两把入口（老桩具/未升级的调用方）⇒ 那一半**不做**，
    返回值如实反映做到了哪一档（'none'），调用方据此改文案，不许谎报"已上栏"。
    返回值：'pin' / 'unpin' / 'gone' / 'none'。
    """
    dock = getattr(gui, 'dock', None)
    if dock is None:
        return 'none'
    fn_name = ('add_session_rank_slot' if mode == 'unpin'
               else 'drop_session_rank_slot')
    fn = getattr(dock, fn_name, None)
    state = mode if callable(fn) else 'none'
    if callable(fn):
        try:
            fn(name)
        except Exception as e:
            logger.warning(f'[排行组] 本次上栏登记失败（不影响分类已存住）: {e}')
            state = 'none'
    try:
        dock.sync_slots(gui.category_manager.get_all_categories())
    except Exception as e:
        logger.warning(f'[排行组] 悬浮栏槽位同步失败（分类已存住，重开软件即好）: {e}')
    return state


def _rank_group_of(cm, name):
    """按名取那条分类，**并且只认排行组身份证**（`rank_source`，★R279-⑦ 同一条判据）。

    返回 (分类 dict 或 None, 拒的理由)。撤组这两把出口都过这一道：没有它，
    「🗑 删除该组」就成了"任何分类都能被这个按钮删掉"（U6 钉住）。"""
    name = (name or '').strip()
    if not name:
        return None, '空组名'
    for c in cm.categories:
        if isinstance(c, dict) and c.get('name') == name:
            if not cm.is_rank_group(c):
                return None, '不是排行组'
            return c, None
    return None, '没有这个组'


def commit_rank_group(gui, name, icons, color, board, m, n, in_dock=True,
                      include_public=False, exclude_system=False):
    """落一个排行组分类（守卫全在这一层，UI 只管收集意图）。返回 (ok, msg)。

    ★R279（批35 item6 + 追加）把守卫重排成"每条出口都留下一行话"——旧写法只有成功
    那条路 gui._log，四种拒建在 app.log 里一个字都不留，用户报"建不起来"时无从归因
    （产品负责人点名：「针对这个功能你要注意有日志功能，方便排查 bug」）。

    出口一共五条，日志一一对得上（判据见 测试_ui/test_r279_rank_group_ux.py L 段）：
      空组名 / 保留名 / 空名单（剪掉幽灵之后）/ 同名但是手工分类 / 写盘失败。
      （★R280-D 摘掉了第六条"槽满降级"——它在真机上恒成立，见下面守卫②的新口径。）

    三条守卫的口径（★R278 设计稿 + ★R279/R280 修订）：
    ① **名字对上的是另一个排行组 ⇒ 就地覆盖刷新**（★R279-⑤，用户点名"第二次要提示
       错误、之前那个名字还在"——他要的是"换区间重建同一个组"，不是被同名挡在门外）。
       判据读的是那条分类**有没有 `rank_source` 键**（排行组的身份证），不是猜名字
       长得像；手工建的分类没有这键 ⇒ 仍然拒，且给可操作的出路（改名/先去管理页删）。
    ② 勾了"显示在悬浮栏"⇒ **一定上栏**（★R280-D）：排行组在 dock 侧走不占槽的临时区，
       这里不再数"别人占了几格"。旧写法数的是 in_dock 枚数、dock 截断看的是排序后前 N 个，
       两套口径同时生效时用户就再也上不了栏（真机 cap=10 / used=13 实证）。
    ③ 区间没命中图标 ⇒ 不建空分类（空分类会在悬浮栏占一格，比不建更坑）。"""
    from utils import i18n
    cm = gui.category_manager
    name = (name or '').strip()
    icons = list(icons or [])
    want_dock = bool(in_dock)
    logger.info(f'[排行组] 建组企图: name={name!r} board={board} 区间={m}-{n} '
                f'命中={len(icons)} 勾常驻={want_dock} 剔系统={bool(exclude_system)}')
    if not name:
        logger.warning('[排行组] 结果=拒(空组名)')
        return False, i18n.tr('组名不能为空', 'Group name is required')
    if name in cm.RESERVED_NAMES:
        logger.warning(f'[排行组] 结果=拒(保留名) name={name}')
        return False, i18n.tr(
            f'「{name}」是保留名（全部/窗/一键目录/✕），换个别的名再建组',
            f'"{name}" is a reserved name (All/Win/Quick/Close) - pick another')
    # ★R292-①（用户 10-04 点名：「建组上栏」再多一枚"不包含系统图标"，默认勾选）：
    # 图标打开榜记的是"点开次数"，此电脑/回收站这些系统图标一样在记 ⇒ 它们常年霸前几名，
    # 不剔就会躺进用户新建的组里（用户原话"不显示我的电脑、网络、回收站…这几个图标"）。
    # 剔在守卫③**之前**：剔完空了要能走"空名单"那条出口，给用户一句人话而不是静默。
    _sys_dropped = []
    if exclude_system:
        _sys = _system_icon_names(gui)
        _kept = [i for i in icons if i not in _sys]
        _sys_dropped = [i for i in icons if i in _sys]
        if _sys_dropped:
            logger.info(f'[排行组] 剔系统图标 {len(_sys_dropped)} 个 name={name} '
                        f'明细={_sys_dropped[:8]}')
        icons = _kept
    # 守卫③先过一遍"还活着"的名单（★R279-⑦），拦在建组门口而不是让用户事后 cleanup
    icons, ghosts = _prune_gone(gui, icons)
    if ghosts:
        logger.info(f'[排行组] 剪掉桌面上已不存在的 {len(ghosts)} 个 '
                    f'name={name} 明细={ghosts[:8]}')
    if not icons:
        logger.warning(f'[排行组] 结果=拒(空名单) name={name} 剪幽灵={len(ghosts)} '
                       f'剔系统={len(_sys_dropped)}')
        if _sys_dropped:
            return False, i18n.tr(
                f'这个区间 {len(_sys_dropped)} 个全是系统图标（此电脑/回收站/网络…），'
                f'去掉系统图标后组里没东西了——把「不包含系统图标」取消再建',
                f'That range is all system icons ({len(_sys_dropped)} of them): '
                f'the group would be empty - untick "Exclude system icons"')
        return False, i18n.tr(
            '这个区间没命中任何**还在桌面上的**图标，先别建组',
            'This range matches no icon that is still on the desktop')
    # 名字先自查一遍再交给 add_category：add_category 对"重名/保留名/写盘失败"
    # 一律返回 False，只看返回值分不出是哪种 ⇒ 文案会把写盘失败说成重名
    dup = None
    for c in cm.categories:
        if c.get('name') == name:
            dup = c
            break
    overwrite = dup is not None and cm.is_rank_group(dup)
    if dup is not None and not overwrite:
        logger.warning(f'[排行组] 结果=拒(同名手工分类) name={name}')
        return False, i18n.tr(
            f'「{name}」是你手工建的分类，排行组不去覆盖它：'
            f'换个组名，或者先去管理页把那个分类删掉',
            f'"{name}" is a category you made by hand, so a rank group will not '
            f'overwrite it - rename it, or delete it on the manage page first')
    note = ''
    in_dock = want_dock
    # ★R280-D（用户 06 批 item7 真机实锤，日志 `结果=槽满降级 cap=10 used=13`）：
    # 旧守卫按"标了 in_dock 的分类枚数 >= max_slots"就把排行组降级不上栏，可用户台上
    # 已有 13 枚标记 vs 10 个槽 ⇒ **永远**满足不了，提示"先在管理页撤一枚"撤完 12>10 还是不行。
    # 现行口径：排行组是 ★R279 定性的临时功能，dock.sync_slots 让它走**尾部临时区**、
    # 不吃用户那 max_slots 个槽 ⇒ 勾了常驻就一定上得了栏，也不再因为"排行组建得多"
    # 去撤用户的分类（那条截断只发生在用户自己的分类之间，规则没变）。
    # 这里**不再做任何槽位判定**：判定的唯一事实源在 dock 侧（两处各数一遍必然打架）。
    _snap = None
    if overwrite:
        # 原地刷新，不走 add_category（它见同名就返回 False，正是用户截图里那条报错）。
        # 先抄一份旧值：名单写盘失败时内存要退回旧名单，不能留"盘上是旧的、内存是新的"
        # 这种分裂（★R130/R207 同一条口径）。
        _snap = dict(dup)
        _snap['icons'] = list(dup.get('icons') or [])
    else:
        _n_before = len(cm.categories)
        if not cm.add_category(name, color=color or '#8be9fd', in_dock=in_dock):
            # ★R280-⑦（挂账 #139）：add_category 是**先 append 再落盘**，落盘失败时
            # 那条空组会留在内存里 ⇒ 管理页显示一个盘上根本没有的组，重启才消失
            # （和用户 06 批 item7 那句"分组里面已经有了"是同一种分裂）。只摘自己
            # 刚追加的那一条（长度确实涨了才摘），别动用户原有的同名分类。
            if len(cm.categories) > _n_before:
                cm.categories.pop()
            logger.warning(f'[排行组] 结果=拒(写盘失败-新建) name={name}')
            return False, i18n.tr(
                f'「{name}」没能写进分类文件（categories.json 写盘失败或本次会话被禁写）',
                f'"{name}" could not be written to categories.json')
    for c in cm.categories:
        if c['name'] == name:
            c['icons'] = list(icons)
            c['rank_source'] = {'board': board, 'm': int(m), 'n': int(n)}
            # ★R282-2（用户 10-01 01 批 R010-2）：勾"包含公共图标"⇒ display_mode='none'
            # （dock.py:1567-1568 叠加 get_sticky_icon_names()＝"设置→常驻"里的图标）；
            # 不勾（默认）⇒ 'exclusive'（dock.py:1563 只显示本组，桌面更清爽）。
            # 复用既有并集逻辑，核心零改动；刷新走 refresh_rank_groups 时按原 display_mode 还原。
            c['display_mode'] = 'none' if include_public else 'exclusive'
            # ★R292-①：这一档**要**存进分类——↻ 刷新是按 rank_source 重取榜单回灌的，
            # 不记着"当初勾没勾"，下一次刷新就会把系统图标又灌回组里（用户看到的
            # 是"我明明去掉了，它自己长回来"）。老组没这键 ⇒ 读回 False＝行为不变。
            c['exclude_system'] = bool(exclude_system)
            if overwrite:
                c['color'] = color or '#8be9fd'
                c['in_dock'] = in_dock
            break
    if not cm._save_categories():
        if overwrite:
            dup.clear()
            dup.update(_snap)
            logger.warning(f'[排行组] 结果=拒(写盘失败-覆盖) name={name}')
            return False, i18n.tr(
                '这次刷新没存住（categories.json 写盘失败），组还是原来的名单',
                'The update was not saved (categories.json write failed); '
                'the group keeps its previous icon list')
        # 写盘失败 ⇒ 把刚追加的那条摘掉，内存里不留半截分类（★R130 口径）。
        # 磁盘上可能残留一个空组（第一次 add_category 那次写成功的），
        # 这话如实写在文案里，别报"已回滚"让用户以为盘上也没了。
        cm.categories = [c for c in cm.categories if c['name'] != name]
        logger.warning(f'[排行组] 结果=拒(写盘失败-名单没存住) name={name}')
        return False, i18n.tr(
            '名单没存住（categories.json 写盘失败），内存里的组已撤掉；'
            '磁盘上若多出一个同名空组，可在管理页删掉再重来',
            'The icon list was not saved (rolled back in memory); '
            'delete the leftover empty group if the manager shows one')
    # ★R281-2a：档位在推给悬浮栏**之前**先摆平——`sync_slots` 是按登记表重建槽位的，
    # 先登记再重建，建组那一下当场就上栏（旧写法这里只 sync，不勾常驻的组永远进不了
    # 临时区 = item2a 的现象）。
    _dock_state = _push_dock_state(gui, name, 'pin' if in_dock else 'unpin')
    _refresh_manage_lists(gui)
    if in_dock:
        # ★R280-D：如实报"上没上栏"。以前这句位子上放的是"槽已满、没上栏"的道歉，
        # 现在排行组不占用户槽，所以这里只说清楚它走的是哪条通道。
        note = i18n.tr('· 已上悬浮栏（排行组走临时区，不占你那几个槽）',
                       ' · pinned to the dock (rank groups take the temp area, '
                       'not your slots)')
    elif _dock_state == 'unpin':
        # ★R281-2a（用户 08 批 item2a）：没勾常驻＝**本次**上栏，重启后自动消失。
        # 这一档以前不存在（不勾就彻底看不见），文案必须把"重启后不在"讲在前面，
        # 免得用户按"勾了才会上栏"的旧印象以为建坏了。
        note = i18n.tr('· 本次已上悬浮栏，重启后自动消失（想一直在就勾「常驻」）',
                       ' · on the dock for this session only; it goes away after '
                       'a restart (tick "Pin to dock bar" to keep it)')
    else:
        note = ''
    _sess_zh = '本次上栏=是' if _dock_state == 'unpin' else '本次上栏=否'
    if overwrite:
        logger.info(f'[排行组] 结果=覆盖刷新 name={name} 图标={len(icons)} '
                    f'剪幽灵={len(ghosts)} in_dock={in_dock} {_sess_zh}')
        msg = i18n.tr(f'✓ 已更新排行组「{name}」：{len(icons)} 个图标{note}',
                      f'✓ Rank group "{name}" updated: {len(icons)} icons{note}')
    else:
        logger.info(f'[排行组] 结果=新建 name={name} 图标={len(icons)} '
                    f'剪幽灵={len(ghosts)} in_dock={in_dock} {_sess_zh}')
        msg = i18n.tr(f'✓ 已建排行组「{name}」：{len(icons)} 个图标{note}',
                      f'✓ Rank group "{name}" created: {len(icons)} icons{note}')
    if ghosts:
        msg += i18n.tr(f'（{len(ghosts)} 个桌面上已不存在的图标没进组）',
                       f' ({len(ghosts)} icons no longer on the desktop were left out)')
    _ui_log(gui, msg)
    return True, msg


def refresh_rank_groups(gui, name=None):
    """★R280-E（N-9②）：按**最新榜单**重取名单，刷新已有的排行组（不重开小窗、不改组名）。

    旧写法建完组就是一份快照：`rank_source` 只记着"当初按哪个榜、第几名"，
    之后桌面变了、榜单位次换了，组里那排名字一直不动（用户读成"软件不更新"）。

    口径：**不开第二条写盘路**——逐组回灌 `commit_rank_group`，同名且带 `rank_source`
    天然走"覆盖刷新"那条出口，于是守卫（幽灵名单剪枝、写盘失败回滚）、
    日志（每条出口一行）、悬浮栏同步全部复用，不会出现"刷新能绕过守卫"的暗道。
    返回 (刷成的组数, 名单变动的组数, 给用户看的一句话)。"""
    from utils import i18n
    cm = gui.category_manager
    targets = [c for c in cm.get_all_categories() if cm.is_rank_group(c)]
    if name:
        targets = [c for c in targets if c.get('name') == name]
    if not targets:
        logger.info('[排行组] 结果=刷新跳过(没有排行组)')
        return 0, 0, i18n.tr('还没有排行组：先点「🏆 建组上栏」建一个',
                             'No rank group to refresh yet')
    done = changed = 0
    detail = []
    for c in targets:
        nm = c.get('name', '')
        src = c.get('rank_source') or {}
        board = src.get('board', 'icon')
        try:
            m = int(src.get('m', 1))
            n = int(src.get('n', 10))
        except (TypeError, ValueError):
            logger.warning(f'[排行组] 结果=拒(刷新跳过-区间坏了) name={nm} '
                           f'src={src!r}')
            detail.append(f'{nm}(区间记录坏了)')
            continue
        old = list(c.get('icons') or [])
        try:
            _names, icons, _desc = rank_snapshot(gui, board, m, n)
        except ValueError as e:
            logger.warning(f'[排行组] 结果=拒(刷新取榜失败) name={nm} — {e}')
            detail.append(f'{nm}(取榜失败)')
            continue
        ok, msg = commit_rank_group(gui, nm, icons, c.get('color'), board, m, n,
                                    in_dock=bool(c.get('in_dock')),
                                    include_public=(c.get('display_mode') == 'none'),
                                    # ★R292-①：老组没这键 ⇒ False＝刷完还是老名单口径
                                    exclude_system=bool(c.get('exclude_system')))
        if not ok:
            detail.append(f'{nm}(没更新:{msg[:24]})')
            continue
        done += 1
        now = []
        for _c in cm.get_all_categories():
            if _c.get('name') == nm:
                now = list(_c.get('icons') or [])
                break
        delta = len(set(now) - set(old)) + len(set(old) - set(now))
        if delta:
            changed += 1
        detail.append(f'{nm}:{len(old)}→{len(now)}')
    logger.info(f'[排行组] 结果=刷新 组数={done}/{len(targets)} 名单变动={changed} '
                f'明细={detail[:8]}{"…（更多）" if len(detail) > 8 else ""}')
    if done == 0:
        return 0, 0, i18n.tr(f'这次一组都没刷上：{"、".join(detail[:3])}',
                             f'Nothing refreshed: {"; ".join(detail[:3])}')
    msg = i18n.tr(f'↻ 已按最新榜单刷新 {done} 个排行组'
                  f'（名单变动 {changed} 个）',
                  f'↻ Refreshed {done} rank group(s); {changed} changed')
    _ui_log(gui, msg)
    return done, changed, msg


def _refresh_manage_lists(gui):
    """分类改动后让管理页重画（有则刷、没有则跳过，失败只留一行话）。"""
    for _fn in ('_update_lists', '_rebuild_sticky_grid'):
        try:
            if hasattr(gui, _fn):
                getattr(gui, _fn)()
        except Exception as e:
            logger.warning(f'[排行组] 管理页刷新失败 {_fn}: {e}')


def unpin_rank_group(gui, name):
    """★R281-2b 后悔药之一：**取消常驻**。只翻 `in_dock` 这一格——组名、颜色、
    图标名单一枚都不动（U1c）。

    翻完当场登记「本次上栏」，所以用户的观感是"它还在栏上，只是重启后不再自动回来"。
    一撤就消失会被读成"软件吞了我的组"（★R255 桌面主权同一条口径：不拿走用户
    已经看见的东西，除非他明说要）。返回 (ok, 给用户看的一句话)。"""
    from utils import i18n
    cm = gui.category_manager
    c, why = _rank_group_of(cm, name)
    if c is None:
        logger.warning(f'[排行组] 结果=拒(取消常驻-{why}) name={(name or "")!r}')
        return False, i18n.tr(
            f'没能取消常驻：{why}（这个入口只管排行组，普通分类请在管理页改）',
            f'Cannot unpin: {why} (this entry only handles rank groups)')
    if not c.get('in_dock'):
        logger.warning(f'[排行组] 结果=拒(取消常驻-本来没常驻) name={c["name"]}')
        return False, i18n.tr(
            f'「{c["name"]}」本来就不是常驻组：它只在本次开着时上栏，重启自动消失',
            f'"{c["name"]}" is not pinned; it only shows for this session')
    _old = c.get('in_dock')
    # 写这一格只走产品自己那把出口（category_manager.set_in_dock，★R130 真实落盘结果），
    # 不在这里再抄一遍"改字段 + 整表落盘"——两处各写一份迟早会漂。
    if not cm.set_in_dock(c['name'], False):
        c['in_dock'] = _old
        logger.warning(f'[排行组] 结果=拒(取消常驻-写盘失败) name={c["name"]}')
        return False, i18n.tr(
            '没能取消常驻（categories.json 写盘失败），它还是原来的状态',
            'Could not unpin (categories.json write failed); state unchanged')
    _push_dock_state(gui, c['name'], 'unpin')
    _refresh_manage_lists(gui)
    logger.info(f'[排行组] 结果=取消常驻 name={c["name"]} in_dock=False 本次仍上栏')
    msg = i18n.tr(f'↩ 已取消「{c["name"]}」的常驻：这一阵它还在悬浮栏，'
                  f'重启后不再自动回来（想再钉住，重新勾常驻即可）',
                  f'↩ Unpinned "{c["name"]}": still on the dock now, '
                  f'gone after a restart (pin it again anytime)')
    _ui_log(gui, msg)
    return True, msg


def delete_rank_group(gui, name):
    """★R281-2b 后悔药之二：**整组删掉**（用户原话"要手动去管理里面的图标分类去删，
    略微不方便"⇒ 这一步挪到排行榜卡片上）。

    只删分类，**桌面上一个图标都不动**（排行组本来就是"哪些图标算一伙"的记号）。
    落盘走 `category_manager.delete_category` 那一条既有出口（X6 钉住"不开第二条
    写 categories.json 的路"），它返回的是真实落盘结果（★R130）；失败时把内存里
    刚摘掉的那份放回去——否则就是"界面没了、盘上还在、重启又活"（U5）。"""
    from utils import i18n
    cm = gui.category_manager
    c, why = _rank_group_of(cm, name)
    if c is None:
        logger.warning(f'[排行组] 结果=拒(删除-{why}) name={(name or "")!r}')
        return False, i18n.tr(
            f'没能删除：{why}（这个入口只管排行组，普通分类请在管理页删）',
            f'Cannot delete: {why} (this entry only handles rank groups)')
    nm = c['name']
    _snap = list(cm.categories)
    if not cm.delete_category(nm):
        cm.categories = _snap
        logger.warning(f'[排行组] 结果=拒(删除-写盘失败) name={nm}')
        return False, i18n.tr(
            f'「{nm}」没能从分类文件里删掉（categories.json 写盘失败），'
            f'组还在原处，没有半截删除',
            f'"{nm}" could not be removed from categories.json; nothing was deleted')
    _push_dock_state(gui, nm, 'gone')
    _refresh_manage_lists(gui)
    logger.info(f'[排行组] 结果=删除 name={nm} 桌面图标未动')
    msg = i18n.tr(f'🗑 已删除排行组「{nm}」（桌面上的图标一个都没动）',
                  f'🗑 Deleted rank group "{nm}" (no desktop icon was touched)')
    _ui_log(gui, msg)
    return True, msg


def ask_rank_group_undo(gui, parent=None):
    """★R281-2b：「⛔ 撤组」小窗——列出所有排行组，两把后悔药按选中的那一组做。

    为什么不放悬浮栏右键：dock 的 `_paint`/`_wndproc` 必须保持纯 GDI，在那条路上
    碰 Tcl 是静默 native abort（项目红线）；而这两步本来就是"建组"那一摊的逆操作，
    跟 🏆 建组上栏 / ↻ 刷新组 放同一张卡片最省心。
    范式照抄 `ask_rank_group_dialog`（transient + grab + wait_window + holder）。
    返回 ('unpin'|'del', 组名, 是否成功) 或 None（没动过手）。
    """
    from utils import i18n
    cm = gui.category_manager
    parent = parent or gui.root
    dlg = tk.Toplevel(parent)
    dlg.title(i18n.tr('⛔ 撤掉排行组', '⛔ Undo Rank Group'))
    dlg.resizable(False, False)
    try:
        dlg.configure(bg=gui._rank_colors['bg'])
    except Exception as e:
        logger.warning(f'[撤组小窗] 底色应用失败（用默认底色，不影响操作）: {e}')
    dlg.transient(parent)
    holder = {'acted': None}

    body = ttk.Frame(dlg)
    body.pack(padx=12, pady=10)
    ttk.Label(body, text=i18n.tr(
        '先选一组，再做那一步：', 'Pick a group, then act:')).grid(
        row=0, column=0, columnspan=2, sticky='w')
    lb = tk.Listbox(body, width=30, height=6, activestyle='dotbox',
                    font=ui_layout.app_font(10))
    lb.grid(row=1, column=0, rowspan=3, sticky='nsw', padx=(0, 10), pady=4)
    names = []

    def _cat(nm):
        for c in cm.categories:
            if isinstance(c, dict) and c.get('name') == nm:
                return c
        return {}

    def _reload():
        del names[:]
        for c in cm.get_all_categories():
            if cm.is_rank_group(c):
                names.append(c.get('name', ''))
        try:
            lb.delete(0, tk.END)
            for nm in names:
                lb.insert(tk.END, f'{nm}   ' + (
                    i18n.tr('· 常驻', '· pinned') if _cat(nm).get('in_dock')
                    else i18n.tr('· 本次', '· this session')))
            if not names:
                lb.insert(tk.END, i18n.tr('（还没有排行组）', '(no rank group yet)'))
        except Exception as e:
            logger.warning(f'[排行组] 撤组小窗列表刷新失败: {e}')

    def _sel():
        try:
            return names[lb.curselection()[0]]
        except Exception:
            return None

    def _act(kind):
        nm = _sel()
        if not nm:
            messagebox.showinfo(i18n.tr('提示', 'Info'),
                                i18n.tr('先在左边选中一组', 'Pick a group first'),
                                parent=dlg)
            return
        if kind == 'del' and not messagebox.askyesno(
                i18n.tr('确认删除', 'Confirm delete'),
                i18n.tr(f'删除排行组「{nm}」？\n桌面上的图标一个都不动，'
                        f'只是这一组记号没了。',
                        f'Delete rank group "{nm}"?\nNo desktop icon is touched; '
                        f'only this group goes away.'),
                parent=dlg):
            return
        ok, msg = (delete_rank_group(gui, nm) if kind == 'del'
                   else unpin_rank_group(gui, nm))
        holder['acted'] = (kind, nm, bool(ok))
        if not ok:
            messagebox.showwarning(i18n.tr('没做成', 'Not done'), msg, parent=dlg)
        _reload()

    ttk.Button(body, text=i18n.tr('↩ 取消常驻', '↩ Unpin'),
               command=lambda: _act('unpin')).grid(row=1, column=1, sticky='ew', pady=2)
    ttk.Button(body, text=i18n.tr('🗑 删除该组', '🗑 Delete'),
               command=lambda: _act('del')).grid(row=2, column=1, sticky='ew', pady=2)
    ttk.Button(body, text=i18n.tr('关闭', 'Close'),
               command=dlg.destroy).grid(row=3, column=1, sticky='ew', pady=2)
    ttk.Label(body, wraplength=250, justify='left', text=i18n.tr(
        '常驻＝重启后还在；本次＝这次开着的时候在，重启自动消失。',
        'Pinned = stays after a restart. This session = shows now, '
        'gone after a restart.')).grid(row=4, column=0, columnspan=2,
                                       sticky='w', pady=(6, 0))
    _reload()
    dlg.update_idletasks()
    try:
        dlg.geometry('+%d+%d' % (max(parent.winfo_x() + 100, 0),
                                 max(parent.winfo_y() + 120, 0)))
    except Exception as e:
        logger.warning(f'[撤组小窗] 定位失败（按窗口管理器给的位置显示）: {e}')
    dlg.grab_set()
    parent.wait_window(dlg)
    return holder['acted']


def _prefill_range(gui):
    """从卡上两行输入取默认区间：行1「前 N 名」⇒ (1,N)；行2 ⇒ (M,N)；都没填 ⇒ (1,10)。"""
    try:
        top_n = _parse_int(gui._rank_top['n'].get(), 'N')
        m = _parse_int(gui._rank_range['m'].get(), 'M')
        n2 = _parse_int(gui._rank_range['n2'].get(), 'N')
    except ValueError:
        return 1, 10
    if top_n:
        return 1, top_n
    if m and n2:
        return m, n2
    return 1, 10


def ask_rank_group_dialog(gui, parent=None):
    """一枚小窗：组名 / 榜源 / 第 M~N 名 / 颜色 / 三枚复选（常驻、含公共、不含系统）。

    确定 ⇒ 当场算快照（算不动就弹提示、窗不关，让用户改）；返回 spec 或 None。
    范式照抄 manage_panel.ask_category_color（transient + grab + wait_window + holder）。"""
    from utils import i18n
    parent = parent or gui.root
    m0, n0 = _prefill_range(gui)
    dim0 = gui._rank_dim.get() if getattr(gui, '_rank_dim', None) else 'icon'
    dlg = tk.Toplevel(parent)
    dlg.title(i18n.tr('🏆 新建排行组', '🏆 New Rank Group'))
    dlg.resizable(False, False)
    dlg.configure(bg=gui._rank_colors['bg'])
    dlg.transient(parent)
    holder = {'spec': None}

    body = ttk.Frame(dlg)
    body.pack(padx=14, pady=10)
    ttk.Label(body, text=i18n.tr('组名:', 'Name:')).grid(row=0, column=0, sticky='w')
    name_v = tk.StringVar(value=i18n.tr(f'排行 {m0}-{n0} 名',
                                        f'Rank {m0}-{n0}'))
    ttk.Entry(body, textvariable=name_v, width=20).grid(row=0, column=1, columnspan=3,
                                                        sticky='w', pady=2)

    ttk.Label(body, text=i18n.tr('榜源:', 'Rank by:')).grid(row=1, column=0, sticky='w')
    dim_v = tk.StringVar(value=dim0)
    ttk.Radiobutton(body, text=i18n.tr('图标打开榜', 'Icon opens'),
                    value='icon', variable=dim_v).grid(row=1, column=1, sticky='w')
    ttk.Radiobutton(body, text=i18n.tr('分类切换榜', 'Category switches'),
                    value='category', variable=dim_v).grid(row=1, column=2, columnspan=2,
                                                           sticky='w')

    ttk.Label(body, text=i18n.tr('第几名到第几名:', 'From rank to:')).grid(
        row=2, column=0, sticky='w')
    m_v = tk.StringVar(value=str(m0))
    n_v = tk.StringVar(value=str(n0))
    cell2 = ttk.Frame(body)
    cell2.grid(row=2, column=1, columnspan=3, sticky='w')
    ttk.Entry(cell2, textvariable=m_v, width=5).pack(side=tk.LEFT)
    ttk.Label(cell2, text=' ~ ').pack(side=tk.LEFT)
    ttk.Entry(cell2, textvariable=n_v, width=5).pack(side=tk.LEFT)

    ttk.Label(body, text=i18n.tr('颜色:', 'Color:')).grid(row=3, column=0, sticky='w')
    color_v = tk.StringVar(value=_group_color(gui))
    sw = ttk.Frame(body)
    sw.grid(row=3, column=1, columnspan=3, sticky='w')
    prev = tk.Label(sw, width=3, bg=color_v.get(), relief=tk.RIDGE, bd=1)
    prev.pack(side=tk.LEFT, padx=(0, 6))
    # 色块另起一个容器再 grid：sw 里已经 pack 了色样 Label，同一个 master 混用
    # pack/grid 会直接 TclError（★R278-A：D 段真开窗跑出来的，小窗根本弹不起来）
    pal = ttk.Frame(sw)
    pal.pack(side=tk.LEFT)

    def _pick(c):
        color_v.set(c)
        try:
            prev.configure(bg=c)
        except Exception as e:
            # 色样小方块刷不动不影响"选哪一色"已经生效（color_v 在上面已 set）
            logger.warning(f'[新建排行组] 色样预览刷新失败（颜色已选定）: {e}')

    for i, c in enumerate(GROUP_PALETTE):
        tk.Button(pal, bg=c, fg='#111111', text='█', relief=tk.FLAT, bd=0,
                  activebackground=c, cursor='hand2', command=lambda cc=c: _pick(cc),
                  font=ui_layout.app_font(9)
                  ).grid(row=i // 6, column=i % 6, sticky='nsew', padx=1)

    dock_v = tk.BooleanVar(value=True)
    ttk.Checkbutton(body, text=i18n.tr('常驻：显示在顶部悬浮栏', 'Pin to dock bar'),
                    variable=dock_v).grid(row=4, column=0, columnspan=4, sticky='w',
                                          pady=(6, 0))
    # ★R282-2（用户 10-01 01 批 R010-2）：排行组默认**只显本组**（桌面更清爽）；
    # 勾上才把"设置→常驻"里的公共图标叠加进来。映射到既有 display_mode 轴——
    # 不勾='exclusive'（dock 只显示自己），勾='none'（dock 叠加 get_sticky_icon_names()），
    # 复用已测的并集逻辑，核心零改动。默认不点＝用户原话"默认不点"。
    pub_v = tk.BooleanVar(value=False)
    ttk.Checkbutton(body, text=i18n.tr(
                        '包含公共图标（勾＝叠加"设置→常驻"的图标，不勾＝只显本组）',
                        'Include public icons (on = also show "Settings→Resident" icons)'),
                    variable=pub_v).grid(row=5, column=0, columnspan=4, sticky='w',
                                          pady=(2, 0))
    # ★R292-①（用户 10-04 点名"现在多个选项'不包含系统图标'，默认勾选"）：第三枚。
    # 默认勾＝新组一律不带系统图标（此电脑/回收站/网络/控制面板/用户文件夹），
    # 因为图标打开榜按"点开次数"排、这几枚常年占名次；不勾＝回到改前行为。
    # 勾选态由样式层的原生指示器表达（★R315：本窗在 Toplevel 里、主窗收尾那次
    # 递归扫不到它，历轮靠 `_mark_checkboxes` 补前缀，现无需补）。
    sys_v = tk.BooleanVar(value=True)
    ttk.Checkbutton(body, text=i18n.tr(
                        '不包含系统图标（此电脑/回收站/网络/控制面板/用户文件夹）',
                        'Exclude system icons (This PC / Recycle Bin / Network / '
                        'Control Panel / user folder)'),
                    variable=sys_v).grid(row=6, column=0, columnspan=4, sticky='w',
                                         pady=(2, 0))

    def _ok():
        from utils import i18n as _t
        try:
            m = int((m_v.get() or '').strip())
            n = int((n_v.get() or '').strip())
        except ValueError:
            # ★R279-⑤：拒在"还没走到 commit"这一段也要留痕——否则用户说"点了建组
            # 没反应"，app.log 里连一条线索都没有（产品负责人点名要日志好排查 bug）
            logger.warning(f'[排行组] 小窗校验失败: M/N 非整数 '
                           f'm={m_v.get()!r} n={n_v.get()!r}')
            messagebox.showwarning(_t.tr('参数有误', 'Invalid input'),
                                   _t.tr('M、N 都要填整数', 'M and N must be integers'),
                                   parent=dlg)
            return
        try:
            _names, icons, desc = rank_snapshot(gui, dim_v.get(), m, n)
        except ValueError as e:
            logger.warning(f'[排行组] 小窗校验失败: 榜单算不动 '
                           f'm={m} n={n} board={dim_v.get()} — {e}')
            messagebox.showwarning(_t.tr('建不了组', 'Cannot create group'), str(e),
                                   parent=dlg)
            return
        if not icons:
            logger.warning(f'[排行组] 小窗校验失败: 区间无命中 '
                           f'm={m} n={n} board={dim_v.get()}')
            messagebox.showinfo(_t.tr('提示', 'Info'),
                                _t.tr('这个区间没命中任何桌面图标（图标可能已改名或移除）。',
                                      'This range matches no desktop icons.'),
                                parent=dlg)
            return
        holder['spec'] = {'name': name_v.get(), 'icons': icons, 'color': color_v.get(),
                          'board': dim_v.get(), 'm': m, 'n': n,
                          'in_dock': bool(dock_v.get()),
                          'include_public': bool(pub_v.get()),
                          'exclude_system': bool(sys_v.get()), 'desc': desc}
        dlg.destroy()

    row = ttk.Frame(dlg)
    row.pack(pady=(0, 12))
    ttk.Button(row, text=i18n.tr('✔ 建组', '✔ Create'),
               command=_ok).pack(side=tk.LEFT, padx=5)
    ttk.Button(row, text=i18n.tr('取消', 'Cancel'),
               command=dlg.destroy).pack(side=tk.LEFT, padx=5)
    # ★R279-⑤（产品负责人 09-30 04 批 item6，附截图）：这枚小窗里
    # 「常驻：显示在顶部悬浮栏」看不见勾选框、两枚榜源单选看不见圆圈，用户读成
    # "应该是勾选项，不能勾选"。根因不在小窗：历轮样式层把 ttk 的原生指示器整层
    # 去掉了（★R222 定案——clam 主题下原生勾画出来像 ✗），勾选态全靠
    # manage_panel 给文字挂 ☑/☐、●/○ 前缀；而前缀那把只在主窗 `_build_ui` 收尾扫
    # 一次 root ⇒ 之后建的任何 Toplevel 一律"看不见选没选"（★R269 给另外两枚
    # 对话框踩过同一个坑）。
    # ★R315（批71，挂账 #272）：勾选框那半**已不需要借助手**——原生指示器回到样式
    # 层（theme_panel 的"色块档"），控件出生就带勾选态；单选钮那半还靠文字前缀，
    # 所以这里只借 `_mark_radios`。口径不变：getattr 容错、整段包 try，借助手失败
    # 顶多退回"看不见圆圈"，绝不能把小窗本身建不起来。
    for _m in ('_mark_radios',):
        try:
            _fn = getattr(gui, _m, None)
            if callable(_fn):
                _fn(dlg)
        except Exception as e:
            logger.debug(f'[排行组] 小窗补选择控件前缀失败（{_m}）: {e}')
    dlg.update_idletasks()
    try:
        w, h = dlg.winfo_width(), dlg.winfo_height()
        dlg.geometry('+%d+%d' % (max(parent.winfo_x() + 80, 0),
                                 max(parent.winfo_y() + 120, 0)))
    except Exception as e:
        logger.warning(f'[新建排行组] 定位失败（按窗口管理器给的位置显示）: {e}')
    dlg.grab_set()
    parent.wait_window(dlg)
    return holder['spec']


def _group_color(gui):
    """新组默认色：按现有分类数在色板上轮转 ⇒ 连续建组不撞色（★R79 同款诉求）。"""
    try:
        n = len(gui.category_manager.get_all_categories())
    except Exception:
        n = 0
    return GROUP_PALETTE[n % len(GROUP_PALETTE)]


def create_rank_group(gui):
    """「🏆 建组上栏」按钮回调：弹小窗 → 落分类 → 上栏。统计关闭时只走状态条提示。"""
    from utils import i18n
    spec = ask_rank_group_dialog(gui)
    if not spec:
        return
    ok, msg = commit_rank_group(gui, spec['name'], spec['icons'], spec['color'],
                                 spec['board'], spec['m'], spec['n'],
                                 in_dock=spec['in_dock'],
                                 include_public=spec.get('include_public', False),
                                 exclude_system=spec.get('exclude_system', False))
    if not ok:
        messagebox.showerror(i18n.tr('建组失败', 'Cannot create group'), msg,
                             parent=gui.root)
        return
    if not gui.stats_capture.enabled:
        # 设计稿定案：采集关着不弹框，只在状态条说一句"榜单截至上次采集"
        gui._log(i18n.tr('⚠ 统计功能当前关闭，名单截至上次采集',
                         '⚠ Stats are off; the snapshot is as of the last capture'))
