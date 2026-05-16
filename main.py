"""Compatibility entrypoint for users who still run `python main.py`."""

from akli.core.app import main


if __name__ == "__main__":
    raise SystemExit(main())
