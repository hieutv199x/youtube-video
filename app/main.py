import sys
import logging
from pathlib import Path
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QDir
from app.core.config import Config
from app.gui.main_window import MainWindow
from app.core.logger import setup_logging

def setup_application():
    """Initialize application configuration and logging."""
    # Ensure required directories exist
    Config.DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    Config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    
    # Setup logging
    setup_logging()
    
def main():
    """Main application entry point."""
    setup_application()
    
    app = QApplication(sys.argv)
    app.setApplicationName("YouTube Manager")
    app.setApplicationVersion("1.0.0")
    app.setOrganizationName("Your Organization")
    
    # Create and show main window
    main_window = MainWindow()
    main_window.show()
    
    return app.exec()

if __name__ == "__main__":
    sys.exit(main())
