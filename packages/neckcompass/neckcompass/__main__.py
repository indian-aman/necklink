"""Allow `python -m neckcompass` execution."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
