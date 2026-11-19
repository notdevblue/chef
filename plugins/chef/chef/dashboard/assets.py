"""Static dashboard resources; preserve original rendering bytes."""

from pathlib import Path

ROOT = Path(__file__).with_name("static")
STYLE = (ROOT / "style.css").read_text(encoding="utf-8")
REFRESH_SCRIPT = (ROOT / "refresh_script.html").read_text(encoding="utf-8")
THEME_SCRIPT = (ROOT / "theme_script.html").read_text(encoding="utf-8")
THEME_CONTROL_SCRIPT = (ROOT / "theme_control_script.html").read_text(encoding="utf-8")
THEME_TOGGLE = (ROOT / "theme_toggle.html").read_text(encoding="utf-8")
DOCUMENT_VIEWER = (ROOT / "document_viewer.html").read_text(encoding="utf-8")
DOCUMENT_VIEWER_SCRIPT = (ROOT / "document_viewer_script.html").read_text(encoding="utf-8")
MARKDOWN_SCRIPT = (ROOT / "markdown_script.html").read_text(encoding="utf-8")
