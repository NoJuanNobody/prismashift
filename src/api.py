"""
FastAPI Backend and REST API for PrismaShift Customer Dashboard.
Serves static dashboard files, real-time telemetry, drag-and-drop file ingestion,
and zip archive downloads.
"""

from __future__ import annotations

import os
import io
import sys
import zipfile
import shutil
import asyncio
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Ensure src modules are discoverable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import Config, load_config, RouteMode
from src.db import DatabaseLedger, TransactionRecord
from src.classifier import ImageClassifier, extract_metadata
from src.organizer import FileOrganizer, RouteAction
from src.watcher import BatchScanner


app = FastAPI(
    title="PrismaShift Dashboard API",
    description="Automated zero-friction photo and image classification pipeline",
    version="1.0.0",
)

# Enable CORS for flexible dashboard embedding
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Load configuration and services
config = load_config()
config.inbox_dir.mkdir(parents=True, exist_ok=True)
config.destination_dir.mkdir(parents=True, exist_ok=True)

ledger = DatabaseLedger(config.db_path)
classifier = ImageClassifier(fallback_category=config.fallback_category)
organizer = FileOrganizer(ledger)

# Static files directory
STATIC_DIR = Path(__file__).resolve().parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# Pydantic Schemas
class StatusResponse(BaseModel):
    status: str
    inbox_queue_count: int
    inbox_queue_bytes: int
    total_sorted: int
    type_a_count: int
    type_b_count: int
    unclassified_count: int
    error_rate: float
    categories: Dict[str, int]
    recent_activity: List[Dict[str, Any]]
    system_health: Dict[str, Any]


class SortedFileItem(BaseModel):
    id: str
    name: str
    category: str
    rule_matched: str
    size_bytes: int
    size_formatted: str
    resolution: str
    format: str
    confidence: float
    timestamp: str
    download_url: str


def format_size(bytes_size: int) -> str:
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_size < 1024.0:
            return f"{bytes_size:.1f} {unit}" if unit != 'B' else f"{bytes_size} {unit}"
        bytes_size /= 1024.0
    return f"{bytes_size:.1f} PB"


def map_category_to_type(category: str) -> str:
    """
    Maps fine-grained categories to high-level Type A / Type B / Unclassified.
    Landscape/Photo/HighRes -> Type A
    Portrait/Screenshot/LowRes -> Type B
    Unclassified/Corrupt -> Unclassified
    """
    cat_lower = category.lower()
    if cat_lower in ["landscape", "photos", "high_res", "type a", "type_a"]:
        return "Type A"
    elif cat_lower in ["portrait", "screenshots", "low_res", "square", "type b", "type_b"]:
        return "Type B"
    elif cat_lower in ["unclassified", "fallback", "corrupt", "quarantine"]:
        return "Unclassified"
    return category


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    """Serves the main customer dashboard interface."""
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        return HTMLResponse(
            "<h1>PrismaShift Dashboard</h1><p>Static index.html not found in src/static.</p>",
            status_code=404
        )
    return FileResponse(str(index_file))


