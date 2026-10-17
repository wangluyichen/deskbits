# -*- coding: utf-8 -*-
r"""DeskBits 自动测试（★ R176，用户 -20/07-1："不能光靠人工测试"）。

用法：
  python 自动测试.py            # 全量（含条件监视引擎实测，约 60 秒）
  python 自动测试.py --quick   # 跳过慢项（引擎实测），约 15 秒

覆盖：日志系统/下载撤回栈/条件监视引擎/一键维护三模式/计划任务解析/
事件查看器/锁屏图片加载/监视配置编辑启停。
每项断言功能返回值 + **app.log 日志证据**；报告落盘 自动测试报告_*.txt
（不进 git）。全过退出码 0，有失败退出码 1（bat/CI 可判定）。
"""
import io
import os
import shutil
import sys
import tempfile
import time
import tkinter as tk
import traceback
import zipfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

APP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP)
os.chdir(APP)

PY_EXE = sys.executable
QUICK = '--quick' in sys.argv
RESULTS = []
# ★R213（NEW5-A1）：用例返回以此前缀开头 = 本轮没跑（quick 模式等），
# 计 SKIP 不计 PASS
SKIP_PREFIX = '[跳过] '


def check(name):
    """装饰器：登记一项测试"""
    def deco(fn):
        RESULTS.append((name, fn))
        return fn
    return deco


def make_gui_stub(root, fake_after=False):
    """隐藏 Tk 的最小 gui 替身（面板函数只用到这些）。

    fake_after=True 时 root.after 不走 Tcl（无 mainloop 的测试环境里
    工作线程调 after 会 "main thread is not in main loop"），改入队，
    由测试主线程手动执行回调。"""
    g = SimpleNamespace()
    g.root = SimpleNamespace() if fake_after else root
    g._after_calls = []
    if fake_after:
        g.root.after = lambda ms, fn: g._after_calls.append(fn)
    # ★R207（GUIB-4/5 后台线程回投改 _ui_queue 后的配套）：替身也提供
    # _ui_queue，测试主线程与 _after_calls 一起泵干
    import queue as _q
    g._ui_queue = _q.Queue()
    g._app_settings = {}
    g._log_lines = []

    def _log(m):
        g._log_lines.append(str(m))
        try:
            logger.info(m)
        except Exception:
            pass
    g._log = _log
    return g


def _pump_ui(g):
    """★R207：测试主线程泵干替身的两路回投（after 队列 + _ui_queue）。
    ★R210（NEW4-R9）：回投回调的异常收集后重新抛出（此前吞掉=表现为
    超时假象），循环加上界防投递回调自打死循环。"""
    _errs = []
    for _ in range(200):
        try:
            cb = g._ui_queue.get_nowait()
        except Exception:
            break
        try:
            cb()
        except Exception as e:
            _errs.append(e)
    else:
        # ★R213（NEW5-A13）：循环上界耗尽后若队列仍有剩余，此前静默丢弃
        # ——用例"通过"可能只是没验到被丢的回调，必须炸
        if not g._ui_queue.empty():
            raise RuntimeError(
                '_pump_ui 泵 200 轮后队列仍有回调（自投递无穷链？需调上界）')
    if _errs:
        raise RuntimeError(f'_pump_ui 回调异常 x{len(_errs)}: {_errs[0]!r}')


# 延迟导入（保证环境自检先跑，缺依赖时报告可读）
logger = None


