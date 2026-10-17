# -*- coding: utf-8 -*-
"""安全的文件持久化工具。

原子写 JSON：先写临时文件再 os.replace（同卷原子），程序写一半被杀
也不会损坏原文件。范式来源：sylva（MIT）config.rs:69-78、
desktop_box（MIT）JsonStoreService.cs。"""
import json
import logging
import os
import time

logger = logging.getLogger("desktop_manager")

_TMP_SUFFIX = '.tmp'
_TMP_MAX_AGE = 30 * 60   # ★R204 ROOT-2：残留 .tmp 超过 30 分钟视为孤儿
_last_sweep = 0.0        # 限频：扫描有成本，不必每次写盘都做


def _sweep_stale_tmp(directory):
    """★R204 ROOT-2（后半）：清理同目录残留 .tmp（唯一临时名方案下，
    进程被杀时写一半的 .tmp 无人认领、永久滞留堆积）。30 分钟以上的
    才清（正在写的临时文件 age≈0），每分钟最多扫一次。"""
    global _last_sweep
    now = time.time()
    if now - _last_sweep < 60.0:
        return
    _last_sweep = now
    try:
        for fn in os.listdir(directory):
            if not fn.endswith(_TMP_SUFFIX):
                continue
            full = os.path.join(directory, fn)
            try:
                if now - os.path.getmtime(full) > _TMP_MAX_AGE:
                    os.remove(full)
                    logger.info(f"已清理残留临时文件: {full}")
            except OSError:
                pass
    except OSError:
        pass


def set_aside_corrupt(path, suffix='corrupt_'):
    """★R231（用户 05 批 item2）：把一份"本会话读不动"的原件另存为带时间戳留底。

    为什么需要它：历轮给分类/文件夹组加的"读失败即禁写"闸（★P1-1 /
    ★R190 CORE-5）保护的是「别拿内存里的默认值**偷覆盖**盘上真数据」，
    实现却做成了「本会话一律禁写」——于是用户把 config/categories.json 改坏
    之后，「清空」「导入」这两个**用户主动提供完整替换内容**的操作也被一起
    拦掉（05 批实拍图4/图5），剩下唯一出路是手改 JSON，对普通用户等于锁死。
    先留底再放行，两头都成立：原件一定还在盘上，闸的不变量一点没削弱。

    返回 (True, 留底路径)；原件已不存在 → (True, '')（没有可覆盖的真数据）；
    搬不动（被占用/只读）→ (False, 原因)——调用方**必须**继续保持禁写。
    """
    if not os.path.exists(path):
        return True, ''
    stamp = time.strftime('%Y%m%d_%H%M%S')
    dst = f'{path}.{suffix}{stamp}'
    n = 1
    while os.path.exists(dst):        # 同一秒连做两次自救也不能互相覆盖
        dst = f'{path}.{suffix}{stamp}_{n}'
        n += 1
    try:
        os.replace(path, dst)
    except Exception as e:
        logger.error(f'损坏文件留底失败 {path}: {e}')
        return False, str(e)
    logger.warning(f'损坏文件已另存留底 → {dst}')
    return True, dst


def _atomic_write(path, write_fn):
    """原子写通用体：唯一临时名 → 写满 → 同卷改名 → 扫残留。失败记日志返回 False。"""
    # ★R190（Qoder ROOT-2）：唯一临时名——固定 .tmp 在多线程并发写
    # 同一文件时互相覆盖，os.replace 可能搬走写一半的文件
    import threading as _th
    tmp = f"{path}.{os.getpid()}.{_th.get_ident()}.tmp"
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            write_fn(f)
        os.replace(tmp, path)
        _sweep_stale_tmp(os.path.dirname(os.path.abspath(str(path))))
        return True
    except Exception as e:
        logger.error(f"原子写入失败 {path}: {e}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


def atomic_write_json(path, data, indent=4, ensure_ascii=False, sort_keys=False):
    """原子写入 JSON 文件。成功返回 True，失败记录日志并返回 False。

    参数语义与 json.dump 一致；调用方各自保持原有的缩进/编码习惯，
    只把"直接写"换成"原子写"。返回值必须消费——假值=写盘失败，
    调用方应告警/回滚（R204：usage_stats/click_counter/parental_mode
    三处丢弃返回值已修）。"""
    return _atomic_write(path, lambda f: json.dump(
        data, f, indent=indent, ensure_ascii=ensure_ascii, sort_keys=sort_keys))


def atomic_write_text(path, text):
    """★R316（顺手）：原子写**纯文本**——契约与 atomic_write_json 同，只是不套 JSON 编码。

    为什么要单开一份而不是复用 json 那份：download_tracker 的清空水位线按纯文本读回
    （`read_text().strip()` + `datetime.fromisoformat`），套上 JSON 就多一层引号 ⇒
    解析抛异常、水位线静默失效＝刚清空的下载记录可能被同步"复活"。而裸 `write_text`
    是先截断再写，写一半被杀留下的半截 ISO 串同样让 fromisoformat 抛 ⇒ 两头都不能用，
    只有"临时名 + 同卷改名"能保住原件要么旧要么新。返回值同样必须消费。"""
    return _atomic_write(path, lambda f: f.write(text))
