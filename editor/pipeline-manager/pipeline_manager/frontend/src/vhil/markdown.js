/*
 * vHIL: markdown to safe HTML for `v-html` (CHANGELOG-VHIL.md).
 *
 * showdown 2.1.0, the last release, has open XSS and ReDoS advisories and
 * escapes no raw HTML in its input, so its output always goes through
 * DOMPurify here, and its input is capped: node descriptions and text-area
 * notes are short, and a huge one is what the ReDoS needs.
 */
import DOMPurify from 'dompurify';
import showdown from 'showdown';

/** Characters of markdown rendered; the rest is cut (the ReDoS guard). */
export const MARKDOWN_MAX = 20000;

const converter = new showdown.Converter({
    smartIndentationFix: true,
    simpleLineBreaks: true,
});

// Upstream's link rewrite, kept: a link opens in a new tab instead of
// leaving the editor, and is not a tab stop.
const aTagRe = /<a href="[a-zA-Z0-9-$_.+!*'()/&?=:%]+">/gm;

/** `text` (markdown) as sanitized HTML. */
export function renderMarkdown(text) {
    let src = String(text ?? '');
    if (src.length > MARKDOWN_MAX) src = `${src.slice(0, MARKDOWN_MAX)}\n\n…`;
    let html = converter.makeHtml(src);
    html.match(aTagRe)?.forEach((match) => {
        const hrefParts = match.split('"');
        const newEnd = ` tabindex="-1" target="_blank"${hrefParts[2]}`;
        html = html.replace(match, [hrefParts[0], hrefParts[1], newEnd].join('"'));
    });
    return DOMPurify.sanitize(html, { ADD_ATTR: ['target'] });
}
