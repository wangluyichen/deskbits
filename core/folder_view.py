# -*- coding: utf-8 -*-
r"""★R246 #198「拖的动作必须是模拟用户拖动吗？不可以程序化吗？不可以更快吗？」

用户 38 批 item2b 的那条"程序化的路"，到本模块为止**跑通并进了产品**：
不用碰鼠标、不用等拖动动画，直接调微软自己的桌面视图接口
`IFolderView2::SetSortColumns(真缓冲, 0)` 把活动排序清掉（本机 4/4 轮 +
六种写法穷举里的 V1/V2 两档全过，单次 ~0.0 秒）。

## 为什么 ★R244 判"官方接口能设不能清"是错的（一手依据）

★R244 只做对了一半：接口拿得到、排序**设得进去**。判"清不掉"栽在两件事上，
本模块的取证都在 `D:/Qoder-workplace/tmp/bugflow/20260925_0733/run/r246/`：

1. **取错了对象**。在 `.Document`（`IShellFolderViewDual`）上直接
   `QueryInterface(IFolderView2)` 是 `E_NOINTERFACE`（`r246_fv2_probe.txt`
   的 6a/6b 两步再复现了一遍：`0x80004002`）。官方给的取法是另一条链：
   `CoCreateInstance(ShellWindows, LOCAL_SERVER)` →
   `IShellWindows::FindWindowSW(SWC_DESKTOP, SWFO_NEEDDISPATCH)` →
   QI `IServiceProvider` → `QueryService(SID_SShellBrowser, IShellBrowser)` →
   `IShellBrowser::QueryActiveShellView` → QI `IFolderView2` = **hr 0**。
2. **"文档说的 NULL 清空"根本送不到 Explorer**。`SetSortColumns(NULL, 0)` 的
   hr 是 `0x800706F4` = Win32 1780 = `RPC_X_NULL_REF_POINTER`（"向存根传递了
   一个空引用指针"）。一手依据：`ShObjIdl_core.h` 里这个形参写作
   `/* [size_is][in] */ __RPC__in_ecount_full(cColumns) const SORTCOLUMN *`
   —— `__RPC__in_ecount_full`（不是 `_opt`）= **不允许 NULL**，存根在跨进程
   那一头就把参数挡了，Explorer 里那个函数压根没被调到。
   ⇒ 正解：**给一块真的（零值）24 字节缓冲 + 计数 0**，hr=0 且真清掉。
   顺手排掉的两个假通过：`{GUID_NULL},pid 0` 会让回读变成 `'prop:System.Null;'`
   （★R244 那个"设成功了其实没治好"的状态）；`ItemNameDisplay + dir=0` 旧列还在。

## 与 dual 的关系（为什么这条不算"另一个影子视图"）

`r246_b4_pc2.txt` 阳性对照②：本模块这条路 `SetSortColumns` 设成名称列之后，
程序一直在用的 dual 读数（`Shell.Application`）当场跟着变成
`'prop:System.ItemNameDisplay;'`，26 个图标当场被重摆（偏离 20）
⇒ 两个入口驱动的是**同一个视图**，判据仍然统一走"COM 读回为空"。

## 红线（和 ★R242~★R245 那套一模一样，一条没松）

* **只调自己进程里拿到的 COM 代理**，绝不写、也不读别的进程的内存，不注入
  （门禁 G2 就是钉这条：那三个跨进程写内存的 API 名字**一个字都不许出现在
  本文件里**，连"我不会用它"的声明都不写——写了就没法用"字串命中"当红线了）；
* 本模块**不发鼠标**，也**不替用户决定要不要修**——它是 `sort_clear` 手里
  的一个可注入工具，什么时候调、要不要摆布局，全在 `core/sort_clear.py`
  那条"读状态 → 该提才提 → 点『是』才动手"的链路里定（免鼠标这条因为
  一根手指都不占，所以放在确认框之前先试，见 `offer_and_clear`）；
* 不进 `core/dock.py`（悬浮条 `_wndproc` 必须保持纯 GDI，门禁 G5）；
* 顶层只 import 标准库（`ctypes`），**不引 comtypes/pywin32**（门禁 G6）——
  裸指针全部自己从 `CoCreateInstance` 拿，打包时一个新依赖都不多。
"""

