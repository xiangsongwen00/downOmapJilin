"""The deliberately small allowlist of Ovi maps that this exporter may download."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import re


FAMILIES = {
    "tianditu_vector": "天地图（矢量）",
    "tianditu_imagery": "天地图影像",
    "jilin_national": "吉林一号卫星影像（全国，自动选择年份）",
    "jilin_global": "吉林一号卫星影像（全球）",
    "four_dimensional_satellite": "四维地球卫星影像（自动选择年份）",
    "century_satellite": "世纪空间卫星影像（自动选择年份）",
    "tencent_vector": "腾讯电子地图",
}

_EXACT = {
    "天地图": "tianditu_vector",
    "天地图影像": "tianditu_imagery",
    "吉林一号卫星图(全球)": "jilin_global",
    "吉林一号卫星影像图(全球)": "jilin_global",
    "吉林一号卫星影像(全球)": "jilin_global",
    "腾讯电子地图": "tencent_vector",
}
_YEARLY = (
    (re.compile(r"^吉林一号卫星(?:图|影像图|影像)\(全国(20\d{2})\)$"), "jilin_national"),
    (re.compile(r"^四维地球卫星影像图\((20\d{2})(?:版)?\)$"), "four_dimensional_satellite"),
    (re.compile(r"^世纪空间卫星影像图\((20\d{2})(?:版)?\)$"), "century_satellite"),
)


@dataclass(frozen=True)
class Basemap:
    family: str
    label: str
    year: int | None

    @property
    def key(self):
        label_hash = hashlib.sha256(self.label.encode("utf-8")).hexdigest()[:8]
        return f"{self.family}_{self.year or 'stable'}_{label_hash}"


def classify(label: str):
    label = label.strip()
    if label in _EXACT:
        return Basemap(_EXACT[label], label, None)
    for pattern, family in _YEARLY:
        match = pattern.fullmatch(label)
        if match:
            return Basemap(family, label, int(match.group(1)))
    return None


def allowed_options(menu_items):
    """Only enabled, exact allowlist matches can appear in the user selector."""
    result = []
    for item in menu_items:
        if isinstance(item, str):
            label, enabled = item, True
        else:
            label, enabled = item.get("name", ""), item.get("enabled", False)
        choice = classify(label) if enabled else None
        if choice is not None:
            result.append(choice)
    return result


def resolve_basemap(family, menu_items, current_year=None):
    if family not in FAMILIES:
        raise ValueError(f"不支持下载的底图类型：{family!r}")
    options = [item for item in allowed_options(menu_items) if item.family == family]
    if not options:
        raise RuntimeError(
            f"奥维地图切换菜单中未找到可用的“{FAMILIES[family]}”。"
            "请检查奥维版本，或在“地图切换 > 定制地图菜单”中启用该底图，"
            "然后在本程序点击“重新读取底图”。")
    year = current_year if current_year is not None else date.today().year
    if all(item.year is None for item in options):
        return max(options, key=lambda item: item.label)
    previous = [item for item in options if item.year is not None and item.year <= year - 1]
    if previous:
        return max(previous, key=lambda item: (item.year, item.label))
    current = [item for item in options if item.year == year]
    if current:
        return max(current, key=lambda item: item.label)
    raise RuntimeError(
        f"“{FAMILIES[family]}”仅发现未来年份版本；"
        "请在奥维中启用上一年或更早的底图，再重新读取底图菜单。")


def menu_title_matches(title, choice):
    match = re.search(r"--\s*(.*?)\[\d+\]", title)
    if not match:
        return False
    current = match.group(1).replace("版)", ")").strip()
    expected = choice.label.replace("版)", ")").strip()
    return current == expected
