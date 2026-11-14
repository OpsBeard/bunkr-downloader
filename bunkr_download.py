r"""Sequential Bunkr album downloader for Windows Terminal (Python 3.10+).

Setup: py -m pip install requests "json5>=0.10"
Run:   py .\bunkr_download.py

Open Windows Terminal as administrator to enable Microsoft Defender scans.
"""

import argparse
import base64
import ctypes
import hashlib
import json
import shutil
import sqlite3
import webbrowser
import os
import random
import re
import stat
import subprocess
import sys
import tempfile
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, unquote_plus, urlencode, urljoin, urlsplit, urlunsplit

try:
    import json5
    import requests
except ImportError:
    sys.exit('Install dependencies first: py -m pip install requests "json5>=0.10"')


INDEX_HOSTS = {"balbums.st", "bunkr-albums.io"}
ALBUM_HOST = re.compile(r"(?:app\.)?bunkr+\.[a-z]{2,8}")
ALBUM_PATH = re.compile(r"/a/([A-Za-z0-9_-]+)/?")
FILE_PAGE_PATH = re.compile(r"/[fvid]/([^/]+)/?")
FILE_ID_PATH = re.compile(r"/file/([0-9]+)/?")
CDN_DOMAINS = ("cdn.cr", "scdn.st", "cdn.bunkr.ru")
CDN_HOST = re.compile(r"(?:[a-z0-9-]+\.)*(?:" + "|".join(map(re.escape, CDN_DOMAINS)) + r")")
ALBUM_FILES = re.compile(r"\bwindow\.albumFiles\s*=\s*(?=\[)")
PAGE_COUNT = re.compile(r"\bpage\s+(\d+)\s*(?:of|/)\s*(\d+)\b", re.I)
NO_RESULTS = re.compile(
    r"\b(?:no (?:matching )?(?:albums|results)|0 results|page 1\s*(?:of|/)\s*0)\b", re.I
)
INVALID_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f-\x9f\ud800-\udfff]')
DEVICE_NAME = re.compile(r"(?:CON|PRN|AUX|NUL|COM[0-9¹²³]|LPT[0-9¹²³])", re.I)
TIMEOUT = (15, 45)
RETRIES = 3
SKIPPED = object()
DUPLICATE_FOLDER = ".bunkr-duplicates"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)


class DownloadError(Exception):
    pass


@dataclass(frozen=True)
class Upload:
    slug: str
    original: str
    size: int
    page_url: str | None = None


def display(value):
    return "".join(char if char.isprintable() else "?" for char in str(value))


SUMMARY_LINES = None


def log(message):
    line = display(message)
    print(line, flush=True)
    if SUMMARY_LINES is not None:
        SUMMARY_LINES.append(line)


def https_url(url):
    try:
        parts = urlsplit(url)
        if (
            parts.scheme == "https" and parts.hostname and parts.username is None
            and parts.password is None and parts.port in (None, 443)
        ):
            return parts
    except (TypeError, ValueError):
        pass
    raise DownloadError("Expected a valid HTTPS URL.")


class Page(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.links = []
        self.title = ""
        self.text = []
        self.hidden = None
        self.feed(source)
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs)
        elif tag == "meta" and attrs.get("property") == "og:title":
            self.title = attrs.get("content") or ""
        elif tag in {"script", "style"}:
            self.hidden = tag

    def handle_endtag(self, tag):
        if tag == self.hidden:
            self.hidden = None

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.text.append(data.strip())


def request(session, method, url, label, allowed_host, allowed_status=(200,), **kwargs):
    """Follow HTTPS redirects only within the hosts expected for this step."""
    for _ in range(8):
        hostname = https_url(url).hostname
        if not allowed_host(hostname):
            raise DownloadError(f"{label} pointed to an unexpected host ({hostname}).")
        try:
            response = session.request(
                method, url, timeout=TIMEOUT, allow_redirects=False, **kwargs
            )
        except requests.RequestException as exc:
            raise DownloadError(f"[{type(exc).__name__}] {label}: Could not connect to the site or finish the request.") from None
        if response.is_redirect:
            with response:
                if method != "GET":
                    raise DownloadError(f"{label} returned an unexpected redirect.")
                url = urljoin(response.url or url, response.headers["Location"])
                kwargs.pop("params", None)
            continue
        if response.status_code not in allowed_status:
            response.close()
            raise DownloadError(f"[HTTP {response.status_code}] {label}: {http_explanation(response.status_code)}")
        return response
    raise DownloadError(f"{label}: too many redirects.")


def fetch_page(session, url, label, allowed_host):
    with request(session, "GET", url, label, allowed_host) as response:
        response.encoding = "utf-8"
        return response.text, response.url


def search_params(parts):
    return parse_qs(parts.query, keep_blank_values=True)


def index_host(host):
    return host.removeprefix("www.") in INDEX_HOSTS


def parse_search_url(search_url):
    parts = https_url(search_url)
    if not index_host(parts.hostname):
        raise DownloadError("Use a balbums.st or bunkr-albums.io search URL.")
    query = search_params(parts)
    if len(query.get("search", [])) != 1 or not query["search"][0].strip():
        raise DownloadError("Paste the search results URL, including ?search=.")
    # A link copied from a later page still starts the search at page one.
    query.pop("page", None)
    return parts._replace(query=urlencode(query, doseq=True), fragment="")


def find_albums(session, parts):
    query = search_params(parts)
    next_url = parts.geturl()
    albums = {}
    visited_pages = set()
    for page_number in range(1, sys.maxsize):
        label = f"Search page {page_number}"
        log(f"Reading {label.lower()}...")
        source, final_url = fetch_page(session, next_url, label, index_host)
        page = Page(source)
        page_albums = {}
        next_link = None
        for link in page.links:
            try:
                target = https_url(urljoin(final_url, link["href"]))
            except DownloadError:
                continue
            album = ALBUM_PATH.fullmatch(target.path)
            if album and ALBUM_HOST.fullmatch(target.hostname):
                album_id = album.group(1)
                page_albums[album_id] = urlunsplit(
                    ("https", target.netloc, f"/a/{album_id}", "", "")
                )
            if index_host(target.hostname):
                params = search_params(target)
                if (
                    params.get("search") == query["search"]
                    and params.get("page") == [str(page_number + 1)]
                ):
                    next_link = target._replace(fragment="").geturl()

        signature = tuple(page_albums)
        summary = " ".join(page.text)
        count = PAGE_COUNT.search(summary)
        if not signature or signature in visited_pages:
            if not albums and NO_RESULTS.search(summary):
                return []
            raise DownloadError(f"{label} returned no new album listing. Search may be incomplete.")
        visited_pages.add(signature)
        for album_id, album_url in page_albums.items():
            albums.setdefault(album_id, album_url)
        if next_link:
            next_url = next_link
        elif count and int(count.group(2)) > page_number:
            query["page"] = [str(page_number + 1)]
            next_url = parts._replace(query=urlencode(query, doseq=True)).geturl()
        else:
            return list(albums.values())


def file_page_segment(value):
    """Normalize a Bunkr identifier to a single URL path segment."""
    if not isinstance(value, str) or not value:
        return None
    try:
        segment = unquote(value, errors="strict")
        if (
            not segment.strip() or segment in {".", ".."}
            or "/" in segment or "\\" in segment or not segment.isprintable()
        ):
            return None
        return quote(segment, safe="")
    except UnicodeError:
        return None


def file_page_link(href, album_url):
    if not isinstance(href, str):
        return None
    try:
        target = https_url(urljoin(album_url, href))
    except (DownloadError, ValueError):
        return None
    match = FILE_PAGE_PATH.fullmatch(target.path)
    if not match or not ALBUM_HOST.fullmatch(target.hostname):
        return None
    segment = file_page_segment(match.group(1))
    if not segment:
        return None
    path = target.path[:3] + segment
    return segment, urlunsplit(("https", target.netloc, path, "", ""))


def read_album(session, album_url):
    album_html, final_url = fetch_page(
        session, album_url + "?advanced=1", "Album listing", ALBUM_HOST.fullmatch
    )
    marker = ALBUM_FILES.search(album_html)
    if not marker:
        raise DownloadError("Album listing did not contain a file manifest.")
    try:
        items = json5.loads(album_html[marker.end():], consume_trailing=False)
    except ValueError:
        raise DownloadError("Could not parse the album file manifest.") from None
    if not isinstance(items, list):
        raise DownloadError("The album file manifest was not a list.")
    page = Page(album_html)
    file_pages = {}
    for link in page.links:
        matched = file_page_link(link["href"], final_url)
        if matched:
            segment, url = matched
            file_pages.setdefault(segment, url)
    return page.title, final_url, items, file_pages


