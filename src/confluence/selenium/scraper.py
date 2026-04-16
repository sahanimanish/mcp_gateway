"""
Config-Based Dynamic Web Scraper
=================================
Scrapes requirement categories > questionnaires > linked pages
using Selenium + BeautifulSoup, driven entirely by scraper_config.yaml
"""

import json
import time
import yaml
import logging
from pathlib import Path
from urllib.parse import urljoin, urlparse

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from bs4 import BeautifulSoup

# ── Logging setup ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)


# ── Config loader ──────────────────────────────────────────────────────────────
def load_config(config_path: str = "scraper_config.yaml") -> dict:
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


# ── Selenium driver setup ──────────────────────────────────────────────────────
def create_driver(selenium_cfg: dict) -> webdriver.Chrome:
    options = Options()
    if selenium_cfg.get("headless", True):
        options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")

    driver = webdriver.Chrome(options=options)
    driver.implicitly_wait(selenium_cfg.get("implicit_wait", 5))
    return driver


# ── Login ──────────────────────────────────────────────────────────────────────
def login(driver, site_cfg: dict, selenium_cfg: dict) -> bool:
    log.info(f"Navigating to login page: {site_cfg['login_url']}")
    driver.get(site_cfg["login_url"])
    time.sleep(selenium_cfg.get("page_load_wait", 3))

    sel = site_cfg["login_selectors"]
    creds = site_cfg["credentials"]

    try:
        wait = WebDriverWait(driver, selenium_cfg.get("wait_timeout", 10))

        username_el = wait.until(EC.presence_of_element_located(
            (By.CSS_SELECTOR, sel["username_field"])
        ))
        username_el.clear()
        username_el.send_keys(creds["username"])

        password_el = driver.find_element(By.CSS_SELECTOR, sel["password_field"])
        password_el.clear()
        password_el.send_keys(creds["password"])

        driver.find_element(By.CSS_SELECTOR, sel["submit_button"]).click()
        time.sleep(selenium_cfg.get("page_load_wait", 3))

        # Verify login success
        wait.until(EC.presence_of_element_located(
            (By.CSS_SELECTOR, site_cfg["login_success_indicator"])
        ))
        log.info("Login successful.")
        return True

    except TimeoutException:
        log.error("Login failed — success indicator not found after login.")
        return False


# ── Page source helper ─────────────────────────────────────────────────────────
def get_page_source(driver, url: str, wait_time: int = 3) -> BeautifulSoup:
    driver.get(url)
    time.sleep(wait_time)
    return BeautifulSoup(driver.page_source, "html.parser")


# ── Extract text safely ────────────────────────────────────────────────────────
def safe_text(element, selector: str) -> str:
    found = element.select_one(selector)
    return found.get_text(strip=True) if found else ""


# ── Scrape a linked child page ─────────────────────────────────────────────────
def scrape_linked_page(driver, url: str, linked_cfg: dict, wait_time: int) -> dict:
    log.info(f"  → Scraping linked page: {url}")
    try:
        soup = get_page_source(driver, url, wait_time)
        return {
            "url": url,
            "title": safe_text(soup, linked_cfg.get("title", "h1")),
            "body_text": safe_text(soup, linked_cfg.get("body_text", ".content-body")),
            "raw_text": soup.get_text(separator=" ", strip=True)[:3000]  # safety cap
        }
    except Exception as e:
        log.warning(f"  ✗ Failed to scrape {url}: {e}")
        return {"url": url, "error": str(e)}


# ── Scrape one target page ─────────────────────────────────────────────────────
def scrape_page(driver, page_url: str, cfg: dict) -> list:
    log.info(f"Scraping page: {page_url}")
    sel = cfg["selectors"]
    selenium_cfg = cfg["selenium"]
    base_url = cfg["site"]["base_url"]

    soup = get_page_source(driver, page_url, selenium_cfg.get("page_load_wait", 3))
    categories = []

    category_containers = soup.select(sel["category"]["container"])
    log.info(f"  Found {len(category_containers)} categories")

    for cat_el in category_containers:
        category_title = safe_text(cat_el, sel["category"]["title"])
        log.info(f"  Category: {category_title}")

        questionnaires = []
        q_containers = cat_el.select(sel["questionnaire"]["container"])
        log.info(f"    Found {len(q_containers)} questionnaires")

        for q_el in q_containers:
            q_title = safe_text(q_el, sel["questionnaire"]["title"])
            q_desc = safe_text(q_el, sel["questionnaire"]["description"])
            q_details = safe_text(q_el, sel["questionnaire"]["other_details"])

            # Relevant links (dedicated link section)
            relevant_links = []
            for a in q_el.select(sel["questionnaire"]["relevant_links"]):
                href = a.get("href", "")
                if href:
                    relevant_links.append({
                        "text": a.get_text(strip=True),
                        "url": urljoin(base_url, href)
                    })

            # Links inside description — scrape as nested children
            linked_pages = []
            desc_cfg = sel.get("description_links", {})
            if desc_cfg.get("scrape_linked_page", False):
                desc_el = q_el.select_one(sel["questionnaire"]["description"])
                if desc_el:
                    for a in desc_el.find_all(desc_cfg.get("tag", "a"), href=True):
                        href = a["href"]
                        full_url = urljoin(base_url, href)

                        # Only scrape internal or same-domain links (configurable)
                        parsed = urlparse(full_url)
                        base_parsed = urlparse(base_url)
                        if parsed.netloc == base_parsed.netloc or not parsed.netloc:
                            child_data = scrape_linked_page(
                                driver,
                                full_url,
                                sel.get("linked_page", {}),
                                selenium_cfg.get("page_load_wait", 3)
                            )
                            linked_pages.append(child_data)

            questionnaires.append({
                "title": q_title,
                "description": q_desc,
                "relevant_links": relevant_links,
                "other_details": q_details,
                "linked_pages": linked_pages
            })

        categories.append({
            "category": category_title,
            "source_page": page_url,
            "questionnaires": questionnaires
        })

    return categories


# ── Main ───────────────────────────────────────────────────────────────────────
def main(config_path: str = "scraper_config.yaml"):
    cfg = load_config(config_path)
    driver = create_driver(cfg["selenium"])

    try:
        # Login
        if not login(driver, cfg["site"], cfg["selenium"]):
            log.error("Aborting — could not log in.")
            return

        # Scrape all target pages
        all_categories = []
        for page_url in cfg["target_pages"]:
            try:
                page_categories = scrape_page(driver, page_url, cfg)
                all_categories.extend(page_categories)
            except Exception as e:
                log.error(f"Failed to scrape {page_url}: {e}")

        # Build final JSON
        output = {
            "requirement_categories": all_categories
        }

        # Save output
        out_path = Path(cfg["output"]["file_path"])
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=cfg["output"].get("indent", 2), ensure_ascii=False)

        log.info(f"\n✅ Done! Output saved to: {out_path}")
        log.info(f"   Total categories scraped: {len(all_categories)}")

    finally:
        driver.quit()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Config-based dynamic web scraper")
    parser.add_argument(
        "--config", default="scraper_config.yaml",
        help="Path to YAML config file (default: scraper_config.yaml)"
    )
    args = parser.parse_args()
    main(args.config)