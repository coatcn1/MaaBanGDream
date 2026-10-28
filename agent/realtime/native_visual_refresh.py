"""默认关闭的判定条刷新观测；不将像素刷新声明为独立GR或某个输入的判定。"""

from collections import deque, OrderedDict
from dataclasses import asdict

import numpy as np
import math

from .native_observation_clock import ObservationClockView

MODEL = 'visual-direction-windows-v2'
SCHEMA_VERSION = 3


class VisualRefreshDetector:
    PATCHES = ((592, 536, 602, 542), (678, 536, 688, 542))

    def __init__(self):
        self.previous = None

    def reset(self):
        self.previous = None

    @staticmethod
    def measure(image):
        if not isinstance(image, np.ndarray) or image.shape != (720, 1280, 3):
            raise ValueError('unsupported refresh image')
        # HSV的V等于BGR最大通道；只读固定小块，不复制或保存整张图。
        return [float(np.max(image[y1:y2, x1:x2], axis=2).mean())
                for x1, y1, x2, y2 in VisualRefreshDetector.PATCHES]

    def observe(self, sample):
        previous, self.previous = self.previous, sample
        if sample['kind'] not in {'fast', 'slow'}:
            self.previous = None
            return None
        if (previous is None or previous['kind'] != sample['kind']
                or sample['frame_sequence'] <= previous['frame_sequence']
                or not 0 < sample['observed_s'] - previous['observed_s'] <= .2):
            return None
        if (all(value - old >= 30 for value, old in zip(sample['patch_v'], previous['patch_v']))
                and sample['slow'] + sample['fast'] > previous['slow'] + previous['fast']):
            return dict(previous=previous, current=sample)
        return None


