"""货运履约领域模型：箱货谱系、节点回执、局部冻结与交付承诺。

核心规则（对应 docs/domain-rules.md）：

- 箱货身份：container_no 是箱的物理身份，lineage 串联拆分/合并/换班；
  箱货（ShipmentUnit）始终归属于某个客户订单。
- 节点顺序：节点只能按 NODE_ORDER 推进；回执重复到达不重复推进；
  同一编号但重量或单证变化时进入核对，不推进节点。
- 局部冻结：危险属性或查验异常只冻结受影响箱货，整列其他箱子继续运行。
- 谱系：同一箱货的拆分、合并、换班（改走下一班）都有记录可追溯。
- 交付承诺：延误、改配、异常处置都重算预计交付区间，
  并能按客户订单解释当前去向、下一责任人和承诺状态。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Iterable, Optional

# ---------------------------------------------------------------------------
# 节点与责任主体
# ---------------------------------------------------------------------------


class Node(str, Enum):
    """班列履约节点，顺序即列内排序。"""

    BOOKED = "booked"                        # 接单（货代）
    LOADED = "loaded"                        # 装箱完成（货代回传装箱信息）
    DEPARTED = "departed"                    # 南昌发运（铁路）
    BORDER_IN = "border_in"                  # 抵达霍尔果斯口岸（铁路/口岸）
    INSPECTED = "inspected"                  # 查验放行（口岸查验人员）
    TRANSHIPPED = "transshipped"             # 换装宽轨（铁路/口岸）
    DEPARTED_BORDER = "departed_border"      # 口岸出境（铁路）
    ARRIVED = "arrived"                      # 抵达比什凯克目的站（铁路）
    DELIVERED = "delivered"                  # 目的站交接完成（目的站交接人员）

    @property
    def label(self) -> str:
        return NODE_LABELS[self]

    @property
    def owner(self) -> str:
        return NODE_OWNERS[self]


NODE_ORDER: list[Node] = list(Node)

NODE_LABELS: dict[Node, str] = {
    Node.BOOKED: "已接单",
    Node.LOADED: "已装箱",
    Node.DEPARTED: "南昌发运",
    Node.BORDER_IN: "到达霍尔果斯",
    Node.INSPECTED: "查验放行",
    Node.TRANSHIPPED: "换装宽轨",
    Node.DEPARTED_BORDER: "口岸出境",
    Node.ARRIVED: "到达比什凯克",
    Node.DELIVERED: "目的站交接完成",
}

# 节点对应的当前责任人（该节点回执由谁回传，到达后此棒在谁手上）
NODE_OWNERS: dict[Node, str] = {
    Node.BOOKED: "货代",
    Node.LOADED: "货代",
    Node.DEPARTED: "铁路承运方",
    Node.BORDER_IN: "铁路承运方",
    Node.INSPECTED: "口岸查验人员",
    Node.TRANSHIPPED: "铁路承运方",
    Node.DEPARTED_BORDER: "铁路承运方",
    Node.ARRIVED: "铁路承运方",
    Node.DELIVERED: "目的站交接人员",
}

DEFAULT_ETA_DAYS = 14


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class JourneyError(ValueError):
    """履约规则冲突。"""


class DuplicateReceipt(JourneyError):
    """回执已处理过或该节点已到达：重复到达，不得重复推进。"""


class OutOfOrder(JourneyError):
    """节点顺序冲突：前置节点未到达。"""


class UnitFrozen(JourneyError):
    """箱货处于冻结状态，任何节点都不能推进。"""


class ReconciliationNeeded(JourneyError):
    """编号相同但重量/单证变化，进入人工核对。"""


# ---------------------------------------------------------------------------
# 值对象
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Doc:
    """随箱单证：类型 + 编号 + 版本；版本变化即单证不一致。"""

    kind: str
    number: str
    version: int = 1


@dataclass(frozen=True)
class Receipt:
    """货代/口岸/铁路/目的站在不同时间回传的节点回执。"""

    receipt_no: str
    container_no: str
    node: Node
    reported_at: datetime
    weight_kg: Optional[float] = None
    docs: tuple[Doc, ...] = ()
    source_party: str = ""
    remark: str = ""

    def content_key(self) -> tuple:
        """重量与单证指纹：同编号回执内容变化据此识别。"""
        docs_key = tuple(sorted((d.kind, d.number, d.version) for d in self.docs))
        weight_key = None if self.weight_kg is None else round(float(self.weight_kg), 3)
        return (weight_key, docs_key)


@dataclass(frozen=True)
class FreezeRecord:
    """冻结只作用于单个箱货；整列其他箱子不受影响。"""

    reason_code: str          # dangerous_goods / inspection_hold / customs_hold
    detail: str
    since: datetime
    case_no: str              # 异常处置工单
    owner: str = "口岸查验人员"
    resolved_at: Optional[datetime] = None
    resolution: str = ""

    @property
    def resolved(self) -> bool:
        return self.resolved_at is not None


@dataclass(frozen=True)
class LineageEvent:
    """拆分 / 合并 / 换班 / 延误谱系记录。"""

    kind: str                 # split_out / split_in / merge_in / merged_out / reassign / delay
    at: datetime
    detail: str
    related_unit: str = ""
    train_no: str = ""


@dataclass(frozen=True)
class DelayRecord:
    """延误或改配导致承诺重算的留痕。"""

    at: datetime
    delay_days: int
    reason: str
    train_no: str = ""


# ---------------------------------------------------------------------------
# 箱货与订单
# ---------------------------------------------------------------------------


@dataclass
class ShipmentUnit:
    """一只集装箱承载的箱货，始终归属一个客户订单。"""

    unit_no: str
    container_no: str
    order_id: str
    cargo_type: str
    weight_kg: float
    docs: tuple[Doc, ...]
    train_no: str
    planned_at: datetime                              # 装载计划时间
    node: Node = Node.BOOKED
    node_history: dict[Node, datetime] = field(default_factory=dict)
    receipt_nos: set[str] = field(default_factory=set)
    # 每个节点首次回执的内容指纹：之后同节点回执必须一致，否则进核对
    node_content: dict[Node, tuple] = field(default_factory=dict)
    freezes: list[FreezeRecord] = field(default_factory=list)
    lineage: list[LineageEvent] = field(default_factory=list)
    delays: list[DelayRecord] = field(default_factory=list)
    eta_min: Optional[datetime] = None
    eta_max: Optional[datetime] = None
    delivered_at: Optional[datetime] = None
    active: bool = True                               # 合并退役后置为 False

    def __post_init__(self) -> None:
        self.node_history[Node.BOOKED] = self.planned_at
        if self.eta_min is None or self.eta_max is None:
            self.eta_min = self.planned_at + timedelta(days=DEFAULT_ETA_DAYS - 2)
            self.eta_max = self.planned_at + timedelta(days=DEFAULT_ETA_DAYS + 2)

    @property
    def frozen(self) -> bool:
        return any(not f.resolved for f in self.freezes)

    @property
    def open_freeze(self) -> Optional[FreezeRecord]:
        return next((f for f in self.freezes if not f.resolved), None)


@dataclass
class Order:
    """客户订单：一个订单可对应多只箱子，承诺对订单生效。"""

    order_id: str
    customer: str
    promise_min: datetime
    promise_max: datetime
    units: dict[str, ShipmentUnit] = field(default_factory=dict)

    def active_units(self) -> list[ShipmentUnit]:
        return [u for u in self.units.values() if u.active]


# ---------------------------------------------------------------------------
# 履约服务（纯内存决策；持久化与停机恢复见 store.AppService）
# ---------------------------------------------------------------------------


class JourneyService:
    def __init__(self) -> None:
        self.orders: dict[str, Order] = {}
        self.units: dict[str, ShipmentUnit] = {}              # unit_no -> unit
        self.containers: dict[str, str] = {}                  # container_no -> unit_no
        self.trains: dict[str, set[str]] = {}                 # train_no -> {unit_no}
        self.processed_receipts: dict[str, str] = {}          # receipt_no -> unit_no

    # ---- 建单与装载计划 ----

    def book_order(
        self,
        order_id: str,
        customer: str,
        promise_min: datetime,
        promise_max: datetime,
    ) -> Order:
        if order_id in self.orders:
            raise JourneyError(f"订单已存在: {order_id}")
        if promise_min > promise_max:
            raise JourneyError("交付承诺区间无效")
        order = Order(order_id, customer, promise_min, promise_max)
        self.orders[order_id] = order
        return order

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
    ) -> ShipmentUnit:
        """装载计划：为订单保存箱号、货类、重量、单证与铁路区段（班列）。"""
        order = self.orders.get(order_id)
        if order is None:
            raise JourneyError(f"订单不存在: {order_id}")
        if unit_no in self.units:
            raise JourneyError(f"箱货编号已存在: {unit_no}")
        if container_no in self.containers:
            raise JourneyError(f"箱号已配箱: {container_no}")
        docs = tuple(docs)
        if not docs:
            raise JourneyError("箱货必须至少随附一份单证")
        unit = ShipmentUnit(
            unit_no=unit_no,
            container_no=container_no,
            order_id=order_id,
            cargo_type=cargo_type,
            weight_kg=float(weight_kg),
            docs=docs,
            train_no=train_no,
            planned_at=planned_at,
        )
        order.units[unit_no] = unit
        self.units[unit_no] = unit
        self.containers[container_no] = unit_no
        self.trains.setdefault(train_no, set()).add(unit_no)
        return unit

    # ---- 回执处理：幂等 / 核对 / 顺序 / 冻结 ----

    def apply_receipt(self, receipt: Receipt) -> ShipmentUnit:
        """处理节点回执。判定顺序：

        1. 回执编号已处理过：内容一致 -> 重复丢弃；内容变了 -> 进核对。
        2. 节点已经到达（另一张同节点回执）：内容一致 -> 重复丢弃；
           重量/单证与首次回执不符 -> 进核对。
        3. 箱货冻结中 -> 拒绝推进（局部冻结，处置走 freeze/resolve）。
        4. 不是紧邻的下一节点 -> 节点顺序冲突。
        5. 与登记重量/单证不符 -> 进核对。
        任何核对/拒绝路径都不改状态、不重复推进。
        """
        unit = self._require_unit(receipt.container_no, active_only=True)

        if receipt.receipt_no in self.processed_receipts:
            bound_unit = self.processed_receipts[receipt.receipt_no]
            if bound_unit != unit.unit_no:
                raise ReconciliationNeeded(
                    f"回执 {receipt.receipt_no} 已归属箱货 {bound_unit}，"
                    "不能重复用于其他箱号，进入核对"
                )
            self._assert_same_content(unit, receipt, same_receipt_no=True)
            raise DuplicateReceipt(
                f"回执 {receipt.receipt_no} 已处理，重复回传不得重复推进"
            )

        node = receipt.node
        if node == Node.BOOKED:
            raise OutOfOrder("接单节点由装载计划建立，不接受回执")

        current_idx = NODE_ORDER.index(unit.node)
        target_idx = NODE_ORDER.index(node)

        if target_idx <= current_idx:
            # 同节点的另一张回执：与该节点首次回执比对
            self._assert_same_content(unit, receipt, same_receipt_no=False)
            raise DuplicateReceipt(f"{node.label} 节点已到达，重复节点回执忽略")

        expected = NODE_ORDER[current_idx + 1]
        if node != expected:
            raise OutOfOrder(
                f"节点顺序冲突：当前 {unit.node.label}，下一节点应为 {expected.label}，"
                f"收到 {node.label}"
            )

        if unit.frozen:
            hold = unit.open_freeze
            raise UnitFrozen(
                f"箱货 {unit.container_no} 因「{hold.detail}」冻结（工单 {hold.case_no}），"
                f"暂不能推进到 {node.label}"
            )

        self._check_against_registration(unit, receipt)

        self.processed_receipts[receipt.receipt_no] = unit.unit_no
        unit.node = node
        unit.node_history[node] = receipt.reported_at
        unit.receipt_nos.add(receipt.receipt_no)
        unit.node_content[node] = receipt.content_key()
        if node == Node.DELIVERED:
            unit.delivered_at = receipt.reported_at
        return unit

    @staticmethod
    def _assert_same_content(
        unit: ShipmentUnit, receipt: Receipt, same_receipt_no: bool
    ) -> None:
        """重复回执内容比对：编号相同但重量或单证变化 -> 核对。"""
        baseline = unit.node_content.get(receipt.node)
        if baseline is not None and receipt.content_key() != baseline:
            raise ReconciliationNeeded(
                f"回执 {receipt.receipt_no} 与 {receipt.node.label} 节点首次申报"
                "重量/单证不一致，进入人工核对（不推进节点）"
            )
        if same_receipt_no and baseline is None:
            # 编号已处理但找不到节点指纹（如换班回退后）：仍按重复处理
            return

    @staticmethod
    def _check_against_registration(unit: ShipmentUnit, receipt: Receipt) -> None:
        """新节点回执与装载计划登记的重量/单证核对。"""
        discrepancies: list[str] = []
        if receipt.weight_kg is not None and round(float(receipt.weight_kg), 3) != round(unit.weight_kg, 3):
            discrepancies.append(
                f"重量 {receipt.weight_kg}kg 与登记 {unit.weight_kg}kg 不一致"
            )
        if receipt.docs:
            registered = {(d.kind, d.number): d.version for d in unit.docs}
            for doc in receipt.docs:
                seen = registered.get((doc.kind, doc.number))
                if seen is None:
                    discrepancies.append(f"出现未登记单证 {doc.kind}:{doc.number}")
                elif seen != doc.version:
                    discrepancies.append(
                        f"单证 {doc.kind}:{doc.number} 版本 {doc.version} 与登记版本 {seen} 不一致"
                    )
        if discrepancies:
            raise ReconciliationNeeded("；".join(discrepancies))

    # ---- 局部冻结与异常处置 ----

    def freeze_unit(
        self,
        container_no: str,
        reason_code: str,
        detail: str,
        since: datetime,
        case_no: str,
        owner: str = "口岸查验人员",
    ) -> FreezeRecord:
        """危险属性或查验异常：只冻结这一只箱子，整列其他箱子照常运行。"""
        unit = self._require_unit(container_no, active_only=True)
        if unit.frozen:
            raise JourneyError(f"箱货 {unit.container_no} 已处于冻结中")
        if unit.node == Node.DELIVERED:
            raise JourneyError("已交付箱货不能冻结")
        record = FreezeRecord(
            reason_code=reason_code,
            detail=detail,
            since=since,
            case_no=case_no,
            owner=owner,
        )
        unit.freezes.append(record)
        return record

    def resolve_freeze(
        self, container_no: str, resolved_at: datetime, resolution: str
    ) -> FreezeRecord:
        """异常处置完成并放行：解除该箱冻结，节点可继续推进。"""
        unit = self._require_unit(container_no, active_only=True)
        hold = unit.open_freeze
        if hold is None:
            raise JourneyError(f"箱货 {unit.container_no} 没有待处置冻结")
        updated = replace(hold, resolved_at=resolved_at, resolution=resolution)
        unit.freezes[-1] = updated
        return updated

    # ---- 拆分 / 合并 / 换班谱系 ----

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
    ) -> ShipmentUnit:
        """口岸处置：原箱中一部分货拆出装入新箱，仍属同一客户订单。

        典型场景：一箱中部分货物被查验暂扣，其余货继续走原班列；
        拆出的新箱由调用方决定冻结或改配，谱系双向串联。
        """
        source = self._require_unit(source_container_no)
        moved_weight_kg = float(moved_weight_kg)
        if moved_weight_kg <= 0 or moved_weight_kg >= source.weight_kg:
            raise JourneyError("拆出重量必须介于 0 与原箱重量之间")
        if new_container_no in self.containers:
            raise JourneyError(f"箱号已配箱: {new_container_no}")
        if new_unit_no in self.units:
            raise JourneyError(f"箱货编号已存在: {new_unit_no}")

        source.weight_kg = round(source.weight_kg - moved_weight_kg, 3)
        child = ShipmentUnit(
            unit_no=new_unit_no,
            container_no=new_container_no,
            order_id=source.order_id,
            cargo_type=cargo_type,
            weight_kg=moved_weight_kg,
            docs=tuple(docs),
            train_no=train_no,
            planned_at=at,
            node=source.node,
        )
        child.node_history = dict(source.node_history)
        child.node_content = dict(source.node_content)
        child.eta_min, child.eta_max = source.eta_min, source.eta_max

        source.lineage.append(
            LineageEvent(
                kind="split_out",
                at=at,
                detail=f"拆出 {moved_weight_kg}kg {cargo_type} 至 {new_container_no}",
                related_unit=new_unit_no,
                train_no=train_no,
            )
        )
        child.lineage.append(
            LineageEvent(
                kind="split_in",
                at=at,
                detail=f"由 {source.container_no} 拆入 {moved_weight_kg}kg",
                related_unit=source.unit_no,
                train_no=source.train_no,
            )
        )

        self.orders[source.order_id].units[new_unit_no] = child
        self.units[new_unit_no] = child
        self.containers[new_container_no] = new_unit_no
        self.trains.setdefault(train_no, set()).add(new_unit_no)
        return child

    def merge_units(
        self,
        source_container_no: str,
        target_container_no: str,
        at: datetime,
    ) -> ShipmentUnit:
        """合并：源箱货并入同订单目标箱，源箱退役，谱系串联。"""
        source = self._require_unit(source_container_no)
        target = self._require_unit(target_container_no)
        if source.order_id != target.order_id:
            raise JourneyError("只能合并同一客户订单的箱货")
        if source.unit_no == target.unit_no:
            raise JourneyError("不能并入自身")
        if source.frozen or target.frozen:
            raise JourneyError("冻结中的箱货不能合并")
        if source.node != target.node:
            raise JourneyError("节点位置不同的箱货不能合并")

        target.weight_kg = round(target.weight_kg + source.weight_kg, 3)
        merged_docs = {(d.kind, d.number, d.version): d for d in (*target.docs, *source.docs)}
        target.docs = tuple(merged_docs.values())
        target.lineage.append(
            LineageEvent(
                kind="merge_in",
                at=at,
                detail=f"并入 {source.container_no} 的 {source.weight_kg}kg {source.cargo_type}",
                related_unit=source.unit_no,
                train_no=target.train_no,
            )
        )
        source.active = False
        source.lineage.append(
            LineageEvent(
                kind="merged_out",
                at=at,
                detail=f"整箱并入 {target.container_no}",
                related_unit=target.unit_no,
                train_no=target.train_no,
            )
        )
        self.trains.get(source.train_no, set()).discard(source.unit_no)
        return target

    def reassign_to_next_train(
        self,
        container_no: str,
        next_train_no: str,
        at: datetime,
        delay_days: int,
        reason: str,
    ) -> ShipmentUnit:
        """暂扣后改走下一班：记换班谱系，节点回退重走，顺延承诺，解除冻结。

        已出境的箱不能改走国内下一班。
        """
        unit = self._require_unit(container_no, active_only=True)
        if unit.node in (Node.DEPARTED_BORDER, Node.ARRIVED, Node.DELIVERED):
            raise JourneyError("已出境箱货不能改走国内下一班")
        if delay_days < 0:
            raise JourneyError("延误天数不能为负")
        if next_train_no == unit.train_no:
            raise JourneyError("新班列编号与当前班列相同")

        old_train = unit.train_no
        rollback_to = (
            Node.BORDER_IN
            if unit.node in (Node.INSPECTED, Node.TRANSHIPPED)
            else Node.LOADED
        )
        old_node = unit.node

        self.trains.get(old_train, set()).discard(unit.unit_no)
        self.trains.setdefault(next_train_no, set()).add(unit.unit_no)
        unit.train_no = next_train_no
        unit.node = rollback_to
        # 回退点之后的首次回执指纹作废，需在新班列上重新取得；历史时间保留
        unit.node_content = {
            n: c for n, c in unit.node_content.items()
            if NODE_ORDER.index(n) <= NODE_ORDER.index(rollback_to)
        }
        unit.eta_min += timedelta(days=delay_days)
        unit.eta_max += timedelta(days=delay_days)
        unit.delays.append(
            DelayRecord(at=at, delay_days=delay_days, reason=reason, train_no=next_train_no)
        )
        unit.lineage.append(
            LineageEvent(
                kind="reassign",
                at=at,
                detail=(
                    f"{reason}：{old_train} 改走 {next_train_no}，"
                    f"节点由 {old_node.label} 回退至 {rollback_to.label} 重走，"
                    f"承诺顺延 {delay_days} 天"
                ),
                train_no=next_train_no,
            )
        )

        hold = unit.open_freeze
        if hold is not None:
            unit.freezes[-1] = replace(
                hold, resolved_at=at, resolution=f"异常处置完成，改走 {next_train_no}"
            )
        return unit

    # ---- 延误 ----

    def record_delay(
        self, container_no: str, at: datetime, delay_days: int, reason: str
    ) -> ShipmentUnit:
        """口岸拥堵、换装积压等延误：顺延预计交付区间，冻结状态不变。"""
        unit = self._require_unit(container_no, active_only=True)
        if delay_days <= 0:
            raise JourneyError("延误天数必须为正")
        unit.eta_min += timedelta(days=delay_days)
        unit.eta_max += timedelta(days=delay_days)
        unit.delays.append(
            DelayRecord(at=at, delay_days=delay_days, reason=reason, train_no=unit.train_no)
        )
        unit.lineage.append(
            LineageEvent(
                kind="delay",
                at=at,
                detail=f"{reason}，预计交付顺延 {delay_days} 天",
                train_no=unit.train_no,
            )
        )
        return unit

    # ---- 查询与解释 ----

    def train_status(self, train_no: str) -> dict[str, list[ShipmentUnit]]:
        """整列视角：冻结箱与可运行箱分组，证明局部冻结不影响其他箱。"""
        result: dict[str, list[ShipmentUnit]] = {"running": [], "frozen": [], "delivered": []}
        for unit_no in self.trains.get(train_no, set()):
            unit = self.units[unit_no]
            if not unit.active:
                continue
            if unit.node == Node.DELIVERED:
                result["delivered"].append(unit)
            elif unit.frozen:
                result["frozen"].append(unit)
            else:
                result["running"].append(unit)
        return result

    def explain_order(self, order_id: str, at: datetime) -> dict:
        """按客户订单解释：当前去向、下一责任人、预计交付区间与承诺影响。"""
        order = self.orders.get(order_id)
        if order is None:
            raise JourneyError(f"订单不存在: {order_id}")
        units = order.active_units()
        if not units:
            raise JourneyError(f"订单 {order_id} 已无在运箱货")

        unit_views = []
        eta_min = max(u.eta_min for u in units)
        eta_max = max(u.eta_max for u in units)
        for unit in sorted(units, key=lambda u: u.unit_no):
            next_node = NODE_ORDER[NODE_ORDER.index(unit.node) + 1] if unit.node != Node.DELIVERED else None
            view = {
                "unit_no": unit.unit_no,
                "container_no": unit.container_no,
                "cargo_type": unit.cargo_type,
                "train_no": unit.train_no,
                "node": unit.node.label,
                "node_code": unit.node.value,
                "location": self._location_of(unit),
                "next_owner": None,
                "next_node": next_node.label if next_node else None,
                "frozen": unit.frozen,
                "freeze": None,
                "eta": [unit.eta_min.isoformat(), unit.eta_max.isoformat()],
                "delivered_at": unit.delivered_at.isoformat() if unit.delivered_at else None,
            }
            if unit.frozen:
                hold = unit.open_freeze
                view["freeze"] = {
                    "case_no": hold.case_no,
                    "reason_code": hold.reason_code,
                    "reason": hold.detail,
                    "owner": hold.owner,
                    "since": hold.since.isoformat(),
                }
                view["next_owner"] = hold.owner
            elif next_node is not None:
                view["next_owner"] = next_node.owner
            unit_views.append(view)

        all_delivered = all(u.node == Node.DELIVERED for u in units)
        return {
            "order_id": order.order_id,
            "customer": order.customer,
            "at": at.isoformat(),
            "units": unit_views,
            "any_frozen": any(u.frozen for u in units),
            "promise": {
                "promise_min": order.promise_min.isoformat(),
                "promise_max": order.promise_max.isoformat(),
                "eta_min": eta_min.isoformat(),
                "eta_max": eta_max.isoformat(),
                "at_risk": eta_max > order.promise_max,
                "breached": at > order.promise_max and not all_delivered,
                "delivered": all_delivered,
            },
            "summary": self._summarize(order, units, eta_min, eta_max, all_delivered),
        }

    @staticmethod
    def _location_of(unit: ShipmentUnit) -> str:
        node = unit.node
        if node == Node.DEPARTED:
            return "南昌—霍尔果斯境内途中"
        if node in (Node.BOOKED, Node.LOADED):
            return "南昌国际陆港"
        if node in (Node.BORDER_IN, Node.INSPECTED, Node.TRANSHIPPED, Node.DEPARTED_BORDER):
            return "霍尔果斯口岸"
        if node == Node.ARRIVED:
            return "比什凯克目的站（待交接）"
        return "比什凯克（已交付）"

    @staticmethod
    def _summarize(
        order: Order,
        units: list[ShipmentUnit],
        eta_min: datetime,
        eta_max: datetime,
        all_delivered: bool,
    ) -> str:
        frozen = [u for u in units if u.frozen]
        reassigned = [
            u
            for u in units
            if u.node != Node.DELIVERED and any(e.kind == "reassign" for e in u.lineage)
        ]
        delivered = [u for u in units if u.node == Node.DELIVERED]
        parts = [f"订单 {order.order_id}（{order.customer}）共 {len(units)} 箱"]
        if all_delivered:
            parts.append("已全部完成目的站交接")
            return "；".join(parts)
        if delivered:
            parts.append(f"{len(delivered)} 箱已交付")
        if frozen:
            hold = frozen[0].open_freeze
            boxes = "、".join(u.container_no for u in frozen)
            parts.append(f"{boxes} 因「{hold.detail}」暂扣，下一责任方 {hold.owner}（工单 {hold.case_no}）")
            running = [u for u in units if not u.frozen and u.node != Node.DELIVERED and u not in reassigned]
            if running:
                parts.append(f"其余 {len(running)} 箱不受影响、继续运行")
        elif reassigned:
            boxes = "、".join(u.container_no for u in reassigned)
            head = reassigned[0]
            next_idx = NODE_ORDER.index(head.node) + 1
            next_owner = NODE_OWNERS[NODE_ORDER[next_idx]] if next_idx < len(NODE_ORDER) else None
            parts.append(
                f"{boxes} 因异常处置改走下一班 {head.train_no}，"
                f"当前在 {head.node.label} 重走"
                + (f"，下一责任方 {next_owner}" if next_owner else "")
            )
        else:
            nodes = sorted({u.node.label for u in units if u.node != Node.DELIVERED})
            parts.append(f"当前位于{'/'.join(nodes)}")
        if eta_max > order.promise_max:
            parts.append(
                f"预计交付 {eta_min.date()}~{eta_max.date()}，晚于承诺上限 {order.promise_max.date()}"
            )
        else:
            parts.append(f"预计交付 {eta_min.date()}~{eta_max.date()}，在承诺区间内")
        return "；".join(parts)

    # ---- 内部 ----

    def _require_unit(self, container_no: str, active_only: bool = False) -> ShipmentUnit:
        unit_no = self.containers.get(container_no)
        if unit_no is None:
            raise JourneyError(f"未知箱号: {container_no}")
        unit = self.units[unit_no]
        if active_only and not unit.active:
            raise JourneyError(f"箱货 {container_no} 已随合并退役")
        return unit
