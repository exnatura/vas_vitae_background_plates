#!/usr/bin/env python3
"""Attach scientific names to photos and classify them against the GBIF backbone.

Names come from two places:
  * BHL's per-page name index (bhl_pagename, found by OCR name-finding)
  * Flickr taxonomy:* machine tags added by BHL's volunteers. Flickr only returns
    these squashed ("taxonomy:binomial=pantheraleo"), so the proper spelling is
    recovered by matching against squashed backbone names.

Reads data/stage.db and data/gbif/simple.txt.gz, and replaces these tables:

  taxon        name, rank, status, kingdom, phylum, class, order_, family
               (a homonym has one row per kingdom; see pick_taxon in tag_plates.py)
  photo_name   photo_id, name, source ('bhl' or 'flickr')

Usage:
  python3 pipeline/resolve_taxa.py

Standard library only.
"""
import gzip
import re
import sqlite3
import time
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STAGE_DB = DATA_DIR / "stage.db"
BACKBONE = DATA_DIR / "gbif" / "simple.txt.gz"

# Columns of the backbone "simple" dump (a PostgreSQL COPY export, \N for null).
COL_KEY, COL_STATUS, COL_RANK = 0, 4, 5
COL_KINGDOM, COL_PHYLUM, COL_CLASS, COL_ORDER, COL_FAMILY = 10, 11, 12, 13, 14
COL_CANONICAL = 19

HIGHER_RANKS = {"KINGDOM", "PHYLUM", "CLASS", "ORDER", "FAMILY"}
# Lower is better when several backbone rows share a name.
STATUS_ORDER = {"ACCEPTED": 0, "DOUBTFUL": 2}  # every kind of synonym is 1

# taxonomy:<rank>=<value>, tolerating the volunteers' misspellings of both halves
# ("taxonony:binomial", "taxonomy:bionomial", "taxonomy:binomia", ...).
TAXON_TAG = re.compile(r"\btaxon[a-z]*:([a-z]+)=(\S+)")
MULTI_WORD_TAGS = ("bi", "tri")  # binomial / trinomial and their misspellings
SINGLE_WORD_TAGS = {"genus", "family", "order", "class", "phylum", "kingdom"}

