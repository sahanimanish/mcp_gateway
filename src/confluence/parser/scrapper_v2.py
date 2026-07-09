"""
Confluence PAT-Based Scraper
==============================
Scrapes Confluence pages using REST API + Personal Access Token.
Saves EACH processed page as an individual JSON file formatted as:
{output_dir}/{Title}_{PageID}.json

requests>=2.31.0
beautifulsoup4>=4.12.0
pyyaml>=6.0
lxml>=4.9.0
"""

import json
import time
import yaml
import logging
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs, unquote_plus

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
def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ── Helper ─────────────────────────────────────────────────────────────────────
def sanitize_filename(name: str) -> str:
    """Removes invalid filename characters from the page title."""
    # Replace invalid chars \ / * ? " < > | : with an underscore
    safe_name = re.sub(r'[\\/*?:"<>|]', '_', name)
    return safe_name.strip()[:150]  # Limit length to prevent OS errors


# ── Confluence API Client ──────────────────────────────────────────────────────
class ConfluenceClient:
    def __init__(self, cfg: dict):
        self.base_url = cfg["base_url"].rstrip("/")
        self.pat_token = cfg["pat_token"]
        self.timeout = 30
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.pat_token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        })

    def _api_url(self, path: str) -> str:
        return f"{self.base_url}/rest/api/{path}"

    def _get_with_retry(self, url: str, params: dict = None) -> requests.Response | None:
        """Helper to handle HTTP 429 Too Many Requests rate limits."""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", 5))
                    log.warning(f"  Rate limited! Retrying in {retry_after}s...")
                    time.sleep(retry_after)
                    continue
                resp.raise_for_status()
                return resp
            except requests.HTTPError as e:
                log.error(f"  HTTP error fetching {url}: {e}")
                return None
            except Exception as e:
                if attempt == max_retries - 1:
                    log.error(f"  Max retries reached for {url}: {e}")
                time.sleep(2 ** attempt)
        return None

    def get_page(self, page_id: str) -> dict | None:
        url = self._api_url(
            f"content/{page_id}"
            f"?expand=body.storage,body.view,title,space,version,history.lastUpdated"
        )
        log.info(f"  Fetching page ID: {page_id}")
        resp = self._get_with_retry(url)
        return resp.json() if resp else None

    def get_pages_by_space(self, space_key: str, label: str = None) -> list:
        params = {
            "spaceKey": space_key,
            "type": "page",          
            "expand": "body.view,title,version,history.lastUpdated",
            "limit": 50,
            "start": 0
        }
        if label:
            params["label"] = label

        pages = []
        while True:
            url = self._api_url("content")
            resp = self._get_with_retry(url, params=params)
            if not resp:
                break
                
            data = resp.json()
            results = data.get("results", [])
            pages.extend(results)
            log.info(f"  Fetched {len(pages)} pages so far from space '{space_key}'...")

            if data.get("size", 0) < params["limit"]:
                break
            params["start"] += params["limit"]

        return pages
        
    def get_page_id_by_vanity_url(self, space_key: str, title: str) -> str | None:
        url = self._api_url("content")
        params = {"spaceKey": space_key, "title": title}
        resp = self._get_with_retry(url, params=params)
        if resp:
            results = resp.json().get("results", [])
            if results:
                return str(results[0]["id"])
        return None

    def get_page_html(self, page_data: dict) -> str:
        return (
            page_data.get("body", {}).get("view", {}).get("value", "")
            or page_data.get("body", {}).get("storage", {}).get("value", "")
        )

    def page_url(self, page_data: dict) -> str:
        links = page_data.get("_links", {})
        webui = links.get("webui", "")
        return f"{self.base_url}{webui}" if webui else ""

    def extract_page_metadata(self, page_data: dict) -> dict:
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
            "version_comment": version.get("message") or ""
        }


# ── HTML Parser ────────────────────────────────────────────────────────────────
def safe_text(el, selector: str) -> str:
    found = el.select_one(selector) if selector else None
    return found.get_text(strip=True) if found else ""


