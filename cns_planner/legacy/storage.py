"""Manifest-only serialization; no external paths are executed or loaded."""
import json
from .project_v1 import Project


def dumps(project: Project) -> str:
    return json.dumps(project.to_dict(), ensure_ascii=False, indent=2, allow_nan=False)


def loads(data: bytes | str) -> Project:
    if len(data) > 1_000_000:
        raise ValueError("项目元信息文件超过 1 MB")
    try:
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError("项目文件必须是 JSON 对象")
        project = Project(**value)
        project.validate()
        return project
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("项目文件格式或字段不正确") from exc
