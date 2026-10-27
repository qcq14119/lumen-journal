"""Bound detailed history while retaining durable request and delivery identities."""

from __future__ import annotations

import hashlib
import json

from .store import AlbumError, now, text_value


class History:
    def __init__(self, store):
        self.store = store

    def rows(self, kind, *, include_archived=False):
        """Read retained history in stable newest-first order."""
        if kind == "jobs":
            sql = "SELECT id,state,data,updated FROM jobs"
        elif kind == "deliveries":
            sql = "SELECT id,state,data,0 AS updated FROM deliveries"
        elif kind == "runs":
            sql = "SELECT id,json_extract(data,'$.state') AS state,data,revision AS updated FROM records WHERE kind='life_run'"
        else:
            raise AlbumError("记录类型不正确")
        if not include_archived:
            sql += (
                " AND" if kind == "runs" else " WHERE"
            ) + " json_extract(data,'$.archived') IS NOT 1"
        with self.store.lock:
            rows = self.store.db.execute(sql + " ORDER BY rowid DESC").fetchall()
        return [
            {
                **json.loads(r["data"]),
                "id": r["id"],
                "state": r["state"],
                "_version": r["updated"],
            }
            for r in rows
        ]

    def token(self, item):
        return hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()

    def protection(self, kind, item, busy=()):
        """Explain why a record must retain its details and recovery information."""
        if kind == "jobs":
            if item["id"] in busy or item["state"] not in (
                "ready",
                "failed",
                "cancelled",
            ):
                return "任务正在处理或结果待核对"
            if item.get("result_available"):
                return "仍有待恢复的原图结果"
            if item.get("qzone_delivery") in ("pending", "sending", "unknown") or (
                item["state"] == "ready"
                and item.get("delivery") in ("pending", "sending", "unknown")
            ):
                return "照片或空间投递尚未结束"
            pending = self.store.db.execute(
                "SELECT 1 FROM deliveries WHERE json_extract(data,'$.job_id')=? AND state IN ('sending','unknown') LIMIT 1",
                (item["id"],),
            ).fetchone()
            return "投递结果待核对" if pending else ""
        if item["state"] not in ("sent", "failed", "held", "skipped", "cancelled"):
            return "执行或投递结果待核对"
        if kind == "deliveries" and item.get("job_id"):
            try:
                return self.protection("jobs", self.store.job(item["job_id"]), busy)
            except AlbumError:
                pass
        if (
            kind == "runs"
            and self.store.db.execute(
                "SELECT 1 FROM records WHERE kind='life_plan' AND id=?",
                (item.get("plan_id"),),
            ).fetchone()
        ):
            last = self.store.db.execute(
                "SELECT id FROM records WHERE kind='life_run' AND json_extract(data,'$.plan_id')=? ORDER BY json_extract(data,'$.at') DESC,rowid DESC LIMIT 1",
                (item.get("plan_id"),),
            ).fetchone()
            if last and last[0] == item["id"]:
                return "保留该计划的最后一次执行，供时间预览使用"
        return ""

    def page(
        self, kind="jobs", *, offset=0, status="", character="", query="", busy=()
    ):
        """Return twenty bounded summaries with stable versions for batch actions."""
        if type(offset) is not int or offset < 0:
            raise AlbumError("页码不正确")
        status, character, query = (
            text_value(v, 100) for v in (status, character, query)
        )
        with self.store.lock:
            rows = self.rows(kind)
            counts = {}
            for row in rows:
                counts[row["state"]] = counts.get(row["state"], 0) + 1
            filtered = [
                r
                for r in rows
                if (not status or r["state"] == status)
                and (not character or r.get("character_id") == character)
                and (
                    not query
                    or query.casefold()
                    in " ".join(
                        str(r.get(k, ""))
                        for k in ("id", "scene", "name", "error", "character_name")
                    ).casefold()
                )
            ]
            total = len(filtered)
            offset = min(offset, max(0, (total - 1) // 20 * 20))
            items = []
            for row in filtered[offset : offset + 20]:
                reason = self.protection(kind, row, busy)
                item = {
                    k: v
                    for k, v in row.items()
                    if k
                    not in (
                        "prompt",
                        "provider",
                        "providers",
                        "failure_style",
                        "_version",
                    )
                }
                item.update(
                    token=self.token(row), cleanable=not reason, protection=reason
                )
                if kind == "jobs" and item.get("asset_id"):
                    try:
                        item["asset_expired"] = self.store.get(
                            "asset", item["asset_id"]
                        ).get("expired", False)
                    except AlbumError:
                        item["asset_expired"] = True
                items.append(item)
            signature = hashlib.sha256(
                "".join(self.token(r) for r in rows).encode()
            ).hexdigest()
            return {
                "items": items,
                "total": total,
                "offset": offset,
                "limit": 20,
                "counts": counts,
                "retained": len(rows),
                "signature": signature,
                "policy": {
                    k: self.store.settings[k]
                    for k in ("records_retention_days", "records_max_count")
                },
            }

    def compact(self, kind, patches, *, busy=()):
        """Atomically remove selected details, preserving idempotency and photo origin.

        Args:
            kind: jobs, deliveries, or runs.
            patches: One to two hundred IDs with the versions shown to the user.
            busy: Job IDs with an active local worker, including completion notices.

        Returns:
            Number of records compacted. No images or remote data are deleted.
        """
        if not isinstance(patches, list) or not 1 <= len(patches) <= 200:
            raise AlbumError("每次请选择1至200条记录")
        with self.store.lock, self.store.db:
            rows = {r["id"]: r for r in self.rows(kind)}
            selected, ids = [], set()
            for patch in patches:
                if (
                    not isinstance(patch, dict)
                    or not isinstance(patch.get("id"), str)
                    or patch.get("id") in ids
                ):
                    raise AlbumError("记录选择重复或格式错误")
                row = rows.get(patch.get("id"))
                if not row or patch.get("token") != self.token(row):
                    raise AlbumError(
                        "记录状态已变化，请刷新后重新选择；本次未清理任何记录"
                    )
                reason = self.protection(kind, row, busy)
                if reason:
                    raise AlbumError(reason + "；本次未清理任何记录")
                ids.add(row["id"])
                selected.append(row)
            for row in selected:
                if kind == "jobs":
                    provider = row.get("provider", {})
                    origin = {
                        "job_id": row["id"],
                        "prompt": row.get("prompt", row.get("scene", "")),
                        "model": provider.get("model", ""),
                        "site": provider.get("name", ""),
                    }
                    assets = self.store.db.execute(
                        "SELECT id,data,revision FROM records WHERE kind='asset' AND (json_extract(data,'$.provenance.job_id')=? OR id=?)",
                        (row["id"], row.get("asset_id", "")),
                    ).fetchall()
                    for asset in assets:
                        data = json.loads(asset["data"])
                        if not data.get("expired"):
                            source = data.setdefault("provenance", {})
                            source.update(generation=origin, job_id=row["id"])
                            source.setdefault(
                                "reference_ids", row.get("reference_ids", [])
                            )
                            source.setdefault(
                                "reference_version", row.get("reference_version", "")
                            )
                            self.store.db.execute(
                                "UPDATE records SET data=?,revision=? WHERE kind='asset' AND id=?",
                                (
                                    json.dumps(data, ensure_ascii=False),
                                    asset["revision"] + 1,
                                    asset["id"],
                                ),
                            )
                    keep = (
                        "scope",
                        "character_id",
                        "asset_id",
                        "delivery",
                        "qzone_delivery",
                        "qzone_tid",
                        "created",
                    )
                    data = {k: row[k] for k in keep if k in row}
                    data.update(archived=True)
                    self.store.db.execute(
                        "UPDATE jobs SET data=? WHERE id=?",
                        (json.dumps(data), row["id"]),
                    )
                elif kind == "deliveries":
                    data = {
                        k: row[k]
                        for k in (
                            "job_id",
                            "asset_id",
                            "asset_ids",
                            "target",
                            "message_id",
                            "tid",
                            "created",
                        )
                        if k in row
                    }
                    data.update(archived=True)
                    self.store.db.execute(
                        "UPDATE deliveries SET data=? WHERE id=?",
                        (json.dumps(data), row["id"]),
                    )
                else:
                    data = {
                        k: row[k]
                        for k in ("plan_id", "state", "at", "timezone")
                        if k in row
                    }
                    data.update(archived=True)
                    self.store.db.execute(
                        "UPDATE records SET data=?,revision=revision+1 WHERE kind='life_run' AND id=?",
                        (json.dumps(data), row["id"]),
                    )
            return {"cleaned": len(selected)}

    def cleanup(self, *, busy=(), preview=False):
        """Apply age/count limits only to settled records; preview is read-only."""
        result = {}
        with self.store.lock:
            for kind in ("jobs", "deliveries", "runs"):
                rows = self.rows(kind)
                ordered = sorted(
                    rows,
                    key=lambda r: (
                        r.get("_version", 0)
                        if kind == "jobs"
                        else r.get("at", r.get("created", 0))
                    ),
                    reverse=True,
                )
                cutoff = now() - self.store.settings["records_retention_days"] * 86400
                count, chosen = len(ordered), []
                protected = sum(bool(self.protection(kind, r, busy)) for r in rows)
                for row in reversed(ordered):
                    timestamp = (
                        row.get("_version", 0)
                        if kind == "jobs"
                        else row.get("at", row.get("created", 0))
                    )
                    if not self.protection(kind, row, busy) and (
                        timestamp < cutoff
                        or count > self.store.settings["records_max_count"]
                    ):
                        chosen.append({"id": row["id"], "token": self.token(row)})
                        count -= 1
                result[kind] = {
                    "eligible": len(chosen),
                    "protected": protected,
                    "retained": len(rows),
                }
                if chosen and not preview:
                    result[kind]["cleaned"] = self.compact(
                        kind, chosen[:200], busy=busy
                    )["cleaned"]
            return result
