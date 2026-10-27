"""Asynchronous generation that persists before submitting or delivering."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil

from .provider import ImageProvider, ProviderError, parse_result
from .attempts import Attempts, provider_settings
from .store import AlbumError, Store, now, strings, text_value


class Engine:
    def __init__(self, store: Store, notify=None, provider_factory=ImageProvider):
        self.store = store
        self.notify = notify
        self.provider_factory = provider_factory
        self.tasks = {}
        self.semaphore = asyncio.Semaphore(3)
        self.notification_lock = asyncio.Lock()
        self.results = store.root / "results"
        self.results.mkdir(exist_ok=True)

    def result_path(self, key: str):
        """Resolve a private result cache only for an existing local job."""
        self.store.job(key)
        return self.results / (hashlib.sha256(key.encode()).hexdigest() + ".json")

    async def start(self):
        self.store.recover()
        for job in self.store.jobs():
            if job.get("providers") and job["state"] == "unknown":
                runner = Attempts(self, job["id"])
                cached = next(
                    (
                        a
                        for a in job.get("attempts", [])
                        if runner.cache_path(a).is_file()
                    ),
                    None,
                )
                if not cached and self.result_path(job["id"]).is_file():
                    cached = next(
                        (
                            a
                            for a in job.get("attempts", [])
                            if a.get("result_available")
                        ),
                        None,
                    )
                if cached:
                    site = next(
                        p for p in job["providers"] if p["id"] == cached["provider_id"]
                    )
                    if runner.cache_path(cached).is_file():
                        os.replace(
                            runner.cache_path(cached), self.result_path(job["id"])
                        )
                    job = self.store.update_job(
                        job["id"],
                        "download_failed",
                        provider=site,
                        result_available=True,
                        upstream_completed=True,
                        error="运行中断，原结果已保存，可恢复下载",
                    )
            if (
                job["state"] in ("queued", "running")
                or job["state"] == "ready"
                and (
                    job.get("delivery") == "pending"
                    or job.get("qzone_delivery") == "pending"
                )
            ):
                self.launch(job["id"])

    def launch(self, key: str):
        if key not in self.tasks or self.tasks[key].done():
            self.tasks[key] = asyncio.create_task(self.run(key))
            self.tasks[key].add_done_callback(lambda _: self.tasks.pop(key, None))

    def submit(
        self,
        character_id: str,
        scene: str,
        scope: str,
        request_id: str,
        reference_ids=None,
        conversation_id: str = "",
        deliver: bool = False,
    ) -> dict:
        sites = self.store.provider_configs()
        if not sites:
            raise AlbumError("请先配置图片服务地址、API Key 和模型")
        for pause in self.store.all("provider_pause"):
            if (
                len(sites) == 1
                and pause["base_url"] == sites[0]["base_url"]
                and pause["until"] > now()
            ):
                raise AlbumError("图片服务正在限流，请按服务端等待时间稍后再试")
        char = self.store.get("character", character_id)
        scene = text_value(scene, 3000)
        if not scene:
            raise AlbumError("请描述想拍下的画面")
        if not char.get("active_version"):
            raise AlbumError("请先上传并启用角色参考图")
        if self.store.cleanup_media()["capacity_blocked"]:
            raise AlbumError(
                "相册数量已达到上限，现有照片均被收藏或任务保护；请先整理相册或调整上限"
            )
        if shutil.disk_usage(self.store.root).free < 512 * 1024 * 1024:
            raise AlbumError("相册所在磁盘可用空间不足 512 MB，暂不提交新生图")
        version = next(x for x in char["versions"] if x["id"] == char["active_version"])
        refs = list(version["asset_ids"])
        for key in strings(reference_ids or [], 3):
            asset = (
                self.store.get("asset", key)
                if not deliver
                else self.store.authorized_asset(key, scope, "reference")
            )
            if asset["character_id"] != character_id or asset["trash"]:
                raise AlbumError("参考照片不属于当前角色或已移除")
            if key not in refs:
                refs.append(key)
        if len(refs) > 4:
            raise AlbumError("首版一次最多使用四张参考图，请减少场景图或角色参考")
        role_count = len(version["asset_ids"])
        prompt = (
            f"角色外观：{char['appearance']}\n视觉风格：{char['style']}\n"
            f"前 {role_count} 张参考图只用于保持角色身份；后续参考图提供场景或待编辑照片。"
            "服装、动作与构图以本次画面要求为准。输出单张完整场景，不要设定图拼版。\n"
            f"本次画面：{scene}"
        )
        fingerprint = hashlib.sha256(request_id.encode()).hexdigest()
        job, created = self.store.reserve_job(
            scope,
            fingerprint,
            {
                "character_id": character_id,
                "character_name": char["name"],
                "reference_version": version["id"],
                "reference_ids": refs,
                "scene": scene,
                "prompt": prompt,
                "failure_style": char["failure_style"],
                "conversation_id": conversation_id,
                "deliver": deliver,
                "delivery": "pending" if deliver else "none",
                "provider_task_id": "",
                "error": "",
                "asset_id": "",
                "provider": {k: v for k, v in sites[0].items() if k != "api_key"},
                "providers": [
                    {k: v for k, v in p.items() if k != "api_key"} for p in sites
                ],
                "retry_policy": {
                    k: self.store.settings[k]
                    for k in (
                        "max_retries",
                        "retry_delay",
                        "parallel_backup",
                        "retry_unknown",
                    )
                },
                "settings_revision": self.store.settings["settings_revision"],
            },
        )
        if created:
            self.launch(job["id"])
        return job

    async def resume(self, key: str):
        job = self.store.job(key)
        if key in self.tasks and not self.tasks[key].done():
            return
        if (
            job["state"] in ("download_failed", "downloading")
            and self.result_path(key).is_file()
        ):
            self.store.update_job(key, "downloading", error="")
            self.launch(key)
            return
        if (
            job.get("providers")
            and job["state"] in ("running", "unknown")
            and any(
                a.get("task_id")
                for a in job.get("attempts", [])
                if a["state"] in ("running", "unknown")
            )
        ):
            self.store.update_job(key, "running", recovery_only=True, error="")
            self.launch(key)
            return
        if job["state"] not in ("running", "unknown") or not job.get(
            "provider_task_id"
        ):
            raise AlbumError("没有可恢复查询的任务编号，不会自动重新提交生图")
        self.store.update_job(key, "running", error="", polling_started=now())
        self.launch(key)

    async def cancel(self, key: str):
        job = self.store.job(key)
        if job["state"] not in (
            "queued",
            "submitting",
            "running",
            "unknown",
            "downloading",
            "download_failed",
        ):
            raise AlbumError("此任务已经结束")
        self.store.update_job(
            key,
            "cancelled",
            error="已停止本地等待；上游任务可能仍会完成",
            result_available=False,
            **({"qzone_delivery": "cancelled"} if job.get("qzone_intent") else {}),
        )
        task = self.tasks.get(key)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.result_path(key).unlink(missing_ok=True)
        if job.get("providers"):
            runner = Attempts(self, key)
            for attempt in self.store.job(key).get("attempts", []):
                runner.cache_path(attempt).unlink(missing_ok=True)

    async def run(self, key: str):
        async with self.semaphore:
            job = self.store.job(key)
            if job["state"] in ("cancelled", "failed", "unknown", "download_failed"):
                return
            stage = "provider"
            try:
                if job["state"] != "ready":
                    if (
                        job.get("providers")
                        and job["state"] != "downloading"
                        and (job.get("attempts") or not job.get("provider_task_id"))
                    ):
                        raw = await Attempts(self, key).execute()
                        job = self.store.job(key)
                        stage = "import"
                        asset = await asyncio.to_thread(
                            self.store.import_image,
                            raw,
                            job["character_id"],
                            "generated",
                            job["scope"] if job["deliver"] else "",
                            job["scene"][:1000],
                            provenance={
                                "job_id": key,
                                "reference_version": job["reference_version"],
                                "reference_ids": job["reference_ids"],
                            },
                        )
                        job = self.store.update_job(
                            key,
                            "ready",
                            asset_id=asset["id"],
                            error="",
                            result_available=False,
                        )
                        self.result_path(key).unlink(missing_ok=True)
                    settings = (
                        provider_settings(self.store, job["provider"])
                        if job["state"] != "ready"
                        else None
                    )
                if job["state"] != "ready":
                    async with self.provider_factory(settings) as client:
                        if job["state"] == "downloading":
                            cached = json.loads(
                                self.result_path(key).read_text(encoding="utf-8")
                            )
                            data = {
                                "url"
                                if cached["kind"] == "url"
                                else "b64_json": cached["value"]
                            }
                        elif job["state"] == "queued":
                            if self.store.cleanup_media()["capacity_blocked"]:
                                raise AlbumError(
                                    "相册容量被受保护照片占满，未提交新生图"
                                )
                            if (
                                shutil.disk_usage(self.store.root).free
                                < 512 * 1024 * 1024
                            ):
                                raise AlbumError(
                                    "磁盘可用空间不足 512 MB，未提交新生图"
                                )
                            refs = [
                                await asyncio.to_thread(
                                    self.store.asset_path(
                                        self.store.get("asset", aid)
                                    ).read_bytes
                                )
                                for aid in job["reference_ids"]
                            ]
                            self.store.update_job(
                                key, "submitting", polling_started=now()
                            )
                            data = await client.submit(job["prompt"], refs)
                        else:
                            data = await client.poll(job["provider_task_id"])
                        while True:
                            result, value = parse_result(
                                data, self.store.job(key).get("provider_task_id", "")
                            )
                            if result == "pending":
                                job = self.store.update_job(
                                    key, "running", provider_task_id=value
                                )
                                if (
                                    now() - job.get("polling_started", job["created"])
                                    > settings["task_timeout"]
                                ):
                                    raise ProviderError(
                                        "等待图片超时，可继续查询已有任务", unknown=True
                                    )
                                await asyncio.sleep(settings["poll_interval"])
                                try:
                                    data = await client.poll(value)
                                except ProviderError as exc:
                                    exc.unknown = True
                                    raise
                                continue
                            if result not in ("base64", "url"):
                                raise ProviderError(
                                    value,
                                    unknown=result == "unsupported",
                                    terminal=result == "failed",
                                )
                            stage = "download"
                            self.store.update_job(
                                key, "downloading", upstream_completed=True
                            )
                            path = self.result_path(key)
                            temp = path.with_suffix(".tmp")
                            temp.write_text(
                                json.dumps({"kind": result, "value": value}),
                                encoding="utf-8",
                            )
                            temp.chmod(0o600)
                            os.replace(temp, path)
                            self.store.update_job(
                                key,
                                "downloading",
                                upstream_completed=True,
                                result_available=True,
                            )
                            raw = await client.image_bytes(result, value)
                            stage = "import"
                            asset = await asyncio.to_thread(
                                self.store.import_image,
                                raw,
                                job["character_id"],
                                "generated",
                                job["scope"] if job["deliver"] else "",
                                job["scene"][:1000],
                                provenance={
                                    "job_id": key,
                                    "reference_version": job["reference_version"],
                                    "reference_ids": job["reference_ids"],
                                },
                            )
                            job = self.store.update_job(
                                key,
                                "ready",
                                asset_id=asset["id"],
                                error="",
                                result_available=False,
                            )
                            try:
                                path.unlink(missing_ok=True)
                            except OSError:
                                pass
                            break
                if job.get("deliver") and self.notify:
                    # Keep a completed photo's caption and image together while
                    # other jobs independently generate or wait for their turn.
                    async with self.notification_lock:
                        await self.notify(job)
            except asyncio.CancelledError:
                raise
            except AlbumError as exc:
                if getattr(exc, "retry_after", 0):
                    pauses = self.store.all("provider_pause")
                    previous = next((p for p in pauses if p["id"] == "service"), None)
                    self.store.save(
                        "provider_pause",
                        "service",
                        {
                            "base_url": job["provider"]["base_url"],
                            "until": now() + exc.retry_after,
                        },
                        previous["revision"] if previous else None,
                    )
                current = self.store.job(key)
                if current["state"] == "ready":
                    self.store.update_job(
                        key, "ready", delivery="failed", error=str(exc)
                    )
                else:
                    state = (
                        "download_failed"
                        if current.get("upstream_completed")
                        else "unknown"
                        if getattr(exc, "unknown", False)
                        or current.get("provider_task_id")
                        and not getattr(exc, "terminal", False)
                        else "failed"
                    )
                    diagnostic = {
                        **getattr(exc, "diagnostic", {}),
                        "stage": getattr(exc, "diagnostic", {}).get("stage") or stage,
                        "at": now(),
                    }
                    job = self.store.update_job(
                        key,
                        state,
                        error=str(exc),
                        diagnostics=(current.get("diagnostics", []) + [diagnostic])[
                            -20:
                        ],
                    )
                    if job.get("deliver") and self.notify:
                        try:
                            async with self.notification_lock:
                                await self.notify(job)
                        except Exception:
                            self.store.update_job(key, state, delivery="failed")
            except Exception as exc:
                current = self.store.job(key)
                if current["state"] == "ready":
                    self.store.update_job(
                        key,
                        "ready",
                        delivery="unknown",
                        error="照片已入册，通知结果需核对",
                    )
                else:
                    self.store.update_job(
                        key,
                        "download_failed"
                        if current.get("upstream_completed")
                        else "unknown",
                        error="结果处理异常，已保留原任务；未自动重新生成",
                        diagnostics=(
                            current.get("diagnostics", [])
                            + [
                                {
                                    "stage": stage,
                                    "code": type(exc).__name__,
                                    "at": now(),
                                }
                            ]
                        )[-20:],
                    )

    async def close(self):
        for task in list(self.tasks.values()):
            task.cancel()
        await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)
