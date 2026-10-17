# -*- coding: utf-8 -*-
"""★R255 桌面哨兵（46 批 item④-1 用户点名"需要做，以保万一"）。

背景：管理页「🙈 隐藏」把 explorer 的 SHELLDLL_DefView 整层 SW_HIDE。
★R254 已把"程序还活着"的全部出路接上复明（退出 teardown / 崩溃钩子），
剩最后一条极端路径：**任务管理器强杀**——Python 钩子根本没机会跑。
本模块就是那条路的第 4 层防线：

  隐藏生效时，主进程拉起一个"影子进程"（同一个 exe 的隐藏模式
  `--desktop-sentinel <主pid>`），它只干一件事：盯着主进程。
  主进程一断气且桌面还黑着 ⇒ 立刻 SW_SHOW 拉亮，然后自己退出。

设计要点（门禁钉的就是这些）：
- **拉起走 `cmd /c start` 中间层**：start 把哨兵托付给系统壳后 cmd 即退出，
  哨兵登记的父 pid 指向那个已消失的中间层 ⇒"结束进程树"按在世父链枚举
  抓不到它（★R255 E2 实测：ShellExecuteW 直拉时哨兵挂在主进程名下，
  树杀连哨兵一起杀、桌面不亮；改 start 后复验通过）。cmd 以
  CREATE_NO_WINDOW 拉起，不留黑框。
- 状态文件 `data/desktop_hide_state.json` 是唯一通信面：
  hide_icons 成功⇒写 hidden=true；show_icons / ensure_desktop_view_visible
  ⇒写 hidden=false（复明即"隐藏态已结束"，哨兵见 false 自己收工，
  所以**正常退出/崩溃/悬浮条全部路径都不需要知道哨兵存在**）。
- 哨兵本体零新增能力：复明调的是 ★R254 模块级
  `core.icon_manager.ensure_desktop_view_visible`（幂等，只发 SW_SHOW），
  不 import Tk、不构造 IconManager、不碰键鼠注入/注册表。
- 已知边界（如实）：①用户把哨兵那个 pid 也单独杀掉 ⇒ 仍靠下次启动自愈；
  ②极短时间反复 隐藏/显示 可能留下多个哨兵——复明幂等，无害，到点都自退。
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

STATE_NAME = 'desktop_hide_state.json'
# 哨兵最长存活：7 天（防主进程常年"隐藏挂着不关"时它变僵尸；到期自退，
# 真被强杀的概率窗口远小于这个数）
MAX_LIFETIME = 7 * 24 * 3600.0


def _app_dir() -> Path:
    """data/ 所在目录：与 launcher.pyw 的 BASE/data、clean_exit.flag 同一处。
    打包态经 utils.app_paths（onefile 取外层 exe 目录）；取不到退回
    源码布局（本文件的上一级=项目根）。"""
    try:
        from utils.app_paths import get_app_dir
        return Path(get_app_dir())
    except Exception:
        return Path(__file__).resolve().parent.parent


def state_path() -> str:
    return str(_app_dir() / 'data' / STATE_NAME)


def set_hidden(hidden: bool) -> bool:
    """写"当前是否处于整层隐藏态"。写失败不抛（调用方都是顺手线，
    不是链路闸）：返回 False 由调用方记日志。"""
    try:
        p = state_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'hidden': bool(hidden), 'pid': os.getpid(),
                       'ts': time.time()}, f, ensure_ascii=False)
        os.replace(tmp, p)
        return True
    except Exception:
        return False


def read_state():
    """读状态文件；缺失/坏档返回 None（哨兵按"无账可查"处理）。"""
    try:
        with open(state_path(), 'r', encoding='utf-8',
                  errors='replace') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _selflog(msg: str):
    """哨兵自己的流水账（data/sentinel.log，逐行即刷）：主进程死了它还得
    活着写日志，所以不借 app 的 logger（那条链可能正断着）。"""
    try:
        p = _app_dir() / 'data' / 'sentinel.log'
        with open(p, 'a', encoding='utf-8', buffering=1) as f:
            f.write('[%s] %s pid=%d\n'
                    % (time.strftime('%Y-%m-%d %H:%M:%S'), msg, os.getpid()))
    except Exception:
        pass


def build_spawn_cmd(pid: int):
    """拉起命令（两态）：
    - 打包态：外层 exe + '--desktop-sentinel <pid>'（launcher.pyw 顶部有分支）；
    - 源码态：pythonw（或 python）+ 本脚本路径 + <pid>。
    返回 (file, params, cwd)。"""
    app = _app_dir()
    if getattr(sys, 'frozen', False):
        try:
            from utils.app_paths import get_executable
            exe = get_executable()
        except Exception:
            exe = sys.executable
        return (exe, '--desktop-sentinel %d' % int(pid), str(app))
    pythonw = sys.executable.replace('python.exe', 'pythonw.exe')
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    script = str(Path(__file__).resolve())
    return (pythonw, '"%s" %d' % (script, int(pid)), str(app))


def spawn(pid: int) -> bool:
    """经 `cmd /c start` 拉起哨兵：cmd 瞬间退出，哨兵登记的父 pid 成孤儿引用，
    taskkill /T 的在世父链枚举抓不到它（躲"结束进程树"）。返回是否成功；
    失败由调用方记日志。CREATE_NO_WINDOW=0x08000000 保证不留任何控制台痕迹。"""
    try:
        file_, params, cwd = build_spawn_cmd(pid)
        if not file_:
            return False
        line = 'start "" "%s" %s' % (file_, params)
        p = subprocess.Popen('cmd /c ' + line, cwd=cwd,
                             creationflags=0x08000000)
        rc = p.wait(timeout=10)
        ok = rc == 0
        _selflog('spawn pid=%d target=%s ok=%s rc=%d'
                 % (os.getpid(), pid, ok, rc))
        return ok
    except Exception:
        return False


def on_hide() -> bool:
    """hide_icons 成功后的接线：记状态 + 拉起哨兵。
    返回哨兵是否已在位（拉不起也至少把状态写了——下次启动自愈兜底）。"""
    set_hidden(True)
    return spawn(os.getpid())


def _show_desktop() -> bool:
    """复明：优先调产品模块级函数（单一实现），万一 import 都炸了
    （打包残缺等异常态）再用纯 ctypes 的同款查找兜底。"""
    try:
        from core.icon_manager import ensure_desktop_view_visible
        return bool(ensure_desktop_view_visible())
    except Exception:
        pass
    try:
        import ctypes
        u = ctypes.windll.user32
        SW_SHOW = 5
        found = False
        progman = u.FindWindowW('Progman', None)
        if progman:
            shell = u.FindWindowExW(progman, 0, 'SHELLDLL_DefView', None)
            if shell:
                found = True
                if not u.IsWindowVisible(shell):
                    u.ShowWindow(shell, SW_SHOW)
        w = u.FindWindowExW(0, 0, 'WorkerW', None)
        while w:
            shell = u.FindWindowExW(w, 0, 'SHELLDLL_DefView', None)
            if shell:
                found = True
                if not u.IsWindowVisible(shell):
                    u.ShowWindow(shell, SW_SHOW)
            w = u.FindWindowExW(0, w, 'WorkerW', None)
        return found
    except Exception:
        return False


def run_watch(pid: int, interval: float = 1.0, _wait=None, _state=None,
              _show=None, _now=None) -> str:
    """哨兵主循环（依赖全部可注入，门禁据此离线断言）：
    返回退出原因 'unhidden'/'superseded'/'restored'/'main-already-dead'/
    'state-lost'/'timeout'。"""
    if _wait is None:
        import ctypes

        def _wait(p, ms):
            # 0=死亡 258=超时（活着）-1(0xFFFFFFFF)=句柄无效 → 按死处理
            h = ctypes.windll.kernel32.OpenProcess(0x00100000, False, p)
            if not h:
                return -1
            try:
                return ctypes.windll.kernel32.WaitForSingleObject(h, ms)
            finally:
                ctypes.windll.kernel32.CloseHandle(h)
    if _state is None:
        _state = read_state
    if _show is None:
        _show = _show_desktop
    if _now is None:
        _now = time.time
    start = _now()
    deadline = start + MAX_LIFETIME
    misses = 0
    _selflog('watch start target=%d interval=%.1fs' % (pid, interval))
    while True:
        st = _state()
        if st is None:
            # 读不到状态：连续 3 次才判"账本没了"收工（避开一次写盘竞态）
            misses += 1
            if misses >= 3:
                _selflog('state lost, exit')
                return 'state-lost'
        else:
            misses = 0
            if not st.get('hidden'):
                _selflog('already unhidden, exit')
                return 'unhidden'
            if int(st.get('pid', pid)) != int(pid):
                # 新一代主进程接管（重启/二开）——老哨兵让位
                _selflog('superseded by pid=%s' % st.get('pid'))
                return 'superseded'
        r = _wait(pid, int(interval * 1000))
        if r == 0 or r == -1:
            ok = _show()
            _selflog('main gone pid=%d show=%s' % (pid, ok))
            return 'restored' if ok else 'main-already-dead'
        if _now() > deadline:
            _selflog('lifetime cap, exit')
            return 'timeout'


def main(argv=None):
    """独立运行入口（源码态 pythonw 直跑 / 打包态由 launcher.pyw 顶部
    --desktop-sentinel 分支转进来）。"""
    argv = argv if argv is not None else sys.argv
    try:
        pid = int(argv[-1])
    except Exception:
        return 2
    run_watch(pid)
    return 0


if __name__ == '__main__':
    # 直跑本文件时 sys.path[0]=core/，补上项目根让 utils/core 包可 import
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    sys.exit(main())
