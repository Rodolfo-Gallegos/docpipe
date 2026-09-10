"""Extract clean text from HTML pages.

Strips scripts, styles and page chrome (nav, footer, sidebars, ads), then
prefers the semantic content root (`<main>`, then `<article>`, then
`<body>`) before flattening to text.

Both removal lists are arguments, so a site that names its real content
container "sidebar-article" can opt out of the default class filter.
"""
from __future__ import annotations

from typing import Optional, Sequence

from bs4 import BeautifulSoup

from docpipe.logger import get_logger

logger = get_logger(__name__)

UNWANTED_TAGS: tuple[str, ...] = (
    "script", "style", "nav", "footer", "header", "aside", "form", "iframe", "noscript",
)
UNWANTED_CLASSES: tuple[str, ...] = (
    "nav", "navigation", "footer", "header", "sidebar", "menu", "advertisement", "ads",
)


def extract_text(
    html: str,
    unwanted_tags: Optional[Sequence[str]] = None,
    unwanted_classes: Optional[Sequence[str]] = None,
    parser: str = "lxml",
) -> str:
    """Extract main text content from HTML, removing chrome."""
    tags = tuple(UNWANTED_TAGS if unwanted_tags is None else unwanted_tags)
    classes = tuple(UNWANTED_CLASSES if unwanted_classes is None else unwanted_classes)

    soup = BeautifulSoup(html, parser)

    if tags:
        for tag in soup(list(tags)):
            tag.decompose()

    for class_name in classes:
        needle = class_name.lower()
        for element in soup.find_all(class_=lambda c: c and needle in c.lower()):
            element.decompose()

    main = soup.find("main") or soup.find("article") or soup.find("body") or soup
    text = main.get_text(separator="\n", strip=True)

    lines = [line.strip() for line in text.split("\n") if line.strip()]
    cleaned = "\n".join(lines)

    logger.info(f"HTML extracted {len(cleaned)} chars")
    return cleaned
