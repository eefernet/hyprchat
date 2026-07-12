"""Email HTML preparation for the Email panel reader.

Turns a raw IMAP HTML body into srcDoc-ready markup: inline `cid:` image
references become data: URIs (from the same fetch's attachments), then the
whole document goes through artifact_files.sanitize_preview_html — the same
regex sanitizer the artifact HTML previews and /api/proxy-preview use (strips
<script>, on*= handlers, javascript: URIs, and injects <base target="_blank">
so links open in a new tab).

This is defense layer one of two: the frontend renders the result in an
iframe sandboxed WITHOUT allow-scripts/allow-same-origin/allow-forms
(allow-popups only, so links can open). Never weaken either layer — email
HTML is fully attacker-controlled content.

artifact_files is imported at call time: it pulls fastapi, and this module
stays importable in offline tests without it.
"""

from __future__ import annotations

import re


def _cid_pattern(cid: str) -> re.Pattern:
    # src="cid:X", src='cid:X', and unquoted src=cid:X
    escaped = re.escape(cid)
    return re.compile(
        r"src\s*=\s*([\"'])cid:" + escaped + r"\1"
        r"|src\s*=\s*cid:" + escaped + r"(?=[\s>])",
        re.IGNORECASE,
    )


def embed_inline_images(html: str, inline_images: list | None) -> str:
    """Replace cid: image references with data URIs. Unmatched cids are left
    alone (they just render as broken images, like any mail client)."""
    for img in inline_images or []:
        cid = (img.get("cid") or "").strip()
        ctype = (img.get("content_type") or "image/png").strip()
        b64 = img.get("b64") or ""
        if not cid or not b64:
            continue
        data_uri = f'src="data:{ctype};base64,{b64}"'
        html = _cid_pattern(cid).sub(data_uri, html)
    return html


def prepare_email_html(html: str, inline_images: list | None = None) -> str:
    """srcDoc-ready sanitized email HTML; empty string when there is no HTML
    part (the reader falls back to the plain-text body)."""
    html = (html or "").strip()
    if not html:
        return ""
    html = embed_inline_images(html, inline_images)
    from artifact_files import sanitize_preview_html  # call-time: pulls fastapi
    return sanitize_preview_html(html, "")
