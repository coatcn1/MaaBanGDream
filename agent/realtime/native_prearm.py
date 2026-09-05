"""Native 开演前预武装的单次交接缓存。"""

from __future__ import annotations

import hashlib
import json
import os
import random
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from .chart_repository import (
    ChartResolution,
    LocalChartRepository,
)


DEFAULT_TTL_SECONDS = 30.0
DEFAULT_READY_TIMEOUT_SECONDS = 10.0


class NativePrearmError(RuntimeError):
    """预武装缓存缺失、过期或身份不匹配。"""


@dataclass(frozen=True, slots=True)
class NativePrearmKey:
    run_id: str
    chart_path: str


@dataclass(slots=True)
class _PrearmEntry:
    key: NativePrearmKey
    backend: Any
    expires_at: float
    timer: Any


def _normalized_chart_path(chart_path: str | Path) -> str:
    resolved = Path(chart_path).resolve(strict=False)
    return os.path.normcase(str(resolved))


def _prearm_key(run_id: str, chart_path: str | Path) -> NativePrearmKey:
    value = str(run_id).strip()
    if not value:
        raise ValueError("run_id 不能为空")
    return NativePrearmKey(value, _normalized_chart_path(chart_path))


class NativePrearmManager:
    """线程安全的单槽、单次消费 Native 预武装缓存。"""

    def __init__(
        self,
        *,
        ttl_s: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        timer_factory: Callable[[float, Callable[[], None]], Any] = (
            threading.Timer
        ),
    ) -> None:
        if ttl_s <= 0:
            raise ValueError("ttl_s 必须大于 0")
        self._ttl_s = float(ttl_s)
        self._clock = clock
        self._timer_factory = timer_factory
        self._lock = threading.RLock()
        self._entry: _PrearmEntry | None = None

    @staticmethod
    def _stop_backend(backend: Any, reason: str) -> None:
        try:
            backend.stop()
        except Exception as exc:  # noqa: BLE001 - watchdog 不能抛出到线程边界
            print(
                "NativePrearm cleanup_failed "
                f"reason={reason} error={type(exc).__name__}: {exc}",
                flush=True,
            )

    def _expire(self, entry: _PrearmEntry) -> None:
        with self._lock:
            if self._entry is not entry:
                return
            self._entry = None
        self._stop_backend(entry.backend, "ttl-expired")
        print(
            "NativePrearm expired "
            f"run_id={entry.key.run_id} chart={entry.key.chart_path}",
            flush=True,
        )

    def prepare(
        self,
        run_id: str,
        chart_path: str | Path,
        backend: Any,
        *,
        ttl_s: float | None = None,
    ) -> NativePrearmKey:
        """缓存已 ready 的后端；同时只保留最新一个交接项。"""
        try:
            lifetime = self._ttl_s if ttl_s is None else float(ttl_s)
            if lifetime <= 0:
                raise ValueError("ttl_s 必须大于 0")
            key = _prearm_key(run_id, chart_path)
            entry: _PrearmEntry

            def expire() -> None:
                self._expire(entry)

            timer = self._timer_factory(lifetime, expire)
            if hasattr(timer, "daemon"):
                timer.daemon = True
            entry = _PrearmEntry(
                key=key,
                backend=backend,
                expires_at=self._clock() + lifetime,
                timer=timer,
            )
        except Exception:
            self._stop_backend(backend, "cache-prepare-failed")
            raise
        try:
            with self._lock:
                previous = self._entry
                self._entry = entry
                # 发布缓存和启动 watchdog 必须在同一临界区，避免消费者
                # 取走尚未启动定时器的后端，或启动失败后误停已消费后端。
                try:
                    timer.start()
                except Exception:
                    self._entry = previous
                    raise
        except Exception:
            self._stop_backend(backend, "watchdog-start-failed")
            raise
        if previous is not None:
            previous.timer.cancel()
            self._stop_backend(previous.backend, "replaced")
        print(
            "NativePrearm prepared "
            f"run_id={key.run_id} chart={key.chart_path} ttl_s={lifetime:g}",
            flush=True,
        )
        return key

    def consume(self, run_id: str, chart_path: str | Path) -> Any:
        """原子消费完全匹配的预武装后端。"""
        expected = _prearm_key(run_id, chart_path)
        cleanup: _PrearmEntry | None = None
        failure: str | None = None
        with self._lock:
            entry = self._entry
            if entry is None:
                raise NativePrearmError("Native 预武装不存在或已被消费")
            if self._clock() >= entry.expires_at:
                self._entry = None
                cleanup = entry
                failure = "Native 预武装已过期"
            elif entry.key != expected:
                self._entry = None
                cleanup = entry
                failure = (
                    "Native 预武装与当前 run_id 或谱面不匹配"
                )
            else:
                self._entry = None
                entry.timer.cancel()
                print(
                    "NativePrearm consumed "
                    f"run_id={expected.run_id} chart={expected.chart_path}",
                    flush=True,
                )
                return entry.backend
        assert cleanup is not None and failure is not None
        cleanup.timer.cancel()
        self._stop_backend(cleanup.backend, "consume-rejected")
        raise NativePrearmError(failure)

    def discard(self, reason: str = "discarded") -> bool:
        """清理未消费后端；已消费后端不再归 manager 所有。"""
        with self._lock:
            entry = self._entry
            self._entry = None
        if entry is None:
            return False
        entry.timer.cancel()
        self._stop_backend(entry.backend, reason)
        return True


