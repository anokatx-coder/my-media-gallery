"""
Media Gallery Data Fetcher
---------------------------
Pulls your logged media from Letterboxd, AniList (anime + manga), Goodreads,
Serializd, and MyDramaList, normalizes everything into one JSON file, and
saves it to docs/data.json for the gallery website to read.

You should not need to edit this file except for the USERNAMES section below.
"""

import json
import re
import ssl
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# Some hosts (like the free Render.com box behind Serializd's unofficial API)
# negotiate TLS in a way Python's default strict settings reject. Loosening
# the cipher security level (while still verifying the certificate) fixes
# "SSLV3_ALERT_HANDSHAKE_FAILURE" errors against those hosts.
_SSL_CONTEXT = ssl.create_default_context()
try:
    _SSL_CONTEXT.set_ciphers("DEFAULT@SECLEVEL=1")
except ssl.SSLError:
    pass

# ============== EDIT THIS SECTION WITH YOUR OWN INFO ==============

LETTERBOXD_USERNAME = "anokatx"
ANILIST_USERNAME = "anokatx"
GOODREADS_USER_ID = "8067565"
GOODREADS_SHELF = "read"          # the shelf name you use for logged/finished books
SERIALIZD_USERNAME = "anokatx"
MYDRAMALIST_USERNAME = "anokatx"

# ====================================================================

HEADERS = {"User-Agent": "Mozilla/5.0 (personal media gallery script)"}
OUTPUT_PATH = "docs/data.json"


def fetch_url(url, method="GET", data=None, timeout=20):
    """Small helper to fetch a URL and return the raw bytes."""
    req = Request(url, headers=HEADERS, method=method, data=data)
    with urlopen(req, timeout=timeout, context=_SSL_CONTEXT) as resp:
        return resp.read()


def safe_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 1. LETTERBOXD (Movies) — official public RSS feed
# ---------------------------------------------------------------------------
def fetch_letterboxd():
    items = []
    url = f"https://letterboxd.com/{LETTERBOXD_USERNAME}/rss/"
    try:
        raw = fetch_url(url)
        root = ET.fromstring(raw)
        ns = {"letterboxd": "https://letterboxd.com"}
        for entry in root.findall(".//item"):
            title = entry.findtext("letterboxd:filmTitle", default="", namespaces=ns)
            year = entry.findtext("letterboxd:filmYear", default="", namespaces=ns)
            rating_raw = entry.findtext("letterboxd:memberRating", default="", namespaces=ns)
            link = entry.findtext("link", default="")
            description = entry.findtext("description", default="") or ""

            if not title:
                # Draft/list entries sometimes lack a film title; skip those.
                continue

            # Poster image is embedded as an <img> tag inside the description HTML
            cover = ""
            match = re.search(r'src="([^"]+)"', description)
            if match:
                cover = match.group(1)

            rating_5 = safe_float(rating_raw)  # Letterboxd is already out of 5

            items.append({
                "title": f"{title} ({year})" if year else title,
                "category": "Movie",
                "rating": rating_5,
                "cover": cover,
                "link": link,
                "source": "Letterboxd",
            })
        print(f"[Letterboxd] fetched {len(items)} items")
    except Exception as e:
        print(f"[Letterboxd] FAILED: {e}")
    return items