class NativeVisualRefreshEvidence:
    CAPACITY = 4096
    PENDING_CAPACITY = 16
    ROI_CAPACITY = 128
    ROI_READY_CAPACITY = 16
    ROI_TOTAL_CAPACITY = 256

    def __init__(self, backend):
        self.backend = backend
        self.detector = DirectionWindowDetector()
        self.pending = deque(maxlen=self.PENDING_CAPACITY)
        self.metrics = deque(maxlen=self.CAPACITY)
        self.dropped = self.pending_dropped = 0
        self.context = self.fence = None
        self.sequence = 0
        self.disabled_reason = None
        self._roi_cache = OrderedDict()
        self._roi_ready = deque()
        self._roi_seen = set()
        self.roi_queued = self.roi_missing = self.roi_dropped = 0
        self.roi_disabled_reason = None
        self._periodic_anchor_s = None
        self.periodic_roi_queued = self.periodic_roi_dropped = 0
        self.periodic_roi_first_s = self.periodic_roi_last_s = None
        self._raw_sequence=-1
        self._raw_run_id=backend._run_id
        self._raw_kind=None
        self.onset_disabled_reason=None
        self.onset_queued=self.onset_dropped=0
        self.onset_first_s=self.onset_last_s=None
        self.low_priority_queued=self.roi_evicted=0
        self._roi_handoff={}
        self.roi_handoff_accepted=self.roi_handoff_rejected=self.roi_handoff_unknown=0

    def reset(self):
        self.pending.clear()
        self.detector.reset()
        self.context = self.fence = None
        self._roi_cache.clear()

    def disable(self, exc):
        self.disabled_reason = type(exc).__name__
        self.reset()

    def _record(self, row):
        if len(self.metrics) == self.CAPACITY:
            self.dropped += 1
        self.metrics.append(row)

    def _state(self, now):
        backend = self.backend
        provenance = backend.observation_provenance()
        guard_epoch = getattr(backend, '_visual_phase_guard_epoch', 0)
        if (getattr(backend, '_phase_closed', True) or backend._session_state != 'running'
                or getattr(backend, '_visual_phase_guard_requested', False)):
            return None
        snapshot = backend.observation_clock_snapshot()
        if (not backend._observation_clock_ready(snapshot, now)
                or snapshot.identity.run_id != backend._run_id
                or backend._clock_basis != 'probe-midpoint'
                or backend._clock_uncertainty_ms is None
                or not math.isfinite(backend._clock_uncertainty_ms)
                or not 0 <= backend._clock_uncertainty_ms <= 1):
            return None
        status = backend.phase_status_snapshot()
        if status.run_id != backend._run_id or status.state in {
                'requested', 'waiting-touch-boundary', 'waiting-gap', 'waiting-execution-receipts', 'stopped'}:
            return None
        effect = backend.phase_effect_snapshot()
        if effect is None and status.cumulative_phase_ms != 0:
            return None
        if effect is not None and effect.cumulative_phase_ms != status.cumulative_phase_ms:
            return None
        # 使用本次实际检查的不可变快照，不能重读新effect给旧执行证明换上下文。
        context = backend._visual_refresh_context_from(snapshot, status, effect, provenance, guard_epoch)
        return snapshot, status, effect, context

    def _support(self, snapshot, effect, request_s, now):
        backend = self.backend
        raw = backend.execution_commit_snapshot()
        by_cursor = {row.command_cursor: row for row in raw}
        rows = [row for row in backend.mapped_execution_commit_snapshot(now)
                if row.clock_identity == snapshot.identity and row.observation_epoch == snapshot.epoch
                and row.trust_revision == snapshot.trust_revision
                and row.execution.run_id == backend._run_id
                and by_cursor.get(row.execution.command_cursor) == row.execution
                and row.execution.command_cursor <= backend._observed_commands
                and row.host_upper_s <= request_s and row.execution.actions
                and 0 <= row.uncertainty_ms <= 1
                and all(abs(row.execution.actual_execution_s - action.planned_s) <= .04
                        for action in row.execution.actions)]
        if effect is not None:
            if (effect.run_id != backend._run_id or effect.state != 'applied'
                    or effect.first_adjusted_due_s is None or effect.first_affected_chunk_sequence is None
                    or effect.first_affected_action_token is None):
                return None
            rows = [row for row in rows if row.execution.chunk_sequence >= effect.first_affected_chunk_sequence
                    and row.execution.actual_execution_s >= effect.first_adjusted_due_s
                    and any(action.action_token >= effect.first_affected_action_token
                            and round(action.planned_s * 1e9) >= round(effect.first_adjusted_due_s * 1e9)
                            for action in row.execution.actions)]
        if not rows:
            return None
        # owner只在逐命令连续核对并接受全部receipt后发布immutable commit。
        # 缓存本context最早可信执行fence，不能每帧追逐latest而永远无法等待满200ms。
        if self.fence is None:
            first = min(rows, key=lambda row: row.execution.command_cursor)
            self.fence = dict(command_cursor=first.execution.command_cursor, host_upper_s=first.host_upper_s)
        return max(rows, key=lambda row: row.execution.command_cursor)

    def enqueue(self, image, metadata, upper_s, observed_s, kind, signals, slow, fast):
        if self.disabled_reason:
            return
        request = metadata['request_s']
        if (metadata.get('reused', False) or type(metadata.get('frame_sequence')) is not int
                or not 0 <= observed_s - request <= .080 or not request <= upper_s <= observed_s):
            self.detector.reset()
            return
        # 调用方只在scene有效且fresh时进入；本支路不依赖clock资格且不改变控制状态。
        self._observe_raw_onset(image,metadata,upper_s,observed_s,kind,signals,slow,fast)
        state = self._state(observed_s)
        if state is None:
            self.reset()
            self._record(dict(event='refresh-rejected', reason='state-unavailable',
                frame_sequence=metadata['frame_sequence'], request_s=request,
                capture_upper_s=upper_s, observed_s=observed_s,
                kind=kind.value if kind is not None else None, slow=int(slow), fast=int(fast),
                patch_v=self.detector.measure(image)))
            return
        snapshot, _, _, context = state
        if context != self.context:
            self.reset()
            self.context = context
        sample = dict(frame_sequence=metadata['frame_sequence'], request_s=request,
            capture_upper_s=upper_s, observed_s=observed_s,
            kind=kind.value if kind is not None and len(signals) == 1 else None,
            slow=int(slow), fast=int(fast), patch_v=self.detector.measure(image),
            clock_identity=asdict(snapshot.identity), clock_epoch=snapshot.epoch,
            clock_trust_revision=snapshot.trust_revision)
        self._record(dict(event='refresh-sample', **sample))
        self._cache_roi(image, sample)
        if len(self.pending) == self.PENDING_CAPACITY:
            self.pending_dropped += 1
            self.detector.reset()
        self.pending.append((context, sample))

    def drain(self, now):
        if self.disabled_reason:
            return ()
        state = self._state(now)
        if state is None:
            self.reset()
            return ()
        snapshot, _, effect, context = state
        if context != self.context:
            self.reset()
            self.context = context
            return ()
        result = []
        while self.pending:
            sealed, sample = self.pending[0]
            if sealed != context or now - sample['observed_s'] > .5:
                self.pending.popleft()
                self.detector.reset()
                self._record(dict(event='refresh-rejected', reason='context-or-frame-expired', **sample))
                continue
            view = snapshot.frame_view(sample['request_s'], sample['capture_upper_s'], now_s=now,
                execution_offset_s=self.backend._device_clock_offset_s)
            if view is None and snapshot.probes[-1].host_send_s < sample['capture_upper_s']:
                break
            self.pending.popleft()
            valid_view = (isinstance(view, ObservationClockView)
                and (view.identity, view.epoch, view.trust_revision) == context[:3])
            support = self._support(snapshot, effect, sample['request_s'], now) if valid_view else None
            if support is None or sample['request_s'] < self.fence['host_upper_s'] + .2:
                self.detector.reject(sample, 'clock-or-execution-fence')
                try:
                    detail=dict(view_present=view is not None,
                        clockkey_match=valid_view if view is not None else None,
                        support_checked=valid_view, has_support=support is not None if valid_view else None,
                        fence_checked=support is not None,
                        fence_upper_s=self.fence['host_upper_s'] if support is not None else None,
                        request_s=sample['request_s'],
                        fence_margin_s=sample['request_s']-self.fence['host_upper_s']-.2 if support is not None else None)
                except Exception:
                    # 细项只解释原判定；诊断构造失败仍沿原拒绝与输入流程继续。
                    detail=None
                self._record(dict(event='refresh-rejected', reason='clock-or-execution-fence',detail=detail,**sample))
                continue
            sample = dict(sample, supporting_execution_cursor=support.execution.command_cursor,
                execution_fence=dict(self.fence), effect_sequence=effect.request_sequence if effect else None)
            window = self.detector.observe(sample)
            if window is not None:
                self.sequence += 1
                window = dict(event='qualified-direction-window', window_id=self.sequence, **window)
                self._record(window)
                self._queue_window_rois(window)
                # 只把不可JSON化的封存凭据交给内存请求链；数值报告保留上面的pulse副本。
                result.append(dict(window, sealed_context=context))
            self._queue_periodic_roi(sample)
        return tuple(result)

    def report(self):
        return dict(mode='direction-window-observation', source_module=__file__, independent_gr_claim=False,
                    model=MODEL,schema_version=SCHEMA_VERSION,unit='qualified-direction-window',
                    baseline_unit='qualified fresh frame without a strong direction (slow<600 and fast<600)',
                    ui_animation_assumption='strong same-colour rebound or reappearance after three NoStrong frames may be indistinguishable from a new judgement animation',
                    capacity=self.CAPACITY, dropped=self.dropped, pending_dropped=self.pending_dropped,
                    pending=len(self.pending), disabled_reason=self.disabled_reason, records=list(self.metrics),
                    roi_evidence=dict(cache_capacity=self.ROI_CAPACITY, ready_capacity=self.ROI_READY_CAPACITY,
                        total_capacity=self.ROI_TOTAL_CAPACITY, queued=self.roi_queued, missing=self.roi_missing,
                        queued_unit='admitted-attempts-including-evicted-before-handoff',
                        dropped=self.roi_dropped, pending=len(self._roi_ready), disabled_reason=self.roi_disabled_reason,
                        periodic_interval_s=5, periodic_queued=self.periodic_roi_queued,
                        periodic_dropped=self.periodic_roi_dropped,
                        periodic_first_observed_s=self.periodic_roi_first_s,
                        periodic_last_observed_s=self.periodic_roi_last_s,
                        low_priority_capacity=128,low_priority_queued=self.low_priority_queued,
                        evicted_before_handoff=self.roi_evicted,
                        handoff_accepted=self.roi_handoff_accepted,handoff_rejected=self.roi_handoff_rejected,
                        handoff_unknown=self.roi_handoff_unknown,
                        handoff_pending=sum(value=='pending' for value in self._roi_handoff.values()),
                        handoff_records=[dict(sequence=sequence,status=status) for sequence,status in self._roi_handoff.items()],
                        saved_status='unknown-until-background-events',
                        coverage_complete=False if (self.roi_dropped or self.roi_missing or self.roi_handoff_rejected
                            or self.roi_disabled_reason or self.onset_disabled_reason) else None,
                        raw_onset=dict(queued=self.onset_queued,dropped=self.onset_dropped,
                            first_observed_s=self.onset_first_s,last_observed_s=self.onset_last_s,
                            disabled_reason=self.onset_disabled_reason)))

    def _queue_periodic_roi(self, sample):
        # 节拍仅由已通过完整证据门禁的原图时间推进；轮询与拒帧不能续租。
        observed=sample['observed_s']
        if self._periodic_anchor_s is None:
            self._periodic_anchor_s=observed
            return
        if observed-self._periodic_anchor_s < 5:
            return
        self._periodic_anchor_s=observed
        sequence=sample['frame_sequence']
        if sequence in self._roi_seen or self.roi_disabled_reason:
            return
        try:
            cached=self._roi_cache.get(sequence)
            if cached is None:
                self.roi_missing+=1
                self.periodic_roi_dropped+=1
                return
            roi,_=cached
            if not self._queue_ready(dict(image=roi,timestamp=observed,window_start_s=observed,
                metadata=dict(sample,sequence=sequence,first_done_observed_s=sample['capture_upper_s'],
                    roi=[570,514,710,556],control_window_id=None,evidence_roles=['periodic-audit'])),low=True):
                self.periodic_roi_dropped+=1
                return
            self.periodic_roi_queued+=1
            if self.periodic_roi_first_s is None:
                self.periodic_roi_first_s=observed
            self.periodic_roi_last_s=observed
        except Exception as exc:
            self.roi_disabled_reason=type(exc).__name__

    def _cache_roi(self, image, sample):
        if self.roi_disabled_reason:
            return
        try:
            roi=image[514:556,570:710].copy()
            roi.setflags(write=False)
            self._roi_cache[sample['frame_sequence']] = (roi,dict(sample))
            if len(self._roi_cache)>self.ROI_CAPACITY:
                self._roi_cache.popitem(last=False)
        except Exception as exc:
            self.roi_disabled_reason=type(exc).__name__
            self._roi_cache.clear()

    def _queue_window_rois(self, window):
        if self.roi_disabled_reason:
            return
        try:
            roles={}
            samples={}
            witness=window['witness']
            if witness.get('type')=='neutral-appearance':
                for sample in witness['baseline_frames']:
                    roles.setdefault(sample['frame_sequence'],[]).append('neutral-baseline')
                    samples[sample['frame_sequence']]=sample
                roles.setdefault(witness['attack']['frame_sequence'],[]).append('appearance-attack')
                samples[witness['attack']['frame_sequence']]=witness['attack']
            else:
                for role in ('peak','valley','attack'):
                    sample=witness[role]
                    roles.setdefault(sample['frame_sequence'],[]).append(role)
                    samples[sample['frame_sequence']]=sample
            for sample in window['frames']:
                roles.setdefault(sample['frame_sequence'],[]).append('confirmation')
                samples[sample['frame_sequence']]=sample
            for sequence,role in roles.items():
                if sequence in self._roi_seen:
                    # 图片原raw角色不改；尚未交接且已被控制窗引用的图不能再按低优先驱逐。
                    for row in self._roi_ready:
                        if row['metadata']['sequence']==sequence:
                            row['control_referenced']=True
                    continue
                cached=self._roi_cache.get(sequence)
                if cached is None:
                    self.roi_missing+=1
                    continue
                roi,metadata=cached
                # 资格封存字段来自这张图本身；缓存仍持有相同seq的原ROI与采样时间。
                metadata=dict(metadata,**samples[sequence])
                self._queue_ready(dict(image=roi,timestamp=metadata['observed_s'],
                    window_start_s=window['witness']['attack']['observed_s'],
                    metadata=dict(metadata,sequence=sequence,first_done_observed_s=metadata['capture_upper_s'],
                        roi=[570,514,710,556],control_window_id=window['window_id'],evidence_roles=role)),low=False)
        except Exception as exc:
            self.roi_disabled_reason=type(exc).__name__

    def _observe_raw_onset(self,image,metadata,upper_s,observed_s,kind,signals,slow,fast):
        if self.onset_disabled_reason:
            return
        try:
            backend=self.backend
            stop=getattr(backend,'_native_input_stop_requested',None)
            if (getattr(backend,'_phase_closed',True) or backend._session_state!='running'
                    or backend._run_id!=self._raw_run_id
                    or stop is not None and stop.is_set()):
                return
            sequence=metadata['frame_sequence']
            if sequence<=self._raw_sequence:
                return
            self._raw_sequence=sequence
            current=kind.value if kind is not None and len(signals)==1 else None
            previous,self._raw_kind=self._raw_kind,current
            if current is None or current==previous:
                return
            if self.low_priority_queued>=128 or self.roi_queued>=self.ROI_TOTAL_CAPACITY:
                self.onset_dropped+=1
                self.roi_dropped+=1
                return
            # 原始首图永不借后续clock/effect水印；资格结论只通过同seq的原数值记录join。
            roi=image[514:556,570:710].copy()
            roi.setflags(write=False)
            sample=dict(frame_sequence=sequence,sequence=sequence,request_s=metadata['request_s'],
                capture_upper_s=upper_s,first_done_observed_s=upper_s,observed_s=observed_s,
                run_id=self._raw_run_id,kind=current,raw_kind=current,slow=int(slow),fast=int(fast),
                patch_v=VisualRefreshDetector.measure(image),qualification='raw-not-assessed',
                qualification_reason='clock-view-support-not-assessed-at-onset',
                diagnostic_only=True,independent_gr_claim=False,distinct_gr_claim=False,control_window_id=None,
                evidence_roles=['raw-direction-onset'],roi=[570,514,710,556])
            if (backend._phase_closed or backend._session_state!='running'
                    or stop is not None and stop.is_set()):
                return
            if self._queue_ready(dict(image=roi,timestamp=observed_s,window_start_s=observed_s,
                    metadata=sample),low=True):
                self.onset_queued+=1
                if self.onset_first_s is None:self.onset_first_s=observed_s
                self.onset_last_s=observed_s
            else:self.onset_dropped+=1
        except Exception as exc:
            self.onset_disabled_reason=type(exc).__name__

    def _queue_ready(self,row,*,low):
        sequence=row['metadata']['sequence']
        if sequence in self._roi_seen:
            return False
        if self.roi_queued>=self.ROI_TOTAL_CAPACITY or low and self.low_priority_queued>=128:
            self.roi_dropped+=1
            return False
        if len(self._roi_ready)>=self.ROI_READY_CAPACITY:
            victim=next((i for i,item in enumerate(self._roi_ready)
                if item['metadata']['control_window_id'] is None and not item.get('control_referenced',False)),None) if not low else None
            if victim is None:
                self.roi_dropped+=1
                return False
            self._roi_ready.rotate(-victim)
            removed=self._roi_ready.popleft()
            self._roi_ready.rotate(victim)
            self._roi_seen.discard(removed['metadata']['sequence'])
            self._roi_handoff[removed['metadata']['sequence']]='evicted-before-handoff'
            if 'raw-direction-onset' in removed['metadata']['evidence_roles']:
                self.onset_dropped+=1
            elif 'periodic-audit' in removed['metadata']['evidence_roles']:
                self.periodic_roi_dropped+=1
            self.roi_evicted+=1
            self.roi_dropped+=1
        self._roi_ready.append(row)
        self._roi_seen.add(sequence)
        self.roi_queued+=1
        if low:self.low_priority_queued+=1
        return True

    def acknowledge_roi_evidence(self,sequence,accepted):
        if sequence not in self._roi_handoff or self._roi_handoff[sequence]!='pending':
            return
        self._roi_handoff[sequence]='accepted-to-queue' if accepted is True else 'handoff-rejected' if accepted is False else 'unknown'
        if accepted is True:self.roi_handoff_accepted+=1
        elif accepted is False:
            self.roi_handoff_rejected+=1
            self._roi_seen.discard(sequence)
        else:self.roi_handoff_unknown+=1

    def take_roi_evidence(self):
        result=tuple(self._roi_ready)
        self._roi_ready.clear()
        for row in result:self._roi_handoff[row['metadata']['sequence']]='pending'
        return result


