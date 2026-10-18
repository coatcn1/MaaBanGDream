from types import SimpleNamespace

from agent.realtime.profile_play_action import StallSafeCapture


def test_prefetched_frame_keeps_job_and_times_when_next_job_is_posted(monkeypatch):
    clock = [1.0]
    monkeypatch.setattr('agent.realtime.profile_play_action.time.perf_counter', lambda: clock[0])
    jobs = []

    class Job:
        def __init__(self):
            self.job_id = 700 + len(jobs)
            self.image = object()
            self.done = False

        def get(self):
            clock[0] += .01
            return self.image

        def wait(self):
            raise AssertionError('prefetch must not wait')

    def post():
        job = Job()
        jobs.append(job)
        clock[0] += .01
        return job

    capture = StallSafeCapture(SimpleNamespace(post_screencap=post))
    capture.enable_fresh_prefetch()
    capture._last_image = object()
    capture.prefetch_fresh(2)
    jobs[0].done = True
    clock[0] = 2
    capture.prefetch_fresh(3)
    assert capture() is jobs[0].image
    metadata = capture.frame_metadata
    assert metadata['screenshot_job_id'] == 700
    assert metadata['sequence'] == 1
    assert metadata['request_s'] == 1
    assert metadata['post_return_s'] == 1.01
    assert metadata['first_done_observed_s'] == 2
    assert metadata['get_begin_s'] == 2
    assert metadata['get_end_s'] == metadata['accept_s'] == 2.01
    assert metadata['first_return_s'] >= metadata['accept_s']
    assert metadata['render_s'] is metadata['capture_completed_s'] is None
    assert metadata['reused'] is False
    clock[0] = 4
    assert capture() is jobs[0].image
    assert capture.frame_metadata == {**metadata, 'reused': True}
    metadata['request_s'] = -1
    assert capture.frame_metadata['request_s'] == 1
    jobs[1].done = True
    assert capture() is jobs[1].image
    assert capture.frame_metadata['screenshot_job_id'] == 701
    assert capture.frame_metadata['sequence'] == 2


def test_initial_blocking_wait_records_upper_bound_and_get_interval(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr('agent.realtime.profile_play_action.time.perf_counter', lambda: clock[0])

    class Job:
        job_id = 99
        done = False

        def wait(self):
            clock[0] = 11
            return self

        def get(self):
            clock[0] = 12
            return 'frame'

    capture = StallSafeCapture(SimpleNamespace(post_screencap=Job))
    capture.enable_fresh_prefetch()
    assert capture() == 'frame'
    metadata = capture.frame_metadata
    assert metadata['screenshot_job_id'] == 99
    assert metadata['first_done_observed_s'] == metadata['get_begin_s'] == 11
    assert metadata['get_end_s'] == metadata['accept_s'] == metadata['first_return_s'] == 12
