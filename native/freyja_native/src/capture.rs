//! Screen + window capture via Quartz / Core Graphics.

use core_graphics::display::{CGDirectDisplayID, CGDisplay, CGMainDisplayID};
use core_graphics::geometry::{CGPoint, CGRect, CGSize};
use core_graphics::image::CGImage;
use core_graphics::window::{
    create_image, kCGWindowImageBoundsIgnoreFraming, kCGWindowImageDefault,
    kCGWindowListOptionIncludingWindow,
};
use image::{
    codecs::jpeg::JpegEncoder, imageops::FilterType, ColorType, ImageBuffer, ImageEncoder, Rgba,
};
use thiserror::Error;

#[derive(Debug, Clone, Copy)]
pub enum ImageFormat {
    Png,
    Jpeg(u8), // quality 1..100
}

impl ImageFormat {
    pub fn from_str_with_quality(s: &str, quality: u8) -> Self {
        match s.to_ascii_lowercase().as_str() {
            "jpeg" | "jpg" => ImageFormat::Jpeg(quality.clamp(1, 100)),
            _ => ImageFormat::Png,
        }
    }
}

#[derive(Debug, Error)]
pub enum CaptureError {
    #[error("CGDisplayCreateImage returned null")]
    DisplayCaptureFailed,
    #[error("CGWindowListCreateImage returned null")]
    WindowCaptureFailed,
    #[error("image encoding failed: {0}")]
    Encode(String),
}

#[derive(Debug, Clone)]
pub struct DisplayInfo {
    pub id: u32,
    pub width: u32,
    pub height: u32,
    pub scale: f64,
    pub is_primary: bool,
}

pub fn list_displays() -> Result<Vec<DisplayInfo>, CaptureError> {
    let main_id: CGDirectDisplayID = unsafe { CGMainDisplayID() };
    let ids: Vec<u32> = CGDisplay::active_displays()
        .unwrap_or_default()
        .into_iter()
        .map(|id| id as u32)
        .collect();

    let mut out = Vec::with_capacity(ids.len());
    for id in ids {
        let display = CGDisplay::new(id as CGDirectDisplayID);
        let bounds = display.bounds();
        let (pixels_w, pixels_h) =
            (display.pixels_wide() as u32, display.pixels_high() as u32);
        let scale = if bounds.size.width > 0.0 {
            (pixels_w as f64) / bounds.size.width
        } else {
            1.0
        };
        out.push(DisplayInfo {
            id,
            width: pixels_w,
            height: pixels_h,
            scale,
            is_primary: id == main_id as u32,
        });
    }
    Ok(out)
}

pub fn capture_display(
    display_id: Option<u32>,
    max_dim: Option<u32>,
    format: ImageFormat,
) -> Result<Vec<u8>, CaptureError> {
    let id: CGDirectDisplayID = match display_id {
        Some(id) => id as CGDirectDisplayID,
        None => unsafe { CGMainDisplayID() },
    };
    let display = CGDisplay::new(id);
    let image = display.image().ok_or(CaptureError::DisplayCaptureFailed)?;
    encode_cgimage(&image, max_dim, format)
}

pub fn capture_window(
    window_id: u32,
    max_dim: Option<u32>,
    format: ImageFormat,
) -> Result<Vec<u8>, CaptureError> {
    let null_rect = CGRect::new(
        &CGPoint::new(f64::INFINITY, f64::INFINITY),
        &CGSize::new(0.0, 0.0),
    );
    let image = create_image(
        null_rect,
        kCGWindowListOptionIncludingWindow,
        window_id,
        kCGWindowImageBoundsIgnoreFraming | kCGWindowImageDefault,
    )
    .ok_or(CaptureError::WindowCaptureFailed)?;
    encode_cgimage(&image, max_dim, format)
}

