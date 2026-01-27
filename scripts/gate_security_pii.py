#!/usr/bin/env python3
"""Gate G2: Security & PII check for source files.

Fails if:
- print( found in runtime code (src/**)
- Logging calls with sensitive keywords without redaction
- Direct logging of payload/request/body without safe_log_context

Usage:
    python scripts/gate_security_pii.py
"""

import re
import sys
from pathlib import Path

# Keywords that must not appear in logger calls without redaction
SENSITIVE_KEYWORDS = (
    "payload",
    "request.body",
    "body_bytes",
    "request.json",
    "webhook",
    "message_text",
    "phone",
)

# Pattern for print statements
PRINT_PATTERN = re.compile(r"\bprint\s*\(")

# Pattern for logger calls: logger.info/debug/warning/error/critical(...)
LOGGER_CALL_PATTERN = re.compile(
    r"logger\.(debug|info|warning|error|critical|exception)\s*\("
)

# Patterns that indicate proper redaction usage
REDACTION_PATTERNS = (
    "safe_log_context",
    "redact_value",
    "redact_string",
)


def check_file(filepath: Path) -> list[str]:
    """Check a single file for violations. Returns list of error messages."""
    errors = []
    try:
        content = filepath.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []  # Skip binary files

    lines = content.splitlines()

    for lineno, line in enumerate(lines, start=1):
        stripped = line.lstrip()

        # Skip comments
        if stripped.startswith("#"):
            continue

        # Check for print statements (skip if in string or after #)
        if PRINT_PATTERN.search(line):
            # Simple heuristic: skip if print appears after # (inline comment)
            code_part = line.split("#")[0] if "#" in line else line
            if PRINT_PATTERN.search(code_part):
                errors.append(f"{filepath}:{lineno}: print() not allowed in runtime code")

        # Check for logger calls with sensitive keywords
        if LOGGER_CALL_PATTERN.search(line):
            line_lower = line.lower()
            for keyword in SENSITIVE_KEYWORDS:
                if keyword in line_lower:
                    # Check if redaction is used on the same line or nearby context
                    has_redaction = any(rp in line for rp in REDACTION_PATTERNS)
                    if not has_redaction:
                        errors.append(
                            f"{filepath}:{lineno}: logger call with '{keyword}' "
                            "must use redaction (safe_log_context/redact_value)"
                        )

    return errors


def main() -> int:
    """Run gate check on src directory."""
    src_dir = Path("src")

    if not src_dir.exists():
        # Try from project root
        src_dir = Path(__file__).parent.parent / "src"

    if not src_dir.exists():
        sys.stderr.write("Error: src directory not found\n")
        return 1

    all_errors: list[str] = []

    for pyfile in src_dir.rglob("*.py"):
        errors = check_file(pyfile)
        all_errors.extend(errors)

    if all_errors:
        sys.stderr.write("Gate G2 FAILED - Security/PII violations found:\n")
        for err in all_errors:
            sys.stderr.write(f"  {err}\n")
        return 1

    sys.stdout.write("Gate G2 PASSED - No security/PII violations found\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
