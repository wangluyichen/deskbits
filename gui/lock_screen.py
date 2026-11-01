# -*- coding: utf-8 -*-
"""🔒 自带锁屏画面（R160，用户定案"不要用 Windows 的，自己带个画面锁屏"）。

- 每台显示器一个全屏置顶窗（覆盖任务栏），主屏=完整界面：壁纸图片 +
  大时钟 + 解锁区；副屏=深色遮罩 + 小时钟。
- 壁纸：内置四套主题图（images/lock_wallpapers/）或用户自定义
  （config/lock_screen_image.<ext>）；PIL 缩放保比例。
- 字号：app_settings 'lock_font' 小/中/大/超大 → 时钟 40/56/72/92px。
- 解锁：设了家长密码=输密码（parental.verify）；没设=按住按钮 3 秒。
- 局限（主流同类软件相同）：Ctrl+Alt+Del 属系统安全界面无法遮挡。
- 家长模式到时自动锁屏（parental.lock_cb）与本模块同一入口。
"""
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

import tkinter as tk

from utils import i18n

logger = logging.getLogger("desktop_manager")

_FONT_CLOCK = {'小': 40, '中': 56, '大': 72, '超大': 92}
_FONT_HINT = {'小': 11, '中': 13, '大': 15, '超大': 18}

LOCK_PRESETS = {
    'dracula': '深色',
    'amoled': '暗黑',
    'light': '浅色',
    'eyecare': '护眼',
}


def resolve_lock_image(settings: dict) -> str:
    """按优先级解析锁屏图片路径：preset → 自定义 → 空串(无图)。"""
    preset = settings.get('lock_preset', '')
    if preset and preset in LOCK_PRESETS:
        from utils.app_paths import get_app_dir
        p = get_app_dir() / 'images' / 'lock_wallpapers' / f'{preset}.jpg'
        if p.is_file():
            return str(p)
        logger.warning(f'内置壁纸文件缺失: {p}')
    img = settings.get('lock_image', '')
    if img:
        _p = Path(img)
        if _p.is_file():
            return str(_p)
        if _p.name:
            from utils.app_paths import get_app_dir
            _cand = get_app_dir() / 'config' / _p.name
            if _cand.is_file():
                return str(_cand)
    return ''


_inst = None   # 当前锁屏实例（防重复弹）


def open_lock(gui):
    """打开全屏锁屏（所有入口统一走这里）。异常时回退 Win 系统锁屏。

    ★ R168b：优先复用已隐藏实例——destroy 在持 grab 的全屏窗上会挂死
    （实测），故解锁只 withdraw 不销毁，重新锁屏时 reshow 复用。"""
    global _inst
    if _inst is not None and any(w.winfo_exists() for w in _inst.wins):
        if _inst._ui_matches():
            _inst.reshow()
            return _inst
        # ★R191/R192b：密码/字号/锁屏图变了，旧解锁 UI 不能复用（见
        # _ui_matches）——销毁重建；destroy 失败退回隐藏仍复用旧 UI
        try:
            _inst.destroy()
        except Exception as e:
            logger.error(f'锁屏实例重建失败，退回隐藏复用: {e}')
            try:
                _inst.hide()
            except Exception:
                pass
        _inst = None
    try:
        _inst = LockScreen(gui)
        return _inst
    except Exception as e:
        logger.error(f"自带锁屏打开失败，回退系统锁屏: {e}")
        # ★R207 NEW3-B2：回退系统锁屏前必须撤注销守卫——系统锁屏的解锁
        # 不经过本程序，守卫不撤=「注销」整个会话隐藏（release 成死代码）
        # ★R210（NEW4-B6）：传 gui——None 会让恢复失败告警全部落空
        try:
            release_logoff_guard(gui)
        except Exception as _e:
            logger.error(f"回退前撤销注销守卫失败: {_e}")
        # ★R221（NEW7-P0-5）：原来这里 `except: pass` 吞掉一切、且不查返回值
        return system_lock_or_warn(gui, f'自带锁屏异常: {e}')


def is_locked() -> bool:
    """★R221（NEW7-P0-6）：锁屏当前是否真的盖在上面。

    全局热键由独立线程的低级钩子投递，锁屏期间照常触发；调用方（热键分发）
    必须用它做守卫，否则能从锁屏上呼出置顶快速定位框并回车启动任意程序。
    只看 `_inst` 不够——解锁后实例按 R191 保留复用，故以窗口实际映射为准。"""
    if _inst is None:
        return False
    try:
        return any(w.winfo_exists() and w.winfo_ismapped()
                   for w in getattr(_inst, 'wins', ()))
    except Exception:
        return False


def system_lock_or_warn(gui, cause: str = '') -> bool:
    """★R221（NEW7-P0-5）：回退到 Windows 系统锁屏，**必须查返回值**。

    LockWorkStation 返回 BOOL：组策略禁用/远程会话下常返回 0。原实现既不查
    返回值、外面又套一层 `except: pass`，结果是"两把锁都没锁上"却只在
    app.log 留一行——用户以为自己已锁屏就离开了座位（安全语义上的假成功）。
    失败必须打到日志页并弹窗。"""
    import ctypes
    ok, detail = False, ''
    try:
        ok = bool(ctypes.windll.user32.LockWorkStation())
        if not ok:
            detail = f'GetLastError={ctypes.windll.kernel32.GetLastError()}'
    except Exception as e:
        detail = f'{type(e).__name__}: {e}'
    if ok:
        return True
    msg = i18n.tr('⚠ 自带锁屏与系统锁屏均失败，桌面【未锁定】！',
                  '⚠ Both the built-in lock and the Windows lock failed — '
                  'the desktop is NOT locked!')
    logger.error(f'{msg} {cause} {detail}'.strip())
    try:
        gui._log(msg)
    except Exception:
        pass
    try:
        from tkinter import messagebox
        messagebox.showerror(
            i18n.tr('锁屏失败', 'Lock failed'),
            msg + '\n\n' + i18n.tr(
                '请改用 Win+L；若仍无效，检查组策略是否禁用了锁定工作站。',
                'Use Win+L instead; if that also fails, check Group Policy '
                'for a disabled workstation lock.'),
            parent=getattr(gui, 'root', None))
    except Exception:
        pass
    return False


