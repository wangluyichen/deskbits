# -*- coding: utf-8 -*-
"""🎨 全局 ttk 样式增强层（★R211 简单美化，用户"目前太难看拿不出手"）。

设计约束（外部顾问方案①采纳 + 现状盘点修正）：
- ThemeMixin._setup_styles 已随主题切换重跑（R46 教训），本层只在该
  函数末尾调用一次 → 天然跟随 4 套主题，不另起炉灶。
- 只写样式表，零逻辑风险；颜色一律取调用方传入的 COLORS（原地更新
  的同一引用），不写 hex 字面量。
- 稳定性红线绕开：不碰 dock/锁屏/_log/emoji 哨兵字符串。
覆盖项 = 现有样式表的缺口：默认 TButton（全项目几十处裸 ttk.Button
此前仍是系统 3D 灰钮=观感"廉价"的最大单一来源）、hover/pressed 微
交互、TScrollbar 扁平化、TProgressbar、Treeview 行高（theme_panel.
TREE_ROWHEIGHT 单点常量）+hover 行。
★R215（UI 美化阶段1）：本层同时是按钮三档层级的 **ghost 档**（默认
TButton）终态定义处，并接住 Treeview 选中/hover 的最终 map——因为是
最后执行，这里写的值就是生效值，改任何默认样式必须以这里为准（R46
"改处不生效"陷阱）。
"""
from gui import ui_layout   # ★R228：样式字号走命名字体别名（界面字号全场缩放）


def apply(style, C):
    """在 ThemeMixin._setup_styles 末尾调用；C = theme_panel.COLORS。"""
    # ★R215（UI 美化阶段1）：按钮三档的 **ghost 档**在这一层落地——
    # 本函数是 _setup_styles 的最后一步，默认 TButton 的终态由它决定，
    # 因此全项目 ~66 处"不写 style= 的裸按钮"自动获得最低权重，
    # 主操作（primary/accent/warning/error 实底）与常规操作（secondary 描边）
    # 才立得住。字号 9→10、padding 与另两档一致，同排按钮才等高对齐。
    yahei = ui_layout.app_font(10)

    # ---- 默认 TButton = ghost：与所在容器同色、无边框，只在 hover/pressed
    #      时提亮（此前 background=surface＝裸按钮在卡片上是一块块亮补丁）----
    style.configure('TButton', font=yahei,
                    padding=ui_layout.pad(12, 6),
                    background=C['card_bg'],
                    foreground=C['fg'],
                    borderwidth=0,
                    relief='flat',
                    focuscolor=C['card_bg'])
    style.map('TButton',
              background=[('disabled', C['card_bg']),
                          ('pressed', C['card_border']),
                          ('active', C['hover'])],
              foreground=[('disabled', C['muted']),
                          ('pressed', C.get('on_primary', C['bg'])),
                          ('active', C['fg'])])

    # ---- ★R221（NEW7-P1-7）：实底四档必须自带 map，不能靠样式继承 ----
    # theme_panel 只给 primary/accent/warning/error 配了 configure（实底色），
    # 一处 map 都没写；ttk 渲染期沿 parent 链解析，于是这四档的 hover/pressed
    # 全命中上面 ghost 档的 map＝按下去先褪成卡片色再弹回来（R215 美化批引入，
    # 实测 `ttk::style lookup primary.TButton -background active` 就是 ghost 值）。
    def _shade(hexv, ratio):
        """按 ratio 明暗实底色；已到底/顶（同色）时反向挪一格，保证按下有反馈"""
        def mul(f):
            try:
                h = str(hexv).lstrip('#')
                if len(h) == 3:
                    h = ''.join(c * 2 for c in h)
                r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
                return '#%02x%02x%02x' % tuple(
                    max(0, min(255, int(round(c * f)))) for c in (r, g, b))
            except Exception:
                return str(hexv).lower()
        v = mul(ratio)
        return v if v != mul(1.0) else mul(2 - ratio)

    for _s, _ck in (('primary', 'primary'), ('accent', 'accent'),
                    ('warning', 'warning'), ('error', 'error'),
                    # ★R251 item2e：➕ 创建 用的大号主按钮（样式在
                    # theme_panel._setup_styles 定），底色同 primary；
                    # 不挂进本循环就会命中 ghost 档 map＝按下褪色（★R221）。
                    ('BigPrimary', 'primary')):
        _base = C[_ck]
        style.map('%s.TButton' % _s,
                  background=[('disabled', C['surface']),
                              ('pressed', _shade(_base, 0.82)),
                              ('active', _shade(_base, 1.18))],
                  foreground=[('disabled', C['muted'])])

    # ---- TScrollbar：扁平化（原生 3D 凸边+粗箭头=老气感的第二大来源）----
    style.configure('TScrollbar',
                    background=C['surface'],
                    troughcolor=C['bg'],
                    bordercolor=C['bg'],
                    arrowcolor=C['fg'],
                    lightcolor=C['surface'],
                    darkcolor=C['surface'],
                    relief='flat')
    style.map('TScrollbar',
              # ★R215：滑块的 hover 用 card_border（比 border 更实），
              # 轨道仍 bg——原来整条滚动条与内容同色，找不到可拖的东西
              background=[('active', C['card_border']),
                          ('pressed', C['primary'])],
              arrowcolor=[('active', C['fg'])])

    # ---- TProgressbar：细条主题色（此前默认绿色系与主题无关）----
    # ★R213（NEW5-F2）：对账——全项目当前 0 个 ttk.Progressbar 消费者
    # （下载面板进度是文本列）。此段保留为"预置"（未来加进度条即得主题），
    # 不再宣称"已生效"。
    style.configure('Horizontal.TProgressbar',
                    background=C['primary'],
                    troughcolor=C['surface'],
                    bordercolor=C['bg'],
                    lightcolor=C['primary'],
                    darkcolor=C['primary'])

    # ---- Treeview：行高 + hover 行提亮（clam 的 active 态）----
    # ★R213（NEW5-F3）：rowheight 单点化——此处与 theme_panel 曾分别
    # 硬写 24/22（双头定义、后执行者静默覆盖），现统一引用
    # theme_panel.TREE_ROWHEIGHT 唯一常量（R46 "改处不生效"陷阱同款）
    from gui.theme_panel import TREE_ROWHEIGHT
    style.configure('Treeview', rowheight=TREE_ROWHEIGHT)
    # ★R215：本层在 theme_panel 之后执行＝style.map 会整体替换同名选项，
    # 所以选中态必须在这里与 theme_panel 写一致（primary_soft），
    # 否则"选中行降饱和"在 theme_panel 里的改动会被这里静默覆盖回
    # 荧光青实底（R46 双头覆盖陷阱的 map 版本）。
    style.map('Treeview',
              background=[('selected', C['primary_soft']),
                          # hover 与常规底拉开一档才叫提亮：border 是控件
                          # 描边色（对比过强且语义错），改 hover 语义 token
                          ('active', C['hover'])],
              foreground=[('selected', C['fg']),
                          ('active', C['fg'])])

    # ---- TEntry 聚焦反馈：边框变主色（输入焦点一眼可见）----
    style.map('TEntry',
              bordercolor=[('focus', C['primary'])],
              lightcolor=[('focus', C['primary'])],
              darkcolor=[('focus', C['primary'])])
