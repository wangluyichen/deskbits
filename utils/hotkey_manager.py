"""全局快捷键模块 - 独立消息循环线程 + 主线程回调队列

- 默认方案参考主流习惯：Ctrl+Alt+数字 切换分类（避开 Win 系、浏览器 Ctrl+数字、微信/QQ 的 Ctrl+Alt+W/A）
- 支持自定义（Ctrl/Alt/Shift/Win + 字母/数字/F1-F12/方向键等）
- 内置冲突检测：注册失败说明被其他程序占用；高风险组合给出提醒
"""
import ctypes
import threading
import queue
import logging
from ctypes import wintypes
from typing import Dict, List, Optional, Tuple, Callable

logger = logging.getLogger("desktop_manager")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

MOD_NAMES = [('Ctrl', MOD_CONTROL), ('Alt', MOD_ALT), ('Shift', MOD_SHIFT), ('Win', MOD_WIN)]

# ---------------------------------------------------------------------------
# 键名 <-> VK 码
# ---------------------------------------------------------------------------

SPECIAL_KEYS = {
    'f1': 0x70, 'f2': 0x71, 'f3': 0x72, 'f4': 0x73, 'f5': 0x74, 'f6': 0x75,
    'f7': 0x76, 'f8': 0x77, 'f9': 0x78, 'f10': 0x79, 'f11': 0x7A, 'f12': 0x7B,
    'up': 0x26, 'down': 0x28, 'left': 0x25, 'right': 0x27,
    'home': 0x24, 'end': 0x23, 'prior': 0x21, 'next': 0x22,   # PgUp/PgDn
    'insert': 0x2D, 'delete': 0x2E, 'space': 0x20,
    'tab': 0x09, 'escape': 0x1B,   # ★ R142 报告#1：缺了会令 tab/escape 的冲突检查成死代码，且配置 Alt+Tab 等时 vk=0 注册必败、误报"被占用"
    'comma': 0xBC, 'period': 0xBE, 'slash': 0xBF, 'semicolon': 0xBA,
    'apostrophe': 0xDE, 'bracketleft': 0xDB, 'bracketright': 0xDD,
    'backslash': 0xDC, 'minus': 0xBD, 'equal': 0xBB, 'grave': 0xC0,
}
VK_NAMES = {v: k.upper() if len(k) == 1 else {'prior': 'PgUp', 'next': 'PgDn'}.get(k, k.upper())
           for k, v in SPECIAL_KEYS.items()}

# ★R230（第 13 批）：显示名 → SPECIAL_KEYS 规范名。
# 面板存盘写的是 `vk_to_name()` 的**显示名**（hotkey_panel.on_edit），而
# SPECIAL_KEYS 的键是 Tk keysym。'prior'/'next' 是这张表里唯二"显示名≠规范名
# 大小写形式"的条目（显示成 PgUp/PgDn），于是设成 PgUp 后 parse_binding 反查
# 得 vk=0，注册处 `if mods and vk` 静默跳过：面板显示"已设 Ctrl+Alt+PgUp"，
# 实际永久无效、连"被占用"提示都不给。补齐名层即可，显示层不动。
KEY_ALIASES = {'pgup': 'prior', 'pageup': 'prior',
               'pgdn': 'next', 'pagedn': 'next', 'pagedown': 'next'}


def vk_to_name(vk: int) -> str:
    if 0x41 <= vk <= 0x5A:
        return chr(vk)
    if 0x30 <= vk <= 0x39:
        return chr(vk)
    if 0x70 <= vk <= 0x7B:
        return f'F{vk - 0x6F}'
    return VK_NAMES.get(vk, f'VK_{vk:02X}')


def name_to_vk(name: str) -> Optional[int]:
    n = name.strip().lower()
    if len(n) == 1 and 'a' <= n <= 'z':
        return ord(n.upper())
    if len(n) == 1 and '0' <= n <= '9':
        return ord(n.upper())
    if n.startswith('f') and n[1:].isdigit() and 1 <= int(n[1:]) <= 12:
        return 0x6F + int(n[1:])
    if n in SPECIAL_KEYS:
        return SPECIAL_KEYS[n]
    alias = KEY_ALIASES.get(n)
    return SPECIAL_KEYS.get(alias) if alias else None


