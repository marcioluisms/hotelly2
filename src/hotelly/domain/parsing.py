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

# Adult-specific patterns
_ADULT_PATTERNS = [
    r"(\d+)\s*(?:adultos?|adts?)",
    r"para\s+(\d+)\s*(?:adultos?|adts?)",
]

# Child count patterns (without ages)
_CHILD_COUNT_PATTERNS = [
    r"(\d+)\s*(?:crianças?|criancas?|kids?|chd)",
]

# Children ages patterns (strict: "3 e 7", "3,7", "3 7", optionally with "anos")
_CHILDREN_AGES_PATTERN = re.compile(
    r"(?:crianças?|criancas?|kids?|chd)\s*(?:de\s+)?(\d{1,2}(?:\s*(?:e|,|\s)\s*\d{1,2})*)\s*(?:anos?)?",
    re.IGNORECASE,
)

# Standalone ages pattern (for multi-turn: "3 e 7 anos", "3,7", "3 7")
_STANDALONE_AGES_PATTERN = re.compile(
    r"^(\d{1,2}(?:\s*(?:e|,)\s*\d{1,2})+)\s*(?:anos?)?$",
    re.IGNORECASE,
)


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


def _extract_adults(text: str) -> int | None:
    """Extract adult count from text."""
    text_lower = text.lower()
    for pattern in _ADULT_PATTERNS:
        match = re.search(pattern, text_lower)
        if match:
            try:
                count = int(match.group(1))
                if 1 <= count <= 20:
                    return count
            except (ValueError, IndexError):
                continue
    return None


def _parse_age_list(ages_str: str) -> list[int] | None:
    """Parse a string of ages like '3 e 7', '3,7', '3 7' into a list of ints.

    Returns list of ages (each 0..17) or None if parsing fails.
    """
    # Normalize separators: replace "e" and "," with space
    normalized = re.sub(r'\s*[e,]\s*', ' ', ages_str.strip())
    parts = normalized.split()

    ages = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        try:
            age = int(p)
            if 0 <= age <= 17:
                ages.append(age)
            else:
                return None  # invalid age
        except ValueError:
            return None

    return ages if ages else None


def _extract_children(text: str) -> tuple[int | None, list[int] | None]:
    """Extract child count and ages from text.

    Returns:
        Tuple of (child_count, children_ages).
        - child_count: number of children mentioned, or None
        - children_ages: list of ages if parsed, or None if children mentioned but ages missing
    """
    text_lower = text.lower()

    # Try to extract ages with context (e.g., "crianças de 3 e 7")
    ages_match = _CHILDREN_AGES_PATTERN.search(text_lower)
    if ages_match:
        ages_str = ages_match.group(1)
        ages = _parse_age_list(ages_str)
        if ages is not None:
            return (len(ages), ages)

    # Try standalone ages (multi-turn: just "3 e 7 anos")
    stripped = text.strip()
    standalone_match = _STANDALONE_AGES_PATTERN.match(stripped)
    if standalone_match:
        ages = _parse_age_list(standalone_match.group(1))
        if ages is not None:
            return (len(ages), ages)

    # Try child count only (no ages)
    for pattern in _CHILD_COUNT_PATTERNS:
        match = re.search(pattern, text_lower)
        if match:
            try:
                count = int(match.group(1))
                if 1 <= count <= 10:
                    return (count, None)  # children mentioned but no ages
            except (ValueError, IndexError):
                continue

    return (None, None)


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

    # Extract adult count and children
    adult_count = _extract_adults(text)
    child_count_parsed, children_ages = _extract_children(text)

    # If "X pessoas/hóspedes" used without explicit adults, treat as adults (legacy compat)
    if adult_count is None and guest_count is not None and child_count_parsed is None:
        adult_count = guest_count

    # Validate: if both child_count and ages exist, they must match
    if child_count_parsed is not None and children_ages is not None:
        if len(children_ages) != child_count_parsed:
            children_ages = None  # force re-prompt

    # Build missing list
    missing: list[str] = []
    if checkin is None:
        missing.append("checkin")
    if checkout is None:
        missing.append("checkout")
    if room_type_id is None:
        missing.append("room_type")
    if adult_count is None:
        missing.append("adult_count")
    if child_count_parsed is not None and child_count_parsed > 0 and children_ages is None:
        missing.append("children_ages")

    return ParsedIntent(
        checkin=checkin,
        checkout=checkout,
        room_type_id=room_type_id,
        guest_count=guest_count,
        adult_count=adult_count,
        children_ages=children_ages,
        missing=missing,
    )
