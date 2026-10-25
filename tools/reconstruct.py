#!/usr/bin/env python3
"""Reconstruct Orbital Fox numbering and threads using only sources/ inputs.

Run from any directory: python3 /path/to/repository/tools/reconstruct.py
Uses Python 3.10+ and its standard library. No network access is needed.
Writes only 2019.mbox, 2020.mbox, 2021.mbox, all.mbox, emails.xml and threads.xml.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import policy
from email.header import decode_header
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
import gzip
import hashlib
from html import unescape
from html.parser import HTMLParser
import mimetypes
from pathlib import Path
import re
import tempfile
from urllib.parse import parse_qs, unquote, urljoin, urlsplit
import xml.etree.ElementTree as ET


CORE_HEADERS = ("from", "date", "subject", "in-reply-to", "references", "message-id")
ARCHIVE_YEARS = (2019, 2020, 2021)
OUTPUT_FILES = tuple(f"{year}.mbox" for year in ARCHIVE_YEARS) + ("all.mbox", "emails.xml", "threads.xml")

# Known input conventions belong in this standalone script. Additional mailbox
# inputs are discovered automatically and default to mboxrd. Supplemental
# exports are used when present; the source evidence checks still apply.
SOURCE_RULES = {
    "2019.mbox": {"format": "mailman", "sequence_year": 2019},
    "2020.mbox": {"format": "mailman", "sequence_year": 2020},
    "2021.mbox": {"format": "mailman", "sequence_year": 2021},
    "2021-nytpu.mbox": {"format": "mboxrd"},
    # Original source: gemini://rawtext.club/~nervuri/gemini-archive/mailing-list/2021_incomplete.txt.gz
    "2021-nervuri.mbox": {"format": "mailman", "sequence_year": 2021},
    # Original source: gemini://rawtext.club/~nervuri/gemini-archive/mailing-list/adnano_complete-archive.mbox.gz
    "adnano-complete.mbox": {"format": "mailman"},
}
# This is a direct personal reply, absent from the original list archive.
EXCLUDED_MESSAGE_IDS = {
    "82y26rj40c.fsf@ceres": "Personal reply to nytpu, not a list message."
}
PRIORITY = {"html-message-id": 0, "original-mbox-order": 1,
            "html-index-metadata": 2, "anchored-chronological-tail": 3}
SEPARATOR = re.compile(rb"^From [^\r\n]*\r?\n", re.M)
HEADER_END = re.compile(rb"\r?\n\r?\n")


# Body normalization and attachment preservation.
# Only generated MIME headers are folded; 8bit body text is never rewrapped.
MIME_POLICY = policy.default.clone(linesep="\n", max_line_length=76)
SIGNATURE_TYPES = {"application/pgp-signature", "application/pkcs7-signature",
                   "application/x-pkcs7-signature"}
HTML_NOTICE = re.compile(r"(?:-+\s*next part\s*-+\s*)?An HTML attachment was scrubbed"
                         r"\.\.\.\s*URL:\s*(?:<[^>]+>|\S+)\s*")


def decode_text(part):
    """Decode strictly; never replace unreadable bytes silently."""
    data = part.get_payload(decode=True) or b""
    declared = part.get_content_charset()
    charsets = [declared] if declared else ["utf-8", "iso-8859-1"]
    if declared and declared.lower() in ("ascii", "us-ascii"):
        charsets.append("utf-8")  # Some exports omit the true UTF-8 declaration.
    for charset in charsets:
        try:
            return data.decode(charset), charset
        except (LookupError, UnicodeDecodeError):
            pass
    raise ValueError(f"Cannot decode text declared as {declared!r}")


def unflow(text, delete_space=False):
    """RFC 3676: quote depth, space stuffing, delsp and '-- ' separators.

    This is called ONLY for explicitly flowed plain text. Fixed text is not
    guessed at or rewrapped. A quote-depth change ends a paragraph even when
    the sender incorrectly left a soft-break marker before it.
    """
    result, pending, depth, soft = [], None, 0, False
    for line in text.splitlines(keepends=True):
        ending = "\n" if line.endswith("\n") else ""
        line = line[:-1] if ending else line
        quote = len(line) - len(line.lstrip(">"))
        content = line[quote:]
        if content.startswith(" "):
            content = content[1:]
        signature = line == "-- " or content == "-- "
        flowed = content.endswith(" ") and not signature
        if flowed and delete_space:
            content = content[:-1]
        if pending is not None and soft and depth == quote and not signature:
            pending += content
        else:
            if pending is not None:
                result.append((">" * depth + (" " if pending else "") if depth else "") + pending + "\n")
            pending, depth = content, quote
        soft = flowed
        if not soft:
            result.append((">" * depth + (" " if pending else "") if depth else "") + pending + ending)
            pending = None
    if pending is not None:
        result.append((">" * depth + (" " if pending else "") if depth else "") + pending
                      + ("\n" if text.endswith("\n") else ""))
    return "".join(result)


class HTMLText(HTMLParser):
    """Small text converter: paragraphs, links, quotes and preformatted code.

    No scripts execute and no URLs are fetched. Keep link destinations in the
    text so conversion does not lose links with descriptive labels.
    """
    def __init__(self, base=""):
        super().__init__(convert_charrefs=True)
        self.base, self.lines, self.line = base, [], ""
        self.hidden, self.pre, self.quote, self.links = 0, 0, 0, []

    def flush(self, blank=False):
        if self.line:
            value = self.line if self.pre else self.line.strip(" ")
            self.lines.append(">" * self.quote + (" " if value and self.quote else "") + value)
            self.line = ""
        if blank and (not self.lines or self.lines[-1] != ""):
            self.lines.append("")

    def emit(self, value):
        if self.pre:
            chunks = value.split("\n")
            for i, chunk in enumerate(chunks):
                self.line += chunk
                if i < len(chunks) - 1:
                    if not self.line:
                        self.lines.append(">" * self.quote)
                    self.flush()
        else:
            value = re.sub(r"[\t\r\n\f ]+", " ", value.replace("\xa0", " "))
            self.line += value if self.line else value.lstrip(" ")

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("head", "script", "style", "template"):
            self.hidden += 1
        if self.hidden:
            return
        if tag in ("p", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote"):
            self.flush(blank=True)
        elif tag in ("div", "li", "tr", "br"):
            had_line = bool(self.line)
            self.flush()
            if tag == "br" and not had_line:
                self.lines.append(">" * self.quote)
        if tag == "pre":
            self.pre += 1
        elif tag == "blockquote":
            self.quote += 1
        elif tag == "li":
            self.emit("* ")
        elif tag in ("td", "th") and self.line:
            self.emit(" | ")
        elif tag == "a":
            self.links.append((urljoin(self.base, attrs.get("href", "")), []))
        elif tag == "img":
            self.emit(attrs.get("alt", ""))

    def handle_endtag(self, tag):
        if tag in ("head", "script", "style", "template"):
            self.hidden = max(0, self.hidden - 1)
            return
        if self.hidden:
            return
        if tag == "a" and self.links:
            href, label = self.links.pop()
            label = "".join(label).strip()
            if href and href not in (label, "mailto:" + label):
                self.emit(" <" + href + ">")
        if tag in ("p", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote"):
            self.flush(blank=True)
        elif tag in ("div", "li", "tr"):
            self.flush()
        if tag == "pre":
            self.pre = max(0, self.pre - 1)
        elif tag == "blockquote":
            self.quote = max(0, self.quote - 1)

    def handle_data(self, value):
        if not self.hidden:
            self.emit(value)
            for _, label in self.links:
                label.append(value)

    def text(self):
        self.flush()
        # Trim converter-created outer blank lines, not preformatted spaces.
        while self.lines and not self.lines[0]:
            self.lines.pop(0)
        while self.lines and not self.lines[-1]:
            self.lines.pop()
        return "\n".join(self.lines) + ("\n" if self.lines else "")


def html_to_text(value, base=""):
    parser = HTMLText(base)
    parser.feed(value)
    parser.close()
    text = parser.text()
    # Mailman sometimes saved escaped HTML inside a <tt>/<pre> wrapper.
    if re.match(r"\s*<(?:tt|pre)>\s*&lt;(?:html|!doctype)\b", value, re.I):
        parser = HTMLText(base)
        parser.feed(text.replace("\xa0", " "))
        parser.close()
        text = parser.text()
    return text


def captured_attachments(text, repo):
    """Locate saved Mailman attachment URLs, staying inside the source tree."""
    root = (repo / "sources/lists.orbitalfox.eu/archives/gemini/attachments").resolve()
    found = []
    for url in dict.fromkeys(re.findall(r"(?im)^[> \t]*URL:\s*<?(https?://[^\s>]+)", text)):
        if not url.startswith(("https://lists.orbitalfox.eu/archives/gemini/attachments/",
                               "http://lists.orbitalfox.eu/archives/gemini/attachments/")):
            continue  # Other links can deliberately demonstrate invalid URLs.
        parsed = urlsplit(url)
        prefix = "/archives/gemini/attachments/"
        if parsed.hostname != "lists.orbitalfox.eu" or not parsed.path.startswith(prefix):
            continue
        path = (root / unquote(parsed.path[len(prefix):])).resolve()
        if path.is_relative_to(root) and path.is_file():
            found.append((url, path))
    return found


def normalize(record, repo):
    """Return serialized normalized MIME fields/body plus auditable changes."""
    source = record.message
    selected = source.get_body(preferencelist=("plain", "html"))
    if selected is None:
        raise ValueError(f"No readable plain/HTML body: {record.mid}")
    text, charset = decode_text(selected)
    changes, attachments, omitted = [], [], []
    if charset.lower().replace("_", "-") not in ("utf-8", "utf8", "ascii", "us-ascii"):
        changes.append("charset-to-utf8")
    if text != text.replace("\r\n", "\n").replace("\r", "\n"):
        changes.append("line-endings-normalized")
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    def add(data, content_type, filename, origin, location=None, params=None, headers=None):
        attachments.append({"data": data, "type": content_type, "filename": filename,
                            "origin": origin, "location": location, "params": params or {}, "headers": headers or {}})

    if selected.get_content_type() == "text/html":
        add(selected.get_payload(decode=True), "text/html", "original-body.html", record.source,
            params=dict(selected.get_params()[1:]))
        text = html_to_text(text)
        changes.append("html-to-text")
    elif str(selected.get_param("format", "")).lower() == "flowed":
        text = unflow(text, str(selected.get_param("delsp", "no")).lower() == "yes")
        changes.append("flowed-to-fixed")

    alternative_count = 0

    def retain(part, alternative=False):
        nonlocal alternative_count
        if part is selected:
            return
        kind = part.get_content_type()
        if kind in SIGNATURE_TYPES:
            omitted.append({"type": kind, "filename": part.get_filename()})
            return
        # An attached email is a single opaque attachment, not another body.
        if part.get_content_maintype() == "message":
            attached = deepcopy(part)
            if "Content-Disposition" in attached:
                del attached["Content-Disposition"]
            attached.add_header("Content-Disposition", "attachment", filename=part.get_filename() or "attached-message.eml")
            add(attached, kind, part.get_filename() or "attached-message.eml", record.source)
        elif part.is_multipart():
            for child in part.iter_parts():
                retain(child, alternative or kind == "multipart/alternative")
        elif alternative and kind in ("text/plain", "text/html") and not part.is_attachment():
            alternative_count += 1
        else:
            add(part.get_payload(decode=True) or b"", kind,
                part.get_filename() or f"attachment-{len(attachments) + 1}" + (mimetypes.guess_extension(kind) or ".bin"),
                record.source, params=dict((part.get_params() or [])[1:]),
                headers={name: str(part[name]) for name in ("Content-ID", "Content-Location", "Content-Description") if part.get(name)})

    if source is not selected:
        retain(source)
    if alternative_count:
        changes.append("duplicate-alternatives-removed")
    if omitted:
        changes.append("detached-signatures-removed")

    captures = captured_attachments(text, repo)
    placeholder = "An HTML attachment was scrubbed" in text and not HTML_NOTICE.sub("", text).strip()
    for url, path in captures:
        data = path.read_bytes()
        kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix.lower() == ".sig":
            omitted.append({"type": "application/pgp-signature", "source": str(path.relative_to(repo))})
            if "detached-signatures-removed" not in changes:
                changes.append("detached-signatures-removed")
            continue
        if placeholder and kind == "text/html" and len(captures) == 1:
            html = EmailMessage()
            html.set_payload(data)
            recovered, _ = decode_text(html)
            text = html_to_text(recovered, url)
            changes.append("scrubbed-html-body-recovered")
        add(data, kind, path.name, str(path.relative_to(repo)), url)
    if any(item["location"] for item in attachments):
        changes.append("saved-attachments-recovered")

    unique = {}
    for item in attachments:
        data = item["data"]
        raw = data.as_bytes(policy=MIME_POLICY) if isinstance(data, EmailMessage) else data
        key = (item["type"], hashlib.sha256(raw).digest())
        if key not in unique:
            unique[key] = item
        elif item["location"]:
            unique[key]["location"] = item["location"]
    attachments = list(unique.values())

    # The writer adds the separate mbox blank line. Only supply a missing
    # terminal newline here; preserve existing intentional body blank lines.
    if not text.endswith("\n"):
        text += "\n"
        changes.append("terminal-newline-normalized")
    result = EmailMessage(policy=MIME_POLICY)
    # Match the C# writer's direct UTF-8 text. 8bit declares those bytes without
    # introducing quoted-printable escapes or transport soft wrapping.
    result.set_content(text, subtype="plain", charset="utf-8", cte="8bit")
    retained = []
    for item in attachments:
        if isinstance(item["data"], EmailMessage):
            if not result.is_multipart():
                result.make_mixed()
            result.attach(item["data"])
            digest = hashlib.sha256(item["data"].as_bytes(policy=MIME_POLICY)).hexdigest()
            size = len(item["data"].as_bytes(policy=MIME_POLICY))
        else:
            main, sub = item["type"].split("/", 1)
            result.add_attachment(item["data"], maintype=main, subtype=sub,
                                  filename=item["filename"], params=item["params"])
            for name, value in item["headers"].items():
                result.get_payload()[-1][name] = value
            if item["location"]:
                if "Content-Location" in result.get_payload()[-1]:
                    del result.get_payload()[-1]["Content-Location"]
                result.get_payload()[-1]["Content-Location"] = item["location"]
            digest, size = hashlib.sha256(item["data"]).hexdigest(), len(item["data"])
        retained.append({"type": item["type"], "filename": item["filename"], "bytes": size,
                         "sha256": digest, "source": item["origin"], "url": item["location"]})
    if retained:
        # Python's default boundary is random; reproducible builds need a fixed
        # boundary that cannot occur in the serialized parts.
        boundary = "orbitalfox-" + hashlib.sha256(record.mid.encode()).hexdigest()[:24]
        while any(boundary.encode() in p.as_bytes(policy=MIME_POLICY) for p in result.iter_parts()):
            boundary += "x"
        result.set_boundary(boundary)
        changes.append("attachments-retained")
    serialized = result.as_bytes(policy=MIME_POLICY)
    header, body = serialized.split(b"\n\n", 1)
    details = {"number": f"{record.number:06}", "message_id": record.mid,
               "operations": changes, "source_body_type": selected.get_content_type(),
               "source_charset": charset, "attachments": retained, "omitted_signatures": omitted,
               "body_status": "html-placeholder-only" if "An HTML attachment was scrubbed" in text
               and not HTML_NOTICE.sub("", text).strip() else "available",
               "text_sha256": hashlib.sha256(result.get_body(preferencelist=("plain",)).get_payload(decode=True)).hexdigest()}
    return header + b"\n", body, details


def normalize_records(records, repo):
    details = []
    for record in records:
        header, body, change = normalize(record, repo)
        record.normalized = (header, body)
        details.append(change)
    return {"policy": "UTF-8 fixed plain text, 8bit; attachments base64; original core headers retained",
            "messages": len(records),
            "operations": dict(Counter(op for row in details for op in row["operations"])),
            "attachments": sum(len(row["attachments"]) for row in details),
            "omitted_signatures": sum(len(row["omitted_signatures"]) for row in details),
            "remaining_placeholder_only_messages": [row["number"] for row in details if row["body_status"] == "html-placeholder-only"],
            "changes": [row for row in details if row["operations"]]}

def require(condition, message):
    """Use explicit checks: running Python with -O must not disable validation."""
    if not condition:
        raise ValueError(message)


def message_id(value):
    value = str(value or "").strip()
    match = re.search(r"<([^<>]+)>", value)
    return match[1] if match else value


def reference_ids(value):
    return re.findall(r"<([^<>]+)>", str(value or ""))


def decoded_header(value):
    parts = []
    for text, charset in decode_header(str(value)):
        if isinstance(text, bytes):
            try:
                text = text.decode(charset or "utf-8", "replace")
            except LookupError:
                text = text.decode("utf-8", "replace")
        parts.append(text)
    return "".join(parts)


def subject_key(value):
    value = " ".join(decoded_header(value).split()).casefold().replace("\\n", " ").replace(" at ", "@")
    while re.match(r"^re\s*:\s*", value):
        value = re.sub(r"^re\s*:\s*", "", value, count=1)
    return "".join(value.split())


def author_key(value):
    value = " ".join(decoded_header(value).split()).replace(" at ", "@")
    # A few archived display names contain UTF-8 decoded once as Latin-1.
    try:
        value = value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    value = value.casefold().replace("(", "").replace(")", "")
    return "draco metallium" if "draco metallium" in value else value


def utc_date(value):
    raw = str(value or "")
    date = parsedate_to_datetime(raw)
    if date.tzinfo is None:
        # RFC 5322 -0000 is UTC with the author's local zone unspecified.
        require(bool(re.search(r"-0000(?:\s*\([^)]*\))?\s*$", raw)),
                f"Date has no usable timezone: {raw!r}")
        date = date.replace(tzinfo=timezone.utc)
    return date.astimezone(timezone.utc)


def iso_date(date):
    return date.isoformat(timespec="seconds").replace("+00:00", "Z")


def split_headers(header):
    """Retain each original header's bytes, folding, spelling and encoding."""
    fields = []
    for line in header.splitlines(keepends=True):
        if line.startswith((b" ", b"\t")) and fields:
            name, raw = fields[-1]
            fields[-1] = (name, raw + line)
        else:
            require(b":" in line, f"Malformed header line: {line[:100]!r}")
            fields.append((line.split(b":", 1)[0].decode("ascii").lower(), line))
    return fields