def close_all():
    """解锁收尾：停计时器+隐藏（不销毁——destroy 在持 grab 的全屏窗上
    会挂死，R168b）。

    ★R191（Qoder GUIB-2 泄漏修复）：不再把 _inst 置 None——原实现让
    open_lock 的 reshow 复用分支永远走不到，每轮锁/解都新建整套窗口
    +_tick/_breath 计时器链并在隐形窗上永续空转。实例保留，下次锁屏
    reshow 复用；密码状态变了由 open_lock 负责销毁重建。"""
    global _inst
    if _inst is not None:
        _inst.hide()


# ---- 锁屏独立密码（R170 改内存存储：重启自动清除，每次重设——
# 用户定案"重启一次软件自动清除，这样每次重启软件都重设密码，稳当"）。
# 密保（parental.json）仍持久化，作为忘记密码时的找回通道。

_own_pwd = None   # (salt, hash) 元组，内存仅此一份


def has_own_password():
    return _own_pwd is not None


def set_own_password(pwd: str):
    global _own_pwd
    import hashlib
    import secrets
    salt = secrets.token_hex(8)
    _own_pwd = (salt, hashlib.sha256((salt + pwd).encode('utf-8')).hexdigest())


def verify_own_password(pwd: str) -> bool:
    if _own_pwd is None:
        return False
    import hashlib
    salt, stored = _own_pwd
    return hashlib.sha256((salt + pwd).encode('utf-8')).hexdigest() == stored


def clear_own_password():
    global _own_pwd
    _own_pwd = None


# ---- 注销守卫（R166，用户定案"锁定时把注销去掉更安全"）----
# HKCU Policies\Explorer\NoLogoff=1 隐藏开始菜单"注销"；原状态写
# config/lock_guard_state.json，解锁恢复；程序启动检测残留自动恢复
# （崩溃自恢复，参照 ComputerLock.RecoverFromCrash）。

_GUARD_KEY = 'Software\\Microsoft\\Windows\\CurrentVersion\\Policies\\Explorer'
_GUARD_FILE = None   # 延迟初始化（依赖 app_paths）


def _guard_file():
    global _GUARD_FILE
    if _GUARD_FILE is None:
        from utils.app_paths import get_app_dir
        _GUARD_FILE = get_app_dir() / 'config' / 'lock_guard_state.json'
    return _GUARD_FILE


def _reg_write_nologoff(value) -> bool:
    """写 NoLogoff。★R210（NEW4-B1）：内部吞异常返 bool——启动孤儿清扫
    等无 try 上下文的调用点（main_window 构造链）不再被杀软注册表防护
    拦截时炸穿 DesktopManager.__init__=每次启动崩死循环。"""
    try:
        import win32api
        import win32con
        key = win32api.RegCreateKey(win32con.HKEY_CURRENT_USER, _GUARD_KEY)
        try:
            win32api.RegSetValueEx(key, 'NoLogoff', 0, win32con.REG_DWORD,
                                   int(value))
        finally:
            win32api.RegCloseKey(key)
        return True
    except Exception as e:
        logger.warning(f'NoLogoff 写入失败({value}): {e}')
        return False


def _reg_del_nologoff() -> bool:
    """删除 NoLogoff（恢复注销入口）。★R207 实测（开发机火绒）：
    win32api 的 RegDeleteValue 会被安全软件按进程拦截（拒绝访问），
    但 reg.exe 是系统工具不受限——三层兜底：win32api 删除 → reg.exe
    删除 → 写 0（REG_DWORD 0=策略显式禁用，功能上等同于值不存在）。
    此前删除失败被静默吞掉 = release 假成功 = 注销永久隐藏。
    ★R210（NEW4-B7/B9）：值本就不存在=已恢复直接 True（不再"凭空建
    键写 0+假拦截日志"）；第三层写 0 仅值仍≥1 时执行；reg.exe 超时
    15s→3s（正常 <200ms，别把解锁回调卡成假死）。"""
    import subprocess
    import win32api
    import win32con
    _ex, _v0 = _reg_read_nologoff()
    if not _ex:
        return True
    try:
        key = win32api.RegOpenKey(win32con.HKEY_CURRENT_USER, _GUARD_KEY)
        try:
            win32api.RegDeleteValue(key, 'NoLogoff')
        finally:
            win32api.RegCloseKey(key)
        return True
    except Exception:
        pass
    try:
        _r = subprocess.run(
            ['reg', 'delete',
             'HKCU' + chr(92) + _GUARD_KEY, '/v', 'NoLogoff', '/f'],
            capture_output=True, timeout=3, creationflags=0x08000000)
        if _r.returncode == 0:
            return True
    except Exception:
        pass
    _ex2, _v2 = _reg_read_nologoff()
    if not _ex2 or _v2 == 0:
        # ★R213（NEW5-B3）：CSV 点名 5 处之一——"<1"只认 0/负数漏了
        # 正数误删后回读，且与全局 !=0 口径分叉；"已恢复"=不存在或恰为 0
        return True   # reg.exe 实际删掉了（rc 误报）或值已为 0
    try:
        key = win32api.RegCreateKey(win32con.HKEY_CURRENT_USER, _GUARD_KEY)
        try:
            win32api.RegSetValueEx(key, 'NoLogoff', 0,
                                   win32con.REG_DWORD, 0)
        finally:
            win32api.RegCloseKey(key)
        logger.warning('NoLogoff 删除被拦截，已改写为 0（等效恢复注销入口）')
        return True
    except Exception as e:
        logger.error(f'NoLogoff 删除/置零全部失败: {e}')
        return False


_LOGOFF_LABEL = None   # ★R210（NEW4-B13）：缓存一次，不每次弹提示都查


def _logoff_label():
    """Windows 对"注销"条目的系统默认叫法：win11（build 22000+）=
    "退出登录"，win10 及更早="注销"。★R209b（用户 11 批）：提示文案
    跟随系统默认叫法，不硬写"注销"。"""
    global _LOGOFF_LABEL
    if _LOGOFF_LABEL is None:
        try:
            import sys as _sys
            _LOGOFF_LABEL = ('退出登录'
                             if _sys.getwindowsversion().build >= 22000
                             else '注销')
        except Exception:
            _LOGOFF_LABEL = '注销'
    return _LOGOFF_LABEL


def _reg_read_nologoff():
    """读当前 NoLogoff 值 → (existed, value)；读失败返回 (False, None)。
    ★R207-3f：release 的读回校验与启动残留清扫共用。"""
    try:
        import win32api
        import win32con
        key = win32api.RegOpenKey(win32con.HKEY_CURRENT_USER, _GUARD_KEY)
        try:
            v, _t = win32api.RegQueryValueEx(key, 'NoLogoff')
            return True, int(v)
        except Exception:
            return False, None
        finally:
            win32api.RegCloseKey(key)
    except Exception:
        return False, None


