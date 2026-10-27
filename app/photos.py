"""Bag photos: checked, re-encoded and written to the private photo dataset (PLAN, "Data model").

The phone already shrinks the photo and drops its EXIF (a canvas re-encode keeps neither). The server doesn't trust
that: it opens the upload with Pillow, accepts only JPEG, PNG or WebP under a size and pixel limit, turns it upright,
scales the long edge to at most 1600 px and saves a fresh JPEG with no metadata at all. Location never reaches the
dataset or Claude.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from datetime import UTC, datetime

from PIL import Image, ImageOps, UnidentifiedImageError

from coffeedata.lake import Lake

MAX_UPLOAD = 12 * 1024 * 1024  # a full-size phone photo, before the phone shrinks it
MAX_PIXELS = 50_000_000  # anything larger is refused before it's decoded (decompression bombs)
LONG_EDGE = 1600
FORMATS = {"JPEG", "PNG", "WEBP"}


class BadPhoto(ValueError):
    """Not a photo we take; the message is shown as is."""


@dataclass(frozen=True)
class Photo:
    jpeg: bytes
    width: int
    height: int
    sha256: str


def normalize(data: bytes) -> Photo:
    if not data:
        raise BadPhoto("The photo was empty. Take it again.")
    if len(data) > MAX_UPLOAD:
        raise BadPhoto("That photo is too large. Take it again with the camera.")
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.format not in FORMATS:
                raise BadPhoto("Use a JPEG, PNG or WebP photo.")
            upright = ImageOps.exif_transpose(im).convert(
                "RGB"
            )  # uses the orientation tag before it's dropped
            upright.thumbnail((LONG_EDGE, LONG_EDGE), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            upright.save(out, "JPEG", quality=85, optimize=True)  # no exif=, no icc_profile=: no metadata
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as e:
        raise BadPhoto("That file isn't a photo we can read.") from e
    jpeg = out.getvalue()
    return Photo(jpeg, upright.width, upright.height, hashlib.sha256(jpeg).hexdigest())


def save(lake: Lake, photo_id: str, user_id: str, photo: Photo) -> None:
    """One Parquet file per photo, in Hugging Face's Image layout, published before returning."""
    now = datetime.now(UTC).replace(microsecond=0)
    lake.write(
        "photos",
        f"photos/{photo_id}.parquet",
        [
            {
                "photo_id": photo_id,
                "user_id": user_id,
                "taken_at": now,
                "image": {"bytes": photo.jpeg, "path": f"{photo_id}.jpg"},
                "media_type": "image/jpeg",
                "width": photo.width,
                "height": photo.height,
                "bytes": len(photo.jpeg),
                "sha256": photo.sha256,
            }
        ],
    )
    lake.publish(f"app: photo {photo_id}")
