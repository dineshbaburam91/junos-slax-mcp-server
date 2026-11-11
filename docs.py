"""Download, cache, and search the Junos XML RPC and SLAX reference sources.

Sources can be PDFs (the Juniper Day One books), a single HTML manual
(the libslax SLAX language manual), a GitHub wiki, or a SLAX script archive.
Each source is fetched once into a local cache (or read from the repo when its
"url" is a relative path), its text is extracted into
labelled sections ("pages"), and a lightweight keyword scorer surfaces the
most relevant passages for a question or script-generation request.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import ssl
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader


def _ssl_context() -> ssl.SSLContext | bool:
    """Prefer the OS trust store so sites with partial chains still verify."""
    try:
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # noqa: BLE001 - fall back to httpx's default (certifi)
        return True


# Catalog of the reference sources this server knows about.
DOCUMENTS: dict[str, dict[str, str]] = {
    "xml-hierarchy": {
        "kind": "pdf",
        "title": "Day One: Navigating the Junos XML Hierarchy",
        "url": "https://www.juniper.net/documentation/en_US/day-one-books/archive/DO_Navigating_Junos_XML_Hierarchy.pdf",
        "topic": "Junos XML RPC requests/responses and the XML configuration hierarchy",
    },
    "automation-reference": {
        "kind": "pdf",
        "title": "This Week: Junos Automation Reference",
        "url": "https://www.juniper.net/documentation/en_US/day-one-books/archive/TW_Junos_Automation_Reference.pdf",
        "topic": "SLAX scripting language, op/commit/event scripts, and automation",
    },
    "slax-manual": {
        "kind": "html",
        "title": "libslax SLAX Language Manual",
        "url": "https://juniper.github.io/libslax/slax-manual.html",
        "topic": "SLAX language syntax, statements, and script structure reference",
    },
    "libslax-wiki": {
        "kind": "wiki",
        "title": "libslax Wiki",
        "url": "https://github.com/Juniper/libslax/wiki",
        "topic": "libslax build, usage, tooling, and SLAX examples",
    },
    "slax-1-1": {
        "kind": "wiki",
        "title": "What's New in SLAX 1.1",
        "url": "https://github.com/Juniper/libslax/wiki/NewIn1-1",
        "topic": "SLAX 1.1 language features and syntax changes",
    },
    "junoscriptorium": {
        "kind": "slax-archive",
        "title": "Junoscriptorium SLAX Examples",
        "url": "https://github.com/Juniper/junoscriptorium/archive/refs/heads/master.tar.gz",
        "topic": "Community SLAX op, commit, and event script examples",
    },
    "translation-scripts": {
        "kind": "pdf",
        "title": "Translation Scripts",
        "url": "docs/TranslationScripts.pdf",
        "topic": "SLAX translation scripts mapping YANG/foreign-schema config to native Junos config",
    },
}

# Sources whose "url" has no scheme are read from this repo directory.
BASE_DIR = Path(__file__).resolve().parent

# Cache directory can be overridden so the server works in read-only installs.
CACHE_DIR = Path(
    os.environ.get("JUNOS_SLAX_CACHE_DIR", Path.home() / ".cache" / "junos-slax-mcp")
)

# Cap how many wiki pages we crawl so a single source can't explode.
MAX_WIKI_PAGES = 50
# Target words per HTML/wiki section so snippets stay focused.
SECTION_WORD_LIMIT = 400

_WORD_RE = re.compile(r"[a-zA-Z0-9_][a-zA-Z0-9_\-]*")


def _tokenize(text: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(text)]


@dataclass
class Page:
    doc_id: str
    number: int  # 1-based section/page number within the document
    text: str
    label: str = ""  # heading or page name for citation
    tokens: set[str] = field(default_factory=set)


@dataclass
class Document:
    doc_id: str
    title: str
    url: str
    topic: str
    kind: str
    pages: list[Page] = field(default_factory=list)
    error: str | None = None

    @property
    def page_count(self) -> int:
        return len(self.pages)


class DocStore:
    """Lazily loads and holds the extracted source text in memory."""

    def __init__(self) -> None:
        self._docs: dict[str, Document] = {}

    # --- fetching / caching -------------------------------------------------

    def _fetch(self, url: str) -> bytes:
        if not urlparse(url).scheme:
            return (BASE_DIR / url).read_bytes()
        key = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        dest = CACHE_DIR / key
        if dest.exists() and dest.stat().st_size > 0:
            return dest.read_bytes()
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with httpx.Client(
            follow_redirects=True,
            timeout=60.0,
            verify=_ssl_context(),
            headers={"User-Agent": "junos-slax-mcp/0.1"},
        ) as client:
            resp = client.get(url)
            resp.raise_for_status()
            dest.write_bytes(resp.content)
        return resp.content

    # --- extraction per source kind ----------------------------------------

    def _extract_pdf(self, doc: Document) -> None:
        data = self._fetch(doc.url)
        reader = PdfReader(io.BytesIO(data))
        for idx, page in enumerate(reader.pages, start=1):
            try:
                text = page.extract_text() or ""
            except Exception:  # noqa: BLE001 - one bad page shouldn't abort
                text = ""
            doc.pages.append(
                Page(
                    doc_id=doc.doc_id,
                    number=idx,
                    text=text,
                    label=f"page {idx}",
                    tokens=set(_tokenize(text)),
                )
            )

    def _extract_html(self, doc: Document) -> None:
        html = self._fetch(doc.url).decode("utf-8", errors="replace")
        for label, text in _html_to_sections(html):
            doc.pages.append(
                Page(
                    doc_id=doc.doc_id,
                    number=len(doc.pages) + 1,
                    text=text,
                    label=label,
                    tokens=set(_tokenize(text)),
                )
            )

    def _extract_slax_archive(self, doc: Document) -> None:
        data = self._fetch(doc.url)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for member in archive:
                if not member.isfile() or not member.name.endswith(".slax"):
                    continue
                source = archive.extractfile(member)
                if source is None:
                    continue
                path = member.name.partition("/")[2]
                text = source.read().decode("utf-8", errors="replace")
                for label, chunk in _chunk_text(text, path):
                    doc.pages.append(
                        Page(
                            doc_id=doc.doc_id,
                            number=len(doc.pages) + 1,
                            text=chunk,
                            label=label,
                            tokens=set(_tokenize(path + " " + chunk)),
                        )
                    )

    def _extract_wiki(self, doc: Document) -> None:
        home_html = self._fetch(doc.url).decode("utf-8", errors="replace")
        page_urls = _wiki_page_links(doc.url, home_html)
        ordered = [doc.url] + [u for u in page_urls if u != doc.url]
        seen: set[str] = set()
        for page_url in ordered[:MAX_WIKI_PAGES]:
            if page_url in seen:
                continue
            seen.add(page_url)
            try:
                html = self._fetch(page_url).decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001 - skip unreachable wiki pages
                continue
            name = _wiki_page_name(page_url)
            body = _markdown_body(html)
            for chunk_label, chunk in _chunk_text(body, name):
                doc.pages.append(
                    Page(
                        doc_id=doc.doc_id,
                        number=len(doc.pages) + 1,
                        text=chunk,
                        label=chunk_label,
                        tokens=set(_tokenize(chunk)),
                    )
                )

    def _extract(self, doc_id: str) -> Document:
        meta = DOCUMENTS[doc_id]
        doc = Document(
            doc_id=doc_id,
            title=meta["title"],
            url=meta["url"],
            topic=meta["topic"],
            kind=meta["kind"],
        )
        try:
            if meta["kind"] == "pdf":
                self._extract_pdf(doc)
            elif meta["kind"] == "html":
                self._extract_html(doc)
            elif meta["kind"] == "wiki":
                self._extract_wiki(doc)
            elif meta["kind"] == "slax-archive":
                self._extract_slax_archive(doc)
            else:  # pragma: no cover - guarded by catalog
                raise ValueError(f"Unknown source kind '{meta['kind']}'")
        except Exception as exc:  # noqa: BLE001 - keep server up if one source fails
            doc.error = f"{type(exc).__name__}: {exc}"
        return doc

    # --- public API ---------------------------------------------------------

    def get(self, doc_id: str) -> Document:
        if doc_id not in DOCUMENTS:
            raise KeyError(
                f"Unknown document id '{doc_id}'. Valid ids: {', '.join(DOCUMENTS)}"
            )
        if doc_id not in self._docs:
            self._docs[doc_id] = self._extract(doc_id)
        return self._docs[doc_id]

    def all_docs(self) -> list[Document]:
        return [self.get(doc_id) for doc_id in DOCUMENTS]

    def search(
        self, query: str, doc_id: str | None = None, max_results: int = 6
    ) -> list[dict[str, object]]:
        """Score sections by query-term overlap and return the best snippets."""
        terms = _tokenize(query)
        if not terms:
            return []
        term_set = set(terms)

        docs = [self.get(doc_id)] if doc_id else self.all_docs()
        scored: list[tuple[float, Page]] = []
        for doc in docs:
            for page in doc.pages:
                if not page.tokens:
                    continue
                overlap = term_set & page.tokens
                if not overlap:
                    continue
                score = float(len(overlap))
                low_text = page.text.lower()
                for term in term_set:
                    if len(term) > 2:
                        score += low_text.count(term) * 0.1
                if query.lower() in low_text:
                    score += 5
                scored.append((score, page))

        scored.sort(key=lambda item: item[0], reverse=True)
        results: list[dict[str, object]] = []
        for score, page in scored[:max_results]:
            meta = DOCUMENTS[page.doc_id]
            results.append(
                {
                    "doc_id": page.doc_id,
                    "title": meta["title"],
                    "section": page.label or f"section {page.number}",
                    "page": page.number,
                    "score": round(score, 2),
                    "snippet": _make_snippet(page.text, term_set),
                }
            )
        return results


# --- HTML helpers -----------------------------------------------------------


def _clean_soup(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "head"]):
        tag.decompose()
    return soup


def _html_to_sections(html: str) -> list[tuple[str, str]]:
    """Split an HTML page into labelled sections keyed by nearest heading."""
    soup = _clean_soup(html)
    root = soup.body or soup
    headings = {"h1", "h2", "h3", "h4", "h5", "h6"}
    blocks = {"p", "pre", "li", "blockquote", "dt", "dd", "td", "code"}

    sections: list[tuple[str, list[str]]] = []
    current_label = soup.title.get_text(strip=True) if soup.title else "Introduction"
    current_parts: list[str] = []

    for el in root.find_all(headings | blocks):
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        if el.name in headings:
            if current_parts:
                sections.append((current_label, current_parts))
            current_label = text
            current_parts = []
        else:
            current_parts.append(text)
    if current_parts:
        sections.append((current_label, current_parts))

    out: list[tuple[str, str]] = []
    for label, parts in sections:
        joined = "\n".join(parts)
        out.extend(_chunk_text(joined, label))
    return out


def _markdown_body(html: str) -> str:
    soup = _clean_soup(html)
    body = soup.find("div", class_="markdown-body")
    if body is None:
        body = soup.find("div", id="wiki-body") or soup.body or soup
    return body.get_text("\n", strip=True)


def _wiki_page_links(base_url: str, html: str) -> list[str]:
    soup = _clean_soup(html)
    parsed = urlparse(base_url)
    wiki_prefix = parsed.path.rstrip("/")  # e.g. /Juniper/libslax/wiki
    links: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        abs_url = urljoin(base_url, str(a["href"]))
        path = urlparse(abs_url).path
        if not path.startswith(wiki_prefix):
            continue
        if path in (wiki_prefix, wiki_prefix + "/"):
            continue
        if any(seg in path for seg in ("/_history", "/_edit", "/_new", "/_compare")):
            continue
        clean = abs_url.split("#")[0].split("?")[0]
        if clean not in seen:
            seen.add(clean)
            links.append(clean)
    return links


def _wiki_page_name(url: str) -> str:
    name = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    return name.replace("-", " ") or "Home"


# --- text helpers -----------------------------------------------------------


def _chunk_text(text: str, label: str) -> list[tuple[str, str]]:
    """Break long text into word-bounded chunks, each carrying the label."""
    words = text.split()
    if not words:
        return []
    if len(words) <= SECTION_WORD_LIMIT:
        return [(label, text)]
    chunks: list[tuple[str, str]] = []
    total = (len(words) + SECTION_WORD_LIMIT - 1) // SECTION_WORD_LIMIT
    for i in range(0, len(words), SECTION_WORD_LIMIT):
        part_no = i // SECTION_WORD_LIMIT + 1
        chunk_label = label if total == 1 else f"{label} ({part_no}/{total})"
        chunks.append((chunk_label, " ".join(words[i : i + SECTION_WORD_LIMIT])))
    return chunks


def _make_snippet(text: str, terms: set[str], window: int = 600) -> str:
    """Return a readable excerpt centered on the first matching term."""
    collapsed = re.sub(r"\s+", " ", text).strip()
    if not collapsed:
        return ""
    low = collapsed.lower()
    pos = -1
    for term in terms:
        found = low.find(term)
        if found != -1 and (pos == -1 or found < pos):
            pos = found
    if pos == -1:
        return collapsed[:window]
    start = max(0, pos - window // 2)
    end = min(len(collapsed), start + window)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(collapsed) else ""
    return f"{prefix}{collapsed[start:end]}{suffix}"
