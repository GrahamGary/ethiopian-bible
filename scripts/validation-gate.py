#!/usr/bin/env python3
"""Strict lock, validation, recovery, and validated-only publication gate."""

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
AUTOMATION = ROOT / ".automation"
LOCK = AUTOMATION / "PRODUCTION_LOCK.json"
STATE = AUTOMATION / "VALIDATED_STATE.json"
READY = AUTOMATION / "PUBLISH_READY.json"
TRANSACTION = AUTOMATION / "FINALIZE_TRANSACTION.json"
PUBLISH_LOCK = AUTOMATION / "PUBLISH_LOCK.json"
PUBLICATION_STATE = AUTOMATION / "PUBLICATION_STATE.json"
PUBLICATION_PENDING = AUTOMATION / "PUBLICATION_STATE.pending.json"
MANIFEST = ROOT / "content-manifest.json"
PROGRESS = ROOT / "PROGRESS.md"
INDEX = ROOT / "index.html"
SERVICE_WORKER = ROOT / "sw.js"
STALE_SECONDS = 6 * 60 * 60
CHAPTER_FILE_RE = re.compile(r"^(?P<book>[a-z0-9-]+)-(?P<chapter>\d{3})\.json$")
REQUIRED_CHECKS = (
    "source",
    "structure",
    "json",
    "manifest",
    "rendering",
    "search",
    "navigation",
    "mobileLayout",
    "previousContentIntegrity",
)


class GateError(RuntimeError):
    pass


def fail(message, code=1):
    print(f"VALIDATION GATE: FAIL — {message}", file=sys.stderr)
    raise SystemExit(code)


def utc_now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso_now():
    return utc_now().isoformat().replace("+00:00", "Z")


def parse_iso(value):
    if not isinstance(value, str):
        raise GateError("timestamp is missing")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise GateError(f"invalid ISO timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise GateError(f"timestamp has no timezone: {value}")
    return parsed.astimezone(dt.timezone.utc)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise GateError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path):
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle, object_pairs_hook=unique_object)
    except FileNotFoundError as exc:
        raise GateError(f"required file is missing: {relative(path)}") from exc
    except json.JSONDecodeError as exc:
        raise GateError(f"invalid JSON in {relative(path)}: {exc}") from exc


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def relative(path):
    return path.resolve().relative_to(ROOT).as_posix()


def safe_path(raw):
    if not isinstance(raw, str) or not raw or raw.startswith("/"):
        raise GateError(f"unsafe repository path: {raw!r}")
    path = (ROOT / raw).resolve()
    try:
        path.relative_to(ROOT)
    except ValueError as exc:
        raise GateError(f"path escapes repository: {raw}") from exc
    return path


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def require_text(value, label, minimum=1):
    if not isinstance(value, str) or len(value.strip()) < minimum:
        raise GateError(f"{label} is missing or too short")


def validate_source(chapter):
    note = chapter.get("sourceNote")
    require_text(note, "sourceNote", 80)
    if not re.search(r"Aramaic|Greek|Hebrew|Ethiopic|Ge[ʽ']ez|Syriac|Coptic|Latin", note, re.I):
        raise GateError("sourceNote does not name a primary biblical-language witness")
    if not re.search(r"no modern English translation|not copied from a modern published English translation", note, re.I):
        raise GateError("sourceNote does not exclude use of a modern English translation base")
    if re.search(r"reconstruct(?:ed|ion)?|guess(?:ed|ing)?|invent(?:ed|ion)?", note, re.I) and not re.search(
        r"without (?:invent|reconstruct)|not (?:invent|reconstruct)|no (?:invent|reconstruct)", note, re.I
    ):
        raise GateError("sourceNote suggests guessed or reconstructed Scripture")


def validate_payload(path, expected_book=None, expected_chapter=None):
    payload = read_json(path)
    if payload.get("format") != "ethiopian-bible-update" or payload.get("schemaVersion") != 1:
        raise GateError(f"{relative(path)} has the wrong update format or schema")
    require_text(payload.get("updateId"), f"{relative(path)} updateId")
    require_text(payload.get("contentVersion"), f"{relative(path)} contentVersion")
    books = payload.get("books")
    chapters = payload.get("chapters")
    if not isinstance(books, list) or len(books) != 1:
        raise GateError(f"{relative(path)} must contain exactly one book record")
    if not isinstance(chapters, list) or len(chapters) != 1:
        raise GateError(f"{relative(path)} must contain exactly one chapter record")
    book = books[0]
    chapter = chapters[0]
    if not isinstance(book, dict) or not isinstance(chapter, dict):
        raise GateError(f"{relative(path)} book/chapter record is invalid")
    book_id = chapter.get("bookId")
    chapter_number = chapter.get("chapter")
    if not isinstance(book_id, str) or not book_id:
        raise GateError(f"{relative(path)} chapter bookId is invalid")
    if type(chapter_number) is not int or chapter_number < 1:
        raise GateError(f"{relative(path)} chapter number is invalid")
    if expected_book is not None and book_id != expected_book:
        raise GateError(f"{relative(path)} bookId does not match {expected_book}")
    if expected_chapter is not None and chapter_number != expected_chapter:
        raise GateError(f"{relative(path)} chapter does not match {expected_chapter}")
    filename_match = CHAPTER_FILE_RE.fullmatch(path.name)
    if not filename_match or filename_match.group("book") != book_id or int(filename_match.group("chapter")) != chapter_number:
        raise GateError(f"{relative(path)} filename does not match its chapter identity")
    if book.get("id") != book_id or book.get("title") != chapter.get("bookTitle"):
        raise GateError(f"{relative(path)} book metadata disagrees with the chapter")
    total = book.get("totalChapterCount")
    if type(total) is not int or total < chapter_number:
        raise GateError(f"{relative(path)} totalChapterCount is invalid")
    require_text(chapter.get("bookTitle"), "bookTitle")
    require_text(chapter.get("title"), "chapter title")
    validate_source(chapter)
    verses = chapter.get("verses")
    if not isinstance(verses, list) or not verses:
        raise GateError(f"{relative(path)} has no verses")
    actual_numbers = []
    for verse in verses:
        if not isinstance(verse, dict) or type(verse.get("verse")) is not int:
            raise GateError(f"{relative(path)} has an invalid verse record")
        actual_numbers.append(verse["verse"])
        require_text(verse.get("text"), f"chapter {chapter_number} verse {verse['verse']} text")
        require_text(verse.get("study"), f"chapter {chapter_number} verse {verse['verse']} study")
        refs = verse.get("crossReferences")
        if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or not ref.strip() for ref in refs):
            raise GateError(f"chapter {chapter_number} verse {verse['verse']} Biblical Connections are invalid")
    expected_numbers = list(range(1, len(verses) + 1))
    if actual_numbers != expected_numbers:
        raise GateError(f"{relative(path)} verse numbering is not sequential from 1")
    explanation = chapter.get("explanation")
    if not isinstance(explanation, dict):
        raise GateError(f"{relative(path)} explanation is missing")
    require_text(explanation.get("heading"), "explanation heading")
    paragraphs = explanation.get("paragraphs")
    if not isinstance(paragraphs, list) or not paragraphs or any(not isinstance(p, str) or not p.strip() for p in paragraphs):
        raise GateError(f"{relative(path)} explanation paragraphs are invalid")
    chapter_refs = chapter.get("crossReferences")
    if not isinstance(chapter_refs, list) or not chapter_refs or any(not isinstance(ref, str) or not ref.strip() for ref in chapter_refs):
        raise GateError(f"{relative(path)} chapter Biblical Connections are invalid")
    return payload, book, chapter


