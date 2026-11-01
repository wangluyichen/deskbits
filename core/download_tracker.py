# -*- coding: utf-8 -*-
"""下载目录记忆（R112 V1，设计稿见 DEV.md"下载目录记忆·功能设计"）。

三条精华（设计稿提炼）：打开所在文件夹一键直达、按日期分组、目录聚合。
V1 数据两路：①启动解析 shell:Recent 的 *.lnk 回填（只读，零钩子）；
②ReadDirectoryChangesW 监听 用户Downloads+用户桌面（daemon 线程，
监控异常静默降级为"只靠 Recent 兜底"，绝不出弹窗）。

隐私红线：记录只落本地 data/download_history.json（封顶 500 条），
清空全部个人数据会一并清掉；不联网、不上报。
"""
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from utils.app_paths import get_app_dir
from utils.safeio import atomic_write_json, atomic_write_text

logger = logging.getLogger("desktop_manager")

TEMP_SUFFIXES = {'.crdownload', '.part', '.tmp', '.download', '.partial'}
EXCLUDE_NAMES = {'desktop.ini', 'thumbs.db'}
MAX_RECORDS = 500
RECENT_BACKFILL_DAYS = 30


def _user_downloads_dir() -> Path:
    """用户 Downloads 目录（注册表优先，落空回退 ~/Downloads）"""
    try:
        import winreg
        with winreg.OpenKeyEx(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer"
                r"\User Shell Folders", 0, winreg.KEY_READ) as k:
            v, _t = winreg.QueryValueEx(k, "{374DE290-123F-4565-9164-39C4925E467B}")
            p = Path(os.path.expandvars(v))
            if p.is_dir():
                return p
    except Exception:
        pass
    return Path.home() / 'Downloads'


def default_watch_dirs():
    """V1 监听目录：用户 Downloads + 用户桌面"""
    dirs = [_user_downloads_dir(), Path.home() / 'Desktop']
    out = []
    for d in dirs:
        try:
            if d.is_dir():
                out.append(str(d))
        except Exception:
            pass
    return out


