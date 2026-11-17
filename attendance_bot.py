#!/usr/bin/env python3
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from playwright.sync_api import Page, Response, sync_playwright

BASE_URL = "https://lms.pmftci.online"
LOGIN_URL = f"{BASE_URL}/site/login"
DASHBOARD_URL = f"{BASE_URL}/user/user/dashboard"
PHT = timezone(timedelta(hours=8))

SCHEDULE_BLOCKS = [
    {"class_id": "460", "name": "Information Assurance and Security 1", "start_hm": (9, 0), "end_hm": (10, 30)},
    {"class_id": "462", "name": "IT 22 - Application Development and Emerging Technologies", "start_hm": (10, 30), "end_hm": (12, 0)},
    {"class_id": "461", "name": "IT 21 - Social and Professional Issues", "start_hm": (14, 30), "end_hm": (16, 0)},
    {"class_id": "463", "name": "IT 23 - Web System and Technologies 2", "start_hm": (16, 0), "end_hm": (17, 30)},
]

VISIBILITY_OVERRIDE_JS = """
(() => {
    Object.defineProperty(document, 'hidden', { get: () => false, configurable: true });
    Object.defineProperty(document, 'visibilityState', { get: () => 'visible', configurable: true });
    document.hasFocus = () => true;
})();
"""

def now_pht() -> datetime:
    return datetime.now(PHT)

def log(msg: str) -> None:
    print(f"[{now_pht().strftime('%Y-%m-%d %I:%M:%S %p PHT')}] {msg}", flush=True)

def get_preferred_class_id(current_dt: datetime) -> Optional[str]:
    cur_minutes = current_dt.hour * 60 + current_dt.minute
    for block in SCHEDULE_BLOCKS:
        start_m = block["start_hm"][0] * 60 + block["start_hm"][1] - 15
        end_m = block["end_hm"][0] * 60 + block["end_hm"][1]
        if start_m <= cur_minutes < end_m:
            return block["class_id"]
    return None

def compute_session_deadline(current_dt: datetime, max_run_minutes: int) -> datetime:
    hard_cap = current_dt + timedelta(minutes=max_run_minutes)
    cur_minutes = current_dt.hour * 60 + current_dt.minute
    if cur_minutes < 12 * 60 + 15:
        block_end = current_dt.replace(hour=12, minute=10, second=0, microsecond=0)
        return min(hard_cap, max(block_end, current_dt + timedelta(minutes=15)))
    if 13 * 60 + 30 <= cur_minutes < 17 * 60 + 45:
        block_end = current_dt.replace(hour=17, minute=40, second=0, microsecond=0)
        return min(hard_cap, max(block_end, current_dt + timedelta(minutes=15)))
    return min(hard_cap, current_dt + timedelta(minutes=30))

