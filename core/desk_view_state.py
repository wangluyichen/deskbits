# -*- coding: utf-8 -*-
r"""★R238 桌面视图状态只读诊断（对应挂账 #123）

背景（本机 win10 实测，取证见 run/lab/desk_bag_1Desktop.json + menu4.txt）：

* 桌面图标视图真正的"排序方式/自动排列"状态存在
      HKCU\Software\Microsoft\Windows\Shell\Bags\1\Desktop
  的 ``Sort`` / ``FFlags`` 两个值里。**不是**
      ...\Classes\Local Settings\...\Shell\Bags\*\{5C4F28B5-...}
  ——那一棵树是"其它文件夹"的 bag，桌面根本不往那儿写（早期误读它，
  才得出"设了排序 Sort 不动"的错误结论）。
* ``Sort`` 是 20 字节头（偏移 16 起 DWORD = 排序列数）+ 每列 24 字节
  （FMTID16 + PID4 + 方向4）。列数 0 / 全零 = 无排序。
* 它是**镜像**不是开关：实测把注册表 Sort 写成全零，右键"刷新"后视图照旧
  按名称重排，且 Sort 立刻被 Explorer 改回 PID=10。⇒ 只读它做诊断有效，
  想靠写它改行为无效（要改行为得走 IFolderView2，见工作进度挂账）。

本模块**只读**：不写注册表、不发消息给 Explorer、不碰任何窗口。
读失败一律返回 ``{'ok': False, 'err': ...}``，绝不抛异常出去。
"""

from __future__ import annotations

import logging
import struct
from typing import Optional

logger = logging.getLogger("deskbits")

DESK_BAG_PATH = r"Software\Microsoft\Windows\Shell\Bags\1\Desktop"
_SORT_FMTID = bytes.fromhex("30f125b7ef471a10a5f102608c9eebac")

# 桌面"排序方式"子菜单用到的属性 PID
PID_LABEL = {
    10: "name",
    11: "type",
    12: "size",
    14: "date-modified",
    4: "name-legacy",
    30: "attrib",
}

# FWF_* / FOLDERFLAGS。位值与名字取自本机 Windows SDK
#   C:\Program Files (x86)\Windows Kits\10\Include\10.0.26100.0\um\ShObjIdl_core.h
#   （enum FOLDERFLAGS，5265 行起），并与
#   .venv/.../win32comext/shell/shellcon.py 的 FWF_* 常量逐条对过。
# ★R242 校准：旧表里 0x100000/0x200000/0x400000 三个名字（nomodify /
#   neversortgroup / nogroups）SDK 查无此名，是按"猜测的含义"起的，
#   真名是 NOENUMREFRESH / FULLROWSELECT / NOFILTERS。名字错会让日志
#   把"桌面常态位"读成"某种排序开关"，直接带偏诊断，故一并改正。
#   关键位 `0x1 = autoarrange` 三处（SDK / shellcon / 我们）一致 ⇒ 之前
#   win11 日志里的 `autoarrange=1` 是**真状态**，不是解码错位。
FWF = {
    0x00000001: "autoarrange",        # FWF_AUTOARRANGE 右键"自动排列图标"
    0x00000004: "snaptogrid",         # FWF_SNAPTOGRID
    0x00000020: "desktop",            # FWF_DESKTOP（桌面视图恒有）
    0x00000200: "noclientedge",       # FWF_NOCLIENTEDGE（桌面常态位）
    0x00000800: "alignleft",          # FWF_ALIGNLEFT
    0x00010000: "nowebview",          # FWF_NOWEBVIEW
    0x00100000: "nogrouping",         # FWF_NOGROUPING（旧名 nomodify，SDK 无此位）
    0x00200000: "fullrowselect",      # FWF_FULLROWSELECT（旧名 neversortgroup）
    0x00400000: "nofilters",          # FWF_NOFILTERS（旧名 nogroups）
    0x40000000: "usesearchfolder",    # FWF_USESEARCHFOLDER（桌面常带）
}


