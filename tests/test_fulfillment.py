"""货运履约服务测试：以南昌直达比什凯克首趟班列为场景。"""

import tempfile
import unittest
from pathlib import Path

from src.eventlog import EventStore
from src.model import Stage
from src.service import FulfillmentService, ServiceError

# 三类货的标准单证
DOCS_SOLAR = ["装箱单", "报关单", "太阳能组件原产地证"]
DOCS_CERAMIC = ["装箱单", "报关单", "陶瓷餐具商检单"]
DOCS_LAMP = ["装箱单", "报关单", "灯具符合性声明"]

# 正常旅程节点顺序（箱货已建档并编组后，从装箱回执开始）
JOURNEY = [
    ("R-STF", Stage.STUFFING, "货代"),
    ("R-GATE", Stage.GATE_IN, "班列运营方"),
    ("R-DEP", Stage.DEPARTED, "班列运营方"),
    ("R-PORT", Stage.PORT_ARRIVED, "铁路承运方"),
    ("R-CUST", Stage.CUSTOMS_RELEASED, "口岸查验人员"),
    ("R-TRANS", Stage.TRANSLOADED, "铁路承运方"),
    ("R-TRANSIT", Stage.IN_TRANSIT, "铁路承运方"),
    ("R-DEST", Stage.DEST_ARRIVED, "铁路承运方"),
]


def advance(service, unit_id, journey=JOURNEY, docs=None):
    """按顺序回传一路节点回执。"""
    for receipt_id, stage, source in journey:
        result = service.ingest_receipt(
            f"{receipt_id}-{unit_id}", unit_id, stage.value, source,
            observed_documents=docs,
        )
        assert result["action"] == "advanced", (unit_id, stage, result)
    return result


def build_service(path: Path):
    service = FulfillmentService(EventStore(path))
    service.restore()
    service.register_train("T8001", "2026-09-20", "2026-10-04", "2026-10-06")
    service.register_order("ORD-1001", "比什凯克光明商贸", "太阳能组件", False,
                           "2026-10-03", "2026-10-08")
    service.register_order("ORD-1002", "比什凯克餐饮用品公司", "陶瓷餐具", False,
                           "2026-10-03", "2026-10-08")
    service.register_order("ORD-1003", "中亚照明经销", "灯具", False,
                           "2026-10-03", "2026-10-08")
    service.register_unit("U-S1", "ORD-1001", "CSNU7000001", "SEAL1001",
                          "太阳能组件", 18500.0, DOCS_SOLAR)
    service.register_unit("U-C1", "ORD-1002", "CSNU7000002", "SEAL1002",
                          "陶瓷餐具", 12000.0, DOCS_CERAMIC)
    service.register_unit("U-L1", "ORD-1003", "CSNU7000003", "SEAL1003",
                          "灯具", 6400.0, DOCS_LAMP)
    for unit_id, railcar in (("U-S1", "C5280001"), ("U-C1", "C5280002"),
                             ("U-L1", "C5280003")):
        service.assign_unit(unit_id, "T8001", railcar)
    service.depart_train("T8001")
    return service


class HappyPathTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(Path(self.temp.name) / "events.jsonl")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_full_journey_to_bishkek_delivery(self) -> None:
        advance(self.service, "U-S1", docs=DOCS_SOLAR)
        self.service.deliver_unit("U-S1", "R-DEL-U-S1", "BIH-POD-7788")

        status = self.service.order_status("ORD-1001")
        unit = status["units"][0]
        self.assertEqual(unit["stage"], Stage.DELIVERED.value)
        self.assertEqual(unit["location"], "比什凯克目的站")
        self.assertEqual(unit["responsible_party"], "目的站交接人员")
        self.assertIsNone(unit["next_responsible_party"])
        self.assertEqual(unit["receiver_ref"], "BIH-POD-7788")
        self.assertFalse(status["affected"])
        self.assertEqual(status["commitment"], "订单已全部完成目的站交接")
        self.assertIsNotNone(status["estimated_delivery_window"])

    def test_next_responsible_party_moves_with_stage(self) -> None:
        self.service.ingest_receipt("R-STF-U-C1", "U-C1", Stage.STUFFING.value, "货代")
        view = self.service.unit_trace("U-C1")
        self.assertEqual(view["responsible_party"], "货代")
        self.assertEqual(view["next_responsible_party"], "班列运营方")


class DuplicateReceiptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(Path(self.temp.name) / "events.jsonl")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_same_receipt_arriving_twice_does_not_advance_twice(self) -> None:
        first = self.service.ingest_receipt(
            "R-STF-U-L1", "U-L1", Stage.STUFFING.value, "货代")
        second = self.service.ingest_receipt(
            "R-STF-U-L1", "U-L1", Stage.STUFFING.value, "货代")
        third = self.service.ingest_receipt(
            "R-STF-U-L1", "U-L1", Stage.STUFFING.value, "货代")
        self.assertEqual(first["action"], "advanced")
        self.assertEqual(second["action"], "duplicate")
        self.assertEqual(third["action"], "duplicate")
        view = self.service.unit_trace("U-L1")
        self.assertEqual(view["stage"], Stage.STUFFING.value)
        history = self.service.units["U-L1"].history
        self.assertEqual(
            [h["event"] for h in history].count("receipt"), 1)

    def test_receipt_id_reused_for_different_unit_is_rejected(self) -> None:
        self.service.ingest_receipt("RX", "U-L1", Stage.STUFFING.value, "货代")
        with self.assertRaisesRegex(ServiceError, "指向了不同箱货"):
            self.service.ingest_receipt("RX", "U-C1", Stage.STUFFING.value, "货代")


class DiscrepancyReviewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(Path(self.temp.name) / "events.jsonl")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_weight_change_opens_review_and_blocks_stage(self) -> None:
        result = self.service.ingest_receipt(
            "R-STF-U-S1", "U-S1", Stage.STUFFING.value, "货代",
            observed_weight_kg=17900.0, observed_documents=DOCS_SOLAR)
        self.assertEqual(result["action"], "review")
        self.assertTrue(any("重量不一致" in d for d in result["discrepancies"]))

        unit = self.service.units["U-S1"]
        self.assertTrue(unit.under_review)
        self.assertEqual(unit.stage, Stage.BOOKED)  # 节点不推进

        status = self.service.order_status("ORD-1001")
        self.assertTrue(status["affected"])
        self.assertIn("U-S1", status["affected_detail"][0])

        # 同一回执重复到达不会再开一张核对单
        again = self.service.ingest_receipt(
            "R-STF-U-S1", "U-S1", Stage.STUFFING.value, "货代",
            observed_weight_kg=17900.0, observed_documents=DOCS_SOLAR)
        self.assertEqual(again["action"], "duplicate")

        # 人工核对：以更正后的重量修订建档并放行，随后回执可正常推进
        self.service.resolve_exception(
            "U-S1", "货代复核装货记录，以实际过磅17900kg为准", True,
            corrected_weight_kg=17900.0)
        self.assertFalse(self.service.units["U-S1"].under_review)
        retried = self.service.ingest_receipt(
            "R-STF-U-S1", "U-S1", Stage.STUFFING.value, "货代",
            observed_weight_kg=17900.0, observed_documents=DOCS_SOLAR)
        self.assertEqual(retried["action"], "advanced")
        self.assertEqual(self.service.units["U-S1"].weight_kg, 17900.0)

    def test_document_change_opens_review(self) -> None:
        result = self.service.ingest_receipt(
            "R-STF-U-C1", "U-C1", Stage.STUFFING.value, "货代",
            observed_weight_kg=12000.0,
            observed_documents=["装箱单", "报关单"])  # 缺商检单
        self.assertEqual(result["action"], "review")
        self.assertTrue(any("缺少单证" in d for d in result["discrepancies"]))


class PartialFreezeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(Path(self.temp.name) / "events.jsonl")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_hazard_flag_freezes_only_flagged_unit(self) -> None:
        # 三只箱子都已到口岸
        for unit_id, docs in (("U-S1", DOCS_SOLAR), ("U-C1", DOCS_CERAMIC),
                              ("U-L1", DOCS_LAMP)):
            advance(self.service, unit_id,
                    journey=[j for j in JOURNEY
                             if j[1] in (Stage.STUFFING, Stage.GATE_IN,
                                         Stage.DEPARTED, Stage.PORT_ARRIVED)],
                    docs=docs)

        # 灯具箱被查获危险属性（含锂电池未申报），只冻结这一只
        self.service.flag_hazard(
            "U-L1", "T8001", "口岸查验人员",
            "灯具含锂电池未做危申报", held_at_stage=Stage.CUSTOMS_RELEASED.value)
        self.assertTrue(self.service.units["U-L1"].frozen)

        # 冻结箱的放行回执被拒绝，重复到达不重复记录
        rejected = self.service.ingest_receipt(
            "R-CUST-U-L1", "U-L1", Stage.CUSTOMS_RELEASED.value, "口岸查验人员")
        rejected_again = self.service.ingest_receipt(
            "R-CUST-U-L1", "U-L1", Stage.CUSTOMS_RELEASED.value, "口岸查验人员")
        self.assertEqual(rejected["action"], "rejected")
        self.assertEqual(rejected["reason"], "unit_frozen")
        self.assertEqual(rejected_again["action"], "duplicate")

        # 整列其他箱子继续完成查验、换装、在途
        advance(self.service, "U-S1",
                journey=[j for j in JOURNEY if j[1] in (
                    Stage.CUSTOMS_RELEASED, Stage.TRANSLOADED,
                    Stage.IN_TRANSIT, Stage.DEST_ARRIVED)],
                docs=DOCS_SOLAR)
        advance(self.service, "U-C1",
                journey=[j for j in JOURNEY if j[1] in (
                    Stage.CUSTOMS_RELEASED, Stage.TRANSLOADED,
                    Stage.IN_TRANSIT, Stage.DEST_ARRIVED)],
                docs=DOCS_CERAMIC)
        self.assertEqual(self.service.units["U-S1"].stage, Stage.DEST_ARRIVED)
        self.assertEqual(self.service.units["U-C1"].stage, Stage.DEST_ARRIVED)
        self.assertEqual(self.service.units["U-L1"].stage, Stage.PORT_ARRIVED)

        train_view = self.service.train_units("T8001")
        frozen_flag = {u["unit_id"]: u["frozen"] for u in train_view["units"]}
        self.assertEqual(frozen_flag, {"U-S1": False, "U-C1": False, "U-L1": True})

    def test_inspection_hold_then_release_resumes(self) -> None:
        advance(self.service, "U-C1",
                journey=[j for j in JOURNEY
                         if j[1] in (Stage.STUFFING, Stage.GATE_IN,
                                     Stage.DEPARTED, Stage.PORT_ARRIVED)],
                docs=DOCS_CERAMIC)
        self.service.inspection_exception(
            "U-C1", "T8001", "口岸查验人员", "暂扣待开箱查验木质包装")
        self.service.resolve_exception("U-C1", "开箱查验合格，放行", True)
        result = self.service.ingest_receipt(
            "R-CUST-U-C1", "U-C1", Stage.CUSTOMS_RELEASED.value, "口岸查验人员")
        self.assertEqual(result["action"], "advanced")
        self.assertFalse(self.service.units["U-C1"].frozen)


class StageOrderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(Path(self.temp.name) / "events.jsonl")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_stage_jump_is_rejected(self) -> None:
        # 刚建档（BOOKED），直接回传口岸到齐回执属于跳节点
        result = self.service.ingest_receipt(
            "R-PORT-U-S1", "U-S1", Stage.PORT_ARRIVED.value, "铁路承运方")
        self.assertEqual(result["action"], "rejected")
        self.assertEqual(result["reason"], "stage_out_of_order")
        self.assertEqual(self.service.units["U-S1"].stage, Stage.BOOKED)


class ReassignTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(Path(self.temp.name) / "events.jsonl")
        # 第二班（下一班）班列
        self.service.register_train("T8005", "2026-09-27", "2026-10-11", "2026-10-13")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_held_unit_reassigned_to_next_train_and_promise_revised(self) -> None:
        advance(self.service, "U-L1",
                journey=[j for j in JOURNEY
                         if j[1] in (Stage.STUFFING, Stage.GATE_IN,
                                     Stage.DEPARTED, Stage.PORT_ARRIVED)],
                docs=DOCS_LAMP)
        self.service.inspection_exception(
            "U-L1", "T8001", "口岸查验人员", "单证暂扣，赶不上本班换装")
        self.service.resolve_exception("U-L1", "补正备案，允许改走下一班", True)
        self.service.reassign_unit(
            "U-L1", "T8005", "C5280099",
            reason="口岸暂扣错过T8001换装",
            impact="改走T8005，交付推迟至10月11-13日",
            revised_promise_start="2026-10-10", revised_promise_end="2026-10-14")

        unit = self.service.units["U-L1"]
        self.assertEqual(unit.train_id, "T8005")
        self.assertEqual(unit.stage, Stage.PORT_ARRIVED)

        status = self.service.order_status("ORD-1003")
        self.assertEqual(status["promised_window"],
                         {"start": "2026-10-10", "end": "2026-10-14"})
        self.assertEqual(status["original_promised_window"],
                         {"start": "2026-10-03", "end": "2026-10-08"})
        self.assertIn("改走T8005", status["commitment"])

        # 新班列旅程可继续推进
        result = self.service.ingest_receipt(
            "R2-CUST-U-L1", "U-L1", Stage.CUSTOMS_RELEASED.value, "口岸查验人员")
        self.assertEqual(result["action"], "advanced")

        train_one = {u["unit_id"] for u in self.service.train_units("T8001")["units"]}
        train_next = {u["unit_id"] for u in self.service.train_units("T8005")["units"]}
        self.assertNotIn("U-L1", train_one)
        self.assertIn("U-L1", train_next)


class SplitMergeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(Path(self.temp.name) / "events.jsonl")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_split_then_merge_keeps_lineage(self) -> None:
        # 太阳能组件母箱拆出一箱小批量货
        child = self.service.split_unit(
            "U-S1", "U-S1B", 3500.0, DOCS_SOLAR, "客户要求分拨给两个收货人")
        self.assertEqual(self.service.units["U-S1"].weight_kg, 15000.0)
        self.assertEqual(child.parents, ("U-S1",))

        trace = self.service.unit_trace("U-S1B")
        self.assertEqual(trace["lineage"]["parents"], ["U-S1"])
        self.assertEqual(self.service.unit_trace("U-S1")["lineage"]["children"],
                         ["U-S1B"])

        # 两只陶瓷箱……这里用同一订单下两只箱演示合并：再建一只同订单箱
        self.service.register_unit("U-C2", "ORD-1002", "CSNU7000004", "SEAL1004",
                                   "陶瓷餐具", 4000.0, DOCS_CERAMIC)
        merged = self.service.merge_units(
            ["U-C1", "U-C2"], "U-CM1", "CSNU7000099", "SEAL1099",
            DOCS_CERAMIC, "目的站拼箱交付同一收货人")
        self.assertFalse(self.service.units["U-C1"].active)
        self.assertFalse(self.service.units["U-C2"].active)
        self.assertTrue(merged.active)
        self.assertEqual(merged.weight_kg, 16000.0)
        lineage = self.service.unit_trace("U-CM1")["lineage"]
        self.assertEqual(sorted(lineage["parents"]), ["U-C1", "U-C2"])

    def test_cannot_merge_frozen_unit(self) -> None:
        self.service.flag_hazard(
            "U-L1", "T8001", "口岸查验人员", "危险属性暂扣")
        with self.assertRaisesRegex(ServiceError, "冻结或核对中"):
            self.service.merge_units(["U-L1"], "U-X", "X", "X", [], "测试")


class DelayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(Path(self.temp.name) / "events.jsonl")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_delay_revises_eta_and_flags_commitment(self) -> None:
        advance(self.service, "U-S1", docs=DOCS_SOLAR)
        self.service.report_delay(
            "T8001", "境外区段调度限速", "全车在途箱",
            "2026-10-09", "2026-10-11")
        status = self.service.order_status("ORD-1001")
        self.assertEqual(status["estimated_delivery_window"],
                         {"start": "2026-10-09", "end": "2026-10-11"})
        self.assertIn("晚于承诺", status["commitment"])


class RestartReplayTest(unittest.TestCase):
    def test_state_and_inflight_exception_survive_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.jsonl"
            service = build_service(path)
            advance(service, "U-S1",
                    journey=[j for j in JOURNEY
                             if j[1] in (Stage.STUFFING, Stage.GATE_IN,
                                         Stage.DEPARTED, Stage.PORT_ARRIVED)],
                    docs=DOCS_SOLAR)
            service.inspection_exception(
                "U-S1", "T8001", "口岸查验人员", "暂扣：机检图像待复核")
            import json
            before_seq = max(json.loads(line)["seq"]
                             for line in path.read_text(encoding="utf-8").splitlines())

            # —— 模拟停机后重启：新实例重放日志 ——
            reopened = FulfillmentService(EventStore(path))
            replayed = reopened.restore()
            self.assertGreater(replayed, 5)
            unit = reopened.units["U-S1"]
            self.assertTrue(unit.frozen)
            self.assertEqual(unit.stage, Stage.PORT_ARRIVED)
            self.assertEqual(unit.train_id, "T8001")

            # 异常处置跨越停机继续推进
            reopened.resolve_exception("U-S1", "图像复核无异常，放行", True)
            result = reopened.ingest_receipt(
                "R-CUST-U-S1", "U-S1", Stage.CUSTOMS_RELEASED.value,
                "口岸查验人员")
            self.assertEqual(result["action"], "advanced")

            # 重启后新事件序号在历史最大值之后
            last_line = path.read_text(encoding="utf-8").splitlines()[-1]
            self.assertGreater(json.loads(last_line)["seq"], before_seq)

            # 订单解释仍可回答去向与下一责任人
            status = reopened.order_status("ORD-1001")
            self.assertEqual(status["units"][0]["stage"],
                             Stage.CUSTOMS_RELEASED.value)
            self.assertEqual(status["units"][0]["next_responsible_party"],
                             "铁路承运方")

    def test_corrupt_log_line_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.jsonl"
            service = build_service(path)
            with path.open("a", encoding="utf-8") as handle:
                handle.write("{不是合法JSON\n")
            reopened = FulfillmentService(EventStore(path))
            with self.assertRaisesRegex(ValueError, "无法解析"):
                reopened.restore()


if __name__ == "__main__":
    unittest.main()
