# -*- coding: utf-8 -*-
"""clean_exit.flag 统一写入（★R207 NEW3-A1）。

历史事故（launcher.pyw P1-N9/#10）：第二实例"唤醒已有实例后退出"必须
留下退出标记，否则守护器把 0.1s 快速退出当启动异常→2s 重启→再唤醒→
循环 6 次（用户"以为关不掉"）。R204 给 main_window 写入口加了 pid=，
但 app_bootstrap 的第二入口仍写裸时间戳→launcher pid 校验恒败→事故
复活。本 helper 统一两个入口的格式：`<iso时间> pid=<pid>`。"""
import time

from utils.app_paths import get_app_dir

FLAG_PATH = get_app_dir() / 'data' / 'clean_exit.flag'


def write_clean_exit_flag():
    """写退出标记（带本进程 PID）。失败静默（与两处旧实现一致——
    launcher 对"无 flag"另有退出码+耗时兜底）。
    ★R213（NEW5-A8）：tmp+os.replace 原子落盘——裸 write_text 被 launcher
    读到半截时 pid= 恰好缺失，会撞上"无 pid=旧格式恒判用户意图"最宽判据。"""
    import os
    try:
        FLAG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _tmp = FLAG_PATH.with_suffix('.flag.tmp')
        _tmp.write_text(
            f"{time.strftime('%Y-%m-%d %H:%M:%S')} pid={os.getpid()}",
            encoding='utf-8')
        os.replace(_tmp, FLAG_PATH)
        return True
    except Exception:
        return False
