# -*- coding: utf-8 -*-
"""👶 家长模式面板（R147 自 gui/main_window.py 拆出，第六刀·Mixin 方案）。

KidsMixin = 家长模式全部操作方法（设置/清除密码与密保、进入/退出/到时、
忘记密码）+ _build_kids_tab（家长页 UI）；_KidsExitDialog/_ForgotDialog
两个对话框随迁。由 DesktopManager 继承——self. 语义零转换，无外部调用方。
纪律：**严禁 import gui.main_window**。
"""
import logging
from contextlib import contextmanager

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

from gui import ui_layout
from gui.ui_layout import SP
from utils import i18n
from quick_find import open_quick_finder   # 家长模式进出时关闭/清理快速定位框
from utils.app_paths import get_app_dir as _get_app_dir

logger = logging.getLogger("desktop_manager")

APP_DIR = _get_app_dir()

# 主题色引用：main_window 启动时 set_colors(COLORS) 注入**同一 dict 对象**
# ★R215：兜底 dict 补 text_secondary——本模块 :519 的提示文字用它
# （set_colors 未调用前的独立使用场景下不能 KeyError）
COLORS = {'bg': '#282a36', 'fg': '#f8f8f2', 'primary': '#8be9fd',
          'surface': '#44475a', 'border': '#636d83', 'warning': '#ffb86c',
          'muted': '#6272a4', 'secondary': '#bd93f9', 'error': '#ff5555',
          'kid': '#ff79c1', 'text_secondary': '#bcc3d6'}


def set_colors(colors: dict):
    global COLORS
    COLORS = colors


class _KidsExitDialog(tk.Toplevel):
    """退出家长模式：输入密码，可跳"忘记密码"走密保找回"""

    def __init__(self, parent, questions):
        super().__init__(parent)
        self.title(i18n.tr('家长模式', 'Parental Mode'))
        self.resizable(False, False)
        self.configure(bg=COLORS['bg'])
        self.result = None
        self.forgot = False
        ttk.Label(self, text=i18n.tr('退出需要家长密码:', 'Parental password required to exit:'),
                  background=COLORS['bg'], foreground=COLORS['fg']
                  ).pack(padx=16, pady=(14, 4))
        # tk.Entry（非 ttk）：显式指定底/字色，否则深色主题下 * 号几乎看不见
        self.pwd = tk.Entry(self, show='*', width=26,
                            bg=COLORS['surface'], fg=COLORS['fg'],
                            insertbackground=COLORS['fg'],
                            relief=tk.FLAT, highlightthickness=1,
                            highlightbackground=COLORS['border'],
                            highlightcolor=COLORS['primary'])
        self.pwd.pack(padx=16, ipady=3)
        row = ttk.Frame(self, style='TFrame')
        row.pack(pady=10)
        ttk.Button(row, text=i18n.tr('确定', 'OK'), command=self._ok).pack(side=tk.LEFT, padx=5)
        ttk.Button(row, text=i18n.tr('取消', 'Cancel'), command=self.destroy).pack(side=tk.LEFT, padx=5)
        # 忘记密码入口常显（未设密保时点击会提示先去设置）
        lbl = ttk.Label(self, text=i18n.tr('忘记密码？', 'Forgot password?'), cursor='hand2',
                        background=COLORS['bg'],
                        foreground=COLORS['primary'])
        lbl.pack(pady=(0, 12))
        lbl.bind('<Button-1>', lambda e: self._forgot())
        self.pwd.focus_set()
        self.bind('<Return>', lambda e: self._ok())
        # ★ 不能用 transient(主窗口)：家长模式下主窗口已 withdraw，
        #   transient 子窗口会跟着不显示——表现为"点退出没反应"
        self.lift()
        self.attributes('-topmost', True)
        self.grab_set()
        self.deiconify()
        self.focus_force()
        self.wait_window()

    def _ok(self):
        self.result = self.pwd.get()
        self.destroy()

    def _forgot(self):
        self.result = None
        self.forgot = True
        self.destroy()



