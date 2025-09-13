from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime
from pathlib import Path
from typing import Optional, List
import uuid

class TaskStatus(Enum):
    PENDING = "pending"
    QUEUED = "queued"  # added
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

class TaskType(Enum):
    VIDEO_AUDIO = "video_audio"
    AUDIO_ONLY = "audio_only"
    VIDEO_ONLY = "video_only"

@dataclass
class DownloadTask:
    """Represents a download task."""
    
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    url: str = ""
    title: str = ""
    output_path: Optional[Path] = None
    output_format: str = "mp4"
    task_type: TaskType = TaskType.VIDEO_AUDIO
    status: TaskStatus = TaskStatus.PENDING
    progress: float = 0.0
    file_size: Optional[int] = None
    download_speed: Optional[str] = None
    eta: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    
    # Split options
    should_split: bool = True
    segment_duration: int = 120
    title_prefix: str = "Part"
    segments: List[Path] = field(default_factory=list)
    overlay_title: str | None = None
    # Single-dimension (legacy) might still be used elsewhere; keep if present
    resolution_width: int | None = None
    resolution_height: int | None = None