from __future__ import annotations

import ctypes
import logging
import struct
import time
from ctypes import wintypes
from typing import Callable, List, Optional, Sequence, Tuple

logger = logging.getLogger("deskbits")

# ── 现场从本机 SDK 头文件数出来的槽位表（那张表在 run/r246/r246_slots.txt）──
# 为什么要数而不是抄：`IFolderView2` 继承 `IFolderView`（14 个方法）继承
# `IShellView` 继承 `IUnknown`，抄来的号经常差 1（实测 `QueryActiveShellView`
# 是 15 不是 12 —— `SendControlMsg` 占着 14）。
SLOTS = {
    "IShellWindows.FindWindowSW": 15,
    "IServiceProvider.QueryService": 3,
    "IShellBrowser.QueryActiveShellView": 15,
    "IFolderView2.SetGroupBy": 17,
    "IFolderView2.GetGroupBy": 18,
    "IFolderView2.GetSortColumnCount": 26,
    "IFolderView2.SetSortColumns": 27,
    "IFolderView2.GetSortColumns": 28,
}
SLOT_QUERYINTERFACE = 0
SLOT_RELEASE = 2

SHELL_WINDOWS_CLSID = '9BA05972-F6A8-11CF-A442-00A0C90A8F39'
IID_ISHELLWINDOWS = '85CB6900-4D95-11CF-960C-0080C7F4EE85'
IID_ISERVICEPROVIDER = '6D5140C1-7436-11CE-8034-00AA006009FA'
# SID_SShellBrowser 与 IID_IShellBrowser 同一个 GUID（ShellEx 里就是这么定义的）
IID_ISHELLBROWSER = '000214E2-0000-0000-C000-000000000046'
IID_IFOLDERVIEW2 = '1AF3A467-214F-4298-908E-06B03E0B39F9'

SWC_DESKTOP = 0x8
SWFO_NEEDDISPATCH = 0x1
CLSCTX_INPROC_SERVER = 1
CLSCTX_LOCAL_SERVER = 4
# 本机注册表实测：HKLM\...\CLSID\{9BA05972-…} 下面**只有** LocalServer32
# （`rundll32 shell32.dll,SHCreateLocalServerRunDll`），没有 InprocServer32
# ⇒ 单给 INPROC_SERVER 会 0x80040154 REGDB_E_CLASSNOTREG，必须走 LOCAL_SERVER。

SORT_ASCENDING = 1
SORT_DESCENDING = -1
#: `SORTCOLUMN` = PROPERTYKEY{FMTID 16B + PID UINT32} + SORTDIRECTION(int) ⇒ 24B
SORTCOLUMN_SIZE = 24
#: 一手来源：SDK `um/propkey.h:2271`
#:   DEFINE_PROPERTYKEY(PKEY_ItemNameDisplay, 0xB725F130,0x47EF,0x101A,
#:                      0xA5,0xF1,0x02,0x60,0x8C,0x9E,0xEB,0xAC, 10)
#: ⇒ "名称"这一列是 fmtid `{B725F130-…-AC}` + pid 10。
#:   ⚠ 别拿 `{F29F85E8-…}` 当名称列（那是 System 属性系统的另一组，阳性对照会假绿）。
PKEY_ITEM_NAME_DISPLAY = ('{B725F130-47EF-101A-A5F1-02608C9EEBAC}', 10)
#: 传 NULL 会被 RPC 存根挡下的那个 hr（见文件头第 2 条）——写在这儿方便日志/门禁认。
HR_NULL_REF_POINTER = 0x800706F4

_P = ctypes.c_void_p
_LP = ctypes.POINTER
ole32 = ctypes.windll.ole32
kernel32 = ctypes.windll.kernel32
kernel32.VirtualQuery.restype = ctypes.c_size_t
kernel32.VirtualQuery.argtypes = [_P, _P, ctypes.c_size_t]