@dataclass
class Email:
    mid: str
    source: str
    ordinal: int
    envelope: bytes
    fields: list
    body: bytes
    message: object
    date: datetime
    number: int | None = None
    year: int | None = None
    evidence: list = field(default_factory=list)
    normalized: tuple | None = None

    @property
    def subject(self):
        raw = next((raw for name, raw in self.fields if name == "subject"), b"Subject:")
        return decoded_header(raw.split(b":", 1)[1].decode("utf-8", "replace")).strip()

    @property
    def sender(self):
        # The structured email policy drops address comments when stringifying
        # From. Mailman's exports put the author's name IN that comment.
        raw = next((raw for name, raw in self.fields if name == "from"), b"From:")
        value = decoded_header(raw.split(b":", 1)[1].decode("utf-8", "replace"))
        names = getaddresses([value])
        return names[0] if names else ("", "")

    @property
    def method(self):
        return min((e["method"] for e in self.evidence), key=PRIORITY.get)

    @property
    def status(self):
        return "inferred" if self.method == "anchored-chronological-tail" else "source-supported"


def read_mbox(path, source=None, body_format="mboxrd"):
    """Recognize real header blocks, rather than splitting every body 'From '.

    Original Mailman text exports are NOT mboxrd escaped. Personal maildir
    exports made by mblaze are mboxrd; decode one transport quote in those.
    """
    data = path.read_bytes()
    if path.suffix == ".gz":
        data = gzip.decompress(data)
    starts, body_from = [], 0
    for match in SEPARATOR.finditer(data):
        end = HEADER_END.search(data, match.end(), min(len(data), match.end() + 65536))
        header = data[match.end():end.start()] if end else b""
        if (re.match(rb"[!-9;-~]+:", header)
                and re.search(rb"^Message-ID:", header, re.M | re.I)
                and re.search(rb"^Date:", header, re.M | re.I)):
            starts.append((match.start(), match.end(), end.end()))
        else:
            body_from += 1
    require(starts and not data[:starts[0][0]].strip(), f"No valid initial message: {path}")
    records = []
    for ordinal, (start, header_start, body_start) in enumerate(starts):
        end = starts[ordinal + 1][0] if ordinal + 1 < len(starts) else len(data)
        header = data[header_start:body_start]
        header = HEADER_END.split(header, maxsplit=1)[0]
        body = data[body_start:end]
        # The final blank line is the mbox container separator. Match stdlib
        # mailbox's behavior, keeping all preceding email-body blank lines.
        if re.search(rb"(?:^|\n)\r?\n$", body):
            body = body[:-2] if body.endswith(b"\r\n") else body[:-1]
        if body_format == "mboxrd":
            body = re.sub(rb"^>(>*From )", rb"\1", body, flags=re.M)
        parsed = BytesParser(policy=policy.default).parsebytes(header + b"\n\n" + body)
        mid = message_id(parsed.get("Message-ID"))
        require(mid and len(parsed.get_all("Date", [])) == 1
                and len(parsed.get_all("Message-ID", [])) == 1, f"Missing/duplicate identity or date: {path}:{ordinal}")
        records.append(Email(mid, source or str(path), ordinal,
                             data[start:header_start].rstrip(b"\r\n"), split_headers(header),
                             body, parsed, utc_date(parsed["Date"])))
    return records, body_from


