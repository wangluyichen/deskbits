# -*- coding: utf-8 -*-
"""★R301（批57）「桌面盒子」：把选中的桌面图标**真的搬进**用户文件夹下的
`桌面盒子deskbits\\<盒子名>\\`，要退再一键原路搬回桌面。

和双属性隐藏（core/icon_manager 的 hidden_state.json）的分工：
- 隐藏＝原地给文件加 HIDDEN|SYSTEM 位，条目还在桌面目录里，切分类走这条；
- 搬家＝文件离开桌面目录，桌面上真的少掉这些条目，手动收纳走这条。
两本账天然不重叠：本模块**拒绝**收纳带隐藏位的条目，所以不会出现
"搬走了、隐藏恢复链回头找不到路径"的错位，icon_manager 一行都不用改。

四条硬规矩（用户 57 批定案，★R310 第 2 条按用户 08:40 口径放宽）：
1. 只用 os.rename —— 同卷是指目录改名（瞬时、不复制数据）；跨卷 rename 会抛错，
   本模块就此拒绝，绝不"复制一份再删原件"（中途失败会留两份）；
2. 桌面已有同名 → **绝不覆盖用户改过的东西**：与盒里全同（大小+内容+修改时间）才算
   同一枚的两个副本、直接覆盖；对不上就给桌面那份同目录改个带日期的名"让路"
   （两份都留下）；是目录或让不出空位 → 整条拒绝，一份都不动；
3. 批量 shell 通知必须离开主线程（主线程连发 SHChangeNotify 会原生 abort）；
4. 导入期不建任何目录。
"""
import ctypes
import hashlib
import json
import logging
import os
import re
import struct
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence

logger = logging.getLogger("deskbits")

BOX_ROOT_NAME = '桌面盒子deskbits'
MANIFEST_VERSION = 1
MANIFEST_NAME = 'desktop_box.json'
MAX_BOX_NAME = 40
# ★R322 J5：账本从程序目录搬进盒根后，旧那份改这个后缀留底（一枚没删）
MOVED_ASIDE_SUFFIX = '.已搬家'

# 桌面目录里的系统占位文件：搬走会把桌面搞乱，永远不列进可收纳清单
NEVER_BOX = ('desktop.ini', 'thumbs.db')

_ILLEGAL_CHARS = set('\\/:*?"<>|')
_WIN_RESERVED = ({'CON', 'PRN', 'AUX', 'NUL'}
                 | {f'COM{i}' for i in range(1, 10)}
                 | {f'LPT{i}' for i in range(1, 10)})

SHCNE_UPDATEDIR = 0x00001000
SHCNE_UPDATEITEM = 0x00002000
SHCNF_PATHW = 0x0005


# ───────────────────────── 路径与账本 ─────────────────────────

def get_box_root(profile: Optional[str] = None) -> str:
    """盒子根＝用户文件夹下的 `桌面盒子deskbits`（不放桌面上，桌面上看不见它）。"""
    return os.path.join(profile or os.path.expanduser('~'), BOX_ROOT_NAME)


def ensure_root(root: Optional[str] = None) -> str:
    r = root or get_box_root()
    Path(r).mkdir(parents=True, exist_ok=True)
    return r


# ★R322 J5b：搬家那道"落点在盒根里"的闸只看**当前**的 `get_box_root()`，而门禁
# （`测试_ui/gate_sandbox.py:90` 与 r301/302/304/309/320/321/322 那几套盒类闸）会把它
# **换成临时目录**、却不一定同时重定向 `get_app_dir()` ⇒ 判据照样成立，于是把用户
# 真 `data/hidden_box.json` 搬进那只临时目录，再随临时目录一起清掉。本机实录
# （10-12 08:2x 一串门禁跑完）：`data/hidden_box.json.已搬家` 在、盒根那份没有、
# app.log 一行没有（门禁进程不挂 app.log handler ⇒ 全程静默）。
# 口径：**单边重定向＝不搬**；两边都没换（生产链）或两边都换（gate_sandbox 那种整套
# 沙箱，历史八套闸靠它隔离）才允许搬——后者搬的是沙箱自己的副本，伤不到真账本。
# 为什么比**路径字符串**而不是比函数对象：`gate_sandbox` 会把 sys.modules 里所有
# 绑着真 `get_app_dir` 的属性换成桩（`if val is old_fn`），拿函数对象当"原图"会被它
# 一起换掉 ⇒ 整套沙箱反被误判成单边、L9/L10 那种搬家判据就空转（★R322 实测踩过）。
try:
    from utils.app_paths import get_app_dir as _gad0
    _ORIG_APP_DIR_STR = os.path.normcase(str(_gad0()))
except Exception:
    _ORIG_APP_DIR_STR = None
try:
    _ORIG_BOX_ROOT_STR = os.path.normcase(get_box_root())
except Exception:
    _ORIG_BOX_ROOT_STR = None


def _redirects_mismatched() -> bool:
    """盒根与程序目录**只换了一个** ⇒ 另一个还是真的 ⇒ 搬家会伤到真数据。"""
    if _ORIG_APP_DIR_STR is None or _ORIG_BOX_ROOT_STR is None:
        return False
    try:
        from utils.app_paths import get_app_dir as _now_gad
        app_swapped = os.path.normcase(str(_now_gad())) != _ORIG_APP_DIR_STR
        box_swapped = os.path.normcase(get_box_root()) != _ORIG_BOX_ROOT_STR
    except Exception:
        return False
    return app_swapped != box_swapped


def migrate_into_box_root(name: str, new_path: str, app_dir=None) -> None:
    """★R322 J5：把一本账从 `<程序目录>/data/` 搬进盒根 —— 一次性、留底、不覆盖。

    为什么要搬：打包模式 `get_app_dir()`＝**exe 所在目录**（utils/app_paths.py:63-73），
    而盒文件夹在用户家目录跨版本活着 ⇒ 换一个新版本目录＝两本账全空，旧盒里的图标
    全变成"盘上有、账本没登记"，散回链一枚都认不出来（win11 R038/R039 实案：
    `收纳·程序` 5 枚 + `收纳·小朋友` 2 枚从此永远走属性法，用户读成"盒子功能不起作用"）。

    两道闸：①给了 `app_dir`（历轮八套闸门靠它隔离）⇒ 直接返回；②传进来的落点**不在盒根里**
    （桩具把 `manifest_path` 换掉了）⇒ 也返回。少一道就是门禁往用户真家目录写账本。
    ★R322 J5b 再加第三道：**盒根与程序目录只换了一个**（门禁钉 `get_box_root` 却没钉
    `get_app_dir`）⇒ 也返回——那种组合下前两闸都成立，却会把用户真 `data/` 里那本账
    搬进临时目录一起销毁（两边都没换的生产链、和两边都换的整套沙箱照旧允许搬）。
    """
    if app_dir is not None:
        return
    if _redirects_mismatched():
        return
    if os.path.normcase(os.path.dirname(os.path.abspath(str(new_path)))) != \
            os.path.normcase(os.path.abspath(get_box_root())):
        return
    try:
        from utils.app_paths import get_app_dir
        legacy = Path(str(get_app_dir())) / 'data' / name
    except Exception:
        return
    new = Path(str(new_path))
    tmp = Path(str(new) + '.tmp')
    if new.exists() or not legacy.is_file():
        return
    try:
        new.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(legacy.read_bytes())
        os.replace(str(tmp), str(new))
        os.replace(str(legacy), str(legacy) + MOVED_ASIDE_SUFFIX)
        logger.info('[盒子] 账本 %s 已搬进盒根（旧那份改名 %s 留底，一枚没删）',
                    name, legacy.name + MOVED_ASIDE_SUFFIX)
    except OSError as e:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        logger.warning('[盒子] 账本 %s 搬家没成功（%s）——继续按旧位置读', name, e)


def manifest_path(app_dir=None) -> str:
    """成员账本落点。★R322 J5：**不给 `app_dir`（生产链）⇒ 落在盒根里跟着盒子走**；
    给了 `app_dir`（门禁沙箱）⇒ 仍是 `<app_dir>/data/`，历轮隔离口径一个字不变。"""
    if app_dir is not None:
        return str(Path(str(app_dir)) / 'data' / MANIFEST_NAME)
    return str(Path(get_box_root()) / MANIFEST_NAME)


def _empty_manifest() -> dict:
    return {'version': MANIFEST_VERSION, 'boxes': {}}


def load_manifest(app_dir=None) -> tuple:
    """读盒子账本 → (data, failed)。

    failed=True 表示"这份盘上的文件我们读不动但还在"，调用方**必须**拒绝写盘
    （★R231 口径：靠上游传标记的防护等于没防护，判据要能独立成立）。
    解析不动（JSON 坏）按 ★R231b 的"先留底再放行"处理：原件另存 corrupt_ 后
    按空表继续，用户不至于被锁死。"""
    p = manifest_path(app_dir)
    migrate_into_box_root(MANIFEST_NAME, p, app_dir)
    if not os.path.exists(p):
        return _empty_manifest(), False
    try:
        with open(p, encoding='utf-8') as fh:
            data = json.load(fh)
    except OSError as e:
        logger.error(f'盒子账本读不动（拒绝写盘）{p}: {e}')
        return _empty_manifest(), True
    except Exception as e:
        from utils import safeio
        ok, aside = safeio.set_aside_corrupt(p)
        if not ok:
            logger.error(f'盒子账本已损坏但留底失败（拒绝写盘）{p}: {aside}')
            return _empty_manifest(), True
        logger.warning(f'盒子账本损坏，已另存留底后按空表继续: {aside}')
        return _empty_manifest(), False
    if not isinstance(data, dict) or not isinstance(data.get('boxes'), dict):
        from utils import safeio
        ok, aside = safeio.set_aside_corrupt(p)
        if not ok:
            return _empty_manifest(), True
        logger.warning(f'盒子账本形状不对，已另存留底后按空表继续: {aside}')
        return _empty_manifest(), False
    data.setdefault('version', MANIFEST_VERSION)
    return data, False


def rebase_imported_manifest(data, root=None, desktop=None,
                             public_desktop=None):
    """换机导入时把盒成员账本里的两条绝对路径**按本机重算**（★R325 F2）。

    → `(data, fixed)`，`fixed` = 被改过的那几枚的**枚数**。

    为什么连 `from` 也要重算（ZCode 20261013_2111 报告 P2）：导入端旧写法只按本机盒根
    重算了 `to`（这一枚现在躺在盒里哪一格），`from`（当年从桌面哪儿搬走的）保留的是
    **旧机器**的绝对路径。散回那一侧走 `dst = rec.get('from') or join(桌面, fn)`，
    只有当那个目录**已经不存在**才退回当前桌面（★R310 特殊情形①）⇒ 旧路径在新机恰好
    还在（同名用户极常见；或桌面被 OneDrive 重定向而旧目录没删）时，图标被搬进一个
    **看不见的文件夹**，用户读回来还是那句"图标丢失"。

    四条保守线：
      · `to` 一律按本机盒根重算（旧口径原样保留）；
      · `from` 只在**目标目录当下真的存在**时才改；拿不到本机桌面就原样留着——
        绝不写空串把来路信息抹掉，那种交给 `move_back` 的 ★R310 兜底；
      · `public` 旗标决定落点（当年从公共桌面搬走的回公共桌面，不落用户桌面，
        ★R305 那条口径）；
      · 形状不对（`boxes`/`items` 不是 dict、单条记录不是 dict）一律跳过，不抛。
    """
    if not isinstance(data, dict):
        return data, 0
    boxes = data.get('boxes')
    if not isinstance(boxes, dict):
        return data, 0
    r = root if root is not None else get_box_root()
    _u = desktop if desktop is not None else user_desktop_dir()
    _p = (public_desktop if public_desktop is not None
          else public_desktop_dir())
    fixed = 0
    for _b, ent in boxes.items():
        if not isinstance(ent, dict):
            continue
        bd = box_dir_path(str(_b), r)
        items = ent.get('items')
        if not isinstance(items, dict):
            continue
        for fn, rec in items.items():
            if not isinstance(rec, dict):
                continue
            _hit = False
            _to = os.path.join(bd, str(fn))
            if os.path.normcase(str(rec.get('to') or '')) != os.path.normcase(_to):
                rec['to'] = _to
                _hit = True
            _dd = _p if rec.get('public') else _u
            if _dd and os.path.isdir(_dd):
                _fr = os.path.join(str(_dd), str(fn))
                if os.path.normcase(str(rec.get('from') or '')) != os.path.normcase(_fr):
                    rec['from'] = _fr
                    _hit = True
            if _hit:
                fixed += 1
    return data, fixed


