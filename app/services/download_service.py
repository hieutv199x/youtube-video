import logging
from pathlib import Path
from typing import Callable, Optional
from PyQt6.QtCore import QObject, pyqtSignal, QThread
import yt_dlp
import subprocess
from app.models.download_task import DownloadTask, TaskStatus, TaskType
from app.core.config import Config
import os
import shutil
import sys  # ensure present
import platform
import textwrap
from concurrent.futures import ThreadPoolExecutor, as_completed
import math
try:
    import psutil  # optional
except Exception:
    psutil = None

logger = logging.getLogger(__name__)

def get_video_duration(input_path):
    """Get duration of a video file in seconds using ffprobe."""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(input_path)
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return float(result.stdout.strip())

def break_and_pad(text: str, width: int = 20) -> str:
    """
    Break text into lines of max `width` characters,
    then pad each line with spaces at start and end
    so all lines are the same length as the longest one.
    """
    # Step 1: wrap text
    lines = textwrap.wrap(text, width=width)

    # Step 2: find the longest line length
    max_len = max(len(line) for line in lines)

    # Step 3: pad each line (left + right)
    padded_lines = []
    for line in lines:
        extra = max_len - len(line)
        left = extra // 2
        right = extra - left
        padded_lines.append(" " * right + line + " " * left)

    return "\n".join(padded_lines)

def _find_font_file() -> Optional[Path]:
    """
    Locate the overlay font for both dev and packaged (frozen) builds.
    Search order:
      1. Env var YT_FONT_FILE (if set)
      2. runtime_base_dir()/font/KeinannPOP.ttf (inside packaged app)
      3. runtime_base_dir()/resources/font/KeinannPOP.ttf
      4. Original absolute development path
    """
    candidates = []
    env_font = os.environ.get("YT_FONT_FILE")
    if env_font:
        candidates.append(Path(env_font))
    base = _runtime_base_dir()
    candidates.extend([
        base / "font" / "KeinannPOP.ttf",
        base / "resources" / "font" / "KeinannPOP.ttf",
        Path("/Users/hieutran/Work/Personal/youtube/font/KeinannPOP.ttf"),
    ])
    for c in candidates:
        if c and c.exists():
            return c
    return None

def split_and_mark_video(input_path, outfolder="downloads", segment_duration=120,
                         title_prefix="Part", video_title=None, h=1920, w=1080):
    """Split video into segments and overlay part label (top) and full video title (bottom)."""
    outdir = Path(outfolder)
    outdir.mkdir(parents=True, exist_ok=True)
    duration = get_video_duration(input_path)
    if video_title is None:
        # Derive from filename if not provided
        stem = input_path.stem
        video_title = stem.rsplit(" - ", 1)[0] if " - " in stem else stem
    safe_title = break_and_pad(video_title, 30)  # wrap long titles for overlay

    # Font file integration (prod-aware)
    font_file = _find_font_file()
    font_param = ""
    if font_file:
        # Escape backslashes and colons for ffmpeg drawtext parser
        escaped_font_path = str(font_file).replace("\\", "\\\\").replace(":", "\\:")
        font_param = f"fontfile='{escaped_font_path}'"

    num_parts = int(duration // segment_duration) + (1 if duration % segment_duration > 0 else 0)
    segments = []

    # Build task descriptors first
    tasks = []
    for i in range(num_parts):
        start = i * segment_duration
        out_file = outdir / f"{input_path.stem}_{title_prefix.lower().replace(' ', '_')}{i+1}{input_path.suffix}"
        top_text = f"{title_prefix} {i+1}"
        tasks.append((i, start, out_file, top_text))

    def _run_segment(idx, start, out_file, top_text):
        vf = (
            f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
            f"drawtext={font_param + ':' if font_param else ''}text='{safe_title}':fontcolor=black:fontsize=36:"
            f"x=(w-text_w)/2:y=h/4-text_h:box=1:boxcolor=yellow@1:boxborderw=10,"
            f"drawtext={font_param + ':' if font_param else ''}text='{top_text}':fontcolor=black:fontsize=48:"
            f"x=(w-text_w)/2:y=h-text_h-h/4:box=1:boxcolor=yellow@1:boxborderw=10"
        )
        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-ss", str(start),
            "-t", str(segment_duration),
            "-vf", vf,
            "-c:a", "copy",
            str(out_file)
        ]
        subprocess.run(cmd, check=True)
        return idx, out_file

    # Parallel execution
    max_workers = min(
        len(tasks),
        max(1, int(os.environ.get("YT_SPLIT_WORKERS", "4")))
    )
    errors = []
    results = {}
    if tasks:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(_run_segment, idx, start, out_file, top_text): idx
                for (idx, start, out_file, top_text) in tasks
            }
            for fut in as_completed(future_map):
                try:
                    idx, produced = fut.result()
                    results[idx] = produced
                    print(f"Created {produced}")
                except Exception as e:
                    errors.append(e)
        if errors:
            # Optionally remove any partially created segments on failure
            raise RuntimeError(
                f"One or more segments failed ({len(errors)}/{len(tasks)}). First error: {errors[0]}"
            )
        # Preserve original order
        segments = [results[i] for i in range(len(tasks))]
    return segments

