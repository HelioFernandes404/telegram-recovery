"""Local selection and deterministic course/module layout; no Telegram access."""

from collections import defaultdict
from dataclasses import dataclass
from pathlib import PurePosixPath

from .database import Database
from .inventory import InventoryFilter
from .naming import parse_lesson_caption, safe_component, suggested_filename


@dataclass(frozen=True)
class DownloadItem:
    video: dict
    relative_path: str
    course: int | None
    module: int | None


def build_plan(
    database: Database,
    channel_id: int,
    *,
    course: int | None = None,
    module: int | None = None,
    filters: InventoryFilter | None = None,
    message_id: int | None = None,
    limit: int | None = None,
) -> list[DownloadItem]:
    if module is not None and course is None:
        raise ValueError("--module exige --course.")
    filters = filters or InventoryFilter()
    courses, modules = {}, {}
    selected = []
    # Canonical names come from the earliest inventoried message, even with filters.
    for video in database.videos(channel_id):
        lesson = parse_lesson_caption(video["caption"])
        c = int(lesson.course_number) if lesson else None
        m = int(lesson.module_number) if lesson else None
        if lesson:
            courses.setdefault(c, f"{c:03d}_{safe_component(lesson.course_title, 80)}")
            modules.setdefault((c, m), f"{m:03d}_{safe_component(lesson.module_title, 80)}")
        if course is not None and c != course or module is not None and m != module:
            continue
        if message_id is not None and video["message_id"] != message_id:
            continue
        if not filters.matches(video["tags"]):
            continue
        filename = suggested_filename(
            video["tags"],
            lesson.lesson_title if lesson else video["title"],
            video["original_filename"],
            video["message_id"],
            caption=video["caption"],
        )
        relative = str(
            PurePosixPath(
                f"channel_{channel_id}",
                courses.get(c, "SEM_CURSO"),
                modules.get((c, m), "SEM_MODULO"),
                filename,
            )
        )
        existing = database.download(channel_id, video["message_id"])
        if existing:
            relative = existing["relative_path"]
        selected.append(
            (
                c if c is not None else float("inf"),
                m if m is not None else float("inf"),
                int(lesson.lesson_number) if lesson else video["message_id"],
                video["message_id"],
                DownloadItem(video, relative, c, m),
            )
        )
    selected.sort(key=lambda row: row[:4])
    return [row[-1] for row in selected[:limit]]


def plan_summary(items: list[DownloadItem]) -> dict:
    groups = defaultdict(
        lambda: {"videos": 0, "expected_bytes": 0, "unknown_sizes": 0, "sample_paths": []}
    )
    for item in items:
        group = groups[str(PurePosixPath(item.relative_path).parent)]
        group["videos"] += 1
        group["expected_bytes"] += item.video["expected_size"] or 0
        group["unknown_sizes"] += item.video["expected_size"] is None
        if len(group["sample_paths"]) < 3:
            group["sample_paths"].append(item.relative_path)
    return {
        "dry_run": True,
        "selected": len(items),
        "expected_bytes": sum(g["expected_bytes"] for g in groups.values()),
        "modules": [{"folder": folder, **group} for folder, group in groups.items()],
    }