def mods_to_str(mods: int) -> str:
    parts = [name for name, bit in MOD_NAMES if mods & bit]
    return '+'.join(parts)


def parse_binding(parts: List[str]) -> Tuple[int, int]:
    """['Ctrl','Alt','1'] -> (mods_mask, vk)；主键取最后一个非修饰键"""
    mods = 0
    vk = 0
    for p in parts:
        q = p.strip().lower()
        if q == 'ctrl':
            mods |= MOD_CONTROL
        elif q == 'alt':
            mods |= MOD_ALT
        elif q == 'shift':
            mods |= MOD_SHIFT
        elif q == 'win':
            mods |= MOD_WIN
        else:
            vk = name_to_vk(q) or 0
    return mods, vk


def format_binding(mods: int, vk: int) -> str:
    base = mods_to_str(mods)
    key = vk_to_name(vk) if vk else ''
    return '+'.join(x for x in (base, key) if x)


# ---------------------------------------------------------------------------
# 冲突检测：返回风险提醒列表（空列表 = 相对安全）
# ---------------------------------------------------------------------------

CONFLICT_RULES: List[Tuple[str, str]] = [
    # (匹配说明, 提醒)
    ('win_mod', 'Win 组合键大量被系统占用（Win+D 显示桌面、Win+E 资源管理器、Win+Tab 任务视图、Win+数字 任务栏），强烈建议避开'),
    ('ctrl_alt_w', 'Ctrl+Alt+W 是微信的打开/截图热键'),
    ('ctrl_alt_a', 'Ctrl+Alt+A 是 QQ 截图热键'),
    ('ctrl_alt_q', 'Ctrl+Alt+Q 是 QQ 锁定热键'),
    ('ctrl_alt_arrows', 'Ctrl+Alt+方向键 是 Intel 核显的屏幕旋转热键'),
    ('alt_f4', 'Alt+F4 是关闭窗口，绝对不能用'),
    ('alt_tab', 'Alt+Tab 是切换窗口，绝对不能用'),
    ('ctrl_number', 'Ctrl+数字 是浏览器切换标签页（若加了 Shift 则冲突较少）'),
    ('ctrl_shift_n', 'Ctrl+Shift+N 是浏览器无痕模式/资源管理器新建文件夹'),
    ('ctrl_shift_t', 'Ctrl+Shift+T 是浏览器恢复刚关闭的标签页'),
    ('ctrl_shift_esc', 'Ctrl+Shift+Esc 是任务管理器'),
    ('f5', 'F5 单键是全局刷新，注册后所有软件的刷新都会被拦截'),
    ('f11', 'F11 是浏览器全屏'),
    ('shift_letter', 'Shift+字母/数字 是输入大写和符号，不能用'),
    # ★R229（用户 01 批 item3）：实测取证——热键吞掉 Alt+字母后，前台窗口还会
    # 停在"Alt 菜单态"，用户表现为"记事本打不进字、退格自动往回删"，最小化
    # 本程序才好像恢复正常（其实是我们不再吞键）。裸 Alt 属高风险档。
    ('alt_letter_digit', 'Alt+字母/数字 会抢走**所有软件**的菜单快捷键（记事本 Alt+E=编辑、'
                         'Alt+F=文件，Office/输入法里 Alt+1~9 也各有用途），'
                         '被吞的那一下还会让目标窗口停在"Alt 菜单态"，'
                         '出现"打字没反应、退格删词"。建议改用 Ctrl+Alt+…'),
]


