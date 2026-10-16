# -*- coding: utf-8 -*-
"""★R255 桌面快捷方式（46 批 item④-2 用户点名"把我们软件的快捷键放桌面上"）。

背景：极端情况（任务管理器强杀 + 黑桌面）下，用户需要一个**手动入口**——
桌面上有我们软件的图标，双击就能开（下次启动自愈/哨兵都会把桌面亮回来）。
口径：
- **仅打包态**创建（源码态在开发机桌面上塞快捷方式纯属污染）；
- 桌面目录走 SHGetFolderPathW(CSIDL_DESKTOPDIRECTORY)——OneDrive 重定向
  的桌面也拿对，不写死 ~/Desktop；
- WScript.Shell COM **必须在主线程**（★R142），接线点在主窗 __init__；
  （★R280-C 更正并推翻这一条：主线程调 COM 正是 R008 那条原生 AV 的病根——
   现在建链走 `create_lnk_via_child`，主进程一发 COM 都不调，接线点不变。）
- 如实告知：整层隐藏生效期间这个快捷方式**也被藏**（它是桌面图标），
  黑屏救场靠哨兵自动亮回来，快捷方式是"桌面亮着时随手点开用"的。

★R281（用户 08 批 item1「现在双击执行程序，不自动往桌面建立快捷键了」）改口径：
旧写法是**布尔记号** `desktop_shortcut_done`——第一次建过之后每次启动都在
`already` / `existing` 两条出口静默 return，于是桌面上那枚 .lnk **永远停在
第一次建它的那个版本**。真机实证（R010 日志包 + 桌面取证）：用户跑 R010，
快捷方式指的是 `deskbits_beta_0.99-R007-单目录/…R007.exe`，双击开的是老程序，
在他眼里就是"不建快捷方式了"；而这两条出口 app.log 里一个字都不留，无从归因。
⇒ 记号换成**当年建给哪个 exe**（`desktop_shortcut_target`），判据从"建过没有"
改成"这台桌面上那枚对不对"：
  · 指向本版 ⇒ 什么都不做（不拉子进程、不动桌面）；
  · 指向别处（老版本/搬过家）⇒ **原地刷新**成当前 exe，并写一条日志；
  · 桌面没有 .lnk 且本机**从没为当前 exe 建过** ⇒ 建（首启）；
  · 桌面没有 .lnk 但**为当前 exe 建过** ⇒ 认"用户自己删了"，不重塞（★R255
    桌面主权口径不变）。
"对不对"不能靠记号自证（用户手动改过目标就看不出来），所以真的去**读那枚
.lnk 的 TargetPath**（★R281-F：读也走一次性子进程沙盒，主进程仍然零 COM）。
"""
import logging
import os
import sys

logger = logging.getLogger("desktop_manager")


SHORTCUT_NAME = '拾碎-懒人桌面工具箱.lnk'


def get_desktop_dir():
    """用户桌面绝对目录（SHGetFolderPathW，兼容 OneDrive 重定向）；失败 None。"""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        CSIDL_DESKTOPDIRECTORY = 0x0000
        if ctypes.windll.shell32.SHGetFolderPathW(
                None, CSIDL_DESKTOPDIRECTORY, None, 0, buf) == 0 and buf.value:
            return buf.value
    except Exception:
        pass
    return None


def _create_lnk(lnk_path, target, work_dir, description):
    """WScript.Shell 建 .lnk。**★R280-C：只许在子进程里调用**（见 run_lnk_helper）。
    成功 True，异常不外抛。"""
    try:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        try:
            shell = win32com.client.Dispatch('WScript.Shell')
            sc = shell.CreateShortCut(lnk_path)
            sc.Targetpath = target
            sc.WorkingDirectory = work_dir
            sc.IconLocation = '%s,0' % target
            sc.Description = description
            sc.save()
            return os.path.exists(lnk_path)
        finally:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass
    except Exception:
        return False


def _lnk_target_is(lnk_path, expected):
    """读 .lnk 的 TargetPath 并判断"是不是就是 expected"。

    **★R281-F：只许在子进程里调用**（与 _create_lnk 同一条 COM 红线）。
    返回 True=已经指向本版；False=指向别处或**这条 .lnk 读不出目标**。
    后两种都要重刷，所以这里合并成一个 False；"文件根本不存在"那一路
    在建链之前就被 os.path.exists 拦掉，不会跑到这儿。"""
    try:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        try:
            shell = win32com.client.Dispatch('WScript.Shell')
            got = shell.CreateShortCut(lnk_path).Targetpath
        finally:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass
    except Exception:
        return False
    if not got:
        return False
    return os.path.normcase(os.path.abspath(got)) == \
        os.path.normcase(os.path.abspath(expected))