def extract_categories(html: str, sel: dict) -> list:
    soup = BeautifulSoup(html, "lxml")
    cat_sel = sel["category"]
    q_sel = sel["questionnaire"]

    categories = []
    headings = soup.select(cat_sel["container"])

    if not headings:
        log.warning("  No category headings found — wrapping full content.")
        categories.append({
            "category": "Uncategorized",
            "questionnaires": _extract_questionnaires_from_container(soup, sel)
        })
        return categories

    for heading in headings:
        category_title = heading.get_text(strip=True)
        questionnaires = []
        
        current_sibling = heading.find_next_sibling()
        while current_sibling and current_sibling.name != heading.name:
            
            q_class = q_sel["container"].lstrip('.')
            if current_sibling.name != "div": 
                q_els = current_sibling.select(q_sel["container"])
            else:
                q_els = []
                if q_class in current_sibling.get("class", []):
                    q_els.append(current_sibling)
                q_els.extend(current_sibling.select(q_sel["container"]))

            for q_el in q_els:
                q_desc_el = q_el.select_one(q_sel.get("description", ""))
                q_desc = q_desc_el.get_text(strip=True) if q_desc_el else ""
                
                desc_links = []
                desc_cfg = sel.get("description_links", {})
                if q_desc_el and desc_cfg.get("scrape_linked_page", False):
                    for a in q_desc_el.find_all(desc_cfg.get("tag", "a"), href=True):
                        desc_links.append({"text": a.get_text(strip=True), "url": a["href"]})

                questionnaires.append({
                    "title": safe_text(q_el, q_sel.get("title", "")),
                    "description": q_desc,
                    "relevant_links": [
                        {"text": a.get_text(strip=True), "url": a.get("href", "")}
                        for a in q_el.select(q_sel.get("relevant_links", "a")) if a.get("href")
                    ],
                    "other_details": safe_text(q_el, q_sel.get("other_details", "")),
                    "_desc_links_to_scrape": desc_links,
                    "linked_pages": []
                })
                
            current_sibling = current_sibling.find_next_sibling()
            
        categories.append({
            "category": category_title,
            "questionnaires": questionnaires
        })

    return categories


def _extract_questionnaires_from_container(container, sel: dict) -> list:
    q_sel = sel["questionnaire"]
    q_containers = container.select(q_sel["container"])
    results = []
    for q_el in q_containers:
        results.append({
            "title": safe_text(q_el, q_sel.get("title", "")),
            "description": safe_text(q_el, q_sel.get("description", "")),
            "_desc_links_to_scrape": [],
            "linked_pages": []
        })
    return results


# ── Link Resolution & Recursion ────────────────────────────────────────────────
def extract_page_id_from_url(url: str, client: ConfluenceClient) -> str | None:
    parsed = urlparse(url)
    path_parts = [p for p in parsed.path.strip("/").split("/") if p]

    if "pages" in path_parts:
        idx = path_parts.index("pages")
        if idx + 1 < len(path_parts) and path_parts[idx + 1].isdigit():
            return path_parts[idx + 1]

    qs = parse_qs(parsed.query)
    if "pageId" in qs:
        return qs["pageId"][0]
        
    if "display" in path_parts:
        idx = path_parts.index("display")
        if idx + 2 < len(path_parts):
            space_key = path_parts[idx + 1]
            title = unquote_plus(path_parts[idx + 2])
            return client.get_page_id_by_vanity_url(space_key, title)

    return None


def _fetch_linked_page_recursive(url: str, client: ConfluenceClient, sel: dict, options: dict, delay: float, current_depth: int) -> dict:
    base_domain = urlparse(client.base_url).netloc
    parsed = urlparse(url)

    if not parsed.netloc:
        url = urljoin(client.base_url, url)
        parsed = urlparse(url)

    if options.get("only_internal_links", True):
        if parsed.netloc and parsed.netloc != base_domain:
            return {"url": url, "skipped": "external link"}

    page_id = extract_page_id_from_url(url, client)
    if not page_id:
        return {"url": url, "note": "no page ID extracted"}

    time.sleep(delay)
    page_data = client.get_page(page_id)
    
    if not page_data:
        return {"url": url, "error": "could not fetch"}

    child_html = client.get_page_html(page_data)
    child_soup = BeautifulSoup(child_html, "lxml")
    linked_sel = sel.get("linked_page", {})
    
    result = {
        "url": url,
        "page_id": page_id,
        "title": page_data.get("title") or "Unknown",
        "body_text": safe_text(child_soup, linked_sel.get("content_container", ".wiki-content")) 
                     or child_soup.get_text(separator=" ", strip=True)[:3000],
        "metadata": client.extract_page_metadata(page_data),
        "nested_links": []
    }

    max_depth = options.get("max_link_depth", 1)
    if current_depth < max_depth:
        nested_a_tags = child_soup.select(linked_sel.get("content_container", ".wiki-content") + " a[href]")
        for a in nested_a_tags:
            nested_url = a.get("href")
            if nested_url and not nested_url.startswith("#"):
                nested_result = _fetch_linked_page_recursive(
                    url=nested_url, client=client, sel=sel, options=options, 
                    delay=delay, current_depth=current_depth + 1
                )
                if "error" not in nested_result and "skipped" not in nested_result:
                     result["nested_links"].append(nested_result)

    return result


