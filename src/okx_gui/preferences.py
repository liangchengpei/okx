"""Local GUI preferences, separate from API credentials."""
import json
import os
from pathlib import Path

from PySide6.QtCore import QIODevice, QSaveFile


def preferences_path():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "okx-trader" / "gui.json"


def load_preferences():
    path = preferences_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("不支持的配置格式")
        return data, ""
    except FileNotFoundError:
        return {}, ""
    except (OSError, ValueError) as exc:
        return {}, f"界面配置读取失败，已使用默认设置：{exc}"


def save_preferences(data):
    path = preferences_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    target = QSaveFile(str(path))
    if not target.open(QIODevice.WriteOnly):
        raise OSError(target.errorString())
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    if target.write(payload) != len(payload):
        target.cancelWriting()
        raise OSError(target.errorString())
    if not target.commit():
        raise OSError(target.errorString())


def chart_view(row):
    price, book = row.funding_area.plot, row.order_book_area.plot
    return {
        "volume_count": row.chart_area.plot.visible_count,
        "price_span": price.manual_span,
        "price_end": price.view_end,
        "price_follow_at": price.resume_follow_at,
        "price_scales": dict(price.y_scales),
        "price_offsets": dict(price.y_offsets),
        "book_center": book.manual_center,
        "book_span": book.manual_span,
        "book_scale": book.y_scale,
        "book_pan": book.pan_y,
    }


def restore_chart_view(row, data):
    import math
    if not isinstance(data, dict):
        return
    def number(value, low, high):
        return type(value) in (int, float) and math.isfinite(value) and low <= value <= high
    price, book = row.funding_area.plot, row.order_book_area.plot
    count = data.get("volume_count")
    if type(count) is int and 10 <= count <= 125:
        row.chart_area.plot.visible_count = count
        row.chart_area.refresh()
    for key, obj, attr, low, high in (
        ("price_span", price, "manual_span", 60000, 7 * 86400000),
        ("price_end", price, "view_end", 0, 1e15),
        ("price_follow_at", price, "resume_follow_at", 0, 1e15),
        ("book_center", book, "manual_center", -1e15, 1e15),
        ("book_span", book, "manual_span", 1e-12, 1e15),
        ("book_scale", book, "y_scale", .001, 1000),
        ("book_pan", book, "pan_y", -1e6, 1e6),
    ):
        value = data.get(key)
        if number(value, low, high):
            setattr(obj, attr, value)
    for key, attr, low, high in (
        ("price_scales", "y_scales", .001, 1000),
        ("price_offsets", "y_offsets", -1e15, 1e15),
    ):
        values = data.get(key)
        if isinstance(values, dict):
            for axis in ("price", "ratio"):
                if number(values.get(axis), low, high):
                    getattr(price, attr)[axis] = values[axis]
