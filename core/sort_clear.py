# -*- coding: utf-8 -*-
r"""★R242 #140「自动清排序」兜底（用户 32 批 item3 + 34 批 item2 清单§一）

## 这模块治什么

桌面图标"刷新一下就变乱、要开一下软件才摆回来"的真凶（★R234~★R239 实测）是
桌面**视图内存里的活动排序**（`SortColumns` 非空）：一旦有活动排序，Explorer
就认为自己拥有排列权，我们写进去的坐标会被它按排序重摆。

已实测排除的路（不要再试）：

* 写注册表 `Bag\1\Desktop\Sort` —— 那份是**镜像**，写零会被 Explorer 当场覆回；
* 右键菜单"排序方式"里的项 / "自动排列"开关 —— 都不清活动排序；
* 零副作用三招（空白单击 / ESC / 真空白拉框）—— 全不通。

**唯一实测可行**：合成"按住-移动-松手"把一个图标拖进空格（★R239 22:52 / 23:12
两轮成功，`SortColumns` 当场清空，之后点"全部"+刷新不再打回）。但它**同参数两次
跑一次成一次败**（#135 未定性），所以本模块的纪律是：

> 拖 → **COM 复核** → 没清就换一个源图标、换一个空格再拖，最多 5 次；
> 只有"排序读回为空"才算成功；成功后按基准布局**一次性把全部图标摆回**
> （★R245 A7），拿不到基准布局或摆位失败才退回"拖过的图标放回原格"。

## 用户口径（判据在 测试_ui/test_r242_sort_clear.py + test_r245_always_on.py）

1. **常开**（★R245 用户 37 批 item2 改判：「那个复选项不用要了，常开就行了」）：
   以前那道 `sort_clear_assist` 复选已经删掉，检测/主动提默认就在跑。
   ⚠ **常开 ≠ 自动动手**——下面第 3 条那道确认框从 ★R242 起一根手指都没松过。
2. **只读诊断先行**：读不到活动排序就**一句提示都不许出**；
   自动排列开着但没有活动排序 ⇒ 只给一句"请你手动取消'自动排列图标'"的建议，
   **绝不为此发鼠标消息**（win11 实测用户那台就是这种状态）。
3. **必须用户点确认**：弹「帮你修一下？」，点"否" ⇒ 注入次数恒 0、不谎报。
4. **结果必须当面说**（★R243）：手动按钮四种结果一律弹；守护触发只有真拖过才弹。

## 红线

* 本模块对注册表**只读**（读数一律走 `core.desk_view_state`，那里已经封了只读闸）；
* **绝不进** `core/dock.py::_wndproc`（纯 GDI，那里调 Tcl 会静默 native abort），
  也不在 `core/dock.py` 里留任何对本模块的引用；
* 顶层只 import 标准库：COM / ctypes 鼠标注入 / Tk 弹窗全部**函数内延迟导入**，
  这样"导入本模块"这件事本身永远不会碰桌面。
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("deskbits")

#: 用户口径"重试 ≤5"；门禁 G5 直接读这个常量
MAX_ATTEMPTS = 5
#: 每个会话最多主动提一次（抢鼠标的动作不能反复发生）
SESSION_MAX_OFFERS = 1
#: 两次主动提之间至少隔 10 分钟
COOLDOWN_SECONDS = 600
# ★R245（用户 37 批 item2「常开就行了」）：以前这里有个 `SETTING_KEY =
# "sort_clear_assist"`，配置里没勾就整条链路装死。用户判下来"默认关"这条**作废**，
# 老配置里残留的 False 也一律不再读（读到就当没有）。其余红线一条没松。

# ★R243 把合成拖动的节奏集中到这里；★R245（用户 37 批 item3"速度不满意"）
# 按 `run/r245/r245_speed.txt` 的实测换档：五档各 4 轮**成功率全 4/4**，
# 旧配方单次 2.23s → V1 1.24s → V2 0.82s → V3 0.47s → V4 0.49s。
# ★R246（用户 38 批 item2b"不可以更快吗？我看见慢悠悠的"）再压一档，
# 按 `run/r246/r246_b32.txt`：C2 最快档（配方和 0.31s）抢鼠标 0.37s、端到端 4/4，
# 但**不直接吃它**——★R239 #135 这配方本来就偶发落空，用户那台是更慢的 win11，
# 所以采"快一档退一步"（配方和 0.46~0.51s：settle_start .06 / hold .10 /
# aim .08 / settle_end .10）。跨机余量押在成功率的头上不值。
# 五步配方**一步都没砍**（★R239 实测：少任何一步都会偶发落空）：
#   settle_start  起手停一下，让 Explorer 认住光标
#   hold          按住后等系统进入"拖动模式"
#   jog_*         先小步抖几下越过系统拖动阈值（不动这 3 下就没后续）
#   step_*        分段移动到目标格（★R246：步数按位移算，见 `drag_steps_for`）
#   aim           落点停一下"瞄准"，松手才不会落到隔壁格
#   settle_end    松手后等 Explorer 真把排序清掉，再去回读
DRAG_TIMING = {
    "settle_start": 0.06,
    "hold": 0.10,
    "jog_steps": 3,
    "jog_delay": 0.03,
    "move_steps": 8,        # ★R246：这一档从此是**上限**，短距离走不满（见下）
    "move_steps_min": 3,    # 短位移最少步数（再少就成"一下甩过去"，★R239 没测过）
    "move_step_px": 45,     # 每段大约走多少像素
    "move_delay": 0.010,
    "aim": 0.08,
    "settle_end": 0.10,
}


# ★R334-c：拖动锚点（"按哪儿才拖得动"）。(17, 30) 是 ★R239 在 **48px 档**实测出来的
# "格子左上 → 图块热区"内缩。`get_icon_rect_by_name` 读回的是 `LVIR_BOUNDS`——**整块
# 条目**（图块 + 下面两行文字），图块只占上面那一条，所以这对数和图标尺寸是绑死的：
# 写死 ⇒ 16/32px 小图标档 `+30` 掉到图块**下面**的标签上（16px 档图块只覆盖 0..16），
# 192/256px 大图标档按在图块最上沿；轻则白抢一次鼠标报"没排好"，重则按到相邻条目拖走
# 别的图标。读不到尺寸（COM 失效 / 老桩具没这方法）⇒ 原样按 48 档，**不猜**。
DRAG_GRAB_BASE_PX = 48
DRAG_GRAB_INSET = (17, 30)
# 与 `IconManager.ICON_SIZE_MIN/MAX` 同一区间；这里不 import，避免拖动模块反向依赖 COM 层。
_ICON_PX_RANGE = (16, 256)


def _drag_inset(icon_px=None):
    """格子左上 → 拖动热区的内缩点；48px 档恒等于 ★R239 实测的 `(17, 30)`。"""
    try:
        px = int(icon_px)
    except (TypeError, ValueError):
        return DRAG_GRAB_INSET
    if not (_ICON_PX_RANGE[0] <= px <= _ICON_PX_RANGE[1]):
        return DRAG_GRAB_INSET
    return (max(1, int(round(px * DRAG_GRAB_INSET[0] / float(DRAG_GRAB_BASE_PX)))),
            max(1, int(round(px * DRAG_GRAB_INSET[1] / float(DRAG_GRAB_BASE_PX)))))


def drag_steps_for(distance_px, timing: Optional[dict] = None) -> int:
    """★R246（用户 38 批 item2a）：按位移长度决定分段步数。

    88px 还走 8 步＝每步 11px，看着就是"慢悠悠挪过去"；走 3 步一样有效
    （`run/r246/r246_b32.txt`：短位移 + 3 步 端到端 4/4）。距离算不出来
    （None / 脏值）一律退回**上限步数**——宁可慢，不可少一步。
    """
    t = DRAG_TIMING if timing is None else timing
    cap = int(t.get("move_steps", 8))
    lo = min(cap, max(1, int(t.get("move_steps_min", 3))))
    per = int(t.get("move_step_px", 45))
    try:
        d = float(distance_px)
        if not (d >= 0.0):                       # NaN / 负数都算脏
            raise ValueError
    except Exception:
        return cap
    if per <= 0:
        return cap
    return max(lo, min(cap, int(-(-d // per))))  # ceil(d / per)


def drag_seconds(timing: Optional[dict] = None) -> float:
    """一次合成拖动**最多**占住鼠标多久（秒）。

    只算我们自己 sleep 的部分，不含系统响应，所以是"上限"而非实测——
    给用户看的"约 N 秒"和门禁的时长上限都用它（判据可比，不靠人肉数 sleep）。
    ★R246 起 `move_steps` 是**步数上限**（短距离走 `move_steps_min` 步），
    所以这个数按上限算＝最坏情况；实测典型值见 `run/r246/r246_b32.txt`。
    """
    t = DRAG_TIMING if timing is None else timing
    return (t["settle_start"] + t["hold"]
            + t["jog_steps"] * t["jog_delay"]
            + t["move_steps"] * t["move_delay"]
            + t["aim"] + t["settle_end"])


def drag_seconds_text(timing: Optional[dict] = None) -> str:
    """给用户看的时长说法：向上取整到 0.5 秒，别写"约 2.06 秒"。"""
    import math
    secs = drag_seconds(timing)
    return "%.1f" % (math.ceil(secs * 2) / 2.0)


# 测试取证旗标：真实 COM / 真实鼠标注入一旦被调用就置 1。
# 门禁 B18 要求"全程走桩具"⇒ 跑测试时这两个必须恒为 0。
_REAL_COM_CALLED = 0
_REAL_DRAG_CALLED = 0

_Point = Tuple[int, int]
_Cell = Tuple[int, int]


def _t(zh: str, en: str) -> str:
    """按当前语言取一句**弹窗/提示**文案（English 档不许弹中文框，★R233 同口径）。

    `utils.i18n` 走函数内延迟导入：本模块顶层只准依赖标准库（红线——"导入本模块"
    这件事本身不许碰桌面/Tk）。取不到语言开关就退回中文原文，绝不抛异常。
    日志行不用这里：那些走 `utils/log_i18n` 的显示层短语典（方案 A 口径）。
    """
    try:
        from utils import i18n
        return i18n.tr(zh, en)
    except Exception:
        return zh


# =====================================================================
# 一、只读诊断
# =====================================================================
def _com_sort_state() -> Optional[dict]:
    """视图内存里的活动排序（COM 权威读数）。拿不到就返回 None，不抛。

    通道：`Shell.Application` → `Windows().FindWindowSW(桌面)` → `Document.SortColumns`
    （★R239 的探针用的就是这条，`_r239_drag.py:sort_columns()`；比 QI IFolderView2
    靠谱——那条在本机 QI 直接返回"不支持此接口"，见 run/probe17.txt）。
    """
    global _REAL_COM_CALLED
    _REAL_COM_CALLED = 1
    try:
        import pythoncom
        from win32com.client import dynamic
        try:
            pythoncom.CoInitialize()
        except Exception:
            pass                                    # 已初始化过也算 ok
        app = dynamic.Dispatch("Shell.Application")
        win = app.Windows().FindWindowSW(None, "", 8, 0, 1)   # 32 = CSIDL_DESKTOP
        raw = getattr(win.Document, "SortColumns", None)
        cols = [c.strip() for c in str(raw or "").split(";") if c.strip()]
        return {"sorted": bool(cols),
                "sort_column": (cols[0].replace(" ", "").lower() if cols else None)}
    except Exception as e:
        logger.debug("[sort_clear] COM read unavailable: %s", type(e).__name__)
        return None


def _registry_state() -> dict:
    """注册表 bag 镜像读数（只读模块，本函数不碰注册表 API）。"""
    try:
        from core import desk_view_state as dvs
        return dvs.read_desk_view_state()
    except Exception as e:
        return {"ok": False, "err": "import %s" % type(e).__name__}


def read_sort_state(com_reader: Optional[Callable[[], Optional[dict]]] = None,
                    registry_reader: Optional[Callable[[], dict]] = None) -> dict:
    """读"桌面现在到底有没有活动排序 / 自动排列开没开"。

    返回 ``{ok, source, sorted, sort_column, autoarrange, fflags, err}``：

    * ``source``：``"com"``＝视图内存权威；``"registry"``＝只拿到镜像兜底；
      ``"none"``＝两路都没读到 —— 这时 ``ok=False``，调用方**必须**当"不知道"，
      绝不许把"读不到"当成"没排序"（判据 B3）。
    * COM 通道成功时，``sorted`` 一律以 COM 为准（注册表镜像可能滞后）；
      ``autoarrange/fflags`` 用镜像补：本机只读探针实测镜像 == 视图内存
      （见 run/r242/fwf_probe.txt），所以这一位可信。
    """
    out = {"ok": False, "source": "none", "sorted": False, "sort_column": None,
           "autoarrange": False, "fflags": 0, "err": ""}
    reg = {}
    try:
        reg = (registry_reader or _registry_state)() or {}
    except Exception as e:                            # 兜底不许外抛
        out["err"] = "registry %s" % type(e).__name__
    if reg.get("ok"):
        out["autoarrange"] = bool(reg.get("autoarrange"))
        out["fflags"] = int(reg.get("fflags") or 0)
        out["sorted"] = bool(reg.get("sorted"))
        out["sort_column"] = reg.get("sort_column")
        out["source"] = "registry"
        out["ok"] = True
    com = None
    try:
        com = (com_reader or _com_sort_state)()
    except Exception as e:
        out["err"] = (out["err"] + " ; " if out["err"] else "") + \
            "com %s" % type(e).__name__
    if com and "sorted" in com:
        out["sorted"] = bool(com.get("sorted"))
        out["sort_column"] = com.get("sort_column")
        out["source"] = "com"
        out["ok"] = True
    elif out["source"] == "none":
        out["err"] = (out["err"] + " ; " if out["err"] else "") + \
            "both readers unavailable"
    return out


def autoarrange_advisory(state: dict) -> str:
    """"自动排列开着、但没有活动排序"时给的一句建议；不该说时返回空串。

    为什么单列一个函数：这种状态下**不该发鼠标消息**（清排序治不了它），
    能做的只有告诉用户去哪儿关。win11 回传日志正是这种情况。
    """
    if not state or not state.get("ok") or not state.get("autoarrange"):
        return ""
    if state.get("sorted"):
        return ""            # 有活动排序时走 offer 那条路，文案里已经带上这句
    return _t("桌面开着「自动排列图标」，这个开关会让布局摆不住。"
              "请在桌面空白处右键 → 查看 → 取消勾选「自动排列图标」。",
              "The desktop has \u201cAuto arrange icons\u201d switched on, and that "
              "switch is what keeps the layout from holding. Right-click an empty "
              "spot on the desktop \u2192 View \u2192 untick \u201cAuto arrange icons\u201d.")


# ── ★R246 B5-①/③：视图状态进存档 + 偏差归因（用户 38 批 item1「值得参考的
#    都带进代码」；做法取自 openFrence 的 `keep_sort`，但**只读版**）──────────
#
# 为什么要这一小段：★R239/R244/R245 一路实测下来，"布局变乱"只有两种成因
#   ① 视图被"排序方式"接管（写进去当场生效、刷新就打回）；
#   ② 桌面开着"自动排列图标"（写进去直接被拒，实测 ret=0）。
# 但历轮的布局存档里**从没记过当初是哪一种**——win11 回传那份存档就没法判读
# "当初是干净抄的还是排完序抄的"（#179 交付文档里挂的那条）。记下来＝把判读
# 从"猜"变成"查"。openFrence 用 keep_sort 是"替他记住并替他改"，我们只记不改：
# 自动排列那个开关要不要替他动，用户在 ★R242 明确要等点头（#161/#165 口径）。
#
# 三条硬规矩（门禁 test_r246_view_stamp.py 逐条钉）：
#   · **纯只读**：读数一律走 `read_sort_state`（COM 权威 + 注册表镜像），
#     本段不写注册表、不发消息、不碰鼠标；
#   · 读不到 ⇒ 返回 ``{}`` ⇒ 调用方**整个键都不写**（比写个 null 好：
#     老存档本来就没这一项，回放按"没记"处理是正常路径，★R245 A2 同口径）；
#   · 归因必须有"读不到=不知道"这一档，绝不把"不知道"说成"被忽略"。
ARCHIVE_STAMP_KEYS = ("sorted", "sort_column", "autoarrange", "source")

CAUSE_AUTOARRANGE = 'autoarrange'    # 系统把位置拉回网格（写进去也没用）
CAUSE_VIEW_SORT = 'view-sort'        # 视图被排序方式接管（刷新就打回）
CAUSE_IGNORED = 'ignored'            # 两道闸门都关着 ⇒ 这次写入没生效
CAUSE_UNKNOWN = 'unknown'            # 读不到状态 ⇒ 只能说不知道


def archive_stamp(state: Optional[dict] = None) -> dict:
    """把"当时的活动排序列 + 自动排列"翻成可直接进存档/进日志的小 dict。

    `state=None` 时自己去读一次（COM + 注册表镜像，两路都只读）。
    读不到 / 读的过程炸了 ⇒ ``{}``：调用方据此**不写这个键**，绝不用
    ``None`` 占位（判据 B2/B4/B5）。返回值只含标准可 JSON 化的类型（B6）。
    """
    try:
        st = read_sort_state() if state is None else (state or {})
    except Exception as e:                            # 诊断不许炸主流程
        logger.debug("[sort_clear] archive_stamp unavailable: %s", type(e).__name__)
        return {}
    try:
        if not st.get("ok"):
            return {}
        return {"sorted": bool(st.get("sorted")),
                "sort_column": st.get("sort_column") or None,
                "autoarrange": bool(st.get("autoarrange")),
                "source": st.get("source") or "none"}
    except Exception:
        return {}


def stamp_from_archive(data: Optional[dict]) -> dict:
    """从**布局存档 dict** 里取当初记的视图状态（回放侧的唯一入口）。

    老存档／脏值／值为 null 一律给 ``{}``（判据 B8/B9）——"没记"是正常情况，
    调用方按"不知道当初"处理，绝不拿现在的状态冒充当初的。
    """
    raw = data.get("view_state") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return {}
    col = raw.get("sort_column")
    return {"sorted": bool(raw.get("sorted")),
            "sort_column": col if isinstance(col, str) and col else None,
            "autoarrange": bool(raw.get("autoarrange")),
            "source": raw.get("source") if isinstance(raw.get("source"), str)
            else "archive"}


def stamp_text(stamp: Optional[dict]) -> str:
    """一行 ASCII 摘要，存档侧与现值侧共用（日志/取证都能直接 grep）。

    ``view=unknown`` ＝ 压根没读到；否则 ``view=sort:<列> autoarrange:<0|1>
    src:<com|registry|archive>``。全 ASCII ⇒ 控制台 GBK 下也不会打印崩。
    """
    if not stamp:
        return "view=unknown"
    return "view=sort:%s autoarrange:%d src:%s" % (
        stamp.get("sort_column") or "none",
        int(bool(stamp.get("autoarrange"))),
        stamp.get("source") or "?")


def drift_cause(state: Optional[dict] = None,
                autoarrange: Optional[bool] = None) -> str:
    """写后回读不符时"到底是谁干的"——返回四个代号之一，绝不抛。

    * `autoarrange`：**窗口样式位**的读数（`IconManager.get_auto_arrange()`），
      True/False 都有分量，None＝没读到 ⇒ 退回注册表镜像那一位（判据 B13）。
    * 优先级：自动排列 > 活动排序 > 写入被忽略。理由——自动排列开着时
      `LVM_SETITEMPOSITION` 实测直接返回 0（★R234），排序那条根本轮不到。
    * 两路都读不到 ⇒ `unknown`，**不许**报成 `ignored`（判据 B16）。
    """
    try:
        st = read_sort_state() if state is None else (state or {})
    except Exception as e:
        logger.debug("[sort_clear] drift_cause read failed: %s", type(e).__name__)
        return CAUSE_UNKNOWN
    aa = autoarrange
    if aa is None:
        aa = bool(st.get("autoarrange")) if st.get("ok") else None
    if aa is True:
        return CAUSE_AUTOARRANGE
    if not st.get("ok"):
        return CAUSE_UNKNOWN
    if st.get("sorted"):
        return CAUSE_VIEW_SORT
    return CAUSE_IGNORED


def drift_text(cause: str, sort_column: Optional[str] = None) -> str:
    """归因代号 → 给用户看的那一句（中文原文；English 走 `utils/log_i18n` 短语典）。

    口径（★R244 #179 与 openFrence 那条一致）：**先说清是谁在动、再说清
    "不是布局坏了"**——用户看到"图标又乱了"第一反应是存档有问题，历轮沟通
    一半时间花在解释这件事上。
    """
    if cause == CAUSE_AUTOARRANGE:
        return (u'位置被系统拉回网格：桌面开着「自动排列图标」，'
                u'写进去的位置会被它改掉——不是布局存档坏了')
    if cause == CAUSE_VIEW_SORT:
        return (u'位置被排序方式接管：桌面当前按「%s」排序，'
                u'刷新一下就会被打回——不是布局存档坏了'
                % (sort_column or u'未知列'))
    if cause == CAUSE_IGNORED:
        return (u'位置写入被忽略：自动排列没开、也没有活动排序，'
                u'是 Explorer 这次没接住写入（可再套一次布局）')
    return u'读不到桌面视图状态，偏差原因未知（不当成"没问题"）'


def should_offer(state: dict, settings: Optional[dict] = None) -> bool:
    """该不该弹「帮你修一下？」——读不到 / 没活动排序就不弹。

    ★R245：`settings` 形参留着只为兼容老调用点（主窗仍然把自己的设置传进来），
    **不再当闸口**——用户 37 批 item2 判的是"常开"。真正还在把关的是：
    读得到桌面视图（``ok``）、确实有活动排序（``sorted``），以及后面那道确认框。
    """
    if not state or not state.get("ok"):
        return False                                  # 读不到 ⇒ 不动手
    return bool(state.get("sorted"))                  # 只有活动排序才是目标


def allow_offer(now: Optional[float] = None, last_offer_ts: float = 0.0,
                offers_this_session: int = 0,
                cooldown: Optional[float] = None) -> bool:
    """节流：每会话最多主动提一次 + 两次之间隔冷却时间（手动按钮不受限）。"""
    if offers_this_session >= SESSION_MAX_OFFERS:
        return False
    t = time.time() if now is None else float(now)
    gap = COOLDOWN_SECONDS if cooldown is None else float(cooldown)
    if last_offer_ts and (t - float(last_offer_ts)) < gap:
        return False
    return True


# =====================================================================
# 二、重试计划
# =====================================================================
def _dist_pt(a: _Point, b: _Point) -> float:
    try:
        return ((float(a[0]) - float(b[0])) ** 2 +
                (float(a[1]) - float(b[1])) ** 2) ** 0.5
    except (TypeError, ValueError, IndexError):
        return float('inf')


def pair_by_min_distance(positions: Dict[str, _Point],
                         free_cells: Sequence[_Cell],
                         cell_pt: Callable[[_Cell], _Point],
                         max_attempts: int = MAX_ATTEMPTS
                         ) -> List[Tuple[str, _Cell, _Point]]:
    """★R246（用户 38 批 item2a「找最短的路径来拖…越短越好」）：贪心最近配对。

    每一手都在"没用过的源图标 × 没用过的空格"里挑**位移最短**的那一对。
    老配法按名字长度先来，第 1 手经常要横穿大半张桌子（实测 310px），
    最短配法同样这张桌子只要 88px（`run/r246/r246_b32.txt` C1：端到端 4/4）。
    距离要用 `cell_pt` 把格子换算成像素——必须是和 `_grid_cells` **同一份标定
    几何**，否则"离得近"和"落点"各算各的。平手按 (距离, 名字, 格子) 排，可复现。
    """
    if not positions or not free_cells or cell_pt is None:
        return []
    cells: List[_Cell] = []
    seen = set()
    for c in free_cells:
        try:
            key = (int(c[0]), int(c[1]))
        except (TypeError, ValueError, IndexError):
            continue
        if key not in seen:
            seen.add(key)
            cells.append(key)
    left_n = [k for k in positions]
    used_c = set()
    plan: List[Tuple[str, _Cell, _Point]] = []
    for _i in range(min(max(0, int(max_attempts)), MAX_ATTEMPTS)):
        best = None
        for n in left_n:
            p = positions[n]
            for c in cells:
                if c in used_c:
                    continue
                d = _dist_pt(p, cell_pt(c))
                if d == float('inf'):
                    continue
                key = (d, str(n), c)
                if best is None or key < best[0]:
                    best = (key, n, c)
        if best is None:
            break
        left_n.remove(best[1])
        used_c.add(best[2])
        plan.append((best[1], best[2], positions[best[1]]))
    return plan


def build_plan(positions: Dict[str, _Point], free_cells: Sequence[_Cell],
               max_attempts: int = MAX_ATTEMPTS,
               cell_pt: Optional[Callable[[_Cell], _Point]] = None
               ) -> List[Tuple[str, _Cell, _Point]]:
    """排"最多 5 次"的尝试表：每条 = (源图标, 目标空格, 拖动前实测坐标)。

    纪律（判据 B9/B9b/B9c）：条数 ≤ MAX_ATTEMPTS；源图标不重复、目标格不重复、
    (源, 格) 组合自然也不重复；每条把**拖动前实测坐标**带着，回位只用它、不靠猜。
    ★R246：给了 `cell_pt`（格子→像素）就走"位移最短"配法；不给＝按"名字短的先来"
    老顺序（判据 B1：老调用点/老门禁行为一字不变，向后兼容）。
    """
    if cell_pt is not None:
        return pair_by_min_distance(positions, free_cells, cell_pt, max_attempts)
    if not positions or not free_cells:
        return []
    plan: List[Tuple[str, _Cell, _Point]] = []
    names = sorted(positions, key=lambda n: (len(str(n)), str(n)))
    cells: List[_Cell] = []
    seen = set()
    for c in free_cells:
        try:
            key = (int(c[0]), int(c[1]))
        except (TypeError, ValueError, IndexError):
            continue
        if key not in seen:
            seen.add(key)
            cells.append(key)
    for i in range(min(max(0, int(max_attempts)), MAX_ATTEMPTS)):
        if i >= len(names) or i >= len(cells):
            break
        name = names[i]
        plan.append((name, cells[i], positions[name]))
    return plan


# =====================================================================
# 三、动手（可注入：drag / verify / restore 全部可由调用方替换）
# =====================================================================
def attempt_clear(plan: Sequence[Tuple[str, _Cell, _Point]],
                  drag: Callable[[str, _Cell], bool],
                  verify: Callable[[], bool],
                  restore: Callable[[str, _Point], None],
                  log: Optional[Callable[..., None]] = None,
                  rebatch: Optional[Callable[
                      [Sequence[Tuple[str, _Point]]], object]] = None,
                  repick: Optional[Callable[
                      [str, _Cell], Optional[_Cell]]] = None,
                  interfere: Optional[Callable[[], bool]] = None) -> dict:
    """按重试表拖图标，**每次拖完都复核**，复核一过就停。

    判据落点（★R242 门禁 `test_r242_sort_clear.py`）：
      * B10 —— 第 2 次复核通过就只拖 2 下（绝不把剩下 3 次拖完）；
      * B11 —— 全不过 ⇒ ``ok=False``，并写一条"没修好 + 原因"的日志（不谎报、不静默）；
      * B12 —— 中途抛异常，``finally`` 也要把拖过的图标放回原格。

    ``repick``（★R246 #197，判据 B11~B15）：**发鼠标之前再核一次落点还是不是空的**。
    ★R246 诊断（`run/r246/r246_diag.txt`）：造病后视图要落定一会儿，之前取的"空格"
    实时可能站着别的图标（那次实测 (1,0) 里是 Google Chrome）——往有人占着的格子上
    拖＝拖了个寂寞（``moved=False``、排序没清），★R239 #135 的"同参数一次成一次败"
    多半就是它。约定：返回**要用的格子**（可以还是原来那个）；返回 ``None`` =
    换不到空格 ⇒ 这一手**一发鼠标都不发**（宁可不动手，不往别人图标身上拖）；
    它自己抛异常 ⇒ 退回计划里的落点（跟 ★R242 一样），不许把整条修复带崩。

    ``rebatch``（★R245 A7，用户插话 item2「不用拖回去了……把所有的图标摆成布局
    的位置」）：清排序**成功**时改调它一次，让它把整份布局批量摆回去（本机实测
    摆 26 个 1.0 秒，且 F5 / 重启 Explorer 都不再掉，见 run/r245/r245_a7.txt）。
    它拿到的是"拖过的图标 + 拖动前实测坐标"，所以就算不批量摆也摆得回去。
    **三条硬约束**（判据 B4~B7）：① 只有 ``ok`` 才允许顶替回位；② 它抛异常或
    返回假值 ⇒ 立刻退回逐图标回位；③ 没给它 ⇒ 行为与 ★R242 完全一致。

    ``interfere``（★R249 一2③）：每**发完一手鼠标**之后探一次左键（生产传
    `left_button_down`，`GetAsyncKeyState` 巡检，**不是钩子**）。探到"我们松手了、
    左键还按着"＝用户这一手想去抢鼠标 ⇒ 结果里 ``interfered=True``，调用方据此
    累计"连续几次想去动鼠标"。不给 ⇒ 不探、恒假；它自己抛 ⇒ 只记一句日志，
    **读数绝不能变成新的故障源**（判据 S4）。
    """
    done: List[Tuple[str, _Cell, _Point]] = []
    attempts: List[dict] = []
    ok = False
    err = ""
    relaid = False
    interfered = False

    def _log(*a):
        if log:
            try:
                log(*a)
            except Exception:
                pass

    def _probe_interfere():
        """左键读数：只在**我们自己松手之后**问一次，任何异常都吞掉。"""
        nonlocal interfered
        if interfere is None:
            return
        try:
            if interfere():
                interfered = True
                _log("sort-clear interfere: 松手后左键仍按着（用户想去动鼠标）")
        except Exception as e:
            _log("sort-clear interfere probe failed err=%s" % type(e).__name__)

    try:
        for name, cell, orig in list(plan)[:MAX_ATTEMPTS]:
            use = cell
            if repick is not None:
                try:
                    got = repick(name, cell)
                except Exception as e:
                    got = cell                    # 复核器自己炸了 ⇒ 照计划来
                    _log("sort-clear pre-check failed err=%s (use planned cell)"
                         % type(e).__name__)
                if got is None:
                    _log("sort-clear skip: 落点被占且换不到空格，这一手不发鼠标 "
                         "icon=%s cell=%s" % (name, cell))
                    attempts.append({"icon": name, "cell": list(cell),
                                     "cleared": False, "skipped": True})
                    continue
                try:
                    if tuple(got) != tuple(cell):
                        _log("sort-clear repick: 落点被占，换格 %s -> %s" % (
                            cell, got))
                except Exception:
                    pass
                use = got
            _log("sort-clear try %d: icon=%s -> cell=%s" % (
                len(done) + 1, name, use))
            done.append((name, use, orig))            # 先登记，保证异常也回位
            drag(name, use)
            _probe_interfere()                        # ★R249 一2③：紧接着探左键
            if verify():
                ok = True
                attempts.append({"icon": name, "cell": list(use),
                                 "cleared": True})
                _log("sort-clear ok: view sort cleared on try %d" % len(done))
                break
            attempts.append({"icon": name, "cell": list(use), "cleared": False})
    except Exception as e:
        err = "%s: %s" % (type(e).__name__, e)
        raise
    finally:
        if ok and rebatch is not None:
            # ★R245 A7：成功 ⇒ 一次性摆全布局，不再一个图标一个图标拖回去
            try:
                relaid = bool(rebatch([(n, xy) for n, _c, xy in done]))
            except Exception as e:
                relaid = False
                _log("sort-clear relayout failed err=%s" % type(e).__name__)
        if not relaid:
            # 顺序反过来放：最后拖动的最先回位，避免互相压格
            for name, _cell, orig in reversed(done):
                try:
                    restore(name, orig)
                except Exception as e:
                    _log("sort-clear restore failed icon=%s err=%s" % (
                        name, type(e).__name__))
        else:
            _log("sort-clear relaid: 已按基准布局一次性摆回，未逐个拖回")
        if not ok:
            _log("没修好：拖了 %d 次，桌面视图里的活动排序还在%s" % (
                len(done), ("（原因：%s）" % err) if err else ""))
    return {"ok": bool(ok), "attempts": attempts, "relaid": bool(relaid),
            "dragged": len(done), "err": err, "interfered": bool(interfered)}


# =====================================================================
# 四、真实环境的 drag / verify / restore（测试里一律被桩具替换）
# =====================================================================
def _grid_cells(im, positions: Dict[str, _Point], geom=None) -> List[_Cell]:
    """找几个"当前没被任何图标占住"的格子（拖动的落点）。

    ★R237 的教训在这里同样成立：几何必须用 `samples=positions` **标定过**的
    那一份，否则算出来的"空格"和 Explorer 真正摆得下的格子不是同一批。
    返回的格子和这个 geom 绑定，后面 `_real_drag` 要用同一个 geom 反算落点。
    """
    try:
        geom = geom or im._grid_geometry(samples=positions)
        cx, cy, _ox, _oy, vw, vh = geom
    except Exception:
        return []
    occupied = set()
    for xy in positions.values():
        try:
            occupied.add(im._cell_index(tuple(xy), geom))
        except Exception:
            continue
    cols = max(1, int(vw // max(1, cx)))
    rows = max(1, int(vh // max(1, cy)))
    out: List[_Cell] = []
    for r in range(rows):
        for c in range(cols):
            if (c, r) not in occupied:
                out.append((c, r))
            if len(out) >= MAX_ATTEMPTS * 4:
                return out
    return out


def _make_repick(im, geom, log: Optional[Callable[..., None]] = None):
    """★R246 #197：发鼠标**之前**用实时坐标再核一次落点空不空，被占就换最近的空格。

    为什么必须有这一步（`run/r246/r246_diag.txt` 的教训）：计划表是提前排的，
    而桌面视图在排序生效后要落定一会儿——期间算出来的"空格"实时可能站着别的图标
    （实测 (1,0) 里是 Google Chrome）。往有人占着的格子上拖，Explorer 不认为这是
    "拖到空白"，排序**不会清**（`moved=False`），白抢一次鼠标还以为是 #135 偶发落空。

    读的是 `_read_all_positions_live`（视图实测），**不是** `get_current_icon_positions`
    那份缓存——门禁 B17 就钉这个。返回 ``None`` 表示"换不到空格"，调用方那一手
    就不发鼠标；任何读不到的情况（拿不到视图/几何没标定/图标不见了）都退回
    原计划落点，绝不因为"复核器不灵"而谎报没空格。
    """
    def _repick(name: str, cell: _Cell):
        try:
            live = im._read_all_positions_live() or {}
        except Exception:
            return cell
        if not live:
            return cell
        try:
            tgt = (int(cell[0]), int(cell[1]))
        except (TypeError, ValueError, IndexError):
            return cell
        occupied = set()
        for xy in live.values():
            try:
                occupied.add(tuple(im._cell_index(tuple(xy), geom)))
            except Exception:
                continue
        if tgt not in occupied:
            return cell                      # 还是空的，照计划拖
        # 被占了 ⇒ 用同一份几何重算空格，挑离这个图标**实时位置**最近的一格
        free = _grid_cells(im, live, geom)
        if not free:
            return None
        pt = live.get(name)
        if pt is None:
            return None                      # 图标都不在了，别拿它试
        best, bd = None, None
        for c in free:
            try:
                ox, oy = im._cell_origin(c, geom)
            except Exception:
                return cell                  # 换算不了就当没这回事，照计划来
            d = _dist_pt(pt, (ox, oy))
            if bd is None or (d, c) < (bd, best):
                best, bd = c, d
        return best if best is not None else cell
    return _repick


def _cell_pt_of(im, geom):
    """给 `build_plan` 用的"格子→像素"换算器；几何拿不到就返回 None（退回老配法）。"""
    if geom is None:
        return None

    def _pt(cell):
        try:
            x, y = im._cell_origin(tuple(cell), geom)
            return (int(x), int(y))
        except Exception:
            return (10 ** 9, 10 ** 9)        # 换算不了 ⇒ 排到最后，别抢短路径
    return _pt


def _real_drag(im, name: str, cell: _Cell, geom=None) -> bool:
    """合成"按住-移动-松手"把某个图标拖到目标格（★R239 实测配方）。

    会**抢鼠标约 `drag_seconds()` 秒**，所以只在用户点过"是"之后才允许调用。
    `geom` 要和 `_grid_cells` 用的是同一份标定几何，否则"目标格"会算偏。
    节奏全在 `DRAG_TIMING`（★R243 集中化：五步一步不砍，只压时长）。
    """
    global _REAL_DRAG_CALLED
    _REAL_DRAG_CALLED = 1
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    LDOWN, LUP, MOVE = 0x0002, 0x0004, 0x0001
    T = DRAG_TIMING
    try:
        rect = im.get_icon_rect_by_name(name)
        if not rect:
            return False
        # ★R334-c：内缩点跟着桌面图标尺寸走。尺寸**读失败不算异常**——按 48 档继续拖，
        # 别让"COM 恰好取不到 IconSize"把整次排序修复取消掉（那是更糟的降级）。
        _gs = getattr(im, 'get_icon_size', None)
        try:
            _px = _gs() if callable(_gs) else None
        except Exception:
            _px = None
        _ix, _iy = _drag_inset(_px)
        gx, gy = int(rect[0]) + _ix, int(rect[1]) + _iy
        tx, ty = im._cell_origin(tuple(cell), geom)
        tx, ty = int(tx) + _ix, int(ty) + _iy
    except Exception:
        return False
    u.SetCursorPos(int(gx), int(gy))
    time.sleep(T["settle_start"])
    u.mouse_event(LDOWN, 0, 0, 0, 0)
    # ★R284-1（P1）：左键一旦按下，之后任一步抛异常（drag_steps_for 的算术/参数、
    #   外部中断等）都必须把键松开——否则全系统卡在"左键一直按住"，用户点哪都像在
    #   拖拽，与 ★R229 记事本"Alt 一直按住"同族、只是换成鼠标左键；且本函数跑在 UI
    #   线程上，异常被 Tk 兜住不崩，用户只会看到"鼠标坏了"更难归因。move 序列放 try，
    #   LUP 放 finally 无条件补发；异常照旧向外传播（不掩盖真错）。
    try:
        time.sleep(T["hold"])
        for d in (3, 6, 9):
            u.SetCursorPos(int(gx + d), int(gy + d // 2))
            u.mouse_event(MOVE, 0, 0, 0, 0)
            time.sleep(T["jog_delay"])
        steps = drag_steps_for(((tx - gx) ** 2 + (ty - gy) ** 2) ** 0.5, T)
        for i in range(1, steps + 1):
            t = i / float(steps)
            u.SetCursorPos(int(gx + (tx - gx) * t), int(gy + (ty - gy) * t))
            u.mouse_event(MOVE, 0, 0, 0, 0)
            time.sleep(T["move_delay"])
        time.sleep(T["aim"])
    finally:
        u.mouse_event(LUP, 0, 0, 0, 0)
    time.sleep(T["settle_end"])
    return True


def _make_verifier():
    """复核器：只认真实读数——活动排序读回为空才算清掉了。"""
    def _verify() -> bool:
        st = read_sort_state()
        return bool(st.get("ok")) and not st.get("sorted")
    return _verify


def _make_restorer(im, log: Optional[Callable[..., None]] = None):
    def _restore(name: str, xy: _Point) -> None:
        try:
            im.apply_positions({name: (int(xy[0]), int(xy[1]))})
        except Exception as e:
            if log:
                log("sort-clear apply back failed icon=%s err=%s" % (
                    name, type(e).__name__))
    return _restore


# =====================================================================
# 五、文案 + 总入口
# =====================================================================
#: 系统读回来的"按哪一列排"是一串内部标识：COM 的 `SortColumns` 给
#: `prop:System.ItemNameDisplay`（`_com_sort_state` 去空格转小写），注册表解码
#: 给 `name` / `date-modified-desc` / `pid-14` 这类标签
#: （`core/desk_view_state.PID_LABEL`）。★R249 一3a 用户原话：「弹出的对话框
#: 里面，没必要出现排序=一串英文，直接大白话解释意思」⇒ 弹窗只说下面这些词。
#: 只收**桌面右键「排序方式」菜单里本来就有的那几列**，没把握的一律不硬编。
_SORT_COL_WORDS = [
    (('name', 'itemname', 'itemnamedisplay', 'title', 'filename',
      'namemodified', 'name-legacy'), u'名称', 'Name'),
    (('modified', 'datemodified', 'date-modified', 'write', 'datesaved',
      'lastwrite'), u'修改日期', 'Date modified'),
    (('size', 'filesize'), u'大小', 'Size'),
    (('type', 'itemtype', 'folderinformation'), u'类型', 'Type'),
    (('created', 'datecreated', 'createnedate'), u'创建日期', 'Date created'),
    (('attrib', 'attributes'), u'属性', 'Attributes'),
    (('group', 'itemtypegroup'), u'分组', 'Group'),
]


def sort_column_words(col) -> Tuple[str, str]:
    """把系统那串列名翻成 ``(中文, English)``；**认不出来返回 ``('', '')``**。

    纯函数、不碰桌面、任何输入都不抛（★R249 一3a）。剥掉的都是"系统写法"不是
    信息：`prop:` / `system.` / 空格 / 尾随 `;` 是格式，`-desc`、`dir=-1` 是方向。
    方向并进词里（「大小（倒着排）」），这样调用方永远只需要填一格文字，
    不会出现"半句中文半句英文"。认不出来时调用方必须改口说"系统没说是按
    哪一列"——**绝不许把原始串怼进弹窗**；要看原始串去日志页那行 `sort=…`
    （`desk_view_state.summarize`，★R229 方案 A：中文日志一个字不改）。
    """
    try:
        raw = str(col or '').strip().lower()
    except Exception:
        return '', ''
    if not raw:
        return '', ''
    rev = False
    if 'dir=-1' in raw:
        rev, raw = True, raw.replace('dir=-1', '')
    for _suf in ('-desc', ';desc', ' desc', '_desc'):
        if raw.endswith(_suf):
            rev, raw = True, raw[:-len(_suf)]
            break
    base = (raw.replace('prop:', '').replace('system.', '')
               .replace(' ', '').strip(' ;,-'))
    if not base:
        return '', ''
    zh_tail = u'（倒着排）' if rev else ''
    en_tail = ' (reversed)' if rev else ''
    for keys, zh, en in _SORT_COL_WORDS:
        if base in keys:
            return zh + zh_tail, en + en_tail
    # 兜底：`parsingname` 这类带前缀的复合名，认得出主干就说主干
    for keys, zh, en in _SORT_COL_WORDS:
        if any(len(k) > 3 and k in base for k in keys):
            return zh + zh_tail, en + en_tail
    return '', ''


def _offer_text(state: dict) -> str:
    """确认框文案（必须让用户看清"要抢鼠标、会挪一个图标、并且自动排列那条路）。

    ★R242：English 档一样要看得懂——每句走 `_t()` 取双语，中文原文一个字不删
    （门禁 G9 就是按中文字面量判"有没有点名自动排列"）。
    ★R249 一3a：第一行不再怼 `prop:system.itemnamedisplay` 这种内部串，改说
    大白话（认不出来就承认"没说按哪一列"）；`（来源：桌面视图/桌面设置…）`
    那格诊断从弹窗拿掉——它只对排查有意义，日志页每轮都写着同一份读数。
    """
    col_zh, col_en = sort_column_words(state.get("sort_column"))
    secs = drag_seconds_text()
    lines = [
        _t("检测到桌面现在按「%s」排着。" % col_zh,
           "The desktop is currently sorted by \u201c%s\u201d." % col_en)
        if col_zh else
        _t("检测到桌面正被系统自己排着，但它没说是按哪一列排的。",
           "The desktop is being sorted by the system itself, but it "
           "wouldn't say which column it used."),
        _t("这种状态下系统认为自己负责摆图标，"
           "我们摆好的位置会在下一次刷新时被重排。",
           "In this state the system thinks it owns the icon layout, so any "
           "positions we set get rearranged on the next refresh."),
        "",
        _t("可以帮你清掉这个活动排序：把桌面上的一个图标按住、拖到空位置"
           "（就是没有图标的地方）再松手，然后把整个桌面按你存好的布局"
           "一次性摆回原位。",
           "I can clear that active sort for you: press and hold one desktop "
           "icon, drag it to an empty spot (anywhere with no icon), release, "
           "and then the whole desktop gets laid back out from your saved "
           "layout in one go."),
        _t("过程需要约 %s 秒，这期间会占用鼠标（请别动鼠标，让它自己拖）；"
           "做完图标会回到布局里的位置，不会留在你刚看到的地方。" % secs,
           "It takes about %s seconds and the mouse is taken over meanwhile "
           "(please keep your hands off it); afterwards the icons sit where "
           "your layout says, not where this drag left them." % secs),
    ]
    if state.get("autoarrange"):
        lines += ["",
                  _t("另外：桌面还开着「自动排列图标」（右键 → 查看 里可以取消勾选）。"
                     "这个开关也得关掉，布局才真正摆得住——本工具不会替你改它。",
                     "Also: \u201cAuto arrange icons\u201d is still on (untick it under "
                     "right-click \u2192 View). That switch has to go off too before the "
                     "layout stays put \u2014 this tool will not change it for you.")]
    lines += ["", _t("要现在帮你清一下活动排序吗？",
                     "Clear the active sort now?")]
    return "\n".join(lines)


def _ask_default(title: str, text: str) -> bool:
    """真弹窗。**拿不到界面就返回 False**（默认值永远是"不动手"）。"""
    try:
        from tkinter import messagebox
        return bool(messagebox.askyesno(title, text))
    except Exception:
        return False


def _default_plan_provider():
    """没有调用方给 plan 时，从真实桌面取（只在生产路径用）。

    返回 ``(im, positions, cells, geom)``：``geom`` 是标定过的同一份几何，
    要一路带到 `_real_drag`，不然"空格"和"落点"会各算各的。
    """
    try:
        from core.icon_manager import IconManager
        im = IconManager()
        pos = im._read_all_positions_live()
        geom = im._grid_geometry(samples=pos) if pos else None
        return im, pos, _grid_cells(im, pos, geom), geom
    except Exception:
        return None, {}, [], None


def left_button_down() -> bool:
    """左键此刻按没按下（★R249 一2③"用户想去动鼠标"的读数）。

    用 `GetAsyncKeyState` **巡检**，不装钩子：本批硬约束里写着"不新增键盘/鼠标
    钩子"（历史上"打字不上"那个 P0 就是钩子来的）。只取高位（按下态），
    **绝不读低位**——低位是一次性计数，读了会把同进程其他模块的计数吃掉。
    这里与 `core/dock.py` 里那份同名读数是**有意重复**的：★R242 红线②要求
    `sort_clear` 不 import `dock`、`dock.py` 也不提 `sort_clear`，所以不越线借。
    拿不到 user32 ⇒ 返回 False（读数失败永远不等于"用户在捣乱"）。
    """
    try:
        import ctypes
        return bool(ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000)
    except Exception:
        return False


#: `silent_clear_for_layout` 的"没传"哨兵：**显式传 None 表示"这台机器没这条
#: 路，直接放弃"**，不传才去延迟导入。测试靠这一点保证"传 None 绝不会真去碰桌面"。
_UNSET = object()


def silent_clear_for_layout(view_clear=_UNSET, state: Optional[dict] = None,
                            log: Optional[Callable[..., None]] = None,
                            where: str = '切分类') -> dict:
    """★R249 一2①②：切分类摆布局**之前**，安静地把桌面的活动排序清掉。

    用户 26 批 02 的原话是「①按建议来 ②不啃声」，风险那条他自己答了：
    「你一点分类，那肯定自动复原……想摆图标就手动保存下布局，本来就是这样设计的」
    ⇒ 挂在切分类这条路上是**原设计**，不是新行为。

    ★R250 ④：`where` 只改**日志措辞**（"切分类前顺手…"→"摆布局前顺手…"），
    行为一个字不变。用户 42 批原话「后来我点"全部"这个选项卡也不好使了」⇒ 这条
    免鼠标清排序从"只挂切分类"（★R249 挂账 #208 的口径）扩到**每次摆布局之前**，
    扩的正是当初留的那句"以后要是发现别的入口也常摆完又被打回，从这条扩"。

    "不啃声"落到三条硬规矩上：
      · **只走免鼠标官方接口**（调用方把 `core/folder_view.clear_active_sort` 接进来），
        一次鼠标都不发、一个框都不弹；清不掉就算了——要抢鼠标的那条路
        仍归原来的确认框管，绝不在这里替用户做主；
      · **读不到状态 / 本来就没排序 ⇒ 一个字节都不写视图**（★R242 判据 B3 同口径：
        "读不到"不许当"没排序"，反过来也不许拿"没排序"当理由去写视图）；
      · 任何异常都吞成 ``ok=False``：摆布局那条流程不能被"顺手清个排序"带崩。

    返回 ``{'ok', 'why', 'tries', 'view'}``；成功只写一行日志（②不啃声≠不留痕，
    排查时要能看出这一轮真的动过桌面）。
    """
    out = {"ok": False, "why": "", "tries": 0, "view": None}

    def _log(*a):
        if log:
            try:
                log(*a)
            except Exception:
                pass
        try:
            logger.info("[sort_clear] %s", " ".join(str(x) for x in a))
        except Exception:
            pass

    try:
        vc = view_clear
        if vc is _UNSET:
            try:
                from core.folder_view import clear_active_sort as vc
            except Exception as e:
                out["why"] = "no-api:%s" % type(e).__name__
                _log("静默修没开始：免鼠标接口导不进来（%s）" % type(e).__name__)
                return out
        if vc is None:
            out["why"] = "no-api"
            _log("静默修没开始：这台机器没接免鼠标接口")
            return out
        st = state if state is not None else read_sort_state()
        out["state"] = st
        if not st or not st.get("ok"):
            out["why"] = "state-unknown"
            _log("静默修没开始：读不到桌面状态，不动手")
            return out
        if not st.get("sorted"):
            out["why"] = "no-sort"
            return out
        out["tries"] = 1
        r = vc() or {}
        out["view"] = r
        if r.get("ok"):
            out["ok"] = True
            out["why"] = ""
            _log("🧹 %s前顺手把桌面的活动排序清掉了（免鼠标，没占用鼠标）" % where)
            return out
        out["why"] = "view-fail:%s" % (r.get("err") or "?")
        _log("静默修没成：%s，留着活动排序照常摆布局" % out["why"])
    except Exception as e:
        out["ok"] = False
        out["why"] = "raised:%s" % type(e).__name__
        _log("静默修出了一下错（%s），已停手" % type(e).__name__)
    return out


def offer_and_clear(settings: Optional[dict] = None,
                    get_state: Optional[Callable[[], dict]] = None,
                    ask: Optional[Callable[[str], bool]] = None,
                    drag: Optional[Callable[[str, _Cell], bool]] = None,
                    verify: Optional[Callable[[], bool]] = None,
                    restore: Optional[Callable[[str, _Point], None]] = None,
                    plan: Optional[Sequence[Tuple[str, _Cell, _Point]]] = None,
                    icon_manager=None,
                    apply_layout: Optional[Callable[
                        [Sequence[Tuple[str, _Point]]], object]] = None,
                    repick: Optional[Callable[
                        [str, _Cell], Optional[_Cell]]] = None,
                    view_clear: Optional[Callable[[], dict]] = None,
                    button_down: Optional[Callable[[], bool]] = None,
                    log: Optional[Callable[..., None]] = None) -> dict:
    """总入口：诊断 → 先试免鼠标官方接口 → 不行才（该提示才）弹确认 → 用户点"是"才动手 → 复核 → 摆回布局。

    所有会碰桌面的参数（``drag/verify/restore/plan/icon_manager/apply_layout``）
    都可以注入，回归测试一律注入桩具 ⇒ 跑测试时真实桌面一根手指都不会被碰到。

    ``apply_layout``（★R245 A7）：清排序成功后**一次性按基准布局摆回全部图标**，
    不再把拖过的那个图标单独拖回原格；不给它（或它自己失败）就走老的回位路。

    ``repick``（★R246 #197）：拖之前复核落点。调用方不给 ⇒ 生产路径自动装
    `_make_repick(icon_manager, geom)`；只要调用方自带 ``plan``（回归测试都这样），
    就**一个字节都不多出**，与 ★R242/★R245 行为一致。

    ``view_clear``（★R246 #198，用户 38 批 item2b「不可以程序化的方式吗？不可以
    更快吗？」）：那条**一根手指都不占**的清法（`core.folder_view.clear_active_sort`
    调 `IFolderView2::SetSortColumns(真缓冲, 0)`）。它成功就直接收工——**连确认框
    都不弹**，因为那道框存在的唯一理由是"过程会占用鼠标"；它失败或这台机器不支持，
    原样落到下面的合成拖动，那条**照旧要点「是」才动手**。
    ⚠ 本模块**不自己 import** 它（判据 S2）：只有调用方显式接进来才试，
    所以历轮门禁与别的调用点行为不变（判据 S8）。

    ``button_down``（★R249 一2③，用户「如果连续三次都想去动鼠标，就弹对话框」）：
    拖动那一段发完鼠标之后探一次左键（生产传 `left_button_down`，**巡检读数、
    不是钩子**）。探到"我们松手之后左键还按着"＝这一次用户想去抢鼠标，结果里带
    ``interfered=True`` 给调用方计数；不给它 ⇒ 不探、字段恒假（历轮门禁行为不变）。
    走免鼠标那条成功时**一律不算干涉**（一根手指都没占用，无从"抢"）。
    """
    def _log(*a):
        if log:
            try:
                log(*a)
            except Exception:
                pass
        try:
            logger.info("[sort_clear] %s", " ".join(str(x) for x in a))
        except Exception:
            pass

    res = {"offered": False, "ok": False, "user_said_no": False, "relaid": False,
           "reason": "", "attempts": [], "state": None, "advisory": "",
           "via": "", "view": None, "interfered": False}
    st = (get_state or read_sort_state)()
    res["state"] = st
    if not should_offer(st, settings or {}):
        res["reason"] = ("no-active-sort" if st and st.get("ok")
                         else "state-unknown")
        res["advisory"] = autoarrange_advisory(st)
        if res["advisory"]:
            _log("desk view autoarrange is on; advise manual toggle (no injection)")
        _log("skipped: %s" % res["reason"])
        return res

    # ★R246 #198：先试那条**免鼠标**的官方接口清法（调用方接进来才试，判据 S2）。
    # 成功 ⇒ 不弹确认框（那道框只为"要占用鼠标 ~0.5 秒"而存在）、一次鼠标都不发，
    # 并且照 item2c"清完立马写布局"；失败 ⇒ 一句日志，原样落到下面的拖动流程。
    if view_clear is not None:
        try:
            vc = view_clear() or {}
        except Exception as e:
            vc = {'ok': False, 'err': '%s:%s' % (type(e).__name__, str(e)[:80])}
        res['view'] = vc
        if vc.get('ok'):
            res['ok'] = True
            res['via'] = 'view-api'
            _log('免鼠标修好了：官方接口一次调用就把活动排序清掉，耗时 %s 秒，'
                 '没占用鼠标' % ('%.2f' % float(vc.get('secs') or 0.0)))
            if apply_layout is not None:
                # 传空表：这条路一个图标都没拖过，摆的是**整桌基准布局**
                # （`_sort_clear_relay` 本来就忽略这个参数，整桌都摆）
                try:
                    res['relaid'] = bool(apply_layout([]))
                except Exception as e:
                    _log('批量摆位抛异常 err=%s' % type(e).__name__)
            return res
        _log('免鼠标那条没走通（%s），改走合成拖动' % (vc.get('err') or '?'))

    text = _offer_text(st)
    res["offered"] = True
    asker = ask if callable(ask) else _ask_default
    try:
        said_yes = bool(asker(text) if ask is not None else asker("帮你修一下？", text))
    except Exception as e:
        said_yes = False
        _log("ask raised %s -> treat as no" % type(e).__name__)
    if not said_yes:
        res["user_said_no"] = True
        res["reason"] = "user-declined"
        _log("user declined; no mouse injection sent")
        return res

    geom = None
    prod_repick = None
    if plan is None:
        im, positions, cells = (icon_manager,
                                (icon_manager._read_all_positions_live()
                                 if icon_manager else {}), [])
        geom = None
        if im is None:
            im, positions, cells, geom = _default_plan_provider()
        else:
            try:
                geom = im._grid_geometry(samples=positions)
            except Exception:
                geom = None
            cells = _grid_cells(im, positions, geom)
        if not positions or not cells:
            res["reason"] = "no-target"
            _log("没修好：拿不到桌面图标或空格，一次都没拖")
            return res
        # ★R246 #197：排表就按"位移最短"排（用户 item2a「越短越好」）。换算格子
        # 用的是**同一份标定几何**；几何拿不到 ⇒ cell_pt=None ⇒ 退回老配法，不硬来。
        plan = build_plan(positions, cells, cell_pt=_cell_pt_of(im, geom))
        # 同一理由：拖之前再核一次落点（判据 G6/B17）。桩具路径（调用方自己给 plan）
        # 走上面无这个分支，行为与 ★R242/★R245 一致。
        if hasattr(im, '_read_all_positions_live'):
            prod_repick = _make_repick(im, geom, log=_log)

    if drag is None:
        _im = icon_manager
        _geom = geom
        if _im is None:
            _im, _p, _c, _geom = _default_plan_provider()
        drag = (lambda n, c: _real_drag(_im, n, c, _geom)) if _im else (lambda n, c: False)
    if verify is None:
        verify = _make_verifier()
    if restore is None:
        _im = icon_manager
        if _im is None:
            _im, _p, _c, _g = _default_plan_provider()
        restore = _make_restorer(_im, log=_log) if _im else (lambda n, xy: None)

    try:
        im_result = attempt_clear(plan, drag=drag, verify=verify,
                                  restore=restore, log=_log,
                                  rebatch=apply_layout,
                                  interfere=button_down,
                                  repick=repick if repick is not None
                                  else prod_repick)
    except Exception as e:
        res["reason"] = "raised:%s" % type(e).__name__
        _log("没修好：过程出错（%s: %s），已把拖过的图标放回原格" % (
            type(e).__name__, e))
        return res
    res["ok"] = bool(im_result.get("ok"))
    res["relaid"] = bool(im_result.get("relaid"))
    res["attempts"] = im_result.get("attempts") or []
    res["interfered"] = bool(im_result.get("interfered"))
    res["via"] = "drag" if res["ok"] else ""
    res["reason"] = "" if res["ok"] else (im_result.get("err") or "still-sorted")
    if res["ok"]:
        _log("sort cleared on try %d" % len(res["attempts"]))
    return res
