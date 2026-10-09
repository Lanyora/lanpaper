"""SQLite 建表、连接与访问封装。

设计要点：
- 内容只有一张主表 items（任务与文章同表）
- 动态只有一张主表 events（进度操作与评论同表）
- 所有访问经一把可重入锁串行化，避免多线程下 database is locked
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from server import util

SCHEMA_VERSION = "2"

SCHEMA = """
-- 1. 用户
CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  username      TEXT    NOT NULL UNIQUE,
  password_hash TEXT    NOT NULL,
  password_salt TEXT    NOT NULL,
  nickname      TEXT    NOT NULL,
  avatar_hash   TEXT,
  is_admin      INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT    NOT NULL,
  created_ip    TEXT    NOT NULL,
  last_login_at TEXT,
  last_login_ip TEXT
);
-- 用户名不区分大小写唯一，避免 Alice 与 alice 并存
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_nocase
  ON users(username COLLATE NOCASE);

-- 2. 会话（固定 session_hours 小时）
CREATE TABLE IF NOT EXISTS sessions (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  token        TEXT    NOT NULL UNIQUE,
  user_id      INTEGER NOT NULL REFERENCES users(id),
  created_at   TEXT    NOT NULL,
  expires_at   TEXT    NOT NULL,
  last_seen_at TEXT,
  login_ip     TEXT    NOT NULL,
  last_ip      TEXT,
  user_agent   TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

-- 3. 一次性邀请码
CREATE TABLE IF NOT EXISTS invite_codes (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  code       TEXT    NOT NULL UNIQUE,
  created_by INTEGER NOT NULL REFERENCES users(id),
  created_at TEXT    NOT NULL,
  expires_at TEXT    NOT NULL,
  used_by    INTEGER REFERENCES users(id),
  used_at    TEXT
);

-- 4. 任务模板
CREATE TABLE IF NOT EXISTS task_templates (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT    NOT NULL,
  payload    TEXT    NOT NULL DEFAULT '{}',
  created_by INTEGER NOT NULL REFERENCES users(id),
  created_at TEXT    NOT NULL
);

-- 5. 内容主表：任务与文章同表
CREATE TABLE IF NOT EXISTS items (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  kind               TEXT    NOT NULL CHECK (kind IN ('task','article')),
  title              TEXT    NOT NULL,
  body_md            TEXT    NOT NULL DEFAULT '',
  author_id          INTEGER NOT NULL REFERENCES users(id),
  created_at         TEXT    NOT NULL,
  created_ip         TEXT    NOT NULL,
  -- 最后一次有动作的时间（写事件时同步刷新）。列表按它排序，符合"最近动过的在最前"的直觉
  last_activity_at   TEXT,
  end_at             TEXT,
  view_count         INTEGER NOT NULL DEFAULT 0,
  deleted_at         TEXT,
  deleted_by         INTEGER REFERENCES users(id),
  source_template_id INTEGER REFERENCES task_templates(id)
);
CREATE INDEX IF NOT EXISTS idx_items_feed   ON items(kind, deleted_at, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_author ON items(author_id);
-- 注意：items 的 last_activity_at 索引不能写在这里。
-- 老库的 items 表已存在但没有该列（CREATE TABLE IF NOT EXISTS 会跳过），
-- 在这里建索引会失败。它由 _migrate() 在补完列之后创建。

-- 6. 进度项
CREATE TABLE IF NOT EXISTS progress_items (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id    INTEGER NOT NULL REFERENCES items(id),
  seq        INTEGER NOT NULL,
  title      TEXT    NOT NULL,
  status     TEXT    NOT NULL DEFAULT 'pending'
             CHECK (status IN ('pending','done','blocked','delayed')),
  cursor     INTEGER NOT NULL DEFAULT 0,
  total      INTEGER NOT NULL DEFAULT 1,
  note       TEXT    NOT NULL DEFAULT '',
  done_at    TEXT,
  created_at TEXT    NOT NULL,
  UNIQUE (item_id, seq)
);

-- 7. 统一事件流：进度操作、评论、待办变更共用一张表
--    item_id / todo_id 二者取一：内容类事件挂 item，待办类事件挂 todo
--    todo_id 用 ON DELETE SET NULL：待办删掉后事件还在，标题已写进 message，留痕不丢
CREATE TABLE IF NOT EXISTS events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id     INTEGER REFERENCES items(id),
  todo_id     INTEGER REFERENCES todos(id) ON DELETE SET NULL,
  progress_id INTEGER REFERENCES progress_items(id),
  actor_id    INTEGER NOT NULL REFERENCES users(id),
  actor_ip    TEXT    NOT NULL,
  type        TEXT    NOT NULL,
  message     TEXT    NOT NULL DEFAULT '',
  payload     TEXT    NOT NULL DEFAULT '{}',
  created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_item     ON events(item_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_time     ON events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_progress ON events(progress_id);

-- 8. 标签
CREATE TABLE IF NOT EXISTS tags (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT    NOT NULL UNIQUE,
  created_at TEXT    NOT NULL
);

-- 9. 内容与标签的关联
CREATE TABLE IF NOT EXISTS item_tags (
  item_id INTEGER NOT NULL REFERENCES items(id),
  tag_id  INTEGER NOT NULL REFERENCES tags(id),
  PRIMARY KEY (item_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_item_tags_tag ON item_tags(tag_id, item_id);

-- 10. 图片（内容寻址）
CREATE TABLE IF NOT EXISTS attachments (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  hash        TEXT    NOT NULL UNIQUE,
  ext         TEXT    NOT NULL,
  mime        TEXT    NOT NULL,
  size        INTEGER NOT NULL,
  width       INTEGER,
  height      INTEGER,
  uploader_id INTEGER NOT NULL REFERENCES users(id),
  created_at  TEXT    NOT NULL,
  ref_count   INTEGER NOT NULL DEFAULT 0
);

-- 11. 定时规则
CREATE TABLE IF NOT EXISTS recurring_rules (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  title       TEXT    NOT NULL,
  freq        TEXT    NOT NULL CHECK (freq IN ('daily','weekly')),
  weekday     INTEGER,          -- 旧字段，保留兼容；新逻辑用 weekdays
  weekdays    TEXT,             -- 每周多选，逗号分隔，如 "1,2,3,4,5"（0=周一）
  time_of_day TEXT    NOT NULL,
  template_id INTEGER REFERENCES task_templates(id),
  payload     TEXT    NOT NULL DEFAULT '{}',
  enabled     INTEGER NOT NULL DEFAULT 1,
  created_by  INTEGER NOT NULL REFERENCES users(id),
  created_at  TEXT    NOT NULL,
  last_run_at TEXT
);

-- 12. 待办清单（日常待办：定时点检表、手动添加的杂事）
--     与"任务"分开：任务有阶段和进度，待办只有"做没做"
CREATE TABLE IF NOT EXISTS todos (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  title      TEXT    NOT NULL,
  done       INTEGER NOT NULL DEFAULT 0,
  done_at    TEXT,
  done_by    INTEGER REFERENCES users(id),
  created_by INTEGER REFERENCES users(id),
  due_date   TEXT,              -- YYYY-MM-DD，空表示长期有效
  source     TEXT    NOT NULL DEFAULT 'manual',   -- manual | recurring
  rule_id    INTEGER REFERENCES recurring_rules(id),
  created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_todos_pending ON todos(done, due_date, id);

-- 13. 元数据
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- 14. 访问明细（二期启用；一期只用 items.view_count）
CREATE TABLE IF NOT EXISTS view_logs (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id   INTEGER NOT NULL REFERENCES items(id),
  user_id   INTEGER REFERENCES users(id),
  ip        TEXT,
  viewed_at TEXT    NOT NULL
);
"""

# 老库缺列时补上（CREATE TABLE IF NOT EXISTS 不会给已存在的表加列）
MIGRATIONS = [
    ("items", "last_activity_at", "TEXT"),
    ("recurring_rules", "weekdays", "TEXT"),
]


class DB:
    """一把锁串行化所有数据库访问。

    本工具的并发量是"一个小组十几个人"，串行化带来的性能损失可以忽略，
    换来的是彻底不用操心多线程竞争。
    """

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute("PRAGMA foreign_keys = ON")
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA synchronous = NORMAL")

    # ------------------------------------------------------------ 建表

    def init_schema(self) -> None:
        with self.lock:
            self.conn.executescript(SCHEMA)
            self._migrate()
            row = self.conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                    (SCHEMA_VERSION,),
                )
                self.conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('initialized_at', ?)",
                    (util.now_iso(),),
                )
            elif row["value"] != SCHEMA_VERSION:
                self.conn.execute(
                    "UPDATE meta SET value = ? WHERE key = 'schema_version'",
                    (SCHEMA_VERSION,),
                )
            self.conn.commit()

    def _migrate(self) -> None:
        """给已存在的老库补列并回填。全部幂等，可反复执行。"""
        for table, column, decl in MIGRATIONS:
            cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

        self._migrate_events()

        # 建在"新增列"上的索引必须等列补好之后再建，否则老库会报 no such column
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_items_activity ON items(deleted_at, last_activity_at DESC)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_todo ON events(todo_id, created_at DESC)"
        )

        # 最后活动时间：优先取该内容最新一条事件的时间，其次保留原值，最后退回创建时间
        self.conn.execute(
            """UPDATE items
               SET last_activity_at = COALESCE(
                     (SELECT MAX(e.created_at) FROM events e WHERE e.item_id = items.id),
                     last_activity_at, created_at)
               WHERE last_activity_at IS NULL
                  OR last_activity_at < COALESCE(
                     (SELECT MAX(e.created_at) FROM events e WHERE e.item_id = items.id),
                     last_activity_at)"""
        )

    def _migrate_events(self) -> None:
        """让事件流能挂待办：老库的 events.item_id 是 NOT NULL，
        SQLite 改不了列约束，只能重建表再把数据搬过去。"""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(events)")}
        if "todo_id" in cols:
            return
        self.conn.executescript(
            """
            CREATE TABLE events_new (
              id          INTEGER PRIMARY KEY AUTOINCREMENT,
              item_id     INTEGER REFERENCES items(id),
              todo_id     INTEGER REFERENCES todos(id) ON DELETE SET NULL,
              progress_id INTEGER REFERENCES progress_items(id),
              actor_id    INTEGER NOT NULL REFERENCES users(id),
              actor_ip    TEXT    NOT NULL,
              type        TEXT    NOT NULL,
              message     TEXT    NOT NULL DEFAULT '',
              payload     TEXT    NOT NULL DEFAULT '{}',
              created_at  TEXT    NOT NULL
            );
            INSERT INTO events_new
              (id, item_id, todo_id, progress_id, actor_id, actor_ip,
               type, message, payload, created_at)
              SELECT id, item_id, NULL, progress_id, actor_id, actor_ip,
                     type, message, payload, created_at FROM events;
            DROP TABLE events;
            ALTER TABLE events_new RENAME TO events;
            CREATE INDEX IF NOT EXISTS idx_events_item     ON events(item_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_events_time     ON events(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_events_progress ON events(progress_id);
            """
        )

    # ------------------------------------------------------------ 读写

    def execute(self, sql: str, params=()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def executemany(self, sql: str, seq) -> None:
        with self.lock:
            self.conn.executemany(sql, seq)
            self.conn.commit()

    def query(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def queryone(self, sql: str, params=()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params=()):
        row = self.queryone(sql, params)
        return None if row is None else row[0]

    def last_id(self) -> int:
        return int(self.scalar("SELECT last_insert_rowid()") or 0)

    def close(self) -> None:
        with self.lock:
            self.conn.close()
