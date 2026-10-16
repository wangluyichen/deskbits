# -*- coding: utf-8 -*-
"""★R301（批57）「📦 桌面盒子」卡（资源页，最小开工版）。

一句话：勾几个桌面图标 → 真的搬进 `用户文件夹\\桌面盒子deskbits\\<盒子名>\\`
→ 桌面上真的少掉这些条目 → 想退一键散回并按搬出时的坐标摆回原位。

本文件只画控件、只转达结果；判据与搬家全在 `core/desktop_box.py`。
刻意**没有**的：悬浮条入口（`UTILITY_SLOTS` 牵动 8 套门禁，先不碰）、
跨卷搬家、把分类切换换成搬家（等取证数字再谈）。
"""
import logging
import os
import tkinter as tk
import weakref
from tkinter import messagebox, ttk

from gui import ui_layout
from utils import i18n, log_i18n

logger = logging.getLogger("desktop_manager")

# ★R302：建分类时同名建盒的钩子多半由**别的页面**触发（管理页/拖拽窗口），
# 那些调用方手里没有主窗对象，所以卡片自己登记一份，建完盒直接回头刷新。
_CARD_REFS = []


def _register_card(gui):
    try:
        if gui is not None and not any(r() is gui for r in _CARD_REFS):
            _CARD_REFS.append(weakref.ref(gui))
    except TypeError:
        pass


def _refresh_cards():
    """刷新所有还活着的盒子卡（已销毁的顺手从登记表摘掉）。"""
    alive = []
    for ref in list(_CARD_REFS):
        g = ref()
        if g is None:
            continue
        alive.append(ref)
        card = getattr(g, 'box_card', None) or {}
        fn = card.get('refresh')
        if not fn:
            continue
        try:
            fn()
        except Exception as e:
            logger.warning(f'同名建盒后刷新盒子卡失败: {e}')
    _CARD_REFS[:] = alive


def _category_names(gui):
    """当前分类名（给「新建」下拉当候选，省得用户自己取名）。读不到就手输。"""
    cm = getattr(gui, 'category_manager', None) if gui is not None else None
    if cm is None:
        return []
    try:
        return [str(c.get('name') or '') for c in cm.get_all_categories()
                if c.get('name')]
    except Exception as e:
        logger.warning(f'读取分类名单失败（盒子卡的新建框只能手输）: {e}')
        return []


def auto_box_for_category(name, gui=None):
    """★R302：建了分类就顺手建一个**同名盒子**（用户点名"省得取名"）。

    只做加法：盒子已存在、名字不合法、建盒失败都只往日志留一行，**不改**调用方
    建分类的结果，也**绝不删**盒子（删仍由用户点「删空盒」，改名同理不动）。
    → (ok, 说明)
    """
    from core import desktop_box
    box = str(name or '').strip()
    try:
        ok, why = desktop_box.create_box(box)
        if not ok:
            logger.warning(f'同名建盒没成：{box}（{why}）')
            return False, why
        logger.info(f'已随分类同名建盒: {box}')
        if gui is not None:
            try:
                gui._log(i18n.tr('📦 已随分类同名建盒：%s' % box,
                                 '📦 Box created with the category name: %s' % box))
            except Exception:
                pass
        _refresh_cards()
        return True, box
    except Exception as e:
        logger.warning(f'同名建盒异常：{box}: {e}')
        return False, str(e)


def place_phrase(place, n_back):
    """★R304：散回摆位回执 → 一句人话。三种情况分开说，**不许**无条件说"已摆好"。

    place 是 `IconManager.apply_positions_settled` 的返回 dict；读不到（老签名
    调用方回 int、或根本没摆位）时退成"位置没动"的诚实说法。
    """
    if not isinstance(place, dict):
        return i18n.tr('已搬回桌面 %d 枚（位置按桌面当前空位放，没回到原格）。' % n_back,
                       'Returned %d icons (placed in free spots, not their old cells).'
                       % n_back)
    miss = list(place.get('missing') or []) + list(place.get('no_room') or []) \
        + list(place.get('unverified') or [])
    adj = int(place.get('adjusted') or 0)
    landed = int(place.get('moved') or 0) + int(place.get('already') or 0)
    if miss:
        return i18n.tr('已搬回桌面 %d 枚：%d 枚回到了原位置，%d 枚暂时没放下'
                       '（桌面视图还没刷新或原格已被占满，再点一次「散回桌面」'
                       '或按 F5 后重试）：%s' % (
                           n_back, landed, len(miss), '、'.join(miss[:5])),
                       'Returned %d icons: %d back at their old spot, %d not placed '
                       'yet: %s' % (n_back, landed, len(miss), ', '.join(miss[:5])))
    if adj:
        return i18n.tr('已搬回桌面 %d 枚，全部回到原位置（其中 %d 枚原来那格已有人占着，'
                       '已挪到最近的空格）。' % (n_back, adj),
                       'Returned %d icons to their old spots (%d had their cell taken, '
                       'moved to the nearest free cell).' % (n_back, adj))
    if landed:
        return i18n.tr('已搬回桌面 %d 枚，全部回到原位置。' % n_back,
                       'Returned %d icons, all back at their old spots.' % n_back)
    return i18n.tr('已搬回桌面 %d 枚（这次没改任何坐标）。' % n_back,
                   'Returned %d icons (no coordinates changed this time).' % n_back)


_place_phrase = place_phrase


def _fmt_refused(res, limit=5):
    """把 refused/blocked 收成两三行短句（弹窗与状态行共用）。"""
    items = res.get('refused') or res.get('blocked') or []
    if not items:
        return ''
    head = items[:limit]
    tail = ('' if len(items) <= limit
            else i18n.tr('…还有 %d 条' % (len(items) - limit),
                         '…and %d more' % (len(items) - limit)))
    # ★R319 K9：core 给的 reason 是**中文整句**，旧写法把它裸着拼进弹窗 ⇒ English 档
    # 当面一屏中文（词典里那些整句规则一直在，只是没人调用）。中文档 translate() 原样
    # 返回、tr 取中文支 ⇒ 输出逐字等于修前。
    sep = i18n.tr('：', ': ')
    return '\n'.join(
        '%s%s%s' % (it.get('name') or it.get('file', ''), sep,
                    # ★R318 K7：自动收纳那条链给的是 'why'，只读 'reason' 会让
                    # 弹窗条目尾巴挂空——用户看见"A 组："却不知道为何没搬。
                    log_i18n.translate(it.get('reason') or it.get('why', '')))
        for it in head) + (('\n' + tail) if tail else '')


def _unclaimed_of(box):
    """★R320（用户 P0「图标丢失」）：这只盒里"盘上有名目、账本没登记"的条目名。

    判据来自 `visibility_router.audit_box_root()`——它读的是 `os.listdir`，不是账本自述
    （这次事故就是账本自述说"盒里没有东西"、盘上却躺着 29 枚图标）。
    ★R320 S5b（独立验收 D1）：登记过的盒**也要报**——报的是成员账本没记上的那几枚
    （`unrecorded`）。旧写法对登记盒一律返回空 ⇒ 「搬回未认领的」对它们永远灰着，
    而界面那句「还有 N 枚没被认领，点按钮」正是从同一个读数来的：等于给用户一张开
    不了的锁。「显示全部」对这一格只点名不自动搬（★R305 A8），动手的口子就在这枚按钮。
    **本身不抛**：这一句是界面上的加分项，不许因为对账读不动把整张卡片搞成空白。
    """
    if not box:
        return []
    try:
        from core import visibility_router as _vr
        for _b in (_vr.audit_box_root().get('boxes') or []):
            if _b.get('box') != box:
                continue
            if _b.get('registered'):
                return list(_b.get('unrecorded') or [])
            return list(_b.get('entries') or [])
        return []
    except Exception as e:
        logger.warning('盒 %s 的"没被认领"读数失败（界面按没有处理）: %s', box, e)
        return []


def auto_action_width(btns):
    """★R323 L2（用户「清理重复的让路后面看不见」）：一排按钮的 `-width` 按**实测自然宽**拉齐。

    Tk 把 `-width` 数成"多少个**平均字符**"，而汉字和 emoji 差不多占两个字符宽
    ⇒ 写死的数字必然裁掉长文案的尾巴（实测 secondary 档每字符 7px，本排最长那一枚
    「🧹 清理重复的图标」的自然宽比 `width=15` 换算出来的像素还大——差的就是尾巴那个字；
    ★R323 当时那枚更长的名字裁得更明显，用户原话「后面看不见」）。
    这里逐枚量 `width=0`（自然宽）要多少像素，再用
    **同一 style** 的"每加一个字符加多少像素"换算回单位数，取最大值给全场
    （等宽排版是 ★R302 C2 钉住的，不能改成各长各的）。
    → 生效后的单位数；按钮都不在位时返回 0，调用方保持原值。
    """
    btns = [b for b in btns if b is not None]
    if not btns:
        return 0
    top = btns[0].winfo_toplevel()
    _keep = [(b, b.cget('width')) for b in btns]
    _units = 0
    try:
        top.update_idletasks()
        for b in btns:
            probe = ttk.Button(b.master, text='0', style=str(b.cget('style')))
            try:
                probe.configure(width=1)
                top.update_idletasks()
                _r1 = int(probe.winfo_reqwidth())
                probe.configure(width=2)
                top.update_idletasks()
                _avg = max(1, int(probe.winfo_reqwidth()) - _r1)
                _base = _r1 - _avg
            finally:
                probe.destroy()
            b.configure(width=0)
            top.update_idletasks()
            _nat = int(b.winfo_reqwidth())
            _need = int((_nat - _base + _avg - 1) / _avg)
            _units = max(_units, max(1, _need))
        for b in btns:
            b.configure(width=_units)
    except Exception as e:
        logger.warning('按钮宽度自适应没做成（保持原 width）: %s', e)
        for b, w in _keep:
            try:
                b.configure(width=w)
            except Exception:
                pass
        return 0
    return _units


