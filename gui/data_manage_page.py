# -*- coding: utf-8 -*-
"""📦 数据管理页（★ R43：自 gui/main_window.py 拆出）。

模块级函数风格：gui=主窗口实例。按钮接线在 build_data_manage_tab；
main_window 侧保留同名薄壳转发到这里。"""
import io
import json
import logging
import os
import re
import threading
import zipfile
from datetime import datetime
from pathlib import Path
from tkinter import ttk, messagebox, filedialog
import tkinter as tk

from utils.safeio import atomic_write_json

from utils.app_paths import get_app_dir  # ★ R65-Bug1：frozen 时 __file__ 指向 _MEIPASS
from utils import i18n   # ★ R73 双语
# ★R216 阶段2：布局骨架 helper + 间距常量（本页从 6 行 run-on pack 改成分区网格）
from gui import ui_layout
from gui.ui_layout import SP
APP_DIR = get_app_dir()
logger = logging.getLogger("desktop_manager")


def _backup_dir(gui):
    """★ R177：导出对话框默认目录=用户自定义 backup_dir 或程序内 backup/"""
    from utils.app_paths import get_backup_dir
    try:
        return str(get_backup_dir(getattr(gui, '_app_settings', None)))
    except Exception:
        return ''


def _export_tabs_state():
    """★R207 NEW3-B4：导出用——读失败（_load_failed）时不带该节"""
    from core import explorer_tabs
    st = explorer_tabs.load_state(APP_DIR / 'config')
    if st.pop('_load_failed', None):
        logger.error('explorer_tabs.json 读取失败，备份包不含文件夹组'
                     '（防把空组带给导入方）')
        return None
    return st


def _save_tabs_checked(state, gui) -> bool:
    """★R207 NEW3-B4：导入/清空侧统一消费 save_state 返回值——写失败
    必须 gui._log 可见（此前静默丢弃=报告成功实际没进）"""
    from core import explorer_tabs
    ok = explorer_tabs.save_state(APP_DIR / 'config', state)
    if not ok:
        gui._log('⚠ 文件夹组写盘失败（读取失败保护或磁盘问题），'
                 '该部分未生效，详见日志')
    return ok


def _isdir_fast(path, timeout=0.5):
    """带超时的"目录还在不在"——主线程绝不替网络盘/可移动盘等 I/O。

    ★R319-M8：`last_export_dir` 可能是网络共享路径（UNC） 或已经拔掉的U盘，而
    `os.path.isdir` 在这种路径上会一路卡在 SMB 超时里（实测几十秒），它又跑在
    **主线程**（打开导出对话框就要问一次）⇒ 整个数据页冻住、窗口"未响应"，
    用户读成"点了没反应"。这里把探测丢进守护线程，超时就按"问不到"处理，
    对话框指回默认目录，用户照样能导出。
    **故意不缓存**那个 True：缓存会把后来断掉的共享一直当成可用目录，
    下一次照样卡在那里（门禁 M8c 钉的就是这条红线）。
    """
    box = [False]                       # ★R249：判据容器显式建，别靠 `or []`
    ev = threading.Event()

    def _probe():
        try:
            box[0] = bool(os.path.isdir(path))
        except Exception as e:
            logger.warning('[数据管理] 目录探测异常（按不可用处理）: %s' % e)
            box[0] = False
        ev.set()

    threading.Thread(target=_probe, daemon=True).start()
    if ev.wait(timeout):
        return box[0]
    logger.warning('[数据管理] 目录暂时问不到（网络盘/可移动盘没响应），'
                   '本次按不可用处理: %s' % path)
    return False


def _last_dir(gui, key):
    """★ R200（用户 26 批）：对话框默认目录——"最后一次实际使用的目录"
    优先；无记录或目录已失效 → 回退 backup_dir（首次=程序内 backup/）。"""
    d = (getattr(gui, '_app_settings', {}) or {}).get(key)
    # ★R204 NEW-A5：设置文件被外部改坏成 dict/list 时 isdir 抛 TypeError，
    # 非字符串一律视为无记录
    if isinstance(d, str) and d and _isdir_fast(d):
        return d
    return _backup_dir(gui)


def _remember_dir(gui, key, path):
    """★ R200：把用户实际选中的文件所在目录记进设置（下次对话框默认）"""
    try:
        d = os.path.dirname(str(path or ''))
        if d and os.path.isdir(d):
            gui._app_settings[key] = d
            gui._save_app_settings()
    except Exception:
        pass


def _save_dialog(gui, **kw):
    """★ R200：导出对话框统一入口——默认上次导出目录，选完记住"""
    path = filedialog.asksaveasfilename(
        parent=gui.root, initialdir=_last_dir(gui, 'last_export_dir'), **kw)
    if path:
        _remember_dir(gui, 'last_export_dir', path)
    return path


def _open_dialog(gui, **kw):
    """★ R200：导入对话框统一入口——默认上次导入目录，选完记住"""
    path = filedialog.askopenfilename(
        parent=gui.root, initialdir=_last_dir(gui, 'last_import_dir'), **kw)
    if path:
        _remember_dir(gui, 'last_import_dir', path)
    return path


def build_icon_manifest_text(gui):
    """★ S 段：生成分类归属清单文本（人工可读）。
    "📤 导出"按钮与"导出全部数据"共用同一来源，杜绝两份清单漂移。"""
    lines = []
    for cat in gui.category_manager.get_all_categories():
        lines.append(f'【{cat["name"]}】({len(cat.get("icons", []))} 个)')
        for icon in cat.get("icons", []):
            lines.append(f'  {icon}')
        lines.append('')
    return '\n'.join(lines)


def on_export_icon_list(gui):
    """导出全部分类的图标名单（文本清单，方便分发/备份/查看）"""
    from tkinter import filedialog
    path = _save_dialog(gui, defaultextension='.txt',
        initialfile=i18n.tr(f'图标分类清单_{datetime.now().strftime("%Y%m%d_%H%M%S")}.txt',
                            f'Icon_Category_List_{datetime.now().strftime("%Y%m%d_%H%M%S")}.txt'),
        filetypes=[('文本', '*.txt')])
    if not path:
        return
    try:
        # （★ S 段顺带）旧实现 open(path,'w') 句柄未显式关闭，改 with
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(gui._build_icon_manifest_text())
        gui._log(f'✓ 已导出图标分类清单 → {path}')
    except Exception as e:
        logger.error(f'导出图标清单失败: {e}')
        messagebox.showerror(i18n.tr('错误', 'Error'),
                             f'{i18n.tr("导出失败", "Export failed")}: {e}',
                             parent=gui.root)


def on_import_icon_list(gui):
    """★ R19：导入图标分类清单（txt）——"📤 导出"的逆操作。

    解析"【分类名】(N 个) + 缩进图标名"格式，**合并语义**：
    - 清单里的分类不存在 → 自动创建（默认色）；
    - 图标并入该分类（去重），现有归属不动；
    - 清单里没有的图标/分类不受影响。
    注意：txt 只携带"归属"关系，颜色/显示模式等用默认值
    （完整备份请用"导出全部数据"，那里含完整 categories）。"""
    from tkinter import filedialog
    import re as _re
    path = _open_dialog(gui, title='选择图标分类清单 (txt)',
        filetypes=[('文本', '*.txt')])
    if not path:
        return
    # ★ R42：统一格式识别（拿错文件给指路提示）
    from io_formats import sniff, mismatch_message, KIND_TXT
    kind = sniff(path)
    if kind and kind != KIND_TXT:
        messagebox.showerror(i18n.tr('文件不匹配', 'File Mismatch'),
                             mismatch_message(KIND_TXT, kind),
                             parent=gui.root)
        return
    try:
        text = Path(path).read_text(encoding='utf-8')
    except Exception as e:
        messagebox.showerror(i18n.tr('错误', 'Error'),
                             f'{i18n.tr("读取失败", "Read failed")}: {e}',
                             parent=gui.root)
        return
    # 解析：【分类名】开新分类；其余非空行（去缩进）为图标名
    parsed = []
    cur = None
    for raw in text.splitlines():
        m = _re.match(r'^【(.+?)】', raw.strip())
        if m:
            cur = {'name': m.group(1).strip(), 'icons': []}
            parsed.append(cur)
            continue
        icon = raw.strip()
        if icon and cur is not None and not icon.startswith('【'):
            cur['icons'].append(icon)
    if not parsed:
        messagebox.showwarning(
            i18n.tr('提示', 'Notice'),
            i18n.tr('清单里没有识别到任何分类。\n'
                    '请确认这是本程序"📤 导出"生成的清单文件。',
                    'No category was recognized in this list.\n'
                    'Please make sure it is a list file generated by '
                    'this app ("📤 Export").'),
            parent=gui.root)
        return
    existing = {c['name'] for c in gui.category_manager.get_all_categories()}
    new_cats = [c['name'] for c in parsed if c['name'] not in existing]
    if not messagebox.askyesno(
            i18n.tr('确认导入清单', 'Confirm List Import'),
            i18n.tr(f'清单含 {len(parsed)} 个分类：\n'
                    f'· 不存在的分类 → 自动创建（{len(new_cats)} 个）\n'
                    f'· 图标并入对应分类（去重，现有归属不动）\n确定？',
                    f'The list contains {len(parsed)} categories:\n'
                    f'- Missing categories will be created ({len(new_cats)})\n'
                    f'- Icons are merged into their category (deduplicated; '
                    f'existing membership untouched)\nContinue?'),
            parent=gui.root):
        return
    # ★R313 F1：同文件另外四个改动入口（:230/:1130/:1647/:2106）都配了这道
    # "分类文件正读不动 ⇒ 先留底再放行"的自救闸，就"按清单导入"没有。
    if not _recover_categories(gui, ('导入图标清单',
                                     'importing an icon list')):
        return
    added_icons = 0
    _write_bad = 0
    for c in parsed:
        if c['name'] not in {x['name'] for x in
                             gui.category_manager.get_all_categories()}:
            gui.category_manager.add_category(c['name'])
        # 前后差值统计新增归属（add_icon_to_category 对重复图标不新增）
        before = len(gui.category_manager.get_category_icons(c['name']))
        for icon in c['icons']:
            # ★R130 起这个接口返回**落盘是否生效**（★R230 口径：内存改了、
            # 磁盘没改＝没改）。旧写法把返回值全丢 ⇒ 分类文件被占用/只读时
            # 界面照打"📥 已按清单导入 N 项"，而磁盘原封不动。
            if not gui.category_manager.add_icon_to_category(c['name'], icon):
                _write_bad += 1
        added_icons += max(0, len(gui.category_manager.get_category_icons(c['name'])) - before)
    gui._update_lists()
    if _write_bad:
        gui._log(f'⚠ 按清单导入：归属写入未生效 {_write_bad} 项'
                 '（分类文件被占用/只读/被杀软拦截），磁盘还是老数据，请排查后重试')
    else:
        gui._log(f'📥 已按清单导入：{len(parsed)} 个分类，'
                 f'新增归属 {added_icons} 项'
                 + (f'（新建分类 {len(new_cats)} 个）' if new_cats else ''))


def on_clear_icon_data(gui):
    """一键清空所有分类的图标名单（保留分类框架），重新归类"""
    total = sum(len(c.get('icons', []))
                for c in gui.category_manager.get_all_categories())
    if not messagebox.askyesno(
            i18n.tr('确认清空', 'Confirm Clear'),
            i18n.tr(f'将清空所有分类里的 {total} 个图标名单（分类名和颜色保留）。\n'
                    '图标本身不会被删除，之后重新拖拽归类即可。确定？',
                    f'This clears the icon lists of all categories '
                    f'({total} entries; category names and colors are kept).\n'
                    'The icons themselves are NOT deleted - drag them back into '
                    'a category afterwards. Continue?'),
            parent=gui.root):
        return
    # ★R231（用户 05 批 item2）：旧写法在这里弹「本次运行禁止写盘」并中止 ⇒
    # 用户改坏分类文件后连"清空重来"都不许，只剩手改 JSON 一条路。
    # 现在改为"先另存留底再放行"（正常态不多弹一次确认）。
    if not _recover_categories(gui, ('清空图标分类',
                                     'clearing the icon lists')):
        return
    try:
        for cat in gui.category_manager.categories:
            cat['icons'] = []
        if not gui.category_manager._save_categories():
            logger.error('清空图标名单：写盘未生效（文件被占用/只读/杀软拦截）')
            messagebox.showerror(
                i18n.tr('清空未生效', 'Clear Did Not Take Effect'),
                i18n.tr('名单写入磁盘失败（文件可能被占用/只读/被杀软拦截），\n'
                        '界面已刷新但磁盘数据未变，请排查后重启程序重试。',
                        'The lists could not be written to disk (file locked, '
                        'read-only, or blocked by antivirus).\n'
                        'The UI was refreshed but the disk still holds the old '
                        'data - fix the cause and restart the app.'),
                parent=gui.root)
            return
        gui._update_lists()
        gui._log('🗑️ 已清空全部分类的图标名单（图标本身未动）')
    except Exception as e:
        logger.error(f'清空图标名单失败: {e}')
        messagebox.showerror(i18n.tr('错误', 'Error'),
                             f'{i18n.tr("清空失败", "Clear failed")}: {e}',
                             parent=gui.root)


def on_export_layouts(gui):
    """一键把全部已保存布局打包为 zip（日期时间命名）"""
    from tkinter import filedialog
    import zipfile
    layouts = gui.icon_manager.list_layouts()
    if not layouts:
        messagebox.showwarning(i18n.tr('提示', 'Notice'),
                               i18n.tr('还没有已保存的布局',
                                       'No saved layout yet'),
                               parent=gui.root)
        return
    path = _save_dialog(gui, defaultextension='.zip',
        initialfile=i18n.tr(f'桌面布局备份_{datetime.now().strftime("%Y%m%d_%H%M%S")}.zip',
                            f'Desktop_Layout_Backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}.zip'),
        filetypes=[('ZIP', '*.zip')])
    if not path:
        return
    try:
        count = 0
        written = set()
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
            for lay in layouts:
                if not os.path.exists(lay['path']):
                    continue
                # ★ P2-B：zip 内同名去重（极少，但备同名备份时防覆盖）
                base = os.path.basename(lay['path'])
                zf, n = base, 1
                while zf in written:
                    stem, dot, ext = base.rpartition('.')
                    zf = f"{stem}_{n}{dot}{ext}" if dot else f"{base}_{n}"
                    n += 1
                written.add(zf)
                z.write(lay['path'], zf)
                count += 1
        gui._log(f'📦 已导出 {count} 份布局 → {path}')
        messagebox.showinfo(i18n.tr('导出完成', 'Export Finished'),
                        i18n.tr(f'已打包 {count} 份布局',
                                f'{count} layout archive(s) packed'),
                        parent=gui.root)
    except Exception as e:
        logger.error(f'导出布局失败: {e}')
        messagebox.showerror(i18n.tr('错误', 'Error'),
                             f'{i18n.tr("导出失败", "Export failed")}: {e}',
                             parent=gui.root)


def import_layouts_batch(gui, layouts):
    """★ R38：布局批量导入（同名策略，用户拍板）。

    - 本地无同名 → 直接导入
    - 同名且保存时间相同 → 自动覆盖（同一份布局，不打扰）
    - 同名但保存时间不同 → 汇总弹**一次**三选（覆盖/保留现有/都留），
      整批生效——不逐个弹窗
    返回 (成功数, 跳过数, 失败数)。"""
    from datetime import datetime as _dt

    def _norm(ts):
        try:
            return _dt.fromisoformat(str(ts)).isoformat()
        except Exception:
            return str(ts or '')

    local = {}
    try:
        for l in gui.icon_manager.list_layouts():
            if isinstance(l, dict) and l.get('name'):
                local.setdefault(l['name'], []).append(
                    _norm(l.get('timestamp', '')))
    except Exception as e:
        logger.warning(f"[布局导入] 读取现有布局失败(按无同名处理): {e}")

    items = []          # [(ld, name, ts)]
    conflict = set()    # 同名但时间不同的布局名
    _seen_pairs = set() # ★ R72：同批去重——导出文件可能已带上一次重复导入
                        # 产生的 _2 副本（同名同时间戳多条），只导第一条，
                        # 其余静默跳过（不然 _2 会滚雪球越滚越多）
    for ld in layouts:
        if not isinstance(ld, dict):
            continue
        nm = gui.icon_manager.sanitize_layout_name(ld.get('name', ''))
        if not nm:
            continue
        ts = _norm(ld.get('timestamp', ''))
        if (nm, ts) in _seen_pairs:
            continue
        _seen_pairs.add((nm, ts))
        items.append((ld, nm, ts))
        if nm in local and ts not in set(local[nm]):
            conflict.add(nm)

    replace_all = skip_all = False
    if conflict:
        preview = '、'.join(sorted(conflict))
        if len(preview) > 100:
            preview = preview[:100] + '…'
        ans = messagebox.askyesnocancel(
            i18n.tr('同名布局的保存时间不同', 'Same-Named Layouts Differ'),
            i18n.tr(
                f'以下 {len(conflict)} 个布局与现有同名、但保存时间不同：\n'
                f'{preview}\n\n'
                '是 = 覆盖（以导入为准，删除本地同名）\n'
                '否 = 保留现有（跳过这些布局）\n'
                '取消 = 两份都留（导入的改名共存）\n\n'
                '同名且保存时间相同的布局会自动覆盖，不再询问。',
                f'{len(conflict)} layout(s) share a name with an existing one '
                f'but were saved at a different time:\n'
                f'{preview}\n\n'
                'Yes = overwrite (the imported one wins, local same-named '
                'ones are deleted)\n'
                'No = keep what is here (these layouts are skipped)\n'
                'Cancel = keep both (imported ones are stored under a new '
                'name)\n\n'
                'Same name AND same save time are overwritten silently.'),
            parent=gui.root)
        if ans is True:
            replace_all = True
        elif ans is False:
            skip_all = True

    ok = skipped = failed = 0
    for ld, nm, ts in items:
        if skip_all and nm in conflict:
            skipped += 1
            continue
        replace = (replace_all if nm in conflict
                   else (nm in local and ts in set(local[nm])))
        r = gui.icon_manager.add_layout(ld, keep_ts=True,
                                         replace_same=replace)
        if r.get('success'):
            ok += 1
            # ★ R72：local 随导入更新——此前只在建表时读一次盘，同一批里
            # 第二条同名条目查不到刚写入的记录，被当"本地没有"生成 _2 副本
            local.setdefault(nm, []).append(ts)
        else:
            failed += 1
    return ok, skipped, failed