# ── 小工具 ─────────────────────────────────────────────────────────
def _guid_bytes(s: str) -> bytes:
    """GUID 字符串 → 16 字节（Data1/2/3 小端，Data4 原样）。"""
    h = s.strip('{}').replace('-', '')
    return (struct.pack('<I', int(h[0:8], 16)) + struct.pack('<H', int(h[8:12], 16))
            + struct.pack('<H', int(h[12:16], 16)) + bytes.fromhex(h[16:32]))


def _gbuf(s: str):
    b = ctypes.create_string_buffer(16)
    ctypes.memmove(b, _guid_bytes(s), 16)
    return b


def _gstr(raw16: bytes) -> str:
    d1, d2, d3 = struct.unpack_from('<IHH', raw16, 0)
    tail = bytes(raw16[8:16])
    return '{%08X-%04X-%04X-%s-%s}' % (d1, d2, d3, tail[:2].hex().upper(),
                                       tail[2:].hex().upper())


def _read_ptr(addr) -> int:
    """读本进程内存里的一个指针（失败返回 0，不抛）。

    ★ 这里踩过一个足以骗过整轮实验的坑：**不能**写成
    `cast(c_void_p(a), POINTER(c_uint64)).contents.value` —— `ctypes.cast`
    与源对象**共用缓冲区**，`.contents` 读到的是那个临时对象自己的字节，
    实测对任何地址都返回"地址本身"，于是整张 vtable 全是 `对象地址 + 8n`，
    每一步都"看着合法"，调下去才 access violation（`r246_fv2_dbg.txt`）。
    正确写法：`cast(addr, POINTER(c_size_t))[0]`（用 `c_size_t` 而不是
    `c_uint64`：32 位 Python 下指针是 4 字节，读 8 字节会把下一个字段抄进来）。
    """
    if not addr:
        return 0
    try:
        return int(ctypes.cast(ctypes.c_void_p(int(addr)),
                               _LP(ctypes.c_size_t))[0]) or 0
    except Exception:
        return 0


class _MBI(ctypes.Structure):
    _fields_ = [('BaseAddress', _P), ('AllocationBase', _P),
                ('AllocationProtect', wintypes.DWORD),
                ('RegionSize', ctypes.c_size_t),
                ('State', wintypes.DWORD), ('Protect', wintypes.DWORD),
                ('Type', wintypes.DWORD)]


def _readable(addr) -> bool:
    """地址是否落在本进程一块**已提交**内存里（裸调 vtable 之前的最后一道护栏）。

    ★ 踩过的第二个坑：跨进程 COM 代理的 **vtable 本身在堆上**，所以合理性检查
    只能卡在"槽里的函数指针"上；拿 `GetModuleHandleExW` 去卡 vtable 会把整条链
    误杀成"bad vtable slot 15"（`r246_fv2_probe.txt` 有对照读数）。
    这一道不能省：调一个坏指针不是抛 Python 异常，是**整个进程当场没了**。
    """
    if not addr:
        return False
    mbi = _MBI()
    try:
        n = kernel32.VirtualQuery(ctypes.c_void_p(int(addr)), ctypes.byref(mbi),
                                  ctypes.c_size_t(ctypes.sizeof(mbi)))
    except Exception:
        return False
    return bool(n) and mbi.State == 0x1000            # MEM_COMMIT


def _slot(ptr, n: int) -> int:
    """取对象第 n 槽的函数地址；不合法一律 0（调用方据此放弃，不硬调）。"""
    vt = _read_ptr(ptr)
    if not vt or not _readable(vt):
        return 0
    f = _read_ptr(vt + n * ctypes.sizeof(_P))
    return f if (f and _readable(f)) else 0


def _bind(ptr, n: int, argtypes: Sequence, restype=ctypes.c_long):
    a = _slot(ptr, n)
    if not a:
        raise RuntimeError('bad vtable slot %d on 0x%x' % (n, ptr or 0))
    return ctypes.CFUNCTYPE(restype, *([_P] + list(argtypes)))(a)


def _qi(ptr, iid: str) -> Tuple[int, int]:
    out = _P()
    hr = _bind(ptr, SLOT_QUERYINTERFACE, [_P, _LP(_P)])(
        ptr, ctypes.byref(_gbuf(iid)), ctypes.byref(out))
    return (hr & 0xffffffff), (out.value or 0)