def main():
    global logger
    root = tk.Tk()
    root.withdraw()

    import logging
    logger = logging.getLogger('auto_test')
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        # ★R319 K22：控制台是 GBK 码页，报告里带 `⚡` 的那几句会被裸 handler 撕成
        # `--- Logging error ---` 堆栈（真机报告里四条）——换成装不下就替 `?` 的那一个。
        from utils.log_rotator import SafeConsoleHandler
        logger.addHandler(SafeConsoleHandler(sys.stdout))

    passed, failed, skipped = [], [], []
    report_lines = [f'DeskBits 自动测试报告  {datetime.now():%Y-%m-%d %H:%M:%S}',
                    f'模式: {"quick" if QUICK else "全量"}  Python: {sys.version.split()[0]}',
                    '=' * 60]
    for name, fn in RESULTS:
        t0 = time.time()
        try:
            detail = fn(root, logger) or ''
            # ★R213（NEW5-A1）：跳过项单独记账——此前计为 PASS，
            # quick 模式"16/16 全过"其实有 N 项根本没跑，掩盖过 t_monitor
            # 断言过期（=3 vs 实际 5）一整轮
            if detail.startswith(SKIP_PREFIX):
                skipped.append((name, detail[len(SKIP_PREFIX):]))
                line = f'[SKIP] {name}  ({time.time() - t0:.1f}s) {detail[len(SKIP_PREFIX):]}'
            else:
                passed.append(name)
                line = f'[PASS] {name}  ({time.time() - t0:.1f}s) {detail}'
        except Exception as e:
            failed.append((name, f'{e}'))
            line = (f'[FAIL] {name}  ({time.time() - t0:.1f}s)\n'
                    + traceback.format_exc(limit=4))
        report_lines.append(line)
        print(line)

    report_lines.append('=' * 60)
    report_lines.append(f'总计 {len(passed) + len(failed) + len(skipped)} 项：'
                        f'PASS {len(passed)} / SKIP {len(skipped)} / '
                        f'FAIL {len(failed)}')
    for n, e in failed:
        report_lines.append(f'  失败项: {n} → {e[:120]}')
    for n, why in skipped:
        report_lines.append(f'  跳过项: {n} → {why[:120]}')
    verdict = 'PASS' if not failed else 'FAIL'
    report_lines.append(f'总体判定: {verdict}')
    out = Path(APP) / f'自动测试报告_{datetime.now():%Y%m%d_%H%M%S}.txt'
    out.write_text('\n'.join(report_lines), encoding='utf-8')
    print(f'\n报告已写入: {out.name}')
    root.destroy()
    return 0 if not failed else 1


# ---------------------------------------------------------------------
# 1 环境自检
# ---------------------------------------------------------------------
@check('环境自检（依赖导入）')
def t_env(root, lg):
    import PIL.Image           # noqa: F401
    import PIL.ImageTk         # noqa: F401  Tk9 兼容实测过
    import win32api            # noqa: F401
    import win32evtlog         # noqa: F401
    import pynput              # noqa: F401
    import ctypes
    ctypes.windll.shell32.SHEmptyRecycleBinW   # 回收站 API 在位
    return 'PIL/win32/evtlog/pynput/SHEmptyRecycleBinW 全部在位'


@check('日志系统写入读回')
def t_log(root, lg):
    from utils import logger as ulog
    import logging as _lg
    from utils.logger import setup_logger, DEBUG_LOG
    test_lg = setup_logger('auto_test_probe', DEBUG_LOG, level=_lg.DEBUG,
                           file_level=_lg.INFO)
    marker = f'AUTOTEST_{int(time.time())}'
    test_lg.info(marker)
    time.sleep(0.3)
    txt = Path(DEBUG_LOG).read_text(encoding='utf-8', errors='replace')
    assert marker in txt, '标记未落盘'
    return f'标记 {marker} 已落 {Path(DEBUG_LOG).name}'