def on_import_layouts(gui):
    """从导出 zip 还原布局（配对"导出zip"，把 zip 里的布局 JSON 导入布局目录）"""
    from tkinter import filedialog
    import zipfile
    path = _open_dialog(gui, title='选择要导入的布局备份 (zip)',
        filetypes=[('ZIP 备份', '*.zip'), ('所有文件', '*.*')])
    if not path:
        return
    # ★ R42：统一格式识别（拿错 zip 给指路提示）
    from io_formats import sniff, mismatch_message, KIND_LAYOUTZIP
    kind = sniff(path)
    if kind and kind != KIND_LAYOUTZIP:
        messagebox.showerror(i18n.tr('文件不匹配', 'File Mismatch'),
                             mismatch_message(KIND_LAYOUTZIP, kind),
                             parent=gui.root)
        return
    ok, failed = 0, 0
    errors = []
    valid = []   # ★ R38：校验通过的布局 dict（循环后统一批量导入）
    # ★ P0-1：入口三道闸——条目数上限 / 单条目大小上限(声明+实读二次确认) / name 白名单
    # name 清洗统一走 icon_manager.sanitize_layout_name（与 add_layout 同一规则）
    MAX_ENTRIES = 200                    # zip 内最多解析的 json 条目数
    MAX_SIZE = 5 * 1024 * 1024           # 单条目解压后 5MB
    seen = 0
    try:
        with zipfile.ZipFile(path, 'r') as z:
            for zi in z.infolist():
                if zi.is_dir() or not zi.filename.lower().endswith('.json'):
                    continue
                base = os.path.basename(zi.filename)
                # 闸①：条目数
                seen += 1
                if seen > MAX_ENTRIES:
                    errors.append(i18n.tr(
                        f'条目超过 {MAX_ENTRIES} 个，已停止解析',
                        f'More than {MAX_ENTRIES} entries - parsing stopped'))
                    break
                # 闸②：解压后大小（先看头部声明，读时再二次确认，防声明被伪造）
                if zi.file_size > MAX_SIZE:
                    failed += 1
                    errors.append(f"{base}: " + i18n.tr(
                        '解压后超过 5MB，跳过',
                        'unpacked size exceeds 5MB - skipped'))
                    continue
                try:
                    raw = z.open(zi).read(MAX_SIZE + 1)
                    if len(raw) > MAX_SIZE:
                        failed += 1
                        errors.append(f"{base}: " + i18n.tr(
                            '实际解压后超过 5MB，跳过',
                            'real unpacked size exceeds 5MB - skipped'))
                        continue
                    data = json.loads(raw)
                    if not isinstance(data, dict):
                        raise ValueError(i18n.tr('不是布局文件',
                                                   'not a layout file'))
                    # 闸③：name 白名单清洗（统一走 icon_manager，双向兜底）
                    raw_name = data.get('name')
                    if not isinstance(raw_name, str):
                        raise ValueError(i18n.tr(
                            'name 字段缺失或不是字符串',
                            'the name field is missing or not a string'))
                    name = gui.icon_manager.sanitize_layout_name(raw_name)
                    if not name:
                        raise ValueError(i18n.tr(
                        'name 字段为空或全为非法字符',
                        'the name field is empty or all characters illegal'))
                    data['name'] = name
                    # ★ R38：校验通过先收集，循环后统一走 _import_layouts_batch
                    # （同名策略：同时间戳自动覆盖 / 不同时间一次三选）
                    valid.append(data)
                except Exception as e:
                    failed += 1
                    errors.append(f"{base}: {e}")
        # ★ R38：统一批量导入（keep_ts + 同名策略，与"导入全部数据"一致）
        if valid:
            ok, skipped, imp_failed = gui._import_layouts_batch(valid)
            if skipped:
                errors.append(i18n.tr(
                    f'{skipped} 份同名布局按你的选择跳过',
                    f'{skipped} same-named layout(s) skipped as you chose'))
            failed += imp_failed
        gui._update_lists()
        msg = (i18n.tr(f'✅ 已导入 {ok} 份布局',
                        f'✅ {ok} layout archive(s) imported')
               if ok else i18n.tr('未导入任何布局', 'Nothing was imported'))
        if failed:
            msg += (f'\n⚠️ {failed} ' + i18n.tr('份失败：\n', 'failed:\n')
                    + '\n'.join(errors[:6]))
        gui._log(f'📥 导入布局完成：成功 {ok}，失败 {failed}')
        if ok:
            messagebox.showinfo(i18n.tr('导入完成', 'Import Finished'),
                                msg, parent=gui.root)
        elif failed:
            messagebox.showwarning(i18n.tr('导入结果', 'Import Result'),
                                   msg, parent=gui.root)
        else:
            # ★ P2-A：补引导文案，避免用户以为选错文件
            messagebox.showinfo(
                i18n.tr('导入完成', 'Import Finished'),
                i18n.tr('zip 中没有找到可导入的布局文件\n'
                        '请确认这是本程序"管理→已保存布局→📦 导出zip"生成的备份。',
                        'No importable layout file was found inside that zip.\n'
                        'Please make sure it is the backup generated by '
                        '"Manage -> Saved layouts -> Export ZIP".'),
                parent=gui.root)
    except Exception as e:
        logger.error(f'导入布局失败: {e}')
        messagebox.showerror(i18n.tr('错误', 'Error'),
                             f'{i18n.tr("导入失败", "Import failed")}: {e}',
                             parent=gui.root)


def _categories_load_failed(gui):
    """★R230（用户 13 批 第 3/4 项顺手修）：分类文件本会话是否"读失败"。

    病根：core/category_manager 在"原文件解析失败且 .bak 也失败"时置
    `_load_failed`，并在 `save_state()` 里禁写（★P1-1）——可是此时内存里是
    **出厂默认分类**。导出走的是这份内存，从来不看这个标记，于是
    "✓ 已导出全部数据"落盘的是一份空/默认备份：用户以为有了退路，拿它去换机
    导入 → 真数据再也找不回。对照 ★R207 NEW3-B4 给 explorer_tabs 加的
    同款防护（`_export_tabs_state` 会剔除外部标记、读失败时不打包该节）。

    ★R313 D1（组一，P0）：上面那段只挡住了 `_load_failed` 那**一路**，而现实里
    **最常见**的损坏形态是"原件被成功另存成 .bak"——那一路 `_load_failed` 留在
    False，内存里换成的仍是出厂默认（`_fallback_defaults=True`）。同一口径
    gui/main_window.py 的 `_live_category_names`（★R281-S6④）早已改成"两个标记
    都要看"，导出端漏同步 ⇒ 写出一份"✓ 已导出"的**默认分类备份**，与 ★R230
    同病同级。判据：`_load_failed`＝禁写盘，`_fallback_defaults`＝这份名单不可信。
    """
    _cm = getattr(gui, 'category_manager', None)
    return bool(getattr(_cm, '_load_failed', False)
                or getattr(_cm, '_fallback_defaults', False))


def _refuse_export(gui, what_zh, what_en):
    """读失败时拒绝导出：只提示、不写文件（返回 True 表示调用方应当中止）。

    ★R230b（S6 验收 P3）：中英两份"是哪个导出"由调用方各给一个词。旧写法只收
    中文 `what`，在 `i18n.tr` **之前**就用 % 插进了两份模板，于是 English 档
    弹窗写成 "would write an empty 全部数据 backup"、日志译文尾巴塞同一份中文
    （用户读成"这软件英文没做完"）。日志侧仍是规范中文短句——文件日志不分双语，
    显示层由 utils/log_i18n 按整句翻（词典里两种 what 各一条完整规则）。
    """
    if not _categories_load_failed(gui):
        return False
    _cm = getattr(gui, 'category_manager', None)
    # ★R231（用户 05 批实拍）：旧文案一口咬定"没有可用备份"，可用户目录里就躺着
    # .bak（而且那份 .bak 自己也坏了）——指路要先验 .bak 能不能读。
    _bak_zh, _bak_en = _bak_hint(getattr(_cm, 'config_dir', ''),
                                 'categories.json')
    _exit = (
        '现在导出只会写出一份%s空备份，已中止。\n'
        '不用手工改文件就能自救：在本页直接「📥 导入分类」或「清空」——'
        '程序会先把损坏原件另存留底再写入。\n%s\n'
        '处理完（分类列表恢复正常）后再导出。' % (what_zh, _bak_zh),
        'Exporting now would write an empty %s backup - aborted.\n'
        'You can recover without a text editor: use "Import Categories" or a '
        'Clear action on this page - the app sets the corrupted original '
        'aside first, then writes.\n%s\n'
        'Re-export once the category list looks normal again.'
        % (what_en, _bak_en))
    msg = i18n.tr(
        '分类数据本会话加载失败（config/categories.json 内容已损坏），'
        '内存里是出厂默认值。\n\n'
        '%s' % _exit[0],
        'Category data failed to load in this session '
        '(config/categories.json is corrupted), so memory holds factory '
        'defaults.\n\n%s' % _exit[1])
    gui._log('⏹ 分类加载失败，%s导出已中止（避免写出空备份）' % what_zh.strip())
    messagebox.showwarning(i18n.tr('导出已中止', 'Export aborted'), msg,
                           parent=gui.root)
    return True


# ★R231（用户 05 批 item2）：自救闸两条"仍未解禁"日志（中文是唯一真源，
# English 显示层见 utils/log_i18n 短语典）
_RECOVER_LOG_CANCEL = '⏹ 已取消：损坏文件未处理，本会话仍禁止写盘'
_RECOVER_LOG_FAIL = '⚠ 损坏文件留底失败（被占用/只读），本会话仍禁止写盘'
_RECOVER_LOG_CATS = ('🛠 损坏的分类文件已另存留底，本会话写盘已解禁',
                     '🛠 Corrupted category file set aside; disk writes '
                     're-enabled this session')
_RECOVER_LOG_GROUPS = ('🛠 损坏的文件夹组配置已另存留底，本会话写盘已解禁',
                       '🛠 Corrupted folder-group config set aside; disk '
                       'writes re-enabled this session')


def _bak_hint(config_dir, name):
    """★R231（用户 05 批实拍）：让人去"把 .bak 改名回来"之前，先验 .bak 自己能不能读。

    用户机上 `config/categories.json.bak` 尾部同样被加了 `xxx`（两个文件都坏），
    原提示照着做只会再坏一次、还以为是自己操作错了。返回 (中文, 英文) 一句指路。
    """
    p = os.path.join(str(config_dir or ''), name + '.bak')
    if not config_dir or not os.path.exists(p):
        return ('同目录没有 .bak 备份，所以程序会先把损坏原件另存留底再写入。',
                'There is no .bak copy in the same folder, so the app sets the '
                'corrupted original aside before writing.')
    try:
        json.loads(Path(p).read_text(encoding='utf-8'))
    except Exception:
        return ('注意：同目录的 .bak 同样读不动（它也坏了），改名回去仍是坏的'
                '——直接继续即可。',
                'Note: the .bak in the same folder fails to parse too (it is '
                'corrupted as well), so renaming it back will not help - '
                'just continue.')
    return (f'想先找回旧数据：点「否」，把同目录的 {name}.bak 改名回 {name} '
            '后重启程序。',
            f'To get your old data back first: choose No, rename {name}.bak '
            f'in the same folder back to {name} and restart the app.')


def _recover_corrupt_write(gui, rescue, action, fname, log_ok, extra) -> bool:
    """数据文件"本会话读不动"时的自救闸（★R231，True=可以继续本次操作）。

    旧写法：`showerror('本次运行禁止写盘…请排查后重启程序')` + 中止 ⇒ 用户把
    categories.json 改坏之后，清空/导入四个入口全被拒（05 批实拍图4、图5），
    唯一出路是手改 JSON——"懒得整文件、直接清空再导入"的用户等于被锁死。
    ★P1-1 / ★R190 的闸本意是"别用内存默认值**偷覆盖**盘上真数据"，而清空/导入
    是用户**主动提供完整替换内容**，闸关错了对象。
    新写法：一次确认里讲清"会先把损坏原件另存留底"；确认 ⇒ 留底成功才解禁；
    取消或留底失败 ⇒ 磁盘一个字节不动、仍保持禁写（不变量没被削弱）。
    action / fname / log_ok / extra 都是 (中文, 英文) 二元组——★R230b 口径：
    中英两份模板各插自己那份，绝不把中文插进英文模板。
    """
    a_zh, a_en = action
    f_zh, f_en = fname
    ok_zh, _ok_en = log_ok
    x_zh, x_en = extra
    if not messagebox.askyesno(
            i18n.tr('数据文件已损坏，先留底再处理',
                    'Corrupted Data File - Set It Aside First'),
            i18n.tr(
                '%s 本会话读取失败（文件内容已损坏），内存里现在是默认值。\n\n'
                '继续「%s」会先做一步：把损坏的原件另存为留底文件'
                '（%s.corrupt_时间戳，同目录已有的 .bak 不动），'
                '然后按你的操作写入新内容。\n\n'
                '%s\n确定继续？' % (f_zh, a_zh, f_zh, x_zh),
                '%s failed to load this session (its content is corrupted), '
                'so memory holds defaults.\n\nContinuing with "%s" first sets '
                'the corrupted original aside as a safety copy '
                '(%s.corrupt_<timestamp>; an existing .bak is left alone), '
                'then writes whatever your action produces.\n\n%s\n\n'
                'Continue?' % (f_en, a_en, f_en, x_en)),
            parent=gui.root):
        gui._log(_RECOVER_LOG_CANCEL)
        return False
    ok, detail = rescue()
    if not ok:
        messagebox.showerror(
            i18n.tr('无法留底，操作已中止', 'Cannot Set Aside - Aborted'),
            i18n.tr(
                '损坏的原件没能另存留底（文件被占用或只读），已中止，'
                '未改动任何数据。\n请关掉占用该文件的程序（或去掉只读属性）后重试。',
                'The corrupted original could not be moved to a safety copy '
                '(the file is locked or read-only), so nothing was changed.\n'
                'Close whatever is holding the file (or clear its read-only '
                'flag) and retry.'),
            parent=gui.root)
        gui._log(_RECOVER_LOG_FAIL)
        return False
    gui._log(ok_zh + (f' [{os.path.basename(detail)}]' if detail else ''))
    return True


def _recover_categories(gui, action, log_ok=None) -> bool:
    """★R231：分类文件（config/categories.json）损坏态的自救闸，4 个入口共用。

    正常态直接返回 True，**一次确认都不多弹**（不给普通用户加打扰）。
    """
    cm = getattr(gui, 'category_manager', None)
    if not getattr(cm, '_load_failed', False):
        return True
    return _recover_corrupt_write(
        gui, lambda: cm.rescue_corrupt_file(), action,
        ('config/categories.json', 'config/categories.json'),
        log_ok or _RECOVER_LOG_CATS,
        _bak_hint(getattr(cm, 'config_dir', ''), 'categories.json'))


def _recover_folder_groups(gui, state, action) -> bool:
    """★R231：文件夹组（config/explorer_tabs.json）读取失败态的自救闸。"""
    if not state.get('_load_failed'):
        return True
    from core import explorer_tabs
    return _recover_corrupt_write(
        gui, lambda: explorer_tabs.rescue_corrupt_file(APP_DIR / 'config',
                                                       state),
        action, ('config/explorer_tabs.json', 'config/explorer_tabs.json'),
        _RECOVER_LOG_GROUPS,
        _bak_hint(APP_DIR / 'config', 'explorer_tabs.json'))



_DL_GATE_ATTR = {'history': '_load_failed',
                 'ignore': '_ignore_load_failed',
                 'forgotten': '_forgotten_load_failed'}
_DL_SECTION_FILE = {'history': 'download_history.json',
                    'ignore': 'download_ignore.json',
                    'forgotten': 'download_forgotten.json'}


def _dl_sections_ok(gui, section='history') -> bool:
    """下载三节能不能带进备份包：**各节查各自己的禁写闸**。

    ★R264 Z5 原本只查 `history` 那把 `_load_failed`，可忽略名单/黑名单在
    ★R283 / ★R285-N6 各立了**独立**的闸（`_ignore_load_failed` /
    `_forgotten_load_failed`）——共用一把会两头错：history 没事、忽略名单读坏时
    包里照样写出 `'download_ignore': []`，"✓ 已导出"谎报用户没有任何忽略规则
    （换机后 R177"忽略了又出现"复发）；反过来 history 读坏时把另外两节也闸掉
    （误伤：那两份内存是好的）。★R313 D4（分诊 P2，顺手门）。

    没有 download_tracker（老版本/未初始化）时保持旧语义：这一节不带。
    """
    dt = getattr(gui, 'download_tracker', None)
    if dt is None:
        return False
    return not getattr(dt, _DL_GATE_ATTR.get(section, '_load_failed'), False)


def _prefix_rules_to_import(data):
    """包里的 `prefix_rules` 能不能往本机写：**空的一节不覆盖对方**。

    ★R313 D3（P1）：源端 `_read_prefix_rules()` 读失败/没文件时返回 `{}`，
    而 `isinstance({}, dict)` 为真 ⇒ 旧判据把目标机自建的前缀规则整份清空。
    同函数的 `rules`（:1120 `and data['rules']`）与 highlight/docks
    （★P2-N15 改 None）都是修过的，前缀这一处漏了。

    返回：非空 dict 原样交回；其余（缺键 / 空 dict / 列表字符串这类脏值）
    一律 None＝这一节不写。
    """
    pr = data.get('prefix_rules')
    return pr if isinstance(pr, dict) and pr else None


def _pub_download_records(records) -> list:
    """把下载记录整理成**能往外给**的样子：剔掉 `_` 前缀的内部标记键。

    ★R287-N24：`_unverified`（★R264 Z4 在导入端给"本机暂时没有的文件"打的记号）
    是我们这一台机器自己的核对状态，不是数据。原来 `list(...records)` 照原样导出，
    换机再导回来就带着上一台机器的核对结论跑（而判据本应在"打开"那端现读）。
    口径与同文件 `app_settings` 那条（★P2-N19 剔 `_migrated_close_restore`）一致：
    **内部标记不出门**。

    非 dict 的条目原样留着（不吞数据、也不在这里判形状——判形状是导入端的事），
    所以传进来几条就出去几条，条数对得上才对得起"导出成功"那句话。
    """
    out = []
    for r in records or []:
        if isinstance(r, dict):
            out.append({k: v for k, v in r.items()
                        if not (isinstance(k, str) and k.startswith('_'))})
        else:
            out.append(r)
    return out


