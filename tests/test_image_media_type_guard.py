"""Unsupported image media types (e.g. generate_svg's image/svg+xml) must never
reach a model API — they 400 the whole request and, once in the transcript,
poison every subsequent turn. Two layers defend against this:

  A. Provider serialization swaps an unsupported ImageBlock for a text
     placeholder (recovers already-poisoned sessions; all providers).
  B. The bridge tool-result sanitizer keeps such blocks out of the transcript
     in the first place, AFTER the UI preview is extracted.
"""

from __future__ import annotations

from engine.types import ImageBlock, TextBlock, image_media_type_supported


def test_supported_media_types_detection():
    assert image_media_type_supported("image/png")
    assert image_media_type_supported("image/jpeg")
    assert image_media_type_supported("image/gif")
    assert image_media_type_supported("image/webp")
    # Case-insensitive + whitespace tolerant.
    assert image_media_type_supported("image/PNG")
    assert image_media_type_supported("  image/webp  ")
    # The actual culprit + other exotics + empties.
    assert not image_media_type_supported("image/svg+xml")
    assert not image_media_type_supported("image/bmp")
    assert not image_media_type_supported("image/tiff")
    assert not image_media_type_supported("")
    assert not image_media_type_supported(None)


def test_anthropic_serialization_swaps_an_unsupported_image_for_text():
    from engine.anthropic_provider import _anthropic_image_block

    kept = _anthropic_image_block(ImageBlock.from_base64("AAAA", "image/png"))
    assert kept["type"] == "image"
    assert kept["source"]["media_type"] == "image/png"

    swapped = _anthropic_image_block(ImageBlock.from_base64("AAAA", "image/svg+xml"))
    assert swapped["type"] == "text"
    assert "image/svg+xml" in swapped["text"]  # the placeholder names the type


def test_bridge_strips_unsupported_images_from_tool_results_and_leaves_clean_ones_alone():
    from bridge.freyja_bridge import _strip_unsupported_image_blocks

    content = [
        TextBlock(text="Successfully generated 1 SVG"),
        ImageBlock.from_base64("AAAA", "image/svg+xml"),
        ImageBlock.from_base64("BBBB", "image/png"),
    ]
    out = _strip_unsupported_image_blocks(content)
    assert out is not None
    assert isinstance(out[0], TextBlock)          # untouched
    assert isinstance(out[1], TextBlock)          # svg -> text
    assert "image/svg+xml" in out[1].text
    assert isinstance(out[2], ImageBlock)         # png preserved
    assert out[2].media_type == "image/png"

    # Returns None so the caller keeps the original object (no needless realloc).
    clean = [TextBlock(text="hi"), ImageBlock.from_base64("AAAA", "image/png")]
    assert _strip_unsupported_image_blocks(clean) is None
    assert _strip_unsupported_image_blocks("plain string result") is None
    assert _strip_unsupported_image_blocks(None) is None
