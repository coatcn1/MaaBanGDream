import cv2
import numpy as np
import pytest
from agent.realtime.native_judgement_grade import ASSETS, OrdinaryVisualExtractor
from agent.realtime.vision_io import imread_unicode


@pytest.mark.parametrize("grade", ["GREAT", "PERFECT"])
@pytest.mark.parametrize("scale", [.60, .725, .85])
def test_complete_centered_grade_matches_without_modifying_image(grade, scale):
    sprite = imread_unicode(ASSETS / ("judge_" + grade.lower() + ".png"), cv2.IMREAD_UNCHANGED)
    sprite = cv2.resize(sprite, None, fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR)
    h, w = sprite.shape[:2]
    image = np.full((720, 1280, 3), 30, np.uint8)
    x, y = int(round(640 - w / 2)), int(round(514 - h / 2))
    alpha = sprite[:, :, 3:4].astype(np.float32) / 255
    image[y:y+h, x:x+w] = (sprite[:, :, :3] * alpha + 30 * (1 - alpha)).astype(np.uint8)
    before = image.copy()
    assert OrdinaryVisualExtractor().scores(image)["category"] == grade
    assert np.array_equal(image, before)


def test_blank_partial_or_wrong_resolution_cannot_supply_grade():
    extractor = OrdinaryVisualExtractor()
    assert extractor.scores(np.zeros((720, 1280, 3), np.uint8))["category"] == "unknown"
    assert extractor.scores(np.zeros((360, 640, 3), np.uint8))["category"] == "unknown"