def validate_app_contracts():
    try:
        html = INDEX.read_text(encoding="utf-8")
        sw = SERVICE_WORKER.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise GateError("application shell is missing") from exc
    contracts = {
        "rendering": (
            "function renderChapter(book, data)",
            'els.chapterContent.appendChild(frag)',
            'label.textContent = "Meaning & context"',
            'label.textContent = "Biblical connections"',
            "function validateChapterData(chapter)",
        ),
        "search": (
            "function searchCurrentChapter(query)",
            'querySelectorAll("[data-search]")',
            'els.readerSearch.addEventListener("input"',
        ),
        "navigation": (
            "async function openChapter(bookId, chapter)",
            "function renderChapterGrid(book, activeChapter)",
            "els.prevChapterButton.disabled",
            "els.nextChapterButton.disabled",
            "function checkForBibleUpdates()",
        ),
        "mobileLayout": (
            'name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=yes"',
            "@media (max-width: 850px)",
            "@media (max-width: 520px)",
            ".verse-line { grid-template-columns:",
            "env(safe-area-inset-bottom)",
        ),
    }
    for check_name, tokens in contracts.items():
        missing = [token for token in tokens if token not in html]
        if missing:
            raise GateError(f"{check_name} contract missing from index.html: {missing[0]}")
    sw_tokens = (
        'url.pathname.endsWith("/content-manifest.json")',
        'url.pathname.endsWith(".json")',
        'request.mode === "navigate"',
        'caches.match("./index.html")',
    )
    missing_sw = [token for token in sw_tokens if token not in sw]
    if missing_sw:
        raise GateError(f"offline/render delivery contract missing from sw.js: {missing_sw[0]}")
    if html.count("<script") != html.count("</script>"):
        raise GateError("index.html script tags are unbalanced")
    if html.count("{") != html.count("}"):
        raise GateError("index.html brace balance is invalid")


def parse_progress(text=None):
    if text is None:
        try:
            text = PROGRESS.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise GateError("PROGRESS.md is missing") from exc
    patterns = {
        "last": r"^Last completed:\s+(.+?)\s+(\d+)\s*$",
        "next": r"^Next chapter:\s+(.+?)\s+(\d+)\s*$",
        "status": r"^Current book status:\s+(\d+)\s+/\s+(\d+) chapters completed\s*$",
        "overall": r"^Overall project status:\s+(.+?)\s+—\s+(\d+) of (\d+) chapters completed;\s+(.+)$",
    }
    values = {}
    for name, pattern in patterns.items():
        matches = re.findall(pattern, text, re.MULTILINE)
        if len(matches) != 1:
            raise GateError(f"PROGRESS.md must contain exactly one valid {name} record")
        values[name] = matches[0]
    last_book, last_chapter = values["last"]
    next_book, next_chapter = values["next"]
    current_done, current_total = map(int, values["status"])
    overall_book, overall_done, overall_total, overall_tail = values["overall"]
    overall_done, overall_total = int(overall_done), int(overall_total)
    if last_book != next_book or last_book != overall_book:
        raise GateError("PROGRESS.md book names disagree")
    if int(last_chapter) != current_done or current_done != overall_done:
        raise GateError("PROGRESS.md completed chapter values disagree")
    if int(next_chapter) != current_done + 1:
        raise GateError("PROGRESS.md next chapter is not sequential")
    if current_total != overall_total:
        raise GateError("PROGRESS.md total chapter values disagree")
    return {
        "lastBook": last_book,
        "lastChapter": int(last_chapter),
        "nextBook": next_book,
        "nextChapter": int(next_chapter),
        "total": current_total,
        "overallTail": overall_tail,
    }


def validate_manifest(manifest=None):
    if manifest is None:
        manifest = read_json(MANIFEST)
    if manifest.get("format") != "ethiopian-bible-online-manifest" or manifest.get("schemaVersion") != 1:
        raise GateError("content-manifest.json has the wrong format or schema")
    parse_iso(manifest.get("updatedAt"))
    books = manifest.get("books")
    updates = manifest.get("updates")
    if not isinstance(books, list) or not books or not isinstance(updates, list):
        raise GateError("manifest books or updates are invalid")
    book_map = {}
    for book in books:
        if not isinstance(book, dict) or not book.get("id") or book["id"] in book_map:
            raise GateError("manifest has invalid or duplicate books")
        book_map[book["id"]] = book
    ids = set()
    pairs = set()
    by_book = {book_id: [] for book_id in book_map}
    for item in updates:
        if not isinstance(item, dict):
            raise GateError("manifest update entry is invalid")
        update_id = item.get("id")
        pair = (item.get("bookId"), item.get("chapter"))
        if not isinstance(update_id, str) or not update_id or update_id in ids:
            raise GateError("manifest update IDs are invalid or duplicated")
        if pair in pairs or pair[0] not in book_map or type(pair[1]) is not int:
            raise GateError("manifest chapter identity is invalid or duplicated")
        ids.add(update_id)
        pairs.add(pair)
        path = safe_path(item.get("url"))
        payload, _, chapter = validate_payload(path, pair[0], pair[1])
        if payload.get("updateId") != update_id or payload.get("contentVersion") != item.get("contentVersion"):
            raise GateError(f"manifest metadata disagrees with {relative(path)}")
        if chapter.get("chapter") != pair[1]:
            raise GateError(f"manifest chapter disagrees with {relative(path)}")
        by_book[pair[0]].append(pair[1])
    for book_id, book in book_map.items():
        built_in = book.get("builtInThroughChapter", 0)
        total = book.get("totalChapterCount")
        if type(built_in) is not int or built_in < 0 or type(total) is not int or total < built_in:
            raise GateError(f"manifest book metadata is invalid for {book_id}")
        chapters = sorted(by_book[book_id])
        if chapters:
            expected = list(range(built_in + 1, max(chapters) + 1))
            if chapters != expected:
                raise GateError(f"manifest discovery sequence has a gap or reorder for {book_id}")
    if not updates:
        raise GateError("manifest contains no validated update")
    last = updates[-1]
    if manifest.get("contentVersion") != last.get("contentVersion"):
        raise GateError("manifest contentVersion does not match its last update")
    return manifest, book_map, last


