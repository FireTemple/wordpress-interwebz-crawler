import asyncio
import logging
import re
import time
from collections import deque
from urllib.parse import urlparse, urljoin, urlsplit
import aiohttp
from bs4 import BeautifulSoup
from aiohttp.client_exceptions import ClientError, ClientConnectorError
import tldextract

# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------
MAX_CONCURRENT_REQUESTS = 100          # Adjust based on your bandwidth / politeness
MAX_DEPTH = 2                          # How deep to follow links from seed (0 = only seed)
REQUEST_DELAY = 0.2                    # Minimum seconds between requests to same domain
TIMEOUT_SECONDS = 10
MAX_DISCOVERED_PER_DOMAIN = 5          # Avoid crawling huge sites deeply
USER_AGENT = "WordPressCrawlerBot/1.0 (+https://yourdomain.com/crawler-info)"

# ------------------------------------------------------------------
# WordPress detection fingerprints (regularly updated)
# ------------------------------------------------------------------
WP_FINGERPRINTS = [
    # Generator meta tag
    (re.compile(r'<meta[^>]+name=["\']generator["\'][^>]+wordpress', re.I), "meta generator"),
    
    # Classic wp-includes or wp-content paths
    (re.compile(r'/wp-includes/'), "wp-includes path"),
    (re.compile(r'/wp-content/(themes|plugins|uploads)/'), "wp-content path"),
    
    # Login page
    (re.compile(r'wp-login\.php'), "wp-login.php"),
    (re.compile(r'wp-admin/'), "wp-admin"),
    
    # Common headers
    (lambda headers: headers.get("x-powered-by", "").lower().startswith("wordpress"), "X-Powered-By header"),
    (lambda headers: "wp-" in headers.get("link", "").lower(), "Link header wp- prefix"),
    
    # XML-RPC, readme.html, etc.
    (re.compile(r'xmlrpc\.php'), "xmlrpc.php"),
    (re.compile(r'/readme\.html[^"]*wordpress', re.I), "readme.html"),
    
    # wp-json API
    (re.compile(r'/wp-json/wp/v2/'), "REST API endpoint"),
]

# ------------------------------------------------------------------
# Helper: exclude wordpress.com and subdomains
# ------------------------------------------------------------------
def is_wordpress_dot_com(url: str) -> bool:
    extracted = tldextract.extract(url)
    domain = f"{extracted.domain}.{extracted.suffix}"
    return domain == "wordpress.com" or extracted.subdomain.endswith(".wordpress.com")

# ------------------------------------------------------------------
# Core crawler
# ------------------------------------------------------------------
class WordPressCrawler:
    def __init__(self, seeds: list[str], output_file: str = "wordpress_sites.txt"):
        self.seeds = seeds
        self.output_file = output_file
        self.found_wordpress = set()
        self.visited = set()
        self.queue = deque()
        self.domain_last_request = {}  # rate limiting per domain
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    def is_wordpress(self, url: str, text: str = None, headers=None) -> tuple[bool, str]:
        if text is None and headers is None:
            return False, ""

        headers = headers or {}

        for pattern_or_callable, description in WP_FINGERPRINTS:
            if callable(pattern_or_callable):
                if pattern_or_callable(headers):
                    return True, description
            else:
                if text and pattern_or_callable.search(text):
                    return True, description

        return False, ""

    async def fetch(self, session: aiohttp.ClientSession, url: str):
        parsed = urlparse(url)
        domain = parsed.netloc.lower()

        # Rate limiting per domain
        now = time.time()
        last = self.domain_last_request.get(domain, 0)
        delay = REQUEST_DELAY - (now - last)
        if delay > 0:
            await asyncio.sleep(delay)

        try:
            async with self.semaphore:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS),
                    allow_redirects=True,
                    ssl=False,
                ) as resp:
                    self.domain_last_request[domain] = time.time()
                    
                    # Read only first 500KB – enough for detection
                    body = await resp.text(errors="ignore")
                    if len(body) > 500_000:
                        body = body[:500_000]

                    return resp.status, resp.headers, body[:500_000], resp.url
        except (ClientConnectorError, ClientError, asyncio.TimeoutError) as e:
            logging.debug(f"Failed {url}: {e}")
            return None, None, None, None
        except Exception as e:
            logging.warning(f"Unexpected error {url}: {e}")
            return None, None, None, None

    async def process_url(self, session: aiohttp.ClientSession, url: str, depth: int):
        if url in self.visited or depth > MAX_DEPTH:
            return

        self.visited.add(url)
        parsed = urlparse(url)
        base_domain = parsed.netloc.lower()

        # Skip wordpress.com entirely
        if is_wordpress_dot_com(url):
            return

        status, headers, html, final_url = await self.fetch(session, url)
        if not html:
            return

        final_url_str = str(final_url)

        # Detect WordPress
        is_wp, evidence = self.is_wordpress(final_url_str, html, headers)
        if is_wp and final_url_str not in self.found_wordpress:
            domain_only = urlparse(final_url_str).netloc
            result = f"{domain_only} | {evidence} | {final_url_str}"
            print(f"FOUND → {result}")
            self.found_wordpress.add(final_url_str)
            
            with open(self.output_file, "a", encoding="utf-8") as f:
                f.write(result + "\n")

        # Only follow links if we haven't found too many pages on this domain
        if len([u for u in self.visited if urlparse(u).netloc == base_domain]) >= MAX_DISCOVERED_PER_DOMAIN:
            return

        # Extract and enqueue internal links
        soup = BeautifulSoup(html, "html.parser")
        for link in soup.find_all("a", href=True):
            href = link["href"]
            absolute = urljoin(url, href.split("#")[0])  # remove fragment
            if urlparse(absolute).netloc == base_domain:  # stay on same domain
                self.queue.append((absolute, depth + 1))

    async def run(self):
        connector = aiohttp.TCPConnector(limit_per_host=10, ssl=False)
        timeout = aiohttp.ClientTimeout(total=30)
        headers = {"User-Agent": USER_AGENT}

        async with aiohttp.ClientSession(connector=connector, timeout=timeout, headers=headers) as session:
            # Seed the queue
            for seed in self.seeds:
                self.queue.append((seed, 0))

            while self.queue:
                url, depth = self.queue.popleft()
                asyncio.create_task(self.process_url(session, url, depth))

                # Small yield to prevent event loop starvation
                await asyncio.sleep(0)

# ------------------------------------------------------------------
# Example usage
# ------------------------------------------------------------------
if __name__ == "__main__":
    # Good starting seeds (high chance of linking to self-hosted WP sites)
    seeds = [
        "https://www.google.com/search?q=powered+by+wordpress+-site:wordpress.com",
        "https://builtwith.com/wordpress",
        "https://wpsites.net/",
        "https://en.blog.wordpress.com/",
        # Add your own seed list (directories, forums, etc.)
    ]

    crawler = WordPressCrawler(seeds, output_file="self_hosted_wordpress_sites.txt")
    asyncio.run(crawler.run())