class MessagePage(HTMLParser):
    """The reply link embeds THIS message's ID, not the ID of its parent."""
    def __init__(self):
        super().__init__()
        self.ids, self.numbers = set(), set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        href = attrs.get("href", "")
        if tag == "link" and attrs.get("rel", "").lower() == "made":
            self.ids.update(message_id(v) for v in parse_qs(urlsplit(href).query).get("In-Reply-To", []))
        if tag == "a" and re.match(r"(?:date|thread|author|subject)\.html#\d+$", href):
            self.numbers.add(int(urlsplit(href).fragment))


def read_html(root):
    direct, snapshots = {}, defaultdict(list)
    pattern = re.compile(r'<!--\d+ (.*?)-->\s*<LI><A HREF="(\d+)\.html">(.*?)</A>'
                         r'<A NAME="\d+">.*?</A>\s*<I>(.*?)</I>', re.I | re.S)
    for path in sorted(root.glob("*/*.html")):
        text = path.read_text(encoding="utf-8")
        year = int(path.parent.name)
        if path.stem.isdigit():
            page = MessagePage()
            page.feed(text)
            number = int(path.stem)
            require(len(page.ids) == 1 and page.numbers == {number}, f"Ambiguous HTML identity/number: {path}")
            mid = next(iter(page.ids))
            require(mid not in direct or direct[mid][0] == number, f"Conflicting HTML identity: {mid}")
            direct[mid] = (number, year, str(path.relative_to(root)))
            continue
        entries = []
        for key, num, subject, author in pattern.findall(text):
            parts = [(int(t), int(n)) for t, n in re.findall(r"(\d+)\.(\d+)-", key)]
            number = int(num)
            require(parts and parts[-1][1] == number, f"Bad thread key: {path}:{number}")
            plain = lambda value: unescape(re.sub(r"<[^>]*>", "", value)).strip()
            entries.append({"number": number, "year": year, "timestamp": parts[-1][0],
                            "path": [n for _, n in parts], "subject": plain(subject),
                            "author": plain(author), "source": str(path.relative_to(root))})
        if entries:
            snapshots[year].append(entries)
    require(direct and snapshots, f"No original HTML found under {root}")
    # index.html can be a NEWER copy of the thread listing than thread.html.
    latest = {year: max(options, key=lambda entries: (max(e["timestamp"] for e in entries), len(entries)))
              for year, options in snapshots.items()}
    return direct, latest


