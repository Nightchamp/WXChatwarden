#!/usr/bin/env python3
from __future__ import annotations

from queue import Queue

from chatwarden.runtime import Runtime


PEER = "wxid_fixture_private_peer"


class FakeWorker:
    def __init__(self):
        self.q = Queue()
        self.events = []

    def put(self, event):
        self.events.append(dict(event))
        self.q.put(dict(event))


class FakeDb:
    direct_query_enabled = False

    def __init__(self):
        self.poll = 0

    def v310_private_sessions(self, snapshot):
        return {
            PEER: {
                "username": PEER,
                "display_name": "Fixture",
                "unread": 1,
                "last_timestamp": 200,
                "last_msg_type": 1,
                "last_msg_sender": "",
                "signature": (200, 1, "", "sig-new"),
            }
        }

    def v310_resolve_private(self, username, snapshot):
        assert username == PEER
        return {
            "chat_name": "Fixture",
            "chat_username": PEER,
            "send_safe": True,
            "send_safe_reason": "ok",
        }

    def v310_private_events_since(self, resolved, previous_timestamp, snapshot):
        self.poll += 1
        if self.poll == 1:
            return []
        return [
            {
                "local_id": 81,
                "server_id": 123,
                "local_type": 1,
                "timestamp": 200,
                "sender_username": PEER,
                "text": "测试",
                "raw_content": "测试",
                "is_image": False,
            }
        ]

    def v310_private_latest_at(self, resolved, timestamp, snapshot):
        raise AssertionError("same-second path is not expected in this fixture")


class AlwaysEmptyDb(FakeDb):
    def v310_private_events_since(self, resolved, previous_timestamp, snapshot):
        self.poll += 1
        return []


class OutgoingOnlyDb(FakeDb):
    def v310_private_events_since(self, resolved, previous_timestamp, snapshot):
        self.poll += 1
        return [
            {
                "local_id": 82,
                "server_id": 124,
                "local_type": 1,
                "timestamp": 200,
                "sender_username": "wxid_bot_fixture",
                "text": "bot reply",
                "raw_content": "bot reply",
                "is_image": False,
            }
        ]


class StaleThenIncomingDb(FakeDb):
    def v310_private_events_since(self, resolved, previous_timestamp, snapshot):
        self.poll += 1
        older = {
            "local_id": 80,
            "server_id": 122,
            "local_type": 1,
            "timestamp": 150,
            "sender_username": "wxid_bot_fixture",
            "text": "older bot row",
            "raw_content": "older bot row",
            "is_image": False,
        }
        if self.poll == 1:
            return [older]
        return [
            older,
            {
                "local_id": 81,
                "server_id": 123,
                "local_type": 1,
                "timestamp": 200,
                "sender_username": PEER,
                "text": "测试",
                "raw_content": "测试",
                "is_image": False,
            },
        ]


def build_runtime():
    runtime = Runtime.__new__(Runtime)
    runtime.db = FakeDb()
    runtime._v310_private_enabled = True
    runtime._v310_private_poll_seconds = 0.0
    runtime._v310_private_last_poll = 0.0
    runtime._v310_private_state = {
        PEER: {
            "signature": (100, 1, "", "sig-old"),
            "last_timestamp": 100,
        }
    }
    runtime._v310_private_workers = {}
    runtime._v310_private_empty_edge_grace_seconds = 30.0
    runtime._v310_private_empty_edges = {}
    worker = FakeWorker()
    runtime._v310_worker_for = lambda resolved: worker
    return runtime, worker


def build_runtime_with_db(db):
    runtime, worker = build_runtime()
    runtime.db = db
    return runtime, worker


def main():
    runtime, worker = build_runtime()

    first = runtime._v310_poll_private(snapshot={})
    assert first == 0
    assert worker.events == []
    assert runtime._v310_private_state[PEER]["signature"] == (
        100,
        1,
        "",
        "sig-old",
    ), "empty message snapshot must not commit the SessionTable edge"

    second = runtime._v310_poll_private(snapshot={})
    assert second == 1
    assert [event["local_id"] for event in worker.events] == [81]
    assert runtime._v310_private_state[PEER]["signature"] == (
        200,
        1,
        "",
        "sig-new",
    )

    outgoing_runtime, outgoing_worker = build_runtime_with_db(
        OutgoingOnlyDb()
    )
    assert outgoing_runtime._v310_poll_private(snapshot={}) == 0
    assert outgoing_worker.events == []
    assert outgoing_runtime._v310_private_state[PEER]["signature"] == (
        200,
        1,
        "",
        "sig-new",
    ), "visible non-incoming rows must still commit the SessionTable edge"
    assert outgoing_runtime._v310_private_empty_edges == {}

    stale_runtime, stale_worker = build_runtime_with_db(
        StaleThenIncomingDb()
    )
    assert stale_runtime._v310_poll_private(snapshot={}) == 0
    assert stale_worker.events == []
    assert stale_runtime._v310_private_state[PEER]["signature"] == (
        100,
        1,
        "",
        "sig-old",
    ), "rows older than SessionTable.last_timestamp must not commit the edge"
    assert stale_runtime._v310_poll_private(snapshot={}) == 1
    assert [event["local_id"] for event in stale_worker.events] == [81]

    expired_runtime, expired_worker = build_runtime_with_db(
        AlwaysEmptyDb()
    )
    expired_runtime._v310_private_empty_edge_grace_seconds = 30.0
    expired_runtime._v310_private_empty_edges[PEER] = {
        "signature": (200, 1, "", "sig-new"),
        "first_seen": -1000.0,
        "attempts": 2,
    }
    assert expired_runtime._v310_poll_private(snapshot={}) == 0
    assert expired_worker.events == []
    assert expired_runtime._v310_private_state[PEER]["signature"] == (
        200,
        1,
        "",
        "sig-new",
    ), "stale empty edges must baseline after the bounded grace window"
    assert expired_runtime._v310_private_empty_edges == {}

    print("V359_PRIVATE_SESSION_MESSAGE_RACE=PASS")


if __name__ == "__main__":
    main()
