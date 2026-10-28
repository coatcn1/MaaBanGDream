"""仅匹配几何音符头的实验性投影；颜色不产生 FLICK 或方向语义。"""
from collections import deque
import itertools
import math
import numpy as np
import cv2
TOL = 0.018
_BOOTSTRAP_BATCH_SIZE = 2048


def _depth_supported(values):
    depths = []
    for y in sorted(values):
        if not depths or y - depths[-1] >= 8:
            depths.append(y)
    return len(depths) >= 3 and depths[-1] - depths[0] >= 100


def score(hs, notes, a, h, c, host):
    """t=c+a/(y-h)，只在已确认的本地谱面上做离散指派。"""
    game = c + a / (590 - h)
    if abs(game - host) > 0.3:
        return None
    pairs = []
    used = set()
    for i, x in enumerate(hs):
        options = [(abs(n.time_s - (c + a / (x['y'] - h))), n) for n in notes if n.lane == x['lane'] and n.note_index not in used]
        if not options:
            continue
        err, n = min(options, key=lambda v: v[0])
        if err <= TOL:
            used.add(n.note_index)
            pairs.append((i, n, float(n.time_s - (c + a / (x['y'] - h)))))
    if not pairs:
        return None
    rms = math.sqrt(np.mean([v[2] ** 2 for v in pairs]))
    lanes = len({hs[i]['lane'] for i, n, e in pairs})
    return dict(count=len(pairs), lanes=lanes, rms_s=rms, game_time_s=game, a=a, h=h, c=c, pairs=[dict(head_index=i, note_index=n.note_index, lane=n.lane, due_s=n.time_s, head_y=hs[i]['y'], error_s=e) for i, n, e in pairs])


def rank(z):
    return (-z['count'], z['rms_s'])


def clusters(items):
    out = []
    for z in sorted(items, key=rank):
        near = next((v for v in out if abs(z['game_time_s'] - v['game_time_s']) <= .025), None)
        if near is None:
            if len(out) < 6:
                out.append({**z, 'game_time_bounds_s': (z['game_time_s'], z['game_time_s'])})
        elif (z['count'] >= max(4, near['count'] - 1) and z['lanes'] >= 3 and z['rms_s'] <= .012):
            # 近相位的等价指派保留整个范围；不能把20ms别名当作精确时钟。
            lo, hi = near['game_time_bounds_s']
            near['game_time_bounds_s'] = (min(lo, z['game_time_s']), max(hi, z['game_time_s']))
    return out


def prepare(heads, judgements, expected_time_s):
    hs = [x for x in heads if x['kind'] != 'green']
    ns = [n for n in judgements if n.kind == 'tap' and expected_time_s - 0.3 <= n.time_s <= expected_time_s + 3.5]
    return (hs, ns)


def evaluate(ranked):
    if not ranked:
        return dict(status='unknown', best=None, competition=[])
    best = ranked[0]
    other = ranked[1] if len(ranked) > 1 else None
    margin = other is None or best['count'] >= other['count'] + 2 or (best['count'] >= other['count'] and other['rms_s'] - best['rms_s'] >= 0.01)
    ok = best['count'] >= 4 and best['lanes'] >= 3 and (best['rms_s'] <= 0.012) and margin
    return dict(status='matched' if ok else 'unknown', best=best, competition=ranked[1:4], margin_pass=margin)


