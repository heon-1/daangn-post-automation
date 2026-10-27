import re
from pathlib import Path
from urllib.parse import urlsplit

from .browser import (BODY_START_HAS_WIDGET, FIELD_TEXT, FILL_CONTENTEDITABLE,
                      IMAGES_BEFORE_TEXT, StopBatch, body_start_shortcut, locate,
                      publication_body, validate_title_length, verify_body_text, verify_title_text)


class AsyncFormPublisher:
    """One page per worker; cookies are shared by the browser context."""

    def __init__(self, page, config, signal):
        self.page, self.config, self.signal = page, config, signal
        self.problem = None
        self.expected_title = None
        self.hosts = {urlsplit(config["write_url"]).netloc, *config.get("watch_hosts", [])}
        page.set_default_timeout(config["timeout_ms"])
        page.on("response", self.on_response)

    def on_response(self, response):
        if urlsplit(response.url).netloc in self.hosts and response.status in {401, 403, 429}:
            reasons = {401: "로그인이 만료되었습니다.", 403: "접근이 거부되었습니다.", 429: "서버의 요청 제한에 도달했습니다."}
            self.problem = StopBatch(f"HTTP {response.status}: {reasons[response.status]}")
            self.signal.stop(self.problem)

    async def guard(self, shared=True):
        if self.problem:
            raise self.problem
        pattern = self.config.get("login_url_pattern")
        if pattern and re.search(pattern, self.page.url):
            raise StopBatch("로그인 화면으로 이동했습니다. 다시 login을 실행하세요.")
        for key, message in (("login_indicator", "로그인이 필요합니다."), ("blocked_indicator", "추가 인증 또는 접근 제한 화면이 표시되었습니다.")):
            if self.config.get(key) and await locate(self.page, self.config[key]).is_visible():
                raise StopBatch(message)
        if shared and self.signal.error is not None:
            raise StopBatch("다른 작업자의 오류로 새 등록을 중단합니다.")

    async def prepare(self, payload):
        validate_title_length(payload["title"], self.config)
        expected_body = publication_body(payload["body"], self.config)
        images = payload["images"] if self.config.get("include_images", True) else []
        await self.guard()
        response = await self.page.goto(self.config["write_url"], wait_until="domcontentloaded")
        await self.guard()
        if response and response.status >= 400:
            raise RuntimeError(f"글쓰기 화면 HTTP {response.status}")
        try:
            await locate(self.page, self.config["authenticated"]).wait_for(state="visible")
        except Exception:
            await self.guard()
            raise StopBatch("로그인 완료 표시를 확인할 수 없습니다. 세션과 선택자를 확인하세요.")
        await self.guard()
        self.expected_title = payload["title"]
        if self.config.get("board_picker"):
            await locate(self.page, self.config["board_picker"]).click()
            await locate(self.page, self.config["board_option"]).click()
        if self.config.get("board_selector"):
            await locate(self.page, self.config["board_selector"]).wait_for(state="visible")
        await locate(self.page, self.config["title"]).fill(payload["title"])
        body = locate(self.page, self.config["body"])
        if not await body.evaluate(FILL_CONTENTEDITABLE, expected_body):
            await body.fill(expected_body)
        if self.config.get("editor_settle_ms"):
            await self.page.wait_for_timeout(self.config["editor_settle_ms"])
        if images:
            if not self.config.get("image_input"):
                raise ValueError("이미지가 있는 글에는 image_input 선택자가 필요합니다.")
            for path in images:
                if not Path(path).is_file():
                    raise ValueError(f"이미지가 없습니다: {path}")
            if self.config.get("image_position") == "start":
                await body.press(body_start_shortcut())
                if await body.evaluate(BODY_START_HAS_WIDGET):
                    await body.press("ArrowDown")
                    await body.press("Home")
            await locate(self.page, self.config["image_input"]).set_input_files(images)
            if self.config.get("images_ready"):
                from playwright.async_api import expect
                ready = locate(self.page, self.config["images_ready"])
                if self.config.get("images_ready_count_matches"):
                    await expect(ready).to_have_count(len(images), timeout=self.config["timeout_ms"])
                else:
                    await ready.wait_for(state="visible")
            if self.config.get("image_position") == "start" and not await body.evaluate(IMAGES_BEFORE_TEXT):
                raise StopBatch("이미지가 본문 앞에 놓이지 않았습니다. 에디터를 확인하세요.")
        if images and self.config.get("editor_settle_ms"):
            await self.page.wait_for_timeout(self.config["editor_settle_ms"])
        await self.guard()
        button = locate(self.page, self.config["submit"])
        await button.wait_for(state="visible")
        if not await button.is_enabled():
            raise ValueError("등록 버튼이 비활성 상태입니다.")
        pattern = self.config.get("success_url_pattern")
        if (not pattern or re.search(pattern, self.page.url)) and await locate(self.page, self.config["success"]).is_visible():
            raise ValueError("등록 전에도 success가 표시됩니다. 등록 후에만 나타나는 조건을 지정하세요.")

        verify_title_text(await locate(self.page, self.config["title"]).evaluate(FIELD_TEXT), payload["title"])
        actual = await body.evaluate(FIELD_TEXT)
        verify_body_text(actual, expected_body)

    async def submit(self):
        await self.guard()
        await locate(self.page, self.config["submit"]).click()
        for spec in self.config.get("confirm_buttons", []):
            await self.guard()
            await locate(self.page, spec).click()
        try:
            if self.config.get("success_url_pattern"):
                await self.page.wait_for_url(re.compile(self.config["success_url_pattern"]))
            await locate(self.page, self.config["success"]).wait_for(state="visible")
            if self.config.get("success_title_matches"):
                from playwright.async_api import expect
                await expect(locate(self.page, self.config["success"])).to_have_text(self.expected_title, timeout=self.config["timeout_ms"])
        except Exception:
            await self.guard(shared=False)
            raise
        # Preserve a confirmed post even if another page stopped the queue in the meantime.
        await self.guard(shared=False)
        return self.page.url

    async def close(self):
        await self.page.close()
