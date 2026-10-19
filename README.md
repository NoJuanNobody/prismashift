# 💠 PrismaShift

> **Intelligent, Automated Homelab Image Sorting Toolchain**  
> Complete with a zero-friction Customer Web Dashboard and an Admin CLI with instant rollback.

---

## 🌟 Why PrismaShift?

When hosting automated file workflows for clients on your homelab:
1. **End users / Customers** want simplicity: drag & drop into a browser or paste into a mapped folder. They should never touch terminals, configs, or risk losing photos.
2. **Admins / Maintainers** need deep visibility: preview rules with dry-runs, monitor network ingestion debouncing, inspect transaction logs, and roll back any operation with a single command.

PrismaShift is engineered in Python with an asynchronous inotify engine and SQLite transaction ledger. Zero JVM overhead, instant startup, low memory footprint (~40MB RAM).

---

## 🏗️ Architecture

```
[ Customer / Camera / Dropzone ]
               │
               ▼
   📥 /data/inbox (Watched Folder)
               │
               ▼
┌───────────────────────────────────────────────┐
│ 1. Debounced Ingestion Worker                 │
│    - Stabilizes file writes (SMB/NFS safe)    │
└──────────────────────┬────────────────────────┘
                       │
                       ▼
┌───────────────────────────────────────────────┐
│ 2. Extensible Classifier                      │
│    - Aspect Ratio (Landscape vs Portrait)     │
│    - EXIF Camera Tag (Camera vs Screenshot)   │
│    - Resolution / Dimension Thresholds        │
└──────────────────────┬────────────────────────┘
                       │
        ┌──────────────┴──────────────┐
        ▼                             ▼
  📂 /data/sorted/TypeA         📂 /data/sorted/TypeB
        │                             │
        └──────────────┬──────────────┘
                       │
                       ▼
┌───────────────────────────────────────────────┐
│ 3. Atomic SQLite Ledger                       │
│    - SHA-256 hashes, paths, timestamps       │
│    - 1-click batch rollback                   │
└───────────────────────────────────────────────┘
```

---

## 🚀 Quickstart (Homelab Docker)

```bash
# Clone the repository
git clone https://github.com/NoJuanNobody/prismashift.git
cd prismashift

# Spin up with Docker Compose
docker compose up -d
```

Open `http://localhost:8080` to access the customer web dashboard!

---

## 💻 Admin CLI (`prismashift-admin`)

Maintainers can interact via SSH or `docker exec`:

### Check System Status
```bash
$ ./prismashift-admin status
```

### Dry-Run Rule Testing
Preview how incoming files would be classified without touching disk:
```bash
$ ./prismashift-admin test-rules --sample 20
```

### Batch Scan Inbox
```bash
$ ./prismashift-admin scan
$ ./prismashift-admin scan --dry-run
```

### Transaction History & 1-Click Rollback
```bash
# View recent transaction batches
$ ./prismashift-admin history --limit 10

# Roll back any transaction by ID or UUID
$ ./prismashift-admin rollback <TX_ID_OR_UUID>
```

### Reprocess Files
```bash
$ ./prismashift-admin reprocess --unclassified-only
```

---

## 🌐 Customer Web Interface

- **Drag-and-Drop Ingestion Zone:** Direct browser upload with instant classification telemetry.
- **Categorized Galleries:** Filter by Type A, Type B, or Unclassified with resolution and neural confidence scores.
- **Instant ZIP Download:** One-click download of sorted category archives.
- **Real-time Activity Stream:** Live ticker showing processed photos in milliseconds.

---

## 🛠️ Configuration Options

Configured via environment variables or `.env`:

| Variable | Default | Description |
| :--- | :--- | :--- |
| `PRISMASHIFT_INBOX_DIR` | `./inbox` | Watched directory for incoming files |
| `PRISMASHIFT_ORGANIZED_DIR` | `./organized` | Destination directory for sorted categories |
| `PRISMASHIFT_DB_PATH` | `./prismashift.db` | SQLite transaction ledger file |
| `PRISMASHIFT_DEBOUNCE_SECONDS` | `1.5` | Settle time for network file transfers |
| `PRISMASHIFT_ROUTE_MODE` | `MOVE` | `MOVE`, `COPY`, or `SYMLINK` |

---

## 🧪 Testing

Run test suite:
```bash
pytest tests/
```