def validate_progress_manifest(progress_text=None, manifest=None):
    progress = parse_progress(progress_text)
    manifest, book_map, last = validate_manifest(manifest)
    book = book_map[last["bookId"]]
    if progress["lastBook"] != book.get("title") or progress["lastChapter"] != last["chapter"]:
        raise GateError("PROGRESS.md and manifest disagree on the last validated chapter")
    if progress["nextBook"] != book.get("title") or progress["nextChapter"] != last["chapter"] + 1:
        raise GateError("PROGRESS.md and manifest disagree on the next unfinished chapter")
    if progress["total"] != book.get("totalChapterCount"):
        raise GateError("PROGRESS.md and manifest disagree on the book total")
    return progress, manifest, book, last


def protected_entries(last):
    entries = [
        {"path": "index.html", "sha256": sha256(INDEX), "scope": "application shell and built-in Chapters 1-3"},
        {"path": "sw.js", "sha256": sha256(SERVICE_WORKER), "scope": "offline and update delivery"},
    ]
    manifest, book_map, _ = validate_manifest()
    for item in manifest["updates"]:
        if item["bookId"] == last["bookId"] and item["chapter"] <= last["chapter"]:
            path = safe_path(item["url"])
            entries.append({"path": relative(path), "sha256": sha256(path), "scope": "validated Scripture"})
    return entries


def validate_state(expected_last=None):
    state = read_json(STATE)
    if state.get("format") != "ethiopian-bible-validated-state" or state.get("schemaVersion") != 1:
        raise GateError("VALIDATED_STATE.json has the wrong format or schema")
    last = state.get("lastValidated")
    next_item = state.get("nextUnfinished")
    if not isinstance(last, dict) or not isinstance(next_item, dict):
        raise GateError("VALIDATED_STATE.json chapter markers are invalid")
    if expected_last and (last.get("bookId"), last.get("chapter")) != expected_last:
        raise GateError("VALIDATED_STATE.json has the wrong last validated chapter")
    if (next_item.get("bookId"), next_item.get("chapter")) != (last.get("bookId"), last.get("chapter") + 1):
        raise GateError("VALIDATED_STATE.json next chapter is not sequential")
    entries = state.get("protectedFiles")
    if not isinstance(entries, list) or not entries:
        raise GateError("VALIDATED_STATE.json has no protected files")
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("path") in seen:
            raise GateError("VALIDATED_STATE.json has an invalid or duplicate protected file")
        seen.add(entry.get("path"))
        path = safe_path(entry.get("path"))
        expected_hash = entry.get("sha256")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_hash or "") or sha256(path) != expected_hash:
            raise GateError(f"previous validated content changed: {entry.get('path')}")
    return state


def validate_ready(expected_last=None):
    ready = read_json(READY)
    if ready.get("format") != "ethiopian-bible-publish-ready" or ready.get("schemaVersion") != 1:
        raise GateError("PUBLISH_READY.json has the wrong format or schema")
    if ready.get("status") != "VALIDATED":
        raise GateError("PUBLISH_READY.json is not marked VALIDATED")
    parse_iso(ready.get("validatedAt"))
    last = ready.get("lastValidated")
    next_item = ready.get("nextUnfinished")
    if not isinstance(last, dict) or not isinstance(next_item, dict):
        raise GateError("PUBLISH_READY.json chapter markers are invalid")
    if expected_last and (last.get("bookId"), last.get("chapter")) != expected_last:
        raise GateError("PUBLISH_READY.json has the wrong last validated chapter")
    if (next_item.get("bookId"), next_item.get("chapter")) != (last.get("bookId"), last.get("chapter") + 1):
        raise GateError("PUBLISH_READY.json next chapter is not sequential")
    checks = ready.get("checks")
    if not isinstance(checks, dict) or any(checks.get(name) != "PASSED" for name in REQUIRED_CHECKS):
        raise GateError("PUBLISH_READY.json does not record every required check as PASSED")
    files = ready.get("files")
    if not isinstance(files, list) or not files:
        raise GateError("PUBLISH_READY.json has no validated files")
    seen = set()
    for entry in files:
        if not isinstance(entry, dict) or entry.get("path") in seen:
            raise GateError("PUBLISH_READY.json has an invalid or duplicate file")
        path_text = entry.get("path")
        seen.add(path_text)
        path = safe_path(path_text)
        if path == READY:
            if entry.get("validation") != "self" or "sha256" in entry:
                raise GateError("PUBLISH_READY.json must use self-validation for its own READY entry")
            continue
        expected_hash = entry.get("sha256")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_hash or "") or sha256(path) != expected_hash:
            raise GateError(f"READY file hash mismatch: {path_text}")
    if relative(READY) not in seen:
        raise GateError("PUBLISH_READY.json does not list itself as publication control")
    return ready


def validate_publication_state():
    if not PUBLICATION_STATE.exists():
        return None
    state = read_json(PUBLICATION_STATE)
    if state.get("format") != "ethiopian-bible-publication-state" or state.get("schemaVersion") != 1:
        raise GateError("PUBLICATION_STATE.json has the wrong format or schema")
    if state.get("status") != "PUBLISHED" or state.get("remote") != "origin" or state.get("branch") != "main":
        raise GateError("PUBLICATION_STATE.json does not record a successful origin/main publication")
    parse_iso(state.get("publishedAt"))
    commit = state.get("commit")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        raise GateError("PUBLICATION_STATE.json commit is invalid")
    last = state.get("lastPublished")
    if (
        not isinstance(last, dict)
        or not isinstance(last.get("bookId"), str)
        or not isinstance(last.get("book"), str)
        or type(last.get("chapter")) is not int
        or last["chapter"] < 1
    ):
        raise GateError("PUBLICATION_STATE.json lastPublished marker is invalid")
    files = state.get("files")
    if not isinstance(files, list) or not files:
        raise GateError("PUBLICATION_STATE.json published file list is invalid")
    return state


