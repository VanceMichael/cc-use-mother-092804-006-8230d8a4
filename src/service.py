"""货运履约服务。

围绕五条领域约束组织：

- 箱货身份：每个订单保存箱号、货类、装载计划、铁路区段、单证、节点回执与责任主体；
  同一箱货的拆分、合并、改配与目的站交接以谱系（parents）串联。
- 节点顺序：回执只能把箱货推进到紧邻的下一节点，重复回执天然幂等，跳节点被拒绝。
- 单证一致：编号相同但重量或单证变化时不推进，进入人工核对。
- 局部冻结：危险属性或查验异常只冻结受影响箱货，整列其他箱子仍可继续运行。
- 交付承诺：延误与改配记录原因和承诺影响，可按订单解释当前去向、下一责任人与预计交付区间。

所有写操作先产生事件并落盘（见 eventlog），再投影到内存状态，重启重放即可恢复。
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone
from typing import Any, Callable

from .eventlog import EventStore
from .model import (
    RESPONSIBLE_PARTY,
    STAGE_LABELS,
    STAGE_LOCATIONS,
    STAGE_ORDER,
    Order,
    Stage,
    TimeWindow,
    Train,
    Unit,
    next_responsible,
    next_stage,
)


class ServiceError(ValueError):
    """业务规则冲突（节点跳跃、冻结箱推进、未知箱号等）。"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class FulfillmentService:
    """以事件溯源方式实现的履约服务；使用方式为构造后调用 restore() 重放历史。"""

    def __init__(self, store: EventStore, clock: Callable[[], str] = _utc_now):
        self._store = store
        self._clock = clock
        self._seq = itertools.count(1)
        self.orders: dict[str, Order] = {}
        self.units: dict[str, Unit] = {}
        self.trains: dict[str, Train] = {}
        # 回执幂等键 -> (unit_id, stage)：同一来源回执重复到达不产生任何效果
        self._receipts: dict[str, tuple[str, Stage]] = {}
        # 被拒回执（冻结/跳节点）单独登记，重复到达只记一次；放行时清除
        self._rejected_receipts: dict[str, str] = {}

    # ------------------------------------------------------------------ 恢复

    def restore(self) -> int:
        """重放事件日志，重建订单、箱货、班列与已处理回执索引；返回重放事件数。"""
        count = 0
        max_seq = 0
        for event in self._store.replay():
            self._apply(event)
            count += 1
            max_seq = max(max_seq, int(event.get("seq", 0)))
        # 序号在历史最大值之后继续，保证重启后事件仍有序
        self._seq = itertools.count(max_seq + 1)
        return count

    def _emit(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        event = {
            "seq": next(self._seq),
            "ts": self._clock(),
            "type": event_type,
            "data": payload,
        }
        self._store.append(event)
        self._apply(event)
        return event

    # ------------------------------------------------------------- 注册建档

    def register_order(
        self,
        order_id: str,
        customer: str,
        cargo_category: str,
        dangerous: bool,
        promised_start: str,
        promised_end: str,
    ) -> Order:
        """受理客户订单并登记交付承诺区间。"""
        if order_id in self.orders:
            raise ServiceError(f"订单{order_id}已存在")
        self._emit(
            "order_registered",
            {
                "order_id": order_id,
                "customer": customer,
                "cargo_category": cargo_category,
                "dangerous": dangerous,
                "promised_window": {"start": promised_start, "end": promised_end},
            },
        )
        return self.orders[order_id]

    def register_train(
        self,
        train_id: str,
        planned_departure: str,
        eta_start: str,
        eta_end: str,
        origin: str = "南昌国际陆港",
        destination: str = "比什凯克",
    ) -> Train:
        """登记班列计划（南昌直达比什凯克，经霍尔果斯口岸出境）。"""
        if train_id in self.trains:
            raise ServiceError(f"班列{train_id}已存在")
        self._emit(
            "train_registered",
            {
                "train_id": train_id,
                "origin": origin,
                "destination": destination,
                "planned_departure": planned_departure,
                "eta_window": {"start": eta_start, "end": eta_end},
            },
        )
        return self.trains[train_id]

    def register_unit(
        self,
        unit_id: str,
        order_id: str,
        container_no: str,
        seal_no: str,
        cargo_category: str,
        weight_kg: float,
        documents: list[str],
        dangerous: bool | None = None,
    ) -> Unit:
        """建档一只箱货：箱号、封条、货类、重量、单证清单与装载计划归属订单。"""
        if order_id not in self.orders:
            raise ServiceError(f"订单{order_id}不存在")
        if unit_id in self.units:
            raise ServiceError(f"箱货{unit_id}已存在")
        order = self.orders[order_id]
        self._emit(
            "unit_registered",
            {
                "unit_id": unit_id,
                "order_id": order_id,
                "container_no": container_no,
                "seal_no": seal_no,
                "cargo_category": cargo_category,
                "weight_kg": weight_kg,
                "documents": list(documents),
                "dangerous": order.dangerous if dangerous is None else dangerous,
            },
        )
        return self.units[unit_id]

    def assign_unit(self, unit_id: str, train_id: str, railcar_no: str) -> None:
        """把箱货编入指定班列与车皮（装载计划落位）。"""
        unit = self._require_unit(unit_id)
        if train_id not in self.trains:
            raise ServiceError(f"班列{train_id}不存在")
        self._emit(
            "unit_assigned",
            {
                "unit_id": unit_id,
                "train_id": train_id,
                "railcar_no": railcar_no,
                "previous_train_id": unit.train_id,
            },
        )

    # ----------------------------------------------------------- 回执摄取

    def ingest_receipt(
        self,
        receipt_id: str,
        unit_id: str,
        stage: str,
        source: str,
        observed_weight_kg: float | None = None,
        observed_documents: list[str] | None = None,
        occurred_at: str | None = None,
    ) -> dict[str, Any]:
        """处理一份节点回执，返回处置结果。

        处置结果 action 取值：

        - ``advanced``：箱货推进到该节点；
        - ``duplicate``：回执重复到达，不重复推进；
        - ``review``：箱号相同但重量或单证与建档不一致，进入核对，节点不推进；
        - ``rejected``：节点跳跃或冻结箱被推进，回执被拒绝并记录。
        """
        unit = self._require_unit(unit_id)
        try:
            target = Stage(stage)
        except ValueError:
            raise ServiceError(f"未知节点{stage}") from None

        # 幂等：同一回执编号重复到达不产生任何效果
        dedup_key = receipt_id
        seen = self._receipts.get(dedup_key)
        if seen is not None:
            if seen != (unit_id, target):
                raise ServiceError(f"回执{receipt_id}指向了不同箱货或节点")
            return {"action": "duplicate", "unit_id": unit_id, "stage": stage}
        if dedup_key in self._rejected_receipts:
            # 此前已被拒（冻结或跳节点）；处置未变化前重复到达不重复记录
            return {"action": "duplicate", "unit_id": unit_id, "stage": stage}
        if unit.frozen:
            self._record_rejected_receipt(
                receipt_id, unit_id, target, source,
                f"箱货被冻结（{unit.freeze_reason}），节点不得推进",
                observed_weight_kg, observed_documents, occurred_at,
            )
            return {"action": "rejected", "unit_id": unit_id, "stage": stage,
                    "reason": "unit_frozen"}

        expected = next_stage(unit.stage)
        if target != expected:
            if target == unit.stage:
                # 不同回执编号但报告的是已达成节点：按重复处理，不重复推进
                return {"action": "duplicate", "unit_id": unit_id, "stage": stage}
            self._record_rejected_receipt(
                receipt_id, unit_id, target, source,
                f"节点跳跃：当前为{STAGE_LABELS[unit.stage]}，"
                f"下一节点应为{STAGE_LABELS[expected] if expected else '无'}",
                observed_weight_kg, observed_documents, occurred_at,
            )
            return {"action": "rejected", "unit_id": unit_id, "stage": stage,
                    "reason": "stage_out_of_order"}

        # 单证一致：同编号箱货的重量或单证发生变化，进入人工核对而不是直接推进
        discrepancies = self._find_discrepancies(unit, observed_weight_kg, observed_documents)
        if discrepancies:
            self._emit(
                "review_opened",
                {
                    "receipt_id": receipt_id,
                    "unit_id": unit_id,
                    "stage": target.value,
                    "source": source,
                    "discrepancies": discrepancies,
                    "observed_weight_kg": observed_weight_kg,
                    "observed_documents": observed_documents,
                    "occurred_at": occurred_at or self._clock(),
                },
            )
            return {"action": "review", "unit_id": unit_id, "stage": stage,
                    "discrepancies": discrepancies}

        self._commit_receipt(
            receipt_id, unit_id, target, source,
            observed_weight_kg, observed_documents, occurred_at,
        )
        return {"action": "advanced", "unit_id": unit_id, "stage": stage}

    def _commit_receipt(
        self, receipt_id: str, unit_id: str, target: Stage, source: str,
        observed_weight_kg: float | None,
        observed_documents: list[str] | None,
        occurred_at: str | None,
    ) -> None:
        self._emit(
            "receipt_confirmed",
            {
                "receipt_id": receipt_id,
                "unit_id": unit_id,
                "stage": target.value,
                "source": source,
                "observed_weight_kg": observed_weight_kg,
                "observed_documents": observed_documents,
                "occurred_at": occurred_at or self._clock(),
                "responsible_party": RESPONSIBLE_PARTY[target],
            },
        )

    def _record_rejected_receipt(
        self, receipt_id: str, unit_id: str, target: Stage, source: str,
        reason: str,
        observed_weight_kg: float | None,
        observed_documents: list[str] | None,
        occurred_at: str | None,
    ) -> None:
        self._emit(
            "receipt_rejected",
            {
                "receipt_id": receipt_id,
                "unit_id": unit_id,
                "stage": target.value,
                "source": source,
                "reason": reason,
                "observed_weight_kg": observed_weight_kg,
                "observed_documents": observed_documents,
                "occurred_at": occurred_at or self._clock(),
            },
        )

    @staticmethod
    def _find_discrepancies(
        unit: Unit,
        observed_weight_kg: float | None,
        observed_documents: list[str] | None,
    ) -> list[str]:
        discrepancies: list[str] = []
        if observed_weight_kg is not None and float(observed_weight_kg) != float(unit.weight_kg):
            discrepancies.append(
                f"重量不一致：建档{unit.weight_kg}kg，回执{observed_weight_kg}kg"
            )
        if observed_documents is not None and sorted(observed_documents) != sorted(unit.documents):
            missing = sorted(set(unit.documents) - set(observed_documents))
            extra = sorted(set(observed_documents) - set(unit.documents))
            detail = []
            if missing:
                detail.append(f"缺少单证{','.join(missing)}")
            if extra:
                detail.append(f"多出单证{','.join(extra)}")
            discrepancies.append("单证不一致：" + "；".join(detail))
        return discrepancies

    # ------------------------------------------------------------- 异常处置

    def flag_hazard(
        self, unit_id: str, train_id: str, source: str, reason: str,
        held_at_stage: str = Stage.CUSTOMS_RELEASED.value,
    ) -> None:
        """危险属性申报/查获：仅冻结涉事箱货，同一班列其他箱子不受影响。"""
        unit = self._require_unit(unit_id)
        self._emit(
            "hazard_flagged",
            {
                "unit_id": unit_id,
                "order_id": unit.order_id,
                "train_id": train_id,
                "source": source,
                "reason": reason,
                "held_at_stage": held_at_stage,
            },
        )

    def inspection_exception(
        self, unit_id: str, train_id: str, source: str, reason: str,
        held_at_stage: str = Stage.CUSTOMS_RELEASED.value,
    ) -> None:
        """查验异常（暂扣等）：仅冻结受影响箱货，整列其他箱子仍可运行。"""
        unit = self._require_unit(unit_id)
        self._emit(
            "inspection_exception",
            {
                "unit_id": unit_id,
                "order_id": unit.order_id,
                "train_id": train_id,
                "source": source,
                "reason": reason,
                "held_at_stage": held_at_stage,
            },
        )

    def resolve_exception(
        self,
        unit_id: str,
        resolution: str,
        release: bool,
        corrected_weight_kg: float | None = None,
        corrected_documents: list[str] | None = None,
    ) -> dict[str, Any]:
        """处置冻结或核对中的箱货。

        ``release`` 为真则解除冻结/核对，箱货停在原节点等待下一节点正常回执；
        重量/单证以 ``corrected_*`` 修订建档数据。暂扣不放行时仅记录处置结论。
        """
        unit = self._require_unit(unit_id)
        if not unit.frozen and not unit.under_review:
            raise ServiceError(f"箱货{unit_id}当前没有待处置的冻结或核对")
        event = self._emit(
            "exception_resolved",
            {
                "unit_id": unit_id,
                "resolution": resolution,
                "release": release,
                "corrected_weight_kg": corrected_weight_kg,
                "corrected_documents": corrected_documents,
            },
        )
        return {"action": "released" if release else "held", "unit_id": unit_id,
                "event_seq": event["seq"]}

    # ------------------------------------------------------- 拆分 / 合并

    def split_unit(
        self,
        parent_id: str,
        child_id: str,
        child_weight_kg: float,
        child_documents: list[str],
        reason: str,
    ) -> Unit:
        """把一只箱货的部分货量拆为新箱货；父子谱系串联，子箱初始在订单受理节点。"""
        parent = self._require_unit(parent_id)
        if child_id in self.units:
            raise ServiceError(f"箱货{child_id}已存在")
        if child_weight_kg <= 0 or child_weight_kg >= parent.weight_kg:
            raise ServiceError("拆分重量必须为正且小于母箱重量")
        self._emit(
            "unit_split",
            {
                "parent_id": parent_id,
                "child_id": child_id,
                "order_id": parent.order_id,
                "container_no": parent.container_no,
                "seal_no": parent.seal_no,
                "cargo_category": parent.cargo_category,
                "child_weight_kg": child_weight_kg,
                "child_documents": list(child_documents),
                "dangerous": parent.dangerous,
                "reason": reason,
            },
        )
        return self.units[child_id]

    def merge_units(
        self,
        from_ids: list[str],
        merged_id: str,
        container_no: str,
        seal_no: str,
        documents: list[str],
        reason: str,
    ) -> Unit:
        """把同订单的多只箱货合并为一只；被合并箱标记为不再活跃，谱系记录全部来源。"""
        if not from_ids:
            raise ServiceError("合并至少需要一只来源箱货")
        if merged_id in self.units:
            raise ServiceError(f"箱货{merged_id}已存在")
        sources = [self._require_unit(uid) for uid in from_ids]
        order_ids = {u.order_id for u in sources}
        if len(order_ids) != 1:
            raise ServiceError("只能合并同一订单下的箱货")
        if any(u.frozen or u.under_review for u in sources):
            raise ServiceError("冻结或核对中的箱货不能合并")
        stages = {u.stage for u in sources}
        if len(stages) != 1:
            raise ServiceError("只能合并处于同一节点的箱货")
        self._emit(
            "units_merged",
            {
                "from_ids": list(from_ids),
                "merged_id": merged_id,
                "order_id": next(iter(order_ids)),
                "container_no": container_no,
                "seal_no": seal_no,
                "cargo_category": sources[0].cargo_category,
                "merged_weight_kg": sum(u.weight_kg for u in sources),
                "documents": list(documents),
                "dangerous": any(u.dangerous for u in sources),
                "stage": sources[0].stage.value,
                "reason": reason,
            },
        )
        return self.units[merged_id]

    # --------------------------------------------------------- 改配 / 延误

    def reassign_unit(
        self,
        unit_id: str,
        to_train_id: str,
        railcar_no: str,
        reason: str,
        impact: str,
        revised_promise_start: str | None = None,
        revised_promise_end: str | None = None,
    ) -> None:
        """箱货暂扣后改走下一班：记录原班列、原因、对客户承诺的影响与新承诺区间。"""
        unit = self._require_unit(unit_id)
        if to_train_id not in self.trains:
            raise ServiceError(f"班列{to_train_id}不存在")
        if unit.train_id == to_train_id:
            raise ServiceError("目标班列与当前班列相同")
        revised_window = None
        if revised_promise_start is not None or revised_promise_end is not None:
            if not (revised_promise_start and revised_promise_end):
                raise ServiceError("修订承诺区间必须同时给出开始与结束日期")
            revised_window = {"start": revised_promise_start, "end": revised_promise_end}
        self._emit(
            "unit_reassigned",
            {
                "unit_id": unit_id,
                "order_id": unit.order_id,
                "from_train_id": unit.train_id,
                "to_train_id": to_train_id,
                "railcar_no": railcar_no,
                "reason": reason,
                "impact": impact,
                "revised_window": revised_window,
            },
        )

    def report_delay(
        self,
        train_id: str,
        reason: str,
        impact_scope: str,
        new_eta_start: str,
        new_eta_end: str,
    ) -> None:
        """登记班列延误及修订后的预计到达区间，影响全班列在途箱货的承诺解释。"""
        if train_id not in self.trains:
            raise ServiceError(f"班列{train_id}不存在")
        affected = [u.unit_id for u in self.units.values()
                    if u.train_id == train_id and u.active and u.stage != Stage.DELIVERED]
        self._emit(
            "delay_reported",
            {
                "train_id": train_id,
                "reason": reason,
                "impact_scope": impact_scope,
                "new_eta_window": {"start": new_eta_start, "end": new_eta_end},
                "affected_unit_ids": affected,
            },
        )

    def depart_train(self, train_id: str) -> None:
        """班列从南昌始发。"""
        if train_id not in self.trains:
            raise ServiceError(f"班列{train_id}不存在")
        self._emit("train_departed", {"train_id": train_id})

    def deliver_unit(self, unit_id: str, receipt_id: str, receiver_ref: str) -> None:
        """目的站交接：最后一段回执推进到交付并登记交接凭证。"""
        unit = self._require_unit(unit_id)
        result = self.ingest_receipt(
            receipt_id, unit_id, Stage.DELIVERED.value,
            source="目的站交接人员",
        )
        if result["action"] != "advanced":
            raise ServiceError(f"箱货{unit_id}未能交付：{result['action']}")
        self._emit(
            "delivery_confirmed",
            {"unit_id": unit_id, "order_id": unit.order_id,
             "receiver_ref": receiver_ref, "delivered_at": self._clock()},
        )

    # --------------------------------------------------------------- 查询

    def order_status(self, order_id: str) -> dict[str, Any]:
        """按客户订单解释当前去向、下一责任人与预计交付区间。

        任一箱货被冻结或核对，订单即标注受影响及原因；预计交付区间取班列 ETA
        （延误后取修订值）与订单承诺区间的对照，明确客户承诺是否受到影响。
        """
        order = self.orders.get(order_id)
        if order is None:
            raise ServiceError(f"订单{order_id}不存在")
        unit_views = []
        affected: list[str] = []
        eta_windows: list[TimeWindow] = []
        for unit in self.units.values():
            if unit.order_id != order_id or not unit.active:
                continue
            if unit.frozen or unit.under_review:
                cause = unit.freeze_reason or "重量/单证核对中"
                affected.append(f"{unit.unit_id}（{cause}）")
            if unit.train_id and unit.stage != Stage.DELIVERED:
                train = self.trains[unit.train_id]
                eta = self._current_eta(train)
                if eta is not None:
                    eta_windows.append(eta)
            unit_views.append(self._unit_view(unit))

        promised = order.revised_window or order.promised_window
        eta = self._merge_windows(eta_windows) if eta_windows else None
        all_delivered = bool(unit_views) and all(
            u["stage"] == Stage.DELIVERED.value for u in unit_views)
        if all_delivered:
            commitment = "订单已全部完成目的站交接"
            delivered_dates = sorted(
                u["delivered_at"][:10] for u in unit_views if u["delivered_at"])
            if delivered_dates:
                eta = TimeWindow(delivered_dates[0], delivered_dates[-1])
        elif eta and eta.end > promised.end:
            commitment = f"预计交付晚于承诺：承诺截止{promised.end}，预计{eta.end}"
        elif order.commitment_impact:
            commitment = order.commitment_impact
        else:
            commitment = "交付承诺暂无影响"

        return {
            "order_id": order_id,
            "customer": order.customer,
            "cargo_category": order.cargo_category,
            "promised_window": promised.as_dict(),
            "original_promised_window": order.promised_window.as_dict(),
            "estimated_delivery_window": eta.as_dict() if eta else None,
            "commitment": commitment,
            "affected": bool(affected),
            "affected_detail": affected,
            "units": unit_views,
        }

    def unit_trace(self, unit_id: str) -> dict[str, Any]:
        """返回一只箱货的完整谱系与节点轨迹（含拆分、合并、改配、冻结、核对）。"""
        unit = self._require_unit(unit_id)
        view = self._unit_view(unit)
        view["lineage"] = self._lineage(unit_id)
        return view

    def train_units(self, train_id: str) -> dict[str, Any]:
        """查看一班列车上各箱货状态，验证局部冻结时其他箱子仍在推进。"""
        if train_id not in self.trains:
            raise ServiceError(f"班列{train_id}不存在")
        train = self.trains[train_id]
        members = [self._unit_view(u) for u in self.units.values()
                   if u.train_id == train_id and u.active]
        return {
            "train_id": train_id,
            "eta_window": self._current_eta(train).as_dict()
            if self._current_eta(train) else train.eta_window.as_dict(),
            "departed": train.departed,
            "units": members,
        }

    # -------------------------------------------------------------- 内部

    def _require_unit(self, unit_id: str) -> Unit:
        unit = self.units.get(unit_id)
        if unit is None:
            raise ServiceError(f"箱货{unit_id}不存在")
        return unit

    def _unit_view(self, unit: Unit) -> dict[str, Any]:
        current_eta = None
        if unit.train_id and unit.stage != Stage.DELIVERED:
            eta = self._current_eta(self.trains[unit.train_id])
            current_eta = eta.as_dict() if eta else None
        return {
            "unit_id": unit.unit_id,
            "order_id": unit.order_id,
            "container_no": unit.container_no,
            "seal_no": unit.seal_no,
            "cargo_category": unit.cargo_category,
            "weight_kg": unit.weight_kg,
            "documents": list(unit.documents),
            "dangerous": unit.dangerous,
            "train_id": unit.train_id,
            "railcar_no": unit.railcar_no,
            "stage": unit.stage.value,
            "stage_label": STAGE_LABELS[unit.stage],
            "location": STAGE_LOCATIONS[unit.stage],
            "responsible_party": RESPONSIBLE_PARTY[unit.stage],
            "next_responsible_party": next_responsible(unit.stage),
            "frozen": unit.frozen,
            "freeze_reason": unit.freeze_reason,
            "frozen_by": unit.frozen_by,
            "under_review": unit.under_review,
            "estimated_delivery_window": current_eta,
            "delivered_at": unit.delivered_at,
            "receiver_ref": unit.receiver_ref,
            "parents": list(unit.parents),
            "active": unit.active,
        }

    def _lineage(self, unit_id: str) -> dict[str, Any]:
        """向上追溯全部母箱（合并箱可能有多个），向下查找由本箱拆出或合入的箱货。"""
        parents: list[str] = []
        stack = list(self.units[unit_id].parents)
        seen = set()
        while stack:
            ancestor = stack.pop()
            if ancestor in seen:
                continue
            seen.add(ancestor)
            parents.append(ancestor)
            stack.extend(self.units[ancestor].parents)
        children = [u.unit_id for u in self.units.values() if unit_id in u.parents]
        return {"parents": parents, "children": children}

    @staticmethod
    def _merge_windows(windows: list[TimeWindow]) -> TimeWindow:
        return TimeWindow(
            start=min(w.start for w in windows),
            end=max(w.end for w in windows),
        )

    def _current_eta(self, train: Train) -> TimeWindow | None:
        """取班列最近一次延误修订的 ETA；没有延误则用班列计划 ETA。"""
        return train.revised_eta or train.eta_window

    # ----------------------------------------------------------- 事件投影

    def _apply(self, event: dict[str, Any]) -> None:
        data = event["data"]
        getattr(self, f"_apply_{event['type']}")(data)

    def _apply_order_registered(self, d: dict[str, Any]) -> None:
        window = d["promised_window"]
        self.orders[d["order_id"]] = Order(
            order_id=d["order_id"],
            customer=d["customer"],
            cargo_category=d["cargo_category"],
            dangerous=d["dangerous"],
            promised_window=TimeWindow(window["start"], window["end"]),
        )

    def _apply_train_registered(self, d: dict[str, Any]) -> None:
        eta = d["eta_window"]
        self.trains[d["train_id"]] = Train(
            train_id=d["train_id"],
            origin=d["origin"],
            destination=d["destination"],
            planned_departure=d["planned_departure"],
            eta_window=TimeWindow(eta["start"], eta["end"]),
        )

    def _apply_unit_registered(self, d: dict[str, Any]) -> None:
        self.units[d["unit_id"]] = Unit(
            unit_id=d["unit_id"],
            order_id=d["order_id"],
            container_no=d["container_no"],
            seal_no=d["seal_no"],
            cargo_category=d["cargo_category"],
            weight_kg=d["weight_kg"],
            documents=tuple(d["documents"]),
            dangerous=d["dangerous"],
        )

    def _apply_unit_assigned(self, d: dict[str, Any]) -> None:
        unit = self.units[d["unit_id"]]
        unit.train_id = d["train_id"]
        unit.railcar_no = d["railcar_no"]
        unit.history.append({"event": "assigned", "train_id": d["train_id"],
                             "railcar_no": d["railcar_no"]})

    def _apply_receipt_confirmed(self, d: dict[str, Any]) -> None:
        unit = self.units[d["unit_id"]]
        stage = Stage(d["stage"])
        unit.stage = stage
        self._receipts[d["receipt_id"]] = (unit.unit_id, stage)
        unit.history.append({
            "event": "receipt",
            "receipt_id": d["receipt_id"],
            "stage": stage.value,
            "source": d["source"],
            "occurred_at": d["occurred_at"],
            "responsible_party": d["responsible_party"],
        })

    def _apply_receipt_rejected(self, d: dict[str, Any]) -> None:
        unit = self.units[d["unit_id"]]
        self._rejected_receipts[d["receipt_id"]] = unit.unit_id
        unit.history.append({"event": "receipt_rejected", **d})

    def _apply_review_opened(self, d: dict[str, Any]) -> None:
        unit = self.units[d["unit_id"]]
        target = Stage(d["stage"])
        unit.under_review = True
        unit.pending_review = d
        # 核对回执同样去重，确认后重传不会再开一个核对单
        self._receipts[d["receipt_id"]] = (unit.unit_id, target)
        unit.history.append({"event": "review_opened", **d})

    def _freeze(self, d: dict[str, Any], reason: str, by: str) -> None:
        unit = self.units[d["unit_id"]]
        unit.frozen = True
        unit.freeze_reason = reason
        unit.frozen_by = by
        unit.history.append({"event": "frozen", "reason": reason, "by": by,
                             "train_id": d["train_id"], "source": d["source"]})

    def _apply_hazard_flagged(self, d: dict[str, Any]) -> None:
        self._freeze(d, f"危险属性：{d['reason']}", d["source"])

    def _apply_inspection_exception(self, d: dict[str, Any]) -> None:
        self._freeze(d, f"查验异常：{d['reason']}", d["source"])

    def _apply_exception_resolved(self, d: dict[str, Any]) -> None:
        unit = self.units[d["unit_id"]]
        if d["release"]:
            if unit.under_review and unit.pending_review:
                # 核对放行：移除挂起回执，允许更正后的同名回执重新推进
                self._receipts.pop(unit.pending_review["receipt_id"], None)
            if unit.frozen:
                # 冻结放行：解除此前被拒回执登记，待处置环节结束后可重新到达
                for receipt_id, owner in list(self._rejected_receipts.items()):
                    if owner == unit.unit_id:
                        del self._rejected_receipts[receipt_id]
            unit.frozen = False
            unit.freeze_reason = None
            unit.frozen_by = None
            unit.under_review = False
            unit.pending_review = None
        if d.get("corrected_weight_kg") is not None:
            unit.weight_kg = d["corrected_weight_kg"]
        if d.get("corrected_documents") is not None:
            unit.documents = tuple(d["corrected_documents"])
        unit.history.append({"event": "exception_resolved", **d})

    def _apply_unit_split(self, d: dict[str, Any]) -> None:
        parent = self.units[d["parent_id"]]
        parent.weight_kg -= d["child_weight_kg"]
        child = Unit(
            unit_id=d["child_id"],
            order_id=d["order_id"],
            container_no=d["container_no"],
            seal_no=d["seal_no"],
            cargo_category=d["cargo_category"],
            weight_kg=d["child_weight_kg"],
            documents=tuple(d["child_documents"]),
            dangerous=d["dangerous"],
            parents=(d["parent_id"],),
        )
        self.units[d["child_id"]] = child
        parent.history.append({"event": "split_out", "child_id": d["child_id"],
                               "reason": d["reason"]})
        child.history.append({"event": "split_from", "parent_id": d["parent_id"],
                              "reason": d["reason"]})

    def _apply_units_merged(self, d: dict[str, Any]) -> None:
        for from_id in d["from_ids"]:
            source = self.units[from_id]
            source.active = False
            source.history.append({"event": "merged_into", "merged_id": d["merged_id"],
                                   "reason": d["reason"]})
        merged = Unit(
            unit_id=d["merged_id"],
            order_id=d["order_id"],
            container_no=d["container_no"],
            seal_no=d["seal_no"],
            cargo_category=d["cargo_category"],
            weight_kg=d["merged_weight_kg"],
            documents=tuple(d["documents"]),
            dangerous=d["dangerous"],
            stage=Stage(d["stage"]),
            parents=tuple(d["from_ids"]),
        )
        self.units[d["merged_id"]] = merged

    def _apply_unit_reassigned(self, d: dict[str, Any]) -> None:
        unit = self.units[d["unit_id"]]
        unit.train_id = d["to_train_id"]
        unit.railcar_no = d["railcar_no"]
        # 改走下一班：从口岸之前重新等车，已产生的口岸之后回执作废，需新旅程回执
        if STAGE_ORDER[unit.stage] > STAGE_ORDER[Stage.PORT_ARRIVED]:
            unit.stage = Stage.PORT_ARRIVED
        for receipt_id, (owner, stage) in list(self._receipts.items()):
            if owner == unit.unit_id and STAGE_ORDER[stage] > STAGE_ORDER[Stage.PORT_ARRIVED]:
                del self._receipts[receipt_id]
        order = self.orders[d["order_id"]]
        order.commitment_impact = d["impact"]
        if d.get("revised_window"):
            window = d["revised_window"]
            order.revised_window = TimeWindow(window["start"], window["end"])
        order.notes.append(f"箱货{d['unit_id']}由{d['from_train_id']}改配至"
                           f"{d['to_train_id']}：{d['reason']}")
        unit.history.append({"event": "reassigned", **d})

    def _apply_delay_reported(self, d: dict[str, Any]) -> None:
        train = self.trains[d["train_id"]]
        window = d["new_eta_window"]
        train.revised_eta = TimeWindow(window["start"], window["end"])

    def _apply_train_departed(self, d: dict[str, Any]) -> None:
        self.trains[d["train_id"]].departed = True

    def _apply_delivery_confirmed(self, d: dict[str, Any]) -> None:
        unit = self.units[d["unit_id"]]
        unit.delivered_at = d["delivered_at"]
        unit.receiver_ref = d["receiver_ref"]
        unit.history.append({"event": "delivered", "receiver_ref": d["receiver_ref"],
                             "delivered_at": d["delivered_at"]})
