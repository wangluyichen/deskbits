# -*- coding: utf-8 -*-
"""📊 统计采集管理器（★ R31：从 gui/main_window.py 拆出——新功能分文件）。

职责：统计功能的"总开关"与三路记录入口，gui 只做薄接线。
- 开关状态持久化：app_settings['stats_capture_enabled']
- 默认值：源码运行开 / PyInstaller 打包（sys.frozen）关——历轮全局鼠标钩子
  是杀软误报高发点，分发版保守默认关（★ R256 起已改只读轮询、无钩子）
- 关闭 = 停掉鼠标采集线程（dock.stop_icon_capture）+ 三路记录全部短路

依赖注入（gui/__init__ 时组装）：
    dock           core.dock.DesktopDock   采集启停/分类点击入口都挂在 dock 上
    usage_stats    usage_stats.UsageStats  数据层（图标打开/文件夹打开）
    app_settings   dict（gui 持有的内存设置，可直接读写）
    save_settings  Callable                gui._save_app_settings
    log            Callable                gui._log（UI 日志）
    refresh_ui     Callable                统计页实时刷新投递（gui._ui_queue.put）

开发规范（用户 2026-09-13 拍板）：新功能一律独立分文件，main_window.py
不再增长功能逻辑（历史遗留体量今后专项治理）。
"""
import logging
import sys

logger = logging.getLogger("desktop_manager")

_SETTINGS_KEY = 'stats_capture_enabled'


def default_enabled() -> bool:
    """统计默认值：源码运行开 / 打包 exe 关（杀软友好）"""
    return not getattr(sys, 'frozen', False)


class StatsCaptureManager:
    """统计采集总开关 + 三路记录入口（图标打开/文件夹打开/分类切换）。

    分类切换的记录在 dock._handle_click 内部（dock 自查 stats_enabled），
    图标打开/文件夹打开经本类的 record_* 进入（gui 注入回调指向这里）。"""

    def __init__(self, dock, usage_stats, app_settings,
                 save_settings, log, refresh_ui):
        self.dock = dock
        self.usage_stats = usage_stats
        self._app_settings = app_settings
        self._save_settings = save_settings
        self._log = log or (lambda m: None)
        self._refresh_ui = refresh_ui or (lambda fn: None)

        self.enabled = bool(app_settings.get(_SETTINGS_KEY, default_enabled()))

    # ------------------------------------------------------------ 启停

    def apply_startup(self):
        """启动时同步 dock 开关并按需起采集线程（gui __init__ 调一次）"""
        self.dock.stats_enabled = self.enabled
        if self.enabled:
            self.dock.start_icon_capture()
        else:
            logger.info('[统计采集] 统计功能按设置为关闭（📊 统计页可一键打开）')

    def apply_settings(self):
        """★R240（#137）：按 app_settings 当前值**重新同步**运行态。

        为什么需要：`enabled` 只在构造时读一次、`apply_startup()` 只在启动调一次。
        「导入全部数据」改的就是 gui._app_settings 这同一个 dict，落盘了但没人
        把新值推给 dock/采集线程 ⇒ 用户观感"导出前统计是开的，导回来变成关的"。

        与 apply_startup 的三点区别：
          ① 值现读（不是用内存里那份旧 self.enabled）；
          ② **幂等**：已经是目标态就直接返回，绝不重复起第二个采集线程
             （★ R256 去钩子化后改轮询线程；重复起是 R130 踩过的
             "双线程消费同一队列、点击统计错乱"）；
          ③ **只读不回写**：不改 app_settings、不 _save_settings()（导入端已落盘，
             这里再存一遍会把导入侧的校验/回退逻辑绕开）。

        返回 True=运行态已与设置一致；False=想要开启但采集线程起不来
        （★ R256 去钩子化后已无"缺库"分支，只剩线程起不动的异常），
        此时 enabled 保持 False，不谎报"采集中"。"""
        want = bool(self._app_settings.get(_SETTINGS_KEY, default_enabled()))
        if want == self.enabled:
            return True
        if want:
            if not self.dock.start_icon_capture():
                # 起不动就保持关闭态：dock.stats_enabled 与 self.enabled 都不动，
                # 否则统计页会显示"采集中"却一条都不记
                logger.warning('[统计采集] 按设置开启失败（采集线程启动失败），'
                               '保持关闭')
                return False
            self.enabled = True
            self.dock.stats_enabled = True
            logger.info('[统计采集] 按设置开启采集（鼠标轮询已启动）')
        else:
            self.dock.stop_icon_capture()
            self.enabled = False
            self.dock.stats_enabled = False
            self._flush_stats()
            logger.info('[统计采集] 按设置关闭采集（鼠标轮询已停止）')
        return True

    def _flush_stats(self):
        """★R270-R3：把合并写窗口（15 秒）里攒的计数当场落盘，别让"关"变成丢账。

        `usage_stats` 按本类的依赖注入契约**可以为 None**（构造签名允许、`record_*`
        也都按这个口径包了 try），所以落盘前先看有没有数据层；没有就没什么可落的，
        但绝不能因为"关个统计开关"把 AttributeError 抛给调用方。"""
        if self.usage_stats is None:
            return
        try:
            self.usage_stats.flush()
        except Exception as e:
            logger.error(f'统计计数落盘失败: {e}')

    def toggle(self):
        """切换开关。返回 (ok, err_msg)：ok=False 时状态未变、err_msg 给 UI 弹指引。"""
        want = not self.enabled
        if want:
            if not self.dock.start_icon_capture():
                return False, ('鼠标采集线程启动失败（★ R256 起不再依赖任何'
                               '全局监听库）。\n请重启程序再试；仍不行请把日志'
                               '打包发我（日志页一键打包）。')
        else:
            self.dock.stop_icon_capture()
            self._flush_stats()   # ★R270-R3：关统计前把合并窗口内的账落盘
        self.enabled = want
        self.dock.stats_enabled = want
        self._app_settings[_SETTINGS_KEY] = want
        try:
            self._save_settings()
        except Exception as e:
            logger.error(f'统计开关状态保存失败: {e}')
        self._log('📊 统计功能已开启（图标/文件夹/分类切换均计入）' if want
                  else '⏸ 统计功能已关闭（不再监视鼠标，规则将基于历史数据）')
        logger.info('[统计采集] 开关切换为 %s', '开启' if want else '关闭')
        return True, ''

    # ------------------------------------------------------------ 记录入口

    def record_icon_open(self, name: str):
        """图标打开记录（★ R256 鼠标轮询线程经队列处理后回调）；统计页正打开时实时刷新"""
        try:
            if not self.enabled:
                return
            self.usage_stats.record_icon_open(name)
            self._refresh_ui()
        except Exception as e:
            logger.error(f'记录图标打开失败: {e}')

    def record_folder_open(self, path: str):
        """文件夹打开记录（explorer_tabs 回调）；统计页正打开时实时刷新"""
        try:
            if not self.enabled:
                return
            self.usage_stats.record_folder_open(path)
            self._refresh_ui()
        except Exception as e:
            logger.error(f'记录文件夹打开失败: {e}')

    # ------------------------------------------------------------ 联动文案

    def rule_hint_text(self) -> str:
        """规则页状态行文案（规则 top/bottom_clicked 依赖分类点击统计）"""
        if self.enabled:
            return '数据源：分类点击统计进行中（top/bottom_clicked 按实时点击数执行）'
        return ('⚠ 统计功能已关闭——点击数据不再更新，执行规则将基于历史数据'
                '（📊 统计页可重新开启）')
