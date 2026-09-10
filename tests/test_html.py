"""Tests for docpipe.extract.html."""
from docpipe.extract import html


def test_removes_script_and_style():
    raw = """
    <html><head><style>body{}</style></head>
    <body>
      <script>alert('x')</script>
      <main>Real content here</main>
    </body></html>
    """
    text = html.extract_text(raw)
    assert "Real content here" in text
    assert "alert" not in text
    assert "body{}" not in text


def test_prefers_main_or_article():
    raw = """
    <html><body>
      <nav>Navigation menu</nav>
      <main>Important content</main>
      <footer>Footer text</footer>
    </body></html>
    """
    text = html.extract_text(raw)
    assert "Important content" in text
    assert "Navigation" not in text
    assert "Footer text" not in text


def test_strips_whitespace():
    text = html.extract_text("<body>   hello   \n\n\n   world   </body>")
    assert "hello" in text
    assert "world" in text
    assert "   " not in text


def test_class_filter_can_be_overridden():
    """A site whose real content lives in a div named "sidebar-article"
    can opt out of the default class filter."""
    raw = '<body><div class="sidebar-article">Keep me</div></body>'
    assert "Keep me" not in html.extract_text(raw)
    assert "Keep me" in html.extract_text(raw, unwanted_classes=[])