def _release(ptr) -> None:
    try:
        _bind(ptr, SLOT_RELEASE, [])(ptr)
    except Exception as e:
        logger.debug("[folder_view] Release failed on 0x%x: %s", ptr or 0, e)


def _co_init() -> None:
    """按需装 COM。装不上就算了（跨进程代理照旧能用），也**不主动 Uninitialize**：
    本进程的 COM 计数可能早就被 pywin32 那边占着，乱还会把别人的引用计数打花。
    `RPC_E_CHANGED_MODE`(0x80010106) 表示"这个线程已经以别的模型装过了"，照用。
    """
    try:
        ole32.CoInitializeEx(None, ctypes.c_uint(0x2))   # APARTMENTTHREADED
    except Exception as e:
        logger.debug("[folder_view] CoInitializeEx failed: %s", e)


# ── 取接口：那条官方链 ─────────────────────────────────────────────
def open_folder_view2(log: Optional[Callable[..., None]] = None
                      ) -> Tuple[int, List[int]]:
    """跑通官方取法拿桌面 `IFolderView2`；返回 ``(指针, 待 Release 列表)``。

    拿不到 ⇒ ``(0, [])``（不抛）。失败点写在日志里（第一条非 0 的 hr 就是断点）。
    """
    keep: List[int] = []

    def _l(*a):
        if log:
            try:
                log(*a)
            except Exception:
                pass

    _co_init()
    sw = 0
    for ctx in (CLSCTX_LOCAL_SERVER, CLSCTX_INPROC_SERVER | CLSCTX_LOCAL_SERVER):
        p = _P()
        hr = ole32.CoCreateInstance(ctypes.byref(_gbuf(SHELL_WINDOWS_CLSID)), None,
                                    ctypes.c_ulong(ctx),
                                    ctypes.byref(_gbuf(IID_ISHELLWINDOWS)),
                                    ctypes.byref(p))
        hr &= 0xffffffff
        if hr == 0 and p.value:
            sw = p.value
            break
        _l('open: CoCreateInstance(ShellWindows) ctx=0x%X hr=0x%08X' % (ctx, hr))
    if not sw:
        return 0, []
    keep.append(sw)

    # 形参写作 `long *phwnd`，但实测代理桩按**指针宽度**写（给 4 字节接收位会
    # 被写越界；r246_fv2_dbg.txt D4 用 8 字节 HWND 才干净地拿到句柄）。
    vl = ctypes.create_string_buffer(16)      # VARIANT (VT_EMPTY)
    vr = ctypes.create_string_buffer(16)
    hwnd = wintypes.HWND()
    disp = _P()
    hr = _bind(sw, SLOTS['IShellWindows.FindWindowSW'],
               [_P, _P, ctypes.c_int, _P, ctypes.c_int, _LP(_P)])(
        sw, ctypes.byref(vl), ctypes.byref(vr), ctypes.c_int(SWC_DESKTOP),
        ctypes.byref(hwnd), ctypes.c_int(SWFO_NEEDDISPATCH), ctypes.byref(disp))
    hr &= 0xffffffff
    if hr != 0 or not disp.value:
        _l('open: FindWindowSW hr=0x%08X' % hr)
        return 0, keep
    keep.append(disp.value)

    hr, sp = _qi(disp.value, IID_ISERVICEPROVIDER)
    if hr != 0 or not sp:
        _l('open: QI IServiceProvider hr=0x%08X' % hr)
        return 0, keep
    keep.append(sp)

    sb = _P()
    hr = _bind(sp, SLOTS['IServiceProvider.QueryService'], [_P, _P, _LP(_P)])(
        sp, ctypes.byref(_gbuf(IID_ISHELLBROWSER)),
        ctypes.byref(_gbuf(IID_ISHELLBROWSER)), ctypes.byref(sb))
    hr &= 0xffffffff
    if hr != 0 or not sb.value:
        _l('open: QueryService(SID_SShellBrowser) hr=0x%08X' % hr)
        return 0, keep
    keep.append(sb.value)

    sv = _P()
    hr = _bind(sb.value, SLOTS['IShellBrowser.QueryActiveShellView'], [_LP(_P)])(
        sb.value, ctypes.byref(sv))
    hr &= 0xffffffff
    if hr != 0 or not sv.value:
        _l('open: QueryActiveShellView hr=0x%08X' % hr)
        return 0, keep
    keep.append(sv.value)

    hr, fv2 = _qi(sv.value, IID_IFOLDERVIEW2)
    if hr != 0 or not fv2:
        _l('open: QI IFolderView2 hr=0x%08X' % hr)
        return 0, keep
    keep.append(fv2)
    return fv2, keep


