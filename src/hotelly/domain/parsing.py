"""Deterministic intent parsing from user messages.

NO LLM. Uses regex and heuristics.
Security: NEVER log raw text (PII).
"""

import re
from datetime import date

from hotelly.domain.intents import ParsedIntent

# Default room type aliases (can be overridden)
DEFAULT_ROOM_TYPE_ALIASES: dict[str, str] = {
    # Portuguese aliases
    "casal": "rt_casal",
    "duplo": "rt_casal",
    "double": "rt_casal",
    "suite": "rt_suite",
    "suíte": "rt_suite",
    "familia": "rt_familia",
    "família": "rt_familia",
    "family": "rt_familia",
    "single": "rt_single",
    "solteiro": "rt_single",
    "simples": "rt_single",
    "triplo": "rt_triplo",
    "triple": "rt_triplo",
    "luxo": "rt_luxo",
    "luxury": "rt_luxo",
    "standard": "rt_standard",
    "padrão": "rt_standard",
    "padrao": "rt_standard",
}

# Date pattern for individual matching (dd/mm or dd-mm or dd/mm/yyyy)
_DATE_PARTS = r"(\d{1,2})[/\-](\d{1,2})(?:[/\-](\d{4}))?"

# Date range pattern: "10/02 a 12/02" or "10/02 até 12/02"
# Groups: 1=day1, 2=month1, 3=year1, 4=day2, 5=month2, 6=year2
_DATE_RANGE_PATTERN = rf"{_DATE_PARTS}\s*(?:a|até|ate|-)\s*{_DATE_PARTS}"

# Guest count patterns
_GUEST_PATTERNS = [
    r"(\d+)\s*(?:hóspedes?|hospedes?|pessoas?|pax|adultos?)",
    r"para\s+(\d+)\s*(?:pessoas?|hóspedes?|hospedes?|pax|adultos?)?",
]


def _parse_date(day: str, month: str, year: str | None, reference_year: int) -> date | None:
    """Parse date components into a date object."""
    try:
        d = int(day)
        m = int(month)
        y = int(year) if year else reference_year

        # Basic validation
        if not (1 <= d <= 31) or not (1 <= m <= 12):
            return None

        return date(y, m, d)
    except (ValueError, TypeError):
        return None


def _extract_dates(text: str, reference_year: int) -> tuple[date | None, date | None]:
    """Extract checkin and checkout dates from text.

    Returns:
        Tuple of (checkin, checkout). Either can be None.
    """
    text_lower = text.lower()

    # Try range pattern first (e.g., "10/02 a 12/02")
    match = re.search(_DATE_RANGE_PATTERN, text_lower)
    if match:
        # Groups: 1=day1, 2=month1, 3=year1, 4=day2, 5=month2, 6=year2
        checkin = _parse_date(match.group(1), match.group(2), match.group(3), reference_year)
        checkout = _parse_date(match.group(4), match.group(5), match.group(6), reference_year)
        if checkin and checkout:
            return (checkin, checkout)

    # Fallback: find individual dates
    date_matches = list(re.finditer(_DATE_PARTS, text_lower))

    if len(date_matches) >= 2:
        # First two dates as checkin/checkout
        m1 = date_matches[0]
        m2 = date_matches[1]
        checkin = _parse_date(m1.group(1), m1.group(2), m1.group(3), reference_year)
        checkout = _parse_date(m2.group(1), m2.group(2), m2.group(3), reference_year)
        return (checkin, checkout)

    if len(date_matches) == 1:
        # Only one date found
        m = date_matches[0]
        checkin = _parse_date(m.group(1), m.group(2), m.group(3), reference_year)
        return (checkin, None)

    return (None, None)


def _extract_guest_count(text: str) -> int | None:
    """Extract guest count from text."""
    text_lower = text.lower()

    for pattern in _GUEST_PATTERNS:
        match = re.search(pattern, text_lower)
        if match:
            try:
                count = int(match.group(1))
                if 1 <= count <= 20:  # Reasonable range
                    return count
            except (ValueError, IndexError):
                continue

    return None


def _extract_room_type(text: str, aliases: dict[str, str]) -> str | None:
    """Extract room type ID from text using aliases."""
    text_lower = text.lower()

    # Sort by length descending to match longer aliases first
    sorted_aliases = sorted(aliases.keys(), key=len, reverse=True)

    for alias in sorted_aliases:
        # Word boundary match to avoid partial matches
        pattern = rf"\b{re.escape(alias)}\b"
        if re.search(pattern, text_lower):
            return aliases[alias]

    return None


def parse_intent(
    text: str,
    *,
    room_type_aliases: dict[str, str] | None = None,
    reference_date: date | None = None,
) -> ParsedIntent:
    """Parse user message to extract booking intent.

    Args:
        text: User message text. NEVER logged.
        room_type_aliases: Optional override for room type alias mapping.
        reference_date: Reference date for year inference (default: today).

    Returns:
        ParsedIntent with extracted fields and list of missing fields.
    """
    if reference_date is None:
        reference_date = date.today()

    aliases = room_type_aliases or DEFAULT_ROOM_TYPE_ALIASES

    # Extract all fields
    checkin, checkout = _extract_dates(text, reference_date.year)
    guest_count = _extract_guest_count(text)
    room_type_id = _extract_room_type(text, aliases)

    # Validate dates: checkin must be before checkout
    if checkin and checkout and checkin >= checkout:
        # Invalid: reset both and mark as missing
        checkin = None
        checkout = None

    # Build missing list
    missing: list[str] = []
    if checkin is None:
        missing.append("checkin")
    if checkout is None:
        missing.append("checkout")
    if room_type_id is None:
        missing.append("room_type")
    if guest_count is None:
        missing.append("guest_count")

    return ParsedIntent(
        checkin=checkin,
        checkout=checkout,
        room_type_id=room_type_id,
        guest_count=guest_count,
        missing=missing,
    )