# ★R280-C（用户 06 批 item2「修」，N-1）：桌面快捷方式的 WScript.Shell COM 在
# **主线程**里崩过一次（R008 那条原生 AV：crash.log 有案、Python 层拦不住，
# 整个进程连带用户的悬浮栏一起没了）。COM/Shell 这类跨进程 API 的原生崩溃
# 只能靠"崩了也只死一个进程"来兜——所以把它挪进子进程：
#   · 主进程一发 COM 都不调（CoInitialize 也不调），原生 AV 打不到主窗；
#   · 子进程失败/被打死 ⇒ 父进程看到 rc!=0 或文件不存在 ⇒ 记 'failed'，
#     程序照常起（快捷方式本来就是"锦上添花"，不该有把它做成 P0 的资格）；
#   · 已有同名 .lnk 那条路在建链**之前**就返回 ⇒ 常态下一次子进程都不拉，桌面零扰动。
#     （★R281：这条"之前"现在装的是**读目标**那一步，见 ensure_desktop_shortcut。）
# 口径照 ★R255 哨兵影子模式：同一个 exe 带自己的隐藏旗标拉起，跑完自退。
# ★R281-F：沙盒多接一味"读"（op=read）。旧口径"退码即接口"只能表达成/不成，
# 而 item1 的修法要能问出"桌面上那枚到底指向谁"——读也得过 COM，主进程一发
# 都不调这条不能为了少写几行就破。退码表因此扩成两味共用（0/1 的语义按 op 分）：
#   op=create：0=建出来了 1=没建出来
#   op=read  ：0=目标**就是** expected（已是本版）1=目标是别的（要刷）
#   任何 op 抛异常/参数坏 ⇒ 2（父进程按"当没读到，宁可重刷"处理，不会谎报已是本版）
HELPER_FLAG = '--lnk-helper'
HELPER_TIMEOUT = 20          # 秒：COM 假死不拖住启动，到点算"没建成"


def run_lnk_helper(raw_payload, raw_expected=None) -> int:
    """子进程入口：解析 payload → 建链/读链 → 用退出码说话（见上面退码表）。

    这里**不 import gui、不起 Tk、不写 app.log**：它就是替主进程挨一发原生崩溃的
    一次性沙盒，越薄越可靠。"""
    try:
        import json
        p = json.loads(raw_payload)
        if p.get('op') == 'read':
            # read 那一路**不往 stdout 写任何东西**（三路 stdio 全 DEVNULL）——
            # 只回"对不对得上"一个比特，退码就是答案，够用且不引入 IPC 新通道。
            return 0 if _lnk_target_is(p['lnk'], raw_expected or '') else 1
        ok = _create_lnk(p['lnk'], p['target'], p['work'], p['desc'])
        return 0 if ok else 1
    except Exception:
        return 2


def _helper_argv(payload, expected=None):
    # 打包态：exe 路径**必须**走 app_paths.get_executable()（★R281 修掉 R174 那一坑：
    # Nuitka onefile 下 sys.executable 是缓存解压目录里的内层 exe，拿它拉子进程会随
    # 缓存生命周期漂移，数据目录算错、极端情况下 WinError 2）——哨兵那一路一直守着的
    # 口径，快捷方式这路历轮漏了。取不到才退回 sys.executable（standalone 下就是它）。
    if getattr(sys, 'frozen', False):
        try:
            from utils.app_paths import get_executable
            exe = get_executable()
        except Exception:
            exe = sys.executable
        base = [exe, HELPER_FLAG, payload]
    else:
        launcher = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), 'launcher.pyw')
        base = [sys.executable, launcher, HELPER_FLAG, payload]
    if expected is not None:
        base.append(expected)
    return base


