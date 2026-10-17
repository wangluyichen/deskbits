# -*- coding: utf-8 -*-
r"""DeskBits Nuitka 打包脚本（★ R176 定案：默认打包路线，替代 PyInstaller 双包）。

用法：
  python 打包_Nuitka.py                # 默认＝双包（单文件 + 单目录一起出）
  python 打包_Nuitka.py onefile       # 只出单文件（解压到用户缓存目录）
  python 打包_Nuitka.py standalone    # 只出单目录（★R182 也是 onefile，
                                       #   但解压到 exe 旁 runtime 文件夹）
  python 打包_Nuitka.py both          # 同默认，两种一起出

产物（Gitee / GitHub Release 附件用；**产物目录不进代码仓库**，已入 .gitignore）：
  dist_待上传\deskbits_beta_<版本>\deskbits_beta_<版本>-single.zip + _SHA256.txt
  dist_待上传\deskbits_beta_<版本>\deskbits_beta_<版本>-dir.zip   + _SHA256.txt
  （★R325 用户点名"出一个全部都是英文文件名的…我要上传 github"：文件名一律英文，
   single＝单文件、dir＝单目录；包内那份说明从 `使用说明.txt` 改名 `README.txt`。
   正文仍是中文，改名只改**文件名**。）

★ R182：单目录版弃用 standalone 模式（用户"解压开一堆文件吓一跳"），
改为 onefile + --onefile-tempdir-spec={PROGRAM_DIR}/runtime——首次运行
在 exe 旁边解压出 runtime 文件夹，最终结构=根目录 1 个 exe + 1 个装
运行文件的文件夹，正是用户要的样子（{PROGRAM_DIR} 前缀是 Nuitka
官方特判，Options.py 剥掉前缀后按相对 exe 的路径处理）。

流程：Nuitka --zig C 编译 → zip（onefile 载荷只含代码与 README/
images，不含运行时个人数据）→ MD5/CRC32/SHA256 校验码。
版本号读 version.txt。
★R214（用户 -22"readme 里版本没更到 0.97，今后自动更新"）：打包时
生成 _build_README.md 嵌为包内 README——源文件用**对外发布版**
发布用_README_Gitee.md（★R214b 用户纠错：此前误嵌内部 README.md），
标题行自动注当前版本号；仓库 README.md 不被构建改动。
★R242（用户 34 批 item1"打包前，尽量先删本地旧包，再打"）：构建开始前先跑
`prune_old_packages()`，只删本版本目录里脚本自己产出的旧 zip / 校验码 /
使用说明，用户解压出来的目录一律不碰。政策同步放宽：自测/取证用途可以直接
打包，但**一个批次只打一次**，避免重复打包（一次 both ≈ 9 分钟）。
★R263（用户 16 批 item1"每次打包完了，把最近 3 次的更新整个大白话的简要
说明写到 dist_待上传 里面，别放在压缩包里面，发布 release 时正好复制用"）：
打包成功后 `write_recent_updates_note()` 把最近 3 次更新的大白话简述
（来源＝各 `发布说明_<版本>.md` 的「发布复制用简述」小节，按版本号排新在前）
写成 `dist_待上传/更新说明_最近3次.md`，序号 1/2/3，**不进 zip**。
⇒ 以后每出一份发布说明，都要顺手写那一小节，否则那一版不会进复制用说明。
★R325（用户 2026-10-14 点名"更新说明，需要中英文，特别是最近3次这个"）：同一份
复制用说明改成**中英双份**——每个版本先中文简述、再英文简述，英文取各发布说明里
新立的那一节 `## 发布复制用简述（EN）`（没写就明写"这一版还没有英文简述"，不硬编）。
"""
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
import zlib

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)

MODE = (sys.argv[1] if len(sys.argv) > 1 else 'both').lower()
if MODE not in ('onefile', 'standalone', 'both'):
    print(f'未知模式 {MODE}（可选 onefile / standalone / both）')
    sys.exit(1)

