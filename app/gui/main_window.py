import logging
import sys
import shutil  # new
from pathlib import Path
from PyQt6.QtWidgets import (QMainWindow, QVBoxLayout, QHBoxLayout, 
                            QWidget, QPushButton, QLineEdit, QLabel,
                            QComboBox, QCheckBox, QSplitter, QStatusBar, QTabWidget,
                            QMessageBox)  # added QMessageBox
from PyQt6.QtCore import Qt
from app.gui.download_list_widget import DownloadListWidget
from app.services.download_service import DownloadService
from app.models.download_task import TaskType, TaskStatus  # ensure TaskStatus imported
from app.core.config import Config
from app.gui.subscriptions_widget import SubscriptionsWidget  # new import

logger = logging.getLogger(__name__)

def get_runtime_base_dir() -> Path:
    """
    Returns the base directory both in dev and when frozen by PyInstaller.
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent.parent

class MainWindow(QMainWindow):
    """Main application window."""
    
    def __init__(self):
        super().__init__()
        self.download_service = DownloadService()
        # NEW: listen for task updates to show detailed errors
        self.download_service.task_updated.connect(self._on_task_updated)
        self.init_ui()
    
    def init_ui(self):
        """Initialize the user interface."""
        self.setWindowTitle("YouTube Manager")
        self.setGeometry(100, 100, Config.WINDOW_WIDTH, Config.WINDOW_HEIGHT)
        
        tabs = QTabWidget()
        
        # Download tab
        download_tab = QWidget()
        d_layout = QVBoxLayout(download_tab)
        
        # URL input section
        url_layout = self.create_url_input_section()
        d_layout.addLayout(url_layout)
        
        # Options section
        options_layout = self.create_options_section()
        d_layout.addLayout(options_layout)
        
        # Download list
        self.download_list = DownloadListWidget(self.download_service)
        d_layout.addWidget(self.download_list)
        
        
        # Subscriptions tab
        self.subscriptions_widget = SubscriptionsWidget(
            download_service=self.download_service,
            options_provider=self.get_current_download_options
        )
        
        tabs.addTab(self.subscriptions_widget, "Subscriptions")
        tabs.addTab(download_tab, "Downloader")
        # Set central
        central_widget = QWidget()
        main_layout = QVBoxLayout(central_widget)
        main_layout.addWidget(tabs)
        self.setCentralWidget(central_widget)
        
        # Status bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready")
        
        # Show download directory once for clarity (removed inner import that caused UnboundLocalError)
        try:
            self.status_bar.showMessage(f"Ready - Download folder: {Config.DOWNLOADS_DIR}")
        except Exception:
            pass
        
    def create_url_input_section(self) -> QHBoxLayout:
        """Create URL input section."""
        layout = QHBoxLayout()
        
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("Enter YouTube URL here...")
        layout.addWidget(QLabel("URL:"))
        layout.addWidget(self.url_input)
        
        self.download_btn = QPushButton("Download")
        self.download_btn.clicked.connect(self.start_download)
        layout.addWidget(self.download_btn)
        
        return layout
    
    def create_options_section(self) -> QHBoxLayout:
        """Create options section."""
        main_layout = QVBoxLayout()
        
        # First row - main options
        layout = QHBoxLayout()
        
        # Task type
        layout.addWidget(QLabel("Type:"))
        self.task_type_combo = QComboBox()
        self.task_type_combo.addItems(["Video + Audio", "Audio Only", "Video Only"])
        layout.addWidget(self.task_type_combo)
        
        # Output format
        layout.addWidget(QLabel("Format:"))
        self.format_combo = QComboBox()
        self.format_combo.addItems(["mp4", "mov", "mkv", "avi"])
        layout.addWidget(self.format_combo)
        
        # Resolution selector
        layout.addWidget(QLabel("Resolution:"))
        self.resolution_input = QLineEdit()
        self.resolution_input.setPlaceholderText("e.g. 1920x1080 or 1080")
        self.resolution_input.setFixedWidth(120)
        self.resolution_input.setText("1920x1080")  # default
        layout.addWidget(self.resolution_input)
        
        # Split option
        self.split_checkbox = QCheckBox("Split into segments")
        self.split_checkbox.setChecked(True)  # default enabled now
        self.split_checkbox.toggled.connect(self.toggle_split_options)
        layout.addWidget(self.split_checkbox)
        
        layout.addStretch()
        main_layout.addLayout(layout)
        
        # Second row - split options (initially hidden -> will show because checked)
        self.split_options_widget = QWidget()
        split_layout = QHBoxLayout(self.split_options_widget)
        split_layout.setContentsMargins(20, 0, 0, 0)
        
        # Segment duration
        split_layout.addWidget(QLabel("Duration (s):"))
        self.duration_input = QLineEdit()
        self.duration_input.setText("120")
        self.duration_input.setPlaceholderText("e.g., 120")
        self.duration_input.setFixedWidth(80)
        split_layout.addWidget(self.duration_input)
        
        # Title prefix
        split_layout.addWidget(QLabel("Part prefix:"))
        self.title_prefix_input = QLineEdit()
        self.title_prefix_input.setPlaceholderText("e.g., Part")
        self.title_prefix_input.setText("Part")
        self.title_prefix_input.setFixedWidth(100)
        split_layout.addWidget(self.title_prefix_input)
        
        # Overlay title (new)
        split_layout.addWidget(QLabel("Overlay title:"))
        self.overlay_title_input = QLineEdit()
        self.overlay_title_input.setPlaceholderText("Bottom overlay text (leave blank = video title)")
        self.overlay_title_input.setFixedWidth(220)
        split_layout.addWidget(self.overlay_title_input)
        
        split_layout.addStretch()
        
        # Initially hide split options
        self.split_options_widget.setVisible(True)  # visible because default split enabled
        main_layout.addWidget(self.split_options_widget)
        
        # Convert to horizontal layout for return
        container_widget = QWidget()
        container_widget.setLayout(main_layout)
        container_layout = QHBoxLayout()
        container_layout.addWidget(container_widget)
        
        return container_layout
    
    def toggle_split_options(self, checked: bool):
        """Show/hide split options based on checkbox state."""
        self.split_options_widget.setVisible(checked)
    
    def _parse_resolution_spec(self, spec: str):
        if not spec:
            return None, None
        spec = spec.lower().strip().replace(" ", "")
        if "x" in spec:
            a, b = spec.split("x", 1)
            w = int(a) if a.isdigit() else None
            h = int(b) if b.isdigit() else None
            if not (w or h):
                return None, None
            return w, h
        if spec.isdigit():
            return None, int(spec)
        return None, None
    
    def start_download(self):
        """Start a new download."""
        url = self.url_input.text().strip()
        if not url:
            return
        # Pre-check for ffmpeg (user feedback earlier than yt_dlp exception)
        if self.split_checkbox.isChecked() or self.task_type_combo.currentIndex() != 1:
            if not shutil.which("ffmpeg"):
                self.status_bar.showMessage("Warning: ffmpeg not found; download may fail.")
                # Show popup only once per session
                if not getattr(self, "_ffmpeg_warned", False):
                    QMessageBox.information(
                        self,
                        "FFmpeg Missing",
                        "FFmpeg was not detected.\n\nInstall via:\n  macOS: brew install ffmpeg\n"
                        "Or bundle binaries in vendor/ffmpeg/<platform>/ before building."
                    )
                    self._ffmpeg_warned = True
        
        # Duplicate prevention
        for t in self.download_service.tasks.values():
            if t.url == url and t.status not in (TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.COMPLETED):
                self.status_bar.showMessage("Already downloading or queued.")
                return
        
        # Get selected options
        task_type_map = {
            0: TaskType.VIDEO_AUDIO,
            1: TaskType.AUDIO_ONLY,
            2: TaskType.VIDEO_ONLY
        }
        
        task_type = task_type_map[self.task_type_combo.currentIndex()]
        output_format = self.format_combo.currentText()
        should_split = self.split_checkbox.isChecked()
        
        # Get split options if enabled
        segment_duration = 120  # default
        title_prefix = "Part"   # default
        overlay_title = ""
        
        if should_split:
            try:
                segment_duration = int(self.duration_input.text() or "120")
            except ValueError:
                segment_duration = 120
            
            title_prefix = self.title_prefix_input.text().strip() or "Part"
            # new: retrieve overlay title
            overlay_title = self.overlay_title_input.text().strip()
        
        # Resolution handling
        w, h = self._parse_resolution_spec(self.resolution_input.text())
        
        # Add download task
        task = self.download_service.add_download_task(
            url=url,
            task_type=task_type,
            output_format=output_format,
            should_split=should_split,
            segment_duration=segment_duration,
            title_prefix=title_prefix,
            overlay_title=overlay_title or None,
            resolution_width=w,
            resolution_height=h
        )
        
        # Start download
        self.download_service.start_download(task.id)
        
        # Clear URL input
        self.url_input.clear()
        
        self.status_bar.showMessage(f"Started download: {url}")
    
    def get_current_download_options(self):
        """Provide current downloader settings for subscriptions download buttons."""
        # Parse segment duration
        try:
            segment_duration = int(self.duration_input.text() or "120")
        except Exception:
            segment_duration = 120
        w, h = self._parse_resolution_spec(self.resolution_input.text())
        return {
            "output_format": self.format_combo.currentText(),
            "should_split": self.split_checkbox.isChecked(),
            "segment_duration": segment_duration,
            "title_prefix": (self.title_prefix_input.text().strip() or "Part"),
            "overlay_title": (self.overlay_title_input.text().strip() if self.split_checkbox.isChecked() else ""),
            "resolution_width": w,
            "resolution_height": h
        }
    
    def _on_task_updated(self, task):
        """Show detailed error info when a download fails."""
        if task.status == TaskStatus.FAILED:
            msg = task.error_message or "Unknown error"
            # Update status bar
            self.status_bar.showMessage(f"Download failed: {task.title or task.url} - {msg}")
            # Popup for visibility
            QMessageBox.warning(
                self,
                "Download Failed",
                f"Title: {task.title or 'N/A'}\nURL: {task.url}\n\nError:\n{msg}"
            )
