# -*- coding: utf-8 -*-
"""资源管理器窗口标签（Explorer tabs）支持。

功能：
1. 枚举当前打开的文件资源管理器窗口及其路径（Shell.Application COM）
2. 保存/读取到 config/explorer_tabs.json
3. 批量打开：第一个文件夹开新窗口，其余以"新选项卡"方式并入
   Win11 选项卡合并采用原生 COM 方案（参考 ExplorerTabUtility v2.5.0）：
     - PostMessage(WM_COMMAND, 0xA21B) 新建选项卡（非键击，零焦点依赖）
     - Shell.Application.Windows() 拿 InternetExplorer COM 对象
     - IShellBrowser.GetWindow() 拿唯一 tabHWND 定位新标签（正解方案）
     - Navigate2(path) 直接 COM 导航（零键击）
   Win10/旧系统不支持选项卡，退化为 ShellExecuteW 多窗口打开。

2026-09-05 新增（用户需求：悬浮条快速打开上次的工作文件夹组）。
2026-09-10 重构：从键击模拟改为原生 COM 方案，稳定性显著提升。
"""
import logging
import re
import subprocess
import sys
import os
import time
import threading
import ctypes

from utils import i18n
from ctypes import wintypes, POINTER, byref, c_void_p, c_long
from urllib.parse import unquote, urlparse

from utils import i18n    # ★R319 K21：体检 ⚠ tooltip 要能跟着语言档走

logger = logging.getLogger("desktop_manager")

# （B14 清理）原 STATE_FILE = None "#由 gui 注入绝对路径" 为死遗留：
# 全项目零引用（load_state/save_state 均走 config_dir 参数），声明本身
# 会误导"还有注入没做"的排查，删除。

# ★ B18（S 段）gui 注入：文件夹成功打开回调（usage_stats 记录排行榜）
on_folder_opened = None


def notify_folder_open(path: str):
    """★ B18：记录一次成功的文件夹打开（gui 注入回调；未注入时静默）。"""
    try:
        if on_folder_opened:
            on_folder_opened(path)
    except Exception:
        pass

# 超时常量（秒）
# - 新建选项卡后 ShellWindows 注册到新 IE 对象的等待时间
#   Win11 快机器 1~2 秒，慢注册场景可能到 4~5 秒，留 8 秒余量
_TAB_REGISTER_TIMEOUT = 8.0
# - PostMessage 等待新 ShellTabWindowClass 出现
_NEW_TAB_POSTMESSAGE_TIMEOUT = 5.0
# - 第一个文件夹打开后等新 CabinetWClass 主窗口出现
_MAIN_WINDOW_WAIT_TIMEOUT = 8.0
# - Navigate2 COM 导航等待 ReadyState 完成
_NAVIGATE_TIMEOUT = 5.0


# 模块级 COM 初始化（线程本地存储）。
# Shell.Application 是 STA 组件；每个线程首次调用 COM 前必须 CoInitialize。
# 用 threading.local 保证线程独立（不能只看进程级标志）。
_com_state = threading.local()


def _ensure_com():
    """确保当前线程已初始化 COM（幂等，每个线程独立）。"""
    if not getattr(_com_state, 'initialized', False):
        try:
            import pythoncom
            pythoncom.CoInitialize()
            _com_state.initialized = True
        except Exception as e:
            logger.error(f'CoInitialize 失败: {e}')


def _shell_windows():
    """枚举所有资源管理器窗口的路径（file:/// URL → 本地路径）"""
    _ensure_com()
    import win32com.client
    paths = []
    try:
        shell = win32com.client.Dispatch("Shell.Application")
        for w in shell.Windows():
            try:
                url = w.LocationURL
                if not url or not url.startswith('file:///'):
                    continue
                path = unquote(urlparse(url).path).replace('/', '\\')
                if path.startswith('\\'):   # file:///E:/x 解析出 \E:\x，去掉前导反斜杠
                    path = path[1:]
                if path and os.path.isdir(path):
                    paths.append(path)
            except Exception:
                continue
    except Exception as e:
        logger.error(f"枚举资源管理器窗口失败: {e}")
    return paths


def list_open_folders(retries: int = 3) -> list:
    """当前打开的文件夹列表（去重保序；COM 冷启动偶发空，重试 3 次）"""
    out = []
    for attempt in range(retries):
        seen, out = set(), []
        for p in _shell_windows():
            if p.lower() not in seen:
                seen.add(p.lower())
                out.append(p)
        if out or attempt == retries - 1:
            break
        time.sleep(0.8)
    return out


# ★R258：「启动自动打开」合法档位（UI 下拉候选与消毒共用同一份，防漂移）
# ★R262（用户 56 批 二2）：下拉支持手动输入数字 ⇒ 消毒从"白名单"放宽为"区间"，
# 档位表只做候选值。
_STARTUP_DELAYS = (5, 10, 15, 30)          # 候选档
_STARTUP_TOPK = (3, 5, 8)                  # 候选档
_STARTUP_MAX_FOLDERS = (5, 8, 10, 15, 20)  # 候选档
_STARTUP_MAX_ICONS = (3, 5, 8, 10, 15)     # 候选档
# 手动输入的合法区间（超出折回默认，防手滑输 99999 开爆窗口）
_DELAY_RANGE = (1, 120)
_TOPK_RANGE = (1, 50)
_MAX_FOLDERS_RANGE = (1, 50)
_MAX_ICONS_RANGE = (1, 30)


def _num_in_range(raw, default, lo, hi):
    """把 raw 解析成 [lo,hi] 内的整数；野值折回 default。"""
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return v if lo <= v <= hi else default


def _k_or_all(raw, default):
    """K 值消毒：正整数（区间内）或 'all'；'全部'（界面显示值）同样认作 all。

    ★R262：下拉可手输 ⇒ 界面回显的是 '全部' 这种显示值，消毒必须认得它，
    否则用户选'全部'保存会被错折成数字档。
    """
    if str(raw).strip() in ('all', '全部'):
        return 'all'
    return _num_in_range(raw, default, *_TOPK_RANGE)


def sanitize_startup_open(raw):
    """★R258 + ★R262：「启动自动打开」配置消毒。

    字段语义：
      enabled        总开关，默认**关**
      delay_s        启动延时秒：候选 5/10/15/30，手动输入合法区间 1~120

      ★R262 三列独立开关 + 模式互斥（用户 56 批 三3）：
      groups_enabled     第1列"按组打开"是否启用，默认 False
      folders_enabled    第2列"跨组高频文件夹"是否启用，默认 False
      folders_mode       第2列取数方式：'auto'（开前K个）| 'manual'（手动勾选），
                         二选一。默认 'auto'
      top_folders_k      第2列 auto 模式开前 K 个：正整数 1~50 或 'all'，默认 3
      selected_folders   第2列 manual 模式勾选的文件夹路径列表，默认 []
      max_folders_limit  文件夹上限（第1列+第2列合计）：1~50，默认 10

      desktop_enabled    第3列"打开桌面图标程序"是否启用，默认 False
      desktop_mode       第3列取数方式：'auto' | 'manual'，默认 'auto'
      top_desktop_k      第3列 auto 模式开前 K 个：1~50 或 'all'，默认 3
      selected_desktop   第3列 manual 模式勾选的图标名列表，默认 []
      max_icons_limit    桌面图标上限（第3列）：1~30，默认 5

      groups         勾选的组名列表，**勾选顺序＝打开顺序**
      smart_recent   🏆 最常用组（自动＝最近使用排名第 1）
      top_k          组内只开前 K 个：正整数或 'all'

    任何野值（导入来的包、手改的 json）一律折回默认，不炸启动路径。
    """
    out = {'enabled': False, 'delay_s': 15,
           'groups_enabled': False, 'folders_enabled': False,
           'folders_mode': 'auto',
           'top_folders_k': 3, 'selected_folders': [],
           'max_folders_limit': 10,
           'desktop_enabled': False, 'desktop_mode': 'auto',
           'top_desktop_k': 3,
           'selected_desktop': [], 'max_icons_limit': 5,
           'groups': [], 'smart_recent': False, 'top_k': 'all'}
    if not isinstance(raw, dict):
        return out
    out['enabled'] = bool(raw.get('enabled'))
    out['delay_s'] = _num_in_range(raw.get('delay_s', 15), 15, *_DELAY_RANGE)
    out['smart_recent'] = bool(raw.get('smart_recent'))
    _k = raw.get('top_k', 'all')
    out['top_k'] = _k_or_all(_k, 'all')
    seen, gs = set(), []
    for g in (raw.get('groups') or []):
        if isinstance(g, str) and g and g not in seen:
            seen.add(g)
            gs.append(g)
    out['groups'] = gs

    # ★R262 新增字段
    out['groups_enabled'] = bool(raw.get('groups_enabled'))
    out['folders_enabled'] = bool(raw.get('folders_enabled'))
    out['folders_mode'] = raw.get('folders_mode') if raw.get(
        'folders_mode') in ('auto', 'manual') else 'auto'
    out['top_folders_k'] = _k_or_all(raw.get('top_folders_k', 3), 3)
    out['selected_folders'] = [p for p in (raw.get('selected_folders') or [])
                               if isinstance(p, str) and p]
    out['max_folders_limit'] = _num_in_range(
        raw.get('max_folders_limit', 10), 10, *_MAX_FOLDERS_RANGE)

    out['desktop_enabled'] = bool(raw.get('desktop_enabled'))
    out['desktop_mode'] = raw.get('desktop_mode') if raw.get(
        'desktop_mode') in ('auto', 'manual') else 'auto'
    out['top_desktop_k'] = _k_or_all(raw.get('top_desktop_k', 3), 3)
    out['selected_desktop'] = [n for n in (raw.get('selected_desktop') or [])
                               if isinstance(n, str) and n]
    out['max_icons_limit'] = _num_in_range(
        raw.get('max_icons_limit', 5), 5, *_MAX_ICONS_RANGE)

    return out


