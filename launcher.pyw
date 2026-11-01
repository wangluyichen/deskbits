"""守护启动器：拉起 gui，异常退出自动重启（干净退出 code=0 不重启）。

用法：
  - 源码模式：启动.bat 用 pythonw 拉起本脚本 → 本脚本 Popen(gui/main_window.py)
  - 打包模式(exe)：双击 exe → 本脚本 Popen([exe, '--gui']) → GUI 子进程

gui 崩溃时：
- data/app.log 记录 Python 异常（gui 的全局钩子负责）
- data/crash.log 记录原生崩溃线程栈（faulthandler 负责）
- 本脚本 2 秒后重启；5 分钟内连续失败超过 5 次则放弃（避免死循环刷屏）

★R213（NEW5-A9）更正头部旧话术：现行逻辑不是"退出太快(<5s)一律继续守护"，
而是 flag 优先——clean_exit.flag 存在且通过 pid/时间窗校验 = 用户意图，
守护结束（不看耗时）；无 flag 才按退出码+耗时兜底（code=0 且 >=5s 视为
干净退出停守护；code=0 且 <5s 判唤醒/启动异常继续守护；code!=0 崩溃重启）。
gui 单实例唤醒路径也写 flag（带 pid），因此"重复启动"不再进重启循环。
"""
import os
import re
import sys
import time
import subprocess
import logging

# ★ R174 C5：Nuitka 编译态默认不设 sys.frozen（只有 PyInstaller 设），
# 不补上它，本文件与 utils/app_paths、utils/logger 的打包判定全走源码
# 分支——实测 exe 落"源码模式"去拉不存在的 python.exe（WinError 2 静默
# 重启循环）。__compiled__ 是 Nuitka 注入主模块的编译标记。
_IS_COMPILED = globals().get('__compiled__') is not None
if _IS_COMPILED and not getattr(sys, 'frozen', False):
    sys.frozen = True


def _outer_exe():
    """外层 exe 绝对路径。Nuitka onefile 下 sys.executable 指向缓存解压
    目录里的内层 exe（数据目录会算错），故优先 Nuitka 官方口
    __compiled__.original_argv0（外层 exe 被调用时的原始路径）→
    sys.argv[0] → sys.executable，逐级过存在性校验。"""
    comp = globals().get('__compiled__')
    for cand in (getattr(comp, 'original_argv0', None) if comp else None,
                 sys.argv[0] if sys.argv else None,
                 sys.executable):
        if not cand:
            continue
        p = os.path.abspath(cand)
        if os.path.exists(p):
            return p
    return sys.executable


# 打包模式(frozen)：BASE = 外层 exe 所在目录；源码模式：launcher.pyw 所在目录
if getattr(sys, 'frozen', False):
    if _IS_COMPILED:
        BASE = os.path.dirname(_outer_exe())
    else:
        BASE = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
MAX_RESTARTS = 5            # 连续失败上限（含单实例唤醒、启动异常、崩溃）
RESTART_WINDOW = 300        # 秒（该窗口内连续失败才计数）
RESTART_DELAY = 2
SUSPICIOUS_EXIT_THRESHOLD = 5  # 秒，低于此值认为是单实例唤醒或启动异常
# ★ P1-N9 + #10：clean_exit.flag = "本次退出是用户意图"的标记，GUI 在两个路径写：
#   ① _on_close 里 root.destroy() 前 —— 用户主动关程序
#   ② _acquire_single_instance 检测到"已有实例"时 —— 重复启动，唤醒后立刻退出
#   两者对 launcher 的语义相同：停止守护，不要重启。
#   （旧实现只靠固定 5 秒阈值判断，onefile 冷启动 3~6s 会误判为干净退出；
#     而"重复启动"路径原先不写任何标记 → 每 2 秒被重启一次 → 每次重启都
#     唤醒已有实例 → 主窗口被反复弹到前台，实测循环 6 次）
CLEAN_EXIT_FLAG = os.path.join(BASE, 'data', 'clean_exit.flag')

