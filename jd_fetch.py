"""Fetch a job description from a posting URL.

Server-rendered boards (Greenhouse, Lever, most company career pages)
read fine. Boards that only render in the browser or block fetchers come
back thin; then the caller asks for the text to be pasted instead. Never
proceed with a thin page, because tailoring against navigation junk is
worse than asking.
"""

import re
from html.parser import HTMLParser

import requests

MAX_CHARS = 20000
MIN_CHARS = 200

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")


class JdFetchError(Exception):
    pass


def looks_like_url(text):
    return bool(re.fullmatch(r"https?://\S+", (text or "").strip()))


class _Text(HTMLParser):
    _SKIP = ("script", "style", "noscript", "svg", "nav", "header", "footer")
    _BREAK = ("p", "div", "h1", "h2", "h3", "h4", "h5", "li", "br",
              "section", "tr", "ul")

    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in self._SKIP:
            self.skip += 1
        if tag in self._BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in self._SKIP and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip:
            self.parts.append(data)


def fetch(url):
    """The posting as {"text": ..., "title": ...}.

    Raises JdFetchError when the page cannot be fetched or comes back too
    thin to be a real job description.
    """
    try:
        r = requests.get(url.strip(), headers={"User-Agent": _UA},
                         timeout=15, allow_redirects=True)
        r.raise_for_status()
    except requests.RequestException as e:
        raise JdFetchError(f"could not fetch the page: {e}")

    parser = _Text()
    try:
        parser.feed(r.text)
    except Exception:
        raise JdFetchError("could not parse the page")
    text = "".join(parser.parts)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()

    if len(text) < MIN_CHARS:
        raise JdFetchError(
            "the page came back nearly empty, it probably renders in the "
            "browser only")
    return {"text": text[:MAX_CHARS], "title": parser.title.strip()}
