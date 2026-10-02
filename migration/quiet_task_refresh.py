"""Background task checks and stable, task-level route reconciliation."""
import copy
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from migration.task_availability import remove_unavailable_tasks


def reconcile_task_pool(current, fresh, protected=lambda route: False):
    """Keep route order/bundles, append new work, and never remove saved orders."""
    fresh_ids = {str(t['id']).strip() for r in fresh for t in r.get('data', [])}
    old_ids = {str(t['id']).strip() for r in current for t in r.get('data', [])}
    removed = set()
    result = []
    for route in current:
        missing = {str(t['id']).strip() for t in route.get('data', [])} - fresh_ids
        if protected(route):
            result.append(route)
        else:
            removed.update(missing)
            result.extend(remove_unavailable_tasks([route], missing))
    # Retain existing bundles and the route currently being edited. New tasks
    # arrive as separate routes rather than silently changing an existing offer.
    seen = set(old_ids)
    added = set()
    for route in fresh:
        new_routes = remove_unavailable_tasks([route], seen)
        for new in new_routes:
            ids = {str(t['id']).strip() for t in new.get('data', [])}
            result.append(new)
            added.update(ids)
            seen.update(ids)
    return result, len(added), len(removed)


class QuietTaskRefresh:
    """Coalesce sessions; failures retain the pool and wait before retrying."""
    def __init__(self, interval=300, clock=time.monotonic):
        self.interval, self.clock = interval, clock
        self.lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='routes-check')
        self.jobs = {}
        self.last_pull = None

    def poll(self, pods, build, force=False):
        key = tuple(sorted(pods))
        with self.lock:
            previous = self.jobs.get(key)
            if previous and (not previous[1].done() or
                             (not force and self.clock() - previous[0] < self.interval)):
                return previous[1]

            def check():
                refresh_source = force or self.last_pull is None or self.clock() - self.last_pull >= self.interval
                result = build(key, refresh_source)
                self.last_pull = self.clock()
                return copy.deepcopy(result)

            future = self.executor.submit(check)
            self.jobs[key] = (self.clock(), future)
            return future
