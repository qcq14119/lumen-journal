"""Read-only, bounded data closure for the stage-B portable backup format.

No business store is constructed here. JSON columns are kept byte-for-byte unless
they are configuration columns whose structured credentials were excluded.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit
import uuid

from .backup_format import (
    BackupError, CHUNK_BYTES, MAX_EXPANDED_BYTES, MAX_FILE_BYTES, MAX_FILES,
    MAX_RECORD_BYTES, _check_source, check_cancel, ensure_space, parse_json,
)

MODULES = ("reference", "life", "memory", "settings")
MEMORY_TABLES_V1 = (
    "mem_chats", "mem_turns", "mem_turn_aliases", "mem_images",
    "mem_turn_images", "mem_batches", "mem_snapshots",
)
MEMORY_TABLES = (*MEMORY_TABLES_V1, "mem_item_guards", "mem_maintenance_runs")
MAPPING_CONTRACT = {
    "version": 1,
    "identity": ["records.(kind,id)", "mem_chats.(scope,cid)",
                 "mem_turns.(id,scope,cid,key,ordinal)", "mem_batches.(id,scope,cid)",
                 "mem_snapshots.(id,scope,cid)", "mem_images.(scope,cid,ref)"],
    "references": ["mem_turns.batch", "mem_turn_aliases.turn_id", "mem_turn_images.turn_id/ref",
                   "mem_snapshots.turns.turns[][0,2]", "mem_snapshots.turns.batches[][0]",
                   "memory.{events,people,profiles,plans}[].batch/closed_batch",
                   "memory.events[].turns (ordinal range)",
                   "memory.people[].relationships[].batch/history[].batch",
                   "mem_batches.diff.items.<collection>[][1,2] (before/after item)",
                   "mem_snapshots.memory (same memory schema)",
                   "character.versions[].asset_ids/active_version", "asset.character_id",
                   "life_plan.character_id/scope/origin_cid",
                   "life_adjustment.plan_id/plan_revision/character_id/scope/cid/anchor_ts/source_ids/evidence_revision",
                   "life_evidence_metadata.(id,revision,data.scope/cid/epoch/at/generation) (source evidence only;never resume scanner)",
                   "settings.providers[].id/api_keys[].id/active_key_id/provider_order/preferred_provider",
                   "mem_chats.settings.model_bindings.*.preset_id",
                   "settings.plan_recovery.assistant_binding.preset_id"],
    "policy": "preserve_source_values;stage_C_maps_only_known_fields;never_recursive_name_replacement",
}
MAPPING_CONTRACT_V1 = copy.deepcopy(MAPPING_CONTRACT)
MAPPING_CONTRACT["version"] = 2
MAPPING_CONTRACT["identity"] += ["mem_chats.maintenance_epoch", "mem_item_guards.(scope,cid,epoch,collection,item_id)",
    "mem_maintenance_runs.(id,task_id,scope,cid,epoch)"]
MAPPING_CONTRACT["references"] += [
    "settings.memory_maintenance.binding.preset_id", "mem_batches.maintenance_claim",
    "mem_item_guards.data.sources/dependencies/last_writer", "mem_item_guards.content_hash (verified structural item hash)",
    "mem_maintenance_runs.task_id", "mem_maintenance_runs.payload.batches/dependencies",
    "mem_maintenance_runs.payload.expected.epoch/source_upper/items[].sources/dependencies/last_writer/hash/value",
    "mem_maintenance_runs.payload.changes[].before/after (same item schema)/hash/previous_writer/sources",
    "mem_maintenance_runs.payload.evidence[].turn_id/batch (content hashes preserved)",
    "mem_maintenance_runs.payload.undo_expected[].hash (versions preserved; verified structural item hash)",
]
SECRET_NAMES = frozenset({
    "api_key", "apikey", "api-key", "key", "token", "access_token", "refresh_token",
    "password", "passwd", "secret", "client_secret", "authorization", "proxy_authorization",
    "cookie", "cookies", "bearer", "credential", "credentials",
})
URL_NAMES = frozenset({"url", "base_url", "api_url", "api_base", "endpoint", "endpoint_url", "proxy_url"})
AUTH_QUERY_NAMES = SECRET_NAMES | {"auth", "auth_token", "signature", "sig", "x_api_key", "x_api_token"}


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _load(value):
    return parse_json(value.encode("utf-8") if isinstance(value, str) else value)


def _credentials(value, parent=""):
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in SECRET_NAMES or parent == "api_keys" and key == "value":
                if isinstance(item, str) and item:
                    yield item
            elif isinstance(item, (dict, list)):
                yield from _credentials(item, key)
    elif isinstance(value, list):
        for item in value:
            yield from _credentials(item, parent)


def _address(value, secrets):
    if not isinstance(value, str) or not value:
        return value
    try:
        parsed = urlsplit(value)
        if not parsed.scheme or not parsed.netloc:
            return ""  # Non-address auth/config input must be supplied again.
        host = parsed.hostname or ""
        host = f"[{host}]" if ":" in host else host
        port = parsed.port
        netloc = host + (f":{port}" if port is not None else "")
        # Scheme/authority are case-insensitive. Never invent a substitute host
        # when a known credential occurs there; the address needs re-entry.
        authority = unquote(parsed.scheme + "://" + netloc).casefold()
        if any(secret.casefold() in authority for secret in secrets if secret):
            return ""
        parts = parsed.path.split("/")
        previous = ""
        for index, part in enumerate(parts):
            plain = unquote(part)
            marker = plain.lower().replace("-", "_")
            if previous in AUTH_QUERY_NAMES or plain in secrets or any(s and s in plain for s in secrets):
                parts[index] = ""
            previous = marker
        query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                 if k.lower().replace("-", "_") not in AUTH_QUERY_NAMES
                 and not any(s and (s in k or s in v) for s in secrets)]
        address = urlunsplit((parsed.scheme, netloc, "/".join(parts), urlencode(query), ""))
        if any(secret in unquote(address) for secret in secrets if secret):
            return ""
        return address
    except ValueError:
        return ""


def strip_credentials(value, *, include_keys=False):
    """Only configuration trees call this; arbitrary memory prose never does."""
    if include_keys:
        return copy.deepcopy(value), 0
    secrets = tuple(_credentials(value))
    changed = 0

    def visit(item, parent=""):
        nonlocal changed
        if isinstance(item, list):
            return [visit(v, parent) for v in item]
        if not isinstance(item, dict):
            return item
        result = {}
        for key, content in item.items():
            name = key.lower()
            if name in SECRET_NAMES or parent == "api_keys" and key == "value":
                changed += bool(content)
                result[key] = ""
            elif name in URL_NAMES and isinstance(content, str):
                result[key] = _address(content, secrets)
                changed += result[key] != content
            elif name in ("headers", "auth", "authentication") and isinstance(content, dict):
                # Preserve field names, not arbitrary values in auth containers.
                result[key] = {k: "" for k in content}
                changed += sum(bool(v) for v in content.values())
            else:
                result[key] = visit(content, key)
        return result

    return visit(value), changed


class _Writer:
    def __init__(self, directory, cancel):
        self.directory, self.cancel = directory, cancel
        self.inventory, self.total = {}, 0
        (directory / "data").mkdir(parents=True, exist_ok=True)

    def rows(self, name, rows):
        digest, size, count = hashlib.sha256(), 0, 0
        with (self.directory / "data" / (name + ".jsonl")).open("xb") as output:
            for row in rows:
                check_cancel(self.cancel)
                raw = (_json(dict(row)) + "\n").encode("utf-8")
                if len(raw) > MAX_RECORD_BYTES:
                    raise BackupError("DATA_RECORD_LIMIT")
                size += len(raw)
                self.total += len(raw)
                if size > MAX_FILE_BYTES or self.total > MAX_EXPANDED_BYTES:
                    raise BackupError("DATA_SIZE_LIMIT")
                ensure_space(self.directory, len(raw))
                output.write(raw)
                digest.update(raw)
                count += 1
        if hasattr(rows, "source_count") and count != rows.source_count:
            raise BackupError("EXPORT_INCOMPLETE")
        self.inventory[name] = {"rows": count, "bytes": size, "sha256": digest.hexdigest()}


def _table(db, name):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


@contextmanager
def _locked(lock, cancel):
    while not lock.acquire(timeout=0.05):
        check_cancel(cancel)
    try:
        check_cancel(cancel)
        yield
    finally:
        lock.release()


class _SourceRows:
    def __init__(self, db, table, where, args, columns, order):
        # All identifiers/clauses are module-owned constants. Measure independently
        # of the row iterator so an accidental skipped row is not called old loss.
        fields = [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
        sizes = "+".join(f"coalesce(length(CAST(\"{field}\" AS BLOB)),0)" for field in fields)
        clause = f" WHERE {where}" if where else ""
        bad = f"({where}) AND " if where else ""
        if db.execute(f"SELECT 1 FROM {table} WHERE {bad}({sizes})>? LIMIT 1", (*args, MAX_RECORD_BYTES)).fetchone():
            raise BackupError("DATA_RECORD_LIMIT")
        self.source_count = db.execute(f"SELECT count(*) FROM {table}{clause}", args).fetchone()[0]
        self.db, self.args = db, args
        self.query = f"SELECT {columns} FROM {table}{clause} ORDER BY {order}"

    def __iter__(self):
        return iter(self.db.execute(self.query, self.args))


def _bounded_rows(db, table, *, where="", args=(), columns="*", order="rowid"):
    return _SourceRows(db, table, where, args, columns, order)


class _MappedRows:
    def __init__(self, rows, transform):
        self.rows, self.transform = rows, transform
        self.source_count = rows.source_count

    def __iter__(self):
        return (self.transform(row) for row in self.rows)


def _reference_path(root, asset):
    digest = asset.get("hash", "")
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise BackupError("REFERENCE_HASH_INVALID")
    relative = asset.get("original", "")
    if relative != f"originals/{digest}.png":
        raise BackupError("REFERENCE_PATH_INVALID")
    path = root / relative
    def linked(candidate):
        return candidate.is_symlink() or bool(getattr(candidate.lstat(), "st_file_attributes", 0) & 0x400)
    if not path.is_file() or linked(path) or linked(path.parent):
        raise BackupError("REFERENCE_FILE_MISSING")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise BackupError("DATA_SIZE_LIMIT")
    return path


def _freeze_reference(db, root, directory, cancel):
    (directory / "media").mkdir(exist_ok=True)
    where = "kind='asset' AND json_extract(data,'$.kind')='reference' AND json_extract(data,'$.expired') IS NOT 1 AND json_extract(data,'$.trash') IS NOT 1 AND json_extract(data,'$.archived') IS NOT 1"
    count, size = 0, 0
    for row in _bounded_rows(db, "records", where=where):
        check_cancel(cancel)
        asset = _load(row["data"])
        source = _reference_path(root, asset)
        destination = directory / "media" / (asset["hash"] + ".png")
        if destination.exists():
            continue
        count += 1
        size += source.stat().st_size
        if count > MAX_FILES - 32 or size > MAX_EXPANDED_BYTES:
            raise BackupError("DATA_SIZE_LIMIT")
        try:
            os.link(source, destination)
        except OSError:
            ensure_space(directory, source.stat().st_size)
            with source.open("rb") as inp, destination.open("xb") as out:
                while chunk := inp.read(CHUNK_BYTES):
                    check_cancel(cancel)
                    out.write(chunk)
    return where


def capture(store, directory: Path, modules, include_keys, include_future, cancel):
    """Freeze a selected source snapshot; never call mutable service entrypoints."""
    if not modules or len(set(modules)) != len(modules) or any(x not in MODULES for x in modules):
        raise BackupError("MODULE_SELECTION_INVALID")
    if type(include_keys) is not bool or type(include_future) is not bool:
        raise BackupError("MODULE_SELECTION_INVALID")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    writer = _Writer(directory, cancel)
    stamp = int(time.time())
    connection = None
    metadata = {
        "modules": [m for m in MODULES if m in modules], "dependencies": [], "counts": {},
        "source": {"namespace": uuid.uuid4().hex, "captured_at": stamp},
        "omissions": ["HOST_CHAT_AND_IMAGES", "GENERATED_ALBUM_IMAGES", "DERIVED_MEDIA",
                      "EXECUTION_DELIVERY_RECOVERY_QUEUES", "RAW_MODEL_RESPONSE_CACHE", "VECTOR_CACHE"],
        "limitations": [], "credentials_included": include_keys, "mapping_contract": MAPPING_CONTRACT,
        "inventory": writer.inventory,
    }
    removed = 0

    def config_rows(rows, field="data"):
        def config(row):
            nonlocal removed
            result = dict(row)
            value, count = strip_credentials(_load(result[field]), include_keys=include_keys)
            removed += count
            # Keep the exact source JSON spelling when it did not need changing.
            if count:
                result[field] = _json(value)
            return result
        return _MappedRows(rows, config) if hasattr(rows, "source_count") else map(config, rows)

    try:
        with _locked(store.lock, cancel):
            check_cancel(cancel)
            root = Path(store.root).resolve()
            connection = sqlite3.connect((root / "album.sqlite3").as_uri() + "?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("BEGIN")
            connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
            settings = copy.deepcopy(store.settings) if "settings" in modules else None
            metadata["source"]["settings_revision"] = int(store.settings.get("settings_revision", 0))
            reference_where = _freeze_reference(connection, root, directory, cancel) if "reference" in modules else ""
        db = connection
        if "settings" in modules:
            value, count = strip_credentials(settings, include_keys=include_keys)
            removed += count
            writer.rows("settings", [{"data": value}])
        if "reference" in modules:
            writer.rows("characters", _bounded_rows(db, "records", where="kind='character'"))
            writer.rows("reference_assets", _bounded_rows(db, "records", where=reference_where))
        if "life" in modules:
            plan_where = "kind='life_plan' AND (json_extract(data,'$.run_once') IS NOT 1"
            plan_args = ()
            if include_future:
                plan_where += " OR json_extract(data,'$.trigger_at')>=? OR id IN (SELECT json_extract(data,'$.plan_id') FROM records WHERE kind='life_adjustment' AND (json_extract(data,'$.anchor_ts')>=? OR json_extract(data,'$.effective_ts')>=?))"
                plan_args = (stamp, stamp, stamp)
            plan_where += ")"
            writer.rows("life_plans", _bounded_rows(db, "records", where=plan_where, args=plan_args))
            adjustment_where = "kind='life_adjustment' AND (json_extract(data,'$.anchor_ts')>=? OR json_extract(data,'$.effective_ts')>=?)"
            writer.rows("life_adjustments", _bounded_rows(db, "records", where=adjustment_where, args=(stamp, stamp)) if include_future else ())
            if not include_future:
                metadata["omissions"].append("OPTIONAL_FUTURE_AGREEMENTS_AND_ADJUSTMENTS")
            # Evidence is selected by the retained adjustments, never the entire
            # recent-chat evidence cache or active execution queue.
            source_where = ("kind IN ('daily_source','restore_source') AND id IN (SELECT j.value FROM records a, "
                            "json_each(a.data,'$.source_ids') j WHERE a.kind='life_adjustment' "
                            "AND (json_extract(a.data,'$.anchor_ts')>=? OR json_extract(a.data,'$.effective_ts')>=?))")
            # C keeps imported evidence outside the live daily scanner. Its
            # portable v1 representation remains a daily_source, selected only
            # by retained adjustments, never by an unbounded archive sweep.
            source_rows = _bounded_rows(db, "records", where=source_where, args=(stamp, stamp)) if include_future else ()
            writer.rows("life_sources", _MappedRows(source_rows, lambda row: {**dict(row), "kind": "daily_source"}) if include_future else ())
            cursor_where = (
                "kind IN ('daily_cursor','restore_evidence') AND EXISTS (SELECT 1 FROM records a WHERE a.kind='life_adjustment' "
                "AND json_extract(a.data,'$.scope')=json_extract(records.data,'$.scope') "
                "AND json_extract(a.data,'$.cid')=json_extract(records.data,'$.cid') "
                "AND (json_extract(a.data,'$.anchor_ts')>=? OR json_extract(a.data,'$.effective_ts')>=?))"
            )

            def evidence_metadata(row):
                value = _load(row["data"])
                revision = value.get("source_revision", row["revision"]) if row["kind"] == "restore_evidence" else row["revision"]
                return {"id": row["id"], "revision": revision, "data": {
                    k: value[k] for k in ("scope", "cid", "epoch", "at", "updated", "character_id", "generation") if k in value
                }}

            cursor_rows = _bounded_rows(db, "records", where=cursor_where, args=(stamp, stamp)) if include_future else ()
            writer.rows("life_evidence_metadata", _MappedRows(cursor_rows, evidence_metadata) if include_future else ())
            if "reference" not in modules:
                def character_dependency(row):
                    value = _load(row["data"])
                    return {"id": row["id"], "revision": row["revision"],
                            "data": {k: value[k] for k in ("name", "sessions") if k in value}}
                source_rows = _bounded_rows(db, "records", where="kind='character' AND id IN (SELECT json_extract(data,'$.character_id') FROM records WHERE " + plan_where + ")", args=plan_args)
                writer.rows("life_characters", _MappedRows(source_rows, character_dependency))
                metadata["dependencies"].append("CHARACTER_SESSION_BINDINGS")
        if "life" in modules:
            writer.rows("calendar", config_rows(_bounded_rows(db, "records", where="kind='calendar'")))
        elif "settings" in modules:
            def calendar_connection(row):
                value = _load(row["data"])
                return {**dict(row), "data": _json({k: value[k] for k in ("search_schemes", "active_search_scheme") if k in value})}
            source_rows = _bounded_rows(db, "records", where="kind='calendar'")
            writer.rows("calendar_connections", config_rows(_MappedRows(source_rows, calendar_connection)))
        if "memory" in modules:
            for table in MEMORY_TABLES:
                if not _table(db, table):
                    writer.rows(table, ())
                    metadata["omissions"].append("SOURCE_TABLE_ABSENT:" + table)
                    continue
                rows = _bounded_rows(db, table)
                if table == "mem_chats":
                    rows = _MappedRows(rows, lambda row: {"maintenance_epoch": "", **dict(row)})
                elif table == "mem_batches":
                    rows = _MappedRows(rows, lambda row: {"maintenance_claim": "", **dict(row)})
                writer.rows(table, config_rows(rows, "settings") if table == "mem_chats" else rows)
        if "memory" in modules or "settings" in modules:
            rows = _bounded_rows(db, "memory_model_settings") if _table(db, "memory_model_settings") else ()
            writer.rows("memory_model_settings", config_rows(rows))
            if "settings" not in modules:
                metadata["dependencies"].append("MEMORY_MODEL_CONNECTIONS")
        metadata["counts"] = {name: item["rows"] for name, item in writer.inventory.items()}
        metadata["credentials_omitted"] = removed
        verified = validate_data(directory, metadata, cancel, source=db)
        metadata["source_integrity"] = verified["source_integrity"]
        metadata["limitations"] = verified["limitations"]
        return metadata
    finally:
        if connection is not None:
            connection.close()


def estimate(store):
    """Metadata-only SQL estimate; this is not the later frozen snapshot."""
    result = {m: {"count": 0, "bytes": 0} for m in MODULES}

    db = sqlite3.connect((Path(store.root) / "album.sqlite3").resolve().as_uri() + "?mode=ro", uri=True)
    db.execute("PRAGMA query_only=ON")
    db.execute("BEGIN")

    def add(module, table, where="", args=()):
        if not _table(db, table):
            return
        fields = [row[1] for row in db.execute(f"PRAGMA table_info({table})")]
        sizes = "+".join(f"coalesce(length(CAST(\"{field}\" AS BLOB)),0)" for field in fields)
        clause = " WHERE " + where if where else ""
        row = db.execute(f"SELECT count(*),coalesce(sum({sizes}),0) FROM {table}{clause}", args).fetchone()
        result[module]["count"] += row[0]
        result[module]["bytes"] += row[1]

    try:
        add("reference", "records", "kind='character' OR (kind='asset' AND json_extract(data,'$.kind')='reference' AND json_extract(data,'$.expired') IS NOT 1 AND json_extract(data,'$.trash') IS NOT 1 AND json_extract(data,'$.archived') IS NOT 1)")
        size = db.execute("SELECT coalesce(sum(bytes),0) FROM (SELECT max(json_extract(data,'$.bytes')) AS bytes FROM records WHERE kind='asset' AND json_extract(data,'$.kind')='reference' AND json_extract(data,'$.expired') IS NOT 1 AND json_extract(data,'$.trash') IS NOT 1 AND json_extract(data,'$.archived') IS NOT 1 GROUP BY json_extract(data,'$.hash'))").fetchone()[0]
        result["reference"]["bytes"] += int(size)
        add("life", "records", "kind IN ('life_plan','life_adjustment','calendar')")
        for table in (*MEMORY_TABLES, "memory_model_settings"):
            add("memory", table)
        add("settings", "memory_model_settings")
        add("settings", "records", "kind='calendar'")
        result["settings"]["count"] += 1
        # The configuration file's byte length is sufficient here; do not load it.
        path = Path(store.root) / "settings.json"
        result["settings"]["bytes"] += path.stat().st_size if path.is_file() else 0
    finally:
        db.close()
    return {"modules": result, "dependencies": {"memory": ["MEMORY_MODEL_CONNECTIONS"], "life": ["CHARACTER_SESSION_BINDINGS"]}, "estimated": True}


def _file_rows(directory, name, cancel):
    path = directory / "data" / (name + ".jsonl")
    if path.is_symlink() or not path.is_file():
        raise BackupError("DATA_FILE_MISSING")
    with path.open("rb") as handle:
        while raw := handle.readline(MAX_RECORD_BYTES + 1):
            check_cancel(cancel)
            if len(raw) > MAX_RECORD_BYTES or not raw.endswith(b"\n"):
                raise BackupError("DATA_RECORD_LIMIT")
            row = _load(raw)
            if not isinstance(row, dict):
                raise BackupError("DATA_ROW_INVALID")
            yield row, raw


_RECORD_KINDS = {
    "characters": "character", "reference_assets": "asset", "life_plans": "life_plan",
    "life_adjustments": "life_adjustment", "life_sources": "daily_source",
    "calendar": "calendar", "calendar_connections": "calendar",
}
_MEMORY_COLUMNS = {
    "mem_chats": ("scope cid settings memory", "revision created updated", "generation next_ordinal", "enabled"),
    "mem_turns": ("scope cid key digest kind user assistant status", "at archived_at summarized_at", "id ordinal", "pruned"),
    "mem_turn_aliases": ("scope cid key", "", "turn_id", ""),
    "mem_images": ("scope cid ref description status", "attempts updated", "", ""),
    "mem_turn_images": ("side ref", "position", "turn_id", ""),
    "mem_batches": ("scope cid state diff stats model", "created undone_at", "id first last", ""),
    "mem_snapshots": ("id scope cid reason memory turns", "created", "", ""),
    "memory_model_settings": ("data", "revision", "id", ""),
    "mem_item_guards": ("scope cid epoch collection item_id content_hash data", "", "version", ""),
    "mem_maintenance_runs": ("id task_id scope cid epoch trigger state payload", "created updated committed_at undone_at", "", ""),
}


def _integer(value, minimum=0):
    return type(value) is int and minimum <= value <= 9223372036854775807


def _require(valid):
    if not valid:
        raise BackupError("DATA_SCHEMA_INVALID")


def _object(value):
    _require(isinstance(value, dict))
    return value


def _json_object(value):
    _require(isinstance(value, str))
    return _object(_load(value))


def _optional_fields(value, *, text=(), integer=(), boolean=(), lists=(), objects=()):
    """Known fields are typed; unknown historical extension fields stay intact."""
    for names, valid in (
        (text, lambda v: isinstance(v, str)), (integer, _integer),
        (boolean, lambda v: type(v) is bool),
        (lists, lambda v: isinstance(v, list)), (objects, lambda v: isinstance(v, dict)),
    ):
        for name in names:
            if name in value:
                _require(valid(value[name]))


_SETTING_BOUNDS = {
    "task_timeout": (60, 3600), "poll_interval": (3, 60),
    "generated_retention_days": (1, 365), "generated_max_count": (1, 1000),
    "records_retention_days": (1, 3650), "records_max_count": (20, 10000),
    "max_retries": (0, 10), "retry_delay": (1, 300),
}
_CHAT_BOUNDS = {
    "summary_trigger_turns": (5, 300), "keep_turns": (0, 100), "memory_tokens": (500, 100000),
    "retention_days": (0, 3650), "summary_max_retries": (0, 10), "summary_timeout": (30, 1200),
    "recall_candidates": (5, 50), "recall_max": (1, 10), "image_keep_turns": (0, 50),
}
_CONFIG_SWITCHES = {
    "settings": ("qzone_enabled", "generated_qzone_allowed", "parallel_backup", "retry_unknown", "primary_enabled"),
    "chat": ("auto_process", "inject_enabled", "vector_enabled", "query_rewrite", "image_describe", "enabled", "maintenance_paused"),
    "provider": ("enabled",), "model": ("custom", "clear_key"), "search": ("is_builtin",),
}


def _bounded_fields(value, bounds):
    for field, (low, high) in bounds.items():
        if field in value:
            _require(type(value[field]) is int and low <= value[field] <= high)


def _config_schema(value, *, context="settings"):
    """Finite writer field contracts; no rewriting or cross-context coercion."""
    _object(value)
    _optional_fields(value, text=("api_key", "base_url", "api_url", "active_key_id", "preferred_provider"),
                     integer=("settings_revision",), lists=("providers", "api_keys", "presets", "search_schemes", "provider_order"),
                     objects=("shared", "model_bindings"), boolean=_CONFIG_SWITCHES.get(context, ()))
    if context == "settings":
        _bounded_fields(value, _SETTING_BOUNDS)
        if "plan_recovery" in value:
            from .store import AlbumError, plan_recovery_settings
            _object(value["plan_recovery"])
            try:
                plan_recovery_settings(value["plan_recovery"])
            except AlbumError:
                raise BackupError("DATA_SCHEMA_INVALID") from None
        if "memory_maintenance" in value:
            from .store import AlbumError, memory_maintenance_settings
            _object(value["memory_maintenance"])
            try:
                memory_maintenance_settings(value["memory_maintenance"])
            except AlbumError:
                raise BackupError("DATA_SCHEMA_INVALID") from None
        _optional_fields(value, text=("primary_name",))
        if "qzone_visibility" in value:
            # The existing writer accepts numerically equal enum values (also
            # True / 1.0). Preserve those saved values without normalization.
            _require(value["qzone_visibility"] in (1, 4, 64))
    if context in ("settings", "provider"):
        _optional_fields(value, text=("name", "model", "size", "reference_field"))
        if "reference_field" in value:
            _require(value["reference_field"] in ("image", "image[]"))
    if context == "chat":
        _bounded_fields(value, _CHAT_BOUNDS)
        _optional_fields(value, text=("summary_prompt", "user_name"))
        for field in ("recall_rerank_min", "recall_vector_min"):
            if field in value:
                _require(type(value[field]) in (int, float) and 0 <= value[field] <= 1)
    if context == "model":
        _optional_fields(value, text=("model", "preset_id", "mode", "type"))
        _bounded_fields(value, {"dimensions": (1, 4096), "max_context_tokens": (1024, 2000000)})
        if "type" in value:
            _require(value["type"] in ("chat", "embedding", "rerank"))
    if context == "calendar":
        _optional_fields(value, text=("active_search_scheme",))
    for field, child_context in (("providers", "provider"), ("api_keys", "key"),
                                 ("presets", "model"), ("search_schemes", "search")):
        seen = set()
        for item in value.get(field, []):
            _object(item)
            _optional_fields(item, text=("id", "name", "value", "api_key", "base_url", "api_url"))
            if "id" in item:
                _require(bool(item["id"]) and (field == "search_schemes" or item["id"] not in seen))
                seen.add(item["id"])
            _config_schema(item, context=child_context)
    _require(all(isinstance(v, str) for v in value.get("provider_order", [])))
    for field in ("shared", "summary", "assistant", "rewrite", "embedding", "rerank"):
        if field in value:
            _config_schema(value[field], context="model")
            if field in ("summary", "assistant") and "mode" in value[field]:
                _require(value[field]["mode"] in ("custom", "current" if field == "summary" else "summary"))
    for binding in value.get("model_bindings", {}).values():
        _object(binding)
        _optional_fields(binding, text=("mode", "preset_id"))


def _calendar_schema(value):
    _object(value)
    _config_schema(value, context="calendar")
    _optional_fields(value, lists=("trips", "custom_days", "holiday_ranges", "workday_ranges"),
                     objects=("personal_calendars", "school_terms"))
    for field in ("holidays", "workdays"):
        if field in value:
            days = value[field]
            _require(isinstance(days, (dict, list)))
            if isinstance(days, dict):
                _require(all(isinstance(label, str) for label in days.values()))
    for field in ("holiday_ranges", "workday_ranges"):
        _require(all(isinstance(part, str) for part in value.get(field, [])))
    for item in value.get("custom_days", []):
        _object(item)
        _optional_fields(item, text=("date", "name", "note"), boolean=("is_holiday",))
    terms = value.get("school_terms", {})
    _optional_fields(terms, text=("winter_start", "winter_end", "summer_start", "summer_end"))
    if "exam_ranges" in terms:
        ranges = terms["exam_ranges"]
        _require(isinstance(ranges, (str, list)))
        if isinstance(ranges, list):
            for part in ranges:
                if not isinstance(part, str):
                    _object(part)
                    _optional_fields(part, text=("start", "end"))
    for item in value.get("trips", []):
        _object(item)
        _optional_fields(item, text=("id", "scope", "title", "start_date", "end_date", "wake_time", "mode", "note"))
        _bounded_fields(item, {"wake_window": (0, 60)})
        if "mode" in item:
            _require(item["mode"] in ("trip", "holiday", "school"))
    for entry in value.get("personal_calendars", {}).values():
        _calendar_schema(entry)


def _memory_item_schema(item, collection):
    _object(item)
    _optional_fields(item, text=("id", "title", "summary", "time", "name", "description", "text", "until",
                                "person", "kind", "status", "reason", "role"),
                     integer=("at",), lists=("participants", "causes", "aliases", "events"))
    for field in ("participants", "causes", "aliases", "events"):
        _require(all(isinstance(value, str) for value in item.get(field, [])))
    for field in ("batch", "closed_batch"):
        if item.get(field) is not None:
            _require(_integer(item[field], 1))
    if "id" in item:
        _require(isinstance(item["id"], str))
    if item.get("turns") is not None:
        interval = item["turns"]
        _require(isinstance(interval, list) and len(interval) == 2
                 and all(_integer(v, 1) for v in interval) and interval[0] <= interval[1])
    if collection == "people":
        _optional_fields(item, lists=("relationships",))
        for relation in item.get("relationships", []):
            _object(relation)
            _optional_fields(relation, lists=("history",))
            for part in (relation, *relation.get("history", [])):
                _object(part)
                _optional_fields(part, text=("to", "text"))
                if part.get("batch") is not None:
                    _require(_integer(part["batch"], 1))


def _keywords_schema(value):
    _require(isinstance(value, list))
    for word in value:
        # Older JSON imports can preserve plain keyword strings. Current merge
        # writes {text, weight}; validation preserves both without normalizing.
        if not isinstance(word, str):
            _object(word)
            _require(isinstance(word.get("text"), str))
            _optional_fields(word, text=("weight",))


def _memory_schema(value):
    _object(value)
    _optional_fields(value, text=("overview",), lists=("keywords", "events", "people", "profiles", "plans"), objects=("seq",))
    _keywords_schema(value.get("keywords", []))
    for collection in ("events", "people", "profiles", "plans"):
        for item in value.get(collection, []):
            _memory_item_schema(item, collection)
    _require(all(_integer(number) for number in value.get("seq", {}).values()))


def _diff_schema(value, state):
    # apply_batch persists this placeholder before merge. All completed states
    # must retain the inputs that core.undo reads without defaults.
    if state == "applying" and value == {}:
        return
    _require(isinstance(value.get("seq"), dict) and isinstance(value.get("items"), dict))
    _require(all(_integer(number) for number in value["seq"].values()))
    for field in ("overview", "keywords"):
        if field not in value:
            continue
        pair = value[field]
        _require(isinstance(pair, list) and len(pair) == 2)
        for side in pair:
            if field == "overview":
                _require(isinstance(side, str))
            else:
                _keywords_schema(side)
    for collection, changes in value["items"].items():
        _require(collection in ("events", "people", "profiles", "plans") and isinstance(changes, list))
        for change in changes:
            _require(isinstance(change, list) and len(change) == 3 and isinstance(change[0], str))
            for item in change[1:]:
                if item is not None:
                    _memory_item_schema(item, collection)
                    _require(item.get("id") == change[0])


def _snapshot_schema(value):
    _require(isinstance(value.get("turns"), list) and isinstance(value.get("batches"), list))
    for turn in value["turns"]:
        _require(isinstance(turn, list) and len(turn) == 3 and _integer(turn[0], 1)
                 and turn[1] in ("pending", "summarized")
                 and (turn[2] is None or _integer(turn[2], 1)))
    for batch in value["batches"]:
        _require(isinstance(batch, list) and len(batch) == 2 and _integer(batch[0], 1)
                 and batch[1] in ("applying", "applied", "undone", "superseded"))


def _record_data_schema(name, value):
    _object(value)
    if name in ("calendar", "calendar_connections"):
        _calendar_schema(value)
    elif name in ("characters", "life_characters"):
        _optional_fields(value, text=("name", "appearance", "style", "failure_style"), lists=("sessions", "versions"))
        _require(all(isinstance(scope, str) for scope in value.get("sessions", [])))
        _require(value.get("active_version") is None or isinstance(value["active_version"], str))
        seen = set()
        for version in value.get("versions", []):
            _object(version)
            _require(isinstance(version.get("id"), str) and bool(version["id"]) and version["id"] not in seen)
            seen.add(version["id"])
            _require(isinstance(version.get("asset_ids"), list) and all(isinstance(v, str) for v in version["asset_ids"]))
            _optional_fields(version, integer=("created",))
    elif name == "reference_assets":
        _require(value.get("kind") == "reference")
        _require(all(isinstance(value.get(k), str) and value[k] for k in ("hash", "character_id", "original")))
        _optional_fields(value, text=("scope", "description", "delivery", "thumbnail"), integer=("created", "width", "height", "bytes"),
                         boolean=("trash", "expired", "archived"), lists=("tags",))
    elif name == "life_plans":
        _optional_fields(value, text=("name", "instruction", "scope", "character_id", "cron", "timezone", "source", "origin_cid", "override_date", "day_mode"),
                         integer=("window_before", "window_after", "trigger_at", "chain_depth", "activated_at"),
                         boolean=("enabled", "run_once", "override_day_wake"))
        _bounded_fields(value, {"window_before": (0, 120), "window_after": (0, 120), "chain_depth": (0, 2)})
        # Empty historical day_mode, like an absent field, uses the existing
        # consumer's cron default. It is not rewritten in the archive.
        if "day_mode" in value:
            _require(value["day_mode"] in ("", "cron", "school_day", "holiday_rest", "sleep_before_work", "night_owl_before_rest"))
        for field, choices in (("photo_mode", ("auto", "always", "never")), ("contact_mode", ("adaptive", "reminder"))):
            if field in value:
                _require(value[field] in choices)
    elif name == "life_adjustments":
        _optional_fields(value, text=("scope", "cid", "plan_id", "character_id", "reason", "replacement", "actor", "progress"),
                         integer=("plan_revision", "anchor_ts", "effective_ts", "auto_delays", "at"),
                         boolean=("explicit",), lists=("source_ids",))
        _require(value.get("evidence_revision") is None or _integer(value["evidence_revision"]))
        _require(all(isinstance(v, str) for v in value.get("source_ids", [])))
        if "decision" in value:
            _require(value["decision"] in ("keep", "continue", "skip", "delay", "replace", "restore", "cancel"))
    elif name in ("life_sources", "life_evidence_metadata"):
        _optional_fields(value, text=("scope", "cid", "character_id", "text", "fingerprint", "checkpoint"),
                         integer=("epoch", "at", "updated"))
        _require(value.get("generation") is None or _integer(value["generation"]))


def _maintenance_identity(value):
    _object(value)
    _require(value.get("collection") in ("events", "people", "profiles", "plans", "overview", "keywords"))
    _require(isinstance(value.get("id"), str) and bool(value["id"]))


def _maintenance_writer(value):
    _require(isinstance(value, dict) and set(value) == {"kind", "id"})
    _require(value["kind"] in ("summary", "manual", "maintenance", "undo", "protect", "legacy"))
    _require(_integer(value["id"], 1) if value["kind"] == "summary" else isinstance(value["id"], str))


def _maintenance_schema(name, row):
    columns = set(" ".join(_MEMORY_COLUMNS[name]).split())
    _require(set(row) == columns and bool(row["epoch"]))
    payload = _json_object(row["data" if name == "mem_item_guards" else "payload"])
    if name == "mem_item_guards":
        _maintenance_identity({"collection": row["collection"], "id": row["item_id"]})
        _require(bool(re.fullmatch(r"[a-f0-9]{64}", row["content_hash"])))
        _require(set(payload) == {"sources", "source_complete", "legacy_unverified", "protected", "last_writer", "dependencies"})
        for key in ("source_complete", "legacy_unverified", "protected"):
            _require(type(payload[key]) is bool)
        _maintenance_writer(payload["last_writer"])
    else:
        _require(bool(row["id"]) and bool(row["task_id"]) and row["trigger"] in ("auto", "manual", "cleanup"))
        _require(row["state"] in ("queued", "reading", "requesting", "validating", "committing", "completed", "findings_only",
            "failed", "cancelled", "interrupted", "stale", "undone", "skipped", "expired", "partial", "unknown"))
        _require(not set(payload) - {"keys", "batches", "apply", "allow_protected", "counts", "findings", "changes",
            "dependencies", "expired", "resolved", "code", "parameters", "elapsed_ms", "expected", "evidence", "undo_expected", "affected"})
        _optional_fields(payload, boolean=("apply", "allow_protected", "expired", "resolved"), integer=("elapsed_ms",),
            text=("code",), objects=("counts", "parameters", "expected"), lists=("keys", "batches", "findings", "changes", "dependencies", "evidence", "undo_expected", "affected"))
        for key in ("keys", "affected", "undo_expected"):
            for item in payload.get(key, []):
                _maintenance_identity(item)
                if key == "undo_expected":
                    _require(_integer(item.get("version"), 1) and isinstance(item.get("hash"), str))
        expected = payload.get("expected", {})
        _require(not set(expected) - {"epoch", "revision", "items", "source_upper"})
        _optional_fields(expected, text=("epoch",), integer=("revision", "source_upper"), lists=("items",))
        for item in expected.get("items", []):
            _maintenance_identity(item)
        for change in payload.get("changes", []):
            _maintenance_identity(change)
            _require(change["collection"] in ("events", "people", "profiles", "plans"))
            _require(not set(change) - {"collection", "id", "version", "hash", "before", "after", "reason", "sources", "previous_writer", "after_version"})
            _require(_integer(change.get("version"), 1) and _integer(change.get("after_version"), 1))
            _require(isinstance(change.get("hash"), str) and isinstance(change.get("sources"), list))
            for part in ("before", "after"):
                _memory_item_schema(change.get(part), change["collection"])
                _require(change[part].get("id") == change["id"])
            _require(change["hash"] == hashlib.sha256(_json(change["before"]).encode()).hexdigest()
                     and change["after_version"] == change["version"] + 1)
            _maintenance_writer(change.get("previous_writer"))
        for evidence in payload.get("evidence", []):
            _object(evidence)
            _require(not set(evidence) - {"turn_id", "key", "kind", "digest", "ordinal", "batch", "pruned", "status", "batch_state", "raw_hash", "projection_hash", "image_signature", "complete"})
            _require(_integer(evidence.get("turn_id"), 1))
            _require(evidence.get("batch") is None or _integer(evidence["batch"], 1))
            _optional_fields(evidence, text=("key", "kind", "digest", "status", "batch_state", "raw_hash", "projection_hash", "image_signature"),
                integer=("ordinal",), boolean=("complete",))
    for field in ("sources", "dependencies", "batches"):
        if field in payload:
            _require(isinstance(payload[field], list) and all(_integer(v, 1) for v in payload[field]))


def _validate_row(name, row, schema_version=2):
    """Portable v1 SQL columns; do not normalize JSON or construct business stores."""
    if name in _MEMORY_COLUMNS:
        text, nonnegative, positive, bit = _MEMORY_COLUMNS[name]
        for fields, valid in ((text, lambda v: isinstance(v, str)),
                              (nonnegative, _integer), (positive, lambda v: _integer(v, 1)),
                              (bit, lambda v: type(v) is int and v in (0, 1))):
            _require(all(field in row and valid(row[field]) for field in fields.split()))
        for field in ("scope", "cid", "key", "ref"):
            if field in row and not (name == "mem_turn_images" and field == "ref"):
                _require(bool(row[field]))
        if name in ("mem_item_guards", "mem_maintenance_runs"):
            _require(schema_version >= 2)
            _maintenance_schema(name, row)
        elif name == "mem_chats":
            if schema_version >= 2:
                _require(isinstance(row.get("maintenance_epoch"), str))
            _config_schema(_json_object(row["settings"]), context="chat")
            _memory_schema(_json_object(row["memory"]))
        elif name == "mem_turns":
            _require("batch" in row and (row["batch"] is None or _integer(row["batch"], 1)))
            _require(row["kind"] in ("chat", "proactive", "business") and row["status"] in ("pending", "summarized"))
        elif name == "mem_images":
            _require(row["status"] in ("pending", "ready", "failed"))
        elif name == "mem_turn_images":
            _require(row["side"] in ("user", "assistant"))
            _require(all(field in row and _integer(row[field], -1) for field in ("start_pos", "end_pos")))
            # These two columns were added later; source-less old spans remain
            # unknown. No new provenance is inferred from offsets or strings.
            _optional_fields(row, text=("source", "body_hash"))
        elif name == "mem_batches":
            if schema_version >= 2:
                _require(isinstance(row.get("maintenance_claim"), str))
            _require(row["state"] in ("applying", "applied", "undone", "superseded") and row["first"] <= row["last"])
            _json_object(row["stats"])
            _diff_schema(_json_object(row["diff"]), row["state"])
        elif name == "mem_snapshots":
            _require(bool(row["id"]))
            _memory_schema(_json_object(row["memory"]))
            _snapshot_schema(_json_object(row["turns"]))
        elif name == "memory_model_settings":
            _require(row["id"] == 1)
            _config_schema(_json_object(row["data"]), context="models")
    elif name == "settings":
        _require("data" in row)
        _config_schema(row["data"])
    else:
        _require(isinstance(row.get("id"), str) and bool(row["id"]) and _integer(row.get("revision"), 1))
        if name in _RECORD_KINDS:
            _require(row.get("kind") == _RECORD_KINDS[name])
            value = _json_object(row.get("data"))
        else:
            value = _object(row.get("data"))
        if name == "life_evidence_metadata":
            _require(set(row) == {"id", "revision", "data"}
                     and not set(value) - {"scope", "cid", "epoch", "at", "updated", "character_id", "generation"})
        _record_data_schema(name, value)


def _record_key(name, row):
    if name == "mem_item_guards":
        return _json([row[key] for key in ("scope", "cid", "epoch", "collection", "item_id")])
    if name in ("mem_chats",):
        return _json([row["scope"], row["cid"]])
    if name == "mem_turn_aliases":
        return _json([row["scope"], row["cid"], row["key"]])
    if name == "mem_images":
        return _json([row["scope"], row["cid"], row["ref"]])
    if name == "mem_turn_images":
        return _json([row["turn_id"], row["side"], row["position"]])
    return "singleton" if name == "settings" else str(row["id"])


def validate_data(directory: Path, manifest, cancel, *, source=None):
    """Validate portable data in a private temporary index, never a live store."""
    directory = Path(directory)
    inventory = manifest.get("inventory")
    allowed = {*MEMORY_TABLES, "settings", "characters", "reference_assets", "life_plans",
               "life_adjustments", "life_sources", "life_evidence_metadata", "life_characters", "calendar", "calendar_connections", "memory_model_settings"}
    if not isinstance(inventory, dict) or not inventory or not set(inventory) <= allowed:
        raise BackupError("DATA_SCHEMA_INVALID")
    actual = {p.name for p in (directory / "data").iterdir()}
    if actual != {name + ".jsonl" for name in inventory}:
        raise BackupError("DATA_INVENTORY_MISMATCH")
    _validate_metadata(manifest, inventory)
    with tempfile.TemporaryDirectory(prefix="backup-index-", dir=directory.parent) as scratch:
        db = sqlite3.connect(Path(scratch) / "index.sqlite3")
        try:
            db.execute("PRAGMA journal_mode=OFF")
            db.execute("PRAGMA cache_size=-2048")
            db.execute("CREATE TABLE rows(name TEXT,key TEXT,scope TEXT,cid TEXT,ordinal INTEGER,data TEXT,PRIMARY KEY(name,key))")
            db.execute("CREATE INDEX scope_rows ON rows(name,scope,cid,ordinal)")
            db.execute("CREATE UNIQUE INDEX turn_keys ON rows(scope,cid,json_extract(data,'$.key')) WHERE name='mem_turns'")
            for name in inventory:
                digest, count, size = hashlib.sha256(), 0, 0
                for row, raw in _file_rows(directory, name, cancel):
                    digest.update(raw)
                    size += len(raw)
                    count += 1
                    if size > MAX_FILE_BYTES:
                        raise BackupError("DATA_SIZE_LIMIT")
                    try:
                        _validate_row(name, row, manifest.get("schema_version", 2))
                        identity = row["data"] if name == "life_evidence_metadata" else row
                        db.execute("INSERT INTO rows VALUES(?,?,?,?,?,?)", (name, _record_key(name, row), identity.get("scope", ""), identity.get("cid", ""), row.get("ordinal"), _json(row)))
                    except (KeyError, TypeError, sqlite3.IntegrityError) as exc:
                        raise BackupError("DATA_IDENTITY_INVALID") from exc
                if {"rows": count, "bytes": size, "sha256": digest.hexdigest()} != inventory[name]:
                    raise BackupError("DATA_INVENTORY_MISMATCH")
            db.commit()
            return _validate_relations(db, directory, manifest, cancel, source)
        finally:
            db.close()


def _validate_metadata(manifest, inventory):
    _check_source(manifest.get("source"))
    modules = manifest.get("modules")
    if not isinstance(modules, list) or not modules or len(set(modules)) != len(modules) or any(m not in MODULES for m in modules):
        raise BackupError("DATA_SCHEMA_INVALID")
    required = set()
    if "reference" in modules:
        required.update(("characters", "reference_assets"))
    if "life" in modules:
        required.update(("life_plans", "life_adjustments", "life_sources", "life_evidence_metadata", "calendar"))
        if "reference" not in modules:
            required.add("life_characters")
    if "memory" in modules:
        required.update((*(MEMORY_TABLES_V1 if manifest.get("schema_version", 2) == 1 else MEMORY_TABLES), "memory_model_settings"))
    if "settings" in modules:
        required.update(("settings", "calendar" if "life" in modules else "calendar_connections", "memory_model_settings"))
    prior_mapping = copy.deepcopy(MAPPING_CONTRACT_V1)
    prior_mapping["references"].remove("settings.plan_recovery.assistant_binding.preset_id")
    mappings = (MAPPING_CONTRACT_V1, prior_mapping) if manifest.get("schema_version", 2) == 1 else (MAPPING_CONTRACT,)
    if set(inventory) != required or manifest.get("mapping_contract") not in mappings:
        raise BackupError("DATA_SCHEMA_INVALID")
    if manifest.get("counts") != {k: v.get("rows") for k, v in inventory.items()} or any(type(v.get("rows")) is not int or v["rows"] < 0 for v in inventory.values()):
        raise BackupError("DATA_SCHEMA_INVALID")
    if "settings" in inventory and inventory["settings"]["rows"] != 1:
        raise BackupError("DATA_SCHEMA_INVALID")
    dependencies = manifest.get("dependencies")
    if not isinstance(dependencies, list) or any(d not in ("CHARACTER_SESSION_BINDINGS", "MEMORY_MODEL_CONNECTIONS") for d in dependencies):
        raise BackupError("DATA_SCHEMA_INVALID")
    omission_codes = {"HOST_CHAT_AND_IMAGES", "GENERATED_ALBUM_IMAGES", "DERIVED_MEDIA",
                      "EXECUTION_DELIVERY_RECOVERY_QUEUES", "RAW_MODEL_RESPONSE_CACHE", "VECTOR_CACHE",
                      "OPTIONAL_FUTURE_AGREEMENTS_AND_ADJUSTMENTS", *("SOURCE_TABLE_ABSENT:" + t for t in MEMORY_TABLES)}
    if not isinstance(manifest.get("omissions"), list) or any(c not in omission_codes for c in manifest["omissions"]):
        raise BackupError("DATA_SCHEMA_INVALID")
    limitation_codes = {"CREDENTIALS_REQUIRE_COMPLETION", "SOURCE_REFERENCES_UNPROVEN", "SOURCE_BINDING_REQUIRES_REVIEW", "ORIGINAL_TEXT_ALREADY_PRUNED", "ORIGINAL_IMAGES_NOT_INCLUDED"}
    if not isinstance(manifest.get("limitations"), list):
        raise BackupError("DATA_SCHEMA_INVALID")
    for item in manifest["limitations"]:
        if not isinstance(item, dict) or set(item) != {"code", "count"} or item["code"] not in limitation_codes or type(item["count"]) is not int or item["count"] < 0:
            raise BackupError("DATA_SCHEMA_INVALID")
    if len({item["code"] for item in manifest["limitations"]}) != len(manifest["limitations"]):
        raise BackupError("DATA_SCHEMA_INVALID")
    if type(manifest.get("credentials_included")) is not bool or type(manifest.get("credentials_omitted")) is not int or manifest["credentials_omitted"] < 0:
        raise BackupError("DATA_SCHEMA_INVALID")
    if manifest["credentials_included"] and manifest["credentials_omitted"]:
        raise BackupError("CREDENTIAL_POLICY_INVALID")
    if manifest["credentials_omitted"] and not any(
        manifest["counts"].get(table, 0)
        for table in ("settings", "memory_model_settings", "calendar", "calendar_connections", "mem_chats")
    ):
        raise BackupError("CREDENTIAL_POLICY_INVALID")


def _validate_relations(db, directory, manifest, cancel, source=None):
    missing = hashlib.sha256()
    missing_count = 0
    pruned_count = 0
    unresolved_images = 0

    def get(name, key):
        if name == "life_evidence_metadata":
            scope, cid = _load(key)
            row = db.execute("SELECT data FROM rows WHERE name=? AND scope=? AND cid=?", (name, scope, cid)).fetchone()
        else:
            row = db.execute("SELECT data FROM rows WHERE name=? AND key=?", (name, str(key))).fetchone()
        return _load(row[0]) if row else None

    def items(name):
        for record in db.execute("SELECT key,data FROM rows WHERE name=? ORDER BY key", (name,)):
            check_cancel(cancel)
            yield record[0], _load(record[1])

    def need(target, key, owner, field, scope=None, cid=None):
        nonlocal missing_count
        found = get(target, key)
        if found and (scope is None or found.get("scope") == scope and found.get("cid") == cid):
            return found
        if source is not None:
            original = None
            if target in ("mem_turns", "mem_batches", "mem_maintenance_runs") and _table(source, target):
                if isinstance(key, str) and key.startswith("ordinal:"):
                    original = source.execute("SELECT scope,cid FROM mem_turns WHERE scope=? AND cid=? AND ordinal=?", (scope, cid, key.split(":", 1)[1])).fetchone()
                else:
                    original = source.execute(f"SELECT scope,cid FROM {target} WHERE id=?", (key,)).fetchone()
            elif target in ("mem_chats", "mem_images") and _table(source, target):
                identity = _load(key)
                if target == "mem_chats":
                    original = source.execute("SELECT scope,cid FROM mem_chats WHERE scope=? AND cid=?", identity).fetchone()
                else:
                    original = source.execute("SELECT scope,cid FROM mem_images WHERE scope=? AND cid=? AND ref=?", identity).fetchone()
            elif target in ("life_plans", "life_sources"):
                kinds = ("life_plan", "life_plan") if target == "life_plans" else ("daily_source", "restore_source")
                original = source.execute("SELECT '' AS scope,'' AS cid FROM records WHERE kind IN (?,?) AND id=?", (*kinds, key)).fetchone()
            elif target == "life_evidence_metadata":
                identity = _load(key)
                original = source.execute("SELECT '' AS scope,'' AS cid FROM records WHERE kind IN ('daily_cursor','restore_evidence') AND json_extract(data,'$.scope')=? AND json_extract(data,'$.cid')=?", identity).fetchone()
            if original is not None and (scope is None or original["scope"] == scope and original["cid"] == cid):
                raise BackupError("EXPORT_INCOMPLETE")
        missing.update((_json([owner, field, target, key, scope, cid]) + "\n").encode("utf-8"))
        missing_count += 1
        return None

    def unresolved(owner, field, value):
        nonlocal missing_count
        missing.update((_json([owner, field, value]) + "\n").encode("utf-8"))
        missing_count += 1

    def memory_item(item, collection, owner, scope, cid):
        if not isinstance(item, dict):
            raise BackupError("MEMORY_STRUCTURE_INVALID")
        for field in ("batch", "closed_batch"):
            if item.get(field) is not None:
                need("mem_batches", item[field], owner, field, scope, cid)
        if collection == "events" and item.get("turns"):
            turns = item["turns"]
            if not isinstance(turns, list) or len(turns) != 2:
                raise BackupError("MEMORY_STRUCTURE_INVALID")
            for ordinal in turns:
                row = db.execute("SELECT key FROM rows WHERE name='mem_turns' AND scope=? AND cid=? AND ordinal=?", (scope, cid, ordinal)).fetchone()
                if not row:
                    need("mem_turns", "ordinal:" + str(ordinal), owner, "turns", scope, cid)
        if collection == "people":
            for relationship in item.get("relationships", []):
                for part in (relationship, *relationship.get("history", [])):
                    if part.get("batch") is not None:
                        need("mem_batches", part["batch"], owner, "relationships.batch", scope, cid)

    def memory(value, owner, scope, cid):
        if not isinstance(value, dict):
            raise BackupError("MEMORY_STRUCTURE_INVALID")
        for collection in ("events", "people", "profiles", "plans"):
            for item in value.get(collection, []):
                memory_item(item, collection, owner, scope, cid)

    if not manifest["credentials_included"]:
        for table, field in (("settings", "data"), ("memory_model_settings", "data"), ("calendar", "data"), ("calendar_connections", "data"), ("mem_chats", "settings")):
            for _key, row in items(table):
                value = row[field]
                if isinstance(value, str):
                    value = _load(value)
                if strip_credentials(value)[1]:
                    raise BackupError("CREDENTIAL_POLICY_INVALID")

    config = get("memory_model_settings", "1")
    presets = {p.get("id") for p in _load(config["data"]).get("presets", [])} if config else set()
    for key, row in items("settings"):
        settings_value = _load(row["data"]) if isinstance(row["data"], str) else row["data"]
        binding = settings_value.get("plan_recovery", {}).get("assistant_binding", {})
        if binding.get("mode") == "preset" and binding.get("preset_id") not in presets:
            unresolved(["settings", key], "plan_recovery.assistant_binding.preset_id", binding["preset_id"])
        binding = settings_value.get("memory_maintenance", {}).get("binding", {})
        if binding.get("mode") == "preset" and binding.get("preset_id") not in presets:
            unresolved(["settings", key], "memory_maintenance.binding.preset_id", binding["preset_id"])
    for key, row in items("mem_chats"):
        memory(_load(row["memory"]), ["mem_chats", key], row["scope"], row["cid"])
        for role, binding in _load(row["settings"]).get("model_bindings", {}).items():
            if binding.get("mode") == "preset" and binding.get("preset_id") and binding["preset_id"] not in presets:
                unresolved(["mem_chats", key], "model_bindings." + role, binding["preset_id"])
    for table in ("mem_turns", "mem_batches", "mem_images", "mem_snapshots"):
        for key, row in items(table):
            owner, scope, cid = [table, key], row["scope"], row["cid"]
            need("mem_chats", _json([scope, cid]), owner, "chat")
            if table == "mem_turns":
                pruned_count += bool(row.get("pruned"))
                if row.get("batch") is not None:
                    need("mem_batches", row["batch"], owner, "batch", scope, cid)
            elif table == "mem_batches":
                if row.get("maintenance_claim"):
                    need("mem_maintenance_runs", row["maintenance_claim"], owner, "maintenance_claim", scope, cid)
                diff = _load(row["diff"])
                for collection, changes in diff.get("items", {}).items():
                    if collection not in ("events", "people", "profiles", "plans"):
                        raise BackupError("MEMORY_STRUCTURE_INVALID")
                    for change in changes:
                        if not isinstance(change, list) or len(change) != 3:
                            raise BackupError("MEMORY_STRUCTURE_INVALID")
                        for item in change[1:]:
                            if item is not None:
                                memory_item(item, collection, owner, scope, cid)
            elif table == "mem_snapshots":
                memory(_load(row["memory"]), owner, scope, cid)
                snapshot = _load(row["turns"])
                for turn_id, _status, batch in snapshot.get("turns", []):
                    need("mem_turns", turn_id, owner, "turns.id", scope, cid)
                    if batch is not None:
                        need("mem_batches", batch, owner, "turns.batch", scope, cid)
                for batch, _state in snapshot.get("batches", []):
                    need("mem_batches", batch, owner, "batches.id", scope, cid)
            else:
                unresolved_images += row.get("status") != "ready"
    for key, row in items("mem_turn_aliases"):
        need("mem_turns", row["turn_id"], ["alias", key], "turn_id", row["scope"], row["cid"])
    for key, row in items("mem_turn_images"):
        turn = need("mem_turns", row["turn_id"], ["link", key], "turn_id")
        if turn:
            need("mem_images", _json([turn["scope"], turn["cid"], row["ref"]]), ["link", key], "ref")

    def maintenance_refs(payload, owner, scope, cid):
        for field, table in (("sources", "mem_turns"), ("dependencies", "mem_batches"), ("batches", "mem_batches")):
            for identifier in payload.get(field, []):
                if isinstance(identifier, int):
                    need(table, identifier, owner, field, scope, cid)
        writer = payload.get("last_writer", payload.get("previous_writer", {}))
        if writer.get("kind") == "summary":
            need("mem_batches", writer["id"], owner, "writer", scope, cid)
        elif writer.get("kind") in ("maintenance", "undo") and writer.get("id"):
            need("mem_maintenance_runs", writer["id"], owner, "writer", scope, cid)

    for table in ("mem_item_guards", "mem_maintenance_runs"):
        for key, row in items(table):
            owner, scope, cid = [table, key], row["scope"], row["cid"]
            need("mem_chats", _json([scope, cid]), owner, "chat")
            payload = _load(row["data" if table == "mem_item_guards" else "payload"])
            maintenance_refs(payload, owner, scope, cid)
            if table == "mem_maintenance_runs" and row["task_id"] != row["id"]:
                need("mem_maintenance_runs", row["task_id"], owner, "task_id", scope, cid)
            expected = payload.get("expected", {})
            if expected.get("source_upper"):
                need("mem_turns", expected["source_upper"], owner, "expected.source_upper", scope, cid)
            for item in expected.get("items", []):
                maintenance_refs(item, owner, scope, cid)
                if item["collection"] in ("events", "people", "profiles", "plans") and isinstance(item.get("value"), dict):
                    memory_item(item["value"], item["collection"], owner, scope, cid)
            evidence = list(payload.get("evidence", []))
            for change in payload.get("changes", []):
                maintenance_refs(change, owner, scope, cid)
                for part in ("before", "after"):
                    memory_item(change[part], change["collection"], owner, scope, cid)
                evidence.extend(v for v in change.get("sources", []) if isinstance(v, dict))
            for proof in evidence:
                original = need("mem_turns", proof["turn_id"], owner, "evidence.turn_id", scope, cid)
                if proof.get("batch") is not None:
                    need("mem_batches", proof["batch"], owner, "evidence.batch", scope, cid)
                if original and proof.get("complete"):
                    content_hash = hashlib.sha256(_json([original["user"], original["assistant"]]).encode()).hexdigest()
                    if proof.get("raw_hash") != content_hash or original["pruned"] or any(
                        proof.get(field) != original.get(field) for field in ("key", "kind", "digest", "ordinal", "batch", "status")):
                        unresolved(owner, "evidence.identity", proof["turn_id"])
    used_media = set()
    for key, row in items("reference_assets"):
        asset = _load(row["data"])
        if asset.get("kind") != "reference" or not get("characters", asset.get("character_id")):
            raise BackupError("REFERENCE_RELATION_INVALID")
        digest = asset.get("hash", "")
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise BackupError("REFERENCE_HASH_INVALID")
        path = directory / "media" / (digest + ".png")
        if not path.is_file() or path.is_symlink():
            raise BackupError("REFERENCE_FILE_MISSING")
        if digest not in used_media:
            actual = hashlib.sha256()
            with path.open("rb") as handle:
                while chunk := handle.read(CHUNK_BYTES):
                    check_cancel(cancel)
                    actual.update(chunk)
            if actual.hexdigest() != digest:
                raise BackupError("REFERENCE_HASH_MISMATCH")
            used_media.add(digest)
    actual_media = {p.stem for p in (directory / "media").glob("*.png")}
    if actual_media != used_media:
        raise BackupError("REFERENCE_INVENTORY_MISMATCH")
    for key, row in items("characters"):
        character = _load(row["data"])
        versions = character.get("versions", [])
        if character.get("active_version") and character["active_version"] not in {v.get("id") for v in versions}:
            raise BackupError("REFERENCE_RELATION_INVALID")
        for version in versions:
            for asset_id in version.get("asset_ids", []):
                asset = get("reference_assets", asset_id)
                if not asset or _load(asset["data"]).get("character_id") != key:
                    raise BackupError("REFERENCE_RELATION_INVALID")
    for key, row in items("life_plans"):
        plan = _load(row["data"])
        char = get("characters", plan.get("character_id")) or get("life_characters", plan.get("character_id"))
        if not char:
            unresolved(["life_plan", key], "character_id", plan.get("character_id"))
    for key, row in items("life_adjustments"):
        adjustment = _load(row["data"])
        owner = ["life_adjustment", key]
        plan = need("life_plans", adjustment.get("plan_id"), owner, "plan_id")
        if plan:
            plan_data = _load(plan["data"])
            if adjustment.get("plan_revision") != plan["revision"] or adjustment.get("scope") != plan_data.get("scope") or adjustment.get("character_id") != plan_data.get("character_id"):
                unresolved(owner, "plan_identity", [adjustment.get("plan_id"), adjustment.get("plan_revision"), adjustment.get("scope"), adjustment.get("character_id")])
        for source_id in adjustment.get("source_ids", []):
            evidence = need("life_sources", source_id, owner, "source_ids")
            if evidence:
                evidence_data = _load(evidence["data"])
                if any(evidence_data.get(field) != adjustment.get(field) for field in ("scope", "cid")):
                    unresolved(owner, "evidence_identity", source_id)
        if adjustment.get("evidence_revision") is not None:
            cursor = need("life_evidence_metadata", _json([adjustment.get("scope"), adjustment.get("cid")]), owner, "evidence_revision")
            if cursor and adjustment["evidence_revision"] != cursor["revision"]:
                unresolved(owner, "evidence_revision", [adjustment["evidence_revision"], cursor["revision"]])
    integrity = {"missing_references": missing_count, "missing_sha256": missing.hexdigest()}
    if "source_integrity" in manifest and manifest["source_integrity"] != integrity:
        raise BackupError("DATA_RELATION_CHANGED")
    limitations = []
    # The omission count is source-capture evidence: removed values cannot be
    # reconstructed from an uploaded package. Verify its declaration, never
    # claim it authenticates the source or detects secrets already removed.
    for code, count in (("CREDENTIALS_REQUIRE_COMPLETION", manifest["credentials_omitted"]),
                        ("SOURCE_BINDING_REQUIRES_REVIEW", manifest["counts"].get("mem_chats", 0))):
        if count:
            limitations.append({"code": code, "count": count})
    for code, count in (("SOURCE_REFERENCES_UNPROVEN", missing_count), ("ORIGINAL_TEXT_ALREADY_PRUNED", pruned_count), ("ORIGINAL_IMAGES_NOT_INCLUDED", unresolved_images)):
        if count:
            limitations.append({"code": code, "count": count})
    if source is None and {item["code"]: item["count"] for item in manifest["limitations"]} != {item["code"]: item["count"] for item in limitations}:
        raise BackupError("DATA_LIMITATIONS_MISMATCH")
    return {"source_integrity": integrity, "limitations": limitations}