def release_chain(keep: Optional[Sequence[int]]) -> None:
    """逆序 Release 链上的每个指针（引用计数不能漏，漏了 Explorer 那头挂着代理）。"""
    for p in reversed(list(keep or [])):
        _release(p)


# ── 读／写排序列 ───────────────────────────────────────────────────
def get_sort_columns(fv2) -> Optional[List[Tuple[str, int, int]]]:
    """读回排序列 `[(fmtid, pid, dir), ...]`（槽 26 数个数 + 槽 28 取内容）。

    失败返回 ``None``——★R242 起的纪律：**读不到就当没治好**，不许当成"空=清干净了"。
    ⚠ 缓冲区必须用 `c_ubyte`：`c_byte` 数组切片是有符号 int，`bytes()` 会
    `ValueError: bytes must in range(0,256)`（第一个非 ASCII 的 GUID 字节就炸）。
    """
    n = ctypes.c_int(-1)
    hr = _bind(fv2, SLOTS['IFolderView2.GetSortColumnCount'],
               [_LP(ctypes.c_int)])(fv2, ctypes.byref(n))
    if hr != 0:
        return None
    cnt = max(int(n.value or 0), 0)
    if not cnt:
        return []
    arr = (ctypes.c_ubyte * (SORTCOLUMN_SIZE * cnt))()
    hr = _bind(fv2, SLOTS['IFolderView2.GetSortColumns'], [_P, ctypes.c_int])(
        fv2, ctypes.addressof(arr), ctypes.c_int(cnt))
    if hr != 0:
        return None
    out = []
    for i in range(cnt):
        b = SORTCOLUMN_SIZE * i
        out.append((_gstr(bytes(arr[b:b + 16])),
                    struct.unpack_from('<I', arr, b + 16)[0],
                    struct.unpack_from('<i', arr, b + 20)[0]))
    return out


def clear_sort_columns(fv2) -> int:
    """把桌面视图的活动排序清掉：`SetSortColumns(零值真缓冲, 0)`（槽 27）。

    ★ 计数 0 **必须配一块真的缓冲**。传 NULL 看着更"按文档"，但那一下在
    RPC 存根就被挡了（`0x800706F4` = `RPC_X_NULL_REF_POINTER`），Explorer 里
    的目标函数压根没跑到——★R244 判"官方接口清不掉"就是撞在这上面。
    返回 hr（已掩成无符号）。
    """
    buf = ctypes.create_string_buffer(SORTCOLUMN_SIZE)
    hr = _bind(fv2, SLOTS['IFolderView2.SetSortColumns'], [_P, ctypes.c_int])(
        fv2, ctypes.addressof(buf), ctypes.c_int(0))
    return hr & 0xffffffff


def set_sort_column(fv2, fmtid: str = PKEY_ITEM_NAME_DISPLAY[0],
                    pid: int = PKEY_ITEM_NAME_DISPLAY[1],
                    direction: int = SORT_ASCENDING) -> int:
    """写一列排序（**只给取证/阳性对照用**，产品路径不调它）。

    用途：证明"这条路和 dual 驱动的是同一个视图"——设完立刻能在 dual 的
    `SortColumns` 上看到同样的值（`r246_b4_pc2.txt`）。返回 hr。
    """
    buf = ctypes.create_string_buffer(SORTCOLUMN_SIZE)
    ctypes.memmove(buf, _guid_bytes(fmtid), 16)
    struct.pack_into('<I', buf, 16, pid)
    struct.pack_into('<i', buf, 20, direction)
    hr = _bind(fv2, SLOTS['IFolderView2.SetSortColumns'], [_P, ctypes.c_int])(
        fv2, ctypes.addressof(buf), ctypes.c_int(1))
    return hr & 0xffffffff


