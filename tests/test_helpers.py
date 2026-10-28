from main import ChatApp


def test_split_compact_numbered_list():
    result = ChatApp._split_compact_numbered_list("1. First item.2. Second item")

    assert result == ["1. First item.\n", "2. Second item"]


def test_markdown_list_item_keeps_trailing_newline():
    assert ChatApp._parse_markdown_list_item("1. Pierwszy punkt\n") == (
        "",
        "1. ",
        "Pierwszy punkt\n",
    )


def test_split_compact_bullet_list():
    result = ChatApp._split_compact_bullet_list("• Alpha,• Beta,• Gamma")

    assert result == ["- Alpha,\n", "- Beta,\n", "- Gamma"]


def test_split_compact_nested_bullets():
    result = ChatApp._split_compact_bullet_list(
        "3. Sprawdź użycie: • tokeny wejściowe — 3144, • tokeny wyjściowe — 295"
    )

    assert result == [
        "3. Sprawdź użycie:\n",
        "  - tokeny wejściowe — 3144,\n",
        "  - tokeny wyjściowe — 295",
    ]


def test_file_size_formatting():
    assert ChatApp.format_file_size(512) == "0 KB"
    assert ChatApp.format_file_size(1024 * 1024) == "1.0 MB"


def test_model_pricing_matches_snapshot_suffix():
    app = ChatApp.__new__(ChatApp)
    app.prices = {"gpt-5.6-luna": (0.2, 1.2)}

    assert app.model_prices("gpt-5.6-luna-2026-02-16") == (0.2, 1.2)
