"""Allow `python -m app.cli …` as an alternative to the `tootflow` script."""
import sys

from app.cli.main import main

if __name__ == "__main__":
    raise SystemExit(main())
