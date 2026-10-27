"""独立时钟 RPC；wire 成功与时钟样本可用于相位是两个不同的资格。"""

from dataclasses import dataclass, field
import secrets
import socket
import struct
import threading
import time

from .native_observation_clock import ClockIdentity, ObservationProbe


MAGIC, VERSION, SIZE = 0x4D424443, 1, 128
QUERY, STOP = 1, 2
WIRE = struct.Struct("!IHHIIQQ16sIIIIQQQQ32s")
WIRE_TIMEOUT_S = 2.0


class ClockWireError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class WireIdentity:
    clock: ClockIdentity
    auth_nonce: bytes = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.clock, ClockIdentity) or type(self.auth_nonce) is not bytes or len(self.auth_nonce) != 16:
            raise ValueError("invalid clock wire identity")


def request_bytes(identity, sequence, kind=QUERY):
    if type(sequence) is not int or not 0 < sequence < 2**64 or kind not in {QUERY, STOP}:
        raise ClockWireError("invalid clock request")
    clock = identity.clock
    return WIRE.pack(MAGIC, VERSION, kind, SIZE, clock.pid, sequence, clock.generation,
        identity.auth_nonce, clock.realtime_id, clock.monotonic_id, 0, 0, 0, 0, 0, 0, bytes(32))


def response_probe(data, identity, sequence, kind, host_send_ns, host_receive_ns):
    if len(data) != SIZE:
        raise ClockWireError("clock response size mismatch")
    (magic, version, response_kind, size, pid, response_seq, generation, nonce,
     realtime_id, monotonic_id, status, reserved, rx_rt, tx_rt, rx_mono, tx_mono, tail) = WIRE.unpack(data)
    clock = identity.clock
    if ((magic, version, response_kind, size, pid, response_seq, generation,
         realtime_id, monotonic_id, status, reserved) !=
        (MAGIC, VERSION, kind | 0x8000, SIZE, clock.pid, sequence, clock.generation,
         clock.realtime_id, clock.monotonic_id, 0, 0)
            or not secrets.compare_digest(nonce, identity.auth_nonce) or tail != bytes(32)):
        raise ClockWireError("clock response protocol mismatch")
    # 不在 transport 层把大 RTT 当断线；合法 wire 仍送 mapper 切 epoch 并等待恢复。
    # 资格仍严格受 mapper 的 1ms/20ms/时钟连续性门限约束，2s 只限制后台接收预算。
    return ObservationProbe(clock, sequence, host_send_ns / 1e9, host_receive_ns / 1e9,
        rx_rt / 1e9, tx_rt / 1e9, rx_mono / 1e9, tx_mono / 1e9)


class ClockRpcClient:
    """只能由单后台线程 query；request_stop 可在调用方立即关闭自己的 FD。"""

    def __init__(self, connection, identity, *, clock_ns=time.perf_counter_ns):
        self.connection, self.identity, self.clock_ns = connection, identity, clock_ns
        self.sequence = 0
        self.closed = False
        self.authenticated_stop = False
        self._request_lock = threading.Lock()

    def query(self, kind=QUERY):
        if not self._request_lock.acquire(False):
            raise ClockWireError("clock query already in flight")
        try:
            if self.closed:
                raise ClockWireError("clock stream closed")
            self.sequence += 1
            request = request_bytes(self.identity, self.sequence, kind)
            deadline = time.monotonic() + WIRE_TIMEOUT_S
            self.connection.settimeout(WIRE_TIMEOUT_S)
            sent_ns = self.clock_ns()
            self.connection.sendall(request)
            data = bytearray()
            while len(data) < SIZE:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ClockWireError("clock response deadline exceeded")
                self.connection.settimeout(remaining)
                part = self.connection.recv(SIZE - len(data))
                if not part:
                    raise ClockWireError("clock response EOF")
                data.extend(part)
            received_ns = self.clock_ns()
            sample = response_probe(bytes(data), self.identity, self.sequence, kind, sent_ns, received_ns)
            if kind == STOP:
                self.authenticated_stop = True
                self.close()
            return sample
        except (OSError, ClockWireError, OverflowError, struct.error) as exc:
            self.close()
            raise ClockWireError("clock transport stopped: " + type(exc).__name__) from exc
        finally:
            self._request_lock.release()

    def close(self):
        self.closed = True
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.connection.close()
