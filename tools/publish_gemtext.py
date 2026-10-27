#!/usr/bin/env python3
"""Publish reconstructed mboxes and XML as a browsable Gemtext archive.

Run from the repository root, using Python 3.10+ and the standard library:
  python3 tools/publish_gemtext.py --output /path/to/archive \
      --root-url gemini://gemi.dev/gemini-mailing-list/

Files are overwritten in place. Cleanup belongs to the Makefile; this script
never removes old pages. It copies artifacts/about.gmi and compresses all.mbox
as entire-archive.mbox.gz in the output root.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
import gzip
from pathlib import Path
import re
import textwrap
from urllib.parse import urlsplit, urlunsplit
import xml.etree.ElementTree as ET


OLD_HOSTS = {"lists.orbitalfox.eu", "list.orbitalfox.eu"}
OLD_PATH = "/archives/gemini/"
HTML_ROOT = "https://mailing-list-archive.gemi.dev/www/"
URL = re.compile(r"\b(?:https?|gemini|gopher|ftps?)://[^\s<>\"'`]+", re.I)
MESSAGE_PATH = re.compile(r"/archives/gemini/\d{4}/(\d{6})\.html")
QUOTE_PREFIX = re.compile(r"^(?:[ \t]*>[ \t]?)+")
MONTHS = ("January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def one_line(value):
    return " ".join(str(value or "").split())


def bare_message_id(value):
    value = str(value or "").strip()
    match = re.search(r"<([^<>]+)>", value)
    return match[1] if match else value


def clean_subject(value):
    # Match Mailman's displayed titles rather than repeating leading Re: tags.
    return re.sub(r"^(?:re\s*:\s*)+", "", one_line(value), flags=re.I) or "(no subject)"


def date_string(value):
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


@dataclass
class Post:
    number: int
    mid: str
    date: datetime
    title: str
    name: str
    address: str
    body: str | None = None

    @property
    def filename(self):
        return f"{self.number:06}.gmi"

    @property
    def author(self):
        value = f"{self.name} ({self.address})" if self.name and self.address else self.name or self.address
        return (value or "Unknown author").replace("@", " (a) ")


@dataclass
class Thread:
    number: int
    posts: list[Post]

    @property
    def filename(self):
        return f"{self.number:06}.gmi"


def read_inventory(directory):
    root = ET.parse(directory / "emails.xml").getroot()
    require(root.tag == "emails", "emails.xml must have an <emails> root")
    posts, identities = {}, set()
    for node in root:
        require(node.tag == "email", "Unexpected element in emails.xml")
        number = int(node.findtext("MessageNumber", ""))
        mid = bare_message_id(node.findtext("MessageID"))
        date = datetime.fromisoformat(node.findtext("Date", "").replace("Z", "+00:00"))
        require(0 <= number <= 999999, f"Invalid message number: {number}")
        require(mid and date.tzinfo is not None, f"Missing identity/timezone: {number}")
        require(number not in posts and mid not in identities, f"Duplicate XML email: {number}")
        posts[number] = Post(number, mid, date.astimezone(timezone.utc),
                             clean_subject(node.findtext("Subject")),
                             one_line(node.findtext("From/Name")), one_line(node.findtext("From/Address")))
        identities.add(mid)
    require(posts, "emails.xml contains no emails")
    return posts


def rewrite_url(value, root_url, posts):
    try:
        parsed = urlsplit(value)
    except ValueError:
        # Protocol discussions include deliberately invalid example URLs.
        return value
    if parsed.hostname not in OLD_HOSTS:
        return value

    match = MESSAGE_PATH.fullmatch(parsed.path)
    if match:
        number = int(match[1])
        require(number in posts, f"Quoted Orbital Fox URL references an unknown message: {value}")
        target = urlsplit(root_url + f"messages/{number:06}.gmi")
    elif parsed.path == OLD_PATH.rstrip("/") or parsed.path.startswith(OLD_PATH):
        # Mailbox downloads, indexes and attachment references stay on HTTPS.
        suffix = "" if parsed.path == OLD_PATH.rstrip("/") else parsed.path[len(OLD_PATH):]
        target = urlsplit(HTML_ROOT + suffix)
    else:
        target = parsed._replace(scheme="https", netloc="mailing-list-archive.gemi.dev")
    return urlunsplit((target.scheme, target.netloc, target.path, parsed.query, parsed.fragment))


def rewrite_body(text, root_url, posts):
    def replace(match):
        # Sentence punctuation surrounding a URL is not part of its destination.
        value = match[0].rstrip(".,;")
        for closing, opening in ((")", "("), ("]", "["), ("}", "{")):
            while value.endswith(closing) and value.count(closing) > value.count(opening):
                value = value[:-1]
        return rewrite_url(value, root_url, posts) + match[0][len(value):]

    # Relative links are never matched or rewritten.
    return URL.sub(replace, text)


def wrap_body(text):
    """Wrap prose like the C# publisher, retaining quotes, code and whole URLs."""
    result, fence = [], None
    for line in text.splitlines():
        quoted = QUOTE_PREFIX.match(line)
        prefix = quoted[0] if quoted else ""
        content = line[len(prefix):]
        delimiter = re.match(r"^[ \t]*(`{3,}|~{3,})", content)
        if delimiter:
            marker = delimiter[1]
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
            rendered = [line]
        elif (fence is not None or len(line) < 90 or len(prefix) >= 75
              or content.startswith((" ", "\t", "=>", "$ "))
              or re.search(r"\t| {2,}", content)):
            # Indented text, ASCII layouts and fenced code keep their layout.
            rendered = [line]
        else:
            rendered = textwrap.wrap(content, width=75, initial_indent=prefix,
                                     subsequent_indent=prefix, expand_tabs=False,
                                     replace_whitespace=False, break_long_words=False,
                                     break_on_hyphens=False) or [line]
        # An email's own fence must not terminate our surrounding literal block.
        result.extend(" " + row if row.startswith("```") else row for row in rendered)
    return "\n".join(result).rstrip("\n")


