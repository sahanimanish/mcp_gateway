"""
Confluence PAT-Based Scraper
==============================
Scrapes Confluence pages using REST API + Personal Access Token.
No Selenium needed — fetches rendered HTML directly via API.

Structure output:
  requirement_categories[]
    └── questionnaires[]
          ├── title, description, relevant_links, other_details
          └── linked_pages[]   ← nested child pages from description links

requests>=2.31.0
beautifulsoup4>=4.12.0
pyyaml>=6.0
lxml>=4.9.0
"""

import json
import time
import yaml
import logging
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)


# ── Config ─────────────────────────────────────────────────────────────────────
def load_config(path: str = "confluence_config.yaml") -> dict:
    with open(path, "r") as f:
        return yaml.safe_load(f)


# ── Confluence API Client ──────────────────────────────────────────────────────
class ConfluenceClient:
    def __init__(self, cfg: dict):
        self.base_url = cfg["base_url"].rstrip("/")
        self.pat_token = cfg["pat_token"]
        self.api_version = cfg.get("api_version", "v2")
        self.timeout = 30
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.pat_token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        })

    def _api_url(self, path: str) -> str:
        return f"{self.base_url}/rest/api/{path}"

    def get_page(self, page_id: str) -> dict | None:
        """Fetch a single page with its rendered HTML body, version, and history."""
        url = self._api_url(
            f"content/{page_id}"
            f"?expand=body.storage,body.view,title,space,version,history.lastUpdated"
        )
        log.info(f"  Fetching page ID: {page_id}")
        try:
            resp = self.session.get(url, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as e:
            log.error(f"  HTTP error fetching page {page_id}: {e}")
            return None
        except Exception as e:
            log.error(f"  Error fetching page {page_id}: {e}")
            return None

    def get_pages_by_space(self, space_key: str, label: str = None) -> list:
        """Fetch all pages in a space, optionally filtered by label."""
        params = {
            "spaceKey": space_key,
            "expand": "body.view,title,version,history.lastUpdated",
            "limit": 50,
            "start": 0
        }
        if label:
            params["label"] = label

        pages = []
        while True:
            url = self._api_url("content")
            resp = self.session.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])
            pages.extend(results)
            log.info(f"  Fetched {len(pages)} pages so far from space '{space_key}'...")

            if data.get("size", 0) < params["limit"]:
                break
            params["start"] += params["limit"]

        return pages

    def get_page_html(self, page_data: dict) -> str:
        """Extract rendered HTML from page data."""
        return (
            page_data.get("body", {})
            .get("view", {})
            .get("value", "")
            or
            page_data.get("body", {})
            .get("storage", {})
            .get("value", "")
        )

    def page_url(self, page_data: dict) -> str:
        """Build the full URL for a page."""
        links = page_data.get("_links", {})
        webui = links.get("webui", "")
        return f"{self.base_url}{webui}" if webui else ""

    def extract_page_metadata(self, page_data: dict) -> dict:
        """
        Extract version number, last modified date, and version comment
        (the editor's note describing what changed in this version).
        """
        version = page_data.get("version", {})
        history = page_data.get("history", {})
        last_updated = history.get("lastUpdated", {})

        return {
            "version_number": version.get("number"),
            "last_modified_date": version.get("when") or last_updated.get("when"),
            "last_modified_by": (
                version.get("by", {}).get("displayName")
                or last_updated.get("by", {}).get("displayName")
            ),
            "version_comment": version.get("message") or ""   # editor's "what changed" note
        }


# ── HTML Parser ────────────────────────────────────────────────────────────────
def safe_text(el, selector: str) -> str:
    found = el.select_one(selector) if selector else None
    return found.get_text(strip=True) if found else ""


def extract_categories(html: str, sel: dict) -> list:
    """
    Parse the page HTML into a list of categories, each with questionnaires.
    Falls back to full-page parse if no category container found.
    """
    soup = BeautifulSoup(html, "lxml")
    cat_sel = sel["category"]
    q_sel = sel["questionnaire"]

    categories = []
    cat_containers = soup.select(cat_sel["container"])

    if not cat_containers:
        log.warning("  No category containers found — treating whole page as one category")
        cat_containers = [soup]

    for cat_el in cat_containers:
        category_title = safe_text(cat_el, cat_sel.get("title"))
        if not category_title:
            # Try direct text if it IS the title element
            category_title = cat_el.get_text(strip=True)

        questionnaires = []
        q_containers = cat_el.select(q_sel["container"])

        for q_el in q_containers:
            q_title = safe_text(q_el, q_sel.get("title", ""))
            q_desc_el = q_el.select_one(q_sel.get("description", ""))
            q_desc = q_desc_el.get_text(strip=True) if q_desc_el else ""
            q_details = safe_text(q_el, q_sel.get("other_details", ""))

            # Relevant links (dedicated section)
            relevant_links = []
            for a in q_el.select(q_sel.get("relevant_links", "a")):
                href = a.get("href", "")
                if href:
                    relevant_links.append({
                        "text": a.get_text(strip=True),
                        "url": href
                    })

            # Links inside description (to be scraped as children later)
            desc_links = []
            desc_cfg = sel.get("description_links", {})
            if q_desc_el and desc_cfg.get("scrape_linked_page", False):
                for a in q_desc_el.find_all(desc_cfg.get("tag", "a"), href=True):
                    desc_links.append({
                        "text": a.get_text(strip=True),
                        "url": a["href"]
                    })

            questionnaires.append({
                "title": q_title,
                "description": q_desc,
                "relevant_links": relevant_links,
                "other_details": q_details,
                "_desc_links_to_scrape": desc_links,   # internal — resolved later
                "linked_pages": []
            })

        categories.append({
            "category": category_title,
            "questionnaires": questionnaires
        })

    return categories