def on_export_all_data(gui):
    """导出全部数据（分类/规则/设置/快捷键/高亮/文件夹组）到单个 JSON"""
    from tkinter import filedialog
    if _refuse_export(gui, '全部数据', 'all-data'):
        return
    path = _save_dialog(gui, defaultextension='.json',
        initialfile=i18n.tr(f'桌面管理数据_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json',
                            f'Desktop_Manager_Data_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'),
        filetypes=[('JSON', '*.json')])
    if not path:
        return
    try:
        import json as _json
        from core import explorer_tabs
        from core.category_manager import CategoryManager

        def _safe_load_json(p, default):
            """读 JSON，文件不存在或解析失败时返回 default（不抛异常）"""
            try:
                if (APP_DIR / p).exists():
                    return _json.loads(
                        (APP_DIR / p).read_text(encoding='utf-8'))
            except Exception as _e:
                logger.warning(f'导出时读取 {p} 失败，用空值替代: {_e}')
            return default

        def _safe_load_abs(p):
            """★R322 J5：按**绝对路径**读一份 json（盒账本在盒根，不在 APP_DIR 下）。
            没有/读不动 ⇒ None ⇒ 这一节不带，导入端就不会把对方机器的账本写空。"""
            try:
                if os.path.isfile(str(p)):
                    return _json.loads(Path(str(p)).read_text(encoding='utf-8'))
            except Exception as _e:
                logger.warning(f'导出时读取 {p} 失败，这一节不带: {_e}')
            return None

        from core import visibility_router as _bx_vr
        from core import desktop_box as _bx_db

        _dl_ok = {'history': _dl_sections_ok(gui, 'history'),
                  'ignore': _dl_sections_ok(gui, 'ignore'),
                  'forgotten': _dl_sections_ok(gui, 'forgotten')}
        if not all(_dl_ok.values()):
            # ★R313 D4：归因要说**具体哪节**读不动——旧写法三节共用一把闸，于是一律
            # 怪 download_history.json，而 history 可能好好的、坏的是忽略名单（用户
            # 照着这句话去查文件，查不出任何名堂＝★R283/★R285-N6 那两把闸没接进导出端）。
            if getattr(gui, 'download_tracker', None) is None:
                gui._log('⚠ 本次会话没有下载追踪器（未初始化），下载三节不进备份包；'
                         '盘上文件没事，重启程序即可')
            else:
                _dl_bad_names = ', '.join(
                    _DL_SECTION_FILE[k]
                    for k in ('history', 'ignore', 'forgotten') if not _dl_ok[k])
                gui._log('⚠ 本机这几份下载账本读不动且留底失败（内存是空账），'
                         '本次导出**不含**：%s；找回请先关闭占用该文件的程序再重启'
                         % _dl_bad_names)

        # ★R279-⑦（产品负责人 09-30 04 批点名）：排行组**不进**全部导出。
        # 它是"榜单第 M~N 名"的一次性快照，榜单天天在变，搬到另一台机器只会得到
        # 一份过期的名单（换机后还得手动去删那个组）。同一族"这一节不带"的先例：
        # rules（★R108 退役、键保留恒空）、icon_manifest（派生数据、导入端不还原）。
        _cats_out, _rg_skipped = CategoryManager.without_rank_groups(
            gui.category_manager.categories)
        if _rg_skipped:
            logger.info(f'[导出全部] 已跳过 {len(_rg_skipped)} 个排行组'
                        f'（临时功能，不跨机器搬）: '
                        f'{[c.get("name") for c in _rg_skipped][:8]}')

        export = {
            'version': '1.0',
            'exported': datetime.now().isoformat(),
            'categories': _cats_out,
            'rules': [],   # ★ R108 规则体系退役：键保留兼容旧导入端，恒空
            # ★ R119：下载记录一并导出（用户 2026-09-17 要求）
            # ★R264 审计 Z5：本机那份 download_history.json 读不动**且留底失败**
            # 的会话里，内存是空账——照原样导出会写出一份"✓ 已导出"的空下载
            # 记录（★R230 的病：备份看着成功，其实是兜底值）。这一节**不带**，
            # 导入端 `isinstance(list)` / `data.get(...) or []` 天然跳过，
            # 既不会写空，也不会把对方机器上的真数据清空。
            'download_history': (_pub_download_records(gui.download_tracker.records)
                                 if _dl_ok['history'] else None),
            # ★R229（用户 01 批 item1）：资源页"忽略规则 + 被忽略条目黑名单"
            # 一并导出。此前只带 download_history，换机导入后同步层会把自己
            # "忽略此条"过的下载原样复活（R177 的坑换个机器重演）。
            # 撤回栈 download_undo 故意不带：那是"刚删了某个文件想找回"的
            # 本机会话栈，跨机路径无意义。
            'download_ignore': (list(gui.download_tracker.list_ignore())
                                if _dl_ok['ignore'] else None),
            'download_forgotten': (list(gui.download_tracker.list_forgotten())
                                   if _dl_ok['forgotten'] else None),
            'prefix_rules': gui._read_prefix_rules(),
            # ★ P2-N19：剔除内部字段（_ 前缀，如 _migrated_close_restore）
            # 避免导入端把这些一次性迁移标记又带回去，导致迁移逻辑反复跑
            'app_settings': {k: v for k, v in gui._app_settings.items()
                              if not k.startswith('_')},
            'hotkeys': gui._hotkey_cfg,
            # ★ P2-N15：默认改 None——原 {} 让导入端 'docks' in data 永真
            # （空字典也 in），会把对方的 docks.json 写空。None 时导入端
            # 判 isinstance(x, dict) and x 才真写，空数据不覆盖
            'highlight': _safe_load_json('config/highlight.json', None),
            'docks': _safe_load_json('config/docks.json', None),
            # ★R207 NEW3-B4：读取失败标记不外泄——带进备份包会让导入
            # 端 save_state 拒写（报告“成功”实际没进）；读失败时干脆
            # 不打包该节（None=导入端不覆盖）
            'explorer_tabs': _export_tabs_state(),
            # ★ S 段/B18（用户发现缺漏）：桌面布局存档此前不在全部数据里，
            # 补上；连同使用统计、分类点击、分类归属清单（人工可读文本）
            'layouts': gui._collect_layout_archives(),
            'usage_stats': gui.usage_stats.export_dict(),
            'clicks': gui.click_tracker.get_all_clicks(),
            # ★ R185（用户 19 批）：条件监视一并导出；计划任务（用户）
            # 是系统级条目，不随数据导出/导入
            'monitors': _safe_load_json('config/monitors.json', None),
            'icon_manifest': gui._build_icon_manifest_text(),
            # ★R322 J5（用户 77 批「程序是不是应该对盒子里面的现有的东西有个记录」）：
            # 盒子那两本账此前**根本不在**导出清单里 ⇒ 换机器/重装后盒里躺着图标、
            # 账本却说一枚都不是自己搬的，散回链永远不认（win11 R038/R039 那起 P0 的
            # 另一半）。两本账的落点在盒根（★R322 改的，不在 APP_DIR 下），
            # 所以按绝对路径读；读不到/读不动 ⇒ None，导入端 None 不覆盖（★P2-N15 口径）。
            'hidden_box_ledger': _safe_load_abs(_bx_vr.ledger_path()),
            'desktop_box_manifest': _safe_load_abs(_bx_db.manifest_path()),
        }
        if not atomic_write_json(path, export, indent=2):
            raise OSError('写盘失败（详见日志）')
        gui._log(f'✓ 已导出全部数据 → {path}')
    except Exception as e:
        logger.error(f'导出全部数据失败: {e}')
        messagebox.showerror(i18n.tr('错误', 'Error'),
                             f'{i18n.tr("导出失败", "Export failed")}: {e}',
                             parent=gui.root)


# =====================================================================
# ★R233（用户 08 批 item4「先做了，因为要丢数据」，★R230 挂账 2）
# 「导入全部数据」的中途失败回滚
# =====================================================================
# 回滚覆盖面：只这两份子目录里的真 JSON —— 它们就是"导入会改到的个人数据"
# 的全部落盘位置（分类/设置/前缀规则/高亮/悬浮条/文件夹组/监视/统计）。
_ROLLBACK_SUBDIRS = ('config', 'data')
# 单文件上限：这套配置的容量假设是"十几 KB 的 JSON"。超上限说明目录被动过
# 手脚（或被塞了二进制），宁可不滚也不能把内存吃掉。
_ROLLBACK_MAX_BYTES = 6_000_000


def _box_ledger_targets() -> list:
    """★R322 K4：本次导入**能不能**碰盒根那两本账 ⇒ `[(键, 中文名, 绝对落点)]`。

    为什么要有这一道：这两节的落点走的是 `ledger_path()` / `manifest_path()`
    （不给 `app_dir` ⇒ 程序自己认的家＝用户家目录里的盒根），而历轮老式门禁
    **只把本模块的 `APP_DIR` 换成临时目录**（如 `test_r233_import_safety.py:430`），
    盒根那一头没换 ⇒ 一个带这两节的测试包就直接写进**用户真盒根**。

    口径照抄 ★R322 J5b 那第三道闸门（`core/desktop_box._redirects_mismatched`，
    同一件事只留一份判据）：**盒根与程序目录只换了一头 ⇒ 整节拒写**；两边都没换
    （用户真机的生产链）、两边都换（`测试_ui/gate_sandbox.py` 整套沙箱）⇒ 照写。
    落点算不出来（导入期模块还没起来等）也按拒写处理——宁可不写不能猜。
    """
    try:
        from core import visibility_router as _vr
        from core import desktop_box as _db
        if _db._redirects_mismatched():
            return []
        return [('hidden_box_ledger', '盒登记账本', str(_vr.ledger_path())),
                ('desktop_box_manifest', '盒成员账本', str(_db.manifest_path()))]
    except Exception as e:
        logger.warning('[导入全部] 盒账本落点算不出来（这两节本次跳过）: %s', e)
        return []


def _snapshot_personal_config(app_dir, max_bytes: int = _ROLLBACK_MAX_BYTES,
                              extra_paths=()) -> tuple:
    """抓一份"改动前的个人数据"快照（放在内存里，不再落第二份盘）。

    返回 `({相对路径: bytes}, 快照时已存在的 .json 集合)`，相对路径形如
    `config/categories.json`。第二份集合是 ★R264 审计 Z3 补的：**没收进内容
    ≠ 不存在**（超上限、读不动的都属这一类），还原端只允许删"集合里没有的"。
    选材口径（每一条都是踩过的坑）：
      · 只收 `_ROLLBACK_SUBDIRS` 下的 `*.json` —— 导入改到的就是这一层；
      · `.bak` / `.corrupt_*` 一律不收：那是 ★R231 给用户留的自救出口，
        回滚把它们覆盖掉等于把出口二次焊死；
      · 图片（theme.jpg 等）不收：导入不写它们；
      · 超过 `max_bytes` 跳过并留日志（见上）。
    快照里**没有**、且**快照时也不存在**的档 = 这次导入新建的 ⇒ 回滚时删掉。

    ★R322 K1：`extra_paths` 是**绝对路径**的那一批（盒根里那两本账）。为什么单开
    这一头：账本从程序目录搬进盒根（J5）之后，"导入会改到的个人数据"不再只落在
    `<app_dir>/config|data` 里，而回滚覆盖面还停在旧地图上 ⇒ 中途异常时分类都滚
    回去了、盒登记却停在包里那份，本机"哪些盒是本系统搬的"被静默换掉＝R038/R039
    同一族。口径与上面一致：只收 `.json`、`*.已搬家` 那种留底永不进快照（它是
    ★R231 的自救出口）、不存在的不进 `seen`（这样"本次新建"才删得掉）。
    """
    snap = {}
    seen = set()
    root = Path(str(app_dir))
    for sub in _ROLLBACK_SUBDIRS:
        d = root / sub
        try:
            if not d.is_dir():
                continue
            names = sorted(os.listdir(d))
        except Exception as e:
            logger.warning(f'[导入回滚] 无法列出 {sub}/（该目录本次不纳入快照）: {e}')
            continue
        for fn in names:
            rel = f'{sub}/{fn}'
            p = d / fn
            try:
                if not fn.lower().endswith('.json') or not p.is_file():
                    continue      # .bak / .corrupt_* / 图片 / 目录都从这条出去
                # ★R264 审计 Z3：**先记账再决定收不收内容**。旧写法只返回
                # snap，还原端拿"不在 snap 里"当"本次导入新建"⇒ 被这两条
                # continue 跳过的既有个人数据会被当场删掉（本该保数据的回滚
                # 反而删数据，和 :806 注释的承诺正好相反）。
                seen.add(rel)
                size = p.stat().st_size
                if size > max_bytes:
                    logger.warning(f'[导入回滚] {rel} 有 {size} 字节，'
                                   '超过快照上限，本次不纳入回滚')
                    continue
                snap[rel] = p.read_bytes()
            except Exception as e:
                # 读不动的档不进快照 ⇒ 回滚也不会去碰它（不假装能还原）
                logger.warning(f'[导入回滚] 快照读取 {rel} 失败（该文件不参与回滚）: {e}')
    for ep in (extra_paths or ()):
        rel = str(ep)
        try:
            if not os.path.basename(rel).lower().endswith('.json'):
                continue                       # `*.已搬家` 那类留底：不进快照也不碰
            if not os.path.isfile(rel):
                continue                       # 现在没有 ⇒ 本次若新建就归回滚删
            seen.add(rel)
            size = os.path.getsize(rel)
            if size > max_bytes:
                logger.warning(f'[导入回滚] {rel} 有 {size} 字节，'
                               '超过快照上限，本次不纳入回滚')
                continue
            with open(rel, 'rb') as _fh:
                snap[rel] = _fh.read()
        except Exception as e:
            logger.warning(f'[导入回滚] 快照读取盒账本 {rel} 失败（不参与回滚）: {e}')
    return snap, seen


def _restore_personal_config(snap: dict, app_dir, seen: set = None,
                             extra_paths=()) -> tuple:
    """按快照把个人数据还原回去。返回 (成功份数, 失败明细 [(rel, 原因)])。

    两条动作：快照里有的 ⇒ 逐字节写回；**快照时不存在**而现在多出来的
    `.json` ⇒ 是这次导入新建的 ⇒ 删掉（否则用户拿到"半新半旧"）。
    非 `.json`（留底、图片）一律不动。
    ★R264 审计 Z3：删除判据从"不在 snap 里"改成"不在 seen 里"——超上限/
    读不动而被跳过收内容的既有数据在 snap 里没有，旧判据会把它当新建删掉。
    `seen` 缺省（旧调用方/旧测试）时退化成"只删 snap 里没有的"＝旧行为，
    但会留一条 warning 说明本次没带存在集合。
    ★R322 K1：`extra_paths`（绝对路径，盒根那两本账）同样两头都要管——快照里有这份
    ⇒ 逐字节写回**原绝对位置**（不能拿去 config/data 里找），快照里没有而现在有了
    ⇒ 是本次导入新建的 ⇒ 删掉。少这一头就是"导入到一半炸了、盒登记却停在包里那份"。
    """
    ok, bad = 0, []
    root = Path(str(app_dir))
    snap = snap or {}
    if seen is None:
        seen = set(snap)
        logger.warning('[导入回滚] 未传"快照时已存在集合"，'
                       '超大/读不动的既有档可能被误判为新建')
    for rel, raw in snap.items():
        # ★R322 K1：盒根那两本账的键是绝对路径 ⇒ 直接回原位（走 root 拼会拼到
        # 程序目录里去，那份根本不在那儿）。
        p = Path(rel) if os.path.isabs(rel) else root / rel.replace('/', os.sep)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(raw)
            ok += 1
        except Exception as e:
            bad.append((rel, str(e)))
            logger.error(f'[导入回滚] 还原 {rel} 失败: {e}')
    for sub in _ROLLBACK_SUBDIRS:
        d = root / sub
        try:
            if not d.is_dir():
                continue
            extras = [fn for fn in os.listdir(d)
                      if fn.lower().endswith('.json') and f'{sub}/{fn}' not in seen]
        except Exception:
            continue
        for fn in extras:
            try:
                (d / fn).unlink()
                ok += 1
                logger.info(f'[导入回滚] 已删除本次新建的 {sub}/{fn}')
            except Exception as e:
                bad.append((f'{sub}/{fn}', str(e)))
                logger.error(f'[导入回滚] 删除本次新建的 {sub}/{fn} 失败: {e}')
    # ★R322 K1：盒根那本若"导入前没有、这次新建" ⇒ 一并删掉，不许在用户家目录里
    # 留一份外来假账（下一轮启动它就变成"这些盒是本系统搬的"凭据）。
    for ep in (extra_paths or ()):
        rel = str(ep)
        try:
            if not os.path.basename(rel).lower().endswith('.json'):
                continue
            if rel in snap or rel in seen:
                continue
            if os.path.isfile(rel):
                os.remove(rel)
                ok += 1
                logger.info(f'[导入回滚] 已删除本次新建的盒账本 {rel}')
        except Exception as e:
            bad.append((rel, str(e)))
            logger.error(f'[导入回滚] 删除本次新建的盒账本 {rel} 失败: {e}')
    return ok, bad


def _resync_memory_after_rollback(gui):
    """回滚之后把内存拉回盘上的样子（每步独立兜底，一步坏不拦其余）。

    为什么必须回灌：`on_import_all_data` 在抛异常之前已经改过
    `category_manager.categories` 与 `_app_settings`。只滚盘不滚内存的话，
    用户随手点一下"✅ 应用"就把半截状态又写回磁盘，等于回滚白做。
    """
    cm = getattr(gui, 'category_manager', None)
    if cm is not None:
        try:
            cats = cm._load_categories()      # ★ 它只在迁移分支里自赋值，返回值才是准的
            if isinstance(cats, list):
                cm.categories = cats
        except Exception as e:
            logger.warning(f'[导入回滚] 分类内存回灌失败（重启后正常）: {e}')
    try:
        s = gui._load_app_settings()
        if isinstance(s, dict):
            gui._app_settings.clear()
            gui._app_settings.update(s)
            gui._apply_app_settings()
    except Exception as e:
        logger.warning(f'[导入回滚] 设置内存回灌失败（重启后正常）: {e}')
    for fn in ('_update_lists', '_rebuild_sticky_grid', '_refresh_explorer_list',
               '_resync_startup_open_vars'):
        try:
            getattr(gui, fn)()
        except Exception:
            pass   # 纯刷新类：失败不影响数据一致性，重启自然好
    # ★R262 批14（item4）：回滚把 docks.json 写回去了，条上也要跟着回旧槽位
    try:
        _d = getattr(gui, 'dock', None)
        if _d is not None:
            _d.slots = _d._load_slots()
            _d._repaint_if_visible()
    except Exception as _e:
        # ★R262 S6-F-D：吞异常可以，静默不行（★R230b 口径）——正向路径
        # :1176 是有 warning 的，回滚这条没有就等于"条上没回旧槽位"零痕迹。
        logger.warning(f'[导入回滚] 悬浮条槽位回灌失败（重启后正常）: {_e}')