class _ForgotDialog(tk.Toplevel):
    """密保找回：逐题作答，全部答对 verified=True"""

    def __init__(self, parent, questions, verifier):
        super().__init__(parent)
        self.title(i18n.tr('密保找回', 'Security Questions'))
        self.resizable(False, False)
        self.configure(bg=COLORS['bg'])
        self.verified = False
        self.verifier = verifier
        self.entries = []
        ttk.Label(self, text=i18n.tr('请回答全部密保问题：', 'Answer all security questions:'),
                  background=COLORS['bg'], foreground=COLORS['fg']
                  ).pack(padx=16, pady=(14, 6))
        for q in questions:
            # ★ R153：点问题行即聚焦输入框（用户建议，省一次点击）
            lbl = ttk.Label(self, text=q, background=COLORS['bg'],
                            foreground=COLORS['fg'], cursor='hand2')
            lbl.pack(padx=16, anchor=tk.W)
            # ★ 答案不遮蔽（show=''）：同 _on_set_questions
            e = tk.Entry(self, show='', width=28,
                         bg=COLORS['surface'], fg=COLORS['fg'],
                         insertbackground=COLORS['fg'],
                         relief=tk.FLAT, highlightthickness=1,
                         highlightbackground=COLORS['border'],
                         highlightcolor=COLORS['primary'])
            e.pack(padx=16, pady=(2, 8))
            lbl.bind('<Button-1>', lambda _ev, en=e: en.focus_set())
            self.entries.append(e)
        # ★ R153：回车=下一题（末题=提交）
        for _i, _e in enumerate(self.entries):
            _e.bind('<Return>', lambda _ev, i=_i: (
                self.entries[i + 1].focus_set()
                if i + 1 < len(self.entries) else self._ok()))
        row = ttk.Frame(self, style='TFrame')
        row.pack(pady=10)
        ttk.Button(row, text=i18n.tr('提交', 'Submit'), command=self._ok).pack(side=tk.LEFT, padx=5)
        ttk.Button(row, text=i18n.tr('取消', 'Cancel'), command=self.destroy).pack(side=tk.LEFT, padx=5)
        self.msg = ttk.Label(self, text='', background=COLORS['bg'],
                             foreground=COLORS['error'])
        self.msg.pack(pady=(0, 10))
        self.entries[0].focus_set()
        self.lift()
        self.attributes('-topmost', True)
        self.grab_set()
        self.deiconify()
        self.focus_force()
        self.wait_window()

    def _ok(self):
        answers = [e.get() for e in self.entries]
        try:
            ok = self.verifier(answers)
        except Exception:
            ok = False
        if ok:
            self.verified = True
            self.destroy()
        else:
            self.msg.configure(text=i18n.tr('答案不正确，请重试', 'Incorrect answer, try again'))



