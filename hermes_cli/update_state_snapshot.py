"""Coherent, transaction-owned SQLite snapshots for Desktop updates."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path


class StateSnapshotError(RuntimeError):
    pass


@dataclass(frozen=True)
class StateSnapshotReceipt:
    path: Path
    sha256: str
    size: int
    transaction_nonce: str


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def snapshot_state_db(home: Path, *, transaction_nonce: str) -> StateSnapshotReceipt:
    """Use SQLite's online-backup API, then integrity/hash/fsync the result."""
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", transaction_nonce):
        raise StateSnapshotError("invalid transaction nonce")
    home = Path(home)
    source = home / "state.db"
    try:
        with source.open("rb") as stream:
            if stream.read(16) != b"SQLite format 3\x00":
                raise StateSnapshotError("state.db has an invalid SQLite header")
    except OSError as exc:
        raise StateSnapshotError(f"state.db header could not be read: {exc}") from exc
    target_dir = home / ".hermes-update-snapshots" / transaction_nonce
    target_dir.mkdir(parents=True, exist_ok=False)
    target = target_dir / "state.db"
    try:
        source_uri = f"file:{source.resolve()}?mode=ro"
        with sqlite3.connect(source_uri, uri=True, timeout=30) as reader:
            with sqlite3.connect(target) as writer:
                reader.backup(writer)
                verdict = writer.execute("PRAGMA integrity_check").fetchall()
                if verdict != [("ok",)]:
                    raise StateSnapshotError("snapshot integrity check failed")
                writer.commit()
        with target.open("rb") as stream:
            os.fsync(stream.fileno())
        receipt = StateSnapshotReceipt(
            target, _hash(target), target.stat().st_size, transaction_nonce
        )
        payload = {**asdict(receipt), "path": str(receipt.path)}
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=target_dir, prefix=".receipt.", delete=False
        ) as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(stream.name, target_dir / "receipt.json")
        _fsync_directory(target_dir)
        _fsync_directory(target_dir.parent)
        return receipt
    except (OSError, sqlite3.Error, StateSnapshotError) as exc:
        if isinstance(exc, StateSnapshotError):
            raise
        raise StateSnapshotError(f"online SQLite snapshot failed: {exc}") from exc


def validate_snapshot_receipt(
    receipt_path: Path, *, transaction_nonce: str
) -> StateSnapshotReceipt:
    """Re-open and verify a snapshot receipt immediately before source mutation."""
    try:
        payload = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
        receipt = StateSnapshotReceipt(
            path=Path(payload["path"]),
            sha256=str(payload["sha256"]),
            size=int(payload["size"]),
            transaction_nonce=str(payload["transaction_nonce"]),
        )
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise StateSnapshotError(f"snapshot receipt is unreadable: {exc}") from exc
    if receipt.transaction_nonce != transaction_nonce:
        raise StateSnapshotError("snapshot receipt nonce does not match the update")
    expected_dir = Path(receipt_path).resolve().parent
    if receipt.path.resolve().parent != expected_dir:
        raise StateSnapshotError("snapshot receipt points outside its transaction directory")
    try:
        actual_size = receipt.path.stat().st_size
    except OSError as exc:
        raise StateSnapshotError(f"snapshot file is unreadable: {exc}") from exc
    if actual_size != receipt.size:
        raise StateSnapshotError("snapshot size does not match its receipt")
    if _hash(receipt.path) != receipt.sha256:
        raise StateSnapshotError("snapshot hash does not match its receipt")
    try:
        source_uri = f"file:{receipt.path.resolve()}?mode=ro&immutable=1"
        with sqlite3.connect(source_uri, uri=True, timeout=30) as db:
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise StateSnapshotError("snapshot integrity check failed during validation")
    except sqlite3.Error as exc:
        raise StateSnapshotError(f"snapshot validation failed: {exc}") from exc
    return receipt


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("snapshot", "validate"))
    parser.add_argument("--home", type=Path)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "snapshot":
            if args.home is None:
                parser.error("snapshot requires --home")
            receipt = snapshot_state_db(args.home, transaction_nonce=args.nonce)
        else:
            if args.receipt is None:
                parser.error("validate requires --receipt")
            receipt = validate_snapshot_receipt(
                args.receipt, transaction_nonce=args.nonce
            )
    except StateSnapshotError as exc:
        print(json.dumps({"ok": False, "message": str(exc)}, sort_keys=True))
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "path": str(receipt.path),
                "receipt": str(receipt.path.parent / "receipt.json"),
                "sha256": receipt.sha256,
                "size": receipt.size,
                "transaction_nonce": receipt.transaction_nonce,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
