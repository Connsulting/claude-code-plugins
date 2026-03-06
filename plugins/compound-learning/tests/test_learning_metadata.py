import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

from lib.learning_metadata import extract_field, extract_tags


def test_extract_field_is_case_insensitive():
    content = "**tOpIc:** API Design\n"
    assert extract_field(content, "Topic") == "API Design"


def test_extract_field_missing_returns_none():
    content = "# No metadata here\n"
    assert extract_field(content, "Topic") is None


def test_extract_tags_plain_tags_are_normalized():
    content = "**tAgS:** Auth, jwt , security \n"
    assert extract_tags(content) == ["auth", "jwt", "security"]


def test_extract_tags_bracketed_tags_can_be_preserved_or_stripped():
    content = "**Tags:** [Auth, jwt, security]\n"
    assert extract_tags(content) == ["[auth", "jwt", "security]"]
    assert extract_tags(content, strip_brackets=True) == ["auth", "jwt", "security"]


def test_extract_tags_respects_max_tag_limit():
    tags = ", ".join(f"tag{i}" for i in range(1, 11))
    content = f"**Tags:** {tags}\n"

    assert extract_tags(content) == [f"tag{i}" for i in range(1, 9)]
    assert extract_tags(content, max_tags=3) == ["tag1", "tag2", "tag3"]


def test_extract_tags_missing_returns_empty_list():
    content = "**Topic:** observability\n"
    assert extract_tags(content) == []
