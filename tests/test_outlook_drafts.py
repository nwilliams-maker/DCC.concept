import pytest

from migration import outlook_drafts as od


class R:
    def __init__(self, status, body):
        self.status_code, self._b = status, body

    def json(self):
        return self._b


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k, v in {"MS_TENANT_ID": "t", "MS_CLIENT_ID": "c", "MS_CLIENT_SECRET": "s",
                 "OUTLOOK_DRAFTS_MAILBOX": "nick@terraboost.biz"}.items():
        monkeypatch.setenv(k, v)
    od._token_cache.clear()


def test_not_configured_lists_missing(monkeypatch):
    monkeypatch.delenv("MS_CLIENT_SECRET")
    assert not od.configured() and od.missing_settings() == ["MS_CLIENT_SECRET"]
    with pytest.raises(od.DraftError, match="MS_CLIENT_SECRET"):
        od.save_draft("a@b.com", "s", "b")


def test_creates_draft_in_mailbox_with_clickable_link():
    calls = []

    def post(url, **kw):
        calls.append((url, kw))
        if "oauth2" in url:
            return R(200, {"access_token": "tok", "expires_in": 3600})
        return R(201, {"id": "m1", "webLink": "https://outlook/x"})

    out = od.save_draft("ic@x.com", "Route Request | Ann-1",
                        "Hi Ann\nLink: https://portal/?route=Ann%201&v2=true", request_post=post)
    assert out == {"id": "m1", "web_link": "https://outlook/x"}
    url, kw = calls[1]
    assert url.endswith("/users/nick@terraboost.biz/messages")
    assert kw["headers"]["Authorization"] == "Bearer tok"
    msg = kw["json"]
    assert msg["toRecipients"] == [{"emailAddress": {"address": "ic@x.com"}}]
    assert '<a href="https://portal/?route=Ann%201&amp;v2=true">' in msg["body"]["content"]
    assert "Hi Ann<br>Link:" in msg["body"]["content"]
    # token is cached for the next draft
    od.save_draft("ic@x.com", "s", "b", request_post=post)
    assert sum("oauth2" in u for u, _ in calls) == 1


def test_no_ic_email_is_an_error():
    with pytest.raises(od.DraftError, match="no email"):
        od.save_draft("", "s", "b", request_post=lambda *a, **k: R(200, {}))


def test_graph_refusal_is_reported():
    def post(url, **kw):
        if "oauth2" in url:
            return R(200, {"access_token": "tok", "expires_in": 3600})
        return R(403, {"error": {"code": "ErrorAccessDenied", "message": "Access is denied."}})
    with pytest.raises(od.DraftError, match="403.*Access is denied"):
        od.save_draft("ic@x.com", "s", "b", request_post=post)


def test_html_escapes_text():
    assert od.text_to_html("<b>&") == "&lt;b&gt;&amp;"
