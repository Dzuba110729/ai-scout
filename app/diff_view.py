"""Двухколоночный вид «было/стало» для карточки изменения.

Хранимый `PageChange.diff_text` — unified diff (одна колонка со знаками +/-), его
неудобно читать нетехническому человеку. Здесь строки старого и нового текста
выравниваются попарно, чтобы показать их рядом двумя колонками.
"""

import difflib
from dataclasses import dataclass

CONTEXT_LINES = 3
MAX_ROWS = 400


@dataclass
class SideBySideRow:
    """Одна строка таблицы. kind: equal | changed | added | removed | skip.

    Для kind="skip" строки пустые, а skipped хранит число свёрнутых совпадающих строк.
    """

    old_line: str | None
    new_line: str | None
    kind: str
    skipped: int = 0


def _pair_replaced(old_block: list[str], new_block: list[str]) -> list[SideBySideRow]:
    rows = []
    for index in range(max(len(old_block), len(new_block))):
        old_line = old_block[index] if index < len(old_block) else None
        new_line = new_block[index] if index < len(new_block) else None
        if old_line is None:
            rows.append(SideBySideRow(None, new_line, "added"))
        elif new_line is None:
            rows.append(SideBySideRow(old_line, None, "removed"))
        else:
            rows.append(SideBySideRow(old_line, new_line, "changed"))
    return rows


def build_side_by_side(
    old_text: str, new_text: str, *, context: int = CONTEXT_LINES, max_rows: int = MAX_ROWS
) -> list[SideBySideRow]:
    old_lines = [line for line in old_text.splitlines() if line.strip()]
    new_lines = [line for line in new_text.splitlines() if line.strip()]

    opcodes = difflib.SequenceMatcher(None, old_lines, new_lines).get_opcodes()
    rows: list[SideBySideRow] = []

    for index, (tag, i1, i2, j1, j2) in enumerate(opcodes):
        if tag == "equal":
            # Совпадающие куски показываем только рядом с изменениями: в начале
            # документа не нужен «хвост» до первой правки, в конце — после последней.
            head = 0 if index == 0 else context
            tail = 0 if index == len(opcodes) - 1 else context
            count = i2 - i1

            if count > head + tail:
                for offset in range(head):
                    rows.append(SideBySideRow(old_lines[i1 + offset], new_lines[j1 + offset], "equal"))
                rows.append(SideBySideRow(None, None, "skip", skipped=count - head - tail))
                for offset in range(tail, 0, -1):
                    rows.append(SideBySideRow(old_lines[i2 - offset], new_lines[j2 - offset], "equal"))
            else:
                for offset in range(count):
                    rows.append(SideBySideRow(old_lines[i1 + offset], new_lines[j1 + offset], "equal"))

        elif tag == "replace":
            rows.extend(_pair_replaced(old_lines[i1:i2], new_lines[j1:j2]))
        elif tag == "delete":
            rows.extend(SideBySideRow(line, None, "removed") for line in old_lines[i1:i2])
        elif tag == "insert":
            rows.extend(SideBySideRow(None, line, "added") for line in new_lines[j1:j2])

    return rows[:max_rows]
