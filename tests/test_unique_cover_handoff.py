"""唯一封面跨启动阶段交接，和真实身份失败的外层手动跳车。"""
from types import SimpleNamespace
import pytest
import numpy as np
from agent.realtime import profile_play_action as profile
from agent.realtime import cooperative_action as coop, versus_action as versus
from agent.realtime.final_cover import FinalCoverIdentityConflict


@pytest.mark.parametrize("level_drift", [False, True])
def test_unique_cover_handoff_does_not_claim_an_ocr_title(monkeypatch, level_drift):
    selected = SimpleNamespace(level_drift_tolerated=level_drift)
    initial = SimpleNamespace(selection=selected, final_title_confirmed=False,
                              unique_cover_confirmed=True,
                              confirmation=SimpleNamespace(bestdori_song_id=253))
    class Resolver:
        reject_member_loading = False
        def __init__(self, **kwargs): pass
        def evidence_reason(self): return None
        def observe(self, *args, **kwargs):
            raise RuntimeError("reconfirmation required")
    monkeypatch.setattr(profile, "FinalCoverResolver", Resolver)
    monkeypatch.setattr(profile, "_save_final_cover_failure", lambda *a: None)
    monkeypatch.setattr(profile, "_frame_is_black", lambda _: False)
    monkeypatch.setattr(profile, "PlayfieldDetector", lambda: (lambda _: False))
    kwargs = dict(controller=None, live_run=SimpleNamespace(song_level=26,song_title=None),
                  selection=None, difficulty="Expert", stopping=lambda:False,
                  repository=SimpleNamespace(regional_level_drift_enabled=True),
                  initial_image=np.ones((720,1280,3),dtype=np.uint8),initial_resolution=initial)
    if level_drift:
        with pytest.raises(RuntimeError,match="reconfirmation required"):
            profile.wait_for_final_cover(**kwargs)
    else:
        outcome = profile.wait_for_final_cover(**kwargs)
        assert outcome.resolution is initial
        assert outcome.status == "confirmed"
        assert not outcome.resolution.final_title_confirmed


@pytest.mark.parametrize("module", [coop,versus])
def test_actual_profile_to_flow_identity_exception_reaches_manual_jump(monkeypatch,module):
    cls=module.CooperativeLiveFlow if module is coop else module.VersusLiveFlow
    flow=object.__new__(cls)
    flow.context=SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    flow.settings=dict(module.DEFAULT_SETTINGS,play_failure_retry_count=10)
    flow.room_recovery_trial=False
    flow.progress_callback=None
    flow.action_argv=lambda params: SimpleNamespace(custom_action_param="{}")
    attempts,jumps,failures=[],[],[]
    def fail(self,*args,**kwargs):
        attempts.append(True)
        raise FinalCoverIdentityConflict("actual final-cover call-chain evidence")
    monkeypatch.setattr(profile.RealtimeProfilePlay,"_run",fail)
    flow.run_attempt=lambda **kwargs: flow.play()
    def jump(reason):
        jumps.append(reason)
        raise module.JumpOutUnavailable(reason+"；请手动跳车")
    flow.jump_after_startup_failure=jump
    flow.recover_after_play_failure=lambda _: pytest.fail("身份冲突不得恢复重试")
    monkeypatch.setattr(module,"record_failure_reason",failures.append)
    assert flow.run() is False
    assert len(attempts)==len(jumps)==1
    assert "actual final-cover call-chain evidence" in failures[-1]
    assert "请手动跳车" in failures[-1]
