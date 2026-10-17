"""智能分类模块 - 前缀规则 + 扩展名 + 关键词自动识别图标类别"""
import os
import re
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from utils.safeio import atomic_write_json, set_aside_corrupt

logger = logging.getLogger("desktop_manager")

# 默认首字母前缀规则（用户可在界面上自由编辑）
# 多字母前缀可实现"第二个字母"消歧：长前缀优先匹配
#   G-钉钉 → 工作组；GY-xxx → 公共组；GZ-xxx → 工作；WL-xxx → 网络；YL-xxx → 娱乐
DEFAULT_PREFIX_RULES: Dict[str, str] = {
    'gy': '公共',
    'gz': '工作',
    'g': '工作',
    'wl': '网络',
    'w': '网络',
    'yl': '娱乐',
    'y': '英语',
    'a': 'AI',
    'l': '临时',
}

# 规则配置版本：升级新默认规则时 +1，旧配置自动补齐缺失项
PREFIX_CONFIG_VERSION = 2


class PrefixRules:
    """首字母前缀规则：图标名以某前缀开头即归入对应分类（如 G→工作, GY→公共, WL→网络）

    多字母前缀按长度优先匹配，实现"看第二个字母"消歧：
    G-钉钉→工作；GY-xxx→公共；GZ-xxx→工作；WL-xxx→网络。
    """

    def __init__(self, config_dir: str):
        self.config_dir = config_dir
        self.path = os.path.join(config_dir, 'prefix_rules.json')
        # ★R313 D2：留底搬不动时才置真——置真＝这份内存不可信，本轮一律禁写
        #   （★R231"闸门要能独立成立"：download_tracker/condition_monitor/
        #   hotkey_panel 都是这个形态，本模块是 safeio 普及时的漏网之鱼）
        self._load_failed = False
        Path(config_dir).mkdir(parents=True, exist_ok=True)
        self.rules: Dict[str, str] = self._load()

    def _load(self) -> Dict[str, str]:
        version = 0
        rules: Dict[str, str] = {}
        if os.path.exists(self.path):
            try:
                with open(self.path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict) and 'rules' in data:
                    version = int(data.get('version', 1))
                    rules = {str(k).lower(): v for k, v in data['rules'].items()}
                else:  # 旧版扁平格式
                    rules = {str(k).lower(): v for k, v in data.items()}
            except Exception as e:
                logger.error(f"加载前缀规则失败: {e}")
                # ★ R130：解析失败先备份原文件——随后的版本升级 _save 会用
                #   默认规则覆写本文件，没有备份用户自定义规则就永久丢了
                #   （对照 category_manager 的 P1-1 设计）
                # ★R313 D2（P1）：旧写法自己 copy2 到**固定名 .bak**——★R204
                #   CORE-31 已定案"第二次损坏会毁掉唯一那份留底"（本批实测：
                #   连坏两次后盘上只剩第二代，第一代那份带用户规则的已经没了）。
                #   改走 utils/safeio.set_aside_corrupt（带时间戳、同名不互盖）。
                _kept, _dst = set_aside_corrupt(self.path)
                if not _kept:
                    # 连留底都搬不动（被占用/只读）⇒ 这一份**一发都不许覆写**：
                    # 旧写法只 warning 一句就照常走升级路 _save，等于把唯一
                    # 还能手工救的原件也毁掉。
                    self._load_failed = True
                    logger.warning("损坏的前缀规则未能另存留底（%s）⇒ 本轮拒写，"
                                   "原件保留在盘上待人工恢复" % (_dst,))
        # 版本升级：只补缺失的默认规则，不覆盖用户自定义
        if version < PREFIX_CONFIG_VERSION:
            for k, v in DEFAULT_PREFIX_RULES.items():
                rules.setdefault(k, v)
            self.rules = rules
            self._save(version=PREFIX_CONFIG_VERSION)
        return rules

    def _save(self, version: int = PREFIX_CONFIG_VERSION):
        if self._load_failed:
            logger.warning("前缀规则本轮拒写：盘上那份读不动且留底失败"
                           "（内存是默认值，写下去会覆盖用户数据）")
            return False
        try:
            ok = atomic_write_json(self.path,
                                   {'version': version, 'rules': self.rules},
                                   indent=4)
        except Exception as e:
            logger.error(f"保存前缀规则失败: {e}")
            return False
        # ★R284-2（P1 静默丢数据）：atomic_write_json 返回 False=没落盘（占用/只读/满），
        #   旧写法把返回值当语句丢弃 ⇒ 用户改的前缀规则以为存了、重启就没了。
        if not ok:
            logger.error("保存前缀规则未落盘（文件可能被占用/只读），本次改动重启后会丢失")
        return bool(ok)

    def all(self) -> Dict[str, str]:
        return self.rules.copy()

    def set_rule(self, prefix: str, category: str) -> bool:
        """★R313 S6 回补 P-D：把 `_save` 的落盘结果交出去。

        旧写法 `self._save()` 当语句用：D2 那道独立禁写闸立起来之后拒写只进
        日志，面板照样从内存刷新 ⇒ 用户读成"存好了"，重启就没了（★R284-2
        "返回值必须消费"同族）。
        """
        prefix = prefix.strip().lower()
        if not prefix:
            return False
        self.rules[prefix] = category.strip()
        return self._save()

    def delete_rule(self, prefix: str) -> bool:
        prefix = prefix.strip().lower()
        if prefix not in self.rules:
            return False
        del self.rules[prefix]
        return self._save()

    def match(self, name: str) -> Optional[str]:
        """返回匹配到的前缀分类，未匹配返回 None（长前缀优先）"""
        low = name.strip().lower()
        for p in sorted(self.rules, key=len, reverse=True):
            if low.startswith(p):
                return self.rules[p]
        return None


