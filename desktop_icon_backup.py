# -*- coding: utf-8 -*-
"""📦 桌面图标备份/恢复（★ R41：新功能分文件——用户曾被桌面图标丢失困扰）。

与现有备份的关系（互不重复）：
- 布局存档/布局zip 只存"位置坐标"；导出全部数据只存分类/规则/统计——
  都是元数据。本模块备份的是**桌面上的实际文件**（.lnk/.url/文档），
  图标文件真丢了时：先恢复本备份（文件回来）→ 再恢复布局（位置摆回）。

范围界定：
- 只备份**文件**（快捷方式/文档等），不含桌面上的文件夹（防止把几个 GB
  的目录卷进 zip）；文件夹名单记入 _备份信息.json 供参考。
- 排除 desktop.ini / thumbs.db / *.zip（避免把旧备份卷进新备份）。
- 桌面 = 用户桌面 + 公共桌面（复用 icon_manager._desktop_dirs 的注册表兜底）。
- zip 根层平铺（恢复时解回用户桌面），附 _备份信息.json 清单。

安全（恢复侧）：zip-slip 防护（成员名取 basename、含路径分隔符的跳过）、
条目数/单条大小/总量三道闸——与布局 zip 导入同款防御。
"""
import io
import json
import logging
import os
import zipfile
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("desktop_manager")

EXCLUDE_NAMES = {'desktop.ini', 'thumbs.db'}
INFO_NAME = '_备份信息.json'
MAX_ENTRIES = 5000         # 恢复闸①：成员数上限（★ R196 文件夹内容打包后放宽）
MAX_MEMBER = 100 * 1024 * 1024   # 恢复闸②：单成员 100MB
MAX_TOTAL = 2 * 1024 * 1024 * 1024   # 恢复闸③：总量 2GB（★ R196 文件夹内容合法变大）
WARN_EXPORT_BYTES = 500 * 1024 * 1024   # ★ R42 导出预估超 500MB → 建议确认
_ESTIMATE_MAX_FILES = 50000   # ★R204 NEW-R1：预估遍历硬顶（防 junction 整盘扫）
_MAX_DEPTH = 8                # ★R204 NEW-R1：文件夹递归深度上限
# ★R204 NEW-R11：Windows 保留设备名（stem 命中即拒），恢复侧不再谎报 restored
_RESERVED_BASES = ({'nul', 'con', 'prn', 'aux'}
                   | {f'com{i}' for i in range(1, 10)}
                   | {f'lpt{i}' for i in range(1, 10)})
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _is_reparse_point(p) -> bool:
    """★R204 NEW-R1：junction（mklink /J）的 is_symlink()=False 且
    os.walk(followlinks=False) 照样会进（自环复现=导出即主线程无限
    递归/整盘打包）——按重解析点属性判定，junction/symlink 一网打尽。"""
    try:
        st = os.stat(p, follow_symlinks=False)
        return bool(getattr(st, 'st_file_attributes', 0)
                    & _FILE_ATTRIBUTE_REPARSE_POINT)
    except OSError:
        return True   # 判不了的一律当危险处理（跳过）


def _has_reserved_name(rel: str) -> bool:
    """★R204 NEW-R11：路径任一段的 stem 是 Windows 保留设备名→True"""
    for part in rel.replace('\\', '/').split('/'):
        stem = part.split('.', 1)[0].lower()
        if stem in _RESERVED_BASES:
            return True
    return False


def estimate_desktop_size(dirs=None) -> int:
    """桌面总字节数（★ R196 与导出同口径：文件 + 文件夹内容）"""
    total = 0
    n = 0
    for p, _ in list_desktop_files(dirs):
        try:
            if p.exists():
                total += p.stat().st_size
        except OSError:
            pass
    for folder, _ in list_desktop_folders(dirs):
        for p in _iter_folder_files(folder):
            n += 1
            if n > _ESTIMATE_MAX_FILES:
                logger.warning("[桌面图标备份] 预估遍历超过 %d 个文件，提前截断",
                               _ESTIMATE_MAX_FILES)
                return total
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def list_desktop_folders(dirs=None):
    """★ R196：收集各桌面顶层文件夹（排除项/reparse 点除外；用户+公共
    桌面同名时保留第一个）。返回 [(Path, 来源桌面名)]"""
    dirs = [Path(x) for x in (dirs or get_desktop_dirs())]   # 兼容 str 传入
    out = []
    seen = set()
    for d in dirs:
        try:
            for p in sorted(Path(d).iterdir()):
                if not p.is_dir():
                    continue
                if p.name.lower() in EXCLUDE_NAMES or _is_reparse_point(p):
                    continue
                if p.name.lower() in seen:
                    continue
                seen.add(p.name.lower())
                out.append((p, d.name or str(d)))
        except Exception as e:
            logger.warning(f"[桌面图标备份] 扫描 {d} 失败: {e}")
    return out