@app.get("/api/status", response_model=StatusResponse)
async def get_system_status():
    """
    GET /api/status: Summary counts, recent activity, system health.
    """
    # 1. Inbox queue calculation
    queue_count = 0
    queue_bytes = 0
    if config.inbox_dir.exists():
        supported = [ext.lower() for ext in config.supported_extensions]
        for f in config.inbox_dir.iterdir():
            if f.is_file() and not f.name.startswith(".") and f.suffix.lower() in supported:
                queue_count += 1
                try:
                    queue_bytes += f.stat().st_size
                except OSError:
                    pass

    # 2. Database statistics
    categories: Dict[str, int] = {}
    total_tx = 0
    completed_tx = 0
    failed_tx = 0
    type_a_count = 0
    type_b_count = 0
    unclassified_count = 0

    with ledger._get_connection() as conn:
        cursor = conn.execute("""
            SELECT category, count(*) FROM transactions 
            WHERE status = 'COMPLETED' 
            GROUP BY category
        """)
        for row in cursor.fetchall():
            cat = row[0]
            cnt = row[1]
            categories[cat] = cnt
            mapped = map_category_to_type(cat)
            if mapped == "Type A":
                type_a_count += cnt
            elif mapped == "Type B":
                type_b_count += cnt
            elif mapped == "Unclassified":
                unclassified_count += cnt

        cursor = conn.execute("SELECT status, count(*) FROM transactions GROUP BY status")
        for row in cursor.fetchall():
            st = row[0]
            cnt = row[1]
            total_tx += cnt
            if st == "COMPLETED":
                completed_tx += cnt
            elif "FAIL" in st.upper():
                failed_tx += cnt

        # Recent activities
        cursor = conn.execute("""
            SELECT id, transaction_uuid, original_path, new_path, category, rule_matched, status, timestamp
            FROM transactions
            ORDER BY id DESC
            LIMIT 15
        """)
        recent_rows = cursor.fetchall()

    recent_activity = []
    for r in recent_rows:
        filename = Path(r["new_path"]).name if r["new_path"] else Path(r["original_path"]).name
        recent_activity.append({
            "id": f"tx-{r['id']}",
            "filename": filename,
            "category": r["category"],
            "mapped_type": map_category_to_type(r["category"]),
            "rule_matched": r["rule_matched"],
            "status": r["status"],
            "timestamp": r["timestamp"],
            "transaction_uuid": r["transaction_uuid"],
        })

    error_rate = (failed_tx / total_tx * 100) if total_tx > 0 else 0.0

    system_health = {
        "storage_ok": config.destination_dir.exists(),
        "database_ok": config.db_path.exists(),
        "route_mode": config.route_mode.value,
        "debounce_seconds": config.debounce_seconds,
        "inbox_path": str(config.inbox_dir),
        "destination_path": str(config.destination_dir),
    }

    return StatusResponse(
        status="healthy",
        inbox_queue_count=queue_count,
        inbox_queue_bytes=queue_bytes,
        total_sorted=completed_tx,
        type_a_count=type_a_count,
        type_b_count=type_b_count,
        unclassified_count=unclassified_count,
        error_rate=round(error_rate, 2),
        categories=categories,
        recent_activity=recent_activity,
        system_health=system_health,
    )


@app.get("/api/files")
async def list_files(
    category: Optional[str] = Query(None, description="Filter by category or Type A / Type B / Unclassified"),
    limit: int = Query(100, ge=1, le=500),
):
    """
    GET /api/files: List sorted files categorized by Type A, Type B, Unclassified,
    or fine-grained categories with metadata and thumbnail preview urls.
    """
    results: List[SortedFileItem] = []
    supported = [ext.lower() for ext in config.supported_extensions]

    if not config.destination_dir.exists():
        return {"total": 0, "files": []}

    # Traverse organized subdirectories
    for cat_dir in sorted(config.destination_dir.iterdir()):
        if not cat_dir.is_dir():
            continue

        raw_cat = cat_dir.name
        mapped_type = map_category_to_type(raw_cat)

        # Apply filtering if requested
        if category:
            cat_query = category.strip().lower()
            if cat_query in ["type a", "type_a", "type-a", "a"] and mapped_type != "Type A":
                continue
            elif cat_query in ["type b", "type_b", "type-b", "b"] and mapped_type != "Type B":
                continue
            elif cat_query in ["unclassified", "unclass"] and mapped_type != "Unclassified":
                continue
            elif cat_query not in ["type a", "type b", "unclassified", "a", "b"] and cat_query != raw_cat.lower():
                continue

        for f in cat_dir.iterdir():
            if f.is_file() and not f.name.startswith(".") and f.suffix.lower() in supported:
                try:
                    stat = f.stat()
                    size_bytes = stat.st_size
                except OSError:
                    size_bytes = 0

                meta = extract_metadata(f)
                res_str = f"{meta.width} x {meta.height}" if meta and meta.width else "Unknown"
                fmt = (meta.format or f.suffix.replace(".", "")).upper() if meta else f.suffix.upper()

                # Get confidence / rule from ledger if present
                rule_matched = "filesystem"
                confidence = 0.95
                with ledger._get_connection() as conn:
                    row = conn.execute(
                        "SELECT rule_matched FROM transactions WHERE new_path = ? ORDER BY id DESC LIMIT 1",
                        (str(f),)
                    ).fetchone()
                    if row:
                        rule_matched = row["rule_matched"]

                file_id = f"{raw_cat}_{f.name}"
                results.append(SortedFileItem(
                    id=file_id,
                    name=f.name,
                    category=raw_cat,
                    rule_matched=rule_matched,
                    size_bytes=size_bytes,
                    size_formatted=format_size(size_bytes),
                    resolution=res_str,
                    format=fmt,
                    confidence=confidence,
                    timestamp="Active",
                    download_url=f"/api/file/{raw_cat}/{f.name}",
                ))

                if len(results) >= limit:
                    break
        if len(results) >= limit:
            break

    return {
        "total": len(results),
        "files": [f.model_dump() for f in results]
    }