def _folder_key(p) -> str:
    """跨列去重用的路径键：大小写不敏感、去掉结尾分隔符与多余斜杠。

    排行榜里同一目录可能写成 `D:\\a\\b` 或 `D:\\a\\b\\`，组里也可能来自
    导入的包——不归一就会重复开同一个文件夹。
    """
    try:
        return os.path.normcase(os.path.normpath(str(p).strip()))
    except Exception:
        return os.path.normcase(str(p).strip())


def _drop_planned(paths, planned):
    """★R264 批17 B2c：挑出"还没被规划过"的目录，返回 (保留列表, 去掉几条)。

    顺序保持原样（组内顺序 / 排行榜名次就是用户要的打开顺序），同时吃掉
    这一批内部的自重复。`planned` 由调用方在计划落地后统一更新。
    """
    kept, seen, dropped = [], set(), 0
    for p in paths:
        k = _folder_key(p)
        if k in planned or k in seen:
            dropped += 1
            continue
        seen.add(k)
        kept.append(p)
    return kept, dropped


def resolve_startup_plan(state, usage_stats=None):
    """★R258 + ★R262：把 startup_open 配置解析成"要打开什么"——纯函数。

    返回 (plans, warns)：
      plans = [{'group': 组名|'folders'|'desktop', 'folders': [路径...]}]，
              按"第1列→第2列→第3列"顺序排列；
      warns = [大白话提示]（勾选的组被删/组空/超上限截断 ⇒ 记名跳过）。

    ★R264 批17（用户 17 批 B2c）：文件夹列之间**跨列去重**——同一个目录只在
    先规划的那一列开一次，后面的列不再重复开，并留一行说明跳过了几个。
    第3列是桌面图标名，与目录不同一套命名空间，不参与去重。
    
    关闭或老 state 没这键 ⇒ ([], [])，行为与批前逐字节相同。
    路径存在性不在这里判（`open_folders` 入口的 `_safe_folders` 才是唯一闸）。
    
    usage_stats: UsageStats 实例，只有「自动」档要读排行榜时才需要（可选，
                 为 None 时第2、3列的自动档降级为空列表并记一行说明；
                 「手动勾选」档不碰排行榜，不受它影响）。
    """
    so = sanitize_startup_open(state.get('startup_open'))
    if not so['enabled']:
        return [], []
    
    groups = state.get('groups') or []
    by_name = {g.get('name'): g for g in groups if isinstance(g, dict)}
    plans, warns, picked_groups = [], [], set()
    folders_opened = 0  # 第1列+第2列合计已开文件夹数
    icons_opened = 0    # 第3列已开图标数
    planned_folders = set()   # ★R264 批17 B2c：已被前面那一列规划走的目录

    # ---------- 第1列：按组打开 ----------
    if so['groups_enabled']:
        def _add_group(name):
            nonlocal folders_opened
            if not name or name in picked_groups:
                return
            g = by_name.get(name)
            if g is None:
                warns.append(f'⚠ 启动自动打开：组 [{name}] 已不存在，已跳过')
                return
            folders = [p for p in (g.get('folders') or []) if isinstance(p, str)]
            if so['top_k'] != 'all':
                folders = folders[:so['top_k']]
            if not folders:
                warns.append(f'⚠ 启动自动打开：组 [{name}] 没有可打开的文件夹，已跳过')
                return
            # ★R264 批17 B2c：组内自身重复 + 与前面列重复的目录都只开一次
            folders, n_dup = _drop_planned(folders, planned_folders)
            if n_dup:
                warns.append(f'⚠ 启动自动打开：组 [{name}] 有 {n_dup} 个文件夹'
                             f'已经开过了，本次不重复')
            if not folders:
                return
            # 检查全局上限
            remaining = so["max_folders_limit"] - folders_opened
            if remaining <= 0:
                warns.append(f'⚠ 启动自动打开：已达文件夹上限 {so["max_folders_limit"]} 个，'
                           f'组 [{name}] 被跳过')
                return
            if len(folders) > remaining:
                folders = folders[:remaining]
                warns.append(f'⚠ 启动自动打开：组 [{name}] 因文件夹上限截断为 '
                           f'{len(folders)} 个文件夹')
            picked_groups.add(name)
            plans.append({'group': name, 'folders': folders})
            folders_opened += len(folders)
            planned_folders.update(_folder_key(p) for p in folders)
        
        if so['smart_recent']:
            _recent = sorted_groups(groups, recent_first=True)
            if _recent and isinstance(_recent[0], dict):
                _add_group(_recent[0].get('name'))
            else:
                warns.append('⚠ 启动自动打开：还没有任何文件夹组，"最常用组"跳过')
        for nm in so['groups']:
            _add_group(nm)
    
    # ---------- 第2列：跨组高频文件夹（★R262 模式二选一）----------
    # ★R262 批15（S6-1/S6-3）：手动档根本不碰排行榜，过去整列被 `usage_stats is not None`
    # 闸在外面 ⇒ 统计模块一缺货，用户勾好的名单静默蒸发、日志零句。两种"这一列没开"
    # 现在各自有一行说明。
    if so['folders_enabled']:
        try:
            if so['folders_mode'] == 'manual':
                # 手动模式：只用用户勾选的列表，不碰排行榜
                final = list(dict.fromkeys(so['selected_folders']))
                if not final:
                    warns.append('⚠ 启动自动打开：跨组高频文件夹选了「手动勾选」'
                               '但一条都没勾，这一列跳过')
            elif usage_stats is None:
                warns.append('⚠ 启动自动打开：跨组高频文件夹是「自动」档，'
                           '但使用统计不可用，这一列跳过')
                final = []
            else:
                # 自动模式：排行榜前 K（K 可为 'all'），不掺手动勾选
                top_k = so['top_folders_k']
                all_folders = usage_stats.top_folders(
                    999 if top_k == 'all' else int(top_k))
                final = [f[0] for f in all_folders if f[0]]
            remaining = so['max_folders_limit'] - folders_opened
            final, n_dup = _drop_planned(final, planned_folders)
            if n_dup:
                warns.append(f'⚠ 启动自动打开：跨组高频文件夹有 {n_dup} 个'
                             f'已在组里开过，本次不重复')
            if remaining > 0 and final:
                if len(final) > remaining:
                    final = final[:remaining]
                    warns.append(f'⚠ 启动自动打开：跨组高频文件夹因文件夹上限截断为 '
                               f'{len(final)} 个')
                plans.append({'group': 'folders', 'folders': final})
                folders_opened += len(final)
                planned_folders.update(_folder_key(p) for p in final)
            elif remaining <= 0 and final:
                warns.append(f'⚠ 启动自动打开：已达文件夹上限 {so["max_folders_limit"]} 个，'
                           f'跨组高频文件夹被跳过')
        except Exception as e:
            warns.append(f'⚠ 启动自动打开：读取文件夹排行榜失败 ({e})，已跳过')
    
    # ---------- 第3列：打开桌面图标程序（★R262 模式二选一，独立图标上限；★R264 批17 B3 由"桌面图标排序"改名——它不做排序，是启动时打开图标目标）----------
    if so['desktop_enabled']:
        try:
            if so['desktop_mode'] == 'manual':
                final = list(dict.fromkeys(so['selected_desktop']))
                if not final:
                    warns.append('⚠ 启动自动打开：桌面图标选了「手动勾选」'
                               '但一条都没勾，这一列跳过')
            elif usage_stats is None:
                warns.append('⚠ 启动自动打开：桌面图标是「自动」档，'
                           '但使用统计不可用，这一列跳过')
                final = []
            else:
                top_k = so['top_desktop_k']
                all_icons = usage_stats.top_icons(
                    999 if top_k == 'all' else int(top_k))
                final = [ic[0] for ic in all_icons if ic[0]]
            remaining = so['max_icons_limit'] - icons_opened
            if remaining > 0 and final:
                if len(final) > remaining:
                    final = final[:remaining]
                    warns.append(f'⚠ 启动自动打开：桌面图标因图标上限截断为 '
                               f'{len(final)} 个')
                # desktop 类型：folders 字段存的是图标名，打开逻辑在调用方
                plans.append({'group': 'desktop', 'folders': final})
                icons_opened += len(final)
            elif remaining <= 0 and final:
                warns.append(f'⚠ 启动自动打开：已达桌面图标上限 {so["max_icons_limit"]} 个，'
                           f'桌面图标被跳过')
        except Exception as e:
            warns.append(f'⚠ 启动自动打开：读取桌面图标排行榜失败 ({e})，已跳过')
    
    return plans, warns


