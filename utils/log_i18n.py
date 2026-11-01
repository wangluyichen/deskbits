# -*- coding: utf-8 -*-
"""★R229（用户 01 批 item2，方案 A）：运行日志英文显示层。

口径（方案 A = 只翻"用户会主动看"的那批，一批收口，不做全量 239 条）：
  · 源码里的 `gui._log('中文…')` **一个字都不改**——中文仍是唯一真源
    （app.log / 远程排障 / 既有门禁断言都按中文原文匹配，改了会全线假红）。
  · 本模块是一张"短语典"：English 模式下，日志**上屏前**按正则短语逐段
    改写成英文；没命中短语的行原样显示中文（宁可少翻，不可翻错）。
  · 只作用于 GUI 日志页 + 状态栏；`data/app.log`、`data/switch.log`
    （机读哨兵）保持中文原样。

为什么用词典型而不是就地 `i18n.tr(中文, 英文)`：
  1. 就地双写要动 60+ 处调用点，每处都要把 f-string 拆两份，任何一处漏
     括号就是 P0（日志在后台线程里抛异常会带走功能）；
  2. 词典型新增一行即生效，且能被门禁测试直接统计"翻了多少条"；
  3. 中文原文不动 ⇒ 历轮所有依赖日志文案的门禁（自动测试.py 的
     "完成 ✓/失败 ✗"、test_r229 的"已改/无需修改"）零风险。

写法约定（新增条目时照抄）：
  · 模式用**半转义**正则：全角标点（，：（）—）不是正则元字符，直接写；
    半角 `( ) [ ] . * + ?` 要 `\\` 转义。
  · 捕获组用 `\\1`\\2 回填。**长句排在短句前面**：改写是逐条 re.sub 链，
    先命中的片段变英文后，更长的那条就再也匹配不上了。
  · 只翻界面文案，不翻用户数据（分类名、文件名、路径、错误详情原样保留）。
"""
import re

from utils import i18n

