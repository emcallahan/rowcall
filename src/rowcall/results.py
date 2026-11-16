"""Write the results CSV: every input column, plus what happened to that row."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable

RESULT_COLUMNS = [
    "rowcall_line",
    "rowcall_outcome",  # sent_ok, sent_failed, skipped, interrupted, or not_sent
    "rowcall_status",
    "rowcall_error",
    "rowcall_response",
    "rowcall_ms",
    "rowcall_attempts",
]

RESPONSE_LIMIT = 500


class ResultsWriter:
    """Writes one line per row as soon as it finishes, so a crash keeps what was done."""

    def __init__(self, path: Path, input_columns: list[str], secrets: Iterable[str] = ()):
        self.path = path
        self.input_columns = input_columns
        # Longest first, so a secret that contains another is fully hidden.
        self.secrets = sorted({s for s in secrets if s}, key=len, reverse=True)
        self._handle = path.open("x", newline="", encoding="utf-8")
        self._writer = csv.writer(self._handle)
        self._writer.writerow(input_columns + RESULT_COLUMNS)
        self._handle.flush()

    def write(
        self,
        line: int,
        values: dict[str, str],
        outcome: str,
        status: int | None = None,
        error: str = "",
        response: str = "",
        elapsed_ms: int | None = None,
        attempts: int | None = None,
    ) -> None:
        self._writer.writerow(
            [values.get(c, "") for c in self.input_columns]
            + [
                line,
                outcome,
                "" if status is None else status,
                self.redact(error),
                # Redact before cutting, so a secret is never left half-visible.
                self.redact(_one_line(response))[:RESPONSE_LIMIT],
                "" if elapsed_ms is None else elapsed_ms,
                "" if attempts is None else attempts,
            ]
        )
        self._handle.flush()

    def redact(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "****")
        return text

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> "ResultsWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _one_line(text: str) -> str:
    return " ".join(text.split())
