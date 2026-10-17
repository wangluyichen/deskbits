# -*- coding: utf-8 -*-
"""★R266（批21 item1）：程序图标——默认「B 拾碎成序」，六套方案全部随包，
用户/客户都能自己换，并且换完能**直接定位到图标文件**。

用户原话："图标选B和F，然后存档……客户可以右键修改图标，直接就定位图标文件，
到时候可以改。默认B，其他的图标都存里面吧，都可以改，B，F排第一第二。"

三条口径：
1. **资产在包里**：`images/app_icons/`（打包脚本 `--include-data-dir=images=images`
   已经带着走，`app_bootstrap.sync_bundled_resources` 会刷到 exe 旁），
   文件名前缀＝显示序：`01_B_拾碎成序` `02_F_工具箱方块` `03_A…`~`06_E…`。
   客户不想用程序里那一套，也可以右键快捷方式→属性→更改图标→指这里的 .ico。
2. **默认 B**：根目录 `icon.png` 就是 B ⇒ exe 文件图标（`_prepare_icon()`）
   与运行时窗口/任务栏图标同源，不用改打包脚本。
3. **选中的东西不许骗人**：设置里存的只是**文件名**，每次用之前都回盘上核一次
   存在不存在；文件被删/换包丢了 ⇒ 自动退回默认那一套并记一条日志，
   绝不拿一个不存在的路径去设图标。
"""
from __future__ import annotations

import json
from pathlib import Path

SUBDIR = Path('images') / 'app_icons'
SETTING_KEY = 'app_icon'
MANIFEST = 'manifest.json'


def icons_dir(app_dir) -> Path:
    return Path(app_dir) / SUBDIR


def list_icons(app_dir) -> list:
    """返回 [{stem, name, ico, png}]，顺序＝显示序（B 第一、F 第二）。

    manifest 读不动就退回扫目录（按文件名排序），保证换了包、
    手工往目录里放一张 ico 也能被认出来。"""
    d = icons_dir(app_dir)
    out = []
    mf = d / MANIFEST
    if mf.is_file():
        try:
            raw = json.loads(mf.read_text(encoding='utf-8'))
            for it in (raw if isinstance(raw, list) else []):
                if not isinstance(it, dict):
                    continue
                stem = str(it.get('file') or '').strip()
                if not stem:
                    continue
                ico = d / stem
                if not ico.is_file():
                    continue
                png = d / str(it.get('preview') or '')
                out.append({'stem': stem,
                            'name': str(it.get('name') or stem),
                            'ico': ico,
                            'png': png if png.is_file() else None})
        except Exception:
            out = []
    if not out:
        for ico in sorted(d.glob('*.ico')):
            png = ico.with_suffix('.png')
            out.append({'stem': ico.name, 'name': ico.stem, 'ico': ico,
                        'png': png if png.is_file() else None})
    return out


def resolve(app_dir, app_settings) -> Path | None:
    """取当前该用的 .ico 路径；设置里的文件不在盘上就退回默认（第一个）。"""
    items = list_icons(app_dir)
    if not items:
        return None
    want = str((app_settings or {}).get(SETTING_KEY) or '')
    if want:
        hit = next((it for it in items if it['stem'] == want), None)
        if hit is None and Path(want).is_absolute():
            p = Path(want)
            hit = next((it for it in items if it['ico'] == p), None)
        if hit is not None:
            return hit['ico']
    return items[0]['ico']


def apply_to_window(widget, ico_path) -> bool:
    """把 ico 设成窗口/任务栏图标（Tk 在 Windows 只认 .ico）。

    返回是否真的设上了——设不上不炸启动路径，只让调用方记一行日志。"""
    if ico_path is None:
        return False
    try:
        widget.iconbitmap(default=str(ico_path))
        return True
    except Exception:
        return False


def explorer_select_cmd(path) -> str:
    """`explorer` 定位某个文件的命令行（单独抽出来是为了能被判据钉住）。

    收成这一个出口有两个理由：① 判据钉得住（旧写法散在调用点里，K10 只能把整个
    `reveal_in_explorer` 打桩，那行 argv 从没被验过）；② 不依赖 `list2cmdline`
    怎么拼参数。⚠ **别把这句当缺陷记录**：★R266 真机 A/B 实测
    （`/select, "路径"` vs `/select,"路径"`，截图状态栏都是"选中 1 个项目"）
    ⇒ 逗号后带空格**在这台 Win10 上也会选中**，旧写法并没有坏。
    整体加引号是为了带空格的目录；NTFS 文件名不允许 `"`，所以没有注入面。"""
    return 'explorer /select,"%s"' % str(path)


def reveal_in_explorer(ico_path) -> bool:
    """在资源管理器里**选中**这个图标文件（用户要的"直接就定位图标文件"）。"""
    import subprocess
    import os
    if ico_path is None:
        return False
    p = Path(ico_path)
    if not p.is_file():
        return False
    try:
        subprocess.Popen(explorer_select_cmd(p))
        return True
    except Exception:
        try:
            os.startfile(str(p.parent))      # 定位失败至少把文件夹打开
            return True
        except Exception:
            return False