class RetreatReattackDetector:
    """已seal的局部缺帧容忍；一个再攻击见证只关闭一个真实三帧方向窗。"""
    measure=staticmethod(VisualRefreshDetector.measure)

    def __init__(self):
        self.high_sequence=-1
        self.reset()

    def reset(self):
        self.peak=self.valley=self.last_good=self.witness=None
        self.frames=[]
        self.submitted=False

    def reject(self,sample,reason):
        if sample['frame_sequence']<=self.high_sequence:
            self.reset()
            return
        self.high_sequence=max(self.high_sequence,sample['frame_sequence'])
        self.frames=[]
        self.witness=None
        if reason!='clock-or-execution-fence':
            self.reset()

    @staticmethod
    def _area(sample):
        return sample['slow']+sample['fast']

    def observe(self,sample):
        sequence=sample['frame_sequence']
        if sequence<=self.high_sequence:
            # 上游即使误标eligible，旧/乱序图也不能成为新peak或谷底。
            self.reset()
            return None
        self.high_sequence=sequence
        previous=self.last_good
        if (sample['kind'] not in {'fast','slow'} or
                previous is not None and (sample['kind']!=previous['kind']
                or not 0<sample['observed_s']-previous['observed_s']<=.2)):
            self.reset()
            if sample['kind'] not in {'fast','slow'}:
                return None
        self.last_good=sample
        if self.submitted:
            return None
        if self.witness is not None and sample['observed_s']-self.witness['attack']['observed_s']>.5:
            self.witness=None
            self.frames=[]
        if self.peak is None:
            self.peak=sample
            return None
        if self.valley is not None and sample['observed_s']-self.valley['observed_s']>.2:
            self.valley=None
        area=self._area(sample)
        if self.valley is not None:
            gain=area-self._area(self.valley)
            if (gain>=600 or gain>0 and all(v-old>=30 for v,old in zip(sample['patch_v'],self.valley['patch_v']))):
                if self.witness is None:
                    self.witness=dict(peak=self.peak,valley=self.valley,attack=sample)
                    self.frames=[]
                self.peak=sample
                self.valley=None
            elif area<self._area(self.valley):
                self.valley=sample
        drop=self._area(self.peak)-area
        if (drop>=600 or drop>0 and all(old-v>=30 for old,v in zip(self.peak['patch_v'],sample['patch_v']))):
            if self.valley is None or area<self._area(self.valley):
                self.valley=sample
        elif area>=self._area(self.peak):
            self.peak=sample
        if self.witness is None:
            return None
        self.frames.append(sample)
        if len(self.frames)<3:
            return None
        self.submitted=True
        return dict(unit='qualified-direction-window',witness=self.witness,frames=list(self.frames),
            previous=self.frames[0],current=self.frames[-1],independent_gr_claim=False)