def validate_repository_records():
    validate_app_contracts()
    progress, manifest, book, last = validate_progress_manifest()
    expected = (last["bookId"], last["chapter"])
    state = validate_state(expected)
    ready = validate_ready(expected)
    markers = (last["bookId"], last["chapter"], last["chapter"] + 1)
    state_markers = (state["lastValidated"]["bookId"], state["lastValidated"]["chapter"], state["nextUnfinished"]["chapter"])
    ready_markers = (ready["lastValidated"]["bookId"], ready["lastValidated"]["chapter"], ready["nextUnfinished"]["chapter"])
    if state_markers != markers or ready_markers != markers:
        raise GateError("progress, manifest, validated state, and publishing record disagree")
    return progress, manifest, book, last, state, ready


def build_progress(book_title, chapter, total):
    return (
        "# Ethiopian Bible Production Progress\n\n"
        f"Last completed: {book_title} {chapter}\n"
        f"Next chapter: {book_title} {chapter + 1}\n"
        f"Current book status: {chapter} / {total} chapters completed\n"
        f"Overall project status: {book_title} — {chapter} of {total} chapters completed; "
        "complete Ethiopian Bible project in progress\n"
    ).encode("utf-8")


def file_role(path):
    if CHAPTER_FILE_RE.fullmatch(path):
        return "scripture"
    if path in ("content-manifest.json", "PROGRESS.md"):
        return "publication-metadata"
    if path.startswith(".automation/"):
        return "validation-infrastructure"
    if path.startswith("scripts/") or path == ".gitignore":
        return "automation-infrastructure"
    return "repository-infrastructure"


def file_entries(paths):
    result = []
    for raw in sorted(set(paths)):
        if safe_path(raw) == READY:
            result.append({"path": raw, "validation": "self", "role": "publication-control-record"})
            continue
        path = safe_path(raw)
        if not path.is_file():
            raise GateError(f"READY path is not a file: {raw}")
        result.append({"path": raw, "sha256": sha256(path), "role": file_role(raw)})
    return result


def initialize(args):
    if LOCK.exists() or TRANSACTION.exists():
        raise GateError("cannot initialize validated records while a lock or recovery transaction exists")
    validate_app_contracts()
    progress, manifest, book, last = validate_progress_manifest()
    last_marker = {"bookId": last["bookId"], "book": book["title"], "chapter": last["chapter"]}
    next_marker = {"bookId": last["bookId"], "book": book["title"], "chapter": last["chapter"] + 1}
    validated_at = args.validated_at or iso_now()
    parse_iso(validated_at)
    state = {
        "format": "ethiopian-bible-validated-state",
        "schemaVersion": 1,
        "lastValidated": last_marker,
        "nextUnfinished": next_marker,
        "validatedAt": validated_at,
        "protectedFiles": protected_entries(last_marker),
    }
    atomic_write(STATE, json_bytes(state))
    ready_paths = list(args.ready_file)
    if ".automation/VALIDATED_STATE.json" not in ready_paths:
        ready_paths.append(".automation/VALIDATED_STATE.json")
    if ".automation/PUBLISH_READY.json" not in ready_paths:
        ready_paths.append(".automation/PUBLISH_READY.json")
    ready = {
        "format": "ethiopian-bible-publish-ready",
        "schemaVersion": 1,
        "status": "VALIDATED",
        "lastValidated": last_marker,
        "nextUnfinished": next_marker,
        "validatedAt": validated_at,
        "checks": {name: "PASSED" for name in REQUIRED_CHECKS},
        "files": file_entries(ready_paths),
    }
    atomic_write(READY, json_bytes(ready))
    validate_repository_records()
    print(f"Initialized VALIDATED publication state through {book['title']} {last['chapter']}.")


def lock_payload(book_id, chapter, run_id, recovered_from=None):
    now = iso_now()
    payload = {
        "format": "ethiopian-bible-production-lock",
        "schemaVersion": 1,
        "bookId": book_id,
        "chapter": chapter,
        "runId": run_id,
        "host": socket.gethostname(),
        "acquiredAt": now,
        "heartbeatAt": now,
        "staleAfterSeconds": STALE_SECONDS,
        "phase": "ACQUIRED",
    }
    if recovered_from:
        payload["recoveredFromRunId"] = recovered_from
    return payload


def create_lock_atomic(payload):
    data = json_bytes(payload)
    AUTOMATION.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(LOCK), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return True


def lock_command(args):
    if args.lock_action == "status":
        if not LOCK.exists():
            print("UNLOCKED")
            return
        lock = read_json(LOCK)
        print(json.dumps(lock, ensure_ascii=False, indent=2))
        return
    if args.lock_action == "acquire":
        if args.chapter < 1 or not args.book_id or not args.run_id:
            raise GateError("book, chapter, and run ID are required")
        if PUBLISH_LOCK.exists():
            raise GateError("a controlled publication is in progress; production lock acquisition refused")
        if TRANSACTION.exists():
            raise GateError("a validated finalization transaction must be recovered before acquiring a chapter")
        try:
            _, _, _, last, state, ready = validate_repository_records()
        except GateError as exc:
            raise GateError(f"records must be valid before lock acquisition: {exc}") from exc
        expected = ready["nextUnfinished"]
        if (args.book_id, args.chapter) != (expected["bookId"], expected["chapter"]):
            raise GateError(
                f"requested {args.book_id} {args.chapter}, but the only permitted next chapter is "
                f"{expected['bookId']} {expected['chapter']}"
            )
        payload = lock_payload(args.book_id, args.chapter, args.run_id)
        if create_lock_atomic(payload):
            if PUBLISH_LOCK.exists():
                current = read_json(LOCK)
                if current.get("runId") == args.run_id:
                    LOCK.unlink()
                raise GateError("a controlled publication started concurrently; production lock acquisition refused")
            print(f"LOCK ACQUIRED: {args.book_id} {args.chapter} ({args.run_id})")
            return
        existing = read_json(LOCK)
        target = (existing.get("bookId"), existing.get("chapter"))
        if target != (args.book_id, args.chapter):
            raise GateError(
                f"production is locked to {target[0]} {target[1]}; do not start {args.book_id} {args.chapter}"
            )
        age = (utc_now() - parse_iso(existing.get("heartbeatAt"))).total_seconds()
        stale_after = existing.get("staleAfterSeconds", STALE_SECONDS)
        if type(stale_after) is not int or stale_after < STALE_SECONDS:
            stale_after = STALE_SECONDS
        if age < stale_after:
            remaining = max(0, int(stale_after - age))
            print(
                f"ACTIVE LOCK: resume/wait only for {args.book_id} {args.chapter}; "
                f"stale recovery unavailable for {remaining}s",
                file=sys.stderr,
            )
            raise SystemExit(75)
        recovered = lock_payload(args.book_id, args.chapter, args.run_id, existing.get("runId"))
        atomic_write(LOCK, json_bytes(recovered))
        print(f"STALE LOCK RECOVERED: resume {args.book_id} {args.chapter} ({args.run_id})")
        return
    if not LOCK.exists():
        raise GateError("no production lock exists")
    lock = read_json(LOCK)
    if lock.get("runId") != args.run_id:
        raise GateError("run ID does not own the production lock")
    if args.lock_action == "heartbeat":
        lock["heartbeatAt"] = iso_now()
        lock["phase"] = args.phase or lock.get("phase") or "IN_PROGRESS"
        atomic_write(LOCK, json_bytes(lock))
        print(f"LOCK HEARTBEAT: {lock['bookId']} {lock['chapter']} — {lock['phase']}")
        return
    raise GateError("the production lock is removed only by successful finalization")