def _iter_folder_files(folder: Path, max_depth: int = _MAX_DEPTH):
    """★ R196：递归列文件夹内容。★R204 NEW-R1：重解析点目录（junction）
    一律剪枝 + 深度上限双保险；desktop.ini/thumbs.db 排除；文件夹内部
    的 zip 是用户数据，照打包（根层排除 *.zip 的"防卷旧备份"规则只
    适用于桌面根层）。"""
    base_depth = len(Path(str(folder)).parts)
    for root, dirs, files in os.walk(folder, followlinks=False):
        if len(Path(root).parts) - base_depth >= max_depth:
            dirs[:] = []   # 到达深度上限：不再下钻
        dirs[:] = [d for d in dirs if not _is_reparse_point(Path(root) / d)]
        for fn in sorted(files):
            p = Path(root) / fn
            # ★R213（NEW5-E3）：.dbk_tmp 排除此前只装在桌面根层扫描
            # （:159），嵌套文件夹成员走这里会漏——被中断的原子写临时件
            # 会被当真实成员备份，恢复时顶掉用户数据（与 NEW4-R3 修复
            # 面同口径补齐）
            if (p.name.lower() in EXCLUDE_NAMES or p.is_symlink()
                    or fn.endswith('.dbk_tmp')):
                continue
            yield p


def _iter_folder_dirs(folder: Path, max_depth: int = _MAX_DEPTH):
    """★R204 NEW-R10：文件夹内全部子目录（含空目录——空文件夹不产生
    zip 条目，恢复侧按清单补建需要这份名单）。剪枝规则同 _iter_folder_files。"""
    base_depth = len(Path(str(folder)).parts)
    for root, dirs, files in os.walk(folder, followlinks=False):
        if len(Path(root).parts) - base_depth >= max_depth:
            dirs[:] = []
        dirs[:] = [d for d in dirs if not _is_reparse_point(Path(root) / d)]
        for d in sorted(dirs):
            yield Path(root) / d


def get_desktop_dirs():
    """用户桌面 + 公共桌面（icon_manager 的健壮实现，含注册表兜底）"""
    from core.icon_manager import _desktop_dirs
    return [Path(p) for p in _desktop_dirs() if p and os.path.isdir(p)]


def list_desktop_files(dirs=None):
    """收集各桌面的文件（不含目录/排除项）。返回 [(Path, 来源桌面名)]"""
    dirs = [Path(x) for x in (dirs or get_desktop_dirs())]   # ★ R196：兼容 str 传入
    out = []
    for d in dirs:
        try:
            for p in sorted(Path(d).iterdir()):
                if not p.is_file():
                    continue
                if p.name.lower() in EXCLUDE_NAMES or p.suffix.lower() == '.zip':
                    continue
                # ★R210（NEW4-R3）：原子写半截残留不进备份（恢复侧首尾
                # 已各扫一遍，这里再挡导出口径）
                if p.name.endswith('.dbk_tmp'):
                    continue
                out.append((p, d.name or str(d)))
        except Exception as e:
            logger.warning(f"[桌面图标备份] 扫描 {d} 失败: {e}")
    return out