class DirectionWindowDetector:
    """三张未见强方向的合格图或一次再攻击见证，各自只关闭一个方向窗。"""
    measure=staticmethod(RetreatReattackDetector.measure)
    def __init__(self):
        self.retreat=RetreatReattackDetector();self.high_sequence=-1;self.reset()
    def reset(self):
        self.retreat.reset();self.neutral=deque(maxlen=3);self.appearance=None;self.confirm=[];self.last=None
    def reject(self,sample,reason):
        self.retreat.reject(sample,reason)
        self.high_sequence=max(self.high_sequence,sample['frame_sequence'])
        self.neutral.clear();self.appearance=None;self.confirm=[];self.last=None
    def observe(self,sample):
        seq=sample['frame_sequence']
        if seq<=self.high_sequence:
            self.reset();return None
        self.high_sequence=seq
        if self.last is not None and not 0<sample['observed_s']-self.last['observed_s']<=.2:
            self.reset()
        self.last=sample
        kind=sample['kind']
        if kind not in {'fast','slow'}:
            self.appearance=None;self.confirm=[]
            self.retreat.observe(sample)
            # kind=None也可能是两强混色；只有两通道均不足强色门限才建立基线。
            if sample['slow']<600 and sample['fast']<600:self.neutral.append(sample)
            else:self.neutral.clear()
            return None
        baseline=list(self.neutral);self.neutral.clear()
        retreat_window=self.retreat.observe(sample)
        if self.appearance is not None and kind!=self.appearance['attack']['kind']:
            self.appearance=None;self.confirm=[]
        if self.appearance is None and len(baseline)==3:
            self.appearance=dict(type='neutral-appearance',baseline_frames=baseline,attack=sample)
            self.confirm=[]
        if self.appearance is not None:
            self.confirm.append(sample)
            if len(self.confirm)==3:
                result=dict(unit='qualified-direction-window',witness=self.appearance,
                    frames=list(self.confirm),previous=self.confirm[0],current=self.confirm[-1],independent_gr_claim=False)
                self.appearance=None;self.confirm=[];self.retreat.submitted=True
                return result
            return None
        if retreat_window:
            retreat_window['witness']=dict(type='retreat-reattack',**retreat_window['witness'])
        return retreat_window
    @property
    def peak(self):
        return self.retreat.peak

    @property
    def valley(self):
        return self.retreat.valley

    @property
    def last_good(self):
        return self.retreat.last_good

    @property
    def witness(self):
        return self.appearance if self.appearance is not None else self.retreat.witness

    @property
    def frames(self):
        return self.confirm if self.appearance is not None else self.retreat.frames

    @property
    def submitted(self):
        return self.retreat.submitted
