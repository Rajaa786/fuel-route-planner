import threading

from apps.planner import wiring


def test_shared_collaborators_are_built_once_under_concurrency():
    """Regression: a cold start with several threads built several geocoder rate limiters."""
    wiring._build_shared.cache_clear()
    barrier = threading.Barrier(8)
    seen = []

    def worker():
        barrier.wait()
        seen.append(wiring._shared())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(seen) == 8
    assert len({id(routing) for routing, _ in seen}) == 1
    assert len({id(resolver) for _, resolver in seen}) == 1
    wiring._build_shared.cache_clear()
