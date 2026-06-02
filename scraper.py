"""
AnkerGames -> Hydra Source Scraper
Generates a Hydra-compatible JSON catalog from ankergames.net

Usage:
    python scraper.py              # Full scrape of all games
    python scraper.py --limit 50   # Scrape first 50 games (for testing)
    python scraper.py --resume     # Continue a previous interrupted run
    python scraper.py --no-dl      # Metadata only, skip download URL generation

Output: ankergames.json (Hydra download source format)
Progress: progress.json (saved between runs for resume support)
"""

import requests
import re
import json
import time
import argparse
import sys
from urllib.parse import unquote
from xml.etree import ElementTree
from pathlib import Path

SITEMAPS = [
    "https://ankergames.net/sitemap_post_1.xml",
    "https://ankergames.net/sitemap_post_2.xml",
]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
DELAY_BETWEEN_GAMES = 1.5   # seconds between requests (be polite)
DELAY_AFTER_ERROR = 5.0     # seconds to wait after an error
CSRF_REFRESH_INTERVAL = 50  # refresh CSRF token every N games
# Rate limit: 15 download URL requests per 3 minutes = 1 per 12 seconds min
DELAY_AFTER_DL_URL = 13.0   # seconds to wait after a successful download URL fetch
OUTPUT_FILE = Path("ankergames.json")
PROGRESS_FILE = Path("progress.json")


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def refresh_csrf(session: requests.Session) -> str:
    resp = session.get("https://ankergames.net/csrf-token", timeout=15)
    resp.raise_for_status()
    return resp.json()["token"]


def get_game_urls_from_sitemaps(session: requests.Session) -> list[str]:
    urls = []
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    for sitemap_url in SITEMAPS:
        resp = session.get(sitemap_url, timeout=15)
        resp.raise_for_status()
        root = ElementTree.fromstring(resp.content)
        for url_el in root.findall("sm:url", ns):
            loc = url_el.find("sm:loc", ns).text
            if "/game/" in loc:
                urls.append(loc)
    print(f"[sitemaps] Found {len(urls)} game URLs")
    return urls


def parse_game_page(html: str) -> dict | None:
    """Extract title, fileSize, uploadDate and downloadId from a game page."""
    # Extract JSON-LD VideoGame schema
    jsonld_match = re.search(
        r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL
    )
    if not jsonld_match:
        return None

    try:
        data = json.loads(jsonld_match.group(1))
    except json.JSONDecodeError:
        return None

    items = data if isinstance(data, list) else [data]
    game_data = next((i for i in items if i.get("@type") == "VideoGame"), None)
    if not game_data:
        return None

    # Extract download ID from Alpine.js call
    dl_id_match = re.search(r"generateDownloadUrl\((\d+)\)", html)
    download_id = int(dl_id_match.group(1)) if dl_id_match else None

    raw_date = game_data.get("dateModified") or game_data.get("datePublished", "")
    if raw_date and "T" not in raw_date:
        raw_date = raw_date + "T00:00:00.000Z"

    return {
        "title": game_data.get("name", "").strip(),
        "fileSize": game_data.get("fileSize", "").strip(),
        "uploadDate": raw_date,
        "downloadId": download_id,
    }


def generate_download_url(
    session: requests.Session, csrf: str, download_id: int, referer: str,
    max_retries: int = 6
) -> str | None:
    """Call /generate-download-url/{id} and return the signed AnkerGames URL.
    Rate limit: 15 requests per 3 minutes. Handles 429 with retry_after.
    """
    for attempt in range(max_retries):
        resp = session.post(
            f"https://ankergames.net/generate-download-url/{download_id}",
            json={"g-recaptcha-response": "development-mode"},
            headers={
                "X-CSRF-TOKEN": csrf,
                "X-Requested-With": "XMLHttpRequest",
                "Referer": referer,
                "Accept": "application/json",
            },
            timeout=15,
        )
        if resp.status_code == 419:
            # CSRF expired — caller should refresh and retry
            return None
        if resp.status_code == 429:
            # Rate limited — use retry_after from JSON body
            try:
                data = resp.json()
                wait_sec = int(data.get("retry_after", 35)) + 3
            except Exception:
                wait_sec = 38
            print(f" [429 wait {wait_sec}s]", end="", flush=True)
            time.sleep(wait_sec)
            continue
        if resp.status_code != 200:
            return None
        try:
            data = resp.json()
        except Exception:
            return None
        if data.get("success") and data.get("download_url"):
            return data["download_url"]
        # Legacy 200-with-error rate limit (fallback detection)
        error_msg = data.get("error", "")
        rl_match = re.search(r"wait\s+(\d+)\s+second", error_msg, re.IGNORECASE)
        if rl_match:
            wait_sec = int(rl_match.group(1)) + 2
            print(f" [rl {wait_sec}s]", end="", flush=True)
            time.sleep(wait_sec)
            continue
        # Other error — no point retrying
        return None
    return None


