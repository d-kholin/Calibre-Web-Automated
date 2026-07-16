#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2025 Calibre-Web contributors
# Copyright (C) 2024-2025 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""
Reading Services API for Kobo Annotations/Highlights
Handles annotation sync from Kobo devices

These routes are at the root level: /api/v3/..., /api/UserStorage/...

Stock Kobo firmware syncs annotations against this API and treats the
server's answers as authoritative: if the server doesn't know a book (which
is the case for every CWA book when the device talks to Kobo's real
readingservices.kobo.com), the device deletes its local annotations on the
next sync. CWA therefore answers annotation requests for its own books
locally - storing every uploaded annotation as a server-side backup
(ub.KoboAnnotation) and serving it back - and only forwards requests about
unknown books (Kobo store purchases) when the Kobo store proxy is enabled.
"""

import json
import os
import zipfile
import re
from datetime import datetime, timezone
from functools import wraps
from typing import TypedDict, NotRequired
from flask import Blueprint, request, make_response, jsonify, abort
from werkzeug.datastructures import Headers
import requests
from lxml import etree

from . import logger, calibre_db, db, config, ub, csrf
from .cw_login import current_user, login_required
from .services import hardcover
from kobo_annotations_utils import (
    extract_annotation_fields,
    extract_content_ids,
    filter_out_known_content,
)

log = logger.create()

# Create blueprints to handle the relevant reading services API routes
readingservices_api_v3 = Blueprint("readingservices_api_v3", __name__, url_prefix="/api/v3")
readingservices_userstorage = Blueprint("readingservices_userstorage", __name__, url_prefix="/api/UserStorage")

KOBO_READING_SERVICES_URL = "https://readingservices.kobo.com"

# Constants for annotation processing
MAX_PROGRESS_PERCENTAGE = 100  # Cap progress at 100%
SYNC_CHECK_BATCH_SIZE = 50  # Batch size for checking existing syncs
REQUEST_TIMEOUT = (2, 10)  # (connect, read) timeouts in seconds

CONNECTION_SPECIFIC_HEADERS = [
    "connection",
    "content-encoding",
    "content-length",
    "transfer-encoding",
]

def redact_headers(headers):
    """Redact sensitive headers from the headers dictionary.
    
    Returns a new dictionary with sensitive headers redacted to avoid
    mutating the original headers object.
    """
    redacted = dict(headers)
    for sensitive_header in ['Authorization', 'x-kobo-userkey', 'Cookie', 'Set-Cookie']:
        if sensitive_header in redacted:
            redacted[sensitive_header] = '***REDACTED***'
    return redacted


def proxy_to_kobo_reading_services(override_body=None):
    """Proxy the request to Kobo's reading services API.

    If override_body is given it replaces the original request body (used to
    forward a filtered checkforchanges payload).
    """
    try:
        kobo_url = KOBO_READING_SERVICES_URL + request.path
        if request.query_string:
            kobo_url += "?" + request.query_string.decode('utf-8')

        log.debug(f"Proxying {request.method} to Kobo Reading Services: {kobo_url}")

        # Forward headers (including Authorization, x-kobo-userkey, etc.)
        outgoing_headers = Headers(request.headers)
        outgoing_headers.remove("Host")
        # Remove CWA session cookie - Kobo doesn't need it and it causes issues
        outgoing_headers.pop("Cookie", None)
        if override_body is not None:
            # requests recomputes the length of the replacement body
            outgoing_headers.pop("Content-Length", None)

        readingservices_response = requests.request(
            method=request.method,
            url=kobo_url,
            headers=outgoing_headers,
            data=request.get_data() if override_body is None else override_body,
            allow_redirects=False,
            timeout=(2, 10)
        )
        
        if readingservices_response.status_code >= 400:
            log.warning(f"Kobo Reading Services error {readingservices_response.status_code}")
            log.warning(f"Response body: {readingservices_response.text[:5000]}")
            log.warning(f"Response headers: {redact_headers(dict(readingservices_response.headers))}")
        
        response_headers = readingservices_response.headers
        for header_key in CONNECTION_SPECIFIC_HEADERS:
            response_headers.pop(header_key, default=None)
        
        return make_response(
            readingservices_response.content, readingservices_response.status_code, response_headers.items()
        )
    except requests.exceptions.Timeout:
        log.error("Timeout connecting to Kobo Reading Services")
        return make_response(jsonify({"error": "Gateway timeout"}), 504)
    except requests.exceptions.ConnectionError as e:
        log.error(f"Connection error to Kobo Reading Services: {e}")
        return make_response(jsonify({"error": "Bad gateway"}), 502)
    except requests.exceptions.RequestException as e:
        log.error(f"Request failed to Kobo Reading Services: {e}")
        return make_response(jsonify({"error": "Bad gateway"}), 502)
    except Exception as e:
        log.error(f"Unexpected error proxying to Kobo Reading Services: {e}")
        import traceback
        log.error(traceback.format_exc())
        return make_response(jsonify({"error": "Internal server error"}), 500)


def proxy_or_empty_response():
    """Proxy to Kobo when the store proxy is enabled, otherwise answer with an
    empty JSON object.

    Used for requests CWA doesn't handle itself. Without the store proxy the
    device has no Kobo account relationship, so forwarding its requests to
    Kobo's servers can only produce answers about content Kobo doesn't know —
    which is exactly what makes the device wipe its local annotations.
    """
    if config.config_kobo_proxy:
        return proxy_to_kobo_reading_services()
    return make_response(jsonify({}))


def requires_reading_services_config(f):
    """
    Config gate for Reading Services endpoints: devices should only be
    pointed at us while Kobo sync is enabled. Authentication is handled per
    endpoint, after the requested content has been identified — requests
    about CWA books must never fall through to Kobo's cloud (its answers
    make the device delete its local annotations), authenticated or not.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not config.config_kobo_sync:
            log.debug("Kobo sync disabled, not handling reading services request")
            if config.config_kobo_proxy:
                return proxy_to_kobo_reading_services()
            return make_response(jsonify({"error": "Kobo sync is disabled"}), 503)
        return f(*args, **kwargs)
    return decorated_function


