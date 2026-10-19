"""
Background filesystem observer using watchdog with file stabilization/debouncing,
plus one-shot batch scan capabilities for PrismaShift.
"""

from __future__ import annotations

import time
import threading
from pathlib import Path
from typing import Optional, Callable, Set, List
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileCreatedEvent, FileModifiedEvent, FileMovedEvent

from .config import Config, RouteMode
from .classifier import ImageClassifier
from .organizer import FileOrganizer, RouteAction
from .db import DatabaseLedger


def wait_until_file_stable(file_path: Path, debounce_sec: float = 1.0, timeout_sec: float = 10.0) -> bool:
    """
    Polls the file to verify that write operations are complete and file size is stable.
    Returns True when stable, False if timeout or deleted.
    """
    start_time = time.time()
    last_size = -1

    while time.time() - start_time < timeout_sec:
        if not file_path.exists():
            return False

        try:
            current_size = file_path.stat().st_size
            if current_size == last_size and current_size > 0:
                # Size remained constant over debounce duration
                return True
            last_size = current_size
        except OSError:
            # File might be locked during write
            pass

        time.sleep(debounce_sec)

    return file_path.exists() and file_path.stat().st_size > 0


class StabilizationHandler(FileSystemEventHandler):
    """
    Debounced event handler that tracks active files and processes them only after
    writes have stabilized.
    """
    def __init__(
        self,
        config: Config,
        classifier: ImageClassifier,
        organizer: FileOrganizer,
        on_processed: Optional[Callable[[Path, Path, str], None]] = None,
    ):
        super().__init__()
        self.config = config
        self.classifier = classifier
        self.organizer = organizer
        self.on_processed = on_processed
        self._processing_paths: Set[str] = set()
        self._lock = threading.Lock()

    def _should_handle(self, path: Path) -> bool:
        if not path.is_file():
            return False
        if path.name.startswith((".", "~$")):
            return False
        return path.suffix.lower() in [ext.lower() for ext in self.config.supported_extensions]

    def _process_file(self, file_path: Path):
        path_str = str(file_path)
        with self._lock:
            if path_str in self._processing_paths:
                return
            self._processing_paths.add(path_str)

        try:
            # Stabilization wait
            is_stable = wait_until_file_stable(
                file_path,
                debounce_sec=self.config.debounce_seconds,
                timeout_sec=self.config.stabilization_timeout_seconds,
            )
            if not is_stable or not file_path.exists():
                return

            # Classify
            result = self.classifier.classify(file_path)

            # Route
            action = RouteAction[self.config.route_mode.value]
            success, final_path, _ = self.organizer.route_file(
                source_path=file_path,
                dest_directory=self.config.destination_dir,
                category=result.category,
                rule_matched=result.rule_matched,
                action=action,
            )

            if success and final_path and self.on_processed:
                self.on_processed(file_path, final_path, result.category)

        finally:
            with self._lock:
                self._processing_paths.discard(path_str)

    def on_created(self, event):
        if not event.is_directory:
            p = Path(event.src_path)
            if self._should_handle(p):
                # Run stabilization and processing in a worker thread to keep watchdog responsive
                threading.Thread(target=self._process_file, args=(p,), daemon=True).start()

    def on_modified(self, event):
        if not event.is_directory:
            p = Path(event.src_path)
            if self._should_handle(p):
                threading.Thread(target=self._process_file, args=(p,), daemon=True).start()

    def on_moved(self, event):
        if not event.is_directory:
            p = Path(event.dest_path)
            if self._should_handle(p):
                threading.Thread(target=self._process_file, args=(p,), daemon=True).start()


class DirectoryWatcher:
    """
    Watches an inbox directory continuously using watchdog.
    """
    def __init__(
        self,
        config: Config,
        classifier: ImageClassifier,
        organizer: FileOrganizer,
        on_processed: Optional[Callable[[Path, Path, str], None]] = None,
    ):
        self.config = config
        self.inbox_dir = self.config.inbox_dir
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        self.handler = StabilizationHandler(config, classifier, organizer, on_processed)
        self.observer = Observer()

    def start(self):
        """Starts background file monitoring."""
        self.observer.schedule(self.handler, str(self.inbox_dir), recursive=False)
        self.observer.start()

    def stop(self):
        """Stops background file monitoring."""
        self.observer.stop()
        self.observer.join()

    def is_alive(self) -> bool:
        return self.observer.is_alive()


class BatchScanner:
    """
    One-shot synchronous batch scanner for an inbox or arbitrary directory.
    """
    def __init__(self, config: Config, classifier: ImageClassifier, organizer: FileOrganizer):
        self.config = config
        self.classifier = classifier
        self.organizer = organizer

    def scan(self, directory: Optional[Path | str] = None) -> List[Tuple[Path, Optional[Path], str]]:
        """
        Scans directory for supported image files and processes them.
        Returns list of tuples: (source_path, final_path, category)
        """
        scan_dir = Path(directory) if directory else self.config.inbox_dir
        results: List[Tuple[Path, Optional[Path], str]] = []

        if not scan_dir.exists():
            return results

        supported = [ext.lower() for ext in self.config.supported_extensions]
        action = RouteAction[self.config.route_mode.value]

        # Scan files in directory
        for p in scan_dir.iterdir():
            if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in supported:
                # Classify
                classification = self.classifier.classify(p)
                success, final_path, _ = self.organizer.route_file(
                    source_path=p,
                    dest_directory=self.config.destination_dir,
                    category=classification.category,
                    rule_matched=classification.rule_matched,
                    action=action,
                )
                if success and final_path:
                    results.append((p, final_path, classification.category))
                else:
                    results.append((p, None, classification.category))

        return results