def load_state(config_dir: str) -> dict:
    """读取文件夹组状态。

    结构：{groups: [{name, folders}], active_group: 名字, as_tabs: bool}
    旧格式（顶层 folders 列表）自动迁移为名为"我的文件夹"的组。"""
    import json
    f = os.path.join(config_dir, 'explorer_tabs.json')
    state = {'groups': [], 'active_group': '', 'as_tabs': True,
            'link_switch': True}          # 切分类联动同名文件夹组
    # ★R258（用户 50 批 二1）：死键 auto_open_on_startup 退役——它"有存无消费"
    # （旧"开机自动打开"当年是**用户点名移除**的），本批起 state 不再携带它，
    # 盘上残留在下一次 save_state 时自然清掉。新功能用 startup_open 键，且
    # 老键 True **不映射为开**：默认关，开不开由用户在资源页新卡上重新决定，
    # 不能借尸还魂一个被撤过的行为。
    state['startup_open'] = sanitize_startup_open(None)
    try:
        with open(f, encoding='utf-8') as fh:
            data = json.load(fh)   # ★ 句柄用 with 包裹，修复漏关（与 P1-N3 对齐）
        if isinstance(data, dict):
            if isinstance(data.get('groups'), list) and data['groups']:
                state['groups'] = [g for g in data['groups']
                                   if isinstance(g, dict) and g.get('name')]
            elif isinstance(data.get('folders'), list) and data['folders']:
                # 旧格式迁移
                state['groups'] = [{'name': '我的文件夹',
                                    'folders': data['folders']}]
            state['as_tabs'] = bool(data.get('as_tabs', True))
            state['link_switch'] = bool(data.get('link_switch', True))
            state['startup_open'] = sanitize_startup_open(
                data.get('startup_open'))
            if 'startup_open' not in data and data.get('auto_open_on_startup'):
                logger.info('[文件夹组] 旧"开机自动打开"死开关已由资源页'
                            '「🚀 启动自动打开」新卡替代：默认关，需要请去那里开')
            state['active_group'] = data.get('active_group', '')
    except FileNotFoundError:
        pass   # ★R190：首启无配置=正常空状态，必须允许写入
    except json.JSONDecodeError as e:
        # ★R190（Qoder CORE-5）：文件存在但读坏=标记拒写，防空组覆盖
        logger.error(f'读取文件夹组配置失败（返回空状态，原文件未动）: {e}')
        state['_load_failed'] = True
    except Exception as e:
        # ★ R130：静默吞掉=组"凭空消失"且后续保存会用空状态覆盖原文件，
        #   至少把异常留在日志里（原文件本次未动，未覆盖）
        logger.error(f'读取文件夹组配置失败（返回空状态，原文件未动）: {e}')
        state['_load_failed'] = True   # save_state 见此标记拒写
    if not state['active_group'] and state['groups']:
        state['active_group'] = state['groups'][0]['name']
    # 悬空修正：active 指向不存在的组时回退第一组（可由导入产生）
    _names = [g.get('name') for g in state['groups'] if isinstance(g, dict)]
    if state['active_group'] and _names and state['active_group'] not in _names:
        state['active_group'] = _names[0]
    # 每组"点分类自动打开"（2026-09-05 用户要求分类级控制）：
    # ★R232（用户 06 批 item2）：**默认改为关**。原来缺字段时继承全局
    # link_switch（=True），等于"没勾过的组也被自动打开"，正是用户抱怨的
    # "默认不要去点取开启"。盘上**显式写了 true 的组不动**——那是用户自己
    # 点出来的选择，本批只改默认值、不改已保存的选择。
    for g in state['groups']:
        if 'auto_open' not in g:
            g['auto_open'] = False
        # ★R262 批14（用户 14 批 问题1）：历史数据里"浏览选择"存进来的是
        # askdirectory 的正斜杠口径（D:/a/b），与 COM 枚举/手输的反斜杠不一
        # 致 ⇒ 读入统一 normpath（盘上下次 save_state 自然收敛）
        if isinstance(g.get('folders'), list):
            g['folders'] = [os.path.normpath(p) if isinstance(p, str) and p
                            else p for p in g['folders']]
    return state


def rescue_corrupt_file(config_dir, state):
    """★R231（用户 05 批 item2）：损坏的 explorer_tabs.json 先另存留底再解禁。

    与 `CategoryManager.rescue_corrupt_file` 同口径：`save_state` 的 ★R190
    CORE-5 拒写闸防的是"用内存空组**偷覆盖**真实配置"，而用户主动「清空文件夹组
    /导入文件夹组」不属于偷覆盖。本文件此前**连 .bak 逻辑都没有**——损坏原件会
    被下一次写盘直接覆盖，所以放行前必须先留底。成功时就地清掉
    `state['_load_failed']`（调用方接着用同一个 state 落盘）。
    返回 (True, 留底路径) / (False, 原因)。
    """
    if not state.get('_load_failed'):
        return True, ''
    from utils.safeio import set_aside_corrupt
    ok, detail = set_aside_corrupt(
        os.path.join(str(config_dir), 'explorer_tabs.json'))
    if not ok:
        return False, detail
    state.pop('_load_failed', None)
    logger.warning('🛠 损坏的文件夹组配置已另存留底，本会话写盘已解禁'
                   + (f' [{os.path.basename(detail)}]' if detail else ''))
    return True, detail


def save_state(config_dir: str, state: dict):
    """★R190（Qoder CORE-5）：读失败标记未清除前拒绝写入——防空组
    覆盖真实配置（文件夹组"凭空消失"事故的根治闸）。

    ★R231-6（S6 独立验收命中）：原实现只看**传进来的那个 dict** 的标记，
    而「导入全部数据」是把外部包里的 `explorer_tabs` 拷一份、`pop` 掉
    `_load_failed` 再传进来 ⇒ 闸被 pop 穿，盘上损坏原件被**静默覆盖且无留底**。
    本批把该入口分类侧从"整个入口中止"改成"先留底再放行"之后，这条路径
    才第一次可达（★R204 GUIB-6 旧写法根本走不到这一节）。
    因此补一条**无状态**判据：写之前先看盘上那份本身读不读得动——读不动就不写。
    自救留底（`rescue_corrupt_file`）后原件已被挪走 ⇒ 自然放行；首启无文件
    （FileNotFoundError）照旧放行（★R190 原意）。
    """
    if state.get('_load_failed'):
        logger.error('[文件夹组] 配置上次读取失败，拒绝写入以防清空；'
                     '可在「📦 数据管理」页清空或导入（会自动留底后放行）')
        return False
    import json
    _f = os.path.join(str(config_dir), 'explorer_tabs.json')
    try:
        with open(_f, encoding='utf-8') as _fh:
            _cur = json.load(_fh)
        if not isinstance(_cur, dict):
            # 本程序写出的永远是 dict；顶层不是 dict ⇒ 不是我们的数据，
            # 和"读不动"同等对待（load_state 对这种文件不报错但等于无配置）
            raise ValueError(f'顶层不是对象({type(_cur).__name__})')
    except FileNotFoundError:
        pass
    except (OSError, UnicodeDecodeError) as e:
        # 真·读不动（占用/编码）：不因此拒写（保持 ★R190 以前的行为口径）
        logger.warning(f'[文件夹组] 写前预检无法读取现有配置（忽略）: {e}')
    except Exception as e:
        logger.error(f'[文件夹组] 盘上现有配置解析失败，拒绝写入以免无留底覆盖: {e}')
        return False
    from utils.safeio import atomic_write_json
    return atomic_write_json(_f, state, indent=2)


