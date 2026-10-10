#!/usr/bin/env python3
"""Give each photo common tags ("birds", "ferns", ...) and flag the ones to leave out.

Tags come from two levels, recorded in plate_tag.source:
  page   the scientific names found on the plate itself (resolve_taxa.py)
  book   the subjects BHL catalogued the whole book under

Reads data/stage.db and replaces these tables:

  plate_tag       photo_id, tag, source
  plate_exclude   photo_id, reason

Exclusions use BHL's page types and subjects, plus the image_class scores from
classify_images.py for whichever plates have been scored so far.

Usage:
  python3 pipeline/tag_plates.py

Standard library only.
"""
import re
import sqlite3
import time
from collections import Counter
from pathlib import Path

STAGE_DB = Path(__file__).resolve().parent.parent / "data" / "stage.db"

# --- taxon -> tags --------------------------------------------------------------------

ALGAE_PHYLA = {"Rhodophyta", "Chlorophyta", "Charophyta", "Ochrophyta", "Glaucophyta"}
PLANT_CLASSES = {
    "Polypodiopsida": "ferns", "Lycopodiopsida": "ferns",
    "Pinopsida": "conifers", "Cycadopsida": "cycads",
}
PLANT_PHYLA = {"Bryophyta": "mosses", "Marchantiophyta": "mosses", "Anthocerotophyta": "mosses"}
PLANT_FAMILIES = {
    "Orchidaceae": "orchids", "Cactaceae": "cacti", "Arecaceae": "palms",
    "Poaceae": "grasses", "Rosaceae": "rose family", "Liliaceae": "lilies",
}
ANIMAL_PHYLA = {
    "Mollusca": "shells & mollusks", "Cnidaria": "corals & jellyfish",
    "Echinodermata": "starfish & urchins", "Porifera": "sponges",
    "Annelida": "worms", "Platyhelminthes": "worms", "Nematoda": "worms",
    "Brachiopoda": "shells & mollusks",
}
ANIMAL_CLASSES = {
    "Aves": "birds", "Mammalia": "mammals", "Amphibia": "amphibians",
    # The backbone splits the reptiles into their orders at class level.
    "Squamata": "reptiles", "Testudines": "reptiles", "Crocodylia": "reptiles", "Sphenodontia": "reptiles",
    "Elasmobranchii": "fishes", "Holocephali": "fishes", "Myxini": "fishes",
    "Cephalaspidomorphi": "fishes", "Petromyzonti": "fishes", "Coelacanthi": "fishes", "Dipneusti": "fishes",
    "Insecta": "insects", "Arachnida": "spiders & scorpions", "Trilobita": "fossils",
    "Malacostraca": "crustaceans", "Copepoda": "crustaceans", "Ostracoda": "crustaceans",
    "Maxillopoda": "crustaceans", "Branchiopoda": "crustaceans", "Hexanauplia": "crustaceans",
    "Thecostraca": "crustaceans", "Remipedia": "crustaceans", "Cephalocarida": "crustaceans",
    "Cephalopoda": "octopus & squid",
}
ANIMAL_ORDERS = {
    "Lepidoptera": "butterflies & moths", "Coleoptera": "beetles",
    "Odonata": "dragonflies", "Hymenoptera": "bees, wasps & ants",
    "Chiroptera": "bats", "Primates": "primates", "Cetacea": "whales & dolphins",
    "Strigiformes": "owls", "Psittaciformes": "parrots",
}
MICROSCOPIC_KINGDOMS = {"Protozoa", "Bacteria", "Archaea", "Chromista"}


