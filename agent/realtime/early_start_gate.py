"""仅用于已测环境和首双 FLICK 拓扑的默认关闭首音候选。"""

from dataclasses import asdict
import math
import cv2
import numpy as np

from .profile_store import EnvironmentSignature

CALIBRATED_ENVIRONMENT = EnvironmentSignature(
    resolution=(1280, 720),
    dpi=240,
    game_fps=60,
    render_quality="standard",
    note_speed=5.0,
    note_skin_type=1,
    tap_effect=4,
    judgement_assist_effect=False,
    engine="native",
)
CALIBRATION = dict(
    version="upper-200-paired-flick-v1",
    line_y=200.0,
    scan_band=[100, 300],
    delay_s=0.89487408445,
    delay_envelope_s=[0.8885792267, 0.9011689422],
    half_width_s=0.00629485775,
    target_semantics="photogate-return-before-profile",
    measured_scope="two recorded environments; speed 5.0; first outer-lane paired FLICK",
    evidence_id="atoz-upper-gate-20261018/fixed-line-200; atoz-start-candidate-20261018/prototype",
    uncertainty="recorded envelope only; sparse interpolation and live capture delay are additional",
)


def first_group_topology(actions):
    if not actions:
        return dict(supported=False, reason="empty-chart")
    first = min(float(a["due_s"]) for a in actions)
    group = [a for a in actions if abs(float(a["due_s"]) - first) <= 1e-7]
    unique = {}
    conflict = False
    for a in group:
        key = a.get("note_index")
        if key is None or (
            key in unique
            and (a["kind"], a["lane"], a.get("flick_direction"))
            != (
                unique[key]["kind"],
                unique[key]["lane"],
                unique[key].get("flick_direction"),
            )
        ):
            conflict = True
        unique[key] = a
    later = [
        float(a["due_s"]) - first for a in actions if float(a["due_s"]) - first > 1e-7
    ]
    gap = min(later) if later else None
    supported = (
        not conflict
        and len(unique) == 2
        and all(a.get("flick_direction") is None for a in unique.values())
        and sorted((a["kind"], int(a["lane"])) for a in unique.values())
        == [("flick", 0), ("flick", 6)]
        and gap is not None
        and gap >= 0.25
    )
    return dict(
        supported=supported,
        reason=None if supported else "unsupported-first-group",
        first_due_s=first,
        next_group_gap_s=gap,
        group=[
            {
                k: a.get(k)
                for k in ("kind", "lane", "note_index", "due_s", "flick_direction")
            }
            for a in unique.values()
        ],
    )


