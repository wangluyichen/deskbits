"""点击统计模块 - 记录每个图标的点击次数"""
import json
import os
import logging
from pathlib import Path
from typing import Dict

from utils.safeio import atomic_write_json

logger = logging.getLogger("desktop_manager")


class ClickTracker:
    """点击统计器"""
    
    def __init__(self, data_dir: str = None):
        self.data_dir = data_dir or str(Path(__file__).parent / "data")
        self.click_file = os.path.join(self.data_dir, "clicks.json")
        Path(self.data_dir).mkdir(parents=True, exist_ok=True)
        self.clicks: Dict[str, int] = self._load_clicks()
    
    def _load_clicks(self) -> Dict[str, int]:
        """加载点击数据"""
        if os.path.exists(self.click_file):
            try:
                with open(self.click_file, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                # ★R204 ROOT-13：结构校验（仿 UsageStats._clean）——列表/
                # 字符串进账后 .get/.items 各处 AttributeError 被吞成"榜单空"
                if isinstance(raw, dict):
                    return {str(k): int(v) for k, v in raw.items()
                            if isinstance(v, (int, float)) and v >= 0}
                logger.error("clicks.json 顶层不是 dict，已按空数据处理")
                return {}
            except Exception as e:
                logger.error(f"加载点击数据失败: {e}")
        return {}
    
    def _save_clicks(self):
        """保存点击数据"""
        try:
            # ★R204 ROOT-2：返回值必须消费——写盘失败留痕
            if not atomic_write_json(self.click_file, self.clicks, indent=4):
                logger.error("点击统计写盘失败（数据仅存内存）")
        except Exception as e:
            logger.error(f"保存点击数据失败: {e}")
    
    def record_click(self, icon_name: str):
        """记录一次点击"""
        self.clicks[icon_name] = self.clicks.get(icon_name, 0) + 1
        self._save_clicks()
    
    def get_clicks(self) -> Dict[str, int]:
        """获取所有点击数据"""
        return self.clicks.copy()
    
    def get_all_clicks(self) -> Dict[str, int]:
        """获取所有点击数据（别名）"""
        return self.clicks.copy()
    
    def get_top_clicked(self, n: int = 10) -> Dict[str, int]:
        """获取点击最多的前N个图标"""
        sorted_clicks = dict(
            sorted(self.clicks.items(), key=lambda x: x[1], reverse=True)
        )
        return dict(list(sorted_clicks.items())[:n])
    
    def reset_clicks(self):
        """重置点击统计"""
        self.clicks = {}
        self._save_clicks()
        logger.info("已重置点击统计")

    def replace_clicks(self, clicks: dict) -> int:
        """★ S 段：整体替换点击数据（导入全部数据用）。校验后写入，返回条数"""
        clean = {}
        for k, v in (clicks if isinstance(clicks, dict) else {}).items():
            if isinstance(k, str) and isinstance(v, int) and v >= 0:
                clean[k] = v
        self.clicks = clean
        self._save_clicks()
        return len(clean)
    
    def get_total_clicks(self) -> int:
        """获取总点击次数"""
        return sum(self.clicks.values())
    
    def get_unique_clickers(self) -> int:
        """获取被点击过的图标数量"""
        return len([c for c in self.clicks.values() if c > 0])
