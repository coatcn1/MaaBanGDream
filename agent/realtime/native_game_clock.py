"""既有截图上的可选游戏相位观察；不改变首拍、Profile 或执行回执门禁。"""

from collections import Counter, deque
import math
import queue
from statistics import median
import threading

from .optional_diagnostics import warning


class NativeGameClockController:
    """在截图线程仲裁视觉请求，唯一 Native owner 仍负责真正应用。"""

    def __init__(self, backend, *, matcher):
        self.backend = backend
        self.matcher = matcher
        self.run_id = backend._run_id
        self.events = deque(maxlen=128)
        self.summary = Counter()
        self.samples = deque(maxlen=3)
        self.last_sequence = -1
        self.last_frame_s = None
        self.pending_sequence = None
        self.applied_effect = None
        self.trusted_at = None
        self.trusted_lag_ms = None
        self._matcher_reset = (0, 0)
        self.baseline_phase_ms = None
        self.visual_applied_ms = 0.0
        self.visual_origin_ms = 0.0
        self._needs_visual_rebase = False
        self.closed = False
        self.epoch = 0
        self._frames = queue.Queue(maxsize=1)
        self._results = queue.Queue(maxsize=1)
        self._stop = threading.Event()
        self._thread = None
        self._reset_revision = 0
        self._reset_full_revision = 0

    def _reset_matcher_if_due(self):
        revision, full_revision = self._matcher_reset
        if revision != self._reset_revision:
            reset = getattr(self.matcher, "reset", None)
            if callable(reset):
                reset(full=full_revision > self._reset_full_revision)
            self._reset_revision = revision
            self._reset_full_revision = full_revision

    def _event(self, reason, now, **details):
        self.summary[reason] += 1
        if len(self.events) == self.events.maxlen:
            self.summary["events_dropped"] += 1
        self.events.append({"reason": reason, "observed_s": now, **details})

    def invalidate(self, now, reason):
        self.epoch += 1
        if reason in {"invalid-scene", "stale-run", "stopped", "terminal", "sampling-gap", "slow-or-reused-frames"} or reason.startswith("observer-error"):
            revision, full_revision = self._matcher_reset
            full_reset = reason not in {"sampling-gap", "slow-or-reused-frames"}
            self._matcher_reset = (revision + 1, revision + 1 if full_reset else full_revision)
            if full_reset:
                self.baseline_phase_ms = None
                self.trusted_lag_ms = None
                self._needs_visual_rebase = True
        self.samples.clear()
        status = self.backend.phase_status_snapshot()
        self._record_owned_application(status)
        if status.owner == "game-clock" and status.request_sequence == self.pending_sequence and status.state == "applied":
            self.applied_effect = status
        elif self.applied_effect is None:
            self.pending_sequence = None
        self.trusted_at = None
        self.backend.revoke_future_phase(reason, owner="game-clock")
        self.backend.set_visual_phase_guard(False)
        self._event(reason, now)

    def _record_owned_application(self, status):
        self.visual_applied_ms = self.backend.phase_owner_cumulative("game-clock") - self.visual_origin_ms

    def _fresh(self, metadata, now, visible):
        if self.closed:
            return False
        if self.backend._run_id != self.run_id:
            self.close(now, "stale-run")
            return False
        sequence, request = metadata.get("sequence"), metadata.get("request_s")
        valid = (visible and type(sequence) is int and sequence > self.last_sequence
                 and not metadata.get("reused", True)
                 and type(request) in {int, float} and math.isfinite(request)
                 and math.isfinite(now) and 0 <= now - request <= .080)
        if type(sequence) is int:
            self.last_sequence = max(sequence, self.last_sequence)
        if not valid:
            self.invalidate(now, "invalid-scene" if not visible else "slow-or-reused-frames")
            return False
        if self.last_frame_s is not None and now - self.last_frame_s > .5:
            self.invalidate(now, "sampling-gap")
        self.last_frame_s = now
        return True

    def _effect_ready(self, now, request_s):
        if self.pending_sequence is None:
            return True
        status = self.backend.phase_status_snapshot()
        latest = status
        self._record_owned_application(status)
        if self.applied_effect is not None:
            if latest.owner == "game-clock" and latest.request_sequence == self.pending_sequence and latest.state == "applied":
                self.applied_effect = latest
            status = self.applied_effect
        elif status.run_id != self.run_id or status.owner != "game-clock" or status.request_sequence != self.pending_sequence:
            return False
        if status.state in {"requested", "waiting-touch-boundary", "waiting-execution-receipts", "waiting-gap"}:
            return False
        if status.state == "applied":
            # 必须见到受本次调整影响的真实 DOWN，不能只看请求被应用。
            due = status.first_adjusted_due_s
            chunk = status.first_affected_chunk_sequence
            uncertainty = getattr(self.backend, "_clock_uncertainty_ms", None)
            if (due is None or chunk is None or latest.transient_actual_s is None
                or latest.transient_planned_s is None or latest.transient_chunk_sequence is None
                or uncertainty is None or not math.isfinite(uncertainty) or not 0 <= uncertainty <= 1
                or latest.transient_actual_s + uncertainty / 1000 > request_s
                or latest.transient_actual_s < due
                or round(latest.transient_planned_s * 1e9) < round(due * 1e9)
                or latest.transient_chunk_sequence < chunk
                or (status.first_affected_action_token is not None
                    and (latest.transient_action_token is None
                         or latest.transient_action_token < status.first_affected_action_token))):
                return False
        self._event("effect-observed" if status.state == "applied" else "request-terminal", now,
                    sequence=self.pending_sequence, state=status.state, terminal_reason=status.terminal_reason)
        self.pending_sequence = None
        self.applied_effect = None
        self.samples.clear()
        return True

    def _accept(self, result, metadata, observed_s, now):
        reason = result.get("reason", "unknown-match")
        if reason.startswith("observer-error"):
            self.close(now, reason)
            warning("NativeGameClock disabled=true reason=" + reason)
            return
        lag = result.get("relative_lag_ms")
        if (result.get("status") != "matched" or type(lag) not in {int, float}
            or not math.isfinite(lag) or abs(lag) > 200 or now - observed_s > .5):
            self.invalidate(now, result.get("reason", "unknown-match"))
            return
        status = self.backend.phase_status_snapshot()
        bounds = result.get("capture_bounds_ms", (lag, lag))
        if (not isinstance(bounds, (tuple, list)) or len(bounds) != 2
            or any(type(value) not in {int, float} or not math.isfinite(value) for value in bounds)
            or bounds[0] > lag or bounds[1] < lag):
            self.invalidate(now, "invalid-capture-bounds")
            return
        if not self._effect_ready(now, metadata["request_s"]):
            if self.trusted_lag_ms is None or abs(lag - self.trusted_lag_ms) > 20:
                self.invalidate(now, "owned-target-contradicted")
                return
            self.trusted_at = observed_s
            self._event("waiting-owned-effect", now)
            return
        if self._needs_visual_rebase:
            # 完整场景基准已重建时，旧 epoch 的已应用相位属于新基准，不能再次归还。
            # 这个入口位于旧效果执行门禁之后，不能把未执行欠账吸收到新 epoch。
            self.visual_origin_ms = self.backend.phase_owner_cumulative("game-clock")
            self.visual_applied_ms = 0.0
            self._needs_visual_rebase = False
        self.samples.append((observed_s, float(lag), tuple(bounds)))
        if len(self.samples) < 3:
            return
        values = [value for _, value, _ in self.samples]
        if self.samples[-1][0] - self.samples[0][0] > .5 or max(values) - min(values) > 20:
            self.invalidate(now, "unstable-match")
            return
        lower = min(bounds[0] for _, _, bounds in self.samples)
        upper = max(bounds[1] for _, _, bounds in self.samples)
        lag = median(values)
        if self.baseline_phase_ms is None:
            if lower < 15 and upper > -15:
                self._event("neutral-game-clock", now, lag_ms=lag)
                return
            self.baseline_phase_ms = status.cumulative_phase_ms - self.visual_applied_ms
        # 视觉只拥有自己已执行的增量，未知期间原反馈的合法微调不能被反向抵消。
        foreign_phase = status.cumulative_phase_ms - self.baseline_phase_ms - self.visual_applied_ms
        target = self.baseline_phase_ms + foreign_phase + lag
        self.trusted_at = observed_s
        self.trusted_lag_ms = median(values)
        self.backend.set_visual_phase_guard(True)
        error = target - status.cumulative_phase_ms
        self._event("trusted-target", now, target_ms=target, lag_ms=median(values),
                    cumulative_phase_ms=status.cumulative_phase_ms)
        current = self.visual_applied_ms
        if abs(lag) < 15 and abs(self.visual_applied_ms) < 1e-6 and lower <= 0 <= upper:
            self.backend.set_visual_phase_guard(False)
            self.baseline_phase_ms = None
            self._event("visual-episode-finished", now)
            return
        # 整个采集不确定区间都须支持这一方向，普通截图抖动不能产生校正。
        if abs(error) < 15 or (error > 0 and lower - current < 15) or (error < 0 and upper - current > -15):
            return
        delta = 15.0 if error > 0 else -15.0
        if self.backend.request_future_phase(delta, now, owner="game-clock"):
            self.pending_sequence = self.backend.phase_status_snapshot().request_sequence
            self.samples.clear()
            self._event("requested", now, sequence=self.pending_sequence, delta_ms=delta)

    def observe(self, image, metadata, now, *, visible):
        """同步纯消费者 seam，仅供 worker 结果测试及离线 replay 使用。"""
        if not self._fresh(metadata, now, visible):
            return
        try:
            self._reset_matcher_if_due()
            result = self.matcher.observe(image, metadata["request_s"], now,
                                          self.backend.first_action_anchor_s)
            self._accept(result, metadata, now, now)
        except Exception as exc:
            self.close(now, "observer-error:" + type(exc).__name__)
            warning("NativeGameClock disabled=true reason=observer-error:" + type(exc).__name__)

    @staticmethod
    def _replace_latest(slot, value):
        try:
            slot.put_nowait(value)
        except queue.Full:
            try:
                slot.get_nowait()
            except queue.Empty:
                pass
            try:
                slot.put_nowait(value)
            except queue.Full:
                pass

    def _work(self):
        while not self._stop.is_set():
            try:
                epoch, image, metadata, observed, anchor = self._frames.get(timeout=.1)
            except queue.Empty:
                continue
            if epoch != self.epoch or self._stop.is_set():
                continue
            try:
                self._reset_matcher_if_due()
                result = self.matcher.observe(image, metadata["request_s"], observed, anchor)
            except Exception as exc:
                result = {"status": "unknown", "reason": "observer-error:" + type(exc).__name__}
            self._replace_latest(self._results, (epoch, result, metadata, observed))
            if result.get("reason", "").startswith("observer-error"):
                return

    def submit(self, image, metadata, now, *, visible):
        """只复用既有新截图，热路径不检测、不等待 worker，也不写盘。"""
        if not self._fresh(metadata, now, visible):
            return
        try:
            epoch, result, source, observed = self._results.get_nowait()
        except queue.Empty:
            pass
        else:
            if epoch == self.epoch:
                self._accept(result, source, observed, now)
        if self.trusted_at is not None and now - self.trusted_at > .5:
            self.invalidate(now, "stale-target")
        if self.closed:
            return
        if self._thread is None:
            self._thread = threading.Thread(target=self._work, name="native-game-clock", daemon=True)
            self._thread.start()
        # 截图控制器可能复用可写缓冲区；有限大小的一份快照明确转移给 worker。
        owned_image = image.copy()
        owned_image.flags.writeable = False
        self._replace_latest(self._frames, (self.epoch, owned_image, dict(metadata), now,
                                          self.backend.first_action_anchor_s))

    def close(self, now, reason="terminal"):
        self.closed = True
        self._stop.set()
        self.invalidate(now, reason)

    def report(self):
        return {"model": "relative-game-clock-v1", "run_id": self.run_id,
                "closed": self.closed, "owned_applied_ms": self.visual_applied_ms,
                "owned_origin_ms": self.visual_origin_ms,
                "episode_base_ms": self.baseline_phase_ms, "pending_sequence": self.pending_sequence,
                "summary": dict(self.summary), "events": list(self.events), "capacity": 128,
                "matcher": self.matcher.report() if hasattr(self.matcher, "report") else {}}