def _batch_assign(hs, ns, aa, h, cc, host):
    """仅并行独立候选，逐头贪心、同分首选及全局 note_index 排除保持不变。"""
    size = len(aa)
    times = np.asarray([n.time_s for n in ns], dtype=np.float64)
    lanes = np.asarray([n.lane for n in ns])
    indices = np.asarray([n.note_index for n in ns])
    used = np.zeros((size, len(ns)), dtype=bool)
    chosen = np.full((size, len(hs)), -1, dtype=np.int64)
    active = np.abs(cc + aa / (590 - h) - host) <= .3
    rows = np.arange(size)
    for i, head in enumerate(hs):
        predicted = cc + aa / (head['y'] - h)
        error = np.abs(times[None, :] - predicted[:, None])
        error[:, lanes != head['lane']] = np.inf
        error[used] = np.inf
        pick = error.argmin(axis=1)
        valid = active & (error[rows, pick] <= TOL)
        chosen[valid, i] = pick[valid]
        used |= valid[:, None] & (indices[None, :] == indices[pick, None])
    counts = (chosen >= 0).sum(axis=1)
    lane_count = np.zeros(size, dtype=np.int64)
    for lane in {head['lane'] for head in hs}:
        positions = [i for i, head in enumerate(hs) if head['lane'] == lane]
        lane_count += (chosen[:, positions] >= 0).any(axis=1)
    return chosen, (counts >= 4) & (lane_count >= 3)


def bootstrap(heads, judgements, expected_time_s):
    hs, ns = prepare(heads, judgements, expected_time_s)
    if not 4 <= len(hs) <= 16 or not ns or not _depth_supported(x['y'] for x in hs):
        return evaluate([])
    unique = {}
    for h in np.arange(-300, 50.01, 2.0):
        pending = []
        refined_seen = set()

        def consume():
            if not pending:
                return
            # 单批最多 2048 个候选，不广播全部候选与所有音符头的笛卡尔积。
            values = np.asarray(pending, dtype=np.float64)
            chosen, keep = _batch_assign(hs, ns, values[:, 0], h, values[:, 1], expected_time_s)
            for row in np.flatnonzero(keep):
                assignment = tuple((i, int(n)) for i, n in enumerate(chosen[row]) if n >= 0)
                if assignment in refined_seen:
                    continue
                refined_seen.add(assignment)
                # 同 h 和初始指派的拟合输入完全相同，只保留首次出现的计算及同分次序。
                design = np.array([[1 / (hs[i]['y'] - h), 1] for i, n in assignment])
                target = np.array([ns[n].time_s for i, n in assignment])
                a, c = np.linalg.lstsq(design, target, rcond=None)[0]
                refined = score(hs, ns, float(a), float(h), float(c), expected_time_s)
                if (refined and refined['count'] >= 4 and refined['lanes'] >= 3
                        and _depth_supported(pair['head_y'] for pair in refined['pairs'])):
                    key = tuple((p['head_index'], p['note_index']) for p in refined['pairs'])
                    if key not in unique or rank(refined) < rank(unique[key]):
                        unique[key] = refined
            pending.clear()

        for i, j in itertools.combinations(range(len(hs)), 2):
            den = 1 / (hs[i]['y'] - h) - 1 / (hs[j]['y'] - h)
            if abs(den) < 1e-08:
                continue
            for ni in [n for n in ns if n.lane == hs[i]['lane']]:
                for nj in [n for n in ns if n.lane == hs[j]['lane']]:
                    a = (ni.time_s - nj.time_s) / den
                    if not 150 < a < 1500:
                        continue
                    c = ni.time_s - a / (hs[i]['y'] - h)
                    pending.append((float(a), float(c)))
                    if len(pending) == _BOOTSTRAP_BATCH_SIZE:
                        consume()
        consume()
    ranked = clusters(list(unique.values()))
    return evaluate(ranked)


def match(heads, judgements, expected_time_s, projection):
    hs, ns = prepare(heads, judgements, expected_time_s)
    host = expected_time_s
    cs = []
    a, h = (projection['a'], projection['h'])
    for x in hs:
        for n in ns:
            if n.lane == x['lane']:
                c = n.time_s - a / (x['y'] - h)
                z = score(hs, ns, a, h, c, host)
                if z:
                    cs.append(z)
    ranked = clusters(cs)
    if ranked:
        best = ranked[0]
        delta = float(np.median([p['error_s'] for p in best['pairs']]))
        refined = score(hs, ns, a, h, best['c'] + delta, host)
        if refined and rank(refined) <= rank(best):
            lo, hi = best['game_time_bounds_s']
            refined['game_time_bounds_s'] = (min(lo, refined['game_time_s']), max(hi, refined['game_time_s']))
            ranked[0] = refined
    return evaluate(ranked)