def find_known_book_uuids(content_ids):
    """Return the subset of content_ids that are uuids of books in the CWA library."""
    known = set()
    if not content_ids:
        return known
    try:
        calibre_db.ensure_session()
        for i in range(0, len(content_ids), SYNC_CHECK_BATCH_SIZE):
            chunk = content_ids[i:i + SYNC_CHECK_BATCH_SIZE]
            for row in calibre_db.session.query(db.Books.uuid).filter(db.Books.uuid.in_(chunk)):
                known.add(row.uuid)
    except Exception as e:
        log.error(f"Error looking up content ids in library: {e}")
    return known


def store_annotation_changes(book_uuid, data):
    """Persist annotation changes uploaded by the device into the local store.

    Updated annotations are upserted with their full raw JSON so they can be
    served back verbatim; deleted ones are tombstoned (never hard-deleted) so
    the server-side backup survives device-side deletions. book_uuid is the
    Kobo entitlement id: the Calibre uuid for CWA books, the store id for
    Kobo store purchases.
    """
    updated_annotations = [a for a in (data.get("updatedAnnotations") or [])
                           if isinstance(a, dict) and a.get("id") and isinstance(a.get("id"), str)]
    deleted_ids = [annotation_id for annotation_id in (data.get("deletedAnnotationIds") or [])
                   if isinstance(annotation_id, str)]

    all_ids = [a["id"] for a in updated_annotations] + deleted_ids
    if not all_ids:
        return

    existing = {}
    for i in range(0, len(all_ids), SYNC_CHECK_BATCH_SIZE):
        chunk = all_ids[i:i + SYNC_CHECK_BATCH_SIZE]
        for row in ub.session.query(ub.KoboAnnotation).filter(
                ub.KoboAnnotation.user_id == current_user.id,
                ub.KoboAnnotation.annotation_id.in_(chunk)):
            existing[row.annotation_id] = row

    now = datetime.now(timezone.utc)
    stored = tombstoned = 0
    for annotation in updated_annotations:
        annotation_id = annotation["id"]
        fields = extract_annotation_fields(annotation)
        row = existing.get(annotation_id)
        if not row:
            row = ub.KoboAnnotation(user_id=current_user.id, book_uuid=book_uuid,
                                    annotation_id=annotation_id)
            ub.session.add(row)
            existing[annotation_id] = row
        row.book_uuid = book_uuid
        row.annotation_type = fields["annotation_type"]
        row.highlighted_text = fields["highlighted_text"]
        row.note_text = fields["note_text"]
        row.highlight_color = fields["highlight_color"]
        row.client_last_modified = fields["client_last_modified"]
        row.raw_data = json.dumps(annotation)
        row.deleted = False
        row.last_modified = now
        stored += 1

    for annotation_id in deleted_ids:
        row = existing.get(annotation_id)
        if row and not row.deleted:
            row.deleted = True
            row.last_modified = now
            tombstoned += 1

    try:
        ub.session_commit()
        log.info(f"Kobo annotations for content {book_uuid}: stored {stored}, tombstoned {tombstoned}")
    except Exception as e:
        log.error(f"Failed to store Kobo annotations for content {book_uuid}: {e}")
        ub.session.rollback()


