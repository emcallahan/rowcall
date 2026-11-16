"""Command-line entry point: rowcall run data.csv --template call.yaml [--dry-run]"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping, TextIO
from urllib.parse import urlparse

import requests

from . import __version__
from .csvio import CsvData, CsvError, Row, read_csv
from .results import ResultsWriter
from .sender import send
from .template import RenderedRequest, RequestTemplate, TemplateError, load_template, render

EXIT_OK = 0
EXIT_ROW_PROBLEMS = 1
EXIT_USAGE = 2
EXIT_CANCELLED = 3

MAX_PROBLEMS_SHOWN = 10


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rowcall",
        description="Make one API call per row of a CSV file, safely.",
    )
    parser.add_argument("--version", action="version", version=f"rowcall {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run a template against every row of a CSV")
    run.add_argument("csv", help="input CSV file (first line must be column names)")
    run.add_argument("-t", "--template", required=True, help="request template (YAML)")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="check every row and preview the requests without sending anything",
    )
    run.add_argument(
        "--show",
        type=int,
        default=3,
        metavar="N",
        help="number of requests to preview in a dry run (default: 3)",
    )
    run.add_argument(
        "-o",
        "--results",
        metavar="FILE",
        help="where to write the results CSV (default: results-<csv name>-<time>.csv)",
    )
    run.add_argument(
        "--skip-invalid",
        action="store_true",
        help="send the good rows even if some rows have problems (they are marked skipped)",
    )
    run.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation before sending")
    run.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="how long to wait for each response (default: 30)",
    )
    return parser


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    out: TextIO | None = None,
    ask: Callable[[str], str] | None = None,
    interactive: bool | None = None,
) -> int:
    out = out or sys.stdout
    env = os.environ if env is None else env
    ask = ask or input
    if interactive is None:
        interactive = sys.stdin.isatty()
    args = build_parser().parse_args(argv)

    if args.command == "run":
        return run_command(args, env, out, ask, interactive)
    return EXIT_USAGE


@dataclass
class CheckedRow:
    row: Row
    request: RenderedRequest | None  # real values, used for sending
    preview: RenderedRequest | None  # secrets masked, safe to print
    problem: str = ""


def run_command(
    args: argparse.Namespace,
    env: Mapping[str, str],
    out: TextIO,
    ask: Callable[[str], str],
    interactive: bool,
) -> int:
    def say(line: str = "") -> None:
        print(line, file=out, flush=True)

    try:
        template = load_template(args.template)
        data = read_csv(args.csv)
    except (TemplateError, CsvError) as exc:
        say(f"Error: {exc}")
        return EXIT_USAGE

    say(f"Template: {template.method} {template.url}")
    say(f"CSV:      {len(data.rows)} rows, columns: {', '.join(data.columns)}")

    missing_columns = sorted(template.columns() - set(data.columns))
    if missing_columns:
        say(f"Error: the template uses column(s) not in the CSV: {', '.join(missing_columns)}")
        return EXIT_USAGE

    missing_env = sorted(name for name in template.env_vars() if not env.get(name))
    if missing_env:
        say(f"Error: set these environment variable(s) first: {', '.join(missing_env)}")
        return EXIT_USAGE

    if not data.rows:
        say("Nothing to do: the CSV has no data rows.")
        return EXIT_OK

    checked = check_rows(template, data, env)
    problems = [c for c in checked if c.problem]
    ready = [c for c in checked if not c.problem]

    if args.dry_run:
        show_previews(say, ready[: max(args.show, 0)])

    say()
    say(f"Checked {len(checked)} rows: {len(ready)} ready, {len(problems)} with problems.")
    show_problems(say, problems)

    if args.dry_run:
        say()
        say("Dry run only. No requests were sent.")
        return EXIT_ROW_PROBLEMS if problems else EXIT_OK

    if problems and not args.skip_invalid:
        say()
        say("Nothing was sent. Fix the rows above, or rerun with --skip-invalid to send only the ready rows.")
        return EXIT_ROW_PROBLEMS

    if not ready:
        say("Nothing to send.")
        return EXIT_ROW_PROBLEMS

    hosts = sorted({urlparse(c.request.url).netloc for c in ready})
    target = hosts[0] if len(hosts) == 1 else f"{len(hosts)} different hosts"
    say()
    say(f"About to send {len(ready)} {template.method} request(s) to {target}.")
    if problems:
        say(f"{len(problems)} row(s) with problems will be skipped.")

    if not args.yes:
        if not interactive:
            say("Not running in an interactive terminal. Rerun with --yes to confirm sending.")
            return EXIT_USAGE
        if ask("Type yes to continue: ").strip().lower() != "yes":
            say("Cancelled. Nothing was sent.")
            return EXIT_CANCELLED

    results_path = Path(args.results) if args.results else default_results_path(args.csv)
    secrets = [env[name] for name in template.env_vars()]
    try:
        writer = ResultsWriter(results_path, data.columns, secrets)
    except FileExistsError:
        say(f"Error: {results_path} already exists. Choose another name with --results.")
        return EXIT_USAGE
    except OSError as exc:
        say(f"Error: cannot create {results_path}: {exc}")
        return EXIT_USAGE

    sent_ok = sent_failed = 0
    total = len(ready)
    with writer, requests.Session() as session:
        say()
        number = 0
        for item in checked:
            if item.problem:
                writer.write(item.row.line, item.row.values, "skipped", error=item.problem)
                continue
            number += 1
            result = send(item.request, session, args.timeout)
            outcome = "sent_ok" if result.ok else "sent_failed"
            writer.write(
                item.row.line,
                item.row.values,
                outcome,
                status=result.status,
                error=result.error,
                response=result.response_text,
                elapsed_ms=result.elapsed_ms,
            )
            if result.ok:
                sent_ok += 1
                say(f"  [{number}/{total}] line {item.row.line}: {result.status} ({result.elapsed_ms} ms)")
            else:
                sent_failed += 1
                say(f"  [{number}/{total}] line {item.row.line}: FAILED {writer.redact(result.error)}")

    say()
    say(f"Done: {sent_ok} succeeded, {sent_failed} failed, {len(problems)} skipped.")
    say(f"Results: {results_path}")
    return EXIT_OK if sent_failed == 0 and not problems else EXIT_ROW_PROBLEMS


def check_rows(template: RequestTemplate, data: CsvData, env: Mapping[str, str]) -> list[CheckedRow]:
    checked: list[CheckedRow] = []
    for row in data.rows:
        try:
            preview = render(template, row.values, env, mask_secrets=True)
            request = render(template, row.values, env)
        except TemplateError as exc:
            checked.append(CheckedRow(row, None, None, str(exc)))
            continue
        checked.append(CheckedRow(row, request, preview))
    return checked


def show_previews(say: Callable[[str], None], items: list[CheckedRow]) -> None:
    if not items:
        return
    say()
    say(f"Preview of the first {len(items)} request(s):")
    for item in items:
        request = item.preview
        say()
        say(f"  [line {item.row.line}] {request.method} {request.url}")
        for name, value in request.headers.items():
            say(f"    {name}: {value}")
        body = request.body_text()
        if body is not None:
            for body_line in body.splitlines():
                say(f"    {body_line}")


def show_problems(say: Callable[[str], None], problems: list[CheckedRow]) -> None:
    for item in problems[:MAX_PROBLEMS_SHOWN]:
        say(f"  line {item.row.line}: {item.problem}")
    if len(problems) > MAX_PROBLEMS_SHOWN:
        say(f"  ...and {len(problems) - MAX_PROBLEMS_SHOWN} more")


def default_results_path(csv_path: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path(f"results-{Path(csv_path).stem}-{stamp}.csv")


if __name__ == "__main__":
    sys.exit(main())
