from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QPushButton, QLabel,
                             QHBoxLayout, QTableWidget, QTableWidgetItem,
                             QSplitter, QAbstractItemView, QHeaderView, QSpinBox,
                             QDialog, QDialogButtonBox, QLineEdit, QCheckBox, QFormLayout,
                             QSizePolicy, QMessageBox, QFileDialog, QInputDialog,
                             QDoubleSpinBox)  # + QDoubleSpinBox
from PyQt6.QtCore import Qt, QSettings
import os
from pathlib import Path
from app.services.youtube_channel_service import YouTubeChannelService
from app.models.download_task import TaskType, TaskStatus
from app.core.config import Config

class SplitOptionsDialog(QDialog):
    """Dialog to collect per-download split options."""
    def __init__(self, parent=None, defaults=None, video_title: str = "", initial_download_dir: str | None = None):
        super().__init__(parent)
        self.setWindowTitle("Download Options")
        self.setSizeGripEnabled(True)             # allow manual resize
        self.setMinimumWidth(520)                 # wider default
        defaults = defaults or {}
        form = QFormLayout(self)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self.split_checkbox = QCheckBox("Enable splitting")
        self.split_checkbox.setChecked(defaults.get("should_split", True))
        form.addRow(self.split_checkbox)

        self.duration_spin = QSpinBox()
        self.duration_spin.setRange(10, 3600)
        self.duration_spin.setValue(int(defaults.get("segment_duration", 120)))
        form.addRow("Segment duration (s):", self.duration_spin)

        self.title_prefix_edit = QLineEdit()
        self.title_prefix_edit.setText(defaults.get("title_prefix", "Part"))
        self.title_prefix_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        form.addRow("Title prefix:", self.title_prefix_edit)

        self.overlay_title_edit = QLineEdit()
        self.overlay_title_edit.setText(video_title or defaults.get("overlay_title", video_title) or "")
        self.overlay_title_edit.setPlaceholderText("Overlay text shown at bottom of each split segment")
        self.overlay_title_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        form.addRow("Overlay title:", self.overlay_title_edit)

        # Resolution selector (new)
        self.resolution_edit = QLineEdit()
        self.resolution_edit.setText(defaults.get("resolution", "1920x1080"))
        self.resolution_edit.setPlaceholderText("e.g. 1920x1080 or 1080")
        if defaults:
            rw = defaults.get("resolution_width")
            rh = defaults.get("resolution_height")
            if rw and rh:
                self.resolution_edit.setText(f"{rw}x{rh}")
            elif rh:
                self.resolution_edit.setText(str(rh))
        form.addRow("Resolution:", self.resolution_edit)

        # New: Speed factor
        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setDecimals(2)
        self.speed_spin.setRange(0.25, 4.0)
        self.speed_spin.setSingleStep(0.1)
        self.speed_spin.setValue(float(defaults.get("speed_factor", 1.0)))
        self.speed_spin.setToolTip("Playback speed for split parts (video+audio). 1.0 = normal")
        form.addRow("Speed (x):", self.speed_spin)

        # Download folder selector (new)
        folder_layout = QHBoxLayout()
        self.folder_edit = QLineEdit()
        self.folder_edit.setPlaceholderText("Select download folder...")
        if initial_download_dir:
            self.folder_edit.setText(initial_download_dir)
        browse_btn = QPushButton("Browse...")
        def _browse():
            start_dir = self.folder_edit.text().strip() or initial_download_dir or str(Path.home())
            picked = QFileDialog.getExistingDirectory(self, "Select Download Folder", start_dir)
            if picked:
                self.folder_edit.setText(picked)
        browse_btn.clicked.connect(_browse)
        folder_layout.addWidget(self.folder_edit)
        folder_layout.addWidget(browse_btn)
        form.addRow("Download folder:", folder_layout)

        # Add cut head/tail spinboxes
        self.cut_head_spin = QSpinBox()
        self.cut_head_spin.setRange(0, 3600)
        self.cut_head_spin.setValue(int(defaults.get("cut_head_seconds", 0)))
        form.addRow("Cut head (s):", self.cut_head_spin)

        self.cut_tail_spin = QSpinBox()
        self.cut_tail_spin.setRange(0, 3600)
        self.cut_tail_spin.setValue(int(defaults.get("cut_tail_seconds", 0)))
        form.addRow("Cut tail (s):", self.cut_tail_spin)

        # Enable/disable controls based on split checkbox
        def toggle(enabled):
            self.duration_spin.setEnabled(enabled)
            self.title_prefix_edit.setEnabled(enabled)
            self.overlay_title_edit.setEnabled(enabled)
            # Resolution remains always enabled (no dependency)
        self.split_checkbox.toggled.connect(toggle)
        toggle(self.split_checkbox.isChecked())

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

        # Adjust initial size after laying out
        self.resize(max(self.sizeHint().width() + 140, 520), self.sizeHint().height() + 20)

    def get_values(self):
        vals = {
            "should_split": self.split_checkbox.isChecked(),
            "segment_duration": self.duration_spin.value(),
            "title_prefix": self.title_prefix_edit.text().strip() or "Part",
            "overlay_title": self.overlay_title_edit.text().strip()
        }
        # Resolution parse
        spec = self.resolution_edit.text().strip().lower().replace(" ", "")
        rw = rh = None
        if "x" in spec:
            a, b = spec.split("x", 1)
            if a.isdigit(): rw = int(a)
            if b.isdigit(): rh = int(b)
        elif spec.isdigit():
            rh = int(spec)
        vals["resolution_width"] = rw
        vals["resolution_height"] = rh
        vals["download_dir"] = self.folder_edit.text().strip()
        vals["cut_head_seconds"] = self.cut_head_spin.value()
        vals["cut_tail_seconds"] = self.cut_tail_spin.value()
        vals["speed_factor"] = float(self.speed_spin.value())
        return vals

