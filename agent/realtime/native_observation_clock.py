"""旁路设备时钟的有界观察视图；绝不改写 Native 执行坐标或调度器。"""

from dataclasses import dataclass
import math


JLOG_DEVICE_UNCERTAINTY_S = .000002


def device_time_enclosure(device_s, uncertainty_s):
    if uncertainty_s == 0:
        return device_s, device_s
    # jlog 的 %.3f ms 与 epoch double 都不是纳秒实读；按已核源保守扩至 ±2µs。
    # 向外取相邻 float，避免大 epoch 上减/加后向内舍入缩窄 enclosure。
    return (math.nextafter(device_s - uncertainty_s, float("-inf")),
            math.nextafter(device_s + uncertainty_s, float("inf")))


@dataclass(frozen=True, slots=True)
class ClockIdentity:
    run_id: str
    generation: int
    pid: int
    start_ticks: int
    realtime_id: int = 0
    monotonic_id: int = 1

    def __post_init__(self):
        if (type(self.run_id) is not str or not 1 <= len(self.run_id) <= 128
                or any(ord(value) < 32 for value in self.run_id)
                or type(self.generation) is not int or not 0 < self.generation < 2**64
                or type(self.pid) is not int or not 0 < self.pid < 2**32
                or type(self.start_ticks) is not int or not 0 < self.start_ticks < 2**64
                or type(self.realtime_id) is not int or self.realtime_id != 0
                or type(self.monotonic_id) is not int or self.monotonic_id != 1):
            raise ValueError("invalid observation clock identity")


@dataclass(frozen=True, slots=True)
class ObservationProbe:
    identity: ClockIdentity
    sequence: int
    host_send_s: float
    host_receive_s: float
    device_receive_s: float
    device_send_s: float
    monotonic_receive_s: float
    monotonic_send_s: float
    qualified_source: bool = True

    @property
    def offset_bounds(self):
        return (math.nextafter(self.host_send_s - self.device_receive_s, float("-inf")),
                math.nextafter(self.host_receive_s - self.device_send_s, float("inf")))


@dataclass(frozen=True, slots=True)
class ObservationClockView:
    identity: ClockIdentity
    epoch: int
    left_sequence: int
    right_sequence: int
    offset_lower_s: float
    offset_upper_s: float
    execution_offset_s: float
    covered_host_lower_s: float
    covered_host_upper_s: float
    covered_device_lower_s: float
    covered_device_upper_s: float
    event_device_s: float | None = None
    device_uncertainty_s: float = 0.0
    trust_revision: int = 0

    @property
    def uncertainty_ms(self):
        event_width = 0.0
        if self.event_device_s is not None:
            lower, upper = device_time_enclosure(self.event_device_s, self.device_uncertainty_s)
            event_width = upper - lower
        return (self.offset_upper_s - self.offset_lower_s + event_width) * 500

    @property
    def delta_bounds(self):
        return (self.offset_lower_s - self.execution_offset_s,
                self.offset_upper_s - self.execution_offset_s)

    def device_to_host(self, device_s):
        if self.event_device_s is not None and device_s != self.event_device_s:
            return None
        lower, upper = device_time_enclosure(device_s, self.device_uncertainty_s)
        if not self.covered_device_lower_s <= lower <= upper <= self.covered_device_upper_s:
            return None
        return (math.nextafter(lower + self.offset_lower_s, float("-inf")),
                math.nextafter(upper + self.offset_upper_s, float("inf")))

    def host_to_execution(self, lower_s, upper_s):
        if not self.covered_host_lower_s <= lower_s <= upper_s <= self.covered_host_upper_s:
            return None
        lo, hi = self.delta_bounds
        return lower_s - hi, upper_s - lo


