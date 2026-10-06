"""Run in the pinned SearXNG venv after applying patches/brave-html-fallback.patch.

SEARXNG_SETTINGS_PATH must point to the staged settings, PYTHONPATH to its src.
"""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from lxml import html
from searx.engines import brave
from searx.exceptions import SearxEngineResponseException


def response(body):
    text = '<script>data: [(function(a,b){return a})(1,2)]</script>' + body
    return SimpleNamespace(text=text, html=lambda: html.fromstring(text))


class BraveCompatibilityTests(unittest.TestCase):
    def test_web_results_extract_visible_content_and_ignore_non_results(self):
        doc = response('''<div class="snippet extra"><a href="https://docs.python.org/3/library/asyncio.html">
            <div class="title">asyncio — <b>Python</b> documentation</div></a>
            <div class="site-name-content">Not the excerpt</div>
            <div class="content">Run asynchronous Python code.</div></div>
            <div class="snippet "><a href="/ad"><div class="title">Ad</div></a></div>
            <div class="snippet "><a href="javascript:alert(1)"><div class="title">Invalid</div></a></div>''')
        with patch.object(brave, "brave_category", "search"):
            results = brave.response(doc)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://docs.python.org/3/library/asyncio.html")
        self.assertEqual(results[0].title, "asyncio — Python documentation")
        self.assertEqual(results[0].content, "Run asynchronous Python code.")

    def test_news_results(self):
        doc = response('''<div class="results"><div data-type="news">
            <a class="result-header" href="https://www.nasa.gov/news/"><span class="snippet-title">NASA update</span></a>
            <p class="desc">Mission news.</p><div class="image-wrapper"><img src="https://www.nasa.gov/image.jpg"></div>
            </div></div>''')
        with patch.object(brave, "brave_category", "news"):
            results = brave.response(doc)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "NASA update")
        self.assertEqual(results[0].content, "Mission news.")
        self.assertEqual(results[0].thumbnail, "https://www.nasa.gov/image.jpg")

    def test_unrecognized_page_is_an_explicit_failure(self):
        with patch.object(brave, "brave_category", "search"):
            with self.assertRaises(SearxEngineResponseException):
                brave.response(response('<h1>Challenge or changed markup</h1>'))

    def test_original_json_parser_remains_in_use_for_other_responses(self):
        doc = SimpleNamespace(text="data: [{original JSON representation}]")
        with patch.object(brave, "brave_category", "search"), patch.object(brave, "_parse_results", return_value="original") as parser:
            self.assertEqual(brave.response(doc), "original")
            parser.assert_called_once_with(brave.parse_search_result, doc)


if __name__ == "__main__":
    unittest.main()