# ★R255（46 批 item④-1）哨兵影子模式：core/desktop_sentinel 经 `cmd /c start`
# 中间层拉起的同一个 exe 带 --desktop-sentinel <主pid>（cmd 秒退，哨兵父链
# 断挂，"结束进程树"抓不到——E2 实测 0.06s 拉亮）。只盯主进程、断了气就
# 拉亮桌面——不建窗口、不写日志链、不进守护循环，跑完自己退。
if '--desktop-sentinel' in sys.argv:
    try:
        from core import desktop_sentinel
        sys.exit(desktop_sentinel.main(sys.argv))
    except Exception:
        sys.exit(0)

# ★R280-C（用户 06 批 item2「修」，N-1）：桌面快捷方式的 WScript.Shell COM
# 挪进子进程——主进程一发 COM 都不调，原生 AV 只可能打死这个一次性沙盒。
# 同一个 exe 带 --lnk-helper <json> 拉起：建完链就用退出码回话，不建窗口、
# 不进守护循环、不写 app.log（口径照上面 ★R255 哨兵影子模式）。
if '--lnk-helper' in sys.argv:
    try:
        from utils.desktop_shortcut import run_lnk_helper
        # ★R281-R（S6 打回）：op=read 的 expected 是**第 4 个**参数（_helper_argv 末尾
        # 追加的那一个），旧写法只转 index+1 ⇒ 子进程拿 '' 去比 ⇒ 恒"不是本版"
        # ⇒ 每次启动都重刷 .lnk，"已是本版⇒零动作"永远达不到。少一个都不行。
        _i = sys.argv.index('--lnk-helper')
        _exp = sys.argv[_i + 2] if len(sys.argv) > _i + 2 else None
        sys.exit(run_lnk_helper(sys.argv[_i + 1], _exp))
    except Exception:
        sys.exit(2)

# ★ 打包模式：exe 被以 --gui 参数拉起时，直接运行 GUI（不再走守护循环）
if '--gui' in sys.argv:
    try:
        from gui.main_window import main
        main()
        sys.exit(0)
    except Exception as e:
        # 打包模式下 GUI 导入/启动异常：写日志 + 弹框提示，避免静默闪退
        try:
            os.makedirs(os.path.join(BASE, 'data'), exist_ok=True)
            with open(os.path.join(BASE, 'data', 'gui_fatal.log'), 'a',
                      encoding='utf-8') as f:
                f.write(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] '
                        f'GUI 启动异常: {e}\n')
        except Exception:
            pass
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0, f'程序启动失败：\n{e}\n\n请查看 data/gui_fatal.log',
                '桌面超强管理助手', 0x10)
        except Exception:
            pass
        sys.exit(1)

# 确保 data/ 存在，否则 basicConfig 的 FileHandler 静默失败
os.makedirs(os.path.join(BASE, 'data'), exist_ok=True)

logging.basicConfig(
    filename=os.path.join(BASE, 'data', 'launcher.log'),
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    encoding='utf-8')
log = logging.getLogger('launcher')


