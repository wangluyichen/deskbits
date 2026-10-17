# -*- coding: utf-8 -*-
"""★R229（用户 01 批 item3）：键盘输入注入安全层。

来由：用户报"开着本软件时，记事本有时打不进字、还自动往回删；最小化就好了"。
2026-09-23 取证（`tmp/bugflow/20260923_2300/forensic_keys.txt`）实测结论：

  E3  只发一次 Alt-down、不配对发 up ⇒ 全系统进入"Alt 一直按住"，
      此后任何前台应用收到的每个键都是 Alt+键：记事本表现为字母敲不进
      （成了菜单助记符）、退格变成 Alt+退格（往回删词）。**400ms 后仍不恢复**，
      属于持久态，必须有人补发 key-up 才解套。
  E1  RegisterHotKey(Alt+K) 触发后修饰键状态干净（热键本身不会卡 Alt）——
      所以嫌疑只剩"我们自己注入、又没配对收尾"这一条路径。
  E4  try/finally 配对 + 事后巡检 ⇒ 22ms 内回到静息态。

规矩（本模块是唯一出口）：**注入修饰键必须走 `bounce_alt()`**，
禁止在别处裸调 `keybd_event`。收尾巡检即使"用户手还按着"也不会误伤
（多补一次 key-up 对物理按键无害，真正按下状态由硬件 key-up 清）。
"""
import ctypes
import logging
import os
import threading
import time
from contextlib import contextmanager

user32 = ctypes.windll.user32
logger = logging.getLogger('desktop_manager')

VK_ALT, VK_CONTROL, VK_SHIFT = 0x12, 0x11, 0x10
KEYEVENTF_KEYUP = 0x0002

MODIFIERS = (('Alt', VK_ALT), ('Ctrl', VK_CONTROL), ('Shift', VK_SHIFT))


def is_down(vk):
    """该键当前是否处于"按下"（异步态，跨进程可见——正是别的应用读到的那份）。

    只用 0x8000 位：`& 0x0001`（自上次查询以来被按过）是**读后即清**的，
    会把别的模块的判据吃掉，历轮已明确弃用（见 core/dock.py 同处注释）。
    """
    try:
        return bool(user32.GetAsyncKeyState(vk) & 0x8000)
    except Exception:
        return False


def stuck_modifiers():
    """返回此刻仍挂在"按下"的修饰键名（正常应为空表）。"""
    return [name for name, vk in MODIFIERS if is_down(vk)]


def press(vk):
    user32.keybd_event(vk, 0, 0, 0)


def release(vk):
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def settle(vk=VK_ALT, retries=3, wait_ms=20):
    """补发 key-up 直到该修饰键不再是"按下"。返回仍卡住的键名或 ''。

    用户手还按着时会重试满次数——那不是缺陷（物理 key-up 会自己清掉），
    所以这里只回状态、不打日志，由调用方决定要不要提示。
    """
    name = dict(MODIFIERS).get(vk, hex(vk))
    for _ in range(max(1, retries)):
        if not is_down(vk):
            return ''
        try:
            release(vk)
        except Exception:
            return name
        time.sleep(wait_ms / 1000.0)
    return '' if not is_down(vk) else name