def read_upload(item, album_url=None, file_pages=None):
    if not isinstance(item, dict):
        raise DownloadError("Invalid file entry in the album manifest.")
    name, size = item.get("original") or item.get("name"), item.get("size")
    if not isinstance(name, str) or not name.strip() or type(size) is not int or size < 0:
        raise DownloadError("File entry is missing a filename or an exact byte size.")
    if album_url:
        for field in ("pageUrl", "url", "href", "slug"):
            matched = file_page_link(item.get(field), album_url)
            if matched:
                slug, url = matched
                return Upload(slug, name, size, url)
    slug = file_page_segment(item.get("slug"))
    if slug:
        return Upload(slug, name, size, (file_pages or {}).get(slug))
    stored_name = file_page_segment(item.get("name"))
    if stored_name and file_pages and stored_name in file_pages:
        return Upload(stored_name, name, size, file_pages[stored_name])
    reason = "invalid slug" if item.get("slug") else "missing slug"
    raise DownloadError(f"No usable file-page link for {name!r} ({reason}).")


def fetch_object(session, method, url, label, allowed_host, **kwargs):
    with request(session, method, url, label, allowed_host, **kwargs) as response:
        try:
            result = response.json()
        except ValueError:
            raise DownloadError(f"{label} did not return JSON.") from None
        if not isinstance(result, dict):
            raise DownloadError(f"{label} returned unexpected data.")
        return result


def signed_download(session, album_url, upload):
    page_url = upload.page_url or urljoin(album_url, f"/f/{upload.slug}")
    html, final_url = fetch_page(
        session, page_url, "File page", ALBUM_HOST.fullmatch
    )
    for link in Page(html).links:
        try:
            resolver = https_url(urljoin(final_url, link["href"]))
        except DownloadError:
            continue
        file_id = FILE_ID_PATH.fullmatch(resolver.path)
        if (
            file_id and resolver.hostname.startswith("dl.")
            and ALBUM_HOST.fullmatch(resolver.hostname[3:])
            and link.get("data-file-id", file_id.group(1)) == file_id.group(1)
        ):
            break
    else:
        raise DownloadError("The file page did not contain a download button.")

    origin = f"https://{resolver.netloc}"
    media = fetch_object(
        session, "POST", f"{origin}/api/_001_v2", "Download resolver",
        lambda host: host == resolver.hostname,
        json={"id": file_id.group(1)}, headers={"Origin": origin, "Referer": resolver.geturl()},
    )
    base, path = media.get("mediafiles"), media.get("path")
    if not isinstance(base, str) or not isinstance(path, str) or not path.startswith("/storage/media/"):
        raise DownloadError("Download resolver did not return a media path.")
    media_url = https_url(base.rstrip("/") + path)
    if not CDN_HOST.fullmatch(media_url.hostname):
        raise DownloadError(
            f"Download resolver returned an unexpected media host ({media_url.hostname})."
        )
    signer = "glb-apisign.cdn.cr"
    signature = fetch_object(
        session, "GET", f"https://{signer}/sign", "Download signer",
        lambda host: host == signer, params={"path": media_url.path},
    )
    token, expiry = signature.get("token"), signature.get("ex")
    if not isinstance(token, str) or not token or type(expiry) not in (int, str) or not expiry:
        raise DownloadError("The download signer did not issue a token.")
    name = media.get("original") or upload.original
    return media_url.geturl(), {"n": name, "token": token, "ex": expiry}


def windows_name(name):
    name = INVALID_NAME.sub("_", name).rstrip(" .")
    if not name:
        name = "_"
    if DEVICE_NAME.fullmatch(name.partition(".")[0].rstrip(" ")):
        name = "_" + name
    return name


