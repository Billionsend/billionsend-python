"""Broadcast groups, paged topic lists, ``audience_id`` and the optional body of an automation stop.

Each case runs through the sync and the async client (the ``api`` fixture), and every
request and queued answer is held to the published snapshot by the contract mock.
"""

from __future__ import annotations

from billionsend.types import CreateBroadcastGroupRequest, UpdateBroadcastGroupRequest

from .api import Api
from .contract import ContractMock

ID = "6a1f1d38-3c55-4a4e-9a57-0f2b3c4d5e6f"
ID2 = "7b2f2e49-4d66-4b5f-8b68-1a3c4d5e6f70"


def test_a_group_is_created_and_sent_in_one_request(api: Api, mock: ContractMock) -> None:
    body: CreateBroadcastGroupRequest = {
        "name": "November",
        "domain_ids": [ID, ID2],
        "from_local_part": "news",
        "subject": "November",
        "html": "<p>News</p>",
        "send": True,
    }
    api.call("broadcast_groups.create", body)
    assert (mock.last.request.method, mock.last.request.url.path) == ("POST", "/broadcast-groups")
    assert mock.last.body == body


def test_a_group_is_read_changed_sent_cancelled_and_removed(api: Api, mock: ContractMock) -> None:
    api.call("broadcast_groups.get", ID)
    assert (mock.last.request.method, mock.last.path_params) == ("GET", {"id": ID})
    changes: UpdateBroadcastGroupRequest = {"subject": "Changed", "topic_id": None}
    api.call("broadcast_groups.update", ID, changes)
    assert (mock.last.request.method, mock.last.body) == ("PATCH", changes)
    api.call("broadcast_groups.send", ID, {"scheduled_at": "in 1 hour"})
    assert mock.last.body == {"scheduled_at": "in 1 hour"}
    api.call("broadcast_groups.send", ID)
    assert mock.last.request.content == b""
    api.call("broadcast_groups.cancel", ID)
    assert (mock.last.request.method, mock.last.request.content) == ("POST", b"")
    api.call("broadcast_groups.remove", ID)
    assert (mock.last.request.method, mock.last.path_params) == ("DELETE", {"id": ID})


def test_the_group_list_is_paged_with_a_cursor(api: Api, mock: ContractMock) -> None:
    api.call("broadcast_groups.list", limit=5, after=ID)
    assert mock.last.query == {"limit": ["5"], "after": [ID]}


def test_topics_are_cursor_lists(api: Api, mock: ContractMock) -> None:
    api.call("topics.list", limit=3, before=ID)
    assert mock.last.query == {"limit": ["3"], "before": [ID]}
    api.call("contacts.topics.list", ID, limit=2, after=ID2)
    assert (mock.last.path_params, mock.last.query) == (
        {"id": ID},
        {"limit": ["2"], "after": [ID2]},
    )


def test_a_broadcast_takes_audience_id_as_the_older_name_of_segment_id(
    api: Api, mock: ContractMock
) -> None:
    api.call(
        "broadcasts.create",
        {
            "from": "news@example.com",
            "subject": "November",
            "html": "<p>News</p>",
            "audience_id": ID,
        },
    )
    assert mock.last.body["audience_id"] == ID
    api.call("broadcasts.update", ID, {"audience_id": ID2})
    assert mock.last.body == {"audience_id": ID2}


def test_stopping_an_automation_can_cancel_its_runs(api: Api, mock: ContractMock) -> None:
    api.call("automations.stop", ID)
    assert mock.last.request.content == b""
    api.call("automations.stop", ID, {"cancel_runs": True})
    assert mock.last.body == {"cancel_runs": True}