def get_active_folders(state: dict) -> list:
    """取当前激活组的文件夹列表（无激活组则取第一组）"""
    groups = state.get('groups', [])
    if not groups:
        return []
    active = state.get('active_group')
    for g in groups:
        if g.get('name') == active:
            return g.get('folders', [])
    return groups[0].get('folders', [])


def set_active_group(config_dir: str, name: str):
    state = load_state(config_dir)
    state['active_group'] = name
    save_state(config_dir, state)


def touch_group(config_dir: str, name: str):
    """记录组刚被使用（菜单排序：最近使用的排前面）"""
    import time as _t
    state = load_state(config_dir)
    for g in state.get('groups', []):
        if g.get('name') == name:
            g['last_used'] = _t.time()
            break
    save_state(config_dir, state)


def sorted_groups(groups, recent_first=True):
    """按最近使用排序（没用过的按名字排最后），供菜单/界面展示。

    ★ P2-12 当 recent_first=False 时改为按名字排序（此前 pass 导致参数无效）。"""
    import time as _t
    if not recent_first:
        return sorted(groups, key=lambda g: g.get('name', ''))
    def keyf(g):
        lu = g.get('last_used', 0)
        return (0, -lu) if lu else (1, 0)
    return sorted(groups, key=keyf)


def tabs_supported() -> bool:
    """资源管理器选项卡需要 Windows 11 22H2（build 22621）及以上。
    Win10 的 Explorer 没有选项卡：Ctrl+T 无效，后续文件夹会挤在
    同一个窗口互相覆盖——必须检测并退化为多窗口。"""
    try:
        return sys.getwindowsversion().build >= 22621
    except Exception:
        return False


# ============================================================
# 原生 COM 方案（参考 ExplorerTabUtility v2.5.0）
# ============================================================

# WM_COMMAND 魔法消息常量（逆向自 explorer.exe，等价于 Ctrl+T）
_WM_COMMAND = 0x0111
_CMD_NEW_TAB = 0xA21B       # 新建选项卡（等价 Ctrl+T）


# SHELLEXECUTEINFOW 结构（模块级定义，避免每次调用重新定义类）
# 注意：不使用 SEE_MASK_NOCLOSEPROCESS，避免 hProcess 句柄泄漏
_SW_SHOWNORMAL = 1

# shell32 单例（use_last_error=True 让 ctypes 保存 Win32 last error，
# 用 ctypes.get_last_error() 读取）
_shell32 = ctypes.WinDLL('shell32', use_last_error=True)


class _SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ('cbSize', wintypes.DWORD),
        ('fMask', wintypes.ULONG),
        ('hwnd', wintypes.HWND),
        ('lpVerb', wintypes.LPCWSTR),
        ('lpFile', wintypes.LPCWSTR),
        ('lpParameters', wintypes.LPCWSTR),
        ('lpDirectory', wintypes.LPCWSTR),
        ('nShow', ctypes.c_int),
        ('hInstApp', wintypes.HINSTANCE),
        ('lpIDList', wintypes.LPVOID),
        ('lpClass', wintypes.LPCWSTR),
        ('hkeyClass', wintypes.HKEY),
        ('dwHotKey', wintypes.DWORD),
        ('hIconOrMonitor', wintypes.HANDLE),
        ('hProcess', wintypes.HANDLE),
    ]

    def __init__(self, **kw):
        super().__init__(cbSize=ctypes.sizeof(self), **kw)


def _find_explorer_main_window():
    """找到 CabinetWClass 主窗口（兜底定位用，主路径是 open_folders 里的窗口差集）。
    取选项卡数最多的窗口（用于已打开多窗口场景兜底）。"""
    import win32gui
    hwnds = []

    def _cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        try:
            cls = win32gui.GetClassName(hwnd)
            if cls in ('CabinetWClass', 'ExploreWClass'):
                hwnds.append(hwnd)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(_cb, None)
    if not hwnds:
        return None

    # 选项卡数最多的窗口（最可能是主工作窗口）
    def _tab_count(h):
        n, hwnd = 0, 0
        while True:
            hwnd = win32gui.FindWindowEx(h, hwnd, 'ShellTabWindowClass', None)
            if hwnd == 0:
                break
            n += 1
        return n
    hwnds.sort(key=_tab_count, reverse=True)
    return hwnds[0]


def _get_child_tabs(parent_hwnd):
    """获取主窗口下的所有 ShellTabWindowClass 子窗口句柄（每个选项卡一个）"""
    import win32gui
    tabs = []
    hwnd = 0
    while True:
        hwnd = win32gui.FindWindowEx(parent_hwnd, hwnd, 'ShellTabWindowClass', None)
        if hwnd == 0:
            break
        tabs.append(hwnd)
    return tabs


def _snap_explorer_hwnds():
    """快照当前所有可见的 CabinetWClass/ExploreWClass 窗口句柄集合。
    用于 open_folders 在 ShellExecuteExW 前后做差集定位新窗口。"""
    import win32gui
    s = set()

    def _cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        try:
            cls = win32gui.GetClassName(hwnd)
            if cls in ('CabinetWClass', 'ExploreWClass'):
                s.add(hwnd)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        pass
    return s


def _get_shell_windows():
    """获取 Shell.Application.Windows() COM 集合（模块级单例缓存）。

    Shell.Application 是 STA 组件，必须初始化 COM（_ensure_com 统一处理）。
    缓存对象在同一线程内可复用；若 explorer 崩溃重启，
    下次调用会因 COM 异常自动重建缓存（Count 调用抛异常时清空）。

    注意：Count==0 是合法状态（用户没打开任何资源管理器窗口），
    不是"崩溃后空壳"——不应在此处重建。"""
    # ★ P1-4：Shell.Application 是 STA 组件，COM 对象不能跨线程复用。
    # 本项目 open_folders 每次在新建的 daemon 线程中调用——若用全局缓存，
    # 第二次打开会复用它首个线程创建的 COM 对象，跨套间调用易触发
    # RPC_E_WRONG_THREAD 或静默失败。故缓存收进 _com_state（thread_local），
    # 每线程独立 Dispatch（首调开销 ~几 ms，可忽略）。
    # explorer 崩溃后 COM 服务器失效：Count 探测失败即清掉**本线程**缓存，
    # 下次调用重建，不影响其他线程。
    _ensure_com()
    cache = getattr(_com_state, 'shell_windows', None)
    if cache is not None:
        try:
            # 验证缓存仍可用（explorer 崩溃后 COM 服务器失效会抛异常）
            _ = cache.Count
            return cache
        except Exception:
            logger.debug('ShellWindows 缓存失效（explorer 崩溃？），重建')
            _com_state.shell_windows = None
    try:
        import win32com.client
        shell = win32com.client.Dispatch('Shell.Application')
        _com_state.shell_windows = shell.Windows()
        return _com_state.shell_windows
    except Exception as e:
        logger.error(f'Shell.Application.Windows() 失败: {e}')
        return None


# IShellBrowser GUID = {000214E2-0000-0000-C000-000000000046}
# 注意：这个值同时也是 SID_STopLevelBrowser（Windows 历史巧合），
# 所以 QueryService 的两个 GUID 参数用的是同一个值。
# 用不可变 c_ubyte 数组而非 create_string_buffer（语义上应不可变）。
_ISHELLBROWSER_GUID_BYTES = (ctypes.c_ubyte * 16)(
    0xE2, 0x14, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00,
    0xC0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46,
)

