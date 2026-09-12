"""The web: search providers, a guarded fetch, and readable extraction.

Two tools, one seam (dsh's shape): web_search and fetch_page share no
schema but one policy owner — provider selection, the SSRF guard, the
size/time caps and the truncation vocabulary all live here.

Search runs through a PROVIDER CHAIN: a self-hosted SearXNG when
configured, Brave when a key is set, and DuckDuckGo's HTML endpoint
always (no key, no setup). Results carry snippets and dates, the model
can ask for FRESH results (day/week/month/year), and duplicates collapse.

Fetch is Odysseus's hardened design, made async: every hop of a redirect
chain is resolved and checked, and the TCP connection is PINNED to the
checked address — so a DNS answer that flips to 127.0.0.1 between the
check and the connect (the classic rebinding trick) buys nothing. Bodies
stream under a byte cap with identity encoding so the cap is honest.
HTML becomes READABLE TEXT — headings, paragraphs, lists, tables and code
kept as lightweight Markdown, chrome dropped — PDFs go through pypdf,
and plain text/JSON/Markdown comes back verbatim. A `focus` argument
returns the passages that match instead of the first N characters,
which is how a 35B reads a 40-page document without drowning.
"""

import asyncio
import html
import ipaddress
import logging
import re
import ssl
import time
import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser
from io import BytesIO

import httpcore
import httpx

from seymour.config import settings
from seymour.tools import MAX_RESULT_CHARS

logger = logging.getLogger(__name__)

# A plain browser-ish UA — several endpoints refuse obvious bots.
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) Seymour/0.2"
# Fetch budgets: bytes read (soft default, hard ceiling), redirects, time.
SOFT_MAX_BYTES = 2_000_000
HARD_MAX_BYTES = 8_000_000
MAX_REDIRECTS = 5
FETCH_TIMEOUT_S = 25.0
SEARCH_TIMEOUT_S = 20.0
# How long a fetched page stays cached (the model re-reads pages often
# within one run; the world doesn't change in 15 minutes).
CACHE_TTL_S = 900
CACHE_MAX = 64
# The freshness vocabulary the model uses, mapped per provider.
FRESHNESS = ("day", "week", "month", "year")


# --------------------------------------------------------------------------- #
#  SSRF guard: public addresses only, and the connection pinned to them       #
# --------------------------------------------------------------------------- #

def _is_public(address: ipaddress._BaseAddress) -> bool:
    """A routable public address — never loopback, private, link-local,
    multicast, reserved, or unspecified."""
    return not (address.is_private or address.is_loopback or address.is_link_local
                or address.is_multicast or address.is_reserved
                or address.is_unspecified)


