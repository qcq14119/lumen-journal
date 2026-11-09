"""Crash recovery for the target database, settings and newly installed media.

The source archive is never a database replacement. Only a verified backup of
the *target*, captured while maintenance is held, may be restored by this log.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import uuid

from .backup_format import BackupError, CHUNK_BYTES, ensure_space

MAX_WORK_BYTES = 3 * 1024 ** 3
MAX_TARGET_BYTES = 1024 ** 3


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while block := handle.read(CHUNK_BYTES):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path, value):
    atomic_bytes(path, json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())


def atomic_bytes(path, raw):
    path = Path(path)
    temporary = path.with_name(path.name + ".writing")
    with temporary.open("wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    # Directory fsync is unavailable on Windows; file flush + replace remain
    # the strongest portable primitives exposed by Python on that platform.
    if os.name != "nt":
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def private_root(root):
    path = Path(root) / "restore-private"
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def fingerprint(db, settings_path):
    """Streaming logical target version, stable across WAL/checkpoint/restart."""
    digest = hashlib.sha256()
    for name, schema in db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"):
        digest.update(json.dumps([name, schema], ensure_ascii=False).encode())
        escaped = name.replace('"', '""')
        for row in db.execute(f'SELECT * FROM "{escaped}" ORDER BY rowid'):
            for value in row:
                raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
                digest.update(str(len(raw)).encode() + b":" + raw)
    settings_path = Path(settings_path)
    digest.update(b"settings:" + (digest_file(settings_path).encode() if settings_path.exists() else b"absent"))
    return digest.hexdigest()


def budget(root, additional):
    """Account for private archives, journals, staging and derived media together."""
    used = 0
    for base in (Path(root) / "restore-private", Path(root) / "tmp"):
        if base.exists():
            for path in base.rglob("*"):
                if path.is_symlink():
                    raise BackupError("PRIVATE_PATH")
                if path.is_file():
                    used += path.stat().st_size
    if used + additional > MAX_WORK_BYTES:
        raise BackupError("RESTORE_DISK_LIMIT")
    ensure_space(Path(root), additional)


def _paths(root, journal):
    identifier = journal.get("id", "")
    if not isinstance(identifier, str) or len(identifier) != 32 or any(c not in "0123456789abcdef" for c in identifier):
        raise BackupError("RECOVERY_REQUIRED")
    folder = private_root(root) / ("transaction-" + identifier)
    for path in (folder, folder / "before.sqlite3", folder / "before.settings"):
        if path.is_symlink():
            raise BackupError("RECOVERY_REQUIRED")
    return folder


def load(root, *, rollback_copy=False):
    if rollback_copy:
        current = load(root)
        if current and current["state"] == "committed":
            if current.get("operation", "restore") == "restore":
                return current
            if current.get("previous_restore"):
                _paths(root, current["previous_restore"])
                return current["previous_restore"]
        if current and current["state"] == "rolled_back" and "rollback_versions" in current:
            return current
    path = Path(root) / "restore-private" / ("rollback.json" if rollback_copy else "journal.json")
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_size > 1024 * 1024:
        raise BackupError("RECOVERY_REQUIRED")
    try:
        value = json.loads(path.read_bytes())
        if not isinstance(value, dict) or value.get("version") != 1:
            raise ValueError()
        _paths(root, value)
        return value
    except (ValueError, OSError) as exc:
        raise BackupError("RECOVERY_REQUIRED") from exc


def write(root, journal, state, **values):
    journal.update(values, state=state)
    atomic_json(private_root(root) / "journal.json", journal)


def prepare(store, *, archive_id, preview_id, new_media, affected=(), operation="restore"):
    root = store.root
    # Preserve the last authoritative rollback pointer before replacing the
    # only commit marker. Failure here occurs before any new business write.
    previous = load(root, rollback_copy=True)
    if previous:
        atomic_json(private_root(root) / "rollback.json", previous)
    size = store.db.execute("PRAGMA page_count").fetchone()[0] * store.db.execute("PRAGMA page_size").fetchone()[0]
    if size > MAX_TARGET_BYTES:
        raise BackupError("RESTORE_TARGET_LIMIT")
    budget(root, size * 2 + 16 * CHUNK_BYTES)
    identifier = uuid.uuid4().hex
    folder = private_root(root) / ("transaction-" + identifier)
    folder.mkdir(mode=0o700)
    before = folder / "before.sqlite3"
    with closing(sqlite3.connect(before)) as saved:
        store.db.backup(saved, pages=256)
        if saved.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise BackupError("ROLLBACK_COPY_INVALID")
    settings_exists = store.settings_path.exists()
    atomic_bytes(folder / "before.settings", store.settings_path.read_bytes() if settings_exists else b"")
    journal = {"version": 1, "id": identifier, "archive_id": archive_id, "operation": operation,
               "preview_id": preview_id, "settings_exists": settings_exists,
               "database_hash": digest_file(before), "settings_hash": digest_file(folder / "before.settings"),
               "media": new_media, "affected": list(affected), "before_fingerprint": fingerprint(store.db, store.settings_path)}
    write(root, journal, "prepared")
    return journal


def _restore(root, journal, db):
    folder = _paths(root, journal)
    before, settings = folder / "before.sqlite3", folder / "before.settings"
    if digest_file(before) != journal["database_hash"] or digest_file(settings) != journal["settings_hash"]:
        raise BackupError("ROLLBACK_COPY_INVALID")
    with closing(sqlite3.connect(before.resolve().as_uri() + "?mode=ro", uri=True)) as saved:
        if saved.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise BackupError("ROLLBACK_COPY_INVALID")
        if db.in_transaction:
            db.rollback()
        saved.backup(db, pages=256)
    destination = Path(root) / "settings.json"
    if journal["settings_exists"]:
        atomic_bytes(destination, settings.read_bytes())
    elif destination.exists():
        destination.unlink()
    # Only additions made by this transaction can be removed. Existing files
    # were never replaced; user and unselected album media remain untouched.
    for item in journal.get("media", []):
        relative = item["path"]
        path = Path(relative)
        if path.is_absolute() or len(path.parts) != 2 or path.parts[0] not in ("originals", "delivery", "thumbnails") or ".." in path.parts:
            raise BackupError("RECOVERY_REQUIRED")
        destination = Path(root) / path
        if destination.exists():
            if destination.is_symlink() or digest_file(destination) != item["hash"]:
                raise BackupError("RECOVERY_REQUIRED")
            destination.unlink()


def rollback(store, journal, *, advance=False):
    versions = []
    if advance:
        for item in journal.get("affected", []):
            if item["kind"] == "memory":
                row = store.db.execute("SELECT revision,generation FROM mem_chats WHERE scope=? AND cid=?", (item["scope"], item["cid"])).fetchone()
                versions.append({**item, "revision": row[0] if row else 0, "generation": row[1] if row else 0})
            else:
                row = store.db.execute("SELECT revision FROM records WHERE kind=? AND id=?", (item["kind"], item["target_id"])).fetchone()
                versions.append({**item, "revision": row[0] if row else 0})
        journal["rollback_versions"] = versions
        journal["rollback_settings_revision"] = store.settings.get("settings_revision", 0)
    write(store.root, journal, "rolling_back")
    _restore(store.root, journal, store.db)
    from .store import DEFAULT_SETTINGS
    store.settings = dict(DEFAULT_SETTINGS)
    if store.settings_path.exists():
        store.settings.update(json.loads(store.settings_path.read_bytes()))
    _advance_versions(store.root, journal, store.db)
    if journal.get("rollback_versions"):
        store.settings = {**DEFAULT_SETTINGS, **json.loads(store.settings_path.read_bytes())}
    write(store.root, journal, "rolled_back")
    if advance:
        atomic_json(private_root(store.root) / "rollback.json", journal)


def _advance_versions(root, journal, db):
    """Idempotent high-water marks survive a crash while manually rolling back."""
    if "rollback_versions" not in journal:
        return
    with db:
        for value in journal["rollback_versions"]:
            if value["kind"] == "memory":
                generation = value["generation"] + 1
                key = hashlib.sha256(json.dumps([value["scope"], value["cid"]], ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
                payload = {"scope": value["scope"], "cid": value["cid"], "generation": generation}
                db.execute("INSERT OR REPLACE INTO records VALUES('restore_epoch',?,?,1)", (key, json.dumps(payload)))
                db.execute("UPDATE mem_chats SET revision=max(revision,?)+1,generation=max(generation,?) WHERE scope=? AND cid=?",
                    (value["revision"], generation, value["scope"], value["cid"]))
            else:
                old_plan = db.execute("SELECT data,revision FROM records WHERE kind='life_plan' AND id=?",
                    (value["target_id"],)).fetchone() if value["kind"] == "life_plan" else None
                db.execute("UPDATE records SET revision=max(revision,?)+1 WHERE kind=? AND id=?",
                    (value["revision"], value["kind"], value["target_id"]))
                if old_plan:
                    payload = json.loads(old_plan[0])
                    revision = max(old_plan[1], value["revision"]) + 1
                    # Only rebind facts which actually belonged to the restored
                    # pre-image. A stale or foreign adjustment stays untrusted.
                    db.execute("UPDATE records SET data=json_set(data,'$.plan_revision',?),revision=revision+1 "
                        "WHERE kind='life_adjustment' AND json_extract(data,'$.plan_id')=? "
                        "AND json_extract(data,'$.plan_revision')=? AND json_extract(data,'$.scope')=? "
                        "AND json_extract(data,'$.character_id')=?",
                        (revision, value["target_id"], old_plan[1], payload.get("scope"), payload.get("character_id")))
    settings_path = Path(root) / "settings.json"
    settings = json.loads(settings_path.read_bytes()) if settings_path.exists() else {}
    settings["settings_revision"] = max(settings.get("settings_revision", 0), journal.get("rollback_settings_revision", 0)) + 1
    atomic_json(settings_path, settings)


def _recover_transaction(root):
    """Repair only the durable cross-medium transaction."""
    journal = load(root)
    if not journal:
        return
    try:
        if journal["state"] == "committed":
            if journal.get("operation", "restore") == "restore":
                atomic_json(private_root(root) / "rollback.json", journal)
            elif journal.get("previous_restore"):
                atomic_json(private_root(root) / "rollback.json", journal["previous_restore"])
            return
        if journal["state"] == "rolled_back":
            # A process can stop after the terminal journal marker but before
            # updating the separately retained manual-rollback pointer.
            if "rollback_versions" in journal:
                atomic_json(private_root(root) / "rollback.json", journal)
            return
        if journal["state"] == "prepared":
            write(root, journal, "rolled_back")
            return
        if journal["state"] not in ("applying", "rolling_back"):
            raise BackupError("RECOVERY_REQUIRED")
        with closing(sqlite3.connect(Path(root) / "album.sqlite3")) as db:
            _restore(root, journal, db)
            _advance_versions(root, journal, db)
        write(root, journal, "rolled_back")
        if "rollback_versions" in journal:
            atomic_json(private_root(root) / "rollback.json", journal)
    except Exception as exc:
        # Do not start a half-restored Store or remove the sole recovery copy.
        raise BackupError("RECOVERY_REQUIRED") from exc


def _cleanup_abandoned(root):
    """Startup only: remove this component's interrupted, unauthenticated work."""
    root = Path(root).resolve()
    for base, archive_writes in ((root / "tmp", False), (root / "restore-private" / "archives", True)):
        if not base.exists():
            continue
        if base.is_symlink() or not base.resolve().is_relative_to(root):
            raise BackupError("RECOVERY_REQUIRED")
        for path in base.iterdir():
            if archive_writes:
                suffix = path.name.removeprefix("writing-")
                owned = path.name.startswith("writing-") and len(suffix) == 32 and all(c in "0123456789abcdef" for c in suffix)
            else:
                owned = path.name.startswith("backup-")
            if not owned:
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(base.resolve()):
                raise BackupError("RECOVERY_REQUIRED")
            if path.is_dir():
                shutil.rmtree(path)


def recover(root):
    """Called by Store once, before opening business state or starting workers."""
    _recover_transaction(root)
    try:
        _cleanup_abandoned(root)
    except Exception as exc:
        # Interrupted plaintext must not remain indefinitely after restart;
        # cleanup failure blocks startup without touching rollback or archives.
        raise BackupError("RECOVERY_REQUIRED") from exc


def prune_previous(root, keep):
    """Keep both current crash recovery and the original manual rollback copy."""
    current, last = load(root), load(root, rollback_copy=True)
    kept = {"transaction-" + keep}
    for value in (current, last, (current or {}).get("previous_restore")):
        if value:
            kept.add(_paths(root, value).name)
    for path in private_root(root).glob("transaction-*"):
        if path.name not in kept and path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