# 扩展名 → 分类映射
EXTENSION_TO_CATEGORY: Dict[str, str] = {
    # 程序
    '.exe': '程序', '.lnk': '程序', '.bat': '程序', '.cmd': '程序',
    '.com': '程序', '.msi': '程序', '.appref-ms': '程序',
    # 网络/浏览器
    '.url': '网络', '.html': '网络', '.htm': '网络',
    # 办公
    '.pdf': '办公', '.doc': '办公', '.docx': '办公',
    '.xls': '办公', '.xlsx': '办公', '.ppt': '办公', '.pptx': '办公',
    '.txt': '办公', '.csv': '办公', '.rtf': '办公',
    '.wps': '办公', '.et': '办公', '.dps': '办公',
    # 视频/媒体
    '.mp4': '视频', '.avi': '视频', '.mkv': '视频', '.flv': '视频',
    '.wmv': '视频', '.mov': '视频', '.mpg': '视频', '.mpeg': '视频',
    '.m4v': '视频', '.3gp': '视频',
    '.mp3': '视频', '.wav': '视频', '.flac': '视频', '.aac': '视频',
    '.ogg': '视频', '.wma': '视频', '.m4a': '视频',
    '.jpg': '视频', '.jpeg': '视频', '.png': '视频', '.gif': '视频',
    '.bmp': '视频', '.webp': '视频', '.tiff': '视频',
    # 英语学习
    '.chm': '英语', '.epub': '英语', '.mobi': '英语',
    '.swf': '英语',
    # 学习资料
    '.zip': '学习', '.rar': '学习', '.7z': '学习', '.tar': '学习', '.gz': '学习',
    '.py': '学习', '.js': '学习', '.java': '学习', '.cpp': '学习', '.c': '学习',
    '.h': '学习', '.cs': '学习', '.go': '学习', '.rs': '学习',
    '.md': '学习', '.json': '学习', '.xml': '学习', '.yaml': '学习', '.yml': '学习',
    '.sql': '学习', '.db': '学习',
}