# tabHWND 定位失败的累计计数器（仅诊断用，输出后清零）
# key: 失败类别（repr_parse / query_service / getwindow_zero / exception）
# value: 累计发生次数
# 首次失败时按 _warn_once 指定的 level 输出日志（warning 或 debug），
# 后续同类失败只累加不再刷屏。
_tab_hwnd_warned = {}


def _release_com(ptr):
    """通过 vtable[2] = IUnknown.Release() 释放 COM 接口。
    ptr 是底层 COM 接口指针（ctypes.c_void_p.value）。"""
    try:
        vtable = ctypes.cast(ptr, ctypes.POINTER(c_void_p)).contents.value
        rel_ptr = ctypes.cast(
            vtable + 2 * ctypes.sizeof(c_void_p),
            ctypes.POINTER(c_void_p)).contents.value
        rel_func = ctypes.WINFUNCTYPE(c_long, c_void_p)(rel_ptr)
        rel_func(ptr)
    except Exception as e:
        logger.debug(f'_release_com 失败 ptr={ptr:#x}: {e}')


def _warn_once(key, msg, level='debug'):
    """同类失败只输出一次日志（首次按 level，后续只累加计数）。
    level: 'debug' | 'info' | 'warning' — 首次失败的日志级别。"""
    if key not in _tab_hwnd_warned:
        log_fn = getattr(logger, level, None)
        if log_fn is None:
            # level 拼错时兜底用 debug，避免失败路径抛新异常
            logger.debug(f'_warn_once 未知 level={level}，原文: {msg}')
        else:
            log_fn(msg)
        _tab_hwnd_warned[key] = 1
    else:
        _tab_hwnd_warned[key] += 1


def _get_tab_hwnd_from_ie(ie_obj):
    """通过 IServiceProvider.QueryService → IShellBrowser.GetWindow()
    获取 InternetExplorer 对象对应的 ShellTabWindowClass 句柄。

    这是 ExplorerTabUtility v2.5.0 的核心身份标识方案：
    Win11 同一 CabinetWClass 窗口的多选项卡共享同一个 InternetExplorer
    COM 对象（相同底层地址），但 ShellWindows 会把它多次引用（不同
    URL/Name），所以 repr_obj_at 差集法永远找不到新标签。

    IShellBrowser.GetWindow() 返回每个选项卡的 ShellTabWindowClass HWND，
    一一对应，不依赖末尾假设、不依赖 repr 解析、不依赖 URL 唯一。

    实测验证：4 个 IE 对象 → 4 个不同 tabHWND，完全正确。
    （2026-09-10 / Win11 / ShellWindows.Count=4）
    其他 Windows 版本需重跑 diag_verify.py 确认。

    返回 tabHWND（int）或 None（获取失败时）。
    """
    isp = None
    try:
        import pythoncom
        ole = ie_obj._oleobj_

        # Step 1: QueryInterface IServiceProvider
        isp = ole.QueryInterface(pythoncom.IID_IServiceProvider)
        m = re.search(r'obj at (0x[0-9a-fA-F]+)', repr(isp))
        if not m:
            # 硬失败：repr 解析失效（pywin32 版本变更？）→ 整套 tabHWND 方案塌
            _warn_once('repr_parse',
                       '_get_tab_hwnd_from_ie: IServiceProvider repr 解析失败（pywin32 版本变更？）',
                       level='warning')
            return None
        sp_ptr = int(m.group(1), 16)

        # Step 2: 拿 QueryService 函数指针
        vtable_ptr = ctypes.cast(
            sp_ptr, ctypes.POINTER(c_void_p)).contents.value
        qs_func_ptr = ctypes.cast(
            vtable_ptr + 3 * ctypes.sizeof(c_void_p),
            ctypes.POINTER(c_void_p)).contents.value
        qs_func = ctypes.WINFUNCTYPE(
            c_long, c_void_p, c_void_p, c_void_p, c_void_p)(qs_func_ptr)

        # Step 3: QueryService → IShellBrowser
        ppv = c_void_p()
        hr = qs_func(sp_ptr,
                     byref(_ISHELLBROWSER_GUID_BYTES),
                     byref(_ISHELLBROWSER_GUID_BYTES),
                     byref(ppv))
        if hr != 0 or not ppv.value:
            # 硬失败：IShellBrowser 拿不到 → 整套 tabHWND 方案失效
            _warn_once('query_service',
                       f'_get_tab_hwnd_from_ie: QueryService hr={hr:#x}（选项卡合并将降级为多窗口）',
                       level='warning')
            return None

        try:
            # Step 4: GetWindow
            isb_vtable = ctypes.cast(
                ppv.value, ctypes.POINTER(c_void_p)).contents.value
            gw_func_ptr = ctypes.cast(
                isb_vtable + 3 * ctypes.sizeof(c_void_p),
                ctypes.POINTER(c_void_p)).contents.value
            gw_func = ctypes.WINFUNCTYPE(
                c_long, c_void_p, ctypes.POINTER(c_void_p))(gw_func_ptr)
            phwnd = c_void_p()
            gw_func(ppv.value, byref(phwnd))
            if not phwnd.value:
                _warn_once('getwindow_zero',
                           '_get_tab_hwnd_from_ie: GetWindow 返回 0')
                return None
            return phwnd.value
        finally:
            # Step 5: Release IShellBrowser
            _release_com(ppv.value)

    except Exception as e:
        _warn_once('exception', f'_get_tab_hwnd_from_ie 异常: {e}')
        return None
    finally:
        # 不能手动 Release sp_ptr——ole.QueryInterface() 返回的 PyIUnknown
        # 自己持有 COM refcount +1，pywin32 GC 时会再 Release 一次。
        # 手动 Release 后 pywin32 再 Release → double-release → refcount 掉至 0 以下，
        # 可能导致 Explorer 进程异常。所以 PyIUnknown 的生命周期完全交给 pywin32。
        # 显式置 None 只是避免函数内持续持有引用延迟 GC 时机。
        isp = None


def _locate_ie_for_new_tab(shell_windows, new_tab_hwnd, timeout=_TAB_REGISTER_TIMEOUT):
    """在 ShellWindows COM 集合中定位与指定 ShellTabWindowClass HWND
    对应的 InternetExplorer 对象。

    方案：遍历 ShellWindows，对每个 IE 对象调 IShellBrowser.GetWindow()，
    找到返回值 == new_tab_hwnd 的那个。
    - 快路径：先查 Item(count-1)（新标签通常在末尾），命中直接返回
    - 慢路径：遍历全部，每轮构造 tabHWND→ie_obj 映射，避免重复计算

    为什么这是正解：
    - IShellBrowser.GetWindow() 返回每个选项卡唯一的 ShellTabWindowClass HWND
    - 不依赖 ShellWindows.Item 顺序（count-1 依赖末尾假设）
    - 不依赖 repr_obj_at 差集（同一窗口多标签共享 obj_at 地址）
    - 不依赖 URL 唯一（同一路径可能被打开两次）

    new_tab_hwnd: PostMessage 后新出现的 ShellTabWindowClass HWND
    返回 ie_obj（找不到返回 None）
    """
    deadline = time.time() + timeout
    first_pass = True
    while time.time() < deadline:
        if not first_pass:
            time.sleep(0.3)
        first_pass = False
        try:
            count = shell_windows.Count
        except Exception:
            continue
        if count == 0:
            continue

        # 快路径：新标签在 ShellWindows 末尾（diag 已验证），
        # 先检查 Item(count-1) 的 tabHWND 是否匹配，命中直接返回
        fast_path_last = None
        try:
            ie_last = shell_windows.Item(count - 1)
            hwnd_last = _get_tab_hwnd_from_ie(ie_last)
            if hwnd_last and hwnd_last == new_tab_hwnd:
                return ie_last
            # 快路径结果带到慢路径复用，避免对 i=count-1 重复算
            if hwnd_last is not None:
                fast_path_last = (hwnd_last, ie_last)
        except Exception:
            pass

        # 慢路径：全遍历构造 {tabHWND: ie_obj} 映射
        # 快路径结果（如有）先塞入 map，再遍历剩余 IE；
        # 若快路径没拿到 hwnd（hwnd_last=None），最后一个也要在慢路径补算，
        # 所以 slow_range 取决于 fast_path_last 是否成功。
        tab_hwnd_to_ie = {}
        if fast_path_last is not None:
            tab_hwnd_to_ie[fast_path_last[0]] = fast_path_last[1]
            slow_range = range(count - 1)   # 跳过最后一个（快路径已算过）
        else:
            slow_range = range(count)       # 包括最后一个（快路径没算出）
        for i in slow_range:
            try:
                ie = shell_windows.Item(i)
                hwnd = _get_tab_hwnd_from_ie(ie)
                if hwnd is not None:
                    # 异常检测：理论上 tabHWND 应唯一，重复意味着方案前提被打破
                    if hwnd in tab_hwnd_to_ie and tab_hwnd_to_ie[hwnd] is not ie:
                        logger.warning(f'tabHWND 重复: {hwnd:#x}（方案前提被打破？）')
                    tab_hwnd_to_ie[hwnd] = ie
            except Exception:
                continue
        if new_tab_hwnd in tab_hwnd_to_ie:
            return tab_hwnd_to_ie[new_tab_hwnd]

    return None


