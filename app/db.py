"""SQLite 连接与初始化。"""
import os
import sqlite3

from flask import Flask, current_app, g

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")


def get_db() -> sqlite3.Connection:
    """请求级连接；row_factory 让结果可以按列名访问。"""
    if "db" not in g:
        conn = sqlite3.connect(
            current_app.config["DB_PATH"],
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        conn.row_factory = sqlite3.Row
        # 外键约束 + 串行写入，保证作废/入档事务的完整性
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        g.db = conn
    return g.db


def close_db(exc=None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db(app: Flask) -> None:
    """按 schema.sql 建表（已存在则跳过）。"""
    conn = sqlite3.connect(app.config["DB_PATH"])
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            conn.executescript(f.read())
        conn.commit()
    finally:
        conn.close()


def init_app(app: Flask) -> None:
    app.teardown_appcontext(close_db)
    os.makedirs(os.path.dirname(app.config["DB_PATH"]), exist_ok=True)
    init_db(app)
