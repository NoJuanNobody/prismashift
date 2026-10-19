"""
Safe atomic file routing engine with transaction logging and collision management.
Supports COPY, MOVE, and SYMLINK routing with full rollback capability.
"""

from __future__ import annotations

import os
import shutil
import hashlib
import uuid
from enum import Enum
from pathlib import Path
from typing import Optional, Tuple

from .config import RouteMode
from .db import DatabaseLedger, TransactionRecord


class RouteAction(str, Enum):
    COPY = "COPY"
    MOVE = "MOVE"
    SYMLINK = "SYMLINK"


def compute_sha256(file_path: Path, block_size: int = 65536) -> str:
    """Computes SHA-256 hash of a file efficiently in chunked blocks."""
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(block_size), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def resolve_collision(target_path: Path) -> Path:
    """
    Resolves naming collisions by appending sequential numbering:
    e.g. `image.jpg` -> `image_1.jpg`, `image_2.jpg`.
    """
    if not target_path.exists():
        return target_path

    parent = target_path.parent
    stem = target_path.stem
    suffix = target_path.suffix

    counter = 1
    while True:
        candidate = parent / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


class FileOrganizer:
    """
    Handles atomic filesystem routing and rollback operations with database auditing.
    """
    def __init__(self, ledger: DatabaseLedger):
        self.ledger = ledger

    def route_file(
        self,
        source_path: Path | str,
        dest_directory: Path | str,
        category: str,
        rule_matched: str = "manual",
        action: RouteAction = RouteAction.MOVE,
        transaction_uuid: Optional[str] = None,
    ) -> Tuple[bool, Optional[Path], Optional[TransactionRecord]]:
        """
        Routes a single file to target destination directory atomically.
        Logs transaction to DB ledger for rollback capability.

        Returns: (success, final_path, transaction_record)
        """
        src = Path(source_path).resolve()
        if not src.exists() or not src.is_file():
            return False, None, None

        if transaction_uuid is None:
            transaction_uuid = str(uuid.uuid4())

        # Target directory inside organized destination
        target_dir = Path(dest_directory).resolve() / category
        target_dir.mkdir(parents=True, exist_ok=True)

        target_candidate = target_dir / src.name
        final_dest = resolve_collision(target_candidate)

        # Compute hash before operation
        file_hash = compute_sha256(src)

        # Prepare transaction record
        record = TransactionRecord(
            transaction_uuid=transaction_uuid,
            file_hash=file_hash,
            original_path=str(src),
            new_path=str(final_dest),
            action=action.value,
            category=category,
            rule_matched=rule_matched,
            status="IN_PROGRESS"
        )
        tx_id = self.ledger.record_transaction(record)
        record.id = tx_id

        try:
            if action == RouteAction.COPY:
                # Atomic copy: copy to temporary file in target dir then atomic rename
                temp_dest = target_dir / f".tmp_{uuid.uuid4().hex}_{final_dest.name}"
                shutil.copy2(src, temp_dest)
                temp_dest.replace(final_dest)

            elif action == RouteAction.MOVE:
                # Direct move if same filesystem or atomic copy+delete across mounts
                temp_dest = target_dir / f".tmp_{uuid.uuid4().hex}_{final_dest.name}"
                shutil.copy2(src, temp_dest)
                temp_dest.replace(final_dest)
                src.unlink()

            elif action == RouteAction.SYMLINK:
                final_dest.symlink_to(src)

            # Mark complete in ledger
            self.ledger.update_status(tx_id, "COMPLETED")
            record.status = "COMPLETED"
            return True, final_dest, record

        except Exception as ex:
            error_msg = str(ex)
            self.ledger.update_status(tx_id, "FAILED", error_message=error_msg)
            # Cleanup potential stray temp files
            if 'temp_dest' in locals() and temp_dest.exists():
                try:
                    temp_dest.unlink()
                except Exception:
                    pass
            record.status = "FAILED"
            record.error_message = error_msg
            return False, None, record

    def rollback_transaction(self, transaction_id: int) -> bool:
        """
        Reverts an individual transaction:
        - If MOVE: moves the file back from new_path to original_path
        - If COPY or SYMLINK: unlinks new_path
        """
        tx = self.ledger.get_transaction(transaction_id)
        if not tx or tx.status != "COMPLETED":
            return False

        orig = Path(tx.original_path)
        cur = Path(tx.new_path)

        try:
            if tx.action == RouteAction.MOVE.value:
                if not cur.exists():
                    self.ledger.update_status(tx.id, "ROLLBACK_FAILED", "File at new_path missing")
                    return False
                # If original path parent directory does not exist, recreate it
                orig.parent.mkdir(parents=True, exist_ok=True)
                final_restore_path = resolve_collision(orig)
                shutil.move(str(cur), str(final_restore_path))

            elif tx.action in (RouteAction.COPY.value, RouteAction.SYMLINK.value):
                if cur.exists() or cur.is_symlink():
                    cur.unlink()

            self.ledger.update_status(tx.id, "ROLLED_BACK")
            return True

        except Exception as e:
            self.ledger.update_status(tx.id, "ROLLBACK_FAILED", str(e))
            return False

    def rollback_batch(self, transaction_uuid: str) -> List[bool]:
        """Rolls back all transactions belonging to a batch transaction UUID."""
        txs = self.ledger.get_transactions_by_uuid(transaction_uuid)
        results = []
        for tx in txs:
            if tx.id:
                results.append(self.rollback_transaction(tx.id))
        return results
