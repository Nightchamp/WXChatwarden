#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
from chatwarden import core


def main():
    assert not Path(__file__).with_name('commandcode_fallback_gateway.py').exists()
    assert core.IMAGE_GEN_ENDPOINT.endswith(':8652/v1/generate')
    assert not hasattr(core, 'HERMES_FALLBACK_ENDPOINT')
    print("V3105_IMAGE_FALLBACK_ROUTE_REMOVED=PASS")


if __name__ == "__main__":
    main()
