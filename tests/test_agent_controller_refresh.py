from pathlib import Path
import os
import subprocess
import sys


def test_real_memory_agent_nested_context_refreshes_controller_without_device_input(tmp_path):
    script = tmp_path / "memory_probe.py"
    script.write_text(r'''import json,sys,subprocess,os
from pathlib import Path
import numpy as np
from maa.custom_action import CustomAction
if len(sys.argv)>1:
    from maa.agent.agent_server import AgentServer
    from agent.realtime import profile_play_action as play
    play.require_game_foreground=lambda controller: (_ for _ in ()).throw(AssertionError("invalid memory info")) if not controller.info.get("offline_memory") else None
    @AgentServer.custom_action("FreshProbe")
    class Probe(CustomAction):
        def run(self,context,argv):
            try:
                old=context.tasker.controller
                assert context.run_task("Nested").status.succeeded
                fresh=play._fresh_foreground_controller(context)
                assert fresh.info["offline_memory"]
                assert fresh.cached_image is not None
                return True
            except Exception as exc:
                print(type(exc).__name__,str(exc),flush=True)
                return False
    AgentServer.start_up(sys.argv[1]);AgentServer.join();AgentServer.shut_down()
else:
    from maa.agent_client import AgentClient
    from maa.controller import CustomController
    from maa.resource import Resource
    from maa.tasker import Tasker
    from maa.toolkit import Toolkit
    root=Path.cwd();(root/"pipeline").mkdir()
    (root/"pipeline"/"test.json").write_text(json.dumps({"Start":{"recognition":"DirectHit","action":"Custom","custom_action":"FreshProbe"},"Nested":{"recognition":"DirectHit","action":"DoNothing"}}))
    Toolkit.init_option(root/"logs");Tasker.set_log_dir(root/"logs")
    resource=Resource();assert resource.post_bundle(root).wait().succeeded
    class Memory(CustomController):
        def connect(self):return True
        def request_uuid(self):return "offline-memory-controller"
        def screencap(self):return np.zeros((720,1280,3),np.uint8)
        def get_custom_info(self):return {"offline_memory":True}
        def start_app(self,*args):raise AssertionError("input forbidden")
        def stop_app(self,*args):raise AssertionError("input forbidden")
        def click(self,*args):raise AssertionError("input forbidden")
        def swipe(self,*args):raise AssertionError("input forbidden")
        def click_key(self,*args):raise AssertionError("input forbidden")
        def input_text(self,*args):raise AssertionError("input forbidden")
        def scroll(self,*args):raise AssertionError("input forbidden")
        def touch_down(self,*args):raise AssertionError("input forbidden")
        def touch_move(self,*args):raise AssertionError("input forbidden")
        def touch_up(self,*args):raise AssertionError("input forbidden")
        def key_down(self,*args):raise AssertionError("input forbidden")
        def key_up(self,*args):raise AssertionError("input forbidden")
    controller=Memory();assert controller.post_connection().wait().succeeded
    assert controller.post_screencap().wait().succeeded
    tasker=Tasker();assert tasker.bind(resource,controller)
    client=AgentClient.create_tcp();assert client.bind(resource);client.set_timeout(3000)
    child=subprocess.Popen([sys.executable,str(Path(__file__)),client.identifier],env=os.environ.copy(),stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    try:
        assert client.connect()
        assert tasker.post_task("Start").wait().succeeded
        print("fresh-after-nested",flush=True)
    finally:
        client.disconnect()
        try:child.communicate(timeout=3)
        except subprocess.TimeoutExpired:child.terminate();child.communicate(timeout=3)
''',encoding="utf-8")
    env=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    result=subprocess.run([sys.executable,str(script)],cwd=tmp_path,env=env,capture_output=True,text=True,timeout=25)
    assert result.returncode==0,result.stdout+result.stderr
    assert "fresh-after-nested" in result.stdout
