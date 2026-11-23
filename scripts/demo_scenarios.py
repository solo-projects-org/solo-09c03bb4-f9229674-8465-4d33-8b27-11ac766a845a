#!/usr/bin/env python3
"""需求场景端到端试跑：
  A. 作废换证：整批作废 -> 原配对解体/保留 -> 幸存者优先重建 -> 该批新报告拒收
  B. 交错入档：重核期间名单照常查询，其他批报告交错续传、自动核销
  C. 并发链路：作废与续传并发（两种提交顺序 + 不指定顺序），验证串行化修复
用法：python3 scripts/demo_scenarios.py [数据库路径]（默认 data/demo.db，自动重建）
"""
import os
import sqlite3
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app          # noqa: E402
from app import services           # noqa: E402
from app.csv_parser import parse_report_csv  # noqa: E402

HEADER = "单号,报告号,原瓶号,实验室,质控批号"


def rep(form, rid, bottle, lab, batch):
    return {"form_id": form, "report_id": rid, "bottle_no": bottle,
            "lab": lab, "batch_id": batch}


def upload(conn, csv_text):
    rows, errors = parse_report_csv(csv_text)
    assert not errors, errors
    return services.import_reports(conn, rows)


def snapshot(conn, title):
    print(f"\n----- {title} -----")
    for f in services.forms_overview(conn):
        if f["pairing_id"]:
            status = f"采信 {f['report_a_id']} + {f['report_b_id']}"
        else:
            status = "待补（无生效配对）"
        recheck = f"，重核中×{f['open_rechecks']}" if f["open_rechecks"] else ""
        print(f"  {f['form_id']}: 有效报告 {f['valid_reports']}/{f['total_reports']} | {status}{recheck}")
    opens = services.open_rechecks(conn)
    if opens:
        print("  重核待补名单:", ", ".join(
            f"{e['form_id']}(触发批 {e['batch_id']}, 有效报告 {e['valid_reports']})"
            for e in opens))
    else:
        print("  重核待补名单: 空")


def scenario_a_invalidate_and_replace(conn):
    print("=" * 70)
    print("场景 A：作废换证")
    print("=" * 70)
    services.import_forms(conn, "F-101\nF-102\nF-103")
    conn.commit()

    upload(conn, "\n".join([
        HEADER,
        # F-101 两份均 BAT-1；F-102 一份 BAT-1 一份 BAT-2；F-103 全 BAT-2
        "F-101,R101-1,B1,LAB-A,BAT-1",
        "F-101,R101-2,B2,LAB-B,BAT-1",
        "F-102,R102-1,B3,LAB-A,BAT-1",
        "F-102,R102-2,B4,LAB-B,BAT-2",
        "F-103,R103-1,B5,LAB-A,BAT-2",
        "F-103,R103-2,B6,LAB-B,BAT-2",
    ]))
    snapshot(conn, "首批报告入档后（三单均已采信）")

    out = services.invalidate_batch(conn, "BAT-1", note="演练作废")
    print(f"\n>> 作废 BAT-1：涉及 {out['affected_forms']}，"
          f"原配对保留 {out['retained']} 单，当场重建 {out['rebuilt']} 单")
    snapshot(conn, "作废生效后（与重核名单同一事务）")

    # 重核期间补其他批报告
    result = upload(conn, "\n".join([
        HEADER,
        "F-101,R101-3,B7,LAB-A,BAT-3",   # F-101 第一份替代
        "F-102,R102-3,B8,LAB-A,BAT-3",   # F-102 幸存者 R102-2 的替代搭档
    ]))
    print(f"\n>> 第一批替代报告入档：配成 {result['paired']}，待补 {result['pending']}")
    p102 = services.active_pairing(conn, "F-102")
    print(f">> F-102 新配对：{p102['report_a_id']} + {p102['report_b_id']}"
          f"（幸存者 R102-2 {'已保留' if 'R102-2' in (p102['report_a_id'], p102['report_b_id']) else '丢失！'}）")
    snapshot(conn, "部分替代报告入档后")

    # 已作废批次的新报告拒收
    rejected = upload(conn, "\n".join([
        HEADER,
        "F-101,R101-X,B9,LAB-C,BAT-1",
    ]))
    print(f"\n>> 作废后再传 BAT-1 报告：入档 {len(rejected['inserted'])} 份，"
          f"拒收 {len(rejected['rejected'])} 份（原因：{rejected['rejected'][0]['reason']}）")

    result = upload(conn, "\n".join([
        HEADER,
        "F-101,R101-4,B9,LAB-B,BAT-3",
    ]))
    print(f">> 第二批替代报告入档：配成 {result['paired']}，待补 {result['pending']}")
    snapshot(conn, "F-101 补齐后的最终状态")
    assert not services.open_rechecks(conn), "重核应全部核销"