def box_provenance(box: str, app_dir=None) -> str:
    """这只盒在**成员账本**里留下的是哪一种凭据（★R322 J7）。

    → 'moved'    至少一枚记着 `moved_at`：那是 `move_into_box` 里 `os.rename` 成功之后
                  才写的 ⇒ **我们自己搬进去的**，不是用户手工放的；
    → 'adopted'  只有 `adopted_at`：那是认领链按盘上实名**补记**的（自证，证明不了是谁放的）；
    → 'none'     账本压根没这只盒/一条没记。

    这两个字段盘上早就分开了（★R310 / ★R320 各写各的），但 `grep -rn "moved_at" core gui`
    除写入点**零命中**——记了从来没人读。★R322 起切分类那条链读它：有 `moved_at` 的盒
    才允许自动接管，只有补记的照旧保守（红线不放宽）。"""
    data, failed = load_manifest(app_dir)
    if failed:
        return 'none'
    items = ((data.get('boxes') or {}).get(box) or {}).get('items') or {}
    _mv = _ad = 0
    for rec in items.values():
        if not isinstance(rec, dict):
            continue
        if rec.get('moved_at'):
            _mv += 1
        elif rec.get('adopted_at'):
            _ad += 1
    if _mv:
        return 'moved'
    return 'adopted' if _ad else 'none'


def save_manifest(data: dict, app_dir=None) -> bool:
    from utils import safeio
    _, failed = load_manifest(app_dir)
    if failed:
        logger.error('盒子账本当前读不动，拒绝覆盖写盘（先修好那份再操作）')
        return False
    p = manifest_path(app_dir)
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    if not safeio.atomic_write_json(p, data):
        logger.error(f'盒子账本写盘失败: {p}')
        return False
    return True


# ───────────────────────── 盒子名 ─────────────────────────

def sanitize_box_name(name) -> str:
    """只做无损清洗：去首尾空白/点、剔掉非法字符与控制字符。
    超长**不截断**（交给 validate 拒绝），免得用户起的名字被悄悄改掉。"""
    s = str(name or '').strip().strip('.')
    s = ''.join(ch for ch in s
                if ch not in _ILLEGAL_CHARS and ord(ch) >= 32)
    return s


def validate_box_name(name) -> tuple:
    """→ (ok, 原因)。原因要么是 ''，要么是能直接上屏的中文说法。"""
    s = sanitize_box_name(name)
    if not s:
        return False, '盒子名不能为空'
    if len(s) > MAX_BOX_NAME:
        return False, f'盒子名太长（最多 {MAX_BOX_NAME} 个字）'
    if s != str(name):
        return False, '盒子名里有不能用于文件名的字符'
    if s.startswith(('.', '~')):
        return False, '盒子名不能以 . 或 ~ 开头'
    if s.upper() in _WIN_RESERVED:
        return False, f'{s} 是系统保留名，不能当盒子名'
    return True, ''


def list_boxes(root: Optional[str] = None) -> List[str]:
    """已存在的盒子（按名字排序）。根目录不存在就是"还没有盒子"，不报错、不建目录。"""
    r = root or get_box_root()
    try:
        return sorted(
            fn for fn in os.listdir(r)
            if os.path.isdir(os.path.join(r, fn)) and not fn.startswith(('.', '~')))
    except OSError:
        return []


def box_dir_path(box: str, root: Optional[str] = None) -> str:
    return os.path.join(root or get_box_root(), box)


def create_box(box: str, root: Optional[str] = None, app_dir=None) -> tuple:
    """→ (ok, 盒子目录 或 原因)。账本里同步登记一个空盒子。"""
    ok, why = validate_box_name(box)
    if not ok:
        return False, why
    r = root or get_box_root()
    d = box_dir_path(box, r)
    if os.path.exists(d):
        return False, f'已经有叫「{box}」的盒子'
    try:
        ensure_root(r)
        os.mkdir(d)
    except OSError as e:
        logger.error(f'建盒子失败 {d}: {e}')
        return False, f'建盒子失败：{e}'
    data, failed = load_manifest(app_dir)
    if not failed:
        data['boxes'].setdefault(box, {'items': {}, 'created': int(time.time())})
        if not save_manifest(data, app_dir):
            logger.error('盒子已建好但账本没记下（下次刷新列表可能看不见它）')
    return True, d


def delete_box(box: str, root: Optional[str] = None, app_dir=None) -> tuple:
    """只允许删**空**盒子（里面有东西先让用户散回，绝不连带删文件）。"""
    r = root or get_box_root()
    d = box_dir_path(box, r)
    if not os.path.isdir(d):
        return False, '没有这个盒子'
    try:
        if os.listdir(d):
            return False, '盒子里还有图标，先点「全部散回桌面」再删'
        os.rmdir(d)
    except OSError as e:
        logger.error(f'删盒子失败 {d}: {e}')
        return False, f'删盒子失败：{e}'
    data, failed = load_manifest(app_dir)
    if not failed and box in data['boxes']:
        data['boxes'].pop(box, None)
        save_manifest(data, app_dir)
    return True, d


# ───────────────────────── 桌面侧 ─────────────────────────

def user_desktop_dir() -> Optional[str]:
    """用户桌面目录（不含公共桌面）。"""
    try:
        from core import icon_manager
        dirs = icon_manager._desktop_dirs()
    except Exception as e:
        logger.error(f'解析桌面目录失败: {e}')
        dirs = []
    for d in dirs:
        try:
            if not os.path.isdir(d):
                continue
            if re.sub(r'\\+$', '', d).lower().endswith(r'\public\desktop'):
                continue
            return d
        except Exception:
            continue
    fallback = os.path.expanduser(r'~\Desktop')
    return fallback if os.path.isdir(fallback) else None


def _virtual_names() -> set:
    try:
        from core import icon_manager
        return set(icon_manager.VIRTUAL_DESKTOP_ICONS.keys())
    except Exception:
        return set()


# 资源管理器**隐扩展名**的那两类——判据不是猜的，是注册表实测（HKCR\.lnk→lnkfile、
# HKCR\.url→InternetShortcut 都带 `NeverShowExt`；`.html/.txt/.exe/.zip/.msc/.ini`
# 都没有 ⇒ 一律原样显示）。见 `run_tmp/_r311_nevershowext.txt`。
NEVER_SHOW_EXT = ('.lnk', '.url')


def display_name(file_name: str) -> str:
    """桌面显示名：Explorer 隐扩展名的（`.lnk`/`.url`）只显主名，其余原样。

    ★R311（批67，用户 0.99-R028 两包真机日志）：历轮只剥 `.lnk`，于是 `foo.url`
    在本程序里叫 `foo.url`，而资源管理器的 ListView、分类名单、实时坐标读数
    一律叫 `foo`（本机现读实锤：live 给 `deepseek`/`gitee`，函数给 `deepseek.url`）。
    两个名字空间一分叉就出真故障：盒子档算的是 `want_boxed = 全集 - visible`，
    那一枚永远"不在 visible 里" ⇒ **被自己那一组的分类切走搬进盒子**，之后散回
    按显示名点名又永远找不到它。真机时间线（`tmp/bugflow/20261008_1240/switch_trace.txt`）：
    点「程序」框名单 10 个（含 `Open-Code-Review-Manager`）⇒ `搬走=46` ⇒ 只剩 9 个，
    用户读成"点程序不显示"。

    剥过头和剥不够一样坏：剥错了就把别的名字搞丢，所以清单只认注册表实测那两个，
    而且整枚文件就叫 `.url` 时不许剥成空串。
    """
    low = file_name.lower()
    for _e in NEVER_SHOW_EXT:
        if low.endswith(_e) and len(file_name) > len(_e):
            return file_name[:-len(_e)]
    return file_name


def public_desktop_dir() -> Optional[str]:
    """公共桌面（`C:\\Users\\<...>\\Public\\Desktop`，这台电脑所有用户共用）。

    ★R310（批66，真机 R027 点名 `Qoder CN` 永远搬不走）：本体就在这儿，而历轮收纳
    只扫用户桌面一个目录 ⇒ 公共桌面的条目**从来没进过候选**。它确实该进——但来源
    目录必须单独认得出，因为①散回时要回到公共桌面（回到用户桌面＝把机器级快捷方式
    变成个人文件）②普通用户对它只有 RX（实测 icacls：INTERACTIVE:(OI)(CI)(RX)，
    完全控制只在 Administrators/SYSTEM 上）⇒ 搬不动时理由要写"公共桌面·需要管理员"，
    不许套进"多半正被程序占用"那句错归因。
    """
    try:
        from core import icon_manager
        dirs = icon_manager._desktop_dirs()
    except Exception as e:
        logger.error(f'解析公共桌面失败: {e}')
        dirs = []
    for d in dirs:
        try:
            if os.path.isdir(d) and re.sub(
                    r'\\+$', '', d).lower().endswith(r'\public\desktop'):
                return d
        except Exception:
            continue
    fb = os.path.join(os.environ.get('PUBLIC', r'C:\Users\Public'), 'Desktop')
    return fb if os.path.isdir(fb) else None


def desk_sources(desktop: Optional[str] = None) -> List[str]:
    """这一轮收纳要扫的目录。

    **只有不传 `desktop` 时才带上公共桌面**：门禁/沙箱一律显式传临时目录，那条路
    必须一枚真用户文件都扫不到（★R296 沙箱铁律）；生产侧不传 ⇒ 用户桌面 + 公共桌面。
    """
    if desktop:
        return [desktop] if os.path.isdir(desktop) else []
    d = user_desktop_dir()
    out = [x for x in (d,) if x and os.path.isdir(x)]
    pub = public_desktop_dir()
    if pub and pub.lower() not in [o.lower() for o in out]:
        out.append(pub)
    return out


def _attrs(path: str) -> int:
    try:
        import win32api
        a = win32api.GetFileAttributes(path)
        return -1 if a is None else int(a)
    except Exception:
        return -1


def list_boxable(desktop: Optional[str] = None) -> List[dict]:
    """桌面上**真的可以搬**的条目（★R310：生产口径是两个来源目录——用户桌面 +
    公共桌面；显式传 `desktop` 时只扫那一个，沙箱/门禁才碰不到真公共桌面）。

    排除：系统占位文件、带隐藏位的（本程序藏的或用户自己藏的——两本账不重叠）、
    虚拟系统图标同名的、以及读不到属性的。每条带 `src_dir`，公共桌面那份再带
    `public=True`。
    """
    virt = _virtual_names()
    out = []
    # 「是不是公共桌面」必须**正面认**（等于 public_desktop_dir()），不能反面认
    # （不等于用户桌面）：显式传临时目录的门禁/沙箱、以及桌面被重定向的用户，
    # 反面认会把普通"正被程序占用"误报成"公共桌面·要管理员"，用户白折腾一轮。
    _pubdir = public_desktop_dir()
    for d in desk_sources(desktop):
        try:
            entries = sorted(os.listdir(d), key=lambda s: s.lower())
        except OSError as e:
            logger.error(f'列桌面目录失败 {d}: {e}')
            continue
        _pub = bool(_pubdir) and os.path.normcase(d) == os.path.normcase(_pubdir)
        for fn in entries:
            p = os.path.join(d, fn)
            if fn.lower() in NEVER_BOX:
                continue
            if not (os.path.isfile(p) or os.path.isdir(p)):
                continue
            a = _attrs(p)
            if a == -1:
                continue
            if a & 0x2:                      # FILE_ATTRIBUTE_HIDDEN
                continue
            if a & 0x400:                    # FILE_ATTRIBUTE_REPARSE_POINT（目录链接点/软链）
                continue
            if display_name(fn) in virt:
                continue
            _it = {'name': display_name(fn), 'file': fn, 'path': p,
                   'is_dir': os.path.isdir(p), 'src_dir': d}
            if _pub:
                _it['public'] = True
            out.append(_it)
    return out


