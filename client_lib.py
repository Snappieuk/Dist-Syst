"""
Client-side networking shared by every client program.

Fault tolerance logic is easy to get subtly wrong, so it lives here once rather
than being copied into send.py, receive.py and gui.py.
"""

import random  # adds jitter to the retry delays (explained below)
import socket  # provides the networking building blocks (sockets)
import time    # time.sleep() waits between retries
import uuid    # generates a unique ID for each request

from common import HOST, PORT, receive_message, send_message

# How long to wait for the server before treating an attempt as failed. Without
# a timeout, a server that has stalled would leave the client waiting forever.
TIMEOUT_SECONDS = 5

MAX_ATTEMPTS = 3

# The delay before the first retry; it doubles after each failure.
BASE_BACKOFF_SECONDS = 0.5


class ServerUnavailableError(OSError):
    """Every attempt to reach the server failed.

    The message is written for the end user, so programs can display it directly
    instead of showing a traceback. It is an OSError because that is what a
    connection failure normally is, so existing handlers still catch it.
    """


def _attempt(request: dict) -> dict:
    """Make one attempt: connect, send the request and wait for the reply."""
    with socket.create_connection((HOST, PORT), timeout=TIMEOUT_SECONDS) as sock:
        send_message(sock, request)
        return receive_message(sock)


def send_request(request: dict) -> dict:
    """Send a request to the server, retrying if the network fails.

    WHY RETRYING NEEDS AN ID: when a reply does not arrive, the client cannot
    tell which of these happened:
        1. the request never reached the server, or
        2. the server did the work, but the reply was lost.
    Retrying is the right response to (1), but in case (2) it would send the
    server the same request twice. So a request_id is generated once here and
    reused by every retry. The server remembers the IDs it has seen and returns
    the original reply for a repeat, which makes retrying safe in both cases.
    The result is effectively "exactly once" processing, built from
    at-least-once delivery plus duplicate detection on the server.

    Raises:
        ServerUnavailableError: every attempt failed at the network level.
        ValueError: the server replied with something that is not a valid
            message. This is not retried, as the network is working and repeating
            the request would not change the outcome.
    """
    # Copy rather than modify the caller's dict, and keep any ID it already has.
    request = {**request, "request_id": request.get("request_id") or str(uuid.uuid4())}

    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return _attempt(request)
        except OSError as exc:  # includes ConnectionError and socket.timeout
            last_error = exc

        if attempt < MAX_ATTEMPTS:
            # WHY BACKOFF: a struggling server recovers faster if clients ease
            # off, whereas immediate retries add to the load causing the problem.
            #
            # WHY JITTER: if a server restarts, many clients fail at the same
            # moment. With identical delays they would all retry at the same
            # moment too and overload it again. A small random extra delay
            # spreads them out.
            delay = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
            time.sleep(delay + random.uniform(0, delay / 2))

    raise ServerUnavailableError(
        f"Could not reach the asset server at {HOST}:{PORT} "
        f"after {MAX_ATTEMPTS} attempts ({last_error}).\n"
        "Check that server.py is running, then try again."
    )
