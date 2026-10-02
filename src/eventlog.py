"""追加式事件日志与重放。

所有状态变更都先落盘为一行 JSON（append + fsync），进程重启后按序重放即可恢复，
因此延误、改配和异常处置跨越停机仍能继续推进。日志只追加、不修改。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterator


class EventStore:
    """JSONL 事件存储：每次追加都刷盘并 fsync，保证回执处理结果不丢。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False, sort_keys=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def replay(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"事件日志第{line_number}行无法解析") from exc
