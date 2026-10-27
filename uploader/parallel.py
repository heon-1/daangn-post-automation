import asyncio
import json
import math
import random
import signal as process_signals
from collections import deque

from .browser import MAX_UPLOAD_WORKERS, StopBatch, write_state


class BatchSignal:
    def __init__(self):
        self.error = None

    def stop(self, error):
        if self.error is None:
            self.error = error


def validate_batch_options(limit, workers, max_attempts, interval_min, interval_max,
                           retry_delay, order, checkpoint_every):
    if (any(isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in (limit, workers, max_attempts, checkpoint_every)) or workers > MAX_UPLOAD_WORKERS):
        raise ValueError(f"limit, max_attempts, checkpoint_every는 양수 정수, workers는 1~{MAX_UPLOAD_WORKERS}이어야 합니다.")
    if (any(not math.isfinite(value) for value in (interval_min, interval_max, retry_delay))
            or interval_min < 0 or interval_max < interval_min or retry_delay < 0):
        raise ValueError("대기 간격 범위를 확인하세요.")
    if order not in {"random", "fifo"}:
        raise ValueError("order는 random 또는 fifo여야 합니다.")


async def run_parallel_batch(queue, publisher_factory, limit=1000, workers=3,
                             max_attempts=3, interval_min=30, interval_max=60,
                             retry_delay=10, order="random", seed=None,
                             pause=asyncio.sleep, checkpoint=None, signal=None,
                             checkpoint_every=1, post_ids=None):
    validate_batch_options(limit, workers, max_attempts, interval_min, interval_max,
                           retry_delay, order, checkpoint_every)
    queue.recover()
    if queue.summary()["counts"].get("review", 0):
        raise ValueError("review 글이 있습니다. 게시판에서 확인하고 resolve하세요.")
    rng = random.Random(seed)
    ids = queue.pending_ids(max_attempts)
    if post_ids is not None:
        if not isinstance(post_ids, (list, tuple, set)) or any(not isinstance(value, str) or not value for value in post_ids):
            raise ValueError("post_ids는 글 id 목록이어야 합니다.")
        selected = set(post_ids)
        known = {row[0] for row in queue.db.execute("SELECT id FROM posts")}
        if selected - known:
            raise ValueError("선택한 글 중 큐에 없는 id가 있습니다. 먼저 import하세요.")
        ids = [post_id for post_id in ids if post_id in selected]
    if order == "random":
        rng.shuffle(ids)
    planned = deque(ids[:limit])
    planned_count = len(planned)
    signal = signal or BatchSignal()
    posted = 0
    checkpoint_posted = 0
    checkpoint_failed = False
    checkpoint_lock = asyncio.Lock()

    async def save_checkpoint(force=False):
        nonlocal checkpoint_posted, checkpoint_failed
        if checkpoint is None:
            return
        async with checkpoint_lock:
            if checkpoint_failed:
                return
            count = posted
            if count == checkpoint_posted or (not force and count - checkpoint_posted < checkpoint_every):
                return
            try:
                await checkpoint()
                checkpoint_posted = count
            except BaseException:
                checkpoint_failed = True
                raise

    async def worker(index):
        nonlocal posted
        publisher = None
        try:
            if not planned or signal.error is not None:
                return
            publisher = await publisher_factory(index, signal)
            while planned and signal.error is None:
                # No await between taking the id and committing its claim.
                row = queue.claim(planned.popleft(), max_attempts)
                if row is None:
                    continue
                payload = json.loads(row["payload"])
                attempts = row["attempts"] + 1
                while True:
                    submitting = False
                    try:
                        if signal.error is not None:
                            raise StopBatch("병렬 작업이 중단되었습니다.")
                        await publisher.prepare(payload)
                        if signal.error is not None:
                            raise StopBatch("병렬 작업이 중단되었습니다.")
                        queue.set_status(row["id"], "submitting")
                        submitting = True
                        url = await publisher.submit()
                        queue.set_status(row["id"], "posted", url=url)
                        posted += 1
                        print(f"[{posted}/{planned_count}] 작업자 {index + 1} 등록 완료: {row['id']}", flush=True)
                        await save_checkpoint()
                        break
                    except BaseException as error:
                        current = queue.db.execute("SELECT status FROM posts WHERE id=?", (row["id"],)).fetchone()[0]
                        if current == "posted":
                            signal.stop(error)
                            return
                        if submitting:
                            queue.set_status(row["id"], "review", error=f"{type(error).__name__}: {error}")
                            signal.stop(error)
                            return
                        stopped = signal.error is not None or isinstance(error, (StopBatch, KeyboardInterrupt, SystemExit, asyncio.CancelledError))
                        if stopped:
                            queue.set_status(row["id"], "pending", error=f"{type(error).__name__}: {error}")
                            queue.refund_attempt(row["id"])
                            signal.stop(error)
                            return
                        if attempts >= max_attempts:
                            queue.set_status(row["id"], "failed", error=f"{type(error).__name__}: {error}")
                            signal.stop(error)
                            return
                        # Keep the reservation while retrying; another worker cannot take this id.
                        queue.set_status(row["id"], "preparing", error=f"{type(error).__name__}: {error}")
                        try:
                            await pause(retry_delay * (2 ** (attempts - 1)))
                        except BaseException as pause_error:
                            queue.set_status(row["id"], "pending", error=type(pause_error).__name__)
                            queue.refund_attempt(row["id"])
                            signal.stop(pause_error)
                            return
                        if signal.error is not None:
                            queue.set_status(row["id"], "pending")
                            return
                        queue.begin(row["id"])
                        attempts += 1
                if planned and signal.error is None:
                    await pause(rng.uniform(interval_min, interval_max))
        except BaseException as error:
            signal.stop(error)
        finally:
            if publisher is not None:
                try:
                    await publisher.close()
                except Exception as error:
                    signal.stop(error)

    # Do not cancel other Playwright calls on failure. They finish or record review,
    # while the shared signal blocks new claims and submissions.
    await asyncio.gather(*(worker(index) for index in range(min(workers, len(planned)))))
    # Flush the final partial group on normal completion and on a stopped batch.
    # Each post's SQLite status is still committed immediately, independently of cookies.
    if not checkpoint_failed:
        try:
            await save_checkpoint(force=True)
        except BaseException as error:
            signal.stop(error)
    if signal.error is not None:
        raise signal.error
    return posted


async def publish_with_browser(queue, config, session_path, **options):
    from playwright.async_api import async_playwright
    from .async_browser import AsyncFormPublisher

    config = {**config, "include_images": options.pop("include_images", config.get("include_images", True))}
    signal = BatchSignal()
    loop = asyncio.get_running_loop()
    old_handler = process_signals.getsignal(process_signals.SIGINT)
    loop.add_signal_handler(process_signals.SIGINT, lambda: signal.stop(KeyboardInterrupt("중단 요청")))
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=options.pop("headless", False))
            context = await browser.new_context(storage_state=str(session_path))
            try:
                async def factory(index, shared_signal):
                    return AsyncFormPublisher(await context.new_page(), config, shared_signal)

                async def checkpoint():
                    write_state(await context.storage_state(indexed_db=True), session_path)

                return await run_parallel_batch(queue, factory, signal=signal, checkpoint=checkpoint, **options)
            finally:
                await context.close()
                await browser.close()
    finally:
        loop.remove_signal_handler(process_signals.SIGINT)
        process_signals.signal(process_signals.SIGINT, old_handler)