def resolve_linked_pages(categories: list, client: ConfluenceClient, sel: dict, options: dict, delay: float) -> list:
    for cat in categories:
        for q in cat["questionnaires"]:
            desc_links = q.pop("_desc_links_to_scrape", [])
            for link in desc_links:
                child_data = _fetch_linked_page_recursive(
                    url=link["url"], client=client, sel=sel, 
                    options=options, delay=delay, current_depth=1
                )
                q["linked_pages"].append(child_data)
    return categories


# ── Main ───────────────────────────────────────────────────────────────────────
def main(config_path: str = "config.yaml"):
    cfg = load_config(config_path)
    client = ConfluenceClient(cfg["confluence"])
    sel = cfg["selectors"]
    options = cfg.get("options", {})
    delay = options.get("request_delay", 0.5)

    # Setup output directory instead of a single output file
    output_dir = Path(cfg.get("output", {}).get("output_dir", "confluence_exports"))
    output_dir.mkdir(parents=True, exist_ok=True)
    indent_level = cfg.get("output", {}).get("indent", 2)

    target = cfg.get("target_pages", {})
    page_ids = target.get("page_ids", [])
    space_key = target.get("space_key")
    label = target.get("label")

    pages_to_process = []

    if page_ids:
        log.info(f"Fetching {len(page_ids)} specific page(s)...")
        for pid in page_ids:
            time.sleep(delay)
            page_data = client.get_page(str(pid))
            if page_data:
                pages_to_process.append(page_data)

    elif space_key:
        log.info(f"Fetching pages from space: {space_key} (label: {label or 'None'})")
        pages_to_process = client.get_pages_by_space(space_key, label)

    else:
        log.error("No target pages configured. Set page_ids or space_key in config.")
        return

    log.info(f"\nProcessing {len(pages_to_process)} page(s)...\n")

    files_created = 0

    # Process and save each page individually
    for page_data in pages_to_process:
        page_title = page_data.get("title") or "Unknown"
        page_id = page_data.get("id") or "unknown_id"
        page_url = client.page_url(page_data)
        
        log.info(f"Parsing page: '{page_title}' (ID: {page_id})")

        html = client.get_page_html(page_data)
        if not html:
            log.warning(f"  No HTML content found for '{page_title}'. Skipping.")
            continue

        categories = extract_categories(html, sel)

        if options.get("scrape_description_links", True):
            categories = resolve_linked_pages(categories, client, sel, options, delay)

        page_meta = client.extract_page_metadata(page_data)
        
        # Build document for this specific page
        page_output = {
            "page_id": page_id,
            "page_title": page_title,
            "source_page": page_url,
            "metadata": page_meta,
            "requirement_categories": categories
        }

        # Save to specific file
        safe_title = sanitize_filename(page_title)
        file_name = f"{safe_title}_{page_id}.json"
        out_path = output_dir / file_name

        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(page_output, f, indent=indent_level, ensure_ascii=False)
            
        log.info(f"  ✅ Saved to: {out_path}")
        files_created += 1

    log.info(f"\n✅ Done! Successfully created {files_created} files in directory: '{output_dir}'")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Confluence PAT-based scraper")
    parser.add_argument("--config", default="config.yaml", help="Path to YAML config file")
    args = parser.parse_args()
    main(args.config)