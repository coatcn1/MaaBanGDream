from __future__ import annotations

import json
import time
import traceback
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np

try:
    from ..foreground_guard import require_game_foreground
except ImportError:  # AgentServer imports realtime as a top-level package.
    from foreground_guard import require_game_foreground

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

from .chart_repository import ChartResolution, LocalChartRepository
from .live_session import reset_live_run, update_live_run
from .song_identity import SongIdentity, UNKNOWN_SONG_ID, identify_song
from .song_title_ocr import recognize_song_title


DIFFICULTY_TARGETS = {
    "Easy": (715, 545),
    "Normal": (827, 545),
    "Hard": (940, 545),
    "Expert": (1051, 545),
    "Special": (1180, 545),
}
SONG_LEVEL_ROI = (1194, 448, 52, 38)
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _normalized_digit(mask: np.ndarray) -> np.ndarray | None:
    ys, xs = np.where(mask > 0)
    if not len(xs):
        return None
    cropped = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    height, width = cropped.shape
    scale = min(24.0 / height, 16.0 / width)
    target_width = max(1, int(round(width * scale)))
    target_height = max(1, int(round(height * scale)))
    resized = cv2.resize(
        cropped,
        (target_width, target_height),
        interpolation=cv2.INTER_NEAREST,
    )
    normalized = np.zeros((32, 24), dtype=np.uint8)
    top = (32 - target_height) // 2
    left = (24 - target_width) // 2
    normalized[top:top + target_height, left:left + target_width] = resized
    return normalized


@lru_cache(maxsize=1)
def _digit_prototypes() -> dict[int, tuple[np.ndarray, ...]]:
    fonts = (
        cv2.FONT_HERSHEY_SIMPLEX,
        cv2.FONT_HERSHEY_DUPLEX,
        cv2.FONT_HERSHEY_COMPLEX,
        cv2.FONT_HERSHEY_TRIPLEX,
        cv2.FONT_HERSHEY_COMPLEX_SMALL,
    )
    prototypes: dict[int, list[np.ndarray]] = {digit: [] for digit in range(10)}
    for digit in range(10):
        for font in fonts:
            for scale in (0.65, 0.75, 0.85, 0.95, 1.05):
                for thickness in (1, 2, 3):
                    canvas = np.zeros((48, 40), dtype=np.uint8)
                    cv2.putText(
                        canvas,
                        str(digit),
                        (2, 38),
                        font,
                        scale,
                        255,
                        thickness,
                        cv2.LINE_AA,
                    )
                    normalized = _normalized_digit(
                        np.where(canvas > 80, 255, 0).astype(np.uint8)
                    )
                    if normalized is not None:
                        prototypes[digit].append(normalized)
    return {digit: tuple(values) for digit, values in prototypes.items()}


def _digit_shape_scores(glyph: np.ndarray) -> list[tuple[float, int]]:
    query = _normalized_digit(glyph)
    if query is None:
        return []
    query_distance = cv2.distanceTransform(
        (query == 0).astype(np.uint8), cv2.DIST_L2, 3
    )
    scores: list[tuple[float, int]] = []
    for digit, prototypes in _digit_prototypes().items():
        best = float("inf")
        for prototype in prototypes:
            prototype_distance = cv2.distanceTransform(
                (prototype == 0).astype(np.uint8), cv2.DIST_L2, 3
            )
            score = float(
                (
                    query_distance[prototype > 0].mean()
                    + prototype_distance[query > 0].mean()
                )
                / 2.0
            )
            best = min(best, score)
        scores.append((best, digit))
    return sorted(scores)