# ★R233（用户 08 批 item1）：完成弹窗里"哪一节没生效"的节名要能显示成英文。
# `_imp` 的键与 `_bad`/`_skip` 的元素是**内部逻辑 ID**（中文，一处改就要改
# 六处写入点），只在弹窗显示时查表翻 —— 与悬浮条槽位名、io_formats 的
# KIND_* 同一套路。查不到退回原值，绝不因为漏了一条就抛。
_SECTION_EN = {
    '前缀规则': 'prefix rules', '高亮设置': 'highlight settings',
    '悬浮条槽位': 'dock slots', '文件夹组': 'folder groups',
    # ★R287-N26 附带（★R230b 口径）：凡是能进 `_bad`/`_skip` 的节名都必须在这里
    # 有英文对位，否则 English 档的完成弹窗会露中文尾巴。
    #   '下载记录' 是 ★R264 Z5 就在写、却一直没配翻译的**潜伏漏网**（要拒写那次
    #   正好撞上英文档才会现形）；'设置项' 是本批新加的（M4 门禁当场抓到）。
    # 判据见 测试_ui/test_r287_p3_sweep.py T10（穷举 _bad/_skip 的取值逐条查表）。
    '下载记录': 'download history', '设置项': 'settings',
    # ★R322 K4：盒子那两本账既可能进 `_bad`（写盘没生效）也可能进 `_skip`
    #   （本地那份读不动没留底 / 落点被闸挡住），漏了翻译就是 English 档露中文。
    '盒登记账本': 'box registration ledger',
    '盒成员账本': 'box membership ledger',
}


def _sec_en(name: str) -> str:
    return _SECTION_EN.get(name, name)


