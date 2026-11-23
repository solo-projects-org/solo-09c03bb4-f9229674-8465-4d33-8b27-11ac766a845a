"""报告 CSV 解析：编码探测 + 表头映射。

允许的表头（大小写/空格不敏感，中英文均可）：
  单号/form_id      报告号/report_id   原瓶号/bottle_no
  实验室/lab        质控批号/batch_id
"""
from __future__ import annotations

import csv
import io

HEADER_ALIASES = {
    "form_id": {"单号", "采样单号", "form_id", "formid", "form"},
    "report_id": {"报告号", "报告编号", "report_id", "reportid", "report"},
    "bottle_no": {"原瓶号", "瓶号", "bottle_no", "bottleno", "bottle"},
    "lab": {"实验室", "检测实验室", "lab", "laboratory"},
    "batch_id": {"质控批号", "质控批", "批号", "batch_id", "batchid", "batch"},
}


def decode_bytes(data: bytes) -> str:
    """按 UTF-8(BOM) -> GB18030 -> UTF-8 容错 的顺序解码。"""
    for enc in ("utf-8-sig", "gb18030", "utf-8"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _normalize(header: str) -> str:
    return (header or "").strip().lstrip("﻿").lower().replace(" ", "")


def parse_report_csv(text: str) -> tuple[list[dict], list[str]]:
    """解析报告 CSV，返回 (数据行, 错误信息)。"""
    errors: list[str] = []
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        return [], ["文件为空，缺少表头"]

    mapping: dict[int, str] = {}
    for idx, raw in enumerate(header):
        key = _normalize(raw)
        for canonical, aliases in HEADER_ALIASES.items():
            if key in {a.lower() for a in aliases}:
                mapping[idx] = canonical
                break

    missing = [c for c in HEADER_ALIASES if c not in mapping.values()]
    if missing:
        cn = {"form_id": "单号", "report_id": "报告号", "bottle_no": "原瓶号",
              "lab": "实验室", "batch_id": "质控批号"}
        errors.append("表头缺少列：" + "、".join(cn[m] for m in missing)
                      + f"；实际表头：{', '.join(h.strip() for h in header)}")
        return [], errors

    rows: list[dict] = []
    for raw_row in reader:
        if not any(cell.strip() for cell in raw_row):
            continue  # 跳过空行
        row = {}
        for idx, canonical in mapping.items():
            row[canonical] = raw_row[idx].strip() if idx < len(raw_row) else ""
        rows.append(row)
    return rows, errors
