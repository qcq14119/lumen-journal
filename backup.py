"""Bounded authenticated backup jobs; explicit restoration uses its own journal."""

from __future__ import annotations

import asyncio
import errno
import json
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path

from astrbot.api.web import error_response, json_response, request
from starlette.responses import StreamingResponse

from . import backup_data, backup_format

PRIVATE = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
CHUNK_BYTES = 1024 * 1024
MAX_UPLOAD_BYTES = 600 * 1024 * 1024
LEASE_SECONDS = 120
MODULES = ("reference", "life", "memory", "settings")


class BackupRequestError(ValueError):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.code, self.status = code, status


def _password(value, *, required=False):
    if value is None and not required:
        return None
    if not isinstance(value, str) or len(value.encode("utf-8")) > 1024:
        raise BackupRequestError("INVALID_PASSWORD")
    if required and len(value) < 8:
        raise BackupRequestError("PASSWORD_TOO_SHORT")
    return value or None


def _fixed_error(exc):
    # Only our error types have public reason codes. Never serialize exceptions.
    if isinstance(exc, BackupRequestError):
        return exc.code
    if isinstance(exc, backup_format.BackupError):
        return exc.code
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        return "DISK_FULL"
    return "BACKUP_FAILED"


class _Download(StreamingResponse):
    """Acquire the file lease when ASGI actually starts, release on disconnect."""

    def __init__(self, service, job, content, headers):
        super().__init__(content, media_type="application/octet-stream", headers=headers)
        self.service, self.job = service, job

    async def __call__(self, scope, receive, send):
        job = self.job
        if self.service.current is not job or job["release"] or job["state"] != "ready" or job["readers"]:
            await error_response("BACKUP_NOT_READY", status_code=409, headers=PRIVATE)(scope, receive, send)
            return
        job["readers"] += 1
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self.body_iterator.aclose()
            job["readers"] -= 1
            if job["release"]:
                self.service._cleanup(job)