def validate_evidence(path, book_id, chapter_number, chapter_file):
    evidence = read_json(path)
    if evidence.get("format") != "ethiopian-bible-validation-evidence" or evidence.get("schemaVersion") != 1:
        raise GateError("validation evidence has the wrong format or schema")
    if (evidence.get("bookId"), evidence.get("chapter"), evidence.get("chapterFile")) != (
        book_id,
        chapter_number,
        relative(chapter_file),
    ):
        raise GateError("validation evidence does not match the locked chapter")
    source_checks = evidence.get("sourceChecks")
    required = (
        "earliestPrimaryWitnessesUsed",
        "modernTranslationNotUsed",
        "commentaryNotUsedAsScripture",
        "missingTextNotGuessed",
        "completeSourceEstablished",
    )
    if not isinstance(source_checks, dict) or any(source_checks.get(name) is not True for name in required):
        raise GateError("validation evidence does not attest every source rule")
    witnesses = evidence.get("primaryWitnesses")
    if not isinstance(witnesses, list) or not witnesses:
        raise GateError("validation evidence names no primary witnesses")
    for witness in witnesses:
        if not isinstance(witness, dict):
            raise GateError("primary witness evidence is invalid")
        require_text(witness.get("language"), "primary witness language")
        require_text(witness.get("witness"), "primary witness name")
        require_text(witness.get("scope"), "primary witness scope")
    return evidence


def dirty_paths():
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise GateError(f"git status failed: {result.stderr.decode('utf-8', 'replace').strip()}")
    items = result.stdout.split(b"\0")
    paths = set()
    index = 0
    while index < len(items):
        item = items[index]
        index += 1
        if not item:
            continue
        if len(item) < 4:
            raise GateError("could not parse git status output")
        status = item[:2].decode("ascii", "replace")
        path = item[3:].decode("utf-8", "surrogateescape")
        if "R" in status or "C" in status:
            if index >= len(items):
                raise GateError("could not parse renamed git path")
            path = items[index].decode("utf-8", "surrogateescape")
            index += 1
        paths.add(path)
    return paths


def role_map(ready):
    return {entry["path"]: entry.get("role", file_role(entry["path"])) for entry in ready["files"]}


def prepare_transaction(files, target, run_id, validated_at):
    transaction = {
        "format": "ethiopian-bible-finalize-transaction",
        "schemaVersion": 1,
        "validationPassed": True,
        "target": target,
        "runId": run_id,
        "validatedAt": validated_at,
        "files": [],
    }
    for path, data in files:
        transaction["files"].append(
            {
                "path": relative(path),
                "sha256": sha256_bytes(data),
                "contentBase64": base64.b64encode(data).decode("ascii"),
            }
        )
    atomic_write(TRANSACTION, json_bytes(transaction))
    return transaction


def apply_transaction(transaction):
    if transaction.get("format") != "ethiopian-bible-finalize-transaction" or transaction.get("validationPassed") is not True:
        raise GateError("finalization transaction is invalid or was not validated")
    lock = read_json(LOCK)
    target = transaction.get("target")
    if not isinstance(target, dict) or (lock.get("bookId"), lock.get("chapter")) != (target.get("bookId"), target.get("chapter")):
        raise GateError("finalization transaction does not match the production lock")
    for entry in transaction.get("files", []):
        path = safe_path(entry.get("path"))
        try:
            data = base64.b64decode(entry.get("contentBase64"), validate=True)
        except Exception as exc:
            raise GateError(f"invalid transaction payload for {entry.get('path')}") from exc
        if sha256_bytes(data) != entry.get("sha256"):
            raise GateError(f"transaction hash mismatch for {entry.get('path')}")
        atomic_write(path, data)
    validate_repository_records()
    target_tuple = (target.get("bookId"), target.get("chapter"))
    ready = read_json(READY)
    if (ready["lastValidated"]["bookId"], ready["lastValidated"]["chapter"]) != target_tuple:
        raise GateError("finalized publishing record does not match the transaction target")
    TRANSACTION.unlink()
    LOCK.unlink()
    candidate = AUTOMATION / "VALIDATION_CANDIDATE.json"
    if candidate.exists():
        candidate.unlink()


