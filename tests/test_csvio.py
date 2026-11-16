import pytest

from rowcall.csvio import CsvError, read_csv


def write(tmp_path, text, encoding="utf-8"):
    path = tmp_path / "data.csv"
    path.write_text(text, encoding=encoding)
    return path


def test_reads_rows_with_line_numbers(tmp_path):
    data = read_csv(write(tmp_path, "id,name\n1,a\n\n2,b\n"))
    assert data.columns == ["id", "name"]
    assert [(r.line, r.values) for r in data.rows] == [
        (2, {"id": "1", "name": "a"}),
        (4, {"id": "2", "name": "b"}),
    ]


def test_handles_excel_bom_and_header_spaces(tmp_path):
    data = read_csv(write(tmp_path, " id , name\n1,a\n", encoding="utf-8-sig"))
    assert data.columns == ["id", "name"]


def test_short_rows_are_padded(tmp_path):
    data = read_csv(write(tmp_path, "id,name\n1\n"))
    assert data.rows[0].values == {"id": "1", "name": ""}


@pytest.mark.parametrize(
    "text, message",
    [
        ("", "empty"),
        ("id,id\n1,2\n", "duplicate"),
        ("id,,name\n1,2,3\n", "no name"),
        ("id\n1,2\n", "has 2 values"),
    ],
)
def test_rejects_bad_files(tmp_path, text, message):
    with pytest.raises(CsvError, match=message):
        read_csv(write(tmp_path, text))


def test_missing_file(tmp_path):
    with pytest.raises(CsvError, match="not found"):
        read_csv(tmp_path / "nope.csv")
