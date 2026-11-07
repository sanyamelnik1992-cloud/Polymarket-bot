from unittest import mock

from pmbot.bot import alerts


def test_chat_ids_split():
    assert alerts.chat_ids("1, 2,,3") == ["1", "2", "3"]
    assert alerts.chat_ids("") == [] and alerts.chat_ids(None) == []


def test_send_html_goes_to_every_chat():
    sent = []

    def fake_post(url, json=None, **kw):
        sent.append(json["chat_id"])
        return mock.Mock(status_code=200)

    with mock.patch.object(alerts.requests, "post", fake_post):
        assert alerts.send_html("t", "10,20", "hi")
    assert sent == ["10", "20"]
