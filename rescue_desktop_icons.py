# -*- coding: utf-8 -*-
"""桌面图标消失·一键救援脚本

症状：桌面全黑无图标（连"此电脑/回收站"都没有），点程序里任何恢复都无效。
原因：程序的"自动隐藏"把整个桌面图标窗口（SHELLDLL_DefView）SW_HIDE 了，
      文件级恢复（unhide/属性法）对它无效；程序重启也不会自动显示。

本脚本三层解法，全部只动可见性/属性，不删不改任何文件：
  1. 枚举所有 SHELLDLL_DefView 窗口（含 WorkerW 下的），隐藏的就 SW_SHOW；
  2. 仍不可见 → 给 Progman 发 WM_COMMAND 0x7402（= 右键桌面→查看→
     "显示桌面图标" 的那条系统命令）切换为显示；
  3. 兜底：清掉 用户桌面+公共桌面 文件的"隐藏/系统"属性
     （跳过 desktop.ini，那是系统自己的）。
  另附检查：注册表 HideIcons 开关（若被设为 1 则改回 0）。

用法：双击 rescue_desktop_icons.bat（或 python rescue_desktop_icons.py）。
等效手动操作：右键桌面空白处 → 查看 → 勾选"显示桌面图标"。
"""
import ctypes
import os
import sys
from ctypes import wintypes

u32 = ctypes.windll.user32
# ★R210（NEW4-R7 部分）：补原型声明——64 位下不声明 restype 会把
# 句柄/LRESULT 截断成 32 位 int，埋间歇性找窗失败
u32.SendMessageTimeoutW.restype = ctypes.c_void_p
u32.SendMessageTimeoutW.argtypes = [
    ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t,
    ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
    ctypes.POINTER(ctypes.c_ulong)]
u32.FindWindowW.restype = ctypes.c_void_p
u32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
try:
    u32.FindWindowExW.restype = ctypes.c_void_p
    u32.FindWindowExW.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_wchar_p, ctypes.c_wchar_p]
except AttributeError:
    pass
try:
    u32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
except AttributeError:
    pass
# ★R213（NEW5-G5）：R7 补声明漏了这对可见性判定——不声明按默认 int 收
# 句柄，64 位高位被截的句柄喂进去结果不可靠（口径不齐）
u32.IsWindow.restype = ctypes.c_bool
u32.IsWindow.argtypes = [ctypes.c_void_p]
u32.IsWindowVisible.restype = ctypes.c_bool
u32.IsWindowVisible.argtypes = [ctypes.c_void_p]
SW_SHOW = 5
WM_COMMAND = 0x0111
SHOW_DESKTOP_TOGGLE = 0x7402   # Progman "显示桌面图标" 切换命令

_found = []          # [(hwnd, visible_before)]


def _enum_shell_views():
    """收集所有桌面图标窗口。注意：SHELLDLL_DefView 是 Progman/WorkerW 的
    **子窗口**，EnumWindows（仅顶层）枚举不到——必须沿父链直查：
    ① Progman 直子；② 所有 WorkerW 的直子（壁纸引擎开启时的拓扑）。"""
    _found.clear()
    views = []
    progman = u32.FindWindowW("Progman", None)
    if progman:
        h = u32.FindWindowExW(progman, 0, "SHELLDLL_DefView", None)
        if h:
            views.append(h)
    w = u32.FindWindowExW(0, 0, "WorkerW", None)
    while w:
        h = u32.FindWindowExW(w, 0, "SHELLDLL_DefView", None)
        if h:
            views.append(h)
        w = u32.FindWindowExW(0, w, "WorkerW", None)
    for h in views:
        # ★R213（NEW5-G5）：R7 另一半——"显示桌面图标"被关时 DefView 仍
        # 可见、真正空的是其子 SysListView32；仅判 DefView 可见会把"图标
        # 依旧没有"误报成✅。可见性 = DefView 与 SysListView32 双可见。
        lv = u32.FindWindowExW(h, 0, "SysListView32", None)
        _vis = bool(u32.IsWindowVisible(h)) and (
            not lv or bool(u32.IsWindowVisible(lv)))
        _found.append((h, _vis))
    return list(_found)


def _any_visible(views):
    # ★R213（NEW5-G5）：与 _enum_shell_views 同口径——实时判 DefView +
    # SysListView32 双可见（此前只判 DefView，"显示桌面图标"关着也算✅）
    def _live(h):
        lv = u32.FindWindowExW(h, 0, "SysListView32", None)
        return bool(u32.IsWindowVisible(h)) and (
            not lv or bool(u32.IsWindowVisible(lv)))
    return any(_live(h) for h, _v in views)


def fix_window_layer():
    views = _enum_shell_views()
    if not views:
        print("  · 未找到桌面图标窗口（SHELLDLL_DefView）——可能不是本问题")
        return False
    shown = 0
    for h, vis in views:
        if not vis and u32.IsWindow(h):
            u32.ShowWindow(h, SW_SHOW)
            shown += 1
    print(f"  · 找到 {len(views)} 个桌面图标窗口，其中隐藏 {sum(1 for _h, v in views if not v)} 个，已执行显示 {shown} 个")
    if _any_visible(views):
        return True
    # SW_SHOW 无效 → 用系统"显示桌面图标"切换命令拉起来
    progman = u32.FindWindowW("Progman", None)
    if progman:
        # ★R204 ROOT-10 + ★R207 NEW3-R3：黑桌面常伴随 Explorer 假死——
        # 无超时 SendMessageW 会把救援脚本一起拖挂。标志必须用
        # SMTO_ABORTIFHUNG=0x0002（挂起立即放弃）；R204 误传 0x0008
        # =SMTO_NOTIMEOUTIFNOTHUNG，语义正好相反（挂起时永远等）
        res = u32.SendMessageTimeoutW(progman, WM_COMMAND,
                                      SHOW_DESKTOP_TOGGLE, 0,
                                      0x0002, 2000, None)   # SMTO_ABORTIFHUNG, 2s
        print("  · SW_SHOW 无效，已发系统'显示桌面图标'切换命令"
              f"（超时保护 2s，结果={res}）")
    views = _enum_shell_views()
    return _any_visible(views) if views else False


