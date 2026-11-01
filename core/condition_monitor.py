# -*- coding: utf-8 -*-
"""👀 条件监视引擎（R166，用户需求："监视我的虚拟机，如果它关机，然后我
可以选择关闭主机"等——进程出现/消失驱动的自动化）。

- 配置：config/monitors.json [{name, process, condition('gone'|'present'),
  action('shutdown'|'reboot'|'command'), command, enabled}]
- 轮询：每 3 秒 tasklist 查进程（★R213 NEW5-D8：docstring 长期写"5 秒"
  与 POLL=3 漂移）；状态翻转（首次轮询只建基线）→ 触发动作
- 动作：shutdown / reboot（shutdown.exe，30 秒缓冲可取消）/ 运行命令
- 典型用法：
  · 监视 vmware-vmx.exe 消失 → 关闭主机（虚拟机停了，主机也休息）
  · 监视某软件进程出现 → 运行命令（联动启动配套工具）
纪律：本模块无 Tk 依赖；UI 在 gui/sys_panel。
"""
import ctypes
import json
import logging
import os
import struct
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger("desktop_manager")
CREATE_NO_WINDOW = 0x08000000


class ConditionMonitor:
    # ★ R176：10→5 秒；★ R186：5→3 秒（用户 win11 实测"触发略慢"——
    # tasklist 每 3 秒一次，CPU 开销可忽略，状态翻转感知更快）
    POLL = 3
    # ★R333-a2：同一条规则的**重复触发冷却**（秒）。边沿判定本身没错，错在
    # "崩溃循环"那种被监视进程：它每 3 秒起停一次，条件就每 3 秒排定一次
    # `shutdown /t 30`、每 3 秒往桌面摞一个模态框——倒计时被后面那次不断重置，
    # 用户看到的是弹窗堆到数不清、机器始终不关也停不下来。冷却只压重复触发，
    # `_last` 照常跟着翻转，进程真的稳定停了之后第一下仍然立刻触发。
    FIRE_COOLDOWN = 60

    def __init__(self, data_dir, log=None, notify=None):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / 'monitors.json'
        self._log = log or (lambda m: logger.info(m))
        # ★ R176：命中通知回调 (监视名, 描述)——UI 层接它弹窗，
        # 用户不再"命令跑了没跑不知道"
        self._notify = notify
        # ★R318 A（用户 73 批定案："不是取消的按钮，应该是取消的操作，具体可以
        # 在对话框里面体现"）：关机/重启**排定之后**由 UI 层在这个回调里弹出
        # "要不要取消这一次"。签名 (监视名, 动作)，与 `_notify` 同一口径由
        # gui/sys_panel.py 接线；没接上就是 None（引擎线程自己什么弹窗都不做）。
        self._power_ask = None
        # ★R213（NEW4-C2）：留底改名失败=盘上原文件仍含用户数据 → 本会话禁写
        # （与 core/icon_manager.py _ledger_load_failed 同款项）
        self._load_failed = False
        self.monitors = self._load()
        self._stop = threading.Event()
        self._thread = None
        # ★R336-d：本监视器**自己**排出去、且还没确认撤销的关机/重启倒计时
        # （值＝'shutdown'/'reboot'，None＝没有）。为什么要有它：`_fire` 入口那次
        # `_stop` 检查只挡得住**还没发出**的动作，挡不住**已经在系统里排队**的
        # 那条 30 秒倒计时 ⇒ 旧写法 `stop()` 只 set+join，用户停了监视器/退了程序，
        # 机器照样被关机。登记点见 `_arm_power`，清空点见 `_drain_arm`(rc!=0) 与
        # `_drain_cancel`（撤没撤成都要说，但只有"系统确实没答应"才算没有倒计时）。
        self._power_pending = None
        self._last = {}   # name -> 进程是否在（首次轮询建基线，不触发）
        # ★R333-a2：冷却表——上次命中的 monotonic 时刻 + "这一轮冷却已提醒过"标记
        # （标记在下次真触发时清掉，于是一次崩溃循环只念一句，而不是每 3 秒刷屏）
        self._last_fire = {}
        self._cool_warned = set()
        # ★R333 S6-3：进程名空着的那条规则整轮不动作（★R333-a3 之后判"读不动"），
        # 光不动还不够——用户看不出为什么没反应。同样用"只说一次"的集合，
        # 免得 3 秒一轮把它刷成噪音。
        self._noname_warned = set()

    # ---------- 配置 ----------
    def _load(self):
        # ★R210（NEW4-C10）：存量复核提示（一次性）——R204~R207 期间
        # 旧白名单把当时新建的"进程出现时"规则静默改写成了 gone 落盘，
        # 数据上与真 gone 不可分辨，只能提示用户自查
        corrupt = False
        try:
            from utils.app_paths import get_app_dir
            _flag = get_app_dir() / 'config' / '.cond_migration_hint'
            if not _flag.exists():
                self._log('提示：若你在 09-21~09-22 创建过"进程出现时"的'
                          '条件监视，请打开监视列表核对条件方向（早期版本'
                          '可能存反）。此提示只出现一次。')
                try:
                    _flag.parent.mkdir(parents=True, exist_ok=True)
                    _flag.write_text('seen', encoding='utf-8')
                except OSError:
                    pass
        except Exception:
            pass
        try:
            if self.path.exists():
                d = json.loads(self.path.read_text(encoding='utf-8'))
                monitors = d if isinstance(d, list) else []
            else:
                monitors = []
        except json.JSONDecodeError as e:
            # ★R191（Qoder CORE-16）：文件存在但读坏=改名留底再播种，
            # 不再静默用空列表顶掉用户原始配置
            bad = self.path.with_suffix('.json.bad')
            _moved = False
            try:
                self.path.replace(bad)
                _moved = True
            except OSError:
                # ★R213（NEW4-C2）：改名失败=原文件仍在盘上且含用户数据，
                # 置禁写闸，防随后任何一次 save 用空账整份顶掉它
                self._load_failed = True
            logger.error(f'monitors.json 解析失败，留底{"成功" if _moved else "同样失败"}'
                         f'（{bad.name}）: {e}')
            self._log(f'⚠ 监视配置文件损坏，' + (
                f'原文件已留底为 {bad.name}，已重置为示例配置' if _moved else
                f'且留底失败（{self.path.name} 仍含原数据，可能被占用）：'
                '本会话只读展示示例，不会覆盖保存'))
            corrupt = True
            monitors = []
        except Exception as e:
            # ★R207（Qoder CORE-16 收尾）：非解析类读失败（权限/占用/编码）
            # 与解析失败同权处理——留底+告警。原分支只记一行错误就落下
            # monitors=[]，空列表照样触发播种，随后的保存会把用户原始
            # 配置整个顶掉（与"读坏留底"保护自相矛盾）。
            bad = self.path.with_suffix('.json.bad')
            _moved = False
            try:
                self.path.replace(bad)
                _moved = True
            except OSError:
                # ★R213（NEW4-C2）：留底失败=原文件仍在盘上含用户数据，禁写
                self._load_failed = True
            logger.error(f'monitors.json 读取失败，留底{"成功" if _moved else "同样失败"}'
                         f'（{bad.name}）: {e}')
            self._log(f'⚠ 监视配置读取失败，' + (
                f'原文件已留底为 {bad.name}，已重置为示例配置' if _moved else
                f'且留底失败（{self.path.name} 仍含原数据，可能被占用）：'
                '本会话只读展示示例，不会覆盖保存'))
            corrupt = True
            monitors = []
        # ★ R175（用户 -20/5-5）：列表为空时给示例（停用状态，必须点
        # "启用"才生效；可编辑可删除）。用户删光自己的测试条目后重启
        # 软件，示例会重新出现。
        # ★ R185（用户 19 批）：第 2 条按用户点名改跑命令示例；另补 2 个
        # 实用例子（vmware 关完关主机省电 / 下班关企业微信自动关机）。
        # ★ R186（用户 01 批实测）：浏览器示范无效——Edge/Chrome 关窗后
        # 后台进程常驻（启动加速/后台扩展），进程数永不归零，"消失"永远
        # 等不到。示例目标换成干净退出的单进程程序（画图/记事本）。
        # ★ R186b（用户 02 批实测修正）：Google Chrome 关窗会干净退出，
        # 示例可以留（用户定案）；只有 Edge 后台进程常驻不行。故第 1 条
        # 恢复 chrome，画图示例撤下（记事本示例已覆盖"干净退出"演示）。
        if not monitors and not corrupt:
            monitors = [
                {'name': '示例·浏览器关了记一笔', 'process': 'chrome.exe',
                 'condition': 'gone', 'action': 'command',
                 'command': 'notepad.exe', 'enabled': False},
                {'name': '示例·记事本关了跑个命令', 'process': 'notepad.exe',
                 'condition': 'gone', 'action': 'command',
                 'command': 'cmd /k dir', 'enabled': False},
                {'name': '实用·虚拟机关完关主机（省电）',
                 'process': 'vmware-vmx.exe', 'condition': 'gone',
                 'action': 'shutdown', 'command': '', 'enabled': False},
                {'name': '实用·下班关企业微信就关机',
                 'process': 'wxwork.exe', 'condition': 'gone',
                 'action': 'shutdown', 'command': '', 'enabled': False},
            ]
            self._log('👀 已放入 2 个示例 + 2 个实用监视'
                      '（停用中，点"▶ 启用"生效）')
        return monitors

    def save(self):
        """★R213（NEW4-C2）：消费写盘返回值 + 禁写闸。
        Returns bool，供 add/update/set_enabled/remove 透传给 UI。"""
        if self._load_failed:
            logger.warning('[条件监视] 原 monitors.json 读坏且留底失败仍在盘上，'
                           '本会话拒绝覆盖保存（解除占用/修权限后重启生效）')
            self._log('⚠ 监视配置本次未保存：原配置文件读不动且留底失败，'
                      '已停止覆盖写入以保护原始数据（关闭占用该文件的程序'
                      '或重启软件后重试）')
            return False
        from utils.safeio import atomic_write_json
        if not atomic_write_json(self.path, self.monitors):
            logger.error(f'[条件监视] monitors.json 写盘失败: {self.path}')
            self._log(f'⚠ 监视配置保存失败（磁盘写入错误）：本次改动未生效，'
                      f'请检查 {self.path.name} 是否被占用')
            return False
        return True

    def list(self):
        return list(self.monitors)

    # ★R207 NEW3-C1（P1）：词表必须与全仓口径一致——docstring/UI/命中判定
    # 都是 gone|present，R204 误写 ('gone','running') 且 :113 静默改写，
    # 用户选"进程出现时"入库变 gone=触发语义反转（选关机→进程退出才关机）
    _CONDITIONS = ('gone', 'present')
    _ACTIONS = ('shutdown', 'reboot', 'command')

    def add(self, name, process, condition, action, command='', enabled=True):
        # ★R204 CORE-35：入口白名单校验——此前零校验，未知 action 静默无日志、
        # 空进程名/同名条目可重复进账本
        name = (name or '').strip()
        process = (process or '').strip()
        if condition not in self._CONDITIONS:
            logger.warning(f'[条件监视] 未知条件 {condition!r}，按 gone 处理: {name}')
            condition = 'gone'
        if action not in self._ACTIONS:
            logger.warning(f'[条件监视] 未知动作 {action!r}，按 command 处理: {name}')
            action = 'command'
        if not name:
            name = f'监视{len(self.monitors) + 1}'
        if not process:
            logger.warning(f'[条件监视] 拒绝添加（进程名为空）: {name}')
            return False
        if any(m.get('name') == name for m in self.monitors):
            logger.warning(f'[条件监视] 同名条目已存在，拒绝重复添加: {name}')
            return False
        self.monitors.append({'name': name, 'process': process,
                              'condition': condition, 'action': action,
                              'command': command, 'enabled': enabled})
        # ★R213（NEW4-C2）：写盘失败则回滚内存账，不留「看着加上了、重启就没」
        if not self.save():
            self.monitors.pop()
            return False
        return True

    def update(self, orig_name, **kw):
        """★ R175：编辑既有条目（同名覆盖字段；改了名字则键随之更新；
        清基线防止下轮误触发）。★R207 NEW3-C10：与 add 同一白名单——
        此前 update 绕过校验，可把脏值直接写进账本。"""
        kw_condition = kw.get('condition')
        if kw_condition is not None and kw_condition not in self._CONDITIONS:
            logger.warning(f'[条件监视] update 未知条件 {kw_condition!r}，'
                           '已拒绝该项')
            kw = dict(kw)
            kw.pop('condition')
        kw_action = kw.get('action')
        if kw_action is not None and kw_action not in self._ACTIONS:
            logger.warning(f'[条件监视] update 未知动作 {kw_action!r}，'
                           '已拒绝该项')
            kw = dict(kw)
            kw.pop('action')
        if 'process' in kw and not (kw.get('process') or '').strip():
            logger.warning('[条件监视] update 拒绝空进程名')
            kw = dict(kw)
            kw.pop('process')
        # ★R210（NEW4-C11）：改名前查重——新名撞已有条目时 _last 基线
        # 串台/remove 一删两条/记账互相覆盖
        new_name = kw.get('name')
        if new_name and new_name != orig_name:
            if any(m.get('name') == new_name for m in self.monitors):
                logger.warning(f'[条件监视] update 拒绝改名（新名已存在）: '
                               f'{orig_name} → {new_name}')
                kw = dict(kw)
                kw.pop('name')
        # ★R213（NEW4-C2）：返回写盘结果，失败回滚该条目字段
        for m in self.monitors:
            if m.get('name') == orig_name:
                _backup = dict(m)
                m.update(kw)
                self._last.pop(orig_name, None)
                if not self.save():
                    m.clear()
                    m.update(_backup)
                    return False
                return True
        return False

    def set_enabled(self, name, enabled):
        """★ R175：启用/停止单条（示例默认停用，点了才跑）
        ★R213（NEW4-C2）：返回写盘结果，失败回滚 enabled"""
        for m in self.monitors:
            if m.get('name') == name:
                _old = m.get('enabled')
                m['enabled'] = enabled
                self._last.pop(name, None)   # 重新建基线防误触发
                if not self.save():
                    m['enabled'] = _old
                    self._last.pop(name, None)
                    return False
                return True
        return False

    def remove(self, name):
        # ★R213（NEW4-C2）：返回写盘结果，失败整表回滚（保留原顺序）
        _old_list = self.monitors
        self.monitors = [m for m in _old_list if m.get('name') != name]
        if len(self.monitors) == len(_old_list):
            self.monitors = _old_list
            return False
        self._last.pop(name, None)
        if not self.save():
            self.monitors = _old_list
            return False
        return True

    # ---------- 进程探测 ----------
    def _proc_running(self, proc):
        # ★R333-a3：空进程名不许判成"在跑"——`'' in text` 恒真，一条 `process`
        # 留空的规则（导入来的、手工改过 json 的）会永远算 present：condition=gone
        # 那条永不触发，condition=present 那条开机第一下就触发一次动作。
        # 返回 None = "探测失败不计入状态翻转"，走的正是既有那一档。
        if not (proc or '').strip():
            return None
        try:
            p = subprocess.run(['tasklist', '/FI', f'IMAGENAME eq {proc}',
                                '/NH', '/FO', 'CSV'],
                               capture_output=True, timeout=30,
                               creationflags=CREATE_NO_WINDOW)
            text = p.stdout.decode('gbk', errors='replace').lower()
            return proc.lower() in text and 'info:' not in text
        except Exception as e:
            logger.warning(f'进程探测失败 {proc}: {e}')
            return None   # 探测失败不计入状态翻转

    # ---------- 动作 ----------
    @staticmethod
    def _pe_subsystem(path):
        """★ R253（用户 44 批 item1）：读**本地文件头**里的 PE Subsystem 字段，
        判"这个 exe 自己要不要控制台窗口"——2=GUI（双击语义，不该有黑框）、
        3=CUI（本来就要黑框）。

        为什么不写死名单：用户原话「需要黑框比如 cmd,powershell 等」＝名单式
        判定早晚漏（第三方的控制台/图形程序名我们根本猜不全）。子系统字段是
        链接器写进文件的事实，本机 12 个样本逐条与常识相符（`run/r253/pe_与落点.txt`：
        notepad/mspaint/calc/explorer=2，cmd/powershell/ping/ipconfig/tasklist/
        shutdown/wmic=3）。
        只 open 读 4KB 头，不起进程、不跨进程写（红线）；读不动一律返回 None，
        由调用方退回旧行为（包进 cmd），绝不抛。"""
        try:
            with open(path, 'rb') as fh:
                head = fh.read(0x1000)
            if len(head) < 0x40 or head[:2] != b'MZ':
                return None
            off = struct.unpack_from('<I', head, 0x3C)[0]      # e_lfanew
            if head[off:off + 4] != b'PE\x00\x00':
                return None
            # COFF 头 20 字节，可选头里 Subsystem 固定在偏移 68（PE32/PE32+ 同位）
            return struct.unpack_from('<H', head, off + 4 + 20 + 68)[0]
        except Exception:
            # 只读判据：任何意外（文件被占用/截断/非 PE/权限）都退回 None
            return None

    @staticmethod
    def _gui_exe_on_path(tok):
        """token 能在 PATH/已知位置解析到一个**无控制台**的 GUI 程序 ⇒ 绝对路径，
        否则 None（交回调用方走旧的 cmd 包装）。
        .bat/.cmd 一律不认：它们必须有控制台才看得见输出。"""
        s = (tok or '').strip()
        if not s:
            return None
        try:
            import shutil
            hit = shutil.which(s)
        except Exception:
            hit = None
        if not hit or hit.lower().endswith(('.bat', '.cmd')):
            return None
        return hit if ConditionMonitor._pe_subsystem(hit) == 2 else None

    @staticmethod
    def _parse_command(cmd: str):
        """★ R193（Qoder CORE-15）：感知引号的命令解析。

        原 s.split() 把带引号路径撕碎："C:\\Program Files\\a.exe" /x →
        "C:\\Program + 残片。shlex(posix=False) 不吃反斜杠（Windows 路径
        安全）。返回 (file, params)：file=去引号首 token（ShellExecuteW
        独立参数，自己带引号反而失败）；params=其余 token 以空格连接，
        其中含空格者重新加引号防二次撕开。解析失败返回 (None, None)。"""
        s = (cmd or '').strip()
        if not s:
            return None, None
        try:
            import shlex
            toks = shlex.split(s, posix=False)
        except ValueError:
            toks = s.split()
        if not toks:
            return None, None

        def _strip(t):
            if len(t) >= 2 and t[0] == t[-1] and t[0] in ('"', "'"):
                return t[1:-1]
            return t

        file_tok = _strip(toks[0])
        rest = []
        for t in toks[1:]:
            t = _strip(t)
            rest.append(f'"{t}"' if ' ' in t else t)
        return file_tok, (' '.join(rest) if rest else None)

    @staticmethod
    def _route_command(cmd: str):
        """★ R193b（用户 19 批实锤 + 参照"一键目录"打开文件夹的双击语义）：
        命令路由，返回 (mode, file, params)。
          direct = ShellExecuteW(file, params) 直开（双击语义）
          cmdk   = 包进 cmd /k（窗口停留可看输出）
          None   = 无法解析

        规则（用户 19 批实锤 "D:\\t t\\1.txt" 不带引号被 split 撕碎）：
          1) 整串（剥成对引号后）就是存在的文件/目录 → 整串直开——
             ShellExecuteW 的 file 参数天然支持空格，根本不用拆
          2) 无引号带空格路径+参数：从右往左找存在的前缀路径
          3) 引号路径+参数：shlex 解析（CORE-15 兜底）
          3.5) ★R253：裸程序名在 PATH 上解析得到、且它自己的 PE 子系统是
             GUI(2) → 直开（记事本/画图/计算器不再白挨一个黑框）
          4) 其余=shell 命令（dir/flushdns 等）→ cmd /k 原样
        .bat/.cmd 恒包 cmd /k（直开会一闪而过看不到输出）"""
        s = (cmd or '').strip()
        if not s:
            return None, None, None
        bare = s
        if len(bare) >= 2 and bare[0] == bare[-1] and bare[0] in ('"', "'"):
            bare = bare[1:-1].strip()
        # 1) 整串就是存在的路径
        if os.path.exists(bare):
            low = bare.lower()
            if low.endswith(('.bat', '.cmd')):
                return 'cmdk', 'cmd.exe', f'/k "{bare}"'
            return 'direct', bare, None
        toks = s.split()
        # 2) 无引号带空格路径 + 参数：从右剥参数找存在前缀
        # ★R204 NEW-C9：候选必须含路径分隔符才按"存在的路径"认——
        # 否则单 token（如 `data xxx`）会按 CWD 相对路径误判为路径直开、
        # 参数被静默丢弃
        for i in range(len(toks) - 1, 0, -1):
            head = ' '.join(toks[:i])
            if ('\\' in head or '/' in head or ':' in head) \
                    and os.path.exists(head):
                low = head.lower()
                if low.endswith('.exe'):
                    return 'direct', head, ' '.join(toks[i:])
                if low.endswith(('.bat', '.cmd')):
                    return 'cmdk', 'cmd.exe', \
                        f'/k "{head}" ' + ' '.join(toks[i:])
                return 'direct', head, None
        # 3) 引号路径 + 参数（shlex）
        file_tok, params = ConditionMonitor._parse_command(s)
        if file_tok and os.path.exists(file_tok):
            low = file_tok.lower()
            if low.endswith('.exe'):
                return 'direct', file_tok, params
            if low.endswith(('.bat', '.cmd')):
                return 'cmdk', 'cmd.exe', \
                    f'/k "{file_tok}"' + (f' {params}' if params else '')
            return 'direct', file_tok, None
        # ★ R253（用户 44 批 item1「notepad.exe 不需要弹出黑窗」）：
        # 裸程序名（不带路径、要靠 PATH 找）过去一律掉进下面第 4 条 cmd /k，
        # 于是记事本/画图/计算器都先闪一个黑框再从框里出来。现在先问一句
        # "这个程序自己要不要控制台"——读它 PE 头里的 Subsystem（见
        # _pe_subsystem），GUI(2) 就直开；CUI(3)/读不动/带 shell 组合符
        # （`& | > ^ %`，如 `echo hi & pause` 直开会后半截当参数丢给前者）
        # 一律照旧走 cmd 包装，cmd/powershell/dir/ipconfig 的黑框一条不误伤。
        # （`<` 同量补齐：S6 独立验收抓到 `notepad < in.txt` 会直开成"找不到
        # 文件"——重定向符号一律不许直开。）
        if not any(ch in s for ch in '&|>^%<'):
            _f0, _pr = ConditionMonitor._parse_command(s)
            _gui = ConditionMonitor._gui_exe_on_path(_f0)
            if _gui:
                return 'direct', _gui, _pr
        # 4) shell 命令原样进 cmd /k
        return 'cmdk', 'cmd.exe', '/k ' + s

    # ---------- ★R318 A：关机/重启排定之后的"取消这一次操作" ----------
    SHUTDOWN_EXE = r'C:\Windows\System32\shutdown.exe'
    POWER_FLAG = {'shutdown': '/s', 'reboot': '/r'}

    # ---------- ★R332-a：排定之前不许宣布"要关机了" ----------
    def _arm_power(self, act, name):
        """发 `shutdown /s|/r /t 30`，**回码没读到之前一个字都不许说"主机将关机"**。

        老写法 `Popen` 一发就打「⚠ 主机将在 30 秒后关机」，把"我打算"说成了
        "已经发生"。真机上这件事会被系统拒：R045 日志包里那条
        `DeskBits_delay_shutdown_212406` 的 `shutdown.exe /s /t 10` "上次结果"
        =2147946720（0x800710E0「操作员或系统管理员拒绝了请求」）——机器没关，
        用户却以为排上了。★R318 那条"先排定、后问取消"的顺序同理：排定压根没
        成，还弹"要不要取消这一次"，就是让用户点一个不存在的倒计时。
        """
        what = '关机' if act == 'shutdown' else '重启'
        try:
            p = subprocess.Popen(
                [self.SHUTDOWN_EXE, self.POWER_FLAG[act], '/t', '30'],
                creationflags=CREATE_NO_WINDOW,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except Exception as e:
            self._log('⚠ 这次%s没能排定：shutdown.exe 没发出去（%s）——'
                      '机器不会自己%s' % (what, e, what))
            return False
        # 主线程/引擎线程只许发出去，等回码必须换线程（★R230 界面假死同族口径：
        # 在这里 communicate() 就是把 30 秒倒计时期间的工作线程钉住）。
        # ★R336-d：**发出去那一刻**就登记在途标记（不是等 rc==0 才登记）——回码读不到
        # 的那一支按"倒计时可能真在走"对待（★R332-a 原话），晚登记就等于那一支不撤。
        self._power_pending = act
        threading.Thread(target=self._drain_arm,
                         args=(p, act, name), daemon=True).start()
        return True

    def _drain_arm(self, p, act, name):
        """后台收排定结果：只有 rc==0 才说"30 秒后%s"并问要不要取消；
        rc!=0 要把返回码 + 系统原文一起上屏（★R230b：兜底可以吞异常，不许沉默）。"""
        what = '关机' if act == 'shutdown' else '重启'
        try:
            out, _ = p.communicate(timeout=10)
            rc = p.returncode
        except Exception as e:
            # 读不到回码 ≠ 系统没答应：倒计时可能真的在走 ⇒ 这句必须留一个
            # "现在就能撤"的口子，不许只写"请把日志发来"。
            self._log('⚠ %s排定的结果读不到（%s）——没法确认机器会不会自己%s；'
                      '倒计时若真的开始了，请立刻运行 shutdown /a 取消，'
                      '并把这段日志发来' % (what, e, what))
            return
        try:
            tail = (out or b'').decode('gbk', 'replace').strip()
        except Exception:
            tail = ''
        if rc == 0:
            self._log('⚠ 主机将在 30 秒后%s（shutdown /a 可取消）' % what)
            # ★R318 A：排定**确认**发生 ⇒ 这一刻才允许问"要不要取消这一次"
            self._ask_cancel_power(name, act)
        else:
            # ★R336-d：系统没答应 ⇒ 不存在可撤的倒计时 ⇒ 摘掉在途标记。
            # 留着它，`stop()` 就会去发一发注定失败的 `/a`，日志里凭空多一条
            # "取消没成功"——把用户的注意力引向一个根本没发生过的倒计时。
            self._power_pending = None
            self._log('⚠ 这次%s没能排定（返回码 %s）——系统没答应，'
                      '机器不会自己%s%s'
                      % (what, rc, what, ('：' + tail) if tail else ''))

    def _ask_cancel_power(self, name, act):
        """排定成功**之后**才问"要不要取消"——这个顺序是硬要求。

        先问后排的话，用户点"取消"那一刻系统里根本没有倒计时可撤（`shutdown /a`
        返回"没有关机在排队"），30 秒后机器照样关 ⇒ 用户以为自己取消了，比没有
        这个入口更危险。回调由 UI 层接线（gui/sys_panel.py），没接上就什么都不做。
        """
        if not self._power_ask:
            return
        try:
            self._power_ask(name, act)
        except Exception as e:
            # ★R230b：兜底可以吞异常，但必须留痕——这一行丢了就是"倒计时排着、
            # 没人问过、也没人取消得了"，真机上无从归因。
            logger.warning('[条件监视] 取消询问没能交给界面（%r）⇒ 倒计时照旧排着，'
                           '要取消请手动运行 shutdown /a', e)

    def cancel_power_action(self):
        """发 `shutdown /a` 撤销刚排定的关机/重启倒计时（非阻塞，结果后台收）。"""
        try:
            p = subprocess.Popen([self.SHUTDOWN_EXE, '/a'],
                                 creationflags=CREATE_NO_WINDOW,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except Exception as e:
            self._log('⚠ 取消失败：shutdown /a 没能发出去（%s）' % (e,))
            return False
        # 主线程只许 Popen（它就是"发出去"这一刻），等结果必须换线程——
        # 在 Tk 主线程 communicate() 就是界面假死（★R230 同族口径）。
        threading.Thread(target=self._drain_cancel, args=(p,), daemon=True).start()
        return True

    def _drain_cancel(self, p):
        """后台收 `shutdown /a` 的结果并如实回报（成没成都要说，不许沉默）。"""
        try:
            out, _ = p.communicate(timeout=10)
            rc = p.returncode
        except Exception as e:
            self._log('⚠ 取消的结果读不到（%s）——请自己核对倒计时还在不在' % (e,))
            return
        try:
            tail = (out or b'').decode('gbk', 'replace').strip()
        except Exception:
            tail = ''
        if rc == 0:
            # ★R336-d：撤销确认成功 ⇒ 在途标记清空（`stop()` 之后不会再补第二发）。
            self._power_pending = None
            self._log('✅ 已取消刚才排定的关机/重启倒计时')
        else:
            # ★R336-d：rc!=0 的两种可能（倒计时已走完 / 当前根本没排队）都意味着
            # **没有东西可撤** ⇒ 同样清空，否则 `stop()` 会再发一发拿同样的错。
            self._power_pending = None
            self._log('⚠ 取消没成功（返回码 %s）——多半是倒计时已经走完，'
                      '或者当前并没有排队中的关机%s'
                      % (rc, ('：' + tail) if tail else ''))

    def _fire(self, m):
        # ★R204 CORE-33：执行动作前再查一次停止位——join 超时的兜底，
        # 防"程序已关、关机动作仍触发"
        if self._stop.is_set():
            return
        name = m.get('name', '?')
        act = m.get('action')
        desc = (f'{m.get("process", "?")} '
                f'{"已退出" if m.get("condition") == "gone" else "已启动"}'
                f' → 动作[{act}]')
        self._log(f'👀 条件命中 [{name}]：{desc}')
        # ★R333-a1：通知挪到**动作分发之后**。旧写法在 `shutdown.exe` 还没发出去
        # 那一刻就弹「动作已执行」，把"我打算"说成了"已经发生"——★R332-a 那起被系统
        # 拒绝的现场，用户照样读到"已执行"。关机/重启两条从此不走 `_notify`：有资格
        # 宣布"30 秒后关机"的只有 `_drain_arm` 读到的回码，要取消也有
        # `_ask_cancel_power` 那枚对话框（它本来就只在 rc==0 时才弹）。
        _ran = False
        try:
            if act == 'shutdown':
                # ★R332-a：这两句（"30 秒后关机" + 要不要取消）都不许在这里说
                self._arm_power('shutdown', name)
            elif act == 'reboot':
                self._arm_power('reboot', name)
            elif act == 'command':
                cmd = (m.get('command') or '').strip()
                if cmd:
                    # ★ R178（用户"cmd /k dir 黑窗还是没显示"）：实证
                    # pythonw（无控制台进程）下 Popen 无论加不加
                    # CREATE_NEW_CONSOLE 都弹不出可见窗口（Win11 组合
                    # 行为，诊断工具/test_cmd_pw.py 三组对照实锤）；
                    # ShellExecuteW（双击语义）则必然可见（result2 实锤
                    # IsWindowVisible=True）。故统一走 ShellExecuteW：
                    # 纯 exe 直开；其余包进 cmd /k（窗口停留可见）。
                    # ★ R193b（用户 19 批）：整串是存在的路径就直开（空格
                    # 免引号，双击语义）；带参数/引号/shell 命令分级路由，
                    # 见 _route_command
                    mode, file_tok, params = self._route_command(cmd)
                    if mode is None:
                        self._log(f'⚠ [{name}] 命令内容无法解析: {cmd}')
                    else:
                        # ★R204 NEW-C10：补 argtypes/restype——缺省按 c_int
                        # 收返回值，64 位下 HINSTANCE 高位截断可误判失败
                        _sh = ctypes.windll.shell32
                        try:
                            _sh.ShellExecuteW.argtypes = [
                                ctypes.c_void_p, ctypes.c_wchar_p,
                                ctypes.c_wchar_p, ctypes.c_wchar_p,
                                ctypes.c_wchar_p, ctypes.c_int]
                            _sh.ShellExecuteW.restype = ctypes.c_void_p
                        except Exception:
                            pass
                        rc = _sh.ShellExecuteW(
                            None, 'open', file_tok, params, None, 1)  # SW_SHOWNORMAL
                        rc = rc or 0
                        if rc <= 32:
                            self._log(f'⚠ [{name}] 命令启动失败 '
                                      f'(ShellExecuteW rc={rc})')
                        else:
                            self._log(f'已执行命令: {cmd}')
                            # ★R333 S6-1：`_ran` 只在**这一支**置真。S6 独立验收抓到
                            # 上一版把它写在 try 末尾（无条件），于是"命令为空 /
                            # 无法解析 / ShellExecuteW 直接拒绝"三种什么都没发生的
                            # 情形照样弹「动作已执行」——正是本批承诺要消灭的那句话。
                            _ran = True
                else:
                    # ★ R175：用户"测试例子无效"常见原因=动作选了运行命令
                    # 但命令栏没填——明说，不再静默
                    self._log(f'⚠ [{name}] 动作是"运行命令"但命令为空，'
                              f'请编辑该条填写命令')
        except Exception as e:
            logger.error(f'监视动作执行失败 [{name}]: {e}')
        if _ran and act not in ('shutdown', 'reboot'):
            try:
                if self._notify:
                    self._notify(name, desc)
            except Exception:
                pass

    # ---------- 轮询 ----------
    def _loop(self):
        while not self._stop.is_set():
            for m in self.monitors:
                if self._stop.is_set():
                    break
                try:
                    self._check_one(m)
                except Exception as e:
                    # ★R190（Qoder CORE-14）：单条坏数据不得炸掉守护线程
                    logger.error(f'监视轮询单条异常(已跳过): {e}')
            self._stop.wait(self.POLL)

    def _check_one(self, m):
        """★R190：单条监视检查（原内联在 _loop；一条 KeyError 炸整轮
        守护的事故防线）。"""
        if not m.get('enabled', True):
            return
        name = m.get('name', '')
        present = self._proc_running(m.get('process', ''))
        if present is None:
            # ★R333 S6-3：读不动就整轮不动作，但"空进程名"这一种是可以直接告诉用户的
            # （多半是新建那条没填完）。真·探测失败（tasklist 跑不动）不在此列——
            # 那种每轮都可能变，说一句假的比不说更误导。
            if not str(m.get('process', '') or '').strip():
                if name not in self._noname_warned:
                    self._noname_warned.add(name)
                    self._log(u'⚠ [%s] 这条没填进程名，不会触发任何动作'
                              u'（编辑该条填上进程名即可）' % (name,))
            return
        last = self._last.get(name)
        hit = False
        if last is not None:
            if m.get('condition') == 'gone' and last and not present:
                hit = True
            elif m.get('condition') == 'present' and not last and \
                    present:
                hit = True
        self._last[name] = present
        if not hit:
            return
        # ★R333-a2：冷却只挡"同一条规则的重复触发"，不挡边沿更新（上面已记账）。
        now = time.monotonic()
        prev = self._last_fire.get(name)
        if prev is not None and now - prev < self.FIRE_COOLDOWN:
            if name not in self._cool_warned:
                self._cool_warned.add(name)
                self._log(u'⚠ [%s] 这个进程在反复起停，%d 秒内不再重复触发'
                          u'（想马上恢复提醒，把该条关掉再打开）'
                          % (name, self.FIRE_COOLDOWN))
            return
        self._cool_warned.discard(name)
        self._last_fire[name] = now
        self._fire(m)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name='ConditionMonitor')
        self._thread.start()
        logger.info(f'[条件监视] 引擎启动，{len(self.monitors)} 条规则，'
                    f'每 {self.POLL} 秒轮询')

    def _abort_power_if_pending(self, reason):
        """★R336-d：把**本监视器自己**排过、还没确认撤销的关机/重启倒计时撤掉。

        为什么必须由"停下来"这条路来撤：`_fire` 入口的 `_stop` 检查只挡得住还没
        发出的动作，挡不住已经进系统队列的那条 30 秒倒计时 ⇒ 旧写法用户停监视器 /
        退出程序之后机器照样被关机，而且程序已经不在了，没人再能取消它。
        幂等：标记在**发之前**摘掉 ⇒ `stop()` 连叫两次也只发一发 `/a`。
        没排定过（标记为 None）⇒ 一发都不发，`/a` 不是停止的固定动作。
        """
        act = self._power_pending
        if not act:
            return False
        self._power_pending = None
        what = '关机' if act == 'shutdown' else '重启'
        self._log('⚠ %s ⇒ 撤销本监视器排定的%s倒计时（shutdown /a）' % (reason, what))
        return self.cancel_power_action()

    def stop(self):
        """★R204 CORE-33：set + join——_loop 一轮里最多串 30s 的
        subprocess.run，只 set 不等=退出流程以为已停、线程还在跑，
        "程序已关电脑仍被关机"就发生在窗口期。

        ★R336-d：join 之后还要撤销**已经发出去**的倒计时（上面那句只覆盖"还没发出"
        的窗口期）。撤销必须排在 join 后面，否则监视线程可能紧接着又排一条新的。
        """
        self._stop.set()
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            # ★R207 NEW3-A4：join 上限收敛——动作全是 Popen/ShellExecuteW
            # 异步发射，_fire 入口已查 _stop；退出等待 34s 毫无必要
            t.join(timeout=self.POLL + 5)
        # ★R336-d：旧写法在这里 `if t is threading.current_thread(): return` 早退，
        # 会把撤销一起跳掉 ⇒ 改成只跳过 join，撤销两条路都走。
        self._abort_power_if_pending('监视器停止')
