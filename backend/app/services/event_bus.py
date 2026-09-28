"""In-process publish/subscribe bus for SSE fan-out (single-process v0.1).

Delivery policy (M2 review finding I6):

- Persisted activity events (``"id" is not None``) are never silently
  dropped in place of a higher id. When a subscriber's activity lane is
  full (slow consumer), that subscriber is forced to disconnect: the
  already-buffered events are delivered in order, then ``get`` returns the
  disconnect sentinel ``None`` so the SSE stream ends cleanly. The
  client's last delivered activity id therefore never skips an id, and a
  reconnect replays the remainder from the database via ``Last-Event-ID``.
- Transient job snapshots (``"id" is None``) use drop-oldest
  backpressure: a slow consumer loses only the oldest live state, the
  newest snapshot is always preserved, and job state is recoverable via
  ``GET /api/v1/jobs/{id}``.
- Subscribers are isolated; a slow subscriber never affects others.
"""

import asyncio
from collections import deque
from typing import Any

_MAX_QUEUE_EVENTS = 256


class _Subscriber:
    """Per-subscriber buffer with separate activity and transient lanes.

    ``get`` raises ``TimeoutError`` on timeout and returns ``None`` once
    the subscriber is disconnected and its buffered events have been
    drained; the consuming stream should then end cleanly. The queue-like
    facade (``qsize``/``empty``/``get_nowait``) exists for tests and
    simple consumers.
    """

    def __init__(self, max_events: int = _MAX_QUEUE_EVENTS) -> None:
        self._activities: deque[dict[str, Any]] = deque()
        self._transients: deque[dict[str, Any]] = deque()
        self._max_events = max_events
        self._disconnected = False
        self._ready = asyncio.Event()

    def offer(self, event: dict[str, Any]) -> bool:
        """Buffer an event for this subscriber; False means it was rejected.

        A rejected event is only ever a persisted activity that would
        overflow the activity lane: the subscriber is disconnected instead
        of skipping an id that the client's cursor would then jump over.
        """
        if self._disconnected:
            return False
        if event.get("id") is not None:
            if len(self._activities) >= self._max_events:
                self._disconnect()
                return False
            self._activities.append(event)
        else:
            if len(self._transients) >= self._max_events:
                # Transient backpressure: drop-oldest, the newest live
                # state is always preserved.
                self._transients.popleft()
            self._transients.append(event)
        self._ready.set()
        return True

    def _disconnect(self) -> None:
        self._disconnected = True
        self._ready.set()

    def _pop(self) -> dict[str, Any] | None:
        # Activities first so the durable activity cursor keeps advancing
        # ahead of lossy transient snapshots.
        if self._activities:
            return self._activities.popleft()
        if self._transients:
            return self._transients.popleft()
        return None

    async def get(self, timeout: float) -> dict[str, Any] | None:
        """Await the next event.

        Raises ``TimeoutError`` when no event arrives within ``timeout``
        seconds. Returns ``None`` (disconnect sentinel) once the subscriber
        is disconnected and its buffered events have been drained.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            event = self._pop()
            if event is not None:
                return event
            if self._disconnected:
                return None
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            await asyncio.wait_for(self._ready.wait(), timeout=remaining)
            self._ready.clear()

    def qsize(self) -> int:
        return len(self._activities) + len(self._transients)

    def empty(self) -> bool:
        return self.qsize() == 0

    def get_nowait(self) -> dict[str, Any]:
        event = self._pop()
        if event is None:
            raise asyncio.QueueEmpty
        return event


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[_Subscriber] = set()

    def subscribe(self) -> _Subscriber:
        subscriber = _Subscriber()
        self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: _Subscriber) -> None:
        subscriber._disconnect()
        self._subscribers.discard(subscriber)

    def publish(self, event: dict[str, Any]) -> None:
        for subscriber in list(self._subscribers):
            subscriber.offer(event)
