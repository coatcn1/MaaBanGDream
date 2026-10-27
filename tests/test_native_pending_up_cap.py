import pytest

from agent.realtime import native_engine


def action(kind, due, contact=0):
    return dict(kind=kind, due_s=due, lane=3, contact=contact, target_x=640., note_index=0, flick_direction=None)


@pytest.mark.parametrize('cap_publish_s', [.31, .06])
def test_real_session_empty_cap_flushes_pending_up_without_consuming_future_group(cap_publish_s):
    now = [0.]
    compiler = native_engine.touch_script_compiler()
    chunks, scripts, receipts = [], [], []
    def publish(chunk):
        chunks.append(chunk)
        materialized = [dict(row['action'], due_s=row['engine_due_s']) for row in chunk['actions']]
        reservations = [dict(row['action'], due_s=row['engine_due_s']) for row in chunk['future_down_reservations']]
        scripts.extend(compiler.compile(materialized, chunk['touch_config'],
            chunk['window_start_s'], chunk['final_chunk'], chunk['window_end_s'],
            reservations))
        receipts.extend(compiler.last_execution_receipts())
        return True
    session = native_engine.playback_session(publish=publish, clock=lambda: now[0])
    session.arm([action('tap', .49, -1), action('down', .65, 1), action('up', .9, 1)], {})
    session.start(.49)
    assert session.publish()
    assert not compiler.phase_boundary_safe()
    hint = compiler.pending_up_boundary_s()
    assert hint == pytest.approx(.54)
    now[0] = cap_publish_s
    assert session.publish(hint)
    assert chunks[-1]['actions'] == [] and not chunks[-1]['final_chunk']
    assert chunks[-1]['window_end_s'] == pytest.approx(hint)
    assert chunks[-1]['sequence'] == chunks[-2]['sequence'] + 1
    assert compiler.phase_boundary_safe()
    assert len(receipts) == 1
    assert session.apply_future_phase(15, compiler.contact_available_s()) == 'applied'
    now[0] = .4
    assert session.publish()
    assert chunks[-1]['window_start_s'] == pytest.approx(hint)
    assert chunks[-1]['actions'][0]['engine_due_s'] == pytest.approx(.665)
    now[0] = .71
    assert session.publish()
    assert len(receipts) == 3
    assert [r['action_token'] for r in receipts] == [1, 2, 3]


def test_real_compiler_no_hint_for_hold_and_pre_up_cap_stays_unsafe():
    compiler = native_engine.touch_script_compiler()
    compiler.compile([action('tap', .49, -1)], {}, 0., False, .5, [])
    hint = compiler.pending_up_boundary_s()
    compiler.compile([], {}, .5, False, hint - .000001, [])
    assert not compiler.phase_boundary_safe()
    compiler.compile([], {}, hint - .000001, False, hint, [])
    assert compiler.phase_boundary_safe()
    compiler.compile([action('down', .7)], {}, hint, False, .8, [])
    assert compiler.pending_up_boundary_s() is None


@pytest.mark.parametrize('base', [86.283329299, 93386.283329299, 9333486.283329299])
def test_hint_roundtrip_flushes_exact_integer_nanosecond_up(base):
    compiler = native_engine.touch_script_compiler()
    compiler.compile([action('tap', base, -1)], {}, base - .1, False, base + .01, [])
    hint = compiler.pending_up_boundary_s()
    compiler.compile([], {}, base + .01, False, hint, [])
    assert compiler.phase_boundary_safe()
    assert compiler.last_execution_receipts() == []


@pytest.mark.parametrize('cap', [float('nan'), float('inf'), -.1, 0.])
def test_invalid_cap_does_not_block_normal_feed(cap):
    chunks = []
    session = native_engine.playback_session(publish=lambda c: chunks.append(c) or True, clock=lambda: 0.)
    session.arm([action('tap', .1, -1), action('tap', 1., -1)], {})
    session.start(.1)
    assert session.publish(cap)
    assert chunks[0]['window_end_s'] == pytest.approx(.5)


