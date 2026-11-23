-- 水质采样独立复核管理系统 —— SQLite 表结构
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 应复核采样单名单
CREATE TABLE IF NOT EXISTS forms (
    form_id        TEXT PRIMARY KEY,
    created_at     TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 质控批号
CREATE TABLE IF NOT EXISTS batches (
    batch_id       TEXT PRIMARY KEY,
    created_at     TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    invalidated_at TEXT
);

-- 续传报告（单号、报告号、原瓶号、实验室、质控批号）
CREATE TABLE IF NOT EXISTS reports (
    report_id      TEXT PRIMARY KEY,
    form_id        TEXT NOT NULL,
    bottle_no      TEXT NOT NULL,
    lab            TEXT NOT NULL,
    batch_id       TEXT NOT NULL,
    archived_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    valid          INTEGER NOT NULL DEFAULT 1 CHECK (valid IN (0, 1)),
    invalidated_at TEXT,
    FOREIGN KEY (form_id)  REFERENCES forms(form_id),
    FOREIGN KEY (batch_id) REFERENCES batches(batch_id)
);
CREATE INDEX IF NOT EXISTS idx_reports_form ON reports(form_id, valid);
CREATE INDEX IF NOT EXISTS idx_reports_batch ON reports(batch_id);

-- 采样单配对（采信的两份报告）；同一单最多一条生效配对（由部分唯一索引保证）
CREATE TABLE IF NOT EXISTS pairings (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    form_id          TEXT NOT NULL,
    report_a_id      TEXT NOT NULL,
    report_b_id      TEXT NOT NULL,
    created_at       TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    dissolved_at     TEXT,
    dissolve_reason  TEXT,
    FOREIGN KEY (form_id)     REFERENCES forms(form_id),
    FOREIGN KEY (report_a_id) REFERENCES reports(report_id),
    FOREIGN KEY (report_b_id) REFERENCES reports(report_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pairings_active
    ON pairings(form_id) WHERE dissolved_at IS NULL;

-- 作废事件（与名单一同生效，记录作废时刻）
CREATE TABLE IF NOT EXISTS batch_invalidations (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id          TEXT NOT NULL UNIQUE,
    invalidated_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    affected_forms    INTEGER NOT NULL DEFAULT 0,
    retained_pairings INTEGER NOT NULL DEFAULT 0,
    rebuilt_pairings  INTEGER NOT NULL DEFAULT 0,
    note              TEXT,
    FOREIGN KEY (batch_id) REFERENCES batches(batch_id)
);

-- 重核名单：作废该批涉及的采样单
CREATE TABLE IF NOT EXISTS recheck_entries (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    form_id          TEXT NOT NULL,
    batch_id         TEXT NOT NULL,
    created_at       TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    resolved_at      TEXT,
    status           TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    FOREIGN KEY (form_id)  REFERENCES forms(form_id),
    FOREIGN KEY (batch_id) REFERENCES batches(batch_id)
);
CREATE INDEX IF NOT EXISTS idx_recheck_open ON recheck_entries(status, form_id);
