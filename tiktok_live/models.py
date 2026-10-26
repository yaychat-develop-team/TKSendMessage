"""Typed domain results."""

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class AccountValidation:
    status: Literal["valid", "expired", "temporary_error"]
    cookies: list[dict[str, Any]] = field(repr=False)
    reason: str
