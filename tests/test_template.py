import pytest

from rowcall.template import TemplateError, parse_template, render


def make(**raw):
    raw.setdefault("url", "https://api.example.com/items")
    return parse_template(raw)


def test_fills_columns_and_env():
    t = make(
        method="post",
        url="https://api.example.com/users/{{id}}",
        headers={"Authorization": "Bearer ${TOKEN}"},
        body={"email": "{{email}}"},
    )
    r = render(t, {"id": "42", "email": "a@b.com"}, {"TOKEN": "secret"})
    assert r.method == "POST"
    assert r.url == "https://api.example.com/users/42"
    assert r.headers["Authorization"] == "Bearer secret"
    assert r.headers["Content-Type"] == "application/json"
    assert r.body == {"email": "a@b.com"}


def test_masks_secrets_for_preview():
    t = make(headers={"Authorization": "Bearer ${TOKEN}"})
    r = render(t, {}, {"TOKEN": "secret"}, mask_secrets=True)
    assert r.headers["Authorization"] == "Bearer ****"


def test_url_values_are_encoded():
    t = make(url="https://api.example.com/users/{{email}}")
    r = render(t, {"email": "a b/c@d.com"}, {})
    assert r.url == "https://api.example.com/users/a%20b%2Fc%40d.com"


def test_empty_value_is_an_error():
    # An empty id could turn DELETE /users/{{id}} into DELETE /users/
    t = make(method="DELETE", url="https://api.example.com/users/{{id}}")
    with pytest.raises(TemplateError, match="column 'id' is empty"):
        render(t, {"id": "  "}, {})


def test_unknown_column_is_an_error():
    t = make(body={"x": "{{nope}}"})
    with pytest.raises(TemplateError, match="unknown column 'nope'"):
        render(t, {"id": "1"}, {})


def test_missing_env_is_an_error():
    t = make(headers={"X-Key": "${KEY}"})
    with pytest.raises(TemplateError, match="KEY is not set"):
        render(t, {}, {})


def test_keeps_explicit_content_type():
    t = make(headers={"content-type": "application/vnd.api+json"}, body={"a": 1})
    r = render(t, {}, {})
    assert r.headers == {"content-type": "application/vnd.api+json"}


def test_text_body_is_left_as_text():
    t = make(body="name={{name}}")
    r = render(t, {"name": "x"}, {})
    assert r.body == "name=x"
    assert "Content-Type" not in r.headers


def test_lists_columns_and_env_vars():
    t = make(
        url="https://api.example.com/{{a}}",
        headers={"Auth": "${T}"},
        body={"items": [{"v": "{{b}}"}]},
    )
    assert t.columns() == {"a", "b"}
    assert t.env_vars() == {"T"}


@pytest.mark.parametrize(
    "raw, message",
    [
        ("just text", "must be a YAML mapping"),
        ({"method": "GET"}, "needs a 'url'"),
        ({"url": "ftp://x"}, "must start with http"),
        ({"url": "https://x", "method": "FETCH"}, "unsupported method"),
        ({"url": "https://x", "headrs": {}}, "unknown template key"),
        ({"url": "https://x", "body": 5}, "'body' must be"),
    ],
)
def test_rejects_bad_templates(raw, message):
    with pytest.raises(TemplateError, match=message):
        parse_template(raw)
