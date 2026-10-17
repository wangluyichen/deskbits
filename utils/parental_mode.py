"""家长模式 - 孩子专用桌面 + 定时锁屏 + 密码保护

进入后：
- 横条只留 [小朋友] [全部] [⏱倒计时] [退出]，其他分类消失
- 桌面只显示"小朋友"分类的图标，其余移出屏幕
- 倒计时结束自动恢复原布局并锁屏（孩子学习时间到）
- 退出家长模式必须输入家长密码（加盐 SHA-256 存储，不存明文）
- 程序重启后若模式仍在生效期，自动恢复儿童模式（防止孩子绕过）
"""
import os
import json
import time
import hashlib
import secrets
import logging
import ctypes
from typing import Optional, Callable

from utils.safeio import atomic_write_json

logger = logging.getLogger("desktop_manager")


def _hash(salt: str, pwd: str) -> str:
    return hashlib.sha256((salt + pwd).encode('utf-8')).hexdigest()


class ParentalMode:
    """家长模式管理器"""

    def __init__(self, dock, icon_manager, category_manager, config_dir: str,
                 log: Optional[Callable[[str], None]] = None):
        self.dock = dock
        self.icon_manager = icon_manager
        self.category_manager = category_manager
        self.config_dir = config_dir
        self._log = log or (lambda m: logger.info(m))
        self.path = os.path.join(config_dir, 'parental.json')
        self._state = self._load()
        self._tick_id = None
        self.lock_cb = None   # ★ R160：自带锁屏回调（main_window 注入）
        self._last_time_text = None

        # gui 注入：孩子点横条 [退出] 时弹密码框
        self.on_exit_request = None
        # gui 注入：模式结束后恢复横条/主窗口
        self.on_kids_end = None

        # 横条挂上退出回调
        dock.on_kids_exit = self._on_dock_exit
        dock.kids_category = '小朋友'
        # 启动自检：若家长模式未激活而任务栏仍处于隐藏态（上次异常退出残留），恢复它
        if not self._state.get('kids'):
            self._show_taskbar()

    # ------------------------------------------------------------------
    # 状态持久化
    # ------------------------------------------------------------------

    def _load(self) -> dict:
        try:
            if os.path.exists(self.path):
                with open(self.path, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"加载家长模式配置失败: {e}")
        return {'salt': '', 'hash': '', 'kids': False, 'end_ts': 0}

    def _save(self):
        try:
            # ★R204 ROOT-2：返回值必须消费——家长模式状态写盘失败必须留痕
            if not atomic_write_json(self.path, self._state, indent=2):
                logger.error("家长模式配置写盘失败（状态仅存内存）")
        except Exception as e:
            logger.error(f"保存家长模式配置失败: {e}")

    # ------------------------------------------------------------------
    # 密码
    # ------------------------------------------------------------------

    def has_password(self) -> bool:
        return bool(self._state.get('hash'))

    def set_password(self, pwd: str):
        # ★ 复用已有 salt：密保答案哈希绑定在 salt 上，换 salt 会让
        #   密保找回永远验证失败（2026-09-05 审核发现）
        salt = self._state.get('salt') or secrets.token_hex(8)
        self._state['salt'] = salt
        self._state['hash'] = _hash(salt, pwd)
        self._save()
        self._log('🔐 家长密码已设置')

    def clear_password(self):
        """清除密码与密保（分发前清理/用户主动重置）。恢复默认为空状态"""
        self._state['hash'] = ''
        self._state.pop('questions', None)
        self._save()
        self._log('🧹 家长密码已清除（恢复默认为空）')

    def clear_questions(self):
        """只清除密保问题（保留密码）。
        分发前清理个人数据用。清除后无法再自助找回密码。"""
        if not self.has_questions():
            return
        self._state.pop('questions', None)
        self._save()
        self._log('🧹 密保问题已清除（密码保留）')

    def verify(self, pwd: str) -> bool:
        if not self.has_password():
            return True  # 未设密码不拦截（首次进入时会强制设置）
        return _hash(self._state.get('salt', ''), pwd) == self._state.get('hash')

    # ------------------------------------------------------------------
    # 密保问题（找回密码）
    # ------------------------------------------------------------------

    def has_questions(self) -> bool:
        return bool(self._state.get('questions'))

    def set_questions(self, qa: list):
        """保存密保问题。qa = [(问题, 答案), ...]，答案加盐哈希存储"""
        salt = self._state.get('salt') or secrets.token_hex(8)
        self._state['salt'] = salt
        self._state['questions'] = [
            {'q': q.strip(), 'h': _hash(salt, a.strip().lower())}
            for q, a in qa if q.strip() and a.strip()]
        self._save()
        self._log(f'🔐 密保问题已保存（{len(self._state["questions"])} 个）')

    def get_questions(self) -> list:
        return [item['q'] for item in self._state.get('questions', [])]

    def verify_answers(self, answers: list) -> bool:
        """校验密保答案（顺序对应 get_questions，忽略大小写与首尾空格）"""
        items = self._state.get('questions', [])
        if not items or len(answers) < len(items):
            return False
        salt = self._state.get('salt', '')
        for item, ans in zip(items, answers):
            if _hash(salt, (ans or '').strip().lower()) != item['h']:
                return False
        return True

    def exit_without_password(self):
        """密保验证通过后免密退出（仅供 _on_forgot_password 在
        新密码已重设成功后调用）。

        ★ 不清 hash —— hash 的替换由 set_password 完成，避免"退出后
        用户取消重设"造成密码永久为空。"""
        self._restore(lock_after=False)

    # ------------------------------------------------------------------
    # 进入 / 退出
    # ------------------------------------------------------------------

    def is_kids(self) -> bool:
        return bool(self._state.get('kids')) and time.time() < self._state.get('end_ts', 0)

    def enter(self, minutes: int, backup: bool = True):
        """进入家长模式（backup=False 用于重启后恢复，避免覆盖进入前的布局备份）"""
        # 记住进入前激活的分类槽位，退出时按它完整恢复（2026-09-05 用户要求
        # 退出回到进入前的样子，而不是"全部显示"导致图标乱）
        self._pre_kids_slot = getattr(self.dock, '_active_slot_name', None)
        if backup:
            try:
                self.icon_manager.save_layout('_家长模式前', source='auto')
            except Exception as e:
                logger.error(f"备份布局失败: {e}")

        if minutes and minutes > 0:
            self._state['end_ts'] = time.time() + minutes * 60
        # minutes<=0：沿用已保存的 end_ts（重启恢复场景）

        cat_name = getattr(self.dock, 'kids_category', '小朋友')
        # 分类不存在时自动创建，保证开箱即用
        cats = [c['name'] for c in self.category_manager.get_all_categories()]
        if cat_name not in cats:
            self.category_manager.add_category(cat_name, '#ff5555')
            self._log(f'📂 已自动创建分类 [{cat_name}]，请在主界面为孩子添加图标')
        cat_color = '#ff5555'
        try:
            cat_color = self.category_manager.get_category_color(cat_name)
        except Exception:
            pass

        self._state['kids'] = True
        self._save()
        # 防护：进入儿童桌面时图标大面积隐/现，压制守护避免误判
        try:
            self.icon_manager.suppress_guard(12.0)
        except Exception:
            pass

        self.dock.set_kids_slots(cat_name, cat_color)
        self._switch_to_kids()
        self._hide_taskbar()
        self._last_time_text = None
        self._start_tick()
        remain = self.remaining_seconds() // 60
        self._log(f'👶 家长模式已开启：只显示 [{cat_name}]，约 {remain} 分钟后自动锁屏')

    def _switch_to_kids(self):
        """桌面只显示小朋友分类的图标（与"点小朋友槽位"同一套布局逻辑）"""
        cat_name = getattr(self.dock, 'kids_category', '小朋友')
        cat_icons = self.category_manager.get_category_icons(cat_name)
        if not cat_icons:
            self._log(f'⚠ [{cat_name}] 分类还没有图标，请在主界面先添加')
            return
        # ★R305：走收纳档路由器——hide 档＝原地改文件属性（旧行为，一枚不动）；
        #   box 档＝别的全进「收纳·<小朋友>」盒子。组名与 dock 那侧同一个，
        #   所以儿童桌面和普通点槽位共用一只盒，不会各搬一份。
        from core import visibility_router
        # ★R321 H 组：开家长模式这一发同样是批量链（用户点的是"只给小朋友看这一组"，
        #   不是"倒空某只盒"）⇒ 散回不碰桌面上那一枚，留在盒里的几枚报进界面日志。
        r = visibility_router.apply_group_visibility(
            self.icon_manager, set(cat_icons), group=cat_name, source='par',
            apply_layout=getattr(self.icon_manager, 'apply_positions_settled', None),
            positions_provider=getattr(self.icon_manager,
                                       '_read_all_positions_live', None),
            log=self._log)
        if r.get('reason'):
            self._log('⚠ ' + str(r['reason']))
        if r.get('failed'):
            # ★R311 自纠：`failed` 里混着系统图标和"这一轮刚搬进盒的"（Explorer 的残影），
            # 历轮一律拿它的长度报"系统图标无法隐藏" ⇒ 儿童桌面同样报出一大串假数。
            # 跳过数读路由器现算的那份；hide 档没这个键时按旧算法回退（与悬浮条同一口径）。
            _skip = r.get('skipped_system')
            if _skip is None:
                _skip = max(0, len(r['failed']) - len(r.get('stuck') or []))
            if _skip:
                self._log(f"⚠ {_skip} 个系统图标无法隐藏，保持可见")
        # ★ 虚拟系统图标：与主窗口"公共分类显示"三列规则统一（2026-09-05
        #   用户要求不要固化）——系统图标常驻开 且 该分类不是"只显示自己+隐系统"
        #   → 显示；否则隐藏。小朋友当前勾的是③，所以儿童桌面不含系统图标。
        try:
            from core.icon_manager import VIRTUAL_DESKTOP_ICONS
            _mode = self.category_manager.get_display_mode(cat_name)
            _resident = getattr(self.dock, 'system_resident', True)
            if _resident and _mode != 'exclusive_sys':
                n = self.icon_manager.show_virtual_icons(
                    set(VIRTUAL_DESKTOP_ICONS.keys()))
                if n:
                    self._log(f'🖥 系统图标常驻开，已显示 {n} 个系统图标')
            else:
                n = self.icon_manager.hide_virtual_icons(
                    set(VIRTUAL_DESKTOP_ICONS.keys()))
                if n:
                    self._log(f'🖥 已隐藏 {n} 个系统图标'
                              f'（[{cat_name}]=只显示自己+隐系统）')
        except Exception as e:
            logger.error(f"系统图标显隐失败: {e}")
        # F5 冲刷：让注册表隐藏立即生效（实测 0.05s 更新）
        try:
            self.dock._force_listview_refresh()
        except Exception:
            pass
        # 布局：与"点小朋友槽位"一致——套用 dock 的分类布局逻辑
        # （有同名布局用同名，否则回退"全部"布局排位置）
        try:
            self.dock._apply_category_layout(cat_name)
        except Exception as e:
            logger.error(f"应用儿童布局失败: {e}")
        # 等儿童图标全部重现，再按记忆位校准（位置与"点小朋友"一致）
        try:
            self.icon_manager.wait_for_stable(len(cat_icons),
                                              max_retries=10, interval=0.3)
            self.icon_manager.realign_icons(list(cat_icons))
        except Exception as e:
            logger.error(f"儿童图标位置校准失败(不影响): {e}")
        if hasattr(self.dock, '_fire_visibility_changed'):
            self.dock._fire_visibility_changed()

    def _on_dock_exit(self):
        """孩子点了横条 [退出]"""
        if self.on_exit_request:
            try:
                self.on_exit_request()
            except Exception as e:
                logger.error(f"退出回调异常: {e}")

    def try_exit(self, password: str) -> bool:
        """用密码退出家长模式"""
        if not self.verify(password or ''):
            return False
        self._restore(lock_after=False)
        return True

    def _restore(self, lock_after: bool):
        """恢复进入前的布局与横条"""
        self._stop_tick()
        # ★ 先压制布局守护 12 秒：unhide/reproduce 期间图标大面积跳变，
        #   守护会把过渡期位置当"用户整理完成"合并进基准布局——
        #   2026-09-05 18:02 用户手动备份被污染的真正漏洞
        try:
            self.icon_manager.suppress_guard(12.0)
        except Exception:
            pass
        try:
            # ★R305 真机实撞（P1）同款缺口：进入家长模式走的是收纳档路由器，
            #   box 档下"别针图标全进「收纳·小朋友」盒"，而退出只会清属性 ⇒ 退出来
            #   一看桌面只剩小朋友那几枚。所以退出的第一步是先把本系统的盒散回来。
            from core import visibility_router as _vr
            _rr = _vr.release_all_boxes(self.icon_manager, source='par-exit')
            if _rr.get('reason'):
                self._log('⚠ ' + str(_rr['reason']))
            if _rr.get('total'):
                self._log(f'📦 退出家长模式：先从盒里搬回 {_rr["total"]} 枚，'
                          f'再恢复其余图标')
        except Exception as e:
            logger.error(f"退出家长模式散回盒子失败（属性法照走）: {e}")
        try:
            self.icon_manager.unhide_all()
        except Exception as e:
            logger.error(f"恢复可见性失败: {e}")
        # F5 冲刷 + 等全部图标重现（实测 84 个到位后再切分类才不会乱）
        self._refresh_desktop()
        self._state['kids'] = False
        self._save()
        self._show_taskbar()
        if hasattr(self.dock, '_fire_visibility_changed'):
            self.dock._fire_visibility_changed()
        if self.on_kids_end:
            try:
                self.on_kids_end()
            except Exception as e:
                logger.error(f"恢复回调异常: {e}")
        # ★ 恢复进入前激活的分类：走完整正常切换（可见性+虚拟图标+布局+校准），
        #   桌面回到进入家长模式前的样子（2026-09-05 用户实测"退出后图标乱"）
        pre = getattr(self, '_pre_kids_slot', None)
        idx = next((i for i, s in enumerate(getattr(self.dock, 'slots', []))
                    if s.name == pre), None) if pre else None
        try:
            if idx is not None and pre != '全部':
                # 恢复分类属程序内部行为：临时屏蔽"切分类联动开文件夹组"
                _cb = getattr(self.dock, 'on_switch_link_folders', None)
                self.dock.on_switch_link_folders = None
                try:
                    self.dock._do_activate_slot(idx)
                finally:
                    self.dock.on_switch_link_folders = _cb
                self._log(f'📂 已恢复家长模式前分类 [{pre}]')
            elif pre == '全部':
                self.dock._show_all_icons()
                try:
                    self.dock._apply_category_layout('全部')
                except Exception:
                    pass
                names = list(self.icon_manager._get_current_icon_map().keys())
                self.icon_manager.realign_icons(names)
                self._log('📂 已恢复家长模式前的[全部]桌面')
            else:
                self._restore_from_backup()
        except Exception as e:
            logger.error(f"恢复进入前分类失败，改用布局备份兜底: {e}")
            self._restore_from_backup()
        self._log('👨‍👩‍👧 家长模式已退出，桌面已恢复')
        if lock_after:
            # ★ R160：改走自带锁屏画面（lock_cb 由 main_window 注入），
            # 回调缺失/异常时回退系统锁屏
            try:
                if self.lock_cb:
                    self.lock_cb()
                else:
                    ctypes.windll.user32.LockWorkStation()
            except Exception as e:
                logger.error(f"锁屏失败: {e}")

    def _restore_from_backup(self):
        """兜底：按 _家长模式前 布局备份恢复 + 位置校准"""
        try:
            layouts = self.icon_manager.list_layouts(include_internal=True)
        except Exception as e:
            logger.error(f"兜底恢复: 读取布局列表失败: {e}")
            return
        snap = next((l for l in layouts if l['name'] == '_家长模式前'), None)
        if not snap:
            return
        self.icon_manager.restore_layout(snap['path'])
        time.sleep(0.8)
        self.icon_manager.restore_layout(snap['path'])
        try:
            names = list(self.icon_manager._get_current_icon_map().keys())
            self.icon_manager.realign_icons(names)
        except Exception as e:
            logger.error(f"兜底位置校准失败: {e}")

    def _refresh_desktop(self):
        """F5 冲刷桌面并等条目数稳定（连续 3 次读数不变 = 图标全部重现）。
        注意不能用 _name_to_path_map 当预期数——它把 desktop.ini 等
        非图标文件也算进去，永远等不到位白等 6 秒。"""
        try:
            self.dock._force_listview_refresh()
        except Exception:
            pass
        try:
            last, same = -1, 0
            for _ in range(20):
                n = self.icon_manager.get_icon_count()
                same = same + 1 if (n == last and n > 0) else 0
                last = n
                if same >= 3:
                    break
                time.sleep(0.3)
            logger.info(f"[家长模式退出] 桌面条目稳定: {last} 个")
        except Exception as e:
            logger.error(f"等待图标重现失败(继续恢复): {e}")

    def _hide_taskbar(self):
        """家长模式隐藏任务栏（防止孩子从任务栏开游戏）

        ★ P1-C 同时隐藏 Shell_TrayWnd（主屏）和所有 Shell_SecondaryTrayWnd（副屏）。
        Windows 11 多显示器副屏各自有独立 Shell_SecondaryTrayWnd 窗口类，
        只隐藏主屏会被副屏任务栏绕过。"""
        try:
            import win32gui, win32con
            # 主屏任务栏
            hwnd = win32gui.FindWindow('Shell_TrayWnd', None)
            if hwnd:
                win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
            # 所有副屏任务栏（可能多个，需枚举）
            hidden_count = 0
            h = win32gui.FindWindow('Shell_SecondaryTrayWnd', None)
            while h:
                win32gui.ShowWindow(h, win32con.SW_HIDE)
                hidden_count += 1
                h = win32gui.FindWindowEx(0, h, 'Shell_SecondaryTrayWnd', None)
            logger.info(f"任务栏已隐藏（家长模式）: 主屏 + {hidden_count} 副屏")
        except Exception as e:
            logger.error(f"隐藏任务栏失败: {e}")

    def _show_taskbar(self):
        """退出家长模式恢复任务栏；启动自检时也调用，防止异常残留

        ★ P1-C 同时恢复主屏和所有副屏 Shell_SecondaryTrayWnd"""
        try:
            import win32gui, win32con
            hwnd = win32gui.FindWindow('Shell_TrayWnd', None)
            if hwnd and not win32gui.IsWindowVisible(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
            # 恢复所有副屏
            shown_count = 0
            h = win32gui.FindWindow('Shell_SecondaryTrayWnd', None)
            while h:
                if not win32gui.IsWindowVisible(h):
                    win32gui.ShowWindow(h, win32con.SW_SHOW)
                    shown_count += 1
                h = win32gui.FindWindowEx(0, h, 'Shell_SecondaryTrayWnd', None)
            if shown_count or (hwnd and not win32gui.IsWindowVisible(hwnd)):
                logger.info(f"任务栏已恢复: {shown_count} 副屏")
        except Exception as e:
            logger.error(f"恢复任务栏失败: {e}")

    # ------------------------------------------------------------------
    # 倒计时
    # ------------------------------------------------------------------

    def remaining_seconds(self) -> int:
        return max(0, int(self._state.get('end_ts', 0) - time.time()))

    def _start_tick(self):
        self._stop_tick()
        self._tick()

    def _stop_tick(self):
        if self._tick_id is not None and self.dock.tk_root is not None:
            try:
                self.dock.tk_root.after_cancel(self._tick_id)
            except Exception:
                pass
        self._tick_id = None

    def _tick(self):
        """每 5 秒检查剩余时间，更新横条倒计时（异常绝不能带崩主循环）"""
        self._tick_id = None
        try:
            if not self._state.get('kids'):
                return
            remain = self.remaining_seconds()
            if remain <= 0:
                self._log('⏰ 学习时间到，正在恢复桌面并锁屏…')
                self._restore(lock_after=True)
                return
            mins = remain // 60
            text = f'⏱{mins}分钟' if mins >= 1 else '⏱<1分钟'
            if text != self._last_time_text:
                self._last_time_text = text
                self.dock.update_kids_time(text)
        except Exception as e:
            logger.error(f"家长模式计时异常(已忽略): {e}")
        if self.dock.tk_root is not None:
            self._tick_id = self.dock.tk_root.after(5000, self._tick)

    def resume_if_active(self) -> bool:
        """程序启动时：若模式仍在生效期则自动恢复（防绕过）"""
        if not self._state.get('kids'):
            return False
        if time.time() >= self._state.get('end_ts', 0):
            # 已过期（程序关闭期间到时）：恢复正常模式，桌面由家长手动恢复
            self._state['kids'] = False
            # ★R313 G1（P1）：这一路专门接"计时内程序被杀/崩溃"的残留——进入模式时
            #   `_hide_taskbar()` 已把主屏 Shell_TrayWnd 和所有副屏任务栏 SW_HIDE，
            #   而 `__init__` 里那句"残留自愈"只在 `kids` 为假时才跑（本例构造时还是
            #   True ⇒ 不走）。旧写法只改状态就 return ⇒ 任务栏**永久不可见**，
            #   孩子和家长都救不回来（只能重启 explorer）。桌面按原设计留给家长手动
            #   复原，任务栏是系统级的东西，必须在这里就恢复。
            self._show_taskbar()
            logger.info('[家长模式] 计时在程序关闭期间到期 ⇒ 任务栏已恢复'
                        '（桌面图标按原设计留给家长手动复原）')
            self._save()
            return False
        self.enter(0, backup=False)  # enter 内部用 end_ts，传入分钟仅用于日志
        return True