# ★R204 NEW-R13/ROOT-18：句柄关闭 + 锚定脚本目录（相对 cwd 不再受启动位置影响）
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       'version.txt'), encoding='utf-8') as _vf:
    VERSION = _vf.read().strip()
APP = 'deskbits_beta_' + VERSION
OUT_DIR = os.path.join('dist_待上传', APP)
#: ★R325（用户 2026-10-14 点名"今后打包的时候，出一个全部都是英文文件名的，
#: 比如 deskbits_beta_0.99-R43-single.zip 类似的，到时候我要上传 github"）：
#: **发布物的文件名**一律英文、中英不留两套（Gitee 与 GitHub 用同一套名字）——
#:   zip            `{APP}-single.zip`（单文件） / `{APP}-dir.zip`（单目录）
#:   校验码 sidecar `{APP}-single_SHA256.txt` / `{APP}-dir_SHA256.txt`
#:   包内那份说明   `README.txt`（原来叫 `使用说明.txt`）
#: 文件名之外的**正文**仍是中文（用户点的是"文件名英文"，不是"翻译成英文"）；
#: 复制用的 `更新说明_最近3次.md` 从本批起**中英双份**（见 write_recent_updates_note）。
#: 旧中文名的残留由 `prune_old_packages()` 一起清掉，避免同一目录里两套名字并排。
CHECK_SUFFIX = '_SHA256.txt'
PKG_README = 'README.txt'
LEGACY_CHECK_SUFFIX = '_校验码.txt'
LEGACY_PKG_README = '使用说明.txt'
CREATE_NO_WINDOW = 0x08000000

# ★R246（用户 38 批 item3「今后每生成一个 0.98，后面标注下，比如 0.98-01，
# 这样好判定版本不一样」）：`version.txt` 从此允许写成 `0.98-R<批次>` 这种
# "主版本 + 构建标注"形式，一套目录名/exe 名/README/标题栏都跟着变。
# 但 Windows 的版本资源**只收点分数字**，所以另拆一个纯数值口径：
#   VER_SHOW = 对外展示与文件名（0.98-R246）
#   VER_NUM  = 喂给 --file-version/--product-version（0.98 → 补成 0.98.0.0）
VER_SHOW = VERSION
_m = re.match(r'^(\d+(?:\.\d+)*)', VERSION)
VER_NUM = _m.group(1) if _m else VERSION
#: 可选构建标注（`-R246`）——README 里"剥旧标注再写新的"必须一起吃掉，
#: 否则重打一次就叠成 `0.98-R246-R246`。
TAG_RE = r'(?:-[A-Za-z]+\d+)?'


def add_tree(zf, path, arc_prefix):
    """把文件或整个目录树写进 zip，返回写入文件数"""
    n = 0
    if os.path.isfile(path):
        zf.write(path, arc_prefix.replace('\\', '/'))
        return 1
    for dirpath, _dirs, files in os.walk(path):
        for fn in files:
            full = os.path.join(dirpath, fn)
            arc = os.path.join(arc_prefix, os.path.relpath(full, path))
            zf.write(full, arc.replace('\\', '/'))
            n += 1
    return n