def _desk_index(desktop: Optional[str] = None) -> tuple:
    """桌面可收纳条目的两份索引：`(按文件名的 dict, 按显示名的 dict→列表)`。

    显示名撞车（裸文件 `foo` 与快捷方式 `foo.lnk` 在 Explorer 里都显 `foo`）时
    **两条都留着**，由调用方点名拒绝歧义。旧写法是一条 dict 推导，后写的 `.lnk`
    把裸文件**彻底挤掉**（`by_file` 又是从挤掉之后 rebuild 的），于是"按 foo 收纳"
    无声搬错对象（★R307 V2）。文件名不会撞，按文件名永远精确命中。

    ★R310 跨目录同名（用户桌面与公共桌面都有 `same.lnk`）：`by_file` 先到先得，
    `desk_sources` 把用户桌面排在前面 ⇒ 指名按文件名搬时搬的是**个人那份**，
    机器级那份不碰（保守方向，A11 钉住）。
    """
    items = list_boxable(desktop)
    by_file: Dict[str, dict] = {}
    for x in items:
        # 先到先得：`desk_sources` 把用户桌面排在公共桌面前面 ⇒ 跨目录同名时
        # 索引里留的是**个人那份**（A11 钉住，绝不无声改机器级状态）。
        by_file.setdefault(x['file'], x)
    by_disp: Dict[str, List[dict]] = {}
    _seen = set()
    for x in items:
        # ★R310：同名**不同目录**（用户桌面与公共桌面各一枚 `same.lnk`）不算歧义——
        # Explorer 的合并视图本来就只显个人那枚，所以这里也只留首位；
        # 真歧义仍是 ★R307 那种**不同文件名**共用一个显示名（裸 `foo` 与 `foo.lnk`）。
        _key = (x['name'], x['file'])
        if _key in _seen:
            continue
        _seen.add(_key)
        by_disp.setdefault(x['name'], []).append(x)
    return by_file, by_disp


def _volume_key(path: str) -> Optional[str]:
    """卷 GUID（拿不到退回盘符）。同卷改名才是"瞬时且零复制"的那条路。"""
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    if not drive:
        return None
    try:
        buf = ctypes.create_unicode_buffer(261)
        if ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(
                drive + '\\', buf, 260):
            return buf.value.upper()
    except Exception:
        pass
    return drive.upper() + '#'


def _yield_name(dst: str) -> Optional[str]:
    """给桌面上那一枚"让路"的名字：原名后面加日期时间（用户 66 批第 1 条点名
    "如果不一样，把源目录的文件重命名即可，比如后面加日期"）。

    只在**同一目录内改名**（`os.rename` 同目录是瞬时元数据操作，不复制数据），
    名字本身再撞就追加 `~2 ~3 …`，最多试 20 个；拿不到空位返回 None ⇒ 调用方
    照旧拒绝，一份文件都不动。
    """
    d, fn = os.path.split(dst)
    stem, ext = os.path.splitext(fn)
    stamp = time.strftime('%Y%m%d_%H%M%S')
    for i in range(20):
        cand = os.path.join(d, '%s_%s%s' % (stem, stamp, ext)) if not i else \
            os.path.join(d, '%s_%s~%d%s' % (stem, stamp, i + 1, ext))
        if not os.path.exists(cand):
            return cand
    return None


def _identical_file(a: str, b: str) -> bool:
    """两份是不是**同一枚文件的两个副本**——散回时要不要覆盖，全看这一条。

    用户 66 批口径："如果一模一样，并且日期哪些都一样，直接覆盖。"落地成三条必须
    同时成立的判据：① 大小 ② 内容 sha256 ③ 修改时间（纳秒，比"日期"更严）。
    三条是为"覆盖 = 删掉桌面上那一枚"服务的：任何一条读不出来、任一条对不上、
    或同名那枚是目录（内容判据根本不存在）⇒ 一律 False，宁可不搬，绝不赌。
    """
    try:
        sa, sb = os.stat(a), os.stat(b)
    except OSError:
        return False
    import stat as _stat
    if not (_stat.S_ISREG(sa.st_mode) and _stat.S_ISREG(sb.st_mode)):
        return False
    if sa.st_size != sb.st_size or sa.st_mtime_ns != sb.st_mtime_ns:
        return False
    try:
        ha, hb = hashlib.sha256(), hashlib.sha256()
        for paths, hs in ((a, ha), (b, hb)):
            with open(paths, 'rb') as fh:
                for chunk in iter(lambda: fh.read(1 << 16), b''):
                    hs.update(chunk)
        return ha.hexdigest() == hb.hexdigest()
    except OSError as e:
        logger.warning(f'散回前比对两份同名文件失败（按"不相同"处理，不覆盖）: {e}')
        return False


# 一次比对的条目上限：超过就不比了（用户桌面上一个文件夹里塞几万个文件是常态，
# 为一枚"要不要收进重复盒"的判断把界面卡住不值当）⇒ 按"判不出"处理，交用户点头。
_SAME_CONTENT_CAP = 4000


def same_content(a: str, b: str) -> tuple:
    """两枚同名的东西**内容是不是同一份** → `(是不是, 一句为什么)`。

    与上面 `_identical_file` 的分工要说清：那一把是"**能不能覆盖**"的尺子（大小 +
    sha256 + 纳秒修改时间三条全齐才敢说一样，因为它服务于"删掉桌面上那一枚"）；
    这一把是"**是不是重复**"的尺子，服务于 ★R324 N3——盒里那枚旧货要不要收进
    「收纳·重复的」，收进去一枚不删、随时能按清单搬回来，所以只看内容、不看时间戳
    （文件夹被用过一次 mtime 就变了，拿时间戳比会把每一份都判成"不一样"）。

    · 两边都是文件：大小 → sha256；
    · 两边都是文件夹：递归比"相对路径 → 大小"这张表（不逐个算哈希：桌面上一个
      程序文件夹动辄上千枚，比表已经足够认出"是不是同一份"）；
    · 一边是文件、一边是文件夹，或任一边读不到 ⇒ 判不出，一句说清是哪一种。
    """
    da, db = os.path.isdir(a), os.path.isdir(b)
    if da != db:
        return False, (u'一个是文件夹、另一个不是'
                       if (da or db) else u'两边都读不到')
    if not da and not db:
        if not (os.path.isfile(a) and os.path.isfile(b)):
            return False, u'两边都读不到'
        try:
            if os.path.getsize(a) != os.path.getsize(b):
                return False, u'大小不一样'
        except OSError:
            return False, u'大小读不出来'
        return (True, '') if _identical_bytes(a, b) else (False, u'内容不一样')

    def _sig(root: str) -> Optional[dict]:
        sig = {}
        for dp, dns, fns in os.walk(root):
            dns.sort()
            for fn in sorted(fns):
                p = os.path.join(dp, fn)
                rel = os.path.relpath(p, root)
                try:
                    sig[rel] = os.path.getsize(p)
                except OSError:
                    return None
                if len(sig) > _SAME_CONTENT_CAP:
                    return None
        return sig

    try:
        sa, sb = _sig(a), _sig(b)
    except OSError:
        return False, u'里面的文件列不出来'
    if sa is None or sb is None:
        return False, (u'里面的东西太多（超过 %d 个文件），没敢逐个比'
                       % _SAME_CONTENT_CAP)
    if sa == sb:
        return True, ''
    _only_a = [k for k in sa if k not in sb]
    _only_b = [k for k in sb if k not in sa]
    if _only_a or _only_b:
        return False, u'两边的文件清单不一样（%s）' % (
            u'盒里这一份多 %d 个、桌面上那一份多 %d 个'
            % (len(_only_a), len(_only_b)))
    return False, u'两边文件一样多，但有几个大小不一样'


def _identical_bytes(a: str, b: str) -> bool:
    """只比字节，不看时间戳（`same_content` 用；覆盖那把尺子仍是 `_identical_file`）。"""
    try:
        ha, hb = hashlib.sha256(), hashlib.sha256()
        for p, h in ((a, ha), (b, hb)):
            with open(p, 'rb') as fh:
                for chunk in iter(lambda: fh.read(1 << 16), b''):
                    h.update(chunk)
        return ha.hexdigest() == hb.hexdigest()
    except OSError:
        return False


# ★R322 J2：逐枚留痕的封顶。一次切分类可能一次挡回十几枚（真机 R039：盒里 7 枚全是
# 文件夹），每一枚都打进 app.log 会把别的行挤掉；封顶 10 行，剩下的数并进一句
# 「另有 N 枚同类」——**一枚都不许丢**，只是不再逐条占行。
PER_ITEM_LOG_CAP = 10


def _desk_side_kind(src: str, dst: str) -> str:
    """桌面上那一枚同名的到底是**什么东西**——散回搬不回的归因（★R322 J2）。

    用户 77 批点名：「程序是不是应该对盒子里面的现有的东西有个记录啊，要不日志没法看，
    没法调试」。旧那句只写「桌面上已经有同名的」，是文件夹、内容不一样、还是一模一样，
    日志里分不出来，只能靠人猜。"""
    if os.path.isdir(dst):
        return u'桌面上同名那枚是个文件夹'
    if _identical_file(src, dst):
        return u'桌面上同名那枚与盒里一模一样'
    return u'桌面上同名那枚内容不一样'


def _notify(paths: Sequence[str], dirs: Sequence[str] = ()):
    """批量 shell 通知一律后台线程（主线程连发会原生 abort）。"""

    paths = [str(p) for p in paths]
    dirs = [str(x) for x in dirs]
    if not paths and not dirs:
        return

    def work():
        for p in paths:
            try:
                ctypes.windll.shell32.SHChangeNotify(
                    SHCNE_UPDATEITEM, SHCNF_PATHW, ctypes.c_wchar_p(p), 0)
            except Exception:
                pass
        for d in dirs:
            try:
                ctypes.windll.shell32.SHChangeNotify(
                    SHCNE_UPDATEDIR, SHCNF_PATHW, ctypes.c_wchar_p(d), 0)
            except Exception:
                pass

    threading.Thread(target=work, daemon=True, name='BoxShellNotify').start()


# ───────────────────────── 搬进盒子 ─────────────────────────

