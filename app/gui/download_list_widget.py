from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QScrollArea, 
                            QLabel, QProgressBar, QPushButton,
                            QHBoxLayout, QFrame)
from PyQt6.QtCore import Qt
from app.services.download_service import DownloadService
from app.models.download_task import DownloadTask, TaskStatus

class DownloadItemWidget(QFrame):
    """Widget representing a single download item."""
    
    def __init__(self, task: DownloadTask, download_service: DownloadService):
        super().__init__()
        self.task = task
        self.download_service = download_service
        self.init_ui()
    
    def init_ui(self):
        """Initialize the UI for this download item."""
        self.setFrameStyle(QFrame.Shape.Box)
        layout = QVBoxLayout(self)
        
        # Title and URL
        title_label = QLabel(f"Title: {self.task.title or 'Loading...'}")
        url_label = QLabel(f"URL: {self.task.url}")
        url_label.setStyleSheet("color: gray; font-size: 10px;")
        
        layout.addWidget(title_label)
        layout.addWidget(url_label)
        
        # Progress section
        progress_layout = QHBoxLayout()
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimum(0)
        self.progress_bar.setMaximum(100)
        self.progress_bar.setValue(int(self.task.progress))
        
        self.status_label = QLabel(self.task.status.value.capitalize())
        self.speed_label = QLabel(self.task.download_speed or "")
        
        progress_layout.addWidget(self.progress_bar)
        progress_layout.addWidget(self.status_label)
        progress_layout.addWidget(self.speed_label)
        
        # Control buttons
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.cancel_download)
        progress_layout.addWidget(self.cancel_btn)
        
        layout.addLayout(progress_layout)
    
    def update_task(self, task: DownloadTask):
        """Update the widget with new task data."""
        self.task = task
        self.progress_bar.setValue(int(task.progress))
        self.status_label.setText(task.status.value.capitalize())
        self.speed_label.setText(task.download_speed or "")
        
        # Update cancel button state
        if task.status in [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]:
            self.cancel_btn.setEnabled(False)
            self.cancel_btn.setText("Finished")
    
    def cancel_download(self):
        """Cancel this download."""
        self.download_service.cancel_download(self.task.id)

class DownloadListWidget(QWidget):
    """Widget for displaying list of downloads."""
    
    def __init__(self, download_service: DownloadService):
        super().__init__()
        self.download_service = download_service
        self.download_items = {}  # task_id -> DownloadItemWidget
        self.init_ui()
        self.setup_connections()
    
    def init_ui(self):
        """Initialize the UI."""
        layout = QVBoxLayout(self)
        
        # Header
        header = QLabel("Downloads")
        header.setStyleSheet("font-size: 16px; font-weight: bold; padding: 10px;")
        layout.addWidget(header)
        
        # Scroll area for download items
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        
        self.scroll_widget = QWidget()
        self.scroll_layout = QVBoxLayout(self.scroll_widget)
        self.scroll_layout.addStretch()
        
        scroll_area.setWidget(self.scroll_widget)
        layout.addWidget(scroll_area)
    
    def setup_connections(self):
        """Setup signal connections."""
        self.download_service.task_added.connect(self.add_download_item)
        self.download_service.task_updated.connect(self.update_download_item)
    
    def add_download_item(self, task: DownloadTask):
        """Add a new download item to the list."""
        item_widget = DownloadItemWidget(task, self.download_service)
        
        # Insert before the stretch
        self.scroll_layout.insertWidget(self.scroll_layout.count() - 1, item_widget)
        self.download_items[task.id] = item_widget
    
    def update_download_item(self, task: DownloadTask):
        """Update an existing download item."""
        if task.id in self.download_items:
            self.download_items[task.id].update_task(task)