def make_zip(zip_path, src, arc_prefix):
    """压缩 → 校验码 txt（大小 + MD5 + CRC32 + SHA256）；返回 (文件数, zip 大小)

    ★R277（用户 09-30 item2「就按现有三个格式 MD5、CRC32、SHA256 来做」）：
    旧版只算 MD5/CRC32，SHA256 那行是一句**提示**，而且提示里的 certutil
    参数写的还是 MD5——标题说 SHA256、命令给 MD5，自相矛盾（就是 N16 那条）。
    现在三样都真算、值都写进去，提示行与标题一致。"""
    count = 0
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        count = add_tree(zf, src, arc_prefix)
    md5, sha, crc = hashlib.md5(), hashlib.sha256(), 0
    with open(zip_path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            md5.update(chunk)
            sha.update(chunk)
            crc = zlib.crc32(chunk, crc)
    size = os.path.getsize(zip_path)
    txt = zip_path.rsplit('.', 1)[0] + CHECK_SUFFIX
    with open(txt, 'w', encoding='utf-8') as f:
        f.write(f'{os.path.basename(zip_path)}\n'
                f'大小: {size} 字节 ({size / 1048576:.2f} MB)\n'
                f'MD5:     {md5.hexdigest()}\n'
                f'CRC32:   {crc & 0xFFFFFFFF:08X}\n'
                f'SHA256:  {sha.hexdigest()}\n'
                f'下载后自核（Windows 自带，在 zip 同一目录里跑）：\n'
                f'  certutil -hashfile "{os.path.basename(zip_path)}" SHA256\n')
    return count, size


def _prepare_icon():
    """★ R179（用户 -10/1"测试打包用根目录 icon.png"）：icon.png 经
    Pillow 转多尺寸 ico（16~256，资源管理器各视图都清晰）；无 png 时
    回退现成 icon.ico；都没有则不带图标参数。"""
    png = os.path.join(ROOT, 'icon.png')
    ico = os.path.join(ROOT, 'icon.ico')
    gen = os.path.join(ROOT, 'icon_generated.ico')
    if os.path.isfile(png):
        try:
            from PIL import Image
            im = Image.open(png).convert('RGBA')
            im.save(gen, format='ICO', sizes=[(16, 16), (24, 24), (32, 32),
                                              (48, 48), (64, 64),
                                              (128, 128), (256, 256)])
            print(f'图标: {png} → {os.path.basename(gen)}（多尺寸）')
            return ['--windows-icon-from-ico=' + gen]
        except Exception as e:
            print(f'icon.png 转 ico 失败（改用 icon.ico）: {e}')
    if os.path.isfile(ico):
        print('图标: icon.ico')
        return ['--windows-icon-from-ico=' + ico]
    print('图标: 未找到（exe 将无自定义图标）')
    return []


def sync_readme():
    """★R214：生成打包用 _build_README.md。

    ★R214b（用户纠错）：发布包嵌的是**对外净化版** 发布用_README_Gitee.md
    （无内部软件名/仓库细节），不是仓库内部 README.md——此前嵌错了文件。
    自动同步：标题行补当前版本号（发布版原本不带），缺失时回退内部
    README（标题/exe 名按 version.txt 改写）。changelog 历史小节不动。"""
    pub = '发布用_README_Gitee.md'
    if os.path.isfile(pub):
        try:
            src = open(pub, encoding='utf-8').read()
        except Exception as e:
            print(f'⚠ 读 {pub} 失败: {e}')
            return pub
        lines = src.split('\n')
        if lines and lines[0].startswith('# '):
            base = re.sub(r'\s*v\d+\.\d+(?:\.\d+)?' + TAG_RE + r'\s*$',
                          '', lines[0])
            lines[0] = f'{base} v{VER_SHOW}'
        out = '\n'.join(lines)
        out = re.sub(r'deskbits_beta_\d+\.\d+' + TAG_RE +
                     r'(-单文件|-单目录)?(\.exe)?',
                     lambda m: APP + (m.group(1) or '') + (m.group(2) or ''),
                     out)
        with open('_build_README.md', 'w', encoding='utf-8') as f:
            f.write(out)
        print(f'打包 README = 发布版（{pub}），标题已注版本 v{VER_SHOW}')
        return '_build_README.md'
    try:
        src = open('README.md', encoding='utf-8').read()
    except Exception as e:
        print(f'⚠ 读 README.md 失败（打包仍嵌原文件）: {e}')
        return 'README.md'
    out = re.sub(r'^# (.*?)v\d+\.\d+(?:\.\d+)?' + TAG_RE,
                 lambda m: f"# {m.group(1)}v{VER_SHOW}", src, count=1,
                 flags=re.M)
    out = re.sub(r'deskbits_beta_\d+\.\d+' + TAG_RE +
                 r'(-单文件|-单目录)?(\.exe)?',
                 lambda m: APP + (m.group(1) or '') + (m.group(2) or ''), out)
    with open('_build_README.md', 'w', encoding='utf-8') as f:
        f.write(out)
    print(f'README 版本字段已同步 → v{VER_SHOW} / {APP}.exe（发布版缺失，回退内部版）')
    return '_build_README.md'


def build(mode):
    """跑 Nuitka 编译，返回 0=成功。★R182 起两种模式都用 onefile。"""
    one = mode == 'onefile'
    # ★ R180：单文件版缓存目录带版本号——旧版(0.95)固定缓存曾让 exe
    # 解压出旧 version.txt/旧 exe 名/无 README 的陈年内容；
    # ★ R182：单目录版解压到 exe 旁 runtime 文件夹（用户要求的
    # "根目录 exe + 旁边一个文件夹"结构），{PROGRAM_DIR} 前缀官方特判
    spec = (f'{{CACHE_DIR}}/deskbits-onefile-{VERSION}' if one
            else '{PROGRAM_DIR}/runtime')
    cmd = [
        sys.executable, '-m', 'nuitka',
        '--mode=onefile',
        f'--output-dir={"dist_nuitka" if one else "dist_nuitka_standalone"}',
        f'--output-filename={APP}.exe',
        '--windows-console-mode=disable',
        # ★ R177/R179：exe 图标（不带就跟 DOS 程序一个样）
        *_prepare_icon(),
        # 本机无 MSVC/gcc；--mingw64 在 Python 3.13+ 被禁用，--zig 是
        # 3.14 下唯一自带下载的 C 编译器路线（clang 内核，64 位 Python）
        '--zig',
        f'--onefile-tempdir-spec={spec}',
        # ★ R180：exe 右键属性可见的版本信息（版本号写进程序）
        # ★R246：这里**只能**是点分数字（`0.98-R246.0.0` 会被版本资源拒收/
        # 悄悄丢掉），所以用拆出来的 VER_NUM；带 -R246 的对外口径走
        # 文件名、目录名、README 和**标题栏**（用户判版本就看标题栏）。
        '--company-name=wanke2022',
        '--product-name=拾碎-懒人桌面工具箱 (DeskBits)',
        f'--file-version={VER_NUM}.0.0',
        f'--product-version={VER_NUM}.0.0',
        f'--include-data-files={sync_readme()}=README.md',
        '--include-data-files=version.txt=version.txt',
        '--include-data-dir=images=images',
        '--nofollow-import-to=matplotlib,numpy,scipy,torch,cryptography',
        '--remove-output',
        '--assume-yes-for-downloads',
        'launcher.pyw',
    ]
    print(f'=== {mode} 编译开始（预计 5~20 分钟）===')
    t0 = time.time()
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    print(f'rc={p.returncode}  耗时 {(time.time() - t0) / 60:.1f} 分钟')
    if p.returncode != 0:
        print(p.stderr[-2000:] if p.stderr else '(stderr 空)')
        return 1
    return 0


def _extract_changelog():
    """★ R190（用户 11 批"发布版简单说下更新了什么"）：从 `更新说明.md`
    的 CHANGELOG-START/END 标记间提取版本更新说明；标记缺失返回空。

    ★R327（用户 2026-10-16 点名"readme 中英文都不要写更新说明，发布版本里已经
    做了，要不重复了"）：这一大块从 README.md 搬进了 `更新说明.md` ⇒ 数据源跟着
    搬。这里**只是兜底**（本版 `发布说明_<版本>.md` 没有简述小节时才用），
    但兜底不能变成静默空段——「宁可为长，不许为空」那条口径不变。"""
    try:
        with open(os.path.join(ROOT, u'更新说明.md'), encoding='utf-8') as _rf:
            t = _rf.read()
        s = t.index('<!-- CHANGELOG-START -->') + len('<!-- CHANGELOG-START -->')
        e = t.index('<!-- CHANGELOG-END -->')
        # txt 里去掉 Markdown 标题/粗体符号，纯文本可读
        lines = [l.lstrip('#').replace('**', '').strip()
                 for l in t[s:e].strip().splitlines()]
        return '\n'.join(lines).strip()
    except Exception:
        return ''


def update_notes_for_readme(version=None):
    """★R265（用户 20 批 item1"先把第二条做了，更新说明短句编号式"）：
    `使用说明.txt` 顶部那段「本版更新」的**数据源**——取本版
    `发布说明_<版本>.md` 的「发布复制用简述」（一行一条的短句编号式），
    不再搬 README 里那几百字散文。

    本版那一小节还没写时退回 `_extract_changelog()`（README 长段）——
    宁可为长，不许为空：包里的说明少一段比多一段更糟。
    """
    ver = version or VERSION
    for v, body in release_summaries(root=ROOT, keep=0):
        if v == ver and body:
            return body
    return _extract_changelog()


def write_readme():
    """★ R177（用户 -09/3"两个执行文件要说明执行哪一个"）：产物目录
    生成 README.txt（★R325 改名，原名 `使用说明.txt`，包根同名）——
    两个 zip 各是什么、下载哪个、运行哪个 exe。
    ★ R190：头部带版本更新说明（进 zip 与产物目录）。
    ★ R265 批20 item1：那段更新说明改成"短句编号式"（源＝本版发布说明的简述小节）。"""
    changelog = update_notes_for_readme()
    head = (f'===== 本版更新（v{VERSION}）=====\r\n{changelog}\r\n\r\n'
            if changelog else '')
    txt = (
        f'{APP} 使用说明\r\n'
        f'====================\r\n\r\n'
        + head +
        f'两个压缩包的区别（下载其中一个即可）：\r\n'
        f'  · {APP}-single.zip   —— 单文件版，解压后只有一个 {APP}.exe，'
        f'双击它运行（推荐，简单）\r\n'
        f'  · {APP}-dir.zip      —— 单目录版，解压后同样只有 1 个 {APP}.exe，'
        f'双击运行即可；\r\n'
        f'    首次启动会在 exe 旁边自动生成 runtime 文件夹'
        f'（程序运行文件都在里面，\r\n'
        f'    不要删除或改名；升级新版本时直接用新 exe 覆盖旧的即可，'
        f'文件夹不用动）\r\n\r\n'
        f'要点：\r\n'
        f'  1. 两种包的主程序都是 1 个 exe（名字都是 {APP}.exe），'
        f'双击它就是运行软件；\r\n'
        f'     单目录版 runtime 文件夹里的 .dll/.pyd 是运行所需的'
        f'组成部分，不用管。\r\n'
        f'  2. 数据（分类/设置/备份）保存在 exe 旁边的 data/config 文件夹，'
        f'换电脑把这两个文件夹一起拷走即可。\r\n'
        f'  3. 首次运行如遇 Windows SmartScreen 提示：点"更多信息"→'
        f'"仍要运行"（软件未做付费签名，属正常现象）。\r\n'
        f'  4. 校验：附件 *{CHECK_SUFFIX} 有大小/MD5/CRC32/SHA256，'
        f'可用 certutil 核对文件完整性。\r\n'
        f'  5. 本软件仅供学习研究和个人使用，不提供任何担保，详见 README。\r\n'
        f'  6. 发布页：Gitee 发布仓与本软件 GitHub 源码仓的 Releases 都有本软件'
        f'（这两个 zip），GitHub 下载页 https://github.com/wangluyichen/deskbits/releases\r\n')
    p = os.path.join(OUT_DIR, PKG_README)
    with open(p, 'w', encoding='utf-8') as f:
        f.write(txt)
    return p


def prune_old_packages():
    """★R242（用户 34 批 item1"打包前，尽量先删本地旧包，再打"）：
    构建开始前清掉本次版本目录里的**旧产物文件**。

    只删本脚本自己产出的文件：`*.zip`、`*_SHA256.txt`、`README.txt`，
    再加 ★R325 改名之前的两份旧名（`*_校验码.txt`、`使用说明.txt`）——
    否则同一个目录里会中英文两套名字并排，上传时拿错那份。
    目录一律不碰——用户常把 zip 解压在产物目录旁边试跑，那是他的东西；
    也不动 `dist_nuitka*`（那是 Nuitka 的增量缓存，删了要多等几分钟）。
    删不掉的（文件被占用）只报告，不中断打包。
    """
    if not os.path.isdir(OUT_DIR):
        print('产物目录还没有旧包（首次打包）')
        return 0
    removed, kept = [], []
    for fn in sorted(os.listdir(OUT_DIR)):
        full = os.path.join(OUT_DIR, fn)
        if os.path.isdir(full):
            if fn != '_stage':
                kept.append(fn + '/')       # 用户解压出来的东西：保留并报备
            continue
        if (fn.endswith('.zip') or fn.endswith(CHECK_SUFFIX)
                or fn == PKG_README
                or fn.endswith(LEGACY_CHECK_SUFFIX) or fn == LEGACY_PKG_README):
            try:
                os.remove(full)
                removed.append(fn)
            except OSError as e:
                print(f'旧产物 {fn} 删不掉（可能被占用），继续: {e}')
    for stale in ('_stage',):               # 上次打包中断留下的临时台子
        shutil.rmtree(os.path.join(OUT_DIR, stale), ignore_errors=True)
    print(f'打包前清理：删掉 {len(removed)} 个旧产物文件'
          + ('（' + ', '.join(removed) + '）' if removed else '')
          + (f'；保留目录 {len(kept)} 个：' + ', '.join(kept) if kept else ''))
    return len(removed)


def package(mode):
    """单 exe 复制 → zip → 校验码。返回产物路径或 None"""
    os.makedirs(OUT_DIR, exist_ok=True)
    # ★ R182：两种包都是 onefile 单 exe（单目录版首次运行才在 exe 旁
    # 解压出 runtime 文件夹，见 build()）；单目录 zip 根另带 README.txt
    label = 'single' if mode == 'onefile' else 'dir'
    exe = os.path.join('dist_nuitka' if mode == 'onefile'
                       else 'dist_nuitka_standalone', APP + '.exe')
    if not os.path.isfile(exe):
        print(f'找不到产物 {exe}')
        return None
    zip_path = os.path.join(OUT_DIR, f'{APP}-{label}.zip')
    stage = os.path.join(OUT_DIR, '_stage')
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage, exist_ok=True)
    shutil.copy2(exe, os.path.join(stage, APP + '.exe'))
    if mode != 'onefile':
        # ★ R177：说明文件放进包根（双击哪个 exe 一目了然）
        shutil.copy2(write_readme(), os.path.join(stage, PKG_README))
    n, size = make_zip(zip_path, stage, '')
    shutil.rmtree(stage, ignore_errors=True)
    write_readme()
    print(f'[OK] {os.path.basename(zip_path)}（{n} 文件, '
          f'{size / 1048576:.2f} MB）+ {os.path.basename(zip_path)[:-4]}{CHECK_SUFFIX}')
    return zip_path


