import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from uploader.__main__ import build_parser, main
from uploader.browser import read_config
from uploader.options import resolve_run_options
from uploader.queue import Queue, read_posts


class RunOptionsTests(unittest.TestCase):
    def setUp(self):
        self.config = read_config(Path(__file__).resolve().parents[1] / "config.example.json")

    def options(self, *arguments):
        return resolve_run_options(build_parser().parse_args(["run", *arguments]), self.config)

    def test_bulk_profile_and_explicit_overrides(self):
        bulk = self.options("--bulk")
        self.assertEqual((bulk["workers"], bulk["interval_min"], bulk["interval_max"], bulk["checkpoint_every"], bulk["include_images"]),
                         (10, 1, 3, 10, False))
        custom = self.options("--bulk", "--workers", "6", "--interval-min", "0.5", "--interval-max", "2",
                              "--checkpoint-every", "4", "--with-images", "--limit", "17")
        self.assertEqual((custom["workers"], custom["interval_min"], custom["interval_max"], custom["checkpoint_every"],
                          custom["include_images"], custom["limit"]), (6, 0.5, 2, 4, True, 17))
        normal = self.options()
        self.assertEqual((normal["workers"], normal["interval_min"], normal["interval_max"], normal["checkpoint_every"]), (3, 30, 60, 1))

    def test_invalid_options_fail_before_opening_browser(self):
        for arguments in (("--workers", "21"), ("--workers", "0"), ("--checkpoint-every", "0"), ("--limit", "0"),
                          ("--interval-min", "nan"), ("--interval-max", "inf"),
                          ("--interval-min", "5", "--interval-max", "2")):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                self.options(*arguments)

    def test_twenty_workers_supported_by_cli_and_config(self):
        self.assertEqual(self.options("--bulk", "--workers", "20")["workers"], 20)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps({**self.config, "workers": 20, "bulk_workers": 20}))
            config = read_config(path)
            self.assertEqual((config["workers"], config["bulk_workers"]), (20, 20))

    def test_dry_run_needs_no_session_and_does_not_publish_or_claim(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path, source, db = root / "config.json", root / "posts.json", root / "queue.sqlite3"
            config_path.write_text(json.dumps(self.config))
            source.write_text(json.dumps([{"id": "one", "title": "AI 시장", "body": "가" * 200, "images": []}]))
            with Queue(db) as queue:
                queue.import_posts(read_posts(source))
            output = io.StringIO()
            argv = ["uploader", "--config", str(config_path), "--db", str(db), "--session", str(root / "missing.json"),
                    "run", "--bulk", "--dry-run"]
            with patch("sys.argv", argv), patch("uploader.__main__.playwright_api") as browser, \
                    patch("uploader.__main__.publish_with_browser") as publish, contextlib.redirect_stdout(output):
                main()
            browser.assert_not_called()
            publish.assert_not_called()
            report = json.loads(output.getvalue())
            self.assertEqual((report["planned_posts"], report["include_images"], report["publishes"]), (1, False, False))
            with Queue(db) as queue:
                self.assertEqual(queue.summary()["counts"], {"pending": 1})
                self.assertEqual(queue.db.execute("SELECT attempts FROM posts").fetchone()[0], 0)

    def test_overlong_pending_title_stops_before_browser_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path, source, db = root / "config.json", root / "posts.json", root / "queue.sqlite3"
            config_path.write_text(json.dumps(self.config))
            source.write_text(json.dumps([{"id": "long-title", "title": "가" * 51,
                                          "body": "가" * 200, "images": []}]))
            with Queue(db) as queue:
                queue.import_posts(read_posts(source))
            session = root / "session.json"
            session.write_text("{}")
            argv = ["uploader", "--config", str(config_path), "--db", str(db), "--session", str(session),
                    "run", "--bulk", "--workers", "20", "--only-from", str(source)]
            with patch("sys.argv", argv), patch("uploader.__main__.playwright_api") as browser, \
                    patch("uploader.__main__.publish_with_browser") as publish, \
                    self.assertRaisesRegex(ValueError, "현재 51자"):
                main()
            browser.return_value.assert_not_called()
            publish.assert_not_called()
            with Queue(db) as queue:
                self.assertEqual(queue.summary()["counts"], {"pending": 1})
                self.assertEqual(queue.db.execute("SELECT attempts FROM posts").fetchone()[0], 0)

    def test_config_rejects_invalid_bulk_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            for overrides in ({"bulk_workers": 21}, {"workers": 21}, {"bulk_checkpoint_every": 0}, {"include_images": "false"},
                              {"max_title_characters": -1}, {"max_title_characters": True},
                              {"bulk_interval_min_seconds": 5}, {"bulk_interval_max_seconds": float("nan")}):
                path.write_text(json.dumps({**self.config, **overrides}))
                with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                    read_config(path)
