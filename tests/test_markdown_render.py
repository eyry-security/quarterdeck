"""Renderer tests for chat markdown tables in the standalone UI."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

INDEX = (Path(__file__).resolve().parents[1] / "quarterdeck" / "static" /
         "index.html")
BEGIN = "/* ==== QD MARKDOWN RENDERER BEGIN ==== */"
END = "/* ==== QD MARKDOWN RENDERER END ==== */"

FINDINGS_TABLE = """Findings for *.example.com (1-3 of 3):

| ID | Host | Severity | Status | Title |
| ---: | --- | --- | --- | --- |
| 7 | a.example.com | high | open | Missing security header |
| 8 | b.example.com | low | closed | Server version leak |
| 9 | c.example.com | info | informative | Informational note |
"""

WIDE_TABLE = """| A | B | C | D | E | F | G | H |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 |
"""

MIXED_MARKDOWN = """Here is `code` and a [link](https://example.com/a?b=1&c=2).

```python
print("hi")
```

- one
- two
"""

FENCED_FINDING = """Reproduction steps for the exposed endpoint:

```bash
curl -sS http://example.com/ -o page.html
echo "**not bold** <b>not html</b>"
```

The command saves the page for comparison.
"""


@pytest.fixture(scope="module")
def renderer():
    source = INDEX.read_text(encoding="utf-8")
    start = source.index(BEGIN)
    end = source.index(END) + len(END)
    return source[start:end]


def _render(renderer, text):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for renderer tests")
    script = (
        renderer +
        "\nconst _in=JSON.parse(require('fs').readFileSync(0,'utf8'));"
        "process.stdout.write(JSON.stringify({"
        "html:qdMarkdownToHtml(_in.text),"
        "markdown:qdTextIsMarkdown(_in.text)}));"
    )
    proc = subprocess.run(
        [node, "-e", script],
        input=json.dumps({"text": text}),
        text=True, capture_output=True, check=True,
    )
    return json.loads(proc.stdout)


def test_findings_table_renders_headers_alignment_and_pill_cells(renderer):
    rendered = _render(renderer, FINDINGS_TABLE)
    html = rendered["html"]

    assert rendered["markdown"] is True
    assert '<div class="md-table-wrap">' in html
    assert '<table class="md-table">' in html
    assert "<thead>" in html and "<tbody>" in html
    assert '<th class="num">ID</th>' in html
    assert '<td class="num">7</td>' in html
    assert '<span class="qd-pill qd-pill-sev-high">high</span>' in html
    assert '<span class="qd-pill qd-pill-open">open</span>' in html
    assert '<span class="qd-pill qd-pill-sev-low">low</span>' in html
    assert '<span class="qd-pill qd-pill-closed">closed</span>' in html
    assert '<span class="qd-pill qd-pill-sev-info">info</span>' in html
    assert ('<span class="qd-pill qd-pill-informative">informative</span>'
            in html)
    assert "Findings for *.example.com" in html


def test_wide_table_gets_scroll_wrapper(renderer):
    html = _render(renderer, WIDE_TABLE)["html"]
    assert html.startswith('<div class="md-table-wrap">')
    assert '<table class="md-table">' in html
    assert html.count("<th ") == 8


def test_plain_text_is_unchanged(renderer):
    text = "plain text <script>alert(1)</script> & done\nsecond line"
    rendered = _render(renderer, text)
    assert rendered["markdown"] is False
    assert rendered["html"] == (
        "plain text &lt;script&gt;alert(1)&lt;/script&gt; &amp; done\n"
        "second line"
    )
    assert "<table" not in rendered["html"]
    assert "<p" not in rendered["html"]


def test_code_lists_and_links_use_the_same_renderer(renderer):
    html = _render(renderer, MIXED_MARKDOWN)["html"]
    assert "<code>code</code>" in html
    assert ('<a href="https://example.com/a?b=1&amp;c=2" target="_blank" '
            'rel="noopener noreferrer">link</a>') in html
    assert '<pre class="md-code language-python"><code>print("hi")</code></pre>' in html
    assert '<ul class="md-list"><li>one</li><li>two</li></ul>' in html


def test_finding_summary_renders_fenced_code_verbatim(renderer):
    rendered = _render(renderer, FENCED_FINDING)
    html = rendered["html"]

    assert rendered["markdown"] is True
    assert html.count("<pre") == 1
    assert ('<pre class="md-code language-bash"><code>'
            'curl -sS http://example.com/ -o page.html\n'
            'echo "**not bold** &lt;b&gt;not html&lt;/b&gt;"'
            '</code></pre>') in html
    assert '<p class="md-p">Reproduction steps for the exposed endpoint:</p>' in html
    assert '<p class="md-p">The command saves the page for comparison.</p>' in html
    assert "<br>" not in html
    assert "<strong>not bold</strong>" not in html
    assert "<b>not html</b>" not in html


def test_unterminated_fence_renders_to_end_of_text(renderer):
    html = _render(renderer, "Steps:\n\n```sh\necho hi\nstill code")["html"]
    assert html.count("<pre") == 1
    assert ('<pre class="md-code language-sh"><code>'
            'echo hi\nstill code</code></pre>') in html


def test_long_cell_truncates_with_full_value_in_title(renderer):
    value = "x" * 120
    html = _render(renderer, f"| Title |\n| --- |\n| {value} |\n")["html"]
    assert 'class="md-cell-clip"' in html
    assert f'title="{value}"' in html
    assert ("x" * 77 + "\u2026") in html


def test_renderer_is_wired_into_message_rendering():
    source = INDEX.read_text(encoding="utf-8")
    for needle in (
        ".md-table-wrap", ".md-table th", ".qd-pill-open", ".md-code",
        ".msg .text.md", "qdTextIsMarkdown(text)", "qdMarkdownToHtml(text)",
    ):
        assert needle in source
