import fcntl
import json
import sqlite3
import time
from datetime import datetime, timezone

PRUNE_AFTER = 7 * 86400

_SCHEMA = """
CREATE TABLE IF NOT EXISTS turn_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,              -- 哪个入口投的：web / tg / wake / test…
    payload     TEXT NOT NULL,              -- JSON：入口自定，恢复重投时对照用
    idem_key    TEXT UNIQUE,                -- 幂等键；NULL 允许多行（SQLite UNIQUE 语义）
    status      TEXT NOT NULL DEFAULT 'pending',
    result      TEXT,                       -- done 时的结果 JSON（尽力序列化）
    error       TEXT,                       -- failed/abandoned/orphaned 的原因
    created_at  REAL NOT NULL,              -- epoch 秒（比较用）
    created_iso TEXT NOT NULL,              -- 人看的
    last_seen   REAL NOT NULL,              -- 等待者心跳；abandon 判据只看它
    started_at  REAL,
    finished_at REAL
);
"""

_TERMINAL = ("done", "failed", "abandoned", "orphaned")

class TurnQueueError(Exception):
    pass

class TurnFailedElsewhere(TurnQueueError):
    pass

class TurnAbandoned(TurnQueueError):
    pass

class TurnOrphaned(TurnQueueError):
    pass

class TurnWaitTimeout(TurnQueueError):
    pass

def queue_path(db_path):
    return str(db_path) + ".turn_queue.db"

def lock_path(db_path):
    return str(db_path) + ".turn_queue.lock"

def _conn(db_path):
    conn = sqlite3.connect(queue_path(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn

def _row_dict(row):
    d = dict(row)
    if d.get("payload"):
        try:
            d["payload"] = json.loads(d["payload"])
        except (json.JSONDecodeError, TypeError):
            pass
    return d

def submit(db_path, source, payload, idem_key=None):
    now = time.time()
    iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = _conn(db_path)
    try:
        conn.execute(
            "DELETE FROM turn_queue WHERE status IN ('done','failed','abandoned') "
            "AND finished_at IS NOT NULL AND finished_at < ?", (now - PRUNE_AFTER,))
        if idem_key is not None:
            row = conn.execute("SELECT * FROM turn_queue WHERE idem_key = ?", (idem_key,)).fetchone()
            if row is not None:
                conn.commit()
                return {**_row_dict(row), "dup": True}
        try:
            cur = conn.execute(
                "INSERT INTO turn_queue(source, payload, idem_key, created_at, created_iso, last_seen) "
                "VALUES (?,?,?,?,?,?)",
                (source, json.dumps(payload, ensure_ascii=False, default=str), idem_key, now, iso, now))
        except sqlite3.IntegrityError:
            conn.commit()
            row = conn.execute("SELECT * FROM turn_queue WHERE idem_key = ?", (idem_key,)).fetchone()
            return {**_row_dict(row), "dup": True}
        conn.commit()
        row = conn.execute("SELECT * FROM turn_queue WHERE id = ?", (cur.lastrowid,)).fetchone()
        return {**_row_dict(row), "dup": False}
    finally:
        conn.close()

def run(db_path, item_id, fn, wait_timeout=1800.0, abandon_after=15.0, poll=0.05):
    lock = open(lock_path(db_path), "a+")
    deadline = time.monotonic() + wait_timeout
    try:
        while True:
            conn = _conn(db_path)
            try:
                conn.execute("UPDATE turn_queue SET last_seen = ? WHERE id = ? AND status = 'pending'",
                             (time.time(), item_id))
                conn.commit()
                row = conn.execute("SELECT * FROM turn_queue WHERE id = ?", (item_id,)).fetchone()
            finally:
                conn.close()
            if row is None:
                raise TurnQueueError(f"排队本里没有 id={item_id} 这一项 —— 它没经过 submit()，或被清理了")
            st = row["status"]
            if st == "done":
                return json.loads(row["result"]) if row["result"] else None
            if st == "failed":
                raise TurnFailedElsewhere(f"这一轮已执行过且失败（不重跑）：{row['error']}")
            if st == "abandoned":
                raise TurnAbandoned(f"这一项排队时被标记放弃（没执行过）：{row['error']}")
            if st == "orphaned":
                raise TurnOrphaned(f"这一项执行中途进程死了，账本可能已有内容，不自动重跑：{row['error']}")

            got = False
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
            except OSError:
                pass

            if got:
                claimed = False
                try:
                    conn = _conn(db_path)
                    try:
                        row = conn.execute("SELECT status FROM turn_queue WHERE id = ?", (item_id,)).fetchone()
                        st = row["status"] if row else None
                        if st == "running":
                            conn.execute(
                                "UPDATE turn_queue SET status='orphaned', finished_at=?, error=? "
                                "WHERE id=? AND status='running'",
                                (time.time(), "执行进程中途死亡；账本里可能已有这一轮的部分内容", item_id))
                            conn.commit()
                            continue
                        if st == "pending":
                            head = conn.execute(
                                "SELECT id, last_seen FROM turn_queue WHERE status='pending' "
                                "ORDER BY id LIMIT 1").fetchone()
                            if head["id"] == item_id:
                                cur = conn.execute(
                                    "UPDATE turn_queue SET status='running', started_at=? "
                                    "WHERE id=? AND status='pending'", (time.time(), item_id))
                                conn.commit()
                                claimed = cur.rowcount == 1
                            elif time.time() - head["last_seen"] > abandon_after:
                                conn.execute(
                                    "UPDATE turn_queue SET status='abandoned', finished_at=?, error=? "
                                    "WHERE id=? AND status='pending'",
                                    (time.time(), f"等待者心跳停了超过 {abandon_after}s，判定已离场，未执行",
                                     head["id"]))
                                conn.commit()
                    finally:
                        conn.close()

                    if claimed:
                        try:
                            result = fn()
                        except BaseException as e:
                            _finish(db_path, item_id, "failed", error=f"{type(e).__name__}: {e}")
                            raise
                        _finish(db_path, item_id, "done", result=result)
                        return result
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)

            if time.monotonic() > deadline:
                raise TurnWaitTimeout(
                    f"排队等待超过 {wait_timeout}s（id={item_id} 仍是 pending，没执行）。"
                    "前面那一轮是不是卡死了？锁文件：" + lock_path(db_path))
            time.sleep(poll)
    finally:
        lock.close()

def _finish(db_path, item_id, status, result=None, error=None):
    blob = None
    if result is not None:
        try:
            blob = json.dumps(result, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            blob = json.dumps({"_unserializable": str(result)[:2000]}, ensure_ascii=False)
    conn = _conn(db_path)
    try:
        conn.execute(
            "UPDATE turn_queue SET status=?, finished_at=?, result=?, error=? WHERE id=?",
            (status, time.time(), blob, error, item_id))
        conn.commit()
    finally:
        conn.close()

def items(db_path, status=None):
    conn = _conn(db_path)
    try:
        if status:
            rows = conn.execute("SELECT * FROM turn_queue WHERE status=? ORDER BY id", (status,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM turn_queue ORDER BY id").fetchall()
        return [_row_dict(r) for r in rows]
    finally:
        conn.close()
