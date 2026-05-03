"""
Django management command to scrape Amazon listing by ASIN

Usage:
    python manage.py scrape_amazon B0DWMQDYSZ
"""

from django.core.management.base import BaseCommand
import requests
from bs4 import BeautifulSoup
import time
import random
import re
from datetime import datetime
import os


class Command(BaseCommand):
    help = 'Scrape Amazon listing by ASIN and save to text file'

    def add_arguments(self, parser):
        parser.add_argument('asin', type=str, help='Amazon ASIN (e.g., B0DWMQDYSZ)')

    def handle(self, *args, **options):
        asin = options['asin'].upper()
        
        self.stdout.write(self.style.SUCCESS(f'\n🔍 Starting scrape for ASIN: {asin}\n'))
        
        # Scrape main product
        self.stdout.write('📦 Scraping your product...')
        your_product = self.scrape_product(asin)
        
        if not your_product:
            self.stdout.write(self.style.ERROR('❌ Failed to scrape product'))
            return
        
        self.stdout.write(self.style.SUCCESS('✅ Product scraped successfully'))
        
        # Find competitors
        self.stdout.write('\n🎯 Finding competitors...')
        competitor_asins = self.find_competitors(asin, your_product)
        
        # Scrape competitors
        competitors = []
        for i, comp_asin in enumerate(competitor_asins, 1):
            self.stdout.write(f'📊 Scraping competitor {i}/3: {comp_asin}')
            comp_data = self.scrape_product(comp_asin)
            if comp_data:
                competitors.append(comp_data)
                self.stdout.write(self.style.SUCCESS(f'✅ Competitor {i} scraped'))
            time.sleep(random.uniform(2, 4))  # Rate limiting
        
        # Analyze with DeepSeek
        self.stdout.write('\n🤖 Analyzing with AI...')
        analysis = self.analyze_listing(your_product, competitors)
        
        # Save to file
        self.stdout.write('\n💾 Saving to file...')
        filename = self.save_to_file(your_product, competitors, asin, analysis)
        
        self.stdout.write(self.style.SUCCESS(f'\n✅ Done! Saved to: {filename}\n'))

    def scrape_product(self, asin):
        """Scrape product details from Amazon"""
        url = f'https://www.amazon.in/dp/{asin}'
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
        }
        
        try:
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Extract data
            product_data = {
                'asin': asin,
                'title': self._extract_title(soup),
                'bullets': self._extract_bullets(soup),
                'description': self._extract_description(soup),
                'price': self._extract_price(soup),
                'rating': self._extract_rating(soup),
                'reviews_count': self._extract_reviews_count(soup),
                'url': url,
            }
            
            return product_data
            
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Error scraping {asin}: {str(e)}'))
            return None

    def _extract_title(self, soup):
        """Extract product title"""
        title_elem = soup.find('span', {'id': 'productTitle'})
        if title_elem:
            return title_elem.text.strip()
        return "Title not found"

    def _extract_bullets(self, soup):
        """Extract bullet points"""
        bullets = []
        
        # Try feature bullets
        bullet_container = soup.find('div', {'id': 'feature-bullets'})
        if bullet_container:
            bullet_items = bullet_container.find_all('span', class_='a-list-item')
            for item in bullet_items:
                text = item.text.strip()
                if text and len(text) > 5:
                    bullets.append(text)
        
        return bullets[:5]  # Max 5 bullets

    def _extract_description(self, soup):
        """Extract product description"""
        # Try product description div
        desc_elem = soup.find('div', {'id': 'productDescription'})
        if desc_elem:
            paragraphs = desc_elem.find_all('p')
            description = ' '.join([p.text.strip() for p in paragraphs])
            return description[:1000]  # Limit to 1000 chars
        
        # Try feature div
        feature_div = soup.find('div', {'id': 'featurebullets_feature_div'})
        if feature_div:
            return feature_div.text.strip()[:1000]
        
        return "No description found"

    def _extract_price(self, soup):
        """Extract product price"""
        # Try multiple price selectors
        price_selectors = [
            ('span', {'class': 'a-price-whole'}),
            ('span', {'id': 'priceblock_ourprice'}),
            ('span', {'id': 'priceblock_dealprice'}),
            ('span', {'class': 'a-price'}),
        ]
        
        for tag, attrs in price_selectors:
            price_elem = soup.find(tag, attrs)
            if price_elem:
                price_text = price_elem.text.strip()
                # Extract numbers
                price_match = re.search(r'[\d,]+', price_text.replace(',', ''))
                if price_match:
                    return f"₹{price_match.group()}"
        
        return "Price not found"

    def _extract_rating(self, soup):
        """Extract product rating"""
        rating_elem = soup.find('span', {'class': 'a-icon-alt'})
        if rating_elem:
            rating_text = rating_elem.text.strip()
            rating_match = re.search(r'(\d+\.?\d*)', rating_text)
            if rating_match:
                return rating_match.group(1)
        return "N/A"

    def _extract_reviews_count(self, soup):
        """Extract number of reviews"""
        reviews_elem = soup.find('span', {'id': 'acrCustomerReviewText'})
        if reviews_elem:
            reviews_text = reviews_elem.text.strip()
            reviews_match = re.search(r'([\d,]+)', reviews_text)
            if reviews_match:
                return reviews_match.group(1)
        return "N/A"

    def find_competitors(self, asin, your_product):
        """Find competitor ASINs by scraping Amazon search results"""
        # Use DeepSeek to generate search term
        search_term = self._generate_search_term(your_product)
        
        self.stdout.write(f'🔎 Search term: "{search_term}"')
        
        # Scrape Amazon search results
        competitor_asins = self._scrape_search_results(search_term, asin)
        
        if not competitor_asins or len(competitor_asins) < 3:
            self.stdout.write(self.style.WARNING('⚠️ Not enough competitors found, using fallback'))
            fallback = self._get_mock_competitors(asin)
            competitor_asins.extend(fallback)
        
        return competitor_asins[:3]

    def _scrape_search_results(self, search_term, exclude_asin):
        """Scrape Amazon search results to find competitor ASINs"""
        search_url = f'https://www.amazon.in/s?k={search_term.replace(" ", "+")}'
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        }
        
        try:
            self.stdout.write(f'🌐 Searching Amazon...')
            response = requests.get(search_url, headers=headers, timeout=10)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Find all product cards
            asins = []
            
            # Method 1: Look for data-asin attribute
            products = soup.find_all('div', {'data-asin': True})
            for product in products:
                asin = product.get('data-asin')
                if asin and len(asin) == 10 and asin != exclude_asin and asin not in asins:
                    asins.append(asin)
                    if len(asins) >= 5:  # Get 5 candidates
                        break
            
            # Method 2: Look in product links if not enough found
            if len(asins) < 3:
                links = soup.find_all('a', href=True)
                for link in links:
                    href = link['href']
                    # Extract ASIN from URLs like /dp/B08XYZ123/
                    match = re.search(r'/dp/([A-Z0-9]{10})', href)
                    if match:
                        asin = match.group(1)
                        if asin != exclude_asin and asin not in asins:
                            asins.append(asin)
                            if len(asins) >= 5:
                                break
            
            self.stdout.write(self.style.SUCCESS(f'✅ Found {len(asins)} competitor ASINs from search'))
            return asins
            
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Error scraping search results: {str(e)}'))
            return []

    def _generate_search_term(self, product_data):
        """Use DeepSeek to generate relevant search term"""
        try:
            import openai
            
            # DeepSeek API (OpenAI compatible)
            client = openai.OpenAI(
                api_key="sk-28d7766d1d854b67811161ff2fc3def6",
                base_url="https://api.deepseek.com"
            )
            
            prompt = f"""Given this Amazon product, generate a SHORT search term (2-4 words) to find similar competing products:

Title: {product_data['title']}

Return ONLY the search term, nothing else."""

            response = client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=20,
            )
            
            search_term = response.choices[0].message.content.strip()
            return search_term
            
        except Exception as e:
            self.stdout.write(self.style.WARNING(f'DeepSeek API error: {str(e)}'))
            # Fallback: extract from title
            words = product_data['title'].split()[:3]
            return ' '.join(words)

    def _get_mock_competitors(self, asin):
        """Fallback: Get some real ASINs from same category"""
        # These are real ASINs from amazon.in magnesium category
        fallback_asins = [
            'B0D6F8QRXG',  # HealthKart Magnesium
            'B0BVQWK6Y7',  # Carbamide Forte Magnesium
            'B0CQTK9M9Y',  # TrueBasics Magnesium
            'B0D3YMHGZ8',  # Nutrabay Magnesium
            'B08X6BK4D4',  # Carbamide Forte
        ]
        
        # Remove the input ASIN if present
        competitors = [c for c in fallback_asins if c != asin]
        
        return competitors[:3]

    def analyze_listing(self, your_product, competitors):
        """Use DeepSeek to analyze listing vs competitors"""
        try:
            import openai
            
            client = openai.OpenAI(
                api_key="sk-28d7766d1d854b67811161ff2fc3def6",
                base_url="https://api.deepseek.com"
            )
            
            # Prepare competitor data for analysis
            comp_summaries = []
            for i, comp in enumerate(competitors, 1):
                summary = f"""Competitor {i}:
- Title: {comp['title']} ({len(comp['title'])} chars)
- Price: {comp['price']}
- Rating: {comp['rating']} stars ({comp['reviews_count']} reviews)
- Bullets: {len(comp['bullets'])} points
"""
                comp_summaries.append(summary)
            
            competitors_text = "\n".join(comp_summaries)
            
            prompt = f"""You are an Amazon listing optimization expert. Compare this listing to competitors and provide SPECIFIC, ACTIONABLE recommendations.

YOUR LISTING:
- Title: {your_product['title']} ({len(your_product['title'])} chars)
- Price: {your_product['price']}
- Rating: {your_product['rating']} stars ({your_product['reviews_count']} reviews)
- Bullets: {len(your_product['bullets'])} points
Bullet Points:
{chr(10).join([f"{i+1}. {b}" for i, b in enumerate(your_product['bullets'])])}

COMPETITORS:
{competitors_text}

COMPETITORS' BULLET POINTS (for keyword analysis):
{chr(10).join([f"Competitor {i+1}: " + ", ".join(comp['bullets'][:3]) for i, comp in enumerate(competitors)])}

Provide analysis in this EXACT format:

## TITLE OPTIMIZATION
[Compare title length, identify if too long/short, suggest optimized version]

## BULLET POINTS ANALYSIS
[Compare bullet count, identify feature vs benefit focus, suggest improvements]

## KEYWORD GAP ANALYSIS
[List 3-5 keywords competitors use but you don't - be specific]

## PRICING POSITION
[Compare price to competitor range, assess positioning]

## QUICK WINS (Top 3)
[List exactly 3 actionable changes with estimated impact]

Be SPECIFIC. Don't say "improve title" - say "shorten from 156 to 80 chars: [exact suggested title]"."""

            self.stdout.write('🧠 Sending to DeepSeek...')
            
            response = client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1500,
                temperature=0.7,
            )
            
            analysis = response.choices[0].message.content.strip()
            self.stdout.write(self.style.SUCCESS('✅ Analysis complete'))
            
            return analysis
            
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Error in AI analysis: {str(e)}'))
            return "Analysis failed - check DeepSeek API key"

    def save_to_file(self, your_product, competitors, asin, analysis=None):
        """Save scraped data to formatted text file"""
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f'amazon_scrape_{asin}_{timestamp}.txt'
        
        with open(filename, 'w', encoding='utf-8') as f:
            # Header
            f.write('='*80 + '\n')
            f.write('AMAZON LISTING SCRAPE REPORT\n')
            f.write('='*80 + '\n\n')
            f.write(f'Generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n')
            f.write(f'ASIN: {asin}\n\n')
            
            # Your Product
            f.write('='*80 + '\n')
            f.write('YOUR PRODUCT\n')
            f.write('='*80 + '\n\n')
            self._write_product_details(f, your_product)
            
            # Competitors
            f.write('\n' + '='*80 + '\n')
            f.write('COMPETITORS\n')
            f.write('='*80 + '\n\n')
            
            if not competitors:
                f.write('(No competitors found)\n')
            else:
                for i, competitor in enumerate(competitors, 1):
                    f.write(f'\n--- Competitor #{i} ---\n\n')
                    self._write_product_details(f, competitor)
                    if i < len(competitors):
                        f.write('\n' + '-'*80 + '\n')
            
            # AI Analysis
            if analysis:
                f.write('\n\n' + '='*80 + '\n')
                f.write('AI ANALYSIS & RECOMMENDATIONS\n')
                f.write('='*80 + '\n\n')
                f.write(analysis)
                f.write('\n')
        
        return filename

    def _write_product_details(self, f, product):
        """Write product details in formatted manner"""
        f.write(f'ASIN: {product["asin"]}\n')
        f.write(f'URL: {product["url"]}\n\n')
        
        f.write(f'TITLE ({len(product["title"])} characters):\n')
        f.write(f'{product["title"]}\n\n')
        
        f.write(f'PRICE: {product["price"]}\n')
        f.write(f'RATING: {product["rating"]} stars\n')
        f.write(f'REVIEWS: {product["reviews_count"]}\n\n')
        
        f.write(f'BULLET POINTS ({len(product["bullets"])} bullets):\n')
        for i, bullet in enumerate(product['bullets'], 1):
            word_count = len(bullet.split())
            f.write(f'{i}. [{word_count} words] {bullet}\n')
        
        if not product['bullets']:
            f.write('(No bullet points found)\n')
        
        f.write(f'\nDESCRIPTION ({len(product["description"])} characters):\n')
        f.write(f'{product["description"]}\n')