def choose_copy(copies):
    """Prefer MIME originals, then the final duplicate within an export.

    Mailman's canonical indexed copy is the last delivery of a repeated ID.
    One repeat adds a courtesy-copy note, which also survives in its HTML.
    """
    def score(record):
        msg = record.message
        return (msg.is_multipart(), bool(msg.get("Content-Type") or msg.get("MIME-Version")),
                "@" in record.sender[1], -len(record.source), record.ordinal)
    return max(copies, key=score)


def content_audit(records, repo):
    """A text/plain label can hide a scrubbed-HTML notice, not an email body.

    Report these separately from surviving HTML-only MIME originals. A saved
    attachment page is evidence, not necessarily the original HTML/MIME bytes.
    """
    html_only, placeholders, notice_count = [], [], 0
    notice = re.compile(r"(?:-+\s*next part\s*-+\s*)?An HTML attachment was scrubbed"
                        r"\.\.\.\s*URL:\s*(?:<[^>]+>|\S+)\s*")
    for record in records:
        plain = record.message.get_body(preferencelist=("plain",))
        if plain is None:
            if record.message.get_body(preferencelist=("html",)) is not None:
                html_only.append(f"{record.number:06}")
            continue
        payload = plain.get_payload(decode=True) or b""
        text = payload.decode(plain.get_content_charset() or "utf-8", "replace")
        if "An HTML attachment was scrubbed" not in text:
            continue
        notice_count += 1
        if notice.sub("", text).strip():
            continue
        attachments = []
        for url in re.findall(r"https?://[^\s>]+", text):
            parsed = urlsplit(url)
            path = repo / "sources" / parsed.netloc / parsed.path.lstrip("/")
            attachments.append({"url": url, "saved_page": str(path.relative_to(repo)) if path.is_file() else None})
        placeholders.append({"number": f"{record.number:06}", "message_id": record.mid,
                             "subject": record.subject, "attachments": attachments})
    return {"html_only_originals": html_only,
            "messages_with_scrubbed_html_notices": notice_count,
            "html_placeholder_only_messages": placeholders}


