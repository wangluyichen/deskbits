# -*- coding: utf-8 -*-
"""★R305（批61 U2/U3）「隐藏盒」收纳模式路由器。

一句话：把「切分类 / 排行榜显示 / 家长模式」这三条链的底层动作做成可切换的两档——

· `hide`（**默认**，今天的行为）：走 `IconManager.apply_visibility` 双属性隐藏法，
  桌面 ListView 里的图标一枚都不动，只翻属性位；
· `box`：这次不该显示的图标**真的搬进盒子**（`桌面盒子deskbits\\收纳·<组名>`），
  下次要看的若还在盒里，就按搬走时记的原坐标散回并摆好。

为什么要有这本 `hidden_box.json`（★R322 J5 起落在**盒根**，旧版在 `data/`）：盒子根目录里
**用户手工建的盒子**一枚都不许被
这条链碰。账本只记"哪几枚盒子是本系统建的、对应哪个组、来自哪条链"，成员清单仍以
`desktop_box` 那本盒子账本为准（两处各记一份迟早漂开，所以只记所有权、不复制成员）。

真搬移的代价（批61 实测，`r305_rename_bench.txt`）：同卷 rename 0.43 ms/枚，加每枚一次
SHChangeNotify 也才 1.05 ms/枚——**搬不是瓶颈**。瓶颈在"文件回来那一刻 Explorer 的
ListView 还没把它列进名册，那一发 LVM_SETITEMPOSITION 会被静默吞掉"（★R304 真机实锤
`[apply] 目标=1 实动=0`），所以散回必须走 `apply_positions_settled`：等名册、逐枚摆、读回核对。
"""
import io
import json
import logging
import os
import tempfile
import time
from typing import Dict, List, Optional, Sequence, Set

from core import desktop_box

logger = logging.getLogger("deskbits")

MODE_KEY = 'pack_mode'
MODE_HIDE = 'hide'
MODE_BOX = 'box'
BOX_PREFIX = '收纳·'                 # 组盒名前缀；Windows 文件名里合法
LEDGER_VERSION = 1
LEDGER_NAME = 'hidden_box.json'

# 这些名字永远不参与收纳（虚拟系统图标属性法管不到、占位文件搬走会把桌面搞乱）
_RESIDENT_HINTS = ('desktop.ini', 'thumbs.db')


# ───────────────────────── 模式 ─────────────────────────

def current_mode(settings=None) -> str:
    """读档：只认 'box'，其它一律回 'hide'（默认＝今天的行为，升级当天体验不变）。

    ★R324 N3-c：`settings=None` 旧写法等于"拿空 dict 去读"⇒ **恒回 hide**，而
    `dock._show_all_icons` 正是那么调的 ⇒ switch.log 里「全部」那一发永远念
    `档=hide`，用户在收纳档也看不出自己走的是哪条档。None 改成"自己读一次设置"；
    显式传 dict 的语义一字不动（`current_mode({}) == 'hide'` 那条历轮判据照旧）。
    """
    if settings is None:
        try:
            settings = load_settings()
        except Exception:
            return MODE_HIDE
    try:
        raw = (settings or {}).get(MODE_KEY, MODE_HIDE)
    except Exception:
        return MODE_HIDE
    return MODE_BOX if str(raw).strip().lower() == MODE_BOX else MODE_HIDE