def export_desktop_icons(zip_path, log=None, dirs=None) -> dict:
    """把桌面文件+文件夹（含内容）打包为 zip（★ R196：文件夹一并备份，
    否则文件丢了救不回来，用户 25 批定案"要不怎么叫备份呢"）。
    结构：根层平铺文件 + 顶层文件夹/内部相对路径 + _备份信息.json。
    返回 {"success", "count", "folders", "skipped", "error"}。"""
    lg = log or (lambda m: None)
    try:
        pairs = list_desktop_files(dirs)
        folder_pairs = list_desktop_folders(dirs)
        if not pairs and not folder_pairs:
            return {"success": False, "error": "桌面上没有可备份的文件图标"}
        folder_names = [p.name for p, _ in folder_pairs]
        empty_dirs = []   # ★R204 NEW-R10：空目录名单（恢复侧补建用）
        for folder, _src in folder_pairs:
            for sub in _iter_folder_dirs(folder):
                try:
                    if not any(sub.iterdir()):
                        empty_dirs.append(
                            f"{folder.name}/{sub.relative_to(folder).as_posix()}")
                except OSError:
                    pass

        seen = set()
        count = 0
        skipped = []
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
            for p, src in pairs:
                arc = p.name
                if arc.lower() in seen:   # 用户/公共桌面同名 → 保留第一个
                    skipped.append(f"{arc}（公共桌面同名）")
                    continue
                seen.add(arc.lower())
                try:
                    z.write(p, arc)
                    count += 1
                except Exception as e:
                    skipped.append(f"{arc}: {e}")
            # ★ R196：文件夹内容一并打包（arc=顶层文件夹/内部相对路径）
            for folder, _src in folder_pairs:
                for f in _iter_folder_files(folder):
                    arc = f"{folder.name}/{f.relative_to(folder).as_posix()}"
                    if arc.lower() in seen:
                        skipped.append(f"{arc}（同名）")
                        continue
                    seen.add(arc.lower())
                    try:
                        z.write(f, arc)
                        count += 1
                    except Exception as e:
                        skipped.append(f"{arc}: {e}")
            info = {
                "app": "桌面超强管理助手 桌面图标备份",
                "exported": datetime.now().isoformat(),
                "count": count,
                "files": sorted(seen),
                "folders": sorted(folder_names),
                "empty_dirs": sorted(empty_dirs),   # ★R204 NEW-R10
                "skipped": skipped,
            }
            z.writestr(INFO_NAME, json.dumps(info, ensure_ascii=False, indent=2))
        logger.info(f"[桌面图标备份] 已导出 {count} 个文件（含 {len(folder_names)} 个文件夹）→ {zip_path}")
        lg(f"✓ 已备份 {count} 个桌面文件（含 {len(folder_names)} 个文件夹）→ {zip_path}")
        if skipped:
            lg(f"⚠ 跳过 {len(skipped)} 项: {'; '.join(skipped[:3])}")
        return {"success": True, "count": count,
                "folders": sorted(folder_names), "skipped": skipped}
    except Exception as e:
        logger.error(f"[桌面图标备份] 导出失败: {e}")
        return {"success": False, "error": str(e)}


def _write_member(z, zi, dest: Path):
    """★R204 ROOT-8：原子写恢复成员——先写 .dbk_tmp 再 os.replace。
    进程被杀/磁盘满不再给桌面留 0 字节半截文件（原件同时被毁=备份
    恢复反成丢文件点）。"""
    tmp = dest.with_name(dest.name + '.dbk_tmp')
    try:
        with z.open(zi) as src, open(tmp, 'wb') as f:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
        os.replace(tmp, dest)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _sweep_dbk_tmp(dirs):
    """★R210（NEW4-R3）：清扫桌面侧 .dbk_tmp 原子写孤儿（超 1 天）。
    真实残留点=恢复目标桌面（进程被杀时写一半），simple_backup 扫项目
    目录永远够不到这里。"""
    import time as _t
    now = _t.time()
    gone = 0
    for d in dirs:
        try:
            for f in Path(d).iterdir():
                if f.is_file() and f.name.endswith('.dbk_tmp'):
                    try:
                        if now - f.stat().st_mtime > 86400:
                            f.unlink()
                            gone += 1
                    except OSError:
                        pass
        except OSError:
            pass
    if gone:
        logger.info(f"[桌面图标备份] 清扫 .dbk_tmp 孤儿 {gone} 个")