# 文件名关键词 → 分类
NAME_KEYWORDS: List[Tuple[str, str]] = [
    # ===== 系统桌面图标 =====
    ('此电脑', '__system__'),
    # ★ R130 前移：原排在其后的 ('网络','__system__') 会先命中"网络工具"
    #   （含"网络"二字），这条永不可达
    ('网络工具', '网络'),
    ('网络', '__system__'),
    ('回收站', '__system__'),
    ('控制面板', '__system__'),
    ('管理员', '__system__'),
    ('administrator', '__system__'),

    # ===== 浏览器/网络工具 =====
    ('chrome', '网络'),
    ('edge', '网络'),
    ('firefox', '网络'),
    ('youtube', '网络'),
    ('哔哩哔哩', '网络'),
    ('bilibili', '网络'),
    ('clash', '网络'),
    ('v2ray', '网络'),
    ('telegram', '网络'),
    ('openvpn', '网络'),
    ('wireshark', '网络'),
    ('net', '网络'),
    ('network', '网络'),
    ('vpn', '网络'),
    ('proxy', '网络'),

    # ===== 通讯软件 =====
    ('微信', '程序'),
    ('qq', '程序'),
    ('腾讯qq', '程序'),
    ('钉钉', '程序'),
    ('to desk', '程序'),
    ('向日葵', '程序'),
    ('teamviewer', '程序'),
    ('vnc', '程序'),
    ('moba', '程序'),
    ('securecrt', '程序'),
    ('securefx', '程序'),
    ('putty', '程序'),
    ('ssh', '程序'),
    ('chatbox', '程序'),

    # ===== 开发工具 =====
    ('pycharm', '程序'),
    ('idea', '程序'),
    ('visual studio', '程序'),
    ('vscode', '程序'),
    ('node', '程序'),
    ('docker', '程序'),
    ('git', '程序'),
    ('trae', '程序'),
    ('sublime', '程序'),
    ('notepad', '程序'),
    ('notepad++', '程序'),
    ('oracle vm', '程序'),
    ('virtualbox', '程序'),
    ('vmware', '程序'),
    ('python', '程序'),
    ('code', '程序'),
    ('programming', '程序'),
    ('编程', '程序'),
    ('hermes', '程序'),
    ('clau', '程序'),
    ('zcode', '程序'),
    ('net-manager', '程序'),
    ('game', '程序'),
    ('TerminateRunning', '程序'),

    # ===== 虚拟机/网络实验 =====
    ('ensp', '学习'),
    ('topology', '学习'),
    ('h3c', '学习'),
    ('huawei', '学习'),
    (' Cisco', '学习'),
    ('cisco', '学习'),
    ('模拟器', '学习'),
    ('模拟', '学习'),

    # ===== 媒体播放/视频 =====
    ('vlc', '视频'),
    ('obs', '视频'),
    ('audacity', '视频'),
    ('spotify', '视频'),
    ('网易云音乐', '视频'),
    ('音乐', '视频'),
    ('荐片', '视频'),
    ('影音', '视频'),
    ('电影', '视频'),
    ('视频', '视频'),

    # ===== 办公 =====
    ('福昕', '办公'),
    ('pdf', '办公'),
    ('word', '办公'),
    ('excel', '办公'),
    ('powerpoint', '办公'),
    ('ppt', '办公'),
    ('wps', '办公'),
    ('office', '办公'),
    ('促销码', '办公'),
    ('序列号', '办公'),
    ('听力真题', '英语'),

    # ===== 英语学习 =====
    ('英语', '英语'),
    ('单词', '英语'),
    ('ielts', '英语'),
    ('ielts模考', '英语'),
    ('欧路', '英语'),
    ('有道', '英语'),
    ('翻译', '英语'),
    ('爱听写', '英语'),
    ('剑听', '英语'),
    ('词典', '英语'),
    ('字典', '英语'),
    ('语料库', '英语'),

    # ===== 云盘/下载 =====
    ('百度网盘', '程序'),
    ('阿里云盘', '程序'),
    ('123云盘', '程序'),
    ('download', '程序'),
    ('下载', '程序'),
    ('library', '程序'),
    ('z-library', '程序'),

    # ===== 金融/交易 =====
    ('同花顺', '程序'),
    ('委托交易', '程序'),
    ('监控', '程序'),
    ('东港', '程序'),

    # ===== 系统工具 =====
    ('monitor', '程序'),
    ('hwmonitor', '程序'),
    ('ics', '程序'),
    ('desktop icon', '程序'),
    ('desktopicon', '程序'),
    ('backup manager', '程序'),
    ('context menu', '程序'),
    ('tool', '程序'),
    ('工具', '程序'),

    # ===== AI 工具 =====
    ('ai', 'AI'),
    ('gpt', 'AI'),
    ('chatgpt', 'AI'),
    ('claude', 'AI'),
    ('ollama', 'AI'),
    ('豆包', 'AI'),
    ('kimi', 'AI'),
    ('deepseek', 'AI'),
    ('copilot', 'AI'),
    ('本地ai', 'AI'),

    # ===== 学习资料 =====
    # （R130：原有一条 ('ai','学习') 永不可达——上面的 ('ai','AI') 首中即
    #   返回，删除以免误导；整词匹配后亦然）
    ('学习', '学习'),
    ('教程', '学习'),
    ('文档', '学习'),
    ('资料', '学习'),
    ('备份', '学习'),
    ('backup', '学习'),
]


