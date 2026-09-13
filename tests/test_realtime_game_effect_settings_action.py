from types import SimpleNamespace

import agent.realtime.game_effect_settings_action as action_module
import agent.realtime.performance_settings_action as performance_module
from agent.realtime.game_effect_settings_action import (
    DEFAULT_COORDINATES,
    RealtimeGameSpeedSettingsGate,
    _run_speed_settings_from_home,
)


def test_disabled_speed_gate_never_reads_or_opens_game_settings(monkeypatch):
    monkeypatch.setattr(
        action_module.RealtimeProfileStore,
        "runtime_options",
        lambda _store: {"note_speed_settings_enabled": False},
    )
    monkeypatch.setattr(
        action_module,
        "_run_speed_settings_from_home",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("关闭流速检查后不得进入游戏设置页")
        ),
    )
    context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False, controller=object())
    )

    assert RealtimeGameSpeedSettingsGate()._run(context, {}) is True


def test_enabled_speed_gate_runs_home_flow(monkeypatch):
    calls = []
    monkeypatch.setattr(
        action_module.RealtimeProfileStore,
        "runtime_options",
        lambda _store: {"note_speed_settings_enabled": True},
    )
    monkeypatch.setattr(
        action_module,
        "_run_speed_settings_from_home",
        lambda context, *, params: calls.append((context, params)) or True,
    )
    context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False, controller=object())
    )
    params = {"entry_mode": "home", "difficulty": "Expert"}

    assert RealtimeGameSpeedSettingsGate()._run(context, params) is True
    assert calls == [(context, params)]


def test_speed_home_flow_clicks_before_first_capture(monkeypatch):
    events = []
    image = object()
    monkeypatch.setattr(
        action_module,
        "_capture",
        lambda _context: events.append(("capture", None)) or image,
    )
    monkeypatch.setattr(
        action_module,
        "_click",
        lambda _context, point: events.append(("click", point)),
    )
    monkeypatch.setattr(action_module, "_wait", lambda *_args: None)
    monkeypatch.setattr(
        performance_module,
        "_expected_speed",
        lambda *args, **kwargs: (5.0, "expert.json"),
    )
    monkeypatch.setattr(
        performance_module,
        "_select_first_tab_and_read",
        lambda *args, **kwargs: events.append(("speed-read", None)) or 4.99,
    )
    monkeypatch.setattr(
        performance_module,
        "_adjust_speed",
        lambda *args, **kwargs: events.append(("speed-adjust", None))
        or (True, 5.0),
    )
    monkeypatch.setattr(
        performance_module,
        "_close_settings_dialog",
        lambda *args, **kwargs: events.append(("settings-close", None)),
    )
    monkeypatch.setattr(
        performance_module,
        "publish_verified_performance_settings",
        lambda **kwargs: events.append(("performance", kwargs)),
    )
    monkeypatch.setattr(
        performance_module,
        "activate_speed_settings_target",
        lambda target: events.append(("activate", target)),
    )
    context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False, controller=object())
    )

    assert _run_speed_settings_from_home(
        context,
        params={"difficulty": "Expert", "require_profile": True},
    ) is True

    names = [event[0] for event in events]
    assert names[:3] == ["click", "click", "capture"]
    assert names.index("speed-read") < names.index("speed-adjust")
    assert names.index("speed-adjust") < names.index("settings-close")
    assert events[0][1] == DEFAULT_COORDINATES["home_menu"]
    assert events[1][1] == DEFAULT_COORDINATES["options"]
    assert ("click", DEFAULT_COORDINATES["menu_close"]) in events
