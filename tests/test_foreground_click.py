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


def run_arg(node_name, *, box=(10, 20, 30, 40)):
    return SimpleNamespace(
        node_name=node_name,
        box=SimpleNamespace(x=box[0], y=box[1], w=box[2], h=box[3]),
    )


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
