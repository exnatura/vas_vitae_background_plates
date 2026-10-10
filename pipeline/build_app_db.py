#!/usr/bin/env python3
"""Build the SQLite database the app ships from data/stage.db.

Writes build/plates.db, build/plates.db.gz for hosting, and catalog.json, the small
pointer the app reads to find the current database (see publishing in instructions.md).
Tables:

  plate        one row per background plate; the image is
               https://live.staticflickr.com/<server>/<id>_<secret>_b.jpg
               (drop "_b" when width is NULL: Flickr has no 1024px rendition)
  book         the BHL title a plate comes from
  author       people and organisations credited on books     (book_author)
  taxon        scientific names found on plates                (plate_taxon)
  tag          common tags such as "birds" or "ferns"          (plate_tag)
  license      the reuse licences plates carry, keyed by plate.license
  entity       everything searchable, one row per book/author/taxon/tag
  entity_fts   full-text index over entity (FTS4, docid = entity.id)
  meta         build date and counts

Searching: match the query against entity_fts, show the entities (ordered by
plate_count), then list plates through the link table for the chosen entity's kind.
Popular plates: ORDER BY popularity (1 is the most popular).
A tag's plates: WHERE tag_id = ? AND rank > ? ORDER BY rank, which pages without sorting.

Usage:
  python3 pipeline/build_app_db.py

Standard library only.
"""
import gzip
import hashlib
import json
import re
import shutil
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAGE_DB = ROOT / "data" / "stage.db"
OUT_DB = ROOT / "build" / "plates.db"
CATALOG_POINTER = ROOT / "catalog.json"
# plates.db.gz is published as an asset on a GitHub release tagged catalog-<build date>.
RELEASE_URL = "https://github.com/exnatura/vas_vitae_background_plates/releases/download/catalog-{version}/plates.db.gz"

SCHEMA = """
    CREATE TABLE plate (
        id INTEGER PRIMARY KEY,          -- Flickr photo id
        server INTEGER NOT NULL,
        secret TEXT NOT NULL,
        width INTEGER,                   -- of the 1024px ("_b") rendition
        height INTEGER,
        book_id INTEGER REFERENCES book (id),
        bhl_page INTEGER,                -- biodiversitylibrary.org/page/<bhl_page>
        year INTEGER,
        license INTEGER NOT NULL REFERENCES license (id),
        faves INTEGER NOT NULL,
        views INTEGER NOT NULL,
        popularity INTEGER NOT NULL      -- rank by faves, then views; 1 is the most popular
    );
    CREATE TABLE book (
        id INTEGER PRIMARY KEY,          -- BHL title id: biodiversitylibrary.org/bibliography/<id>
        title TEXT NOT NULL,
        authors TEXT,                    -- display string
        publication TEXT,
        year INTEGER,
        plate_count INTEGER NOT NULL
    );
    CREATE TABLE author (id INTEGER PRIMARY KEY, name TEXT NOT NULL, plate_count INTEGER NOT NULL);
    CREATE TABLE book_author (
        author_id INTEGER, book_id INTEGER, PRIMARY KEY (author_id, book_id)
    ) WITHOUT ROWID;
    CREATE TABLE taxon (
        id INTEGER PRIMARY KEY, name TEXT NOT NULL, rank TEXT, kingdom TEXT, class TEXT,
        family TEXT, plate_count INTEGER NOT NULL
    );
    CREATE TABLE plate_taxon (
        taxon_id INTEGER, plate_id INTEGER, PRIMARY KEY (taxon_id, plate_id)
    ) WITHOUT ROWID;
    CREATE TABLE tag (id INTEGER PRIMARY KEY, name TEXT NOT NULL, plate_count INTEGER NOT NULL);
    CREATE TABLE plate_tag (
        tag_id INTEGER,
        rank INTEGER,                    -- order within the tag: on-plate matches first, then by popularity
        plate_id INTEGER NOT NULL,
        on_plate INTEGER NOT NULL,       -- 1: from a name on the plate itself, 0: from the book's subjects
        PRIMARY KEY (tag_id, rank)
    ) WITHOUT ROWID;
    CREATE TABLE license (id INTEGER PRIMARY KEY, name TEXT NOT NULL, url TEXT);
    CREATE TABLE entity (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,              -- 'book', 'author', 'taxon' or 'tag'
        ref_id INTEGER NOT NULL,         -- id in that kind's table
        label TEXT NOT NULL,
        plate_count INTEGER NOT NULL
    );
    CREATE VIRTUAL TABLE entity_fts USING fts4 (text, tokenize=unicode61, prefix="2,3");
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""
INDEXES = """
    CREATE INDEX plate_book ON plate (book_id);
    CREATE INDEX plate_popularity ON plate (popularity);
    CREATE INDEX plate_tag_plate ON plate_tag (plate_id);
    CREATE INDEX plate_taxon_plate ON plate_taxon (plate_id);
    CREATE INDEX book_author_book ON book_author (book_id);
    CREATE INDEX entity_kind ON entity (kind, plate_count DESC);