@check('真启动主窗：进程 8 秒存活（页签构建无异常）')
def t_startup(root, lg):
    """★R212b（20 批 P0 盲区补课）：此前 15 项全是组件级替身，真启动
    主窗零覆盖——页签布局改动把构建搞崩（Tk before= 引用未 pack）时
    自动测试照样全绿。本用例真拉起主窗：崩溃退出（约 3s）即 FAIL。
    ★R213（NEW5-A2）：带 --selfcheck 拉起——main_window 在该模式下照常
    建窗建页签（被测面），但跳过对用户机器的真实副作用（监视线程/
    蒙版/全局热键/注册表守卫清扫/隐藏图标恢复）。
    ★R213（NEW5-A3）：收尸不裸 /F——先 WM_CLOSE 优雅退（走 _on_close
    正常清理），超时/仍存活再 /F 升级，最后 poll() 复查确认死透；
    杀不掉=探针进程泄漏在用户机上跑，绝不静默记 PASS。"""
    import subprocess as _sp
    flag = Path('data/clean_exit.flag')
    if flag.exists():
        flag.unlink()
    # ★R213（NEW5-A11）：pythonw 替换在无 pythonw 的环境（CI/嵌入式）
    # 会得到不存在的路径，Popen 直接 FileNotFoundError——回退 sys.executable
    pyw = sys.executable.replace('python.exe', 'pythonw.exe')
    if not Path(pyw).exists():
        pyw = sys.executable
    proc = _sp.Popen([pyw, 'gui/main_window.py', '--selfcheck'],
                     cwd=str(Path(__file__).resolve().parent),
                     creationflags=0x08000000)

    def _skip_with_history(why):
        """连续 SKIP 甄别：读最近一份报告，若上一轮 t_startup 也跳过，
        升级提示——防"永远跳过永远绿"（SKIP 合法但不该长期没人看）。"""
        prev_skipped = False
        try:
            reps = sorted(Path(APP).glob('自动测试报告_*.txt'))
            if reps:
                prev_skipped = '跳过项: 真启动主窗' in reps[-1].read_text(
                    encoding='utf-8', errors='replace')
        except OSError:
            pass
        return SKIP_PREFIX + why + ('（⚠ 上轮也已跳过——真启动面连续两轮'
                                    '未覆盖，请关闭在用实例后重跑）'
                                    if prev_skipped else '')

    try:
        time.sleep(8)
        alive = proc.poll() is None
        if alive:
            # 按 PID 精确结束（纪律：严禁 taskkill python.exe 按名清场）
            # ① 优雅：无 /F 的 taskkill 向该进程窗口发 WM_CLOSE
            _sp.run(['taskkill', '/PID', str(proc.pid)],
                    capture_output=True)
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            if proc.poll() is None:
                # ② 升级：仍活着（如"关窗保留横条"设置拦住了 WM_CLOSE）
                # 才强杀整棵子进程树
                _sp.run(['taskkill', '/PID', str(proc.pid), '/F', '/T'],
                        capture_output=True)
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
            # ③ 复查：两轮后仍存活=进程泄漏，必须炸出来
            if proc.poll() is None:
                raise AssertionError(
                    f'探针主窗进程 pid={proc.pid} 两轮收尸（WM_CLOSE→/F）'
                    '后仍存活——已泄漏在后台，请手动结束后排查')
            return '主窗拉起后 8 秒存活（selfcheck 模式），全部页签构建无异常'
        # ★R212c 修正：秒退≠必崩——若机器上已有实例在跑（或上一个探针刚被
        # 杀、互斥体未及释放），新实例会"唤醒旧窗即退出"（rc=0+写 flag）。
        # 与构建期崩溃（rc=1）区分，前者跳过不误报。
        if proc.returncode == 0 and flag.exists():
            flag.unlink()
            return _skip_with_history('已有实例在运行（唤醒退出），真启动检查本轮未跑')
        raise AssertionError(
            f'主窗 8 秒内退出（构建期崩溃，rc={proc.returncode}），'
            '详见 data/app.log')
    finally:
        # 异常路径（含 8s 存活判定前的意外）兜底：确保不留活口
        if proc.poll() is None:
            _sp.run(['taskkill', '/PID', str(proc.pid), '/F', '/T'],
                    capture_output=True)


