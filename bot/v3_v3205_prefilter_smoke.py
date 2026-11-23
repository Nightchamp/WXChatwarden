#!/usr/bin/env python3
"""v3.205: cheap rule layer in front of the follow-up and proactive judges.

Rules may only say "no". In shadow mode (default) the model is still called
and the rule is logged next to its answer; in active mode a matching rule
skips the model call; off disables the rules.
"""
import os
import time

from chatwarden import core, prefilter
from chatwarden import worker as worker_mod


def check_rules():
    assert prefilter.reaction_only("哈哈哈哈哈")
    assert prefilter.reaction_only("hhhh")
    assert prefilter.reaction_only("[捂脸][捂脸]")
    assert prefilter.reaction_only("😂😂！")
    assert prefilter.reaction_only("笑死")
    assert prefilter.reaction_only("   ")
    # Affirmatives can answer a question the bot asked: never a rule hit.
    for text in ("好", "可以", "行", "ok", "对", "要", "嗯好的那你查一下"):
        assert not prefilter.reaction_only(text), text
    assert not prefilter.reaction_only("哈哈那你再说一遍")
    assert not prefilter.reaction_only("为什么？")
    print("V3205_REACTION_RULE=PASS")

    base = {"local_type": 1, "sender_username": "wxid_demo_alice"}
    assert prefilter.followup_rule(dict(base, text="哈哈哈哈")) == "reaction-only"
    assert prefilter.followup_rule(
        dict(base, text="@Bob 你看这个", mentioned_wxids=["wxid_demo_bob"]),
        bot_wxid="wxid_demo_bot",
    ) == "mentions-other"
    # Mentioning the bot (even alongside others) is never filtered.
    assert prefilter.followup_rule(
        dict(base, text="@Bob @bot 你们看", mentioned_wxids=["wxid_demo_bob", "wxid_demo_bot"]),
        bot_wxid="wxid_demo_bot",
    ) is None
    assert prefilter.followup_rule(dict(base, text="那后来呢")) is None
    print("V3205_FOLLOWUP_RULE=PASS")

    reactions = [{"text": "哈哈哈"}, {"text": "笑死"}, {"text": "[旺柴]"}]
    assert prefilter.proactive_rule(reactions) == "reactions-only"
    assert prefilter.proactive_rule(reactions, has_visual=True) is None
    assert prefilter.proactive_rule(reactions + [{"text": "周末去哪玩"}]) is None
    assert prefilter.proactive_rule([]) is None
    print("V3205_PROACTIVE_RULE=PASS")

    for value, expected in (("", "shadow"), ("active", "active"), ("OFF", "off"), ("bogus", "shadow")):
        os.environ["CW_PREFILTER"] = value
        assert prefilter.mode() == expected, (value, prefilter.mode())
    os.environ.pop("CW_PREFILTER", None)
    assert prefilter.mode() == "shadow"
    print("V3205_MODE=PASS")


def make_worker():
    group_cfg = {
        "name": "synthetic-prefilter-test",
        "session_key": "synthetic-session",
        "personality": "",
    }
    return worker_mod.GroupWorker(
        {"group_cfg": group_cfg, "chat_name": group_cfg["name"]},
        None,
        {},
        "wxid_demo_bot",
        "wxid_demo_owner",
    )


