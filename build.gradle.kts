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

tasks.register("generatePlates") {
    group = "plates"
    description = "Scrapes Flickr pages from plate_urls.json and writes plates.json"

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

                // og:title — strip trailing " | Flickr"
                val title = doc.select("meta[property=og:title]").attr("content")
                    .removeSuffix(" | Flickr").trim()

                // og:url — canonical page URL
                val canonicalUrl = doc.select("meta[property=og:url]").attr("content")
                    .ifBlank { pageUrl }

                // og:description — bibliographic source text
                val source = doc.select("meta[property=og:description]").attr("content").trim()

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
