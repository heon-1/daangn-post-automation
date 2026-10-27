import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from uploader.browser import StopBatch
from uploader.parallel import run_parallel_batch
from uploader.queue import Queue, read_posts


class ParallelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.queue = Queue(self.root / "queue.sqlite3")
        self.prepared, self.submitted = [], []
        self.active = self.peak = 0
        self.failures = {}
        self.fail_submit = None
        self.checkpoints_active = self.checkpoints_peak = 0

    async def asyncTearDown(self):
        self.queue.close()
        self.temp.cleanup()

    def import_count(self, count):
        path = self.root / "posts.json"
        path.write_text(json.dumps([{"id": f"p-{i}", "title": f"AI 글 {i}", "body": f"본문 {i}", "images": []} for i in range(count)]))
        self.queue.import_posts(read_posts(path))

    async def factory(self, worker, signal):
        owner = self
        class Publisher:
            async def prepare(self, payload):
                self.post_id = payload["id"]
                owner.prepared.append(self.post_id)
                owner.active += 1
                owner.peak = max(owner.peak, owner.active)
                try:
                    await asyncio.sleep(0.001)
                    if owner.failures.get(self.post_id, 0):
                        owner.failures[self.post_id] -= 1
                        raise RuntimeError("temporary preparation failure")
                finally:
                    owner.active -= 1

            async def submit(self):
                owner.submitted.append(self.post_id)
                if owner.fail_submit == self.post_id:
                    raise StopBatch("HTTP 429")
                await asyncio.sleep(0)
                return "https://example.test/posts/" + self.post_id

            async def close(self):
                pass
        return Publisher()

    async def checkpoint(self):
        self.checkpoints_active += 1
        self.checkpoints_peak = max(self.checkpoints_peak, self.checkpoints_active)
        await asyncio.sleep(0)
        self.checkpoints_active -= 1

    async def run_batch(self, **options):
        publisher_factory = options.pop("publisher_factory", self.factory)
        options = {"limit": 1000, "workers": 3, "interval_min": 0, "interval_max": 0,
                   "retry_delay": 0, "seed": 7, "checkpoint": self.checkpoint, **options}
        with contextlib.redirect_stdout(io.StringIO()):
            return await run_parallel_batch(self.queue, publisher_factory, **options)

    async def test_thousand_random_parallel_posts_resume_without_duplicates(self):
        self.import_count(1000)
        self.assertEqual(await self.run_batch(limit=430), 430)
        self.assertEqual(await self.run_batch(), 570)
        self.assertEqual(await self.run_batch(), 0)
        self.assertEqual(len(set(self.submitted)), 1000)
        self.assertEqual(self.queue.summary()["counts"], {"posted": 1000})
        self.assertGreater(self.peak, 1)
        self.assertLessEqual(self.peak, 3)
        self.assertEqual(self.checkpoints_peak, 1)

    async def test_retry_keeps_reservation_and_does_not_publish_twice(self):
        self.import_count(12)
        self.failures["p-0"] = 2
        self.assertEqual(await self.run_batch(), 12)
        self.assertEqual(self.prepared.count("p-0"), 3)
        self.assertEqual(self.submitted.count("p-0"), 1)

    async def test_twenty_workers_publish_unique_posts_and_skip_completed(self):
        self.import_count(40)
        self.assertEqual(await self.run_batch(workers=20, checkpoint_every=10), 40)
        self.assertEqual(await self.run_batch(workers=20), 0)
        self.assertEqual(len(self.submitted), 40)
        self.assertEqual(len(set(self.submitted)), 40)
        self.assertEqual(self.peak, 20)
        self.assertEqual(self.checkpoints_peak, 1)
        self.assertEqual(self.queue.summary()["counts"], {"posted": 40})

    async def test_one_failure_stops_new_work_and_keeps_other_confirmed_posts(self):
        self.import_count(20)
        self.fail_submit = "p-0"
        with self.assertRaises(StopBatch):
            await self.run_batch(order="fifo")
        counts = self.queue.summary()["counts"]
        self.assertEqual(counts.get("review"), 1)
        self.assertEqual(counts.get("preparing", 0), 0)
        self.assertEqual(counts.get("submitting", 0), 0)
        self.assertGreaterEqual(counts.get("pending", 0), 17)
        self.assertLessEqual(len(self.prepared), 3)
        with self.assertRaises(ValueError):
            await self.run_batch()

    async def test_failed_preparation_has_no_duplicate_claims(self):
        self.import_count(10)
        self.failures["p-0"] = 100
        with self.assertRaises(RuntimeError):
            await self.run_batch(order="fifo")
        self.assertEqual(self.prepared.count("p-0"), 3)
        self.assertNotIn("p-0", self.submitted)
        self.assertEqual(self.queue.db.execute("SELECT status FROM posts WHERE id='p-0'").fetchone()[0], "failed")

    async def test_seed_repeats_assignment_order(self):
        self.import_count(20)
        await self.run_batch(workers=1)
        first = self.prepared[:]
        with self.queue.db:
            self.queue.db.execute("UPDATE posts SET status='pending', attempts=0")
        self.prepared.clear()
        await self.run_batch(workers=1)
        self.assertEqual(self.prepared, first)
        self.assertNotEqual(first, [f"p-{i}" for i in range(20)])

    async def test_claim_cannot_take_already_reserved_post(self):
        self.import_count(1)
        self.assertIsNotNone(self.queue.claim("p-0"))
        self.assertIsNone(self.queue.claim("p-0"))

    async def test_delay_stays_within_requested_range(self):
        self.import_count(6)
        delays = []
        async def pause(seconds):
            delays.append(seconds)
            await asyncio.sleep(0)
        await self.run_batch(workers=1, interval_min=3, interval_max=9, pause=pause)
        self.assertEqual(len(delays), 5)
        self.assertTrue(all(3 <= seconds <= 9 for seconds in delays))

    async def test_checkpoint_failure_preserves_recorded_success(self):
        self.import_count(1)
        async def fail():
            raise OSError("disk full")
        with self.assertRaises(OSError):
            await self.run_batch(checkpoint=fail)
        self.assertEqual(self.queue.summary()["counts"], {"posted": 1})

    async def test_bulk_thousand_posts_flush_checkpoint_and_resume(self):
        self.import_count(1000)
        snapshots = []
        async def checkpoint():
            snapshots.append(self.queue.summary()["counts"].get("posted", 0))
            await self.checkpoint()
        self.assertEqual(await self.run_batch(limit=437, workers=8, checkpoint_every=10, checkpoint=checkpoint), 437)
        self.assertEqual(snapshots[-1], 437)
        self.assertEqual(self.queue.summary()["counts"], {"posted": 437, "pending": 563})
        self.assertEqual(await self.run_batch(workers=8, checkpoint_every=10, checkpoint=checkpoint), 563)
        self.assertEqual(snapshots[-1], 1000)
        self.assertEqual(len(set(self.submitted)), 1000)
        self.assertLessEqual(len(snapshots), 101)
        self.assertEqual(self.peak, 8)
        self.assertEqual(self.checkpoints_peak, 1)

    async def test_stop_flushes_successful_partial_checkpoint_group(self):
        self.import_count(20)
        self.fail_submit = "p-0"
        snapshots = []
        inflight = 0
        all_submitting = asyncio.Event()
        async def factory(index, signal):
            publisher = await self.factory(index, signal)
            original_submit = publisher.submit
            async def submit():
                nonlocal inflight
                inflight += 1
                if inflight == 8:
                    all_submitting.set()
                await all_submitting.wait()
                return await original_submit()
            publisher.submit = submit
            return publisher
        async def checkpoint():
            snapshots.append(self.queue.summary()["counts"].get("posted", 0))
        with self.assertRaises(StopBatch):
            await self.run_batch(order="fifo", workers=8, checkpoint_every=10,
                                 checkpoint=checkpoint, publisher_factory=factory)
        counts = self.queue.summary()["counts"]
        self.assertEqual(counts.get("review"), 1)
        self.assertGreater(counts.get("posted", 0), 0)
        self.assertEqual(snapshots, [counts["posted"]])
        self.assertEqual(counts.get("preparing", 0) + counts.get("submitting", 0), 0)

    async def test_final_checkpoint_failure_keeps_all_posted_results(self):
        self.import_count(7)
        async def fail():
            raise OSError("disk full")
        with self.assertRaises(OSError):
            await self.run_batch(workers=8, checkpoint_every=10, checkpoint=fail)
        self.assertEqual(self.queue.summary()["counts"], {"posted": 7})
        self.assertEqual(len(set(self.submitted)), 7)

    async def test_selected_batch_leaves_other_pending_posts_untouched(self):
        self.import_count(12)
        selected = ["p-3", "p-7", "p-10"]
        self.assertEqual(await self.run_batch(workers=8, post_ids=selected), 3)
        self.assertEqual(set(self.submitted), set(selected))
        self.assertEqual(self.queue.summary()["counts"], {"pending": 9, "posted": 3})
        self.assertEqual(await self.run_batch(post_ids=selected), 0)
        with self.assertRaises(ValueError):
            await self.run_batch(post_ids=["missing-id"])
        self.assertEqual(self.queue.summary()["counts"], {"pending": 9, "posted": 3})


if __name__ == "__main__":
    unittest.main()
