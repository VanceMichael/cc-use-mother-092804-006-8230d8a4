"""履约领域模型：跨境班列节点、责任主体与箱货状态。

节点按南昌始发→霍尔果斯口岸查验换装→境外区段→比什凯克交付的顺序排列，
每个节点对应唯一的下一责任主体，保证运营人员随时能回答"现在谁负责"。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class Stage(enum.Enum):
    """一箱货从订单受理到目的站交付的有序节点。"""

    BOOKED = "booked"                       # 订单受理
    STUFFING = "stuffing"                   # 货代完成装箱
    GATE_IN = "gate_in"                     # 进站编组
    DEPARTED = "departed"                   # 南昌始发
    PORT_ARRIVED = "port_arrived"           # 抵达霍尔果斯口岸
    CUSTOMS_RELEASED = "customs_released"   # 口岸查验放行
    TRANSLOADED = "transloaded"             # 换装完成（准轨转宽轨）
    IN_TRANSIT = "in_transit"               # 境外铁路区段在途
    DEST_ARRIVED = "dest_arrived"           # 抵达比什凯克目的站
    DELIVERED = "delivered"                 # 目的站交接交付


STAGE_ORDER: dict[Stage, int] = {stage: index for index, stage in enumerate(Stage)}

STAGE_LABELS: dict[Stage, str] = {
    Stage.BOOKED: "订单已受理",
    Stage.STUFFING: "货代装箱完成",
    Stage.GATE_IN: "进站编组完成",
    Stage.DEPARTED: "南昌国际陆港始发",
    Stage.PORT_ARRIVED: "抵达霍尔果斯口岸",
    Stage.CUSTOMS_RELEASED: "口岸查验放行",
    Stage.TRANSLOADED: "口岸换装完成",
    Stage.IN_TRANSIT: "境外铁路区段在途",
    Stage.DEST_ARRIVED: "抵达比什凯克目的站",
    Stage.DELIVERED: "目的站交接完成",
}

STAGE_LOCATIONS: dict[Stage, str] = {
    Stage.BOOKED: "南昌国际陆港",
    Stage.STUFFING: "货代装箱点",
    Stage.GATE_IN: "南昌国际陆港",
    Stage.DEPARTED: "南昌国际陆港",
    Stage.PORT_ARRIVED: "霍尔果斯口岸",
    Stage.CUSTOMS_RELEASED: "霍尔果斯口岸",
    Stage.TRANSLOADED: "霍尔果斯口岸",
    Stage.IN_TRANSIT: "境外铁路区段",
    Stage.DEST_ARRIVED: "比什凯克目的站",
    Stage.DELIVERED: "比什凯克目的站",
}

# 各节点的负责主体；当前节点完成后，"下一责任人"取下一节点的责任主体。
RESPONSIBLE_PARTY: dict[Stage, str] = {
    Stage.BOOKED: "班列运营方",
    Stage.STUFFING: "货代",
    Stage.GATE_IN: "班列运营方",
    Stage.DEPARTED: "班列运营方",
    Stage.PORT_ARRIVED: "铁路承运方",
    Stage.CUSTOMS_RELEASED: "口岸查验人员",
    Stage.TRANSLOADED: "铁路承运方",
    Stage.IN_TRANSIT: "铁路承运方",
    Stage.DEST_ARRIVED: "铁路承运方",
    Stage.DELIVERED: "目的站交接人员",
}


def next_stage(stage: Stage) -> Stage | None:
    """返回紧邻的下一节点，交付后没有下一节点。"""
    index = STAGE_ORDER[stage]
    ordered = list(Stage)
    if index + 1 >= len(ordered):
        return None
    return ordered[index + 1]


def next_responsible(stage: Stage) -> str | None:
    """当前节点之后由谁接手。"""
    following = next_stage(stage)
    if following is None:
        return None
    return RESPONSIBLE_PARTY[following]


@dataclass
class TimeWindow:
    """预计或承诺的时间区间，统一使用 YYYY-MM-DD 字符串以便字典序比较。"""

    start: str
    end: str

    def as_dict(self) -> dict[str, str]:
        return {"start": self.start, "end": self.end}


@dataclass
class Unit:
    """一只可独立跟踪的箱货：箱号、货类、重量单证、所属订单与当前节点。"""

    unit_id: str
    order_id: str
    container_no: str
    seal_no: str
    cargo_category: str
    weight_kg: float
    documents: tuple[str, ...]
    dangerous: bool = False
    train_id: str | None = None
    railcar_no: str | None = None
    stage: Stage = Stage.BOOKED
    frozen: bool = False
    freeze_reason: str | None = None
    frozen_by: str | None = None
    under_review: bool = False
    pending_review: dict[str, Any] | None = None
    parents: tuple[str, ...] = ()
    active: bool = True
    delivered_at: str | None = None
    receiver_ref: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Order:
    """客户订单及其交付承诺；改配等处置可给出修订后的承诺区间。"""

    order_id: str
    customer: str
    cargo_category: str
    dangerous: bool
    promised_window: TimeWindow
    revised_window: TimeWindow | None = None
    commitment_impact: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class Train:
    """一班班列及其预计到达比什凯克的时间区间。"""

    train_id: str
    origin: str
    destination: str
    planned_departure: str
    eta_window: TimeWindow
    revised_eta: TimeWindow | None = None
    departed: bool = False