def get_stored_annotations_response(book):
    """Build the annotations payload for a book from the local store."""
    rows = ub.session.query(ub.KoboAnnotation).filter(
        ub.KoboAnnotation.user_id == current_user.id,
        ub.KoboAnnotation.book_uuid == str(book.uuid),
        ub.KoboAnnotation.deleted == False).all()
    annotations = []
    for row in rows:
        try:
            annotations.append(json.loads(row.raw_data))
        except (TypeError, ValueError) as e:
            log.warning(f"Skipping stored annotation {row.annotation_id} with unreadable data: {e}")
    return {"annotations": annotations}


def get_book_by_entitlement_id(entitlement_id):
    """Get book from database by UUID (entitlement_id)."""
    try:
        book = calibre_db.get_book_by_uuid(entitlement_id)
        return book
    except Exception as e:
        log.error(f"Error getting book by entitlement ID {entitlement_id}: {e}")
        return None


def get_book_identifiers(book):
    """Extract relevant identifiers from book."""
    identifiers = {}
    if book and book.identifiers:
        for identifier in book.identifiers:
            id_type = identifier.type.lower()
            if id_type in ['hardcover-id', 'hardcover-edition', 'hardcover-slug', 'isbn']:
                identifiers[id_type] = identifier.val
    return identifiers


def log_annotation_data(entitlement_id, method, data=None):
    """Log annotation data and link to book identifiers."""
    log.debug(f"ANNOTATION {method}")
    log.debug(f"Entitlement ID: {entitlement_id}")
    log.debug(f"User: {current_user.name}")
    
    # Try to link to book
    book = get_book_by_entitlement_id(entitlement_id)
    if book:
        log.debug(f"Book: {book.title}")
        log.debug(f"Book ID: {book.id}")
        
        # Log identifiers
        if book.identifiers:
            log.debug("Identifiers:")
            for identifier in book.identifiers:
                log.debug(f"  {identifier.type}: {identifier.val}")
    else:
        log.warning(f"Could not find book for entitlement ID: {entitlement_id}")
    
    if data:
        log.debug("Annotation Data:")
        log.debug(json.dumps(data, indent=2))


