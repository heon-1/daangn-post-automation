import contextlib
import importlib.util
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from uploader.browser import FormPublisher, StopBatch, save_state
from uploader.queue import Queue, read_posts
from uploader.runner import run_batch


EDITOR = """<!doctype html><html lang="ko"><body>
<div id="account">테스트 계정</div>
<input id="title"><iframe id="editor" src="/body"></iframe>
<input id="images" type="file" multiple><button id="publish">등록</button>
<div id="success" hidden>등록 성공</div>
<script>
document.querySelector('#publish').onclick = async () => {
  const data = {
    title: document.querySelector('#title').value,
    body: document.querySelector('#editor').contentWindow.editorSnapshot ?? document.querySelector('#editor').contentDocument.body.innerText,
    images: Array.from(document.querySelector('#images').files).map(f => f.name)
  };
  const response = await fetch('/publish', {method: 'POST', body: JSON.stringify(data)});
  if (response.ok) {
    document.querySelector('#success').textContent = data.title;
    document.querySelector('#success').hidden = false;
    history.pushState({}, '', '/posts/created');
  }
};
</script></body></html>"""

DELAYED_BODY = '''<!doctype html><html><head><script>
window.editorSnapshot='';
document.addEventListener('DOMContentLoaded',()=>{
  let timer;
  new MutationObserver(()=>{
    clearTimeout(timer);
    timer=setTimeout(()=>{window.editorSnapshot=document.body.innerText},200);
  }).observe(document.body,{subtree:true,characterData:true,childList:true});
});
</script></head><body contenteditable="true"></body></html>'''


FOCUS_STEALING_BODY = DELAYED_BODY.replace(
    "let timer;",
    """let timer;
    document.body.addEventListener('focus', () => {
      setTimeout(() => parent.document.querySelector('#title').focus(), 0);
    });""")


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright 설치 후 실행하세요.")
class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from uploader.__main__ import playwright_api
        cls.playwright = playwright_api()().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        self.server.accepted = []
        self.server.publish_status = 201
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.config = {
            "login_url": self.base + "/login", "write_url": self.base + "/write",
            "authenticated": "#account", "title": "#title",
            "body": {"frames": ["#editor"], "selector": "body"},
            "image_input": "#images", "submit": "#publish", "success": "#success",
            "login_indicator": "#login", "timeout_ms": 1500,
        }
        self.queue = Queue(self.root / "queue.sqlite3")
        self.context = self.browser.new_context()

    def tearDown(self):
        self.context.close()
        self.queue.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    @staticmethod
    def handler():
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path == "/write" and "session=fake-test-session" not in self.headers.get("Cookie", ""):
                    self.send_response(302)
                    self.send_header("Location", "/login")
                    self.end_headers()
                    return
                pages = {"/write": EDITOR, "/login": '<button id="login">로그인</button>',
                         "/body": getattr(self.server, 'body_fixture', '<!doctype html><html><body contenteditable="true"></body></html>')}
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(pages.get(self.path, "").encode())

            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.server.publish_status == 201:
                    self.server.accepted.append(data)
                self.send_response(self.server.publish_status)
                self.end_headers()
                self.wfile.write(b"{}")
        return Handler

    def login(self):
        self.context.add_cookies([{"name": "session", "value": "fake-test-session", "url": self.base}])

    def import_posts(self, count=2, images=None):
        source = self.root / "posts.json"
        source.write_text(json.dumps([{"id": f"p-{i}", "title": f"제목 {i}",
                                      "body": f"본문 {i}\n둘째 줄", "images": images or []} for i in range(count)]))
        self.queue.import_posts(read_posts(source))

    def run_batch(self, context=None):
        page = (context or self.context).new_page()
        publisher = FormPublisher(page, self.config)
        with contextlib.redirect_stdout(io.StringIO()):
            return run_batch(self.queue, publisher, 1000, 3, lambda _: None, 1, 1, lambda: None)

    def test_saved_cookie_and_local_storage_work_in_new_context(self):
        self.login()
        page = self.context.new_page()
        page.goto(self.base + "/write")
        page.evaluate("localStorage.setItem('test-token', 'fake-local-token')")
        session = self.root / ".auth/session.json"
        save_state(self.context, session)
        self.assertEqual(session.stat().st_mode & 0o777, 0o600)
        self.import_posts()
        restored = self.browser.new_context(storage_state=str(session))
        try:
            check_page = restored.new_page()
            check_page.goto(self.base + "/write")
            self.assertEqual(check_page.evaluate("localStorage.getItem('test-token')"), "fake-local-token")
            self.assertEqual(self.run_batch(restored), 2)
            self.assertEqual(self.run_batch(restored), 0)
            self.assertEqual(len(self.server.accepted), 2)
            self.assertEqual(self.server.accepted[0]["body"], "본문 0\n둘째 줄")
        finally:
            restored.close()

    def test_missing_session_stops_before_posting(self):
        self.import_posts(1)
        with self.assertRaises(StopBatch):
            self.run_batch()
        self.assertEqual(self.server.accepted, [])
        self.assertEqual(self.queue.summary()["counts"], {"pending": 1})

    def test_429_response_stops_batch_with_uncertain_post(self):
        self.login()
        self.import_posts()
        self.server.publish_status = 429
        with self.assertRaises(StopBatch):
            self.run_batch()
        self.assertEqual(self.server.accepted, [])
        self.assertEqual(self.queue.summary()["counts"], {"review": 1, "pending": 1})

    def test_image_file_is_sent_to_browser_form(self):
        self.login()
        # The fixture records the selected filename; no external service receives the file.
        (self.root / "photo.png").write_bytes(b"fake-fixture-image")
        self.import_posts(1, ["photo.png"])
        self.assertEqual(self.run_batch(), 1)
        self.assertEqual(self.server.accepted[0]["images"], ["photo.png"])

    def test_delayed_editor_document_is_committed_before_submit(self):
        self.login()
        self.import_posts(1)
        self.server.body_fixture = DELAYED_BODY
        self.config['editor_settle_ms'] = 350
        self.assertEqual(self.run_batch(), 1)
        self.assertEqual(self.server.accepted[0]['body'], '본문 0\n둘째 줄')

    def test_editor_autofocus_cannot_redirect_body_into_title(self):
        self.login()
        self.import_posts(1)
        self.server.body_fixture = FOCUS_STEALING_BODY
        self.config['editor_settle_ms'] = 350
        self.assertEqual(self.run_batch(), 1)
        self.assertEqual(self.server.accepted[0]['title'], '제목 0')
        self.assertEqual(self.server.accepted[0]['body'], '본문 0\n둘째 줄')

    def test_post_url_and_title_must_match_before_success_is_recorded(self):
        self.login()
        self.import_posts(1)
        self.config["success_url_pattern"] = r"/posts/created$"
        self.config["success_title_matches"] = True
        self.assertEqual(self.run_batch(), 1)
        row = self.queue.db.execute("SELECT result_url FROM posts").fetchone()
        self.assertEqual(row[0], self.base + "/posts/created")

    def test_unexpected_post_title_requires_review(self):
        self.login()
        self.import_posts(1)
        self.config["success_title_matches"] = True
        self.config["success"] = "#account"
        self.config["success_url_pattern"] = r"/posts/created$"
        with self.assertRaises(AssertionError):
            self.run_batch()
        self.assertEqual(self.queue.summary()["counts"], {"review": 1})
        self.assertEqual(len(self.server.accepted), 1)


if __name__ == "__main__":
    unittest.main()
