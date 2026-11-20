from bs4 import BeautifulSoup

from bellhaven_sync.scraper import community_links, next_page, parse_detail

DETAIL = """
<h1>Bellhaven Shores of Erie</h1>
<dl class="detail">
  <dt>Address</dt><dd>4930 W Lake Rd<br>Erie, PA 16505</dd>
  <dt>Care Offerings</dt><dd><span class="badge">Assisted Living</span> <span class="badge">Memory Support</span></dd>
  <dt>Administrator</dt><dd>Angela Quinn</dd>
  <dt>Phone</dt><dd>(614) 792-9343</dd>
</dl>"""

LISTING = """
<div class="card"><h3><a href="/communities/bellhaven-of-goshen">Bellhaven of Goshen</a></h3></div>
<a href="/communities">All</a>
<div class="pager"><a href="/communities?page=1">&larr; Previous</a><span>Page 2 / 3</span>
<a href="/communities?page=3">Next &rarr;</a></div>"""


def test_parse_detail_page():
    loc = parse_detail(BeautifulSoup(DETAIL, "html.parser"), "bellhaven-shores-of-erie", "https://x/communities/bellhaven-shores-of-erie")
    assert (loc.name, loc.street, loc.city, loc.state, loc.zip) == ("Bellhaven Shores of Erie", "4930 W Lake Rd", "Erie", "PA", "16505")
    assert loc.care_offerings == ["Assisted Living", "Memory Support"]
    assert loc.care_types == ["Assisted Living", "Memory Care"]
    assert loc.administrator == "Angela Quinn" and loc.phone == "(614) 792-9343"


def test_listing_links_and_pagination():
    soup = BeautifulSoup(LISTING, "html.parser")
    assert community_links(soup) == ["/communities/bellhaven-of-goshen"]
    assert next_page(soup) == "/communities?page=3"
    assert next_page(BeautifulSoup('<div class="pager"><a href="/communities?page=2">&larr; Previous</a></div>', "html.parser")) is None