def read_sort_columns_live() -> Optional[List[Tuple[str, int, int]]]:
    """只读跑一遍真链（开链 → 读排序列 → Release）；任何异常都收成 ``None``。

    这是"我能不能碰到那个视图"的诊断入口，**一个字节都不写**。
    """
    ptr, keep = 0, []
    try:
        ptr, keep = open_folder_view2()
        return get_sort_columns(ptr) if ptr else None
    except Exception as e:
        logger.debug("[folder_view] read_sort_columns_live: %s: %s",
                     type(e).__name__, e)
        return None
    finally:
        release_chain(keep)


# ── 产品入口 ───────────────────────────────────────────────────────
def clear_active_sort(opener: Optional[Callable[[], Tuple[int, Sequence]]] = None,
                      setter_probe: Optional[Callable[[int], None]] = None,
                      setter_hr: Optional[int] = None,
                      reader: Optional[Callable[[int], Optional[list]]] = None,
                      closer: Optional[Callable[[Sequence], None]] = None,
                      dry_run: bool = False) -> dict:
    """免鼠标清掉桌面的活动排序；**永远返回 dict，永远不抛**。

    返回：``{'ok', 'hr', 'before', 'after', 'secs', 'err'}``。

    成败口径（判据 V2~V8，别放宽）：
      * `ok` 要求 **hr==0 且"清完再读回"真的是空列表**——★R244 的教训就是
        hr=0 不等于治好（`{GUID_NULL}` 那档 hr 也是 0，回读却变成 `System.Null`）；
      * 读回读不到（``None``）⇒ 判失败：不确定就当没治好；
      * 链都拿不到 ⇒ `ok=False`、`err='no-folder-view2'`，**一发都不发**；
      * 里面任何一步抛异常 ⇒ 吞成 `ok=False`，不许把主窗带崩；
      * 不管走哪一支，链上拿到的指针都要 Release（引用计数漏给 Explorer）。

    全部参数都可注入 ⇒ 回归测试一次真的都不发（`测试_ui/test_r246_view_clear.py`）：
    ``opener/reader/closer`` 换掉那条 COM 链，``setter_hr`` 直接给定 hr，
    ``setter_probe`` 用来数"到底发没发"，``dry_run`` = 只开链看看、一列都不写。
    """
    t0 = time.time()
    out = {'ok': False, 'hr': None, 'before': None, 'after': None,
           'secs': 0.0, 'err': ''}
    keep: Sequence = []
    try:
        ptr, keep = (opener or open_folder_view2)()
        if not ptr:
            out['err'] = 'no-folder-view2'
            return out
        read = reader or get_sort_columns
        out['before'] = read(ptr)
        if dry_run:
            out['err'] = 'dry-run(no-write)'
            return out
        if setter_probe is not None:
            setter_probe(ptr)
        hr = setter_hr if setter_hr is not None else clear_sort_columns(ptr)
        hr = None if hr is None else (int(hr) & 0xffffffff)
        out['hr'] = hr
        after = read(ptr)
        out['after'] = after
        if hr not in (0,):
            out['err'] = ('set-hr:0x%08X%s' % (hr or 0, '(NULL 被 RPC 存根挡下)'
                                               if hr == HR_NULL_REF_POINTER else '')
                          )
        elif after is None:
            out['err'] = 'readback-failed'
        elif len(after):
            out['err'] = 'still-sorted:%d col' % len(after)
        else:
            out['ok'] = True
        return out
    except Exception as e:
        out['err'] = '%s:%s' % (type(e).__name__, str(e)[:120])
        logger.debug("[folder_view] clear_active_sort failed: %s", out['err'])
        return out
    finally:
        out['secs'] = round(time.time() - t0, 3)
        try:
            if closer is not None:
                closer(keep)
            else:
                release_chain(keep)
        except Exception:
            pass


if __name__ == '__main__':          # 只读自检：不开窗、不写任何东西
    cols = read_sort_columns_live()
    print('IFolderView2 sort columns (read-only) =', cols)