def test_cap_does_not_split_same_due_group_or_expand_final_tail():
    now = [0.]
    chunks = []
    session = native_engine.playback_session(publish=lambda c: chunks.append(c) or True, clock=lambda: now[0])
    session.arm([action('tap', .2, -1), action('tap', .2, -1)], {})
    session.start(.2)
    assert session.publish(.21)
    assert len(chunks[0]['actions']) == 2
    assert not chunks[0]['final_chunk'] and chunks[0]['window_end_s'] == pytest.approx(.21)
    now[0] = .02
    assert session.publish()
    assert chunks[1]['actions'] == [] and chunks[1]['final_chunk']


def test_failed_capped_publish_does_not_commit_sequence_cursor_or_counts():
    chunks = []
    session = native_engine.playback_session(publish=lambda c: chunks.append(c) or False, clock=lambda: 0.)
    session.arm([action('tap', .2, -1)], {})
    session.start(.2)
    assert not session.publish(.21)
    report = session.report()
    assert report['sent'] == 0 and report['chunks'] == 0
    assert not session.publish()
    assert len(chunks) == 1


@pytest.mark.parametrize('fault', [None, 'receipts', 'expired', 'stop', 'clock'])
def test_real_owner_cap_then_original_consumer_applies(tmp_path, monkeypatch, fault):
    from test_native_clock_domains import fixture
    backend, _, now, _, commit, _, applied = fixture(tmp_path, monkeypatch)
    for host in (200.2254, 200.3254, 200.4254):
        commit(host)
    backend._refresh_observation_views()
    compiler = native_engine.touch_script_compiler()
    compiler.compile([action('tap', 200.49, -1)], {}, 200., False, 200.5, [])
    backend._compiler = compiler
    assert backend.request_future_phase(15, now[0], owner='feedback')
    caps = []
    def publish(cap=None):
        caps.append(cap)
        compiler.compile([], {}, 200.5, False, cap if cap is not None else 200.8, [])
        return True
    backend._session.publish = publish
    backend._session.poll = lambda: 'running'
    if fault == 'receipts':
        backend._phase_receipts.clear()
    elif fault == 'expired':
        now[0] += 2.01
    elif fault == 'stop':
        backend._phase_closed = True
    elif fault == 'clock':
        monkeypatch.setattr(backend, '_phase_observation_qualification', lambda now: 'hard-invalid')
    backend._drive_session()
    assert len(caps) == 1
    if fault is None:
        assert caps == [pytest.approx(200.54)]
        assert len(applied) == 1 and backend.phase_status_snapshot().state == 'applied'
    else:
        assert not applied
        if fault != 'receipts':
            assert caps == [None]


@pytest.mark.parametrize('component, missing', [('touch_script_compiler', 'pending_up_boundary_s'),
                                                ('playback_session', 'supports_window_end_cap')])
def test_trial_rejects_old_binary_capability_at_construction(tmp_path, monkeypatch, component, missing):
    from test_native_clock_domains import fixture
    factory = getattr(native_engine, component)
    class Legacy:
        def __init__(self, inner):
            self.inner = inner
        def __getattr__(self, name):
            if name == missing:
                raise AttributeError(name)
            return getattr(self.inner, name)
    monkeypatch.setattr(native_engine, component, lambda *a, **kw: Legacy(factory(*a, **kw)))
    with pytest.raises(RuntimeError, match='窗口裁限接口'):
        fixture(tmp_path, monkeypatch)


