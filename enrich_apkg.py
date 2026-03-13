#!/usr/bin/env python3
"""
enrich_apkg.py — Enrich an Anki .apkg deck with Heisig RTK data from a CSV.

For each note in the "Kaishi 1.5k" notetype, this script:
  1. Reads the Word field and looks up each character in the Heisig CSV.
  2. Adds a "heisig" tag and a zero-padded RTK frame-number tag (e.g. "0042").
  3. Populates the heisig-keyword field with matched keywords.

Usage:
    python enrich_apkg.py <input.apkg> <heisig-kanjis.csv> [output.apkg]

If output path is omitted, defaults to <input>_enriched.apkg.

Requirements:
    pip install zstandard
"""

import csv
import io
import os
import sqlite3
import sys
import tempfile
import zipfile

import zstandard as zstd


# ---------------------------------------------------------------------------
# 1. Load the Heisig CSV into a lookup dict:  kanji → (id_6th_ed, keyword_6th_ed)
# ---------------------------------------------------------------------------

def load_heisig_csv(csv_path: str) -> dict[str, tuple[int, str]]:
    """
    Returns {kanji_char: (frame_number, keyword)} for every row where
    both `kanji` and `id_6th_ed` are non-empty.
    """
    lookup: dict[str, tuple[int, str]] = {}
    with open(csv_path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            kanji = row.get("kanji", "").strip()
            id_raw = row.get("id_6th_ed", "").strip()
            keyword = row.get("keyword_6th_ed", "").strip()
            if not kanji or not id_raw:
                continue  # skip rows without a kanji or frame number
            try:
                frame_num = int(id_raw)
            except ValueError:
                continue  # skip non-numeric ids
            lookup[kanji] = (frame_num, keyword)
    return lookup


# ---------------------------------------------------------------------------
# 2. Unpack the .apkg zip and decompress collection.anki21b
# ---------------------------------------------------------------------------

def read_apkg(apkg_path: str) -> tuple[bytes, zipfile.ZipFile]:
    """
    Opens the .apkg zip, finds collection.anki21b, decompresses it with
    zstandard, and returns (raw_sqlite_bytes, open_zipfile).
    """
    zf = zipfile.ZipFile(apkg_path, "r")

    # The newer Anki format uses collection.anki21b (zstd-compressed SQLite)
    compressed = zf.read("collection.anki21b")

    dctx = zstd.ZstdDecompressor()
    raw_db = dctx.decompress(compressed, max_output_size=256 * 1024 * 1024)

    return raw_db, zf


# ---------------------------------------------------------------------------
# 3. Open the raw SQLite bytes as an in-memory database
# ---------------------------------------------------------------------------

def open_db_in_memory(raw_db: bytes) -> sqlite3.Connection:
    """
    Loads raw SQLite bytes into an in-memory database so we can modify it
    without touching the filesystem.
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript("")  # ensure a writable database exists
    # Deserialize: load the byte blob as the "main" database
    conn.close()

    # Use the sqlite3 backup API to copy from a temp file into memory.
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.write(raw_db)
    tmp.close()

    disk_conn = sqlite3.connect(tmp.name)
    mem_conn = sqlite3.connect(":memory:")
    disk_conn.backup(mem_conn)
    disk_conn.close()
    os.unlink(tmp.name)

    return mem_conn


# ---------------------------------------------------------------------------
# 4. Build field-index maps from the `fields` table
# ---------------------------------------------------------------------------

def build_field_maps(conn: sqlite3.Connection) -> dict[int, dict[str, int]]:
    """
    Returns {notetype_id: {field_name: field_ord}} built from the `fields`
    table (Anki 23.10+ schema).
    """
    cur = conn.execute("SELECT ntid, ord, name FROM fields")
    maps: dict[int, dict[str, int]] = {}
    for ntid, ord_, name in cur.fetchall():
        maps.setdefault(ntid, {})[name] = ord_
    return maps


# ---------------------------------------------------------------------------
# 5. Enrich notes
# ---------------------------------------------------------------------------

FIELD_SEP = "\x1f"  # Anki uses Unit Separator to delimit fields


def enrich_notes(
    conn: sqlite3.Connection,
    field_maps: dict[int, dict[str, int]],
    heisig: dict[str, tuple[int, str]],
) -> int:
    """
    For every note whose notetype has a 'Word' field, look up each character
    in the Heisig dict. If matches are found, add tags and set the
    heisig-keyword field. Returns the number of notes modified.
    """
    # Identify which notetypes have a "Word" field
    eligible_ntids: dict[int, tuple[int, int | None]] = {}
    for ntid, fmap in field_maps.items():
        if "Word" in fmap:
            word_idx = fmap["Word"]
            kw_idx = fmap.get("heisig-keyword")  # may be None
            eligible_ntids[ntid] = (word_idx, kw_idx)

    if not eligible_ntids:
        print("  ⚠  No notetypes with a 'Word' field found. Nothing to do.")
        return 0

    cur = conn.execute("SELECT id, mid, tags, flds FROM notes")
    rows = cur.fetchall()

    modified = 0

    for note_id, mid, tags_str, flds_str in rows:
        if mid not in eligible_ntids:
            continue  # notetype has no Word field — skip

        word_idx, kw_idx = eligible_ntids[mid]
        fields = flds_str.split(FIELD_SEP)

        # Safety: make sure the field index is within range
        if word_idx >= len(fields):
            continue

        word_value = fields[word_idx]

        # --- Look up each character against the Heisig CSV ---
        matches: list[tuple[int, str]] = []  # (frame_number, keyword)
        seen_kanji: set[str] = set()

        for ch in word_value:
            if ch in heisig and ch not in seen_kanji:
                seen_kanji.add(ch)
                matches.append(heisig[ch])

        if not matches:
            continue  # no kanji in Heisig — leave note untouched

        # --- Determine the tag to add ---
        # Highest frame number among all matched kanji, zero-padded to 4 digits
        max_frame = max(frame for frame, _ in matches)
        frame_tag = f"{max_frame:04d}"

        # --- Update tags (idempotent) ---
        existing_tags = set(tags_str.split())
        new_tags = set(existing_tags)
        new_tags.add("heisig")
        new_tags.add(frame_tag)

        tags_changed = new_tags != existing_tags

        # Rebuild the tags string.  Anki stores tags with a leading and
        # trailing space so that substring searches work reliably.
        if tags_changed:
            new_tags_str = " " + " ".join(sorted(new_tags)) + " "
        else:
            new_tags_str = tags_str

        # --- Update heisig-keyword field (idempotent) ---
        flds_changed = False
        if kw_idx is not None and kw_idx < len(fields):
            current_kw = fields[kw_idx]
            if not current_kw.strip():
                # Only write if the field is currently empty
                keywords_str = ", ".join(kw for _, kw in matches)
                fields[kw_idx] = keywords_str
                flds_changed = True

        if not tags_changed and not flds_changed:
            continue  # already enriched on a previous run — skip

        new_flds_str = FIELD_SEP.join(fields)

        conn.execute(
            "UPDATE notes SET tags = ?, flds = ? WHERE id = ?",
            (new_tags_str, new_flds_str, note_id),
        )
        modified += 1

    conn.commit()
    return modified


# ---------------------------------------------------------------------------
# 6. Serialize the in-memory DB back to bytes
# ---------------------------------------------------------------------------

def serialize_db(conn: sqlite3.Connection) -> bytes:
    """Dump the in-memory SQLite database back to raw bytes."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()

    disk_conn = sqlite3.connect(tmp.name)
    conn.backup(disk_conn)
    disk_conn.close()

    with open(tmp.name, "rb") as f:
        raw = f.read()
    os.unlink(tmp.name)
    return raw


# ---------------------------------------------------------------------------
# 7. Recompress and repack into a new .apkg
# ---------------------------------------------------------------------------

def write_apkg(
    output_path: str,
    modified_db: bytes,
    original_zip: zipfile.ZipFile,
) -> None:
    """
    Creates a new .apkg zip with the modified collection.anki21b and all
    other files (media, etc.) copied from the original.
    """
    # Recompress with zstandard
    cctx = zstd.ZstdCompressor()
    compressed = cctx.compress(modified_db)

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_STORED) as out_zip:
        for item in original_zip.infolist():
            if item.filename == "collection.anki21b":
                out_zip.writestr("collection.anki21b", compressed)
            else:
                # Copy media and any other files as-is
                out_zip.writestr(item, original_zip.read(item.filename))

    original_zip.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: python enrich_apkg.py <input.apkg> <heisig-kanjis.csv> [output.apkg]")
        sys.exit(1)

    apkg_path = sys.argv[1]
    csv_path = sys.argv[2]
    output_path = sys.argv[3] if len(sys.argv) > 3 else None

    if output_path is None:
        base, ext = os.path.splitext(apkg_path)
        output_path = f"{base}_enriched{ext}"

    print(f"Loading Heisig CSV from: {csv_path}")
    heisig = load_heisig_csv(csv_path)
    print(f"  → {len(heisig)} kanji entries loaded")

    print(f"Reading .apkg from: {apkg_path}")
    raw_db, original_zip = read_apkg(apkg_path)
    print(f"  → Decompressed DB: {len(raw_db):,} bytes")

    conn = open_db_in_memory(raw_db)

    # Build field maps from the `fields` table
    field_maps = build_field_maps(conn)
    print(f"  → Found {len(field_maps)} notetype(s) with field definitions")

    # Show what we found for debugging
    for ntid, fmap in field_maps.items():
        # Try to get the notetype name
        row = conn.execute(
            "SELECT name FROM notetypes WHERE id = ?", (ntid,)
        ).fetchone()
        nt_name = row[0] if row else "(unknown)"
        field_names = ", ".join(
            f"{name}[{idx}]" for name, idx in sorted(fmap.items(), key=lambda x: x[1])
        )
        has_word = "Word" in fmap
        print(f"    • {nt_name} (id={ntid}): {field_names}"
              f" {'✓ eligible' if has_word else '— skipped'}")

    print("Enriching notes...")
    count = enrich_notes(conn, field_maps, heisig)
    print(f"  → {count} note(s) modified")

    print(f"Writing enriched .apkg to: {output_path}")
    modified_db = serialize_db(conn)
    conn.close()
    write_apkg(output_path, modified_db, original_zip)

    print("Done!")


if __name__ == "__main__":
    main()
