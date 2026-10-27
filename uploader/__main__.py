import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from .browser import MAX_UPLOAD_WORKERS, FormPublisher, locate, publication_body, read_config, save_state, validate_title_length
from .options import resolve_run_options
from .queue import Queue, read_posts
from .parallel import publish_with_browser


def playwright_api():
    local_browsers = Path(__file__).resolve().parent.parent / ".browsers"
    if local_browsers.is_dir():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(local_browsers))
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ValueError("먼저 pip install . 과 python -m playwright install chromium을 실행하세요.")
    return sync_playwright


def build_parser():
    parser = argparse.ArgumentParser(description="직접 로그인한 세션으로 카페 글을 무작위 병렬 등록합니다.")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--db", default=".state/queue.sqlite3")
    parser.add_argument("--session", default=".auth/session.json")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("login", help="브라우저에서 직접 로그인하고 세션 저장")
    imported = commands.add_parser("import", help="JSON/CSV 글 목록 검증 및 큐 추가")
    imported.add_argument("source")
    commands.add_parser("status", help="처리 결과 및 확인이 필요한 글 보기")
    commands.add_parser("preview", help="다음 글을 로컬에서 확인; 브라우저를 열지 않음")
    commands.add_parser("check", help="저장한 세션으로 글쓰기 화면 접근 확인")
    inspect = commands.add_parser("inspect", help="세션을 복원하고 화면의 입력 요소·버튼을 확인")
    inspect.add_argument("url")
    run = commands.add_parser("run", help="대기 글 무작위 병렬 등록 및 재개")
    run.add_argument("--limit", type=int, default=1000)
    run.add_argument("--only-from", help="가져온 JSON/CSV에 포함된 id만 등록; 다른 대기 글은 제외")
    run.add_argument("--headless", action="store_true")
    run.add_argument("--bulk", action="store_true", help="벌크 모드: 기본 작업자 10개, 1~3초 대기, 세션 10건마다 저장")
    run.add_argument("--workers", type=int, help=f"동시 업로드 작업자 수 (1~{MAX_UPLOAD_WORKERS}; 일반 기본 3, 벌크 기본 10)")
    run.add_argument("--order", choices=["random", "fifo"], default="random")
    run.add_argument("--seed", type=int, help="게시 순서·대기 시간 난수 시드")
    run.add_argument("--interval-min", type=float, help="작업자별 등록 사이 최소 대기 초")
    run.add_argument("--interval-max", type=float, help="작업자별 등록 사이 최대 대기 초")
    run.add_argument("--checkpoint-every", type=int, help="세션 저장 간격(등록 건수); 게시 결과는 항상 건별 즉시 저장")
    images = run.add_mutually_exclusive_group()
    images.add_argument("--no-images", dest="include_images", action="store_false", help="원고에 이미지가 있어도 텍스트만 등록")
    images.add_argument("--with-images", dest="include_images", action="store_true", help="원고에 지정한 이미지 첨부")
    run.set_defaults(include_images=None)
    run.add_argument("--dry-run", action="store_true", help="실행 설정·대기 글 검증만 수행; 브라우저와 등록 버튼 사용 안 함")
    resolved = commands.add_parser("resolve", help="게시판에서 결과를 확인한 글의 상태 수정")
    resolved.add_argument("id")
    resolved.add_argument("outcome", choices=["posted", "pending"])
    resolved.add_argument("--url")
    return parser


