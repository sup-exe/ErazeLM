#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import fitz  # PyMuPDF
import argparse
import os
import logging
import cv2
import numpy as np
import zipfile
import shutil
import tempfile
from typing import Optional, List
from dataclasses import dataclass
from tqdm import tqdm
from PIL import Image
import io

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
#  Configuration                                                       #
# ------------------------------------------------------------------ #

@dataclass
class WatermarkConfig:
    """Configuration for watermark detection and removal."""
    # Search margins from the bottom-right corner (PDF pts / image px)
    search_margin_x: int = 300
    search_margin_y: int = 65

    # Padding around the tight watermark bbox before inpainting (pixels)
    watermark_padding: int = 8

    # Threshold for median-blur difference detection
    pixel_threshold: int = 30

    # PDF rendering scale factor (higher = better quality but slower)
    pdf_dpi_scale: float = 3.0

    # Inpainting radius for cv2.inpaint
    inpaint_radius: int = 5

    # Minimum number of watermark components (icon + text may merge into 1)
    min_watermark_components: int = 1

    # Minimum total pixel area of selected watermark components
    min_watermark_area: int = 800

    # Per-component area threshold (filters tiny noise)
    min_component_area: int = 200


# ------------------------------------------------------------------ #
#  Core engine                                                         #
# ------------------------------------------------------------------ #