@contextmanager
def bounce_alt(timeout_ms=400):
    """把 Alt 短暂"按住"用于绕过 SetForegroundWindow 的前台锁，**保证收尾**。

    用法：`with keystate.bounce_alt(): user32.SetForegroundWindow(hwnd)`
    即便块内抛异常，finally 也会补发 key-up 并巡检回静息态。

    ★R230（第 13 批复查，同一症状的第二条路径）——try/finally 只挡住了
    "块内抛异常"，挡不住"块内**不返回**"：SetForegroundWindow 是跨进程调用，
    目标线程假死时会阻塞在系统里（本程序 data/app.log 里那种 Explorer 忙
    的瞬间就是），finally 那一刻永远等不到；本进程若在此期间被守护器
    `os._exit` 砍掉（main_window 的看门狗路径），Alt 就带着"按下"态留在
    全系统，症状与取证 E3 完全一致（记事本打不进字、退格删词）。补三道：
      ① **只在 Alt 本来没按时才注入**——用户物理按着 Alt 时不去抬它
         （原来无条件 release 会把那边的 Alt 菜单态/Alt+拖拽当场打断）；
      ② **守护线程兜底**：块内超过 timeout_ms 还没结束就代为补发 key-up 并
         巡检。ctypes 调外部 API 时会释放 GIL，所以主线程卡在系统调用里
         这个线程照样跑得动——把"永久卡住"压成"最多卡 timeout"；
      ③ 正常退出时先解除守护，再按原路收尾（顺序无关，两边都判 is_down）。
    """
    armed = threading.Event()
    injected = False
    # ★R232（S5，修首版自指噪声）：快照必须取在**注入之前**。旧写法在 press
    #   之后读，日志里那一栏永远含 Alt——那是我们自己按的，不是用户按住的键，
    #   归因"打字不上"时反而指错方向。放在最前面，本帧只读一次。
    pre = stuck_modifiers()
    if not is_down(VK_ALT):
        press(VK_ALT)
        injected = True
        # ★R232（用户 06 批 item1）：借 Alt 这件事本身要留痕——原来只在"没抬干净"
        #   时才 debug 一句，打包版根本看不到，于是"下次再出现打字不上"无从归因。
        #   记 INFO + pid：打包态是 launcher→引导→--gui 多进程，没 pid 分不清谁。
        logger.info(f'[keystate] 借用 Alt 置前台（收尾会再巡检一次）'
                    f'｜pid={os.getpid()}｜注入前按下态={pre}')
        # ★R230b（S6 验收 P3）：守护线程的 start() 旧写法在 try **之外**。
        #   线程/句柄耗尽时它抛 RuntimeError("can't start new thread")，异常从
        #   `with` 表达式处离开、finally 那一刻还没建立 ⇒ Alt 带着"按下"态留在
        #   全系统——正是本模块要灭的那个症状。就地收尾后再抛，不静默。
        try:
            threading.Thread(target=_watchdog, args=(armed, timeout_ms),
                             daemon=True).start()
        except Exception:
            injected = False
            release(VK_ALT)
            stuck = settle(VK_ALT)
            if stuck:
                logger.warning(f'[keystate] 守护线程起不来、补抬 Alt 仍有余留: '
                               f'{stuck}｜pid={os.getpid()}'
                               f'｜仍按下={stuck_modifiers()}')
            raise
    try:
        yield
    finally:
        armed.set()
        if injected:
            release(VK_ALT)
            stuck = settle(VK_ALT)
            if stuck:
                # ★R232：debug → warning + pid + 当时的修饰键快照（"打字不上"的
                #   直接证据；INFO 级以上才会进 data/app.log 的默认可见档）
                logger.warning(f'[keystate] bounce_alt 收尾仍有按键未回位: '
                               f'{stuck}｜pid={os.getpid()}'
                               f'｜仍按下={stuck_modifiers()}')


def _watchdog(armed, timeout_ms):
    """守护线程：块内超时未结束就代为把 Alt 放回静息态（见 bounce_alt ②）。"""
    if armed.wait(max(0.05, timeout_ms / 1000.0)):
        return          # 正常收尾，什么都不用做
    if is_down(VK_ALT):
        settle(VK_ALT, retries=5, wait_ms=20)


def sweep_on_startup():
    """★R230：进程起来先巡检一遍修饰键，把上一世"卡住"的 Alt/Ctrl/Shift 放回。

    守护线程救不了"进程直接被杀"那一种（线程跟进程一起没），但那种留下的
    卡键态是持久的（取证 E3：400ms 不自愈）——所以把它交给**下一次启动**兜底：
    用户真遇到"上次用完打不进字"，重开本程序即解套，不必再靠重启 Explorer。
    返回本次清掉的键名列表（空表=系统态本来就干净）。
    """
    cleared = []
    for name, vk in MODIFIERS:
        if is_down(vk) and settle(vk, retries=3, wait_ms=20) == '':
            cleared.append(name)
    if cleared:
        # ★R232：这一句是"上一次退出把键带走了"的唯一痕迹（干净才不记，B1d）
        logger.info(f'[keystate] 启动巡检复位卡住的修饰键: {cleared}'
                    f'｜pid={os.getpid()}｜仍按下={stuck_modifiers()}')
    return cleared