def _exit_says_user(txt, mtime, proc_pid, code, start, now, elapsed,
                    mtime_stop_used):
    """★R213（NEW5-A10）：守护「用户意图」五条判据纯函数化。

    原内联链与文件系统/真实时钟/日志副作用纠缠，离线无法穷举验证；
    抽净后由 自动测试 t_flag_matrix 做全矩阵断言。本函数零副作用。
    入参：txt=flag 文件内容（None=无 flag 或读失败）；mtime=flag 修改
    时间（None=不可读，按窗外）；proc_pid=本轮子进程 pid；code/elapsed
    =退出码与本轮真实耗时；start/now=本轮墙钟端点；mtime_stop_used=
    本场守护"仅凭时间窗停守护"配额是否已用（A5）。
    返回 (decision, mtime_stop_used, why)：decision ∈ 'stop'（结束守
    护）/'restart'（继续守护）；why=供日志的人类可读理由。"""
    # A6 窗口归一化：NTP 回拨/手改时间/VM 快照恢复可致 now<start，
    # 端点倒挂会让真 flag 恒落窗外
    win_lo = min(start, now) - 1
    win_hi = max(start, now) + 2
    in_win = mtime is not None and win_lo <= mtime <= win_hi

    def _fallback(ignore_why):
        # ④ flag 被忽略 → ⑤ 按退出码+耗时兜底
        if code == 0 and elapsed >= SUSPICIOUS_EXIT_THRESHOLD:
            return ('stop', mtime_stop_used,
                    f'{ignore_why}；且为干净退出（无有效 flag，耗时 '
                    f'{elapsed:.1f}s≥{SUSPICIOUS_EXIT_THRESHOLD}s），守护结束')
        if code == 0:
            return ('restart', mtime_stop_used,
                    f'{ignore_why}；快速干净退出（{elapsed:.1f}s < '
                    f'{SUSPICIOUS_EXIT_THRESHOLD}s），判单实例唤醒或启动异常')
        return ('restart', mtime_stop_used,
                f'{ignore_why}；异常退出 code={code}')

    if txt is not None:
        m = re.search(r'pid=(\d+)', txt)
        if m is None:
            # ③ 无 pid（旧格式/第二实例唤醒）：同样受本轮区间约束（A4，
            # 防删失败的残留 flag 恒判意图把守护谎杀）
            if in_win:
                return ('stop', mtime_stop_used,
                        'clean_exit.flag 无 pid 字段（旧格式/第二实例唤醒'
                        '退出），时间戳在本轮区间，按用户意图处理')
            return _fallback(
                f'clean_exit.flag 无 pid 且时间戳不在本轮区间'
                f'（mtime={mtime}），疑为删失败的残留，忽略')
        if int(m.group(1)) == proc_pid:
            # ① 最强判据：pid 实证匹配本子进程（无需时间/退码）
            return ('stop', mtime_stop_used,
                    'clean_exit.flag pid 匹配本轮子进程，按用户主动退出处理')
        # ②④ pid 不匹配：单文件包的内层进程、或单目录包/外部写手——三重闸：
        # 窗口命中 + code==0 + 全场仅一次（A5，防二份程序反复冒充意图）
        if in_win and code == 0 and not mtime_stop_used:
            return ('stop', True,
                    'clean_exit.flag pid 不匹配但时间戳在本轮区间且 code=0'
                    '（单文件包的内层进程、单目录包的子进程都会留下这种 pid 对不上'
                    '的 flag），按用户意图处理（路条已用掉一次）')
        if in_win and code == 0:
            return _fallback('clean_exit.flag pid 不匹配、窗内且 code=0，'
                             '但本场"仅时间窗停守护"配额已用尽（A5），忽略')
        if in_win:
            return _fallback(f'clean_exit.flag pid 不匹配、时间窗命中但 '
                             f'code={code}≠0——身份不明 flag 不足以取消崩溃重启，忽略')
        return _fallback(f'clean_exit.flag PID 不匹配（flag pid={m.group(1)}，'
                         f'子进程 pid={proc_pid}）且窗外，疑似残留污染，忽略')
    # ⑤ 无 flag：纯兜底口径
    return _fallback('无 clean_exit.flag')


