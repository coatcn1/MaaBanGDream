from types import SimpleNamespace

from agent import foreground_click


class Job:
    def __init__(self, value=None):
        self.value = value

    def wait(self):
        return self

    def get(self):
        return self.value


class Controller:
    def __init__(self, foreground):
        self.foreground = foreground
        self.clicks = []

    def post_shell(self, _command, _timeout=20000):
        return Job(f"mCurrentFocus=Window{{123 u0 {self.foreground}/.MainActivity}}")

    def post_click(self, x, y):
        self.clicks.append((x, y))
        return Job()

    def post_screencap(self):
        return Job(object())


class FailingClickController(Controller):
    def post_click(self, _x, _y):
        class FailingJob:
            def wait(self):
                raise RuntimeError("touch backend failed")

        return FailingJob()


def run_arg(node_name, *, box=(10, 20, 30, 40), params=None):
    return SimpleNamespace(
        node_name=node_name,
        box=SimpleNamespace(x=box[0], y=box[1], w=box[2], h=box[3]),
        custom_action_param=params,
    )


class ConfirmationContext:
    def __init__(self, controller, recognitions):
        self.tasker = SimpleNamespace(stopping=False, controller=controller)
        self.recognitions = iter(recognitions)

    def run_recognition(self, _node, _image):
        return next(self.recognitions)


def test_pipeline_click_is_blocked_on_foreign_foreground():
    controller = Controller("com.bilibili.azurlane")
    context = SimpleNamespace(tasker=SimpleNamespace(stopping=False, controller=controller))

    assert not foreground_click.ForegroundClick().run(
        context, run_arg("AutoLiveLoginTap")
    )

    assert controller.clicks == []


def test_pipeline_click_preserves_fixed_target_center():
    controller = Controller("com.bilibili.star.bili")
    context = SimpleNamespace(tasker=SimpleNamespace(stopping=False, controller=controller))

    assert foreground_click.ForegroundClick().run(
        context,
        run_arg("AutoLiveHomeLive", box=(1085, 580, 180, 130)),
    )
    assert controller.clicks == [(1175, 645)]


def test_pipeline_click_accepts_framework_json_null_parameter():
    controller = Controller("com.bilibili.star.bili")
    context = SimpleNamespace(tasker=SimpleNamespace(stopping=False, controller=controller))

    assert foreground_click.ForegroundClick().run(
        context,
        run_arg("AutoLiveHomeLive", params="null"),
    )

    assert controller.clicks == [(25, 40)]


def test_pipeline_click_callback_contains_touch_job_exceptions():
    controller = FailingClickController("com.bilibili.star.bili")
    context = SimpleNamespace(tasker=SimpleNamespace(stopping=False, controller=controller))

    assert not foreground_click.ForegroundClick().run(
        context,
        run_arg("AutoLiveHomeLive", params="null"),
    )


def test_pipeline_click_uses_resolved_override_target_from_framework():
    controller = Controller("com.bilibili.star.bili")
    context = SimpleNamespace(tasker=SimpleNamespace(stopping=False, controller=controller))

    assert foreground_click.ForegroundClick().run(
        context,
        run_arg("AutoLiveDifficulty", box=(1051, 545, 1, 1)),
    )

    assert controller.clicks == [(1051, 545)]


def test_pipeline_click_treats_user_stop_as_cancellation_not_failure():
    controller = Controller("com.bilibili.star.bili")
    context = SimpleNamespace(tasker=SimpleNamespace(stopping=True, controller=controller))

    assert foreground_click.ForegroundClick().run(
        context,
        run_arg("AutoLiveHomeLive", box=(1085, 580, 180, 130)),
    )
    assert controller.clicks == []


def test_start_click_retries_until_the_start_marker_disappears():
    controller = Controller("com.bilibili.star.bili")
    marker_box = SimpleNamespace(x=100, y=200, w=40, h=20)
    context = ConfirmationContext(controller, [
        SimpleNamespace(hit=True, box=marker_box),
        SimpleNamespace(hit=False, box=None),
    ])

    assert foreground_click.ForegroundClick().run(
        context,
        run_arg(
            "RealtimeLiveRehearsalStart",
            params=(
                '{"confirm_absent_node":"RealtimeLiveRehearsalStart",'
                '"confirm_attempts":3,"confirm_interval_ms":0}'
            ),
        ),
    )

    assert controller.clicks == [(25, 40), (120, 210)]


def test_start_click_fails_when_the_start_marker_never_disappears():
    controller = Controller("com.bilibili.star.bili")
    marker_box = SimpleNamespace(x=100, y=200, w=40, h=20)
    context = ConfirmationContext(controller, [
        SimpleNamespace(hit=True, box=marker_box),
        SimpleNamespace(hit=True, box=marker_box),
        SimpleNamespace(hit=True, box=marker_box),
    ])

    assert not foreground_click.ForegroundClick().run(
        context,
        run_arg(
            "RealtimeLiveRehearsalStart",
            params=(
                '{"confirm_absent_node":"RealtimeLiveRehearsalStart",'
                '"confirm_attempts":3,"confirm_interval_ms":0}'
            ),
        ),
    )

    assert controller.clicks == [(25, 40), (120, 210), (120, 210)]
