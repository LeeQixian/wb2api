"""Facade: the CLI implementation lives in gateway.cli.wbctl."""

from gateway.cli.wbctl import main  # noqa: F401

if __name__ == "__main__":
    import sys
    sys.exit(main())