def check_conflicts(mods: int, vk: int) -> List[str]:
    """检查组合键与主流软件/系统的冲突，返回提醒列表"""
    warnings: List[str] = []
    if mods & MOD_WIN:
        warnings.append('win_mod')
    key = vk_to_name(vk).lower() if vk else ''
    is_letter = len(key) == 1 and key.isalpha()
    is_digit = len(key) == 1 and key.isdigit()
    is_arrow = key in ('up', 'down', 'left', 'right')
    if mods == MOD_CONTROL | MOD_ALT:
        if key == 'w':
            warnings.append('ctrl_alt_w')
        elif key == 'a':
            warnings.append('ctrl_alt_a')
        elif key == 'q':
            warnings.append('ctrl_alt_q')
        if is_arrow:
            warnings.append('ctrl_alt_arrows')
    if mods == MOD_ALT and key == 'f4':
        warnings.append('alt_f4')
    if mods == MOD_ALT and key == 'tab':
        warnings.append('alt_tab')
    if mods == MOD_CONTROL and is_digit:
        warnings.append('ctrl_number')
    if mods == MOD_CONTROL | MOD_SHIFT:
        if key == 'n':
            warnings.append('ctrl_shift_n')
        elif key == 't':
            warnings.append('ctrl_shift_t')
        elif key == 'escape':
            warnings.append('ctrl_shift_esc')
    if mods == 0 and key == 'f5':
        warnings.append('f5')
    if mods == 0 and key == 'f11':
        warnings.append('f11')
    if mods == MOD_SHIFT and (is_letter or is_digit):
        warnings.append('shift_letter')
    # ★R229：裸 Alt + 字母/数字（见 CONFLICT_RULES 同条说明）
    if mods == MOD_ALT and (is_letter or is_digit):
        warnings.append('alt_letter_digit')
    return [desc for tag, desc in CONFLICT_RULES if tag in warnings]


def binding_is_bare_alt(parts: List[str]) -> bool:
    """这组绑定是不是"只按 Alt"？（['Alt','E'] 或分类方案的 ['Alt'] + 数字）

    ★R229 取证结论：RegisterHotKey 本身不会把 Alt 卡在按下态（实测注入完整
    一对 down/up 后系统态干净），但裸 Alt 组合会吃掉前台窗口的菜单助记键，
    那一下之后目标窗口常停在"Alt 菜单态"——用户在记事本里就是"字母敲不进、
    退格删词"。所以判定标准是 mods 恰好等于 MOD_ALT 且主键是字母/数字。
    """
    mods, vk = parse_binding(parts or [])
    if mods != MOD_ALT:
        return False
    if not vk:
        return True   # 分类方案只存修饰键，主键永远是数字 1~9
    key = vk_to_name(vk)
    return len(key) == 1 and (key.isalpha() or key.isdigit())


def to_ctrl_alt(parts: List[str]) -> List[str]:
    """把裸 Alt 组合改成 Ctrl+Alt+同主键（['Alt','E']→['Ctrl','Alt','E']）"""
    mods, vk = parse_binding(parts or [])
    out = ['Ctrl', 'Alt']
    if vk:
        out.append(vk_to_name(vk))
    return out if mods == MOD_ALT else list(parts or [])


# 默认快捷键方案：Ctrl+Alt 系列在主流软件里最空闲（数字位尤其安全）
DEFAULT_HOTKEY_CONFIG = {
    'toggle_dock': ['Ctrl', 'Alt', 'E'],   # 悬浮窗开关
    'show_all': ['Ctrl', 'Alt', '0'],      # 显示全部图标
    'quick_find': ['Ctrl', 'Alt', 'F'],    # 快速定位图标（按名字片段搜索）
    'category': ['Ctrl', 'Alt'],           # 切换分类 = 该修饰键 + 数字1-9（按顶部槽位顺序）
    'category_skip': [],                   # 注册被占用而放弃的数字（1-9），避免每次启动重试
}


# ---------------------------------------------------------------------------
# 管理器：独立线程消息循环，回调经队列转交主线程
# ---------------------------------------------------------------------------