def _ascii_word_in(kw: str, name: str, lower: str) -> bool:
    """★ R130：纯 ASCII 关键词的整词包含判断。

    出现处两侧都不是 ASCII 字母/数字才算词边界；另容忍驼峰切换
    （MobaXterm 的 moba、Xcode 的 code、OpenAI 的 ai 也算命中）。"""
    start = 0
    while True:
        i = lower.find(kw, start)
        if i < 0:
            return False
        j = i + len(kw)
        before = name[i - 1] if i > 0 else ''
        after = name[j] if j < len(name) else ''
        before_ok = (not before) or not (before.isascii() and before.isalnum())
        after_ok = (not after) or not (after.isascii() and after.isalnum())
        if not before_ok and before.isascii() and before.isupper() \
                and (not after or after.islower()):
            before_ok = True    # 驼峰：Xcode 的 code
        if not before_ok and i > 0 and name[i].isascii() and name[i].isupper() \
                and name[i - 1].isascii() and name[i - 1].islower():
            before_ok = True    # 驼峰转换位：OpenAI 的 ai（A 为大写转换点）
        if not after_ok and after.isascii() and after.isupper() \
                and (i == 0 or (before.isascii() and before.islower())):
            after_ok = True     # 驼峰：MobaXterm 的 moba（词首也算）
        if before_ok and after_ok:
            return True
        start = i + 1


def localize_category_name(name: str) -> str:
    """★R215（NEW6-B1）：把分类器输出的**中文出厂名**换成当前语言下的同名分类。

    本模块三张表（DEFAULT_PREFIX_RULES / EXTENSION_TO_CATEGORY /
    NAME_KEYWORDS）的目标分类全部写死中文（'工作'/'公共'/'娱乐'/'学习'/'AI'…），
    而 EN 界面下分类框架早已按 R96 迁成英文名（Work/Public/Study/Kids…）。
    不翻译就直接 `add_category('学习')` → 与已有 'Study' **并存两个同义分类**，
    图标被劈成两半、显示规则/布局按名匹配全失准。

    映射只覆盖 REQUIRED_CATEGORIES 那 7 个出厂名，且与
    `CategoryManager._r_name` **同源**（不在这里另立一份 zh→en 表，避免
    两边改一边漏改的 R46 双头陷阱）。其余名字（'网络'/'程序'/'办公'…
    以及用户自建名、用户改过的规则名）原样返回——非出厂名没有"同义英文
    分类"可言。中文界面零影响（is_en() 为假直接返回）。
    """
    if not name:
        return name
    try:
        from utils import i18n
        if not i18n.is_en():
            return name
        from core.category_manager import CategoryManager
        for r in CategoryManager.REQUIRED_CATEGORIES:
            if r['name'] == name:
                return r.get('name_en', name)
    except Exception as e:
        logger.warning(f'分类名本地化失败（按原名处理）: {e}')
    return name