# ---------------------------------------------------------------------
# 2 下载记录引擎（临时目录，不碰真实数据）
# ---------------------------------------------------------------------
@check('下载记录：撤回栈持久化+回插+水位线')
def t_download(root, lg):
    from core.download_tracker import DownloadTracker
    tmp = Path(tempfile.mkdtemp(prefix='deskbits_at_'))
    try:
        f = tmp / 'a.txt'
        f.write_text('x', encoding='utf-8')
        tr = DownloadTracker(tmp, log=lambda m: None)
        rec = {'name': 'a.txt', 'path': str(f), 'dir': str(tmp),
               'size': 1, 'ts': '2026-09-20T09:00:00', 'status': 'done'}
        tr.records = [dict(rec)]
        tr.push_undo(rec)
        # ★ R177 黑名单：忽略后同步层拒收，撤回解除
        tr.forget(str(f))
        assert tr._is_forgotten(str(f)), 'forget 未生效'
        assert tr.add_or_update(f, src='chrome') is False, '黑名单没拦同步'
        tr.remove(str(f))
        # 重启等价：新实例从盘上撤回栈取回并回插（R176 修：原先先在本
        # 实例 pop 掉再开新实例，消费顺序错了，测的是假问题）
        tr2 = DownloadTracker(tmp, log=lambda m: None)
        assert tr2._is_forgotten(str(f)), '黑名单未持久化'
        back2 = tr2.pop_undo()
        assert back2 and back2['path'] == str(f), '撤回栈未持久化'
        tr2.restore_record(back2)
        assert any(r['path'] == str(f) for r in tr2.records), '回插失败'
        assert not tr2._is_forgotten(str(f)), '撤回未解除黑名单'
        tr3 = DownloadTracker(tmp, log=lambda m: None)
        tr3.clear()
        assert tr3.add_or_update(f, ts='2026-09-19T08:00:00') is False, \
            '水位线没拦旧下载'
        return '持久化撤回+黑名单+回插+水位线 7 项断言过'
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------
# 3 条件监视引擎（真实进程实测；--quick 跳过）
# ---------------------------------------------------------------------
@check('条件监视引擎：进程消失→命令执行（真实进程）')
def t_monitor(root, lg):
    if QUICK:
        return SKIP_PREFIX + 'quick 模式不跑真实 notepad 实测'
    import subprocess as sp
    from core.condition_monitor import ConditionMonitor
    tmp = Path(tempfile.mkdtemp(prefix='deskbits_mon_'))
    hit = tmp / 'hit.txt'

    def _np_alive():
        return bool(_np_pids())

    def _np_pids():
        # ★R213（NEW5-A14）：改"存活判断/收尸"都按 PID 集合，不再
        # taskkill /IM notepad.exe——那会连带杀掉用户自己开着未存盘的
        # 记事本（数据丢失面）。基线为空（下方 assert），故测试期间出现
        # 的 notepad.exe 全归因于本用例自拉起的那一个。
        q = sp.run(['tasklist', '/FI', 'IMAGENAME eq notepad.exe', '/NH',
                    '/FO', 'CSV'], capture_output=True, creationflags=0x08000000)
        pids = set()
        for ln in q.stdout.decode('gbk', 'replace').splitlines():
            cols = [c.strip('"') for c in ln.split('","')]
            if len(cols) >= 2 and cols[0].lower() == 'notepad.exe':
                try:
                    pids.add(int(cols[1]))
                except ValueError:
                    pass
        return pids

    try:
        mon = ConditionMonitor(tmp, log=lambda m: None)
        mon.add('AT-消失测试', 'notepad.exe', 'gone', 'command',
                f'cmd /c echo hit>{hit}')
        mon.start()
        assert not _np_alive(), '测试前置：已有 notepad 在跑会干扰基线'
        proc = sp.Popen(['notepad'])         # 弹出后马上会被关闭（无害）
        dl = time.time() + 15
        while time.time() < dl and not _np_alive():
            time.sleep(1)
        assert _np_alive(), 'notepad 未能启动（基线建不起来）'
        time.sleep(6)
        # ★ R177 加固原用 /IM（Store 版 stub PID 先退，/PID 杀不到真身
        # → flaky）。★R213（NEW5-A14）：基线空前提下把"当前全部 notepad
        # PID"逐个 /PID/F 杀，既躲 stub 竞态又不伤用户记事本
        for _pid in sorted(_np_pids()):
            sp.run(['taskkill', '/PID', str(_pid), '/F'],
                   capture_output=True, creationflags=0x08000000)
        time.sleep(1)
        assert not _np_alive(), 'notepad 未被杀掉（无法进入消失态）'
        deadline = time.time() + 40
        while time.time() < deadline and not hit.exists():
            time.sleep(2)
        assert hit.exists(), '进程消失未触发命令（40 秒内无命中）'
        mon2 = ConditionMonitor(tmp, log=lambda m: None)
        # ★R213（NEW5-A1）：R185 起种子是 4 条，旧断言"播种2+测试1=3"必炸
        #（上轮 16/16 靠 --quick 跳过本用例掩盖）
        assert len(mon2.list()) == 5, \
            f'配置未落盘（应为播种4+测试1={5}，实际 {len(mon2.list())}）'
        mon.stop()
        try:
            proc.kill()
        except Exception:
            pass
        return 'notepad 消失→命令执行命中，配置回读 OK'
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@check('条件监视：编辑/启停/播种')
def t_monitor_cfg(root, lg):
    from core.condition_monitor import ConditionMonitor
    tmp = Path(tempfile.mkdtemp(prefix='deskbits_mon2_'))
    try:
        mon = ConditionMonitor(tmp, log=lambda m: None)
        ex = mon.list()
        # ★ R185：种子扩为 4 条（2 示例 + 2 实用）
        assert len(ex) == 4, f'空配置未播种 4 个示例（实际 {len(ex)}）'
        assert all(not m.get('enabled', True) for m in ex), '示例应默认停用'
        assert any(m['name'].startswith('实用·') for m in ex), \
            '缺"实用·"系列示例'
        mon.set_enabled(ex[0]['name'], True)
        assert mon.list()[0]['enabled'] is True, '启用失败'
        mon.update(ex[0]['name'], process='x2.exe')
        assert mon.list()[0]['process'] == 'x2.exe', '编辑失败'
        return '播种4条(停用)/启停/编辑 4 项断言过'
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------
# 4 一键维护链路（_qa_run 三模式 + 日志证据；不真清回收站）
# ---------------------------------------------------------------------
@check('一键维护：wait 模式执行+app.log 证据')
def t_qa_wait(root, lg):
    import gui.sys_panel as sp
    from utils.logger import DEBUG_LOG
    # 猴补 messagebox：记录调用不弹窗（自动化无人值守）
    real_mb = sp.messagebox
    sp.messagebox = SimpleNamespace(
        showinfo=lambda *a, **k: _MB.append(('info', a)),
        showwarning=lambda *a, **k: _MB.append(('warn', a)),
        askyesno=lambda *a, **k: True)
    _MB = []
    try:
        g = make_gui_stub(root, fake_after=True)
        btn = SimpleNamespace(configure=lambda **k: None)
        marker = f'AT_QA_{int(time.time())}'
        # app.log 的 handler 属于运行中的应用；测试进程里给
        # desktop_manager logger 挂临时文件 handler，验证"日志代码执行"
        import logging as _lg
        tmplog = Path(tempfile.mktemp(prefix='at_qa_', suffix='.log'))
        h = _lg.FileHandler(tmplog, encoding='utf-8')
        lg_dm = _lg.getLogger('desktop_manager')
        lg_dm.addHandler(h)
        lg_dm.setLevel(_lg.DEBUG)
        sp._qa_run(g, marker, 'cmd /c exit 0', btn, 'wait')
        deadline = time.time() + 20
        while time.time() < deadline:
            while g._after_calls:            # 主线程执行 after 回调
                g._after_calls.pop(0)()
            _pump_ui(g)                      # ★R207：_ui_queue 一起泵
            if any('完成' in m for m in g._log_lines):
                break
            time.sleep(0.5)
        h.flush()
        assert any('完成' in m for m in g._log_lines), '完成状态未回报'
        txt = tmplog.read_text(encoding='utf-8', errors='replace')
        assert f'一键维护[{marker}]' in txt, \
            f'文件日志无一键维护记录: {txt[:200]}'
        assert _MB and _MB[-1][0] == 'info', \
            f'成功未弹提示 _MB={_MB!r}'
        return '执行+GUI 日志+文件日志+弹窗 四证据齐'
    finally:
        sp.messagebox = real_mb
        try:
            lg_dm.removeHandler(h)
            h.close()
        except Exception:
            pass


