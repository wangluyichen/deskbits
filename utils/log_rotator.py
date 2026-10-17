# -*- coding: utf-8 -*-
"""安全轮转 handler（唯一的正主，★R313 G2）。

放在这个叶子模块里是有原因的：历史上这个类同时存在**两份拷贝**——
`utils/logger.py`（logs/debug.log 等）与 `gui/app_bootstrap.py`
（data/app.log + data/switch.log，用户实际发给我看的那份）——各自
`except (PermissionError, OSError): pass`。gui/app_bootstrap 不能 import
utils.logger（后者模块级就 `LOG_DIR.mkdir()`，data/ 不可写时会把入口一起带崩，
正是 ★P0-3 拼命护住的那条路），所以两边一直各修各的、谁也修不到谁。
本模块只依赖 stdlib，两侧共用这一份实现。
"""
import logging
import logging.handlers
import os
import time


class SafeRotator(logging.handlers.RotatingFileHandler):
    """Windows 下轮转遇文件被占用时：**留痕 + 退避 + 继续写**，绝不因日志崩程序。

    ★R313 G2（取证 `run_tmp/_r313_g2_probe_pre.txt`，py3.14.7）：另一个句柄
    占住日志文件时 `rotate()` 确实抛 `PermissionError [WinError 32]`，旧写法
    只 `pass`。后果不是丢日志（3.14 的 `shouldRollover` 会把关掉的 stream 重开，
    这条由门禁控制组 G2-1 钉住），而是：

      · **一条痕都不留**——docstring 承诺"放弃轮转继续写"，可没人知道它已经
        放弃了，下一轮排查只能重猜；
      · `shouldRollover` 之后**每条记录都再试一次**失败的 os.remove + os.rename
        （实测两次 emit ⇒ 两次 rotate RAISE），文件一路涨过 maxBytes ⇒
        轮转层唯一的用途"防撑爆磁盘"落空（app.log 被用户开着时就是这种状态）。

    现在：失败即进入退避窗口（默认 5 分钟）——窗口内不再尝试改名、照常写当前
    文件；并留一条 warning 说清"哪个文件、为什么、多久之内不再试"。
    """

    BACKOFF_S = 300            # 改名失败后多久之内不再尝试轮转
    _backoff_until = 0.0       # 类属性兜底：没失败过=0（失败后写实例自己的键）

    def shouldRollover(self, record):
        if time.time() < self._backoff_until:
            return False
        return super().shouldRollover(record)

    def doRollover(self):
        try:
            super().doRollover()
        except (PermissionError, OSError) as e:
            # 先立退避牌、再留痕：这条 warning 自己也要过 shouldRollover，
            # 顺序反过来就是自己招自己（每写一条日志再撞一次失败改名）。
            self._backoff_until = time.time() + self.BACKOFF_S
            logging.getLogger('desktop_manager').warning(
                '[日志轮转] %s 改名失败（被其他程序占用？）：%s ⇒ %d 秒内不再尝试轮转，'
                '期间日志继续写当前文件、长度暂不受限'
                % (os.path.basename(self.baseFilename), e, self.BACKOFF_S))


class SafeConsoleHandler(logging.StreamHandler):
    """控制台 handler：码页装不下的字符就地替换，绝不炸成 `--- Logging error ---`。

    ★R319 K22（真机自动测试报告里四条那样的堆栈块）：基类 `emit` 就是
    `stream.write(self.format(record) + terminator)`，而 Windows 中文控制台默认
    GBK 码页——日志里带 `⚡`（电源、清桌那几句）时 `write` 抛 UnicodeEncodeError，
    logging 的 `handleError` 于是往 stderr 打一整块堆栈，**真线索埋在一屏
    Traceback 中间**。控制台只是"顺带看一眼"，落盘那份才是正主，所以这里宁可
    把装不下的那个字符换成 `?`，也不许把整行换成堆栈。

    放在这个叶子模块而不是 `utils/logger.py`：★R313 同一条理由——启动入口不能
    为了拿一个 handler 去 import 会在模块级 `LOG_DIR.mkdir()` 的 logger。
    """

    def emit(self, record):
        try:
            msg = self.format(record)
            stream = self.stream
            enc = getattr(stream, 'encoding', None) or 'utf-8'
            try:
                msg.encode(enc)
            except (UnicodeEncodeError, LookupError):
                # 先编一遍探码页，装不下就整行按该码页替换后再写（只损失那几个字符）
                msg = msg.encode(enc, 'replace').decode(enc, 'replace')
            stream.write(msg + self.terminator)
            self.flush()
        except RecursionError:
            raise
        except Exception:
            self.handleError(record)
