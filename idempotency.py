"""
Remembers recent replies so a retried request is not carried out twice.

An operation is "idempotent" if doing it twice has the same effect as doing it
once. Registering an asset is not naturally idempotent: the second attempt would
fail with DUPLICATE_ID, even though the first one worked and the client simply
never received the reply. This cache makes it idempotent from the client's point
of view, because a repeat gets the original reply back. See client_lib.py for
why clients need to retry at all.

LIMITATION: the cache is held in memory, so it is empty after a restart. A retry
that crosses a restart is carried out again. For REGISTER that is still safe,
because the duplicate check rejects it and nothing is stored twice, but the
client sees DUPLICATE_ID instead of the original success reply.
"""

import threading
import time
from collections import OrderedDict
from typing import Callable

# The cache needs limits, or a long-running server would slowly run out of memory.
# Ten minutes is far longer than any client keeps retrying a request.
DEFAULT_MAX_ENTRIES = 10000
DEFAULT_TTL_SECONDS = 600

# How long a repeat waits for the original request to finish.
IN_FLIGHT_WAIT_SECONDS = 30


class _Entry:
    """One request, which is either still running or finished."""

    def __init__(self) -> None:
        self.created = time.monotonic()
        self.done = threading.Event()  # set once the original attempt has finished
        self.reply = None              # stays None if the original attempt failed


class RequestCache:
    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES,
                 ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._lock = threading.Lock()

        # Ordered oldest first, so expired entries are always found at the front.
        self._entries = OrderedDict()

    def _evict(self) -> None:
        """Drop expired entries, then the oldest if still full. Caller holds the lock."""
        now = time.monotonic()
        while self._entries:
            oldest = next(iter(self._entries.values()))
            if (now - oldest.created <= self._ttl_seconds
                    and len(self._entries) <= self._max_entries):
                break
            self._entries.popitem(last=False)

    def run_once(self, key: str, operation: Callable[[], dict]) -> dict:
        """Return the reply for `key`, calling `operation` only if it is new.

        Rejections such as DUPLICATE_ID are ordinary replies, so they are cached.
        If `operation` raises, the entry is removed so a retry can try again. A
        temporary failure, such as a full disk, must not be remembered forever.
        """
        while True:
            with self._lock:
                entry = self._entries.get(key)
                is_new = entry is None
                if is_new:
                    entry = _Entry()
                    self._entries[key] = entry
                    self._evict()

            if is_new:
                # The lock is released while the operation runs, so a slow save
                # for one request does not hold up unrelated requests here.
                try:
                    entry.reply = operation()
                    return entry.reply
                except BaseException:
                    with self._lock:
                        if self._entries.get(key) is entry:
                            del self._entries[key]
                    raise
                finally:
                    # Always wake any waiting repeats, even after a failure.
                    entry.done.set()

            # WHY WAIT: a client can time out and retry while its first attempt is
            # still running. Checking only for a finished reply would miss that
            # case and carry out the request a second time.
            if not entry.done.wait(IN_FLIGHT_WAIT_SECONDS):
                raise TimeoutError(f"Timed out waiting for the original request {key!r}")

            if entry.reply is not None:
                return entry.reply

            # The original attempt failed and removed its entry, so go round the
            # loop again. This thread, or another repeat, will now carry it out.