def reconstruct(repo):
    source_dir = repo / "sources/mbox"
    rules = SOURCE_RULES
    excluded = EXCLUDED_MESSAGE_IDS
    versions, sequences, input_stats = defaultdict(list), defaultdict(list), []
    for path in sorted(source_dir.rglob("*")):
        if not path.is_file() or not re.search(r"\.(?:mbox|mbx)(?:\.gz)?$", path.name, re.I):
            continue
        relative = str(path.relative_to(source_dir))
        # Source conventions are the same for compressed and uncompressed copies.
        options = rules.get(relative.removesuffix(".gz"), {})
        records, repaired = read_mbox(path, relative, options.get("format", "mboxrd"))
        for record in records:
            versions[record.mid].append(record)
        if options.get("sequence_year"):
            sequences[options["sequence_year"]].append(records)
        input_stats.append({"path": f"sources/mbox/{relative}", "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                            "occurrences": len(records), "identities": len({r.mid for r in records}),
                            "unescaped_body_from_lines": repaired})
    emails = {mid: choose_copy(copies) for mid, copies in versions.items()}
    supplemented, variants = [], []
    for mid, copies in versions.items():
        require(len({r.date for r in copies}) == 1, f"Conflicting source dates: {mid}")
        # A richer MIME copy can omit a reply header that another copy retains.
        # Fill missing core headers from evidence; never replace existing ones.
        chosen = emails[mid]
        for name in CORE_HEADERS:
            alternatives = [r for r in copies if r.message.get(name) is not None]
            if chosen.message.get(name) is None and alternatives:
                donor = choose_copy(alternatives)
                chosen.fields.extend((n, raw) for n, raw in donor.fields if n == name)
                chosen.message[name] = donor.message[name]
                supplemented.append({"message_id": mid, "header": name, "source": donor.source})
        # Different bytes can reflect MIME vs flattened text. List the variants
        # for inspection, rather than silently calling every repeated ID an
        # identical duplicate. Numbering still selects the indexed delivery.
        body_hashes = {hashlib.sha256(r.body).hexdigest() for r in copies}
        differing_headers = [name for name in CORE_HEADERS if len({
            tuple(raw for n, raw in r.fields if n == name) for r in copies}) > 1]
        if len(body_hashes) > 1 or differing_headers:
            variants.append({"message_id": mid, "selected_source": chosen.source,
                             "selected_ordinal": chosen.ordinal, "differing_raw_headers": differing_headers,
                             "copies": [{"source": r.source, "ordinal": r.ordinal,
                                         "body_sha256": hashlib.sha256(r.body).hexdigest()} for r in copies]})
    direct, snapshots = read_html(repo / "sources/lists.orbitalfox.eu/archives/gemini")
    owners, aliases = {}, {}

    def assign(mid, number, year, method, source):
        require(mid in emails,
                f"Missing source email {number:06} ({mid}). Saved HTML identifies it, "
                "but the current source mboxes do not contain it. Restore the supplemental "
                "source mboxes (2021-nervuri.mbox and adnano-complete.mbox) under "
                "sources/mbox/, or supply another mbox containing this email.")
        require(mid not in excluded, f"Excluded email appears in original archive: {mid}")
        record = emails[mid]
        require(record.number in (None, number), f"Number conflict for {mid}: {record.number} vs {number} ({source})")
        require(number not in owners or owners[number] == mid, f"Two emails assigned number {number}")
        record.number, record.year = number, year
        owners[number] = mid
        record.evidence.append({"method": method, "source": source})

    for mid, (number, year, path) in direct.items():
        assign(mid, number, year, "html-message-id", path)
    for year, alternatives in sorted(sequences.items()):
        require(year in snapshots, f"No original yearly index for {year}")
        source = max(alternatives, key=len)
        for shorter in alternatives:
            require([r.mid for r in source[:len(shorter)]] == [r.mid for r in shorter],
                    f"Original-order exports disagree: {source[0].source} and {shorter[0].source}")
        offset = min(e["number"] for e in snapshots[year])
        last = {r.mid: offset + i for i, r in enumerate(source)}
        # Duplicate deliveries consumed slots. Deduplicate AFTER replaying them.
        for i, record in enumerate(source):
            if offset + i != last[record.mid]:
                aliases[offset + i] = last[record.mid]
        for mid, number in last.items():
            assign(mid, number, year, "original-mbox-order", source[0].source)
    by_time = defaultdict(set)
    for mid, copies in versions.items():
        by_time[int(copies[0].date.timestamp())].add(mid)
    for entries in snapshots.values():
        for entry in entries:
            candidates = [mid for mid in by_time[entry["timestamp"]]
                          if any(subject_key(r.subject) == subject_key(entry["subject"])
                                 and author_key(r.sender[0] or r.sender[1]) == author_key(entry["author"])
                                 for r in versions[mid])]
            require(len(candidates) == 1, f"Ambiguous index match {entry['number']}: {candidates}")
            assign(candidates[0], entry["number"], entry["year"], "html-index-metadata", entry["source"])

    last_year = max(snapshots)
    last_entry = max(snapshots[last_year], key=lambda e: e["number"])
    tail = sorted((r for mid, r in emails.items() if mid not in excluded
                   and int(r.date.timestamp()) > last_entry["timestamp"]), key=lambda r: (r.date, r.mid))
    anchors = 0
    for number, record in enumerate(tail, last_entry["number"] + 1):
        if record.number is not None:
            require(record.number == number, f"Tail order disagrees with original HTML: {record.mid}")
            anchors += 1
        else:
            assign(record.mid, number, last_year, "anchored-chronological-tail", record.source)
    require(not tail or anchors, "Cannot infer a chronological tail without surviving HTML anchors")
    require(all(r.number is not None for mid, r in emails.items() if mid not in excluded),
            "Some messages remain unnumbered; add original evidence before publishing")
    records = sorted((r for mid, r in emails.items() if mid not in excluded), key=lambda r: r.number)
    parents, thread_methods, order, anomalies = {}, {}, [], []
    for year, entries in sorted(snapshots.items()):
        for entry in entries:
            path = []
            for number in entry["path"]:
                number = aliases.get(number, number)
                if not path or path[-1] != number:
                    path.append(number)
            require(len(set(path)) == len(path) and path[-1] == entry["number"], f"Cyclic original thread path: {entry}")
            require(all(n in owners for n in path), f"Unknown message in original thread path: {entry}")
            if path != entry["path"]:
                anomalies.append({"number": entry["number"], "original": entry["path"], "canonical": path})
            number = entry["number"]
            parents[number] = path[-2] if len(path) > 1 else None
            thread_methods[number] = "html-index"
            order.append(number)
    for record in records:
        if record.number in parents:
            continue
        ids = reference_ids(record.message.get("In-Reply-To"))
        ids += list(reversed(reference_ids(record.message.get("References"))))
        parent = next((emails[mid] for mid in ids if mid in emails and emails[mid].number is not None
                       and mid != record.mid and emails[mid].year == record.year), None)
        parents[record.number] = parent.number if parent else None
        thread_methods[record.number] = ("email-headers" if parent else
                                        "unresolved" if ids or re.match(r"^re\s*:", record.subject, re.I)
                                        else "header-root")
        order.append(record.number)
    for number, parent in parents.items():
        seen = {number}
        while parent is not None:
            require(parent in parents and parent not in seen, f"Cyclic/unknown thread parent for {number}")
            seen.add(parent)
            parent = parents[parent]
    report = {"messages": len(records), "years": dict(Counter(str(r.year) for r in records)),
              "first_date_utc": iso_date(min(r.date for r in records)),
              "last_date_utc": iso_date(max(r.date for r in records)),
              "numbering_methods": dict(Counter(r.method for r in records)),
              "numbering_status": dict(Counter(r.status for r in records)),
              "individual_html_identities_checked": len(direct),
              "threading_methods": dict(Counter(thread_methods.values())),
              "original_thread_snapshots": [{"year": year, "source": entries[0]["source"],
                                              "messages": len(entries)} for year, entries in sorted(snapshots.items())],
              "canonicalized_original_thread_paths": anomalies,
              "inferred_numbers": [f"{r.number:06}" for r in records if r.status == "inferred"],
              "excluded_messages": [{"message_id": mid, "reason": reason} for mid, reason in excluded.items() if mid in emails],
              "supplemented_headers": supplemented,
              "source_copy_variants": variants,
              "content": content_audit(records, repo),
              "inputs": input_stats}
    return records, parents, thread_methods, order, report


