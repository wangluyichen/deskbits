"""统一日志系统 - 记录所有关键操作，方便调试"""
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

# ★R313 G2：轮转兜底不再在这里另写一份，统一走 utils/log_rotator.py 那个正主
# （`_SafeRotator` 这个名字被门禁按模块层引用，保留原拼写）。
from utils.log_rotator import (SafeRotator as _SafeRotator,
                                SafeConsoleHandler as _SafeConsoleHandler)

# 日志目录：与 data/、config/ 保持一致，统一放在"程序根目录"——
# 打包后 = exe 同目录（用户一眼能找到）；源码模式 = 项目根目录。
# ★ 不能用 Path(__file__)：打包后 __file__ 指向 PyInstaller 的 _MEIPASS
#   临时解压目录 —— onefile 版退出即随临时目录删除（日志全丢），
#   onedir 版会落到 _internal/logs/（用户找不到）。
# ★ R174 C5：改走 app_paths 统一出口——Nuitka onefile 下
#   sys.executable=缓存解压目录的内层 exe，须取外层 exe 目录。
from utils.app_paths import get_app_dir
BASE_DIR = get_app_dir()
LOG_DIR = BASE_DIR / 'logs'
LOG_DIR.mkdir(parents=True, exist_ok=True)

# 日志文件路径
DEBUG_LOG = LOG_DIR / 'debug.log'
FRAME_LOG = LOG_DIR / 'frame_debug.log'  # 方框绘制专用日志
ERROR_LOG = LOG_DIR / 'error.log'


def setup_logger(name: str, log_file: Path, level: int = logging.DEBUG,
                 file_level: int = logging.DEBUG) -> logging.Logger:
    """创建日志记录器。

    file_level：文件 handler 的门槛。frame_debug 等高频调试日志传 INFO，
    把"每 2 秒一轮"的周期性明细（R70 降噪，2026-09-15）挡在文件外；
    logger 本身仍收 DEBUG，排查方框问题时把 file_level 调回 DEBUG 即可见。"""
    logger = logging.getLogger(name)
    logger.setLevel(level)

    # 避免重复添加 handler
    if logger.handlers:
        return logger

    # 文件 handler（轮转上限 2MB×2 份，防止长期使用撑爆磁盘）
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler = _SafeRotator(log_file, maxBytes=2 * 1024 * 1024,
                                backupCount=2, encoding='utf-8')
    file_handler.setLevel(file_level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # 控制台 handler（只输出警告以上）
    # ★R319 K22：裸的 StreamHandler 遇 GBK 控制台装不下的字符（`⚡`）就把整行换成
    # 一块 `--- Logging error ---` 堆栈，真线索反被埋了。
    console_handler = _SafeConsoleHandler()
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    return logger


# 创建日志记录器
debug_logger = setup_logger('debug', DEBUG_LOG)
# ★ R70 降噪：frame_debug 曾 3 天写满 2MB（高亮可见时每 2 秒一轮"开始应用
# 数据/对齐统计/逐图标 original=…"十几~几十行）。文件门槛提到 INFO 后只剩
# 罕见事件；周期明细改走 log_frame_debug（DEBUG），需要时把 file_level 调回。
frame_logger = setup_logger('frame_debug', FRAME_LOG, file_level=logging.INFO)
# ★ R142 降噪：切断向 root 的传播——app.log 的 handler 挂在 root logger 上，
# frame 的 DEBUG 周期明细虽被自身文件的 INFO 门槛挡住，却经 propagate
# 全数涌进 app.log（win11 长跑日志实锤：app.log 万余行里九成带
# frame_debug: 前缀）。方框排查本来就该看 logs/frame_debug.log。
frame_logger.propagate = False
error_logger = setup_logger('error', ERROR_LOG, logging.ERROR)


def log_frame_action(action: str, **kwargs):
    """记录方框绘制相关操作"""
    msg = f"[FRAME] {action}"
    if kwargs:
        msg += " | " + " | ".join(f"{k}={v}" for k, v in kwargs.items())
    frame_logger.info(msg)


def log_frame_debug(action: str, **kwargs):
    """★ R70：周期性方框明细走 DEBUG（被文件 INFO 门槛挡住，不落盘）"""
    msg = f"[FRAME] {action}"
    if kwargs:
        msg += " | " + " | ".join(f"{k}={v}" for k, v in kwargs.items())
    frame_logger.debug(msg)


def log_debug(msg: str, **kwargs):
    """记录调试信息"""
    full_msg = msg
    if kwargs:
        full_msg += " | " + " | ".join(f"{k}={v}" for k, v in kwargs.items())
    debug_logger.info(full_msg)


def log_error(msg: str, exc: Exception = None):
    """记录错误信息"""
    if exc:
        error_logger.error(msg, exc_info=exc)
    else:
        error_logger.error(msg)


def log_warning(msg: str):
    """记录警告信息"""
    debug_logger.warning(msg)


# 便捷函数
def print_frame_info(rects: dict, idx_name: dict, shared_edges: set = None):
    """打印方框绘制信息（便于调试）——★ R70 降为 DEBUG（周期性明细不落盘）"""
    log_frame_debug("绘制信息",
                    total_icons=len(rects),
                    visible_icons=len([r for r in rects.values() if r[2]-r[0] > 40 and r[3]-r[1] > 30]),
                    shared_edges=len(shared_edges) if shared_edges else 0)

    # 打印前 5 个图标的信息
    for idx in list(rects.keys())[:5]:
        l, t, r, b = rects[idx]
        name = idx_name.get(idx, '?')
        log_frame_debug(f"图标 {idx}",
                        name=name,
                        rect=f"({l},{t},{r},{b})",
                        size=f"{r-l}x{b-t}")


def print_alignment_info(rects: dict, aligned: dict, label: str):
    """打印对齐前后的对比信息——★ R70 降为 DEBUG（每 2 秒一轮的逐图标
    original/aligned 明细曾把 frame_debug.log 3 天刷满 2MB）"""
    log_frame_debug(f"{label} 对齐统计",
                    total=len(rects),
                    changed=sum(1 for idx in rects if rects[idx] != aligned.get(idx)))

    # 打印发生变化的图标
    for idx in rects:
        if rects[idx] != aligned.get(idx):
            orig = rects[idx]
            new = aligned.get(idx)
            if new is None:
                # ★R204 ROOT-14 残留：aligned 缺键跳过（原 aligned[idx] 会
                # KeyError）；顺带删除"把左坐标当图标名"的死变量行
                continue
            log_frame_debug(f"{label} 图标 {idx}",
                            original=f"({orig[0]},{orig[1]},{orig[2]},{orig[3]})",
                            aligned=f"({new[0]},{new[1]},{new[2]},{new[3]})")
