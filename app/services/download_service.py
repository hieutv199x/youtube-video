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
import sys
import platform

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
    safe_title = video_title.replace("\\", "\\\\").replace(":", r'\:').replace("'", r"\'")
    num_parts = int(duration // segment_duration) + (1 if duration % segment_duration > 0 else 0)
    segments = []
    for i in range(num_parts):
        start = i * segment_duration
        out_file = outdir / f"{input_path.stem}_{title_prefix.lower().replace(' ', '_')}{i+1}{input_path.suffix}"
        top_text = f"{title_prefix} {i+1}"
        vf = (
            f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
            f"drawtext=text='{safe_title}':fontcolor=black:fontsize=36:"
            f"x=(w-text_w)/2:y=h/4-text_h:box=1:boxcolor=yellow@1:boxborderw=10,"
            f"drawtext=text='{top_text}':fontcolor=black:fontsize=48:"
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
        segments.append(out_file)
        print(f"Created {out_file}")
    
    return segments

def _runtime_base_dir():
    """Return base dir in dev or frozen mode."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
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

            # Always attempt to resolve ffmpeg early
            ffmpeg_path, ffprobe_path, ffmpeg_dir = _locate_ffmpeg()

            # Pre-fetch metadata FIRST so title is known even if we later error out
            preliminary_opts = {
                'quiet': True,
                'skip_download': True,
                'nocheckcertificate': True,
                'ignoreerrors': False,
            }
            try:
                with yt_dlp.YoutubeDL(preliminary_opts) as meta_ydl:
                    info = meta_ydl.extract_info(self.task.url, download=False)
                    if info:
                        self.task.title = info.get('title') or self.task.title
            except Exception as meta_err:
                # Non-fatal for now; continue – title may remain Unknown
                logger.debug(f"Metadata pre-fetch failed: {meta_err}")

            needs_video_processing = (self.task.task_type != TaskType.AUDIO_ONLY) or self.task.should_split
            if needs_video_processing and (not ffmpeg_path or not ffprobe_path):
                # Provide explicit failure early (better UX)
                raise RuntimeError(
                    "FFmpeg not found. Install ffmpeg (and ffprobe) or bundle them.\n"
                    "macOS (brew): brew install ffmpeg\n"
                    "Windows (scoop): scoop install ffmpeg\n"
                    "Or place binaries in vendor/ffmpeg/<platform>/ before building.\n"
                    "Splitting / muxing requires ffmpeg."
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

            format_selector = self._get_format_selector()

            ydl_opts = {
                'outtmpl': str(outdir / '%(title)s - %(id)s.%(ext)s'),
                'format': format_selector,
                'merge_output_format': self.task.output_format,
                'progress_hooks': [progress_hook],
                'ignoreerrors': False,
            }
            if ffmpeg_dir:
                ydl_opts['ffmpeg_location'] = ffmpeg_dir

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(self.task.url, download=True)
                if info and not self.task.title:
                    self.task.title = info.get('title') or self.task.title

                downloaded_files = list(outdir.glob(f"*{info['id']}*.{self.task.output_format}")) if info else []
                if not downloaded_files:
                    raise Exception("Downloaded file not found after yt-dlp run.")
                file_path = downloaded_files[0]
                segments = []

                if self.task.should_split:
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
                    "\nHint: Provide ffmpeg & ffprobe via system PATH or bundle static binaries "
                    "in vendor/ffmpeg/<platform>/ (ffmpeg, ffprobe) before building."
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
    
    def add_download_task(self, url: str, task_type: TaskType = TaskType.VIDEO_AUDIO,
                          output_format: str = None, should_split: bool = False,
                          segment_duration: int = 120, title_prefix: str = "Part",
                          overlay_title: str | None = None,
                          resolution: int | None = None,  # legacy height
                          resolution_width: int | None = None,
                          resolution_height: int | None = None) -> DownloadTask:
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
    
    def _on_download_completed(self, task_id: str, file_path: str, segments: list = None):
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