COPY_SECTION = '## 发布复制用简述'
#: ★R325（用户 2026-10-14 点名"更新说明，需要中英文，特别是最近3次这个"）：
#: 各 `发布说明_<版本>.md` 里紧跟中文小节的那一份**英文**简述。
#: 为什么单独立一节、不并进中文那一节：① 中文小节是 Gitee 发布页/包内说明的
#: **逐字数据源**（I2/H2 两条门禁钉死"逐字相等"+ 单行 ≤50 字 + 首行「1，」），
#: 往里塞英文行会把那三条一起打断；② 英文条数与中文条数一一对应，
#: 复制出去就是"中文一段、英文一段"，GitHub Release 直接贴。
COPY_SECTION_EN = '## 发布复制用简述（EN）'
#: 复制用说明的落点：`dist_待上传/` 顶层（**不在版本产物目录里 ⇒ 不进 zip**，
#: 也不会被 prune 的那三类文件名匹配到）
COPY_DIR = 'dist_待上传'
COPY_FILE = '更新说明_最近3次.md'


def _ver_sort_key(ver):
    """`0.99-R002` → ((0, 99), 2)：主版本号元组 + 构建标注数字。

    排序必须按**版本号**，不能按 mtime——重打一次包就把老文件的 mtime 刷新的
    话，"最近 3 次"会退回上上上版，用户复制出去的是错的版本说明。
    """
    m = re.match(r'^(\d+(?:\.\d+)*)[A-Za-z-]*(\d*)', ver)
    if not m:
        return ((), 0)
    nums = tuple(int(x) for x in m.group(1).split('.'))
    tail = int(m.group(2)) if m.group(2) else 0
    return (nums, tail)