class SubscriptionsWidget(QWidget):
    def __init__(self, parent=None, download_service=None, options_provider=None):
        super().__init__(parent)
        self.svc = YouTubeChannelService()
        self.download_service = download_service
        self.options_provider = options_provider
        self._subs = []
        self._videos = {}          # per-channel
        self._all_videos = []      # aggregated list
        self._all_mode = False
        self._pending_channels = 0
        self._processed_channels = 0
        self._download_buttons = {}  # url -> QPushButton
        self._last_download_dir = QSettings().value("last_download_dir", str(getattr(Config, "DOWNLOADS_DIR", Path.home())))
        self._build_ui()
        self._wire()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.auth_btn = QPushButton("Authenticate")
        self.refresh_subs_btn = QPushButton("Refresh Subscriptions")
        self.refresh_subs_btn.setEnabled(False)
        self.load_all_btn = QPushButton("Load All New Videos")
        self.load_all_btn.setEnabled(False)
        self.refresh_videos_btn = QPushButton("Load Channel Videos")
        self.refresh_videos_btn.setEnabled(False)
        self.max_results = QSpinBox()
        self.max_results.setRange(1, 100)
        self.max_results.setValue(10)
        self.since_hours = QSpinBox()
        self.since_hours.setRange(1, 720)
        self.since_hours.setValue(72)
        self.channel_limit = QSpinBox()
        self.channel_limit.setRange(0, 1000)
        self.channel_limit.setValue(0)  # 0 = all
        top.addWidget(self.auth_btn)
        top.addWidget(self.refresh_subs_btn)
        top.addWidget(self.load_all_btn)
        top.addWidget(self.refresh_videos_btn)
        top.addWidget(QLabel("Max videos:"))
        top.addWidget(self.max_results)
        top.addWidget(QLabel("Since (h):"))
        top.addWidget(self.since_hours)
        top.addWidget(QLabel("Channel limit:"))
        top.addWidget(self.channel_limit)
        top.addStretch()

        self.low_quota_checkbox = QCheckBox("Low quota mode")
        self.low_quota_checkbox.setToolTip("Uses playlistItems (≈2 units/channel) instead of search (≈100 units/channel).")
        top.addWidget(self.low_quota_checkbox)

        layout.addLayout(top)

        splitter = QSplitter(Qt.Orientation.Vertical)
        # Subscriptions table
        self.subs_table = QTableWidget(0, 2)
        self.subs_table.setHorizontalHeaderLabels(["Channel Title", "Channel ID"])
        self.subs_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self.subs_table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)

        # Videos table now 5 columns: Published, Channel, Title, URL, Action
        self.videos_table = QTableWidget(0, 5)
        self.videos_table.setHorizontalHeaderLabels(["Published", "Channel", "Title", "URL", ""])
        self.videos_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self.videos_table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)

        splitter.addWidget(self.subs_table)
        splitter.addWidget(self.videos_table)
        layout.addWidget(splitter)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.setLayout(layout)

    def _wire(self):
        self.auth_btn.clicked.connect(self._on_auth_clicked)
        self.refresh_subs_btn.clicked.connect(lambda: self._load_subs())
        self.refresh_videos_btn.clicked.connect(self._load_selected_channel_videos)
        self.subs_table.itemSelectionChanged.connect(self._on_channel_selected)
        self.load_all_btn.clicked.connect(self._load_all_channels_videos)

        self.svc.auth_changed.connect(self._on_auth_changed)
        self.svc.subscriptions_loaded.connect(self._on_subs_loaded)
        self.svc.channel_videos_loaded.connect(self._on_videos_loaded)
        self.svc.multiple_videos_loaded.connect(self._on_all_videos_loaded)
        self.svc.error_occurred.connect(self._on_error)
        self.svc.quota_exceeded.connect(self._on_quota_exceeded)
        if self.download_service:
            self.download_service.task_updated.connect(self._on_task_update)
        # NEW: attempt silent authentication reuse
        if self.svc.ensure_session():
            # Automatically load subscriptions after silent auth
            self.refresh_subs_btn.setEnabled(True)
            self.load_all_btn.setEnabled(True)
            self._load_subs()

    def _on_auth_clicked(self):
        """Handle authenticate button click."""
        self.status_label.setText("Opening browser for Google authentication...")
        self.auth_btn.setEnabled(False)
        self.svc.authenticate()

    def _on_auth_changed(self, ok: bool):
        self.auth_btn.setEnabled(True)
        if ok:
            self.status_label.setText("Authenticated")
            self.refresh_subs_btn.setEnabled(True)
            self.load_all_btn.setEnabled(True)
            self._load_subs()
        else:
            # show detailed error if available
            err = getattr(self.svc, "last_error", None)
            self.status_label.setText(f"Auth failed: {err or 'Unknown error'}")

    def _load_subs(self):
        self.status_label.setText("Loading subscriptions...")
        self.svc.load_subscriptions()

    def _on_subs_loaded(self, subs: list):
        self._subs = subs
        self.subs_table.setRowCount(0)
        for row, ch in enumerate(subs):
            self.subs_table.insertRow(row)
            self.subs_table.setItem(row, 0, QTableWidgetItem(ch["title"]))
            self.subs_table.setItem(row, 1, QTableWidgetItem(ch["channel_id"]))
        self.load_all_btn.setEnabled(bool(subs))
        self.status_label.setText(f"Loaded {len(subs)} subscriptions")

    def _on_channel_selected(self):
        has = len(self.subs_table.selectedItems()) > 0
        self.refresh_videos_btn.setEnabled(has)

    def _load_selected_channel_videos(self):
        sel = self.subs_table.selectedItems()
        if not sel:
            return
        row = self.subs_table.currentRow()
        item = self.subs_table.item(row, 1)
        if item is None:
            self.status_label.setText("Error: No channel selected.")
            return
        channel_id = item.text()
        self.status_label.setText(f"Loading videos for {channel_id}...")
        self.svc.load_channel_videos(
            channel_id,
            max_results=self.max_results.value(),
            since_hours=self.since_hours.value(),
            use_cache=False,      # force fresh
            force=True,           # bypass cached file even if present
            use_search_fallback=not self.low_quota_checkbox.isChecked()
        )

    def _load_all_channels_videos(self):
        if not self._subs:
            return
        channel_ids = [c["channel_id"] for c in self._subs]
        limit = self.channel_limit.value() or None
        effective_ids = channel_ids[:limit] if limit else channel_ids
        use_search = not self.low_quota_checkbox.isChecked()
        # Quota estimate
        try:
            # Estimate quota units locally since import failed
            def estimate_quota_units(channel_count, use_search):
                # Example logic: search uses 100 units/channel, playlist uses 2 units/channel
                return channel_count * (100 if use_search else 2)
            estimated = estimate_quota_units(len(effective_ids), use_search)
        except Exception:
            estimated = None
        if estimated:
            self.status_label.setText(f"Estimated quota cost: {estimated} unit(s). Starting fetch...")
            # Ask confirmation if high
            if estimated >= 5000:
                resp = QMessageBox.question(
                    self,
                    "High Quota Usage",
                    f"This request may consume about {estimated} quota units.\n"
                    f"Daily quota is typically 10,000.\n\nProceed?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No
                )
                if resp != QMessageBox.StandardButton.Yes:
                    self.status_label.setText("Cancelled by user (quota warning).")
                    return
        self._all_mode = True
        self.status_label.setText(
            f"Loading newest videos from {len(effective_ids)} channel(s) "
            f"({'search' if use_search else 'playlist'} strategy)..."
        )
        self.svc.load_multiple_channels_videos(
            effective_ids,
            max_results=self.max_results.value(),
            since_hours=self.since_hours.value(),
            channel_limit=None,
            use_search_strategy=use_search,
            use_cache=False       # always fresh
        )

    def _on_videos_loaded(self, channel_id: str, videos: list):
        # single channel mode only
        if self._all_mode:
            return
        # Obtain channel title
        title_lookup = next((c["title"] for c in self._subs if c["channel_id"] == channel_id), channel_id)
        for v in videos:
            v["_channel_title"] = title_lookup
        self._display_videos(videos, aggregated=False)

    def _on_all_videos_loaded(self, videos: list):
        # finalize aggregated load
        for v in videos:
            title_lookup = next((c["title"] for c in self._subs if c["channel_id"] == v.get("_channel_id")), v.get("_channel_id"))
            v["_channel_title"] = title_lookup
        self._all_mode = False
        self._display_videos(videos, aggregated=True)

    def _display_videos(self, videos: list, aggregated: bool):
        # Sort by published desc
        videos_sorted = sorted(videos, key=lambda x: x["published_at"], reverse=True)
        self.videos_table.setRowCount(0)
        self._download_buttons.clear()
        for r, v in enumerate(videos_sorted):
            self.videos_table.insertRow(r)
            self.videos_table.setItem(r, 0, QTableWidgetItem(v["published_at"]))
            self.videos_table.setItem(r, 1, QTableWidgetItem(v.get("_channel_title", "")))
            self.videos_table.setItem(r, 2, QTableWidgetItem(v["title"]))
            self.videos_table.setItem(r, 3, QTableWidgetItem(v["url"]))
            btn = QPushButton("Download")
            self._download_buttons[v["url"]] = btn
            # If already tracked by a task, adjust state later
            existing_task = self._find_task_by_url(v["url"])
            if existing_task:
                self._apply_task_state_to_button(existing_task, btn)
            btn.clicked.connect(lambda _=False, video=v: self._download_video(video))
            self.videos_table.setCellWidget(r, 4, btn)
        self.status_label.setText(f"{len(videos_sorted)} video(s) shown{' (all channels)' if aggregated else ''}")

    def _find_task_by_url(self, url: str):
        # Access download_service tasks
        if not self.download_service:
            return None
        for t in self.download_service.tasks.values():
            if t.url == url:
                return t
        return None

    def _apply_task_state_to_button(self, task, btn):
        # Preserve existing logic; add tooltip for errors and progress
        if task.status == TaskStatus.QUEUED:
            btn.setText("Queued")
            btn.setEnabled(False)
            btn.setToolTip("Queued for download")
        elif task.status == TaskStatus.DOWNLOADING:
            btn.setText(f"{int(task.progress)}%")
            btn.setEnabled(False)
            tip = f"Downloading... {task.progress:.1f}%"
            if task.download_speed:
                tip += f" @ {task.download_speed}"
            if task.eta:
                tip += f" (ETA {task.eta})"
            btn.setToolTip(tip)
        elif task.status == TaskStatus.PROCESSING:
            btn.setText("Processing")
            btn.setEnabled(False)
            btn.setToolTip("Post-processing (splitting / marking)")
        elif task.status == TaskStatus.COMPLETED:
            btn.setText("Done")
            btn.setEnabled(False)
            btn.setToolTip("Download completed")
        elif task.status == TaskStatus.FAILED:
            btn.setText("Retry")
            btn.setEnabled(True)
            btn.setToolTip(task.error_message or "Failed")
        elif task.status == TaskStatus.CANCELLED:
            btn.setText("Cancelled")
            btn.setEnabled(True)
            btn.setToolTip("Cancelled")
        else:
            btn.setText("Download")
            btn.setEnabled(True)
            btn.setToolTip("Start download")

    def _download_video(self, video: dict):
        if not self.download_service:
            self.status_label.setText("Error: download service not available.")
            return
        existing = self._find_task_by_url(video["url"])
        if existing and existing.status not in (TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.COMPLETED):
            self.status_label.setText("Already downloading / queued.")
            return

        # Base defaults from main window options provider (if available)
        opts = self.options_provider() if callable(self.options_provider) else {}
        base_opts = opts if isinstance(opts, dict) else {}
        dlg = SplitOptionsDialog(
            self,
            defaults=base_opts,
            video_title=video.get("title", ""),
            initial_download_dir=self._last_download_dir
        )

        if dlg.exec() != QDialog.DialogCode.Accepted:
            self.status_label.setText("Download cancelled.")
            return
        user_opts = dlg.get_values()
        folder = user_opts.get("download_dir") or self._last_download_dir
        if not folder:
            self.status_label.setText("Download cancelled (no folder).")
            return
        try:
            os.makedirs(folder, exist_ok=True)
        except Exception as e:
            QMessageBox.warning(self, "Folder Error", f"Cannot create/use folder:\n{folder}\n\n{e}")
            return
        self._last_download_dir = folder
        QSettings().setValue("last_download_dir", folder)

        task = self.download_service.add_download_task(
            url=video["url"],
            task_type=TaskType.VIDEO_AUDIO,
            output_format=base_opts.get("output_format", "mp4"),
            should_split=user_opts["should_split"],
            segment_duration=user_opts["segment_duration"],
            title_prefix=user_opts["title_prefix"],
            overlay_title=user_opts["overlay_title"] or video.get("title", ""),
            resolution_width=user_opts["resolution_width"] if user_opts["resolution_width"] is not None else base_opts.get("resolution_width"),
            resolution_height=user_opts["resolution_height"] if user_opts["resolution_height"] is not None else base_opts.get("resolution_height"),
            download_dir=folder,
            cut_head_seconds=user_opts.get("cut_head_seconds", 0),
            cut_tail_seconds=user_opts.get("cut_tail_seconds", 0),
            speed_factor=user_opts.get("speed_factor", 1.0)
        )
        self.download_service.start_download(task.id)
        btn = self._download_buttons.get(video["url"])
        if btn:
            self._apply_task_state_to_button(task, btn)
        self.status_label.setText(f"Queued/Started: {video['title']}")

    # Connect to download service to reflect progress
    def _on_task_update(self, task):
        btn = self._download_buttons.get(task.url)
        if btn:
            self._apply_task_state_to_button(task, btn)
        # Show error in status label immediately
        if task.status == TaskStatus.FAILED:
            self.status_label.setText(f"Error: {task.error_message or 'Unknown error'}")

    # Added missing method referenced by signal connection
    def _on_error(self, msg: str):
        if hasattr(self, "status_label"):
            self.status_label.setText(f"Error: {msg}")
        else:
            print(f"Error: {msg}")
        if hasattr(self, "status_label"):
            self.status_label.setText(f"Error: {msg}")
        else:
            print(f"Error: {msg}")

    def _on_quota_exceeded(self, message: str, partial_videos: list):
        # Display partial videos (if any) and show message
        for v in partial_videos:
            title_lookup = next((c["title"] for c in self._subs if c["channel_id"] == v.get("_channel_id")), v.get("_channel_id"))
            v["_channel_title"] = title_lookup
        self._all_mode = False
        if partial_videos:
            self._display_videos(partial_videos, aggregated=True)
        self.status_label.setText(message + (" - Shown partial results." if partial_videos else ""))