def output_parts(record):
    """Preserve core headers, then add the normalized MIME entity and metadata."""
    require(record.normalized is not None, f"Email has not been normalized: {record.mid}")
    kept = []
    for wanted in CORE_HEADERS:
        for name, raw in record.fields:
            if name == wanted:
                if name == "from" and b"@" not in raw.split(b"(", 1)[0]:
                    prefix, mark, comment = raw.partition(b"(")
                    raw = prefix.replace(b" at ", b"@", 1) + mark + comment
                kept.append(raw)
    mime_header, body = record.normalized
    kept.append(mime_header)
    kept += [f"X-Orbital-Fox-Message-Number: {record.number}".encode(),
             f"X-Orbital-Fox-Url: https://lists.orbitalfox.eu/archives/gemini/{record.year}/{record.number:06}.html".encode(),
             f"X-Orbital-Fox-Message-Number-Source: {record.method}".encode()]
    # Only add a field's final newline. Its internal folding stays unchanged.
    header = b"".join(raw if raw.endswith(b"\n") else raw + b"\n" for raw in kept)
    envelope = record.envelope
    envelope = re.sub(rb"^From (\S+) at (\S+)", rb"From \1@\2", envelope)
    sender = envelope.split()[1]
    date = record.date
    # Source separators have no zone and some used a summer-time server clock.
    # Generate a UTC container timestamp from Date, without changing Date itself.
    # Spell month/day names explicitly so output does not depend on locale.
    weekday = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[date.weekday()]
    month = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")[date.month - 1]
    # Use one space after the sender and keep the unpadded day number.
    timestamp = f"{weekday} {month} {date.day} {date:%H:%M:%S} {date.year:04}"
    envelope = b"From " + sender + b" " + timestamp.encode("ascii")
    return envelope, header, body


