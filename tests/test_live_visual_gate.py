import numpy as np

from agent.realtime import live_visual_gate


def blank_screen() -> np.ndarray:
    return np.full((720, 1280, 3), 255, dtype=np.uint8)


def with_tag(color_bgr: tuple[int, int, int]) -> np.ndarray:
    image = blank_screen()
    x0, x1, y0, y1 = live_visual_gate.MODE_TAG_REGION
    image[y0:y1, x0:x1] = color_bgr
    return image


def test_blank_tag_region_is_off():
    assert live_visual_gate.live_performance_mode_is_off(blank_screen())


def test_red_3d_tag_is_not_off():
    # 红底白字“3D演出”标签：BGR 红色。
    assert not live_visual_gate.live_performance_mode_is_off(
        with_tag((0, 0, 255))
    )


def test_pink_mv_tag_is_not_off():
    # FILM LIVE MV 粉色标签。
    assert not live_visual_gate.live_performance_mode_is_off(
        with_tag((180, 120, 240))
    )


def test_small_saturation_noise_stays_off():
    # 少量饱和噪声低于阈值时仍判定为 OFF，避免抗锯齿边缘误判。
    image = blank_screen()
    x0, x1, y0, y1 = live_visual_gate.MODE_TAG_REGION
    image[y0:y0 + 4, x0:x0 + 4] = (0, 0, 255)
    assert live_visual_gate.live_performance_mode_is_off(image)
