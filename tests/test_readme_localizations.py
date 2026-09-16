from __future__ import annotations

import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).parents[1]
README_FILES = (
    PROJECT_ROOT / "README.md",
    PROJECT_ROOT / "docs" / "README.zh-Hant.md",
    PROJECT_ROOT / "docs" / "README.zh-Hans.md",
    PROJECT_ROOT / "docs" / "README.es.md",
    PROJECT_ROOT / "docs" / "README.ja.md",
)
MACHINE_TERMS = (
    "--plan",
    "advertised-unvalidated",
    "no-resume-reuse",
    "--resume",
    "cache clear-incomplete",
    "transcript-clean.md",
    "large-v3-turbo",
)


def _code_blocks(markdown: str) -> tuple[str, ...]:
    return tuple(re.findall(r"```[^\n]*\n.*?\n```", markdown, flags=re.DOTALL))


def test_localized_readmes_mirror_commands_and_machine_terms() -> None:
    english = README_FILES[0].read_text(encoding="utf-8")
    expected_heading_count = len(re.findall(r"^## ", english, flags=re.MULTILINE))
    expected_code_blocks = _code_blocks(english)

    for path in README_FILES:
        markdown = path.read_text(encoding="utf-8")
        assert len(re.findall(r"^## ", markdown, flags=re.MULTILINE)) == (
            expected_heading_count
        )
        code_blocks = _code_blocks(markdown)
        assert len(code_blocks) == len(expected_code_blocks)
        # The first text block is the localized pipeline diagram. Commands,
        # paths, file trees, and other machine-facing blocks stay identical.
        assert code_blocks[1:] == expected_code_blocks[1:]
        if path != README_FILES[0]:
            assert code_blocks[0] != expected_code_blocks[0]
        for term in MACHINE_TERMS:
            assert term in markdown, f"{path.name} is missing {term}"


def test_localized_readme_links_resolve() -> None:
    for path in README_FILES:
        markdown = path.read_text(encoding="utf-8")
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", markdown):
            if "://" in target or target.startswith("#"):
                continue
            local_target = target.split("#", 1)[0]
            assert (path.parent / local_target).is_file(), (
                f"{path.name} links to missing file {target}"
            )


def test_language_option_is_not_described_as_translation_or_script_conversion() -> None:
    expected = (
        "does not translate or convert between Simplified and Traditional Chinese",
        "不會翻譯或進行簡繁轉換",
        "不会翻译或进行简繁转换",
        "no traduce ni convierte entre chino simplificado y tradicional",
        "翻訳や簡体字・繁体字の変換は行いません",
    )
    for path, phrase in zip(README_FILES, expected):
        markdown = " ".join(path.read_text(encoding="utf-8").split())
        assert phrase in markdown, f"{path.name} is missing the language caveat"
