import socket
import threading
import time
from types import SimpleNamespace

import pytest

from agent.realtime import native_engine
from agent.realtime.native_minitouch import NativeMinitouchDevice
from test_native_clock_domains import fixture


def test_native_receive_timestamp_uses_perf_counter_domain_on_real_loopback():
    client = native_engine.minitouch_client()
    assert callable(getattr(client, 'receive_with_timestamp', None))
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        assert client.connect('127.0.0.1', listener.getsockname()[1])
        with listener.accept()[0] as peer:
            peer.sendall(b'first\n')
            lower=time.perf_counter()
            text,returned=client.receive_with_timestamp(1024,500)
            upper=time.perf_counter()
            assert text=='first\n' and lower<=returned<=upper
            empty_lower=time.perf_counter()
            empty,timeout_return=client.receive_with_timestamp(1024,20)
            assert empty=='' and empty_lower<=timeout_return<=time.perf_counter() and client.connected
            peer.sendall(b'legacy\n')
            assert client.receive(1024,500)=='legacy\n'
            peer.shutdown(socket.SHUT_WR)
            text,returned=client.receive_with_timestamp(1024,500)
            assert text=='' and returned is not None
        client.close()
        assert client.receive_with_timestamp(1024,500)==('',None)


def test_delayed_python_delivery_keeps_transport_line_and_owner_times_distinct():
    device=NativeMinitouchDevice('unused','unused')
    returned=time.perf_counter()
    deliver=threading.Event()
    class Client:
        connected=True
        def receive_with_timestamp(self,*args):
            assert deliver.wait(2)
            self.connected=False
            return 'one\ntwo\n',returned
    client=Client()
    device._client=client
    device._closed=False
    thread=threading.Thread(target=device._read_logs,args=(client,))
    thread.start()
    time.sleep(.02)
    deliver.set()
    thread.join(1)
    assert device.log_records_since(0)[0]==2
    accepted=time.perf_counter()
    device.note_log_owner_accept(2,accepted,action_token=7)
    rows=device.receipt_timing_diagnostics['rows']
    assert [row['log_cursor'] for row in rows]==[1,2]
    assert rows[1]['transport_return_s']==returned<rows[1]['line_record_s']<=accepted
    assert rows[1]['owner_accept_s']==accepted and rows[1]['action_token']==7
    assert not device.reset_executed


def test_owner_accept_is_marked_only_after_real_session_acceptance(tmp_path,monkeypatch):
    backend,_,now,_,commit,accepted,_=fixture(tmp_path,monkeypatch)
    marks=[]
    backend._device.note_log_owner_accept=lambda *args,**kwargs:marks.append((args,kwargs,len(accepted)))
    commit(200.4254)
    assert len(marks)==1 and marks[0][0][0]==1 and marks[0][2]==1
    assert marks[0][0][1]==now[0]


def test_legacy_reader_has_explicit_missing_transport_and_bounded_diagnostics():
    device=NativeMinitouchDevice('unused','unused')
    class Client:
        connected=True
        def receive(self,*args):
            self.connected=False
            return 'legacy\n'
    client=Client();device._client=client;device._closed=False
    device._read_logs(client)
    assert device.receipt_timing_diagnostics['rows'][0]['transport_return_s'] is None
    for _ in range(6000):
        device._record_log_line('ordinary')
    report=device.receipt_timing_diagnostics
    assert len(report['rows'])==report['capacity']==4608 and report['dropped']==1393
    assert report['rows'][3778]['log_cursor']==3779
    assert report['rows'][4095]['log_cursor']==4096
    assert report['rows'][4096]['log_cursor']==5490 and report['rows'][-1]['log_cursor']==6001
    device.note_log_owner_accept(99999,time.perf_counter(),action_token=1)
    assert not device.reset_executed


def test_native_timestamp_precedes_python_delivery_when_gil_is_held():
    import ctypes
    import sys
    client=native_engine.minitouch_client()
    result={}
    entered=threading.Event()
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0));listener.listen(1)
        assert client.connect('127.0.0.1',listener.getsockname()[1])
        with listener.accept()[0] as peer:
            def receive():
                entered.set()
                result['text'],result['transport']=client.receive_with_timestamp(1024,500)
                result['python']=time.perf_counter()
            worker=threading.Thread(target=receive)
            worker.start()
            assert entered.wait(1)
            time.sleep(.01)
            # PyDLL调用不释放GIL；对端发送后继续持有GIL，隔离recv返回与Python交付。
            winsock=ctypes.PyDLL('Ws2_32.dll')
            winsock.send.argtypes=[ctypes.c_size_t,ctypes.c_char_p,ctypes.c_int,ctypes.c_int]
            winsock.send.restype=ctypes.c_int
            interval=sys.getswitchinterval()
            try:
                sys.setswitchinterval(1.)
                assert winsock.send(peer.fileno(),b'x',1,0)==1
                until=time.perf_counter()+.06
                while time.perf_counter()<until:
                    pass
            finally:
                sys.setswitchinterval(interval)
            worker.join(1)
            assert not worker.is_alive() and result['text']=='x'
            assert result['python']-result['transport']>=.03
        client.close()


def test_diagnostic_owner_failure_never_changes_session_acceptance(tmp_path,monkeypatch):
    backend,_,_,_,commit,accepted,_=fixture(tmp_path,monkeypatch)
    def broken(*args,**kwargs):
        raise OSError('diagnostic unavailable')
    backend._device.note_log_owner_accept=broken
    commit(200.4254)
    assert len(accepted)==1 and backend._observed_commands==1
    assert backend._observation_error is None


def test_rejected_session_receipt_has_no_owner_accept_stamp(tmp_path,monkeypatch):
    backend,_,_,_,commit,_,_=fixture(tmp_path,monkeypatch)
    marks=[]
    backend._device.note_log_owner_accept=lambda *args,**kwargs:marks.append(args)
    backend._session.observe_execution=lambda *args:False
    with pytest.raises(RuntimeError):
        commit(200.4254)
    assert not marks


def test_partial_line_uses_final_recv_batch_not_first_fragment():
    device=NativeMinitouchDevice('unused','unused')
    class Client:
        connected=True
        calls=0
        def receive_with_timestamp(self,*args):
            self.calls+=1
            if self.calls==1:
                return 'par',1.
            self.connected=False
            return 'tial\n',2.
    client=Client();device._client=client;device._closed=False
    device._read_logs(client)
    assert device.log_records_since(0)[0]==1
    assert device.receipt_timing_diagnostics['rows'][0]['transport_return_s']==2.
    assert not device.reset_executed


def test_owner_diagnostic_never_waits_for_reader_lock():
    device=NativeMinitouchDevice('unused','unused')
    device._log_lock.acquire()
    try:
        started=time.perf_counter()
        device.note_log_owner_accept(1,started,action_token=1)
        assert time.perf_counter()-started<.05
    finally:
        device._log_lock.release()
    assert device.receipt_timing_diagnostics['owner_missing']==1