class Backups:
    """One lease from upload/capture through worker exit and frozen download."""

    def __init__(self, store):
        self.store = store
        self.current = None
        self.closed = False

    def _reserve(self, owner, operation):
        if self.closed or self.current is not None:
            raise BackupRequestError("BACKUP_BUSY", 409)
        # This directory is private and never part of an archive inventory.
        root = Path(tempfile.mkdtemp(prefix="backup-", dir=self.store.root / "tmp"))
        root.chmod(0o700)
        job = {
            "task_id": uuid.uuid4().hex, "owner": owner, "operation": operation,
            "state": "running", "root": root, "cancel": threading.Event(),
            "touched": time.monotonic(), "release": False, "worker": None,
            "readers": 0, "upload_busy": False, "password": None,
        }
        self.current = job
        job["watchdog"] = asyncio.create_task(self._watch(job))
        return job

    def _get(self, owner, task_id, *, touch=True):
        job = self.current
        if not job or job["owner"] != owner or job["task_id"] != task_id:
            raise BackupRequestError("BACKUP_NOT_FOUND", 404)
        if touch:
            job["touched"] = time.monotonic()
        return job

    async def _watch(self, job):
        while self.current is job:
            await asyncio.sleep(5)
            if time.monotonic() - job["touched"] > LEASE_SECONDS:
                job["cancel"].set()
                job["release"] = True
                self._cleanup(job)

    def _cleanup(self, job):
        if self.current is not job:
            return
        worker = job["worker"]
        if (worker is not None and not worker.done()) or job["readers"] or job["upload_busy"]:
            return
        job["password"] = None
        try:
            shutil.rmtree(job["root"])
        except FileNotFoundError:
            pass
        except OSError:
            job["state"], job["error"] = "failed", "CLEANUP_FAILED"
            job["release"] = True
            return
        if self.current is job:
            self.current = None
        if job["watchdog"] is not asyncio.current_task():
            job["watchdog"].cancel()

    async def _run(self, job, work):
        thread = asyncio.create_task(asyncio.to_thread(work))
        try:
            # Repeated cancellation must not release the gate before the thread.
            while True:
                try:
                    result = await asyncio.shield(thread)
                    break
                except asyncio.CancelledError:
                    job["cancel"].set()
                    job["release"] = True
            if job["cancel"].is_set() and not job.get("committed"):
                job["state"] = "cancelled"
            else:
                job["summary"] = result
                job["state"] = "ready"
        except Exception as exc:
            if job.get("committed_summary") and _fixed_error(exc) != "RECOVERY_REQUIRED":
                job["summary"], job["state"] = job["committed_summary"], "ready"
            else:
                job["state"] = "cancelled" if job["cancel"].is_set() else "failed"
                job["error"] = _fixed_error(exc)
        finally:
            job["password"] = None
            # The worker is now actually finished; a callback owns cleanup.
            asyncio.get_running_loop().call_soon(self._finished, job)

    def _finished(self, job):
        if job["release"]:
            self._cleanup(job)
        elif job["state"] in ("failed", "cancelled"):
            # Retain only the safe status until release, never failed payloads.
            try:
                for path in job["root"].iterdir():
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
            except OSError:
                job["error"] = "CLEANUP_FAILED"
                job["release"] = True

    @staticmethod
    def _summary(manifest, validated_data, size):
        # Values here originate from the validated format/data contracts only.
        return {
            "package_id": manifest["package_id"],
            "plugin_version": manifest["plugin_version"], "created_utc": manifest["created_utc"],
            "modules": manifest["modules"], "counts": manifest.get("counts", {}),
            "bytes": size, "encrypted": manifest["encrypted"],
            "credentials_included": manifest.get("credentials_included", False),
            "dependencies": manifest.get("dependencies", {}),
            "omissions": manifest.get("omissions", []),
            "limitations": validated_data["limitations"],
            "restore_supported": False,
        }

    async def options(self, owner):
        job = self._reserve(owner, "options")

        def work():
            result = backup_data.estimate(self.store)
            result["limits"] = {"max_upload_bytes": MAX_UPLOAD_BYTES, "chunk_bytes": CHUNK_BYTES}
            return result

        job["worker"] = asyncio.create_task(self._run(job, work))
        try:
            await asyncio.shield(job["worker"])
            if job["state"] != "ready":
                raise BackupRequestError(job.get("error", "BACKUP_CANCELLED"))
            return job["summary"]
        finally:
            job["release"] = True
            job["cancel"].set()
            self._cleanup(job)

    def export(self, owner, body):
        modules = body.get("modules")
        if (not isinstance(modules, list) or not modules or
                any(not isinstance(m, str) or m not in MODULES for m in modules) or
                len(modules) != len(set(modules))):
            raise BackupRequestError("INVALID_MODULES")
        for field in ("include_keys", "include_future", "encrypt"):
            if not isinstance(body.get(field), bool):
                raise BackupRequestError("INVALID_OPTIONS")
        if body["include_keys"] and not body["encrypt"]:
            raise BackupRequestError("KEYS_REQUIRE_ENCRYPTION")
        password = _password(body.get("password"), required=body["encrypt"])
        if not body["encrypt"]:
            password = None
        job = self._reserve(owner, "export")

        def work():
            stage = job["root"] / "stage"
            stage.mkdir(mode=0o700)
            metadata = backup_data.capture(self.store, stage, modules, body["include_keys"], body["include_future"], job["cancel"])
            output = job["root"] / "backup.cab"
            backup_format.build_archive(stage, output, metadata, password, job["cancel"])
            shutil.rmtree(stage)
            # Verify our own output with the exact same read-only import gate.
            checked = job["root"] / "checked"
            checked.mkdir(mode=0o700)
            verified = backup_format.inspect_archive(output, checked, password, job["cancel"])
            validated_data = backup_data.validate_data(checked, verified, job["cancel"])
            summary = self._summary(verified, validated_data, output.stat().st_size)
            shutil.rmtree(checked)
            return summary

        job["worker"] = asyncio.create_task(self._run(job, work))
        return {"task_id": job["task_id"], "state": job["state"]}

    def begin_validation(self, owner, body):
        size = body.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_UPLOAD_BYTES:
            raise BackupRequestError("UPLOAD_LIMIT", 413)
        password = _password(body.get("password"))
        job = self._reserve(owner, "validate")
        job.update(state="uploading", expected_bytes=size, uploaded_bytes=0, password=password)
        return {"task_id": job["task_id"], "state": "uploading"}

    async def upload(self, owner, task_id, offset, get_file):
        job = self._get(owner, task_id)
        if (job["state"] != "uploading" or job["upload_busy"] or
                job["cancel"].is_set() or offset != str(job["uploaded_bytes"])):
            raise BackupRequestError("UPLOAD_CONFLICT", 409)
        job["upload_busy"] = True
        upload = None
        try:
            upload = await get_file()
            if upload is None:
                raise BackupRequestError("MISSING_UPLOAD")
            raw = await upload.read(CHUNK_BYTES + 1)
            if not raw or len(raw) > CHUNK_BYTES or len(raw) + job["uploaded_bytes"] > job["expected_bytes"]:
                raise BackupRequestError("UPLOAD_LIMIT", 413)
            if job["cancel"].is_set():
                raise BackupRequestError("BACKUP_CANCELLED")
            # One bounded write, no await between offset validation and append.
            if shutil.disk_usage(job["root"]).free < len(raw) + 64 * CHUNK_BYTES:
                raise BackupRequestError("DISK_SPACE")
            with (job["root"] / "upload.cab").open("ab") as handle:
                handle.write(raw)
            job["uploaded_bytes"] += len(raw)
            return {"uploaded_bytes": job["uploaded_bytes"]}
        except BaseException:
            job["cancel"].set()
            job["release"] = True
            raise
        finally:
            if upload is not None:
                await upload.close()
            job["upload_busy"] = False
            if job["release"]:
                self._cleanup(job)

    def validate(self, owner, task_id):
        job = self._get(owner, task_id)
        if (job["state"] != "uploading" or job["upload_busy"] or
                job["uploaded_bytes"] != job["expected_bytes"] or job["cancel"].is_set()):
            raise BackupRequestError("UPLOAD_INCOMPLETE", 409)
        job["state"] = "running"

        def work():
            checked = job["root"] / "checked"
            checked.mkdir(mode=0o700)
            source = job["root"] / "upload.cab"
            manifest = backup_format.inspect_archive(source, checked, job["password"], job["cancel"])
            validated_data = backup_data.validate_data(checked, manifest, job["cancel"])
            summary = self._summary(manifest, validated_data, source.stat().st_size)
            shutil.rmtree(checked)
            source.unlink()
            return summary

        job["worker"] = asyncio.create_task(self._run(job, work))
        return {"task_id": task_id, "state": "running"}

    def status(self, owner, task_id):
        job = self._get(owner, task_id)
        return {key: job[key] for key in ("task_id", "operation", "state", "summary", "error", "uploaded_bytes") if key in job}

    def release(self, owner, task_id, *, cancel=False):
        job = self._get(owner, task_id)
        job["cancel"].set()
        if cancel and job["state"] in ("running", "uploading"):
            worker = job["worker"]
            job["state"] = "cancelling" if worker is not None and not worker.done() else "cancelled"
        job["release"] = job["release"] or not cancel
        if cancel and job["worker"] is None:
            self._finished(job)
        if job["release"]:
            self._cleanup(job)
        return {"task_id": task_id, "state": "cancelled"}

    def download(self, owner, task_id):
        job = self._get(owner, task_id)
        if job["operation"] != "export" or job["state"] != "ready" or job["release"]:
            raise BackupRequestError("BACKUP_NOT_READY", 409)
        async def chunks():
            with (job["root"] / "backup.cab").open("rb") as handle:
                while not job["cancel"].is_set() and (block := handle.read(CHUNK_BYTES)):
                    job["touched"] = time.monotonic()
                    yield block
                    await asyncio.sleep(0)

        return _Download(self, job, chunks(), headers={
            **PRIVATE, "Content-Length": str(job["summary"]["bytes"]),
            "Content-Disposition": 'attachment; filename="companion-backup.cab"',
        })

    async def close(self):
        self.closed = True
        job = self.current
        if job is None:
            return
        job["cancel"].set()
        job["release"] = True
        if job["worker"]:
            await asyncio.shield(job["worker"])
        self._cleanup(job)


