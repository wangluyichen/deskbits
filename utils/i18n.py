# -*- coding: utf-8 -*-
"""双语支持框架（★ R73）：中文 / English。

用法（分模块逐步接入，未接入处保持中文原样，不会崩）：
    from utils import i18n
    i18n.set_language(settings.get('ui_language', '中文'))   # 构建界面前
    ttk.Button(text=i18n.tr('打开帮助', 'Open Help'))

- tr(zh, en)：当前语言为 English 返回 en，否则 zh。
- 语言持久化在 app_settings.json 的 ui_language 键（'中文'/'English'）；
  切换后需重启程序——Tkinter 界面是构建时求值，热切换要重造全部控件，
  按"重启生效"设计（切换弹窗已双语说明）。
- 语言切换控件本身双语（🌐 语言/Language: + 值 中文/English），
  两种语言的用户都能认得。
"""
import threading

LANG_ZH = '中文'
LANG_EN = 'English'
_LANGS = (LANG_ZH, LANG_EN)

_lock = threading.Lock()
_lang = LANG_ZH


def set_language(lang):
    """设置当前语言（非法值回退中文）。gui 启动读设置后、构建界面前调用。"""
    global _lang
    with _lock:
        _lang = lang if lang in _LANGS else LANG_ZH


def get_language():
    with _lock:
        return _lang


def is_en():
    with _lock:
        return _lang == LANG_EN


def tr(zh, en):
    """按当前语言取文案。"""
    with _lock:
        return en if _lang == LANG_EN else zh
