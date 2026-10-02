"""货运履约服务测试：以首趟南昌直达比什凯克班列为场景。

场景：订单 ORD-NC-BISHKEK-01 三只箱子装太阳能组件、陶瓷餐具、灯具，
货代/铁路/口岸/目的站在不同时间回传回执；灯具箱在口岸被查出带电
附件暂扣，拆分后改走下一班，其余箱子继续运行并按期交付。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from src.journey import (
    Doc,
    DuplicateReceipt,
    JourneyError,
    Node,
    OutOfOrder,
    ReconciliationNeeded,
    Receipt,
    UnitFrozen,
)
from src.store import open_app

PLAN = datetime(2026, 9, 1, 9, 0)


def docs() -> tuple[Doc, ...]:
    return (
        Doc("报关单", "CUS-7701"),
        Doc("装箱单", "PKL-7701"),
        Doc("运单", "SMGS-7701"),
    )


class FirstTrainScenarioTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = open_app(Path(self.tmp.name) / "journey.jsonl")
        self.app.book_order(
            "ORD-NC-001",
            "中亚光能贸易",
            PLAN + timedelta(days=11),
            PLAN + timedelta(days=18),
            at=PLAN,
        )
        containers = [
            ("U-SOLAR", "CBHU1000001", "太阳能组件", 18000.0, "1"),
            ("U-CERAMIC", "CBHU1000002", "陶瓷餐具", 20000.0, "2"),
            ("U-LAMP", "CBHU1000003", "灯具", 12000.0, "3"),
        ]
        for unit_no, container, cargo, weight, seq in containers:
            self.app.plan_loading(
                unit_no=unit_no,
                order_id="ORD-NC-001",
                container_no=container,
                cargo_type=cargo,
                weight_kg=weight,
                docs=(
                    Doc("报关单", f"CUS-770{seq}"),
                    Doc("装箱单", f"PKL-770{seq}"),
                    Doc("运单", "SMGS-80201"),
                ),
                train_no="X8021",
                planned_at=PLAN,
            )

    # ---- 辅助 ----

    def send(self, container: str, node: Node, day: int, no: str, **kw) -> None:
        self.app.receive_receipt(
            Receipt(
                receipt_no=no,
                container_no=container,
                node=node,
                reported_at=PLAN + timedelta(days=day, hours=8),
                source_party=kw.pop("source_party", ""),
                **kw,
            )
        )

    def move_normal_container_to_border(self, container: str, tag: str) -> None:
        self.send(container, Node.LOADED, 1, f"R-LOAD-{tag}", source_party="货代")
        self.send(container, Node.DEPARTED, 1, f"R-DEP-{tag}", source_party="铁路承运方")
        self.send(container, Node.BORDER_IN, 5, f"R-BIN-{tag}", source_party="铁路承运方")

    # ---- 主流程 ----

    def test_full_journey_with_partial_freeze_split_and_next_train(self) -> None:
        solar, ceramic, lamp = "CBHU1000001", "CBHU1000002", "CBHU1000003"
        for c, tag in ((solar, "S"), (ceramic, "C"), (lamp, "L")):
            self.move_normal_container_to_border(c, tag)

        # 第 6 天：灯具箱被查出含疑似锂电附件，危险属性暂扣，只冻这一只
        self.app.freeze_unit(
            lamp,
            reason_code="dangerous_goods",
            detail="灯具夹带未申报锂电附件，疑似危险属性",
            since=PLAN + timedelta(days=6),
            case_no="CASE-2026-015",
            owner="口岸查验人员",
        )

        # 冻结箱任何节点都不能推进
        with self.assertRaises(UnitFrozen):
            self.send(lamp, Node.INSPECTED, 6, "R-INSP-L", source_party="口岸查验人员")

        # 整列其他箱子继续查验、换装、出境
        status = self.app.train_status("X8021")
        self.assertEqual(set(status["frozen"]), {lamp})
        self.assertEqual(set(status["running"]), {solar, ceramic})

        for c, tag in ((solar, "S"), (ceramic, "C")):
            self.send(c, Node.INSPECTED, 6, f"R-INSP-{tag}", source_party="口岸查验人员")
            self.send(c, Node.TRANSHIPPED, 7, f"R-TR-{tag}", source_party="铁路承运方")
            self.send(c, Node.DEPARTED_BORDER, 7, f"R-DBO-{tag}", source_party="铁路承运方")

        # 第 8 天查验结论：2000kg 锂电附件暂扣待许可，其余灯具放行
        self.app.split_unit(
            source_container_no=lamp,
            new_unit_no="U-BATTERY",
            new_container_no="CBHU1000004",
            cargo_type="灯具锂电附件",
            moved_weight_kg=2000.0,
            docs=(Doc("暂扣清单", "HLD-015"), Doc("运单", "SMGS-80201")),
            train_no="X8021",
            at=PLAN + timedelta(days=8),
        )
        self.app.resolve_freeze(
            lamp, PLAN + timedelta(days=8), "暂扣锂电附件 2000kg，其余灯具放行"
        )
        # 拆出的附件箱继续冻结等待许可，许可到后改走下一班 X8023
        battery = "CBHU1000004"
        self.app.freeze_unit(
            battery,
            reason_code="customs_hold",
            detail="锂电附件待补危包证与许可",
            since=PLAN + timedelta(days=8),
            case_no="CASE-2026-015",
            owner="海关",
        )
        self.app.reassign_to_next_train(
            battery,
            next_train_no="X8023",
            at=PLAN + timedelta(days=10),
            delay_days=7,
            reason="暂扣许可办结后改走下一班",
        )

        # 灯具原箱继续完成出境前节点（放行后可推进）
        for node, day, no, party in (
            (Node.INSPECTED, 8, "R-INSP-L2", "口岸查验人员"),
            (Node.TRANSHIPPED, 9, "R-TR-L2", "铁路承运方"),
            (Node.DEPARTED_BORDER, 9, "R-DBO-L2", "铁路承运方"),
        ):
            self.send(lamp, node, day, no, source_party=party)

        # 陶瓷餐具遇换装积压延误 2 天，仍在承诺内
        self.app.record_delay(
            ceramic, PLAN + timedelta(days=9), delay_days=2, reason="霍尔果斯换装积压"
        )

        # 三只正常箱子抵达并交付
        for c, tag in ((solar, "S"), (ceramic, "C"), (lamp, "L2")):
            self.send(c, Node.ARRIVED, 13, f"R-ARR-{tag}", source_party="铁路承运方")
            self.send(c, Node.DELIVERED, 14, f"R-DLV-{tag}", source_party="目的站交接人员")

        view = self.app.explain_order("ORD-NC-001", PLAN + timedelta(days=14, hours=12))
        by_container = {u["container_no"]: u for u in view["units"]}

        self.assertTrue(by_container[solar]["delivered_at"])
        self.assertTrue(by_container[ceramic]["delivered_at"])
        self.assertTrue(by_container[lamp]["delivered_at"])

        # 改配箱：在新班列上从“已装箱”重走，谱系可追溯，承诺顺延 7 天
        bat = by_container[battery]
        self.assertEqual(bat["train_no"], "X8023")
        self.assertEqual(bat["node_code"], "loaded")
        self.assertFalse(bat["frozen"])
        self.assertEqual(bat["next_owner"], "铁路承运方")
        self.assertTrue(view["any_frozen"] is False)
        self.assertTrue(view["promise"]["at_risk"])  # 顺延 7 天后最晚 ETA 超承诺上限
        self.assertIn("改走下一班 X8023", view["summary"])

        # 谱系串联：原箱 split_out、附件箱 split_in + reassign
        j = self.app.journey
        lamp_unit = j.units["U-LAMP"]
        bat_unit = j.units["U-BATTERY"]
        self.assertEqual(lamp_unit.weight_kg, 10000.0)
        kinds = {e.kind for e in lamp_unit.lineage}
        self.assertIn("split_out", kinds)
        self.assertEqual(
            [e.kind for e in bat_unit.lineage if e.kind in ("split_in", "reassign")],
            ["split_in", "reassign"],
        )

    # ---- 幂等与核对 ----

    def test_duplicate_receipt_does_not_advance_twice(self) -> None:
        solar = "CBHU1000001"
        self.move_normal_container_to_border(solar, "S")
        unit = self.app.journey.units["U-SOLAR"]
        history_size = len(unit.node_history)

        # 同编号回执再次到达
        with self.assertRaises(DuplicateReceipt):
            self.send(solar, Node.BORDER_IN, 5, "R-BIN-S", source_party="铁路承运方")
        self.assertEqual(len(unit.node_history), history_size)

        # 同节点另一编号、内容一致：仍属重复，不推进
        with self.assertRaises(DuplicateReceipt):
            self.send(solar, Node.BORDER_IN, 5, "R-BIN-S-COPY", source_party="铁路承运方")
        self.assertEqual(unit.node, Node.BORDER_IN)

        # 重启后重复回执同样被挡（见 RestartTest），这里继续正常推进
        self.send(solar, Node.INSPECTED, 6, "R-INSP-S", source_party="口岸查验人员")
        self.assertEqual(unit.node, Node.INSPECTED)

    def test_same_number_changed_weight_enters_reconciliation(self) -> None:
        solar = "CBHU1000001"
        self.send(solar, Node.LOADED, 1, "R-LOAD-S", source_party="货代")
        self.send(solar, Node.DEPARTED, 1, "R-DEP-S", source_party="铁路承运方")

        # 重量与登记不符：进核对、不推进、回执编号不算消费
        with self.assertRaisesRegex(ReconciliationNeeded, "重量"):
            self.send(
                solar, Node.BORDER_IN, 5, "R-BIN-S",
                weight_kg=17999.0, docs=(Doc("报关单", "CUS-7701"),),
                source_party="铁路承运方",
            )
        unit = self.app.journey.units["U-SOLAR"]
        self.assertEqual(unit.node, Node.DEPARTED)

        # 同一回执编号用正确内容重发即可正常推进
        self.send(
            solar, Node.BORDER_IN, 5, "R-BIN-S",
            weight_kg=18000.0,
            docs=(Doc("报关单", "CUS-7701"), Doc("装箱单", "PKL-7701"), Doc("运单", "SMGS-80201")),
            source_party="铁路承运方",
        )
        self.assertEqual(unit.node, Node.BORDER_IN)

        # 已到达节点再来一张重量变化的回执：编号相同内容变了 → 核对
        with self.assertRaisesRegex(ReconciliationNeeded, "人工核对"):
            self.send(
                solar, Node.BORDER_IN, 5, "R-BIN-S2",
                weight_kg=18001.0, docs=(), source_party="口岸查验人员",
            )

    def test_changed_document_version_enters_reconciliation(self) -> None:
        solar = "CBHU1000001"
        self.send(solar, Node.LOADED, 1, "R-LOAD-S", source_party="货代")
        self.send(solar, Node.DEPARTED, 1, "R-DEP-S", source_party="铁路承运方")
        with self.assertRaisesRegex(ReconciliationNeeded, "版本 2"):
            self.send(
                solar, Node.BORDER_IN, 5, "R-BIN-S",
                weight_kg=18000.0,
                docs=(Doc("报关单", "CUS-7701", version=2),),
                source_party="铁路承运方",
            )
        with self.assertRaisesRegex(ReconciliationNeeded, "未登记单证"):
            self.send(
                solar, Node.BORDER_IN, 5, "R-BIN-S",
                weight_kg=18000.0,
                docs=(Doc("商检单", "CIQ-9"),),
                source_party="铁路承运方",
            )

    def test_out_of_order_receipt_rejected(self) -> None:
        solar = "CBHU1000001"
        # 未装箱先报发运
        with self.assertRaises(OutOfOrder):
            self.send(solar, Node.DEPARTED, 1, "R-DEP-X", source_party="铁路承运方")
        self.send(solar, Node.LOADED, 1, "R-LOAD-S", source_party="货代")
        # 跳过口岸节点直接报到达比什凯克
        with self.assertRaises(OutOfOrder):
            self.send(solar, Node.ARRIVED, 13, "R-ARR-X", source_party="铁路承运方")

    def test_failed_command_writes_no_event(self) -> None:
        log = Path(self.tmp.name) / "journey.jsonl"
        before = log.read_text(encoding="utf-8").count("\n")
        with self.assertRaises(JourneyError):
            self.send("CBHU1000001", Node.ARRIVED, 13, "R-BAD", source_party="铁路承运方")
        after = log.read_text(encoding="utf-8").count("\n")
        self.assertEqual(before, after)

    # ---- 合并 ----

    def test_merge_same_order_units(self) -> None:
        self.app.book_order(
            "ORD-NC-002", "比什凯克瓷光商行",
            PLAN + timedelta(days=11), PLAN + timedelta(days=18), at=PLAN,
        )
        self.app.plan_loading(
            "U-A", "ORD-NC-002", "CBHU2000001", "陶瓷餐具", 9000.0,
            (Doc("报关单", "CUS-A"),), "X8021", PLAN,
        )
        self.app.plan_loading(
            "U-B", "ORD-NC-002", "CBHU2000002", "陶瓷餐具", 8000.0,
            (Doc("报关单", "CUS-B"),), "X8021", PLAN,
        )
        for c in ("CBHU2000001", "CBHU2000002"):
            self.send(c, Node.LOADED, 1, f"R-LOAD-{c[-1:]}", source_party="货代")

        self.app.merge_units("CBHU2000001", "CBHU2000002", PLAN + timedelta(days=2))
        target = self.app.journey.units["U-B"]
        source = self.app.journey.units["U-A"]
        self.assertEqual(target.weight_kg, 17000.0)
        self.assertFalse(source.active)
        view = self.app.explain_order("ORD-NC-002", PLAN + timedelta(days=2))
        self.assertEqual([u["container_no"] for u in view["units"]], ["CBHU2000002"])
        with self.assertRaises(JourneyError):
            self.send("CBHU2000001", Node.DEPARTED, 2, "R-DEP-A", source_party="铁路承运方")


class RestartTest(unittest.TestCase):
    """停机恢复：延误、改配、异常处置跨越重启继续推进。"""

    def test_state_rebuilt_from_event_log_and_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "journey.jsonl"
            app = open_app(log)
            app.book_order(
                "ORD-NC-001", "中亚光能贸易",
                PLAN + timedelta(days=11), PLAN + timedelta(days=18), at=PLAN,
            )
            app.plan_loading(
                "U-SOLAR", "ORD-NC-001", "CBHU1000001", "太阳能组件", 18000.0,
                (Doc("报关单", "CUS-1"), Doc("运单", "SMGS-80201")),
                "X8021", PLAN,
            )

            def r(container, node, day, no):
                app.receive_receipt(
                    Receipt(
                        receipt_no=no, container_no=container, node=node,
                        reported_at=PLAN + timedelta(days=day),
                    )
                )

            r("CBHU1000001", Node.LOADED, 1, "R-1")
            r("CBHU1000001", Node.DEPARTED, 1, "R-2")
            r("CBHU1000001", Node.BORDER_IN, 5, "R-3")
            app.freeze_unit(
                "CBHU1000001", "inspection_hold", "布标信息待核",
                PLAN + timedelta(days=6), "CASE-9",
            )
            app.record_delay(
                "CBHU1000001", PLAN + timedelta(days=6), 1, "查验等待"
            )
            events_before = log.read_text(encoding="utf-8").count("\n")

            # ---- 模拟停机后重启 ----
            app2 = open_app(log)
            unit = app2.journey.units["U-SOLAR"]
            self.assertEqual(unit.node, Node.BORDER_IN)
            self.assertTrue(unit.frozen)
            self.assertEqual(unit.open_freeze.case_no, "CASE-9")
            self.assertEqual(len(unit.delays), 1)
            self.assertNotIn("CBHU1000001", app2.train_status("X8021")["running"])
            self.assertIn("CBHU1000001", app2.train_status("X8021")["frozen"])

            # 已处理回执在重启后仍然幂等
            with self.assertRaises(DuplicateReceipt):
                r2 = app2.receive_receipt(
                    Receipt(
                        receipt_no="R-3", container_no="CBHU1000001",
                        node=Node.BORDER_IN, reported_at=PLAN + timedelta(days=5),
                    )
                )

            # 处置完成，继续推进剩余节点直至交付
            app2.resolve_freeze("CBHU1000001", PLAN + timedelta(days=7), "布标相符，放行")
            for node, day, no in (
                (Node.INSPECTED, 7, "R-4"),
                (Node.TRANSHIPPED, 8, "R-5"),
                (Node.DEPARTED_BORDER, 8, "R-6"),
                (Node.ARRIVED, 14, "R-7"),
                (Node.DELIVERED, 15, "R-8"),
            ):
                app2.receive_receipt(
                    Receipt(
                        receipt_no=no, container_no="CBHU1000001", node=node,
                        reported_at=PLAN + timedelta(days=day),
                    )
                )
            self.assertEqual(unit.node, Node.DELIVERED)
            self.assertTrue(unit.delivered_at)

            # 再重启一次，终态稳定，日志没有任何重复写入
            app3 = open_app(log)
            self.assertEqual(
                app3.journey.units["U-SOLAR"].node, Node.DELIVERED
            )
            self.assertEqual(
                log.read_text(encoding="utf-8").count("\n"), events_before + 6
            )

    def test_corrupt_log_fails_loudly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "journey.jsonl"
            log.write_text('{"broken": true}\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                open_app(log)


if __name__ == "__main__":
    unittest.main()