def scenario_b_interleaved_archiving(conn):
    print("\n" + "=" * 70)
    print("场景 B：交错入档（重核期间照常查名单、收其他批报告）")
    print("=" * 70)
    services.import_forms(conn, "F-201\nF-202\nF-203")
    conn.commit()

    upload(conn, "\n".join([
        HEADER,
        "F-201,R201-1,B1,LAB-A,BAT-X1",
        "F-201,R201-2,B2,LAB-B,BAT-X1",
        "F-202,R202-1,B3,LAB-A,BAT-X1",   # 只有一份，本就待补
    ]))
    snapshot(conn, "BAT-X1 部分入档（F-201 采信，F-202/F-203 待补）")

    services.invalidate_batch(conn, "BAT-X1")
    print("\n>> BAT-X1 作废，F-201/F-202 进入重核；重核期间继续操作")
    snapshot(conn, "作废后即刻查询名单（页面查询不受影响）")

    # 交错：其他批报告一份一份来，中间夹一个无关新单的配对
    upload(conn, "\n".join([HEADER, "F-201,R201-3,B4,LAB-A,BAT-X2"]))
    print("\n>> F-201 收到 BAT-X2 第一份（仍缺一份），名单仍可查：")
    snapshot(conn, "F-201 半补齐状态")

    upload(conn, "\n".join([
        HEADER,
        "F-203,R203-1,B5,LAB-A,BAT-X4",
        "F-203,R203-2,B6,LAB-B,BAT-X4",
    ]))
    print("\n>> 重核期间无关单 F-203 用 BAT-X4 正常入档并配成：")
    snapshot(conn, "无关单 F-203 正常配成，F-201/F-202 仍重核")

    upload(conn, "\n".join([
        HEADER,
        "F-201,R201-4,B7,LAB-B,BAT-X3",
        "F-202,R202-2,B8,LAB-B,BAT-X2",   # 与 R202-1？R202-1 已失效；还需第二份
    ]))
    print("\n>> F-201 补齐自动核销；F-202 只来一份仍待补：")
    snapshot(conn, "F-201 核销，F-202 继续待补")

    upload(conn, "\n".join([HEADER, "F-202,R202-3,B9,LAB-C,BAT-X3"]))
    snapshot(conn, "F-202 补齐后的最终状态")
    assert not services.open_rechecks(conn)