def mbox_messages(path):
    """Read our canonical mboxrd without opening the mailbox for writing.

    Reconstruction escapes every body line starting with From, so unescaped
    From_ lines are safe separators. It also adds one transport blank line
    after each record; remove that line, retaining the body's own blank lines.
    """
    def finish(lines):
        raw = b"".join(lines)
        newline = b"\r\n" if raw.endswith(b"\r\n") else b"\n"
        require(raw.endswith(newline * 2), "Missing canonical mbox transport blank line")
        return raw[:-len(newline)]

    current = None
    with path.open("rb") as source:
        for line in source:
            if line.startswith(b"From "):
                if current is not None:
                    yield finish(current)
                current = []
            else:
                require(current is not None, "Mbox does not start with a From_ separator")
                current.append(line)
    require(current is not None, "all.mbox is empty")
    yield finish(current)


def read_messages(directory, posts, root_url):
    """Decode canonical plain text and undo exactly one layer of mboxrd quoting."""
    for raw in mbox_messages(directory / "all.mbox"):
        boundary = re.search(rb"\r?\n\r?\n", raw)
        require(boundary is not None, "Missing mbox header/body separator")
        raw = raw[:boundary.end()] + re.sub(rb"^>(>*From )", rb"\1", raw[boundary.end():], flags=re.M)
        message = BytesParser(policy=policy.default).parsebytes(raw)
        number = int(str(message.get("X-Orbital-Fox-Message-Number", "")))
        require(number in posts, f"Mbox email is absent from emails.xml: {number}")
        post = posts[number]
        require(post.body is None, f"Duplicate mbox number: {number}")
        require(bare_message_id(message.get("Message-ID")) == post.mid,
                f"Mbox/XML Message-ID mismatch: {number}")
        date = parsedate_to_datetime(str(message["Date"]))
        date = date.replace(tzinfo=timezone.utc) if date.tzinfo is None else date.astimezone(timezone.utc)
        require(date == post.date, f"Mbox/XML date mismatch: {number}")
        part = message.get_body(preferencelist=("plain",))
        require(part is not None, f"No canonical plain-text body: {number}")
        text = (part.get_payload(decode=True) or b"").decode(part.get_content_charset() or "utf-8")
        post.body = wrap_body(rewrite_body(text, root_url, posts))
    require(all(post.body is not None for post in posts.values()), "Some XML emails are absent from all.mbox")


def read_threads(directory, posts):
    """XML defines thread membership; headers are never used to invent threads."""
    root = ET.parse(directory / "threads.xml").getroot()
    require(root.tag == "threads", "threads.xml must have a <threads> root")
    threads, seen = [], set()
    for number, node in enumerate(root):
        require(node.tag == "thread" and len(node) == 1, "Invalid XML thread root")
        members, stack = [], [node[0]]
        while stack:
            item = stack.pop()
            require(item.tag == "message", "Unexpected element in threads.xml")
            message_number = int(item.get("number", ""))
            require(message_number in posts and message_number not in seen,
                    f"Unknown/duplicate XML thread message: {message_number}")
            members.append(posts[message_number])
            seen.add(message_number)
            stack.extend(reversed(list(item)))
        # Copy the C# display: messages within a discussion appear by sent time.
        threads.append(Thread(number, sorted(members, key=lambda post: (post.date, post.number))))
    require(seen == set(posts), "threads.xml does not contain every email exactly once")
    return threads


def literal_body(post):
    return ["", "```text/plain", post.body, "```"]


def message_page(thread, index):
    post = thread.posts[index]
    total = len(thread.posts)
    lines = [f"# {post.title}", f"* 🗣️ From: {post.author}",
             f"* 📅 Sent: {date_string(post.date)}", f"* 📧 Message {index + 1} of {total}"]
    lines.extend(literal_body(post))
    lines.extend(["", "---"])
    if index:
        previous = thread.posts[index - 1]
        lines.append(f"=> {previous.filename} Previous in thread ({index} of {total}): 🗣️ {previous.author}")
    if index + 1 < total:
        following = thread.posts[index + 1]
        lines.append(f"=> {following.filename} Next in thread ({index + 2} of {total}): 🗣️ {following.author}")
    lines.append(f"=> ../{thread.filename} 🧵 View entire thread.")
    return "\n".join(lines) + "\n"


