from app.diff_view import build_side_by_side


def _kinds(rows):
    return [row.kind for row in rows]


def test_identical_text_has_no_changes():
    rows = build_side_by_side("одна\nдве\nтри", "одна\nдве\nтри")
    assert set(_kinds(rows)) <= {"equal", "skip"}


def test_replaced_line_is_paired_side_by_side():
    rows = build_side_by_side("цена 1000 руб\nхвост", "цена 2000 руб\nхвост")
    changed = [row for row in rows if row.kind == "changed"]
    assert len(changed) == 1
    assert changed[0].old_line == "цена 1000 руб"
    assert changed[0].new_line == "цена 2000 руб"


def test_added_line_has_empty_old_side():
    rows = build_side_by_side("шапка", "шапка\nрассрочка 12 месяцев")
    added = [row for row in rows if row.kind == "added"]
    assert len(added) == 1
    assert added[0].old_line is None
    assert added[0].new_line == "рассрочка 12 месяцев"


def test_removed_line_has_empty_new_side():
    rows = build_side_by_side("шапка\nстарый оффер", "шапка")
    removed = [row for row in rows if row.kind == "removed"]
    assert len(removed) == 1
    assert removed[0].old_line == "старый оффер"
    assert removed[0].new_line is None


def test_long_unchanged_run_is_collapsed():
    filler = "\n".join(f"строка {i}" for i in range(50))
    rows = build_side_by_side(f"было\n{filler}", f"стало\n{filler}")

    skips = [row for row in rows if row.kind == "skip"]
    assert len(skips) == 1
    assert skips[0].skipped > 0
    # Свёрнутое плюс показанное должно покрывать весь одинаковый хвост.
    equal_shown = len([row for row in rows if row.kind == "equal"])
    assert skips[0].skipped + equal_shown == 50


def test_blank_lines_are_ignored():
    rows = build_side_by_side("текст\n\n\n", "текст")
    assert set(_kinds(rows)) <= {"equal", "skip"}


def test_output_is_capped():
    old = "\n".join(f"старое {i}" for i in range(500))
    new = "\n".join(f"новое {i}" for i in range(500))
    rows = build_side_by_side(old, new, max_rows=40)
    assert len(rows) == 40
