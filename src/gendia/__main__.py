"""CLI entry point.

Run via `python -m gendia ...` or, after installation, the `gendia` script
that pyproject.toml registers.
"""

from __future__ import annotations

import sys

from gendia.cli.arguments import build_parser, dispatch
from gendia.cli.banner import emit as emit_banner
from gendia.observability.logger import configure_logging


def main(argv: list[str] | None = None) -> int:
    # Bare invocation (no args): show banner + help and exit cleanly.
    real_argv = sys.argv[1:] if argv is None else argv
    if not real_argv:
        emit_banner()
        build_parser().print_help(sys.stderr)
        return 0

    parser = build_parser()
    args = parser.parse_args(argv)

    configure_logging(
        level=getattr(args, "log_level", None) or "INFO",
        output_format=getattr(args, "log_format", None),
    )

    # The banner is shown for top-level help only; subcommand `--help` stays
    # narrowly scoped. argparse always exits before reaching `dispatch` for
    # the help case, so we hook it earlier.
    return dispatch(args)


if __name__ == "__main__":
    sys.exit(main())
