"""端到端演示：南昌直达比什凯克班列的履约跟踪。

运行：python3 -m src.demo
只使用内存临时目录，不连接任何外部系统。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from src.eventlog import EventStore
from src.model import STAGE_ORDER, Stage
from src.service import FulfillmentService

SOLAR_DOCS = ["装箱单", "报关单", "太阳能组件原产地证"]
CERAMIC_DOCS = ["装箱单", "报关单", "陶瓷餐具商检单"]
LAMP_DOCS = ["装箱单", "报关单", "灯具符合性声明"]

JOURNEY = [
    ("STF", Stage.STUFFING, "货代"),
    ("GATE", Stage.GATE_IN, "班列运营方"),
    ("DEP", Stage.DEPARTED, "班列运营方"),
    ("PORT", Stage.PORT_ARRIVED, "铁路承运方"),
    ("CUST", Stage.CUSTOMS_RELEASED, "口岸查验人员"),
    ("TRANS", Stage.TRANSLOADED, "铁路承运方"),
    ("TRANSIT", Stage.IN_TRANSIT, "铁路承运方"),
    ("DEST", Stage.DEST_ARRIVED, "铁路承运方"),
]


def advance(service: FulfillmentService, unit_id: str, docs: list[str],
            until: Stage | None = None) -> None:
    current_order = STAGE_ORDER[service.units[unit_id].stage]
    for code, stage, source in JOURNEY:
        if STAGE_ORDER[stage] <= current_order:
            continue  # 已达成的节点跳过，避免把历史回执当重复重放
        if until is not None and stage == until:
            break
        result = service.ingest_receipt(
            f"R-{code}-{unit_id}", unit_id, stage.value, source,
            observed_documents=docs)
        assert result["action"] == "advanced", result


def explain(service: FulfillmentService, order_id: str, title: str) -> None:
    status = service.order_status(order_id)
    print(f"\n=== {title}：{order_id}（{status['customer']}）===")
    eta = status["estimated_delivery_window"]
    print(f"承诺区间：{status['promised_window']}（原承诺 {status['original_promised_window']}）")
    print(f"预计交付：{eta or '尚无法预计'}")
    print(f"承诺影响：{status['commitment']}")
    if status["affected_detail"]:
        for detail in status["affected_detail"]:
            print(f"  ! 受影响箱货：{detail}")
    for unit in status["units"]:
        print(f"- {unit['unit_id']} 箱号{unit['container_no']} {unit['cargo_category']}"
              f" -> {unit['stage_label']}（{unit['location']}）"
              f" 当前责任：{unit['responsible_party']}"
              f" 下一责任：{unit['next_responsible_party'] or '已完结'}"
              f" 班列：{unit['train_id']}"
              f"{' [冻结]' if unit['frozen'] else ''}{' [核对中]' if unit['under_review'] else ''}")


def main() -> None:
    with tempfile.TemporaryDirectory() as temp:
        service = FulfillmentService(EventStore(Path(temp) / "events.jsonl"))
        service.restore()

        service.register_train("T8001", "2026-09-20", "2026-10-04", "2026-10-06")
        service.register_train("T8005", "2026-09-27", "2026-10-11", "2026-10-13")
        service.register_order("ORD-1001", "比什凯克光明商贸", "太阳能组件", False,
                               "2026-10-03", "2026-10-08")
        service.register_order("ORD-1003", "中亚照明经销", "灯具", False,
                               "2026-10-03", "2026-10-08")
        service.register_unit("U-S1", "ORD-1001", "CSNU7000001", "SEAL1001",
                              "太阳能组件", 18500.0, SOLAR_DOCS)
        service.register_unit("U-L1", "ORD-1003", "CSNU7000003", "SEAL1003",
                              "灯具", 6400.0, LAMP_DOCS)
        service.assign_unit("U-S1", "T8001", "C5280001")
        service.assign_unit("U-L1", "T8001", "C5280003")
        service.depart_train("T8001")

        # 两只箱子一路到口岸
        advance(service, "U-S1", SOLAR_DOCS, until=Stage.CUSTOMS_RELEASED)
        advance(service, "U-L1", LAMP_DOCS, until=Stage.CUSTOMS_RELEASED)

        # 灯具箱被暂扣（查验异常）：只冻结 U-L1
        service.inspection_exception(
            "U-L1", "T8001", "口岸查验人员", "灯具加施封条号与单证不符，暂扣待补正")
        explain(service, "ORD-1003", "暂扣发生")

        # 太阳能组件箱随整列继续运行直至比什凯克交付
        advance(service, "U-S1", SOLAR_DOCS)
        service.deliver_unit("U-S1", "R-DEL-U-S1", "BIH-POD-7788")
        explain(service, "ORD-1001", "整列继续运行")

        # 灯具箱补正后放行，改走下一班 T8005，承诺区间修订
        service.resolve_exception("U-L1", "封条号补正一致，放行改走下一班", True)
        service.reassign_unit(
            "U-L1", "T8005", "C5280099",
            reason="口岸暂扣错过T8001换装",
            impact="改走T8005，交付推迟至10月11-13日",
            revised_promise_start="2026-10-10", revised_promise_end="2026-10-14")
        advance(service, "U-L1", LAMP_DOCS)
        service.deliver_unit("U-L1", "R2-DEL-U-L1", "BIH-POD-7901")
        explain(service, "ORD-1003", "改配下一班后交付")


if __name__ == "__main__":
    main()