def finalize(args):
    if TRANSACTION.exists():
        raise GateError("a finalization transaction already exists; recover it first")
    lock = read_json(LOCK)
    if lock.get("runId") != args.run_id:
        raise GateError("run ID does not own the production lock")
    book_id = lock.get("bookId")
    chapter_number = lock.get("chapter")
    chapter_file = safe_path(args.chapter_file)
    state = validate_state()
    ready = validate_ready()
    expected = ready["nextUnfinished"]
    if (book_id, chapter_number) != (expected["bookId"], expected["chapter"]):
        raise GateError("locked chapter is not the publishing record's next unfinished chapter")
    payload, book_meta, chapter = validate_payload(chapter_file, book_id, chapter_number)
    validate_evidence(safe_path(args.evidence), book_id, chapter_number, chapter_file)
    validate_app_contracts()
    manifest = read_json(MANIFEST)
    _, book_map, last = validate_manifest(manifest)
    if (last["bookId"], last["chapter"] + 1) != (book_id, chapter_number):
        raise GateError("candidate chapter is not sequential after the manifest's last validated chapter")
    if book_id not in book_map:
        raise GateError("candidate book is absent from the manifest")
    update_entry = {
        "id": payload["updateId"],
        "bookId": book_id,
        "chapter": chapter_number,
        "contentVersion": payload["contentVersion"],
        "url": relative(chapter_file),
    }
    updated_at = iso_now()
    new_manifest = json.loads(json.dumps(manifest))
    new_manifest["contentVersion"] = payload["contentVersion"]
    new_manifest["updatedAt"] = updated_at
    new_manifest["updates"].append(update_entry)
    validate_manifest(new_manifest)
    total = book_meta["totalChapterCount"]
    progress_bytes = build_progress(chapter["bookTitle"], chapter_number, total)
    parse_progress(progress_bytes.decode("utf-8"))
    manifest_bytes = json_bytes(new_manifest)
    last_marker = {"bookId": book_id, "book": chapter["bookTitle"], "chapter": chapter_number}
    next_marker = {"bookId": book_id, "book": chapter["bookTitle"], "chapter": chapter_number + 1}
    new_state = json.loads(json.dumps(state))
    new_state["lastValidated"] = last_marker
    new_state["nextUnfinished"] = next_marker
    new_state["validatedAt"] = updated_at
    new_state["protectedFiles"].append(
        {"path": relative(chapter_file), "sha256": sha256(chapter_file), "scope": "validated Scripture"}
    )
    state_bytes = json_bytes(new_state)
    existing_ready_paths = [entry["path"] for entry in ready["files"] if entry["path"] in dirty_paths()]
    ready_paths = set(existing_ready_paths)
    ready_paths.update(
        {
            relative(chapter_file),
            "content-manifest.json",
            "PROGRESS.md",
            ".automation/VALIDATED_STATE.json",
            ".automation/PUBLISH_READY.json",
        }
    )
    overrides = {
        "content-manifest.json": manifest_bytes,
        "PROGRESS.md": progress_bytes,
        ".automation/VALIDATED_STATE.json": state_bytes,
    }
    ready_entries = []
    for path_text in sorted(ready_paths):
        if safe_path(path_text) == READY:
            ready_entries.append(
                {"path": path_text, "validation": "self", "role": "publication-control-record"}
            )
            continue
        data = overrides.get(path_text)
        digest = sha256_bytes(data) if data is not None else sha256(safe_path(path_text))
        ready_entries.append({"path": path_text, "sha256": digest, "role": file_role(path_text)})
    new_ready = {
        "format": "ethiopian-bible-publish-ready",
        "schemaVersion": 1,
        "status": "VALIDATED",
        "lastValidated": last_marker,
        "nextUnfinished": next_marker,
        "validatedAt": updated_at,
        "checks": {name: "PASSED" for name in REQUIRED_CHECKS},
        "files": ready_entries,
    }
    ready_bytes = json_bytes(new_ready)
    transaction = prepare_transaction(
        [(MANIFEST, manifest_bytes), (PROGRESS, progress_bytes), (STATE, state_bytes), (READY, ready_bytes)],
        last_marker,
        args.run_id,
        updated_at,
    )
    apply_transaction(transaction)
    print(f"VALIDATED: {chapter['bookTitle']} {chapter_number}; next chapter {chapter_number + 1}")


def recover_finalize(_args):
    if not TRANSACTION.exists():
        print("No finalization transaction requires recovery.")
        return
    if not LOCK.exists():
        raise GateError("recovery transaction exists without its production lock")
    transaction = read_json(TRANSACTION)
    apply_transaction(transaction)
    target = transaction["target"]
    print(f"RECOVERED VALIDATED FINALIZATION: {target['book']} {target['chapter']}")


def verify(_args):
    progress, manifest, book, last, state, ready = validate_repository_records()
    print(f"VALIDATION GATE: PASS — {book['title']} {last['chapter']} is VALIDATED")
    print(f"Next unfinished chapter: {ready['nextUnfinished']['book']} {ready['nextUnfinished']['chapter']}")


def publication_gate():
    if LOCK.exists():
        lock = read_json(LOCK)
        raise GateError(f"production lock exists for {lock.get('bookId')} {lock.get('chapter')}; publication refused")
    if TRANSACTION.exists():
        raise GateError("a finalization recovery transaction exists; publication refused")
    if not READY.exists():
        raise GateError(".automation/PUBLISH_READY.json does not exist; publication refused")
    progress, manifest, book, last, state, ready = validate_repository_records()
    published = validate_publication_state()
    dirty = dirty_paths()
    approved = {entry["path"] for entry in ready["files"]}
    would_commit = sorted(dirty & approved)
    excluded = sorted(dirty - approved)
    unfinished = sorted(path for path in excluded if CHAPTER_FILE_RE.fullmatch(path))
    return {
        "progress": progress,
        "manifest": manifest,
        "book": book,
        "last": last,
        "state": state,
        "ready": ready,
        "published": published,
        "dirty": dirty,
        "approved": approved,
        "wouldCommit": would_commit,
        "excluded": excluded,
        "unfinished": unfinished,
    }


def print_publication_gate(result):
    book = result["book"]
    last = result["last"]
    ready = result["ready"]
    published = result["published"]
    would_commit = result["wouldCommit"]
    excluded = result["excluded"]
    unfinished = result["unfinished"]
    roles = role_map(ready)
    print(f"Last validated chapter: {book['title']} {last['chapter']}")
    print(f"Next unfinished chapter: {ready['nextUnfinished']['book']} {ready['nextUnfinished']['chapter']}")
    if published:
        marker = published["lastPublished"]
        print(f"Last successfully published chapter: {marker['book']} {marker['chapter']}")
    else:
        print("Last successfully published chapter: (none recorded — first controlled publication pending)")
    print("READY files that would be committed:")
    if would_commit:
        for path in would_commit:
            print(f"  {path} [{roles.get(path, file_role(path))}]")
    else:
        print("  (none — all allow-listed files are already clean)")
    print("Excluded changed files:")
    if excluded:
        for path in excluded:
            print(f"  {path}")
    else:
        print("  (none)")
    print("Unfinished/unvalidated chapter files excluded:")
    if unfinished:
        for path in unfinished:
            print(f"  {path}")
    else:
        print("  (none present)")


