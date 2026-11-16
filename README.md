# rowcall

Make one API call per row of a CSV file, safely.

You have a spreadsheet of records and an API that only takes them one at a
time. rowcall fills in a request template for each row, checks every row
before anything is sent, and (soon) sends them with rate limiting, retries,
and a results file you can open in a spreadsheet.

> Status: early. Templates and dry runs work. Sending requests is next.

## Try it

```bash
python -m venv .venv
.venv/Scripts/activate        # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"

export DEMO_TOKEN=anything    # PowerShell: $env:DEMO_TOKEN = "anything"
rowcall run examples/jsonplaceholder/posts.csv \
  -t examples/jsonplaceholder/create-post.yaml --dry-run
```

The dry run checks every row and previews the first few requests. Secrets
from environment variables are shown as `****`. Nothing is sent.

## Templates

```yaml
method: POST
url: https://api.example.com/users/{{user_id}}
headers:
  Authorization: Bearer ${API_TOKEN}
body:
  email: "{{email}}"
```

- `{{column}}` is filled in from the CSV row. Values in the URL are URL-encoded.
- `${NAME}` is filled in from an environment variable, so secrets never live
  in the template or the CSV.
- A mapping or list `body` is sent as JSON. A text `body` is sent as-is.
- A row with an empty value for a column the template uses is reported as a
  problem instead of being sent, so `DELETE /users/{{id}}` can never become
  `DELETE /users/`.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | every row is ready |
| 1 | some rows have problems |
| 2 | the template, CSV, or settings need fixing before anything can run |

## Roadmap

1. [x] Templates and dry run
2. [ ] Send requests and write a results CSV
3. [ ] Rate limiting, retries (honoring `Retry-After`), clean stop on Ctrl+C
4. [ ] Demo GIF and a first release

## Later (not in version 1)

- Typed values in JSON bodies (today every `{{column}}` becomes a string)
- Resume a run from its results file
- OAuth sign-in flows, pagination, GraphQL, scheduling
