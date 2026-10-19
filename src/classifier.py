"""
Modular image classification engine with pluggable strategies.
Extracts metadata, dimensions, EXIF, and evaluates configured classification rules.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
from PIL import Image, ExifTags


@dataclass
class ImageMetadata:
    path: Path
    format: Optional[str] = None
    width: int = 0
    height: int = 0
    aspect_ratio: float = 1.0  # width / height
    orientation: str = "square"  # portrait, landscape, square
    megapixels: float = 0.0
    total_pixels: int = 0
    has_exif: bool = False
    camera_make: Optional[str] = None
    camera_model: Optional[str] = None
    software: Optional[str] = None
    date_taken: Optional[str] = None
    raw_exif: Dict[str, Any] = field(default_factory=dict)
    extra_tags: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ClassificationResult:
    category: str
    rule_matched: str
    confidence: float
    metadata: ImageMetadata
    tags: List[str] = field(default_factory=list)


def extract_metadata(image_path: Path) -> Optional[ImageMetadata]:
    """
    Safely inspects and extracts image metadata and EXIF tags without loading full pixel buffers.
    """
    try:
        with Image.open(image_path) as img:
            width, height = img.size
            img_format = img.format
            raw_exif: Dict[str, Any] = {}
            camera_make = None
            camera_model = None
            software = None
            date_taken = None
            has_exif = False

            # Extract EXIF if available
            try:
                exif_data = img._getexif()
                if exif_data:
                    has_exif = True
                    for tag_id, val in exif_data.items():
                        tag_name = ExifTags.TAGS.get(tag_id, str(tag_id))
                        # Filter/convert bytes to str if needed
                        if isinstance(val, bytes):
                            try:
                                val = val.decode("utf-8", errors="ignore").strip()
                            except Exception:
                                val = str(val)
                        raw_exif[tag_name] = val

                    camera_make = raw_exif.get("Make")
                    camera_model = raw_exif.get("Model")
                    software = raw_exif.get("Software")
                    date_taken = raw_exif.get("DateTimeOriginal") or raw_exif.get("DateTime")

                    # Handle EXIF Orientation tag for dimension flipping
                    orientation_tag = raw_exif.get("Orientation")
                    if orientation_tag in [5, 6, 7, 8]:
                        # 90 or 270 degree rotation
                        width, height = height, width
            except Exception:
                pass

            total_pixels = width * height
            megapixels = round(total_pixels / 1_000_000.0, 2)
            aspect_ratio = round(width / float(height), 4) if height > 0 else 1.0

            if aspect_ratio > 1.05:
                orientation = "landscape"
            elif aspect_ratio < 0.95:
                orientation = "portrait"
            else:
                orientation = "square"

            return ImageMetadata(
                path=image_path,
                format=img_format,
                width=width,
                height=height,
                aspect_ratio=aspect_ratio,
                orientation=orientation,
                megapixels=megapixels,
                total_pixels=total_pixels,
                has_exif=has_exif,
                camera_make=camera_make,
                camera_model=camera_model,
                software=software,
                date_taken=date_taken,
                raw_exif=raw_exif,
            )
    except Exception:
        return None


class BaseStrategy(ABC):
    """
    Abstract classification strategy plugin.
    """
    @abstractmethod
    def evaluate(self, meta: ImageMetadata) -> Optional[Tuple[str, str, float, List[str]]]:
        """
        Evaluates metadata.
        Returns Optional tuple of: (category, rule_name, confidence, tags)
        or None if this strategy has no match.
        """
        pass


class ExifCameraStrategy(BaseStrategy):
    """
    Classifies photos taken by real cameras vs digital graphics / screenshots.
    Detects smartphone/camera EXIF (Make/Model/Lens/Aperture).
    Detects screenshot software signatures or lack of EXIF.
    """
    def evaluate(self, meta: ImageMetadata) -> Optional[Tuple[str, str, float, List[str]]]:
        # Check screenshot clues in software tag or filename
        stem = meta.path.stem.lower()
        if (
            "screenshot" in stem
            or "screen shot" in stem
            or "captura de pantalla" in stem
            or "snip" in stem
        ):
            return ("screenshots", "filename_screenshot_keyword", 0.95, ["screenshot", "screen-capture"])

        if meta.software:
            soft = meta.software.lower()
            if any(k in soft for k in ["screenshot", "capture", "snip", "sharex"]):
                return ("screenshots", "software_screenshot_detected", 0.95, ["screenshot"])

        # Real camera detection
        if meta.camera_make or meta.camera_model:
            tags = ["photo", "camera"]
            if meta.camera_make:
                tags.append(str(meta.camera_make).strip())
            return ("photos", "exif_camera_hardware_present", 0.90, tags)

        return None


class OrientationStrategy(BaseStrategy):
    """
    Classifies based on portrait vs landscape orientation.
    Useful for wallpapers, mobile screens, or photo print preparation.
    """
    def evaluate(self, meta: ImageMetadata) -> Optional[Tuple[str, str, float, List[str]]]:
        if meta.orientation == "portrait":
            return ("portrait", "aspect_portrait", 0.85, ["portrait", "vertical"])
        elif meta.orientation == "landscape":
            return ("landscape", "aspect_landscape", 0.85, ["landscape", "horizontal"])
        elif meta.orientation == "square":
            return ("square", "aspect_square", 0.85, ["square", "1:1"])
        return None


class ResolutionStrategy(BaseStrategy):
    """
    Classifies images into UltraHD / High-Res vs Web / Low-Res / Thumbnail.
    """
    def __init__(self, high_res_min: int = 8_294_400, low_res_max: int = 921_600):
        self.high_res_min = high_res_min
        self.low_res_max = low_res_max

    def evaluate(self, meta: ImageMetadata) -> Optional[Tuple[str, str, float, List[str]]]:
        if meta.total_pixels >= self.high_res_min:
            return ("high_res", "resolution_ultra_hd", 0.88, ["4k+", "wallpaper", "high-res"])
        elif meta.total_pixels <= self.low_res_max:
            return ("low_res", "resolution_small", 0.88, ["thumbnail", "low-res", "web"])
        return None


class ImageClassifier:
    """
    Primary orchestrator for classification.
    Runs active strategies in priority sequence, returning matched category or fallback.
    """
    def __init__(
        self,
        strategies: Optional[List[BaseStrategy]] = None,
        fallback_category: str = "unclassified",
    ):
        self.fallback_category = fallback_category
        if strategies is not None:
            self.strategies = strategies
        else:
            # Default pipeline
            self.strategies = [
                ExifCameraStrategy(),
                OrientationStrategy(),
                ResolutionStrategy(),
            ]

    def add_strategy(self, strategy: BaseStrategy, at_front: bool = False):
        if at_front:
            self.strategies.insert(0, strategy)
        else:
            self.strategies.append(strategy)

    def classify(self, image_path: Path | str) -> ClassificationResult:
        """
        Inspects image file and returns ClassificationResult.
        Guaranteed not to raise an unhandled exception on corrupt/unreadable files.
        """
        path = Path(image_path).resolve()
        meta = extract_metadata(path)

        if meta is None:
            # Corrupted or unreadable image
            empty_meta = ImageMetadata(path=path)
            return ClassificationResult(
                category=self.fallback_category,
                rule_matched="unreadable_or_corrupt",
                confidence=0.0,
                metadata=empty_meta,
                tags=["corrupt", "unreadable"],
            )

        # Run registered strategies in order
        for strategy in self.strategies:
            match = strategy.evaluate(meta)
            if match is not None:
                category, rule_name, conf, tags = match
                return ClassificationResult(
                    category=category,
                    rule_matched=rule_name,
                    confidence=conf,
                    metadata=meta,
                    tags=tags,
                )

        # If no strategy matched, return fallback
        return ClassificationResult(
            category=self.fallback_category,
            rule_matched="default_fallback",
            confidence=0.5,
            metadata=meta,
            tags=["fallback"],
        )