async def public_ips(hostname: str) -> list[str]:
    """Resolve a hostname and keep only public addresses (empty = refuse).
    Async so a slow resolver never blocks the event loop."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(hostname, None)
    except (OSError, UnicodeError):
        return []
    addresses: list[str] = []
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if _is_public(address) and str(address) not in addresses:
            addresses.append(str(address))
    return addresses


class _PinnedBackend(httpcore.AsyncNetworkBackend):
    """Route every TCP connect to ONE pre-checked address. The URL stays
    untouched, so TLS SNI and the Host header keep the real hostname —
    only the destination is pinned."""

    def __init__(self, ip: str) -> None:
        self._ip = ip
        self._real = httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None,
                          socket_options=None):
        return await self._real.connect_tcp(self._ip, port, timeout,
                                            local_address, socket_options)

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        return await self._real.connect_unix_socket(path, timeout, socket_options)

    async def sleep(self, seconds: float) -> None:
        await self._real.sleep(seconds)


class _BodyStream(httpx.AsyncByteStream):
    """Adapts an httpcore response stream for httpx (streaming, so the
    byte cap below can stop reading early)."""

    def __init__(self, response: httpcore.Response) -> None:
        self._response = response

    async def __aiter__(self):
        async for chunk in self._response.stream:       # type: ignore[union-attr]
            yield chunk

    async def aclose(self) -> None:
        await self._response.aclose()


class _PinnedTransport(httpx.AsyncBaseTransport):
    """An httpx transport over a connection pool whose sockets all go to
    the pinned address (public httpcore API only — no private imports)."""

    def __init__(self, ip: str) -> None:
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(), http1=True, http2=False,
            network_backend=_PinnedBackend(ip))

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(scheme=request.url.raw_scheme, host=request.url.raw_host,
                             port=request.url.port, target=request.url.raw_path),
            headers=request.headers.raw, content=request.stream,
            extensions=request.extensions)
        core_response = await self._pool.handle_async_request(core_request)
        return httpx.Response(status_code=core_response.status,
                              headers=core_response.headers,
                              stream=_BodyStream(core_response),
                              extensions=core_response.extensions)

    async def aclose(self) -> None:
        await self._pool.aclose()


@dataclass
class Fetched:
    """A capped, guarded GET. A non-2xx status is a RESULT (dsh's rule),
    not an exception — the model can reason about a 404."""

    url: str                       # final URL after redirects
    status: int
    content_type: str
    content: bytes
    truncated: bool                # the byte cap cut the body
    declared_bytes: int | None     # Content-Length, when the server said


class FetchRefused(Exception):
    """A fetch the guard would not make (private target, too many hops,
    body too large by declaration, unsupported scheme)."""


async def guarded_get(url: str, max_bytes: int = SOFT_MAX_BYTES) -> Fetched:
    """GET with manual, re-validated redirects and a streamed byte cap."""
    cap = min(max_bytes or SOFT_MAX_BYTES, HARD_MAX_BYTES)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/pdf,text/*;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.7",
        # identity: with gzip the wire bytes are a fraction of the decoded
        # body, and the cap would lie.
        "Accept-Encoding": "identity",
    }
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        parsed = urllib.parse.urlparse(current)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise FetchRefused(f"{current} is not a public http(s) URL")
        if parsed.username or parsed.password:
            raise FetchRefused("URLs with embedded credentials are refused")
        ips = await public_ips(parsed.hostname)
        if not ips:
            # Say it the way the model will repeat it: the fetch FAILED and
            # the host could not be reached — a dead domain and a private
            # target are both "nothing to read here".
            raise FetchRefused(
                f"could not fetch {current} — the host {parsed.hostname} could "
                "not be resolved (no such domain, or it does not point to a "
                "public address), so the page is unreachable")
        transport = _PinnedTransport(ips[0])
        try:
            async with httpx.AsyncClient(transport=transport, headers=headers,
                                         timeout=FETCH_TIMEOUT_S,
                                         follow_redirects=False) as client:
                async with client.stream("GET", current) as response:
                    if response.is_redirect and "location" in response.headers:
                        current = str(httpx.URL(current).join(
                            response.headers["location"]))
                        continue                  # re-validated at the top
                    declared = response.headers.get("content-length")
                    declared_bytes = int(declared) if declared and declared.isdigit() else None
                    if declared_bytes is not None and declared_bytes > HARD_MAX_BYTES:
                        raise FetchRefused(
                            f"body is {declared_bytes:,} bytes, over the "
                            f"{HARD_MAX_BYTES:,}-byte ceiling")
                    chunks: list[bytes] = []
                    got = 0
                    truncated = False
                    async for chunk in response.aiter_raw():
                        room = cap - got
                        if room <= 0:
                            truncated = True
                            break
                        chunks.append(chunk[:room])
                        got += len(chunk[:room])
                        if len(chunk) > room:
                            truncated = True
                            break
                    return Fetched(url=current, status=response.status_code,
                                   content_type=response.headers.get("content-type", ""),
                                   content=b"".join(chunks), truncated=truncated,
                                   declared_bytes=declared_bytes)
        finally:
            await transport.aclose()
    raise FetchRefused(f"more than {MAX_REDIRECTS} redirects")


# --------------------------------------------------------------------------- #
#  HTML → readable text                                                        #
# --------------------------------------------------------------------------- #

# Elements whose whole subtree is page chrome or non-content.
_DROP = frozenset({"script", "style", "noscript", "template", "svg", "nav",
                   "header", "footer", "aside", "form", "button", "iframe",
                   "canvas", "select", "option", "video", "audio"})
# Elements that start a new text block.
_BLOCK = frozenset({"p", "div", "section", "article", "main", "li", "h1", "h2",
                    "h3", "h4", "h5", "h6", "tr", "pre", "blockquote", "dd", "dt",
                    "figcaption", "summary", "details", "br", "hr", "ul", "ol",
                    "table", "thead", "tbody"})
_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}


@dataclass
class Page:
    """What a fetched HTML page boils down to."""

    title: str = ""
    text: str = ""
    description: str = ""
    image: str = ""                # og:image, for report thumbnails
    js_shell: bool = False         # looked like a JS app with no server text
    links: list = field(default_factory=list)   # (text, href) of in-content links


class _Extractor(HTMLParser):
    """A one-pass readability pass with the standard library.

    It is not Readability (no scoring), but it keeps what a reader wants:
    the title, headings as `#` lines, paragraphs, list items as `- `,
    table rows as `| a | b |`, code blocks fenced, and in-content links
    (so the model can follow a page to the next one). Chrome elements
    are dropped whole.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.page = Page()
        self._skip: list[str] = []       # stack of dropped tags we're inside
        self._blocks: list[str] = []     # finished text blocks
        self._buf: list[str] = []        # inline text of the current block
        self._prefix = ""                # marker for the block being built
        self._pre = 0                    # inside <pre>: keep whitespace
        self._title = False
        self._cells: list[str] | None = None   # collecting a table row
        self._cell: list[str] = []
        self._scripts = 0
        self._href: str | None = None    # the <a> we're inside
        self._link_text: list[str] = []

    # -- block helpers --------------------------------------------------------
    def _flush(self) -> None:
        text = "".join(self._buf)
        text = text if self._pre else re.sub(r"[ \t\r\f\v]+", " ", text).strip()
        if text:
            self._blocks.append(self._prefix + text)
        self._buf = []
        self._prefix = ""

    # -- parser callbacks -----------------------------------------------------
    def handle_starttag(self, tag, attrs):
        if tag in _DROP:
            if tag == "script":
                self._scripts += 1
            self._skip.append(tag)
            return
        if self._skip:
            return
        a = dict(attrs)
        if tag == "meta":
            prop = (a.get("property") or a.get("name") or "").lower()
            content = (a.get("content") or "").strip()
            if prop == "og:image" and content.startswith(("http://", "https://")):
                self.page.image = self.page.image or content[:500]
            elif prop in ("description", "og:description"):
                self.page.description = self.page.description or content[:300]
            return
        if tag == "title":
            self._title = True
            return
        if tag == "a":
            self._href = a.get("href") or None
            self._link_text = []
        if tag in ("td", "th") and self._cells is not None:
            self._cell = []
            return
        if tag == "tr":
            self._flush()
            self._cells = []
            return
        if tag in _BLOCK:
            self._flush()
            if tag in _HEADINGS:
                self._prefix = "#" * _HEADINGS[tag] + " "
            elif tag == "li":
                self._prefix = "- "
            elif tag == "blockquote":
                self._prefix = "> "
            elif tag == "pre":
                self._pre += 1
                self._blocks.append("```")
            elif tag == "hr":
                self._blocks.append("---")

    def handle_endtag(self, tag):
        if self._skip:
            if tag == self._skip[-1]:
                self._skip.pop()
            return
        if tag == "title":
            self._title = False
            return
        if tag == "a" and self._href:
            text = re.sub(r"\s+", " ", "".join(self._link_text)).strip()
            if text and len(self.page.links) < 200:
                self.page.links.append((text[:80], self._href[:300]))
            self._href = None
        if tag in ("td", "th") and self._cells is not None:
            self._cells.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = []
            return
        if tag == "tr" and self._cells is not None:
            if any(self._cells):
                self._blocks.append("| " + " | ".join(self._cells) + " |")
            self._cells = None
            return
        if tag in _BLOCK:
            self._flush()
            if tag == "pre" and self._pre:
                self._pre -= 1
                self._blocks.append("```")

    def handle_data(self, data):
        if self._skip:
            return
        if self._title:
            self.page.title += data
            return
        if self._cells is not None and self._cell is not None and self._cells is not None:
            self._cell.append(data)
        self._buf.append(data)
        if self._href is not None:
            self._link_text.append(data)

    # -- result -------------------------------------------------------------
    def finish(self) -> Page:
        self._flush()
        # Paragraph spacing: blank lines between prose blocks, none inside
        # lists/tables/code so they stay compact.
        out: list[str] = []
        for block in self._blocks:
            compact = block.startswith(("- ", "| ", "```")) or (out and out[-1].startswith(("- ", "| ", "```")))
            if out and not compact:
                out.append("")
            out.append(block)
        text = "\n".join(out).strip()
        text = re.sub(r"\n{3,}", "\n\n", text)
        self.page.title = re.sub(r"\s+", " ", self.page.title).strip()[:200]
        self.page.text = text
        self.page.js_shell = len(text) < 400 and self._scripts >= 5
        return self.page


def extract_html(raw: str) -> Page:
    """Run the extractor; a parser hiccup degrades to tag-stripping,
    never to an exception."""
    try:
        parser = _Extractor()
        parser.feed(raw)
        parser.close()
        return parser.finish()
    except Exception:                       # malformed markup of any kind
        page = Page()
        stripped = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.DOTALL | re.IGNORECASE)
        page.text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", stripped))).strip()
        return page


