"""Run the independent offline inventory tool from any working directory."""

from pathlib import Path
import sys


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tools.api_inventory.cli import main

    raise SystemExit(main())
