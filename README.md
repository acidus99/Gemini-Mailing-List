# Gemini Protocol Mailing List

This is the rebuilt archive of the [Gemini](https://en.wikipedia.org/wiki/Gemini_(protocol)) Mailing List.

It is available as:

* A comprehensive HTML archive of the full contents of all messages.
* Clean, parsable mbox files containing the full contents all messages.

Various sources were used to completely reconstruct the mailing list and make it available.

## History

The mailing list documents the design and evolution of the [Gemini protocol](https://geminiprotocol.net/docs/), gemtext, companion ideas like robots.txt, various clients and servers, the community as a whole, and much more. When new people learn about Gemini, they often have questions about certain design choices. The mailing list is a great resource into those choices.

This list was active from early 2019 until January 2022 when the server hosting the mailing list, `list.orbitalfox.eu`, suffered a hardware failure.

## Sources (and problems)

All sources used to reconstruct the archive are available in `sources/`. They came from a variety of places:

- [HTML versions of the mailing lists messages](https://web.archive.org/web/20211218034113/https://lists.orbitalfox.eu/archives/gemini/), generated by Mailman, captured by the Internet Archive's Wayback Machine.
- Yearly mbox archives, generated by Mailman, captured by the Internet Archive's Wayback Machine.
- Personal mbox files uploaded by members of the community after `list.orbitalfox.eu` went offline. ([gemini://rawtext.club/~nervuri/gemini-archive/mailing-list/](gemini://rawtext.club/~nervuri/gemini-archive/mailing-list/))

Unfortunately, many of these sources are incomplete or have errors:

* About 20% of all messages are missing from the Wayback machine's HTML captures. Messages from 2019 and late 2021 are especially spotty.
* The Date/Thread/Author pages from the Wayback machine don't contain links to all messages.
* Mbox files contain truncated messages or are unparsable due to [from-munging](https://en.wikipedia.org/wiki/mbox#mbox_family) or [escaping](https://datatracker.ietf.org/doc/html/rfc3676#section-4.4) issues.
* Mbox files mask sender names or addresses, which can break parsing.
* Mbox files contain duplicate messages.
* Later mbox files were uploaded by community members and contain MBs of superfluous MIME headers (spam score, DKIM, list action headers, etc.) which are many times larger than the message contents themselves. 

## Rebuilding the archive