def main():
    if getattr(sys, 'frozen', False):
        # 打包模式：用同一个 exe + --gui 参数拉起 GUI 子进程
        # ★ R174：走 _outer_exe（original_argv0 优先）——Nuitka onefile
        # 下 sys.executable 是缓存解压目录的内层 exe，直接用会
        # WinError 2 / 数据目录错位
        gui_cmd = [_outer_exe(), '--gui']
        log.info(f'守护启动器开始（打包模式）: {gui_cmd}')
    else:
        # 源码模式：pythonw gui/main_window.py
        pythonw = sys.executable.replace('python.exe', 'pythonw.exe')
        if not os.path.exists(pythonw):
            pythonw = sys.executable
        gui_cmd = [pythonw, 'gui/main_window.py']
        log.info(f'守护启动器开始（源码模式）: {gui_cmd}')

    restarts = []  # 失败时间戳（含崩溃、快速干净退出）
    # ★R213（NEW5-A5）：pid 不匹配仅凭时间窗停守护整场守护最多用一次——
    # 防"另一目录第二份程序/常驻外部写手"反复冒充用户意图杀守护
    _mtime_stop_used = False
    while True:
        # gui 已有单实例锁（窗口互斥），不需要 launcher 再查进程
        # 直接 Popen + wait 即可——gui 第二次启动会发现已有实例、弹窗、sys.exit(0)
        # ★ P1-N9：Popen 前先清掉上次残留的 clean_exit.flag
        # （若上次崩溃，flag 不会被写，残留的是更早的；启动新一轮前先清零）
        try:
            if os.path.exists(CLEAN_EXIT_FLAG):
                os.remove(CLEAN_EXIT_FLAG)
        except Exception as _e:
            # ★R213（NEW5-A4）：删除失败必须留痕——残留旧 flag 会参与下方
            # 用户意图判定（无 pid 旧格式恒判意图），静默失败=误因难查
            log.warning(f'清理残留 clean_exit.flag 失败（可能影响本轮判定）: {_e}')
        start = time.time()
        _start_mono = time.monotonic()   # ★R213（NEW5-A6）计时防时钟回拨
        proc = subprocess.Popen(gui_cmd, cwd=BASE)
        code = proc.wait()
        elapsed = time.monotonic() - _start_mono
        log.info(f'GUI 退出，code={code}，耗时 {elapsed:.1f}s')

        now = time.time()
        restarts = [t for t in restarts if now - t < RESTART_WINDOW]

        # ★ P2-N21 + ★R213（NEW5-A10）：判定主体已抽为纯函数
        # _exit_says_user（五条判据①~⑤，配套 t_flag_matrix 离线穷举）；
        # 此处只负责 flag 读写与日志。★R213（NEW5-A6）窗口端点归一化
        # 也已移入纯函数内。
        _txt = None
        _mt = None
        _flag_exists = False
        if os.path.exists(CLEAN_EXIT_FLAG):
            _flag_exists = True
            try:
                with open(CLEAN_EXIT_FLAG, 'r', encoding='utf-8',
                          errors='replace') as _f:
                    _txt = _f.read()
                try:
                    _mt = os.path.getmtime(CLEAN_EXIT_FLAG)
                except OSError:
                    _mt = None      # 时间戳不可读 → 纯函数按窗外处理
            except Exception as _e:
                log.warning(f'clean_exit.flag 读取失败（按无 flag 处理）: {_e}')
                _txt = None
                _mt = None
        decision, _mtime_stop_used, why = _exit_says_user(
            _txt, _mt, proc.pid, code, start, now, elapsed, _mtime_stop_used)
        if decision == 'stop':
            if _flag_exists:
                try:
                    os.remove(CLEAN_EXIT_FLAG)
                except Exception as _e:
                    # ★R213（NEW5-A4）：留痕（守护已结束，残留会被下轮开头清掉）
                    log.warning(f'退出后删除 clean_exit.flag 失败: {_e}')
            log.info(f'{why}（code={code}, 耗时 {elapsed:.1f}s）')
            return

        # 继续守护：崩溃重启 / 快速干净退出都算失败
        restarts.append(now)
        if len(restarts) > MAX_RESTARTS:
            log.error(f'{RESTART_WINDOW}s 内已失败 {len(restarts)} 次，放弃重启，'
                      f'请查看 data/app.log / data/crash.log')
            return
        log.warning(f'{why}——继续守护（近 {RESTART_WINDOW}s 第 {len(restarts)} '
                    f'次），{RESTART_DELAY}s 后重启')
        time.sleep(RESTART_DELAY)


if __name__ == '__main__':
    main()