def _open_folder_new_window(path):
    """★R264 批17（用户 17 批 B2c）：强制**新开一个资源管理器窗口**打开 path。

    Win11 默认把外部 `open` 请求并到已有窗口的标签条上（ShellExecuteExW /
    os.startfile 都一样），于是「自动打开设置」里第1列的组和第2列的高频文件夹
    会挤成**同一组选项卡**——用户报的就是这个。`explorer.exe /n,<路径>` 是
    不需要键击、不依赖焦点的唯一强制新窗口入口。
    返回 True 只表示"命令发出去了"，窗口是否真独立由调用方按窗口差集判定。
    """
    try:
        # 字符串命令形式 = 手册里的 `explorer.exe /n,"C:\路径"`（路径已在
        # `_safe_folders` 闸里确认是真实存在的目录，NTFS 不允许引号字符）
        subprocess.Popen('explorer.exe /n,"%s"' % path,
                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                         close_fds=True)
        return True
    except Exception as e:
        logger.error(f'explorer /n 打开失败（回退普通打开）: {path}: {e}')
        return False


def _open_first_folder_com(path):
    """用 ShellExecuteExW 打开第一个文件夹，返回 True/False 表示调用是否成功。
    注意：不返回 hwnd，由后续 EnumWindows 定位（因为 ShellExecuteExW 是异步的，
    返回时窗口可能还没创建完成）。"""
    info = _SHELLEXECUTEINFOW(
        fMask=0,  # 不使用 SEE_MASK_NOCLOSEPROCESS，避免 hProcess 句柄泄漏
        hwnd=None, lpVerb=None, lpFile=path,
        lpParameters=None, lpDirectory=None, nShow=_SW_SHOWNORMAL,
    )
    try:
        # use_last_error=True 让 ctypes 在调用后保存 Win32 last error
        # 必须用 ctypes.get_last_error() 读取（不是 shell32.GetLastError()，
        # 因为 ctypes 内部调用可能已覆盖 last error）
        if not _shell32.ShellExecuteExW(ctypes.byref(info)):
            err = ctypes.get_last_error()
            logger.error(f'ShellExecuteExW 失败 错误码={err}: {path}')
            return False
    except Exception as e:
        logger.error(f'ShellExecuteExW 异常: {e}')
        return False
    return True


def _open_new_tab_via_postmessage(main_hwnd, timeout=_NEW_TAB_POSTMESSAGE_TIMEOUT):
    """向主窗口发送 0xA21B 新建选项卡，等待 ShellTabWindowClass 子窗口数 +1。
    返回新选项卡的 hwnd，失败返回 None。

    探测脚本验证过发给第一个 ShellTabWindowClass 可触发新建。
    其他标签未穷举验证——如遇失败，降级为多窗口打开（由 open_folders 兜底）。"""
    import win32api
    tabs_before = _get_child_tabs(main_hwnd)
    if not tabs_before:
        logger.warning(f'主窗口 {main_hwnd} 无 ShellTabWindowClass，无法新建选项卡')
        return None
    # 探测验证：发给第一个 ShellTabWindowClass 即可（消息是窗口级的，不是标签级的）
    target_tab = tabs_before[0]
    try:
        win32api.PostMessage(target_tab, _WM_COMMAND, _CMD_NEW_TAB, 0)
    except Exception as e:
        logger.error(f'PostMessage 0xA21B 失败: {e}')
        return None
    # 等待新选项卡出现
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.2)
        tabs_after = _get_child_tabs(main_hwnd)
        if len(tabs_after) > len(tabs_before):
            new_tabs = [t for t in tabs_after if t not in tabs_before]
            if new_tabs:
                return new_tabs[0]
    return None


def _navigate_via_com(ie_obj, path, timeout=_NAVIGATE_TIMEOUT):
    """对 InternetExplorer 对象调用 Navigate2，等待导航完成。

    路径处理：直接传 Windows 路径（如 D:\\下载），不转 file:/// URL。
    参考 ExplorerTabUtility: Navigate2 接受 Windows 路径字符串，
    对中文、空格等 Unicode 字符能正确处理；
    但对含 # 的路径，# 会被当成 URL fragment 导致截断，
    必须用 Shell.NameSpace(path) 拿 Folder 对象再 Navigate2(folder)。

    返回 True 的三种情况：
    - Navigate2 抛异常 → 真失败，path 有问题 / ie_obj 失效
    - ReadyState 读异常 → ie_obj 已失效（Explorer 崩溃/COM 释放）
    - Navigate2 已发出但 ReadyState 超时未到 4 → **返回 True**
      因为 Navigate2 是异步的，消息已发出、Explorer 正在加载中，
      判失败会让降级 os.startfile(path) 再开一次 → 同一文件夹两个窗口。
    """
    abs_path = os.path.normpath(path)
    try:
        if '#' not in abs_path and '%23' not in abs_path:
            # 普通路径：直接传 Windows 路径字符串
            ie_obj.Navigate2(abs_path)
        else:
            # 含 # 的路径：# 是 URL fragment 分隔符，直接传会被截断
            # 用 Shell.NameSpace 拿 Folder 对象再传 Navigate2
            import win32com.client
            shell = win32com.client.Dispatch('Shell.Application')
            folder = shell.NameSpace(abs_path)
            if folder is not None:
                ie_obj.Navigate2(folder)
            else:
                logger.error(f'Shell.NameSpace 找不到路径: {abs_path}')
                return False
    except Exception as e:
        logger.error(f'Navigate2({path}) 异常: {e}')
        return False

    # Navigate2 已发出（无异常），等待 ReadyState=4 (COMPLETE)
    deadline = time.time() + timeout
    last_state = None
    while time.time() < deadline:
        time.sleep(0.2)
        try:
            state = ie_obj.ReadyState
            last_state = state
            if state == 4:
                return True
        except Exception as e:
            # ie_obj 读异常 → 真失败（Explorer 崩溃/COM 释放）
            logger.debug(f'ReadyState 读异常: {e}，判定导航失败')
            return False

    # ReadyState 超时未到 4，但 Navigate2 消息已发出、Explorer 正在异步加载
    # → 返回 True。判失败会触发降级 os.startfile(path)，导致同一文件夹被打开两次。
    if last_state is not None and last_state != 4:
        logger.debug(f'Navigate2({abs_path}) ReadyState 超时，最后状态={last_state}（仍返回 True，避免重复打开）')
    return True


def is_openable(p) -> bool:
    """★R233（用户 08 批 item4，★R230 挂账 1）：一条路径"当下能不能打开"的**公开**判据。

    为什么要单独公开：★R230 把"只放行真实存在的目录"这道闸只加在了
    `open_folders` 的入口（`_safe_folders`），而资源页**双击列表项**那条路径
    是直接 `os.startfile(folders[i])`，根本没经过 `open_folders` —— 同一份
    来自导入文件的 `folders` 列表有两个出口，只关了一个。双击恰好是最随手
    的那个（列表就摆在眼前）。判据抽成一份、两处共用，防止以后再漂移。

    目录一律放行（含 `\\\\?\\` 前缀：那是导出端自己加的字面路径前缀）；
    非目录 / 不存在 / 空值一律 False。异常按 False 处理（真·占用/无权限时
    宁可不打开，也不能把"能不能执行"交给猜）。
    """
    try:
        return bool(p) and os.path.isdir(p)
    except Exception:
        return False


