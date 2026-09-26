"""A mistaken extraction must be corrected before it can become an artifact step."""
import json

import pytest

from bank_gpt.core import ModelClient
from bank_gpt.models import Parameter


@pytest.mark.asyncio
async def test_extract_retry_requires_declared_output_and_matching_label(monkeypatch):
    replies = [
        {"action": "extract", "index": 0, "output": "member_id_value"},
        {"action": "extract", "index": 1, "output": "balance"},
    ]
    sent = []

    class Response:
        def __init__(self, decision):
            self.decision = decision

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": json.dumps(self.decision)}}]}

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers, json):
            sent.append(json)
            return Response(replies.pop(0))

    monkeypatch.setattr("bank_gpt.core.httpx.AsyncClient", Client)
    decision = await ModelClient("http://model/v1", "demo", "test").decide(
        "Read the savings balance",
        {"controls": [{"index": 0, "row_label": "Member ID", "text": "[value]"},
                      {"index": 1, "row_label": "Savings balance", "text": "[value]"}]},
        [Parameter(name="member_id", type="string", description="Member ID")],
        [Parameter(name="balance", type="string", description="Displayed savings balance")],
        [], [])
    assert decision == {"action": "extract", "index": 1, "output": "balance"}
    assert len(sent) == 2
    assert "proposed extract is invalid" in sent[1]["messages"][-1]["content"]