class EarlyStartGate:
    def __init__(self, environment, actions, *, geometry_supported=True):
        self.environment = asdict(environment) if environment is not None else None
        self.topology = first_group_topology(actions)
        self.eligible = (
            environment == CALIBRATED_ENVIRONMENT and self.topology["supported"]
        )
        self.rejected_reason = (
            "environment-not-calibrated"
            if environment != CALIBRATED_ENVIRONMENT
            else self.topology["reason"]
        )
        if not geometry_supported:
            self.eligible = False
            self.rejected_reason = "geometry-not-calibrated"
        self.previous = None
        self.evidence = None
        self.rois = None
        self.last_sequence = None
        self.started_s = None
        self.disabled = False

    def invalidate(self, reason):
        # 已经看见首组后不允许跨缺头/弹窗重新认领后续同拓扑。
        if self.previous is not None:
            self.disabled = True
            self.rejected_reason = reason
        self.previous = None

    def detect(self, image):
        if image.shape[:2] != (720, 1280):
            return None
        # 只转换上段，热路径不扫描角色区以外的全画面。
        hsv = cv2.cvtColor(image[100:301, :, :3], cv2.COLOR_BGR2HSV)
        pink = (
            (hsv[:, :, 0] >= 140)
            & (hsv[:, :, 0] <= 179)
            & (hsv[:, :, 1] >= 60)
            & (hsv[:, :, 2] >= 100)
        )
        candidates = []
        for y in range(100, 301):
            scale = (y - 20) / 570
            spans = []
            centers = []
            for lane_x in (190, 1090):
                x = 640 + (lane_x - 640) * scale
                half = round(110 * scale + 4)
                left = max(0, round(x) - half)
                right = min(1280, round(x) + half + 1)
                xs = np.flatnonzero(pink[y - 100, left:right])
                spans.append(int(xs[-1] - xs[0] + 1) if len(xs) >= 2 else 0)
                centers.append(float(left + (xs[0] + xs[-1]) / 2) if len(xs) else None)
            # 菱形头的横向投影宽于窄箭头，且双头必须在同一高度同时成立。
            if min(spans) < max(3, 75 * scale) or min(spans) < max(spans) * 0.5:
                continue
            candidates.append(
                dict(y=y, normalized_span=min(spans) / scale, centers=centers)
            )
        if not candidates:
            return None
        groups = []
        for row in candidates:
            if not groups or row["y"] - groups[-1][-1]["y"] > 6:
                groups.append([row])
            else:
                groups[-1].append(row)
        group = groups[-1]
        peak = max(r["normalized_span"] for r in group)
        chosen = [r for r in group if r["normalized_span"] >= peak * 0.95]
        return dict(
            y=float(np.median([r["y"] for r in chosen])),
            normalized_span=peak,
            centers=np.median(
                np.array([r["centers"] for r in chosen]), axis=0
            ).tolist(),
            group_extent=[group[0]["y"], group[-1]["y"]],
            lanes=[0, 6],
        )

    def observe(self, image, now, metadata):
        if not self.eligible or self.disabled or self.evidence is not None:
            return None
        if metadata.get("reused") is True:
            return None
        sequence = metadata.get("sequence")
        if isinstance(sequence, int) and not isinstance(sequence, bool):
            if self.last_sequence is not None and sequence <= self.last_sequence:
                return None
            self.last_sequence = sequence
        if self.started_s is None:
            self.started_s = float(now)
        if now - self.started_s > self.topology["first_due_s"] + 0.6:
            self.disabled = True
            self.rejected_reason = "first-group-acquisition-window-expired"
            return None
        if image.shape[:2] != (720, 1280):
            self.disabled = True
            self.rejected_reason = "frame-resolution-mismatch"
            return None
        pair = self.detect(image)
        if pair is None:
            self.invalidate("first-head-track-lost")
            return None
        if self.previous is None and pair["y"] >= 200:
            self.disabled = True
            self.rejected_reason = "first-head-already-past-line"
            return None
        roi = image[100:301, :, :3].copy()
        roi.flags.writeable = False
        current = dict(head=pair, observed_s=float(now), metadata=dict(metadata))
        previous = self.previous
        self.previous = (current, roi)
        if previous is None:
            return None
        prior, prior_roi = previous
        dt = now - prior["observed_s"]
        dy = pair["y"] - prior["head"]["y"]
        ratio = pair["normalized_span"] / prior["head"]["normalized_span"]
        if 0 < dt <= 0.6 and -0.5 <= dy <= 0 and 0.5 <= ratio <= 2:
            # 菱形头的像素中心会量化停留或回退半像素；这对不能插值，但不是换了后续音符。
            self.rejected_reason = "subpixel-motion-reference-replaced"
            return None
        if (
            0 < dt <= 0.6
            and 0 < dy <= 180
            and not 0.5 <= ratio <= 2
            and prior["head"]["y"] < 200
            and pair["y"] < 200
        ):
            # 头部刚进入 ROI 时会被上边界裁切；不跨该失配插值，只保留新的穿线前样本。
            self.rejected_reason = "pre-crossing-width-reference-replaced"
            return None
        if not (
            math.isfinite(dt) and 0 < dt <= 0.6 and 0 < dy <= 180 and 0.5 <= ratio <= 2
        ):
            self.disabled = True
            self.rejected_reason = "first-head-motion-discontinuous"
            return None
        if prior["head"]["y"] < 200 <= pair["y"]:
            crossing = prior["observed_s"] + (200 - prior["head"]["y"]) / dy * dt
            anchor = crossing + CALIBRATION["delay_s"]
            self.evidence = dict(
                source="upper-first-pair-crossing",
                confidence="experimental-calibrated-envelope",
                calibration=dict(CALIBRATION),
                environment=self.environment,
                first_group=self.topology,
                roi=[0, 100, 1280, 201],
                observations=[prior, current],
                crossing_s=crossing,
                gate_anchor_s=anchor,
                remaining_before_profile_s=anchor - now,
            )
            self.rois = (prior_roi, roi)
            return anchor
        return None

    def report(self):
        return dict(
            enabled=True,
            eligible=self.eligible,
            rejected_reason=self.rejected_reason,
            disabled=self.disabled,
            environment=self.environment,
            first_group=self.topology,
            environment_source="accepted Profile declaration validated against task signature; not live skin/effect measurement",
            calibration=dict(CALIBRATION),
            trigger=self.evidence,
        )
