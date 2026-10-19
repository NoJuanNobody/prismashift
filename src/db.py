"""
SQLite Transaction Ledger for PrismaShift.
Maintains an immutable audit log of file movements, enabling instant query and rollback.
"""

from __future__ import annotations

import sqlite3
import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, asdict


@dataclass
class TransactionRecord:
    id: Optional[int] = None
    transaction_uuid: str = ""
    file_hash: str = ""
    original_path: str = ""
    new_path: str = ""
    action: str = ""  # COPY, MOVE, SYMLINK
    category: str = ""
    rule_matched: str = ""
    timestamp: str = ""
    status: str = "COMPLETED"  # COMPLETED, ROLLED_BACK, FAILED
    error_message: Optional[str] = None


class DatabaseLedger:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        """Initializes database schema and indices."""
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    transaction_uuid TEXT NOT NULL,
                    file_hash TEXT NOT NULL,
                    original_path TEXT NOT NULL,
                    new_path TEXT NOT NULL,
                    action TEXT NOT NULL,
                    category TEXT NOT NULL,
                    rule_matched TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'COMPLETED',
                    error_message TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_trans_uuid ON transactions(transaction_uuid)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_file_hash ON transactions(file_hash)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON transactions(status)")
            conn.commit()

    def record_transaction(self, record: TransactionRecord) -> int:
        """Records a new transaction."""
        if not record.timestamp:
            record.timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()

        with self._get_connection() as conn:
            cursor = conn.execute("""
                INSERT INTO transactions (
                    transaction_uuid, file_hash, original_path, new_path,
                    action, category, rule_matched, timestamp, status, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                record.transaction_uuid,
                record.file_hash,
                record.original_path,
                record.new_path,
                record.action,
                record.category,
                record.rule_matched,
                record.timestamp,
                record.status,
                record.error_message
            ))
            conn.commit()
            return cursor.lastrowid

    def update_status(self, transaction_id: int, status: str, error_message: Optional[str] = None):
        """Updates the status of an existing transaction."""
        with self._get_connection() as conn:
            conn.execute("""
                UPDATE transactions
                SET status = ?, error_message = ?
                WHERE id = ?
            """, (status, error_message, transaction_id))
            conn.commit()

    def get_transaction(self, transaction_id: int) -> Optional[TransactionRecord]:
        """Retrieves a single transaction by ID."""
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
            if row:
                return TransactionRecord(**dict(row))
        return None

    def get_transactions_by_uuid(self, uuid: str) -> List[TransactionRecord]:
        """Retrieves transactions belonging to a batch transaction UUID."""
        with self._get_connection() as conn:
            rows = conn.execute("SELECT * FROM transactions WHERE transaction_uuid = ?", (uuid,)).fetchall()
            return [TransactionRecord(**dict(r)) for r in rows]

    def get_active_transactions(self, limit: int = 100) -> List[TransactionRecord]:
        """Returns completed (un-reverted) transactions ordered from newest to oldest."""
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM transactions WHERE status = 'COMPLETED' ORDER BY id DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [TransactionRecord(**dict(r)) for r in rows]

    def find_by_hash(self, file_hash: str) -> List[TransactionRecord]:
        """Looks up transactions matching a content SHA-256 hash."""
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM transactions WHERE file_hash = ? ORDER BY id DESC",
                (file_hash,)
            ).fetchall()
            return [TransactionRecord(**dict(r)) for r in rows]
