# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Helpers for the local Kobo annotation store (Reading Services API).

Stock Kobo firmware syncs annotations (highlights and notes) against the
Kobo Reading Services API. These helpers contain the pure logic used by
cps/readingservices.py to back annotations up locally and to keep Kobo's
cloud from ever answering for books it doesn't know about (which is what
makes the device delete its local annotations).
"""

# Keys (compared case-insensitively, underscores stripped) whose string value
# identifies a piece of content in Reading Services payloads.
_CONTENT_ID_KEYS = frozenset(("entitlementid", "contentid"))


def extract_annotation_fields(annotation):
    """Extract the fields worth indexing from a Kobo annotation object.

    The full annotation is stored as raw JSON alongside these; extraction
    failures must never lose data, so every field is optional.
    """
    if not isinstance(annotation, dict):
        annotation = {}
    return {
        "annotation_id": annotation.get("id"),
        "annotation_type": annotation.get("type"),
        "highlighted_text": annotation.get("highlightedText"),
        "note_text": annotation.get("noteText"),
        "highlight_color": annotation.get("highlightColor"),
        "client_last_modified": annotation.get("clientLastModifiedUtc"),
    }


def _entry_content_id(entry):
    if not isinstance(entry, dict):
        return None
    for key, value in entry.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        if key.lower().replace("_", "") in _CONTENT_ID_KEYS:
            return value
    return None


def extract_content_ids(payload):
    """Collect content/entitlement ids referenced anywhere in a payload.

    The exact shape of the checkforchanges request isn't publicly documented,
    so this walks the structure generically and picks up any dict entry with
    an entitlementId/contentId string.
    """
    ids = []
    seen = set()

    def walk(node):
        if isinstance(node, dict):
            content_id = _entry_content_id(node)
            if content_id and content_id not in seen:
                seen.add(content_id)
                ids.append(content_id)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return ids


def filter_out_known_content(payload, is_known):
    """Return a copy of payload with list entries for known content removed.

    ``is_known`` is called with each entitlementId/contentId string found in a
    dict that sits inside a list; entries it claims are dropped from that
    list. Returns ``(filtered_payload, removed_ids)``.
    """
    removed = []

    def walk(node):
        if isinstance(node, list):
            filtered = []
            for item in node:
                content_id = _entry_content_id(item)
                if content_id is not None and is_known(content_id):
                    removed.append(content_id)
                    continue
                filtered.append(walk(item))
            return filtered
        if isinstance(node, dict):
            return {key: walk(value) for key, value in node.items()}
        return node

    return walk(payload), removed