# ---------------------------------------------------------------------------
# 2 & 3. ANILIST (Anime + Manga) — official public GraphQL API
# ---------------------------------------------------------------------------
def anilist_query(query, variables):
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = Request(
        "https://graphql.anilist.co",
        data=body,
        headers={**HEADERS, "Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=20, context=_SSL_CONTEXT) as resp:
        return json.loads(resp.read())


def get_anilist_score_format():
    query = """
    query ($name: String) {
      User(name: $name) {
        mediaListOptions { scoreFormat }
      }
    }
    """
    try:
        data = anilist_query(query, {"name": ANILIST_USERNAME})
        return data["data"]["User"]["mediaListOptions"]["scoreFormat"]
    except Exception:
        return "POINT_10"  # reasonable fallback


def normalize_anilist_score(score, score_format):
    """Convert AniList's score (whatever format the user set) into out-of-5 stars."""
    if not score:
        return None
    if score_format == "POINT_100":
        return round(score / 20, 1)
    if score_format in ("POINT_10", "POINT_10_DECIMAL"):
        return round(score / 2, 1)
    if score_format == "POINT_5":
        return round(score, 1)
    if score_format == "POINT_3":
        # 1 = bad, 2 = ok, 3 = good -> map to 5-star roughly
        return round(score * (5 / 3), 1)
    return round(score, 1)


def fetch_anilist(media_type):
    """media_type is 'ANIME' or 'MANGA'."""
    items = []
    category = "Anime" if media_type == "ANIME" else "Manga"
    query = """
    query ($name: String, $type: MediaType) {
      MediaListCollection(userName: $name, type: $type) {
        lists {
          entries {
            score
            status
            media {
              title { romaji english }
              coverImage { large }
              siteUrl
            }
          }
        }
      }
    }
    """
    try:
        score_format = get_anilist_score_format()
        data = anilist_query(query, {"name": ANILIST_USERNAME, "type": media_type})
        lists = data["data"]["MediaListCollection"]["lists"]
        for lst in lists:
            for entry in lst["entries"]:
                media = entry["media"]
                title = media["title"]["english"] or media["title"]["romaji"]
                items.append({
                    "title": title,
                    "category": category,
                    "rating": normalize_anilist_score(entry["score"], score_format),
                    "cover": media["coverImage"]["large"],
                    "link": media["siteUrl"],
                    "source": "AniList",
                })
        print(f"[AniList {category}] fetched {len(items)} items")
    except Exception as e:
        print(f"[AniList {category}] FAILED: {e}")
    return items


# ---------------------------------------------------------------------------
# 4. GOODREADS (Books) — shelf RSS feed
# ---------------------------------------------------------------------------
def fetch_goodreads():
    items = []
    url = f"https://www.goodreads.com/review/list_rss/{GOODREADS_USER_ID}?shelf={GOODREADS_SHELF}"
    try:
        raw = fetch_url(url)
        root = ET.fromstring(raw)
        for entry in root.findall(".//item"):
            title = entry.findtext("title", default="").strip()
            cover = entry.findtext("book_large_image_url") or entry.findtext("book_image_url") or ""
            rating_raw = entry.findtext("user_rating", default="0")
            link = entry.findtext("link", default="")

            if not title:
                continue

            rating = safe_float(rating_raw)  # Goodreads is already out of 5
            if rating == 0:
                rating = None

            items.append({
                "title": title,
                "category": "Book",
                "rating": rating,
                "cover": cover,
                "link": link,
                "source": "Goodreads",
            })
        print(f"[Goodreads] fetched {len(items)} items")
    except Exception as e:
        print(f"[Goodreads] FAILED: {e}")
    return items


# ---------------------------------------------------------------------------
# 5. SERIALIZD (Western TV) — unofficial public JSON endpoint
# ---------------------------------------------------------------------------
def fetch_serializd():
    items = []
    base = f"https://www.serializd.onrender.com/api/user/{SERIALIZD_USERNAME}"

    # Pull the ratings first so we can attach them to shows below
    ratings_by_show = {}
    try:
        raw = fetch_url(f"{base}/reviewspage_v3/?sort_by=date_desc&include_ratings=true")
        data = json.loads(raw)
        for review in data.get("items", []):
            show_id = review.get("showId")
            rating = review.get("rating")
            if show_id is not None and rating:
                ratings_by_show[show_id] = rating  # Serializd ratings are out of 10
    except Exception as e:
        print(f"[Serializd ratings] FAILED (continuing without ratings): {e}")

    try:
        page = 1
        while True:
            raw = fetch_url(f"{base}/watchedpage_v2/{page}?sort_by=date_desc")
            data = json.loads(raw)
            for show in data.get("items", []):
                rating_10 = ratings_by_show.get(show.get("showId"))
                rating_5 = round(rating_10 / 2, 1) if rating_10 else None
                items.append({
                    "title": show.get("showName", "Unknown"),
                    "category": "TV Show",
                    "rating": rating_5,
                    "cover": show.get("bannerImage", ""),
                    "link": "",
                    "source": "Serializd",
                })
            total_pages = data.get("totalPages", 1)
            if page >= total_pages:
                break
            page += 1
            time.sleep(0.5)
        print(f"[Serializd] fetched {len(items)} items")
    except Exception as e:
        print(f"[Serializd] FAILED: {e}")
    return items


# ---------------------------------------------------------------------------
# 6. MYDRAMALIST (Asian Drama) — unofficial scraper API (kuryana)
# ---------------------------------------------------------------------------
def _key_matches(key, *needles):
    key_lower = key.lower()
    return any(n in key_lower for n in needles)


def _walk_for_dramas(node, found, seen_titles):
    """Recursively walk the JSON looking for dict entries that look like a
    single drama (has some kind of title field). This makes us resilient to
    this unofficial API changing its exact nesting/key names over time."""
    if isinstance(node, dict):
        title = image = link = None
        rating = None
        for key, value in node.items():
            if isinstance(value, str) and value.strip():
                if title is None and _key_matches(key, "title") and "query" not in key.lower():
                    title = value.strip()
                elif image is None and _key_matches(key, "image", "poster", "thumb", "banner", "cover"):
                    image = value.strip()
                elif link is None and _key_matches(key, "url", "link", "slug"):
                    link = value.strip()
            if rating is None and _key_matches(key, "rating", "score") and "average" not in key.lower():
                rating = safe_float(value)

        if title and title not in seen_titles:
            seen_titles.add(title)
            found.append({"title": title, "rating": rating, "image": image, "link": link})

        for value in node.values():
            _walk_for_dramas(value, found, seen_titles)

    elif isinstance(node, list):
        for entry in node:
            _walk_for_dramas(entry, found, seen_titles)


def fetch_mydramalist():
    items = []
    url = f"https://kuryana.tbdh.app/dramalist/{MYDRAMALIST_USERNAME}"
    try:
        raw = fetch_url(url)
        data = json.loads(raw)

        found = []
        _walk_for_dramas(data.get("data", data), found, set())

        for drama in found:
            rating_10 = drama["rating"]
            rating_5 = round(rating_10 / 2, 1) if rating_10 else None
            link = drama["link"] or ""
            if link and link.startswith("/"):
                link = "https://mydramalist.com" + link
            items.append({
                "title": drama["title"],
                "category": "Asian Drama",
                "rating": rating_5,
                "cover": drama["image"] or "",
                "link": link,
                "source": "MyDramaList",
            })
        print(f"[MyDramaList] fetched {len(items)} items")
        if not items:
            print("[MyDramaList] NOTE: still 0 — raw response (first 1000 chars):")
            print(json.dumps(data)[:1000])
    except Exception as e:
        print(f"[MyDramaList] FAILED: {e}")
    return items


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    all_items = []
    all_items += fetch_letterboxd()
    all_items += fetch_anilist("ANIME")
    all_items += fetch_anilist("MANGA")
    all_items += fetch_goodreads()
    all_items += fetch_serializd()
    all_items += fetch_mydramalist()

    output = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(all_items),
        "items": all_items,
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\nDone. Wrote {len(all_items)} total items to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