def classify_icon(name: str, extension: str = '',
                  prefix_rules: Optional['PrefixRules'] = None) -> str:
    """
    根据图标名称智能判断所属分类。
    优先级：首字母前缀规则 > 扩展名 > 关键词，未匹配返回 '其他'。
    """
    # 0. 首字母前缀规则（用户自定义，如 w→网络, y→英语）
    if prefix_rules is not None:
        matched = prefix_rules.match(name)
        if matched:
            return matched

    # 1. 扩展名匹配
    if extension:
        ext = extension.lower().strip()
        if ext in EXTENSION_TO_CATEGORY:
            return EXTENSION_TO_CATEGORY[ext]

    # 2. 文件名关键词匹配
    # ★ R130 修复子串误命中：'ai' 命中 Mail/Baidu、'git' 命中 digital、
    #   'ics' 命中 physics、'code' 命中 barcode、'edge' 命中 knowledge、
    #   'word' 命中 password——纯 ASCII 关键词改为整词匹配（两侧不是
    #   ASCII 字母数字才算边界，另容驼峰切换如 MobaXterm/OpenAI）；
    #   中文无词边界概念，维持包含语义。
    lower_name = name.lower()
    for keyword, category in NAME_KEYWORDS:
        kw = keyword.lower()
        if kw.isascii():
            if not _ascii_word_in(kw, name, lower_name):
                continue
        elif kw not in lower_name:
            continue
        if category == '__system__':
            return '其他'
        return category

    # 3. 默认返回 "其他"
    return '其他'


def get_all_desktop_icons(icon_manager,
                          prefix_rules: Optional['PrefixRules'] = None) -> List[Dict[str, object]]:
    """获取桌面所有图标的名称和位置信息。"""
    positions = icon_manager.get_current_icon_positions()
    icons = []
    for name, (x, y) in positions.items():
        ext = Path(name).suffix.lower() if '.' in name else ''
        base_name = Path(name).stem
        category = localize_category_name(
            classify_icon(base_name, ext, prefix_rules))
        icons.append({
            'name': name,
            'x': x,
            'y': y,
            'ext': ext,
            'base_name': base_name,
            'category': category,
        })
    return icons


def auto_categorize_icons(
    icon_manager,
    category_manager,
    prefix_rules: Optional['PrefixRules'] = None
) -> Dict[str, List[str]]:
    """对所有桌面图标进行智能分类并写入 CategoryManager。"""
    icons = get_all_desktop_icons(icon_manager, prefix_rules)
    result: Dict[str, List[str]] = {}
    for icon in icons:
        cat = icon['category']
        if cat not in result:
            result[cat] = []
        result[cat].append(icon['name'])

    for cat_name, icon_names in result.items():
        # ★R204 NEW-C11：无名条目不再炸——统一走 .get
        existing = [c.get('name') for c in category_manager.get_all_categories()]
        if cat_name not in existing:
            category_manager.add_category(cat_name)
    # ★ R193（Qoder CORE-17）：图标名单改批量写——原逐图标
    # add_icon_to_category，每个图标一次全量 JSON 落盘（200 图标=200 次
    # 同步写，UI 线程卡死数秒到数十秒），且中途失败半应用
    category_manager.add_icons_to_category_batch(result)
    return result


def get_suggested_categories(
    icon_manager,
    category_manager,
    prefix_rules: Optional['PrefixRules'] = None
) -> Dict[str, List[str]]:
    """预览智能分类结果，但不写入分类管理器。"""
    icons = get_all_desktop_icons(icon_manager, prefix_rules)
    result: Dict[str, List[str]] = {}
    for icon in icons:
        cat = icon['category']
        if cat not in result:
            result[cat] = []
        result[cat].append(icon['name'])
    return result
