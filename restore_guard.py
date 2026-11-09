"""Restoration maintenance boundary, explicit task ownership and pending objects.

This is not a scheduler. It refuses a restore while existing work is active and
prevents new work crossing the commit epoch. Restored objects remain quarantined
by persistent records until the separate activation checks remove their gates.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import inspect
import threading
import time


_operation = ContextVar("album_restore_operation", default=None)
_privileged = ContextVar("album_restore_privileged", default=None)
ERROR_CODES = frozenset({"RESTORE_MAINTENANCE", "RESTORE_BUSY", "RESTORE_STALE_OPERATION",
                         "RESTORE_PENDING_ACTIVATION", "RESTORE_TICKET_INVALID"})
PENDING_JOB_SQL = (
    " AND coalesce(json_extract(jobs.data,'$.restore_obsolete'),'')='' "
    "AND NOT EXISTS (SELECT 1 FROM records rp WHERE rp.kind='restore_pending' AND ("
    "(json_extract(rp.data,'$.kind')='character' AND json_extract(rp.data,'$.target_id')=json_extract(jobs.data,'$.character_id')) OR "
    "(json_extract(rp.data,'$.kind')='memory' AND json_extract(rp.data,'$.scope')=jobs.scope "
    "AND json_extract(rp.data,'$.cid')=json_extract(jobs.data,'$.conversation_id'))))"
)


def guard_for(owner):
    guard = getattr(owner, "restore_guard", None)
    if guard is not None:
        return guard
    store = getattr(owner, "store", None)
    return getattr(store, "restore_guard", None)


def managed(function):
    """Lease one explicitly decorated business operation, including its awaits."""
    if inspect.iscoroutinefunction(function):
        @wraps(function)
        async def asynchronous(self, *args, **kwargs):
            guard = guard_for(self)
            if guard is None:
                return await function(self, *args, **kwargs)
            try:
                guard.check()
                if args and hasattr(args[0], "get_extra"):
                    guard.check_event(args[0])
            except Exception as error:
                if function.__name__.startswith("api_"):
                    from astrbot.api.web import error_response
                    code = error.args[0] if error.args and isinstance(error.args[0], str) and error.args[0] in ERROR_CODES else "RESTORE_MAINTENANCE"
                    return error_response(code, status_code=409, headers={"Cache-Control": "no-store"})
                raise
            with guard.operation():
                return await function(self, *args, **kwargs)
        return asynchronous

    @wraps(function)
    def synchronous(self, *args, **kwargs):
        guard = guard_for(self)
        if guard is None:
            return function(self, *args, **kwargs)
        with guard.operation():
            return function(self, *args, **kwargs)
    return synchronous


class RestoreGuard:
    def __init__(self, store, plugin=None):
        self.store, self.plugin = store, plugin
        self.epoch = time.time_ns()
        self.active = 0
        self.frozen = False
        self._mutex = threading.RLock()
        self._ticket = None

    @staticmethod
    def _error(code):
        from .store import AlbumError
        error = AlbumError(code)
        error.status_code = 409
        return error

    def check(self):
        if _privileged.get() is self:
            return
        context = _operation.get()
        with self._mutex:
            if context is not None and context[0] is self and context[1] != self.epoch:
                raise self._error("RESTORE_STALE_OPERATION")
            if self.frozen:
                raise self._error("RESTORE_MAINTENANCE")

    def check_epoch(self, epoch):
        self.check()
        if epoch != self.epoch:
            raise self._error("RESTORE_STALE_OPERATION")

    def check_event(self, event):
        epoch = event.get_extra("companion_restore_epoch")
        if epoch is None:
            event.set_extra("companion_restore_epoch", self.epoch)
        else:
            self.check_epoch(epoch)

    @contextmanager
    def operation(self):
        with self._mutex:
            self.check()
            self.active += 1
            token = _operation.set((self, self.epoch))
        try:
            yield
        finally:
            _operation.reset(token)
            with self._mutex:
                self.active -= 1

    @contextmanager
    def privileged(self):
        """Only the coordinator uses this inside its actual commit/rollback."""
        token = _privileged.set(self)
        try:
            yield
        finally:
            _privileged.reset(token)

    def pending(self, kind, target_id="", *, scope="", cid=""):
        with self.store.lock:
            row = self.store.db.execute(
                "SELECT 1 FROM records WHERE kind='restore_pending' "
                "AND json_extract(data,'$.kind')=? AND "
                "(CASE WHEN ?='memory' THEN json_extract(data,'$.scope')=? AND json_extract(data,'$.cid')=? "
                "ELSE json_extract(data,'$.target_id')=? END) LIMIT 1",
                (kind, kind, scope, cid, target_id),
            ).fetchone()
        return row is not None

    def check_chat(self, scope, cid):
        self.check()
        if _privileged.get() is not self and self.pending("memory", scope=scope, cid=cid):
            raise self._error("RESTORE_PENDING_ACTIVATION")

    def check_object(self, kind, target_id):
        self.check()
        if _privileged.get() is not self and self.pending(kind, target_id):
            raise self._error("RESTORE_PENDING_ACTIVATION")

    def check_job(self, job):
        self.external_allowed()
        supplied = job
        if job.get("id"):
            job = self.store.job(job["id"])
        if job.get("restore_obsolete") or supplied.get("restore_obsolete"):
            raise self._error("RESTORE_STALE_OPERATION")
        self.check_object("character", job.get("character_id", ""))
        scope, cid = job.get("scope", ""), job.get("conversation_id", "")
        if not cid:
            return
        self.check_chat(scope, cid)
        with self.store.lock:
            if self.store.db.execute("SELECT 1 FROM sqlite_master WHERE name='mem_chats'").fetchone():
                row = self.store.db.execute("SELECT generation FROM mem_chats WHERE scope=? AND cid=?", (scope, cid)).fetchone()
                if row and any(value.get("memory_generation") is not None and value["memory_generation"] != row[0]
                               for value in (supplied, job)):
                    raise self._error("RESTORE_STALE_OPERATION")

    def external_allowed(self):
        self.check()
        with self.store.lock:
            blocked = self.store.db.execute(
                "SELECT 1 FROM records WHERE kind='restore_pending' "
                "AND json_extract(data,'$.kind')='settings' LIMIT 1"
            ).fetchone()
        if blocked and _privileged.get() is not self:
            raise self._error("RESTORE_PENDING_ACTIVATION")

    def busy(self):
        """Known workers plus full host turns, not just summary tasks."""
        if self.active:
            return True
        plugin = self.plugin
        if plugin is None:
            return False
        paths = (
            ("engine", "tasks"), ("plans", "workers"), ("notice_tasks",),
            ("memory_runtime", "active"), ("memory_service", "tasks"),
            ("memory_service", "busy"), ("memory_service", "_backup_tasks"),
            ("memory_service", "_backup_handles"),
            ("memory_service", "images", "tasks"), ("memory_service", "vectors", "running"),
            ("proactive", "tasks"), ("proactive", "inflight"),
        )
        for path in paths:
            value = plugin
            for part in path:
                value = getattr(value, part, None)
            if value:
                return True
        return False

    async def acquire(self, timeout=0):
        """Reject busy work or wait without cutting an in-flight platform send."""
        deadline = time.monotonic() + max(0, min(float(timeout), 30))
        while True:
            with self.store.lock, self._mutex:
                if self.frozen:
                    raise self._error("RESTORE_MAINTENANCE")
                if not self.busy():
                    self.frozen = True
                    self._ticket = object()
                    return self._ticket
            if time.monotonic() >= deadline:
                raise self._error("RESTORE_BUSY")
            await asyncio.sleep(0.05)

    def release(self, ticket, *, changed=True):
        with self.store.lock, self._mutex:
            if ticket is not self._ticket or not self.frozen:
                raise self._error("RESTORE_TICKET_INVALID")
            if changed:
                self.epoch += 1
                self.refresh()
            self._ticket = None
            self.frozen = False

    def refresh(self):
        plugin = self.plugin
        if plugin is None:
            return
        for owner, names in (
            (getattr(plugin, "memory_runtime", None), ("last",)),
            (getattr(plugin, "memory_service", None), ("failed",)),
            (getattr(getattr(plugin, "memory_service", None), "models", None), ("cache",)),
            (getattr(getattr(plugin, "memory_service", None), "images", None), ("_current", "_results")),
            (plugin, ("latest_chat",)),
        ):
            for name in names:
                value = getattr(owner, name, None)
                if value is not None:
                    value.clear()