# ---- 短语典（按功能区分组，组内长→短）----
# 每项：(正则, 英文模板)
_TABLE = [
    # ============ ★R335（批88）·「为什么一枚没散」这一族理由句 ============
    # 批87（★R334-b）把逐盒理由汇总进 `out['reason']` 时只写了中文，本批起三个界面
    # 读点（`gui/main_window.py`、`gui/rank_display.py`、`gui/desktop_box_panel.py`）
    # 统一过这层显示 ⇒ 这几句**第一次真地上屏给人看**（从前只在 app.log，而 app.log
    # 按口径保持中文原样，不归本模块管）。真句回放实测漏 5 条 ⇒ 这里补 4 条。
    # 铁律①照守：整句在前、裸短语在后（`未配置` 那味排在它下面的整句之后）。
    # 坑位分两类，S6 复核时逐条数过（★R335-S6 建议 1 说"全表只有这一条带自由文本
    # 捕获组、没有回显路径的先例"——**不属实**：本表另有 195 条（S6 用 AST 逐条数过：含任一 `\N` 回填的 297 条）把 `(.*?)` 装的文件名/
    # 路径原样回填，模块头口径第 24 行写的就是"分类名、文件名、路径、错误详情原样
    # 保留"）。⇒ 口径按类走：
    #   · **程序自己写的中文文案**（盒名、「逐盒理由」整句）＝汉字坑位 ⇒ 不回显，
    #     只把话指回运行日志（第 1、3 条那两味）。
    #   · **用户数据**（桌面路径、让路文件名）＝照旧原样回显，用户要知道到底是哪个
    #     目录读不动（第 2 条的 `\1`；中文用户名会连带汉字上屏，但那是他自己的路径，
    #     与全表其余 195 条同一口径，不是本批新开的口子）。
    (r'有 (\d+) 只盒这次一枚没散：.*',
     'No icon came back from \\1 box(es) - the reason for each box is '
     'in the run log'),
    (r'找不到桌面目录（(.*?)），这只盒的 (\d+) 枚一枚没动——先确认桌面路径',
     'The desktop folder could not be read (\\1) - all \\2 item(s) in this box '
     'stayed where they are; check the desktop path first'),
    (r'回收盒里同名那一格已经占满（试了 (\d+) 个空位）',
     'Every same-name slot in the quarantine box is already taken '
     '(tried \\1 free spots)'),
    (r'未配置', 'not configured'),

    # ============ ★R324（批79）·「清理重复的图标」这一族新句子 ============
    # 用户点名把按钮改名（原名对用户是黑话），顺带把 `dedupe_yield_names` 从"只收
    # 字节相同/同一枚快捷方式"扩到**四档可收**，于是演练句、逐枚留痕句全是新形状。
    # 排在整张表最前（铁律①：整句在前、裸短语在后；这一组的句子都比下面的通用规则长）。
    # 两个"不许回显"的坑位：① 盒名（`收纳·重复的`，汉字）② `_dup_note` 那句"差在哪一味"
    # （汉字）⇒ 通用那条只说"细节在运行日志里"，具体那几味各配一条精确规则。
    # ---- ★R324 N3（box 档以搬移为主、属性隐藏只兜底）那 20 句 ----
    # 新句子来自 `visibility_router.box_stale_conflicts / _reveal_ours /
    # yield_stale_box_items` 与面板状态行。两处坑位照样**捕获不回显**：盒名（`\2`，
    # 如「收纳·程序」）和 `move_into_box` 的拒因（`\3`，整句是界面文案、由各自的
    # 精确规则翻，塞进 \3 只会把汉字原文带回英文行）。路径/文件名/异常名照旧回显。
    # 顺序：带尾巴的（文件夹那味、显在桌面那味）排在裸句之前；`没搬进盒：.+` 那条
    # 兜底必须排在两条精确句之后。
    (r'\[盒子\] 清理重复的让路名：盒里没有散不回桌面的陈旧同名枚（这一档一枚没动）',
     r'[box] tidy duplicate icons: nothing in the boxes is stuck away from the '
     r'desktop (this tier touched nothing)'),
    (r'\[盒子\] 清理重复的让路名：盒里有 (\d+) 枚陈旧同名枚，但每一枚都判不出跟'
     r'桌面上那枚是不是同一份东西 ⇒ 默认一枚没动（要收请当面点那一句【是】）',
     r'[box] tidy duplicate icons: \1 stale same-named item(s) sit in the boxes, '
     r'but none can be told apart from the one on the desktop - nothing is touched '
     r'by default (press YES in the dialog to collect them)'),
    (r'\[盒子\] 清理重复的让路名：盒里有 (\d+) 枚陈旧同名枚可以收（桌面上已经有'
     r'同名的，这一批永远散不回来），一枚没动',
     r'[box] tidy duplicate icons: \1 stale same-named item(s) in the boxes can be '
     r'collected (the desktop already has that name, so they can never come back), '
     r'nothing was touched yet'),
    (r'\[盒子\] 清理重复的让路名：(.*?) 从盒里收进重复盒（桌面上已经有同名的，'
     r'而且那一枚是个文件夹，这一枚永远散不回来）',
     r'[box] tidy duplicate icons: \1 was collected out of a box into the '
     r'duplicate box (the desktop already has a folder of that name, so this one '
     r'could never come back)'),
    (r'\[盒子\] 清理重复的让路名：(.*?) 从盒里收进重复盒（桌面上已经有同名的，'
     r'这一枚永远散不回来）',
     r'[box] tidy duplicate icons: \1 was collected out of a box into the '
     r'duplicate box (the desktop already has that name, so this one could never '
     r'come back)'),
    # 兜底：中间那一味（`desk_kind` 那句）以后再加也不会漏汉字（那一味不回显）。
    (r'\[盒子\] 清理重复的让路名：(.*?) 从盒里收进重复盒'
     r'（桌面上已经有同名的.+?，这一枚永远散不回来）',
     r'[box] tidy duplicate icons: \1 was collected out of a box into the '
     r'duplicate box (the desktop already has that name, so this one could never '
     r'come back - what the desktop copy is, is in the run log)'),
    (r'\[盒子\] 清理重复的让路名：盒子账本读不动 ⇒ 陈旧同名枚一枚没搬（(\d+) 枚）',
     r'[box] tidy duplicate icons: the box ledger could not be read, so no stale '
     r'same-named item was moved (\1)'),
    (r'\[盒子\] 清理重复的让路名：成员账本没写回（东西已经搬进回收盒了）'
     r'——下一次扫描还会把这几枚报成冲突',
     r'[box] tidy duplicate icons: the member ledger was not written back (the '
     r'items are already in the quarantine box), so the next scan will report them '
     r'as conflicts again'),
    (r'\[盒子\] 清理重复的让路名：(.*?) 桌面上那一枚没搬进盒：那一枚是你自己藏的'
     r'（属性里没有本程序的指纹）⇒ 一个位都没动',
     r'[box] tidy duplicate icons: the desktop copy of \1 was not put into a box - '
     r'you hid that one yourself (its attributes carry no fingerprint of this '
     r'program), so not a single bit was changed'),
    (r'\[盒子\] 清理重复的让路名：(.*?) 桌面上那一枚没搬进盒：隐藏位清不掉',
     r'[box] tidy duplicate icons: the desktop copy of \1 was not put into a box - '
     r'the hidden flag could not be cleared'),
    (r'\[盒子\] 清理重复的让路名：(.*?) 桌面上那一枚没能搬进 (.*?)'
     r'：(.*?)（这一枚显在桌面上，下次切分类会再试一次）',
     r'[box] tidy duplicate icons: the desktop copy of \1 could not be moved into '
     r'its box (the reason is in the run log) - it is visible on the desktop, the '
     r'next category switch will try again'),
    (r'\[盒子\] 清理重复的让路名：(.*?) 桌面上那一枚已经搬进 (.*?)'
     r'（桌面上从此只剩盒子这一种模式）',
     r'[box] tidy duplicate icons: the desktop copy of \1 has been moved into its '
     r'box (the desktop now uses the box mode only)'),
    # 兜底：`move_into_box` 以后再加拒因也不会漏汉字（理由不回显，指路到运行日志）。
    (r'\[盒子\] 清理重复的让路名：(.*?) 桌面上那一枚没搬进盒：.+',
     r'[box] tidy duplicate icons: the desktop copy of \1 was not put into a box '
     r'(the reason is in the run log)'),
    (r'\[盒子\] 清理重复的让路名：已把桌面上 (\d+) 枚搬进各自那只盒'
     r'（收纳档以搬移为主，属性隐藏只兜底）',
     r'[box] tidy duplicate icons: \1 desktop item(s) were moved into their own '
     r'boxes (this mode moves things first, attribute hiding is only the fallback)'),
    (r'\[隐藏盒\] 所有权账本读不动 ⇒ 陈旧同名枚一枚不点',
     r'[hidden-box] the ownership ledger could not be read, so no stale '
     r'same-named item is flagged'),
    (r'\[隐藏盒\] 成员账本读不动 ⇒ 陈旧同名枚一枚不点',
     r'[hidden-box] the member ledger could not be read, so no stale same-named '
     r'item is flagged'),
    (r'\[隐藏盒\] (.*?) 只带隐藏位、没有本程序的指纹位 ⇒ 是用户自己藏的，'
     r'一个位都没动',
     r'[hidden-box] \1 only carries the hidden flag without the fingerprint bit of '
     r'this program - it was hidden by you, so not a single bit was changed'),
    (r'\[隐藏盒\] 清隐藏位失败（这一枚先不搬进盒）(.*?): (.*)',
     r'[hidden-box] the hidden flag could not be cleared, so this one stays out of '
     r'the box for now: \1 (\2)'),
    (r'\[隐藏盒\] 读不到 win32 属性接口，这一枚先不搬进盒: (.*)',
     r'[hidden-box] the win32 attribute API could not be loaded, so this one is '
     r'not put into a box: \1'),
    (r'\[隐藏盒\] 读文件属性失败（这一枚先不搬进盒）(.*?): (.*)',
     r'[hidden-box] the file attributes could not be read, so this one is not put '
     r'into a box: \1 (\2)'),
    (r'\[隐藏盒\] 读文件属性返回 -1（这一枚先不搬进盒）(.*?)',
     r'[hidden-box] reading the file attributes returned -1, so this one is not '
     r'put into a box: \1'),
    (r'\[隐藏盒\] 属性法账本里那一条没销掉（不影响这一枚搬进盒）: (.*)',
     r'[hidden-box] that entry could not be removed from the attribute-method '
     r'ledger (this item still goes into its box): \1'),
    (r'扫盒里的陈旧同名枚失败（桌面上那一发照旧）: (.*)',
     r'Scanning the boxes for stale same-named items failed (the desktop scan ran '
     r'as before): \1'),
    (r'扫盒里的陈旧同名枚失败（状态行少报一档，按钮仍可点）: (.*)',
     r'Scanning the boxes for stale same-named items failed (the status line '
     r'under-reports one tier, the button stays clickable): \1'),
    (r'\[盒子\] 清理重复的让路名：可收 (\d+) 枚（内容一样 (\d+)、同一个程序 (\d+)、'
     r'原名不在了 (\d+)、以前那几把 (\d+)），另有 (\d+) 枚判不出是不是同一枚，'
     r'一枚没动',
     r'[box] tidy duplicate icons: \1 can be collected (\2 byte-identical, \3 the '
     r'same program, \4 the original is gone, \5 left by earlier rounds), \6 more '
     r'cannot be told apart - nothing was touched'),
    # ★R325 F1：`可收` 那一行不再把"拿不出凭据"的那档算进去，尾巴多出一档。
    # 上面那条旧措辞**留着不删**（历史 app.log 还要翻得动，:182 的同款口径）。
    (r'\[盒子\] 清理重复的让路名：可收 (\d+) 枚（内容一样 (\d+)、同一个程序 (\d+)、'
     r'以前那几把 (\d+)），另有 (\d+) 枚判不出是不是同一枚、'
     r'(\d+) 枚拿不出凭据，一枚没动',
     r'[box] tidy duplicate icons: \1 can be collected (\2 byte-identical, \3 the '
     r'same program, \4 left by earlier rounds), \5 more cannot be told apart and '
     r'\6 carry no proof of being ours - nothing was touched'),
    (r'\[盒子\] 清理重复的让路名：没有可以自动收拾的重复图标，有 (\d+) 枚判不出'
     r'是不是同一枚，要当面问一句才收',
     r'[box] tidy duplicate icons: nothing can be collected automatically, \1 '
     r'cannot be told apart and will only be collected after you are asked'),
    (r'\[盒子\] 清理重复的让路名：有 (\d+) 枚拿不出凭据（只有名字带日期时间，'
     r'多半是你自己导出的备份包），要当面问一句才收',
     r'[box] tidy duplicate icons: \1 carry no proof of being ours (only the name '
     r'is date-stamped - most likely backup files you exported yourself), so they '
     r'are only collected after you are asked'),
    (r'\[盒子\] 清理重复的让路名：(.*?) 没碰（判不出是不是同一枚：大小不一样）',
     r'[box] tidy duplicate icons: \1 was not touched (cannot be told apart: the '
     r'sizes differ)'),
    (r'\[盒子\] 清理重复的让路名：(.*?) 没碰（判不出是不是同一枚：'
     r'指向的不是同一个程序）',
     r'[box] tidy duplicate icons: \1 was not touched (cannot be told apart: they '
     r'point at different programs)'),
    (r'\[盒子\] 清理重复的让路名：(.*?) 没碰（判不出是不是同一枚：内容不一样）',
     r'[box] tidy duplicate icons: \1 was not touched (cannot be told apart: the '
     r'contents differ)'),
    (r'\[盒子\] 清理重复的让路名：(.*?) 没碰（判不出是不是同一枚：'
     r'读不出快捷方式指向哪里）',
     r'[box] tidy duplicate icons: \1 was not touched (cannot be told apart: the '
     r'shortcut target could not be read)'),
    (r'\[盒子\] 清理重复的让路名：(.*?) 没碰（判不出是不是同一枚：'
     r'其中一枚不是文件（目录一类的东西不能合并））',
     r'[box] tidy duplicate icons: \1 was not touched (cannot be told apart: one of '
     r'them is not a file - folder trees are never merged)'),
    (r'\[盒子\] 清理重复的让路名：(.*?) 没碰（判不出是不是同一枚：'
     r'有一枚读不到（可能被删了或正被占用））',
     r'[box] tidy duplicate icons: \1 was not touched (cannot be told apart: one of '
     r'them cannot be read - it may have been deleted or is in use)'),
    # 兜底那条：`_dup_note` 以后再加一味也不会漏汉字（理由不回显，指路到运行日志）。
    (r'\[盒子\] 清理重复的让路名：(.*?) 没碰（判不出是不是同一枚：.+?）+',
     r'[box] tidy duplicate icons: \1 was not touched (it cannot be told apart - '
     r'how they differ is in the run log)'),
    # ★R325 F1 的逐枚点名档（措辞里没有"判不出是不是同一枚"，上面那条通配吃不到它）。
    (r'\[盒子\] 清理重复的让路名：(.*?) 没碰（只有名字带日期时间，拿不出凭据）',
     r'[box] tidy duplicate icons: \1 was not touched (only its name is '
     r'date-stamped, and there is no proof it is one of ours)'),
    # 盒名（`收纳·重复的`）是汉字 ⇒ 这里**捕获但不回显**（\2 不进英文模板）。
    (r'\[盒子\] 清理重复的让路名：(.*?) 收进「(.*?)」（原名已经不在了）',
     r'[box] tidy duplicate icons: \1 was collected into the duplicate box (the '
     r'item with the original name is no longer there)'),
    (r'\[盒子\] 清理重复的让路名：另有 (\d+) 枚同一类没逐条念（上面已念 (\d+) 条）',
     r'[box] tidy duplicate icons: \1 more of the same kind were not listed one by '
     r'one (\2 were listed above)'),
    # ★R324 N1：按钮改名 ⇒ 面板那两条兜底 warning 的措辞跟着改（旧措辞那两条规则
    # 留在下面不动，历史 app.log 还要翻得动）。
    (r'扫描重复的图标失败（按钮仍可点，点了会再扫一次）: (.*?)',
     r'Cannot scan the desktop for duplicate icons (the button stays clickable, it '
     r'scans again when you press it): \1'),
    (r'「清理重复的图标」按钮状态没刷新: (.*?)',
     r'The "Tidy duplicates" button state did not refresh: \1'),
    # ============ ★R323（批78）·core/desktop_box.py 那八句（S6 复评补刀） ============
    # 本批改动的四个文件里，历轮那两套 H 段回放闸只覆盖 **面板**（gui/desktop_box_panel.py），
    # `core/desktop_box.py` 的日志行从来没有一台闸扫过 ⇒ 本批新增的这八句整句留在中文，
    # English 档日志直接漏汉字（★R232「判据范围 ≠ 改动范围」的同一种漏）。八句的形状由
    # AST 从源码抽（正则会被拆行字面量骗，★R323 口径二），逐条真句回放钉死。
    # 盒名/清单名这类**带汉字的坑位不回显**（★R319 口径），路径与文件名照旧回显。
    (r'\[盒子\] 散回「(.*?)」：这一只只进不出 ⇒ 一枚没动（要清理请在文件夹里删）',
     r'[box] returning to the desktop: that box only takes deposits, so this one '
     r'was not moved - to clean it up, open that folder and delete it yourself'),
    (r'\[盒子\] 散回「(.*?)」：(.*?) ⇒ 这一枚留在盒里'
     r'（桌面上那一枚的名字和内容一个字节都没动）',
     r'[box] returning \1: this one stays in the box (the desktop copy keeps its '
     r'name and its bytes, not a single byte touched)'),
    (r'\[盒子\] 认旧回收目录读不动 (.*?): (.*?)',
     r'[box] the legacy recycle folder cannot be read, so it was not adopted: \1 (\2)'),
    (r'\[盒子\] 清理重复的让路名：(.*?) 在回收盒里找不到空位',
     r'[box] tidy duplicate make-way names: no free slot left in the recycle box '
     r'for \1'),
    (r'\[盒子\] 旧回收目录读不动，这一把不动 (.*?): (.*?)',
     r'[box] the legacy recycle folder cannot be read, so this round leaves it '
     r'alone: \1 (\2)'),
    (r'\[盒子\] 旧回收目录里只剩一份清单（东西已经并进「.*?」那份）：'
     r'(.*?) —— 这一把目录我没删，要清就删它',
     r'[box] only a manifest is left in the legacy recycle folder (its items have '
     r'already been merged into the other box): \1 - that folder was not deleted '
     r'this round, remove it yourself if you want it gone'),
    (r'\[盒子\] 旧回收目录里还剩 (\d+) 样没搬 (.*?)（这一节只搬文件，'
     r'目录树一类不动）',
     r'[box] \1 item(s) were not moved out of the legacy recycle folder (this step '
     r'only moves files, never folder trees): \2'),
    (r'\[盒子\] 旧回收目录没收回 (.*?): (.*?)',
     r'[box] the legacy recycle folder was not reclaimed: \1 (\2)'),
    # ============ ★R323（批78）：盒子卡新增的那几句留痕行（L2/L5） ============
    # 排在**整张表最前**：这几句是"整句"，按上面的铁律①必须走在可能吃到片段的
    # 裸短语之前，否则后面任何一条通用规则先 sub 一下就静默假绿。
    # 判据来自 `test_r304_auto_pack` H1 / `test_r309_open_box_root` H2 从源码抽行
    # 逐条真句回放——这五条是那次回放抓出来的漏网。
    (r'按钮宽度自适应没做成（保持原 width）: (.*?)',
     r'[box] button width auto-fit failed (the original width is kept): \1'),
    (r'\[盒子\] 一键清理空盒子：没删掉的 (\d+) 只 (.*)',
     r'[box] tidy empty boxes: \1 stayed (\2)'),
    (r'\[盒子\] 一键清理空盒子：删掉 (\d+) 只（(.*?)）',
     r'[box] tidy empty boxes: removed \1 (\2)'),
    (r'\[盒子\] 一键清理空盒：(.*?) 的目录读不动（当成非空，这一枚不删）: (.*?)',
     r'[box] tidy empty boxes: cannot read \1 (treated as non-empty, kept): \2'),
    (r'\[盒子\] 一键清理空盒：(.*?) 判空失败（当成非空，这一枚不删）: (.*?)',
     r'[box] tidy empty boxes: cannot tell if \1 is empty (treated as non-empty, '
     r'kept): \2'),
    # ★R323 L2 把那句失败提示的措辞从"那枚按钮置灰"改成"按钮仍可点，点了会再扫一次"
    # （按钮不再置灰了）⇒ 新措辞一条规则；旧措辞那条留在下面不动，因为
    # `test_r321_no_auto_rename` 的英文回放还拿旧句子做字面断言。
    (r'扫描重复的让路名失败（按钮仍可点，点了会再扫一次）: (.*?)',
     r'Cannot scan the make-way names for duplicates (the button stays clickable, '
     r'it scans again when you press it): \1'),
    # ★R323 L8：那枚按钮开的是「收纳·重复的」盒根，旧那条规则钉的是改名前的
    # `打开让路回收目录失败`，这一句是新造的行 ⇒ 另开一条。
    (r'打开重复回收目录失败 (.*?): (.*?)',
     r'Could not open the duplicate-collection folder: \1 (\2)'),
    # ============ ★R321（批76）：自动散回不再改桌面上的名字 ⇒ 冲突要出声 ============
    # 0.99-R038 在 win11 真机上一把「显示全部」造出 40 个带日期的重复名（用户原话
    # "桌面图标多了很多，感觉是重复的"）。修法是自动/批量那两条链改成"桌面上那枚
    # 不碰"，于是多了下面这一族"留在盒里"的诊断行与"清理让路名"的操作行。
    # 顺序两条铁律：① **整句在前、裸短语在后**——`translate` 是把每条规则依次 `sub`
    # 全链跑完，裸短语排前面会先把整句里那半截吃掉，整句规则随即永不命中（静默假绿）；
    # ② 特例（`：跨卷`）排在通配（`：(.*?)`）之前，同一个道理。
    # 方括号一律 `\[`（★R232：写成 `[隐藏盒]` 是字符集，永不命中真句）；坑位数字用
    # `(\d+)`、名字用 `(.*?)`（★R304-2）。**英文模板里不回显带汉字的坑位**（如目录名
    # `清单.json`、回收目录名）——回显了 F1 的"零汉字"判据就失去意义，那些名目按
    # 用户数据的口径留在中文行里，英文行只说"那份清单/那个目录"。
    (r'\[盒子\] 散回「(.*?)」：桌面上已经有同名的 ⇒ 这一枚留在盒里，'
     r'桌面上那枚的名字和内容都没动',
     r'[box] returning \1: an item with that name is already on the desktop, so '
     r'this one stays in the box - the desktop copy keeps its name and its bytes'),
    # ---- ★R323 L8（批78）：`收纳·重复的` 只进不出 ⇒ 面板那两处（⚠ 那行 + 拒收弹窗）
    # 都会把 core 的 reason 原样上屏，英文档不能整句中文。盒名是汉字 ⇒ 英文行不回显
    # （★R319 口径：带汉字的坑位不进英文模板）。
    (r'「(.*?)」只进不出：这些是和桌面上同名同路径的重复快捷方式，'
     r'程序不会把它们搬回桌面——确认不要了请打开那只文件夹自己删',
     r'That box is drop-off only: these are duplicate shortcuts that share a name '
     r'AND a target with something already on the desktop, so the program never '
     r'moves them back - if you are done with them, open that folder and delete '
     r'them yourself'),
    (r'回收目录没写出（详情见运行日志）',
     r'The recycle folder was not written (see the run log for details)'),
    (r'桌面上已经有同名的 ⇒ 这一枚留在盒里，桌面上那枚的名字和内容都没动',
     r'An item with that name is already on the desktop, so this one stays in the '
     r'box - the desktop copy keeps its name and its bytes'),
    (r'\[隐藏盒\] 显示全部：(\d+) 枚留在盒里没动（桌面上已经有同名的）'
     r'——要搬请在资源页的盒子面板点对那只盒',
     r'[hidden-box] show-all: \1 icon(s) stayed in the box (an item with that '
     r'name is already on the desktop) - to move them, pick that box in the box '
     r'panel on the resource page'),
    (r'📦 还有 (\d+) 枚留在盒里没动（桌面上已经有同名的）'
     r'——要搬请在资源页的盒子面板点对那只盒',
     r'📦 \1 more icon(s) stayed in the box (an item with that name is already on '
     r'the desktop) - to move them, pick that box in the box panel on the '
     r'resource page'),
    (r'\[隐藏盒\] 认领（来源=(.*?)）：(\d+) 枚留在盒里没动（桌面上已经有同名的）',
     r'[hidden-box] claim (from=\1): \2 icon(s) stayed in the box (an item with '
     r'that name is already on the desktop)'),
    (r'桌面和回收目录不在同一个磁盘，这一枚留在原地',
     r'The desktop and the quarantine folder are on different disks, so this one '
     r'stays where it is'),
    # ★R325 F4（ZCode 20261013_2111 报告）：这里曾有**第二条**与上面 :271 逐字相同
    # 的 `回收目录没写出（详情见运行日志）` 规则（英文措辞略异）。`translate()` 按序
    # sub、先命中的吃掉 ⇒ 第二条**永远不会被用到**，是死规则（留着只会让人以为改它
    # 有效果）。删掉，实际渲染出来的英文一句没变（仍是上面那条）。
    # ---- 「清理重复的让路名」那 7 句（★R321 J4/J5 新增的操作链）----
    (r'\[盒子\] 清理重复的让路名：可收 (\d+) 枚（字节相同 (\d+)、同一枚快捷方式 (\d+)）'
     r'，另有 (\d+) 枚内容不一样，一枚没动',
     r'[box] tidy make-way names: \1 can be collected (\2 byte-identical, \3 the '
     r'same shortcut), \4 differ in content, nothing was touched'),
    (r'\[盒子\] 清理重复的让路名：已收 (\d+) 枚进 (.*?)（一枚没删，退路在那份 (.*?)）',
     r'[box] tidy make-way names: \1 moved into \2 (nothing was deleted - the '
     r'manifest kept inside that folder is the way back)'),
    (r'\[盒子\] 清理重复的让路名：已搬 (\d+) 枚但 (.*?) 没写出（(.*?)）'
     r'——退路是那个目录本身，里面的文件按原名认',
     r'[box] tidy make-way names: \1 were moved but the manifest could not be '
     r'written (\3) - the folder itself is the way back, its files keep their '
     r'original names'),
    (r'\[盒子\] 清理重复的让路名：(.*?) 没能搬进回收目录：跨卷',
     r'[box] tidy make-way names: \1 could not be moved into the quarantine '
     r'folder - different disk'),
    (r'\[盒子\] 清理重复的让路名：(.*?) 没能搬进回收目录：(.+)',
     r'[box] tidy make-way names: \1 could not be moved into the quarantine '
     r'folder (\2)'),
    (r'\[盒子\] 清理重复的让路名：回收目录写不出，这次一枚没搬：(.*?) - (.*?)',
     r'[box] tidy make-way names: the quarantine folder could not be created, so '
     r'nothing was moved this time: \1 - \2'),
    (r'\[盒子\] 清理重复的让路名：桌面上没有可以收拾的让路名',
     r'[box] tidy make-way names: nothing to tidy on the desktop'),
    (r'\[盒子\] 清理重复的让路名：目录读不动 (.*?): (.*?)',
     r'[box] tidy make-way names: cannot read \1: \2'),
    # 面板那三行是"按钮为什么灰着/为什么没刷新/目录打不开"的兜底 warning（★R320 同款
    # 口径：兜底可以吞异常，但必须留痕，而且留痕的行在 English 档也不能留汉字——
    # `test_r304_auto_pack` H1 / `test_r309_open_box_root` H2 从源码抽行逐条对账）。
    (r'扫描重复的让路名失败（那枚按钮置灰）: (.*?)',
     r'Cannot scan the make-way names for duplicates (that button stays greyed '
     r'out): \1'),
    (r'「清理重复的让路名」按钮状态没刷新: (.*?)',
     r'The "Tidy duplicates" button state did not refresh: \1'),
    (r'打开让路回收目录失败 (.*?): (.*?)',
     r'Could not open the make-way quarantine folder: \1 (\2)'),

    # ============ ★R322（批77）：谁挡谁要说清 + 账本跟着盒子走 + 搬入搬出逐枚留痕 ====
    # 用户 77 批原话「程序是不是应该对盒子里面的现有的东西有个记录啊，要不日志没法看，
    # 没法调试」⇒ 这一批给冲突加了归因（桌面上那一枚是文件夹 / 一模一样 / 内容不一样），
    # 那些新句子又经 `desktop_box_panel._fmt_refused` 上屏，English 档一句中文都不许留。
    # 顺序三条铁律（★R321 那一节的注照样适用）：① 整句在前、裸短语在后；
    # ② **带尾巴的排在裸句之前**——"这一组先按双属性隐藏"正好是带"；其中 N 枚…"那句的
    #   前缀，裸句先跑会把半句吃掉、剩下的中文尾巴永远翻不掉；
    # ③ 模式里的 `**` 要写成 `\*\*`（`**` 是量词叠加，`re.compile` 直接 "multiple
    #   repeat" 抛错，一条坏规则会打断整链）。
    (r'\[盒子\] 散回「(.*?)」：桌面上同名那枚是个文件夹 ⇒ 这一枚留在盒里'
     r'（桌面上那一枚的名字和内容一个字节都没动）',
     r'[box] returning \1: the same-named item on the desktop is a folder, so this '
     r'one stays in the box (the desktop copy keeps every byte of its name and '
     r'content)'),
    (r'\[盒子\] 散回「(.*?)」：桌面上同名那枚与盒里一模一样 ⇒ 这一枚留在盒里'
     r'（桌面上那一枚的名字和内容一个字节都没动）',
     r'[box] returning \1: the same-named item on the desktop is byte-identical to '
     r'the boxed one, so this one stays in the box (the desktop copy keeps every '
     r'byte of its name and content)'),
    (r'\[盒子\] 散回「(.*?)」：桌面上同名那枚内容不一样 ⇒ 这一枚留在盒里'
     r'（桌面上那一枚的名字和内容一个字节都没动）',
     r'[box] returning \1: the same-named item on the desktop has different '
     r'content, so this one stays in the box (the desktop copy keeps every byte of '
     r'its name and content)'),
    (r'\[盒子\] 散回「(.*?)」：桌面上同名那枚是个文件夹 ⇒ 不动它的名字，这一枚留在盒里',
     r'[box] returning \1: the same-named item on the desktop is a folder, so its '
     r'name is left alone and this one stays in the box'),
    (r'\[盒子\] 散回「(.*?)」：桌面上同名那枚内容不一样，而让路的名字也让不出来 '
     r'⇒ 这一枚留在盒里',
     r'[box] returning \1: the same-named item on the desktop has different '
     r'content and no make-way name could be freed, so this one stays in the box'),
    (r'\[盒子\] 散回「(.*?)」：另有 (\d+) 枚同类（逐条日志封顶 (\d+) 行，'
     r'全量见返回的 blocked 清单）',
     r'[box] returning \1: \2 more item(s) of this kind (per-item logging is '
     r'capped at \3 lines - the full set is in the returned blocked list)'),
    (r'\[盒子\] 收纳「(.*?)」：另有 (\d+) 枚同类（逐条日志封顶 (\d+) 行，'
     r'全量看返回的 moved 清单）',
     r'[box] storing into \1: \2 more item(s) of this kind (per-item logging is '
     r'capped at \3 lines - the full set is in the returned moved list)'),
    (r'\[盒子\] 收纳「(.*?)」→ 盒「(.*?)」（文件名 (.*?)，原位置 (.*?)）',
     r'[box] stored \1 into box \2 (file \3, from \4)'),
    (r'\[盒子\] 账本 (.*?) 已搬进盒根（旧那份改名 (.*?)\.已搬家 留底，一枚没删）',
     r'[box] ledger \1 now lives in the box root (the previous copy was kept '
     r'aside as \2.moved-aside; no file was deleted)'),
    (r'\[盒子\] 账本 (.*?) 搬家没成功（(.*?)）——继续按旧位置读',
     r'[box] ledger \1 could not be moved (\2) - still reading the old location'),
    (r'\[隐藏盒\] 目标盒 (.*?) 在盘上但没登记、里面有 (\d+) 枚（(.*?)）'
     r'——成员账本里记着 moved_at 搬入凭据 ⇒ 是本系统搬进去的，登记后照常搬，'
     r'不再每切一次分类降级一回（★R322 J3/J7）',
     r'[hidden-box] box \1 is on disk but not registered and holds \2 item(s) '
     r'(\3), while the member ledger carries the moved_at credential - we moved '
     r'them in, so the box is registered and the icons still move instead of '
     r'degrading on every category switch'),
    # ---- 降级理由句：`而<账本凭什么> ⇒ 证明不了是本系统建的…`（带尾巴的先排）----
    (r'\[隐藏盒\] 目标盒 (.*?) 在盘上但没登记、里面有 (\d+) 枚（(.*?)）；'
     r'其中 (\d+) 枚桌面上正有同名的挡着（(.*?)）；'
     r'成员账本里只有认领时补记的 adopted_at（自证，证明不了是谁放的） ⇒ '
     r'一枚不搬，交回属性法',
     r'[hidden-box] box \1 is on disk but not registered and holds \2 item(s) '
     r'(\3); \4 of them have a same-named item on the desktop in the way (\5), and '
     r'the member ledger only has the adopted_at note added when we adopted it - '
     r'that proves nothing about who put them there, so nothing moves and the '
     r'attribute method handles this group'),
    (r'\[隐藏盒\] 目标盒 (.*?) 在盘上但没登记、里面有 (\d+) 枚（(.*?)）；'
     r'其中 (\d+) 枚桌面上正有同名的挡着（(.*?)）；成员账本压根没记过这只盒 ⇒ '
     r'一枚不搬，交回属性法',
     r'[hidden-box] box \1 is on disk but not registered and holds \2 item(s) '
     r'(\3); \4 of them have a same-named item on the desktop in the way (\5), and '
     r'the member ledger has never recorded this box - that proves nothing about '
     r'who put them there, so nothing moves and the attribute method handles this '
     r'group'),
    (r'盒子里已有同名的「(.*?)」，里面有 (\d+) 枚东西（(.*?)），'
     r'而成员账本里只有认领时补记的 adopted_at（自证，证明不了是谁放的）'
     r' ⇒ 证明不了是本系统建的，这一组先按双属性隐藏；其中 (\d+) 枚是被\*\*'
     r'桌面上同名的那一枚\*\*挡着的（如「(.*?)」），程序不敢替你挪桌面上那一枚，'
     r'要搬请在资源页的盒子面板点对那只盒',
     r'The box named \1 already exists and holds \2 item(s) (\3), but the member '
     r'ledger only has the adopted_at note added when we adopted it - that proves '
     r'nothing about who put them there, so this group is hidden by attribute '
     r'instead; \4 of them are blocked by an item of the same name on the desktop '
     r'(\5) - the program will not touch that desktop item for you, use the box '
     r'panel on the resource page to move them'),
    (r'盒子里已有同名的「(.*?)」，里面有 (\d+) 枚东西（(.*?)），'
     r'而成员账本压根没记过这只盒 ⇒ 证明不了是本系统建的，这一组先按双属性隐藏；'
     r'其中 (\d+) 枚是被\*\*桌面上同名的那一枚\*\*挡着的（如「(.*?)」），'
     r'程序不敢替你挪桌面上那一枚，要搬请在资源页的盒子面板点对那只盒',
     r'The box named \1 already exists and holds \2 item(s) (\3), but the member '
     r'ledger has never recorded this box - that proves nothing about who put them '
     r'there, so this group is hidden by attribute instead; \4 of them are blocked '
     r'by an item of the same name on the desktop (\5) - the program will not '
     r'touch that desktop item for you, use the box panel on the resource page to '
     r'move them'),
    (r'盒子里已有同名的「(.*?)」，里面有 (\d+) 枚东西（(.*?)），'
     r'而成员账本里只有认领时补记的 adopted_at（自证，证明不了是谁放的）'
     r' ⇒ 证明不了是本系统建的，这一组先按双属性隐藏',
     r'The box named \1 already exists and holds \2 item(s) (\3), but the member '
     r'ledger only has the adopted_at note added when we adopted it - that proves '
     r'nothing about who put them there, so this group is hidden by attribute '
     r'instead'),
    (r'盒子里已有同名的「(.*?)」，里面有 (\d+) 枚东西（(.*?)），'
     r'而成员账本压根没记过这只盒 ⇒ 证明不了是本系统建的，这一组先按双属性隐藏',
     r'The box named \1 already exists and holds \2 item(s) (\3), but the member '
     r'ledger has never recorded this box - that proves nothing about who put them '
     r'there, so this group is hidden by attribute instead'),
    # ---- 裸归因（`blocked`/`conflicts` 的 reason，上屏那一条）----
    (r'桌面上同名那枚是个文件夹 ⇒ 这一枚留在盒里，桌面上那枚的名字和内容都没动',
     r'The same-named item on the desktop is a folder, so this one stays in the '
     r'box - the desktop copy keeps its name and its bytes'),
    (r'桌面上同名那枚与盒里一模一样 ⇒ 这一枚留在盒里，桌面上那枚的名字和内容都没动',
     r'The same-named item on the desktop is byte-identical to the boxed one, so '
     r'this one stays in the box - the desktop copy keeps its name and its bytes'),
    (r'桌面上同名那枚内容不一样 ⇒ 这一枚留在盒里，桌面上那枚的名字和内容都没动',
     r'The same-named item on the desktop has different content, so this one '
     r'stays in the box - the desktop copy keeps its name and its bytes'),

    # ============ ★R306（批62）：收纳盒成员读不动 ⇒ 这一轮不剪幽灵 ============
    # 这句只在 `data/hidden_box.json`/清单读不动的兜底分支里打，是"为什么不剪"
    # 的当面理由，English 档不能留汉字。
    (r'🧹 这一轮一个图标都不剪：收纳盒里都有谁认不出来'
     r'（多半是 `data/hidden_box\.json` 读不动）——'
     r'宁可留着可疑的坐标记忆，也不冒「把搬进盒子的图标当删掉的抹掉」这个险',
     r'🧹 no icon position dropped this round: the storage boxes could not be '
     r'read (most likely `data/hidden_box.json` is unreadable) — better keep a '
     r'doubtful position memory than erase icons that were only moved into a box'),
    (r'\[ghost\] 收纳盒成员读不动，这一轮一个都不剪(?:%s|: .*)?',
     r'[ghost] box members unreadable, nothing dropped this round'),

    # ============ ★R305（批61）：收纳档 / 隐藏盒路由器（core/visibility_router + 三条链）============
    # 这 13 句是本批新可达路径上才会打出来的诊断行与"当面点名"的理由句（理由句经
    # `⚠ + reason` 上屏，dock / 排行榜 / 家长模式三条链共用同一批）。
    # 方括号一律 `\[`：★R232 那条最贵的教训——原样写 `[隐藏盒]` 是**字符集**，
    # `re.compile` 不报错、lint() 也不报，但这条规则**永远匹配不上真句**（静默假绿）。
    # 捕获一律 `(.*?)`（★R304-2 口径）：源码字面量喂进来时坑位是 `%s`/`%d`，
    # 运行期喂进来的是渲染后的值，两种形态都要能命中，数字照样回填。
    # ============ ★R305（批61）：收纳档 / 隐藏盒路由器（core/visibility_router + 三条链）============
    # 这 23 句是本批新可达路径上才会打出来的诊断行与"当面点名"的理由句（理由句经
    # `⚠ + reason` 上屏，dock / 排行榜 / 家长模式三条链共用同一批）。
    # 方括号一律 `\[`：★R232 那条最贵的教训——原样写 `[隐藏盒]` 是**字符集**，
    # `re.compile` 不报错、lint() 也不报，但这条规则**永远匹配不上真句**（静默假绿）。
    # 捕获一律 `(.*?)`（★R304-2 口径）：源码字面量喂进来时坑位是 `%s`/`%d`，
    # 运行期喂进来的是渲染后的值，两种形态都要能命中，数字照样回填。
    # ---- 「显示全部」散盒那 10 句（★R305 真机 P1 之后补的出口）----
    # 坑位是**数字**的地方用 `(\d+)` 而不是 `(.*?)`：`散回=(\d+) 枚` 后面紧跟
    # `动过的盒=`，懒配会把中间的字吞进坑位，回填出来的英文行反而更乱。
    # ★R320（批75）：这一行长了两个坑——`（其中认领=N 枚）` 与行尾 `没被认领=N`。
    # 先长后短（★R318 K19 口径）：新句式必须排在下面那条旧规则**之前**，否则旧规则
    # 的 `枚 动过的盒=` 对新句永不命中，整行留在中文档里。
    (r'\[隐藏盒\] 显示全部 来源=(.*?) 散回=(\d+) 枚（其中认领=(\d+) 枚）'
     r'动过的盒=(\d+) 只 搬不回=(\d+) 已不在盒里=(\d+) 让路改名=(\d+)'
     r' 没被认领=(\d+)(?:（演练）)?',
     r'[hidden-box] show-all from=\1: returned \2 icon(s) (\3 of them claimed '
     r'from boxes the ledger had forgotten), \4 box(es) touched, '
     r'\5 could not be moved back, \6 was no longer in the box, '
     r'\7 renamed to make room, \8 still unclaimed'),
    (r'\[隐藏盒\] 显示全部 来源=(.*?) 散回=(\d+) 枚 动过的盒=(\d+) 只 '
     r'搬不回=(\d+) 已不在盒里=(\d+) 让路改名=(\d+)(?:（演练）)?',
     r'[hidden-box] show-all from=\1: returned \2 icon(s) from \3 box(es), '
     r'\4 could not be moved back, \5 was no longer in the box, '
     r'\6 renamed to make room'),
    (r'\[隐藏盒\] 显示全部：账本读不动 ⇒ 一发都不散（来源=(.*?)）',
     r'[hidden-box] show-all: the ownership ledger could not be read, so nothing '
     r'is returned (from=\1)'),
    (r'\[隐藏盒\] 显示全部：盒 (.*?) 目录读不动（这一只跳过）',
     r'[hidden-box] show-all: the folder of box \1 could not be read '
     r'(this one skipped)'),
    (r'\[隐藏盒\] 显示全部散回失败（属性法照走）',
     r'[hidden-box] show-all could not return the boxed icons (the attribute '
     r'method still runs)'),
    (r'\[隐藏盒\] 散回失败（来源=(.*?)，后续步骤照走）',
     r'[hidden-box] returning the boxed icons failed (from=\1, the following '
     r'steps still run)'),
    (r'退出家长模式散回盒子失败（属性法照走）',
     r'Leaving kid mode could not return the boxed icons (the attribute method '
     r'still runs)'),
    (r'📦 显示全部：先从 (\d+) 只盒子搬回 (\d+) 枚，再恢复属性',
     r'📦 Show all: first returning \2 icon(s) from \1 box(es) to the desktop, '
     r'then restoring the attributes'),
    (r'📦 退出家长模式：先从盒里搬回 (\d+) 枚，再恢复其余图标',
     r'📦 Leaving kid mode: first returning \1 icon(s) from the boxes, then '
     r'restoring the rest'),
    (r'📦 已从盒里搬回 (\d+) 枚（(\d+) 只盒）',
     r'📦 Returned \1 icon(s) to the desktop from \2 box(es)'),
    (r'隐藏盒清单读不动，这次一枚都没散回——先确认盒里没有你要的东西',
     r'The hidden-box list could not be read, so nothing was returned this time '
     r'- check the boxes first, in case your icons are still in one'),
    # ★R310：这一行末尾多了 `让路改名=N`（旧规则那记 `(.*?)` 会把新字段连中文一起
    # 带回英文行），逐字段写死，尾巴（`（演练）`）单独一条规则吃掉。
    (r'\[隐藏盒\] 组=(.*?) 来源=(.*?) 搬走=(\d+) 散回=(\d+) 留在盒里=(\d+) '
     r'退回属性法=(\d+) 让路改名=(\d+)（演练）',
     r'[hidden-box] group=\1 from=\2 packed=\3 back to desktop=\4 still in box=\5 '
     r'attribute fallback=\6 renamed to make room=\7 (dry run)'),
    (r'\[隐藏盒\] 组=(.*?) 来源=(.*?) 搬走=(\d+) 散回=(\d+) 留在盒里=(\d+) '
     r'退回属性法=(\d+) 让路改名=(\d+)',
     r'[hidden-box] group=\1 from=\2 packed=\3 back to desktop=\4 still in box=\5 '
     r'attribute fallback=\6 renamed to make room=\7'),
    (r'\[隐藏盒\] 账本读不动 ⇒ 本次切换一枚都不搬（组=(.*?)）',
     r'[hidden-box] the ownership ledger could not be read, so this switch moves '
     r'nothing (group=\1)'),
    # ★R318（批73）S6 真句回放补的三条：`_fallback_to_attribute` 那条**包装行**
    # 历轮只有下面"账本读不动"那一句精确规则，K3/K4 新走这条路后 reason 是另外两句
    # ⇒ 外壳没规则、English 档上屏剩一截 `[隐藏盒]` 汉字。这里只吃外壳，reason
    # 本体由后面的逐句规则翻（`translate` 是逐条 sub，不是首中即停）。
    (r'\[隐藏盒\] (.*?) ⇒ 本次切换一枚都不搬（组=(.*?)）',
     r'[hidden-box] \1 ⇒ this switch moves nothing (group=\2)'),
    (r'\[隐藏盒\] 搬完之后更新登记没落盘（组=(.*?)）——盒内 (\d+) 枚'
     r'仍按上一次登记的来源散回',
     r'[hidden-box] the ownership entry could not be updated after packing '
     r'(group=\1), so the \2 icon(s) inside still return per the last recorded '
     r'source'),
    (r'\[隐藏盒\] 散回后登记本组盒没落盘（组=(.*?)）——下次切到这一组'
     r'会重新建盒并接管空壳',
     r'[hidden-box] the box for this group could not be registered after '
     r'releasing (group=\1), so the next switch to this group will rebuild and '
     r'take over the empty folder'),
    (r'\[隐藏盒\] 目标盒 (.*?) 在盘上但没登记 ⇒ 一枚不搬，交回属性法',
     r'[hidden-box] box \1 exists on disk but is not registered here, so nothing is '
     r'moved and the attribute method handles this group'),
    (r'\[隐藏盒\] 账本读不动（(.*?)），本次一枚都不搬：',
     r'[hidden-box] the ledger could not be read (\1), nothing is moved this time: '),
    (r'\[隐藏盒\] 设置读不动（按 hide 档继续）',
     r'[hidden-box] the mode setting could not be read (staying on the attribute '
     r'method) '),
    (r'\[隐藏盒\] 账本写入失败', r'[hidden-box] writing the ledger failed'),
    (r'\[隐藏盒\] 读当前坐标失败（散回时按可用空位摆）',
     r'[hidden-box] the current icon coordinates could not be read (returned icons '
     r'land on any free cell)'),
    (r'隐藏盒清单读不动，这次先按双属性隐藏，一枚都没搬',
     r'The hidden-box list could not be read, so this switch hides by attribute '
     r'instead and moves nothing'),
    # ★R310：这一句在批66 改了措辞（多了"里面有 N 枚东西（名单）"），旧规则**从此匹配
    # 不上真句**（★R232 那类静默假绿），新规则写在下面 ★R310 那一节里。
    (r'盒子账本写不进去，已尽量退回桌面',
     r'The box ledger could not be written; icons already moved were returned to '
     r'the desktop'),
    (r'组名不能当盒子名：(.*?)，这一组先按双属性隐藏',
     r'The group name cannot be used as a box name (\1), so this group is hidden '
     r'by attribute instead'),
    # ★R318 K4：所有权账本记不上那条 reason 同样经 `⚠ ` 上屏，逐句吃掉。
    (r'盒清单登记不上（hidden_box.json 写不进去），这一组先按双属性隐藏',
     r'The hidden-box list could not be registered (hidden_box.json could not be '
     r'written), so this group is hidden by attribute instead'),
    (r'组名不能当盒子名：', r'The group name cannot be used as a box name: '),
    (r'\[收纳档\] pack_mode=(.*?)（下一次切换起生效，本步零文件操作）',
     r'[packing mode] pack_mode=\1 (takes effect from the next switch; this step '
     r'touches no files)'),
    # ★R309（批65 #363）：切到盒子收纳时那句"要不要先备份"的两条兜底行
    (r'\[收纳档\] 备份提醒没能弹出来（切档照做）',
     r'[packing mode] the backup prompt could not be shown (the switch still '
     r'applies)'),
    (r'\[收纳档\] 备份这一步没走成（切档照做）',
     r'[packing mode] the backup step did not run (the switch still applies)'),
    (r'\[排行榜\] 可见集 (.*?) 个 → 收纳档落地',
     r'[rank] a visible set of \1 icon(s) was handed to the packing mode'),

    # ============ ★R310（批66）：公共桌面进收纳 + 散回"全同就覆盖" + 搬不走逐名点名 ====
    # 这一批把 `blocked`/`attr_fallback` 的理由句**逐名打到界面上**（历轮只报一个数字），
    # 那些句子第一次进入 English 档的上屏链 ⇒ 逐条补精确规则，不许留通配把中文尾巴
    # 带回英文行（★R302 同一口径：已知原因逐条吃掉）。
    (r'盒子里已有同名的「(.*?)」，里面有 (.*?) 枚东西（(.*?)），'
     r'不是本系统建的，这一组先按双属性隐藏',
     r'A box named \1 already exists and holds \2 item(s) (\3) that we did not '
     r'create, so this group is hidden by attribute instead'),
    (r'\[隐藏盒\] 目标盒 (.*?) 在盘上但没登记、且里面有名目 ⇒ 一枚不搬，交回属性法'
     r'（(.*?)）',
     r'[hidden-box] box \1 exists on disk but is not registered and holds items '
     r'(\2), so nothing is moved and the attribute method handles this group'),
    (r'\[隐藏盒\] 目标盒 (.*?) 在盘上但没登记，是个.*?⇒ 登记成本系统的盒，照常搬'
     r'（.*?）',
     r'[hidden-box] box \1 exists on disk but is not registered and is empty, so '
     r'it is taken over as ours and the icons still move'),
    (r'\[隐藏盒\] (.*?) 枚既没进盒子也藏不了（桌面上没有本体）：',
     r'[hidden-box] \1 icon(s) neither packed nor hidden (no file behind them on '
     r'the desktop): '),
    (r'\[盒子\] 散回「(.*?)」：桌面上同名那份与盒里字节、修改时间完全相同 '
     r'⇒ 按用户口径直接覆盖（(.*?)）',
     r'[box] returning \1: the copy on the desktop matches it byte for byte and '
     r'date for date, so it is overwritten (\2)'),
    (r'\[盒子\] 散回「(.*?)」：原目录 (.*?) 已经不在了 ⇒ 退回当前桌面',
     r'[box] returning \1: the folder it came from (\2) no longer exists, so it '
     r'lands on the current desktop instead'),
    (r'散回前比对两份同名文件失败（按"不相同"处理，不覆盖）',
     r'Cannot compare the two same-named files before returning (treated as '
     r'different, nothing is overwritten)'),
    (r'解析公共桌面失败', r'Cannot resolve the public desktop folder'),
    (r'它在公共桌面（这台电脑所有用户共用），当前权限搬不动——'
     r'以管理员身份运行本程序再收它',
     r'It sits on the public desktop (shared by every user on this PC) and the '
     r'current rights cannot move it - run DeskBits as administrator to pack it'),
    (r'它原本在公共桌面（这台电脑所有用户共用），当前权限放不回去——'
     r'以管理员身份运行本程序再散，或就让它留在盒里',
     r'It came from the public desktop (shared by every user on this PC) and the '
     r'current rights cannot put it back - run DeskBits as administrator to return '
     r'it, or leave it in the box'),
    # ★R310 扫 bug：这两句的归因反了写（原先一句"多半需要管理员"打头，而用户自己
    # 桌面上的 PermissionError 绝大多数是被程序占用）⇒ 模板跟着新文案一起改，
    # 旧规则不改就变成永不命中的死规则。
    (r'没有权限搬它（多半正被程序占用，关掉占用它的程序再收；少数情况才需要管理员）',
     r'No permission to move it (a program is probably still using it - close it '
     r'and pack again; only rarely do you need administrator rights)'),
    (r'没有权限放回它（多半正被程序占用，关掉占用它的程序再散；少数情况才需要管理员）：',
     r'No permission to put it back (a program is probably still using it - close '
     r'it and return again; only rarely do you need administrator rights): '),
    # ★R310（用户 08:40 第 1 条后半）："对不上就给桌面那一枚改个带日期的名让路" ⇒
    # 旧那句"不覆盖、不改名"从此不再产生（规则一并删），换成下面这一族。
    (r'\[盒子\] 散回「(.*?)」：桌面上同名那份与盒里对不上 ⇒ 把它改名叫 (.*?) '
     r'让路，两份都留下（(.*?)）',
     r'[box] returning \1: the copy on the desktop differs, so it is renamed to \2 '
     r'to make room and both files are kept (\3)'),
    (r'桌面上同名那枚是文件夹，不动它的名字（这一枚留在盒里）',
     r'The same-named item on the desktop is a folder, so its name is left alone '
     r'(this one stays in the box)'),
    (r'桌面上已经有同名的了，内容又对不上，连让路的名字都让不出来（这一枚留在盒里）',
     r'An item with the same name is already on the desktop and its content '
     r'differs, but there is no free name to move it to (this one stays in the box)'),
    (r'桌面上已经有同名的了，改名让它让路也没改成（多半正被占用），这一枚留在盒里：',
     r'An item with the same name is already on the desktop and renaming it to '
     r'make room failed (some program is probably holding it), so this item stays '
     r'in the box: '),
    (r'让路改了名，可这一枚没能落下，而桌面那份的名字改不回去（它现在叫 (.*?)）：',
     r'The desktop copy was renamed to make room, but this one did not land and '
     r'that name cannot be restored (it is now \1): '),
    (r'让路改了名，可这一枚没能落下 ⇒ 已把桌面那份名字改回去：',
     r'The desktop copy was renamed to make room, but this one did not land, so '
     r'its name was put back: '),
    (r'桌面上那份与盒里完全相同，但它正被占用删不掉，这一枚留在盒里：',
     r'The copy on the desktop is identical to the boxed one but is locked and '
     r'cannot be deleted, so this item stays in the box: '),
    (r'桌面上那份同名的已按"完全相同"删掉，可这一枚没能落下'
     r'（文件还在盒里，账本没清，可以再散一次）：',
     r'The identical copy on the desktop was deleted, but this one did not land '
     r'(the file is still in the box and the ledger is untouched - returning it '
     r'can be retried): '),
    (r'，(\d+) 个没能收走（桌面上没有它的文件）：',
     r'; \1 could not be packed (no file behind them on the desktop): '),
    (r'，(\d+) 个没能收进盒子：', r'; \1 could not be packed into the box: '),
    # 下面这几条不是新句子，是**历轮就有、这一批才第一次上屏**的理由（逐名点名把它们
    # 端到了界面上），一并补规则。
    (r'盒子里已经有同名的了', r'The box already holds an item with that name'),
    (r'桌面和盒子不在同一个磁盘，本版本不搬',
     r'The desktop and the box are on different drives; this version does not '
     r'move them'),
    (r'桌面上没有这个图标（或它是隐藏/系统图标）',
     r'There is no such icon on the desktop (or it is a hidden/system one)'),
    (r'搬不动（多半正被程序占用）：',
     r'Cannot move it (some program is probably holding it): '),
    (r'搬不回（多半正被程序占用）：',
     r'Cannot return it (some program is probably holding it): '),
    (r'桌面上有两个都叫「(.*?)」的（一个是快捷方式、一个是同名文件），'
     r'先改掉一个名字再收纳',
     r'Two items on the desktop both show as \1 (a shortcut and a file of the '
     r'same name), so rename one of them before packing'),
    # ============ ★R320 S5b（批75 D4）：本批新增的盒子/散回/启动自查整句 ============
    # 口径：只补"**本批新写的字面量**"——按 `run_tmp/pre_tree_r320` 差分筛出 24 句
    # （取证 `_i18n_probe_s5b.txt`），历轮那些没规则的长尾仍按 P3 挂账不动。
    # 为什么排在这里：这两句 `找不到桌面目录，这次一枚都没认领` /
    # `盒子里已经没有这个文件夹了` 的**前半截**下面 (:269/:282) 早有短语规则，
    # `translate()` 是逐条 re.sub 链、先命中的吃掉 ⇒ 整句规则必须排在短句**之前**
    # （★R230b / ★R318 K19 同一条纪律）。
    (r'找不到桌面目录，这次一枚都没认领',
     r'The desktop folder could not be found, so nothing was claimed this time'),
    (r'找不到桌面目录，这次一枚都没搬',
     r'The desktop folder could not be found, so nothing was moved this time'),
    (r'盒子里已经没有这个文件夹了',
     r'That folder is no longer in the box'),
    (r'隐藏盒清单读不动，这次一枚都没认领——先确认盒里没有你要的东西',
     r'The hidden-box list could not be read, so nothing was claimed this time'
     r' - first make sure the boxes do not hold what you want'),
    (r'隐藏盒清单读不动，这次一枚都没搬——先确认盒里没有你要的东西',
     r'The hidden-box list could not be read, so nothing was moved this time'
     r' - first make sure the boxes do not hold what you want'),
    (r'\[隐藏盒\] 认领：账本读不动 ⇒ 一发都不动（来源=(.*?)）',
     r'[hidden-box] claim: the ledger could not be read, so nothing was moved'
     r' (source=\1)'),
    (r'\[隐藏盒\] 认领：桌面目录读不出（(.*?)）⇒ 一发都不动',
     r'[hidden-box] claim: the desktop folder could not be read (\1),'
     r' so nothing was moved'),
    (r'\[隐藏盒\] 认领后登记没落盘（盒=(.*?)）——下次对账还会把它当没登记的再走一遍',
     r'[hidden-box] the registration after claiming was not saved (box=\1)'
     r' - the next reconciliation will run it again as if it were unregistered'),
    (r'\[隐藏盒\] 认领（来源=(.*?)）：从 (\d+) 只没登记的盒里找回 (\d+) 枚到桌面；'
     r'还有 (\d+) 枚没被认领（证明不了是我们的，一枚没动）(.*)',
     r'[hidden-box] claim (source=\1): \3 icon(s) came back to the desktop from'
     r' \2 unregistered box(es); \4 stayed unclaimed (they cannot be proven to'
     r' be ours, so not one of those moved) \5'),
    (r'\[隐藏盒\] 一键搬回 (.*?)：清单读不动 ⇒ 一发都不动',
     r'[hidden-box] reclaim-all \1: the list could not be read,'
     r' so nothing was moved'),
    (r'\[隐藏盒\] 一键搬回「(.*?)」（来源=(.*?)）：搬回 (\d+) 枚，搬不回 (\d+) 枚，'
     r'盒里还剩 (\d+) 枚',
     r'[hidden-box] reclaim-all "\1" (source=\2): \3 icon(s) back,'
     r' \4 could not be moved back, \5 still in the box'),
    (r'\[隐藏盒\] 显示全部：盒 (.*?) 散回后又读了一遍目录，没读动: (.*)',
     r'[hidden-box] show-all: box \1 was listed again after unboxing and that'
     r' read failed: \2'),
    (r'\[隐藏盒\] 切档前的散回没走成（切档照做）: (.*)',
     r'[hidden-box] the unboxing before the mode switch did not run'
     r' (the switch itself went ahead): \1'),
    (r'\[隐藏盒\] 启动自查：认领=(\d+) 枚 动过的盒=(\d+) 只',
     r'[hidden-box] startup box check: claimed=\1 icon(s),'
     r' boxes touched=\2'),
    (r'\[隐藏盒\] 启动自查失败（不影响其它功能）: (.*)',
     r'[hidden-box] the startup box check failed (nothing else is affected):'
     r' \1'),
    # ★R323 L4：启动自查只提醒不动手的那两行（残留计数 / 这一趟没查成）
    (r'\[隐藏盒\] 启动自查：早期让路目录残留 (\d+) 个（只提醒，没动手）',
     r'[hidden-box] startup box check: \1 old "yield" backup folder(s) left'
     r' behind (a reminder only, nothing was touched)'),
    (r'\[隐藏盒\] 启动自查：让路残留没查成（不影响其它功能）: (.*)',
     r'[hidden-box] the startup check for leftover yield folders did not run'
     r' (nothing else is affected): \1'),
    # ★R323 L3：「全部」那一发递不出回执 / 收纳档读数失败（旧写法静默 pass）
    (r'\[切换日志\] 「全部」那一发回执没递出去: (.*)',
     r'[switch-log] the "All" receipt could not be handed over: \1'),
    (r'\[隐藏盒\] 收纳档读数失败（切换日志按 hide 记）: (.*)',
     r'[hidden-box] the storage mode could not be read (the switch log falls'
     r' back to hide): \1'),
    (r'📦 启动自查：从没登记的盒子里找回 (\d+) 枚图标到桌面'
     r'（换过版本目录就会这样，以后每次启动都替你查一遍）',
     r'📦 Startup box check: \1 icon(s) were claimed back to the desktop from'
     r' boxes the ledger had forgotten (this is what happens after the program'
     r' folder changes; every start now checks it for you)'),
    (r'📦 启动自查：盒里还有 (\d+) 枚没被认领（分类名单里没有它们的名字）'
     r'——资源页的盒子面板可以一键搬回',
     r'📦 Startup box check: \1 icon(s) are still unclaimed in the boxes'
     r' (their names are not on the category list) - the box panel on the'
     r' Resources page can bring them all back'),
    (r'\[盒子\] 散回「(.*?)」：成员账本没记东西，盒目录也没读动: (.*)',
     r'[box] unboxing "\1" found nothing: the member ledger records no items'
     r' and the box folder could not be read either: \2'),
    (r'盒里有 (\d+) 枚图标成员账本没记到（多半是换了程序目录，或那份清单被另存过）'
     r'——这一条一枚没动；要全搬请在资源页的盒子面板点「搬回未认领的」',
     r'\1 icon(s) are inside the box but the member ledger never recorded them'
     r' (most likely the program folder changed, or that list was saved'
     r' elsewhere) - this action moved not a single one; to bring them all'
     r' back use "Reclaim unclaimed" in the box panel on the Resources page'),
    (r'这只盒是空的，没有要搬回来的东西',
     r'This box is empty - there is nothing to bring back'),
    (r'\[盒子\] (.*?) 目录读不动（一枚都不补记）(.*?): (.*)',
     r'[box] \1: its folder could not be read, so nothing was adopted'
     r' (\2): \3'),
    (r'\[盒子\] 成员账本读不动 ⇒ 不补记（先修好那份）',
     r'[box] the member ledger could not be read, so nothing was adopted'
     r' (fix that file first)'),
    (r'\[盒子\] 补记没落盘（(.*?) 少 (\d+) 条）——散回仍会漏这些枚',
     r'[box] the adoption did not reach the disk (\1 is short \2 entries)'
     r' - unboxing will still miss these'),
    (r'\[盒子\] (.*?)：盘上有 (\d+) 枚没记进成员账本 ⇒ 按盘上实名补记（来源=(.*?)）：'
     r'(.*)',
     r'[box] \1: \2 file(s) on disk were not in the member ledger, adopted by'
     r' their real names (source=\3): \4'),
    (r'\[隐藏盒\] 分类名单读不动（认领按"无可凭据"处理）(.*?): (.*)',
     r'[hidden-box] the category list could not be read (claiming proceeds as'
     r' if there were no evidence) \1: \2'),
    (r'找不到桌面目录', r'The desktop folder could not be found'),
    (r'盒子账本读不动，先别散',
     r'The box ledger could not be read, so nothing is returned'),
    (r'盒子账本读不动，先别搬',
     r'The box ledger could not be read, so nothing is moved'),
    # ============ ★R319（批74）：盒子拒收弹窗剩下没规则的三条 ============
    # 取证 `_r319_k9_k10_probe.txt` ①：15 条静态拒收原因里 12 条历轮早有整句规则，
    # 只有这三条从来没有 ⇒ 之前没上屏（`_fmt_refused` 压根不调词典），本批接通了
    # 调用点，这三句就当场露在 English 档的弹窗里。
    (r'桌面上已经找不到它了', r'It is no longer to be found on the desktop'),
    (r'账本写不进去，已尽量退回桌面',
     r'The ledger could not be written, so the items were put back on the '
     r'desktop as far as possible'),
    (r'盒子里已经没有这个文件', r'That file is no longer in the box'),
    (r'盒子建不起来（原因见日志）',
     r'The box could not be created (see the log for the reason)'),

    # ============ ★R320（批75）：盒子面板那两句"没被认领"读数兜底 ============
    # 这两句走 `logger.warning`，界面照常按"没有"处理，所以只在异常现场出现；
    # 没有整句规则就会整条留在 English 档（★R304-2：坑位用 `(.*?)`，
    # 源码字面量形态 `%s` 与运行期渲染形态都要能命中）。
    (r'盒 (.*?) 的"没被认领"读数失败（界面按没有处理）: (.*)',
     r'[box] the unclaimed count for box "\1" could not be read '
     r'(the panel treats it as none): \2'),
    (r'「搬回未认领的」按钮状态没刷新: (.*)',
     r'[box] the "Reclaim unclaimed" button state did not refresh: \1'),
    # ★R320 改判后这句的**理由**变了（不只是"分类名单里没有名字"，还多了
    # "成员账本记过一些、盘上又多出几枚"那一格），整句规则跟着重写。
    (r'\[隐藏盒\] 显示全部：还有 (.*?) 枚在盒里没被认领（(.*?)）——它们没登记在'
     r'成员账本里（或整盒没在分类名单上），程序不敢替你搬，资源页的盒子面板可以一键搬回',
     r'[hidden-box] show-all: \1 icon(s) are still unclaimed (\2) - they are not '
     r'in the member ledger, or the whole box is not on the category list, so '
     r'nothing is moved for you; the box panel on the Resources page can '
     r'reclaim them'),

    # ============ ★R302（批58）：建分类同名建盒 + 盒子卡三栏 ============
    # 组合句（"同名建盒没成：X（原因）"）里嵌的是 core 给的中文原因，
    # 所以**每种已知原因各写一条精确句**、通配兜底排最后——通配那条会把原因原样
    # 带回英文行（★R230b 同款漏网），已知原因必须逐条吃掉。
    (r'同名建盒没成：(.*?)（已经有叫「(.*?)」的盒子）',
     r'Auto-box for category "\1" skipped: a box by that name already exists'),
    (r'同名建盒没成：(.*?)（(.*?) 是系统保留名，不能当盒子名）',
     r'Auto-box for category "\1" skipped: "\2" is a name Windows reserves'),
    (r'同名建盒没成：(.*?)（盒子名里有不能用于文件名的字符）',
     r'Auto-box for category "\1" skipped: the name holds characters a '
     r'file name cannot use'),
    (r'同名建盒没成：(.*?)（盒子名不能为空）',
     r'Auto-box skipped: the category name is empty'),
    (r'同名建盒没成：(.*?)（盒子名太长（最多 \d+ 个字））',
     r'Auto-box for category "\1" skipped: the name is too long'),
    (r'同名建盒没成：(.*?)（盒子名不能以 \. 或 ~ 开头）',
     r'Auto-box for category "\1" skipped: names cannot start with . or ~'),
    (r'同名建盒没成：(.*?)（(.+?)）',
     r'Auto-box for category "\1" skipped: \2'),
    (r'同名建盒异常：', r'Auto-box raised: '),
    (r'已随分类同名建盒: ', r'Box created with the category name: '),
    # ★R318（批73）S6 真句回放补：`validate_box_name` 那五条原因**单独成句时**也上屏
    # （K3 的 `组名不能当盒子名：<原因>，这一组先按双属性隐藏` 走的就是这一条），
    # 历轮只在 `同名建盒没成：…（原因）` 那种整句里吃掉过，规则排在**那些整句之后**
    # ——否则先替换了尾巴，整句规则就匹配不上、外层中文反而漏出来（顺序是硬要求）。
    (r'盒子名太长（最多 (\d+) 个字）',
     r'the box name is too long (at most \1 characters)'),
    (r'盒子名里有不能用于文件名的字符',
     r'the box name holds characters a file name cannot use'),
    (r'盒子名不能为空', r'the box name cannot be empty'),
    (r'盒子名不能以 \. 或 ~ 开头',
     r'the box name cannot start with . or ~'),
    (r'(.*?) 是系统保留名，不能当盒子名',
     r'\1 is a name Windows reserves and cannot be a box name'),
    (r'读取分类名单失败（盒子卡的新建框只能手输）',
     r'Cannot read the category list (the new-box field still takes typed '
     r'names)'),
    (r'同名建盒后刷新盒子卡失败',
     r'Box was created but the box card failed to refresh'),

    # ============ ★R309（批65 #362）：「打开盒子」补建被误删的目录 ============
    # 这三条嵌着路径，规则只吃中文前缀、尾巴原样留着（★R301 同一口径）。
    (r'盒子根目录补建不出来', r'Cannot re-create the box root folder'),
    (r'盒子目录补建没成（盒根照开）',
     r'Could not re-create that box folder (the root still opens): '),
    (r'盒子账本读不动（当作没登记，不补建）',
     r'Box ledger is unreadable (so nothing counts as registered and '
     r'nothing is re-created): '),

    # ============ ★R301（批57）：📦 桌面盒子（core/desktop_box + gui/desktop_box_panel）============
    # 排在链首：这些整句里嵌着路径与系统错误详情（用户数据不是翻译素材），
    # 规则只吃掉中文前缀、尾巴原样留着。同类前缀（"盒子账本…"六条）互不为
    # 前缀，但一律按"长的写全、短的别泛指"处理，免得日后加泛规则把细节吃掉。
    (r'⚠ 桌面盒子卡加载失败，资源页该块已跳过：',
     r'⚠ Desktop box card failed to load; that block was skipped: '),
    (r'盒子账本已损坏但留底失败（拒绝写盘）',
     r'Box ledger is corrupt and no copy could be kept (write refused): '),
    (r'盒子账本形状不对，已另存留底后按空表继续',
     r'Box ledger had an unexpected shape; a copy was kept and an empty table is used: '),
    (r'盒子账本损坏，已另存留底后按空表继续',
     r'Box ledger was corrupt; a copy was kept and an empty table is used: '),
    (r'盒子账本当前读不动，拒绝覆盖写盘（先修好那份再操作）',
     r'Box ledger cannot be read right now, so the overwrite was refused '
     r'(fix that file first)'),
    (r'盒子账本读不动（拒绝写盘）',
     r'Box ledger is unreadable (write refused): '),
    (r'盒子账本写入失败，正在退回刚搬进去的图标',
     r'Box ledger could not be written; returning the icons just packed'),
    (r'盒子账本写盘失败', r'Box ledger write failed'),
    (r'已散回桌面但盒子账本没更新（重启后盒内清单可能多几条，散回判据按"文件不在盒里"自动清）',
     r'Icons were returned to the desktop but the box ledger was not updated '
     r'(the box list may show extra rows after a restart; the return path '
     r'cleans anything no longer in the box)'),
    (r'已散回桌面但盒子账本没更新',
     r'Icons were returned to the desktop but the box ledger was not updated'),
    (r'盒子已建好但账本没记下（下次刷新列表可能看不见它）',
     r'The box exists but the ledger did not record it '
     r'(it may be missing until the next refresh)'),
    (r'散回后摆位失败', r'Re-placing the returned icons failed'),
    (r'读取桌面坐标失败（收纳仍可做，散回时不摆位）',
     r'Reading desktop positions failed (packing still works; '
     r'returns will not re-place)'),
    (r'打开盒子文件夹失败', r'Failed to open the box folder'),
    (r'复制盒子路径失败', r'Failed to copy the box path'),
    (r'退回失败', r'Rollback failed'),
    (r'建盒子失败', r'Failed to create the box'),
    (r'删盒子失败', r'Failed to delete the box'),
    (r'列桌面目录失败', r'Failed to list the desktop folder'),
    (r'解析桌面目录失败', r'Failed to resolve the desktop folder'),
    (r'新建盒子: ', r'New box created: '),

    # ============ ★R304（批60 U1）：散回归位（icon_manager.apply_positions_settled + 盒子散回）============
    # 这些是"等视图→摆位→读回"这条新链留下的诊断行，English 档必须同样说得清。
    # 带数字/名字的整句写成**通配整句**，且排在自家短前缀之前（★R232：整句被短规则
    # 先吃掉半句就再也匹配不上）；尾巴上的图标名列表原样留着（用户数据不是翻译素材）。
    (r'\[apply-settled\] 读名册失败（当没这枚处理）',
     r'[apply-settled] icon roster could not be read (that icon counts as absent)'),
    (r'\[apply-settled\] 读实时坐标失败，只用目标格',
     r'[apply-settled] live coordinates could not be read; only the recorded cell is used'),
    (r'\[apply-settled\] 网格几何读不到，用默认格距',
     r'[apply-settled] grid geometry could not be read; default cell size used'),
    (r'\[apply-settled\] 自动排列闸门检查异常（照写并留痕）',
     r'[apply-settled] auto-arrange gate check failed (writing anyway, logged)'),
    (r'\[apply-settled\] 自动排列闸门=(.+?)｜关不掉时 LVM_SETITEMPOSITION '
     r'可能被 Explorer 拒（★R234），本轮照写并以读回为准',
     r'[apply-settled] auto-arrange gate=\1; while it cannot be turned off, Explorer '
     r'may refuse LVM_SETITEMPOSITION (★R234) — this pass writes anyway and trusts '
     r'the read-back'),
    (r'\[apply-settled\] (\d+) 枚等 (\d+)ms 仍没出现在桌面视图里，'
     r'这一枚一枚都没摆（多半是 Explorer 还没刷新）',
     r'[apply-settled] \1 icon(s) still had not appeared in the desktop view after '
     r'\2 ms, so none of them were placed (Explorer most likely had not refreshed)'),
    (r'\[apply-settled\] (\d+) 枚的原格被人占着、且全屏找不到空格，为不叠图标本轮没摆',
     r'[apply-settled] \1 icon(s) had their recorded cell taken and no free cell was '
     r'found, so they were left unplaced rather than stacked'),
    (r'\[apply-settled\] 读回失败，(\d+) 枚落位未知',
     r'[apply-settled] read-back failed, placement of \1 icon(s) is unknown'),
    (r'\[apply-settled\] (\d+) 枚写完读回不符（写了没落）',
     r'[apply-settled] \1 icon(s) did not match the read-back (written, not landed)'),
    (r'\[apply-settled\] (.+?) 格子换算失败，按原坐标写',
     r'[apply-settled] \1: cell math failed, the recorded pixel was used'),
    (r'\[apply-settled\] 位置缓存没写进去',
     r'[apply-settled] the in-memory position cache was not updated'),
    (r'\[apply-settled\] 簿记日志跳过',
     r'[apply-settled] bookkeeping log skipped'),
    # ★R304（批60 U3）自动收纳：这两条一条是本轮新增、一条是 ★R302 就漏下的
    # （当时没人点过"分类读不动"这条路，本轮把它变成常用路径才暴露）。
    (r'自动收纳开关没能写进设置（本次仍生效）',
     r'The auto-sort switch could not be saved to settings (still active this '
     r'session)'),
    (r'读取分类归属失败（收纳仍按下拉里那枚盒子）',
     r'Reading icon categories failed (packing still uses the box in the '
     r'dropdown)'),
    (r'散回后有 (\d+) 枚没能摆回记录的位置（文件已在桌面，只有坐标没归位）',
     r'\1 icon(s) are back on the desktop but not at their recorded position '
     r'(the files landed, only the coordinates did not)'),

    # ============ ★R304-2（批60 U2）：缩放诊断行 + 页签第④级降级 ============
    # 这四条都是"数字型整句"：诊断行的数字是要给用户看的证据，所以逐个捕获再回填，
    # 不写成抹掉数字的粗规则（★R229：抹掉数字的诊断行等于没诊断）。
    # 捕获写成 `(.*?)` 而不是 `(\d+)`：★R232 B8b 喂给词典的是**源码里那条字面量**
    # （带 `%d`/`%.3f` 占位符），而运行期喂的是渲染后的行——两种形态都要能命中，
    # 数字本身照样靠 `\1`~`\7` 原样回填（★R229：抹掉数字的诊断行等于没诊断）。
    (r'界面缩放诊断: tk scaling=(.*?) 约=(.*?)% 窗口宽=(.*?) 标题栏需求=(.*?) '
     r'页签=(.*?)枚\(内边距(.*?)\) 字号=(.*)',
     r'UI scale diagnostics: tk scaling=\1 about \2% window \3px, title bar needs '
     r'\4px, \5 tab(s) (padding \6), font \7'),
    (r'界面缩放诊断没记上', r'UI scale diagnostics could not be recorded'),
    (r'页签内边距收到最紧一档\((\d+)\)还挤，再降一级：页签只显文字'
     r'（摘掉 (\d+) 枚图标前缀），窗口宽 (\d+)、共 (\d+) 枚',
     r'Tab padding is already at its tightest (\1) and the row still does not '
     r'fit, so tabs fall back to text only (\2 icon prefix(es) hidden) - '
     r'window \3px, \4 tab(s) in total'),
    (r'页签排在最紧一档\((\d+)\)仍放不下：窗口宽 (\d+) 装不下 (\d+) 枚页签'
     r'（缩放/字号叠加的物理极限）',
     r'Tabs still do not fit at the tightest padding (\1): \3 tab(s) cannot be '
     r'placed in a \2px window (scaling and font size combined, physical limit)'),
    # ============ ★R295（批51）：gui/** 全量普查里"纯字面"那 19 条整句 ============
    # 这一批排在**链首**：整句规则若排在短语规则之后，前面的短规则会先把半句改写成
    # 英文，长句就再也匹配不上（★R229 约定"长句排在短句前面"的最狠版本）。
    # 每条都是"界面真会打出来的那一整行"，新门禁 test_r295_log_i18n_tail.py 逐条回放。
    # ★R313 D4：下载三节改成"各查各的禁写闸"后，导出的降级行换成下面这两句
    # ——旧那句一口咬定"download_history.json 读不动"，而实际坏的可能只是忽略
    # 名单（用户照着查文件查不出名堂）。坑位里插的是**ASCII 文件名**，不是用户数据。
    (r'⚠ 本机这几份下载账本读不动且留底失败（内存是空账），'
     r'本次导出\*\*不含\*\*：(.+?)；找回请先关闭占用该文件的程序再重启',
     r'⚠ These download ledgers are unreadable here and no copy could be kept '
     r'(memory holds an empty ledger); this export does **not** include: \1. '
     r'To recover them, close the program holding the file, then restart'),
    (r'⚠ 本次会话没有下载追踪器（未初始化），下载三节不进备份包；'
     r'盘上文件没事，重启程序即可',
     r'⚠ No download tracker in this session (not initialised); the three '
     r'download sections are left out of the backup. The files on disk are '
     r'fine — just restart the program'),
    (r'⚠ 设置未写盘（config/app_settings\.json 读不动且留底失败，已拒写以防覆盖）：'
     r'本次改动重启后不生效，请先关闭占用该文件的程序再重启',
     r'⚠ Settings not written (config/app_settings.json is unreadable and no '
     r'copy could be kept; the write was refused to avoid overwriting): this '
     r'change will not survive a restart — close the program holding the file, '
     r'then restart'),
    (r'⚠ 下载记录未写盘（本机那份 download_history\.json 读不动且留底失败，'
     r'已拒写以防覆盖）：内存里已合并，重启后仍是旧数据',
     r'⚠ Download records not written (the local download_history.json is '
     r'unreadable and no copy could be kept; write refused to avoid '
     r'overwriting): merged in memory only — after a restart it is still the '
     r'old data'),
    (r'⌨️ ⚠ 快捷键配置\*\*未能写入磁盘\*\*（文件被占用或只读？）：'
     r'本会话内已生效，但\*\*重启后会回到改动前\*\*——请关闭占用后重设',
     r'⌨️ ⚠ Hotkey config **could not be written to disk** '
     r'(file locked or read-only?): it works in this session, but **a restart '
     r'rolls it back** — close whatever holds the file and set it again'),
    (r'⚠ 导入的统计开关未能生效：鼠标采集线程启动失败（重启程序再试，'
     r'📊 统计页也可手动打开）',
     r'⚠ The imported statistics switch did not take effect: the mouse capture '
     r'thread failed to start (restart the app, or turn it on manually on the '
     r'📊 Statistics page)'),
    (r'⚠ 检测到你开启了"显示受保护的操作系统文件"，被分类收起的图标会以半透明显示；'
     r'建议在文件夹选项里关闭它，收起的图标才彻底不可见',
     r'⚠ "Show protected operating system files" is on, so icons collapsed by a '
     r'category appear translucent; turn it off in Folder Options and the '
     r'collapsed icons become fully invisible'),
    (r'⌨️ 检测到旧版"被占用即永久放弃"留下的瘫痪配置，已恢复默认快捷键'
     r'（Ctrl\+Alt\+E 悬浮窗 / Ctrl\+Alt\+0 显示全部 / Ctrl\+Alt\+1~9 切分类）',
     r'⌨️ A broken config left by the old "give up permanently once occupied" '
     r'behaviour was detected; default hotkeys restored (Ctrl+Alt+E dock / '
     r'Ctrl+Alt+0 show all / Ctrl+Alt+1~9 switch category)'),
    (r'📂 已恢复上次状态 \[全部\]（退化直调）',
     r'📂 Last state restored [All] (fallback direct call)'),
    (r'📂 已恢复上次状态 \[全部\]', r'📂 Last state restored [All]'),
    (r'✓ 已导入高亮/框选设置：规则页的勾选框与桌面上的框已即时跟着变（无需重启）',
     r'✓ Highlight/selection settings imported: the checkboxes on the Rules '
     r'page and the boxes on the desktop changed right away (no restart needed)'),
    (r'🧹 已当面说明过"刚才是程序在动鼠标"，连续计数归零',
     r'🧹 Already explained that "the program was moving the mouse"; the streak '
     r'counter reset'),
    (r'🧹 这一次没人碰鼠标，"想去动鼠标"的连续计数归零',
     r'🧹 Nobody touched the mouse this time; the "about to move the mouse" '
     r'counter reset'),
    (r'🧹 清理诊断日志：用户取消，未做任何改动',
     r'🧹 Clean diagnostic logs: cancelled by user, nothing changed'),
    (r'⚙️ \[selfcheck\] 随登录自启仅记录，未触碰系统任务',
     r'⚙️ [selfcheck] start-on-login was only recorded, system tasks untouched'),
    (r'🎨 程序图标已恢复默认（01_B 拾碎成序）',
     r'🎨 App icon reset to default (icon 01_B)'),
    (r'🖼️ 锁屏壁纸 = 无（下次锁屏生效）',
     r'🖼️ Lock-screen wallpaper = none (takes effect at the next lock)'),
    (r'📅 计划任务列宽已按手动调整，备注列不再自动铺满窗口',
     r'📅 Scheduled-task column widths are now manual; the notes column no '
     r'longer fills the window'),
    (r'↳ 开机触发已自动开启「错过补跑」，登录后会立刻补运行一次',
     r'↳ Boot trigger: "run if missed" was enabled automatically; it runs once '
     r'right after sign-in'),
    (r'⚠ 工作目录未生效（任务照常创建），程序可能因相对路径起不来，详见日志',
     r'⚠ Working directory not applied (the task is still created); the app may '
     r'fail to start because of relative paths — see the log'),
    # ============ 悬浮条 / 分类切换（core/dock.py）============
    (r'✓ 已切换到 \[([^\]]+)\]：显示 (\d+) 个图标',
     r'✓ Switched to [\1]: showing \2 icons'),
    (r'，常驻保留 (\d+) 个', r', kept \1 pinned'),
    (r'，(\d+) 个系统图标跳过', r'; \1 system icons skipped'),
    (r'，隐藏 (\d+) 个', r', hid \1'),
    (r'（([\d.]+)s）', r' (\1s)'),
    (r'⚠ 桌面网格约可摆放 (\d+) 个图标（含副屏），'
     r'分类 \[([^\]]+)\] 需要 (\d+) 个——多出的会排在屏幕外看不到',
     r'⚠ Desktop grid fits ~\1 icons (incl. second screen); '
     r'category [\2] needs \3 — extras fall off-screen'),
    (r'⚠ 桌面网格约可摆放 (\d+) 个图标（含副屏），'
     r'当前全部图标 (\d+) 个——多出的会排在屏幕外看不到',
     r'⚠ Desktop grid fits ~\1 icons (incl. second screen); '
     r'showing \2 — extras fall off-screen'),
    (r'✓ 已显示全部图标（恢复 (\d+) 个）', r'✓ All icons shown (\1 restored)'),
    (r'✓ 已显示(全部|所有)图标', r'✓ All icons shown'),
    (r'✓ 已隐藏(全部|所有)图标', r'✓ All icons hidden'),
    # ★R254：点「隐藏」后补的那句提示（English 档日志页不许剩裸中文）
    (r'ℹ 退出或崩溃后会自动恢复桌面显示',
     r'ℹ Desktop auto-restores on exit or crash'),
    (r'\[([^\]]+)\] 空分类，已隐藏全部图标',
     r'[\1] is empty — all icons hidden'),
    (r'⚠ 未读取到桌面图标', r'⚠ No desktop icons found'),
    (r'💾 已自动备份当前桌面布局（可用"恢复布局"找回）',
     r'💾 Current layout auto-backed up ("Restore Layout" gets it back)'),
    (r'⚠ 隐藏状态写盘失败，本次切换未记账——下次切换会重试',
     r'⚠ Hidden-state write failed; this switch was not recorded '
     r'— will retry next switch'),
    (r'⚠ 切换分类出错: ', r'⚠ Category switch failed: '),
    (r'^悬浮窗已禁用，请在设置中开启$',
     r'Dock is disabled — enable it in Settings'),
    (r'👶 家长模式：请先退出后再切换分类',
     r'👶 Parent mode: exit it before switching categories'),
    (r'📂 尚未保存文件夹组：先在主界面"📂 资源管理"页"存为组"',
     r'📂 No folder group saved yet: use "Save as group" '
     r'on the 📂 Resources page'),
    (r'👀 看护让位：\[([^\]]+)\] 判定为手动整理，随你摆',
     r'👀 Watch stood down: [\1] looks like manual tidying, left as is'),
    (r'👀 看护：(\d+) 个图标漂移已自动归位',
     r'👀 Watch: \1 drifted icons snapped back'),
    (r'🛡 检测到 (\d+)/(\d+) 个图标位置瞬间跳变'
     r'（刷新/显示拓扑切换回退），已自动恢复保存的布局',
     r'🛡 \1/\2 icons jumped at once (refresh/display-topology revert)'
     r' — saved layout restored'),
    (r'🛡 守护熔断：10 分钟内已自动拉回 3 次图标仍被改写，'
     r'疑似与其他机制冲突——已自动停用布局守护，防止图标反复拉扯。'
     r'重新保存/套用布局会自动重新武装。',
     r'🛡 Guard fuse: icons were rewritten 3 times within 10 min despite '
     r'auto pull-back — something else owns the layout, so guard is off. '
     r'Saving/applying a layout arms it again.'),
    (r'🛡 布局守护已开启：', r'🛡 Layout guard ON: '),
    (r'🛡 布局守护已关闭', r'🛡 Layout guard OFF'),
    (r'🛡 布局守护已以此全量布局 \[([^\]]+)\] 为基准',
     r'🛡 Layout guard now baselined on full layout [\1]'),
    (r'🛡 布局守护已以此布局 \[([^\]]+)\] 为基准'
     r'（遇到瞬间大面积回退会自动恢复）',
     r'🛡 Layout guard now baselined on layout [\1] '
     r'(auto-restores after a sudden mass revert)'),

    # ============ 数据管理：导出 / 导入 / 备份 / 清空 ============
    (r'✓ 已导出全部数据 → ', r'✓ All data exported → '),
    (r'✓ 已导入全部数据（多数项已即时生效，个别未刷新可重启程序）',
     r'✓ All data imported (most panels applied live; '
     r'restart the app if one looks stale)'),
    # ★R325 F2：换机导入时那一条"路径按本机重算"的诊断行（旧措辞那条从来没进词典，
    # 现在这条是新的 ⇒ 一并进，英文档里不许留整句中文）。
    (r'\[导入全部\] 盒成员账本里 (\d+) 枚的路径已按本机重算（盒内位置按本机盒根、'
     r'来路按本机桌面；那份备份来自别的目录/别的机器）',
     r'[import-all] \1 boxed member record(s) had their paths recomputed for this '
     r'machine (where each sits in its box follows this machine\'s box root, where '
     r'each came from follows this machine\'s desktop; that backup came from another '
     r'folder or another machine)'),
    (r'✓ 已并入下载忽略规则 (\d+) 条、已忽略条目黑名单 (\d+) 条'
     r'（这些下载不会再复活）',
     r'✓ Merged \1 download ignore rules and \2 ignored entries '
     r'(they will not come back)'),
    (r'✓ 已导入 (\d+) 条下载记录（按路径去重合并）',
     r'✓ Imported \1 download records (deduped by path)'),
    (r'📥 已导入布局存档 (\d+) 份', r'📥 Imported \1 layout archive(s)'),
    (r'（同名跳过 (\d+)）', r', \1 skipped (same name)'),
    (r'（失败 (\d+)）', r', \1 failed'),
    (r'📥 已导入使用统计 (\d+) 条', r'📥 Imported \1 usage-stat entries'),
    (r'📥 已导入分类点击 (\d+) 条', r'📥 Imported \1 category-click records'),
    (r'📥 已导入条件监视 (\d+) 条（全部为停用状态，请逐条确认后手动启用）',
     r'📥 Imported \1 condition monitor(s) — all disabled, '
     r'review then enable manually'),
    # ★R322 J5：盒子那两本账（所有权 + 成员）跟着盒子搬进盒根，导入/导出各新增两句
    (r'📥 已导入(.+?)（(\d+) 只盒）', r'📥 Imported \2 box(es) into \1'),
    (r'⚠ 本机那份(.+?)读不动，已另存留底（(.+?)），再写入导入包里的这份',
     r'⚠ The local \1 was unreadable - set aside first (\2), then wrote the '
     r'copy from this import package'),
    (r'ℹ️ 计划任务不随数据导入（属系统级配置，需在本机重建）',
     r'ℹ️ Scheduled tasks are not imported (machine-level, rebuild here)'),
    (r'ℹ️ 该文件不含 布局存档/使用统计/分类点击'
     r'（旧版导出的文件没有这三项，用新版重新导出即可）',
     r'ℹ️ This file has no layouts / usage stats / category clicks '
     r'(old exports lack them — re-export with the current version)'),
    (r'⏹ 备份内有 (\d+) 条规则：规则体系已退役，已跳过导入',
     r'⏹ \1 rules in the backup: the rule engine is retired, import skipped'),
    (r'✓ 已导出文件夹组 → ', r'✓ Folder groups exported → '),
    (r'✓ 已合并导入文件夹组：新增 (\d+) 个、覆盖 (\d+) 个，'
     r'现共 (\d+) 个（详见 📦 数据管理 页列表）',
     r'✓ Folder groups merged: \1 added, \2 replaced, \3 total '
     r'(see the 📦 Data page list)'),
    (r'🗑️ 已清空全部文件夹组', r'🗑️ All folder groups cleared'),
    (r'🔒 已清空全部个人数据（图标名单/文件夹组/布局/统计/日志/UI痕迹）',
     r'🔒 All personal data cleared (icon lists / folder groups / layouts '
     r'/ stats / logs / UI traces)'),
    (r'🔒 个人数据清空完成，但文件夹组未清空（读取保护），暂不建议分发',
     r'🔒 Personal data cleared, but folder groups were SKIPPED '
     r'(read protection) — do not distribute yet'),
    # ★R285-N9：清空时"下载那四份没删净"新增的一行。括号里只有文件名（ASCII），
    # 所以可以原样带进英文行（★R230b 那条"别把中文变量插进两份模板"的坑在这儿
    # 不成立——插进去的是 download_undo.json 这类字面量）。
    (r'🔒 个人数据清空完成，但下载记忆未删净（([\w.,*\-、/ ]+)），'
     r'暂不建议分发',
     r'🔒 Personal data cleared, but download memory is still on disk '
     r'(\1) — do not distribute yet'),
    (r'⚠ 文件夹组写盘失败（读取失败保护或磁盘问题），该部分未生效，详见日志',
     r'⚠ Folder-group write failed (read protection or disk issue); '
     r'that part did not take effect — see log'),
    (r'⚠ 导入未完全生效：', r'⚠ Import only partly applied: '),
    # ★R231-7：上面那条通配只翻到冒号，本批新写的"跳过节"尾巴单独补
    # （与 gui/data_manage_page.py 的 _skip 日志逐字对齐，中文为唯一真源）
    (r' 未写入（损坏原件未留底：再点一次同一按钮，在"先留底再处理"的确认框选确定即可）',
     r' not written (corrupt original was not set aside: click the same '
     r'button again and choose OK in the "set it aside first" prompt)'),
    # ★R233（用户 08 批 item1/item4）：本批两条新写的界面日志——不补就是
    # English 档日志页露中文（中文仍是唯一真源，只翻显示层）
    (r'↩ 导入中断，已回滚 (\d+) 个数据文件（内存已同步，建议重启程序）',
     r'↩ Import aborted, rolled back \1 data files (memory resynced - '
     r'restart the app)'),
    (r'⛔ 已拒绝打开一个不是文件夹的条目（详见日志）',
     r'⛔ Refused to open an entry that is not a folder (see log)'),
    (r' 写盘失败（文件被占用/只读？），详见日志',
     r' failed to write (file locked or read-only?), see log'),
    (r'📦 已导出 (\d+) 份布局 → ', r'📦 Exported \1 layout(s) → '),
    (r'📥 导入布局完成：成功 (\d+)，失败 (\d+)',
     r'📥 Layout import done: \1 ok, \2 failed'),
    (r'✓ 已导出图标分类清单 → ', r'✓ Icon/category manifest exported → '),
    (r'🗑️ 已清空全部分类的图标名单（图标本身未动）',
     r'🗑️ Icon name lists cleared for all categories (icons untouched)'),
    (r'📥 已按清单导入：(\d+) 个分类，新增归属 (\d+) 项',
     r'📥 Manifest imported: \1 categories, \2 new memberships'),
    # ★R313 F1：写盘未生效那句（English 档必须照样说得出"没落盘"）
    (r'⚠ 按清单导入：归属写入未生效 (\d+) 项'
     r'（分类文件被占用/只读/被杀软拦截），磁盘还是老数据，请排查后重试',
     r'⚠ Manifest import: \1 membership(s) were NOT written to disk '
     r'(categories file locked, read-only, or blocked by antivirus) - '
     r'fix the cause and retry'),
    (r'✓ 已导出分类数据 → ', r'✓ Categories exported → '),
    (r'✓ 已导入 (\d+) 个分类', r'✓ Imported \1 categories'),
    (r'🔄 正在重启资源管理器以固化布局（打开的文件夹窗口会关闭）…',
     r'🔄 Restarting Explorer to commit the layout '
     r'(open folder windows will close)…'),
    (r'✓ 资源管理器已重启，布局已固化',
     r'✓ Explorer restarted, layout committed'),
    (r'检测到系统已自动重启资源管理器，跳过手动启动',
     r'Explorer already restarted by the system — skipping manual launch'),
    (r'📦 桌面文件约 ([\d.]+) MB，开始备份…',
     r'📦 Desktop files ~\1 MB, backing up…'),
    (r'📦 已取消桌面图标备份（文件过大）',
     r'📦 Desktop icon backup cancelled (files too large)'),
    (r'📁 备份路径已设为: ', r'📁 Backup folder set to: '),

    # ============ 管理页：隐藏 / 布局 / 缓存 ============
    (r'✓ 已保存全部布局 \[([^\]]+)\] (\S+) 个图标（含被分类隐藏的图标）',
     r'✓ Saved full layout [\1], \2 icons (incl. category-hidden ones)'),
    (r'✓ 已保存布局 \[([^\]]+)\] (\d+) 个图标',
     r'✓ Saved layout [\1], \2 icons'),
    # ★R229 尾注：保存布局时挂在后面的那句（前一条规则翻完它还在），单独补一条。
    (r'（切换到该分类时将自动套用）',
     r'(applied automatically when switching to this category)'),
    (r'（含被分类隐藏的图标）', r'(incl. category-hidden icons)'),
    (r'✗ 保存全部失败: ', r'✗ Save-all failed: '),
    (r'✗ 恢复图标出错: ', r'✗ Failed to restore icons: '),
    (r'✗ 错误: ', r'✗ Error: '),
    (r'⚠ 主窗口隐藏中（悬浮窗模式），照常执行',
     r'⚠ Main window hidden (dock mode) — running anyway'),
    (r'🧹 已清理 (\d+) 个失效图标名', r'🧹 Cleaned \1 stale icon names'),
    (r'✓ 已删除 (\d+) 个分类: ', r'✓ Deleted \1 categories: '),
    (r'🗑️ 已删除 (\d+)/(\d+) 个布局', r'🗑️ Deleted \1/\2 layouts'),
    (r'✓ 已创建分类: ', r'✓ Category created: '),
    (r'（点"📌 顶部"可移出悬浮窗）', r' (pin it out of the dock to move it)'),
    (r'🖱️ 已打开拖拽分类面板', r'🖱️ Drag-to-categorize panel opened'),
    (r'🔄 正在重建图标缓存…', r'🔄 Rebuilding the icon cache…'),
    (r'✓ 已删除 (\d+) 个缓存文件并重启资源管理器，图标缓存已重建',
     r'✓ Deleted \1 cache files and restarted Explorer — '
     r'icon cache rebuilt'),
    (r'⚠ taskkill 返回 (\d+)，0 个缓存被删除（文件被锁定）',
     r'⚠ taskkill returned \1, 0 cache files deleted (locked)'),
    (r'⚠ 资源管理器未自动重启，请手动启动 explorer\.exe',
     r'⚠ Explorer did not restart — start explorer.exe manually'),
    (r'✗ 资源管理器重启失败，请手动启动 explorer\.exe',
     r'✗ Explorer restart failed — start explorer.exe manually'),
    (r'✗ 重建失败: ', r'✗ Rebuild failed: '),
    (r'🎨 分类 \[([^\]]+)\] 颜色已改为 (\S+)',
     r'🎨 Category [\1] recolored to \2'),
    (r'➖ 已从 \[([^\]]+)\] 移除 (\d+) 个图标',
     r'➖ Removed \2 icons from [\1]'),
    (r'✓ 已重命名为: ', r'✓ Renamed to: '),
    (r'✏️ 布局已重命名: ', r'✏️ Layout renamed: '),
    (r'🎨 悬浮条文字色已恢复默认', r'🎨 Dock text color reset to default'),
    (r'🎨 悬浮条文字色已更新: ', r'🎨 Dock text color updated: '),
    (r'📌 \[([^\]]+)\] 已显示在顶部', r'📌 [\1] shown in the dock'),
    (r'📌 \[([^\]]+)\] 已从顶部移除', r'📌 [\1] removed from the dock'),
    (r'🤖 \[([^\]]+)\] 已常驻桌面（切换分类时保持可见）',
     r'🤖 [\1] pinned to the desktop (stays visible across categories)'),
    (r'🤖 \[([^\]]+)\] 已取消常驻', r'🤖 [\1] unpinned'),

    # ============ 下载卡（gui/download_panel.py）============
    (r'➕ 手动添加下载记录 (\d+) 条', r'➕ Manually added \1 download record(s)'),
    (r'✕ 已忽略 \[([^\]]+)\] 下 (\d+) 条记录（点"↩ 撤回忽略"可找回）',
     r'✕ Ignored \2 record(s) under [\1] ("↩ Undo" restores them)'),
    (r'✕ 已忽略此条（点"↩ 撤回忽略"可找回）：',
     r'✕ Ignored this entry ("↩ Undo" restores it): '),
    (r'↩ 已撤回忽略：', r'↩ Ignore undone: '),
    (r'🚫 已忽略目录（新下载不再记录）：',
     r'🚫 Folder ignored (new downloads here are not recorded): '),
    (r'♻ 已取消忽略规则 (\d+) 条：', r'♻ Removed \1 ignore rule(s): '),

    # ============ 快捷键（gui/hotkey_panel.py）============
    (r'⌨️ 快捷键已更新: ', r'⌨️ Hotkey updated: '),
    (r'⌨️ 已恢复默认快捷键方案', r'⌨️ Default hotkey scheme restored'),
    # ★R285-N28：热键读侧闸门拒写那一行（整句、无中文插值，English 档要能翻干净）
    (r'⌨️ ⚠ 快捷键配置未写入磁盘：本会话读不动 hotkeys.json（原件也留不住底），'
     r'为防用出厂默认键位覆盖你的自定义，已拒写——重启程序后可恢复',
     r'⌨️ ⚠ Hotkey config was NOT written to disk: hotkeys.json is unreadable '
     r'this session (its original could not be kept aside either) — refused so '
     r'your custom keys are not overwritten with factory defaults. Restart the '
     r'app to recover.'),
    # ★R285-O2：热键线程失联看门狗那两行（数字用捕获组，整句可译）
    (r'⌨️ ⚠ 全局热键曾失效，已自动恢复（第 (\d+) 次）',
     r'⌨️ ⚠ Global hotkeys had stopped responding — auto-restarted (attempt \1)'),
    (r'⌨️ 全局热键自动恢复已达上限（3 次），本轮不再重试——重启程序后可恢复',
     r'⌨️ Global-hotkey auto-restart hit its 3-attempt cap — no more retries '
     r'this session; restarting the app restores them'),
    (r'⌨️ 当前没有"只按 Alt"的热键组合，无需修改',
     r'⌨️ No bare-Alt hotkeys found — nothing to change'),
    (r'🔧 已改：', r'🔧 Changed: '),
    (r'。其他软件（记事本/浏览器/Office）的菜单快捷键不再被'
     r'抢走；想换别的键，双击面板里那一行即可',
     r'. Menu shortcuts in other apps (Notepad / browsers / Office) '
     r'are no longer stolen; double-click a row in the panel to rebind.'),
    (r'🔒 锁屏中，快捷键已忽略', r'🔒 Screen locked — hotkey ignored'),
    (r'🔒 家长模式中，"显示全部"已禁用',
     r'🔒 Parent mode: "Show all" is disabled'),
    (r'🔒 家长模式中，"快速定位"已禁用',
     r'🔒 Parent mode: "Quick find" is disabled'),
    (r'🔒 家长模式中，分类切换热键已禁用',
     r'🔒 Parent mode: category hotkeys are disabled'),
    (r'⚠ 快捷键：分类槽位 (\d+) 不存在', r'⚠ Hotkey: category slot \1 missing'),
    (r'本次启动被其他程序占用，悬浮窗开关热键暂不可用'
     r'（配置已保留，下次启动自动重试；长期冲突可在快捷键面板换键）',
     r'is taken by another app this session — the dock toggle hotkey is '
     r'off (config kept, retried next start; rebind it in the hotkey panel)'),
    (r'本次启动被其他程序占用，"显示全部"热键暂不可用'
     r'（配置已保留，下次启动自动重试）',
     r'is taken by another app this session — "Show all" is off '
     r'(config kept, retried next start)'),
    (r'本次被占用，这些分类热键暂不可用（其余照常，下次启动自动重试）',
     r'is taken this session — these category hotkeys are off '
     r'(the rest work; retried next start)'),
    (r'^⌨️ \[', r'⌨️ ['),

    # ============ 设置页（gui/settings_panel.py）============
    (r'⚠ 以下分类应用失败（改名/删除后请重启程序刷新面板）: ',
     r'⚠ These categories failed to apply (restart to refresh the panel): '),
    (r'🤖 公共分类显示已应用：常驻=(.+?)，只显示自己=(.+?)，'
     r'只显示自己\+隐系统=(.+?)，系统图标常驻=(.+)$',
     r'🤖 Public category display applied: pinned=\1, self-only=\2, '
     r'self-only+hide-system=\3, system-icons-pinned=\4'),
    (r'🧹 整理桌面模式已开启：布局守护暂停，拖完请取消勾选并重新保存布局',
     r'🧹 Desktop tidy mode ON: layout guard paused — untick it and '
     r're-save the layout when you are done'),
    (r'🧹 整理桌面模式已关闭：布局守护恢复',
     r'🧹 Desktop tidy mode OFF: layout guard resumed'),
    (r'📌 横条分类槽位上限: ', r'📌 Dock category slot limit: '),
    (r'📌 悬浮窗字体已设为 \[([^\]]+)\]', r'📌 Dock font set to [\1]'),
    (r'📌 悬浮窗置顶已开启（关闭时会被其他窗口覆盖）',
     r'📌 Dock always-on-top ON (turning it off lets other windows '
     r'cover it)'),
    (r'📌 悬浮窗置顶已关闭（关闭时会被其他窗口覆盖）',
     r'📌 Dock always-on-top OFF (other windows can cover it)'),
    (r'📂 已开启：切分类时自动套用同名布局的摆放',
     r'📂 ON: switching a category applies the layout of the same name'),
    (r'📂 已关闭：切分类不再自动挪动图标位置',
     r'📂 OFF: switching a category no longer moves icons'),
    (r'⚙️ 关闭主窗口时：', r'⚙️ On closing the main window: '),
    (r'保留悬浮窗后台运行', r'dock keeps running in the background'),
    (r'一并退出', r'app quits as well'),
    (r'（横条右端 ✕ 可随时退出）', r' (the ✕ at the dock end quits anytime)'),
    (r'⚙️ 退出时恢复布局已启用', r'⚙️ Restore-on-exit enabled'),
    (r'⚙️ 退出时恢复布局已禁用', r'⚙️ Restore-on-exit disabled'),
    (r'⚙️ 退出恢复设置已保存: 启用 / ',
     r'⚙️ Restore-on-exit settings saved: enabled / '),
    (r'⚙️ 退出恢复设置已保存: 禁用 / ',
     r'⚙️ Restore-on-exit settings saved: disabled / '),
    (r'请先在下拉框选择一个布局；"当前"表示保持现状，无需套用',
     r'Pick a layout in the dropdown first; "Current" keeps things as they are'),
    (r'✓ 已套用布局 \[([^\]]+)\]：匹配 (\S+) 个图标',
     r'✓ Applied layout [\1]: \2 icons matched'),
    (r'🔤 界面字号已设为 \[([^\]]+)\]', r'🔤 UI font size set to [\1]'),
    (r'🎨 界面显示已设为 \[([^\]]+)\]', r'🎨 UI density set to [\1]'),

    # ============ 系统页：计划任务 / 监视 / 锁屏 / 维护 ============
    (r'📅 计划任务已创建: ', r'📅 Scheduled task created: '),
    (r'📅 计划任务已删除: ', r'📅 Scheduled task deleted: '),
    (r'📅 计划任务已修改: ', r'📅 Scheduled task modified: '),
    (r'📅 计划任务\[(.+?)\] (.+?) 完成', r'📅 Scheduled task [\1] \2 done'),
    # ★R331/★R332-c：存量虚拟机启动脚本自愈那句（刷新页与启动探测两条都走这句）
    (r'🔧 已修好 (\d+) 个老版本的虚拟机启动脚本'
     r'（以前虚拟机明明启动成功了也会报失败、还会白等 20 秒）',
     r'🔧 Fixed \1 legacy VM start script(s) — they used to report a failed '
     r'start even when the VM came up, and waited 20s for nothing'),
    (r'🧩 模板已填入创建器: ', r'🧩 Template loaded into the creator: '),
    (r'👀 已编辑条件监视 \[([^\]]+)\]', r'👀 Condition monitor [\1] edited'),
    (r'👀 已添加条件监视 \[([^\]]+)\]（默认停止 · 点 ▶ 启用后才开始监视）',
     r'👀 Condition monitor [\1] added (disabled by default — '
     r'press ▶ to start watching)'),
    (r'⚠ 条件监视 \[([^\]]+)\] 编辑未保存（禁写闸或磁盘故障），详见日志',
     r'⚠ Condition monitor [\1] edit not saved (write guard or disk) '
     r'— see log'),
    (r'⚠ 条件监视 \[([^\]]+)\] 添加失败（重名或进程名为空），详见日志',
     r'⚠ Failed to add condition monitor [\1] (duplicate or empty '
     r'process) — see log'),
    (r'👀 已删除条件监视 \[([^\]]+)\]', r'👀 Condition monitor [\1] deleted'),
    (r'👀 \[([^\]]+)\] 已启用 ▶', r'👀 [\1] enabled ▶'),
    (r'👀 \[([^\]]+)\] 已停止 ⏸', r'👀 [\1] stopped ⏸'),
    (r'👀 条件命中 \[([^\]]+)\]：', r'👀 Condition hit [\1]: '),
    (r'⚠ 主机将在 30 秒后关机（shutdown /a 可取消）',
     r'⚠ Host will shut down in 30s (cancel with shutdown /a)'),
    (r'⚠ 主机将在 30 秒后重启（shutdown /a 可取消）',
     r'⚠ Host will reboot in 30s (cancel with shutdown /a)'),
    # ★R333-f（P1）：★R332-a 那三句"排定没成功"的新文案当时只写了中文，English 档
    # 整句留汉字（实测 `coverage()` 逐条 0）——★R323 S6 抓过的同一个漏：**判据范围
    # ≠ 改动范围**。坑位里的返回码/异常原文照旧留着（那是排障证据，不是翻译素材）。
    (r'⚠ 这次关机没能排定：shutdown\.exe 没发出去（(.+?)）——机器不会自己关机',
     r'⚠ This shutdown was not scheduled: shutdown.exe could not be sent (\1)'
     r' — the machine will not shut down by itself'),
    (r'⚠ 这次重启没能排定：shutdown\.exe 没发出去（(.+?)）——机器不会自己重启',
     r'⚠ This reboot was not scheduled: shutdown.exe could not be sent (\1)'
     r' — the machine will not reboot by itself'),
    (r'⚠ 这次关机没能排定（返回码 (-?[\w.]+)）——系统没答应，机器不会自己关机',
     r'⚠ This shutdown was not scheduled (exit code \1) — the system refused, '
     r'the machine will not shut down by itself'),
    (r'⚠ 这次重启没能排定（返回码 (-?[\w.]+)）——系统没答应，机器不会自己重启',
     r'⚠ This reboot was not scheduled (exit code \1) — the system refused, '
     r'the machine will not reboot by itself'),
    (r'⚠ 关机排定的结果读不到（(.+?)）——没法确认机器会不会自己关机；'
     r'倒计时若真的开始了，请立刻运行 shutdown /a 取消，并把这段日志发来',
     r'⚠ Could not read the shutdown schedule result (\1) — cannot confirm '
     r'whether the machine will shut down; if a countdown really started, run '
     r'shutdown /a at once to cancel it and send this log'),
    (r'⚠ 重启排定的结果读不到（(.+?)）——没法确认机器会不会自己重启；'
     r'倒计时若真的开始了，请立刻运行 shutdown /a 取消，并把这段日志发来',
     r'⚠ Could not read the reboot schedule result (\1) — cannot confirm '
     r'whether the machine will reboot; if a countdown really started, run '
     r'shutdown /a at once to cancel it and send this log'),
    # ★R333-a2 那句"反复起停"的冷却提醒走 `gui._log` 上屏 ⇒ 同样要翻净
    (r'⚠ \[([^\]]+)\] 这个进程在反复起停，(\d+) 秒内不再重复触发'
     r'（想马上恢复提醒，把该条关掉再打开）',
     r'⚠ [\1] this process keeps starting and stopping - no retrigger within '
     r'\2s (toggle the rule to get reminders back)'),
    # ★R333 S6-3：空进程名那句提示同样走 `gui._log` 上屏 ⇒ English 档也要翻净
    (r'⚠ \[([^\]]+)\] 这条没填进程名，不会触发任何动作（编辑该条填上进程名即可）',
     r'⚠ [\1] has no process name - it will never trigger (edit the rule and '
     r'fill in a process name)'),
    # ★R333-f：`_notify` 那枚**对话框**正文里 desc 的那句白话（★R230b：English 档
    # 不许留汉字尾巴）。调用方现在把英文那份也过一遍 `translate()`，这两条就是它
    # 要命中的句子——只管"已退出/已启动 → 动作[x]"这一段，进程名与动作码原样留着。
    (r'已退出 → 动作\[([^\]]+)\]', r'has exited -> action [\1]'),
    (r'已启动 → 动作\[([^\]]+)\]', r'has started -> action [\1]'),
    # ★R318 A：这四句是「取消这一次关机/重启」那条链的当面回执，走 `gui._log`
    # 上屏 ⇒ English 档必须翻净（★R230b）。坑位里的**返回码与异常原文要留着**
    # （本模块口径=只翻界面文案，错误详情是排障证据，不是翻译素材）。
    # 长句排在短前缀之前。
    (r'⚠ 取消失败：shutdown /a 没能发出去',
     r'⚠ Cancel failed: shutdown /a could not be sent'),
    (r'⚠ 取消的结果读不到（(.+?)）——请自己核对倒计时还在不在',
     r'⚠ Could not read the cancel result (\1) — please check yourself '
     r'whether the countdown is still pending'),
    (r'⚠ 取消没成功（返回码 (-?[\w.]+)）——多半是倒计时已经走完，'
     r'或者当前并没有排队中的关机(.*)',
     r'⚠ Cancel did not take effect (exit code \1) — the countdown had most '
     r'likely already finished, or no shutdown is queued right now\2'),
    (r'✅ 已取消刚才排定的关机/重启倒计时',
     r'✅ Cancelled the shutdown/reboot countdown scheduled just now'),
    (r'已执行命令: ', r'Command executed: '),
    (r'🔒 锁屏独立密码已设置', r'🔒 Lock-screen password set'),
    (r'🔒 锁屏字体已设为 \[([^\]]+)\]（下次锁屏生效）',
     r'🔒 Lock-screen font set to [\1] (takes effect next lock)'),
    (r'⚠ 未设密保：忘记锁屏密码将只能重启（建议尽快补设）',
     r'⚠ No recovery questions set — if you forget the lock password '
     r'you must restart the PC (set them soon)'),
    (r'🖼️ 锁屏图片已设置：(.+?)（下次锁屏生效）',
     r'🖼️ Lock-screen image set: \1 (takes effect next lock)'),
    (r'🖼️ 锁屏图片已清除（恢复默认 🔒）',
     r'🖼️ Lock-screen image cleared (default 🔒 restored)'),
    (r'🔒 未设置密码，已取消锁屏', r'🔒 No password set — lock cancelled'),
    (r'🔓 系统"(.+?)"入口已恢复', r'🔓 System "\1" entry restored'),
    (r'🔒 已隐藏系统"(.+?)"入口（解锁自动恢复）',
     r'🔒 System "\1" entry hidden (auto-restored on unlock)'),
    (r'⚡ (.+?)：执行中…', r'⚡ \1: running…'),
    (r'：已在确认框取消', r': cancelled in the confirm box'),
    (r'：完成 ✓', r': done ✓'),
    (r'：失败 ✗', r': failed ✗'),
    (r'📜 过滤脚本已生成并运行：', r'📜 Filter script generated and run: '),
    (r'⚠ 事件日志组件加载失败，事件查看器不可用（其余功能不受影响）',
     r'⚠ Event-log component failed to load — Event Viewer is '
     r'unavailable (everything else works)'),

    # ============ 家长 / 儿童模式 ============
    (r'🔐 家长密码已设置（加密存储，不保存明文）',
     r'🔐 Parent password set (stored hashed, never in plain text)'),
    (r'🔐 家长密码已设置', r'🔐 Parent password set'),
    (r'🧹 家长密码已清除（密保一并清除，恢复默认为空）',
     r'🧹 Parent password cleared (recovery questions too, back to empty)'),
    (r'🧹 家长密码已清除（恢复默认为空）',
     r'🧹 Parent password cleared (back to default empty)'),
    (r'🧹 密保问题已清除（密码保留）',
     r'🧹 Recovery questions cleared (password kept)'),
    (r'🔐 密保问题已保存（(\d+) 个）', r'🔐 Recovery questions saved (\1)'),
    (r'👶 家长模式已开启，主窗口已隐藏（横条"退出"需密码）',
     r'👶 Parent mode ON, main window hidden (the dock "Exit" needs '
     r'the password)'),
    (r'👶 家长模式已开启：只显示 \[([^\]]+)\]，约 (\d+) 分钟后自动锁屏',
     r'👶 Parent mode ON: showing only [\1], auto-lock in ~\2 min'),
    (r'🔓 未设置密码，已直接退出家长模式',
     r'🔓 No password set — left parent mode directly'),
    (r'🔓 密码正确，已退出家长模式', r'🔓 Password OK — left parent mode'),
    (r'⚠ 退出家长模式被拒绝：密码错误',
     r'⚠ Parent-mode exit refused: wrong password'),
    (r'ℹ️ 密保验证通过但用户取消了密码重设，密码保持不变',
     r'ℹ️ Recovery answers OK, but you cancelled the reset — '
     r'password unchanged'),
    (r'🔓 密保验证成功且已设新密码，已免密退出家长模式',
     r'🔓 Recovery OK and a new password was set — left parent mode'),
    (r'🔓 密保验证通过：锁屏密码已清除并解锁',
     r'🔓 Recovery OK: lock password cleared and unlocked'),
    ('👨\u200d👩\u200d👧 家长模式已退出，桌面已恢复',
     '👨\u200d👩\u200d👧 Parent mode exited, desktop restored'),
    (r'📂 已恢复家长模式前分类 \[([^\]]+)\]',
     r'📂 Category [\1] from before parent mode restored'),
    (r'📂 已恢复家长模式前的\[全部\]桌面',
     r'📂 Pre-parent-mode [All] desktop restored'),
    (r'📂 已自动创建分类 \[([^\]]+)\]，请在主界面为孩子添加图标',
     r'📂 Category [\1] created automatically — add your child'
     r's icons on the main page'),
    (r'⚠ \[([^\]]+)\] 分类还没有图标，请在主界面先添加',
     r'⚠ Category [\1] has no icons yet — add some on the main page'),
    (r'⚠ (\d+) 个系统图标无法隐藏，保持可见',
     r'⚠ \1 system icons could not be hidden and stay visible'),
    (r'🖥 系统图标常驻开，已显示 (\d+) 个系统图标',
     r'🖥 System icons pinned ON — \1 system icons shown'),
    (r'🖥 已隐藏 (\d+) 个系统图标（\[([^\]]+)\]=只显示自己\+隐系统）',
     r'🖥 Hid \1 system icons ([\2] = self-only + hide system)'),
    (r'⏰ 学习时间到，正在恢复桌面并锁屏…',
     r'⏰ Study time is up — restoring the desktop and locking…'),
    (r'✗ 快速定位打开失败：', r'✗ Quick find failed to open: '),

    # ============ 规则页 / 高亮框 ============
    (r'🚫 已清除所有高亮框（"框住分类和常驻"已一并取消，勾回即恢复全框）',
     r'🚫 All highlight frames cleared ("frame categories + pinned" '
     r'unticked too; tick it back to reframe everything)'),
    (r'🔲 "框住分类和常驻"已自动取消（与只框分类互斥）',
     r'🔲 "Frame categories + pinned" unticked (mutually exclusive '
     r'with "categories only")'),
    (r'🎯 "只框分类"已自动取消（与框住分类和常驻互斥）',
     r'🎯 "Categories only" unticked (mutually exclusive with '
     r'"frame categories + pinned")'),
    (r'🔄 已重新生成方框（30 秒内保持显示）',
     r'🔄 Frames regenerated (stay visible for 30s)'),
    (r'⚠ 当前没有可绘制的内容：请勾选"🔲 框住分类和常驻"，'
     r'或先切分类 / 点"🖍️ 高亮选中分类"',
     r'⚠ Nothing to draw: tick "🔲 Frame categories + pinned", '
     r'or switch category / click "🖍️ Highlight selected category"'),
    (r'🎨 已切换主题 \[([^\]]+)\]', r'🎨 Theme switched to [\1]'),
    (r'🎨 主题已切换为 \[([^\]]+)\]', r'🎨 Theme switched to [\1]'),
    (r'🖍️ 已高亮 \[([^\]]+)\] 的 (\d+) 个图标',
     r'🖍️ Highlighted \2 icons of [\1]'),
    # ============ ★R294（批50，挂账 ★R290 那条 P3 顺手修）：规则页四味下拉 ============
    #   画法/并团/配色/方式 这四行是"方式=分区"下最常用的四档，English 档以前整句挂
    #   中文。档名（Envelope / Touching / Single color…）走 `_hl_t` 已经是英文，
    #   **没翻的是句子**：说明、尾巴 `只有"方式=分区"用得到`、以及 `高亮方式:` 这个前缀。
    #   顺序照本文件口径：先长句（十档说明、两条配色整句）、再带坑位的整句、最后前缀。
    #   全角标点（，：（）；。、）不是正则元字符照抄；半角括号没有，方括号没有。
    # ---- 画法十档说明（PAINT_HINTS 逐档；尾巴的分号留在这里，整句规则只补句子）----
    (r'一圈线框住整团，粗细方圆全跟 形状/线宽/样式 走；',
     r'One ring around the whole group; shape, width and style all apply;'),
    (r'整团套一圈磨砂色环，中间透出桌面；',
     r'A frosted colour ring around the group, the desktop shows through;'),
    (r'每行图标底下来一条色带，团里几行就几条；',
     r'One colour band under each icon row, as many bands as rows;'),
    (r'每个图标角上一枚点，团右上角再贴一枚名牌；',
     r'A dot on each icon corner, plus a name tag at the group top-right;'),
    (r'只在团四角各钉一枚实心括号，中间全透出桌面；',
     r'Solid corner brackets at the four group corners only, middle fully clear;'),
    (r'两圈同色线、中间留一条透明缝，比单线更钉；',
     r'Two same-colour rings with a transparent gap between, firmer than one;'),
    (r'每行图标底下来一条 45° 斜纹带，比荧光笔压得住；',
     r'A 45-degree hatch band under each icon row, steadier than the marker;'),
    (r'沿团边向内三圈渐淡的光晕，中心透出桌面；',
     r'A glow fading three rings inward from the group edge, centre stays clear;'),
    (r'还是两圈夹一条透明缝，但缝更窄、线更细一档；',
     r'Still two rings and a gap, but a narrower gap and thinner lines;'),
    (r'还是三圈渐淡光晕，但带更窄、颜色更淡一档；',
     r'Still a three-ring glow, but a narrower band and a lighter colour;'),
    # ---- 两条分区配色整句（档名是源码里的中文字面，必须连档名一起翻）----
    (r'🎨 分区配色 = 统一色（每个分区都用上面"颜色/模式"选的那一个色；'
     r'只有"方式=分区"用得到，即时生效）',
     r'🎨 Zone colour = Single color (every zone uses the one colour picked '
     r'above under "colour/mode"; only used by "mode=Zone", applies instantly)'),
    (r'🎨 分区配色 = 跟随分类色（一团一色，各分区用自己分类的颜色；'
     r'只有"方式=分区"用得到，即时生效）',
     r'🎨 Zone colour = By category (one colour per group, each zone keeps '
     r'its own category colour; only used by "mode=Zone", applies instantly)'),
    # ---- 并团：档名 + 两个"空几格"的数字都要留住（\2 \3 回填，不能吞）----
    (r'🧩 并团 = (.+?)（中间空 (\d+) 格以内还算一伙，空 (\d+) 格就分成两伙。'
     r'只有"方式=分区"用得到，即时生效）',
     r'🧩 Zone merge = \1 (icons up to \2 apart still count as one group, '
     r'\3 apart splits them into two; only used by "mode=Zone", applies instantly)'),
    # ---- 画法：档名 \1 + 说明 \2（说明已被上面十档规则变英文，这里只补句子）----
    (r'🖌 分区画法 = (.+?)（(.+?)只有"方式=分区"用得到，即时生效）',
     r'🖌 Zone paint = \1 (\2 only used by "mode=Zone", applies instantly)'),
    # ---- 显示方式前缀（值走 _hl_t 已是英文，只剩这句前缀）----
    (r'🖍️ 高亮方式: ', r'🖍️ Highlight mode: '),
    (r'🌗 亮度作用: ', r'🌗 Brightness set to: '),
    (r'🌗 软件蒙版作用: ', r'🌗 Software overlay level: '),
    (r'⏹ 已恢复全部图标显示（规则筛选已撤销）',
     r'⏹ All icons shown again (rule filter revoked)'),
    (r'⏹ 已恢复全部图标显示（unhide_all 兜底）',
     r'⏹ All icons shown again (unhide_all fallback)'),
    # ★R230（第 13 批顺手修）：分类读失败时拒绝导出的中止日志——不加这条，
    # English 档点"导出全部数据"被挡下时，日志行整句挂中文（用户看不到"为什么没导"）
    # ★R230b（S6 验收 P3）：**按"是哪个导出"各补一条完整规则**。原先只有通配
    #   `(.+?)` 那一条，它把中文 what 原样带进英文行（"…failed to load - 全部数据
    #   export aborted"）。translate 按序 sub，精确规则必须排在通配之前；通配那条
    #   留作将来第三个导出入口的兜底（宁可留中文尾巴，也不能整句不译）。
    (r'⏹ 分类加载失败，全部数据导出已中止（避免写出空备份）',
     r'⏹ Categories failed to load - all-data export aborted '
     r'(an empty backup would be written)'),
    (r'⏹ 分类加载失败，分类导出已中止（避免写出空备份）',
     r'⏹ Categories failed to load - category export aborted '
     r'(an empty backup would be written)'),
    (r'⏹ 分类加载失败，(.+?)导出已中止（避免写出空备份）',
     r'⏹ Categories failed to load - \1 export aborted '
     r'(an empty backup would be written)'),

    # ============ ★R231（用户 05 批 item2）损坏文件自救闸 ============
    # 四条都是**整句**（不带中文插值）：留底文件名是 ASCII，追加在句尾不影响命中。
    (r'🛠 损坏的分类文件已另存留底，本会话写盘已解禁',
     r'🛠 Corrupted category file set aside; disk writes re-enabled this '
     r'session'),
    (r'🛠 损坏的文件夹组配置已另存留底，本会话写盘已解禁',
     r'🛠 Corrupted folder-group config set aside; disk writes re-enabled '
     r'this session'),
    (r'⏹ 已取消：损坏文件未处理，本会话仍禁止写盘',
     r'⏹ Cancelled - the corrupted file was left untouched, disk writes '
     r'stay disabled this session'),
    (r'⚠ 损坏文件留底失败（被占用/只读），本会话仍禁止写盘',
     r'⚠ Could not set the corrupted file aside (locked/read-only); disk '
     r'writes stay disabled this session'),

    # ============ 统计 / 资源 / 主窗口 ============
    (r'📤 已导出使用统计 → ', r'📤 Usage stats exported → '),
    (r'📥 已导入使用统计（(\d+) 条）', r'📥 Usage stats imported (\1 entries)'),
    (r'🧹 使用统计已清零', r'🧹 Usage stats reset to zero'),
    (r'🧹 已清理 (\d+) 个过期日志文件（>(\d+) 天，'
     r'含 data/ 与 logs/ 两个目录）',
     r'🧹 Cleaned \1 expired log files (older than \2 days, '
     r'in data/ and logs/)'),
    (r'🔊 全量日志已开启：DEBUG 明细落盘 app.log（复现问题后'
     r'请关闭并回传 app\.log）',
     r'🔊 Verbose logging ON: DEBUG detail goes to app.log — turn it off '
     r'and send app.log back after reproducing'),
    (r'🔊 全量日志已关闭', r'🔊 Verbose logging OFF'),
    (r'📂 已加入组 \[([^\]]+)\]: ', r'📂 Added to group [\1]: '),
    (r'📂 该文件夹已在组 \[([^\]]+)\] 中', r'📂 Folder already in group [\1]'),
    (r'⚠ 当前没有组：请先"存为组"', r'⚠ No group yet — use "Save as group"'),
    (r'🗑️ 已从组 \[([^\]]+)\] 删除 (\d+) 个文件夹记录',
     r'🗑️ Removed \2 folder entries from group [\1]'),
    # ★R262 批14（「添加到当前组」/ 悬浮条槽位导入）：来源标签是中文变量，
    # 按 R230b 口径给**每种取值各写一条完整句精确规则**（不把中文 % 进英文模板，
    # 否则 English 档必带中文尾巴），排在下面的尾片段规则之前。
    (r'📂 浏览选择 → 组 \[([^\]]+)\]：新增 (\d+) 个',
     r'📂 Browse picker → group [\1]: \2 added'),
    (r'📂 当前打开的窗口 → 组 \[([^\]]+)\]：新增 (\d+) 个',
     r'📂 Open windows → group [\1]: \2 added'),
    (r'📂 粘贴地址 → 组 \[([^\]]+)\]：新增 (\d+) 个',
     r'📂 Pasted paths → group [\1]: \2 added'),
    (r'，已在组内 (\d+) 个', r'; \1 already in the group'),
    (r'，无效 (\d+) 个（不存在或不是文件夹）',
     r'; \1 invalid (missing or not a folder)'),
    (r'⚠ 当前没有组：先"➕ 新建组"或"💾 存为组"',
     r'⚠ No group yet — use "➕ New Group" or "💾 Save as Group"'),
    (r'⚠ 粘贴框里没有可识别的路径',
     r'⚠ Nothing recognizable in the paste box'),
    (r'📂 当前没有打开的资源管理器窗口',
     r'📂 No Explorer window is open right now'),
    (r'📥 已导入悬浮条槽位 (\d+) 个（条上已即时刷新）',
     r'📥 Imported \1 dock slots (bar redrawn live)'),
    (r'📥 已导入悬浮条槽位 (\d+) 个', r'📥 Imported \1 dock slots'),
    (r'⚠ 尚未选中文件夹组', r'⚠ No folder group selected'),
    (r'🔗 联动: 已同时打开 \[([^\]]+)\] 文件夹组',
     r'🔗 Linked: also opened the [\1] folder group'),
    (r'🔗 组 \[([^\]]+)\] 点分类自动打开: 开',
     r'🔗 Group [\1] auto-opens when its category is switched: on'),
    (r'🔗 组 \[([^\]]+)\] 点分类自动打开: 关',
     r'🔗 Group [\1] auto-opens when its category is switched: off'),
    (r'🔗 分类联动文件夹组: 开（切分类时同名文件夹组一并打开）',
     r'🔗 Category→folder-group linking: on (switching a category also '
     r'opens the group of the same name)'),
    (r'🔗 分类联动文件夹组: 关', r'🔗 Category→folder-group linking: off'),
    (r'📂 当前文件夹组: \[([^\]]+)\]（悬浮条一键目录即打开）',
     r'📂 Current folder group: [\1] (one click on the dock opens it)'),
    (r'📂 已直接打开 \[([^\]]+)\]: (\d+)/(\d+) 个文件夹',
     r'📂 Opened [\1] directly: \2/\3 folders'),
    # src_desc 是"组名/分类名"这类用户数据，原样保留，只翻界面骨架。
    (r'📂 已打开 (.+?): (\d+)/(\d+) 个文件夹',
     r'📂 Opened \1: \2/\3 folders'),
    (r'✅ 已打开 (\d+)/(\d+) 个文件夹，可以正常操作了',
     r'✅ Opened \1/\2 folders, ready to work'),
    (r'📂 \[([^\]]+)\] 没有文件夹组，当前组也没有文件夹',
     r'📂 [\1] has no folder group and the current group is empty'),
    (r'📂 已打开 (\d+)/(\d+) 个文件夹', r'📂 Opened \1/\2 folders'),
    (r'⚠ 组名不能为空', r'⚠ Group name cannot be empty'),
    (r'💾 已保存文件夹组 \[([^\]]+)\]（(\d+) 个文件夹）',
     r'💾 Folder group [\1] saved (\2 folders)'),
    (r'🗑️ 已删除文件夹组 \[([^\]]+)\]', r'🗑️ Folder group [\1] deleted'),
    (r'📂 当前组没有文件夹：先"存为组"或添加文件夹',
     r'📂 Current group has no folders — "Save as group" or add some'),
    (r'✓ 已恢复 (\d+) 个图标', r'✓ Restored \1 icons'),
    (r'✗ 恢复收尾出错: ', r'✗ Restore cleanup failed: '),
    (r'🫥 桌面与目标布局一致，跳过恢复（无卡顿）',
     r'🫥 Desktop already matches the target layout — skipped (no flicker)'),
    # ★R247（#109/#131 同名图标）：存档/补位/丢弃三条新日志。三条都是整句，
    #   长短互不吞（"补位"与"补位：桌面上找不到"分属不同句，各自命中）。
    (r'🧹 同名图标补位：按索引写了 (\d+) 个、(\d+) 个本来就在位'
     r'（不靠名字，所以两个同名能各回各的格子）',
     r'🧹 Same-name icons re-placed: \1 written by index, \2 already in place '
     r'(matched by index, so each copy gets its own cell)'),
    (r'🧹 同名补位：桌面上找不到「(.+?)」这个图标，存档里 (\d+) 条附加坐标本次不写',
     r'🧹 Same-name skip: [\1] is not on the desktop, \2 extra cell(s) not written'),
    (r'📦 这份布局里有 (\d+) 组同名图标，多出来的坐标一并记进 dup_cells（(.+?)）',
     r'📦 \1 same-name icon group(s) stored in dup_cells (\2)'),
    (r'🫥 主窗口已隐藏，悬浮窗继续运行（横条"窗"可呼出，"✕"退出）',
     r'🫥 Main window hidden, dock keeps running ("Main" shows it, '
     r'"✕" quits)'),
    (r'⚠ 设置写盘失败（磁盘/权限？），本次改动重启后可能丢失',
     r'⚠ Settings write failed (disk/permissions?) — these changes '
     r'may be lost after restart'),
    (r'⚠ 下载记录卡加载失败，资源页该块已跳过：',
     r'⚠ Download card failed to load; that block was skipped: '),
    (r'⚠ 系统相关页加载失败，已降级为提示页：',
     r'⚠ System page failed to load; degraded to a notice page: '),
    (r'🔄 即将重启以应用新', r'🔄 Restarting to apply the new '),
    # ============ ★R232（用户 06 批 item1）「打字不上」诊断行 ============
    # 规则按**形状**写（不写死取值）：这些行的值来自 f-string 插值，门禁
    # 用 `{...}`→X 的探针逐条过词典，写死取值的规则会匹配不上真句。
    (r'📛 诊断记录已达每小时上限，本小时同类记录不再写入',
     r'📛 Diag hourly cap reached; similar records are skipped this hour'),
    (r'⌨ 诊断采样未启用（设置里可开：typing_diag_log）',
     r'⌨ Diag sampler disabled (turn it on in Settings: typing_diag_log)'),
    (r'⌨ 诊断采样器自身出错（已跳过本轮）: ',
     r'⌨ Diag sampler error (this round skipped): '),
    (r'⌨ 诊断采样已启动', r'⌨ Diag sampler started'),
    (r'⌨ 诊断｜修饰键持续按下', r'⌨ diag｜modifier key held'),
    (r'🔎 诊断｜(\S+) 后前台切换：', r'🔎 diag｜after \1 the foreground changed:'),
    (r'前台归属=别的应用', r'fg-owner=another app'),
    (r'前台归属=本程序', r'fg-owner=this app'),
    (r'｜前台=', r'｜fg='),
    (r'｜本程序=', r'｜this-app='),
    (r' 实例=', r' x='),
    (r'｜阈值=', r'｜threshold='),
    (r' 事件窗=', r' event-window='),
    # ★R232 S5：同批诊断行**不止 typing_diag 一个文件**（keystate / quick_find /
    #   main_window 各有一条，字段名还会和文案拼进同一行）。门禁 B8b 逐文件扫
    #   字面量并按 English 档断言"不许留 CJK"，漏一条就是英文日志里混半句中文。
    #   注意：模式里的 `[...]` 在正则里是字符集，必须转义成 `\[...\]`，
    #   否则规则照样编译通过、lint() 也不报，只是永远匹配不上真句（静默假绿）。
    (r'\[keystate\] 借用 Alt 置前台（收尾会再巡检一次）',
     r'[keystate] borrowing Alt to raise the window (re-checked on exit)'),
    (r'\[keystate\] bounce_alt 收尾仍有按键未回位: ',
     r'[keystate] bounce_alt ended with keys still down: '),
    (r'\[keystate\] 守护线程起不来、补抬 Alt 仍有余留: ',
     r'[keystate] watchdog thread failed to start; Alt still stuck: '),
    (r'\[keystate\] 启动巡检复位卡住的修饰键: ',
     r'[keystate] startup sweep released stuck modifiers: '),
    (r'｜仍按下=', r'｜still-down='),
    (r'｜注入前按下态=', r'｜pre-injection mods='),
    (r'\[quick_find\] 跳到桌面图标过程出错（已按失败处理）: ',
     r'[quick_find] jump-to-desktop-icon failed (treated as a failure): '),
    (r'\[启动\] 打字诊断采样未启用（不影响功能）: ',
     r'[start] typing-diag sampler not enabled (no feature impact): '),
    # ★R333-d：诊断包任务快照后台预热的兜底行（预热没起来⇒点击时当场补一次）。
    # 只写插值前的前缀（★R242/#140 同口径），异常原文留在坑位里当排障证据。
    (r'\[启动\] 诊断包任务快照没预热上（点击时当场补一次）: ',
     r'[start] tasks-snapshot prewarm did not start (a live probe runs '
     r'when you click): '),
    # ★R242（#140 桌面排序兜底）：这条功能会往 GUI 日志里写"检查/成功/没修好"
    #   三类短句，English 档不补规则就会整行留中文（用户会连 English 模式一起
    #   冒烟）。规则按"整句 + 插值前的前缀"两种写法：带 %s/%d 的行只取插值前的
    #   那截，避免写死取值匹配不上真句（★R231 同口径）。
    (r'🧹 桌面视图检查\(', r'🧹 desk view check('),
    # 建议句在源码里已按语言取文案（core/sort_clear._t）；这条是"会话中途换语言
    # 时旧中文行也能翻"的兜底，缺了不影响正常路径。
    (r'桌面开着「自动排列图标」，这个开关会让布局摆不住。'
     r'请在桌面空白处右键 → 查看 → 取消勾选「自动排列图标」。',
     r'"Auto arrange icons" is on and it keeps the layout from holding. '
     r'Right-click an empty spot on the desktop -> View -> untick '
     r'"Auto arrange icons."'),
    (r'🧹 暂时读不到桌面视图状态（主窗口还没就绪）',
     r'🧹 desktop view state unavailable for now (main window not ready)'),
    (r'🧹 检查失败：', r'🧹 check failed: '),
    (r'🧹 修复失败：', r'🧹 fix failed: '),
    # ★R318 K19：先长后短——带尾巴那句必须排在下面那条短句**之前**，否则短句
    #   先把前半截翻掉，长规则永无匹配（English 日志页留中文尾巴）。
    (r'🧹 已清掉桌面的活动排序（没占用鼠标）',
     r'🧹 active desktop sort cleared (no mouse was used)'),
    (r'🧹 已清掉桌面的活动排序', r'🧹 active desktop sort cleared'),
    # ★R245 A7：清完排序改"整桌按基准布局一次性摆回"，三条新日志各配一条；
    #   同时删掉旧的「⚙️ 桌面排序修复已开启/已关闭」两条——那枚复选已按用户
    #   37 批口径删除，规则留着就是死规则（词典只留活行）。
    # ★R247：三条日志的尾部随"到底拖没拖过图标"分叉（免鼠标那一路什么都没挪，
    #   再说"改把拖过的图标放回原位"就是假话），且成功行尾部多了基准出处
    #   （文件名·来源）。**长句排在短句前面**（改写是逐条 re.sub 链）。
    (r'🧹 没有可用的基准布局（([^）]*)）（这一路本来没挪动任何图标，桌面保持现状）',
     r'🧹 no baseline layout (\1); this path never moved any icon, '
     r'desktop left as-is'),
    (r'🧹 批量摆位没做成（([^）]*)）（这一路本来没挪动任何图标，桌面保持现状）',
     r'🧹 batch relayout failed (\1); this path never moved any icon, '
     r'desktop left as-is'),
    (r'🧹 已一次性按基准布局摆回 (\d+)/(\d+) 个图标（基准=([^）]*)）',
     r'🧹 desktop laid back from the saved baseline: \1/\2 icons (baseline \3)'),
    (r'🧹 已一次性按基准布局摆回 (\d+)/(\d+) 个图标',
     r'🧹 desktop laid back from the saved baseline: \1/\2 icons'),
    (r'🧹 没有可用的基准布局（([^）]*)），改把拖过的图标放回原位',
     r'🧹 no baseline layout (\1); putting the dragged icons back'),
    (r'🧹 批量摆位没做成（([^）]*)），改把拖过的图标放回原位',
     r'🧹 batch relayout failed (\1); putting the dragged icons back'),
    (r'没修好：拖了 ', r'not fixed: tried '),
    (r' 次，桌面视图里的活动排序还在', r' drags, the view sort is still active'),
    (r'（原因：', r' (reason: '),
    # 异常原文里常带系统回传的中文（"拒绝访问"这类），不补就整条留中文
    (r'拒绝访问', r'access denied'),
    (r'没修好：拿不到桌面图标或空格，一次都没拖',
     r'not fixed: no desktop icon or free cell found, nothing dragged'),
    (r'没修好：过程出错（', r'not fixed: error ('),
    (r'），已把拖过的图标放回原格', r'), dragged icons put back in their cells'),
    # ★R242 S6 疑点收口：计划任务页"建后回读核对"是 §二 D2 新增的成功态日志
    #   （gui/sys_panel.py 里走 gui._log 上屏），当时漏了词典 → English 档整行
    #   留中文。顺带把同族的"已设工作目录"（★R241 加）一起补上。
    #   注意：`：` 是全角，_POLISH 只在命中后才改写标点，所以模式必须照抄全角。
    (r'↳ 回读核对通过：', r'↳ read-back OK: '),
    (r' 要运行的动作已确认', r' — its action is confirmed'),
    (r'↳ 已设工作目录: ', r'↳ working directory set: '),
    # ============ ★R246 B5-①/③：桌面视图状态归因 + 污染存档判读 ============
    #   四条 = core/sort_clear.drift_text 的四句（写后回读不符时到底谁在动）；
    #   两条 = core/icon_manager.restore_layout 读存档"当初是干净抄的还是排完序
    #   抄的"的提示；一条 = 偏差总数那行。整句规则，全角标点照抄（判据 B24-B26）。
    (r'位置被系统拉回网格：桌面开着「自动排列图标」，写进去的位置会被它改掉——不是布局存档坏了',
     r'Positions were pulled back to the grid: the desktop has "Auto arrange icons" '
     r'on, so anything written gets rewritten by it — the layout archive is fine'),
    (r'位置被排序方式接管：桌面当前按「([^」]*)」排序，刷新一下就会被打回——不是布局存档坏了',
     r'Positions are owned by the sort order: the desktop is currently sorted by '
     r'"\1", a refresh will undo them — the layout archive is fine'),
    (r'位置写入被忽略：自动排列没开、也没有活动排序，是 Explorer 这次没接住写入（可再套一次布局）',
     r'Position writes were ignored: no auto-arrange and no active sort — Explorer '
     r'did not take this write (apply the layout again)'),
    (r'读不到桌面视图状态，偏差原因未知（不当成"没问题"）',
     r'Desktop view state unavailable, cause of the drift unknown '
     r'(do NOT read this as "all good")'),
    (r'这份布局记录时桌面正按「([^」]*)」排序：存进去的就是排完序的位置，套回后一刷新会被打回（不是本次恢复失败）',
     r'This layout was saved while the desktop was sorted by "\1": it holds sorted '
     r'positions and a refresh will undo them (not a failure of this restore)'),
    (r'这份布局记录时桌面开着「自动排列图标」：存进去的是系统网格位，套回后会被重新打包（不是本次恢复失败）',
     r'This layout was saved with "Auto arrange icons" on: it holds system grid '
     r'positions and will be repacked after restore (not a failure of this restore)'),
    (r'套完布局有 (\d+) 个图标没落到存档位置',
     r'Layout applied but \1 icons did not land on their archived positions'),
    # ============ ★R246 #198：免鼠标清排序（core/folder_view + sort_clear）============
    #   三条 = 成功/回退/结论。带 %s 的行只取插值前的那截（★R231 口径）；
    #   ★R318 K19：`🧹 已清掉桌面的活动排序（没占用鼠标）` 那条**长句**已挪到
    #   短句 `🧹 已清掉桌面的活动排序` 之前（本文件自己的口径是先长后短）——
    #   旧写法短句在前，短规则先把前半截翻掉，后半句永远匹配不上＝死规则。
    (r'免鼠标修好了：官方接口一次调用就把活动排序清掉，耗时 ',
     r'mouse-free fix: the desktop-view API cleared the active sort in '),
    (r' 秒，没占用鼠标', r' s without touching the mouse'),
    (r'免鼠标那条没走通（', r'mouse-free route did not work ('),
    (r'），改走合成拖动', r'), falling back to the synthetic drag'),
    # ============ ★R247-2（用户 39 批 item2）：计划任务「⚡ 电源条件」 ============
    #   ★R256（#220）改**从句级**规则：原先"抬头+每种勾法"整句展开要 2^n 条，
    #   加进"错过补跑/允许唤醒"后是 16 种组合，整句表永远追不上（历轮那两条
    #   "停止该任务"就是 ★R250 改措辞后没跟上的死规则——写的是旧串，真句再也
    #   匹配不上）。现在：抬头、基础态、每条从句各翻各的，组合天然全覆盖。
    #   基础态两条带「电源条件已设：」前缀锚定，免得波及其它出现同词的行。
    #   判据：test_r256_miss_wake L 段（16 组合真句回放 CJK-free，★R232 口径）。
    (r'⚡ 电源条件已设：', r'⚡ power conditions set: '),
    # 第二条＝app.log 侧那行（`电源条件已设(名字): …`，抬头前没有 ⚡）。
    (r'电源条件已设：', r'power conditions set: '),
    (r'只在交流电（电源适配器）下运行',
     r'runs only on AC power (plugged into the adapter)'),
    (r'插不插电源都运行', r'runs on battery too'),
    (r'；改用电池供电时停止正在运行的任务',
     r'; stops the running task when it switches to battery'),
    (r'；错过计划后尽快补跑', r'; missed runs are caught up ASAP'),
    (r'；允许唤醒计算机运行', r'; the computer may wake up for it'),
    (r'⚠ 电源条件没设上（任务照常存在）：',
     r'⚠ power conditions not applied (the task itself is kept): '),
    (r'⚡ 电源条件没设上（异常）：',
     r'⚡ power conditions not applied (the call failed): '),
    # 自启那一路的整行（★R241 起就在往界面日志里写，当时漏补词典；★R247-2 的
    #   电源条件尾串正好拼在它后面，不补就是整行中文夹一句英文）。带插值的行
    #   只取插值前的那截（★R231 口径）。
    (r'⚙️ 随登录自启动已开启：', r'⚙️ logon auto-start turned on: '),
    (r'⚙️ 随登录自启动已关闭：', r'⚙️ logon auto-start turned off: '),
    (r'⚙️ 随登录自启设置失败，已回滚: ',
     r'⚙️ logon auto-start failed, rolled back: '),
    (r'🚀 已设为随登录自启（登录后 ',
     r'🚀 logon auto-start created (delay after login: '),
    (r' 起，任务名 ', r', task name '),
    (r'，可在 📅 计划任务页删除）', r'; delete it on the 📅 Scheduled tasks page)'),
    (r'🚀 已取消随登录自启', r'🚀 logon auto-start removed'),
    # 自启失败时会把"给用户看的话"整串拼进日志行（★R241 起就这样），这几句
    #   本身没有双语出口，只能在这儿补。`无输出` 是电源条件回读空返回的兜底词。
    (r'自启任务回读核对不通过（已撤回），请改用手动启动或反馈这条日志',
     r'the logon task failed its read-back check (it was withdrawn); start the '
     r'app manually or report this line'),
    (r'创建自启任务失败', r'creating the logon task failed'),
    (r'删除自启任务失败', r'removing the logon task failed'),
    (r'电源条件设置异常：', r'power-condition call failed: '),
    (r'无输出', r'no output'),
    # ★R248（用户 40 批 item5）：日志页"📦 打包日志到桌面"那两行界面日志。
    #   源码里仍写中文（方案 A：日志留中文、上屏前改写），这里补 English。
    (r'📦 日志已打包到桌面：(.+)（(\d+) 个文件）',
     r'📦 Logs packaged to the desktop: \1 (\2 files)'),
    (r'📦 校验码：(.+)', r'📦 Checksum file: \1'),
    #   ★R248 S6 自查补：日志被别的程序占用（例：VMware 抓着自己的 .log 不放）
    #   时打包会跳过它并**当面说数量**，这行也得有 English 出口。
    (r'📦 有 (\d+) 个日志没能打包（可能被其他程序占用或已删除）：(.*)',
     r'📦 \1 log file(s) were left out (locked by another program or already '
     r'deleted): \2'),
    # ★R255（46 批 item④-2）：桌面快捷方式两条上屏日志（完整句精确规则）。
    (r'🖥️ 已在桌面创建软件快捷方式「[^」]+」',
     r'🖥️ Created a desktop shortcut for DeskBits'),
    (r'⚠ 桌面快捷方式创建失败（不影响使用，可从原位置打开）',
     r'⚠ Could not create the desktop shortcut (harmless — open the app '
     r'from its own folder)'),
    # ★R286-A1：上一会话停在"问桌面没返回"那一发时的上屏句（原始行只在 app.log）。
    (r'⚠ 上次运行结束在一条未返回的桌面调用上（进程被系统掐断，'
     r'通常是 Explorer 侧的问题）——详情看 data/win32_trail\.log',
     r'⚠ The previous run ended inside a desktop query that never returned '
     r'(the process was killed — usually an Explorer-side issue); see '
     r'data/win32_trail.log for detail'),
    # ============ ★R337（批90）：排行组"名单比存档新"的补位链 ============
    #   这一族句子来自 `core/dock.py::_anchor_rank_group_members` 与
    #   `core/icon_manager.py::plan_layout_member_fill`。坑位口径与历轮一致：
    #   数字、缺席名单、组名都是**图标名/文件名级别的证据**，照旧回显；
    #   每条都写到句尾，汉字不带进英文行。带尾巴的整句排在裸短语之前。
    (r'\[dock\] 排行组 \[([^\]]*)\] 补位 (\d+) 枚（这份手动布局里没有它们的位置）'
     r'摆好 (\d+) 枚 改格 (\d+) 缺席 (.*?)｜名单 (\d+) 枚比存档 (\d+) 条差 (.*?)$',
     r'[dock] rank group [\1]: gave \2 member(s) this manual layout never had a '
     r'cell for a deterministic home (\3 placed, \4 re-snapped, missing \5) - '
     r'roster \6 names vs \7 archived entries, gap \8'),
    (r'\[dock\] 排行组 \[([^\]]*)\] 补位没跑成（布局已套好，守护仍按清洗后的存档武装）: (.*)',
     r'[dock] rank group [\1]: the fill-in step did not run (the layout is already '
     r'applied; the guard still arms from the cleaned archive): \2'),
    (r'\[dock\] 排行组 \[([^\]]*)\] 没有清洗后的基准可补位（照旧按存档武装）',
     r'[dock] rank group [\1]: no cleaned baseline to fill in - arming from the '
     r'archive as before'),
    (r'\[dock\] 排行组 \[([^\]]*)\] 名单读不到 ⇒ 本轮不补位',
     r'[dock] rank group [\1]: roster unreadable - no fill-in this round'),
    (r'\[dock\] 读排行组名单失败 \[([^\]]*)\]: (.*)',
     r'[dock] rank group [\1]: reading the roster failed: \2'),
    (r'\[dock\] 排行组 \[([^\]]*)\] 旁观者占位读不到（只按存档格子算补位）: (.*)',
     r'[dock] rank group [\1]: bystander cells unreadable (fill-in computed from '
     r'the archive alone): \2'),
    (r'\[anchor\] 用户文件夹显示名读不到（只按静态五枚算）: (.*)',
     r'[anchor] user-folder display names unreadable (the static five are used '
     r'alone): \1'),
    (r'\[anchor\] 格子换算失败（本轮不补位，存档位置照旧）: (.*)',
     r'[anchor] cell math failed (no fill-in this round, archived positions stand): \1'),
    (r'\[anchor\] (\d+) 枚成员找不到空格补位（(.*?)）⇒ 保持它们现在的位置不动',
     r'[anchor] \1 member(s) found no free cell (\2) - they keep where they stand'),
    (r'\[anchor\] 补位=(\d+) 幽灵格继承=(\d+) (.*)',
     r'[anchor] filled=\1 ghost-cells reused=\2 \3'),
]

