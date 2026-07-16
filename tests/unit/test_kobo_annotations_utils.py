# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Unit tests for the Kobo annotation store helpers"""

import pytest

from kobo_annotations_utils import (
    extract_annotation_fields,
    extract_content_ids,
    filter_out_known_content,
)


FULL_ANNOTATION = {
    "id": "e0f8f9c2-0000-4000-8000-000000000001",
    "type": "highlight",
    "clientLastModifiedUtc": "2026-07-01T10:00:00Z",
    "highlightColor": "yellow",
    "highlightedText": "Some highlighted passage",
    "noteText": "A note",
    "location": {
        "span": {
            "chapterFilename": "OEBPS/chapter1.xhtml",
            "chapterProgress": 0.5,
            "chapterTitle": "Chapter 1",
            "startChar": 10,
            "startPath": "span#kobo\\.1\\.1",
            "endChar": 42,
            "endPath": "span#kobo\\.1\\.2",
        }
    },
}


@pytest.mark.unit
class TestExtractAnnotationFields:
    def test_full_annotation(self):
        fields = extract_annotation_fields(FULL_ANNOTATION)
        assert fields == {
            "annotation_id": "e0f8f9c2-0000-4000-8000-000000000001",
            "annotation_type": "highlight",
            "highlighted_text": "Some highlighted passage",
            "note_text": "A note",
            "highlight_color": "yellow",
            "client_last_modified": "2026-07-01T10:00:00Z",
        }

    def test_minimal_annotation(self):
        fields = extract_annotation_fields({"id": "abc"})
        assert fields["annotation_id"] == "abc"
        assert fields["annotation_type"] is None
        assert fields["highlighted_text"] is None

    def test_non_dict_input(self):
        fields = extract_annotation_fields(None)
        assert fields["annotation_id"] is None


@pytest.mark.unit
class TestExtractContentIds:
    def test_list_of_entries(self):
        payload = [
            {"entitlementId": "uuid-1", "etag": "x"},
            {"entitlementId": "uuid-2"},
        ]
        assert extract_content_ids(payload) == ["uuid-1", "uuid-2"]

    def test_nested_and_case_insensitive(self):
        payload = {"contents": [{"EntitlementId": "uuid-1"}, {"content_id": "uuid-2"}]}
        assert extract_content_ids(payload) == ["uuid-1", "uuid-2"]

    def test_deduplicates(self):
        payload = [{"entitlementId": "uuid-1"}, {"entitlementId": "uuid-1"}]
        assert extract_content_ids(payload) == ["uuid-1"]

    def test_ignores_non_string_values_and_unrelated_keys(self):
        payload = [{"entitlementId": 5}, {"name": "uuid-1"}, "bare-string"]
        assert extract_content_ids(payload) == []

    def test_empty_payloads(self):
        assert extract_content_ids(None) == []
        assert extract_content_ids({}) == []
        assert extract_content_ids([]) == []


@pytest.mark.unit
class TestFilterOutKnownContent:
    def test_removes_known_entries_from_lists(self):
        payload = {"contents": [
            {"entitlementId": "known-1", "etag": "a"},
            {"entitlementId": "unknown-1", "etag": "b"},
        ]}
        filtered, removed = filter_out_known_content(payload, lambda cid: cid.startswith("known"))
        assert removed == ["known-1"]
        assert filtered == {"contents": [{"entitlementId": "unknown-1", "etag": "b"}]}

    def test_top_level_list(self):
        payload = [{"entitlementId": "known-1"}, {"entitlementId": "unknown-1"}]
        filtered, removed = filter_out_known_content(payload, lambda cid: cid == "known-1")
        assert removed == ["known-1"]
        assert filtered == [{"entitlementId": "unknown-1"}]

    def test_nothing_known_keeps_payload_intact(self):
        payload = {"contents": [{"entitlementId": "unknown-1"}], "other": 1}
        filtered, removed = filter_out_known_content(payload, lambda cid: False)
        assert removed == []
        assert filtered == payload

    def test_preserves_non_dict_list_items(self):
        payload = {"ids": ["bare-string", 42], "contents": [{"entitlementId": "known-1"}]}
        filtered, removed = filter_out_known_content(payload, lambda cid: True)
        assert removed == ["known-1"]
        assert filtered == {"ids": ["bare-string", 42], "contents": []}