def on_import_all_data(gui):
    """导入全部数据：分类/规则/前缀规则/设置/快捷键/高亮/文件夹组"""
    from tkinter import filedialog
    path = _open_dialog(gui, filetypes=[('JSON', '*.json')])
    if not path:
        return
    _snap = None      # ★R233：改动前快照；None=还没走到写盘那一步（无需回滚）
    _snap_seen = set()   # ★R264 Z3：快照时已存在的 .json 集合（删除判据用）
    _box_paths = []      # ★R322 K1：盒根那两本账的绝对落点（本次允许写的那几本）
    try:
        # ★ R42：统一格式识别——拿错文件给"指路"提示（各入口同款）
        from io_formats import sniff, mismatch_message, KIND_FULL
        kind = sniff(path)
        if kind and kind != KIND_FULL:
            messagebox.showerror(i18n.tr('文件不匹配', 'File Mismatch'),
                                 mismatch_message(KIND_FULL, kind),
                                 parent=gui.root)
            return
        import json as _json
        data = _json.loads(Path(path).read_text(encoding='utf-8'))
        # 兜底校验（sniff 不认识的文件走这里）
        if not isinstance(data, dict):
            messagebox.showerror(
                i18n.tr('错误', 'Error'),
                i18n.tr('文件格式不对（这不是"导出全部数据"生成的文件）。\n'
                        '请选择 数据管理 → 📤 导出全部数据 生成的 JSON。',
                        'Wrong file format (this was not generated by '
                        '"Export All Data").\nPlease pick the JSON produced by '
                        'Data Management -> Export All Data.'),
                parent=gui.root)
            return
        if not isinstance(data.get('categories'), list):
            messagebox.showerror(
                i18n.tr('错误', 'Error'),
                i18n.tr('文件格式不对（缺少 categories）',
                        'Wrong file format (the categories field is missing)'),
                parent=gui.root)
            return
        if not messagebox.askyesno(
                i18n.tr('确认导入', 'Confirm Import'),
                # ★R249 补充1/D4：原文宣传"覆盖规则"，而规则体系 ★R108 就退役了
                # （进包的是前缀规则）——措辞跟着改口，不再承诺不存在的东西。
                i18n.tr('将覆盖当前的全部分类、前缀规则、设置、快捷键、高亮、'
                        '文件夹组，以及盒子那两本账（盒登记账本、盒成员账本，'
                        '记的是哪些盒由本机搬进去、每只盒里有什么）。\n'
                        '多数项已即时生效，个别面板未刷新时重启程序即可。\n'
                        # ★R240（#138 → 用户 33 批点头）：覆盖面要说全——
                        # 锁屏密码只在内存（gui/lock_screen.py:138），计划任务是
                        # 系统级条目、压根不进包（导出字典里没有这一节）。此前
                        # 只有"条件监视停用"那条显形，用户换机后会以为密码也带过去了。
                        # ★R322 K2（批77 自查）：★R322 J5 之后"盒子两本账"也进了包，
                        # 覆盖承诺却还停在旧清单上 ⇒ 换机导入会把本机"哪些盒是我搬的"
                        # 静默换成包里那份（R038/R039 读成的就是"图标丢了"）。
                        '不含：锁屏密码（只在本机本次运行内有效）、'
                        '计划任务（需在本机重建）。\n确定？',
                        'This overwrites your current categories, prefix rules, '
                        'settings, hotkeys, highlight colors, folder groups and '
                        'the two desk-box ledgers (which boxes this machine put '
                        'away, and what each box holds).\nMost of it takes effect '
                        'immediately; restart the app if a panel still looks old.\n'
                        'Not included: the lock-screen password (only valid for '
                        'this run on this machine) and scheduled tasks '
                        '(recreate them on this machine).\nContinue?'),
                parent=gui.root):
            return
        # ★R233（★R230 挂账 2）：用户点头之后、**任何写入之前**抓快照。
        #   放确认之前会白抓一份（取消时没什么可滚），放后面原件已经丢了。
        #   `_snap` 为 None 表示"还没走到要写盘的地步"⇒ 异常时无需回滚。
        # ★R322 K1：快照要盖住盒根那两本账（它们不在 <app_dir>/config|data 里）。
        # ★R322 K4：`_box_tgt` 一次算完，下面写盘那一节与回滚都用这一份决定，
        #   免得两处判据走岔（一边写一边不写＝半新半旧）。
        _box_tgt = _box_ledger_targets()
        _box_paths = [p for _, _, p in _box_tgt]
        _snap, _snap_seen = _snapshot_personal_config(APP_DIR, extra_paths=_box_paths)
        logger.info(f'[导入回滚] 已为本次导入建立改动前快照'
                    f'（{len(_snap)} 个数据文件）')
        # 分类（逐项 normalize + 过滤畸形条目，审查修复）
        # ★R279-⑦（产品负责人 09-30 04 批点名）：包里的**排行组**不落地。
        # 它是"榜单第 M~N 名"的一次性快照，换一台机器那份名单就过期了，
        # 用户还得手动去删。与导出端用的是同一个判据
        # （CategoryManager.without_rank_groups / is_rank_group），两头都挡才不漏：
        # 旧版本导出的包（里面带 rank_source）拿到今天导入，也会在这里被剔掉，
        # 不会把本机分类表污染成一个死组。
        from core.category_manager import CategoryManager as _CM
        cats, _rg_in = [], []
        for c in data['categories']:
            if isinstance(c, dict) and c.get('name'):
                if _CM.is_rank_group(c):
                    _rg_in.append(c.get('name'))
                    continue
                gui.category_manager._normalize(c)
                cats.append(c)
        if _rg_in:
            logger.info(f'[导入全部] 包里 {len(_rg_in)} 个排行组没落地'
                        f'（临时功能，不跨机器搬）: {_rg_in[:8]}')
        if not cats:
            messagebox.showerror(
                i18n.tr('错误', 'Error'),
                i18n.tr('导入文件里没有有效的分类'
                        + (f'（{len(_rg_in)} 个排行组按设计不导入）' if _rg_in else ''),
                        'The file holds no valid category'
                        + (f' ({len(_rg_in)} rank groups are not imported by design)'
                           if _rg_in else '')),
                parent=gui.root)
            return
        # ★R231（用户 05 批 item2）：★R204 GUIB-6 的"失败态禁导入"改成
        # "先留底再放行"——导入内容来自用户选的备份文件，不是内存默认值，
        # 拦下来只会把用户关在门外（实拍图5）。
        if not _recover_categories(gui, ('导入全部数据',
                                         'importing all data')):
            return
        _old_cats = gui.category_manager.categories
        gui.category_manager.categories = cats
        if not gui.category_manager._save_categories():
            gui.category_manager.categories = _old_cats
            messagebox.showerror(
                i18n.tr('错误', 'Error'),
                i18n.tr('分类文件写盘失败，导入未生效（已还原）',
                        'The category file could not be written, so the import '
                        'did not take effect (memory was restored)'),
                                 parent=gui.root)
            return
        # ★ R34：导入明细（完成弹窗展示）
        # ★R249 补充1/D3：原来这一格在初始化时就被钉死成 0，完成弹窗于是永远
        # 报「规则 0 条」——规则体系 ★R108 就退役了，真正随包走的是**前缀规则**
        # （prefix_rules.json 的 rules 字典），既不报数又白占一格 ⇒ 用户盯着
        # "规则 0 条"读成"规则里的设置没导进来"（本轮补充 1 的直接来源之一）。
        # 现在删掉那一格，前缀规则按包里的真实条数记账（见下面前缀规则节）。
        _imp = {'分类': len(cats), '布局': 0, '统计': 0, '点击': 0}
        _bad = []   # ★R213（NEW5-E5）：写盘未生效的部分（B11 消费口径）
        # ★R231-7（S6 定稿验收）：与 `_bad` 分列——"损坏原件未留底 ⇒ 这一节被
        # 跳过"不是写盘失败，原因和出口都不同（再点一次＋在确认框选确定），
        # 混在一起会让日志里"文件被占用/只读？"变成假指引。
        _skip = []
        # 同步悬浮条 + 列表 + sticky 网格（即使重启前点"应用"也不会失败）
        gui.dock.sync_slots(gui.category_manager.get_all_categories())
        gui._update_lists()
        gui._rebuild_sticky_grid()
        # 规则（2026-09-05 修复：原来误写 category_manager.rules/
        # _save_rules，两处都不存在，导入必失败）
        if isinstance(data.get('rules'), list) and data['rules']:
            # ★ R108 规则体系退役：旧备份文件里的规则不再导入（数据在
            # 备份文件里不丢），提示而不是崩溃——用户实测导出全部数据
            # 报 "'DesktopManager' object has no attribute 'rule_manager'"
            gui._log(f'⏹ 备份内有 {len(data["rules"])} 条规则：规则体系已退役，'
                     '已跳过导入')
        # 前缀规则
        _pr_in = _prefix_rules_to_import(data)
        if _pr_in is None:
            logger.info('[导入] 包内 prefix_rules 为空或缺失 ⇒ 本机那份**不动**'
                        '（★R313 D3：源端读失败返回的 {} 若照常写盘，目标机自建的'
                        '前缀规则整份蒸发）')
        else:
            # ★R213（NEW5-E5）：三处 atomic_write_json 返回值必须消费
            # （B11 同款口径）——写失败只进日志，"导入成功"弹窗不再
            # 与磁盘实况矛盾
            if not atomic_write_json(APP_DIR / 'config' / 'prefix_rules.json',
                                     _pr_in, indent=2):
                _bad.append('前缀规则')
            else:
                # ★R249 补充1/D3：完成弹窗报的是**包里的真实条数**
                # （prefix_rules.json 形如 {'version':2,'rules':{前缀:分类}}），
                # 写盘失败时不记账 ⇒ 不会出现"报了 3 条、盘上还是旧的"。
                _imp['前缀规则'] = len(_pr_in.get('rules') or {})
        # ★ R119：下载记录导入合并（按路径去重，封顶 500）
        _dl_touched = False
        if (isinstance(data.get('download_history'), list)
                and data['download_history']
                and hasattr(gui, 'download_tracker')):
            _have = {r.get('path') for r in gui.download_tracker.records}
            # ★R264 审计 Z4（用户 18 批点头口径：记录保留 + 打开时确认）：
            # 这里的 path 是**外部输入**，而下载页双击会把它喂给
            # os.startfile（＝资源管理器双击语义，exe/bat/lnk 会真跑）。
            # 项目口径（★R230）是这类串必须过"当下真的存在"的判据；但**不能
            # 直接拒收**——换机导入时文件本来就不在这台机器上，拒收＝下载
            # 历史整份蒸发（那才是用户会报的另一个 bug）。所以：照常收，
            # 本机没有的打 `_unverified` 标记 + 逐条留日志，执行判据挪到
            # 打开那一端（gui/download_panel._open_selected 的可执行扩展名确认）。
            _add, _unver = [], []
            for _r in data['download_history']:
                if not isinstance(_r, dict) or not _r.get('path'):
                    continue
                if _r.get('path') in _have:
                    continue
                if not os.path.isfile(str(_r['path'])):
                    _r['_unverified'] = True
                    _unver.append(str(_r['path']))
                _add.append(_r)
            if _unver:
                logger.warning(
                    '导入的下载记录有 %d 条在本机不存在该文件（%s），'
                    '已标记 _unverified，打开时会再核对'
                    % (len(_unver), '; '.join(_unver[:3])[:200]))
                gui._log(f'ℹ️ 其中 {len(_unver)} 条本机暂无此文件'
                         '（换机导入常见，记录已保留）')
            gui.download_tracker.records = (_add +
                                            gui.download_tracker.records)[:500]
            # ★R264 Z5：_save 的返回值必须消费（★R213 B11 口径）——原件读不动
            # 且留底失败时它拒写，此时"✓ 已导入"只能算内存态，弹窗要照实说
            if gui.download_tracker._save():
                _imp['下载记录'] = len(_add)
                gui._log(f'✓ 已导入 {len(_add)} 条下载记录（按路径去重合并）')
            else:
                _bad.append('下载记录')
                gui._log('⚠ 下载记录未写盘（本机那份 download_history.json 读不动'
                         '且留底失败，已拒写以防覆盖）：内存里已合并，重启后仍是旧数据')
            _dl_touched = _dl_touched or bool(_add)
        # ★R229（用户 01 批 item1）：忽略规则 + "忽略此条"黑名单同步并入，
        # 否则换机后那些被忽略过的下载会被浏览器历史/Recent 再次灌回来
        if hasattr(gui, 'download_tracker'):
            try:
                _gi = gui.download_tracker.merge_ignore(
                    data.get('download_ignore') or [])
                _gf = gui.download_tracker.merge_forgotten(
                    data.get('download_forgotten') or [])
                if _gi or _gf:
                    _dl_touched = True
                    _imp['下载忽略'] = _gi
                    _imp['已忽略条目'] = _gf
                    gui._log(f'✓ 已并入下载忽略规则 {_gi} 条、'
                             f'已忽略条目黑名单 {_gf} 条（这些下载不会再复活）')
            except Exception as _e:
                logger.warning(f'导入下载忽略规则失败: {_e}')
        # ★R229：资源页下载卡即时刷新（原来要重启才看得到，用户以为没导进来）
        if _dl_touched:
            try:
                from gui import download_panel as _dlp
                _dlp.refresh(gui)
            except Exception as _e:
                logger.warning(f'导入后刷新下载卡失败（重启后正常）: {_e}')
        # 设置
        if isinstance(data.get('app_settings'), dict):
            # ★ P1-5：导入端也必须过滤 `_` 前缀（一次性迁移标记等私有字段）。
            # 导出端已剔 _ 前缀（_export_all_data），但旧版本导出文件里仍可能带
            # _migrated_close_restore 等字段——不过滤会被 update 带回来 → 迁移
            # 标记被重置为 True → 一次性迁移逻辑不再执行 → 行为回退到旧默认值。
            incoming = {k: v for k, v in data['app_settings'].items()
                        if not k.startswith('_')}
            gui._app_settings.update(incoming)
            # ★ R71：退出恢复目标校验——导入文件可能带着本机不存在的分类/
            # 布局名（换机导入常见），或指向空分类（桌面会被清空，用户以为
            # 图标丢失）。无效或无内容一律回退"当前"。
            _crl = gui._app_settings.get('close_restore_layout', '当前')
            if (_crl and _crl != '当前'
                    and not gui._close_restore_target_has_content(_crl)):
                gui._app_settings['close_restore_layout'] = '当前'
                logger.warning(f'导入的退出恢复目标 "{_crl}" 在本机不存在或为'
                               '空分类，已回退为"当前"')
            # ★R287-N26：这一节是导入链上最后一个"写没写上都没人知道"的口子
            # （同族的 前缀规则/下载记录/高亮/悬浮条 早在 ★R284 就消费返回值了）。
            # 闸门立着时：内存照样合并、界面照样刷新，用户随手点一下"✅ 应用"
            # 就把半截状态写回盘 ⇒ 导入白做，而完成弹窗此前只说"成功"。
            if not gui._save_app_settings():
                _bad.append('设置项')
            # ★ P2-N1：刷 UI（之前只更新内存不刷新，用户以为"导入失败"）
            # _apply_app_settings 会读 _app_settings 重置所有控件值
            gui._apply_app_settings()
            gui._update_lists()
            # ★ 顺带重建 sticky 网格：导入的 system_icons_resident 与分类
            # display_mode 都要反映到"公共分类显示"三列网格的勾选态。
            # 不重建的话，用户导入后不重启直接点"✅ 应用"会把旧的
            # _system_resident_var 写回 app_settings，覆盖刚导入的值。
            gui._rebuild_sticky_grid()
            # ★R240（#137/#138，用户 32 批 item2）：把导入的设置**推给运行中的对象**。
            # 这些键本来就随包走（app_settings 整包导出、上面也已落盘），缺的是
            # "落盘之后没人应用"：统计开关只在启动读一次、榜单字号/主题/字号/密度/
            # 图标/悬浮窗置顶也各有应用路径 ⇒ 用户观感"导出前统计是开的，导回来
            # 变成关的、字号主题也没跟过来"。收在 gui/settings_apply.py 一个入口，
            # 每项独立 try（一项坏不影响导入主流程），并明确区分"即时/重启后生效"。
            try:
                from gui import settings_apply as _sa
                _now, _later = _sa.apply_runtime(gui)
                # ★R249 补充1/D2：桌面亮度那三键走单独一路，判据是**包里带了没有**
                # ——`apply_runtime` 拿到的是合并后的 `_app_settings`，分不出
                # "包带来的"和"本机原有的"，所以这里把 `incoming` 原样交给它。
                # 顺手回填规则页那份构建期拷贝 `_dim_levels`（gui/rule_panel.py:833）：
                # 不回填的话用户下次拖滑条会拿旧拷贝写回 dim_scope_levels，
                # 把刚导入的分屏记忆整份盖掉（这才是"丢数据"的那一半）。
                if _sa.apply_dimmer(gui, incoming):
                    _now = _now + ['桌面亮度']
                if _now:
                    gui._log('⚡ 导入设置已即时生效：' + '、'.join(_now))
                if _later:
                    gui._log('↻ 以下设置需重启后整窗生效：' + '、'.join(_later)
                             + '（Tk 文案/排版在窗口构建时求值，做不到热切换）')
            except Exception as _e:
                logger.warning(f'导入后应用设置失败（重启后正常）: {_e}')
        # 快捷键
        # ★ R154：整表替换（防 update 残留旧键）+ 立即重新注册——此前只
        # 写盘，导入的热键要重启才生效（用户 -09/3 指出导入应含热键数据）
        if isinstance(data.get('hotkeys'), dict):
            gui._hotkey_cfg = {k: list(v) for k, v in
                               data['hotkeys'].items() if isinstance(v, list)}
            gui._save_hotkey_config()
            try:
                from gui import hotkey_panel as _hk
                _hk.apply_hotkeys(gui)
                _hk.update_list(gui)
            except Exception as _e:
                logger.warning(f'热键导入后重新注册失败（重启后生效）: {_e}')
        # 高亮（★R249 补充1：用户说的"规则里面的设置方框"就是这一节的勾选项）
        if isinstance(data.get('highlight'), dict):
            if not atomic_write_json(APP_DIR / 'config' / 'highlight.json',
                                     data['highlight'], indent=4):
                _bad.append('高亮设置')
            else:
                # ★R249 补充1/D1：本节此前"只写文件、没人应用"——屏幕上勾选框
                # 还是旧状态（用户读成"没导进来"），更要命的是运行中的
                # highlighter 也还是旧值，用户随手点一下任一框，
                # `_save_highlight_settings()` 就把旧内存写回盘、把刚导入的抹掉。
                # 只有**写盘成功**才应用：写失败时磁盘与内存都是旧的，一致；
                # 只推内存会造出"界面显示新值、磁盘还是旧值"的假生效。
                try:
                    from gui import settings_apply as _sa
                    if _sa.apply_highlight(gui):
                        gui._log('✓ 已导入高亮/框选设置：规则页的勾选框与桌面上的'
                                 '框已即时跟着变（无需重启）')
                except Exception as _e:
                    logger.warning(f'导入后应用高亮设置失败（重启后正常）: {_e}')
        # 悬浮条槽位
        # ★ P2-N15 配合导出端默认 None：空值不覆盖目标端已有的 docks.json
        # ★R262 批14（用户 14 批 item4）：docks.json 真身是**列表**
        # （`DesktopDock._save_slots` 写 `[s.to_dict() ...]`），而这里旧判据只认
        # `isinstance(..., dict)` ⇒ 悬浮条槽位**从来没被导入成功过**，还不计进
        # _bad/_skip，用户看到的是"导入成功"而条上还是自己原来的槽位。
        # 改成 list/dict 都收，并在写盘成功后就地重载 + 重绘（_repaint_if_visible
        # 是纯 GDI 重绘，与 set_kids_slots 同一出口，不碰 _wndproc）。
        _dk = data.get('docks')
        if isinstance(_dk, (list, dict)) and _dk:
            if not atomic_write_json(APP_DIR / 'config' / 'docks.json',
                                     _dk, indent=4):
                _bad.append('悬浮条槽位')
            else:
                _imp['悬浮条槽位'] = len(_dk)
                try:
                    _d = getattr(gui, 'dock', None)
                    if _d is not None:
                        _d.slots = _d._load_slots()
                        _d._repaint_if_visible()
                        gui._log(f'📥 已导入悬浮条槽位 {len(_dk)} 个（条上已即时刷新）')
                    else:
                        gui._log(f'📥 已导入悬浮条槽位 {len(_dk)} 个')
                except Exception as _e:
                    logger.warning(f'导入后刷新悬浮条槽位失败（重启后正常）: {_e}')
        # 文件夹组
        if isinstance(data.get('explorer_tabs'), dict):
            # ★R231-6（S6 独立验收命中）：本节此前把外部 dict 的 `_load_failed`
            # pop 掉就直接写 —— CORE-5 那道闸只看传入 dict，等于被 pop 穿；
            # 本地 `explorer_tabs.json` 若正处"读不动"状态，损坏原件会被
            # 静默覆盖且**零留底**。与分类侧同口径：先确认 + 另存留底再放行，
            # 用户取消/留底失败 ⇒ 该节计入 _bad 如实上报（不谎报半成功）。
            from core import explorer_tabs as _xt_mod
            _local_tabs = _xt_mod.load_state(APP_DIR / 'config')
            if not _recover_folder_groups(gui, _local_tabs,
                                          ('导入全部数据·文件夹组',
                                           'importing folder groups')):
                _skip.append('文件夹组')
                logger.error('导入全部数据：文件夹组损坏原件未留底（用户取消或'
                             '留底失败），该节已跳过')
            else:
                # ★R207 NEW3-B4：导入侧剥离外部带来的内部标记再落盘
                _xt_in = dict(data['explorer_tabs'])
                _xt_in.pop('_load_failed', None)
                if _save_tabs_checked(_xt_in, gui):
                    # ★R229（用户 01 批 item1）：弹窗里报出组数——文件夹组本来
                    # 就在"导出/导入全部数据"内，但完成弹窗从不提它，用户导完
                    # 看不到字样 ⇒ 以为"资源页数据没覆盖"（本批 item1 的来源）
                    _imp['文件夹组'] = len(
                        [g for g in (_xt_in.get('groups') or [])
                         if isinstance(g, dict) and g.get('name')])
                    # ★R229（用户 01 批 item1）：文件夹组落盘后刷新资源页组列表
                    # （单组导入走的是这条，"导出全部/导入全部"此前漏刷，
                    # 用户导完看到资源页还是旧组 ⇒ 以为没导进来）
                    try:
                        gui._refresh_explorer_list()
                    except Exception as _e:
                        logger.warning(f'导入后刷新文件夹组列表失败（重启后正常）: {_e}')
        # ★ S 段/B18：布局存档（additive——经 add_layout 校验+唯一文件名，
        # 不覆盖已有同名存档）；使用统计；分类点击。
        # icon_manifest（归属清单文本）为派生数据，导入不还原——categories
        # 已恢复，随时可重新"📤 导出"生成
        if isinstance(data.get('layouts'), list):
            _lok, _skp, _lbad = gui._import_layouts_batch(
                [x for x in data['layouts'][:200] if isinstance(x, dict)])
            gui._log(f'📥 已导入布局存档 {_lok} 份' +
                      (f'（同名跳过 {_skp}）' if _skp else '') +
                      (f'（失败 {_lbad}）' if _lbad else ''))
            _imp['布局'] = _lok
            gui._update_lists()
        else:
            # ★ R19：旧版导出文件没有 layouts/usage_stats/clicks 三项——
            # 明确提示，避免用户以为"导入失败/漏了数据"
            gui._log('ℹ️ 该文件不含 布局存档/使用统计/分类点击'
                      '（旧版导出的文件没有这三项，用新版重新导出即可）')
        if isinstance(data.get('usage_stats'), dict):
            _n = gui.usage_stats.import_dict(data['usage_stats'])
            _imp['统计'] = _n
            gui._log(f'📥 已导入使用统计 {_n} 条')
        if isinstance(data.get('clicks'), dict):
            _n = gui.click_tracker.replace_clicks(data['clicks'])
            _imp['点击'] = _n
            gui._log(f'📥 已导入分类点击 {_n} 条')
        # ★ R185：条件监视导入——**全部置为停用**（监视动作含关机/重启
        # 这类高危项，换机导入直接启用有风险；同名跳过防重复）
        if isinstance(data.get('monitors'), list) and data['monitors']:
            _mons = []
            for _mo in data['monitors'][:100]:
                if not (isinstance(_mo, dict) and _mo.get('process')):
                    continue
                _mons.append({
                    'name': str(_mo.get('name') or _mo['process'])[:60],
                    'process': str(_mo['process'])[:120],
                    'condition': _mo.get('condition')
                    if _mo.get('condition') in ('gone', 'present') else 'gone',
                    'action': _mo.get('action')
                    if _mo.get('action') in ('shutdown', 'reboot', 'command')
                    else 'command',
                    'command': str(_mo.get('command') or ''),
                    'enabled': False})
            _mon = getattr(gui, 'monitor', None)
            if _mons and _mon is not None:
                _have = {m.get('name') for m in _mon.monitors}
                _new = [m for m in _mons if m['name'] not in _have]
                _mon.monitors.extend(_new)
                _mon.save()
                _imp['条件监视(停用)'] = len(_new)
                gui._log(f'📥 已导入条件监视 {len(_new)} 条'
                         '（全部为停用状态，请逐条确认后手动启用）')
        # ★R322 J5（用户 77 批点名「搬入搬出应该是有记录的」）：盒子两本账一并导入。
        # 两道闸：
        # ① 闸门**独立成立**（★R231 口径，不看上游传进来的标记）——本地那份只要
        #    "存在且读不动"，就先另存留底再写；留底留不出去 ⇒ 这一节跳过并计入 _skip，
        #    绝不静默覆盖（★R231-6 那起就是被 pop 掉标记的干净 dict 穿过去的）。
        # ② 导入来的 `to` 是**外部输入**（★R230）：它是散回时的源文件路径，别的机器上
        #    那份根本不在这个位置 ⇒ 一律按本机盒根重算，不照抄备份里的绝对路径。
        # ★R322 K4 再加第三道：**落点由 `_box_ledger_targets()` 一次定夺**——盒根与
        #    程序目录只重定向了一头（老式门禁只换本模块 `APP_DIR`）⇒ 这一节整节拒写、
        #    计进 `_skip` 显形，绝不往那一头落一枚。
        from core import desktop_box as _ib_db
        _ib_paths = dict((k, p) for k, _nm, p in _box_tgt)
        for _k, _nm in (('hidden_box_ledger', '盒登记账本'),
                        ('desktop_box_manifest', '盒成员账本')):
            _v = data.get(_k)
            if not isinstance(_v, dict) or not isinstance(_v.get('boxes'), dict):
                continue
            if _k not in _ib_paths:
                _skip.append(_nm)
                logger.error('导入全部数据：%s 本次拒写（盒根与程序目录只重定向了一头，'
                             '口径同 ★R322 J5b 第三道闸）⇒ 未覆盖、原样保留', _nm)
                continue
            _p = _ib_paths[_k]
            if os.path.isfile(_p):
                try:
                    _loc = json.loads(Path(_p).read_text(encoding='utf-8'))
                    if not isinstance(_loc, dict):
                        raise ValueError('not a dict')
                except Exception:
                    from utils import safeio as _sio
                    _ok_as, _aside = _sio.set_aside_corrupt(_p)
                    if not _ok_as:
                        _skip.append(_nm)
                        logger.error('导入全部数据：%s 本地那份读不动且留底失败（%s）'
                                     '⇒ 该节跳过，未覆盖', _nm, _aside)
                        continue
                    gui._log(f'⚠ 本机那份{_nm}读不动，已另存留底（{_aside}），'
                             '再写入导入包里的这份')
            if _k == 'desktop_box_manifest':
                # ★R325 F2（审查报告 20261013_2111 P2）：旧写法在这一头**只重算 to**
                # （盒里那一枚现在躺哪一格），`from`（当年从桌面哪儿搬走的）留着
                # 旧机器的绝对路径 ⇒ 散回时 `dst = rec['from']`，而只有那个目录**不存在**
                # 才退回当前桌面（★R310）；同名用户/OneDrive 重定向在新机常常"恰好还在"，
                # 图标就落进一个看不见的文件夹——用户那句"图标丢失"是这么复现的。
                # 现在两条一起按本机重算（`public` 旗标决定回公共桌面还是用户桌面）。
                _v, _fixed = _ib_db.rebase_imported_manifest(_v)
                if _fixed:
                    logger.info('[导入全部] 盒成员账本里 %d 枚的路径已按本机重算'
                                '（盒内位置按本机盒根、来路按本机桌面；那份备份来自'
                                '别的目录/别的机器）', _fixed)
            try:
                os.makedirs(os.path.dirname(_p), exist_ok=True)
            except OSError as _e:
                logger.warning('导入全部数据：%s 的目录建不出来 %s: %s', _nm, _p, _e)
            if not atomic_write_json(Path(_p), _v, indent=2):
                _bad.append(_nm)
                logger.error('导入全部数据：%s 写盘未生效 %s', _nm, _p)
                continue
            _nb = len(_v['boxes'])
            _imp[_nm] = _nb
            gui._log(f'📥 已导入{_nm}（{_nb} 只盒）')
        # 计划任务（用户）= 系统级条目，暂不支持随数据导入（明确告知）
        gui._log('ℹ️ 计划任务不随数据导入（属系统级配置，需在本机重建）')
        # ★R262 批14（用户 14 批 item4）：「自动打开设置」卡的控件只在建卡时
        # 读一次初值，而任何一次勾选都会用界面上那份**整份覆盖**盘上的
        # startup_open ⇒ 导入进来的设置必须同步进界面，否则用户随手点一下
        # 卡上的开关就把刚导入的配置静默回退（"导入了又没了"）。
        try:
            gui._resync_startup_open_vars()
        except Exception as _e:
            logger.warning(f'导入后同步自动打开设置卡失败（重启后正常）: {_e}')
        if getattr(gui, 'stats_panel', None):
            gui.stats_panel.refresh()
        # ★ R34：完成弹窗列明各项数量——0 条一目了然（例如清空之后才
        # 导出的文件，统计本来就是空的），避免"导出时有统计、导入没有"的困惑
        # ★R207 NEW3-B7：改"多数已即时生效"口径（分类/文件夹组/悬浮条
        # 导入路径本就现场刷新，承诺重启反而误导）
        gui._log('✓ 已导入全部数据（多数项已即时生效，个别未刷新可重启程序）')
        # ★R213（NEW5-E5）：写盘失败的子项必须显形，否则"导入成功"
        # 弹窗与磁盘实况矛盾（B11 消费口径的收口半边）
        if _bad:
            logger.error(f'导入完成但以下部分写盘失败: {_bad}')
            gui._log(f'⚠ 导入未完全生效：{"、".join(_bad)} 写盘失败'
                     '（文件被占用/只读？），详见日志')
        # ★R231-7：跳过的节（损坏原件未留底）单独报，出口写进日志里
        if _skip:
            logger.error(f'导入完成但以下部分被跳过（未自救）: {_skip}')
            gui._log(f'⚠ 导入未完全生效：{"、".join(_skip)} 未写入'
                     '（损坏原件未留底：再点一次同一按钮，在"先留底再处理"'
                     '的确认框选确定即可）')
        # ★R231-7（S6 定稿验收）：完成弹窗的"0 个"只解释为"文件里本就没有"，
        # 失败/跳过的节如果不在弹窗里显形，`文件夹组 0 个` 会被读成"没数据"
        # ⇒ 与磁盘实况矛盾（★R229 用户 01 批 item1 的同类病根）。
        # ★R233（用户 08 批 item1）：English 档整句出英文，节名查 _SECTION_EN。
        _nt = _bad + _skip
        _nt_zh = '、'.join(_nt)
        _nt_en = ', '.join(_sec_en(x) for x in _nt)

        def _n(k):
            return _imp.get(k, 0)

        messagebox.showinfo(
            i18n.tr('导入完成（部分未生效）', 'Import Finished (Not Fully Applied)')
            if _nt else i18n.tr('导入成功', 'Import Successful'),
            ((i18n.tr(f'⚠ 未生效：{_nt_zh}\n'
                      '（前者=文件被占用/只读写不进去；后者=损坏原件未留底、'
                      '这一节已跳过。详见日志）\n\n',
                      f'⚠ Not applied: {_nt_en}\n'
                      '(the file was locked or read-only, or a corrupt original '
                      'had no backup left and that section was skipped - '
                      'see the log)\n\n') if _nt else '') +
             i18n.tr(
                 f'已导入：分类 {_n("分类")} 个 · 前缀规则 {_n("前缀规则")} 条 · '
                 f'布局存档 {_n("布局")} 份\n'
                 f'使用统计 {_n("统计")} 条 · 分类点击 {_n("点击")} 条 · '
                 f'下载记录 {_n("下载记录")} 条 · '
                 f'条件监视 {_n("条件监视(停用)")} 条（停用）\n'
                 # ★R229（用户 01 批 item1）：资源页三类数据在弹窗里显形
                 f'文件夹组 {_n("文件夹组")} 个（含自动打开设置） · '
                 f'悬浮条槽位 {_n("悬浮条槽位")} 个 · '
                 f'下载忽略规则 {_n("下载忽略")} 条 · '
                 f'已忽略条目 {_n("已忽略条目")} 条\n'
                 # ★R322 K4（★R213/★R229 同一口径：包里有这一节就得在弹窗里显形）
                 f'盒子账本两本：登记 {_n("盒登记账本")} 只 · '
                 f'成员 {_n("盒成员账本")} 只\n\n'
                 '（某项为 0 = 该文件里本就没有这部分数据，\n'
                 '例如清空个人数据之后才导出的文件，统计已被清零）\n'
                 '条件监视导入后全部为停用，确认无误后请手动启用。\n'
                 # ★R240（#138）：与确认框同一口径，完成框也说清"没带过来的两样"
                 '不含：锁屏密码（只在本机本次运行内有效）、'
                 '计划任务（需在本机重建）。\n'
                 '多数项已即时生效；如个别面板未刷新，重启程序即可。',
                 f'Imported: categories {_n("分类")}, prefix rules '
                 f'{_n("前缀规则")}, '
                 f'layout archives {_n("布局")}\n'
                 f'usage stats {_n("统计")}, category clicks {_n("点击")}, '
                 f'download records {_n("下载记录")}, '
                 f'monitors {_n("条件监视(停用)")} (disabled)\n'
                 f'folder groups {_n("文件夹组")} (with auto-open settings), '
                 f'dock slots {_n("悬浮条槽位")}, '
                 f'ignore rules {_n("下载忽略")}, '
                 f'ignored entries {_n("已忽略条目")}\n'
                 f'desk-box ledgers: {_n("盒登记账本")} registered boxes, '
                 f'{_n("盒成员账本")} membership boxes\n\n'
                 '(A 0 just means that file never held this kind of data - '
                 'for example a backup exported after "clear personal data", '
                 'whose stats were already reset.)\n'
                 'Imported monitors all start disabled; enable them by hand '
                 'once you have checked them.\n'
                 'Not included: the lock-screen password (only valid for this '
                 'run on this machine) and scheduled tasks (recreate them on '
                 'this machine).\n'
                 'Most of it is live right away - restart the app if a panel '
                 'still looks old.')),
            parent=gui.root)
    except Exception as e:
        logger.error(f'导入全部数据失败: {e}')
        # ★R233（★R230 挂账 2）：本入口是一节一节往磁盘写的，中途抛异常时
        # 前面几节已经落地、后面几节还是旧的 ⇒ 用户拿到"半新半旧"的配置，
        # 旧写法只弹一句「导入失败: <异常>」，什么退路都没给。现在按改动前
        # 快照整批还原 + 把内存拉回来 + 弹窗如实说明范围。
        _rb = None
        if _snap:
            try:
                _rb = _restore_personal_config(_snap, APP_DIR, _snap_seen,
                                               extra_paths=_box_paths)
                _resync_memory_after_rollback(gui)
            except Exception as _e2:
                logger.error(f'[导入回滚] 回滚过程自身失败: {_e2}')
                _rb = None
        if _rb:
            _ok, _bl = _rb
            _nbad = len(_bl)
            logger.warning(f'[导入回滚] 导入中断，已还原 {_ok} 个数据文件'
                           + (f'，{_nbad} 个失败' if _nbad else ''))
            try:
                gui._log(f'↩ 导入中断，已回滚 {_ok} 个数据文件'
                         '（内存已同步，建议重启程序）')
            except Exception:
                pass
            _rb_zh = (f'已把数据回滚到本次导入之前的状态'
                      f'（还原 {_ok} 个文件'
                      + (f'，{_nbad} 个未能还原' if _nbad else '') + '）。\n')
            _rb_en = (f'The data was rolled back to its pre-import state '
                      f'({_ok} files restored'
                      + (f', {_nbad} could not be restored' if _nbad else '')
                      + ').\n')
            _title_zh, _title_en = '导入已回滚', 'Import Rolled Back'
        else:
            _rb_zh = ('改动前的数据快照未能取得（或写入尚未开始），'
                      '因此没有自动回滚。\n')
            _rb_en = ('No pre-import snapshot was available (or writing had '
                      'not started yet), so nothing was rolled back.\n')
            _title_zh, _title_en = '错误', 'Error'
        messagebox.showerror(
            i18n.tr(_title_zh, _title_en),
            i18n.tr(
                f'导入失败：{e}\n\n{_rb_zh}'
                '布局存档、桌面图标备份属"只增不覆盖"，不在回滚范围内。\n'
                '为让界面与磁盘重新一致，请重启程序。',
                f'Import failed: {e}\n\n{_rb_en}'
                'Layout archives and desktop-icon backups are additive and '
                'were never part of the rollback.\n'
                'Please restart the program so the UI matches the disk.'),
            parent=gui.root)


