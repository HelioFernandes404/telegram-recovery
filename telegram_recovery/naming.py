"""Caption metadata and deterministic, path-safe filename suggestions."""

import re
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath

# Some authorized channels number lessons with two digits (F01..F99), while
# the original Alura channel uses four. Keep the boundary checks and accept
# both formats so inventory does not silently discard a channel's index.
TAG_PATTERN = re.compile(r"(?<!\w)#F([0-9]{2,})(?!\w)", re.IGNORECASE)
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".mpeg", ".mpg", ".ts"}
LESSON_LINE = re.compile(r"^(={0,2})([0-9]{1,6})\s*[-–—]\s*(\S.*?)\s*$")
MODULE_LESSON_LINE = re.compile(r"^(=?)([0-9]{1,6})\.\s+(\S.*?)\s*$")
TAGGED_LESSON_LINE = re.compile(
    r"^#F([0-9]{2,})(?:\s+([0-9]{1,6})\.\s+|\s+)(\S.*?)\s*$", re.IGNORECASE
)
CHANNEL_MODULE_LINE = re.compile(r"^([0-9]{1,6}(?:\.[0-9]+)?)\s*[-–—]\s*(\S.*?)\s*$")


@dataclass(frozen=True)
class LessonCaption:
    course_number: str
    course_title: str
    module_number: str
    lesson_number: str
    lesson_title: str
    module_title: str = ""


def parse_lesson_caption(caption: str) -> LessonCaption | None:
    """Recognize course/module/lesson or the channel's module/lesson dotted format."""
    parts = []
    for line in caption.splitlines():
        match = LESSON_LINE.fullmatch(line.strip())
        if match:
            parts.append(match.groups())
    if [level for level, _, _ in parts] == ["", "=", "=="]:
        return LessonCaption(
            parts[0][1], parts[0][2], parts[1][1], parts[2][1], parts[2][2], parts[1][2]
        )
    if parts:
        tagged = [
            match.groups()
            for line in caption.splitlines()
            if (match := TAGGED_LESSON_LINE.fullmatch(line.strip()))
        ]
        modules = [
            match.groups()
            for line in caption.splitlines()
            if (match := CHANNEL_MODULE_LINE.fullmatch(line.strip()))
        ]
        if (
            len(tagged) == 1
            and len(modules) == 1
            and ("." in modules[0][0] or int(modules[0][0]) <= 99)
        ):
            module_token, module_title = modules[0]
            module_number = "000" if module_token == "0.2" else f"{int(module_token):03d}"
            lesson_number = tagged[0][1] or tagged[0][0]
            return LessonCaption(
                "000",
                "Curso do canal",
                module_number,
                f"{int(lesson_number):03d}",
                tagged[0][2],
                module_title,
            )
        return None
    dotted = [
        match.groups()
        for line in caption.splitlines()
        if (match := MODULE_LESSON_LINE.fullmatch(line.strip()))
    ]
    if [level for level, _, _ in dotted] != ["", "="]:
        tagged = [
            match.groups()
            for line in caption.splitlines()
            if (match := TAGGED_LESSON_LINE.fullmatch(line.strip()))
        ]
        modules = [
            match.groups()
            for line in caption.splitlines()
            if (match := CHANNEL_MODULE_LINE.fullmatch(line.strip()))
        ]
        if (
            len(tagged) != 1
            or len(modules) != 1
            or ("." not in modules[0][0] and int(modules[0][0]) > 99)
        ):
            return None
        module_token, module_title = modules[0]
        # The source uses 0.2 for its first group; retain it as module 000
        # so it does not collide with the real module 002.
        module_number = "000" if module_token == "0.2" else f"{int(module_token):03d}"
        lesson_number = tagged[0][1] or tagged[0][0]
        return LessonCaption(
            "000",
            "Curso do canal",
            module_number,
            f"{int(lesson_number):03d}",
            tagged[0][2],
            module_title,
        )
    # Zero is an explicit local grouping for a channel without a separate course level.
    # Preserve the real module and lesson numbers; never infer them from a tag offset.
    return LessonCaption(
        "000",
        "Curso do canal",
        f"{int(dotted[0][1]):03d}",
        f"{int(dotted[1][1]):03d}",
        dotted[1][2],
        dotted[0][2],
    )


def normalize_tag(value: str) -> str:
    value = value.strip().lstrip("#").upper()
    if not re.fullmatch(r"F[0-9]{2,}", value):
        raise ValueError("Use uma tag como F01, F001, F2072 ou '#F2072'.")
    return f"F{int(value[1:]):04d}"


def extract_tags(caption: str) -> list[str]:
    return list(dict.fromkeys(normalize_tag(f"F{n}") for n in TAG_PATTERN.findall(caption)))


def original_basename(filename: str) -> str:
    return PurePosixPath(filename.replace("\\", "/")).name


def lesson_title(caption: str, filename: str | None, message_id: int) -> str:
    lesson = parse_lesson_caption(caption)
    if lesson:
        return lesson.lesson_title
    for line in TAG_PATTERN.sub("", caption).splitlines():
        title = line.strip(" \t-_|:;•")
        if title:
            return title
    if filename:
        return PurePosixPath(original_basename(filename)).stem
    return f"Mensagem {message_id}"


def safe_component(value: str, max_length: int = 140) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", ascii_value).strip("_-.")
    return cleaned[:max_length].rstrip("_-") or "sem_titulo"


def suggested_filename(
    tags: list[str], title: str, filename: str | None, message_id: int, *, caption: str = ""
) -> str:
    """Deterministic basename; the downloader separately checks existing destinations."""
    extension = PurePosixPath(original_basename(filename or "")).suffix.lower()
    if extension not in VIDEO_EXTENSIONS:
        extension = ".mp4"
    prefix = tags[0] if tags else "SEM_TAG"
    lesson = parse_lesson_caption(caption)
    if lesson:
        context = (
            f"{lesson.course_number}_{safe_component(lesson.course_title, 70)}__"
            f"{lesson.module_number}__{lesson.lesson_number}_{safe_component(title, 100)}"
        )
        name = safe_component(context, 190)
    else:
        name = safe_component(title)
    return f"{safe_component(prefix, 24)}__{name}__m{message_id}{extension}"
