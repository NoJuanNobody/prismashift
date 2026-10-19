#!/usr/bin/env python3
"""
PrismaShift Admin CLI.
Comprehensive management tool for PrismaShift image sorting toolchain.
"""

from __future__ import annotations

import os
import sys
import argparse
import datetime
from pathlib import Path
from typing import Optional, List

# Ensure src can be imported
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import Config, load_config
from src.db import DatabaseLedger, TransactionRecord
from src.classifier import ImageClassifier, extract_metadata
from src.organizer import FileOrganizer, RouteAction
from src.watcher import BatchScanner


def format_size(bytes_size: int) -> str:
    """Format bytes into human readable string."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_size < 1024.0:
            return f"{bytes_size:.1f} {unit}" if unit != 'B' else f"{bytes_size} {unit}"
        bytes_size /= 1024.0
    return f"{bytes_size:.1f} PB"


def cmd_status(args, config: Config, ledger: DatabaseLedger):
    """
    Displays daemon status, queue count, total sorted by type, error rate.
    """
    print("\n" + "=" * 60)
    print(" 💠 PRISMASHIFT SYSTEM STATUS & TELEMETRY")
    print("=" * 60)

    # 1. Daemon / Process Check
    inbox_exists = config.inbox_dir.exists()
    dest_exists = config.destination_dir.exists()
    db_exists = config.db_path.exists()

    print(f"Inbox Directory : {config.inbox_dir} {'[OK]' if inbox_exists else '[MISSING]'}")
    print(f"Organized Dir   : {config.destination_dir} {'[OK]' if dest_exists else '[MISSING]'}")
    print(f"Database Path   : {config.db_path} {'[OK]' if db_exists else '[MISSING]'}")
    print(f"Route Mode      : {config.route_mode.value}")
    print(f"Debounce Window : {config.debounce_seconds}s (stabilization timeout: {config.stabilization_timeout_seconds}s)")

    # 2. Inbox Queue Count
    queue_count = 0
    total_queue_bytes = 0
    if inbox_exists:
        supported = [ext.lower() for ext in config.supported_extensions]
        for f in config.inbox_dir.iterdir():
            if f.is_file() and not f.name.startswith(".") and f.suffix.lower() in supported:
                queue_count += 1
                try:
                    total_queue_bytes += f.stat().st_size
                except OSError:
                    pass

    print("-" * 60)
    print(f"Pending Inbox Queue : {queue_count} files ({format_size(total_queue_bytes)})")

    # 3. Database Statistics (Sorted by Type, Errors, Total)
    total_tx = 0
    completed_tx = 0
    failed_tx = 0
    rolled_back_tx = 0
    cat_counts = {}

    with ledger._get_connection() as conn:
        cursor = conn.execute("SELECT status, count(*) FROM transactions GROUP BY status")
        for row in cursor.fetchall():
            status = row[0]
            count = row[1]
            total_tx += count
            if status == "COMPLETED":
                completed_tx += count
            elif "FAIL" in status.upper():
                failed_tx += count
            elif status == "ROLLED_BACK":
                rolled_back_tx += count

        cursor = conn.execute("""
            SELECT category, count(*) FROM transactions 
            WHERE status = 'COMPLETED' 
            GROUP BY category
        """)
        for row in cursor.fetchall():
            cat_counts[row[0]] = row[1]

    error_rate = (failed_tx / total_tx * 100) if total_tx > 0 else 0.0

    print("-" * 60)
    print(f"Total Transactions  : {total_tx}")
    print(f"Completed Active    : {completed_tx}")
    print(f"Rolled Back         : {rolled_back_tx}")
    print(f"Failed Operations   : {failed_tx}")
    print(f"Error Rate          : {error_rate:.2f}%")

    print("\nSorted Files by Category:")
    if cat_counts:
        for cat, cnt in sorted(cat_counts.items(), key=lambda x: x[1], reverse=True):
            print(f"  • {cat:<20} : {cnt:>6} files")
    else:
        print("  (No files recorded in database ledger yet)")

    print("=" * 60 + "\n")


def cmd_scan(args, config: Config, ledger: DatabaseLedger):
    """
    Batch processes the inbox immediately with optional preview / dry-run.
    """
    dry_run = getattr(args, "dry_run", False)
    target_dir = Path(args.path).resolve() if getattr(args, "path", None) else config.inbox_dir

    print(f"\n🔍 Scanning directory: {target_dir}")
    if dry_run:
        print("⚠️  DRY RUN MODE ENABLED — No files will be moved or modified.\n")

    if not target_dir.exists():
        print(f"❌ Target directory does not exist: {target_dir}")
        return

    classifier = ImageClassifier(fallback_category=config.fallback_category)
    organizer = FileOrganizer(ledger)

    supported = [ext.lower() for ext in config.supported_extensions]
    candidate_files = [
        f for f in target_dir.iterdir()
        if f.is_file() and not f.name.startswith(".") and f.suffix.lower() in supported
    ]

    if not candidate_files:
        print("No supported image files found to process.")
        return

    print(f"Found {len(candidate_files)} image files. Beginning classification...\n")

    processed_count = 0
    error_count = 0
    category_summary = {}

    import uuid
    batch_uuid = str(uuid.uuid4())

    for idx, f in enumerate(candidate_files, 1):
        res = classifier.classify(f)
        cat = res.category
        category_summary[cat] = category_summary.get(cat, 0) + 1

        if dry_run:
            print(f"[{idx}/{len(candidate_files)}] [DRY-RUN] {f.name} -> {cat}/ (rule: {res.rule_matched}, conf: {res.confidence:.0%})")
            processed_count += 1
        else:
            action = RouteAction[config.route_mode.value]
            success, final_path, tx = organizer.route_file(
                source_path=f,
                dest_directory=config.destination_dir,
                category=cat,
                rule_matched=res.rule_matched,
                action=action,
                transaction_uuid=batch_uuid
            )
            if success and final_path:
                print(f"[{idx}/{len(candidate_files)}] ✅ {f.name} -> {final_path.relative_to(config.destination_dir)} (rule: {res.rule_matched})")
                processed_count += 1
            else:
                err = tx.error_message if tx else "Unknown routing failure"
                print(f"[{idx}/{len(candidate_files)}] ❌ Failed {f.name}: {err}")
                error_count += 1

    print("\n" + "-" * 50)
    print(f"Scan Summary: {processed_count} files evaluated, {error_count} errors.")
    if not dry_run and processed_count > 0:
        print(f"Batch Transaction UUID: {batch_uuid}")
    print("Breakdown by category:")
    for cat, count in sorted(category_summary.items()):
        print(f"  • {cat}: {count}")
    print("-" * 50 + "\n")


def cmd_test_rules(args, config: Config, ledger: DatabaseLedger):
    """
    Previews matches on sample images without moving them.
    """
    target_dir = Path(args.path).resolve() if getattr(args, "path", None) else config.inbox_dir
    sample_limit = getattr(args, "sample", 10) or 10

    print(f"\n🧪 Testing rules against images in: {target_dir}")
    print(f"Sample limit: {sample_limit}\n")

    if not target_dir.exists():
        print(f"❌ Directory does not exist: {target_dir}")
        return

    classifier = ImageClassifier(fallback_category=config.fallback_category)
    supported = [ext.lower() for ext in config.supported_extensions]
    all_files = [
        f for f in target_dir.iterdir()
        if f.is_file() and not f.name.startswith(".") and f.suffix.lower() in supported
    ]

    sample_files = all_files[:sample_limit]

    if not sample_files:
        print("No image files found in target path.")
        return

    header = f"{'Filename':<35} | {'Category':<15} | {'Rule Matched':<25} | {'Conf':<6} | {'Resolution':<12} | {'Orientation'}"
    print(header)
    print("-" * len(header))

    for f in sample_files:
        res = classifier.classify(f)
        meta = res.metadata
        res_str = f"{meta.width}x{meta.height}" if meta.width else "N/A"
        orient = meta.orientation or "N/A"
        print(f"{f.name[:35]:<35} | {res.category:<15} | {res.rule_matched:<25} | {res.confidence:<6.0%} | {res_str:<12} | {orient}")

    print("-" * len(header) + "\n")


def cmd_history(args, config: Config, ledger: DatabaseLedger):
    """
    Lists recent sorting transactions with IDs and counts.
    """
    limit = getattr(args, "limit", 20) or 20
    print(f"\n📜 Transaction History (last {limit} records):")

    with ledger._get_connection() as conn:
        cursor = conn.execute("""
            SELECT id, transaction_uuid, original_path, new_path, category, rule_matched, status, timestamp
            FROM transactions
            ORDER BY id DESC
            LIMIT ?
        """, (limit,))
        rows = cursor.fetchall()

    if not rows:
        print("No transactions recorded.")
        return

    header = f"{'ID':<6} | {'Time':<19} | {'Category':<15} | {'Status':<12} | {'File Name':<30} | {'Tx UUID':<12}"
    print(header)
    print("-" * len(header))

    for r in rows:
        tx_id = r["id"]
        ts = r["timestamp"][:19].replace("T", " ")
        cat = r["category"]
        st = r["status"]
        name = Path(r["new_path"]).name if r["new_path"] else Path(r["original_path"]).name
        uuid_short = r["transaction_uuid"][:8] + "..." if r["transaction_uuid"] else "-"
        print(f"{tx_id:<6} | {ts:<19} | {cat:<15} | {st:<12} | {name[:30]:<30} | {uuid_short:<12}")

    print("-" * len(header) + "\n")


def cmd_rollback(args, config: Config, ledger: DatabaseLedger):
    """
    Restores files from a transaction ID or batch UUID back to original locations.
    """
    tx_identifier = args.transaction_id.strip()
    organizer = FileOrganizer(ledger)

    print(f"\n⏪ Initiating rollback for identifier: {tx_identifier}")

    # Check if integer ID or UUID string
    if tx_identifier.isdigit():
        tx_id = int(tx_identifier)
        tx = ledger.get_transaction(tx_id)
        if not tx:
            print(f"❌ Transaction #{tx_id} not found.")
            return

        print(f"Transaction details: File '{Path(tx.new_path).name}' -> restore to '{tx.original_path}'")
        success = organizer.rollback_transaction(tx_id)
        if success:
            print(f"✅ Successfully rolled back transaction #{tx_id}.")
        else:
            print(f"❌ Failed to rollback transaction #{tx_id}. Check ledger error status.")
    else:
        # Batch UUID rollback
        txs = ledger.get_transactions_by_uuid(tx_identifier)
        if not txs:
            print(f"❌ No transactions found for UUID: {tx_identifier}")
            return

        print(f"Found {len(txs)} transactions in batch. Rolling back...")
        results = organizer.rollback_batch(tx_identifier)
        succeeded = sum(1 for r in results if r)
        print(f"Rollback complete: {succeeded}/{len(results)} files restored successfully.")

    print()


def cmd_reprocess(args, config: Config, ledger: DatabaseLedger):
    """
    Re-evaluates files against active rules (e.g. unclassified-only or all organized files).
    """
    unclassified_only = getattr(args, "unclassified_only", False)
    target_cat = config.fallback_category if unclassified_only else None

    print(f"\n🔄 Reprocessing organized files (filter: {'Unclassified only' if unclassified_only else 'All categories'})...")

    if not config.destination_dir.exists():
        print(f"❌ Organized directory does not exist: {config.destination_dir}")
        return

    classifier = ImageClassifier(fallback_category=config.fallback_category)
    organizer = FileOrganizer(ledger)
    supported = [ext.lower() for ext in config.supported_extensions]

    # Gather files
    files_to_reprocess = []
    if unclassified_only:
        search_dirs = [config.destination_dir / config.fallback_category]
    else:
        search_dirs = [d for d in config.destination_dir.iterdir() if d.is_dir()]

    for s_dir in search_dirs:
        if s_dir.exists():
            for f in s_dir.rglob("*"):
                if f.is_file() and not f.name.startswith(".") and f.suffix.lower() in supported:
                    files_to_reprocess.append(f)

    if not files_to_reprocess:
        print("No files found to reprocess.")
        return

    print(f"Found {len(files_to_reprocess)} files to re-evaluate.\n")

    reassigned = 0
    unchanged = 0

    for idx, f in enumerate(files_to_reprocess, 1):
        res = classifier.classify(f)
        cur_category = f.parent.name

        if res.category != cur_category:
            print(f"[{idx}/{len(files_to_reprocess)}] 🔀 Reclassifying {f.name}: {cur_category} -> {res.category} (rule: {res.rule_matched})")
            success, final_path, _ = organizer.route_file(
                source_path=f,
                dest_directory=config.destination_dir,
                category=res.category,
                rule_matched=f"reprocess_{res.rule_matched}",
                action=RouteAction.MOVE
            )
            if success:
                reassigned += 1
        else:
            unchanged += 1

    print("\n" + "-" * 50)
    print(f"Reprocess complete: {reassigned} files re-routed, {unchanged} remained in place.")
    print("-" * 50 + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prismashift-admin",
        description="PrismaShift Admin CLI — High-velocity automated image sorting manager"
    )
    parser.add_argument("--config", "-c", help="Path to custom config JSON file", default=None)

    subparsers = parser.add_subparsers(dest="command", required=True, help="Available subcommands")

    # status
    p_status = subparsers.add_parser("status", help="Displays daemon status, queue count, total sorted by type, error rate")

    # scan
    p_scan = subparsers.add_parser("scan", help="Batch processes the inbox immediately with optional preview")
    p_scan.add_argument("--dry-run", action="store_true", help="Preview matches without moving any files")
    p_scan.add_argument("--path", help="Alternative directory to scan instead of default inbox", default=None)

    # test-rules
    p_test = subparsers.add_parser("test-rules", help="Previews matches on sample images without moving them")
    p_test.add_argument("--path", help="Directory containing images to test", default=None)
    p_test.add_argument("--sample", type=int, default=10, help="Number of sample files to inspect")

    # history
    p_hist = subparsers.add_parser("history", help="Lists recent sorting transactions with IDs and counts")
    p_hist.add_argument("--limit", type=int, default=20, help="Maximum number of history records to display")

    # rollback
    p_rb = subparsers.add_parser("rollback", help="Restores all files from a transaction ID or UUID back to original locations")
    p_rb.add_argument("transaction_id", help="Transaction record ID or batch UUID")

    # reprocess
    p_rep = subparsers.add_parser("reprocess", help="Re-evaluates files against active rules")
    p_rep.add_argument("--unclassified-only", action="store_true", help="Only reprocess files in unclassified fallback category")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    config = load_config(args.config)
    ledger = DatabaseLedger(config.db_path)

    cmd_map = {
        "status": cmd_status,
        "scan": cmd_scan,
        "test-rules": cmd_test_rules,
        "history": cmd_history,
        "rollback": cmd_rollback,
        "reprocess": cmd_reprocess,
    }

    handler = cmd_map.get(args.command)
    if handler:
        handler(args, config, ledger)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
