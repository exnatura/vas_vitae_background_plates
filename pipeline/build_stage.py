#!/usr/bin/env python3
"""Load the raw crawl and the matching slice of the BHL export into data/stage.db.

Steps (re-running a step replaces its tables):

  flickr   data/flickr/{photos,albums,members} -> photo, album, album_photo
  bhl      data/bhl/data.zip, restricted to the pages the Flickr photos point at
           -> bhl_page, bhl_pagename, bhl_item, bhl_title, bhl_creator, bhl_subject

Usage:
  python3 pipeline/build_stage.py [flickr] [bhl]

stage.db is a working database for the later tagging and export steps, not the file
the app ships. Standard library only.
"""
import argparse
import gzip
import io
import json
import re
import sqlite3
import time
import zipfile
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
FLICKR_DIR = DATA_DIR / "flickr"
BHL_ZIP = DATA_DIR / "bhl" / "data.zip"
STAGE_DB = DATA_DIR / "stage.db"

PAGE_TAG = re.compile(r"\bbhl:page=(\d+)")
PAGE_LINK = re.compile(r"biodiversitylibrary\.org/page/(\d+)")
ITEM_LINK = re.compile(r"biodiversitylibrary\.org/item/(\d+)")

SCHEMA = {
    "flickr": """
        DROP TABLE IF EXISTS photo;
        DROP TABLE IF EXISTS album;
        DROP TABLE IF EXISTS album_photo;
        CREATE TABLE photo (
            id INTEGER PRIMARY KEY, secret TEXT, server TEXT, title TEXT, description TEXT,
            tags TEXT, machine_tags TEXT, views INTEGER, faves INTEGER, license TEXT,
            date_upload INTEGER, o_width INTEGER, o_height INTEGER,
            l_width INTEGER, l_height INTEGER, page_id INTEGER
        );
        CREATE TABLE album (
            id INTEGER PRIMARY KEY, title TEXT, description TEXT, item_id INTEGER,
            photo_count INTEGER, views INTEGER
        );
        CREATE TABLE album_photo (album_id INTEGER, photo_id INTEGER, PRIMARY KEY (album_id, photo_id));
    """,
    "bhl": """
        DROP TABLE IF EXISTS bhl_page;
        DROP TABLE IF EXISTS bhl_pagename;
        DROP TABLE IF EXISTS bhl_item;
        DROP TABLE IF EXISTS bhl_title;
        DROP TABLE IF EXISTS bhl_creator;
        DROP TABLE IF EXISTS bhl_subject;
        CREATE TABLE bhl_page (
            page_id INTEGER PRIMARY KEY, item_id INTEGER, sequence INTEGER, year TEXT, volume TEXT,
            page_prefix TEXT, page_number TEXT, page_type TEXT
        );
        CREATE TABLE bhl_pagename (page_id INTEGER, name TEXT);
        CREATE TABLE bhl_item (
            item_id INTEGER PRIMARY KEY, title_id INTEGER, volume TEXT, year TEXT,
            institution TEXT, copyright_status TEXT
        );
        CREATE TABLE bhl_title (
            title_id INTEGER PRIMARY KEY, full_title TEXT, short_title TEXT,
            publication_details TEXT, start_year TEXT, end_year TEXT, language TEXT
        );
        CREATE TABLE bhl_creator (title_id INTEGER, creator_id INTEGER, creator_type TEXT, name TEXT);
        CREATE TABLE bhl_subject (title_id INTEGER, subject TEXT);
    """,
}


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def read_json_gz(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def first_int(pattern, text):
    m = pattern.search(text or "")
    return int(m.group(1)) if m else None


def load_flickr(db):
    db.executescript(SCHEMA["flickr"])

    files = sorted((FLICKR_DIR / "photos").glob("page_*.json.gz"))
    for f in files:
        rows = []
        for p in read_json_gz(f)["photos"]["photo"]:
            description = p.get("description", {}).get("_content", "")
            machine_tags = p.get("machine_tags", "")
            rows.append((
                int(p["id"]), p["secret"], p["server"], p.get("title"), description,
                p.get("tags", ""), machine_tags, to_int(p.get("views")), to_int(p.get("count_faves")),
                p.get("license"), to_int(p.get("dateupload")),
                to_int(p.get("o_width")), to_int(p.get("o_height")),
                to_int(p.get("width_l")), to_int(p.get("height_l")),
                first_int(PAGE_TAG, machine_tags) or first_int(PAGE_LINK, description),
            ))
        # Pages shift when BHL uploads mid-crawl, so the same photo can appear twice.
        db.executemany("INSERT OR REPLACE INTO photo VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    db.commit()
    log(f"photo: {db.execute('SELECT count(*) FROM photo').fetchone()[0]} rows from {len(files)} page file(s)")

    for f in sorted((FLICKR_DIR / "albums").glob("list_*.json.gz")):
        db.executemany(
            "INSERT OR REPLACE INTO album VALUES (?,?,?,?,?,?)",
            [(
                int(a["id"]), a["title"]["_content"], a["description"]["_content"],
                first_int(ITEM_LINK, a["description"]["_content"]),
                to_int(a.get("count_photos")), to_int(a.get("count_views")),
            ) for a in read_json_gz(f)["photosets"]["photoset"]],
        )
    for f in (FLICKR_DIR / "members").glob("*.json.gz"):
        m = read_json_gz(f)
        db.executemany(
            "INSERT OR IGNORE INTO album_photo VALUES (?,?)",
            [(int(m["id"]), int(pid)) for pid in m["photo_ids"]],
        )
    db.commit()
    log(f"album: {db.execute('SELECT count(*) FROM album').fetchone()[0]} rows, "
        f"album_photo: {db.execute('SELECT count(*) FROM album_photo').fetchone()[0]} rows")


def bhl_rows(archive, table):
    """Yield (header-keyed dict) rows of one tab-separated table inside the export zip."""
    with archive.open(f"Data/{table}.txt") as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace")
        header = text.readline().rstrip("\r\n").split("\t")
        width = len(header)
        for line in text:
            fields = line.rstrip("\r\n").split("\t")
            # A few free-text fields contain stray line breaks; skip the fragments.
            if len(fields) == width:
                yield dict(zip(header, fields))


def load_bhl(db):
    db.executescript(SCHEMA["bhl"])
    page_ids = {r[0] for r in db.execute("SELECT DISTINCT page_id FROM photo WHERE page_id IS NOT NULL")}
    if not page_ids:
        raise SystemExit("No photos with BHL page ids — run the flickr step first")
    log(f"{len(page_ids)} distinct BHL pages referenced by photos")

    def insert(table, rows, batch=50_000):
        buf, total = [], 0
        for row in rows:
            buf.append(row)
            if len(buf) >= batch:
                db.executemany(f"INSERT OR REPLACE INTO {table} VALUES ({','.join('?' * len(row))})", buf)
                total += len(buf)
                buf.clear()
        if buf:
            db.executemany(f"INSERT OR REPLACE INTO {table} VALUES ({','.join('?' * len(buf[0]))})", buf)
            total += len(buf)
        db.commit()
        log(f"{table}: {total} rows")

    with zipfile.ZipFile(BHL_ZIP) as z:
        # page.txt repeats a page once per page type (e.g. Illustration and Text), so
        # fold the rows into one per page with the types joined.
        pages = {}
        for r in bhl_rows(z, "page"):
            page_id = to_int(r["PageID"])
            if page_id not in page_ids:
                continue
            row = pages.setdefault(page_id, [
                page_id, to_int(r["ItemID"]), to_int(r["SequenceOrder"]), r["Year"], r["Volume"],
                r["PagePrefix"], r["PageNumber"], [],
            ])
            if r["PageTypeName"] and r["PageTypeName"] not in row[7]:
                row[7].append(r["PageTypeName"])
        insert("bhl_page", ((*row[:7], ", ".join(sorted(row[7]))) for row in pages.values()))
        del pages
        insert("bhl_pagename", (
            (int(r["PageID"]), r["NameConfirmed"])
            for r in bhl_rows(z, "pagename") if to_int(r["PageID"]) in page_ids
        ))

        item_ids = {r[0] for r in db.execute("SELECT DISTINCT item_id FROM bhl_page")}
        item_ids |= {r[0] for r in db.execute("SELECT DISTINCT item_id FROM album WHERE item_id IS NOT NULL")}
        insert("bhl_item", (
            (int(r["ItemID"]), to_int(r["TitleID"]), r["VolumeInfo"], r["Year"],
             r["InstitutionName"], r["CopyrightStatus"])
            for r in bhl_rows(z, "item") if to_int(r["ItemID"]) in item_ids
        ))

        title_ids = {r[0] for r in db.execute("SELECT DISTINCT title_id FROM bhl_item")}
        insert("bhl_title", (
            (int(r["TitleID"]), r["FullTitle"], r["ShortTitle"], r["PublicationDetails"],
             r["StartYear"], r["EndYear"], r["LanguageCode"])
            for r in bhl_rows(z, "title") if to_int(r["TitleID"]) in title_ids
        ))
        insert("bhl_creator", (
            (int(r["TitleID"]), to_int(r["CreatorID"]), r["CreatorType"], r["CreatorName"])
            for r in bhl_rows(z, "creator") if to_int(r["TitleID"]) in title_ids
        ))
        insert("bhl_subject", (
            (int(r["TitleID"]), r["Subject"].strip())
            for r in bhl_rows(z, "subject") if to_int(r["TitleID"]) in title_ids
        ))

    db.executescript("""
        CREATE INDEX bhl_pagename_page ON bhl_pagename (page_id);
        CREATE INDEX bhl_creator_title ON bhl_creator (title_id);
        CREATE INDEX bhl_subject_title ON bhl_subject (title_id);
    """)
    db.commit()


STEPS = {"flickr": load_flickr, "bhl": load_bhl}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("steps", nargs="*", choices=[*STEPS, []], help="steps to run (default: all)")
    args = parser.parse_args()

    db = sqlite3.connect(STAGE_DB)
    for step in args.steps or list(STEPS):
        log(f"== {step} ==")
        STEPS[step](db)
    log("done")


if __name__ == "__main__":
    main()
