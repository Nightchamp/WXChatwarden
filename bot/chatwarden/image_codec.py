"""Turning a stored image file into image bytes.

The built-in codec accepts files that already are ordinary images and
refuses everything else. A deployment can drop an ``image_codec_local.py``
next to this file; its ``Codec`` class is then used instead.
"""
from __future__ import annotations

import importlib


class ImageNotReadable(RuntimeError):
    """The stored file is not an ordinary image this codec can pass on."""


class PlainImageCodec:
    def __init__(self, pipeline):
        self.pipeline = pipeline

    def decode(self, raw):
        """Return ``(image_bytes, format, mode)``."""
        fmt = self.pipeline._format(raw)
        if not fmt:
            raise ImageNotReadable(
                "stored image is not a readable image file"
            )
        return raw, fmt, "plain"


def load_codec(pipeline):
    local_name = f"{__package__}.image_codec_local"
    try:
        local = importlib.import_module(local_name)
    except ModuleNotFoundError as exc:
        if exc.name != local_name:
            raise
        return PlainImageCodec(pipeline)
    return local.Codec(pipeline)