@check('一键维护：失败检测+警告弹窗')
def t_qa_fail(root, lg):
    import gui.sys_panel as sp
    real_mb = sp.messagebox
    _MB = []
    sp.messagebox = SimpleNamespace(
        showinfo=lambda *a, **k: _MB.append(('info', a)),
        showwarning=lambda *a, **k: _MB.append(('warn', a)),
        askyesno=lambda *a, **k: True)
    try:
        g = make_gui_stub(root, fake_after=True)
        btn = SimpleNamespace(configure=lambda **k: None)
        # 注意名字不能含"失败"二字（会匹配"执行中"判定恒真，测试自坑）
        sp._qa_run(g, 'AT_QA_FAIL_EXIT3', 'cmd /c exit 3', btn, 'wait')
        deadline = time.time() + 20
        while time.time() < deadline:
            while g._after_calls:
                g._after_calls.pop(0)()
            _pump_ui(g)                      # ★R207：_ui_queue 一起泵
            if any('失败 ✗' in m for m in g._log_lines):
                break
            time.sleep(0.5)
        assert any('失败 ✗' in m for m in g._log_lines), '失败未回报'
        assert _MB and _MB[-1][0] == 'warn', \
            f'失败未弹警告 _MB={_MB!r} logs={g._log_lines!r}'
        return 'rc=3 → 失败回报+警告弹窗'
    finally:
        sp.messagebox = real_mb


@check('一键维护：py 模式（回收站计数只读不清）')
def t_qa_py(root, lg):
    # 只验证"数回收站条目"这一只读环节可用；真实清空不自动跑
    # （防误清用户回收站），真实清空已由人工+日志双重验证过
    import subprocess as _sp
    ps = ("$s=New-Object -ComObject Shell.Application;"
          "$b=$s.NameSpace(0xA);@($b.Items()).Count")
    p = _sp.run(['powershell', '-NoProfile', '-Command', ps],
                capture_output=True, timeout=30, creationflags=0x08000000)
    n = int(p.stdout.decode('gbk', 'replace').strip() or '0')
    return f'回收站当前 {n} 项（只读）'