def _runtime_base_dir():
    """Return base dir in dev or frozen mode."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent.parent))
    return Path(__file__).resolve().parent.parent.parent

def _candidate_ffmpeg_paths():
    base = _runtime_base_dir()
    # In a macOS .app the executable lives in .../Contents/MacOS
    return [
        base / "ffmpeg",
        base / "ffprobe",
        base.parent / "ffmpeg",
        base.parent / "ffprobe",
        base.parent.parent / "ffmpeg",
        base.parent.parent / "ffprobe",
        base / "ffmpeg.exe",
        base / "ffprobe.exe",
        base / "vendor" / "ffmpeg" / "macos" / "ffmpeg",
        base / "vendor" / "ffmpeg" / "macos" / "ffprobe",
        base / "vendor" / "ffmpeg" / "windows" / "ffmpeg.exe",
        base / "vendor" / "ffmpeg" / "windows" / "ffprobe.exe",
    ]

def _locate_ffmpeg() -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Try to locate ffmpeg & ffprobe.
    Search order:
      1. Existing PATH
      2. Bundled binaries in runtime base dir
      3. vendor/ffmpeg subfolder (if packaged)
    Returns (ffmpeg_path, ffprobe_path, ffmpeg_dir)
    """
    # Try PATH first
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg and ffprobe:
        return ffmpeg, ffprobe, str(Path(ffmpeg).parent)
    # Try bundled candidates
    found_ffmpeg = None
    found_ffprobe = None
    for p in _candidate_ffmpeg_paths():
        if p.exists() and p.is_file():
            if "ffprobe" in p.name and not found_ffprobe:
                found_ffprobe = str(p)
            elif "ffmpeg" in p.name and not found_ffmpeg:
                found_ffmpeg = str(p)
    if found_ffmpeg and found_ffprobe:
        return found_ffmpeg, found_ffprobe, str(Path(found_ffmpeg).parent)
    return None, None, None

def _ffmpeg_available():
    f, p, _ = _locate_ffmpeg()
    return bool(f and p)

