#!/usr/bin/env python3
"""v3.177 muted groups are not ingested; unmute rebases without replay. Fakes only."""
import os

os.environ['CW_DECISION_SHADOW'] = '0'

from chatwarden import core, runtime as rt


class FakeDb:
    def __init__(self):
        self.latest = 100
        self.max_calls = 0

    def max_local_id(self, resolved, snapshot):
        self.max_calls += 1
        return self.latest


class FakeState:
    def __init__(self):
        self.checkpoints = []

    def set_checkpoint(self, name, chat, table, cursor):
        self.checkpoints.append((name, cursor))


def main():
    muted = {'value': True}
    saved = (core.group_muted, core.log)
    core.group_muted = lambda name: muted['value'] if name == 'A群' else False
    core.log = lambda *a, **k: None
    try:
        cls = next(v for v in vars(rt).values()
                   if isinstance(v, type) and hasattr(v, '_v3177_muted_gate'))
        runtime = cls.__new__(cls)
        runtime.db = FakeDb()
        runtime.state = FakeState()
        entry = {'resolved': {'chat_username': '1@chatroom', 'table_name': 'Msg_x'},
                 'cursor': 40, 'worker': None}

        # Muted: skipped every poll, cursor frozen, no DB work at all.
        for _ in range(3):
            assert runtime._v3177_muted_gate('A群', entry, {}) is True
        assert entry['muted'] is True and entry['cursor'] == 40
        assert runtime.db.max_calls == 0 and runtime.state.checkpoints == []
        print('V3177_MUTED_GROUP_SKIPPED_WITHOUT_DB_READ=PASS')

        # Unmute: one rebase to the latest local_id (gap 41..100 never replayed).
        muted['value'] = False
        assert runtime._v3177_muted_gate('A群', entry, {}) is True
        assert entry['cursor'] == 100 and entry['muted'] is False
        assert runtime.state.checkpoints == [('A群', 100)]
        assert runtime._v3177_muted_gate('A群', entry, {}) is False
        other = {'resolved': entry['resolved'], 'cursor': 7, 'worker': None}
        assert runtime._v3177_muted_gate('B群', other, {}) is False and 'muted' not in other
        print('V3177_UNMUTE_REBASES_TO_LATEST_NO_REPLAY=PASS')

        # Cold start of a muted group baselines at latest without gap backfill.
        calls = []

        def new_entry(cfg, snapshot, reason, **kw):
            calls.append((reason, kw))
            return {'resolved': {}, 'cursor': 100, 'worker': None}
        runtime._new_entry = new_entry
        runtime._v395_backfill_group_history = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError('muted group was back-filled'))
        muted['value'] = True
        out = runtime._v3139_startup_entry({'name': 'A群'}, {})
        assert out['muted'] is True and calls == [('startup-muted-baseline', {})], calls
        print('V3177_STARTUP_MUTED_BASELINE_NO_BACKFILL=PASS')
    finally:
        core.group_muted, core.log = saved

    # New groups are registered with the master switch off (see v3127 smoke too).
    import inspect
    assert '"muted": True' in inspect.getsource(core.auto_register_group_config)
    print('V3177_AUTO_REGISTERED_GROUP_DEFAULT_MUTED=PASS')


if __name__ == '__main__':
    main()
