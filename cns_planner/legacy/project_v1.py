"""Legacy phase-1 project manifest; the schema-v2 workbench does not depend on it."""
from dataclasses import dataclass, field, asdict
from math import isfinite
from uuid import uuid4


@dataclass
class Project:
    name: str
    mode: str = "single"
    id: str = field(default_factory=lambda: str(uuid4()))
    schema_version: int = 1
    bbox: list[float] | None = None  # west, south, east, north

    def validate(self):
        if not isinstance(self.name, str) or not self.name.strip() or len(self.name) > 120:
            raise ValueError("项目名称须为 1–120 个字符")
        if self.mode not in ("single", "multi"):
            raise ValueError("不支持的航路模式")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("不支持的项目版本，不能直接打开")
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("项目 ID 缺失")
        if self.bbox is not None:
            if not isinstance(self.bbox, list) or len(self.bbox) != 4:
                raise ValueError("边界须为西、南、东、北四个数值")
            if any(type(v) not in (int, float) or not isfinite(v) for v in self.bbox):
                raise ValueError("边界坐标须为有限数值")
            w, s, e, n = self.bbox
            if not (-180 <= w < e <= 180 and -90 < s < n < 90):
                raise ValueError("边界范围无效：西须小于东，南须小于北")

    def to_dict(self):
        self.validate()
        return asdict(self)
