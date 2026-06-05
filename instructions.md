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
