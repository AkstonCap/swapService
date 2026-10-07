"""Track custody-capable threads through timeout and shutdown boundaries."""
import threading
import time
import weakref

_workers = weakref.WeakSet()
_lock = threading.Lock()


def start(thread):
    """Register before starting so a timeout cannot hide an unfinished RPC."""
    with _lock:
        _workers.add(thread)
        thread.start()


def drain(timeout_sec):
    """Join with one bounded budget; include workers spawned by workers."""
    deadline = time.monotonic() + timeout_sec
    while True:
        with _lock:
            workers = [worker for worker in _workers if worker.is_alive()]
        if not workers:
            return True
        if time.monotonic() >= deadline:
            return False
        for worker in workers:
            worker.join(max(0, deadline - time.monotonic()))
