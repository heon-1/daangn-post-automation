import contextlib
import importlib.util
import io
import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

import test_browser as fixture
from uploader.async_browser import AsyncFormPublisher
from uploader.browser import StopBatch, publication_body, write_state
from uploader.parallel import run_parallel_batch
from uploader.queue import Queue, read_posts


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright 설치 후 실행하세요.")
class AsyncBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from uploader.__main__ import playwright_api
        from playwright.async_api import async_playwright
        playwright_api()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        base_handler = fixture.BrowserTests.handler()

        class Handler(base_handler):
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                server = self.server
                with server.guard:
                    server.active += 1
                    server.peak = max(server.peak, server.active)
                try:
                    if server.barrier:
                        server.barrier.wait(timeout=5)
                    status = 429 if data["title"] == server.block_title else 201
                    time.sleep(0.02 if status == 429 else 0.2)
                    if status == 201:
                        with server.guard:
                            server.accepted.append(data)
                    self.send_response(status)
                    self.end_headers()
                    self.wfile.write(b"{}")
                finally:
                    with server.guard:
                        server.active -= 1

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.accepted = []
        self.server.active = self.server.peak = 0
        self.server.guard = threading.Lock()
        self.server.block_title = None
        self.server.barrier = None
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.config = {
            "write_url": self.base + "/write", "authenticated": "#account",
            "title": "#title", "body": {"frames": ["#editor"], "selector": "body"},
            "submit": "#publish", "success": "#success", "login_indicator": "#login",
            "success_url_pattern": r"/posts/created$", "success_title_matches": True,
            "timeout_ms": 3000, "post_footer": "카페에 가입해 주세요.\nhttps://cafe.example/ai",
        }
        self.queue = Queue(self.root / "queue.sqlite3")
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.context = await self.browser.new_context()

    async def asyncTearDown(self):
        await self.context.close()
        await self.browser.close()
        await self.playwright.stop()
        self.queue.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    async def login(self):
        await self.context.add_cookies([{"name": "session", "value": "fake-test-session", "url": self.base}])
        session_path = self.root / "session.json"
        write_state(await self.context.storage_state(indexed_db=True), session_path)
        await self.context.close()
        self.context = await self.browser.new_context(storage_state=str(session_path))

    def import_count(self, count):
        source = self.root / "posts.json"
        posts = [{"id": f"p-{i}", "title": f"AI 글 {i}", "body": f"시장 이야기 {i}", "images": []}
                 for i in range(count)]
        # Half the authored drafts already have the footer; imported drafts do not.
        for row in posts[::2]:
            row["body"] = publication_body(row["body"], self.config)
        source.write_text(json.dumps(posts, ensure_ascii=False))
        self.queue.import_posts(read_posts(source))

    async def run_batch(self, **options):
        async def factory(index, signal):
            return AsyncFormPublisher(await self.context.new_page(), self.config, signal)
        with contextlib.redirect_stdout(io.StringIO()):
            return await run_parallel_batch(self.queue, factory,
                                            **{"workers": 3, "order": "fifo", "interval_min": 0,
                                               "interval_max": 0, "retry_delay": 0, **options})

    async def test_parallel_browser_posts_reuse_session_and_append_footer_once(self):
        await self.login()
        self.import_count(9)
        self.assertEqual(await self.run_batch(), 9)
        self.assertEqual(await self.run_batch(), 0)
        self.assertEqual(len({row["title"] for row in self.server.accepted}), 9)
        self.assertGreater(self.server.peak, 1)
        self.assertLessEqual(self.server.peak, 3)
        for row in self.server.accepted:
            self.assertTrue(row["body"].endswith(self.config["post_footer"]))
            self.assertEqual(row["body"].count(self.config["post_footer"]), 1)
        self.assertEqual(self.queue.summary()["counts"], {"posted": 9})

    async def test_429_stops_assignment_but_preserves_inflight_confirmed_posts(self):
        await self.login()
        self.import_count(10)
        self.server.block_title = "AI 글 0"
        self.server.barrier = threading.Barrier(3)
        with self.assertRaises(StopBatch):
            await self.run_batch()
        self.assertEqual(self.queue.summary()["counts"], {"posted": 2, "review": 1, "pending": 7})
        self.assertEqual(len(self.server.accepted), 2)

    async def test_missing_session_stops_all_workers_before_posting(self):
        self.import_count(6)
        with self.assertRaises(StopBatch):
            await self.run_batch()
        self.assertEqual(self.server.accepted, [])
        self.assertEqual(self.queue.summary()["counts"], {"pending": 6})
        self.assertEqual(self.queue.db.execute("SELECT sum(attempts) FROM posts").fetchone()[0], 0)

    async def test_eight_workers_publish_text_only_without_mutating_image_drafts(self):
        await self.login()
        self.config.update(include_images=False, editor_settle_ms=500)
        self.server.body_fixture = fixture.DELAYED_BODY
        self.import_count(16)
        expected_bodies = {json.loads(row[0])["title"]: publication_body(json.loads(row[0])["body"], self.config)
                           for row in self.queue.db.execute("SELECT payload FROM posts")}
        # No image_input is configured; these images would fail if the image path ran.
        with self.queue.db:
            for row in self.queue.db.execute("SELECT id, payload FROM posts").fetchall():
                payload = json.loads(row["payload"])
                payload["images"] = [str(self.root / "unavailable-image.png")]
                self.queue.db.execute("UPDATE posts SET payload=? WHERE id=?", (json.dumps(payload), row["id"]))
        self.assertEqual(await self.run_batch(workers=8, checkpoint_every=10), 16)
        self.assertGreater(self.server.peak, 3)
        self.assertLessEqual(self.server.peak, 8)
        self.assertEqual(len({row["title"] for row in self.server.accepted}), 16)
        for row in self.server.accepted:
            self.assertEqual(row["images"], [])
            # contenteditable may add blank display lines between paragraphs.
            self.assertEqual([line for line in row["body"].splitlines() if line.strip()],
                             [line for line in expected_bodies[row["title"]].splitlines() if line.strip()])
            self.assertEqual(row["body"].count(self.config["post_footer"]), 1)
        for row in self.queue.db.execute("SELECT payload FROM posts"):
            self.assertEqual(len(json.loads(row[0])["images"]), 1)

    async def test_parallel_autofocus_preserves_title_and_full_body(self):
        await self.login()
        self.config.update(include_images=False, editor_settle_ms=350)
        self.server.body_fixture = fixture.FOCUS_STEALING_BODY
        self.import_count(16)
        self.assertEqual(await self.run_batch(workers=8), 16)
        self.assertEqual(len(self.server.accepted), 16)
        for row in self.server.accepted:
            i = int(row['title'].split()[-1])
            self.assertEqual(row['title'], f'AI 글 {i}')
            self.assertEqual([line for line in row['body'].splitlines() if line.strip()],
                             [line for line in publication_body(f'시장 이야기 {i}', self.config).splitlines() if line.strip()])
