"""
Background processing for Amazon scraping

File: api/scraper.py
"""

import html
import random
import re
import time
import os
from urllib.parse import quote_plus
os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
from asgiref.sync import sync_to_async
import asyncio



import requests
from bs4 import BeautifulSoup
from django.conf import settings
from django.db import transaction

from .models import AnalysisTask, AnalysisResult

import base64
import os
import uuid
from io import BytesIO
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

from PIL import Image
# ============================================================================
# CONFIG
# ============================================================================

AMAZON_BASE_URL = "https://www.amazon.in"

REQUEST_TIMEOUT = 15
MAX_COMPETITORS = 3
_playwright_instance = None
_browser_instance = None


COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9,en-US;q=0.8",
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

# ============================================================================
# MAIN PROCESS
# ============================================================================

def process_analysis(task_id):
    """
    Process Amazon listing analysis and update progress in DB.

    This is called from Celery task:
        scrape_amazon_listing_task.delay(str(task.id))
    """
    task = None

    try:
        task = AnalysisTask.objects.get(id=task_id)
        asin = task.asin.upper().strip()

        update_task(task, "processing", 0, "Starting scrape...")

        # Step 1: Scrape main product
        update_task(task, "processing", 10, "Scraping your product and reviews...")

        your_product = scrape_product(asin)

        if not your_product:
            raise Exception("Failed to scrape product. Amazon may have blocked the request.")

        if your_product.get("title") == "Title not found":
            raise Exception(
                "Product page loaded, but title was not found. "
                "Amazon may have returned captcha/blocked HTML."
            )

        update_task(task, "processing", 20, "Product scraped successfully")

        # Step 2: Find competitors
        update_task(task, "processing", 30, "Finding competitors...")

        search_term = generate_search_term(your_product)
        competitor_asins = scrape_search_results(search_term, asin)

        # Important fix:
        # Do NOT fail immediately if Amazon search returns fewer than 3.
        # Use real scraped ASINs first, then fallback ASINs only to fill missing slots.
        if len(competitor_asins) < MAX_COMPETITORS:
            fallback_asins = get_fallback_competitors(asin)

            for fallback_asin in fallback_asins:
                if (
                    fallback_asin != asin
                    and fallback_asin not in competitor_asins
                    and len(competitor_asins) < MAX_COMPETITORS
                ):
                    competitor_asins.append(fallback_asin)

        competitor_asins = competitor_asins[:MAX_COMPETITORS]

        if not competitor_asins:
            raise Exception(
                "No competitors found from Amazon search or fallback list."
            )

        update_task(
            task,
            "processing",
            40,
            f"Found {len(competitor_asins)} competitor ASINs"
        )

        # Step 3: Scrape competitors
        competitors = []
        progress_steps = [55, 70, 80]

        for i, comp_asin in enumerate(competitor_asins):
            update_task(
                task,
                "processing",
                progress_steps[min(i, len(progress_steps) - 1)],
                f"Scraping competitor {i + 1}/{len(competitor_asins)} product and reviews: {comp_asin}"
            )

            comp_data = scrape_product(comp_asin)

            if comp_data and comp_data.get("title") != "Title not found":
                competitors.append(comp_data)

            time.sleep(random.uniform(2, 4))

        if not competitors:
            raise Exception(
                "Competitor ASINs were found, but competitor product pages could not be scraped."
            )

# Step 4A: Image Analysis with Gemini
        update_task(task, "processing", 82, "Analyzing product images...")
        
        image_analysis = analyze_product_images_with_gemini(your_product, competitors)

        # Step 4B: Text Analysis with DeepSeek
        update_task(task, "processing", 85, "Analyzing listing text...")
        
        text_analysis = analyze_listing(your_product, competitors)
        
        # Step 4C: Combine both analyses
        analysis = text_analysis + image_analysis
        # Step 5: Generate HTML previews
        update_task(task, "processing", 90, "Generating previews...")

        your_html = generate_product_html(your_product)
        comp_htmls = [generate_product_html(comp) for comp in competitors]

        # Step 6: Improve only your product image
        update_task(task, "processing", 95, "Improving product image for ecommerce listing...")

        improved_image_url = improve_product_image_with_openai(
            your_product,
            competitors=competitors,
        )

        if improved_image_url:
            your_product["improved_image_url"] = improved_image_url

        with transaction.atomic():
            AnalysisResult.objects.update_or_create(
                task=task,
                defaults={
                    "asin": asin,
                    "your_product_data": your_product,
                    "competitors_data": competitors,
                    "analysis_text": analysis,
                    "your_product_html": your_html,
                    "competitor_1_html": comp_htmls[0] if len(comp_htmls) > 0 else "",
                    "competitor_2_html": comp_htmls[1] if len(comp_htmls) > 1 else "",
                    "competitor_3_html": comp_htmls[2] if len(comp_htmls) > 2 else "",
                },
            )

            update_task(task, "completed", 100, "Analysis complete!")

    except Exception as e:
        error_message = str(e)

        if task is not None:
            update_task(
                task,
                "failed",
                0,
                "Analysis failed",
                error=error_message,
            )

        print(f"[SCRAPER ERROR] task_id={task_id} error={error_message}")
def extract_rating_near_review_node(node):
    """
    Try to find rating text near a review node.
    """
    possible_text = ""

    try:
        parent = node.parent
        if parent:
            possible_text += " " + clean_text(parent.get_text(" ", strip=True))

        grandparent = parent.parent if parent else None
        if grandparent:
            possible_text += " " + clean_text(grandparent.get_text(" ", strip=True))
    except Exception:
        pass

    match = re.search(r"(\d+\.?\d*)\s*out of\s*5", possible_text, re.I)

    if match:
        return match.group(1)

    star_match = re.search(r"(\d+\.?\d*)\s*stars?", possible_text, re.I)

    if star_match:
        return star_match.group(1)

    return "N/A"

