from dataclasses import replace
import socket
import threading
import time

import pytest

from agent.realtime.native_clock_rpc import (
    QUERY, STOP, WIRE, ClockRpcClient, ClockWireError, WireIdentity, request_bytes, response_probe,
)
from agent.realtime.native_observation_clock import ClockIdentity, ObservationClockMapper


IDENTITY = WireIdentity(ClockIdentity("rpc-run", 23, 42, 100), bytes(range(16)))


def response(request, *, device_ns=100_000_000_000, processing_ns=0):
    fields = list(WIRE.unpack(request))
    fields[2] |= 0x8000
    fields[12:16] = (device_ns, device_ns + processing_ns, device_ns - 1_000_000_000,
                     device_ns - 1_000_000_000 + processing_ns)
    return WIRE.pack(*fields)


@pytest.mark.parametrize("field,value", [(0, 0), (1, 2), (2, QUERY), (3, 127), (4, 43),
    (5, 2), (6, 24), (7, bytes(16)), (8, 1), (9, 0), (10, 1), (11, 1), (16, b"x" * 32)])
def test_real_wire_protocol_rejects_every_identity_and_reserved_mismatch(field, value):
    raw = list(WIRE.unpack(response(request_bytes(IDENTITY, 1))))
    raw[field] = value
    with pytest.raises(ClockWireError):
        response_probe(WIRE.pack(*raw), IDENTITY, 1, QUERY, 200_000_000_000, 200_000_800_000)


def test_large_valid_rtt_does_not_close_stream_and_recovers_three_new_samples():
    client_socket, server_socket = socket.socketpair()
    times = []
    specs = []
    for index in range(1, 10):
        host_ns = 200_000_000_000 + index * 50_000_000
        rtt_ns = 1_200_000_000 if index == 5 else 800_000
        if index > 5:
            host_ns += 1_200_000_000
        times.extend((host_ns, host_ns + rtt_ns))
        specs.append(host_ns + rtt_ns // 2 - 100_000_000_000)
    clock_values = iter(times)
    client = ClockRpcClient(client_socket, IDENTITY, clock_ns=lambda: next(clock_values))
    seen = []
    def serve():
        try:
            for device_ns in specs:
                data = bytearray()
                while len(data) < 128:
                    data.extend(server_socket.recv(128 - len(data)))
                seen.append(WIRE.unpack(data))
                raw = response(data, device_ns=device_ns)
                # 真实 socket 碎片不能使单请求预算被重置，也不能误认为半包成功。
                server_socket.sendall(raw[:17])
                server_socket.sendall(raw[17:])
        finally:
            server_socket.close()
    thread = threading.Thread(target=serve)
    thread.start()
    mapper = ObservationClockMapper(IDENTITY.clock)
    for index in range(1, 10):
        sample = client.query()
        accepted = mapper.observe(sample)
        if index == 5:
            assert not accepted and not client.closed
            assert mapper.snapshot().reason == "unqualified-probe"
        if 5 < index < 8:
            assert mapper.snapshot().reason == "warming"
    assert mapper.snapshot().reason is None
    assert [row[5] for row in seen] == list(range(1, 10))
    assert all(row[7] == IDENTITY.auth_nonce for row in seen)
    assert mapper.snapshot().epoch == 1
    assert not client.authenticated_stop
    client.close()
    thread.join(1)
    assert not thread.is_alive()


@pytest.mark.parametrize("mode", ["partial-eof", "bad-identity", "stop"])
def test_stream_failure_or_authenticated_stop_reports_actual_terminal_mode(mode):
    left, right = socket.socketpair()
    client = ClockRpcClient(left, IDENTITY)
    def serve():
        try:
            request = right.recv(128)
            raw = response(request)
            if mode == "partial-eof":
                right.sendall(raw[:30])
            elif mode == "bad-identity":
                values = list(WIRE.unpack(raw))
                values[4] = 999
                right.sendall(WIRE.pack(*values))
            else:
                right.sendall(raw)
        finally:
            right.close()
    thread = threading.Thread(target=serve)
    thread.start()
    if mode == "stop":
        client.query(STOP)
        assert client.authenticated_stop
    else:
        with pytest.raises(ClockWireError):
            client.query()
        assert not client.authenticated_stop
    assert client.closed
    thread.join(1)
    assert not thread.is_alive()


def test_bad_nonce_is_never_exposed_by_repr_or_error():
    assert IDENTITY.auth_nonce.hex() not in repr(IDENTITY)
    with pytest.raises(ValueError, match="invalid clock wire identity"):
        replace(IDENTITY, auth_nonce=b"private")


def test_true_wire_deadline_closes_stream_instead_of_reusing_late_reply(monkeypatch):
    from agent.realtime import native_clock_rpc
    monkeypatch.setattr(native_clock_rpc, "WIRE_TIMEOUT_S", .020)
    left, right = socket.socketpair()
    client = ClockRpcClient(left, IDENTITY)
    before = time.perf_counter()
    with pytest.raises(ClockWireError):
        client.query()
    assert time.perf_counter() - before < .2 and client.closed
    assert not client.authenticated_stop
    right.close()