def taxon_tags(kingdom, phylum, class_, order, family):
    tags = set()
    if kingdom == "Plantae" or phylum in ALGAE_PHYLA:
        if phylum in ALGAE_PHYLA:
            tags.add("algae & seaweed")
        else:
            tags.add("plants")
            for value, lookup in ((phylum, PLANT_PHYLA), (class_, PLANT_CLASSES), (family, PLANT_FAMILIES)):
                if value in lookup:
                    tags.add(lookup[value])
    elif kingdom == "Fungi":
        tags.add("lichens" if class_ == "Lecanoromycetes" else "fungi")
    elif kingdom == "Animalia":
        tags.add("animals")
        for value, lookup in ((phylum, ANIMAL_PHYLA), (class_, ANIMAL_CLASSES), (order, ANIMAL_ORDERS)):
            if value in lookup:
                tags.add(lookup[value])
        # Ray-finned fishes have no class in the backbone, only their order.
        if phylum == "Chordata" and class_ is None and order is not None:
            tags.add("fishes")
    elif kingdom in MICROSCOPIC_KINGDOMS:
        tags.add("microscopic life")
    return tags


# --- book subject -> tags -------------------------------------------------------------

# Matched against the lower-cased subject (and the book's title, which is why a few
# French, German, Dutch and Latin words are here) on word starts, so "bird" also
# catches "birds" but "fern" does not catch "Fernando".
SUBJECT_TAGS = [
    (r"bird|ornitholog|oiseaux|v[oö]gel", ["birds", "animals"]),
    (r"mammal|mammif[eè]re|s[aä]uget[h]?ier", ["mammals", "animals"]),
    (r"reptil|herpetolog|snake|lizard|turtle", ["reptiles", "animals"]),
    (r"amphibi|frog", ["amphibians", "animals"]),
    (r"fish|ichthyolog|poissons|fische", ["fishes", "animals"]),
    (r"insect|entomolog|insekt", ["insects", "animals"]),
    (r"lepidoptera|butterfl|moth|papillons|schmetterling", ["butterflies & moths", "insects", "animals"]),
    (r"coleoptera|beetle", ["beetles", "insects", "animals"]),
    (r"arachnid|spider", ["spiders & scorpions", "animals"]),
    (r"crustacea|crab|decapod", ["crustaceans", "animals"]),
    (r"mollus|shell|conch[oy]l|coquill", ["shells & mollusks", "animals"]),
    (r"coral|coelenterat|cnidari|medusa", ["corals & jellyfish", "animals"]),
    (r"echinoderm|starfish|sea urchin", ["starfish & urchins", "animals"]),
    (r"sponge", ["sponges", "animals"]),
    (r"marine animal|marine invertebrat|marine biolog", ["marine life", "animals"]),
    (r"zoolog|animal|dierkunde|fauna", ["animals"]),
    (r"botan|plant|flor[ae]\b|flower|fleurs|pflanzen|horticultur|floricultur|garden|herbal|hort[iu]s", ["plants"]),
    (r"orchid", ["orchids", "plants"]),
    (r"fern|pteridophyt", ["ferns", "plants"]),
    (r"moss|bryolog|bryophyt|liverwort", ["mosses", "plants"]),
    (r"conifer|pinaceae", ["conifers", "plants"]),
    (r"cact", ["cacti", "plants"]),
    (r"palms", ["palms", "plants"]),
    (r"grass", ["grasses", "plants"]),
    (r"rose", ["rose family", "plants"]),
    (r"tree|forest|arboricultur|dendrolog", ["trees", "plants"]),
    (r"fruit|pomolog", ["fruit", "plants"]),
    (r"medicinal plant|botany, medical|materia medica|herbal", ["medicinal plants", "plants"]),
    (r"seed|nurser|bulbs", ["seed & nursery catalogs", "plants"]),
    (r"fung|mushroom|mycolog|champignon|pilze", ["fungi"]),
    (r"lichen", ["lichens"]),
    (r"alga|seaweed|diatom", ["algae & seaweed"]),
    (r"paleontolog|paleobotan|fossil", ["fossils"]),
    (r"microscop|protozoa|infusoria|foraminifera|radiolaria", ["microscopic life"]),
]
SUBJECT_TAGS = [(re.compile(rf"\b(?:{pattern})"), tags) for pattern, tags in SUBJECT_TAGS]

