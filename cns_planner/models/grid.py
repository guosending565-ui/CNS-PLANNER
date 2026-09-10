"""Core data model for MH/T 4063.1 airspace grid cells."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GridCell:
    """
    一个标准平面空域网格单元。

    当前阶段只保存网格本身的空间属性。
    人口、地形、空域、风险和 CNS 等业务属性以后单独映射，
    不直接写入这个基础模型。
    """

    grid_code: str
    level: int

    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float

    def __post_init__(self) -> None:
        """检查网格基础数据是否合法。"""

        if not self.grid_code:
            raise ValueError("grid_code 不能为空")

        if self.level < 1:
            raise ValueError("level 必须大于等于 1")

        if not -180.0 <= self.min_lon <= 180.0:
            raise ValueError("min_lon 超出有效经度范围")

        if not -180.0 <= self.max_lon <= 180.0:
            raise ValueError("max_lon 超出有效经度范围")

        if not -90.0 <= self.min_lat <= 90.0:
            raise ValueError("min_lat 超出有效纬度范围")

        if not -90.0 <= self.max_lat <= 90.0:
            raise ValueError("max_lat 超出有效纬度范围")

        if self.min_lon >= self.max_lon:
            raise ValueError("min_lon 必须小于 max_lon")

        if self.min_lat >= self.max_lat:
            raise ValueError("min_lat 必须小于 max_lat")

    @property
    def center_lon(self) -> float:
        """网格中心经度。"""
        return (self.min_lon + self.max_lon) / 2.0

    @property
    def center_lat(self) -> float:
        """网格中心纬度。"""
        return (self.min_lat + self.max_lat) / 2.0

    @property
    def center(self) -> tuple[float, float]:
        """返回网格中心点：(longitude, latitude)。"""
        return self.center_lon, self.center_lat

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        """
        返回网格边界。

        顺序：
        (min_lon, min_lat, max_lon, max_lat)
        """
        return (
            self.min_lon,
            self.min_lat,
            self.max_lon,
            self.max_lat,
        )

    def to_dict(self) -> dict:
        """转换为便于 API / JSON 使用的字典。"""
        return {
            "grid_code": self.grid_code,
            "level": self.level,
            "bbox": list(self.bbox),
            "center": list(self.center),
        }