def fix_registry_layer():
    """HideIcons=1 会让 Explorer 整体不画桌面图标（与窗口隐藏表现相同）"""
    try:
        import winreg
        key_path = (r"Software\Microsoft\Windows\CurrentVersion"
                    r"\Explorer\Advanced")
        with winreg.OpenKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0,
                              winreg.KEY_READ | winreg.KEY_WRITE) as k:
            try:
                val, _t = winreg.QueryValueEx(k, "HideIcons")
            except FileNotFoundError:
                print("  · 注册表 HideIcons 未设置（正常）")
                return
            if val:
                winreg.SetValueEx(k, "HideIcons", 0, winreg.REG_DWORD, 0)
                print("  · 注册表 HideIcons=1 → 已改回 0（若仍未显示，重启一次资源管理器）")
            else:
                print("  · 注册表 HideIcons=0（正常）")
    except Exception as e:
        print(f"  · 注册表检查跳过: {e}")


def unhide_one(get_attr, set_attr, path):
    """清掉一个文件的 隐藏/系统 属性，返回**是否真的改了**。

    ★R313 G5（救援工具谎报已修，两条历轮漏口都在"没人消费结果"上）：
      · ★R213 给 `GetFileAttributesW` 补了 `restype=DWORD` 之后，失败返回值是
        `0xFFFFFFFF` 而**不再等于 -1** ⇒ 旧判据 `attr != -1` 反向变成恒真：
        属性读不出来的文件（公共桌面无权限项、listdir 与 GetAttr 之间被删的）
        照样进分支，把 `0xFFFFFFFF & ~0x6 = 0xFFFFFFFB`（含 COMPRESSED/
        ENCRYPTED/REPARSE 等根本设不了的位）提交给 `SetFileAttributesW`；
      · `SetFileAttributesW` 的返回值没人看：API 返回 FALSE 时 ctypes 不抛，
        于是 `fixed += 1` 照写、"已清隐藏属性"照打——用户以为救援成功，
        实际一个文件都没动。
    把这两条收进一个有返回值的出口，循环里只认 True。
    """
    INVALID = 0xFFFFFFFF          # restype=DWORD 下 GetFileAttributesW 的失败值
    HIDDEN, SYSTEM = 0x2, 0x4
    try:
        attr = get_attr(path)
    except Exception:
        return False
    if attr is None or attr in (INVALID, -1):
        return False              # 读不出来＝不动它，也别谎报
    if not (attr & (HIDDEN | SYSTEM)):
        return False              # 本来就没隐藏/系统属性，不白写一次
    try:
        return bool(set_attr(path, attr & ~(HIDDEN | SYSTEM)))
    except Exception:
        return False


def fix_attr_layer():
    """清掉桌面文件的 隐藏/系统 属性（跳过 desktop.ini）"""
    import winreg
    SetAttr = ctypes.windll.kernel32.SetFileAttributesW
    SetAttr.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    SetAttr.restype = wintypes.BOOL     # ★R313 G5：返回值要消费，先声明对类型
    # ★R213（NEW5-G5）：R7 漏配的一半——GetFileAttributesW 未声明 argtypes，
    # str 默认按 char*(UTF-8 字节) 喂进宽字符 API → 恒返回 -1(INVALID) →
    # `attr != -1` 恒假 → 清隐藏属性兜底全环境静默空转（与路径是否中文无关）。
    # 与同族 SetFileAttributesW 对称补声明，restype=DWORD。
    GetAttr = ctypes.windll.kernel32.GetFileAttributesW
    GetAttr.argtypes = [wintypes.LPCWSTR]
    GetAttr.restype = wintypes.DWORD
    dirs = []
    try:
        with winreg.OpenKeyEx(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer"
                r"\User Shell Folders", 0, winreg.KEY_READ) as k:
            v, _t = winreg.QueryValueEx(k, "Desktop")
            dirs.append(os.path.expandvars(v))
    except Exception:
        dirs.append(os.path.expanduser(r"~\Desktop"))
    dirs.append(r"C:\Users\Public\Desktop")
    fixed = 0
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if name.lower() == 'desktop.ini':
                continue
            p = os.path.join(d, name)
            if not os.path.isfile(p):
                continue
            try:
                if unhide_one(GetAttr, SetAttr, p):
                    fixed += 1
                    print(f"  · 已清隐藏属性: {d}\\{name}")
            except Exception:
                pass
    print(f"  · 文件隐藏属性：清了 {fixed} 个")


def main():
    print("=" * 46)
    print("桌面图标消失·一键救援")
    print("=" * 46)
    ok = fix_window_layer()
    fix_registry_layer()
    fix_attr_layer()
    print("-" * 46)
    if ok:
        print("✅ 桌面图标窗口已恢复显示。桌面还没刷出来的话，按一下 F5。")
    else:
        print("⚠ 窗口层未确认恢复。请手动：右键桌面空白处 → 查看 →")
        print("  勾选'显示桌面图标'；仍不行就重启资源管理器（任务管理器→")
        print("  找到 Windows 资源管理器→重新启动）——图标文件都还在，不会丢。")
    if os.name == 'nt' and not sys.stdout.isatty():
        pass
    try:
        input("\n按回车关闭…")
    except Exception:
        pass


if __name__ == '__main__':
    main()
