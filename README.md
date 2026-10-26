# Gemini Protocol Mailing List Archive

A reconstructed archive of the mailing list that documented the Gemini protocol’s design, development, and community.

The archive covers August 2019 through December 2021. Its original host, Orbital Fox, went offline after a hardware failure in January 2022.

Available as:

- [Browsable HTML](https://mailing-list-archive.gemi.dev/www/)
- [Clean, reconstructed mbox files](mbox/)
- Gemtext version hosted at `gemini://gemi.dev/gemini-mailing-list/`

## Sources

Original files are preserved in `sources/`:

- Mailman message pages and indexes captured by the Wayback Machine.
- Official yearly Mailman mbox exports.
- Subscriber mailboxes contributed by the community.

These sources contain gaps, duplicates, truncated messages, masked addresses, and inconsistent headers. Combining them recovers messages missing from individual sources.

## Reconstruction

`tools/reconstruct.py` combines the sources, repairs truncated messages, removes duplicate deliveries, and normalizes unnecessary headers.

Original HTML message numbers are recovered using:

- **Message pages:** Match the embedded `Message-ID` to an email; the filename supplies its number.
- **Official mboxes:** Reconstruct delivery order, counting duplicate deliveries before deduplication.
- **Index pages:** Match numbered entries using subjects, authors, and timestamps.
- **Chronology:** Infer nine remaining numbers from known neighboring messages.

Each email records its number, original URL, and mapping method:

```text
X-Orbital-Fox-Message-Number: 15
X-Orbital-Fox-Url: https://lists.orbitalfox.eu/archives/gemini/2019/000015.html
X-Orbital-Fox-Message-Number-Source: original-mbox-order
```

Reconstruction produces yearly mboxes, `all.mbox`, `emails.xml`, and `threads.xml`. Threading primarily follows the saved Mailman indexes.

## Publishing

Run from the repository root:

```sh
python3 tools/reconstruct.py
python3 tools/publish_html.py
python3 tools/publish_gemtext.py
```

Both publishers use the reconstructed mboxes and XML files.

The HTML publisher recreates Mailman’s message pages and thread, subject, author, and date views in `www/`. Links to old Orbital Fox posts point to this archive, and mbox downloads link to GitHub.