def decode_sort(blob: Optional[bytes]) -> dict:
    """把 ``Sort`` 原始字节翻成结构化结果（纯函数，便于测试）。

    返回 ``{"count": int, "columns": [(pid, pid_label, direction)],
            "column": str|None, "sorted": bool}``。
    长度不足 / 全零都算"无排序"，不抛异常。
    """
    out = {"count": 0, "columns": [], "column": None, "sorted": False}
    if not blob:
        return out
    try:
        data = bytes(blob)
    except (TypeError, ValueError):
        return out
    if len(data) < 20:
        return out
    try:
        count = struct.unpack_from("<I", data, 16)[0]
    except struct.error:
        return out
    if count <= 0 or count > 8:
        return out
    off = 20
    cols = []
    for _ in range(count):
        if off + 24 > len(data):
            break
        fmtid = data[off:off + 16]
        try:
            pid = struct.unpack_from("<I", data, off + 16)[0]
            direction = struct.unpack_from("<i", data, off + 20)[0]
        except struct.error:
            break
        label = PID_LABEL.get(pid, "pid-%d" % pid)
        cols.append((pid, label, "asc" if direction == 1 else
                     "desc" if direction == -1 else str(direction)))
        off += 24
    out["count"] = len(cols)
    out["columns"] = cols
    if cols:
        out["column"] = cols[0][1] + ("" if cols[0][2] == "asc" else "-" + cols[0][2])
        out["sorted"] = True
    return out


def decode_fflags(value: int) -> list:
    """FWF 位图 → 名字列表（纯函数）。"""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return []
    return [name for bit, name in sorted(FWF.items()) if v & bit]


def read_desk_view_state(path: str = DESK_BAG_PATH) -> dict:
    """只读桌面视图状态。任何异常都吞掉，返回 ``ok=False`` + ``err``。"""
    res = {"ok": False, "err": "", "sort": None, "sorted": False,
           "sort_column": None, "fflags": 0, "fflags_hex": "0x00000000",
           "flags": [],
           "autoarrange": False, "mode": None, "icon_size": None,
           "raw_sort": ""}
    try:
        import winreg
    except ImportError:                                   # 非 Windows
        res["err"] = "no winreg"
        return res
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                            winreg.KEY_READ) as k:
            def _get(name):
                try:
                    return winreg.QueryValueEx(k, name)[0]
                except OSError:
                    return None
            raw = _get("Sort")
            res["raw_sort"] = (raw.hex() if isinstance(raw, (bytes, bytearray))
                               else "")
            res["sort"] = decode_sort(raw)
            res["sorted"] = bool(res["sort"]["sorted"])
            res["sort_column"] = res["sort"]["column"]
            ff = _get("FFlags")
            res["fflags"] = int(ff) if isinstance(ff, int) else 0
            res["fflags_hex"] = "0x%08X" % (res["fflags"] & 0xFFFFFFFF)
            res["flags"] = decode_fflags(res["fflags"])
            res["autoarrange"] = bool(res["fflags"] & 0x1)
            res["mode"] = _get("Mode")
            res["icon_size"] = _get("IconSize")
        res["ok"] = True
    except FileNotFoundError:
        res["err"] = "bag key missing"
    except OSError as e:
        res["err"] = "reg %s" % (getattr(e, "strerror", None) or e)
    except Exception as e:                                # 兜底：诊断不许炸主流程
        res["err"] = "%s: %s" % (type(e).__name__, e)
    return res


def summarize(st: dict) -> str:
    """一行英文摘要，直接进日志（日志英文化方案 A）。"""
    if not st or not st.get("ok"):
        return "desk-view=unavailable(%s)" % (st or {}).get("err", "?")
    parts = ["sort=%s" % (st.get("sort_column") or "none"),
             "autoarrange=%d" % int(bool(st.get("autoarrange"))),
             "flags=%s" % ("|".join(st.get("flags") or []) or "0"),
             "fflags=%s" % (st.get("fflags_hex") or
                             "0x%08X" % (int(st.get("fflags") or 0) & 0xFFFFFFFF)),
             "mode=%s" % st.get("mode"),
             "icon=%s" % st.get("icon_size")]
    if st.get("sorted"):
        parts.append("note=view-sort-owns-layout")
    if st.get("autoarrange"):
        parts.append("note=autoarrange-on")
    return " ".join(parts)


def log_desk_view_state(tag: str = "desk-view") -> dict:
    """读一次 + 打一行 INFO 日志；返回读到的 dict 供调用方自用。"""
    st = read_desk_view_state()
    try:
        logger.info("[%s] %s", tag, summarize(st))
    except Exception:
        pass
    return st
