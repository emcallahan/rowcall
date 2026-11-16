"""Read the input CSV."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path


class CsvError(Exception):
    """Raised when the CSV file cannot be used."""


@dataclass
class Row:
    line: int  # line number in the file, so problems can be found in a spreadsheet
    values: dict[str, str]


@dataclass
class CsvData:
    columns: list[str]
    rows: list[Row]


def read_csv(path: str | Path) -> CsvData:
    path = Path(path)
    try:
        # utf-8-sig drops the hidden marker Excel adds to the start of CSV exports.
        handle = path.open(newline="", encoding="utf-8-sig")
    except FileNotFoundError:
        raise CsvError(f"CSV file not found: {path}") from None

    with handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration:
            raise CsvError("CSV file is empty") from None

        columns = [name.strip() for name in header]
        if not any(columns):
            raise CsvError("first line of the CSV must be column names")
        blanks = [i + 1 for i, name in enumerate(columns) if name == ""]
        if blanks:
            raise CsvError(f"column(s) {', '.join(map(str, blanks))} have no name in the header")
        duplicates = sorted({name for name in columns if columns.count(name) > 1})
        if duplicates:
            raise CsvError(f"duplicate column name(s): {', '.join(duplicates)}")

        rows: list[Row] = []
        for values in reader:
            if not any(v.strip() for v in values):
                continue  # skip blank lines
            if len(values) > len(columns):
                raise CsvError(
                    f"line {reader.line_num} has {len(values)} values but the header has {len(columns)} columns"
                )
            padded = values + [""] * (len(columns) - len(values))
            rows.append(Row(line=reader.line_num, values=dict(zip(columns, padded))))

    return CsvData(columns=columns, rows=rows)
