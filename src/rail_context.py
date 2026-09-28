"""读取并校验跨境班列箱货节点与异常责任的领域上下文。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DOMAIN = "rail-freight-journey"
REQUIRED_KEYS = frozenset({"domain", "version", "actors", "facts", "constraints"})


def load_context(path: Path) -> dict[str, Any]:
    """读取领域资料，并拒绝缺字段、错领域或空约束。"""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != REQUIRED_KEYS:
        raise ValueError("领域资料字段不完整")
    if value["domain"] != DOMAIN:
        raise ValueError("领域标识不一致")
    if not isinstance(value["version"], int) or isinstance(value["version"], bool) or value["version"] < 1:
        raise ValueError("资料版本无效")
    for key, minimum in (("actors", 3), ("facts", 3), ("constraints", 4)):
        values = value[key]
        if not isinstance(values, list) or len(values) < minimum:
            raise ValueError(f"{key}内容不足")
        if any(not isinstance(entry, str) or not entry.strip() for entry in values):
            raise ValueError(f"{key}包含空内容")
    return value


def context_fingerprint(value: dict[str, Any]) -> str:
    """生成与键顺序无关的资料摘要，便于识别版本内容。"""
    import hashlib

    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
