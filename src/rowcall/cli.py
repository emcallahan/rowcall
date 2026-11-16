"""Command-line entry point: rowcall run data.csv --template call.yaml --dry-run"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Mapping, TextIO

from . import __version__
from .csvio import CsvError, read_csv
from .template import RenderedRequest, TemplateError, load_template, render

EXIT_OK = 0
EXIT_ROW_PROBLEMS = 1
EXIT_USAGE = 2

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
    return parser


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    env = os.environ if env is None else env
    args = build_parser().parse_args(argv)

    if args.command == "run":
        return run_command(args, env, out)
    return EXIT_USAGE


def run_command(args: argparse.Namespace, env: Mapping[str, str], out: TextIO) -> int:
    def say(line: str = "") -> None:
        print(line, file=out)

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

    if not args.dry_run:
        say("Sending requests is not built yet (coming in milestone 2). Use --dry-run for now.")
        return EXIT_USAGE

    previews: list[tuple[int, RenderedRequest]] = []
    problems: list[tuple[int, str]] = []
    for row in data.rows:
        try:
            request = render(template, row.values, env, mask_secrets=True)
        except TemplateError as exc:
            problems.append((row.line, str(exc)))
            continue
        if len(previews) < max(args.show, 0):
            previews.append((row.line, request))

    if previews:
        say()
        say(f"Preview of the first {len(previews)} request(s):")
        for line, request in previews:
            say()
            say(f"  [line {line}] {request.method} {request.url}")
            for name, value in request.headers.items():
                say(f"    {name}: {value}")
            body = request.body_text()
            if body is not None:
                for body_line in body.splitlines():
                    say(f"    {body_line}")

    ready = len(data.rows) - len(problems)
    say()
    say(f"Checked {len(data.rows)} rows: {ready} ready, {len(problems)} with problems.")
    for line, message in problems[:MAX_PROBLEMS_SHOWN]:
        say(f"  line {line}: {message}")
    if len(problems) > MAX_PROBLEMS_SHOWN:
        say(f"  ...and {len(problems) - MAX_PROBLEMS_SHOWN} more")
    say()
    say("Dry run only. No requests were sent.")

    return EXIT_ROW_PROBLEMS if problems else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
