import csv
import fcntl
import hashlib
import json
import sqlite3
from pathlib import Path


def read_posts(source):
    source = Path(source).resolve()
    if source.suffix.lower() == ".csv":
        with source.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    elif source.suffix.lower() == ".json":
        rows = json.loads(source.read_text(encoding="utf-8-sig"))
    else:
        raise ValueError("입력 파일은 JSON 또는 CSV여야 합니다.")
    if not isinstance(rows, list) or not rows:
        raise ValueError("글 목록은 비어 있지 않은 배열이어야 합니다.")
    posts = []
    image_hashes = {}
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"{index}번 글이 객체가 아닙니다.")
        for field in ("id", "title", "body"):
            if not isinstance(row.get(field), str) or not row[field].strip():
                raise ValueError(f"{index}번 글의 {field}는 빈 값이 아닌 문자열이어야 합니다.")
        images = row.get("images") or []
        if isinstance(images, str):
            images = json.loads(images)
        if not isinstance(images, list) or any(not isinstance(p, str) for p in images):
            raise ValueError(f"{index}번 글의 images는 경로 배열이어야 합니다.")
        resolved, digests = [], []
        for image in images:
            path = (source.parent / image).resolve()
            if not path.is_file():
                raise ValueError(f"{index}번 글의 이미지가 없습니다: {path}")
            key = str(path)
            if key not in image_hashes:
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                image_hashes[key] = digest.hexdigest()
            resolved.append(key)
            digests.append(image_hashes[key])
        payload = {"id": row["id"].strip(), "title": row["title"], "body": row["body"], "images": resolved}
        signature = json.dumps([payload["title"], payload["body"], digests], ensure_ascii=False)
        posts.append((payload, hashlib.sha256(signature.encode()).hexdigest()))
    return posts


class Queue:
    """A single-process queue; commit submission intent before touching the website."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = self.path.with_suffix(".lock").open("a+")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise ValueError("다른 업로더가 같은 큐를 사용 중입니다.")
        try:
            self.db = sqlite3.connect(str(self.path))
            self.db.row_factory = sqlite3.Row
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS posts (
                    id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    result_url TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            self.path.chmod(0o600)
        except BaseException:
            self.lock.close()
            raise

    def close(self):
        self.db.close()
        self.lock.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def bind(self, target):
        with self.db:
            current = self.db.execute("SELECT value FROM metadata WHERE key='target'").fetchone()
            if current and current[0] != target:
                raise ValueError("이 큐는 다른 계정·게시판에 연결되어 있습니다. 별도 --db를 사용하세요.")
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('target', ?)", (target,))

    def import_posts(self, posts):
        added = skipped = 0
        with self.db:
            for payload, fingerprint in posts:
                existing = self.db.execute("SELECT fingerprint FROM posts WHERE id=?", (payload["id"],)).fetchone()
                if existing:
                    if existing[0] != fingerprint:
                        raise ValueError(f"기존 id의 내용이 달라졌습니다: {payload['id']}")
                    skipped += 1
                    continue
                if self.db.execute("SELECT 1 FROM posts WHERE fingerprint=?", (fingerprint,)).fetchone():
                    skipped += 1
                    continue
                self.db.execute("INSERT INTO posts(id, fingerprint, payload) VALUES (?, ?, ?)",
                                (payload["id"], fingerprint, json.dumps(payload, ensure_ascii=False)))
                added += 1
        return added, skipped

    def recover(self):
        with self.db:
            self.db.execute("UPDATE posts SET status='pending', attempts=max(0, attempts-1), updated_at=CURRENT_TIMESTAMP WHERE status='preparing'")
            self.db.execute("UPDATE posts SET status='review', error='등록 중 프로세스가 종료되었습니다. 게시판에서 확인하세요.', updated_at=CURRENT_TIMESTAMP WHERE status='submitting'")

    def next(self, max_attempts=3):
        return self.db.execute("SELECT * FROM posts WHERE status='pending' AND attempts < ? ORDER BY rowid LIMIT 1", (max_attempts,)).fetchone()

    def pending_ids(self, max_attempts=3):
        return [row[0] for row in self.db.execute("SELECT id FROM posts WHERE status='pending' AND attempts < ? ORDER BY rowid", (max_attempts,))]

    def claim(self, post_id, max_attempts=3):
        # All coroutines share one event-loop thread. No await may split this transaction.
        with self.db:
            row = self.db.execute("SELECT * FROM posts WHERE id=? AND status='pending' AND attempts < ?", (post_id, max_attempts)).fetchone()
            if row is None:
                return None
            changed = self.db.execute("UPDATE posts SET status='preparing', attempts=attempts+1, error=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'", (post_id,)).rowcount
            return row if changed == 1 else None

    def begin(self, post_id):
        with self.db:
            self.db.execute("UPDATE posts SET status='preparing', attempts=attempts+1, error=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?", (post_id,))

    def refund_attempt(self, post_id):
        with self.db:
            self.db.execute("UPDATE posts SET attempts=max(0, attempts-1) WHERE id=?", (post_id,))

    def set_status(self, post_id, status, error=None, url=None):
        if status not in {"pending", "preparing", "submitting", "posted", "failed", "review"}:
            raise ValueError("알 수 없는 상태입니다.")
        with self.db:
            self.db.execute("UPDATE posts SET status=?, error=?, result_url=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, error, url, post_id))

    def summary(self):
        counts = dict(self.db.execute("SELECT status, count(*) FROM posts GROUP BY status"))
        attention = [dict(row) for row in self.db.execute("SELECT id, status, attempts, error, result_url FROM posts WHERE status IN ('failed', 'review', 'submitting') ORDER BY rowid")]
        return {"counts": counts, "attention": attention}

    def resolve(self, post_id, outcome, url=None):
        row = self.db.execute("SELECT status FROM posts WHERE id=?", (post_id,)).fetchone()
        if not row or row[0] not in {"review", "failed"}:
            raise ValueError("review 또는 failed 상태의 글만 확인 처리할 수 있습니다.")
        self.set_status(post_id, outcome, url=url)
        if outcome == "pending":
            with self.db:
                self.db.execute("UPDATE posts SET attempts=0 WHERE id=?", (post_id,))
