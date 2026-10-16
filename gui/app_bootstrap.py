# -*- coding: utf-8 -*-
"""🚀 应用引导（R149 自 gui/main_window.py 拆出，第七刀）。

进程级启动/退出集群：日志与崩溃诊断接线（app.log/switch.log/crash.log）、
单实例保护（命名互斥体+唤醒事件+clean_exit.flag）、DPI 感知声明（PMv2
逐级降级）、main()（系统版本兜底/_MEIPASS 资源落位/接力等待/DesktopManager
构造与主循环）。DesktopManager 留守 main_window（全部 Mixin 汇聚点），
此处**函数内局部导入**——模块双身份防护：本模块只被入口 import 一次。
纪律：launcher.pyw → gui.app_bootstrap.main()；不再直跑 main_window。
"""
import faulthandler   # ★ R162 审计补回：缺它 crash.log 静默失效
import json
import logging
import logging.handlers
import os           # ★ R162：stderr 重定向用
import sys
import threading    # ★ R162：线程异常钩子用
import time
from pathlib import Path

import ctypes

from utils.app_paths import get_app_dir as _get_app_dir

APP_DIR = _get_app_dir()   # ★ R149：APP_DIR 不是 utils.app_paths 直接导出（R140 教训）

logger = logging.getLogger("desktop_manager")

# ----------------------- 日志 / 崩溃诊断 -----------------------
# 全部异常落盘到 data/app.log：pythonw 下没有控制台，此前"自动退出"无迹可循；
# faulthandler 把原生崩溃（访问违例等）的线程栈写进 data/crash.log。

