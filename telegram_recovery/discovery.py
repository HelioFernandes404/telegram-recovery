"""Aggregate observations from committed pages, without caption or filename content."""

from collections import Counter
from dataclasses import dataclass, field

from .database import VideoRecord
from .naming import parse_lesson_caption

MIME_BUCKETS = {"video/mp4", "video/webm", "video/x-matroska", "video/quicktime"}


@dataclass
class Discovery:
    counts: Counter = field(default_factory=Counter)
    tag_counts: Counter = field(default_factory=Counter)
    mime_counts: Counter = field(default_factory=Counter)

    def add(self, records: list[VideoRecord], scanned: int, selected: int) -> None:
        self.counts.update(
            pages=1,
            scanned=scanned,
            videos=len(records),
            non_video=scanned - len(records),
            selected=selected,
            filtered_out=len(records) - selected,
        )
        for record in records:
            self.counts.update(
                without_tags=not record.tags,
                multiple_tags=len(record.tags) > 1,
                missing_filename=not record.original_filename,
                missing_mime=not record.mime_type,
                unknown_size=record.expected_size is None,
                missing_duration=record.duration is None,
                structured_captions=parse_lesson_caption(record.caption) is not None,
                expected_bytes=record.expected_size or 0,
                duration_seconds=record.duration or 0,
            )
            # Every tag counts once per message, including a repeated tag in a caption.
            self.tag_counts.update({int(tag[1:]) for tag in record.tags})
            mime = (record.mime_type or "").lower()
            # Unrecognized MIME strings may contain arbitrary private content: do not copy them.
            bucket = (
                mime
                if mime in MIME_BUCKETS
                else (
                    "missing"
                    if not mime
                    else "other_video"
                    if mime.startswith("video/")
                    else "other"
                )
            )
            self.mime_counts[bucket] += 1

    def snapshot(self) -> dict:
        numbers = sorted(self.tag_counts)
        repeated = sorted(n for n, count in self.tag_counts.items() if count > 1)
        gaps = []
        missing_count = 0
        for left, right in zip(numbers, numbers[1:], strict=False):
            if right > left + 1:
                missing_count += right - left - 1
                if len(gaps) < 20:
                    gaps.append([left + 1, right - 1])
        return {
            "counts": dict(self.counts),
            "mime_counts": dict(self.mime_counts),
            "distinct_tags": len(numbers),
            "min_tag": numbers[0] if numbers else None,
            "max_tag": numbers[-1] if numbers else None,
            "repeated_tags": len(repeated),
            "repeated_tag_sample": repeated[:20],
            "unobserved_tag_numbers": missing_count,
            "gap_range_sample": gaps,
        }