def engage_logoff_guard(gui):
    """锁定时隐藏"注销"；原状态持久化。★R204 NEW-B3：幂等——状态文件
    已存在说明守卫已在位（锁中再锁），不重读注册表不覆盖账本：否则第二
    次会把"自己写的 1"当原值记账，解锁后按账本把 NoLogoff 恢复成 1，
    开始菜单注销被永久隐藏且状态文件已删无从补救。
    ★R207 NEW3-B3/B10：幂等分支的注册表写也要 try；账本写盘失败时
    **撤销隐藏并放弃 engage**——幂等的前提是"账本一定写成功"，写失败
    还继续=下一次锁中再锁必然记错原值。"""
    gf = _guard_file()
    if gf.exists():
        # ★R207-3f：账本在≠守卫真在——上轮 release 删除失败（win11 实锤
        # 用户"解锁后注销没了"）时账本已 unlink 但值残留，下一轮 engage
        # 又把 1 当原值记进账本=永久隐藏的毒账本。两种毒账本都推翻：
        # ①值不在/为 0（守卫实际没在位）；②账本说"原值≥1"（恢复目标
        # 是 1=解锁后仍然隐藏，违背守卫本意，只可能是残留）。
        _ex, _v = _reg_read_nologoff()
        _stale = (not _ex or _v == 0)
        try:
            import json as _pj
            _d = _pj.loads(gf.read_text(encoding='utf-8'))
            if _d.get('existed') and int(_d.get('old_value') or 0) != 0:
                # ★R213（NEW5-B3）：pywin32 某些版本把 DWORD 回读成带符号
                # 负数（0xFFFFFFFF=-1，语义=隐藏），≥1 判定漏负数——
                # "在位"口径全局统一为非 0
                _stale = True
        except Exception:
            pass
        if _stale:
            logger.warning('注销守卫账本异常（值不在位或记账目标=仍隐藏），'
                           '推翻重建')
            try:
                gf.unlink()
            except OSError:
                pass
        else:
            # ★R210（NEW4-B1）：_reg_write_nologoff 已改返 bool
            if not _reg_write_nologoff(1):   # 确保仍隐藏（幂等），账本不动
                logger.warning('注销守卫重写失败（不影响账本）')
            else:
                # ★R213（NEW5-B1）：幂等分支同样补标记——旧版本此处
                # 不写标记，后续清扫会把我们的残留判成"他人所写"保留
                # ★R215（NEW6-E1）：函数名写反（_our_marker_write 不存在）——
                # 幂等重入分支（锁中再锁 / 上轮 release 被拦截后每次点锁屏）
                # 必抛 NameError，且它在 engage 调用链上＝**锁屏直接失败**。
                _write_our_marker()
            return
    old_val = None
    existed = False
    try:
        import win32api
        import win32con
        key = win32api.RegOpenKey(win32con.HKEY_CURRENT_USER, _GUARD_KEY)
        try:
            old_val, _t = win32api.RegQueryValueEx(key, 'NoLogoff')
            # ★R210（NEW4-B8）：REG_SZ/BINARY 形态（GPO 扩展/第三方脚本
            # 常写）转 int 会炸——转换并入读 try，失败按"不存在"记账
            old_val = int(old_val)
            existed = True
        except Exception:
            # ★R213（NEW5-B2）：NEW4-B8 只做了"按不存在记账"的一半——
            # except 里 old_val 仍绑着原始对象（REG_SZ 字符串会直接写进
            # 账本 old_value；REG_BINARY bytes 让 json.dump 炸穿账本写
            # 盘）。"转换失败=不存在"必须两个字段一起归零。
            old_val = None
            existed = False
        finally:
            win32api.RegCloseKey(key)
    except Exception as e:
        logger.warning(f'注销守卫读取原状态失败: {e}')
    # ★R207：删除兜底会留下 NoLogoff=0（REG_DWORD 0=策略禁用，功能上
    # 等同值不存在）——按"不存在"记账，否则解锁 release 会把 0 当原值
    # 回写，账本里 old_value=0 恒假 existed，下次锁中再锁记错原值。
    # ★R207-3f（win11"解锁后注销永久消失"根治）：原值非 0 也一律按"无
    # 原值"记账（absorb）——NoLogoff=1 只可能是本软件上次没恢复干净
    # 的残留（正常机器不会有这个策略），把它记成恢复目标=解锁后照样
    # 隐藏=毒循环。absorb 后解锁直接删除，循环在根上断掉。
    # ★R213（NEW5-B3）：absorb/毒账本/删除/清扫判定统一 !=0（负数=
    # 0xFFFFFFFF 也是"在位"，≥1 会漏）
    if existed and (old_val is None or old_val != 0):
        if old_val is not None and old_val != 0:
            logger.warning(f'engage 时 NoLogoff 已={old_val}（上次残留），'
                           '按无原值记账，解锁将直接删除')
        existed = False
        old_val = None
    if not _reg_write_nologoff(1):
        logger.error('注销守卫写入失败（放弃隐藏，锁屏继续）')
        return
    try:
        from utils.safeio import atomic_write_json
        if not atomic_write_json(_guard_file(), {'existed': existed,
                                                 'old_value': old_val}):
            raise OSError('账本写盘失败')
    except Exception as e:
        # ★R207 NEW3-B3：账本写不进去=下次 engage 必记错原值——宁可这次
        # 不隐藏注销，也不能埋"解锁后注销永久消失"的雷
        logger.error(f'注销守卫账本持久化失败，已撤销隐藏: {e}')
        try:
            _reg_del_nologoff()
        except Exception:
            pass
        try:
            gui._log(f'⚠ 守卫状态保存失败，本次不隐藏"{_logoff_label()}"入口')
        except Exception:
            pass
        return
    # ★R213（NEW5-B1）：标记写在账本成功之后（此时是我们真正持有
    # 隐藏的时点）；helper 内部已含 mkdir+失败留痕（原裸 except OSError
    # 静默——标记缺写=下轮清扫误判）
    _write_our_marker()
    gui._log(f'🔒 已隐藏系统"{_logoff_label()}"入口（解锁自动恢复）')