def on_export_categories(gui):
    """导出分类定义（含图标名单）"""
    from tkinter import filedialog
    if _refuse_export(gui, '分类', 'category'):
        return
    path = _save_dialog(gui, defaultextension='.json',
        initialfile=i18n.tr(f'分类数据_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json',
                            f'Category_Data_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'),
        filetypes=[('JSON', '*.json')])
    if not path:
        return
    try:
        if not atomic_write_json(path, gui.category_manager.categories,
                                 indent=2):
            raise OSError('写盘失败（详见日志）')
        gui._log(f'✓ 已导出分类数据 → {path}')
    except Exception as e:
        logger.error(f'导出分类失败: {e}')
        messagebox.showerror(i18n.tr('错误', 'Error'),
                             f'{i18n.tr("导出失败", "Export failed")}: {e}',
                             parent=gui.root)


def on_import_categories(gui):
    """导入分类定义（覆盖当前）"""
    from tkinter import filedialog
    path = _open_dialog(gui, filetypes=[('JSON', '*.json')])
    if not path:
        return
    try:
        # ★ R42：统一格式识别（sniff）替代 R34 手写特征检查
        from io_formats import sniff, mismatch_message, KIND_CATS
        kind = sniff(path)
        if kind and kind != KIND_CATS:
            messagebox.showerror(i18n.tr('文件不匹配', 'File Mismatch'),
                                 mismatch_message(KIND_CATS, kind),
                                 parent=gui.root)
            return
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        cats = data if isinstance(data, list) else None
        if not isinstance(cats, list):
            messagebox.showerror(
                i18n.tr('错误', 'Error'),
                i18n.tr('文件格式不对：顶层应是分类列表（【📤 导出分类】生成的 JSON）',
                        'Wrong file format: the top level must be a list of '
                        'categories (the JSON from "Export Categories")'),
                parent=gui.root)
            return
        if not messagebox.askyesno(
                i18n.tr('确认导入', 'Confirm Import'),
                i18n.tr(f'将覆盖当前 {len(gui.category_manager.categories)} 个分类，确定？',
                        f'This overwrites your {len(gui.category_manager.categories)} '
                        'current categories. Continue?'),
                parent=gui.root):
            return
        # ★R231（用户 05 批 item2）：★R204 GUIB-6 的"失败态禁导入"改成自救闸
        # ——先给损坏原件另存留底再写入导入内容（保存失败仍回滚内存态）
        if not _recover_categories(gui, ('导入分类',
                                         'importing categories')):
            return
        _old_cats = gui.category_manager.categories
        for c in cats:
            gui.category_manager._normalize(c)
        gui.category_manager.categories = cats
        if not gui.category_manager._save_categories():
            gui.category_manager.categories = _old_cats
            messagebox.showerror(
                i18n.tr('错误', 'Error'),
                i18n.tr('分类文件写盘失败，导入未生效（已还原）',
                        'The category file could not be written, so the import '
                        'did not take effect (memory was restored)'),
                                 parent=gui.root)
            return
        gui.dock.sync_slots(gui.category_manager.get_all_categories())
        gui._update_lists()
        gui._rebuild_sticky_grid()
        gui._log(f'✓ 已导入 {len(cats)} 个分类')
    except Exception as e:
        logger.error(f'导入分类失败: {e}')
        messagebox.showerror(i18n.tr('错误', 'Error'),
                             f'{i18n.tr("导入失败", "Import failed")}: {e}',
                             parent=gui.root)


def on_export_desktop_icons(gui):
    """★ R41：桌面图标文件备份（打包 zip）——配对 _on_restore_desktop_icons"""
    from tkinter import filedialog
    from desktop_icon_backup import (export_desktop_icons,
                                     estimate_desktop_size)
    # ★ R42：大桌面预检——超 500MB 先确认（有用户桌面文件超 1G 的先例）
    total = estimate_desktop_size()
    if total > 500 * 1024 * 1024:
        gb = total / (1024 * 1024 * 1024)
        tip = i18n.tr(
            f'桌面文件共 {gb:.2f} GB，超过 500MB。\n备份耗时较长、zip 很大，'
            f'恢复也慢。\n\n建议先清理桌面上的大文件（视频/安装包）。\n仍要继续备份吗？',
            f'The desktop holds {gb:.2f} GB, over the 500MB mark.\n'
            f'Backup takes a while, the zip gets big, and restoring is slow '
            f'too.\n\nConsider removing large desktop files (videos, '
            f'installers) first.\nBack up anyway?')
        if not messagebox.askyesno(i18n.tr('桌面文件较大', 'Large Desktop'),
                                   tip, parent=gui.root):
            gui._log('📦 已取消桌面图标备份（文件过大）')
            return
    elif total > 100 * 1024 * 1024:
        gui._log(f'📦 桌面文件约 {total/1024/1024:.0f} MB，开始备份…')
    path = _save_dialog(gui, defaultextension='.zip',
        initialfile=i18n.tr(f'桌面图标备份_{datetime.now().strftime("%Y%m%d_%H%M%S")}.zip',
                            f'Desktop_Icons_Backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}.zip'),
        filetypes=[('ZIP 备份', '*.zip')])
    if not path:
        return
    r = export_desktop_icons(path, log=gui._log)
    if r.get('success'):
        msg = i18n.tr(f"已备份 {r['count']} 个桌面文件。",
                      f"{r['count']} desktop file(s) backed up.")
        size_mb = os.path.getsize(path) / 1024 / 1024
        if size_mb > 500:
            msg += i18n.tr(
                f'\n⚠ 备份文件 {size_mb:.0f} MB——桌面图标太大了，建议清理桌面大文件。',
                f'\n⚠ The backup is {size_mb:.0f} MB - the desktop is very '
                'full, consider removing large files.')
        if r.get('folders'):
            # ★ R196：文件夹连同内容一并打包（用户 25 批"要不怎么叫备份呢"）
            msg += i18n.tr(
                f"\n（含 {len(r['folders'])} 个桌面文件夹（内容一并打包）："
                + '、'.join(r['folders'][:6])
                + ('…' if len(r['folders']) > 6 else '') + '）',
                f"\n(Including {len(r['folders'])} desktop folder(s), packed "
                f'with their contents: '
                + ', '.join(r['folders'][:6])
                + ('...' if len(r['folders']) > 6 else '') + ')')
        messagebox.showinfo(i18n.tr('桌面图标备份完成', 'Desktop Backup Done'),
                            msg, parent=gui.root)
    else:
        messagebox.showerror(
            i18n.tr('错误', 'Error'),
            f"{i18n.tr('备份失败', 'Backup failed')}: {r.get('error')}",
            parent=gui.root)


def on_restore_desktop_icons(gui):
    """★ R41：从备份 zip 恢复桌面图标文件（解回用户桌面后 F5 刷新）"""
    from tkinter import filedialog
    from desktop_icon_backup import (restore_desktop_icons,
                                     get_desktop_dirs)
    path = _open_dialog(gui, title='选择桌面图标备份 (zip)',
        filetypes=[('ZIP 备份', '*.zip'), ('所有文件', '*.*')])
    if not path:
        return
    # ★ R42：统一格式识别（拿错 zip 给指路提示）
    from io_formats import sniff, mismatch_message, KIND_ICONZIP
    kind = sniff(path)
    if kind and kind != KIND_ICONZIP:
        messagebox.showerror(i18n.tr('文件不匹配', 'File Mismatch'),
                             mismatch_message(KIND_ICONZIP, kind),
                             parent=gui.root)
        return
    dirs = get_desktop_dirs()
    if not dirs:
        messagebox.showerror(
            i18n.tr('错误', 'Error'),
            i18n.tr('未找到桌面目录', 'The desktop folder was not found'),
            parent=gui.root)
        return
    overwrite = messagebox.askyesno(
        i18n.tr('恢复桌面图标', 'Restore Desktop Icons'),
        i18n.tr('若桌面已存在同名文件，覆盖吗？\n\n'
                '是 = 覆盖（整桌还原场景用）\n'
                '否 = 跳过同名，只补回缺失的',
                'Overwrite files with the same name on the desktop?\n\n'
                'Yes = overwrite (full-desktop restore)\n'
                'No = skip same names, restore missing ones only'),
        parent=gui.root)
    # ★ R79g：文件级恢复会让 Explorer 重新排位，布局守护会把这种跳变
    # 误判为"回退"开火拉扯（2026-09-16 日志实锤：🛡 15/20 跳变自动恢复
    # 与用户点 All/F5 互相拉锯）。恢复期间挂起守护，落定后按当前视图
    # 重套布局并重新布防。
    prev_tidy = bool(getattr(gui.icon_manager, 'desktop_tidy_mode', False))
    gui.icon_manager.desktop_tidy_mode = True

    def _rebase_after_restore():
        try:
            active = getattr(gui.dock, '_active_slot_name', '') or ''
            lay = gui.icon_manager.pick_layout(active) if active else None
            if lay:
                gui._apply_layout_by_name(active)
                try:
                    gui.icon_manager.arm_guard(str(lay['path']), active)
                except Exception:
                    pass
                gui._log(i18n.tr('✓ 已按当前视图布局重新落定，布局守护重新布防',
                                 '✓ Layout re-applied for the current view; '
                                 'layout guard re-armed'))
        except Exception as e:
            logger.error(f"恢复后重新布防失败: {e}")
        finally:
            gui.icon_manager.desktop_tidy_mode = prev_tidy

    try:
        r = restore_desktop_icons(path, dirs[0], overwrite=overwrite,
                                  log=gui._log)
        if not r.get('success'):
            messagebox.showerror(i18n.tr('错误', 'Error'),
                                 f"{i18n.tr('恢复失败', 'Restore failed')}: "
                                 f"{r.get('error')}",
                                 parent=gui.root)
            return
        try:
            gui.dock._force_listview_refresh()
        except Exception:
            pass
    finally:
        # ★ R81（参考 desktop-icon-mcp restore 的参数语义：disable_redraw/
        # disable_auto_arrange + 多遍稳定化 passes/settle + 末遍验证）：
        # Explorer 对新文件的条目身份未固化时每次刷新都重排，且会从旧样式
        # 位重开自动排列——先强制关自动排列，600ms 间隔连套 4 遍压住，
        # 3.2s 末遍套完重新布防（tidy 在该遍 finally 恢复）。
        # ★ R82（持久化落盘扳机）概念已由下方 ★R234 真机制接管：清
        # SysListView32 的 LVS_AUTOARRANGE 样式位并读回确认。这里原来挂着一枚
        # 发阻塞 `SendMessageW(lv, 0x1036 …)` 的嵌套函数 `_snap_grid`，全仓零调用
        # （grep 只命中定义行），且用的正是下方注释点名"Explorer 忙时把这里拖死"
        # 而删掉的那类阻塞式跨进程 SendMessage ⇒ ★R283 顺手清掉这枚死脚枪，
        # 免得日后有人误接回去。（判据：test_r283_notify_updatedir 附带静态闸）
        try:
            # ★R234（第 14~16 批根因修复）：这里原来发 `SendMessageW(lv, 0x1039, 0, 0)`
            # 当"关自动排列"用——SDK 里根本没有 LVM_SETAUTOARRANGE，0x1039 是
            # LVM_SUBITEMHITTEST，所以这条**从来没生效过**；而自动排列开着时
            # LVM_SETITEMPOSITION 会被 Explorer 直接拒（实测 ret=0，一格不动），
            # 表现就是"恢复/套位看起来成功、一刷新又乱"。改走真机制：清
            # SysListView32 的 LVS_AUTOARRANGE 样式位并读回确认。
            # 顺带去掉阻塞式 SendMessageW——Explorer 忙时会把这里拖死。
            _aa = gui.icon_manager.ensure_auto_arrange_off()
            if _aa not in ('off', 'cleared'):
                gui._log(f"⚠ 桌面“自动排列图标”关不掉（{_aa}），"
                         f"位置写入可能被系统覆盖")
        except Exception:
            pass

        def _apply_only():
            try:
                active = getattr(gui.dock, '_active_slot_name', '') or ''
                lay = gui.icon_manager.pick_layout(active) if active else None
                if lay:
                    gui._apply_layout_by_name(active)
            except Exception:
                pass

        try:
            gui.root.after(800, _apply_only)
            # ★ R92：精简——只保留 800ms 套位 + 2.9s 校准两遍可见动作；
            # 网格开关（实测无效）与中间重复套位移除，Explorer 重启才是
            # 最终固化手段
            gui.root.after(2900, _rebase_after_restore)         # 布防+tidy 恢复
        except Exception:
            pass
    # ★ R90：自动"固化"序列——Explorer 优雅退出时会把当前图标布局写入
    # 持久化流（唯一可靠的存盘时机），重启后从新流加载，F5 永久稳定。
    # ★R204 NEW-B8：时序注释更正——R191（GUIB-3）已删除"接力重启"实现，
    # 本程序固化后原地继续运行；只有 explorer 重启序列（4.5s 关→自愈/手动起）。
    def _solidify():
        import subprocess as _sp
        import threading as _th
        import sys as _s
        import time as _t

        def _seq():
            try:
                gui._log('🔄 正在重启资源管理器以固化布局（打开的文件夹窗口会关闭）…')
                _t.sleep(1.0)
                _sp.Popen(['taskkill', '/IM', 'explorer.exe'],
                          creationflags=0x08000000)   # CREATE_NO_WINDOW，无 /F=优雅关闭→触发存盘
                # ★ R92：等 explorer 真正退出；若系统已自动重启（进程还在），
                # 跳过手动启动，避免弹出"快速访问"文件夹窗口（用户实测）
                _gone = False
                for _i in range(20):
                    _t.sleep(0.5)
                    _chk = _sp.run(['tasklist', '/FI', 'IMAGENAME eq explorer.exe'],
                                   capture_output=True,
                                   creationflags=0x08000000).stdout or b''
                    if b'explorer.exe' not in _chk:
                        _gone = True
                        break
                if _gone:
                    _sp.Popen(['C:\\Windows\\explorer.exe'])
                else:
                    gui._log('检测到系统已自动重启资源管理器，跳过手动启动')
                gui._log('✓ 资源管理器已重启，布局已固化')
                # ★ R191（Qoder GUIB-3）：删除 R93 残留的"接力重启"实现——
                # R93 定案本来就是"不自动退出接力"（新实例会被守护启动器
                # 作业对象连带击杀=程序闪退），但 Popen 残留一直在，触发
                # 单实例唤醒+假超时弹窗。本程序原地继续运行。
            except Exception as e:
                logger.error(f"固化序列失败: {e}")

        _th.Thread(target=_seq, daemon=True).start()
        # ★ R93：不再自动退出接力——新实例会被守护启动器的作业对象连带
        # 击杀（用户视角=程序闪退）。恢复+固化后本程序原地继续运行；
        # 若 Explorer 重启后悬浮条/高亮异常，手动重启一次程序即可。

    try:
        gui.root.after(4500, _solidify)
    except Exception:
        pass

    info = r.get('info') or {}
    msg = i18n.tr(f"已恢复 {r['restored']} 个文件到桌面。\n"
                  f"（跳过 {r['skipped']}，失败 {r['failed']}）",
                  f"Restored {r['restored']} file(s) to the desktop.\n"
                  f"(skipped {r['skipped']}, failed {r['failed']})")
    msg += i18n.tr('\n若桌面上仍看不到，是图标处于分类隐藏状态——点悬浮条"全部"显示全部图标。',
                   '\nIf they are still invisible, icons are hidden by a category '
                   'view — click "All" on the floating bar to show all icons.')
    # ★R213（NEW5-E4）：R191 已删"接力重启"——恢复后只有资源管理器
    # 重启固化，本程序原地继续运行；"与本程序"挂账 5 轮未改，与页面
    # 底部"无需重启"口径互相矛盾，统一为仅重启资源管理器
    msg += i18n.tr('\n接下来将自动重启资源管理器，以固化布局（文件夹窗口会关闭）。',
                   '\nExplorer will restart automatically to make the '
                   'layout permanent (folder windows will close).')
    # ★ R91：恢复后 30 分钟内点 All 自动进入定向看护；其余时间点 All
    # 不启用看护，用户整理图标零干扰
    try:
        import time as _t
        gui.dock._restore_watch_pending = _t.time() + 1800
    except Exception:
        pass
    if info.get('exported'):
        msg += i18n.tr(
            f"\n备份时间：{str(info['exported'])[:19].replace('T', ' ')}",
            f"\nBackup taken: {str(info['exported'])[:19].replace('T', ' ')}")
    messagebox.showinfo(i18n.tr('恢复完成', 'Restore Finished'), msg,
                        parent=gui.root)


