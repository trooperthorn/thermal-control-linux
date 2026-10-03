"""Command line entry point. Real commands arrive with the controller slices."""

from __future__ import annotations

import sys

from . import __version__


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["--version"]:
        print(__version__)
        return 0
    print("thermalctl: no commands yet; see docs/ARCHITECTURE.md", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
