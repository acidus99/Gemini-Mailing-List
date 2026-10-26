#!/usr/bin/env python3
"""Publish the reconstructed Gemini mailing list as a static HTML archive.

Run: python3 tools/publish_html.py
Requires Python 3.10+ and the standard library. Reads only the reconstructed
mbox directory and its emails.xml / threads.xml. Replaces www/ after validation.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html import escape
from html.parser import HTMLParser
import mailbox
import mimetypes
from pathlib import Path, PurePosixPath
import re
import tempfile
from urllib.parse import unquote, urlsplit, urlunsplit
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo


ABOUT_URL = "https://github.com/acidus99/Gemini-Mailing-List"
MBOX_URL = ABOUT_URL + "/blob/main/mbox/"
PUBLIC_URL = "https://mailing-list-archive.gemi.dev/www/"
OLD_HOSTS = {"lists.orbitalfox.eu", "list.orbitalfox.eu"}
OLD_PATH = "/archives/gemini/"
VIEWS = ("thread", "subject", "author", "date")
DISPLAY_ZONE = ZoneInfo("Europe/London")  # Match the original GMT/BST display.
URL = re.compile(r"\b(?:https?|gemini|gopher|ftps?)://[^\s<>\"']+", re.I)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def one_line(value):
    return " ".join(str(value or "").split())


def bare_message_id(value):
    text = str(value or "").strip()
    match = re.search(r"<([^<>]+)>", text)
    return match[1] if match else text


def subject(value):
    # Pipermail omitted leading Re: prefixes from its displayed subjects.
    return re.sub(r"^(?:re\s*:\s*)+", "", one_line(value), flags=re.I) or "(no subject)"


def date_string(value):
    value = value.astimezone(DISPLAY_ZONE)
    weekday = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[value.weekday()]
    month = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")[value.month - 1]
    return f"{weekday} {month} {value.day:2} {value:%H:%M:%S} {value.tzname()} {value.year}"


@dataclass
class Post:
    number: int
    year: str
    mid: str
    date: datetime
    title: str
    name: str
    address: str
    message: object = None

    @property
    def filename(self):
        return f"{self.number:06}.html"

    @property
    def masked_address(self):
        return self.address.replace("@", " at ", 1)

    @property
    def author(self):
        return (self.name or self.address or "Unknown author").replace("@", " at ", 1)


def read_inventory(directory):
    root = ET.parse(directory / "emails.xml").getroot()
    require(root.tag == "emails", "emails.xml must have an <emails> root")
    posts, identities = {}, set()
    for node in root.findall("email"):
        number = int(node.findtext("MessageNumber", ""))
        year = node.findtext("ArchiveYear", "")
        mid = bare_message_id(node.findtext("MessageID"))
        date = datetime.fromisoformat(node.findtext("Date", "").replace("Z", "+00:00"))
        require(number >= 0 and re.fullmatch(r"\d{4}", year), "Invalid XML message number/year")
        require(mid and date.tzinfo is not None, f"Missing XML identity/timezone: {number}")
        require(number not in posts and mid not in identities, f"Duplicate XML email: {number}")
        posts[number] = Post(number, year, mid, date.astimezone(timezone.utc),
                             subject(node.findtext("Subject")),
                             one_line(node.findtext("From/Name")), one_line(node.findtext("From/Address")))
        identities.add(mid)
    require(posts, "emails.xml contains no emails")
    return posts


def read_messages(directory, posts):
    """Read mbox independently; undo exactly one layer of mboxrd quoting."""
    box = mailbox.mbox(directory / "all.mbox", create=False)
    try:
        for key in box.iterkeys():
            raw = box.get_bytes(key)
            boundary = re.search(rb"\r?\n\r?\n", raw)
            require(boundary is not None, "Missing mbox header/body separator")
            raw = raw[:boundary.end()] + re.sub(rb"^>(>*From )", rb"\1", raw[boundary.end():], flags=re.M)
            message = BytesParser(policy=policy.default).parsebytes(raw)
            number = int(str(message.get("X-Orbital-Fox-Message-Number", "")))
            require(number in posts, f"Mbox email is absent from emails.xml: {number}")
            post = posts[number]
            require(post.message is None, f"Duplicate mbox number: {number}")
            require(bare_message_id(message.get("Message-ID")) == post.mid,
                    f"Mbox/XML Message-ID mismatch: {number}")
            date = parsedate_to_datetime(str(message["Date"]))
            date = date.replace(tzinfo=timezone.utc) if date.tzinfo is None else date.astimezone(timezone.utc)
            require(date == post.date, f"Mbox/XML date mismatch: {number}")
            require(message.get_body(preferencelist=("plain",)) is not None,
                    f"No canonical plain-text body: {number}")
            post.message = message
    finally:
        box.close()
    require(all(post.message is not None for post in posts.values()), "Some XML emails are absent from all.mbox")


def read_threads(directory, posts):
    """Use only XML order/relationships; never infer new threads from headers."""
    root = ET.parse(directory / "threads.xml").getroot()
    require(root.tag == "threads", "threads.xml must have a <threads> root")
    rows, seen = defaultdict(list), set()
    for thread in root:
        require(thread.tag == "thread" and len(thread) == 1, "Invalid XML thread root")
        stack = [(thread[0], 0, None)]
        while stack:
            node, depth, year = stack.pop()
            require(node.tag == "message", "Unexpected element in threads.xml")
            number = int(node.get("number", ""))
            require(number in posts and number not in seen, f"Unknown/duplicate XML thread message: {number}")
            post = posts[number]
            require(year is None or year == post.year, f"Thread crosses archive years: {number}")
            rows[post.year].append((post, depth))
            seen.add(number)
            stack.extend((child, depth + 1, post.year) for child in reversed(list(node)))
    require(seen == set(posts), "threads.xml does not contain every email exactly once")
    return rows


def original_attachment_path(location):
    """Retain a historical attachment path only when MIME metadata supplies it."""
    try:
        parsed = urlsplit(str(location or "").strip())
    except ValueError:
        return None
    if parsed.hostname not in OLD_HOSTS or not parsed.path.startswith(OLD_PATH):
        return None
    path = unquote(parsed.path[len(OLD_PATH):])
    parts = path.split("/")
    if (len(parts) < 2 or parts[0] != "attachments"
            or any(part in ("", ".", "..") or "\\" in part or "\x00" in part for part in parts)):
        return None
    return PurePosixPath(*parts)


def write_attachments(stage, post):
    """Save available files for existing body links; do not add attachment links."""
    for index, part in enumerate(post.message.iter_attachments(), 1):
        name = part.get_filename() or ("attachment" + (mimetypes.guess_extension(part.get_content_type()) or ".bin"))
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", name.replace("\\", "/").split("/")[-1])
        safe_name = safe_name.strip(".") or "attachment.bin"
        relative = original_attachment_path(part.get("Content-Location"))
        if relative is None:
            relative = PurePosixPath("attachments", post.year, f"{post.number:06}", f"{index:02}-{safe_name}")
        if part.get_content_maintype() == "message" and isinstance(part.get_payload(), list):
            data = b"\n".join(child.as_bytes(policy=policy.default.clone(linesep="\n")) for child in part.get_payload())
        else:
            data = part.get_payload(decode=True)
        require(isinstance(data, bytes), f"Cannot decode attachment: {post.number}:{name}")
        path = stage / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        require(not path.exists() or path.read_bytes() == data, f"Conflicting attachment contents: {relative}")
        path.write_bytes(data)


def rewrite_url(value):
    parsed = urlsplit(value)
    if parsed.hostname in OLD_HOSTS and (parsed.path == OLD_PATH.rstrip("/") or parsed.path.startswith(OLD_PATH)):
        target = urlsplit(PUBLIC_URL)
        suffix = "" if parsed.path == OLD_PATH.rstrip("/") else parsed.path[len(OLD_PATH):]
        return urlunsplit((target.scheme, target.netloc,
                           target.path + suffix, parsed.query, parsed.fragment))
    return value


def linked_text(text):
    """Escape email text before adding links. No email address becomes mailto:."""
    result, position = [], 0
    for match in URL.finditer(text):
        value = match[0].rstrip(".,;")
        for closing, opening in ((")", "("), ("]", "["), ("}", "{")):
            while value.endswith(closing) and value.count(closing) > value.count(opening):
                value = value[:-1]
        result.append(escape(text[position:match.start()]))
        try:
            target = rewrite_url(value)
            # Accessing hostname also validates bracketed IP literals.
            valid = bool(urlsplit(target).hostname)
        except ValueError:
            valid = False
        if valid:
            result.append(f'<a href="{escape(target, quote=True)}">{escape(target)}</a>')
        else:
            result.append(escape(value))
        position = match.start() + len(value)
    result.append(escape(text[position:]))
    return "".join(result)


def message_body(post):
    part = post.message.get_body(preferencelist=("plain",))
    text = (part.get_payload(decode=True) or b"").decode(part.get_content_charset() or "utf-8")
    lines = []
    for line in text.splitlines(keepends=True):
        quoted = re.match(r"^[ \t]*>", line)
        if quoted:
            # Mailman left the first > upright and italicized the text after it.
            rendered = escape(line[:quoted.end()]) + "<i>" + linked_text(line[quoted.end():]) + "</i>"
        else:
            rendered = linked_text(line)
        lines.append(rendered)
    return "".join(lines)


def document(title, content, head=""):
    return (f'<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f'<title>{escape(title)}</title>\n'
            '<style>body { background: white; color: black; } '
            'body > h1:first-child { margin-top: 0; } pre { white-space: pre-wrap; }</style>\n'
            f'{head}</head>\n<body>\n{content}\n</body>\n</html>\n')


def about():
    return f'<a href="{ABOUT_URL}">About this archive</a>'


def view_links(anchor="start", current=None, views=VIEWS):
    return " ".join(f'<a href="{view}.html#{anchor}">[ {view} ]</a>' for view in views if view != current)


def index_navigation(current):
    return (f'<ul>\n<li><b>Messages sorted by:</b> {view_links(current=current)}</li>\n'
            f'<li><b><a href="../index.html">Archive home</a> | {about()}</b></li>\n</ul>')


def message_navigation(post, previous, following):
    lines = ["<ul>"]
    for label, other in (("Previous", previous), ("Next", following)):
        if other is not None:
            lines.append(f'<li>{label} message (by thread): '
                         f'<a href="{other.filename}">{escape(other.title)}</a></li>')
    links = view_links(str(post.number), views=("date", "thread", "subject", "author"))
    lines += [f'<li><b>Messages sorted by:</b> {links}</li>', "</ul>"]
    return "\n".join(lines)


def list_html(rows):
    """Keep full XML traversal order, but cap visible reply indentation at 3."""
    lines, previous = ["<ul>"], 0
    for index, (post, actual_depth) in enumerate(rows):
        depth = min(actual_depth, 3)
        if index:
            if depth > previous:
                require(depth == previous + 1, "Invalid thread display depth")
                lines.append("<ul>")
            else:
                lines.append("</li>")
                lines.extend("</ul></li>" for _ in range(previous - depth))
        else:
            require(depth == 0, "Thread display must start at a root")
        lines.append(f'<li id="{post.number}"><a href="{post.filename}">{escape(post.title)}</a> '
                     f'<i>{escape(post.author)}</i>')
        previous = depth
    if rows:
        lines.append("</li>")
        lines.extend("</ul></li>" for _ in range(previous))
    lines.append("</ul>")
    return "\n".join(lines)


def year_page(year, view, rows, posts):
    beginning, ending = min(p.date for p in posts), max(p.date for p in posts)
    navigation = index_navigation(view)
    content = (f'<h1 id="start">{year} Archives by {view}</h1>\n{navigation}\n'
               f'<p><b>Starting:</b> <i>{date_string(beginning)}</i><br>\n'
               f'<b>Ending:</b> <i>{date_string(ending)}</i><br>\n'
               f'<b>Messages:</b> {len(posts)}</p>\n{list_html(rows)}\n'
               f'<p id="end"><b>Last message date:</b> <i>{date_string(ending)}</i></p>\n{navigation}')
    return document(f"The Gemini {year} Archive by {view}", content)


def post_page(post, previous, following):
    navigation = message_navigation(post, previous, following)
    head = '<link rel="index" href="index.html">\n'
    for rel, other in (("prev", previous), ("next", following)):
        if other is not None:
            head += f'<link rel="{rel}" href="{other.filename}">\n'
    content = (f'<h1>{escape(post.title)}</h1>\n<b>{escape(post.author)}</b> '
               f'{escape(post.masked_address)}<br>\n<i>{date_string(post.date)}</i>\n'
               f'{navigation}\n<hr>\n<!--beginarticle-->\n<pre>{message_body(post)}</pre>\n'
               f'<!--endarticle-->\n<hr>\n{navigation}\n<hr>\n'
               f'<a href="../index.html">Archive home</a> | {about()}')
    return document(post.title, content, head)


def archive_page(years):
    rows = []
    for year in reversed(years):
        links = " ".join(f'<a href="{year}/{view}.html">[ {view.title()} ]</a>' for view in VIEWS)
        rows.append(f'<tr><td>{year}:</td><td>{links}</td>'
                    f'<td><a href="{MBOX_URL}{year}.mbox">[ Mbox ]</a></td></tr>')
    content = ('<h1>The Gemini Archives</h1>\n'
               f'<p>{about()}</p>\n<table border="3">\n'
               '<tr><th>Archive</th><th>View by:</th><th>Downloadable version</th></tr>\n'
               + "\n".join(rows) + f'\n</table>\n<p><a href="{MBOX_URL}all.mbox">All messages (mbox)</a></p>')
    return document("The Gemini Archives", content)


class PageLinks(HTMLParser):
    """Check generated navigation separately from URLs quoted inside emails."""
    def __init__(self):
        super().__init__()
        self.anchors, self.links, self.pre = set(), [], 0

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        self.pre += tag == "pre"
        for name in ("id", "name"):
            if attrs.get(name):
                self.anchors.add(attrs[name])
        if tag in ("a", "link") and attrs.get("href"):
            href = attrs["href"]
            require(not href.lower().startswith("mailto:"), "Unexpected mailto hyperlink")
            parsed = urlsplit(href)
            if parsed.scheme or parsed.netloc:
                require(self.pre or href == ABOUT_URL or href.startswith(MBOX_URL),
                        f"Unexpected absolute navigation URL: {href}")
            else:
                self.links.append(parsed)

    def handle_endtag(self, tag):
        if tag == "pre":
            self.pre -= 1


def validate_site(stage, page_paths, posts):
    generated = {path.relative_to(stage).as_posix() for path in page_paths if path.stem.isdigit()}
    expected = {f"{post.year}/{post.filename}" for post in posts.values()}
    require(generated == expected, "Generated message pages disagree with emails.xml")
    pages = {}
    for path in page_paths:
        parser = PageLinks()
        parser.feed(path.read_text(encoding="utf-8"))
        parser.close()
        pages[path.resolve()] = parser
    for path, page in pages.items():
        for link in page.links:
            target = (path.parent / unquote(link.path)).resolve() if link.path else path
            require(target.is_relative_to(stage.resolve()) and target.is_file(),
                    f"Broken local link on {path.name}: {link.geturl()}")
            if link.fragment and target in pages:
                require(unquote(link.fragment) in pages[target].anchors,
                        f"Broken anchor on {path.name}: {link.geturl()}")


def publish(directory, output):
    posts = read_inventory(directory)
    read_messages(directory, posts)
    threads = read_threads(directory, posts)
    years = sorted({post.year for post in posts.values()})
    annual = {year: (directory / f"{year}.mbox").read_bytes() for year in years}
    combined = (directory / "all.mbox").read_bytes()
    require(b"".join(annual[year] for year in years) == combined, "Yearly mboxes disagree with all.mbox")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".publish-html-", dir=output.parent) as temporary:
        work = Path(temporary)
        stage = work / "www"
        stage.mkdir()
        pages = []

        def write_page(relative, content):
            path = stage / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            pages.append(path)

        for year in years:
            thread_rows = threads[year]
            year_posts = [post for post, _ in thread_rows]
            ordered = {
                "date": sorted(year_posts, key=lambda p: (p.date, p.number)),
                "subject": sorted(year_posts, key=lambda p: (p.title.casefold(), p.date, p.number)),
                "author": sorted(year_posts, key=lambda p: (p.author.split()[-1].casefold(),
                                                            p.author.casefold(), p.date, p.number)),
            }
            for view in VIEWS:
                rows = thread_rows if view == "thread" else [(post, 0) for post in ordered[view]]
                write_page(f"{year}/{view}.html", year_page(year, view, rows, year_posts))
            write_page(f"{year}/index.html", year_page(year, "thread", thread_rows, year_posts))
            for index, post in enumerate(year_posts):
                write_attachments(stage, post)
                previous = year_posts[index - 1] if index else None
                following = year_posts[index + 1] if index + 1 < len(year_posts) else None
                write_page(f"{year}/{post.filename}", post_page(post, previous, following))
        # Link to the canonical mboxes on GitHub instead of copying downloads.
        write_page("index.html", archive_page(years))
        validate_site(stage, pages, posts)

        # Replace the entire generated site so stale duplicate-number pages
        # and old captured files cannot remain after a successful build.
        backup = work / "previous-www"
        existed = output.exists()
        if existed:
            require(output.is_dir(), f"Output is not a directory: {output}")
            output.rename(backup)
        try:
            stage.rename(output)
        except Exception:
            if existed:
                backup.rename(output)
            raise
    print(f"Published {len(posts):,} messages and all four views to {output}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--input", type=Path, default=Path("mbox"), help="Reconstructed mboxes and XML; relative to repo")
    parser.add_argument("--output", type=Path, default=Path("www"), help="Generated site directory; relative to repo")
    args = parser.parse_args()
    repo = args.repo.resolve()
    directory = (args.input if args.input.is_absolute() else repo / args.input).resolve()
    output = (args.output if args.output.is_absolute() else repo / args.output).resolve()
    require(not repo.is_relative_to(output), "Output cannot be the repository or a parent of it")
    require(not directory.is_relative_to(output) and not output.is_relative_to(directory),
            "Input and output directories must be separate")
    publish(directory, output)


if __name__ == "__main__":
    main()