def extract_review_snippets_from_product_page(soup, max_reviews=10):
    """
    Aggressive fallback for product pages where Amazon shows review snippets
    but not full data-hook review cards.
    """
    reviews = []
    seen = set()

    selectors = [
        '[data-hook="review-body"]',
        '.review-text-content',
        '.cr-original-review-text',
        '.reviewText',
        'span[data-hook="review-body"]',

        # broader Amazon review/snippet containers
        '[id*="customer_review"]',
        '[id*="review"]',
        '[class*="review"]',
        '[class*="Review"]',
        '[data-hook*="review"]',
        '.a-expander-content',
    ]

    bad_phrases = [
        "customer reviews",
        "top reviews",
        "there was a problem filtering reviews",
        "see all reviews",
        "write a product review",
        "how customer reviews and ratings work",
        "sort by",
        "filter by",
        "verified purchase",
        "read more",
    ]

    for selector in selectors:
        for item in soup.select(selector):
            text = clean_text(item.get_text(" ", strip=True))

            if not text:
                continue

            lowered = text.lower()

            if len(text) < 35:
                continue

            if len(text) > 1500:
                continue

            if any(bad in lowered for bad in bad_phrases):
                continue

            # avoid duplicate / repeated wrapper text
            fingerprint = text[:120].lower()

            if fingerprint in seen:
                continue

            seen.add(fingerprint)

            rating = extract_rating_near_review_node(item)

            reviews.append({
                "title": f"Visible Review #{len(reviews) + 1}",
                "rating": rating,
                "body": text[:1000],
                "date": "",
                "verified": "verified purchase" in lowered,
            })

            if len(reviews) >= max_reviews:
                return reviews

    return reviews
def extract_visible_reviews_from_product_page(soup, asin, max_reviews=10):
    """
    Extract top/visible reviews directly from the product detail page.
    """
    reviews = []

    selectors = [
        '#cm-cr-dp-review-list div[data-hook="review"]',
        '#customerReviews div[data-hook="review"]',
        '#reviews-medley-footer div[data-hook="review"]',
        'div[data-hook="review"]',
        'div[id^="customer_review"]',
        '.review',
        '.a-section.review',
    ]

    review_containers = []

    for selector in selectors:
        found = soup.select(selector)

        for item in found:
            if item not in review_containers:
                review_containers.append(item)

    print(
        f"[VISIBLE REVIEWS] ASIN={asin} found "
        f"{len(review_containers)} review containers on product page"
    )

    if len(review_containers) == 0:
        debug_filename = f"debug_product_page_reviews_{asin}.html"

        with open(debug_filename, "w", encoding="utf-8") as f:
            f.write(str(soup))

        print(f"[VISIBLE REVIEWS DEBUG] Saved product page HTML: {debug_filename}")

    for container in review_containers:
        if len(reviews) >= max_reviews:
            break

        review = extract_review_from_container(container)

        if review and review.get("body"):
            reviews.append(review)

    if not reviews:
        fallback_reviews = extract_review_snippets_from_product_page(
            soup,
            max_reviews=max_reviews,
        )
        reviews.extend(fallback_reviews)

    print(f"[VISIBLE REVIEWS] ASIN={asin} extracted {len(reviews)} reviews")

    return reviews[:max_reviews]

def update_task(task, status, progress, message, error=None):
    task.status = status
    task.progress = progress
    task.message = message

    if error is not None:
        task.error = error

    # Force synchronous save even in async context
    try:
        task.save(update_fields=["status", "progress", "message", "error", "updated_at"])
    except Exception as e:
        # If we're in async context, use sync_to_async
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We're in async context - need to run in executor
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor() as executor:
                    executor.submit(
                        task.save,
                        update_fields=["status", "progress", "message", "error", "updated_at"]
                    ).result()
            else:
                raise e
        except RuntimeError:
            # No event loop - just raise original error
            raise e

# ============================================================================
# SCRAPING
# ============================================================================

def get_browser():
    """
    Get or create a persistent browser instance.
    Reusing browser is more efficient than creating new one each time.
    """
    global _playwright_instance, _browser_instance
    
    if _browser_instance is None or not _browser_instance.is_connected():
        print("[PLAYWRIGHT] Creating new browser instance...")
        
        if _playwright_instance is None:
            _playwright_instance = sync_playwright().start()
        
        _browser_instance = _playwright_instance.chromium.launch(
            headless=True,  # Run without GUI
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-blink-features=AutomationControlled',
            ]
        )
        
        print("[PLAYWRIGHT] Browser instance created")
    
    return _browser_instance

def scrape_product_reviews_with_playwright(asin, max_reviews=10):
    """
    Scrape reviews using Playwright (more reliable than requests).
    """
    asin = asin.upper().strip()
    review_url = f"{AMAZON_BASE_URL}/product-reviews/{asin}/?reviewerType=all_reviews&sortBy=recent&pageNumber=1"
    
    browser = None
    context = None
    page = None
    
    try:
        print(f"[PLAYWRIGHT REVIEWS] Fetching reviews for ASIN {asin}")
        
        time.sleep(random.uniform(3, 6))
        
        browser = get_browser()
        
        context = browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        
        page = context.new_page()
        
        response = page.goto(review_url, wait_until='domcontentloaded', timeout=30000)
        
        if response.status >= 400:
            print(f"[PLAYWRIGHT REVIEWS] HTTP Error: {response.status}")
            return []
        
        time.sleep(random.uniform(2, 4))
        
        html_content = page.content()
        soup = BeautifulSoup(html_content, "html.parser")
        
        if is_blocked_page(soup, html_content):
            print(f"[PLAYWRIGHT REVIEWS BLOCKED] ASIN={asin}")
            return []
        
        # Extract reviews
        review_containers = find_review_containers(soup)
        print(f"[PLAYWRIGHT REVIEWS] Found {len(review_containers)} review containers")
        
        reviews = []
        for container in review_containers:
            if len(reviews) >= max_reviews:
                break
            
            review = extract_review_from_container(container)
            if review and review.get("body"):
                reviews.append(review)
        
        print(f"[PLAYWRIGHT REVIEWS SUCCESS] Extracted {len(reviews)} reviews for ASIN {asin}")
        
        return reviews[:max_reviews]
        
    except Exception as e:
        print(f"[PLAYWRIGHT REVIEWS ERROR] ASIN={asin} error={e}")
        return []
    
    finally:
        if page:
            try:
                page.close()
            except:
                pass
        
        if context:
            try:
                context.close()
            except:
                pass

def cleanup_browser():
    """
    Clean up browser instance (call this when shutting down).
    """
    global _playwright_instance, _browser_instance
    
    if _browser_instance:
        try:
            _browser_instance.close()
        except:
            pass
        _browser_instance = None
    
    if _playwright_instance:
        try:
            _playwright_instance.stop()
        except:
            pass
        _playwright_instance = None

