from types import SimpleNamespace
from agent.realtime.profile_play_action import StallSafeCapture


def test_legacy_double_buffer_and_empty_first_get_bind_actual_fallback_job(monkeypatch):
    clock = [20.0]
    monkeypatch.setattr('agent.realtime.profile_play_action.time.perf_counter', lambda: clock[0])
    jobs = []

    class Job:
        done = True

        def __init__(self):
            self.job_id = len(jobs) + 1
            self.image = None if self.job_id == 1 else object()

        def wait(self):
            return self

        def get(self):
            clock[0] += .01
            return self.image

    def post():
        job = Job()
        jobs.append(job)
        return job

    capture = StallSafeCapture(SimpleNamespace(post_screencap=post))
    assert capture() is jobs[1].image
    metadata = capture.frame_metadata
    assert metadata['screenshot_job_id'] == 2
    assert metadata['sequence'] == 1
    assert len(jobs) == 3
    assert capture() is jobs[3].image
    assert capture.frame_metadata['screenshot_job_id'] == 4
    assert metadata['screenshot_job_id'] == 2


def test_missing_job_id_is_unknown(monkeypatch):
    monkeypatch.setattr('agent.realtime.profile_play_action.time.perf_counter', lambda: 1.0)
    job = SimpleNamespace(done=True, get=lambda: object())
    capture = StallSafeCapture(SimpleNamespace(post_screencap=lambda: job))
    capture.enable_fresh_prefetch()
    capture()
    assert capture.frame_metadata['screenshot_job_id'] is None