# ---------------------------------------------------------------------
# 5 计划任务列表（真实 schtasks 端到端）
# ---------------------------------------------------------------------
@check('计划任务列表：解析+动作+备注带程序名')
def t_tasks(root, lg):
    import gui.sys_panel as sp
    g = make_gui_stub(root, fake_after=True)
    tree = ttk_tree(root, ('name', 'action', 'memo', 'created', 'next',
                           'status', 'last'))
    # ★R241：装具的备注列给 260px（产品里 memo 是 170 起 + stretch=True）。
    #   ttk_tree 默认每列 60px，而 R241 起"列宽放不下就保头加…"会真去
    #   省略——60px 下 `登录时 打开软件·Qoder` 会被铺成 `登录时 打开软件…`，
    #   下面那条"备注含·（R176）"判据就会假红。装具要贴产品，不改判据。
    tree.column('memo', width=260)
    sp._load_tasks(g, tree)
    prev = -1
    for _ in range(30):
        while g._after_calls:                # 主线程执行 _apply
            g._after_calls.pop(0)()
        _pump_ui(g)
        n = len(tree.get_children())
        if n > 0 and n == prev:
            break
        prev = n
        time.sleep(2)
    rows = [tree.item(i, 'values') for i in tree.get_children()]
    assert len(rows) > 5, f'任务列表行数异常: {len(rows)}'
    # ★R213（NEW5-A12）：取列前校验列数——生产加减列时这里给可读断言，
    # 而非 r[6] 的 IndexError
    # ★R247（用户 39 批 item2）：新增「电池下不启动」列 ⇒ 7 列改判成 8 列。
    #   列序 name/action/memo/created/next/status/last/bat，r[6] 仍是"上次结果"。
    assert all(len(r) == 8 for r in rows), \
        f'任务列表列数异常（应 8 列）: {[len(r) for r in rows][:3]!r}'
    memos = [str(r[2]) for r in rows]
    assert any('·' in m for m in memos), '备注无程序名（R176 新功能）'
    # ★R210（NEW4-R9）：新增"上次结果"列零断言的补课——DeskBits 任务
    # 的 last 值应为"正常"或数字/状态短语，不允许出现列错位串味
    lasts = [str(r[6]) for r in rows]
    # ★R213（NEW5-A12）：值域放宽纳入有符号错误码（-2147018820 这类
    # 首字符非数字），此前 v[:1].isdigit() 会把它误判成列错位——本就是
    # 合法的"上次结果"错误码。仍拦截真正的串味（汉字短语外的错位）。
    assert all(v == '' or v == '正常' or v[:1].isdigit() or v[:2] == '-1'
               or '运行' in v or '已' in v
               for v in lasts), f'上次结果列异常: {set(lasts)!r}'
    # ★R247：电池列零断言的补课——只许出现三种短语（读不到时留空），
    #   出现别的值就说明 PS 侧字段/解析侧下标没同步（整列错位）。
    bats = [str(r[7]) for r in rows]
    assert all(v in ('', '⚠ 电池下不启动', '插不插都运行', '—')
               for v in bats), f'电池列值异常: {set(bats)!r}'
    return f'{len(rows)} 行；备注样例: {memos[0][:40]}'


# ---------------------------------------------------------------------
# 6 事件查看器（真实 System 日志只读）
# ---------------------------------------------------------------------
@check('事件查看器：System 日志读取+大白话映射')
def t_events(root, lg):
    import gui.sys_panel as sp
    g = make_gui_stub(root)
    g._evt_filter = {}
    g._evt_id_only = None
    g._evt_status = None
    tree = ttk_tree(root, ('time', 'what', 'src', 'id'))
    sp._load_events(g, tree)
    root.update()
    rows = [tree.item(i, 'values') for i in tree.get_children()]
    assert rows, '事件列表为空'
    return f'{len(rows)} 条关键事件'


@check('事件查看器：全部典型/更多预设自动逐个点击')
def t_event_presets(root, lg):
    """★ R188f（用户 08 批"手动检查太累了，能不能自动点"）：
    模拟点击 全部/开关机/异常崩溃/服务故障/磁盘 + 更多下拉全部条目，
    断言：原始数据里匹配到的行，预设后必须显示出来（不再被分类勾选
    误杀成空列表——域名解析 25 条只出占位行的事故）。"""
    import gui.sys_panel as sp
    g = make_gui_stub(root)
    g._evt_filter = {'boot': tk.BooleanVar(value=True),
                     'bad': tk.BooleanVar(value=True),
                     'other': tk.BooleanVar(value=True)}
    g._evt_id_only = None
    g._evt_preset_name = ''
    g._evt_status = tk.StringVar()
    tree = ttk_tree(root, ('time', 'what', 'src', 'id'))
    sp._load_events(g, tree)
    total = len(g._evt_rows)
    assert total > 0, '未扫描到事件'
    PRESETS = {
        '全部': (('boot', 'bad', 'other'), None),
        '开关机': (('boot',), None),
        '异常崩溃': (('bad',), None),
        '服务故障': (('bad',), {7031, 7034, 7026, 7040, 7011, 7022,
                                7023, 7024}),
        '磁盘': (('bad',), {7, 51, 55, 129, 153, 157}),
        '域名解析': (('bad',), {1014, 1015}),
        '驱动': (('bad',), {219, 4101}),
        '时间同步': (('bad',), {1}),
        'Windows更新': (('bad',), {19, 20, 43}),
        '网络': (('bad',), {4201, 4202}),
    }
    results = []
    for name, (cats, ids) in PRESETS.items():
        for k, v in g._evt_filter.items():
            v.set(k in cats)
        g._evt_id_only = ids
        sp._load_events(g, tree)
        shown_ids = set()
        for iid in tree.get_children():
            v = str(tree.set(iid, 'id') or '').strip()
            if v.isdigit():
                shown_ids.add(int(v))
        raw = sum(1 for r in g._evt_rows
                  if ids is None or r['eid'] in set(ids))
        # 断言1（08 批事故断链点）：原始有匹配就不得显示为空
        if raw > 0:
            assert shown_ids, f'{name}: 原始 {raw} 条却显示空（过滤断链）'
        # 断言2：显示的必须都是预设内 ID
        if ids is not None:
            bad = shown_ids - set(ids)
            assert not bad, f'{name}: 混入预设外 ID {sorted(bad)[:5]}'
        results.append(f'{name}:{len(shown_ids)}')
    # 还原默认
    g._evt_id_only = None
    sp._load_events(g, tree)
    return ' '.join(results)