class HotkeyManager:
    """全局热键管理器（RegisterHotKey + 专用消息线程）"""

    def __init__(self):
        self._bindings: List[dict] = []      # [{'id','mods','vk','payload'}]
        self._failed: List[dict] = []        # 注册失败的结构化信息
        self._registered_evt = threading.Event()  # 注册完成信号（消除竞态）
        self._queue: 'queue.Queue[dict]' = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        # ★R285-O2：线程退出原因 + 自愈计数（看门狗 revive_if_dead 用）
        self._exit_reason = ''
        self._revives = 0
        self._capped_logged = False
        # ★R313 G3：2s 内没退出的旧线程名单（热键还被它们占着，下次 stop 继续收）
        self._stale: List[threading.Thread] = []

    @property
    def failed(self) -> List[dict]:
        """注册失败列表：[{'id','mods','vk','label'}]"""
        return list(self._failed)

    def start(self, bindings: List[dict]):
        """用新的绑定列表（重）启动热键线程；返回前注册已完成（failed 可用）"""
        self.stop()
        with self._lock:
            self._bindings = list(bindings)
            self._failed = []
        self._exit_reason = ''   # ★R285-O2：新线程的退出原因从零记，别拿上一条的
        self._registered_evt.clear()
        if self._stale:
            # ★R313 G3：还有没退的旧线程＝它手里那批热键还没交出来，
            # 本次注册大概率撞"已被占用"，先把话说在前面（否则只能看到一串注册失败）
            logger.warning('⌨ 仍有 %d 条旧热键线程未退出 ⇒ 本次注册可能撞"已被占用"'
                           % len(self._stale))
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name='HotkeyThread')
        self._thread.start()
        if not self._registered_evt.wait(timeout=2):
            # ★ R142 报告#3：等待超时如实告警（此时 failed 可能不完整）
            logger.warning('等待热键注册完成超时(2s)，失败清单可能不完整')

    def _quit_thread(self, t: threading.Thread) -> bool:
        """给一条热键线程投 WM_QUIT 并等它退；返回它**是否真的退了**。

        WM_QUIT 是唯一机制（`_stop_flag` 未被 `_run` 读取），投给该线程的
        消息队列；返回 False 意味着它还活着并且仍持有自己注册的那批热键。
        """
        try:
            tid = getattr(t, '_hotkey_tid', 0)
            if tid:
                user32.PostThreadMessageW(tid, WM_QUIT, 0, 0)
        except Exception:
            pass
        t.join(timeout=2)
        return not t.is_alive()

    def stop(self):
        # ★R313 G3：先收"历次没退的"——退了的清出名单，还活着的留着下次再收
        for t in list(self._stale):
            if self._quit_thread(t):
                self._stale.remove(t)
        t = self._thread
        if t and t.is_alive():
            if not self._quit_thread(t):
                # ★ R142 报告#2：超时不隐瞒——线程还活着（热键仍被它占着）。
                #   ★R313 补正：光"保留引用"其实保不住——紧接着的 start() 会
                #   无条件把 self._thread 覆盖成新线程，那条引用就永远没人再给它
                #   投 WM_QUIT：它继续持有热键、新线程逐个注册失败 ⇒ 热键整条
                #   不可用直到重启（用户报的"一个会话内热键会失效"的另一半）。
                #   改挂进 _stale 名单，由下一次 stop() 继续收。
                logger.warning('热键线程 2s 未退出，挂入滞留名单待下次重试'
                               '（其已注册的热键本次可能仍被占用）')
                self._stale.append(t)
                return
        self._thread = None

    def _run(self):
        threading.current_thread()._hotkey_tid = kernel32.GetCurrentThreadId()
        # ★R204 NEW-R8：先强制创建消息队列——线程的消息队列是懒创建的，
        # stop() 若在本线程首次 GetMessageW 之前 PostThreadMessage(WM_QUIT)
        # 会失败被吞，线程永不退出（ROOT-3 撤回后的真实残留竞态）。
        # PeekMessage 建队零副作用。
        _mq = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(_mq), None, 0, 0, 0)
        registered = []
        try:
            for b in self._bindings:
                ok = user32.RegisterHotKey(None, b['id'],
                                           b['mods'] | MOD_NOREPEAT, b['vk'])
                if not ok:
                    self._failed.append({'id': b['id'], 'mods': b['mods'],
                                         'vk': b['vk'],
                                         'label': b.get('label', str(b['id']))})
                    logger.warning(f"热键注册失败(被占用): {format_binding(b['mods'], b['vk'])}")
                else:
                    registered.append((b['id'], b))
        except Exception as e:
            logger.error(f"热键注册异常: {e}")
        finally:
            self._registered_evt.set()  # gui 的 start() 在等这个信号

        msg = wintypes.MSG()
        why = ''
        try:
            while True:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret == 0:      # WM_QUIT：正常收摊
                    why = '收到 WM_QUIT（正常退出）'
                    break
                if ret < 0:       # GetMessageW 出错：以前这里和 WM_QUIT 混在
                    why = 'GetMessageW 出错 err=%d' % (kernel32.GetLastError() or 0)
                    logger.error(f'⌨ 热键消息循环读取出错，全局热键将停止响应: {why}')
                    break
                if msg.message == WM_HOTKEY:
                    for hid, b in registered:
                        if hid == msg.wParam:
                            self._queue.put({'id': b['id'], 'payload': b.get('payload')})
                            break
        except Exception as e:
            # ★R285-O2（P1，用户 R012 实测"一个会话内热键会失效、重启才好"）：
            # 原写法 pump 一异常就静默穿出 while，接着注销全部热键、线程结束，
            # **app.log 里一行都不留**（那次 3 小时 8 个会话的日志包实测：
            # 既无"热键处理异常"，也无"锁屏中已忽略"，也无注册失败）——
            # 用户只看到"按了没反应"，谁都无从判断。留痕 + 下面交给看门狗自愈。
            why = f'消息循环异常 {type(e).__name__}: {e}'
            logger.error(f'⌨ 热键线程异常退出（全局热键已失效）: {why}', exc_info=True)
        finally:
            # ★R313 G3：只有"我仍是当前那条线程"时才认领退出原因——挂进 _stale
            # 的旧线程可能晚几秒甚至几分钟才退，那时 start() 已经把
            # `_exit_reason` 清零并跑着新线程，让它写这句就是把新线程的
            # 状态记成上一条的病（★R285-O2 归因链本来就是为了不再猜）。
            # `_thread is None`（线程还没登记过）照旧认领：这一路没有"别人"
            # 会被写串，把它一起挡掉等于把 ★R285-O2 的留痕又变回"未记录原因"。
            if self._thread is None or threading.current_thread() is self._thread:
                self._exit_reason = why
            else:
                logger.info(f'⌨ 滞留热键线程退出（不计入当前线程原因）: {why or "未记录原因"}')
            # 退出前注销（RegisterHotKey 是线程本地的，必须本线程注销）。
            # 挪进 finally：以前异常穿出会跳过这一段。
            for hid, _b in registered:
                try:
                    user32.UnregisterHotKey(None, hid)
                except Exception as e:
                    logger.warning(f'热键注销失败(可忽略，线程退出系统会回收): {e}')
            logger.info(f'⌨ 热键线程结束：{why or "未记录原因"}｜注销 {len(registered)} 个')

    def is_alive(self) -> bool:
        t = self._thread
        return bool(t and t.is_alive())

    def revive_if_dead(self, cap: int = 3) -> str:
        """热键线程不在了 ⇒ 就地按原绑定重注册（看门狗，主线程轮询里调）。

        返回 ''=无需救；`CAPPED`=封顶了别再打转（只报一次）；否则返回线程
        退出原因（已重启）。封顶是因为"起来就死"的病根若在下一次注册处，
        无限重启会把 CPU 和日志一起刷爆。
        """
        with self._lock:
            bindings = list(self._bindings)
        if not bindings:
            return ''
        if self.is_alive():
            # ★R318 K16：线程活着＝上一次复活成功了 ⇒ "连续失败"这条链断了。
            # 封顶的本意（见上面 docstring）是防"起来就死"刷爆 CPU 和日志，
            # 计数只增不减会把一个长会话里三次**独立**死亡（每次都救回来、
            # 之后正常跑几小时）误判成同一种病 ⇒ 第三次之后永久没人救。
            if self._revives or self._capped_logged:
                self._revives = 0
                self._capped_logged = False
            return ''
        if self._revives >= cap:
            if self._capped_logged:
                return ''
            self._capped_logged = True
            return 'CAPPED'
        self._revives += 1
        reason = self._exit_reason or '线程已不在（原因未留痕）'
        logger.error(f'⌨ 检测到热键线程失联，第 {self._revives} 次自动重启。原因: {reason}')
        self.start(bindings)
        return reason

    def poll(self) -> List[dict]:
        """主线程轮询：取走所有触发的热键事件"""
        events = []
        while True:
            try:
                events.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return events