@pytest.mark.parametrize('capped', [False, True])
@pytest.mark.parametrize('high_water', [False, True])
def test_same_real_session_compiler_and_owner_baseline_vs_cap(tmp_path, monkeypatch, capped, high_water):
    from test_native_clock_domains import fixture
    backend, _, now, _, commit, _, _ = fixture(tmp_path, monkeypatch)
    for host in (200.2254, 200.3254, 200.4254):
        commit(host)
    backend._refresh_observation_views()
    compiler = native_engine.touch_script_compiler()
    chunks, receipts, scripts = [], [], []
    def publish(chunk):
        chunks.append(chunk)
        rows = [dict(row['action'], due_s=row['engine_due_s']) for row in chunk['actions']]
        reservations = [dict(row['action'], due_s=row['engine_due_s']) for row in chunk['future_down_reservations']]
        scripts.extend(compiler.compile(rows, chunk['touch_config'], chunk['window_start_s'],
            chunk['final_chunk'], chunk['window_end_s'], reservations))
        receipts.extend(compiler.last_execution_receipts())
        return True
    shift = .44 if high_water else 0.
    now[0] = 200. + shift
    session = native_engine.playback_session(publish=publish, clock=lambda: now[0])
    session.arm([action('tap', 200.49 + shift, -1), action('down', 200.65 + shift, 1), action('up', 201.5 + shift, 1)], {})
    session.start(200.49 + shift)
    assert session.publish()
    backend._session, backend._compiler = session, compiler
    now[0] = 200.5008
    assert backend.request_future_phase(15, now[0], owner='feedback')
    if not capped:
        monkeypatch.setattr(backend, '_pending_phase_publish_cap', lambda: None)
    backend._drive_session()
    if capped:
        assert chunks[1]['actions'] == []
        assert compiler.phase_boundary_safe()
        assert session.future_phase_offset_ms == 15
        assert backend.phase_status_snapshot().state == 'applied'
    else:
        if high_water:
            assert len(chunks) == 1
        else:
            assert len(chunks[1]['actions']) == 1
        assert not compiler.phase_boundary_safe()
        assert session.future_phase_offset_ms == 0
        assert backend.phase_status_snapshot().state == 'waiting-touch-boundary'
    while not chunks[-1]['final_chunk']:
        now[0] += .31
        session.publish()
    selected = [row for chunk in chunks for row in chunk['actions']]
    assert len(selected) == 3
    assert [row['engine_due_s'] for row in selected] == pytest.approx(
        [value + shift for value in ([200.49, 200.665, 201.515] if capped else [200.49, 200.65, 201.5])])
    assert [row['action_token'] for row in receipts] == [1, 2, 3]
    assert sum(line.startswith('d ') for line in scripts) == 2
    assert sum(line.startswith('u ') for line in scripts) == 2
    assert all(b['sequence'] == a['sequence'] + 1 for a, b in zip(chunks, chunks[1:]))

@pytest.mark.parametrize('cap',[None,float('nan'),float('inf'),-.1,.5,.57,.8])
def test_high_water_invalid_or_out_of_original_lookahead_cap_preserves_state(cap):
    now=[0.];chunks=[]
    session=native_engine.playback_session(publish=lambda c:chunks.append(c) or True,clock=lambda:now[0])
    session.arm([action('tap',.49,-1),action('down',.65,1),action('up',.9,1)],{})
    session.start(.49);assert session.publish()
    before=session.report();now[0]=.06
    assert not (session.publish() if cap is None else session.publish(cap))
    assert session.report()==before and len(chunks)==1
    now[0]=.31;assert session.publish()
    assert chunks[-1]['sequence']==2 and chunks[-1]['window_start_s']==pytest.approx(.5)


def test_high_water_cap_containing_same_due_group_is_not_early_published():
    now=[0.];chunks=[]
    session=native_engine.playback_session(publish=lambda c:chunks.append(c) or True,clock=lambda:now[0])
    session.arm([action('tap',.49,-1),action('tap',.53,-1),action('down',.53,1),action('up',.9,1)],{})
    session.start(.49);assert session.publish()
    now[0]=.06;assert not session.publish(.54)
    assert len(chunks)==1
    now[0]=.31;assert session.publish()
    assert len(chunks[-1]['actions'])==2


