"""File names and episode titles."""

import re

_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_filename(text: str, max_len: int = 150) -> str:
    """Make text safe for SMB/NAS filenames."""
    text = _UNSAFE.sub("", text.replace(":", " -").replace("/", "-"))
    text = re.sub(r"\s+", " ", text).strip().rstrip(".")
    return text[:max_len].rstrip() or "Untitled"


# NRK numbers episode titles of sequential series: "1. Fysiske fordeler", "12. episode"
_NUMBER_PREFIX = re.compile(r"^\d{1,4}\.\s+")


def strip_number(title: str) -> str:
    """'1. Fysiske fordeler' -> 'Fysiske fordeler'. A title that is only the number stays."""
    rest = _NUMBER_PREFIX.sub("", title).strip()
    return rest if rest and rest.casefold() != "episode" else title.strip()


def date_title_suffix(title: str) -> str:
    """What an NRK date title says besides the date: '31. august 2001 · Fleksnes' -> 'Fleksnes'."""
    _, sep, rest = title.partition("·")
    return rest.strip() if sep else ""