class EpubProgressCalculator:
    """
    Helper class to calculate progress from EPUB/KEPUB files efficiently.
    Parses the book structure once and reuses it for multiple calculations.
    """
    def __init__(self, book: db.Books):
        self.book = book
        self.spine_items: list[str] = []
        self.chapter_lengths: list[int] = []
        self.total_chars = 0
        self.initialized = False
        self.error = False

    def _initialize(self):
        if self.initialized:
            return

        if not self.book or not self.book.path:
            self.error = True
            return

        book_data = None
        kepub_datas = [data for data in self.book.data if data.format.lower() == 'kepub']
        if len(kepub_datas) >= 1:
            book_data = kepub_datas[0]
        else:
            epub_datas = [data for data in self.book.data if data.format.lower() == 'epub']
            if len(epub_datas) >= 1:
                book_data = epub_datas[0]
        
        if not book_data:
            self.error = True
            return

        try:
            file_path = os.path.normpath(os.path.join(
                config.get_book_path(),
                self.book.path,
                book_data.name + "." + book_data.format.lower()
            ))
            
            if not os.path.exists(file_path):
                self.error = True
                return
            
            with zipfile.ZipFile(file_path, 'r') as epub_zip:
                # Find OPF
                container_data = epub_zip.read('META-INF/container.xml')
                container_tree = etree.fromstring(container_data)
                ns = {
                    'container': 'urn:oasis:names:tc:opendocument:xmlns:container',
                    'opf': 'http://www.idpf.org/2007/opf'
                }
                opf_path = container_tree.xpath(
                    '//container:rootfile/@full-path',
                    namespaces={'container': ns['container']}
                )[0]
                
                # Parse OPF
                opf_data = epub_zip.read(opf_path)
                opf_tree = etree.fromstring(opf_data)
                opf_dir = os.path.dirname(opf_path)
                
                # Get manifest
                manifest = {}
                for item in opf_tree.xpath('//opf:manifest/opf:item', namespaces={'opf': ns['opf']}):
                    item_id = item.get('id')
                    href = item.get('href')
                    if item_id and href:
                        full_href = os.path.normpath(os.path.join(opf_dir, href)).replace('\\', '/')
                        manifest[item_id] = full_href
                
                # Get spine
                for itemref in opf_tree.xpath('//opf:spine/opf:itemref', namespaces={'opf': ns['opf']}):
                    idref = itemref.get('idref')
                    if idref and idref in manifest:
                        self.spine_items.append(manifest[idref])
                
                if not self.spine_items:
                    self.error = True
                    return

                # Calculate lengths
                for spine_item in self.spine_items:
                    try:
                        content = epub_zip.read(spine_item).decode('utf-8', errors='ignore')
                        try:
                            html_tree = etree.fromstring(content.encode('utf-8'))
                            text_content = ''.join(html_tree.itertext())
                            char_count = len(text_content.strip())
                        except etree.XMLSyntaxError:
                            text_content = re.sub(r'<[^>]+>', '', content)
                            char_count = len(text_content.strip())
                        self.chapter_lengths.append(char_count)
                    except Exception:
                        self.chapter_lengths.append(0)
                
                self.total_chars = sum(self.chapter_lengths)
                self.initialized = True

        except Exception as e:
            log.error(f"Error initializing EPUB calculator: {e}")
            self.error = True

    def calculate(self, chapter_filename: str, chapter_progress: float):
        if not self.initialized:
            self._initialize()
        
        if self.error or self.total_chars == 0:
            return None

        normalized_chapter = chapter_filename.replace('\\', '/')
        target_chapter_index = None
        
        for idx, spine_item in enumerate(self.spine_items):
            if normalized_chapter in spine_item or spine_item.endswith(normalized_chapter):
                target_chapter_index = idx
                break
        
        if target_chapter_index is None:
            return None
        
        chars_before = sum(self.chapter_lengths[:target_chapter_index])
        chars_in_chapter = self.chapter_lengths[target_chapter_index]
        chars_read = chars_before + (chars_in_chapter * chapter_progress)
        
        return (chars_read / self.total_chars) * 100


class AnnotationSpan(TypedDict):
    """Kobo annotation span location data."""
    chapterFilename: str
    chapterProgress: float
    chapterTitle: str
    endChar: int
    endPath: str
    startChar: int
    startPath: str


class AnnotationLocation(TypedDict):
    """Kobo annotation location data."""
    span: AnnotationSpan


class KoboAnnotation(TypedDict):
    """Kobo annotation structure from Reading Services API."""
    clientLastModifiedUtc: str
    highlightColor: str
    highlightedText: NotRequired[str]
    id: str
    location: AnnotationLocation
    noteText: NotRequired[str]
    type: str  # "note" or "highlight"


