"""作废 / 续传并发链路的回归测试。

修复前的事实（见需求）：作废与续传并发时，
  - 作废读到的“有效报告清单”与后续写库不在同一事务，期间入档的同批报告
    被置失效，但所属采样单不进重核、生效配对不解体（页面仍采信该批报告）；
  - 续传的“批次是否已作废”检查与入档不在同一事务，作废先提交时新报告
    仍会写进已作废批次；
  - 两个写操作互相撞锁，常有一方报 “database is locked”。

修复后（BEGIN IMMEDIATE 串行化写事务）：两种提交顺序都必须收敛到
正确终态，且任何时刻满足两条不变量：
  A. 已作废批次中不存在 valid=1 的报告；
  B. 生效配对（未解体）不引用任何失效报告。
"""
import os
import sqlite3
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app import services  # noqa: E402


def rep(form, rid, bottle, lab, batch):
    return {"form_id": form, "report_id": rid, "bottle_no": bottle,
            "lab": lab, "batch_id": batch}


class ConcurrencyTestBase(unittest.TestCase):
    """每个用例独立数据库；种子数据：F-1 已凭 BAT-1 的 R1+R2 配成。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "conc.db")
        create_app({"TESTING": True, "DB_PATH": self.db_path})  # 建表
        conn = self._connect()
        services.import_forms(conn, "F-1\nF-2\nF-3")
        services.import_reports(conn, [
            rep("F-1", "R1", "B1", "LAB-A", "BAT-1"),
            rep("F-1", "R2", "B2", "LAB-B", "BAT-1"),
        ])
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def _run_threads(self, *workers):
        threads = [threading.Thread(target=w, daemon=True) for w in workers]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        for t in threads:
            self.assertFalse(t.is_alive(), "并发线程超时未结束（疑似死锁）")

    def _invariant_violations(self, conn):
        slipped = conn.execute(
            "SELECT r.report_id FROM reports r "
            "JOIN batches b ON b.batch_id = r.batch_id "
            "WHERE b.invalidated_at IS NOT NULL AND r.valid = 1"
        ).fetchall()
        stale = conn.execute(
            "SELECT p.id FROM pairings p "
            "JOIN reports r ON r.report_id IN (p.report_a_id, p.report_b_id) "
            "WHERE p.dissolved_at IS NULL AND r.valid = 0"
        ).fetchall()
        return [r["report_id"] for r in slipped], [r["id"] for r in stale]

    def _assert_invariants(self, conn):
        slipped, stale = self._invariant_violations(conn)
        self.assertEqual(slipped, [], f"已作废批次中仍有有效报告：{slipped}")
        self.assertEqual(stale, [], f"生效配对引用了失效报告：{stale}")


class OrderedSubmissionTests(ConcurrencyTestBase):
    """两种提交顺序：入档先作废后 / 作废先入档后（并发持锁、确定顺序）。"""

    def test_upload_commits_first_then_invalidate_voids_those_reports(self):
        """顺序一：同批报告先入档 -> 随批次一起作废，所属采样单进重核。"""
        upload_inside = threading.Event()     # 续传已持锁并插入首行
        invalidate_started = threading.Event()  # 作废线程已启动（在等锁）
        outcome, errors = {}, {}

        def upload_rows():
            # 首行：与将被作废批次同批（BAT-1）的新报告
            yield rep("F-2", "R100", "B100", "LAB-A", "BAT-1")
            upload_inside.set()
            invalidate_started.wait(5)
            # 其余行：其他批次的合规报告，应照常入档
            for i in range(30):
                yield rep("F-3", f"R2{i:02d}", f"BB{i}",
                          "LAB-B" if i % 2 else "LAB-C", "BAT-2")

        def upload_worker():
            conn = self._connect()
            try:
                outcome["upload"] = services.import_reports(conn, upload_rows())
            except Exception as exc:  # noqa: BLE001 - 收集后统一断言
                errors["upload"] = exc
            finally:
                conn.close()

        def invalidate_worker():
            conn = self._connect()
            try:
                invalidate_started.set()
                outcome["invalidate"] = services.invalidate_batch(conn, "BAT-1")
            except Exception as exc:  # noqa: BLE001
                errors["invalidate"] = exc
            finally:
                conn.close()

        upload_thread = threading.Thread(target=upload_worker, daemon=True)
        upload_thread.start()
        self.assertTrue(upload_inside.wait(5), "续传线程未能进入写事务")
        invalidate_thread = threading.Thread(target=invalidate_worker, daemon=True)
        invalidate_thread.start()
        upload_thread.join(20)
        invalidate_thread.join(20)
        self.assertFalse(upload_thread.is_alive() or invalidate_thread.is_alive(),
                         "并发线程超时未结束（疑似死锁）")

        self.assertEqual(errors, {}, f"并发操作出现异常：{errors}")
        # 作废在续传提交后生效：先入档的 R100 也随批失效
        self.assertEqual(sorted(outcome["invalidate"]["affected_forms"]), ["F-1", "F-2"])
        conn = self._connect()
        r100 = conn.execute(
            "SELECT valid, invalidated_at FROM reports WHERE report_id='R100'"
        ).fetchone()
        self.assertIsNotNone(r100, "先入档的 R100 应已落库")
        self.assertEqual(r100["valid"], 0, "先入档的同批报告应随批作废")
        self.assertIsNotNone(r100["invalidated_at"])
        # F-2 唯一报告被作废 -> 进入重核待补
        open_forms = {r["form_id"] for r in services.open_rechecks(conn)}
        self.assertIn("F-2", open_forms)
        self.assertIn("F-1", open_forms)
        # 其他批次 30 行全部照收
        archived = conn.execute(
            "SELECT COUNT(*) c FROM reports WHERE batch_id='BAT-2' AND valid=1"
        ).fetchone()["c"]
        self.assertEqual(archived, 30)
        self._assert_invariants(conn)
        conn.close()

    def test_invalidate_commits_first_then_upload_rejects_batch_rows(self):
        """顺序二：作废先完成 -> 同批未入档报告逐行拒收，其余合规行照收。"""
        invalidate_in_tx = threading.Event()  # 作废已持锁
        upload_started = threading.Event()    # 续传线程已启动（在等锁）
        outcome, errors = {}, {}

        orig_now = services._now

        def hooked_now():
            # 作废事务内的第一个时间戳：此刻作废已持锁，放行并等待续传来撞锁
            if not invalidate_in_tx.is_set():
                invalidate_in_tx.set()
                upload_started.wait(5)
            return orig_now()

        def invalidate_worker():
            conn = self._connect()
            try:
                outcome["invalidate"] = services.invalidate_batch(conn, "BAT-1")
            except Exception as exc:  # noqa: BLE001
                errors["invalidate"] = exc
            finally:
                conn.close()

        def upload_worker():
            conn = self._connect()
            try:
                upload_started.set()
                outcome["upload"] = services.import_reports(conn, [
                    rep("F-2", "R100", "B100", "LAB-A", "BAT-1"),   # 已作废批 -> 拒收
                    rep("F-2", "R101", "B101", "LAB-A", "BAT-2"),   # 合规 -> 照收
                    rep("F-3", "R200", "B200", "LAB-C", "BAT-2"),   # 合规 -> 照收
                ])
            except Exception as exc:  # noqa: BLE001
                errors["upload"] = exc
            finally:
                conn.close()

        services._now = hooked_now
        try:
            invalidate_thread = threading.Thread(target=invalidate_worker, daemon=True)
            invalidate_thread.start()
            self.assertTrue(invalidate_in_tx.wait(5), "作废线程未能进入写事务")
            upload_thread = threading.Thread(target=upload_worker, daemon=True)
            upload_thread.start()
            invalidate_thread.join(20)
            upload_thread.join(20)
        finally:
            services._now = orig_now
        self.assertFalse(invalidate_thread.is_alive() or upload_thread.is_alive(),
                         "并发线程超时未结束（疑似死锁）")

        self.assertEqual(errors, {}, f"并发操作出现异常：{errors}")
        result = outcome["upload"]
        # 作废先提交：BAT-1 行逐行拒收，BAT-2 两行照收
        self.assertEqual(len(result["rejected"]), 1)
        self.assertEqual(result["rejected"][0]["report_id"], "R100")
        self.assertIn("已作废", result["rejected"][0]["reason"])
        self.assertEqual({r["report_id"] for r in result["inserted"]}, {"R101", "R200"})
        conn = self._connect()
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM reports WHERE report_id='R100'").fetchone(),
            "作废后同批新报告不得落库")
        self._assert_invariants(conn)
        conn.close()


class UnorderedConcurrencyTests(ConcurrencyTestBase):
    """不指定顺序的并发：任意交错都必须收敛到正确终态。"""

    def test_concurrent_upload_and_invalidate_any_order(self):
        for round_no in range(5):
            with self.subTest(round=round_no):
                self._one_round(round_no)

    def _one_round(self, round_no):
        # 每轮独立数据库，避免轮次间相互影响
        self.tearDown()
        self.setUp()
        outcome, errors = {}, {}

        def upload_worker():
            conn = self._connect()
            try:
                rows = [rep("F-2", "R100", "B100", "LAB-A", "BAT-1")]
                rows += [rep("F-3", f"R3{i:02d}", f"BC{i}",
                             "LAB-B" if i % 2 else "LAB-C", "BAT-2")
                         for i in range(20)]
                outcome["upload"] = services.import_reports(conn, rows)
            except Exception as exc:  # noqa: BLE001
                errors["upload"] = exc
            finally:
                conn.close()

        def invalidate_worker():
            conn = self._connect()
            try:
                outcome["invalidate"] = services.invalidate_batch(conn, "BAT-1")
            except Exception as exc:  # noqa: BLE001
                errors["invalidate"] = exc
            finally:
                conn.close()

        self._run_threads(upload_worker, invalidate_worker)

        self.assertEqual(errors, {}, f"第 {round_no} 轮并发出现异常：{errors}")
        self.assertIn("invalidate", outcome, "作废必须成功且只成功一次")
        conn = self._connect()
        # 作废事件唯一
        inv_count = conn.execute(
            "SELECT COUNT(*) c FROM batch_invalidations WHERE batch_id='BAT-1'"
        ).fetchone()["c"]
        self.assertEqual(inv_count, 1)
        # 其他批次 20 行全部照收
        archived = conn.execute(
            "SELECT COUNT(*) c FROM reports WHERE batch_id='BAT-2' AND valid=1"
        ).fetchone()["c"]
        self.assertEqual(archived, 20)
        # R100 两种合法终态：作废先提交 -> 拒收未落库；入档先提交 -> 已落库但随批失效
        r100 = conn.execute(
            "SELECT valid FROM reports WHERE report_id='R100'").fetchone()
        if r100 is not None:
            self.assertEqual(r100["valid"], 0, "R100 随批作废后不得仍为有效")
        self._assert_invariants(conn)
        conn.close()

    def test_concurrent_uploads_same_report_id(self):
        """两个续传并发撞同一报告号：后提交者逐行拒收，而不是整批报错。"""
        outcome, errors = {}, {}

        def make_worker(tag):
            def worker():
                conn = self._connect()
                try:
                    outcome[tag] = services.import_reports(conn, [
                        rep("F-2", "R100", "B100", "LAB-A", "BAT-2"),
                        rep("F-2", "R101", "B101", "LAB-B", "BAT-2"),
                    ])
                except Exception as exc:  # noqa: BLE001
                    errors[tag] = exc
                finally:
                    conn.close()
            return worker

        self._run_threads(make_worker("t1"), make_worker("t2"))
        self.assertEqual(errors, {}, f"并发续传出现异常：{errors}")
        inserted = {t: [r["report_id"] for r in outcome[t]["inserted"]] for t in outcome}
        rejected = {t: [r["report_id"] for r in outcome[t]["rejected"]] for t in outcome}
        # 恰好一方全入档，另一方全因重复拒收
        winner = "t1" if inserted["t1"] else "t2"
        loser = "t2" if winner == "t1" else "t1"
        self.assertEqual(inserted[winner], ["R100", "R101"])
        self.assertEqual(inserted[loser], [])
        self.assertEqual(sorted(rejected[loser]), ["R100", "R101"])
        conn = self._connect()
        self.assertEqual(
            conn.execute("SELECT COUNT(*) c FROM reports WHERE batch_id='BAT-2'")
            .fetchone()["c"], 2)
        self._assert_invariants(conn)
        conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