class WatermarkRemover:
    """ErazeLM: Removes NotebookLM watermarks from PDFs, images, and PPTX files."""

    WATERMARK_TEXT = "NotebookLM"

    def __init__(self, config: WatermarkConfig = WatermarkConfig()):
        self.config = config

    # ---------- detection helpers ---------- #

    def _build_watermark_mask(
        self, roi_bgr: np.ndarray
    ) -> Optional[np.ndarray]:
        """
        Build a pixel-precise inpainting mask that covers ONLY the watermark
        component pixels inside *roi_bgr*.

        The watermark (icon + "NotebookLM" text) always sits in the
        bottom-right area of the search ROI.  Other sharp features in the
        ROI — slide borders, grid dots, decorative elements — are ignored
        because they fail the position / size filters.

        Returns a dilated binary mask ready for ``cv2.inpaint``, or *None*
        if no watermark is detected.
        """
        h, w = roi_bgr.shape[:2]
        if h < 5 or w < 5:
            return None

        ksize = max(11, min(31, (min(h, w) // 6) | 1))
        background = cv2.medianBlur(roi_bgr, ksize)
        diff_gray = cv2.cvtColor(
            cv2.absdiff(roi_bgr, background), cv2.COLOR_BGR2GRAY
        )
        _, binary = cv2.threshold(
            diff_gray, self.config.pixel_threshold, 255, cv2.THRESH_BINARY
        )

        num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(
            binary, connectivity=8
        )

        # Select components whose centre is in the bottom-right quadrant
        # and whose area is large enough to be part of the watermark.
        wm_labels: List[int] = []
        for i in range(1, num_labels):
            cx, cy, cw, ch, area = stats[i]
            if area < self.config.min_component_area:
                continue
            if cx + cw / 2 < w * 0.5:          # must be in right half
                continue
            if cy + ch / 2 < h * 0.5:          # must be in bottom half
                continue
            if cw > w * 0.7 or ch > h * 0.8:   # not a full-span border
                continue
            wm_labels.append(i)

        if len(wm_labels) < self.config.min_watermark_components:
            return None

        # Check total area — confirms this is a real watermark, not noise
        total_area = sum(int(stats[i][4]) for i in wm_labels)
        if total_area < self.config.min_watermark_area:
            return None

        # Mask contains ONLY watermark pixels — surrounding content untouched
        mask = np.zeros((h, w), dtype=np.uint8)
        for lid in wm_labels:
            mask[labels == lid] = 255

        # Dilate to cover anti-aliased / blurred edges around the glyphs
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        mask = cv2.dilate(mask, kernel, iterations=3)

        return mask if cv2.countNonZero(mask) > 0 else None

    def _get_watermark_bbox_in_roi(
        self, roi_bgr: np.ndarray
    ) -> Optional[tuple]:
        """
        Return (x, y, w, h) bounding box of the watermark inside the ROI,
        or None if no watermark is detected.
        """
        mask = self._build_watermark_mask(roi_bgr)
        if mask is None:
            return None
        coords = cv2.findNonZero(mask)
        if coords is None:
            return None
        return cv2.boundingRect(coords)

    def _has_watermark(self, roi_bgr: np.ndarray) -> bool:
        """True if the ROI contains a watermark."""
        return self._build_watermark_mask(roi_bgr) is not None

    # ---------- inpainting ---------- #

    def _inpaint_region(self, img_bgr: np.ndarray) -> np.ndarray:
        """
        Inpaints ALL detected sharp features in *img_bgr*.
        This should only be called on a tightly-cropped region where
        everything that differs from the background IS watermark.
        """
        try:
            h, w = img_bgr.shape[:2]
            if h < 5 or w < 5:
                return img_bgr

            ksize = max(11, min(31, (min(h, w) // 6) | 1))
            background = cv2.medianBlur(img_bgr, ksize)
            diff = cv2.absdiff(img_bgr, background)
            diff_gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
            _, mask = cv2.threshold(
                diff_gray, self.config.pixel_threshold, 255, cv2.THRESH_BINARY
            )

            # Dilate to cover anti-aliased edges
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
            mask = cv2.dilate(mask, kernel, iterations=3)

            if cv2.countNonZero(mask) == 0:
                return img_bgr

            return cv2.inpaint(
                img_bgr, mask, self.config.inpaint_radius, cv2.INPAINT_TELEA
            )
        except Exception as e:
            logger.warning(f"Inpainting failed: {e}")
            return img_bgr

    def _clean_watermark_in_roi(self, roi_bgr: np.ndarray) -> Optional[np.ndarray]:
        """
        Precision cleaning: builds a pixel-precise mask covering only the
        watermark components, then inpaints those specific pixels.
        Surrounding content (borders, decorations, slide text) is untouched.
        Returns the modified ROI, or None if no watermark is detected.
        """
        mask = self._build_watermark_mask(roi_bgr)
        if mask is None:
            return None

        return cv2.inpaint(
            roi_bgr, mask, self.config.inpaint_radius, cv2.INPAINT_TELEA
        )

    # ------------------------------------------------------------------ #
    #  PDF processing                                                      #
    # ------------------------------------------------------------------ #

    def _find_watermark_rect_text(self, page: fitz.Page) -> Optional[fitz.Rect]:
        """Locate watermark via PDF text search.  Returns padded Rect or None."""
        w, h = page.rect.width, page.rect.height
        instances = page.search_for(self.WATERMARK_TEXT)
        if not instances:
            return None

        best: Optional[fitz.Rect] = None
        best_score = float('inf')

        for rect in instances:
            cy = (rect.y0 + rect.y1) / 2
            cx = (rect.x0 + rect.x1) / 2
            if cy < h * 0.80 or rect.width > 250 or rect.height > 40:
                continue
            dist = abs(w - cx) + abs(h - cy)
            if dist < best_score:
                best_score = dist
                best = rect

        if best is None:
            return None

        wm_rect = fitz.Rect(best)

        # Expand left to catch the icon
        icon_zone = fitz.Rect(best.x0 - 80, best.y0 - 15, best.x0 + 5, best.y1 + 15)
        try:
            for d in page.get_drawings():
                if d["rect"].intersects(icon_zone):
                    wm_rect = wm_rect | d["rect"]
        except Exception:
            pass
        try:
            for img_info in page.get_images(full=True):
                for ir in page.get_image_rects(img_info[0]):
                    if ir.intersects(icon_zone):
                        wm_rect = wm_rect | ir
        except Exception:
            pass

        wm_rect.x0 = min(wm_rect.x0, best.x0 - 45)
        pad = self.config.watermark_padding
        return fitz.Rect(
            max(0, wm_rect.x0 - pad),
            max(0, wm_rect.y0 - pad),
            min(w, wm_rect.x1 + pad),
            min(h, wm_rect.y1 + pad),
        )

    def _pixmap_to_bgr(self, pix: fitz.Pixmap) -> Optional[np.ndarray]:
        """Convert a PyMuPDF Pixmap to a BGR numpy array."""
        data = np.frombuffer(pix.samples, dtype=np.uint8)
        if pix.n == 4:
            return cv2.cvtColor(data.reshape(pix.h, pix.w, 4), cv2.COLOR_RGBA2BGR)
        if pix.n == 3:
            return cv2.cvtColor(data.reshape(pix.h, pix.w, 3), cv2.COLOR_RGB2BGR)
        return None

    def _patch_pdf_rect(self, page: fitz.Page, rect: fitz.Rect, precision: bool = True) -> bool:
        """
        Rasterise *rect*, clean watermark, paste back.
        If *precision* is True, only the tight sub-region is inpainted.
        """
        mat = fitz.Matrix(self.config.pdf_dpi_scale, self.config.pdf_dpi_scale)
        pix = page.get_pixmap(clip=rect, matrix=mat)
        roi_bgr = self._pixmap_to_bgr(pix)
        if roi_bgr is None:
            return False

        if precision:
            cleaned = self._clean_watermark_in_roi(roi_bgr)
            if cleaned is None:
                return False
        else:
            cleaned = self._inpaint_region(roi_bgr)

        cleaned_rgb = cv2.cvtColor(cleaned, cv2.COLOR_BGR2RGB)
        buf = io.BytesIO()
        Image.fromarray(cleaned_rgb).save(buf, format='PNG')
        page.insert_image(rect, stream=buf.getvalue())
        return True

    def process_pdf(self, input_path: str, output_path: str, preview: bool = False) -> bool:
        """Process a PDF: detect watermark per page and inpaint it."""
        try:
            doc = fitz.open(input_path)
        except Exception as e:
            logger.error(f"Could not open {input_path}: {e}")
            return False

        filename = os.path.basename(input_path)
        pbar = tqdm(enumerate(doc), total=len(doc), desc=f"Processing {filename}", unit="page")
        patched = skipped = 0

        for i, page in pbar:
            if preview and i > 0:
                break

            w, h = page.rect.width, page.rect.height

            # Strategy 1: text-based (vector PDFs)
            wm_rect = self._find_watermark_rect_text(page)
            if wm_rect is not None:
                if self._patch_pdf_rect(page, wm_rect, precision=False):
                    patched += 1
                    pbar.set_postfix(patched=patched, skipped=skipped)
                    continue

            # Strategy 2: pixel-based (raster PDFs — common for NotebookLM)
            corner = fitz.Rect(max(0, w - self.config.search_margin_x),
                               max(0, h - self.config.search_margin_y), w, h)
            if self._patch_pdf_rect(page, corner, precision=True):
                patched += 1
            else:
                skipped += 1
            pbar.set_postfix(patched=patched, skipped=skipped)

        try:
            doc.save(output_path, garbage=3, deflate=True, clean=True)
            doc.close()
            logger.info(f"Saved {output_path} ({patched} patched, {skipped} skipped)")
            return True
        except Exception as e:
            logger.error(f"Error saving {output_path}: {e}")
            return False

    # ------------------------------------------------------------------ #
    #  Image processing                                                    #
    # ------------------------------------------------------------------ #

    def _clean_roi_scaled(self, roi_bgr: np.ndarray) -> Optional[np.ndarray]:
        """
        Upscale *roi_bgr* by ``pdf_dpi_scale``, run watermark detection and
        inpainting at the higher resolution (so the same thresholds used for
        PDFs apply), then downscale the result back.

        This keeps detection parameters consistent regardless of the input
        image resolution.
        """
        scale = self.config.pdf_dpi_scale
        h, w = roi_bgr.shape[:2]
        roi_hr = cv2.resize(roi_bgr, None, fx=scale, fy=scale,
                            interpolation=cv2.INTER_LINEAR)
        cleaned_hr = self._clean_watermark_in_roi(roi_hr)
        if cleaned_hr is None:
            return None
        return cv2.resize(cleaned_hr, (w, h),
                          interpolation=cv2.INTER_LINEAR)

    def process_image(
        self, input_path: str, output_path: str,
        overlay_path: Optional[str] = None
    ) -> bool:
        """Remove watermark from a standalone image file (PNG/JPG/WEBP).
        If overlay_path is given, paste that image over the watermark region."""
        try:
            img = cv2.imread(input_path, cv2.IMREAD_UNCHANGED)
            if img is None:
                logger.error(f"Could not read: {input_path}")
                return False

            h, w = img.shape[:2]
            has_alpha = len(img.shape) == 3 and img.shape[2] == 4

            if has_alpha:
                channels = cv2.split(img)
                img_bgr = cv2.merge(channels[:3])
                alpha = channels[3]
            else:
                img_bgr = img.copy()
                alpha = None

            mx, my = self.config.search_margin_x, self.config.search_margin_y
            y0 = max(0, h - my)
            x0 = max(0, w - mx)

            roi = img_bgr[y0:h, x0:w].copy()

            # Get watermark bounds before cleaning
            scale = self.config.pdf_dpi_scale
            roi_hr = cv2.resize(roi, None, fx=scale, fy=scale,
                                interpolation=cv2.INTER_LINEAR)
            wm_bbox = self._get_watermark_bbox_in_roi(roi_hr)

            cleaned_roi = self._clean_roi_scaled(roi)

            if cleaned_roi is None:
                logger.warning(f"No watermark detected in {input_path}")
                return False

            img_bgr[y0:h, x0:w] = cleaned_roi

            # Overlay custom image if provided
            if overlay_path and wm_bbox is not None:
                self._apply_overlay(img_bgr, overlay_path, x0, y0, wm_bbox, scale)

            if has_alpha:
                img_final = cv2.merge([*cv2.split(img_bgr), alpha])
            else:
                img_final = img_bgr

            cv2.imwrite(output_path, img_final)
            logger.info(f"Saved cleaned image to {output_path}")
            return True
        except Exception as e:
            logger.error(f"Error processing {input_path}: {e}")
            return False

    def _apply_overlay(
        self, img_bgr: np.ndarray, overlay_path: str,
        roi_x0: int, roi_y0: int,
        wm_bbox: tuple, scale: float
    ):
        """Paste overlay image at the exact watermark location."""
        try:
            overlay = cv2.imread(overlay_path, cv2.IMREAD_UNCHANGED)
            if overlay is None:
                logger.warning(f"Could not read overlay: {overlay_path}")
                return

            bx, by, bw, bh = wm_bbox
            # Convert from high-res coords back to original
            ox = int(bx / scale)
            oy = int(by / scale)
            ow = int(bw / scale)
            oh = int(bh / scale)

            img_h, img_w = img_bgr.shape[:2]

            # Preserve aspect ratio of the overlay
            overlay_h, overlay_w = overlay.shape[:2]
            aspect_ratio = overlay_w / overlay_h

            # Use a consistent height minimum (e.g. 2.2% of image height) 
            # to prevent it from shrinking on poor detection frames
            target_h = max(oh, int(img_h * 0.022))
            target_w = int(target_h * aspect_ratio)

            # Anchor to the bottom-right of the detected watermark
            br_x = roi_x0 + ox + ow
            br_y = roi_y0 + oy + oh

            abs_x = br_x - target_w
            abs_y = br_y - target_h

            # Ensure it doesn't go out of bounds
            abs_x = max(0, min(abs_x, img_w - target_w))
            abs_y = max(0, min(abs_y, img_h - target_h))

            # Resize overlay maintaining original aspect ratio
            overlay_resized = cv2.resize(overlay, (target_w, target_h), interpolation=cv2.INTER_AREA)

            has_alpha = len(overlay_resized.shape) == 3 and overlay_resized.shape[2] == 4

            if has_alpha:
                overlay_bgr = overlay_resized[:, :, :3]
                overlay_alpha = overlay_resized[:, :, 3].astype(float) / 255.0
                for c in range(3):
                    img_bgr[abs_y:abs_y+target_h, abs_x:abs_x+target_w, c] = (
                        overlay_alpha * overlay_bgr[:, :, c] +
                        (1.0 - overlay_alpha) * img_bgr[abs_y:abs_y+target_h, abs_x:abs_x+target_w, c]
                    ).astype(np.uint8)
            else:
                if len(overlay_resized.shape) == 2:
                    overlay_resized = cv2.cvtColor(overlay_resized, cv2.COLOR_GRAY2BGR)
                img_bgr[abs_y:abs_y+target_h, abs_x:abs_x+target_w] = overlay_resized[:, :, :3]

            logger.info(f"Applied overlay at ({abs_x},{abs_y}) size ({target_w}x{target_h})")

        except Exception as e:
            logger.warning(f"Failed to apply overlay: {e}")

    def process_image_bytes(
        self, img_bytes: bytes, overlay_bytes: Optional[bytes] = None
    ) -> Optional[bytes]:
        """Process an image from bytes (for web API). Returns cleaned image bytes or None."""
        try:
            arr = np.frombuffer(img_bytes, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_UNCHANGED)
            if img is None:
                return None

            h, w = img.shape[:2]
            has_alpha = len(img.shape) == 3 and img.shape[2] == 4

            if has_alpha:
                channels = cv2.split(img)
                img_bgr = cv2.merge(channels[:3])
                alpha = channels[3]
            else:
                img_bgr = img.copy()
                alpha = None

            mx, my = self.config.search_margin_x, self.config.search_margin_y
            y0 = max(0, h - my)
            x0 = max(0, w - mx)

            roi = img_bgr[y0:h, x0:w].copy()
            scale = self.config.pdf_dpi_scale
            roi_hr = cv2.resize(roi, None, fx=scale, fy=scale,
                                interpolation=cv2.INTER_LINEAR)
            wm_bbox = self._get_watermark_bbox_in_roi(roi_hr)

            cleaned_roi = self._clean_roi_scaled(roi)
            if cleaned_roi is None:
                return None

            img_bgr[y0:h, x0:w] = cleaned_roi

            # Apply overlay if provided
            if overlay_bytes is not None and wm_bbox is not None:
                overlay_arr = np.frombuffer(overlay_bytes, dtype=np.uint8)
                overlay_img = cv2.imdecode(overlay_arr, cv2.IMREAD_UNCHANGED)
                if overlay_img is not None:
                    import tempfile
                    tmp = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
                    cv2.imwrite(tmp.name, overlay_img)
                    self._apply_overlay(img_bgr, tmp.name, x0, y0, wm_bbox, scale)
                    os.unlink(tmp.name)

            if has_alpha:
                img_final = cv2.merge([*cv2.split(img_bgr), alpha])
            else:
                img_final = img_bgr

            ok, encoded = cv2.imencode('.png', img_final)
            return encoded.tobytes() if ok else None
        except Exception as e:
            logger.error(f"Error processing image bytes: {e}")
            return None

    # ------------------------------------------------------------------ #
    #  PPTX processing                                                     #
    # ------------------------------------------------------------------ #

    def _clean_pptx_image_bytes(self, img_bytes: bytes) -> tuple:
        """
        Decodes an image from raw bytes, removes the watermark from the
        bottom-right corner, and re-encodes.

        Returns (cleaned_bytes, wm_bbox, roi_info) where:
          - cleaned_bytes: PNG bytes or None if no watermark detected
          - wm_bbox: watermark bounding box in high-res ROI (detected from ORIGINAL)
          - roi_info: (x0, y0, scale) for overlay positioning
        """
        arr = np.frombuffer(img_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_UNCHANGED)
        if img is None:
            return None, None, (0, 0, 1.0)

        h, w = img.shape[:2]
        has_alpha = len(img.shape) == 3 and img.shape[2] == 4

        if has_alpha:
            channels = cv2.split(img)
            img_bgr = cv2.merge(channels[:3])
            alpha = channels[3]
        else:
            img_bgr = img.copy()
            alpha = None

        mx, my = self.config.search_margin_x, self.config.search_margin_y
        y0, x0 = max(0, h - my), max(0, w - mx)
        scale = self.config.pdf_dpi_scale

        roi = img_bgr[y0:h, x0:w].copy()

        # Detect watermark bbox from ORIGINAL ROI (before cleaning)
        roi_hr = cv2.resize(roi, None, fx=scale, fy=scale,
                            interpolation=cv2.INTER_LINEAR)
        wm_bbox = self._get_watermark_bbox_in_roi(roi_hr)

        cleaned_roi = self._clean_roi_scaled(roi)
        if cleaned_roi is None:
            return None, None, (x0, y0, scale)

        img_bgr[y0:h, x0:w] = cleaned_roi

        if has_alpha:
            img_final = cv2.merge([*cv2.split(img_bgr), alpha])
        else:
            img_final = img_bgr

        ok, encoded = cv2.imencode('.png', img_final)
        return (encoded.tobytes() if ok else None), wm_bbox, (x0, y0, scale)

    def process_pptx(
        self, input_path: str, output_path: str,
        overlay_path: Optional[str] = None,
        progress_callback=None
    ) -> bool:
        """
        Processes a PPTX file by opening it as a ZIP archive, finding every
        embedded slide image, removing the watermark via inpainting, and
        saving the modified PPTX.

        NotebookLM PPTX files store each slide as a single full-page PNG
        image (ppt/media/image-N-1.png), so we process those directly.
        """
        try:
            tmpdir = tempfile.mkdtemp()
            with zipfile.ZipFile(input_path, 'r') as zin:
                zin.extractall(tmpdir)

            media_dir = os.path.join(tmpdir, 'ppt', 'media')
            if not os.path.isdir(media_dir):
                logger.error(f"No media directory in {input_path}")
                shutil.rmtree(tmpdir)
                return False

            image_exts = ('.png', '.jpg', '.jpeg', '.webp')
            images = sorted([
                f for f in os.listdir(media_dir)
                if f.lower().endswith(image_exts)
            ])

            if not images:
                logger.error(f"No images found in {input_path}")
                shutil.rmtree(tmpdir)
                return False

            patched = 0
            pbar = tqdm(images, desc=f"Processing {os.path.basename(input_path)}", unit="img")

            # İlk slayttaki tespit edilen filigran çerçevesini şablon olarak saklayacağız.
            # Böylece logo her slaytta milimetrik olarak aynı boyutta ve konumda olacak.
            first_wm_bbox = None

            for idx, img_name in enumerate(pbar):
                img_path = os.path.join(media_dir, img_name)
                with open(img_path, 'rb') as f:
                    original = f.read()

                cleaned, wm_bbox, roi_info = self._clean_pptx_image_bytes(original)
                
                # İlk tespit edilen bbox'ı tüm slaytlar için şablon olarak kaydet
                if wm_bbox is not None and first_wm_bbox is None:
                    first_wm_bbox = wm_bbox

                if cleaned is not None:
                    # Apply overlay if provided, using bbox from the FIRST image for perfect consistency
                    active_bbox = first_wm_bbox if first_wm_bbox is not None else wm_bbox
                    
                    if overlay_path and active_bbox is not None:
                        cleaned = self._apply_overlay_to_bytes(cleaned, overlay_path, active_bbox, roi_info)
                    with open(img_path, 'wb') as f:
                        f.write(cleaned)
                    patched += 1
                pbar.set_postfix(patched=patched)

                if progress_callback:
                    progress_callback(idx + 1, len(images), img_name)

            # Re-pack the ZIP
            with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zout:
                for root, _, files in os.walk(tmpdir):
                    for fname in files:
                        full_path = os.path.join(root, fname)
                        arcname = os.path.relpath(full_path, tmpdir)
                        zout.write(full_path, arcname)

            shutil.rmtree(tmpdir)
            logger.info(f"Saved {output_path} ({patched}/{len(images)} images patched)")
            return True

        except Exception as e:
            logger.error(f"Error processing PPTX {input_path}: {e}")
            try:
                if os.path.isdir(tmpdir):
                    shutil.rmtree(tmpdir)
            except Exception:
                pass
            return False

    def _apply_overlay_to_bytes(
        self, img_bytes: bytes, overlay_path: str,
        wm_bbox: tuple, roi_info: tuple
    ) -> bytes:
        """Apply overlay to already-cleaned image bytes using pre-detected bbox.

        Args:
            img_bytes: cleaned PNG image bytes
            overlay_path: path to overlay image file
            wm_bbox: watermark bounding box (detected from ORIGINAL, not cleaned)
            roi_info: (x0, y0, scale) positioning info from _clean_pptx_image_bytes
        """
        try:
            arr = np.frombuffer(img_bytes, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_UNCHANGED)
            if img is None:
                return img_bytes

            has_alpha = len(img.shape) == 3 and img.shape[2] == 4
            if has_alpha:
                img_bgr = cv2.merge(cv2.split(img)[:3])
            else:
                img_bgr = img.copy()

            x0, y0, scale = roi_info
            self._apply_overlay(img_bgr, overlay_path, x0, y0, wm_bbox, scale)

            if has_alpha:
                alpha = cv2.split(img)[3]
                img_final = cv2.merge([*cv2.split(img_bgr), alpha])
            else:
                img_final = img_bgr

            ok, encoded = cv2.imencode('.png', img_final)
            return encoded.tobytes() if ok else img_bytes
        except Exception:
            return img_bytes


# ------------------------------------------------------------------ #
#  CLI                                                                 #
# ------------------------------------------------------------------ #

def main():
    parser = argparse.ArgumentParser(
        description="ErazeLM: Watermark Remover for PDFs, Images & PPTX"
    )
    parser.add_argument("path", help="File (PDF/PPTX/PNG/JPG) or directory")
    parser.add_argument("-o", "--output", help="Output path")
    parser.add_argument(
        "--preview", action="store_true",
        help="Process only first page (PDF only)",
    )
    parser.add_argument(
        "--margin-x", type=int, default=None,
        help="Search margin width in px from right edge (default: 300).",
    )
    parser.add_argument(
        "--margin-y", type=int, default=None,
        help="Search margin height in px from bottom edge (default: 65).",
    )

    args = parser.parse_args()
    config = WatermarkConfig()

    if args.margin_x is not None:
        config.search_margin_x = args.margin_x
    if args.margin_y is not None:
        config.search_margin_y = args.margin_y

    remover = WatermarkRemover(config)

    supported = ('.pdf', '.pptx', '.png', '.jpg', '.jpeg', '.webp')

    if os.path.isdir(args.path):
        tasks = sorted([
            os.path.join(args.path, f)
            for f in os.listdir(args.path)
            if f.lower().endswith(supported)
        ])
        logger.info(f"Found {len(tasks)} supported files.")
    elif os.path.isfile(args.path) and args.path.lower().endswith(supported):
        tasks = [args.path]
    else:
        logger.error("Invalid path or unsupported format.")
        return

    for input_path in tasks:
        ext = os.path.splitext(input_path)[1].lower()

        if args.output and len(tasks) == 1:
            out_path = args.output
        else:
            base, _ = os.path.splitext(input_path)
            out_path = f"{base}_cleaned{ext}"

        if ext == '.pdf':
            remover.process_pdf(input_path, out_path, preview=args.preview)
        elif ext == '.pptx':
            remover.process_pptx(input_path, out_path)
        else:
            remover.process_image(input_path, out_path)


if __name__ == "__main__":
    main()
