#!/usr/bin/env python3
"""Look at each plate with CLIP to find the ones that aren't pictures of living things.

Steps (download and embed are resumable):

  download   240px thumbnails from Flickr's CDN -> data/thumbs/<id mod 1000>/<id>.jpg
  embed      one CLIP image embedding per thumbnail -> data/embeddings.db
  score      zero-shot scores from the embeddings -> stage.db table image_class
             (photo_id, label, score, human, non_biological, text)

Each of human / non_biological / text is the summed probability of that group of
prompts; tag_plates.py excludes a plate when one of them passes its threshold.
Embedding is the slow part (hours on a CPU). Scoring takes seconds, so the prompts
below can be reworded and re-scored freely.

Usage:
  data/venv/bin/python pipeline/classify_images.py [download] [embed] [score] [--sample N]

--sample N works on a reproducible random N plates plus up to N/3 from books whose
subjects suggest people or geology, to gauge the filter before a full run.

Needs torch, open_clip_torch and pillow (see data/venv); everything else in the
pipeline is standard library only.
"""
import argparse
import random
import sqlite3
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STAGE_DB = DATA_DIR / "stage.db"
EMBEDDING_DB = DATA_DIR / "embeddings.db"
THUMB_DIR = DATA_DIR / "thumbs"

# Flickr's CDN answers 429 to non-browser user agents.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
DOWNLOAD_THREADS = 6
MODEL, PRETRAINED = "ViT-B-32", "laion2b_s34b_b79k"
BATCH_SIZE = 64

# (group, label, prompt). Groups other than "keep" are reasons to leave a plate out.
PROMPTS = [
    ("keep", "plant", "a botanical illustration of a plant or flower"),
    ("keep", "plant parts", "a botanical drawing of seeds, leaves or dissected flowers"),
    ("keep", "fruit", "an illustration of fruit or vegetables"),
    ("keep", "plant photo", "a photograph of a flower or plant specimen"),
    ("keep", "bird", "a natural history illustration of a bird"),
    ("keep", "mammal", "a natural history illustration of a mammal"),
    ("keep", "reptile", "a natural history illustration of a reptile or amphibian"),
    ("keep", "fish", "a natural history illustration of a fish"),
    ("keep", "insect", "a scientific illustration of insects or butterflies"),
    ("keep", "shell", "an illustration of seashells or molluscs"),
    ("keep", "marine", "an illustration of marine invertebrates, corals or crustaceans"),
    ("keep", "fungi", "an illustration of mushrooms or fungi"),
    ("keep", "fossil", "an illustration of fossils"),
    ("keep", "animal bones", "a drawing of an animal skeleton, skull or bones"),
    ("keep", "microscopic", "a scientific drawing of microscopic organisms or cells"),
    ("keep", "animal anatomy", "an anatomical drawing of the organs of an animal"),
    ("human", "portrait", "a portrait of a person"),
    ("human", "people", "an illustration of people"),
    ("human", "people photo", "a photograph of people"),
    ("human", "human body", "a drawing of the human body"),
    ("human", "ethnographic", "an ethnographic illustration of people in traditional dress"),
    ("non_biological", "map", "a map"),
    ("non_biological", "landscape", "a landscape view of scenery"),
    ("non_biological", "buildings", "a drawing of buildings or a town"),
    ("non_biological", "geology", "a geological diagram of rock strata"),
    ("non_biological", "apparatus", "a drawing of scientific instruments or machinery"),
    ("non_biological", "ship", "a drawing of a ship at sea"),
    ("text", "text", "a page of printed text"),
    ("text", "handwriting", "a handwritten page"),
    ("text", "table", "a table of numbers or a chart"),
    ("text", "title page", "a title page with large lettering"),
    ("text", "blank", "a blank page"),
]

SUSPECT_SUBJECTS = (
    "anthropolog", "ethnolog", "portrait", "indians", "voyages", "travel", "geolog", "antiquit", "anatomy",
)


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def thumb_path(photo_id):
    return THUMB_DIR / f"{photo_id % 1000:03d}" / f"{photo_id}.jpg"


# tag_plates.py marks its image-based exclusions this way; they must stay selected
# here or re-scoring would forget them.
NOT_EXCLUDED_BY_METADATA = "id NOT IN (SELECT photo_id FROM plate_exclude WHERE reason NOT LIKE '%(image)')"


