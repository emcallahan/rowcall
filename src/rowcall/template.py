"""Load a request template and render it for one CSV row.

A template is a small YAML file:

    method: POST
    url: https://api.example.com/users/{{user_id}}
    headers:
      Authorization: Bearer ${API_TOKEN}
    body:
      email: "{{email}}"

{{column}} is filled in from the CSV row. ${NAME} is filled in from an
environment variable, so secrets never live in the template or the CSV.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

import yaml

ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
ALLOWED_KEYS = {"method", "url", "headers", "body"}

COLUMN_PATTERN = re.compile(r"\{\{\s*([^{}\s]+)\s*\}\}")
ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

SECRET_MASK = "****"


class TemplateError(Exception):
    """Raised when a template is invalid or cannot be rendered for a row."""


@dataclass
class RequestTemplate:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: Any = None

    def columns(self) -> set[str]:
        """Every CSV column name the template refers to."""
        found: set[str] = set()
        _walk_strings([self.url, self.headers, self.body], lambda s: found.update(COLUMN_PATTERN.findall(s)))
        # Header names can hold placeholders too.
        for name in self.headers:
            found.update(COLUMN_PATTERN.findall(name))
        return found

    def env_vars(self) -> set[str]:
        """Every environment variable name the template refers to."""
        found: set[str] = set()
        _walk_strings([self.url, self.headers, self.body], lambda s: found.update(ENV_PATTERN.findall(s)))
        return found


@dataclass
class RenderedRequest:
    method: str
    url: str
    headers: dict[str, str]
    body: Any  # None, a string, or a dict/list that will be sent as JSON

    def body_text(self) -> str | None:
        if self.body is None:
            return None
        if isinstance(self.body, str):
            return self.body
        return json.dumps(self.body, indent=2)


def load_template(path: str | Path) -> RequestTemplate:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise TemplateError(f"template file not found: {path}") from None
    except yaml.YAMLError as exc:
        raise TemplateError(f"template is not valid YAML: {exc}") from None
    return parse_template(raw)


def parse_template(raw: Any) -> RequestTemplate:
    if not isinstance(raw, dict):
        raise TemplateError("template must be a YAML mapping with at least a 'url' key")

    unknown = set(raw) - ALLOWED_KEYS
    if unknown:
        raise TemplateError(
            f"unknown template key(s): {', '.join(sorted(unknown))} "
            f"(allowed: {', '.join(sorted(ALLOWED_KEYS))})"
        )

    method = str(raw.get("method", "GET")).upper()
    if method not in ALLOWED_METHODS:
        raise TemplateError(f"unsupported method '{method}'")

    url = raw.get("url")
    if not isinstance(url, str) or not url.strip():
        raise TemplateError("template needs a 'url'")
    if not re.match(r"^https?://", url):
        raise TemplateError("url must start with http:// or https://")

    headers = raw.get("headers") or {}
    if not isinstance(headers, dict):
        raise TemplateError("'headers' must be a mapping of name: value")
    headers = {str(k): str(v) for k, v in headers.items()}

    body = raw.get("body")
    if body is not None and not isinstance(body, (str, dict, list)):
        raise TemplateError("'body' must be text, a mapping, or a list")

    return RequestTemplate(method=method, url=url.strip(), headers=headers, body=body)


def render(
    template: RequestTemplate,
    row: Mapping[str, str],
    env: Mapping[str, str],
    mask_secrets: bool = False,
) -> RenderedRequest:
    """Fill in the template for one row.

    Raises TemplateError if a column is missing or empty, or an environment
    variable is not set. With mask_secrets=True, environment values are
    replaced with **** so the result is safe to print.
    """

    def fill(text: str, url_encode: bool = False) -> str:
        def column_value(match: re.Match) -> str:
            name = match.group(1)
            if name not in row:
                raise TemplateError(f"unknown column '{name}'")
            value = (row[name] or "").strip()
            if value == "":
                raise TemplateError(f"column '{name}' is empty")
            return quote(value, safe="") if url_encode else value

        def env_value(match: re.Match) -> str:
            name = match.group(1)
            if name not in env or env[name] == "":
                raise TemplateError(f"environment variable {name} is not set")
            return SECRET_MASK if mask_secrets else env[name]

        text = COLUMN_PATTERN.sub(column_value, text)
        return ENV_PATTERN.sub(env_value, text)

    def fill_any(value: Any) -> Any:
        if isinstance(value, str):
            return fill(value)
        if isinstance(value, dict):
            return {fill(str(k)): fill_any(v) for k, v in value.items()}
        if isinstance(value, list):
            return [fill_any(v) for v in value]
        return value

    url = fill(template.url, url_encode=True)
    headers = {fill(k): fill(v) for k, v in template.headers.items()}
    body = fill_any(template.body)

    if isinstance(body, (dict, list)) and not _has_header(headers, "Content-Type"):
        headers["Content-Type"] = "application/json"

    return RenderedRequest(method=template.method, url=url, headers=headers, body=body)


def _has_header(headers: Mapping[str, str], name: str) -> bool:
    return any(k.lower() == name.lower() for k in headers)


def _walk_strings(value: Any, visit) -> None:
    if isinstance(value, str):
        visit(value)
    elif isinstance(value, dict):
        for k, v in value.items():
            visit(str(k))
            _walk_strings(v, visit)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _walk_strings(v, visit)
