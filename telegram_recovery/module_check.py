"""Derive module completion from the current inventory and validated local files."""

import asyncio
from collections import defaultdict
from dataclasses import dataclass

from .download_files import destination, file_size, sha256_file
from .download_plan import DownloadItem, build_plan
from .security import RecoveryError


def module_key(item):
    return item.video["channel_id"], item.course, item.module


@dataclass
class ModuleCheck:
    completed: list[DownloadItem]
    pending: list[DownloadItem]
    modules: list[dict]

    @property
    def completed_modules(self):
        return sum(m["complete"] for m in self.modules)

    def summary(self):
        return {
            "already_downloaded": len(self.completed),
            "pending_videos": len(self.pending),
            "pending_expected_bytes": sum(i.video["expected_size"] or 0 for i in self.pending),
            "modules_complete": self.completed_modules,
            "module_checks": self.modules,
        }


async def local_copy_complete(database, root, item) -> bool:
    video = item.video
    saved = database.download(video["channel_id"], video["message_id"])
    if (
        not saved
        or not saved["sha256"]
        or type(video["expected_size"]) is not int
        or video["expected_size"] <= 0
        or saved["document_id"] != video["document_id"]
        or saved["expected_size"] != video["expected_size"]
        or saved["relative_path"] != item.relative_path
    ):
        return False
    try:
        path = destination(root, item.relative_path, video["channel_id"], create=False)
        if file_size(path) != video["expected_size"]:
            return False
        return await asyncio.to_thread(sha256_file, path) == saved["sha256"]
    except (OSError, RecoveryError):
        # A conflicting/missing file must never make a module look complete.
        # The normal downloader preserves it and records an actionable error.
        return False


async def check_modules(database, root, selected, *, progress=lambda _: None) -> ModuleCheck:
    wanted = defaultdict(list)
    for item in selected:
        wanted[module_key(item)].append(item)
    inventory = defaultdict(list)
    for channel_id in {key[0] for key in wanted}:
        for item in build_plan(database, channel_id):
            key = module_key(item)
            if key in wanted:
                inventory[key].append(item)
    valid = {}
    modules = []
    for key, items in wanted.items():
        channel_id, course, module = key
        known = inventory[key]
        if course is not None and module is not None:
            progress(
                f"Conferindo curso {course:03d}, módulo {module:03d}: "
                f"{len(known)} aulas inventariadas."
            )
        for item in known:
            identity = (item.video["channel_id"], item.video["message_id"])
            valid[identity] = await local_copy_complete(database, root, item)
        verified = sum(valid[(i.video["channel_id"], i.video["message_id"])] for i in known)
        # Unknown hierarchies cannot be declared a completed module.
        complete = (
            bool(known) and verified == len(known) and course is not None and module is not None
        )
        modules.append(
            {
                "channel_id": channel_id,
                "course": course,
                "module": module,
                "known_videos": len(known),
                "selected_videos": len(items),
                "verified_videos": verified,
                "complete": complete,
            }
        )
        if complete:
            progress(
                f"Curso {course:03d}, módulo {module:03d}: já completo no manifesto; ignorado."
            )
    completed, pending = [], []
    for item in selected:
        identity = (item.video["channel_id"], item.video["message_id"])
        (completed if valid.get(identity, False) else pending).append(item)
    return ModuleCheck(completed, pending, modules)