def _summaries_for(root, section):
    """按 `section` 那一节收 `发布说明_*.md`，返回 [(版本号, 正文)]，新版在前。

    `release_summaries` / `release_summaries_en` 共用这一把尺子：口径只有一份，
    中英两边解析行为不许分叉（一节抓不到的版本，另一节同规则抓不到）。
    没有该小节的文件、坏文件名一律不吃。正文＝小节标题之后到下一个 `## ` 之前
    （两头去空行）——所以英文节必须**排在中文节之后**，两节互不吃到对方。
    """
    got = []
    for fn in sorted(os.listdir(root)):
        if not (fn.startswith('发布说明_') and fn.endswith('.md')):
            continue
        ver = fn[len('发布说明_'):-len('.md')]
        try:
            text = open(os.path.join(root, fn), encoding='utf-8').read()
        except (OSError, UnicodeDecodeError) as e:
            print(f'读 {fn} 失败（跳过）: {e}')
            continue
        # 命中点必须是**整行标题**：`## 发布复制用简述` 是 `## 发布复制用简述（EN）`
        # 的前缀，只按 find() 取第一处会在"某版只有英文节"时把英文当中文收走。
        i, _hit = -1, False
        _p = 0
        while True:
            i = text.find(section, _p)
            if i < 0:
                break
            if text[i + len(section):i + len(section) + 1] == '\n':
                _hit = True
                break
            _p = i + 1
        if not _hit:
            continue
        body = text[i + len(section):]
        j = body.find('\n## ')
        if j >= 0:
            body = body[:j]
        body = body.strip()
        if body:
            got.append((ver, body))
    got.sort(key=lambda it: _ver_sort_key(it[0]), reverse=True)
    return got