def build_box_card(parent, gui, colors):
    """在"📂 资源"标签页构建"📦 桌面盒子"卡片，返回 frame。

    ★R302 排版：三栏（左=桌面上可收纳 / 中=等宽动作列 / 右=盒里现有），
    选中行与隔行底色都走当前色板，换主题由 `ui_layout.refresh_zebra_listboxes`
    统一重刷（旧版单列表 + 系统 SystemHighlight 选中，深色系下读不清）。
    """
    from core import desktop_box

    frame = ttk.LabelFrame(
        parent, text=i18n.tr('📦 桌面盒子', '📦 Desktop Box'),
        padding='10', style='Card.TLabelframe')
    # ★R308 批64（用户"把桌面盒子这个地方变窄点，现在这个真的不好看"）：
    # 旧写法 `fill=X` 让卡片摊满整页（实测 1029px），两张表被 grid 的 weight
    # 拉到 390px 一行、路径 Entry 拉到 872px——内容没变多，全是空格子。
    # 现在卡片按**自身请求宽**收拢（≈726px），两张表停在 32 字宽。
    frame.pack(anchor=tk.W, pady=(6, 0))

    lb_font = ui_layout.app_font(10)
    _LB_W = 32        # 两张表请求宽一致（对称的底线：窗口再窄也各占一半）

    def _listbox(parent, width, height, activestyle='dotbox'):
        """两张表的同一份配色（选中行走主题色，不用系统高亮色）。"""
        return tk.Listbox(
            parent, width=width, height=height, activestyle=activestyle,
            relief=tk.FLAT, highlightthickness=0, exportselection=False,
            bg=colors['surface'], fg=colors['fg'],
            selectbackground=colors['primary'],
            selectforeground=colors['on_primary'],
            font=lb_font, borderwidth=0)

    # ---------- 行1：一枚下拉搞定"选已有盒子"和"起新名字"（★R302 用户点名两框多余） ----------
    row1 = ttk.Frame(frame)
    row1.pack(fill=tk.X)
    ttk.Label(row1, text=i18n.tr('盒子', 'Box'),
              style='Startup.TLabel').pack(side=tk.LEFT)
    # 变量必须显式挂到本卡的 master：Python 3.14 的 tkinter 里 `tk.Tk()` **不会**
    # 把 `_default_root` 换成新建的根（实测 default 仍是第一个根），裸 `tk.StringVar()`
    # 会落到别的解释器上 ⇒ 控件写的是它自己那份、代码读的是另一份（筛选框失灵）。
    box_var = tk.StringVar(master=frame)
    box_cb = ttk.Combobox(row1, textvariable=box_var, width=22,
                          style='Startup.TCombobox')
    box_cb.pack(side=tk.LEFT, padx=(4, 10))
    btn_new = ttk.Button(row1, text=i18n.tr('建盒子', 'New box'),
                         style='secondary.TButton')
    btn_new.pack(side=tk.LEFT, padx=(4, 2))
    btn_del = ttk.Button(row1, text=i18n.tr('删空盒', 'Delete empty'),
                         style='secondary.TButton')
    btn_del.pack(side=tk.LEFT, padx=2)
    # ★R323 L5（用户点名「一键清理空盒子」）：一只一只选、一只一只删，攒到最后
    # 盒根里全是历轮试出来的空壳。这一枚把**所有空的**一次收尾（判据仍在下面）。
    btn_del_all = ttk.Button(row1, text=i18n.tr('删所有空盒', 'Delete all empty'),
                             style='secondary.TButton')
    btn_del_all.pack(side=tk.LEFT, padx=2)
    auto_var = tk.BooleanVar(master=frame, value=bool(
        getattr(gui, '_app_settings', {}).get('box_auto_pack', True)))
    cb_auto = ttk.Checkbutton(row1, text=i18n.tr('自动收纳', 'Auto-sort'),
                              variable=auto_var, style='Startup.TCheckbutton')
    cb_auto.pack(side=tk.LEFT, padx=(10, 2))
    btn_refresh = ttk.Button(row1, text=i18n.tr('🔄 刷新', '🔄 Refresh'),
                             style='secondary.TButton')
    btn_refresh.pack(side=tk.RIGHT)

    # ---------- 行2：完整路径（明写＋可复制，用户点名要看得见） ----------
    row2 = ttk.Frame(frame)
    row2.pack(fill=tk.X, pady=(6, 0))
    path_var = tk.StringVar(master=frame)
    ttk.Entry(row2, textvariable=path_var, width=44,
              state='readonly').pack(side=tk.LEFT, fill=tk.X, expand=True)
    btn_copy = ttk.Button(row2, text=i18n.tr('复制路径', 'Copy path'),
                          style='secondary.TButton')
    btn_copy.pack(side=tk.LEFT, padx=(6, 0))

    # ---------- 行2.5：筛选（用户点名"不支持搜索"） ----------
    # 一张表可能上百枚，滚着找不如打两个字。两表共用这一个词：左表筛"桌面上可收纳"、
    # 右表筛"盒里现有"，各自的表头报「匹配/总数」。
    rowf = ttk.Frame(frame)
    rowf.pack(fill=tk.X, pady=(6, 0))
    ttk.Label(rowf, text=i18n.tr('筛选', 'Filter'),
              style='Startup.TLabel').pack(side=tk.LEFT)
    filt_var = tk.StringVar(master=frame)
    filt = ttk.Entry(rowf, textvariable=filt_var, width=18)
    filt.pack(side=tk.LEFT, padx=(4, 4))
    btn_clear = ttk.Button(rowf, text=i18n.tr('清除', 'Clear'),
                           style='secondary.TButton', width=5)
    btn_clear.pack(side=tk.LEFT)

    # ---------- 行3：左表 ｜ 中等宽动作列 ｜ 右表（用户点名"左右对称好看"） ----------
    # grid + uniform 让左右两列**永远等宽**（旧 pack 写法左表 expand 吃满、右表定长，
    # 一眼就看出来不对称）。中间动作列固定宽、整组垂直居中，对齐两张表的中线。
    row3 = ttk.Frame(frame)
    row3.pack(fill=tk.X, pady=(8, 0))
    row3.columnconfigure(0, weight=1, uniform='side')
    row3.columnconfigure(2, weight=1, uniform='side')
    col_l = ttk.Frame(row3)
    act = ttk.Frame(row3)
    col_r = ttk.Frame(row3)
    col_l.grid(row=0, column=0, sticky='nsew')
    act.grid(row=0, column=1, sticky='ns', padx=10)
    col_r.grid(row=0, column=2, sticky='nsew')

    lb_head = ttk.Label(col_l, text='', style='Startup.TLabel',
                        foreground=colors['text_secondary'])
    lb_head.pack(anchor=tk.W)
    list_wrap = ttk.Frame(col_l)
    list_wrap.pack(fill=tk.BOTH, expand=True)
    lb = _listbox(list_wrap, _LB_W, 8)
    lb.configure(selectmode=tk.EXTENDED)
    sb = ttk.Scrollbar(list_wrap, orient=tk.VERTICAL, command=lb.yview)
    sb.pack(side=tk.RIGHT, fill=tk.Y)
    lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    lb.configure(yscrollcommand=sb.set)

    # 三枚等宽（width 一致）+ fill=X：长短不齐的按钮贴在窄列里就是"丑"的主因
    # 这个 15 只是临时值——五枚都建完后 auto_action_width() 按实测自然宽重写
    # （★R323 L2：写死的数字会裁掉「🧹 清理重复的图标」的尾巴）
    _abtn_w = 15
    act_in = ttk.Frame(act)
    act_in.pack(expand=True)
    btn_pack = ttk.Button(act_in, text=i18n.tr('📥 装进盒子', '📥 Pack'),
                          style='primary.TButton', width=_abtn_w)
    btn_pack.pack(fill=tk.X, pady=(0, 4))
    btn_open = ttk.Button(act_in, text=i18n.tr('📂 打开盒子', '📂 Open'),
                          style='secondary.TButton', width=_abtn_w)
    btn_open.pack(fill=tk.X, pady=(0, 4))
    btn_unbox = ttk.Button(act_in, text=i18n.tr('📤 全部散回桌面', '📤 Return all'),
                           style='secondary.TButton', width=_abtn_w)
    btn_unbox.pack(fill=tk.X, pady=(0, 4))
    # ★R320（用户 P0「图标丢失」）：盒里有东西、账本却没登记（换了新版本目录/账本损坏
    # 留底之后都会这样）⇒ 程序**不敢替你搬**，但要说给你听，并把"搬"做成这一枚按钮：
    # 点了才动=授权。默认置灰，只有当前盒确实有没被认领的名目时才亮。
    btn_reclaim = ttk.Button(act_in, text=i18n.tr('📥 搬回未认领的', '📥 Reclaim'),
                             style='secondary.TButton', width=_abtn_w)
    btn_reclaim.pack(fill=tk.X)
    # ★R321 J5（win11 真机 0.99-R038 那 40 对"看着就是重复"的图标）：闸门已经关上
    # （自动链再也不碰桌面上那一枚的名字），但**已经造出来的**那些得有个出路。这一枚
    # 把桌面上那些带日期时间的重复图标收进盒根下那只见得到的盒——同盘改名、一枚不删，
    # 退路是里面的 清单.json。
    # ★R324 N1（用户「这个名字可以改成"清理重复的图标"」）：这枚按钮的名字不再叫
    # 「让路名」（那是程序内部的词，用户读不懂），改叫「清理重复的图标」。
    # 一直亮着：它走"①演练 ②问一句 ③才动手"，亮着不诱人乱点（★R323 L2 口径）。
    btn_dedupe = ttk.Button(act_in, text=i18n.tr('🧹 清理重复的图标',
                                                 '🧹 Tidy duplicates'),
                            style='secondary.TButton', width=_abtn_w)
    btn_dedupe.pack(fill=tk.X, pady=(4, 0))
    # ★R323 L2：五枚的 width 换成实测值（最长那一枚的自然宽），一枚都不许裁尾
    auto_action_width([btn_pack, btn_open, btn_unbox, btn_reclaim, btn_dedupe])

    in_head = ttk.Label(col_r, text='', style='Startup.TLabel',
                        foreground=colors['text_secondary'])
    in_head.pack(anchor=tk.E)
    inbox_wrap = ttk.Frame(col_r)
    inbox_wrap.pack(fill=tk.BOTH, expand=True)
    lb_in = _listbox(inbox_wrap, _LB_W, 8, activestyle='none')
    lb_in.configure(selectmode=tk.SINGLE)
    sb_in = ttk.Scrollbar(inbox_wrap, orient=tk.VERTICAL, command=lb_in.yview)
    sb_in.pack(side=tk.RIGHT, fill=tk.Y)
    lb_in.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    lb_in.configure(yscrollcommand=sb_in.set)

    # ---------- 行4：状态一行 + 上一次结果 ----------
    status = ttk.Label(frame, text='', foreground=colors['text_secondary'])
    status.pack(anchor=tk.W, pady=(6, 0))
    detail = ttk.Label(frame, text='', foreground=colors['text_secondary'],
                       justify=tk.LEFT, wraplength=520)
    detail.pack(anchor=tk.W)

    state = {'items': [], 'boxes': [], 'shown': [],
             'inbox': [], 'shown_in': []}

    def _positions():
        im = getattr(gui, 'icon_manager', None)
        if im is None:
            return {}
        try:
            return im.get_all_icon_positions()
        except Exception as e:
            logger.warning(f'读取桌面坐标失败（收纳仍可做，散回时不摆位）: {e}')
            return {}

    def _pick(labels, word):
        """留下匹配行的**原下标**：筛选后表里的第 k 行不一定是数据的第 k 条，
        选中要能翻译回去（串位就是静默搬错文件）。"""
        return [i for i, t in enumerate(labels)
                if not word or word in t.lower()]

    def _fill(lb, labels, hint):
        """重建一张表。空表给一行提示，并把整张表置 DISABLED——
        那行不是数据，选不中才安全（否则「装进盒子」会把提示当图标搬走）。
        次序不能反：DISABLED 的 Listbox 连 `insert` 都会被 Tcl 拒掉（实测静默不写）。"""
        lb.configure(state=tk.NORMAL)
        lb.delete(0, tk.END)
        if not labels:
            lb.insert(tk.END, hint)
            lb.itemconfigure(0, foreground=colors['text_secondary'])
            lb.configure(state=tk.DISABLED)
            return
        for t in labels:
            lb.insert(tk.END, t)

    def _refresh():
        state['boxes'] = desktop_box.list_boxes()
        # 一枚下拉的候选：已有盒子在前，"还不是盒子的分类名"跟在后面
        # （★R302 同名建盒之后，分类名基本就是盒子名，剩下的那几个就是"顺手建"的捷径）
        cats = [c for c in _category_names(gui) if c not in state['boxes']]
        box_cb['values'] = list(state['boxes']) + cats
        # 打字打一半的名字**不许被刷新冲掉**（旧写法两框分立时才敢"不在名单就复位"）
        if not box_var.get().strip() and state['boxes']:
            box_var.set(state['boxes'][0])
        _on_box_change()

    def _on_box_change(*_a):
        box = box_var.get().strip()
        word = filt_var.get().strip().lower()
        state['items'] = desktop_box.list_boxable()
        names = [it['name'] for it in state['items']]
        state['shown'] = _pick(names, word)
        _fill(lb, [names[i] for i in state['shown']],
              i18n.tr('（没有匹配的图标）', '(no match)') if word
              else i18n.tr('（桌面上没有可收纳的图标）', '(nothing to pack)'))
        ui_layout.zebra_listbox(lb, colors)
        state['inbox'] = desktop_box.box_contents(box) if box else []
        disp = [desktop_box.display_name(fn) for fn in state['inbox']]
        state['shown_in'] = _pick(disp, word)
        _fill(lb_in, [disp[i] for i in state['shown_in']],
              i18n.tr('（没有匹配的文件）', '(no match)') if word
              else i18n.tr('（盒子里是空的）', '(box is empty)'))
        ui_layout.zebra_listbox(lb_in, colors)
        path_var.set(desktop_box.box_dir_path(box) if box
                     else desktop_box.get_box_root())
        _sync_heads()
        # ★R320：这一只盒里有没有"盘上有、账本没登记"的名目（=用户报的图标丢失现场）。
        # 判据走 `audit_box_root`（以 os.listdir 为准），不读账本自述。
        _stray = _unclaimed_of(box)
        if box and box not in state['boxes']:
            # 一个框之后"打字"和"选中"混在一起 ⇒ 得说清这个名字还没落地
            status.config(text=i18n.tr('📦 %s ｜ 还没建，点「建盒子」' % box,
                                       '📦 %s | not created yet, press "New box"' % box))
        elif _stray:
            status.config(text=i18n.tr(
                '📦 %s ｜ 有 %d 枚没被认领（账本里没登记它们）' % (box, len(_stray)),
                '📦 %s | %d not claimed (missing from the ledger)'
                % (box, len(_stray))))
            # ★R302 C5 那把尺子（卡内不许 >40 字的解释性长提示，用户原话"别啰嗦"）：
            # 卡片只报"几枚 + 往哪儿点"，逐枚名目与后果写在按下按钮后的确认框里，
            # 用法进 README。
            detail.config(text=i18n.tr(
                '没被认领 %d 枚，点「搬回未认领的」' % len(_stray),
                '%d not claimed - press "Reclaim"' % len(_stray)))
        elif desktop_box.is_dup_box(box):
            # ★R323 L8（用户「我到时候去删掉就行」）：这只盒只进不出，状态行直接说清
            # "程序不搬回、你自己删"，别再给人"点一下就能散回"的错觉。
            status.config(text=i18n.tr(
                '📦 %s ｜ 只进不出：程序不会把这些搬回桌面' % box,
                '📦 %s | one way only: none of these go back to the desktop' % box))
            detail.config(text=i18n.tr(
                '重复的快捷方式 %d 枚，点「打开盒子」进去自己删'
                % (len(state['inbox']) or 0),
                '%d duplicate shortcut(s) - press "Open" and delete them yourself'
                % (len(state['inbox']) or 0)))
        else:
            status.config(text=i18n.tr(
                '📦 %s ｜ 隐藏的与系统图标不参与' % (box or '—'),
                '📦 %s | hidden and system icons are excluded' % (box or '—')))
            detail.config(text='')
        # ttk 的 statespec 是**字符串列表**（['!disabled'] / ['disabled']）。
        # 写成 [('!disabled', True)] 这种元组会抛 TypeError，又被下面的 try 吞成
        # 一行 warning ⇒ 按钮状态永远不动、真机才发现（★R320 冒烟 app.log 实锤，
        # 回归闸 测试_ui/test_r320_box_orphan_reclaim.py R6e~R6h 真建卡片盯着）。
        try:
            btn_reclaim.state(['!disabled'] if _stray else ['disabled'])
        except Exception as e:
            logger.warning('「搬回未认领的」按钮状态没刷新: %s', e)
        # ★R321 J5：桌面上有没有成对的让路重复名；★R323 L8 再加上盒根下历轮
        # `~让路…` 目录里等着清理的那几枚——两档都得算数，只数桌面上那批会让按钮
        # 在该亮的时候灰着（用户报的"这个按钮是灰色的"就是这一格）。
        # ★R323 L2（用户点名「这个按钮是灰色的」）：**这枚不再按数量决定亮灰**。
        # 它走的是"①先演练 ②问一句 ③点【是】才动手"（见 _do_dedupe），一直亮着
        # 不会诱人乱点；反过来"扫到几枚才亮"会把**扫描失败**说成"没有重复"——明明
        # 有东西却灰着，用户只能读成"功能坏了"。按钮常可点，数量与失败写在状态行。
        _dup_fail = False
        try:
            _cnd = desktop_box.yield_dup_candidates()
        except Exception as e:
            logger.warning(u'扫描重复的图标失败（按钮仍可点，点了会再扫一次）: %s', e)
            _cnd = {}
            _dup_fail = True
        # ★R324 N2：`orphan`（原名已经不在了的那一枚）也算数——win11 R041 那枚
        # `HashKit_20261011_152441` 就落在这一档，从前状态行只数 same+legacy ⇒
        # 明明有一枚等着收，界面却念成"没有可以收拾的"。
        _dup = (len(_cnd.get('same') or []) + len(_cnd.get('legacy') or [])
                + len([x for x in (_cnd.get('orphan') or [])
                       if os.path.isfile(x.get('path') or '')]))
        # ★R324 N3（用户看图报的病：「桌面上还是存在盒子和隐藏模式共存…w网络工具2-4、
        # game 等等文件夹还是隐藏模式」）：盒里那几枚**永远散不回桌面**的陈旧同名枚也
        # 得算进这一格——只数桌面上那批，用户看到的仍是"没有可以收拾的"，而桌面上那
        # 几枚永远搬不进盒子。只在收纳档扫（隐藏档一个字都不动，用户定案"别弄错了"）。
        _stale = 0
        try:
            from core import visibility_router as _vr1
            if _vr1.current_mode(_vr1.load_settings()) == _vr1.MODE_BOX:
                _stale = len(_vr1.box_stale_conflicts())
        except Exception as e:
            logger.warning(u'扫盒里的陈旧同名枚失败（状态行少报一档，按钮仍可点）: %s', e)
        try:
            btn_dedupe.state(['!disabled'])
        except Exception as e:
            logger.warning(u'「清理重复的图标」按钮状态没刷新: %s', e)
        if _dup and _stale and not _stray and not desktop_box.is_dup_box(box):
            detail.config(text=i18n.tr(
                '重复 %d 枚＋盒里散不回桌面的 %d 枚，点「清理重复的图标」'
                % (_dup, _stale),
                '%d duplicate(s) + %d stuck in a box - press "Tidy"'
                % (_dup, _stale)))
        elif _stale and not _dup and not _stray and not desktop_box.is_dup_box(box):
            detail.config(text=i18n.tr(
                '盒里有 %d 枚散不回桌面（桌面上已有同名的），点「清理重复的图标」'
                % _stale,
                '%d in a box can never come back (a same-named one is on the '
                'desktop) - press "Tidy"' % _stale))
        elif _dup and not _stray and not desktop_box.is_dup_box(box):
            detail.config(text=i18n.tr(
                '重复的图标 %d 枚，点「清理重复的图标」' % _dup,
                '%d duplicate icon(s) - press "Tidy"' % _dup))
        elif _dup_fail and not _stray and not desktop_box.is_dup_box(box):
            detail.config(text=i18n.tr(
                '重复图标没扫出来，点「清理重复的图标」再扫一次',
                'Scan failed - press "Tidy" to retry'))

    def _sync_heads(*_a):
        """表头：筛了就说「匹配/总数」，左表再实时报「已选 N 枚」。"""
        word = bool(filt_var.get().strip())
        n_shown, n_all = len(state['shown']), len(state['items'])
        head = (i18n.tr('桌面上可收纳 %d/%d' % (n_shown, n_all),
                        'On desktop: %d/%d' % (n_shown, n_all)) if word
                else i18n.tr('桌面上可收纳 %d' % n_all, 'On desktop: %d' % n_all))
        sel = len([i for i in lb.curselection() if i < n_shown]) if n_shown else 0
        lb_head.config(text=head + (i18n.tr(' ｜ 已选 %d 枚' % sel,
                                            ' | %d selected' % sel) if sel else ''))
        nin, nall_in = len(state['shown_in']), len(state['inbox'])
        in_head.config(text=(i18n.tr('盒里现有 %d/%d' % (nin, nall_in),
                                     'In box: %d/%d' % (nin, nall_in)) if word
                             else i18n.tr('盒里现有 %d' % nall_in,
                                         'In box: %d' % nall_in)))

    def _selected():
        try:
            return [state['items'][state['shown'][i]]['name']
                    for i in lb.curselection() if i < len(state['shown'])]
        except Exception:
            return []

    def _plan_for(picks):
        """★R304（批60 U3）「自动收纳」：按分类把选中的图标分成若干组，
        毄组对应一枚**同名盒子**（没有的那枚在收纳那一刻顺手建，见 _do_pack）。

        ★R302 那版只认"唯一分类 + 盒子已存在"，跨分类就放弃 ⇒ 用户点名的
        "默认全部搬回娱乐"实际上每次都发生在这里。现在一次点击可以各回各盒。
        分不出来的一律进 loose（未分类），绝不替用户猜一枚盒子。
        → (分组 [(@名, [图标])], 未分类 [图标], 失败原因)"""
        if not picks:
            return [], [], '没选图标'
        cm = getattr(gui, 'category_manager', None)
        if cm is None:
            return [], [], '没有分类数据'
        try:
            cats = cm.get_all_categories() or []
        except Exception as e:
            logger.warning(f'读取分类归属失败（收纳仍按下拉里那枚盒子）: {e}')
            return [], [], '分类读不动'
        groups, loose = {}, []
        for name in picks:
            owner = next((str(c.get('name') or '') for c in cats
                          if name in (c.get('icons') or [])), '')
            if owner:
                groups.setdefault(owner, []).append(name)
            else:
                loose.append(name)      # 没归分类 ⇒ 当面点名，不替用户选
        return sorted(groups.items()), loose, ''

    def _need_box():
        box = box_var.get().strip()
        if not box:
            messagebox.showinfo(i18n.tr('还没有盒子', 'No box yet'),
                                i18n.tr('先在上面的框里起个盒子名，点「建盒子」。',
                                        'Type a box name above and press "New box".'))
            return None
        if box not in state['boxes']:
            # 下拉里能选到"还不是盒子的分类名"，也能自己打字 ⇒ 没建的名字不许当盒子用，
            # 更不能让它顺手在盒根下造一个目录
            messagebox.showinfo(i18n.tr('还没有这个盒子', 'No such box yet'),
                                i18n.tr('「%s」还没建。点「建盒子」建一下，'
                                        '或从下拉里选一个已有的。' % box,
                                        'Box "%s" is not created yet. Press "New box", '
                                        'or pick an existing one.' % box))
            return None
        return box

    def _do_create():
        name = box_var.get()
        if not name.strip():
            messagebox.showinfo(i18n.tr('请起个名', 'Name required'),
                                i18n.tr('盒子名不能空着。', 'The box name cannot be empty.'))
            return
        ok, msg = desktop_box.create_box(name.strip())
        if not ok:
            messagebox.showwarning(i18n.tr('建不了', 'Cannot create'), msg)
            return
        logger.info(f'新建盒子: {name.strip()}')
        gui._log(i18n.tr('📦 新建盒子：%s' % name.strip(),
                         '📦 New box: %s' % name.strip()))
        box_var.set(name.strip())
        _refresh()

    def _do_delete():
        box = _need_box()
        if not box:
            return
        if not messagebox.askyesno(i18n.tr('删空盒子', 'Delete empty box'),
                                   i18n.tr('删掉盒子「%s」？（只肯删空盒子，'
                                           '里面还有图标请先散回桌面）' % box,
                                           'Delete box "%s"? Only empty boxes go — '
                                           'return its icons first.' % box)):
            return
        ok, msg = desktop_box.delete_box(box)
        if not ok:
            messagebox.showwarning(i18n.tr('删不了', 'Cannot delete'), msg)
            return
        gui._log(i18n.tr('📦 删除空盒子：%s' % box, '📦 Empty box deleted: %s' % box))
        box_var.set('')
        _refresh()

    def _empty_boxes_now():
        """盒根下**此刻真的是空目录**的那几只（逐枚现读，不信账本、不信上次那份列表）。

        两条排除写死在这里：
        · `收纳·重复的`（★R323 L8 那只"只进不出"的回收盒）永远不进候选——里面装着
          用户点名的重名件，删掉等于把人家要清理的东西顺手扔掉，而且它的来路只写在
          盒里那份 `清单.json`，目录一没就什么都查不到了；
        · 目录读不动 ⇒ 当成"非空"（证明不了是空的就不许删，★R301 第 1 条同一条规矩）。
        """
        out = []
        for b in desktop_box.list_boxes():
            if desktop_box.is_dup_box(b):
                continue
            try:
                if not os.listdir(desktop_box.box_dir_path(b)):
                    out.append(b)
            except OSError as e:
                logger.warning('[盒子] 一键清理空盒：%s 的目录读不动'
                               '（当成非空，这一枚不删）: %s', b, e)
            except Exception as e:
                logger.warning('[盒子] 一键清理空盒：%s 判空失败'
                               '（当成非空，这一枚不删）: %s', b, e)
        return out

    def _do_delete_all_empty():
        """★R323 L5（用户点名「一键清理空盒子」）：一次收尾盒根下历轮攒出来的空壳。

        单只「删空盒」要选一只删一只，攒了十几只就得点十几遍；这一枚把**所有**空的
        一次删掉，但一条都没放宽：**确认框逐枚点名**（历轮教训——只写"共 N 只"人签不
        出这个字）、**非空绝不动**（`delete_box` 自己还有一道非空拒删的闸，这里是第二道）、
        **「收纳·重复的」不进候选**。删掉的是目录 + 账本登记，里面本来一枚图标都没有，
        所以没有任何文件被删。
        """
        cands = _empty_boxes_now()
        if not cands:
            messagebox.showinfo(
                i18n.tr('没有空盒子', 'No empty boxes'),
                i18n.tr('盒根下这会儿没有空盒子可删。\n'
                        '（里面有图标的盒子一枚不碰，「收纳·重复的」那只回收盒'
                        '也永远不进这个名单。）',
                        'There are no empty boxes to remove right now.\n'
                        '(Boxes with icons are never touched, and the duplicate '
                        'collector box is never on this list.)'))
            _refresh()
            return
        _zh = '、'.join(cands)
        _en = ', '.join(cands)
        if not messagebox.askyesno(
                i18n.tr('一键清理空盒子', 'Tidy empty boxes'),
                i18n.tr('删掉这 %d 只空盒子？\n\n%s\n\n'
                        '只删这会儿**真是空的**那些，里面有图标的一枚不碰；'
                        '「收纳·重复的」那只回收盒不在名单里。' % (len(cands), _zh),
                        'Delete these %d empty box(es)?\n\n%s\n\n'
                        'Only folders that are truly empty right now go - boxes '
                        'with icons stay, and the duplicate collector box is not '
                        'on this list.' % (len(cands), _en))):
            return
        done, failed = [], []
        for b in cands:
            ok, msg = desktop_box.delete_box(b)
            if ok:
                done.append(b)
            else:
                failed.append((b, msg))
        if done:
            gui._log(i18n.tr('📦 一键清理空盒子：删掉 %d 只（%s）'
                             % (len(done), '、'.join(done)),
                             '📦 Tidy empty boxes: %d removed (%s)'
                             % (len(done), ', '.join(done))))
            logger.info('[盒子] 一键清理空盒子：删掉 %d 只（%s）',
                        len(done), '、'.join(done))
        if failed:
            _wzh = '；'.join('%s：%s' % (b, m) for b, m in failed[:6])
            _wen = '; '.join('%s: %s' % (b, m) for b, m in failed[:6])
            messagebox.showwarning(
                i18n.tr('有几只没删掉', 'Some boxes stayed'),
                i18n.tr('这 %d 只没删掉（多半是这一会儿又有图标进盒了）：\n%s'
                        % (len(failed), _wzh),
                        '%d box(es) did not go (icons probably moved in just '
                        'now):\n%s' % (len(failed), _wen)))
            logger.warning('[盒子] 一键清理空盒子：没删掉的 %d 只 %s',
                           len(failed), _wzh)
        try:
            if box_var.get().strip() in cands:
                box_var.set('')
        except Exception:
            pass
        _refresh()

    def _do_pack():
        picks = _selected()
        if not picks:
            messagebox.showinfo(i18n.tr('没选图标', 'Nothing selected'),
                                i18n.tr('在左边列表里点选要收纳的桌面图标'
                                        '（按住 Ctrl 或 Shift 可多选）。',
                                        'Tick the desktop icons on the left '
                                        '(Ctrl/Shift for multiple).'))
            return
        auto_on = bool(auto_var.get())
        # 点下去那一刻下拉里选的是哪枚：下面要判断"目标是不是自动收纳改的"
        picked_box = box_var.get().strip()
        plan, loose, why = ([], list(picks), '')
        if auto_on:
            plan, loose, why = _plan_for(picks)
            if why:
                plan, loose = [], list(picks)
        if not plan:
            if auto_on and why == '':
                # 分类名单读得动，只是选中的这些**还没归到任何分类** ⇒ 自动收纳
                # 不知道放哪个盒子，就一枚都不动，也绝不擅自塞进下拉里那枚
                # （那是用户没点过的决定）。
                messagebox.showinfo(
                    i18n.tr('自动收纳没动它们', 'Auto-sort left them alone'),
                    i18n.tr('这 %d 枚还没归到任何分类，自动收纳不知道放哪个盒子。\n'
                            '先把它们归一个分类，或取消勾选「自动收纳」再按上面'
                            '那枚盒子收。\n\n%s'
                            % (len(loose), '、'.join(loose[:8])),
                            'These %d icons are not in any category, so '
                            'auto-sort has no box for them.\n'
                            'Give them a category, or untick "Auto-sort" and '
                            'use the box above.\n\n%s'
                            % (len(loose), ', '.join(loose[:8]))))
                return
            box = _need_box()
            if not box:
                return
            plan, loose = [(box, loose)], []
        pos = _positions()
        known = set(state['boxes'])
        ran, refused_all, created = [], [], []
        for box, names in plan:
            if box not in known:
                okc, _msg = auto_box_for_category(box, gui)
                if not okc:
                    refused_all.extend(
                        {'name': n, 'why': '盒子建不起来（原因见日志）'}
                        for n in names)
                    continue
                known.add(box)
                created.append(box)
            res = desktop_box.move_into_box(box, names, positions=pos)
            ran.append((box, res))
            refused_all.extend(res['refused'])
        n_moved = sum(len(r['moved']) for _b, r in ran)
        n_ref = len(refused_all)
        # ★R230b：会 % 进两份模板的变量必须**中英各算一份**，否则 English 档
        # 的弹窗/日志尾巴上挂着「枚」「、」这类汉字。
        tally_zh = '、'.join('%s %d 枚' % (b, len(r['moved']))
                             for b, r in ran if r['moved'])
        tally_en = ', '.join('%s %d' % (b, len(r['moved']))
                             for b, r in ran if r['moved'])
        multi = len(ran) > 1
        # ★R304 收尾：目标若是「自动收纳」按分类挑出来的（跟下拉那枚不一样），
        # 日志和弹窗都必须点名"是按分类选的"——否则用户以为自己搬进了选中的那枚
        # 盒子，实际上是搬去了另一枚（★R302 C14b 立的规矩，U3 重写路由时漏下了）。
        by_cat = auto_on and bool(ran) and (multi or ran[0][0] != picked_box)
        head_zh = '按分类（%s）：' % tally_zh if by_cat \
            else '装进盒子「%s」：' % (ran[0][0] if ran else '')
        head_en = 'by category (%s): ' % tally_en if by_cat \
            else 'into "%s": ' % (ran[0][0] if ran else '')
        tail_zh = '' if not created else '，顺手新建 %d 枚盒子' % len(created)
        tail_en = '' if not created else '; %d box(es) created' % len(created)
        gui._log(i18n.tr('📦 %s进了 %d 枚，拒了 %d 枚%s'
                         % (head_zh, n_moved, n_ref, tail_zh),
                         '📦 Packed %s%d moved, %d refused%s'
                         % (head_en, n_moved, n_ref, tail_en)))
        if n_moved:
            if len(created) == 1:
                extra = i18n.tr('（盒子「%s」是顺手新建的）' % created[0],
                                ' (box "%s" was just created)' % created[0])
            elif created:
                extra = i18n.tr('（顺手新建了 %d 枚盒子：%s）'
                                % (len(created), '、'.join(created)),
                                ' (%d boxes just created: %s)'
                                % (len(created), ', '.join(created)))
            else:
                extra = ''
            if multi:
                body_zh, body_en = ('按分类：%s' % tally_zh,
                                    'by category: %s' % tally_en)
            elif by_cat:
                body_zh = '按分类选的盒子「%s」%d 枚' % (ran[0][0], n_moved)
                body_en = 'the box "%s" picked by category (%d)' \
                    % (ran[0][0], n_moved)
            else:
                body_zh = '盒子「%s」%d 枚' % (ran[0][0], n_moved)
                body_en = 'into "%s" (%d)' % (ran[0][0], n_moved)
            tail_zh = ('｜未分类没动：%s' % '、'.join(loose[:8])) if loose else ''
            tail_en = (' | left alone (no category): %s'
                       % ', '.join(loose[:8])) if loose else ''
            messagebox.showinfo(
                i18n.tr('已收纳', 'Packed'),
                i18n.tr('已搬进 %s，桌面上少了这 %d 枚。%s%s'
                        % (body_zh, n_moved, extra, tail_zh),
                        'Moved %s; they are really off the desktop. %s%s'
                        % (body_en, extra, tail_en)))
            if by_cat and len(ran) == 1:
                # 下拉跟着搬到图标真正进去的那枚：否则用户下一步点「打开盒子」
                # 开的还是上一次选的旧目标，看着像"图标不见了"
                box_var.set(ran[0][0])
        if n_ref:
            messagebox.showwarning(
                i18n.tr('有几枚没搬成', 'Some were refused'),
                _fmt_refused({'refused': refused_all}))
        detail.config(text=_fmt_refused({'refused': refused_all}) if n_ref else '')
        _refresh()

    def _registered(name):
        """这枚盒子在**账本**里登记过吗（目录可能被用户删了，账本还记着）。
        只给登记过的名字补目录：下拉里能打字，没建过的名目不许顺手造一个盒子。"""
        try:
            data, failed = desktop_box.load_manifest()
            return (not failed) and name in (data.get('boxes') or {})
        except Exception as e:
            logger.warning('盒子账本读不动（当作没登记，不补建）: %s', e)
            return False

    def _do_open():
        """★R309 #362：开的是**盒根**（`桌面盒子deskbits`），不是里面某一枚；
        被误删的目录当场补空壳再开，只有真建不出来 / 真打不开才当面点名。"""
        r = desktop_box.get_box_root()
        made = []
        try:
            if not os.path.isdir(r):
                os.makedirs(r)
                made.append(r)
        except Exception as e:
            logger.error('盒子根目录补建不出来 %s: %s', r, e)
            gui._log(i18n.tr('📦 盒根建不出来：%s（原因见日志）' % r,
                             '📦 Cannot create the box root: %s (see the log)' % r))
            messagebox.showerror(
                i18n.tr('打不开', 'Cannot open'),
                i18n.tr('盒子根目录打不开，也补建不出来：\n%s\n（原因见日志）' % r,
                        'Cannot open or re-create the box root:\n%s\n'
                        '(reason is in the log)' % r))
            return
        box = box_var.get().strip()
        if box:
            d = desktop_box.box_dir_path(box, r)
            if not os.path.isdir(d) and _registered(box):
                try:
                    os.mkdir(d)
                    made.append(d)
                except Exception as e:
                    logger.warning('盒子目录补建没成（盒根照开）%s: %s', d, e)
        if made:
            gui._log(i18n.tr('📦 已补建被删掉的目录（只是空目录，没动任何文件）：%s'
                            % '、'.join(made),
                            '📦 Re-created the missing folder(s), empty folders '
                            'only, nothing else touched: %s' % ', '.join(made)))
        try:
            from core import explorer_tabs
            explorer_tabs.open_folders([r], as_tabs=False)
        except Exception as e:
            logger.error(f'打开盒子文件夹失败 {r}: {e}')
            gui._log(i18n.tr('📦 打开盒子失败：%s' % e,
                             '📦 Failed to open the box: %s' % e))
            messagebox.showerror(i18n.tr('打不开', 'Cannot open'), str(e))

    def _do_unbox():
        box = _need_box()
        if not box:
            return
        if not messagebox.askyesno(
                i18n.tr('散回桌面', 'Return to desktop'),
                i18n.tr('把盒子「%s」里的图标全部搬回桌面，并按搬出时的位置摆好？\n'
                        '桌面上若已有同名的：跟盒里这一模一样的直接覆盖；对不上的'
                        '给桌面那枚改个带日期的名让路（两份都留着，不覆盖内容）。'
                        % box,
                        'Return everything in "%s" to the desktop, back at their '
                        'old spots?\nIf the desktop already holds that name: an '
                        'exactly identical copy is replaced; a different one is '
                        'renamed with a date stamp so both stay (contents are '
                        'never overwritten).' % box)):
            return
        im = getattr(gui, 'icon_manager', None)
        # ★R304：散回必须走"等视图刷新→摆位→读回"这条，不能用裸 apply_positions——
        #   真机 R023 三次散回全是 `[apply] 目标=1 实动=0`（文件刚搬回来那枚还没进
        #   Explorer 的 ListView，索引查不到就被静默跳过），用户读成"搬回去位置不对"。
        apply_layout = getattr(im, 'apply_positions_settled', None)
        res = desktop_box.move_back(box, apply_layout=apply_layout)
        n_back, n_block = len(res['back']), len(res['blocked'])
        # ★R310（用户 08:40 第 1 条后半）：给桌面那一枚"改名让路"的那几枚必须单独
        #   说一句——用户回到桌面看见的是"多了个带日期的名字"，不点名就是凭空改名。
        _yielded = res.get('renamed') or []
        if _yielded:
            gui._log(i18n.tr(
                '📦 改名让路 %d 枚（桌面上原名给了盒里那枚，旧的那份改叫 %s）'
                % (len(_yielded), '、'.join(
                    os.path.basename(str(x.get('kept') or ''))
                    for x in _yielded[:3])),
                '📦 Renamed %d to make room (the box copy took the original name; '
                'the old one is now %s)' % (
                    len(_yielded), ', '.join(
                        os.path.basename(str(x.get('kept') or ''))
                        for x in _yielded[:3]))))
        # ★R323 L7：这里**不**念「留在盒里没动」——面板这一条是用户点名整盒散回的
        # 授权路径（`on_conflict='resolve'`，★R310 那两条：一模一样就覆盖、对不上
        # 就给桌面那枚改名让路），`kept_in_box` 恒空；念了就是根拔了线的灯。
        # 撞名留在盒里那一句在自动链那一头（`visibility_router` 的
        # 「📦 还有 %d 枚留在盒里没动」，★R321 C1 钉着）。
        gui._log(i18n.tr('📦 散回桌面「%s」：回了 %d 枚，%d 枚搬不回'
                         % (box, n_back, n_block),
                         '📦 Returned "%s": %d back, %d blocked'
                         % (box, n_back, n_block)))
        # ★R320 S5b（独立验收 D3）：零枚不许再只是上面那行数字。成员账本没记上的那几枚
        # （换了程序目录 / 清单被另存过）过去在这里是**彻底静默**——盒里明明有东西，
        # 点完一句「回了 0 枚，0 枚搬不回」，用户只能自己猜。理由来自 core，界面复述。
        _rz = str(res.get('reason') or '')
        if _rz and not n_back and not n_block and not res.get('gone'):
            gui._log(u'⚠ ' + log_i18n.translate(_rz))
            messagebox.showwarning(i18n.tr('一枚都没搬', 'Nothing was moved'),
                                   log_i18n.translate(_rz))
        if n_back:
            msg = _place_phrase(res.get('place'), n_back)
            messagebox.showinfo(i18n.tr('已散回', 'Returned'), msg)
            if msg:
                gui._log(i18n.tr('📦 散回摆位：%s' % msg,
                                 '📦 Return placement: %s' % msg))
        if n_block or res.get('gone'):
            txt = _fmt_refused(res)
            gone = res.get('gone') or []
            if gone:
                txt = (txt + '\n' if txt else '') + i18n.tr(
                    '盒里已没有的文件（账本自动清掉 %d 条）' % len(gone),
                    'Files no longer in the box (ledger cleaned: %d)' % len(gone))
            messagebox.showwarning(i18n.tr('有几枚没搬回', 'Some stayed in the box'),
                                   txt)
            detail.config(text=txt)
        elif _rz and not n_back:
            detail.config(text=_rz)
        else:
            detail.config(text='')
        _refresh()

    def _do_reclaim():
        """★R320（用户 P0「图标丢失」）：整盒搬回**未登记的那些**——点了才动=授权。

        「全部散回桌面」只走登记过的盒（那是本系统搬进去的），这一枚补的是另一格：
        盘上有名目、账本里没登记（换了新版本目录、或成员账本损坏被留底之后）。
        自动链不敢动它们（里面可能掺着用户手工放的文件），所以由用户点名。
        """
        from core import visibility_router as _vr
        box = box_var.get().strip()
        _left = _unclaimed_of(box)
        if not box or not _left:
            return
        if not messagebox.askyesno(
                i18n.tr('把这一盒全部放回桌面？', 'Put this box back on the desktop?'),
                # ★R322 J6（用户「这个提示是啥意思，我不太懂」）：旧文案讲机制
                # （"账本没登记 / 覆盖 / 让路"），不讲**下一步** ⇒ 翻成三行大白话：
                # 这里有什么 → 点“是”会怎样 → 撞名怎么办一句。中英两份各写各的，
                # 中文那份不插进英文那份（★R302b：英文尾巴带中文）。
                i18n.tr('盒「%s」里有 %d 枚东西，是更早的版本收进去的，现在这份记录里查不到：\n'
                        '%s\n\n'
                        '点【是】：这一盒全部放回桌面，各回它搬走前占的那一格。\n'
                        '点【否】：一个都不动，这些东西继续留在盒里。\n\n'
                        '碰上桌面上已经有同名的：内容一模一样的，用盒里这一枚顶上；'
                        '对不上的，给桌面上那枚换个带日期的名字，两份都留着。'
                        % (box, len(_left), '、'.join(_left[:6])),
                        'Box "%s" holds %d item(s) packed by an older build - the '
                        'current record does not list them:\n%s\n\n'
                        'Click YES: the whole box goes back to the desktop, each item '
                        'into the spot it left from.\nClick NO: nothing moves, they '
                        'stay in the box.\n\n'
                        'If the desktop already has that name: a byte-identical item '
                        'is replaced by the one from the box; a different one is '
                        'renamed with a date stamp, so both stay.'
                        % (box, len(_left), ', '.join(str(x) for x in _left[:6])))):
            return
        im = getattr(gui, 'icon_manager', None)
        r = _vr.reclaim_box(box, source='panel',
                            apply_layout=getattr(im, 'apply_positions_settled', None))
        n_back = len(r.get('back') or [])
        gui._log(i18n.tr('📦 搬回未认领「%s」：%d 枚回到桌面，%d 枚搬不回'
                         % (box, n_back, len(r.get('blocked') or [])),
                         '📦 Reclaimed "%s": %d back, %d blocked'
                         % (box, n_back, len(r.get('blocked') or []))))
        if r.get('reason'):
            gui._log(u'⚠ ' + log_i18n.translate(str(r['reason'])))
            messagebox.showwarning(i18n.tr('没能搬回', 'Nothing moved'),
                                   log_i18n.translate(str(r['reason'])))
        elif n_back:
            messagebox.showinfo(i18n.tr('已搬回', 'Reclaimed'),
                                _place_phrase(r.get('place'), n_back))
        if r.get('blocked'):
            txt = _fmt_refused({'blocked': r['blocked']})
            messagebox.showwarning(i18n.tr('有几枚没搬回', 'Some stayed in the box'),
                                   txt)
            detail.config(text=txt)
        _refresh()

    def _do_dedupe():
        """★R321 J5 → ★R323 L8 → ★R324 N1/N2：把桌面上那些重复图标收进**一只看得见的盒**。

        三步一个不许省（顺序由回归闸按函数体行号钉）：
        ① 先**演练**（`dry_run=True`：桌面/公共桌面/盒根三个目录一字节都不动，也不建
          那只盒）拿到清单；② 把数量与前几个名字念给用户，`askyesno` 问一句；
        ③ 用户点"是"才真搬。
        搬 = 原样挪进 `盒根\\收纳·重复的\\`（同盘 rename，不复制），**一枚都不删**，
        里面一份 `清单.json` 跨轮累加（名字/原名/来自哪个目录）⇒ 不想收拾了按清单搬回去就行。
        ★R324 N2 收这三类（以前只收第一类，剩下两类永远留在桌面上、界面还念成"没有可以
        收拾的"，用户只能读成"功能坏了"）：
          ① 判得出是重复的（内容完全一样 / 同一个快捷方式）；
          ② 盒根下历轮 `~让路…` 回收目录里的（用户「方便我清理」）；
          ③ **原名已经不在了**的那一枚——win11 R041 那枚 `HashKit_20261011_152441`
            就栽在这一档：形状是本程序当年造的、原名却不在同一个目录。
        ④ 判不出是不是同一枚的（`differ`）**默认一枚不碰**（★R321 D5 那条红线保留），
          改成**当面再问一句**：把每一枚差在哪一味念出来（大小不一样 / 指向的不是同一个
          程序 …），用户点【是】才一起收进同一只盒，点【否】就原样留在桌面上——程序不猜。
        ★R324 N3（用户看图报的病：「桌面上还是存在"盒子"和"隐藏"模式共存的情况…
        w网络工具2-4、game 等等文件夹还是隐藏模式，按理说他们应该被搬到盒子里面才对」
        「能尽量用一种模式最好」）：**只在收纳档**多扫一档——盒里那几枚永远散不回桌面的
        陈旧同名枚（`visibility_router.box_stale_conflicts`）。两步一起做才算修好：
        先把盒里那枚收进「收纳·重复的」，腾出格子，再把桌面上那枚（多半带着我们自己的
        隐藏指纹）显出来搬进它原来那只盒 ⇒ 桌面上只剩盒子这一种模式。
        隐藏档压根不进这一档（用户定案：「隐藏模式 目前没啥问题，点了隐藏模式，那么
        基本都是隐藏模式，别弄错了」）。判不出是不是同一份东西的那几枚并进 ④ 那个
        **同一个问句**，不新加一处 askyesno。
        ★R324 N1（用户「这个名字可以改成"清理重复的图标"」）：这枚按钮原来的名字里
        有"让路名"三个字——那是程序内部的词，用户读不懂 ⇒ 现在对外一律叫「清理重复的图标」；
        app.log 里那一族行的前缀**没动**，历史日志与词典那 11 条规则还认它。
        """
        _pre = desktop_box.dedupe_yield_names(desktops=None, root=None,
                                              dry_run=True)
        _same = list(_pre.get('same') or [])
        _legacy = list(_pre.get('legacy') or [])
        _orp = list(_pre.get('orphan') or [])
        _dif = list(_pre.get('differ') or [])
        # ★R324 N3（用户看图报的病：「桌面上还是存在"盒子"和"隐藏"模式共存的情况…
        # w网络工具2-4、game 等等文件夹还是隐藏模式，按理说他们应该被搬到盒子里面
        # 才对」「能尽量用一种模式最好」）：**只在收纳档**多加这一档——盒里那几枚永远
        # 散不回桌面的陈旧同名枚先收进「收纳·重复的」，腾出来的那一格再把桌面上那枚
        # 搬进盒子。隐藏档压根不扫、更不动（用户定案「隐藏模式目前没啥问题，点了隐藏
        # 模式，那么基本都是隐藏模式，别弄错了」）。
        _vr = None
        _im = getattr(gui, 'icon_manager', None)
        _bsame, _bdif = [], []
        try:
            from core import visibility_router as _vr0
            if _vr0.current_mode(_vr0.load_settings()) == _vr0.MODE_BOX:
                _vr = _vr0
                _bpre = _vr0.box_stale_conflicts()
                _bsame = [x for x in _bpre if x.get('same')]
                _bdif = [x for x in _bpre if not x.get('same')]
        except Exception as e:
            logger.error(f'扫盒里的陈旧同名枚失败（桌面上那一发照旧）: {e}')
        _boxres = []
        # ★R325 F1（ZCode 20261013_2111 报告 P1，本批定级 P1）：`_orp` 从**自动批**里摘出来。
        # 真机实案——用户桌面上此刻就躺着 `DeskBits_日志_0.99-R042_20261014_081147.zip`：
        # 名字形状撞上我们的让路名，剥出来的"原名"却不存在，程序拿不出任何凭据说
        # "这是我们造的"。旧写法把它算进"本程序以前留下的"并一次搬走 ⇒ 用户自己导出的
        # 备份包被搬进盒里。这一档现在只在**第二问**里当面点名（与判不出是不是同一枚
        # 的那几枚并成同一个问句，不新加一处 askyesno），点【是】才带 include_orphan=True 收。
        _todo = _same + _legacy
        # 第二问的候选集合：判不出是不是同一枚（桌面 + 盒里）+ 拿不出凭据的那几枚。
        _ask_all = _dif + _orp + _bdif
        r = {}
        _did = False
        if _todo or _bsame:
            _nb = sum(1 for x in _same if x.get('why') == 'bytes')
            _names = '、'.join(str(x.get('name') or '') for x in _todo[:6])
            # ★R325 F5：英文那一份**不能**吃中文顿号「、」（★R230b 同款：变量插进
            # 英文模板 ⇒ English 档夹一个中文标点）。名字本身是 ASCII，用英文逗号重拼一份。
            _names_en = ', '.join(str(x.get('name') or '') for x in _todo[:6])
            _box_hint = i18n.tr('\n另外 %d 枚在以前那几把回收目录里，一起并进来。'
                                % len(_legacy) if _legacy else '',
                                '\nAnother %d sit in the old quarantine folders and '
                                'will be merged in.' % len(_legacy) if _legacy else '')
            _orp_hint = i18n.tr(
                '\n还有 %d 枚只有名字带日期时间、拿不出凭据（多半是你自己导出的备份包），'
                '这一批先不动，待会儿单独问你一句。' % len(_orp) if _orp else '',
                '\nAnother %d have only a date-stamped name and no proof of being '
                'ours (most likely backup files you exported yourself): they stay '
                'put and you will be asked about them next.'
                % len(_orp) if _orp else '')
            _dif_hint = i18n.tr('\n再有 %d 枚判不出是不是同一枚，这一批先不动，'
                                '待会儿单独问你一句。' % len(_dif) if _dif else '',
                                '\nAnother %d cannot be matched with certainty: they '
                                'stay put and you will be asked about them next.'
                                % len(_dif) if _dif else '')
            # ★R324 N3：这一句是用户看图报的那个病（"两种模式共存"）的正面回答——
            # 说清盒里那几枚为什么永远出不来、点【是】之后桌面上会怎样。
            _stale_hint = i18n.tr(
                '\n另外盒子里还躺着 %d 枚，桌面上又有同名的——这一批永远散不回桌面，'
                '桌面上那几枚也就永远搬不进盒子（你看到的"两种模式一起"就是它）。'
                '点【是】会先把盒里那几枚收进下面那只重复盒，再把桌面上那几枚搬进盒子。'
                % len(_bsame) if _bsame else '',
                '\nAnother %d sit inside a box while a same-named one is on the '
                'desktop: they can never come back, so the desktop ones can never go '
                'in either - that is the two-modes-at-once you saw. YES first moves '
                'the boxed ones into the duplicate folder below, then files the '
                'desktop ones into their box.' % len(_bsame) if _bsame else '')
            if not messagebox.askyesno(
                    i18n.tr('把这些重复的图标收进一个文件夹？',
                            'Collect these duplicates into one folder?'),
                    # ★R322 J6：文案先说清**桌面上会少掉什么**，再说去哪、怎么退。
                    # ★R325 F1：这一问只报"拿得出凭据的那几枚"——数目不再把 `_orp`
                    # 算进去，"原名已经不在了"那一档也从这一句里摘掉（它不是"本程序
                    # 以前留下的"，我们**证明不了**；它进下面那**同一个**第二问）。
                    i18n.tr('桌面上有 %d 枚是本程序以前留下的重复图标（内容完全一样 %d 枚、'
                            '同一个快捷方式 %d 枚）：\n%s%s%s%s%s\n\n'
                            '点【是】：把带日期时间的这一批从桌面搬走，收进盒子文件夹里的 '
                            '%s（一枚都不删，里面留一份清单，随时按原名搬回来）；'
                            '搬完自动把那个文件夹打开给你看。\n'
                            '点【否】：一个都不动。\n\n'
                            '桌面上原名那几枚始终不动。'
                            % (len(_same), _nb, len(_same) - _nb,
                               _names, _box_hint, _orp_hint, _dif_hint,
                               _stale_hint, desktop_box.DUP_BOX_NAME),
                            'The desktop has %d duplicate icon(s) this program left '
                            'behind (byte-identical %d, same shortcut %d): %s %s %s %s %s\n\n'
                            'Click YES: the date-stamped ones leave the desktop and go '
                            'into the "%s" folder inside the box folder (nothing is '
                            'deleted, a manifest stays there so any of them can be '
                            'moved back by name); the folder opens for you afterwards.\n'
                            'Click NO: nothing moves.\n\n'
                            'The originals under their plain names never change.'
                            % (len(_same), _nb, len(_same) - _nb,
                               _names_en, _box_hint.replace('\n', ' '),
                               _orp_hint.replace('\n', ' '),
                               _dif_hint.replace('\n', ' '),
                               _stale_hint.replace('\n', ' '),
                               desktop_box.DUP_BOX_NAME))):
                return
            r = desktop_box.dedupe_yield_names(desktops=None, root=None)
            _did = True
            if _vr is not None and _bsame:
                _boxres.append(_vr.yield_stale_box_items(im=_im))
        elif not _dif and not _bdif and not _orp:
            gui._log(i18n.tr('🧹 没有可以收拾的重复图标',
                             '🧹 Nothing to tidy'))
            messagebox.showinfo(i18n.tr('清理重复的图标', 'Tidy duplicates'),
                                i18n.tr('没有可以收拾的重复图标'
                                        '（只认本程序自己造的那种带日期时间的名字）。',
                                        'Nothing to tidy - only the date-stamped names '
                                        'this program itself created are considered.'))
            _refresh()
            return
        else:
            gui._log(i18n.tr('🧹 没有可以自动收拾的重复图标，先问你一句',
                             '🧹 Nothing safe to tidy automatically - asking first'))
        n = int(r.get('total') or 0)
        qd = str(r.get('quarantine') or '')
        _fail = len(r.get('failed') or [])
        _merr = str(r.get('manifest_error') or '')
        if _ask_all:
            # ★R324 N2：这一档程序**不猜**，当面念出每一枚差在哪一味再问一句；
            # 点【是】才把它们收进**同一只盒**（清单照记来路），点【否】一枚不动。
            # ★R324 N3 把"盒里那枚与桌面上那枚判不出是不是同一份东西"的那几枚并进
            # **同一个问句**（不新加一处 askyesno）：收走盒里那枚，桌面上同名的那枚
            # 才搬得进盒子——用户要的就是"桌面上只剩一种模式"。
            # ★R325 F1 再并进"只有名字带日期时间、拿不出凭据"的那几枚：**还是同一个问句**
            # （askyesno 全仓这一处函数里仍是两处），因为两档的处置完全一样——
            # 程序不猜、当面点名、点【是】才收、点【否】一枚不动。
            _dn = '、'.join('%s（%s）' % (x.get('name'),
                                          x.get('note') or u'对不上')
                            for x in _ask_all[:5])
            # ★R230b：英文那一份不许把中文的"差在哪一味"插进去——那几句是 core 给的
            # 中文短句，翻成一句 CJK-free 的指路话，细节留在运行日志里逐枚点名。
            # ★R325 F5：措辞从"how they differ"改成中性的"the reason is in the run log"
            # ——orphan 那一档不是"差一味"，是"拿不出凭据"。
            _dn_en = ', '.join('%s (the reason is in the run log)'
                               % x.get('name') for x in _ask_all[:5])
            _dmore = i18n.tr('\n…另外 %d 枚见运行日志。' % (len(_ask_all) - 5)
                             if len(_ask_all) > 5 else '',
                             '\n...and %d more in the run log.' % (len(_ask_all) - 5)
                             if len(_ask_all) > 5 else '')
            if messagebox.askyesno(
                    i18n.tr('这几枚判不出是不是重复，要一起收进去吗？',
                            'Collect these unmatched ones too?'),
                    i18n.tr('还有 %d 枚程序不敢自己收：有的判不出它跟同名那一枚是不是'
                            '同一个东西（每一枚差在哪一味），有的只有名字带日期时间、'
                            '拿不出凭据（多半是你自己导出的备份包）：\n%s%s\n\n'
                            '点【是】：把这 %d 枚也收进盒子文件夹里的 %s，'
                            '一枚都不删，清单里记着来路，随时按原名搬回去；'
                            '盒里那几枚收走之后，桌面上同名的那几枚会搬进盒子。\n'
                            '点【否】：一枚不动，它们留在原处。'
                            % (len(_ask_all), _dn, _dmore, len(_ask_all),
                               desktop_box.DUP_BOX_NAME),
                            'Another %d the program dares not decide about on its own: '
                            'some cannot be matched with certainty, others only carry a '
                            'date-stamped name (most likely backup files you exported '
                            'yourself) - the reason for each is listed: %s%s\n\n'
                            'Click YES: these %d also go into the "%s" folder - '
                            'nothing is deleted and the manifest records where each '
                            'came from, so any of them can be moved back by name; '
                            'once the boxed ones leave, the same-named desktop ones '
                            'are filed into their box.\n'
                            'Click NO: nothing moves.'
                            % (len(_ask_all), _dn_en, _dmore.replace('\n', ' '),
                               len(_ask_all), desktop_box.DUP_BOX_NAME))):
                if _dif or _orp:
                    # 两档一起放行：这一问点【是】＝对**这两档**都点了头（列名字时
                    # 已经逐枚点名）。哪一档是空的，传了旗标也只是空转。
                    _r2 = desktop_box.dedupe_yield_names(
                        desktops=None, root=None,
                        include_differ=True, include_orphan=True)
                    n += int(_r2.get('total') or 0)
                    _fail += len(_r2.get('failed') or [])
                    if not _merr:
                        _merr = str(_r2.get('manifest_error') or '')
                    if not qd:
                        qd = str(_r2.get('quarantine') or '')
                    if not r:
                        r = _r2
                if _vr is not None and _bdif:
                    _boxres.append(_vr.yield_stale_box_items(im=_im,
                                                             include_differ=True))
                _did = True
            elif not _todo and not _bsame:
                _refresh()
                return
        for _b in _boxres:
            n += int(_b.get('total') or 0)
            _fail += len(_b.get('failed') or [])
            if not qd:
                qd = str(_b.get('quarantine') or '')
            if not _merr:
                _merr = str(_b.get('manifest_error') or '')
        _bin = [x for _b in _boxres for x in (_b.get('moved_in') or [])]
        _bref = [x for _b in _boxres for x in (_b.get('refused_in') or [])]
        if not _did:
            _refresh()
            return
        gui._log(i18n.tr('🧹 已把 %d 枚重复的收进「%s」（一枚没删）：%s'
                         % (n, desktop_box.DUP_BOX_NAME, qd or '—'),
                         '🧹 %d duplicate(s) moved into "%s" (nothing deleted): %s'
                         % (n, desktop_box.DUP_BOX_NAME, qd or '-')))
        if _bin:
            # ★R324 N3：这一句是用户那句"两种模式一起感觉很怪异"的正面回答——
            # 腾出来的格子马上用上，桌面上从此只剩盒子这一种模式。
            gui._log(i18n.tr('🧹 腾出来的格子已经用上：%d 枚从桌面搬进了各自那只盒'
                             '（桌面上从此只剩盒子这一种模式）：%s'
                             % (len(_bin), '、'.join(str(x.get('name') or '')
                                                     for x in _bin[:6])),
                             '🧹 The freed slots are used: %d item(s) left the desktop '
                             'and went into their box (only one mode is left): %s'
                             % (len(_bin), ', '.join(str(x.get('name') or '')
                                                     for x in _bin[:6]))))
        if _bref:
            gui._log(i18n.tr('🧹 另有 %d 枚还显在桌面上、没能搬进盒子（理由逐枚在运行'
                             '日志里）：%s'
                             % (len(_bref), '、'.join(
                                 '%s（%s）' % (x.get('name'), x.get('reason') or '—')
                                 for x in _bref[:4])),
                             '🧹 %d item(s) are still visible on the desktop and could '
                             'not be filed into a box (reasons in the run log)'
                             % len(_bref)))
        if _fail:
            gui._log(i18n.tr('🧹 另有 %d 枚没能搬进去，留在原地（理由在运行日志里）'
                             % _fail,
                             '🧹 %d could not be moved and stayed where they are '
                             '(see the run log)' % _fail))
        if n:
            if _merr:
                # ★R321-S6：清单没写出，界面就不许再说"按 清单.json 搬回去"——
                # 那是一句照着不存在的文件写的操作指引（历轮只落 app.log）。
                gui._log(i18n.tr('🧹 清单.json 没写出（%s）：那 %d 枚已在「%s」里，'
                                 '按原名那一枚认'
                                 % (_merr, n, desktop_box.DUP_BOX_NAME),
                                 '🧹 The manifest could not be written (%s): the %d '
                                 'name(s) are already in "%s", recognise them by '
                                 'their original name'
                                 % (_merr, n, desktop_box.DUP_BOX_NAME)))
                messagebox.showwarning(
                    i18n.tr('已收进那只盒，但清单没写出',
                            'Moved, but no manifest'),
                    i18n.tr('收了 %d 枚，一枚没删。\n文件夹在：%s\n'
                            '可 清单.json 没写出（%s）——要还原就在里面按原名把那枚'
                            '搬回它原来的目录。'
                            % (n, qd, _merr),
                            '%d moved, nothing deleted.\nFolder: %s\n'
                            'The manifest was not written (%s) - to undo, move each '
                            'name back to its original folder by hand.'
                            % (n, qd, _merr)))
            else:
                messagebox.showinfo(i18n.tr('已收进「%s」' % desktop_box.DUP_BOX_NAME,
                                            'Moved into "%s"'
                                            % desktop_box.DUP_BOX_NAME),
                                    i18n.tr('收了 %d 枚，一枚没删。\n文件夹在：%s\n'
                                            '要清理就在里面直接删；要还原就按 清单.json '
                                            '把名字搬回它原来的目录。' % (n, qd),
                                            '%d moved, nothing deleted.\nFolder: %s\n'
                                            'To finish cleaning, delete them there; '
                                            'to undo, move the names back using the '
                                            'manifest.' % (n, qd)))
            # ★R321 J5：动完把那一目录打开给人看——用户点这枚按钮就是为了找到那些东西。
            # 只在真收了东西时才开（一枚没动就别凭空弹一个资源管理器窗口）。
            try:
                from core import explorer_tabs
                explorer_tabs.open_folders([qd], as_tabs=False)
            except Exception as e:
                logger.error(f'打开重复回收目录失败 {qd}: {e}')
                gui._log(i18n.tr('🧹 打开那个文件夹失败：%s（路径在上面那行里）' % e,
                                 '🧹 Could not open the folder: %s '
                                 '(the path is in the line above)' % e))
        elif not _fail:
            messagebox.showwarning(i18n.tr('一枚没能收进去', 'Nothing was moved'),
                                   log_i18n.translate(
                                       str(r.get('reason') or '回收目录没写出（详情见运行日志）')))
        _refresh()

    def _do_copy():
        try:
            gui.root.clipboard_clear()
            gui.root.clipboard_append(path_var.get())
            status.config(text=i18n.tr('路径已复制到剪贴板', 'Path copied'))
        except Exception as e:
            logger.error(f'复制盒子路径失败: {e}')

    btn_new.config(command=_do_create)
    btn_del.config(command=_do_delete)
    btn_del_all.config(command=_do_delete_all_empty)
    btn_refresh.config(command=_refresh)
    btn_pack.config(command=_do_pack)
    btn_open.config(command=_do_open)
    btn_unbox.config(command=_do_unbox)
    btn_reclaim.config(command=_do_reclaim)
    btn_dedupe.config(command=_do_dedupe)
    btn_copy.config(command=_do_copy)
    box_cb.bind('<<ComboboxSelected>>', _on_box_change)
    # 筛选走**变量**而不是 <KeyRelease>：本机 Tk 合成的 KeyRelease 打不到绑定
    # （取证 D:/Qoder-workplace/tmp/bugflow/20261006_1120/r302_u3_evt.txt：got=[]），
    # 而且变量监听对打字、粘贴、右键清空一律生效，比盯按键事件更稳。
    filt_var.trace_add('write', lambda *_a: _on_box_change())
    btn_clear.config(command=lambda: filt_var.set(''))
    lb.bind('<<ListboxSelect>>', _sync_heads)

    def _on_auto_toggle():
        """★R304「自动收纳」开关：**先落盘再应用**（★R226 口径）。
        落盘失败也照样生效本次会话——设置读写不该把功能锁死。"""
        v = bool(auto_var.get())
        st = getattr(gui, '_app_settings', None)
        if isinstance(st, dict):
            st['box_auto_pack'] = v
            try:
                gui._save_app_settings()
            except Exception as e:
                logger.warning(f'自动收纳开关没能写进设置（本次仍生效）: {e}')
        status.config(text=i18n.tr(
            '📦 自动收纳已%s：按分类各回各盒，缺的盒子顺手建' % ('打开' if v else '关掉'),
            '📦 Auto-sort %s: each category into its own box'
            % ('on' if v else 'off')))

    cb_auto.config(command=_on_auto_toggle)

    def _on_return(_e=None):
        """回车＝"我要的就是这个名字"：已有的直接选中，没建的顺手建一个。"""
        name = box_var.get().strip()
        if name and name not in state['boxes']:
            _do_create()

    box_cb.bind('<Return>', _on_return)

    _refresh()
    gui.box_card = {'frame': frame, 'refresh': _refresh,
                    'listbox': lb, 'inbox_listbox': lb_in,
                    'box_var': box_var, 'combobox': box_cb,
                    'filter': filt, 'filter_var': filt_var,
                    'auto': cb_auto, 'auto_var': auto_var}
    _register_card(gui)
    return frame