def move_into_box(box: str, names: Sequence[str], *,
                  desktop: Optional[str] = None, root: Optional[str] = None,
                  app_dir=None,
                  positions: Optional[Dict[str, Sequence]] = None,
                  dry_run: bool = False) -> dict:
    """把桌面条目搬进盒子。→ {'moved': [...], 'refused': [{'name','reason'}]}

    只判据 + os.rename，不动任何属性位、不改任何名字。dry_run 走完同一条判据链
    但不发任何写调用（取证用，★R249 口径）。"""
    res = {'box': box, 'moved': [], 'refused': [], 'dry_run': dry_run}
    ok, why = validate_box_name(box)
    if not ok:
        res['refused'] = [{'name': n, 'reason': why} for n in names]
        return res
    d = desktop or user_desktop_dir()
    if not d or not os.path.isdir(d):
        res['refused'] = [{'name': n, 'reason': '找不到桌面目录'} for n in names]
        return res
    r = root or get_box_root()
    bd = box_dir_path(box, r)
    # ★R319 K10：这道拒写闸必须排在 `create_box` **之前**。旧写法先建盒再读账本，
    # 于是"账本读不动"的每一发都在盒根里留下一枚**空盒子**（按分类数堆积），
    # 然后才拒搬——用户看到的"桌面盒子文件夹越来越多、里面全是空的"就是这个。
    data, failed = load_manifest(app_dir)
    if failed:
        res['refused'] = [{'name': n, 'reason': '盒子账本读不动，先别搬'}
                          for n in names]
        return res
    if not dry_run and not os.path.isdir(bd):
        ok, why = create_box(box, r, app_dir)
        if not ok:
            res['refused'] = [{'name': n, 'reason': why} for n in names]
            return res
        bd = box_dir_path(box, r)
    dst_vol = _volume_key(bd if os.path.isdir(bd) else r)
    _vol_cache = {}
    # ★R310：候选来自两个目录（用户桌面 + 公共桌面），索引必须按同一套来源建，
    # 传 `d` 会把公共桌面那枚筛掉（真机 `Qoder CN` 的病根就在这两行上）。
    by_file, by_disp = _desk_index(desktop)
    ledger = data['boxes'].get(box, {}).get('items', {})
    _nlog = 0

    for raw in names:
        n = str(raw)
        item = by_file.get(n)
        if item is None:
            cands = by_disp.get(n) or []
            if len(cands) > 1:
                res['refused'].append({
                    'name': n,
                    'reason': '桌面上有两个都叫「%s」的（一个是快捷方式、一个是同名文件），'
                              '先改掉一个名字再收纳' % n})
                continue
            item = cands[0] if cands else None
        if item is None:
            res['refused'].append({'name': n,
                                   'reason': '桌面上没有这个图标（或它是隐藏/系统图标）'})
            continue
        src, fn = item['path'], item['file']
        dst = os.path.join(bd, fn)
        if os.path.exists(dst):
            res['refused'].append({'name': n, 'reason': '盒子里已经有同名的了'})
            continue
        # ★R310：来源可能是两个目录里的任一个，同盘判据要按**这一枚自己的**来源算
        # （旧写法拿用户桌面的卷标去比所有条目，公共桌面/重定向桌面一进来就是错账）。
        _sd = item.get('src_dir') or d
        if _sd not in _vol_cache:
            _vol_cache[_sd] = _volume_key(_sd)
        svol = _vol_cache[_sd]
        if svol and dst_vol and svol != dst_vol:
            res['refused'].append({'name': n, 'reason': '桌面和盒子不在同一个磁盘，本版本不搬'})
            continue
        pos = None
        if positions:
            pv = positions.get(item['name']) or positions.get(fn)
            if pv:
                pos = [int(pv[0]), int(pv[1])]
        _public = bool(item.get('public'))
        if dry_run:
            res['moved'].append({'name': item['name'], 'file': fn,
                                 'from': src, 'to': dst, 'pos': pos,
                                 'public': _public})
            continue
        try:
            os.rename(src, dst)
        except FileNotFoundError:
            res['refused'].append({'name': n, 'reason': '桌面上已经找不到它了'})
            continue
        except FileExistsError:
            res['refused'].append({'name': n, 'reason': '盒子里已经有同名的了'})
            continue
        except PermissionError:
            # ★R310（实测 icacls：公共桌面给 INTERACTIVE 只有 RX）：这一枚来自公共桌面
            # 时不是"被占用"，是没权限，归因写错用户会去关程序、重启资源管理器，白折腾
            # 一轮。**其余情况反过来**——用户自己桌面上的 PermissionError 绝大多数是
            # 文件正被程序占用（★R310 扫 bug：把"需要管理员"写在前面等于让所有人去提权）。
            res['refused'].append({
                'name': n,
                'reason': ('它在公共桌面（这台电脑所有用户共用），当前权限搬不动——'
                           '以管理员身份运行本程序再收它'
                           if _public else
                           '没有权限搬它（多半正被程序占用，关掉占用它的程序再收；'
                           '少数情况才需要管理员）')})
            continue
        except OSError as e:
            res['refused'].append({'name': n, 'reason': f'搬不动（多半正被程序占用）：{e}'})
            continue
        ledger[fn] = {'name': item['name'],
                      'from': src, 'to': dst, 'pos': pos,
                      'moved_at': int(time.time())}
        if _public:
            # 散回靠 `from` 就能回公共桌面；这一枚只是给界面/事后排查留个凭据
            ledger[fn]['public'] = True
        res['moved'].append({'name': item['name'], 'file': fn,
                             'from': src, 'to': dst, 'pos': pos,
                             'public': _public})
        # ★R322 J7（用户点名「搬入搬出应该是有记录的」）：搬进盒这一口逐枚留痕。
        # `moved_at` 就是在这一行之后才写的 ⇒ 这条日志和那份凭据是**同一发**，
        # 事后对着 app.log 就能说清"哪一枚、什么时候、进了哪只盒"。
        if _nlog < PER_ITEM_LOG_CAP:
            logger.info('[盒子] 收纳「%s」→ 盒「%s」（文件名 %s，原位置 %s）',
                        item['name'], box, fn, src)
        _nlog += 1

    if res['moved'] and _nlog > PER_ITEM_LOG_CAP:
        logger.info('[盒子] 收纳「%s」：另有 %d 枚同类（逐条日志封顶 %d 行，'
                    '全量看返回的 moved 清单）', box,
                    _nlog - PER_ITEM_LOG_CAP, PER_ITEM_LOG_CAP)

    if dry_run or not res['moved']:
        return res

    # 来源目录可能不止用户桌面一枚：公共桌面也要刷，否则那半边视图挂着已搬走的图标
    _src_dirs = [d, bd]
    for m in res['moved']:
        _from_dir = os.path.dirname(m['from'])
        if _from_dir and _from_dir not in _src_dirs:
            _src_dirs.append(_from_dir)

    data['boxes'].setdefault(box, {'items': {}, 'created': int(time.time())})
    data['boxes'][box]['items'] = ledger
    if not save_manifest(data, app_dir):
        # 账本写不进去 = 这些文件从此没人认得路。立刻原路退回去，别留孤儿。
        logger.error('盒子账本写入失败，正在退回刚搬进去的图标')
        back = []
        for rec in list(res['moved']):
            try:
                if not os.path.exists(rec['to']):
                    continue
                if os.path.exists(rec['from']):
                    back.append(rec)
                    continue
                os.rename(rec['to'], rec['from'])
            except Exception as e:
                logger.error(f"退回失败 {rec['to']}: {e}")
                back.append(rec)
        if back:
            res['refused'].extend(
                {'name': b['name'], 'reason': '账本写不进去，已尽量退回桌面'}
                for b in back)
        # 两头都要刷：退回桌面的那几枚（from）、留在盒里那几枚（to）
        _notify([m['to'] for m in res['moved']] + [m['from'] for m in res['moved']],
                _src_dirs)
        # 账本没落盘 ⇒ 一枚都不算"已收纳"（退回去的更不算）。界面若按 moved 计数
        # 报"已搬进盒子 N 枚"，就是对用户撒了第二回谎。
        res['moved'] = []
        res['save_ok'] = False
        return res

    _notify([m['to'] for m in res['moved']] + [m['from'] for m in res['moved']],
            _src_dirs)
    res['save_ok'] = True
    return res


# ───────────────────────── 散回桌面 ─────────────────────────

