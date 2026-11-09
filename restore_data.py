"""Explicit v1 backup mappings. This module never commits or calls a service.

The caller owns authenticated files, maintenance, target transactions and media
publication. Large source tables and their ID maps stay in a private disk index.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path

from . import backup_data
from .backup_format import BackupError, check_cancel

MAX_OBJECTS = 200
_INDEX = ".restore-index.sqlite3"
_COLLECTIONS = ("events", "people", "profiles", "plans")


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def chat_key(scope, cid):
    return _json([scope, cid])


def _need(condition, code="RESTORE_MAPPING_INVALID"):
    if not condition:
        raise BackupError(code)


def _index(checked, manifest, cancel=None):
    path = Path(checked) / _INDEX
    fresh = not path.exists()
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    if fresh:
        try:
            db.executescript("""
                CREATE TABLE source(name TEXT, identity TEXT, owner TEXT, payload TEXT,
                    PRIMARY KEY(name,identity));
                CREATE INDEX source_owner ON source(name,owner);
                CREATE TABLE ids(kind TEXT,owner TEXT,old TEXT,new INTEGER,
                    PRIMARY KEY(kind,owner,old));
                CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
            """)
            for name in manifest["inventory"]:
                for row, _ in backup_data._file_rows(Path(checked), name, cancel):
                    owner = chat_key(row.get("scope", ""), row.get("cid", ""))
                    identity = backup_data._record_key(name, row)
                    db.execute("INSERT INTO source VALUES(?,?,?,?)", (name, identity, owner, _json(row)))
            db.commit()
        except BaseException:
            db.close()
            path.unlink(missing_ok=True)
            raise
    return db


def _rows(db, name, owner=None):
    sql, args = "SELECT payload FROM source WHERE name=?", [name]
    if owner is not None:
        sql += " AND owner=?"
        args.append(owner)
    for row in db.execute(sql + " ORDER BY rowid", args):
        yield json.loads(row[0])


def _get(db, name, identifier):
    row = db.execute("SELECT payload FROM source WHERE name=? AND identity=?", (name, str(identifier))).fetchone()
    return json.loads(row[0]) if row else None


def _data(row):
    return json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]


def _target(db, kind, identifier):
    row = db.execute("SELECT * FROM records WHERE kind=? AND id=?", (kind, identifier)).fetchone()
    return dict(row) if row else None


def _models(db):
    try:
        row = db.execute("SELECT data,revision FROM memory_model_settings WHERE id=1").fetchone()
    except sqlite3.OperationalError:
        row = None
    return (json.loads(row[0]), row[1]) if row else ({}, 0)


def _identifier(archive_id, kind, value):
    return "restore_" + hashlib.sha256(_json([archive_id, kind, value]).encode()).hexdigest()[:24]


def _calendar_trips(incoming, current, mapping):
    """Trip IDs are globally addressed by the normal calendar writer."""
    scopes = mapping["scopes"]
    trips = [copy.deepcopy(trip) for trip in current
             if trip.get("scope", "") and trip["scope"] not in scopes.values()]
    for trip in incoming:
        source_scope = trip.get("scope") or ""
        if source_scope and source_scope not in scopes:
            continue
        target_scope = scopes[source_scope] if source_scope else ""
        # Global trips have their own domain; personal copies bind both owners.
        identifier = _identifier(mapping["archive_id"], "trip-personal" if source_scope else "trip-global",
            [source_scope, target_scope, trip.get("id", "")])
        trips.append({**copy.deepcopy(trip), "scope": target_scope, "id": identifier})
    _need(len({trip.get("id", "") for trip in trips}) == len(trips), "RESTORE_TRIP_CONFLICT")
    return trips


def _snapshot_id(mapping, item, source_id):
    return _identifier(mapping["archive_id"], "snapshot", [item["source_scope"], item["source_cid"],
        item["target_scope"], item["target_cid"], source_id])


def character_binding_conflicts(target, replacements):
    """Check the final bindings, including unchanged characters, without bodies."""
    if not replacements:
        return set()
    rows = target.execute("SELECT id,json_extract(data,'$.sessions') FROM records WHERE kind='character' LIMIT 201").fetchall()
    _need(len(rows) <= 200, "CATALOG_LIMIT")
    final = {row[0]: json.loads(row[1] or "[]") for row in rows}
    final.update(replacements)
    owners, conflicts = {}, set()
    for identifier, sessions in final.items():
        for scope in set(sessions):
            if scope in owners and owners[scope] != identifier:
                conflicts.update((identifier, owners[scope]))
            owners[scope] = identifier
    return conflicts.intersection(replacements)


def _calendar_scope(source, current, mapping):
    selected = mapping["calendar"]
    incoming = next((_data(row) for row in _rows(source, "calendar")), {})
    source_trips, target_trips = incoming.get("trips", []), current.get("trips", [])
    scopes = mapping["scopes"] if selected else {}
    return {
        "global": {"action": "replace" if selected else "keep",
                   "source_trips": sum(not v.get("scope", "") for v in source_trips),
                   "target_trips": sum(not v.get("scope", "") for v in target_trips)},
        "mapped": [{"source_scope": old, "target_scope": new,
                    "source_trips": sum(v.get("scope") == old for v in source_trips),
                    "target_trips": sum(v.get("scope") == new for v in target_trips)} for old, new in scopes.items()],
        "preserved_target_trips": sum(not selected or bool(v.get("scope", "")) and v.get("scope") not in scopes.values() for v in target_trips),
        "unselected_source_trips": sum(not selected or bool(v.get("scope", "")) and v.get("scope") not in scopes for v in source_trips),
    }


def catalog(checked, manifest, cancel=None):
    """Only identifiers, names and counts; never history or connection secrets."""
    db = _index(checked, manifest, cancel)
    result = {"modules": manifest["modules"], "characters": [], "plans": [], "chats": [],
              "connections": {"presets": [], "providers": [], "search_schemes": []}, "calendar": False,
              "scopes": [], "life_chats": []}
    scopes, life_chats = set(), set()
    try:
        for table, field in (("characters", "characters"), ("life_plans", "plans")):
            for row in _rows(db, table):
                value = _data(row)
                entry = {"id": row["id"], "name": value.get("name", ""), "revision": row["revision"]}
                if table == "life_plans":
                    entry.update({k: value.get(k) for k in ("scope", "run_once", "enabled", "character_id")})
                    if value.get("scope"):
                        scopes.add(value["scope"])
                    if value.get("scope") and value.get("origin_cid"):
                        life_chats.add((value["scope"], value["origin_cid"]))
                else:
                    entry["sessions"] = value.get("sessions", [])
                    scopes.update(entry["sessions"])
                result[field].append(entry)
                _need(len(result[field]) <= MAX_OBJECTS, "RESTORE_CATALOG_LIMIT")
        known_characters = {item["id"] for item in result["characters"]}
        for row in _rows(db, "life_characters"):
            if row["id"] not in known_characters:
                value = _data(row)
                result["characters"].append({"id": row["id"], "name": value.get("name", ""),
                                              "sessions": value.get("sessions", []), "dependency_only": True})
                scopes.update(value.get("sessions", []))
        for row in _rows(db, "life_adjustments"):
            value = _data(row)
            if value.get("scope") and value.get("cid"):
                life_chats.add((value["scope"], value["cid"]))
                scopes.add(value["scope"])
            _need(len(life_chats) <= MAX_OBJECTS, "RESTORE_CATALOG_LIMIT")
        for row in _rows(db, "mem_chats"):
            owner = chat_key(row["scope"], row["cid"])
            counts = {name: db.execute("SELECT count(*) FROM source WHERE name=? AND owner=?", (name, owner)).fetchone()[0]
                      for name in ("mem_turns", "mem_batches", "mem_snapshots", "mem_images")}
            settings = json.loads(row["settings"])
            scopes.add(row["scope"])
            result["chats"].append({"scope": row["scope"], "cid": row["cid"], "enabled": bool(row["enabled"]),
                                    "counts": counts, "retention_days": settings.get("retention_days", 30)})
            _need(len(result["chats"]) <= MAX_OBJECTS, "RESTORE_CATALOG_LIMIT")
        for row in _rows(db, "memory_model_settings"):
            for item in _data(row).get("presets", []):
                result["connections"]["presets"].append({k: item.get(k, "") for k in ("id", "name", "type", "model")})
        for row in _rows(db, "settings"):
            value = _data(row)
            result["connections"]["providers"] = [{"id": "primary", "name": value.get("primary_name", "主站")}]
            result["connections"]["providers"] += [{"id": p["id"], "name": p.get("name", "")} for p in value.get("providers", [])]
        for table in ("calendar", "calendar_connections"):
            for row in _rows(db, table):
                value = _data(row)
                result["calendar"] = table == "calendar"
                result["connections"]["search_schemes"] += [{"id": p.get("id", ""), "name": p.get("name", "")}
                    for p in value.get("search_schemes", [])]
                scopes.update(value.get("personal_calendars", {}))
                scopes.update(item["scope"] for item in value.get("trips", []) if item.get("scope"))
        result["scopes"] = sorted(scopes)
        result["life_chats"] = [{"scope": scope, "cid": cid} for scope, cid in sorted(life_chats)]
        _need(sum(len(result[k]) for k in ("characters", "plans", "chats"))
              + sum(map(len, result["connections"].values())) + len(life_chats) + len(scopes) <= MAX_OBJECTS, "RESTORE_CATALOG_LIMIT")
        return result
    finally:
        db.close()


def _choose_records(source, target, selected, table, kind, archive_id):
    _need(isinstance(selected, list) and len(selected) <= MAX_OBJECTS)
    results, seen, targets = [], set(), set()
    for item in selected:
        _need(isinstance(item, dict) and isinstance(item.get("source_id"), str))
        source_id = item["source_id"]
        _need(source_id not in seen)
        seen.add(source_id)
        row = _get(source, table, source_id)
        _need(row is not None)
        identifier = item.get("target_id") or _identifier(archive_id, kind, source_id)
        _need(isinstance(identifier, str) and 0 < len(identifier) <= 128 and identifier not in targets)
        targets.add(identifier)
        old = _target(target, kind, identifier)
        _need(old is None or item.get("replace") is True, "RESTORE_REPLACE_REQUIRED")
        results.append({"source_id": source_id, "target_id": identifier,
                        "name": str(_data(row).get("name", ""))[:80],
                        "target_summary": str(_data(old or row).get("name", ""))[:80],
                        "revision": old["revision"] + 1 if old else 1, "action": "replace" if old else "add"})
    return results


def plan(checked, manifest, target_db, target_settings, selection, *, archive_id, host_evidence=None, cancel=None):
    """Freeze explicit choices; returned mapping contains no source body or key."""
    _need(isinstance(selection, dict))
    modules = selection.get("modules", [])
    _need(isinstance(modules, list) and len(modules) == len(set(modules)) and set(modules) <= set(manifest["modules"]))
    available = catalog(checked, manifest, cancel)
    source = _index(checked, manifest, cancel)
    token = uuid.uuid4().hex
    mapping = {"version": 1, "plan_id": token, "archive_id": archive_id, "modules": modules,
               "characters": [], "plans": [], "chats": [], "scopes": {},
               "settings": selection.get("settings", "keep"), "connections": selection.get("connections", {}),
               "calendar": selection.get("calendar", False), "character_links": selection.get("character_links", {}),
               "life_chats": selection.get("life_chats", [])}
    _need(mapping["settings"] in ("keep", "replace"))
    _need(isinstance(mapping["connections"], dict) and type(mapping["calendar"]) is bool)
    _need(not set(mapping["connections"]) - {"presets", "providers", "search_schemes"})
    for aliases in mapping["connections"].values():
        _need(isinstance(aliases, dict) and len(aliases) <= MAX_OBJECTS
              and all(isinstance(old, str) and isinstance(new, str) for old, new in aliases.items()))
    _need(isinstance(mapping["life_chats"], list) and len(mapping["life_chats"]) <= MAX_OBJECTS)
    for chat in mapping["life_chats"]:
        _need(isinstance(chat, dict) and all(isinstance(chat.get(field), str) and chat[field]
              for field in ("source_scope", "source_cid", "target_scope", "target_cid")))
    _need(isinstance(mapping["character_links"], dict) and len(mapping["character_links"]) <= MAX_OBJECTS)
    for old, new in mapping["character_links"].items():
        _need(isinstance(old, str) and isinstance(new, str) and _target(target_db, "character", new) is not None,
              "RESTORE_CHARACTER_MISSING")
    scopes = selection.get("scopes", {})
    _need(isinstance(scopes, dict) and len(scopes) <= MAX_OBJECTS
          and all(isinstance(k, str) and isinstance(v, str) and k and v and len(v) <= 512 for k, v in scopes.items()))
    mapping["scopes"] = scopes
    try:
        source.execute("DELETE FROM ids")
        source.execute("INSERT OR REPLACE INTO meta VALUES('plan',?)", (token,))
        if "reference" in modules:
            mapping["characters"] = _choose_records(source, target_db, selection.get("characters", []), "characters", "character", archive_id)
        if "life" in modules:
            mapping["plans"] = _choose_records(source, target_db, selection.get("plans", []), "life_plans", "life_plan", archive_id)
        character_ids = {**mapping["character_links"], **{item["source_id"]: item["target_id"] for item in mapping["characters"]}}
        for item in mapping["characters"]:
            value = _data(_get(source, "characters", item["source_id"]))
            item["sessions"] = list(dict.fromkeys(scopes[scope] for scope in value.get("sessions", []) if scope in scopes))
            item["reason"] = "SESSION_BINDING_REQUIRED" if any(scope not in scopes for scope in value.get("sessions", [])) else ""
        _need(not character_binding_conflicts(target_db, {item["target_id"]: item["sessions"] for item in mapping["characters"]}),
              "SESSION_BINDING_CONFLICT")
        selected_plans = {item["source_id"]: item for item in mapping["plans"]}
        plan_sources = {}
        for item in mapping["plans"]:
            plan_row = _get(source, "life_plans", item["source_id"])
            value = _data(plan_row)
            plan_sources[item["source_id"]] = (plan_row["revision"], value)
            item["scope"] = scopes.get(value.get("scope"), "")
            item["character_id"] = character_ids.get(value.get("character_id"), "")
            item["reason"] = "LIFE_BINDING_REQUIRED" if not item["scope"] or not item["character_id"] else ""
            item["requires_future_confirmation"] = bool(value.get("run_once"))
        for adjustment_row in _rows(source, "life_adjustments"):
            check_cancel(cancel)
            adjustment = _data(adjustment_row)
            item = selected_plans.get(adjustment.get("plan_id"))
            if item is None:
                continue
            revision, value = plan_sources[item["source_id"]]
            if (adjustment.get("plan_revision") != revision
                or adjustment.get("character_id") != value.get("character_id")
                or adjustment.get("scope") != value.get("scope")
                or value.get("origin_cid") and adjustment.get("cid") != value["origin_cid"]):
                item["adjustment_conflict"] = True
                item["reason"] = "ADJUSTMENT_CONFLICT"
        _need(mapping["settings"] == "keep" or "settings" in modules)
        _need(not mapping["calendar"] or "life" in modules)
        selected = selection.get("chats", []) if "memory" in modules else []
        _need(isinstance(selected, list) and len(selected) <= MAX_OBJECTS)
        owners, destinations = set(), set()
        for item in selected:
            _need(isinstance(item, dict))
            ss, sc = item.get("source_scope"), item.get("source_cid")
            _need(isinstance(ss, str) and isinstance(sc, str))
            owner = chat_key(ss, sc)
            _need(owner not in owners)
            owners.add(owner)
            row = _get(source, "mem_chats", owner)
            _need(row is not None)
            mode = item.get("mode", "archive")
            _need(mode in ("original", "new", "archive"))
            dest = (item.get("target_scope", ""), item.get("target_cid", ""))
            _need(all(isinstance(v, str) for v in dest))
            proof = (host_evidence or {}).get(owner, {})
            reason = "ARCHIVE_ONLY"
            if mode != "archive":
                _need(dest[0] and dest[1] and len(dest[0]) <= 512 and len(dest[1]) <= 256)
                _need(dest not in destinations, "RESTORE_CHAT_COLLISION")
                destinations.add(dest)
                if not proof.get("exists") or not (proof.get("verified") if mode == "original" else proof.get("empty")):
                    reason = "CHAT_IDENTITY_UNPROVEN" if mode == "original" else "CHAT_NOT_EMPTY"
                    mode = "archive"
            old = target_db.execute("SELECT revision,generation FROM mem_chats WHERE scope=? AND cid=?", dest).fetchone() if mode != "archive" else None
            epoch = _target(target_db, "restore_epoch", hashlib.sha256(chat_key(*dest).encode()).hexdigest()) if mode != "archive" else None
            epoch_generation = _data(epoch).get("generation", 0) if epoch else 0
            _need(type(epoch_generation) is int and epoch_generation >= 0, "RESTORE_TARGET_SCHEMA")
            _need(old is None or item.get("replace") is True, "RESTORE_REPLACE_REQUIRED")
            counts = {table: source.execute("SELECT count(*) FROM source WHERE name=? AND owner=?", (table, owner)).fetchone()[0]
                      for table in ("mem_turns", "mem_batches", "mem_snapshots")}
            policy = json.loads(row["settings"])
            days = policy.get("retention_days", 30)
            keep = max(1, policy.get("keep_turns", 10))
            stamp = int(time.time())
            floor = source.execute("SELECT coalesce(min(ordinal),0) FROM (SELECT json_extract(payload,'$.ordinal') ordinal FROM source "
                "WHERE name='mem_turns' AND owner=? AND json_extract(payload,'$.status')='summarized' ORDER BY ordinal DESC LIMIT ?)",
                (owner, keep)).fetchone()[0]
            expired_originals = source.execute("SELECT count(*) FROM source WHERE name='mem_turns' AND owner=? "
                "AND json_extract(payload,'$.status')='summarized' AND json_extract(payload,'$.pruned')=0 "
                "AND json_extract(payload,'$.summarized_at')>0 AND json_extract(payload,'$.summarized_at')<? "
                "AND json_extract(payload,'$.ordinal')<?", (owner, stamp - days * 86400, floor)).fetchone()[0] if days else 0
            expired_snapshots = source.execute("SELECT count(*) FROM source WHERE name='mem_snapshots' AND owner=? "
                "AND json_extract(payload,'$.created')<?", (owner, stamp - 30 * 86400)).fetchone()[0]
            mapping["chats"].append({"source_scope": ss, "source_cid": sc, "target_scope": dest[0], "target_cid": dest[1],
                "mode": mode, "reason": reason if mode == "archive" else "", "action": "archive" if mode == "archive" else "replace" if old else "add",
                "revision": old[0] + 1 if old else 1, "generation": max(old[1] if old else 0, epoch_generation) + 1,
                "host_fingerprint": proof.get("fingerprint", ""), "counts": counts,
                "retention": {"days": days, "keep_turns": keep, "expired_originals": expired_originals, "expired_snapshots": expired_snapshots}})
        for item in mapping["chats"]:
            if item["mode"] != "original":
                continue
            for snapshot in _rows(source, "mem_snapshots", chat_key(item["source_scope"], item["source_cid"])):
                check_cancel(cancel)
                existing = target_db.execute("SELECT scope,cid FROM mem_snapshots WHERE id=?",
                    (_snapshot_id(mapping, item, snapshot["id"]),)).fetchone()
                _need(existing is None or tuple(existing) == (item["target_scope"], item["target_cid"]),
                      "RESTORE_SNAPSHOT_CONFLICT")
        life_destinations = {(item["source_scope"], item["source_cid"]): (item["target_scope"], item["target_cid"])
                             for item in mapping["chats"] if item["mode"] != "archive"}
        for item in mapping["life_chats"]:
            key = (item["source_scope"], item["source_cid"])
            destination = (item["target_scope"], item["target_cid"])
            _need(key not in life_destinations or life_destinations[key] == destination, "RESTORE_CHAT_COLLISION")
            _need(scopes.get(key[0]) == destination[0], "RESTORE_SCOPE_MISMATCH")
            life_destinations[key] = destination
        for item in mapping["plans"]:
            _, value = plan_sources[item["source_id"]]
            needs_cid = bool(value.get("origin_cid")) or bool(value.get("run_once") and value.get("source") in ("chat", "chain"))
            destination = life_destinations.get((value.get("scope"), value.get("origin_cid")))
            if needs_cid:
                if destination:
                    _need(destination[0] == item["scope"], "RESTORE_SCOPE_MISMATCH")
                else:
                    item["reason"] = "LIFE_BINDING_REQUIRED"
                    item["missing_chat_binding"] = True
        for table in ("mem_turns", "mem_batches"):
            maximum = target_db.execute(f"SELECT coalesce(max(id),0) FROM {table}").fetchone()[0]
            high_water = target_db.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone()
            maximum = max(maximum, high_water[0] if high_water else 0)
            for item in mapping["chats"]:
                if item["mode"] != "original":
                    continue
                owner = chat_key(item["source_scope"], item["source_cid"])
                for row in _rows(source, table, owner):
                    check_cancel(cancel)
                    maximum += 1
                    _need(maximum <= 9223372036854775807, "RESTORE_ID_LIMIT")
                    source.execute("INSERT INTO ids VALUES(?,?,?,?)", (table, owner, str(row["id"]), maximum))
            source.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (table + "_next", str(maximum + 1)))
        source.commit()
        # Validate explicit connection reuse before a commit can be offered.
        _apply_settings(source, target_settings, mapping, [])
        incoming_models = list(_rows(source, "memory_model_settings"))
        existing_models, _ = _models(target_db)
        for old, new in mapping["connections"].get("presets", {}).items():
            original = next((p for p in (_data(incoming_models[0]).get("presets", []) if incoming_models else []) if p["id"] == old), None)
            current = next((p for p in existing_models.get("presets", []) if p["id"] == new), None)
            _need(original is not None and current is not None and _connection_signature(original) == _connection_signature(current),
                  "RESTORE_CONNECTION_MISMATCH")
        existing_calendar = _target(target_db, "calendar", "default")
        calendar_draft = _data(existing_calendar) if existing_calendar else {}
        calendar_scope = _calendar_scope(source, calendar_draft, mapping)
        for table in ("calendar", "calendar_connections"):
            for row in _rows(source, table):
                if mapping["calendar"] and table == "calendar":
                    _calendar_trips(_data(row).get("trips", []), calendar_draft.get("trips", []), mapping)
                _calendar_connections(_data(row), calendar_draft, mapping)
        preview = {"characters": mapping["characters"], "plans": mapping["plans"], "chats": mapping["chats"],
                   "settings": mapping["settings"], "calendar": mapping["calendar"], "modules": modules,
                   "calendar_scope": calendar_scope,
                   "limitations": manifest["limitations"], "archive_keeps_all_modules": True}
        preview["objects"] = [{"kind": kind, **item} for kind, key in (("character", "characters"), ("life_plan", "plans"), ("memory", "chats"))
                              for item in mapping[key]]
        preview["objects"] += [{"kind": "settings", "target_id": "global", "action": "replace" if mapping["settings"] == "replace" else "keep"},
                               {"kind": "calendar", "target_id": "default", "action": "replace" if mapping["calendar"] else "keep"}]
        for kind, key in (("character", "characters"), ("life_plan", "plans")):
            selected_ids = {row["source_id"] for row in mapping[key]}
            preview["objects"] += [{"kind": kind, "source_id": row["id"], "name": row.get("name", ""), "action": "keep"}
                                   for row in available[key] if row["id"] not in selected_ids]
        selected_chats = {(row["source_scope"], row["source_cid"]) for row in mapping["chats"]}
        preview["objects"] += [{"kind": "memory", "source_scope": row["scope"], "source_cid": row["cid"], "action": "archive"}
                               for row in available["chats"] if (row["scope"], row["cid"]) not in selected_chats]
        incoming_settings = list(_rows(source, "settings"))
        source_settings = _data(incoming_settings[0]) if incoming_settings else {}
        source_providers = {"primary": source_settings, **{p["id"]: p for p in source_settings.get("providers", [])}}
        target_providers = {"primary": target_settings, **{p["id"]: p for p in target_settings.get("providers", [])}}
        preview["credential_reuse"] = [{"source_id": old, "target_id": new,
            "policy": "ALL_TARGET_SLOTS_AND_SELECTION", "source_slots": len(source_providers[old].get("api_keys", [])),
            "target_slots": len(target_providers[new].get("api_keys", []))}
            for old, new in mapping["connections"].get("providers", {}).items()]
        return {"preview": preview, "mapping": mapping}
    finally:
        source.close()


def _mapped_id(source, kind, owner, old):
    if old is None:
        return None
    row = source.execute("SELECT new FROM ids WHERE kind=? AND owner=? AND old=?", (kind, owner, str(old))).fetchone()
    if row:
        return row[0]
    # A source with missing old provenance stays missing. Allocate a distinct
    # unused ID; never resolve it against an unrelated same-number target row.
    key = kind + "_next"
    row = source.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    number = int(row[0])
    _need(number <= 9223372036854775807, "RESTORE_ID_LIMIT")
    source.execute("UPDATE meta SET value=? WHERE key=?", (str(number + 1), key))
    source.execute("INSERT INTO ids VALUES(?,?,?,?)", (kind, owner, str(old), number))
    return number


def _map_item(item, collection, batch, baseline=False):
    value = copy.deepcopy(item)
    for field in ("batch", "closed_batch"):
        if field in value and value[field] is not None:
            if baseline:
                value.pop(field)
            else:
                value[field] = batch(value[field])
    if collection == "events" and baseline:
        value.pop("turns", None)
    if collection == "people":
        for relation in value.get("relationships", []):
            for part in (relation, *relation.get("history", [])):
                if part.get("batch") is not None:
                    if baseline:
                        part.pop("batch")
                    else:
                        part["batch"] = batch(part["batch"])
    return value


def map_memory(value, batch, *, baseline=False):
    result = copy.deepcopy(value)
    for collection in _COLLECTIONS:
        if collection in result:
            result[collection] = [_map_item(item, collection, batch, baseline) for item in result[collection]]
    return result


def map_diff(value, batch):
    result = copy.deepcopy(value)
    for collection, changes in result.get("items", {}).items():
        result["items"][collection] = [[identifier, *[_map_item(item, collection, batch) if item is not None else None for item in (before, after)]]
                                       for identifier, before, after in changes]
    return result


def _insert(db, table, row):
    fields = [part[1] for part in db.execute(f"PRAGMA table_info({table})")]
    _need(not set(row) - set(fields), "RESTORE_TARGET_SCHEMA")
    values = {key: value for key, value in row.items() if key in fields}
    _need(values, "RESTORE_TARGET_SCHEMA")
    db.execute(f"INSERT INTO {table} ({','.join(values)}) VALUES ({','.join('?' for _ in values)})", tuple(values.values()))


def _save_record(db, kind, identifier, value, revision):
    db.execute("INSERT OR REPLACE INTO records(kind,id,data,revision) VALUES(?,?,?,?)", (kind, identifier, _json(value), revision))


def _remove_chat(db, scope, cid):
    db.execute("DELETE FROM mem_turn_images WHERE turn_id IN (SELECT id FROM mem_turns WHERE scope=? AND cid=?)", (scope, cid))
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ("mem_chats", "mem_turns", "mem_turn_aliases", "mem_images", "mem_batches", "mem_snapshots", "mem_results", "mem_vectors"):
        if table in tables:
            db.execute(f"DELETE FROM {table} WHERE scope=? AND cid=?", (scope, cid))


def _obsolete_history(db, scope, cid, archive_id):
    # Keep immutable delivered text and deduplication fingerprints, but block
    # old callbacks (including legacy rows without generation) from resurrecting
    # their history into the replaced chat baseline.
    rows = db.execute("SELECT id,data,revision FROM records WHERE kind='proactive_message' "
                      "AND json_extract(data,'$.scope')=? AND json_extract(data,'$.cid')=?", (scope, cid))
    for row in rows:
        value = json.loads(row["data"])
        value["restore_obsolete"] = archive_id
        db.execute("UPDATE records SET data=?,revision=revision+1 WHERE kind='proactive_message' AND id=?", (_json(value), row["id"]))
    for row in db.execute("SELECT id,data FROM jobs WHERE scope=? "
                          "AND json_extract(data,'$.conversation_id')=?", (scope, cid)):
        value = json.loads(row["data"])
        value["restore_obsolete"] = archive_id
        db.execute("UPDATE jobs SET data=? WHERE id=?", (_json(value), row["id"]))


def _connection_signature(value):
    return tuple(value.get(key, "") for key in ("type", "base_url", "model", "dimensions"))


def _source_model_view(incoming):
    from .memory.models import MemoryModels, MODELS
    # Read-only routing uses the same business resolver, without constructing a
    # model store. Mirror only private()'s historical summary-mode normalization.
    value = copy.deepcopy(incoming)
    summary = value.setdefault("summary", {})
    if summary and "mode" not in summary:
        if not summary.get("custom"):
            summary.update(value.get("shared", {}))
        summary.update(mode="custom", custom=True)
    summary.setdefault("mode", "current")
    value.setdefault("assistant", {}).setdefault("mode", "summary")
    value.setdefault("shared", {})
    for role, model in MODELS.items():
        value.setdefault(role, {}).setdefault("model", model)
    view = object.__new__(MemoryModels)
    view.private = lambda: value
    return view, value


def _add_model(target, presets, imported, mapping, source_id, kind):
    identifier = imported["id"]
    saved = next((p for p in presets if p["id"] == identifier), None)
    origin = {"archive_id": mapping["archive_id"], "source_id": source_id, "kind": kind,
              "target_id": identifier, "signature": list(_connection_signature(imported))}
    if saved is not None:
        record = _target(target, "restore_connection", identifier)
        _need(record is not None and _data(record) == origin
              and _connection_signature(saved) == _connection_signature(imported), "RESTORE_DUPLICATE_OBJECT")
        # A later archive binding may reuse a key the user supplied to this
        # exact imported connection. Names and arbitrary target IDs prove none
        # of that; provenance survives normal model-editor saves separately.
        return
    presets.append(imported)
    _save_record(target, "restore_connection", identifier, origin, 1)


def _import_models(source, target, mapping):
    rows = list(_rows(source, "memory_model_settings"))
    if not rows or not (any(item["mode"] != "archive" for item in mapping["chats"]) or mapping["settings"] == "replace"):
        return {}
    incoming = _data(rows[0])
    view, source_config = _source_model_view(incoming)
    needed, legacy_roles = set(), set()
    for chat in mapping["chats"]:
        if chat["mode"] == "archive":
            continue
        settings = json.loads(_get(source, "mem_chats", chat_key(chat["source_scope"], chat["source_cid"]))["settings"])
        for role in ("summary", "assistant", "embedding", "rerank"):
            binding = view.selection(role, settings)
            if binding["mode"] == "preset":
                needed.add(binding["preset_id"])
            elif binding["mode"] == "legacy":
                legacy_roles.add(role)
                if source_config.get(role, {}).get("preset_id"):
                    needed.add(source_config[role]["preset_id"])
    current, revision = _models(target)
    current = copy.deepcopy(current)
    presets = current.setdefault("presets", [])
    aliases = {}
    explicit = mapping["connections"].get("presets", {})
    _need(isinstance(explicit, dict))
    for item in incoming.get("presets", []):
        old = item["id"]
        if mapping["settings"] != "replace" and old not in needed:
            continue
        reuse = explicit.get(old)
        if reuse:
            found = next((p for p in presets if p["id"] == reuse), None)
            _need(found is not None and _connection_signature(found) == _connection_signature(item), "RESTORE_CONNECTION_MISMATCH")
            aliases[old] = reuse
            continue
        identifier = _identifier(mapping["archive_id"], "model", old)
        imported = {**copy.deepcopy(item), "id": identifier}
        aliases[old] = identifier
        _add_model(target, presets, imported, mapping, old, "preset")
    # Legacy global purpose definitions become private named connections for
    # imported chats. Existing chats retain their global definitions/secrets.
    legacy = {}
    for role in sorted(legacy_roles):
        config = source_config.get(role, {})
        if config.get("preset_id") in aliases:
            legacy[role] = aliases[config["preset_id"]]
        elif config:
            identifier = _identifier(mapping["archive_id"], "model-role", role)
            config.setdefault("model", "")
            merged = copy.deepcopy(view._legacy_config(role, source_config))
            merged.update(id=identifier, name="迁移连接 · " + role, type=role if role in ("embedding", "rerank") else "chat")
            _add_model(target, presets, merged, mapping, role, "legacy-role")
            legacy[role] = identifier
    if mapping["settings"] == "replace":
        for role in ("shared", "summary", "assistant", "rewrite", "embedding", "rerank"):
            if role in incoming:
                current[role] = copy.deepcopy(incoming[role])
                if current[role].get("preset_id"):
                    current[role]["preset_id"] = aliases.get(current[role]["preset_id"], "")
    _need(len(presets) <= 24, "RESTORE_CONNECTION_LIMIT")
    target.execute("INSERT OR REPLACE INTO memory_model_settings(id,revision,data) VALUES(1,?,?)", (revision + 1, _json(current)))
    return {"aliases": aliases, "legacy": legacy, "source_config": source_config}


def _map_bindings(settings, connections):
    result = copy.deepcopy(settings)
    bindings = result.setdefault("model_bindings", {})
    for role, value in bindings.items():
        if value.get("preset_id"):
            value["preset_id"] = connections.get("aliases", {}).get(value["preset_id"], "")
    view, _ = _source_model_view(connections.get("source_config", {}))
    for role in ("summary", "assistant", "embedding", "rerank"):
        selection = view.selection(role, settings)
        identifier = connections.get("legacy", {}).get(role)
        if selection["mode"] == "legacy" and identifier:
            bindings[role] = {"mode": "preset", "preset_id": identifier}
        elif role == "summary" and selection["mode"] == "current":
            # A source legacy "current" must not resolve through the target's
            # unrelated global custom connection after a memory-only import.
            result["summary_provider_id"] = ""
            bindings.pop("summary", None)
        elif role not in settings.get("model_bindings", {}) and selection["mode"] in ("disabled", "summary", "embedding", "host"):
            bindings[role] = selection
    return result


def _apply_memory(source, target, mapping, connections, pending, cancel):
    for item in mapping["chats"]:
        if item["mode"] == "archive":
            continue
        check_cancel(cancel)
        owner = chat_key(item["source_scope"], item["source_cid"])
        scope, cid = item["target_scope"], item["target_cid"]
        row = _get(source, "mem_chats", owner)
        original = item["mode"] == "original"
        def batch(old):
            return _mapped_id(source, "mem_batches", owner, old)

        def turn(old):
            return _mapped_id(source, "mem_turns", owner, old)
        _obsolete_history(target, scope, cid, mapping["archive_id"])
        _remove_chat(target, scope, cid)
        settings = _map_bindings(json.loads(row["settings"]), connections)
        memory = map_memory(json.loads(row["memory"]), batch, baseline=not original)
        new_row = {**row, "scope": scope, "cid": cid, "enabled": 0, "revision": item["revision"], "generation": item["generation"],
                   "settings": _json(settings), "memory": _json(memory), "next_ordinal": row["next_ordinal"] if original else 1}
        _insert(target, "mem_chats", new_row)
        if original:
            for table in ("mem_turns", "mem_batches", "mem_images", "mem_turn_aliases", "mem_snapshots"):
                for saved in _rows(source, table, owner):
                    check_cancel(cancel)
                    value = {**saved, "scope": scope, "cid": cid}
                    if table == "mem_turns":
                        value.update(id=turn(saved["id"]), batch=batch(saved["batch"]))
                    elif table == "mem_batches":
                        value.update(id=batch(saved["id"]), diff=_json(map_diff(json.loads(saved["diff"]), batch)))
                    elif table == "mem_turn_aliases":
                        value["turn_id"] = turn(saved["turn_id"])
                    elif table == "mem_snapshots":
                        state = json.loads(saved["turns"])
                        state["turns"] = [[turn(t), status, batch(b)] for t, status, b in state["turns"]]
                        state["batches"] = [[batch(b), status] for b, status in state["batches"]]
                        value.update(id=_snapshot_id(mapping, item, saved["id"]),
                                     memory=_json(map_memory(json.loads(saved["memory"]), batch)), turns=_json(state))
                    _insert(target, table, value)
            for saved in _rows(source, "mem_turn_images"):
                source_turn = _get(source, "mem_turns", saved["turn_id"])
                if source_turn and chat_key(source_turn["scope"], source_turn["cid"]) == owner:
                    _insert(target, "mem_turn_images", {**saved, "turn_id": turn(saved["turn_id"])})
        pending.append({"kind": "memory", "id": chat_key(scope, cid), "scope": scope, "cid": cid,
                        "intent": {"enabled": bool(row["enabled"]), "mode": item["mode"], "source_scope": row["scope"],
                                   "source_cid": row["cid"], "retention_days": settings.get("retention_days", 30),
                                   "retention": item["retention"],
                                   "settings": settings,
                                   "source_archive": mapping["archive_id"], "host_fingerprint": item["host_fingerprint"]}})


def _key_slots(value, archive_id, owner):
    value = copy.deepcopy(value)
    slots = {item["id"]: _identifier(archive_id, "key", [owner, item["id"]]) for item in value.get("api_keys", [])}
    for item in value.get("api_keys", []):
        item["id"] = slots[item["id"]]
    if value.get("active_key_id"):
        value["active_key_id"] = slots.get(value["active_key_id"], "")
    return value


def _apply_settings(source, current, mapping, pending):
    if mapping["settings"] != "replace":
        return copy.deepcopy(current)
    rows = list(_rows(source, "settings"))
    _need(len(rows) == 1)
    incoming = _data(rows[0])
    value = _key_slots(incoming, mapping["archive_id"], "primary")
    reuse = mapping["connections"].get("providers", {})
    current_providers = {"primary": current, **{p["id"]: p for p in current.get("providers", [])}}
    source_providers = {"primary": incoming, **{p["id"]: p for p in incoming.get("providers", [])}}
    for old, new in reuse.items():
        _need(old in source_providers and new in current_providers, "RESTORE_CONNECTION_MISMATCH")
        expected, actual = source_providers[old], current_providers[new]
        _need(all(expected.get(field, "") == actual.get(field, "") for field in ("base_url", "model", "reference_field")),
              "RESTORE_CONNECTION_MISMATCH")
    _need(not reuse.get("primary") or reuse["primary"] == "primary", "RESTORE_CONNECTION_MISMATCH")
    if reuse.get("primary"):
        for field in ("api_key", "api_keys", "active_key_id"):
            if field in current:
                value[field] = copy.deepcopy(current[field])
    providers = {"primary": "primary"}
    for item in value.get("providers", []):
        providers[item["id"]] = reuse.get(item["id"]) or _identifier(mapping["archive_id"], "provider", item["id"])
    _need(len(set(providers.values())) == len(providers), "RESTORE_CONNECTION_COLLISION")
    rewritten = []
    for item in value.get("providers", []):
        old = item["id"]
        item = _key_slots(item, mapping["archive_id"], old)
        if old in reuse:
            for field in ("api_key", "api_keys", "active_key_id"):
                if field in current_providers[reuse[old]]:
                    item[field] = copy.deepcopy(current_providers[reuse[old]][field])
        item["id"] = providers[old]
        rewritten.append(item)
    value["providers"] = rewritten
    value["provider_order"] = [providers[p] for p in value.get("provider_order", []) if p in providers]
    value["preferred_provider"] = providers.get(value.get("preferred_provider"), "primary")
    value["settings_revision"] = int(current.get("settings_revision", 0)) + 1
    intent = {k: value.get(k, False) for k in ("qzone_enabled", "generated_qzone_allowed", "primary_enabled")}
    intent["providers"] = {p["id"]: p.get("enabled", True) for p in value["providers"]}
    for field in intent:
        if field != "providers":
            value[field] = False
    for item in value["providers"]:
        item["enabled"] = False
    pending.append({"kind": "settings", "id": "global", "intent": intent})
    return value


def _apply_reference(source, target, mapping, pending, media, cancel):
    chars = {item["source_id"]: item for item in mapping["characters"]}
    _need(not character_binding_conflicts(target, {item["target_id"]: item["sessions"] for item in chars.values()}),
          "SESSION_BINDING_CONFLICT")
    assets = {}
    for row in _rows(source, "reference_assets"):
        value = _data(row)
        character = chars.get(value["character_id"])
        if character is None:
            continue
        check_cancel(cancel)
        identifier = _identifier(mapping["archive_id"], "asset", row["id"])
        _need(_target(target, "asset", identifier) is None, "RESTORE_DUPLICATE_OBJECT")
        assets[row["id"]] = identifier
        digest = value["hash"]
        paths = {"original": f"originals/{digest}.png", "delivery": f"delivery/{digest}.jpg", "thumbnail": f"thumbnails/{digest}.jpg"}
        value.update(character_id=character["target_id"], scope=mapping["scopes"].get(value.get("scope", ""), ""), **paths)
        _save_record(target, "asset", identifier, value, 1)
        if not any(item["source"] == f"media/{digest}.png" for item in media):
            media.append({"source": f"media/{digest}.png", **paths})
    for old, item in chars.items():
        row = _get(source, "characters", old)
        value = _data(row)
        versions = {version["id"]: _identifier(mapping["archive_id"], "version", [old, version["id"]]) for version in value.get("versions", [])}
        for version in value.get("versions", []):
            version["id"] = versions[version["id"]]
            version["asset_ids"] = [assets[identifier] for identifier in version.get("asset_ids", [])]
        if value.get("active_version"):
            value["active_version"] = versions[value["active_version"]]
        sessions = item["sessions"]
        value["sessions"] = []
        value["restore_origin"] = {"archive_id": mapping["archive_id"], "id": old, "revision": row["revision"]}
        _save_record(target, "character", item["target_id"], value, item["revision"])
        pending.append({"kind": "character", "id": item["target_id"], "intent": {"sessions": sessions}})


def _calendar_connections(value, target_value, mapping):
    schemes = target_value.setdefault("search_schemes", [])
    aliases = {}
    reuse = mapping["connections"].get("search_schemes", {})
    for item in value.get("search_schemes", []):
        old = item.get("id", "")
        if old in reuse:
            found = next((s for s in schemes if s.get("id") == reuse[old]), None)
            _need(found is not None and all(found.get(field, "") == item.get(field, "") for field in ("api_url", "model")),
                  "RESTORE_CONNECTION_MISMATCH")
            aliases[old] = reuse[old]
            continue
        identifier = _identifier(mapping["archive_id"], "calendar-search", old)
        # The B schema deliberately permits historical duplicate scheme IDs.
        aliases[old] = identifier
        schemes.append({**copy.deepcopy(item), "id": identifier})
    if value.get("active_search_scheme"):
        target_value["active_search_scheme"] = aliases.get(value["active_search_scheme"], "")


def _apply_life(source, target, mapping, pending, cancel):
    if not mapping["plans"] and not mapping["calendar"] and mapping["settings"] != "replace":
        return
    plans = {item["source_id"]: item for item in mapping["plans"]}
    characters = {**mapping["character_links"], **{item["source_id"]: item["target_id"] for item in mapping["characters"]}}
    scopes = mapping["scopes"]
    chat_dest = {(item["source_scope"], item["source_cid"]): (item["target_scope"], item["target_cid"])
                 for item in mapping["chats"] if item["mode"] != "archive"}
    # Life-only imports may explicitly map a source scope/cid without importing
    # memory. CID mappings are still exact pairs, never same-name inference.
    for item in mapping.get("life_chats", []):
        chat_dest[(item["source_scope"], item["source_cid"])] = (item["target_scope"], item["target_cid"])
    plan_pending = {}
    for old, item in plans.items():
        check_cancel(cancel)
        row = _get(source, "life_plans", old)
        value = _data(row)
        original = {"archive_id": mapping["archive_id"], "id": old, "revision": row["revision"],
                    "scope": value.get("scope", ""), "origin_cid": value.get("origin_cid", "")}
        wanted = bool(value.get("enabled", False))
        mapped_chat = chat_dest.get((value.get("scope"), value.get("origin_cid")))
        needs_cid = bool(value.get("origin_cid")) or bool(value.get("run_once") and value.get("source") in ("chat", "chain"))
        if mapped_chat:
            _need(mapped_chat[0] == scopes.get(value.get("scope"), ""), "RESTORE_SCOPE_MISMATCH")
        value.update(scope=scopes.get(value.get("scope"), ""), character_id=characters.get(value.get("character_id"), ""),
                     enabled=False, activated_at=0, restore_origin=original)
        if value.get("origin_cid"):
            value["origin_cid"] = mapped_chat[1] if mapped_chat else ""
        if item["action"] == "replace":
            target.execute("DELETE FROM records WHERE kind='life_adjustment' AND json_extract(data,'$.plan_id')=?", (item["target_id"],))
        _save_record(target, "life_plan", item["target_id"], value, item["revision"])
        entry = {"kind": "life_plan", "id": item["target_id"], "scope": value["scope"],
                 "intent": {"enabled": wanted, "run_once": bool(value.get("run_once")), "trigger_at": value.get("trigger_at", 0),
                            "adjustment_ids": [], "requires_future_confirmation": bool(value.get("run_once")),
                            "missing_binding": not value["scope"] or not value["character_id"] or needs_cid and not value.get("origin_cid")}}
        if item.get("adjustment_conflict"):
            entry["intent"]["adjustment_conflict"] = True
        plan_pending[old] = entry
        pending.append(entry)
    source_ids, evidence_owners = set(), set()
    for row in _rows(source, "life_adjustments"):
        value = _data(row)
        item = plans.get(value.get("plan_id"))
        if item is None:
            continue
        old_plan = value["plan_id"]
        evidence_owners.add((value.get("scope"), value.get("cid")))
        destination = chat_dest.get((value.get("scope"), value.get("cid")))
        original = {"archive_id": mapping["archive_id"], "id": row["id"], "revision": row["revision"],
                    "plan_revision": value.get("plan_revision"), "source_ids": value.get("source_ids", []),
                    "evidence_revision": value.get("evidence_revision"), "explicit": value.get("explicit", False)}
        source_ids.update(value.get("source_ids", []))
        identifier = _identifier(mapping["archive_id"], "adjustment", row["id"])
        value.update(plan_id=item["target_id"], plan_revision=item["revision"],
                     character_id=characters.get(value.get("character_id"), ""), scope=destination[0] if destination else scopes.get(value.get("scope"), ""),
                     cid=destination[1] if destination else "", restore_origin=original)
        value["source_ids"] = [_identifier(mapping["archive_id"], "evidence-source", old) for old in value.get("source_ids", [])]
        matching = source.execute("SELECT count(*) FROM source WHERE name='life_adjustments' "
            "AND json_extract(payload,'$.data') IS NOT NULL AND json_extract(json_extract(payload,'$.data'),'$.plan_id')=? "
            "AND json_extract(json_extract(payload,'$.data'),'$.scope')=? AND json_extract(json_extract(payload,'$.data'),'$.cid')=? "
            "AND json_extract(json_extract(payload,'$.data'),'$.anchor_ts')=?",
            (old_plan, original.get("scope", _data(row).get("scope")), _data(row).get("cid"), value.get("anchor_ts"))).fetchone()[0]
        if destination and matching == 1:
            from .daily import _hash as occurrence_hash
            identifier = occurrence_hash([destination[0], destination[1], f"{item['target_id']}:{value['anchor_ts']}"])
        elif matching > 1:
            plan_pending[old_plan]["intent"]["adjustment_conflict"] = True
            plan_pending[old_plan]["intent"]["missing_binding"] = True
        # It must stay blocked until the activation screen confirms the future
        # occurrence and rebinds evidence/revision. Never invent a live cursor.
        value["explicit"] = False
        _save_record(target, "life_adjustment", identifier, value, 1)
        plan_pending[old_plan]["intent"]["adjustment_ids"].append(identifier)
        plan_pending[old_plan]["intent"]["requires_future_confirmation"] = True
        if destination is None:
            plan_pending[old_plan]["intent"]["missing_binding"] = True
    for row in _rows(source, "life_sources"):
        if row["id"] not in source_ids:
            continue
        value = _data(row)
        destination = chat_dest.get((value.get("scope"), value.get("cid")))
        value.update(scope=destination[0] if destination else "", cid=destination[1] if destination else "")
        _save_record(target, "restore_source", _identifier(mapping["archive_id"], "evidence-source", row["id"]), value, 1)
    for row in _rows(source, "life_evidence_metadata"):
        value = _data(row)
        if (value.get("scope"), value.get("cid")) not in evidence_owners:
            continue
        destination = chat_dest.get((value.get("scope"), value.get("cid")))
        if destination:
            value.update(scope=destination[0], cid=destination[1], source_revision=row["revision"])
            if value.get("character_id"):
                value["character_id"] = characters.get(value["character_id"], "")
            _save_record(target, "restore_evidence", _identifier(mapping["archive_id"], "cursor", row["id"]), value, 1)
    if mapping["calendar"] or mapping["settings"] == "replace":
        current_row = _target(target, "calendar", "default")
        current = _data(current_row) if current_row else {}
        calendar_scope = _calendar_scope(source, current, mapping)
        for table in ("calendar", "calendar_connections"):
            for row in _rows(source, table):
                incoming = _data(row)
                if mapping["calendar"] and table == "calendar":
                    # Public calendar replaces only when explicitly selected;
                    # personal/trip rows are restricted to mapped scopes.
                    for field in ("holidays", "workdays", "custom_days", "school_terms", "holiday_ranges", "workday_ranges"):
                        if field in incoming:
                            current[field] = copy.deepcopy(incoming[field])
                    personal = current.setdefault("personal_calendars", {})
                    for scope, value in incoming.get("personal_calendars", {}).items():
                        if scope in scopes:
                            personal[scopes[scope]] = copy.deepcopy(value)
                    current["trips"] = _calendar_trips(incoming.get("trips", []), current.get("trips", []), mapping)
                _calendar_connections(incoming, current, mapping)
        if current:
            # Existing target plans must keep their current day profile until
            # the imported calendar is explicitly checked and enabled.
            pending.append({"kind": "calendar", "id": "default", "intent": {
                "calendar_payload": current, "base_revision": current_row["revision"] if current_row else 0,
                "calendar_scope": calendar_scope}})


def apply(checked, manifest, target_db, target_settings, mapping, *, cancel=None):
    """Write selected rows in caller transaction; return private commit inputs."""
    source = _index(checked, manifest, cancel)
    try:
        token = source.execute("SELECT value FROM meta WHERE key='plan'").fetchone()
        _need(token and token[0] == mapping["plan_id"], "RESTORE_PREVIEW_STALE")
        pending, media = [], []
        connections = _import_models(source, target_db, mapping)
        _apply_memory(source, target_db, mapping, connections, pending, cancel)
        _apply_reference(source, target_db, mapping, pending, media, cancel)
        _apply_life(source, target_db, mapping, pending, cancel)
        settings = _apply_settings(source, target_settings, mapping, pending)
        # Missing source references reserve IDs without creating fake turns or
        # batches. Future real AUTOINCREMENT writes must never claim these IDs.
        for table in ("mem_turns", "mem_batches"):
            high = source.execute("SELECT coalesce(max(new),0) FROM ids WHERE kind=?", (table,)).fetchone()[0]
            current = target_db.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone()
            if high and current:
                target_db.execute("UPDATE sqlite_sequence SET seq=max(seq,?) WHERE name=?", (high, table))
            elif high:
                target_db.execute("INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)", (table, high))
        for item in pending:
            item["target_id"] = item["id"]
        return {"settings": settings, "media": media, "pending": pending}
    finally:
        source.close()
