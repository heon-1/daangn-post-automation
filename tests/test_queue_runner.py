import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from uploader.browser import StopBatch, read_config
from uploader.queue import Queue, read_posts
from uploader.runner import run_batch


class FakePublisher:
    def __init__(self, prepare_error=None, submit_error=None):
        self.prepared = []
        self.submitted = []
        self.prepare_error = prepare_error
        self.submit_error = submit_error

    def prepare(self, payload):
        self.prepared.append(payload["id"])
        if self.prepare_error:
            raise self.prepare_error

    def submit(self):
        self.submitted.append(self.prepared[-1])
        if self.submit_error:
            raise self.submit_error
        return "https://example.test/posts/" + self.prepared[-1]


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.queue = Queue(self.root / "queue.sqlite3")

    def tearDown(self):
        self.queue.close()
        self.temp.cleanup()

    def import_count(self, count):
        rows = [{"id": f"p-{i}", "title": f"제목 {i}", "body": f"본문 {i}", "images": []} for i in range(count)]
        source = self.root / "posts.json"
        source.write_text(json.dumps(rows), encoding="utf-8")
        return self.queue.import_posts(read_posts(source))

    def run_batch(self, publisher, limit=1000, checkpoint=lambda: None):
        with contextlib.redirect_stdout(io.StringIO()):
            return run_batch(self.queue, publisher, limit, 3, lambda _: None, 1, 1, checkpoint)

    def test_thousand_posts_are_not_published_again_on_resume(self):
        self.assertEqual(self.import_count(1000), (1000, 0))
        publisher = FakePublisher()
        self.assertEqual(self.run_batch(publisher, 400), 400)
        self.assertEqual(self.run_batch(publisher, 1000), 600)
        self.assertEqual(self.run_batch(publisher), 0)
        self.assertEqual(len(set(publisher.submitted)), 1000)
        self.assertEqual(self.queue.summary()["counts"], {"posted": 1000})

    def test_reimport_skips_duplicates(self):
        self.import_count(3)
        self.assertEqual(self.import_count(3), (0, 3))

    def test_changed_id_rolls_back_entire_import(self):
        self.import_count(1)
        posts = [({"id": "new", "title": "new", "body": "new", "images": []}, "new-hash"),
                 ({"id": "p-0", "title": "changed", "body": "changed", "images": []}, "changed-hash")]
        with self.assertRaises(ValueError):
            self.queue.import_posts(posts)
        self.assertEqual(self.queue.summary()["counts"], {"pending": 1})

    def test_same_content_with_different_id_is_deduplicated(self):
        self.import_count(1)
        row = self.queue.next()
        payload = json.loads(row["payload"])
        payload["id"] = "another-id"
        self.assertEqual(self.queue.import_posts([(payload, row["fingerprint"])]), (0, 1))

    def test_submission_timeout_is_never_automatically_retried(self):
        self.import_count(2)
        publisher = FakePublisher(submit_error=TimeoutError("response lost"))
        with self.assertRaises(TimeoutError):
            self.run_batch(publisher)
        self.assertEqual(self.queue.summary()["counts"], {"pending": 1, "review": 1})
        with self.assertRaises(ValueError):
            self.run_batch(publisher)
        self.assertEqual(len(publisher.submitted), 1)
        self.queue.resolve("p-0", "posted")
        resumed = FakePublisher()
        self.assertEqual(self.run_batch(resumed), 1)
        self.assertEqual(resumed.submitted, ["p-1"])

    def test_crash_during_submission_becomes_review(self):
        self.import_count(1)
        self.queue.begin("p-0")
        self.queue.set_status("p-0", "submitting")
        with self.assertRaises(ValueError):
            self.run_batch(FakePublisher())
        self.assertEqual(self.queue.summary()["counts"], {"review": 1})

    def test_crash_during_preparation_is_resumable(self):
        self.import_count(1)
        self.queue.begin("p-0")
        self.queue.db.execute("UPDATE posts SET attempts=3")
        self.queue.db.commit()
        self.assertEqual(self.run_batch(FakePublisher()), 1)

    def test_auth_expiry_stops_without_consuming_retry_budget(self):
        self.import_count(1)
        for _ in range(4):
            with self.assertRaises(StopBatch):
                self.run_batch(FakePublisher(prepare_error=StopBatch("HTTP 401")))
        row = self.queue.next()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(self.run_batch(FakePublisher()), 1)

    def test_rate_limit_after_submit_requires_review(self):
        self.import_count(1)
        with self.assertRaises(StopBatch):
            self.run_batch(FakePublisher(submit_error=StopBatch("HTTP 429")))
        self.assertEqual(self.queue.summary()["counts"], {"review": 1})

    def test_preparation_retries_are_bounded(self):
        self.import_count(1)
        publisher = FakePublisher(prepare_error=RuntimeError("connection lost"))
        with self.assertRaises(RuntimeError):
            self.run_batch(publisher)
        self.assertEqual(len(publisher.prepared), 3)
        self.assertEqual(publisher.submitted, [])
        self.assertEqual(self.queue.summary()["counts"], {"failed": 1})

    def test_checkpoint_failure_preserves_success(self):
        self.import_count(1)
        def fail():
            raise OSError("disk full")
        with self.assertRaises(OSError):
            self.run_batch(FakePublisher(), checkpoint=fail)
        self.assertEqual(self.queue.summary()["counts"], {"posted": 1})
        self.assertEqual(self.run_batch(FakePublisher()), 0)

    def test_same_queue_cannot_be_used_by_two_processes(self):
        with self.assertRaises(ValueError):
            Queue(self.root / "queue.sqlite3")

    def test_destination_cannot_change(self):
        self.queue.bind("account-a:cafe-a:board-a")
        with self.assertRaises(ValueError):
            self.queue.bind("account-b:cafe-a:board-a")

    def test_missing_image_rejects_input(self):
        source = self.root / "posts.json"
        source.write_text(json.dumps([{"id": "p", "title": "title", "body": "body", "images": ["missing.jpg"]}]))
        with self.assertRaises(ValueError):
            read_posts(source)

    def test_owner_only_db_permissions(self):
        self.assertEqual(self.queue.path.stat().st_mode & 0o777, 0o600)

    def test_login_can_be_configured_before_editor_is_known(self):
        path = self.root / "config.json"
        path.write_text(json.dumps({"login_url": "https://cafe.daangn.com/"}))
        self.assertEqual(read_config(path, login_only=True)["login_url"], "https://cafe.daangn.com/")
        with self.assertRaises(ValueError):
            read_config(path)


if __name__ == "__main__":
    unittest.main()
