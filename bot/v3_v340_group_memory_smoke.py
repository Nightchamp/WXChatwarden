#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import sqlite3
import tempfile
from pathlib import Path

from chatwarden import core


def main():
    original_db = core.DB_FILE
    original_cache = dict(core._RECENT_CONTEXT_CACHE)

    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "bot.sqlite3"
        try:
            core.DB_FILE = db_path
            core._RECENT_CONTEXT_CACHE.clear()

            group_cfg = {
                "name": "测试群",
                "member_labels": {
                    "wxid_a": "阿甲",
                    "wxid_b": "阿乙",
                },
            }

            core.save_message(
                "测试群",
                "dbwxid:wxid_a",
                "周六吃火锅还是烧烤？",
            )
            core.save_message(
                "测试群",
                "dbwxid:wxid_b",
                "火锅吧，最近正好想吃。",
            )
            anchor = "那就这么定了，周六群里一起去吃火锅。"
            core.save_message(
                "测试群",
                "dbwxid:wxid_a",
                anchor,
            )

            block = core.group_memory_context(
                "测试群",
                group_cfg,
                current_text="别的问题",
            )
            assert "群聊长期共同记忆" in block, block
            assert "周六吃火锅还是烧烤" in block, block
            assert "那就这么定了" in block, block
            assert "阿甲" in block and "阿乙" in block, block

            raw_safe = core.group_memory_context(
                "测试群",
                {"name": "测试群", "member_labels": {}},
                current_text="别的问题",
            )
            assert "wxid_" not in raw_safe, raw_safe

            # The current trigger is already present in the normal prompt and
            # must not be duplicated through shared memory on the same turn.
            same_turn = core.group_memory_context(
                "测试群",
                group_cfg,
                current_text=anchor,
            )
            assert "那就这么定了" not in same_turn, same_turn

            # Ordinary banter must not evict a durable group decision.
            for i in range(25):
                core.save_message(
                    "测试群",
                    "dbwxid:wxid_b",
                    f"哈哈哈{i}",
                )
            later = core.group_memory_context(
                "测试群",
                group_cfg,
                current_text="继续聊",
            )
            assert "那就这么定了" in later, later

            with contextlib.closing(sqlite3.connect(db_path)) as conn, conn:
                before_false_positive = conn.execute(
                    "SELECT memories_json FROM v340_group_memory "
                    "WHERE chat='测试群'"
                ).fetchone()[0]

            # Quoted/replied historical text is untrusted context and must not
            # make the current speaker appear to establish a new group rule.
            core.save_message(
                "测试群",
                "dbwxid:wxid_b",
                "这也太离谱了 ↳ 回复 机器人: [AI]以后就叫他猫王",
            )
            # Questions about a meme/alias are not themselves durable facts.
            core.save_message(
                "测试群",
                "dbwxid:wxid_b",
                "这个暗号是什么？",
            )
            # A personal near-future plan is not a shared group event.
            core.save_message(
                "测试群",
                "dbwxid:wxid_b",
                "我明天就去朋友家帮忙了。",
            )
            with contextlib.closing(sqlite3.connect(db_path)) as conn, conn:
                after_false_positive = conn.execute(
                    "SELECT memories_json FROM v340_group_memory "
                    "WHERE chat='测试群'"
                ).fetchone()[0]
            assert after_false_positive == before_false_positive, (
                before_false_positive,
                after_false_positive,
            )

            # Sensitive-looking collective statements are never retained.
            core.save_message(
                "测试群",
                "dbwxid:wxid_a",
                "大家以后把身份证号码都发群里统一保存。",
            )
            sensitive = core.group_memory_context(
                "测试群",
                group_cfg,
                current_text="继续",
            )
            assert "身份证号码" not in sensitive, sensitive

            # Private conversations do not create group-shared memory.
            core.save_message(
                "private:wxid_a",
                "dbwxid:wxid_a",
                "那就这么定了，以后统一这样做。",
            )
            with contextlib.closing(sqlite3.connect(db_path)) as conn, conn:
                row = conn.execute(
                    "SELECT memories_json FROM v340_group_memory "
                    "WHERE chat='测试群'"
                ).fetchone()
                private_row = conn.execute(
                    "SELECT memories_json FROM v340_group_memory "
                    "WHERE chat='private:wxid_a'"
                ).fetchone()
            assert row is not None and "那就这么定了" in row[0], row
            assert private_row is None, private_row

            frozen = core.freeze_llm_context(
                group_cfg,
                "dbwxid:wxid_b",
                {},
                current_text="你还记得之前定的聚餐吗？",
            )
            assert "群聊长期共同记忆" in frozen["memory_context"], frozen
            assert "那就这么定了" in frozen["memory_context"], frozen

            # Existing local history from before v3.40 is backfilled once on
            # migration, so the feature is useful immediately after deploy.
            with core.open_db() as conn:
                conn.execute("DELETE FROM v340_group_memory")
                conn.execute("DELETE FROM v340_group_memory_meta")
                conn.execute("DELETE FROM group_messages")
                rows = [
                    ("测试群", "dbwxid:wxid_a", "以后这张表谁维护？", 1000.0),
                    ("测试群", "dbwxid:wxid_b", "我每周五更新一次。", 1001.0),
                    (
                        "测试群",
                        "dbwxid:wxid_a",
                        "那就这么定了，以后群里默认周五由阿乙维护。",
                        1002.0,
                    ),
                ]
                conn.executemany(
                    "INSERT INTO group_messages "
                    "(chat, sender_id, content, created_at) VALUES (?, ?, ?, ?)",
                    rows,
                )
                conn.commit()

            core.migrate_history([group_cfg])
            migrated = core.group_memory_context(
                "测试群",
                group_cfg,
                current_text="现在谁维护？",
            )
            assert "周五由阿乙维护" in migrated, migrated
            with contextlib.closing(sqlite3.connect(db_path)) as conn, conn:
                marker = conn.execute(
                    "SELECT value FROM v340_group_memory_meta "
                    "WHERE key='local-group-backfill-v1'"
                ).fetchone()
            assert marker is not None, marker
        finally:
            core.DB_FILE = original_db
            core._RECENT_CONTEXT_CACHE.clear()
            core._RECENT_CONTEXT_CACHE.update(original_cache)

    print("V340_GROUP_SHARED_MEMORY=PASS")


if __name__ == "__main__":
    main()