def release_summaries(root=None, keep=3):
    """从 `发布说明_<版本>.md` 的「发布复制用简述」小节收最近 keep 次更新。

    返回 [(版本号, 大白话正文)]，新版本在前。没有小节的文件、坏文件名一律不吃。
    正文＝小节标题之后到下一个 `## ` 之前的内容（两头去空行）。
    """
    got = _summaries_for(root or ROOT, COPY_SECTION)
    return got[:keep] if keep else got


def release_summaries_en(root=None, keep=3):
    """★R325：同一批发布说明里那份**英文**简述（`## 发布复制用简述（EN）`）。

    老版本没写英文节就是没有——不硬编、不机器翻译凑数（宁缺毋滥：复制出去的是
    发布页正文，写一句猜的比不写更糟）。
    """
    got = _summaries_for(root or ROOT, COPY_SECTION_EN)
    return got[:keep] if keep else got


def write_recent_updates_note(root=None, out_dir=None, keep=3):
    """★R263（用户 16 批 item1）：每次打包后把最近 3 次更新的**大白话简述**
    写成 `dist_待上传/更新说明_最近3次.md`，发 release 时直接复制，不用打字。

    刻意放在**版本产物目录之外**：zip 里不带它（用户要的是复制用，不是包内容），
    `prune_old_packages()` 也不会把它当旧产物删掉。
    返回写出的路径；一条简述都没有时返回 None（不落一个空壳文件）。
    """
    items = release_summaries(root=root, keep=keep)
    if not items:
        print('没有带「发布复制用简述」小节的发布说明，复制用说明这次不生成')
        return None
    #: ★R325：英文简述按**同一批**版本配对（同一个 keep 窗口、同一把排序）。
    _en_map = dict(release_summaries_en(root=root, keep=keep))
    out_dir = out_dir or os.path.join(ROOT, COPY_DIR)
    lines = ['# 最近 %d 次更新（发布 release 时直接复制）' % len(items),
             '',
             'Auto-generated from `发布说明_*.md`. Chinese first, English below '
             'each version — paste the pair straight into the release page.',
             '',
             '自动生成于 %s ｜ 来源：`发布说明_*.md` 的「发布复制用简述」'
             '与「发布复制用简述（EN）」小节 ｜ 由 打包_Nuitka.py 每次打包后刷新'
             % time.strftime('%Y-%m-%d %H:%M'),
             '']
    for n, (ver, body) in enumerate(items, 1):
        lines.append('## %d. %s' % (n, ver))
        lines.append('')
        lines.append('**中文**')
        lines.append('')
        lines.extend(body.split('\n'))
        lines.append('')
        lines.append('**English**')
        lines.append('')
        _en = _en_map.get(ver)
        if _en:
            lines.extend(_en.split('\n'))
        else:
            # 缺英文就明写缺，不静默少一段（复制的人看得见缺什么，才知道要补哪一节）
            lines.append(u'（这一版还没有英文简述：在 `发布说明_%s.md` 里补一节 '
                         u'`%s`，下次打包自动带上）' % (ver, COPY_SECTION_EN[3:]))
        lines.append('')
    os.makedirs(out_dir, exist_ok=True)
    p = os.path.join(out_dir, COPY_FILE)
    with open(p, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(lines))
    print(f'复制用说明已写出：{p}（{len(items)} 条）')
    return p