def extract_heads(im):
    hsv = cv2.cvtColor(im, cv2.COLOR_BGR2HSV)
    found = []
    for kind, lo, hi in [('cyan', (80, 45, 215), (112, 255, 255)), ('green', (38, 65, 210), (81, 255, 255)), ('pink', (135, 65, 210), (179, 255, 255))]:
        colour = cv2.inRange(hsv, np.array(lo), np.array(hi))
        mask = cv2.bitwise_or(colour, cv2.inRange(hsv, np.array((0, 0, 235)), np.array((179, 90, 255))))
        mask[:95] = 0
        mask[565:] = 0
        opened = np.zeros_like(mask)
        # 水平形态学核按深度缩放，拒绝文字、箭头和 Slide 连接的碎片。
        for top in range(95, 565, 40):
            bot = min(565, top + 40)
            k = max(9, round(top * 0.22))
            band = cv2.morphologyEx(mask[max(0, top - 10):min(720, bot + 10)], cv2.MORPH_OPEN, np.ones((1, k), np.uint8))
            opened[top:bot] = band[top - max(0, top - 10):bot - max(0, top - 10)]
        n, labs, stats, cents = cv2.connectedComponentsWithStats(opened)
        for st, cent in zip(stats[1:], cents[1:]):
            x, y, w, h, area = map(int, st)
            cx, cy = map(float, cent)
            if not 100 <= cy <= 560 or not 0.24 * cy <= w <= 0.42 * cy or h > max(5, 0.028 * cy):
                continue
            if cv2.countNonZero(colour[max(0, y - 2):y + h + 2, x:x + w]) < max(5, w * 0.15):
                continue
            spacing = 148 * cy / 590
            lane = round((cx - 640) / spacing + 3)
            if not 0 <= lane <= 6:
                continue
            target = 640 + (lane - 3) * spacing
            if abs(cx - target) > spacing * 0.38:
                continue
            found.append(dict(kind=kind, lane=lane, x=cx, y=cy, width=w, height=h, score=abs(w / cy - 0.32)))
    found.sort(key=lambda r: r['score'])
    heads = []
    for r in found:
        if any((abs(r['y'] - s['y']) < 8 and r['lane'] == s['lane'] for s in heads)):
            continue
        heads.append(r)
    heads.sort(key=lambda r: r['y'])
    return heads


def aggregate_bootstrap(frame_inputs, judgements, *, seed_results=None):
    seeds = []
    for index, frame in enumerate(frame_inputs):
        result = (bootstrap(frame['heads'], judgements, frame['host_estimate_s'])
                  if seed_results is None else seed_results[index])
        if result['status'] == 'matched':
            seeds.append((frame, result['best']))
    if len(seeds) < 3:
        return dict(status='unknown', reason='insufficient-bootstrap-frames', seeds=len(seeds))
    candidates = []
    # 各帧拥有独立平移，透视参数跨新帧共享；基准冻结后不再拟合透视。
    for h in np.arange(-300, 50.01, 1.0):
        design = []
        targets = []
        for fi, (frame, best) in enumerate(seeds):
            for pair in best['pairs']:
                row = [1 / (pair['head_y'] - h)] + [1.0 if fi == j else 0.0 for j in range(len(seeds))]
                design.append(row)
                targets.append(pair['due_s'])
        design = np.asarray(design)
        targets = np.asarray(targets)
        co = np.linalg.lstsq(design, targets, rcond=None)[0]
        error = targets - design @ co
        rms = float(np.sqrt(np.mean(error ** 2)))
        candidates.append(dict(a=float(co[0]), h=float(h), rms_s=rms, frame_intercepts=list(map(float, co[1:]))))
    best = min(candidates, key=lambda r: r['rms_s'])
    # RMS 是实验验收门槛，所有仍可通过的模型都参与区间，不能只保留漂亮的最优解。
    family = [dict(a=z['a'], h=z['h']) for z in candidates
              if z['rms_s'] <= .012 and 150 < z['a'] < 1500]
    best['family'] = family
    checks = [match(frame['heads'], judgements, frame['host_estimate_s'], best) for frame, _ in seeds]
    ok = all((check['status'] == 'matched' for check in checks)) and best['rms_s'] <= 0.012
    return dict(status='matched' if ok else 'unknown', projection=best, seeds=len(seeds), checks=checks)


