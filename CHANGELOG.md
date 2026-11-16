# Changelog

## 0.1.0 - 2026-11-16

First release.

- Request templates in YAML: `{{column}}` from the CSV, `${NAME}` from
  environment variables
- Dry run that checks every row and previews requests with secrets masked
- Sending with a confirmation prompt, and a results CSV written as each row
  finishes
- Rows with problems block the run unless `--skip-invalid` is given
- Rate limiting (`--rate`), and retries only where they cannot do something
  twice, honoring `Retry-After`
- Clean stop on Ctrl+C with every row accounted for in the results file