def _safe_folders(folders):
    """★R230（用户 13 批 第 3/4 项顺手修）：只放行"当下真的存在的目录"。

    病根：文件夹组的组名与 folders 列表整份存在 config/explorer_tabs.json，
    而这份文件在"📦 数据管理 → 导入全部数据"里**原样落盘**（只校验外层结构）。
    下游 `open_folders` 又直接把它喂给 `os.startfile` / COM `Navigate2`
    （ShellExecute 语义）——于是"组"里写什么路径，点一下"一键目录"/双击组名/
    切到同名分类联动就会执行什么：填 .exe 就是运行程序，填离线盘/他人 UNC
    就是带凭据外联。软件自己的导出端只会写资源管理器路径，所以这是
    **"导入别人给的备份包"这一条路径**上的口子（README 明确鼓励换机迁移）。
    目录一律放行（含 \\\\?\\ 前缀：那是导出端自己加的字面路径前缀），
    非目录/不存在的一律跳过并记日志（不静默）。

    ★R233：判定口径挪到公开的 `is_openable`（GUI 侧双击入口共用同一份），
    本函数只保留"整表过滤 + 逐条留痕"的职责。
    """
    keep = []
    for p in folders or []:
        if is_openable(p):
            keep.append(p)
        else:
            logger.warning(f'[文件夹组] 已跳过不可打开的条目（不是现存目录）: {p!r}')
    return keep


# ★R262（用户 61 批 2）：桌面图标按名打开——「一键打开」与启动计划共用。
# 虚拟图标（此电脑/回收站等）不在桌面目录里，用 explorer 的 shell: 协议兜底。
_SHELL_ICON_TARGETS = {
    '此电脑': 'shell:MyComputerFolder', '这台电脑': 'shell:MyComputerFolder',
    '计算机': 'shell:MyComputerFolder', '电脑': 'shell:MyComputerFolder',
    '回收站': 'shell:RecycleBinFolder', '网络': 'shell:NetworkedFolder',
    '控制面板': 'shell:ControlPanelFolder',
    'this pc': 'shell:MyComputerFolder', 'my computer': 'shell:MyComputerFolder',
    'recycle bin': 'shell:RecycleBinFolder', 'network': 'shell:NetworkedFolder',
    'control panel': 'shell:ControlPanelFolder',
}


def open_desktop_icons(names):
    """按桌面图标名打开程序/文件夹：命中桌面目录文件（去扩展名对比，
    .lnk 经 ShellExecute 即双击语义）⇒ 打开；已知虚拟图标 ⇒ shell: 协议；
    其余进 missed 由调用方大白话提示。⇒ (opened 图标名list, missed 图标名list)"""
    import subprocess
    by_name = {}
    try:
        from core import icon_manager
        dirs = icon_manager._desktop_dirs()
    except Exception as e:
        logger.warning(f'桌面目录定位失败（用兜底路径）: {e}')
        dirs = [os.path.join(os.path.expanduser('~'), 'Desktop'),
                r'C:\Users\Public\Desktop']
    for d in dirs:
        try:
            for fn in os.listdir(d):
                if fn.lower() == 'desktop.ini':
                    continue
                stem = os.path.splitext(fn)[0]
                by_name.setdefault(stem.casefold(),
                                   os.path.join(d, fn))
        except OSError:
            continue
    opened, missed = [], []
    for n in names or []:
        path = by_name.get(str(n).casefold())
        try:
            if path:
                os.startfile(path)
                opened.append(n)
                if os.path.isdir(path):
                    notify_folder_open(path)
                time.sleep(0.3)
                continue
            target = _SHELL_ICON_TARGETS.get(str(n).casefold()) or \
                _SHELL_ICON_TARGETS.get(str(n))
            if target:
                subprocess.Popen(['explorer.exe', target])
                opened.append(n)
                time.sleep(0.3)
            else:
                missed.append(n)
        except Exception as e:
            logger.warning(f'桌面图标打开失败 [{n}]: {e}')
            missed.append(n)
    return opened, missed


def desktop_live_stems():
    """当下桌面上真有的图标名（去扩展名、casefold 键 → 原始显示名）。

    与 `open_desktop_icons` 用同一套枚举口径——判"在不在"和"能不能打开"
    必须是同一个真相源，否则会出现"标了失效、一点却打得开"。"""
    live = {}
    try:
        from core import icon_manager
        dirs = icon_manager._desktop_dirs()
    except Exception:
        dirs = [os.path.join(os.path.expanduser('~'), 'Desktop'),
                r'C:\Users\Public\Desktop']
    for d in dirs:
        try:
            for fn in os.listdir(d):
                if fn.lower() == 'desktop.ini':
                    continue
                stem = os.path.splitext(fn)[0]
                live.setdefault(stem.casefold(), stem)
        except OSError:
            continue
    return live


def audit_desktop_names(names):
    """★R266（批21 item3）：把"记着的名"跟桌面上真有的对一遍。

    用户原话："图标有时候存在改名了，或者图标排行榜里面它在，但实际图标
    已经不在桌面了，所以这种情况要提示下，当然也得考虑其他情况。"

    返回 {原名: None | 说明串}——None＝正常。只判不管：
      · 命中（含 shell 虚拟图标）        → None
      · 桌面上只剩一个"像它改名"的候选   → 提示改名成什么了
      · 有多个候选                       → 报候选，让用户自己认
      · 一个都没有                       → 报"已不在桌面（删了或挪走了）"
    **绝不自动改名、绝不自动删记录**：统计与勾选都是用户自己的数据，
    猜错了比不猜更毒。
    """
    from difflib import SequenceMatcher
    live = desktop_live_stems()
    out = {}
    for n in (names or []):
        key = str(n).casefold()
        if key in live or key in _SHELL_ICON_TARGETS or str(n) in _SHELL_ICON_TARGETS:
            out[n] = None
            continue
        cands = [v for k, v in live.items()
                 if k != key and (k.startswith(key) or key.startswith(k)
                                  or SequenceMatcher(None, k, key).ratio() >= 0.75)]
        if len(cands) == 1:
            out[n] = i18n.tr(
                f'桌面上没有「{n}」了，像是改名叫「{cands[0]}」',
                "There is no '%s' on the desktop any more - it looks like it "
                "was renamed to '%s'" % (n, cands[0]))
        elif len(cands) > 1:
            out[n] = i18n.tr(
                '桌面上没有「%s」了，可能是改名叫 %s 之一'
                % (n, '、'.join(cands[:3])),
                "There is no '%s' on the desktop any more - maybe it was "
                "renamed to one of %s" % (n, ', '.join(cands[:3])))
        else:
            out[n] = i18n.tr(
                f'桌面上已经找不到「{n}」（删了或挪走了）',
                "The desktop no longer has '%s' (deleted or moved away)" % n)
    return out


def audit_folder_paths(paths):
    """同款体检，管第2列的高频文件夹路径：目录还在不在。"""
    out = {}
    for p in (paths or []):
        s = str(p)
        if not s:
            continue
        if os.path.isdir(s):
            out[s] = None
        elif os.path.exists(s):
            out[s] = i18n.tr(
                f'这个路径现在是个文件，不是文件夹：{s}',
                'This path is now a file, not a folder: %s' % s)
        else:
            out[s] = i18n.tr(
                f'这个文件夹已经不在了（删除或移动了）：{s}',
                'This folder is no longer there (deleted or moved): %s' % s)
    return out



def open_folders(folders, as_tabs: bool = True, on_degrade=None,
                 new_window: bool = False):
    """打开文件夹组：第一个开新窗口，其余并入为选项卡（Win11 原生 COM 方案）。

    实现方式（零键击、零焦点依赖、零剪贴板污染）：
      1. ShellExecuteExW 打开第一个文件夹
      2. 找到主窗口 CabinetWClass → ShellTabWindowClass
      3. 循环对每个后续文件夹：
         a. PostMessage(WM_COMMAND, 0xA21B) 新建选项卡
         b. Shell.Application.Windows() 定位新增的 InternetExplorer 对象
         c. Navigate2(path) 直接 COM 导航
      4. 任一步失败 → 降级为 ShellExecuteExW 多窗口

    on_degrade(reason): 选项卡不可用时回调（系统过旧/COM 失败），
    由调用方决定如何提示用户。

    new_window=True（★R264 批17 用户 B2c）：这一批的**第一个**文件夹改用
    `explorer /n` 强制独立开窗。Win11 默认会把普通 open 并到已有窗口的标签条上，
    自动打开的几组就会挤成同一组选项卡；调用方（启动自动打开 / 悬浮条一键打开）
    每一组都传 True，组与组之间就各占一个窗口。默认的 False 保持老行为不变
    （双击组条、分类一键打开仍跟着系统的合并设置走）。"""
    try:
        return _open_folders_impl(folders, as_tabs, on_degrade, new_window)
    finally:
        # 所有 return 路径（包括早退）都会经过此汇总
        if _tab_hwnd_warned:
            summary = ', '.join(f'{k}×{v}' for k, v in _tab_hwnd_warned.items())
            logger.info(f'[选项卡] 本次 tabHWND 定位失败统计: {summary}')
            _tab_hwnd_warned.clear()


