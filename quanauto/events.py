"""事件引擎 —— `Event` 数据类 + `EventEngine` 类（契约 §2.3）。

为什么回测引擎要用它，而不是直接函数调用：契约 §2.1.1 把 `EventEngine` 列在
`BacktestEngine` 的依赖里，而且事件驱动是「同一份 K 线数据、多种消费者（策略 / 撮合 /
绩效）」的天然解耦点。所以 I1 的 `run()` 是**真的**按 BAR 事件走的，不是为了凑门禁才
挂一个没人用的类。

确定性是这里的第一要求（否则「同一条命令跑两次指标一致」无从谈起）：

* 队列是 FIFO 的 `collections.deque`，不排序、不按优先级抢占 —— 一排序就变成
  「同等优先级时结果依赖排序实现」的不确定源。
* `publish` 同步派发（不是起线程）。契约提了并发安全，但 I1 是单线程回测，
  起线程只会引入调度不确定性。真做多线程是「本迭代不做」。
* 处理器按**订阅顺序**调用，订阅表用 `dict`（Python 3.7+ 保序）。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Deque, Dict, List

from .enums import EventType


@dataclass
class Event:
    """事件对象。字段顺序照抄契约 §2.3.3。"""

    event_type: EventType
    data: Any
    timestamp: datetime
    source: str
    priority: int


class EventEngine:
    """同步、保序、可重放的事件引擎。

    与契约的一处**有意偏差**：契约的方法体全是 `pass`，没说要怎么存订阅。这里用
    `_handlers: Dict[EventType, Dict[str, Callable]]`（外层按类型、内层按订阅 ID 保序），
    这样 `unsubscribe` 是 O(1) 且不打乱别人的相对顺序。
    """

    def __init__(self) -> None:
        self._handlers: Dict[EventType, Dict[str, Callable]] = {}
        self._queue: Deque[Event] = deque()
        self._running = False
        self._counter = 0
        self._dispatched = 0

    # ── 订阅管理 ──────────────────────────────────────────────────────
    def subscribe(self, event_type: EventType, handler: Callable) -> str:
        """订阅事件，返回订阅 ID。**同一个 handler 重复订阅是允许的**（会收到两次），
        因为契约没禁止；要避免重复是调用方的事。"""
        self._counter += 1
        subscription_id = "sub-%d" % self._counter
        self._handlers.setdefault(event_type, {})[subscription_id] = handler
        return subscription_id

    def unsubscribe(self, subscription_id: str) -> bool:
        """取消订阅。ID 不存在返回 `False`，不抛异常。"""
        for handlers in self._handlers.values():
            if subscription_id in handlers:
                del handlers[subscription_id]
                return True
        return False

    # ── 事件流 ────────────────────────────────────────────────────────
    def publish(self, event: Event) -> bool:
        """把事件放进队列。

        契约只写了「发布成功返回 True」。I1 的语义是：**队列关了就不收**
        （`stop()` 之后 publish 返回 False），这样调用方能看出事件被丢了，
        而不是静默消失。
        """
        if not self._running:
            return False
        self._queue.append(event)
        return True

    def start(self) -> None:
        """启动（幂等）。"""
        self._running = True

    def stop(self) -> None:
        """停止（幂等）。停止后 `publish` 一律返回 False。"""
        self._running = False

    def run(self) -> None:
        """事件循环：把当前队列**全部**排空。

        注意这里是「排空而不是常驻」：回测是一根一根 K 线推进的，常驻循环需要
        外部中断条件（时间源 / 信号），那属于实盘引擎的形态，I1 不做。事件处理器
        在处理过程中新 publish 的事件也会被这一轮排空（队列在增长，直到空为止）。
        """
        self.start()
        while self._queue:
            event = self._queue.popleft()
            for handler in list(self._handlers.get(event.event_type, {}).values()):
                handler(event)
            self._dispatched += 1

    def get_queue_size(self) -> int:
        """当前队列里还没处理的事件数。"""
        return len(self._queue)

    def clear(self) -> None:
        """清空队列（不动订阅表 —— 契约把「清空事件队列」和「停止」分开写了）。"""
        self._queue.clear()

    # ── 便于测试与诊断的只读视图（契约没有，登记在 manifest.members_extra）──
    @property
    def dispatched(self) -> int:
        """已经派发过的事件总数。回测结束时拿它跟 K 线根数对账，是「事件真的走完全程」的证据。"""
        return self._dispatched

    def subscriber_count(self, event_type: EventType) -> int:
        """某类型上的订阅者数量。"""
        return len(self._handlers.get(event_type, {}))

    def handler_ids(self, event_type: EventType) -> List[str]:
        """某类型上的订阅 ID，按注册顺序返回。"""
        return list(self._handlers.get(event_type, {}).keys())
