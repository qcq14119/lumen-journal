"""Bounded generation attempts with one usable winner and private result caches."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import shutil
import warnings

from PIL import Image

from .provider import ProviderError, parse_result
from .store import AlbumError, MAX_BYTES, MAX_PIXELS, credential_entries, now


def provider_settings(store, snapshot):
    """Resolve credentials only from the same configured provider and address."""
    sites = [{**store.settings, "id": "primary"}, *store.settings["providers"]]
    site = next((p for p in sites if p["id"] == snapshot.get("id", "primary")), None)
    if not site or site["base_url"] != snapshot["base_url"]:
        raise ProviderError(
            "原图片服务配置已改变，请恢复该站配置后再查询", unknown=True
        )
    key = next(
        (
            k["value"]
            for k in credential_entries(site)
            if k["id"] == snapshot.get("key_id", "legacy")
        ),
        "",
    )
    if not key:
        raise ProviderError(
            "原任务使用的密钥已移除或修改，请核对原站任务", unknown=True
        )
    return {
        **{k: store.settings[k] for k in ("task_timeout", "poll_interval")},
        **snapshot,
        "api_key": key,
    }


class Attempts:
    def __init__(self, engine, key):
        self.engine, self.store, self.key = engine, engine.store, key
        self.job = self.store.job(key)
        self.policy = self.job["retry_policy"]
        self.first_failure = asyncio.Event()
        self.workers = []

    def update(self, attempt, **patch):
        attempt.update(patch)
        job = self.store.job(self.key)
        rows = [a for a in job.get("attempts", []) if a["id"] != attempt["id"]]
        rows.append(dict(attempt))
        self.store.update_job(self.key, job["state"], attempts=rows)

    def cache_path(self, attempt):
        name = hashlib.sha256((self.key + ":" + attempt["id"]).encode()).hexdigest()
        return self.engine.results / (name + ".json")

    async def attempt(self, site, previous=None):
        """Persist before POST; download and decode before claiming success.

        Args:
            site: Immutable provider configuration without credentials.
            previous: An interrupted attempt with a known task ID, polled only.

        Returns:
            The winning attempt record and decoded image bytes.
        """
        rows = self.store.job(self.key).get("attempts", [])
        attempt = (
            dict(previous)
            if previous
            else {
                "id": str(len(rows) + 1),
                "provider_id": site["id"],
                "provider_name": site["name"],
                "state": "preparing",
                "at": now(),
            }
        )
        self.update(attempt)
        stage = "prepare"
        try:
            settings = provider_settings(self.store, site)
            async with self.engine.provider_factory(settings) as client:
                if previous:
                    stage = "poll"
                    data = await client.poll(attempt["task_id"])
                else:
                    if (
                        self.store.cleanup_media()["capacity_blocked"]
                        or shutil.disk_usage(self.store.root).free < 512 * 1024 * 1024
                    ):
                        raise AlbumError("相册容量或可用磁盘不足，未提交图片请求")
                    refs = [
                        await asyncio.to_thread(
                            self.store.asset_path(
                                self.store.get("asset", aid)
                            ).read_bytes
                        )
                        for aid in self.job["reference_ids"]
                    ]
                    stage = "submit"
                    self.update(attempt, state="submitting")
                    self.store.update_job(self.key, "running")
                    data = await client.submit(self.job["prompt"], refs)
                deadline = now() + settings["task_timeout"]
                while True:
                    kind, value = parse_result(data, attempt.get("task_id", ""))
                    if kind != "pending":
                        break
                    stage = "poll"
                    self.update(attempt, state="running", task_id=value)
                    if now() >= deadline:
                        raise ProviderError(
                            "等待图片超时，可继续查询已有任务",
                            unknown=True,
                            code="poll_timeout",
                        )
                    await asyncio.sleep(settings["poll_interval"])
                    data = await client.poll(value)
                if kind not in ("url", "base64"):
                    raise ProviderError(
                        value,
                        unknown=kind == "unsupported",
                        terminal=kind == "failed",
                        retryable=kind == "failed",
                        code=kind,
                    )
                stage = "download"
                path = self.cache_path(attempt)
                temp = path.with_suffix(".tmp")
                temp.write_text(
                    json.dumps({"kind": kind, "value": value}), encoding="utf-8"
                )
                temp.chmod(0o600)
                os.replace(temp, path)
                self.update(attempt, state="downloading", result_available=True)
                raw = None
                # Download retries reuse this exact result; they never regenerate.
                for number in range(self.policy["max_retries"] + 1):
                    try:
                        raw = await client.image_bytes(kind, value)
                        if not raw or len(raw) > MAX_BYTES:
                            raise AlbumError("图片大小不符合限制")
                        with warnings.catch_warnings():
                            warnings.simplefilter(
                                "error", Image.DecompressionBombWarning
                            )
                            with Image.open(io.BytesIO(raw)) as image:
                                if (
                                    image.format not in ("PNG", "JPEG", "WEBP")
                                    or image.width * image.height > MAX_PIXELS
                                    or getattr(image, "n_frames", 1) != 1
                                ):
                                    raise AlbumError("图片格式或像素不符合限制")
                                image.load()
                        break
                    except (
                        AlbumError,
                        OSError,
                        ValueError,
                        Image.DecompressionBombWarning,
                        Image.DecompressionBombError,
                    ) as exc:
                        self.update(attempt, download_attempts=number + 1)
                        if number == self.policy["max_retries"]:
                            if isinstance(exc, AlbumError):
                                raise
                            raise ProviderError(
                                "已生成但无法读取原图",
                                stage="download",
                                code=type(exc).__name__,
                            ) from exc
                        await asyncio.sleep(self.policy["retry_delay"])
                self.update(attempt, state="completed", finished=now())
                return attempt, raw, site
        except asyncio.CancelledError:
            self.update(
                attempt,
                state="abandoned" if attempt["state"] == "completed" else "unknown",
                code="local_wait_cancelled",
                stage=stage,
                finished=now(),
            )
            raise
        except AlbumError as exc:
            unknown = getattr(exc, "unknown", False) or (
                stage == "poll" and not getattr(exc, "terminal", False)
            )
            self.update(
                attempt,
                state="download_failed"
                if stage == "download"
                else "unknown"
                if unknown
                else "failed",
                error=str(exc),
                stage=stage,
                finished=now(),
                **{
                    k: v
                    for k, v in getattr(exc, "diagnostic", {}).items()
                    if k != "stage"
                },
            )
            if getattr(exc, "retry_after", 0):
                self.update(attempt, retry_after=exc.retry_after)
            exc.unknown = unknown
            raise
        except Exception as exc:
            self.update(
                attempt,
                state="unknown",
                stage=stage,
                code=type(exc).__name__,
                finished=now(),
            )
            raise ProviderError(
                "图片请求结果待核对", unknown=True, code=type(exc).__name__
            ) from exc

    async def sequence(self, sites):
        for site in sites:
            old = [
                a
                for a in self.store.job(self.key).get("attempts", [])
                if a["provider_id"] == site["id"]
            ]
            pending = next(
                (
                    a
                    for a in old
                    if a["state"] in ("running", "unknown") and a.get("task_id")
                ),
                None,
            )
            # A process interruption never grants another generation attempt.
            if any(
                a["state"] in ("submitting", "preparing", "unknown")
                and not a.get("task_id")
                for a in old
            ):
                return None
            count = len(old)
            if self.job.get("recovery_only") and not pending:
                continue
            while pending or count <= self.policy["max_retries"]:
                try:
                    pause = next(
                        (
                            p
                            for p in self.store.all("provider_pause")
                            if p["base_url"] == site["base_url"] and p["until"] > now()
                        ),
                        None,
                    )
                    if pause:
                        await asyncio.sleep(pause["until"] - now())
                    return await self.attempt(site, pending)
                except AlbumError as exc:
                    rows = self.store.job(self.key).get("attempts", [])
                    last = next(
                        a for a in reversed(rows) if a["provider_id"] == site["id"]
                    )
                    pending = None
                    count = len([a for a in rows if a["provider_id"] == site["id"]])
                    if last["state"] == "download_failed":
                        return None
                    if self.job.get("recovery_only"):
                        break
                    if exc.unknown and (
                        not self.policy["retry_unknown"] or last.get("task_id")
                    ):
                        return None
                    self.first_failure.set()
                    if getattr(exc, "retry_after", 0):
                        pause_id = hashlib.sha256(
                            site["base_url"].encode()
                        ).hexdigest()[:16]
                        previous = next(
                            (
                                p
                                for p in self.store.all("provider_pause")
                                if p["id"] == pause_id
                            ),
                            None,
                        )
                        self.store.save(
                            "provider_pause",
                            pause_id,
                            {
                                "base_url": site["base_url"],
                                "until": now() + exc.retry_after,
                            },
                            previous["revision"] if previous else None,
                        )
                    if (
                        not getattr(exc, "retryable", False)
                        or count > self.policy["max_retries"]
                    ):
                        break
                    await asyncio.sleep(
                        max(
                            self.policy["retry_delay"] * min(2 ** (count - 1), 8),
                            getattr(exc, "retry_after", 0),
                        )
                    )
        return None

    async def execute(self):
        sites = self.job["providers"]
        first = asyncio.create_task(self.sequence(sites[:1]))
        self.workers.append(first)
        if self.job.get("recovery_only") and len(sites) > 1:
            self.workers.append(asyncio.create_task(self.sequence(sites[1:])))
        signal = asyncio.create_task(self.first_failure.wait())
        winner = None
        try:
            await asyncio.wait([first, signal], return_when=asyncio.FIRST_COMPLETED)
            if (
                not self.job.get("recovery_only")
                and self.policy["parallel_backup"]
                and signal.done()
                and len(sites) > 1
                and not (first.done() and first.result())
            ):
                self.workers.append(asyncio.create_task(self.sequence(sites[1:])))
            active = set(self.workers)
            while active:
                done, active = await asyncio.wait(
                    active, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    result = task.result()
                    if result and winner is None:
                        winner = result
                if winner:
                    break
            if not winner and len(self.workers) == 1 and len(sites) > 1:
                rows = self.store.job(self.key).get("attempts", [])
                uncertain = any(a["state"] == "unknown" for a in rows)
                queryable = any(
                    a["state"] == "unknown" and a.get("task_id") for a in rows
                )
                generated = any(a.get("result_available") for a in rows)
                if (
                    not generated
                    and not queryable
                    and (not uncertain or self.policy["retry_unknown"])
                ):
                    backup = asyncio.create_task(self.sequence(sites[1:]))
                    self.workers.append(backup)
                    winner = await backup
        finally:
            signal.cancel()
            for task in self.workers:
                if not task.done():
                    task.cancel()
            await asyncio.gather(signal, *self.workers, return_exceptions=True)
        rows = self.store.job(self.key).get("attempts", [])
        if not winner:
            cached = next(
                (
                    a
                    for a in rows
                    if a.get("result_available") and self.cache_path(a).exists()
                ),
                None,
            )
            if cached:
                site = next(p for p in sites if p["id"] == cached["provider_id"])
                os.replace(self.cache_path(cached), self.engine.result_path(self.key))
                self.store.update_job(
                    self.key,
                    "downloading",
                    provider=site,
                    result_available=True,
                    upstream_completed=True,
                )
            uncertain = any(a["state"] == "unknown" for a in rows)
            last = rows[-1] if rows else {}
            raise ProviderError(
                "图片已生成，但原图下载未完成，可恢复下载原结果"
                if cached
                else last.get("error", "请求结果待核对"),
                unknown=uncertain,
                stage="download" if cached else last.get("stage", "provider"),
                code=last.get("code", ""),
                http_status=last.get("http_status"),
            )
        attempt, raw, site = winner
        os.replace(self.cache_path(attempt), self.engine.result_path(self.key))
        self.store.update_job(
            self.key,
            "downloading",
            provider=site,
            winner=attempt["id"],
            result_available=True,
            upstream_completed=True,
        )
        for row in rows:
            self.cache_path(row).unlink(missing_ok=True)
            if row["id"] != attempt["id"] and row["state"] == "completed":
                self.update(row, state="unused")
        # Do not overwrite settings changed by an administrator during the run.
        if (
            self.store.settings["settings_revision"] == self.job["settings_revision"]
            and site["id"] != self.store.settings["preferred_provider"]
        ):
            self.store.save_settings(
                {
                    "preferred_provider": site["id"],
                    "settings_revision": self.job["settings_revision"],
                }
            )
        return raw