SCHEMA = """
    DROP TABLE IF EXISTS taxon;
    DROP TABLE IF EXISTS photo_name;
    CREATE TABLE taxon (
        name TEXT, rank TEXT, status TEXT, kingdom TEXT, phylum TEXT, class TEXT,
        order_ TEXT, family TEXT, PRIMARY KEY (name, kingdom)
    );
    CREATE TABLE photo_name (photo_id INTEGER, name TEXT, source TEXT, PRIMARY KEY (photo_id, name));
"""


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def squash(name):
    """Mimic Flickr's tag normalisation: lower-case, letters and digits only."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def clean_bhl_name(name):
    return re.sub(r"\s+", " ", name.replace("×", " ")).strip()


def fallbacks(name):
    """The name itself, then its binomial, then its genus."""
    words = name.split(" ")
    out = [name]
    if len(words) > 2:
        out.append(" ".join(words[:2]))
    if len(words) > 1:
        out.append(words[0])
    return out


def main():
    db = sqlite3.connect(STAGE_DB)
    db.executescript(SCHEMA)

    # --- what do we need to look up? -------------------------------------------------
    bhl_names = {}  # cleaned name -> [page_id]
    for page_id, raw in db.execute("SELECT page_id, name FROM bhl_pagename"):
        name = clean_bhl_name(raw)
        if name:
            bhl_names.setdefault(name, []).append(page_id)
    wanted = {candidate for name in bhl_names for candidate in fallbacks(name)}

    flickr_tags = {}  # (squashed value, multi_word) -> [photo_id]
    for photo_id, machine_tags in db.execute(
        "SELECT id, machine_tags FROM photo WHERE machine_tags LIKE '%taxon%'"
    ):
        for kind, value in TAXON_TAG.findall(machine_tags):
            if kind.startswith(MULTI_WORD_TAGS):
                flickr_tags.setdefault((value, True), []).append(photo_id)
            elif kind in SINGLE_WORD_TAGS:
                flickr_tags.setdefault((value, False), []).append(photo_id)
    wanted_squashed = {value for value, _ in flickr_tags}
    log(f"looking up {len(bhl_names)} BHL names and {len(flickr_tags)} Flickr taxonomy tag values")

    # --- one pass over the backbone ---------------------------------------------------
    higher = {}      # backbone key -> name, for kingdom..family rows
    matches = {}     # canonical name -> {kingdom key: (status order, row fields)}
    unsquashed = {}  # (squashed, multi_word) -> {canonical names}
    with gzip.open(BACKBONE, "rt", encoding="utf-8") as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            name = p[COL_CANONICAL]
            if name == "\\N":
                continue
            if p[COL_RANK] in HIGHER_RANKS and p[COL_STATUS] == "ACCEPTED":
                higher[p[COL_KEY]] = name
            multi_word = " " in name
            hit = name in wanted
            if not hit:
                key = (squash(name), multi_word)
                if key[0] in wanted_squashed and key in flickr_tags:
                    unsquashed.setdefault(key, set()).add(name)
                    hit = True
            elif (squash(name), multi_word) in flickr_tags:
                unsquashed.setdefault((squash(name), multi_word), set()).add(name)
            if hit:
                order = STATUS_ORDER.get(p[COL_STATUS], 1)
                by_kingdom = matches.setdefault(name, {})
                best = by_kingdom.get(p[COL_KINGDOM])
                if best is None or order < best[0]:
                    by_kingdom[p[COL_KINGDOM]] = (order, p)
    log(f"backbone: {len(matches)} names matched, {len(higher)} higher taxa")

    def taxon_name(key):
        return higher.get(key) if key != "\\N" else None

    rows = []
    for name, by_kingdom in matches.items():
        # Keep only the best-status rows; a doubtful homonym in another kingdom
        # shouldn't make a well-established name ambiguous.
        best = min(order for order, _ in by_kingdom.values())
        for order, p in by_kingdom.values():
            if order == best:
                rows.append((
                    name, p[COL_RANK], p[COL_STATUS], taxon_name(p[COL_KINGDOM]),
                    taxon_name(p[COL_PHYLUM]), taxon_name(p[COL_CLASS]),
                    taxon_name(p[COL_ORDER]), taxon_name(p[COL_FAMILY]),
                ))
    db.executemany("INSERT OR REPLACE INTO taxon VALUES (?,?,?,?,?,?,?,?)", rows)

    # --- names per photo --------------------------------------------------------------
    photos_by_page = {}
    for photo_id, page_id in db.execute("SELECT id, page_id FROM photo WHERE page_id IS NOT NULL"):
        photos_by_page.setdefault(page_id, []).append(photo_id)

    photo_names, unmatched_bhl = [], 0
    for name, page_ids in bhl_names.items():
        # Use the most specific form the backbone knows; OCR name-finding also emits
        # plain words ("Verum", "Orchid"), which match nothing and are dropped.
        resolved = next((c for c in fallbacks(name) if c in matches), None)
        if resolved is None:
            unmatched_bhl += 1
            continue
        for page_id in page_ids:
            photo_names += [(photo_id, resolved, "bhl") for photo_id in photos_by_page.get(page_id, [])]
    unmatched_flickr = 0
    for key, photo_ids in flickr_tags.items():
        names = unsquashed.get(key)
        if not names:
            unmatched_flickr += 1
            continue
        # Two spellings can squash to the same tag; prefer an accepted one.
        name = min(names, key=lambda n: (min(o for o, _ in matches[n].values()), n))
        photo_names += [(photo_id, name, "flickr") for photo_id in photo_ids]
    # Flickr tags were added by people looking at the plate, so let them win a tie.
    photo_names.sort(key=lambda r: r[2])
    db.executemany("INSERT OR REPLACE INTO photo_name VALUES (?,?,?)", photo_names)
    db.execute("CREATE INDEX photo_name_name ON photo_name (name)")
    db.commit()

    count = lambda sql: db.execute(sql).fetchone()[0]
    log(f"taxon: {count('SELECT count(*) FROM taxon')} rows "
        f"({count('SELECT count(*) FROM (SELECT name FROM taxon GROUP BY name HAVING count(*) > 1)')} homonyms)")
    log(f"photo_name: {count('SELECT count(*) FROM photo_name')} rows on "
        f"{count('SELECT count(DISTINCT photo_id) FROM photo_name')} photos")
    log(f"unmatched: {unmatched_bhl} BHL names, {unmatched_flickr} Flickr tag values")
    log("done")


if __name__ == "__main__":
    main()