"""

# Flickr's license ids, for the ones BHL uses.
LICENSES = [
    (1, "CC BY-NC-SA 2.0", "https://creativecommons.org/licenses/by-nc-sa/2.0/"),
    (4, "CC BY 2.0", "https://creativecommons.org/licenses/by/2.0/"),
    (7, "No known copyright restrictions", "https://www.flickr.com/commons/usage/"),
    (9, "CC0 1.0", "https://creativecommons.org/publicdomain/zero/1.0/"),
    (10, "Public Domain Mark 1.0", "https://creativecommons.org/publicdomain/mark/1.0/"),
]

# Extra words a tag should be found by.
TAG_SEARCH_WORDS = {
    "animals": "animal zoology fauna",
    "plants": "plant botany botanical flora flower flowers",
    "birds": "bird ornithology",
    "insects": "insect bug bugs entomology",
    "shells & mollusks": "shell mollusc molluscs snail snails clam clams conchology",
    "butterflies & moths": "butterfly moth lepidoptera caterpillar",
    "fishes": "fish ichthyology",
    "fossils": "fossil paleontology palaeontology",
    "mammals": "mammal",
    "beetles": "beetle coleoptera",
    "fungi": "fungus mushroom mushrooms toadstool",
    "orchids": "orchid",
    "trees": "tree forest",
    "crustaceans": "crustacean crab crabs lobster shrimp crayfish",
    "reptiles": "reptile snake snakes lizard lizards turtle turtles tortoise crocodile",
    "ferns": "fern",
    "marine life": "sea ocean marine",
    "spiders & scorpions": "spider scorpion arachnid arachnids",
    "microscopic life": "microscope microscopic protozoa plankton diatom",
    "seed & nursery catalogs": "seeds catalogue bulbs",
    "amphibians": "amphibian frog frogs toad toads salamander newt",
    "algae & seaweed": "alga kelp",
    "corals & jellyfish": "coral jellyfish anemone medusa",
    "rose family": "roses",
    "grasses": "grass",
    "bees, wasps & ants": "bee wasp ant hymenoptera",
    "starfish & urchins": "starfish urchin sea star echinoderm",
    "worms": "worm",
    "conifers": "conifer pine fir spruce",
    "parrots": "parrot macaw cockatoo",
    "palms": "palm",
    "sponges": "sponge",
    "primates": "primate monkey monkeys ape apes lemur",
    "lilies": "lily",
    "mosses": "moss liverwort",
    "octopus & squid": "cephalopod cuttlefish nautilus",
    "owls": "owl",
    "dragonflies": "dragonfly damselfly",
    "whales & dolphins": "whale dolphin porpoise",
    "cacti": "cactus succulent",
    "bats": "bat",
    "lichens": "lichen",
    "cycads": "cycad",
}

PERSONAL_NAME_NOISE = re.compile(
    r"^(?:\d{4}.*|-\d{4}|(?:fl|b|d|ca)\. ?\d.*|\d+(?:th|st|nd|rd) cent.*|"
    r"sir|baron|comte|graf|freiherr|ritter|lord|dame|mrs?\.?|dr\.?|"
    r"active .*|approximately .*)$",
    re.IGNORECASE,
)


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def display_author(name, creator_type):
    """'Donovan, E. (Edward), 1768-1837' -> 'Edward Donovan'."""
    name = name.strip().rstrip(",")
    if "Personal" not in creator_type:
        return name.rstrip(".").strip()
    parts = [p.strip() for p in name.split(",")]
    parts = [p for p in parts if p and not PERSONAL_NAME_NOISE.match(p)]
    if not parts:
        return ""
    surname, given = parts[0], " ".join(parts[1:])
    # Prefer the spelled-out form the catalogue gives in brackets after initials.
    spelled_out = re.search(r"\(([^)]+)\)", given)
    if spelled_out:
        given = spelled_out.group(1)
    return f"{given} {surname}".strip().rstrip(".").strip()


def to_year(*values):
    for value in values:
        m = re.search(r"\b(1[4-9]\d\d|20[0-2]\d)\b", value or "")
        if m:
            return int(m.group(1))
    return None


def write_catalog_pointer(gz_path):
    """Describe the gzipped database in catalog.json so the app can find and verify it."""
    db = sqlite3.connect(OUT_DB)
    meta = dict(db.execute("SELECT key, value FROM meta"))
    db.close()
    pointer = {
        "version": meta["built"],
        "schemaVersion": int(meta["schema_version"]),
        "url": RELEASE_URL.format(version=meta["built"]),
        "bytes": gz_path.stat().st_size,
        "sha256": hashlib.sha256(gz_path.read_bytes()).hexdigest(),
        "plates": int(meta["plate_count"]),
    }
    CATALOG_POINTER.write_text(json.dumps(pointer, indent=2) + "\n")
    return pointer


def main():
    OUT_DB.parent.mkdir(exist_ok=True)
    OUT_DB.unlink(missing_ok=True)
    db = sqlite3.connect(OUT_DB)
    db.executescript(SCHEMA)
    db.execute("ATTACH DATABASE ? AS stage", (str(STAGE_DB),))

    # --- plates and books -----------------------------------------------------------
    db.executemany("INSERT INTO license VALUES (?,?,?)", LICENSES)
    db.execute("""
        INSERT INTO plate
        SELECT p.id, CAST(p.server AS INTEGER), p.secret, p.l_width, p.l_height, i.title_id, p.page_id,
               NULL, CAST(p.license AS INTEGER), coalesce(p.faves, 0), coalesce(p.views, 0),
               row_number() OVER (ORDER BY coalesce(p.faves, 0) DESC, coalesce(p.views, 0) DESC, p.id)
        FROM stage.photo p
        LEFT JOIN stage.bhl_page b USING (page_id)
        LEFT JOIN stage.bhl_item i USING (item_id)
        WHERE p.id NOT IN (SELECT photo_id FROM stage.plate_exclude)
    """)
    years = [
        (to_year(page_year, item_year, start_year), photo_id)
        for photo_id, page_year, item_year, start_year in db.execute("""
            SELECT p.id, b.year, i.year, t.start_year FROM stage.photo p
            JOIN stage.bhl_page b USING (page_id) JOIN stage.bhl_item i USING (item_id)
            JOIN stage.bhl_title t USING (title_id)
        """)
    ]
    db.executemany("UPDATE plate SET year = ? WHERE id = ?", years)

    authors, book_authors = {}, {}  # display name -> id; title id -> [author id], main authors first
    for title_id, creator_type, name in db.execute("""
        SELECT title_id, creator_type, name FROM stage.bhl_creator
        WHERE creator_type LIKE '%Personal%' OR creator_type LIKE '%Corporate%'
        ORDER BY title_id, creator_type LIKE 'Main%' DESC, creator_type LIKE '%Personal%' DESC, creator_id
    """):
        display = display_author(name, creator_type)
        if display:
            author_id = authors.setdefault(display, len(authors) + 1)
            if author_id not in book_authors.setdefault(title_id, []):
                book_authors[title_id].append(author_id)
    author_names = {author_id: name for name, author_id in authors.items()}

    book_rows = []
    for title_id, short_title, full_title, publication, start_year, plates in db.execute("""
        SELECT t.title_id, t.short_title, t.full_title, t.publication_details, t.start_year, count(*)
        FROM plate JOIN stage.bhl_title t ON t.title_id = plate.book_id GROUP BY t.title_id
    """):
        names = [author_names[a] for a in book_authors.get(title_id, [])]
        book_rows.append((
            title_id, (short_title or full_title).strip(), ", ".join(names[:3]) or None,
            publication.strip() or None, to_year(start_year, publication), plates,
        ))
    db.executemany("INSERT INTO book VALUES (?,?,?,?,?,?)", book_rows)
    book_plates = {row[0]: row[5] for row in book_rows}

    author_plates = {}
    for title_id, author_ids in book_authors.items():
        for author_id in author_ids:
            if title_id in book_plates:
                author_plates[author_id] = author_plates.get(author_id, 0) + book_plates[title_id]
                db.execute("INSERT INTO book_author VALUES (?,?)", (author_id, title_id))
    db.executemany(
        "INSERT INTO author VALUES (?,?,?)",
        [(author_id, author_names[author_id], plates) for author_id, plates in author_plates.items()],
    )

    # --- taxa and tags --------------------------------------------------------------
    db.execute("""
        INSERT INTO taxon (name, rank, kingdom, class, family, plate_count)
        SELECT n.name, min(t.rank), CASE WHEN count(DISTINCT t.kingdom) = 1 THEN min(t.kingdom) END,
               CASE WHEN count(DISTINCT t.kingdom) = 1 THEN min(t.class) END,
               CASE WHEN count(DISTINCT t.kingdom) = 1 THEN min(t.family) END,
               count(DISTINCT n.photo_id)
        FROM stage.photo_name n JOIN plate ON plate.id = n.photo_id
        JOIN stage.taxon t USING (name) GROUP BY n.name
    """)
    db.execute("""
        INSERT INTO plate_taxon
        SELECT taxon.id, n.photo_id FROM stage.photo_name n
        JOIN plate ON plate.id = n.photo_id JOIN taxon USING (name)
    """)
    db.execute("""
        INSERT INTO tag (name, plate_count)
        SELECT g.tag, count(*) FROM stage.plate_tag g JOIN plate ON plate.id = g.photo_id
        GROUP BY g.tag ORDER BY count(*) DESC
    """)
    db.execute("""
        INSERT INTO plate_tag
        SELECT tag.id,
               row_number() OVER (PARTITION BY tag.id ORDER BY g.source = 'page' DESC, plate.popularity),
               g.photo_id, g.source = 'page'
        FROM stage.plate_tag g
        JOIN plate ON plate.id = g.photo_id JOIN tag ON tag.name = g.tag
    """)

    # --- search ---------------------------------------------------------------------
    entities = []  # (kind, ref_id, label, plate_count, search text)
    for book_id, title, book_author_names, plates in db.execute("SELECT id, title, authors, plate_count FROM book"):
        entities.append(("book", book_id, title, plates, f"{title} {book_author_names or ''}"))
    for author_id, name, plates in db.execute("SELECT id, name, plate_count FROM author"):
        entities.append(("author", author_id, name, plates, name))
    for taxon_id, name, family, plates in db.execute("SELECT id, name, family, plate_count FROM taxon"):
        entities.append(("taxon", taxon_id, name, plates, f"{name} {family or ''}"))
    for tag_id, name, plates in db.execute("SELECT id, name, plate_count FROM tag"):
        entities.append(("tag", tag_id, name, plates, f"{name} {TAG_SEARCH_WORDS.get(name, '')}"))
    for entity_id, (kind, ref_id, label, plates, text) in enumerate(entities, 1):
        db.execute("INSERT INTO entity VALUES (?,?,?,?,?)", (entity_id, kind, ref_id, label, plates))
        db.execute("INSERT INTO entity_fts (docid, text) VALUES (?,?)", (entity_id, text))

    db.executescript(INDEXES)
    counts = {
        table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("plate", "book", "author", "taxon", "tag")
    }
    db.executemany("INSERT INTO meta VALUES (?,?)", [
        ("schema_version", "1"),
        ("built", time.strftime("%Y-%m-%d")),
        *((f"{table}_count", str(n)) for table, n in counts.items()),
    ])
    db.commit()
    db.execute("DETACH DATABASE stage")
    db.execute("INSERT INTO entity_fts (entity_fts) VALUES ('optimize')")
    db.commit()
    db.execute("VACUUM")
    db.close()

    gz_path = OUT_DB.with_name(OUT_DB.name + ".gz")
    with open(OUT_DB, "rb") as src, gzip.open(gz_path, "wb", compresslevel=9) as dst:
        shutil.copyfileobj(src, dst)
    log(", ".join(f"{n} {table}s" for table, n in counts.items()))
    log(f"{OUT_DB.relative_to(ROOT)}: {OUT_DB.stat().st_size / 1e6:.1f} MB, "
        f"gzipped {gz_path.stat().st_size / 1e6:.1f} MB")
    pointer = write_catalog_pointer(gz_path)
    log(f"{CATALOG_POINTER.name}: version {pointer['version']}, publish as release catalog-{pointer['version']}")


if __name__ == "__main__":
    main()