def move_back(box: str, *, root: Optional[str] = None,
              desktop: Optional[str] = None, names: Optional[Sequence[str]] = None,
              app_dir=None, on_conflict: str = 'resolve',
              dry_run: bool = False, apply_layout=None) -> dict:
    """按账本原路把盒子里的图标搬回桌面，并按搬出时记的坐标摆回。

    → {'back': [...], 'blocked': [{'name','reason'}], 'gone': [...], 'over': [...],
       'renamed': [...], 'conflicts': [...], 'kept_in_box': [...]}
    桌面上已有同名时怎么走，看 `on_conflict`：
      · `'resolve'`（默认，**只给用户点名一只盒的那几条路径**用，★R310 口径原样）：
        ① 三项判据（大小/内容/修改时间）**全同** ⇒ 判成同一枚的两个副本，删掉桌面那份
        把盒里的落下，进 `over` 点名（用户 08:40 第 1 条前半）；
        ② 对不上 ⇒ 给桌面那份**改个带日期的名让路**（同一目录内改名，不复制、不覆盖，
        两份都留下），进 `renamed` 点名（同条后半："把源目录的文件重命名即可，比如后面加日期"）；
        ③ 桌面那份是**目录**、或名字让不出空位 ⇒ 拒绝，两份都不动。
      · `'keep'`（★R321，批量/自动路径专用：启动恢复「全部」、切档、退家长模式、
        点「显示全部」）：**桌面上那一枚一个字节、一个名字都不动**——连"完全相同"那一支
        也不覆盖。这一枚留在盒里，进 `conflicts` + `kept_in_box` 点名并出声。
        ★R323 L7（用户定案）：这一档**不算失败**、不进 `blocked`——「撞名的就不要搬回
        桌面了，放进盒子里面」。从前它记成 blocked ⇒ 界面念「搬不回=7」，用户读成坏了 7 枚。
        根因是 win11 R038 那起 P0：自动链连开 40 发改名 ⇒ 桌面凭空多 40 个"重复图标"。
        真要搬，由用户在资源页的盒子面板点对那只盒（`resolve` 那条路）。
    来源是公共桌面的（账本 `public`/`from` 认）回到公共桌面，不落到用户桌面。"""
    keep = (str(on_conflict or 'resolve') == 'keep')
    res = {'box': box, 'back': [], 'blocked': [], 'gone': [], 'over': [],
           'renamed': [], 'conflicts': [], 'kept_in_box': [], 'dry_run': dry_run}
    if is_dup_box(box):
        # ★R323 L8（用户「我到时候去删掉就行」）：`收纳·重复的` 是**只进不出**的那一只。
        # 里面的东西本来就是"和桌面上同名同路径的一模一样"的重复快捷方式，搬回去
        # 等于把用户报的那堆重复图标再造一遍 ⇒ 无论谁点名（面板「全部散回桌面」、
        # 「显示全部」、认领链）都在这里一口回绝，清理由他在文件夹里自己动手。
        res['reason'] = (u'「%s」只进不出：这些是和桌面上同名同路径的重复快捷方式，'
                         u'程序不会把它们搬回桌面——确认不要了请打开那只文件夹自己删'
                         % DUP_BOX_NAME)
        res['blocked'] = [{'name': '(整盒)', 'reason': res['reason']}]
        logger.info('[盒子] 散回「%s」：这一只只进不出 ⇒ 一枚没动（要清理请在文件夹里删）',
                    DUP_BOX_NAME)
        return res
    r = root or get_box_root()
    d = desktop or user_desktop_dir()
    data, failed = load_manifest(app_dir)
    if failed:
        res['blocked'] = [{'name': '(整盒)', 'reason': '盒子账本读不动，先别散'}]
        return res
    items = dict(data['boxes'].get(box, {}).get('items', {}))
    if not items:
        # ★R320 S5b（独立验收 D3）：这一支以前**裸退**——`release_all_boxes` 那侧有
        # 补记、有 `stray`，而面板「📤 全部散回桌面」是裸调这一条的，于是盒里明明躺着
        # 3 枚图标，用户点完只看到一行「回了 0 枚，0 枚搬不回」，连个理由都没有。
        # 补记不归这里做（自动搬会连累桌面上同名的那枚改名，★R305 A8），但**必须出声**。
        _on = []
        try:
            _on = [n for n in sorted(os.listdir(box_dir_path(box, r)))
                   if str(n).lower() not in NEVER_BOX]
        except OSError as e:
            logger.warning('[盒子] 散回「%s」：成员账本没记东西，盒目录也没读动: %s',
                           box, e)
        res['reason'] = (
            (u'盒里有 %d 枚图标成员账本没记到（多半是换了程序目录，或那份清单被另存过）'
             u'——这一条一枚没动；要全搬请在资源页的盒子面板点「搬回未认领的」' % len(_on))
            if _on else u'这只盒是空的，没有要搬回来的东西')
        return res
    want = set(names) if names else None

    if not d or not os.path.isdir(d):
        res['blocked'] = [{'name': n, 'reason': '找不到桌面目录'}
                          for n in items]
        return res
    src_vol = _volume_key(box_dir_path(box, r))
    _vol_cache: Dict[str, Optional[str]] = {}

    removed = []
    _nlog = 0
    for fn, rec in items.items():
        # 三把尺子：文件名 / 账本里的显示名 / 从文件名现剥的显示名。第三把是给 ★R307
        # 之前生成的**存量账本**用的——那时只记文件名，而调用方（切分类、排行榜）传的是
        # 显示名，桌面图标又绝大多数是 .lnk ⇒ 一枚都散不回，且只在"组间互切"这条链上发作
        # （"显示全部"不传 names，走 want=None 全量分支，所以真机自测看不出来）。
        if (want is not None and fn not in want
                and rec.get('name') not in want
                and display_name(fn) not in want):
            continue
        src = rec.get('to') or os.path.join(box_dir_path(box, r), fn)
        dst = rec.get('from') or os.path.join(d, fn)
        _public = bool(rec.get('public'))
        if not os.path.exists(src):
            res['gone'].append({'file': fn, 'reason': '盒子里已经没有这个文件'})
            removed.append(fn)
            continue
        _over = False
        _ren = None                        # 让路：桌面那份要改成的新名字（None=不用让路）
        _dd = os.path.dirname(dst)
        if not os.path.isdir(_dd):
            # ★R310 特殊情形①（用户 08:40 第 3 条"特殊情况要得到妥善的处理"）：
            #   账本记的那条来路**目录已经不在了**（桌面被重定向、公共桌面被改名/移走）
            #   ⇒ 旧写法让 `os.rename` 抛 FileNotFoundError，那一枚永远卡在盒里、
            #   用户点多少次散回都是"搬不回"。这一把起退回**当前桌面**落下并点名；
            #   公共桌面身份同时作废（回来的地方是个个人桌面，账本 `public` 跟着清）。
            if d and os.path.isdir(d):
                logger.info('[盒子] 散回「%s」：原目录 %s 已经不在了 ⇒ 退回当前桌面 %s',
                            display_name(fn), _dd, d)
                dst = os.path.join(d, fn)
                _dd = d
                _public = False
        if os.path.exists(dst):
            if keep:
                # ★R321 J1：自动/批量路径撞上桌面上同名那一枚 ⇒ **桌面上那枚不碰**
                # （不覆盖、不改名），这一枚留在盒里并点名出声。
                # ★R322 J2：光喊"有同名的"不够——用户 77 批「日志没法看，没法调试」。
                # 这一枚究竟是被**文件夹**、被**内容不一样的同名文件**、还是被**一模一样的
                # 副本**挡住的，必须逐枚写在日志里（三种的下一步动作完全不同）。
                _kind = _desk_side_kind(src, dst)
                _cf = {'file': fn, 'name': display_name(fn), 'at': dst,
                       'reason': '%s ⇒ 这一枚留在盒里，桌面上那枚的名字和内容都没动' % _kind,
                       'kept_in_box': True}
                res['conflicts'].append(_cf)
                # ★R323 L7（用户定案：「撞名的，就不要搬回桌面了，就放进盒子里面，
                # 一样的快捷键，指向同一个程序，放在桌面没有意义」）：这一格**不是失败**，
                # 从前它进 `blocked` ⇒ 界面念成「搬不回=7」，用户一直以为坏了 7 枚。
                # 现在单列一档 `kept_in_box`；桌面上那一枚消失之后下一次散回自然就走得通，
                # 不必另外销标（这一档每次散回现算，不写进账本，不留过期状态）。
                res['kept_in_box'].append(_cf)
                if _nlog < PER_ITEM_LOG_CAP:
                    logger.info('[盒子] 散回「%s」：%s ⇒ 这一枚留在盒里'
                                '（桌面上那一枚的名字和内容一个字节都没动）',
                                display_name(fn), _kind)
                _nlog += 1
                continue
            # ★R310（用户 08:40 第 1 条，前后两半是两条路）：
            #   前半"如果一模一样，并且日期哪些都一样，直接覆盖"⇒ 三条判据
            #   （大小/sha256/mtime 纳秒）全中才算同一枚，删桌面那份落下盒里这份；
            #   后半"如果不一样，把源目录的文件重命名即可，比如后面加日期"⇒ 对不上
            #   就**给桌面那一枚改个带日期时间的名让路**（同目录改名，不复制不覆盖），
            #   盒里这枚落在它原来的名字上，两份文件都留下。
            #   两种情况都不许动用户那一枚的**字节**（B3b/B4 钉的就是这个）。
            if _identical_file(src, dst):
                _over = True
            elif os.path.isdir(dst):
                # 目录不让路：内容判据对目录根本不成立，改文件夹名还会牵扯别人的引用
                res['blocked'].append({
                    'file': fn, 'name': display_name(fn),
                    'reason': '桌面上同名那枚是文件夹，不动它的名字（这一枚留在盒里）'})
                # ★R322 J2：这一支以前**只进返回值、日志一个字没有**——真机 R039 那
                # 7 枚全是这一格，界面上只看见"搬不回=7"，app.log 里查不到为什么。
                if _nlog < PER_ITEM_LOG_CAP:
                    logger.info('[盒子] 散回「%s」：桌面上同名那枚是个文件夹 ⇒ '
                                '不动它的名字，这一枚留在盒里', display_name(fn))
                _nlog += 1
                continue
            else:
                _ren = _yield_name(dst)
                if not _ren:
                    res['blocked'].append({
                        'file': fn, 'name': display_name(fn),
                        'reason': '桌面上已经有同名的了，内容又对不上，'
                                  '连让路的名字都让不出来（这一枚留在盒里）'})
                    if _nlog < PER_ITEM_LOG_CAP:
                        logger.info('[盒子] 散回「%s」：桌面上同名那枚内容不一样，'
                                    '而让路的名字也让不出来 ⇒ 这一枚留在盒里',
                                    display_name(fn))
                    _nlog += 1
                    continue
        _dd = os.path.dirname(dst)
        if _dd not in _vol_cache:
            _vol_cache[_dd] = _volume_key(_dd)
        dvol = _vol_cache[_dd]
        if src_vol and dvol and src_vol != dvol:
            res['blocked'].append({'file': fn, 'name': display_name(fn),
                                   'reason': '桌面和盒子不在同一个磁盘，本版本不搬'})
            continue
        if dry_run:
            _b = {'file': fn, 'name': display_name(fn),
                  'from': src, 'to': dst, 'pos': rec.get('pos'),
                  'over': _over, 'public': _public}
            if _ren:
                _b['renamed'] = os.path.basename(_ren)
            res['back'].append(_b)
            continue
        if _over:
            try:
                os.remove(dst)
            except OSError as e:
                # 完全相同的副本连删都删不动 ⇒ 这一枚整条不动（盒里那份原地留着），
                # 理由照实写，别写成"桌面上已经有同名的了"那种半句话
                res['blocked'].append({
                    'file': fn, 'name': display_name(fn),
                    'reason': f'桌面上那份与盒里完全相同，但它正被占用删不掉，这一枚留在盒里：{e}'})
                continue
        if _ren:
            # 让路：同目录改名，瞬时且不复制数据。这一步只动**名字**，不动内容；
            # 改不动（占用/权限）就整条拒绝，桌面那一枚的名字与内容都保持原样。
            try:
                os.rename(dst, _ren)
            except OSError as e:
                res['blocked'].append({
                    'file': fn, 'name': display_name(fn),
                    'reason': '桌面上已经有同名的了，改名让它让路也没改成'
                              '（多半正被占用），这一枚留在盒里：%s' % e})
                continue

        def _undo_yield():
            """让路改了名、这一枚却没落下 ⇒ 把桌面那份的名字改回去。"""
            try:
                if os.path.exists(_ren) and not os.path.exists(dst):
                    os.rename(_ren, dst)
                    return True
            except OSError:
                return False
            return False

        try:
            os.rename(src, dst)
        except PermissionError as e:
            # ★R310 特殊情形②：来路是公共桌面（普通用户对它只有读取+运行，实测 icacls
            #   INTERACTIVE:(OI)(CI)(RX)）⇒ 以管理员收进去的、普通权限散不回来。
            #   这句必须跟"多半正被程序占用"分开写，不然用户会去关程序，而真要关的是权限。
            _back = _undo_yield() if _ren else False
            if _ren and not _back:
                res['blocked'].append({
                    'file': fn, 'name': display_name(fn),
                    'reason': '让路改了名，可这一枚没能落下，'
                              '而桌面那份的名字改不回去（它现在叫 %s）：%s'
                              % (os.path.basename(_ren), e)})
                continue
            res['blocked'].append({
                'file': fn, 'name': display_name(fn),
                'reason': (('它原本在公共桌面（这台电脑所有用户共用），当前权限放不回去——'
                            '以管理员身份运行本程序再散，或就让它留在盒里')
                           if _public else
                           '没有权限放回它（多半正被程序占用，关掉占用它的程序再散；'
                           '少数情况才需要管理员）：%s' % e)})
            continue
        except OSError as e:
            _back = _undo_yield() if _ren else False
            if _ren:
                # 让路是**可退的**：落不下就把名字还回去，绝不留"改了名却没东西"的现场
                res['blocked'].append({
                    'file': fn, 'name': display_name(fn),
                    'reason': ('让路改了名，可这一枚没能落下 ⇒ 已把桌面那份名字改回去：'
                               if _back else
                               '让路改了名，可这一枚没能落下，而桌面那份的名字改不回去'
                               '（它现在叫 %s）：' % os.path.basename(_ren)) + str(e)})
                continue
            if _over:
                # 删掉了"完全相同"的那一份、这一枚又没落下 ⇒ 话要说全：文件还在盒里、
                # 账本没清，再点一次散回就行（别让人以为东西丢了）
                _why = ('桌面上那份同名的已按"完全相同"删掉，可这一枚没能落下'
                        '（文件还在盒里，账本没清，可以再散一次）：%s' % e)
            else:
                _why = f'搬不回（多半正被程序占用）：{e}'
            res['blocked'].append({'file': fn, 'name': display_name(fn),
                                   'reason': _why})
            continue
        res['back'].append({'file': fn, 'name': display_name(fn),
                            'from': src, 'to': dst, 'pos': rec.get('pos'),
                            'over': _over, 'public': _public})
        if _over:
            res['over'].append({'file': fn, 'name': display_name(fn),
                                'at': dst})
            logger.info('[盒子] 散回「%s」：桌面上同名那份与盒里字节、修改时间完全相同 '
                        '⇒ 按用户口径直接覆盖（%s）', display_name(fn), dst)
        if _ren:
            res['renamed'].append({'file': fn, 'name': display_name(fn),
                                   'kept': _ren, 'to': dst})
            logger.info('[盒子] 散回「%s」：桌面上同名那份与盒里对不上 ⇒ 把它改名叫 %s '
                        '让路，两份都留下（%s）', display_name(fn),
                        os.path.basename(_ren), dst)
        removed.append(fn)

    if _nlog > PER_ITEM_LOG_CAP:
        # ★R322 J2：封顶之后剩下的数不许丢——这一句把差额报出来，一枚都不少。
        logger.info('[盒子] 散回「%s」：另有 %d 枚同类（逐条日志封顶 %d 行，'
                    '全量见返回的 blocked 清单）', box,
                    _nlog - PER_ITEM_LOG_CAP, PER_ITEM_LOG_CAP)

    if dry_run or not removed:
        return res

    for fn in removed:
        items.pop(fn, None)
    if items:
        data['boxes'].setdefault(box, {'items': {}})['items'] = items
    else:
        data['boxes'].setdefault(box, {'items': {}})['items'] = {}
    if not save_manifest(data, app_dir):
        logger.error('已散回桌面但盒子账本没更新（重启后盒内清单可能多几条，'
                     '散回判据按"文件不在盒里"自动清）')
        res['save_ok'] = False
    else:
        res['save_ok'] = True

    _dst_dirs = [d, box_dir_path(box, r)]
    for b in res['back']:
        _td = os.path.dirname(b['to'])
        if _td and _td not in _dst_dirs:
            _dst_dirs.append(_td)
    _notify([b['to'] for b in res['back']] + [b['from'] for b in res['back']]
            + [x['kept'] for x in res['renamed']],
            _dst_dirs)

    if res['back'] and apply_layout is not None:
        target = {}
        for b in res['back']:
            pv = b.get('pos')
            if pv:
                target[b['name']] = (int(pv[0]), int(pv[1]))
        if target:
            try:
                out = apply_layout(target)
            except Exception as e:
                logger.error(f'散回后摆位失败: {e}')
                out = None
            if isinstance(out, dict):
                # ★R304：把摆位回执原样带出去，界面才敢照实说"回到原位 / 挪到
                #   附近空格 / 暂时没放下"。真机 R023 的病就是这里"发了消息就当摆好"。
                res['place'] = out
                bad = ([u'没等到视图：%s' % n for n in out.get('missing') or []]
                       + [u'原格被占且全屏无空位：%s' % n
                          for n in out.get('no_room') or []]
                       + [u'写了没落位：%s' % n for n in out.get('unverified') or []])
                if bad:
                    logger.warning('散回后有 %d 枚没能摆回记录的位置（文件已在桌面，'
                                   '只有坐标没归位）：%s', len(bad), bad[:6])
            else:
                # 老调用方回 int（apply_positions）：只当"发了几条写"，不说落位。
                res['place'] = {'target': len(target), 'moved': out
                                if isinstance(out, int) else 0}
    return res


def adopt_disk_items(box: str, *, root: Optional[str] = None, app_dir=None,
                     desktop: Optional[str] = None,
                     only: Optional[Sequence[str]] = None,
                     source: str = '') -> List[str]:
    """盘上真有、成员账本里没记的名目 ⇒ 按**盘上实名**补记账本（零文件操作）。

    ★R320（用户 P0「图标丢失」本机实撞孤儿态 B）：`move_back` 只遍历账本 `items`，
    `if not items: return` ⇒ 账本一空白/缺项，盒里的图标就**静默散不回**，连一句
    警告都没有。账本损坏时 ★R231b 那份"另存留底后按空表继续"正是这条死路的入口。
    文件名本身就是成员身份（盒目录是我们建的），显示名从文件名现剥（★R311 口径），
    所以补记不需要猜任何东西。

    `only` 给认领链用：只补"可证明是我们的"那几枚（按文件名或显示名匹配）。
    → 补记到的文件名 list（空 = 账本本来就全，或读不动）。
    """
    r = root or get_box_root()
    bd = box_dir_path(box, r)
    try:
        disk = [fn for fn in os.listdir(bd)
                if str(fn).lower() not in NEVER_BOX]
    except OSError as e:
        logger.warning('[盒子] %s 目录读不动（一枚都不补记）%s: %s', box, bd, e)
        return []
    if only is not None:
        want = {str(x) for x in only}
        disk = [fn for fn in disk
                if fn in want or display_name(fn) in want]
    data, failed = load_manifest(app_dir)
    if failed:
        logger.error('[盒子] 成员账本读不动 ⇒ 不补记（先修好那份）')
        return []
    ent = data['boxes'].setdefault(box, {'items': {}, 'created': int(time.time())})
    items = ent.get('items') or {}
    added = [fn for fn in disk if fn not in items]
    if not added:
        return []
    d = desktop or user_desktop_dir()
    for fn in added:
        items[fn] = {'name': display_name(fn),
                     'from': os.path.join(d, fn) if d else '',
                     'to': os.path.join(bd, fn),
                     'pos': None, 'public': False,
                     'adopted_at': int(time.time())}
    ent['items'] = items
    data['boxes'][box] = ent
    if not save_manifest(data, app_dir):
        logger.error('[盒子] 补记没落盘（%s 少 %d 条）——散回仍会漏这些枚',
                     box, len(added))
        return []
    logger.info('[盒子] %s：盘上有 %d 枚没记进成员账本 ⇒ 按盘上实名补记'
                '（来源=%s）：%s', box, len(added), source or '?', added[:6])
    return added


