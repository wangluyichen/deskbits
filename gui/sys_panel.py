# -*- coding: utf-8 -*-
"""🧰 系统相关面板（R159 新增选项卡；R160 锁屏自带画面+事件查看器+计划任务）。

- 🔒 锁屏：自带全屏画面（gui/lock_screen.py），图片/字号可自定义；
  标题栏"🔒 锁屏"按钮同入口。
- 📜 事件查看器：系统事件大白话列表（开关机/异常/崩溃），只读。
- 📅 计划任务：用户级任务列表 + 运行/启停（系统任务需管理员才可改）。
纪律：**严禁 import gui.main_window**（模块双身份）。
"""
import io
import logging
import subprocess   # ★ R176 补模块级导入：_qa_run 用它但只有局部 import
                    # （自动测试逮住的第二个真 bug——wait/spawn 键全 NameError）
import threading
import os
import re
import sys          # ★R241：_selfstart_target 要读 frozen/executable 判定打包态
import weakref
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from gui import ui_layout
from gui.ui_layout import SP
from gui.theme_panel import COLORS
from utils import i18n

logger = logging.getLogger("desktop_manager")

# ★ R185：子进程防黑框（此前散落用字面量 0x08000000，新代码统一用本常量）
CREATE_NO_WINDOW = 0x08000000

# ★R223：三张表（监视/事件/计划任务）的语义 tag 前景色改走色板 token。
# 此前写死深色主题十六进制（warn=#ffd866 淡黄、running=#50fa7b 荧光绿、
# off=#6272a4），浅色/护眼下浅字压浅底对比度≈1.6:1＝整行"隐形"
# （用户 2026-09-23 冒烟截图实证）。深色/暗黑的 token 值＝原 hex，观感不变。
_SEMANTIC_TAG_SPEC = {
    'mon': (('danger', 'error'), ('cmd', 'warning'), ('off', 'muted')),
    'evt': (('err', 'error'), ('warn', 'warn_text')),
    'task': (('running', 'ok_text'), ('disabled', 'muted'),
             ('danger', 'error'), ('errlast', 'warning')),
}
_SEMANTIC_TREES = []


def tag_semantic(tree, kind):
    """按当前色板给语义 tag 上前景色，并登记进换肤重刷表。"""
    for tag, token in _SEMANTIC_TAG_SPEC[kind]:
        tree.tag_configure(tag, foreground=COLORS[token])
    if not any(r() is tree for _, r in _SEMANTIC_TREES):
        _SEMANTIC_TREES.append((kind, weakref.ref(tree)))


def refresh_semantic_tags(colors=None):
    """换肤后按新色板重刷已登记表格的语义 tag（与 zebra 同套路）。"""
    alive = []
    for kind, ref in _SEMANTIC_TREES:
        tree = ref()
        if tree is None:
            continue
        alive.append((kind, ref))
        try:
            for tag, token in _SEMANTIC_TAG_SPEC[kind]:
                tree.tag_configure(tag, foreground=COLORS[token])
        except Exception:
            logger.exception('[sys_panel] 语义 tag 换肤重刷失败 kind=%s', kind)
    _SEMANTIC_TREES[:] = alive


def lock_now(gui):
    """🔒 打开自带全屏锁屏（R160）。

    ★ R166 安全三件套（用户 -16/1）：
    ①首次锁屏强制先设锁屏密码（取消=不锁屏，杜绝"锁了不知道怎么解"）；
    ②锁定期间用注册表隐藏开始菜单"注销"（防绕过），解锁恢复；
    ③守卫状态写 config/lock_guard_state.json，程序启动检测残留自动恢复
    （崩溃自恢复，参照 ComputerLock.RecoverFromCrash）。"""
    from gui import lock_screen
    # ① 必须有可用解锁方式：独立锁屏密码或家长密码，否则先设密码
    try:
        has = lock_screen.has_own_password() or gui.parental.has_password()
    except Exception:
        has = False
    if not has:
        set_lock_password(gui)   # 内部已含取消路径（返回即放弃锁屏）
        if not lock_screen.has_own_password():
            gui._log('🔒 未设置密码，已取消锁屏')
            return
    # ② 注销守卫
    # ★R215（NEW6-E1）：engage 兜底——守卫是"防绕过"的加固项，它自己抛异常
    # （注册表读失败/账本写盘失败/函数名级 bug）不该让**锁屏本身**失败：
    # 用户点"锁屏"拿不到锁屏，比"注销没隐藏成"严重得多。失败只告警继续。
    try:
        lock_screen.engage_logoff_guard(gui)
    except Exception as e:
        logger.warning(f"注销守卫 engage 失败（继续锁屏，注销入口本轮未隐藏）: {e}")
    # ③ 开锁
    try:
        lock_screen.open_lock(gui)
    except Exception as e:
        logger.error(f"自带锁屏失败，回退系统锁屏: {e}")
        # ★R215：release 同样兜底——它抛异常会跳过下面的系统锁屏回退，
        # 结果是"两种锁都没锁上"
        try:
            lock_screen.release_logoff_guard(gui)
        except Exception as e2:
            logger.warning(f"回退前 release 守卫失败（启动清扫兜底）: {e2}")
        # ★R221（NEW7-P0-5）：与 lock_screen.open_lock 的回退路径同一真身——
        # 原来两处都是"不查返回值 + except: pass"，两把锁全失败也毫无提示
        lock_screen.system_lock_or_warn(gui, f'自带锁屏异常: {e}')


def set_lock_password(gui):
    """设置/修改锁屏独立密码（≥4 位，两次一致；不连家长模式）"""
    from tkinter import simpledialog
    p1 = simpledialog.askstring(
        i18n.tr('锁屏密码', 'Lock Password'),
        i18n.tr('请输入锁屏密码（至少 4 位）：', 'Enter lock password (4+ chars):'),
        show='*', parent=gui.root)
    if p1 is None:
        return
    if len(p1) < 4:
        messagebox.showwarning(i18n.tr('提示', 'Notice'),
                               i18n.tr('密码至少 4 位', 'At least 4 characters'),
                               parent=gui.root)
        return
    p2 = simpledialog.askstring(i18n.tr('锁屏密码', 'Lock Password'),
                                i18n.tr('请再输入一次确认:', 'Confirm:'),
                                show='*', parent=gui.root)
    if p2 is None:
        return
    if p1 != p2:
        messagebox.showwarning(i18n.tr('提示', 'Notice'),
                               i18n.tr('两次输入不一致', 'Mismatch'),
                               parent=gui.root)
        return
    from gui import lock_screen
    lock_screen.set_own_password(p1)
    gui._log('🔒 锁屏独立密码已设置')
    # ★ R167：锁屏密码的自救通道=密保找回——没设密保时强制引导
    #（用户实测陷阱：设了密码又忘、密保也没有 → 只能重启）
    try:
        has_q = gui.parental.has_questions()
    except Exception:
        has_q = False
    if not has_q:
        if messagebox.askyesno(
                i18n.tr('必须设置密保', 'Security Questions Required'),
                i18n.tr('锁屏密码的找回依赖密保问题。\n'
                        '不设密保的话，忘记密码只能重启电脑。\n\n'
                        '现在设置密保问题吗？',
                        'Password recovery relies on security questions.\n'
                        'Without them a forgotten password means a reboot.\n\n'
                        'Set them now?'),
                parent=gui.root):
            gui._on_set_questions_btn()
        else:
            gui._log('⚠ 未设密保：忘记锁屏密码将只能重启（建议尽快补设）')


def lock_screen_has_pwd():
    from gui import lock_screen
    return lock_screen.has_own_password()


def pick_image(gui):
    """选择锁屏图片：复制到 config/ 固定名，随包/随目录走"""
    p = filedialog.askopenfilename(
        parent=gui.root, title=i18n.tr('选择锁屏图片', 'Pick Lock Image'),
        filetypes=[(i18n.tr('图片', 'Image'), '*.png *.jpg *.jpeg *.gif *.bmp')])
    if not p:
        return
    from utils.app_paths import get_app_dir
    dest = get_app_dir() / 'config' / ('lock_screen_image' + Path(p).suffix.lower())
    try:
        from PIL import Image
        Image.open(p)   # 先验证是有效图片
        import shutil
        shutil.copy2(p, dest)
        gui._app_settings['lock_image'] = dest.name
        gui._app_settings.pop('lock_preset', None)
        gui._save_app_settings()
        gui._log(f'🖼️ 锁屏图片已设置：{dest.name}（下次锁屏生效）')
    except Exception as e:
        messagebox.showerror(i18n.tr('错误', 'Error'),
                             i18n.tr('图片无效或复制失败：', 'Invalid image or copy failed: ')
                             + str(e), parent=gui.root)


def clear_image(gui):
    gui._app_settings.pop('lock_image', None)
    gui._save_app_settings()
    gui._log('🖼️ 锁屏图片已清除（恢复默认 🔒）')
    # ★ R193b（用户 19 批"点清除应该有提示"）：此前只写 GUI 日志页，
    # 用户点了没感知
    messagebox.showinfo(
        i18n.tr('提示', 'Notice'),
        i18n.tr('锁屏图片已清除，下次锁屏恢复默认 🔒',
                'Lock image cleared; next lock shows the default 🔒'),
        parent=gui.root)


# ★ R188（用户 05 批抓到）：不同来源会复用同一事件 ID——
# Kernel-Processor-Power 的 55 号="处理器电源管理功能"信息级通知，
# 与 disk/volmgr 的 55 号"磁盘数据结构损坏"完全无关；只按 ID 过滤会把
# 电源信息错标成磁盘故障。故每条映射带"来源家族"，读取与原生视图都校验。
# prov=原生视图 XPath 里的精确 Provider 名；key=UI 侧对 SourceName
# 小写包含匹配的判别串
_FAM_PROVIDERS = {
    'eventlog': (['EventLog', 'Microsoft-Windows-Eventlog'],
                 ('eventlog',)),
    'kpower': (['Microsoft-Windows-Kernel-Power', 'Kernel-Power'],
               ('kernel-power',)),
    'user32': (['USER32'], ('user32',)),
    'scm': (['Service Control Manager'], ('service control manager',)),
    'bugcheck': (['BugCheck'], ('bugcheck',)),
    'disk': (['disk', 'volmgr', 'volsnap', 'Ntfs', 'stornvme', 'storahci',
              'iaStor'],
             ('disk', 'volmgr', 'volsnap', 'ntfs', 'stornvme', 'storahci',
              'iastor')),
    'display': (['Display'], ('display',)),
    'pnp': (['Microsoft-Windows-Kernel-PnP', 'Kernel-PnP'],
            ('kernel-pnp',)),
    'wu': (['Microsoft-Windows-WindowsUpdateClient', 'WindowsUpdateClient'],
           ('windowsupdateclient',)),
    'dns': (['Microsoft-Windows-DNS-Client', 'DnsClient'], ('dns',)),
    'kgeneral': (['Microsoft-Windows-Kernel-General', 'Kernel-General'],
                 ('kernel-general',)),
    'tcpip': (['Tcpip', 'NlaSvc', 'Microsoft-Windows-NlaSvc'],
              ('tcpip', 'nlasvc')),
}
# (大白话, 分类, 来源家族 or None)
_EVENT_MAP = {6005: ('开机', 'boot', 'eventlog'),
              6006: ('正常关机', 'boot', 'eventlog'),
              6008: ('意外关机（死机/断电后重启）', 'bad', 'eventlog'),
              6009: ('系统版本信息（开机记录）', 'boot', 'eventlog'),
              41: ('异常断电（未正常关机）', 'bad', 'kpower'),
              42: ('系统进入睡眠', 'boot', 'kpower'),
              1074: ('程序/用户发起关机或重启', 'boot', 'user32'),
              1076: ('异常关机后首次记录（关机原因）', 'boot', 'user32'),
              6013: ('系统运行时长上报', 'boot', 'eventlog'),
              1001: ('系统崩溃记录（蓝屏类）', 'bad', 'bugcheck'),
              104: ('系统日志被清除（注意核实）', 'bad', 'eventlog'),
              7045: ('新服务被安装', 'other', 'scm'),
              7031: ('服务意外终止', 'bad', 'scm'),
              7034: ('服务意外终止后自动重启', 'bad', 'scm'),
              7026: ('开机时有服务加载失败', 'bad', 'scm'),
              7040: ('服务启动类型被更改（注意核实）', 'other', 'scm'),
              7011: ('服务响应超时', 'bad', 'scm'),
              7022: ('服务启动卡住未完成', 'bad', 'scm'),
              7023: ('服务启动失败', 'bad', 'scm'),
              7024: ('服务因错误终止（带错误码）', 'bad', 'scm'),
              7: ('磁盘坏块（设备硬错误）', 'bad', 'disk'),
              51: ('磁盘分页读取错误', 'bad', 'disk'),
              55: ('磁盘数据结构损坏（建议查盘）', 'bad', 'disk'),
              129: ('存储设备被重置（磁盘超时）', 'bad', 'disk'),
              153: ('磁盘 IO 超时（盘子系统不稳）', 'bad', 'disk'),
              157: ('磁盘被意外移除', 'bad', 'disk'),
              4101: ('显卡驱动停止响应（已自动恢复）', 'bad', 'display'),
              219: ('驱动安装失败', 'bad', 'pnp'),
              20: ('Windows 更新安装失败', 'bad', 'wu'),
              1014: ('域名解析失败', 'other', 'dns'),
              1015: ('域名解析超时', 'other', 'dns'),
              # ★ R188d：更多下拉新增条目（均经真日志实测核对）
              19: ('更新安装成功', 'other', 'wu'),
              43: ('更新开始安装', 'other', 'wu'),
              1: ('系统时间已更改', 'other', 'kgeneral'),
              4201: ('网络适配器已连接', 'other', 'tcpip'),
              4202: ('网络适配器已断开', 'other', 'tcpip')}


def _load_events(gui, tree):
    """读 System 日志里的关键事件，大白话列表（只读不改）。

    ★ R163：完整记录存 gui._evt_rows（含事件原文），行 iid=下标——
    双击行弹详情对话框 + 同类事件列表 + 过滤脚本。"""
    tree.delete(*tree.get_children())
    # ★R204 GUIA-7：import 入 try——打包环境漏收 win32evtlog 子模块时
    # 不能让异常沿 build_sys_tab→__init__ 上抛（整个程序双击打不开）
    try:
        import win32evtlog
    except Exception as e:
        logger.error(f'win32evtlog 导入失败，事件列表降级: {e}')
        try:
            gui._log('⚠ 事件日志组件加载失败，事件查看器不可用（其余功能不受影响）')
        except Exception:
            pass
        return
    # ★ R165/R174 两轮扩容（参照 windows-log-analyzer 分类思路，覆盖
    # System 日志常见故障项：服务/磁盘/驱动/更新/事件日志服务）。
    # R174 按本机 7 天高频 ID 实证筛选：Id=1 在本机是 FilterManager
    # 噪音（非睡眠唤醒），不收录；高频的 20/51/129 等才值得进表。
    MAP = _EVENT_MAP
    rows = []
    try:
        hand = win32evtlog.OpenEventLog(None, 'System')
        flags = (win32evtlog.EVENTLOG_BACKWARDS_READ |
                 win32evtlog.EVENTLOG_SEQUENTIAL_READ)
        read = 0
        seen = {}   # ★ R174：单 ID 每次扫描最多 25 条，防高频 ID
                    # （如本机 1014 日均 40+ 条）把 200 行占满、稀有
                    # 故障事件被挤出视野
        while read < 4000 and len(rows) < 200:
            evs = win32evtlog.ReadEventLog(hand, flags, 0)
            if not evs:
                break
            read += len(evs)
            for e in evs:
                eid = e.EventID & 0xFFFF
                ent = MAP.get(eid)
                if ent is None or seen.get(eid, 0) >= 25:
                    continue
                txt, cat, fam = ent
                # ★ R188：来源家族校验——同 ID 不同来源=别家的事件不冒领
                # （如 Kernel-Processor-Power 的 55 号是电源信息非磁盘故障）
                if fam:
                    src = (e.SourceName or '').lower()
                    if not any(t in src
                               for t in _FAM_PROVIDERS[fam][1]):
                        continue
                seen[eid] = seen.get(eid, 0) + 1
                msg = ''
                try:
                    if e.StringInserts:
                        msg = '\n'.join(str(s) for s in e.StringInserts
                                        if s)
                except Exception:
                    pass
                # ★ R187：记事件级别（1=错误 2=警告 4=信息）——着色
                # 与原生查看器对齐，不再按分类一锅端全红
                rows.append({'time': e.TimeGenerated.Format('%m-%d %H:%M'),
                             'txt': txt, 'src': e.SourceName, 'eid': eid,
                             'cat': cat, 'msg': msg, 'lv': e.EventType})
    except Exception as e:
        logger.error(f'读取系统事件失败: {e}')
        tree.insert('', 'end', values=('', f'读取失败: {e}', '', ''))
        return
    flt = getattr(gui, '_evt_filter', None) or {}
    id_only = getattr(gui, '_evt_id_only', None)
    gui._evt_rows = rows
    n = 0
    for i, r in enumerate(rows):
        # ★ R188f（用户 08 批"域名解析点了没东西"）：更多预设带明确 ID
        # 组时按 ID 过滤为准——此前先过分类闸（预设把分类勾成 bad，而
        # DNS 属 other）→ 25 条 DNS 全被拦掉只剩占位行
        if id_only is not None:
            show = r['eid'] in id_only
        else:
            try:
                show = bool(flt[r['cat']].get())
            except Exception:
                show = True
        if not show:
            continue
        # ★ R187b（用户定案）：着色完全对齐 Windows 原生图标语义——
        # 错误级（红 X）=红、警告级（黄 !）=黄，其余一律默认白色，
        # 不再按分类染灰/红
        lv = r.get('lv') or 0
        if lv == 1:
            tags = ('err',)
        elif lv == 2:
            tags = ('warn',)
        else:
            tags = ()
        tree.insert('', 'end', iid=str(i), tags=tags,
                    values=(r['time'], r['txt'], r['src'], r['eid']))
        n += 1
    if not n:
        tree.insert('', 'end', values=('', '没有匹配的记录', '', ''))
    st = getattr(gui, '_evt_status', None)
    if st:
        st.set(f'共扫描 {len(rows)} 条关键事件 · 当前显示 {n} 条')
    # ★R222 阶段 4：隔行底色（只铺奇数行，err/warn 语义色不受影响）
    ui_layout.zebra_treeview(tree)