def _setup_diagnostics():
    """初始化日志/崩溃诊断。★★ P0-3（修正范围）★★：
    data/ 不可写（Program Files / 受限账户）时，任何 open(...,'a') 抛
    PermissionError。若此处不包 try，模块导入直接失败 → --gui 分支弹
    gui_fatal.log 退出，用户以为"程序启动失败"。故整个初始化进 try，
    失败降级到 OutputDebugStringW（pythonw 的 stderr 是黑洞），不拦截启动。
    注意：switch logger 的 handler 构造在更靠前的顶层（L120 附近），
    已单独包 try，见下方。"""
    try:
        log_dir = APP_DIR / 'data'
        log_dir.mkdir(exist_ok=True)
        # ★ 用安全轮转版：双实例/守护器并发写 app.log 时 rotate 会撞
        # WinError 32，普通版会报 Logging error 并丢日志（2026-09-02 修复）
        handler = _SafeRotatingHandler(
            log_dir / 'app.log', maxBytes=2 * 1024 * 1024,
            backupCount=2, encoding='utf-8')
        handler.setFormatter(logging.Formatter(
            '%(asctime)s [%(levelname)s] %(name)s: %(message)s'))
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        # ★ R98 幂等防御：同一文件只挂一个 handler。模块级代码若因双身份
        # 被执行两遍（历史事故：app.log/switch.log 每行翻倍），logger 是
        # 进程级单例，据此判重即可杜绝。
        _app_log = str(log_dir / 'app.log').lower()
        if not any(str(getattr(_h, 'baseFilename', '')).lower() == _app_log
                   for _h in root.handlers):
            root.addHandler(handler)

        # ★ R154：排障全量日志开关——用户报错要有证据。默认关（降噪）；
        # 排障时把 config/app_settings.json 加 "verbose_log": true 重启，
        # DEBUG 级明细（统计采集/诊断/FRAME 之外的周期行）全部落 app.log。
        try:
            _vs = json.loads((APP_DIR / 'config' / 'app_settings.json')
                             .read_text(encoding='utf-8'))
            if _vs.get('verbose_log'):
                root.setLevel(logging.DEBUG)
                handler.setLevel(logging.DEBUG)
                logger.info('[日志] verbose_log 开启：DEBUG 明细落盘')
        except Exception:
            pass
        try:
            crash_file = open(log_dir / 'crash.log', 'a', encoding='utf-8', buffering=1)
            faulthandler.enable(file=crash_file)
        except Exception:
            pass

        # ★ P2-N5：删 _global_excepthook 死代码（L108 被 _sys_hook 覆盖）
        # 全局异常钩子在下方 _sys_hook + _thread_hook 统一处理

        # pythonw 下 C 层 stderr（fd 2）会进黑洞：Tcl_Panic 等原生错误文本全丢，
        # 只剩 crash.log 里的 "Aborted"。无条件把 fd 2 重定向到文件，崩溃原因可见。
        # （注意不能以 sys.stderr is None 为条件：launcher 用 Popen 启动时 stderr
        #   可能是个无效句柄对象而非 None，会绕过重定向——实际踩坑）
        try:
            fd = os.open(str(log_dir / 'stderr.log'),
                         os.O_APPEND | os.O_CREAT | os.O_WRONLY)
            os.dup2(fd, 2)
            os.close(fd)
        except Exception:
            pass

        def _sys_hook(tp, val, tb):
            logger.error(f"未捕获异常: {tp.__name__}: {val}", exc_info=(tp, val, tb))
            # ★ R254（45 批 item2）崩溃路径黑桌面防线：「隐藏」藏的是 explorer
            # 的桌面视图窗，不随本进程崩溃复原。只 import 模块级函数调用
            # （不构造 IconManager——__init__ 有 mkdir/DPI 副作用，崩态禁碰）；
            # 整段 try 兜住，恢复失败绝不能再把异常抛进钩子。
            # （SIGKILL/native crash 到不了这里 ⇒ 兜底=下次启动自愈，见挂账）
            try:
                from core.icon_manager import ensure_desktop_view_visible
                ensure_desktop_view_visible()
            except Exception:
                pass

        def _thread_hook(args):
            logger.error(f"线程 {getattr(args.thread, 'name', '?')} 未捕获异常: "
                         f"{args.exc_type.__name__}: {args.exc_value}",
                         exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
            # ★ R254 同款：后台线程崩（如监视线程命中规则后动作抛异常）也不许
            # 把用户留在黑桌面上。函数幂等，重复调用零副作用。
            try:
                from core.icon_manager import ensure_desktop_view_visible
                ensure_desktop_view_visible()
            except Exception:
                pass

        sys.excepthook = _sys_hook
        threading.excepthook = _thread_hook
    except Exception:
        # data/ 不可写：不拦截启动。降级提示（stderr 是黑洞，用 OutputDebugStringW，
        # 调试器/ DebugView 可见；程序本身不受影响）。
        try:
            import ctypes
            ctypes.windll.kernel32.OutputDebugStringW(
                '[拾碎-懒人桌面工具箱] data/ 不可写，诊断日志降级到 OutputDebugString')
        except Exception:
            pass
        return


# ★R313 G2：这里原来另立一份同名兜底（`except (PermissionError, OSError): pass`），
# 与 utils/logger.py 那份是同一个病的两份拷贝——各自 `pass` 后谁也不知道轮转已经
# 停了、且每条记录都再撞一次失败改名（app.log 涨过上限）。现在两侧共用
# utils/log_rotator.py 那个正主（只依赖 stdlib，不拖累入口的 import 面）。
# 类名保留 `_SafeRotatingHandler`：v0.3.52 那次重构弄丢过这个定义、启动直接失败
# （2026-09-02 补回），留旧名让两处调用点与历史注释都对得上。
from utils.log_rotator import SafeRotator as _SafeRotatingHandler


# 分类切换专用日志：每个切换操作记录完整步骤，方便事后排查
# ★★ P0-3（修正范围）关键★★：switch handler 构造**先于** _setup_diagnostics()
# 执行（在 L131 之前）。_SafeRotatingHandler 内部 open(...,'a') 在 data/ 不可写
# 时抛 PermissionError——若不加 try，_setup_diagnostics 的 try 根本没机会执行，
# 模块照样导入失败。故把整个 switch logger 初始化包 try，失败时 handler 不挂
# 进 logger（propagate 到 root），并降级到 OutputDebugStringW，绝不拦截启动。
_switch_logger = logging.getLogger("switch")
try:
    _switch_handler = _SafeRotatingHandler(
        APP_DIR / 'data' / 'switch.log', maxBytes=512 * 1024,
        backupCount=3, encoding='utf-8')
    _switch_handler.setFormatter(logging.Formatter(
        '%(asctime)s | %(message)s', datefmt='%H:%M:%S'))
    # ★ R98 幂等防御（同 _setup_diagnostics）：按目标文件判重再挂
    _sw_log = str(APP_DIR / 'data' / 'switch.log').lower()
    if not any(str(getattr(_h, 'baseFilename', '')).lower() == _sw_log
               for _h in _switch_logger.handlers):
        _switch_logger.addHandler(_switch_handler)
    _switch_logger.setLevel(logging.INFO)
except Exception:
    # data/ 不可写：不拦截启动。switch logger 无 handler，日志走 root（仅当
    # _setup_diagnostics 成功才可写文件；均不可写则静默，程序照常运行）。
    try:
        import ctypes
        ctypes.windll.kernel32.OutputDebugStringW(
            '[拾碎-懒人桌面工具箱] switch.log 不可写，切换日志降级到 OutputDebugString')
    except Exception:
        pass


_setup_diagnostics()

# ---------------------------------------------------------------------------

# ----------------------- 单实例保护 -----------------------
# 重复双击"启动.bat"时不再开出第二个程序（历史上多实例并存会互相打架：
# 热键全部注册失败、多根横条叠加，且实测伴随 Tcl abort 硬崩溃）。
# 第二次启动 = 通过命名事件唤醒已有实例弹出主窗口，然后自己退出。

_SINGLETON_MUTEX = 'DesktopSuperManager_SingleInstance_Mutex'
_SINGLETON_SHOW_EVENT = 'DesktopSuperManager_ShowWindow_Event'


def _wait_previous_exited_for_relaunch(timeout_s: int = 60):
    """★ R79d：语言切换自动重启的接力等待（新进程带 --relaunch-wait 启动）。

    R79 旧实现在本进程里用非守护线程等"互斥体消失"再拉起新进程——但
    互斥体被本进程自己握着，本进程又要等该线程拉起新进程后才退出：
    结构性死等，直到 30 秒轮询放弃才启动（2026-09-16 用户实测"很久才
    启动，点了好多次"）。改为：旧进程立即拉起带 --relaunch-wait 的新
    进程，由新进程（独立于旧互斥体）等旧实例退出后继续正常启动。"""
    k32 = ctypes.windll.kernel32
    SYNCHRONIZE = 0x00100000
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        h = k32.OpenMutexW(SYNCHRONIZE, False, _SINGLETON_MUTEX)
        if not h:
            logger.info("[relaunch] 旧实例已退出，继续启动")
            return
        k32.CloseHandle(h)
        time.sleep(0.3)
    logger.warning("[relaunch] 等待旧实例退出超时，继续尝试启动")
    # ★ R150：超时不再无声——告知用户（继续尝试通常=唤醒被藏的旧窗）
    try:
        ctypes.windll.user32.MessageBoxW(
            0, '自动重启等待超时（旧实例未退出）。\n即将尝试继续启动；'
               '若窗口未出现，请手动启动程序。',
            '拾碎-懒人桌面工具箱', 0x40)
    except Exception:
        pass


def _acquire_single_instance():
    """返回 (互斥锁句柄, 是否首个实例)。非首个时通知已有实例显示主窗口。"""
    k32 = ctypes.windll.kernel32
    ERROR_ALREADY_EXISTS = 183
    EVENT_MODIFY_STATE = 0x0002   # 实测教训：写成 0x0010 会 OpenEvent 失败，
                                  # 第二次启动永远唤不醒已有实例（表现为"启动不了"）
    # ★ P0-1（上轮遗漏）：CreateMutexW 前必须 SetLastError(0)。
    # Win32 GetLastError 保留"上次调用的残留值"——CreateMutexW 创建成功时它
    # 不会自动清零；若残留 183（之前任何 API 留下的），首个实例会被误判为
    # "已有实例" → 唤醒不存在的兄弟 + 写 clean_exit.flag 退出 → 偶发"双击打不开"。
    # 通用准则：CreateMutexW / CreateEventW / OpenEventW / CreateWindowEx 任何
    # "判返回 + GetLastError" 的组合，调用前都要 SetLastError(0)。
    try:
        k32.SetLastError(0)
    except Exception:
        pass   # 个别受限环境下 SetLastError 不可用，CreateMutexW 仍照调（退化为旧逻辑）
    mutex = k32.CreateMutexW(None, False, _SINGLETON_MUTEX)
    err = k32.GetLastError()   # 创建后立刻取一次，后续统一用此值判断
    # ★ P0-2 + P1-N21：CreateMutexW 返回 0/None 表示创建失败（句柄无效）
    # 旧 P0-2 修复把失败当"非首个实例退出"，但受限环境下根本没实例在跑，
    # 会导致用户双击启动程序完全没反应。改为"放弃单实例保护继续运行"——
    # 多实例并存虽会打架（热键注册失败、多横条），但比"完全打不开"好。
    # 弹框提示用户知情，并记日志。
    if not mutex:
        logger.warning(f"CreateMutexW 失败（err={err}），放弃单实例保护继续运行")
        try:
            ctypes.windll.user32.MessageBoxW(
                0,
                '无法获取单实例锁（系统权限受限或命名对象被拦截），'
                '将以多实例模式运行。\n热键可能注册失败，建议关闭后重试。',
                '拾碎-懒人桌面工具箱', 0x40)
        except Exception:
            pass
        # 仍创建 event 让后续 SetEvent 路径不报错（虽无 mutex 保护）
        k32.CreateEventW(None, False, False, _SINGLETON_SHOW_EVENT)
        return None, True   # ← 关键：True 放行
    if err == ERROR_ALREADY_EXISTS:
        evt = k32.OpenEventW(EVENT_MODIFY_STATE, False, _SINGLETON_SHOW_EVENT)
        if evt:
            k32.SetEvent(evt)
            k32.CloseHandle(evt)
        else:
            logger.warning("唤醒已有实例失败（OpenEventW 返回空）")
        # ★ #10 修复：本实例是"重复启动"——唤醒已有实例后立刻退出（耗时 <1s）。
        #   必须留下退出标记，否则守护器会把这 0.1s 的退出误判为"启动异常"，
        #   每 2 秒重启一次；而每次重启又 SetEvent 一次唤醒已有实例
        #   → 已有实例的主窗口被反复弹到前台（实测循环 6 次，用户以为"关不掉"）。
        #   标记与"用户主动退出"共用同一文件：对守护器而言语义相同——
        #   "本次退出是用户意图，停止守护"。
        #   ★R207 NEW3-A1（P1）：统一走 write_clean_exit_flag（带 pid）——
        #   此处曾写裸时间戳，launcher 的 pid 校验认不出→唤醒退出被当
        #   启动异常→重启循环复活
        try:
            from utils.clean_exit import write_clean_exit_flag
            write_clean_exit_flag()
        except Exception:
            pass
        return None, False
    k32.CreateEventW(None, False, False, _SINGLETON_SHOW_EVENT)  # 自动重置
    return mutex, True


def _declare_dpi_awareness():
    """★ R65-Bug2 → R219：进程级 Per-Monitor-V2 DPI 感知（方框准确性根基）。

    此前进程未声明 DPI 感知，Windows 对所有坐标 API 做主屏 DPI 虚拟化：
    highlighter 采集图标矩形走线程级 PMv2（物理像素），overlay 定位却被
    虚拟化缩放——主程序拖到副屏（DPI 上下文变化）后主屏方框整体错位且
    不恢复（2026-09-15 用户实测）。PMv2 下全部坐标 API 直返物理像素，
    采集/定位口径一致；老系统无 PMv2 时逐级降级。必须在创建任何窗口
    （Tk 主窗/单实例互斥窗）之前调用。

    ★R219：实现挪进 core.dpi_geom 统一收口——感知档位先到先得且不可覆盖，
    任何模块（历史上 icon_manager 自己调过一次 SetProcessDPIAware）抢跑都会
    把进程永久钉死在低档位，125% 缩放下表现为"查找桌面图标框不准"。"""
    from core.dpi_geom import declare_process_dpi_awareness, effective_awareness
    declare_process_dpi_awareness(logger)
    logger.info(f"[DPI] 生效档位={effective_awareness()}")


# 随包只读资源：exe 解压目录 → exe 旁边，名字必须与打包脚本一致
BUNDLED_RESOURCES = ('images', 'version.txt', 'README.md')


def _needs_resync(src, dst):
    """单文件是否要从 src 刷到 dst：目标不存在 / 大小不同 / 内容不同。

    读取失败（被占用、权限）时保守返回 False——宁可这次不刷新，也不能
    因写资源把启动流程崩掉（本函数跑在任何窗口之前）。"""
    try:
        if not dst.exists():
            return True
        if dst.stat().st_size != src.stat().st_size:
            return True
        return dst.read_bytes() != src.read_bytes()
    except Exception:
        return False


def sync_bundled_resources(meipass, app_dir, names=BUNDLED_RESOURCES):
    """★R230（用户 13 批 item2）：把随包只读资源同步到 exe 旁，**内容变了要覆盖**。

    病根：打包版首次运行会把 exe 内嵌的 version.txt / README.md 复制到
    exe 目录（APP_DIR 引用统一生效），但旧代码条件是 `not dst.exists()`
    ——只补空、不刷新。用户按 使用说明.txt 的口径"用新 exe 覆盖旧 exe"
    升级后，外层那份旧的 README.md / version.txt 永久留存，于是
    ①关于页显示的还是上一个版本号，②「打开 README」点开的是旧说明
    （没有"🧰 系统/🔒 锁屏"等新功能小节）。这正是本批用户报的现象。

    ★R180/R181 把**解压缓存目录**按版本号分开，解决的是"内层读到上版
    解压内容"；外层这份副本从没管过，两级都要对齐才算真刷新。

    目录（images/）保持"缺才补 + 同名文件按内容差异覆盖"，★R266 起**连子目录一起
    递归覆盖**（老口径只扫顶层，新随包的子目录对老客户永远刷不出来），
    仍然不做删除式镜像，避免动到用户可能自行放入的文件。
    返回本次实际刷新的名字列表（便于测试与日志）。
    """
    import shutil
    synced = []
    meipass = Path(meipass)
    app_dir = Path(app_dir)
    for name in names:
        src = meipass / name
        dst = app_dir / name
        if not src.exists():
            continue
        try:
            if src.is_dir():
                if not dst.exists():
                    shutil.copytree(str(src), str(dst))
                    synced.append(name)
                    continue
                # ★R266（S6 独立验收打回 P1）：只遍历**顶层**文件 ⇒ 老客户 exe 旁
                # 早就有 images/（★R230 起带 qrcode_tip.png），新随包的**子目录**
                # images/app_icons 永远刷不出去 —— 覆盖升级之后图标卡整块没有、
                # 连默认那枚都丢（源码态测不出来，因为文件本来就躺在项目树里）。
                # 改成逐文件递归按相对路径刷；仍然**不做删除式镜像**，
                # 用户自己往 images/ 里放的东西一个不动。
                for f in sorted(src.rglob('*')):
                    if not f.is_file():
                        continue
                    rel = f.relative_to(src)
                    target = dst / rel
                    if not _needs_resync(f, target):
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(f), str(target))
                    if name not in synced:
                        synced.append(name)
            elif dst.is_dir():
                # ★R230b：源是文件而 exe 旁同名项是**目录**（用户手工建过同名目录）
                #   时绝不能 copy2 —— 那会把 README.md 写进 README.md/ 里面：
                #   外层"还是没有 README"的假象原样留着，还顺手把用户目录写脏。
                logger.warning(f"[资源] {name} 在 exe 旁是个目录，跳过刷新"
                               f"（请删掉或改名那个目录后重试）")
            elif _needs_resync(src, dst):
                shutil.copy2(str(src), str(dst))
                synced.append(name)
        except Exception as _e:
            # ★R230b：失败绝不拦启动（原行为），但必须留痕——刷新不动时用户看到的
            #   仍是旧 README（＝本批用户报的原症状），一行日志都没有就查不出来。
            logger.warning(f"[资源] 刷新 {name} 失败（忽略，不拦启动）: {_e}")
    return synced


