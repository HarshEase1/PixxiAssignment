"""
Celery tasks for Amazon listing analyzer (Database-backed)

File: api/tasks.py
"""

from celery import shared_task
from .scraper import process_analysis


@shared_task(bind=True)
def scrape_amazon_listing_task(self, task_id):
    """
    Run full Amazon analysis flow.

    This delegates to scraper.process_analysis(), which handles:
    - scraping your product
    - scraping competitors
    - review extraction
    - AI listing analysis
    - HTML preview generation
    - OpenAI improved product image generation
    - database saving
    """
    return process_analysis(task_id)