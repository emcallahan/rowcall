import csv
import io
import json

from rowcall.cli import EXIT_CANCELLED, EXIT_INTERRUPTED, EXIT_OK, EXIT_ROW_PROBLEMS, EXIT_USAGE, main
from rowcall.sender import send


def setup(tmp_path, server, rows, path="/items/{{id}}", method="POST"):
    csv_path = tmp_path / "in.csv"
    csv_path.write_text("id,name\n" + "".join(f"{a},{b}\n" for a, b in rows), encoding="utf-8")
    template = tmp_path / "t.yaml"
    template.write_text(
        f"method: {method}\n"
        f"url: {server.base_url}{path}\n"
        "headers:\n"
        "  Authorization: Bearer ${TOKEN}\n"
        "body:\n"
        '  name: "{{name}}"\n',
        encoding="utf-8",
    )
    return csv_path, template, tmp_path / "results.csv"


def run(args, env=None, answer=None, interactive=False, sleeps=None, send_fn=None):
    out = io.StringIO()
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return answer

    # Record waits instead of really sleeping, so tests stay fast.
    sleeps = [] if sleeps is None else sleeps
    extra = {"send_fn": send_fn} if send_fn else {}
    code = main(
        args,
        env=env or {"TOKEN": "s3cret-token"},
        out=out,
        ask=ask,
        interactive=interactive,
        sleep=sleeps.append,
        **extra,
    )
    return code, out.getvalue(), asked


def read_results(path):
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_sends_every_row_and_writes_results(tmp_path, server):
    csv_path, template, results = setup(tmp_path, server, [("1", "a"), ("2", "b")])
    code, out, _ = run(["run", str(csv_path), "-t", str(template), "-o", str(results), "--yes"])

    assert code == EXIT_OK
    assert "Done: 2 succeeded, 0 failed, 0 skipped." in out
    assert [r["path"] for r in server.received] == ["/items/1", "/items/2"]
    first = server.received[0]
    assert first["method"] == "POST"
    assert first["headers"]["Authorization"] == "Bearer s3cret-token"
    assert json.loads(first["body"]) == {"name": "a"}

    rows = read_results(results)
    assert [r["rowcall_outcome"] for r in rows] == ["sent_ok", "sent_ok"]
    assert rows[0]["id"] == "1" and rows[0]["rowcall_status"] == "201" and rows[0]["rowcall_line"] == "2"


def test_failures_are_recorded_and_run_continues(tmp_path, server):
    csv_path, template, results = setup(tmp_path, server, [("1", "a"), ("2", "b")], path="/fail/{{id}}")
    code, out, _ = run(["run", str(csv_path), "-t", str(template), "-o", str(results), "--yes"])

    assert code == EXIT_ROW_PROBLEMS
    assert len(server.received) == 2
    assert "Done: 0 succeeded, 2 failed, 0 skipped." in out
    rows = read_results(results)
    assert rows[0]["rowcall_outcome"] == "sent_failed"
    assert rows[0]["rowcall_status"] == "500"
    assert "something broke" in rows[0]["rowcall_response"]


def test_secrets_echoed_back_are_redacted_in_results(tmp_path, server):
    csv_path, template, results = setup(tmp_path, server, [("1", "a")], path="/echo-auth/{{id}}")
    run(["run", str(csv_path), "-t", str(template), "-o", str(results), "--yes"])

    text = results.read_text(encoding="utf-8")
    assert "s3cret-token" not in text
    assert "Bearer ****" in text


def test_problem_rows_block_sending_by_default(tmp_path, server):
    csv_path, template, results = setup(tmp_path, server, [("1", "a"), ("", "b")])
    code, out, _ = run(["run", str(csv_path), "-t", str(template), "-o", str(results), "--yes"])

    assert code == EXIT_ROW_PROBLEMS
    assert server.received == []
    assert not results.exists()
    assert "--skip-invalid" in out


def test_skip_invalid_sends_good_rows_and_marks_skipped(tmp_path, server):
    csv_path, template, results = setup(tmp_path, server, [("1", "a"), ("", "b")])
    code, out, _ = run(
        ["run", str(csv_path), "-t", str(template), "-o", str(results), "--yes", "--skip-invalid"]
    )

    assert code == EXIT_ROW_PROBLEMS
    assert [r["path"] for r in server.received] == ["/items/1"]
    rows = read_results(results)
    assert [r["rowcall_outcome"] for r in rows] == ["sent_ok", "skipped"]
    assert rows[1]["rowcall_error"] == "column 'id' is empty"


def test_asks_before_sending_and_respects_no(tmp_path, server):
    csv_path, template, results = setup(tmp_path, server, [("1", "a")], method="DELETE")
    code, out, asked = run(
        ["run", str(csv_path), "-t", str(template), "-o", str(results)], answer="no", interactive=True
    )

    assert code == EXIT_CANCELLED
    assert "About to send 1 DELETE request(s) to 127.0.0.1:" in out
    assert asked == ["Type yes to continue: "]
    assert server.received == []
    assert not results.exists()


def test_asks_before_sending_and_proceeds_on_yes(tmp_path, server):
    csv_path, template, results = setup(tmp_path, server, [("1", "a")])
    code, _, _ = run(
        ["run", str(csv_path), "-t", str(template), "-o", str(results)], answer="yes", interactive=True
    )
    assert code == EXIT_OK
    assert len(server.received) == 1


