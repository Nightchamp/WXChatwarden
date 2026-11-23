#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi  # noqa: E402


def _bounds(node):
    try:
        component = node.get_component_iface()
        if not component:
            return None
        rect = component.get_extents(Atspi.CoordType.SCREEN)
        if rect.width <= 0 or rect.height <= 0:
            return None
        return {
            "x": int(rect.x),
            "y": int(rect.y),
            "width": int(rect.width),
            "height": int(rect.height),
        }
    except Exception:
        return None


def _walk(node, depth, max_depth):
    if node is None or depth > max_depth:
        return None
    try:
        role = str(node.get_role_name() or "").replace(" ", "-")
        name = str(node.get_name() or "")
        result = {
            "role": role,
            "name": name,
        }
        if role in {"frame", "list-item"}:
            bounds = _bounds(node)
            if bounds:
                result["bounds"] = bounds

        children = []
        if depth < max_depth:
            for index in range(node.get_child_count()):
                child = _walk(
                    node.get_child_at_index(index),
                    depth + 1,
                    max_depth,
                )
                if child:
                    children.append(child)
        if children:
            result["children"] = children
        return result
    except Exception:
        return None


def dump_application(application_name, max_depth):
    Atspi.init()
    desktop = Atspi.get_desktop(0)
    matches = []
    for index in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(index)
        try:
            if str(app.get_name() or "") == application_name:
                matches.append(app)
        except Exception:
            continue

    if len(matches) != 1:
        raise RuntimeError(
            f"exact AT-SPI application {application_name!r} count={len(matches)}"
        )

    app = matches[0]
    root = {
        "role": str(app.get_role_name() or "").replace(" ", "-"),
        "name": str(app.get_name() or ""),
    }
    children = []
    for index in range(app.get_child_count()):
        child = app.get_child_at_index(index)
        try:
            role = str(child.get_role_name() or "").replace(" ", "-")
            name = str(child.get_name() or "")
        except Exception:
            continue

        # Detached chat frames only need their exact title and bounds. The
        # Chats list lives inside the main Weixin frame, so that is the only
        # top-level window whose descendants are relevant to lazy open/send.
        child_depth = max_depth if role == "frame" and name == "Weixin" else 1
        item = _walk(child, 1, child_depth)
        if item:
            children.append(item)
    if children:
        root["children"] = children
    return root


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--application", default="wechat")
    parser.add_argument("--max-depth", type=int, default=30)
    args = parser.parse_args()
    root = dump_application(
        str(args.application).strip(),
        max(1, int(args.max_depth)),
    )
    if not root:
        raise RuntimeError("target AT-SPI application tree is empty")
    json.dump(root, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