def scenario_c_concurrent_upload_and_invalidate(db_path):
    """作废与续传并发：两个线程各用独立连接同时操作同一数据库文件。"""
    print("\n" + "=" * 70)
    print("场景 C：作废与续传并发（BEGIN IMMEDIATE 串行化）")
    print("=" * 70)

    def connect():
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def check_invariants(tag):
        conn = connect()
        slipped = conn.execute(
            "SELECT r.report_id FROM reports r JOIN batches b ON b.batch_id = r.batch_id "
            "WHERE b.invalidated_at IS NOT NULL AND r.valid = 1").fetchall()
        stale = conn.execute(
            "SELECT p.id FROM pairings p JOIN reports r "
            "ON r.report_id IN (p.report_a_id, p.report_b_id) "
            "WHERE p.dissolved_at IS NULL AND r.valid = 0").fetchall()
        assert not slipped, f"{tag}: 作废批中仍有有效报告 {[r['report_id'] for r in slipped]}"
        assert not stale, f"{tag}: 生效配对引用了失效报告 {[r['id'] for r in stale]}"
        print(f"  不变量[{tag}]：作废批无有效报告、生效配对无失效成员 ✔")
        conn.close()

    # ---- 顺序一：续传先提交，作废把“刚入档的同批报告”一起失效并重核 ----
    conn = connect()
    services.import_forms(conn, "F-301\nF-302\nF-303")
    upload(conn, "\n".join([
        HEADER,
        "F-301,R301-1,B1,LAB-A,BAT-C1",
        "F-301,R301-2,B2,LAB-B,BAT-C1",
    ]))
    conn.close()

    upload_inside = threading.Event()
    invalidate_started = threading.Event()
    errors = {}

    def rows_order1():
        yield rep("F-302", "R302-1", "B3", "LAB-A", "BAT-C1")   # 与作废批同批
        upload_inside.set()
        invalidate_started.wait(5)
        for i in range(10):                                      # 其他批合规行
            yield rep("F-303", f"R303-{i}", f"BB{i}",
                      "LAB-B" if i % 2 else "LAB-C", "BAT-C2")

    def upload_worker():
        c = connect()
        try:
            services.import_reports(c, rows_order1())
        except Exception as exc:  # noqa: BLE001
            errors["upload"] = exc
        finally:
            c.close()

    def invalidate_worker():
        c = connect()
        try:
            invalidate_started.set()
            services.invalidate_batch(c, "BAT-C1")
        except Exception as exc:  # noqa: BLE001
            errors["invalidate"] = exc
        finally:
            c.close()

    t1 = threading.Thread(target=upload_worker)
    t1.start()
    assert upload_inside.wait(5)
    t2 = threading.Thread(target=invalidate_worker)
    t2.start()
    t1.join(20)
    t2.join(20)
    assert not errors, f"顺序一并发出现异常：{errors}"
    conn = connect()
    r = conn.execute("SELECT valid FROM reports WHERE report_id='R302-1'").fetchone()
    assert r["valid"] == 0, "先入档的同批报告应随批作废"
    opens = {e["form_id"] for e in services.open_rechecks(conn)}
    assert {"F-301", "F-302"} <= opens, f"F-301/F-302 应进重核，实际 {opens}"
    n = conn.execute("SELECT COUNT(*) c FROM reports WHERE batch_id='BAT-C2'").fetchone()["c"]
    assert n == 10, "其他批次 10 行应全部入档"
    print("  顺序一（入档先->作废后）：R302-1 随批失效，F-301/F-302 进重核，"
          "BAT-C2 十行照收 ✔")
    check_invariants("顺序一")
    conn.close()

    # ---- 顺序二：作废先提交，续传的同批行逐行拒收、其余行照收 ----
    conn = connect()
    services.import_forms(conn, "F-401\nF-402")
    upload(conn, "\n".join([
        HEADER,
        "F-401,R401-1,B1,LAB-A,BAT-D1",
        "F-401,R401-2,B2,LAB-B,BAT-D1",
    ]))
    services.invalidate_batch(conn, "BAT-D1")
    result = upload(conn, "\n".join([
        HEADER,
        "F-402,R402-1,B3,LAB-A,BAT-D1",   # 已作废批 -> 拒收
        "F-402,R402-2,B4,LAB-A,BAT-D2",   # 合规 -> 照收
        "F-402,R402-3,B5,LAB-B,BAT-D2",   # 合规 -> 照收并配成
    ]))
    assert len(result["rejected"]) == 1 and "已作废" in result["rejected"][0]["reason"]
    assert len(result["inserted"]) == 2
    assert services.active_pairing(conn, "F-402") is not None
    print("  顺序二（作废先->入档后）：BAT-D1 行拒收（"
          f"{result['rejected'][0]['reason']}），BAT-D2 两行照收并配成 ✔")
    check_invariants("顺序二")
    conn.close()

    # ---- 不指定顺序：多轮并发，任意交错都收敛到正确终态 ----
    for round_no in range(3):
        conn = connect()
        services.import_forms(conn, f"F-5{round_no}1\nF-5{round_no}2")
        upload(conn, "\n".join([
            HEADER,
            f"F-5{round_no}1,R5{round_no}1-1,B1,LAB-A,BAT-E{round_no}",
            f"F-5{round_no}1,R5{round_no}1-2,B2,LAB-B,BAT-E{round_no}",
        ]))
        conn.close()
        errors = {}

        def upload_worker2(rn=round_no):
            c = connect()
            try:
                services.import_reports(c, [
                    rep(f"F-5{rn}2", f"R5{rn}2-1", "B3", "LAB-A", f"BAT-E{rn}"),
                    rep(f"F-5{rn}2", f"R5{rn}2-2", "B4", "LAB-B", f"BAT-F{rn}"),
                    rep(f"F-5{rn}2", f"R5{rn}2-3", "B5", "LAB-C", f"BAT-F{rn}"),
                ])
            except Exception as exc:  # noqa: BLE001
                errors["upload"] = exc
            finally:
                c.close()

        def invalidate_worker2(rn=round_no):
            c = connect()
            try:
                services.invalidate_batch(c, f"BAT-E{rn}")
            except Exception as exc:  # noqa: BLE001
                errors["invalidate"] = exc
            finally:
                c.close()

        t1 = threading.Thread(target=upload_worker2)
        t2 = threading.Thread(target=invalidate_worker2)
        t1.start()
        t2.start()
        t1.join(20)
        t2.join(20)
        assert not errors, f"第 {round_no} 轮并发异常：{errors}"
        conn = connect()
        rid = f"R5{round_no}2-1"
        row = conn.execute("SELECT valid FROM reports WHERE report_id=?", (rid,)).fetchone()
        state = "未落库（作废先提交，被拒收）" if row is None else \
            f"已落库 valid={row['valid']}（入档先提交，随批失效）"
        assert row is None or row["valid"] == 0
        print(f"  第 {round_no} 轮无序并发：{rid} {state} ✔")
        check_invariants(f"第 {round_no} 轮")
        conn.close()


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "demo.db")
    if os.path.exists(db_path):
        for suffix in ("", "-wal", "-shm"):
            p = db_path + suffix
            if os.path.exists(p):
                os.remove(p)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    app = create_app({"DB_PATH": db_path})
    with app.app_context():
        from app.db import get_db
        conn = get_db()
        scenario_a_invalidate_and_replace(conn)
        scenario_b_interleaved_archiving(conn)
    scenario_c_concurrent_upload_and_invalidate(db_path)
    print("\n三个场景试跑全部通过 ✔")


if __name__ == "__main__":
    main()
