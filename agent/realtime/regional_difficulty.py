"""已验证的区服谱面等级差异。

Bestdori 的全局曲目元数据会随区服进度回填，而 CN 客户端可在谱面内容不变时
显示不同的 Expert 等级。这里仅记录已经由实拍、谱面 SHA 和难度共同确认的例外；
不能把它当作一般等级模糊匹配。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VerifiedRegionalDifficulty:
    bestdori_song_id: int
    difficulty: str
    cn_level: int
    source_level: int
    chart_sha256: str


_VERIFIED_CN_EXPERT_LEVELS = (
    VerifiedRegionalDifficulty(
        bestdori_song_id=581,
        difficulty="expert",
        cn_level=27,
        source_level=26,
        chart_sha256=(
            "1f265d0a59a144d9534468391b43ebfd10838ff354c0851d48be68ed452fcbff"
        ),
    ),
    VerifiedRegionalDifficulty(
        bestdori_song_id=705,
        difficulty="expert",
        cn_level=26,
        source_level=27,
        chart_sha256=(
            "fa39d04f14a90a9f838dd5caa36db9d87e3f15fda3ccf800a4a471330b299cc5"
        ),
    ),
)


def verified_level_variants(
    bestdori_song_id: int,
    difficulty: str,
    chart_sha256: str | None,
) -> frozenset[int]:
    """返回同一已验证谱面允许的 CN 与全局等级，未知条目一律为空。"""
    for record in _VERIFIED_CN_EXPERT_LEVELS:
        if (
            record.bestdori_song_id == int(bestdori_song_id)
            and record.difficulty == str(difficulty).strip().casefold()
            and record.chart_sha256 == str(chart_sha256 or "")
        ):
            return frozenset((record.cn_level, record.source_level))
    return frozenset()


def verified_cn_level(
    bestdori_song_id: int,
    difficulty: str,
    chart_sha256: str | None,
) -> int | None:
    """仅在显式记录且 SHA 一致时返回可写入 CN 曲库的等级。"""
    for record in _VERIFIED_CN_EXPERT_LEVELS:
        if (
            record.bestdori_song_id == int(bestdori_song_id)
            and record.difficulty == str(difficulty).strip().casefold()
            and record.chart_sha256 == str(chart_sha256 or "")
        ):
            return record.cn_level
    return None


def catalog_level_metadata(
    *,
    bestdori_song_id: int,
    difficulty: str,
    chart_sha256: str | None,
    source_level: int,
    jacket_server: str,
    old_entry: object | None = None,
) -> dict[str, object]:
    """生成同步写入的默认区服等级与全局源等级。

    同步默认使用 CN 封面。只要同一谱面 SHA 已有 CN 记录，就继续保留它，
    避免全局索引下一次更新再次覆盖本地实际等级；显式验证记录优先于旧格式。
    """
    source_level = int(source_level)
    server = str(jacket_server).strip().casefold()
    old = old_entry if isinstance(old_entry, dict) else {}
    regional_levels: dict[str, int] = {}
    if old.get("chart_sha256") == chart_sha256:
        previous = old.get("regional_levels")
        if isinstance(previous, dict):
            for region, value in previous.items():
                try:
                    regional_levels[str(region).casefold()] = int(value)
                except (TypeError, ValueError):
                    continue
    known_cn = verified_cn_level(
        bestdori_song_id, difficulty, chart_sha256,
    )
    if known_cn is not None:
        regional_levels["cn"] = known_cn
    level = regional_levels.get(server, source_level)
    metadata: dict[str, object] = {
        "level": level,
        "source_level": source_level,
    }
    if regional_levels:
        metadata["regional_levels"] = dict(sorted(regional_levels.items()))
    return metadata
