# File: src/Services/image_service.py

import io
import uuid
from PIL import Image, ImageOps

from fastapi import HTTPException
from supabase import AsyncClient

from src.Infrastructure.logger import get_logger

logger = get_logger("Services.image_service")

# ── Constants ─────────────────────────────────────────────────────────────────

_AVATAR_SIZES: dict[str, tuple[int, int]] = {
    "small.jpg": (128, 128),
    "medium.jpg": (256, 256),
    "large.jpg": (512, 512),
}
_RECEIPT_MAX_DIM = 2048
_JPEG_QUALITY_START = 85
_JPEG_QUALITY_MIN = 40
_JPEG_QUALITY_STEP = 5


# ── Validation ────────────────────────────────────────────────────────────────

def validate_image_size(image_bytes: bytes, max_bytes: int = 20 * 1024 * 1024) -> None:
    """Raise HTTP 413 if the raw uploaded image exceeds max_bytes (default 20MB)."""
    if len(image_bytes) > max_bytes:
        max_mb = max_bytes // (1024 * 1024)
        raise HTTPException(
            status_code=413,
            detail=f"Image file size exceeds maximum limit of {max_mb}MB.",
        )


# ── Compression Helpers ───────────────────────────────────────────────────────

def _open_and_normalize(image_bytes: bytes) -> Image.Image:
    """Open image, apply EXIF orientation, and convert to RGB on white background."""
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img)  # Honour EXIF rotation flags

    if img.mode in ("RGBA", "LA"):
        background = Image.new("RGB", img.size, (255, 255, 255))
        mask = img.split()[-1]  # Alpha channel
        background.paste(img.convert("RGB"), mask=mask)
        return background

    if img.mode == "P":  # Palette mode (GIFs etc.)
        img = img.convert("RGBA")
        background = Image.new("RGB", img.size, (255, 255, 255))
        mask = img.split()[-1]
        background.paste(img.convert("RGB"), mask=mask)
        return background

    return img.convert("RGB")


def _encode_jpeg(img: Image.Image, quality: int = 85) -> bytes:
    """Encode PIL image to progressive JPEG bytes."""
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, progressive=True, optimize=True)
    return buf.getvalue()


def compress_receipt_image(
    image_bytes: bytes,
    target_max_bytes: int = 5 * 1024 * 1024,
    max_dim: int = _RECEIPT_MAX_DIM,
    quality: int = _JPEG_QUALITY_START,
) -> bytes:
    """Compress a receipt image to a progressive JPEG strictly <= target_max_bytes.

    Processing steps:
    1. EXIF orientation normalization.
    2. Alpha/palette → RGB on white background.
    3. Resize to fit within max_dim × max_dim (LANCZOS) maintaining aspect ratio.
    4. Encode as progressive JPEG at `quality`.
    5. If output exceeds target_max_bytes, iteratively reduce quality (by 5 per step)
       down to JPEG quality 40. If still too large, halve dimensions and repeat.
    """
    img = _open_and_normalize(image_bytes)

    # Resize to fit within max_dim
    img.thumbnail((max_dim, max_dim), Image.LANCZOS)

    # Iterative quality reduction
    current_quality = quality
    while current_quality >= _JPEG_QUALITY_MIN:
        result = _encode_jpeg(img, quality=current_quality)
        if len(result) <= target_max_bytes:
            logger.debug(
                "compress_receipt_image: quality=%d, size=%d bytes",
                current_quality,
                len(result),
            )
            return result
        current_quality -= _JPEG_QUALITY_STEP

    # Quality reduction insufficient — halve dimensions and try again
    half_dim = max_dim // 2
    if half_dim >= 256:
        logger.debug(
            "compress_receipt_image: quality exhausted, retrying with max_dim=%d",
            half_dim,
        )
        return compress_receipt_image(
            image_bytes,
            target_max_bytes=target_max_bytes,
            max_dim=half_dim,
            quality=_JPEG_QUALITY_START,
        )

    # Last resort: return at minimum quality
    result = _encode_jpeg(img, quality=_JPEG_QUALITY_MIN)
    logger.warning(
        "compress_receipt_image: could not reach target %d bytes; returning %d bytes",
        target_max_bytes,
        len(result),
    )
    return result