def load_settings(app_dir=None) -> dict:
    """三条链没带 settings 时的默认来源：`<应用目录>/config/app_settings.json`。

    只读不写；读不动按 `{}` ⇒ `current_mode` 回 hide。设置页那枚开关是"先落盘再应用"
    （★R226 口径），所以盘上这份就是当前档。
    """
    if app_dir is None:
        from utils.app_paths import get_app_dir
        app_dir = get_app_dir()
    p = os.path.join(str(app_dir), 'config', 'app_settings.json')
    try:
        with io.open(p, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning('[隐藏盒] 设置读不动（按 hide 档继续）%s: %s', p, e)
        return {}


def box_name_for(group: str) -> str:
    """组名 → 组盒名。空组名仍旧带前缀，免得建出一枚名叫空串的盒子。"""
    g = str(group or '').strip()
    return (BOX_PREFIX + g) if g else BOX_PREFIX


def group_of_box(box: str) -> str:
    s = str(box or '')
    return s[len(BOX_PREFIX):] if s.startswith(BOX_PREFIX) else s


def is_system_box(box: str) -> bool:
    return str(box or '').startswith(BOX_PREFIX)


# ───────────────────────── 所有权账本 ─────────────────────────

def ledger_path(app_dir=None) -> str:
    """所有权账本落点。★R322 J5：**不给 `app_dir`（生产链）⇒ 落在盒根里跟着盒子走**
    —— 旧口径落 `<程序目录>/data/`，而打包模式那是 exe 所在目录，换一个新版本目录
    就等于把"哪些盒是本系统的"这份记忆清零（win11 R038/R039 实案根因）。
    给了 `app_dir`（门禁沙箱重定向 `utils.app_paths.get_app_dir()`）⇒ 仍走
    `<app_dir>/data/`，历轮八套闸门的隔离口径一个字都不变。"""
    if app_dir is not None:
        return os.path.join(str(app_dir), 'data', LEDGER_NAME)
    return os.path.join(desktop_box.get_box_root(), LEDGER_NAME)


def load_ledger(app_dir=None, *, recover_for_write: bool = False) -> tuple:
    """→ (data, failed)。failed=True 表示**读不动**，调用方必须一发都不搬。

    ★R325 F6（ZCode 20261013_2111 报告 P5）：「文件在、但里面是我们写坏/别人改坏的
    一段 JSON」和「文件读不动」以前并进同一档 ⇒ 一律 `failed=True`，用户从此**永远**
    点不动收纳（账本坏一次就终身锁死，只能手工删文件）。现在按 ★R231b 的
    「先留底再放行」分两档，与 `desktop_box.load_manifest` 逐字同款口径：
      · `OSError`（被占用/权限/IO 故障，文件还是好是坏都不知道）⇒ 照旧 `failed=True`，
        一发不搬——这一档没有"原件已安全另存"的保证，写回去会覆盖掉读不动的那份；
      · JSON 解析不动 / 顶层形状不对 ⇒ **无论谁来读都先 `safeio.set_aside_corrupt`
        把原件另存留底**（证据不静默丢，也保证下一次读看到的是干净档，不再终身锁死）；
        **留底失败**（连另存都不成功）⇒ 一律 `failed=True`。
      · 留底成功之后往哪走，**分两档**（★R325 定稿串跑实测：一律放行会把
        "先证明这盒是我们的才动手"那条保守线一起放掉——剪幽灵 r306 K7、散回 r305 R4、
        接管空壳 r309 K1、认领散落 r320 K1 四套既有闸当场红，红得对）：
          - **读路径（默认）**：`failed=True` ⇒ 剪/散/接管/认领这一发照旧一枚不动。
            空表不等于"这些盒是我们的"，认不出就动手会去碰用户手工放的东西；
          - **写路径**（用户刚点的那一发收纳，`recover_for_write=True`）：`failed=False`
            ⇒ 按空表继续搬并重新登记。这一档动的是"当前这一组要收起来"这件用户已经
            点过的事，边搬边把凭据重新写回账本，正是 F6 要修的"点不动"那一条。
    """
    p = ledger_path(app_dir)
    desktop_box.migrate_into_box_root(LEDGER_NAME, p, app_dir)
    empty = {'version': LEDGER_VERSION, 'boxes': {}}
    # 首启/搬家前：文件压根不存在 ≠ 读不动（FileNotFoundError 是 OSError 的子类，
    # 少了这一道先发制人，第一次点收纳就会被自己判成"锁死"）。
    if not os.path.isfile(p):
        return empty, False

    def _aside(_e):
        from utils import safeio
        ok, note = safeio.set_aside_corrupt(p)
        if not ok:
            logger.error('[隐藏盒] 账本已损坏但留底失败（拒绝写盘）%s: %s | %s',
                         p, note, _e)
            return empty, True
        if recover_for_write:
            logger.warning('[隐藏盒] 账本损坏，已另存留底；这一发按空表继续搬: %s | %s',
                           note, _e)
            return empty, False
        logger.warning('[隐藏盒] 账本损坏，已另存留底；本轮仍按读不动处理（一枚都不动）'
                       ': %s | %s', note, _e)
        return empty, True

    try:
        with io.open(p, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except OSError as e:
        logger.warning('[隐藏盒] 账本读不动（%s），本次一枚都不搬：%s', p, e)
        return empty, True
    except Exception as e:
        return _aside(e)
    if not isinstance(data, dict):
        return _aside(ValueError('not a dict'))
    data.setdefault('version', LEDGER_VERSION)
    if not isinstance(data.get('boxes'), dict):
        return _aside(ValueError('boxes is not a dict'))
    return data, False


def save_ledger(data: dict, app_dir=None) -> bool:
    """原子写：临时文件 + os.replace。写失败要留日志，绝不静默。"""
    p = ledger_path(app_dir)
    try:
        d = os.path.dirname(p)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix='.hidden_box_', suffix='.tmp', dir=d)
        try:
            with io.open(fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, p)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        return True
    except Exception as e:
        logger.error('[隐藏盒] 账本写入失败 %s: %s', p, e)
        return False


def register_box(group: str, source: str = '', app_dir=None,
                 data: Optional[dict] = None) -> bool:
    """把一枚组盒登记成"本系统建的"。登记过才允许这条链再动它。"""
    own = data if data is not None else load_ledger(app_dir)[0]
    box = box_name_for(group)
    ent = own['boxes'].get(box) or {'created': int(time.time())}
    ent['source'] = source or ent.get('source') or ''
    ent['group'] = group_of_box(box)
    own['boxes'][box] = ent
    return save_ledger(own, app_dir)


# ───────────────────────── 成员读数 ─────────────────────────

def boxed_members(app_dir=None, root: Optional[str] = None) -> Dict[str, str]:
    """→ {图标显示名: 所在组盒名}，只统计**登记过**的组盒。"""
    data, failed = load_ledger(app_dir)
    if failed:
        return {}
    mdata, m_failed = desktop_box.load_manifest(app_dir)
    if m_failed:
        return {}
    r = root or desktop_box.get_box_root()
    out: Dict[str, str] = {}
    for box in data['boxes']:
        if not is_system_box(box):
            continue
        if not os.path.isdir(desktop_box.box_dir_path(box, r)):
            continue
        for _fn in (mdata['boxes'].get(box, {}).get('items', {}) or {}):
            # ★R311：显示名按**盒里的文件名**现算，不读账本存的 `name`。账本那份是
            # 写它那一轮的口径（修前 `foo.url` 就记成 `foo.url`），而调用方、分类名单、
            # Explorer 全是 `foo` ⇒ 存量盒档一枚都散不回，用户读成"图标被吞了"。
            # 文件名才是这一枚在盒里的真身，从它算显示名永远跟当下口径一致（自愈）。
            out[desktop_box.display_name(_fn)] = box
    return out


def desktop_names(desktop: Optional[str] = None) -> Set[str]:
    # ★R310：`desktop` **原样转给** `list_boxable`——不传时它自己扫"用户桌面+公共桌面"
    # 两手。历轮这里先把 None 解析成用户桌面、再当显式目录传下去 ⇒ 公共桌面被这一行
    # 筛掉，真机 `Qoder CN` 就是这么永远进不了收纳候选的。
    return {x['name'] for x in desktop_box.list_boxable(desktop)}


def _positions_of(im, names: Sequence[str],
                  provider=None) -> Dict[str, Sequence]:
    """搬走前先记下这几枚当前在桌面的哪一格，散回时才谈得上"回原位"。"""
    pos: Dict[str, Sequence] = {}
    try:
        live = provider() if callable(provider) else (
            im._read_all_positions_live() if im is not None else {})
    except Exception as e:
        logger.warning('[隐藏盒] 读当前坐标失败（散回时按可用空位摆）: %s', e)
        return pos
    for n in names:
        p = (live or {}).get(n)
        if p is not None:
            pos[n] = p
    return pos


def _attribute_pass(im, visible: Set[str]) -> dict:
    """把可见集原样交给属性法——box 档里"搬不走的那几枚"与"该回来的却藏在属性里"的兜底。"""
    if im is None:
        return {}
    return dict(im.apply_visibility(visible) or {})


def _merge_hide(out: Dict, hide: dict) -> None:
    """属性法那份读数**平铺**到返回值上（hidden/shown/failed/save_ok）。

    三条链的下游代码本来就按这几个键算 ListView 期望条数、拼界面文案；box 档若只把
    它们塞在 `hide_result` 里，调用方读不到就会退回默认 0 ⇒ 每轮空等 2 秒（★R204
    NEW-C6 那条老坑）。`hidden_now`/`shown_now` 是搬移计数，与这四个键不重名。
    """
    out['hide_result'] = hide
    for k in ('hidden', 'shown', 'failed', 'newly_hidden', 'save_ok'):
        if k in hide:
            out[k] = hide[k]
    out.setdefault('failed', [])


def _fallback_to_attribute(out: Dict, im, visible: Set[str],
                           tag: str, reason: str) -> dict:
    """失败分支的统一收手：这一组**一枚都不搬**，退回双属性隐藏，并把原因点名。

    ★R318 K3/K4 同族口径：降级不是"这次什么都不做"——用户点的是"只显示这一组"，
    属性法照样能让这一组生效；但必须说出来（degraded + reason + attr_fallback
    三样齐），不许静默成"看起来没反应"。调用方（core/dock.py、gui/rank_display.py）
    读的就是平铺出来的那一份。
    """
    logger.warning('[隐藏盒] %s ⇒ 本次切换一枚都不搬（组=%s）',
                   reason, out.get('group'))
    hide = _attribute_pass(im, visible)
    _merge_hide(out, hide)
    out.update({'ok': True, 'reason': reason, 'degraded': tag,
                'attr_fallback': [{'name': '(整组)', 'reason': reason}],
                'summary': {'in': 0, 'out': 0, 'kept': 0, 'fallback': 1}})
    return out


def stray_entries(path: Optional[str]) -> List[str]:
    """一枚盒目录里"**算用户东西**"的条目名。返回空 list ⇒ 是个空壳，可以接管。

    Explorer 自己写的 `desktop.ini`/`Thumbs.db` 不算东西（★R309 #360：用户手工删空的
    文件夹里常常就剩这一枚，把它算成东西 ⇒ 那一组又被锁死）。
    **读不动一律按"有东西"处理**：接管的前提是我们真看得清它是空的，看不清就不搬。
    """
    if not path:
        return ['(路径读不出)']
    try:
        names = os.listdir(path)
    except Exception as e:
        logger.warning('[隐藏盒] 盒目录读不动（按"里面有东西"保守处理）%s: %s', path, e)
        return ['(读不动)']
    return [n for n in names if str(n).lower() not in _RESIDENT_HINTS]


# ───────────────────────── 盘上对账（★R320） ─────────────────────────

def category_index(app_dir=None) -> Dict[str, Set[str]]:
    """`config/categories.json` 里点过名的图标/分类（**只读**，零副作用）。

    ★R320（用户 P0「图标丢失」）：这本名单是"这枚图标是本系统收进去的"唯一还拿得准的
    盘上凭据。所有权账本 `hidden_box.json` 与成员账本 `desktop_box.json` 当年
    写在 **exe 所在目录**（`get_app_dir()` 跟着 exe 走），而盒子根在**用户家目录**
    （`~\\桌面盒子deskbits`）跨版本活着 ⇒ 换一个新版本目录=两本账全空，旧盒里的图标
    就变成"盘上有、账本没登记"：散回链一枚都认不出来（★R322 J5 已把两本账搬进盒根）。名单外的名目一律按用户自己的
    东西处理（一枚不动）。
    """
    out: Dict[str, Set[str]] = {'icons': set(), 'names': set()}
    if app_dir is None:
        from utils.app_paths import get_app_dir
        app_dir = get_app_dir()
    p = os.path.join(str(app_dir), 'config', 'categories.json')
    try:
        with io.open(p, 'r', encoding='utf-8') as f:
            cats = json.load(f)
    except FileNotFoundError:
        return out
    except Exception as e:
        logger.warning('[隐藏盒] 分类名单读不动（认领按"无可凭据"处理）%s: %s', p, e)
        return out
    if not isinstance(cats, list):
        return out
    for c in cats:
        if not isinstance(c, dict):
            continue
        n = str(c.get('name') or '').strip()
        if n:
            out['names'].add(n)
        for i in (c.get('icons') or []):
            s = str(i or '').strip()
            if s:
                out['icons'].add(s)
    return out


def audit_box_root(app_dir=None, root: Optional[str] = None) -> dict:
    """盒根**以盘为准**过一遍：哪只登记过、哪只没有、里面各有什么、哪些能证明是我们的。

    → {'root', 'boxes': [{'box','group','registered','ours','dir','entries',
       'claimable','unclaimable','unrecorded'}], 'stray_total', 'claimable_total',
       'ledger_failed'}

    判据一律来自 `os.listdir`，**不读账本自述**——这次事故就是账本自述说"盒里没有东西"
    而盘上躺着 29 枚图标（`_repro1.txt` 孤儿态 A/B）。`ledger_failed=True` 时只报读不动，
    一个认领建议都不给（保守同 ★R305 口径）。

    `unrecorded`（★R320 S5b，独立验收 D1）：登记过的盒里**成员账本没记上**的那几枚。
    「显示全部」对这一格只点名不自动搬（★R305 A8），但界面必须知道有几枚，才点得亮
    面板上那枚「搬回未认领的」——旧写法只报未登记盒，登记盒永远灰着。
    """
    r = root or desktop_box.get_box_root()
    res: Dict = {'root': r, 'boxes': [], 'stray_total': 0, 'claimable_total': 0,
                 'ledger_failed': False}
    own, failed = load_ledger(app_dir)
    res['ledger_failed'] = failed
    if failed:
        return res
    cat = category_index(app_dir)
    _mem, _mem_failed = desktop_box.load_manifest(app_dir)
    for box in desktop_box.list_boxes(r):
        bd = desktop_box.box_dir_path(box, r)
        entries = list(stray_entries(bd))
        if desktop_box.is_dup_box(box):
            # ★R323 L8（用户「把重名同路径的放在一起，我到时候去删掉就行」）：
            # 这只回收盒是**只进不出**的，它进 `boxes` 只为让面板看得见、
            # 数得出、点得开；三格一律清零 ⇒ 它不计进 `stray_total`，
            # 「没被认领 N 枚」那句和「搬回未认领的」那枚按钮都永远轮不到它，
            # 认领链（`claim_stray_boxes`）也在这儿被第二道闸挡掉。
            res['boxes'].append({'box': box, 'group': group_of_box(box),
                                 'registered': False, 'ours': True, 'dir': bd,
                                 'entries': entries, 'claimable': [],
                                 'unclaimable': [], 'unrecorded': [],
                                 'dup_box': True})
            continue
        _ours = is_system_box(box)
        reg = box in (own.get('boxes') or {})
        _rec = ((_mem.get('boxes') or {}).get(box) or {}).get('items') or {}
        unrec = [f for f in entries if _mem_failed or f not in _rec] if reg else []
        if reg:
            # 所有权在账上 ⇒ 整盒都是我们搬进去的（★R320 R1c 口径）
            claim, unclaim = entries, []
        elif _ours and group_of_box(box) in cat['names']:
            # ★R320 S5b（独立验收 D2，P1）：盒名反推的**组名也得在分类名单里**才允许
            # 自动认领。旧写法只比显示名在不在所有分类 icons 的**并集**里，于是用户在
            # 盒根下手工建的 `收纳·股票` 里一枚 `secret.lnk`，会因为 'secret' 恰好登记在
            # 「程序」分类里就在开机那一下被自动搬上桌面——而文档承诺的是「组名不是
            # 分类名 ⇒ 一枚不动」。名单外的组盒整盒进 `unclaimable`（只点名、不动手）。
            claim = [f for f in entries
                     if desktop_box.display_name(f) in cat['icons']]
            unclaim = [f for f in entries if f not in claim]
        elif _ours:
            claim, unclaim = [], entries
        else:
            # 不带「收纳·」前缀＝用户手工建的盒 ⇒ 从头到尾不相干
            claim, unclaim = [], []
        res['boxes'].append({'box': box, 'group': group_of_box(box),
                             'registered': reg, 'ours': _ours, 'dir': bd,
                             'entries': entries, 'claimable': claim,
                             'unclaimable': unclaim, 'unrecorded': unrec})
        if _ours and not reg:
            res['claimable_total'] += len(claim)
            res['stray_total'] += len(unclaim)
    return res


def claim_stray_boxes(*, app_dir=None, root: Optional[str] = None,
                      desktop: Optional[str] = None, source: str = '',
                      dry_run: bool = False) -> dict:
    """把"盘上有、账本没登记"的组盒里**拿得准是我们的**那几枚图标搬回桌面。

    → {'claimed': {盒: [文件名]}, 'total', 'left': {盒: [文件名]}, 'registered',
       'blocked', 'conflicts', 'renamed', 'reason', 'dry_run'}

    三条保守线（★R320，红线来自 ★R305 A10/A11 与 ★R309 H3）：
      · 只动带「收纳·」前缀的组盒，**且这只盒反推出来的组名要在分类名单里**
        （★R320 S5b D2：用户手工建的 `收纳·股票` 不归本系统管，即便里面那枚的名字
        恰好登记在别的分类里，也不许开机自动把它搬上桌面）；
      · 组名对得上的，也只动显示名在分类名单里的那几枚——名单外的名目
        原地不动、点名给人看（`left`）；
      · 盒里还留着证明不了的名目 ⇒ **不登记**（登记了下次「显示全部」会连用户手工放的
        那枚一起倒出来）。
    不新造第三本账：补记走现成 `desktop_box.adopt_disk_items`，登记走现成 `register_box`，
    搬运走现成 `move_back`——这一条链里不出现任何直接改名调用（改名只在 desktop_box 那侧）。
    """
    out: Dict = {'claimed': {}, 'total': 0, 'left': {}, 'registered': [],
                 'blocked': [], 'conflicts': [], 'renamed': [],
                 'kept_in_box': [],
                 'reason': '', 'dry_run': dry_run}
    own, failed = load_ledger(app_dir)
    if failed:
        out['reason'] = '隐藏盒清单读不动，这次一枚都没认领——先确认盒里没有你要的东西'
        logger.error('[隐藏盒] 认领：账本读不动 ⇒ 一发都不动（来源=%s）', source or '?')
        return out
    r = root or desktop_box.get_box_root()
    d = desktop or desktop_box.user_desktop_dir()
    if not d or not os.path.isdir(d):
        out['reason'] = '找不到桌面目录，这次一枚都没认领'
        logger.error('[隐藏盒] 认领：桌面目录读不出（%s）⇒ 一发都不动', d)
        return out
    aud = audit_box_root(app_dir=app_dir, root=r)
    for b in aud['boxes']:
        if desktop_box.is_dup_box(b['box']):
            # ★R323 L8 第二道闸：那只重名回收盒一枚都不许被认领回桌面
            # （`audit_box_root` 已经把它的 `claimable` 清零，这一句是"就算将来有人
            # 改了判据、认领链也仍然绕开它"——只进不出这条不能只靠一处实现）。
            continue
        if b['registered'] or not b['ours']:
            continue
        claim, left = list(b['claimable']), list(b['unclaimable'])
        if not claim:
            if left:
                out['left'][b['box']] = left
            continue
        if dry_run:
            out['claimed'][b['box']] = claim
            out['total'] += len(claim)
            if left:
                out['left'][b['box']] = left
            continue
        desktop_box.adopt_disk_items(b['box'], root=r, app_dir=app_dir, desktop=d,
                                     only=claim, source=source or 'claim')
        res = desktop_box.move_back(b['box'], root=r, desktop=d, names=claim,
                                    app_dir=app_dir, dry_run=dry_run,
                                    on_conflict='keep')
        back = [str(x.get('file') or desktop_box.display_name(str(x.get('name') or '')))
                for x in (res.get('back') or [])]
        if back:
            out['claimed'][b['box']] = back
            out['total'] += len(back)
        out['blocked'].extend(res.get('blocked') or [])
        out['conflicts'].extend(res.get('conflicts') or [])
        # ★R323 L7：撞名留在盒里的那一档单独收集，不再混进 blocked ⇒ 界面不再念成"搬不回"
        out['kept_in_box'].extend(res.get('kept_in_box') or [])
        # ★R322 J1：认领链的改名也得上账（下面 `release_all_boxes` 汇总那一句读的就是
        # 这一格）。keep 之下本来应当恒空，**一旦不空就是 ★R321 那条红线被踩穿**，
        # 所以这里既收集也报警，两件事一起做。
        out['renamed'].extend(res.get('renamed') or [])
        _now = list(stray_entries(b['dir']))
        if _now:
            out['left'][b['box']] = _now
        elif not left:
            # 整盒倒空 ⇒ 这只盒从此归本系统管（空壳留在盘上，★R309 那条接管照常）
            if register_box(b['group'], source or 'claim', app_dir=app_dir, data=own):
                out['registered'].append(b['box'])
            else:
                logger.error('[隐藏盒] 认领后登记没落盘（盒=%s）——下次对账还会把它'
                             '当没登记的再走一遍', b['box'])
    if out['renamed']:
        # ★R322 J1：★R321 把自动/批量路径定成"零改名"，可这条链上**没有任何人读**
        # `move_back` 返回的 `renamed`——报警器是拔了线的。现在接上：真出现了就 ERROR。
        logger.error('[隐藏盒] 认领（来源=%s）：自动链里出现了改名 %d 枚'
                     '（★R321 红线：自动路径一枚都不许改名，请查桌面上这些名字）：%s',
                     source or '?', len(out['renamed']),
                     ['%s→%s' % (x.get('name'), os.path.basename(str(x.get('kept') or '')))
                      for x in out['renamed'][:6]])
    if out['conflicts']:
        # ★R321 J3：自动认领撞名不再替桌面那枚改名，但**必须出声**——不许只躺 app.log，
        # 也不许写成"改过名"（对外口径：自动路径零改名）。
        logger.warning('[隐藏盒] 认领（来源=%s）：%d 枚留在盒里没动（桌面上已经有同名的）',
                       source or '?', len(out['conflicts']))
    if out['total'] or out['left']:
        logger.info('[隐藏盒] 认领（来源=%s）：从 %d 只没登记的盒里找回 %d 枚到桌面；'
                    '还有 %d 枚没被认领（证明不了是我们的，一枚没动）%s',
                    source or '?', len(out['claimed']), out['total'],
                    sum(len(v) for v in out['left'].values()),
                    '（演练）' if dry_run else '')
    return out


# ─────────── ★R324 N3：盒里那几枚"永远散不回来"的陈旧同名枚 ───────────

def box_stale_conflicts(*, app_dir=None, root: Optional[str] = None,
                        desktop: Optional[str] = None) -> List[dict]:
    """盒里那几枚**永远散不回桌面**的陈旧同名枚（只读，一个字节都不动）。

    → [{'name','file','box','path','desk_path','desk_kind','is_dir','same','note'}]

    批79 用户看图报的病：「桌面上还是存在"盒子"和"隐藏"模式共存的情况…w网络工具2-4、
    game 等等文件夹还是隐藏模式，按理说他们应该被搬到盒子里面才对」。根因不是搬不动，
    是**盒里先躺着一枚同名的旧货**，两条链各自都"按规矩办事"，合起来就是死结：
      · 散回那一步 `move_back` 每跑一次都是"桌面上已经有同名的 ⇒ 留在盒里"
        （★R323 L7 用户定案：撞名不搬回、不算失败）；
      · 收纳那一步 `move_into_box` 又搬不进去（盒里那一格已被占）⇒ 退属性法把它藏掉，
        而 `list_boxable` 从此把带隐藏位的那一枚排除在候选之外 ⇒ 永久进不了盒子。
    于是盒里一枚永远出不来、桌面那枚永远进不去，用户看到的就是两种模式共存。

    真机形状（R041 日志包 + 包内两本账逐条抄，见 `tmp/bugflow/20261012_2115/
    N3_取证定案.md`）：`收纳·程序` 里 7 枚**全是文件夹**，`from` 就是桌面上那个同名
    文件夹的真实路径 ⇒ 判据③必须**文件夹也算**，只认 `isfile` 等于这一修没修。

    三条判据全齐才算（宁可漏、不可错——动的是**盒里**的东西，不是用户桌面上的）：
      ① 那只盒在**所有权账本**里登记过（`is_system_box`），且不是「收纳·重复的」；
      ② 盒里那一枚在盘上真存在（文件或文件夹都算），且成员账本给它记着 `moved_at`
         （我们自己搬进去的凭据，★R322 J7 那条口径：只有 `adopted_at` 补记的一律
         照旧保守，不碰）；
      ③ 桌面上此刻**正有同名的那一枚**——按 `move_back` 同一把尺子（`rec['from']`，
         拿不到就退回 `当前桌面 + 文件名`）。

    `same` / `note`：两边内容是不是同一份（`desktop_box.same_content`）。一样的并进
    主确认框那一发直接收；判不出的默认一枚不碰、逐枚带着 `note` 交用户点头
    （★R321 D5 那条红线保留）。
    """
    r = root or desktop_box.get_box_root()
    own, ledger_failed = load_ledger(app_dir)
    if ledger_failed:
        # 认不出哪些盒子是本系统的 ⇒ 一枚都不点名（点错了就会去动用户手工放的东西）。
        logger.error('[隐藏盒] 所有权账本读不动 ⇒ 陈旧同名枚一枚不点')
        return []
    mdata, m_failed = desktop_box.load_manifest(app_dir)
    if m_failed:
        logger.error('[隐藏盒] 成员账本读不动 ⇒ 陈旧同名枚一枚不点')
        return []
    d = desktop or desktop_box.user_desktop_dir()
    out: List[dict] = []
    for box in sorted(own['boxes']):
        if not is_system_box(box) or desktop_box.is_dup_box(box):
            continue
        bd = desktop_box.box_dir_path(box, r)
        if not os.path.isdir(bd):
            continue
        items = ((mdata['boxes'].get(box) or {}).get('items') or {})
        for fn in sorted(items):
            rec = items[fn]
            if not isinstance(rec, dict) or not rec.get('moved_at'):
                continue
            src = str(rec.get('to') or os.path.join(bd, str(fn)))
            if not (os.path.isfile(src) or os.path.isdir(src)):
                continue
            dst = str(rec.get('from') or (os.path.join(d, str(fn)) if d else ''))
            if not dst or not os.path.exists(dst):
                continue
            same, note = desktop_box.same_content(src, dst)
            out.append({'name': desktop_box.display_name(str(fn)), 'file': str(fn),
                        'box': box, 'path': src, 'desk_path': dst,
                        'is_dir': os.path.isdir(src),
                        'desk_kind': (u'文件夹' if os.path.isdir(dst) else u'文件'),
                        'same': bool(same), 'note': note})
    return out


def _reveal_ours(path: str, im=None) -> str:
    """把**我们自己藏的那一枚**显出来（★R324 N3 第二步的前置）。

    → `'ok'`（本来就显的，或已清掉）／`'user'`（是用户自己藏的，一个位都没动）／
      `'fail'`（想清却清不掉）。

    只认指纹：HIDDEN 与 SYSTEM **同时在场**才算本程序藏的（`_set_hidden_bits` 一直是
    成对写这两位）。只有 HIDDEN 没有 SYSTEM ⇒ 是用户自己藏的 ⇒ 一个位都不动
    （★R194b 那条"以指纹为准，别动他的属性"的口径照抄），并且**不当成功**——
    那一枚带着隐藏位就进不了 `list_boxable`，硬搬只会被拒，得把理由说成人话。
    其他属性位（只读/压缩/存档）一律保留——位运算增删，不写 NORMAL。
    """
    try:
        import win32api
        import win32file
    except Exception as e:
        logger.warning('[隐藏盒] 读不到 win32 属性接口，这一枚先不搬进盒: %s', e)
        return 'fail'
    try:
        a = win32api.GetFileAttributes(path)
    except Exception as e:
        logger.warning('[隐藏盒] 读文件属性失败（这一枚先不搬进盒）%s: %s', path, e)
        return 'fail'
    if a == -1:
        logger.warning('[隐藏盒] 读文件属性返回 -1（这一枚先不搬进盒）%s', path)
        return 'fail'
    _H = win32file.FILE_ATTRIBUTE_HIDDEN
    _S = win32file.FILE_ATTRIBUTE_SYSTEM
    if not (a & _H):
        return 'ok'                      # 本来就是显的
    if not (a & _S):
        logger.info('[隐藏盒] %s 只带隐藏位、没有本程序的指纹位 ⇒ 是用户自己藏的，'
                    '一个位都没动', os.path.basename(path))
        return 'user'
    try:
        win32api.SetFileAttributes(path, a & ~(_H | _S))
    except Exception as e:
        logger.warning('[隐藏盒] 清隐藏位失败（这一枚先不搬进盒）%s: %s', path, e)
        return 'fail'
    # 属性法的账本里那一条要跟着销掉，不然 `hidden_names()` 还数着它。
    try:
        hid = getattr(im, '_hidden', None)
        if isinstance(hid, dict):
            _np = os.path.normcase(os.path.abspath(path))
            for k in [k for k, v in hid.items()
                      if os.path.normcase(os.path.abspath(
                          str((v or {}).get('path') or ''))) == _np]:
                hid.pop(k, None)
            if callable(getattr(im, '_save_hidden_state', None)):
                im._save_hidden_state()
    except Exception as e:
        logger.warning('[隐藏盒] 属性法账本里那一条没销掉（不影响这一枚搬进盒）: %s', e)
    desktop_box._notify([path], [os.path.dirname(path)])
    return 'ok'


def yield_stale_box_items(*, app_dir=None, root: Optional[str] = None,
                          desktop: Optional[str] = None, im=None,
                          include_differ: bool = False,
                          dry_run: bool = False) -> dict:
    """把 `box_stale_conflicts` 点出来的那几枚**盒里的旧货**收进「收纳·重复的」，
    再把桌面上那几枚搬进它原来那只盒 ⇒ 桌面上只剩盒子一种模式。

    → {'total','taken','failed','quarantine','manifest','manifest_error',
       'conflicts','differ','moved_in','refused_in','dry_run'}

    两步缺一不可（只做第一步，用户点完按钮看到的还是桌面上那 7 个文件夹，只是不再
    冲突——他报的那句"两种模式共存"一个字没消）：
      第一步 盒里那枚旧货 → 「收纳·重复的」；
      第二步 桌面上那枚 → 先清掉我们的隐藏指纹（`_reveal_ours`），再 `move_into_box`
             搬进它账本上记着的那只盒。

    为什么是"收进重复盒"，不是删掉、也不是给桌面上那枚改名让路：
      · 删——红线，一枚都不删（★R301 第 1 条）；
      · 改名让路——★R321 已经判死（win11 R038 一次改出 40 个带日期的重复名）；
      · 收进「收纳·重复的」——那只盒**只进不出**（三处 `is_dup_box` 闸门），不会被
        "显示全部"/认领链再倒回桌面造第二遍；退路是盒里那份 `清单.json`
        （记着 名字/原名/来自哪只盒），要还原按清单搬回去就行。

    两档口径（★R321 D5 红线保留）：两边内容一样的直接收；`same_content` 判不出的
    默认**一枚不碰**，逐枚带 `note` 放进返回值 `differ`，界面上再问一句，用户点【是】
    才带 `include_differ=True` 收进同一只盒。

    动作口径：同盘 `os.rename`（跨卷一律拒，不复制）；`dry_run` 走完同一条判据链但
    一个字节都不动、一个属性位都不改；成员账本里被搬走的那几条同步删掉（不删的话
    `boxed_members` 还数着它，下一轮又报一遍冲突）。**hide 档压根不调这一枚函数**
    （面板那一侧按 `current_mode` 判档），本函数自己也不动任何属性位以外的东西
    ⇒ 用户定案那句"隐藏模式目前没啥问题，别弄错了"守得住。
    """
    P = desktop_box.DUPE_LOG_PREFIX
    res = {'total': 0, 'taken': [], 'failed': [], 'quarantine': '',
           'manifest': '', 'manifest_error': '', 'dry_run': dry_run,
           'conflicts': [], 'differ': [], 'moved_in': [], 'refused_in': []}
    cand = box_stale_conflicts(app_dir=app_dir, root=root, desktop=desktop)
    res['conflicts'] = cand
    todo = [x for x in cand if x.get('same') or include_differ]
    res['differ'] = [x for x in cand if not (x.get('same') or include_differ)]
    if not cand:
        logger.info(P + u'盒里没有散不回桌面的陈旧同名枚（这一档一枚没动）')
        return res
    if not todo:
        logger.info(P + u'盒里有 %d 枚陈旧同名枚，但每一枚都判不出跟桌面上那枚是不是'
                    u'同一份东西 ⇒ 默认一枚没动（要收请当面点那一句【是】）', len(cand))
        return res
    if dry_run:
        logger.info(P + u'盒里有 %d 枚陈旧同名枚可以收（桌面上已经有同名的，'
                    u'这一批永远散不回来），一枚没动', len(todo))
        return res
    r = root or desktop_box.get_box_root()
    d = desktop or desktop_box.user_desktop_dir()
    qd = desktop_box.dup_box_dir(r)
    res['quarantine'] = qd
    try:
        os.makedirs(qd, exist_ok=True)
    except OSError as e:
        res['quarantine'] = ''
        logger.error(P + u'回收目录写不出，这次一枚没搬：%s - %s', qd, e)
        return res
    mdata, m_failed = desktop_box.load_manifest(app_dir)
    if m_failed:
        # 东西还没动，先把话说清：账本读不动 ⇒ 一枚不搬（★R231 那道无状态拒写闸同款）。
        res['quarantine'] = ''
        res['failed'] = [{'name': x['name'], 'reason': u'盒子账本读不动'}
                         for x in todo]
        logger.error(P + u'盒子账本读不动 ⇒ 陈旧同名枚一枚没搬（%d 枚）', len(todo))
        return res
    qvol = desktop_box._volume_key(qd)
    _dirs = set()
    _nlog = 0
    for x in todo:
        src, box = x['path'], x['box']
        if desktop_box._volume_key(src) != qvol:
            res['failed'].append({'name': x['name'], 'reason': u'跨卷'})
            logger.warning(P + u'%s 没能搬进回收目录：跨卷', x['name'])
            continue
        dst = desktop_box._dup_slot(os.path.join(qd, x['file']))
        if not dst:
            res['failed'].append({'name': x['name'],
                                  'reason': u'在回收盒里找不到空位'})
            logger.warning(P + u'%s 在回收盒里找不到空位', x['name'])
            continue
        try:
            os.rename(src, dst)
        except OSError as e:
            res['failed'].append({'name': x['name'],
                                  'reason': '%s: %s' % (type(e).__name__, e)})
            logger.warning(P + u'%s 没能搬进回收目录：%s: %s',
                           x['name'], type(e).__name__, e)
            continue
        bd = os.path.dirname(src)
        res['taken'].append({'name': os.path.basename(dst), 'base': x['file'],
                             'dir': bd, 'why': 'stale_box', 'to': dst,
                             'box': box, 'desk_path': x['desk_path'],
                             'desk_kind': x['desk_kind']})
        res['total'] += 1
        _dirs.add(bd)
        _dirs.add(qd)
        _it = ((mdata['boxes'].get(box) or {}).get('items') or {})
        if x['file'] in _it:
            del _it[x['file']]
        if _nlog < desktop_box.PER_ITEM_LOG_CAP:
            _nlog += 1
            logger.info(P + u'%s 从盒里收进重复盒（桌面上已经有同名的%s，'
                        u'这一枚永远散不回来）', x['name'],
                        u'，而且那一枚是个文件夹' if x['desk_kind'] == u'文件夹'
                        else u'')
    if res['total'] > _nlog:
        logger.info(P + u'另有 %d 枚同一类没逐条念（上面已念 %d 条）',
                    res['total'] - _nlog, _nlog)
    if res['taken']:
        if not desktop_box.save_manifest(mdata, app_dir):
            logger.error(P + u'成员账本没写回（东西已经搬进回收盒了）——'
                        u'下一次扫描还会把这几枚报成冲突')
        try:
            desktop_box._write_yield_manifest(qd, res['taken'])
            res['manifest'] = os.path.join(qd, desktop_box.YIELD_MANIFEST_NAME)
        except OSError as e:
            res['manifest_error'] = '%s: %s' % (type(e).__name__, e)
            logger.error(P + u'已搬 %d 枚但 清单.json 没写出（%s）'
                        u'——退路是那个目录本身，里面的文件按原名认',
                        len(res['taken']), e)
        logger.info(P + u'已收 %d 枚进 %s（一枚没删，退路在那份 %s）',
                    res['total'], qd, desktop_box.YIELD_MANIFEST_NAME)

    # ── 第二步：腾出来的那一格，把桌面上那枚搬进去（box 档只走一种模式的关键） ──
    _nlog2 = 0
    _freed = {t['base'] for t in res['taken']}
    for x in todo:
        if x['file'] not in _freed:
            continue                     # 这一枚没腾出来（跨卷/没空位/rename 失败）
        box = x['box']
        dp = str(x.get('desk_path') or '')
        if not dp or not os.path.exists(dp):
            continue
        _rv = _reveal_ours(dp, im)
        if _rv != 'ok':
            _why = (u'那一枚是你自己藏的（属性里没有本程序的指纹）⇒ 一个位都没动'
                    if _rv == 'user' else u'隐藏位清不掉')
            res['refused_in'].append({'name': x['name'], 'box': box, 'reason': _why})
            logger.warning(P + u'%s 桌面上那一枚没搬进盒：%s', x['name'], _why)
            continue
        _mi = desktop_box.move_into_box(box, [x['file']], desktop=desktop, root=r,
                                        app_dir=app_dir)
        if _mi.get('moved'):
            res['moved_in'].append({'name': x['name'], 'box': box})
            if _nlog2 < desktop_box.PER_ITEM_LOG_CAP:
                _nlog2 += 1
                logger.info(P + u'%s 桌面上那一枚已经搬进 %s（桌面上从此只剩盒子'
                            u'这一种模式）', x['name'], box)
        else:
            _why = str((_mi.get('refused') or [{}])[0].get('reason') or u'没搬进去')
            res['refused_in'].append({'name': x['name'], 'box': box, 'reason': _why})
            logger.warning(P + u'%s 桌面上那一枚没能搬进 %s：%s（这一枚显在桌面上，'
                           u'下次切分类会再试一次）', x['name'], box, _why)
    if len(res['moved_in']) > _nlog2:
        logger.info(P + u'另有 %d 枚同一类没逐条念（上面已念 %d 条）',
                    len(res['moved_in']) - _nlog2, _nlog2)
    if res['moved_in']:
        logger.info(P + u'已把桌面上 %d 枚搬进各自那只盒（收纳档以搬移为主，'
                    u'属性隐藏只兜底）', len(res['moved_in']))
    if res['taken'] or res['moved_in']:
        desktop_box._notify([t['to'] for t in res['taken']], list(_dirs))
    return res


def reclaim_box(box: str, *, app_dir=None, root: Optional[str] = None,
                desktop: Optional[str] = None, source: str = 'manual',
                apply_layout=None) -> dict:
    """**授权**路径：界面里用户点了「一键搬回」⇒ 这一只盒整盒散回桌面。

    与 `claim_stray_boxes` 的分工：那条是自动的，只敢动拿得准的；这一条是用户点名
    要搬的，盒里剩下"证明不了是我们的"那几枚也一起搬（点了才动，不需要凭据），
    并且倒干净后登记成本系统的盒。搬不回的（同名占用/跨盘）交 `move_back` 判 blocked，
    绝不覆盖用户的东西。
    """
    out: Dict = {'ok': True, 'box': box, 'back': [], 'blocked': [], 'gone': [],
                 'renamed': [], 'left': [], 'reason': '', 'registered': False}
    own, failed = load_ledger(app_dir)
    if failed:
        out['reason'] = '隐藏盒清单读不动，这次一枚都没搬——先确认盒里没有你要的东西'
        logger.error('[隐藏盒] 一键搬回 %s：清单读不动 ⇒ 一发都不动', box)
        return out
    r = root or desktop_box.get_box_root()
    d = desktop or desktop_box.user_desktop_dir()
    bd = desktop_box.box_dir_path(box, r)
    if not os.path.isdir(bd):
        out['reason'] = '盒子里已经没有这个文件夹了'
        return out
    if not d or not os.path.isdir(d):
        out['reason'] = '找不到桌面目录，这次一枚都没搬'
        return out
    if not stray_entries(bd):
        out['reason'] = '这只盒是空的，没有要搬回来的东西'
        return out
    desktop_box.adopt_disk_items(box, root=r, app_dir=app_dir, desktop=d,
                                 source=source or 'reclaim')
    res = desktop_box.move_back(box, root=r, desktop=d, app_dir=app_dir,
                                apply_layout=apply_layout)
    out['back'] = list(res.get('back') or [])
    out['blocked'] = list(res.get('blocked') or [])
    out['gone'] = list(res.get('gone') or [])
    out['renamed'] = list(res.get('renamed') or [])
    out['place'] = res.get('place')
    out['left'] = list(stray_entries(bd))
    out['ok'] = bool(out['back']) and not out['blocked']
    if not out['left'] and not out['blocked']:
        out['registered'] = bool(register_box(group_of_box(box), source or 'reclaim',
                                              app_dir=app_dir, data=own))
    logger.info('[隐藏盒] 一键搬回「%s」（来源=%s）：搬回 %d 枚，搬不回 %d 枚，'
                '盒里还剩 %d 枚', box, source or '?', len(out['back']),
                len(out['blocked']), len(out['left']))
    return out


# ───────────────────────── 路由器主入口 ─────────────────────────

def apply_group_visibility(im, visible_names, *, group: str, source: str = '',
                           settings=None, app_dir=None, desktop=None, root=None,
                           apply_layout=None, positions_provider=None,
                           dry_run: bool = False, log=None) -> dict:
    """按当前模式把"这一组该显示谁"落到桌面上。

    → {'mode', 'group', 'source', 'ok',
       # hide 档：直接是 apply_visibility 的返回 dict（hidden/shown/failed/save_ok…）
       # box  档：'hidden_now'(真搬走) / 'shown_now'(真散回) / 'kept'(留在盒里没动)
       #          / 'conflicts'(桌面已有同名 ⇒ 这一枚没动) / 'attr_fallback'
       #          ([{'name','reason'}]) / 'blocked' / 'gone'
       #          / 'place'(散回归位回执) / 'ledger_failed'
       'summary': {'in': n, 'out': n, 'kept': n, 'fallback': n}}

    `log`（★R321 H 组）：调用方的界面日志通道（dock/rank/parental 各自的 `_log`）。
    这一条是**批量**链——用户点的是"显示这一组"，不是"倒空这一只盒"，所以散回一律
    `on_conflict='keep'`（桌面上那一枚的名字和内容一个字节都不动），留在盒里的那几枚
    必须经 `log` 说给人听，不许只躺在 app.log 里（★R320"零枚不许静默"同款口径）。
    """
    if settings is None:
        settings = load_settings(app_dir)
    mode = current_mode(settings)
    visible = {str(v) for v in (visible_names or set())}
    if mode == MODE_HIDE:
        # 逐字节转发：这一档不许夹带任何新逻辑（★R303/★R304 那套尺寸与自愈都建立在
        # "属性法行为没变"之上）。
        res = im.apply_visibility(visible) if im is not None else {}
        res = dict(res or {})
        res.update({'mode': MODE_HIDE, 'group': group, 'source': source,
                    'ok': bool(res.get('save_ok', True))})
        return res

    box = box_name_for(group)
    out: Dict = {'mode': MODE_BOX, 'group': group, 'source': source,
                 'hidden': 0, 'shown': 0, 'failed': [], 'save_ok': True,
                 'hidden_now': [], 'shown_now': [], 'kept': [],
                 'attr_fallback': [], 'blocked': [], 'gone': [],
                 'stuck': [], 'renamed': [], 'conflicts': [],
                 'kept_in_box': [], 'place': None}

    # ★R325 F6（收窄档）：只有"用户刚点的这一发"允许在**已留底的坏账本**上按空表继续
    # （边搬边重新登记，坏一次就终身点不动是原来的病）；`OSError`/留底失败仍走下面
    # 那一支保守兜底。剪幽灵、散回、接管空壳、认领散落四条读路径不传这个旗标。
    own, ledger_failed = load_ledger(app_dir, recover_for_write=True)
    if ledger_failed:
        # 认不出哪些盒子是自己的 ⇒ 一发都不搬（搬错会把用户手工放的东西搞丢）；
        # 但不空手而归：退回双属性隐藏，用户这一组的"只显示这些"照样生效。
        out.update({'ok': True, 'ledger_failed': True, 'degraded': 'ledger_failed',
                    'reason': '隐藏盒清单读不动，这次先按双属性隐藏，一枚都没搬'})
        logger.error('[隐藏盒] 账本读不动 ⇒ 本次切换一枚都不搬（组=%s）', group)
        out['hide_result'] = _attribute_pass(im, visible)
        _merge_hide(out, out['hide_result'])
        return out

    if not desktop_box.validate_box_name(box)[0]:
        # ★R318 K3（审查报告 1455 批）：旧写法这里只写 ok=False/reason 就 return
        # ——**一个动作都没做**。同函数另两条降级路（账本读不动、盘上有同名实物盒）
        # 都退 `_attribute_pass`，独这一条不退；而调用方 core/dock.py:1654 只把
        # reason 打进日志、不兜底 ⇒ box 档下点这个分类＝桌面保持上一个分类不动，
        # 用户读成"这个分类点不动"。分类创建走 askstring、没有长度上限，所以
        # "组名太长/带非法字符"是正常用户就能踩到的（>37 字即中）。
        return _fallback_to_attribute(
            out, im, visible, 'bad_box_name',
            '组名不能当盒子名：%s，这一组先按双属性隐藏'
            % (desktop_box.validate_box_name(box)[1],))

    d = desktop or desktop_box.user_desktop_dir()
    r = root or desktop_box.get_box_root()
    # ★R320（用户 P0「图标丢失」，win11 R037 实案 20+ 条"在盘上但没登记 ⇒ 一枚不搬"）：
    #   切分类这条链上先跑一次**认领**——盘上躺着、账本没登记的旧盒图标，凡显示名在分类
    #   名单里的都先找回桌面，找回的那些若这次该显示就留在桌面（R5b），这次不该显示的
    #   照常进本组盒。旧写法是直接退属性法：用户点一下分类，卡住的图标继续躺在盒里，
    #   桌面上看不见也搬不出来。空名单（新装/没分类过）⇒ 这一趟零动作，行为同修前。
    _cl = claim_stray_boxes(app_dir=app_dir, root=r, desktop=d,
                            source=(source or '?') + '-switch', dry_run=dry_run)
    out['claimed'] = _cl
    # ★R321 H 组：认领那一步撞上的同名枚也算这一组的冲突（同一句界面日志一起报）。
    out['conflicts'].extend(_cl.get('conflicts') or [])
    # ★R323 L7：认领那一步"撞名留在盒里"的那一档也跟着汇总走（同 `renamed` 那两条的
    # 道理——只声明不收集就是根拔了线的灯，★R322 K3 同款）。
    out['kept_in_box'].extend(_cl.get('kept_in_box') or [])
    # ★R322 K3（★R322 J1 那条病在这一头还没接上）：认领返回的改名也必须并进汇总，
    #   否则下面那句 `让路改名=%d` 读的是**散回那一步**那一格，认领改掉的枚数永远
    #   不进表＝报警器看着在、线是拔的。自动路径按 ★R321 红线本该恒 0，
    #   一旦不空就得在这句里显形。
    out['renamed'].extend(_cl.get('renamed') or [])
    # ★R310：候选目录按**调用方有没有显式指定**分两手——不传（生产链）⇒ 用户桌面 +
    # 公共桌面都算，`Qoder CN` 那类机器级快捷方式才进得了收纳；传了（门禁/沙箱给
    # 临时目录）⇒ 只扫那一个，真公共桌面一枚都不许被测试吃进来。
    on_desk = desktop_names(desktop)
    boxed = boxed_members(app_dir, r)

    # 组盒已存在于盘上、却没在账本里 ⇒ 多半是**用户手工建的同名盒子**，绝不能当自己的盒用。
    # 这一组退回双属性隐藏：用户看到的仍然是"只有这一组"，只是没走盒子（要点名，不许静默）。
    #
    # ★R309（#360，真机 R026 日志 11 条 + 用户 42 批「c盘下面已经有文件夹存在是正常的…
    #   直接覆盖就行了吧」）：**空目录不是用户的东西**。用户把
    #   `桌面盒子deskbits\` 里面的文件删了（他要的"这版才能用"就是这么做的），目录壳
    #   还留在盘上、账本里却没有 ⇒ 旧写法整组退属性法 ⇒ 用户看到的是"收纳无效、
    #   图标还是隐藏的"，`Qoder CN` 那类搬不走也是同因。空壳直接登记接管；
    #   **里面有东西（或读不动）照旧一枚不搬**——红线不变：绝不碰用户的东西。
    if box not in own['boxes'] and os.path.isdir(desktop_box.box_dir_path(box, r)):
        _bd = desktop_box.box_dir_path(box, r)
        _stray = stray_entries(_bd)
        if _stray:
            # ★R322 J7：**读凭据**。成员账本里 `moved_at` 是 `move_into_box` 里
            # `os.rename` 成功之后才写的（自证不了别人放的东西），而 `adopted_at` 是
            # 认领链按盘上实名补记的（自证）。这两个字段盘上从 ★R310/★R320 就分开了，
            # 但全仓除写入点**零读取** ⇒ 只要"盒里有东西 + 桌面上有同名挡着"就永远
            # 认不出是自己的盒：真机 R039 的 `收纳·程序` 7 枚全是文件夹、桌面上各有一个
            # 同名文件夹，倒不空 ⇒ 不登记 ⇒ 每切一次分类降级一回 = 用户 77 批那句
            # "盒子功能几乎不起作用，回退成隐藏功能了"。有搬入凭据的接管，只有补记的照旧保守。
            _prov = desktop_box.box_provenance(box, app_dir)
            if _prov == 'moved':
                logger.warning('[隐藏盒] 目标盒 %s 在盘上但没登记、里面有 %d 枚（%s）——'
                               '成员账本里记着 moved_at 搬入凭据 ⇒ 是本系统搬进去的，'
                               '登记后照常搬，不再每切一次分类降级一回（★R322 J3/J7）',
                               box, len(_stray), '、'.join(_stray[:5]))
                if not dry_run and not register_box(group, source, app_dir=app_dir,
                                                    data=own):
                    return _fallback_to_attribute(
                        out, im, visible, 'register_failed',
                        '盒清单登记不上（hidden_box.json 写不进去），这一组先按双属性隐藏')
            else:
                # ★R322 J3：降级那一句必须说清**谁挡谁**。旧那句只列盒里的名目，用户
                # 看不出是桌面上那一枚在挡路，也就不知道该关程序还是去面板点按钮。
                _blk = [n for n in _stray
                        if os.path.exists(os.path.join(d, n))]
                _kind = (u'成员账本里只有认领时补记的 adopted_at（自证，证明不了是谁放的）'
                         if _prov == 'adopted' else u'成员账本压根没记过这只盒')
                reason = ('盒子里已有同名的「%s」，里面有 %d 枚东西（%s），'
                          '而%s ⇒ 证明不了是本系统建的，这一组先按双属性隐藏%s'
                          % (box, len(_stray), '、'.join(_stray[:5]), _kind,
                             ('；其中 %d 枚是被**桌面上同名的那一枚**挡着的（如「%s」），'
                              '程序不敢替你挪桌面上那一枚，要搬请在资源页的盒子面板'
                              '点对那只盒' % (len(_blk), _blk[0])) if _blk else ''))
                logger.warning('[隐藏盒] 目标盒 %s 在盘上但没登记、里面有 %d 枚（%s）；'
                               '其中 %d 枚桌面上正有同名的挡着（%s）；%s ⇒ '
                               '一枚不搬，交回属性法',
                               box, len(_stray), _stray[:8], len(_blk), _blk[:8], _kind)
                hide = _attribute_pass(im, visible)
                _merge_hide(out, hide)
                out.update({'ok': True, 'reason': reason,
                            'degraded': 'unregistered_box',
                            'attr_fallback': [{'name': '(整组)', 'reason': reason}],
                            'summary': {'in': 0, 'out': 0, 'kept': 0, 'fallback': 1}})
                return out
        logger.info('[隐藏盒] 目标盒 %s 在盘上但没登记，是个**空目录** ⇒ '
                    '登记成本系统的盒，照常搬（★R309 #360）', box)
        if not dry_run and not register_box(group, source, app_dir=app_dir, data=own):
            # ★R318 K4：接管都记不上 ⇒ 后面照样记不上，直接收手（一枚不搬）。
            return _fallback_to_attribute(
                out, im, visible, 'register_failed',
                '盒清单登记不上（hidden_box.json 写不进去），这一组先按双属性隐藏')

    universe = set(on_desk) | set(boxed)
    want_boxed = {n for n in universe if n not in visible
                  and not any(h in n.lower() for h in _RESIDENT_HINTS)}
    to_box = sorted(want_boxed - set(boxed))           # 桌面上、这次该收走
    to_back = sorted(set(boxed) - want_boxed)          # 盒子里、这次该回来
    kept = sorted(want_boxed & set(boxed))             # 盒子里、这次仍不该显示 ⇒ 一枚不动

    # ★R318 K4：**登记这一枪排在搬运之前**，而且消费返回值。旧写法三处
    # `register_box(...)` 全把返回值当语句丢了 ⇒ `hidden_box.json` 写不进（data 目录被
    # 备份/杀软瞬时锁住、磁盘满）时：文件已真搬进盒、成员账本已记，但**所有权账本
    # 没登记**——而 `release_all_boxes` 只遍历 `own['boxes']`，于是「显示全部」/退出
    # 家长模式/清空数据永远散不回这些图标，`unhide_all` 也看不见它们，下次切换还把
    # 自己的盒当"盘上有目录、账本没登记"的散客反复告警。
    # 现在：记不上就**一枚都不搬**，退双属性隐藏（用户这一组照样生效）。
    if to_box and not dry_run and box not in own['boxes']:
        if not register_box(group, source, app_dir=app_dir, data=own):
            return _fallback_to_attribute(
                out, im, visible, 'register_failed',
                '盒清单登记不上（hidden_box.json 写不进去），这一组先按双属性隐藏')

    # 1) 先散回该回来的（按盒分组，各回各盒）
    if to_back and not dry_run:
        by_box: Dict[str, List[str]] = {}
        for n in to_back:
            by_box.setdefault(boxed[n], []).append(n)
        back_all, place_last = [], None
        for b, ns in sorted(by_box.items()):
            # ★R321 H 组（S6 复核命中，同一失效类的第二起）：这一条也是**批量**链——
            #   启动「退出恢复=当前」(`gui/main_window.py:677 → _reactivate_last_slot
            #   → dock._do_activate_slot`)、排行榜显示 (`gui/rank_display.py:393`)、
            #   家长模式 (`utils/parental_mode.py:214`) 都会走到这里，用户点的是
            #   "显示这一组"而不是"倒空这一只盒"。以前没带 `on_conflict` ⇒ 吃默认
            #   'resolve' ⇒ 桌面同名那枚会被改个带日期的名让路（win11 R038 那 40 发
            #   就是这条批量链上的另一侧走出来的）。现在：桌面上那一枚一个名字、
            #   一个字节都不动，这一枚留在盒里并经 `log` 出声；要真搬，请在资源页
            #   的盒子面板对**那一只盒**点名（`reclaim_box` / `_do_unbox` 两条）。
            res = desktop_box.move_back(b, root=r, desktop=d, names=ns,
                                        app_dir=app_dir, on_conflict='keep',
                                        apply_layout=apply_layout)
            back_all.extend(res.get('back') or [])
            place_last = res.get('place') or place_last
            out['blocked'].extend(res.get('blocked') or [])
            out['gone'].extend(res.get('gone') or [])
            out['conflicts'].extend(res.get('conflicts') or [])
            # ★R323 L7：这一档是"按用户定案留在盒里"，不是失败 ⇒ 单独收集，
            # 上面那格 `blocked` 从此不再吃掉它（win11 R038 那类"搬不回=7 一直不降"的读数）。
            out['kept_in_box'].extend(res.get('kept_in_box') or [])
            # ★R310（用户 08:40 第 1 条后半）：散回时给桌面上那一枚"改名让路"的那几枚，
            #   界面上必须单独点出来——用户看见的是"桌面多了一个带日期的名字"，
            #   闷在"回了 N 枚"里等于没说。★R321 之后这一条批量链不再改名，
            #   `renamed` 恒空（转发留着是给面板那两条点名链同一份形状）；
            #   代价由 `conflicts` 承担 ⇒ 留在盒里几枚必须经下面的 `log` 说出去。
            out['renamed'].extend(res.get('renamed') or [])
        out['shown_now'] = back_all
        out['place'] = place_last

    # 2) 再收走该收的（一次批量搬进本组盒；搬走前先记坐标）
    if to_box:
        pos = _positions_of(im, to_box, positions_provider)
        res = desktop_box.move_into_box(box, to_box, desktop=desktop, root=r,
                                        app_dir=app_dir, positions=pos,
                                        dry_run=dry_run)
        out['hidden_now'] = res.get('moved') or []
        out['attr_fallback'] = res.get('refused') or []
        if res.get('save_ok') is False:
            out['ok'] = False
            out['reason'] = '盒子账本写不进去，已尽量退回桌面'
        elif not dry_run and not register_box(group, source,
                                              app_dir=app_dir, data=own):
            # ★R318 K4：走到这里文件**已经真进盒**（登记在搬运之前已成功过一次，
            # 这一枪只是把来源/组名补写回去）。记不上不必把用户刚看到的收纳再退回去
            # （那才是"点了没反应"），但必须点名：所有权这条没更新，`save_ledger`
            # 失败那份原始登记仍在盘上（写失败不动原文件），所以散回链不会断。
            out['register_failed'] = True
            logger.error('[隐藏盒] 搬完之后更新登记没落盘（组=%s）——盒内 %d 枚'
                         '仍按上一次登记的来源散回', group, len(out['hidden_now']))
    elif not dry_run and to_back and not register_box(group, source,
                                                      app_dir=app_dir, data=own):
        # 这一趟只散回、没搬入：记不上不影响散回链（源头盒本来就是登记过的），
        # 但"这一组有自己的盒"这件事没记上 ⇒ 同样点名，不许静默。
        out['register_failed'] = True
        logger.error('[隐藏盒] 散回后登记本组盒没落盘（组=%s）——下次切到这一组'
                     '会重新建盒并接管空壳', group)

    # 3) 收尾一律过一遍属性法（U5）：搬不走的那几枚由此藏掉，
    #    从 hide 档切过来时残留在属性里的该显示图标由此恢复。visible 之外的早在
    #    第 2 步就搬进盒子了，所以这一遍在理想情况下是空操作。
    if not dry_run:
        out['hide_result'] = _attribute_pass(im, visible)

    # ★R310（#366 N-1b，真机 R027「两个图标不能搬走、日志里连名字都没有」）：属性法那份
    #   `failed` 历轮只躺在 `hide_result` 里，界面读的是平铺键 ⇒ 用户只看见"收纳无效"，
    #   软件明明知道是哪一枚却不说。这里平铺出来，再挑出**既没进盒子也藏不了**的那几枚
    #   （桌面上根本没有本体＝资源管理器残影，`LibreOffice 26.8` 是实案）。
    #   虚拟系统图标不算 stuck——它们本来就不归收纳管，界面另有一句"系统图标跳过"。
    _fail = [str(x) for x in ((out.get('hide_result') or {}).get('failed') or [])]
    out['failed'] = _fail
    _virt = desktop_box._virtual_names()
    # ★R311 自纠第二条（只读复核命中）：`_virtual_names()` 里那个 'Administrator' 只是
    # **历史别名**——「用户文件夹」那枚从 `icon_manager.py:430` 起按**当前账户名**动态解析。
    # 账户名不叫 Administrator 的机器上，那一枚在属性法里"找不到本体" ⇒ 被点名成
    # "没能收走"。同款口径 `icon_manager.py:1051/1436` 早就带上它，收纳这一处漏了。
    _uf = getattr(im, 'get_user_folder_icon_name', None)
    if callable(_uf):
        _virt.add(str(_uf() or ''))
    # ★R311（批67，真机 R028 日志每次切换 43/15/9 枚"既没进盒子也藏不了"）：这一句
    # 数的是**属性法找不到本体**的名字，而第 2 步刚搬进盒的那批正好就是这个状态——
    # Explorer 的 ListView 还留着它们的残影（F5 之后才散）。它们明明在盒里，
    # 却被报成"既没进盒子也藏不了"，用户读成"几十枚卡住了"。判据补一条正面的：
    # **这一轮进了盒的（原先就在 + 刚搬走的）不算卡住**，剩下的才是真残影。
    _in_box = set(boxed) | {str(x.get('name') or '') for x in out['hidden_now']}
    out['stuck'] = [n for n in _fail if n not in _virt and n not in _in_box]
    # ★R311 自纠（批67，改 A 打通 B）：`stuck` 一变小，界面那句"N 个系统图标跳过"要是
    # 拿 `len(failed) - len(stuck)` 反推，就会把**刚搬进盒的那批**也算成系统图标 ⇒ 假数
    # 从"43 枚没能收走"换成"43 个系统图标跳过"，一样错。跳过数由这里直接数虚拟名给出，
    # 调用方不做减法。
    out['skipped_system'] = sum(1 for n in _fail if n in _virt)
    if out['stuck']:
        logger.warning('[隐藏盒] %d 枚既没进盒子也藏不了（桌面上没有本体）：%s',
                       len(out['stuck']), '、'.join(out['stuck'][:8]))

    out['ok'] = out.get('ok', True)
    out['kept'] = kept
    # ★R321 H 组：认领那一步和上面散回那一步可能撞上**同一枚**（认领先把它补记进
    #   成员账本、散回又按账本试一次）⇒ 按 文件名+显示名 去重，免得界面报"2 枚"
    #   而桌面实际只有 1 枚没动。
    if out['conflicts']:
        _seen, _uniq = set(), []
        for x in out['conflicts']:
            _k = (str(x.get('file') or ''), str(x.get('name') or ''))
            if _k not in _seen:
                _seen.add(_k)
                _uniq.append(x)
        out['conflicts'] = _uniq
    _cf = len(out['conflicts'])
    if _cf and callable(log):
        # 与 `release_all_for` 同一句文案（词典里这条整句★R321 已经建好）：批量链
        # 不碰桌面上那一枚 ⇒ 留在盒里的必须说给人听，并告诉他去哪一条点名链搬。
        log(u'📦 还有 %d 枚留在盒里没动（桌面上已经有同名的）'
            u'——要搬请在资源页的盒子面板点对那只盒' % _cf)
    out['summary'] = {'in': len(out['hidden_now']), 'out': len(out['shown_now']),
                      'kept': len(kept), 'fallback': len(out['attr_fallback']),
                      'yielded': len(out['renamed'])}
    logger.info('[隐藏盒] 组=%s 来源=%s 搬走=%d 散回=%d 留在盒里=%d 退回属性法=%d'
                ' 让路改名=%d%s',
                group, source or '?', out['summary']['in'], out['summary']['out'],
                out['summary']['kept'], out['summary']['fallback'],
                out['summary']['yielded'],
                '（演练）' if dry_run else '')
    return out


def release_all_boxes(im=None, *, app_dir=None, root: Optional[str] = None,
                      desktop: Optional[str] = None, source: str = '',
                      dry_run: bool = False, apply_layout=None) -> dict:
    """把**本系统搬进盒里的**图标全部散回桌面——「显示全部」那一侧的退路。

    为什么必须有这一条（★R305 真机 2026-10-07 实撞，P1）：盒档切过一次分类以后，
    那些"这次不该显示"的**真的不在桌面上**了，而历轮「显示全部」只会走属性法
    （`im.unhide_all()`），它看不见盒里的东西 ⇒ 点完"全部"桌面只剩三枚，
    用户读成"图标被软件弄丢了"。桌面上只要还有本系统登记的盒，这一条就必须先散回。

    边界：只动 `hidden_box.json` 登记过的盒，外加 ★R320 补的两条——
    ① 登记过但成员账本**一枚都没记**（换了新版本目录、或 ★R231b 损坏留底后按空表
      继续正是这条路）⇒ 先按**盘上实名**补记再散回，旧写法 `if not items: return`
      连一句警告都不打。**账本记过一些、盘上又多出几枚**的那种不自动补：证明不了
      是我们的（★R305 A8 撞到过"别人那枚被搬回桌面还连累同名文件改名"），只出声
      进 `没被认领`，动手由用户在盒子面板点「搬回未认领的」；
    ② 盘上有名目、账本压根没登记的组盒 ⇒ 先走一次 `claim_stray_boxes`（只搬分类名单
      里点过名的），剩下的进 `stray` 点名，不许只躺在 app.log 里。
    账本读不动 ⇒ 一发不散并如实点名；桌面已有同名 ⇒ 走 `move_back` 的
    `on_conflict='keep'`：**桌面上那一枚一个字节、一个名字都不动**，这一枚留在盒里并
    进 `conflicts` 点名
    （★R321——docstring 这句承诺在 R038 之前只是话，闸门没有，自动链照 ★R310 连开 40 发
    改名；所以这一把是可以反复点的）。
    """
    out = {'ok': True, 'released': {}, 'total': 0, 'blocked': [], 'gone': [],
           'renamed': [], 'conflicts': [], 'kept_in_box': [],
           'reason': '', 'dry_run': dry_run,
           'claimed': {}, 'stray': {}}
    own, ledger_failed = load_ledger(app_dir)
    if ledger_failed:
        out['reason'] = '隐藏盒清单读不动，这次一枚都没散回——先确认盒里没有你要的东西'
        logger.error('[隐藏盒] 显示全部：账本读不动 ⇒ 一发都不散（来源=%s）',
                     source or '?')
        return out

    r = root or desktop_box.get_box_root()
    d = desktop or desktop_box.user_desktop_dir()

    # 1) 盘上有、账本没登记的组盒：能证明是我们的先认领回来（★R320 R4）
    cl = claim_stray_boxes(app_dir=app_dir, root=r, desktop=d,
                           source=(source or '?') + '-release', dry_run=dry_run)
    out['claimed'] = cl.get('claimed') or {}
    out['total'] += int(cl.get('total') or 0)
    out['stray'] = cl.get('left') or {}
    out['blocked'].extend(cl.get('blocked') or [])
    out['conflicts'].extend(cl.get('conflicts') or [])
    # ★R322 J1（真机 R039 汇总行 `让路改名=0` 是死数）：旧写法只并了第 2 步散回链那一份，
    # 而第 1 步认领链返回的 `renamed` **从来没进过汇总** ⇒ 底下那句
    # `让路改名=%d` 读的是一份永远为空的表：报警器看着在、其实拔了线。
    out['renamed'].extend(cl.get('renamed') or [])

    # 2) 登记过的盒：以盘为准逐只散回，散回前补记成员账本（★R320 R3 孤儿态 B）
    for box in sorted((own.get('boxes') or {}).keys()):
        if desktop_box.is_dup_box(box):
            # ★R323 L8：账本里正常情况下压根不该有这只盒（dedupe 走的是纯目录创建）。
            # 万一被导入/历史数据带进来了，这里也跳过——补记和散回都不碰它，
            # `move_back` 那一侧还有一道回绝，两道闸同时成立。
            continue
        try:
            bp = desktop_box.box_dir_path(box, r)
            if not os.path.isdir(bp) or not os.listdir(bp):
                continue
        except Exception as e:
            logger.error('[隐藏盒] 显示全部：盒 %s 目录读不动（这一只跳过）: %s', box, e)
            continue
        # ★R320 R3（孤儿态 B）：成员账本**一枚都没记**的那只盒才按盘上实名补记。
        # 记过一些、盘上又多出几枚的盒**不自动补**——那几枚和"上一个版本收进来却
        # 没记上"长得一模一样，但也可能是用户自己丢进盒文件夹的东西（★R305 A8 就是
        # 这一格：补记以后「显示全部」会把别人那枚搬回桌面，还要给桌面上同名的
        # 那枚改名让路）。所以这条路只出声（进 `没被认领`，面板上出「搬回未认领的」），
        # 动手要用户点——见 `gui/desktop_box_panel.py::_do_reclaim`。
        _mem, _mem_failed = desktop_box.load_manifest(app_dir)
        _items_here = ((_mem.get('boxes') or {}).get(box) or {}).get('items') or {}
        if (not dry_run and is_system_box(box) and not _mem_failed
                and not _items_here):
            desktop_box.adopt_disk_items(box, root=r, app_dir=app_dir, desktop=d,
                                         source=(source or '?') + '-release')
        res = desktop_box.move_back(box, root=r, desktop=d, app_dir=app_dir,
                                    apply_layout=apply_layout, dry_run=dry_run,
                                    on_conflict='keep')
        back = list(res.get('back') or [])
        if back:
            out['released'][box] = back
            out['total'] += len(back)
        out['blocked'].extend(res.get('blocked') or [])
        out['conflicts'].extend(res.get('conflicts') or [])
        # ★R323 L7：撞名留在盒里的那一档单独收集，不再混进 blocked ⇒ 界面不再念成"搬不回"
        out['kept_in_box'].extend(res.get('kept_in_box') or [])
        out['gone'].extend(res.get('gone') or [])
        out['renamed'].extend(res.get('renamed') or [])
        # ★R320：账本记过一些、散回后盒里还剩没记的名目 ⇒ 进 `stray` 点名。
        # 这一格不自动搬（见上面那段注释），但**必须出声**——面板拿它出
        # 「没被认领」那一句，用户点「搬回未认领的」才动（★R305 A8 那枚 `f` 的归宿）。
        if _items_here and not _mem_failed:
            try:
                _left = [fn for fn in sorted(os.listdir(bp))
                         if str(fn).lower() not in desktop_box.NEVER_BOX
                         and fn not in _items_here]
            except OSError as e:
                logger.warning('[隐藏盒] 显示全部：盒 %s 散回后又读了一遍目录，没读动: %s',
                               box, e)
                _left = []
            # ★R321：撞名留在盒里的那几枚已经进了 `conflicts`（另有一句准话），
            # 不要再混进"没被认领"——它们是认了领、只是不敢动桌面那一枚。
            _cf_names = {str(x.get('file')) for x in (res.get('conflicts') or [])}
            _left = [fn for fn in _left if str(fn) not in _cf_names]
            if _left:
                out['stray'][box] = _left

    if out['renamed'] and not dry_run:
        # ★R322 J1：两条链的改名并到这里统一报警（口径与 ★R321 一致：**自动路径零改名**）。
        # 计数活了而没人喊，等于下一个 R038 还是靠用户数桌面图标发现。
        logger.error('[隐藏盒] 显示全部：自动链里出现了改名 %d 枚'
                     '（★R321 红线：自动路径一枚都不许改名）：%s',
                     len(out['renamed']),
                     ['%s→%s' % (x.get('name'),
                                 os.path.basename(str(x.get('kept') or '')))
                      for x in out['renamed'][:6]])

    if out['conflicts']:
        # ★R321 J3：批量路径零改名的同时必须出声——几枚留在盒里、为什么、去哪搬。
        logger.warning('[隐藏盒] 显示全部：%d 枚留在盒里没动（桌面上已经有同名的）'
                       '——要搬请在资源页的盒子面板点对那只盒', len(out['conflicts']))

    if out['stray']:
        # ★R320 R3c/R4b：零散回不许静默——证明不了是我们的那几枚必须说给人听，
        # 界面（gui/desktop_box_panel.py）拿这个 dict 出「没被认领」那一句 + 一键搬回。
        logger.warning('[隐藏盒] 显示全部：还有 %d 枚在盒里没被认领（%s）——它们没登记在'
                       '成员账本里（或整盒没在分类名单上），程序不敢替你搬，'
                       '资源页的盒子面板可以一键搬回',
                       sum(len(v) for v in out['stray'].values()),
                       '、'.join('%s:%d' % (k, len(v))
                                 for k, v in sorted(out['stray'].items()))[:200])
    if out['total'] or out['blocked'] or out['stray']:
        logger.info('[隐藏盒] 显示全部 来源=%s 散回=%d 枚（其中认领=%d 枚）动过的盒=%d 只'
                    ' 搬不回=%d 已不在盒里=%d 让路改名=%d 没被认领=%d%s',
                    source or '?', out['total'],
                    sum(len(v) for v in (out['claimed'] or {}).values()),
                    len(out['released']), len(out['blocked']), len(out['gone']),
                    len(out['renamed']), sum(len(v) for v in out['stray'].values()),
                    '（演练）' if dry_run else '')
    return out


def release_all_for(im, source: str = '', log=None) -> int:
    """「显示全部」那一侧的固定动作：先把本系统的盒散回，再把结果报进界面日志。

    悬浮条 / 家长模式退出 / 退出恢复那几处 ★R305 已经各自内联接好并经真机验证，
    不再动；管理页按钮、规则页撤销筛选、清空个人数据这三处共用这一条，免得同一段
    try + 两句文案抄三份。**本身不抛**：调用方的"清属性"这一步不许因为散回失败而中断
    （★R230b：兜底可以吞异常，但必须留一行日志）。
    """
    try:
        r = release_all_boxes(im, source=source)
        n = int(r.get('total') or 0)
        if r.get('reason') and callable(log):
            log(u'⚠ ' + str(r['reason']))
        if n and callable(log):
            log(u'📦 显示全部：先从 %d 只盒子搬回 %d 枚，再恢复属性'
                % (len(r.get('released') or {}), n))
        _left = sum(len(v) for v in (r.get('stray') or {}).values())
        if _left and callable(log):
            # ★R320（用户 P0「图标丢失」）：这句话以前只进 app.log，用户看不见 ⇒
            # 只能自己拖图标。界面日志点出来，并告诉他去哪一键搬回。
            log(u'📦 还有 %d 枚在盒里没被认领（分类名单里没有它的名字，程序不敢替你搬）'
                u'——资源页的盒子面板可以一键搬回' % _left)
        _cf = len(r.get('conflicts') or [])
        if _cf and callable(log):
            # ★R321 J3（win11 R038 那 40 发）：自动路径不碰桌面上那一枚 ⇒ 留在盒里的
            # 那几枚必须报进界面日志，并且口径是"没动"，不是"改过名"。
            log(u'📦 还有 %d 枚留在盒里没动（桌面上已经有同名的）'
                u'——要搬请在资源页的盒子面板点对那只盒' % _cf)
        return n
    except Exception as e:
        logger.error('[隐藏盒] 散回失败（来源=%s，后续步骤照走）: %s', source or '?', e)
        return 0
