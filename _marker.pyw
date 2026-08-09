from pathlib import Path
Path(__file__).with_name("from_shortcut.txt").write_text("started", encoding="utf-8")
import launch