def release_logoff_guard(gui=None):
    """解锁恢复"注销"；残留状态文件一并清理。
    ★R207-3f（win11 用户实锤"解锁后注销没了"）：恢复后必须**读回校验**
    ——此前删除失败被吞、账本已删无从重试，下一轮 engage 还把 1 记成
    原值=永久隐藏。现在：恢复动作后读回，值仍在（≥1）就再兜底一轮
    （再删→写 0），仍失败则大声告警并**保留账本**（下次启动清扫兜底）；
    成功才删账本。"""
    gf = _guard_file()
    ledger = None
    try:
        if gf.exists():
            import json as _json
            ledger = _json.loads(gf.read_text(encoding='utf-8'))
            if ledger.get('existed'):
                # ★R210（NEW4-C5）：毒账本归一——恢复目标≥1=解锁后仍然
                # 隐藏，违背守卫本意（R204-R207 存量账本），按删除处理。
                # 此前 release 按毒账本回写 1、读回相符还报"恢复成功"。
                try:
                    _ov = int(ledger.get('old_value') or 0)
                except (TypeError, ValueError):
                    _ov = 0
                if _ov != 0:   # ★R213（NEW5-B3）：非 0 即毒（含负数）
                    logger.warning('release 读到毒账本（恢复目标非 0），'
                                   '按删除处理')
                    ledger['existed'] = False
                    ledger['old_value'] = None
                    _reg_del_nologoff()
                else:
                    _reg_write_nologoff(_ov)
            else:
                _reg_del_nologoff()
        else:
            _reg_del_nologoff()
    except Exception as e:
        logger.warning(f'注销守卫恢复失败: {e}')
    # 读回校验（账本说 existed=True 时按原值核验；否则应为 0/不存在）
    try:
        _ex, _v = _reg_read_nologoff()
        _want = int((ledger or {}).get('old_value') or 0) \
            if (ledger or {}).get('existed') else 0
        # ★R213（NEW5-B3）：读回"在位"判定同样 !=0（负数=0xFFFFFFFF 漏判）
        if _ex and _v != _want and _v != 0:
            logger.error(f'注销守卫恢复读回不符（现={_v} 应={_want}），'
                         '再兜底一轮')
            _reg_del_nologoff()
            _ex, _v = _reg_read_nologoff()
            if _ex and _v != 0:
                _reg_write_nologoff(0)
                _ex, _v = _reg_read_nologoff()
            if _ex and _v != 0:
                logger.error('注销守卫兜底后 NoLogoff 仍非 0（注册表被'
                             '安全软件锁死？）——保留账本，下次启动再清')
                if gui:
                    try:
                        gui._log(f'⚠ "{_logoff_label()}"入口恢复失败（被安全软件拦截？）'
                                 '已记录，下次启动自动再清')
                    except Exception:
                        pass
                return False
        if gf.exists():
            gf.unlink()
        # ★R213（NEW5-B1）：恢复到位=标记生命周期终点，随账本一并删除
        # （上方兜底失败分支已 return，走到这里值确认不再非 0）
        _clear_our_marker()
        if gui:
            gui._log(f'🔓 系统"{_logoff_label()}"入口已恢复')
    except Exception as e:
        logger.warning(f'注销守卫读回校验失败: {e}')
        return False
    # ★R213（NEW5-B6）：release 返回真实结果——成功清账=True；此前恒返
    # None，调用方 recover_guard_on_startup 无论成败都打"已自动恢复"，
    # 恢复失败被谎报成成功（正好是本次启动清扫想修的假象）
    return True


def _our_marker_file():
    """★R210（NEW4-B2 轻量版）：所有权标记——本软件写 NoLogoff=1 时
    落一个标记文件；启动孤儿清扫只清有标记的机器（无标记而值≥1 可能
    是域 GPO/共享机策略/第三方锁机写的，保留不动）。"""
    global _OUR_MARKER
    if _OUR_MARKER is None:
        from utils.app_paths import get_app_dir
        _OUR_MARKER = get_app_dir() / 'config' / 'logoff_guard.ours'
    return _OUR_MARKER


_OUR_MARKER = None


def _write_our_marker():
    """★R213（NEW5-B1）：统一写所有权标记（幂等）。engage 两条写 1 的
    分支（全新 + 幂等重写）都调用它，保证只要我们把值置 1 就有标记，
    后续启动清扫不会把我们自己的残留误判为'他人所写'而保留。失败仅
    告警（标记是加强判据，缺了最坏是漏清我们自己的残留，不伤用户）。"""
    try:
        mf = _our_marker_file()
        mf.parent.mkdir(parents=True, exist_ok=True)
        mf.write_text('written by DeskBits lock_screen', encoding='utf-8')
    except Exception as _e:
        logger.warning(f'写 logoff_guard 所有权标记失败: {_e}')


def _clear_our_marker():
    """★R213（NEW5-B1）：标记生命周期收口——注销入口已确认恢复（值不在
    位/已删），标记随之删除；否则标记会永久残留，将来第三方/GPO 真写
    NoLogoff=1 时会被我们误认作自己的残留清掉（撤销他人策略）。"""
    try:
        _our_marker_file().unlink(missing_ok=True)
    except Exception as _e:
        logger.warning(f'删除 logoff_guard 所有权标记失败: {_e}')