def _show_event_detail(gui, tree, colors):
    """★ R163 双击事件行：详情对话框（原文+同类列表+过滤脚本+事件查看器）"""
    sel = tree.selection()
    if not sel:
        return
    rows = getattr(gui, '_evt_rows', [])
    try:
        r = rows[int(sel[0])]
    except Exception:
        return
    eid = r['eid']
    win = tk.Toplevel(gui.root)
    win.title(i18n.tr('事件详情', 'Event Detail'))
    win.configure(bg=colors['bg'])
    win.transient(gui.root)

    gl = lambda: colors

    ttk.Label(win, text=f"{r['time']}  ·  {r['txt']}",
              font=ui_layout.app_font(12, 'bold'),
              foreground=gl()['primary'], background=gl()['bg']).pack(
        anchor=tk.W, padx=14, pady=(12, 2))
    ttk.Label(win, text=i18n.tr(f'来源: {r["src"]}    事件 ID: {eid}',
                                f'Source: {r["src"]}    Event ID: {eid}'),
              foreground=gl()['muted'], background=gl()['bg']).pack(
        anchor=tk.W, padx=14)
    ttk.Label(win, text=i18n.tr('事件原文：', 'Message:'),
              foreground=gl()['fg'], background=gl()['bg']).pack(
        anchor=tk.W, padx=14, pady=(8, 0))
    txtw = tk.Text(win, height=7, wrap=tk.WORD, bg=colors['surface'],
                   fg=colors['fg'], relief=tk.FLAT, highlightthickness=0)
    txtw.pack(fill=tk.BOTH, expand=True, padx=14)
    txtw.insert('1.0', r['msg'] or i18n.tr('（无详细描述）', '(no details)'))
    txtw.configure(state=tk.DISABLED)

    same = [x for x in rows if x['eid'] == eid]
    ttk.Label(win, text=i18n.tr(f'同类事件（近 200 条扫描内 {len(same)} 条）：',
                                f'Same-type events: {len(same)}'),
              foreground=gl()['fg'], background=gl()['bg']).pack(
        anchor=tk.W, padx=14, pady=(6, 0))
    lb = tk.Listbox(win, height=5, bg=colors['surface'],
                    fg=colors['fg'], relief=tk.FLAT, highlightthickness=0)
    lb.pack(fill=tk.X, padx=14)
    for x in same[:8]:
        lb.insert(tk.END, f"{x['time']}  {x['txt']}")

    btns = ttk.Frame(win)
    btns.pack(fill=tk.X, padx=14, pady=10)

    def _gen_script():
        """★ 用户 -14/6 方案：生成过滤查看脚本并运行。
        ★ R188：加 ProviderName——"同类"=同 ID+同来源（不同驱动的同号
        事件含义完全不同，如 Kernel-Boot 153=虚拟化安全策略状态 vs
        disk 153=磁盘 IO 重试）。"""
        bat = Path(os.environ.get('APPDATA', str(Path.home()))) / \
            f'DeskBits_事件过滤_ID{eid}.bat'
        # ★R204 GUIA-13：src 拼进 PowerShell 单引号串必须转义 ' → ''
        src_ps = (r.get('src') or '').replace("'", "''")
        ps = (f"Get-WinEvent -FilterHashtable @{{LogName='System'; Id={eid}; "
              f"ProviderName='{src_ps}'; "
              f"StartTime=(Get-Date).AddDays(-7)}} | "
              f"Format-List TimeCreated, ProviderName, Message | "
              f"Out-String -Width 200")
        # ★R207 NEW3-A3：非中文码页上 mbcs 编不出中文 echo 会抛
        # UnicodeEncodeError（按钮变死）——失败回退 UTF-8+chcp 65001
        try:
            bat.write_text(
                '@echo off\r\n'
                f'echo 正在列出近 7 天 事件 ID {eid} 的记录（关闭窗口即退出）...\r\n'
                f'powershell -NoExit -Command "{ps}"\r\n',
                encoding='mbcs')
        except UnicodeEncodeError:
            bat.write_text(
                '@echo off\r\nchcp 65001>nul\r\n'
                f'echo 正在列出近 7 天 事件 ID {eid} 的记录（关闭窗口即退出）...\r\n'
                f'powershell -NoExit -Command "{ps}"\r\n',
                encoding='utf-8')
        os.startfile(str(bat))
        gui._log(f'📜 过滤脚本已生成并运行：{bat.name}')

    ttk.Button(btns, text=i18n.tr('🔍 查看同类事件（近 7 天，生成过滤脚本）',
                                  '🔍 Same events (7d, via script)'),
               command=_gen_script).pack(side=tk.LEFT)
    ttk.Button(btns, text=i18n.tr('🌐 打开事件查看器（此类事件视图）',
                                  '🌐 Open Event Viewer (this event class)'),
               command=lambda: _open_event_class_in_viewer(gui, r)).pack(
        side=tk.LEFT, padx=(8, 0))
    ttk.Button(win, text=i18n.tr('关闭', 'Close'),
               command=win.destroy).pack(anchor=tk.E, padx=14, pady=(0, 10))

    # ★ R192b（用户 17 批"按钮显示不全"，win11 系统显示放大=高 DPI）：
    # 定死 600x430 在放大环境下装不下内容，底部按钮行被裁掉（同向导
    # R183 病）。改按内容实际需求自适应+居中，超屏再封顶。
    win.update_idletasks()
    _w = max(600, win.winfo_reqwidth())
    _h = win.winfo_reqheight()
    _max_h = win.winfo_screenheight() - 80
    if _h > _max_h:
        _h = _max_h
    _x = max(0, (win.winfo_screenwidth() - _w) // 2)
    _y = max(20, (win.winfo_screenheight() - _h) // 2 - 30)
    win.geometry(f'{_w}x{_h}+{_x}+{_y}')


def _qa_run(gui, name, cmd, btn, mode='wait'):
    """一键维护：后台执行，成功/失败都弹窗提示（用户 -20/5-4"完全静默"
    整改），并写 app.log（-20/5-5"程序应有日志记录"）。

    mode='wait'  等待完成，检查 rc 与输出失败字样；
    mode='spawn' 即发即忘（GUI 工具如磁盘清理，不等待其窗口关闭——
                 旧实现 subprocess.run 等到 180s 超时=用户"没窗口"观感）；
    mode='py'    cmd 为 python 可调用，返回 str 则作为结果详情。"""
    btn.configure(state='disabled')
    gui._log(f'⚡ {name}：执行中…')

    def _work():
        ok, detail = True, ''
        try:
            if callable(cmd):
                r = cmd()
                detail = r if isinstance(r, str) and r else '完成'
            elif mode == 'spawn':
                subprocess.Popen(cmd, shell=True, creationflags=0x08000000)
                detail = i18n.tr('已启动（窗口已打开，无需等待）',
                                 'launched')
            else:
                p = subprocess.run(cmd, shell=True, capture_output=True,
                                   timeout=180, creationflags=0x08000000)
                ok = p.returncode == 0
                detail = (p.stdout.decode('gbk', errors='replace') + ' '
                          + p.stderr.decode('gbk', errors='replace')).strip()
                # ★R204 GUIA-10：剔除"0 个失败/0 个错误"式正常输出再判关键词，
                # 中文系统"0 个错误"不再误报失败
                import re as _re10
                detail_chk = _re10.sub(
                    r'^.*?\b0\s*(个)?\s*(失败|错误|error|failed)\b.*$',
                    '', detail, flags=_re10.IGNORECASE | _re10.MULTILINE)
                if ok and ('失败' in detail_chk or '错误' in detail_chk
                           or 'denied' in detail_chk.lower()):
                    ok = False   # rc=0 但输出带失败字样（如拒绝访问）
        except Exception as e:
            ok = False
            detail = str(e)
            logger.error(f'一键维护 {name} 失败: {e}')
        logger.info(f'一键维护[{name}] ' +
                    ('完成' if ok else f'失败: {detail[:200]}'))

        def _done():
            try:
                btn.configure(state='normal')
            except Exception:
                pass
            gui._log(f'⚡ {name}：' + ('完成 ✓' if ok else '失败 ✗'))
            if ok:
                messagebox.showinfo(
                    i18n.tr('完成', 'Done'),
                    f'{name} ✓\n{(detail or "完成")[:300]}',
                    parent=gui.root)
            else:
                messagebox.showwarning(
                    i18n.tr('执行失败', 'Failed'),
                    i18n.tr(f'{name} 执行失败。\n{detail[:300]}',
                            f'{name} failed.\n{detail[:300]}'),
                    parent=gui.root)

        try:
            # ★R207（Qoder GUIB-4）：后台线程回投改 _ui_queue（项目纪律）
            gui._ui_queue.put(_done)
        except Exception:
            pass

    # ★ R176 修复（自动测试逮住）：R174 重写时丢了线程启动行——
    # _work 定义后从未执行，点按钮后永远"执行中…"且无成功/失败、
    # 无弹窗、app.log 无记录 = 用户"一键维护完全静默"的真凶
    import threading
    threading.Thread(target=_work, daemon=True,
                     name='QuickMaint').start()


def _evt_querylist(ids):
    """★ R188：按来源家族分组生成 QueryList 内部 XML。

    同 ID 不同来源（如 Kernel-Processor-Power 的 55 号）会污染纯 ID
    过滤的原生视图（用户 win11 实测"DeskBits 磁盘"291 条全是电源信息），
    故带 Provider 条件的家族各出一个 Select（多个 Select=或关系）。
    ★ R188d：**不再有无来源守卫的裸 Select**——不在 _EVENT_MAP（无
    来源家族）的 ID 一律跳过。此前 44/4616/4208 这类未核实 ID 生成裸
    EventID 条件，任意来源的同号事件全部涌进视图（"乱的很"推手）。"""
    groups = {}
    for eid in ids:
        ent = _EVENT_MAP.get(eid)
        if not ent or not ent[2]:
            continue   # 无来源家族=无法准确过滤=宁可不进视图
        groups.setdefault(ent[2], []).append(eid)
    parts = []
    for fam, eids in groups.items():
        provs = _FAM_PROVIDERS[fam][0]
        # ★ R188c（用户 07 批逮住）：此前漏拼 Provider[ ] 包装，生成
        # `System[(@Name=…) …]` 非法 XPath，eventvwr 直接丢弃整个视图
        # （"开关机没了"真凶）。合法形态=System[Provider[@Name=… or …]
        # and (EventID=…)]
        p = ' or '.join(f"@Name='{x}'" for x in provs)
        c = ' or '.join(f'EventID={i}' for i in sorted(set(eids)))
        parts.append(f'<Select Path="System">*[System[Provider[{p}] '
                     f'and ({c})]]</Select>')
    return ''.join(parts)


def _write_custom_view(title, querylist_inner, log_name='System'):
    """★ R187：写事件查看器"自定义视图"XML——**结构以 Windows 自带的
    黄金参照 C:\\Windows\\System32\\NdfEventView.xml 逐字节比对定案**：

      <ViewerConfig><QueryConfig><QueryParams><UserQuery/></QueryParams>
      <QueryNode><Name>视图名</Name><QueryList>
        <Query Id="0" Path="System"><Select Path="System">…XPath…</Select>
        </Query></QueryList></QueryNode></QueryConfig></ViewerConfig>

    此前两代失败原因：①FilterXml 方言（Query Id+Text+QueryList 顶层）
    ②QueryId+Query 结构——两者 eventvwr 都只列文件名、解析不出查询
    （视图树里的显示名其实来自 QueryNode/Name，解析失败才回退文件名）。
    落盘=无 BOM UTF-8（与黄金文件一致，不加 XML 声明）。"""
    xml = (
        '<ViewerConfig><QueryConfig><QueryParams><UserQuery/></QueryParams>'
        '<QueryNode><Name>' + title + '</Name>'
        '<QueryList><Query Id="0" Path="' + log_name + '">'
        + querylist_inner +
        '</Query></QueryList></QueryNode>'
        '</QueryConfig></ViewerConfig>')
    base = Path(os.path.join(
        os.environ.get('ProgramData', r'C:\ProgramData'),
        'Microsoft', 'Event Viewer', 'Views', 'Views'))
    base.mkdir(parents=True, exist_ok=True)
    # 文件名即视图名；覆盖写同视图=更新，不产生垃圾
    (base / (title + '.xml')).write_bytes(xml.encode('utf-8'))
    return base / (title + '.xml')


def _open_event_class_in_viewer(gui, r):
    """★ R188e（用户 07 批"跟脚本一致"）：详情对话框按钮=为双击的这一
    类事件生成视图（同 ID+同来源，与 🔍 过滤脚本完全同口径），视图名用
    大白话（如 DeskBits 域名解析失败）。

    此前误用"当前筛选"状态——用户双击 DNS 事件却得到 36 个 ID 的大
    杂烩视图（名字还叫 开关机+异常+其他），完全驴唇不对马嘴。"""
    src = (r.get('src') or '').replace("'", '')
    eid = r.get('eid')
    inner = (f'<Select Path="System">*[System['
             f"Provider[@Name='{src}'] and (EventID={eid})]]</Select>")
    txt = re.sub(r'[\\/:*?"<>|]', '',
                 (r.get('txt') or f'事件{eid}').split('（')[0].strip())
    title = f'DeskBits {txt}'
    try:
        _write_custom_view(title, inner)
    except Exception as e:
        logger.warning(f'事件同类视图写入失败: {e}')
    _start_eventvwr()
    gui._log(f'📜 已打开事件查看器（自定义视图 → Views → "{title}"：'
             f'{src} 的 {eid} 号事件）')
    messagebox.showinfo(
        i18n.tr('已在事件查看器中打开', 'Opened in Event Viewer'),
        i18n.tr(f'事件查看器已打开，左侧 自定义视图 → Views →\n'
                f'"{title}" 就是这类事件的过滤视图\n'
                f'（{src} · 事件 ID {eid}，与"查看同类事件"脚本同口径）。',
                f'Event Viewer opened. Custom Views → Views →\n'
                f'"{title}" (same provider+ID as the script).'),
        parent=gui.root)


def _start_eventvwr():
    """★ R186b：打开事件查看器（普通打开）。

    实测 eventvwr.exe 的位置参数是"远程计算机名"（/COMPUTER:xxx），
    不是自定义视图名——没有命令行直达视图的能力，只能靠弹窗指路。"""
    try:
        os.startfile('eventvwr.msc')
        return True
    except Exception as e:
        logger.warning(f'eventvwr 打开失败: {e}')
        return False


# ★ R186c：原 _open_event_in_viewer（双击单 ID 开视图）已删——双击自
# R163 起走详情对话框，此函数无调用方；win11 上遗留的 DeskBits_事件*.xml
# 空视图文件即老版本所写，点一次"🌐 打开事件查看器（按当前筛选生成
# 视图）"即以新格式覆盖，或手动删 Views 文件夹里的 DeskBits 文件


def _open_evt_filter_in_viewer(gui):
    """★ R186（用户 01 批"在 Windows 查看器中打开这类日志，自动加过滤"）：
    按列表当前筛选状态（勾选分类+典型 ID 组）生成事件查看器"自定义视图"
    XML 并打开 eventvwr——左侧 自定义视图 出现 DeskBits 视图，用户直接在
    原生查看器里看过滤好的同类事件。"""
    id_only = getattr(gui, '_evt_id_only', None)
    flt = getattr(gui, '_evt_filter', None) or {}
    cats = tuple(k for k, v in flt.items() if v.get())
    if id_only:
        ids = sorted(set(id_only))
        # ★ R186c：标题带典型名（"DeskBits 磁盘"），用户一眼对上
        title = 'DeskBits ' + (getattr(gui, '_evt_preset_name', '')
                               .replace('/', '') or '典型筛选')
    elif cats:
        ids = sorted(eid for eid, ent in _EVENT_MAP.items()
                     if ent[1] in cats)
        _cn = {'boot': '开关机', 'bad': '异常', 'other': '其他'}
        title = 'DeskBits ' + '+'.join(_cn.get(c, c) for c in cats)
    else:
        messagebox.showinfo(
            i18n.tr('提示', 'Notice'),
            i18n.tr('分类全部未勾选（列表为空），没有可打开的筛选。',
                    'No category selected; nothing to open.'),
            parent=gui.root)
        return
    id_cond = _evt_querylist(ids)   # ★ R188：带 Provider 条件的分组 Select
    try:
        # ★ R186b：规范 XML（QueryNode 结构）；R188：来源家族过滤
        _write_custom_view(title, id_cond)
    except Exception as e:
        logger.warning(f'事件筛选自定义视图写入失败: {e}')
        messagebox.showwarning(
            i18n.tr('提示', 'Notice'),
            i18n.tr(f'自定义视图写入失败（{e}），只打开查看器不带过滤。',
                    f'Custom view write failed ({e}); opening plain viewer.'),
            parent=gui.root)
    _start_eventvwr()
    gui._log(f'📜 已打开 Windows 事件查看器（左侧 自定义视图 → Views → '
             f'"{title}"，含 {len(ids)} 个事件 ID）')
    messagebox.showinfo(
        i18n.tr('已在事件查看器中打开', 'Opened in Event Viewer'),
        i18n.tr(f'事件查看器已打开并定位到视图"{title}"\n'
                f'（若没自动跳转：左侧 自定义视图 → Views → "{title}"）。\n'
                f'该视图按 {len(ids)} 个事件 ID 过滤。',
                f'Event Viewer opened at view "{title}"\n'
                f'(or find it under Custom Views → Views).\n'
                f'Filtered by {len(ids)} event IDs.'),
        parent=gui.root)


# =====================================================================
# ★R241（用户 33 批）计划任务：路径清洗 / 建后回读 / 延时 / 备注带 VM 名
# 一组模块级纯函数。放模块级是为了**能被门禁直接 exec 调用**（藏在嵌套
# 作用域里的话测试只能靠字符串猜，R240 的教训）。
# =====================================================================

_QUOTE_PAIRS = (('"', '"'), ('“', '”'), ('「', '」'), ('『', '』'),
                ('"', '"'))


def _clean_task_path(raw):
    """剥掉路径首尾**成对**的引号与空白——用户从资源管理器右键"复制为路径"
    拿到的就是 `"D:\\xx\\yy.bat"`，而代码又要给它包一层引号交给 schtasks，
    结果存进任务的动作被拆成 `<Command>""</Command>` + 一截尾巴，
    到点必然失败（本机实存活任务 2026-09-25 07:20:20 返回 0x80070057 实锤，
    并按同写法复现）。剥到没有成对引号为止，兼容叠了两层和全角引号；
    只有一侧引号（手打漏一半）同样剥掉。"""
    s = (raw or '').strip()
    changed = True
    while changed and s:
        changed = False
        for a, b in _QUOTE_PAIRS:
            if len(s) >= 2 and s.startswith(a) and s.endswith(b):
                s = s[len(a):len(s) - len(b)].strip()
                changed = True
                break
        if not changed and (s[:1] in ('"', '“', '「', '『', '"')
                            or s[-1:] in ('"', '”', '」', '』', '”')):
            s = s.strip('“”「」『』"').strip()
            changed = True
    return s


class _BusyWait:
    """★R251（07 批 item2a「创建的时候需要提示'请稍等，正在…'，要不用户
    不知道自己点了还是没点」）：主线程同步做慢操作时的「⏳ 请稍等」小窗。

    为什么不开线程：红线「Tk 控件调用绝不进后台线程」，而这里慢的本就是
    subprocess（schtasks / PowerShell，冷启动一条 4~10 秒），它一直在主线程
    跑。所以做成「先把窗画出来 → 分阶段改文案 → 每阶段 update() 一下」，
    全程单线程，不引入任何跨线程 Tk 调用。

    出口纪律（item2d「添加成功没有提示」）：close() 幂等；实例挂在
    `gui._busy_wait` 上，创建入口最外层兜底里还有 `_busy_close(gui)`，
    于是 成功 / 失败 / 回读不符 / 抛异常 四条出口都不会留下一个转圈不停的
    小窗，也不会出现"静悄悄没终态提示"。
    """

    def __init__(self, gui, text, sub=''):
        self._win = None
        self._txt = None
        try:
            root = getattr(gui, 'root', None)
            if root is None:
                return
            win = tk.Toplevel(root)
            win.overrideredirect(True)
            win.attributes('-topmost', True)
            win.resizable(False, False)
            win.configure(bg=COLORS['surface'])
            f = tk.Frame(win, bg=COLORS['surface'], padx=18, pady=14)
            f.pack()
            self._txt = tk.Label(f, text=text, bg=COLORS['surface'],
                                 fg=COLORS['fg'],
                                 font=ui_layout.app_font(11, 'bold'),
                                 justify=tk.CENTER)
            self._txt.pack()
            if sub:
                tk.Label(f, text=sub, bg=COLORS['surface'],
                         fg=COLORS['text_secondary'],
                         font=ui_layout.app_font(9),
                         justify=tk.CENTER).pack(pady=(6, 0))
            win.update_idletasks()
            w = max(win.winfo_reqwidth(), ui_layout.scaled_size(240))
            h = win.winfo_reqheight()
            try:
                px, py = root.winfo_rootx(), root.winfo_rooty()
                pw, ph = root.winfo_width(), root.winfo_height()
            except Exception:
                px = py = 0
                pw = ph = ui_layout.scaled_size(900)
            win.geometry('%dx%d+%d+%d' % (w, h,
                                          px + max(0, (pw - w) // 2),
                                          py + max(0, (ph - h) // 3)))
            self._win = win
            gui._busy_wait = self
            win.update()          # 立刻画出来，别让界面看起来"点了没反应"
        except Exception as e:    # 画不出来也不能挡住创建本身
            logger.debug('忙碌窗创建失败（不影响流程）: %s' % e)

    def set(self, text):
        """改文案并强制重绘——慢操作分阶段调用，用户才看得见"进度在动"。"""
        if self._win is None:
            return
        try:
            self._txt.configure(text=text)
            self._win.update_idletasks()
            self._win.update()
        except Exception:
            pass

    def close(self):
        win, self._win, self._txt = self._win, None, None
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass


def _busy_open(gui, text, sub=''):
    """开忙碌窗（已有先关，防连点叠窗）。返回 _BusyWait 实例（可能画不出来，
    但对象总在，调用方可以无脑 `wb.set(...)` / `_busy_close(gui)`）。"""
    _busy_close(gui)
    return _BusyWait(gui, text, sub)


def _busy_close(gui):
    """关当前忙碌窗（幂等：没挂、已关、窗已销毁都不炸）。"""
    box = getattr(gui, '_busy_wait', None)
    if box is None:
        return
    try:
        box.close()
    except Exception as e:
        logger.debug('关闭忙碌窗异常: %s' % e)
    finally:
        gui._busy_wait = None


def _parse_startup_delay(txt, unit):
    """登录/开机延后启动：`(秒, 错误)`。

    ★R251 item2c（用户 43 批「计划任务页面的延时单位加个天」）：单位多一档「天」。
    上限**按单位分档**，不把"秒/分/时"那档原来 23 小时的封顶一并放宽——
    老闸 `test_r241_tasks` B7 钉着「24 小时要报错」（`_pd('24','小时')[1]`），
    一刀切提到 30 天会让它假红；正确口径是**加一档、不动老一档**：
      · 秒 / 分钟 / 小时 → 1 秒 ~ 82800 秒（23 小时，与历轮一致）
      · 天 → 1 ~ 30 天（再长就该改用"每天/每隔 N 天"这种周期触发，不是延时）
    越界/非法**明确报错**，不静默截断。单位同时认中英文（English 档下拉里是
    s/min/h/day）。"""
    try:
        n = int(str(txt).strip())
    except (TypeError, ValueError):
        return 0, '延后启动要填整数（秒/分钟/小时/天）'
    u = (unit or '').strip().lower()
    if u in ('天', '日', 'd', 'day', 'days'):
        if n < 1 or n > 30:
            return 0, '按"天"延后启动要在 1 ~ 30 天之间（现在 = %d 天）' % n
        return n * 86400, ''
    mult = 1
    if u in ('秒', '秒(s)', 's', 'sec', 'secs', 'second', 'seconds'):
        mult = 1
    elif u in ('分钟', '分', 'm', 'min', 'minute', 'minutes', '分钟(min)'):
        mult = 60
    elif u in ('小时', '时', 'h', 'hr', 'hrs', 'hour', 'hours', '小时(h)'):
        mult = 3600
    else:
        return 0, '延后启动单位不认识：%s' % unit
    sec = n * mult
    if sec < 1 or sec > 82800:
        return 0, '延后启动要在 1 秒 ~ 23 小时之间（现在 = %d 秒）' % sec
    return sec, ''


def _pt_duration(sec):
    """秒 → ISO-8601 时长（调度器 Delay 字段格式）。
    90→PT1M30S、120→PT2M、3600→PT1H、3661→PT1H1M1S、0→''（0 不该写 Delay）。
    ★R251 item2c：能吃下"天"了 ⇒ 86400→P1D、90000→P1DT6H（先拆天再拆时分秒，
    没有天时仍是纯 PT…，历轮 B8~B10b 四条判据一个字都不受影响）。
    取证（run/r251/delay_day_probe.txt，本机只读，未注册任务）：
    `New-ScheduledTaskTrigger -AtLogOn` 的 `.Delay` 赋 'P1D'/'P3D'/'P1DT2H'
    都收，赋 'P2D' 读回就是 'P2D' ⇒ 天这一档是调度器原生支持的。
    取证（run/r240b/tr_quote_probe3.txt）：`New-ScheduledTaskTrigger
    -AtStartup/-AtLogOn` 的对象**有 Delay 属性**，赋 PT90S/PT2M 后
    Register-ScheduledTask 建出来读回 PT1M30S/PT2M ⇒ 延时交给调度器，
    不往 bat 里塞 sleep（`timeout` 在无控制台场景会静默不等待）。"""
    sec = int(sec or 0)
    if sec <= 0:
        return ''
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    out = 'P'
    if d:
        out += '%dD' % d
    tpart = ''
    if h:
        tpart += '%dH' % h
    if m:
        tpart += '%dM' % m
    if s:
        tpart += '%dS' % s
    if tpart:
        out += 'T' + tpart
    return out if len(out) > 1 else ''


def _ps_q(s):
    """PowerShell 单引号串内安全：单引号翻倍"""
    return str(s or '').replace("'", "''")


def _ps_readback_cmd(name, expect, expect_arg=''):
    """生成"回读任务实际动作"的 PowerShell 串。

    输出三态：`OK` / `MISMATCH:<实读到的东西>` / `MISSING`（任务或动作读不到）。
    比对前两侧都 Trim + 去外层引号 + 忽略大小写——schtasks 会把 `"路径"` 连引号
    一起存进 `<Command>`，这是正常形态（本机取证 A/A2），不能当不匹配。
    异常路径一律 `exit 1` 且把原因打到 stderr：拿不到证据时**不许**当成功。

    ★R275（item2 隐藏黑窗）第三参 `expect_arg`：**只在非空时**才拼
    `$a.Arguments` 那两段。默认空 ⇒ 生成的串与批前**逐字节等价**（自启那条
    路 1227 行只传两参，绝不能被本批改出行为差异）。为什么非加不可：动作被
    换成 `wscript.exe "<启动器>.vbs"` 后，`<Command>` 里恒是系统那个
    wscript，只比 Execute 会把任何一条启动器都核成"对上了"（假绿），
    真正的目标在 `<Arguments>` 里。"""
    _ag = ''
    _ok = ("if ($got -ieq $exp) { Write-Output 'OK'; exit 0 } "
           "else { Write-Output ('MISMATCH:' + $got); exit 3 }")
    if expect_arg:
        _ag = (
            "$ga = ('' + $a.Arguments).Trim(); "
            "if ($ga.Length -ge 2 -and $ga.StartsWith('\"') -and "
            "$ga.EndsWith('\"')) { $ga = $ga.Trim('\"').Trim() }; "
            "$ea = '%s'.Trim(); "
            "if ($ea.Length -ge 2 -and $ea.StartsWith('\"') -and "
            "$ea.EndsWith('\"')) { $ea = $ea.Trim('\"').Trim() }; " % (
                _ps_q(expect_arg)))
        _ok = ("if ($got -ieq $exp -and $ga -ieq $ea) { "
               "Write-Output 'OK'; exit 0 } "
               "else { Write-Output ('MISMATCH:' + $got + ' ' + $ga); "
               "exit 3 }")
    return ("$ErrorActionPreference='Stop'; "
            "try { "
            "$t = Get-ScheduledTask -TaskName '" + _ps_q(name) + "' "
            "-ErrorAction Stop; "
            "$a = @($t.Actions) | Where-Object { $_.Execute } | "
            "Select-Object -First 1; "
            "if (-not $a) { Write-Output 'MISSING'; exit 4 }; "
            "$got = $a.Execute.Trim(); "
            "if ($got.Length -ge 2 -and $got.StartsWith('\"') -and "
            "$got.EndsWith('\"')) { $got = $got.Trim('\"').Trim() }; "
            "$exp = '" + _ps_q(expect) + "'.Trim(); "
            "if ($exp.Length -ge 2 -and $exp.StartsWith('\"') -and "
            "$exp.EndsWith('\"')) { $exp = $exp.Trim('\"').Trim() }; "
            "if ($got -eq '') { Write-Output 'MISMATCH:'; exit 3 }; "
            + _ag + _ok + " } "
            "catch { Write-Error ($_ | Out-String); exit 1 }")


def _ps_delayed_register(name, tr_val, wd, trig_v, pt):
    """生成"登录/开机 + 原生延时"的 Register-ScheduledTask PowerShell 串。

    ★R241（用户 item1b「需要加个总的延时自定义的设置，或者你出个更好的方案」）：
    延时交给任务计划程序自己（`$t.Delay`），不塞进 bat——bat 里的 `timeout`
    在没有控制台时会**静默不等待**，等于埋了个假延时。
    `-Delay` 参数这台机器上的模块版本没有（取证⑤：报"找不到与参数名称 Delay
    匹配的参数"），所以只能拿对象再赋属性。
    tr_val 形如 `'"exe路径"'` 或 `'"exe" 参数'`；工作目录空串时**不写**
    `-WorkingDirectory ''`（PS 参数校验拒空串，实测）。"""
    tv = (tr_val or '').strip()
    if tv.startswith('"'):
        _e1 = tv.find('"', 1)
        if _e1 > 0:
            exe, arg = tv[1:_e1], tv[_e1 + 1:].strip()
        else:
            exe, arg = tv.strip('"'), ''
    else:
        _p2 = tv.split(None, 1)
        exe = _p2[0] if _p2 else ''
        arg = _p2[1] if len(_p2) > 1 else ''
    _a = "New-ScheduledTaskAction -Execute '%s'" % _ps_q(exe)
    if arg:
        _a += " -Argument '%s'" % _ps_q(arg)
    if wd:
        _a += " -WorkingDirectory '%s'" % _ps_q(wd)
    _trig = ('New-ScheduledTaskTrigger -AtStartup' if trig_v == 'boot'
             else 'New-ScheduledTaskTrigger -AtLogOn')
    return ("$ErrorActionPreference='Stop'; "
            "$t = %s; $t.Delay='%s'; "
            "$a = %s; "
            "Register-ScheduledTask -TaskName '%s' -Action $a -Trigger $t "
            "-Force | Out-Null") % (_trig, _ps_q(pt), _a, _ps_q(name))


def _split_action(tv):
    """动作串 → `(可执行文件, 参数)`。

    ★R277：旧口径对**没加引号、路径本身含空格**的动作串按第一个空格切，
    于是真机日志每次刷新都刷 `计划任务动作文件已失效 … → C:\\ProgramData\\Qoder`
    ——那条任务实际指向 `C:\\ProgramData\\Qoder CN\\Qoder CN Launcher\\…exe`。
    误报的死链会把好任务标橙、把用户吓去删，所以这里改成：
      1) 带引号 ⇒ 原口径（回读核对 `_ps_readback_cmd` 依赖这个 split 位置）；
      2) 第一段本身就是盘上真实文件 ⇒ 照旧（绝大多数动作长这样，一次 stat 搞定）；
      3) 否则从最长的前缀往回找**盘上存在的最长前缀**当 exe；
      4) 全都不存在（真·死链）⇒ 退回第一段，绝不返回空串把"死链"判成"没动作"。"""
    s = (tv or '').strip()
    if not s:
        return '', ''
    if s.startswith('"'):
        e = s.find('"', 1)
        if e > 0:
            return s[1:e], s[e + 1:].strip()
        return s.strip('"'), ''
    parts = s.split()
    first = parts[0]
    try:
        if len(parts) == 1 or os.path.isfile(first):
            return first, ' '.join(parts[1:]).strip()
    except OSError:
        return first, ' '.join(parts[1:]).strip()
    for i in range(len(parts), 1, -1):
        cand = ' '.join(parts[:i])
        try:
            if os.path.isfile(cand):
                return cand, ' '.join(parts[i:]).strip()
        except OSError:
            break
    return first, ' '.join(parts[1:]).strip()


def _exe_of(tv):
    """`'"C:\\a b\\x.exe" -arg'` → `C:\\a b\\x.exe`（与
    `_ps_delayed_register` 的拆分口径一致，回读核对要用同一个值比）。"""
    return _split_action(tv)[0]


def _arg_of(tv):
    """同 `_exe_of` 的拆分位置，取**参数**那半段（没参数给空串）。"""
    return _split_action(tv)[1]


# ============================================================================
# ★R275（用户 2026-9-29 06 批 item2）：隐藏黑窗启动器
# ----------------------------------------------------------------------------
# 用户原话：「启动虚拟机……尽量不要出现黑窗，有点难看。其他启动任务，除了
# 必须的 cmd 外，能不显示最好不显示。」
# 病根：任务计划程序跑 `.bat` 时**必然**给 cmd.exe 开一个控制台窗口，而
# `schtasks /tr` 里没有任何"窗口样式"可传（窗口那格在 GUI 编辑器的"配置"
# 页才有，命令行/PS 走 `New-ScheduledTaskAction` 也没有这个属性）。
# 不引第三方 exe、又保住「上次结果＝真返回码」的做法只有一个：动作改指
# `wscript.exe "<启动器>.vbs"`，VBS 里 `Shell.Run` 用**三引号包住 bat 路径**
# （VBS 的字符串里嵌一个引号要写两个），再 `Run(…, 0, True)`——
# 0＝隐藏窗口、True＝等 bat 跑完，再 `WScript.Quit rc` 把 bat 的返回码交回
# wscript ⇒ 列表里那一列不会变成假的 0。
#
# 改这里必须同时保住三条既有链路（少一条就是"修一个黑窗毁三处"）：
#   1) **死链检测**：`_act_missing`/`_tr_exe` 要指向**真正的 bat**，不是
#      wscript.exe——系统那个文件永远在盘上，指向它等于死链永远测不出（★R271
#      那条"升级换目录后静默不启动"的教训会被悄悄抹掉）；
#   2) **备注反解**：虚拟机名是从**文件名**里解出来的（★R241 item1a），所以
#      启动器文件名沿用目标 stem，且 `_prog_name` 要先解开启动器；
#   3) **动作回读核对**（★R241 硬纪律）：schtasks 会把 vbs 路径存进
#      `<Arguments>`，只比 `$a.Execute` 会把 `wscript.exe` 核成"对上了"
#      ⇒ 必须连 `$a.Arguments` 一起比（见 `_ps_readback_cmd` 第三参）。
# ============================================================================

_HL_DIR = 'hidden_launchers'
_HL_EXES = ('wscript.exe', 'cscript.exe')
# VBS 里嵌一个引号要写两个，路径两侧各一个 ⇒ 连排三个。**源码里不写成字面
# 三引号**：本仓多把门禁（`test_r250`/`test_r251` 的 `code_only`）用启发式
# 正则剔 docstring，源文件里冒出连续三个双引号会把它后面整段代码当字符串吞掉
# ——★R275 实测因此红掉 12 条与本批无关的判据。
_Q3 = '"' * 3


def _launcher_dir(create=True):
    """启动器（.vbs）目录：放在启动 bat 同一个家下面，便于一起清算。
    拿不到目录返回空串——调用方据此**放弃隐藏**而不是让创建失败。"""
    base = _vm_bat_dir(create)
    if not base:
        return ''
    p = os.path.join(base, _HL_DIR)
    try:
        if create:
            os.makedirs(p, exist_ok=True)
        return p
    except OSError:
        logger.warning(f'隐藏启动器目录建不出来: {p}', exc_info=True)
        return ''


def _wscript_path():
    """wscript.exe 的绝对路径；盘上找不到就返回空串（不隐藏，直接跑 bat）。"""
    w = os.environ.get('WINDIR') or r'C:\Windows'
    for p in (os.path.join(w, 'System32', 'wscript.exe'),
              os.path.join(w, 'wscript.exe')):
        try:
            if os.path.isfile(p):
                return p
        except OSError:
            pass
    return ''


def _hide_vbs_body(target):
    """隐藏启动器正文；目标路径**含双引号**⇒ 返回 ''（VBS 串塞不进去，
    与其写出一份跑不了的文件，不如保留原来的黑窗）。"""
    t = str(target or '').strip()
    if not t or '"' in t:
        return ''
    q = t.replace('"', '""')
    return ('Set s = CreateObject("WScript.Shell")\r\n'
            'rc = s.Run(%s%s%s, 0, True)\r\n'
            'WScript.Quit rc\r\n' % (_Q3, q, _Q3))


def _launcher_name(target, stem=None):
    """启动器文件名＝**目标 stem** + 目标完整路径的 8 位哈希。
    stem 保住"备注能从文件名认出是哪台虚拟机"；哈希防两个不同目录下的
    同名 bat 共用一份启动器（共用＝后写的把前一条任务的动作悄悄换掉，
    到点起错程序，比黑窗严重得多）。"""
    import hashlib
    h = hashlib.sha1(os.path.normcase(str(target or '')).encode(
        'utf-8', errors='replace')).hexdigest()[:8]
    base = stem or os.path.splitext(os.path.basename(str(target or '')))[0]
    base = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', str(base or '')).strip()
    return '%s_%s.vbs' % ((base or 'task')[:60], h)


def _is_hide_launcher(vbs_path):
    """这份 .vbs 是不是我们自己造的隐藏启动器（按目录名认，不看内容）。"""
    low = str(vbs_path or '').replace('/', '\\').lower()
    return low.endswith('.vbs') and ('\\' + _HL_DIR + '\\') in low


def _launcher_target(vbs_path):
    """从启动器正文里解出它要跑的东西；读不到（文件被删/手工改过）给 ''。
    **不靠文件名猜**：启动器同名 .bat 可能早被 30 天清算删掉了。"""
    try:
        with open(vbs_path, 'r', encoding='mbcs', errors='replace') as f:
            body = f.read()
    except (OSError, ValueError):
        return ''
    m = re.search(r'Run\s*\(' + _Q3 + r'(.*?)' + _Q3 + r'\s*,\s*0',
                  body, re.IGNORECASE)
    return m.group(1).replace('""', '"').strip() if m else ''


def _need_hide(target):
    """这个目标到点会不会开出黑窗：**只有 cmd 系脚本会**。
    exe 本来就没有控制台、vbs 由 wscript 起本就隐藏、ps1 有自己的关联方式，
    给它们套一层启动器只是多一种坏法（用户原话留了口径："除了必须的 cmd 外"）。"""
    return str(target or '').lower().endswith(('.bat', '.cmd'))


def _hide_launcher(target, stem=None):
    """给 target 造一份隐藏启动器，返回该写进 `schtasks /tr` 的动作串；
    任何一步不成一律返回 '' —— 调用方**保留原动作**（黑窗只是难看，
    任务不跑是另一回事）。"""
    d = _launcher_dir()
    body = _hide_vbs_body(target)
    ws = _wscript_path()
    if not d or not body or not ws:
        return ''
    vp = os.path.join(d, _launcher_name(target, stem))
    try:
        # newline='' 是必需的：正文里已经写了 `\r\n`，文本模式再翻译一次
        # 就变成 `\r\r\n`（实测读回来每行中间夹一空行）
        with open(vp, 'w', encoding='mbcs', newline='') as f:
            f.write(body)
    except (OSError, ValueError) as e:
        # ★R285-N4：兜底原先只写 `except OSError`，而 `encoding='mbcs'` 在
        # `write()` 阶段抛的是 **UnicodeEncodeError（ValueError 的子类）**——
        # 目标路径里只要有一个码页外字符（用户给虚拟机/脚本起 emoji 名很常见）
        # 就逃出兜底：新建任务弹"创建过程出错"、✏️ 编辑保存点了没反应。
        # 更糟的是 `open()` 那一步已经把文件建好了 ⇒ 启动器目录里留下一份
        # 0 字节 .vbs 残尸（`_launcher_target` 从它解出空串，死链判定跟着歪）。
        # 所以两类一起兜，并把残尸扫掉；扫不掉也要留一行痕，不许静默。
        gone = True
        try:
            if os.path.exists(vp):
                os.remove(vp)
        except OSError as e2:
            gone = False
            logger.warning(f'隐藏启动器残尸没能删掉（可手工清理）: {vp} ({e2})')
        logger.warning(f'隐藏启动器写不出来，保留直接跑原文件: {target} '
                       f'({type(e).__name__}' + ('' if gone else ', 残尸未清') + ')',
                       exc_info=True)
        return ''
    tv = '"%s" "%s"' % (ws, vp)
    if len(tv) > 250:
        # schtasks /tr 上限 261 字符（★R185 就是为此才写 bat 的）
        logger.warning(f'隐藏启动器动作过长（{len(tv)} 字符），保留直接跑原文件')
        return ''
    return tv


def _unwrap_action(run):
    """任务动作串 → `(真正要跑的东西, 是不是我们的隐藏启动器)`。

    `'"…wscript.exe" "…\\hidden_launchers\\x.vbs"'` → `('…\\x.bat', True)`；
    其余一律 `(那一段的 exe, False)`。`_tr_exe` 走这个口径 ⇒ 死链检测、
    备注反解都自动跟上，不用每个调用点各解一遍。"""
    s = (run or '').strip()
    exe = _exe_of(s)
    if not exe.lower().endswith(_HL_EXES):
        return exe, False
    vbs = _arg_of(s).strip('"').strip()
    if not _is_hide_launcher(vbs):
        return exe, False
    return _launcher_target(vbs) or vbs, True


def _tr_exe(tv):
    """任务动作里**真正要跑的那个文件**（隐藏启动器要解到背后的 bat）。"""
    return _unwrap_action(tv)[0]


def _hide_action(tr_str, stem=None):
    """动作串 → `(该写进 schtasks /tr 的动作串, 有没有套隐藏启动器)`。

    ★R277：这是"要不要隐藏黑窗"的**唯一口径**——创建、✏️ 编辑保存、存量任务
    迁移三处共用。★R275 那版把判断散在创建路里，结果编辑保存逐字透传、
    存量任务没人管 ⇒ 用户 09-30 实测「跑 .bat 还是有 cmd 黑窗口」。

    只做"裸跑 bat/cmd"这一种：
      · **带参数**的不动——重写参数引号比留个黑窗更坑（套错了任务直接不跑）；
      · 已经是隐藏启动器的不动（幂等，防每次刷新都改一遍任务）；
      · exe/vbs/ps1 不套（用户口径"除了必须的 cmd 外"，多一层文件多一层坏法）；
      · 目标**不在盘上**不套：那本来就该被死链检测标橙报警，给它造启动器
        等于凭空多一份文件，还会把"到点什么都不做"这件事糊过去；
      · 启动器造不出来 ⇒ 原样返回（难看比断链好修）。"""
    raw = tr_str if isinstance(tr_str, str) else str(tr_str or '')
    exe, arg = _split_action(raw)
    if not exe or arg:
        return raw, False
    if _unwrap_action(raw)[1]:
        return raw, False
    if not _need_hide(exe):
        return raw, False
    try:
        if not os.path.isfile(exe):
            return raw, False
    except OSError as _e:
        # ★R277-S6 返修：这条兜底原先静默吞（黑窗没隐藏、日志零痕迹，下一轮只能
        # 重猜）。吞可以，留痕必须有。
        logger.warning(f'隐藏启动器判定失败（读不动目标文件，按不套处理）: '
                       f'{exe} ({_e})')
        return raw, False
    tv = _hide_launcher(exe, stem)
    if not tv:
        return raw, False
    return tv, True


def _hide_action_for_task(name, tr_str, stem=None):
    """**编辑保存 / 存量迁移**共用的入口：只有我们自己的任务才套隐藏启动器。

    ★R285-N5：★R277 把判断收进 `_hide_action` 时，"这条任务是不是我们的"那道筛
    只留在了存量迁移（:1262，`DeskBits_` 开头）——**编辑保存没有**。于是用户在
    列表里选中一条别家软件建的每日 bat 任务改个时间点，保存就把它的动作换成了
    `wscript.exe "…我们目录里的 x.vbs"`：那是**改别人的任务**，比留个黑窗严重——
    启动器受 30 天清算，到期一删，别人那条任务到点就什么都不做，而用户以为是
    自己没存好。名字口径与迁移那一处**逐字一致**（strip + lstrip('\\') + 前缀）。
    **新建**路不经过这里：任务名由我们自己按 `DeskBits_` 生成，天然属于我们。"""
    nm = str(name or '').strip().lstrip('\\')
    if not nm.startswith('DeskBits_'):
        raw = tr_str if isinstance(tr_str, str) else str(tr_str or '')
        return raw, False
    return _hide_action(tr_str, stem)


def _migrate_bare_bats(rows, runner=None):
    """把**存量**的"裸 bat/cmd"任务动作改成隐藏启动器（★R277 第二个洞）。

    ★R275 之前建的任务，动作里存的还是 `"X.bat"`，到点照弹黑窗；用户不会
    为了去掉一个黑窗把每条任务删了重建。所以在用户**主动点刷新**时顺手改一次。

    只碰同时满足四条的：① 名字是 `DeskBits_` 开头（我们建的，别人的任务一个字
    不动）；② 动作能解出 exe 且**没有参数**；③ 目标文件**当下真的在盘上**
    （死链那条该继续被标橙报警，不是迁移的料）；④ 还没套过启动器。
    `runner` 只为门禁可注桩；默认走 `_schtasks_run`。返回迁移过的任务名列表。"""
    out = []
    run = runner or _schtasks_run
    for r in rows or []:
        try:
            name = str((r or {}).get('name') or '').strip()
            if not name.lstrip('\\').startswith('DeskBits_'):
                continue
            tv, hidden = _hide_action((r or {}).get('run') or '')
            if not hidden:
                continue
            tgt = _tr_exe(tv)          # 引号/参数都不在，解出来就是 bat 本身
            if not tgt or not os.path.isfile(tgt):
                continue
            ok, msg = run(['schtasks', '/change', '/tn', name, '/tr', tv])
            if ok:
                out.append(name)
                r['run'] = tv      # 让本轮列表/编辑框立刻按新动作显示
                logger.info(f'★R277 存量任务已改走隐藏启动器（黑窗消失）: {name}')
            else:
                logger.warning(f'存量任务改隐藏启动器失败（保留原动作）: '
                               f'{name} — {str(msg)[:120]}')
        except Exception as e:
            logger.warning(f'存量任务迁移单条失败（跳过这条）: {e}')
    return out



# ==================== ★R247（用户 39 批 item2）：电源条件 ====================
# 用户体感：「vmware、v2rayN 有时候能启动有时候不能启动了……'带交流电源'能
# 启动，'电池电源'不让启动。我觉得默认去掉这个条件。……先把 仅交流电源可用，
# 电池可用 这两个选项先做出来。要不笔记本上忘记插电源就不能用了。」
# 根因查实：任务计划程序给**新任务的默认值**就是 `DisallowStartIfOnBatteries
# = true`（"使用电池时，不启动此任务"），schtasks 与 New-ScheduledTaskSettingsSet
# 都带这个默认；★R171 当年只给"关机/重启"两类动作清过它，"打开软件/运行脚本/
# 打开虚拟机"三类**从来没清** ⇒ 笔记本不插交流电时到点系统直接不起任务，
# 而列表里又看不见这个条件 ⇒ 表现成"时好时坏、根因不可见"。
# 本批只做用户点名的这两条（网络可用/空闲等其余条件按 item2 要求"存档"）。

def _bat_cn(v):
    """电池条件字段的显示文案（列表新列「电池下不启动」）。

    读不到（老数据、schtasks 回退路、PS 没跑出来）给中性占位 `—`——
    **没证据就不许下"不受限"的结论**（★R241 同一口径）。
    纯函数，判据在 `测试_ui/test_r247_power_conditions.py` D 段。"""
    s = str(v or '').strip().lower()
    if s in ('yes', 'true', '1'):
        return '⚠ 电池下不启动'
    if s in ('no', 'false', '0'):
        return '插不插都运行'
    return '—'


def _ps_power_cmd(name, ac_only, stop_on_battery, catch_up=False, wake=False):
    """生成"设 + 回读"电源条件的 PowerShell 串（只给
    `_apply_power_settings` 用，别处不要再拼第二份）。

    为什么只能走 PowerShell：`schtasks /create|/change` **没有**对应参数，
    这些条件挂在任务的 Settings 上，只有 `Set-ScheduledTask` 改得动。
    回读是硬要求（★R241 纪律）：设完重新 `Get-ScheduledTask` 读回来比对，
    一致才输出 `OK|<ac>|<stop>`，不一致输出 `MISMATCH:...` 并 exit 3，
    异常一律 exit 1 —— **拿不到证据不许当成功**。

    ★R256（#220）：后两条「错过补跑 StartWhenAvailable / 允许唤醒 WakeToRun」
    是**三态**参数——只有 True 才写入；False/None **连属性名都不出现**。
    📝 编辑路与自启路不传这两个参数 ⇒ 生成的串和批前**逐字节相同**，
    绝不会把用户已在任务计划程序（settings）里勾好的旗标静默抹掉（电池两条
    不受影响：创建器默认全不勾，把它们显式清成 $false 正是 ★R247 用户点名的
    "默认去掉电池限制"）。判据：test_r256_miss_wake P1/P5（批前金标准）。"""
    _ac = '$true' if ac_only else '$false'
    _sb = '$true' if stop_on_battery else '$false'
    _ea = 'True' if ac_only else 'False'
    _es = 'True' if stop_on_battery else 'False'
    _n = _ps_q(name)
    # ★R256 三态拼装：catch_up/wake 只在 True 时追加"赋值 + 回读比对"两段，
    # 默认串（两个都不勾/不传）与批前备份逐字节一致——新增行一个字都不许动
    # 既有部分（金标准 run/r256/_golden_ps.json 钉死）。
    _set = ''
    _rd = ''
    _cmp = " ($x -eq '%s' -and $y -eq '%s')" % (_ea, _es)
    if catch_up:
        _set += "$t.Settings.StartWhenAvailable = $true; "
        _rd += "$z = [string]$r.Settings.StartWhenAvailable; "
        _cmp = _cmp[:-1] + " -and $z -eq 'True')"
    if wake:
        _set += "$t.Settings.WakeToRun = $true; "
        _rd += "$w = [string]$r.Settings.WakeToRun; "
        _cmp = _cmp[:-1] + " -and $w -eq 'True')"
    return ("$ErrorActionPreference='Stop'; try { "
            "$t = Get-ScheduledTask -TaskName '%s' -ErrorAction Stop; "
            "$t.Settings.DisallowStartIfOnBatteries = %s; "
            "$t.Settings.StopIfGoingOnBatteries = %s; "
            + _set +
            "Set-ScheduledTask -InputObject $t | Out-Null; "
            "$r = Get-ScheduledTask -TaskName '%s'; "
            "$x = [string]$r.Settings.DisallowStartIfOnBatteries; "
            "$y = [string]$r.Settings.StopIfGoingOnBatteries; "
            + _rd +
            "if" + _cmp + " { "
            "Write-Output ('OK|' + $x + '|' + $y); exit 0 } "
            "Write-Output ('MISMATCH:' + $x + '|' + $y); exit 3 } "
            "catch { Write-Error ($_ | Out-String); exit 1 }") % (
                _n, _ac, _sb, _n)


def _read_power_flags(name, _run=None):
    """**只读**回当前两条件（📝 编辑框预填用），返回 `(ac_only, stop, ok)`。

    读不到 ⇒ `(False, False, False)`：`ok=False` 让调用方明确知道"这是没读到"
    而不是"读到了、确实没限制"，界面会写清楚并照常落盘（不拿默认值冒充实读）。
    超时给 20 秒（比设值的 45 秒紧）：这一下在**开窗口的同步路径**上，
    卡太久用户以为双击没反应。"""
    _run = _run or subprocess.run
    ps = ("$ErrorActionPreference='Stop'; try { "
          "$r = Get-ScheduledTask -TaskName '%s' -ErrorAction Stop; "
          "Write-Output ('PWR|' + "
          "[string]$r.Settings.DisallowStartIfOnBatteries + '|' + "
          "[string]$r.Settings.StopIfGoingOnBatteries); exit 0 } "
          "catch { Write-Error ($_ | Out-String); exit 1 }") % _ps_q(name)
    try:
        p = _run(['powershell', '-NoProfile', '-NonInteractive',
                  '-Command', ps], capture_output=True, timeout=20,
                 stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
        out = p.stdout.decode('gbk', errors='replace').strip()
    except Exception as e:
        logger.debug(f'读电源条件失败({name}): {e}')
        return False, False, False
    if p.returncode != 0 or not out.startswith('PWR|'):
        return False, False, False
    seg = out.split('|')
    if len(seg) < 3:
        return False, False, False
    return (seg[1].strip().lower() == 'true',
            seg[2].strip().lower() == 'true', True)


def _plog(gui, msg):
    """往界面日志写一行；`gui` 拿不到 `_log`（或残缺对象）也不能抛——
    自启/编辑/创建三条路共用本函数，日志失败不该把主流程带崩。"""
    try:
        if gui is not None and hasattr(gui, '_log'):
            gui._log(msg)
    except Exception:
        pass


def _apply_power_settings(gui, name, ac_only, stop_on_battery, _run=None,
                          catch_up=False, wake=False):
    """★R247 唯一写入者：把"电池两条件"写进任务并回读核对。

    返回 `(ok, 给用户看的话)`。三条创建路（延时 PS / 原生延时 PS / 普通
    schtasks）、随登录自启、📝 编辑任务**都只经这一个函数**动这两条
    （★R171 的关机/重启特例已并进来，源码里不再有第二份实现）。

    红线：**设条件失败绝不删任务**。任务已建成是事实，因为一个附属设置没写
    进去就把整条任务删掉，用户丢的比得到的多 ⇒ 只警告 + 落日志，让他自己在
    📝 编辑里重试（与"动作回读不符⇒删"不同：那条任务到点什么都不会发生，
    留着才是骗人）。"""
    _run = _run or subprocess.run
    # ★R248（用户 40 批 item0，win11 实测）：Windows 里"改用电池时停止"这一条
    # **依赖**"只在交流电才启动"——上面没勾、单独勾它不生效。界面已按依赖置灰，
    # 这里在唯一写入者再兜一道：ac_only=False ⇒ stop 一律按 False 写，且下面的
    # 描述文案同步走，绝不出现"界面/日志说设了电池停止、系统里其实没设"。
    stop_on_battery = bool(stop_on_battery) and bool(ac_only)
    # ★R256（#220）：三态参数——只有勾了才往下传（_ps_power_cmd 里 False
    # 连属性名都不出现）⇒ 不传就是"不动存量旗标"。★R271-S6 之后 📝 编辑路
    # 对「开机时」任务会显式传 catch_up=True（其余路、自启任务仍不传）。
    catch_up = bool(catch_up)
    wake = bool(wake)
    try:
        p = _run(['powershell', '-NoProfile', '-NonInteractive', '-Command',
                  _ps_power_cmd(name, ac_only, stop_on_battery,
                                catch_up=catch_up, wake=wake)],
                 capture_output=True, timeout=45,
                 stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
    except Exception as e:
        logger.warning(f'电源条件设置异常({name}): {e}')
        _plog(gui, f'   ⚡ 电源条件没设上（异常）：{e}')
        # 异常原文会进**弹窗**，所以这里也走 i18n.tr（★R229 口径：日志留中文、
        # 弹窗双语；漏了这步 English 档就会冒出一整句中文）。
        return False, i18n.tr('电源条件设置异常：' + str(e),
                              'Power-condition call failed: ' + str(e))
    out = (p.stdout.decode('gbk', errors='replace').strip()
           or p.stderr.decode('gbk', errors='replace').strip())
    # 返回值会进**弹窗**（弹窗必须双语，见 ★R229 口径：日志中文一个字不改、
    # 弹窗文案才走 i18n.tr），所以这里出 zh/en 两份；界面日志只写中文。
    _zh = '只在交流电（电源适配器）下运行' if ac_only else '插不插电源都运行'
    _en = 'runs only on AC power' if ac_only else 'runs on battery too'
    if stop_on_battery:
        # ★R250 三3：措辞与两枚复选同一口径——说清"停的是正在跑的那一次"
        _zh += '；改用电池供电时停止正在运行的任务'
        _en += ', stops a running task if switching to battery'
    # ★R256（#220）：两枚新复选的从句——字面与词典（utils/log_i18n 从句规则）、
    # 门禁（test_r256_miss_wake U4/L 段）三处逐字同源，改一处必须三处同改。
    if catch_up:
        _zh += '；错过计划后尽快补跑'
        _en += ', missed runs are caught up ASAP'
    if wake:
        _zh += '；允许唤醒计算机运行'
        _en += ', the computer may wake up for it'
    if p.returncode == 0 and out.startswith('OK'):
        logger.info(f'电源条件已设({name}): {_zh}（回读 {out}）')
        _plog(gui, f'   ⚡ 电源条件已设：{_zh}')
        return True, i18n.tr('⚡ 电源条件：' + _zh,
                             '⚡ Power conditions: ' + _en)
    logger.warning(f'电源条件未生效({name}) rc={p.returncode}: {out[:160]}')
    _why = (out[:120] or '无输出') if p.returncode == 0 else (out[:120] or '失败')
    _plog(gui, f'   ⚠ 电源条件没设上（任务照常存在）：{_why}')
    return False, i18n.tr('⚠ 电源条件没设上（任务照常创建/保留，'
                          '可在 📝 编辑里重试）：' + _why,
                          '⚠ Power conditions NOT applied (task kept; '
                          'retry via Edit): ' + _why)


# ★R241（用户 item1「随登陆自启动，这个需要有个复选项，用户自主决定」）：
# 自启任务用**固定名**——重复勾选不会叠出一串（R177 的教训），用户在
# 📅 计划任务页一眼认得出、删得掉。
SELFSTART_TASK = 'DeskBits_logon_open_selfstart'
_SELFSTART_DELAY_PT = 'PT15S'   # 登录后 15 秒再起（见 _apply_selfstart 注释）


def _selfstart_target():
    """"随登录自启"要跑的东西=软件自己。

    打包态必须拿**外层 exe**（Nuitka onefile 下 `sys.executable` 是缓存
    解压目录里的内层 exe，能跑但那个目录会被清理/换名，任务里存它迟早
    失效——`app_paths.get_executable()` 就是为这件事存在的）。
    源码态用 pythonw 跑 launcher.pyw（用 python.exe 会挂一个黑框）。"""
    from utils.app_paths import get_executable, get_app_dir
    if getattr(sys, 'frozen', False) or \
            getattr(sys.modules.get('__main__'), '__compiled__', None):
        return f'"{get_executable()}"'
    py = sys.executable or 'python.exe'
    pyw = re.sub(r'python(?:\.\d+)?\.exe$', 'pythonw.exe', py, flags=re.I)
    if not os.path.isfile(pyw):
        pyw = py
    return f'"{pyw}" "{os.path.join(str(get_app_dir()), "launcher.pyw")}"'


def _apply_selfstart(gui, on):
    """勾=建"登录时自启"任务；取消=删掉。返回 `(ok, 给用户看的话)`。

    为什么走任务计划程序而不是注册表 `HKCU\\...\\Run`：只有任务计划程序
    能带**原生延时**——刚进桌面那一刻资源管理器/磁盘/杀毒扫描都在抢启动，
    立刻拉软件容易起不来（正是本轮"开机打开 vmware 不成功、隔很久才开"
    同族问题）；而且这条任务会出现在计划任务页列表里，用户看得见、
    删得掉，不是偷偷写在注册表里。
    建完照旧**回读核对**（★R241 新纪律：schtasks/PS 说成功只代表写进去了）。
    """
    import subprocess
    if on:
        tv = _selfstart_target()
        ps = _ps_delayed_register(SELFSTART_TASK, tv, '', 'logon',
                                 _SELFSTART_DELAY_PT)
        try:
            q = subprocess.run(['powershell', '-NoProfile', '-NonInteractive',
                                '-Command', ps], capture_output=True,
                               timeout=90, stdin=subprocess.DEVNULL,
                               creationflags=0x08000000)
            if q.returncode != 0:
                return False, (q.stderr.decode('gbk', errors='replace')
                               .strip() or '创建自启任务失败')
            rb = subprocess.run(
                ['powershell', '-NoProfile', '-NonInteractive', '-Command',
                 _ps_readback_cmd(SELFSTART_TASK, _tr_exe(tv))],
                capture_output=True, timeout=45, stdin=subprocess.DEVNULL,
                creationflags=0x08000000)
            got = rb.stdout.decode('gbk', errors='replace').strip()
            if rb.returncode != 0 or not got.startswith('OK'):
                subprocess.run(['schtasks', '/delete', '/f', '/tn',
                                SELFSTART_TASK], capture_output=True,
                               timeout=45, stdin=subprocess.DEVNULL,
                               creationflags=0x08000000)
                logger.warning(f'自启任务回读不符，已删除: {got[:160]}')
                return False, '自启任务回读核对不通过（已撤回），请改用' \
                              '手动启动或反馈这条日志'
            # ★R247（item2）：软件自己的"随登录自启"也会被系统默认的
            # "使用电池时不启动"卡住——笔记本不插电源就连软件都起不来，
            # 用户看到的正是"有时候开了有时候没开"。这条任务**强制**按
            # "两条件都不限制"写（设置页没有电源条件复选，语义就该跟着
            # 用户勾"自启"这个动作走）。
            _p_ok, _p_txt = _apply_power_settings(
                gui, SELFSTART_TASK, False, False)
            # 分隔符也跟着语言走：English 档下这行日志/这句提示里不该留全角
            # 分号（词典只翻短语，标点得自己抬）。
            _p_tail = '' if _p_ok else (i18n.tr('；', '; ') + _p_txt)
            return True, (f'🚀 已设为随登录自启（登录后 {_SELFSTART_DELAY_PT} '
                          f'起，任务名 {SELFSTART_TASK}，可在 📅 计划任务页删除）'
                          + _p_tail)
        except Exception as e:
            return False, f'创建自启任务异常: {e}'
    try:
        q = subprocess.run(['schtasks', '/delete', '/f', '/tn',
                            SELFSTART_TASK], capture_output=True,
                           timeout=60, stdin=subprocess.DEVNULL,
                           creationflags=0x08000000)
        out = (q.stdout.decode('gbk', errors='replace')
               + q.stderr.decode('gbk', errors='replace'))
        # 任务本来就不存在 = 目标状态已达，按成功收（幂等：勾上又取消、
        # 或用户在计划任务页手动删过）
        if q.returncode == 0 or '找不到' in out or 'not found' in out.lower():
            return True, '🚀 已取消随登录自启'
        return False, out.strip()[:200] or '删除自启任务失败'
    except Exception as e:
        return False, f'删除自启任务异常: {e}'


def _vm_slug(name):
    """虚拟机显示名 → 文件名安全的 slug：保留中文/字母/数字/`._-`，
    空格转 `_`，其余（`/\\:*?\"<>|` 等）替换为 `_`，长度封顶 24。
    全被替换光时回退 `vm`（不许出空串——空串会拼出 `vm__123.bat` 那种
    认不出来的名字）。"""
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', str(name or '').strip())
    s = re.sub(r'\s+', '_', s).strip('._ ')
    if len(s) > 24:
        s = s[:24].rstrip('._ ')
    return s or 'vm'


def _vm_bat_dir(create=True):
    r"""★R271（用户 `----2026-9-29 02----` item1）：启动 bat 的**固定**存放目录。

    此前写在 `get_app_dir()/scheduled_vms`，而打包态的 app_dir 就是
    **带版本号的程序目录**（`…/deskbits_beta_0.99-R006-单目录/`）。计划任务的
    动作里存的是这条绝对路径，所以每升一次版（解压到新目录）老任务就指向
    一个不存在/将被删掉的 bat ⇒ 任务照跑、静默什么都不做，用户看到的是
    "vmware 不能自动启动"。改到 `%LOCALAPPDATA%\DeskBits\scheduled_vms`：
    跟版本、跟安装位置都无关，覆盖升级/换目录都不会断链。

    拿不到 LOCALAPPDATA（极少见）或建目录失败 ⇒ 退回老的 app_dir 口径，
    **宁可退回旧行为也不让创建流程失败**。"""
    base = os.environ.get('LOCALAPPDATA') or ''
    if base:
        p = os.path.join(base, 'DeskBits', 'scheduled_vms')
        try:
            if create:
                os.makedirs(p, exist_ok=True)
            return p
        except OSError:
            logger.warning('LOCALAPPDATA 下建 scheduled_vms 失败，'
                           '退回程序目录（升级换目录会让任务动作断链）',
                           exc_info=True)
    from utils.app_paths import get_app_dir as _gad
    p = os.path.join(str(_gad()), 'scheduled_vms')
    if create:
        try:
            os.makedirs(p, exist_ok=True)
        except OSError:
            logger.warning(f'scheduled_vms 目录建不出来: {p}', exc_info=True)
    return p


def _catch_up_for(trig_v, user_checked):
    """★R271（用户 `----2026-9-29 02----` item1）：这条任务要不要开"错过补跑"。

    返回 `(开不开, 是不是被强制开的)`。纯函数，门禁直接穷举四种组合。

    为什么"开机时"必须强制开：任务注册出来是 `LogonType=Interactive`
    （本机实读两条 DeskBits 任务都是这个），开机那一刻**还没有用户会话**，
    交互式任务起不来；`StartWhenAvailable` 默认又是 False ⇒ 错过之后永远
    不补。两者一叠，"开机时"那一档就**结构性不可能自动启动**——用户看到的
    正是"vmware 不能自动启动，后来我是手动启动的"。开了补跑，Windows 会在
    用户登上桌面后立刻补运行一次，这才是用户要的效果。
    其余触发档（登录/每天/每隔 N 分…）尊重用户勾选，一个字不改。"""
    if trig_v == 'boot':
        return True, not bool(user_checked)
    return bool(user_checked), False


def _act_missing(run):
    """任务动作指向的文件还在不在（列表里标"死链"用）。

    返回 True=**确定**不存在。判不了的一律 False——空动作、`shutdown.exe /s /t 10`
    这类内建命令、相对名、读不到盘的网络路径都不算死链（★R241 口径：
    没证据不许下结论，误报"死链"会把好任务吓得用户删掉）。"""
    p = _tr_exe(run)
    if not p or not re.match(r'^[A-Za-z]:[\\/]', p):
        return False
    try:
        return not os.path.isfile(p)
    except OSError:
        return False


def _vm_bat_stem(names, hhmmss, pid):
    """启动 bat 文件名：**带上虚拟机名**（用户 item1a「备注里面写上类似
    vm_这块写虚拟机名字，这样好区别……比如 vm_win10_x6_2other」）。
    单台 `vm_<名>_<时分秒>_<pid3>`；多台只放第一台 + `_x<N>`（文件名塞不下
    一串全名，完整台数在备注里显示"等 N 台"）。"""
    first = _vm_slug(names[0] if names else '')
    n = max(1, len(names))
    tail = '_x%d' % n if n > 1 else ''
    return 'vm_%s%s_%s_%03d' % (first, tail, hhmmss, int(pid) % 1000)


def _build_vm_bat(entries, log_name):
    """生成启动虚拟机的 bat 正文（CRLF 文本）。

    ★R241（用户 item1「开机打开vmware不成功，后来隔了很长时间才自动打开」
    ——这种"有时候行有时候不行"没法靠猜修）每台 VM 一段：
      · 跑前跑后各写一行 `%date% %time%` 到同目录 `.log`，带 `rc=`；
        下次再出问题，看一眼日志文件就知道"到底跑没跑、几点跑、返回几"；
      · 失败重试，间隔 10 秒、**最多 3 次**（VMware 的授权服务在冷开机时
        常慢半拍，vmrun 早跑一步就是 rc≠0，重试比"让用户重开机"管用）；
      · 等待用 `powershell Start-Sleep`——`timeout /t` 在没有控制台的场景下
        会静默不等待（等于没延时）。
    全程不用括号块（`if ... ( a & b )` 里 `%TRY%` 是解析期展开，重试次数会
    永远停在 0），改成 goto 串，逐行解析。每台独立一段：第一台失败不拖累第二台。
    """
    ls = ['@echo off', 'setlocal',
          'set "LOG=%%~dp0%s"' % log_name,
          'rem log file: %s' % log_name,
          'echo ==== %%date%% %%time%% 开始启动 %d 台 ==== >>"%%LOG%%"'
          % len(entries)]
    for i, (nm, cmd) in enumerate(entries, 1):
        ls += ['', 'rem ---- %s ----' % nm,
               'set TRY=0',
               ':try%d' % i,
               'set /a TRY+=1',
               '%s >>"%%LOG%%" 2>&1' % cmd,
               # ★R277（win11 真机日志实锤）：这一行**没有** `% ` 运算符，旧写法
               # 把 `%%errorlevel%%` 原样写进了 bat ⇒ cmd 里 RC 变成字面串
               # `%errorlevel%`，`if %RC%==0` 永不成立 ⇒ 启动成功也被当成失败：
               # 白等 2×10 秒、再跑两遍 vmrun、日志写 `FAIL ... rc=%errorlevel%`。
               # 本行没有任何替换项，直接写单百分号才是 bat 里正确的展开。
               'set RC=%errorlevel%',
               'if %%RC%%==0 goto ok%d' % i,
               'if %%TRY%% LSS 3 goto retry%d' % i,
               'echo [%%date%% %%time%%] FAIL %s rc=%%RC%% after 3 tries '
               '>>"%%LOG%%"' % nm,
               'goto end%d' % i,
               ':retry%d' % i,
               'powershell -NoProfile -NonInteractive -Command '
               '"Start-Sleep -Seconds 10" >>"%LOG%" 2>&1',
               'goto try%d' % i,
               ':ok%d' % i,
               'echo [%%date%% %%time%%] OK %s rc=0 try=%%TRY%% '
               '>>"%%LOG%%"' % nm,
               ':end%d' % i]
    ls += ['', 'exit /b 0', '']
    return '\r\n'.join(ls)


# ---- ★R331-b：存量启动 bat 自愈（只修那一行坏的错误码展开） ----
# ★R277 把模板里的 `set RC=%%errorlevel%%` 改成了单百分号，可**已经生成在用户
# 机器上的 bat 没人管**：`_migrate_bare_bats` 改的是任务"指向哪个文件"，不动文件
# 正文。R045 真机日志包实锤——`vms/vm_Win10-x64-2other_164309_172.bat`（2026-09-29
# 生成）第 12 行还是 `set RC=%%errorlevel%%`，同目录 .log 里就是
# `FAIL Win10-x64-2other rc=%errorlevel% after 3 tries`：cmd 里 RC 成了字面串，
# `if %RC%==0` 永不成立 ⇒ **虚拟机启动成功也被判成失败**，白等 2×10 秒、vmrun
# 多跑两遍、日志写 FAIL。这正是 ★R241 当年"开机打开vmware不成功，后来隔了很长
# 时间才自动打开"那条症状的最后一截。
# 按**字节**改：这一行纯 ASCII，不碰编码（那两份 .log 是 cmd 的 936 与子进程输出
# 混着写的，任何"读成文本再写回"都会把另一半弄坏）、不碰行尾、不碰别的行。
STALE_RC_LINE = b'set RC=%%errorlevel%%'
HEALED_RC_LINE = b'set RC=%errorlevel%'


def heal_vm_bat_bytes(data):
    """纯函数：把那一行坏展开改对。返回 (新字节, 改了几处)。
    没有这一行 ⇒ 原样返回、计数 0（幂等，第二趟什么都不会做）。"""
    if not data or STALE_RC_LINE not in data:
        return data, 0
    return data.replace(STALE_RC_LINE, HEALED_RC_LINE), data.count(STALE_RC_LINE)


def _heal_stale_vm_bats(dirs=None):
    """扫我们自己的 scheduled_vms 目录，把老版本生成的坏 bat 就地修好。

    只在用户**主动点刷新**时跑（和 ★R277 那次迁移同一个口径——不趁用户没注意
    偷偷写盘）。只碰 `vm_*.bat`；写盘走同目录 temp + `os.replace`（不留半截文件）；
    单条失败只跳过这条，不影响列表。返回修过的文件名列表。"""
    fixed = []
    for d in (dirs if dirs is not None else [_vm_bat_dir(create=False)]):
        try:
            if not d or not os.path.isdir(d):
                continue
            for fn in sorted(os.listdir(d)):
                if not (fn.startswith('vm_') and fn.lower().endswith('.bat')):
                    continue
                p = os.path.join(d, fn)
                try:
                    with open(p, 'rb') as fh:
                        old = fh.read()
                    new, n = heal_vm_bat_bytes(old)
                    if not n:
                        continue
                    tmp = p + '.r331tmp'
                    with open(tmp, 'wb') as fh:
                        fh.write(new)
                    os.replace(tmp, p)
                    fixed.append(fn)
                    logger.info('★R331 老版本虚拟机启动脚本已修好（启动成功不再误报失败）: '
                                '%s（%d 处）' % (fn, n))
                except Exception as e:
                    logger.warning('虚拟机启动脚本自愈单条失败（跳过这条）: %s — %s'
                                   % (fn, e))
        except Exception as e:
            logger.warning('虚拟机启动脚本自愈扫描失败（跳过）: %s' % e)
    return fixed


def scan_stale_vm_bats(dirs=None):
    """★R332-c（§卅六 三·5）：**只读**数一遍有几只老版本的坏 bat——一个字都不写。

    ★R331-b 的自愈只挂在"刷新任务列表"那一趟，从不打开那页的用户，坏 bat 会一直
    坏着。启动时先探测、问一句再修：既不"程序一开就偷偷往用户目录写文件"，也不让
    坏 bat 一直躺着。目录不在/读不动一律回空列表，同一把尺子（heal_vm_bat_bytes）。"""
    hits = []
    for d in (dirs if dirs is not None else [_vm_bat_dir(create=False)]):
        try:
            if not d or not os.path.isdir(d):
                continue
            for fn in sorted(os.listdir(d)):
                if not (fn.startswith('vm_') and fn.lower().endswith('.bat')):
                    continue
                try:
                    with open(os.path.join(d, fn), 'rb') as fh:
                        _n = heal_vm_bat_bytes(fh.read())[1]
                    if _n:
                        hits.append(fn)
                except Exception as e:
                    logger.warning('虚拟机启动脚本只读探测单条失败（跳过这条）: '
                                   '%s — %s' % (fn, e))
        except Exception as e:
            logger.warning('虚拟机启动脚本只读探测失败（跳过）: %s' % e)
    return hits


# 一次进程最多问一遍（用户点【否】或没空理这个框时，不该每 10 分钟再骚扰一次）
_STARTUP_VM_ASKED = [False]


def startup_vm_bat_check(gui):
    """★R332-c：启动探测的接线。扫描在后台线程，弹窗回投 UI 线程，点【是】才写盘。

    刷新任务列表那条路（★R331-b 闸口内的自愈）照旧——先跑过那趟的话，这里读到的
    就已经是好的，自然不会再问。"""
    if _STARTUP_VM_ASKED[0]:
        return
    _STARTUP_VM_ASKED[0] = True

    def _work():
        try:
            bad = scan_stale_vm_bats()
        except Exception as e:
            logger.warning('虚拟机启动脚本启动探测失败（跳过）: %s' % e)
            return
        if not bad:
            return
        try:
            gui._ui_queue.put(
                lambda n=len(bad): _ask_heal_vm_bats(gui, n))
        except Exception as e:
            # ★R230b 口径：兜底可以吞异常，但必须留一行"谁来修"的下一步
            logger.warning('虚拟机启动脚本提示没能交给界面（%r）⇒ 坏脚本照旧，'
                           '可到计划任务页点一次刷新修好', e)

    threading.Thread(target=_work, daemon=True).start()


def _ask_heal_vm_bats(gui, n):
    """UI 线程里问一句。【否】＝一个字不写；【是】＝走 ★R331-b 那把已验过的自愈。"""
    try:
        yes = messagebox.askyesno(
            i18n.tr('发现老版本启动脚本', 'Old VM Launch Scripts'),
            i18n.tr(
                f'发现 {n} 个老版本的虚拟机启动脚本有毛病：虚拟机明明启动成功了，'
                f'它也会报失败，还要白等 20 秒。\n现在帮你修好吗？'
                f'（只改我们自己目录里的那几个文件）',
                f'{n} old VM launch script(s) are faulty: they report failure even '
                f'though the VM started, and wait 20 extra seconds.\nFix them now? '
                f'(only files inside our own folder)'),
            parent=gui.root)
    except Exception as e:
        logger.warning('老版本启动脚本的确认框没能弹出（%r）⇒ 不写盘' % (e,))
        return
    if not yes:
        logger.info('用户选择暂不修老版本虚拟机启动脚本（本次运行不再问）')
        return

    def _do():
        try:
            fixed = _heal_stale_vm_bats()
        except Exception as e:
            logger.warning('老版本虚拟机启动脚本修复失败: %s' % e)
            return
        if fixed:
            gui._ui_queue.put(
                lambda k=len(fixed): gui._log(
                    f'🔧 已修好 {k} 个老版本的虚拟机启动脚本'
                    f'（以前虚拟机明明启动成功了也会报失败、还会白等 20 秒）'))

    threading.Thread(target=_do, daemon=True).start()


def _vm_memo_tag(stem):
    """从 bat 文件名反解 `(虚拟机名, 台数)`，供备注列显示。
    `vm_win10_x64_x2_093000_413` → ('win10_x64', 2)；
    老任务（`vm_122249_413` 只有时分秒）→ ('', 1)，备注退回原样显示，
    不拿时间戳冒充虚拟机名。"""
    s = (stem or '').strip()
    if not s.startswith('vm_'):
        return '', 1
    toks = s[3:].split('_')

    def _strip_time(t):
        while len(t) > 1 and re.match(r'^\d{1,8}$', t[-1]):
            t.pop()
        return t

    toks = _strip_time(toks)
    n = 1
    if toks and re.match(r'^x\d+$', toks[-1] or ''):
        n = int(toks[-1][1:])
        toks = _strip_time(toks[:-1])
    name = '_'.join(toks)
    if not name or name.isdigit():
        return '', n
    return name, n


def _elide_keep_head(text, max_len):
    """备注列显示不全时按字符预算做**中间省略**：留住开头（虚拟机名就是
    关键字，用户原话），尾巴用 `…` 代替。预算内放得下就原样返回。"""
    s = str(text or '')
    try:
        m = int(max_len)
    except (TypeError, ValueError):
        return s
    if m <= 0 or not s:
        return ''
    if len(s) <= m:
        return s
    if m == 1:
        return '…'
    # 省略号前不留分隔符（`vm_win10_x64_…` 比 `vm_win10_x64…` 难认）
    return s[:m - 1].rstrip('_ -·/') + '…'


def _memo_real_width(tree, col='memo'):
    """备注列**屏幕上真正**的宽度（px），量不到就返回 0（调用方不许猜）。

    ★R275（用户「备注往旁边拉也不能看见全部的文字，我希望是往右拉能看全」）：
    这个 Tk 构建的 ttk::treeview **没有** `tellcolumn` 子命令（实测
    `bad command "tellcolumn"`），而 `tree.column(col,'width')` 读的是配置值、
    `winfo_width()` 读的是整棵树的宽——都不是那一列的实宽。唯一可信的量具是
    **单元格的 bbox**（`bbox(iid, col)` 返回的宽就是该列当前占的像素，
    实测配置 60/170/300/400 四档逐档相等）。树里没行 ⇒ 量不到 ⇒ 0。"""
    try:
        kids = tree.get_children()
        if not kids:
            return 0
        bb = tree.bbox(kids[0], col)
        if bb:
            w = int(bb[2])
            return w if w > 0 else 0
    except Exception:
        pass
    return 0


def _memo_char_budget(tree, col='memo'):
    """备注列**当前**放得下多少个字符（省略预算）。

    ★R241（用户「默认显示需要显示全点，适应下窗口」）：其余列 stretch=False
    ⇒ 窗口拉宽的多余空间全归备注列，所以预算要跟列的实际宽度走，不能写死。
    控件还没映射时 `winfo_width()` 返回 1，此时退回列配置宽。每字按 ~9px 估
    （中文≈12px、ASCII≈6.5px，取偏保守值：宁可早点省略也不要溢出截半字）。
    ★R275 改口径：预算**优先取单元格实宽**（拖分隔线也算）。旧写法在
    "树比各列之和宽"时把整块富余当成备注列的（`w = total - fixed - 16`），
    于是用户把备注列拖到多宽都无关紧要——那一串带 `…` 的文本是按虚高预算
    存进单元格的，而且重铺只挂在 `<Configure>`（窗口变化）上，拖分隔线
    根本不触发 ⇒ 症状正是「往右拉也看不见全部文字」。"""
    _real = _memo_real_width(tree, col)
    if _real:
        return max(10, int(_real / 9))
    try:
        cfg = {c: int(tree.column(c, 'width') or 0) for c in tree['columns']}
    except Exception:
        cfg = {}
    if not cfg.get(col):
        # ★R241：列宽**未知**（配置宽为 0，例如测试装具/未初始化的树）时
        # 不去猜——直接不省略。宁可整行列出来，也不要在信息不足时把
        # "打开软件·xxx" 这类关键字眼砍掉（用户口径：默认要显示全点）。
        return 10 ** 6
    w = int(cfg.get(col) or 150)
    try:
        total = int(tree.winfo_width())
        fixed = sum(v for c, v in cfg.items() if c != col)
        if total > fixed + 24:
            w = total - fixed - 16        # 16px 留给边框/内边距
    except Exception:
        pass
    return max(10, int(w / 9))


def _memo_display(text, tree, col='memo'):
    """全文 → 该列**当前实宽**下该显示的那串（放得下就全文）。"""
    return _elide_keep_head(text, _memo_char_budget(tree, col))


def _memo_refit_tree(tree, col='memo'):
    """把 `_memo_full` 里的全文按当前实宽重铺回单元格，返回铺了几行。

    只改单元格文本，不重跑加载（加载要开 PowerShell 拉全量≈4 秒）。"""
    n = 0
    for iid, full in (getattr(tree, '_memo_full', {}) or {}).items():
        try:
            if tree.exists(iid):
                tree.set(iid, col, _memo_display(full, tree, col))
                n += 1
        except Exception:
            pass
    return n


def _bind_memo_refit(tree, col='memo'):
    """挂备注列的两条重铺扳机，返回**本次新挂**的条数（已挂过返回 0＝幂等）。

    · `<Configure>`：窗口/树被拉大拉小；
    · `<ButtonRelease-1>`：★R275 用户点名的那一半——**拖完分隔线抬手**。
      拖动不产生 `<Configure>`，所以旧写法永远不重铺，拉多宽都还是旧文本。
    拖窗口时事件密集，150ms 去抖后再铺。"""
    if getattr(tree, '_memo_refit_bound', False):
        return 0
    tree._memo_refit_bound = True
    tree._memo_refit_job = None

    def _on_cfg(_e=None):
        try:
            if tree._memo_refit_job:
                tree.after_cancel(tree._memo_refit_job)
        except Exception:
            pass
        try:
            tree._memo_refit_job = tree.after(150, _memo_refit_tree, tree, col)
        except Exception:
            pass

    tree.bind('<Configure>', _on_cfg, add='+')
    tree.bind('<ButtonRelease-1>', _on_cfg, add='+')
    return 2


def _prog_name(cmdline, limit=16):
    """★ R176（用户 -06/5）：从"要运行的任务"提取程序简名——
    'C:\\...\\notepad.exe' → notepad；powershell -Command ... → powershell。
    用户要求备注里把程序名写出来（如 zcode），一眼看清跑的是什么。
    ★R241：从 `_load_tasks` 里挪到模块级＝门禁能直接调（原先只能靠字符串猜）。
    ★R241b：`limit` 可放大——vm 的备注要从 **完整** bat 文件名里反解
    （`vm_<名>_x<台数>_<时分秒>_<pid>`），16 字符封顶会把 `_x2_` 台数标记
    截掉，`_vm_memo_tag` 只能拿半截文件名冒充虚拟机名（实测备注
    `win10_x64_x2_`）。显示长度另有列宽省略 + 悬停全文兜底，不靠这里截。"""
    s = (cmdline or '').strip().strip('"')
    if not s or s.upper() == 'N/A':
        return ''
    # ★R275（item2）：动作被换成隐藏启动器时，备注要还写**原来的程序名**
    # （`wscript` 对用户没有任何信息量，虚拟机名也全在 bat 文件名里）。
    _un, _hl = _unwrap_action(cmdline)
    if _hl and _un:
        s = str(_un).strip().strip('"')
        if not s:
            return ''
    m = re.search(r'["\s\\/]([\w.\- ]+\.(?:exe|bat|cmd|ps1|py|vbs))\b',
                  s, re.IGNORECASE) or re.match(
                      r'^([\w.\-]+\.(?:exe|bat|cmd|ps1|py|vbs))\b', s,
                      re.IGNORECASE)
    if not m:
        return s.split()[0].strip('"')[:limit] if s.split() else ''
    base = m.group(1).strip()
    base = base.split('\\')[-1].split('/')[-1]
    return base.rsplit('.', 1)[0][:limit]


# ============================================================================
# ★R251 item2b（用户 43 批）：备注里的**频率**必须由任务真实的调度参数算出来
# ----------------------------------------------------------------------------
# 用户原话：「我点了每周清空回收站，然后创建脚本，备注里面显示的是每天清空，
# 对不上……比如我设置的 N 是 2 天，那么备注必须一致，要不误导用户……
# 因为是动态的。N 是可以设置的。」
#
# 实锤取证（run/r251/trig_dump.txt、run/r251/schtasks_v.txt，全程只读）：
#   · 模板「🗑 每周清空回收站」= `trig='days', n='7'` → `schtasks /sc daily /mo 7`
#     → 落成 `MSFT_TaskDailyTrigger`，那个 **7 存在 DaysInterval=7**；
#   · 旧的 PS 取证串**只取 CimClassName** ⇒ 类名只能翻成"每天"，7 从来没被读出来；
#   · 同一个任务在 `schtasks /v` 回退路里 `天='每 7 天'（列值是对的）`，
#     `_memo_of()` 命中 `'每' in day` ⇒ **主路"每天"、回退路"每 7 天"**，
#     两条数据源口径不一致本身就是缺陷。
#   · 另一例 `DeskBits_minutes_open_172446`：`计划类型='一次性, 分钟'` +
#     `重复: 每='0 小时， 2 分钟'` ⇒ 主路拼出「定时每2分钟」，那个"定时"是类名兜底词，
#     同样是错的。
# 所以下面这几个函数是**唯一**的翻译表，主路（PowerShell）和回退路（schtasks /v）
# 都走它，谁也不许再自己拼一套。
# ============================================================================

#: 触发器类名 → 中文兜底词（**只在**认不出具体频率时用；★R251 从 `_load_tasks`
#: 的嵌套 `_TRIG_TXT` 挪到模块级，才能被 `_when_cn` 用、才能被门禁真调用真断言
#: ——与 ★R241 把 `_memo_of` 挪出嵌套作用域同一件事）
_TRIG_TXT = {
    'MSFT_TaskBootTrigger': '开机时',
    'MSFT_TaskLogonTrigger': '登录时',
    'MSFT_TaskDailyTrigger': '每天',
    'MSFT_TaskWeeklyTrigger': '每周',
    'MSFT_TaskTimeTrigger': '一次性',
    'MSFT_TaskCalendarTrigger': '按日历',
    'MSFT_TaskIdleTrigger': '空闲时',
    'MSFT_TaskEventTrigger': '事件触发',
    'MSFT_TaskRegistrationTrigger': '创建后',
    'MSFT_TaskSessionStateChangeTrigger': '会话变化时',
}

#: 「每 1 天」和「每天」是同一件事——两种数据源都翻成同一个词，
#: 用户才不会出现"同一个任务两处显示不一样"的新误解（★R251）。
_EVERY_ONE = {'天': '每天', '周': '每周', '小时': '每小时',
              '分钟': '每分钟', '秒': '每秒'}


def _interval_txt(rep):
    """时长（ISO 8601 / TimeSpan / 中文时长串）→ '每30分钟'/'每2天'。

    ★R251：从 `_load_tasks` 的嵌套函数挪到模块级（`_when_cn` 要用，门禁也要
    能真调用）。解析逻辑与 ★R204 GUIA-9 / ★R207 NEW3-A5 定的完全一致，
    **只是加了中文时长串入口**（`0 小时， 2 分钟` 这类 schtasks /v 的列值），
    让两条数据源共用同一个翻译出口。
    ★R204 GUIA-9：原正则捕获组错位把秒当天数（PT5M30S→720 小时），
    且 TimeSpan 串 '00:30:00' 不匹配——重写：ISO 8601 解析 +
    TimeSpan 兜底，统一换算总分钟。
    """
    import re as _re
    if not rep:
        return ''
    s = str(rep).strip()
    if not s or s in ('已禁用', 'N/A', 'n/a'):
        return ''
    up = s.upper()
    total_min = 0
    unit_txt = ''
    m = _re.match(
        r'^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$', up)
    if m and any(m.groups()):
        d, h, mn, sec = (int(g or 0) for g in m.groups())
        total_min = d * 24 * 60 + h * 60 + mn + (1 if sec >= 30 else 0)
    else:
        m = _re.match(r'^(?:(\d+)\.)?(\d+):(\d+)(?::(\d+))?$', s)
        if m:
            d = int(m.group(1) or 0)
            h = int(m.group(2) or 0)
            mn = int(m.group(3) or 0)
            sec = int(m.group(4) or 0)
            total_min = d * 24 * 60 + h * 60 + mn + (1 if sec >= 30 else 0)
        else:
            # ★R251：schtasks /v 的"重复: 每"是中文时长串（'0 小时， 2 分钟'），
            # 旧写法直接返回 '' ⇒ 回退路的频率词全靠运气。逐段取数再合成。
            parts = _re.findall(r'(\d+)\s*(天|周|小时|分钟|分|秒)',
                                s.replace('，', ','))
            if not parts:
                return ''
            _d = _w = _h = _mn = _sec = 0
            for v, u in parts:
                v = int(v)
                if u == '天':
                    _d += v
                elif u == '周':
                    _w += v
                elif u.startswith('小时'):
                    _h += v
                elif u.startswith('分'):
                    _mn += v
                else:
                    _sec += v
            if _w == 1 and _d == 0 and _h == 0 and _mn == 0 and _sec == 0:
                return _EVERY_ONE['周']
            if _w > 1:
                return '每%d周' % _w
            total_min = _d * 24 * 60 + _h * 60 + _mn + (1 if _sec >= 30 else 0)
            if total_min <= 0:
                return ''
    if total_min <= 0:
        return ''
    if total_min % (24 * 60) == 0:
        _days = total_min // (24 * 60)
        return _EVERY_ONE['天'] if _days == 1 else f'每{_days}天'
    _d, _rest = divmod(total_min, 24 * 60)
    _h, _mn = divmod(_rest, 60)
    # ★R207 NEW3-A5：混合时长组出来（PT2H30M 不再退化成"每150分钟"）
    _parts = ([f'{_d}天'] if _d else []) + \
             ([f'{_h}小时'] if _h else []) + \
             ([f'{_mn}分钟'] if _mn else [])
    return '每' + ''.join(_parts)


def _trig_detail(seg):
    """PS 侧触发器串 → dict（键小写：cls/ri/di/wi/sb）。

    新格式 `MSFT_TaskDailyTrigger#DI=7#SB=2026-09-19T20:49:00`；
    老格式 `MSFT_TaskTimeTrigger#PT30M`（`#` 后面**没有 `=`** 的那段就是
    重复间隔）继续认——win11 上用户机器里存量任务的串就是老格式，
    读不到新字段时行为退回修前，不许抛也不许当成"没频率"。
    纯函数、任何输入都不炸（★R249：门禁要能真调用）。"""
    out = {}
    for i, piece in enumerate(str(seg or '').split('#')):
        piece = piece.strip()
        if not piece:
            continue
        if i == 0 and '=' not in piece:
            out['cls'] = piece
            continue
        if '=' in piece:
            k, v = piece.split('=', 1)
            out[k.strip().lower()] = v.strip()
        else:
            out.setdefault('ri', piece)     # 老格式：裸时长=重复间隔
    return out


def _hhmm_from_sb(sb):
    """时间串 → 'HH:MM'；读不到给 ''。

    两种入口都要能吃（★R251：两条数据源必须逐字一致）：
    PowerShell 的 `StartBoundary`=`2026-09-19T20:49:00`、
    `schtasks /v` 的"开始时间"列=`8:01:00`（不带日期、小时可以是一位）。"""
    import re as _re
    m = _re.search(r'(\d{1,2}):(\d{2})', str(sb or ''))
    if not m:
        return ''
    try:
        return '%02d:%02d' % (int(m.group(1)), int(m.group(2)))
    except (TypeError, ValueError):
        return ''


def _when_cn(seg):
    """PS 触发器串 → 大白话频率（★R251 item2b 的主路口径）。

    口径：**能算出 N 就报 N**（`每7天`/`每2天`/`每30分钟`），
    算不出才退回类名词（`每天`/`每周`/`开机时`）；
    一次性任务不再顶着"定时"这个类名兜底词。
    每天/每周/一次性这三类会把 StartBoundary 的"几点"补上——
    用户在创建器里填的时间就是它，备注不写等于少了一半信息。"""
    d = _trig_detail(seg)
    cls = d.get('cls') or ''
    ri = d.get('ri') or ''
    sb = d.get('sb') or ''

    def _num(key):
        try:
            return int(str(d.get(key) or '').strip() or 0)
        except (TypeError, ValueError):
            return 0

    di, wi = _num('di'), _num('wi')
    when = ''
    if cls == 'MSFT_TaskDailyTrigger':
        when = f'每{di}天' if di > 1 else (_EVERY_ONE['天'] if di == 1 else '')
    elif cls == 'MSFT_TaskWeeklyTrigger':
        when = f'每{wi}周' if wi > 1 else (_EVERY_ONE['周'] if wi == 1 else '')
    elif cls == 'MSFT_TaskTimeTrigger':
        # 有重复间隔=每隔 N 分/时/天；没有=一次性（★R185 延时任务走这支）
        when = (_interval_txt(ri) if ri else
                (f'每{di}天' if di > 1 else '一次性'))
    elif cls == 'MSFT_TaskCalendarTrigger':
        when = (_interval_txt(ri) if ri else '') or '按日历'
    else:
        when = _TRIG_TXT.get(cls, '')
        if when and ri:                     # 登录/开机 + 重复（罕见但合法）
            when += _interval_txt(ri)
    if not when:
        when = _TRIG_TXT.get(cls, '')
    hm = _hhmm_from_sb(sb)
    if hm and cls in ('MSFT_TaskDailyTrigger', 'MSFT_TaskWeeklyTrigger') or \
            (hm and cls == 'MSFT_TaskTimeTrigger' and not ri):
        when = (when + ' ' + hm).strip()
    return when


#: `schtasks /v` 的"计划类型"列 → 与 `_TRIG_TXT` 同一批中文词（★R251）。
#: 列值随系统语言/版本变化（本机实测见 `run/r251/schtasks_v.txt`：
#: '每天'/'一次性, 分钟'/'系统定义的登陆'/'启动时'），所以按**关键词**认，
#: 认不出才原样给出去（宁可土一点，也不要翻错成另一个频率）。
def _ctype_cn(ctype):
    c = str(ctype or '').strip()
    if not c or c in ('N/A',):
        return ''
    if '登陆' in c or '登录' in c or 'logon' in c.lower():
        return '登录时'
    if '启动' in c or '开机' in c or 'start' in c.lower():
        return '开机时'
    if '每周' in c or 'weekly' in c.lower():
        return '每周'
    if '每天' in c or 'daily' in c.lower():
        return '每天'
    if '一次性' in c or 'once' in c.lower():
        return '一次性'
    return c


def _when_cn_from_v(ctype, day, rep, stime=''):
    """schtasks /v 的列（计划类型/天/重复: 每/开始时间）→ **与 `_when_cn` 同一口径**。

    ★R251：回退路原来只把列值原样塞进备注（`每 7 天` 带空格、`0 小时， 2 分钟`
    整串），两条源各写各的 ⇒ 同一个任务换个数据源就换个说法。
    列名以真机取证为准（`run/r251/schtasks_v.txt`）。

    "补不补钟点"必须跟主路**同一条规则**，否则同一个任务两条源显示不一样：
      · 频率来自**"天"列**（= DaysInterval，主路的 Daily/Weekly 触发器）→ 补；
      · 频率来自**"重复: 每"列**（= Repetition.Interval，主路 TimeTrigger 带
        ri 那支**不补**）→ 不补；
      · 触发事件型（登录/开机/空闲/事件/创建后/会话）→ 不补——那列的"开始时间"
        是占位串（本机实测 '12:00:00 AM'），补上去就是"登录时 00:00"的假信息。"""
    when = ''
    from_rep = False
    for raw, is_rep in ((day, False), (rep, True)):
        s = str(raw or '').strip()
        if not s or s in ('N/A', '已禁用'):
            continue
        txt = _interval_txt(s)
        if txt:
            when, from_rep = txt, is_rep
            break
    if not when:
        when = _ctype_cn(ctype)
    if when and not from_rep and not any(
            k in when for k in ('登录', '开机', '空闲', '事件',
                                '创建后', '会话')):
        hm = _hhmm_from_sb(stime)
        if hm:
            when = (when + ' ' + hm).strip()
    return when


def _when_v_from_row(r):
    """`schtasks /v` 的一行读数 → 频率词（**主路 `_when_cn` 的同款口径**）。

    ★R251：整条口径（含"补不补钟点"）只有 `_when_cn_from_v` 一份实现，
    `_memo_of` 和门禁读列后都调这里——本文件不存在第二份内联逻辑，
    也就不可能出现"探针和产品各说各话"（测的是探针不是产品）。"""
    r = r or {}
    return _when_cn_from_v(r.get('ctype') or '', r.get('day') or '',
                           r.get('rep') or '', r.get('stime') or '')


def _boot_task_needs_catch_up(freq_txt) -> bool:
    """★R271-S6（用户 `----2026-9-29 02----` item1）：这条判据管的是**存量**任务。

    「开机时」触发的任务注册出来是 `LogonType=Interactive`，开机那一刻还没有用户
    会话 ⇒ 不开补跑就永远不会运行（本机实读 StartWhenAvailable=False 证实）。创建
    路已由 `_catch_up_for` 强制开；老版本建出来的那批只能走 📝 编辑补——用户现场
    正是这一批，只修创建路等于他的 vmware 照样不开。"""
    return str(freq_txt or '').strip() == '开机时'


def _memo_of(name, r):
    """★ 02-3 备注（用户：注明作用，如"x 小时重启"，方便了解功能）。
    DeskBits 任务=从 /v 计划字段+任务名拼大白话；其他任务=自带注释。
    ★R241（用户 item1a）：`打开虚拟机` 任务的备注要写**虚拟机名**
    （如 `win10_x64 等2台`），不能只是把任务名/那串时分秒复制一遍。"""
    r = r or {}
    nm = str(name or '').lstrip('\\').lstrip('/')
    parts = nm.split('_')
    if nm.startswith('DeskBits_') and len(parts) >= 3:
        act = {'shutdown': '关机', 'reboot': '重启', 'open': '打开软件',
               'script': '跑脚本', 'cmd': '执行命令',
               'vm': '打开虚拟机'}.get(parts[2], '任务')
        # ★R251 item2b：回退路也走**同一张翻译表**——旧写法把列值原样塞进备注
        # （`每 7 天` 带空格、`0 小时， 2 分钟` 整串），于是同一个任务换个数据源
        # 就换个说法；现在 `每 7 天`→`每7天`、`0 小时， 2 分钟`→`每2分钟`，
        # 与 PowerShell 主路逐字一致（口径见 `_when_cn`）。
        # 补不补钟点那条判断也走 `_when_v_from_row`：本文件只留一份口径实现，
        # 门禁测的就是这里用的那一段（★R251 判据 M6）。
        when = _when_v_from_row(r)
        prog = _prog_name(r.get('run', ''))
        if parts[2] == 'vm':
            # ★R241b：台数标记在被截断的那 16 个字符之外，必须拿完整
            #   文件名才能反解出 `win10_x64 等2台`（截过的会把
            #   `win10_x64_x2_` 当名字显示）。
            vname, vcnt = _vm_memo_tag(_prog_name(r.get('run', ''), limit=99))
            if vname:
                prog = vname + (' 等%d台' % vcnt if vcnt > 1 else '')
        head = f'{when} {act}'.strip()
        return f'{head}·{prog}' if prog else head
    c = (r.get('comment') or '').strip()
    if c not in ('', 'N/A'):
        return c[:40]
    prog = _prog_name(r.get('run', ''))
    return f'系统任务·{prog}' if prog else '系统任务'


# ★R241 口径：表格显示层的翻译逻辑挪到模块级，门禁才能真调用真断言（藏在
# 嵌套作用域里时只能靠字符串猜）。★R331-c 补上新收的真机码：
# 2147946720 = 0x800710E0，本机 FormatMessageW 解出来是「操作员或系统管理员
# 拒绝了请求」（R045 日志包 `DeskBits_delay_shutdown_212406` 那次关机就是这么
# 被系统挡下来的，界面上原先只有一串裸数字）。
_LAST_HINT = {267009: '正在运行', 267011: '尚未运行',
              267014: '被用户终止', 2: '文件不存在',
              3: '路径不存在', 267010: '尚未运行(未设时间)',
              2147942402: '找不到文件',
              2147946720: '系统拒绝了这次操作'}


def _last_txt(code):
    """★R207（06批-1）：上次结果错误码 → 人话。0=正常；267011=
    尚未运行（任务建好从没跑过）；2/3/0x80070002=文件/路径不存在
    （v2rayN 案最需要的诊断）；其余原样给码。给不出人话就空。"""
    c = (code or '').strip()
    if not c:
        return ''
    try:
        n = int(c)
    except ValueError:
        return c[:16]
    if n == 0:
        return '正常'
    hint = _LAST_HINT.get(n, '')
    if hint:
        return f'{c} {hint}' if n not in (267009, 267011) else hint
    return c


def _mark_task_selectors(gui, win):
    """★R251 item4：给**事后才造出来的窗口**补上单选钮的圆圈文字前缀。

    勾选框那半（★R250 三3c 的口径）已由 ★R315 撤掉——勾选态回到样式层的原生
    指示器（"色块档"），Toplevel 里的复选框出生就带，不需要再补文字前缀。
    单选钮那半仍保留：用户 43 批「什么时候做（触发）、做什么（动作）这几项，
    没选中的前面没有小圈圈，看不见」。从主窗实例上取方法，**取不到就静默跳过**：
    补前缀是锦上添花，绝不能让它把编辑窗/监视窗弄弹不出来。"""
    for _m in ('_mark_radios',):
        try:
            _fn = getattr(gui, _m, None)
            if callable(_fn):
                _fn(win)
        except Exception as e:
            logger.debug(f'补选择控件前缀失败（{_m}）: {e}')


def _bind_memo_tip(tree, col='#3', memo_attr='memo'):
    """给 Treeview 的备注列挂"悬停看全文"提示。

    ★R241（用户 item2 细则「显示不全就显示关键字，其他…字符代替」）：
    中间省略保住了虚拟机名，但完整串（含时分秒/台数）仍要能看到——
    用一枚随鼠标弹出的小窗，不额外占列宽。`tree._memo_full` 是
    `_load_tasks` 每次刷新重建的 `iid → 全文` 字典。"""
    tip = {'win': None, 'iid': None}

    def _hide(_e=None):
        if tip['win']:
            try:
                tip['win'].destroy()
            except Exception:
                pass
            tip['win'] = None

    def _on_motion(event):
        iid = tree.identify_row(event.y)
        col_id = tree.identify_column(event.x)
        full = (getattr(tree, '_memo_full', {}) or {}).get(iid, '') if iid else ''
        if not full or col_id != col:
            if iid != tip['iid']:
                _hide()
                tip['iid'] = iid
            return
        shown = str(tree.set(iid, memo_attr) or '')
        if shown == full:      # 没被省略过 ⇒ 不打扰
            _hide()
            return
        if tip['win'] and tip['iid'] == iid:
            return
        _hide()
        try:
            w = tk.Toplevel(tree)
            w.wm_overrideredirect(True)
            w.wm_geometry('+%d+%d' % (event.x_root + 12, event.y_root + 14))
            tk.Label(w, text=full, justify=tk.LEFT, background='#ffffe0',
                     relief='solid', borderwidth=1,
                     font=ui_layout.app_font(9)).pack()
            tip['win'], tip['iid'] = w, iid
        except Exception:
            pass

    tree.bind('<Motion>', _on_motion, add='+')
    tree.bind('<Leave>', _hide, add='+')
    tree.bind('<Button-1>', _hide, add='+')
    return _hide


def _load_tasks(gui, tree, migrate=False):
    """schtasks 列表（★ R174 重写：/v 详细模式按表头解析出"动作"列 +
    创建日期排序 + 后台线程不卡 UI）。

    表头匹配中英文名（本机实测=任务名/下次运行时间/模式/要运行的任务）；
    匹配失败回退 R170 自适应列位解析（找以 \\ 开头的字段作任务名，
    无动作列）。动作归类 shutdown→关机/重启、脚本解释器→脚本/命令、
    .exe→打开软件。

    `migrate`（★R277）= 本轮是**用户主动**要求刷新，允许顺手把存量裸 bat
    任务的动作改成隐藏启动器。默认 False：页面首次构建、自动测试真建主窗
    都会走到这里，那些场合**一个字都不许写系统任务**。"""
    tree.delete(*tree.get_children())
    import csv
    import io
    import subprocess

    def _act_kind(cmdline):
        """★ A2 从"要运行的任务"归类动作"""
        s = (cmdline or '').strip().strip('"')
        if not s or s.upper() == 'N/A':
            return ''
        low = s.lower()
        if 'shutdown' in low:
            return '重启' if ('/r' in low or '/g' in low) else '关机'
        if any(k in low for k in ('powershell', 'cmd ', 'cmd.exe', 'python',
                                  'wscript', 'cscript', '.bat', '.cmd',
                                  '.ps1', '.vbs')):
            return '脚本/命令'
        if '.exe' in low or 'rundll32' in low:
            return '打开软件'
        return '命令'

    def _fmt_created(iso):
        """★ 02-3 创建时间显示：ISO '2026-09-19T20:47:14' → '09-19 20:47'"""
        s = (iso or '').strip()
        if len(s) >= 16 and s[4] == '-' and s[7] == '-' and s[10] in 'T ':
            return s[5:10] + ' ' + s[11:16]
        return s

    # ★R241：_prog_name / _memo_of 已上移到模块级（同一份逻辑，藏在嵌套
    # 作用域里时门禁只能靠字符串猜，挪出来才能真调用真断言）。此处直接
    # 用模块级版本，vm 任务的备注才会显示**虚拟机名**而不是那串时分秒。

    _STATE_CN = {'Ready': '就绪', 'Running': '运行中', 'Disabled': '已禁用',
                 'Queued': '排队中'}
    # ★R251 item2b：`_TRIG_TXT` 与 `_interval_txt` 已上移到模块级——
    # `_when_cn()`（主路）和 `_when_cn_from_v()`（/v 回退路）都要用同一份，
    # 留在嵌套作用域里就只能各拼各的，正是"每周的任务被写成每天"的成因。

    def _fmt_next(s):
        """下次运行显示：'09/26/2026 20:49:00' 或 '2026/9/26 20:49:00'
        → '09-26 20:49'（美式/ISO/文化格式都兼容）"""
        import re as _re
        s = (s or '').strip()
        if not s or s.upper() == 'N/A':
            return ''
        m = _re.match(r'^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})', s)
        if m:
            return f'{m.group(2)}-{m.group(3)} {m.group(4)}:{m.group(5)}'
        m = _re.match(r'^(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})', s)
        if m:
            return f'{int(m.group(1)):02d}-{int(m.group(2)):02d} ' \
                   f'{int(m.group(4)):02d}:{m.group(5)}'
        m = _re.match(r'^(\d{4})/(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})', s)
        if m:
            return f'{int(m.group(2)):02d}-{int(m.group(3)):02d} ' \
                   f'{int(m.group(4)):02d}:{m.group(5)}'
        return s[:16]

    # ★R331-c：`_last_txt` 已上移到模块级（和 `_prog_name`/`_memo_of` 同一套路），
    # 下面 2688/2708 两处调用点名字不变，走的就是模块级那一份。

    def _last_err(code):
        """上次结果是否属于"错误"（要标橙）：除 0/运行中/尚未运行外"""
        c = (code or '').strip()
        try:
            n = int(c)
        except (ValueError, TypeError):
            return False
        return n not in (0, 267009, 267010, 267011)

    _CMD_INTENT = (
        # ★ R177（用户 -09/6）："执行命令·powershell"太宽泛没概念——
        # 按命令内容翻译成大方向人话；兜底才写解释器名
        ('clear-recyclebin', '清空回收站'),
        ('$env:temp', '删除临时文件'),
        ('remove-item', '删除文件'),
        ('flushdns', '清理DNS缓存'),
        ('cleanmgr', '磁盘清理'),
        ('ipconfig /release', '断开网络'),
        ('ipconfig /renew', '重连网络'),
        ('defrag', '磁盘碎片整理'),
        ('sfc /scannow', '系统文件修复'),
        ('chkdsk', '磁盘检查修复'),
        ('taskkill /f /im explorer', '重启资源管理器'),
        ('del ', '删除文件'),
        ('rd ', '删除目录'),
        ('shutdown', '关机/重启'),
        ('robocopy', '备份复制'),
        ('xcopy', '复制文件'),
    )

    def _cmd_intent(run):
        """自定义命令 → 大方向人话（如"删除临时文件"）；认不出才空"""
        low = (run or '').lower()
        for kw, txt in _CMD_INTENT:
            if kw in low:
                return txt
        return ''

    def _memo_ps(r):
        """★ R177：PowerShell 数据源的备注=触发词+动作词+·程序名。
        触发词来自 Trigger 类名+Repetition 间隔；动作词 DeskBits 任务名
        优先（关机/重启…），否则按 run 内容归类。"""
        nm = r['name'].lstrip('\\').lstrip('/')
        parts = nm.split('_')
        act = None
        if nm.startswith('DeskBits_') and len(parts) >= 3:
            act = {'shutdown': '关机', 'reboot': '重启', 'open': '打开软件',
                   'script': '跑脚本', 'cmd': '执行命令',
                   'vm': '打开虚拟机'}.get(parts[2])
        run_txt = r.get('run', '')
        if not act:
            act = _act_kind(run_txt) or '任务'
        # ★ R177："执行命令·powershell"太宽泛——命令意图能翻译就翻译
        # （"删除临时文件·powershell"），兜底才写解释器名
        if act in ('执行命令', '脚本/命令', '命令'):
            act = _cmd_intent(run_txt) or act
        # ★R251 item2b：频率词一律由 `_when_cn()` 从**真实调度参数**算
        # （DaysInterval/WeeksInterval/Repetition.Interval/StartBoundary）。
        # 旧写法只拿类名查表 + 重复间隔拼接，于是 `/sc daily /mo 7` 的
        # "每周清空回收站"被写成"每天 清空回收站"（用户 43 批 item2 实锤）。
        when = _when_cn(r.get('trigger') or '')
        head = f'{when} {act}'.strip()
        prog = _prog_name(r.get('run', ''))
        # ★R241（用户 item1a「备注里写上虚拟机名字，这样好区别，不要只是
        # 简单复制任务名」）：主数据源是 PowerShell，vm 任务的备注在这里
        # 也要换成"虚拟机名（等 N 台）"，不能只在 /v 回退路里做。
        if (parts[2:3] == ['vm']):
            # ★R241b：同 _memo_of——台数在 16 字符之外，要用完整文件名反解
            _vn, _vc = _vm_memo_tag(_prog_name(r.get('run', ''), limit=99))
            if _vn:
                prog = _vn + (' 等%d台' % _vc if _vc > 1 else '')
        return f'{head}·{prog}' if prog else head

    # ★ R177 主路：PowerShell 一次拉全（~4 秒 vs schtasks /v 的 12 秒，
    # 用户"刷新比较慢"），字段=名/状态/创建日期/动作/下次运行/触发器
    # ★R207（06批-1 v2rayN 诊断）：加 LastTaskResult 上次结果——用户
    # "任务不能执行，需要看下问题在哪儿"：列表直接给错误码（0=正常、
    # 267011=尚未运行、2/0x80070002=找不到文件），不用再去系统里翻
    PS_SRC = (
        "Get-ScheduledTask | ForEach-Object { "
        "$i = $_ | Get-ScheduledTaskInfo; "
        "$a = @($_.Actions)[0]; "
        "$x = ''; "
        "if ($a) { $x = [string]$a.Execute; "
        "if ($a.Arguments) { $x = $x + ' ' + $a.Arguments } } "
        "$d = ''; if ($_.Date) { $d = [string]$_.Date } "
        "$n = ''; "
        "if ($i -and $i.NextRunTime -and $i.NextRunTime.Year -gt 2000) "
        "{ $n = [string]$i.NextRunTime } "
        "$l = ''; if ($i) { $l = [string]$i.LastTaskResult } "
        "$b = ''; try { if ([string]$_.Settings.DisallowStartIfOnBatteries "
        "-eq 'True') { $b = 'yes' } else { $b = 'no' } } catch { $b = '' } "
        "$tg = ''; "
        "if (@($_.Triggers).Count -gt 0) { "
        "$t0 = @($_.Triggers)[0]; "
        "$tg = $t0.CimClass.CimClassName; "
        "if ($t0.Repetition -and $t0.Repetition.Interval) "
        "{ $tg = $tg + '#RI=' + [string]$t0.Repetition.Interval }; "
        # ★R251 item2b：类名只到"每天"这一层，**用户填的 N 在字段里**——
        # `/sc daily /mo 7` 的 7 存在 DailyTrigger.DaysInterval（本机取证
        # run/r251/trig_dump.txt：那台"每周清空回收站"的任务 DI=7）。
        # 以前这三个字段一个都没取，备注只能猜"每天"。
        # 段数不变（仍是 8 段 / 7 个 TAB），扩展全挤在触发器那一段里用
        # `#KEY=值` 拼——`test_r247_power_conditions` D.2/D.3 那道列数闸不受影响。
        "if ($t0.DaysInterval) { $tg = $tg + '#DI=' + [string]$t0.DaysInterval }; "
        "if ($t0.WeeksInterval) { $tg = $tg + '#WI=' + [string]$t0.WeeksInterval }; "
        "if ($t0.StartBoundary) { $tg = $tg + '#SB=' + [string]$t0.StartBoundary } } "
        "[string]$_.TaskPath + $_.TaskName + \"`t\" + $_.State + \"`t\" + $d + \"`t\" "
        "+ $x + \"`t\" + $n + \"`t\" + $tg + \"`t\" + $l + \"`t\" + $b }"
    )

    # ★R204 GUIA-6：代数计数——快刷两次时旧 loader 的 _apply 作废，
    # 防行重复与 _task_run_map 被后到者整体重置
    gen = getattr(gui, '_task_load_gen', 0) + 1
    gui._task_load_gen = gen

    def _work():
        rows = []    # dict(name, action, run, nxt, status, memo, created)
        dates = {}   # 任务全路径 -> 创建日期 ISO
        ps_ok = False
        try:
            q = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command',
                                PS_SRC],
                               capture_output=True, timeout=90,
                               stdin=subprocess.DEVNULL,
                               creationflags=0x08000000)
            for line in q.stdout.decode('gbk', errors='replace') \
                    .splitlines():
                # ★R204 GUIA-8：与 PS 侧同步改（★R207 NEW3-A6 改转义写法防格式化吞 TAB）
                # ★R247（item2）：PS 侧加了"电池条件"第 8 段 ⇒ 上限 6→7。
                # 这两处**必须一起改**，差一步就是"上次结果"列显示电池文案、
                # 整列右移一格的静默错位（门禁 D 段把两侧数量钉死）。
                seg = line.split('\t', 7)
                if len(seg) < 6 or not seg[0].strip().startswith('\\'):
                    continue
                name, state, date, run, nxt, trig = \
                    (x.strip() for x in seg[:6])
                status = _STATE_CN.get(state, state)
                r = dict(name=name,
                         action=_act_kind(run), run=run,
                         nxt=_fmt_next(nxt), status=status,
                         trigger=trig,
                         last=seg[6].strip() if len(seg) > 6 else '',
                         bat=seg[7].strip() if len(seg) > 7 else '',
                         created=_fmt_created(date))
                r['memo'] = _memo_ps(r)
                rows.append(r)
                if date:
                    dates[name] = date
            ps_ok = bool(rows)
            if not ps_ok:
                logger.warning('PowerShell 任务源返回空，回退 schtasks /v')
        except Exception as e:
            logger.warning(f'PowerShell 任务源失败（回退 schtasks /v）: {e}')

        if not ps_ok:
            try:
                p = subprocess.run(['schtasks', '/query', '/fo', 'CSV',
                                    '/v'],
                                   capture_output=True, timeout=90,
                                   creationflags=0x08000000)
                text = p.stdout.decode('gbk', errors='replace')
                rdr = csv.reader(io.StringIO(text))
                h = [x.strip() for x in (next(rdr, None) or [])]

                def _col(*names):
                    for nm in names:
                        if nm in h:
                            return h.index(nm)
                    return -1

                i_name = _col('任务名', 'TaskName')
                i_next = _col('下次运行时间', 'Next Run Time')
                i_stat = _col('模式', '状态', 'Status')
                i_run = _col('要运行的任务', 'Task To Run')
                i_ctype = _col('计划类型', 'Schedule Type')
                i_stime = _col('开始时间', 'Start Time')
                i_cm = _col('注释', 'Comment')
                i_day = _col('天', 'Day')
                i_rep = _col('重复: 每', 'Repeat: Every')
                i_last = _col('上次结果', 'Last Result')

                def _g(row, i):
                    return row[i].strip() if 0 <= i < len(row) else ''

                if i_name >= 0:
                    for row in rdr:
                        if len(row) <= i_name:
                            continue
                        name = row[i_name].strip()
                        if not name.startswith('\\'):
                            continue
                        r = dict(
                            name=name,
                            action=_act_kind(row[i_run])
                            if 0 <= i_run < len(row) else '',
                            run=_g(row, i_run),
                            nxt=_g(row, i_next), status=_g(row, i_stat),
                            ctype=_g(row, i_ctype), stime=_g(row, i_stime),
                            comment=_g(row, i_cm), day=_g(row, i_day),
                            rep=_g(row, i_rep), trigger='',
                            last=_g(row, i_last),
                            # ★R247：schtasks /query /v 读不到 Settings 这两条
                            # （只有 PS 拿得动）⇒ 留空，列表显示"—"
                            bat='')
                        r['memo'] = _memo_of(name, r)
                        rows.append(r)
                else:
                    # 回退：R170 自适应（无表头详细模式可用时）
                    for row in rdr:
                        if len(row) < 2:
                            continue
                        name_field = next(
                            (f for f in row
                             if f.strip().startswith('\\')), None)
                        if not name_field:
                            continue
                        idx = row.index(name_field)
                        r = dict(
                            name=name_field.strip(), action='', run='',
                            nxt=row[idx + 1].strip()
                            if idx + 1 < len(row) else '',
                            status=row[idx + 2].strip()
                            if idx + 2 < len(row) else '', trigger='')
                        r['memo'] = _memo_of(r['name'], r)
                        rows.append(r)
            except Exception as e:
                # ★ R181（扫描逮住）：lambda 里的 e 在 except 结束后已被
                # 删除，回调执行时必炸 NameError——先定格成字符串
                _msg = f'读取失败: {e}'
                logger.error(f'列出计划任务失败: {e}')
                # ★R207（Qoder GUIB-4/5）：后台线程回投改 _ui_queue
                gui._ui_queue.put(lambda: tree.insert(
                    '', 'end',
                    values=(_msg, '', '', '', '', '', '', '')))
                return
            # 创建日期补齐（PS 主路自带；回退路单独拉）
            try:
                ps = ("Get-ScheduledTask | ForEach-Object { \"{0}`t{1}\" -f "
                      "($_.TaskPath + $_.TaskName), $_.Date }")
                q = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command',
                                    ps],
                                   capture_output=True, timeout=90,
                                   stdin=subprocess.DEVNULL,
                                   creationflags=0x08000000)
                for line in q.stdout.decode('gbk', errors='replace') \
                        .splitlines():
                    if '\t' not in line:
                        continue
                    k, _, v = line.partition('\t')
                    if v.strip():
                        dates[k.strip()] = v.strip()
            except Exception as e:
                logger.warning(f'读取任务创建日期失败（跳过排序）: {e}')

        # ★R277（用户 09-30 实测「跑 .bat 还是有 cmd 黑窗口」——第三个洞）：
        # R275 之前建的任务，动作里存的还是裸 bat，到点照弹黑窗；让用户"删了
        # 重建"是为了一个黑窗折腾他点八下。所以用户**主动**刷新时顺手迁移一次
        # （判据见 `_migrate_bare_bats`，只碰 DeskBits_ 前缀 + 裸 bat + 目标在
        # 盘上 + 还没套过的）。这里在后台线程里跑，不碰 Tk；提示走 _ui_queue。
        if migrate and rows:
            try:
                _mig = _migrate_bare_bats(rows)
                if _mig:
                    gui._ui_queue.put(
                        lambda n=len(_mig): gui._log(
                            f'📅 已把 {n} 条老任务的运行方式改成隐藏窗口'
                            f'（下次到点不再弹黑框）'))
            except Exception as e:
                logger.warning(f'存量任务隐藏迁移失败（跳过，不影响列表）: {e}')
            # ★R331-b：同一趟把老版本生成的启动 bat 里那行坏的错误码展开修掉
            # （判据见 `heal_vm_bat_bytes`；R045 日志包里那两只 bat 就是证据）。
            try:
                _heal = _heal_stale_vm_bats()
                if _heal:
                    gui._ui_queue.put(
                        lambda n=len(_heal): gui._log(
                            f'🔧 已修好 {n} 个老版本的虚拟机启动脚本'
                            f'（以前虚拟机明明启动成功了也会报失败、还会白等 20 秒）'))
            except Exception as e:
                logger.warning(f'存量启动脚本自愈失败（跳过，不影响列表）: {e}')

        def _apply():
            if getattr(gui, '_task_load_gen', 0) != gen:
                logger.info('[任务列表] 过期的加载结果已丢弃（代数不匹配）')
                return
            desbits_only = bool(getattr(gui, '_tasks_desbits_only', None)
                                and gui._tasks_desbits_only.get())
            gui._task_run_map = {}   # ★ R176：任务名→要运行的程序（编辑器预填）
            gui._task_ctype_map = {}  # ★R271-S6：任务名→频率词（编辑路要认「开机时」）
            tree._memo_full = {}     # ★R241：iid→未省略全文（悬停提示用）
            dead_links = []          # ★R271：(任务名, 动作路径) 动作文件已失效
            # ★R241 修回归：绑定挂在这里（主线程），不挂后台线程里
            try:
                _ensure_memo_refit_bound()
            except Exception as _e:
                logger.warning(f'备注列宽自适应绑定失败（不影响列表）: {_e}')
            shown = [r for r in rows
                     if not desbits_only or 'DeskBits' in r['name']]
            # ★ 新创建的在前；无日期（多为系统任务）排后面按名称
            dated = sorted([r for r in shown if r['name'] in dates],
                           key=lambda r: dates[r['name']], reverse=True)
            plain = sorted([r for r in shown if r['name'] not in dates],
                           key=lambda r: r['name'])
            for r in dated + plain:
                name, act, nxt, status = (r['name'], r['action'],
                                          r['nxt'], r['status'])
                gui._task_run_map[name.lstrip('\\')] = r.get('run', '')
                gui._task_ctype_map[name.lstrip('\\')] = _when_v_from_row(r)
                tag = ('running' if '运行' in status
                       else 'disabled' if '已禁用' in status else '')
                # ★ R171/R174：关机/重启红色标（动作归类为主，名称兜底）
                if act in ('关机', '重启') or '_shutdown_' in name \
                        or '_reboot_' in name:
                    tag = 'danger'
                created = r.get('created') or _fmt_created(
                    dates.get(name, ''))
                # ★R207（06批-1）：上次结果出错标橙（关机/重启红不受影响）
                if _last_err(r.get('last', '')) and tag != 'danger':
                    tag = 'errlast'
                # ★R271（用户 `----2026-9-29 02----` item1）：动作文件已不在
                # ⇒ 任务到点会**静默什么都不做**，而列表上看着一切正常
                # （状态"就绪"、上次结果 0）。这就是"vmware 不能自动启动"
                # 却查不出东西的那一面。标橙 + 备注前缀写清，别让用户猜。
                _dead = _act_missing(r.get('run', ''))
                if _dead:
                    dead_links.append((name.lstrip('\\'),
                                       _tr_exe(r.get('run', ''))))
                    r['memo'] = ('⚠ 动作文件不存在，到点不会运行（升级换了'
                                 '目录？删掉重建）｜' + r['memo'])
                    if tag != 'danger':
                        tag = 'errlast'
                # ★R241（用户 item2 细则）：备注列放不下时**保关键字头**
                # （虚拟机名）+ 尾部…，全文留在 _memo_full 里给悬停提示；
                # 列宽随窗口变（<Configure> 里 _refit_memo 重新铺一遍）。
                _iid = tree.insert('', 'end', tags=(tag,),
                                   values=(name.lstrip('\\'), act,
                                           r['memo'], created, nxt, status,
                                           _last_txt(r.get('last', '')),
                                           _bat_cn(r.get('bat', ''))))
                tree._memo_full[_iid] = r['memo']
            if dead_links:
                logger.warning(
                    '计划任务动作文件已失效 %d 条（到点会静默什么都不做）: %s'
                    % (len(dead_links),
                       '; '.join('%s → %s' % d for d in dead_links[:8])))
            _refit_memo()
            # ★R222 阶段 4：隔行底色（running/disabled/danger/errlast 只设 fg）
            ui_layout.zebra_treeview(tree)

        def _refit_memo():
            """按备注列**当前实际宽度**重铺显示文案（窗口拉宽⇒少省略）。

            ★R275：实现挪到模块级 `_memo_refit_tree`（门禁要真调用它才能断言
            "拉宽⇒全文"这条端到端行为，嵌套函数没法从外面拿）。这里只留一层
            委托，`_apply` 里的调用点一个字不动。"""
            return _memo_refit_tree(tree)

        def _ensure_memo_refit_bound():
            """给备注列挂重铺扳机（`<Configure>` + 拖分隔线抬手）。

            ★R241 修回归：这段**必须在主线程调**。上一版把它写在后台
            `_work` 里，Tk 注册回调要走 `createcommand`，非主线程直接抛
            `RuntimeError: main thread is not in main loop`，线程当场死在
            `gui._ui_queue.put(_apply)` 之前 ⇒ 计划任务列表永远空白
            （自动测试 t_tasks 实测 ROWS=0；改造前的同一棵树是 229 行）。
            """
            return _bind_memo_refit(tree)

        # ★R207（Qoder GUIB-4/5）：后台线程回投改 _ui_queue
        gui._ui_queue.put(_apply)

    import threading
    threading.Thread(target=_work, daemon=True,
                     name='TaskListLoader').start()


def _schtasks_run(args, timeout=60):
    """schtasks 统一执行（★ R178，用户"编辑保存提示输入 administrator
    密码，估计失败了"实锤机制）：/change 类操作会打一行交互提示
    "请输入 Administrator 的密码:"，GUI 进程无 stdin 时 schtasks 傻等
    输入直到超时 → 误报失败（修改实际已生效）。stdin=DEVNULL 让它
    立即继续；并把该提示行从输出里剔掉不吓用户。
    返回 (ok, 清理后的输出)。"""
    import subprocess
    p = subprocess.run(args, capture_output=True, timeout=timeout,
                       stdin=subprocess.DEVNULL,
                       creationflags=0x08000000)  # CREATE_NO_WINDOW 防黑框
    out = p.stdout.decode('gbk', errors='replace') + \
        p.stderr.decode('gbk', errors='replace')
    out = '\n'.join(ln for ln in out.splitlines()
                    if '密码' not in ln and '运行方式' not in ln)
    ok = p.returncode == 0 or '成功' in out or 'SUCCESS' in out.upper()
    return ok, out.strip()


def _task_delete(gui, tree):
    """删除选中任务（★ R163：配合创建器，自建任务可删；
    ★R256（48 批三9）：支持**批量删除**——原 `sel[0]` 只删第一条，
    用户"只能一个一个删"。现遍历全部选中：空名跳过、同名去重；
    单条=旧文案，多条=计数确认框一次问清；逐条删，**单条失败不中断**
    其余，收尾统一刷一次列表。日志行沿用词典已有句（不动 log_i18n）。"""
    sel = tree.selection()
    if not sel:
        return
    names, seen = [], set()
    for iid in sel:
        try:
            name = tree.set(iid, 'name')
        except Exception:
            name = ''
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    if not names:
        messagebox.showwarning(i18n.tr('提示', 'Notice'),
                               i18n.tr('任务名为空，无法删除', 'Empty task name'),
                               parent=gui.root)
        return
    if len(names) == 1:
        ask_msg = i18n.tr(f'确定删除计划任务 {names[0]} ？',
                          f'Delete task {names[0]}?')
    else:
        _shown = '\n'.join(names[:10]) + ('\n…' if len(names) > 10 else '')
        ask_msg = i18n.tr(f'确定批量删除下面 {len(names)} 条计划任务？\n{_shown}',
                          f'Delete {len(names)} selected tasks?\n{_shown}')
    if not messagebox.askyesno(i18n.tr('删除任务', 'Delete Task'),
                               ask_msg, parent=gui.root):
        return
    ok_names, bad = [], []
    for name in names:
        try:
            ok, out = _schtasks_run(['schtasks', '/delete', '/tn', name, '/f'])
        except Exception as e:
            ok, out = False, str(e)
        if ok:
            ok_names.append(name)
            gui._log(f'📅 计划任务已删除: {name}')
        else:
            bad.append((name, out))
            logger.error(f'删除计划任务失败: {name}: {out}')
    if bad:
        _b = '；'.join(f'{n}（{(o or "").strip()[:80]}）' for n, o in bad)
        messagebox.showwarning(
            i18n.tr('部分失败', 'Partially failed'),
            i18n.tr(f'已删除 {len(ok_names)} 条，失败 {len(bad)} 条：\n{_b}',
                    f'Deleted {len(ok_names)}, failed {len(bad)}:\n{_b}'),
            parent=gui.root)
    _load_tasks(gui, tree)


def _task_op(gui, tree, op, op_name):
    sel = tree.selection()
    if not sel:
        return
    name = tree.set(sel[0], 'name')
    import subprocess
    # ★ R177（用户"点禁用提示 无效参数/disable"实锤）：本机与 win11 的
    # schtasks 都不收顶层 /disable /enable（实测 报"无效参数/选项"），
    # 必须用 /change 承载；/run 顶层可用保留
    args = {'run': ['schtasks', '/run', '/tn', name],
            'enable': ['schtasks', '/change', '/tn', name, '/enable'],
            'disable': ['schtasks', '/change', '/tn', name, '/disable']}[op]
    try:
        ok, out = _schtasks_run(args)
        if ok:
            gui._log(f'📅 计划任务[{op_name}] {name} 完成')
            # ★R250 三3e（用户 42 批「禁用了，应该提醒刷新下，不要自动刷新」）：
            # win11 上 `schtasks /change` 改完，查询接口不一定立刻给出新状态，
            # 自动刷新有几率把列表刷回"还没改"的样子 ⇒ 用户读成"点了没反应/
            # 状态又弹回去"。所以这里**不自动刷**（也不学 ★R187 创建那样补一
            # 次 after），只把"要点一下刷新才看得到"写进白话提示里。
            messagebox.showinfo(
                i18n.tr('完成', 'Done'),
                i18n.tr(f'{op_name}: {name}\n\n列表里的状态不会自己变，'
                        f'请点上方"🔄 刷新"看最新状态（win11 生效略慢）。',
                        f'{op_name}: {name}\n\nThe list will not update by '
                        f'itself - click "🔄 Refresh" above to see the latest '
                        f'status (Win11 is a bit slow).'),
                parent=gui.root)
        else:
            hint = '（系统自带任务通常需要管理员权限运行本软件）' \
                if '拒绝访问' in out or '拒绝' in out else ''
            messagebox.showwarning(i18n.tr('失败', 'Failed'),
                                   out.strip()[:300] + hint, parent=gui.root)
    except Exception as e:
        logger.error(f'计划任务操作失败: {e}')


def _task_edit(gui, tree, colors):
    """★ R176（用户 -06/5"最好是我们自己的编辑器"）：编辑计划任务。

    schtasks /change 支持改：开始时间(/st)、要运行的程序(/tr)；
    触发周期（每 N 分钟等）不支持原地改——如实提示"删了重建"。"""
    sel = tree.selection()
    if not sel:
        messagebox.showinfo(i18n.tr('提示', 'Notice'),
                            i18n.tr('先在列表选中一个任务',
                                    'Select a task first'),
                            parent=gui.root)
        return
    name = tree.set(sel[0], 'name')
    if not name:
        messagebox.showwarning(i18n.tr('提示', 'Notice'),
                               i18n.tr('任务名为空', 'Empty task name'),
                               parent=gui.root)
        return
    run_map = getattr(gui, '_task_run_map', {})
    # ★R271-S6：选中这条任务当前是不是「开机时」触发（列表加载时记下的）
    _boot_cu = _boot_task_needs_catch_up(
        getattr(gui, '_task_ctype_map', {}).get(name, ''))
    win = tk.Toplevel(gui.root)
    win.title(i18n.tr('编辑计划任务', 'Edit Task'))
    # ★R250 三3b（用户 42 批「点编辑这块，在我的缩放 100+ 的 win11 上面显示
    # 不太好」）：原先写死 `520x360`——同样的字在高缩放下要占更多像素，固定
    # 框子把"保存修改"整枚挤到框外（★R192b 关于窗当年是同一个症状、同一个
    # 解法）。这里只留一个**起始档**，真正的尺寸构建完按内容量（见 `_fit_edit`）。
    _w0 = ui_layout.scaled_size(520)
    win.geometry('%dx%d' % (_w0, ui_layout.scaled_size(360)))
    win.configure(bg=colors['bg'])
    win.transient(gui.root)
    win.grab_set()
    ttk.Label(win, text=f'{name}',
              font=ui_layout.app_font(11, 'bold'),
              foreground=colors['primary'],
              background=colors['bg']).pack(anchor=tk.W, padx=14,
                                            pady=(12, 2))
    _desc = ttk.Label(win, text=i18n.tr(
        '可修改：开始时间（HH:MM，仅每日/每周类任务）、要运行的程序或命令、'
        '电源条件（笔记本）。\n触发周期（每 N 分钟/小时/天）系统不支持原地'
        '修改——需要的话删除后重建。',
        'Editable: start time (HH:MM), program/command, power conditions.\n'
        'Trigger interval is NOT editable in place — delete and recreate.'),
        foreground=colors['text_secondary'], justify=tk.LEFT,
        wraplength=ui_layout.scaled_size(480))
    _desc.pack(anchor=tk.W, padx=14, pady=(0, 8))
    st_v = tk.StringVar()
    _run_raw = run_map.get(name, '')
    # ★R275（item2 连带）：动作被换成隐藏启动器之后，编辑框不能给用户看
    # `wscript.exe "…\hidden_launchers\x.vbs"` 这串天书——预填**真正在跑的那个
    # 文件**。比对基准同步用这一份：用户没动过就**不发 /tr**（否则一保存就把
    # 隐藏改回直跑 bat，黑窗又回来了）。非启动器的任务一个字都不改地照旧
    # 显示原始动作串（`shutdown.exe /s /t 10` 这类带参数的不能剥）。
    _tgt, _is_hl = _unwrap_action(_run_raw)
    _run_disp = _tgt if _is_hl and _tgt else _run_raw
    tr_v = tk.StringVar(value=_run_disp)
    f1 = ttk.Frame(win); f1.pack(fill=tk.X, padx=14, pady=3)
    ttk.Label(f1, text=i18n.tr('开始时间:', 'Start time:'),
              width=12).pack(side=tk.LEFT)
    ttk.Entry(f1, textvariable=st_v, width=10).pack(side=tk.LEFT)
    f2 = ttk.Frame(win); f2.pack(fill=tk.X, padx=14, pady=3)
    ttk.Label(f2, text=i18n.tr('程序/命令:', 'Program/Command:'),
              width=12).pack(side=tk.LEFT)
    ttk.Entry(f2, textvariable=tr_v).pack(side=tk.LEFT, fill=tk.X,
                                          expand=True)
    # ★R247（用户 item2）：**存量任务**同样要能改这两条——用户机上那批
    # VMware / v2rayN 任务早就带着系统默认值"使用电池时不启动"建出来了，
    # 只能删了重建太折腾。预填走**只读回读**（`_read_power_flags`）：读不到
    # 就照实说"没读到"，不拿默认值冒充实读（★R241 同一口径）。
    _ac_cur, _sb_cur, _pw_ok0 = _read_power_flags(name)
    # ★R248（用户 40 批 item0）：Windows 规则里"电池时停止"依赖"只在交流电才启动"
    # ——上面没勾、这枚单独 True 是**无效组合**（XML 手建的任务可能出现）。
    # 归一化后再建 var，既让界面联动成立，也让下面的"有没有改动"比较口径一致。
    if not _ac_cur:
        _sb_cur = False
    _ac_v = tk.BooleanVar(value=bool(_ac_cur))
    _sb_v = tk.BooleanVar(value=bool(_sb_cur))
    f3 = ttk.Frame(win); f3.pack(fill=tk.X, padx=14, pady=(6, 0))
    ttk.Label(f3, text=i18n.tr('⚡ 电源条件:', '⚡ Power conditions:'),
              width=12).pack(side=tk.TOP, anchor=tk.W)
    _pf = ttk.Frame(f3); _pf.pack(side=tk.LEFT, fill=tk.X, expand=True)
    _ac_chk2 = ttk.Checkbutton(
        _pf, variable=_ac_v,
        text=i18n.tr('只在交流电（插着电源适配器）时才启动',
                     'Start only on AC power (plugged in)'))
    _ac_chk2.pack(anchor=tk.W)
    _sb_chk2 = ttk.Checkbutton(
        _pf, variable=_sb_v,
        # ★R250 三3（用户 42 批「它的意思是不让启动吧，但是启动了就不管了吧，
        # 它只管启动对不对？」——问得反了，而旧文案"停止该任务"确实读得出那个
        # 意思）：这枚管的是**跑着的时候**拔电要不要停，管"到点能不能启动"的
        # 是上面那枚。措辞把分工写进字面，不再靠用户猜。
        text=i18n.tr('改用电池供电时，停止正在运行的任务（需先勾上面那枚）',
                     'Stop a RUNNING task when switching to battery '
                     '(needs AC-only ticked)'))
    _sb_chk2.pack(anchor=tk.W)

    def _sync_pw_dep2():
        if _ac_v.get():
            _sb_chk2.configure(state='normal')
        else:
            _sb_v.set(False)
            _sb_chk2.configure(state='disabled')
    _ac_chk2.configure(command=_sync_pw_dep2)
    _sync_pw_dep2()
    _pw_hint = ttk.Label(_pf, justify=tk.LEFT,
                         foreground=colors['text_secondary'],
                         font=ui_layout.app_font(9),
                         wraplength=ui_layout.scaled_size(460),
                         text=(i18n.tr('两枚的分工：上面那枚管「到点能不能启动」，'
                                       '这枚管「正在跑的任务拔电要不要停」。'
                                       '两枚都不勾＝插不插电源都能启动、'
                                       '跑着也不会中途停（笔记本建议这样）',
                                       'The first controls whether a task may '
                                       'START on battery; the second whether a '
                                       'RUNNING task gets STOPPED. Both off = '
                                       'starts and keeps running on battery '
                                       '(recommended for laptops)')
                                if _pw_ok0 else
                                i18n.tr('（没读到这条任务当前的电源条件，下面按'
                                        '"不限制"显示；点保存会照此写入）',
                                        '(current values unreadable — saving '
                                        'writes "no restriction")')))
    _pw_hint.pack(anchor=tk.W)

    def _save():
        args = ['schtasks', '/change', '/tn', name]
        st = st_v.get().strip().replace('：', ':')
        tr = tr_v.get().strip()
        if st:
            if not re.match(r'^\d{1,2}:\d{2}$', st):
                messagebox.showwarning(
                    i18n.tr('提示', 'Notice'),
                    i18n.tr('时间格式 HH:MM，如 09:30', 'Use HH:MM'),
                    parent=gui.root)
                return
            args += ['/st', st]
        # ★R277（用户 09-30 实测「拖到底能看见了，但是跑 .bat 还是有 cmd 黑窗口」）：
        # ★R275 那次只在**新建**这条路套隐藏启动器，编辑保存把框里的串**逐字**写回
        # `/tr` ⇒ 用户改个时间点，黑窗就回来了。现在三处（新建/编辑/存量迁移）共用
        # `_hide_action` 一个口径；比对基准也从"界面显示的那串"换成"任务里现在存的
        # 那串"（`_run_raw`）——只要最终动作与现状不同就写，用户没动但这本是条裸 bat
        # 时顺手修好。已是启动器时 `_hide_action` 原样返回 ⇒ 相等 ⇒ 不多发一次写。
        # ★R285-N5：这一处补上"任务归属筛"（走 `_hide_action_for_task`）——第三方
        # 任务的动作逐字不动，只有 `DeskBits_` 开头的（我们建的）才套壳。
        if tr:
            _tv = _hide_action_for_task(name, tr)[0]
            if _tv != _run_raw:
                args += ['/tr', _tv]
        # ★R247：电源条件变了也算"有改动"（这两条 schtasks 管不了，得由
        # `_apply_power_settings` 单独走 PowerShell 写）；否则用户只勾一枚
        # 会被"没有要修改的内容"挡回去。当前值**没读到**时一律算"变了"——
        # 宁可多写一遍，也不许让用户觉得"点了保存没反应"。
        _pw_dirty = (not _pw_ok0
                     or (_ac_v.get(), _sb_v.get()) != (_ac_cur, _sb_cur)
                     or _boot_cu)
        if len(args) == 4 and not _pw_dirty:
            messagebox.showinfo(i18n.tr('提示', 'Notice'),
                                i18n.tr('没有要修改的内容', 'Nothing to change'),
                                parent=gui.root)
            return
        if len(args) > 4:
            try:
                ok, out = _schtasks_run(args)
            except Exception as e:
                ok, out = False, str(e)
            if not ok:
                messagebox.showwarning(i18n.tr('失败', 'Failed'),
                                       out.strip()[:300], parent=gui.root)
                return
            gui._log(f'📅 计划任务已修改: {name}')
        _pw_line = ''
        if _pw_dirty:
            # 成败自带 ⚡/⚠ 已在文案里（_apply_power_settings 按语言档出词），
            # 这里只贴上去
            _pw_ok1, _pw_txt = _apply_power_settings(
                gui, name, _ac_v.get(), _sb_v.get(), catch_up=_boot_cu)
            if _boot_cu and _pw_ok1:
                logger.info(f'计划任务({name}) 触发=开机时 ⇒ 编辑保存已补上'
                            f'"错过补跑"(StartWhenAvailable)')
            _pw_line = '\n' + _pw_txt
        messagebox.showinfo(
            i18n.tr('完成', 'Done'),
            i18n.tr('已保存修改', 'Changes saved') + _pw_line,
            parent=gui.root)
        win.destroy()
        # ★R247：改完刷一次列表——新列"电池下不启动"必须跟着变，否则用户
        # 看到的是改之前的状态（此前编辑完压根不刷列表）。
        try:
            _load_tasks(gui, tree)
        except Exception as _e:
            logger.debug(f'编辑后刷新任务列表失败: {_e}')

    ttk.Button(win, text=i18n.tr('保存修改', 'Save'),
               style='primary.TButton', command=_save).pack(
        anchor=tk.E, padx=14, pady=(8, 10))

    # ★R250 三3c（用户 42 批「编辑这块的复选项，不太好看，特别是不知道选中还是
    # 没有选中」）当年记的是**病因**：勾选态当时由主窗 `_build_ui` 收尾**一次性**
    # 递归挂的 ☑/☐ 文字前缀表达，而样式层又把 clam 的原生小框摘掉了（★R222 定案：
    # 那个勾画出来像 ✗），这个窗是点按钮才造的 Toplevel、不在那次递归里 ⇒
    # 在这枚窗里"勾上"和"没勾"长得一模一样。
    # ★R315（批71，挂账 #272）已把这层因果**拆掉**：原生指示器回到样式层（"色块档"：
    # 勾选=整块 primary 且笔画与块同色 ⇒ 看不见叉），每个复选框出生即带方框，
    # 事后新建的行与后弹出的 Toplevel 都不再需要借助手补挂 ⇒ 这里**只**留单选钮那一刀。
    # ★R251 item4：单选钮（●/○）仍靠文字前缀——编辑窗里"停止条件"那几项就是单选组，
    # 用户 43 批点名的"看不见小圈圈"在这儿仍然成立，故助手不许删。
    _mark_task_selectors(gui, win)

    def _fit_edit():
        """★R250 三3b：按**内容**量尺寸（口径照搬 ★R192b 关于窗那套）

        先按起始档摆一次，再读 `winfo_reqwidth` 定宽、把两段说明的 `wraplength`
        跟着窗宽重算（原先写死 480：换英文/调大字号就整段糊在一起），最后按
        重排后的 `reqheight` 定高——这样"保存修改"那枚再也不会掉到框外。"""
        try:
            win.update_idletasks()
            sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
            w = max(_w0, min(win.winfo_reqwidth(), int(sw * 0.92)))
            _pad = ui_layout.scaled_size(40)
            for _lb in (_desc, _pw_hint):
                try:
                    _lb.configure(wraplength=max(ui_layout.scaled_size(220),
                                                 w - _pad))
                except Exception:
                    pass
            win.update_idletasks()
            h = max(ui_layout.scaled_size(240),
                    min(win.winfo_reqheight(), sh - ui_layout.scaled_size(80)))
            win.geometry('%dx%d' % (w, h))
        except Exception as e:
            logger.debug(f'编辑计划任务窗自适应失败: {e}')
    win.after(60, _fit_edit)


def _parse_delay_duration(s):
    """★R209c（用户 12 批定案）：延时输入解析，返回 (总秒数, None) 或
    (None, 错误提示)。

    格式=00:00:00:00 固定四段（天:时:分:秒）；**超上限不报错，直接钳
    位跳回最大 24/60/60**（天段≤99），与输入框失焦归一行为一致。兼容
    三段 时:分[:秒]、点号分隔、裸数字（=分钟）、中文组合式。仅两类
    仍报错：不是纯数字格式、总时长不足 1 分钟（R185 定案）。"""
    s = (s or '').strip()
    if not s:
        return None, '请填写时长'
    s = s.replace('：', ':').replace('　', '').replace(' ', '')
    # 全角数字转半角
    s = s.translate(str.maketrans('０１２３４５６７８９', '0123456789'))

    # ① 组合式（中文单位，附加兼容；超上限同样钳位）
    m = re.match(
        r'^(?:(\d{1,3})天)?(?:(\d{1,2})小时|(\d{1,2})时)?'
        r'(?:(\d{1,2})分钟|(\d{1,2})分)?(?:(\d{1,2})秒)?$', s)
    if m and any(m.groups()):
        d = int(m.group(1) or 0)
        h = min(int(m.group(2) or m.group(3) or 0), 24)
        mn = min(int(m.group(4) or m.group(5) or 0), 60)
        sec = min(int(m.group(6) or 0), 60)
        total = min(d, 99) * 86400 + h * 3600 + mn * 60 + sec
        if total < 60:
            return None, '延时至少 1 分钟'
        return total, None
    # ② 冒号式：四段 天:时:分:秒（主格式）/ 三段 时:分[:秒]
    # （'.' 兼容小键盘点号分隔，旧版即收）
    if '.' in s and '天' not in s:
        s = s.replace('.', ':')
    m = re.match(
        r'^(\d{1,3})(?::(\d{1,2}))?(?::(\d{1,2}))?(?::(\d{1,2}))?$', s)
    if m and ':' in s:
        seg = [int(g) for g in m.groups() if g is not None]
        while len(seg) < 4:
            seg.insert(0, 0)
        d = min(seg[0], 99)
        h, mn, sec = (min(seg[1], 24), min(seg[2], 60), min(seg[3], 60))
        total = d * 86400 + h * 3600 + mn * 60 + sec
        if total < 60:
            return None, '延时至少 1 分钟'
        return total, None
    # ③ 裸数字=分钟（超 60 钳位；UI 失焦归一后此路近乎不可达）
    if s.isdigit():
        n = min(int(s), 60)
        if n < 1:
            return None, '延时至少 1 分钟'
        return n * 60, None
    return None, '时长格式不对，示例：00:01:30:00（天:时:分:秒）'


def _normalize_delay_str(s):
    """★R210（NEW4-A3/A6）：延时输入归一（模块级纯函数，可单测）——
    归一成 00:00:00:00 四段；时/分/秒超上限钳位跳回 24/60/60（天≤99）；
    **单段裸数字按"分钟"放位**（与 _parse_delay_duration 同口径，消除
    此前"解析器=分钟、归一=秒"的 60 倍双语义）；2~3 段按右对齐（最右
    =秒）；含空段/非数字返回 None（不猜意思，交给创建时校验报错）。"""
    s = (s or '').strip()
    if not s:
        return None
    s = (s.replace('：', ':').replace('.', ':')
          .replace('　', '').replace(' ', ''))
    s = s.translate(str.maketrans('０１２３４５６７８９', '0123456789'))
    if any(p == '' for p in s.split(':')):
        return None   # NEW4-A6：'1::30' 这类空段不猜意思
    parts = [p for p in s.split(':') if p != '']
    if not parts or len(parts) > 4 or not all(p.isdigit() for p in parts):
        return None
    nums = [int(p) for p in parts]
    if len(nums) == 1:
        nums = [0, 0, min(nums[0], 60), 0]   # 裸数字=分钟
    while len(nums) < 4:
        nums.insert(0, 0)
    d, h, mn, sec = nums
    return (f'{min(d, 99):02d}:{min(h, 24):02d}:'
            f'{min(mn, 60):02d}:{min(sec, 60):02d}')


def build_sys_tab(tab_sys, gui, colors):
    """构建"🧰 系统相关"页签（锁屏/事件查看器/计划任务）"""

    # ---- 🔒 锁屏（自带画面） ----
    lock_frame = ttk.LabelFrame(tab_sys, text=i18n.tr('🔒 锁屏', '🔒 Lock Screen'),
                                padding='10', style='Card.TLabelframe')
    lock_frame.pack(fill=tk.X, pady=(0, 6))
    lock_row = ttk.Frame(lock_frame)
    lock_row.pack(fill=tk.X)
    ttk.Button(lock_row, text=i18n.tr('🔒 立即锁屏', '🔒 Lock Now'),
               command=lambda: lock_now(gui),
               style='primary.TButton').pack(side=tk.LEFT)
    # ★ R161：锁屏独立密码（不连家长模式；没设时按住 3 秒解锁）
    ttk.Button(lock_row, text=i18n.tr('🔑 设置锁屏密码', '🔑 Set Lock Password'),
               command=lambda: set_lock_password(gui),
               style='secondary.TButton').pack(side=tk.LEFT,
                                               padx=(8, 0))
    _pwd_state = i18n.tr('已设置', 'Set') if lock_screen_has_pwd() \
        else i18n.tr('未设置（按住 3 秒解锁）', 'Not set (hold 3s to unlock)')
    ttk.Label(lock_row, text=_pwd_state,
              foreground=colors['text_secondary'],
              font=ui_layout.app_font(9)).pack(side=tk.LEFT, padx=(6, 0))
    ttk.Label(lock_row, text=i18n.tr('壁纸:', 'Wallpaper:')).pack(
        side=tk.LEFT, padx=(14, 4))
    _wp_labels = [i18n.tr('无', 'None')] + \
        [i18n.tr(v, v) for v in ('深色', '暗黑', '浅色', '护眼')] + \
        [i18n.tr('自定义…', 'Custom…')]
    _wp_ids = ['', 'dracula', 'amoled', 'light', 'eyecare', '__custom__']

    def _wp_current_label():
        preset = gui._app_settings.get('lock_preset', '')
        if preset and preset in ('dracula', 'amoled', 'light', 'eyecare'):
            from gui.lock_screen import LOCK_PRESETS
            return i18n.tr(LOCK_PRESETS[preset], LOCK_PRESETS[preset])
        if gui._app_settings.get('lock_image', ''):
            return i18n.tr('自定义…', 'Custom…')
        return _wp_labels[0]

    wp_var = tk.StringVar(value=_wp_current_label())
    wp_combo = ttk.Combobox(lock_row, textvariable=wp_var, width=9,
                            state='readonly', values=_wp_labels)

    def _on_wp_change(_ev=None):
        sel = wp_var.get()
        idx = _wp_labels.index(sel) if sel in _wp_labels else 0
        wid = _wp_ids[idx]
        if wid == '__custom__':
            pick_image(gui)
            wp_var.set(_wp_current_label())
            return
        if wid:
            gui._app_settings['lock_preset'] = wid
            gui._app_settings.pop('lock_image', None)
            from gui.lock_screen import LOCK_PRESETS
            gui._log(f'🖼️ 锁屏壁纸 = {LOCK_PRESETS[wid]}（下次锁屏生效）')
        else:
            gui._app_settings.pop('lock_preset', None)
            gui._app_settings.pop('lock_image', None)
            gui._log('🖼️ 锁屏壁纸 = 无（下次锁屏生效）')
        gui._save_app_settings()

    wp_combo.bind('<<ComboboxSelected>>', _on_wp_change)
    wp_combo.pack(side=tk.LEFT)
    ttk.Label(lock_row, text=i18n.tr('字体:', 'Font:')).pack(side=tk.LEFT,
                                                             padx=(14, 4))
    font_var = tk.StringVar(
        value=gui._app_settings.get('lock_font', '中'))

    def _font_change():
        gui._app_settings['lock_font'] = font_var.get()
        gui._save_app_settings()
        gui._log(f'🔒 锁屏字体已设为 [{font_var.get()}]（下次锁屏生效）')

    ttk.Combobox(lock_row, textvariable=font_var, width=5, state='readonly',
                 values=['小', '中', '大', '超大'],
                 postcommand=None).pack(side=tk.LEFT)
    font_var.trace_add('write', lambda *_: _font_change())
    # ★R216 阶段 2b：原"（图片显示在时钟上方，支持 PNG/JPG/GIF/BMP）"是
    # pack(LEFT) 的常驻行内小字，把后面的 ❓ 帮助按钮挤出卡片右缘（默认窗口
    # 宽下点不到）。文案并入下方 wraplength 小字行，❓ 回到可视区。
    def _help_lock():
        messagebox.showinfo(
            i18n.tr('🔒 锁屏说明', '🔒 Lock Screen Help'),
            i18n.tr(
                '① 点"立即锁屏"→ 全屏锁定画面出现（大时钟+壁纸图片）。\n'
                '② 解锁：输入锁屏密码（没设密码则按住按钮 3 秒）。\n'
                '③ 锁定期间开始菜单的"注销"被隐藏（防绕过），解锁自动恢复。\n'
                '④ 壁纸下拉可选内置四套主题图或导入自定义图片；字号同卡片设置。\n'
                '⑤ 锁的是"画面"不是"程序"——后台功能照常运行。',
                '① Click Lock Now → fullscreen lock appears.\n'
                '② Unlock: enter lock password (or hold button 3s if none).\n'
                '③ Start menu "Sign out" is hidden while locked.\n'
                '④ Wallpaper dropdown: 4 built-in themes or custom image.\n'
                '⑤ Locks the screen only — background features keep running.'),
            parent=gui.root)
    ttk.Button(lock_row, text='❓', width=3,
               command=_help_lock).pack(side=tk.LEFT, padx=(6, 0))

    ttk.Label(lock_frame,
              text=i18n.tr(
                  '自带全屏锁屏画面：覆盖所有显示器，大时钟+壁纸图片；'
                  '解锁=输入家长密码（没设密码则按住按钮 3 秒）。'
                  '锁的是"画面"不是"程序"——后台整理/下载记录/家长计时照常。'
                  '家长模式到时会自动弹本锁屏。'
                  '壁纸下拉选内置主题图或"自定义…"导入自己的图片；'
                  '图片显示在时钟上方（PNG/JPG/GIF/BMP）。',
                  'Custom fullscreen lock covering all monitors: big clock + '
                  'wallpaper; unlock with parental password (or hold 3s if '
                  'no password set). The session keeps running in background. '
                  'Parental mode auto-shows this when time is up. '
                  'Wallpaper dropdown: built-in themes or "Custom…" to import '
                  'your own image; it shows above the clock (PNG/JPG/GIF/BMP).'),
              foreground=colors['text_secondary'],
              font=ui_layout.app_font(9), justify=tk.LEFT,
              wraplength=760).pack(anchor=tk.W, pady=(6, 0))

    # ---- 🖥️ 桌面系统图标（★R292-②：一键显隐此电脑/回收站/网络…） ----
    # 用户原话：「"系统"选项卡里面"锁屏"旁边或者下面，加个功能"一键显示系统图标"，
    # 相当于桌面右键→个性化→主题→桌面图标设置里那五个勾」。放在锁屏卡**下面**
    # 独立一张卡：挤进 lock_row 会被 test_phase2b_bars ⑤b 那条"行内不许再有长提示"
    # 判成回归，而且这功能跟锁屏不是一回事。
    sysic_frame = ttk.LabelFrame(tab_sys,
                                 text=i18n.tr('🖥️ 桌面系统图标',
                                              '🖥️ Desktop System Icons'),
                                 padding='10', style='Card.TLabelframe')
    sysic_frame.pack(fill=tk.X, pady=(0, 6))
    sysic_row = ttk.Frame(sysic_frame)
    sysic_row.pack(fill=tk.X)
    # 初始态读注册表（只读，不写）：五项都在显示才勾上——新机器上默认不显示，
    # 所以刚装好这枚是**空勾**，点下去才是"我要它们出来"。
    try:
        _sysic_init = bool(gui.icon_manager.virtual_icons_all_shown())
    except Exception as e:
        logger.warning(f'[系统图标] 初始态读不动（复选框按未勾显示）: {e}')
        _sysic_init = False
    sysic_v = tk.BooleanVar(value=_sysic_init)
    # ★R297-D：暴露给设置页——两页共用同一把「系统图标常驻」总闸，谁改了都要把
    # 对侧那枚的回显带走（gui/settings_panel.py::on_sticky_apply 读它）。
    gui._sysic_var = sysic_v

    def _on_sysic_toggle():
        want = bool(sysic_v.get())
        # ★R230b 口径：中文变量不许插进英文模板（英文档会带中文尾巴）⇒ 中/英各一份
        word, word_en = ('显示', 'shown') if want else ('隐藏', 'hidden')
        im = getattr(gui, 'icon_manager', None)
        fn = getattr(im, 'set_virtual_icons_shown', None)
        if not callable(fn):
            sysic_v.set(not want)
            gui._log(i18n.tr('⚠ 图标管理器没起来，系统图标开关这次无效',
                             '⚠ Icon manager unavailable; toggle ignored'))
            logger.warning('[系统图标] 结果=拒(icon_manager 不可用)')
            return
        try:
            n, failed = fn(want)
        except Exception as e:
            sysic_v.set(not want)
            logger.error(f'[系统图标] 一键{word}异常（复选框已退回）: {e}')
            gui._log(i18n.tr(f'⚠ 系统图标{word}失败：{e}',
                             f'⚠ Failed to set system icons: {e}'))
            return
        # ★R297-D（用户 10-05 09:5x win11 真机追报"点了一键显示→切『小朋友』→点
        # 『全部』，系统图标又出现了"）：这枚开关此前**只写注册表**，不动 dock 的
        # 常驻判据（core/dock.py 切分类时读的是 self.system_resident，值来自设置页
        # 「🖥 系统图标」那枚，默认 True），所以下一次切分类照样把那五枚请回来。
        # 用户定案②：这枚=「系统图标常驻」的第二个入口 ⇒ 注册表写成了就必须同步
        # 总闸。★R226 次序：先写内存表 → 落盘 → 推运行时 → 最后动对侧回显（进程内
        # 异常不至于丢设置）。n=0（一项都没写成）时**一个字都不许动**，否则界面
        # 回退了、判据却留成新值 = 用户读成"点没点都一样"。
        if n:
            try:
                gui._app_settings['system_icons_resident'] = bool(want)
                gui._save_app_settings()
                gui.dock.system_resident = bool(want)
                _chip = getattr(gui, '_system_resident_var', None)
                if _chip is not None:
                    _chip.set(bool(want))
                    # 那枚 chip 的高亮是 refresh_sticky_chip 手动刷的（不是变量绑定），
                    # 只 set 变量会得到"值变了、勾还亮着"的半截回显
                    _rf = getattr(gui, '_refresh_sticky_chip', None)
                    if _rf is not None:
                        _rf(u'🖥 系统图标', 'resident')
            except Exception as e:
                logger.warning(f'[系统图标] 常驻总闸同步失败（显隐本身已写盘）: {e}')
        # 刷新口径：历轮实测 F5 冲刷 0.05s 就让注册表显隐生效，而 ASSOCCHANGED 是
        # 重型刷新（dock.py:1621-1624 那次"全部→分类屏幕空白偏长"的元凶）⇒ 先 F5，
        # 刷不动才退回 SHChangeNotify，两条都不成也只是"要用户自己按一次 F5"。
        try:
            gui.dock._force_listview_refresh()
        except Exception as e:
            logger.warning(f'[系统图标] F5 冲刷失败，退回桌面重建通知: {e}')
            try:
                im.notify_virtual_change()
            except Exception as e2:
                logger.warning(f'[系统图标] 重建通知也失败（显隐已写，等 Explorer 自己刷）: {e2}')
        # ★R297-B（用户 10-05 08:4x win11 真机"点一次桌面就变乱"）：这次 F5 冲刷让
        #   Explorer 按自己那份排序方式整屏重排过，必须把布局摆回来。顺序是硬要求——
        #   摆位只能排在冲刷**之后**（先摆后刷＝摆好的位置当场被打回，★R232 同款）。
        #   摆位是善后动作不是主功能：失败只记一行日志，不许把这次点击炸掉。
        try:
            gui.dock.relayout_after_sysicon_change()
        except Exception as e:
            logger.warning(f'[系统图标] 收尾摆位失败（显隐本身已生效）: {e}')
        if not n:
            sysic_v.set(not want)
            gui._log(i18n.tr('⚠ 系统图标没写成（注册表被安全软件拦了？明细见日志）',
                             '⚠ Registry write failed (blocked by security software?)'))
        elif failed:
            gui._log(i18n.tr(
                f'🖥️ 系统图标{word}：{n} 项已写，{len(failed)} 项失败（{"、".join(failed)}）',
                f'🖥️ System icons {word_en}: {n} done, {len(failed)} failed'))
        else:
            gui._log(i18n.tr(
                f'🖥️ 桌面系统图标已全部{word}（{n} 项：此电脑/回收站/用户的文件/控制面板/网络）',
                f'🖥️ All desktop system icons {word_en} ({n} items)'))

    ttk.Checkbutton(sysic_row, text=i18n.tr(
                        '一键显示系统图标（此电脑/回收站/用户的文件/控制面板/网络）',
                        'Show system icons (This PC / Recycle Bin / User files / '
                        'Control Panel / Network)'),
                    variable=sysic_v, command=_on_sysic_toggle).pack(side=tk.LEFT)
    # ★R292 用户回话：这张卡**只要那一行**——"一键显示系统图标（五个名字）"，
    # 原来那三行大白话提示判成啰嗦 ⇒ 删掉；用法写在 README 的"系统"页小节里。

    # ---- 👀 条件监视（进程出现/消失 → 动作；典型=监视虚拟机） ----
    mon_frame = ttk.LabelFrame(tab_sys,
                               text=i18n.tr('👀 条件监视（进程出现/消失 → 动作）',
                                            '👀 Condition Watch'),
                               padding='10', style='Card.TLabelframe')
    mon_frame.pack(fill=tk.X, pady=(0, 6))

    # ★ R176：命中时弹窗通知（引擎线程→主线程），动作执行
    # 用户看得见；此前只有日志，用户"命令跑了没跑不知道"
    # ★R207（Qoder GUIB-4）：引擎线程回投改 _ui_queue（root.after 非
    # 主线程调用非线程安全，项目纪律=后台只 put(callable)）
    gui.monitor._notify = lambda name, desc: gui._ui_queue.put(
        lambda: messagebox.showinfo(
            i18n.tr('👀 条件命中', '👀 Condition Hit'),
            i18n.tr(f'[{name}] 触发！\n{desc}\n\n动作已执行。',
                    f'[{name}] triggered!\n{desc}\n\nAction executed.'),
            parent=gui.root))

    # ★R318 A（用户 73 批定案："不是取消的按钮，应该是取消的操作，具体可以在
    # 对话框里面体现"）：关机/重启**排定之后**把"取消这一次"做成对话框里的动作
    # ——选「是」就发 `shutdown /a` 撤销倒计时。顺序不能反过来（先问后排的话，
    # 点取消那一刻系统里根本没有可撤的倒计时，机器照关），所以这个回调挂在
    # condition_monitor 的 Popen **之后**那一步。引擎线程不许直接碰 Tk ⇒
    # 和 `_notify` 同一纪律，只回投 `_ui_queue`（★R207 GUIB-4）。
    def _do_ask_cancel(name, what_zh, what_en):
        if messagebox.askyesno(
                i18n.tr('⏰ 要取消这次%s吗？' % what_zh,
                        '⏰ Cancel this %s?' % what_en),
                i18n.tr('[%s] 已排定：30 秒后%s。\n\n现在取消吗？'
                        '（30 秒内没人点，机器就真的%s了）'
                        % (name, what_zh, what_zh),
                        '[%s] scheduled: %s in 30 seconds.\n\n'
                        'Cancel it now? (if nobody answers within 30 seconds '
                        'it really happens)' % (name, what_en)),
                parent=gui.root):
            gui.monitor.cancel_power_action()

    def _ask_cancel_power(name, act):
        _zh = '关机' if act == 'shutdown' else '重启'
        _en = 'shutdown' if act == 'shutdown' else 'restart'
        gui._ui_queue.put(lambda: _do_ask_cancel(name, _zh, _en))

    gui.monitor._power_ask = _ask_cancel_power

    def _proc_exists(proc):
        """进程当前是否在跑（添加/启用时给"进程名核对"提示用）。
        ★ R177：必须 /FO CSV——/NH 表格的映像名列宽 25 字符，长进程名
        （如 ContextMenuManager.NET.4.0.exe）被截断 → 误报"不存在"
        （引擎本体用 CSV 所以能正常命中，只有这处提示在瞎报）"""
        try:
            p = subprocess.run(['tasklist', '/FI', f'IMAGENAME eq {proc}',
                                '/NH', '/FO', 'CSV'],
                               capture_output=True, timeout=30,
                               creationflags=0x08000000)
            t = p.stdout.decode('gbk', errors='replace').lower()
            return proc.lower() in t and 'info:' not in t
        except Exception:
            return True   # 探测失败不拦

    def _warn_if_absent(proc):
        if proc and not _proc_exists(proc):
            messagebox.showwarning(
                i18n.tr('提示', 'Notice'),
                i18n.tr(f'当前没发现进程 {proc}。\n'
                        f'请确认是确切的 exe 文件名（任务管理器→详细信息'
                        f'里看），名字不对监视永远不会命中。',
                        f'Process {proc} is not running now.\n'
                        f'Make sure it is the exact exe name.'),
                parent=gui.root)

    def _help_mon():
        messagebox.showinfo(
            i18n.tr('👀 条件监视说明', '👀 Condition Watch Help'),
            i18n.tr('监视某个进程的"出现"或"消失"，触发时自动执行动作。\n'
                    '典型用法：监视 vmware-vmx.exe（虚拟机进程）消失 → 关机主机。\n'
                    '每 3 秒轮询一次；关机/重启有 30 秒缓冲（shutdown /a 可取消）。\n'
                    '⚠ 监视"消失"要挑会整个退出的程序：Edge 关窗后后台进程\n'
                    '常驻不归零，"消失"不会触发（Chrome/记事本/画图没问题）。\n'
                    '测试方法：先监视 notepad.exe，打开/关闭记事本验证。\n'
                    '程序名不用写 cmd /k：直接写 notepad.exe 就会正常打开，'
                    '不会多弹一个黑框（★R253）。\n'
                    '技巧：想让命令窗口停留看清结果，命令写 cmd /k 你的命令。',
                    'Watches a process for exit/start, triggers an action.\n'
                    'Typical: watch vmware-vmx.exe exit → shutdown host.\n'
                    'Polls every 3s; shutdown/reboot has 30s buffer.\n'
                    '⚠ For "gone" triggers avoid Edge: it keeps background\n'
                    'processes running, so "gone" never fires.\n'
                    '(Chrome/Notepad/Paint are fine.)\n'
                    'Test with notepad.exe first.\n'
                    'A program name needs no cmd /k: "notepad.exe" opens '
                    'without a console window.\n'
                    'Tip: use "cmd /k yourcommand" to keep the window open.'),
            parent=gui.root)
    # ★ R175：加"状态"列（启用中/已停用），配 启用⇄停止 按钮
    mon_tree = ttk.Treeview(mon_frame,
                            columns=('name', 'proc', 'cond', 'act', 'state'),
                            show='headings', height=4)
    # ★R222 U2：表头此前是裸中文（English 模式下整页就这五个字是中文）
    mon_heads = (('name', 130, i18n.tr('名称', 'Name')),
                 ('proc', 190, i18n.tr('监视进程', 'Watched process')),
                 ('cond', 105, i18n.tr('条件', 'When')),
                 ('act', 105, i18n.tr('动作', 'Action')),
                 ('state', 70, i18n.tr('状态', 'State')))
    for c, w, txt in mon_heads:
        mon_tree.heading(c, text=txt)
        mon_tree.column(c, width=w, anchor=tk.W)
    mon_scroll = ttk.Scrollbar(mon_frame, orient=tk.VERTICAL,
                               command=mon_tree.yview)
    mon_tree.configure(yscrollcommand=mon_scroll.set)
    mon_scroll.pack(side=tk.RIGHT, fill=tk.Y)
    mon_tree.pack(fill=tk.X)
    _COND_TXT = {'gone': i18n.tr('进程消失时', 'when exits'),
                 'present': i18n.tr('进程出现时', 'when starts')}
    _ACT_TXT = {'shutdown': i18n.tr('关机主机', 'shutdown host'),
                'reboot': i18n.tr('重启主机', 'reboot host'),
                'command': i18n.tr('运行命令', 'run command')}

    # ★ R187（用户 04 批）：按动作着色——关机/重启红色警示、运行命令
    # 橙色、停用条目灰色（与上方任务列表同风格）
    # ★R223：颜色取色板 token（浅色/护眼下写死的深色 hex 会"隐形"）
    tag_semantic(mon_tree, 'mon')

    def _reload_mon():
        mon_tree.delete(*mon_tree.get_children())
        for m in gui.monitor.list():
            vals = (
                m.get('name'), m.get('process'),
                _COND_TXT.get(m.get('condition'), m.get('condition')),
                _ACT_TXT.get(m.get('action'), m.get('action')),
                i18n.tr('启用中', 'ON') if m.get('enabled', True)
                else i18n.tr('已停用', 'OFF'))
            if not m.get('enabled', True):
                tag = 'off'
            elif m.get('action') in ('shutdown', 'reboot'):
                tag = 'danger'
            else:
                tag = 'cmd'
            mon_tree.insert('', 'end', values=vals, tags=(tag,))
        # ★R222 阶段 4：隔行底色（danger/off/cmd 三档语义色只设 foreground，不冲突）
        ui_layout.zebra_treeview(mon_tree)

    def _mon_dialog(orig=None):
        """添加/编辑监视对话框（★ R175：orig=None=新增，否则编辑回填）"""
        top = tk.Toplevel(gui.root)
        top.title(i18n.tr('编辑条件监视', 'Edit Watch') if orig else
                  i18n.tr('添加条件监视', 'Add Watch'))
        top.geometry('460x330')
        top.configure(bg=colors['bg'])
        top.transient(gui.root)
        top.grab_set()
        ttk.Label(top, text=i18n.tr(
            '监视某个进程：它"退出"或"启动"时执行动作。\n'
            '例：监视 vmware-vmx.exe 消失 → 关机主机（虚拟机停了主机也休息）。',
            'Watch a process; run an action when it exits or starts.'),
            foreground=colors['text_secondary'], justify=tk.LEFT,
            wraplength=420).pack(padx=12, pady=(10, 6), anchor=tk.W)
        name_v = tk.StringVar(
            value=(orig or {}).get('name') or i18n.tr('我的虚拟机', 'My VM'))
        proc_v = tk.StringVar(
            value=(orig or {}).get('process') or 'vmware-vmx.exe')
        cond_v = tk.StringVar(value=(orig or {}).get('condition') or 'gone')
        act_v = tk.StringVar(value=(orig or {}).get('action') or 'shutdown')
        cmd_v = tk.StringVar(value=(orig or {}).get('command') or '')
        f1 = ttk.Frame(top); f1.pack(fill=tk.X, padx=12, pady=3)
        ttk.Label(f1, text=i18n.tr('名称:', 'Name:'), width=10).pack(side=tk.LEFT)
        ttk.Entry(f1, textvariable=name_v).pack(side=tk.LEFT, fill=tk.X,
                                                expand=True)
        f2 = ttk.Frame(top); f2.pack(fill=tk.X, padx=12, pady=3)
        ttk.Label(f2, text=i18n.tr('进程名:', 'Process:'), width=10).pack(
            side=tk.LEFT)
        ttk.Entry(f2, textvariable=proc_v).pack(side=tk.LEFT, fill=tk.X,
                                                expand=True)
        f3 = ttk.Frame(top); f3.pack(fill=tk.X, padx=12, pady=3)
        ttk.Label(f3, text=i18n.tr('条件:', 'When:'), width=10).pack(side=tk.LEFT)
        for v, txt in (('gone', _COND_TXT['gone']), ('present', _COND_TXT['present'])):
            ttk.Radiobutton(f3, text=txt, value=v, variable=cond_v).pack(
                side=tk.LEFT, padx=(0, 8))
        f4 = ttk.Frame(top); f4.pack(fill=tk.X, padx=12, pady=3)
        ttk.Label(f4, text=i18n.tr('动作:', 'Action:'), width=10).pack(side=tk.LEFT)
        for v, txt in (('shutdown', _ACT_TXT['shutdown']),
                       ('reboot', _ACT_TXT['reboot']),
                       ('command', _ACT_TXT['command'])):
            ttk.Radiobutton(f4, text=txt, value=v, variable=act_v).pack(
                side=tk.LEFT, padx=(0, 8))
        f5 = ttk.Frame(top); f5.pack(fill=tk.X, padx=12, pady=3)
        ttk.Label(f5, text=i18n.tr('命令:', 'Command:'), width=10).pack(
            side=tk.LEFT)
        ttk.Entry(f5, textvariable=cmd_v).pack(side=tk.LEFT, fill=tk.X,
                                               expand=True)
        # ★R251 item4：这枚窗是点按钮才造的 Toplevel，"条件/动作"两组单选
        # 不在主窗启动那次递归里 ⇒ 一样是"看不见小圈圈"，这里一并补上。
        _mark_task_selectors(gui, top)

        def _save():
            proc = proc_v.get().strip()
            if not proc:
                messagebox.showwarning(i18n.tr('提示', 'Notice'),
                                       i18n.tr('先填进程名', 'Enter process name'),
                                       parent=gui.root)
                return
            new_name = name_v.get().strip() or proc
            # ★R221（NEW7-P1-4）：关机/重启是"到点真关机"级别的动作，原来
            # 对话框一路到底、零确认，且新建条目直接 enabled=True —— 填个进程名
            # 点保存就是一上架即生效的自动关机闸（默认动作正是 shutdown）。
            if act_v.get() in ('shutdown', 'reboot'):
                _shut = act_v.get() == 'shutdown'
                _zh = i18n.tr('关机' if _shut else '重启',
                              'shut down' if _shut else 'reboot')
                if not messagebox.askyesno(
                        i18n.tr('确认破坏性动作', 'Confirm destructive action'),
                        i18n.tr('条件命中后会在 30 秒后%s本机。\n'
                                '确认保存这条监视？' % _zh,
                                'When the condition matches, this will %s the PC '
                                'after 30 seconds.\nSave this watch?' % _zh),
                        parent=gui.root):
                    return
            if orig is not None:
                # ★R213（NEW5-D6）：update 返回值此前被丢——add 侧 C11
                # 修过同款，update 依旧"编辑失败也报已编辑"，与 _reload
                # 后列表实际内容自相矛盾
                if gui.monitor.update(orig.get('name'), name=new_name,
                                      process=proc, condition=cond_v.get(),
                                      action=act_v.get(), command=cmd_v.get()):
                    gui._log(f'👀 已编辑条件监视 [{new_name}]')
                else:
                    gui._log(f'⚠ 条件监视 [{new_name}] 编辑未保存'
                             '（禁写闸或磁盘故障），详见日志')
            else:
                # ★R221（NEW7-P1-4）：新建**默认停用**（enabled=False）。R175
                # 的注释定的口径就是"示例默认停用，点了 ▶ 才跑"，但 add 的
                # 形参默认是 True，等于用户手建的每一条都直接上岗。
                if gui.monitor.add(new_name, proc, cond_v.get(),
                                   act_v.get(), cmd_v.get(), enabled=False):
                    gui._log(f'👀 已添加条件监视 [{new_name}]'
                             '（默认停止 · 点 ▶ 启用后才开始监视）')
                else:
                    gui._log(f'⚠ 条件监视 [{new_name}] 添加失败'
                             '（重名或进程名为空），详见日志')
            top.destroy()
            _reload_mon()
            # ★ R176：进程当前不在=多半 exe 名写错，立刻提醒
            if cond_v.get() == 'gone':
                _warn_if_absent(proc)

        # ★R222 P4-4：原来只有一枚"保存"，模态框唯一的出路是关窗右上角 ✕
        # （Windows 标题栏），既不合主窗"取消/确认成对"的习惯，也让用户以为
        # 必须落库才能退出。取消＝只 destroy，不碰 monitor（含不触发二次确认）。
        ttk.Button(top, text=i18n.tr('保存', 'Save'), style='primary.TButton',
                   command=_save).pack(side=tk.RIGHT, padx=(0, 12), pady=(8, 10))
        ttk.Button(top, text=i18n.tr('取消', 'Cancel'),
                   command=top.destroy).pack(side=tk.RIGHT, pady=(8, 10))
        top.protocol('WM_DELETE_WINDOW', top.destroy)
        top.bind('<Escape>', lambda e: top.destroy())

    def _add_mon():
        _mon_dialog()

    def _selected_mon():
        """选中行对应的监视条目（无选中提示并返回 None）"""
        sel = mon_tree.selection()
        if not sel:
            messagebox.showinfo(i18n.tr('提示', 'Notice'),
                                i18n.tr('先在列表选中一条监视',
                                        'Select a watch first'),
                                parent=gui.root)
            return None
        name = mon_tree.set(sel[0], 'name')
        return next((m for m in gui.monitor.list()
                     if m.get('name') == name), None)

    def _edit_mon():
        # ★ R175：编辑选中条目（回填对话框）
        m = _selected_mon()
        if m:
            _mon_dialog(orig=m)

    def _toggle_mon():
        # ★ R175：启用⇄停止（示例默认停用，点了才跑）
        m = _selected_mon()
        if not m:
            return
        new = not m.get('enabled', True)
        gui.monitor.set_enabled(m.get('name'), new)
        _reload_mon()
        gui._log(f'👀 [{m.get("name")}] ' +
                 (i18n.tr('已启用 ▶', 'enabled') if new
                  else i18n.tr('已停止 ⏸', 'stopped')))

    def _del_mon():
        sel = mon_tree.selection()
        if not sel:
            return
        name = mon_tree.set(sel[0], 'name')
        gui.monitor.remove(name)
        _reload_mon()
        gui._log(f'👀 已删除条件监视 [{name}]')

    btn_row = ttk.Frame(mon_frame)
    btn_row.pack(fill=tk.X, pady=(SP['s'], 0))
    # ★R222 P4-6：❓ 原来单独 pack 在表格上方一行（anchor=E），
    # 等于为一枚 3 字符宽的小钮留了整行高度＝标题与表格之间一条大空白。
    # 挪到按钮排右端：先占右侧槽位，再让 action_bar 吃满剩余宽度。
    ttk.Button(btn_row, text='❓', width=3,
               command=_help_mon).pack(side=tk.RIGHT, padx=(SP['s'], 0))
    # ★R216 阶段 2b：四枚按钮走 action_bar 等宽（原先 pack(LEFT, padx=6) ⇒
    # 长短不齐，和上方表格列边也对不上）
    ui_layout.action_bar(btn_row, [
        (i18n.tr('➕ 添加监视', '➕ Add Watch'), _add_mon, 'primary.TButton'),
        (i18n.tr('✏️ 编辑', '✏️ Edit'), _edit_mon, 'secondary.TButton'),
        (i18n.tr('▶ 启用 / ⏸ 停止', '▶ Enable / ⏸ Stop'), _toggle_mon,
         'secondary.TButton'),
        (i18n.tr('🗑 删除选中', '🗑 Delete Selected'), _del_mon,
         'error.TButton')],
        cols=4, pad=SP['s'])
    ttk.Label(mon_frame,
              text=i18n.tr('每 3 秒轮询一次进程；关机/重启动作有 30 秒缓冲'
                           '（shutdown /a 可取消）。开机自动开虚拟机：用下方'
                           '创建器选"登录 Windows 时"。',
                           'Polls every 3s; shutdown/reboot has a 30s buffer.'),
              foreground=colors['text_secondary'], justify=tk.LEFT,
              font=ui_layout.app_font(9), wraplength=760).pack(
        anchor=tk.W, pady=(SP['xs'], 0))
    _reload_mon()

    # ---- ⚡ 一键维护（★ R168 参照 FluentTaskScheduler Quick Actions；
    # 用户 -16/3b"抄实用功能"。立即执行不建任务）----
    qa_frame = ttk.LabelFrame(tab_sys,
                              text=i18n.tr('⚡ 一键维护（立即执行）',
                                           '⚡ Quick Maintenance'),
                              padding='10', style='Card.TLabelframe')
    qa_frame.pack(fill=tk.X, pady=(0, 6))
    qa_grid = ttk.Frame(qa_frame)
    qa_grid.pack(fill=tk.X)
    for _i in range(4):
        qa_grid.columnconfigure(_i, weight=1, uniform='qa')
    def _empty_recycle_bin():
        """★ R174 修复"清空回收站点了没效果"（用户 -20/4）：原命令
        Clear-RecycleBin -Force -ErrorAction SilentlyContinue 把错误全
        吞了，失败也显示成功。改 ctypes 直调 SHEmptyRecycleBinW，
        返回码可查：0=成功；0x8000FFFF=回收站本就是空（也算成功）。
        ★ R175：弹窗报数（清了几项/本来就是空的），用户一眼看到效果；
        回收站文件夹视图可能不自动刷新，重新打开即见。"""
        import ctypes

        def _count():
            ps = ("$s=New-Object -ComObject Shell.Application;"
                  "$b=$s.NameSpace(0xA);@($b.Items()).Count")
            p = subprocess.run(['powershell', '-NoProfile', '-Command', ps],
                               capture_output=True, timeout=30,
                               creationflags=0x08000000)
            return int(p.stdout.decode('gbk', 'replace').strip() or '0')

        n = _count()
        hr = ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 0x0007)
        if hr not in (0, -2147418113):
            raise OSError(f'SHEmptyRecycleBinW hr=0x{hr & 0xFFFFFFFF:08X}')
        if n == 0:
            return i18n.tr('回收站本来就是空的', 'Recycle Bin was already empty')
        return i18n.tr(f'已清空 {n} 项（回收站窗口若没刷新，重新打开即见）',
                       f'Emptied {n} item(s)')

    def _clear_clipboard():
        # ★R190（Qoder GUIA-2）：一键维护在后台线程跑，Tk clipboard
        # 只能主线程碰——回投主线程执行
        def _do():
            gui.root.clipboard_clear()
            gui.root.clipboard_append('')
        try:
            # ★R207（Qoder GUIB-4）：后台线程回投改 _ui_queue
            gui._ui_queue.put(_do)
        except Exception:
            _do()

    def _clear_recent_files():
        """★ R175 第 8 键（用户"加一个凑成 8 个"）：清空 Recent
        "最近使用"记录（隐私清理，与下载记录主题配套）"""
        import glob as _g
        rp = os.path.join(os.environ.get('APPDATA', ''),
                          'Microsoft', 'Windows', 'Recent')
        n = 0
        for f in _g.glob(os.path.join(rp, '*')):
            try:
                if os.path.isfile(f):
                    os.remove(f)
                    n += 1
            except Exception:
                pass
        return i18n.tr(f'已清空 {n} 条最近使用记录',
                       f'cleared {n} recent entries')

    # ★R222 U3：8 枚"一键维护"按钮文案（含确认框里的 qnote）此前是裸中文，
    # English 模式下整块 16 个字全是中文。emoji 前缀保持原样（只翻文字部分）。
    # PS 载荷里的 "已清理 N 项" 是命令**回显文本**，同样按语言拼。
    _tmp_done = i18n.tr('已清理 ', 'Cleaned ')
    _tmp_tail = i18n.tr(' 项（占用/系统锁定文件自动跳过）',
                        ' items (in-use/locked files skipped)')
    QA = [
        ('🧹 ' + i18n.tr('清理临时文件', 'Clean Temp Files'),
         'powershell -NoProfile -Command "$b=(Get-ChildItem $env:TEMP '
         '-Recurse -ErrorAction SilentlyContinue | Measure-Object).Count; '
         'Get-ChildItem $env:TEMP -Recurse -ErrorAction SilentlyContinue | '
         'Remove-Item -Recurse -Force -ErrorAction SilentlyContinue; '
         '$a=(Get-ChildItem $env:TEMP -Recurse -ErrorAction '
         'SilentlyContinue | Measure-Object).Count; '
         f"'{_tmp_done}' + ($b-$a) + '{_tmp_tail}'\"",
         '', 'wait'),
        ('🌐 ' + i18n.tr('刷新 DNS 缓存', 'Flush DNS Cache'),
         'ipconfig /flushdns', '', 'wait'),
        ('🔁 ' + i18n.tr('重启资源管理器', 'Restart Explorer'),
         'taskkill /f /im explorer.exe & start explorer.exe',
         i18n.tr('桌面会闪一下', 'The desktop will flash briefly'), 'wait'),
        ('🗑 ' + i18n.tr('清空回收站', 'Empty Recycle Bin'), _empty_recycle_bin,
         i18n.tr('会永久删除回收站内文件',
                 'Permanently deletes everything in the Recycle Bin'), 'py'),
        ('🖼 ' + i18n.tr('刷新图标缓存', 'Refresh Icon Cache'),
         'ie4uinit.exe -show',
         i18n.tr('桌面图标显示异常（白板/错位）时用',
                 'Use when desktop icons look blank or misplaced'), 'wait'),
        ('📋 ' + i18n.tr('清空剪贴板', 'Clear Clipboard'), _clear_clipboard,
         '', 'py'),
        ('🧾 ' + i18n.tr('清空最近使用记录', 'Clear Recent Files'),
         _clear_recent_files,
         i18n.tr('清空后"最近使用"列表为空',
                 'The "Recent" list will be empty afterwards'), 'py'),
        ('🧽 ' + i18n.tr('磁盘清理（C 盘）', 'Disk Cleanup (C:)'),
         'cleanmgr.exe /d C:',
         i18n.tr('打开系统自带磁盘清理窗口',
                 'Opens the built-in Disk Cleanup dialog'), 'spawn'),
    ]
    for _i, (qname, qcmd, qnote, qmode) in enumerate(QA):
        qa_btn = ttk.Button(qa_grid, text=qname, width=16,
                            style='secondary.TButton')

        def _mk(nm, cm, bt, nt, md):
            def _go():
                if nt and not messagebox.askyesno(
                        i18n.tr('确认', 'Confirm'), nm + '\n' + nt,
                        parent=gui.root):
                    # ★ R176：取消也留痕——用户"点了静默"时日志能区分
                    gui._log(f'⚡ {nm}：已在确认框取消')
                    return
                _qa_run(gui, nm, cm, bt, md)
            return _go
        qa_btn.configure(command=_mk(qname, qcmd, qa_btn, qnote, qmode))
        # 4 列两行（R175 凑成 8 键）
        qa_btn.grid(row=_i // 4, column=_i % 4, padx=3, pady=2,
                    sticky='nsew')
    ttk.Label(qa_frame,
              text=i18n.tr('立即执行（不建计划任务）；"重启资源管理器"桌面会闪一下；'
                           '临时文件/回收站清理会永久删除对应内容。',
                           'Runs immediately (no scheduled task). Explorer '
                           'restart flashes the desktop; temp/recycle cleanup '
                           'permanently deletes.'),
              foreground=colors['text_secondary'], justify=tk.LEFT,
              font=ui_layout.app_font(9), wraplength=760).pack(
        anchor=tk.W, pady=(4, 0))

    # ---- 📜 事件查看器（大白话，只读） ----
    evt_frame = ttk.LabelFrame(tab_sys,
                               text=i18n.tr('📜 事件查看器（系统大白话）',
                                            '📜 Event Viewer (plain language)'),
                               padding='10', style='Card.TLabelframe')
    evt_frame.pack(fill=tk.X, pady=(0, 6))

    def _help_evt():
        messagebox.showinfo(
            i18n.tr('📜 事件查看器说明', '📜 Event Viewer Help'),
            i18n.tr('把 Windows 系统日志翻译成大白话。\n'
                    '· 点顶部按钮按类别筛选（全部/开关机/异常崩溃/服务/磁盘）。\n'
                    '· 点"🌐 原生查看器"= 按当前筛选在 Windows 事件查看器\n'
                    '  打开（左侧 自定义视图 → Views → DeskBits…），\n'
                    '  方便用原生界面继续翻这些日志。\n'
                    '· 错误级红色、警告级黄色、其余白色'
                    '（与 Windows 原生红X/黄!图标一致）。\n'
                    '· 双击某条 → 看详情+同类事件+生成过滤脚本。\n'
                    '只读不改，取自 System 日志。',
                    'Translates Windows System log to plain language.\n'
                    '· Top buttons filter by category.\n'
                    '· "🌐 Native Viewer" opens Event Viewer with the\n'
                    '  current filter as a Custom View.\n'
                    '· Errors shown in red.\n'
                    '· Double-click for detail + script.\n'
                    'Read-only, from System log.'),
            parent=gui.root)
    ttk.Button(evt_frame, text='❓', width=3,
               command=_help_evt).pack(anchor=tk.E)
    evt_row = ttk.Frame(evt_frame)
    evt_row.pack(fill=tk.X)
    # ★ R165 筛选行 chrome 化（参照 EventLogExpert）：分类勾选开关，
    # 全不勾=显示全部；勾选变化即刷新
    gui._evt_filter = {'boot': tk.BooleanVar(value=True),
                       'bad': tk.BooleanVar(value=True),
                       'other': tk.BooleanVar(value=True)}
    gui._evt_id_only = None   # ★ R166：典型按钮可进一步限定事件 ID 组
    gui._evt_preset_name = ''   # ★ R186c：当前典型名（"磁盘"等，视图标题用）
    # ★ R165/R166：典型筛选按钮（放不下的进"更多"下拉，用户 -16/2）
    def _preset(cats, ids=None, name=''):
        gui._evt_id_only = ids
        gui._evt_preset_name = name
        for k, v in gui._evt_filter.items():
            v.set(k in cats)
        gui.root.after(300, lambda: _load_events(gui, evt_tree))
    for txt, cats, ids in (
            (i18n.tr('全部', 'All'), ('boot', 'bad', 'other'), None),
            (i18n.tr('开关机', 'Boot'), ('boot',), None),
            (i18n.tr('异常/崩溃', 'Crash'), ('bad',), None),
            (i18n.tr('服务故障', 'Services'),
             ('bad',), {7031, 7034, 7026, 7040, 7011, 7022, 7023, 7024}),
            (i18n.tr('磁盘', 'Disk'), ('bad',),
             {7, 51, 55, 129, 153, 157})):
        ttk.Button(evt_row, text=txt, width=8,
                   style='secondary.TButton',
                   command=lambda c=cats, i=ids, t=txt: _preset(c, i, t)).pack(
            side=tk.LEFT, padx=(4, 0))
    ttk.Button(evt_row, text=i18n.tr('🔄 刷新', '🔄 Refresh'),
               command=lambda: _load_events(gui, evt_tree)).pack(
        side=tk.LEFT, padx=(6, 0))
    # ★ R186（用户 01 批）：按当前筛选在 Windows 事件查看器打开（自定义视图）
    ttk.Button(evt_row, text=i18n.tr('🌐 原生查看器', '🌐 Native Viewer'),
               command=lambda: _open_evt_filter_in_viewer(gui),
               style='secondary.TButton').pack(
        side=tk.LEFT, padx=(4, 0))
    ttk.Label(evt_row, text=i18n.tr('更多:', 'More:'),
              foreground=colors['text_secondary']).pack(side=tk.LEFT, padx=(10, 2))
    more_var = tk.StringVar(value='')
    more_combo = ttk.Combobox(evt_row, textvariable=more_var, width=8,
                              state='readonly',
                              values=[
        i18n.tr('域名解析', 'DNS'),
        i18n.tr('驱动', 'Driver'),
        i18n.tr('时间同步', 'Time Sync'),
        i18n.tr('Windows 更新', 'Windows Update'),
        i18n.tr('网络', 'Network'),
    ],   # ★R204 GUIA-12：删"打印"——R188d 已删对应 ID，选中静默无反应
                              postcommand=None)
    more_combo.pack(side=tk.LEFT)

    def _more(_e=None):
        _m = more_var.get()
        _id_sets = {
            # ★ R188d（用户 07 批"力求准确"）：只保留可核实来源家族的
            # ID——4616 是安全日志的（System 不出现）、44/4208/打印三件
            # 来源核实不了，全部移除，宁少勿错
            i18n.tr('域名解析', 'DNS'): {1014, 1015},
            i18n.tr('驱动', 'Driver'): {219, 4101},
            i18n.tr('时间同步', 'Time Sync'): {1},
            i18n.tr('Windows 更新', 'Windows Update'): {19, 20, 43},
            i18n.tr('网络', 'Network'): {4201, 4202},
        }
        ids = _id_sets.get(_m)
        if ids:
            _preset(('bad',), ids, _m)
    more_combo.bind('<<ComboboxSelected>>', _more)
    ttk.Label(evt_row, text=i18n.tr('只读不改 · 双击行看详情',
                                    'Read-only · double-click for detail'),
              foreground=colors['text_secondary'],
              font=ui_layout.app_font(9)).pack(side=tk.LEFT, padx=(10, 0))
    cols = ('time', 'what', 'src', 'id')
    evt_tree = ttk.Treeview(evt_frame, columns=cols, show='headings', height=6)
    for c, w, txt in (('time', 110, '时间'), ('what', 260, '发生了什么'),
                      ('src', 150, '来源'), ('id', 60, 'ID')):
        evt_tree.heading(c, text=txt)
        evt_tree.column(c, width=w, anchor=tk.W)
    # ★ R164：异常/崩溃行红色着色（参照 EventLogExpert 语义色）
    # ★ R187b：着色=Windows 原生语义：错误级（红 X）红、警告级（黄 !）黄、
    # 其余默认白
    # ★R223：黄/红改走色板 token——写死的 #ffd866 淡黄在浅色/护眼主题下
    # 对比度≈1.6:1，用户截图实证"看不清"；换肤时由 refresh_semantic_tags 重刷
    tag_semantic(evt_tree, 'evt')
    evt_scroll = ttk.Scrollbar(evt_frame, orient=tk.VERTICAL,
                               command=evt_tree.yview)
    evt_tree.configure(yscrollcommand=evt_scroll.set)
    evt_scroll.pack(side=tk.RIGHT, fill=tk.Y)
    evt_tree.pack(fill=tk.X, pady=(4, 0))
    gui._evt_status = tk.StringVar(value='')
    ttk.Label(evt_frame, textvariable=gui._evt_status,
              foreground=colors['muted'],
              font=ui_layout.app_font(9)).pack(anchor=tk.W)
    evt_frame.rowconfigure(0, weight=0)
    # ★R213（NEW5-D7）：读整段 System 事件日志是主线程同步 IO，此前挂在
    # 启动路径上（build_sys_tab 在 __init__ 期执行）拖慢首窗——A9 只把
    # 预设按钮延后，这里漏了。after 让窗口先起来再载入，不冻启动。
    gui.root.after(120, lambda: _load_events(gui, evt_tree))
    # ★ R161：双击某条 → 打开 Windows 事件查看器（尽力带过滤自定义视图）
    evt_tree.bind('<Double-1>',
                  lambda e: _show_event_detail(gui, evt_tree, colors))

    # ---- 📅 计划任务 ----
    task_frame = ttk.LabelFrame(tab_sys,
                                text=i18n.tr('📅 计划任务（用户级）',
                                             '📅 Scheduled Tasks (user)'),
                                padding='10', style='Card.TLabelframe')
    task_frame.pack(fill=tk.X, pady=(0, 6))

    def _help_task():
        messagebox.showinfo(
            i18n.tr('📅 计划任务说明', '📅 Scheduled Tasks Help'),
            i18n.tr('管理 Windows 计划任务。\n'
                    '· 默认只显示本软件创建的任务（DeskBits_ 开头）。\n'
                    '· 取消勾选可看系统全部任务。\n'
                    '· 选中后可 运行/启停/删除。\n'
                    '· 改系统自带任务需管理员权限。',
                    'Manages Windows scheduled tasks.\n'
                    '· Shows DeskBits-created tasks by default.\n'
                    '· Uncheck to see all system tasks.\n'
                    '· Select then Run/Enable/Disable/Delete.\n'
                    '· System tasks need admin.'),
            parent=gui.root)
    ttk.Button(task_frame, text='❓', width=3,
               command=_help_task).pack(anchor=tk.E)
    task_row = ttk.Frame(task_frame)
    task_row.pack(fill=tk.X)
    # ★R250 三3d（用户 42 批「有的时候在放大窗口的 win11 上面，那个删除按钮
    # 看不见」）：原先六枚操作按钮 + 一枚复选**全排 side=LEFT**，删除排在最后
    # ⇒ 窗口一窄（高缩放＝同样的文字要占更多像素）就被前面的兄弟吃光宽度、
    # 当场裁没（★R222 记过的 pack 老账：后到的兄弟会被先到的挤扁）。
    # 改法：**删除先 pack(side=RIGHT) 占位**，它永远在右上角可见；其余操作装进
    # 左侧子框，真放不下时宁可让"只显示本软件创建的任务"那行灰字变短，也不再
    # 把唯一的破坏性按钮藏起来。
    ttk.Button(task_row, text=i18n.tr('🗑 删除', '🗑 Delete'),
               command=lambda: _task_delete(gui, task_tree)).pack(
        side=tk.RIGHT)
    task_ops = ttk.Frame(task_row)
    task_ops.pack(side=tk.LEFT, fill=tk.X, expand=True)
    # ★R277：这两处是**用户主动**要刷新 ⇒ 允许顺手迁移存量裸 bat 任务
    # （页面首次构建那次走默认 False，自动测试真建主窗时一个字都不写系统任务）
    ttk.Button(task_ops, text=i18n.tr('🔄 刷新', '🔄 Refresh'),
               command=lambda: _load_tasks(gui, task_tree,
                                           migrate=True)).pack(side=tk.LEFT)
    ttk.Button(task_ops, text=i18n.tr('▶ 运行', '▶ Run'),
               command=lambda: _task_op(gui, task_tree, 'run',
                                        i18n.tr('运行', 'Run'))).pack(
        side=tk.LEFT, padx=(6, 0))
    # ★ R176：自带编辑器（改开始时间/程序命令；周期不可原地改）
    ttk.Button(task_ops, text=i18n.tr('✏️ 编辑', '✏️ Edit'),
               command=lambda: _task_edit(gui, task_tree, colors)).pack(
        side=tk.LEFT, padx=(6, 0))
    ttk.Button(task_ops, text=i18n.tr('⏸ 禁用', '⏸ Disable'),
               command=lambda: _task_op(gui, task_tree, 'disable',
                                        i18n.tr('禁用', 'Disable'))).pack(
        side=tk.LEFT, padx=(6, 0))
    ttk.Button(task_ops, text=i18n.tr('✔ 启用', '✔ Enable'),
               command=lambda: _task_op(gui, task_tree, 'enable',
                                        i18n.tr('启用', 'Enable'))).pack(
        side=tk.LEFT, padx=(6, 0))
    # ★ R170：复选语义改"只显示本软件创建的任务"（默认勾选=只看 DeskBits）
    gui._tasks_desbits_only = tk.BooleanVar(value=True)
    ttk.Checkbutton(task_ops,
                    text=i18n.tr('只显示本软件创建的任务', 'Only DeskBits tasks'),
                    variable=gui._tasks_desbits_only,
                    command=lambda: _load_tasks(gui, task_tree,
                                                migrate=True)).pack(
        side=tk.LEFT, padx=(12, 0))
    # ★R247（item2）：多一列"电池下不启动"——用户要的"把条件显示出来"
    tcols = ('name', 'action', 'memo', 'created', 'next', 'status', 'last',
             'bat')
    # ★R250 三3a-3：表格装进一个小容器，两条滚动条**先 pack 占位**再放树
    # （★R222 老账：同一排里后到的兄弟会被先到的吃光宽度那枚挤掉）。
    # 横向条常驻：八列总宽 > 可视宽时它是唯一能把"上次结果/电池下不启动"
    # 拉到眼前的入口。
    task_view = ttk.Frame(task_frame)
    task_view.pack(fill=tk.X, pady=(4, 0))
    task_tree = ttk.Treeview(task_view, columns=tcols, show='headings',
                             height=6,
                             # ★R256（48 批三9）：默认 browse 只能单选 ⇒
                             # 用户"只能一个一个删"。改 extended：Ctrl/Shift
                             # 多选后 🗑 删除一次删一批（其余操作按钮对
                             # 多选取第一条，单选路径完全不变）。
                             selectmode='extended')
    # ★ R164：状态着色（运行中绿/已禁用灰/就绪默认）
    # ★R207（06批-1）：上次结果出错=橙（区别于关机/重启红）
    # ★R223：同上，四色全走色板 token + 换肤重刷
    tag_semantic(task_tree, 'task')
    # ★ R174：新增"动作/备注/创建时间"列（-20/3：备注注明作用方便了解，
    # 创建时间方便排序），默认按创建时间新→旧
    task_heads = (('name', 230, '任务名'), ('action', 65, '动作'),
                  ('memo', 145, '备注'), ('created', 95, '创建时间'),
                  ('next', 110, '下次运行'), ('status', 65, '状态'),
                  ('last', 110, '上次结果'),
                  ('bat', 110, '电池下不启动'))
    # ★ R175（-20/5-6）：点列头排序——首次从低到高（升序），再点切换；
    # 当前列标题带 ▲/▼
    _sort_st = {'col': None, 'asc': True}

    def _sort_tasks(col):
        desc = (not _sort_st['asc']) if _sort_st['col'] == col else False
        _sort_st['col'], _sort_st['asc'] = col, not desc
        items = [(str(task_tree.set(i, col)), i)
                 for i in task_tree.get_children()]
        items.sort(key=lambda t: t[0].lower(), reverse=desc)
        for _v, i in items:
            task_tree.move(i, '', tk.END)
        # ★R222 阶段 4：行序变了要重铺条纹
        ui_layout.zebra_treeview(task_tree)
        for c2, _w, t2 in task_heads:
            if c2 == col:
                task_tree.heading(c2, text=t2 + (' ▼' if desc else ' ▲'))
            else:
                task_tree.heading(c2, text=t2)
    for c, w, txt in task_heads:
        task_tree.heading(c, text=txt,
                          command=lambda c=c: _sort_tasks(c))
        # ★R241：除备注外一律不参与拉伸——不然窗口拉宽时多余宽度七列平分，
        # 备注照样截断，用户要的"显示全点"永远等不到
        task_tree.column(c, width=w, anchor=tk.W, stretch=False)
    # ★R250 三3a（用户 42 批「我想把备注这个列缩小，不让缩小，我想看看后面的
    # 列，老是显示不全」）：★R241 把备注设成**唯一**吃多余宽度的列，副作用是
    # 它压根拖不动——拖动省出来的宽度当场又被它自己吸收回去（tile 的 stretch
    # 就是这个语义），而八列总宽超过可视宽时右侧几列又没处滚 ⇒ 一句反馈里两个
    # 症状同时命中。改法三条，★R241 那半诉求一条不删：
    #   1) 备注 `stretch=False` + `minwidth` 放到 60 ⇒ 分隔线拖得动、拖完定住；
    #   2) "默认显示全点 / 适应窗口"改由下面的 `<Configure>` **自动铺宽**完成，
    #      一旦用户在分隔线上抬手就记成手动档、从此不再自动铺（否则又打回
    #      "不让缩小"）；
    #   3) 后面的列看不全 ⇒ 补**横向滚动条**（原先只有纵向条，超宽只能干瞪眼）。
    task_tree.column('memo', width=170, minwidth=60, stretch=False,
                     anchor=tk.W)
    _memo_fit = {'manual': False, 'base': 170, 'max': 520}

    def _memo_auto_fit(_ev=None):
        """未被手动拖过：把窗口富余宽度铺给备注列（★R241「显示全点」的接手人）

        留 4px 死区：Treeview 的**请求宽度＝各列宽之和**，铺得太贴边会反过来
        把窗口顶大 ⇒ 差值小于 4px 就当地面已经平了，别再动（也免得
        Configure ⇄ column 配置来回抖）。"""
        if _memo_fit['manual']:
            return
        try:
            cur = int(task_tree.column('memo', 'width'))
            shown = sum(int(task_tree.column(c, 'width')) for c in tcols)
            avail = int(getattr(_ev, 'width', 0) or task_tree.winfo_width())
            if avail <= 1 or shown <= 0:
                return
            slack = avail - shown
            if -4 <= slack <= 4:
                return
            want = min(_memo_fit['max'],
                       max(_memo_fit['base'], cur + slack - 4))
            if want != cur:
                task_tree.column('memo', width=want)
        except Exception as e:
            logger.debug(f'备注列自适应失败: {e}')

    def _memo_manual(_ev=None):
        """落在**列分隔线**上的抬手 = 用户自己在调宽 ⇒ 从此尊重他的值
        （点列头排序不算：那种抬手不在 separator 上）"""
        try:
            if task_tree.identify_region(_ev.x, _ev.y) != 'separator':
                return
        except Exception:
            return
        if not _memo_fit['manual']:
            _memo_fit['manual'] = True
            gui._log('📅 计划任务列宽已按手动调整，备注列不再自动铺满窗口')

    task_tree.bind('<Configure>', _memo_auto_fit)
    task_tree.bind('<ButtonRelease-1>', _memo_manual)
    # 被省略的备注：鼠标悬停看全文（不靠加列宽解决）
    _bind_memo_tip(task_tree, '#3')
    task_xscroll = ttk.Scrollbar(task_view, orient=tk.HORIZONTAL,
                                 command=task_tree.xview)
    task_xscroll.pack(side=tk.BOTTOM, fill=tk.X)
    task_scroll = ttk.Scrollbar(task_view, orient=tk.VERTICAL,
                                command=task_tree.yview)
    task_scroll.pack(side=tk.RIGHT, fill=tk.Y)
    task_tree.configure(yscrollcommand=task_scroll.set,
                        xscrollcommand=task_xscroll.set)
    task_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    ttk.Label(task_frame,
              text=i18n.tr('勾选=只显示本软件创建的任务（DeskBits_ 开头）；'
                           '取消勾选可看系统全部任务。改系统自带任务需要管理员权限。',
                           'Lists tasks created by this app by default; system '
                           'tasks need admin to change.'),
              foreground=colors['text_secondary'],
              font=ui_layout.app_font(9)).pack(anchor=tk.W, pady=(4, 0))
    _load_tasks(gui, task_tree)

    # ---- 🧩 懒人模板库（★ R165 参照 FluentTaskScheduler：点击=预填
    # 下方创建器；每个模板带风险/权限 Note）----
    tpl_frame = ttk.LabelFrame(tab_sys,
                               text=i18n.tr('🧩 懒人模板（点击填入下方创建器）',
                                            '🧩 Templates (click to prefill)'),
                               padding='10', style='Card.TLabelframe')
    tpl_frame.pack(fill=tk.X, pady=(0, 6))
    tpl_grid = ttk.Frame(tpl_frame)
    tpl_grid.pack(fill=tk.X)
    for _i in range(3):
        tpl_grid.columnconfigure(_i, weight=1, uniform='tpl')

    TEMPLATES = [
        ('🌙 定时关机', '每天指定时间自动关机（创建前想好时间）',
         dict(trig='daily', tm='23:30', act='shutdown'),
         '电脑会在设定时间关机，延迟 10 秒可取消'),
        ('🧹 每周清理临时文件', '每周日凌晨自动清理系统临时目录',
         dict(trig='days', n='7', act='cmd',
              pvar='powershell -NoProfile -Command "Get-ChildItem $env:TEMP -Recurse -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue"'),
         '会永久删除临时目录内文件'),
        ('🌐 每天清 DNS 缓存', '网页打不开/解析异常时有用',
         dict(trig='days', n='1', act='cmd',
              pvar='ipconfig /flushdns'),
         '无风险'),
        ('🗑 每周清空回收站', '每周自动清空回收站',
         dict(trig='days', n='7', act='cmd',
              pvar='powershell -NoProfile -Command "Clear-RecycleBin -Force -ErrorAction SilentlyContinue"'),
         '会永久删除回收站内文件'),
        ('▶ 登录时打开软件', '登录 Windows 后自动运行指定软件',
         dict(trig='logon', act='open'),
         '创建前先在右侧选好软件路径'),
        ('🔁 定时重启', '每天指定时间自动重启',
         dict(trig='daily', tm='04:00', act='reboot'),
         '重启会关闭所有程序，注意保存'),
        ('⏲ 2 小时后关机', '★R185 新触发：指定多长时间后（一次性）自动关机',
         # ★R228（用户 22 批 item10-2）：原来填 '0:2:0:0'——解析器认（=7200 秒，
         # 没算错），但它**不符合自己写在说明里的格式** 00:00:00:00，
         # 用户一眼就看到两套写法打架。改成补零四段。
         dict(trig='delay', dl='00:02:00:00', act='shutdown'),
         '从创建时刻算 2 小时后关机，临关机想取消可在命令行 shutdown /a'),
    ]

    def _use_tpl(idx):
        name, desc, pre, note = TEMPLATES[idx]
        trig.set(pre.get('trig', 'daily'))
        if 'n' in pre:
            nvar.set(pre['n'])
        if 'tm' in pre:
            tmvar.set(pre['tm'])
        if 'dl' in pre:
            # ★R228（item10-2）：模板值过一遍归一函数——以后再挂模板写成
            # '2:0'、'0：2：0：0' 这种野格式，落到输入框前也会变成规范四段
            dlvar.set(_normalize_delay_str(pre['dl']) or pre['dl'])
        act.set(pre.get('act', 'open'))
        pvar.set(pre.get('pvar', ''))
        # ★ R170：显式同步显隐——trig.set() 不触发 Radiobutton command，
        # 此前模板预填后 N/时间字段显隐错乱（用户 -20/3 实测）
        try:
            _sync_trig()
        except Exception:
            pass
        # ★ R185：动作区同理（vm 列表/路径栏切换）
        try:
            _sync_act()
        except Exception:
            pass
        messagebox.showinfo(
            i18n.tr('已填入创建器', 'Prefilled'),
            i18n.tr('模板[' + name + ']已填入下方创建器。\n说明：' + desc +
                    '\n⚠ ' + note + '\n确认无误后点"➕ 创建"。',
                    'Template [' + name + '] prefilled.\n' + desc +
                    '\n⚠ ' + note),
            parent=gui.root)
        gui._log(f'🧩 模板已填入创建器: {name}')

    for _i, (name, desc, pre, note) in enumerate(TEMPLATES):
        ttk.Button(tpl_grid, text=name, width=16,
                   command=lambda i=_i: _use_tpl(i)).grid(
            row=_i // 3, column=_i % 3, padx=3, pady=3, sticky='nsew')

    # ---- ➕ 创建计划任务（左=触发，右=动作；用户 -14/7 定案布局）----
    cr_frame = ttk.LabelFrame(tab_sys,
                              text=i18n.tr('➕ 创建计划任务',
                                           '➕ Create Scheduled Task'),
                              padding='10', style='Card.TLabelframe')

    # ★R220：❓ 从 cr_frame 顶部单独一行挪到下方操作行（与 ➕ 创建 同排）。
    def _help_create():
        messagebox.showinfo(
            i18n.tr('➕ 创建器说明', '➕ Creator Help'),
            i18n.tr('左边选"什么时候做"，右边选"做什么"，点创建即可。\n'
                    '· "指定多长时间后"=一次性延时任务，时长填 00:00:00:00（天:时:分:秒）'
                    '（如 00:02:30:00=两个半小时后）。\n'
                    '· "打开虚拟机"：先点扫描，列表里选一台或多台'
                    '（Ctrl 多选）；支持 VMware/VirtualBox。\n'
                    '· 也可以从上方"🧩 懒人模板"一键填入。\n'
                    '· 关机/重启类任务会弹二次确认。\n'
                    '· 创建后可在上方列表管理（运行/启停/删除）。',
                    'Left: when (trigger). Right: what (action). Click Create.\n'
                    '· "After a duration" = one-shot task; enter d:h:m:s (00:00:00:00).\n'
                    '· "Start VM(s)": scan, then pick one or more; '
                    'VMware/VirtualBox supported.\n'
                    '· Or use a template above to prefill.\n'
                    '· Shutdown/reboot tasks ask for confirmation.\n'
                    '· Manage created tasks in the list above.'),
            parent=gui.root)
    cr_frame.pack(fill=tk.X, pady=(0, 6))
    grid = ttk.Frame(cr_frame)
    grid.pack(fill=tk.X)
    grid.columnconfigure(0, weight=1, uniform='task')
    grid.columnconfigure(1, weight=1, uniform='task')

    # 左：什么时候做（触发）
    trigf = ttk.LabelFrame(grid, text=i18n.tr('什么时候做（触发）',
                                              'When (trigger)'), padding='8')
    trigf.grid(row=0, column=0, sticky='nsew', padx=(0, 6))
    trig = tk.StringVar(value='minutes')
    nvar = tk.StringVar(value='30')
    tmvar = tk.StringVar(value='09:00')
    # ★ R185（用户 19 批）：新增"指定多长时间后"（一次性延时任务），
    # ★R209c（12 批定案）：固定格式 00:00:00:00，超上限自动跳回
    dlvar = tk.StringVar(value='00:01:00:00')
    TRIGS = [('minutes', i18n.tr('每隔 N 分钟', 'Every N minutes')),
             ('hours', i18n.tr('每隔 N 小时', 'Every N hours')),
             ('days', i18n.tr('每隔 N 天', 'Every N days')),
             ('daily', i18n.tr('每天指定时间', 'Daily at time')),
             ('delay', i18n.tr('指定多长时间后（一次性）',
                               'After a duration (once)')),
             ('logon', i18n.tr('登录 Windows 时', 'At Windows logon')),
             ('boot', i18n.tr('开机时（需管理员）', 'At startup (admin)'))]
    for v, txt in TRIGS:
        ttk.Radiobutton(trigf, text=txt, value=v, variable=trig).pack(
            anchor=tk.W)
    # ★ R166 动态字段：N 只在 分钟/小时/天 显示；时间只在"每天"显示；
    # 时长只在"多长时间后"显示
    nrow = ttk.Frame(trigf)
    nrow.pack(anchor=tk.W, pady=(4, 0))
    ttk.Label(nrow, text='N =').pack(side=tk.LEFT)
    nspin = ttk.Spinbox(nrow, from_=1, to=999, width=5,
                        textvariable=nvar)
    nspin.pack(side=tk.LEFT, padx=4)
    tmrow = ttk.Frame(trigf)
    tmrow.pack(anchor=tk.W, pady=(4, 0))
    ttk.Label(tmrow, text=i18n.tr('时间(HH:MM:SS) =',
                                  'Time (HH:MM:SS) =')).pack(
        side=tk.LEFT, padx=(0, 2))
    ttk.Entry(tmrow, textvariable=tmvar, width=9).pack(side=tk.LEFT)
    dlrow = ttk.Frame(trigf)
    ttk.Label(dlrow, text=i18n.tr('时长(天:时:分:秒) =',
                                  'Duration (d:h:m:s) =')).pack(
        side=tk.LEFT, padx=(0, 2))
    dlentry = ttk.Entry(dlrow, textvariable=dlvar, width=12)
    dlentry.pack(side=tk.LEFT)

    # ★R241（用户 item1b「是不是应该加个总的延时自定义的设置……或者你出个
    # 更好的方案」）：只对"登录 Windows 时 / 开机时"两种触发给一行
    # **延后启动**。延时交给任务计划程序自己的 Trigger.Delay（本机取证：
    # $t.Delay 赋 PT1M30S 后读回就是 PT1M30S），不塞进 bat——bat 里的
    # `timeout /t` 在没有控制台时**静默不等待**，等于给用户一个假延时。
    delayrow = ttk.Frame(trigf)
    _sdvar = tk.StringVar(value='30')
    # 单位下拉存显示文案，解析器认中文也认英文（English 档不用反查表）
    _svunit = tk.StringVar(value=i18n.tr('秒', 'sec'))
    ttk.Label(delayrow, text=i18n.tr('延后启动 =', 'Delay =')).pack(
        side=tk.LEFT)
    ttk.Entry(delayrow, textvariable=_sdvar, width=6).pack(
        side=tk.LEFT, padx=(2, 2))
    # ★R251 item2c（用户 43 批「延时的单位加个天」）：下拉多一档「天」。
    # 存的是显示文案，解析器 `_parse_startup_delay` 中英都认（English 档是 'day'）。
    ttk.Combobox(delayrow, textvariable=_svunit, width=6, state='readonly',
                 values=[i18n.tr('秒', 'sec'), i18n.tr('分钟', 'min'),
                         i18n.tr('小时', 'hour'),
                         i18n.tr('天', 'day')]).pack(side=tk.LEFT)
    ttk.Label(delayrow,
              text=i18n.tr('（交给系统调度器；秒/分/时 ≤23 小时，天 ≤30 天）',
                           '(scheduler-native; s/min/h ≤23h, day ≤30d)'),
              foreground=colors['text_secondary'],
              font=ui_layout.app_font(9)).pack(side=tk.LEFT, padx=(4, 0))

    def _normalize_delay_entry(_e=None):
        """★R209c/R210（NEW4-A3/A6）：失焦归一——逻辑在模块级
        _normalize_delay_str（可单测），本壳只回写 var。"""
        _s = _normalize_delay_str(dlvar.get())
        if _s:
            dlvar.set(_s)

    dlentry.bind('<FocusOut>', _normalize_delay_entry)

    # ★R209c（用户 12 批）：格式 00:00:00:00，超上限自动跳回（不报错）
    # ★R228（用户 22 批 item10-1「只有点'指定多长时间后'，格式说明才出现，不合理」）：
    # 说明行本来就常驻，可它被 pack 在动态行**之前**，而 _sync_trig 每次都会
    # 重新 pack N/时间/时长行（重排到卡片的最后一格）⇒ 视觉上说明像是跟着
    # "多长时间后"才冒出来的。现在把说明钉在卡片最后一行（anchor 不变），
    # 三枚动态输入行一律 before=_dl_hint 往它上方插，说明任何触发方式都在。
    _dl_hint = ttk.Label(trigf, text=i18n.tr(
        '说明："指定多长时间后"按 00:00:00:00（天:时:分:秒）填写；'
        '超 24/60/60 自动回到上限',
        'Note: "After a duration" uses 00:00:00:00 (d:h:m:s); '
        'values over 24/60/60 clamp back'),
        foreground=colors['text_secondary'],
        font=ui_layout.app_font(9))
    _dl_hint.pack(anchor=tk.W)

    def _sync_trig(_e=None):
        v = trig.get()
        if v in ('minutes', 'hours', 'days'):
            # ★R207-3g（用户 09 批）：分钟上限 59——要更长用小时，别写
            # "90 分钟"；小时/天不封顶（用户"小时无所谓，可以长点"）
            try:
                nspin.configure(to=59 if v == 'minutes' else 999)
            except Exception:
                pass
            nrow.pack(anchor=tk.W, pady=(4, 0), before=_dl_hint)
            tmrow.pack_forget()
            dlrow.pack_forget()
        elif v == 'daily':
            nrow.pack_forget()
            tmrow.pack(anchor=tk.W, pady=(4, 0), before=_dl_hint)
            dlrow.pack_forget()
        elif v == 'delay':
            nrow.pack_forget()
            tmrow.pack_forget()
            dlrow.pack(anchor=tk.W, pady=(4, 0), before=_dl_hint)
        else:
            nrow.pack_forget()
            tmrow.pack_forget()
            dlrow.pack_forget()
        # ★R241：只有"登录/开机"才谈"延后启动"——别的触发方式自己就有时间点
        # （每隔 N 分钟 / 每天 HH:MM / 多长时间后），再加一层延时只会绕晕人。
        # 关机/重启不吃这一行（下面 _create 里也明确排除）。
        if v in ('logon', 'boot'):
            delayrow.pack(anchor=tk.W, pady=(4, 0), before=_dl_hint)
        else:
            delayrow.pack_forget()
    for _r in [w for w in trigf.winfo_children()
               if isinstance(w, ttk.Radiobutton)]:
        _r.configure(command=_sync_trig)
    _sync_trig()

    # 右：做什么（动作）
    actf = ttk.LabelFrame(grid, text=i18n.tr('做什么（动作）', 'What (action)'),
                          padding='8')
    actf.grid(row=0, column=1, sticky='nsew')
    act = tk.StringVar(value='shutdown')
    # ★R241（用户 item1d「至于之前的关机，重启，这两个默认10秒效果就很好，
    # 不需要改，到时候得隔开这两个按钮，免得误解」）：动作区拆两段——
    # 上面是"启动类"（会吃延后启动），关机/重启单独一个带标题的分组框，
    # 并写明它们自带 10 秒倒计时、不受上面延时影响，免得用户以为
    # "设了延后启动，关机也会跟着延"。
    ACTS = [('open', i18n.tr('打开软件/文件', 'Open app/file')),
            ('script', i18n.tr('运行脚本(.bat/.py)', 'Run script (.bat/.py)')),
            ('cmd', i18n.tr('自定义命令', 'Custom command')),
            ('vm', i18n.tr('打开虚拟机（VMware/VirtualBox）',
                           'Start VM(s) (VMware/VirtualBox)'))]
    for v, txt in ACTS:
        ttk.Radiobutton(actf, text=txt, value=v, variable=act).pack(
            anchor=tk.W)
    prow = ttk.Frame(actf)
    prow.pack(fill=tk.X, pady=(4, 0))
    pvar = tk.StringVar()

    def _browse():
        pth = filedialog.askopenfilename(parent=gui.root)
        if pth:
            pvar.set(pth)

    ttk.Entry(prow, textvariable=pvar).pack(side=tk.LEFT, fill=tk.X,
                                            expand=True)
    ttk.Button(prow, text='…', width=3, command=_browse).pack(
        side=tk.LEFT, padx=(4, 0))

    # ★R241：关机 / 重启 —— 与上面"启动类"隔开一组（用户「免得误解」）
    pwr_frame = ttk.LabelFrame(
        actf, text=i18n.tr('⏻ 关机 / 重启', '⏻ Shutdown / Reboot'),
        padding='6')
    pwr_frame.pack(fill=tk.X, pady=(8, 0))
    for _pv, _pt in (('shutdown', i18n.tr('关机（延迟 10 秒）',
                                          'Shutdown (10s delay)')),
                     ('reboot', i18n.tr('重启（延迟 10 秒）',
                                        'Reboot (10s delay)'))):
        ttk.Radiobutton(pwr_frame, text=_pt, value=_pv,
                        variable=act).pack(anchor=tk.W)
    ttk.Label(pwr_frame, text=i18n.tr('自带 10 秒倒计时，不受上方延时影响',
                                      'Fixed 10s countdown; ignores the '
                                      'delay above'),
              foreground=colors['text_secondary'],
              font=ui_layout.app_font(9)).pack(anchor=tk.W)

    # ★ R185（用户 19 批）：打开主流虚拟机的某个/某几个虚拟机。
    # VMware 走 inventory.vmls 清单+vmrun 启动；VirtualBox 走
    # VBoxManage list vms+startvm。多选=写一个启动 bat 交给计划任务
    # （schtasks /tr 有 261 字符上限，bat 不受限）。
    vmrow = ttk.Frame(actf)
    _vm_tools = {'vmrun': None, 'vbox': None, 'vmdir': None}

    def _find_vmware_dir():
        """★R205（用户 01 批"列表只有 VBox 没有 VMware"）：定位 VMware
        安装目录——注册表 InstallPath 优先（Workstation/Player 都认，
        含 Wow6432Node），再兜底两个 Program Files 的常见目录名。
        Player 默认不带 vmrun，但 inventory.vmls 照常有——列表不能被
        vmrun 绑架。"""
        if _vm_tools['vmdir']:
            return _vm_tools['vmdir']
        import winreg as _wr
        cands = []
        for hive in (_wr.HKEY_LOCAL_MACHINE, _wr.HKEY_CURRENT_USER):
            for sub in (r'SOFTWARE\Wow6432Node\VMware, Inc.\VMware Workstation',
                        r'SOFTWARE\VMware, Inc.\VMware Workstation',
                        r'SOFTWARE\Wow6432Node\VMware, Inc.\VMware Player',
                        r'SOFTWARE\VMware, Inc.\VMware Player'):
                try:
                    k = _wr.OpenKey(hive, sub)
                    ip = _wr.QueryValueEx(k, 'InstallPath')[0]
                    cands.append(ip)
                    _wr.CloseKey(k)
                except OSError:
                    pass
        import glob as _glob
        cands += _glob.glob(r'C:\Program Files (x86)\VMware\VMware *')
        cands += _glob.glob(r'C:\Program Files\VMware\VMware *')
        for c in cands:
            c = c.rstrip('\\')
            if os.path.isfile(os.path.join(c, 'vmware.exe')):
                _vm_tools['vmdir'] = c
                return c
        return None

    def _find_vmrun():
        if _vm_tools['vmrun']:
            return _vm_tools['vmrun']
        import shutil as _sh
        vmdir = _find_vmware_dir()
        if vmdir:
            p = os.path.join(vmdir, 'vmrun.exe')
            if os.path.isfile(p):
                _vm_tools['vmrun'] = p
                return p
        _vm_tools['vmrun'] = _sh.which('vmrun')
        return _vm_tools['vmrun']

    def _find_vbox():
        if _vm_tools['vbox']:
            return _vm_tools['vbox']
        import shutil as _sh
        p = r'C:\Program Files\Oracle\VirtualBox\VBoxManage.exe'
        _vm_tools['vbox'] = p if os.path.isfile(p) else _sh.which('VBoxManage')
        return _vm_tools['vbox']

    # ★R207（用户 05批-2）：列表加高（4→10）——只能看 4 个太挤；
    # ★R207（05批-2 用户"列表略短+得美观"）：Listbox 换 Treeview——
    # 类型/名称/位置 三列对齐（原来 'VMware｜D:\...' 长串挤一行），
    # 行底色斑马纹+VMware/VBox 用主题强调色区分；高度 4→9 行。
    # vm_rows=每行的结构化数据（_create 直接消费，不再解析 '｜' 串）
    vm_rows = []   # [{'kind':'vmware'|'vbox', 'name':…, 'detail':vmx或uuid}]

    def _blend_hex(c1, c2, t):
        """两 hex 颜色线性混合（斑马纹底色用，随主题变化）"""
        a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
        b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
        return '#' + ''.join(
            f'{round(x + (y - x) * t):02x}' for x, y in zip(a, b))

    # ★R220（用户 06批-2"扫描按钮默认窗口显示不全，而且界面不是很美观"）：
    # 原来 Treeview / 滚动条 / 扫描按钮 / 用法提示 四个控件挤在 vmrow 的同一
    # 个 pack 行里（vmlist 先 pack side=LEFT fill=X expand=True）。Tk 把行宽
    # 全给了列表：扫描按钮实测 winfo_width=2（请求 66，等于不存在）；提示
    # Label 作为后到的 TOP 兄弟被顶到窗口外（实测 y=1039 > 窗口底 989）；
    # height=9 的列表又把右列撑到 470px，➕ 创建 落到折叠线以下。
    # 改=三段竖排：工具行（扫描 + 状态）/ 列表行（滚动条先 pack RIGHT 占位，
    # 列表吃剩下的）/ 提示行；列表 9→5 行，列宽收窄到右列分得到的宽度。
    vm_hd = ttk.Frame(vmrow)
    vm_hd.pack(fill=tk.X)
    vm_stat = ttk.Label(vm_hd, text=i18n.tr('未扫描', 'Not scanned'),
                        foreground=colors['text_secondary'],
                        font=ui_layout.app_font(9))
    vm_stat.pack(side=tk.RIGHT, padx=(6, 0))
    vm_body = ttk.Frame(vmrow)
    vm_body.pack(fill=tk.X, pady=(4, 0))
    vmlist = ttk.Treeview(vm_body, columns=('kind', 'name', 'path'),
                          show='headings', height=5,
                          selectmode='extended')
    vmlist.heading('kind', text=i18n.tr('类型', 'Kind'))
    vmlist.heading('name', text=i18n.tr('名称', 'Name'))
    vmlist.heading('path', text=i18n.tr('路径 / UUID', 'Path / UUID'))
    vmlist.column('kind', width=70, anchor=tk.CENTER, stretch=False)
    vmlist.column('name', width=140, anchor=tk.W, stretch=False)
    vmlist.column('path', width=170, anchor=tk.W)
    # VMware 行=主题紫、VBox 行=主题青（强调色随主题切换）；斑马纹=bg 与
    # fg 按 4% 混合，深浅主题都不突兀
    vmlist.tag_configure('vmw', foreground=colors['secondary'])
    vmlist.tag_configure('vbx', foreground=colors['primary'])
    vmlist.tag_configure('odd', background=_blend_hex(
        colors['bg'], colors['fg'], 0.04))
    vmlist.tag_configure('muted', foreground=colors['text_secondary'])
    vmsb = ttk.Scrollbar(vm_body, command=vmlist.yview)
    vmsb.pack(side=tk.RIGHT, fill=tk.Y)
    vmlist.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    vmlist.configure(yscrollcommand=vmsb.set)

    def _scan_vms():
        import subprocess as _sp2
        vmlist.delete(*vmlist.get_children())
        vm_rows.clear()
        # ★R220：VBoxManage list vms 可能要一秒上下，先给个进行中状态
        vm_stat.configure(text=i18n.tr('扫描中…', 'Scanning…'))

        def _put(kind, name, detail, idx):
            tags = []
            if kind == 'vmware':
                tags.append('vmw')
            else:
                tags.append('vbx')
            if idx % 2:
                tags.append('odd')
            vmlist.insert('', tk.END, tags=tags,
                          values=('VMware' if kind == 'vmware'
                                  else 'VirtualBox', name, detail))

        idx = 0
        # ★R205（用户 01 批"列表只有 VBox 没有 VMware"实锤）：VMware 分支
        # 此前被 `if vmrun:` 整体拦住——vmrun 找不到（Player 默认不带/
        # 非标安装路径）连 inventory.vmls 都不去读，列表只剩 VBox。
        # 修=列表与 vmrun 解耦：清单文件独立扫描，vmrun 只在"启动"时才需要。
        _vmdir = _find_vmware_dir()
        _inv_cands = [os.path.join(os.environ.get('APPDATA', ''),
                                   'VMware', 'inventory.vmls')]
        import glob as _glob
        _inv_cands += sorted(_glob.glob(
            os.path.join(os.environ.get('APPDATA', ''), 'VMware', '*.vmls')))
        inv = next((p for p in _inv_cands if p and os.path.isfile(p)), None)
        if inv:
            try:
                # ★R204 GUIA-11：with 关句柄 + BOM 嗅探编码（.vmls 可能
                # 是 UTF-16，utf-8+replace 读出乱码路径）
                with open(inv, 'rb') as _rf:
                    raw = _rf.read()
                enc = 'utf-16' if raw[:2] in (b'\xff\xfe', b'\xfe\xff') \
                    else 'utf-8'
                with open(inv, encoding=enc, errors='replace') as _lf:
                    for line in _lf:
                        line = line.strip()
                        # 行如：index0.config = "D:\VMs\win7\win7.vmx"
                        if '.config' in line and '=' in line:
                            vmx = line.split('=', 1)[1].strip().strip('"')
                            if vmx:
                                _nm = os.path.splitext(
                                    os.path.basename(vmx))[0]
                                vm_rows.append(dict(kind='vmware',
                                                    name=_nm, detail=vmx))
                                _put('vmware', _nm, vmx, idx)
                                idx += 1
            except Exception as e:
                logger.warning(f'读 VMware 清单失败: {e}')
        elif _vmdir and not _vm_tools.get('inv_warned'):
            # 装了 VMware 却没有清单文件（装完没跑过/没建过虚拟机）
            _vm_tools['inv_warned'] = True
            logger.info('VMware 已装但无 inventory.vmls（还没创建过虚拟机）')
        def _finish_scan():
            """结果回填后的收尾（**只在主线程调用**）：空表提示 / 数量摘要"""
            if not vm_rows:
                vmlist.insert('', tk.END, tags=('muted',),
                              values=('—', i18n.tr('未扫到虚拟机',
                                                   'No VMs found'), ''))
                # ★R220：长说明放状态位（原来塞在列表单元格里，列宽不够就被裁成
                # "没扫到虚拟机：装好 VMware,"）
                vm_stat.configure(text=i18n.tr(
                    '装好 VMware/VirtualBox 并创建过虚拟机后再扫描',
                    'Install VMware/VirtualBox, create a VM, then rescan'))
            else:
                _nw = sum(1 for r in vm_rows if r['kind'] == 'vmware')
                vm_stat.configure(text=i18n.tr(
                    f'共 {len(vm_rows)} 台 · VMware {_nw} · VirtualBox '
                    f'{len(vm_rows) - _nw}',
                    f'{len(vm_rows)} VMs · VMware {_nw} · VirtualBox '
                    f'{len(vm_rows) - _nw}'))

        # ★R221（NEW7-P1-5）：VBoxManage 是唯一可能吃掉 30 秒的调用（VirtualBox
        # 服务/注册表卡住时），原来同步跑在主线程＝整个界面冻住，而且"扫描中…"
        # 和阻塞在同一线程上连提示都刷不出来（用户口径"按钮没反应"）。
        # 挪进后台线程，结果按既有口径经 gui._ui_queue 回主线程填表。
        vbox = _find_vbox()
        if vbox:
            def _merge_vbox(got):
                n = len(vm_rows)
                for i, (nm, uid) in enumerate(got):
                    vm_rows.append(dict(kind='vbox', name=nm, detail=uid))
                    _put('vbox', nm, uid, n + i)
                _finish_scan()

            def _vbox_worker():
                got = []
                try:
                    pp = _sp2.run([vbox, 'list', 'vms'], capture_output=True,
                                  timeout=30, creationflags=CREATE_NO_WINDOW)
                    txt = pp.stdout.decode('utf-8', errors='replace')
                    got = [(m.group(1), m.group(2)) for m in re.finditer(
                        r'"(.+?)"\s*\{([0-9a-fA-F-]+)\}', txt)]
                except Exception as e:
                    logger.warning(f'VBoxManage list vms 失败: {e}')
                try:
                    gui._ui_queue.put(lambda: _merge_vbox(got))
                except Exception:
                    pass
            import threading as _th
            _th.Thread(target=_vbox_worker, daemon=True).start()
            return
        _finish_scan()

    # ★R220：扫描按钮回到工具行（旧版 width=5 + 挤在列表同一 pack 行 → 2px）
    ttk.Button(vm_hd, text=i18n.tr('↻ 扫描', '↻ Scan'),
               command=_scan_vms).pack(side=tk.LEFT)
    # ★R207（用户 05批-2）：用法提示；★R220 单独一行（原来被顶到窗口外）
    ttk.Label(vmrow, text=i18n.tr(
        '单击选中（Ctrl/Shift 多选）· 双击=只选这一台 · 选好后点右下角 ➕ 创建',
        'Click select (Ctrl/Shift multi) · double-click=single · '
        'then ➕ Create below'),
        foreground=colors['text_secondary'], justify=tk.LEFT,
        font=ui_layout.app_font(9)).pack(fill=tk.X, pady=(3, 0))

    def _vm_pick_one(event):
        """双击=只选双击的那一行（EXTENDED 模式下保留多选语义的补充操作）"""
        iid = vmlist.identify_row(event.y)
        if iid:
            vmlist.selection_set(iid)
            vmlist.see(iid)
    vmlist.bind('<Double-Button-1>', _vm_pick_one)

    def _sync_act(_e=None):
        # ★R241：prow/vmrow 一律插在 pwr_frame **之前**——关机/重启那组钉在
        # 卡片底部，路径行跟着动作切换时不会甩到它下面被误读成"关机的参数"
        if act.get() == 'vm':
            prow.pack_forget()
            vmrow.pack(fill=tk.X, pady=(4, 0), before=pwr_frame)
            if not vmlist.get_children():
                _scan_vms()
        else:
            vmrow.pack_forget()
            prow.pack(fill=tk.X, pady=(4, 0), before=pwr_frame)
    # ★R241：关机/重启的单选现在是 pwr_frame 的孩子，绑 command 要把
    # 那组一起扫进来（否则切到它们时路径行还挂着）
    for _ra in [w for w in (actf.winfo_children() + pwr_frame.winfo_children())
                if isinstance(w, ttk.Radiobutton)]:
        _ra.configure(command=_sync_act)

    def _create():
        import subprocess as _sp
        import time as _time
        trig_v, act_v = trig.get(), act.get()
        tr_args = []
        _delay_target = None
        # ★R207（06批-1）：任务动作的工作目录。schtasks /create 没法设
        # "起始于"（Task Scheduler GUI 才有）——不设的话任务以 CWD=
        # System32 运行，很多程序（如 v2rayN 按相对路径找配置）直接起
        # 不来=用户看到的"任务不能执行"。open/script/vm 三类动作在
        # 创建成功后补写 Actions[0].WorkingDirectory。
        _wd = ''
        # ★R241：建完要拿它跟任务里实际存的动作比对（open/script/vm 才有）
        _rb_expect = ''
        # ★R275（item2）：走隐藏启动器时，`<Command>` 是 wscript，真正的目标
        # 在 `<Arguments>` 里 ⇒ 回读必须两条一起比（空串＝不比 Arguments，
        # 与批前行为逐字节等价）
        _rb_expect_arg = ''
        # ★R241（用户 item1b）：延后启动只对"登录/开机 + 启动类动作"有效。
        # 关机/重启自带 10 秒倒计时、语义固定，不吃这一行（用户"10 秒就很
        # 好，不需要改"），且开关机在延后启动行里根本不显示。
        _sd_sec = 0
        if trig_v in ('logon', 'boot') and act_v not in ('shutdown', 'reboot'):
            _sd_sec, _sd_err = _parse_startup_delay(_sdvar.get(),
                                                    _svunit.get())
            if _sd_err:
                messagebox.showwarning(
                    i18n.tr('提示', 'Notice'),
                    i18n.tr(_sd_err, _sd_err), parent=gui.root)
                return
        if trig_v in ('minutes', 'hours', 'days'):
            # ★R207-3g（用户 09 批）：分钟上限 59（要更长用小时），小时/
            # 天 1~999；不再静默截断，超界明确报
            _cap = 59 if trig_v == 'minutes' else 999
            _unit = {'minutes': '分钟', 'hours': '小时',
                     'days': '天'}[trig_v]
            try:
                _n = int(nvar.get().strip())
            except Exception:
                _n = 0
            if not 1 <= _n <= _cap:
                messagebox.showwarning(
                    i18n.tr('提示', 'Notice'),
                    i18n.tr(f'N（{_unit}）要是 1~{_cap} 的整数'
                            + ('（更久请改用"每隔 N 小时"）'
                               if trig_v == 'minutes' else ''),
                            f'N ({_unit}) must be an integer 1~{_cap}'),
                    parent=gui.root)
                return
            sc = {'minutes': 'minute', 'hours': 'hourly', 'days': 'daily'}[trig_v]
            tr_args = ['/sc', sc, '/mo', str(_n)]
        elif trig_v == 'daily':
            # ★ R166：宽松解析 9:5 / 09:05 / 21:05:30（可选秒）
            s = tmvar.get().strip().replace('：', ':')
            m = re.match(r'^(\d{1,2})[:.](\d{1,2})(?:[:.](\d{1,2}))?$', s)
            if (not m or not (0 <= int(m.group(1)) <= 23
                              and 0 <= int(m.group(2)) <= 59)
                    or (m.group(3) and not 0 <= int(m.group(3)) <= 59)):
                messagebox.showwarning(
                    i18n.tr('提示', 'Notice'),
                    i18n.tr('时间格式不对，示例：9:30、21:05:30',
                            'Bad time, e.g. 9:30 or 21:05:30'),
                    parent=gui.root)
                return
            _st = f"{int(m.group(1)):02d}:{int(m.group(2)):02d}"
            if m.group(3):
                _st += f":{int(m.group(3)):02d}"
            tr_args = ['/sc', 'daily', '/st', _st]
        elif trig_v == 'logon':
            tr_args = ['/sc', 'onlogon']
        elif trig_v == 'delay':
            # ★ R185：一次性延时任务（用户 19 批"指定多长时间后"）。
            # schtasks /sd 日期格式随系统区域变化，改用 PowerShell
            # Register-ScheduledTask 注册（ParseExact 区域无关），
            # 触发点 = 现在 + 时长
            # ★R207-3g（用户 09 批）：支持"天小时分钟秒"组合格式，各
            # 单位带范围约束（解析器统一报错，含旧式 H:MM:SS 兼容）
            _sec, _err = _parse_delay_duration(dlvar.get())
            if _err:
                messagebox.showwarning(
                    i18n.tr('提示', 'Notice'),
                    i18n.tr(_err, _err),
                    parent=gui.root)
                return
            _delay_target = _time.strftime(
                '%Y-%m-%d %H:%M:%S',
                _time.localtime(_time.time() + _sec))
        else:
            tr_args = ['/sc', 'onstart']
        if act_v in ('open', 'script'):
            # ★R241（P1 实锤，用户 item1「开机打开vmware不成功……感觉有
            # 启动成功率」）：从资源管理器右键"复制为路径"拿到的字符串
            # **自带引号**，而下面交给 schtasks 又要包一层 ⇒ 任务动作变成
            # ""path""，被拆成 <Command>""</Command> ⇒ 到点 0x80070057
            # 什么都没发生。更坑的是存在性校验写的 path.strip('"')，把引号
            # 去了照样放行 ⇒ 弹窗"创建成功"、任务实坏。
            # 现在**先清洗、校验与拼接用同一个值**，三处口径一致。
            path = _clean_task_path(pvar.get())
            if not path:
                messagebox.showwarning(i18n.tr('提示', 'Notice'),
                                       i18n.tr('先选要打开/运行的文件',
                                               'Pick a file first'),
                                       parent=gui.root)
                return
            # ★R207（用户 05批-1"v2rayN 任务不能执行"实锤）：路径不存在
            # 时 schtasks 也"创建成功、运行也报成功"，但 exe 找不到=啥都
            # 不发生（用户把文件夹从 E: 挪到 D:，旧任务静默失效）。创建
            # 时就拦住，并区分"盘不在/文件不在"两种提示。
            _raw = path
            if _raw[:2] in ('\\\\',) or re.match(r'^[A-Za-z]:', _raw):
                if not os.path.exists(_raw):
                    _drive = _raw[:2] if re.match(r'^[A-Za-z]:', _raw) else ''
                    _tip = (f'盘 {_drive} 不存在（是否移动硬盘/网络盘没挂载？）'
                            if _drive and not os.path.isdir(_drive + '\\')
                            else '文件不存在（被移动/删除/改名？）')
                    messagebox.showerror(
                        i18n.tr('路径无效', 'Invalid Path'),
                        i18n.tr(f'{_raw}\n{_tip}\n请重新选择有效路径，'
                                f'否则任务到点什么都不会发生。',
                                f'{_raw}\n{_tip}\nPick a valid path, or '
                                f'the task will silently do nothing.'),
                        parent=gui.root)
                    return
            tr_val = f'"{path}"'
            _wd = os.path.dirname(_raw)
            _rb_expect = path
            # ★R275（用户 item2「除了必须的 cmd 外，能不显示最好不显示」）：
            # `.bat/.cmd` 到点必然带一个黑色控制台，而这两类正是"打开软件/
            # 跑脚本"最常见的目标。exe 本来就没有控制台，**不套**启动器——
            # 多一层文件就多一层坏法，隐藏收益为零。
            # ★R277：判断收进 `_hide_action`（与 ✏️ 编辑保存、存量迁移同一口径，
            # 不再在这里各写一份 `if _need_hide(...)`）。
            _tv, _hid = _hide_action(tr_val)
            if _hid:
                tr_val = _tv
                _rb_expect = _exe_of(_tv)
                _rb_expect_arg = _arg_of(_tv)
        elif act_v == 'vm':
            # ★ R185：选中虚拟机（可多选）→ 写启动 bat → 任务跑 bat
            # （schtasks /tr 有 261 字符上限，多台虚拟机必然超）
            # ★R207：Treeview 化后直接吃 vm_rows 结构化数据（iid→行号→行）
            sels = [vm_rows[vmlist.index(i)] for i in vmlist.selection()
                    if vmlist.index(i) < len(vm_rows)]
            if not sels:
                messagebox.showwarning(
                    i18n.tr('提示', 'Notice'),
                    i18n.tr('先点"↻ 扫描"，再在列表里选虚拟机（按住 Ctrl 可多选）',
                            'Click Scan, then pick VM(s) (Ctrl-click multi)'),
                    parent=gui.root)
                return
            # ★R241：entries=(虚拟机名, 启动命令) —— 名字要进文件名和 bat
            # 的分段注释，用户才能一眼认出"这一段在起哪台"。
            entries = []
            for r in sels:
                _vn = str(r.get('name') or r.get('detail') or 'vm')
                if r['kind'] == 'vmware':
                    _vr = _find_vmrun()
                    if _vr:
                        entries.append(
                            (_vn, f'"{_vr}" -T ws start "{r["detail"]}"'))
                    else:
                        # ★R205：vmrun 缺席（Player 默认不带/绿色版）回退
                        # vmware.exe GUI 打开该 vmx——列表能扫到就该能启动
                        _vd = _find_vmware_dir()
                        _vme = os.path.join(_vd, 'vmware.exe') if _vd else None
                        if not _vme or not os.path.isfile(_vme):
                            messagebox.showwarning(
                                i18n.tr('提示', 'Notice'),
                                i18n.tr('没找到 vmrun.exe/vmware.exe'
                                        '（VMware 未安装或绿色版）',
                                        'vmrun.exe/vmware.exe not found '
                                        '(VMware missing?)'),
                                parent=gui.root)
                            return
                        entries.append((_vn, f'"{_vme}" "{r["detail"]}"'))
                else:
                    _vb = _find_vbox()
                    if not _vb:
                        messagebox.showwarning(
                            i18n.tr('提示', 'Notice'),
                            i18n.tr('没找到 VBoxManage.exe（VirtualBox 未安装）',
                                    'VBoxManage.exe not found'),
                            parent=gui.root)
                        return
                    entries.append(
                        (_vn, f'"{_vb}" startvm "{r["detail"]}"'))
            # ★R207（用户 05批-2"点创建没有用"实锤）：此处曾 import 不存在
            # 的 APP_DIR → ImportError 让 Tk 回调无声死亡（打包态无
            # stderr=按钮像坏的）。app_paths 的正确入口是 get_app_dir()。
            # ★R271：不再用 get_app_dir()——那是**带版本号的程序目录**，
            # 升级换目录后任务动作里的绝对路径就成了死链（详见 _vm_bat_dir）。
            _vdir = _vm_bat_dir()
            # ★R204 GUIA-15：文件名加随机后缀防同秒重名互相覆盖 + 顺手清理
            # 30 天以上的旧 bat（此前只增不删）
            # ★R241（用户 item1a「备注里面写上类似 vm_这块写虚拟机名字，
            # 这样好区别，不要只是简单的复制任务名」）：文件名带虚拟机名
            # （vm_win10_x64_093000_413），多台=第一台 + _xN 台数标记。
            _stem = _vm_bat_stem([str(r.get('name') or 'vm') for r in sels],
                                 _time.strftime('%H%M%S'), os.getpid())
            _bat = os.path.join(_vdir, _stem + '.bat')
            try:
                _now = _time.time()
                # ★R207 NEW3-A2 + ★R210（NEW4-A2）：清理前查 schtasks 引用
                # ——bat 被任务 /tr 指着时删掉=用户的开机拉虚拟机静默失效。
                # 修复两点：①查询必须带 /v（无 /v 的 LIST 没有动作字段，
                # 引用比对恒空=保护空转，本机实测 2072 行 0 命中）；②查询
                # 失败 fail-closed——拿不到引用集就跳过本轮清理，宁可不删。
                _refs = None
                try:
                    _rq = _sp.run(['schtasks', '/query', '/fo', 'LIST', '/v'],
                                  capture_output=True, timeout=60,
                                  stdin=_sp.DEVNULL,
                                  creationflags=0x08000000)
                    if _rq.returncode == 0:
                        _refs = os.path.normcase(
                            _rq.stdout.decode('gbk', errors='replace'))
                        # ★R213（NEW5-D2）：fail-closed 此前只装异常与 rc≠0，
                        # 没装"rc=0 但输出为空/整页乱码"——_refs='' 时所有
                        # `in` 恒 miss = 老 bat 全删，与修复前完全等效。
                        # 正常输出必含动作字段行（中文"要运行的任务"/英文
                        # 'Task To Run'，normcase 后小写），不含=不可信。
                        if (not _refs or ('要运行的任务' not in _refs
                                          and 'task to run' not in _refs)):
                            _refs = None
                except Exception:
                    _refs = None
                if _refs is None:
                    logger.warning('无法获取计划任务引用清单，跳过本轮 '
                                   'vm bat 清理（防误删现役引用）')
                else:
                    # ★R275（item2）：隐藏启动器 .vbs 跟 bat 同一个家、同一把
                    # 尺子清算（下面按**文件名 token** 查引用，被任务指着的
                    # 那条永远删不掉）。旧写法只扫 `_vdir` 顶层，启动器会
                    # 只增不删——★R204 那条"此前只增不删"的教训别再来一遍。
                    _cand = [(os.path.join(_vdir, _f), _f)
                             for _f in os.listdir(_vdir)]
                    _ld = os.path.join(_vdir, _HL_DIR)
                    if os.path.isdir(_ld):
                        _cand += [(os.path.join(_ld, _f), _f)
                                  for _f in os.listdir(_ld)]
                    for _fp, _f in _cand:
                        try:
                            if (_f.startswith('vm_')
                                    or _f.endswith('.vbs')) and \
                                    _now - os.path.getmtime(_fp) > 30 * 86400:
                                # ★R213（NEW5-D3）：整条长路径去子串匹配会被
                                # schtasks LIST /v 的动作列截断（80+ 字符任务
                                # 名+引号→尾部 .bat 被砍），路径 miss 但文件
                                # 其实仍被引用→误删。改用唯一文件名 token
                                # （vm_<秒>_<pid3>.bat）匹配，短且总在。
                                import re as _re
                                _tok = _re.escape(_f)  # 形如 vm_a_153012_007.bat
                                if _re.search(_tok, _refs):
                                    logger.info(f'vm bat 仍被计划任务引用，'
                                                f'跳过清理: {_f}')
                                    continue
                                os.remove(_fp)
                        except OSError:
                            pass
            except OSError:
                pass
            # mbcs=系统 ANSI 码页，cmd 可正确读中文路径
            # ★R241（用户 item1「有时候隔很长时间才能启动」）：bat 不再是
            # 光秃秃一行命令——每台一段、跑前跑后写时间戳+返回码到同目录
            # .log，失败自动重试（10 秒一次、最多 3 次）。下次再出问题，
            # 打开 scheduled_vms\vm_xxx.log 就知道"到底跑没跑、几点跑的、
            # 返回几"，不用靠"重启一遍试试"。
            with open(_bat, 'w', encoding='mbcs') as _bf:
                _bf.write(_build_vm_bat(entries, _stem + '.log'))
            tr_val = f'"{_bat}"'
            _wd = _vdir
            _rb_expect = _bat
            # ★R275（用户 item2「启动虚拟机……尽量不要出现黑窗」）：动作改指
            # 隐藏启动器（wscript + vbs，`Run …, 0, True`），黑窗没了、
            # "上次结果"仍是 bat 的真返回码。造不出来（目录/权限/路径含引号/
            # 动作超 250 字符）⇒ 一个字不改地保留直接跑 bat：**难看比断链好修**。
            # ★R277：走 `_hide_action` 统一口径（stem 要留着，备注靠文件名认虚拟机）。
            _tv, _hid = _hide_action(tr_val, _stem)
            if _hid:
                tr_val = _tv
                _rb_expect = _exe_of(_tv)
                _rb_expect_arg = _arg_of(_tv)
        elif act_v == 'cmd':
            cmdv = pvar.get().strip()
            if not cmdv:
                messagebox.showwarning(i18n.tr('提示', 'Notice'),
                                       i18n.tr('先填写命令', 'Enter a command'),
                                       parent=gui.root)
                return
            tr_val = cmdv
        elif act_v == 'shutdown':
            tr_val = r'C:\Windows\System32\shutdown.exe /s /t 10'
        else:
            tr_val = r'C:\Windows\System32\shutdown.exe /r /t 10'
        # ★ R177（用户"好几个开机启动 zcode"）：同触发+同动作的任务已有
        # 时先确认，防手滑重复创建一串
        try:
            same = [str(task_tree.set(i, 'name'))
                    for i in task_tree.get_children()
                    if str(task_tree.set(i, 'name'))
                    .startswith(f'DeskBits_{trig_v}_{act_v}_')]
            if same and not messagebox.askyesno(
                    i18n.tr('已有同类任务', 'Similar Task Exists'),
                    i18n.tr(
                        f'已有 {len(same)} 个相同触发+动作的任务'
                        f'（如 {same[0].lstrip(chr(92))}）。\n'
                        f'仍要再创建一条吗？',
                        f'{len(same)} task(s) with same trigger+action '
                        f'already exist.\nCreate another one?'),
                    parent=gui.root):
                return
        except Exception:
            pass
        # ★ R166：关机/重启动作必须二次确认（用户实测：测试任务到点
        # 触发 shutdown.exe 导致"莫名其妙重启"）
        if act_v in ('shutdown', 'reboot'):
            act_txt = (i18n.tr('关机', 'Shutdown') if act_v == 'shutdown'
                       else i18n.tr('重启', 'Reboot'))
            if not messagebox.askyesno(
                    i18n.tr('高危动作确认', 'Confirm Dangerous Action'),
                    i18n.tr('该任务将在触发时执行【' + act_txt + '】！\n'
                            '确认创建吗？（创建后可在上方列表删除）',
                            'This task will ' + act_txt.upper() +
                            ' the machine when triggered.\nCreate it?'),
                    parent=gui.root):
                return
        # ★R251（07 批 item2a + item2d）：**从这里开始全是"跟系统打交道"的慢活**
        # ——登记任务 → 补工作目录 → 动作回读 → 电源条件 → 查下次运行时间，
        # 合计 5~20 秒，期间主线程被 subprocess 占住、界面看着像死掉。
        # 原来这段一句提示都没有 ⇒ 用户"不知道自己点了还是没点"（item2a），
        # 脚本类任务那句"添加成功没提示"也出在这段（item2d）。
        # 前面的校验/二次确认都在开窗之前，不会闪一下没提示的窗。
        _wb = _busy_open(
            gui, i18n.tr('⏳ 请稍等，正在创建计划任务…',
                         '⏳ Please wait, creating the task…'),
            i18n.tr('别关窗口，正在和系统打交道（约几秒到十几秒）',
                    'Keep this window open — talking to the system '
                    '(a few seconds up to ~20s)'))
        # ★ R166b：任务名带动作（列表一眼可辨"这个任务是干什么的"——
        # 本次事故用户看到 DeskBits_minutes_172800 无法知道它是关机任务）
        name = f'DeskBits_{trig_v}_{act_v}_' + _time.strftime('%H%M%S')
        if trig_v == 'delay':
            # ★ R185：PowerShell 注册一次性延时任务（区域无关，见上）
            tv = tr_val.strip()
            if tv.startswith('"'):
                _e1 = tv.find('"', 1)
                _exe, _arg = tv[1:_e1], tv[_e1 + 1:].strip()
            else:
                _pp2 = tv.split(None, 1)
                _exe = _pp2[0]
                _arg = _pp2[1] if len(_pp2) > 1 else ''
            _psa = (f"$a = New-ScheduledTaskAction -Execute "
                    f"'{_exe.replace(chr(39), chr(39) * 2)}'")
            if _arg:
                _psa += (f" -Argument "
                         f"'{_arg.replace(chr(39), chr(39) * 2)}'")
            # ★R207（06批-1）：延时任务同样补工作目录
            if _wd:
                _psa += (f" -WorkingDirectory "
                         f"'{_wd.replace(chr(39), chr(39) * 2)}'")
            # 空参数不能写 -Argument ''（PS 参数校验拒绝空串，实测）
            _ps = (_psa + "; "
                   "$t = New-ScheduledTaskTrigger -Once -At "
                   "([datetime]::ParseExact("
                   f"'{_delay_target}','yyyy-MM-dd HH:mm:ss',$null)); "
                   f"Register-ScheduledTask -TaskName '{name}' "
                   "-Action $a -Trigger $t -Force | Out-Null")
            try:
                pp = _sp.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', _ps],
                             capture_output=True, timeout=60, stdin=_sp.DEVNULL,
                             creationflags=CREATE_NO_WINDOW)
                out = '成功' if pp.returncode == 0 else (
                    pp.stderr.decode('gbk', errors='replace').strip()
                    or '注册失败')
            except Exception as e:
                out = str(e)
        elif _sd_sec and trig_v in ('logon', 'boot'):
            # ★R241（用户 item1b「加个总的延时自定义的设置……或者你出个
            # 更好的方案」）：填了延后启动 ⇒ 改走 PowerShell，把延时交给
            # 任务计划程序原生的 Trigger.Delay（schtasks /create 没有延时
            # 参数；`schtasks /delay` 要 mm:ss 且并非全版本可用；塞进 bat
            # 的 timeout 在无控制台时静默不等待=假延时）。
            # **没填延时仍走下面的原 schtasks 路，一行没动**。
            _ps = _ps_delayed_register(name, tr_val, _wd, trig_v,
                                       _pt_duration(_sd_sec))
            try:
                pp = _sp.run(['powershell', '-NoProfile', '-NonInteractive',
                              '-Command', _ps], capture_output=True,
                             timeout=90, stdin=_sp.DEVNULL,
                             creationflags=CREATE_NO_WINDOW)
                out = '成功' if pp.returncode == 0 else (
                    pp.stderr.decode('gbk', errors='replace').strip()
                    or '注册失败')
            except Exception as e:
                out = str(e)
        else:
            try:
                pp = _sp.run(['schtasks', '/create', '/f', '/tn', name,
                              '/tr', tr_val] + tr_args, capture_output=True,
                             timeout=60,
                             creationflags=CREATE_NO_WINDOW)  # ★R191 GUIA-5 防黑框
                out = pp.stdout.decode('gbk', errors='replace') + \
                    pp.stderr.decode('gbk', errors='replace')
            except Exception as e:
                out = str(e)
        # ★R251 item2a：分段改文案——用户盯得到"进度在动"，才不会以为没点上
        _wb.set(i18n.tr('⏳ 已登记，正在核对任务动作…',
                        '⏳ Registered, verifying the stored action…'))
        if '成功' in out or 'SUCCESS' in out.upper():
            gui._log(f'📅 计划任务已创建: {name} → {tr_val}')
            # ★R207（06批-1）：schtasks 建完立刻补"起始于"（工作目录），
            # 否则任务以 CWD=System32 运行——v2rayN 这类按相对路径找
            # 配置的程序直接起不来（用户"路径明明是对的"的真因）。
            # 失败仅记日志不回滚：任务本身已建成，行为与旧版一致。
            # ★R210（NEW4-A4/A5）：stdin=DEVNULL+$ErrorActionPreference
            # 防 Get-ScheduledTask 未命中时交互提示卡满 30s；延时路已带
            # -WorkingDirectory，这里跳过防重复写；成功后回读核对。
            if _wd and act_v in ('open', 'script', 'vm') \
                    and trig_v != 'delay' and not _sd_sec:
                try:
                    _wd_ps = _wd.replace(chr(39), chr(39) * 2)
                    _pw = _sp.run(
                        ['powershell', '-NoProfile', '-NonInteractive',
                         '-Command',
                         "$ErrorActionPreference='Stop'; "
                         f"$wd='{_wd_ps}'; "
                         f"try {{ $t = Get-ScheduledTask -TaskName '{name}' "
                         "-ErrorAction Stop; "
                         "$t.Actions[0].WorkingDirectory = $wd; "
                         "Set-ScheduledTask -InputObject $t | Out-Null; "
                         # ★R213（NEW5-D5）：注释承诺"回读核对"，代码只凭
                         # Set-ScheduledTask rc==0 就当成功——任务目录被安全
                         # 软件/权限拦下没真正写入时静默假成功。补真回读比对。
                         "$back=(Get-ScheduledTask -TaskName "
                         f"'{name}').Actions[0].WorkingDirectory; "
                         "if ($back -ne $wd) { "
                         "Write-Output (\"MISMATCH:\" + $back); exit 3 } "
                         "exit 0 } "
                         # catch 里把异常打到 stderr（原 exit 1 吞 stderr=
                         # 失败原因不可查）
                         "catch { Write-Error ($_ | Out-String); exit 1 }"],
                        capture_output=True, timeout=30, stdin=_sp.DEVNULL,
                        creationflags=0x08000000)
                    _pw_out = (_pw.stdout.decode('gbk', errors='replace').strip()
                               + _pw.stderr.decode('gbk', errors='replace')
                               .strip())
                    if _pw.returncode == 0:
                        gui._log(f'   ↳ 已设工作目录: {_wd}')
                    else:
                        logger.warning(
                            f'工作目录设置失败({name}) rc={_pw.returncode}: '
                            + _pw_out
                            + '——任务已建成，程序可能以 System32 为工作目录运行')
                        try:
                            gui._log(f'   ⚠ 工作目录未生效（任务照常创建），'
                                     '程序可能因相对路径起不来，详见日志')
                        except Exception:
                            pass
                except Exception as e:
                    logger.warning(f'工作目录设置异常({name}): {e}')
            # ★R241（P1 收口）：**建完必须把动作读回来核对**。schtasks 说
            # "成功"只代表任务写进去了，不代表到点跑得起来——本轮那个
            # <Command>""</Command> 就是"创建成功、开机静默失败"的形态。
            # 对不上 ⇒ 当场删掉这条坏任务 + 明确报错，绝不弹"创建成功"。
            _rb_ok = True
            if act_v in ('open', 'script', 'vm'):
                try:
                    _prb = _sp.run(
                        ['powershell', '-NoProfile', '-NonInteractive',
                         '-Command',
                         _ps_readback_cmd(name, _rb_expect,
                                          _rb_expect_arg)],
                        capture_output=True, timeout=45, stdin=_sp.DEVNULL,
                        creationflags=0x08000000)
                    _rb_got = _prb.stdout.decode('gbk', errors='replace').strip()
                    _rb_ok = (_prb.returncode == 0 and _rb_got.startswith('OK'))
                    if not _rb_ok:
                        logger.warning(f'计划任务动作回读不符({name}) '
                                       f'{_rb_got[:160]} ⇒ 删除该任务')
                        try:
                            _sp.run(['schtasks', '/delete', '/f', '/tn', name],
                                    capture_output=True, timeout=45,
                                    stdin=_sp.DEVNULL,
                                    creationflags=0x08000000)
                        except Exception:
                            pass
                except Exception as e:
                    _rb_ok = False
                    logger.warning(f'计划任务动作回读异常({name}): {e}')
            # 回读核对没过 ⇒ 只报错，不往下走成功提示
            if not _rb_ok:
                # ★R251 item2a/2d：终态提示之前先收掉忙碌窗，两条不叠着
                _busy_close(gui)
                messagebox.showerror(
                    i18n.tr('任务没建对（已删除这条）',
                            'Task Was Built Wrongly (deleted)'),
                    i18n.tr('任务已创建，但**回读核对发现它要运行的动作不对**'
                            '（这种任务到点什么都不会发生），已自动删除。\n\n'
                            '请在路径框里重新点 "…" 选择文件后再创建一次'
                            '（路径不用自己加引号，软件会处理）。',
                            'The task was created but its stored action '
                            'failed read-back (so it would silently do '
                            'nothing), and has been deleted.\n\nRe-pick the '
                            'file with "…" (no quotes needed) and create '
                            'again.'),
                    parent=gui.root)
                _load_tasks(gui, task_tree)
                return
            # ★R242（§二 D2 收口）：**回读通过也要留痕**。此前只有"回读不符"
            #   才写日志，用户回传的 win11 日志里看不出"这条任务当时动作是对的"，
            #   于是"开机有没有起来"只能靠猜（本轮那条 vmware 打不开的判读就卡
            #   在这儿）。成功态一行 INFO：任务名 + 读到的动作，够对账就行。
            if act_v in ('open', 'script', 'vm'):
                logger.info(f'计划任务动作回读通过({name}): {_rb_got[:160]}')
                try:
                    gui._log(f'   ↳ 回读核对通过：{name} 要运行的动作已确认')
                except Exception:
                    pass
            # ★R247（用户 item2）：**所有动作**统一在创建成功后设电源条件
            # （★R171 当年只给关机/重启清过电池限制，"打开软件/脚本/虚拟机"
            # 三类从来没清 ⇒ 笔记本不插交流电时到点根本不起任务，就是用户
            # 这次说的"有时候能启动有时候不能"）。两枚复选默认都不勾＝把
            # 系统那个"使用电池时不启动"的默认值清掉。
            # 设失败**不删任务**（任务已建成是事实），只在完成提示里写明。
            _wb.set(i18n.tr('⏳ 正在设置电源条件…',
                            '⏳ Setting power conditions…'))
            # ★R271（用户 `----2026-9-29 02----` item1「任务计划里面 vmware
            # 不能自动启动，后来我是手动启动的」）：**「开机时」触发必须强制
            # 错过补跑**。取证：本机实读两条 DeskBits 任务都是
            # `Principal.LogonType=Interactive` + `Settings.StartWhenAvailable=False`
            # ——开机那一刻还没有用户会话，交互式任务**起不来**；补跑又没开
            # ⇒ 错过之后永远不补，表现就是"开机时那一档从来不自动启动"。
            # 强制开补跑后，Windows 会在用户登录上桌面后立刻补运行一次，
            # 这正是用户要的"开机自动打开 VMware"。
            # 只改这一处调用（`_ps_delayed_register` 不许碰：
            # test_r247_power_conditions Z.3 红线钉着"延时注册路不得顺手设电池条件"）。
            _cu, _cu_forced = _catch_up_for(trig_v, _catch_var.get())
            _pw_ok, _pw_txt = _apply_power_settings(
                gui, name, _ac_only_var.get(), _stop_bat_var.get(),
                catch_up=_cu, wake=_wake_var.get())
            if _cu_forced:
                logger.info(f'计划任务({name}) 触发=开机时 ⇒ 已强制开启'
                            f'"错过补跑"(StartWhenAvailable)：开机时还没有用户'
                            f'会话，交互式任务不补跑就永远不会自动运行')
                try:
                    gui._log(f'   ↳ 开机触发已自动开启「错过补跑」，'
                             f'登录后会立刻补运行一次')
                except Exception:
                    pass
            # 成功/失败两种文案都自带 ⚡/⚠ 且已按语言档出词
            # （在 _apply_power_settings 里定的），这里只贴上去，
            # 不再二次拼接——免得 English 档混出一串中文（★R229 口径）。
            _pw_line = _pw_txt
            # ★ R163：成功即告知"下次运行时间"（主流软件做法，用户 -14/5）
            _wb.set(i18n.tr('⏳ 正在查询下次运行时间…',
                            '⏳ Querying the next run time…'))
            nxt = ''
            try:
                pq = _sp.run(['schtasks', '/query', '/tn', name, '/fo', 'CSV',
                              '/nh'], capture_output=True, timeout=60,
                           creationflags=0x08000000)  # CREATE_NO_WINDOW 防黑框
                import csv as _csv
                for row in _csv.reader(
                        io.StringIO(pq.stdout.decode('gbk', errors='replace'))):
                    # ★R191（Qoder GUIA-3）：CSV 列序实证=[任务名, 下次运行
                    # 时间, 模式]——原取 row[2] 把"就绪"当下次运行显示
                    if len(row) >= 2:
                        v = row[1].strip()
                        if v and v.upper() != 'N/A':
                            nxt = v
                            break
            except Exception:
                pass
            # ★R251 item2a：先收掉「⏳ 请稍等」再弹「✅ 创建成功」——用户要的
            # "和创建成功的提示配合"就是这句：等待窗不能把终态提示压在下面。
            _busy_close(gui)
            messagebox.showinfo(
                i18n.tr('完成', 'Done'),
                i18n.tr(f'✅ 计划任务创建成功：{name}\n'
                        f'下次运行: {nxt or "（按触发条件）"}\n'
                        + _pw_line + '\n\n'
                        f'请到上方"📅 计划任务（用户）"列表查看这条任务。\n'
                        f'如果列表里还没显示或显示不对，点一下"🔄 刷新"'
                        f'再看（win11 创建后生效略慢）。',
                        f'✅ Task created: {name}\n'
                        f'Next run: {nxt or "(per trigger)"}\n'
                        + _pw_line + '\n\n'
                        f'See it in the "📅 Scheduled Tasks" list above.\n'
                        f'If not shown yet, click "🔄 Refresh"'
                        f' (slower on Win11).'),
                parent=gui.root)
            _load_tasks(gui, task_tree)
            # ★ R187（用户 04 批"win11 下面创建略慢"）：2 秒后自动再刷
            # 一次，多数情况用户不用手动点刷新
            try:
                gui.root.after(2000, lambda: _load_tasks(gui, task_tree))
            except Exception:
                pass
        else:
            _busy_close(gui)          # ★R251 item2a：失败同样是终态，先收窗
            hint = i18n.tr('（"开机时"与部分动作需要管理员权限）',
                           '(Startup trigger and some actions need admin)')
            messagebox.showwarning(i18n.tr('失败', 'Failed'),
                                   out.strip()[:300] + hint, parent=gui.root)

    # ★R247（用户 39 批 item2「笔记本上忘记插电源就不能用了」）：电源条件
    # 两枚复选，**默认全不勾**＝把任务计划程序给新任务的"使用电池时不启动"
    # 默认值清掉。位置钉在创建器卡片底部、➕ 创建 上方，与「⏻ 关机/重启」
    # 那组**分开**（用户原话"隔开这两个按钮，免得误解"——这两条对**所有**
    # 动作生效，不是关机的专属选项）。
    # 其余条件（网络可用时才启动、空闲时才启动…）本批按 item2「其他功能存档」
    # 只做记录不做界面，见 run/r247/其他办法存档_r247.md。
    pwr_cond_frame = ttk.LabelFrame(
        cr_frame, text=i18n.tr('⚡ 电源条件（笔记本）',
                               '⚡ Power conditions (laptop)'),
        padding='6')
    pwr_cond_frame.pack(fill=tk.X, pady=(8, 0))
    _ac_only_var = tk.BooleanVar(value=False)
    _stop_bat_var = tk.BooleanVar(value=False)
    _ac_chk = ttk.Checkbutton(
        pwr_cond_frame, variable=_ac_only_var,
        text=i18n.tr('只在交流电（插着电源适配器）时才启动',
                     'Start only on AC power (plugged in)'))
    _ac_chk.pack(anchor=tk.W)
    # ★R248（用户 40 批 item0，win11 实测）：这枚"改用电池时停止"对上面那枚
    # "只在交流电才启动"有**依赖**——Windows 的规则里它只对"插电才启动"的任务
    # 有意义，单独勾它不生效。所以做成：上面没勾 ⇒ 这枚置灰且自动取消；
    # 上面勾上 ⇒ 这枚才可点。与任务计划程序"条件"页的联动一致。
    _sb_chk = ttk.Checkbutton(
        pwr_cond_frame, variable=_stop_bat_var,
        # ★R250 三3：措辞与编辑窗同一口径——这枚只管"正在跑的任务拔电要不要停"，
        # 管"到点能不能启动"的是上面那枚（用户 42 批把它读反了，说明旧文案
        # "停止该任务"确实容易读成"不让启动"）。
        text=i18n.tr('改用电池供电时，停止正在运行的任务（需先勾上面那枚）',
                     'Stop a RUNNING task when switching to battery '
                     '(needs the AC-only box ticked)'))
    _sb_chk.pack(anchor=tk.W)

    def _sync_pw_dep():
        if _ac_only_var.get():
            _sb_chk.configure(state='normal')
        else:
            _stop_bat_var.set(False)
            _sb_chk.configure(state='disabled')
    _ac_chk.configure(command=_sync_pw_dep)
    _sync_pw_dep()
    # ★R256（#220，用户 48 批三7 点头"再添加 2 个按钮"）：错过补跑 + 允许唤醒。
    # 这两枚对**所有**任务独立生效，不依赖"只在交流电"（与上面那对联动不同），
    # 所以插在联动函数之后、灰字分工说明之前，默认全不勾＝一切照旧。
    # 三态口径：只有勾了才写进任务；不勾**不会**把任务上已有的旗标改成关
    # （📝 编辑路不传这两个参数，见 _ps_power_cmd docstring）。
    _catch_var = tk.BooleanVar(value=False)
    _wake_var = tk.BooleanVar(value=False)
    _catch_chk = ttk.Checkbutton(
        pwr_cond_frame, variable=_catch_var,
        text=i18n.tr('错过计划后尽快补跑（电脑休眠/关机错过时间点，开机后马上'
                     '补运行一次）',
                     'Catch up missed runs (if the PC was asleep/off when the '
                     'time passed, run once right after boot)'))
    _catch_chk.pack(anchor=tk.W)
    _wake_chk = ttk.Checkbutton(
        pwr_cond_frame, variable=_wake_var,
        text=i18n.tr('允许唤醒计算机运行（到点把睡着的电脑叫醒干活）',
                     'Allow the scheduler to wake the computer for this run'))
    _wake_chk.pack(anchor=tk.W)
    # ★R271（用户 `----2026-9-29 02----` item1）：选「开机时」就把「错过补跑」
    # 自动勾上并说明原因——开机那一刻没有用户会话，交互式任务不补跑就永远
    # 不会自动运行（本机实读 StartWhenAvailable=False 证实）。用户可以取消勾选，
    # 但取消后创建时仍会被强制打开并在结果里写明（见 _create 里的 _cu_forced）。
    _boot_cue = ttk.Label(
        pwr_cond_frame, foreground=colors['text_secondary'],
        font=ui_layout.app_font(9), justify=tk.LEFT,
        wraplength=ui_layout.scaled_size(560))
    _boot_cue.pack(anchor=tk.W, pady=(2, 0))

    def _sync_boot_catch(*_a):
        # 不写 early-return：本函数夹在 `_create` 与 `_create_safe` 之间，
        # test_r251_create_ux C6 按行号扫"开窗之后的每条 return 前有没有收窗"，
        # 一个无关的裸 return 会被当成"漏收转圈窗"（假红）。if/else 一样清楚。
        if trig.get() == 'boot':
            _catch_var.set(True)
            _boot_cue.configure(text=i18n.tr(
                '⚠ 选的是「开机时」：已自动勾上「错过补跑」。开机那一刻你还没登录，'
                '系统只能等你登上桌面后马上补运行一次——不勾这条任务就永远不会自动跑。',
                '⚠ Trigger is "At startup": "Catch up missed runs" was turned on '
                'automatically. Nobody is logged on at boot, so Windows runs the '
                'task right after you log on — without it the task would never '
                'fire on its own.'))
        else:
            _boot_cue.configure(text='')

    trig.trace_add('write', _sync_boot_catch)
    _sync_boot_catch()
    ttk.Label(pwr_cond_frame,
              text=i18n.tr('两枚的分工：上面那枚管「到点能不能启动」，这枚管'
                           '「正在跑的任务拔电要不要停」。都不勾＝插不插电源都能'
                           '启动、跑着也不会中途停（笔记本建议这样，默认就是这样）'
                           '；下面两枚管「错过了要不要补跑」和「能不能把电脑叫醒」，'
                           '跟插不插电无关',
                           'The first controls whether a task may START on '
                           'battery; the second whether a RUNNING task gets '
                           'STOPPED. Both off = starts and keeps running on '
                           'battery (default, recommended for laptops); the '
                           'bottom two cover catching up missed runs and '
                           'waking the PC, independent of power source'),
              foreground=colors['text_secondary'],
              font=ui_layout.app_font(9), justify=tk.LEFT,
              wraplength=ui_layout.scaled_size(560)).pack(
        anchor=tk.W, pady=(2, 0))

    ctn = ttk.Frame(cr_frame)
    ctn.pack(fill=tk.X, pady=(8, 0))
    ttk.Label(ctn, text=i18n.tr('任务名自动生成（DeskBits_ 开头，可在此页禁用/删除）',
                                'Task name auto (DeskBits_*); disable/delete above'),
              foreground=colors['text_secondary'],
              font=ui_layout.app_font(9)).pack(side=tk.LEFT)

    def _create_safe():
        """★R207（用户 05批-2）：创建入口全程兜异常——此前 IMPORT 类异常
        在窗口态回调里无声死亡，按钮看起来"没有用"。任何异常都弹可见
        错误框并落日志。

        ★R251（07 批 item2a + item2e「➕ 创建 不明显，容易误会没点到」）：
        点下去的**第一件事**是改按钮自己——置灰 + 文案变「⏳ 正在创建…」，
        全程结束再还原。忙窗（_BusyWait）说明"在忙什么"，这一层回答
        "我到底点没点到"，顺带挡住连点叠建两条任务。异常出口在 finally 里
        再 `_busy_close` 一次（幂等），保证四条出口都不留转圈窗。"""
        try:
            btn_create.configure(state='disabled',
                                 text=i18n.tr('⏳ 正在创建…',
                                              '⏳ Creating…'))
            btn_create.update_idletasks()
            btn_create.update()
        except Exception:
            pass
        try:
            _create()
        except Exception as e:
            _busy_close(gui)
            logger.error(f"创建计划任务异常: {e}", exc_info=True)
            messagebox.showerror(
                i18n.tr('创建失败', 'Create Failed'),
                i18n.tr(f'创建过程出错：{e}', f'Error while creating: {e}'),
                parent=gui.root)
        finally:
            _busy_close(gui)
            try:
                btn_create.configure(
                    state='normal',
                    text=i18n.tr('➕ 创建', '➕ Create'))
            except Exception:
                pass

    # ★R251 item2e：➕ 创建 走 BigPrimary.TButton（比其它主按钮更大一号、
    # 实底粗体），并显式给 width——原来只靠文字撑，跟左边那串灰字挤在一排，
    # 用户读不出"这才是终点按钮"。
    btn_create = ttk.Button(ctn, text=i18n.tr('➕ 创建', '➕ Create'),
                            style='BigPrimary.TButton', width=14,
                            command=_create_safe)
    btn_create.pack(side=tk.RIGHT)
    # ★R220：说明按钮并入操作行（原来挂在创建器右上角，白占一行还没人看见）
    ttk.Button(ctn, text='❓', width=3, command=_help_create).pack(
        side=tk.RIGHT, padx=(0, 6))