_GLOBAL_MANAGER = NativePrearmManager()


def _cooperative_jittered_chart(
    chart_path: Path,
    run_id: str,
    root: Path,
) -> Path:
    """协力模式按 run_id 确定性漏掉 1~2 个普通单点，避免整局全 P。

    首音是 photogate 的时钟锚点，漏掉首音会破坏整局节奏，因此漏键固定
    放在整曲末尾的最后 1~2 个单点，中段节奏不受影响。漏 1 个还是 2 个
    由 run_id 决定，同一次 run 内两次解析结果一致，保证预武装与正式
    消费拿到同一份谱面。
    """
    try:
        payload = json.loads(Path(chart_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Path(chart_path)
    chart = payload.get("chart")
    if not isinstance(chart, list):
        return Path(chart_path)
    singles = [
        index
        for index, entry in enumerate(chart)
        if isinstance(entry, dict) and entry.get("type") == "Single"
    ]
    candidates = singles[1:]
    if not candidates:
        return Path(chart_path)
    rng = random.Random(hashlib.sha1(run_id.encode("utf-8")).digest())
    drops = min(len(candidates), rng.randint(1, 2))
    # 从全部单点的末尾取最后 1~2 个；candidates 已排除首音，因此
    # 首音不会被删除，删除下标降序保证原列表删除安全。
    dropped = sorted(candidates[-drops:], reverse=True)
    jittered = json.loads(json.dumps(payload, ensure_ascii=False))
    for index in dropped:
        del jittered["chart"][index]
    output_dir = root / "debug" / "jittered-charts"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{run_id}.json"
    output_path.write_text(
        json.dumps(jittered, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"NativeCooperativeJitter run_id={run_id} dropped={drops} "
        f"singles={len(singles)}",
        flush=True,
    )
    return output_path


def resolve_confirmed_chart(
    live_run: Any,
    difficulty: str,
    *,
    repository: LocalChartRepository | None = None,
    project_root: str | Path | None = None,
) -> ChartResolution:
    """只接受本轮准备页已确认的歌曲与难度。"""
    if live_run is None or not bool(live_run.prepared_for_play):
        return ChartResolution(None, "no fresh song/difficulty identity")
    if str(live_run.difficulty).strip().lower() != str(difficulty).strip().lower():
        return ChartResolution(None, "no fresh song/difficulty identity")
    root = (
        Path(project_root)
        if project_root is not None
        else Path(__file__).resolve().parents[2]
    )
    repository = repository or LocalChartRepository(root / "resource" / "charts")
    resolution = repository.resolve(
        live_run.song_id,
        difficulty,
        level=getattr(live_run, "song_level", None),
        title=getattr(live_run, "song_title", None),
    )
    selection = resolution.selection
    mode = str(getattr(live_run, "mode", "") or "").lower()
    if selection is not None and mode == "cooperative":
        from .profile_store import RealtimeProfileStore

        runtime_options = RealtimeProfileStore(
            root / "profiles"
        ).runtime_options()
        if bool(runtime_options.get("cooperative_jitter_enabled", True)):
            jittered_path = _cooperative_jittered_chart(
                selection.path,
                str(getattr(live_run, "run_id", "")),
                root,
            )
            if jittered_path != selection.path:
                selection = replace(selection, path=jittered_path)
                resolution = ChartResolution(selection, resolution.reason)
    return resolution


def controller_adb_endpoint(controller: Any) -> tuple[str, str]:
    """从 Maa controller 取真实 ADB 端点，缺失时禁止猜测。"""
    try:
        info = dict(controller.info or {})
    except Exception as exc:
        raise RuntimeError(
            "Native 需要 controller.info 中的 adb_path 和 adb_serial"
        ) from exc
    adb_path = str(info.get("adb_path") or "").strip()
    adb_serial = str(info.get("adb_serial") or "").strip()
    if not adb_path or not adb_serial:
        raise RuntimeError(
            "Native 需要 controller.info 中的 adb_path 和 adb_serial"
        )
    return adb_path, adb_serial


def prepare_native_for_settings_gate(
    *,
    controller: Any,
    live_run: Any,
    difficulty: str,
    project_root: str | Path,
    runtime_options: dict[str, Any] | None = None,
    repository: LocalChartRepository | None = None,
    backend_factory: Callable[..., Any] | None = None,
    manager: NativePrearmManager = _GLOBAL_MANAGER,
    ready_timeout_s: float = DEFAULT_READY_TIMEOUT_SECONDS,
    ttl_s: float = DEFAULT_TTL_SECONDS,
) -> Any | None:
    """在 SettingsGate 成功后编译、武装并缓存 Native 后端。"""
    root = Path(project_root)
    if runtime_options is None:
        from .profile_store import RealtimeProfileStore

        runtime_options = RealtimeProfileStore(
            root / "profiles"
        ).runtime_options()
    if not bool(runtime_options.get("native_realtime_enabled", False)):
        return None

    manager.discard("new-prearm-attempt")
    resolution = resolve_confirmed_chart(
        live_run,
        difficulty,
        repository=repository,
        project_root=root,
    )
    selection = resolution.selection
    if selection is None:
        # Easy/Normal 未收录本地谱面，或身份/等级冲突时没有可靠谱面；
        # 本局必须整体回退 Legacy 视觉演奏，不能在预武装阶段把任务打停。
        print(
            "NativePrearm skipped=true reason=no-reliable-local-chart "
            f"detail={resolution.reason}",
            flush=True,
        )
        return None
    adb_path, adb_serial = controller_adb_endpoint(controller)
    if backend_factory is None:
        from .native_play import NativeMinitouchBackend

        backend_factory = NativeMinitouchBackend
    backend = backend_factory(
        selection.path,
        adb_path=adb_path,
        serial=adb_serial,
        # y=565 是视觉预判线；Native 直触必须与 Legacy/上游一致落在 y=590。
        judgement_y=590,
        press_bias_ms=0,
        start_gate_mode=str(
            getattr(live_run, "mode", None) or "realtime"
        ),
        run_id=live_run.run_id,
        jlog_path=(
            root / "debug" / "native-jlog" / f"{live_run.run_id}.jsonl"
        ),
    )
    try:
        backend.arm()
        if not bool(backend.wait_until_ready(float(ready_timeout_s))):
            raise RuntimeError("Native 预武装等待 minitouch ready 失败")
    except Exception as exc:
        NativePrearmManager._stop_backend(backend, "prepare-failed")
        raise RuntimeError(
            "Native 预武装失败："
            f"{type(exc).__name__}: {exc}"
        ) from exc
    manager.prepare(
        live_run.run_id,
        selection.path,
        backend,
        ttl_s=float(ttl_s),
    )
    return selection


def consume_prearmed_backend(run_id: str, chart_path: str | Path) -> Any:
    return _GLOBAL_MANAGER.consume(run_id, chart_path)


def discard_prearmed_backend(reason: str = "discarded") -> bool:
    return _GLOBAL_MANAGER.discard(reason)