def scrape_product(asin, retries=3):
    """
    Scrape product details from Amazon using Playwright (headless browser).
    More reliable than requests as it executes JavaScript like a real browser.
    """
    asin = asin.upper().strip()
    amazon_url = f"{AMAZON_BASE_URL}/dp/{asin}"

    for attempt in range(retries):
        browser = None
        context = None
        page = None
        
        try:
            # Add delay between attempts
            if attempt > 0:
                backoff = (2 ** attempt) * 5 + random.uniform(3, 8)
                print(f"[RETRY] Attempt {attempt + 1}/{retries}, waiting {backoff:.1f}s...")
                time.sleep(backoff)
            else:
                # Human-like delay
                time.sleep(random.uniform(3, 7))

            print(f"[PLAYWRIGHT] Fetching ASIN={asin} (attempt {attempt + 1}/{retries})")
            
            # Get persistent browser
            browser = get_browser()
            
            # Create new context (like incognito mode - fresh cookies each time)
            context = browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                locale='en-IN',
                timezone_id='Asia/Kolkata',
            )
            
            # Create new page
            page = context.new_page()
            
            # Set extra headers
            page.set_extra_http_headers({
                "Accept-Language": "en-IN,en-GB;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            })
            
            # Navigate to product page
            print(f"[PLAYWRIGHT] Loading page: {amazon_url}")
            
            response = page.goto(
                amazon_url,
                wait_until='domcontentloaded',  # Wait for DOM
                timeout=30000  # 30 second timeout
            )
            
            # Check response status
            if response.status >= 400:
                print(f"[PLAYWRIGHT] HTTP Error: {response.status}")
                raise Exception(f"HTTP {response.status}")
            
            # Wait a bit for dynamic content
            time.sleep(random.uniform(2, 4))
            
            # Get page HTML
            html_content = page.content()
            
            # Parse with BeautifulSoup
            soup = BeautifulSoup(html_content, "html.parser")

            # Check if blocked
            if is_blocked_page(soup, html_content):
                print(f"[BLOCKED] Amazon blocked request for ASIN {asin} (attempt {attempt + 1})")
                
                # Save screenshot for debugging
                screenshot_path = f"/tmp/blocked_{asin}_{attempt}.png"
                page.screenshot(path=screenshot_path)
                print(f"[DEBUG] Saved screenshot: {screenshot_path}")
                
                if attempt < retries - 1:
                    continue
                else:
                    return None

            # Extract reviews
            product_reviews = extract_visible_reviews_from_product_page(
                soup,
                asin,
                max_reviews=10,
            )

            if not product_reviews:
                print(
                    f"[REVIEWS] No visible reviews found on product page for {asin}, "
                    "trying review page fallback..."
                )
                product_reviews = scrape_product_reviews_with_playwright(asin, max_reviews=10)

            print(f"[SUCCESS] ASIN={asin} scraped successfully (attempt {attempt + 1})")
            print(f"[PRODUCT REVIEWS FINAL] ASIN={asin} reviews_count={len(product_reviews)}")

            # Extract product data
            product_data = {
                "asin": asin,
                "title": extract_title(soup),
                "bullets": extract_bullets(soup),
                "description": extract_description(soup),
                "price": extract_price(soup),
                "rating": extract_rating(soup),
                "reviews_count": extract_reviews_count(soup),
                "url": amazon_url,
                "image_url": extract_image(soup),
                "reviews": product_reviews,
            }

            return product_data

        except PlaywrightTimeout:
            print(f"[TIMEOUT] Page load timeout for ASIN {asin} (attempt {attempt + 1})")
            
            if attempt == retries - 1:
                return None

        except Exception as e:
            print(f"[PLAYWRIGHT ERROR] ASIN={asin} error={e} (attempt {attempt + 1})")
            
            if attempt == retries - 1:
                return None

        finally:
            # Clean up page and context (but keep browser alive)
            if page:
                try:
                    page.close()
                except:
                    pass
            
            if context:
                try:
                    context.close()
                except:
                    pass

    return None

def extract_review_card(card):
    """
    Extract one review card.
    """
    title = ""

    title_elem = card.select_one('[data-hook="review-title"]')
    if title_elem:
        title = clean_text(title_elem.get_text(" ", strip=True))

        # Amazon often includes rating text inside title, clean it lightly
        title = re.sub(r"^\d+\.?\d*\s*out of\s*\d+\s*stars\s*", "", title, flags=re.I)

    rating = "N/A"

    rating_elem = card.select_one('[data-hook="review-star-rating"]')
    if not rating_elem:
        rating_elem = card.select_one('[data-hook="cmps-review-star-rating"]')

    if rating_elem:
        rating_text = clean_text(rating_elem.get_text(" ", strip=True))
        rating_match = re.search(r"(\d+\.?\d*)", rating_text)
        if rating_match:
            rating = rating_match.group(1)

    body = ""

    body_elem = card.select_one('[data-hook="review-body"]')
    if body_elem:
        body = clean_text(body_elem.get_text(" ", strip=True))

    date = ""

    date_elem = card.select_one('[data-hook="review-date"]')
    if date_elem:
        date = clean_text(date_elem.get_text(" ", strip=True))

    verified = False

    verified_elem = card.select_one('[data-hook="avp-badge"]')
    if verified_elem:
        verified = True

    if not body:
        return None

    return {
        "title": title or "Review",
        "rating": rating,
        "body": body[:1000],
        "date": date,
        "verified": verified,
    }
def find_review_containers(soup):
    """
    Try multiple Amazon review card selectors.
    """
    selectors = [
        'div[data-hook="review"]',
        'div[id^="customer_review"]',
        'div.review',
        'li[data-hook="review"]',
        'div.a-section.review',
        '#cm_cr-review_list div[data-hook="review"]',
        '#cm_cr-review_list div[id^="customer_review"]',
    ]

    containers = []

    for selector in selectors:
        found = soup.select(selector)

        for item in found:
            if item not in containers:
                containers.append(item)

    return containers


def is_suspicious_amazon_page(soup, raw_html):
    """
    Detect pages that are not real review pages.
    """
    text = soup.get_text(" ", strip=True).lower()
    html_lower = raw_html.lower()

    suspicious_signals = [
        "sign in",
        "signin",
        "to discuss automated access",
        "api-services-support",
        "sorry",
        "we couldn't find that page",
        "looking for something",
        "click the button below to continue shopping",
        "enter the characters",
        "validatecaptcha",
    ]

    return any(signal in text or signal in html_lower for signal in suspicious_signals)

def scrape_product_reviews(asin, max_reviews=10):
    """
    Scrape recent/top Amazon reviews for a product.

    Best-effort scraper. Amazon may return blocked/different HTML.
    """
    asin = asin.upper().strip()

    review_urls = [
        f"{AMAZON_BASE_URL}/product-reviews/{asin}/?reviewerType=all_reviews&sortBy=recent&pageNumber=1",
        f"{AMAZON_BASE_URL}/product-reviews/{asin}/?reviewerType=all_reviews&sortBy=helpful&pageNumber=1",
        f"{AMAZON_BASE_URL}/dp/product-reviews/{asin}/?reviewerType=all_reviews&sortBy=recent&pageNumber=1",
        f"{AMAZON_BASE_URL}/product-reviews/{asin}/ref=cm_cr_dp_d_show_all_btm?ie=UTF8&reviewerType=all_reviews",
    ]

    session = requests.Session()

    reviews = []

    for review_url in review_urls:
        try:
            time.sleep(random.uniform(3, 6))

            print(f"[REVIEWS] Fetching reviews for ASIN {asin}")
            print(f"[REVIEWS] URL: {review_url}")

            headers = {
                **COMMON_HEADERS,
                "Referer": f"{AMAZON_BASE_URL}/dp/{asin}",
            }

            response = session.get(
                review_url,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
            )

            print(f"[REVIEWS DEBUG] status={response.status_code}")
            print(f"[REVIEWS DEBUG] final_url={response.url}")
            print(f"[REVIEWS DEBUG] html_length={len(response.text)}")

            if response.status_code in [429, 503]:
                print(f"[REVIEWS BLOCKED] ASIN={asin} status={response.status_code}")
                continue

            response.raise_for_status()

            soup = BeautifulSoup(response.text, "html.parser")

            page_title = soup.title.get_text(strip=True) if soup.title else "NO TITLE"
            print(f"[REVIEWS DEBUG] page_title={page_title}")

            # Save HTML when zero reviews are found. This helps debug exact Amazon response.
            should_save_debug = True

            if is_blocked_page(soup, response.text) or is_suspicious_amazon_page(soup, response.text):
                debug_filename = f"debug_reviews_blocked_{asin}.html"
                with open(debug_filename, "w", encoding="utf-8") as f:
                    f.write(response.text)

                print(f"[REVIEWS BLOCKED/SUSPICIOUS] Saved HTML: {debug_filename}")
                continue

            review_containers = find_review_containers(soup)

            print(f"[REVIEWS] Found {len(review_containers)} review containers")

            if not review_containers and should_save_debug:
                debug_filename = f"debug_reviews_zero_{asin}.html"
                with open(debug_filename, "w", encoding="utf-8") as f:
                    f.write(response.text)

                print(f"[REVIEWS DEBUG] Zero containers HTML saved: {debug_filename}")

            for container in review_containers:
                if len(reviews) >= max_reviews:
                    break

                review = extract_review_from_container(container)

                if review and review.get("body"):
                    reviews.append(review)
                    print(
                        f"[REVIEW] Extracted: {review.get('rating')} stars - "
                        f"{review.get('title', '')[:50]}..."
                    )

            if reviews:
                print(f"[REVIEWS SUCCESS] Extracted {len(reviews)} reviews for ASIN {asin}")
                break

        except requests.exceptions.RequestException as e:
            print(f"[REVIEWS REQUEST ERROR] ASIN={asin} error={e}")

        except Exception as e:
            print(f"[REVIEWS SCRAPE ERROR] ASIN={asin} error={e}")

    return reviews[:max_reviews]


def extract_review_from_container(container):
    """
    Extract review data from a single review container.
    
    Amazon review structure (as of 2024):
    - Review title: [data-hook="review-title"] or class "review-title"
    - Rating: [data-hook="review-star-rating"] or class "review-rating"
    - Body: [data-hook="review-body"] or class "review-text-content"
    - Date: [data-hook="review-date"] or class "review-date"
    - Verified: [data-hook="avp-badge"]
    """
    
    # Extract title
    title = ""

    title_elem = (
        container.select_one('[data-hook="review-title"]')
        or container.select_one(".review-title")
    )


    if not title_elem:
        title_elem = container.find("span", {"data-hook": "review-title"})
    if not title_elem:
        title_elem = container.find(class_="review-title")
    
    if title_elem:
        title_parts = [
            clean_text(span.get_text(" ", strip=True))
            for span in title_elem.find_all("span")
            if clean_text(span.get_text(" ", strip=True))
        ]

        if title_parts:
            title = title_parts[-1]
        else:
            title = clean_text(title_elem.get_text(" ", strip=True))

        title = re.sub(
            r"^\d+\.?\d*\s+out of\s+\d+\s+stars\s*",
            "",
            title,
            flags=re.I,
        ).strip()
    # if title_elem:
    #     title_text = clean_text(title_elem.get_text(" ", strip=True))
    #     # Remove rating prefix like "5.0 out of 5 stars"
    #     title = re.sub(r'^\d+\.?\d*\s+out of\s+\d+\s+stars\s*', '', title_text, flags=re.I)
    
    # Extract rating
    rating = "N/A"
    rating_elem = container.find("i", {"data-hook": "review-star-rating"})
    if not rating_elem:
        rating_elem = container.find("span", {"data-hook": "review-star-rating"})
    if not rating_elem:
        rating_elem = container.find(class_="review-rating")
    
    if rating_elem:
        rating_text = clean_text(rating_elem.get_text(" ", strip=True))
        rating_match = re.search(r'(\d+\.?\d*)', rating_text)
        if rating_match:
            rating = rating_match.group(1)
    
    # Extract body (main review text)
    body = ""
    body_elem = container.find("span", {"data-hook": "review-body"})
    if not body_elem:
        body_elem = container.find("div", {"data-hook": "review-body"})
    if not body_elem:
        body_elem = container.find(class_="review-text-content")
    
    if body_elem:
        # Remove "Read more" links and extra spans
        for unwanted in body_elem.find_all(['span', 'a'], class_=lambda x: x and 'read-more' in x.lower()):
            unwanted.decompose()
        
        body = clean_text(body_elem.get_text(" ", strip=True))
    
    # Extract date
    date = ""
    date_elem = container.find("span", {"data-hook": "review-date"})
    if not date_elem:
        date_elem = container.find(class_="review-date")
    
    if date_elem:
        date = clean_text(date_elem.get_text(" ", strip=True))
        # Clean up "Reviewed in India on March 15, 2024" to just date
        date = re.sub(r'Reviewed in .+ on ', '', date, flags=re.I)
    
    # Check if verified purchase
    verified = False
    verified_elem = container.find("span", {"data-hook": "avp-badge"})
    if not verified_elem:
        verified_elem = container.find(string=re.compile(r'Verified Purchase', re.I))
    if verified_elem:
        verified = True
    
    # Only return if we have body text
    if not body or len(body) < 10:
        return None
    
    return {
        "title": title or "Review",
        "rating": rating,
        "body": body[:1000],  # Limit to 1000 chars
        "date": date,
        "verified": verified,
    }

def scrape_search_results(search_term, exclude_asin):
    """
    Scrape Amazon search results to find HIGHER-RANKED competitor ASINs.
    
    Key improvement: Returns only competitors that rank ABOVE the input ASIN.
    This ensures we analyze what top performers are doing right.
    """
    exclude_asin = exclude_asin.upper().strip()
    clean_term = clean_search_term(search_term)

    if not clean_term:
        return []

    search_url = f"{AMAZON_BASE_URL}/s?k={quote_plus(clean_term)}"

    for attempt in range(3):
        try:
            delay = random.uniform(2, 4) if attempt == 0 else random.uniform(5, 9)
            time.sleep(delay)

            print(f"[SEARCH] attempt={attempt + 1}/3 term='{clean_term}'")

            response = requests.get(
                search_url,
                headers=COMMON_HEADERS,
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code in [429, 503]:
                print(f"[SEARCH BLOCKED] status={response.status_code}")
                continue

            response.raise_for_status()

            soup = BeautifulSoup(response.text, "html.parser")

            if is_blocked_page(soup, response.text):
                print("[SEARCH BLOCKED] Captcha/block page detected")
                continue

            # Extract ASINs IN ORDER (this preserves ranking)
            ordered_asins = []
            seen = set()

            # Method 1: data-asin attributes (maintains order)
            products = soup.find_all("div", attrs={"data-asin": True})

            for product in products:
                asin = product.get("data-asin", "").strip().upper()

                # Valid ASIN and not seen yet
                if (
                    asin
                    and re.match(r"^[A-Z0-9]{10}$", asin)
                    and asin not in seen
                ):
                    ordered_asins.append(asin)
                    seen.add(asin)

            # Method 2: product links (if we don't have enough)
            if len(ordered_asins) < 10:
                links = soup.find_all("a", href=True)

                for link in links:
                    href = link.get("href", "")
                    found_asins = extract_asins_from_url(href)

                    for asin in found_asins:
                        if (
                            asin
                            and re.match(r"^[A-Z0-9]{10}$", asin)
                            and asin not in seen
                        ):
                            ordered_asins.append(asin)
                            seen.add(asin)

                        if len(ordered_asins) >= 15:
                            break

                    if len(ordered_asins) >= 15:
                        break

            # Find where OUR product ranks
            your_position = None

            for i, asin in enumerate(ordered_asins):
                if asin == exclude_asin:
                    your_position = i
                    break

            print(f"[SEARCH] Total ASINs found: {len(ordered_asins)}")
            print(f"[SEARCH] Your product position: {your_position if your_position is not None else 'NOT FOUND'}")

            # Case 1: Your product found in results
            if your_position is not None:
                # Get competitors BEFORE your product (higher ranked)
                higher_ranked = ordered_asins[:your_position]

                if higher_ranked:
                    print(f"[SEARCH] Found {len(higher_ranked)} competitors ranked ABOVE you")
                    return higher_ranked[:5]  # Return top 5 higher-ranked

                else:
                    # You're ranked #1! Take the ones just below you
                    print("[SEARCH] Your product is #1! Taking next-best competitors")
                    next_best = [
                        asin for asin in ordered_asins[your_position + 1:]
                        if asin != exclude_asin
                    ]
                    return next_best[:5]

            # Case 2: Your product NOT in top search results
            else:
                print("[SEARCH] Your product not in top results - taking top competitors")
                # Take top non-excluded ASINs
                competitors = [
                    asin for asin in ordered_asins
                    if asin != exclude_asin
                ]
                return competitors[:5]

        except requests.exceptions.RequestException as e:
            print(f"[SEARCH REQUEST ERROR] attempt={attempt + 1} error={e}")

        except Exception as e:
            print(f"[SEARCH ERROR] attempt={attempt + 1} error={e}")

    print("[SEARCH RESULT] No ASINs found from Amazon search")
    return []

def extract_asins_from_url(url):
    """
    Extract ASINs from common Amazon URLs:
    /dp/B0XXXXXXX
    /gp/product/B0XXXXXXX
    """
    patterns = [
        r"/dp/([A-Z0-9]{10})",
        r"/gp/product/([A-Z0-9]{10})",
    ]

    found = []

    for pattern in patterns:
        matches = re.findall(pattern, url)

        for match in matches:
            asin = match.upper().strip()
            if asin not in found:
                found.append(asin)

    return found


def is_valid_competitor_asin(asin, exclude_asin, existing_asins):
    return (
        asin
        and re.match(r"^[A-Z0-9]{10}$", asin)
        and asin != exclude_asin
        and asin not in existing_asins
    )


def is_blocked_page(soup, raw_html):
    """
    Detect Amazon captcha/block pages.
    """
    text = soup.get_text(" ", strip=True).lower()
    html_lower = raw_html.lower()

    blocked_signals = [
        "enter the characters you see below",
        "sorry, we just need to make sure you're not a robot",
        "captcha",
        "robot check",
    ]

    return any(signal in text or signal in html_lower for signal in blocked_signals)


# ============================================================================
# EXTRACTORS
# ============================================================================

def extract_title(soup):
    selectors = [
        ("span", {"id": "productTitle"}),
        ("h1", {"id": "title"}),
    ]

    for tag, attrs in selectors:
        elem = soup.find(tag, attrs)
        if elem:
            text = clean_text(elem.get_text(" ", strip=True))
            if text:
                return text

    return "Title not found"


def extract_bullets(soup):
    bullets = []

    bullet_container = soup.find("div", {"id": "feature-bullets"})

    if bullet_container:
        bullet_items = bullet_container.find_all("span", class_="a-list-item")

        for item in bullet_items:
            text = clean_text(item.get_text(" ", strip=True))

            bad_texts = [
                "make sure that you are posting in the form",
                "to calculate the overall star rating",
            ]

            if (
                text
                and len(text) > 5
                and not any(bad in text.lower() for bad in bad_texts)
            ):
                bullets.append(text)

    # Backup selector
    if not bullets:
        for li in soup.select("#featurebullets_feature_div li span"):
            text = clean_text(li.get_text(" ", strip=True))
            if text and len(text) > 5:
                bullets.append(text)

    return bullets[:5]


def extract_description(soup):
    desc_elem = soup.find("div", {"id": "productDescription"})

    if desc_elem:
        paragraphs = desc_elem.find_all("p")
        description = " ".join(
            clean_text(p.get_text(" ", strip=True))
            for p in paragraphs
        )

        if description:
            return description[:1000]

    feature_div = soup.find("div", {"id": "featurebullets_feature_div"})

    if feature_div:
        description = clean_text(feature_div.get_text(" ", strip=True))
        if description:
            return description[:1000]

    return "No description found"


def extract_price(soup):
    selectors = [
        ("span", {"class": "a-price-whole"}),
        ("span", {"id": "priceblock_ourprice"}),
        ("span", {"id": "priceblock_dealprice"}),
        ("span", {"class": "a-offscreen"}),
        ("span", {"class": "a-price"}),
    ]

    for tag, attrs in selectors:
        elem = soup.find(tag, attrs)

        if elem:
            text = clean_text(elem.get_text(" ", strip=True))
            match = re.search(r"₹?\s*([\d,]+)", text)

            if match:
                return f"₹{match.group(1).replace(',', '')}"

    return "Price not found"


def extract_rating(soup):
    selectors = [
        ("span", {"class": "a-icon-alt"}),
        ("i", {"class": "a-icon-star"}),
    ]

    for tag, attrs in selectors:
        elem = soup.find(tag, attrs)

        if elem:
            text = clean_text(elem.get_text(" ", strip=True))
            match = re.search(r"(\d+\.?\d*)", text)

            if match:
                return match.group(1)

    return "N/A"


def extract_reviews_count(soup):
    selectors = [
        ("span", {"id": "acrCustomerReviewText"}),
        ("a", {"id": "acrCustomerReviewLink"}),
    ]

    for tag, attrs in selectors:
        elem = soup.find(tag, attrs)

        if elem:
            text = clean_text(elem.get_text(" ", strip=True))
            match = re.search(r"([\d,]+)", text)

            if match:
                return match.group(1)

    return "N/A"


def extract_image(soup):
    img_elem = soup.find("img", {"id": "landingImage"})

    if img_elem and img_elem.get("src"):
        return img_elem["src"]

    img_elem = soup.find("img", {"class": "a-dynamic-image"})

    if img_elem and img_elem.get("src"):
        return img_elem["src"]

    return None

def analyze_product_images_with_gemini(your_product, competitors):
    """
    Analyze product images using Google Gemini Vision (FREE)
    
    Compares YOUR image vs COMPETITOR images and recommends improvements.
    """
    try:
        from google import genai
        from google.genai import types
        
        api_key = getattr(settings, "GOOGLE_GEMINI_API_KEY", None)
        
        if not api_key:
            print("[GEMINI] API key missing, skipping image analysis")
            return ""
        
        # Configure Gemini with NEW package
        client = genai.Client(api_key=api_key)
        
        # Get image URLs
        your_image_url = your_product.get('image_url')
        
        if not your_image_url:
            return "\n## IMAGE ANALYSIS\nNo product image found."
        
        competitor_images = [
            c.get('image_url') for c in competitors 
            if c.get('image_url')
        ][:3]
        
        # Build prompt with all image URLs
        prompt_parts = [
            "You are an Amazon listing image expert. Analyze these product images:\n\n",
            f"**MY PRODUCT IMAGE:** {your_image_url}\n",
        ]
        
        for i, comp_url in enumerate(competitor_images, 1):
            prompt_parts.append(f"**COMPETITOR {i} IMAGE:** {comp_url}\n")
        
        prompt_parts.append("""

Compare these Amazon product images:

1. **Image Types**: Classify each (studio shot, lifestyle, infographic, white background, in-use, packaging)
2. **Quality**: Professional vs amateur (lighting, resolution, composition)
3. **Background**: White, colored, lifestyle setting, gradient
4. **Composition**: Product angle, zoom level, context
5. **Missing Elements**: What do competitors show that we don't?

Provide analysis:

## IMAGE ANALYSIS

**Your Product Image:**
- Type: [classification]
- Quality: [professional/amateur, lighting quality]
- Background: [description]
- Composition: [angle, zoom, framing]

**Competitor Images:**
- Competitor 1: [type, quality, key differences from yours]
- Competitor 2: [type, quality, key differences from yours]
- Competitor 3: [type, quality, key differences from yours]

## IMAGE RECOMMENDATIONS

1. **[Specific Action]** (Priority: HIGH/MEDIUM/LOW)
   - What to do: [exact description - be specific!]
   - Why it matters: [conversion impact reason]
   - Expected impact: [percentage or metric]
   - Implementation: [how to create - DIY or photographer cost]

2. **[Specific Action]** (Priority: HIGH/MEDIUM/LOW)
   - What to do: [exact description]
   - Why it matters: [reason]
   - Expected impact: [metric]
   - Implementation: [how-to]

3. **[Specific Action]** (Priority: HIGH/MEDIUM/LOW)
   - What to do: [exact description]
   - Why it matters: [reason]
   - Expected impact: [metric]
   - Implementation: [how-to]

Be VERY specific. Don't say "improve image" - say "Add lifestyle shot showing product on white marble kitchen counter with morning sunlight, person's hand holding it, green plant in background".
""")
        
        print("[GEMINI] Analyzing product images...")
        
        # Use NEW API with gemini-2.0-flash-exp (FREE model)
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents="".join(prompt_parts)
        )
        
        print("[GEMINI] Image analysis complete")
        return "\n\n" + response.text
        
    except Exception as e:
        print(f"[GEMINI ERROR] {e}")
        import traceback
        traceback.print_exc()
        return "\n\n## IMAGE ANALYSIS\nImage analysis unavailable"
    
    
def improve_product_image_with_openai(product, competitors=None):
    """
    Improve only OUR product image for ecommerce listing.

    Uses the scraped product image URL as input and creates one enhanced
    ecommerce-ready image.
    """
    image_url = product.get("image_url")

    if not image_url:
        print("[IMAGE IMPROVE] No image_url found for product")
        return None

    api_key = getattr(settings, "OPENAI_API_KEY", None)

    if not api_key:
        print("[IMAGE IMPROVE] OPENAI_API_KEY missing")
        return None

    try:
        from openai import OpenAI

        print(f"[IMAGE IMPROVE] Downloading product image: {image_url}")

        image_response = requests.get(
            image_url,
            headers=COMMON_HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
        image_response.raise_for_status()

        image_bytes = image_response.content

        # Convert image to clean PNG/JPG compatible file.
        # Amazon images can be webp/jpeg with odd modes, so normalize.
        pil_image = Image.open(BytesIO(image_bytes)).convert("RGBA")

        input_buffer = BytesIO()
        pil_image.save(input_buffer, format="PNG")
        input_buffer.seek(0)
        input_buffer.name = "product_input.png"

        competitor_titles = ""
        if competitors:
            competitor_titles = "\n".join([
                f"- {comp.get('title', '')[:180]}"
                for comp in competitors[:3]
            ])

        prompt = f"""
        Edit this product image into a premium high-converting ecommerce hero image.

        Strict preservation rules:
        - Keep the exact same product identity.
        - Do not change the product shape, branding, packaging text, logo, material, or colors.
        - The product must remain clearly recognizable as the same item.

        Allowed improvements:
        - Improve the background so it looks more premium and visually appealing.
        - Do NOT use a plain white background.
        - Use a premium ecommerce-style background such as a soft gradient studio backdrop, elegant minimal lifestyle surface, or subtle premium setting.
        - Improve lighting, contrast, sharpness, highlights, and shadows.
        - Add depth and a premium polished look.
        - Reposition the product into a more attractive composition.
        - Show the product from a slightly improved angle such as a subtle 3/4 perspective if it helps presentation.
        - Make the product stand out more than typical competitor listing images.
        - Add realistic soft shadow and refined studio lighting.
        - Make the image look polished, modern, premium, and conversion-focused.

        Restrictions:
        - Do not add distracting props.
        - Do not create multiple products.
        - Do not heavily stylize or distort the product.
        - Do not make the background busy.
        - Keep the result suitable for an ecommerce marketplace listing.

        Competitor context:
        {competitor_titles if competitor_titles else "No competitor context available."}

        Return one final premium product image.
        """

        client = OpenAI(api_key=api_key)

        print("[IMAGE IMPROVE] Calling OpenAI image edit...")

        result = client.images.edit(
            model="gpt-image-1",
            image=input_buffer,
            prompt=prompt,
            size="1024x1024",
            n=1,
        )

        image_b64 = result.data[0].b64_json
        output_bytes = base64.b64decode(image_b64)

        folder = os.path.join(settings.MEDIA_ROOT, "improved_products")
        os.makedirs(folder, exist_ok=True)

        filename = f"improved_{product.get('asin', 'product')}_{uuid.uuid4().hex}.png"
        file_path = os.path.join(folder, filename)

        with open(file_path, "wb") as f:
            f.write(output_bytes)

        backend_base_url = getattr(settings, "BACKEND_BASE_URL", "https://pixii.selectease.in")
        improved_url = f"{backend_base_url}{settings.MEDIA_URL}improved_products/{filename}"

        print(f"[IMAGE IMPROVE] Saved improved image: {improved_url}")

        return improved_url

    except Exception as e:
        print(f"[IMAGE IMPROVE ERROR] {e}")
        return None
    
# ============================================================================
# COMPETITOR FALLBACK
# ============================================================================

def get_fallback_competitors(input_asin):
    """
    Fallback competitor ASINs.

    These should ideally be category-specific.
    For assignment/demo purpose, this prevents scraper.py from failing
    when Amazon search blocks the request.
    """
    fallback_asins = [
        "B0D6F8QRXG",
        "B0BVQWK6Y7",
        "B0CQTK9M9Y",
        "B0D3YMHGZ8",
        "B08X6BK4D4",
    ]

    return [
        asin for asin in fallback_asins
        if asin != input_asin
    ]


# ============================================================================
# AI
# ============================================================================

def generate_search_term(product_data):
    """
    Use DeepSeek to generate short competitor search term.
    Falls back to first meaningful words from title.
    """
    title = product_data.get("title", "")

    try:
        import openai

        api_key = getattr(settings, "DEEPSEEK_API_KEY", None)

        if not api_key:
            raise Exception("DEEPSEEK_API_KEY missing in settings")

        client = openai.OpenAI(
            api_key=api_key,
            base_url="https://api.deepseek.com",
        )

        prompt = f"""
Generate a SHORT Amazon India search term of 2-4 words to find similar competing products.

Product title:
{title}

Return ONLY the search term.
"""

        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=20,
            temperature=0.2,
        )

        search_term = response.choices[0].message.content.strip()
        return clean_search_term(search_term)

    except Exception as e:
        print(f"[DEEPSEEK SEARCH TERM FALLBACK] {e}")
        return fallback_search_term_from_title(title)


def analyze_listing(your_product, competitors):
    """
    Use DeepSeek to analyze listing vs competitors.
    """
    try:
        import openai

        api_key = getattr(settings, "DEEPSEEK_API_KEY", None)

        if not api_key:
            raise Exception("DEEPSEEK_API_KEY missing in settings")

        client = openai.OpenAI(
            api_key=api_key,
            base_url="https://api.deepseek.com",
        )

        comp_summaries = []
        comp_review_summaries = []

        for i, comp in enumerate(competitors, 1):
            summary = f"""
Competitor {i}:
- ASIN: {comp.get("asin", "N/A")}
- Title: {comp.get("title", "N/A")} ({len(comp.get("title", ""))} chars)
- Price: {comp.get("price", "N/A")}
- Rating: {comp.get("rating", "N/A")} stars
- Reviews: {comp.get("reviews_count", "N/A")}
- Bullets: {len(comp.get("bullets", []))} points
"""
            comp_summaries.append(summary)
            reviews = comp.get("reviews", [])[:10]
            review_text = "\n".join([
                f"- {review.get('rating', 'N/A')} stars | {review.get('title', '')}: {review.get('body', '')}"
                for review in reviews
            ])

            comp_review_summaries.append(f"""
        Competitor {i} Reviews:
        {review_text if review_text else "No review text found"}
        """)
        your_reviews = your_product.get("reviews", [])[:10]

        your_reviews_text = "\n".join([
            f"- {review.get('rating', 'N/A')} stars | {review.get('title', '')}: {review.get('body', '')}"
            for review in your_reviews
        ])
        prompt = f"""
        You are an Amazon listing optimization expert. Compare this listing to competitors and provide specific recommendations.

        YOUR LISTING:
        - ASIN: {your_product.get("asin", "N/A")}
        - Title: {your_product.get("title", "N/A")} ({len(your_product.get("title", ""))} chars)
        - Price: {your_product.get("price", "N/A")}
        - Rating: {your_product.get("rating", "N/A")} stars
        - Reviews: {your_product.get("reviews_count", "N/A")}
        - Bullets: {len(your_product.get("bullets", []))} points

        YOUR BULLETS:
        {chr(10).join([f"{i + 1}. {b}" for i, b in enumerate(your_product.get("bullets", []))])}

        YOUR RECENT/TOP REVIEWS:
        {your_reviews_text if your_reviews_text else "No review text found"}

        COMPETITORS:
        {chr(10).join(comp_summaries)}

        COMPETITOR BULLETS:
        {chr(10).join([f"Competitor {i + 1}: " + ", ".join(comp.get("bullets", [])[:3]) for i, comp in enumerate(competitors)])}

        COMPETITOR RECENT/TOP REVIEWS:
        {chr(10).join(comp_review_summaries)}

        Provide analysis in this exact format:

        ## TITLE OPTIMIZATION
        [Specific title recommendation]

        ## BULLET POINTS ANALYSIS
        [Bullet improvements]

        ## KEYWORD GAP ANALYSIS
        [Missing keywords]

        ## PRICING POSITION
        [Price analysis]

        ## QUICK WINS (Top 3)
        [3 actionable changes]

        ## REVIEW SENTIMENT COMPARISON
        Compare the customer's actual review feedback from this product against competitors.
        Include:
        - What customers like about our product
        - What customers complain about in our product
        - What competitors are praised for
        - What competitors are criticized for
        - Any review-based trust signals or objections

        Use a markdown table if helpful:
        | Area | Our Product | Competitors | Insight |

        ## PRODUCT IMPROVEMENT SUMMARY
        Give a final product improvement summary based on listing + review comparison.
        Include:
        1. Product positioning improvement
        2. Listing copy improvement
        3. Packaging / trust signal improvement
        4. Customer objection to address
        5. Final recommendation for increasing conversion
        """

        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2500,
            temperature=0.7,
        )

        return response.choices[0].message.content.strip()

    except Exception as e:
        return f"Analysis failed: {str(e)}"


# ============================================================================
# HTML GENERATION
# ============================================================================

def generate_product_html(product):
    """
    Generate Amazon-like product card HTML.
    Escapes scraped text to avoid broken HTML.
    """
    title = html.escape(product.get("title", "Title not found"))
    rating = html.escape(str(product.get("rating", "N/A")))
    reviews_count = html.escape(str(product.get("reviews_count", "N/A")))
    price = html.escape(product.get("price", "Price not found"))
    url = html.escape(product.get("url", "#"))
    image_url = product.get("image_url")

    bullets_html = "".join([
        f'<li class="bullet-point">{html.escape(bullet)}</li>'
        for bullet in product.get("bullets", [])
    ])

    if image_url:
        safe_image_url = html.escape(image_url)
        image_html = (
            f'<img src="{safe_image_url}" alt="Product" class="product-image">'
        )
    else:
        image_html = '<div class="no-image">No Image</div>'

    html_body = f"""
    <div class="amazon-product-card">
        <div class="product-image-container">
            {image_html}
        </div>

        <div class="product-details">
            <h3 class="product-title">{title}</h3>

            <div class="product-rating">
                <span class="rating">{rating} ⭐</span>
                <span class="reviews">({reviews_count} reviews)</span>
            </div>

            <div class="product-price">
                <span class="price">{price}</span>
            </div>

            <ul class="product-bullets">
                {bullets_html}
            </ul>

            <a href="{url}" target="_blank" rel="noopener noreferrer" class="view-on-amazon">
                View on Amazon →
            </a>
        </div>
    </div>
    """

    css = """
    <style>
    .amazon-product-card {
        border: 1px solid #ddd;
        border-radius: 8px;
        padding: 20px;
        max-width: 800px;
        margin: 20px auto;
        background: white;
        box-shadow: 0 2px 8px rgba(0,0,0,0.1);
    }
    .product-image-container {
        text-align: center;
        margin-bottom: 20px;
    }
    .product-image {
        max-width: 300px;
        height: auto;
        border-radius: 4px;
    }
    .no-image {
        width: 300px;
        height: 300px;
        background: #f0f0f0;
        display: flex;
        align-items: center;
        justify-content: center;
        margin: 0 auto;
        border-radius: 4px;
    }
    .product-title {
        font-size: 18px;
        font-weight: 600;
        color: #0F1111;
        line-height: 1.4;
        margin-bottom: 12px;
    }
    .product-rating {
        display: flex;
        align-items: center;
        gap: 8px;
        margin-bottom: 12px;
    }
    .rating {
        color: #FF9900;
        font-weight: 600;
    }
    .reviews {
        color: #007185;
        font-size: 14px;
    }
    .product-price {
        margin-bottom: 16px;
    }
    .price {
        font-size: 24px;
        font-weight: 600;
        color: #B12704;
    }
    .product-bullets {
        list-style: none;
        padding: 0;
        margin: 0 0 16px 0;
    }
    .bullet-point {
        padding: 8px 0;
        padding-left: 20px;
        position: relative;
        color: #0F1111;
        font-size: 14px;
        line-height: 1.5;
    }
    .bullet-point:before {
        content: "•";
        position: absolute;
        left: 0;
        color: #565959;
    }
    .view-on-amazon {
        display: inline-block;
        background: #FF9900;
        color: #0F1111;
        padding: 10px 20px;
        border-radius: 8px;
        text-decoration: none;
        font-weight: 600;
        transition: background 0.2s;
    }
    .view-on-amazon:hover {
        background: #FFA500;
    }
    </style>
    """

    return css + html_body


# ============================================================================
# TEXT HELPERS
# ============================================================================

def clean_text(value):
    if not value:
        return ""

    value = re.sub(r"\s+", " ", value)
    return value.strip()


def clean_search_term(value):
    if not value:
        return ""

    value = value.strip()
    value = value.replace('"', "")
    value = value.replace("'", "")
    value = re.sub(r"\s+", " ", value)

    # Keep search term short
    words = value.split()
    return " ".join(words[:5])


def fallback_search_term_from_title(title):
    if not title:
        return ""

    stopwords = {
        "for", "with", "and", "the", "a", "an", "of", "in", "on",
        "pack", "combo", "best", "new", "india", "by"
    }

    words = re.findall(r"[A-Za-z0-9]+", title)

    useful_words = [
        word for word in words
        if len(word) > 2 and word.lower() not in stopwords
    ]

    return " ".join(useful_words[:4])