def restore_desktop_icons(zip_path, target_dir, overwrite=False, log=None) -> dict:
    """从 zip 恢复桌面文件到 target_dir（用户桌面）。

    三道闸（条目数/单成员/总量）+ zip-slip 防护（平铺 basename）。
    ★R204 NEW-R2：*.zip 排除只对根层平铺成员生效——文件夹内 zip 是
    用户数据（导出照打包），按 basename 一刀切=备份不可逆。
    返回 {"success", "restored", "skipped", "failed", "info", "error"}。"""
    lg = log or (lambda m: None)
    restored = skipped = failed = 0
    restored_names = []   # ★ R97：逐文件留痕
    routed = []           # ★ R101：写回非用户桌面的（公共桌面路由标注）
    info = None
    try:
        target = Path(target_dir)
        target.mkdir(parents=True, exist_ok=True)
        # ★ R97：全部桌面目录（用户+公共），用于"已存在路由"
        from core.icon_manager import _desktop_dirs
        all_dirs = [Path(p) for p in _desktop_dirs() if p and os.path.isdir(p)]
        # ★R210（NEW4-R3）：恢复首尾各扫一遍 .dbk_tmp——真实残留点在
        # 桌面（restore 的 dest 侧），simple_backup 扫项目目录永远够
        # 不到；超 1 天的必是孤儿（正常原子写瞬间完成）
        _sweep_dbk_tmp(all_dirs)
        total = 0
        with zipfile.ZipFile(zip_path, 'r') as z:
            names = z.namelist()
            if len(names) > MAX_ENTRIES:
                return {"success": False,
                        "error": f"zip 内 {len(names)} 个条目，超过上限 {MAX_ENTRIES}"}
            for zi in z.infolist():
                if zi.is_dir():
                    continue
                if zi.file_size > MAX_MEMBER:
                    skipped += 1
                    lg(f"⚠ 跳过 {zi.filename}: 单文件超过 100MB")
                    continue
                total += zi.file_size
                if total > MAX_TOTAL:
                    return {"success": False, "restored": restored,
                            "error": f"累计解压超过 {MAX_TOTAL // (1024 * 1024)}MB，"
                                     "中止（疑似选错文件）"}
            for zi in z.infolist():
                if zi.is_dir() or zi.filename == INFO_NAME:
                    continue
                # ★R204 ROOT-7：解压循环内再验单成员闸（预扫描只计数不设防，
                # 超大成员照样写出桌面）
                if zi.file_size > MAX_MEMBER:
                    skipped += 1
                    lg(f"⚠ 跳过 {zi.filename}: 单文件超过 100MB")
                    continue
                rel = zi.filename.replace('\\', '/').rstrip('/')
                base = os.path.basename(rel)
                if not base or base.startswith('__MACOSX'):
                    continue
                if _has_reserved_name(rel):
                    # ★R204 NEW-R11：保留设备名（nul/com1…）不写盘不谎报
                    skipped += 1
                    lg(f"⚠ 跳过保留名成员: {rel}")
                    continue
                if '/' in rel:
                    # ★R213（NEW5-E6）：R8 平铺分支已显式拒 '.'/'..'，
                    # 文件夹分支只靠 resolve 前缀闸——'a/.' 这类含 '.'
                    # 级联的 rel 归一后仍落在 target 内穿过闸，写出怪路径
                    # （pathlib 保留 '.'）。任一级联段为 '.'/'..'/空段即
                    # 整条拒绝。
                    if any(seg in ('', '.', '..') for seg in rel.split('/')):
                        skipped += 1
                        lg(f"⚠ 跳过含 . / .. / 空级联的成员: {rel}")
                        continue
                    # ★ R196：本模块导出的文件夹成员（顶层文件夹/内部路径）
                    # ——解回目录结构；zip-slip 防护改为"解析后必须仍落在
                    # 目标目录内"（平铺 basename 法对文件夹条目不适用）
                    top = rel.split('/', 1)[0]
                    if top.lower() in EXCLUDE_NAMES or base.lower() in EXCLUDE_NAMES:
                        skipped += 1
                        continue   # ★R204 NEW-R2：文件夹内 .zip 是用户数据，不排除
                    dest = target / rel
                    try:
                        if not str(dest.resolve()).startswith(
                                str(target.resolve()) + os.sep):
                            skipped += 1
                            continue
                    except Exception:
                        skipped += 1
                        continue
                    # ★R204 NEW-R5：顶层文件夹已存在于某个桌面目录（含公共
                    # 桌面）就写回原地——来源路由不再被丢弃，否则每次恢复
                    # 都在用户桌面新建一份公共桌面文件夹（R97 症状搬到文件夹层）
                    dest = None
                    for d in all_dirs:
                        cand = d / rel
                        if cand.exists():
                            dest = cand
                            break
                    if dest is None:
                        dest = target / rel
                    if dest.exists() and not overwrite:
                        skipped += 1
                        continue
                    if dest != target / rel:
                        routed.append(f"{rel}（→ {dest.parent}）")
                    try:
                        if not dest.parent.exists():
                            dest.parent.mkdir(parents=True, exist_ok=True)
                        _write_member(z, zi, dest)
                        restored += 1
                        restored_names.append(rel)   # ★R204 NEW-R10：文件夹成员同样留痕
                    except Exception as e:
                        failed += 1
                        logger.warning(
                            f"[桌面图标恢复] {rel} 写入失败: {e}")
                    continue
                # ★ R101：与导出同口径排除系统文件——外部来源的 zip 可能
                # 带着 desktop.ini；★R204 NEW-R2：*.zip 排除只在这里（根层）
                if base.lower() in EXCLUDE_NAMES or base.lower().endswith('.zip'):
                    skipped += 1
                    continue
                # ★R210（NEW4-R8）：'.'/'..' 级联名显式拒绝（此前仅靠
                # pathlib 归一+os.replace 撞目录"巧合安全"，非设计）
                if base in ('.', '..'):
                    skipped += 1
                    continue
                # ★ R97：按"已存在于哪个桌面"路由——公共桌面(全用户共享)
                # 的快捷方式写回原地，不再复制进用户桌面造成重复图标
                # （win11 实测：OBS/H3C/Microsoft Edge 等 12 颗每次恢复翻倍）
                dest = None
                for d in all_dirs:
                    cand = d / base
                    if cand.exists():
                        dest = cand
                        break
                if dest is None:
                    dest = target / base
                if dest.exists() and not overwrite:
                    skipped += 1
                    continue
                try:
                    _write_member(z, zi, dest)
                    restored += 1
                    restored_names.append(base)   # ★ R97 逐文件留痕
                    if dest.parent != target:
                        routed.append(f"{base}（→ {dest.parent}）")
                except Exception as e:
                    failed += 1
                    logger.warning(f"[桌面图标恢复] {base} 写入失败: {e}")
            try:
                info = json.loads(z.read(INFO_NAME).decode('utf-8'))
            except Exception:
                info = None
            # ★ R196/R204 NEW-R10：空文件夹不产生 zip 条目——按清单补建
            # （顶层名单 + 子级空目录名单；逐条 try，单个非法名不中断全部）
            try:
                for fname in (info or {}).get('folders') or []:
                    try:
                        if fname != Path(fname).name:   # 防清单伪造路径
                            continue
                        if fname.lower() in EXCLUDE_NAMES or _has_reserved_name(fname):
                            continue
                        (target / fname).mkdir(parents=True, exist_ok=True)
                    except Exception as e:
                        logger.warning(f"[桌面图标恢复] 补建文件夹 {fname} 失败: {e}")
                for rel_dir in (info or {}).get('empty_dirs') or []:
                    try:
                        rd = Path(rel_dir.replace('\\', '/'))
                        if rd.is_absolute() or '..' in rd.parts:
                            continue
                        if any(x.lower() in EXCLUDE_NAMES for x in rd.parts):
                            continue
                        if _has_reserved_name(str(rd)):
                            continue
                        (target / rd).mkdir(parents=True, exist_ok=True)
                    except Exception as e:
                        logger.warning(f"[桌面图标恢复] 补建子目录 {rel_dir} 失败: {e}")
            except Exception as e:
                logger.warning(f"[桌面图标恢复] 补建文件夹失败: {e}")
        # ★ R201：通知 Explorer 重扫桌面目录（GUI 恢复流程另有 F5 兜底，
        # 模块层自带通知保证独立调用也能立刻看到）
        try:
            import ctypes
            ctypes.windll.shell32.SHChangeNotify(
                0x00001000,   # SHCNE_UPDATEDIR
                0x0005,       # SHCNF_PATHW
                ctypes.c_wchar_p(str(target)), None)
        except Exception:
            pass
        logger.info(f"[桌面图标恢复] 恢复 {restored} 跳过 {skipped} 失败 {failed} → {target}")
        lg(f"✓ 已恢复 {restored} 个桌面文件（跳过 {skipped}，失败 {failed}）")
        for _n in sorted(restored_names):
            lg(f"  · 已解回: {_n}")
        if routed:
            lg(f"  · 其中 {len(routed)} 个按原位置写回其他桌面目录: "
               + '; '.join(sorted(routed)))
        # ★R210（NEW4-R3）：恢复收尾再扫一遍 .dbk_tmp（写一半被杀的孤儿）
        _sweep_dbk_tmp(all_dirs)
        return {"success": True, "restored": restored, "skipped": skipped,
                "names": sorted(restored_names),
                "failed": failed, "info": info}
    except zipfile.BadZipFile:
        return {"success": False, "error": "不是有效的 zip 文件"}
    except Exception as e:
        logger.error(f"[桌面图标恢复] 失败: {e}")
        return {"success": False, "restored": restored, "error": str(e)}
