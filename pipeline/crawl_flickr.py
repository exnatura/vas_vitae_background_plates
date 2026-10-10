#!/usr/bin/env python3
"""Crawl the Biodiversity Heritage Library Flickr account into data/flickr/.

Stages (each resumable — finished files are skipped on re-run):

  photos     every public photo with tags, machine tags, views, faves, description
             -> data/flickr/photos/page_NNNN.json.gz   (raw API pages, 500 photos each)
  albums     the album list (one album per book)
             -> data/flickr/albums/list_NNNN.json.gz
  members    which photos belong to each album
             -> data/flickr/members/<album_id>.json.gz

Usage:
  python3 pipeline/crawl_flickr.py [photos] [albums] [members] [--max-pages N]

No stage arguments runs all three in order. Standard library only.

Like the generatePlateUrls Gradle task, this uses the public site key Flickr embeds
in its own pages; set FLICKR_API_KEY to use a key of your own instead.
"""
import argparse
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

USER_ID = "61021753@N02"  # biodivlibrary
PROFILE_URL = "https://www.flickr.com/photos/biodivlibrary/"
API_URL = "https://api.flickr.com/services/rest/"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
PER_PAGE = 500
PHOTO_EXTRAS = "tags,machine_tags,views,count_faves,description,license,date_upload,o_dims,url_l"
# Flickr allows 3600 calls/hour per key; stay comfortably under it.
DELAY_SECONDS = 1.1
MAX_ATTEMPTS = 6

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "flickr"


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def find_api_key():
    key = os.environ.get("FLICKR_API_KEY")
    if key:
        return key
    html = http_get(PROFILE_URL).decode("utf-8", "replace")
    m = re.search(r'flickr\.api\.site_key\s*=\s*"([0-9a-f]+)"', html)
    if not m:
        sys.exit("Could not find Flickr api site_key on the profile page")
    return m.group(1)


class Flickr:
    def __init__(self, api_key):
        self.api_key = api_key
        self.last_call = 0.0

    def call(self, method, **params):
        query = urllib.parse.urlencode(
            {"method": method, "api_key": self.api_key, "format": "json", "nojsoncallback": 1, **params}
        )
        for attempt in range(1, MAX_ATTEMPTS + 1):
            wait = self.last_call + DELAY_SECONDS - time.time()
            if wait > 0:
                time.sleep(wait)
            self.last_call = time.time()
            try:
                resp = json.loads(http_get(f"{API_URL}?{query}"))
                if resp.get("stat") == "ok":
                    return resp
                error = f"Flickr API error {resp.get('code')}: {resp.get('message')}"
            except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as e:
                error = f"{type(e).__name__}: {e}"
            if attempt == MAX_ATTEMPTS:
                raise RuntimeError(f"{method} {params} failed after {attempt} attempts: {error}")
            backoff = min(300, 5 * 2 ** (attempt - 1))
            log(f"  {error} — retrying in {backoff}s ({attempt}/{MAX_ATTEMPTS})")
            time.sleep(backoff)


def write_json_gz(path, obj):
    """Write atomically so an interrupted run never leaves a partial file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(path)


def read_json_gz(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def crawl_paged(flickr, method, root_key, out_dir, prefix, max_pages, **params):
    """Fetch every page of a paged Flickr listing, one file per page."""
    page, pages = 1, None
    while pages is None or page <= pages:
        path = out_dir / f"{prefix}_{page:04d}.json.gz"
        if path.exists():
            if pages is None:
                pages = int(read_json_gz(path)[root_key]["pages"])
        else:
            resp = flickr.call(method, user_id=USER_ID, per_page=PER_PAGE, page=page, **params)
            pages = int(resp[root_key]["pages"])
            write_json_gz(path, resp)
            log(f"{prefix} {page}/{pages}")
        if max_pages:
            pages = min(pages, max_pages)
        page += 1
    return pages


def crawl_photos(flickr, max_pages):
    # New uploads during the crawl shift later pages down by a few photos; the build
    # step dedupes by id, and anything missed is picked up by the next full crawl.
    pages = crawl_paged(
        flickr, "flickr.people.getPublicPhotos", "photos",
        DATA_DIR / "photos", "page", max_pages, extras=PHOTO_EXTRAS,
    )
    log(f"photos: {pages} page(s) present")


def crawl_albums(flickr, max_pages):
    pages = crawl_paged(
        flickr, "flickr.photosets.getList", "photosets",
        DATA_DIR / "albums", "list", max_pages,
    )
    log(f"albums: {pages} page(s) present")


def crawl_members(flickr, max_pages):
    list_files = sorted((DATA_DIR / "albums").glob("list_*.json.gz"))
    if not list_files:
        sys.exit("No album lists found — run the albums stage first")
    albums = [a for f in list_files for a in read_json_gz(f)["photosets"]["photoset"]]
    if max_pages:
        albums = albums[: max_pages * 5]
    out_dir = DATA_DIR / "members"
    done = 0
    for i, album in enumerate(albums, 1):
        path = out_dir / f"{album['id']}.json.gz"
        if path.exists():
            continue
        ids, page, pages = [], 1, 1
        while page <= pages:
            resp = flickr.call(
                "flickr.photosets.getPhotos",
                user_id=USER_ID, photoset_id=album["id"], per_page=PER_PAGE, page=page,
            )
            photoset = resp["photoset"]
            pages = int(photoset["pages"])
            ids += [p["id"] for p in photoset["photo"]]
            page += 1
        write_json_gz(path, {"id": album["id"], "photo_ids": ids})
        done += 1
        if done % 50 == 0 or i == len(albums):
            log(f"members {i}/{len(albums)}")
    log(f"members: {len(albums)} album(s) present")


STAGES = {"photos": crawl_photos, "albums": crawl_albums, "members": crawl_members}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("stages", nargs="*", choices=[*STAGES, []], help="stages to run (default: all)")
    parser.add_argument("--max-pages", type=int, default=0, help="limit pages per stage, for a trial run")
    args = parser.parse_args()

    flickr = Flickr(find_api_key())
    for stage in args.stages or list(STAGES):
        log(f"== {stage} ==")
        STAGES[stage](flickr, args.max_pages)
    log("done")


if __name__ == "__main__":
    main()
