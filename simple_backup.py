# -*- coding: utf-8 -*-
"""一键备份脚本：把项目当前必要内容打包为带时间戳的 ZIP。

用法：python simple_backup.py backup   （或直接 python simple_backup.py）
由 一键备份.bat / backup.bat 调用；2026-09-05 重建（原文件在 v0.3.52
重构时遗失导致一键备份失效）。

★R260（用户 52 批："现在的备份文件过大……临时文件不需要备份"）：
备份范围收敛为"源代码 + 测试脚本 + 配置 + 图片 + 用户状态数据 + 活文档 +
历史归档"，只排**跑一次就能重生成的东西和临时副本**。历史归档
（`run/`、`代码存档/`、`代码报告*/`）**照旧进包**——用户点名要带，
合计才 0.6MB 上下，不是体积元凶；元凶是 `run_tmp/`（当天 29.75MB 里占 24.29MB：
一个 19MB 冒烟 exe 副本 + 一棵 5MB 改前基线树）。
判据集中在 `skip_reason()`/`should_include()` 一处，便于门禁直接测。
"""
import os
import shutil
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
# 临时/产物/缓存类目录：任何深度出现都整棵跳过
EXCLUDE_DIRS = {'backup', 'logs', '__pycache__', '.git', '.venv',
                'dist_onefile', 'dist_onedir', 'dist_nuitka',
                'dist_nuitka_standalone', 'build', 'dist_待上传',
                'node_modules', 'run_tmp', 'tmp',
                'backup_20260831_081524_broken',
                'backup_cleanup_20260901_104836',
                '_quarantine_20260904', '_quarantine_20260905'}
# ★ R184：保留名不区分大小写排除（曾抓过根目录大写 NUL）
RESERVED_NAMES = {'nul', 'con', 'prn', 'aux'}
# ★ R260：'.log' 入扩展名黑名单——运行时日志（data/app.log、crash.log、
# sentinel.log…）是"日志不需要备份"的正主
EXCLUDE_EXT = {'.pyc', '.pyo', '.log'}
# ★ R204 ROOT-1（备份侧）：全权限令牌绝不进备份包（git 侧 R190 已闭合，
# 这是"一键备份 zip 被拷走"场景的最后防线）
EXCLUDE_FILES = {'token.txt'}
# ★R265（用户 20 批："提示词不进备份，我单独复制"）：点名不带进包的文件——
# 不是"可再生成物"，是他自己拿走保管的一份说明文档，理由单列一类，别混进
# "跑一次就能重生成"那句摘要里（写了就是撒谎）。
USER_OWNED_FILES = {'deskbits提示词.md'}
# ★ R208+（用户 10 批）：自动测试报告每次跑都落一个新 txt，备份
# "多了一堆文本"——前缀匹配排除（报告名=自动测试报告_日期_时间.txt）
EXCLUDE_PREFIXES = ('自动测试报告_', '排错报告_')
# ★ R260：门禁报告 `测试_ui/*.txt` 每跑一次就覆写一次，属可再生成物；
# 同目录的 `test_*.py` 是代码，必须进包（用户原话"你的测试脚本，代码肯定要备份的"）
GEN_REPORT_DIRS = {'测试_ui'}
# ★R280-F（用户 06 批 item6「可以排除在外」）：`run/**/*_pre_tree.zip` 是**修复前的
# 旁树快照**（一整份代码树的拷贝，实测 10~25 MB/把）。它存在的意义是"给门禁当红基线"，
# 用完当批就能从 git 无关的旁拷贝重造 ⇒ 属可再生成物；而 run/ 里其余证据（报告/
# 脚本/截图）是"这批改了什么"的凭据，照旧必须进包（用户 24 批的备份范围纪律）。
# 只认这个后缀，不认整个 run/——一排除就把凭据也丢了。
PRE_TREE_SUFFIX = '_pre_tree.zip'


def skip_reason(rel):
    """rel（相对 ROOT、用 '/' 分隔）为何不该进包；该进则返回 ''。"""
    parts = [p for p in rel.replace('\\', '/').split('/') if p not in ('', '.')]
    if not parts:
        return 'empty'
    dirs, fn = parts[:-1], parts[-1]
    if any(d in EXCLUDE_DIRS for d in dirs):
        return 'temp_dir'
    if fn.lower() in RESERVED_NAMES:
        return 'reserved'
    if fn in EXCLUDE_FILES:
        return 'secret'
    if fn in USER_OWNED_FILES:
        return 'user_owned'
    if fn.startswith(EXCLUDE_PREFIXES):
        return 'generated_report'
    if os.path.splitext(fn)[1].lower() in EXCLUDE_EXT or '.log.' in fn:
        return 'runtime_log'
    if parts[0] in GEN_REPORT_DIRS and fn.endswith('.txt'):
        return 'generated_report'
    if parts[0] == 'run' and fn.endswith(PRE_TREE_SUFFIX):
        return 'pre_tree'
    return ''


def should_include(rel):
    return skip_reason(rel) == ''



