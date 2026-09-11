"""A source you know you cannot read, recorded on purpose.

Every fleet accumulates these: the origin is behind a login, a hard WAF
that even a hosted browser does not pass, or a captcha. The temptation is
to delete the entry and move on. Don't. An absent source and an impossible
one look identical in a recipe file, so the same site gets rediscovered,
re-probed and re-abandoned every few months by whoever is onboarding next.

This adapter fetches nothing. It exists so the decision survives: the URL
is on file, the reason is written down, and `docpipe run` reports it as a
known gap instead of a failure to investigate.

    {
      "id": "acme-portal",
      "source_type": "blocked",
      "config": {
        "origin_url": "https://portal.example.gov/documents",
        "reason": "login_required",
        "detail": "Documents sit behind an account request form, approved by a human."
      }
    }

If the block ever lifts, re-probe and replace the entry with a real
adapter. `checked_at` is there to tell you how stale the verdict is.
"""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import Field

from docpipe.base import BaseSource, FetchedDocument, SourceConfig
from docpipe.logger import get_logger
from docpipe.registry import register_source

logger = get_logger(__name__)

Reason = Literal[
    "login_required",
    "waf",
    "captcha",
    "ip_blocked",
    "paywall",
    "no_public_archive",
    "other",
]

# What to try next, per reason. A block is not always final, and the reason
# determines whether anything in this toolbox would help.
_NEXT_STEP: dict[str, str] = {
    "waf": (
        "A hosted browser passes many WAF challenges. Try cloud_render "
        "before accepting this as blocked."
    ),
    "ip_blocked": (
        "The origin refuses datacenter ranges. cloud_render with "
        "proxy: true uses a residential exit IP, which is the one thing "
        "that helps here."
    ),
    "captcha": "Nothing here solves a captcha. This needs a human or an agreement with the site.",
    "login_required": "Credentials or an API agreement with the site owner. Out of scope for scraping.",
    "paywall": "A subscription, and terms that allow automated access.",
    "no_public_archive": (
        "The site publishes documents but keeps no archive. Consider "
        "polling the listing on a schedule so you catch them as they appear."
    ),
}


def next_step_for(reason: str) -> str:
    """What to try for this kind of block, or why nothing will help."""
    return _NEXT_STEP.get(
        reason, "No known way through. Revisit if the site changes."
    )


class BlockedConfig(SourceConfig):
    """`origin_url`: where the documents live, for the record.

    `reason`: why it cannot be read. Drives the suggested next step.
    `detail`: what you actually found, in your own words. This is the part
        that saves the next person the investigation.
    `checked_at`: ISO date of the last time someone verified the block.
    """

    origin_url: str = Field(pattern=r"^https?://")
    reason: Reason = "other"
    detail: Optional[str] = None
    checked_at: Optional[str] = None


@register_source("blocked")
class BlockedSource(BaseSource):
    config_model = BlockedConfig
    config: BlockedConfig

    @property
    def next_step(self) -> str:
        return next_step_for(self.config.reason)

    def fetch_documents(self, limit: int = 3) -> List[FetchedDocument]:
        next_step = self.next_step
        logger.info(
            f"{self.tag} blocked ({self.config.reason}) at {self.config.origin_url}. "
            f"{self.config.detail or ''} {next_step}".strip()
        )
        return []

    def to_text(self, document: FetchedDocument) -> tuple[str, str]:
        raise ValueError(
            "BlockedSource never produces documents. It records a source that "
            "cannot be read and why."
        )