def startup_modifier_sweep(argv=None):
    """★R230b：启动修饰键巡检的**唯一入口**（从 main() 里那段内联代码抽出来）。

    两件事是抽出来才做得对的：
    ① **`--selfcheck` 探针直接跳过**。上一世卡住的 Alt/Ctrl/Shift 是全系统持久态
      （★R229 取证 E3：400ms 不自愈），所以进程一起来就要巡检兜底；但 `is_down()`
      读的是 `GetAsyncKeyState & 0x8000`，**分不清"上一世卡住"和"用户正按着"**，
      每多跑一次就多一次打断用户按键的机会。自动测试里那条"真起主窗"的探针
      （★R216 特意绕过单实例守卫）本就要求"跳过一切真实副作用"——旧写法挂在
      守卫之前，等于每跑一次测试就往真键盘注一次 key-up。
    ② **调用点挪到单实例守卫之后**：重复启动（第二进程唤醒旧窗就 `sys.exit(0)`）
      不该改键盘态，它连窗口都没建，纯粹是"用户双击了两下"。
    返回本次清掉的键名列表（空表=系统态本来就干净 / 被豁免）。异常只告警不抛。
    """
    if argv is None:
        argv = sys.argv
    if '--selfcheck' in argv:
        logger.info('[selfcheck] 跳过启动按键巡检（探针不动真键盘）')
        return []
    try:
        from utils import keystate as _ks
        return list(_ks.sweep_on_startup())
    except Exception as _e:
        logger.warning(f"启动按键巡检失败（忽略）: {_e}")
        return []