def process_annotation_for_sync(
    annotation: KoboAnnotation, 
    book: db.Books, 
    identifiers, 
    progress_percent=None, 
    existing_syncs=None,
    progress_calculator: 'EpubProgressCalculator | None' = None,
    is_blacklisted: bool = False
):
    """
    Process a single annotation and sync to Hardcover if needed.
    
    Args:
        annotation: Annotation dict from Kobo
        book: Calibre book object
        identifiers: Book identifiers dict
        progress_percent: Optional overall book progress
        existing_syncs: Optional dict of {annotation_id: sync_record} for batch processing
    
    Returns:
        True if synced successfully, False otherwise
    """
    annotation_id = annotation.get('id')
    highlighted_text = annotation.get('highlightedText')
    note_text = annotation.get('noteText')
    highlight_color = annotation.get('highlightColor')

    # Skip if no text content
    if not highlighted_text and not note_text:
        log.warning("Skipping annotation with no text content")
        return False

    # Check if already synced
    existing_sync = None
    if not annotation_id:
        log.warning("Annotation ID is required for sync")
        return False

    if existing_syncs is not None:
        # Use pre-loaded sync records (batch processing)
        existing_sync = existing_syncs.get(annotation_id)
    else:
        # Fall back to individual query
        existing_sync = ub.session.query(ub.KoboAnnotationSync).filter(
            ub.KoboAnnotationSync.annotation_id == annotation_id,
            ub.KoboAnnotationSync.user_id == current_user.id
        ).first()
    
    if existing_sync and existing_sync.synced_to_hardcover and existing_sync.highlighted_text == highlighted_text and existing_sync.note_text == note_text and existing_sync.highlight_color == highlight_color:
        log.info(f"Annotation {annotation_id} already synced to Hardcover, skipping")
        return False
    
    if not current_user.hardcover_token:
        log.warning("User has no Hardcover token, skipping sync")
        return False

    if is_blacklisted:
        log.info(f"Skipping annotation sync for book {book.id} - blacklisted for annotations")
        return False
        
    progress_page = None
    chapter_filename = annotation.get('location', {}).get('span', {}).get('chapterFilename')
    chapter_progress = annotation.get('location', {}).get('span', {}).get('chapterProgress')
    
    if progress_percent is None and progress_calculator and chapter_filename:
        progress_percent = progress_calculator.calculate(chapter_filename, chapter_progress)
        if progress_percent is None:
             log.warning(f"Failed to calculate exact progress for annotation in book '{book.title}' (ID: {book.id}). Annotation will sync without progress data.")

    # Sync to Hardcover if enabled and user has valid token
    if (config.config_kobo_sync and
        config.config_hardcover_annotations_sync and
        bool(hardcover)):
        if identifiers:
            log.info(f"Syncing annotation to Hardcover with identifiers: {identifiers}")
            try:
                hardcover_client = hardcover.HardcoverClient(current_user.hardcover_token)
                result = None
                if existing_sync and existing_sync.synced_to_hardcover:
                    # existing but not the same as the previous entry so update it
                    result = hardcover_client.update_journal_entry(
                        journal_id=existing_sync.hardcover_journal_id,
                        note_text=note_text,
                        highlighted_text=highlighted_text
                    )
                else:
                    result = hardcover_client.add_journal_entry(
                        identifiers=identifiers,
                        note_text=note_text,
                        progress_percent=progress_percent,
                        progress_page=progress_page,
                        highlighted_text=highlighted_text
                    )
                
                if result:
                    # Track sync in database only after successful Hardcover sync
                    try:
                        if existing_sync:
                            existing_sync.synced_to_hardcover = True
                            existing_sync.hardcover_journal_id = result.get('id')
                            existing_sync.last_synced = datetime.now(timezone.utc)
                            existing_sync.highlighted_text = highlighted_text
                            existing_sync.note_text = note_text
                            existing_sync.highlight_color = highlight_color
                        else:
                            sync_record = ub.KoboAnnotationSync(
                                user_id=current_user.id,
                                annotation_id=annotation_id,
                                book_id=book.id,
                                synced_to_hardcover=True,
                                hardcover_journal_id=result.get('id'),
                                highlighted_text=highlighted_text,
                                note_text=note_text,
                                highlight_color=highlight_color
                            )
                            ub.session.add(sync_record)
                        ub.session_commit()
                        log.info(f"Successfully synced annotation {annotation_id} to Hardcover (journal ID: {result.get('id')})")
                        return True
                    except Exception as e:
                        log.error(f"Failed to save sync record for annotation {annotation_id}: {e}")
                        log.error(f"Hardcover journal entry {result.get('id')} was created but DB tracking failed - may cause duplicate on retry")
                        ub.session.rollback()
                        # Note: Hardcover sync succeeded but DB record failed
                        # On retry, this could create a duplicate journal entry on Hardcover
                        # TODO: Add cleanup task to reconcile orphaned journal entries
                        return False
                else:
                    log.warning(f"Failed to sync annotation {annotation_id} to Hardcover")
                    return False
            except Exception as e:
                log.error(f"Error syncing annotation to Hardcover: {e}")
                import traceback
                log.error(traceback.format_exc())
                return False
        else:
            log.info("No Hardcover identifiers found, skipping sync")
            return False
    