# A book catalogued only under subjects like these is not about living things.
NON_BIOLOGICAL_SUBJECT = re.compile(
    r"\b(?:geolog|mineralog|mines and mineral|astronom|meteorolog|physics|physical sciences|chemistry|"
    r"geophysic|anthropolog|ethnolog|antiquit|portrait|indians|archaeolog|architect)"
)

# --- exclusions -----------------------------------------------------------------------

# BHL page types that mean "there is a picture here"; anything else is front matter or text.
# When a book has no usable subjects or title words, fall back to the kingdom most of
# its named plates belong to, if it is this clear-cut.
BOOK_KINGDOM_MIN_NAMES = 5
BOOK_KINGDOM_MIN_SHARE = 0.8
KINGDOM_TAGS = {"Plantae": "plants", "Animalia": "animals", "Fungi": "fungi"}

PICTURE_PAGE_TYPES = {"Illustration", "Specimen", "Foldout", "Chart"}
HUMAN_NAMES = {"Homo", "Homo sapiens"}
# Flickr license ids the app can't use: 0 is all rights reserved (a handful of photos
# BHL uploaded without a reuse licence); 1-3 are the non-commercial CC licences.
UNUSABLE_LICENSES = {"0": "all rights reserved", "1": "non-commercial licence",
                     "2": "non-commercial licence", "3": "non-commercial licence"}

# Minimum image_class group score to exclude on, chosen by eye from a 2,000-plate
# sample: people are rarely confused with anything else, while captioned plates and
# habitat scenes can look a little like text pages and landscapes.
IMAGE_THRESHOLDS = [
    ("human", 0.5, "people (image)"),
    ("non_biological", 0.7, "not a living thing (image)"),
    ("text", 0.8, "text page (image)"),
]


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def pick_taxon(candidates, *kingdom_votes):
    """Choose between homonyms (the same name in two kingdoms) using context.

    kingdom_votes are Counters of kingdoms, most local first (the plate, then its book).
    Returns None when nothing settles it, so a coin-flip never becomes a tag.
    """
    if len(candidates) == 1:
        return candidates[0]
    for votes in kingdom_votes:
        ranked = sorted(candidates, key=lambda c: -votes.get(c[0], 0))
        if votes.get(ranked[0][0], 0) > votes.get(ranked[1][0], 0):
            return ranked[0]
    return None