def extract_dlproxy_url(session: requests.Session, ankergames_dl_url: str) -> str | None:
    """Load the treasure-box page and extract the actual download URL.

    Converts node*.datanodes.to:PORT/d/CODE/FILE  →  datanodes.to/CODE/FILE
    so Hydra's native Datanodes downloader can handle it properly.
    Skips dlproxy.uk links (they expire quickly and can't be stored).
    """
    resp = session.get(ankergames_dl_url, timeout=15)
    if resp.status_code != 200:
        return None
    matches = re.findall(r"downloadPage\('([^']+)'", resp.text)
    if not matches:
        return None
    url = unquote(matches[0])

    # Convert node*.datanodes.to:PORT/d/CODE/FILE → datanodes.to/CODE/FILE
    # This gives a permanent URL that Hydra's Datanodes downloader handles natively
    node_match = re.match(
        r"https://node\d+\.datanodes\.to(?::\d+)?/d/([^/?]+)/([^?]+)", url
    )
    if node_match:
        code = node_match.group(1)
        filename = node_match.group(2)
        return f"https://datanodes.to/{code}/{filename}"

    # dlproxy.uk URLs are temporary Cloudflare tunnels that expire — skip them
    if "dlproxy.uk" in url:
        return None

    return url


def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"completed": {}, "failed": []}


def save_progress(progress: dict) -> None:
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False)


def save_output(downloads: list) -> None:
    catalog = {
        "name": "AnkerGames",
        "downloads": downloads,
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=2)
    print(f"\n[output] Saved {len(downloads)} games to {OUTPUT_FILE}")


def scrape(args: argparse.Namespace) -> None:
    session = make_session()
    progress = load_progress() if args.resume else {"completed": {}, "failed": []}

    print("[init] Refreshing CSRF token...")
    csrf = refresh_csrf(session)
    print(f"[init] CSRF ready")

    print("[sitemaps] Fetching game list...")
    all_urls = get_game_urls_from_sitemaps(session)

    if args.limit:
        all_urls = all_urls[: args.limit]
        print(f"[limit] Processing first {args.limit} games")

    total = len(all_urls)
    downloads = []

    # Re-load already completed entries
    for url, entry in progress["completed"].items():
        downloads.append(entry)

    pending = [u for u in all_urls if u not in progress["completed"]]
    print(f"[progress] {len(progress['completed'])} done, {len(pending)} remaining\n")

    for idx, game_url in enumerate(pending, start=1):
        slug = game_url.rstrip("/").split("/")[-1]

        # Refresh CSRF periodically
        if idx % CSRF_REFRESH_INTERVAL == 0:
            try:
                csrf = refresh_csrf(session)
                print(f"  [csrf] Refreshed at game {idx}")
            except Exception:
                pass

        print(f"[{idx}/{len(pending)}] {slug}", end="", flush=True)

        try:
            # Step 1: Fetch game page
            resp = session.get(game_url, timeout=20)
            if resp.status_code != 200:
                print(f" -> HTTP {resp.status_code} SKIP")
                progress["failed"].append(game_url)
                save_progress(progress)
                time.sleep(DELAY_AFTER_ERROR)
                continue

            meta = parse_game_page(resp.text)
            if not meta or not meta.get("title"):
                print(" -> parse error SKIP")
                progress["failed"].append(game_url)
                save_progress(progress)
                continue

            entry = {
                "title": meta["title"],
                "uris": [game_url],   # fallback: game page URL
                "uploadDate": meta["uploadDate"],
                "fileSize": meta["fileSize"],
            }

            # Step 2: Generate actual download URL (optional)
            if not args.no_dl and meta.get("downloadId"):
                dl_signed = generate_download_url(
                    session, csrf, meta["downloadId"], game_url
                )
                if dl_signed:
                    dlproxy = extract_dlproxy_url(session, dl_signed)
                    if dlproxy:
                        entry["uris"] = [dlproxy]
                        print(f" -> got URL", end="")
                    else:
                        print(f" -> no proxy URL", end="")
                else:
                    # CSRF may have expired, refresh and retry once
                    try:
                        csrf = refresh_csrf(session)
                        dl_signed = generate_download_url(
                            session, csrf, meta["downloadId"], game_url
                        )
                        if dl_signed:
                            dlproxy = extract_dlproxy_url(session, dl_signed)
                            if dlproxy:
                                entry["uris"] = [dlproxy]
                                print(f" -> retried OK", end="")
                    except Exception:
                        pass

            print(f" -- {meta['fileSize']}")

            downloads.append(entry)
            progress["completed"][game_url] = entry
            save_progress(progress)

            # Periodically save full output
            if idx % 50 == 0:
                save_output(downloads)

        except KeyboardInterrupt:
            print("\n[interrupt] Saving progress...")
            save_output(downloads)
            save_progress(progress)
            sys.exit(0)
        except Exception as e:
            print(f" -> ERROR: {e}")
            progress["failed"].append(game_url)
            save_progress(progress)
            time.sleep(DELAY_AFTER_ERROR)
            continue

        time.sleep(DELAY_BETWEEN_GAMES)

    save_output(downloads)
    if progress["failed"]:
        print(f"\n[warn] {len(progress['failed'])} games failed: {progress['failed'][:5]}...")