# ---------------------------------------------------------------------
# 7 锁屏图片加载（★R282-8：优先内置壁纸，回退 config 自定义；R176 修复验证）
# ---------------------------------------------------------------------
@check('锁屏图片：_load_image 不再炸 wins')
def t_lockimg(root, lg):
    from gui.lock_screen import LockScreen, resolve_lock_image
    from utils.app_paths import get_app_dir
    # ★R282-8：内置壁纸是主路径；config 自定义图是回退
    path = resolve_lock_image({'lock_preset': 'dracula'})
    if not path:
        for p in Path(get_app_dir() / 'config').glob('lock_screen_image.*'):
            path = str(p)
            break
    assert path, '内置壁纸与 config 自定义图均缺失'
    ls = object.__new__(LockScreen)      # 跳过完整构造
    ls.gui = make_gui_stub(root)
    ls._photo = None
    ph = ls._load_image(path)
    assert ph is not None, '图片加载返回 None（降级🔒）'
    return f'加载成功: {Path(path).name}'


# ---------------------------------------------------------------------
# 8 桌面图标备份/恢复离线回归（★R204 NEW-R15：R196 口径改动零覆盖）
# ---------------------------------------------------------------------
@check('备份恢复：文件夹内 zip 必须回来（NEW-R2 回归）')
def t_bk_inner_zip(root, lg):
    import desktop_icon_backup as dib
    tmp = Path(tempfile.mkdtemp(prefix='deskbits_bk_'))
    try:
        desk = tmp / 'desktop'; desk.mkdir()
        folder = desk / 'W工具'
        folder.mkdir()
        (folder / 'inner.zip').write_bytes(
            b'PK\x05\x06' + b'\x00' * 18)   # 空 zip 魔数
        (folder / 'readme.txt').write_text('hi', encoding='utf-8')
        zp = tmp / 'bak.zip'
        r = dib.export_desktop_icons(str(zp), dirs=[str(desk)])
        assert r['success'], f"导出失败: {r.get('error')}"
        r2 = dib.restore_desktop_icons(str(zp), str(tmp / 'rd'), overwrite=True)
        assert r2['success'], f"恢复失败: {r2.get('error')}"
        assert (tmp / 'rd' / 'W工具' / 'inner.zip').exists(),             '文件夹内 zip 未恢复（NEW-R2 回归复发：根层 .zip 排除误伤文件夹成员）'
        assert (tmp / 'rd' / 'W工具' / 'readme.txt').exists()
        return '文件夹内 zip/readme 均恢复'
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@check('备份恢复：超大单成员被闸门拦下（ROOT-7/NEW-A4 回归）')
def t_bk_big_member(root, lg):
    import desktop_icon_backup as dib
    tmp = Path(tempfile.mkdtemp(prefix='deskbits_bk2_'))
    try:
        desk = tmp / 'desktop'; desk.mkdir()
        big = desk / 'big.bin'
        # 稀疏写 101MB（> MAX_MEMBER=100MB），实际占盘小
        with open(big, 'wb') as f:
            f.seek(dib.MAX_MEMBER + 1024 * 1024)
            f.write(b'\0')
        zp = tmp / 'big.zip'
        with zipfile.ZipFile(zp, 'w', zipfile.ZIP_DEFLATED) as z:
            z.write(big, 'big.bin')
        r = dib.restore_desktop_icons(str(zp), str(tmp / 'rd'), overwrite=True)
        assert r['success'], '恢复流程不应失败'
        assert r['restored'] == 0 and r['skipped'] >= 1,             f"超大成员没被跳过: {r}"
        assert not (tmp / 'rd' / 'big.bin').exists(), '超大成员被写出桌面'
        return f"超大成员已拦（skipped={r['skipped']}）"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@check('备份恢复：zip-slip ../evil 不落盘（三道闸回归）')
