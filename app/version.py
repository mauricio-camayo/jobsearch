from pathlib import Path

_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"
APP_VERSION = _VERSION_FILE.read_text().strip() if _VERSION_FILE.exists() else "unknown"
