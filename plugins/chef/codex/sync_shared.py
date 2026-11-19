"""Checkout-only compatibility alias for tools/check_shared.py."""
from pathlib import Path
import runpy

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).resolve().parents[3] / "tools/check_shared.py"), run_name="__main__")