def _open_folders_impl(folders, as_tabs: bool, on_degrade,
                       new_window: bool = False) -> int:
    """open_folders 的实际实现（被 try/finally 包裹以保证汇总统计输出）。"""
    # ★R230：入口先过一遍"只留真实存在的目录"（导入来的组可能带可执行路径）
    folders = _safe_folders(folders)
    if not folders:
        return 0

    # Win10/旧系统：直接多窗口打开，跳过 COM 方案
    if as_tabs and not tabs_supported():
        if on_degrade:
            on_degrade('系统不支持（文件资源管理器选项卡需要 Windows 11 22H2）')
        as_tabs = False

    # 不合并选项卡或单文件夹：多窗口/单窗口打开
    if not as_tabs or len(folders) == 1:
        opened = 0
        for path in folders:
            try:
                # ★R264 批17 B2c：这一批要独立开窗 ⇒ 走 explorer /n，
                # 发不出去（返回 False）才回退普通 open
                if not new_window or not _open_folder_new_window(path):
                    os.startfile(path)
                opened += 1
                notify_folder_open(path)   # ★ B18
                time.sleep(0.3)
            except Exception as e:
                logger.error(f'打开 {path} 失败: {e}')
        return opened

    # === Win11 多文件夹合并选项卡路径 ===
    opened = 0

    # 1. 打开前快照现有窗口集合（用于差集定位新窗口）
    hwnds_before = _snap_explorer_hwnds()

    # 2. 打开第一个文件夹
    #    new_window=True ⇒ 走 explorer /n 强制独立窗口（★R264 批17 B2c），
    #    它发不出去才回退普通 open；False 时保持历轮行为不变
    if new_window:
        _first_ok = _open_folder_new_window(folders[0])
    else:
        _first_ok = _open_first_folder_com(folders[0])
    if not _first_ok:
        logger.error(f'第一个文件夹打开失败: {folders[0]}')
        # 兜底：os.startfile
        try:
            os.startfile(folders[0])
            opened += 1
            notify_folder_open(folders[0])   # ★ B18
        except Exception:
            pass
        # 继续打开剩余的（独立窗口）
        for path in folders[1:]:
            try:
                os.startfile(path)
                opened += 1
                notify_folder_open(path)   # ★ B18
                time.sleep(0.3)
            except Exception as e:
                logger.error(f'兜底打开 {path} 失败: {e}')
        return opened
    opened += 1
    notify_folder_open(folders[0])   # ★ B18

    # 3. 轮询等待新窗口出现（差集定位，非固定 sleep）
    # 多个新窗口时取 HWND 值最大的（刚创建的通常句柄值更大，是经验规则不是保证）
    main_hwnd = None
    deadline = time.time() + _MAIN_WINDOW_WAIT_TIMEOUT
    while time.time() < deadline:
        time.sleep(0.3)
        hwnds_after = _snap_explorer_hwnds()
        new_hwnds = hwnds_after - hwnds_before
        if new_hwnds:
            main_hwnd = max(new_hwnds)
            break
    if not main_hwnd:
        if new_window:
            # 强制新窗口没成 ⇒ 系统把这一组并进了别人家的标签条。这一条不能静默：
            # 用户看到的正是"两组挤在一个窗口里"，日志要能指认是谁干的。
            logger.warning('自动打开：系统把这一组并进了已有窗口的选项卡'
                           '（资源管理器「在选项卡中打开」设置），'
                           '这一组后续的文件夹仍并入该窗口: %s' % folders[0])
        # 差集失败兜底：取窗口数最多的（用户可能切走导致差集为空）
        main_hwnd = _find_explorer_main_window()
    if not main_hwnd:
        logger.error('打开后找不到 CabinetWClass 主窗口')
        if on_degrade:
            on_degrade('资源管理器窗口未就绪')
        # 兜底多窗口
        for path in folders[1:]:
            try:
                os.startfile(path)
                opened += 1
                notify_folder_open(path)   # ★ B18
                time.sleep(0.3)
            except Exception as e:
                # ★ R130：失败要留痕——GUI 提示"详见运行日志"，日志里得真有东西
                logger.warning(f'打开文件夹失败(降级单独开窗): {path}: {e}')
        return opened

    # 4. 获取 COM 集合（用于定位新选项卡的 InternetExplorer 对象）
    shell_windows = _get_shell_windows()
    if shell_windows is None:
        if on_degrade:
            on_degrade('Shell.Application COM 不可用')
        for path in folders[1:]:
            try:
                os.startfile(path)
                opened += 1
                notify_folder_open(path)   # ★ B18
                time.sleep(0.3)
            except Exception as e:
                # ★ R130：失败要留痕——GUI 提示"详见运行日志"，日志里得真有东西
                logger.warning(f'打开文件夹失败(降级单独开窗): {path}: {e}')
        return opened

    # 5. 循环打开后续文件夹为新选项卡
    degraded_paths = []  # 记录降级的路径，循环结束后统一回调
    for path in folders[1:]:
        try:
            # a. PostMessage 新建选项卡，拿到新 ShellTabWindowClass HWND
            new_tab_hwnd = _open_new_tab_via_postmessage(main_hwnd, timeout=_NEW_TAB_POSTMESSAGE_TIMEOUT)
            if not new_tab_hwnd:
                logger.warning(f'新建选项卡失败，降级为单独开窗: {path}')
                degraded_paths.append(path)
                try:
                    os.startfile(path)
                    opened += 1
                    notify_folder_open(path)   # ★ B18
                    time.sleep(0.3)
                except Exception as e:
                    # ★ R130：失败要留痕（同上）
                    logger.warning(f'打开文件夹失败(新建选项卡降级): {path}: {e}')
                continue
            # b. 用 IShellBrowser.GetWindow() 在 COM 集合中定位
            #    与 new_tab_hwnd 匹配的 InternetExplorer 对象（正解方案）
            ie_obj = _locate_ie_for_new_tab(
                shell_windows, new_tab_hwnd)   # 用默认 _TAB_REGISTER_TIMEOUT=8.0
            if ie_obj is None:
                # ★ 不再 startfile：新标签已创建（new_tab_hwnd 有效），startfile 会造成同文件夹两窗口。
                #   保留空白新标签让用户手动 Ctrl+W 关闭。
                logger.warning(f'COM 集合定位失败，已跳过（保留空白新标签供手动关闭）: {path}')
                degraded_paths.append(path)
                continue
            # c. Navigate2 导航
            if not _navigate_via_com(ie_obj, path, timeout=_NAVIGATE_TIMEOUT):
                # ★ 同上：新标签已存在，Navigate2 失败后不要 startfile 开第二个窗口
                logger.warning(f'Navigate2 失败（保留新标签空白供手动关闭）: {path}')
                degraded_paths.append(path)
                continue
            opened += 1
            notify_folder_open(path)   # ★ B18
            logger.info(f'选项卡合并成功: {path}')
        except Exception as e:
            logger.error(f'选项卡打开 {path} 异常: {e}')
            degraded_paths.append(path)
            # ★ 异常兜底不再 startfile：若 new_tab_hwnd 已在 try 前半段拿到，说明新标签已存在，
            #   startfile 只会多开一个窗口。保留空白标签供手动 Ctrl+W。

    # 循环结束后统一通知降级情况（避免循环内多次回调刷屏）
    if degraded_paths and on_degrade:
        on_degrade(f'部分文件夹未打开（选项卡定位失败，请手动 Ctrl+W 关闭空白标签）: '
                   f'{", ".join(degraded_paths)}')

    logger.info(f'文件夹组打开完成 {opened}/{len(folders)} 个')
    return opened