class KidsMixin:
    # ----------------------- 家长模式 -----------------------

    def _on_set_parental_pwd(self, skip_verify=False) -> bool:
        """设置/修改家长密码。返回 True=成功设置，False=取消/失败。

        skip_verify=True 时跳过原密码验证——用于「忘记密码」密保找回路径，
        用户本来就是因为忘了密码才来的，再要原密码完全不合理。"""
        if self.parental.has_password() and not skip_verify:
            p0 = simpledialog.askstring(i18n.tr('验证原密码', 'Verify Old Password'),
                                        i18n.tr('请输入当前密码:', 'Enter current password:'),
                                        show='*', parent=self.root)
            if p0 is None:
                return False
            if not self.parental.verify(p0):
                messagebox.showerror(i18n.tr('错误', 'Error'),
                                     i18n.tr('原密码不正确，无法修改', 'Old password is incorrect'),
                                     parent=self.root)
                return False
        # ★ R153：位数不足/两次不一致不再退出流程——原地循环重输
        #（取消才放弃）。用户实测：忘记密码答完密保后因位数不足直接退出，
        # 密保白答一遍。
        while True:
            p1 = simpledialog.askstring(i18n.tr('家长密码', 'Parental Password'),
                                        i18n.tr('请输入新密码（至少4位）:', 'Enter new password (at least 4 characters):'),
                                        show='*', parent=self.root)
            if p1 is None:
                return False
            if len(p1) < 4:
                messagebox.showwarning(i18n.tr('提示', 'Notice'),
                                       i18n.tr('密码至少 4 位，请重新输入（取消则放弃）',
                                               'Password must be at least 4 characters — please try again (Cancel to give up)'),
                                       parent=self.root)
                continue
            p2 = simpledialog.askstring(i18n.tr('家长密码', 'Parental Password'),
                                        i18n.tr('请再输入一次确认:', 'Confirm the new password:'), show='*',
                                        parent=self.root)
            if p2 is None:
                return False
            if p1 != p2:
                messagebox.showwarning(i18n.tr('提示', 'Notice'),
                                       i18n.tr('两次输入不一致，请重新输入（取消则放弃）',
                                               'The two entries do not match — please try again (Cancel to give up)'),
                                       parent=self.root)
                continue
            break
        self.parental.set_password(p1)
        self._log('🔐 家长密码已设置（加密存储，不保存明文）')
        # 密保问题：没有就引导设置；有了就询问是否更新
        if not self.parental.has_questions():
            if messagebox.askyesno(
                    i18n.tr('密保问题', 'Security Questions'),
                    i18n.tr('建议设置密保问题，忘记密码时可以找回。\n现在设置吗？',
                            'Security questions let you recover a forgotten password.\nSet them now?'),
                    parent=self.root):
                self._on_set_questions()
        elif messagebox.askyesno(i18n.tr('密保问题', 'Security Questions'),
                                 i18n.tr('是否同时更新密保问题？', 'Update security questions too?'),
                                 parent=self.root):
            self._on_set_questions()
        return True

    def _on_set_questions_btn(self):
        """密保问题按钮：已有密码时先验证，再设置/更新密保"""
        if self.parental.has_password():
            p0 = simpledialog.askstring(i18n.tr('验证密码', 'Verify Password'),
                                        i18n.tr('请输入当前家长密码:', 'Enter current parental password:'),
                                        show='*', parent=self.root)
            if p0 is None:
                return
            if not self.parental.verify(p0):
                messagebox.showerror(i18n.tr('错误', 'Error'),
                                     i18n.tr('密码不正确', 'Incorrect password'), parent=self.root)
                return
        self._on_set_questions()
        if self.parental.has_questions():
            messagebox.showinfo(i18n.tr('完成', 'Done'),
                                i18n.tr('密保问题已保存。\n忘记密码时可在退出框点"忘记密码？"找回。',
                                        'Security questions saved.\nUse "Forgot password?" in the exit dialog to recover.'),
                                parent=self.root)

    def _on_set_questions(self):
        """设置/更新 3 个密保问题（忘记密码时凭答案验证后免密退出）"""
        presets = [i18n.tr('你的手机号是？', 'Your phone number?'),
                   i18n.tr('你的常用邮箱是？', 'Your email address?'),
                   i18n.tr('你的家乡名字是？', 'Your hometown?')]
        qa = []
        for i in range(3):
            q = simpledialog.askstring(
                i18n.tr(f'密保问题 {i + 1}/3', f'Security Question {i + 1}/3'),
                i18n.tr(f'第 {i + 1} 个问题（可自行修改）:', f'Question {i + 1} (editable):'),
                initialvalue=presets[i], parent=self.root)
            if not q or not q.strip():
                return
            # ★ 密保答案不用遮蔽（show=''）：答案不是登录密码，遮蔽只会增加
            #   家长输错的概率（自己也看不见刚才填了啥）。孩子能看见问题
            #   但不知道答案，安全性足够。
            a = simpledialog.askstring(
                i18n.tr(f'密保答案 {i + 1}/3', f'Security Answer {i + 1}/3'),
                i18n.tr(f'「{q.strip()}」的答案:', f'Answer to "{q.strip()}":'),
                show='', parent=self.root)
            if not a or not a.strip():
                return
            qa.append((q, a))
        self.parental.set_questions(qa)

    def _on_clear_parental_pwd(self):
        """清除家长密码（分发前清理）：验证当前密码后恢复默认为空"""
        if not self.parental.has_password():
            messagebox.showinfo(i18n.tr('提示', 'Notice'),
                                i18n.tr('当前没有设置密码（默认为空）', 'No password is set (empty by default)'),
                                parent=self.root)
            return
        p = simpledialog.askstring(i18n.tr('清除密码', 'Clear Password'),
                                   i18n.tr('请输入当前密码以验证:', 'Enter current password to verify:'),
                                   show='*', parent=self.root)
        if p is None:
            return
        if not self.parental.verify(p):
            messagebox.showerror(i18n.tr('错误', 'Error'),
                                 i18n.tr('密码不正确，无法清除', 'Incorrect password, cannot clear'), parent=self.root)
            return
        self.parental.clear_password()
        self._update_kids_status()
        self._log('🧹 家长密码已清除（密保一并清除，恢复默认为空）')

    def _on_clear_questions(self):
        """清除密保问题（保留密码）：验证当前密码后执行。
        分发前清理个人数据用——把你的密保带走，密码保留给新用户。"""
        if not self.parental.has_questions():
            messagebox.showinfo(i18n.tr('提示', 'Notice'),
                                i18n.tr('当前没有设置密保问题', 'No security questions are set'),
                                parent=self.root)
            return
        if not self.parental.has_password():
            messagebox.showinfo(i18n.tr('提示', 'Notice'),
                                i18n.tr('还没设置密码，密码为空时密保也没意义',
                                        'No password set — security questions are pointless with an empty password'),
                                parent=self.root)
            return
        # 必须验证密码——不能让任何人随便把密保清了让你无法找回密码
        p = simpledialog.askstring(i18n.tr('清除密保', 'Clear Security Questions'),
                                   i18n.tr('请输入当前家长密码以验证:',
                                           'Enter current parental password to verify:'),
                                   show='*', parent=self.root)
        if p is None:
            return
        if not self.parental.verify(p):
            messagebox.showerror(i18n.tr('错误', 'Error'),
                                 i18n.tr('密码不正确，无法清除密保',
                                         'Incorrect password, cannot clear security questions'),
                                 parent=self.root)
            return
        if not messagebox.askyesno(i18n.tr('确认清除', 'Confirm Clear'),
                                   i18n.tr('清除后将无法通过密保自助找回密码。\n'
                                   '密码本身保留。继续？',
                                           'After clearing, the password can no longer be recovered via security questions.\nThe password itself is kept. Continue?'),
                                   parent=self.root):
            return
        self.parental.clear_questions()
        self._update_kids_status()
        self._log('🧹 密保问题已清除（密码保留）')
        messagebox.showinfo(i18n.tr('完成', 'Done'),
                            i18n.tr('密保问题已清除。密码保留，忘记密码时无法自助找回。',
                                    'Security questions cleared. Password kept — self-service recovery is no longer possible.'),
                            parent=self.root)

    def _on_enter_kids(self):
        """进入家长模式"""
        if self.parental.is_kids():
            messagebox.showinfo(i18n.tr('提示', 'Notice'),
                                i18n.tr('家长模式已在进行中', 'Parental mode is already running'))
            return
        if not self.parental.has_password():
            messagebox.showinfo(i18n.tr('首次使用', 'First Time'),
                                i18n.tr('请先设置家长密码（退出模式时需要）',
                                        'Set a parental password first (required to exit)'))
            self._on_set_parental_pwd()
            if not self.parental.has_password():
                return
        # ★R204 GUIB-13：非数字输入弹提示并中止（原静默取 0→max(1,·)=
        # 1 分钟，儿童模式几乎立刻到期且无任何提示）
        try:
            h = int(self.kids_hours_var.get() or 0)
        except (TypeError, ValueError):
            messagebox.showerror(i18n.tr('时长无效', 'Invalid duration'),
                                 i18n.tr('小时数必须是数字',
                                         'Hours must be a number'))
            return
        try:
            m = int(self.kids_minutes_var.get() or 0)
        except (TypeError, ValueError):
            messagebox.showerror(i18n.tr('时长无效', 'Invalid duration'),
                                 i18n.tr('分钟数必须是数字',
                                         'Minutes must be a number'))
            return
        if h < 0 or m < 0:
            # ★R207 NEW3-B6：负数输入不再静默坍缩成 1 分钟
            messagebox.showerror(i18n.tr('时长无效', 'Invalid duration'),
                                 i18n.tr('时长不能为负数',
                                         'Duration cannot be negative'))
            return
        minutes = max(1, h * 60 + m)   # 至少 1 分钟
        self.parental.enter(minutes)
        self._update_kids_status()
        self.root.withdraw()
        # 家长模式：强制显示高亮框（即使 frame_all=off）
        self.highlighter._kids_mode = True
        self.highlighter.notify_changed()
        logger.info("👶 进入家长模式，高亮框强制开启")
        self._log('👶 家长模式已开启，主窗口已隐藏（横条"退出"需密码）')

    @contextmanager
    def _kids_modal_parent(self):
        """★R215（NEW6-E2）：在家长模式里弹 `parent=self.root` 的原生对话框前用。

        进入家长模式时主窗是 withdraw 的（`_on_kids_start` 里
        `self.root.withdraw()`），而 messagebox/simpledialog 都挂 parent=root——
        父窗不可见时对话框弹不出来也点不到，表现为"答对密保后没反应"，
        孩子那边永久锁死。`_KidsExitDialog`/`_ForgotDialog` 当年就是为此
        改成非 transient + topmost 自建框（见 :67 注释），但**重设密码链**
        （密保通过 → askstring 输新密码 → 密保问题引导）仍走原生对话框。
        这里不重写那一长串对话框，改为"流程期间临时把主窗抬回前台、结束还原"：
        进出各判一次 `is_kids()`——若流程中途已免密退出（kids 变 False），
        就不再收回主窗，交给 `_on_kids_end` 正常 deiconify。
        非家长模式下本上下文零操作。
        """
        hidden = self.parental.is_kids()
        if hidden:
            try:
                self.root.deiconify()
                self.root.lift()
                self.root.focus_force()
            except Exception as e:
                logger.warning(f'密保找回流程临时显示主窗失败: {e}')
        try:
            yield
        finally:
            if hidden and self.parental.is_kids():
                try:
                    self.root.withdraw()
                except Exception:
                    pass

    def _on_kids_exit_request(self):
        """孩子在横条点了 [退出]：密码正确才放行；支持密保找回"""
        if not self.parental.has_password():
            # 未设置密码（默认为空）：不弹框直接退出
            self.parental.try_exit('')
            self._log('🔓 未设置密码，已直接退出家长模式')
            return
        dlg = _KidsExitDialog(self.root, self.parental.get_questions())
        if dlg.forgot:
            self._on_forgot_password()
            return
        pwd = dlg.result
        if pwd is None:
            return
        if self.parental.try_exit(pwd):
            self._log('🔓 密码正确，已退出家长模式')
            return
        # ★R215（NEW6-E2 同类）：kids 下 root 是 withdraw 的，"密码错误"提示
        # 原来同样弹不出来＝输错密码看着像"没反应"
        with self._kids_modal_parent():
            messagebox.showerror(i18n.tr('家长模式', 'Parental Mode'),
                                 i18n.tr('密码错误', 'Incorrect password'))
        self._log('⚠ 退出家长模式被拒绝：密码错误')

    def _on_forgot_password(self):
        """忘记密码：密保全部答对 → 先重设密码（成功才继续），再免密退出 kids mode。

        ★ 顺序很关键：exit_without_password 会清空 hash，若用户在新密码
        输入框里取消，密码就永久为空了（孩子趁机能绕过家长模式）。所以
        必须先重设成功再退出 kids mode。

        ★R215（NEW6-E2）：整段套进 `_kids_modal_parent()`——下面的提示框、
        重设密码的 askstring、密保问题引导框全挂 parent=self.root，
        kids 模式下主窗是隐藏的，不抬起来就等于流程卡死（本 bug 的实测表现：
        答对密保后"没有反应"）。"""
        was_kids = self.parental.is_kids()
        with self._kids_modal_parent():
            if not self.parental.has_questions():
                messagebox.showinfo(
                    i18n.tr('未设置密保', 'No Security Questions'),
                    i18n.tr('本程序没有设置密保问题，无法自助找回。\n'
                    '如需重置，请先退出当前家长模式（或清空家长数据）。',
                            'No security questions are set, so self-service recovery is unavailable.\nTo reset, first exit parental mode (or clear parental data).'),
                    parent=self.root)
                return
            dlg = _ForgotDialog(self.root, self.parental.get_questions(),
                                self.parental.verify_answers)
            if not dlg.verified:
                return
            # ★ 先重设密码（成功才继续），再退出 kids mode
            if not self._on_set_parental_pwd(skip_verify=True):
                self._log('ℹ️ 密保验证通过但用户取消了密码重设，密码保持不变')
                return
            if was_kids:
                self.parental.exit_without_password()
                self._log('🔓 密保验证成功且已设新密码，已免密退出家长模式')
            messagebox.showinfo(
                i18n.tr('验证成功', 'Verified'),
                i18n.tr('密保答案正确，密码已重设！', 'Answers correct — the password has been reset!') +
                (i18n.tr('\n已退出家长模式。', '\nParental mode exited.') if was_kids else ''),
                parent=self.root)

    def _on_kids_end(self):
        """家长模式结束（密码退出/到时锁屏）：恢复横条与状态显示"""
        self.dock.sync_slots(self.category_manager.get_all_categories())
        self._update_kids_status()
        # 退出家长模式：恢复正常高亮框行为
        self.highlighter._kids_mode = False
        self.highlighter.notify_changed()
        logger.info("🔓 退出家长模式，高亮框恢复正常行为")
        if not self.parental.is_kids():
            self.root.deiconify()

    def _update_kids_status(self):
        """刷新家长模式状态文字（定时刷新里也会调用）"""
        try:
            _pwd = (i18n.tr('密码: 已设置', 'Password: set') if self.parental.has_password()
                    else i18n.tr('密码: 默认为空', 'Password: empty (default)'))
            if self.parental.is_kids():
                remain = self.parental.remaining_seconds()
                m, sec = remain // 60, remain % 60
                self.kids_status_var.set(
                    i18n.tr(f'👶 进行中，剩余 {m}分{sec:02d}秒 后锁屏　·　{_pwd}',
                            f'👶 Running — screen locks in {m}m{sec:02d}s　·　{_pwd}'))
            else:
                self.kids_status_var.set(
                    i18n.tr(f'未开启　·　{_pwd}', f'Off　·　{_pwd}'))
        except Exception:
            pass

    def _on_open_readme(self):
        """★ R33：薄壳兼容保留——实现在 gui/about_page.open_readme"""
        from gui import about_page
        about_page.open_readme(APP_DIR, self._log)


    def _open_quick_find(self):
        """全局快速定位：搜索图标名任意片段，回车跳到桌面选中并方框提醒。

        ★ R151：方框改 quick_find 模块自绘（位置=实时读回的图标矩形），
        不再借用高亮框管线——缓存快照在 Explorer 重排后错位，曾"框到
        别的图标上"（用户实测）。"""
        try:
            open_quick_finder(self.root, self.icon_manager,
                              self.category_manager)
        except Exception as e:
            logger.error(f"打开快速定位失败: {e}")
            # ★R221（NEW7-P0-4）：原来只写 app.log。日志页才是用户看得见的
            # 地方——本轮"功能整条死掉但界面毫无反应"就是因为异常被这两层
            # try 吞成一行 logger.error（另一层在 hotkey_panel.start_poll）。
            try:
                self._log(f'✗ 快速定位打开失败：{type(e).__name__}: {e}')
            except Exception:
                pass

    def _toggle_dock(self):
        """切换悬浮窗显示"""
        self.dock.toggle_dock()



    def _build_kids_tab(self, tab_kids):
        """家长页 UI（R147 自 _build_ui 原样迁入）"""
        # ============ 👶 家长模式 ============
        kid_frame = ttk.LabelFrame(tab_kids, text=i18n.tr('👶 家长模式', '👶 Parental Mode'),
                                   padding='12 10', style='Kid.TLabelframe')
        kid_frame.pack(fill=tk.X)

        kid_inner = ttk.Frame(kid_frame)
        kid_inner.pack(fill=tk.X, pady=(0, 6))

        # ---- 第 1 行：状态 ----
        ttk.Label(kid_inner, text=i18n.tr('状态:', 'Status:')).pack(side=tk.LEFT, padx=(0, 4))
        self.kids_status_var = tk.StringVar(value=i18n.tr('未开启', 'Off'))
        self.kids_status_label = ttk.Label(kid_inner, textvariable=self.kids_status_var,
                                           foreground=COLORS['secondary'],
                                           font=ui_layout.app_font(11, 'bold'))
        self.kids_status_label.pack(side=tk.LEFT)

        # ---- 第 1.5 行：密码/密保（★R216 阶段 2b：独立小节 + action_bar）----
        # 5 枚按钮原先 pack(padx=8) 一排到底：长短不齐、"清除"类破坏性操作和
        # "设置/查询"混在一起看不出轻重。现在等宽分两排，清除类上 error 色。
        g_p, body_p = ui_layout.group_block(
            kid_frame, i18n.tr('🔐 密码与密保', '🔐 Password & Recovery'), 'accent')
        g_p.pack(fill=tk.X, pady=(SP['s'], 0))
        ui_layout.action_bar(body_p, [
            (i18n.tr('🔐 设置密码', '🔐 Set Password'), self._on_set_parental_pwd,
             'secondary.TButton'),
            (i18n.tr('❓ 密保问题', '❓ Security Questions'), self._on_set_questions_btn,
             'secondary.TButton'),
            (i18n.tr('🔑 忘记密码', '🔑 Forgot Password'), self._on_forgot_password,
             'secondary.TButton'),
            (i18n.tr('🧹 清除密保', '🧹 Clear Questions'), self._on_clear_questions,
             'error.TButton'),
            (i18n.tr('🧹 清除密码', '🧹 Clear Password'), self._on_clear_parental_pwd,
             'error.TButton')],
            cols=3, pad=SP['s'])

        # ---- 第 2 行：时长（自定义小时/分钟）+ 进入 ----
        g_k, body_k = ui_layout.group_block(
            kid_frame, i18n.tr('⏱️ 时长与进入', '⏱️ Duration & Enter'), 'primary')
        g_k.pack(fill=tk.X, pady=(SP['m'], 0))
        fk = ui_layout.form_block(body_k)
        self.kids_hours_var = tk.StringVar(value='1')
        self.kids_minutes_var = tk.StringVar(value='0')
        ui_layout.form_row(fk, 0, i18n.tr('时长:', 'Duration:'),
                           ttk.Spinbox(fk, from_=0, to=99, width=3,
                                       textvariable=self.kids_hours_var),
                           ttk.Label(fk, text=i18n.tr('时', 'h')),
                           ttk.Spinbox(fk, from_=0, to=59, width=3,
                                       textvariable=self.kids_minutes_var),
                           ttk.Label(fk, text=i18n.tr('分', 'min')))
        ui_layout.action_bar(body_k, [
            (i18n.tr('👶 进入家长模式', '👶 Enter Parental Mode'),
             self._on_enter_kids, 'primary.TButton')])

        ttk.Label(kid_frame,
                  text=i18n.tr('进入家长模式后：悬浮条只剩小朋友分类，到时自动锁屏；退出需密码。\n'
                               '小朋友分类的图标可在"📁 管理"里编辑（🤖 常驻 标记的分类不会被切走）。',
                               'In parental mode the bar shows only the Kids category '
                               'and auto-locks when time is up; exiting needs a password.\n'
                               'Edit Kids icons on the "📁 Files" tab (categories marked '
                               '🤖 Resident are not switched away).'),
                  foreground=COLORS['text_secondary'],
                  font=ui_layout.app_font(9), justify=tk.LEFT).pack(
            anchor=tk.W, pady=(SP['s'], 0))

