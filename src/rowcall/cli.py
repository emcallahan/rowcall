"""Command-line entry point: rowcall run data.csv --template call.yaml [--dry-run]"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping, TextIO
from urllib.parse import urlparse

import requests

from . import __version__
from .csvio import CsvData, CsvError, Row, read_csv
from .results import ResultsWriter
from .retry import RateLimiter, RetryingSender, RetryPolicy
from .sender import SendResult, send
from .template import RenderedRequest, RequestTemplate, TemplateError, load_template, render

EXIT_OK = 0
EXIT_ROW_PROBLEMS = 1
EXIT_USAGE = 2
EXIT_CANCELLED = 3
EXIT_INTERRUPTED = 130  # the usual exit code for a program stopped with Ctrl+C

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
    run.add_argument(
        "--rate",
        type=float,
        default=5.0,
        metavar="PER_SECOND",
        help="at most this many requests per second, retries included (default: 5, 0 = no limit)",
    )
    run.add_argument(
        "--retries",
        type=int,
        default=3,
        metavar="N",
        help="retries per row when it is safe to retry (default: 3, 0 = never retry)",
    )
    run.add_argument(
        "--max-wait",
        type=float,
        default=60.0,
        metavar="SECONDS",
        help="longest wait before one retry; give up if the server asks for longer (default: 60)",
    )
    return parser


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    out: TextIO | None = None,
    ask: Callable[[str], str] | None = None,
    interactive: bool | None = None,
    sleep: Callable[[float], None] = time.sleep,
    send_fn: Callable[..., SendResult] = send,
) -> int:
    out = out or sys.stdout
    env = os.environ if env is None else env
    ask = ask or input
    if interactive is None:
        interactive = sys.stdin.isatty()
    args = build_parser().parse_args(argv)

    if args.command == "run":
        return run_command(args, env, out, ask, interactive, sleep, send_fn)
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
    sleep: Callable[[float], None],
    send_fn: Callable[..., SendResult],
) -> int:
    def say(line: str = "") -> None:
        print(line, file=out, flush=True)

    if args.retries < 0 or args.rate < 0 or args.max_wait < 0 or args.timeout <= 0:
        say("Error: --retries, --rate and --max-wait cannot be negative, and --timeout must be above 0.")
        return EXIT_USAGE

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
    if args.rate > 0:
        minutes = len(ready) / args.rate / 60
        estimate = f"about {minutes:.0f} minute(s)" if minutes >= 1 else "under a minute"
        say(f"Pace: up to {args.rate:g} request(s) per second, {estimate} without retries.")

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

    counts = {"sent_ok": 0, "sent_failed": 0, "interrupted": 0, "not_sent": 0}
    total = len(ready)
    position = 0  # index into `checked` of the row being worked on
    interrupted = False

    with writer, requests.Session() as session:

        def on_retry(attempt: int, result: SendResult, delay: float) -> None:
            reason = writer.redact(result.error)
            say(f"      {reason}, retrying in {delay:g}s (retry {attempt} of {args.retries})")

        sender = RetryingSender(
            send_fn=lambda request: send_fn(request, session, args.timeout),
            policy=RetryPolicy(max_retries=args.retries, max_wait=args.max_wait),
            limiter=RateLimiter(args.rate, sleep=sleep),
            sleep=sleep,
            on_retry=on_retry,
        )

        say()
        number = 0
        try:
            for position, item in enumerate(checked):
                if item.problem:
                    writer.write(item.row.line, item.row.values, "skipped", error=item.problem)
                    continue
                number += 1
                result, attempts = sender.send(item.request)
                outcome = "sent_ok" if result.ok else "sent_failed"
                counts[outcome] += 1
                writer.write(
                    item.row.line,
                    item.row.values,
                    outcome,
                    status=result.status,
                    error=result.error,
                    response=result.response_text,
                    elapsed_ms=result.elapsed_ms,
                    attempts=attempts,
                )
                tries = f", {attempts} attempts" if attempts > 1 else ""
                if result.ok:
                    say(f"  [{number}/{total}] line {item.row.line}: {result.status} ({result.elapsed_ms} ms{tries})")
                else:
                    say(f"  [{number}/{total}] line {item.row.line}: FAILED {writer.redact(result.error)}{tries}")
        except KeyboardInterrupt:
            interrupted = True
            current = checked[position]
            if not current.problem:
                if sender.attempted:
                    counts["interrupted"] += 1
                    writer.write(
                        current.row.line,
                        current.row.values,
                        "interrupted",
                        error="stopped while this request was in progress; check whether it went through",
                    )
                else:
                    counts["not_sent"] += 1
                    writer.write(current.row.line, current.row.values, "not_sent", error="stopped before sending")
            for item in checked[position + 1 :]:
                if item.problem:
                    writer.write(item.row.line, item.row.values, "skipped", error=item.problem)
                else:
                    counts["not_sent"] += 1
                    writer.write(item.row.line, item.row.values, "not_sent", error="stopped before sending")

    say()
    if interrupted:
        say("Stopped by Ctrl+C. Every row is in the results file, including the ones not sent.")
    summary = f"{counts['sent_ok']} succeeded, {counts['sent_failed']} failed, {len(problems)} skipped"
    if interrupted:
        summary += f", {counts['interrupted']} interrupted, {counts['not_sent']} not sent"
    say(f"Done: {summary}.")
    say(f"Results: {results_path}")
    if interrupted:
        return EXIT_INTERRUPTED
    return EXIT_OK if counts["sent_failed"] == 0 and not problems else EXIT_ROW_PROBLEMS


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