def on_export_explorer_groups(gui):
    """导出文件夹组"""
    from tkinter import filedialog
    # ★R213（NEW5-E5）：死 import 删除——R210 B11 改走 _export_tabs_state
    # 统一入口后本函数再无 explorer_tabs 引用
    path = _save_dialog(gui, defaultextension='.json',
        initialfile=i18n.tr(f'文件夹组_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json',
                            f'Folder_Groups_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'),
        filetypes=[('JSON', '*.json')])
    if not path:
        return
    try:
        # ★R210（NEW4-B11）：走统一导出入口——本地读失败时不把
        # 空组+_load_failed 写进用户备份文件；写盘返回值必须消费
        st = _export_tabs_state()
        if st is None:
            # ★R213（NEW5-E5）：R210 新弹窗漏走 i18n.tr（规约双语成对）
            # ★R231-5：措辞照实说——这条是"读不动所以不导出空数据"，
            # 旧文案把 JSON 解析失败说成"或被占用"（本批 T1/T3 同族口径），
            # 且不提"页里就能自救"
            messagebox.showerror(
                i18n.tr('无法导出文件夹组', 'Cannot Export Folder Groups'),
                i18n.tr('文件夹组配置本会话读取失败（文件内容已损坏），\n'
                        '为防止把空数据写进备份文件，已中止导出。\n'
                        '想让它能导出：在本页「清空文件夹组」或「导入文件夹组」，'
                        '确认那步会先把损坏原件另存留底再放行。',
                        'The folder-group file failed to load this session '
                        '(its content is corrupted).\nExport aborted to avoid '
                        'writing empty data into your backup file.\n'
                        'To make export work again: use "Clear folder groups" '
                        'or "Import folder groups" on this page and confirm '
                        'the set-aside step.'),
                parent=gui.root)
            return
        if not atomic_write_json(path, st, indent=2):
            raise OSError('写盘失败（详见日志）')
        gui._log(f'✓ 已导出文件夹组 → {path}')
    except Exception as e:
        logger.error(f'导出文件夹组失败: {e}')
        messagebox.showerror(i18n.tr('错误', 'Error'),
                             f'{i18n.tr("导出失败", "Export failed")}: {e}',
                             parent=gui.root)


def on_import_explorer_groups(gui):
    """导入文件夹组（2026-09-05 改为合并：同名组覆盖，现有其他组保留。

    原来是整组覆盖，用户导入一个单组文件就把 程序/英语 全冲掉了）"""
    from tkinter import filedialog
    path = _open_dialog(gui, filetypes=[('JSON', '*.json')])
    if not path:
        return
    try:
        # ★ R42：统一格式识别（sniff）替代 R34 手写特征检查
        from io_formats import sniff, mismatch_message, KIND_GROUPS
        kind = sniff(path)
        if kind and kind != KIND_GROUPS:
            messagebox.showerror(i18n.tr('文件不匹配', 'File Mismatch'),
                                 mismatch_message(KIND_GROUPS, kind),
                                 parent=gui.root)
            return
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        if not isinstance(data.get('groups'), list):
            messagebox.showerror(
                i18n.tr('错误', 'Error'),
                i18n.tr('文件格式不对（缺少 groups）——请选【📤 导出文件夹组】'
                        '生成的 JSON',
                        'Wrong file format (the groups field is missing) - '
                        'pick the JSON from "Export Folder Groups"'),
                parent=gui.root)
            return
        # 结构校验（审查修复）：组名字符串 + folders 字符串列表
        groups = []
        for g in data['groups']:
            if not isinstance(g, dict) or not isinstance(g.get('name'), str) or not g['name']:
                continue
            fs = g.get('folders', [])
            if not isinstance(fs, list) or not all(isinstance(x, str) for x in fs):
                continue
            groups.append({'name': g['name'], 'folders': fs,
                           'last_used': g.get('last_used', 0)})
        if not groups:
            messagebox.showwarning(
                i18n.tr('提示', 'Notice'),
                i18n.tr('文件里没有有效的文件夹组',
                        'The file holds no valid folder group'),
                parent=gui.root)
            return
        if not messagebox.askyesno(
                i18n.tr('确认导入', 'Confirm Import'),
                i18n.tr(f'将合并导入 {len(groups)} 个组：\n'
                        f'· 与现有同名的组 → 覆盖\n'
                        f'· 现有其他组 → 保留\n确定？',
                        f'{len(groups)} group(s) will be merged in:\n'
                        '- A group with a name you already have is overwritten\n'
                        '- Your other groups are kept\nContinue?'),
                parent=gui.root):
            return
        from core import explorer_tabs
        state = explorer_tabs.load_state(APP_DIR / 'config')
        # ★R231（用户 05 批 item2）：读取失败时旧写法是"合并完再被 save_state
        # 静默拒写"（只留一行 ⚠ 日志，用户以为导好了）。改为先走自救闸。
        if not _recover_folder_groups(gui, state,
                                      ('导入文件夹组',
                                       'importing folder groups')):
            return
        existing = {g['name']: g for g in state.get('groups', [])}
        added = updated = 0
        for g in groups:
            if g['name'] in existing:
                updated += 1
            else:
                added += 1
            existing[g['name']] = g
        merged = list(existing.values())
        state['groups'] = merged
        if state.get('active_group') not in [g['name'] for g in merged]:
            state['active_group'] = merged[0]['name'] if merged else ''
        if _save_tabs_checked(state, gui):
            gui._refresh_explorer_list()
            gui._log(f"✓ 已合并导入文件夹组：新增 {added} 个、覆盖 {updated} 个，"
                  f"现共 {len(merged)} 个（详见 📦 数据管理 页列表）")
    except Exception as e:
        logger.error(f'导入文件夹组失败: {e}')
        messagebox.showerror(i18n.tr('错误', 'Error'),
                             f'{i18n.tr("导入失败", "Import failed")}: {e}',
                             parent=gui.root)


def on_clear_folder_groups(gui):
    """清空全部文件夹组（保留选项卡开关设置）"""
    # ★R213（NEW5-E5）：R210 新增两弹窗补 i18n 成对
    if not messagebox.askyesno(
            i18n.tr('确认清空', 'Confirm Clear'),
            i18n.tr('将清空全部文件夹组（各组的文件夹记录全部删除，'
                    '图标本身不受影响）。确定？',
                    'This deletes all folder-group records (icons themselves '
                    'are unaffected). Continue?'),
            parent=gui.root):
        return
    try:
        from core import explorer_tabs
        state = explorer_tabs.load_state(APP_DIR / 'config')
        # ★R210（NEW4-B3）→★R231（用户 05 批 item2）：本地 _load_failed=读不动
        # （损坏/占用）。旧写法弹错中止 ⇒ 和分类一样是死路；现在改为自救闸：
        # 确认 → 损坏原件另存留底（本文件此前连 .bak 都没有）→ 再清空。
        # 注意仍**不能**直接 pop 标记硬写（那才是打穿 CORE-5 拒写闸）。
        if not _recover_folder_groups(gui, state,
                                      ('清空文件夹组',
                                       'clearing folder groups')):
            return
        state['groups'] = []
        state['active_group'] = ''
        if _save_tabs_checked(state, gui):
            gui._refresh_explorer_list()
            gui._log('🗑️ 已清空全部文件夹组')
    except Exception as e:
        logger.error(f'清空文件夹组失败: {e}')


def on_clear_all_privacy(gui):
    """清空全部个人数据（分发前用）：图标名单/文件夹组/布局/统计/状态/日志"""
    if not messagebox.askyesno(
            i18n.tr('清空全部个人数据', 'Clear All Personal Data'),
            i18n.tr('将清空：所有分类的图标名单、全部文件夹组、布局备份、\n'
                    '点击统计、隐藏状态、日志、下载记录（含忽略规则）。\n'
                    '★ 分类列表将还原为出厂初始（导入的、自建的分类会删除）。\n\n'
                    '用于把软件分发给别人前抹掉个人使用痕迹。\n'
                    '自己的布局请先"📦 导出zip"或导出分类数据留底。确定？',
                    'This clears: all category icon lists, all folder groups, '
                    'layout backups,\nclick stats, hidden states, logs and '
                    'download records (incl. ignore rules).\n'
                    '★ The category list will be reset to factory defaults '
                    '(imported and self-created categories are removed).\n\n'
                    'Intended for wiping personal traces before sharing the app.\n'
                    'Back up your layouts via "📦 Export ZIP" or category export '
                    'first. Continue?'),
            parent=gui.root):
        return
    # ★R231（用户 05 批 item2，实拍图4）：★R30 在这里是"直接弹无法清空并中止"
    # ⇒ 分类文件损坏的用户连"清空重来"这条路都没有。改为自救闸：
    # 确认 → 损坏原件另存留底 → 解除禁写 → 照常清空。
    if not _recover_categories(gui, ('清空全部个人数据',
                                     'clearing all personal data')):
        return
    try:
        # ★ R16（R14 遗留同口径修复）：先恢复全量可见——
        # ① 分发场景应给接收者一个完整可见的桌面；② 否则清空 _hidden
        # 内存后，物理 HIDDEN|SYSTEM 属性仍在的图标要等下次启动自愈才回来。
        # unhide_all 会恢复全部文件属性 + 系统虚拟图标（均有自愈兜底的方法）
        # ★R305：收纳档下"该隐藏的"是真的躺在盒子里，光清属性看不见它们 ⇒ 分发出去
        #   的桌面缺图标。先把本系统的盒散回桌面，再走下面这套清空。
        from core import visibility_router as _vr
        _vr.release_all_for(gui.icon_manager, 'clear-all', gui._log)
        gui.icon_manager.unhide_all()
        try:
            gui.dock._force_listview_refresh()
        except Exception:
            pass
        # 1. 图标名单 + 分类框架还原出厂初始
        # ★ R36（用户拍板）：分类名/自建分类也是个人痕迹——"清空全部
        # 个人数据"必须把列表还原成初始（此前只清 icons 名单、保留
        # 导入的/自建的分类框架，用户实测"列表还原不了初始"）
        if not gui.category_manager.restore_defaults():
            # ★ R30：写盘失败不再静默——保留现场（不清日志）让用户排查
            logger.error('清空全部个人数据：分类还原默认写盘未生效，中止后续清理')
            messagebox.showerror(
                i18n.tr('清空未生效', 'Clear Did Not Take Effect'),
                i18n.tr('分类还原默认写入磁盘失败（文件可能被占用/只读/被杀软拦截）。\n'
                        '已保留现场日志（data/app.log），未清除任何数据，请排查后重试。',
                        'Writing the factory-default categories failed (file may '
                        'be locked/read-only/blocked by antivirus).\n'
                        'Nothing was cleared. See data/app.log for details.'),
                parent=gui.root)
            return
        # ★ R36：写后复核——重新读盘确认分类集合==出厂默认且 icons 全空
        _chk_err = ''
        try:
            import json as _json
            _chk = _json.loads((APP_DIR / 'config' / 'categories.json')
                               .read_text(encoding='utf-8'))
            _left_icons = sum(len(c.get('icons', [])) for c in _chk) \
                if isinstance(_chk, list) else -1
            _names = {c.get('name') for c in _chk} \
                if isinstance(_chk, list) else set()
            _req = {gui.category_manager._r_name(r) for r in
                    gui.category_manager.REQUIRED_CATEGORIES}
        except Exception as _e:
            _left_icons, _names = -1, set()
            _chk_err = repr(_e)
        if _left_icons != 0 or _names != _req:
            logger.error(f'清空全部个人数据：写后复核异常'
                         f'（盘上 icons={_left_icons}，分类={sorted(_names)}）')
            _detail = f'；复核读取异常: {_chk_err}' if _chk_err else ''
            messagebox.showerror(
                i18n.tr('清空未生效', 'Clear Did Not Take Effect'),
                i18n.tr(f'写入后复核发现磁盘数据异常（icons={_left_icons}，'
                f'分类集合与默认不符{_detail}）。\n'
                '可能有其他程序/实例在写回此文件。已保留现场，未清除任何数据。',
                        f'Post-write check found abnormal data on disk '
                        f'(icons={_left_icons}, category set differs from '
                        f'default{_detail}).\nAnother program/instance may be '
                        'writing this file. Nothing was cleared.'),
                parent=gui.root)
            return
        # ★ R36：分类框架变了——悬浮条槽位同步为默认集
        gui.dock.sync_slots(gui.category_manager.get_all_categories())
        # 2. 文件夹组（保留选项卡开关）
        from core import explorer_tabs
        st = explorer_tabs.load_state(APP_DIR / 'config')
        # ★R210（NEW4-B3）：同上——读失败拒清，不打穿拒写闸
        # ★R213（NEW5-E2）：记录真实执行结果——此前跳过文件夹组后
        # 日志/审计/终弹窗照样"已清空可分发"，盘上真实残留个人文件夹
        # 路径而存证说谎；自救指引与 on_clear_folder_groups 版拉齐
        _groups_cleared = False
        # ★R210（NEW4-B3）→★R231-5（主线程自查命中）：这一支原先是"读取失败
        # 就一律跳过 + 让用户重启程序"。本批把同入口的分类侧接上自救闸之后，
        # 文件夹组侧成了唯一死路——后果是"分发前抹个人痕迹"这个目的没达成
        # （盘上仍残留个人文件夹路径）。现在复用 G2 那条已验证的闸：
        # 确认 ⇒ 损坏原件另存留底 ⇒ 解禁 ⇒ 照清；只有"用户取消 / 留底真失败"
        # 才落到下面的跳过支，措辞也照实说（不再把 JSON 损坏说成"被占用"）。
        if st.get('_load_failed') and not _recover_folder_groups(
                gui, st, ('清空文件夹组', 'clearing the folder groups')):
            messagebox.showerror(
                i18n.tr('无法清空文件夹组', 'Cannot Clear Folder Groups'),
                i18n.tr('文件夹组配置本会话读取失败（文件内容已损坏），'
                        '你未选择"先留底再清空"，该部分已跳过。\n'
                        '为防止用空数据覆盖真实配置，这里不会强行写入。\n'
                        '再点一次「清空文件夹组」并在确认那步选"是"，'
                        '程序会自动留底后放行。',
                        'The folder-group file failed to load this session '
                        '(corrupted content) and you declined "set it aside '
                        'first", so this part was skipped.\nWriting was not '
                        'forced, to avoid overwriting real config with empty '
                        'data.\nRun "Clear folder groups" again and confirm '
                        'the set-aside step — the app keeps a copy, then '
                        'proceeds.'),
                parent=gui.root)
        else:
            st['groups'] = []
            st['active_group'] = ''
            _groups_cleared = bool(_save_tabs_checked(st, gui))
        # 3. 布局备份
        import glob as _glob
        removed = 0
        for f in _glob.glob(str(APP_DIR / 'icon_backups' / '*.json')):
            try:
                os.remove(f)
                removed += 1
            except Exception:
                pass
        # 4. 点击统计 / 隐藏状态 / 虚拟图标状态
        # ★ R14：clicks 改用 reset_clicks()——旧写法只清文件不清内存，
        # 统计页分类榜仍显示旧数据，且后续点击会把旧数据整本写回
        gui.click_tracker.reset_clicks()
        # ★ R16：内存同步清空（unhide_all 已恢复物理属性，此处清登记，
        # 与 hidden_state.json / virtual_icon_state.json 写 '{}' 保持一致）
        gui.icon_manager._hidden.clear()
        gui.icon_manager._currently_hidden = set()
        for f in ['config/hidden_state.json', 'config/virtual_icon_state.json']:
            fp = APP_DIR / f
            if fp.exists():
                try:
                    fp.write_text('{}', encoding='utf-8')
                except Exception as e:
                    # ★R284-5（P1 隐私）：这份没清成必须留痕——旧写法 except: pass 静默，
                    #   界面照样报"个人数据已清空"，盘上 hidden/virtual 痕迹原封不动。
                    logger.warning(f'清空个人数据时 {f} 未能写回空表（可能被占用/只读），'
                                   f'该状态可能仍留在盘上: {e}')
        # ★ R14（S 段）：使用统计（图标打开榜/文件夹打开榜）一并清零
        gui.usage_stats.reset()
        # ★ R112：下载记录一并清零（隐私红线：记录只落本地，清空要彻底）
        # ★R285-N9：这四份（history/ignore/forgotten/undo）里都有个人绝对路径，
        # "哪一份没删掉"必须一路带到完成弹窗——旧写法把 clear() 的返回值扔了，
        # 闸门态下文件原样留盘，界面却照旧报"可以打包分发了"（R229 的隐私承诺落空）。
        _dl_left = []
        try:
            _dl_left = [str(x) for x in
                        (gui.download_tracker.clear_data_file() or [])]
        except Exception as e:
            _dl_left = ['download_*.json']
            logger.warning(f'清空下载记录失败（这几份可能仍含个人路径）: {e}')
        # ★ R120：清空后刷新下载卡（此前界面残影，用户以为没清掉）
        try:
            from gui import download_panel as _dlp
            _dlp.refresh(gui)
        except Exception:
            pass
        # 5. 日志（★ P1-H 补清 crash/stderr/launcher/debug/error）
        for f in ['data/switch.log', 'data/app.log', 'logs/frame_debug.log',
                  'data/crash.log', 'data/stderr.log', 'data/launcher.log',
                  'data/win32_trail.log',
                  'logs/debug.log', 'logs/error.log']:
            fp = APP_DIR / f
            if fp.exists():
                try:
                    fp.write_text('', encoding='utf-8')
                except Exception:
                    pass
        # ★ R30/R34：清理后立即落一行结论——app.log 只剩这一行（无个人
        # 痕迹），同时是"清空流程确实执行完毕"的存证
        # ★R213（NEW5-E2）：结论按文件夹组实际结果分支，审计存证不谎报
        _done_txt = ('清空全部个人数据完成（图标名单/文件夹组/布局/统计/日志）'
                     if _groups_cleared else
                     '清空个人数据完成（文件夹组未清空：读取保护）')
        # ★R285-N9：审计存证跟着实际结果走（只写文件名，不含个人路径）
        if _dl_left:
            _done_txt += '；下载记忆未删净：%s' % '、'.join(_dl_left)
        logger.info('🔒 ' + _done_txt)
        # ★ R34：结论同步写独立审计文件（app.log 自清，save_audit.log
        # 不清——下次排查"清空后数据复活"时前后证据连成完整时间线）
        try:
            audit = APP_DIR / 'data' / 'save_audit.log'
            audit.parent.mkdir(parents=True, exist_ok=True)
            with open(audit, 'a', encoding='utf-8') as _af:
                import os as _os
                _af.write(f'{datetime.now():%Y-%m-%d %H:%M:%S} '
                          f'pid={_os.getpid()} 🔒 {_done_txt}\n')
        except Exception:
            pass
        # 6. 家长模式数据（密码/密保/状态）
        # ★ R154：密保问题此前漏清（用户 -09/6：密码、密保都得清空）
        gui.parental.clear_password()
        try:
            gui.parental.clear_questions()
        except Exception as _e:
            logger.warning(f'清空密保问题失败（忽略）: {_e}')
        # ★ R155：全局快捷键一并恢复默认（用户 -10/4：清空数据要连带）
        try:
            from gui import hotkey_panel as _hk
            from utils.hotkey_manager import DEFAULT_HOTKEY_CONFIG as _dhc
            gui._hotkey_cfg = {k: list(v) for k, v in _dhc.items()}
            gui._save_hotkey_config()
            _hk.apply_hotkeys(gui)
            _hk.update_list(gui)
        except Exception as _e:
            logger.warning(f'清空快捷键失败（忽略）: {_e}')
        # 7. 守护基准 + 上次分类/组记忆
        gui.icon_manager.disarm_guard()
        gui._app_settings.pop('last_active_slot', None)
        gui._app_settings.pop('last_guard_layout', None)
        # ★ P2-N18：清 UI 使用痕迹（dock 颜色 / 主题 / 字号 / 宽度 / 上次 tab）
        # 分发前抹掉这些个人偏好，让接收者拿到默认外观
        for _k in ('ui_tab', 'ui_theme', 'ui_font_size',
                   'dock_text_color', 'dock_active_text_color',
                   'cat_pane_width', 'main_window_width',
                   'main_window_height', 'last_dim_level',
                   # ★R266（批21 item1）：程序图标偏好同属"UI 痕迹"，
                   # 清空后回到默认那一套（01_B），不把个人选图带给下一位用户
                   'app_icon'):
            gui._app_settings.pop(_k, None)
        gui._save_app_settings()
        gui._update_lists()
        gui._refresh_explorer_list()
        # ★R266（批21 item2）：自动打开设置卡也要跟着刷新——这张卡的"勾选名单"
        # 只在 `_resync_startup_open_vars()` 跑过才跟盘对齐。原先清空全部个人数据
        # 时唯独漏了它，界面上还挂着刚被清掉的组名/路径，用户随手点一下卡上任何
        # 开关就把它们写回盘（＝"清空没清干净"，个人路径又回来了）。
        if hasattr(gui, '_resync_startup_open_vars'):
            try:
                gui._resync_startup_open_vars()
            except Exception as _e:
                logger.warning(f'清空后同步自动打开设置卡失败（界面可能与配置不一致）: {_e}')
        # ★ R14：统计页同步清空（图标/文件夹/分类三榜）
        if getattr(gui, 'stats_panel', None):
            gui.stats_panel.refresh()
        # ★R213（NEW5-E2）：终弹窗按实际结果分支——含读取保护跳过时
        # 明说"文件夹组未清空（盘上残留个人路径），暂勿分发"
        # ★R285-N9：同一把尺子量下载那四份（history/ignore/forgotten/undo 里全是
        # 个人绝对路径）。旧写法只看文件夹组，`clear_data_file()` 的返回值被扔掉 ⇒
        # 闸门态下文件原样留盘，弹窗照样报"可以打包分发了"。
        if _groups_cleared:
            gui._log('🔒 已清空全部个人数据（图标名单/文件夹组/布局/统计/日志/UI痕迹）')
        else:
            gui._log('🔒 个人数据清空完成，但文件夹组未清空（读取保护），'
                     '暂不建议分发')
        if _dl_left:
            gui._log('🔒 个人数据清空完成，但下载记忆未删净（%s），'
                     '暂不建议分发' % '、'.join(_dl_left))
        if _groups_cleared and not _dl_left:
            messagebox.showinfo(
                i18n.tr('完成', 'Done'),
                i18n.tr('个人数据已清空，可以打包分发了',
                        'Personal data cleared — ready for packaging/distribution'),
                parent=gui.root)
        else:
            _zb, _eb = [], []
            if not _groups_cleared:
                _zb.append('【文件夹组未清空】（读取保护）——\n'
                           '盘上仍残留个人文件夹路径。\n'
                           '再点一次「清空文件夹组」并在确认那步选"是"即可'
                           '（程序会先把损坏原件另存留底再放行）。')
                _eb.append('folder groups were NOT cleared (load protection) —\n'
                           'personal folder paths remain on disk.\n'
                           'Run "Clear folder groups" again and confirm the '
                           'set-aside step\n(the app keeps a copy of the corrupt '
                           'file first).')
            if _dl_left:
                _zb.append('【下载记忆没删干净】：%s——\n'
                           '那几份里仍可能含个人绝对路径。\n'
                           '请先关闭正在占用它们的程序（或重启本软件）后再清一次。'
                           % '、'.join(_dl_left))
                _eb.append('download memory was NOT fully cleared: %s —\n'
                           'those files may still contain personal absolute paths.\n'
                           'Close whatever holds them open (or restart the app) '
                           'and clear again.' % '、'.join(_dl_left))
            messagebox.showwarning(
                i18n.tr('部分完成', 'Partly Done'),
                i18n.tr('个人数据已清空，但\n' + '\n'.join(_zb) + '\n暂勿分发。',
                        'Personal data cleared, but\n' + '\n'.join(_eb)
                        + '\nDo not distribute yet.'),
                parent=gui.root)
    except Exception as e:
        logger.error(f'清空隐私数据失败: {e}')
        messagebox.showerror(i18n.tr('错误', 'Error'),
                             f'{i18n.tr("失败", "Failed")}: {e}', parent=gui.root)



