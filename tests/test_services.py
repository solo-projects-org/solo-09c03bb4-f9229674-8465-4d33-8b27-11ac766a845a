"""服务层领域逻辑测试。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app import services  # noqa: E402
from app.csv_parser import decode_bytes, parse_report_csv  # noqa: E402


def report(form, rid, bottle, lab, batch):
    return {"form_id": form, "report_id": rid, "bottle_no": bottle,
            "lab": lab, "batch_id": batch}


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.app = create_app({"TESTING": True, "DB_PATH": self.db_path})
        self.ctx = self.app.app_context()
        self.ctx.push()
        from app.db import get_db
        self.conn = get_db()
        services.import_forms(self.conn, "F-1\nF-2\nF-3\nF-4")
        self.conn.commit()

    def tearDown(self):
        self.ctx.pop()
        self.tmp.cleanup()


class PairingRuleTests(ServiceTestBase):
    def test_valid_pair_requires_different_bottle_and_lab(self):
        # 同瓶同实验室 -> 不能配对
        r = services.import_reports(self.conn, [
            report("F-1", "R1", "B1", "LAB-A", "BAT-1"),
            report("F-1", "R2", "B1", "LAB-A", "BAT-1"),
        ])
        self.assertEqual(len(r["inserted"]), 2)
        self.assertIsNone(services.active_pairing(self.conn, "F-1"))

        # 同瓶不同实验室 -> 仍不能配对
        r = services.import_reports(self.conn, [
            report("F-1", "R3", "B1", "LAB-B", "BAT-2"),
        ])
        self.assertIsNone(services.active_pairing(self.conn, "F-1"))

        # 不同瓶不同实验室 -> 配对成功，采信最早可配的两份
        r = services.import_reports(self.conn, [
            report("F-1", "R4", "B2", "LAB-B", "BAT-2"),
        ])
        pairing = services.active_pairing(self.conn, "F-1")
        self.assertIsNotNone(pairing)
        # R1(B1,LAB-A) 与 R4(B2,LAB-B) 是最早满足条件的组合
        self.assertEqual({pairing["report_a_id"], pairing["report_b_id"]},
                         {"R1", "R4"})

    def test_rejected_rows(self):
        r = services.import_reports(self.conn, [
            report("F-1", "R1", "B1", "LAB-A", "BAT-1"),
            report("F-1", "R1", "B2", "LAB-B", "BAT-1"),          # 文件内重复
            report("F-9", "R9", "B1", "LAB-A", "BAT-1"),           # 名单外
            {"form_id": "F-1", "report_id": "R10", "bottle_no": "",
             "lab": "LAB-A", "batch_id": "BAT-1"},                 # 缺字段
        ])
        self.assertEqual(len(r["inserted"]), 1)
        self.assertEqual(len(r["rejected"]), 3)

        # 已入档报告号再次续传 -> 拒收
        r = services.import_reports(self.conn, [
            report("F-2", "R1", "B9", "LAB-Z", "BAT-9"),
        ])
        self.assertEqual(len(r["inserted"]), 0)
        self.assertIn("已入档", r["rejected"][0]["reason"])


class InvalidationTests(ServiceTestBase):
    def _setup_two_forms(self):
        services.import_reports(self.conn, [
            report("F-1", "R1", "B1", "LAB-A", "BAT-1"),
            report("F-1", "R2", "B2", "LAB-B", "BAT-1"),
            report("F-2", "R3", "B3", "LAB-A", "BAT-1"),
            report("F-2", "R4", "B4", "LAB-B", "BAT-2"),
        ])
        self.assertIsNotNone(services.active_pairing(self.conn, "F-1"))
        self.assertIsNotNone(services.active_pairing(self.conn, "F-2"))

    def test_invalidate_rebuild_keep_survivor(self):
        """作废换证：F-1 两份报告全失效 -> 待补；补齐后幸存者规则重建。"""
        self._setup_two_forms()
        out = services.invalidate_batch(self.conn, "BAT-1")
        self.assertEqual(sorted(out["affected_forms"]), ["F-1", "F-2"])

        # F-1：配对整体失效，进入重核待补
        self.assertIsNone(services.active_pairing(self.conn, "F-1"))
        # F-2：R3 失效、R4 幸存，配对解体，暂无替代 -> 待补
        self.assertIsNone(services.active_pairing(self.conn, "F-2"))

        rows = self.conn.execute(
            "SELECT * FROM recheck_entries WHERE status='open' ORDER BY form_id"
        ).fetchall()
        self.assertEqual([r["form_id"] for r in rows], ["F-1", "F-2"])

        # 作废后该批新报告一律拒收
        r = services.import_reports(self.conn, [
            report("F-1", "R5", "B5", "LAB-C", "BAT-1"),
        ])
        self.assertEqual(len(r["inserted"]), 0)
        self.assertIn("已作废", r["rejected"][0]["reason"])

        # 用其他批次补报告：F-2 幸存者 R4 须保留在新配对中
        r = services.import_reports(self.conn, [
            report("F-2", "R6", "B6", "LAB-A", "BAT-3"),
        ])
        pairing = services.active_pairing(self.conn, "F-2")
        self.assertIsNotNone(pairing)
        self.assertEqual({pairing["report_a_id"], pairing["report_b_id"]},
                         {"R4", "R6"})  # 幸存者 R4 保留
        # F-2 的重核记录已核销，F-1 仍待补
        open_forms = [row["form_id"] for row in services.open_rechecks(self.conn)]
        self.assertEqual(open_forms, ["F-1"])

        # F-1 补齐两份不同瓶/实验室报告 -> 自动配成并核销
        services.import_reports(self.conn, [
            report("F-1", "R7", "B7", "LAB-A", "BAT-3"),
            report("F-1", "R8", "B8", "LAB-B", "BAT-3"),
        ])
        self.assertIsNotNone(services.active_pairing(self.conn, "F-1"))
        self.assertEqual(len(services.open_rechecks(self.conn)), 0)

        # 历史配对记录保留：F-2 有一条解体记录 + 一条生效记录
        history = self.conn.execute(
            "SELECT COUNT(*) c FROM pairings WHERE form_id='F-2'"
        ).fetchone()["c"]
        self.assertEqual(history, 2)

    def test_retain_still_valid_pairing(self):
        """原配对两份报告均不在被作废批次 -> 原样保留，不重新配对。"""
        self._setup_two_forms()
        # F-3 完全使用 BAT-2 配对；作废 BAT-1 不影响其两份报告
        services.import_reports(self.conn, [
            report("F-3", "R10", "B10", "LAB-A", "BAT-2"),
            report("F-3", "R11", "B11", "LAB-B", "BAT-2"),
        ])
        pair_id_before = services.active_pairing(self.conn, "F-3")["id"]
        # F-4：两份 BAT-1 报告（B32/LAB-C）与 R32 同瓶、与 R33 同实验室，
        # 与两份 BAT-2 报告都不兼容；生效配对只能是 R32+R33（均 BAT-2），
        # BAT-1 报告游离在配对之外
        services.import_reports(self.conn, [
            report("F-4", "R30", "B32", "LAB-C", "BAT-1"),
            report("F-4", "R31", "B32", "LAB-C", "BAT-1"),
            report("F-4", "R32", "B32", "LAB-B", "BAT-2"),
            report("F-4", "R33", "B33", "LAB-C", "BAT-2"),
        ])
        out = services.invalidate_batch(self.conn, "BAT-1")

        # F-3 不涉及 BAT-1：不进重核名单，配对原样保留（统计中不计 retained）
        self.assertEqual(out["retained"], 1)
        self.assertEqual(sorted(out["affected_forms"]), ["F-1", "F-2", "F-4"])
        after = services.active_pairing(self.conn, "F-3")
        self.assertIsNotNone(after)
        self.assertEqual(after["id"], pair_id_before)
        self.assertEqual({after["report_a_id"], after["report_b_id"]},
                         {"R10", "R11"})
        forms_in_recheck = {r["form_id"] for r in self.conn.execute(
            "SELECT DISTINCT form_id FROM recheck_entries WHERE batch_id='BAT-1'"
        ).fetchall()}
        self.assertEqual(forms_in_recheck, {"F-1", "F-2", "F-4"})
        self.assertNotIn("F-3", forms_in_recheck)

        # F-4：含 BAT-1 报告但均未参与配对；生效配对 R32+R33 不受作废影响 -> 保留
        f4 = services.active_pairing(self.conn, "F-4")
        self.assertEqual({f4["report_a_id"], f4["report_b_id"]}, {"R32", "R33"})
        self.assertEqual(out["retained"], 1)
        open_f4 = self.conn.execute(
            "SELECT COUNT(*) c FROM recheck_entries WHERE form_id='F-4' AND status='open'"
        ).fetchone()["c"]
        self.assertEqual(open_f4, 0)

    def test_failed_invalidation_changes_nothing(self):
        """失败不作废：批次不存在 / 重复作废 / 无有效报告均回滚。"""
        with self.assertRaises(ValueError):
            services.invalidate_batch(self.conn, "NOT-EXIST")
        services.import_reports(self.conn, [
            report("F-1", "R1", "B1", "LAB-A", "BAT-1"),
            report("F-1", "R2", "B2", "LAB-B", "BAT-1"),
        ])
        services.invalidate_batch(self.conn, "BAT-1")
        valid_count = self.conn.execute(
            "SELECT COUNT(*) c FROM reports WHERE valid=1"
        ).fetchone()["c"]
        self.assertEqual(valid_count, 0)

        # 重复作废 -> 失败回滚
        with self.assertRaises(ValueError):
            services.invalidate_batch(self.conn, "BAT-1")
        self.assertEqual(len(self.conn.execute(
            "SELECT * FROM batch_invalidations").fetchall()), 1)

        # 已作废批不能二次作废，且批次表仍只有一条作废时间
        b = self.conn.execute(
            "SELECT * FROM batches WHERE batch_id='BAT-1'").fetchone()
        self.assertIsNotNone(b["invalidated_at"])

    def test_interleaved_archiving_during_recheck(self):
        """交错入档：重核期间其他批报告照常接收、照常配对。"""
        services.import_reports(self.conn, [
            report("F-1", "R1", "B1", "LAB-A", "BAT-1"),
            report("F-1", "R2", "B2", "LAB-B", "BAT-1"),
        ])
        services.invalidate_batch(self.conn, "BAT-1")
        self.assertIsNone(services.active_pairing(self.conn, "F-1"))

        # 重核期间：先到一份别的批报告（不足以配对），名单仍可查
        services.import_reports(self.conn, [
            report("F-1", "R3", "B3", "LAB-A", "BAT-2"),
        ])
        self.assertIsNone(services.active_pairing(self.conn, "F-1"))
        self.assertEqual(len(services.open_rechecks(self.conn)), 1)

        # 无关采样单 F-4 在此期间正常入档配成
        services.import_reports(self.conn, [
            report("F-4", "R20", "B20", "LAB-A", "BAT-4"),
            report("F-4", "R21", "B21", "LAB-B", "BAT-4"),
        ])
        self.assertIsNotNone(services.active_pairing(self.conn, "F-4"))

        # 第二份到达后 F-1 配成，重核核销
        services.import_reports(self.conn, [
            report("F-1", "R4", "B4", "LAB-B", "BAT-3"),
        ])
        self.assertEqual(
            {x["report_id"] for x in services.valid_reports_for_form(self.conn, "F-1")},
            {"R3", "R4"},
        )
        self.assertEqual(len(services.open_rechecks(self.conn)), 0)


class CsvParserTests(unittest.TestCase):
    def test_header_aliases_and_encoding(self):
        csv_text = "单号,报告号,原瓶号,实验室,质控批号\nF-1,R1,B1,LAB-A,BAT-1\n"
        rows, errors = parse_report_csv(csv_text)
        self.assertEqual(errors, [])
        self.assertEqual(rows[0]["form_id"], "F-1")

        en = "form_id,report_id,bottle_no,lab,batch_id\nF-1,R1,B1,LAB-A,BAT-1"
        rows, errors = parse_report_csv(en)
        self.assertEqual(rows[0]["batch_id"], "BAT-1")

        bad = "单号,报告号\nF-1,R1"
        rows, errors = parse_report_csv(bad)
        self.assertTrue(errors and "缺少列" in errors[0])

        # GBK 编码可正确解码
        gbk_bytes = "单号,报告号,原瓶号,实验室,质控批号\nF-1,R1,B1,实验室甲,BAT-1".encode("gbk")
        decoded = decode_bytes(gbk_bytes)
        rows, errors = parse_report_csv(decoded)
        self.assertEqual(rows[0]["lab"], "实验室甲")


if __name__ == "__main__":
    unittest.main(verbosity=2)