def main():
    print(f'=== DeskBits Nuitka 打包（{MODE}）→ {APP} ===')
    # ★ R181：一并清掉无版本后缀的旧 onefile 缓存目录（0.95 时代
    # 旧解压内容曾让用户看到旧 version.txt/旧 exe 名）
    import shutil as _sh
    _old_cache = os.path.join(os.environ.get('LOCALAPPDATA', ''),
                              'deskbits-onefile')
    if os.path.isdir(_old_cache):
        try:
            _sh.rmtree(_old_cache, ignore_errors=True)
            print(f'已清理旧缓存 {_old_cache}')
        except Exception:
            pass
    # ★ R180：清历史残留 exe（旧名 deskbits-nuitka*.exe 等）
    import glob as _g
    for _d in ('dist_nuitka', 'dist_nuitka_standalone'):
        for _old in _g.glob(os.path.join(_d, 'deskbits-nuitka*.exe')):
            try:
                os.remove(_old)
                print(f'已清理旧产物 {_old}')
            except OSError:
                pass
    # ★R242 用户 item1：先删本地旧包，再打（避免"以为拿到新包其实是旧的"）
    prune_old_packages()
    modes = ['onefile', 'standalone'] if MODE == 'both' else [MODE]
    fail = 0
    for m in modes:
        if build(m):
            fail += 1
            continue
        if not package(m):
            fail += 1
    if fail:
        print(f'=== {fail} 个模式失败 ===')
        return 1
    # ★R263（用户 16 批 item1）：打成之后才刷新复制用说明（没打成就别冒充）
    write_recent_updates_note()
    print(f'=== 全部完成，产物在 {OUT_DIR} ===')
    return 0


if __name__ == '__main__':
    sys.exit(main())
