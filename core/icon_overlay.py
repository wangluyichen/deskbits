"""桌面图标框选 overlay（分层窗口 + 色键透明 + GDI 直绘）

机制（2026-09-04 定案）：
1. WS_EX_LAYERED + SetLayeredWindowAttributes(LWA_COLORKEY)：黑色像素透明
2. 绘制 = GetDC(hwnd) 后直接 GDI 画框——老 overlay 多日验证过的可靠路径
3. WS_EX_TRANSPARENT 点击穿透，不影响桌面操作

不使用 UpdateLayeredWindow 的原因（2026-09-04 实测定案）：
- create 里先调 SLWA 后，ULW 一律 err=87（模式互斥）
- 不调 SLWA 时 ULW 虽返回成功，但内容不上屏（源位图为 DDB 时静默无效）
- SLWA + 窗口 DC 直绘是本机长期稳定工作的机制

形状支持：圆角立体/圆角/直角/双线/下划线/U形/四角，虚线开关。
"""

import ctypes
import logging

import win32gui
import win32con
import win32api

logger = logging.getLogger("desktop_manager")

_gdi32 = ctypes.windll.gdi32

# ★R334-a：ctypes 不声明原型时按 c_int 返回，64 位句柄的高位会被砍；实参不声明
# argtypes 时，一枚 64 位宽的句柄喂给同一 DLL 的另一个函数会直接
# OverflowError（本机实测：声明后 CreateFontW 回 0xFFFFFFFF87F0xxxx，未声明的
# DeleteObject 收到它就炸在参数转换上）。下面把本文件用到的原型逐个钉住；
# Create*W 的字体名也顺手钉成 c_wchar_p（*W 函数吃 ANSI 字节是另一路坑）。
_gdi32.CreateBitmap.restype = ctypes.c_void_p
_gdi32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint,
                               ctypes.c_uint, ctypes.c_void_p]
_gdi32.CreatePatternBrush.restype = ctypes.c_void_p
_gdi32.CreatePatternBrush.argtypes = [ctypes.c_void_p]
_gdi32.CreateHatchBrush.restype = ctypes.c_void_p
_gdi32.CreateHatchBrush.argtypes = [ctypes.c_int, ctypes.c_uint]
_gdi32.CreateFontW.restype = ctypes.c_void_p
_gdi32.CreateFontW.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, ctypes.c_uint,
                               ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,
                               ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,
                               ctypes.c_uint, ctypes.c_wchar_p]
_gdi32.DeleteObject.restype = ctypes.c_int
_gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
_gdi32.Ellipse.restype = ctypes.c_int
_gdi32.Ellipse.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                           ctypes.c_int, ctypes.c_int]
_gdi32.PatBlt.restype = ctypes.c_int
_gdi32.PatBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int, ctypes.c_int, ctypes.c_uint]
_gdi32.RoundRect.restype = ctypes.c_int
_gdi32.RoundRect.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int]

# c_void_p 把 NULL 读成 None（旧口径 c_int 读成 0）：创建失败统一折算回 0，
# 让后面每一发 SelectObject 的行为与修前逐字一致。


def _handle_or_0(val):
    return val if val else 0


# ★R273（批29 B5）色键窗没有逐像素 alpha ⇒ "半透明"只能用 8×8 有序抖动
# （Bayer）点阵伪装：着色点占比＝"覆盖率"，其余点是纯黑＝色键＝桌面透出。
_BAYER8 = (
    (0, 32, 8, 40, 2, 34, 10, 42),
    (48, 16, 56, 24, 50, 18, 58, 26),
    (12, 44, 4, 36, 14, 46, 6, 38),
    (60, 28, 52, 20, 62, 30, 54, 22),
    (3, 35, 11, 43, 1, 33, 9, 41),
    (51, 19, 59, 27, 49, 17, 57, 25),
    (15, 47, 5, 37, 13, 45, 7, 39),
    (63, 31, 55, 23, 61, 29, 53, 21),
)
# 新四档的旋钮集中在这一块（用户 item4："字最好看清楚一点点，具体先做出来
# 再调试" ⇒ 下批调观感只改这几个数，不再动绘制结构）
MARKER_COVER = 0.12       # 荧光笔色条覆盖率（★R273 真绘制调参 22%→12%；
                          # ★S6 返修：步长错时这个数根本没生效（实画 52.6%），
                          # 修后 G1 钉住"实测==旋钮"）
MARKER_INSET = 8          # 色条两端内缩，不压到相邻图标格
MARKER_BOTTOM = 4         # ★R274 色条离格底多高（越大越往下、越不碰字）
BLOCKRING_COVER = 0.40    # 色块纱环覆盖率
ZONE_GAP_PX = 16          # 网格分行/分列容差**下限**（同列读数只差 1~2 px；
                          # ★R278-C：还要和格子自身尺寸取大，见 zone_clusters）
ZONE_LABEL_PAD = 7        # 分区标签左右内边距
ZONE_LINK_PX = 28           # ★R274 两格并团的**真实净空**上限（索引挨着不算邻居）
ZONE_LINK_RATIO = 0.55      # ★R278-C 净空余量按格子自身尺寸给的系数：真机实测
                            # 图标格 74×72、横距 75⇒净空 1px，纵距 98⇒净空 26px，
                            # 光靠 28 只剩 2px 余量，125% 缩放（净空 32）起竖排必断
ZONE_LABEL_COVER = 0.72     # ★R274 无处可挪时骑线名牌的抖动覆盖率（<1=纱）
ZONE_LABEL_TEXTONLY = True  # ★R274 True=骑线档改画描边文字（完全不铺底）
# ★R279-①（产品负责人 09-30 04 批 item1「如果可以多个设置，或者定制，多少
# "图标并团"，这个是不是更好些呢」）：并团做成四档，用户自己定"隔多远还算一伙"。
# 那一版的旋钮是倍率（'紧'0.5 / '中'1.0 / '松'1.6 / '很松'2.6），★R281-4 换成
# "空几格"后**余量仍然随格子尺寸走**（★R278-C 那条口径没变：写死像素在 125%/175%
# 缩放上一档就废），变的只是"怎么数"——从形容词乘系数，改成数中间空了几格图标。
# ★R281-4（产品负责人 09-30 08 批 item4「这种概念不够准确，直接按照图标隔开来计数
# 好理解点……按照主流软件的概念来设计」）：档位从**倍率**改成**空几格**。
# 为什么非改不可（取证 run/r281/_probe_tight.txt，六套夹具 × 旧四档的实测团数）：
#   旧旋钮把同一个像素阈值拿去比 `max(dx,dy)`，而真机横排邻格净空只有 1px、竖排 26px，
#   于是"横排·挨着"四档全是 1 团（旋钮零作用＝用户原话"横排没有体现"）、
#   "松(63px)"与"中(39px)"六套夹具逐档同值（四档实际只有两种行为）、
#   而"隔开一个图标"（横 76 / 竖 124）旧表里连"很松(102)"都只够到横排那一半——
#   竖排那一半压根进不了比较：`zone_clusters` 只把**索引±1**当候选，隔一格的
#   两格索引差 2，倍率放再大也没用。⇒ 光改文案救不了这个概念，档名、换算、
#   候选窗口三处一起改（窗口见 `zone_clusters` 里的 `span`）。
# 值＝中间允许空几格图标位，0 就是"挨着"。默认档 '挨着' 的算式与 ★R278-C **逐字同值**
# （闸 Z1b 逐格宽 0~240 比对）⇒ 没动过这枚旋钮的老用户看到的还是原来那个世界。
ZONE_TIGHTNESS = {'挨着': 0, '隔1格': 1, '隔2格': 2, '隔3格': 3}
ZONE_TIGHT_DEFAULT = '挨着'
# ★R279-① 那四档旧名按**实测行为**归队，不按名字像不像：
#   中(1.0)=39px 与 松(1.6)=63px 在六套夹具上给出的团数逐档相同 ⇒ 同归 '挨着'；
#   很松(2.6)=102px 会把"横排隔1格"（净空 76）并进来 ⇒ 归 '隔1格'；
#   紧(0.5)=19px 唯一效果是把竖排邻格（净空 26）拆断——"只连横排不连竖排"不是
#   "隔几格"这种数法能表达的档，旧存档里它落到 '挨着'（＝竖排邻格重新开始团一起，
#   团得更少而不是更多；这一条不是无损迁移，如实写进工作进度 ★R281）。
ZONE_TIGHT_LEGACY = {'紧': '挨着', '中': '挨着', '松': '挨着', '很松': '隔1格'}
# ★R288（批44，用户 2026-10-03 点名"把色块纱环/荧光笔/角贴这种样式也支持到分区，
# 这样分区的选择更多、更好看，不要把原来的功能丢掉"）：分区的**画法**四档，
# 顺序就是下拉顺序。默认档 '包络框' ＝ ★R282-4 起的现行渲染，**逐像素不变**
# （闸 A1/A1c 两头钉：没挂属性 ≡ 默认档 ≡ 修前那段代码还在）。
# 其余三档把逐图标那三档调好的画法按**整团**画：
#   色块纱环＝团外沿一圈抖动纱 + 中间真透明（色键黑）；
#   荧光笔＝团内**每一行**的字下沿一条带（一行一条，不糊第一行的字）；
#   角贴＝每格一枚角点 + 团右上角**一**枚名牌（头顶那块带牌这一档不画——
#         用户原话"只画角上那一枚"，两块都画就是一团上两枚重复牌）。
# ★R288 时那句"只有 '包络框' 读 形状/线宽/样式"到 ★R293-③ 已作废（下面那段重写）：
# 现在每档吃哪几味看 core.highlighter.ZONE_PAINT_CAPS，面板按那张表逐档灰。
# ★R289（批45，用户 2026-10-03 点名"目前 4 个不多，弄点有创新的样式出来"）：
# 往后**追加**四档创新画法（老四档的渲染码一段没动，闸 B1 拿修前基线树逐字节比；
# ★R293-③ 之后这句只对 包络框/角贴 两档还成立——纱环与荧光笔各加了一味线宽/形状读取）。
# "创新"受一条物理约束：色键窗没有逐像素 alpha ⇒ 半透明/发光/纹理只能靠点阵或
# 图案刷伪装，这四档就是在这条约束里做文章：
#   取景角＝团四角各一枚**实心 L 形括号**，中间与四条边全透明（遮挡最小的"框住了"，
#           区别于 包络框·四角 那档的 1~2px 描线）；
#   双线框＝外一枚 + 内一枚**同色**分类色，**两线之间留一条透明缝**（比单线更"钉住"；
#           ★R290：内线原来是近黑 1px，深色壁纸下看不见 ⇒ 同色 + 缝 8px + 内线 2px）；
#   斜纹带＝荧光笔那一条行底带，带内换成 45° **斜纹图案刷**（比荧光笔重一档、
#           比纱环有方向感，多行团每行都有）；
#   柔光晕＝沿团边向内**三条递减密度**（0.36/0.22/0.09）的抖动带 + 中心挖真透明
#           （最接近"发光"，包围盒与其余档完全一致 ⇒ 切画法不会让团长大一圈）。
# 新四档统统不读 形状/线宽/样式（自带笔/自带图案）⇒ 面板把那三味灰掉。
# ★R293（批49，用户 ③-2/③-3）：① 上面那句**只有 角贴 还成立**——三味控件已经从
# "只有包络框吃"打通到其余各档（换算式见 `_zk`：派生量＝基准量×线宽÷2 ⇒ 默认线宽 2
# 逐字节等于旧画面；形状只认 '直角' 这一味，其余一律按今天的圆角画）；② 画法表
# 加两档"细"预设（细双线框/细柔光晕），治的是"太抢眼"（遮挡 21.9%/14.5% 那两档）。
# 唯一仍不读那三味的是 角贴：它借用逐图标那枚 `_dot`，而动 `_dot` 等于同时动
# "角贴显示模式"那条链（★R291 挂账 5），本轮不拆开 ⇒ 只挂账。
ZONE_STYLES = ('包络框', '色块纱环', '荧光笔', '角贴',
               '取景角', '双线框', '斜纹带', '柔光晕',
               '细双线框', '细柔光晕')
