"""既有截图上的可选游戏相位观察；不改变首拍、Profile 或执行回执门禁。"""

from collections import Counter, deque
import math
import queue
from statistics import median
import threading

from .optional_diagnostics import warning
from .native_observation_clock import ObservationClockView
from .native_shared_phase_budget import NativeSharedPhaseBudget


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
        self._sampling_gap_invalidated = False
        self.pending_sequence = None
        self.applied_effect = None
        effect_reader = getattr(backend, "phase_effect_snapshot", None)
        initial_effect = effect_reader() if callable(effect_reader) else None
        self._foreign_effect_epoch = self._effect_epoch(initial_effect) if initial_effect is not None else None
        self._foreign_effect_pending = None
        self.trusted_at = None
        self.trusted_lag_ms = None
        self._matcher_reset = (0, 0)
        self.baseline_phase_ms = None
        self.visual_applied_ms = 0.0
        self.visual_origin_ms = 0.0
        self._needs_visual_rebase = False
        self.closed = False
        self.epoch = 0
        self._semantic_epoch = 0
        self._hard_provenance = None
        self._opening_effect_key = None
        self._frames = queue.Queue(maxsize=1)
        self._results = queue.Queue(maxsize=1)
        self._stop = threading.Event()
        self._thread = None
        self._reset_revision = 0
        self._reset_full_revision = 0
        self._relation_required = bool(getattr(backend, "_clock_relation_required", False))
        self._relation_baseline_ms = None
        self._relation_trusted = None
        self._relation_reason = None
        self._relation_last_cursor = 0
        self._relation_recovery_after = 0
        self._relation_reader_warned = False
        self._relation_latest_delta_ms = None
        self._observation_required = bool(getattr(backend, '_native_clock_trial', False))
        self._observation_key = None
        try:
            initial_history = getattr(backend, 'phase_effect_history_snapshot', lambda: ())() if self._observation_required else ()
            if not isinstance(initial_history, tuple) or len(initial_history) > 64:
                raise ValueError('invalid history')
            initial_feedback_sequence = max((row.request_sequence for row in initial_history
                if row.run_id == self.run_id and row.owner == 'feedback'), default=0)
        except Exception:
            initial_history = None
            initial_feedback_sequence = 0
        self._shared_budget = (NativeSharedPhaseBudget(self.run_id,
            feedback_total=backend.phase_owner_cumulative('feedback'), feedback_sequence=initial_feedback_sequence)
            if self._observation_required else None)
        self._shared_before_raw = None

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
        self._semantic_epoch += 1
        if reason in {"invalid-scene", "stale-run", "stopped", "terminal", "sampling-gap", "slow-or-reused-frames"} or reason.startswith("observer-error"):
            revision, full_revision = self._matcher_reset
            full_reset = reason not in {"sampling-gap", "slow-or-reused-frames"}
            self._matcher_reset = (revision + 1, revision + 1 if full_reset else full_revision)
            if full_reset:
                self.baseline_phase_ms = None
                self.trusted_lag_ms = None
                self._needs_visual_rebase = True
        self.samples.clear()
        self._retain_owned_effect()
        self.trusted_at = None
        self.backend.revoke_future_phase(reason, owner="game-clock")
        # 非阻塞撤销可能与 owner 的 apply 交错；撤销未确认时仍保留本方请求身份。
        self._retain_owned_effect(confirm_terminal=True)
        self.backend.set_visual_phase_guard(False)
        self._event(reason, now)

    def _quality_gap(self, now, reason):
        # 质量中断撤当前决策与独占声明；合法历史种子和原 TTL 内的意图仍保留。
        self.epoch += 1
        self.samples.clear()
        self._retain_owned_effect()
        self.trusted_at = None
        self.backend.set_visual_phase_guard(False)
        self._event(reason, now)

    def _provenance(self):
        reader = getattr(self.backend, 'observation_provenance', None)
        return reader() if callable(reader) else None

    def _clock_gap(self, now, reason):
        provenance = self._provenance()
        if self._hard_provenance is not None and provenance != self._hard_provenance:
            self.invalidate(now, 'clock-hard-fence')
            revision, _full = self._matcher_reset
            self._matcher_reset = (revision + 1, revision + 1)
            self._needs_visual_rebase = True
        else:
            self._quality_gap(now, reason)
        self._hard_provenance = provenance

    def _phase_watermark(self):
        return self.backend.phase_effect_watermark()

    def _record_owned_application(self, status):
        self.visual_applied_ms = self.backend.phase_owner_cumulative("game-clock") - self.visual_origin_ms

    def _retain_owned_effect(self, *, confirm_terminal=False):
        status = self.backend.phase_status_snapshot()
        self._record_owned_application(status)
        if self.pending_sequence is None:
            return
        reader = getattr(self.backend, "phase_effect_snapshot", None)
        effect = reader() if callable(reader) else None
        candidates = [item for item in (status, effect) if item is not None
                      and item.run_id == self.run_id and item.owner == "game-clock"
                      and item.request_sequence == self.pending_sequence and item.state == "applied"]
        if candidates:
            self.applied_effect = max(candidates, key=lambda item: item.event_cursor)
        elif (confirm_terminal and self.applied_effect is None and status.run_id == self.run_id
                and status.owner == "game-clock" and status.request_sequence == self.pending_sequence
                and status.state in {"revoked", "rejected", "expired", "stopped"}):
            # 只认本请求的已确认终态；外来覆盖及锁忙不能证明本方效果不存在。
            self.pending_sequence = None

    def _relation_failed(self, now, reason, *, bad_cursor=None):
        if bad_cursor is not None:
            self._relation_recovery_after = max(self._relation_recovery_after, bad_cursor)
        elif self._relation_baseline_ms is not None and self._relation_trusted is not False:
            self._relation_recovery_after = max(self._relation_recovery_after, self._relation_last_cursor)
        if self._relation_trusted is not False:
            # invalidate 先保存已应用效果及本来源欠账，再只撤本来源未应用请求。
            if self._observation_required:
                self._clock_gap(now, 'clock-relation-' + reason)
            else:
                self.invalidate(now, "clock-relation-" + reason)
        self._relation_trusted = False
        self._relation_reason = reason

    def _clock_relation_ready(self, now):
        if self._observation_required:
            return self._clock_observation_ready(now)
        if not self._relation_required:
            return True
        try:
            reader = getattr(self.backend, "clock_relation_snapshot", None)
            snapshot = reader() if callable(reader) else None
            if not isinstance(snapshot, tuple) or not 1 <= len(snapshot) <= 64:
                self._relation_failed(now, "unavailable")
                return False
            rows = {}
            previous_cursor = 0
            for row in snapshot:
                cursor = row.command_cursor
                values = (row.actual_mapped_s, row.received_host_s, row.probe_uncertainty_ms)
                if (row.run_id != self.run_id or type(cursor) is not int or cursor <= 0
                        or any(type(value) not in {int, float} or not math.isfinite(value) for value in values)
                        or not 0 <= row.probe_uncertainty_ms <= 1
                        or cursor < previous_cursor or row.received_host_s > now
                        or row.actual_mapped_s > row.received_host_s + row.probe_uncertainty_ms / 1000):
                    self._relation_failed(now, "invalid-sample")
                    return False
                if cursor in rows and rows[cursor] != row:
                    self._relation_failed(now, "conflicting-cursor")
                    return False
                rows[cursor] = row
                previous_cursor = cursor
            if max(rows) < self._relation_last_cursor:
                self._relation_failed(now, "old-snapshot")
                return False
            self._relation_last_cursor = max(self._relation_last_cursor, max(rows))
            latest = rows[max(rows)]
            self._relation_latest_delta_ms = (latest.received_host_s - latest.actual_mapped_s) * 1000
            recent = [row for _, row in sorted(rows.items())
                      if now - 1.5 <= row.received_host_s - row.probe_uncertainty_ms / 1000
                      and row.received_host_s + row.probe_uncertainty_ms / 1000 <= now]
            if self._relation_baseline_ms is not None:
                bad = [row.command_cursor for row in recent if abs(
                    (row.received_host_s - row.actual_mapped_s) * 1000 - self._relation_baseline_ms) > 20]
                if bad and max(bad) > self._relation_recovery_after:
                    self._relation_failed(now, "changed", bad_cursor=max(bad))
                recent = [row for row in recent if row.command_cursor > self._relation_recovery_after]
            tail_age = min((now - (row.received_host_s - row.probe_uncertainty_ms / 1000)
                            for row in recent), default=float("inf"))
            if len(recent) < 3 or tail_age > .5:
                self._relation_failed(now, "insufficient-recent-commits")
                return False
            deltas = [(row.received_host_s - row.actual_mapped_s) * 1000 for row in recent]
            if max(deltas) - min(deltas) > 20:
                self._relation_failed(now, "unstable")
                return False
            if self._relation_baseline_ms is None:
                anchor = self.backend.first_action_anchor_s
                if anchor is None or not 0 <= now - anchor <= 8:
                    self._relation_failed(now, "opening-baseline-unavailable")
                    return False
                # 本局开场只冻结一次；持续的新偏差绝不吸收为新的时钟基线。
                self._relation_baseline_ms = median(deltas)
                self._event("clock-relation-baseline", now, delta_ms=self._relation_baseline_ms)
            if self._relation_trusted is not True:
                self._event("clock-relation-trusted", now, baseline_delta_ms=self._relation_baseline_ms,
                            newest_cursor=recent[-1].command_cursor)
            self._relation_trusted = True
            self._relation_reason = None
            return True
        except Exception as exc:
            self._relation_failed(now, "reader-error")
            if not self._relation_reader_warned:
                self._relation_reader_warned = True
                warning("NativeGameClock clock_relation_warning=" + type(exc).__name__)
            return False

    def _observation_snapshot(self, now):
        reader = getattr(self.backend, 'observation_clock_snapshot', None)
        ready = getattr(self.backend, '_observation_clock_ready', None)
        snapshot = reader() if callable(reader) else None
        return snapshot if callable(ready) and ready(snapshot, now) else None

    @staticmethod
    def _clock_key(snapshot):
        return snapshot.identity, snapshot.epoch, snapshot.trust_revision

    def _clock_observation_ready(self, now):
        try:
            snapshot = self._observation_snapshot(now)
            if snapshot is None:
                self._relation_failed(now, 'observation-unavailable')
                return False
            key = self._clock_key(snapshot)
            if self._observation_key is not None and self._observation_key != key:
                self._relation_failed(now, 'observation-epoch')
            self._observation_key = key
            provenance = self._provenance()
            if self._hard_provenance is not None and provenance != self._hard_provenance:
                self._clock_gap(now, 'clock-hard-fence')
            self._hard_provenance = provenance
            if self._relation_trusted is not True:
                self._event('clock-observation-trusted', now, epoch=snapshot.epoch,
                            trust_revision=snapshot.trust_revision)
            self._relation_trusted, self._relation_reason = True, None
            return True
        except Exception as exc:
            self._relation_failed(now, 'observation-reader-error')
            if not self._relation_reader_warned:
                self._relation_reader_warned = True
                warning('NativeGameClock clock_observation_warning=' + type(exc).__name__)
            return False

    @staticmethod
    def _capture_upper(metadata, observed):
        upper = metadata.get('first_done_observed_s')
        request = metadata['request_s']
        return upper if type(upper) in {int, float} and math.isfinite(upper) and request <= upper <= observed else observed

    def _frame_clock_view(self, metadata, observed, now):
        snapshot = self._observation_snapshot(now)
        if snapshot is None or self._clock_key(snapshot) != metadata.get('_clock_key'):
            return None
        return snapshot.frame_view(metadata['request_s'], self._capture_upper(metadata, observed),
            now_s=now, execution_offset_s=self.backend._device_clock_offset_s)

    def _match_frame(self, image, metadata, observed, anchor, *, wait=False):
        if not self._observation_required:
            return self.matcher.observe(image, metadata['request_s'], observed, anchor)
        while not self._stop.is_set():
            now = self.backend._clock()
            if now - observed > .5:
                return {'status': 'unknown', 'reason': 'clock-frame-expired-before-seal'}
            snapshot = self._observation_snapshot(now)
            if (snapshot is None or self._clock_key(snapshot) != metadata.get('_clock_key')
                    or self._provenance() != metadata.get('_intake_provenance')
                    or self._phase_watermark() != metadata.get('_intake_phase_watermark')):
                return {'status': 'unknown', 'reason': 'clock-frame-epoch-invalid'}
            view = self._frame_clock_view(metadata, observed, now)
            if view is not None:
                metadata['_clock_view'] = view
                provenance = self._provenance()
                watermark = self._phase_watermark()
                effect = self.backend.phase_effect_snapshot()
                if effect is not None and not self._effect_execution_ready(effect,
                        self.backend.phase_status_snapshot(), metadata['request_s'], now, view):
                    return {'status': 'unknown', 'reason': 'waiting-phase-effect-execution'}
                metadata['_sealed_provenance'] = provenance
                metadata['_phase_watermark'] = watermark
                if (getattr(self.matcher, 'projection', None) is None
                        and self._opening_effect_key is not None and self._opening_effect_key != watermark):
                    self.matcher.reset(full=True)
                self._opening_effect_key = watermark
                projection_before = getattr(self.matcher, 'projection', None)
                semantic_before = self._semantic_epoch
                stale = False
                matcher_error = None
                try:
                    result = self.matcher.observe(image, metadata['request_s'],
                        self._capture_upper(metadata, observed), anchor, view)
                except Exception as exc:
                    matcher_error = exc
                finally:
                    latest = self._observation_snapshot(self.backend._clock())
                    stale = (self._stop.is_set() or self._semantic_epoch != semantic_before
                             or self._provenance() != provenance or self._phase_watermark() != watermark)
                    if stale and projection_before is None:
                        reset = getattr(self.matcher, 'reset', None)
                        if callable(reset):
                            # 成功或异常返回都不能让旧在途第3帧迟提交 opening 校准。
                            reset(full=True)
                quality_changed = latest is None or self._clock_key(latest) != metadata.get('_clock_key')
                if matcher_error is not None and quality_changed and projection_before is None:
                    self.matcher.reset(full=True)
                if stale or matcher_error is not None and quality_changed:
                    return {'status': 'unknown', 'reason': 'clock-frame-epoch-invalid'}
                if matcher_error is not None:
                    raise matcher_error
                return result
            if (not wait or now - observed > .5
                    or snapshot.probes[-1].host_send_s >= self._capture_upper(metadata, observed)):
                return {'status': 'unknown', 'reason': 'clock-frame-uncovered'}
            # 只在已有后台 worker 等未来双侧 probe；不续原图 H 时间或 500ms TTL。
            self._stop.wait(.005)
        return {'status': 'unknown', 'reason': 'stopped'}

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
        if not visible:
            self.invalidate(now, "invalid-scene")
            return False
        # 先否定失信关系及旧 epoch；短复用帧也不能绕过检查或续租视觉独占。
        if not self._clock_relation_ready(now):
            return False
        stale_target = self.trusted_at is not None and now - self.trusted_at > .5
        gap = self.last_frame_s is not None and now - self.last_frame_s > .5
        if gap and not self._sampling_gap_invalidated:
            # 连续坏图只使同一个真实采样缺口失效一次；租约到期也不能被早返回绕过。
            if stale_target:
                self._event("stale-target", now)
            if self._observation_required:
                self._quality_gap(now, 'sampling-gap')
            else:
                self.invalidate(now, "sampling-gap")
            self._sampling_gap_invalidated = True
        elif stale_target and not valid:
            self.invalidate(now, "stale-target")
        if not valid:
            # 单张短复用或慢图不供证据，但不能销毁同局开场种子或仍有效的独立 worker 结果。
            self._event("slow-or-reused-frames", now)
            return False
        self.last_frame_s = now
        self._sampling_gap_invalidated = False
        return True

    def _effect_execution_ready(self, effect, latest, request_s, now, clock_view):
        due, chunk = effect.first_adjusted_due_s, effect.first_affected_chunk_sequence
        uncertainty = getattr(self.backend, '_clock_uncertainty_ms', None)
        if (due is None or chunk is None or uncertainty is None or not math.isfinite(uncertainty)
                or not 0 <= uncertainty <= 1):
            return False
        if self._observation_required:
            if not isinstance(clock_view, ObservationClockView):
                return False
            reader = getattr(self.backend, 'mapped_execution_commit_snapshot', None)
            rows = reader(now) if callable(reader) else ()
            for row in reversed(rows):
                if (row.clock_identity != clock_view.identity or row.observation_epoch != clock_view.epoch
                        or row.trust_revision != clock_view.trust_revision or row.host_upper_s > request_s
                        or row.execution.chunk_sequence < chunk or row.execution.actual_execution_s < due):
                    continue
                for action in row.execution.actions:
                    if (action.command == 'd' and action.kind in {'tap', 'flick'}
                            and round(action.planned_s * 1e9) >= round(due * 1e9)
                            and (effect.first_affected_action_token is None
                                 or action.action_token >= effect.first_affected_action_token)):
                        return True
            return False
        return (latest.transient_actual_s is not None and latest.transient_planned_s is not None
            and latest.transient_chunk_sequence is not None
            and latest.transient_actual_s + uncertainty / 1000 <= request_s and latest.transient_actual_s >= due
            and round(latest.transient_planned_s * 1e9) >= round(due * 1e9)
            and latest.transient_chunk_sequence >= chunk
            and (effect.first_affected_action_token is None or latest.transient_action_token is not None
                 and latest.transient_action_token >= effect.first_affected_action_token))

    def _effect_ready(self, now, request_s, clock_view=None):
        self._retain_owned_effect()
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
            if not self._effect_execution_ready(status, latest, request_s, now, clock_view):
                return False
        self._event("effect-observed" if status.state == "applied" else "request-terminal", now,
                    sequence=self.pending_sequence, state=status.state, terminal_reason=status.terminal_reason)
        self.pending_sequence = None
        self.applied_effect = None
        self.samples.clear()
        return True

    @staticmethod
    def _effect_epoch(effect):
        return (effect.run_id, effect.request_sequence, effect.cumulative_phase_ms,
                effect.first_adjusted_due_s, effect.first_affected_chunk_sequence,
                effect.first_affected_action_token)

    def _foreign_effect_ready(self, now, request_s, clock_view=None):
        reader = getattr(self.backend, "phase_effect_snapshot", None)
        effect = reader() if callable(reader) else None
        if effect is not None and effect.run_id == self.run_id and effect.owner != "game-clock":
            epoch = self._effect_epoch(effect)
            if epoch != self._foreign_effect_epoch:
                self._foreign_effect_epoch = epoch
                self._foreign_effect_pending = effect
                self.samples.clear()
                self.trusted_at = self.trusted_lag_ms = None
                # 外来合法请求保留原槽位与 TTL；只撤下已过期的视觉观测声明。
                self.backend.set_visual_phase_guard(False)
                self._event("foreign-effect-changed", now, sequence=effect.request_sequence)
        pending = self._foreign_effect_pending
        if pending is None:
            return True
        latest = self.backend.phase_status_snapshot()
        # 复用本方效果的执行门禁，不能用 apply 回执代替受影响输入与新截图。
        if not self._effect_execution_ready(pending, latest, request_s, now, clock_view):
            self.samples.clear()
            self._event("waiting-foreign-effect", now, sequence=pending.request_sequence)
            return False
        self._foreign_effect_pending = None
        self.samples.clear()
        self._event("foreign-effect-observed", now, sequence=pending.request_sequence)
        return True

    def _accept(self, result, metadata, observed_s, now):
        clock_view = metadata.get('_clock_view')
        if self._observation_required:
            snapshot = self._observation_snapshot(now)
            if (snapshot is None or not isinstance(clock_view, ObservationClockView)
                    or (clock_view.identity, clock_view.epoch, clock_view.trust_revision) != self._clock_key(snapshot)):
                self._clock_gap(now, 'clock-frame-epoch-invalid')
                return
            if (metadata.get('_sealed_provenance') != self._provenance()
                    or metadata.get('_phase_watermark') != self._phase_watermark()):
                self.invalidate(now, 'phase-frame-context-changed')
                return
        reason = result.get("reason", "unknown-match")
        if reason.startswith("observer-error"):
            self.close(now, reason)
            warning("NativeGameClock disabled=true reason=" + reason)
            return
        if (self._observation_required and result.get('status') == 'unknown'
                and reason == 'opening-needs-multiple-frames' and not self.closed
                and not self._stop.is_set() and 0 <= now - observed_s <= .5):
            # 已封存上下文仍有效的旧warming只表示种子未齐；不能撤销正在计算的新种子。
            # 不读取matcher当前projection，后台可能已完成第三帧；此处不授资格或续请求。
            self._event(reason, now)
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
        if not self._foreign_effect_ready(now, metadata["request_s"], clock_view):
            return
        pending_unapplied = (self._observation_required and self.pending_sequence is not None
            and status.owner == 'game-clock' and status.request_sequence == self.pending_sequence
            and status.state in {'requested','waiting-touch-boundary','waiting-execution-receipts','waiting-gap'})
        if not pending_unapplied and not self._effect_ready(now, metadata["request_s"], clock_view):
            if self.trusted_lag_ms is None or abs(lag - self.trusted_lag_ms) > 20:
                self.invalidate(now, "owned-target-contradicted")
                return
            self.trusted_at = observed_s
            self._event("waiting-owned-effect", now)
            return
        if self._needs_visual_rebase:
            # 旧消费者保留原重建策略；共享额度没有可信参考桥时保债，不吸收真实应用。
            # 此入口仍位于执行门禁后，不能把未执行欠账误作已完成。
            if self._shared_budget is not None and self._shared_budget.reference is not None:
                self._shared_budget.unresolved_reason = 'unbridged-game-reference'
                self.backend.set_visual_phase_guard(False)
                self._event('shared-reference-unbridged', now)
                return
            if self._shared_budget is None:
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
        if self._shared_budget is not None:
            self._accept_shared_budget(status, lag, (lower, upper), metadata, observed_s, now)
            return
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
            self.backend.set_visual_phase_guard(False)
            self._event("passive-target", now, lag_ms=lag, owned_phase_ms=current)
            return
        if status.state in {"requested", "waiting-touch-boundary", "waiting-execution-receipts", "waiting-gap"}:
            # 已有外来合法槽位时继续被动观察，不能反复 claim/release 清空判定反馈票。
            self.backend.set_visual_phase_guard(False)
            self._event("visual-target-busy", now)
            return
        delta = 15.0 if error > 0 else -15.0
        self.backend.set_visual_phase_guard(True)
        if self.backend.request_future_phase(delta, now, owner="game-clock"):
            self.pending_sequence = self.backend.phase_status_snapshot().request_sequence
            self.samples.clear()
            self._event("requested", now, sequence=self.pending_sequence, delta_ms=delta)
        else:
            self.backend.set_visual_phase_guard(False)
            self.samples.clear()
            self._event("visual-request-rejected", now)

    def _shared_reference(self):
        projection = getattr(self.matcher, 'projection', None)
        if projection is None:
            return None
        return (self.run_id, id(projection), self.backend.first_action_anchor_s, self.backend._device_clock_offset_s)

    def _accept_shared_budget(self, status, lag, bounds, metadata, observed_s, now):
        budget = self._shared_budget
        reference = self._shared_reference()
        if reference is None or not budget.reference_ready(reference):
            self.backend.set_visual_phase_guard(False)
            self._event('shared-reference-unbridged', now)
            return
        try:
            history = self.backend.phase_effect_history_snapshot()
            if not isinstance(history, tuple) or len(history) > 64:
                raise RuntimeError('phase effect history unavailable')
            keys = set()
            for effect in history:
                key = effect.owner, effect.request_sequence
                if not budget.effect_valid(effect) or key in keys:
                    raise ValueError('invalid effect history')
                keys.add(key)
            foreign = sorted((effect for effect in history if effect.owner == 'feedback'),
                             key=lambda effect: effect.request_sequence)
            for effect in foreign:
                if (effect.request_sequence <= budget.feedback_sequence
                        and not budget.observe_executed_feedback(effect, execution_confirmed=False,
                                                                before_after_confirmed=False)):
                    raise ValueError('conflicting effect history')
            feedback_total = self.backend.phase_owner_cumulative('feedback')
            visual_total = self.backend.phase_owner_cumulative('game-clock')
            current = self.visual_applied_ms = visual_total - self.visual_origin_ms
            neutral = abs(lag) < 15 and bounds[0] <= 0 <= bounds[1]
            pending = (self.pending_sequence is not None and status.owner == 'game-clock'
                and status.request_sequence == self.pending_sequence
                and status.state in {'requested','waiting-touch-boundary','waiting-execution-receipts','waiting-gap'})
            if neutral and abs(current) < 1e-6 and not pending:
                was_active = budget.active
                budget.neutral_baseline(feedback_total, max((effect.request_sequence for effect in foreign), default=0))
                self._shared_before_raw = (reference, observed_s)
                self.baseline_phase_ms = None
                self.trusted_at, self.trusted_lag_ms = observed_s, lag
                self.backend.set_visual_phase_guard(False)
                self._event('visual-episode-finished' if was_active else 'neutral-game-clock', now,
                            retained_feedback_ms=feedback_total)
                return
            budget.active = True
            # 临时 reader 异常撤销本次资格；有效账本恢复后仍可重新验证，不永久封死观察器。
            if (budget.unresolved_reason or '').startswith('effect-history:'):
                budget.unresolved_reason = None
            for effect in foreign:
                if effect.request_sequence <= budget.feedback_sequence:
                    if not budget.observe_executed_feedback(effect, execution_confirmed=False,
                                                           before_after_confirmed=False):
                        break
                    continue
                before = self._shared_before_raw
                before_after = (before is not None and before[0] == reference
                    and effect.applied_host_s is not None and before[1] <= effect.applied_host_s)
                confirmed = self._effect_execution_ready(effect, self.backend.phase_status_snapshot(),
                    metadata['request_s'], now, metadata.get('_clock_view'))
                if not budget.observe_executed_feedback(effect, execution_confirmed=confirmed,
                                                       before_after_confirmed=before_after):
                    break
            if abs(budget.feedback_total - feedback_total) > 1e-6 and budget.unresolved_reason is None:
                budget.unresolved_reason = 'foreign-effect-history-gap'
            remaining = budget.remaining(lag, bounds, current)
            if remaining is None:
                self.backend.set_visual_phase_guard(False)
                self._event('shared-budget-unresolved', now, detail=budget.unresolved_reason)
                return
        except Exception as exc:
            budget.unresolved_reason = 'effect-history:' + type(exc).__name__
            self.backend.set_visual_phase_guard(False)
            self._event('shared-budget-unresolved', now, detail=budget.unresolved_reason)
            return
        self._shared_before_raw = (reference, observed_s)
        error, (lower, upper) = remaining
        target = status.cumulative_phase_ms + error
        self.trusted_at, self.trusted_lag_ms = observed_s, lag
        if self.baseline_phase_ms is None:
            self.baseline_phase_ms = status.cumulative_phase_ms - current
        self._event('trusted-target', now, target_ms=target, lag_ms=lag,
                    cumulative_phase_ms=status.cumulative_phase_ms, shared_feedback_credit_ms=budget.feedback_credit)
        snapshot = self._observation_snapshot(now)
        view = metadata.get('_clock_view')
        if (snapshot is None or view is None or self._clock_key(snapshot) !=
                (view.identity, view.epoch, view.trust_revision)):
            self._clock_gap(now, 'clock-frame-epoch-invalid')
            return
        if (metadata.get('_sealed_provenance') != self._provenance()
                or metadata.get('_phase_watermark') != self._phase_watermark()):
            self.invalidate(now, 'phase-frame-context-changed')
            return
        deadline = min(row[0] + .5 for row in self.samples)
        qualification = (deadline, self._clock_key(snapshot),
                         self._provenance(), self._phase_watermark())
        if pending:
            delta = status.delta_ms
            supported = ((delta > 0 and error >= delta and lower >= delta)
                         or (delta < 0 and error <= delta and upper <= delta))
            opposite = ((delta > 0 and error <= -15 and upper <= -15)
                        or (delta < 0 and error >= 15 and lower >= 15))
            if opposite:
                self.backend.revoke_future_phase('visual-intent-contradicted', owner='game-clock',
                                                 expected_sequence=self.pending_sequence)
                self._retain_owned_effect(confirm_terminal=True)
                self.samples.clear()
            refreshed = (not opposite and deadline >= now and supported
                and self.backend.refresh_visual_phase_qualification(self.pending_sequence, qualification))
            if not refreshed and not opposite:
                self.backend.refresh_visual_phase_qualification(self.pending_sequence, None)
            self.backend.set_visual_phase_guard(bool(refreshed))
            self._event('visual-intent-revalidated' if refreshed else 'visual-intent-waiting-evidence', now)
            return
        if abs(error) < 15 or (error > 0 and lower < 15) or (error < 0 and upper > -15):
            self.backend.set_visual_phase_guard(False)
            self._event('passive-target', now, lag_ms=lag, owned_phase_ms=current,
                        remaining_bounds_ms=(lower, upper))
            return
        if deadline < now:
            self.backend.set_visual_phase_guard(False)
            self._event('visual-proof-expired', now)
            return
        latest = self.backend.phase_status_snapshot()
        if latest.state in {'requested','waiting-touch-boundary','waiting-execution-receipts','waiting-gap'}:
            self.backend.set_visual_phase_guard(False)
            self._event('visual-target-busy', now)
            return
        delta = 15. if error > 0 else -15.
        self.backend.set_visual_phase_guard(True)
        if self.backend.request_future_phase(delta, observed_s, owner='game-clock',
                expected_owner_totals=(feedback_total, visual_total), visual_qualification=qualification):
            self.pending_sequence = self.backend.phase_status_snapshot().request_sequence
            self.samples.clear()
            self._event('requested', now, sequence=self.pending_sequence, delta_ms=delta)
        else:
            self.backend.set_visual_phase_guard(False)
            self.samples.clear()
            self._event('visual-request-rejected', now)

    def observe(self, image, metadata, now, *, visible):
        """同步纯消费者 seam，仅供 worker 结果测试及离线 replay 使用。"""
        if not self._fresh(metadata, now, visible):
            return
        try:
            self._reset_matcher_if_due()
            source = dict(metadata)
            if self._observation_required:
                source['_clock_key'] = self._observation_key
                source['_intake_provenance'] = self._provenance()
                source['_intake_phase_watermark'] = self._phase_watermark()
            result = self._match_frame(image, source, now, self.backend.first_action_anchor_s)
            self._accept(result, source, now, max(now, self.backend._clock()))
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
                result = self._match_frame(image, metadata, observed, anchor, wait=True)
            except Exception as exc:
                result = {"status": "unknown", "reason": "observer-error:" + type(exc).__name__}
            if epoch != self.epoch or self._stop.is_set():
                continue
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
        source = dict(metadata)
        if self._observation_required:
            source['_clock_key'] = self._observation_key
            source['_intake_provenance'] = self._provenance()
            source['_intake_phase_watermark'] = self._phase_watermark()
        self._replace_latest(self._frames, (self.epoch, owned_image, source, now,
                                          self.backend.first_action_anchor_s))

    def close(self, now, reason="terminal"):
        self.closed = True
        self._stop.set()
        self.invalidate(now, reason)

    def report(self):
        return {"model": "relative-game-clock-v2-clock-interval" if self._observation_required else "relative-game-clock-v1", "run_id": self.run_id,
                "closed": self.closed, "owned_applied_ms": self.visual_applied_ms,
                "owned_origin_ms": self.visual_origin_ms,
                "episode_base_ms": self.baseline_phase_ms, "pending_sequence": self.pending_sequence,
                "summary": dict(self.summary), "events": list(self.events), "capacity": 128,
                "clock_relation": {"required": self._relation_required, "trusted": self._relation_trusted,
                    "source": "independent-clock-rpc" if self._observation_required else "jlog-negative-guard",
                    "baseline_delta_ms": self._relation_baseline_ms, "reason": self._relation_reason,
                    "latest_delta_ms": self._relation_latest_delta_ms,
                    "last_cursor": self._relation_last_cursor, "recovery_after_cursor": self._relation_recovery_after},
                "shared_budget": self._shared_budget.report() if self._shared_budget is not None else None,
                "matcher": self.matcher.report() if hasattr(self.matcher, "report") else {}}
