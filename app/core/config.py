from pathlib import Path
from dataclasses import dataclass
from typing import Dict, Any, Optional
import json

@dataclass
class Config:
    """Application configuration settings."""
    
    # Directories
    BASE_DIR: Path = Path(__file__).parent.parent.parent
    DOWNLOADS_DIR: Path = BASE_DIR / "downloads"
    LOGS_DIR: Path = BASE_DIR / "logs"
    CONFIG_DIR: Path = BASE_DIR / "config"
    
    # Application settings
    DEFAULT_OUTPUT_FORMAT: str = "mp4"
    DEFAULT_QUALITY: str = "best"
    MAX_CONCURRENT_DOWNLOADS: int = 4
    SEGMENT_DURATION: int = 120  # seconds
    
    # GUI settings
    WINDOW_WIDTH: int = 1200
    WINDOW_HEIGHT: int = 800
    THEME: str = "dark"
    
    @classmethod
    def load_from_file(cls, config_path: Optional[Path] = None) -> 'Config':
        """Load configuration from JSON file."""
        if config_path is None:
            config_path = cls.CONFIG_DIR / "settings.json"
            
        if config_path.exists():
            with open(config_path, 'r') as f:
                data = json.load(f)
                # Update class attributes with loaded data
                for key, value in data.items():
                    if hasattr(cls, key):
                        setattr(cls, key, value)
        return cls()
    
    def save_to_file(self, config_path: Optional[Path] = None):
        """Save current configuration to JSON file."""
        if config_path is None:
            config_path = self.CONFIG_DIR / "settings.json"
            
        config_path.parent.mkdir(parents=True, exist_ok=True)
        
        config_data = {
            attr: getattr(self, attr) for attr in dir(self)
            if not attr.startswith('_') and not callable(getattr(self, attr))
        }
        
        with open(config_path, 'w') as f:
            json.dump(config_data, f, indent=2, default=str)
