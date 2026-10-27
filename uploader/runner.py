import json

from .browser import StopBatch


def run_batch(queue, publisher, limit, max_attempts, pause, interval, retry_delay, checkpoint):
    queue.recover()
    counts = queue.summary()["counts"]
    if counts.get("review", 0):
        raise ValueError("결과가 불확실한 review 글이 있습니다. status로 확인한 뒤 resolve하세요.")
    posted = 0
    while posted < limit:
        row = queue.next(max_attempts)
        if row is None:
            break
        queue.begin(row["id"])
        submitting = False
        try:
            publisher.prepare(json.loads(row["payload"]))
            # Persist intent before clicking: an interrupted submission must never silently retry.
            queue.set_status(row["id"], "submitting")
            submitting = True
            url = publisher.submit()
            queue.set_status(row["id"], "posted", url=url)
            posted += 1
            print(f"[{posted}/{limit}] 등록 완료: {row['id']}", flush=True)
            checkpoint()
        except BaseException as error:
            # Preserve a success already recorded even if session checkpointing fails.
            current = queue.db.execute("SELECT status FROM posts WHERE id=?", (row["id"],)).fetchone()[0]
            if current == "posted":
                raise
            if submitting:
                queue.set_status(row["id"], "review", error=f"{type(error).__name__}: {error}")
                raise
            stop = isinstance(error, (StopBatch, KeyboardInterrupt, SystemExit))
            attempts = row["attempts"] + 1
            status = "pending" if stop or attempts < max_attempts else "failed"
            queue.set_status(row["id"], status, error=f"{type(error).__name__}: {error}")
            if stop:
                queue.refund_attempt(row["id"])
            if stop or status == "failed":
                raise
            pause(retry_delay * (2 ** (attempts - 1)))
            continue
        if posted < limit and queue.next(max_attempts):
            pause(interval)
    return posted
