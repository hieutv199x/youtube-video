from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Optional
import time
import platform
import os

def _base_dir() -> Path:
    sys = platform.system().lower()
    home = Path.home()
    if sys == "darwin":
        base = home / "Library" / "Application Support" / "YouTubeManager" / "cache"
    elif sys == "windows":
        base = Path(os.environ.get("LOCALAPPDATA", home)) / "YouTubeManager" / "cache"
    else:
        base = home / ".local" / "share" / "YouTubeManager" / "cache"
    base.mkdir(parents=True, exist_ok=True)
    return base

def cache_path(name: str) -> Path:
    return _base_dir() / name

def load_json(name: str, max_age_sec: Optional[int] = None) -> Optional[Any]:
    p = cache_path(name)
    if not p.exists():
        return None
    if max_age_sec:
        if time.time() - p.stat().st_mtime > max_age_sec:
            return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

def write_json(name: str, data: Any):
    p = cache_path(name)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