@dataclass(frozen=True, slots=True)
class ObservationClockSnapshot:
    identity: ClockIdentity
    epoch: int = 0
    probes: tuple[ObservationProbe, ...] = ()
    reason: str | None = "warming"
    recovery_sequence: int | None = None
    maximum_bracket_s: float = .150
    maximum_age_s: float = .500
    rate_bound: float = .0002
    trust_revision: int = 0

    def _view(self, rows, execution_offset_s, now_s, *, event_device_s=None, device_uncertainty_s=0.0):
        # 端点间假设时钟速率偏差有界，且不存在未观测的跳变后立即跳回。
        # 区间保留双侧路径不对称及速率包络，绝不把 midpoint 当精确映射。
        left, right = rows[0], rows[-1]
        span = right.host_send_s - left.host_receive_s
        if (not 0 <= span <= self.maximum_bracket_s
                or not 0 <= now_s - right.host_receive_s <= self.maximum_age_s):
            return None
        padding = span * self.rate_bound
        lo = math.nextafter(min(row.offset_bounds[0] for row in rows) - padding, float("-inf"))
        hi = math.nextafter(max(row.offset_bounds[1] for row in rows) + padding, float("inf"))
        view = ObservationClockView(self.identity, self.epoch, left.sequence, right.sequence,
            lo, hi, execution_offset_s, left.host_receive_s, right.host_send_s,
            left.device_send_s, right.device_receive_s, event_device_s, device_uncertainty_s, self.trust_revision)
        return view if view.uncertainty_ms <= 1 else None

    def _available(self, now_s, execution_offset_s):
        return (self.reason is None and self.recovery_sequence is not None and self.probes
            and type(now_s) in {int, float} and math.isfinite(now_s)
            and type(execution_offset_s) in {int, float} and math.isfinite(execution_offset_s)
            and 0 <= now_s - self.probes[-1].host_receive_s <= self.maximum_age_s)

    def frame_view(self, lower_s, upper_s, *, now_s, execution_offset_s):
        if not self._available(now_s, execution_offset_s) or not lower_s <= upper_s:
            return None
        # 截图采集窗可跨多个 20Hz 采样段；使用最紧双侧 bracket，并保留全部中间点。
        left_index = next((index for index in range(len(self.probes) - 1, -1, -1)
                           if self.probes[index].host_receive_s <= lower_s
                           and self.probes[index].sequence >= self.recovery_sequence), None)
        if left_index is not None:
            for right_index in range(left_index + 1, len(self.probes)):
                if upper_s <= self.probes[right_index].host_send_s:
                    return self._view(self.probes[left_index:right_index + 1], execution_offset_s, now_s)
        return None

    def commit_view(self, device_s, *, now_s, execution_offset_s, device_uncertainty_s=0.0):
        if (not self._available(now_s, execution_offset_s) or not math.isfinite(device_s)
                or type(device_uncertainty_s) not in {int, float} or not math.isfinite(device_uncertainty_s)
                or not 0 <= device_uncertainty_s <= .001):
            return None
        lower, upper = device_time_enclosure(device_s, device_uncertainty_s)
        for left, right in zip(self.probes, self.probes[1:]):
            if (left.sequence >= self.recovery_sequence and left.device_send_s <= lower
                    and upper <= right.device_receive_s):
                return self._view((left, right), execution_offset_s, now_s,
                    event_device_s=device_s, device_uncertainty_s=device_uncertainty_s)
        return None


class ObservationClockMapper:
    """单后台 writer 发布不可变快照；消费者读取不取锁、不外推、不追溯跨 epoch。"""

    def __init__(self, identity, *, capacity=64):
        if not 4 <= capacity <= 256:
            raise ValueError("invalid observation history capacity")
        self.identity, self.capacity = identity, capacity
        self._snapshot = ObservationClockSnapshot(identity)
        self._last_sequence = 0

    def snapshot(self):
        return self._snapshot

    def invalidate(self, reason):
        previous = self._snapshot
        self._snapshot = ObservationClockSnapshot(self.identity, previous.epoch + 1, reason=reason)

    @staticmethod
    def _continuous(previous, probe, lo, hi):
        last = previous.probes[-1]
        gap = probe.host_send_s - last.host_receive_s
        allowance = max(0, gap) * previous.rate_bound
        before_lo, before_hi = last.offset_bounds
        rt_step = probe.device_receive_s - last.device_send_s
        mono_step = probe.monotonic_receive_s - last.monotonic_send_s
        return (0 <= gap <= previous.maximum_bracket_s and rt_step >= 0 and mono_step >= 0
                and abs(rt_step - mono_step) <= .0001
                and lo <= before_hi + allowance and hi >= before_lo - allowance)

    def observe(self, probe):
        previous = self._snapshot
        if (probe.identity != self.identity or type(probe.sequence) is not int
                or not self._last_sequence < probe.sequence < 2**64):
            self.invalidate("identity-or-sequence")
            return False
        self._last_sequence = probe.sequence
        values = (probe.host_send_s, probe.host_receive_s, probe.device_receive_s, probe.device_send_s,
                  probe.monotonic_receive_s, probe.monotonic_send_s)
        if any(type(value) not in {int, float} or not math.isfinite(value) for value in values):
            self.invalidate("nonfinite-probe")
            return False
        rtt = probe.host_receive_s - probe.host_send_s
        processing = probe.device_send_s - probe.device_receive_s
        monotonic_processing = probe.monotonic_send_s - probe.monotonic_receive_s
        lo, hi = probe.offset_bounds
        if (probe.qualified_source is not True or not 0 <= rtt <= .020 or not 0 <= processing <= min(rtt, .002)
                or not 0 <= monotonic_processing <= min(rtt, .002)
                or abs(processing - monotonic_processing) > .0001):
            self.invalidate("unqualified-probe")
            return False
        rows = previous.probes
        continuous = bool(rows) and self._continuous(previous, probe, lo, hi)
        width = hi - lo
        if not 0 <= width <= .002:
            if (math.isfinite(width) and width > .002 and previous.reason is None
                    and previous.recovery_sequence is not None and len(rows) >= 3 and continuous
                    and 0 <= probe.host_receive_s - rows[-1].host_receive_s <= previous.maximum_age_s):
                # 孤立宽点只丢弃自身，序号仍消费；不入映射、不刷新lastgood或可信年龄。
                # 连续宽点及下个好点仍相对原lastgood检查，不能借丢弃点链式续租。
                return False
            self.invalidate("unqualified-probe")
            return False
        if rows and not continuous:
            self.invalidate("clock-discontinuity")
            previous = self._snapshot
            rows = ()
        rows = (*rows, probe)[-self.capacity:]
        recovery = previous.recovery_sequence
        if recovery is None and len(rows) >= 3:
            # 恢复前的输入/截图不因之后拿到好点而重新获得资格。
            recovery = rows[-1].sequence
        self._snapshot = ObservationClockSnapshot(self.identity, previous.epoch, rows,
            None if recovery is not None else "warming", recovery)
        return True
