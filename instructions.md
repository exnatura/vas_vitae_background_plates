# Plate background manifest

The app's background plates are driven by a **static JSON manifest** — no Flickr
API, no API key, no per-request cost. The app fetches one JSON file over plain
HTTP, picks a random entry per launch, downloads the image (disk-cached for
offline reuse), desaturates + inverts it to match the design, and shows an
"about this plate" sheet with the metadata + a link to `pageUrl`.

## 1. Build the manifest

Copy `plates.sample.json` and fill in real entries. Schema per entry:

| field      | required | notes |
|------------|----------|-------|
| `imageUrl` | yes      | Direct image URL. Flickr's CDN works without the API, e.g. `https://live.staticflickr.com/<server>/<id>_<secret>_b.jpg` (`_b` = 1024px). |
| `title`    | no       | Shown in the info sheet. |
| `author`   | no       | e.g. "Biodiversity Heritage Library". |
| `source`   | no       | Bibliographic citation / description (plain text). |
| `license`  | no       | Human-readable, e.g. "No known copyright restrictions". |
| `pageUrl`  | no       | "View source" target — the Flickr/BHL page. Defaults to `imageUrl`. |
| `id`       | no       | Stable cache id; defaults to a hash of `imageUrl`. |

Either a bare array (as in the sample) or `{ "plates": [ ... ] }` is accepted.

BHL plates are public domain / "No known copyright restrictions", so re-listing
and hot-linking them is fine. If you'd rather not depend on Flickr's CDN, commit
the image files to the repo and point `imageUrl` at their raw/jsDelivr URLs too.

## 2. Host it free on GitHub

Commit `plates.json` to a (public) repo, then use one of:

- **raw:** `https://raw.githubusercontent.com/<owner>/<repo>/<branch>/plates.json`
- **jsDelivr (CDN, cached):** `https://cdn.jsdelivr.net/gh/<owner>/<repo>@<branch>/plates.json`

jsDelivr is recommended (CDN-cached, higher rate limits, good for images too).

## 3. Point the app at it

Set `MANIFEST_URL` in
`shared/src/commonMain/kotlin/studio/exnatura/vasvitae/background/PlateSourceConfig.kt`
to that URL. While it's blank the app just keeps the bundled plate overlay.

## Generating from a Flickr faves list

The faves list (e.g. `flickr.com/photos/biodivlibrary/faves`) can be turned into a
manifest with a one-off script — ask Claude to write one. It does **not** need the
paid API: the static image URLs and photo pages are public.

## Full searchable catalogue (`pipeline/`)

`plates.json` above is a hand-picked gallery. The scripts in `pipeline/` instead build
`build/plates.db`, a SQLite database of every BHL plate on Flickr with books, authors,
scientific names, common tags, popularity and a full-text search index. Each script's
docstring describes its inputs, outputs and tables. Run them in this order:

```sh
python3 pipeline/crawl_flickr.py          # ~2.5 h, resumable  -> data/flickr/
curl -L -o data/bhl/data.zip https://www.biodiversitylibrary.org/data/data.zip
curl -L -C - -o data/gbif/simple.txt.gz \
  https://hosted-datasets.gbif.org/datasets/backbone/current/simple.txt.gz   # drops often; re-run until complete
python3 pipeline/build_stage.py           # ~20 min            -> data/stage.db
python3 pipeline/resolve_taxa.py          # scientific names per plate
python3 pipeline/tag_plates.py            # common tags + exclusions
data/venv/bin/python pipeline/classify_images.py   # hours; optional image-based exclusions
python3 pipeline/tag_plates.py            # again, to apply the image scores
python3 pipeline/build_app_db.py          # -> build/plates.db (+ .gz)
```

Everything is standard-library Python except `classify_images.py`, which needs
`torch`, `open_clip_torch` and `pillow` (CPU builds are fine) in `data/venv`.
`data/` and `build/` are git-ignored.

### Publishing a new catalogue

`build_app_db.py` also writes `catalog.json`, which the app fetches (next to
`plates.json`) to learn the current version, download URL, size and checksum. To
publish a build, upload the database as a release asset and commit the pointer:

```sh
gh release create catalog-<version> build/plates.db.gz --title "Plate catalogue <version>"
git add catalog.json && git commit -m "Catalogue <version>" && git push
```

Create the release first: once `catalog.json` is on `main`, apps start downloading from
its URL. Rebuilding changes the checksum, so always publish the `.gz` from the same
build that wrote `catalog.json`.
