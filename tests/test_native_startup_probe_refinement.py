import json
from types import SimpleNamespace

import pytest

from agent.realtime import native_play


def probe_fixture(tmp_path, monkeypatch, samples, *, trial=True, stop_after=None, stop_wait=False, identity=None):
    clock = [200.]
    stopped = [False]
    monkeypatch.setattr(native_play.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(native_play, 'probe_native_boot_identity', lambda *_args, **_kwargs:
        {'boot_id': 'unit'} if identity is None else identity)
    monkeypatch.setattr(native_play, 'NativeRunDeviceClaim', lambda *_args, **_kwargs:
        SimpleNamespace(acquire=lambda: None, current=lambda: True, stopping=lambda: None))
    chart = tmp_path / 'chart.json'
    chart.write_text('[{"type":"BPM","beat":0,"bpm":120},{"type":"Single","beat":2,"lane":2}]', encoding='utf-8')

    class Device:
        _reset_generation = 1
        connected = False
        max_x = max_y = 32767
        max_contacts = 10
        def __init__(self):
            self.sent, self.rows = [], []
        def start(self, **_kwargs):
            self.connected = True
        def stop(self):
            self.connected = False
            return True
        def request_reset(self):
            return True
        def publish(self, payload):
            assert payload == 'c\nw 0\nc\n'
            assert len(self.sent) < len(samples), 'probe次数必须有固定上限'
            self.sent.append(clock[0])
            spec = samples[len(self.sent) - 1]
            if spec.get('generation-change'):
                self._reset_generation += 1
            rtt, offset = spec.get('rtt', .004), spec.get('offset', 100.)
            device = clock[0] + rtt / 2 - offset
            for index, command in enumerate(('c', 'w 0', 'c')):
                if spec.get('missing') == index:
                    continue
                start = device + (index - 1) * .00001
                rx = clock[0] + rtt + (index - 1) * .00001
                if index == 1 and spec.get('gap'):
                    self.rows.append((rx - .000005, 'unexpected gap'))
                if index == 1 and spec.get('wrong-command'):
                    command = 'w 1'
                self.rows.append((rx, 'jlog ' + json.dumps(dict(cmd=command, st=start * 1000,
                    et=(start + .000001) * 1000, c=.001))))
            if stop_after == len(self.sent):
                stopped[0] = True
        def log_records_since(self, cursor):
            ready = [(line, stamp) for stamp, line in self.rows if stamp <= clock[0]]
            return len(ready), ready[cursor:]

    device = Device()
    backend = native_play.NativeMinitouchBackend(chart, adb_path='unused', serial='unused',
        device=device, require_probe=True, life_feedback_enabled=True,
        observation_clock_trial_enabled=trial, clock=lambda: clock[0])
    monkeypatch.setattr(backend, '_start_native_clock_observer', lambda: None)
    cleanup = []
    monkeypatch.setattr(backend, '_cancel_device_start',
        lambda _timeout: (cleanup.append('cancel'), backend._device_start_cancel.set()))
    monkeypatch.setattr(backend, '_emergency_stop_device_with_budget', lambda _timeout: cleanup.append('stop'))

    class PumpEvent:
        value = False
        def set(self): self.value = True
        def clear(self): self.value = False
        def is_set(self): return self.value
        def wait(self, timeout):
            if stop_wait and len(device.sent) >= 2:
                stopped[0] = True
                return False
            end = clock[0] + timeout
            while not self.value and clock[0] < end:
                future = [stamp for stamp, _line in device.rows if stamp > clock[0]]
                clock[0] = min(end, min(future, default=end))
                backend._observe_new_logs()
            return self.value
    backend._probe_complete = PumpEvent()
    backend._state = 'arming'
    backend._device_thread = SimpleNamespace(is_alive=lambda: False, join=lambda **_kwargs: None)
    backend._start_device()
    return backend, device, clock, stopped, cleanup


def test_initial_15569us_probe_can_refine_before_anchor_freeze(tmp_path, monkeypatch):
    backend, device, clock, _, _ = probe_fixture(tmp_path, monkeypatch,
        [dict(rtt=.0031138), dict(rtt=.001)])
    assert backend.wait_until_ready(.5)
    assert len(device.sent) == 2
    assert backend._clock_uncertainty_ms < 1
    assert abs(backend._device_clock_offset_s - 100.) < .00001
    assert backend._first_action_anchor_s is None and not backend._playback_observation_started


@pytest.mark.parametrize('fault', ['conflict', 'incomplete'])
def test_incompatible_or_unclosed_sent_probe_cannot_be_selected_or_started(tmp_path, monkeypatch, fault):
    second = dict(rtt=.001, offset=99.) if fault == 'conflict' else dict(rtt=.001, missing=2)
    backend, device, clock, _, _ = probe_fixture(tmp_path, monkeypatch, [dict(rtt=.0031138), second])
    with pytest.raises(RuntimeError):
        backend.wait_until_ready(.5)
    assert len(device.sent) == 2 and backend._first_action_anchor_s is None


def test_trial_disabled_keeps_original_single_probe_mapping(tmp_path, monkeypatch):
    backend, device, _, _, _ = probe_fixture(tmp_path, monkeypatch, [dict(rtt=.0031138)], trial=False)
    assert backend.wait_until_ready(.5)
    assert len(device.sent) == 1
    assert backend._clock_uncertainty_ms == pytest.approx(1.5569)


def test_all_complete_coarse_samples_retain_first_honest_estimate_without_weakening_gate(tmp_path, monkeypatch):
    backend, device, _, _, _ = probe_fixture(tmp_path, monkeypatch,
        [dict(rtt=.004), dict(rtt=.0032), dict(rtt=.003), dict(rtt=.0028)])
    assert backend.wait_until_ready(.5) and len(device.sent) == 4
    assert backend._clock_uncertainty_ms > 1
    report = backend._startup_probe_report()
    assert report['frozen'] and report['selected_attempt'] == 1
    assert report['reason'] == 'coarse-complete-no-qualified-sample'
    assert all(row['complete'] for row in report['attempts']) and report['pending'] is None


def test_first_good_sample_fast_path_and_frozen_mapping_never_change_again(tmp_path, monkeypatch):
    backend, device, clock, _, _ = probe_fixture(tmp_path, monkeypatch, [dict(rtt=.001)])
    assert backend.wait_until_ready(.5) and len(device.sent) == 1
    frozen = backend._device_clock_offset_s, backend._clock_uncertainty_ms, backend._first_read_delay_s
    clock[0] += .1
    assert backend.wait_until_ready(.5)
    backend._playback_observation_started = True
    with pytest.raises(RuntimeError):
        backend._publish_startup_probe_locked()
    assert frozen == (backend._device_clock_offset_s, backend._clock_uncertainty_ms, backend._first_read_delay_s)
    assert len(device.sent) == 1


def test_original_prepare_deadline_is_shared_and_can_skip_unsafe_extra_send(tmp_path, monkeypatch):
    backend, device, clock, _, _ = probe_fixture(tmp_path, monkeypatch, [dict(rtt=.080)])
    assert backend.wait_until_ready(.1) and len(device.sent) == 1
    assert clock[0] <= 200.1 and backend._clock_uncertainty_ms > 1


def test_extra_probe_drain_uses_fixed_time_budget_and_blocks_late_wait_from_input(tmp_path, monkeypatch):
    backend, device, clock, _, cleanup = probe_fixture(tmp_path, monkeypatch,
        [dict(rtt=.0031138), dict(rtt=.8)])
    with pytest.raises(RuntimeError, match='未完整回读'):
        backend.wait_until_ready(.5)
    assert len(device.sent) == 2 and clock[0] < 200.3 and cleanup
    assert not backend._startup_probe_frozen and backend._device_clock_offset_s is None
    clock[0] = 201.
    backend._observe_new_logs()
    assert backend._calibrator.event_count == 0 and not backend._playback_observation_started
    with pytest.raises(RuntimeError):
        backend.start(201.)


@pytest.mark.parametrize('fault', ['generation-change', 'gap', 'wrong-command'])
def test_generation_and_exact_raw_receipt_sequence_are_required(tmp_path, monkeypatch, fault):
    backend, device, _, _, _ = probe_fixture(tmp_path, monkeypatch,
        [dict(rtt=.0031138), dict(rtt=.001, **{fault: True})])
    with pytest.raises(RuntimeError):
        backend.wait_until_ready(.5)
    assert not backend._startup_probe_frozen and backend._first_action_anchor_s is None


@pytest.mark.parametrize('point', ['before-wait', 'after-send', 'during-wait'])
def test_stop_preempts_any_additional_probe_wait_or_clock_freeze(tmp_path, monkeypatch, point):
    backend, device, _, stopped, _ = probe_fixture(tmp_path, monkeypatch,
        [dict(rtt=.0031138), dict(rtt=.001)],
        stop_after=2 if point == 'after-send' else None, stop_wait=point == 'during-wait')
    if point == 'before-wait':
        stopped[0] = True
    with pytest.raises(InterruptedError):
        backend.wait_until_ready(.5, should_stop=lambda: stopped[0])
    assert len(device.sent) == (1 if point == 'before-wait' else 2)
    assert not backend._startup_probe_frozen and backend._device_clock_offset_s is None


def test_jlog_quantization_is_included_before_deciding_first_sample_is_good(tmp_path, monkeypatch):
    backend, device, _, _, _ = probe_fixture(tmp_path, monkeypatch,
        [dict(rtt=.0019995), dict(rtt=.001)])
    assert backend.wait_until_ready(.5) and len(device.sent) == 2
    report = backend._startup_probe_report()
    assert report['attempts'][0]['uncertainty_ms'] > 1 and report['selected_attempt'] == 2
    for row in report['attempts']:
        assert row['offset_lower_s'] <= 100 <= row['offset_upper_s']
        assert row['first_cursor'] > row['raw_start_cursor'] and row['last_cursor'] == row['first_cursor'] + 2
