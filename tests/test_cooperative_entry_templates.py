from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).parents[1]


def test_cooperative_entry_keeps_both_layouts_at_the_original_threshold():
    pipeline = json.loads(
        (ROOT / "resource/pipeline/cooperative_live.json").read_text(encoding="utf-8")
    )
    node = pipeline["CooperativeLiveEntryTemplate"]
    assert node["template"] == [
        "cooperative/live_entry.png",
        "cooperative/live_entry_compact.png",
    ]
    assert node["threshold"] == 0.88
    for name, click in (("CooperativeSelectPage", False), ("CooperativeEnterLive", True)):
        params = pipeline[name]["custom_action_param"]
        assert params["template_node"] == "CooperativeLiveEntryTemplate"
        assert params["click"] is click
    params = pipeline["CooperativeEnterLive"]["custom_action_param"]
    assert params["delivery_node"] == "CooperativeRoomSelectMarker"
    assert 10000 <= params["delivery_timeout_ms"] <= 15000
    assert params["delivery_retry_interval_ms"] >= 1000
    assert params["delivery_max_clicks"] == 3
    marker = pipeline[params["delivery_node"]]
    assert marker["template"] == "cooperative/room_search.png"
    assert marker["roi"] == [557, 612, 216, 76]
    assert marker["threshold"] == 0.9
    interface = json.loads((ROOT / "interface.json").read_text(encoding="utf-8"))

    def check_overrides(value):
        if isinstance(value, dict):
            override = value.get("CooperativeEnterLive", {})
            if "custom_action_param" in override:
                assert override["custom_action_param"]["delivery_node"] == params["delivery_node"]
            for child in value.values():
                check_overrides(child)
        elif isinstance(value, list):
            for child in value:
                check_overrides(child)

    check_overrides(interface)


def test_native_live_select_accepts_both_layouts_without_ocr_or_device_input(tmp_path):
    # 导入 AgentServer 会替换原生绑定，因此需要隔离真实框架。
    code = r'''
import json
from pathlib import Path
import sys

import cv2
import numpy as np
from maa.controller import CustomController
from maa.pipeline import JRecognitionType, JTemplateMatch
from maa.resource import Resource
from maa.tasker import Tasker
from maa.toolkit import Toolkit

data = json.load(sys.stdin)
root = Path(data["root"])
sys.path.insert(0, str(root))
Toolkit.init_option(data["log_dir"])
Tasker.set_log_dir(data["log_dir"])
resource = Resource()
assert resource.post_bundle(root / "resource").wait().succeeded
pipeline = json.loads((root / "resource/pipeline/cooperative_live.json").read_text(encoding="utf-8"))
entry = pipeline["CooperativeLiveEntryTemplate"]

image = np.zeros((720, 1280, 3), dtype=np.uint8)
class SavedImageController(CustomController):
    connect = lambda self: True
    request_uuid = lambda self: "offline-cooperative-template-regression"
    screencap = lambda self: image.copy()
    def reject_input(self, *_args):
        raise AssertionError("Saved-image regression must never send device input")
    start_app = stop_app = click = swipe = touch_down = touch_move = touch_up = click_key = input_text = key_down = key_up = reject_input

controller = SavedImageController()
assert controller.post_connection().wait().succeeded
tasker = Tasker()
assert tasker.bind(resource, controller)

from maa.agent.agent_server import AgentServer
AgentServer.custom_action = lambda _name: lambda cls: cls
from agent.live_select import LiveSelectFind
from maa.library import Library
import maa
Library.open(Path(maa.__file__).parent / "bin", agent_server=False)
assert resource.register_custom_action("LiveSelectFind", LiveSelectFind())

def recognize(templates):
    job = tasker.post_recognition(
        JRecognitionType.TemplateMatch,
        JTemplateMatch(template=templates, threshold=[entry["threshold"]]),
        image,
    ).wait()
    detail = job.get()
    return detail.nodes[0].recognition

for template, x, y in (
    ("cooperative/live_entry.png", 970, 400),
    ("cooperative/live_entry_compact.png", 989, 483),
    ("cooperative/live_entry_compact.png", 760, 260),
):
    image[:] = 0
    patch = cv2.imdecode(np.fromfile(root / "resource/image" / template, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert patch is not None
    h, w = patch.shape[:2]
    image[y:y+h, x:x+w] = patch
    result = recognize(entry["template"])
    assert result.hit and (result.box.x, result.box.y, result.box.w, result.box.h) == (x, y, w, h)
    if template.endswith("_compact.png"):
        # 复现原始故障：大卡片标题模板无法匹配此入口。
        assert not recognize(["cooperative/live_entry.png"]).hit
    params = dict(pipeline["CooperativeSelectPage"]["custom_action_param"])
    params.update(click=False, timeout_ms=0, interval_ms=0)
    action = tasker.post_task("CooperativeSelectPage", {
        "CooperativeSelectPage": {"next": [], "on_error": [], "custom_action_param": params},
    }).wait()
    assert action.succeeded, template

# 仅凭颜色不能通过模板门禁。
image[:] = 0
image[202:345, 949:1162] = (213, 93, 183)
image[382:526, 949:1162] = (70, 70, 235)
assert not recognize(entry["template"]).hit
image[:] = 0
assert not recognize(entry["template"]).hit
marker = pipeline["CooperativeRoomSelectMarker"]
patch = cv2.imdecode(np.fromfile(root / "resource/image" / marker["template"], dtype=np.uint8), cv2.IMREAD_COLOR)
h, w = patch.shape[:2]
image[620:620+h, 565:565+w] = patch
assert tasker.post_task("CooperativeRoomSelectMarker").wait().succeeded
print("cooperative entry: legacy, compact, moved compact, and negatives passed")
'''
    result = subprocess.run(
        [sys.executable, "-B", "-c", code],
        input=json.dumps({"root": str(ROOT), "log_dir": str(tmp_path)}),
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "legacy, compact, moved compact, and negatives passed" in result.stdout
