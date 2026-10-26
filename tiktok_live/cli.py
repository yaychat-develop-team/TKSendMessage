"""Command-line entrypoint."""

from .config import MANUAL_INPUT
from .errors import SafeOperationError, log_safe_error
from .runner import run_saved_accounts


def run_cli() -> int:
    try:
        run_saved_accounts(MANUAL_INPUT["username"], MANUAL_INPUT["message"])
    except SafeOperationError as exc:
        log_safe_error(exc.classification)
        return 1
    except KeyboardInterrupt:
        log_safe_error("run_cancelled")
        return 1
    except Exception:
        log_safe_error("runtime_error")
        return 1
    return 0


def main() -> None:
    raise SystemExit(run_cli()) from None


if __name__ == "__main__":
    main()
