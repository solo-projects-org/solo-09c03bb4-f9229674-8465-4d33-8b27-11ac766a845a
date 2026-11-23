"""网页端到端测试：名单导入、CSV 上传、配对展示、整批作废。"""
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402

CSV_HEADER = "单号,报告号,原瓶号,实验室,质控批号"


class WebFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app({
            "TESTING": True,
            "DB_PATH": os.path.join(self.tmp.name, "web.db"),
        })
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_flow_via_http(self):
        # 1) 导入名单（含一个非法条目）
        resp = self.client.post("/forms/import", data={
            "form_ids": "F-1\nF-2\nBAD ITEM\n"
        }, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("F-1", resp.text)
        self.assertIn("非法 1 条", resp.text)

        # 2) 续传报告：F-1 可配对；F-2 只有一份 -> 待补；名单外行拒收
        csv_text = "\n".join([
            CSV_HEADER,
            "F-1,R1,B1,LAB-A,BAT-1",
            "F-1,R2,B2,LAB-B,BAT-1",
            "F-2,R3,B3,LAB-A,BAT-1",
            "F-9,R9,B9,LAB-Z,BAT-1",
        ])
        resp = self.client.post(
            "/reports/upload",
            data={"csv_file": (io.BytesIO(csv_text.encode("utf-8-sig")), "r.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("入档 3 份", resp.text)
        self.assertIn("拒收 1 份", resp.text)

        # 首页列出采信报告号，F-2 标待补
        resp = self.client.get("/")
        self.assertIn("R1", resp.text)
        self.assertIn("R2", resp.text)
        self.assertIn("待补", resp.text)

        # 详情页
        resp = self.client.get("/forms/F-1")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("生效中（采信）", resp.text)

        # 3) 作废 BAT-1：F-1/F-2 全部进入重核待补
        resp = self.client.post("/batches", data={"batch_id": "BAT-1", "note": "演练"},
                                follow_redirects=True)
        self.assertIn("已作废", resp.text)
        self.assertIn("F-1", resp.text)
        self.assertIn("F-2", resp.text)

        resp = self.client.get("/rechecks")
        self.assertIn("F-1", resp.text)
        self.assertIn("F-2", resp.text)

        # 4) 作废后该批新报告被拒；其他批报告补齐 F-1 后自动核销
        csv_text2 = "\n".join([
            CSV_HEADER,
            "F-1,R4,B4,LAB-A,BAT-1",   # 已作废批 -> 拒收
            "F-1,R5,B5,LAB-A,BAT-2",
            "F-1,R6,B6,LAB-B,BAT-2",
        ])
        resp = self.client.post(
            "/reports/upload",
            data={"csv_text": csv_text2},
            follow_redirects=True,
        )
        self.assertIn("入档 2 份", resp.text)
        self.assertIn("拒收 1 份", resp.text)

        resp = self.client.get("/rechecks")
        self.assertNotIn("F-1", resp.text)   # 已核销
        self.assertIn("F-2", resp.text)      # 仍待补

        # 5) 重复作废 -> 失败提示且不改动
        resp = self.client.post("/batches", data={"batch_id": "BAT-1"},
                                follow_redirects=True)
        self.assertIn("作废失败", resp.text)

    def test_invalid_batch_failure_changes_nothing(self):
        self.client.post("/forms/import", data={"form_ids": "F-1"})
        resp = self.client.post("/batches", data={"batch_id": "GHOST"},
                                follow_redirects=True)
        self.assertIn("作废失败", resp.text)
        resp = self.client.get("/batches")
        self.assertNotIn("GHOST", resp.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