def test_will_not_overwrite_existing_results(tmp_path, server):
    csv_path, template, results = setup(tmp_path, server, [("1", "a")])
    results.write_text("keep me", encoding="utf-8")
    code, out, _ = run(["run", str(csv_path), "-t", str(template), "-o", str(results), "--yes"])

    assert code == EXIT_USAGE
    assert "already exists" in out
    assert results.read_text(encoding="utf-8") == "keep me"
    assert server.received == []


def test_connection_error_is_recorded(tmp_path):
    csv_path = tmp_path / "in.csv"
    csv_path.write_text("id\n1\n", encoding="utf-8")
    template = tmp_path / "t.yaml"
    # Port 9 on localhost has nothing listening.
    template.write_text("url: http://127.0.0.1:9/x/{{id}}\n", encoding="utf-8")
    results = tmp_path / "results.csv"
    code, out, _ = run(["run", str(csv_path), "-t", str(template), "-o", str(results), "--yes", "--timeout", "2", "--retries", "0"])

    assert code == EXIT_ROW_PROBLEMS
    rows = read_results(results)
    assert rows[0]["rowcall_outcome"] == "sent_failed"
    assert rows[0]["rowcall_status"] == ""
    assert rows[0]["rowcall_error"].startswith("could not connect")


def test_retries_after_429_and_honors_retry_after(tmp_path, server):
    csv_path, template, results = setup(tmp_path, server, [("1", "a")], path="/flaky/{{id}}")
    sleeps = []
    code, out, _ = run(
        ["run", str(csv_path), "-t", str(template), "-o", str(results), "--yes", "--rate", "0"], sleeps=sleeps
    )

    assert code == EXIT_OK
    assert len(server.received) == 2
    assert sleeps == [2.0]  # the server's Retry-After
    assert "retrying in 2s (retry 1 of 3)" in out
    assert read_results(results)[0]["rowcall_attempts"] == "2"


def test_post_is_not_retried_after_bad_gateway(tmp_path, server):
    # A 502 means the server may have created the record; retrying a POST could duplicate it.
    csv_path, template, results = setup(tmp_path, server, [("1", "a")], path="/gateway/{{id}}")
    code, _, _ = run(["run", str(csv_path), "-t", str(template), "-o", str(results), "--yes"])
    assert code == EXIT_ROW_PROBLEMS
    assert len(server.received) == 1


def test_delete_is_retried_after_bad_gateway(tmp_path, server):
    csv_path, template, results = setup(
        tmp_path, server, [("1", "a")], path="/gateway/{{id}}", method="DELETE"
    )
    sleeps = []
    run(["run", str(csv_path), "-t", str(template), "-o", str(results), "--yes", "--rate", "0"], sleeps=sleeps)
    assert len(server.received) == 4  # first try plus 3 retries
    assert sleeps == [1, 2, 4]


def test_gives_up_when_server_wants_too_long_a_wait(tmp_path, server):
    csv_path, template, results = setup(tmp_path, server, [("1", "a")], path="/busy/{{id}}")
    code, _, _ = run(["run", str(csv_path), "-t", str(template), "-o", str(results), "--yes"])
    assert code == EXIT_ROW_PROBLEMS
    assert len(server.received) == 1
    assert "longer than --max-wait" in read_results(results)[0]["rowcall_error"]


def test_rate_limit_spaces_out_requests(tmp_path, server):
    csv_path, template, results = setup(tmp_path, server, [("1", "a"), ("2", "b"), ("3", "c")])
    sleeps = []
    code, out, _ = run(
        ["run", str(csv_path), "-t", str(template), "-o", str(results), "--yes", "--rate", "2"], sleeps=sleeps
    )
    assert code == EXIT_OK
    # Starts are scheduled at 0s, 0.5s and 1.0s. Sleeps are faked here, so the real clock
    # does not move and the second wait covers the time until the 1.0s slot.
    assert len(sleeps) == 2
    assert 0 < sleeps[0] <= 0.5 and 0.5 < sleeps[1] <= 1.0
    assert "up to 2 request(s) per second" in out


def test_ctrl_c_marks_every_row_and_stops(tmp_path, server):
    csv_path, template, results = setup(tmp_path, server, [("1", "a"), ("2", "b"), ("3", "c"), ("4", "d")])
    calls = []

    def send_fn(request, session, timeout):
        calls.append(request.url)
        if len(calls) == 2:
            raise KeyboardInterrupt  # as if Ctrl+C was pressed while row 2 was in flight
        return send(request, session, timeout)

    code, out, _ = run(
        ["run", str(csv_path), "-t", str(template), "-o", str(results), "--yes"], send_fn=send_fn
    )

    assert code == EXIT_INTERRUPTED
    assert len(server.received) == 1
    rows = read_results(results)
    assert [r["rowcall_outcome"] for r in rows] == ["sent_ok", "interrupted", "not_sent", "not_sent"]
    assert "Stopped by Ctrl+C" in out
    assert "1 succeeded, 0 failed, 0 skipped, 1 interrupted, 2 not sent" in out


def test_bad_numbers_are_rejected(tmp_path, server):
    csv_path, template, results = setup(tmp_path, server, [("1", "a")])
    code, out, _ = run(["run", str(csv_path), "-t", str(template), "--yes", "--retries", "-1"])
    assert code == EXIT_USAGE
    assert server.received == []
