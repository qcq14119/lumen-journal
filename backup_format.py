"""Versioned, bounded backup containers. Never opens a business database.

The encrypted envelope authenticates its entire fixed header and payload before
the ZIP or JSON parser sees plaintext. Temporary plaintext lives only in the
caller's private task directory and is removed on success, failure or cancel.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import struct
import uuid
import warnings
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from PIL import Image, UnidentifiedImageError

FORMAT = "companion-plugin-backup"
SCHEMA_VERSION = 2
MAGIC = b"CJBACKUP"
ENVELOPE_VERSION = 1
KDF_ITERATIONS = 600_000
HEADER = struct.Struct(">8sBI16s12s")
TAG_BYTES = 16
CHUNK_BYTES = 1024 * 1024
MAX_EXPANDED_BYTES = 512 * CHUNK_BYTES
MAX_INPUT_BYTES = 600 * CHUNK_BYTES
MAX_FILE_BYTES = 64 * CHUNK_BYTES
MAX_RECORD_BYTES = 8 * CHUNK_BYTES
MAX_MANIFEST_BYTES = 8 * CHUNK_BYTES
MAX_FILES = 4096
MAX_DEPTH = 32
MAX_PIXELS = 40_000_000
MAX_COMPRESSION_RATIO = 200
DISK_RESERVE_BYTES = 64 * CHUNK_BYTES
MODULES = frozenset({"reference", "life", "memory", "settings"})
METADATA_KEYS = frozenset({
    "modules", "dependencies", "counts", "source", "omissions", "limitations",
    "credentials_included", "mapping_contract",
    "inventory", "source_integrity", "credentials_omitted",
})
MANIFEST_KEYS = METADATA_KEYS | {
    "format", "schema_version", "plugin_version", "created_utc", "package_id",
    "entries", "expanded_bytes", "file_count", "encryption", "encrypted",
}
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_COMPONENT = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}\Z")
_DEVICE = re.compile(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?\Z")


class BackupError(ValueError):
    """A fixed public reason code, never a source value or exception string."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def check_cancel(cancel: Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise BackupError("CANCELLED")


def ensure_space(directory: Path, required: int) -> None:
    if required < 0 or shutil.disk_usage(directory).free < required + DISK_RESERVE_BYTES:
        raise BackupError("DISK_SPACE")


def _linked(path: Path) -> bool:
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _name(name: str, *, manifest: bool = False) -> str:
    if manifest and name == "manifest.json":
        return name
    parts = name.split("/")
    if (
        len(parts) < 2 or len(parts) > 8 or parts[0] not in {"data", "media"}
        or any(
            not _COMPONENT.fullmatch(part) or part in {".", ".."}
            or part.endswith(".") or _DEVICE.fullmatch(part)
            for part in parts
        )
    ):
        raise BackupError("ARCHIVE_PATH")
    if parts[0] == "media":
        if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{64}\.png", parts[1]):
            raise BackupError("ARCHIVE_PATH")
    elif not name.endswith((".json", ".jsonl")):
        raise BackupError("ARCHIVE_TYPE")
    return name


def _depth(raw: bytes) -> None:
    depth = 0
    quoted = False
    escaped = False
    for value in raw:
        if quoted:
            if escaped:
                escaped = False
            elif value == 92:
                escaped = True
            elif value == 34:
                quoted = False
        elif value == 34:
            quoted = True
        elif value in (91, 123):
            depth += 1
            if depth > MAX_DEPTH:
                raise BackupError("JSON_DEPTH")
        elif value in (93, 125):
            depth -= 1


def _pairs(items: list[tuple]) -> dict:
    result = {}
    for key, value in items:
        if key in result:
            raise BackupError("JSON_DUPLICATE_KEY")
        result[key] = value
    return result


def _constant(_value: str) -> None:
    raise BackupError("JSON_INVALID")


def parse_json(raw: bytes, limit: int | None = None):
    limit = MAX_RECORD_BYTES if limit is None else limit
    if len(raw) > limit:
        raise BackupError("RECORD_LIMIT")
    _depth(raw)
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError, OverflowError) as exc:
        raise BackupError("JSON_INVALID") from exc