def thread_page(thread, previous, following):
    first, last = thread.posts[0], thread.posts[-1]
    total = len(thread.posts)
    lines = [f"# {first.title}"]
    if total > 1:
        authors = len({(post.name, post.address) for post in thread.posts})
        lines.extend([f"* 📧 Messages: {total}", f"* 🗣️ Authors: {authors}",
                      f"* 📅 First Message: {date_string(first.date)}",
                      f"* 📅 Last Message: {date_string(last.date)}"])
    title = first.title
    for index, post in enumerate(thread.posts):
        lines.append(f"## {index + 1}. {post.author}")
        if title != post.title:
            lines.append(f"* Subject Changed! New Subject: {post.title}")
        lines.extend([f"* 📅 Sent: {date_string(post.date)}", f"* 📧 Message {index + 1} of {total}"])
        lines.extend(literal_body(post))
        lines.append(f"=> messages/{post.filename} Link to individual message.")
        title = post.title
    lines.append("---")
    if previous is not None:
        lines.append(f"=> {previous.filename} 🧵 Previous Thread: {previous.posts[0].title}")
    if following is not None:
        lines.append(f"=> {following.filename} 🧵 Next Thread: {following.posts[0].title}")
    return "\n".join(lines) + "\n"


def index_page(threads):
    lines = ["# Gemini Mailing List Archives", "=> about.gmi About this archive"]
    previous = None
    for thread in threads:
        first = thread.posts[0]
        current = (first.date.year, first.date.month)
        if previous is None or previous[0] != current[0]:
            lines.extend(["", f"## {current[0]}"])
        if previous != current:
            lines.append(f"### {MONTHS[current[1] - 1]} {current[0]}")
        authors = len({(post.name, post.address) for post in thread.posts})
        lines.append(f"=> {thread.filename} {first.title} • 📧 {len(thread.posts)} • 🗣️ {authors}")
        previous = current
    return "\n".join(lines) + "\n"


def publish(directory, output, threads, about):
    output.mkdir(parents=True, exist_ok=True)
    messages = output / "messages"
    messages.mkdir(exist_ok=True)
    (output / "about.gmi").write_bytes(about)
    # Compress the exact reconstructed mailbox, with no changing gzip timestamp.
    (output / "entire-archive.mbox.gz").write_bytes(
        gzip.compress((directory / "all.mbox").read_bytes(), mtime=0))
    for index, thread in enumerate(threads):
        previous = threads[index - 1] if index else None
        following = threads[index + 1] if index + 1 < len(threads) else None
        (output / thread.filename).write_text(thread_page(thread, previous, following), encoding="utf-8", newline="\n")
        for position, post in enumerate(thread.posts):
            (messages / post.filename).write_text(message_page(thread, position), encoding="utf-8", newline="\n")
    (output / "index.gmi").write_text(index_page(threads),
                                     encoding="utf-8", newline="\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--input", type=Path, default=Path("mbox"), help="Reconstructed mboxes and XML; relative to repo")
    parser.add_argument("--output", type=Path, required=True, help="Gemtext directory; relative to repo")
    parser.add_argument("--root-url", required=True, help="Public gemini:// URL of the archive root")
    parser.add_argument("--check", action="store_true", help="Validate inputs and arguments without writing files")
    args = parser.parse_args()
    repo = args.repo.resolve()
    directory = (args.input if args.input.is_absolute() else repo / args.input).resolve()
    output = (args.output if args.output.is_absolute() else repo / args.output).resolve()
    root = urlsplit(args.root_url)
    require(root.scheme == "gemini" and root.hostname and not root.query and not root.fragment
            and root.username is None and root.password is None, "Root URL must be an absolute gemini:// URL without query/fragment")
    root_url = urlunsplit((root.scheme, root.netloc, root.path.rstrip("/") + "/", "", ""))
    require(not repo.is_relative_to(output), "Output cannot be the repository or a parent of it")
    require(not directory.is_relative_to(output) and not output.is_relative_to(directory),
            "Input and output directories must be separate")
    require(not output.exists() or output.is_dir(), f"Output is not a directory: {output}")
    require(not (output / "messages").exists() or (output / "messages").is_dir(), "messages/ is not a directory")
    # Load the checked-in page during validation, before Makefile cleanup runs.
    about = (repo / "artifacts/about.gmi").read_bytes()
    posts = read_inventory(directory)
    read_messages(directory, posts, root_url)
    threads = read_threads(directory, posts)
    if args.check:
        print(f"Validated {len(posts):,} messages and {len(threads):,} threads; no files written")
    else:
        publish(directory, output, threads, about)
        print(f"Published {len(posts):,} messages and {len(threads):,} threads to {output}")


if __name__ == "__main__":
    main()