@app.get("/api/file/{category}/{filename}")
async def get_raw_file(category: str, filename: str):
    """Serve or download individual image file with safety validation."""
    target = (config.destination_dir / category / filename).resolve()

    # Prevent path traversal
    if not str(target).startswith(str(config.destination_dir.resolve())):
        raise HTTPException(status_code=403, detail="Forbidden access outside destination directory")

    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(str(target))


@app.post("/api/upload")
async def upload_files(
    files: List[UploadFile] = File(...),
    immediate_process: bool = Form(True),
):
    """
    POST /api/upload: Direct drag-and-drop file upload endpoint that feeds directly
    into the sorting pipeline or drops into inbox for debounced ingestion.
    """
    uploaded_results = []
    supported = [ext.lower() for ext in config.supported_extensions]

    import uuid
    upload_batch_uuid = str(uuid.uuid4())

    for upload in files:
        ext = Path(upload.filename).suffix.lower()
        if ext not in supported:
            uploaded_results.append({
                "filename": upload.filename,
                "status": "rejected",
                "reason": f"Unsupported extension {ext}"
            })
            continue

        # Save to inbox
        dest_inbox_path = config.inbox_dir / upload.filename
        dest_inbox_path = config.inbox_dir / f"{Path(upload.filename).stem}_{uuid.uuid4().hex[:6]}{ext}"

        with open(dest_inbox_path, "wb") as buffer:
            shutil.copyfileobj(upload.file, buffer)

        if immediate_process:
            # Execute instant classification and routing
            classification = classifier.classify(dest_inbox_path)
            action = RouteAction[config.route_mode.value]

            success, final_path, tx = organizer.route_file(
                source_path=dest_inbox_path,
                dest_directory=config.destination_dir,
                category=classification.category,
                rule_matched=classification.rule_matched,
                action=action,
                transaction_uuid=upload_batch_uuid,
            )

            if success and final_path:
                uploaded_results.append({
                    "filename": upload.filename,
                    "status": "sorted",
                    "category": classification.category,
                    "mapped_type": map_category_to_type(classification.category),
                    "rule_matched": classification.rule_matched,
                    "confidence": classification.confidence,
                    "final_path": str(final_path),
                    "download_url": f"/api/file/{classification.category}/{final_path.name}"
                })
            else:
                uploaded_results.append({
                    "filename": upload.filename,
                    "status": "failed",
                    "error": tx.error_message if tx else "Routing failed"
                })
        else:
            # Left in inbox for debounce watcher
            uploaded_results.append({
                "filename": upload.filename,
                "status": "queued_in_inbox",
                "inbox_path": str(dest_inbox_path)
            })

    return {
        "batch_uuid": upload_batch_uuid,
        "processed": len(uploaded_results),
        "results": uploaded_results
    }


