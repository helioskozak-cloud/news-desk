"""fetch_top: the top-stories parse and the body reader (2026-10-20)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scan"))

import fetch_top as ft


def test_split_title_takes_the_publisher_off_the_end():
    assert ft.split_title("Dow jumps 500 points - WSJ") == ("Dow jumps 500 points", "WSJ")
    assert ft.split_title("No publisher here") == ("No publisher here", "")


def test_a_cnbc_style_json_body_is_read_including_linked_single_words():
    page = ('<html><p>Skip</p><script>var x={"body":{"content":[{"tagName":"p","children":'
            '["1. ",{"tagName":"a","children":["Apple"]}," shares rose after the launch."]}]}}</script></html>')
    text = ft.article_text(page)
    assert "Apple" in text and "shares rose after the launch." in text


def test_top_stories_link_to_the_first_outlet_without_a_login():
    item = ('<item><title>Stocks rally - WSJ</title><link>https://g/1</link>'
            '<description>&lt;ol&gt;&lt;li&gt;&lt;a href="https://g/wsj"&gt;Stocks rally&lt;/a&gt;&amp;nbsp;'
            '&lt;font color="#6f6f6f"&gt;WSJ&lt;/font&gt;&lt;/li&gt;&lt;li&gt;&lt;a href="https://g/cnbc"&gt;'
            'Dow up&lt;/a&gt;&amp;nbsp;&lt;font color="#6f6f6f"&gt;CNBC&lt;/font&gt;&lt;/li&gt;&lt;/ol&gt;'
            '</description></item>')
    s = ft.parse_top("<rss>" + item + "</rss>")[0]
    assert s["outlets"] == 2 and s["link"] == "https://g/cnbc" and s["link_outlet"] == "CNBC"
    assert s["walled"] is False
