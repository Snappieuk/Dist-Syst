"""
Thread-safe, persistent storage for asset records.

The store is kept separate from the networking code so each can be understood
and tested alone: nothing here knows about sockets, and the server never has to
think about locks or files.
"""

import json       # the on-disk format, matching the format used on the wire
import os         # os.replace() and os.fsync() provide the crash safety below
import threading  # threading.Lock protects the data shared between client threads
from typing import Dict, List

from common import DUPLICATE_ID, RequestError


class AssetStore:
    """Holds every asset in memory and mirrors each change to a JSON file.

    Reads are served from memory because that is fast. The file exists so the
    data survives a crash or restart, and is only read at startup.
    """

    def __init__(self, path: str) -> None:
        self._path = path

        # One lock guards both the in-memory data and the file.
        #
        # WHY: every client has its own thread, so without a lock this race is
        # possible when two clients register the same asset_id:
        #     Thread A: is AST-1 taken? -> no
        #     Thread B: is AST-1 taken? -> no
        #     Thread A: store AST-1
        #     Thread B: store AST-1      <- duplicate!
        # The check and the store must behave as one indivisible step. The file
        # save is included because two threads writing the file at once could
        # leave an older version on disk after a newer one.
        #
        # The trade-off is that writes happen one at a time, which limits write
        # throughput. That is acceptable at this scale, and a single lock is
        # easy to reason about and cannot deadlock against a second lock.
        self._lock = threading.Lock()

        # Keyed by asset_id so the duplicate check does not have to scan a list.
        # Python dicts keep insertion order, so listings stay in the order
        # the assets were registered.
        self._assets: Dict[str, dict] = self._load()

    def _load(self) -> Dict[str, dict]:
        """Read the assets file, or start empty if it does not exist yet.

        A missing file is normal on first run. A corrupt file is different, so
        the error is allowed to propagate and stop the server: starting empty
        would mean the next save overwrites whatever could still be recovered.
        Refusing to start is safer than silently losing the data.
        """
        try:
            with open(self._path, "r", encoding="utf-8") as file:
                records = json.load(file)
        except FileNotFoundError:
            return {}

        if not isinstance(records, list) or not all(
            isinstance(record, dict) and "asset_id" in record for record in records
        ):
            raise ValueError(f"{self._path} does not contain a list of assets")

        return {record["asset_id"]: record for record in records}

    def _save(self) -> None:
        """Write every asset to disk atomically. The caller must hold the lock.

        WHY ATOMIC: opening the real file for writing empties it immediately. A
        crash during the write would leave it empty or half-written, losing data
        that was safe a moment earlier. So the new contents go into a temporary
        file, and os.replace() swaps it into place in one atomic operation.
        Anyone reading the path sees the complete old file or the complete new
        file, never something in between.
        """
        # The temp file must be in the same folder: os.replace() is only atomic
        # within one filesystem. A fixed name is safe because the lock ensures
        # only one thread is ever saving.
        temp_path = self._path + ".tmp"

        with open(temp_path, "w", encoding="utf-8") as file:
            json.dump(list(self._assets.values()), file, indent=2)

            # flush() moves Python's buffer to the operating system; fsync()
            # asks the operating system to put it on the disk. Without these
            # the rename could survive a power cut while the data does not.
            file.flush()
            os.fsync(file.fileno())

        os.replace(temp_path, self._path)

    def register(self, asset: dict) -> dict:
        """Store a new, already validated asset and return a copy of it.

        Raises RequestError(DUPLICATE_ID) if the asset_id is already in use.
        Raises OSError if the save fails, leaving the store unchanged.
        """
        asset_id = asset["asset_id"]

        with self._lock:
            if asset_id in self._assets:
                raise RequestError(DUPLICATE_ID, f"Asset ID {asset_id!r} is already registered")

            self._assets[asset_id] = dict(asset)

            try:
                self._save()
            except OSError:
                # Undo the change so memory and disk stay in agreement. The
                # client is about to be told this failed, so the asset must not
                # appear in later listings or vanish on the next restart.
                del self._assets[asset_id]
                raise

            return dict(asset)

    def list_all(self) -> List[dict]:
        """Return copies of every asset, taken while holding the lock.

        Copies matter because the caller sends the result over the network
        after the lock is released. Otherwise another thread could change a
        record while it is being encoded. Releasing the lock before sending also
        means one slow client cannot hold up every other client.
        """
        with self._lock:
            return [dict(asset) for asset in self._assets.values()]