@csrf.exempt
@readingservices_api_v3.route("/content/<entitlement_id>/annotations", methods=["GET", "PATCH"])
@requires_reading_services_config
def handle_annotations(entitlement_id):
    """
    Handle annotation requests for a specific book.
    GET: Serve annotations for a CWA book from the local store
    PATCH: Store annotation changes locally (backup) and optionally sync to Hardcover

    Books unknown to CWA (e.g. Kobo store purchases) are proxied to Kobo,
    which stays authoritative for them; their uploads are still backed up
    locally when the user can be identified. Books known to CWA are always
    answered locally — even unauthenticated requests are never proxied,
    because Kobo's cloud doesn't know them and its answers would make the
    device delete its local annotations.
    """
    book = get_book_by_entitlement_id(entitlement_id)
    if not book:
        log.debug(f"Annotations request for entitlement {entitlement_id} unknown to CWA")
        if config.config_kobo_proxy:
            # Back up store-book annotation uploads too before forwarding
            if request.method == "PATCH" and current_user.is_authenticated:
                data = request.get_json(silent=True)
                if isinstance(data, dict):
                    try:
                        store_annotation_changes(entitlement_id, data)
                    except Exception as e:
                        log.error(f"Error storing annotations for content {entitlement_id}: {e}")
            return proxy_to_kobo_reading_services()
        return make_response(jsonify({"error": "Content not found"}), 404)

    # CWA book: requires a user to answer, and must never fall through to Kobo
    if not current_user.is_authenticated:
        log.debug(f"Unauthenticated annotations request for CWA book {entitlement_id}, returning 401")
        return make_response(jsonify({"error": "Unauthorized"}), 401)

    if request.method == "GET":
        return make_response(jsonify(get_stored_annotations_response(book)))

    try:
        data = request.get_json()
    except Exception:
        data = None
    if not isinstance(data, dict):
        log.debug("Received malformed annotations PATCH request")
        return make_response(jsonify({"error": "Malformed request"}), 400)

    log_annotation_data(entitlement_id, "PATCH", data)

    # Always back the changes up locally, independent of Hardcover
    try:
        store_annotation_changes(str(book.uuid), data)
    except Exception as e:
        log.error(f"Error storing annotations for book {book.id}: {e}")

    # Optionally push the changes to Hardcover
    if config.config_hardcover_annotations_sync and bool(hardcover):
        try:
            identifiers = get_book_identifiers(book)

            if data and "deletedAnnotationIds" in data:
                deleted_ids = data["deletedAnnotationIds"]
                log.info(f"Processing {len(deleted_ids)} deleted annotation IDs")
                for annotation_id in deleted_ids:
                    sync_record = ub.session.query(ub.KoboAnnotationSync).filter(
                        ub.KoboAnnotationSync.annotation_id == annotation_id,
                        ub.KoboAnnotationSync.user_id == current_user.id
                    ).first()
                    if sync_record:
                        try:
                            hardcover_client = hardcover.HardcoverClient(current_user.hardcover_token)
                            deleted_id = hardcover_client.delete_journal_entry(journal_id=sync_record.hardcover_journal_id)
                            if deleted_id == sync_record.hardcover_journal_id:
                                try:
                                    ub.session.delete(sync_record)
                                    ub.session_commit()
                                    log.info(f"Successfully deleted journal entry {sync_record.hardcover_journal_id} from Hardcover and local DB")
                                except Exception as db_error:
                                    log.error(f"Failed to delete local sync record after Hardcover deletion succeeded: {db_error}")
                                    log.error(f"Annotation {annotation_id} deleted from Hardcover but DB record remains - manual cleanup may be needed")
                                    ub.session.rollback()
                            else:
                                log.warning(f"Failed to delete journal entry {sync_record.hardcover_journal_id} from Hardcover - keeping local record")
                        except Exception as api_error:
                            log.error(f"Error deleting annotation {annotation_id} from Hardcover: {api_error}")
                            # Don't delete local record if Hardcover deletion failed
                    else:
                        log.warning(f"Sync record not found for annotation {annotation_id}, skipping deletion")

            # Extract updated annotations
            if data and "updatedAnnotations" in data:
                annotations = data['updatedAnnotations']
                log.info(f"Processing {len(annotations)} updated annotations")

                # Batch load existing sync records to avoid N+1 queries
                existing_syncs = {}
                annotation_ids = [a.get('id') for a in annotations if a.get('id')]
                if annotation_ids:
                    syncs = ub.session.query(ub.KoboAnnotationSync).filter(
                        ub.KoboAnnotationSync.annotation_id.in_(annotation_ids),
                        ub.KoboAnnotationSync.user_id == current_user.id
                    ).all()
                    existing_syncs = {s.annotation_id: s for s in syncs}

                # Check blacklist once per book
                book_blacklist = ub.session.query(ub.HardcoverBookBlacklist).filter(
                    ub.HardcoverBookBlacklist.book_id == book.id
                ).first()
                is_blacklisted = book_blacklist and book_blacklist.blacklist_annotations

                # Initialize progress calculator once per book
                progress_calculator = EpubProgressCalculator(book)

                for annotation in annotations:
                    process_annotation_for_sync(
                        annotation=annotation,
                        book=book,
                        identifiers=identifiers,
                        existing_syncs=existing_syncs,
                        progress_calculator=progress_calculator,
                        is_blacklisted=is_blacklisted
                    )

        except Exception as e:
            log.error(f"Error processing PATCH annotations: {e}")
            import traceback
            log.error(traceback.format_exc())

    # The device only needs a success status; never forward CWA books to Kobo
    return make_response(jsonify({}))


