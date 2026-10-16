"""分类管理模块 - 管理图标分类和规则"""
import json
import os
import logging
from pathlib import Path
from typing import Dict, List, Optional

from utils.safeio import atomic_write_json

logger = logging.getLogger("desktop_manager")


class CategoryManager:
    """分类管理器"""
    
    def __init__(self, config_dir: str = None):
        self.config_dir = config_dir or str(Path(__file__).parent / "config")
        self.category_file = os.path.join(self.config_dir, "categories.json")
        Path(self.config_dir).mkdir(parents=True, exist_ok=True)
        self._load_failed = False   # ★ P1-1：分类加载失败且备份失败 → 本会话禁写盘
        # ★R281-S6④：另一件**不是一回事**的事——"读不动，但已把原件另存成 .bak，
        # 内存里这份是**出厂默认顶替**的"。这种情况 `_load_failed` 是 False（★P0-1
        # 故意留的：原件已经安全，写盘允许，盘与内存也确实一致），可任何"拿这份名单
        # 当用户真有哪些分类"的比较都会整榜误标（统计页 ⚠ 就在这）。
        self._fallback_defaults = False
        self.categories: List[dict] = self._load_categories()
    
    # ★ P2-1：保留名，禁止建分类占用（与 core/dock.UTILITY_SLOTS 工具槽同名，
    # 若分类重名会与工具槽冲突，破坏悬浮窗/切换逻辑）
    RESERVED_NAMES = ('全部', '窗', '一键目录', '✕')

    @staticmethod
    def is_rank_group(cat) -> bool:
        """★R279-⑦：这条分类是不是「排行组」（榜单第 M~N 名的名单快照）。

        身份证就是 `rank_source` 那个键（gui/rank_display.py 建组时写进去的），
        没有它的是普通手工分类。判据收在这一个函数里：
        「同名能不能覆盖」「进不进全部导入导出」「动不动布局」三处都读它，
        别各处再手写一遍 `c.get('rank_source')`——键名一改就漏一处。"""
        return bool(isinstance(cat, dict) and cat.get('rank_source'))

    @staticmethod
    def without_rank_groups(cats):
        """把分类表拆成 (留下的, 排行组)。

        产品负责人 09-30 04 批点名：排行组是**临时性**功能（"排行榜在变化"），
        所以不该跟着「数据 → 全部导入/导出」跨机器搬——换机后带过去的是一份
        已经过期的名单，用户还得手动去删。导出端、导入端各调一次这个函数。"""
        kept, skipped = [], []
        for c in (cats or []):
            (skipped if CategoryManager.is_rank_group(c) else kept).append(c)
        return kept, skipped

    def _meta_path(self):
        return os.path.join(self.config_dir, 'categories_meta.json')

    def _read_meta(self) -> dict:
        try:
            with open(self._meta_path(), 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}

    def _write_meta(self, meta: dict):
        try:
            atomic_write_json(self._meta_path(), meta, indent=2)
        except Exception as e:
            logger.error(f"写入迁移元数据失败: {e}")

    # 迁移用：升级时自动补齐的分类（已存在的不动）
    REQUIRED_CATEGORIES = [
        {"name": "工作",   "name_en": "Work",          "color": "#2196f3", "display_mode": "none"},
        {"name": "AI",     "name_en": "AI",            "color": "#a78bfa", "display_mode": "resident"},
        {"name": "公共",   "name_en": "Public",        "color": "#f1fa8c", "display_mode": "resident"},
        {"name": "娱乐",   "name_en": "Entertainment", "color": "#ff5555", "display_mode": "none"},
        {"name": "证券",   "name_en": "Securities",    "color": "#e06c75", "display_mode": "none"},
        {"name": "学习",   "name_en": "Study",         "color": "#4a7a5a", "display_mode": "none"},
        {"name": "小朋友", "name_en": "Kids",          "color": "#ff79c1", "display_mode": "exclusive_sys"},
    ]

    @staticmethod
    def _r_name(r):
        """★ R96：出厂分类名按当前语言选择——EN 环境恢复出厂得到英文名
        （Work/Public/...），建好后名字即稳定的用户数据，不再随语言变化"""
        try:
            from utils import i18n
            if i18n.is_en():
                return r.get('name_en', r['name'])
        except Exception:
            pass
        return r['name']

    def restore_defaults(self) -> bool:
        """★ R36：分类框架还原出厂初始（REQUIRED_CATEGORIES，图标名单全空）。

        供"清空全部个人数据（分发前用）"调用——分类名/自建分类本身也是
        个人痕迹。此前该操作只清 icons 名单、保留分类框架，用户实测
        "列表还原不了初始"（2026-09-13 拍板：清空=回到初始状态）。
        返回 True=已落盘。"""
        cats = [{"name": (self._r_name(r)), "color": r['color'], "icons": [],
                 "in_dock": True, "display_mode": r['display_mode']}
                for r in self.REQUIRED_CATEGORIES]
        self.categories = cats
        return self._save_categories()

    def rescue_corrupt_file(self):
        """★R231（用户 05 批 item2）：留底损坏的 categories.json 并解除本会话禁写。

        返回 (True, 留底路径) / (False, 原因)。留底失败**不解禁**——那时
        "文件被占用/只读"才是准确说法，硬写会把用户唯一那份原件冲掉。

        为什么 ★P1-1 的一律禁写要开这条口子：闸保护的是"别用内存里的出厂
        默认**偷覆盖**盘上真数据"，而"清空全部个人数据 / 导入分类"是用户
        **主动提供完整替换内容**，不是偷覆盖。旧实现把两者一起拦死（实拍图4/
        图5），用户只剩"手改 JSON"一条路；先另存留底再放行，闸的不变量没削弱。
        """
        if not getattr(self, '_load_failed', False):
            return True, ''
        from utils.safeio import set_aside_corrupt
        ok, detail = set_aside_corrupt(self.category_file)
        if not ok:
            return False, detail
        self._load_failed = False
        # 与 gui/data_manage_page 的自救日志、utils/log_i18n 词典整句对齐
        # （中文是唯一真源，English 显示层按整句翻，措辞不一致=漏翻）
        logger.warning('🛠 损坏的分类文件已另存留底，本会话写盘已解禁'
                       + (f' [{os.path.basename(detail)}]' if detail else ''))
        return True, detail

    def _load_categories(self) -> List[dict]:
        """加载分类数据。

        ★ P0-1：JSON 损坏（强杀写一半 / 磁盘满 / 杀软锁文件）时，绝不静默用默认覆盖：
        先把原文件备份为 .bak 再回退到默认，用户的分类数据永远可恢复。
        ★ P1-1：REQUIRED_CATEGORIES 仅补齐一次（一次性迁移标记）——不每次启动强制追加，
        用户主动删掉的不会自动回来，尊重用户意图。
        """
        migrated = False
        self._load_failed = False     # 重置；标记加载失败且备份失败 → 本会话禁写入原文件
        # ★R281-S6④：`_load_failed` 只管"能不能写盘"，**不**管"内存里这份是不是顶替来的"
        # ——损坏文件被成功另存成 .bak 那一路（最常见的损坏形态）历轮把 `_load_failed`
        # 留在 False，于是内存里躺着出厂默认却没人知道。首启（根本没有文件）也会拿到
        # 同一份默认值，但那**就是**真相，所以这个标记只在"有文件却读不动"时置位。
        self._fallback_defaults = False
        if os.path.exists(self.category_file):
            try:
                with open(self.category_file, "r", encoding="utf-8") as f:
                    cats = json.load(f)
                if not isinstance(cats, list):
                    raise ValueError(f"categories.json 顶层不是 list，实际是 {type(cats).__name__}")
                for c in cats:
                    # ★ R193（Qoder CORE-18）：无名条目先剔除再 normalize——
                    # _normalize 里 cat["name"] 缺失即 KeyError，原实现把
                    # 整个文件当损坏回退默认（用户的真实分类全丢）；实测
                    # 一个无名条目就炸（走 .bak 分支）
                    if not isinstance(c, dict) or not c.get('name'):
                        continue
                    self._normalize(c)
                _bad = len(cats)
                cats = [c for c in cats
                        if isinstance(c, dict) and c.get('name')]
                if len(cats) != _bad:
                    logger.warning(
                        f"categories.json 有 {_bad - len(cats)} 条无名条目，已剔除")
            except Exception as e:
                logger.error(f"加载分类数据失败（原文件备份为 .bak）: {e}")
                # ★R281-S6④：文件确实在、确实读不动 ⇒ 后面拿到的那份默认值是**顶替**
                # 出来的，不管这次备份成不成功都算（成功那一路 `_load_failed` 还是 False，
                # 靠的就是这个标记）。rescue_corrupt_file 不解这个标记：它解的是写盘禁令，
                # 内存里这份出厂壳到重启之前仍然是顶替。
                self._fallback_defaults = True
                # ★ P0-1：关键：先备份原文件，避免后续 _save_categories 覆盖
                bak_path = self.category_file + '.bak'
                if os.path.exists(bak_path):
                    # ★R204 CORE-31：.bak 已存在不覆盖——固定名覆盖会让第二
                    # 次损坏毁掉唯一那份可恢复留底。原损坏文件原样保留 +
                    # 本会话禁写（保守分支），用户数据不冒进。
                    logger.warning("已有 .bak 留底，损坏文件原样保留（本会话禁写）")
                    self._load_failed = True
                    cats = None
                else:
                    try:
                        os.replace(self.category_file, bak_path)
                        logger.warning(f"已备份损坏的分类文件 → {bak_path}")
                    except Exception as ee:
                        logger.error(f"备份损坏文件失败（将保留原文件，禁止写盘）: {ee}")
                        self._load_failed = True   # 备份失败 → 保守分支，本会话禁写
                    cats = None
        else:
            cats = None

        if cats is None:
            # 首次使用 / 加载失败：用默认分类
            # ★R214：出厂名走 _r_name（前 4 项 REQUIRED 同源）——English
            # 首启直接落英文名（用户冒烟：此前恒为中文，须手动"恢复默认"）
            cats = [
                {"name": self._r_name(r), "color": r["color"], "icons": [],
                 "in_dock": True, "display_mode": r["display_mode"]}
                for r in self.REQUIRED_CATEGORIES[:4]
            ]
            migrated = True

        # 迁移：清理与证券重复的"股票"默认分类（仅当没有图标时才删，
        # 用户真在用则保留——2026-09-05 用户确认默认里不该有股票）
        before = len(cats)
        cats = [c for c in cats
                if not (c.get('name') == '股票' and not c.get('icons'))]
        if len(cats) != before:
            logger.info('已清理空的默认分类 [股票]（与证券重复）')
            migrated = True

        # ★ P1-1：只有 meta 标记"未做 REQUIRED 补齐"时才做（一次性迁移）。
        # 不再每次启动都强制追加，尊重用户"删掉这个默认分类"的意图。
        # 不与 cats 本身耦合，用单独 meta 文件存迁移标记，不影响用户数据结构。
        meta = self._read_meta()
        if not meta.get('_migrated_required_categories'):
            # 第一次补齐 → 只做一次
            # ★R214：在位判定同时认中文/英文出厂名（EN 首启的 seed 已是
            # 英文名，不能再按中文名重复追加）；补齐名走 _r_name 随语言
            existing = {c['name'] for c in cats}
            for req in self.REQUIRED_CATEGORIES:
                if (req['name'] not in existing
                        and req.get('name_en', req['name']) not in existing):
                    cats.append({"name": self._r_name(req), "color": req['color'],
                                 "icons": [], "in_dock": True,
                                 "display_mode": req['display_mode']})
                    migrated = True
            meta['_migrated_required_categories'] = True
            logger.info("完成 REQUIRED 分类一次性迁移补齐")

        # ★R214：出厂分类名跟随语言切换（一次性）。用户冒烟反馈：切到
        # English 重启后默认分类仍显示中文，须手动点"恢复默认"才变英文。
        # 只改"名字还恰好等于出厂名（中/英任一形态）"的条目=用户没动过
        # 的出厂壳，改名/自建分类一概不碰；图标名单、颜色、成员原样保留
        # （等价于内置 rename_category，非破坏性）。
        try:
            from utils import i18n as _i18n
            _cur_lang = 'en' if _i18n.is_en() else 'zh'
        except Exception:
            _cur_lang = 'zh'
        if meta.get('_factory_lang') != _cur_lang:
            _renamed = []
            for req in self.REQUIRED_CATEGORIES:
                _zh, _en = req['name'], req.get('name_en', req['name'])
                _target = _en if _cur_lang == 'en' else _zh
                for c in cats:
                    if c.get('name') in (_zh, _en) and c['name'] != _target:
                        _renamed.append(f"{c['name']}→{_target}")
                        c['name'] = _target
            if _renamed:
                migrated = True
                logger.info(f"出厂分类名已跟随语言 [{_cur_lang}]: "
                            f"{'、'.join(_renamed)}")
            meta['_factory_lang'] = _cur_lang

        # ★R282-5（用户 10-01 01 批 R011-3）：非常驻排行组＝临时快照，界面上已承诺
        # "本次上栏，重启后自动消失"。dock 槽本就是内存态（`_session_rank`，重启即无），
        # 可分类条目却一直留在 categories.json ⇒ 管理→图标分类 里还能看见它（用户读成
        # "没删干净"）。载入时把"是排行组 且 没勾常驻(in_dock=False)"的条目一并清掉，
        # 让重启后 dock 与管理列表一起干净。**只清排行组**：常驻排行组、普通分类
        # （哪怕 in_dock=False）一律不动；走下面 migrated→save 那条出口落盘，
        # 受 _load_failed 写盘禁令保护（读不动时绝不写）。
        before_rk = len(cats)
        cats = [c for c in cats
                if not (self.is_rank_group(c) and not c.get('in_dock', True))]
        if len(cats) != before_rk:
            logger.info(f'已清理 {before_rk - len(cats)} 个非常驻排行组'
                        f'（临时快照，重启即消失；常驻排行组与普通分类不动）')
            migrated = True

        if migrated and not self._load_failed:
            self.categories = cats
            # ★ R130：真正落实 P2-1 意图——原代码没接 save 返回值就无条件
            #   写标记，写盘失败时标记照样置位，下次启动不再补齐默认分类
            if self._save_categories():
                self._write_meta(meta)
            else:
                logger.warning("默认分类补齐未能写盘，迁移标记不置位（下次启动重试）")
        elif self._load_failed:
            # ★R231（用户 05 批包内日志实拍）：这条日志原文写"备份也失败"，
            # 实际是"同目录已有 .bak，按 ★R204 CORE-31 故意不覆盖"——
            # 排障时照着"备份失败"去找磁盘问题会全找错方向。
            logger.warning("分类数据加载失败，本会话禁止写盘（损坏原件原样保留；"
                           "同目录 .bak 未被覆盖）⇒ 可在「📦 数据管理」页"
                           "清空或导入自救")
            self.categories = cats

        return cats

    @staticmethod
    def _normalize(cat: dict) -> dict:
        """补齐旧版本配置缺失的字段"""
        cat.setdefault("icons", [])
        cat.setdefault("in_dock", True)    # 是否显示在顶部悬浮窗
        # 显示模式（2026-09-03 三列网格）：resident=常驻 / exclusive=只显示自己 /
        # exclusive_sys=只显示自己且连系统图标也隐藏 / none=普通
        if "display_mode" not in cat:
            if cat.get("exclusive"):
                cat["display_mode"] = "exclusive_sys"
            elif cat["name"] == "小朋友":
                cat["display_mode"] = "exclusive_sys"   # 用户设计默认
            elif cat.get("sticky"):
                cat["display_mode"] = "resident"
            else:
                cat["display_mode"] = "none"
        # 同步旧字段（兼容仍读 sticky/exclusive 的代码路径）
        cat["sticky"] = cat["display_mode"] == "resident"
        cat["exclusive"] = cat["display_mode"] in ("exclusive", "exclusive_sys")
        return cat

    def _save_categories(self):
        """保存分类数据。返回 True=已落盘 / False=未落盘（★ R30）。

        ★ P1-1：加载失败且备份失败时禁止写盘——否则用户后续任意一次改动
        （add/rename/set_display_mode 都走这里）都会覆盖原文件，而 .bak
        里没有任何东西可恢复。
        ★ R30：atomic_write_json 失败是"返回 False"而不是抛异常——此前
        返回值被丢弃，写盘失败（文件被占用/只读/杀软拦截）会被当成成功，
        "清空全部个人数据"弹了"完成"但盘上数据原封不动（Win11 实测踩坑）。
        """
        if getattr(self, '_load_failed', False):
            logger.warning("本次会话禁止写盘（原分类文件加载失败且备份失败），改动未落盘")
            return False
        try:
            ok = atomic_write_json(self.category_file, self.categories, indent=4)
            if not ok:
                logger.error("保存分类数据失败：写入未生效（检查文件只读属性/占用/杀软拦截）")
            else:
                # ★ R32/R34 写盘审计：排查"清空后数据复活"——每次落盘记
                # pid+调用链前 3 帧。app.log 会被"清空全部个人数据"自清
                # （证据自毁，R34 教训），因此同时追加写独立审计文件
                # data\save_audit.log（清空流程不清它）。
                import inspect
                import os as _os
                from datetime import datetime as _dt
                st = inspect.stack()
                callers = ' <- '.join(
                    f'{fr.filename.split(chr(92))[-1]}:{fr.lineno}:{fr.function}'
                    for fr in st[2:5])
                line = (f'{_dt.now():%Y-%m-%d %H:%M:%S} pid={_os.getpid()} '
                        f'{len(self.categories)} 分类 icons='
                        f'{sum(len(c.get("icons", [])) for c in self.categories)}'
                        f' ← {callers}')
                logger.info('[分类写盘] ' + line)
                try:
                    audit = (Path(self.category_file).parent.parent
                             / 'data' / 'save_audit.log')
                    audit.parent.mkdir(parents=True, exist_ok=True)
                    with open(audit, 'a', encoding='utf-8') as f:
                        f.write(line + '\n')
                except Exception:
                    pass
            return bool(ok)
        except Exception as e:
            logger.error(f"保存分类数据失败: {e}")
            return False

    def get_all_categories(self) -> List[dict]:
        """获取所有分类"""
        return self.categories.copy()

    def add_category(self, name: str, color: str = "#8be9fd",
                     in_dock: bool = True, sticky: bool = False) -> bool:
        """添加新分类"""
        name = name.strip()   # ★ P2-2：归一化首尾空格，避免用户误输入产生视觉同名分类
        if not name:
            return False
        # ★ P2-1+P2-2：拦截保留名（与工具槽同名，避免占用'全部'/'窗'/'一键目录'/'✕'），含 .strip 检验
        if name in self.RESERVED_NAMES:
            return False
        for cat in self.categories:
            if cat["name"] == name:
                return False
        self.categories.append({"name": name, "color": color, "icons": [],
                                "in_dock": bool(in_dock),
                                "display_mode": "resident" if sticky else "none",
                                "sticky": bool(sticky)})
        # ★ R130：返回真实落盘结果（写盘失败调用方可见，不再恒报成功）
        return self._save_categories()

    def set_in_dock(self, name: str, in_dock: bool) -> bool:
        """设置分类是否显示在顶部悬浮窗"""
        for cat in self.categories:
            if cat["name"] == name:
                cat["in_dock"] = bool(in_dock)
                return self._save_categories()   # ★ R130 真实落盘结果
        return False

    def set_sticky(self, name: str, sticky: bool) -> bool:
        """设置常驻（兼容入口，内部走 display_mode）"""
        return self.set_display_mode(name, "resident" if sticky else "none")

    def get_sticky_icon_names(self, category_name: str = None) -> set:
        """获取 sticky 图标名集合。

        若指定 category_name，只返回该分类自身的 sticky 图标（选中 sticky
        分类时不附带其他 sticky 分类的图标）；不指定则返回所有 sticky 图标。
        """
        names = set()
        for cat in self.categories:
            if cat.get("display_mode") == "resident":
                if category_name is None or cat["name"] == category_name:
                    names.update(cat.get("icons", []))
        return names

    def is_sticky(self, name: str) -> bool:
        """判断分类是否为常驻（display_mode=resident）"""
        return self.get_display_mode(name) == "resident"

    def is_exclusive(self, name: str) -> bool:
        """判断分类是否"只显示自己"（含隐系统变体）"""
        return self.get_display_mode(name) in ("exclusive", "exclusive_sys")

    def get_display_mode(self, name: str) -> str:
        """分类显示模式：resident / exclusive / exclusive_sys / none"""
        for cat in self.categories:
            if cat["name"] == name:
                return cat.get("display_mode", "none")
        return "none"

    def get_exclude_system(self, name: str) -> bool:
        """这组是否勾了"不包含系统图标"（建排行组那一格的唯一读口）。

        它和"常驻叠加/只显示自己"那格是两件事：那一格管组的成员怎么摆，这一格
        只管桌面上那五枚系统图标的显隐。老分类没这个键 ⇒ False（行为一字不变）。"""
        for cat in self.categories:
            if cat["name"] == name:
                return bool(cat.get("exclude_system", False))
        return False

    def set_display_mode(self, name: str, mode: str) -> bool:
        """设置分类显示模式（三列网格互斥，由 UI 层保证）"""
        for cat in self.categories:
            if cat["name"] == name:
                cat["display_mode"] = mode if mode in (
                    "resident", "exclusive", "exclusive_sys") else "none"
                cat["sticky"] = cat["display_mode"] == "resident"
                cat["exclusive"] = cat["display_mode"] in ("exclusive", "exclusive_sys")
                return self._save_categories()   # ★ R130 真实落盘结果
        return False


    
    def delete_category(self, name: str) -> bool:
        """删除分类（★ R130：返回真实落盘结果）"""
        existed = any(c["name"] == name for c in self.categories)
        if not existed:
            return False
        self.categories = [c for c in self.categories if c["name"] != name]
        return self._save_categories()
    
    def rename_category(self, old_name: str, new_name: str) -> bool:
        """重命名分类（★ 重命名前校验新名不与其他分类重复，避免同名破坏按名匹配）"""
        new_name = new_name.strip()   # ★ P2-2：归一化首尾空格
        if not new_name:
            return False
        # ★ P2-1+P2-2：拦截保留名（与工具槽同名，避免占用'全部'/'窗'/'一键目录'/'✕'）
        if new_name in self.RESERVED_NAMES:
            return False
        for cat in self.categories:
            if cat["name"] == new_name and cat["name"] != old_name:
                return False    # 目标名已存在 → 拒绝，防止同名分类
        for cat in self.categories:
            if cat["name"] == old_name:
                cat["name"] = new_name
                return self._save_categories()   # ★ R130 真实落盘结果
        return False
    
    def set_color(self, name: str, color: str) -> bool:
        """设置分类颜色（★ R130：返回真实落盘结果）"""
        for cat in self.categories:
            if cat["name"] == name:
                cat["color"] = color
                return self._save_categories()
        return False
    
    def add_icon_to_category(self, category: str, icon_name: str) -> bool:
        """添加图标到分类（★ R130：返回真实落盘结果）"""
        for cat in self.categories:
            if cat["name"] == category:
                if icon_name not in cat["icons"]:
                    cat["icons"].append(icon_name)
                    return self._save_categories()
                return True
        return False

    def add_icons_to_category_batch(self,
                                    assignments: Dict[str, List[str]]) -> bool:
        """★ R193（Qoder CORE-17）：批量归类——内存全部改完只落盘一次。

        智能分类原逐图标调 add_icon_to_category：每个图标一次全量 JSON
        原子写，200 图标=200 次同步写，UI 线程卡死数秒到数十秒，中途失败
        还会半应用。缺分类时在内存创建（保留名沿用 add_category 拦截），
        全部就绪后单次 _save_categories。返回落盘结果（无变化=True）。
        ★R207 NEW3-C5：写盘失败精确回滚内存（撤新建分类+撤追加图标）——
        此前失败后内存保留幽灵分类，重启即蒸发（与导入路径的回滚不同源）。"""
        # ★R204 NEW-C11：gui 导入路径可能带入无名条目，c["name"] 下标=启动崩
        cats_by_name = {c["name"]: c for c in self.categories if c.get("name")}
        changed = False
        _created: List[str] = []          # 本次新建的分类名
        _added: List[tuple] = []          # (cat_dict, icon_name) 本次追加
        for cat_name, icon_names in (assignments or {}).items():
            cat_name = (cat_name or '').strip()
            if not cat_name or cat_name in self.RESERVED_NAMES:
                continue
            cat = cats_by_name.get(cat_name)
            if cat is None:
                cat = {"name": cat_name, "color": "#8be9fd", "icons": [],
                       "in_dock": True, "display_mode": "none"}
                self.categories.append(cat)
                cats_by_name[cat_name] = cat
                _created.append(cat_name)
                changed = True
            for icon_name in icon_names or []:
                if icon_name not in cat["icons"]:
                    cat["icons"].append(icon_name)
                    _added.append((cat, icon_name))
                    changed = True
        if not changed:
            return True
        if not self._save_categories():
            # ★R207 NEW3-C5：内存与磁盘保持一致——撤回全部内存变更
            for cat, icon_name in reversed(_added):
                try:
                    cat["icons"].remove(icon_name)
                except ValueError:
                    pass
            if _created:
                _created_set = set(_created)
                self.categories = [c for c in self.categories
                                   if c.get("name") not in _created_set]
            logger.error(f"批量归类写盘失败，已回滚内存变更"
                         f"（撤 {len(_added)} 条归属/{len(_created)} 个新分类）")
            return False
        return True
    
    def remove_icon_from_category(self, category: str, icon_name: str) -> bool:
        """从分类中移除图标（★ R130：返回真实落盘结果）"""
        for cat in self.categories:
            if cat["name"] == category:
                if icon_name in cat["icons"]:
                    cat["icons"].remove(icon_name)
                    return self._save_categories()
                return True
        return False
    
    def get_category_icons(self, category: str) -> List[str]:
        """获取分类下的图标列表"""
        for cat in self.categories:
            if cat["name"] == category:
                return cat["icons"].copy()
        return []
    
    def get_category_color(self, category: str) -> str:
        """获取分类颜色"""
        for cat in self.categories:
            if cat["name"] == category:
                return cat.get("color", "#8be9fd")
        return "#8be9fd"


