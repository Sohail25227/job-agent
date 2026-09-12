"""Source abstractions shared by every job provider."""

from __future__ import annotations

import html
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Iterable

from ..config import Config
from ..http import HttpClient

log = logging.getLogger(__name__)

BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article",
    "h1", "h2", "h3", "h4", "h5", "h6", "header", "footer", "blockquote", "pre",
}


@dataclass(slots=True)
class JobPosting:
    """Normalised posting, independent of which board it came from."""

    source: str
    external_id: str
    company: str
    title: str
    url: str
    ats: str | None = None
    location: str | None = None
    remote: bool = False
    apply_url: str | None = None
    description: str | None = None
    posted_at: datetime | None = None
    extra: dict = field(default_factory=dict)


class Source(ABC):
    """A provider of job postings.

    Implementations must never raise for a single bad board or query: log and
    keep going, so one dead slug cannot take down the nightly run.
    """

    name: str = "base"
    ats: str | None = None

    def __init__(self, config: Config, client: HttpClient):
        self.config = config
        self.client = client

    @abstractmethod
    def fetch(self) -> Iterable[JobPosting]:
        ...


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self._skip += 1
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self._skip:
            self._skip -= 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        return "".join(self.parts)


def html_to_text(raw: str | None) -> str:
    """Flatten a JD's HTML into readable plain text.

    Job boards double-encode entities often enough that unescaping twice is
    worth it; the second pass is a no-op on already-clean input.
    """
    if not raw:
        return ""
    text = html.unescape(html.unescape(raw))
    parser = _TextExtractor()
    try:
        parser.feed(text)
        parser.close()
        text = parser.text()
    except Exception:  # malformed markup: fall back to a crude tag strip
        text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


def parse_iso(value: str | int | float | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # Lever and friends use epoch milliseconds.
        seconds = value / 1000 if value > 1e11 else value
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    for candidate in (text, text.split("T")[0], text.split(" ")[0]):
        try:
            parsed = datetime.fromisoformat(candidate)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


REMOTE_PATTERN = re.compile(r"\b(remote|work from home|wfh|anywhere|distributed)\b", re.I)


def looks_remote(*values: str | None) -> bool:
    return any(REMOTE_PATTERN.search(v) for v in values if v)