# ── Resolve linked child pages ─────────────────────────────────────────────────
def resolve_linked_pages(
    categories: list,
    client: ConfluenceClient,
    sel: dict,
    options: dict,
    delay: float
) -> list:
    """
    For each questionnaire, visit links found in descriptions,
    fetch their Confluence content, and nest it as linked_pages.
    """
    base_domain = urlparse(client.base_url).netloc
    linked_sel = sel.get("linked_page", {})

    for cat in categories:
        for q in cat["questionnaires"]:
            desc_links = q.pop("_desc_links_to_scrape", [])

            for link in desc_links:
                url = link["url"]
                parsed = urlparse(url)

                # Resolve relative URLs
                if not parsed.netloc:
                    url = urljoin(client.base_url, url)
                    parsed = urlparse(url)

                # Only follow internal Confluence links if configured
                if options.get("only_internal_links", True):
                    if parsed.netloc and parsed.netloc != base_domain:
                        log.info(f"    Skipping external link: {url}")
                        q["linked_pages"].append({"url": url, "skipped": "external link"})
                        continue

                # Try to extract page ID from URL
                page_id = extract_page_id_from_url(url)
                if page_id:
                    time.sleep(delay)
                    page_data = client.get_page(page_id)
                    if page_data:
                        child_html = client.get_page_html(page_data)
                        child_soup = BeautifulSoup(child_html, "lxml")
                        child_meta = client.extract_page_metadata(page_data)
                        q["linked_pages"].append({
                            "url": url,
                            "page_id": page_id,
                            "title": page_data.get("title", ""),
                            "body_text": safe_text(
                                child_soup,
                                linked_sel.get("content_container", ".wiki-content")
                            ) or child_soup.get_text(separator=" ", strip=True)[:3000],
                            "metadata": child_meta
                        })
                    else:
                        q["linked_pages"].append({"url": url, "error": "could not fetch"})
                else:
                    # Non-Confluence link — store as-is
                    q["linked_pages"].append({"url": url, "note": "no page ID extracted"})

    return categories


def extract_page_id_from_url(url: str) -> str | None:
    """
    Try to extract Confluence page ID from various URL formats:
    - /wiki/spaces/SPACE/pages/123456789/Page+Title
    - /pages/viewpage.action?pageId=123456789
    - /wiki/display/SPACE/Page+Title  (no ID — returns None)
    """
    parsed = urlparse(url)
    path_parts = parsed.path.strip("/").split("/")

    # Format: /wiki/spaces/SPACE/pages/{id}/...
    if "pages" in path_parts:
        idx = path_parts.index("pages")
        if idx + 1 < len(path_parts):
            candidate = path_parts[idx + 1]
            if candidate.isdigit():
                return candidate

    # Format: ?pageId=123456789
    from urllib.parse import parse_qs
    qs = parse_qs(parsed.query)
    if "pageId" in qs:
        return qs["pageId"][0]

    return None


# ── Main ───────────────────────────────────────────────────────────────────────
def main(config_path: str = "confluence_config.yaml"):
    cfg = load_config(config_path)
    client = ConfluenceClient(cfg["confluence"])
    sel = cfg["selectors"]
    options = cfg.get("options", {})
    delay = options.get("request_delay", 0.5)

    all_categories = []

    target = cfg.get("target_pages", {})
    page_ids = target.get("page_ids", [])
    space_key = target.get("space_key")
    label = target.get("label")

    # Collect pages to process
    pages_to_process = []

    if page_ids:
        log.info(f"Fetching {len(page_ids)} specific page(s)...")
        for pid in page_ids:
            time.sleep(delay)
            page_data = client.get_page(str(pid))
            if page_data:
                pages_to_process.append(page_data)

    elif space_key:
        log.info(f"Fetching all pages from space: {space_key}" + (f" (label: {label})" if label else ""))
        pages_to_process = client.get_pages_by_space(space_key, label)

    else:
        log.error("No target pages configured. Set page_ids or space_key in config.")
        return

    log.info(f"\nProcessing {len(pages_to_process)} page(s)...\n")

    for page_data in pages_to_process:
        page_title = page_data.get("title", "Unknown")
        page_url = client.page_url(page_data)
        log.info(f"Parsing page: '{page_title}' ({page_url})")

        html = client.get_page_html(page_data)
        if not html:
            log.warning(f"  No HTML content found for page '{page_title}', skipping.")
            continue

        categories = extract_categories(html, sel)

        # Resolve description links as nested children
        if options.get("scrape_description_links", True):
            categories = resolve_linked_pages(categories, client, sel, options, delay)

        # Tag each category with its source page + metadata
        page_meta = client.extract_page_metadata(page_data)
        for cat in categories:
            cat["source_page"] = page_url
            cat["source_page_title"] = page_title
            cat["metadata"] = page_meta

        all_categories.extend(categories)

    # Final output
    output = {"requirement_categories": all_categories}
    out_path = Path(cfg["output"]["file_path"])
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=cfg["output"].get("indent", 2), ensure_ascii=False)

    log.info(f"\n✅ Done! Saved to: {out_path}")
    log.info(f"   Total categories: {len(all_categories)}")
    log.info(f"   Total questionnaires: {sum(len(c['questionnaires']) for c in all_categories)}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Confluence PAT-based scraper")
    parser.add_argument(
        "--config", default="confluence_config.yaml",
        help="Path to YAML config file"
    )
    args = parser.parse_args()
    main(args.config)