import time
import pytest
from pathlib import Path
from PIL import Image

from src.config import Config, RouteMode
from src.db import DatabaseLedger, TransactionRecord
from src.classifier import (
    ImageClassifier,
    extract_metadata,
    ExifCameraStrategy,
    OrientationStrategy,
    ResolutionStrategy,
)
from src.organizer import FileOrganizer, RouteAction
from src.watcher import BatchScanner, DirectoryWatcher, wait_until_file_stable


@pytest.fixture
def tmp_workspace(tmp_path):
    inbox = tmp_path / "inbox"
    dest = tmp_path / "organized"
    db_file = tmp_path / "test.db"
    inbox.mkdir()
    dest.mkdir()
    cfg = Config(
        inbox_dir=inbox,
        destination_dir=dest,
        db_path=db_file,
        route_mode=RouteMode.MOVE,
        debounce_seconds=0.1,
        stabilization_timeout_seconds=2.0,
    )
    ledger = DatabaseLedger(db_file)
    classifier = ImageClassifier()
    organizer = FileOrganizer(ledger)
    return {
        "inbox": inbox,
        "dest": dest,
        "db": db_file,
        "config": cfg,
        "ledger": ledger,
        "classifier": classifier,
        "organizer": organizer,
    }


def create_test_image(path: Path, width: int, height: int, color="red", exif_data=None):
    img = Image.new("RGB", (width, height), color=color)
    if exif_data:
        exif = img.getexif()
        for k, v in exif_data.items():
            exif[k] = v
        img.save(path, exif=exif)
    else:
        img.save(path)


def test_database_ledger(tmp_workspace):
    ledger = tmp_workspace["ledger"]
    tx = TransactionRecord(
        transaction_uuid="tx-123",
        file_hash="dummyhash",
        original_path="/path/a.jpg",
        new_path="/dest/a.jpg",
        action="MOVE",
        category="photos",
        rule_matched="test_rule",
    )
    tx_id = ledger.record_transaction(tx)
    assert tx_id is not None

    record = ledger.get_transaction(tx_id)
    assert record.file_hash == "dummyhash"
    assert record.status == "COMPLETED"

    ledger.update_status(tx_id, "ROLLED_BACK")
    assert ledger.get_transaction(tx_id).status == "ROLLED_BACK"


def test_classifier_orientation(tmp_workspace):
    inbox = tmp_workspace["inbox"]
    classifier = tmp_workspace["classifier"]

    portrait_img = inbox / "portrait.jpg"
    create_test_image(portrait_img, 600, 1200)

    res_portrait = classifier.classify(portrait_img)
    assert res_portrait.category == "portrait"
    assert "vertical" in res_portrait.tags or "portrait" in res_portrait.tags

    landscape_img = inbox / "landscape.jpg"
    create_test_image(landscape_img, 1200, 600)

    res_landscape = classifier.classify(landscape_img)
    assert res_landscape.category == "landscape"


def test_classifier_screenshot(tmp_workspace):
    inbox = tmp_workspace["inbox"]
    classifier = tmp_workspace["classifier"]

    screenshot_img = inbox / "Screenshot_20261019.png"
    create_test_image(screenshot_img, 1920, 1080)

    res = classifier.classify(screenshot_img)
    assert res.category == "screenshots"


def test_organizer_routing_and_collision(tmp_workspace):
    inbox = tmp_workspace["inbox"]
    dest = tmp_workspace["dest"]
    organizer = tmp_workspace["organizer"]

    file1 = inbox / "img.jpg"
    create_test_image(file1, 500, 500)

    success, path1, tx1 = organizer.route_file(
        source_path=file1,
        dest_directory=dest,
        category="square",
        action=RouteAction.MOVE,
    )
    assert success
    assert path1.exists()
    assert not file1.exists()
    assert path1.name == "img.jpg"

    # Route second file with identical name to test collision resolution
    file2 = inbox / "img.jpg"
    create_test_image(file2, 500, 500)

    success2, path2, tx2 = organizer.route_file(
        source_path=file2,
        dest_directory=dest,
        category="square",
        action=RouteAction.MOVE,
    )
    assert success2
    assert path2.exists()
    assert path2.name == "img_1.jpg"

    # Test rollback
    rolled_back = organizer.rollback_transaction(tx2.id)
    assert rolled_back
    assert not path2.exists()
    assert file2.exists()


def test_batch_scanner(tmp_workspace):
    inbox = tmp_workspace["inbox"]
    dest = tmp_workspace["dest"]
    cfg = tmp_workspace["config"]
    classifier = tmp_workspace["classifier"]
    organizer = tmp_workspace["organizer"]

    img_a = inbox / "img_a.jpg"
    img_b = inbox / "Screenshot_app.png"
    create_test_image(img_a, 800, 1600)
    create_test_image(img_b, 1000, 800)

    scanner = BatchScanner(cfg, classifier, organizer)
    results = scanner.scan()

    assert len(results) == 2
    # Verify categorized output directories
    assert (dest / "portrait").exists()
    assert (dest / "screenshots").exists()


def test_watcher_event(tmp_workspace):
    inbox = tmp_workspace["inbox"]
    dest = tmp_workspace["dest"]
    cfg = tmp_workspace["config"]
    classifier = tmp_workspace["classifier"]
    organizer = tmp_workspace["organizer"]

    processed = []

    def on_processed(src, dst, cat):
        processed.append((src, dst, cat))

    watcher = DirectoryWatcher(cfg, classifier, organizer, on_processed=on_processed)
    watcher.start()

    try:
        new_file = inbox / "watch_test.jpg"
        create_test_image(new_file, 800, 400)

        # Allow stabilization and thread processing
        for _ in range(30):
            if processed:
                break
            time.sleep(0.1)

        assert len(processed) >= 1
        assert (dest / "landscape" / "watch_test.jpg").exists()
    finally:
        watcher.stop()