ZONE_STYLE_DEFAULT = '包络框'
# ★R289 新四档的旋钮（集中一处，下批调观感只改这几个数，不动绘制结构）
BRACKET_ARM_MIN, BRACKET_ARM_MAX = 12, 26   # 取景角臂长按团短边 1/6 给，夹在这两头
BRACKET_T = 4                               # 取景角括号粗细（实心，不是描线）
DOUBLE_GAP = 8                              # 双线框内外两线间距（★R290：5→8，缝才读得出）
DOUBLE_W_OUT, DOUBLE_W_IN = 2, 2            # 两枚线同粗同色（★R290：内线不再近黑，见 _zone_double）
HATCH_STYLE = 5                             # 斜纹带图案：5=HS_DIAGCROSS（45° 交叉纹）
GLOW_BANDS = ((0, 0.36), (4, 0.22), (8, 0.09))   # 柔光晕三条带（内缩, 抖动覆盖率）
GLOW_DEPTH = 12                            # 三条带总深＝内缩到这一格就挖透明洞
# ★R293-②（批49，用户 ③-2）：两档"细"预设。只加这几个数，绘制结构一段不动
# （`_zone_double`/`_zone_glow` 各多一枚 thin 旗标读这两组）：细双线框缝 8→5、
# 两枚线各 2→1；细柔光晕三条带 4px→3px、密度 .36/.22/.09→.30/.16/.06、总深 12→9。
# "抢眼"是要被量出来的，不是嘴上说：闸 R2 拿同一包围盒数非黑像素，细档必须严格更少，
# 同时"双"仍读得出（扫描线仍两段）、三条带仍**挨着铺**（0/3/6 + 总深 9，等差）。
DOUBLE_GAP_THIN = 5
DOUBLE_W_THIN = 1
GLOW_BANDS_THIN = ((0, 0.30), (3, 0.16), (6, 0.06))
GLOW_DEPTH_THIN = 9
# "一格"在横竖两轴上不是一样宽：★R278-C 同次真机 100% 实测图标格 74×72，
# 横排格距 75（⇒净空 1px）、竖排格距 98（⇒净空 26px）——比值才是稳定的量，
# 像素值随 DPI 走。125%/175% 复核同一比值：90→122（1.356）、126→171（1.357），
# 与 98/72=1.361 同档 ⇒ 竖排用格高×1.361 换算格距，横排用格宽×1.014。
ZONE_PITCH_RATIO_X = 75 / 74
ZONE_PITCH_RATIO_Y = 98 / 72
_RIM = (35, 38, 45)       # 描边文字用的近黑轮廓（不能取纯黑：那是色键=透明）
# ★R291（批 47，用户 ③-1"把描边也提亮"）：角标圆点那一圈 1px 原来也吃 `_RIM`，
# 深色壁纸实测底色 (2,14,30) 下最大通道差仅 33/255 ⇒ 包络框/角贴两档"看得见/不透明"
# 比只有 0.57（其余六档 1.00）。这枚中性灰对深色差 148、对浅色 (238,240,244) 差 88，
# 两张壁纸都看得见；三通道留在 [40,215] 区间——再亮就在浅色壁纸上糊成一团白。
_DOT_RIM = (150, 155, 165)


def _zk(base, pen_width):
    """★R293-③ 唯一换算口径：**派生量 = 基准量 × 线宽 ÷ 2**（整数除）。

    为什么是"÷2"而不是"×线宽"：面板的默认线宽就是 2，`base*2//2 == base` 是一条
    整数恒等式 ⇒ "打通三味控件"这件事**不需要**改动今天的默认画面（零漂移的证据是
    这条等式加闸 R1 的逐像素比对，不是"看起来差不多"）。线宽 1 减半、8 翻四倍。
    各画法里原本写死的几何量（环宽 / 带高 / 括号粗 / 缝宽 / 光晕内缩与总深）都走这里；
    虚线与直角各有各的语义，不在这个函数里掺，由调用方自己判。
    """
    return int(base) * max(1, int(pen_width or 1)) // 2