def login(page: Page, username: str, password: str) -> bool:
    log(f"Opening login page: {LOGIN_URL}")
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1500)
    if "/site/login" not in page.url:
        return True
    user_input = page.locator('input[name*="user" i], input[name*="login" i], input[name*="email" i], input[name*="identity" i], input[type="text"]').first
    pass_input = page.locator('input[type="password"]').first
    user_input.wait_for(state="visible", timeout=15000)
    user_input.fill(username)
    pass_input.fill(password)
    page.locator('button[type="submit"], input[type="submit"]').first.click()
    page.wait_for_load_state("domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)
    return "/site/login" not in page.url

def parse_live_classes_from_dashboard_html(html: str) -> List[str]:
    live_ids: List[str] = []
    anchor_pattern = re.compile(r'<a\b[^>]*href=["\'][^"\']*?/class/(\d+)["\'][^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
    for match in anchor_pattern.finditer(html):
        class_id, inner_html = match.group(1), match.group(2)
        clean_text = re.sub(r"<[^>]+>", " ", inner_html).strip().lower()
        if "join class" in clean_text and class_id not in live_ids:
            live_ids.append(class_id)
    return live_ids

def fetch_live_classes(page: Page) -> List[str]:
    try:
        resp = page.context.request.get(DASHBOARD_URL, timeout=20000)
        if resp.ok:
            return parse_live_classes_from_dashboard_html(resp.text())
    except Exception as exc:
        log(f"Warning: dashboard check failed ({exc})")
    return []

def pick_best_live_class(live_ids: List[str]) -> Optional[str]:
    if not live_ids:
        return None
    preferred = get_preferred_class_id(now_pht())
    if preferred and preferred in live_ids:
        return preferred
    return live_ids[0]

def leave_current_class_if_present(page: Page) -> None:
    try:
        leave_locator = page.locator('a:has-text("Leave Class"), button:has-text("Leave Class")').first
        if leave_locator.is_visible(timeout=2000):
            log(f"Clicking 'Leave Class' on {page.url}...")
            leave_locator.click()
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(1500)
    except Exception:
        pass

def simulate_activity(page: Page) -> None:
    page.mouse.move(random.randint(200, 700), random.randint(200, 500), steps=5)
    page.mouse.wheel(0, 30)
    page.wait_for_timeout(150)
    page.mouse.wheel(0, -30)
    page.keyboard.press("Shift")

def stay_in_live_class(page: Page, class_id: str, deadline: datetime) -> None:
    class_url = f"{BASE_URL}/class/{class_id}"
    log(f"Joining live class /class/{class_id} -> {class_url}")
    page.goto(class_url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2000)
    body_text = page.locator("body").inner_text(timeout=5000)
    if "only be in one class at a time" in body_text.lower():
        log("Releasing previous active class...")
        for other_block in SCHEDULE_BLOCKS:
            other_id = other_block["class_id"]
            if other_id != class_id:
                page.goto(f"{BASE_URL}/class/{other_id}", wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(1000)
                leave_current_class_if_present(page)
        page.goto(class_url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2000)

    last_heartbeat_state: Dict[str, object] = {"live": True}
    def on_response(response: Response) -> None:
        if "/presence/heartbeat" in response.url and response.ok:
            try:
                data = response.json()
                last_heartbeat_state["live"] = bool(data.get("live", True))
                last_heartbeat_state["seconds"] = data.get("seconds")
            except Exception:
                pass
    page.on("response", on_response)
    try:
        cycle = 0
        while now_pht() < deadline:
            cycle += 1
            simulate_activity(page)
            if cycle % 3 == 1:
                log(f"[/class/{class_id}] Heartbeat active (seconds={last_heartbeat_state.get('seconds', '?')})")
            if last_heartbeat_state.get("live") is False:
                log(f"[/class/{class_id}] Session ended by teacher.")
                leave_current_class_if_present(page)
                return
            if cycle % 3 == 0:
                live_ids = fetch_live_classes(page)
                best_id = pick_best_live_class(live_ids)
                if best_id and best_id != class_id:
                    log(f"Switching from /class/{class_id} to newly live /class/{best_id}...")
                    leave_current_class_if_present(page)
                    return
            time.sleep(20)
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass

def main() -> int:
    username = os.environ.get("LMS_USERNAME", "").strip()
    password = os.environ.get("LMS_PASSWORD", "").strip()
    max_run_minutes = int(os.environ.get("MAX_RUN_MINUTES", "200"))
    if not username or not password:
        log("ERROR: LMS_USERNAME and LMS_PASSWORD are required.")
        return 1
    deadline = compute_session_deadline(now_pht(), max_run_minutes)
    log(f"Starting LMS Attendance Bot until {deadline.strftime('%I:%M %p PHT')}")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled", "--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding"])
        context = browser.new_context(viewport={"width": 1280, "height": 800}, timezone_id="Asia/Manila")
        context.add_init_script(VISIBILITY_OVERRIDE_JS)
        page = context.new_page()
        if not login(page, username, password):
            browser.close()
            return 1
        while now_pht() < deadline:
            if "/site/login" in page.url and not login(page, username, password):
                time.sleep(30)
                continue
            live_ids = fetch_live_classes(page)
            target_id = pick_best_live_class(live_ids)
            if target_id:
                stay_in_live_class(page, target_id, deadline)
            else:
                log("No live class ('Join Class') yet. Checking again in 60s...")
                time.sleep(60)
        leave_current_class_if_present(page)
        browser.close()
    return 0

if __name__ == "__main__":
    sys.exit(main())
