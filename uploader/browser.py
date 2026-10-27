import json
import math
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


MAX_UPLOAD_WORKERS = 20


class StopBatch(RuntimeError):
    """Authentication, server limits, or access failure require a stopped queue."""


def read_config(path, login_only=False):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("설정은 JSON 객체여야 합니다.")
    required = ("login_url",) if login_only else ("target_id", "login_url", "write_url", "authenticated", "title", "body", "submit", "success")
    for key in required:
        if not config.get(key):
            raise ValueError(f"config.json의 {key}를 설정하세요.")
    for key in ("login_url", "write_url"):
        if not config.get(key) and login_only:
            continue
        parsed = urlsplit(config[key])
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise ValueError(f"{key}는 HTTP(S) URL이어야 합니다.")
    for key in ("authenticated", "title", "body", "submit", "success", "image_input", "images_ready", "login_button", "login_indicator", "blocked_indicator", "board_selector", "board_picker", "board_option"):
        if config.get(key):
            validate_locator(config[key], key)
    for item in config.get("confirm_buttons", []):
        validate_locator(item, "confirm_buttons")
    if config.get("login_url_pattern"):
        re.compile(config["login_url_pattern"])
    if config.get("success_url_pattern"):
        re.compile(config["success_url_pattern"])
    if "post_footer" in config and not isinstance(config["post_footer"], str):
        raise ValueError("post_footer는 문자열이어야 합니다.")
    minimum = config.get("min_body_characters", 0)
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 0:
        raise ValueError("min_body_characters는 0 이상의 정수여야 합니다.")
    maximum = config.get("max_title_characters", 0)
    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 0:
        raise ValueError("max_title_characters는 0 이상의 정수여야 합니다.")
    if "strip_sources_and_links" in config and not isinstance(config["strip_sources_and_links"], bool):
        raise ValueError("strip_sources_and_links는 true 또는 false여야 합니다.")
    if "include_images" in config and not isinstance(config["include_images"], bool):
        raise ValueError("include_images는 true 또는 false여야 합니다.")
    for key in ("workers", "bulk_workers", "checkpoint_every", "bulk_checkpoint_every"):
        if key in config:
            value = config[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 1 or (key.endswith("workers") and value > MAX_UPLOAD_WORKERS):
                raise ValueError(f"{key}는 양수 정수여야 하며, 작업자 수는 1~{MAX_UPLOAD_WORKERS}입니다.")
    if config.get("image_position", "cursor") not in {"cursor", "start"}:
        raise ValueError("image_position은 cursor 또는 start여야 합니다.")
    settle = config.get("editor_settle_ms", 0)
    if isinstance(settle, bool) or not isinstance(settle, int) or settle < 0:
        raise ValueError("editor_settle_ms는 0 이상의 정수여야 합니다.")
    if bool(config.get("board_picker")) != bool(config.get("board_option")):
        raise ValueError("board_picker와 board_option은 함께 설정하세요.")
    verification = config.get("verification", {})
    if not isinstance(verification, dict):
        raise ValueError("verification은 설정 객체여야 합니다.")
    if "verify_feed" in verification and not isinstance(verification["verify_feed"], bool):
        raise ValueError("verification.verify_feed는 true 또는 false여야 합니다.")
    if verification.get("body"):
        validate_locator(verification["body"], "verification.body")
    for key in ("items", "title", "more"):
        if key in verification and (not isinstance(verification[key], str) or not verification[key].strip()):
            raise ValueError(f"verification.{key}는 선택자 문자열이어야 합니다.")
    if verification.get("feed_url"):
        parsed = urlsplit(verification["feed_url"])
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("verification.feed_url은 HTTP(S) URL이어야 합니다.")
    for key, default in (("interval_seconds", 30), ("timeout_ms", 30000), ("max_attempts", 3), ("retry_delay_seconds", 10)):
        value = config.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{key}는 양수여야 합니다.")
        if key in {"timeout_ms", "max_attempts"} and not isinstance(value, int):
            raise ValueError(f"{key}는 정수여야 합니다.")
        config[key] = value
    for key in ("interval_min_seconds", "interval_max_seconds", "bulk_interval_min_seconds", "bulk_interval_max_seconds"):
        if key in config:
            value = config[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{key}는 0 이상의 유한한 숫자여야 합니다.")
    lower = config.get("interval_min_seconds", config["interval_seconds"])
    upper = config.get("interval_max_seconds", max(lower, config["interval_seconds"] * 2))
    if upper < lower:
        raise ValueError("interval_max_seconds는 최소 대기 시간 이상이어야 합니다.")
    if config.get("bulk_interval_max_seconds", 3) < config.get("bulk_interval_min_seconds", 1):
        raise ValueError("bulk_interval_max_seconds는 최소 대기 시간 이상이어야 합니다.")
    return config


def strip_publication_references(text):
    lines = []
    in_sources = False
    for line in text.splitlines():
        heading = line.strip().lstrip("# ")
        if re.match(r"^(?:출처|참고\s*(?:자료|문헌)|sources?|references?)(?:\s|[(:：]|$)", heading, re.I):
            in_sources = True
            continue
        if in_sources:
            if not line.strip():
                in_sources = False
                lines.append("")
            continue
        line = re.sub(r"\[([^\]]+)\]\(https?://[^\s)]+\)", r"\1", line)
        line = re.sub(r"(?:https?://|www\.)[^\s<>]+", "", line).rstrip()
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def body_start_shortcut():
    return "Meta+ArrowUp" if sys.platform == "darwin" else "Control+Home"


BODY_START_HAS_WIDGET = """el => el.isContentEditable &&
    el.firstElementChild?.getAttribute('contenteditable') === 'false'"""

IMAGES_BEFORE_TEXT = """el => {
    if (!el.isContentEditable) return true;
    const walker = el.ownerDocument.createTreeWalker(el, NodeFilter.SHOW_TEXT, {
        acceptNode: node => node.textContent.trim() &&
            !node.parentElement.closest('[contenteditable="false"]') ?
            NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT
    });
    const firstText = walker.nextNode();
    const images = [...el.querySelectorAll('img')];
    return firstText && images.length > 0 && images.every(img =>
        img.compareDocumentPosition(firstText) & Node.DOCUMENT_POSITION_FOLLOWING);
}"""


FILL_CONTENTEDITABLE = """(el, text) => {
    if (!el.isContentEditable) return false;
    el.focus();
    const doc = el.ownerDocument;
    const range = doc.createRange();
    range.selectNodeContents(el);
    const selection = doc.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    if (!doc.execCommand('insertText', false, text))
        throw new Error('본문을 입력할 수 없습니다.');
    return true;
}"""


FIELD_TEXT = "el => ['INPUT', 'TEXTAREA'].includes(el.tagName) ? el.value : el.innerText"


def validate_title_length(title, config):
    maximum = config.get("max_title_characters", 0)
    if maximum and len(title) > maximum:
        raise ValueError(f"제목은 {maximum}자 이하여야 합니다. 현재 {len(title)}자입니다.")


def verify_title_text(actual, expected):
    if actual != expected:
        raise StopBatch("입력된 제목이 원고와 다릅니다. 에디터를 확인하세요.")


def verify_body_text(actual, expected):
    # Rich text editors add display-only whitespace between paragraphs and images.
    if re.sub(r"\s+", "", actual) != re.sub(r"\s+", "", expected):
        raise StopBatch("입력된 본문이 원고와 다릅니다. 에디터와 임시 원고를 확인하세요.")


def publication_body(body, config):
    """Append the configured signup footer once, including imported third-party drafts."""
    footer = config.get("post_footer", "").strip()
    if config.get("strip_sources_and_links"):
        body = strip_publication_references(body)
        footer = strip_publication_references(footer)
    story = body.rstrip()
    if footer and story.endswith(footer):
        story = story[:-len(footer)].rstrip()
    minimum = config.get("min_body_characters", 0)
    if len(story.strip()) < minimum:
        raise ValueError(f"가입 문구를 제외한 본문은 {minimum}자 이상이어야 합니다. 현재 {len(story.strip())}자입니다.")
    if not footer:
        return body
    body = body.rstrip()
    if body.endswith(footer):
        return body
    return f"{body}\n\n{footer}"


def validate_locator(spec, name):
    if isinstance(spec, str) and spec.strip():
        return
    if (isinstance(spec, dict) and isinstance(spec.get("selector"), str)
            and spec["selector"].strip() and isinstance(spec.get("frames", []), list)
            and all(isinstance(frame, str) and frame.strip() for frame in spec.get("frames", []))):
        return
    raise ValueError(f"{name}: 선택자 문자열 또는 {{selector, frames}}가 필요합니다.")


def locate(page, spec):
    if isinstance(spec, str):
        return page.locator(spec)
    root = page
    for frame in spec.get("frames", []):
        root = root.frame_locator(frame)
    return root.locator(spec["selector"])


def save_state(context, path):
    """Atomically save cookies, localStorage and IndexedDB with owner-only permissions."""
    write_state(context.storage_state(indexed_db=True), path)


def write_state(state, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp_path = tempfile.mkstemp(prefix="session-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False)
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


class FormPublisher:
    def __init__(self, page, config):
        self.page = page
        self.config = config
        self.problem = None
        self.expected_title = None
        self.origin = urlsplit(config["write_url"]).netloc
        page.set_default_timeout(config["timeout_ms"])
        page.on("response", self.on_response)

    def on_response(self, response):
        # Do not treat an unrelated advertisement or analytics request as a publishing failure.
        origins = {self.origin, *self.config.get("watch_hosts", [])}
        if urlsplit(response.url).netloc in origins and response.status in {401, 403, 429}:
            messages = {401: "로그인이 만료되었습니다.", 403: "접근이 거부되었습니다.", 429: "서버의 요청 제한에 도달했습니다."}
            self.problem = f"HTTP {response.status}: {messages[response.status]}"

    def guard(self):
        if self.problem:
            raise StopBatch(self.problem)
        pattern = self.config.get("login_url_pattern")
        if pattern and re.search(pattern, self.page.url):
            raise StopBatch("로그인 화면으로 이동했습니다. 다시 login을 실행하세요.")
        for key, message in (("login_indicator", "로그인이 필요합니다."), ("blocked_indicator", "추가 인증 또는 접근 제한 화면이 표시되었습니다.")):
            if self.config.get(key) and locate(self.page, self.config[key]).is_visible():
                raise StopBatch(message)

    def open_editor(self):
        self.problem = None
        response = self.page.goto(self.config["write_url"], wait_until="domcontentloaded")
        self.guard()
        if response and response.status >= 400:
            raise RuntimeError(f"글쓰기 화면 HTTP {response.status}")
        try:
            locate(self.page, self.config["authenticated"]).wait_for(state="visible")
        except Exception:
            self.guard()
            raise StopBatch("로그인 완료 표시를 확인할 수 없습니다. 세션과 선택자를 확인하세요.")
        self.guard()

    def prepare(self, payload):
        validate_title_length(payload["title"], self.config)
        expected_body = publication_body(payload["body"], self.config)
        images = payload["images"] if self.config.get("include_images", True) else []
        self.open_editor()
        self.expected_title = payload["title"]
        if self.config.get("board_picker"):
            locate(self.page, self.config["board_picker"]).click()
            locate(self.page, self.config["board_option"]).click()
        if self.config.get("board_selector"):
            locate(self.page, self.config["board_selector"]).wait_for(state="visible")
        locate(self.page, self.config["title"]).fill(payload["title"])
        body = locate(self.page, self.config["body"])
        # Keep selection and insertion in one page task. Delayed autofocus and
        # parallel tabs can move focus between Playwright's select/insert calls.
        if not body.evaluate(FILL_CONTENTEDITABLE, expected_body):
            body.fill(expected_body)
        if self.config.get("editor_settle_ms"):
            self.page.wait_for_timeout(self.config["editor_settle_ms"])
        if images:
            if not self.config.get("image_input"):
                raise ValueError("이미지가 있는 글에는 image_input 선택자가 필요합니다.")
            for path in images:
                if not Path(path).is_file():
                    raise ValueError(f"이미지가 없습니다: {path}")
            if self.config.get("image_position") == "start":
                body.press(body_start_shortcut())
                # ProseMirror has a non-editable dropzone before its first paragraph.
                # Move from that gap into the paragraph so the upload uses its selection.
                if body.evaluate(BODY_START_HAS_WIDGET):
                    body.press("ArrowDown")
                    body.press("Home")
            locate(self.page, self.config["image_input"]).set_input_files(images)
            if self.config.get("images_ready"):
                ready = locate(self.page, self.config["images_ready"])
                if self.config.get("images_ready_count_matches"):
                    from playwright.sync_api import expect
                    expect(ready).to_have_count(len(images), timeout=self.config["timeout_ms"])
                else:
                    ready.wait_for(state="visible")
            if self.config.get("image_position") == "start" and not body.evaluate(IMAGES_BEFORE_TEXT):
                raise StopBatch("이미지가 본문 앞에 놓이지 않았습니다. 에디터를 확인하세요.")
        # The site's editor delays its onChange callback; visible DOM text alone
        # does not mean the submit handler has received the document yet.
        if images and self.config.get("editor_settle_ms"):
            self.page.wait_for_timeout(self.config["editor_settle_ms"])
        self.guard()
        button = locate(self.page, self.config["submit"])
        button.wait_for(state="visible")
        if not button.is_enabled():
            raise ValueError("등록 버튼이 비활성 상태입니다.")
        pattern = self.config.get("success_url_pattern")
        on_success_page = not pattern or re.search(pattern, self.page.url)
        if on_success_page and locate(self.page, self.config["success"]).is_visible():
            raise ValueError("등록 전에도 success가 표시됩니다. 등록 후에만 나타나는 선택자를 지정하세요.")
        verify_title_text(locate(self.page, self.config["title"]).evaluate(FIELD_TEXT), payload["title"])
        actual = body.evaluate(FIELD_TEXT)
        verify_body_text(actual, expected_body)

    def submit(self):
        self.guard()
        locate(self.page, self.config["submit"]).click()
        for spec in self.config.get("confirm_buttons", []):
            self.guard()
            locate(self.page, spec).click()
        try:
            if self.config.get("success_url_pattern"):
                self.page.wait_for_url(re.compile(self.config["success_url_pattern"]))
            locate(self.page, self.config["success"]).wait_for(state="visible")
            if self.config.get("success_title_matches"):
                from playwright.sync_api import expect
                expect(locate(self.page, self.config["success"])).to_have_text(self.expected_title, timeout=self.config["timeout_ms"])
        except Exception:
            self.guard()
            raise
        self.guard()
        return self.page.url
