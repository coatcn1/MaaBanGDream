from types import SimpleNamespace

import pytest

from agent.realtime import engine as engine_module
from agent.realtime import profile_play_action as play_module
from agent.realtime.native_life_feedback import NativeLifeFeedback
from agent.realtime.profile_play_action import StallSafeCapture
from test_native_clock_domains import fixture
from test_native_life_feedback import _feedback_image


def run_async(tmp_path, monkeypatch, delay, *, base_job=False, successor=None, stop_on_post=False,
              peek_error=False, done_error=False):
    backend, _, now, _, commit, _, _ = fixture(tmp_path, monkeypatch)
    anchor = now[0]
    stopped = [False]
    calls = SimpleNamespace(jobs=[], observations=[], acknowledgements=[], monitors=[], samplers=[],
                            done_checks=[], get_attempts=[], warnings=[])
    image = _feedback_image(None)
    commit(anchor)
    transitioned = [False]

    def sleep(seconds):
        now[0] += seconds
        if successor and not transitioned[0] and now[0] >= anchor+.055:
            transitioned[0] = True
            commit(anchor+.055, kind=successor, command='d')

    monkeypatch.setattr(engine_module.time, 'sleep', sleep)
    monkeypatch.setattr(engine_module, 'warning', calls.warnings.append)
    monkeypatch.setattr(play_module.time, 'perf_counter', lambda: now[0])

    class Job:
        def __init__(self, request):
            self.request = request
            self.done_at = request+delay
            self.job_id = len(calls.jobs)+1
            self.gets = self.waits = 0

        @property
        def done(self):
            assert not stopped[0], '停止后不得peek异步作业'
            calls.done_checks.append((self.job_id, now[0]))
            if done_error:
                raise RuntimeError('SDK done query failed')
            return now[0] >= self.done_at

        def get(self):
            calls.get_attempts.append((self.job_id, now[0]))
            assert now[0] >= self.done_at
            self.gets += 1
            return image

        def wait(self):
            self.waits += 1
            pytest.fail('Native已启动后不得等待异步截图')

    def post():
        assert not stopped[0]
        job = Job(now[0])
        calls.jobs.append(job)
        if stop_on_post:
            stopped[0] = True
        return job

    class Capture(StallSafeCapture):
        def __call__(self):
            result = super().__call__()
            now[0] += .0001
            return result
    capture = Capture(SimpleNamespace(post_screencap=post))
    capture.enable_fresh_prefetch()
    capture._last_image = image
    capture._sequence = 1
    capture._image_request_s = anchor
    capture._image_metadata = dict(request_s=anchor, first_done_observed_s=anchor)
    if base_job:
        capture._pending = capture._post()
        if base_job == 'ready':
            capture._pending.done_at = anchor
    if peek_error:
        def broken():
            raise RuntimeError('ready peek failure')
        capture.has_ready_frame = broken

    class Feedback(NativeLifeFeedback):
        def __init__(self, target):
            super().__init__(target)
            calls.samplers.append(self)

        def capture_requested(self, stamp):
            calls.acknowledgements.append((self._clocked_capture_token, stamp))
            return super().capture_requested(stamp)

        def fresh(self, metadata, stamp):
            result = super().fresh(metadata, stamp)
            if result:
                calls.observations.append((dict(metadata), stamp))
            return result

    monkeypatch.setattr(engine_module, 'NativeLifeFeedback', Feedback)
    for name, value in dict(arm=lambda: None, start=lambda _anchor: None,
                            observe_start_frame=lambda _image, stamp: stamp,
                            stop=lambda: None, poll=lambda _stamp: None, report=lambda: {},
                            set_visual_phase_guard=None).items():
        monkeypatch.setattr(backend, name, value)
    monitor = SimpleNamespace(active=True, mark_active=lambda _stamp: None,
        observe=lambda _image, stamp: calls.monitors.append(stamp) or 'active')
    engine = engine_module.RealtimeEngine(SimpleNamespace(), SimpleNamespace(timing_offset_ms=63),
        SimpleNamespace(close=lambda: None, dispatch=lambda *_: pytest.fail('Native不派Legacy输入')),
        clock=lambda: now[0], native_backend=backend, playfield_monitor=monitor)
    stats = engine.run(capture, lambda: stopped[0], duration_seconds=1., target_fps=60)
    assert not stats.cleanup_failed
    return calls, stats, capture, anchor