def t_bk_zipslip(root, lg):
    import desktop_icon_backup as dib
    tmp = Path(tempfile.mkdtemp(prefix='deskbits_bk3_'))
    try:
        zp = tmp / 'evil.zip'
        with zipfile.ZipFile(zp, 'w') as z:
            z.writestr('../evil.txt', 'bad')
            z.writestr('W工具/../../escape.txt', 'bad')
        outside = tmp.parent / ('evil_check_' + str(int(time.time())))
        r = dib.restore_desktop_icons(str(zp), str(tmp / 'rd'), overwrite=True)
        assert r['success'], '流程不应失败'
        assert r['restored'] == 0, f"zip-slip 成员被写出: {r}"
        # ★R207 NEW3-R5：全树扫描泄漏名（不再只盯两个固定落点）
        leaked = [str(p) for p in tmp.rglob('*')
                  if p.name in ('evil.txt', 'escape.txt')]
        assert not leaked, f'zip-slip 泄漏: {leaked}'
        shutil.rmtree(outside, ignore_errors=True)
        return '路径穿越成员全部拦截'
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@check('守护 flag 判据矩阵：五条判据 14 格离线穷举（NEW5-A10 回归）')
def t_flag_matrix(root, lg):
    # launcher 判定链已纯函数化（_exit_says_user），无需真实双进程/文件——
    # 全矩阵直接喂参数断言。窗口固定 [999,1008]（start=1000, now=1006）
    from importlib.machinery import SourceFileLoader
    lz = SourceFileLoader('launcher_at',
                          os.path.join(APP, 'launcher.pyw')).load_module()
    IN, OUT = 1004.0, 900.0
    # (说明, txt, mtime, code, elapsed, used_in, 期望decision, 期望used_out,
    #  start, now)
    CASES = [
        ('①pid匹配+code=0', 'pid=111', OUT, 0, 1.0, False, 'stop', False, None),
        ('①pid匹配+崩溃code=1（最强判据不看退码）', 'pid=111', OUT, 1, 1.0, False, 'stop', False, None),
        ('③无pid+窗内+code=0', 'x', IN, 0, 1.0, False, 'stop', False, None),
        ('③无pid+窗内+崩溃code=1（写flag后destroy前崩）', 'x', IN, 1, 1.0, False, 'stop', False, None),
        ('③无pid+窗外残留+跑够久→忽略后⑤兜底停', 'x', OUT, 0, 20.0, False, 'stop', False, None),
        ('③无pid+窗外残留+崩溃→必须重启', 'x', OUT, 1, 20.0, False, 'restart', False, None),
        ('②pid不匹配+窗内+code=0+配额可用（onefile内层）', 'pid=222', IN, 0, 1.0, False, 'stop', True, None),
        ('②同上但A5配额已用尽→restart', 'pid=222', IN, 0, 1.0, True, 'restart', True, None),
        ('④pid不匹配+窗内+code=1→不能压掉崩溃重启', 'pid=222', IN, 1, 20.0, False, 'restart', False, None),
        ('④pid不匹配+窗外+干净退→忽略后⑤兜底停', 'pid=222', OUT, 0, 20.0, False, 'stop', False, None),
        ('⑤无flag干净退出', None, None, 0, 20.0, False, 'stop', False, None),
        ('⑤无flag快速退出（唤醒/启动异常）', None, None, 0, 1.0, False, 'restart', False, None),
        ('⑤无flag崩溃', None, None, 3, 20.0, False, 'restart', False, None),
        ('A6时钟回拨：now<start 端点归一化，无pid窗内判据仍命中', 'x', 1003.0, 0, 1.0, False, 'stop', False, (1006.0, 1000.0)),
    ]
    bad = []
    for (desc, txt, mt, code, el, used_in, want, want_used, clock) in CASES:
        start, now = clock if clock else (1000.0, 1006.0)
        dec, used_out, why = lz._exit_says_user(txt, mt, 111, code,
                                                start, now, el, used_in)
        if dec != want:
            bad.append(f'{desc}: 判出 {dec}≠期望 {want}（{why}）')
        elif not isinstance(why, str) or not why:
            bad.append(f'{desc}: why 为空')
        elif used_out != want_used:
            bad.append(f'{desc}: 配额位 {used_out}≠期望 {want_used}')
    assert not bad, '；'.join(bad)
    return f'{len(CASES)} 格全合（①~⑤+A5 配额+A6 回拨归一化）'


# ---------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------
def ttk_tree(root, cols):
    from tkinter import ttk
    t = ttk.Treeview(root, columns=cols, show='headings')
    for c in cols:
        t.heading(c, text=c)
        t.column(c, width=60)
    return t


if __name__ == '__main__':
    sys.exit(main())