# ───────────────── 让路重复名的可逆回收（★R321 J4 → ★R323 L8） ─────────────────
#
# 背景（win11 真机 0.99-R038）：自动链照 ★R310 给桌面上同名那枚改了 40 个带日期的名，
# 于是桌面上一夜之间多出 40 对"看着就是重复"的图标。J1 把那扇门关上了，这一节是**退路**：
# 已经造出来的那些名字要能收拾，而收拾必须是**可逆**的——只搬到回收落点，一枚都不删，
# 并留一份 `清单.json` 写清"哪一个从哪儿来、原名是谁"。
#
# ★R323 L8（用户 16:47「你直接做一个类似"收纳-重名"的文件夹，把重名同路径的放在一起，
# 我到时候去删掉就行。功能也要体现在盒子里面」+ 16:54「收纳-重复的，放在一个收纳-重复的
# 文件夹里面」）：回收落点从"每次一把新的 `~让路…` 目录"换成**一只看得见、名字由他定**
# 的盒 `收纳·重复的`。旧落点坏在"看不见"——`~` 前缀让 `list_boxes` 读不到它 ⇒ 盒子面板
# 里没有它、点不开、数不出，用户要清理只能进资源管理器一层层翻（★R322 J4 那次只把
# 名字改短，没解决"看不见"）。
# 代价是"看得见"会带来两条新风险，所以这只盒改成**只进不出**，由三处闸门共同钉住：
#   ① `move_back` 一进门回绝（面板「全部散回桌面」、启动「显示全部」都走这一条）；
#   ② `audit_box_root` 对它的 claimable/unclaimable/unrecorded 三格清零 ⇒ 不计进
#     「没被认领」，那枚按钮永远轮不到它；
#   ③ `claim_stray_boxes` 显式绕开（就算将来②的判据被人改动，认领链仍不动它）。
# 它**不进任何一本账**（`hidden_box.json`/`desktop_box.json` 都不登记）——登记了
# `audit_box_root` 会按"整盒都是我们的"给出 claimable（★R320 S5b 那格判据），
# 退路就变成"自动倒回桌面"了。来路只写在盒里的 `清单.json`。
DUP_BOX_NAME = u'收纳·重复的'
LEGACY_YIELD_PREFIX = '~让路'
YIELD_MANIFEST_NAME = u'清单.json'

# `_yield_name` 造出来的那一发形状：原名_YYYYMMDD_HHMMSS[~n].ext
_YIELD_SHAPE = re.compile(
    r'^(?P<stem>.+)_(?P<date>\d{8})_(?P<time>\d{6})'
    r'(?:~(?P<seq>\d+))?(?P<ext>\.[^.\\/]+)$')

# ShellLink 头（MS-SHLC）里固定不变的那一小段：头长 76 + 这个 CLSID。
# 对不上就说明这不是 .lnk（或读坏了）⇒ 判据一律"认不出来就当不一样"。
_LNK_HEADER_SIZE = 76
_LNK_CLSID = (b'\x01\x14\x02\x00\x00\x00\x00\x00'
              b'\xc0\x00\x00\x00\x00\x00\x00\x46')

# 这一节（回收重复名）所有 app.log 行的共同前缀。★R324：界面上那颗按钮改名
# 「清理重复的图标」，**日志前缀故意不动**——历轮词典那 11 条对照规则、三套既有闸
# 都靠它认行，历史日志还要翻得动（改它=把老行全变成没翻译的中文）。
DUPE_LOG_PREFIX = u'[盒子] 清理重复的让路名：'


def is_dup_box(box) -> bool:
    """这一只盒是不是那一只「收纳·重复的」——**只进不出**的那一只。"""
    return str(box or '') == DUP_BOX_NAME


def dup_box_dir(root: Optional[str] = None) -> str:
    """重名回收盒的目录（不建、不落盘，只给路径）。"""
    return box_dir_path(DUP_BOX_NAME, root or get_box_root())


def yield_leftover_dirs(root: Optional[str] = None) -> List[str]:
    """盒根下历轮那几把 `~让路…` 回收目录（绝对路径，按名字排序）。

    两代名字（`~让路回收_<时刻>` / `~让路_<月日>-<第几把>`）共用 `~让路` 这一截前缀
    ⇒ 一次枚举全抓到。只认**目录**，`list_boxes` 那侧靠 `~` 前缀本来就读不到它们。
    """
    r = root or get_box_root()
    try:
        names = sorted(os.listdir(r))
    except OSError:
        return []
    out = []
    for fn in names:
        if not str(fn).startswith(LEGACY_YIELD_PREFIX):
            continue
        p = os.path.join(r, fn)
        if os.path.isdir(p):
            out.append(p)
    return out


def _dup_slot(dst: str) -> Optional[str]:
    """盒里那一格的空位：名字撞了就追加 `~2 ~3 …`（同 `_yield_name` 的写法）。

    桌面上同名那枚本来就不该出现两次，会撞的是**两个桌面目录各有一枚同名**、
    或历轮回收目录并进来时撞上——撞了绝不覆盖（★R301 第 1 条：一份都不许凭空消失）。
    """
    d, fn = os.path.split(dst)
    stem, ext = os.path.splitext(fn)
    if not os.path.exists(dst):
        return dst
    for i in range(1, 21):
        cand = os.path.join(d, '%s~%d%s' % (stem, i, ext))
        if not os.path.exists(cand):
            return cand
    return None


def _sha256_of(path: str) -> str:
    try:
        h = hashlib.sha256()
        with open(path, 'rb') as fh:
            for chunk in iter(lambda: fh.read(1 << 16), b''):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ''


def _read_cstr(blob: bytes, start: int, stop: int) -> str:
    """LinkInfo 里那串**以 NUL 结尾的 ANSI 路径**（MS-SHLP 2.5：LocalBasePath 恒为
    单字节字符集，跟头里的 IsUnicode 无关）。越界就返回空串，由调用方判"读不出"。"""
    if start < 0 or start >= stop or stop > len(blob):
        return ''
    end = blob.find(b'\x00', start, stop)
    if end < 0:
        return ''
    raw = blob[start:end]
    try:
        return raw.decode('mbcs')
    except (LookupError, UnicodeDecodeError):
        return raw.decode('latin-1', 'replace')


def _read_wstr(blob: bytes, start: int, stop: int) -> str:
    """LinkInfo 里那串**以双 NUL 结尾的 UTF-16LE 路径**（LocalBasePathUnicode）。
    对齐不对（奇数偏移/长度不够）一律返回空串，由调用方退回 ANSI 那一份。"""
    if start < 0 or start + 2 > stop or stop > len(blob):
        return ''
    parts = []
    i = start
    while i + 1 < stop:
        ch = struct.unpack_from('<H', blob, i)[0]
        if ch == 0:
            break
        parts.append(chr(ch))
        i += 2
    return ''.join(parts)


def _lnk_semantics(path: str) -> Optional[tuple]:
    """纯字节读出一枚 .lnk 的**语义**：`(目标, 工作目录, 参数, 图标, 图标序号, 窗口模式)`。

    ★R323 L1（真机恒灰的根因）：旧写法是"抹掉头里 22:52 那 30 字节，尾巴整段照比"。
    那 30 字节确实只含 FileAttributes + 三个 FILETIME + 记录的目标大小，**复制品**比得中；
    但尾巴里有 TrackerData（MS-SHLP 2.2.9）——16 字节 Droid + 8 字节 Droid Birth
    **每建一枚新链就重新生成一次**。让路名那批里"原名被重建过"的那一半（安装器/自更新
    重新落了一份同名 .lnk）于是永远判成"不一样" ⇒ `same` 恒 0 ⇒「清理重复的让路名」
    在用户机器上永远点不亮（R040 真机：桌面上 `HashKit_20261011_152441`、
    `Qoder CN_20261011_152441` 两枚清不掉）。

    现在只比"这枚快捷方式到底是什么"：指向谁、在哪个目录跑、带什么参数、用哪个图标、
    以什么窗口开。头/LinkInfo/StringData 全部按 MS-SHLP 手工走字节——**一发 COM 都不调、
    一个子进程都不拉**（★R280-C 红线：主进程调 WScript.Shell 崩过整个程序）。
    读不出目标一律返回 None ⇒ 调用方按"判不出来，一枚不碰"处理。
    """
    try:
        with open(path, 'rb') as fh:
            blob = fh.read()
    except OSError:
        return None
    n = len(blob)
    if n < _LNK_HEADER_SIZE:
        return None
    if struct.unpack_from('<I', blob, 0)[0] != _LNK_HEADER_SIZE:
        return None
    if blob[4:20] != _LNK_CLSID:
        return None
    flags = struct.unpack_from('<I', blob, 20)[0]
    icon_index = struct.unpack_from('<i', blob, 56)[0]
    show = struct.unpack_from('<I', blob, 60)[0]
    off = _LNK_HEADER_SIZE
    if flags & 0x00000001:                      # HasLinkTargetIDList
        if off + 2 > n:
            return None
        off += 2 + struct.unpack_from('<H', blob, off)[0]
    target = ''
    if flags & 0x00000002:                      # HasLinkInfo
        if off + 20 > n:
            return None
        li_size = struct.unpack_from('<I', blob, off)[0]
        li_hdr = struct.unpack_from('<I', blob, off + 4)[0]
        # MS-SHLP 2.5 的字段次序（★R323：把 0x0C 当 LocalBasePathOffset 读会拿到
        # VolumeID 的头长，真机 Real.lnk 实测解出 `\x16`——量具靠真链才抓得出来）：
        #   0x0C VolumeIDOffset / 0x10 LocalBasePathOffset / 0x14 CommonPathSuffixOffset
        #   0x1C LocalBasePathOffsetUnicode（仅 LinkInfoHeaderSize ≥ 0x24 时存在）
        lbp_off = struct.unpack_from('<I', blob, off + 16)[0]
        if li_size < 0x1C or li_hdr < 0x1C or off + li_size > n:
            return None
        if li_hdr >= 0x24:
            lbu = struct.unpack_from('<I', blob, off + 0x1C)[0]
            if lbu:
                target = _read_wstr(blob, off + lbu, off + li_size)
        if not target:
            target = _read_cstr(blob, off + lbp_off, off + li_size)
        off += li_size
    uni = bool(flags & 0x00000080)              # IsUnicode：只影响 StringData
    step = 2 if uni else 1

    def _s(present_flag: int) -> str:
        nonlocal off
        if not flags & present_flag:
            return ''
        if off + 2 > n:
            return ''
        cnt = struct.unpack_from('<H', blob, off)[0]
        off += 2
        raw = blob[off:off + cnt * step]
        off += cnt * step
        try:
            return (raw.decode('utf-16-le') if uni else raw.decode('mbcs'))
        except (LookupError, UnicodeDecodeError):
            return raw.decode('latin-1', 'replace')

    _s(0x00000004)                              # Name（显示名，不参与判同）
    _s(0x00000008)                              # RelativePath
    work = _s(0x00000010)
    args = _s(0x00000020)
    icon = _s(0x00000040)
    if not target:
        return None

    def _np(p: str) -> str:
        p = (p or '').strip()
        if not p:
            return ''
        try:
            return os.path.normcase(os.path.abspath(p))
        except Exception:
            return os.path.normcase(p)

    return (_np(target), _np(work), ' '.join((args or '').lower().split()),
            ' '.join((icon or '').lower().split()), icon_index, show)