def publish_dry_run(_args):
    try:
        result = publication_gate()
    except GateError as exc:
        fail(str(exc), 65)
    print_publication_gate(result)
    print("VALIDATION GATE: PASS")
    print("DRY-RUN COMPLETE: no Git commit or push was performed.")


def git_run(arguments, *, check=True, input_bytes=None):
    result = subprocess.run(
        ["git", *arguments],
        cwd=str(ROOT),
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        if not detail:
            detail = result.stdout.decode("utf-8", "replace").strip()
        raise GateError(f"git {' '.join(arguments)} failed: {detail or f'exit {result.returncode}'}")
    return result


def git_text(arguments):
    return git_run(arguments).stdout.decode("utf-8", "surrogateescape").strip()


def decode_nul_paths(data):
    return {
        item.decode("utf-8", "surrogateescape")
        for item in data.split(b"\0")
        if item
    }


def staged_paths():
    result = git_run(["diff", "--cached", "--no-renames", "--name-only", "-z"])
    return decode_nul_paths(result.stdout)


def require_publish_git_context():
    if git_text(["symbolic-ref", "--quiet", "--short", "HEAD"]) != "main":
        raise GateError("controlled publication requires the existing local main branch")
    remotes = set(git_text(["remote"]).splitlines())
    if "origin" not in remotes:
        raise GateError("controlled publication requires the existing origin remote")
    for ref in ("refs/heads/main", "refs/remotes/origin/main"):
        result = git_run(["show-ref", "--verify", "--quiet", ref], check=False)
        if result.returncode != 0:
            raise GateError(f"controlled publication requires existing {ref}")
    head = git_text(["rev-parse", "--verify", "refs/heads/main"])
    origin_main = git_text(["rev-parse", "--verify", "refs/remotes/origin/main"])
    if head != origin_main:
        raise GateError("local main must exactly match origin/main before creating a publication commit")
    existing_staged = staged_paths()
    if existing_staged:
        listing = ", ".join(sorted(existing_staged))
        raise GateError(f"Git index is not clean; publication refused without changing staged files: {listing}")
    return head


def require_existing_remote_main(expected_commit):
    result = git_run(["ls-remote", "--exit-code", "--heads", "origin", "refs/heads/main"], check=False)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise GateError(f"existing origin/main could not be verified: {detail or 'remote branch not found'}")
    lines = [line for line in result.stdout.decode("ascii", "replace").splitlines() if line]
    if len(lines) != 1:
        raise GateError("existing origin/main lookup returned an unexpected result")
    fields = lines[0].split("\t", 1)
    if len(fields) != 2 or fields[1] != "refs/heads/main":
        raise GateError("existing origin/main lookup returned the wrong ref")
    if fields[0] != expected_commit:
        raise GateError("origin/main changed after publication preparation; push refused")


def ready_entry_map(ready):
    return {entry["path"]: entry for entry in ready["files"]}


def staged_bytes(path):
    return git_run(["show", f":{path}"]).stdout


def verify_staged_batch(expected_paths, ready):
    expected = set(expected_paths)
    actual = staged_paths()
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise GateError(f"staged paths do not exactly match READY changes; missing={missing}, extra={extra}")
    entries = ready_entry_map(ready)
    for path in sorted(actual):
        entry = entries.get(path)
        if entry is None:
            raise GateError(f"staged file is not READY: {path}")
        data = staged_bytes(path)
        if safe_path(path) == READY:
            if data != READY.read_bytes():
                raise GateError("staged PUBLISH_READY.json differs from the validated working copy")
            continue
        if sha256_bytes(data) != entry.get("sha256"):
            raise GateError(f"staged READY hash mismatch: {path}")
    whitespace = git_run(["diff", "--cached", "--check"], check=False)
    if whitespace.returncode != 0:
        detail = whitespace.stdout.decode("utf-8", "replace").strip()
        raise GateError(f"staged commit checks failed: {detail or 'git diff --cached --check failed'}")


def acquire_publish_lock():
    if LOCK.exists():
        lock = read_json(LOCK)
        raise GateError(f"production lock exists for {lock.get('bookId')} {lock.get('chapter')}; publication refused")
    if PUBLISH_LOCK.exists():
        raise GateError("another controlled publication lock exists; publication refused")
    if PUBLICATION_PENDING.exists():
        raise GateError("a pending publication-state record exists; inspect it before another publication")
    run_id = str(uuid.uuid4())
    payload = {
        "format": "ethiopian-bible-publish-lock",
        "schemaVersion": 1,
        "runId": run_id,
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "acquiredAt": iso_now(),
        "target": "origin/main",
    }
    data = json_bytes(payload)
    AUTOMATION.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(PUBLISH_LOCK), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise GateError("another controlled publication lock exists; publication refused") from exc
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    if LOCK.exists():
        release_publish_lock(run_id)
        lock = read_json(LOCK)
        raise GateError(
            f"production lock appeared for {lock.get('bookId')} {lock.get('chapter')}; publication refused"
        )
    return run_id


def release_publish_lock(run_id):
    if not PUBLISH_LOCK.exists():
        return
    lock = read_json(PUBLISH_LOCK)
    if lock.get("runId") != run_id:
        raise GateError("publication lock ownership changed unexpectedly; lock was preserved")
    PUBLISH_LOCK.unlink()


def cleanup_staging(paths):
    if not paths:
        return
    result = git_run(["restore", "--staged", "--", *paths], check=False)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise GateError(f"could not restore the pre-publication index after failure: {detail}")


def commit_paths(old_head, new_head):
    result = git_run(
        ["diff-tree", "--no-commit-id", "--name-only", "-r", "--no-renames", "-z", old_head, new_head]
    )
    return decode_nul_paths(result.stdout)


def commit_file_bytes(commit, path):
    return git_run(["show", f"{commit}:{path}"]).stdout


def verify_publication_commit(old_head, new_head, expected_paths, ready, message):
    if new_head == old_head:
        raise GateError("publication commit did not advance main")
    if git_text(["rev-parse", f"{new_head}^"]) != old_head:
        raise GateError("publication commit is not a single direct child of the pre-publication main")
    if git_text(["show", "-s", "--format=%s", new_head]) != message:
        raise GateError("publication commit message check failed")
    actual = commit_paths(old_head, new_head)
    expected = set(expected_paths)
    if actual != expected:
        raise GateError(
            f"publication commit paths do not exactly match READY changes; "
            f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )
    entries = ready_entry_map(ready)
    for path in sorted(actual):
        data = commit_file_bytes(new_head, path)
        entry = entries[path]
        if safe_path(path) == READY:
            if data != READY.read_bytes():
                raise GateError("committed PUBLISH_READY.json differs from the validated control record")
        elif sha256_bytes(data) != entry.get("sha256"):
            raise GateError(f"committed READY hash mismatch: {path}")
    if staged_paths():
        raise GateError("Git index is not clean after the publication commit")
    if git_text(["symbolic-ref", "--quiet", "--short", "HEAD"]) != "main":
        raise GateError("branch changed during publication; push refused")
    if git_text(["rev-parse", "--verify", "refs/heads/main"]) != new_head:
        raise GateError("main does not point to the checked publication commit")


def publication_state_payload(result, commit, paths):
    last = result["last"]
    book = result["book"]
    return {
        "format": "ethiopian-bible-publication-state",
        "schemaVersion": 1,
        "status": "PUBLISHED",
        "lastPublished": {
            "bookId": last["bookId"],
            "book": book["title"],
            "chapter": last["chapter"],
        },
        "publishedAt": iso_now(),
        "validatedAt": result["ready"]["validatedAt"],
        "commit": commit,
        "remote": "origin",
        "branch": "main",
        "files": [
            {"path": path, "sha256": sha256_bytes(commit_file_bytes(commit, path))}
            for path in sorted(paths)
        ],
    }


def publish_validated(_args):
    publish_run_id = acquire_publish_lock()
    staging_started = False
    commit_created = False
    paths = []
    try:
        old_head = require_publish_git_context()
        result = publication_gate()
        print_publication_gate(result)
        paths = result["wouldCommit"]
        if not paths:
            raise GateError("no changed READY files exist; publication commit refused")

        staging_started = True
        git_run(["add", "--", *paths])
        verify_staged_batch(paths, result["ready"])

        # Re-run the complete gate after staging and before committing anything.
        checked = publication_gate()
        if checked["wouldCommit"] != paths:
            raise GateError("READY changes changed after staging; publication refused")
        verify_staged_batch(paths, checked["ready"])

        message = f"Publish validated Ethiopian Bible through {checked['book']['title']} {checked['last']['chapter']}"
        commit_result = git_run(["commit", "-m", message], check=False)
        new_head = git_text(["rev-parse", "--verify", "HEAD"])
        commit_created = new_head != old_head
        if commit_result.returncode != 0:
            detail = commit_result.stderr.decode("utf-8", "replace").strip()
            if not detail:
                detail = commit_result.stdout.decode("utf-8", "replace").strip()
            raise GateError(f"publication commit failed: {detail or f'exit {commit_result.returncode}'}")
        verify_publication_commit(old_head, new_head, paths, checked["ready"], message)

        state_payload = publication_state_payload(checked, new_head, paths)
        atomic_write(PUBLICATION_PENDING, json_bytes(state_payload))

        # This is the final operation before the explicit origin/main push.
        final_check = publication_gate()
        if final_check["ready"] != checked["ready"] or final_check["last"] != checked["last"]:
            raise GateError("validated publication records changed after commit; push refused")
        verify_publication_commit(old_head, new_head, paths, final_check["ready"], message)
        if LOCK.exists():
            raise GateError("production lock appeared after final validation; push refused")
        require_existing_remote_main(old_head)

        push = git_run(
            [
                "push",
                "--porcelain",
                "--no-follow-tags",
                "--recurse-submodules=no",
                f"--force-with-lease=refs/heads/main:{old_head}",
                "origin",
                "refs/heads/main:refs/heads/main",
            ],
            check=False,
        )
        if push.returncode != 0:
            detail = push.stderr.decode("utf-8", "replace").strip()
            if not detail:
                detail = push.stdout.decode("utf-8", "replace").strip()
            raise GateError(f"origin/main push failed: {detail or f'exit {push.returncode}'}")

        os.replace(PUBLICATION_PENDING, PUBLICATION_STATE)
        directory_fd = os.open(str(AUTOMATION), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        marker = state_payload["lastPublished"]
        print(f"PUBLISHED: {marker['book']} {marker['chapter']} at commit {new_head}")
        print("PUBLICATION STATE: .automation/PUBLICATION_STATE.json")
    except BaseException:
        if staging_started and not commit_created:
            cleanup_staging(paths)
        raise
    finally:
        release_publish_lock(publish_run_id)


def parser():
    command_parser = argparse.ArgumentParser(description=__doc__)
    subparsers = command_parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("initialize", help="initialize records from the already validated repository")
    init_parser.add_argument("--validated-at")
    init_parser.add_argument("--ready-file", action="append", default=[])
    init_parser.set_defaults(func=initialize)

    lock_parser = subparsers.add_parser("lock", help="manage the production lock")
    lock_subparsers = lock_parser.add_subparsers(dest="lock_action", required=True)
    acquire_parser = lock_subparsers.add_parser("acquire")
    acquire_parser.add_argument("book_id")
    acquire_parser.add_argument("chapter", type=int)
    acquire_parser.add_argument("run_id")
    acquire_parser.set_defaults(func=lock_command)
    heartbeat_parser = lock_subparsers.add_parser("heartbeat")
    heartbeat_parser.add_argument("run_id")
    heartbeat_parser.add_argument("--phase")
    heartbeat_parser.set_defaults(func=lock_command)
    status_parser = lock_subparsers.add_parser("status")
    status_parser.set_defaults(func=lock_command)

    finalize_parser = subparsers.add_parser("finalize", help="validate and atomically finalize one locked chapter")
    finalize_parser.add_argument("--chapter-file", required=True)
    finalize_parser.add_argument("--evidence", required=True)
    finalize_parser.add_argument("--run-id", required=True)
    finalize_parser.set_defaults(func=finalize)

    recover_parser = subparsers.add_parser("recover-finalize", help="finish a validated interrupted transaction")
    recover_parser.set_defaults(func=recover_finalize)

    verify_parser = subparsers.add_parser("verify", help="verify the current validated state")
    verify_parser.set_defaults(func=verify)

    publish_parser = subparsers.add_parser("publish-dry-run", help="show the validated-only Git allow-list")
    publish_parser.set_defaults(func=publish_dry_run)

    live_publish_parser = subparsers.add_parser(
        "publish", help="commit the exact validated allow-list and push only origin/main"
    )
    live_publish_parser.set_defaults(func=publish_validated)
    return command_parser


def main():
    args = parser().parse_args()
    try:
        args.func(args)
    except GateError as exc:
        fail(str(exc))


if __name__ == "__main__":
    main()