def _validate_file(path: Path, name: str, cancel: Event | None) -> None:
    check_cancel(cancel)
    if name.startswith("media/"):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(path) as picture:
                    if (
                        picture.format != "PNG" or picture.width * picture.height > MAX_PIXELS
                        or max(picture.size) > 16384 or getattr(picture, "n_frames", 1) != 1
                    ):
                        raise BackupError("IMAGE_LIMIT")
                    picture.verify()
        except (
            UnidentifiedImageError, OSError, SyntaxError, ValueError,
            Image.DecompressionBombError, Image.DecompressionBombWarning,
        ) as exc:
            if isinstance(exc, BackupError):
                raise
            raise BackupError("IMAGE_INVALID") from exc
    else:
        with path.open("rb") as stream:
            if name.endswith(".jsonl"):
                while raw := stream.readline(MAX_RECORD_BYTES + 2):
                    check_cancel(cancel)
                    # Every record has exactly one terminating newline. An empty
                    # table is an empty file; blank lines are not records.
                    if not raw.endswith(b"\n"):
                        raise BackupError("JSONL_INVALID")
                    if not isinstance(parse_json(raw[:-1]), dict):
                        raise BackupError("JSONL_INVALID")
            else:
                parse_json(stream.read(MAX_RECORD_BYTES + 1))


def _hash(path: Path, cancel: Event | None) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK_BYTES):
            check_cancel(cancel)
            count += len(chunk)
            if count > MAX_FILE_BYTES:
                raise BackupError("FILE_LIMIT")
            digest.update(chunk)
    return count, digest.hexdigest()


def _version() -> str:
    raw = Path(__file__).with_name("metadata.yaml").read_text(encoding="utf-8")
    match = re.search(r"^version:\s*([0-9]+(?:\.[0-9]+){2})\s*$", raw, re.MULTILINE)
    if not match:
        raise BackupError("PLUGIN_VERSION")
    return match.group(1)


def _manifest(metadata: dict, entries: list[dict], encrypted: bool) -> dict:
    if not isinstance(metadata, dict) or set(metadata) != METADATA_KEYS:
        raise BackupError("MANIFEST_INVALID")
    result = dict(metadata)
    result.update({
        "format": FORMAT, "schema_version": SCHEMA_VERSION, "plugin_version": _version(),
        "created_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "package_id": uuid.uuid4().hex, "entries": entries,
        "expanded_bytes": sum(entry["size"] for entry in entries), "file_count": len(entries),
        "encryption": "aes-256-gcm-pbkdf2-sha256-v1" if encrypted else "none",
        "encrypted": encrypted,
    })
    _check_manifest(result, encrypted)
    return result


def _check_source(source) -> None:
    if (
        not isinstance(source, dict)
        or not {"namespace", "captured_at", "settings_revision"} <= set(source)
        or not isinstance(source["namespace"], str)
        or not re.fullmatch(r"[0-9a-f]{32}", source["namespace"])
        or any(type(source[field]) is not int or not 0 <= source[field] <= 9223372036854775807
               for field in ("captured_at", "settings_revision"))
    ):
        raise BackupError("MANIFEST_INVALID")


