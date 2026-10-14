"""首音门控的有界后台证据写入，不参与锚点或输入决策。"""

from copy import deepcopy
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import queue
import re
import threading

from .vision_io import imwrite_unicode


@dataclass(frozen=True, slots=True)
class _EvidenceState:
    state: str
    metadata_path: str
    roi_paths: tuple[str, str]
    warnings: tuple[str, ...] = ()


class NativeStartEvidenceTicket:
    def __init__(self, directory):
        self._status = _EvidenceState("queued", str(directory / "start.json"),
            (str(directory / "previous.png"), str(directory / "current.png")))

    def report(self):
        status = self._status
        return {"state": status.state, "metadata_path": status.metadata_path,
                "roi_paths": list(status.roi_paths), "warnings": list(status.warnings)}

    def _finish(self, state, warning=None):
        previous = self._status
        self._status = _EvidenceState(state, previous.metadata_path, previous.roi_paths,
                                      (warning,) if warning else ())


class NativeStartEvidenceWriter:
    def __init__(self, root="debug/recordings", *, capacity=2, start_worker=True):
        if capacity < 1:
            raise ValueError("首音证据队列容量必须为正数")
        self.root = Path(root)
        self._queue = queue.Queue(maxsize=capacity)
        # 全部局共用一个线程；每局只持 ticket，不积累等待线程或无界状态表。
        if start_worker:
            threading.Thread(target=self._run, name="native-start-evidence", daemon=True).start()

    def submit(self, run_id, rois, metadata):
        safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", str(run_id))[:80]
        ticket = NativeStartEvidenceTicket(self.root / ("native-start-" + safe_id))
        try:
            # 图像已在观察时复制并冻结，此处只复制小 JSON，禁止编码、建目录或写盘。
            self._queue.put_nowait((ticket, rois, deepcopy(metadata)))
        except queue.Full:
            ticket._finish("queue-full", "首音证据队列已满")
        except Exception as exc:
            ticket._finish("failed", "首音证据入队失败：" + type(exc).__name__)
        return ticket

    def _write_next(self, *, block=False):
        ticket, rois, metadata = self._queue.get(block=block)
        try:
            status = ticket.report()
            path = Path(status["metadata_path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            for target, roi in zip(status["roi_paths"], rois):
                if not imwrite_unicode(target, roi):
                    raise OSError("ROI 编码或写入失败")
            path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
            ticket._finish("written")
        except Exception as exc:
            ticket._finish("failed", "首音证据保存失败：" + type(exc).__name__)
            # 仅后台线程允许输出警告；队列满时主消费者只缓存状态供结果报告。
            if block:
                try:
                    logging.warning("Native 首音证据保存失败：%s", type(exc).__name__)
                except Exception:
                    pass
        finally:
            self._queue.task_done()

    def _run(self):
        while True:
            self._write_next(block=True)


_shared_writer = None
_shared_lock = threading.Lock()


def shared_start_evidence_writer():
    global _shared_writer
    # 在 Native 后端构造/预武装阶段初始化，首拍触发时只做 put_nowait。
    with _shared_lock:
        if _shared_writer is None:
            _shared_writer = NativeStartEvidenceWriter()
        return _shared_writer
