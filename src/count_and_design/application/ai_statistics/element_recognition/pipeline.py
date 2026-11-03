"""Resume page stages only when their provider dependencies have completed."""

from concurrent.futures import Future, ThreadPoolExecutor
from inspect import isgenerator
from threading import Lock


def submit_page(pool: ThreadPoolExecutor, function, *args) -> Future:
    """Run a page generator without parking a worker on yielded futures.

    Stages yield a tuple of dependencies. Their results (including errors) are
    consumed by the next stage, keeping the page's normal fallback handling.
    Ordinary callables are supported too, for injected page implementations.
    """
    result = Future()
    lock = Lock()
    stages = None

    def schedule():
        with lock:
            if result.done():
                return
            try:
                pool.submit(advance)
            except RuntimeError as exc:
                result.set_exception(exc)

    def advance():
        nonlocal stages
        if result.done():
            return
        try:
            if stages is None:
                stages = function(*args)
                if not isgenerator(stages):
                    with lock:
                        if not result.done():
                            result.set_result(stages)
                    return
            dependencies = tuple(next(stages))
        except StopIteration as completed:
            with lock:
                if not result.done():
                    result.set_result(completed.value)
            return
        except BaseException as exc:
            with lock:
                if not result.done():
                    result.set_exception(exc)
            return

        remaining = len(dependencies)
        dependency_lock = Lock()

        def ready(_future):
            nonlocal remaining
            with dependency_lock:
                remaining -= 1
                last = remaining == 0
            if last:
                schedule()

        if not dependencies:
            schedule()
        else:
            for dependency in dependencies:
                dependency.add_done_callback(ready)

    schedule()
    return result
