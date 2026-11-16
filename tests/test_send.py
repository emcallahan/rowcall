import csv
import io
import json

from rowcall.cli import EXIT_CANCELLED, EXIT_OK, EXIT_ROW_PROBLEMS, EXIT_USAGE, main


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


def run(args, env=None, answer=None, interactive=False):
    out = io.StringIO()
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return answer

    code = main(args, env=env or {"TOKEN": "s3cret-token"}, out=out, ask=ask, interactive=interactive)
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
    code, out, _ = run(["run", str(csv_path), "-t", str(template), "-o", str(results), "--yes", "--timeout", "2"])

    assert code == EXIT_ROW_PROBLEMS
    rows = read_results(results)
    assert rows[0]["rowcall_outcome"] == "sent_failed"
    assert rows[0]["rowcall_status"] == ""
    assert rows[0]["rowcall_error"].startswith("could not connect")
