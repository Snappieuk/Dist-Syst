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
from collections import OrderedDict
from typing import Callable, Hashable

# The cache needs a limit, or a long-running server would slowly run out of memory.
# Clients only retry for a few seconds, so old entries are soon of no use.
MAX_ENTRIES = 10000


class RequestCache:
    def __init__(self, max_entries: int = MAX_ENTRIES) -> None:
        self._max_entries = max_entries

        # An OrderedDict remembers insertion order, so the oldest is easy to find.
        self._replies = OrderedDict()
        self._lock = threading.Lock()

    def run_once(self, key: Hashable, operation: Callable[[], dict]) -> dict:
        """Return the saved reply for `key`, or run `operation` and save its reply.

        The lock is held while the operation runs. WHY: a client can time out and
        retry while its first attempt is still running. Holding the lock makes the
        repeat wait, and then it finds the finished reply. Requests that change
        data already happen one at a time because of the store's lock, so this
        adds almost no extra waiting and keeps the code simple.

        Rejections such as DUPLICATE_ID are ordinary replies, so they are saved.
        If `operation` raises an exception, nothing is saved, so that a retry can
        try again after a temporary failure such as a full disk.
        """
        with self._lock:
            if key in self._replies:
                return self._replies[key]

            reply = operation()
            self._replies[key] = reply

            # Forget the oldest reply once the cache is full.
            if len(self._replies) > self._max_entries:
                self._replies.popitem(last=False)

            return reply