# 英文模式下顺手把全角标点换掉（只在命中过短语后执行，避免动到纯中文行）
_POLISH = [(u'，', u', '), (u'：', u': '), (u'（', u' ('), (u'）', u') '),
           (u'。', u'. '), (u'、', u', '), (u'——', u' — ')]

_COMPILED = [(re.compile(p), r) for p, r in _TABLE]
_CJK_RE = re.compile(r'[㐀-䶿一-鿿豈-﫿]')


def lint():
    """返回 [序号, ...] 里"模板写坏"的条目（组引用越界、非法转义等）。

    re.sub 在**没有匹配**时也会先解析替换模板，所以拿空串喂一遍就能把
    `bad escape \\u`、`invalid group reference` 这类错误全部提前炸出来——
    门禁测试用它把整张词典型钉住（★R229 首版就踩过 \\u200d 写在替换模板里，
    一条坏规则把整链 translate 打断，全项目日志一句都没翻）。"""
    bad = []
    for i, (rx, repl) in enumerate(_COMPILED):
        try:
            rx.sub(repl, '')
        except Exception:
            bad.append(i)
    return bad


def translate(msg):
    """English 模式下把日志里的界面文案改写成英文；其余情况原样返回。

    单条规则出错只跳过该条（宁可少翻一句，不能带走日志通路）。"""
    if not msg:
        return msg
    try:
        if not i18n.is_en():
            return msg
        s = str(msg)
        if not _CJK_RE.search(s):
            return msg          # 纯英文/数字行（含 i18n.tr 已翻过的）直接过
        hit = False
        for rx, repl in _COMPILED:
            try:
                s2 = rx.sub(repl, s)
            except Exception:
                continue
            if s2 != s:
                s, hit = s2, True
        if hit:
            for a, b in _POLISH:
                s = s.replace(a, b)
        return s
    except Exception:
        return msg


def coverage(text):
    """给门禁/盘点用：返回 text 里被多少条短语命中（>0 = 有英文可看）"""
    n = 0
    for rx, _ in _COMPILED:
        if rx.search(text):
            n += 1
    return n


def rule_count():
    return len(_COMPILED)
