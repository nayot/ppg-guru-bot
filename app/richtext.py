"""Convert the LLM's Markdown-ish answers into LINE Flex Messages.

Plain LINE TextMessages can't render bold or tables — LINE's chat UI shows
`**bold**` and `| a | b |` literally. Flex Messages support bold via text
"spans" and tables via nested boxes, so we do a small, purpose-built Markdown
subset -> Flex conversion here instead of pulling in a general Markdown
renderer (LINE's layout model isn't HTML).
"""

import json
import re

from linebot.v3.messaging import (
    FlexBox,
    FlexBubble,
    FlexMessage,
    FlexSpan,
    FlexText,
)

ALT_TEXT_MAX = 400
BUBBLE_JSON_LIMIT = 30_000

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_TABLE_SEPARATOR_RE = re.compile(r"^\|?[\s:-]+\|[\s:|-]*$")


def _inline_spans(text: str) -> list[FlexSpan]:
    spans = []
    pos = 0
    for m in _BOLD_RE.finditer(text):
        if m.start() > pos:
            spans.append(FlexSpan(text=text[pos:m.start()]))
        spans.append(FlexSpan(text=m.group(1), weight="bold"))
        pos = m.end()
    if pos < len(text):
        spans.append(FlexSpan(text=text[pos:]))
    return spans or [FlexSpan(text=text)]


def _paragraph(text: str) -> FlexText:
    return FlexText(wrap=True, size="sm", contents=_inline_spans(text))


def _bullet(text: str) -> FlexBox:
    return FlexBox(
        layout="horizontal",
        spacing="sm",
        contents=[
            FlexText(text="•", size="sm", flex=0),
            FlexText(wrap=True, size="sm", flex=1, contents=_inline_spans(text)),
        ],
    )


def _is_table_row(line: str) -> bool:
    return line.startswith("|") and line.endswith("|")


def _split_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip("|").split("|")]


def _table_row(row: list[str], *, header: bool) -> FlexBox:
    return FlexBox(
        layout="horizontal",
        spacing="sm",
        contents=[
            FlexBox(
                layout="vertical",
                flex=1,
                contents=[_paragraph(f"**{cell}**" if header else cell)],
            )
            for cell in row
        ],
    )


def _table(rows: list[list[str]]) -> FlexBox:
    row_boxes = [_table_row(row, header=(i == 0)) for i, row in enumerate(rows)]
    return FlexBox(layout="vertical", spacing="xs", margin="md", contents=row_boxes)


def markdown_to_flex_contents(markdown: str) -> list:
    lines = [line.rstrip() for line in markdown.strip("\n").split("\n")]
    blocks: list = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped:
            i += 1
            continue

        if (
            _is_table_row(stripped)
            and i + 1 < len(lines)
            and _TABLE_SEPARATOR_RE.match(lines[i + 1].strip())
        ):
            rows = [_split_row(stripped)]
            i += 2
            while i < len(lines) and _is_table_row(lines[i].strip()):
                rows.append(_split_row(lines[i].strip()))
                i += 1
            blocks.append(_table(rows))
            continue

        if stripped.startswith(("- ", "* ")):
            blocks.append(_bullet(stripped[2:].strip()))
            i += 1
            continue

        if stripped.startswith("#"):
            blocks.append(
                FlexText(text=stripped.lstrip("#").strip(), weight="bold", size="md", wrap=True)
            )
            i += 1
            continue

        blocks.append(_paragraph(stripped))
        i += 1

    return blocks or [_paragraph(markdown.strip() or " ")]


def strip_markdown(markdown: str) -> str:
    """Plain-text fallback used for altText and if Flex conversion is skipped."""
    text = _BOLD_RE.sub(r"\1", markdown)
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or _TABLE_SEPARATOR_RE.match(stripped):
            continue
        stripped = stripped.lstrip("#").strip()
        if _is_table_row(stripped):
            stripped = " | ".join(_split_row(stripped))
        elif stripped.startswith(("- ", "* ")):
            stripped = stripped[2:].strip()
        lines.append(stripped)
    return " ".join(lines)


def build_reply_message(markdown: str):
    """Build a FlexMessage from `markdown`, falling back to plain text.

    Falls back if conversion produces an oversized bubble (LINE caps a single
    bubble's JSON at 30KB) or otherwise fails, so a malformed/huge LLM answer
    can never break message delivery.
    """
    from linebot.v3.messaging import TextMessage

    try:
        bubble = FlexBubble(
            body=FlexBox(layout="vertical", spacing="md", contents=markdown_to_flex_contents(markdown))
        )
        alt_text = strip_markdown(markdown)[:ALT_TEXT_MAX] or "New message"
        message = FlexMessage(alt_text=alt_text, contents=bubble)
        if len(json.dumps(message.to_dict())) > BUBBLE_JSON_LIMIT:
            raise ValueError("flex bubble exceeds LINE's 30KB size limit")
        return message
    except Exception:
        return TextMessage(text=strip_markdown(markdown)[:4999])