def _dup_why(base_path: str, yield_path: str) -> str:
    """原名那枚 vs 带日期那枚是不是同一个东西：`'bytes'` / `'lnk'` / `''`（''=不碰）。

    两档判同各管一半现场：`'bytes'` 收"整份复制出来的"（头尾全同），`'lnk'` 收
    "同一个程序先后建的"（★R323：按 `_lnk_semantics` 那五件语义比，不比 TrackerData）。
    任一条判不出来 ⇒ `''`，宁可留着不碰，也不冒把用户两枚**不同**快捷方式合并掉的险。
    """
    import stat as _stat
    try:
        sa, sb = os.stat(base_path), os.stat(yield_path)
    except OSError:
        return ''
    if not (_stat.S_ISREG(sa.st_mode) and _stat.S_ISREG(sb.st_mode)):
        return ''
    if sa.st_size == sb.st_size:
        ha = _sha256_of(base_path)
        if ha and ha == _sha256_of(yield_path):
            return 'bytes'
    if base_path.lower().endswith('.lnk') and yield_path.lower().endswith('.lnk'):
        sem_a, sem_b = _lnk_semantics(base_path), _lnk_semantics(yield_path)
        if sem_a is not None and sem_a == sem_b:
            return 'lnk'
    return ''


def _dup_note(base_path: str, yield_path: str) -> str:
    """判不出是不是同一枚时，**差在哪一味**（★R324 D7：只说"不一样"等于没说）。

    只用来写日志/界面措辞，不参与判同（判同是 `_dup_why` 那一条路）。每一味的判据
    都跟 `_dup_why` 同源，所以这句不会跟"为什么不收"打架：
      · 读不到任何一份 ⇒「有一枚读不到」（多半刚被删/正被占用）
      · 任一份不是普通文件 ⇒「其中一枚不是文件」（目录树一类不合并）
      · 大小不同 ⇒「大小不一样」
      · `.lnk` 语义读不出 ⇒「读不出快捷方式指向哪里」
      · `.lnk` 语义不同 ⇒「指向的不是同一个程序」
      · 其余（大小同、哈希不同、非 .lnk）⇒「内容不一样」
    """
    import stat as _stat
    try:
        sa, sb = os.stat(base_path), os.stat(yield_path)
    except OSError:
        return u'有一枚读不到（可能被删了或正被占用）'
    if not (_stat.S_ISREG(sa.st_mode) and _stat.S_ISREG(sb.st_mode)):
        return u'其中一枚不是文件（目录一类的东西不能合并）'
    if sa.st_size != sb.st_size:
        return u'大小不一样'
    if base_path.lower().endswith('.lnk') and yield_path.lower().endswith('.lnk'):
        sem_a, sem_b = _lnk_semantics(base_path), _lnk_semantics(yield_path)
        if sem_a is None or sem_b is None:
            return u'读不出快捷方式指向哪里'
        if sem_a != sem_b:
            return u'指向的不是同一个程序'
    return u'内容不一样'


def yield_dup_candidates(desktops: Optional[Sequence[str]] = None,
                         root: Optional[str] = None) -> dict:
    """扫桌面目录，只挑**我们自己造出来的**那一发"让路名"，按判同分三档；
    另把盒根下历轮那几把 `~让路…` 回收目录里的东西列成第四档 `legacy`（★R323 L8）。

    → {'same': [{name,base,dir,path,why}], 'differ': [...], 'orphan': [...],
       'orphan_dir': [...], 'legacy': [...]}

    两道门槛（宁缺勿滥）：
      ① 名字形状必须是 `_yield_name` 那一种（`原名_YYYYMMDD_HHMMSS[~n].ext`）——
        用户自己的 `照片 3 final.docx`、别的软件造的 `xxx - Copy` 一概不认；
      ② 原名此刻**不在同一个目录** ⇒ 那一枚进 `orphan`（★R324 D1：形状是我们造的、
        原名却已经没了，多半是用户自己把桌面上那枚删了/搬走了，带日期这一枚
        就是我们造的让路副本 ⇒ 收进「收纳·重复的」，一枚不删、退路在清单里）。
        **目录不进这一档**：名字形状像但它是目录 ⇒ 进 `orphan_dir`，永远不碰
        （★R321 L7 那条口径：目录树一类不是我们造的让路名，用户自己的东西）。
    `desktops` 不传才走真机桌面（用户 + 公共）；门禁/沙箱一律显式传临时目录。

    `legacy` 那一档不走这两道门：那些目录**本来就是我们自己建的回收落点**，里面的
    东西当年已经判过一次同。用户点名"放在一起方便清理"（★R323 L8），所以旧那几把
    里的每一枚都算可搬；`base`/`dir` 优先从那一把自己的 `清单.json` 里读回来，
    读不到就按名字形状现剥、来路记成空串（照旧一枚不删，只是少一句来路）。
    """
    out: Dict[str, List[dict]] = {'same': [], 'differ': [], 'orphan': [],
                                  'orphan_dir': [], 'legacy': []}
    for d in desk_sources() if desktops is None else list(desktops):
        if not d or not os.path.isdir(d):
            continue
        try:
            names = sorted(os.listdir(d))
        except OSError as e:
            logger.warning('[盒子] 清理重复的让路名：目录读不动 %s: %s', d, e)
            continue
        for fn in names:
            if str(fn).lower() in NEVER_BOX:
                continue
            m = _YIELD_SHAPE.match(str(fn))
            if not m:
                continue
            base = m.group('stem') + m.group('ext')
            rec = {'name': str(fn), 'base': base, 'dir': d,
                   'path': os.path.join(d, fn)}
            bp = os.path.join(d, base)
            if not os.path.isfile(bp):
                # ★R324 D3：形状一样但它是**目录** ⇒ 单独一档，谁都别想搬它
                # （用户自己的目录树不是我们造的让路名，★R321 L7 同款口径）。
                if os.path.isdir(rec['path']):
                    out['orphan_dir'].append(rec)
                else:
                    # ★R325 F1（ZCode 20261013_2111 报告 P1，本批定级 P1）：形状对得上、
                    # 原名却不在同一目录 ⇒ 我们**拿不出凭据**说这是我们造的让路副本。
                    # 用户自己导出的东西完全命中这个形状（真机实案：他桌面上此刻就躺着
                    # `DeskBits_日志_0.99-R042_20261014_081147.zip`，剥出的原名并不存在），
                    # 旧写法却把它并进自动批 ⇒ 点一次就搬走。这一档从此**默认一枚不搬**，
                    # 只点名；要收由界面那**同一个**第二问问过用户，点【是】才带
                    # `include_orphan=True` 再来一趟。理由句在这一头算好带给界面与日志，
                    # 不留两处口径（`differ` 那档的 `_dup_note` 同款做法）。
                    rec['note'] = u'只有名字带日期时间，拿不出凭据'
                    out['orphan'].append(rec)
                continue
            why = _dup_why(bp, rec['path'])
            if why:
                rec['why'] = why
                out['same'].append(rec)
            else:
                # ★R324 D7：判不出那一档顺手把"差在哪一味"也算好带给界面——
                # 弹窗里念得出「note_20261011_152441.lnk：大小不一样」，比一句
                # "内容对不上"有用得多（同一份判据 `_dup_note`，不留两处口径）。
                rec['note'] = _dup_note(bp, rec['path'])
                out['differ'].append(rec)
    for qd in yield_leftover_dirs(root):
        _old = _read_yield_manifest(qd)
        _by_name = {str(x.get('name')): x for x in (_old.get('items') or [])}
        try:
            names = sorted(os.listdir(qd))
        except OSError as e:
            logger.warning('[盒子] 认旧回收目录读不动 %s: %s', qd, e)
            continue
        for fn in names:
            if str(fn) == YIELD_MANIFEST_NAME or str(fn).lower() in NEVER_BOX:
                continue
            _p = os.path.join(qd, fn)
            if not os.path.isfile(_p):
                # 旧那把里除了文件还掺了目录 ⇒ 整把留在原地不动（这一节只搬**文件**，
                # 不替用户搬他可能自己建的目录树——那已经不是"我们造的让路名"了）。
                continue
            _rec = _by_name.get(str(fn)) or {}
            m = _YIELD_SHAPE.match(str(fn))
            out['legacy'].append({
                'name': str(fn),
                'base': str(_rec.get('base') or
                            ((m.group('stem') + m.group('ext')) if m else '')),
                'dir': str(_rec.get('dir') or ''),
                'path': _p,
                'why': str(_rec.get('why') or 'legacy'),
                'from_dir': qd})
    return out