def recover_guard_on_startup(gui):
    """程序启动时检测锁屏守卫残留（上次进程被杀没恢复）→ 自动恢复。
    ★R207-3f（win11 实锤"解锁后注销永久消失"）：账本不在但 NoLogoff
    仍=1 也算残留（旧版删除失败被吞的遗留，或账本丢了的孤儿值）——
    一并清扫。本软件是用户机上唯一写 NoLogoff 的程序，误伤面=零；
    真有域策略的机器会再被锁屏重新记账恢复。"""
    gf = _guard_file()
    if gf.exists():
        # ★R213（NEW5-B6）：按 release 真实返回值区分记账——恢复失败
        # 不能再打"已自动恢复"
        if release_logoff_guard(gui):
            logger.warning('检测到锁屏守卫残留（上次可能被强杀），已自动恢复注销入口')
        else:
            logger.error('检测到锁屏守卫残留，自动恢复未成功'
                         '（注册表被拦截？）——账本已保留，下次启动再清')
        return
    _ex, _v = _reg_read_nologoff()
    if _ex and _v != 0:   # ★R213（NEW5-B3）：非 0 即在位（含负数）
        # ★R210（NEW4-B2 轻量版）：无所有权标记而值≥1 → 可能是域策略/
        # 第三方软件所写，保留不动（避免静默撤销他人策略）
        if not _our_marker_file().exists():
            logger.info('检测到 NoLogoff=1 但无本软件所有权标记'
                        '（可能是系统策略/其他软件所写），保留不动')
            # ★R213（NEW5-B1）：清扫拒绝此前只进日志，用户视角"注销
            # 消失且软件装死"——可见提示说明为什么不动
            try:
                gui._log(f'检测到"{_logoff_label()}"入口被隐藏，但非本软件'
                         '所写（无所有权标记，可能来自系统策略/其他软件），'
                         '已保留不动')
            except Exception:
                pass
            return
        # ★R210（NEW4-B1）：清扫段整包 try——此前裸 _reg_write_nologoff(0)
        # 被杀软拦即抛穿 DesktopManager.__init__=启动崩死循环且不自愈。
        # 失败只告警绝不影响启动。
        try:
            _reg_del_nologoff()
            _ex2, _v2 = _reg_read_nologoff()
            if _ex2 and _v2 != 0 and not _reg_write_nologoff(0):
                logger.error('注销入口残留无法清除（杀软注册表防护？）——'
                             '不影响启动；下次锁屏解锁会再试')
                return
            logger.warning('检测到 NoLogoff=1 残留（上轮恢复失败的孤儿值），'
                           '已清除，注销入口恢复')
            # ★R213（NEW5-B1）：清扫成功 = 我们的残留已归零，标记随删
            _clear_our_marker()
            try:
                gui._log(f'🔓 检测到上次未恢复的"{_logoff_label()}"隐藏，'
                         '已自动恢复')
            except Exception:
                pass
        except Exception as e:
            logger.error(f'注销残留清扫失败（不影响启动）: {e}')


def _font_scale(settings):
    return settings.get('lock_font', '中')