def _spawn_helper(payload, expected=None):
    """拉一发沙盒，返回退出码（拿不到 ⇒ None，调用方按"不知道"处理）。"""
    try:
        import subprocess
        kw = {}
        if os.name == 'nt':
            kw['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        argv = _helper_argv(payload, expected)
        return subprocess.run(argv, timeout=HELPER_TIMEOUT,
                              stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, **kw).returncode
    except Exception:
        return None


def create_lnk_via_child(lnk_path, target, work_dir, description):
    """把建链那一下丢进子进程，返回 True/False（父进程零 COM）。"""
    try:
        import json
        payload = json.dumps({'op': 'create', 'lnk': lnk_path, 'target': target,
                              'work': work_dir, 'desc': description},
                             ensure_ascii=False)
    except Exception:
        return False
    rc = _spawn_helper(payload)
    return rc == 0 and os.path.exists(lnk_path)


def lnk_target_is_via_child(lnk_path, expected):
    """问一句"这枚 .lnk 是不是就指向 expected"（父进程零 COM）。

    返回 True/False；**拿不准（rc 不是 0 也不是 1）一律 False**——这条判据的真
    含义是"确认已经是本版了才不动"，读不到就该重刷，宁可多改一次桌面，
    也不许在没核实的情况下继续留着指向老版本的快捷方式（那正是 item1 的病）。"""
    try:
        import json
        payload = json.dumps({'op': 'read', 'lnk': lnk_path},
                             ensure_ascii=False)
    except Exception:
        return False
    return _spawn_helper(payload, expected) == 0


def ensure_desktop_shortcut(gui, _frozen=None, _desktop_dir=None,
                            _create=None, _check=None, _exe=None):
    """启动接线入口（返回值写 app.log 的状态行，动作另走上屏日志一条）。

    依赖可注入：_frozen=打包态模拟、_desktop_dir=桌面目录、_create=建链函数、
    _check=读目标函数、_exe=(target, work_dir) 覆盖。
    幂等闸（★R281 起）=app_settings['desktop_shortcut_target']：记的是"当年这枚
    快捷方式建给哪个 exe"，所以**换版本会刷新、删了不重塞**两件事不再互相牺牲。"""
    settings = getattr(gui, '_app_settings', None)
    if not isinstance(settings, dict):
        logger.info('[快捷方式] 结果=跳过(没有设置容器)')
        return 'no-settings'
    frozen = (getattr(sys, 'frozen', False)
              if _frozen is None else bool(_frozen))
    if not frozen:
        # 源码态不碰开发机桌面。真机装出来必须带编译标记（Nuitka 的 __compiled__
        # 由 utils/logger.py 统一补成 sys.frozen），这条判据不许放宽成"看起来像打包"。
        logger.info('[快捷方式] 结果=跳过(源码态)')
        return 'source-mode'
    ddir = (_desktop_dir or get_desktop_dir)()
    if not ddir or not os.path.isdir(ddir):
        logger.info('[快捷方式] 结果=跳过(拿不到桌面目录)')
        return 'no-desktop-dir'
    if _exe is not None:
        target, work_dir = _exe
    else:
        try:
            from utils.app_paths import get_executable, get_app_dir
            target = get_executable()
            work_dir = str(get_app_dir())
        except Exception:
            target = sys.executable
            work_dir = os.path.dirname(target)
    lnk = os.path.join(ddir, SHORTCUT_NAME)
    if os.path.exists(lnk):
        same = (_check or lnk_target_is_via_child)(lnk, target)
        if same:
            # 常态出口：桌面上那枚就是本版的，什么都不动（也不拉建链子进程）。
            # 记号缺了要补上：只补"我们建给谁"这条记录，不因此动桌面（删过就不再塞）。
            if settings.get('desktop_shortcut_target') != target:
                settings['desktop_shortcut_target'] = target
                gui._save_app_settings()
            logger.info('[快捷方式] 结果=跳过(已在且指向本版)')
            return 'current'
        logger.info(f'[快捷方式] 目标过期 → 刷新: lnk→{target}')
        if (_create or create_lnk_via_child)(lnk, target, work_dir,
                                             '拾碎-懒人桌面工具箱（DeskBits）'):
            settings['desktop_shortcut_target'] = target
            gui._save_app_settings()
            gui._log('🖥️ 桌面快捷方式已刷新为当前版本（老版本目录可删）')
            logger.info('[快捷方式] 结果=刷新成功')
            return 'refreshed'
        gui._log('⚠ 桌面快捷方式刷新失败（不影响使用，可从安装目录打开）')
        logger.warning('[快捷方式] 结果=刷新失败')
        return 'failed'
    # 桌面上没有：建过当前 exe ⇒ 用户自己删掉了，认他的主权不重塞；没建过 ⇒ 首启建。
    if settings.get('desktop_shortcut_target') == target:
        logger.info('[快捷方式] 结果=跳过(用户已删除，不重塞)')
        return 'removed-by-user'
    create = _create or create_lnk_via_child
    if create(lnk, target, work_dir, '拾碎-懒人桌面工具箱（DeskBits）'):
        settings['desktop_shortcut_target'] = target
        gui._save_app_settings()
        gui._log('🖥️ 已在桌面创建软件快捷方式「拾碎-懒人桌面工具箱」')
        logger.info('[快捷方式] 结果=新建成功')
        return 'created'
    gui._log('⚠ 桌面快捷方式创建失败（不影响使用，可从安装目录打开）')
    logger.warning('[快捷方式] 结果=新建失败')
    return 'failed'