class DownloadTracker:
    """下载记录器：Recent 回填 + 目录监听（线程内跑，出事只丢记录）"""

    def __init__(self, data_dir, log=None):
        self.data_dir = Path(data_dir)
        self.history_path = self.data_dir / 'download_history.json'
        self.clear_mark_path = self.data_dir / 'download_clear_mark.txt'
        self._clear_mark = self._load_clear_mark()   # ★ R120 清空水位线
        self.records = []            # 新的在前
        # ★R283：日志出口先于**所有**盘的读取建立——_load_* 走留底/拒写闸时要能 self.log
        # （★R285-N6：忽略名单也要走同一把闸，原来它排在日志出口之前，只能静默回空）
        self._log_cb = log
        self.log = self._safe_log
        # ★R285-N6（Z5/★R283 同族第三份）：download_ignore.json 此前读坏了静默回
        # []、无留底、无禁写闸 ⇒ 坏档被下一次"加忽略规则/导入"原子覆盖、零留底。
        # 现在与黑名单/撤回栈同口径：先置闸位再读盘（复用本模块 _load_list_state）。
        self._ignore_load_failed = False
        self._ignore = self._load_ignore()
        # ★R283（对齐本模块 _load/_save 的 R264-Z5 双保护）：黑名单/撤回栈同族漏改，
        # 读坏了会被下一次 forget()/push_undo() 用空内存原子覆盖原件 → 先置禁写闸再读盘
        self.undo_path = self.data_dir / 'download_undo.json'
        self._undo_load_failed = False
        self._undo = self._load_undo()   # ★ R175 撤回栈（持久化，重启不丢）
        self.forgotten_path = self.data_dir / 'download_forgotten.json'
        self._forgotten_load_failed = False
        # ★ R177 被忽略条目黑名单（持久化）——"忽略此条"若只删记录，
        # 浏览器历史/Recent 再同步会把同一条反复加回（用户 001.jpg
        # "点忽略又出现"实锤 src=chrome）；进黑名单后同步层直接拒
        self._forgotten = self._load_forgotten()
        self.watch_dirs = default_watch_dirs()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads = []
        # ★R264 Z5：禁写闸先置位再读盘（留底失败的会话里整程拒写）
        self._load_failed = False
        self._load()

    def _safe_log(self, m):
        """日志回调兜底：回填线程可能在 UI 建好前就产出消息
        （gui._log 依赖 log_text 控件），绝不让日志炸掉记录线程"""
        try:
            self._log_cb(m)
        except Exception:
            logger.info(f"[下载记忆] {m}")

    # ---- 存储 ----

    def _load_clear_mark(self):
        """★ R120：清空水位线——清空全部个人数据的时刻。之后的同步
        （浏览器历史/Recent/监听）不再收录早于该时刻的下载，否则刚
        清空就被再同步"复活"（用户 2026-09-17 实测）。"""
        try:
            return datetime.fromisoformat(
                self.clear_mark_path.read_text(encoding='utf-8').strip())
        except Exception:
            return None

    def _load(self):
        # ★R264 审计 Z5：旧写法损坏时只 warning + records=[]，随后监视线程
        # 落定一条下载（或用户跑「导入全部数据」直接调 _save）就用内存里那份
        # 空账**原子覆盖**损坏原件 ⇒ 最多 500 条记录的人工抢救机会归零。
        # 同族对照 core/condition_monitor.py:77-114、category_manager 都是
        # "留底 + 禁写闸"双保护，本模块漏改。现在照抄：
        #   · 解析失败/形状不对/读不动 ⇒ 先另存留底；留底成功 ⇒ 空账可以继续
        #     （原件仍在盘上，用户能人工抢救）；
        #   · 连留底都搬不动（被占用/权限）⇒ 置 _load_failed，_save 整段拒写；
        #   · 文件不存在 = 首启，不是损坏 ⇒ 照常放行（★R231：别把首启
        #     变成永久拒写）。
        try:
            if self.history_path.exists():
                data = json.loads(self.history_path.read_text(encoding='utf-8'))
                if isinstance(data, list):
                    # ★R285-N10：顶层是列表**不等于**每条都能用。旧写法照单全收，
                    # 半写坏/手改过的档里混进字符串或数字，下游 grouped()/去重/
                    # download_panel 多处裸 `r.get` 就抛 AttributeError——表现是
                    # 下载卡渲染失败，更狠的是"导入全部数据"被外层 except 触发
                    # **整份回滚**（一份好备份因此全数作废）。口径同导入路
                    # gui/data_manage_page.py 的 `isinstance(_r, dict)`：加载端
                    # 逐条过滤，脏元素丢掉并留一行痕。
                    clean = [r for r in data if isinstance(r, dict)]
                    dropped = len(data) - len(clean)
                    if dropped:
                        logger.warning('[下载记忆] download_history.json 里有 %d 条'
                                       '不是对象（文件被手改或半写坏），已丢弃这 %d 条、'
                                       '保留其余 %d 条' % (dropped, dropped, len(clean)))
                        self.log('⚠ 下载记录文件里有 %d 条格式不对（可能被手改/半写坏），'
                                 '已跳过这 %d 条，其余 %d 条照常载入'
                                 % (dropped, dropped, len(clean)))
                    self.records = clean
                else:
                    self._corrupt_keep_and_gate(
                        ValueError('顶层不是列表，是 %s' % type(data).__name__))
        except json.JSONDecodeError as e:
            self._corrupt_keep_and_gate(e)
        except Exception as e:
            # ★R264：非解析类读失败（权限/占用/编码）与损坏同权处理
            # （口径同 ★R207 CORE-16 收尾——原分支只记一行就落下空账，
            # 照样会被下一次写盘顶掉用户数据）
            self._corrupt_keep_and_gate(e)

    def _corrupt_keep_and_gate(self, e):
        """损坏原件先另存留底，留不住底就立禁写闸。

        留底走 utils.safeio.set_aside_corrupt（带时间戳唯一名）——固定名
        （.bad/.bak）会把上一份唯一留底覆盖掉，★R204 CORE-31 同款坑。
        """
        from utils.safeio import set_aside_corrupt
        kept, detail = set_aside_corrupt(self.history_path)
        self.records = []
        if not kept:
            self._load_failed = True
        logger.error('[下载记忆] download_history.json 损坏，原件留底%s: %s'
                     % ('成功' if kept else '同样失败（可能被占用）', e))
        # 注意：本类没有 _log 方法，日志出口是 self.log（=_safe_log，
        # 内部已带兜底 try），别写成 self._log ⇒ 被 except 吞成静默。
        if kept and detail:
            self.log('⚠ 下载记录文件损坏，原件已另存留底（%s），本次从空账开始'
                     '（可人工找回旧记录）' % os.path.basename(detail))
        elif kept:
            self.log('⚠ 下载记录文件损坏，原件已不在原位（可能被移走），本次从空账开始')
        else:
            self.log('⚠ 下载记录文件损坏，且留底失败（%s 仍含原数据，可能被占用）：'
                     '本次会话不再写盘，重启后会自动重试' % self.history_path.name)

    def _save(self):
        # ★R264 Z5：闸门必须独立成立——原件读不动又留不住底的会话里，
        # 任何一次写盘都会用空账顶掉用户数据（★R231 同款口径）
        if getattr(self, '_load_failed', False):
            logger.warning('[下载记忆] 本次未写盘：原件读不动且留底失败，'
                           '继续写会用空账覆盖用户数据')
            return False
        try:
            self.records = self.records[:MAX_RECORDS]
            # ★R264：atomic_write_json 失败只返回 False 不抛，原写法丢弃
            # 返回值 ⇒ 写盘失败仍报"保存成功"（★R213 B11 同款口径）
            return bool(atomic_write_json(self.history_path, self.records))
        except Exception as e:
            logger.warning(f"[下载记忆] 记录写盘失败: {e}")
            return False

    # ---- 记录操作 ----

    # ---- 忽略规则（R160）：data/download_ignore.json = [路径/文件名子串]
    def _ignore_path(self):
        return Path(self.history_path).with_name('download_ignore.json')

    def _load_ignore(self):
        # ★R285-N6：与黑名单/撤回栈同一把闸（留底 + 禁写），读坏了不再静默回空。
        # 拿到手仍按旧口径过滤成"非空字符串名单"（脏元素不进内存，也就不会被写回盘）。
        raw = self._load_list_state(
            self._ignore_path(), '_ignore_load_failed', '忽略名单', as_set=False)
        return [s for s in raw if isinstance(s, str) and s.strip()]

    def list_ignore(self):
        return list(self._ignore)

    # ---- ★R229（用户 01 批 item1）：忽略规则/黑名单纳入"导出全部数据" ----
    # 换机导入后，这两份记忆若不在包里，被"忽略此条"过的下载会被浏览器
    # 历史/Recent 同步原样复活（R177 实锤），用户看到的就是"忽略了又出现"。
    def list_forgotten(self):
        return sorted(self._forgotten)

    def merge_ignore(self, patterns):
        """并入一组忽略规则（大小写不敏感去重），返回实际新增条数。"""
        added = 0
        have = {x.lower() for x in self._ignore}
        for p in (patterns or []):
            s = str(p).strip()
            if s and s.lower() not in have:
                self._ignore.append(s)
                have.add(s.lower())
                added += 1
        if added:
            self._save_ignore()
        return added

    def merge_forgotten(self, paths):
        """并入一组"被忽略条目"黑名单，返回实际新增条数。"""
        before = len(self._forgotten)
        for p in (paths or []):
            s = str(p).strip().lower()
            if s:
                self._forgotten.add(s)
        added = len(self._forgotten) - before
        if added:
            self._save_forgotten()
        return added

    def add_ignore(self, pattern):
        s = str(pattern).strip()
        if s and s.lower() not in (x.lower() for x in self._ignore):
            self._ignore.append(s)
            self._save_ignore()
            logger.info(f'[下载记忆] 已加忽略规则: {s}')

    def remove_ignore(self, pattern):
        self._ignore = [x for x in self._ignore if x != pattern]
        self._save_ignore()

    def _save_ignore(self):
        from utils.safeio import atomic_write_json
        # ★R285-N6：闸门必须独立成立（★R231 口径）——原件读不动又留不住底的
        # 会话里，任何一次写盘都是用空/短名单顶掉用户那份坏档（里面还有可人工
        # 抢救的规则）。口径同本模块 _save_forgotten/_save_undo（★R283）。
        if getattr(self, '_ignore_load_failed', False):
            logger.warning('[下载记忆] 忽略名单本次未写盘：原件读不动且留底失败，'
                           '继续写会用空表覆盖用户数据')
            self.log('⚠ 下载忽略名单未写盘（本机那份 download_ignore.json 读不动'
                     '且留底失败，已拒写以防覆盖）：本次改动重启后不生效')
            return False
        # ★R284-6（顺手）：忽略名单写失败不再静默（返回值必须消费）。
        ok = atomic_write_json(self._ignore_path(), self._ignore)
        if not ok:
            self.log('⚠ 下载忽略名单未能写入磁盘（文件可能被占用/只读），重启后忽略设置可能丢失')
        return bool(ok)

    def _is_ignored(self, p):
        s = str(p).lower()
        return any(x.lower() in s for x in self._ignore)

    def _is_forgotten(self, p):
        """★ R177：路径是否在"被忽略条目"黑名单（不区分大小写）"""
        s = str(p).lower()
        return s in self._forgotten

    def forget(self, path):
        """加入黑名单（忽略此条时调；同步层此后拒收该路径）"""
        self._forgotten.add(str(path).lower())
        self._save_forgotten()

    def unforgot(self, path):
        """撤回时解除黑名单"""
        self._forgotten.discard(str(path).lower())
        self._save_forgotten()

    def _load_list_state(self, path, gate_attr, label, as_set=False):
        """黑名单/撤回栈共用：读坏了先留底、留不住底立禁写闸（★R283，
        对齐同模块 _load/_corrupt_keep_and_gate 的 R264-Z5 双保护）。

        文件不存在=首启，不是损坏 ⇒ 照常放行（★R231：别把首启变永久拒写）。"""
        empty = set() if as_set else []
        try:
            if not path.exists():
                return empty
            data = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(data, list):
                return set(data) if as_set else data
            raise ValueError('顶层不是列表，是 %s' % type(data).__name__)
        except Exception as e:
            from utils.safeio import set_aside_corrupt
            kept, detail = set_aside_corrupt(path)
            if not kept:
                setattr(self, gate_attr, True)
            logger.error('[下载记忆] %s 文件损坏/读不动，原件留底%s: %s'
                         % (label, '成功' if kept else '同样失败（可能被占用）', e))
            if kept and detail:
                self.log('⚠ 下载%s文件损坏，原件已另存留底（%s），本次从空开始'
                         % (label, os.path.basename(detail)))
            elif kept:
                self.log('⚠ 下载%s文件损坏，原件已不在原位，本次从空开始' % label)
            else:
                self.log('⚠ 下载%s文件损坏且留底失败（%s 仍含原数据，可能被占用）：'
                         '本次会话不再写盘，重启后自动重试' % (label, path.name))
            return empty

    def _load_forgotten(self):
        return self._load_list_state(
            self.forgotten_path, '_forgotten_load_failed', '黑名单', as_set=True)

    def _save_forgotten(self):
        # ★R283：闸门独立成立——原件读不动又留不住底的会话里，任何一次写盘
        # 都会用空黑名单顶掉用户数据（★R231 同款）；改走原子写并消费返回值
        if getattr(self, '_forgotten_load_failed', False):
            logger.warning('[下载记忆] 黑名单本次未写盘：原件读不动且留底失败，'
                           '继续写会用空集合覆盖用户数据')
            return False
        try:
            self.forgotten_path.parent.mkdir(parents=True, exist_ok=True)
            return bool(atomic_write_json(
                self.forgotten_path, sorted(self._forgotten)))
        except Exception as e:
            logger.warning(f"[下载记忆] 黑名单写盘失败: {e}")
            return False

    def add_or_update(self, path, ts=None, size=None, src=None):
        """记录一个落定的下载文件（同路径去重，新的在前）"""
        try:
            p = Path(path)
            if p.suffix.lower() in TEMP_SUFFIXES or p.name.lower() in EXCLUDE_NAMES:
                return False
            # ★ R177 黑名单：被"忽略此条"过的路径，同步层永远拒收
            if self._is_forgotten(p):
                return False
            # ★ R160 忽略规则：手动添加（src='manual'）不受限，其余命中即拒
            if src != 'manual' and self._is_ignored(p):
                logger.debug(f"[下载记忆] 命中忽略规则，跳过: {p}")
                return False
            # ★ R120 水位线：清空之前的旧下载不再入账（浏览器历史/Recent
            # 再同步会原样"复活"旧记录——实测）
            ts_dt = ts if isinstance(ts, datetime) else None
            if ts_dt is None:
                try:
                    ts_dt = datetime.fromisoformat(str(ts))
                except Exception:
                    ts_dt = datetime.now()
            if self._clear_mark and ts_dt <= self._clear_mark:
                return False
            if not p.is_file():
                return False   # 文件已不在则不记（记录无法打开）
            rec = {
                'name': p.name,
                'path': str(p),
                'dir': str(p.parent),
                'size': size if size is not None else p.stat().st_size,
                # ★R229（本批 item1 回归发现）：原来写 `(ts or datetime.now())
                # .isoformat(...)`——ts 按签名允许传 ISO **字符串**（水位线那段
                # 就是为此才 fromisoformat 解析），字符串没有 .isoformat，
                # 直接 AttributeError 被外层 except 吞掉 ⇒ 该条记录**静默不入账**
                'ts': ts_dt.isoformat(timespec='seconds'),
                'status': 'done',
                'src': src or '',
            }
            with self._lock:
                self.records = [r for r in self.records
                                if r.get('path') != rec['path']]
                self.records.insert(0, rec)
                self.records = self.records[:MAX_RECORDS]
                self._save()
            return True
        except Exception as e:
            logger.warning(f"[下载记忆] 记录失败 {path}: {e}")
            return False

    def remove(self, path):
        with self._lock:
            self.records = [r for r in self.records if r.get('path') != str(path)]
            self._save()

    # ---- 撤回栈（★ R175：用户 -20/5-1"撤回忽略不起效果"——原内存栈
    # 重启即丢，且恢复走 add_or_update 会被水位线/文件不存在拦截）----

    def _load_undo(self):
        return self._load_list_state(
            self.undo_path, '_undo_load_failed', '撤回栈', as_set=False)

    def _save_undo(self):
        # ★R283：同黑名单——读坏留不住的会话拒写，改原子写并消费返回值
        if getattr(self, '_undo_load_failed', False):
            logger.warning('[下载记忆] 撤回栈本次未写盘：原件读不动且留底失败，'
                           '继续写会用空栈覆盖用户数据')
            return False
        try:
            self.undo_path.parent.mkdir(parents=True, exist_ok=True)
            return bool(atomic_write_json(self.undo_path, self._undo))
        except Exception as e:
            logger.warning(f"[下载记忆] 撤回栈写盘失败: {e}")
            return False

    def push_undo(self, rec):
        """忽略此条时入栈（封顶 50 条，新的在前，持久化）"""
        self._undo.insert(0, dict(rec))
        self._undo = self._undo[:50]
        self._save_undo()

    def pop_undo(self):
        if not self._undo:
            return None
        rec = self._undo.pop(0)
        self._save_undo()
        return rec

    def restore_record(self, rec):
        """撤回恢复：直接回插记录（与导入同口径——绕过水位线/忽略规则/
        存在性检查，用户明确"忽略的那条要找回来"）；★ R177 同时解除
        黑名单，否则浏览器同步删了又回来打擂台"""
        r = dict(rec)
        self.unforgot(r.get('path', ''))
        with self._lock:
            self.records = [x for x in self.records
                            if x.get('path') != r.get('path')]
            self.records.insert(0, r)
            self.records = self.records[:MAX_RECORDS]
            self._save()
        return True

    def _wipe_file(self, path, gate_attr=None):
        """隐私清空的动作语义就是**删掉这份文件**，不是"往它里面写空表"。

        · 文件本来就不存在 ⇒ 算清干净（首启/已被删）；
        · 删不动（被占用/权限/是个目录）⇒ 记 logger 并返回 False，由调用方如实报。
        删成功时顺手放开该那份的禁写闸：原件已经不在了，后续再写不存在"覆盖用户数据"
        这回事（★R231 的闸门是为了保护盘上那份，不是永久拒写）。
        """
        try:
            os.remove(str(path))
            if gate_attr:
                setattr(self, gate_attr, False)
            return True
        except FileNotFoundError:
            if gate_attr:
                setattr(self, gate_attr, False)
            return True
        except OSError as e:
            logger.warning('[下载记忆] 清空个人数据时删不掉 %s: %s'
                           % (Path(path).name, e))
            return False

    def clear(self):
        """清空下载侧的全部个人痕迹，返回**没清掉的文件名列表**（★R285-N9）。

        ★R229 的隐私承诺是"分发前必须把个人绝对路径清掉"，而这四份文件
        （history/ignore/forgotten/undo）里都含绝对路径。旧写法对后三份走
        `_save_*`（写空表），闸门态下它直接拒写 ⇒ 文件原样留盘、完成弹窗却
        照旧称"可以打包分发了"。现在四份统一走删除，删不掉就一条条回报给界面。
        """
        left = []
        with self._lock:
            self.records = []
            if not self._wipe_file(self.history_path, '_load_failed'):
                left.append(self.history_path.name)
        # ★ R229（用户 01 批 item1 / 隐私收口）：忽略规则、被忽略条目
        # 黑名单、撤回栈里同样带着个人绝对路径（download_ignore.json /
        # download_forgotten.json / download_undo.json），"分发前清空个
        # 人数据"若只清记录，这三份文件仍留在 data/ 里 —— 一并清零。
        # 安全性：旧下载不会因黑名单清空而复活，下面 R120 水位线本来就
        # 会拒收 ts <= 清空时刻 的同步记录（add_or_update 里判）。
        self._ignore = []
        if not self._wipe_file(self._ignore_path(), '_ignore_load_failed'):
            left.append(self._ignore_path().name)
        self._forgotten = set()
        if not self._wipe_file(self.forgotten_path, '_forgotten_load_failed'):
            left.append(self.forgotten_path.name)
        self._undo = []
        if not self._wipe_file(self.undo_path, '_undo_load_failed'):
            left.append(self.undo_path.name)
        for n in left:
            self.log('⚠ 清空个人数据：%s 没能删掉（文件可能被占用）——里面可能仍含'
                     '个人绝对路径，分发前请手工删除' % n)
        # ★ R120：记录清空水位线（持久化——重启后同步仍遵守）
        self._clear_mark = datetime.now()
        try:
            self.clear_mark_path.parent.mkdir(parents=True, exist_ok=True)
            # ★R316（顺手，P3）：换原子写。裸 write_text 是"先截断再写"，写一半被杀
            #   就留下半截 ISO 串 ⇒ _load_clear_mark 的 fromisoformat 抛 ⇒ 水位线静默
            #   失效＝刚清空的旧下载重启后被同步"复活"（正是这条线要防的事）。
            #   格式仍是纯文本 ISO，不能套 JSON（那边读 read_text().strip()）。
            _mark_ok = atomic_write_text(self.clear_mark_path,
                                         self._clear_mark.isoformat())
        except Exception as e:
            _mark_ok = False
            logger.debug(f"[下载记忆] 水位线目录准备异常: {e}")
        if not _mark_ok:
            logger.warning("[下载记忆] 水位线写盘失败：本次清空只在这一程内存里生效，"
                           "重启后早于此刻的旧下载可能被同步复活（请重试清空）")
        return left

    def clear_data_file(self):
        """清空全部个人数据用：记录清零 + 四份文件删除。

        ★R285-N9：返回没清掉的文件名列表，调用方（gui/data_manage_page 的完成
        弹窗）据此如实报——不能再无条件说"可以打包分发了"。"""
        return self.clear()

    @staticmethod
    def group_of(ts_iso, now=None):
        """时间 → 今天/昨天/本周/更早（验收：跨零点正确）"""
        try:
            t = datetime.fromisoformat(ts_iso)
        except Exception:
            return '更早'
        now = now or datetime.now()
        day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if t >= day0:
            return '今天'
        if t >= day0 - timedelta(days=1):
            return '昨天'
        if t >= day0 - timedelta(days=6):
            return '本周'
        return '更早'

    def grouped(self):
        """[(组名, [记录...])]，组按 今天→更早，组内新的在前"""
        order = ['今天', '昨天', '本周', '更早']
        groups = {k: [] for k in order}
        for r in self.records:
            groups.setdefault(self.group_of(r.get('ts', '')), []).append(r)
        return [(k, groups[k]) for k in order if groups[k]]

    # ---- Recent .lnk 回填（只读） ----

    def scan_recent_lnk(self):
        """解析 shell:Recent 的 .lnk 目标，落在监听目录内的记入历史"""
        try:
            recent = (Path(os.environ.get('APPDATA', '')) /
                      'Microsoft' / 'Windows' / 'Recent')
            if not recent.is_dir():
                return 0
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()
            try:
                shell = win32com.client.Dispatch('WScript.Shell')
                now = time.time()
                n = 0
                cutoff = now - RECENT_BACKFILL_DAYS * 86400
                for lnk in sorted(recent.glob('*.lnk'),
                                  key=lambda p: p.stat().st_mtime, reverse=True):
                    try:
                        if lnk.stat().st_mtime < cutoff:
                            break   # 按新→旧排序，老的直接停
                        target = shell.CreateShortCut(str(lnk)).Targetpath
                        if not target:
                            continue
                        tp = Path(target)
                        if any(tp.parent == Path(d) for d in self.watch_dirs):
                            if self.add_or_update(
                                    tp, ts=datetime.fromtimestamp(lnk.stat().st_mtime)):
                                n += 1
                    except Exception:
                        continue
                if n:
                    self.log(f'📥 已从"最近使用"回填 {n} 条下载记录')
                return n
            finally:
                pythoncom.CoUninitialize()
        except Exception as e:
            logger.warning(f"[下载记忆] Recent 回填失败（不影响主功能）: {e}")
            return 0

    # ---- 浏览器下载历史（R115：浏览器下载到任意路径都要记录——
    #      目录监听只覆盖 Downloads/桌面，这里补盲区；只读拷贝防锁库）----

    @staticmethod
    def _browser_history_paths():
        """[(来源名, History 库路径)]：Chrome 与 Edge 的 Default 配置"""
        local = os.environ.get('LOCALAPPDATA', '')
        out = []
        if local:
            base = Path(local)
            out.append(('chrome', base / 'Google' / 'Chrome' / 'User Data'
                        / 'Default' / 'History'))
            out.append(('edge', base / 'Microsoft' / 'Edge' / 'User Data'
                        / 'Default' / 'History'))
        return out

    def scan_browser_history(self, days=30):
        """读 Chrome/Edge downloads 表（已完成、近 N 天），合并进记录"""
        import shutil as _sh
        import sqlite3 as _sq
        import tempfile as _tf
        n = 0
        epoch0 = datetime(1601, 1, 1)
        cutoff = datetime.now() - timedelta(days=days)
        for src, lib in self._browser_history_paths():
            if not lib.is_file():
                continue
            tmpd = Path(_tf.mkdtemp(prefix='dlhist_'))
            try:
                dst = tmpd / 'History'
                _sh.copy2(lib, dst)   # 只读拷贝：浏览器运行时原库被锁
                con = _sq.connect(str(dst))
                try:
                    cur = con.execute(
                        "SELECT target_path, total_bytes, start_time "
                        "FROM downloads WHERE state=1 "
                        "ORDER BY start_time DESC LIMIT 300")
                    for tpath, tbytes, stime in cur:
                        if not tpath:
                            continue
                        try:
                            dt = epoch0 + timedelta(microseconds=int(stime))
                        except Exception:
                            continue
                        if dt < cutoff:
                            continue
                        if self.add_or_update(tpath, ts=dt,
                                              size=tbytes, src=src):
                            n += 1
                finally:
                    con.close()
            except Exception as e:
                logger.warning(f"[下载记忆] 浏览器历史读取失败 {src}: {e}")
            finally:
                _sh.rmtree(tmpd, ignore_errors=True)
        if n:
            self.log(f'📥 已从浏览器下载历史同步 {n} 条记录')
        return n

    # ---- 目录监听（daemon 线程） ----

    def _watch_loop(self, directory):
        try:
            import win32con
            import win32file
            h = win32file.CreateFile(str(directory), win32con.GENERIC_READ,
                                     win32con.FILE_SHARE_READ |
                                     win32con.FILE_SHARE_WRITE |
                                     win32con.FILE_SHARE_DELETE,
                                     None, win32con.OPEN_EXISTING,
                                     win32con.FILE_FLAG_BACKUP_SEMANTICS, None)
        except Exception as e:
            logger.warning(f"[下载记忆] 监听打不开 {directory}: {e}")
            return
        logger.info(f"[下载记忆] 监听线程就绪: {directory}")
        try:
            flags = (win32con.FILE_NOTIFY_CHANGE_FILE_NAME |
                     win32con.FILE_NOTIFY_CHANGE_LAST_WRITE)
            while not self._stop.is_set():
                try:
                    # pywin32 阻塞式四参调用：返回 [(action, 文件名), ...]
                    # ★ R130：缓冲 8KB→64KB，下载突发大批事件不易溢出
                    events = win32file.ReadDirectoryChangesW(
                        h, 65536, False, flags)
                except Exception as e:
                    # ★ R130：缓冲溢出（ERROR_NOTIFY_ENUM_DIR=1022）只丢当批
                    #   事件——继续循环重新挂读，不再永久退出监听线程
                    if getattr(e, 'winerror', None) == 1022:
                        logger.warning(
                            f"[下载记忆] 事件缓冲溢出（下载太猛），"
                            f"丢弃当批继续监听: {directory}")
                        continue
                    logger.warning(f"[下载记忆] 监听退出 {directory}: {e}")
                    break   # 目录没了/句柄失效：静默降级（仅丢实时记录）
                for action, name in events:
                    if self._stop.is_set():
                        break
                    # ★R204 CORE-34：原始目录事件降 debug——每事件一条 INFO
                    # 配合 sleep(0.6) 拉长处理窗口、放大缓冲溢出概率；
                    # 入账事件另有 INFO 留痕
                    logger.debug(f"[下载记忆] 事件 action={action} name={name}")
                    # 1=ADDED 5=RENAMED_NEW_NAME：新文件落定才算下载；
                    # 3=MODIFIED（改内容/续写）不重录，2/4=删除与旧名忽略
                    if action not in (1, 5):
                        continue
                    p = Path(directory) / str(name)
                    if p.suffix.lower() in TEMP_SUFFIXES:
                        continue   # 半成品：落定改名事件会带最终名来
                    time.sleep(0.6)   # 等写入收尾
                    self.add_or_update(p)
        finally:
            try:
                win32file.CloseHandle(h)
            except Exception:
                pass

    def start(self):
        # 监听线程先起（实时事件一个不漏）；Recent 回填放后台（解析
        # 上百个 lnk 慢，曾把监听线程启动拖到事件错过之后——沙箱实测）
        for d in self.watch_dirs:
            t = threading.Thread(target=self._watch_loop, args=(d,),
                                 daemon=True, name=f'DownloadWatch:{d}')
            t.start()
            self._threads.append(t)
        logger.info(f"[下载记忆] 监听 {len(self.watch_dirs)} 个目录 "
                    f"({', '.join(self.watch_dirs)})")
        def _backfill():
            # ★ R142：Recent .lnk 回填不再在后台线程做——WScript.Shell COM
            #   在线程里曾触发原生访问违例、整进程闪退（win11 长跑日志
            #   crash.log 实锤 faulthandler 栈=_backfill→scan_recent_lnk，
            #   AV 无法被 try 捕获）；改由 GUI 主线程 root.after 调度
            #   （STA 主线程 COM 稳定），本线程只管 sqlite（无 COM）。
            try:
                self.scan_browser_history()
            except Exception as e:
                logger.warning(f"[下载记忆] 浏览器历史同步失败: {e}")
        tb = threading.Thread(target=_backfill, daemon=True,
                              name='DownloadBackfill')
        tb.start()
        self._threads.append(tb)

    def stop(self):
        self._stop.set()