def file_size(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISREG(info.st_mode) and not (
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        return info.st_size
    return -1


def real_directory(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(info.st_mode) and not (
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def regular_files(folder, on_error, exclude_archive=False):
    for root, directories, filenames in os.walk(folder, onerror=on_error):
        parent = Path(root)
        children = []
        for name in directories:
            if exclude_archive and (
                (parent == folder and name.lower() in INTERNAL_FOLDERS)
                or name.startswith((".bunkr-encode-", ".bunkr-benchmark-"))
            ):
                continue
            try:
                if real_directory(parent / name):
                    children.append(name)
            except OSError as exc:
                on_error(exc)
        directories[:] = children
        for name in filenames:
            if exclude_archive and parent == folder and (name.casefold().startswith(CATALOG_NAME) or name.casefold().startswith(SUMMARY_PREFIX)):
                continue
            if name.startswith(".bunkr-") and name.endswith(".part"):
                continue
            path = parent / name
            try:
                size = file_size(path)
            except OSError as exc:
                on_error(exc)
                continue
            if size is not None and size >= 0:
                yield path, size


def archived_downloads(folder):
    archive = folder / DUPLICATE_FOLDER
    try:
        if not real_directory(archive):
            return {}
    except OSError as exc:
        log(f"Duplicate folder read error | {exc}")
        return {}
    log(f"  Checking {DUPLICATE_FOLDER} for existing filenames and sizes...")
    matches = {}
    last_update = time.monotonic()
    for count, (path, size) in enumerate(
        regular_files(archive, lambda exc: log(f"Duplicate folder read error | {exc}")), 1
    ):
        # The first component is the batch folder. Match only paths the downloader
        # would have checked before the file was moved.
        original = path.relative_to(archive).parts[1:]
        if len(original) == 1 or (len(original) == 4 and original[0] == ".bunkr-conflicts"):
            matches.setdefault((os.path.normcase(path.name), size), path)
        now = time.monotonic()
        if now - last_update >= 5:
            log(f"  Duplicate folder | {count:,} files checked")
            last_update = now
    log(f"  Duplicate folder | {len(matches):,} filename and size matches available")
    return matches


def fingerprint(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


CHUNK_BYTES = 4 * 1024 * 1024
CATALOG_NAME = "bunkr_catalog.sqlite3"
RAW_FOLDER = "processed_raws"
PARTIAL_FOLDER = ".bunkr-partials"
INTERNAL_FOLDERS = {DUPLICATE_FOLDER, RAW_FOLDER, PARTIAL_FOLDER}
SUMMARY_PREFIX = "bunkr-summary-"


def managed_directory(root, name):
    folder = root / name
    folder.mkdir(exist_ok=True)
    if not real_directory(folder):
        raise OSError(f"[FOLDER] {folder} is not an ordinary directory.")
    return folder


def write_json(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False, mode="w", encoding="utf-8") as out:
        temporary = Path(out.name)
        try:
            json.dump(data, out)
            out.flush()
            os.fsync(out.fileno())
        except BaseException:
            out.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def publish_file(source, target):
    if os.name == "nt":
        source.rename(target)
    else:
        os.link(source, target)
        source.unlink()


def hash_file(path):
    if file_size(path) in (None, -1):
        raise OSError(f"File is missing or is not a regular file: {path}")
    before = fingerprint(path.lstat())
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        opened = fingerprint(os.fstat(handle.fileno()))
        # Windows can report different creation/change times for a path and
        # an open handle. Compare size and last-write time across the APIs,
        # then use full fingerprints within each API to detect changes.
        if os.name == "nt":
            different_file = bool(opened[1] and before[1] and opened[:2] != before[:2])
            changed = different_file or opened[2:4] != before[2:4]
        else:
            changed = opened != before
        if changed:
            raise OSError(f"File changed before hashing: {path}")
        bytes_read = 0
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            bytes_read += len(block)
        if bytes_read != before[2] or fingerprint(os.fstat(handle.fileno())) != opened:
            raise OSError(f"File changed while hashing: {path}")
        if fingerprint(path.lstat()) != before:
            raise OSError(f"File changed or moved while hashing: {path}")
    return digest.hexdigest(), before


def upload_key(album_url, upload):
    return f"{urlsplit(album_url).path.rstrip('/')}/{upload.slug}"


@contextmanager
def collection_lock(folder):
    path = folder / (CATALOG_NAME + ".lock")
    if file_size(path) == -1:
        raise OSError("[CATALOG_LOCK] The collection lock is not a regular file.")
    with path.open("a+b") as lock:
        try:
            if os.name == "nt":
                import msvcrt
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise OSError("[CATALOG_LOCK] Another run is using this collection, or the folder does not support locking.") from None
        yield


class Catalog:
    def __init__(self, folder):
        self.folder = folder
        self.files = {}
        self.locations = {}
        path = folder / CATALOG_NAME
        if any(file_size(folder / (CATALOG_NAME + suffix)) == -1 for suffix in ("", "-journal", "-wal", "-shm")):
            raise OSError(f"[CATALOG_PATH] {path} is not a regular file.")
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, stamp TEXT, digest TEXT);
            CREATE TABLE IF NOT EXISTS uploads (id TEXT PRIMARY KEY, size INTEGER, digest TEXT);
            CREATE TABLE IF NOT EXISTS aliases (
                name TEXT, size INTEGER, digest TEXT, PRIMARY KEY(name, size, digest)
            );
            CREATE TABLE IF NOT EXISTS replacements (original TEXT PRIMARY KEY, encoded TEXT);
            CREATE TABLE IF NOT EXISTS pending_encodes (original TEXT PRIMARY KEY, encoded TEXT);

        """)

    def remember(self, path, *, index=True):
        rel = str(path.relative_to(self.folder))
        stamp = fingerprint(path.lstat())
        stamp_text = json.dumps(stamp)
        row = self.db.execute("SELECT digest FROM files WHERE path=? AND stamp=?", (rel, stamp_text)).fetchone()
        if row:
            digest = row[0]
        else:
            digest, stamp = hash_file(path)
            self.db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?)", (rel, json.dumps(stamp), digest))
        if index:
            self.files[path] = (digest, stamp)
            self.locations.setdefault(digest, set()).add(path)
        return digest

    def alias(self, name, size, digest):
        self.db.execute("INSERT OR IGNORE INTO aliases VALUES (?,?,?)", (name.casefold(), size, digest))

    def match_archived(self, album_url, upload, names, archived):
        known = self.db.execute(
            "SELECT digest FROM uploads WHERE id=? AND size=?",
            (upload_key(album_url, upload), upload.size),
        ).fetchone()
        for name in dict.fromkeys(names):
            held = archived.get((os.path.normcase(name), upload.size))
            if held is None:
                continue
            try:
                if file_size(held) != upload.size:
                    continue
                log(f"  Checking saved duplicate | {held.name} ({format_size(upload.size)})")
                with self.db:
                    digest = self.remember(held, index=False)
                    self.alias(name, upload.size, digest)
            except OSError as exc:
                log(f"  [ARCHIVE_READ] Could not verify {held.name}: {exc}")
                continue
            if known and known[0] != digest:
                continue
            return self.locate(album_url, upload, names) or (held, digest)
        return None

    def index(self):
        self.files.clear()
        self.locations.clear()
        errors = 0

        def report(exc):
            nonlocal errors
            errors += 1
            log(f"  [FILE_READ] Could not index a file: {exc}")

        with self.db:
            for index, (path, size) in enumerate(regular_files(self.folder, report, exclude_archive=True), 1):
                if index == 1 or index % 100 == 0:
                    log(f"  Indexing local files | {index:,} checked")
                try:
                    digest = self.remember(path)
                    self.alias(path.name, size, digest)
                except OSError as exc:
                    report(exc)
        return errors

    def recover_encodes(self):
        with self.db:
            for original, encoded in self.db.execute("SELECT original, encoded FROM pending_encodes").fetchall():
                if self.locations.get(encoded):
                    self.db.execute("INSERT OR REPLACE INTO replacements VALUES (?,?)", (original, encoded))
                    log("  Recovered a validated encode from a previous run. Any remaining original was kept.")
                self.db.execute("DELETE FROM pending_encodes WHERE original=?", (original,))

    def locate(self, album_url, upload, names):
        row = self.db.execute(
            "SELECT digest FROM uploads WHERE id=? AND size=?", (upload_key(album_url, upload), upload.size)
        ).fetchone()
        if row:
            digests = {row[0]}
        else:
            digests = {
                row[0] for name in names for row in self.db.execute(
                    "SELECT digest FROM aliases WHERE name=? AND size=?", (name.casefold(), upload.size)
                )
            }
        matches = {}
        for digest in digests:
            replacement = self.db.execute(
                "SELECT encoded FROM replacements WHERE original=?", (digest,)
            ).fetchone()
            for current in ([replacement[0], digest] if replacement else [digest]):
                for path in sorted(self.locations.get(current, ())):
                    try:
                        if file_size(path) == self.files[path][1][2] and fingerprint(path.lstat()) == self.files[path][1]:
                            matches[digest] = path
                            break
                    except FileNotFoundError:
                        continue
                if digest in matches:
                    break
        if len(matches) == 1 and (row or len(digests) == 1):
            digest, path = next(iter(matches.items()))
            return path, digest
        return None

    def bind(self, album_url, upload, name, path, digest=None):
        with self.db:
            digest = digest or self.remember(path)
            self.db.execute("INSERT OR REPLACE INTO uploads VALUES (?,?,?)",
                            (upload_key(album_url, upload), upload.size, digest))
            self.alias(name, upload.size, digest)
            self.alias(windows_name(upload.original), upload.size, digest)


def file_age(path):
    info = path.lstat()
    if hasattr(info, "st_birthtime"):
        return info.st_birthtime
    return info.st_ctime if os.name == "nt" else info.st_mtime


def resolve_duplicates(catalog):
    folder = catalog.folder
    groups = {}
    errors = 0
    for path, (digest, stamp) in catalog.files.items():
        try:
            age = file_age(path)
            groups.setdefault((stamp[2], digest), []).append((age, path))
        except OSError as exc:
            log(f"  [FILE_READ] Could not check the age of {path}: {exc}")
            errors += 1
    moved = 0
    batch = None
    for group in groups.values():
        if len(group) < 2:
            continue
        _, keeper = min(group)
        for _, path in group:
            if path == keeper:
                continue
            try:
                if any(fingerprint(p.lstat()) != catalog.files[p][1] for p in (path, keeper)):
                    raise OSError("A file changed after hashing.")
                if batch is None:
                    archive = managed_directory(folder, DUPLICATE_FOLDER)
                    batch = Path(tempfile.mkdtemp(prefix="run-", dir=archive))
                target = batch / path.relative_to(folder)
                target.parent.mkdir(parents=True, exist_ok=True)
                # All aliases were committed during indexing before any move.
                publish_file(path, target)
                moved += 1
            except OSError as exc:
                errors += 1
                log(f"  [DUPLICATE_MOVE] Could not move {path.relative_to(folder)}: {exc}")
    log(f"Duplicates | {moved:,} exact copies moved, {errors:,} errors. Oldest copies retained.")
    important(f"Keep {CATALOG_NAME} in the collection root. It maps old names and upload IDs to the files you kept, even after a rename or conversion.")
    if moved or (folder / DUPLICATE_FOLDER).is_dir():
        log(f"Review {DUPLICATE_FOLDER} before deleting anything in it.")
    return moved, errors


def http_explanation(status):
    return {
        403: "The site refused this request.",
        404: "The site could not find this file or page.",
        429: "The site is receiving too many requests.",
        503: "The site is temporarily unavailable.",
    }.get(status, "The site returned an error while serving the request.")


def enable_colors():
    if not sys.stdout.isatty():
        return False
    if os.name != "nt":
        return os.environ.get("TERM") != "dumb"
    import msvcrt
    kernel = ctypes.WinDLL("kernel32")
    handle = ctypes.c_void_p(msvcrt.get_osfhandle(sys.stdout.fileno()))
    mode = ctypes.c_uint()
    return bool(kernel.GetConsoleMode(handle, ctypes.byref(mode)) and kernel.SetConsoleMode(handle, mode.value | 4))


COLOR = False


def important(message):
    heading = "\x1b[1;31mIMPORTANT\x1b[0m" if COLOR else "IMPORTANT"
    print(f"{heading} | {display(message)}", flush=True)


def show_progress(received, size, started, offset=0):
    fraction = received / size if size else 1
    speed = (received - offset) / max(time.monotonic() - started, 0.001) / 2**20
    suffix = f" {fraction:6.1%} · {received:,}/{size:,} bytes · {speed:.1f} MiB/s"
    if not sys.stdout.isatty():
        if fraction == 1:
            log(suffix.strip())
        return
    filled = int(fraction * 24)
    line = "━" * filled
    if COLOR:
        red, green = min(255, int(510 * (1 - fraction))), min(230, int(460 * fraction))
        line = f"\x1b[38;2;{red};{green};55m{line}\x1b[0m"
    clear = "\x1b[2K" if COLOR else ""
    print(f"\r{clear}    {line}{'─' * (24 - filled)}{suffix}", end="", flush=True)


def destination(folder, name, size):
    # Keep the filename intact when another upload already uses it.
    normal = folder / name
    normal_size = file_size(normal)
    if normal_size == size:
        return normal, True

    conflicts = folder / ".bunkr-conflicts"
    conflict_root = conflicts / str(size)
    for directory in (conflicts, conflict_root):
        if os.path.lexists(directory) and not real_directory(directory):
            if normal_size is None:
                return normal, False
            raise DownloadError("The filename conflicts with an existing file and the conflict folder is unavailable.")
    first_free = None
    if real_directory(conflict_root):
        for directory in sorted(conflict_root.iterdir()):
            if not directory.name.isdecimal() or not real_directory(directory):
                continue
            candidate = directory / name
            existing_size = file_size(candidate)
            if existing_size == size:
                return candidate, True
            if existing_size is None and first_free is None:
                first_free = candidate

    if normal_size is None:
        return normal, False
    if first_free is not None:
        return first_free, False
    number = 1
    while os.path.lexists(conflict_root / str(number)):
        number += 1
    return conflict_root / str(number) / name, False


def transfer(session, album_url, upload, folder, name, archived=None):
    for local_name in dict.fromkeys((name, windows_name(upload.original))):
        path, exists = destination(folder, local_name, upload.size)
        if exists:
            return path, True
        held = (archived or {}).get((os.path.normcase(local_name), upload.size))
        if held is not None and file_size(held) == upload.size:
            return held, True

    url, params = signed_download(session, album_url, upload)
    token = hashlib.sha256(f"{album_url.split('?')[0]}:{upload.slug}:{upload.size}:{name}".encode()).hexdigest()
    # Save checkpoints next to the data so a later invocation can resume.
    work = managed_directory(folder, ".bunkr-partials")
    part, checkpoint = work / (token + ".part"), work / (token + ".json")
    for candidate in (part, checkpoint):
        if file_size(candidate) == -1:
            raise DownloadError("[PARTIAL_PATH] A download checkpoint is not a regular file.")
    resource = urlsplit(url)._replace(query="", fragment="").geturl()
    state = {"etag": None, "resource": resource, "chunks": []}
    offset = 0
    if checkpoint.exists():
        try:
            state = json.loads(checkpoint.read_text(encoding="utf-8"))
            if state["resource"] != resource:
                raise ValueError("the server returned a different file location")
            chunks = state["chunks"]
            if not isinstance(chunks, list) or not re.fullmatch(r'"[^"]*"', state["etag"] or ""):
                raise ValueError("missing strong server identifier")
            with part.open("rb") as saved:
                for size, digest in chunks:
                    if not isinstance(size, int) or not 0 < size <= CHUNK_BYTES or offset + size > upload.size:
                        raise ValueError("invalid saved chunk length")
                    block = saved.read(size)
                    if len(block) != size or hashlib.sha256(block).hexdigest() != digest:
                        raise ValueError("saved data failed its hash check")
                    offset += size
        except (ValueError, OSError, TypeError, KeyError) as exc:
            log(f"  [RESUME_RESTART] {name}: {exc}. Restarting from zero.")
            state, offset = {"etag": None, "resource": resource, "chunks": []}, 0
    elif part.exists():
        log(f"  [RESUME_RESTART] {name}: No verified checkpoint. Restarting from zero.")

    received = offset
    if not offset or offset < upload.size:
        headers = {"Accept-Encoding": "identity"}
        if offset:
            headers.update({"Range": f"bytes={offset}-", "If-Range": state["etag"]})
        with request(session, "GET", url, "Download", CDN_HOST.fullmatch,
                     params=params, headers=headers, stream=True, allowed_status=(200, 206, 416)) as response:
            status = response.status_code
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type in {"text/html", "application/xhtml+xml"} and (
                not name.lower().endswith((".html", ".htm", ".xhtml"))
                or not response.headers.get("Content-Disposition", "").lower().startswith("attachment")
            ):
                raise DownloadError(f"[HTTP {status} / UNEXPECTED_RESPONSE] The site sent a web page instead of the requested file.")
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise DownloadError(f"[HTTP {status} / ENCODED_RESPONSE] The site changed how it sent the file. Its bytes cannot be verified.")
            etag = response.headers.get("ETag", "")
            if status == 206:
                expected_range = f"bytes {offset}-{upload.size - 1}/{upload.size}"
                if not offset or response.headers.get("Content-Range") != expected_range or etag != state["etag"]:
                    checkpoint.unlink(missing_ok=True)
                    raise DownloadError("[HTTP 206 / RESUME_RESTART] The server did not confirm the saved file and requested range. The next attempt starts from zero.")
                log(f"  [HTTP 206] Verified {offset:,} saved bytes. Resuming {name}.")
            elif status == 416:
                checkpoint.unlink(missing_ok=True)
                raise DownloadError("[HTTP 416 / RESUME_RESTART] The server refused the saved offset. The next attempt starts from zero.")
            else:
                if offset:
                    log(f"  [HTTP 200 / RESUME_RESTART] The server sent the whole file. Restarting {name} from zero.")
                offset = received = 0
                state = {"etag": etag if re.fullmatch(r'"[^"]*"', etag) else None,
                         "resource": resource, "chunks": []}
                checkpoint.unlink(missing_ok=True)

            length = response.headers.get("Content-Length", "")
            if length.isdecimal() and int(length) != upload.size - offset:
                raise DownloadError(f"[HTTP {status} / WRONG_SIZE] The site reported {int(length):,} bytes. Expected {upload.size - offset:,} bytes.")
            started = time.monotonic()
            last_update = started
            pending = bytearray()
            try:
                with part.open("r+b" if offset else "wb") as output:
                    output.seek(offset)
                    output.truncate()
                    def save_chunk():
                        output.write(pending)
                        if state["etag"]:
                            output.flush()
                            os.fsync(output.fileno())
                            state["chunks"].append((len(pending), hashlib.sha256(pending).hexdigest()))
                            write_json(checkpoint, state)
                        pending.clear()
                    show_progress(received, upload.size, started, offset)
                    try:
                        for block in response.iter_content(chunk_size=128 * 1024):
                            if received + len(block) > upload.size:
                                raise DownloadError(f"[HTTP {status} / WRONG_SIZE] The site sent more data than expected.")
                            pending.extend(block)
                            received += len(block)
                            if len(pending) >= CHUNK_BYTES:
                                # Stream blocks are usually aligned, but short reads need not be.
                                tail = pending[CHUNK_BYTES:]
                                del pending[CHUNK_BYTES:]
                                save_chunk()
                                pending.extend(tail)
                            now = time.monotonic()
                            if now - last_update >= 0.2:
                                show_progress(received, upload.size, started, offset)
                                last_update = now
                    except requests.RequestException as exc:
                        if pending and state["etag"]:
                            save_chunk()
                        raise DownloadError(f"[{type(exc).__name__}] The connection dropped while downloading {name}. Saved bytes will be checked on retry.") from None
                    if pending:
                        save_chunk()
                    if received != upload.size:
                        raise DownloadError(f"[HTTP {status} / INCOMPLETE] Received {received:,} bytes, expected {upload.size:,}.")
                    show_progress(received, upload.size, started, offset)
            finally:
                if sys.stdout.isatty():
                    print()
                if not state["etag"] and received != upload.size:
                    part.unlink(missing_ok=True)
                    log("  [RESUME_UNAVAILABLE] The server gave no strong file identifier. This file must restart.")

    while True:
        path, exists = destination(folder, name, upload.size)
        if exists:
            part.unlink(missing_ok=True)
            checkpoint.unlink(missing_ok=True)
            return path, True
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            publish_file(part, path)
        except FileExistsError:
            continue
        checkpoint.unlink(missing_ok=True)
        return path, False


def try_or_skip(action, label):
    while True:
        for attempt in range(RETRIES + 1):
            try:
                return action()
            except (DownloadError, OSError) as exc:
                log(f"  Failed {label}: {exc}")
            if attempt < RETRIES:
                delay = 5 + random.randint(1, 5)
                log(f"  Automatic retry {attempt + 1}/{RETRIES} in {delay}s.")
                time.sleep(delay)
        while True:
            choice = input("  [R]epeat or [S]kip? ").strip().lower()
            if choice in {"r", "repeat", "s", "skip"}:
                break
        if choice in {"s", "skip"}:
            log(f"  Skipped {label}.")
            return SKIPPED


def defender_executable():
    if os.name != "nt":
        raise DownloadError("Microsoft Defender Antivirus scanning requires Windows.")
    if not ctypes.windll.shell32.IsUserAnAdmin():
        raise DownloadError("Open Windows Terminal as administrator to enable album scans.")

    program_data = os.environ.get("ProgramData")
    if program_data:
        platform = Path(program_data) / "Microsoft" / "Windows Defender" / "Platform"
        for executable in sorted(platform.glob("*/MpCmdRun.exe"), reverse=True):
            if executable.is_file():
                return executable
    for variable in ("ProgramW6432", "ProgramFiles"):
        program_files = os.environ.get(variable)
        if program_files:
            executable = Path(program_files) / "Windows Defender" / "MpCmdRun.exe"
            if executable.is_file():
                return executable
    raise DownloadError("Microsoft Defender Antivirus (MpCmdRun.exe) was not found.")


def defender_scan_path(path):
    # Give Defender an ordinary drive or UNC path where MAX_PATH allows it.
    value = str(path)
    unc_prefix = "\\\\?\\UNC\\"
    if value.startswith(unc_prefix):
        ordinary = "\\\\" + value[len(unc_prefix):]
    elif value.startswith("\\\\?\\"):
        ordinary = value[4:]
    else:
        return value
    return ordinary if len(ordinary.encode("utf-16-le")) < 520 else value


def scan_with_defender(executable, folder, paths):
    if not paths:
        log("  Defender scan | No local files to scan.")
        return True

    log(f"  Defender scan | Starting custom scans for {len(paths)} file(s).")
    follow_up = 0
    for path in paths:
        relative = path.relative_to(folder)
        log(f"  Defender scanning | {relative}")
        try:
            result = subprocess.run(
                [str(executable), "-Scan", "-ScanType", "3", "-File", defender_scan_path(path)],
                cwd=executable.parent,
                check=False,
            )
        except OSError as exc:
            log(f"  Defender could not start | {exc}")
            return False
        if result.returncode:
            follow_up += 1
            log(f"  [DEFENDER EXIT {result.returncode}] Defender could not confirm a clean scan for {relative}. Check Protection history.")

    if follow_up:
        log(
            f"  Defender scan | {follow_up} file(s) require review. "
            "See the scan output and Windows Security > Protection history."
        )
    else:
        log("  Defender scan | Completed. Check Protection history for any remediation.")
    return not follow_up


def run(session, search, folder, scan_album=None, *, interactive=False):
    with collection_lock(folder):
        catalog = Catalog(folder)
        try:
            return run_collection(session, search, catalog, scan_album, interactive)
        finally:
            catalog.db.close()


def run_collection(session, search, catalog, scan_album, interactive):
    folder = catalog.folder
    index_errors = catalog.index()
    catalog.recover_encodes()
    album_urls = try_or_skip(lambda: find_albums(session, search), "search")
    if album_urls is SKIPPED:
        return 1
    log(f"Found {len(album_urls)} album(s).")
    totals = Counter()
    skipped_albums = 0
    scan_failures = 0
    archived = archived_downloads(folder)
    scan_review = set()
    scanned = 0
    for index, album_url in enumerate(album_urls, start=1):
        print()
        log(f"[{index}/{len(album_urls)}] {album_url}")
        album = try_or_skip(lambda: read_album(session, album_url), "album listing")
        if album is SKIPPED:
            skipped_albums += 1
            continue
        title, final_url, items, file_pages = album
        log(f"  {title or album_url} | {len(items)} upload(s)")
        counts = Counter()
        album_paths = {}
        for number, item in enumerate(items, start=1):
            try:
                upload = read_upload(item, final_url, file_pages)
            except DownloadError as exc:
                log(f"  [LISTING_METADATA] [{number}/{len(items)}] Skipped: {exc}")
                counts["skipped"] += 1
                continue
            name = windows_name(unquote_plus(upload.original, errors="replace"))
            log(f"  [{number}/{len(items)}] {name} | {upload.size:,} bytes")
            names = (name, windows_name(upload.original))
            try:
                match = catalog.locate(final_url, upload, names)
                if match is None:
                    match = catalog.match_archived(final_url, upload, names, archived)
                if match:
                    path, digest = match
                    exists = True
                else:
                    result = try_or_skip(
                        lambda: transfer(session, final_url, upload, folder, name), name
                    )
                    if result is SKIPPED:
                        counts["skipped"] += 1
                        continue
                    path, exists = result
                    digest = None
                catalog.bind(final_url, upload, name, path, digest)
            except (OSError, sqlite3.Error) as exc:
                log(f"  [CATALOG_WRITE] Could not record {name}: {exc}")
                counts["skipped"] += 1
                continue
            album_paths[path] = None
            status = "already present" if exists else "downloaded"
            counts[status] += 1
            log(f"  {status.capitalize()} | {path.relative_to(folder)}")
        log(
            f"  Album total | {counts['downloaded']} downloaded, "
            f"{counts['already present']} already present, {counts['skipped']} skipped"
        )
        totals.update(counts)
        if scan_album is not None:
            scanned += len(album_paths)
            if not scan_album(folder, tuple(album_paths)):
                scan_failures += 1
                scan_review.update(album_paths)

    log(
        f"Run total | {totals['downloaded']} downloaded, {totals['already present']} already present, "
        f"{totals['skipped']} uploads skipped, {skipped_albums} albums skipped"
    )
    log(f"Defender | {scanned:,} file scan(s) requested, {scan_failures:,} album(s) requiring review.")
    review_hashes = {catalog.files[path][0] for path in scan_review if path in catalog.files}
    _, dedupe_errors = resolve_duplicates(catalog)
    index_errors += catalog.index()
    disk_errors = disk_summary(folder)
    compression_errors = 0
    if interactive:
        if scan_review:
            log(f"Compression | {len(scan_review):,} file path(s) held back because their album scan needs review.")
        write_summary, compression_errors = compression_workflow(catalog, review_hashes)
        disk_errors += disk_summary(folder)
        if write_summary:
            with tempfile.NamedTemporaryFile(
                dir=folder, prefix=SUMMARY_PREFIX, suffix=".txt",
                mode="w", encoding="utf-8", delete=False,
            ) as summary:
                summary.write("\n".join(SUMMARY_LINES or ()) + "\n")
                summary_path = Path(summary.name)
            log(f"Summary saved | {summary_path.name}")
    return int(bool(totals["skipped"] or skipped_albums or scan_failures or dedupe_errors
                    or index_errors or disk_errors or compression_errors))


FFMPEG_PAGE = "https://ffmpeg.org/download.html"
FFMPEG_WINDOWS_PAGE = "https://www.gyan.dev/ffmpeg/builds/"
FFMPEG_PACKAGE = "Gyan.FFmpeg.Essentials"
JXL_PAGE = "https://github.com/libjxl/libjxl/releases"
MEDIA_TYPES = ("video", "image", "audio")
MIN_SAVING_PERCENT = 10
MEDIA_FORMATS = "mov,matroska,avi,mpeg,mpegts,asf,wav,aiff,au,caf,flac"
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpg", ".mpeg", ".ts", ".mts", ".wmv", ".3gp"}
AUDIO_EXTENSIONS = {".wav", ".aif", ".aiff", ".aifc", ".au", ".caf"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".jpe", ".jfif"}


def choose(prompt, choices):
    while True:
        answer = input(prompt).strip().lower()
        if answer in choices:
            return answer


def compression_tool_path(name):
    path = shutil.which(name)
    if path or os.name != "nt" or name not in {"ffmpeg", "ffprobe"}:
        return path
    # WinGet can add its links to PATH after this terminal has already started.
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        links = Path(local_app_data) / "Microsoft" / "WinGet" / "Links"
        path = shutil.which(name + ".exe", path=str(links))
    return path


def install_ffmpeg():
    winget = shutil.which("winget.exe")
    if not winget:
        log("  WinGet is unavailable. Install or update App Installer, or use the Windows build link under Learn more.")
        return False
    log(f"  WinGet will install {FFMPEG_PACKAGE} for your Windows user.")
    try:
        result = subprocess.run([
            winget, "install", "--id", FFMPEG_PACKAGE, "--exact",
            "--source", "winget", "--scope", "user", "--no-upgrade",
        ])
    except OSError as exc:
        log(f"  Could not start WinGet | {exc}")
        return False
    if result.returncode:
        log(f"  [WinGet exit {result.returncode}] Installation did not complete. See the WinGet output above.")
        return False
    if not all(compression_tool_path(name) for name in ("ffmpeg", "ffprobe")):
        log("  WinGet finished. If the tools are still missing, reopen Windows Terminal to refresh PATH.")
    return True


def verify_compression_tools(tools):
    """Exercise each available media path on a tiny generated sample."""
    with tempfile.TemporaryDirectory(prefix="bunkr-tool-check-") as temporary:
        work = Path(temporary)
        ffmpeg = [tools["ffmpeg"], "-nostdin", "-v", "error"] if tools["ffmpeg"] else []

        def video_test():
            sample = work / "test.mkv"
            media_command([*ffmpeg, "-f", "lavfi", "-i", "testsrc2=s=192x108:r=5",
                           "-frames:v", "5", "-c:v", "libx265", "-preset", "ultrafast",
                           "-x265-params", "pools=1:frame-threads=1", str(sample)], timeout=30)
            if probe(tools, sample)["streams"][0].get("codec_name") != "hevc":
                raise DownloadError("ffprobe did not detect an HEVC video.")
            media_command([*ffmpeg, "-i", str(sample), "-i", str(sample),
                           "-lavfi", "[0:v][1:v]libvmaf", "-f", "null", "-"], timeout=30)

        def audio_test():
            sample = work / "test.flac"
            media_command([*ffmpeg, "-f", "lavfi", "-i", "sine=frequency=440",
                           "-t", "0.1", "-c:a", "flac", str(sample)], timeout=30)
            if probe(tools, sample)["streams"][0].get("codec_name") != "flac":
                raise DownloadError("ffprobe did not detect FLAC audio.")
            media_command([*ffmpeg, "-i", str(sample), "-f", "null", "-"], timeout=30)

        def image_test():
            jpeg = base64.b64decode(
                "/9j/4AAQSkZJRgABAgAAAQABAAD//gAQTGF2YzYxLjE5LjEwMQD/2wBDAAgEBAQEBAUFBQUFBQYGBgYGBgYGBgYHBwcICAgH"
                "BwcGBgcHCAgICAkJCQgICAgJCQoKCgwMCwsODg4RERT/xABKAAEAAAAAAAAAAAAAAAAAAAAAAQEAAAAAAAAAAAAAAAAAAAAA"
                "EAEAAAAAAAAAAAAAAAAAAAAAEQEAAAAAAAAAAAAAAAAAAAAA/8AAEQgAEAAQAwEiAAIRAAMRAP/aAAwDAQACEQMRAD8AAA//2Q=="
            )
            source, encoded, restored = (work / name for name in ("test.jpg", "test.jxl", "restored.jpg"))
            source.write_bytes(jpeg)
            media_command([tools["cjxl"], str(source), str(encoded), "-d", "0", "--lossless_jpeg=1"], timeout=30)
            media_command([tools["djxl"], str(encoded), str(restored)], timeout=30)
            if restored.read_bytes() != jpeg:
                raise DownloadError("The restored JPEG did not match the original bytes.")

        for kind, test in (("video", video_test), ("audio", audio_test), ("image", image_test)):
            if not tools.get(kind):
                continue
            try:
                test()
            except (DownloadError, OSError, KeyError, IndexError, TypeError) as exc:
                tools.pop(kind)
                log(f"  [TOOL_TEST] {kind.title()} unavailable | {exc}")
            else:
                log(f"  Tool test passed | {kind}")


def compression_tools(*, check_only=False):
    log("Compression needs external tools to test quality and measure space savings.")
    log("FFmpeg encodes video and audio. ffprobe checks what's inside each file.")
    log("Choose an FFmpeg build with libx265 and libvmaf for HEVC encoding and video quality checks.")
    log("cjxl and djxl shrink JPEGs and verify that the original JPEG bytes can be restored.")
    log("The image pass currently handles JPEGs. The audio pass handles supported uncompressed PCM files.")
    if os.name == "nt":
        log("Use Install FFmpeg to get ffmpeg and ffprobe through WinGet. JPEG XL tools are installed separately.")
    install_option = " / [I]nstall FFmpeg" if os.name == "nt" else ""
    choices = "ynli" if install_option else "ynl"
    check_now = False
    while True:
        choice = "y" if check_now else choose(
            f"Check for compression tools? [Y]es / [N]o / [L]earn more{install_option}: ", choices
        )
        check_now = False
        if choice == "n":
            return {}
        if choice == "i":
            check_now = install_ffmpeg()
            continue
        if choice == "l":
            log(f"FFmpeg and ffprobe | {FFMPEG_PAGE}")
            log(f"Windows x64 builds | {FFMPEG_WINDOWS_PAGE}")
            log(f"JPEG XL tools (cjxl and djxl) | {JXL_PAGE}")
            log("Extract the tools and add the folders containing the .exe files to your user PATH.")
            log("Use Install FFmpeg for video and audio, or install the tools manually from these pages.")
            if choose("Open these official pages in your default browser? [Y]es / [N]o: ", "yn") == "y":
                for url in (FFMPEG_PAGE, FFMPEG_WINDOWS_PAGE, JXL_PAGE):
                    try:
                        if not webbrowser.open(url, new=2):
                            log(f"Could not open the browser. Copy this address: {url}")
                    except (OSError, webbrowser.Error):
                        log(f"Could not open the browser. Copy this address: {url}")
            continue
        tools = {name: compression_tool_path(name) for name in ("ffmpeg", "ffprobe", "cjxl", "djxl")}
        for name, path in tools.items():
            if path:
                log(f"  Found {name} | {path}")
            else:
                log(f"  Missing {name} | Windows Terminal cannot find it on PATH.")
        if tools["ffmpeg"] and tools["ffprobe"]:
            try:
                encoders = media_command([tools["ffmpeg"], "-hide_banner", "-encoders"]).decode(errors="replace")
                if re.search(r"\bflac\b", encoders):
                    tools["audio"] = True
                else:
                    log("  Missing FFmpeg feature | FLAC encoder (audio).")
                filters = media_command([tools["ffmpeg"], "-hide_banner", "-filters"]).decode(errors="replace")
                missing_video = [
                    feature for feature, listing in (("libx265", encoders), ("libvmaf", filters))
                    if not re.search(rf"\b{feature}\b", listing)
                ]
                if missing_video:
                    log(f"  Missing FFmpeg feature(s) | {', '.join(missing_video)} (video).")
                else:
                    tools["video"] = True
            except (DownloadError, OSError) as exc:
                log(str(exc))
        if tools["cjxl"] and tools["djxl"]:
            tools["image"] = True
        if any(tools.get(kind) for kind in MEDIA_TYPES):
            verify_compression_tools(tools)
        missing = [kind for kind in MEDIA_TYPES if not tools.get(kind)]
        if missing:
            log(f"Unavailable with this installation | {', '.join(missing)}. Use Learn more for the tool links.")
        if check_only or len(missing) < len(MEDIA_TYPES):
            return tools
        log("Choose No to continue to the summary menu, or Learn more for setup instructions.")
        log("If you just installed the tools, start a new Windows Terminal session so it can see the updated PATH.")


def media_command(args, cwd=None, timeout=3600):
    try:
        result = subprocess.run(args, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise DownloadError("[TIMEOUT] The media tool took too long to finish.") from None
    if result.returncode:
        detail = " ".join(result.stderr.decode(errors="replace").splitlines()[-2:])[:300]
        raise DownloadError(f"[EXIT {result.returncode}] {Path(args[0]).name} could not process this file. {detail}")
    return result.stdout


def probe(tools, path):
    output = media_command([tools["ffprobe"], "-v", "error", "-protocol_whitelist", "file", "-format_whitelist", MEDIA_FORMATS, "-show_streams", "-show_format",
                            "-show_chapters", "-of", "json", "-i", defender_scan_path(path)], timeout=45)
    try:
        return json.loads(output)
    except ValueError:
        raise DownloadError("[MEDIA_INFO] The tool could not read the file details.") from None


def media_kind(path):
    suffix = path.suffix.lower()
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in AUDIO_EXTENSIONS:
        return "audio"
    return None


def encode_verified(tools, kind, source, work, benchmark=False):
    original_size = source.stat().st_size
    if kind == "image":
        output = work / "encoded.jxl"
        restored = work / "restored.jpg"
        media_command([tools["cjxl"], defender_scan_path(source), defender_scan_path(output), "-d", "0", "--lossless_jpeg=1"])
        media_command([tools["djxl"], defender_scan_path(output), defender_scan_path(restored)])
        if hash_file(source)[0] != hash_file(restored)[0]:
            raise DownloadError("[IMAGE_VALIDATION] The original JPEG could not be reconstructed exactly.")
    else:
        ffmpeg = [tools["ffmpeg"], "-hide_banner", "-nostdin", "-v", "error", "-xerror"]
        input_options = ["-protocol_whitelist", "file", "-format_whitelist", MEDIA_FORMATS]
        info = probe(tools, source)
        streams = info.get("streams", [])
        if kind == "video":
            videos = [s for s in streams if s.get("codec_type") == "video"]
            if len(videos) != 1 or videos[0].get("disposition", {}).get("attached_pic"):
                return None
            video = videos[0]
            # Leave specialized video and unsupported auxiliary tracks intact.
            if (
                video.get("codec_name") in {"hevc", "av1"}
                or video.get("pix_fmt") != "yuv420p"
                or video.get("color_transfer") in {"smpte2084", "arib-std-b67"}
                or video.get("side_data_list")
                or video.get("field_order") not in (None, "unknown", "progressive")
                or any(s.get("codec_type") not in {"video", "audio", "subtitle"} for s in streams)
            ):
                return None
            output = work / "encoded.mkv"
            options = ["-c", "copy", "-c:V", "libx265", "-preset", "slow", "-crf", "20"]
        else:
            if len(streams) != 1 or info.get("chapters"):
                return None
            audio = streams[0]
            if audio.get("codec_type") != "audio" or audio.get("codec_name") not in {
                "pcm_u8", "pcm_s16le", "pcm_s16be", "pcm_s24le", "pcm_s24be",
            }:
                return None
            output = work / "encoded.flac"
            options = ["-c:a", "flac"]
        if benchmark and kind != "image":
            try:
                duration = float(info["format"]["duration"])
            except (KeyError, ValueError, TypeError):
                return None
            # Benchmark short whole files, including validation, without extrapolating
            # clip container overhead or treating an arbitrary excerpt as a whole file.
            if not 0 < duration <= 30 or original_size > 100 * 1024 * 1024:
                return None
        media_command([*ffmpeg, *input_options, "-i", defender_scan_path(source), "-map", "0", *options, defender_scan_path(output)])
        if output.stat().st_size * 100 > original_size * (100 - MIN_SAVING_PERCENT):
            return None
        encoded = probe(tools, output)
        source_streams = [(s.get("codec_type"), s.get("codec_name")) for s in streams]
        expected = [(kind_, "hevc" if kind_ == "video" else "flac" if kind == "audio" else codec)
                    for kind_, codec in source_streams]
        actual = [(s.get("codec_type"), s.get("codec_name")) for s in encoded.get("streams", [])]
        if actual != expected or len(info.get("chapters", [])) != len(encoded.get("chapters", [])):
            raise DownloadError("[STREAM_VALIDATION] The new file is missing an expected track or chapter.")
        if kind == "video":
            new_video = next(s for s in encoded["streams"] if s.get("codec_type") == "video")
            for field in ("width", "height", "pix_fmt", "sample_aspect_ratio",
                          "color_range", "color_space", "color_transfer", "color_primaries"):
                if new_video.get(field) != video.get(field):
                    raise DownloadError(f"[VIDEO_VALIDATION] The encode changed {field}.")
            try:
                original_duration = float(info["format"]["duration"])
                encoded_duration = float(encoded["format"]["duration"])
                if not 0 < original_duration < float("inf") or not 0 < encoded_duration < float("inf") or abs(encoded_duration - original_duration) > 0.1:
                    raise ValueError
            except (KeyError, ValueError, TypeError):
                raise DownloadError("[DURATION_VALIDATION] The original and new running times could not be matched.") from None
            media_command([*ffmpeg, *input_options, "-i", defender_scan_path(output),
                           "-map", "0:v", "-map", "0:a?", "-f", "null", "-"])
            def video_frame_count(path):
                count = media_command([
                    tools["ffprobe"], "-v", "error", *input_options, "-select_streams", "V:0",
                    "-count_frames", "-show_entries", "stream=nb_read_frames",
                    "-of", "csv=p=0", "-i", defender_scan_path(path),
                ])
                return int(count.strip())

            if video_frame_count(source) != video_frame_count(output):
                raise DownloadError("[FRAME_VALIDATION] The output has a different number of video frames.")
            for index, stream in enumerate(streams):
                if stream["codec_type"] not in {"audio", "subtitle"}:
                    continue

                def track_hash(path):
                    return media_command([
                        *ffmpeg, *input_options, "-i", defender_scan_path(path),
                        "-map", f"0:{index}", "-c", "copy", "-f", "hash", "-hash", "sha256", "-",
                    ])

                if track_hash(source) != track_hash(output):
                    raise DownloadError("[TRACK_VALIDATION] A copied audio or subtitle track changed.")
            compare = (
                "[0:v]settb=AVTB,setpts=PTS-STARTPTS[d];"
                "[1:v]settb=AVTB,setpts=PTS-STARTPTS[r];"
                "[d][r]libvmaf=log_fmt=json:log_path=quality.json"
            )
            media_command([*ffmpeg, *input_options, "-i", defender_scan_path(output), *input_options, "-i", defender_scan_path(source), "-filter_complex",
                           compare, "-an", "-f", "null", "-"], cwd=work)
            scores = json.loads((work / "quality.json").read_text(encoding="utf-8"))
            values = sorted(frame["metrics"]["vmaf"] for frame in scores["frames"])
            if any(not 0 <= value <= 100 for value in values):
                raise DownloadError("[VIDEO_QUALITY] The quality tool returned an invalid score.")
            # Avoid accepting an excellent average with visibly weaker scenes.
            if not values or sum(values) / len(values) < 95 or values[int(len(values) * 0.05)] < 90:
                raise DownloadError("[VIDEO_QUALITY] The encode did not meet the video quality thresholds.")
        else:
            def pcm_digest(path):
                return media_command([*ffmpeg, *input_options, "-i", defender_scan_path(path), "-map", "0:a", "-c:a", "pcm_s32le",
                                      "-f", "hash", "-hash", "sha256", "-"])
            new_audio = encoded["streams"][0]
            if any(audio.get(field) != new_audio.get(field) for field in ("channels", "sample_rate")) or pcm_digest(source) != pcm_digest(output):
                raise DownloadError("[AUDIO_VALIDATION] The new file did not reproduce the original audio samples.")
    return output if output.stat().st_size * 100 <= original_size * (100 - MIN_SAVING_PERCENT) else None


def format_size(size):
    return f"{size / (1024 ** 3):,.2f} GiB" if size >= 1024 ** 3 else f"{size / (1024 ** 2):,.2f} MiB"


def disk_summary(folder):
    counts, sizes = Counter(), Counter()
    errors = 0

    def report(exc):
        nonlocal errors
        errors += 1
        log(f"  [DISK_READ] {exc}")

    for root, dirs, files in os.walk(folder, onerror=report):
        parent = Path(root)
        allowed = []
        for name in dirs:
            try:
                if real_directory(parent / name):
                    allowed.append(name)
            except OSError as exc:
                report(exc)
        dirs[:] = allowed
        for name in files:
            path = parent / name
            try:
                size = file_size(path)
            except OSError as exc:
                report(exc)
                continue
            if size is None or size < 0:
                continue
            first = path.relative_to(folder).parts[0].casefold()
            label = {
                DUPLICATE_FOLDER: "Duplicates pending review",
                RAW_FOLDER: "Processed originals",
                PARTIAL_FOLDER: "Incomplete downloads / checkpoints",
            }.get(first, "Retained files")
            if first.startswith((CATALOG_NAME, SUMMARY_PREFIX)):
                label = "Catalog / summaries"
            elif name.startswith(".bunkr-") and name.endswith(".part"):
                label = "Incomplete downloads / checkpoints"
            counts[label] += 1
            sizes[label] += size
    log(f"On disk | {sum(counts.values()):,} files, {format_size(sum(sizes.values()))} logical bytes{' (partial inventory)' if errors else ''}")
    for label in ("Retained files", "Duplicates pending review", "Processed originals",
                  "Incomplete downloads / checkpoints", "Catalog / summaries"):
        log(f"  {label} | {counts[label]:,} files, {format_size(sizes[label])}")
    return errors


def benchmark_source(tools, kind, source, work):
    if kind == "image":
        return source, 1.0
    info = probe(tools, source)
    try:
        duration = float(info["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        return None, 0.0
    if not 0 < duration < float("inf"):
        return None, 0.0
    if duration <= 30 and source.stat().st_size <= 100 * 1024 * 1024:
        return source, 1.0
    length = min(15.0, duration)
    if kind == "audio":
        # FLAC's benchmark must use the source PCM format, not a requantized copy.
        suffix = source.suffix.lower()
        if suffix not in {".wav", ".aif", ".aiff"}:
            return None, 0.0
        excerpt = work / ("sample" + suffix)
    else:
        excerpt = work / "sample.mkv"
    media_command([
        tools["ffmpeg"], "-hide_banner", "-v", "error", "-xerror", "-nostdin",
        "-protocol_whitelist", "file", "-format_whitelist", MEDIA_FORMATS,
        "-ss", str(max(0.0, (duration - length) / 2)), "-i", defender_scan_path(source),
        "-t", str(length), "-map", "0", "-c", "copy", defender_scan_path(excerpt),
    ], timeout=90)
    clip_duration = float(probe(tools, excerpt)["format"]["duration"])
    return (excerpt, duration / clip_duration) if clip_duration > 0 else (None, 0.0)


def compression_report(catalog, tools, candidates):
    log("Compression estimate | CPU only. x265 and its quality check can heavily load the CPU.")
    log("  Measuring up to six files or excerpts per media type.")
    log("  Moving originals to processed_raws or leaving them alongside outputs retains their disk usage.")
    with tempfile.TemporaryDirectory(prefix=".bunkr-benchmark-", dir=catalog.folder) as temporary:
        for kind in MEDIA_TYPES:
            paths = candidates[kind]
            if not paths or not tools.get(kind):
                log(f"  {kind.title()} | {len(paths):,} candidate(s), no estimate available.")
                continue
            # Spread bounded samples across the file-size distribution.
            ordered = sorted(paths, key=lambda path: catalog.files[path][1][2])
            indexes = sorted({round(i * (len(ordered) - 1) / 5) for i in range(6)})
            sampled = accepted = total = saved = 0
            times = []
            for index in indexes:
                source = ordered[index]
                if kind == "image" and catalog.files[source][1][2] > 100 * 1024 * 1024:
                    continue
                started = time.monotonic()
                try:
                    with tempfile.TemporaryDirectory(dir=temporary) as sample:
                        work = Path(sample)
                        excerpt, time_scale = benchmark_source(tools, kind, source, work)
                        if excerpt is None:
                            continue
                        output = encode_verified(tools, kind, excerpt, work, benchmark=True)
                        source_size = source.stat().st_size
                        if output:
                            accepted += 1
                            saved += source_size * (1 - output.stat().st_size / excerpt.stat().st_size)
                        total += source_size
                        sampled += 1
                        times.append((time.monotonic() - started) * time_scale)
                except (DownloadError, OSError, ValueError, KeyError) as exc:
                    log(f"  Benchmark skipped | {source.name} | {exc}")
            if not accepted or not total:
                log(f"  {kind.title()} | No sample passed both quality and 10% savings checks. Batch time and savings unknown.")
                continue
            size = sum(catalog.files[path][1][2] for path in paths)
            estimate = size * saved / total
            estimate_minutes = len(paths) * sum(times) / len(times) / 60
            log(f"  {kind.title()} | {accepted}/{sampled} sample(s) passed. Rough projection: "
                f"{format_size(estimate)} potential savings if originals are removed, "
                f"~{estimate_minutes:.1f} min based on sampled media and this PC.")
            if sampled != len(paths):
                log("    Full-file results can vary from short excerpts and the sample mix.")


def compression_selection():
    while True:
        log("1 All media   2 Select media types   3 Finish with text summary   4 Finish without summary")
        choice = choose("Choose [1–4]: ", "1234")
        if choice != "2":
            return MEDIA_TYPES if choice == "1" else choice
        log("1 Video   2 Images   3 Audio   4 Video + images   5 Video + audio   6 Images + audio   7 All   0 Back")
        choice = choose("Choose [0–7]: ", "01234567")
        if choice != "0":
            return {
                "1": ("video",), "2": ("image",), "3": ("audio",), "4": ("video", "image"),
                "5": ("video", "audio"), "6": ("image", "audio"), "7": MEDIA_TYPES,
            }[choice]


def compress_files(catalog, tools, candidates, selected, originals):
    results = Counter()
    folder = catalog.folder
    raw_batch = None
    for kind in selected:
        if not tools.get(kind):
            log(f"  {kind.title()} skipped | Required tools are unavailable.")
            continue
        for source in candidates[kind]:
            log(f"  Encoding | {source.relative_to(folder)}")
            original_digest, stamp = catalog.files[source]
            for attempt in range(2):
                try:
                    if fingerprint(source.lstat()) != stamp:
                        raise DownloadError("[SOURCE_CHANGED] The source changed after indexing.")
                    with tempfile.TemporaryDirectory(prefix=".bunkr-encode-", dir=source.parent) as temporary:
                        output = encode_verified(tools, kind, source, Path(temporary))
                        if output is None:
                            results["kept"] += 1
                            log("    Original kept | Ineligible format or less than 10% savings.")
                            break
                        digest, _ = hash_file(output)
                        if fingerprint(source.lstat()) != stamp:
                            raise DownloadError("[SOURCE_CHANGED] The source changed during encoding.")
                        target = source.with_name(source.name + ".compressed" + output.suffix)
                        existing = file_size(target)
                        if existing is not None and hash_file(target)[0] != digest:
                            raise DownloadError("[OUTPUT_EXISTS] A different file already uses the output name.")
                        with catalog.db:
                            catalog.db.execute("INSERT OR REPLACE INTO pending_encodes VALUES (?,?)",
                                               (original_digest, digest))
                        if existing is None:
                            publish_file(output, target)
                        with catalog.db:
                            catalog.remember(target)
                            catalog.db.execute("INSERT OR REPLACE INTO replacements VALUES (?,?)",
                                               (original_digest, digest))
                            catalog.db.execute("DELETE FROM pending_encodes WHERE original=?", (original_digest,))
                    if originals == "1":
                        if raw_batch is None:
                            raw_batch = Path(tempfile.mkdtemp(prefix="run-", dir=managed_directory(folder, RAW_FOLDER)))
                        held = raw_batch / source.relative_to(folder)
                        held.parent.mkdir(parents=True, exist_ok=True)
                        publish_file(source, held)
                    elif originals == "2":
                        if fingerprint(source.lstat()) != stamp or hash_file(target)[0] != digest:
                            raise DownloadError("[DELETE_VALIDATION] The original or its replacement changed. The original was kept.")
                        source.unlink()
                    results["encoded"] += 1
                    log(f"    Saved | {target.relative_to(folder)}")
                    break
                except (DownloadError, OSError, ValueError, KeyError, sqlite3.Error) as exc:
                    log(f"    {exc} | {source.name}")
                    if attempt == 0 and isinstance(exc, OSError) and isinstance(exc, PermissionError):
                        log("    File may be temporarily locked. Retrying once in 2 seconds.")
                        time.sleep(2)
                        continue
                    if attempt == 0 and str(exc).startswith("[TIMEOUT]"):
                        log("    Retrying the media tool once.")
                        continue
                    results["errors"] += 1
                    break
    log(f"Compression | {results['encoded']:,} saved, {results['kept']:,} unchanged, {results['errors']:,} errors.")
    return results["errors"]


def compression_workflow(catalog, review_hashes):
    tools = compression_tools()
    candidates = {kind: [] for kind in MEDIA_TYPES}
    converted = {
        digest for row in catalog.db.execute("SELECT original, encoded FROM replacements")
        if catalog.locations.get(row[1]) for digest in row
    }
    for path, (digest, _) in catalog.files.items():
        kind = media_kind(path)
        if kind and ".compressed." not in path.name and digest not in review_hashes and digest not in converted:
            candidates[kind].append(path)
    if tools:
        compression_report(catalog, tools, candidates)
    else:
        log("Compression estimate | Skipped by request.")
    errors = 0
    while True:
        selected = compression_selection()
        if isinstance(selected, str):
            return selected == "3", errors
        if not any(tools.get(kind) and candidates[kind] for kind in selected):
            log("There are no eligible files with the required tools for that selection.")
            continue
        log("Original files | 1 Move to processed_raws   2 Delete after validation   3 Leave alongside outputs (uses extra space)   0 Back")
        originals = choose("Choose [0–3]: ", "0123")
        if originals == "0":
            continue
        if originals == "2":
            log("Deletion is permanent. Video conversion is lossy even after passing quality checks.")
            if choose("Delete originals after successful validation? [Y]es / [N]o: ", "yn") == "n":
                continue
        errors += compress_files(catalog, tools, candidates, selected, originals)
        return choose("Write a text summary to the collection root? [Y]es / [N]o: ", "yn") == "y", errors


def main():
    global COLOR, SUMMARY_LINES
    COLOR = enable_colors()
    SUMMARY_LINES = []
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check-tools", action="store_true",
                        help="test compression dependencies without searching or downloading")
    if parser.parse_args().check_tools:
        log(f"Python {sys.version.split()[0]} | requests and json5 loaded")
        try:
            scanner = defender_executable()
        except (DownloadError, OSError) as exc:
            log(f"Defender scans unavailable | {exc}")
        else:
            log(f"Defender command found | {scanner} (no scan started)")
        tools = compression_tools(check_only=True)
        ready = [kind for kind in MEDIA_TYPES if tools.get(kind)]
        log(f"Compression ready | {', '.join(ready) if ready else 'none'}")
        return int(len(ready) != len(MEDIA_TYPES))
    search_url = input("Bunkr album search URL: ").strip().strip('"')
    search = parse_search_url(search_url)
    folder_input = input("Download folder: ").strip().strip('"')
    if not folder_input:
        raise DownloadError("Enter a destination folder.")
    folder = Path(os.path.expandvars(folder_input)).expanduser().resolve()
    if os.name == "nt":
        # Use extended-length paths for deeply nested destinations and UNC shares.
        raw = str(folder)
        if not raw.startswith("\\\\?\\"):
            prefix = "\\\\?\\UNC\\" if raw.startswith("\\\\") else "\\\\?\\"
            folder = Path(prefix + (raw[2:] if raw.startswith("\\\\") else raw))
    folder.mkdir(parents=True, exist_ok=True)
    scan_album = None
    try:
        scan_album = partial(scan_with_defender, defender_executable())
    except (DownloadError, OSError) as exc:
        log(f"Defender album scans unavailable | {exc}")
    with requests.Session() as session:
        session.headers["User-Agent"] = USER_AGENT
        result = run(session, search, folder, scan_album, interactive=True)
    return result if scan_album is not None else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nStopped.")
        sys.exit(130)
    except (DownloadError, OSError, sqlite3.Error) as error:
        log(f"Error: {error}")
        sys.exit(1)