class BackupAPI:
    def __init__(self, plugin):
        self.context = plugin.context
        self.service = Backups(plugin.store)
        from .restore import Restores
        self.restore = Restores(self.service, plugin)

    def register(self, name):
        routes = (
            ("options", "GET"), ("export", "POST"), ("validate", "POST"),
            ("validate/start", "POST"), ("upload/<task_id>/<offset>", "POST"),
            ("status", "GET"), ("cancel", "POST"), ("release", "POST"),
            ("download", "GET"),
            ("import", "POST"), ("import/start", "POST"),
            ("archive/save", "POST"), ("archive/open", "POST"),
            ("archives", "GET"), ("archive/download", "GET"),
            ("restore/preview", "POST"), ("restore/commit", "POST"),
            ("restore/pending", "GET"), ("restore/check", "POST"), ("restore/enable", "POST"),
            ("restore/rollback-preview", "POST"), ("restore/rollback", "POST"),
        )
        for operation, method in routes:
            async def handle(operation=operation, **params):
                return await self.handle(operation, **params)
            self.context.register_web_api(f"/{name}/backup/{operation}", handle, [method], "Private backups")

    async def handle(self, operation, **params):
        if not request.username:
            return error_response("请登录后管理备份", status_code=401, headers=PRIVATE)
        owner = request.username
        form = None
        try:
            if operation.startswith("upload/"):
                if int(request.headers.get("content-length", "0")) > CHUNK_BYTES + 65536:
                    raise BackupRequestError("UPLOAD_LIMIT", 413)

                async def get_file():
                    nonlocal form
                    native = request._request
                    payload = bytearray()
                    async for block in native.stream():
                        if len(payload) + len(block) > CHUNK_BYTES + 65536:
                            raise BackupRequestError("UPLOAD_LIMIT", 413)
                        payload.extend(block)
                    # A bounded single chunk is parsed only after the network
                    # read completes, so disconnect cannot orphan parser spools.
                    from starlette.formparsers import MultiPartParser

                    async def parts():
                        yield bytes(payload)

                    parser = MultiPartParser(native.headers, parts(), max_files=1, max_fields=0, max_part_size=CHUNK_BYTES + 65536)
                    try:
                        form = await parser.parse()
                    except BaseException:
                        for handle in parser._files_to_close_on_error:
                            handle.close()
                        raise
                    return form.get("file")

                result = await self.service.upload(owner, params["task_id"], params["offset"], get_file)
            elif operation == "options":
                result = await self.service.options(owner)
            elif operation == "archives":
                result = await asyncio.to_thread(self.restore.archives, owner)
            elif operation == "archive/download":
                return self.restore.download(owner, request.query.get("id", ""))
            elif operation == "restore/pending":
                result = await asyncio.to_thread(self.restore.pending)
            elif operation in ("status", "download"):
                if operation == "download":
                    return self.service.download(owner, request.query.get("task_id", ""))
                result = self.service.status(owner, request.query.get("task_id", ""))
            else:
                # Read bounded JSON through the host request's ASGI stream.
                raw = bytearray()
                async for block in request._request.stream():
                    if len(raw) + len(block) > (262144 if operation.startswith("restore/") else 4096):
                        raise BackupRequestError("REQUEST_LIMIT", 413)
                    raw.extend(block)
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise BackupRequestError("INVALID_OPTIONS")
                if operation == "export":
                    result = self.service.export(owner, body)
                elif operation == "validate":
                    result = self.service.begin_validation(owner, body)
                elif operation == "validate/start":
                    result = self.service.validate(owner, body.get("task_id", ""))
                elif operation == "import":
                    result = self.restore.begin(owner, body)
                elif operation == "import/start":
                    result = self.restore.start(owner, body.get("task_id", ""))
                elif operation == "archive/save":
                    result = await self.restore.save_archive(owner, body)
                elif operation == "archive/open":
                    result = self.restore.open_archive(owner, body)
                elif operation == "restore/preview":
                    result = await self.restore.preview(owner, body)
                elif operation == "restore/commit":
                    result = await self.restore.commit(owner, body)
                elif operation == "restore/check":
                    result = await self.restore.check(owner, body)
                elif operation == "restore/enable":
                    result = await self.restore.enable(owner, body)
                elif operation == "restore/rollback-preview":
                    result = await asyncio.to_thread(self.restore.rollback_preview, owner)
                elif operation == "restore/rollback":
                    result = await self.restore.rollback(owner, body)
                elif operation in ("cancel", "release"):
                    result = self.service.release(owner, body.get("task_id", ""), cancel=operation == "cancel")
                else:
                    raise BackupRequestError("BACKUP_NOT_FOUND", 404)
            return json_response(result, headers=PRIVATE)
        except BackupRequestError as exc:
            return error_response(exc.code, status_code=exc.status, headers=PRIVATE)
        except backup_format.BackupError as exc:
            return error_response(exc.code, status_code=409, headers=PRIVATE)
        except (ValueError, TypeError, KeyError):
            return error_response("INVALID_OPTIONS", status_code=400, headers=PRIVATE)
        except Exception as exc:
            from .restore_guard import ERROR_CODES
            from .store import AlbumError
            if isinstance(exc, AlbumError) and exc.args and exc.args[0] in ERROR_CODES:
                return error_response(exc.args[0], status_code=409, headers=PRIVATE)
            return error_response("BACKUP_FAILED", status_code=500, headers=PRIVATE)
        finally:
            if form is not None:
                await form.close()