def select_photos(db, sample):
    """(id, server, secret) for every plate not already excluded by metadata."""
    rows = db.execute(
        f"SELECT id, server, secret FROM photo WHERE {NOT_EXCLUDED_BY_METADATA} ORDER BY id"
    ).fetchall()
    if not sample:
        return rows
    rng = random.Random(0)
    chosen = {row[0]: row for row in rng.sample(rows, min(sample, len(rows)))}
    like = " OR ".join("lower(s.subject) LIKE ?" for _ in SUSPECT_SUBJECTS)
    suspects = db.execute(f"""
        SELECT DISTINCT p.id, p.server, p.secret FROM photo p
        JOIN bhl_page b USING (page_id) JOIN bhl_item i USING (item_id) JOIN bhl_subject s USING (title_id)
        WHERE ({like}) AND p.{NOT_EXCLUDED_BY_METADATA} ORDER BY p.id
    """, [f"%{s}%" for s in SUSPECT_SUBJECTS]).fetchall()
    for row in rng.sample(suspects, min(sample // 3, len(suspects))):
        chosen[row[0]] = row
    return sorted(chosen.values())


def download_one(photo):
    photo_id, server, secret = photo
    path = thumb_path(photo_id)
    if path.exists():
        return True
    url = f"https://live.staticflickr.com/{server}/{photo_id}_{secret}_m.jpg"
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
            return True
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                return False
            time.sleep(10 * 2 ** attempt if e.code == 429 else 2)
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(2 * 2 ** attempt)
    return False


def download(db, photos):
    todo = [p for p in photos if not thumb_path(p[0]).exists()]
    log(f"{len(photos) - len(todo)} thumbnails present, downloading {len(todo)}")
    done = failed = 0
    with ThreadPoolExecutor(DOWNLOAD_THREADS) as pool:
        for ok in pool.map(download_one, todo):
            done += 1
            failed += not ok
            if done % 2000 == 0:
                log(f"downloaded {done}/{len(todo)} ({failed} failed)")
    log(f"download: {done - failed} fetched, {failed} failed")


def load_model():
    import open_clip

    model, _, preprocess = open_clip.create_model_and_transforms(MODEL, pretrained=PRETRAINED)
    model.eval()
    return model, preprocess, open_clip.get_tokenizer(MODEL)


def open_embeddings():
    db = sqlite3.connect(EMBEDDING_DB)
    # vec is the L2-normalised image embedding as little-endian float16.
    db.execute("CREATE TABLE IF NOT EXISTS embedding (photo_id INTEGER PRIMARY KEY, vec BLOB)")
    return db


def embed(db, photos):
    import torch
    from PIL import Image

    embeddings = open_embeddings()
    seen = {r[0] for r in embeddings.execute("SELECT photo_id FROM embedding")}
    todo = [p[0] for p in photos if p[0] not in seen and thumb_path(p[0]).exists()]
    log(f"{len(seen)} already embedded, embedding {len(todo)}")
    if not todo:
        return
    model, preprocess, _ = load_model()

    started = time.time()
    for start in range(0, len(todo), BATCH_SIZE):
        ids, images = [], []
        for photo_id in todo[start:start + BATCH_SIZE]:
            try:
                images.append(preprocess(Image.open(thumb_path(photo_id)).convert("RGB")))
                ids.append(photo_id)
            except OSError:
                continue  # unreadable thumbnail; delete the file to have it fetched again
        if not ids:
            continue
        with torch.no_grad():
            features = model.encode_image(torch.stack(images))
            features /= features.norm(dim=-1, keepdim=True)
        vectors = features.numpy().astype("<f2")
        embeddings.executemany(
            "INSERT OR REPLACE INTO embedding VALUES (?,?)",
            [(photo_id, vectors[i].tobytes()) for i, photo_id in enumerate(ids)],
        )
        embeddings.commit()
        if (start // BATCH_SIZE) % 50 == 0:
            done = start + len(ids)
            log(f"embedded {done}/{len(todo)} ({done / (time.time() - started):.1f}/s)")
    log("embed: done")


def score(db, photos):
    import numpy as np
    import torch

    wanted = {p[0] for p in photos}
    rows = [r for r in open_embeddings().execute("SELECT photo_id, vec FROM embedding") if r[0] in wanted]
    if not rows:
        raise SystemExit("No embeddings yet — run the embed step first")
    ids = [r[0] for r in rows]
    images = torch.from_numpy(np.stack([np.frombuffer(r[1], dtype="<f2") for r in rows]).astype("float32"))

    model, _, tokenizer = load_model()
    with torch.no_grad():
        text_features = model.encode_text(tokenizer([prompt for _, _, prompt in PROMPTS]))
        text_features /= text_features.norm(dim=-1, keepdim=True)
    probs = (100.0 * images @ text_features.T).softmax(dim=-1).numpy()

    labels = [label for _, label, _ in PROMPTS]
    best = probs.argmax(axis=1)
    group_scores = [
        probs[:, [group == g for group, _, _ in PROMPTS]].sum(axis=1)
        for g in ("human", "non_biological", "text")
    ]
    db.executescript("""
        DROP TABLE IF EXISTS image_class;
        CREATE TABLE image_class (
            photo_id INTEGER PRIMARY KEY, label TEXT, score REAL,
            human REAL, non_biological REAL, text REAL
        );
    """)
    db.executemany("INSERT INTO image_class VALUES (?,?,?,?,?,?)", [
        (photo_id, labels[best[i]], float(probs[i, best[i]]), *(float(g[i]) for g in group_scores))
        for i, photo_id in enumerate(ids)
    ])
    db.commit()
    log(f"score: {len(ids)} plates scored")


STEPS = {"download": download, "embed": embed, "score": score}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("steps", nargs="*", choices=[*STEPS, []], help="steps to run (default: all)")
    parser.add_argument("--sample", type=int, default=0, help="work on a random sample of this many plates")
    args = parser.parse_args()

    db = sqlite3.connect(STAGE_DB)
    photos = select_photos(db, args.sample)
    for step in args.steps or list(STEPS):
        log(f"== {step} ({len(photos)} plates) ==")
        STEPS[step](db, photos)
    log("done")


if __name__ == "__main__":
    main()
