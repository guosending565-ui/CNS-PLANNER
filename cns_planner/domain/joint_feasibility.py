"""M4 finite JSON contracts; no state, I/O or safety formula ownership."""
from __future__ import annotations
from copy import deepcopy
import hashlib
import json
import math
from typing import Protocol

FORBIDDEN = {"runtime", "module_path", "callback", "script", "pickle", "factory", "execute"}


def json_value(value):
    def check(v):
        if v is None or type(v) in (str, bool, int): return
        if type(v) is float and math.isfinite(v): return
        if type(v) is list:
            for item in v: check(item)
            return
        if type(v) is dict:
            for key, item in v.items():
                if type(key) is not str or key in FORBIDDEN:
                    raise ValueError("JSON execution field or invalid key")
                check(item)
            return
        raise ValueError("finite JSON values required")
    check(value)
    return deepcopy(value)


def digest(value):
    json_value(value)
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def seal(value, key):
    result=json_value(value)
    result[key]=digest({k:v for k,v in result.items() if k != key})
    return result


def verify(value, key):
    json_value(value)
    if value.get(key) != digest({k:v for k,v in value.items() if k != key}):
        raise ValueError("fingerprint mismatch: "+key)


class SearchRuntime(Protocol):
    def evaluate_action_set(self, *, actions: list[dict]) -> dict: ...
    def evaluate_necessary_conditions(self, *, node: dict) -> dict: ...
    def monotonic_seconds(self) -> float: ...
    def cancel_requested(self) -> bool: ...
    def resident_memory_mb(self) -> float | None: ...
