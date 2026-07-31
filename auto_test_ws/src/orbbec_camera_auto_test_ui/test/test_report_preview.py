from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = PACKAGE_ROOT / "orbbec_camera_auto_test_ui" / "static"


def test_report_summaries_use_safe_markdown_renderer():
    script = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    renderer = script.split("function renderMarkdown(source)", 1)[1].split(
        "function renderJsonSummary", 1
    )[0]

    assert "renderMarkdown(text)" in script
    assert 'container.className = "markdown-body"' in renderer
    assert 'document.createElement("table")' in renderer
    assert "textContent" in renderer
    assert "innerHTML" not in renderer


def test_report_markdown_has_readable_table_and_code_styles():
    styles = (STATIC_ROOT / "style.css").read_text(encoding="utf-8")

    assert ".markdown-body h1" in styles
    assert ".markdown-table-wrap" in styles
    assert ".markdown-body table" in styles
    assert ".markdown-body pre code" in styles