def sweep_reserved_files():
    r"""★ R184 自愈：删除树内保留名文件（NUL 等）。

    根因（20260920 用户实测备份只出 53 个文件）：Git Bash 的 `> nul`
    会落一个真 NUL 文件；os.path.relpath('D:/.../NUL') 被 Windows
    规范化成设备路径 \\.\NUL，判定跨挂载点抛 ValueError——且这行
    在 per-file try 之外，整个备份当场中断。这里开备前先扫一遍，
    走 \\?\ 前缀删掉（abspath 会把 NUL 变设备路径，必须用 normpath）。"""
    gone = []
    for dirpath, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for fn in files:
            # ★R207 NEW3-R4：桌面图标恢复的原子写残留（.dbk_tmp，进程被杀
            # 时滞留）一并清扫——超 1 天的必是孤儿（正常恢复瞬间完成）
            if fn.endswith('.dbk_tmp'):
                full = os.path.join(dirpath, fn)
                try:
                    if time.time() - os.path.getmtime(full) > 86400:
                        os.remove(full)
                        gone.append(full)
                except OSError:
                    pass
                continue
            if fn.lower() in RESERVED_NAMES:
                full = os.path.normpath(os.path.join(dirpath, fn))
                try:
                    os.remove('\\\\?\\' + full)
                    gone.append(full)
                except OSError as e:
                    print(f'! 保留名文件删除失败 {full}: {e}')
    for p in gone:
        print(f'已自愈删除保留名文件: {p}')
    return gone


# ★R262 批15（用户 15 批 四27："run_tmp 随着程序自动删吧，如果不需要可以删掉"）：
# run_tmp/ 是历轮取证的一次性现场（冒烟 exe 副本、改前基线整树、探针输出），
# ★R260 已把它挡在备份之外，但磁盘上仍占 24MB+。按**修改时间**自动剪：
# 超过 RUN_TMP_AGE_S 的顶层条目整条删除，还在用的（当天新落）留着不动。
# 只碰 run_tmp/ 这一个目录，别的一律不动。
RUN_TMP_AGE_S = 14 * 86400


def sweep_run_tmp(base=None, now=None):
    root_tmp = base or os.path.join(ROOT, 'run_tmp')
    if not os.path.isdir(root_tmp):
        return []
    cutoff = (now if now is not None else time.time()) - RUN_TMP_AGE_S
    gone, failed = [], []
    for name in sorted(os.listdir(root_tmp)):
        full = os.path.join(root_tmp, name)
        try:
            if os.path.getmtime(full) > cutoff:
                continue        # 还在用：当天/近两周的现场留着
            if os.path.isdir(full) and not os.path.islink(full):
                shutil.rmtree(full)
            else:
                os.remove(full)
            gone.append(name)
        except OSError as e:
            failed.append(name)
            print(f'! run_tmp/{name} 清理失败: {e}')
    if gone:
        print(f'run_tmp 自动清理（超过 {RUN_TMP_AGE_S // 86400} 天未改动）：'
              f'{len(gone)} 条，失败 {len(failed)} 条')
    return gone


def main():
    sweep_reserved_files()
    sweep_run_tmp()
    stamp = time.strftime('%Y%m%d_%H%M%S')
    # ★ R260：输出目录可重定向——门禁 test_r260 要在临时目录里真跑一次备份
    # 核名单，不能为此在项目 backup/ 里再落一个包
    out_dir = os.environ.get('DESKBITS_BACKUP_OUT') or os.path.join(ROOT, 'backup')
    os.makedirs(out_dir, exist_ok=True)
    # ★ R158：产物名跟新名（DeskBits）；dist_待上传（发行包）不进备份
    out = os.path.join(out_dir, f'DeskBits_backup_{stamp}.zip')

    count = 0
    skipped = {}
    pruned = {}
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for dirpath, dirnames, filenames in os.walk(ROOT):
            # ★R260：整棵剪掉的目录也记账——只数逐文件判据的话，`run_tmp/` 那
            # 783 个文件是目录级剪枝跳过的，摘要里一个都不报（读数偏小=撒谎）
            keep = [d for d in dirnames if d not in EXCLUDE_DIRS]
            for d in set(dirnames) - set(keep):
                pruned[d] = pruned.get(d, 0) + 1
            dirnames[:] = keep
            for fn in filenames:
                # ★ R184：保留名（不分大小写）与 .pyc 排除；rel/双写全程
                # 进 try——任何单文件异常只跳过不中断（此前 relpath 在
                # try 外，遇 NUL 直接把整个备份带走）
                full = os.path.join(dirpath, fn)
                try:
                    rel = os.path.relpath(full, ROOT).replace('\\', '/')
                except Exception as e:
                    print(f'跳过 {fn}: {e}')
                    continue
                reason = skip_reason(rel)
                if reason:
                    skipped[reason] = skipped.get(reason, 0) + 1
                    continue
                try:
                    z.write(full, rel)
                    count += 1
                except Exception as e:
                    print(f'跳过 {fn}: {e}')

    size = os.path.getsize(out)
    # ★R213（NEW5 挂账）：'✓' 不在 GBK 码页——GBK 控制台下 print 抛
    # UnicodeEncodeError，zip 已建成却以非 0 退出（backup.bat 误报失败）。
    # 结论文案只用 GBK 里有的字符。
    print(f'[OK] 备份完成: {count} 个文件, {size / 1024 / 1024:.2f} MB')
    print(f'  位置: {out}')
    if skipped:
        detail = '，'.join(f'{k}={v}' for k, v in sorted(skipped.items()))
        print(f'  已排除 {sum(skipped.values())} 个文件（日志/生成报告/缓存/红基线旁树＝跑一次能重生成，'
              f'user_owned＝用户点名自己保管）: {detail}')
    if pruned:
        # 只报"剪掉几处目录"，不谎称知道里面多少文件（没数过就不写数）
        print('  目录级剪枝（整棵不进包）: '
              + '，'.join(f'{k}×{v}' for k, v in sorted(pruned.items())))
    return 0


if __name__ == '__main__':
    sys.exit(main())
