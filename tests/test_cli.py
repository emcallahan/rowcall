import io
from pathlib import Path

from rowcall.cli import EXIT_OK, EXIT_ROW_PROBLEMS, EXIT_USAGE, main

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "jsonplaceholder"
TEMPLATE = str(EXAMPLES / "create-post.yaml")


def run(args, env=None):
    out = io.StringIO()
    code = main(args, env=env if env is not None else {}, out=out)
    return code, out.getvalue()


def test_dry_run_example_previews_and_masks_secret():
    code, out = run(
        ["run", str(EXAMPLES / "posts.csv"), "-t", TEMPLATE, "--dry-run"],
        env={"DEMO_TOKEN": "super-secret-value"},
    )
    assert code == EXIT_OK
    assert "Checked 4 rows: 4 ready, 0 with problems." in out
    assert "Authorization: Bearer ****" in out
    assert "super-secret-value" not in out
    assert "No requests were sent." in out
    assert out.count("[line ") == 3


def test_dry_run_reports_problem_rows():
    code, out = run(
        ["run", str(EXAMPLES / "posts-with-problems.csv"), "-t", TEMPLATE, "--dry-run"],
        env={"DEMO_TOKEN": "x"},
    )
    assert code == EXIT_ROW_PROBLEMS
    assert "1 ready, 2 with problems" in out
    assert "line 3: column 'user_id' is empty" in out
    assert "line 4: column 'title' is empty" in out


def test_missing_env_var_stops_before_checking_rows():
    code, out = run(["run", str(EXAMPLES / "posts.csv"), "-t", TEMPLATE, "--dry-run"])
    assert code == EXIT_USAGE
    assert "DEMO_TOKEN" in out


def test_column_missing_from_csv(tmp_path):
    csv_path = tmp_path / "d.csv"
    csv_path.write_text("user_id,title\n1,a\n", encoding="utf-8")
    code, out = run(["run", str(csv_path), "-t", TEMPLATE, "--dry-run"], env={"DEMO_TOKEN": "x"})
    assert code == EXIT_USAGE
    assert "not in the CSV: body" in out


def test_sending_is_not_built_yet():
    code, out = run(["run", str(EXAMPLES / "posts.csv"), "-t", TEMPLATE], env={"DEMO_TOKEN": "x"})
    assert code == EXIT_USAGE
    assert "--dry-run" in out
