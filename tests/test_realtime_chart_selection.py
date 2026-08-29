from __future__ import annotations

from types import SimpleNamespace

from agent.realtime.chart_repository import ChartResolution
from agent.realtime.profile_play_action import resolve_local_chart_for_run


class Repository:
    def __init__(self):
        self.calls = []

    def resolve(self, song_id, difficulty):
        self.calls.append((song_id, difficulty))
        return ChartResolution(None, "resolved by fake repository")


def test_chart_selection_requires_fresh_prepared_identity():
    repository = Repository()

    missing = resolve_local_chart_for_run(
        None,
        "Expert",
        repository=repository,
    )
    stale = resolve_local_chart_for_run(
        SimpleNamespace(
            prepared_for_play=False,
            difficulty="Expert",
            song_id="confirmed",
        ),
        "Expert",
        repository=repository,
    )

    assert missing.reason == "no fresh song/difficulty identity"
    assert stale.reason == "no fresh song/difficulty identity"
    assert repository.calls == []


def test_chart_selection_requires_exact_difficulty_and_passes_song_identity():
    repository = Repository()
    live_run = SimpleNamespace(
        prepared_for_play=True,
        difficulty="Expert",
        song_id="song-jacket-phash-v2-0123456789abcdef",
    )

    mismatch = resolve_local_chart_for_run(
        live_run,
        "Hard",
        repository=repository,
    )
    exact = resolve_local_chart_for_run(
        live_run,
        "expert",
        repository=repository,
    )

    assert mismatch.reason == "no fresh song/difficulty identity"
    assert exact.reason == "resolved by fake repository"
    assert repository.calls == [
        ("song-jacket-phash-v2-0123456789abcdef", "expert")
    ]
