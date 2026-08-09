-- ============================================================
-- Aemeath 共享黑板 DDL（SQLite，WAL 模式）
-- 由 src/blackboard/db.py 在每次连接时幂等执行（全部 IF NOT EXISTS）。
--
-- 关键 PRAGMA 在 db.py connect() 中设置，勿在此重复：
--   journal_mode = WAL     多进程/多线程并发读写（容器间 Volume 共享亦安全）
--   busy_timeout = 5000    SQLITE_BUSY → 自动排队而非丢写
--   synchronous  = NORMAL  WAL 下安全且快
-- ============================================================

-- ------------------------------------------------------------
-- 事实表（发现、死路、部分 Flag）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS facts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    problem_id  TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    type        TEXT    NOT NULL DEFAULT 'discovery'
                    CHECK (type IN ('discovery', 'deadend', 'partial')),
    source      TEXT,                                   -- 来源标识（模型/引擎/人工）
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP     -- UTC
);
CREATE INDEX IF NOT EXISTS idx_facts_problem ON facts(problem_id);

-- ------------------------------------------------------------
-- 意图表（总控下发的行动计划）
-- status: pending(待认领) → claimed(已认领) → done / failed
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS intents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    problem_id   TEXT    NOT NULL,
    action_type  TEXT    NOT NULL,                      -- 如 analyze / brute / crack / submit
    target       TEXT,                                  -- 目标（URL/文件/字段）
    reasoning    TEXT,                                  -- 下发理由
    status       TEXT    NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'claimed', 'done', 'failed')),
    claimed_by   TEXT,                                  -- 认领该 Intent 的 Worker
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,   -- UTC
    claimed_at   TIMESTAMP,
    completed_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_intents_problem ON intents(problem_id);
CREATE INDEX IF NOT EXISTS idx_intents_status  ON intents(status);

-- ------------------------------------------------------------
-- Worker 心跳表
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workers (
    id             TEXT PRIMARY KEY,
    problem_id     TEXT,
    last_heartbeat TIMESTAMP DEFAULT CURRENT_TIMESTAMP, -- UTC
    status         TEXT DEFAULT 'alive'
);
CREATE INDEX IF NOT EXISTS idx_workers_problem ON workers(problem_id);