def main():
    # ★ 系统兼容性兜底：Win10 以下不受支持（纯 Win32 弹窗，不依赖 Tk）
    try:
        _v = sys.getwindowsversion()
        if _v.build < 10240:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0,
                '本软件需要 Windows 10 或以上系统（检测到当前系统版本过低），'
                '即将退出。未对系统做任何修改。',
                '拾碎-懒人桌面工具箱', 0x40)
            sys.exit(0)
    except Exception:
        pass   # 检测本身失败不拦启动（正常系统都能读到版本）

    # ★ onefile 模式：把打包进 exe 的只读资源（images/、version.txt）
    #   从临时解压目录 _MEIPASS 复制到 exe 所在目录，让 APP_DIR 引用统一生效
    # ★ R174 C5：Nuitka 无 _MEIPASS（frozen 且缺 _MEIPASS 即 Nuitka），
    #   其只读资源在缓存解压目录 dirname(sys.executable)，同样复制
    # ★R230：改为 sync_bundled_resources——外层副本按内容差异覆盖，
    #   否则"新 exe 覆盖旧 exe"升级后 README/版本号永远停在上一版
    if getattr(sys, 'frozen', False):
        _meipass = getattr(sys, '_MEIPASS', None) or \
            str(Path(sys.executable).resolve().parent)
        if _meipass:
            _synced = sync_bundled_resources(_meipass, APP_DIR)
            if _synced:
                logger.info(f"[资源] 已随包刷新: {_synced}")

    logger.info("main() 进入")
    _declare_dpi_awareness()   # ★ R65-Bug2：须在任何窗口创建前
    # ★ R79d：语言切换自动重启的接力——新进程先等旧实例退出再继续，
    #   否则会被单实例保护当成"重复启动"（唤醒旧窗后自己退出）
    if '--relaunch-wait' in sys.argv:
        _wait_previous_exited_for_relaunch()
    # ★R216：--selfcheck 探针绕过单实例守卫。自动测试里唯一覆盖"真起主窗 +
    # 建全部页签"的那一项，在用户程序开着时会被守卫直接 sys.exit(0) ⇒ 永久
    # SKIP，UI 改动无从回归。探针模式本就跳过一切真实副作用（见 main_window
    # SELF_CHECK 注释），再抢一次互斥锁不影响用户实例，只是多开一个隐藏窗。
    if '--selfcheck' in sys.argv:
        logger.info('[selfcheck] 跳过单实例守卫（允许与用户实例并存）')
        mutex, first = None, True
    else:
        mutex, first = _acquire_single_instance()
    if not first:
        logger.info("检测到已有实例在运行，已通知其弹出主窗口，本实例退出")
        sys.exit(0)

    # ★R230（第 13 批）：修饰键巡检——上一进程若在"注入 Alt 还没抬起"时被强杀/
    #   砍掉，那份按下态是全系统持久的（★R229 取证 E3：不会自愈），用户表现为
    #   "记事本打不进字、退格自动删词"，下一次启动兜底清掉。
    # ★R230b（S6 验收 P2）：调用点从"资源同步之后"挪到**单实例守卫之后**——
    #   重复启动只唤醒旧窗、不该动全系统键盘态；--selfcheck 探针在函数内豁免。
    #   仍在任何窗口创建之前（DesktopManager 还没构造）。
    _stuck = startup_modifier_sweep(sys.argv)
    if _stuck:
        logger.info(f"[按键] 启动巡检已复位卡住的修饰键: {_stuck}")

    # ★ R149：DesktopManager 局部导入（第七刀反向依赖，进程单次）
    from gui.main_window import DesktopManager
    exit_code = 0
    try:
        app = DesktopManager()
        app.mainloop()
        logger.info("mainloop 正常返回（干净退出）")
    except Exception:
        logger.exception("主循环异常退出（守护进程将重启）")
        exit_code = 1
    sys.exit(exit_code)


if __name__ == '__main__':   # 直跑兼容（正式入口 launcher.pyw→gui.app_bootstrap）
    main()