def read_song_level(
    image,
    roi: tuple[int, int, int, int] = SONG_LEVEL_ROI,
) -> int | None:
    """Read the selected chart level using a strict local shape classifier."""
    if not isinstance(image, np.ndarray) or image.ndim != 3:
        return None
    x, y, width, height = map(int, roi)
    if image.shape[0] < y + height or image.shape[1] < x + width:
        return None
    gray = cv2.cvtColor(image[y:y + height, x:x + width], cv2.COLOR_BGR2GRAY)
    mask = np.where(gray < 140, 255, 0).astype(np.uint8)
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    glyphs: list[tuple[int, np.ndarray]] = []
    for component in range(1, count):
        left, top, glyph_width, glyph_height, area = map(
            int, stats[component]
        )
        if not (
            3 <= glyph_width <= 20
            and 10 <= glyph_height <= 28
            and 20 <= area <= 260
        ):
            continue
        glyphs.append((
            left,
            mask[top:top + glyph_height, left:left + glyph_width],
        ))
    glyphs.sort(key=lambda item: item[0])
    if not 1 <= len(glyphs) <= 2:
        return None
    digits: list[int] = []
    for _, glyph in glyphs:
        scores = _digit_shape_scores(glyph)
        if len(scores) < 2:
            return None
        (best_score, digit), (runner_up, _) = scores[:2]
        score_margin = runner_up - best_score
        score_ratio = runner_up / max(best_score, 0.05)
        if (
            best_score > 0.65
            or score_margin < 0.04
            or score_ratio < 1.22
        ):
            return None
        digits.append(digit)
    level = int("".join(str(digit) for digit in digits))
    return level if 5 <= level <= 40 else None


