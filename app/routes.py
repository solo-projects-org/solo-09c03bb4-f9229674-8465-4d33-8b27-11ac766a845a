"""网页路由：名单导入、报告续传、配对名单、按批作废与重核。"""
from __future__ import annotations

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from . import services
from .csv_parser import decode_bytes, parse_report_csv
from .db import get_db

bp = Blueprint("main", __name__)


@bp.route("/")
def index():
    conn = get_db()
    forms = services.forms_overview(conn)
    stats = {
        "total_forms": len(forms),
        "paired": sum(1 for f in forms if f["pairing_id"] is not None),
        "pending": sum(1 for f in forms if f["pairing_id"] is None),
        "open_rechecks": len(services.open_rechecks(conn)),
    }
    return render_template("index.html", forms=forms, stats=stats)


@bp.route("/forms/import", methods=["GET", "POST"])
def import_forms():
    if request.method == "POST":
        raw = request.form.get("form_ids", "")
        conn = get_db()
        result = services.import_forms(conn, raw)
        conn.commit()
        flash(f"名单导入完成：新增 {len(result['added'])} 单，"
              f"已存在 {len(result['existing'])} 单，"
              f"非法 {len(result['invalid'])} 条。", "info")
        if result["invalid"]:
            flash("非法条目（含空格）：" + "、".join(result["invalid"][:20]), "warning")
        return redirect(url_for("main.index"))
    return render_template("import_forms.html")


@bp.route("/reports/upload", methods=["GET", "POST"])
def upload_reports():
    result = None
    if request.method == "POST":
        text = request.form.get("csv_text", "")
        file = request.files.get("csv_file")
        if file and file.filename:
            text = decode_bytes(file.read())
        if not text.strip():
            flash("请上传 CSV 文件或粘贴 CSV 文本。", "warning")
            return redirect(url_for("main.upload_reports"))

        rows, errors = parse_report_csv(text)
        if errors:
            for msg in errors:
                flash("CSV 解析失败：" + msg, "danger")
            return render_template("upload_reports.html", result=None)

        conn = get_db()
        try:
            result = services.import_reports(conn, rows)
        except Exception as exc:  # 意外错误整体回滚
            conn.rollback()
            current_app.logger.exception("报告入档失败")
            flash(f"入档失败，已全部回滚（未作废任何数据）：{exc}", "danger")
            return render_template("upload_reports.html", result=None)

        flash(
            f"续传完成：入档 {len(result['inserted'])} 份，"
            f"拒收 {len(result['rejected'])} 份；"
            f"新配成 {len(result['paired'])} 单，待补 {len(result['pending'])} 单。",
            "info",
        )
    return render_template("upload_reports.html", result=result)


@bp.route("/forms/<form_id>")
def form_detail(form_id):
    conn = get_db()
    detail = services.form_detail(conn, form_id)
    if detail is None:
        flash(f"单号 {form_id} 不在名单中。", "warning")
        return redirect(url_for("main.index"))
    return render_template("form_detail.html", d=detail)


@bp.route("/batches", methods=["GET", "POST"])
def batches():
    conn = get_db()
    if request.method == "POST":
        batch_id = (request.form.get("batch_id") or "").strip()
        note = (request.form.get("note") or "").strip()
        if not batch_id:
            flash("请填写要作废的质控批号。", "warning")
            return redirect(url_for("main.batches"))
        try:
            outcome = services.invalidate_batch(conn, batch_id, note)
        except ValueError as exc:
            conn.rollback()
            flash(f"作废失败，未改动任何数据：{exc}", "danger")
            return redirect(url_for("main.batches"))
        inv = outcome["invalidation"]
        flash(
            f"质控批 {batch_id} 已作废（{inv['invalidated_at']}）："
            f"涉及 {len(outcome['affected_forms'])} 单，"
            f"原配对保留 {outcome['retained']} 单，"
            f"以其他批报告重建 {outcome['rebuilt']} 单，"
            "其余进入重核待补；该批新报告今后一律拒收。",
            "info",
        )
        return redirect(url_for("main.batches"))
    return render_template(
        "batches.html",
        batches=services.batches_overview(conn),
        rechecks=services.open_rechecks(conn),
    )


@bp.route("/rechecks")
def rechecks():
    conn = get_db()
    return render_template("rechecks.html", rechecks=services.open_rechecks(conn))
