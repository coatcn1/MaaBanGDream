from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).parents[1]


def test_challenge_entry_retry_graph_is_bounded_and_keeps_original_prepare_path():
    pipeline = json.loads((ROOT / "resource/pipeline/challenge_live.json").read_text(encoding="utf-8"))
    for name in ("ChallengeEntry", "ChallengeEntryRetry"):
        params = pipeline[name]["custom_action_param"]
        assert params["delivery_node"] == "ChallengeSongMarker"
        assert params["delivery_label"] == "选曲页"
        assert params["delivery_timeout_ms"] == 15000
        assert params["delivery_retry_interval_ms"] == 4000
        assert params["delivery_max_clicks"] == 3
        assert pipeline[name]["next"] == ["ChallengeDifficulty"]
    assert pipeline["ChallengeDifficulty"]["next"] == ["ChallengePrepare"]
    assert pipeline["ChallengeEntry"]["on_error"] == ["ChallengeEntryRecoverHome"]
    recover = pipeline["ChallengeEntryRecoverHome"]
    assert recover["custom_action"] == "CommonRecover"
    assert recover["custom_action_param"]["restart_limit"] == 0
    assert recover["custom_action_param"]["escape_timeout_ms"] == 30000
    retry_path = ["ChallengeEntryRecoverHome", "ChallengeEntryRetryHomeLive", "ChallengeEntryRetryClose", "ChallengeEntryRetrySelectPage", "ChallengeEntryRetry"]
    assert pipeline["ChallengeEntryRecoverHome"]["next"] == ["ChallengeEntryRetryHomeLive"]
    assert pipeline["ChallengeEntryRetryHomeLive"]["next"] == ["ChallengeEntryRetryClose", "ChallengeEntryRetrySelectPage"]
    assert pipeline["ChallengeEntryRetryClose"]["template"] == pipeline["ChallengeClose"]["template"]
    assert pipeline["ChallengeEntryRetryClose"]["next"] == ["ChallengeEntryRetrySelectPage"]
    assert pipeline["ChallengeEntryRetrySelectPage"]["next"] == ["ChallengeEntryRetry"]
    for name in retry_path:
        assert pipeline[name]["on_error"] == ["ChallengeNoEvent"]
    reason = pipeline["ChallengeNoEvent"]["custom_action_param"]["reason"]
    assert "未能进入选曲页" in reason
    assert "没有可用" not in reason