def resolve_chart_for_selected_song(
    song_id: str,
    difficulty: str,
    song_level: int | None,
    song_title: str | None,
) -> ChartResolution:
    try:
        return LocalChartRepository(
            PROJECT_ROOT / "resource" / "charts"
        ).resolve(
            song_id,
            difficulty,
            level=song_level,
            title=song_title,
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return ChartResolution(
            None,
            f"local chart repository invalid: {type(exc).__name__}: {exc}",
        )


def _should_retry_song_identity(resolution: ChartResolution) -> bool:
    if resolution.selection is not None:
        return False
    reason = str(resolution.reason).casefold()
    return any(
        marker in reason
        for marker in (
            "ambiguous",
            "selected song level does not match",
            "song fingerprint is unknown",
            "song title is not confirmed",
        )
    )


def selected_difficulty(
    image,
    targets: dict[str, tuple[int, int]] = DIFFICULTY_TARGETS,
) -> str | None:
    """Return the coloured difficulty button on the 1280x720 song screen."""
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    scores = {}
    for name, (x, y) in targets.items():
        roi = hsv[max(0, y - 30):y + 20, max(0, x - 25):x + 25]
        scores[name] = float(roi[:, :, 1].mean()) if roi.size else 0.0
    winner = max(scores, key=scores.get)
    return winner if scores[winner] >= 50.0 else None


@AgentServer.custom_action("RealtimeDifficultySelect")
class RealtimeDifficultySelect(CustomAction):
    """Select and confirm difficulty before the start button can be clicked."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            params = json.loads(argv.custom_action_param or "{}")
            requested = str(params.get("difficulty", "Easy"))
            if requested not in DIFFICULTY_TARGETS:
                raise ValueError(f"unsupported difficulty: {requested}")
            attempts = int(params.get("max_attempts", 3))
            if context.tasker.stopping:
                return True
            reset_live_run(
                mode=str(params.get("mode", "realtime")),
                difficulty=requested,
                profile_name=params.get("profile_name"),
                expected_note_speed=params.get("note_speed"),
                debug_recording=bool(params.get("debug_recording", False)),
            )
            controller = context.tasker.controller
            configured_targets = params.get("difficulty_targets", DIFFICULTY_TARGETS)
            targets = {
                str(name): tuple(int(value) for value in point)
                for name, point in configured_targets.items()
            }
            if requested not in targets:
                raise ValueError(f"missing difficulty target: {requested}")
            target = targets[requested]
            level_roi = tuple(
                int(value)
                for value in params.get("song_level_roi", SONG_LEVEL_ROI)
            )
            title_roi_value = params.get("song_title_roi")
            title_roi = (
                tuple(int(value) for value in title_roi_value)
                if title_roi_value is not None else None
            )
            use_song_identity = bool(params.get("song_identity", True))
            for attempt in range(1, attempts + 1):
                if context.tasker.stopping:
                    return True
                require_game_foreground(controller)
                controller.post_click(*target).wait()
                time.sleep(float(params.get("verify_delay_seconds", 0.35)))
                image = controller.post_screencap().wait().get()
                recognized = selected_difficulty(image, targets)
                print(
                    f"RealtimeDifficultySelect requested={requested} "
                    f"target={target} attempt={attempt}/{attempts} "
                    f"recognized={recognized}",
                    flush=True,
                )
                if recognized == requested:
                    identity_attempts = max(
                        1,
                        int(params.get("identity_read_attempts", 4)),
                    )
                    identity_delay = max(
                        0.0,
                        float(params.get(
                            "identity_retry_delay_seconds", 0.25,
                        )),
                    )
                    identity_image = image
                    best_reading = None
                    for identity_attempt in range(1, identity_attempts + 1):
                        identity = (
                            identify_song(identity_image)
                            if use_song_identity
                            else SongIdentity(UNKNOWN_SONG_ID, "unknown")
                        )
                        song_level = read_song_level(identity_image, level_roi)
                        title_reading = None
                        try:
                            title_reading = recognize_song_title(
                                identity_image,
                                **({"roi": title_roi} if title_roi is not None else {}),
                            )
                        except (OSError, ValueError, RuntimeError) as exc:
                            print(
                                "RealtimeDifficultySelect title_ocr=unavailable "
                                f"reason={type(exc).__name__}: {exc}",
                                flush=True,
                            )
                        song_title = (
                            title_reading.text
                            if title_reading is not None else None
                        )
                        chart_resolution = resolve_chart_for_selected_song(
                            identity.song_id,
                            requested,
                            song_level,
                            song_title,
                        )
                        reading_score = (
                            chart_resolution.selection is not None,
                            song_level is not None,
                            song_title is not None,
                            identity.method != "unknown",
                            float(getattr(
                                title_reading, "confidence", 0.0,
                            )),
                        )
                        if (
                            best_reading is None
                            or reading_score > best_reading[0]
                        ):
                            best_reading = (
                                reading_score,
                                identity,
                                song_level,
                                title_reading,
                                chart_resolution,
                            )
                        print(
                            "RealtimeDifficultySelect identity_read "
                            f"attempt={identity_attempt}/{identity_attempts} "
                            f"song={identity.song_id} "
                            f"song_level={song_level} "
                            f"song_title={song_title!r} "
                            f"chart_preflight={chart_resolution.reason}",
                            flush=True,
                        )
                        if not _should_retry_song_identity(chart_resolution):
                            break
                        if identity_attempt >= identity_attempts:
                            break
                        if context.tasker.stopping:
                            return True
                        time.sleep(identity_delay)
                        identity_image = (
                            controller.post_screencap().wait().get()
                        )

                    assert best_reading is not None
                    (
                        _score,
                        identity,
                        song_level,
                        title_reading,
                        chart_resolution,
                    ) = best_reading
                    song_title = (
                        title_reading.text
                        if title_reading is not None else None
                    )
                    update_live_run(
                        song_id=identity.song_id,
                        song_id_method=identity.method,
                        song_level=song_level,
                        song_title=song_title,
                        song_title_confidence=(
                            title_reading.confidence
                            if title_reading is not None else None
                        ),
                        prepared_for_play=True,
                    )
                    print(
                        "RealtimeDifficultySelect "
                        f"song={identity.song_id} method={identity.method} "
                        f"song_level={song_level} "
                        f"song_title={song_title!r} "
                        f"title_confidence={getattr(title_reading, 'confidence', None)} "
                        f"chart_preflight={chart_resolution.reason}",
                        flush=True,
                    )
                    return True
            print(
                f"RealtimeDifficultySelect failed requested={requested} "
                f"target={target} attempts={attempts}",
                flush=True,
            )
            return False
        except Exception as exc:
            traceback.print_exc()
            print(f"RealtimeDifficultySelect failed={type(exc).__name__}: {exc}", flush=True)
            return False