def main():
    args = build_parser().parse_args()

    if args.command in {"import", "status", "preview", "resolve"}:
        posts = read_posts(args.source) if args.command == "import" else None
        with Queue(args.db) as queue:
            if args.command in {"status", "resolve"}:
                queue.recover()
            if args.command == "import":
                added, skipped = queue.import_posts(posts)
                print(f"추가 {added}개, 중복 생략 {skipped}개")
            elif args.command == "resolve":
                queue.resolve(args.id, args.outcome, args.url)
                print(f"{args.id}: {args.outcome}")
            elif args.command == "preview":
                row = queue.next()
                print(row["payload"] if row else "대기 글이 없습니다.")
            else:
                print(json.dumps(queue.summary(), ensure_ascii=False, indent=2))
        return

    config = read_config(args.config, login_only=args.command in {"login", "inspect"})
    options = resolve_run_options(args, config) if args.command == "run" else None
    selection = read_posts(args.only_from) if args.command == "run" and args.only_from else None
    def verify_selection(queue):
        if selection is None:
            return None
        for payload, fingerprint in selection:
            row = queue.db.execute("SELECT fingerprint FROM posts WHERE id=?", (payload["id"],)).fetchone()
            if row is None or row[0] != fingerprint:
                raise ValueError(f"선택 파일과 큐가 다릅니다. 먼저 import하세요: {payload['id']}")
        return {payload["id"] for payload, _ in selection}
    if args.command == "run" and args.dry_run:
        with Queue(args.db) as queue:
            selected = verify_selection(queue)
            ids = queue.pending_ids(options["max_attempts"])
            if selected is not None:
                ids = [post_id for post_id in ids if post_id in selected]
            # Validate every eligible draft; this does not claim posts or change their state.
            for post_id in ids:
                row = queue.db.execute("SELECT payload FROM posts WHERE id=?", (post_id,)).fetchone()
                payload = json.loads(row[0])
                validate_title_length(payload["title"], config)
                publication_body(payload["body"], config)
            print(json.dumps({"mode": "bulk" if args.bulk else "normal", **options,
                              "only_from": args.only_from,
                              "eligible_posts": len(ids), "planned_posts": min(options["limit"], len(ids)),
                              "attention": queue.summary()["attention"], "publishes": False}, ensure_ascii=False, indent=2))
        return
    sync_playwright = playwright_api()
    if args.command != "login" and not Path(args.session).is_file():
        raise ValueError("저장된 세션이 없습니다. 먼저 login을 실행하세요.")
    if args.command == "run":
        with Queue(args.db) as queue:
            selected = verify_selection(queue)
            if selected is not None:
                options["post_ids"] = sorted(selected)
            for post_id in queue.pending_ids(options["max_attempts"]):
                if selected is None or post_id in selected:
                    row = queue.db.execute("SELECT payload FROM posts WHERE id=?", (post_id,)).fetchone()
                    validate_title_length(json.loads(row[0])["title"], config)
            queue.bind(json.dumps([config["target_id"], config["write_url"]]))
            print(f"{'벌크' if args.bulk else '일반'} 모드: 작업자 {options['workers']}개, 순서 {options['order']}, "
                  f"대기 {options['interval_min']}~{options['interval_max']}초, "
                  f"이미지 {'사용' if options['include_images'] else '생략'}, 세션 {options['checkpoint_every']}건마다 저장", flush=True)
            started = time.monotonic()
            count = asyncio.run(publish_with_browser(queue, config, args.session, **options))
            elapsed = time.monotonic() - started
            print(f"이번 실행 등록 완료: {count}개 / {elapsed:.1f}초 / 분당 {count * 60 / max(elapsed, 0.001):.1f}개")
        return
    # Lock the queue before launching the browser; also serializes session updates for this queue.
    with Queue(args.db) as queue:
        if args.command not in {"login", "inspect"}:
            queue.bind(json.dumps([config["target_id"], config["write_url"]]))
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=getattr(args, "headless", False))
            context = browser.new_context(storage_state=args.session if args.command != "login" else None)
            try:
                page = context.new_page()
                if args.command == "login":
                    page.goto(config["login_url"], wait_until="domcontentloaded")
                    if config.get("login_button"):
                        locate(page, config["login_button"]).click()
                    input("브라우저에서 직접 로그인한 후, 이 터미널에서 Enter를 누르세요: ")
                    if config.get("write_url") and config.get("authenticated"):
                        FormPublisher(page, config).open_editor()
                    elif config.get("login_indicator"):
                        page.goto(config["login_url"], wait_until="domcontentloaded")
                        if locate(page, config["login_indicator"]).is_visible():
                            raise ValueError("로그인 표시가 남아 있습니다. 로그인을 완료한 뒤 다시 저장하세요.")
                    save_state(context, args.session)
                    print("세션 저장 완료. check 명령으로 글쓰기 화면 접근을 확인하세요.")
                elif args.command == "check":
                    publisher = FormPublisher(page, config)
                    publisher.open_editor()
                    for key in ("title", "body", "submit", "board_selector"):
                        if config.get(key):
                            locate(page, config[key]).wait_for(state="visible")
                    save_state(context, args.session)
                    print("저장된 세션으로 글쓰기 화면·제목·본문·등록 버튼·게시판 확인 완료")
                elif args.command == "inspect":
                    page.goto(args.url, wait_until="domcontentloaded")
                    try:
                        page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        pass
                    elements = page.locator("main button, main a, input, textarea, [contenteditable=true], iframe, header button").evaluate_all("""elements => elements.map(el => ({
                        tag: el.tagName.toLowerCase(),
                        text: ['BUTTON', 'A'].includes(el.tagName) ? el.innerText.trim().slice(0, 100) : '',
                        visible: !!(el.getClientRects().length),
                        attributes: Object.fromEntries(['id', 'name', 'type', 'accept', 'multiple', 'placeholder', 'aria-label', 'role', 'data-track-event', 'contenteditable', 'href'].filter(key => el.hasAttribute(key)).map(key => [key, el.getAttribute(key)]))
                    }))""")
                    output = Path(args.db).parent / "inspection.json"
                    output.write_text(json.dumps({"url": page.url, "elements": elements}, ensure_ascii=False, indent=2), encoding="utf-8")
                    output.chmod(0o600)
                    page.screenshot(path=str(output.with_suffix(".png")), full_page=False)
                    output.with_suffix(".png").chmod(0o600)
                    save_state(context, args.session)
                    print(json.dumps({"url": page.url, "elements": [e for e in elements if e["visible"] and e["tag"] != "a"], "inspection": str(output)}, ensure_ascii=False, indent=2))
            finally:
                context.close()
                browser.close()


def cli():
    """Shared error handling for the installed command and python -m uploader."""
    try:
        main()
    except KeyboardInterrupt:
        print("\n중단했습니다. status로 확인하고 run으로 재개하세요.", file=sys.stderr)
        sys.exit(130)
    except Exception as error:
        print(f"중단: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    cli()