def write_mbox(path, records):
    with path.open("wb") as out:
        for record in records:
            envelope, header, body = output_parts(record)
            # mboxrd quotes every From_ line, including already quoted ones.
            body = re.sub(rb"^(>*From )", rb">\1", body, flags=re.M)
            out.write(envelope + b"\n" + header + b"\n" + body)
            if not body.endswith(b"\n"):
                out.write(b"\n")
            # Always add a separate transport blank line. Readers must not
            # consume a blank line that belongs to the normalized MIME body.
            out.write(b"\n")


def write_xml(directory, records, parents, methods, order):
    inventory = ET.Element("emails")
    by_number = {r.number: r for r in records}
    for record in records:
        node = ET.SubElement(inventory, "email")
        for key, value in [("MessageID", record.mid), ("MessageNumber", f"{record.number:06}"),
                           ("ArchiveYear", str(record.year)), ("Date", iso_date(record.date)),
                           ("Subject", record.subject)]:
            ET.SubElement(node, key).text = value
        sender = ET.SubElement(node, "From")
        name, address = record.sender
        if "@" not in address:
            # The remaining masked records have Mailman's address-first form.
            fixed = str(record.message.get("From", "")).replace(" at ", "@", 1)
            addresses = getaddresses([fixed])
            if addresses:
                name, address = addresses[0]
        ET.SubElement(sender, "Name").text = name
        ET.SubElement(sender, "Address").text = address
        for key, value in [("NumberMethod", record.method), ("NumberStatus", record.status),
                           ("MessageSource", record.source)]:
            ET.SubElement(node, key).text = value
    roots, children = [], defaultdict(list)
    for number in order:
        if parents[number] is None:
            roots.append(number)
        else:
            children[parents[number]].append(number)
    threads = ET.Element("threads")

    def append_message(container, number):
        record = by_number[number]
        attrs = {"number": f"{number:06}", "subject": record.subject, "date": iso_date(record.date)}
        if methods[number] != "html-index":
            attrs["threading"] = methods[number]
        node = ET.SubElement(container, "message", attrs)
        for child in children[number]:
            append_message(node, child)

    for number in roots:
        append_message(ET.SubElement(threads, "thread"), number)
    for name, root in [("emails.xml", inventory), ("threads.xml", threads)]:
        ET.indent(root, space="  ")
        ET.ElementTree(root).write(directory / name, encoding="utf-8", xml_declaration=True)