def build_data_manage_tab(tab_dm, gui, colors):
        # ============ 📦 数据管理（导出/导入/清空，分发前清隐私）============
        dm_frame = ttk.LabelFrame(tab_dm, text=i18n.tr('📦 数据管理（分类 / 文件夹组 / 全部设置）', '📦 Data (Categories / Folder Groups / All Settings)'),
                                  padding='10', style='Card.TLabelframe')
        dm_frame.pack(fill=tk.X, pady=(SP['s'], 0))

        # ★R216 阶段2（用户 2026-09-23 图2："感觉别扭，又说不上来问题"）：
        # 病灶是 6 行 run-on pack——①两个同色实底按钮并排（权重相同、不知点
        # 哪个）②备份路径行右半页空③4 个 ghost 按钮间距忽 4 忽 12 ④说明文字
        # 飘在按钮尾巴右边⑤3 个红色危险按钮挤在左下且与常规操作同排。
        # 改成 2×2 功能区 + 底部危险区：组内 action_bar 等宽、form_row 对齐，
        # 整页实心只留"📤 导出全部数据"一个主操作。
        dm_grid = ttk.Frame(dm_frame)
        dm_grid.pack(fill=tk.X)
        dm_grid.columnconfigure(0, weight=1, uniform='dmcol')
        dm_grid.columnconfigure(1, weight=1, uniform='dmcol')

        # ---- 组①：📤 全量迁移（primary）----
        g_a, body_a = ui_layout.group_block(
            dm_grid, i18n.tr('📤 全量迁移（一个 JSON 全带走）',
                             '📤 Full Migration (one JSON)'), 'primary')
        g_a.grid(row=0, column=0, sticky='nw',
                 padx=(0, SP['l']), pady=(0, SP['m']))
        ui_layout.action_bar(body_a, [
            (i18n.tr('📤 导出全部数据', '📤 Export All Data'),
             gui._on_export_all_data, 'primary.TButton'),
            (i18n.tr('📥 导入全部数据', '📥 Import All Data'),
             gui._on_import_all_data, 'secondary.TButton'),
        ], cols=2, pad=SP['s'])
        ttk.Label(body_a,
                  # ★R249 补充1/D4：删掉已退役的"规则"，只列真在包里的前缀规则
                  text=i18n.tr('分类+前缀规则+设置+快捷键+高亮+文件夹组'
                               '+布局存档+统计+分类清单',
                               'categories+prefix rules+settings+hotkeys'
                               '+highlight+folder groups+layouts+stats+manifest'),
                  foreground=colors['text_secondary'], justify=tk.LEFT,
                  wraplength=470,
                  font=ui_layout.app_font(9)).pack(anchor=tk.W, pady=(SP['xs'], 0))

        # ---- 组②：📁 备份位置（accent）----
        g_b, body_b = ui_layout.group_block(
            dm_grid, i18n.tr('📁 备份位置', '📁 Backup Location'), 'accent')
        g_b.grid(row=0, column=1, sticky='nw', pady=(0, SP['m']))
        # ★ R177（用户 -09/7）：备份路径=导出默认目录，默认程序内 backup/，
        # 可自定义（持久化 app_settings['backup_dir']），方便整体拷走
        bk_var = tk.StringVar(value=_backup_dir(gui))

        def _chg_backup():
            from tkinter import filedialog as _fd
            d = _fd.askdirectory(parent=gui.root,
                                 title=i18n.tr('选择备份/导出根目录',
                                               'Pick backup folder'),
                                 initialdir=bk_var.get() or None)
            if not d:
                return
            gui._app_settings['backup_dir'] = d
            gui._save_app_settings()
            bk_var.set(d)
            gui._log(f'📁 备份路径已设为: {d}')

        def _open_backup():
            import subprocess as _sp
            target = bk_var.get()
            # ★R233（用户 08 批 item4 同族收口，S4 自查新发现）：`explorer <路径>`
            # 走的是 shell "双击"语义——路径指到 .exe 就等于把那个程序跑起来。
            # 而 backup_dir 存在 config/app_settings.json 里，这份文件正是
            # 「导入全部数据」会原样落盘的一节 ⇒ 一份别人给的备份包就能把它
            # 换成任意路径，点一下「📂 打开备份目录」即执行。判据与文件夹组
            # 双击 / open_folders 共用 explorer_tabs.is_openable（口径只留一份）。
            from core import explorer_tabs
            if not explorer_tabs.is_openable(target):
                _shown = target or i18n.tr('（空）', '(empty)')
                gui._log(i18n.tr(f'⛔ 已拒开备份路径（不是现存文件夹）: {target!r}',
                                 f'⛔ Refused to open backup path '
                                 f'(not an existing folder): {target!r}'))
                logger.warning(f'[备份位置] 拒开不可打开的路径: {target!r}')
                messagebox.showwarning(
                    i18n.tr('路径无效', 'Invalid Path'),
                    i18n.tr(f'备份路径不是一个当前存在的文件夹，已拒绝打开：\n{_shown}\n\n'
                            '若这份设置来自你刚导入的备份文件，请重新点'
                            '"📁 选择"指回自己的目录。',
                            f'The backup path is not a folder that currently '
                            f'exists, so opening it was refused:\n{_shown}\n\n'
                            'If this setting came from a backup file you just '
                            'imported, click "📁 Choose" and point it back to '
                            'your own folder.'),
                    parent=gui.root)
                return
            try:
                _sp.Popen(['explorer', target])
            except Exception:
                pass

        fb = ui_layout.form_block(body_b)
        ui_layout.form_row(
            fb, 0, i18n.tr('导出默认存到这里:', 'Exports default here:'),
            ttk.Label(fb, textvariable=bk_var, foreground=colors['secondary'],
                      font=ui_layout.app_font(9)))
        ui_layout.action_bar(body_b, [
            (i18n.tr('更改', 'Change'), _chg_backup, 'secondary.TButton'),
            (i18n.tr('📂 打开', '📂 Open'), _open_backup, 'secondary.TButton'),
        ], cols=2, pad=SP['s'])

        # ---- 组③：🗂️ 分类与文件夹组（secondary）----
        g_c, body_c = ui_layout.group_block(
            dm_grid, i18n.tr('🗂️ 分类与文件夹组', '🗂️ Categories & Folder Groups'),
            'secondary')
        g_c.grid(row=1, column=0, sticky='nw',
                 padx=(0, SP['l']), pady=(0, SP['m']))
        ui_layout.action_bar(body_c, [
            (i18n.tr('📤 导出分类', '📤 Export Categories'),
             gui._on_export_categories, 'secondary.TButton'),
            (i18n.tr('📥 导入分类', '📥 Import Categories'),
             gui._on_import_categories, 'secondary.TButton'),
            (i18n.tr('📤 导出文件夹组', '📤 Export Folder Groups'),
             gui._on_export_explorer_groups, 'secondary.TButton'),
            (i18n.tr('📥 导入文件夹组', '📥 Import Folder Groups'),
             gui._on_import_explorer_groups, 'secondary.TButton'),
        ], cols=2, pad=SP['s'])
        # 当前文件夹组一览（2026-09-05 用户反馈：导入后看不见组都在不在）
        gui.dm_groups_var = tk.StringVar(value='')
        ttk.Label(body_c, textvariable=gui.dm_groups_var, wraplength=420,
                  justify=tk.LEFT, foreground=colors['muted'],
                  font=ui_layout.app_font(9)).pack(anchor=tk.W,
                                                       pady=(SP['xs'], 0))
        gui._refresh_dm_groups_label()

        # ---- 组④：📦 桌面图标文件（muted）----
        # ★ R41：桌面图标文件备份/恢复（图标文件本身丢失的兜底——布局存档
        # 只存位置坐标，文件丢了救不回来；本组备份的是文件，恢复后可再套布局）
        g_d, body_d = ui_layout.group_block(
            dm_grid, i18n.tr('📦 桌面图标文件', '📦 Desktop Icon Files'),
            'text_secondary')
        g_d.grid(row=1, column=1, sticky='nw', pady=(0, SP['m']))
        ui_layout.action_bar(body_d, [
            (i18n.tr('📦 导出桌面图标', '📦 Export Desktop Icons'),
             gui._on_export_desktop_icons, 'secondary.TButton'),
            (i18n.tr('📥 恢复桌面图标', '📥 Restore Desktop Icons'),
             gui._on_restore_desktop_icons, 'secondary.TButton'),
        ], cols=2, pad=SP['s'])
        ttk.Label(body_d,
                  text=i18n.tr('备份桌面上的文件与文件夹（文件夹含全部内容）；',
                               'Backs up desktop files and folders (folders with contents); ')
                       + i18n.tr('图标丢失后：先恢复文件，再恢复布局',
                                 'after icon loss: restore files first, then layouts'),
                  foreground=colors['text_secondary'],
                  font=ui_layout.app_font(9), justify=tk.LEFT,
                  wraplength=470
                  ).pack(anchor=tk.W, pady=(SP['xs'], 0))

        # ---- 组⑤：🧹 危险操作（error，整宽独立成组）----
        # ★R216：原来 3 个红色按钮跟常规操作混在同一列流里，"清空全部个人数据"
        # 这种不可逆操作离常规按钮只差一次点击。收进底部独立组 + 整宽等宽。
        g_e, body_e = ui_layout.group_block(
            dm_frame, i18n.tr('🧹 危险操作（不可逆）', '🧹 Dangerous (irreversible)'),
            'error')
        g_e.pack(fill=tk.X, pady=(SP['xs'], 0))
        ui_layout.action_bar(body_e, [
            (i18n.tr('🧹 清空图标分类（保留分类框架）',
                     '🧹 Clear Icon Categories (keep framework)'),
             gui._on_clear_icon_data, 'error.TButton'),
            (i18n.tr('🗑️ 清空文件夹组', '🗑️ Clear Folder Groups'),
             gui._on_clear_folder_groups, 'error.TButton'),
            (i18n.tr('🔒 清空全部个人数据（分发前用）',
                     '🔒 Clear All Personal Data (pre-distribution)'),
             gui._on_clear_all_privacy, 'error.TButton'),
        ], cols=3, pad=SP['s'])

        ttk.Label(dm_frame,
                  text=i18n.tr('"全部数据" = 分类+前缀规则+全部设置+快捷键+高亮'
                               '+文件夹组+布局存档+使用统计+分类清单（一个 JSON 文件）。',
                               '"All Data" = categories+prefix rules+all settings'
                               '+hotkeys+highlight+folder groups+layouts+usage stats'
                               '+manifest (one JSON file).')
                       # ★R213（NEW5-E4）：原文"导入后重启程序生效"与
                       # NEW3-B7 定案口径（多数即时生效）冲突，同步
                       + i18n.tr('多数项导入后即时生效，个别面板未刷新时重启程序即可。',
                                 'most items apply immediately; restart if a panel '
                                 'does not refresh.')
                       + i18n.tr('清空类操作用于把软件分发给别人前抹掉个人使用痕迹。',
                                 'Clear operations wipe personal traces before '
                                 'sharing the app.'),
                  foreground=colors['text_secondary'], wraplength=880,
                  justify=tk.LEFT,
                  font=ui_layout.app_font(9)).pack(anchor=tk.W, pady=(SP['s'], 0))


