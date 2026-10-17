"""事件总线：把后台线程产生的事件转交给每个 WebSocket 连接所在的事件循环。"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

Event = dict[str, Any]


class EventBus:
    def __init__(self, max_pending: int = 1000) -> None:
        self._max_pending = max_pending
        self._lock = threading.Lock()
        self._subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue[Event]]] = []

    def subscribe(self) -> asyncio.Queue[Event]:
        """在事件循环里调用，返回该连接专用的队列。"""
        queue: asyncio.Queue[Event] = asyncio.Queue(self._max_pending)
        with self._lock:
            self._subscribers.append((asyncio.get_running_loop(), queue))
        return queue

    @property
    def listeners(self) -> int:
        """正连着的界面个数。"""
        with self._lock:
            return len(self._subscribers)

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        with self._lock:
            self._subscribers = [(loop, q) for loop, q in self._subscribers if q is not queue]

    def publish(self, event: Event) -> None:
        """任何线程都可以调用；某个连接处理不过来时丢弃它最旧的事件，不影响其他连接。"""
        with self._lock:
            subscribers = list(self._subscribers)
        for loop, queue in subscribers:
            try:
                loop.call_soon_threadsafe(_offer, queue, event)
            except RuntimeError:  # 事件循环已关闭
                self.unsubscribe(queue)


def _offer(queue: asyncio.Queue[Event], event: Event) -> None:
    if queue.full():
        queue.get_nowait()
    queue.put_nowait(event)