def _glow_inset_cap(w, h):
    """★R319 K13：柔光晕往里最多能缩多少（短边一半留 2px）。

    `RoundRect(x0+d, y0+d, x1-d, y1-d)` 只要 `2d >= 短边` 就左右/上下**颠倒**，而 GDI
    不报错、照画一枚错位带 ⇒ 中心透明核不再对着洞心。真桌面最小的团是落单那一格
    （★R278-C 实测 74×72），线宽 6 起洞深 36 就越过半格 35，线宽 7~8 更甚。
    同族双线框早有 `_double_inner_fits` 守卫，这一侧一直到本批才有。
    默认线宽 2 时洞深 12、三条带 0/4/8 全都够不着这枚上限 ⇒ 今天的画面一格不动。
    """
    return max(0, (min(int(w), int(h)) - 2) // 2)


def _shape_is_square(shape) -> bool:
    """★R293-③：「形状」这一味对非包络档**只认 '直角' 一种取值**。

    其余取值（圆角/圆角立体/双线/四角/下划线/U形）在包络框那档有各自的画法，但"整团
    一圈抖动填充"没有对应的方角语义 ⇒ 一律按今天的圆角画。做成函数（而不是散在各档里的
    `== '直角'`）是为了让门禁直接钉真值表：认领集说吃形状，就得真的改画面。
    """
    return shape == '直角'


def _double_inner_fits(w, h, gap=DOUBLE_GAP):
    """★R290 双线框：这团套不套得下第二圈（短边判据，做成函数好让门禁直接钉数字）。

    真桌面最小的团是落单那一格（★R278-C 实测 74×72 @100%），门槛 2*DOUBLE_GAP+2=18
    ⇒ 正常团一律套得下；只有异常窄的团才退回"只画外圈"。
    ★R293：gap 变成参数（细档 5、线宽一缩放就更窄），默认仍 DOUBLE_GAP=8 ⇒ 老档
    判据逐字不变，但"套不套得下"从此跟着**实际缝宽**走，不再拿固定值去判缩过的几何。
    """
    return w > 2 * gap + 2 and h > 2 * gap + 2

def _rect_gap(a, b):
    """两个图标格的**真实净空**：两轴都重叠⇒0，否则取间隙较大的那一轴。"""
    dx = max(a[0] - (b[0] + b[2]), b[0] - (a[0] + a[2]))
    dy = max(a[1] - (b[1] + b[3]), b[1] - (a[1] + a[3]))
    return max(dx, dy, 0)


def _link_allow(a, b, link_px=ZONE_LINK_PX, tight=ZONE_TIGHT_DEFAULT):
    """两格可否并团的**净空余量**（★R278-C 的相对余量 + ★R281-4 的"空几格"换算）。

    '挨着'（0 格）走 ★R278-C 原式：取「像素下限」与「格子自身尺寸的
    ZONE_LINK_RATIO 倍」的较大者——逐字同值，闸 Z1b 拿格宽 0~240 全比过，
    所以不改设置的老用户画面一个像素都没变。

    空 N 格（N≥1）按**分离那一轴**换算：中间空 N 格时净空 = (N+1)×格距−格宽，
    而格距 = 该轴格尺寸 × ZONE_PITCH_RATIO_*（实测比值，随 DPI 一起放大）。
    取 (N+1.5)×格距−格宽＝落在"空 N 格"与"空 N+1 格"两条缝的正中间偏下一点，
    既不越档、又给手摆图标留出 37~50px 的抖动余量（闸 M2f 逐档核这两条边界）。
    为什么要分轴：横排净空实测 1px、竖排 26px，同一个标量阈值乘系数在横排上
    四档全连、在竖排上只有两档有分别——那枚旋钮就白做了（run/r281/_probe_tight.txt）。"""
    own = min(a[2], a[3], b[2], b[3])
    base = int(max(link_px, ZONE_LINK_RATIO * own))
    n = zone_tight_level(tight)
    if n <= 0:
        return base
    dx = max(a[0] - (b[0] + b[2]), b[0] - (a[0] + a[2]))
    dy = max(a[1] - (b[1] + b[3]), b[1] - (a[1] + a[3]))
    if dx >= dy:      # 分离轴＝间隙较大的那一轴，与 _rect_gap 同一口径
        size, step = max(a[2], b[2]), ZONE_PITCH_RATIO_X
    else:
        size, step = max(a[3], b[3]), ZONE_PITCH_RATIO_Y
    return max(base, int((n + 1.5) * size * step - size))


def zone_tight_resolve(name):
    """档名（含 ★R279-① 的旧倍率档名）→ 当前档名。认不得的回默认档：
    坏存档不该打死绘制，旧存档也不该被静默读成"没设置"。"""
    key = ZONE_TIGHT_LEGACY.get(name, name)
    return key if key in ZONE_TIGHTNESS else ZONE_TIGHT_DEFAULT


def zone_tight_level(name):
    """档名 → 中间允许空几格（旧档名先折叠再取值）。"""
    return ZONE_TIGHTNESS[zone_tight_resolve(name)]


# ★R280-B（用户 06 批 item3「界面无量化提示」）：把"这一档到底是几像素"给界面看。
# 参考格 = ★R278-C 那次真机 100% 实测的图标格尺寸，两条缝也是同一次实测读出来的
# （竖排邻格净空 26 = 纵距 98 − 格高 72；跳过一整行 124 ≈ 1.7 倍格高）。
# 它们是**提示用的参照系**，不是渲染输入——渲染永远读每格自己的 w/h。
ZONE_REF_CELL = (74, 72)
ZONE_REF_GAP_COL = 26     # 竖排相邻两格的缝（纵距 98 − 格高 72）
ZONE_REF_GAP_H = 1        # ★R281-4：横排相邻两格的缝（横距 75 − 格宽 74）——
                          # 这条就是"横排看不出松紧"的根源：1px 谁都比得上，
                          # 所以旧表四档在横排上全是同一张画面。提示里要如实分开说。
ZONE_REF_GAP_ROW = 124    # 竖排跳过一整行的缝（2×98 − 72）＝"空 1 格"


def zone_margin_px(tight, cell=ZONE_REF_CELL):
    """档位 → 参考格下的余量像素（**竖排那一轴**，两轴中较宽的一条）。
    走的是 `_link_allow` 这**同一个**算法出口（合成两条同尺寸的格即可，它只读 w/h），
    不在界面里另写一遍公式——那样一改设置就两处对不上。横排那条要一起说时读
    `zone_link_bounds`。"""
    a = (0, 0, cell[0], cell[1])
    return _link_allow(a, a, tight=tight)


def zone_tight_table():
    """[(档位名, 中间空几格, 竖向余量px)]，顺序就是下拉的顺序（ZONE_TIGHTNESS 的键序）。"""
    return [(k, ZONE_TIGHTNESS[k], zone_margin_px(k)) for k in ZONE_TIGHTNESS]


def zone_link_bounds(tight, cell=ZONE_REF_CELL):
    """档位 → (横排余量px, 竖排余量px)。★R281-4 起两轴不是一样的数（格距比不同），
    提示里要说"横着最多多少、竖着最多多少"，只给一个数就是骗人。
    两轴都走 `_link_allow` **本身**（合成一横一竖两对格），界面里不另写公式。"""
    w, h = cell
    ax = (0, 0, w, h)
    return (_link_allow(ax, (w, 0, w, h), tight=tight),
            _link_allow(ax, (0, h, w, h), tight=tight))


def _size_tol(vals, floor):
    """坐标归并成列/行号的容差：同样按该轴格子尺寸给（治手摆竖排的 x 抖动）。"""
    if not vals:
        return floor
    s = sorted(vals)
    return max(floor, int(0.35 * s[len(s) // 2]))


def zone_clusters(items, link_px=ZONE_LINK_PX, gap_px=ZONE_GAP_PX,
                  tight=ZONE_TIGHT_DEFAULT):
    """把同分类、且**真实净空 <= 余量** 的图标格并成团（★R274 取代"索引相邻"，
    ★R278-C 把余量与列/行容差都改成随格子自身尺寸走，
    ★R279-① 余量按"并团松紧"档位走，★R281-4 候选窗口也跟着档位放宽）。

    历轮 _axis_index 把 x/y 压成连续组号后就把间距信息丢了：BFS 只看组号
    正负一，于是同分类中间空 200px 的两格照样并成一整条巨框（桌面摆得稀疏
    时"分区"会跨过空白圈进不相关的图标，实测巨框宽 693px）。现在组号只用来
    把候选收窄到**同轴 ±(1+空几格)** 的邻域，**是否同团只问真实净空**。
    为什么窗口要跟着档位走：组号只数"被占住的坐标值"，所以中间空 1 格的两格
    组号只差 1、空 2 格也只差 1——窗口写死 ±1 时"隔 N 格"的两格**永远进不了
    比较**，倍率放再大也白放（★R281-4 实测：竖排隔 1 格四档全是 6 团）。
    组号距离 ≤ 实际隔的格数+1 ⇒ ±(1+N) 是"空 N 格"的可靠超集，多出来的候选
    由净空判据挡回去，不会把 ★R274 的巨框放回来。对角仍不算邻居（只认同轴）。
    纯函数、不碰 GDI ⇒ 门禁可以直接喂夹具断言团数。"""
    if not items:
        return []

    # ★R278-C：手摆的竖排同列 x 能差 20~25px，固定 16px 容差会把它们拆成两列，
    # 于是纵向永远进不了 4 邻域⇒竖排不连。容差按该轴中位格尺寸给。
    tol_x = _size_tol([it[2] for it in items], gap_px)
    tol_y = _size_tol([it[3] for it in items], gap_px)
    span = 1 + zone_tight_level(tight)   # '挨着'⇒±1（与历轮同形），隔 N 格⇒±(N+1)

    def _axis_index(axis, tol):
        vals = sorted({it[axis] for it in items})
        m, grp, prev = {}, 0, None
        for v in vals:
            if prev is not None and v - prev > tol:
                grp += 1
            m[v] = grp
            prev = v
        return m

    ci, ri = _axis_index(0, tol_x), _axis_index(1, tol_y)
    cells = {}
    for it in items:
        cells.setdefault((ci[it[0]], ri[it[1]], it[4]), []).append(it)
    seen, comps = set(), []
    for key in list(cells):
        if key in seen:
            continue
        seen.add(key)
        comp = list(cells[key])
        stack = [key]
        while stack:
            col, row, rgb = cur = stack.pop()
            # 同轴 ±1..±span（四个方向各一段，展平成一个列表——写成 [[(..),(..)] for d]
            # 会把"一对"当成一个元素，`for ncol, nrow in neigh` 解出来的是两个元组，
            # 候选全部落空且一声不吭（★R281-4 自查时就这样静默退化成"谁都不团"）。
            neigh = ([(col + d, row) for d in range(1, span + 1)]
                     + [(col - d, row) for d in range(1, span + 1)]
                     + [(col, row + d) for d in range(1, span + 1)]
                     + [(col, row - d) for d in range(1, span + 1)])
            for ncol, nrow in neigh:
                nk = (ncol, nrow, rgb)
                if nk in seen or nk not in cells:
                    continue
                if not any(_rect_gap(a, b) <= _link_allow(a, b, link_px, tight)
                           for a in cells[cur] for b in cells[nk]):
                    continue    # 索引挨着不算，间距说了才算（余量随格子尺寸走）
                seen.add(nk)
                stack.append(nk)
                comp.extend(cells[nk])
        comps.append(comp)
    return comps


def _box_hits(x0, y0, x1, y1, boxes):
    """牌窗与任一图标格相交？（闭开区间，贴边不算盖）"""
    for ox, oy, ow, oh in boxes:
        if x0 < ox + ow and ox < x1 and y0 < oy + oh and oy < y1:
            return True
    return False


def _label_band(cx0, cw, ch, y0, y1, win_h, occupied):
    """分类名小牌的顶边 + 是否落到"最后手段"档。

    候选：包络上方 => 包络下方，两个候选都要**不与任何图标格相交**
    （★R274 预览自查：两行密集网格下'上团挪下方/下团挪上方'正好砸在
    邻行的名字上，只判窗口边界是不够的）；都不干净才退到骑线——
    牌底贴图标格顶（包络上沿 + 它自己那 4px 外边距），并按 ZONE_LABEL_*
    走纱牌或描边文字。色键窗没有逐像素 alpha，所以'盖少一点'只有这两条路。"""
    for cy in (y0 - ch - 2, y1 + 2):
        if cy < 0:
            continue
        if win_h and cy + ch > win_h:
            continue
        if not _box_hits(cx0, cy, cx0 + cw, cy + ch, occupied):
            return cy, False
    return max(0, y0 + 4 - ch), True

class IconOverlayWindow:
    """桌面图标框选窗口（色键透明 + 点击穿透 + 多形状边框）"""

    def __init__(self):
        self.hwnd = None
        self.width = 0
        self.height = 0
        self.color = (0, 191, 255)
        self.shape = '圆角立体'   # 边框形状（与 highlighter._shape 同步）
        self.dashed = False       # 虚线开关（与 highlighter._dashed 同步）
        self.display_mode = 'border'  # 见 highlighter.DISPLAY_MODES：border/block/text_hl/spotlight + zone/block_ring/marker/badge
        # ★R279-①：并团松紧档位名（只有 display_mode=='zone' 那一路会读它）
        self.zone_tightness = ZONE_TIGHT_DEFAULT
        # ★R288：分区画法档位名（同样只有 zone 那一路读；默认档=现行包络框）
        self.zone_style = ZONE_STYLE_DEFAULT
        self.pen_width = 2
        self._class_registered = False
        # ★R273：抖动图案刷按 (颜色, 覆盖率) 复用、标签字体按像素高复用。
        # 每帧每图标各造一个 GDI 对象就是 R193 那笔泄漏账的重演（上限约 1 万），
        # 所以两类缓存都挂在窗对象上、destroy() 里统一释放。
        self._dither_brushes = {}
        # ★R289：斜纹带那枚图案刷同样按 (颜色, 图案) 复用（同一笔泄漏账）
        self._hatch_brushes = {}
        self._fonts = {}

    def create(self, x: int, y: int, width: int, height: int) -> bool:
        """创建色键透明的分层窗口"""
        if self.hwnd:
            self.destroy()

        className = "IconOverlayClass"
        if not self._class_registered:
            wc = win32gui.WNDCLASS()
            wc.hInstance = win32api.GetModuleHandle(None)
            wc.lpszClassName = className
            wc.style = win32con.CS_HREDRAW | win32con.CS_VREDRAW
            wc.lpfnWndProc = self._wndproc
            wc.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
            wc.hbrBackground = 0
            try:
                win32gui.RegisterClass(wc)
                self._class_registered = True
            except win32gui.error:
                pass

        self.hwnd = win32gui.CreateWindowEx(
            win32con.WS_EX_LAYERED | win32con.WS_EX_NOACTIVATE |
            win32con.WS_EX_TOOLWINDOW | win32con.WS_EX_TRANSPARENT,
            className,
            None,
            win32con.WS_POPUP,
            x, y, width, height,
            None,
            None,
            win32api.GetModuleHandle(None),
            None
        )

        if not self.hwnd:
            return False

        self.width = width
        self.height = height

        # 色键模式：黑色像素透明（必须在绘制前设置，一次即可）
        win32gui.SetLayeredWindowAttributes(
            self.hwnd, 0, 0, win32con.LWA_COLORKEY)
        # 重置分层模式缓存：同实例 destroy→create 重建后必须重新应用
        # spotlight 的 LWA_ALPHA（否则暗幕变回全不透明，"背景彻底没了"）
        self._layer_alpha = None

        win32gui.ShowWindow(self.hwnd, win32con.SW_HIDE)
        return True

    def clear(self):
        """清空绘制表面（全黑=全透明）"""
        if not self.hwnd:
            return
        hdc = None
        brush = None
        try:
            hdc = win32gui.GetDC(self.hwnd)
            brush = win32gui.CreateSolidBrush(0)
            old = win32gui.SelectObject(hdc, brush)
            try:
                win32gui.Rectangle(hdc, 0, 0, self.width, self.height)
            finally:
                win32gui.SelectObject(hdc, old)
        except Exception as e:
            logger.error(f"overlay clear 失败: {e}")
        finally:
            # ★R334-a：刷此前只在成功路径删——SelectObject/Rectangle 一发抛就
            # 永久漏一枚 GDI 刷（进程 GDI 上限约 1 万，漏满整窗画不出）
            if brush:
                try:
                    win32gui.DeleteObject(brush)
                except Exception as e:
                    logger.warning(f"overlay clear 刷释放失败: {e}")
            if hdc:
                win32gui.ReleaseDC(self.hwnd, hdc)

    def _dashed_line(self, hdc, x1, y1, x2, y2, dash=9, gap=7):
        """手绘虚线：GDI 的 PS_DASH 在线宽 >1 时退化为实线，
        虚线效果必须自己分段画（2026-09-05 用户反馈实线虚线难区分）。"""
        import math
        dx, dy = x2 - x1, y2 - y1
        length = math.hypot(dx, dy)
        if length <= 0:
            return
        ux, uy = dx / length, dy / length
        t = 0.0
        on = True
        while t < length:
            seg = dash if on else gap
            t2 = min(t + seg, length)
            if on:
                win32gui.MoveToEx(hdc, int(x1 + ux * t), int(y1 + uy * t))
                win32gui.LineTo(hdc, int(x1 + ux * t2), int(y1 + uy * t2))
            t = t2
            on = not on

    def _dither_brush(self, rgb, coverage):
        """取（必要时造）一枚 Bayer 抖动图案刷，按 (颜色, 覆盖率档) 复用。

        单色位图的 GDI 语义是死的：位=1 走**背景色**、位=0 走**前景色** ⇒
        这里把"要透出的点"置 1 配黑色背景（色键=桌面透出），"着色的点"置 0
        配分类色。位序：每行 8 位补齐到 **WORD**（单色 DDB 扫描行按 2 字节
        对齐；按 DWORD 补的那 2 个填充字节会让 GDI 隔行读到空行＝整行着色，
        ★S6 实测 0.12 画成 0.526），最低位是最左像素。
        位图/图案刷都走 gdi32：pywin32 的 CreateBitmap 不接受 bits 缓冲区
        （实测 TypeError: This param must be None）。
        """
        key = (tuple(rgb), int(round(max(0.0, min(1.0, coverage)) * 64)))
        got = self._dither_brushes.get(key)
        if got:
            return got[0]
        thr = key[1]
        rows = bytearray()
        for y in range(8):
            bits = 0
            row = _BAYER8[y]
            for x in range(8):
                if row[x] >= thr:
                    bits |= 1 << x
            rows += bytes((bits, 0))
        bmp = _handle_or_0(_gdi32.CreateBitmap(8, 8, 1, 1, bytes(rows)))
        brush = _handle_or_0(_gdi32.CreatePatternBrush(bmp))
        # 位图随刷一起留着：图案刷不复制位图内容，提前 DeleteObject 会画飞
        self._dither_brushes[key] = (brush, bmp)
        return brush

    def _select_dither(self, hdc, rgb, coverage):
        """把抖动纱选进 DC 并配好前/背景色；返回收尾三元组供 _restore_dc。"""
        brush = self._dither_brush(rgb, coverage)
        return (win32gui.SelectObject(hdc, brush),
                win32gui.SetTextColor(hdc, win32api.RGB(*rgb)),
                win32gui.SetBkColor(hdc, 0))

    @staticmethod
    def _restore_dc(hdc, state):
        win32gui.SelectObject(hdc, state[0])
        win32gui.SetTextColor(hdc, state[1])
        win32gui.SetBkColor(hdc, state[2])

    def _font(self, px):
        """按像素高取（必要时造）标签字体，挂在窗对象上复用。"""
        f = self._fonts.get(px)
        if f is None:
            f = _handle_or_0(_gdi32.CreateFontW(
                -px, 0, 0, 0, 700, 0, 0, 0, 1,
                0, 0, 4, 0, "Microsoft YaHei UI"))
            self._fonts[px] = f
        return f

    def _dot(self, hdc, x, y, w, rgb):
        """角贴：右上角一枚 d×d 实心分类色圆点 + 1px 中性描边。

        ★R291：描边色换 `_DOT_RIM`——近黑那一枚在深色壁纸下等于没画。"""
        d = max(7, min(11, w // 9))
        x0, y0 = x + w - d - 2, y + 2
        pen = win32gui.CreatePen(win32con.PS_SOLID, 1, win32api.RGB(*_DOT_RIM))
        fill = win32gui.CreateSolidBrush(win32api.RGB(*rgb))
        try:
            old_p = win32gui.SelectObject(hdc, pen)
            old_b = win32gui.SelectObject(hdc, fill)
            _gdi32.Ellipse(hdc, x0, y0, x0 + d, y0 + d)
            win32gui.SelectObject(hdc, old_b)
            win32gui.SelectObject(hdc, old_p)
        finally:
            win32gui.DeleteObject(fill)
            win32gui.DeleteObject(pen)

    def draw_rect(self, x: int, y: int, w: int, h: int, hdc, color=None):
        """按形状绘制边框到指定 DC。

        形状：下划线（底边一条线）/ U形（左右+底，无顶）/
        四角（四角 L 形标记，Fences 风格）/ 双线 / 圆角 / 圆角立体 / 直角。
        color: (r,g,b) 覆盖 self.color（分类色模式）。"""
        r, g, b = color if color else self.color
        # 虚线：直线形状走 _dashed_line 手绘；圆类形状（RoundRect）GDI
        # 限制只能 1px 虚线，故 pen 宽度在虚线时降为 1
        # ★R278-D：垫底三档（block/text_hl/block_ring）画的是自己的圆角/矩形
        # 轮廓，跟"形状"那一档无关，而"形状"对它们已经置灰不可点 ⇒ 门槛不能
        # 再写 self.shape，否则存档恰好是"直角"时"样式"在这三档假可用。
        curve_ctx = (self.shape in ('圆角', '双线', '圆角立体')
                     or self.display_mode in ('block', 'text_hl', 'block_ring'))
        dashed_curve = self.dashed and curve_ctx
        pw = 1 if dashed_curve else self.pen_width
        style = win32con.PS_DASH if dashed_curve else win32con.PS_SOLID
        pen = win32gui.CreatePen(style, pw, win32api.RGB(r, g, b))
        null_brush = win32gui.GetStockObject(win32con.NULL_BRUSH)
        gdi32 = ctypes.windll.gdi32
        # ★R336-g（用户点头解冻方框红线）：pen 一建成就进 try、收尾放 finally。
        # 修前 L4 实测「SelectObject 一抛就 建=5 漏=5」——那枚 pen 只在成功路径删。
        # 中途抛异常时 DC 还选着这支笔，DeleteObject 会被 GDI 拒（选中的对象删不掉），
        # 所以 finally 先把它选回、再删；两处 select 自己也可能抛，兜一行日志。
        old_pen = old_brush = None
        try:
            old_pen = win32gui.SelectObject(hdc, pen)
            old_brush = win32gui.SelectObject(hdc, null_brush)

            # --- 色块垫底 / 文字高亮 模式（display_mode 级别，非 shape 级别）---
            if self.display_mode == 'block':
                # 色块垫底 v2（2026-09-05 用户确认回退）：圆角色垫包住整个图标格，
                # 中间挖 colorkey 透明洞——图标本体+文字完全可见，
                # 四周是带深色描边的圆角"垫子"
                bw = max(5, min(9, w // 14))
                rad = 24
                # 1) 深色底（外圈描边，立体感）
                dk = (int(r * 0.45), int(g * 0.45), int(b * 0.45))
                rim = win32gui.CreateSolidBrush(win32api.RGB(*dk))
                old_f = win32gui.SelectObject(hdc, rim)
                gdi32.RoundRect(hdc, x, y, x + w, y + h, rad, rad)
                # 2) 主体亮色（略内缩，露出 2px 深边）
                body = win32gui.CreateSolidBrush(win32api.RGB(r, g, b))
                win32gui.SelectObject(hdc, body)
                gdi32.RoundRect(hdc, x + 2, y + 2, x + w - 2, y + h - 2, rad, rad)
                # 3) 挖透明洞（colorkey 黑 = 透明，图标和文字透出）
                hole = win32gui.CreateSolidBrush(0)
                win32gui.SelectObject(hdc, hole)
                gdi32.RoundRect(hdc, x + bw, y + bw, x + w - bw, y + h - bw,
                                max(8, rad - bw * 2), max(8, rad - bw * 2))
                win32gui.SelectObject(hdc, old_f)
                win32gui.DeleteObject(rim)
                win32gui.DeleteObject(body)
                win32gui.DeleteObject(hole)
                # ★ R193（Qoder CORE-7）：此前直接 return 漏掉 pen/brush 收尾——
                # 色块模式每帧每图标漏 1 个 GDI pen，用一天耗尽（上限约 1 万）
                win32gui.SelectObject(hdc, old_pen)
                win32gui.SelectObject(hdc, old_brush)
                return
            if self.display_mode == 'text_hl':
                # 文字高亮 v2（2026-09-05 用户确认回退）：U形细色带（左+右+底，各4px）
                # 包住文字区，文字本体完全可见
                tz = max(26, h * 32 // 100)    # 文字区高度
                ty = y + h - tz                # 文字区顶
                bw2 = 4
                fill = win32gui.CreateSolidBrush(win32api.RGB(r, g, b))
                old_f = win32gui.SelectObject(hdc, fill)
                win32gui.Rectangle(hdc, x, ty, x + bw2, y + h)                       # 左
                win32gui.Rectangle(hdc, x + w - bw2, ty, x + w, y + h)               # 右
                win32gui.Rectangle(hdc, x, y + h - bw2, x + w, y + h)                # 底
                win32gui.SelectObject(hdc, old_f)
                win32gui.DeleteObject(fill)
                # ★ R193（Qoder CORE-7）：同上，补 pen/brush 收尾再返回
                win32gui.SelectObject(hdc, old_pen)
                win32gui.SelectObject(hdc, old_brush)
                return
            if self.display_mode == 'block_ring':
                # ★R273 新②b「色块纱环」：四周那圈"垫"是抖动纱，中间是**真透明**
                # （色键黑），图标与文字零覆盖。与旧 block 的区别就一条：
                # "实色环 + 硬洞" → "纱环 + 软边界"（用户点名治"闷、贴片感"）
                bw = max(5, min(9, w // 14))
                rad = 24
                st = self._select_dither(hdc, (r, g, b), BLOCKRING_COVER)
                gdi32.RoundRect(hdc, x, y, x + w, y + h, rad, rad)
                hole = win32gui.CreateSolidBrush(0)
                null_pen = win32gui.GetStockObject(win32con.NULL_PEN)
                try:
                    op = win32gui.SelectObject(hdc, null_pen)
                    ob = win32gui.SelectObject(hdc, hole)
                    gdi32.RoundRect(hdc, x + bw, y + bw, x + w - bw, y + h - bw,
                                    max(8, rad - bw * 2), max(8, rad - bw * 2))
                    win32gui.SelectObject(hdc, ob)
                    win32gui.SelectObject(hdc, op)
                finally:
                    win32gui.DeleteObject(hole)
                self._restore_dc(hdc, st)
                win32gui.SelectObject(hdc, old_pen)
                win32gui.SelectObject(hdc, old_brush)
                return
            if self.display_mode == 'marker':
                # ★R273 新③b「荧光笔」：色条只压文字区**下沿**（字主体不被点阵
                # 穿过——用户 item4 原话"字最好看清楚一点点"），两端圆头 + 1px 描边
                tz = max(26, h * 32 // 100)    # 文字区高度（与旧 text_hl 同口径）
                bh = max(9, min(16, tz * 2 // 5))
                by = y + h - bh - MARKER_BOTTOM
                st = self._select_dither(hdc, (r, g, b), MARKER_COVER)
                # 色带只要点阵填充：沿用上面那枚 pen 会按用户线宽画一圈实心
                # 分类色描边（pen_width=2 实测越界 2px 进文字区 ⇒ B3 红 63px），
                # "只压字下沿"就不成立。这里临时换 NULL_PEN，收尾仍交回 old_pen。
                op = win32gui.SelectObject(hdc, win32gui.GetStockObject(win32con.NULL_PEN))
                gdi32.RoundRect(hdc, x + MARKER_INSET, by, x + w - MARKER_INSET,
                                by + bh, bh, bh)
                win32gui.SelectObject(hdc, op)
                self._restore_dc(hdc, st)
                win32gui.SelectObject(hdc, old_pen)
                win32gui.SelectObject(hdc, old_brush)
                return
            if self.display_mode == 'badge':
                # ★R273 新④「角贴」：整格只留右上角一枚色点，桌面几乎无感
                self._dot(hdc, x, y, w, (r, g, b))
                win32gui.SelectObject(hdc, old_pen)
                win32gui.SelectObject(hdc, old_brush)
                return
            if self.shape == '下划线':
                # 两端内缩：相邻图标的下划线不连成一条长线
                inset = max(4, min(12, w // 10))
                if self.dashed:
                    self._dashed_line(hdc, x + inset, y + h, x + w - inset, y + h)
                else:
                    win32gui.MoveToEx(hdc, x + inset, y + h)
                    win32gui.LineTo(hdc, x + w - inset, y + h)
            elif self.shape == 'U形':
                if self.dashed:
                    self._dashed_line(hdc, x, y, x, y + h)
                    self._dashed_line(hdc, x, y + h, x + w, y + h)
                    self._dashed_line(hdc, x + w, y + h, x + w, y)
                else:
                    win32gui.MoveToEx(hdc, x, y)
                    win32gui.LineTo(hdc, x, y + h)
                    win32gui.LineTo(hdc, x + w, y + h)
                    win32gui.LineTo(hdc, x + w, y)
            elif self.shape == '四角':
                arm = max(8, min(22, w // 5, h // 5))
                dl = self._dashed_line if self.dashed else None
                if dl:
                    dl(hdc, x, y + arm, x, y)
                    dl(hdc, x, y, x + arm, y)
                    dl(hdc, x + w - arm, y, x + w, y)
                    dl(hdc, x + w, y, x + w, y + arm)
                    dl(hdc, x + w, y + h - arm, x + w, y + h)
                    dl(hdc, x + w, y + h, x + w - arm, y + h)
                    dl(hdc, x + arm, y + h, x, y + h)
                    dl(hdc, x, y + h, x, y + h - arm)
                else:
                    win32gui.MoveToEx(hdc, x, y + arm)
                    win32gui.LineTo(hdc, x, y)
                    win32gui.LineTo(hdc, x + arm, y)
                    win32gui.MoveToEx(hdc, x + w - arm, y)
                    win32gui.LineTo(hdc, x + w, y)
                    win32gui.LineTo(hdc, x + w, y + arm)
                    win32gui.MoveToEx(hdc, x + w, y + h - arm)
                    win32gui.LineTo(hdc, x + w, y + h)
                    win32gui.LineTo(hdc, x + w - arm, y + h)
                    win32gui.MoveToEx(hdc, x + arm, y + h)
                    win32gui.LineTo(hdc, x, y + h)
                    win32gui.LineTo(hdc, x, y + h - arm)
            elif self.shape == '双线':
                gdi32.RoundRect(hdc, x, y, x + w, y + h, 18, 18)
                inset2 = 3 + pw
                gdi32.RoundRect(hdc, x + inset2, y + inset2,
                                x + w - inset2, y + h - inset2, 12, 12)
            elif self.shape == '圆角':
                gdi32.RoundRect(hdc, x, y, x + w, y + h, 14, 14)
            elif self.shape == '圆角立体':
                sr = max(3, int(r * 0.35))
                sg = max(3, int(g * 0.35))
                sb = max(3, int(b * 0.35))
                shadow_pen = win32gui.CreatePen(
                    win32con.PS_SOLID, max(1, self.pen_width - 1),
                    (sb << 16) | (sg << 8) | sr)
                old_sh = win32gui.SelectObject(hdc, shadow_pen)
                gdi32.RoundRect(hdc, x + 2, y + 2, x + w + 2, y + h + 2, 14, 14)
                win32gui.SelectObject(hdc, old_sh)
                win32gui.DeleteObject(shadow_pen)
                gdi32.RoundRect(hdc, x, y, x + w, y + h, 14, 14)
            else:   # 直角
                if self.dashed:
                    self._dashed_line(hdc, x, y, x + w, y)
                    self._dashed_line(hdc, x, y + h, x + w, y + h)
                    self._dashed_line(hdc, x, y, x, y + h)
                    self._dashed_line(hdc, x + w, y, x + w, y + h)
                else:
                    win32gui.Rectangle(hdc, x, y, x + w, y + h)

            win32gui.SelectObject(hdc, old_pen)
            win32gui.SelectObject(hdc, old_brush)
        finally:
            try:
                if old_pen is not None:
                    win32gui.SelectObject(hdc, old_pen)
                if old_brush is not None:
                    win32gui.SelectObject(hdc, old_brush)
            except Exception as e:
                logger.debug('[框] draw_rect 收尾选回原笔刷没成（pen 仍照常删）: %r' % e)
            win32gui.DeleteObject(pen)

    def draw_icons(self, rects: dict, colors: dict = None,
                   labels: dict = None) -> bool:
        """绘制图标框：窗口 DC 直绘（黑底=色键透明 + 各形状边框）。

        colors: {idx: (r,g,b)} 每图标独立颜色（分类色模式），缺省用 self.color。
        labels: {idx: 分类名} 仅 'zone' 档用（包络上沿的小牌）。
        spotlight 模式：全屏暗化 + 选中图标区域透明镂空。
        zone 模式：整团一个包络，不逐格画框。"""
        if not self.hwnd or not rects:
            return False
        self._apply_layer_mode()
        if self.display_mode == 'spotlight':
            return self._draw_spotlight(rects, colors)
        hdc = None
        try:
            hdc = win32gui.GetDC(self.hwnd)
            # 黑底 = 色键透明（★R204 CORE-7 残留：bg 配对收尾）
            bg = win32gui.CreateSolidBrush(0)
            try:
                old_bg = win32gui.SelectObject(hdc, bg)
                win32gui.Rectangle(hdc, 0, 0, self.width, self.height)
                win32gui.SelectObject(hdc, old_bg)
            finally:
                win32gui.DeleteObject(bg)
            # 各图标框（★R273：分区档例外，整团一个包络）
            if self.display_mode == 'zone':
                self._draw_zones(hdc, rects, colors, labels)
            else:
                for idx, (x, y, w, h) in rects.items():
                    color = colors.get(idx) if colors else None
                    self.draw_rect(x, y, w, h, hdc, color)
            return True
        except Exception as e:
            logger.error(f"overlay draw_icons 失败: {e}")
            return False
        finally:
            if hdc:
                win32gui.ReleaseDC(self.hwnd, hdc)

    def _apply_layer_mode(self):
        """按 display_mode 调整分层属性（缓存上次值，避免每次绘制都调用）。

        spotlight：LWA_COLORKEY | LWA_ALPHA（整窗 185/255 不透明）——
        2026-09-05 用户反馈纯色暗幕"背景彻底没了、任务栏也看不见"；
        半透明后壁纸与任务栏隐约可见，镂空区（色键）不受 alpha 影响仍全透。"""
        if not self.hwnd:
            return
        # ★ R130 修复：从聚光灯切回其他模式时，原代码不带 LWA_ALPHA 调用，
        #   之前设的 185 半透明不被覆盖、缓存又记 0 不再重试 → 整窗持续发虚。
        #   改为恒带 LWA_ALPHA：非聚光灯显式写 255（视觉等同无透明）。
        alpha = 185 if self.display_mode == 'spotlight' else 255
        if getattr(self, '_layer_alpha', None) == alpha:
            return
        flags = win32con.LWA_COLORKEY | win32con.LWA_ALPHA
        win32gui.SetLayeredWindowAttributes(self.hwnd, 0, alpha, flags)
        self._layer_alpha = alpha

    def _draw_spotlight(self, rects: dict, colors: dict) -> bool:
        """聚光灯模式：全屏暗化，选中图标区域镂空（透明=色键黑）。

        每个选中图标：先挖一个透明洞（填充色键黑），洞周围画亮色标记框。"""
        if not self.hwnd:
            return False
        # ★ B3：except 里引用 hdc——若 GetDC 之前（_apply_layer_mode）就抛异常，
        #   未初始化的 hdc 会引发二次 NameError 掩盖原始异常
        hdc = None
        dim = None
        try:
            self._apply_layer_mode()
            hdc = win32gui.GetDC(self.hwnd)
            # 1) 全屏填暗色（非色键色，半透明可见的暗色蒙版）
            dim = win32gui.CreateSolidBrush(win32api.RGB(32, 32, 46))
            old_b = win32gui.SelectObject(hdc, dim)
            try:
                win32gui.Rectangle(hdc, 0, 0, self.width, self.height)
            finally:
                win32gui.SelectObject(hdc, old_b)
            # ★R334-a：暗幕刷挪到 finally 删（原来只在成功路径删，Rectangle 一抛
            # 就漏；下面那个逐条目的 hole/pen 早就配了 finally，这一枚是漏网的）
            win32gui.DeleteObject(dim)
            dim = None
            # 2) 每个选中图标区域挖透明洞 + 画标记框
            # ★R204 CORE-30：per-item 创建/删除配对 try/finally——异常路径
            # 漏 DeleteObject = 聚光灯模式每未完成条目泄漏 1 笔 1 刷
            for idx, (x, y, w, h) in rects.items():
                # 挖洞：填色键黑色 → 该区域透明，桌面图标透出
                hole = win32gui.CreateSolidBrush(0)
                try:
                    hb = win32gui.SelectObject(hdc, hole)
                    win32gui.Rectangle(hdc, x, y, x + w, y + h)
                    win32gui.SelectObject(hdc, hb)
                finally:
                    win32gui.DeleteObject(hole)
                # 洞周围画亮色框标记（简洁矩形——形状联动已回退：
                # 2026-09-05 用户实测各种形状的标记框让桌面很乱）
                c = colors.get(idx) if colors else self.color
                cr, cg, cb = c if c else (0, 191, 255)
                pen = win32gui.CreatePen(win32con.PS_SOLID, 2,
                                         win32api.RGB(cr, cg, cb))
                try:
                    old_p = win32gui.SelectObject(hdc, pen)
                    nb = win32gui.GetStockObject(win32con.NULL_BRUSH)
                    win32gui.SelectObject(hdc, nb)
                    win32gui.Rectangle(hdc, x - 2, y - 2, x + w + 2, y + h + 2)
                    win32gui.SelectObject(hdc, old_p)
                finally:
                    win32gui.DeleteObject(pen)
            win32gui.ReleaseDC(self.hwnd, hdc)
            return True
        except Exception as e:
            logger.error(f"spotlight 绘制失败: {e}")
            if dim:
                try:
                    win32gui.DeleteObject(dim)
                except Exception as _e2:
                    # ★R335-b（批88，挂账 §四十 item2）：兜底可以吞异常，但**必须留一行**
                    # ——同文件 `clear()` 与 ★R227 那条纪律都是这么写的，这一处从前是
                    # `pass`。漏痕的后果不是崩，是用户下次看到"桌面暗幕擦不干净"时
                    # 日志里一个字都没有，只能重猜。
                    logger.warning('[蒙版] spotlight 异常补删暗幕刷没成功（句柄可能仍占着）: %s',
                                   _e2)
            try:
                win32gui.ReleaseDC(self.hwnd, hdc)
            except Exception as _e3:
                logger.warning('[蒙版] spotlight 异常路径 ReleaseDC 没成（DC 可能还没还）: %s',
                               _e3)
            return False

    def _draw_zones(self, hdc, rects, colors, labels):
        """★R273 新①「分区」：同一分类且**间距够近**的图标并成一团，一团一个
        圆角包络 + 分类名标签（用户点名治"图标多了方框太密"）。

        ★R274：聚类搬进纯函数 zone_clusters——判据换成真实净空，
        空白地带不再被包络跨过去。簇 ≤2 格不画整框，退化成角贴
        （两格的框比两个点更碍眼）。纯 GDI（RoundRect/ExtTextOut），
        走同一条 DC 直绘路径，不新增任何跨进程读取。"""
        items = []
        for idx, (x, y, w, h) in rects.items():
            c = (colors or {}).get(idx) or self.color
            items.append([x, y, w, h, tuple(c), (labels or {}).get(idx) or ''])
        occupied = [(it[0], it[1], it[2], it[3]) for it in items]
        # ★R279-①：档位在这里换算成余量倍率（唯一读者就是这一路）
        for comp in zone_clusters(items, tight=self.zone_tightness):
            self._draw_cluster(hdc, comp, occupied)


    def _draw_cluster(self, hdc, comp, occupied=()):
        """一个分簇：**≥2 格就画包络 + 标签**，只有落单的那一格退化成角贴。

        ★R278-C（用户 09-30 点名）：旧门槛是 ≤2 格不画整框，于是"两个挨着的
        图标"（竖排尤其常见——一列只摆两三个）各吃一枚角贴，用户读成"竖着的不连"。
        单格仍走角贴：整框圈一个图标既没信息量又会和邻团打架。

        ★R288（批44，用户点名"把色块纱环/荧光笔/角贴也支持到分区"）：这里改成
        画法**分派**（`self.zone_style`，表在 ZONE_STYLES；★R289 起八档）。默认档 '包络框'
        走 `_zone_envelope`——那是修前那段代码逐字搬过去的，不是重写（闸 A1/A1c）；
        另三档借用逐图标那三档的画法，但包围盒按**整团**给：同一套 x0/y0/x1/y1，
        切画法不会让团"长大一圈/缩一圈"。认不得的档名回默认档——坏存档不该把整帧
        画崩，也不该画成"什么都没画"。"""
        rgb = comp[0][4]
        style = getattr(self, 'zone_style', ZONE_STYLE_DEFAULT)
        if style not in ZONE_STYLES:
            style = ZONE_STYLE_DEFAULT
        if style == ZONE_STYLE_DEFAULT and len(comp) <= 1:
            for it in comp:
                self._dot(hdc, it[0], it[1], it[2], rgb)
            return
        x0 = min(i[0] for i in comp) - 4
        y0 = min(i[1] for i in comp) - 4
        x1 = max(i[0] + i[2] for i in comp) + 4
        y1 = max(i[1] + i[3] for i in comp) + 4
        if style == '色块纱环':
            self._zone_ring(hdc, x0, y0, x1, y1, rgb)
        elif style == '荧光笔':
            self._zone_marker(hdc, comp, x0, y0, x1, y1, rgb)
        elif style == '角贴':
            for it in comp:
                self._dot(hdc, it[0], it[1], it[2], rgb)
        elif style == '取景角':
            self._zone_brackets(hdc, x0, y0, x1, y1, rgb)
        elif style == '双线框':
            self._zone_double(hdc, x0, y0, x1, y1, rgb)
        elif style == '细双线框':
            self._zone_double(hdc, x0, y0, x1, y1, rgb, thin=True)
        elif style == '斜纹带':
            self._zone_hatch(hdc, comp, x0, y0, x1, y1, rgb)
        elif style == '柔光晕':
            self._zone_glow(hdc, x0, y0, x1, y1, rgb)
        elif style == '细柔光晕':
            self._zone_glow(hdc, x0, y0, x1, y1, rgb, thin=True)
        else:
            self._zone_envelope(hdc, x0, y0, x1, y1, rgb)
        names = [i[5] for i in comp if i[5]]
        # ★R288 画法=角贴：名牌只留**团右上角那一枚**（头顶那块带牌不画）——
        # 用户口径"只画角上那一枚"，两块都画就是一团上两枚重复牌。落单那一格
        # 仍不铺牌（挨着的才叫分区，一格也摆不出"团"的角）。
        if names and len(comp) > 1:
            _nm = max(set(names), key=names.count)
            self._zone_label(hdc, _nm, x0, y0, x1, y1, comp[0][3], rgb,
                             occupied, corner=(style == '角贴'))

    def _zone_ring(self, hdc, x0, y0, x1, y1, rgb):
        """★R288 画法②「色块纱环」：团外沿一圈抖动纱 + 中间**真透明**（色键黑）。

        纱的覆盖率与逐图标档同口径（BLOCKRING_COVER），带宽改按**团的短边**给：
        大团还是 5~9px 那一圈，不会糊成一整块色板。
        ★R293-③ 这一档现在**读** 线宽（环宽 = 基准带宽按 `_zk` 缩放 ⇒ 默认线宽 2 逐字节
        等于修前）与 形状（`直角` ⇒ 方角环，内外两圈一起变方，其余仍圆角）；
        仍**不读**样式——这一档一支笔都不用（描边一律 NULL_PEN），填充是抖动刷，
        "虚线"没有可以虚的东西 ⇒ 面板把那三味里的"样式"单独灰掉是实话。
        收尾口径照 R193：洞刷建了就删，纱刷交回 `_restore_dc`。"""
        bw = _zk(max(5, min(9, min(x1 - x0, y1 - y0) // 14)), self.pen_width)
        sq = _shape_is_square(self.shape)
        rad = 0 if sq else 24
        st = self._select_dither(hdc, rgb, BLOCKRING_COVER)
        null_pen = win32gui.GetStockObject(win32con.NULL_PEN)
        hole = win32gui.CreateSolidBrush(0)
        try:
            op = win32gui.SelectObject(hdc, null_pen)
            _gdi32.RoundRect(hdc, x0, y0, x1, y1, rad, rad)
            ob = win32gui.SelectObject(hdc, hole)
            _gdi32.RoundRect(hdc, x0 + bw, y0 + bw, x1 - bw, y1 - bw,
                             0 if sq else max(8, rad - bw * 2),
                             0 if sq else max(8, rad - bw * 2))
            win32gui.SelectObject(hdc, ob)
            win32gui.SelectObject(hdc, op)
        finally:
            win32gui.DeleteObject(hole)
            self._restore_dc(hdc, st)

    @staticmethod
    def _cluster_rows(comp):
        """把一团按**行底**归并，返回每行最底那条 y（荧光笔"一行一条带"用）。
        容差按**格子高**给（不是 `_size_tol` 那种按坐标量级给）：一个 2×2 团
        两行的行底本来就差一整个格距，若容差跟着 y 的绝对值走，桌面下半部的
        团（y≈600）会把两行并成一行 ⇒ 上面那行的字吃不到带（★R288 视觉体检抓到，
        闸 Z3d）。手摆竖排那点抖动仍落在 35% 格高以内。"""
        bottoms = sorted(i[1] + i[3] for i in comp)
        tol = max(ZONE_GAP_PX, int(0.35 * max(i[3] for i in comp)))
        rows = []
        for b in bottoms:
            if rows and b - rows[-1][0] <= tol:
                rows[-1].append(b)
            else:
                rows.append([b])
        return [max(g) for g in rows]

    def _zone_marker(self, hdc, comp, x0, y0, x1, y1, rgb):
        """★R288 画法③「荧光笔」：团内**每一行**的字下沿一条色带，带随团宽。

        带宽/两端内缩/离底多高全部沿用逐图标档那三个常量（MARKER_COVER /
        MARKER_INSET / MARKER_BOTTOM），只是拿"这一行的行底"当基线——整团只压
        最底一行的话，上面几行的字就被漏在外面，而第一行恰是最容易被认错的一行。
        ★R293-③ 这一档现在**读** 线宽：带高 = 基准值按 `_zk` 缩放（默认线宽 2 仍是
        9~16px 那条带 ⇒ 逐字节等于修前）。形状/样式仍不吃——一条横带没有"形状"可言，
        而带子是点阵填充、无笔可虚 ⇒ 面板灰掉那两味是实话。"""
        cell_h = max(i[3] for i in comp)
        tz = max(26, cell_h * 32 // 100)      # 文字区高度（与逐图标档同口径）
        bh = _zk(max(9, min(16, tz * 2 // 5)), self.pen_width)
        st = self._select_dither(hdc, rgb, MARKER_COVER)
        null_pen = win32gui.GetStockObject(win32con.NULL_PEN)
        try:
            op = win32gui.SelectObject(hdc, null_pen)
            for bottom in self._cluster_rows(comp):
                by = bottom - bh - MARKER_BOTTOM
                _gdi32.RoundRect(hdc, x0 + MARKER_INSET, by,
                                 x1 - MARKER_INSET, by + bh, bh, bh)
            win32gui.SelectObject(hdc, op)
        finally:
            self._restore_dc(hdc, st)

    def _zone_glow(self, hdc, x0, y0, x1, y1, rgb, thin=False):
        """★R289 画法⑧「柔光晕」／★R293 画法⑩「细柔光晕」（`thin=True`）：
        沿团边向内**三条递减密度**的抖动带，中心挖真透明。

        色键窗没有逐像素 alpha ⇒ "发光"只能靠密度梯度伪装：`GLOW_BANDS` 那三条
        （0.36 / 0.22 / 0.09，每条 4px）依次往里套，最后用黑色实心刷把第 `GLOW_DEPTH`
        格以内挖掉——读起来是"光从边上往里淡"，而图标本体一格都没被糊住。
        三条带**挨着铺**（内缩 0/4/8 + 总深 12）⇒ 闸 K7g 拿表自身的等差钉住这件事，
        改出一格缝就红。
        包围盒与其余档**完全一致**（同一枚 x0/y0/x1/y1）⇒ 换画法不会让团长大一圈。
        ★R293-② 细档换的**只有这两组数**（带 4px→3px、密度降到 .30/.16/.06、总深 12→9），
        画法的结构、包围盒、挖洞这一套一段没动 ⇒ 它是"同一盏灯调细调淡"，不是第二种光。
        ★R293-③ 这一档现在**读** 线宽（三条带内缩与总深按 `_zk` 缩放 ⇒ 默认线宽 2 逐字节
        等于修前）与 形状（`直角` ⇒ 方角光晕，其余按今天的圆角）；仍**不读**样式——
        带子是抖动填充、一支笔都不用，虚线对它无意义（面板据此仍灰掉那一味）。
        收尾照 R193：洞刷建了就删，纱刷交回 `_restore_dc`。"""
        bands = GLOW_BANDS_THIN if thin else GLOW_BANDS
        cap = _glow_inset_cap(x1 - x0, y1 - y0)
        _gdep = _zk(GLOW_DEPTH_THIN if thin else GLOW_DEPTH, self.pen_width)
        depth = min(_gdep, cap)
        sq = _shape_is_square(self.shape)
        null_pen = win32gui.GetStockObject(win32con.NULL_PEN)
        hole = win32gui.CreateSolidBrush(0)
        try:
            op = win32gui.SelectObject(hdc, null_pen)
            for i, (ins, cov) in enumerate(bands):
                st = self._select_dither(hdc, rgb, cov)
                si = min(_zk(ins, self.pen_width), cap)
                rad = 0 if sq else max(8, 24 - 4 * i)
                _gdi32.RoundRect(hdc, x0 + si, y0 + si, x1 - si, y1 - si,
                                 rad, rad)
                self._restore_dc(hdc, st)
            ob = win32gui.SelectObject(hdc, hole)
            _gdi32.RoundRect(hdc, x0 + depth, y0 + depth,
                             x1 - depth, y1 - depth,
                             0 if sq else 8, 0 if sq else 8)
            win32gui.SelectObject(hdc, ob)
            win32gui.SelectObject(hdc, op)
        finally:
            win32gui.DeleteObject(hole)

    def _zone_brackets(self, hdc, x0, y0, x1, y1, rgb):
        """★R289 画法⑤「取景角」：只在团四角各画一枚**实心 L 形括号**。

        臂长按团的短边 1/6 给（夹在 BRACKET_ARM_MIN/MAX 两头），粗细基准 BRACKET_T=4
        ⇒ ★R293-③ 这一档现在**读** 线宽：`t = _zk(BRACKET_T, 线宽)`（默认线宽 2 ⇒ 还是
        4px，逐字节等于修前；线宽 8 ⇒ 16px）。**臂长不跟线宽**——它是"括号占格子多少"
        的几何量，跟着笔粗走会让小团被括号吃满。形状/样式仍不吃（实心块，PatBlt 无笔）。
        `t` 收在 `arm-1` 以内：线宽拉到最大时竖臂至少还剩一格，不然 `arm - t` 变负、
        PatBlt 拿到负高度直接不画 ⇒ 用户会读成"四角少了两条腿"。
        与 包络框·四角 那一档的区别是**实心 vs 描线**：那档是 1~2px 的笔，这一档
        是 4px 的块，四角更"钉"；而四条边的中段一个像素都不落（色键黑＝桌面透出），
        所以它是各档里遮挡最小的"框住了"。
        每角两支矩形拼 L：横臂占满 arm×t，竖臂只补横臂以下那一段——**不重叠**，
        于是接缝处不会留出第二道描边，也不必再建第二枚笔。
        填的是 `PatBlt(PATCOPY)` 不是 `Rectangle`：后者带 NULL_PEN 时右下各少一列
        一行（实测 24×4 只上屏 23×3），横臂就短了一格；PatBlt 按整数矩形精确上色，
        括号的下沿才真贴在团边上。"""
        arm = max(BRACKET_ARM_MIN,
                  min(BRACKET_ARM_MAX, min(x1 - x0, y1 - y0) // 6))
        t = min(arm - 1, _zk(BRACKET_T, self.pen_width))
        fill = win32gui.CreateSolidBrush(win32api.RGB(*rgb))
        try:
            ob = win32gui.SelectObject(hdc, fill)
            for (rx, ry, rw, rh) in (
                    (x0, y0, arm, t),                     # 左上·横
                    (x0, y0 + t, t, arm - t),             # 左上·竖
                    (x1 - arm, y0, arm, t),               # 右上·横
                    (x1 - t, y0 + t, t, arm - t),         # 右上·竖
                    (x0, y1 - t, arm, t),                 # 左下·横
                    (x0, y1 - arm, t, arm - t),           # 左下·竖
                    (x1 - arm, y1 - t, arm, t),           # 右下·横
                    (x1 - t, y1 - arm, t, arm - t)):      # 右下·竖
                _gdi32.PatBlt(hdc, rx, ry, rw, rh, win32con.PATCOPY)
            win32gui.SelectObject(hdc, ob)
        finally:
            win32gui.DeleteObject(fill)

    def _zone_double(self, hdc, x0, y0, x1, y1, rgb, thin=False):
        """★R289 画法⑥「双线框」／★R290 调观感：外一圈 + 内一圈**同色**分类色，中间留一条透明缝。

        缝是真的（两枚线之间不铺任何底）——黑色在色键窗里就是透明，所以这档读起来
        像"给整团套了一道双线卡口"，比单线更钉，又不会像纱环那样吃掉一整圈。
        ★R290 改的就是"读得出来"这一件事：内线原来用角贴那枚近黑描边色（1px），
        用户真机壁纸实测底色 (2,14,30)，两者最大通道差仅 13~30/255 ⇒ 深色壁纸下第二道
        等于没画（他的原话"看起来还是单线"，像素复验见 `run/r015_真机回话_双线框/`）。
        色键窗**读不到壁纸明暗**，所以内线不再猜色：沿用外线那枚分类色，"双"完全靠
        真透明缝分隔 ⇒ 深浅壁纸不会只治好一半（闸 L1b/L2b）。
        ★R293-② `thin=True` 是「细双线框」：缝基准 8→5、两枚线基准 2→1，别的都不动
        （还是两圈、还是真透明缝）⇒ 治"太抢眼"靠减料，不靠加新画法。
        ★R293-③ 这一档现在**读**那三味（旧口径"线宽由这一档自己给、不读用户旋钮"作废）：
        线宽⇒两枚线粗与缝宽一起按 `_zk` 缩放（默认线宽 2 = 今天的 2px/2px/8px 缝）；
        形状⇒`直角` 画方角双圈，其余仍 18/12 圆角；样式⇒虚线时两枚笔都转 `PS_DASH`
        且宽度落回 1（GDI 只有 1px cosmetic 笔画得出虚线，与 `_zone_envelope` 那档
        同一条限制，不是这里偷懒）。
        窄团（短边不够套两圈）只画外圈，不画退化成一坨的内圈——门槛读**缩放后的实际缝宽**。
        单笔红线照旧：两枚笔各建一次、各删一次，删在 finally。★R289-S6 复核点的
        是"第二枚笔建在 try **外面**"——那一抛会让第一枚没人删（同一帧每团各画一次，
        漏的就是 R193 那笔账），所以第二枚挪进 try，收尾按"建了几枚删几枚"判。"""
        sq = _shape_is_square(self.shape)
        dashed_curve = bool(self.dashed)
        gap = _zk(DOUBLE_GAP_THIN if thin else DOUBLE_GAP, self.pen_width)
        # 虚线那一支：宽度必须是 1，PS_DASH 配 >1 的宽会被 GDI 静默退回实心
        if dashed_curve:
            w_out = w_in = 1
        else:
            w_out = max(1, _zk(DOUBLE_W_THIN if thin else DOUBLE_W_OUT,
                               self.pen_width))
            w_in = max(1, _zk(DOUBLE_W_THIN if thin else DOUBLE_W_IN,
                              self.pen_width))
        pen_style = win32con.PS_DASH if dashed_curve else win32con.PS_SOLID
        p_out = win32gui.CreatePen(pen_style, w_out, win32api.RGB(*rgb))
        p_in = None
        null_brush = win32gui.GetStockObject(win32con.NULL_BRUSH)
        try:
            p_in = win32gui.CreatePen(pen_style, w_in, win32api.RGB(*rgb))
            ob = win32gui.SelectObject(hdc, null_brush)
            op = win32gui.SelectObject(hdc, p_out)
            _gdi32.RoundRect(hdc, x0, y0, x1, y1, 0 if sq else 18, 0 if sq else 18)
            if _double_inner_fits(x1 - x0, y1 - y0, gap):
                win32gui.SelectObject(hdc, p_in)
                _gdi32.RoundRect(hdc, x0 + gap, y0 + gap,
                                 x1 - gap, y1 - gap,
                                 0 if sq else 12, 0 if sq else 12)
            win32gui.SelectObject(hdc, op)
            win32gui.SelectObject(hdc, ob)
        finally:
            if p_in:
                win32gui.DeleteObject(p_in)
            win32gui.DeleteObject(p_out)

    def _hatch_brush(self, rgb, style):
        """取（必要时造）一枚图案刷（斜纹那一类），按 (颜色, 图案) 复用。

        色键语义与 `_dither_brush` 同一套：GDI 拿 **DC 背景色**填纹路之间的空隙 ⇒
        配 `SetBkColor(黑)` 后空隙＝色键＝桌面透出，只有纹线走分类色（所以
        `_select_hatch` 必须同时把背景色钉成黑，漏了它整条带会变实心色板）。
        刷挂在窗对象上复用——每帧每团各造一枚就是 R193 那笔 GDI 泄漏账的重演
        （上限约 1 万），`destroy()` 里与抖动刷一起释放。"""
        key = (tuple(rgb), int(style))
        got = self._hatch_brushes.get(key)
        if got:
            return got
        got = _handle_or_0(_gdi32.CreateHatchBrush(int(style), win32api.RGB(*rgb)))
        self._hatch_brushes[key] = got
        return got

    def _select_hatch(self, hdc, rgb, style):
        """把图案刷选进 DC 并把背景钉成色键黑；返回收尾三元组供 `_restore_dc`。"""
        brush = self._hatch_brush(rgb, style)
        return (win32gui.SelectObject(hdc, brush),
                win32gui.SetTextColor(hdc, win32api.RGB(*rgb)),
                win32gui.SetBkColor(hdc, 0))

    def _zone_hatch(self, hdc, comp, x0, y0, x1, y1, rgb):
        """★R289 画法⑦「斜纹带」：荧光笔那一条行底带，带内换成 45° 斜纹图案刷。

        带几何（带宽 / 两端内缩 / 离底多高 / 一行一条）与 `_zone_marker` **同口径**，
        换的只有填充：点阵 12% → `HS_DIAGCROSS` 交叉斜纹（约 25% 且**有方向**）⇒
        比荧光笔压得住、比纱环透气。多行团仍每行一条（`_cluster_rows` 那一套）。
        ★R293-③ 这一档现在**读** 线宽（带高与 `_zone_marker` 同一枚 `_zk` 缩放 ⇒ 默认
        线宽 2 逐字节等于修前；图案密度本身不缩放，那是图案刷的固有纹样）。
        形状/样式仍不吃 ⇒ NULL_PEN，只有图案填充。"""
        cell_h = max(i[3] for i in comp)
        tz = max(26, cell_h * 32 // 100)      # 文字区高度（与逐图标档同口径）
        bh = _zk(max(9, min(16, tz * 2 // 5)), self.pen_width)
        st = self._select_hatch(hdc, rgb, HATCH_STYLE)
        null_pen = win32gui.GetStockObject(win32con.NULL_PEN)
        try:
            op = win32gui.SelectObject(hdc, null_pen)
            for bottom in self._cluster_rows(comp):
                by = bottom - bh - MARKER_BOTTOM
                _gdi32.RoundRect(hdc, x0 + MARKER_INSET, by,
                                 x1 - MARKER_INSET, by + bh, bh, bh)
            win32gui.SelectObject(hdc, op)
        finally:
            self._restore_dc(hdc, st)

    def _zone_envelope(self, hdc, x0, y0, x1, y1, rgb):
        """★R288 画法①（默认档）「包络框」＝★R282-4 起的现行分区框，逐字保留。

        下面这段（建笔 → 按 self.shape 分派 → 收尾删笔）是从修前的
        `_draw_cluster` **原样搬过来的**，一个字符没改（闸 A1c 拿修前基线树比字节）
        ⇒ "不要把原来的功能丢掉"这句承诺有证据，不是嘴上说说。"""
        # ★R278-D（用户点名"分区要能跟样式/线宽联动"）：包络笔不再写死
        # PS_SOLID/2px，改读用户的线宽与虚线开关。虚线时宽度必须落回 1
        # ——GDI 的 RoundRect 只有 1px cosmetic 笔才画得出虚线（与 border
        # 档 draw_rect:349-351 同一条限制，不是这里偷懒）。
        # ★R282-4（用户 10-01 01 批 R010-3"分区需加点框的样式，太单一"）：包络
        # 不再写死一种圆角，改按 self.shape 画多种分区框（圆角/直角/双线/四角/
        # 下划线/U形）。全部复用**同一枚笔**（单笔红线：建笔/删笔各一次、删笔在
        # finally）——虚线由 PS_DASH 笔自身出，直线曲线都吃得下，所以这里不再逐段
        # 手绘虚线。'圆角立体' 在分区档等同 '圆角'（立体阴影要第二枚笔，会破单笔
        # 红线，且大包络上阴影几乎看不出）。
        dashed_curve = bool(self.dashed)
        pw = 1 if dashed_curve else max(1, int(self.pen_width or 1))
        pen = win32gui.CreatePen(
            win32con.PS_DASH if dashed_curve else win32con.PS_SOLID,
            pw, win32api.RGB(*rgb))
        null_brush = win32gui.GetStockObject(win32con.NULL_BRUSH)
        w = x1 - x0
        h = y1 - y0
        shape = self.shape
        try:
            op = win32gui.SelectObject(hdc, pen)
            ob = win32gui.SelectObject(hdc, null_brush)
            if shape == '直角':
                win32gui.Rectangle(hdc, x0, y0, x1, y1)
            elif shape == '双线':
                _gdi32.RoundRect(hdc, x0, y0, x1, y1, 18, 18)
                inset2 = 3 + pw
                _gdi32.RoundRect(hdc, x0 + inset2, y0 + inset2,
                                 x1 - inset2, y1 - inset2, 12, 12)
            elif shape == '四角':
                arm = max(10, min(30, w // 8, h // 8))
                win32gui.MoveToEx(hdc, x0, y0 + arm)
                win32gui.LineTo(hdc, x0, y0)
                win32gui.LineTo(hdc, x0 + arm, y0)
                win32gui.MoveToEx(hdc, x1 - arm, y0)
                win32gui.LineTo(hdc, x1, y0)
                win32gui.LineTo(hdc, x1, y0 + arm)
                win32gui.MoveToEx(hdc, x1, y1 - arm)
                win32gui.LineTo(hdc, x1, y1)
                win32gui.LineTo(hdc, x1 - arm, y1)
                win32gui.MoveToEx(hdc, x0 + arm, y1)
                win32gui.LineTo(hdc, x0, y1)
                win32gui.LineTo(hdc, x0, y1 - arm)
            elif shape == '下划线':
                inset = max(6, min(20, w // 12))
                win32gui.MoveToEx(hdc, x0 + inset, y1)
                win32gui.LineTo(hdc, x1 - inset, y1)
            elif shape == 'U形':
                win32gui.MoveToEx(hdc, x0, y0)
                win32gui.LineTo(hdc, x0, y1)
                win32gui.LineTo(hdc, x1, y1)
                win32gui.LineTo(hdc, x1, y0)
            else:   # 圆角 / 圆角立体 / 默认：整团一枚圆角包络
                _gdi32.RoundRect(hdc, x0, y0, x1, y1, 18, 18)
            win32gui.SelectObject(hdc, ob)
            win32gui.SelectObject(hdc, op)
        finally:
            win32gui.DeleteObject(pen)

    def _zone_label(self, hdc, text, x0, y0, x1, y1, cell_h, rgb, occupied=(),
                    corner=False):
        """分类名小牌。★R274：优先整块放到包络**外侧**（上→下），
        挪不出去才骑线，且骑线时是抖动纱牌——色键窗无 alpha，实心牌
        等于把第一行图标左上角那块桌面像素直接挖掉（修前实测 124px）。

        ★R288 画法=角贴（`corner=True`）：牌就贴在**团右上角**（不挪外侧、也不
        骑线），铺底固定走抖动纱那一支——`ZONE_LABEL_TEXTONLY` 那档是给"实在没
        地方放牌"的描边文字用的，角贴这一档要的正是"一枚看得清名字的贴角"，
        实心牌又会挖掉桌面像素，所以只有纱牌这一条路对得上用户的"角上那一枚"。"""
        px = max(11, min(16, cell_h // 8))
        fnt = self._font(px)
        old_f = win32gui.SelectObject(hdc, fnt)
        try:
            tw, th = win32gui.GetTextExtentPoint32(hdc, text)
            cw = tw + ZONE_LABEL_PAD * 2
            hh = (th + 8) // 2
            if corner:
                # 角贴：贴在团右上角，不查外侧、不骑线（★R288）
                cx0 = max(1, x1 - cw - 1)
                cy0 = max(0, y0 + 2)
                soft = True
            else:
                cx0 = x0 + 6
                if cx0 + cw > x1:
                    cx0 = max(1, x1 - cw - 1)
                cy0, soft = _label_band(cx0, cw, hh * 2, y0, y1,
                                        self.height, occupied)
            cy1 = cy0 + hh * 2
            null_pen = win32gui.GetStockObject(win32con.NULL_PEN)
            om = win32gui.SetBkMode(hdc, win32con.TRANSPARENT)
            ofg = win32gui.SetTextColor(hdc, win32api.RGB(18, 20, 26))
            try:
                if soft and ZONE_LABEL_TEXTONLY and not corner:
                    # 描边文字档：完全不铺底（贴顶那几格实在没地方放牌）
                    ty = max(0, y0 + 4 - th)
                    rim, cat = win32api.RGB(*_RIM), win32api.RGB(*rgb)
                    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                        win32gui.SetTextColor(hdc, rim)
                        win32gui.ExtTextOut(hdc, cx0 + ZONE_LABEL_PAD + dx,
                                            ty + dy, 0, None, text)
                    win32gui.SetTextColor(hdc, cat)
                    win32gui.ExtTextOut(hdc, cx0 + ZONE_LABEL_PAD, ty,
                                        0, None, text)
                else:
                    if soft:
                        st = self._select_dither(hdc, rgb, ZONE_LABEL_COVER)
                        op = win32gui.SelectObject(hdc, null_pen)
                        _gdi32.RoundRect(hdc, cx0, cy0, cx0 + cw, cy1, 10, 10)
                        win32gui.SelectObject(hdc, op)
                        self._restore_dc(hdc, st)
                    else:
                        chip = win32gui.CreateSolidBrush(win32api.RGB(*rgb))
                        try:
                            oc = win32gui.SelectObject(hdc, chip)
                            on = win32gui.SelectObject(hdc, null_pen)
                            _gdi32.RoundRect(hdc, cx0, cy0, cx0 + cw,
                                            cy1, 10, 10)
                            win32gui.SelectObject(hdc, on)
                            win32gui.SelectObject(hdc, oc)
                        finally:
                            win32gui.DeleteObject(chip)
                    win32gui.SetTextColor(hdc, win32api.RGB(18, 20, 26))
                    win32gui.ExtTextOut(hdc, cx0 + ZONE_LABEL_PAD,
                                        (cy0 + cy1 - th) // 2, 0, None, text)
            finally:
                win32gui.SetTextColor(hdc, ofg)
                win32gui.SetBkMode(hdc, om)
        except Exception as e:
            logger.error(f"分区标签绘制失败: {e}")
        finally:
            win32gui.SelectObject(hdc, old_f)

    def show(self):
        """显示窗口"""
        if self.hwnd:
            win32gui.ShowWindow(self.hwnd, win32con.SW_SHOW)

    def hide(self):
        """隐藏窗口"""
        if self.hwnd:
            win32gui.ShowWindow(self.hwnd, win32con.SW_HIDE)

    def destroy(self):
        """销毁窗口。

        ★R273：抖动图案刷与标签字体是**进程级 GDI 对象**（挂在窗对象上复用），
        窗口没了不释放就是 R193 那类泄漏；这里统一收口。"""
        if self.hwnd:
            win32gui.DestroyWindow(self.hwnd)
            self.hwnd = None
        for brush, bmp in list(self._dither_brushes.values()):
            try:
                _gdi32.DeleteObject(brush)
                win32gui.DeleteObject(bmp)
            except Exception as e:
                logger.error(f"overlay 抖动刷释放失败: {e}")
        self._dither_brushes.clear()
        # ★R289：斜纹带那枚图案刷一并释放（CreateHatchBrush 建的就得删，别留孤儿句柄）
        for brush in list(self._hatch_brushes.values()):
            try:
                _gdi32.DeleteObject(brush)
            except Exception as e:
                logger.error(f"overlay 图案刷释放失败: {e}")
        self._hatch_brushes.clear()
        for f in list(self._fonts.values()):
            try:
                _gdi32.DeleteObject(f)
            except Exception as e:
                logger.error(f"overlay 字体释放失败: {e}")
        self._fonts.clear()

    @staticmethod
    def _wndproc(hwnd, msg, w_param, l_param):
        """窗口过程"""
        if msg == 0x0014:  # WM_ERASEBKGND
            return 1
        return win32gui.DefWindowProc(hwnd, msg, w_param, l_param)

    def __del__(self):
        try:
            self.destroy()
        except Exception:
            pass
