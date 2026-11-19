"""Sort and clean up a Gmail account (labels by category, filters, trash)."""


def main() -> None:
    import sys

    from .cli import main as cli_main
    from .gmail import SetupError

    try:
        sys.exit(cli_main())
    except SetupError as e:
        print(f"mailroom: {e}", file=sys.stderr)
        sys.exit(2)