class LockScreen:
    """全屏锁屏：每显示器一窗，主屏带解锁区。"""

    def __init__(self, gui):
        self.gui = gui
        # ★R190（Qoder GUIB-1 P0）：无条件先建——无密码路径此前不创建
        # 本变量，解锁收尾 hide() 里 pwd_var.set('') 即 AttributeError
        # → 解锁失败永久锁死
        self.pwd_var = tk.StringVar()
        self.wins = []
        self._photo = None          # PhotoImage 引用（防 GC）
        # ★R191（GUIB-2）：单 job 引用——原 _tick_ids/_breath_jobs 列表
        # 每秒 append 且永不清理（一小时膨胀 3600 项），destroy 也没法精简
        self._tick_job = None
        self._breath_job = None
        self._hold_job = None
        self._hold_left = 0
        self._entry = None          # 密码框（多屏聚焦/reshow 复用用）
        self._strip = None          # 呼吸灯条（reshow 重启动画用）
        import win32api
        # ★R192（用户 win11 双屏实测）：解锁区放 Windows 认定的主屏
        # （MONITORINFOF_PRIMARY 标志）——原按"面积最大"排序，副屏更大时
        # 密码框跑到副屏。
        # ★R192b 修正：GetMonitorInfo 返回的是 dict（本机实测 type=dict），
        # R192 误写成 tuple 检查导致标志永远检不到=修复未生效（win11 17 批
        # 实锤"还是在副屏"）。dict/tuple 两种形态都认。
        monitors = []
        primary_idx = -1
        for i, m in enumerate(win32api.EnumDisplayMonitors(None, None)):
            monitors.append(m[2])
            try:
                info = win32api.GetMonitorInfo(m[0])
                midict = None
                if isinstance(info, dict):
                    midict = info
                elif isinstance(info, tuple) and info and \
                        isinstance(info[-1], dict):
                    midict = info[-1]
                if midict is not None and midict.get('Flags', 0) & 1:
                    primary_idx = i
            except Exception:
                pass
        if not monitors:
            monitors = [(0, 0, 1920, 1080)]
        elif primary_idx > 0:
            monitors.insert(0, monitors.pop(primary_idx))
        elif primary_idx < 0:
            # 无主屏标志（罕见）：退回面积最大当主屏
            monitors.sort(key=lambda r: (r[2] - r[0]) * (r[3] - r[1]),
                          reverse=True)
        # ★R221（NEW7-P1-8）：EnumDisplayMonitors 给的是**物理像素**，Tk 的
        # geometry 吃的是本进程 DPI 感知档下的**逻辑像素**——125% 缩放下直接把
        # 物理矩形塞进 geometry，锁屏窗比屏幕大 25%、副屏锁窗错位（用户那台
        # 125% 的 win11 正是这个口径）。与 R219 方框同一收口：过 to_tk_rect；
        # 100%/PMv2 下比例恒为 1，零行为改变。
        try:
            from core.dpi_geom import to_tk_rect
            _sw = gui.root.winfo_screenwidth()
            monitors = [to_tk_rect(m, _sw) for m in monitors]
        except Exception:
            logger.warning('锁屏 DPI 换算失败，按物理像素使用', exc_info=True)
        scale = _font_scale(gui._app_settings)
        self._clock_px = _FONT_CLOCK.get(scale, 56)
        self._hint_px = _FONT_HINT.get(scale, 13)

        img_path = resolve_lock_image(gui._app_settings)
        self._img = self._load_image(img_path)

        self._breath_job = None
        # ★R204 NEW-B4：构建段全程自毁保护——中途抛 TclError（副屏建窗
        # 失败等）时已 deiconify 的半成品全屏黑窗若无引用又没销毁，会
        # 一直盖着桌面吞输入，close_all 够不着；回退 LockWorkStation 后
        # 用户面对的是孤儿窗。失败先自毁再上抛。
        try:
            for i, (l, t, r, b) in enumerate(monitors):
                win = tk.Toplevel(gui.root)
                # ★ R168b：标准全屏序列——此前 geometry 在"未映射+overrideredirect"
                # 的窗上被 Windows 忽略，锁窗实际是 1x1 隐形窗+grab_set 吞输入
                #（用户实测"输入 1234 解锁不了"实锤；复现 geometry=1x1+0+0）。
                # withdraw → 配置 → geometry → deiconify 才能保证全屏生效。
                win.withdraw()
                win.overrideredirect(True)
                win.attributes('-topmost', True)
                win.configure(bg='#101018')
                win.geometry(f'{r - l}x{b - t}+{l}+{t}')
                # ★ R164：顶部 4px"呼吸灯"条（已锁定可感知，参照 ComputerLock）
                strip = tk.Frame(win, bg='#8be9fd', height=4)
                strip.pack(side=tk.TOP, fill=tk.X)
                self.wins.append(win)
                if i == 0:
                    self._strip = strip
                    self._breath(strip)
                    self._build_primary(win, w=r - l, h=b - t)
                else:
                    self._build_secondary(win)
                win.deiconify()
                win.update_idletasks()
            for win in self.wins:
                win.protocol('WM_DELETE_WINDOW', lambda: None)   # 禁关闭
            # ★R191（多屏密码输入修复，用户两屏实测"不能输入密码"）：
            # grab 只挂密码框所在的 wins[0]。此前每块屏的窗都 grab_set()——
            # Tk 的 grab 后调顶掉先调，最终挂在最后一块（最小的）副屏窗上，
            # 主屏密码框点不进也打不进字。单屏只有一窗，不受影响。
            try:
                self.wins[0].grab_set()
            except Exception:
                pass
            try:
                self.wins[0].lift()
                self.wins[0].focus_force()
            except Exception:
                pass
            # deiconify 副屏可能抢走前台，最后再把焦点钉回密码框
            if self._entry is not None and self._entry.winfo_exists():
                try:
                    self._entry.focus_set()
                except Exception:
                    pass
            self._tick()
        except Exception:
            for win in getattr(self, 'wins', []):
                try:
                    win.grab_release()
                except Exception:
                    pass
                try:
                    win.destroy()
                except Exception:
                    pass
            self.wins = []
            raise
        self._ui_sig0 = self._ui_sig()   # ★R192b：记录构建时状态指纹

    # ---------- UI ----------

    def _build_primary(self, win, w, h):
        holder = tk.Frame(win, bg='#101018')
        holder.place(relx=0.5, rely=0.42, anchor=tk.CENTER)
        if self._img is not None:
            tk.Label(holder, image=self._img, bg='#101018').pack(pady=(0, 14))
        else:
            tk.Label(holder, text='🔒', bg='#101018', fg='#8be9fd',
                     font=('Segoe UI Emoji', max(48, self._clock_px // 2))
                     ).pack(pady=(0, 10))
        self.clock_var = tk.StringVar(value=datetime.now().strftime('%H:%M:%S'))
        tk.Label(holder, textvariable=self.clock_var, bg='#101018',
                 fg='#f8f8f2',
                 font=('Consolas', self._clock_px, 'bold')).pack()
        tk.Label(holder,
                 text=datetime.now().strftime('%Y-%m-%d %A') + '   ·   ' +
                 i18n.tr('拾碎-懒人桌面工具箱', 'DeskBits'),
                 bg='#101018', fg='#6272a4',
                 font=('Microsoft YaHei UI', self._hint_px)).pack(pady=(4, 0))

        # 解锁区（底部）
        unlock = tk.Frame(win, bg='#101018')
        unlock.place(relx=0.5, rely=0.88, anchor=tk.CENTER)
        self.msg_var = tk.StringVar(value='')
        # ★ R160 解锁优先级：独立锁屏密码 → 家长密码 → 无密码时按住 3 秒
        has_pwd = has_own_password()
        self._pwd_kind = 'own' if has_pwd else 'parental'
        if not has_pwd:
            try:
                has_pwd = self.gui.parental.has_password()
            except Exception:
                has_pwd = False
        if has_pwd:
            # ★ R194：复用 __init__ 已建的 pwd_var（GUIB-1），这里不再
            # new——旧实例被丢弃时孤儿 Tk 变量在 Py3.14 的注销时机不稳
            # （root.destroy 报 "can't delete Tcl command" 的候选源），
            # 且语义上完全没必要
            self.pwd_var.set('')
            ent = tk.Entry(unlock, textvariable=self.pwd_var, show='●',
                           width=20, justify=tk.CENTER,
                           bg='#1f2030', fg='#f8f8f2',
                           insertbackground='#f8f8f2', relief=tk.FLAT,
                           highlightthickness=1, highlightcolor='#8be9fd',
                           highlightbackground='#44475a',
                           font=('Microsoft YaHei UI', self._hint_px + 3))
            ent.pack(pady=(0, 6))
            ent.focus_set()
            self._entry = ent   # ★R191：多屏 grab/focus 与 reshow 都要用
            ent.bind('<Return>', lambda e: self._try_password())
            # ★ R164：输入即校验（参照 ComputerLock）——不等回车，输对立即解锁
            ent.bind('<KeyRelease>', lambda e: self._try_password(silent=True))
            row = tk.Frame(unlock, bg='#101018')
            row.pack()
            tk.Button(row, text=i18n.tr('解 锁', 'Unlock'), width=10,
                      bg='#8be9fd', fg='#101018', relief=tk.FLAT,
                      font=('Microsoft YaHei UI', self._hint_px, 'bold'),
                      command=self._try_password).pack(side=tk.LEFT, padx=4)
            # ★ R163：忘记密码 → 家长密保找回（用户 -15/6 定案联动）
            tk.Button(row, text=i18n.tr('❓ 忘记密码？', '❓ Forgot?'),
                      width=10, bg='#44475a', fg='#f8f8f2', relief=tk.FLAT,
                      font=('Microsoft YaHei UI', self._hint_px),
                      command=self._forgot_flow).pack(side=tk.LEFT, padx=4)
        else:
            # 未设密码：按住 3 秒解锁（防孩子误触）
            self.hold_var = tk.StringVar(
                value=i18n.tr('按住下方按钮 3 秒解锁', 'Hold the button for 3s to unlock'))
            tk.Label(unlock, textvariable=self.hold_var, bg='#101018',
                     fg='#6272a4',
                     font=('Microsoft YaHei UI', self._hint_px)).pack(pady=(0, 6))
            self.hold_btn = tk.Button(
                unlock, text=i18n.tr('按住解锁', 'Hold to unlock'),
                width=16, bg='#44475a', fg='#f8f8f2', relief=tk.FLAT,
                font=('Microsoft YaHei UI', self._hint_px, 'bold'))
            self.hold_btn.pack()
            self.hold_btn.bind('<ButtonPress-1>', self._hold_start)
            self.hold_btn.bind('<ButtonRelease-1>', lambda e: self._hold_stop())
        self.msg = tk.Label(unlock, textvariable=self.msg_var, bg='#101018',
                            fg='#ff5555',
                            font=('Microsoft YaHei UI', self._hint_px))
        self.msg.pack(pady=(6, 0))

    def _build_secondary(self, win):
        v = tk.StringVar(value=datetime.now().strftime('%H:%M'))
        tk.Label(win, textvariable=v, bg='#101018', fg='#44475a',
                 font=('Consolas', 28, 'bold')).place(relx=0.5, rely=0.5,
                                                      anchor=tk.CENTER)
        self._sec_vars = getattr(self, '_sec_vars', []) + [v]

    def _load_image(self, path):
        """PIL 载入锁屏图片，等比缩到高≈45% 主屏。失败返回 None（用 🔒）。
        ★ R176 修复：此前取 self.wins[0] 当屏幕参照，而本函数在锁屏窗
        循环创建**之前**被调（wins 还是空表 → IndexError → 永远降级🔒，
        用户 09:33 日志三连实锤）；改用主窗屏幕高度，不再依赖 wins。"""
        if not path:
            return None
        try:
            from PIL import Image, ImageTk
            mh = self.gui.root.winfo_screenheight()
            im = Image.open(path)
            ratio = min((mh * 0.45) / im.height, 1.6)
            im = im.resize((max(1, int(im.width * ratio)),
                            max(1, int(im.height * ratio))),
                           Image.LANCZOS)
            ph = ImageTk.PhotoImage(im)
            self._photo = ph
            return ph
        except Exception as e:
            logger.warning(f"锁屏图片载入失败（改用默认🔒）: {e}")
            return None

    def _breath(self, strip, step=0):
        """顶部呼吸灯渐变动画（R164）"""
        if not strip.winfo_exists():
            return
        colors = ['#8be9fd', '#57c7e8', '#2f9fc4', '#57c7e8']
        strip.configure(bg=colors[step % len(colors)])
        self._breath_job = strip.after(600, lambda: self._breath(strip, step + 1))

    # ---------- 时钟 ----------

    def _tick(self):
        now = datetime.now()
        if getattr(self, 'clock_var', None):
            try:
                self.clock_var.set(now.strftime('%H:%M:%S'))
            except Exception:
                return
        for v in getattr(self, '_sec_vars', []):
            try:
                v.set(now.strftime('%H:%M'))
            except Exception:
                pass
        if any(w.winfo_exists() for w in self.wins):
            self._tick_job = self.wins[0].after(1000, self._tick)

    # ---------- 解锁 ----------

    def _forgot_flow(self):
        """❓ 忘记锁屏密码：走家长模式密保找回（用户 -15/6 定案联动）——
        密保答对 = 清除锁屏独立密码并解锁（下次锁屏按住 3 秒或重设密码）。"""
        from tkinter import simpledialog
        gui = self.gui
        try:
            qs = gui.parental.get_questions()
        except Exception:
            qs = []
        if not qs:
            self.msg_var.set(i18n.tr(
                '未设置密保，无法自助找回（可用"清空全部数据"重置）',
                'No security questions set; use "Clear All Data" to reset'))
            return
        answers = []
        for q in qs:
            a = simpledialog.askstring(i18n.tr('密保问题', 'Security Question'),
                                       q, show='', parent=self.wins[0])
            if a is None:
                return
            answers.append(a)
        try:
            ok = gui.parental.verify_answers(answers)
        except Exception:
            ok = False
        if ok:
            clear_own_password()   # 清锁屏独立密码 → 下次按住 3 秒可解锁
            gui._log('🔓 密保验证通过：锁屏密码已清除并解锁')
            self._unlock()
        else:
            self.msg_var.set(i18n.tr('密保答案不正确', 'Incorrect answers'))

    def _try_password(self, silent=False):
        if getattr(self, '_unlocked', False):
            return
        pwd = self.pwd_var.get()
        if not pwd:
            return
        try:
            ok = verify_own_password(pwd) if self._pwd_kind == 'own' \
                else self.gui.parental.verify(pwd)
            logger.info(f'[锁屏] 验证(kind={self._pwd_kind}) 结果={ok}')
            if ok:
                self._unlock()
            elif silent:
                return   # ★ R164 输入即校验：未输完时静默，不闪红
            else:
                self.msg_var.set(i18n.tr('密码不正确', 'Incorrect password'))
                self.pwd_var.set('')
        except Exception as e:
            logger.error(f"锁屏验证异常: {e}")
            self.msg_var.set('验证异常')

    def _hold_start(self, _e):
        self._hold_left = 3
        self._hold_countdown()

    def _hold_stop(self):
        if self._hold_job:
            try:
                self.wins[0].after_cancel(self._hold_job)
            except Exception:
                pass
            self._hold_job = None
        # ★R204 NEW-B5：无条件复位文案——倒数到"再按住 1 秒…"时松手
        # _hold_left 已是 0，旧条件把它当 falsy 跳过，残文永远留在按钮上
        self.hold_var.set(i18n.tr('按住下方按钮 3 秒解锁',
                                  'Hold the button for 3s to unlock'))
        self._hold_left = 0

    def _hold_countdown(self):
        if self._hold_left <= 0:
            self._unlock()
            return
        self.hold_var.set(i18n.tr(
            f'再按住 {self._hold_left} 秒…', f'Keep holding {self._hold_left}s…'))
        self._hold_left -= 1
        self._hold_job = self.wins[0].after(1000, self._hold_countdown)

    def _unlock(self):
        # ★ R168：防重入（KeyRelease 自动重复曾连刷几十条"已解锁"）
        # ★R207 NEW3-B9：hide() 会复位 _unlocked，单布尔在"解锁同事件的
        # 自动重复"窗口失效——叠加 0.6s 时间闸
        import time as _t
        if getattr(self, '_unlocked', False):
            return
        if _t.monotonic() - getattr(self, '_last_unlock_ts', -1e9) < 0.6:
            # ★R210（NEW4-B5）：时间闸早退必须留痕，否则"按回车没反应"
            # 无从归因
            # ★R213（NEW5-B4）：debug 级在 root 默认 INFO 下永远不落
            # app.log（app_bootstrap.py:52）——B5 修复实际隐身，用户
            # 报"没反应"依旧无证据。分支仅 0.6s 内重复按键，刷屏封顶
            # =重复率，info 安全。
            logger.info('[锁屏] 0.6s 解锁时间闸，忽略重复解锁')
            return
        self._unlocked = True
        self._last_unlock_ts = _t.monotonic()
        before = sum(1 for w in self.wins if w.winfo_exists())
        logger.info(f'🔒 锁屏已解锁（窗 {before} 个，暂停计时器并隐藏待复用）')
        try:
            release_logoff_guard(self.gui)
        except Exception:
            pass
        # ★R210（NEW4-B4）：close_all 包 try——hide 一抛不能把"已解锁"
        # 状态卡死（_try_password 见 _unlocked=True 即 return=锁窗永久
        # 叫不动）；失败至少留双通道日志
        try:
            close_all()
        except Exception as e:
            logger.error(f'锁屏 close_all 异常（已尝试兜底隐藏）: {e}')
            try:
                self.hide()
            except Exception as e2:
                logger.error(f'锁屏兜底 hide 也失败: {e2}')
            if getattr(self, 'wins', None):
                for w in self.wins:
                    try:
                        w.grab_release()
                        w.withdraw()
                    except Exception:
                        pass
            self._unlocked = False
        after = sum(1 for w in self.wins if w.winfo_exists())
        logger.info(f'🔒 锁屏收尾完成：{after}/{before} 窗存活待复用')

    def _pause_timers(self):
        """★R191（GUIB-2）：隐藏前停掉全部计时器链——原实现 withdraw 后
        _tick/_breath 照常在隐形窗上每秒/0.6s 空转，每轮锁/解泄漏一套。"""
        for name in ('_tick_job', '_breath_job', '_hold_job'):
            job = getattr(self, name, None)
            if job:
                try:
                    self.wins[0].after_cancel(job)
                except Exception:
                    pass
            setattr(self, name, None)

    def _ui_sig(self):
        """UI 关键状态指纹（★R192b）：密码种类/字号档/锁屏图。任一变化，
        旧解锁 UI 一律作废重建。R191 只查密码；win11 17 批实锤"清了图片
        下次锁屏还显示旧图"——设置已 pop，复用窗还揣着旧 PhotoImage。
        字号档（lock_font）同理在列。★R204 NEW-B2：密码"种类"必须入指纹
        （仅有家长密码锁过一次→设锁屏密码→再锁，旧指纹布尔恒 True=
        复用旧 UI，新密码被送去 parental.verify 恒败）。"""
        if has_own_password():
            kind = 'own'
        else:
            try:
                kind = 'parental' if self.gui.parental.has_password() else 'none'
            except Exception:
                kind = 'none'
        st = self.gui._app_settings
        preset = str(st.get('lock_preset', '') or '')
        img = str(st.get('lock_image', '') or '')
        if img:
            try:
                from utils.app_paths import get_app_dir
                _pf = Path(img)
                if not _pf.is_file() and _pf.name:
                    _pf = get_app_dir() / 'config' / _pf.name
                img = f'{img}|{int(_pf.stat().st_mtime)}'
            except OSError:
                pass
        return (kind, _font_scale(st), preset, img)

    def _ui_matches(self):
        """重建信号：指纹与构建时不一致=不许 reshow 复用（R191 原理的
        泛化版，_unlock_ui_matches 由它取代）"""
        return self._ui_sig() == getattr(self, '_ui_sig0', None)

    def hide(self):
        """解锁收尾：停计时器 + 释放 grab + 全部窗口 withdraw（不销毁，
        R168b；实例保留供下次锁屏 reshow 复用——R191 泄漏修复）。
        ★R210（NEW4-B4）：grab_release/withdraw 提到最前——hide 中途抛
        异常时窗口必须已经"叫得动"（P0 铁律：全屏带 grab 的锁窗对键鼠
        永久无反应=GUIB-1 复活）。
        ★R213（NEW5-B5）：docstring 原本自称"计时器复位在 try/finally"、
        实为同一个 try 包三句——pwd_var.set('') 若抛，_pause_timers()
        整句跳过，_tick/_breath 继续跑到下次 reshow（正是 R191 GUIB-2
        泄漏模式）。停计时器挪到最前并独立 finally，任何变量复位失败
        都不许拖住它。"""
        for w in self.wins:
            try:
                w.grab_release()
            except Exception:
                pass
            try:
                w.withdraw()
            except Exception:
                pass
        try:
            self._unlocked = False
            self.pwd_var.set('')
        except Exception as e:
            logger.error(f'锁屏收尾变量复位异常（窗口已隐藏/释放）: {e}')
        finally:
            try:
                self._pause_timers()
            except Exception as e:
                logger.error(f'锁屏收尾停计时器失败（R191 泄漏面）: {e}')

    def reshow(self):
        """重新锁屏：重置状态、重新 grab+聚焦、恢复计时器（★R191：
        hide 已释放 grab 并停了时钟/呼吸灯，复用必须全部接回，否则
        锁不住输入、锁屏没有已锁定感）。★R204 NEW-B1：先停后启——
        "锁中再锁"（家长到时自动锁/连点）路径计时器还在跑，直接重启
        会双链覆写单引用，多出的链在隐形窗上永停不下来。"""
        self._pause_timers()
        self._unlocked = False
        self.pwd_var.set('')
        # ★R210（NEW4-B5）：复用前复位 0.6s 解锁时间闸基准——否则"解锁
        # 后立刻再锁+秒输对密码"会被上一次解锁时间戳静默吞一次
        import time as _t
        self._last_unlock_ts = _t.monotonic() - 1.0
        if getattr(self, 'hold_var', None) is not None:
            try:
                self.hold_var.set(i18n.tr('按住下方按钮 3 秒解锁',
                                          'Hold the button for 3s to unlock'))
            except Exception:
                pass
            self._hold_left = 0
        if getattr(self, 'msg_var', None) is not None:
            try:
                self.msg_var.set('')
            except Exception:
                pass
        for w in self.wins:
            try:
                w.deiconify()
                w.attributes('-topmost', True)
            except Exception:
                pass
        try:
            self.wins[0].grab_set()
        except Exception:
            pass
        try:
            self.wins[0].lift()
            self.wins[0].focus_force()
        except Exception:
            pass
        e = getattr(self, '_entry', None)
        if e is not None and e.winfo_exists():
            try:
                e.focus_set()
            except Exception:
                pass
        self._tick()
        strip = getattr(self, '_strip', None)
        if strip is not None and strip.winfo_exists():
            self._breath(strip)

    def destroy(self):
        self._pause_timers()
        for w in self.wins:
            try:
                # ★ R168b：destroy 前必须释放 grab——持 grab 的窗 destroy
                # 会静默失败（复现实锤：unlock 后窗仍 alive 吞全部输入）
                w.grab_release()
            except Exception:
                pass
            try:
                w.destroy()
            except Exception:
                pass
        self.wins = []
        global _inst
        _inst = None
