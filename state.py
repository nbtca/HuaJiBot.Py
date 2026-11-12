"""Persistent notification queue and explicitly enabled group history; no credentials."""

import sqlite3
import time


class State:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS targets (origin TEXT PRIMARY KEY)")
        self.db.execute("""CREATE TABLE IF NOT EXISTS queue (
            id TEXT PRIMARY KEY, origin TEXT NOT NULL, text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            due REAL NOT NULL, created REAL NOT NULL)""")
        self.db.commit()
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(queue)")}
        if "payload" not in columns:
            self.db.execute("ALTER TABLE queue ADD COLUMN payload TEXT")
            self.db.commit()
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self.db.execute("""CREATE TABLE IF NOT EXISTS history (
            origin TEXT, message_id TEXT, timestamp REAL, sender TEXT, text TEXT,
            PRIMARY KEY(origin,message_id))""")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS history_time ON history(origin,timestamp)"
        )
        self.db.commit()

    def get(self, key, default=None):
        import json

        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        import json

        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO kv VALUES (?,?)", (key, json.dumps(value))
            )

    def delete(self, key):
        with self.db:
            self.db.execute("DELETE FROM kv WHERE key=?", (key,))

    def record(self, origin, message_id, timestamp, sender, text):
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO history VALUES (?,?,?,?,?)",
                (origin, message_id, timestamp, sender[:100], text[:20000]),
            )

    def history(self, origin, start, end, limit=20000):
        rows = self.db.execute(
            "SELECT timestamp,sender,text FROM history WHERE origin=? AND timestamp>=? AND timestamp<? ORDER BY timestamp LIMIT ?",
            (origin, start, end, limit + 1),
        ).fetchall()
        if len(rows) > limit:
            raise ValueError("当天消息数量超过安全上限，请缩小记录范围")
        return rows

    def prune(self, cutoff):
        with self.db:
            self.db.execute("DELETE FROM history WHERE timestamp<?", (cutoff,))
            self.db.execute(
                "DELETE FROM queue WHERE status IN ('sent','cancelled') AND created<?",
                (time.time() - 7 * 86400,),
            )

    def counts(self):
        return dict(
            self.db.execute("SELECT status,count(*) FROM queue GROUP BY status")
        )

    def card_paths(self):
        import json

        paths = set()
        for row in self.db.execute(
            "SELECT payload FROM queue WHERE payload IS NOT NULL"
        ):
            payload = json.loads(row[0])
            if isinstance(payload, dict) and isinstance(payload.get("image"), str):
                paths.add(payload["image"])
        return paths

    def enqueue_parts(self, key, origin, text, due, payload=None):
        """Atomically accept every part and the dedupe marker, or accept nothing."""
        import json

        if not self.bound(origin):
            raise ValueError("target is not bound")
        if self.get("accepted:" + key):
            return
        parts = [text[i : i + 2500] for i in range(0, len(text), 2500)]
        now = time.time()
        with self.db:
            pending = self.db.execute(
                "SELECT count(*) FROM queue WHERE status='pending'"
            ).fetchone()[0]
            if pending + len(parts) > 1000:
                raise ValueError("notification queue is full")
            for index, part in enumerate(parts):
                self.db.execute(
                    "INSERT INTO queue(id,origin,text,due,created,payload) VALUES (?,?,?,?,?,?)",
                    (
                        f"{key}:{index}",
                        origin,
                        part,
                        due,
                        now,
                        json.dumps(payload) if payload and index == 0 else None,
                    ),
                )
            self.db.execute(
                "INSERT INTO kv VALUES (?,?)", ("accepted:" + key, json.dumps(True))
            )

    def schedule(self, key, origin, text, due):
        status = self.enqueue(key, origin, text)
        # Never move an existing retry's backoff back into the past.
        with self.db:
            self.db.execute(
                "UPDATE queue SET due=max(due,?) WHERE id=? AND attempts=0 AND status='pending'",
                (due, key),
            )
        return status

    def bind(self, origin):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO targets VALUES (?)", (origin,))

    def unbind(self, origin):
        with self.db:
            self.db.execute("DELETE FROM targets WHERE origin=?", (origin,))
            self.db.execute(
                "UPDATE queue SET status='cancelled' WHERE origin=? AND status='pending'",
                (origin,),
            )

    def bound(self, origin):
        return (
            self.db.execute(
                "SELECT 1 FROM targets WHERE origin=?", (origin,)
            ).fetchone()
            is not None
        )

    def enqueue(self, key, origin, text):
        if not self.bound(origin):
            raise ValueError("target is not bound")
        row = self.db.execute(
            "SELECT origin,text,status FROM queue WHERE id=?", (key,)
        ).fetchone()
        if row:
            if row[:2] != (origin, text):
                raise ValueError("idempotency key reused with different content")
            return row[2]
        now = time.time()
        with self.db:
            self.db.execute(
                "DELETE FROM queue WHERE status!='pending' AND created<?",
                (now - 7 * 86400,),
            )
            if (
                self.db.execute(
                    "SELECT count(*) FROM queue WHERE status='pending'"
                ).fetchone()[0]
                >= 100
            ):
                raise ValueError("notification queue is full")
            self.db.execute(
                "INSERT INTO queue(id,origin,text,due,created) VALUES (?,?,?,?,?)",
                (key, origin, text, now, now),
            )
        return "pending"

    def next(self):
        return self.db.execute(
            "SELECT id,origin,text,attempts FROM queue WHERE status='pending' AND due<=? ORDER BY created LIMIT 1",
            (time.time(),),
        ).fetchone()

    def payload(self, key):
        import json

        row = self.db.execute("SELECT payload FROM queue WHERE id=?", (key,)).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    def finish(self, key, status):
        with self.db:
            self.db.execute("UPDATE queue SET status=? WHERE id=?", (status, key))

    def retry(self, key, attempts):
        with self.db:
            self.db.execute(
                "UPDATE queue SET attempts=?,due=?,status=? WHERE id=?",
                (
                    attempts,
                    time.time() + min(300, 30 * 2**attempts),
                    "failed" if attempts >= 3 else "pending",
                    key,
                ),
            )

    def close(self):
        self.db.close()
