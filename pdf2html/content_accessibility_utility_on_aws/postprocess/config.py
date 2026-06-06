"""Centralized feature flags for optional post-processing layers."""

import os
from dataclasses import dataclass


def _env_bool(
    name: str,
    default: bool,
) -> bool:
    """Read a boolean environment variable safely."""
    raw_value = os.environ.get(name)

    if raw_value is None:
        return default

    return raw_value.strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _env_int(
    name: str,
    default: int,
) -> int:
    """Read an integer environment variable safely."""
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(
    name: str,
    default: float,
) -> float:
    """Read a floating-point environment variable safely."""
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class PostprocessSettings:
    """
    Optional post-processing configuration.

    Textract and Nova can be enabled or disabled independently.
    """

    textract_structure_enabled: bool
    structure_apply_html_changes_enabled: bool

    nova_ai_enabled: bool
    nova_image_analysis_enabled: bool
    nova_structure_adjudication_enabled: bool

    textract_max_wait_seconds: int
    textract_poll_interval_seconds: float
    textract_lambda_reserve_seconds: int

    structure_diagnostics_prefix: str

    @classmethod
    def from_env(cls) -> "PostprocessSettings":
        """Load configuration from Lambda environment variables."""
        return cls(
            textract_structure_enabled=_env_bool(
                "TEXTRACT_STRUCTURE_ENABLED",
                False,
            ),
            structure_apply_html_changes_enabled=_env_bool(
                "STRUCTURE_APPLY_HTML_CHANGES_ENABLED",
                False,
            ),
            nova_ai_enabled=_env_bool(
                "NOVA_AI_ENABLED",
                True,
            ),
            nova_image_analysis_enabled=_env_bool(
                "NOVA_IMAGE_ANALYSIS_ENABLED",
                True,
            ),
            nova_structure_adjudication_enabled=_env_bool(
                "NOVA_STRUCTURE_ADJUDICATION_ENABLED",
                False,
            ),
            textract_max_wait_seconds=_env_int(
                "TEXTRACT_MAX_WAIT_SECONDS",
                120,
            ),
            textract_poll_interval_seconds=_env_float(
                "TEXTRACT_POLL_INTERVAL_SECONDS",
                3.0,
            ),
            textract_lambda_reserve_seconds=_env_int(
                "TEXTRACT_LAMBDA_RESERVE_SECONDS",
                45,
            ),
            structure_diagnostics_prefix=os.environ.get(
                "STRUCTURE_DIAGNOSTICS_PREFIX",
                "diagnostics",
            ).strip("/"),
        )

    @property
    def nova_image_enabled(self) -> bool:
        """Return whether image classification may call Nova."""
        return (
            self.nova_ai_enabled
            and self.nova_image_analysis_enabled
        )

    @property
    def nova_structure_enabled(self) -> bool:
        """Return whether structural adjudication may call Nova."""
        return (
            self.nova_ai_enabled
            and self.nova_structure_adjudication_enabled
        )