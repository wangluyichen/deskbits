# -*- coding: utf-8 -*-
"""★ R65-Bug1：全项目统一的 APP_DIR 解析。

打包(frozen)后模块 __file__ 指向 PyInstaller 的 _MEIPASS 临时目录，
而 config/ data/ icon_backups/ 都在 exe 旁边。此前 gui/data_manage_page
用 Path(__file__) 推 APP_DIR，"清空全部个人数据"的写后复核读了临时
目录里不存在的 categories.json → 误报"清空未生效 icons=-1"
（2026-09-14 用户打包实测）。utils/logger.py 此前已单独修过同款问题，
本模块把规则收敛为一处，各分文件模块统一引用。"""
import sys
from pathlib import Path


def _compiled_outer_dir():
    """★ R174 C5：Nuitka 编译态取外层 exe 目录。

    Nuitka onefile 下 sys.executable 指向缓存解压目录里的内层 exe，
    而 data/config 必须在外层 exe 旁边。主模块(launcher)带有 Nuitka
    注入的 __compiled__.original_argv0 = 外层 exe 被调用时的原始路径。
    取不到（非 Nuitka 形态）返回 None。

    ★R213（NEW5-A7）：回落级次对齐 launcher._outer_exe() 的三级口径
    （original_argv0 → sys.argv[0] → sys.executable）——此前本函数缺
    sys.argv[0] 一级，取不到 original_argv0 时 launcher 算外层 exe 目录、
    本模块直接落到 sys.executable（=内层缓存目录），同一 flag/数据目录
    出现两个算源 → onefile 重启循环复发。"""
    try:
        m = sys.modules.get('__main__')
        comp = getattr(m, '__compiled__', None)
        for cand in (getattr(comp, 'original_argv0', None) if comp else None,
                     sys.argv[0] if sys.argv else None):
            if not cand:
                continue
            p = Path(cand)
            if not p.is_absolute():
                p = Path.cwd() / p
            if p.exists():
                return p.parent
    except Exception:
        pass
    return None


def get_executable() -> str:
    """打包态=外层 exe 绝对路径（供重启/拉起子进程用——Nuitka onefile
    下 sys.executable 是缓存目录的内层 exe，也能跑但位置不可靠）；
    源码态=sys.executable。"""
    if getattr(sys, 'frozen', False):
        d = _compiled_outer_dir()
        if d is not None:
            m = sys.modules.get('__main__')
            comp = getattr(m, '__compiled__', None)
            oa = getattr(comp, 'original_argv0', None)
            if oa:
                p = Path(oa)
                if not p.is_absolute():
                    p = Path.cwd() / p
                return str(p)
        return str(Path(sys.executable).resolve())
    return sys.executable


def get_app_dir() -> Path:
    """打包模式返回 exe 所在目录；源码模式返回项目根目录。

    ★ R174：Nuitka onefile 下 sys.executable=缓存解压目录，
    优先从主模块 __compiled__.original_argv0 取外层 exe 目录。"""
    if getattr(sys, 'frozen', False):
        d = _compiled_outer_dir()
        if d is not None:
            return d
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def get_backup_dir(settings=None) -> Path:
    """★ R177（用户 -09/7）：备份/导出根目录——用户自定义
    （app_settings['backup_dir']）优先；缺省=程序目录下 backup/。
    目录不存在自动创建（用户要求"默认我们需要有这个文件夹"）；
    自定义目录不可写时回退用户家目录。"""
    d = None
    if isinstance(settings, dict):
        d = settings.get('backup_dir') or None
    if not d:
        d = str(get_app_dir() / 'backup')
    p = Path(d)
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        p = Path.home() / 'DeskBits_backup'
        p.mkdir(parents=True, exist_ok=True)
    return p