@csrf.exempt
@readingservices_api_v3.route("/content/checkforchanges", methods=["POST"])
@requires_reading_services_config
def handle_check_for_changes():
    """
    Handle check for changes request.

    The device asks which content has server-side annotation changes. For
    books in the CWA library the answer is always "no changes" — a Kobo cloud
    answer for them would make the device delete its local annotations. With
    the store proxy enabled, entries for books unknown to CWA are still
    forwarded to Kobo so store purchases keep working. None of this needs the
    user's identity, so it applies to unauthenticated requests as well.
    """
    data = request.get_json(silent=True)
    log.debug(f"checkforchanges payload: {data}")

    if not config.config_kobo_proxy:
        # No upstream annotation state exists: report "no changes"
        return make_response(jsonify({}))

    if data is None:
        return proxy_to_kobo_reading_services()

    known_uuids = find_known_book_uuids(extract_content_ids(data))
    if not known_uuids:
        return proxy_to_kobo_reading_services()
    filtered, removed = filter_out_known_content(data, lambda cid: cid in known_uuids)
    if not extract_content_ids(filtered):
        # Request was only about CWA books; nothing to ask Kobo
        log.debug(f"checkforchanges only referenced CWA books ({len(removed)}), answering no changes")
        return make_response(jsonify({}))
    log.debug(f"checkforchanges: forwarding to Kobo without {len(removed)} CWA book(s)")
    return proxy_to_kobo_reading_services(override_body=json.dumps(filtered).encode("utf-8"))


@csrf.exempt
@readingservices_userstorage.route("/<path:subpath>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
@requires_reading_services_config
def handle_user_storage(subpath):
    """
    Handle UserStorage API requests (e.g., /api/UserStorage/Metadata).
    Proxies to Kobo's reading services when the store proxy is enabled.
    """
    return proxy_or_empty_response()


@csrf.exempt
@readingservices_api_v3.route("/<path:subpath>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
@requires_reading_services_config
def handle_unknown_reading_service_request(subpath):
    """
    Catch-all handler for any reading services requests not explicitly handled.
    Proxies to Kobo's reading services when the store proxy is enabled.
    """
    return proxy_or_empty_response()