class DownloadWorker(QThread):
    """Worker thread for downloading videos."""
    
    progress_updated = pyqtSignal(str, float, str, str)  # task_id, progress, speed, eta
    status_changed = pyqtSignal(str, TaskStatus)  # task_id, status
    error_occurred = pyqtSignal(str, str)  # task_id, error_message
    download_completed = pyqtSignal(str, str, list)  # task_id, file_path, segments
    
    def __init__(self, task: DownloadTask):
        super().__init__()
        self.task = task
        self._cancelled = False
    
    def cancel(self):
        """Cancel the download."""
        self._cancelled = True
    
    def run(self):
        """Execute the download task."""
        try:
            if self._cancelled:
                return
            self.status_changed.emit(self.task.id, TaskStatus.DOWNLOADING)

            ffmpeg_path, ffprobe_path, ffmpeg_dir = _locate_ffmpeg()
            has_ffmpeg = bool(ffmpeg_path and ffprobe_path)

            # Pre-fetch metadata for title (safe even without ffmpeg)
            preliminary_opts = {
                'quiet': True,
                'skip_download': True,
                'ignoreerrors': False,
            }
            try:
                with yt_dlp.YoutubeDL(preliminary_opts) as meta_ydl:
                    info = meta_ydl.extract_info(self.task.url, download=False)
                    if info:
                        self.task.title = info.get('title') or self.task.title
            except Exception:
                pass  # keep going

            splitting_required = self.task.should_split
            video_processing_required = (self.task.task_type != TaskType.AUDIO_ONLY)

            # Fallback mode: allow simple progressive download if only merge was needed
            fallback_no_ffmpeg = (
                not has_ffmpeg
                and video_processing_required
                and not splitting_required
                and self.task.task_type == TaskType.VIDEO_AUDIO
            )

            if not has_ffmpeg and not fallback_no_ffmpeg:
                if video_processing_required or splitting_required:
                    raise RuntimeError(
                        "FFmpeg not found. Required for this operation (video muxing / splitting)."
                    )

            if ffmpeg_dir:
                os.environ["PATH"] = ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")

            outdir = Config.DOWNLOADS_DIR
            outdir.mkdir(parents=True, exist_ok=True)

            def progress_hook(d):
                if self._cancelled:
                    raise Exception("Download cancelled by user")
                if d.get('status') == 'downloading':
                    progress = 0.0
                    total = d.get('total_bytes') or d.get('total_bytes_estimate')
                    if total:
                        progress = (d.get('downloaded_bytes', 0) / total) * 100
                    speed = d.get('speed') or 0
                    eta = d.get('eta') or 0
                    speed_str = f"{speed/1024/1024:.1f} MB/s" if speed else "Unknown"
                    eta_str = f"{eta}s" if eta else "Unknown"
                    self.progress_updated.emit(self.task.id, progress, speed_str, eta_str)

            # Determine format selector (may override for fallback)
            if fallback_no_ffmpeg:
                # Use a single progressive format (no separate A/V -> no merge)
                format_selector = "best[ext=mp4]/best"
            else:
                format_selector = self._get_format_selector()

            ydl_opts = {
                'outtmpl': str(outdir / '%(title)s - %(id)s.%(ext)s'),
                'format': format_selector,
                'merge_output_format': None if fallback_no_ffmpeg else self.task.output_format,
                'progress_hooks': [progress_hook],
                'ignoreerrors': False,
            }
            if ffmpeg_dir and has_ffmpeg:
                ydl_opts['ffmpeg_location'] = ffmpeg_dir

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(self.task.url, download=True)
                if info and not self.task.title:
                    self.task.title = info.get('title') or self.task.title

                # Try both output_format and mp4 when fallback used
                search_ext = self.task.output_format if not fallback_no_ffmpeg else "mp4"
                downloaded_files = list(outdir.glob(f"*{info['id']}*.{search_ext}")) if info else []
                if not downloaded_files:
                    # Last resort: any file with the id
                    downloaded_files = list(outdir.glob(f"*{info['id']}*"))
                if not downloaded_files:
                    raise Exception("Downloaded file not found after yt-dlp run.")
                file_path = downloaded_files[0]
                segments = []

                if self.task.should_split:
                    if not has_ffmpeg:
                        raise RuntimeError("Splitting requested but ffmpeg is not available.")
                    self.status_changed.emit(self.task.id, TaskStatus.PROCESSING)
                    try:
                        segments = split_and_mark_video(
                            file_path,
                            str(outdir),
                            self.task.segment_duration,
                            self.task.title_prefix,
                            self.task.overlay_title or self.task.title
                        )
                    except Exception as split_error:
                        logger.warning(f"Failed to split video: {split_error}")

                self.download_completed.emit(self.task.id, str(file_path), segments)
                self.status_changed.emit(self.task.id, TaskStatus.COMPLETED)

        except Exception as e:
            msg = str(e)
            if "ffmpeg" in msg.lower():
                msg += (
                    "\nHint: Add ffmpeg & ffprobe to PATH or bundle them under vendor/ffmpeg/<platform>/."
                    "\n(Automatic fallback used only for simple non-split progressive downloads.)"
                )
            logger.error(f"Download failed for task {self.task.id}: {msg}")
            self.error_occurred.emit(self.task.id, msg)
            self.status_changed.emit(self.task.id, TaskStatus.FAILED)
    
    def _get_format_selector(self) -> str:
        """Select format with optional width/height constraints."""
        w = getattr(self.task, "resolution_width", None)
        h = getattr(self.task, "resolution_height", None)
        legacy_h = getattr(self.task, "resolution", None) if not h else None
        if legacy_h and not h:
            h = legacy_h
        if self.task.task_type == TaskType.AUDIO_ONLY:
            return "bestaudio/best"
        constraint = ""
        if h:
            constraint += f"[height<={h}]"
        if w:
            constraint += f"[width<={w}]"
        if self.task.task_type == TaskType.VIDEO_ONLY:
            return f"bestvideo{constraint}/bestvideo"
        return (
            f"bestvideo{constraint}[ext=mp4]+bestaudio[ext=m4a]/"
            f"best{constraint}[ext=mp4]/best{constraint}/bv*+ba/b"
        )

