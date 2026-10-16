# -*- coding: utf-8 -*-
"""🔍 导入文件特征识别（★ R42：导入导出格式太多，统一"认文件+指路"）。

程序有 6 类备份格式，用户拿错文件时给**统一、指路**的提示：
"这是【分类数据】的文件，不是【全部数据】——请改用【📥 导入分类】"。

各类格式特征：
- 全部数据        dict 含 "categories"（且含 exported/layouts 等多键）
- 分类数据        顶层 list（元素含 name）
- 文件夹组        dict 含 "groups"
- 使用统计        dict 含 "icon_opens"
- 布局备份zip     成员为 *_YYYYMMDD_HHMMSS.json（内容含 name+icons）
- 桌面图标备份zip 含 "_备份信息.json" 清单
- 图标分类清单    文本以 "【分类名】" 开头

识别不出返回 None（各入口的原有详细校验兜底，不拦陌生文件）。
"""
import json
import re
import zipfile
from pathlib import Path


KIND_FULL = '全部数据'
KIND_CATS = '分类数据'
KIND_GROUPS = '文件夹组'
KIND_STATS = '使用统计'
KIND_LAYOUTZIP = '桌面布局备份'
KIND_ICONZIP = '桌面图标备份'
KIND_TXT = '图标分类清单'

# 按钮 → 期望格式（统一指路文案用）
KIND_BUTTON = {
    KIND_FULL: '📥 导入全部数据',
    KIND_CATS: '📥 导入分类',
    KIND_GROUPS: '📥 导入文件夹组',
    KIND_STATS: '📊 统计页 📥 导入',
    KIND_LAYOUTZIP: '📥 导入zip（已保存的布局区）',
    KIND_ICONZIP: '📥 恢复桌面图标（从备份解回）',
    KIND_TXT: '📥 导入清单（管理页）',
}

# ★R233（用户 08 批 item1）：English 档的"指路文案"要能整句读通。
#   KIND_* 与 KIND_BUTTON 的**值是逻辑 ID**（sniff 返回它、各入口拿它比较），
#   改值等于改协议（与悬浮条槽位名同理：值保持中文规范值，只在显示层翻）。
#   所以这里只做"显示用映射"，两个 dict 的键仍是那批 ID。
KIND_EN = {
    KIND_FULL: 'All Data',
    KIND_CATS: 'Categories',
    KIND_GROUPS: 'Folder Groups',
    KIND_STATS: 'Usage Stats',
    KIND_LAYOUTZIP: 'Desktop Layout Backup',
    KIND_ICONZIP: 'Desktop Icon Backup',
    KIND_TXT: 'Icon List',
}
KIND_BUTTON_EN = {
    KIND_FULL: '📥 Import All Data',
    KIND_CATS: '📥 Import Categories',
    KIND_GROUPS: '📥 Import Folder Groups',
    KIND_STATS: '📊 Stats page 📥 Import',
    KIND_LAYOUTZIP: '📥 Import ZIP (Saved Layouts box)',
    KIND_ICONZIP: '📥 Restore Desktop Icons (from backup)',
    KIND_TXT: '📥 Import List (Manage page)',
}


def _ui_is_en() -> bool:
    """当前是否 English 档（取不到语言模块时按中文走，绝不因为 i18n 出错而抛）"""
    try:
        from utils import i18n
        return bool(i18n.is_en())
    except Exception:
        return False


def _kind_of_json_obj(obj) -> str | None:
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) and 'name' in x for x in obj[:5]):
            return KIND_CATS
        return None
    if isinstance(obj, dict):
        if 'icon_opens' in obj:
            return KIND_STATS
        if 'groups' in obj:
            return KIND_GROUPS
        if 'categories' in obj:
            return KIND_FULL
    return None


def sniff_json(path) -> str | None:
    try:
        obj = json.loads(Path(path).read_text(encoding='utf-8'))
        return _kind_of_json_obj(obj)
    except Exception:
        return None


def sniff_zip(path) -> str | None:
    try:
        with zipfile.ZipFile(path, 'r') as z:
            names = z.namelist()
            if any(n.endswith('_备份信息.json') for n in names):
                return KIND_ICONZIP
            jsons = [n for n in names if n.lower().endswith('.json')]
            if not jsons:
                return None
            # 布局备份：成员名 <名字>_YYYYMMDD_HHMMSS.json 且内容含 name+icons
            layout_like = [n for n in jsons
                           if re.match(r'.+_\d{8}_\d{6}(\_\d+)?\.json$',
                                       n.rsplit('/', 1)[-1])]
            if layout_like:
                # ★R204 ROOT-17：验证对象应是候选布局文件本身（jsons[0]
                # 未必是）；布局成员解析失败不再"一律判布局 zip"
                try:
                    d = json.loads(z.read(layout_like[0]).decode('utf-8'))
                    if isinstance(d, dict) and 'name' in d and 'icons' in d:
                        return KIND_LAYOUTZIP
                except Exception as e:
                    import logging as _lg
                    _lg.getLogger('desktop_manager').warning(
                        f"sniff_zip: 布局成员解析失败 {layout_like[0]}: {e}")
                    return None
    except Exception:
        return None
    return None


def sniff_txt(path) -> str | None:
    try:
        head = Path(path).read_text(encoding='utf-8', errors='replace')[:200]
    except Exception:
        return None
    if re.search(r'^【.+?】', head, re.M):
        return KIND_TXT
    return None


def sniff(path) -> str | None:
    """识别文件类型；返回 KIND_* 或 None（不认识）"""
    p = str(path)
    low = p.lower()
    if low.endswith('.zip'):
        return sniff_zip(p)
    if low.endswith('.txt'):
        return sniff_txt(p)
    if low.endswith('.json'):
        return sniff_json(p)
    return sniff_json(p) or sniff_zip(p) or sniff_txt(p)   # 无扩展名按内容猜


def mismatch_message(expected: str, actual: str | None) -> str:
    """统一指路文案：actual=实际识别出的类型（None=未识别）

    ★R233（用户 08 批 item1）：English 档同样要读得通。旧实现只发中文，
    而调用点把标题双语化之后正文仍是中文 ⇒ "半英不英"更难看。
    中文仍是真源；English 分支自己组整句（不把中文类型名插进英文模板，
    ★R230b 口径），且**只用 ASCII 标点**（【】（）属 CJK 区间的字符，
    英文句子中出现一个就会被 English 门禁判红）。
    """
    if _ui_is_en():
        _e = KIND_EN.get(expected, expected)
        _b = KIND_BUTTON_EN.get(expected, expected)
        if actual:
            return (f'File mismatch: this is a "{KIND_EN.get(actual, actual)}" '
                    f'file.\nThis button only accepts "{_e}" - '
                    f'use [{_b}] instead.')
        return (f'Unrecognized format: this button only accepts "{_e}"\n'
                f'(generated by the matching Export for [{_b}]).')
    if actual:
        return (f'文件不匹配：这是【{actual}】生成的文件。\n'
                f'本按钮只认【{expected}】，请改用 '
                f'【{KIND_BUTTON.get(expected, expected)}】。')
    return (f'文件格式不对：本按钮只认【{expected}】\n'
            f'（由 {KIND_BUTTON.get(expected, expected)} 对应的"导出"生成）。')
