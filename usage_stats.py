"""使用统计（★ S 段/B18）：桌面图标打开 / 文件夹打开 的计数排行。

数据文件：data/usage_stats.json（atomic_write_json 原子写）。
- icon_opens:   {图标名: {"count": int, "last_ts": iso}} —— 桌面图标双击打开
- folder_opens: {路径:   {"count": int, "last_ts": iso}} —— 文件夹成功打开
分类切换计数不在本文件（历史数据在 clicks.json / ClickTracker），统计页并列展示。
"""
import json
import logging
import os
import threading
import time
from datetime import datetime

from utils.safeio import atomic_write_json

logger = logging.getLogger("desktop_manager")


class UsageStats:
    """打开次数统计（图标 / 文件夹）

    ★R204 NEW-R7：全部读改写共用一把 RLock——原无锁状态下主线程
    reload() 整体替换字典与工作线程 _bump_locked 并发互踩（数字回退/丢更新），
    json.dump 迭代中字典被替换还会抛 RuntimeError 被吞。"""

    def __init__(self, data_dir: str):
        self.path = os.path.join(data_dir, 'usage_stats.json')
        self._lock = threading.RLock()
        self.icon_opens = {}
        self.folder_opens = {}
        # ★R264 审计 Z10：损坏原件留不住底的会话禁写（先置位再读盘）
        self._load_failed = False
        # ★R270-R3：合并写状态（窗口内的改动先记脏，到点/到时机才落盘）
        self._dirty = False
        self._last_save_ts = 0.0
        self._load()

    # ------------------------------------------------------------------

    def _load(self):
        # ★R264 审计 Z10（P2）：旧写法损坏时只 error 一行 ⇒ 内存里是空账，
        # 却与"首建（本来就没文件）"完全同形；用户随后双击一个图标，
        # `_bump_locked` 立刻 _save 一份**只有 1 条**的账本原子覆盖整份历史
        # 榜单（写侧 ★R204/★R210 加固了三轮，读侧一直裸奔）。
        # 现在与 core/category_manager、core/download_tracker 同口径：
        # 损坏先另存留底，留不住底就立禁写闸；文件不存在=首建，照常放行。
        try:
            if os.path.exists(self.path):
                with open(self.path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    io_ = self._clean(data.get('icon_opens'))
                    fo_ = self._clean(data.get('folder_opens'))
                    with self._lock:   # ★R204 NEW-R7：整体替换原子化
                        self.icon_opens, self.folder_opens = io_, fo_
                else:
                    self._corrupt_keep_and_gate(
                        ValueError('顶层不是对象，是 %s' % type(data).__name__))
        except Exception as e:
            self._corrupt_keep_and_gate(e)

    def _corrupt_keep_and_gate(self, e):
        """损坏原件先另存留底，留不住底就立禁写闸（★R264 Z10）"""
        from utils.safeio import set_aside_corrupt
        kept, detail = set_aside_corrupt(self.path)
        if not kept:
            self._load_failed = True
        with self._lock:
            self.icon_opens, self.folder_opens = {}, {}
        logger.error('usage_stats.json 损坏，原件留底%s: %s'
                     % ('成功' if kept else '同样失败（可能被占用）', e))

    @staticmethod
    def _clean(raw) -> dict:
        """结构校验：键=字符串，值={count:int, last_ts:str}，畸形条目丢弃"""
        out = {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                if (isinstance(k, str) and isinstance(v, dict)
                        and isinstance(v.get('count'), int) and v['count'] >= 0):
                    out[k] = {'count': v['count'],
                              'last_ts': str(v.get('last_ts', ''))}
        return out

    def _save(self):
        # ★R264 审计 Z10：闸门必须独立成立（★R231 口径）——原件读不动又
        # 留不住底的会话里，任何一次写盘都是用"内存里那 1 条新记录"整份
        # 顶掉用户的历史榜单。
        if getattr(self, '_load_failed', False):
            logger.warning('本次未写使用统计：usage_stats.json 读不动且留底失败，'
                           '继续写会覆盖用户历史榜单')
            return False
        # ★R204 ROOT-2：返回值必须消费——写盘失败留痕（此前静默丢数据）
        # ★R204 NEW-R7：快照在锁内取（防 dump 迭代中被替换）
        # ★R210（NEW4-R6）：条目级深拷贝——dict(store) 浅拷贝与活动
        # store 共享内层对象，并发下可写出 count 已加 last_ts 未加的
        # 撕裂条目
        with self._lock:
            # ★R270-R3：真写一次就把"脏"和窗口起点一起归位（直接调 _save 的
            # 导入/重置路径也不会留下过期的脏标记）
            self._dirty = False
            self._last_save_ts = time.monotonic()
            payload = {
                'version': '1.0',
                'icon_opens': {k: dict(v) for k, v in self.icon_opens.items()},
                'folder_opens': {k: dict(v)
                                 for k, v in self.folder_opens.items()},
            }
        if not atomic_write_json(self.path, payload,
                                 indent=2, ensure_ascii=False):
            logger.error("使用统计写盘失败（数据仅存内存，退出即丢）")

    def reload(self):
        """★ R29：从文件重读（文件为唯一真相源）。

        统计面板刷新前调用——覆盖"面板打开期间其他来源写入/重启后
        内存与文件不同步"等场景，保证榜单显示的是文件里的最新状态。"""
        self.flush()      # ★R270-R3：合并写的窗口里还有未落盘的计数，先落盘
        self._load()

    # ------------------------------------------------------------------
    # ★R270-R3（用户 24 批："保障软件流畅的情况下，才保障统计的准确性"）：
    #   点击热路径原本**每点一次就整份重写** usage_stats.json（全量 dict 深拷贝
    #   + 临时文件 + 原子改名），长跑时每次双击都要摊上这一笔同步磁盘 I/O。
    #   现在：窗口内的多次改动只记"脏"，到点或遇到必须落盘的时机才写真身。
    #   代价（如实告知）：进程被强行杀死最多丢 _SAVE_COALESCE_SEC 秒的计数；
    #   正常退出 / 关闭统计 / 重读 / 导入导出前都会 flush，不丢数据。

    _SAVE_COALESCE_SEC = 15.0

    def _touch_save(self):
        """点击热路径专用：距上次真写不足窗口先记脏，不碰磁盘"""
        now = time.monotonic()
        with self._lock:
            if now - self._last_save_ts < self._SAVE_COALESCE_SEC:
                self._dirty = True
                return False
            self._dirty = False
            self._last_save_ts = now
        self._save()
        return True

    def flush(self):
        """把窗口内攒下的改动落盘（退出/关统计/reload/导出前调）"""
        with self._lock:
            if not self._dirty:
                return False
            self._dirty = False
            self._last_save_ts = time.monotonic()
        self._save()
        return True

    # ------------------------------------------------------------------

    def record_icon_open(self, name: str):
        # ★R207 NEW3-R1：store 引用必须在锁内绑定——锁外取 self.icon_opens
        # 再传参，reload() 在间隙重绑字典时改动会丢
        # ★R210（NEW4-R5）：恢复 try 包裹（新调用点异常不再直冒双击链）
        try:
            with self._lock:
                self._bump_locked(self.icon_opens, name)
            self._touch_save()
        except Exception as e:
            logger.error(f"记录图标打开失败 {name}: {e}")

    def record_folder_open(self, path: str):
        try:
            with self._lock:
                self._bump_locked(self.folder_opens, path)
            self._touch_save()
        except Exception as e:
            logger.error(f"记录文件夹打开失败 {path}: {e}")

    def _bump_locked(self, store: dict, key: str):
        """锁内自增（调用方必须已持锁）"""
        entry = store.setdefault(key, {'count': 0, 'last_ts': ''})
        entry['count'] = int(entry.get('count', 0)) + 1
        entry['last_ts'] = datetime.now().isoformat(timespec='seconds')

    # ------------------------------------------------------------------

    @staticmethod
    def top(store: dict, n: int = 50) -> list:
        """按次数降序取前 n：[(名称, 次数, 最近时间)]"""
        items = sorted(store.items(),
                       key=lambda kv: (-kv[1].get('count', 0), kv[0]))
        return [(k, v.get('count', 0), v.get('last_ts', ''))
                for k, v in items[:n]]

    def top_icons(self, n: int = 50) -> list:
        # ★R207 NEW3-R2：读侧也在锁内取快照——与 worker setdefault 并发时
        # 裸迭代可能 RuntimeError（被吞成"榜单渲染失败"）
        with self._lock:
            return self.top(dict(self.icon_opens), n)

    def top_folders(self, n: int = 50) -> list:
        with self._lock:
            return self.top(dict(self.folder_opens), n)

    # ------------------------------------------------------------------

    def export_dict(self) -> dict:
        # ★R213（NEW5-E1）：R6 深拷贝修在 _save 却漏了 export_dict——
        # dict(store) 浅拷贝共享内层条目对象，锁外 atomic_write_json dump
        # 期间并发 _bump 改 count/last_ts 会写出撕裂条目（与 NEW4-R6 同源）。
        with self._lock:   # ★R204 NEW-R7
            io_ = {k: dict(v) for k, v in self.icon_opens.items()}
            fo_ = {k: dict(v) for k, v in self.folder_opens.items()}
        return {'version': '1.0',
                'exported': datetime.now().isoformat(timespec='seconds'),
                'icon_opens': io_,
                'folder_opens': fo_}

    @staticmethod
    def _merge(cur: dict, new: dict) -> dict:
        """两份账合并：新键照收，同键 count 取**较大**、last_ts 取**较新**。

        ★R285-N1 的合并口径为什么不是"相加"：备份里可能是换机前导出的整份账
        （count 大），本地可能是导出之后又点了几次（last_ts 新）——相加会把重复
        那一段计两遍，取较大才是"两份里更完整的那一份"。ISO 时间串同时区同格式，
        字典序即时间序，可以直接 max。"""
        out = {k: dict(v) for k, v in cur.items()}
        for k, v in new.items():
            old = out.get(k)
            if not isinstance(old, dict):
                out[k] = dict(v)
                continue
            out[k] = {'count': max(int(old.get('count', 0)), int(v.get('count', 0))),
                      'last_ts': max(str(old.get('last_ts', '')),
                                     str(v.get('last_ts', '')))}
        return out

    def import_dict(self, data: dict) -> int:
        """把备份**合并**进现有账（导入备份/统计导入用）。返回包内有效条目数。

        ★R285-N1（P2）：统计页 📖 说明承诺的就是"📥 导入 = 榜单数据 JSON 备份
        （导入为合并，不清空现有）"（中英两份都是），旧实现却是整份顶掉——用户
        按文案的预期导一份旧备份，当前积累的榜单就静默没了，而这条路**没有**
        `🧹 清零` 那样的二次确认弹窗。修法是**实现跟文案走**（把文案改成"会清空"
        等于把风险藏起来，用户已经在按合并理解了）。
        入参不是 dict（读坏/拿错文件）也不再清账：合并空集＝保持原样。"""
        io_ = self._clean(data.get('icon_opens')) if isinstance(data, dict) else {}
        fo_ = self._clean(data.get('folder_opens')) if isinstance(data, dict) else {}
        n = len(io_) + len(fo_)
        with self._lock:   # ★R204 NEW-R7
            self.icon_opens = self._merge(self.icon_opens, io_)
            self.folder_opens = self._merge(self.folder_opens, fo_)
        self._save()
        return n

    def reset(self):
        with self._lock:   # ★R204 NEW-R7
            self.icon_opens = {}
            self.folder_opens = {}
        self._save()