@app.get("/api/download/{category}")
async def download_category_zip(category: str):
    """
    GET /api/download/{category}: Download a zip archive of sorted files
    for a specific category, Type A, Type B, or 'all'.
    """
    target_dirs: List[Path] = []
    cat_query = category.strip().lower()

    if cat_query == "all":
        target_dirs = [d for d in config.destination_dir.iterdir() if d.is_dir()]
    elif cat_query in ["type a", "type_a", "type-a", "a"]:
        target_dirs = [
            d for d in config.destination_dir.iterdir()
            if d.is_dir() and map_category_to_type(d.name) == "Type A"
        ]
    elif cat_query in ["type b", "type_b", "type-b", "b"]:
        target_dirs = [
            d for d in config.destination_dir.iterdir()
            if d.is_dir() and map_category_to_type(d.name) == "Type B"
        ]
    elif cat_query in ["unclassified", "unclass"]:
        target_dirs = [
            d for d in config.destination_dir.iterdir()
            if d.is_dir() and map_category_to_type(d.name) == "Unclassified"
        ]
    else:
        exact_dir = config.destination_dir / category
        if exact_dir.exists() and exact_dir.is_dir():
            target_dirs = [exact_dir]

    if not target_dirs:
        raise HTTPException(status_code=404, detail=f"No files or directory found for category: {category}")

    # Build in-memory zip
    zip_buffer = io.BytesIO()
    file_count = 0
    supported = [ext.lower() for ext in config.supported_extensions]

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for cat_d in target_dirs:
            for file_path in cat_d.iterdir():
                if file_path.is_file() and not file_path.name.startswith(".") and file_path.suffix.lower() in supported:
                    arcname = f"{cat_d.name}/{file_path.name}"
                    zf.write(file_path, arcname=arcname)
                    file_count += 1

    if file_count == 0:
        raise HTTPException(status_code=404, detail="No matching images found to bundle into archive")

    zip_buffer.seek(0)
    safe_filename = f"prismashift_{category.lower().replace(' ', '_')}_{file_count}_files.zip"

    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe_filename}"'}
    )


@app.post("/api/scan")
async def trigger_manual_scan(background_tasks: BackgroundTasks, dry_run: bool = False):
    """
    POST /api/scan: Trigger immediate manual scan of the inbox.
    """
    scanner = BatchScanner(config, classifier, organizer)
    results = scanner.scan()

    processed = []
    for src, final, cat in results:
        processed.append({
            "source": src.name,
            "final": final.name if final else None,
            "category": cat,
            "status": "success" if final else "failed"
        })

    return {
        "status": "completed",
        "scanned_count": len(results),
        "results": processed
    }


# SSE Stream for Live Telemetry
@app.get("/api/events")
async def sse_telemetry_stream():
    """
    SSE stream providing real-time telemetry updates to dashboard clients.
    """
    async def event_generator():
        import json
        while True:
            try:
                # Calculate quick status
                queue_count = 0
                if config.inbox_dir.exists():
                    queue_count = sum(1 for f in config.inbox_dir.iterdir() if f.is_file() and not f.name.startswith("."))

                with ledger._get_connection() as conn:
                    cursor = conn.execute("SELECT count(*) FROM transactions WHERE status = 'COMPLETED'")
                    total_sorted = cursor.fetchone()[0]

                data = {
                    "inbox_queue": queue_count,
                    "total_sorted": total_sorted,
                    "timestamp": asyncio.get_event_loop().time()
                }
                yield f"data: {json.dumps(data)}\n\n"
                await asyncio.sleep(2.0)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(2.0)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.api:app", host="0.0.0.0", port=8080, reload=True)
