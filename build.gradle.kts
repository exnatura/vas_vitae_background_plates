buildscript {
    repositories {
        mavenCentral()
    }
    dependencies {
        classpath("org.jsoup:jsoup:1.17.2")
        classpath("org.json:json:20240303")
    }
}

import org.jsoup.Jsoup
import org.json.JSONArray
import org.json.JSONObject

// Default gallery for generatePlateUrls; override with -PgalleryUrl=...
val defaultGalleryUrl = "https://www.flickr.com/photos/194446193@N08/galleries/72157725007949999"

tasks.register("generatePlateUrls") {
    group = "plates"
    description = "Scrapes a Flickr gallery's photo pages into plate_urls.json (-PgalleryUrl=... to override)"

    outputs.file(layout.projectDirectory.file("plate_urls.json"))

    doLast {
        val galleryUrl = (findProperty("galleryUrl") as? String ?: defaultGalleryUrl).trimEnd('/')
        val urlsFile = layout.projectDirectory.file("plate_urls.json").asFile
        val userAgent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

        // The gallery id is the numeric segment after /galleries/ in the URL.
        val galleryId = galleryUrl.substringAfter("/galleries/", "").trimEnd('/').substringBefore('/')
        require(galleryId.isNotBlank()) { "Could not parse gallery id from $galleryUrl" }

        // Flickr only server-renders the first 25 photos into the gallery HTML; the
        // rest load client-side via its JSON API. So fetch the page once purely to
        // lift the public web API key (fig.flickr.api.site_key), then page through
        // flickr.galleries.getPhotos for the full membership.
        logger.lifecycle("Fetching $galleryUrl/ for API key")
        val pageHtml = Jsoup.connect("$galleryUrl/")
            .userAgent(userAgent).timeout(20_000).followRedirects(true).get().html()
        val apiKey = Regex("""flickr\.api\.site_key\s*=\s*"([0-9a-f]+)"""")
            .find(pageHtml)?.groupValues?.get(1)
            ?: error("Could not find Flickr api site_key on gallery page")

        val urls = LinkedHashSet<String>()
        var page = 1
        var pages = 1
        do {
            val api = "https://api.flickr.com/services/rest/" +
                "?method=flickr.galleries.getPhotos&api_key=$apiKey&gallery_id=$galleryId" +
                "&per_page=500&page=$page&format=json&nojsoncallback=1"
            logger.lifecycle("Fetching gallery photos page $page")
            val json = Jsoup.connect(api)
                .userAgent(userAgent).timeout(20_000).ignoreContentType(true).get().body().text()

            val resp = JSONObject(json)
            if (resp.optString("stat") != "ok")
                error("Flickr API error: ${resp.optString("message", json.take(200))}")
            val photos = resp.getJSONObject("photos")
            pages = photos.optInt("pages", 1)

            val arr = photos.getJSONArray("photo")
            for (i in 0 until arr.length()) {
                val p = arr.getJSONObject(i)
                urls.add("https://www.flickr.com/photos/${p.getString("owner")}/${p.getString("id")}/")
            }
            page++
            if (page <= pages) Thread.sleep(500) // polite crawl delay
        } while (page <= pages)

        urlsFile.writeText(JSONArray(urls.toList()).toString(2))
        logger.lifecycle("Wrote ${urls.size} URL(s) → ${urlsFile.name}")
    }
}

tasks.register("generatePlatesFromGallery") {
    group = "plates"
    description = "Refreshes plate_urls.json from the Flickr gallery, then regenerates plates.json"
    dependsOn("generatePlateUrls", "generatePlates")
}

tasks.register("generatePlates") {
    group = "plates"
    description = "Scrapes Flickr pages from plate_urls.json and writes plates.json"
    mustRunAfter("generatePlateUrls")

    inputs.file(layout.projectDirectory.file("plate_urls.json"))
    outputs.file(layout.projectDirectory.file("plates.json"))

    doLast {
        val urlsFile = layout.projectDirectory.file("plate_urls.json").asFile
        val platesFile = layout.projectDirectory.file("plates.json").asFile

        val urls = JSONArray(urlsFile.readText())
        val plates = JSONArray()

        for (i in 0 until urls.length()) {
            val pageUrl = urls.getString(i).trimEnd('/')
            logger.lifecycle("[${i + 1}/${urls.length()}] $pageUrl")

            try {
                val doc = Jsoup.connect(pageUrl)
                    .userAgent("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
                    .timeout(20_000)
                    .followRedirects(true)
                    .get()

                // og:image → force _b (1024 px) size
                val rawImage = doc.select("meta[property=og:image]").attr("content")
                val imageUrl = rawImage.replace(Regex("_[a-z]n?\\.jpg$"), "_b.jpg")

                if (imageUrl.isBlank()) {
                    logger.warn("  No image URL found — skipping")
                    continue
                }

                // og:url — canonical page URL
                val canonicalUrl = doc.select("meta[property=og:url]").attr("content")
                    .ifBlank { pageUrl }

                // og:description — bibliographic source text
                val source = doc.select("meta[property=og:description]").attr("content").trim()

                // og:title — strip trailing " | Flickr". Flickr auto-upload titles are
                // filenames (e.g. "n121_w1150"); derive a real title from the citation.
                val rawTitle = doc.select("meta[property=og:title]").attr("content")
                    .removeSuffix(" | Flickr").trim()
                val title = derivePlateTitle(rawTitle, source)

                // JSON-LD (schema.org ImageObject) → author name + license
                var author = ""
                var license = ""
                for (script in doc.select("script[type=application/ld+json]")) {
                    runCatching {
                        val ld = JSONObject(script.data())
                        if (author.isEmpty())
                            author = ld.optJSONObject("author")?.optString("name", "") ?: ""
                        if (license.isEmpty()) {
                            val l = ld.optString("license", "")
                            if (l.isNotEmpty()) license = resolveLicense(l)
                        }
                    }
                }

                // Fallbacks when JSON-LD is missing/absent. Author: the actual photo
                // owner from the page URL (these are biodivlibrary's faves, so the owner
                // may be anyone). License: the page's rel=license link, else "no known
                // copyright restrictions" only for known Flickr Commons members.
                if (author.isBlank()) author = fallbackAuthor(pageUrl)
                if (license.isBlank()) {
                    val relLicense = doc.select("a[rel=license]").attr("abs:href")
                    license = when {
                        relLicense.isNotBlank() -> resolveLicense(relLicense)
                        isCommonsOwner(pageUrl) -> "No known copyright restrictions"
                        else -> ""
                    }
                }

                val id = pageUrl.substringAfterLast('/')
                plates.put(JSONObject().apply {
                    put("id", id)
                    put("imageUrl", imageUrl)
                    if (title.isNotBlank()) put("title", title)
                    if (author.isNotBlank()) put("author", author)
                    if (source.isNotBlank()) put("source", source)
                    if (license.isNotBlank()) put("license", license)
                    put("pageUrl", canonicalUrl)
                })

                logger.lifecycle("  ✓ ${title.take(70)}")
                Thread.sleep(500) // polite crawl delay
            } catch (e: Exception) {
                logger.error("  FAILED: ${e.message}")
            }
        }

        platesFile.writeText(plates.toString(2))
        logger.lifecycle("Wrote ${plates.length()} plate(s) → ${platesFile.name}")
    }
}

fun resolveLicense(url: String): String = when {
    "flickr.com/commons" in url  -> "No known copyright restrictions"
    "publicdomain/zero" in url   -> "CC0 1.0 Universal"
    "licenses/by-nc-nd" in url   -> ccLabel("CC BY-NC-ND", url)
    "licenses/by-nc-sa" in url   -> ccLabel("CC BY-NC-SA", url)
    "licenses/by-nc" in url      -> ccLabel("CC BY-NC", url)
    "licenses/by-nd" in url      -> ccLabel("CC BY-ND", url)
    "licenses/by-sa" in url      -> ccLabel("CC BY-SA", url)
    "licenses/by/" in url        -> ccLabel("CC BY", url)
    else -> url
}

fun ccLabel(prefix: String, url: String): String {
    val version = Regex("""/(\d+\.\d+)/""").find(url)?.groupValues?.get(1) ?: return prefix
    return "$prefix $version"
}

// Flickr Commons members whose plates carry "No known copyright restrictions".
// Owners can appear as a friendly slug (from og:url) or an NSID like 61021753@N02
// (from the gallery API), so list both forms for the same account.
val commonsOwners = setOf("biodivlibrary", "61021753@N02")

/** The owner slug from a Flickr photo URL: flickr.com/photos/<owner>/<id>. */
fun ownerSlug(pageUrl: String): String =
    pageUrl.substringAfter("/photos/", "").substringBefore('/')

/** A display author when JSON-LD has none: a known name for the owner, else the slug. */
fun fallbackAuthor(pageUrl: String): String = when (val owner = ownerSlug(pageUrl)) {
    "biodivlibrary", "61021753@N02", "" -> "Biodiversity Heritage Library"
    else -> owner
}

fun isCommonsOwner(pageUrl: String): Boolean = ownerSlug(pageUrl) in commonsOwners

// BHL/Flickr auto-upload titles look like "n121_w1150" — meaningless to show.
val filenameTitleRegex = Regex("""^[a-z]?\d+(_w\d+)?$""", RegexOption.IGNORE_CASE)

/**
 * A human title for a plate. When the Flickr title is a filename, derive the book
 * name from the start of the BHL citation (reads "<Title>, … <Place> :<Publisher>,…"),
 * falling back to a generic label. Mirrors PlateInfo.displayTitle in the app so the
 * manifest ships clean titles and the app's heuristic is just a safety net.
 */
fun derivePlateTitle(rawTitle: String, source: String): String {
    val t = rawTitle.trim()
    if (t.isNotBlank() && !filenameTitleRegex.matches(t)) return t
    val src = source.trim()
    if (src.isEmpty()) return "Botanical plate"
    val beforeColon = src.substringBefore(" :").trim()
    val beforeComma = src.substringBefore(", ").trim()
    val name = listOf(beforeColon, beforeComma).filter { it.isNotEmpty() }.minByOrNull { it.length }
    return name?.take(80)?.ifBlank { null } ?: "Botanical plate"
}
