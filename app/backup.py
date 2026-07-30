from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import Database

BACKUP_FORMAT = "invite-mailer-backup"
BACKUP_FORMAT_VERSION = 1
DEFAULT_BACKUP_DIR = Path(os.getenv("BACKUP_PATH", "/backups"))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_name(value: str) -> str:
    return "".join(ch for ch in value if ch.isalnum() or ch in {"-", "_", "."})


def _database_snapshot(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(source)
    target_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()


def _iter_optional_files() -> list[tuple[Path, str]]:
    root = Path("/opt/invite-mailer")
    result: list[tuple[Path, str]] = []
    for relative in ("config", "data"):
        source = root / relative
        if source.exists():
            result.append((source, relative))
    for relative in (".env", "docker-compose.yml"):
        source = root / relative
        if source.is_file():
            result.append((source, relative))
    return result


def create_backup(db: Database, *, trigger: str = "manual", backup_dir: Path = DEFAULT_BACKUP_DIR) -> dict[str, Any]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    created = _utc_now()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    filename = f"invite-mailer-backup-{stamp}.zip"
    final_path = backup_dir / filename

    with tempfile.TemporaryDirectory(prefix="invite-mailer-backup-") as tmp:
        stage = Path(tmp)
        database_path = stage / "state" / "invite_mailer.sqlite"
        _database_snapshot(db.path, database_path)

        for source, relative in _iter_optional_files():
            destination = stage / relative
            if source.is_dir():
                shutil.copytree(source, destination, dirs_exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)

        files: list[dict[str, Any]] = []
        for path in sorted(item for item in stage.rglob("*") if item.is_file()):
            relative = path.relative_to(stage).as_posix()
            files.append({"path": relative, "size": path.stat().st_size, "sha256": _sha256(path)})

        manifest = {
            "format": BACKUP_FORMAT,
            "format_version": BACKUP_FORMAT_VERSION,
            "application_version": "2.4.0",
            "created_at": created.isoformat(),
            "trigger": trigger,
            "database_source": str(db.path),
            "files": files,
        }
        (stage / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        temporary_archive = backup_dir / f".{filename}.tmp"
        try:
            with zipfile.ZipFile(temporary_archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                for path in sorted(item for item in stage.rglob("*") if item.is_file()):
                    archive.write(path, path.relative_to(stage).as_posix())
            os.replace(temporary_archive, final_path)
        finally:
            temporary_archive.unlink(missing_ok=True)

    return describe_backup(final_path)


def describe_backup(path: Path) -> dict[str, Any]:
    stat = path.stat()
    metadata: dict[str, Any] = {
        "name": path.name,
        "size": stat.st_size,
        "sha256": _sha256(path),
        "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).replace(microsecond=0).isoformat(),
    }
    try:
        with zipfile.ZipFile(path, "r") as archive:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        metadata.update({
            "created_at": manifest.get("created_at"),
            "trigger": manifest.get("trigger"),
            "application_version": manifest.get("application_version"),
            "format_version": manifest.get("format_version"),
        })
    except Exception as error:
        metadata["error"] = str(error)
    return metadata


def list_backups(backup_dir: Path = DEFAULT_BACKUP_DIR) -> list[dict[str, Any]]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    return [describe_backup(path) for path in sorted(backup_dir.glob("invite-mailer-backup-*.zip"), reverse=True)]


def resolve_backup(name: str, backup_dir: Path = DEFAULT_BACKUP_DIR) -> Path:
    safe = _safe_name(name)
    if safe != name or not name.endswith(".zip"):
        raise ValueError("Некорректное имя резервной копии")
    path = backup_dir / name
    if not path.is_file():
        raise FileNotFoundError(name)
    return path


def verify_backup(path: Path) -> dict[str, Any]:
    errors: list[str] = []
    checked = 0
    try:
        with zipfile.ZipFile(path, "r") as archive:
            bad = archive.testzip()
            if bad:
                errors.append(f"Поврежден ZIP-элемент: {bad}")
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            if manifest.get("format") != BACKUP_FORMAT:
                errors.append("Неизвестный формат архива")
            for item in manifest.get("files", []):
                checked += 1
                name = str(item.get("path", ""))
                expected = str(item.get("sha256", ""))
                try:
                    actual = hashlib.sha256(archive.read(name)).hexdigest()
                except KeyError:
                    errors.append(f"Отсутствует файл: {name}")
                    continue
                if actual != expected:
                    errors.append(f"Не совпадает SHA-256: {name}")
    except Exception as error:
        errors.append(str(error))
    return {"valid": not errors, "checked_files": checked, "errors": errors, "archive_sha256": _sha256(path)}


def enforce_retention(retention: int, backup_dir: Path = DEFAULT_BACKUP_DIR) -> list[str]:
    retention = max(1, retention)
    removed: list[str] = []
    valid_paths: list[Path] = []
    for item in sorted(backup_dir.glob("invite-mailer-backup-*.zip"), reverse=True):
        if verify_backup(item)["valid"]:
            valid_paths.append(item)
    for path in valid_paths[retention:]:
        path.unlink(missing_ok=True)
        removed.append(path.name)
    return removed
