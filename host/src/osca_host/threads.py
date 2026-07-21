"""统一有界执行模型（复核 P1）：所有可能阻塞的重活跑在**守护线程**，绝不进默认执行器。

为什么不用 asyncio.to_thread：默认执行器线程不可取消，且 asyncio.run 收尾会无限等它
（`loop.shutdown_default_executor()`）、concurrent.futures 的 atexit 钩子也会 join——
一个卡死的 settle/persist/poll 能让「Host 已 STOPPED、socket 已删」之后**进程永不退出**。
守护线程随进程消亡：进程退出对关停上限真实有界。

线程不可终止的诚实边界：守护线程在进程存活期间无法被强杀（POSIX 无安全线程终止原语；
需要「超时即强制终止」语义的部署侧应以进程隔离运行 Host 本体）。因此「STOPPED 后无迟到
副作用」不靠杀线程，靠**副作用强制点 fail-closed**：关停即逐包 revoke——迟到线程的每一次
connector 外呼（authorize_tool）、LLM 调用（authorize_llm）、对账落账（settle 的 revoked
门）都在授权层被拒；在途外呼由各执行器自身的 timeout 有界。

结果/异常经 call_soon_threadsafe 回事件循环；循环已关（进程收尾）即丢弃。
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time


class PublishFence:
    """不可逆发布的预约栅栏（五轮复核 P1，与 policy 终局提交同型）——覆盖 load/index/swap。

    发布方在栅栏锁内 `begin(check)`：复核作废条件并登记在途，随后在**锁外**执行发布 I/O、
    `end()` 归还。生命周期变更方在**条件已生效之后**调 `barrier(grace)` 有界等待在途发布收尾。
    「检查已过、真正 syscall 尚未执行」的窗由此关死：发布要么先于变更返回完成、要么 begin 被拒；
    存储卡死（超过 grace）按有界语义明标悬挂，不无界阻塞生命周期（与 H-1 同一诚实边界——
    需要「超时强制终止」语义的部署以进程隔离运行）。
    """

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._inflight = 0

    def begin(self, check) -> str | None:
        """预约一次不可逆发布：栅栏锁内跑 check——返回 None=已登记可发布；返回原因串=已作废。"""
        with self._cv:
            why = check() if check is not None else None
            if why is not None:
                return why
            self._inflight += 1
            return None

    def end(self) -> None:
        with self._cv:
            self._inflight = max(0, self._inflight - 1)
            self._cv.notify_all()

    def barrier(self, grace: float) -> int:
        """生命周期变更（作废条件已生效）后的收尾栅栏：有界等待在途发布归零；返回悬挂数（0=干净）。"""
        deadline = time.monotonic() + grace
        with self._cv:
            while self._inflight > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cv.wait(remaining)
            return self._inflight


async def run_in_daemon_thread(fn, *args, name: str = "osca-worker"):
    """在守护线程执行 fn(*args) 并 await 其结果；异常原样回传。await 方被取消时线程照常
    跑完（与 to_thread 同语义），其迟到结果被丢弃——副作用由授权强制点拒绝，非靠取消。"""
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()

    def _deliver(setter, value) -> None:
        if not future.done():
            setter(value)

    def _runner() -> None:
        try:
            result = fn(*args)
        except BaseException as e:  # noqa: BLE001 —— 异常原样回传给 await 方
            with contextlib.suppress(RuntimeError):  # 循环已关：进程正在退出，结果无处交付
                loop.call_soon_threadsafe(_deliver, future.set_exception, e)
        else:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(_deliver, future.set_result, result)

    threading.Thread(target=_runner, name=name, daemon=True).start()
    return await future
