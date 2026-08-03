"""
Media Gallery Data Fetcher
---------------------------
Pulls your logged media from Letterboxd, AniList (anime + manga), Goodreads,
and MyDramaList, normalizes everything into one JSON file, and saves it to
docs/data.json for the gallery website to read.

You should not need to edit this file except for the USERNAMES section below.
"""

import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests

# ============== EDIT THIS SECTION WITH YOUR OWN INFO ==============

LETTERBOXD_USERNAME = "anokatx"
ANILIST_USERNAME = "anokatx"
GOODREADS_USER_ID = "8067565"
GOODREADS_SHELF = "read"          # the shelf name you use for logged/finished books
MYDRAMALIST_USERNAME = "anokatx"

# ====================================================================

HEADERS = {"User-Agent": "Mozilla/5.0 (personal media gallery script)"}
OUTPUT_PATH = "docs/data.json"


def fetch_url(url, method="GET", timeout=20):
    """Small helper to fetch a URL and return the raw bytes."""
    resp = requests.request(method, url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.content


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
    resp = requests.post(
        "https://graphql.anilist.co",
        json={"query": query, "variables": variables},
        headers={**HEADERS, "Accept": "application/json"},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


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
# 5. MYDRAMALIST (Asian Drama) — unofficial scraper API (kuryana)
# ---------------------------------------------------------------------------
def _key_matches(key, *needles):
    key_lower = key.lower()
    return any(n in key_lower for n in needles)


def _find_first_image(node):
    """Recursively look for the first field that looks like a poster/cover image."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str) and value.strip() and _key_matches(key, "poster", "image", "thumb", "cover"):
                return value.strip()
        for value in node.values():
            found = _find_first_image(value)
            if found:
                return found
    elif isinstance(node, list):
        for entry in node:
            found = _find_first_image(entry)
            if found:
                return found
    return None


def fetch_drama_cover(slug):
    """Cover images aren't in the list view, so we look them up per-show."""
    try:
        raw = fetch_url(f"https://kuryana.tbdh.app/id/{slug}")
        data = json.loads(raw)
        return _find_first_image(data.get("data", data))
    except Exception:
        return None


def fetch_mydramalist():
    items = []
    url = f"https://kuryana.tbdh.app/dramalist/{MYDRAMALIST_USERNAME}"
    try:
        raw = fetch_url(url)
        data = json.loads(raw)
        # Confirmed shape: data -> data -> list -> {CategoryName: {items: [...]}}
        lists = data.get("data", {}).get("list", {})

        entries = []
        for category_data in lists.values():
            entries.extend(category_data.get("items", []))

        for entry in entries:
            title = entry.get("name")
            slug = entry.get("id")
            if not title:
                continue

            rating_10 = safe_float(entry.get("score"))
            rating_5 = round(rating_10 / 2, 1) if rating_10 else None
            link = f"https://mydramalist.com/{slug}" if slug else ""

            cover = ""
            if slug:
                cover = fetch_drama_cover(slug) or ""
                time.sleep(0.3)  # be polite to the free unofficial API

            items.append({
                "title": title,
                "category": "Asian Drama",
                "rating": rating_5,
                "cover": cover,
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