def check_followup(logs):
    worker = make_worker()
    sender = "wxid_demo_alice"
    calls = {"gate": 0, "answer": 0}
    original_gate = core.call_followup_gate

    def fake_gate(*args, **kwargs):
        calls["gate"] += 1
        return True

    def fake_answer(*args, **kwargs):
        calls["answer"] += 1

    def event(local_id, text, mentions=None):
        return {
            "local_id": local_id, "server_id": local_id, "timestamp": 0,
            "local_type": 1, "sender_username": sender,
            "sender_display": "Alice", "text": text, "msgsource": "",
            "mentioned_wxids": list(mentions or []),
        }

    def run(ev, want_gate, want_answer):
        calls.update(gate=0, answer=0)
        worker.followups[sender] = {"last_success": time.monotonic()}
        worker._handle_text(ev)
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline and calls["answer"] < want_answer:
            time.sleep(0.01)
        time.sleep(0.1)
        assert calls == {"gate": want_gate, "answer": want_answer}, (ev["text"], calls)

    try:
        core.call_followup_gate = fake_gate
        worker._answer = fake_answer

        os.environ["CW_PREFILTER"] = "shadow"
        run(event(200, "哈哈哈哈哈哈"), 1, 1)
        assert any("[v3.205-prefilter] kind=followup" in line and "agree=no" in line
                   and "mode=shadow" in line for line in logs), logs[-5:]
        print("V3205_FOLLOWUP_SHADOW_KEEPS_BEHAVIOUR=PASS")

        os.environ["CW_PREFILTER"] = "active"
        run(event(201, "哈哈哈哈哈哈"), 0, 0)
        run(event(202, "@Bob 你看这个", ["wxid_demo_bob"]), 0, 0)
        assert any("model_call=skipped" in line and "reason=mentions-other" in line for line in logs)
        # A real follow-up still reaches the model.
        run(event(203, "那后来呢"), 1, 1)
        print("V3205_FOLLOWUP_ACTIVE_SKIPS_MODEL=PASS")

        os.environ["CW_PREFILTER"] = "off"
        before = len([l for l in logs if "v3.205-prefilter" in l])
        run(event(204, "哈哈哈哈哈哈"), 1, 1)
        assert len([l for l in logs if "v3.205-prefilter" in l]) == before
        print("V3205_FOLLOWUP_OFF=PASS")
    finally:
        os.environ.pop("CW_PREFILTER", None)
        core.call_followup_gate = original_gate
        worker.stop()
        worker.thread.join(timeout=2.0)


def check_proactive(logs):
    worker = make_worker()
    calls = {"judge": 0}

    def fake_judge(prompt, image_path=None):
        calls["judge"] += 1
        return ("SILENT", "", "nothing to add", {}, 0.1, 0.0, "fixture", False)

    worker._v39_generate_shadow_decision = fake_judge
    worker._v392_send_gate = lambda now: (True, "", {})
    worker.proactive_shadow_enabled = True

    def fire(texts):
        now = time.monotonic()
        with worker._v39_shadow_lock:
            worker._v39_burst_judged = False
            worker._v39_shadow_inflight = False
            worker._v39_recent_humans = [
                {"local_id": 300 + i, "sender_username": f"wxid_demo_{i % 3}",
                 "sender_display": f"M{i}", "text": t, "seen_mono": now}
                for i, t in enumerate(texts)
            ]
            generation = worker._v39_shadow_generation
        worker._v39_shadow_timer_fire(generation)

    burst = ["哈哈哈", "笑死", "[旺柴]", "hhhh", "哈哈哈哈", "草"]
    try:
        os.environ["CW_PREFILTER"] = "shadow"
        fire(burst)
        assert calls["judge"] == 1, calls
        assert any("kind=proactive" in l and "mode=shadow" in l and "agree=yes" in l for l in logs)
        print("V3205_PROACTIVE_SHADOW_KEEPS_BEHAVIOUR=PASS")

        os.environ["CW_PREFILTER"] = "active"
        calls["judge"] = 0
        fire(burst)
        assert calls["judge"] == 0, calls
        assert worker._v39_burst_judged is True
        assert worker._v39_last_judgment_outcome == "SILENT"
        assert worker._v39_shadow_inflight is False
        fire(burst[:-1] + ["这周末有人去爬山吗"])
        assert calls["judge"] == 1, calls
        print("V3205_PROACTIVE_ACTIVE_SKIPS_MODEL=PASS")
    finally:
        os.environ.pop("CW_PREFILTER", None)
        worker.stop()
        worker.thread.join(timeout=2.0)


def main():
    check_rules()
    logs = []
    original_log = core.log

    def capture(message, *args, **kwargs):
        logs.append(str(message))

    core.log = capture
    try:
        check_followup(logs)
        check_proactive(logs)
    finally:
        core.log = original_log
    print("V3205_PREFILTER_SMOKE=PASS")


if __name__ == "__main__":
    main()
