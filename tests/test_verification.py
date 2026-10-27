import asyncio
import contextlib
import html
import io
import json
import tempfile
import threading
import unittest
from argparse import Namespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from uploader.browser import write_state
from uploader.queue import Queue, read_posts
from uploader.verification import publication_matches, verify


class VerificationPolicyTests(unittest.TestCase):
    def item(self, **changes):
        return dict(title_matches=True, body_matches=True, body_characters_excluding_footer=250,
                    images=1, footer_count=0, footer_at_bottom=False, body_links=1,
                    contains_url=True, contains_source_section=False, **changes)

    def test_optional_footer_and_links_do_not_reject_a_valid_generic_post(self):
        self.assertTrue(publication_matches(self.item(), 1, {'min_body_characters': 200}))

    def test_explicit_image_footer_and_link_policies_are_enforced(self):
        item = self.item()
        self.assertFalse(publication_matches(item, 0, {}))
        self.assertFalse(publication_matches(item, 1, {'post_footer': 'Join us'}))
        self.assertFalse(publication_matches(item, 1, {'strip_sources_and_links': True}))

    def test_changed_body_or_short_body_never_passes(self):
        item = self.item()
        item['body_matches'] = False
        self.assertFalse(publication_matches(item, 1, {}))
        self.assertFalse(publication_matches(self.item(), 1, {'min_body_characters': 251}))


class GenericVerificationTests(unittest.TestCase):
    def test_custom_body_feed_images_and_optional_footer_work_without_real_account(self):
        body = 'Example with a preserved link https://example.com/docs'

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                if self.path == '/feed':
                    link = '<a class="entry" href="/post/two"><span>Example post 2</span></a>'
                    page = ('<a class="entry" href="/post/one"><span>Example post 1</span></a><button id="more">More</button>'
                            + '<script>document.querySelector("#more").onclick=()=>setTimeout(()=>{'
                            + 'document.body.insertAdjacentHTML("beforeend",' + json.dumps(link) + ');'
                            + 'document.querySelector("#more").remove()},50)</script>')
                else:
                    title = 'Example post 2' if self.path.endswith('/two') else 'Example post 1'
                    page = ('<h1>' + title + '</h1><div class="content">' + html.escape(body)
                            + '<img src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="></div>')
                self.wfile.write(page.encode())

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                image = root / 'test.gif'
                image.write_bytes(b'local-test-image')
                source, config, session, db, out = [root / name for name in (
                    'posts.json', 'config.json', 'session.json', 'queue.sqlite3', 'report.json')]
                base = f'http://127.0.0.1:{server.server_port}'
                source.write_text(json.dumps([{'id':value,'title':f'Example post {i}','body':body,'images':['test.gif']}
                                              for i,value in enumerate(['one','two'], 1)]))
                settings = {'target_id':'test', 'login_url':base+'/login','write_url':base+'/new',
                    'authenticated':'#account','title':'input','body':'textarea','submit':'button','success':'h1',
                    'include_images':True,'verification':{'body':'.content','verify_feed':False}}
                config.write_text(json.dumps(settings))
                write_state({'cookies': [], 'origins': []}, session)
                with Queue(db) as queue:
                    queue.import_posts(read_posts(source))
                    for value in ['one','two']:
                        queue.set_status(value, 'posted', url=base+'/post/'+value)
                args = Namespace(config=config,source=source,session=session,db=db,out=out,workers=1,resume_report=False)
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertTrue(asyncio.run(verify(args)))
                report = json.loads(out.read_text())
                self.assertTrue(report['complete'])
                self.assertEqual(report['checks'][0]['expected_images'], 1)
                self.assertEqual(report['checks'][0]['footer_count'], 0)
                self.assertNotIn('feed', report)
                settings['verification'].update(verify_feed=True, feed_url=base+'/feed',
                                                items='a.entry', title='span', more='#more')
                config.write_text(json.dumps(settings))
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertTrue(asyncio.run(verify(args)))
                report = json.loads(out.read_text())
                self.assertEqual(report['feed']['selected_posts_seen'], 2)
                self.assertEqual(report['feed']['snapshots'], 2)
        finally:
            server.shutdown()
            server.server_close()
            worker.join()