def fix_urls(args: argparse.Namespace) -> None:
    """Second-pass: fill in download URLs for games that only have a page URL (fallback)."""
    progress = load_progress()
    if not progress["completed"]:
        print("[fix] No progress file found. Run the scraper first.")
        return

    session = make_session()
    csrf = refresh_csrf(session)
    print(f"[fix] CSRF ready. Scanning for games missing download URLs...")

    # Find games whose only URI is an ankergames.net/game/ URL
    needs_fix = {
        url: entry
        for url, entry in progress["completed"].items()
        if entry.get("uris") and entry["uris"][0].startswith("https://ankergames.net/game/")
    }
    print(f"[fix] {len(needs_fix)} games need real download URLs\n")

    fixed = 0
    for idx, (game_url, entry) in enumerate(needs_fix.items(), start=1):
        slug = game_url.rstrip("/").split("/")[-1]
        print(f"[{idx}/{len(needs_fix)}] {slug}", end="", flush=True)

        if idx % CSRF_REFRESH_INTERVAL == 0:
            try:
                csrf = refresh_csrf(session)
                print(f"  [csrf] Refreshed")
            except Exception:
                pass

        try:
            resp = session.get(game_url, timeout=20)
            meta = parse_game_page(resp.text)
            if not meta or not meta.get("downloadId"):
                print(" -> no download ID SKIP")
                time.sleep(DELAY_BETWEEN_GAMES)
                continue

            dl_signed = generate_download_url(session, csrf, meta["downloadId"], game_url)
            if not dl_signed:
                csrf = refresh_csrf(session)
                dl_signed = generate_download_url(session, csrf, meta["downloadId"], game_url)

            if dl_signed:
                dlproxy = extract_dlproxy_url(session, dl_signed)
                if dlproxy:
                    entry["uris"] = [dlproxy]
                    progress["completed"][game_url] = entry
                    save_progress(progress)
                    print(f" -> fixed")
                    fixed += 1
                    time.sleep(DELAY_AFTER_DL_URL)  # pace to avoid 429 wall
                    continue
                else:
                    print(f" -> no proxy URL")
            else:
                print(f" -> API error")

        except KeyboardInterrupt:
            print("\n[interrupt] Saving...")
            save_progress(progress)
            break
        except Exception as e:
            print(f" -> ERROR: {e}")

        time.sleep(DELAY_BETWEEN_GAMES)

    print(f"\n[fix] Fixed {fixed}/{len(needs_fix)} games")

    # Rebuild output JSON from progress
    downloads = list(progress["completed"].values())
    save_output(downloads)


def main() -> None:
    parser = argparse.ArgumentParser(description="AnkerGames -> Hydra JSON scraper")
    parser.add_argument("--limit", type=int, default=0, help="Only process first N games")
    parser.add_argument("--resume", action="store_true", help="Resume interrupted scrape")
    parser.add_argument("--no-dl", action="store_true", help="Skip download URL generation (metadata only)")
    parser.add_argument("--fix-urls", action="store_true", help="Second pass: fill in missing download URLs")
    args = parser.parse_args()
    if args.fix_urls:
        fix_urls(args)
    else:
        scrape(args)


if __name__ == "__main__":
    main()