def main():
    db = sqlite3.connect(STAGE_DB)
    db.executescript("""
        DROP TABLE IF EXISTS plate_tag;
        DROP TABLE IF EXISTS plate_exclude;
        CREATE TABLE plate_tag (photo_id INTEGER, tag TEXT, source TEXT, PRIMARY KEY (photo_id, tag));
        CREATE TABLE plate_exclude (photo_id INTEGER PRIMARY KEY, reason TEXT);
    """)

    taxa = {}  # name -> [(kingdom, phylum, class, order, family)]
    for name, *classification in db.execute("SELECT name, kingdom, phylum, class, order_, family FROM taxon"):
        taxa.setdefault(name, []).append(tuple(classification))

    photo_title, photo_types, unlicensed = {}, {}, {}
    for photo_id, title_id, page_type, license_id in db.execute("""
        SELECT p.id, i.title_id, b.page_type, p.license FROM photo p
        LEFT JOIN bhl_page b USING (page_id) LEFT JOIN bhl_item i USING (item_id)
    """):
        photo_title[photo_id] = title_id
        if license_id in UNUSABLE_LICENSES:
            unlicensed[photo_id] = UNUSABLE_LICENSES[license_id]
        photo_types[photo_id] = set(page_type.split(", ")) if page_type else set()

    photo_names = {}
    for photo_id, name in db.execute("SELECT photo_id, name FROM photo_name"):
        photo_names.setdefault(photo_id, []).append(name)

    # Kingdom votes from unambiguous names, per plate and per book.
    empty = Counter()
    plate_votes, book_votes = {}, {}
    for photo_id, names in photo_names.items():
        for name in names:
            candidates = taxa[name]
            if len(candidates) == 1 and candidates[0][0]:
                plate_votes.setdefault(photo_id, Counter())[candidates[0][0]] += 1
                book_votes.setdefault(photo_title[photo_id], Counter())[candidates[0][0]] += 1

    book_tags, non_biological_books = {}, set()
    subjects = {}
    for title_id, subject in db.execute("SELECT title_id, subject FROM bhl_subject"):
        subjects.setdefault(title_id, []).append(subject.lower())
    for title_id, full_title in db.execute("SELECT title_id, full_title FROM bhl_title"):
        book_subjects = subjects.get(title_id, [])
        texts = book_subjects + [full_title.lower()]
        tags = {tag for s in texts for pattern, tags in SUBJECT_TAGS if pattern.search(s) for tag in tags}
        if not tags:
            votes = book_votes.get(title_id, empty)
            (kingdom, n), = votes.most_common(1) or [(None, 0)]
            if (n >= BOOK_KINGDOM_MIN_NAMES and n >= BOOK_KINGDOM_MIN_SHARE * sum(votes.values())
                    and kingdom in KINGDOM_TAGS):
                tags = {KINGDOM_TAGS[kingdom]}
        book_tags[title_id] = tags
        if not tags and any(NON_BIOLOGICAL_SUBJECT.search(s) for s in book_subjects):
            non_biological_books.add(title_id)

    image_reason = {}
    if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'image_class'").fetchone():
        for photo_id, *scores in db.execute("SELECT photo_id, human, non_biological, text FROM image_class"):
            for value, (_, threshold, reason) in zip(scores, IMAGE_THRESHOLDS):
                if value >= threshold:
                    image_reason[photo_id] = reason
                    break

    tag_rows, exclude_rows = [], []
    for photo_id, title_id in photo_title.items():
        names = photo_names.get(photo_id, [])
        page_tags = set()
        for name in names:
            taxon = pick_taxon(taxa[name], plate_votes.get(photo_id, empty), book_votes.get(title_id, empty))
            if taxon:
                page_tags |= taxon_tags(*taxon)
        tag_rows += [(photo_id, tag, "page") for tag in page_tags]
        tag_rows += [(photo_id, tag, "book") for tag in book_tags.get(title_id, ()) if tag not in page_tags]

        types = photo_types[photo_id]
        if photo_id in unlicensed:
            exclude_rows.append((photo_id, unlicensed[photo_id]))
        elif "Map" in types:
            exclude_rows.append((photo_id, "map"))
        elif types and not types & PICTURE_PAGE_TYPES:
            exclude_rows.append((photo_id, "not an illustration"))
        elif HUMAN_NAMES & set(names):
            exclude_rows.append((photo_id, "human"))
        elif title_id in non_biological_books and not page_tags:
            exclude_rows.append((photo_id, "non-biological book"))
        elif photo_id in image_reason:
            exclude_rows.append((photo_id, image_reason[photo_id]))

    db.executemany("INSERT INTO plate_tag VALUES (?,?,?)", tag_rows)
    db.executemany("INSERT INTO plate_exclude VALUES (?,?)", exclude_rows)
    db.execute("CREATE INDEX plate_tag_tag ON plate_tag (tag)")
    db.commit()

    total = len(photo_title)
    count = lambda sql: db.execute(sql).fetchone()[0]
    tagged = count("SELECT count(DISTINCT photo_id) FROM plate_tag")
    page_tagged = count("SELECT count(DISTINCT photo_id) FROM plate_tag WHERE source = 'page'")
    log(f"{tagged} of {total} photos tagged ({page_tagged} from names on the plate itself)")
    log(f"{len(exclude_rows)} photos excluded: " + ", ".join(
        f"{reason} {n}" for reason, n in db.execute(
            "SELECT reason, count(*) FROM plate_exclude GROUP BY 1 ORDER BY 2 DESC")))
    log("done")


if __name__ == "__main__":
    main()
