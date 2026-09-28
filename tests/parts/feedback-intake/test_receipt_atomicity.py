"""InMemoryFeedbackReceiptStore の冪等性が並行実行で壊れないこと（Issue #1 指摘 7）。

Test/STG 用アダプタだが、check→create→store が排他されていないと同一 key で
create が二重に走り、ledger に二重書き込みが入る。feedback 検証と ledger 件数が
不安定になる。
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from feedback_intake import InMemoryFeedbackReceiptStore


def test_concurrent_execute_once_runs_create_exactly_once():
    """同一 key への同時 execute_once で create は 1 回・created=True も 1 件だけ。"""
    store = InMemoryFeedbackReceiptStore()
    thread_count = 8
    start = threading.Barrier(thread_count)
    create_barrier = threading.Barrier(thread_count)
    create_calls: list[int] = []
    lock = threading.Lock()

    def create() -> dict:
        with lock:
            create_calls.append(1)
        # 排他が無ければ全スレッドがここへ到達して barrier が解ける。
        # 排他されていれば 1 スレッドしか来ないので timeout し、BrokenBarrier になる。
        try:
            create_barrier.wait(timeout=1)
        except threading.BrokenBarrierError:
            pass
        return {"receipt_id": "r-1"}

    def call():
        start.wait(timeout=5)
        return store.execute_once(
            scope={"tenant_id": "t-1"}, record_id="rec-1", feedback_seq="1", create=create
        )

    with ThreadPoolExecutor(max_workers=thread_count) as ex:
        results = list(ex.map(lambda _: call(), range(thread_count)))

    assert len(create_calls) == 1, f"create が {len(create_calls)} 回走った（排他されていない）"
    assert sum(1 for _, created in results if created) == 1
    assert all(payload == {"receipt_id": "r-1"} for payload, _ in results)


def test_create_failure_leaves_no_receipt_so_a_retry_can_succeed():
    """create が例外を投げたら receipt を残さない（残すと再試行が永久に created=False になる）。"""
    store = InMemoryFeedbackReceiptStore()
    key = dict(scope={"tenant_id": "t-1"}, record_id="rec-1", feedback_seq="1")

    def failing_create() -> dict:
        raise RuntimeError("ledger write failed")

    with pytest.raises(RuntimeError):
        store.execute_once(**key, create=failing_create)

    payload, created = store.execute_once(**key, create=lambda: {"receipt_id": "r-2"})
    assert created is True, "失敗した試行の receipt が残っており再試行が新規扱いにならない"
    assert payload == {"receipt_id": "r-2"}


def test_different_keys_do_not_block_each_other():
    """別 key の create は並行に走る（global lock 1 本にしていないことの証明）。

    key ごとの lock という docstring の約束をテストで固定する。global lock でも
    「同一 key で create が 1 回」は通ってしまうため、この対が無いと約束が空証明になる。
    """
    store = InMemoryFeedbackReceiptStore()
    both_inside = threading.Barrier(2)
    reached: list[str] = []
    lock = threading.Lock()

    def make_create(tag: str):
        def create() -> dict:
            with lock:
                reached.append(tag)
            # 両方の key が同時に create の中に居られること。global lock だと
            # 片方が入れず timeout し BrokenBarrierError になる。
            both_inside.wait(timeout=2)
            return {"receipt_id": tag}
        return create

    def call(tag: str, record_id: str):
        return store.execute_once(
            scope={"tenant_id": "t-1"},
            record_id=record_id,
            feedback_seq="1",
            create=make_create(tag),
        )

    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = [ex.submit(call, "a", "rec-a"), ex.submit(call, "b", "rec-b")]
        results = [f.result() for f in futures]

    # barrier が解けた＝2 つの create が同時に走った。timeout していれば
    # BrokenBarrierError が result() で送出され、ここへ到達しない。
    assert sorted(reached) == ["a", "b"]
    assert all(created for _, created in results)