@pytest.mark.parametrize('delay', [.010, .029, .031, .060])
def test_whole_engine_consumes_last_clocked_request_before_base_period(tmp_path, monkeypatch, delay):
    calls, _, _, anchor = run_async(tmp_path, monkeypatch, delay)
    assert len(calls.acknowledgements) == 2
    target_jobs = calls.jobs[:2]
    observed = {metadata['screenshot_job_id']: (metadata, stamp) for metadata, stamp in calls.observations
                if metadata.get('screenshot_job_id') is not None}
    for job in target_jobs:
        metadata, stamp = observed[job.job_id]
        assert job.done_at <= stamp <= job.done_at+.0021
        assert stamp-anchor < .2
        assert metadata['request_s'] == job.request
        assert stamp-metadata['request_s'] <= .08
        assert metadata['first_done_observed_s'] <= stamp
        assert job.gets == 1 and job.waits == 0
    sequences = [metadata['sequence'] for metadata, _ in calls.observations]
    assert len(sequences) == len(set(sequences))
    assert all(stamp-anchor >= .2 for stamp in calls.monitors[1:])
    assert all(right-left >= .199 for left, right in zip(calls.monitors, calls.monitors[1:]))


@pytest.mark.parametrize('base_job', [False, 'inflight', 'ready'])
@pytest.mark.parametrize('successor', ['tap', 'down'])
def test_ready_consumption_does_not_depend_on_current_token_or_tap_eligibility(tmp_path, monkeypatch,
                                                                            base_job, successor):
    calls, _, _, _ = run_async(tmp_path, monkeypatch, .031, base_job=base_job, successor=successor)
    observed = {metadata.get('screenshot_job_id') for metadata, _ in calls.observations}
    assert calls.jobs[0].job_id in observed
    assert all(job.gets <= 1 and job.waits == 0 for job in calls.jobs)
    assert len(calls.acknowledgements) <= len(calls.jobs)-int(bool(base_job))


def test_stop_after_post_precedes_any_ready_peek_or_frame_observation(tmp_path, monkeypatch):
    calls, stats, _, _ = run_async(tmp_path, monkeypatch, .01, stop_on_post=True)
    assert stats.stopped and len(calls.jobs) == 1
    assert calls.jobs[0].gets == 0 and not calls.done_checks
    assert len(calls.observations) == 1


def test_peek_exception_degrades_to_base_without_disabling_feedback(tmp_path, monkeypatch):
    calls, _, _, anchor = run_async(tmp_path, monkeypatch, .01, peek_error=True)
    assert calls.samplers[0].disabled_reason is None
    assert len([row for row in calls.warnings if 'NativeCaptureReady' in row]) == 1
    assert all(stamp-anchor >= .2 for _, stamp in calls.observations[1:])


def test_real_job_done_exception_never_becomes_ready_or_early_get_post_ack(tmp_path, monkeypatch):
    calls, _, _, anchor = run_async(tmp_path, monkeypatch, .01, done_error=True)
    assert calls.samplers[0].disabled_reason is None
    assert len([row for row in calls.warnings if 'NativeCaptureReady' in row]) == 1
    assert len([job for job in calls.jobs if job.request-anchor < .2]) == 1
    assert len([row for row in calls.acknowledgements if row[1]-anchor < .2]) == 1
    assert all(stamp-anchor >= .2 for _, stamp in calls.get_attempts)
    assert all(stamp-anchor >= .2 for _, stamp in calls.observations[1:])


def test_stop_after_post_precedes_real_job_done_exception(tmp_path, monkeypatch):
    calls, stats, _, _ = run_async(tmp_path, monkeypatch, .01, done_error=True, stop_on_post=True)
    assert stats.stopped and not calls.done_checks and not calls.get_attempts
    assert not calls.warnings and len(calls.jobs) == 1


def test_peek_never_gets_posts_or_waits_and_unread_cache_precedes_new_done_job(monkeypatch):
    clock = [1.]
    monkeypatch.setattr(play_module.time, 'perf_counter', lambda: clock[0])
    posts = []
    jobs = [SimpleNamespace(done=True, job_id=i, get=lambda i=i: i,
                            wait=lambda: pytest.fail('peek不得wait')) for i in (1, 2)]
    def post():
        posts.append(clock[0])
        return jobs[len(posts)-1]
    capture = StallSafeCapture(SimpleNamespace(post_screencap=post))
    capture.enable_fresh_prefetch()
    capture._last_image = -1
    assert capture.prefetch_fresh(1.)
    calls = []
    jobs[0].get = lambda: calls.append(1) or 1
    assert capture.has_ready_frame()
    assert posts == [1.] and calls == []
    first_done = capture._pending_metadata['first_done_observed_s']
    clock[0] = 1.005
    assert capture.has_ready_frame()
    assert capture._pending_metadata['first_done_observed_s'] == first_done
    clock[0] = 1.01
    assert capture.prefetch_fresh(1.01)
    assert calls == [1] and len(posts) == 2
    jobs[1].get = lambda: calls.append(2) or 2
    assert capture.has_ready_frame()
    assert capture() == 1 and calls == [1]
    assert capture.frame_metadata['sequence'] == 1 and not capture.frame_metadata['reused']
    assert capture.has_ready_frame()
    assert capture() == 2 and calls == [1, 2]
    assert capture.frame_metadata['sequence'] == 2 and not capture.frame_metadata['reused']
    assert not capture.has_ready_frame()
    assert capture() == 2 and capture.frame_metadata['reused']
    assert len(posts) == 2 and calls == [1, 2]