@pytest.mark.parametrize("second_attempt", ["delivered", "failed", "stopped"])
def test_real_maafw_challenge_error_path_recovers_once_and_preserves_progress(tmp_path, second_attempt):
    # 用内存控制器执行真实 MaaFw 图；入口与恢复 Action 仍运行项目代码。
    code = r'''
import json, sys, threading, traceback
from pathlib import Path
from types import SimpleNamespace
import numpy as np
from maa.agent.agent_server import AgentServer
from maa.controller import CustomController
from maa.custom_action import CustomAction
from maa.library import Library
from maa.resource import Resource
from maa.tasker import Tasker
from maa.toolkit import Toolkit
import maa
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')
AgentServer.custom_action = lambda _name: lambda cls: cls
AgentServer.custom_recognition = lambda _name: lambda cls: cls
data = json.load(sys.stdin)
sys.path.insert(0, str(Path(data['repo']) / 'tests'))
from test_live_select import DeliveryContext
from test_common_recover import Context as RecoveryContext
from agent import live_select, common_recover, task_reporting
Library.open(Path(maa.__file__).parent / 'bin', agent_server=False)
root = Path(data['root'])
Toolkit.init_option(root / 'logs')
bundle = root / 'bundle'
(bundle / 'pipeline').mkdir(parents=True)
pipeline = data['pipeline']
names = ['ChallengeEntry', 'ChallengeEntryRecoverHome', 'ChallengeEntryRetryHomeLive', 'ChallengeEntryRetryClose',
         'ChallengeEntryRetrySelectPage', 'ChallengeEntryRetry', 'ChallengeDifficulty', 'ChallengeNoEvent']
for name in names:
    pipeline[name]['recognition'] = 'DirectHit'
    pipeline[name]['action'] = 'Custom'
    pipeline[name]['custom_action'] = 'Probe'
    pipeline[name]['pre_delay'] = pipeline[name]['post_delay'] = 0
    pipeline[name]['focus'] = {}
for name in ('ChallengeDifficulty', 'ChallengeNoEvent'):
    pipeline[name]['next'] = []
    pipeline[name]['on_error'] = []
pipeline['TaskReportVisible'] = {'recognition': 'DirectHit', 'action': 'DoNothing', 'pre_delay': 0, 'post_delay': 0}
(bundle / 'pipeline' / 'test.json').write_text(json.dumps(pipeline), encoding='utf-8')
class Memory(CustomController):
    connect = lambda self: True
    request_uuid = lambda self: 'offline-challenge-retry'
    screencap = lambda self: np.zeros((720, 1280, 3), np.uint8)
controller = Memory()
assert controller.post_connection().wait().succeeded
resource = Resource()
assert resource.post_bundle(bundle).wait().succeeded
tasker = Tasker()
assert tasker.bind(resource, controller)
now = [0.0]
live_select.time.monotonic = lambda: now[0]
live_select.time.sleep = lambda seconds: now.__setitem__(0, now[0] + seconds)
events = []
clicks = []
errors = []
entry_finished = threading.Event()
progress = []
class Probe(CustomAction):
    def run(self, context, argv):
        try:
            return self.probe(context, argv)
        except Exception:
            errors.append(traceback.format_exc())
            return False
    def probe(self, context, argv):
        name = argv.node_name
        events.append(name)
        if name == 'ChallengeEntry':
            restore = SimpleNamespace(task_detail=argv.task_detail, node_name=name, custom_action_param=json.dumps({
                'task_name': 'ChallengeLive', 'label': 'challenge', 'total': 13,
                'phase': 'restore', 'completed': 12, 'next_started': True,
            }))
            assert task_reporting.TaskProgress().run(context, restore)
            progress.append(task_reporting._states[argv.task_detail.task_id])
        assert progress[0].completed == 12 and progress[0].total == 13
        if name in ('ChallengeEntry', 'ChallengeEntryRetry'):
            fake = DeliveryContext([(100, 200)])
            recognize = fake.run_recognition
            def recognition(node, image):
                if node == 'ChallengeSongMarker':
                    return SimpleNamespace(hit=name == 'ChallengeEntryRetry' and data['second'] == 'delivered'
                                           and len(fake.tasker.controller.clicks) == 2)
                assert node == 'ChallengeEntryTemplate'
                return recognize('EntryTemplate', image)
            fake.run_recognition = recognition
            original_click = fake.tasker.controller.post_click
            def click(x, y):
                result = original_click(x, y)
                if name == 'ChallengeEntryRetry' and data['second'] == 'stopped':
                    fake.tasker.stopping = True
                    context.tasker.post_stop()
                return result
            fake.tasker.controller.post_click = click
            result = live_select.LiveSelectFind().run(fake, argv)
            clicks.append(len(fake.tasker.controller.clicks))
            if name == 'ChallengeEntryRetry':
                entry_finished.set()
            return result
        if name == 'ChallengeEntryRecoverHome':
            fake = RecoveryContext()
            fake.run_recognition = lambda node, image: SimpleNamespace(
                hit=node == 'ChallengeHomeMarker' and bool(fake.tasker.controller.keys), box=None)
            common_recover._wait_unless_stopping = lambda _, seconds: now.__setitem__(0, now[0] + seconds) or True
            assert common_recover.CommonRecover().run(fake, argv)
            assert fake.tasker.controller.keys == [4]
            assert not fake.tasker.controller.starts and not fake.tasker.controller.stops
            events.append('ESC')
        if name == 'ChallengeEntryRetrySelectPage':
            fake = DeliveryContext([(100, 200)])
            fake.run_recognition = lambda node, image: SimpleNamespace(hit=True, box=SimpleNamespace(x=100,y=200,w=80,h=40))
            assert live_select.LiveSelectFind().run(fake, argv)
            assert not fake.tasker.controller.clicks
        if name == 'ChallengeNoEvent':
            assert json.loads(argv.custom_action_param)['status'] == 'failure'
            return task_reporting.TaskOutcome().run(context, argv)
        return True
assert resource.register_custom_action('Probe', Probe())
job = tasker.post_task('ChallengeEntry').wait()
expected = ['ChallengeEntry', 'ChallengeEntryRecoverHome', 'ESC', 'ChallengeEntryRetryHomeLive', 'ChallengeEntryRetryClose',
            'ChallengeEntryRetrySelectPage', 'ChallengeEntryRetry']
if data['second'] == 'stopped':
    assert entry_finished.wait(3), errors
    assert events == expected, events
    assert clicks == [3, 1], clicks
else:
    expected.append('ChallengeDifficulty' if data['second'] == 'delivered' else 'ChallengeNoEvent')
    assert events == expected, events
    assert clicks == [3, 2 if data['second'] == 'delivered' else 3], clicks
    assert job.succeeded == (data['second'] == 'delivered')
assert progress[0].completed == 12 and progress[0].total == 13
assert not errors, errors
print('challenge-retry-ok')
'''
    pipeline = json.loads((ROOT / "resource/pipeline/challenge_live.json").read_text(encoding="utf-8"))
    result = subprocess.run(
        [sys.executable, "-c", code],
        input=json.dumps({"root": str(tmp_path), "repo": str(ROOT), "pipeline": pipeline, "second": second_attempt}),
        text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=25,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "challenge-retry-ok" in result.stdout
    if second_attempt == "failed":
        assert "12/13" in result.stdout
