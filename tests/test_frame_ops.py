import numpy as np

from joy.capture.frame_ops import (
    apply_crop,
    average_hash,
    hamming,
    is_mostly_black,
    resize_max_side,
    to_jpeg_bytes,
)
from joy.core.config import CropRatio


def test_crop_and_hash() -> None:
    rgb = np.zeros((100, 200, 3), dtype=np.uint8)
    rgb[:, :, 0] = 40
    cropped = apply_crop(rgb, CropRatio(left=0.1, top=0.1, right=0.1, bottom=0.1))
    assert cropped.shape[0] == 80
    assert cropped.shape[1] == 160
    h1 = average_hash(cropped)
    h2 = average_hash(cropped)
    assert h1 == h2
    assert hamming(h1, h2) == 0


def test_resize_and_jpeg_in_memory() -> None:
    rgb = np.random.randint(0, 255, (900, 1600, 3), dtype=np.uint8)
    small = resize_max_side(rgb, 1280)
    assert max(small.shape[0], small.shape[1]) <= 1280
    data = to_jpeg_bytes(small, quality=80)
    assert data[:2] == b"\xff\xd8"
    assert not is_mostly_black(small)