/// Encode a CGImage to bytes in the requested format. If `max_dim` is
/// set, the image is downscaled so its long edge is ≤ `max_dim`
/// pixels before encoding. This is the bandwidth-control knob for the
/// IPC pipe — full-res Retina frames are tens of MB each, which
/// overwhelms `webContents.send` when streamed at 2+ fps.
fn encode_cgimage(
    image: &CGImage,
    max_dim: Option<u32>,
    format: ImageFormat,
) -> Result<Vec<u8>, CaptureError> {
    let width = image.width() as u32;
    let height = image.height() as u32;
    let bytes_per_row = image.bytes_per_row();
    let bits_per_pixel = image.bits_per_pixel();

    if bits_per_pixel != 32 {
        return Err(CaptureError::Encode(format!(
            "unsupported bpp {bits_per_pixel} (expected 32)"
        )));
    }
    let raw = image.data();
    let raw_bytes = raw.bytes();

    // Convert BGRA → RGBA row by row, trimming stride padding.
    let mut rgba = Vec::with_capacity((width * height * 4) as usize);
    for y in 0..(height as usize) {
        let row_start = y * bytes_per_row;
        let row_end = row_start + (width as usize) * 4;
        let row = &raw_bytes[row_start..row_end];
        for px in row.chunks_exact(4) {
            rgba.push(px[2]);
            rgba.push(px[1]);
            rgba.push(px[0]);
            rgba.push(px[3]);
        }
    }

    // Optional downscale. We use Triangle filter for speed over quality
    // since this frame is just a UI preview; the model can request a
    // full-res capture by passing `max_dim=None` when it needs one.
    let (enc_w, enc_h, enc_bytes): (u32, u32, Vec<u8>) = match max_dim {
        Some(limit) if width.max(height) > limit => {
            let scale = limit as f64 / width.max(height) as f64;
            let new_w = ((width as f64) * scale).round().max(1.0) as u32;
            let new_h = ((height as f64) * scale).round().max(1.0) as u32;
            let buf: ImageBuffer<Rgba<u8>, Vec<u8>> =
                ImageBuffer::from_raw(width, height, rgba)
                    .ok_or_else(|| CaptureError::Encode("ImageBuffer::from_raw".into()))?;
            let resized = image::imageops::resize(&buf, new_w, new_h, FilterType::Triangle);
            (new_w, new_h, resized.into_raw())
        }
        _ => (width, height, rgba),
    };

    match format {
        ImageFormat::Png => {
            let mut out = Vec::with_capacity(((enc_w * enc_h) as usize) / 2);
            let encoder = image::codecs::png::PngEncoder::new_with_quality(
                &mut out,
                image::codecs::png::CompressionType::Fast,
                image::codecs::png::FilterType::NoFilter,
            );
            encoder
                .write_image(&enc_bytes, enc_w, enc_h, ColorType::Rgba8.into())
                .map_err(|e| CaptureError::Encode(e.to_string()))?;
            Ok(out)
        }
        ImageFormat::Jpeg(quality) => {
            // JPEG doesn't support alpha — convert RGBA → RGB by
            // dropping the alpha channel. macOS screenshots have
            // alpha = 255 everywhere (no transparency on the
            // desktop), so this is lossless.
            let mut rgb = Vec::with_capacity((enc_w * enc_h * 3) as usize);
            for px in enc_bytes.chunks_exact(4) {
                rgb.push(px[0]);
                rgb.push(px[1]);
                rgb.push(px[2]);
            }
            let mut out = Vec::with_capacity(((enc_w * enc_h) as usize) / 6);
            let mut encoder = JpegEncoder::new_with_quality(&mut out, quality);
            encoder
                .write_image(&rgb, enc_w, enc_h, ColorType::Rgb8.into())
                .map_err(|e| CaptureError::Encode(e.to_string()))?;
            Ok(out)
        }
    }
}

// macOS 10.15+ official Screen Recording permission API. These are
// the only reliable way to check — `CGDisplayCreateImage` returning
// non-null is NOT enough because macOS returns a privacy-filtered
// image (just wallpaper + menubar) when permission is denied.
#[link(name = "CoreGraphics", kind = "framework")]
extern "C" {
    fn CGPreflightScreenCaptureAccess() -> bool;
    fn CGRequestScreenCaptureAccess() -> bool;
}

/// Non-prompting check for Screen Recording permission via the
/// official macOS API. Returns true only if the current process has
/// effective Screen Recording access — captures will include real
/// window content from other apps, not a filtered wallpaper-only view.
pub fn check_screen_recording_permission() -> bool {
    unsafe { CGPreflightScreenCaptureAccess() }
}

/// Prompting variant: same as above but if access is not granted,
/// macOS will pop a system dialog pointing at this binary's
/// responsible process. Returns the state BEFORE the prompt
/// (typically false when prompting is useful).
pub fn prompt_screen_recording_permission() -> bool {
    unsafe { CGRequestScreenCaptureAccess() }
}
