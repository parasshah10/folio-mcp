"""Heading-based section parsing. Shared across backends —
any backend that stores markdown needs section read/write."""

from __future__ import annotations

import re


def find_section(content: str, heading: str) -> tuple[int, int] | None:
    """Find the start and end character positions of a section.

    A section starts at the heading line and ends just before the next
    heading of the same or higher level (fewer or equal #'s), or at EOF.

    Args:
        content: Full markdown content.
        heading: Heading text WITHOUT the # prefix. e.g. 'Status' not '## Status'.

    Returns:
        (start, end) character positions, or None if heading not found.
    """
    lines = content.split("\n")
    heading_lower = heading.strip().lower()
    start_line = None
    heading_level = None

    for i, line in enumerate(lines):
        match = re.match(r"^(#{1,6})\s+(.+)$", line.strip())
        if not match:
            continue

        level = len(match.group(1))
        text = match.group(2).strip().lower()

        if start_line is None:
            # Looking for our target heading
            if text == heading_lower:
                start_line = i
                heading_level = level
        else:
            # Found our heading already — look for the end
            if level <= heading_level:
                # Next heading at same or higher level = end of section
                end_line = i
                start = _line_offset(lines, start_line)
                end = _line_offset(lines, end_line)
                return (start, end)

    if start_line is not None:
        # Section runs to end of file
        return (_line_offset(lines, start_line), len(content))

    return None


def extract_section(content: str, heading: str) -> str | None:
    """Extract the content of a section (including its heading line).

    Returns None if heading not found.
    """
    span = find_section(content, heading)
    if span is None:
        return None
    start, end = span
    return content[start:end].rstrip("\n")


def extract_section_body(content: str, heading: str) -> str | None:
    """Extract section content WITHOUT the heading line itself.

    Returns None if heading not found.
    """
    span = find_section(content, heading)
    if span is None:
        return None
    start, end = span
    section = content[start:end]
    # Remove the heading line
    first_newline = section.find("\n")
    if first_newline == -1:
        return ""
    return section[first_newline + 1 :].rstrip("\n")


def replace_section(content: str, heading: str, new_body: str) -> str:
    """Replace the body of a section, keeping the heading line.

    Raises ValueError if heading not found.
    """
    span = find_section(content, heading)
    if span is None:
        raise ValueError(f"Heading not found: '{heading}'")

    start, end = span
    section = content[start:end]

    # Preserve the heading line
    first_newline = section.find("\n")
    if first_newline == -1:
        heading_line = section
    else:
        heading_line = section[:first_newline]

    # Rebuild: everything before section + heading + new body + everything after
    new_section = heading_line + "\n" + new_body.strip() + "\n"
    return content[:start] + new_section + content[end:]


def list_headings(content: str) -> list[dict[str, str | int]]:
    """List all headings in the content.

    Returns list of {'level': int, 'text': str} dicts.
    """
    headings = []
    for line in content.split("\n"):
        match = re.match(r"^(#{1,6})\s+(.+)$", line.strip())
        if match:
            headings.append({
                "level": len(match.group(1)),
                "text": match.group(2).strip(),
            })
    return headings


def _line_offset(lines: list[str], line_index: int) -> int:
    """Get the character offset of a line index."""
    offset = 0
    for i in range(line_index):
        offset += len(lines[i]) + 1  # +1 for newline
    return offset