def _read_yield_manifest(qd: str) -> dict:
    """读一份回收清单（读不动/没有 ⇒ 空表，绝不抛）。"""
    try:
        with open(os.path.join(qd, YIELD_MANIFEST_NAME), encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_yield_manifest(qd: str, taken: Sequence[dict]) -> None:
    """把 `清单.json` 写进回收盒——那是"一枚没删、随时按原名搬回去"的唯一凭据。

    ★R323 L8：落点从"每次一把新的 `~让路…` 目录"换成**同一只可见的盒**
    （`收纳·重复的`）⇒ 这份清单从"一次一份"变成**跨轮累加**：旧条目按
    (名字, 来路目录) 原样保留，新条目追加，绝不把上一轮的退路覆盖掉。
    单独一枚函数只为让门禁能**点名让这一步失败**（真造 OSError 在沙箱里造不出：
    目录刚建好、盘也够），从而钉住"清单没写出 ⇒ 界面不许再提 清单.json"。
    """
    old = _read_yield_manifest(qd)
    items = []
    seen = set()
    for x in (list(old.get('items') or []) + list(taken)):
        _k = (str(x.get('name') or ''), str(x.get('dir') or ''))
        if _k in seen:
            continue
        seen.add(_k)
        items.append({'name': x.get('name'), 'base': x.get('base'),
                      'dir': x.get('dir'), 'why': x.get('why') or ''})
    _now = int(time.time())
    with open(os.path.join(qd, YIELD_MANIFEST_NAME), 'w',
              encoding='utf-8') as fh:
        fh.write(json.dumps(
            {'version': 1,
             'created': int(old.get('created') or _now), 'updated': _now,
             'moved_at': time.strftime('%Y-%m-%d %H:%M:%S'),
             'box': DUP_BOX_NAME,
             'how': u'同盘改名搬进来的，一枚没删；这张表跨轮累加，'
                    u'要还原就把每一条 name 搬回它自己的 dir',
             'items': items},
            ensure_ascii=False, indent=2))


def dedupe_yield_names(*, desktops: Optional[Sequence[str]] = None,
                       root: Optional[str] = None,
                       dry_run: bool = False,
                       include_differ: bool = False,
                       include_orphan: bool = False) -> dict:
    """把桌面上成对的"让路重复名"**搬进可见的重名回收盒**（同盘改名，一枚不删）。

    → {'same','differ','orphan','orphan_dir','legacy','taken','failed','total',
       'quarantine','dry_run','manifest','manifest_error','leftover_cleaned'}
    `manifest` 只在那份 `清单.json` **真写出来了**时给路径；写不出就给
    `manifest_error`（文件照搬、一枚不删，但界面上不许再承诺"按 清单.json 还原"）。

    收三类（★R324 D1 起）：① `yield_dup_candidates` 在桌面上判出 `'bytes'`/`'lnk'`
    的那几枚；② 盒根下历轮 `~让路…` 回收目录里的东西（★R323 L8）；
    ③ **原名已经不在了的那一枚文件**（`orphan`）——★R325 F1 起这一档**默认一枚不搬**。
    ★R324 把它并进自动批的理由是 win11 R041 那枚 `HashKit_20261011_152441.lnk` 留在
    桌面上、界面却念成"没有可以收拾的"；但代价更大：这一档**一把尺子都没有**（`same`
    有字节/快捷方式两把、`legacy` 有 `清单.json` 凭据），只靠文件名形状 + "原名不在同一
    目录"。用户自己导出的东西完全命中同一个形状——真机实案：报告 P1 那天他桌面上就躺着
    `DeskBits_日志_0.99-R042_20261014_081147.zip`，点一次【清理重复的图标】就把取证件
    收走了，而确认框还断言"这 N 枚是本程序以前留下的"。与红线「绝不碰用户的东西」冲突
    ⇒ 现在：默认只**点名**（逐枚一句「拿不出凭据」+ 一句汇总），要收由界面那**同一个**
    第二问问过用户，点【是】才带 `include_orphan=True` 再来一趟（★R321 D5 给 `differ`
    的那条口径，这一档照抄）。
    `differ`（判不出是不是同一枚）默认**一枚不碰**（★R321 D5 红线），但必须逐枚留痕
    说清差在哪一味；界面拿这个再问一句，用户点【是】才带 `include_differ=True` 再来一趟。
    `orphan_dir` 永远不碰。搬 = `os.rename` 进 `盒根/收纳·重复的/`，
    清单跨轮累加 ⇒ 想还原就把名字搬回 `dir` 那一格，不依赖本程序。
    **这一只盒只进不出**：`move_back` / 认领链 / 对账链对它一律绕开（见
    `is_dup_box` 那三处闸门），程序永远不会把里面的东西倒回桌面，也不删它。
    旧回收目录搬空后 `os.rmdir` 收掉（只收空目录，非空一律留在原地）；
    `清单.json` 本身不搬（它的条目已经并进盒里那份累加清单）。
    演练态（`dry_run=True`）一个目录都不建、一个字节都不动。
    跨卷（回收盒和桌面不在同一个盘）⇒ 那一枚留在原地进 `failed`，绝不"复制一份
    再删原件"（★R301 第 1 条口径：中途失败会留两份）。
    """
    res: Dict = {'same': [], 'differ': [], 'orphan': [], 'orphan_dir': [],
                 'legacy': [], 'taken': [], 'failed': [], 'total': 0,
                 'quarantine': '', 'dry_run': dry_run, 'manifest': '',
                 'manifest_error': '', 'leftover_cleaned': []}
    cand = yield_dup_candidates(desktops, root)
    res['same'], res['differ'], res['orphan'] = (
        cand['same'], cand['differ'], cand['orphan'])
    res['orphan_dir'], res['legacy'] = cand['orphan_dir'], cand['legacy']
    r = root or get_box_root()
    # 落点＝盒根下一只**看得见**的盒（★R323 L8）。从前是 `~让路_<MMDD>-<第几把>`：
    # 每次新开一把、名字带 `~` ⇒ 盒子面板读不到它（`list_boxes` 只认不带 `~` 的目录），
    # 用户既看不见也点不开，只能进资源管理器一层层翻 ——「方便我清理」恰恰没方便到。
    res['quarantine'] = dup_box_dir(r)
    _orp = [x for x in cand['orphan'] if os.path.isfile(x['path'])]
    for x in _orp:
        x.setdefault('why', 'orphan')
    # ★R325 F1：`orphan` 不再进自动批（这一档拿不出凭据，见函数 docstring）。
    _todo = list(cand['same']) + list(cand['legacy'])
    if include_differ:
        _todo += [x for x in cand['differ'] if os.path.isfile(x['path'])]
    if include_orphan:
        _todo += list(_orp)
    _nb = sum(1 for x in cand['same'] if x.get('why') == 'bytes')
    _nl = len(cand['same']) - _nb
    _nd = len(cand['differ'])
    _no = len(_orp)
    # 逐枚留痕的封顶（★R322 J7 同款）：一次收 39 枚不该念 39 行，超过 `PER_ITEM_LOG_CAP`
    # 就收口，剩下的**数**并进一句汇总——绝不静默吞掉，也绝不把 app.log 刷成一堵墙。
    _note_n = [0]

    def _note(fmt, *a):
        if _note_n[0] < PER_ITEM_LOG_CAP:
            _note_n[0] += 1
            logger.info(DUPE_LOG_PREFIX + fmt, *a)
            return True
        return False

    def _note_cap(total_n, got_n):
        """封顶之后把"还剩几枚没逐条念"报出来（差额不许丢，★R322 J2 同款口径）。"""
        if total_n > got_n:
            logger.info(DUPE_LOG_PREFIX + u'另有 %d 枚同一类没逐条念（上面已念 %d 条）',
                        total_n - got_n, got_n)

    def _log_left_differ():
        """默认不收的那几枚：逐枚点名 + 差在哪一味（★R324 D7）。

        win11 R041 实案的另一半：`differ`/`orphan` 全程**零逐枚日志**（那份 app.log 里
        `HashKit` 命中 0 行），界面只念一句"没有可以收拾的"——用户既不知道有这一枚，
        也不知道为什么不动它，只能读成"功能坏了"。
        """
        _n = _got = 0
        for x in cand['differ']:
            if not os.path.isfile(x['path']):
                continue
            _n += 1
            if _note(u'%s 没碰（判不出是不是同一枚：%s）', x['name'],
                     x.get('note') or _dup_note(os.path.join(x['dir'], x['base']),
                                                x['path'])):
                _got += 1
        _note_cap(_n, _got)

    def _log_left_orphan():
        """★R325 F1：默认不收的那几枚「拿不出凭据」的——逐枚点名 + 一句理由。

        口径与 `_log_left_differ` 完全一致：不许只声明不收集（★R322 K3 那族"根拔了线的
        灯"），也不许把 app.log 刷成一堵墙（同一份 `_note`/`_note_cap` 封顶）。
        """
        _n = _got = 0
        for x in _orp:
            _n += 1
            if _note(u'%s 没碰（只有名字带日期时间，拿不出凭据）', x['name']):
                _got += 1
        _note_cap(_n, _got)

    if not _todo:
        _asked = False
        if _nd and not include_differ:
            logger.info(DUPE_LOG_PREFIX + u'没有可以自动收拾的重复图标，有 %d 枚判不出'
                        u'是不是同一枚，要当面问一句才收', _nd)
            _log_left_differ()
            _asked = True
        if _no and not include_orphan:
            logger.info(DUPE_LOG_PREFIX + u'有 %d 枚拿不出凭据（只有名字带日期时间，'
                        u'多半是你自己导出的备份包），要当面问一句才收', _no)
            _log_left_orphan()
            _asked = True
        if not _asked:
            # 这一句**维持历轮原文**（词典里有它的规则、历史日志也认它）；
            # 只是"什么都没有"才念这一句，有"判不出/没凭据"那批时走上面那两句。
            logger.info(DUPE_LOG_PREFIX + u'桌面上没有可以收拾的让路名')
        return res
    if dry_run:
        logger.info(DUPE_LOG_PREFIX + u'可收 %d 枚（内容一样 %d、同一个程序 %d、'
                    u'以前那几把 %d），另有 %d 枚判不出是不是同一枚、%d 枚拿不出凭据，'
                    u'一枚没动',
                    len(_todo), _nb, _nl, len(cand['legacy']), _nd, _no)
        return res
    qd = res['quarantine']
    try:
        os.makedirs(qd, exist_ok=True)
    except OSError as e:
        logger.error('[盒子] 清理重复的让路名：回收目录写不出，这次一枚没搬：%s - %s',
                     qd, e)
        res['quarantine'] = ''
        return res
    qvol = _volume_key(qd)
    _moved_dirs = set()
    _orp_n = [0, 0]
    for x in _todo:
        target = _dup_slot(os.path.join(qd, x['name']))
        if not target:
            res['failed'].append(dict(x, reason='回收盒里同名那一格已经占满（试了 20 个空位）'))
            logger.warning('[盒子] 清理重复的让路名：%s 在回收盒里找不到空位', x['name'])
            continue
        xvol = _volume_key(x['dir'] or os.path.dirname(x['path']))
        if xvol and qvol and xvol != qvol:
            res['failed'].append(dict(x, reason='桌面和回收目录不在同一个磁盘，这一枚留在原地'))
            logger.warning('[盒子] 清理重复的让路名：%s 没能搬进回收目录：跨卷',
                           x['name'])
            continue
        try:
            os.rename(x['path'], target)
        except OSError as e:
            res['failed'].append(dict(x, reason=str(e)))
            logger.warning('[盒子] 清理重复的让路名：%s 没能搬进回收目录：%s - %s',
                           x['name'], type(e).__name__, e)
            continue
        _moved = dict(x)
        _moved['name'] = os.path.basename(target)
        res['taken'].append({'name': _moved['name'], 'base': x['base'],
                             'dir': x['dir'], 'why': x.get('why') or '',
                             'to': target})
        res['total'] += 1
        _moved_dirs.add(os.path.dirname(x['path']))
        if x.get('why') == 'orphan':
            # ★R324 D8：这一枚是"原名已经不在了"那一档，收得跟 same 档不一样理由，
            # 就得逐枚留一行说清为什么收它（用户下次翻日志能对上号）。
            _orp_n[0] += 1
            if _note(u'%s 收进「%s」（原名已经不在了）', _moved['name'], DUP_BOX_NAME):
                _orp_n[1] += 1
    _note_cap(_orp_n[0], _orp_n[1])
    if not include_differ:
        _log_left_differ()
    if not include_orphan:
        # ★R325 F1：这一档默认不收 ⇒ 也必须逐枚点名（跟 `differ` 同一套口径，
        # 不能"自动批里有东西就闭嘴"——用户要的是每一枚没动的理由都说得出）。
        # 汇总那一句也照样念（措辞与"什么都没得收"那一支**逐字相同** ⇒ 词典只加一条
        # 规则就够）：搬完那几枚有凭据的之后，用户看得见"还有 2 枚我们没碰、为什么没碰"。
        if _no:
            logger.info(DUPE_LOG_PREFIX + u'有 %d 枚拿不出凭据（只有名字带日期时间，'
                        u'多半是你自己导出的备份包），要当面问一句才收', _no)
        _log_left_orphan()
    # 旧那几把回收：搬**空**了才收掉目录（只删空目录，非空一枚不动）。
    # 只剩一份自己的 `清单.json` 的也算非空——那把里的东西已经并进盒里这份累加清单了，
    # 但"一枚都不删"不给自己开后门：目录留着，只在日志里说清它只剩一张纸。
    for _old in yield_leftover_dirs(r):
        try:
            _rest = sorted(os.listdir(_old))
        except OSError as e:
            logger.warning('[盒子] 旧回收目录读不动，这一把不动 %s: %s', _old, e)
            continue
        try:
            if not _rest:
                os.rmdir(_old)
                res['leftover_cleaned'].append(_old)
                _moved_dirs.add(_old)
            elif _rest == [YIELD_MANIFEST_NAME]:
                logger.info('[盒子] 旧回收目录里只剩一份清单（东西已经并进「%s」那份）：'
                            '%s —— 这一把目录我没删，要清就删它', DUP_BOX_NAME, _old)
            else:
                logger.info('[盒子] 旧回收目录里还剩 %d 样没搬 %s（这一节只搬文件，'
                            '目录树一类不动）', len(_rest), _old)
        except OSError as e:
            logger.warning('[盒子] 旧回收目录没收回 %s: %s', _old, e)
    if res['taken']:
        try:
            _write_yield_manifest(qd, res['taken'])
            res['manifest'] = os.path.join(qd, YIELD_MANIFEST_NAME)
        except OSError as e:
            # 东西已经搬进来了，清单写不出 ⇒ 必须大声说，别让人以为退路还在
            # ★R321-S6：从前只落 app.log，界面上那句"按 清单.json 把名字搬回去"就成了
            # 假承诺（文件根本没有）。失败标记现算现带给调用方，面板据此换措辞。
            res['manifest_error'] = '%s: %s' % (type(e).__name__, e)
            logger.error('[盒子] 清理重复的让路名：已搬 %d 枚但 清单.json 没写出（%s）'
                         '——退路是那个目录本身，里面的文件按原名认',
                         len(res['taken']), e)
        logger.info('[盒子] 清理重复的让路名：已收 %d 枚进 %s（一枚没删，退路在那份 %s）',
                    len(res['taken']), qd, YIELD_MANIFEST_NAME)
        # ★R325 F7（报告 P7）：第二份路径旧写法拼的是 `dir + t['name']`——而 `name`
        # 在上面已被换成**盒里的新名**（撞名还带 `~2`），拼上原目录指向一个从未存在过
        # 的路径；真正空出来的那一格是 `dir/base`。目录级通知一直兜住了刷新，所以这是
        # 死代码级错误、无人可见——现在拼对，桌面立刻看得见那一格腾出来。
        _notify([t['to'] for t in res['taken']]
                + [os.path.join(t['dir'], t['base']) for t in res['taken']
                   if t.get('dir') and t.get('base')],
                list(_moved_dirs | {os.path.dirname(t['to']) for t in res['taken']}
                     | {qd}))
    return res


def box_contents(box: str, root: Optional[str] = None) -> List[str]:
    """盒子里当前实际有哪些文件（以磁盘为准，不读账本）。"""
    d = box_dir_path(box, root or get_box_root())
    try:
        return sorted(os.listdir(d), key=lambda s: s.lower())
    except OSError:
        return []


def box_summary(root: Optional[str] = None) -> List[dict]:
    """每个盒子一条：名字 / 目录 / 里面几枚 / 账本记了几枚。"""
    r = root or get_box_root()
    out = []
    for b in list_boxes(r):
        files = box_contents(b, r)
        out.append({'box': b, 'dir': box_dir_path(b, r),
                    'count': len(files), 'files': files})
    return out
