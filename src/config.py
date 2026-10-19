"""
Configuration management for PrismaShift.
Supports Pydantic BaseModel with environment variable overrides and JSON/YAML config loading.
"""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Any
from pydantic import BaseModel, Field


class RouteMode(str, Enum):
    COPY = "COPY"
    MOVE = "MOVE"
    SYMLINK = "SYMLINK"


class ClassificationRule(BaseModel):
    name: str
    target_subdir: str
    description: Optional[str] = None
    priority: int = 100  # Lower number = higher priority
    enabled: bool = True
    conditions: Dict[str, Any] = Field(default_factory=dict)


class Config(BaseModel):
    """
    Main application configuration.
    """
    # Directory paths
    inbox_dir: Path = Field(
        default_factory=lambda: Path(os.getenv("PRISMASHIFT_INBOX_DIR", "./inbox")).resolve()
    )
    destination_dir: Path = Field(
        default_factory=lambda: Path(os.getenv("PRISMASHIFT_DEST_DIR", "./organized")).resolve()
    )
    db_path: Path = Field(
        default_factory=lambda: Path(os.getenv("PRISMASHIFT_DB_PATH", "./prismashift.db")).resolve()
    )

    # Behavior settings
    route_mode: RouteMode = Field(
        default_factory=lambda: RouteMode(os.getenv("PRISMASHIFT_ROUTE_MODE", "MOVE").upper())
    )
    debounce_seconds: float = Field(
        default_factory=lambda: float(os.getenv("PRISMASHIFT_DEBOUNCE_SECONDS", "1.5"))
    )
    stabilization_timeout_seconds: float = Field(
        default_factory=lambda: float(os.getenv("PRISMASHIFT_STABILIZATION_TIMEOUT", "10.0"))
    )
    supported_extensions: List[str] = Field(
        default_factory=lambda: [
            ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff", ".tif", ".heic", ".avif"
        ]
    )

    # Resolution & Dimension thresholds (pixels)
    high_res_min_pixels: int = Field(
        default_factory=lambda: int(os.getenv("PRISMASHIFT_HIGH_RES_MIN", "8294400"))  # 4K / ~8MP
    )
    low_res_max_pixels: int = Field(
        default_factory=lambda: int(os.getenv("PRISMASHIFT_LOW_RES_MAX", "921600"))   # ~720p / <1MP
    )

    # Rules mapping
    rules: List[ClassificationRule] = Field(default_factory=list)

    # Fallback category name if no specific rule matches
    fallback_category: str = Field(
        default_factory=lambda: os.getenv("PRISMASHIFT_FALLBACK_CATEGORY", "unclassified")
    )

    model_config = {"arbitrary_types_allowed": True}


def load_config(config_path: Optional[str | Path] = None) -> Config:
    """
    Loads config from file if provided, with environment variable fallbacks.
    """
    if config_path and Path(config_path).exists():
        import json
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return Config(**data)

    return Config()