def _check_manifest(value: dict, encrypted: bool) -> None:
    if (
        not isinstance(value, dict) or set(value) != MANIFEST_KEYS
        or value.get("format") != FORMAT or type(value.get("schema_version")) is not int
        or value["schema_version"] not in (1, SCHEMA_VERSION)
    ):
        raise BackupError("FORMAT_UNSUPPORTED")
    if (
        not isinstance(value["modules"], list) or not value["modules"]
        or any(not isinstance(item, str) or item not in MODULES for item in value["modules"])
        or len(set(value["modules"])) != len(value["modules"])
        or type(value["credentials_included"]) is not bool
        or type(value["encrypted"]) is not bool or value["encrypted"] != encrypted
        or (value["credentials_included"] and not encrypted)
        or value["encryption"] != ("aes-256-gcm-pbkdf2-sha256-v1" if encrypted else "none")
        or not isinstance(value["plugin_version"], str)
        or not re.fullmatch(r"\d+\.\d+\.\d+", value["plugin_version"])
        or not isinstance(value["package_id"], str)
        or not re.fullmatch(r"[0-9a-f]{32}", value["package_id"])
        or not isinstance(value["created_utc"], str) or not value["created_utc"].endswith("Z")
        or not isinstance(value["dependencies"], (dict, list))
        or not isinstance(value["counts"], dict) or not isinstance(value["source"], dict)
        or not isinstance(value["omissions"], list) or not isinstance(value["limitations"], list)
        or not isinstance(value["mapping_contract"], (dict, list))
        or type(value["credentials_omitted"]) is not int or value["credentials_omitted"] < 0
        or not isinstance(value["inventory"], dict) or not isinstance(value["source_integrity"], dict)
        or any(not isinstance(key, str) or type(count) is not int or count < 0 for key, count in value["counts"].items())
    ):
        raise BackupError("MANIFEST_INVALID")
    _check_source(value["source"])
    for name, item in value["inventory"].items():
        if (
            not isinstance(name, str) or not _COMPONENT.fullmatch(name)
            or not isinstance(item, dict) or set(item) != {"rows", "bytes", "sha256"}
            or type(item["rows"]) is not int or item["rows"] < 0
            or type(item["bytes"]) is not int or not 0 <= item["bytes"] <= MAX_FILE_BYTES
            or not isinstance(item["sha256"], str) or not _HEX.fullmatch(item["sha256"])
        ):
            raise BackupError("MANIFEST_INVALID")
    integrity = value["source_integrity"]
    if (
        set(integrity) != {"missing_references", "missing_sha256"}
        or type(integrity["missing_references"]) is not int or integrity["missing_references"] < 0
        or not isinstance(integrity["missing_sha256"], str) or not _HEX.fullmatch(integrity["missing_sha256"])
    ):
        raise BackupError("MANIFEST_INVALID")
    try:
        datetime.fromisoformat(value["created_utc"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise BackupError("MANIFEST_INVALID") from exc
    entries = value.get("entries")
    if not isinstance(entries, list) or len(entries) > MAX_FILES:
        raise BackupError("FILE_COUNT_LIMIT")
    seen = set()
    total = 0
    for entry in entries:
        if (
            not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}
            or not isinstance(entry["path"], str) or type(entry["size"]) is not int
            or entry["size"] < 0 or entry["size"] > MAX_FILE_BYTES
            or not isinstance(entry["sha256"], str) or not _HEX.fullmatch(entry["sha256"])
        ):
            raise BackupError("MANIFEST_INVALID")
        name = _name(entry["path"])
        if name.casefold() in seen:
            raise BackupError("ARCHIVE_DUPLICATE")
        seen.add(name.casefold())
        total += entry["size"]
    if total > MAX_EXPANDED_BYTES:
        raise BackupError("TOTAL_LIMIT")
    if (
        type(value["file_count"]) is not int or value["file_count"] != len(entries)
        or type(value["expanded_bytes"]) is not int or value["expanded_bytes"] != total
    ):
        raise BackupError("MANIFEST_INVALID")


def _password(password: str | None) -> bytes:
    if not isinstance(password, str) or not password or len(password.encode("utf-8")) > 1024:
        raise BackupError("PASSWORD_REQUIRED")
    return password.encode("utf-8")


def _derive(password: str, salt: bytes) -> bytes:
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=KDF_ITERATIONS).derive(
        _password(password)
    )


