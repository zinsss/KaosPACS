from __future__ import annotations

import json
import os
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path


SOURCE = Path(os.getenv("MIRROR_SOURCE", "/source"))
DESTINATION = Path(os.getenv("MIRROR_DESTINATION", "/dest"))
STATUS_PATH = Path(os.getenv("MIRROR_STATUS_PATH", "/dest/.kaospacs-mirror-status.json"))
INTERVAL_SECONDS = max(5, int(os.getenv("MIRROR_INTERVAL_SECONDS", "60")))
DELETE_ENABLED = os.getenv("MIRROR_DELETE", "false").strip().lower() in {"1", "true", "yes", "on"}
EXCLUDED_NAMES = {"lost+found", ".kaospacs-mirror-status.json"}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def log(message: str) -> None:
    print(f"{utc_now()} {message}", flush=True)


def write_status(payload: dict[str, object]) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_PATH.with_name(f".{STATUS_PATH.name}.tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=True), encoding="utf-8")
    tmp.replace(STATUS_PATH)


def iter_files(root: Path):
    for current, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if name not in EXCLUDED_NAMES]
        current_path = Path(current)
        for name in files:
            if name in EXCLUDED_NAMES or name.startswith(".kaospacs-mirror-"):
                continue
            yield current_path / name


def should_copy(source: Path, destination: Path) -> bool:
    if not destination.exists():
        return True
    source_stat = source.stat()
    destination_stat = destination.stat()
    return source_stat.st_size != destination_stat.st_size or int(source_stat.st_mtime) > int(destination_stat.st_mtime)


def copy_file(source: Path, destination: Path) -> str:
    before = source.stat()
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(f".kaospacs-mirror-{destination.name}.tmp")
    shutil.copy2(source, tmp)
    after = source.stat()
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        tmp.unlink(missing_ok=True)
        return "changed_during_copy"
    tmp.replace(destination)
    return "copied"


def mirror_once() -> dict[str, object]:
    started_at = utc_now()
    scanned = 0
    copied = 0
    skipped = 0
    changed_during_copy = 0
    errors = 0
    deleted = 0
    source_files: set[Path] = set()

    for source in iter_files(SOURCE):
        relative = source.relative_to(SOURCE)
        source_files.add(relative)
        destination = DESTINATION / relative
        scanned += 1
        try:
            if should_copy(source, destination):
                result = copy_file(source, destination)
                if result == "copied":
                    copied += 1
                else:
                    changed_during_copy += 1
            else:
                skipped += 1
        except Exception as exc:
            errors += 1
            log(f"mirror_copy_error class={exc.__class__.__name__} path={relative}")

    if DELETE_ENABLED:
        for destination in iter_files(DESTINATION):
            if destination == STATUS_PATH:
                continue
            relative = destination.relative_to(DESTINATION)
            if relative not in source_files:
                try:
                    destination.unlink()
                    deleted += 1
                except Exception as exc:
                    errors += 1
                    log(f"mirror_delete_error class={exc.__class__.__name__} path={relative}")

    status = {
        "service": "kaospacs-storage-mirror",
        "source": str(SOURCE),
        "destination": str(DESTINATION),
        "started_at": started_at,
        "finished_at": utc_now(),
        "interval_seconds": INTERVAL_SECONDS,
        "delete_enabled": DELETE_ENABLED,
        "scanned": scanned,
        "copied": copied,
        "skipped": skipped,
        "changed_during_copy": changed_during_copy,
        "deleted": deleted,
        "errors": errors,
        "ok": errors == 0,
    }
    write_status(status)
    return status


def main() -> int:
    if not SOURCE.exists():
        log(f"mirror_source_missing source={SOURCE}")
        return 2
    DESTINATION.mkdir(parents=True, exist_ok=True)
    log(
        "mirror_start "
        f"source={SOURCE} destination={DESTINATION} interval_seconds={INTERVAL_SECONDS} "
        f"delete_enabled={str(DELETE_ENABLED).lower()}"
    )
    while True:
        status = mirror_once()
        log(
            "mirror_cycle "
            f"scanned={status['scanned']} copied={status['copied']} skipped={status['skipped']} "
            f"changed_during_copy={status['changed_during_copy']} deleted={status['deleted']} "
            f"errors={status['errors']}"
        )
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