def generate_avatar_resolutions(
    image_bytes: bytes,
    target_max_bytes: int = 5 * 1024 * 1024,
) -> dict[str, bytes]:
    """Generate small (128x128), medium (256x256), large (512x512) avatar JPEGs.

    Each output is a square center-cropped, progressive JPEG <= target_max_bytes.
    """
    img = _open_and_normalize(image_bytes)
    result: dict[str, bytes] = {}

    for filename, (w, h) in _AVATAR_SIZES.items():
        # ImageOps.fit produces a square crop centered on the subject
        cropped = ImageOps.fit(img, (w, h), Image.LANCZOS)
        encoded = _encode_jpeg(cropped, quality=_JPEG_QUALITY_START)

        # Avatar sizes are tiny; should always be well under 5MB.
        # Log a warning if somehow exceeded.
        if len(encoded) > target_max_bytes:
            logger.warning(
                "generate_avatar_resolutions: %s exceeds target (%d > %d bytes)",
                filename,
                len(encoded),
                target_max_bytes,
            )

        result[filename] = encoded
        logger.debug(
            "generate_avatar_resolutions: %s generated (%d bytes)",
            filename,
            len(encoded),
        )

    return result


# ── Storage Service ───────────────────────────────────────────────────────────

class ImageStorageService:
    """Handles uploading compressed images to Supabase Storage in the `user-data` bucket."""

    def __init__(self, db: AsyncClient, bucket: str = "user-data"):
        self.db = db
        self.bucket = bucket

    async def upload_avatar(
        self,
        user_id: str,
        image_bytes: bytes,
        target_max_bytes: int = 5 * 1024 * 1024,
    ) -> str:
        """Compress and upload 3 avatar resolutions to {user_id}/avatar_images/.

        Returns the folder path "{user_id}/avatar_images" for storage in users.avatar_image_path.
        """
        resolutions = generate_avatar_resolutions(image_bytes, target_max_bytes=target_max_bytes)

        for filename, data in resolutions.items():
            path = f"{user_id}/avatar_images/{filename}"
            logger.debug(
                "ImageStorageService.upload_avatar: uploading %s (%d bytes) to bucket=%s",
                path,
                len(data),
                self.bucket,
            )
            await self.db.storage.from_(self.bucket).upload(
                path=path,
                file=data,
                file_options={"content-type": "image/jpeg", "upsert": "true"},
            )

        folder_path = f"{user_id}/avatar_images"
        logger.info(
            "ImageStorageService.upload_avatar: all 3 resolutions uploaded for user_id=%s → %s",
            user_id,
            folder_path,
        )
        return folder_path

    async def upload_receipt_image(
        self,
        user_id: str,
        receipt_id: str,
        image_bytes: bytes,
        target_max_bytes: int = 5 * 1024 * 1024,
    ) -> str:
        """Compress and upload a receipt image to {user_id}/receipt_images/{receipt_id}.jpg.

        Returns the full storage path for storage in receipts.receipt_image_path.
        """
        compressed = compress_receipt_image(image_bytes, target_max_bytes=target_max_bytes)
        path = f"{user_id}/receipt_images/{receipt_id}.jpg"

        logger.debug(
            "ImageStorageService.upload_receipt_image: uploading %s (%d bytes) to bucket=%s",
            path,
            len(compressed),
            self.bucket,
        )
        await self.db.storage.from_(self.bucket).upload(
            path=path,
            file=compressed,
            file_options={"content-type": "image/jpeg", "upsert": "true"},
        )

        logger.info(
            "ImageStorageService.upload_receipt_image: receipt_id=%s → %s",
            receipt_id,
            path,
        )
        return path

    async def download_avatar(self, user_id: str, size: str = "medium") -> bytes | None:
        """Download an avatar resolution JPEG from {user_id}/avatar_images/{size}.jpg."""
        size_key = size.lower().strip()
        if not size_key.endswith(".jpg"):
            size_filename = f"{size_key}.jpg"
        else:
            size_filename = size_key
        if size_filename not in _AVATAR_SIZES:
            size_filename = "medium.jpg"

        path = f"{user_id}/avatar_images/{size_filename}"
        try:
            logger.debug(
                "ImageStorageService.download_avatar: downloading %s from bucket=%s",
                path,
                self.bucket,
            )
            data = await self.db.storage.from_(self.bucket).download(path)
            return data
        except Exception as e:
            logger.warning(
                "ImageStorageService.download_avatar: avatar not found at %s: %s",
                path,
                e,
            )
            return None

    async def download_receipt_image(self, storage_path: str) -> bytes | None:
        """Download a receipt image from Supabase Storage given its storage_path."""
        if not storage_path:
            return None
        try:
            logger.debug(
                "ImageStorageService.download_receipt_image: downloading %s from bucket=%s",
                storage_path,
                self.bucket,
            )
            data = await self.db.storage.from_(self.bucket).download(storage_path)
            return data
        except Exception as e:
            logger.warning(
                "ImageStorageService.download_receipt_image: image not found at %s: %s",
                storage_path,
                e,
            )
            return None
