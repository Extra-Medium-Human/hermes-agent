from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest


def test_online_snapshot_captures_live_wal_and_fsyncs_valid_database(tmp_path: Path) -> None:
    from hermes_cli.update_state_snapshot import snapshot_state_db

    home = tmp_path / "home"
    home.mkdir()
    source = home / "state.db"
    writer = sqlite3.connect(source)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE events(value TEXT)")
    writer.execute("INSERT INTO events VALUES ('committed-in-wal')")
    writer.commit()

    receipt = snapshot_state_db(home, transaction_nonce="nonce-snapshot")

    assert receipt.path == home / ".hermes-update-snapshots" / "nonce-snapshot" / "state.db"
    assert receipt.sha256 == hashlib.sha256(receipt.path.read_bytes()).hexdigest()
    assert receipt.size == receipt.path.stat().st_size
    with sqlite3.connect(receipt.path) as copied:
        assert copied.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        result = copied.execute("SELECT value FROM events").fetchall()
    assert result == [("committed-in-wal",)]
    writer.close()


def test_invalid_sqlite_header_fails_without_snapshot_receipt(tmp_path: Path) -> None:
    from hermes_cli.update_state_snapshot import StateSnapshotError, snapshot_state_db

    home = tmp_path / "home"
    home.mkdir()
    (home / "state.db").write_bytes(b"not sqlite" * 64)

    with pytest.raises(StateSnapshotError, match="header"):
        snapshot_state_db(home, transaction_nonce="nonce-invalid")

    assert not (home / ".hermes-update-snapshots" / "nonce-invalid" / "receipt.json").exists()


def test_snapshot_receipt_validation_is_nonce_and_content_bound(tmp_path: Path) -> None:
    from hermes_cli.update_state_snapshot import (
        StateSnapshotError,
        snapshot_state_db,
        validate_snapshot_receipt,
    )

    home = tmp_path / "home"
    home.mkdir()
    source = home / "state.db"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE events(value TEXT)")
        db.execute("INSERT INTO events VALUES ('safe')")
        db.commit()

    receipt = snapshot_state_db(home, transaction_nonce="nonce-bound")
    receipt_path = receipt.path.parent / "receipt.json"
    assert validate_snapshot_receipt(receipt_path, transaction_nonce="nonce-bound") == receipt

    with pytest.raises(StateSnapshotError, match="nonce"):
        validate_snapshot_receipt(receipt_path, transaction_nonce="other-nonce")

    receipt.path.write_bytes(receipt.path.read_bytes() + b"tamper")
    with pytest.raises(StateSnapshotError, match="hash|size"):
        validate_snapshot_receipt(receipt_path, transaction_nonce="nonce-bound")
