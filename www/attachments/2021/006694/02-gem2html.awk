#!/usr/bin/awk -f

BEGIN {
    print "<!DOCTYPE html>"
    print "<html>"
    print "<head>"
    print "    <meta charset=\"utf-8\">"
    print "    <meta name=\"referrer\" content=\"no-referrer\">"
    print "    <style>"
    print "        body {font-family: sans-serif; margin: 1em; background: #282528; color: #e9f0f1 ; margin: 0 auto; max-width: 60em; padding: 1em; }"
    print "      pre {overflow: auto; font-family: monospace; color: #c7ff00;  max-width: calc(100vw - 2em);}"
    print "        hr  {color:#eee; background-color:#eee; border:#eee; height:4px; }"
    print " blockquote { margin: 0; border-left: 5px solid #fff9;  padding: 0.5em 1em;}"
    print " a {text-decoration: none;color: #f0c724;}"
    print " h1, h2, h3 {color: #9fe9ff;}"
    print "    </style>"
    print "    <title>TITLEEE</title>"
    print "</head>"
    print "<body>"

    in_pre = 0;
    in_list = 0;
}

!in_pre && /^```/ {
    in_pre = 1;
    if (in_list) {
       in_list = 0;
       print("</ul>");
    }
    print "<pre>";
    next
}
in_pre && /^```/    { in_pre = 0; print "</pre>"; next }
in_pre       { print san($0); next }

/^###/  { output("<h3>", substr($0, 4), "</h3>"); next }
/^##/   { output("<h2>", substr($0, 3), "</h2>"); next }
/^#/    { output("<h1>", substr($0, 2), "</h1>"); next }
/^>/    { output("<blockquote>", substr($0, 2), "</blockquote>"); next }
/^\*/   { output("<li>", substr($0, 2), "</li>"); next }
/^=>/   {
    $0 = substr($0, 3);
    link = $1;
    $1 = "";
    output_link(link, $0);
    next;
}
//  { output("<p>", $0, "</p>"); next }

END {
    if (in_list)
       print "</ul>"
    if (in_pre)
       print "</pre>"
    print "</body>\n</html>"
}

function trim(s) {
    sub("^[ \t]*", "", s);
    return s;
}

function san(s) {
    gsub("&", "\\&amp;", s)
    gsub("<", "\\&lt;", s)
    gsub(">", "\\&gt;", s)
    return s;
}

function output(ot, content, ct) {
    content = trim(content);

    if (!in_list && ot == "<li>") {
       in_list = 1;
       print "<ul>";
    }

    if (in_list && ot != "<li>") {
       in_list = 0;
       print "</ul>";
    }

    if (ot == "<p>" && content == "")
       return;

    printf("%s%s%s\n", ot, san(content), ct);
}

function output_link(link, content) {
    if (in_list) {
       in_list = 0;
       print "</ul>";
    }

   # If it's a local gemini file, link to the html:
    if((link !~ /^[a-zA-Z]*:\/\//) && (link ~ /\.gmi$/)){
        sub(/\.gmi$/, ".html", link)
    }


    if (content == "")
       content = link;

    printf("&#8594; <a href=\"%s\">%s</a><br>\n", link, trim(san(content)));
}

