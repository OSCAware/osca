"""统一有界执行模型：守护线程执行器 + 不可逆发布预约栅栏（五轮复核 P1）。"""

from __future__ import annotations

import threading
import time

from osca_host.threads import PublishFence


def test_publish_fence_reservation_drains_before_barrier_returns():
    """发布方预约在途时,变更方 barrier 等到 end 归还才返回——发布先于变更完成,零静默迟到。"""
    fence = PublishFence()
    state = {"why": None}
    assert fence.begin(lambda: state["why"]) is None  # 预约成功(在途)
    done: dict = {}
    started = time.monotonic()

    def barrier_thread():
        done["hung"] = fence.barrier(5.0)
        done["elapsed"] = time.monotonic() - started

    th = threading.Thread(target=barrier_thread, daemon=True)
    th.start()
    threading.Timer(0.2, fence.end).start()  # 发布 0.2s 后收尾
    th.join(10)
    assert not th.is_alive()
    assert done["hung"] == 0  # 干净收尾,零悬挂
    assert 0.1 < done["elapsed"] < 3.0  # barrier 确实等到了 end,而非立即返回

    state["why"] = "已作废（STOPPED）"
    assert fence.begin(lambda: state["why"]) == "已作废（STOPPED）"  # 变更生效后新预约被拒


def test_publish_fence_barrier_times_out_and_reports_hung():
    """在途发布悬挂(存储卡死):barrier 有界超时并报悬挂数——不无界阻塞生命周期。"""
    fence = PublishFence()
    assert fence.begin(lambda: None) is None
    started = time.monotonic()
    hung = fence.barrier(0.1)
    assert hung == 1  # 悬挂明标
    assert time.monotonic() - started < 2.0  # 有界
    fence.end()  # 迟到收尾:计数归零不炸
    assert fence.barrier(0.1) == 0
