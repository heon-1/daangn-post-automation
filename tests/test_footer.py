import unittest

from uploader.browser import publication_body


class FooterTests(unittest.TestCase):
    def test_imported_draft_gets_footer_after_its_source(self):
        config = {"post_footer": "카페에 가입해 주세요.\nhttps://cafe.example/ai"}
        result = publication_body("AI 시장 이야기\n\n출처: https://source.example\n", config)
        self.assertEqual(result, "AI 시장 이야기\n\n출처: https://source.example\n\n" + config["post_footer"])

    def test_directly_authored_footer_is_not_duplicated(self):
        config = {"post_footer": "가입해 주세요.\nhttps://cafe.example/ai"}
        original = "창업자 이야기\n\n" + config["post_footer"]
        self.assertEqual(publication_body(original + "\n\n", config), original)
        self.assertEqual(publication_body(publication_body(original, config), config), original)

    def test_empty_footer_preserves_the_original(self):
        for config in ({}, {"post_footer": "  "}):
            self.assertEqual(publication_body("본문\n", config), "본문\n")

    def test_minimum_length_excludes_signup_footer(self):
        config = {"post_footer": "카페에 가입해 주세요.", "min_body_characters": 200}
        for draft in ("가" * 199, "가" * 199 + "\n\n" + config["post_footer"]):
            with self.assertRaisesRegex(ValueError, "현재 199자"):
                publication_body(draft, config)
        valid = "가" * 200 + "\n\n" + config["post_footer"]
        self.assertEqual(publication_body(valid, config), valid)

    def test_old_drafts_lose_sources_and_links_without_duplicate_footer(self):
        config = {"strip_sources_and_links": True, "post_footer": "카페에 가입해 주세요.", "min_body_characters": 200}
        story = "AI 시장 이야기입니다. " * 25
        old = story + "\n\n출처(확인):\n공식 자료: https://source.example/article\n\n카페에 가입해 주세요.\nhttps://cafe.example/ai"
        self.assertEqual(publication_body(old, config), story.rstrip() + "\n\n" + config["post_footer"])

    def test_inline_links_are_removed_and_do_not_count_toward_minimum(self):
        config = {"strip_sources_and_links": True, "min_body_characters": 200}
        with self.assertRaisesRegex(ValueError, "200자"):
            publication_body("짧은 본문\nhttps://source.example/" + "a" * 300, config)
        body = "가" * 200 + "\n[관련 회사](https://source.example/company)\nwww.example.com"
        self.assertEqual(publication_body(body, config), "가" * 200 + "\n관련 회사")