class NativeGameClockMatcher:
    """开场多新帧联合校准后冻结透视，后续仅拟合谱面平移。"""

    def __init__(self, chart=None, *, chart_path=None, environment=None):
        self.chart = chart
        self.chart_path = chart_path
        self.environment = environment
        self.opening = deque(maxlen=6)
        self.projection = None
        self.baseline_interval = None
        self.baseline_lag = None
        self.calibration_report = {}
        self.baseline_models = []

    def reset(self, *, full=True):
        self.opening.clear()
        if full:
            self.projection = None
            self.baseline_interval = None
            self.baseline_lag = None
            self.calibration_report = {}
            self.baseline_models = []

    @staticmethod
    def _game_for_pairs(pairs, projection):
        a, h = (projection['a'], projection['h'])
        intercept = float(np.median([pair['due_s'] - a / (pair['head_y'] - h) for pair in pairs]))
        return intercept + a / (590 - h)

    def observe(self, image, request_s, observed_s, anchor_s, clock_view=None):
        if self.chart is None:
            env = self.environment
            if (env is None or tuple(getattr(env, 'resolution', ())) != (1280, 720)
                or getattr(env, 'dpi', None) != 240 or getattr(env, 'game_fps', None) != 60
                or getattr(env, 'render_quality', None) != 'standard'
                or getattr(env, 'note_skin_type', None) != 1 or getattr(env, 'note_speed', None) != 5.0
                or getattr(env, 'tap_effect', None) != 4 or getattr(env, 'judgement_assist_effect', None) is not False
                or getattr(env, 'engine', None) != 'native'):
                return {'status': 'unknown', 'reason': 'unsupported-environment'}
            # 只在后台加载本地谱面；未验证环境不猜测几何，也不会触发磁盘读取。
            from .chart_timeline import ChartTimeline
            self.chart = ChartTimeline.from_json(self.chart_path)
        if not isinstance(image, np.ndarray) or image.shape != (720, 1280, 3) or anchor_s is None or (not 0 <= observed_s - request_s <= 0.08):
            return {'status': 'unknown', 'reason': 'unsupported-frame'}
        capture_lower_s, capture_upper_s = request_s, observed_s
        if clock_view is not None:
            bounds = clock_view.host_to_execution(request_s, observed_s)
            if bounds is None:
                return {'status': 'unknown', 'reason': 'clock-frame-uncovered'}
            capture_lower_s, capture_upper_s = bounds
        # 候选搜索先验和最终 lag/bounds 共用冻结 E 域，不能只在输出扣一个 scalar。
        host = (capture_lower_s + capture_upper_s) / 2 - anchor_s + self.chart.start_time_s
        heads = extract_heads(image)
        if self.projection is None:
            if observed_s - anchor_s > 8:
                return {'status': 'unknown', 'reason': 'opening-calibration-unavailable'}
            result = bootstrap(heads, self.chart.judgements, host)
            if result['status'] != 'matched':
                return {'status': 'unknown', 'reason': 'opening-ambiguous-or-insufficient'}
            # 种子与自产帧一起受六帧上限和 reset 管理，不另建全谱面内容缓存。
            self.opening.append({'heads': heads, 'host_estimate_s': host, 'request_s': request_s,
                                 'observed_s': observed_s, 'bootstrap_seed': result,
                                 'capture_lower_s': capture_lower_s, 'capture_upper_s': capture_upper_s})
            if len(self.opening) < 3:
                return {'status': 'unknown', 'reason': 'opening-needs-multiple-frames'}
            joint = aggregate_bootstrap(list(self.opening), self.chart.judgements,
                                        seed_results=[frame['bootstrap_seed'] for frame in self.opening])
            if joint['status'] != 'matched':
                return {'status': 'unknown', 'reason': 'opening-inconsistent'}
            bounds, lags = ([], [])
            for frame, check in zip(self.opening, joint['checks']):
                game = check['best']['game_time_s']
                game_lo, game_hi = check['best']['game_time_bounds_s']
                bounds.append((frame['capture_lower_s'] - anchor_s + self.chart.start_time_s - game_hi,
                               frame['capture_upper_s'] - anchor_s + self.chart.start_time_s - game_lo))
                lags.append(frame['host_estimate_s'] - game)
            if max(lags) - min(lags) > 0.02:
                return {'status': 'unknown', 'reason': 'opening-capture-inconsistent'}
            self.projection = joint['projection']
            self.baseline_interval = (min((lo for lo, hi in bounds)), max((hi for lo, hi in bounds)))
            self.baseline_lag = float(np.median(lags))
            self.baseline_models = [(projection, float(np.median([frame['host_estimate_s'] - self._game_for_pairs(check['best']['pairs'], projection) for frame, check in zip(self.opening, joint['checks'])]))) for projection in self.projection['family']]
            self.calibration_report = {'frames': len(self.opening), 'projection': self.projection, 'baseline_interval_s': self.baseline_interval, 'baseline_lag_s': self.baseline_lag}
        result = match(heads, self.chart.judgements, host, self.projection)
        if result['status'] != 'matched':
            return {'status': 'unknown', 'reason': 'ambiguous-or-insufficient-heads'}
        game = result['best']['game_time_s']
        game_lo, game_hi = result['best']['game_time_bounds_s']
        lo = capture_lower_s - anchor_s + self.chart.start_time_s - game_hi - self.baseline_interval[1]
        hi = capture_upper_s - anchor_s + self.chart.start_time_s - game_lo - self.baseline_interval[0]
        relative = host - game - self.baseline_lag
        alternatives = [host - self._game_for_pairs(result['best']['pairs'], projection) - baseline for projection, baseline in self.baseline_models]
        if alternatives:
            lo += min(0, min(alternatives) - relative)
            hi += max(0, max(alternatives) - relative)
        return {'status': 'matched', 'relative_lag_ms': (host - game - self.baseline_lag) * 1000, 'capture_bounds_ms': (lo * 1000, hi * 1000), 'match': result['best']}

    def report(self):
        # 开场搜索最多351个h候选；仅复制已冻结结果，不重算几何或参与纠偏。
        capacity, models, count, warning = 351, [], 0, None
        try:
            count = len(self.baseline_models)
            for projection, baseline in itertools.islice(self.baseline_models, capacity):
                a, h, lag = float(projection['a']), float(projection['h']), float(baseline)
                if not all(math.isfinite(value) for value in (a, h, lag)):
                    raise ValueError('non-finite baseline model')
                models.append(dict(projection=dict(a=a, h=h), baseline_lag_s=lag))
            family_count = len(self.projection.get('family', ())) if self.projection is not None else 0
            complete = count == len(models) == family_count
            if not complete:
                warning = 'incomplete-family-baselines'
        except Exception as exc:
            # 附加报告损坏只撤销该诊断的完整性，不改变matcher或演奏状态。
            models, complete, warning = [], False, type(exc).__name__
        return {'calibrated': self.projection is not None,
                'bounds_sources': ['capture-window', 'accepted-projection-family'],
                'baseline_models_schema_version': 1,
                'baseline_models': models,
                'baseline_models_capacity': capacity,
                'baseline_models_dropped': max(0, count - len(models)),
                'baseline_models_complete': complete,
                'baseline_models_warning': warning,
                'baseline_interval_scope': 'nominal-projection-opening-capture-envelope',
                'experimental_thresholds': {'rms_ms': 12, 'inlier_ms': 18,
                                            'minimum_depth_groups': 3, 'minimum_y_span_px': 100},
                **self.calibration_report}
