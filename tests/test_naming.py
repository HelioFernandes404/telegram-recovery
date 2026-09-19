import pytest

from telegram_recovery.naming import (
    extract_tags,
    lesson_title,
    normalize_tag,
    parse_lesson_caption,
    suggested_filename,
)

# Synthetic caption with the structure observed during discovery; no account data.
STRUCTURED = "#F2072 aula\n\n116 - Redes\n=001 - Protocolos\n==005 - Camadas de Rede 2"


def test_tag_boundaries_and_deduplication():
    assert extract_tags("#f2072 #F2072 #F2073 #F10000") == ["F2072", "F2073", "F10000"]
    assert extract_tags("x#F2072 #F2072oops #F12 #F2073_suffix") == ["F0012"]
    assert normalize_tag(" #f02072 ") == "F2072"


@pytest.mark.parametrize("value", ["F1", "F2072xx", "F-2072", "2072", ""])
def test_reject_invalid_tags(value):
    with pytest.raises(ValueError):
        normalize_tag(value)


def test_title_fallbacks():
    assert lesson_title("#F2072\n116 Redes\nDescrição", "file.mp4", 4) == "116 Redes"
    assert lesson_title("#F2072", "../../aula.mp4", 4) == "aula"
    assert lesson_title("", None, 4) == "Mensagem 4"


def test_filename_is_safe_bounded_and_distinguishes_messages():
    first = suggested_filename(["F2072"], "../../Camadas de Réde\x00", "C:\\tmp\\A.MP4", 1)
    assert first == "F2072__Camadas_de_Rede__m1.mp4"
    assert suggested_filename([], "CON", "evil.exe", 2) == "SEM_TAG__CON__m2.mp4"
    second = suggested_filename(["F2072"], "../../Camadas de Réde\x00", "a.mp4", 2)
    assert first != second
    assert len(suggested_filename([], "á" * 500, None, 123).encode()) < 255


def test_hierarchy_uses_lesson_instead_of_generic_header_and_filename():
    lesson = parse_lesson_caption(STRUCTURED)
    assert lesson.course_number == "116"
    assert lesson.module_number == "001"
    assert lesson.lesson_number == "005"
    title = lesson_title(STRUCTURED, "aula.mp4", 2074)
    assert title == "Camadas de Rede 2"
    assert suggested_filename(["F2072"], title, "aula.mp4", 2074, caption=STRUCTURED) == (
        "F2072__116_Redes__001__005_Camadas_de_Rede_2__m2074.mp4"
    )


@pytest.mark.parametrize(
    "caption",
    [
        "001 - Curso\n==001 - Aula",  # Incomplete hierarchy.
        "=001 - Módulo\n001 - Curso\n==001 - Aula",  # Wrong order.
        STRUCTURED + "\n==006 - Outra aula",  # Ambiguous lesson.
    ],
)
def test_incomplete_or_ambiguous_hierarchy_preserves_fallback(caption):
    assert parse_lesson_caption(caption) is None
    assert lesson_title(caption, "aula.mp4", 1) == caption.splitlines()[0].replace("#F2072 ", "")


def test_hierarchy_whitespace_unicode_dashes_and_safe_bounded_names():
    caption = (
        "#F2072\r\n 116 – "
        + "Curso/../á" * 100
        + "\r\n =001 — Módulo\r\n ==005 - "
        + "Aula/../é" * 100
    )
    title = lesson_title(caption, "aula.mp4", 2147483647)
    name = suggested_filename(["F2072"], title, "aula.mp4", 2147483647, caption=caption)
    assert len(name.encode()) <= 255
    assert "/" not in name and "\\" not in name and ".." not in name
    assert "__001__005_" in name
    assert name.endswith("__m2147483647.mp4")


def test_three_digit_tags_share_existing_canonical_four_digit_identity():
    assert extract_tags("#F001 #f0001 #F002 #F123 #F2072") == ["F0001", "F0002", "F0123", "F2072"]
    assert normalize_tag("F001") == normalize_tag("F0001")
    assert extract_tags("x#F001 #F001abc #F001_suffix #F01") == ["F0001"]


def test_dotted_module_lesson_caption_keeps_real_hierarchy_and_lesson_title():
    caption = "#F003 1. Aula\n\n2. Fundamentos de DevOps\n=2. DevOps e as 3 maneiras"
    parsed = parse_lesson_caption(caption)
    assert parsed.course_number == "000"
    assert parsed.module_number == "002" and parsed.lesson_number == "002"
    assert parsed.module_title == "Fundamentos de DevOps"
    title = lesson_title(caption, "1. Aula.mp4", 4)
    assert title == "DevOps e as 3 maneiras"
    assert suggested_filename(extract_tags(caption), title, "1. Aula.mp4", 4, caption=caption) == (
        "F0003__000_Curso_do_canal__002__002_DevOps_e_as_3_maneiras__m4.mp4"
    )


def test_tagged_lesson_with_channel_module_header_keeps_module_folder():
    caption = "#F01 1. Boas-vindas (NAO PULE!)\n\n0.2 - Comece aqui"
    parsed = parse_lesson_caption(caption)
    assert parsed.course_number == "000"
    assert parsed.module_number == "000"
    assert parsed.lesson_number == "001"
    assert parsed.module_title == "Comece aqui"
    assert parsed.lesson_title == "Boas-vindas (NAO PULE!)"


def test_tagged_lesson_without_local_number_uses_tag_as_stable_fallback():
    parsed = parse_lesson_caption("#F88 Agradecimento + pedido\n\n12 - Agradecimento")
    assert parsed.module_number == "012"
    assert parsed.lesson_number == "088"
    assert parsed.lesson_title == "Agradecimento + pedido"


@pytest.mark.parametrize(
    "caption",
    [
        "2. Modulo\n=1. Aula\n=2. Outra aula",
        "=1. Aula\n2. Modulo",
        "2. Modulo",
        "002 - Curso\n=1. Aula\n2. Modulo",
    ],
)
def test_dotted_hierarchy_rejects_ambiguous_incomplete_or_mixed_formats(caption):
    assert parse_lesson_caption(caption) is None