def _compute_resource_limits():
    """
    Return dict with tuned limits based on CPU cores and (optionally) RAM.
    Heuristics:
      - max_concurrent_downloads ~ cores/4 (min 1, max 4; reduced if low RAM)
      - max_split_workers ~ cores/2 (min 1, max 8; reduced if low RAM)
    Environment vars (YT_MAX_CONCURRENT / YT_SPLIT_WORKERS) override but are capped by safe maxima.
    """
    cores = os.cpu_count() or 2
    total_gb = None
    if psutil:
        try:
            total_gb = psutil.virtual_memory().total / (1024**3)
        except Exception:
            total_gb = None

    # Base recommendations
    rec_concurrent = min(4, max(1, cores // 4 or 1))
    rec_split = min(8, max(1, cores // 2 or 1))

    # Adjust for low memory (<4GB => be conservative)
    if total_gb is not None and total_gb < 4:
        rec_concurrent = min(rec_concurrent, 1)
        rec_split = min(rec_split, 2)

    # Apply env overrides (capped)
    env_concurrent = os.environ.get("YT_MAX_CONCURRENT")
    if env_concurrent and env_concurrent.isdigit():
        rec_concurrent = max(1, min(int(env_concurrent), 8))

    env_split = os.environ.get("YT_SPLIT_WORKERS")
    if env_split and env_split.isdigit():
        rec_split = max(1, min(int(env_split), 16))

    return {
        "max_concurrent_downloads": rec_concurrent,
        "max_split_workers": rec_split,
        "cores": cores,
        "total_ram_gb": total_gb
    }

def _stable_download_dir() -> Path:
    """Return a stable, platform-appropriate download directory."""
    system = platform.system().lower()
    home = Path.home()
    if system == "darwin":  # macOS
        base = home / "Library" / "Application Support" / "YouTubeManager" / "Downloads"
    elif system == "windows":
        local_app = Path(os.environ.get("LOCALAPPDATA", home))
        base = local_app / "YouTubeManager" / "Downloads"
    else:  # linux / others
        base = home / ".local" / "share" / "YouTubeManager" / "Downloads"
    base.mkdir(parents=True, exist_ok=True)
    return base

class DownloadService(QObject):
    """Service for managing download operations."""
    
    task_added = pyqtSignal(DownloadTask)
    task_updated = pyqtSignal(DownloadTask)
    
    def __init__(self):
        super().__init__()
        # Override Config.DOWNLOADS_DIR once with a stable absolute path
        try:
            Config.DOWNLOADS_DIR = _stable_download_dir()
        except Exception as e:
            # Fallback to existing value if something unexpected happens
            logger.warning(f"Failed to set stable download dir, using default: {e}")
        self.active_workers = {}
        self.tasks = {}
        self.queue = []  # task ids waiting
        self.max_concurrent = Config.MAX_CONCURRENT_DOWNLOADS
        self._apply_resource_tuning()

    def _apply_resource_tuning(self):
        limits = _compute_resource_limits()
        # Store for inspection
        self.resource_limits = limits
        # Override configured max_concurrent only if higher than recommendation
        if self.max_concurrent > limits["max_concurrent_downloads"]:
            self.max_concurrent = limits["max_concurrent_downloads"]
        # Ensure split workers env set (only set if missing)
        if not os.environ.get("YT_SPLIT_WORKERS"):
            os.environ["YT_SPLIT_WORKERS"] = str(limits["max_split_workers"])
        # Soft ceiling for total queued+active tasks (multiplier)
        self.capacity_task_limit = self.max_concurrent * 5
        logger.info(
            f"Resource tuning applied: max_concurrent_downloads={self.max_concurrent}, "
            f"split_workers={os.environ.get('YT_SPLIT_WORKERS')}, "
            f"cores={limits['cores']}, ram_gb={limits['total_ram_gb'] or 'unknown'}"
        )

    def add_download_task(self, url: str, task_type: TaskType = TaskType.VIDEO_AUDIO,
                          output_format: Optional[str] = None, should_split: bool = False,
                          segment_duration: int = 120, title_prefix: str = "Part",
                          overlay_title: str | None = None,
                          resolution: int | None = None,
                          resolution_width: int | None = None,
                          resolution_height: int | None = None) -> DownloadTask:
        # Capacity advisory (does not block, only warns)
        active_or_queued = sum(
            1 for t in self.tasks.values()
            if t.status in (TaskStatus.DOWNLOADING, TaskStatus.PROCESSING, TaskStatus.QUEUED)
        )
        if hasattr(self, "capacity_task_limit") and active_or_queued >= self.capacity_task_limit:
            logger.warning(
                "High load: active/queued tasks=%s exceed soft capacity=%s. "
                "Performance may degrade.",
                active_or_queued, self.capacity_task_limit
            )
        if resolution and not resolution_height:
            resolution_height = resolution
        task = DownloadTask(
            url=url,
            task_type=task_type,
            output_format=output_format or Config.DEFAULT_OUTPUT_FORMAT,
            should_split=should_split,
            segment_duration=segment_duration,
            title_prefix=title_prefix,
            overlay_title=overlay_title,
            resolution_width=resolution_width,
            resolution_height=resolution_height
        )
        self.tasks[task.id] = task
        self.task_added.emit(task)
        return task
    
    def start_download(self, task_id: str):
        """Start downloading a task."""
        if task_id not in self.tasks:
            return
            
        task = self.tasks[task_id]
        # If already active or queued, skip
        if task.status in (TaskStatus.DOWNLOADING, TaskStatus.PROCESSING, TaskStatus.QUEUED):
            return
        if len(self.active_workers) >= self.max_concurrent:
            task.status = TaskStatus.QUEUED
            self.task_updated.emit(task)
            if task_id not in self.queue:
                self.queue.append(task_id)
            return
        self._launch_worker(task)

    def _launch_worker(self, task: DownloadTask):
        worker = DownloadWorker(task)
        # Connect signals
        worker.progress_updated.connect(self._on_progress_updated)
        worker.status_changed.connect(self._on_status_changed)
        worker.error_occurred.connect(self._on_error_occurred)
        worker.download_completed.connect(self._on_download_completed)
        worker.finished.connect(lambda: self._on_worker_finished(task.id))
        self.active_workers[task.id] = worker
        worker.start()

    def _on_worker_finished(self, task_id: str):
        if task_id in self.active_workers:
            w = self.active_workers.pop(task_id)
            w.deleteLater()
        self._maybe_start_next()

    def _maybe_start_next(self):
        while self.queue and len(self.active_workers) < self.max_concurrent:
            next_id = self.queue.pop(0)
            if next_id in self.tasks:
                t = self.tasks[next_id]
                if t.status == TaskStatus.QUEUED:
                    self._launch_worker(t)

    def cancel_download(self, task_id: str):
        # Cancel active worker
        if task_id in self.active_workers:
            self.active_workers[task_id].cancel()
            return
        # Cancel queued
        if task_id in self.queue:
            self.queue.remove(task_id)
            if task_id in self.tasks:
                t = self.tasks[task_id]
                t.status = TaskStatus.CANCELLED
                self.task_updated.emit(t)

    def _on_progress_updated(self, task_id: str, progress: float, speed: str, eta: str):
        """Handle progress updates."""
        if task_id in self.tasks:
            task = self.tasks[task_id]
            task.progress = progress
            task.download_speed = speed
            task.eta = eta
            self.task_updated.emit(task)
    
    def _on_status_changed(self, task_id: str, status: TaskStatus):
        """Handle status changes."""
        if task_id in self.tasks:
            task = self.tasks[task_id]
            task.status = status
            self.task_updated.emit(task)
    
    def _on_error_occurred(self, task_id: str, error_message: str):
        """Handle errors."""
        if task_id in self.tasks:
            task = self.tasks[task_id]
            task.error_message = error_message
            self.task_updated.emit(task)
    
    def _on_download_completed(self, task_id: str, file_path: str, segments: Optional[list] = None):
        """Handle download completion."""
        if task_id in self.tasks:
            task = self.tasks[task_id]
            task.output_path = Path(file_path)
            if segments:
                task.segments = [Path(seg) for seg in segments]
            self.task_updated.emit(task)
            if segments:
                task.segments = [Path(seg) for seg in segments]
            self.task_updated.emit(task)