def extract_pdf(content: bytes, max_pages: int = 60) -> tuple[str, int]:
    """PDF text via pypdf (already a dependency for attachments). Returns
    (text, page_count); text is empty when extraction fails."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(content))
        pages = []
        for index, page in enumerate(reader.pages):
            if index >= max_pages:
                break
            pages.append(page.extract_text() or "")
        return "\n\n".join(pages).strip(), len(reader.pages)
    except Exception as error:
        logger.warning("pdf extraction failed: %s", error)
        return "", 0


# --------------------------------------------------------------------------- #
#  Focus: return the passages that matter, not the first N characters         #
# --------------------------------------------------------------------------- #

_STOP = frozenset("""a an and are as at be by for from has have how in is it its of on or
that the this to was were what when where which who why will with does did do can
about into than then them they their there these those you your""".split())


def _terms(focus: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9][a-z0-9\-_.]{2,}", focus.lower())
            if w not in _STOP]


def _chunks(text: str, size: int = 700) -> list[str]:
    """Paragraph-aligned chunks of roughly `size` characters; a heading
    sticks to the paragraph that follows it."""
    chunks: list[str] = []
    current = ""
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if current and len(current) + len(para) > size and not current.startswith("#"):
            chunks.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
        if current.startswith("#") and "\n" not in current:
            continue                      # keep the heading attached to what follows
    if current:
        chunks.append(current)
    return chunks


def focus_excerpts(text: str, focus: str, budget: int) -> str:
    """The best-matching chunks for `focus`, in document order, within
    `budget` characters — with an honest header saying how much of the
    document that is. Falls back to the beginning when nothing matches."""
    terms = _terms(focus)
    chunks = _chunks(text)
    if not terms or not chunks:
        return truncate_text(text, budget)
    scored = []
    for index, chunk in enumerate(chunks):
        low = chunk.lower()
        score = sum(low.count(term) * (2 if len(term) > 6 else 1) for term in terms)
        if score:
            scored.append((score, index))
    if not scored:
        return (f"[no passages matched {focus!r}; showing the beginning]\n"
                + truncate_text(text, budget - 80))
    scored.sort(reverse=True)
    chosen: dict[int, str] = {}
    used = 0
    for _score, index in scored:
        chunk = chunks[index]
        if used + len(chunk) > budget:
            # The best match must never be dropped for being long: the
            # first pick gets trimmed to the budget instead of skipped.
            if chosen:
                continue
            chunk = truncate_text(chunk, max(200, budget - 120))
        chosen[index] = chunk
        used += len(chunk) + 8
    ordered = [chosen[i] for i in sorted(chosen)]
    header = (f"[{len(ordered)} of {len(chunks)} sections matched {focus!r}; "
              f"the rest of the document is omitted]\n")
    return header + "\n[…]\n".join(ordered)


def truncate_text(text: str, limit: int) -> str:
    """Cap at a sentence boundary when one falls near the end, and SAY
    that a cut happened."""
    if len(text) <= limit:
        return text
    cut = text.rfind(". ", 0, limit)
    cut = cut + 1 if cut > limit * 0.8 else limit
    return text[:cut] + f"\n[truncated at {cut} of {len(text)} characters — the document continues; pass a focus to read the parts you need]"


# --------------------------------------------------------------------------- #
#  fetch_page                                                                  #
# --------------------------------------------------------------------------- #

@dataclass
class Document:
    """A fetched, extracted document — what both tools and research use."""

    url: str
    title: str
    text: str
    kind: str                      # "html" | "pdf" | "text"
    status: int
    image: str = ""
    truncated: bool = False
    note: str = ""                 # anything the reader should know (JS shell…)
    links: list = field(default_factory=list)


_cache: dict[str, tuple[float, Document]] = {}


async def fetch_document(url: str, max_bytes: int = SOFT_MAX_BYTES) -> Document:
    """Fetch + extract, cached. Raises FetchRefused for guarded refusals
    and httpx errors for transport failures — callers turn both into
    readable text."""
    url = url.strip()
    if url and "://" not in url:
        url = "https://" + url                    # a bare domain means https
    key = f"{url}#{max_bytes}"
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL_S:
        return hit[1]
    fetched = await guarded_get(url, max_bytes)
    ctype = fetched.content_type.lower()
    path = urllib.parse.urlparse(fetched.url).path.lower()
    if "pdf" in ctype or path.endswith(".pdf"):
        text, pages = extract_pdf(fetched.content)
        doc = Document(url=fetched.url, title=path.rsplit("/", 1)[-1] or fetched.url,
                       text=text, kind="pdf", status=fetched.status,
                       truncated=fetched.truncated,
                       note=(f"PDF, {pages} pages" if pages else "PDF text could not be extracted")
                       + ("; download was cut at the byte cap" if fetched.truncated else ""))
    elif "html" in ctype or (not ctype and fetched.content.lstrip()[:1] == b"<"):
        page = extract_html(fetched.content.decode(_charset(ctype), errors="replace"))
        doc = Document(url=fetched.url, title=page.title, text=page.text, kind="html",
                       status=fetched.status, image=page.image, truncated=fetched.truncated,
                       note=("this page renders its content with JavaScript; the server "
                             "sent almost no text — try a different source or a "
                             "documentation/print URL" if page.js_shell else ""),
                       links=page.links)
    else:
        text = fetched.content.decode(_charset(ctype), errors="replace").strip()
        doc = Document(url=fetched.url, title=path.rsplit("/", 1)[-1] or fetched.url,
                       text=text, kind="text", status=fetched.status,
                       truncated=fetched.truncated)
    if len(_cache) >= CACHE_MAX:
        oldest = min(_cache, key=lambda k: _cache[k][0])
        _cache.pop(oldest, None)
    _cache[key] = (time.monotonic(), doc)
    return doc


def _charset(content_type: str) -> str:
    match = re.search(r"charset=([\w\-]+)", content_type)
    return match.group(1) if match else "utf-8"


async def fetch_page(url: str, focus: str = "") -> str:
    """Tool entry: a page as readable text, optionally focused."""
    if not (url or "").strip():
        return "Error: fetch_page needs a url"
    try:
        doc = await fetch_document(url)
    except FetchRefused as refused:
        return f"Refused: {refused}"
    except httpx.TimeoutException:
        return f"Error: fetching {url} timed out after {FETCH_TIMEOUT_S:.0f}s"
    except httpx.HTTPError as error:
        return f"Error: fetching {url} failed: {type(error).__name__}: {error}"
    budget = MAX_RESULT_CHARS - 600                # room for the header lines
    header = [f"# {doc.title}" if doc.title else f"# {doc.url}", f"URL: {doc.url}"]
    if doc.status >= 400:
        header.append(f"HTTP {doc.status} — the server returned an error page; "
                      "what it said follows.")
    if doc.note:
        header.append(f"Note: {doc.note}")
    body = doc.text or "(no readable text on this page)"
    body = (focus_excerpts(body, focus, budget) if focus.strip()
            else truncate_text(body, budget))
    return "\n".join(header) + "\n\n" + body


async def fetch_page_with_meta(url: str) -> dict:
    """Research's entry: text plus og:image, never raising (an empty text
    is the failure signal the pipeline already understands)."""
    try:
        doc = await fetch_document(url)
    except (FetchRefused, httpx.HTTPError) as error:
        return {"text": f"Refused or failed: {error}", "image": ""}
    return {"text": truncate_text(doc.text, MAX_RESULT_CHARS), "image": doc.image}


# --------------------------------------------------------------------------- #
#  web_search                                                                  #
# --------------------------------------------------------------------------- #

def _freshness(value: str) -> str | None:
    value = (value or "").strip().lower()
    return value if value in FRESHNESS else None


async def _searxng(query: str, limit: int, fresh: str | None) -> list[dict]:
    """A self-hosted SearXNG instance (JSON API), English-pinned."""
    params = {"q": query, "format": "json", "language": "en", "categories": "general"}
    if fresh:
        params["time_range"] = fresh
    async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT_S) as client:
        response = await client.get(f"{settings.searxng_url.rstrip('/')}/search", params=params)
        response.raise_for_status()
    return [{"title": r.get("title", ""), "url": r.get("url", ""),
             "snippet": r.get("content", ""), "published": r.get("publishedDate") or ""}
            for r in response.json().get("results", [])[:limit] if r.get("url")]


async def _brave(query: str, limit: int, fresh: str | None) -> list[dict]:
    """Brave Search API (a key in SEYMOUR_BRAVE_API_KEY)."""
    params = {"q": query, "count": min(limit, 20)}
    if fresh:
        params["freshness"] = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}[fresh]
    async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT_S) as client:
        response = await client.get(
            "https://api.search.brave.com/res/v1/web/search", params=params,
            headers={"Accept": "application/json",
                     "X-Subscription-Token": settings.brave_api_key})
        response.raise_for_status()
    results = (response.json().get("web") or {}).get("results") or []
    return [{"title": r.get("title", ""), "url": r.get("url", ""),
             "snippet": r.get("description", ""), "published": r.get("age") or ""}
            for r in results[:limit] if r.get("url")]


async def _duckduckgo(query: str, limit: int, fresh: str | None) -> list[dict]:
    """DuckDuckGo's HTML endpoint: no key, no setup. Results are anchors
    (`result__a`) each followed by a snippet anchor (`result__snippet`);
    the href is a redirect wrapper carrying the real URL in `uddg`."""
    params = {"q": query, "kl": "us-en"}
    if fresh:
        params["df"] = fresh[0]                   # d / w / m / y
    body = ""
    for attempt in range(2):
        async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT},
                                     timeout=SEARCH_TIMEOUT_S, follow_redirects=True) as client:
            response = await client.get("https://html.duckduckgo.com/html/", params=params)
            response.raise_for_status()
        body = response.text
        # A burst of searches earns a bot-check page instead of results
        # (measured mid-eval: three research runs, then six empty
        # searches). It has no result anchors and says so in its markup —
        # that is a throttle, not "no results": wait once, then retry.
        throttled = "result__a" not in body and (
            "anomaly" in body.lower() or "challenge" in body.lower()
            or "bots" in body.lower())
        if not throttled:
            break
        logger.warning("duckduckgo served a bot-check page (attempt %d)", attempt + 1)
        await asyncio.sleep(2.5)
    anchors = [(m.start(), "a", m.group(1), m.group(2)) for m in re.finditer(
        r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        body, flags=re.DOTALL)]
    snippets = [(m.start(), "s", "", m.group(1)) for m in re.finditer(
        r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', body, flags=re.DOTALL)]
    results: list[dict] = []
    current: dict | None = None
    for _pos, kind, href, text in sorted(anchors + snippets):
        clean = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text))).strip()
        if kind == "a":
            query_string = urllib.parse.urlparse(href).query
            real = urllib.parse.parse_qs(query_string).get("uddg", [href])[0]
            if real.startswith("//"):
                real = "https:" + real
            current = {"title": clean, "url": real, "snippet": "", "published": ""}
            results.append(current)
        elif current is not None and not current["snippet"]:
            current["snippet"] = clean
        if len(results) > limit:
            break
    return results[:limit]


def _dedupe(results: list[dict]) -> list[dict]:
    """Collapse the same page reached by different URLs (fragment,
    trailing slash, host case)."""
    seen: set[str] = set()
    kept = []
    for r in results:
        parsed = urllib.parse.urlparse(r["url"])
        key = (parsed.netloc.lower(), parsed.path.rstrip("/"), parsed.query)
        if key in seen or not parsed.netloc:
            continue
        seen.add(key)
        kept.append(r)
    return kept


async def search_results(query: str, limit: int = 8, freshness: str | None = None) -> list[dict]:
    """Structured search results [{title, url, snippet, published}, …].

    The provider CHAIN: SearXNG if configured, Brave if keyed, DuckDuckGo
    always. The first provider that answers with results wins; a failing
    or empty one falls through with a log line, never an exception.
    """
    fresh = _freshness(freshness or "")
    chain = []
    if settings.searxng_url:
        chain.append(("searxng", _searxng))
    if settings.brave_api_key:
        chain.append(("brave", _brave))
    chain.append(("duckduckgo", _duckduckgo))
    for name, provider in chain:
        try:
            results = _dedupe(await provider(query, limit, fresh))
        except Exception as error:
            logger.warning("search provider %s failed: %s", name, error)
            continue
        if results:
            return results[:limit]
    return []


async def web_search(query: str, freshness: str = "") -> str:
    """Tool entry: numbered results with snippets, ready to read."""
    query = (query or "").strip()
    if not query:
        return "Error: web_search needs a query"
    results = await search_results(query, limit=8, freshness=freshness)
    if not results:
        return (f"No results for {query!r}. Try different words, fewer words, "
                "or drop the freshness filter.")
    lines = []
    for index, r in enumerate(results, 1):
        when = f" ({r['published'][:10]})" if r.get("published") else ""
        lines.append(f"{index}. {r['title'] or r['url']}{when}\n   {r['url']}")
        if r.get("snippet"):
            lines.append(f"   {r['snippet'][:300]}")
    lines.append("\nRead a result with fetch_page(url) — pass a focus to pull "
                 "just the relevant passages.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
#  The tools                                                                   #
# --------------------------------------------------------------------------- #

from seymour.tools import Tool                      # noqa: E402  (registry type)

TOOLS = [
    Tool(
        name="web_search",
        description=("Search the web. Returns numbered results with titles, URLs "
                     "and snippets. For news or anything time-sensitive pass "
                     "freshness (day | week | month | year)."),
        args={"query": "the search query — specific words beat questions",
              "freshness": "day | week | month | year — only recent results"},
        optional=frozenset({"freshness"}),
        tier="read",
        func=web_search,
    ),
    Tool(
        name="fetch_page",
        description=("Fetch a public web page, PDF or text file and return its "
                     "readable content (headings, paragraphs, lists, tables, code). "
                     "Long documents are cut with a note; pass focus to get the "
                     "passages that match instead of the beginning."),
        args={"url": "the http(s) URL",
              "focus": "what you are looking for on the page (words or a question)"},
        optional=frozenset({"focus"}),
        tier="read",
        func=fetch_page,
    ),
]