def validate_output(directory, records):
    """Read outputs back; check identities, original dates, normalized MIME/XML."""
    output, rejected = read_mbox(directory / "all.mbox")
    require(not rejected and len(output) == len(records), "Output mbox message boundaries changed")
    require(len({r.mid for r in output}) == len(records), "Duplicate output identities")
    require(len({r.message["X-Orbital-Fox-Message-Number"] for r in output}) == len(records), "Duplicate output numbers")
    for before, after in zip(records, output):
        require(before.mid == after.mid and before.date == after.date, f"Output identity/date changed: {before.mid}")
        require(before.normalized[1].rstrip(b"\r\n") == after.body.rstrip(b"\r\n"), f"Normalized body changed during storage: {before.mid}")
        plain = after.message.get_body(preferencelist=("plain",))
        require(plain is not None and plain.get_content_charset() == "utf-8"
                and str(plain["Content-Transfer-Encoding"]).lower() == "8bit"
                and plain.get_param("format") is None and plain.get_param("delsp") is None,
                f"Output lacks canonical UTF-8 fixed text: {before.mid}")
        require(after.message.get_content_type() in ("text/plain", "multipart/mixed"),
                f"Unexpected normalized MIME container: {before.mid}")
        original_date = [raw for name, raw in before.fields if name == "date"]
        output_date = [raw for name, raw in after.fields if name == "date"]
        require(original_date == output_date, f"Original Date header was rewritten: {before.mid}")
        require(after.message.get("X-Orbital-Fox-Message-Number-Status") is None,
                f"Removed status header reappeared: {before.mid}")
        require(all(after.message.get(name) is None for name in ("To", "Cc", "Reply-To")),
                f"Excluded recipient/reply header reappeared: {before.mid}")
    annual_ids = []
    for year in sorted({r.year for r in records}):
        annual, _ = read_mbox(directory / f"{year}.mbox")
        annual_ids.extend(r.mid for r in annual)
    require(annual_ids == [r.mid for r in records], "Annual mboxes disagree with all.mbox")
    tree = ET.parse(directory / "threads.xml")
    numbers = [int(n.attrib["number"]) for n in tree.iter("message")]
    require(len(numbers) == len(set(numbers)) == len(records), "Duplicate/missing XML thread messages")
    require(set(numbers) == {r.number for r in records}, "Thread XML references unknown numbers")
    inventory = ET.parse(directory / "emails.xml")
    require([n.findtext("MessageID") for n in inventory.findall("email")] == [r.mid for r in records],
            "XML inventory disagrees with mboxes")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=Path("mbox"), help="Relative to the repository unless absolute")
    args = parser.parse_args()
    repo = args.repo.resolve()
    output = args.output if args.output.is_absolute() else repo / args.output
    records, parents, methods, order, _ = reconstruct(repo)
    require({record.year for record in records}.issubset(ARCHIVE_YEARS), "Unexpected archive year")
    normalize_records(records, repo)
    output.mkdir(parents=True, exist_ok=True)
    # Validate a complete temporary build before replacing any existing output.
    with tempfile.TemporaryDirectory(prefix=".reconstruct-", dir=output.parent) as temp:
        stage = Path(temp)
        for year in ARCHIVE_YEARS:
            write_mbox(stage / f"{year}.mbox", [r for r in records if r.year == year])
        write_mbox(stage / "all.mbox", records)
        write_xml(stage, records, parents, methods, order)
        validate_output(stage, records)
        for name in OUTPUT_FILES:
            (stage / name).replace(output / name)
    print(f"Wrote {len(records):,} messages to {output}")


if __name__ == "__main__":
    main()
