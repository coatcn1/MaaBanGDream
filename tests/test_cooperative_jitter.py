from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent.realtime.chart_repository import ChartResolution, ChartSelection
from agent.realtime.native_prearm import _cooperative_jittered_chart


def _payload(single_count: int) -> dict:
    chart = [
        {"type": "BPM", "bpm": 160, "beat": 0},
        {"type": "System", "data": "bgm.wav", "beat": 0},
    ]
    for index in range(single_count):
        chart.append({
            "type": "Single",
            "lane": (index % 7) + 1,
            "beat": 2 + index,
        })
    chart.append({"type": "Long", "lane": 3, "beat": 2 + single_count})
    chart.append({"type": "Slide", "lane": 4, "beat": 2 + single_count})
    return {
        "schema_version": 1,
        "source": "test",
        "song": {},
        "difficulty": {},
        "chart": chart,
    }


def test_jitter_drops_one_or_two_singles_and_keeps_first(tmp_path):
    source = tmp_path / "chart.json"
    source.write_text(json.dumps(_payload(12)), encoding="utf-8")
    output = _cooperative_jittered_chart(source, "run-1", tmp_path)
    assert output != source
    assert output.exists()
    data = json.loads(output.read_text(encoding="utf-8"))
    singles = [
        entry for entry in data["chart"] if entry.get("type") == "Single"
    ]
    assert len(singles) in (10, 11)
    # 首音是 photogate 锚点，必须保留。
    assert singles[0]["beat"] == 2
    # 漏键固定放在末尾：剩余单点必须是原谱单点的前缀。
    remaining_beats = [entry["beat"] for entry in singles]
    assert remaining_beats == list(range(2, 2 + len(singles)))
    types = [entry.get("type") for entry in data["chart"]]
    assert types.count("Long") == 1
    assert types.count("Slide") == 1


def test_jitter_drops_only_the_tail_across_runs(tmp_path):
    source = tmp_path / "chart.json"
    source.write_text(json.dumps(_payload(5)), encoding="utf-8")
    drop_counts: set[int] = set()
    for run_index in range(40):
        output = _cooperative_jittered_chart(
            source,
            f"run-{run_index}",
            tmp_path,
        )
        data = json.loads(output.read_text(encoding="utf-8"))
        beats = [
            entry["beat"]
            for entry in data["chart"]
            if entry.get("type") == "Single"
        ]
        missing = set(range(2, 7)) - set(beats)
        assert missing <= {5, 6}
        assert 2 in beats
        drop_counts.add(len(missing))
    assert drop_counts == {1, 2}


def test_jitter_is_deterministic_per_run(tmp_path):
    source = tmp_path / "chart.json"
    source.write_text(json.dumps(_payload(12)), encoding="utf-8")
    first = _cooperative_jittered_chart(source, "run-1", tmp_path)
    second = _cooperative_jittered_chart(source, "run-1", tmp_path)
    assert first == second
    assert first.read_text(encoding="utf-8") == second.read_text(
        encoding="utf-8"
    )


def test_jitter_falls_back_when_no_droppable_single(tmp_path):
    source = tmp_path / "chart.json"
    source.write_text(json.dumps(_payload(1)), encoding="utf-8")
    assert _cooperative_jittered_chart(source, "run-1", tmp_path) == source


def test_jitter_falls_back_on_malformed_chart(tmp_path):
    source = tmp_path / "chart.json"
    source.write_text("not json", encoding="utf-8")
    assert _cooperative_jittered_chart(source, "run-1", tmp_path) == source


def _selection(path: Path) -> ChartSelection:
    return ChartSelection(
        bestdori_song_id=306,
        title="EXIST",
        difficulty="Expert",
        path=path,
        timeline=None,
    )


def test_mode_gate_applies_jitter_to_cooperative_and_versus_only(tmp_path, monkeypatch):
    from agent.realtime import native_prearm

    source = tmp_path / "chart.json"
    source.write_text(json.dumps(_payload(12)), encoding="utf-8")
    (tmp_path / "profiles").mkdir()
    jittered = tmp_path / "debug" / "jittered-charts" / "run-x.json"
    calls = []

    def fake_jitter(chart_path, run_id, root):
        calls.append((Path(chart_path), str(run_id)))
        return jittered

    monkeypatch.setattr(native_prearm, "_cooperative_jittered_chart", fake_jitter)

    class Repo:
        def resolve(self, *_args, **_kwargs):
            return ChartResolution(_selection(source), "ok")

    def resolve(mode):
        calls.clear()
        live = SimpleNamespace(
            prepared_for_play=True,
            difficulty="Expert",
            song_id=306,
            song_level=26,
            song_title="EXIST",
            mode=mode,
            run_id="run-x",
        )
        return native_prearm.resolve_confirmed_chart(
            live, "Expert", repository=Repo(), project_root=tmp_path
        )

    # 竞演与协力都按 run 生成漏键副本。
    assert resolve("versus").selection.path == jittered
    assert calls == [(source, "run-x")]
    assert resolve("cooperative").selection.path == jittered
    assert calls == [(source, "run-x")]
    # 单人正式不抖动，保持 canonical 谱面。
    assert resolve("formal").selection.path == source
    assert calls == []