def _private_file(path: Path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")


def _encrypt(source: Path, target: Path, password: str, cancel: Event | None) -> None:
    salt, nonce = os.urandom(16), os.urandom(12)
    header = HEADER.pack(MAGIC, ENVELOPE_VERSION, KDF_ITERATIONS, salt, nonce)
    check_cancel(cancel)
    cipher = Cipher(algorithms.AES(_derive(password, salt)), modes.GCM(nonce)).encryptor()
    cipher.authenticate_additional_data(header)
    with source.open("rb") as incoming, _private_file(target) as outgoing:
        outgoing.write(header)
        while chunk := incoming.read(CHUNK_BYTES):
            check_cancel(cancel)
            outgoing.write(cipher.update(chunk))
        outgoing.write(cipher.finalize())
        outgoing.write(cipher.tag)
        outgoing.flush()
        os.fsync(outgoing.fileno())


def _decrypt(source: Path, target: Path, password: str | None, cancel: Event | None) -> None:
    length = source.stat().st_size
    if length < HEADER.size + TAG_BYTES:
        raise BackupError("ENVELOPE_TRUNCATED")
    with source.open("rb") as incoming:
        raw_header = incoming.read(HEADER.size)
        magic, version, iterations, salt, nonce = HEADER.unpack(raw_header)
        # Reject attacker-controlled work factors before password derivation.
        if magic != MAGIC or version != ENVELOPE_VERSION or iterations != KDF_ITERATIONS:
            raise BackupError("ENVELOPE_UNSUPPORTED")
        _password(password)
        incoming.seek(-TAG_BYTES, os.SEEK_END)
        tag = incoming.read(TAG_BYTES)
        incoming.seek(HEADER.size)
        check_cancel(cancel)
        cipher = Cipher(algorithms.AES(_derive(password, salt)), modes.GCM(nonce, tag)).decryptor()
        cipher.authenticate_additional_data(raw_header)
        remaining = length - HEADER.size - TAG_BYTES
        with _private_file(target) as outgoing:
            while remaining:
                check_cancel(cancel)
                chunk = incoming.read(min(CHUNK_BYTES, remaining))
                if not chunk:
                    raise BackupError("ENVELOPE_TRUNCATED")
                remaining -= len(chunk)
                outgoing.write(cipher.update(chunk))
            try:
                outgoing.write(cipher.finalize())
            except InvalidTag as exc:
                raise BackupError("AUTHENTICATION_FAILED") from exc


def build_archive(
    stage: Path, output: Path, metadata: dict, password: str | None, cancel: Event | None,
) -> dict:
    """Freeze an inventory, then pack it without holding any business lock."""
    stage, output = Path(stage), Path(output)
    check_cancel(cancel)
    if _linked(stage) or not stage.is_dir() or output.exists():
        raise BackupError("PRIVATE_PATH")
    if metadata.get("credentials_included") and password is None:
        raise BackupError("ENCRYPTION_REQUIRED")
    if password is not None:
        _password(password)
    entries = []
    total = 0
    for root, directories, files in os.walk(stage, followlinks=False):
        check_cancel(cancel)
        for directory in directories:
            candidate = Path(root) / directory
            if _linked(candidate):
                raise BackupError("ARCHIVE_LINK")
        for filename in sorted(files):
            path = Path(root) / filename
            if _linked(path) or not path.is_file():
                raise BackupError("ARCHIVE_LINK")
            name = _name(path.relative_to(stage).as_posix())
            if len(entries) >= MAX_FILES or path.stat().st_size > MAX_FILE_BYTES:
                raise BackupError("FILE_LIMIT")
            _validate_file(path, name, cancel)
            size, digest = _hash(path, cancel)
            if name.startswith("media/") and Path(name).stem != digest:
                raise BackupError("IMAGE_HASH")
            total += size
            if total > MAX_EXPANDED_BYTES:
                raise BackupError("TOTAL_LIMIT")
            entries.append({"path": name, "size": size, "sha256": digest})
    entries.sort(key=lambda item: item["path"])
    manifest = _manifest(metadata, entries, password is not None)
    raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    parse_json(raw, MAX_MANIFEST_BYTES)
    ensure_space(output.parent, (total + len(raw) + MAX_FILES * 256) * (2 if password is not None else 1))
    temporary = output.with_name("." + uuid.uuid4().hex + ".zip")
    encrypted = output.with_name("." + uuid.uuid4().hex + ".encrypted")
    try:
        # PNG is already compressed. STORED also avoids treating repetitive but
        # legitimate source text as a bomb in packages produced by this writer.
        with _private_file(temporary) as archive_file:
            with zipfile.ZipFile(archive_file, "w", compression=zipfile.ZIP_STORED, allowZip64=False) as archive:
                archive.writestr("manifest.json", raw)
                for entry in entries:
                    check_cancel(cancel)
                    path = stage / entry["path"]
                    if _linked(path):
                        raise BackupError("ARCHIVE_LINK")
                    digest, size = hashlib.sha256(), 0
                    with path.open("rb") as incoming, archive.open(entry["path"], "w") as outgoing:
                        while chunk := incoming.read(CHUNK_BYTES):
                            check_cancel(cancel)
                            size += len(chunk)
                            if size > entry["size"]:
                                raise BackupError("SOURCE_CHANGED")
                            outgoing.write(chunk)
                            digest.update(chunk)
                    if size != entry["size"] or digest.hexdigest() != entry["sha256"]:
                        raise BackupError("SOURCE_CHANGED")
            archive_file.flush()
            os.fsync(archive_file.fileno())
        check_cancel(cancel)
        if password is not None:
            _encrypt(temporary, encrypted, password, cancel)
            check_cancel(cancel)
            os.replace(encrypted, output)
        else:
            os.replace(temporary, output)
        return manifest
    except OSError as exc:
        raise BackupError("DISK_IO") from exc
    finally:
        temporary.unlink(missing_ok=True)
        encrypted.unlink(missing_ok=True)


def _zip_inventory(path: Path, archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > MAX_FILES + 1:
        raise BackupError("FILE_COUNT_LIMIT")
    count, _ = _zip_preflight(path)
    if (
        count != len(infos) or archive.comment
        or (infos and min(info.header_offset for info in infos) != 0)
    ):
        raise BackupError("ARCHIVE_INVALID")
    result, folded = {}, set()
    total = 0
    for info in infos:
        name = _name(info.orig_filename, manifest=True)
        if name != info.filename:
            raise BackupError("ARCHIVE_PATH")
        if name.casefold() in folded:
            raise BackupError("ARCHIVE_DUPLICATE")
        folded.add(name.casefold())
        mode = info.external_attr >> 16
        if (
            info.is_dir() or (stat.S_IFMT(mode) not in (0, stat.S_IFREG))
            or info.flag_bits & ~0x800 or info.extra or info.comment
            or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
        ):
            raise BackupError("ARCHIVE_TYPE")
        if info.file_size > (MAX_MANIFEST_BYTES if name == "manifest.json" else MAX_FILE_BYTES):
            raise BackupError("FILE_LIMIT")
        if info.file_size > max(1, info.compress_size) * MAX_COMPRESSION_RATIO:
            raise BackupError("COMPRESSION_LIMIT")
        total += info.file_size
        if total > MAX_EXPANDED_BYTES + MAX_MANIFEST_BYTES:
            raise BackupError("TOTAL_LIMIT")
        result[name] = info
    if "manifest.json" not in result:
        raise BackupError("MANIFEST_MISSING")
    # Local headers cannot smuggle different names, link extensions, legacy
    # encryption or streamed descriptors behind an innocuous central entry.
    with path.open("rb") as stream:
        for info in infos:
            stream.seek(info.header_offset)
            header = stream.read(30)
            if len(header) != 30:
                raise BackupError("ARCHIVE_INVALID")
            fields = struct.unpack("<4s5H3I2H", header)
            name_size, extra_size = fields[-2:]
            encoded_name = info.filename.encode("utf-8" if info.flag_bits & 0x800 else "ascii")
            if (
                fields[0] != b"PK\x03\x04" or fields[2] != info.flag_bits
                or fields[3] != info.compress_type or fields[6] != info.CRC
                or fields[7] != info.compress_size or fields[8] != info.file_size
                or extra_size or name_size != len(encoded_name)
                or stream.read(name_size) != encoded_name
            ):
                raise BackupError("ARCHIVE_INVALID")
    return result


def _zip_preflight(path: Path) -> tuple[int, int]:
    """Bound central-directory allocation before constructing ZipFile."""
    length = path.stat().st_size
    if length < 22:
        raise BackupError("ARCHIVE_INVALID")
    with path.open("rb") as stream:
        stream.seek(-22, os.SEEK_END)
        end = stream.read(22)
    signature, disk, central_disk, disk_count, count, central_size, central_offset, comment = struct.unpack("<4s4H2IH", end)
    if (
        signature != b"PK\x05\x06" or disk or central_disk or disk_count != count or comment
        or count > MAX_FILES + 1 or central_size > (MAX_FILES + 1) * 1200
        or central_offset + central_size != length - 22
    ):
        raise BackupError("ARCHIVE_INVALID")
    return count, central_size


def inspect_archive(
    source: Path, directory: Path, password: str | None, cancel: Event | None,
) -> dict:
    """Authenticate, check the complete inventory, then extract into an empty private directory."""
    source, directory = Path(source), Path(directory)
    check_cancel(cancel)
    if _linked(source) or not source.is_file() or source.stat().st_size > MAX_INPUT_BYTES:
        raise BackupError("INPUT_LIMIT")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if _linked(directory) or any(directory.iterdir()):
        raise BackupError("PRIVATE_PATH")
    temporary = directory / ".unauthenticated.zip"
    success = False
    try:
        with source.open("rb") as stream:
            magic = stream.read(len(MAGIC))
        encrypted = magic == MAGIC
        if not encrypted and not magic.startswith(b"PK\x03\x04"):
            raise BackupError("FORMAT_UNSUPPORTED")
        if encrypted:
            ensure_space(directory, source.stat().st_size)
            _decrypt(source, temporary, password, cancel)
        plain = temporary if encrypted else source
        check_cancel(cancel)
        _zip_preflight(plain)
        with zipfile.ZipFile(plain, "r") as archive:
            inventory = _zip_inventory(plain, archive)
            with archive.open("manifest.json") as stream:
                manifest = parse_json(stream.read(MAX_MANIFEST_BYTES + 1), MAX_MANIFEST_BYTES)
            _check_manifest(manifest, encrypted)
            expected = {entry["path"]: entry for entry in manifest["entries"]}
            if set(inventory) != set(expected) | {"manifest.json"}:
                raise BackupError("INVENTORY_MISMATCH")
            for name, entry in expected.items():
                if inventory[name].file_size != entry["size"]:
                    raise BackupError("INVENTORY_MISMATCH")
            ensure_space(directory, manifest["expanded_bytes"])
            for name, entry in expected.items():
                check_cancel(cancel)
                target = directory / name
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                digest, count = hashlib.sha256(), 0
                with archive.open(name) as incoming, _private_file(target) as outgoing:
                    while chunk := incoming.read(CHUNK_BYTES):
                        check_cancel(cancel)
                        count += len(chunk)
                        if count > entry["size"]:
                            raise BackupError("FILE_LIMIT")
                        outgoing.write(chunk)
                        digest.update(chunk)
                if count != entry["size"] or digest.hexdigest() != entry["sha256"]:
                    raise BackupError("FILE_HASH")
                if name.startswith("media/") and Path(name).stem != entry["sha256"]:
                    raise BackupError("IMAGE_HASH")
                _validate_file(target, name, cancel)
        check_cancel(cancel)
        success = True
        return manifest
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, NotImplementedError) as exc:
        raise BackupError("ARCHIVE_INVALID") from exc
    except OSError as exc:
        raise BackupError("DISK_IO") from exc
    finally:
        temporary.unlink(missing_ok=True)
        if not success:
            # The directory was empty on entry and is task-private. No source
            # business path is traversed or removed by validation.
            for child in directory.iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink(missing_ok=True)
