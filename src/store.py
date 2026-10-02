"""货运履约服务的事件日志存储与停机恢复。

所有状态变更（接单、装载计划、节点回执、冻结、处置、拆分/合并/换班、
延误）先经 ``JourneyService`` 校验，再以一行一个 JSON 事件追加到日志；
进程重启后按顺序重放事件即可重建全部状态，延误、改配和异常处置
不会因停机丢失，可继续推进。

写入为追加 + flush/fsync；校验失败的命令不产生任何事件。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from src.journey import (
    Doc,
    JourneyService,
    Node,
    Receipt,
)

SCHEMA_VERSION = 1


def _dt(value: datetime) -> str:
    return value.isoformat()


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _doc_to_dict(doc: Doc) -> dict[str, Any]:
    return {"kind": doc.kind, "number": doc.number, "version": doc.version}


def _doc_from_dict(value: dict[str, Any]) -> Doc:
    return Doc(kind=value["kind"], number=value["number"], version=int(value.get("version", 1)))


@dataclass
class AppService:
    """对外应用服务：命令落事件日志，查询直接走内存领域模型。"""

    journey: JourneyService
    log_path: Path

    # ---- 命令 ----

    def book_order(
        self,
        order_id: str,
        customer: str,
        promise_min: datetime,
        promise_max: datetime,
        at: datetime,
    ) -> None:
        self.journey.book_order(order_id, customer, promise_min, promise_max)
        self._append(
            "order_booked",
            at,
            {
                "order_id": order_id,
                "customer": customer,
                "promise_min": _dt(promise_min),
                "promise_max": _dt(promise_max),
            },
        )

    def plan_loading(
        self,
        unit_no: str,
        order_id: str,
        container_no: str,
        cargo_type: str,
        weight_kg: float,
        docs: Iterable[Doc],
        train_no: str,
        planned_at: datetime,
        at: Optional[datetime] = None,
    ) -> None:
        docs = tuple(docs)
        self.journey.plan_loading(
            unit_no, order_id, container_no, cargo_type, weight_kg, docs, train_no, planned_at
        )
        self._append(
            "loading_planned",
            at or planned_at,
            {
                "unit_no": unit_no,
                "order_id": order_id,
                "container_no": container_no,
                "cargo_type": cargo_type,
                "weight_kg": float(weight_kg),
                "docs": [_doc_to_dict(d) for d in docs],
                "train_no": train_no,
                "planned_at": _dt(planned_at),
            },
        )

    def receive_receipt(self, receipt: Receipt) -> None:
        """回传节点回执。重复回执/核对/顺序/冻结冲突由领域层拒绝且不落事件。"""
        self.journey.apply_receipt(receipt)
        self._append(
            "receipt_applied",
            receipt.reported_at,
            {
                "receipt_no": receipt.receipt_no,
                "container_no": receipt.container_no,
                "node": receipt.node.value,
                "reported_at": _dt(receipt.reported_at),
                "weight_kg": receipt.weight_kg,
                "docs": [_doc_to_dict(d) for d in receipt.docs],
                "source_party": receipt.source_party,
                "remark": receipt.remark,
            },
        )

    def freeze_unit(
        self,
        container_no: str,
        reason_code: str,
        detail: str,
        since: datetime,
        case_no: str,
        owner: str = "口岸查验人员",
    ) -> None:
        self.journey.freeze_unit(container_no, reason_code, detail, since, case_no, owner)
        self._append(
            "unit_frozen",
            since,
            {
                "container_no": container_no,
                "reason_code": reason_code,
                "detail": detail,
                "since": _dt(since),
                "case_no": case_no,
                "owner": owner,
            },
        )

    def resolve_freeze(
        self, container_no: str, resolved_at: datetime, resolution: str
    ) -> None:
        self.journey.resolve_freeze(container_no, resolved_at, resolution)
        self._append(
            "freeze_resolved",
            resolved_at,
            {
                "container_no": container_no,
                "resolved_at": _dt(resolved_at),
                "resolution": resolution,
            },
        )

    def split_unit(
        self,
        source_container_no: str,
        new_unit_no: str,
        new_container_no: str,
        cargo_type: str,
        moved_weight_kg: float,
        docs: Iterable[Doc],
        train_no: str,
        at: datetime,
    ) -> None:
        docs = tuple(docs)
        self.journey.split_unit(
            source_container_no,
            new_unit_no,
            new_container_no,
            cargo_type,
            moved_weight_kg,
            docs,
            train_no,
            at,
        )
        self._append(
            "unit_split",
            at,
            {
                "source_container_no": source_container_no,
                "new_unit_no": new_unit_no,
                "new_container_no": new_container_no,
                "cargo_type": cargo_type,
                "moved_weight_kg": float(moved_weight_kg),
                "docs": [_doc_to_dict(d) for d in docs],
                "train_no": train_no,
            },
        )

    def merge_units(
        self, source_container_no: str, target_container_no: str, at: datetime
    ) -> None:
        self.journey.merge_units(source_container_no, target_container_no, at)
        self._append(
            "units_merged",
            at,
            {
                "source_container_no": source_container_no,
                "target_container_no": target_container_no,
            },
        )

    def reassign_to_next_train(
        self,
        container_no: str,
        next_train_no: str,
        at: datetime,
        delay_days: int,
        reason: str,
    ) -> None:
        self.journey.reassign_to_next_train(
            container_no, next_train_no, at, delay_days, reason
        )
        self._append(
            "train_reassigned",
            at,
            {
                "container_no": container_no,
                "next_train_no": next_train_no,
                "delay_days": delay_days,
                "reason": reason,
            },
        )

    def record_delay(
        self, container_no: str, at: datetime, delay_days: int, reason: str
    ) -> None:
        self.journey.record_delay(container_no, at, delay_days, reason)
        self._append(
            "delay_recorded",
            at,
            {
                "container_no": container_no,
                "delay_days": delay_days,
                "reason": reason,
            },
        )

    # ---- 查询 ----

    def explain_order(self, order_id: str, at: datetime) -> dict[str, Any]:
        return self.journey.explain_order(order_id, at)

    def train_status(self, train_no: str) -> dict[str, list[str]]:
        grouped = self.journey.train_status(train_no)
        return {key: [u.container_no for u in units] for key, units in grouped.items()}

    # ---- 持久化 ----

    def _append(self, event_type: str, at: datetime, payload: dict[str, Any]) -> None:
        event = {
            "schema": SCHEMA_VERSION,
            "type": event_type,
            "at": _dt(at),
            "payload": payload,
        }
        line = json.dumps(event, ensure_ascii=False, sort_keys=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def open_app(log_path: Path | str) -> AppService:
    """打开履约服务：日志不存在则新建，存在则重放全部事件重建状态。"""
    path = Path(log_path)
    service = JourneyService()
    if path.exists():
        with path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    _replay(service, event)
                except Exception as exc:  # 损坏日志必须显式失败，不能静默跳过
                    raise ValueError(f"事件日志第 {line_no} 行无法重放: {exc}") from exc
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    return AppService(journey=service, log_path=path)


def _replay(service: JourneyService, event: dict[str, Any]) -> None:
    if event.get("schema") != SCHEMA_VERSION:
        raise ValueError(f"不支持的事件 schema 版本: {event.get('schema')}")
    p = event["payload"]
    etype = event["type"]

    if etype == "order_booked":
        service.book_order(
            p["order_id"],
            p["customer"],
            _parse_dt(p["promise_min"]),
            _parse_dt(p["promise_max"]),
        )
    elif etype == "loading_planned":
        service.plan_loading(
            p["unit_no"],
            p["order_id"],
            p["container_no"],
            p["cargo_type"],
            p["weight_kg"],
            tuple(_doc_from_dict(d) for d in p["docs"]),
            p["train_no"],
            _parse_dt(p["planned_at"]),
        )
    elif etype == "receipt_applied":
        service.apply_receipt(
            Receipt(
                receipt_no=p["receipt_no"],
                container_no=p["container_no"],
                node=Node(p["node"]),
                reported_at=_parse_dt(p["reported_at"]),
                weight_kg=p.get("weight_kg"),
                docs=tuple(_doc_from_dict(d) for d in p.get("docs", [])),
                source_party=p.get("source_party", ""),
                remark=p.get("remark", ""),
            )
        )
    elif etype == "unit_frozen":
        service.freeze_unit(
            p["container_no"],
            p["reason_code"],
            p["detail"],
            _parse_dt(p["since"]),
            p["case_no"],
            p.get("owner", "口岸查验人员"),
        )
    elif etype == "freeze_resolved":
        service.resolve_freeze(
            p["container_no"], _parse_dt(p["resolved_at"]), p["resolution"]
        )
    elif etype == "unit_split":
        service.split_unit(
            p["source_container_no"],
            p["new_unit_no"],
            p["new_container_no"],
            p["cargo_type"],
            p["moved_weight_kg"],
            tuple(_doc_from_dict(d) for d in p["docs"]),
            p["train_no"],
            _parse_dt(event["at"]),
        )
    elif etype == "units_merged":
        service.merge_units(
            p["source_container_no"], p["target_container_no"], _parse_dt(event["at"])
        )
    elif etype == "train_reassigned":
        service.reassign_to_next_train(
            p["container_no"],
            p["next_train_no"],
            _parse_dt(event["at"]),
            int(p["delay_days"]),
            p["reason"],
        )
    elif etype == "delay_recorded":
        service.record_delay(
            p["container_no"],
            _parse_dt(event["at"]),
            int(p["delay_days"]),
            p["reason"],
        )
    else:
        raise ValueError(f"未知事件类型: {etype}")