def test_high_water_cap_callback_failure_does_not_commit_counts_or_allow_retry():
    now=[0.];chunks=[]
    def publish(chunk):chunks.append(chunk);return len(chunks)==1
    session=native_engine.playback_session(publish=publish,clock=lambda:now[0])
    session.arm([action('tap',.49,-1),action('down',.65,1),action('up',.9,1)],{})
    session.start(.49);assert session.publish()
    now[0]=.06;assert not session.publish(.54)
    assert len(chunks)==2 and chunks[-1]['actions']==[]
    assert session.report()['chunks']==1 and session.report()['sent']==1
    assert not session.publish(.54) and len(chunks)==2


def test_high_water_cap_no_future_and_stopped_session_never_apply_phase():
    now=[0.];chunks=[]
    session=native_engine.playback_session(publish=lambda c:chunks.append(c) or True,clock=lambda:now[0])
    session.arm([action('tap',.49,-1)],{})
    session.start(.49);assert session.publish(.5)
    now[0]=.06;assert not session.publish(.54)
    assert session.apply_future_phase(15,.54)=='no-future-actions'
    assert chunks[-1]['final_chunk'] and len(chunks)==1
    assert session.apply_future_phase(15,.54)=='no-future-actions'
    assert session.cancel('test stop')
    assert not session.publish(.55)

@pytest.mark.parametrize('fault',['receipts','expired','stop','clock'])
def test_real_high_water_owner_cap_keeps_original_consumer_rejections(tmp_path,monkeypatch,fault):
    from test_native_clock_domains import fixture
    backend,_,now,_,commit,_,_=fixture(tmp_path,monkeypatch)
    for host in (200.2254,200.3254,200.4254):commit(host)
    backend._refresh_observation_views()
    compiler=native_engine.touch_script_compiler();chunks=[]
    def publish(chunk):
        chunks.append(chunk)
        rows=[dict(row['action'],due_s=row['engine_due_s']) for row in chunk['actions']]
        reservations=[dict(row['action'],due_s=row['engine_due_s']) for row in chunk['future_down_reservations']]
        compiler.compile(rows,chunk['touch_config'],chunk['window_start_s'],chunk['final_chunk'],chunk['window_end_s'],reservations)
        return True
    now[0]=200.44
    session=native_engine.playback_session(publish=publish,clock=lambda:now[0])
    session.arm([action('tap',200.93,-1),action('down',201.09,1),action('up',201.94,1)],{})
    session.start(200.93);assert session.publish()
    backend._session,backend._compiler=session,compiler
    now[0]=200.5008
    assert backend.request_future_phase(15,now[0],owner='feedback')
    if fault=='receipts':backend._phase_receipts.clear()
    elif fault=='expired':now[0]+=2.01
    elif fault=='stop':backend._phase_closed=True
    else:monkeypatch.setattr(backend,'_phase_observation_qualification',lambda now:'hard-invalid')
    backend._drive_session()
    assert session.future_phase_offset_ms==0
    assert backend.phase_status_snapshot().state!='applied'


def test_original_owner_loop_retries_pending_boundary_at_existing_twenty_ms_cadence():
    import queue
    from agent.realtime.native_play import NativeMinitouchBackend
    backend=NativeMinitouchBackend.__new__(NativeMinitouchBackend)
    stamp=[47.7287390402];attempts=[];response=queue.Queue()
    class Commands:
        def get(self,timeout):
            if len(attempts)==2:return ('shutdown',(),response)
            stamp[0]+=timeout
            raise queue.Empty
    backend._owner_commands=Commands();backend._publisher_poll_s=.020
    backend._session_state='running'
    backend._drive_session=lambda:attempts.append(stamp[0])
    backend._publisher_loop()
    assert attempts==pytest.approx([47.7487390402,47.7687390402])
    assert all(48.2300968522 < now+.5 and now < 47.7761420402 for now in attempts)