class RuleManager:
    """规则管理器"""
    
    def __init__(self, config_dir: str = None):
        self.config_dir = config_dir or str(Path(__file__).parent / "config")
        self.rule_file = os.path.join(self.config_dir, "rules.json")
        Path(self.config_dir).mkdir(parents=True, exist_ok=True)
        self.rules: List[dict] = self._load_rules()
    
    def _load_rules(self) -> List[dict]:
        """加载规则数据"""
        if os.path.exists(self.rule_file):
            try:
                with open(self.rule_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"加载规则数据失败: {e}")
        return []
    
    def _save_rules(self):
        """保存规则数据"""
        try:
            ok = atomic_write_json(self.rule_file, self.rules, indent=4)
        except Exception as e:
            logger.error(f"保存规则数据失败: {e}")
            return False
        # ★R284-3（P1 静默丢数据）：写失败(False)不再静默——分类规则没存上必须留痕。
        if not ok:
            logger.error("保存分类规则未落盘（文件可能被占用/只读），本次改动重启后会丢失")
        return bool(ok)
    
    def get_all_rules(self) -> List[dict]:
        """获取所有规则"""
        return self.rules.copy()

    def replace_rules(self, rules: List[dict]) -> int:
        """整体替换规则（导入全部数据用）。结构校验后写入，返回导入条数"""
        clean: List[dict] = []
        for r in (rules if isinstance(rules, list) else []):
            if isinstance(r, dict) and isinstance(r.get('name'), str) and r['name']:
                try:
                    clean.append({
                        'name': r['name'],
                        'type': r.get('type', 'top_clicked'),
                        'threshold': int(r.get('threshold', 10)),
                        'used': int(r.get('used', 0)),
                    })
                except (TypeError, ValueError):
                    continue
        self.rules = clean
        self._save_rules()
        return len(clean)
    
    def add_rule(self, name: str, rule_type: str, threshold: int) -> bool:
        """添加规则"""
        for rule in self.rules:
            if rule["name"] == name:
                return False
        self.rules.append({
            "name": name,
            "type": rule_type,
            "threshold": threshold,
            "used": 0
        })
        self._save_rules()
        return True
    
    def delete_rule(self, name: str) -> bool:
        """删除规则"""
        self.rules = [r for r in self.rules if r["name"] != name]
        self._save_rules()
        return True
    
    def get_rule(self, name: str) -> Optional[dict]:
        """获取规则详情"""
        for rule in self.rules:
            if rule["name"] == name:
                return rule
        return None
    
    def use_category(self, name: str):
        """标记规则为已使用"""
        for rule in self.rules:
            if rule["name"] == name:
                rule["used"] = rule.get("used", 0) + 1
                self._save_rules()
                return True
        return False
