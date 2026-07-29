"""
腾讯云 Cookie 获取工具
用法:
  python get_cookies.py              # 交互式选择获取方式
  python get_cookies.py --mode pw    # Playwright 方式
  python get_cookies.py --mode cdp   # Chrome CDP 方式
登录后自动保存Cookie到 cookies.json，供 snap_up_server.py 使用
"""

import json
import time
import os
import sys
import subprocess
import argparse

ACTIVITY_URL = f"https://cloud.tencent.com/act/pro/featured-{time.strftime('%Y%m')}"
REQUIRED_COOKIES = ["skey", "uin", "tinyid"]
KEY_COOKIE_NAMES = ["skey", "uin", "qcmainCSRFToken", "qcact_fingerprint", "tinyid", "loginType"]

DEBUG_PORT = 9222
USER_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chrome_profile")


def check_login_status(cookies):
    tc_cookies = {}
    for c in cookies:
        name = c.get("name", "")
        domain = c.get("domain", "")
        if "cloud.tencent.com" in domain:
            tc_cookies[name] = c.get("value", "")
    missing = [k for k in REQUIRED_COOKIES if k not in tc_cookies or not tc_cookies[k]]
    return len(missing) == 0, missing


def print_login_error(missing):
    print()
    print("=" * 50)
    print("  ⚠️ 登录态不完整！")
    print("=" * 50)
    print(f"  缺少关键Cookie: {missing}")
    print("  可能原因：")
    print("    - 未完成扫码登录")
    print("    - 登录后页面未加载完成")
    print("    - Cookie域名不匹配")
    print()
    print("  请确认浏览器中已成功登录腾讯云，")
    print("  然后重新运行本脚本。")
    print("=" * 50)


def display_key_cookies(cookies):
    tc_cookies = [c for c in cookies if "cloud.tencent.com" in c.get("domain", "")]
    key_map = {}
    for c in tc_cookies:
        name = c.get("name", "")
        if name in KEY_COOKIE_NAMES:
            val = c.get("value", "")
            key_map[name] = val[:20] + "..." if len(val) > 20 else val
    print(f"关键Cookie: {key_map}")


def save_cookies_to_file(cookies):
    with open("cookies.json", "w", encoding="utf-8") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)
    tc_count = sum(1 for c in cookies if "tencent.com" in c.get("domain", ""))
    print(f"已保存 {len(cookies)} 条Cookie到 cookies.json (腾讯云: {tc_count} 条)")


# ===== Playwright 方式 =====

def get_cookies_playwright():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        print("正在打开腾讯云登录页面...")
        page.goto("https://cloud.tencent.com/login", timeout=60000)

        print("=" * 50)
        print("请在浏览器中扫码登录腾讯云")
        print("登录成功后，在这里按回车键继续...")
        print("=" * 50)
        input()

        print("正在导航到活动页面以获取完整Cookie...")
        try:
            page.goto(ACTIVITY_URL, timeout=30000, wait_until="domcontentloaded")
        except Exception:
            print("  页面跳转被重定向，继续获取Cookie...")
        time.sleep(3)

        print("正在保存Cookie...")
        cookies = context.cookies()

        valid, missing = check_login_status(cookies)
        if not valid:
            print_login_error(missing)
            browser.close()
            return False

        display_key_cookies(cookies)
        save_cookies_to_file(cookies)
        browser.close()
        return True


# ===== Chrome CDP 方式 =====

def find_chrome_path():
    candidates = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Chromium\Application\chrome.exe"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def start_chrome_debug():
    import requests

    chrome_path = find_chrome_path()
    if not chrome_path:
        print("未找到Chrome，请手动以调试模式启动：")
        print(f'  chrome.exe --remote-debugging-port={DEBUG_PORT} --remote-allow-origins=* --user-data-dir=chrome_profile')
        return None

    os.makedirs(USER_DATA_DIR, exist_ok=True)
    print(f"启动Chrome调试实例 (端口 {DEBUG_PORT})...")
    print(f"  Chrome路径: {chrome_path}")

    cmd = [
        chrome_path,
        f"--remote-debugging-port={DEBUG_PORT}",
        f"--user-data-dir={USER_DATA_DIR}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "https://cloud.tencent.com/login"
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"  进程PID: {proc.pid}")

    for i in range(15):
        time.sleep(1)
        try:
            requests.get(f"http://localhost:{DEBUG_PORT}/json", timeout=2)
            print(f"  Chrome就绪 (等待{i+1}秒)")
            return proc
        except Exception:
            pass

    print("Chrome启动超时，请检查是否有其他Chrome实例占用调试端口")
    return None


def get_cookies_cdp():
    import requests

    proc = None
    try:
        tabs_resp = requests.get(f"http://localhost:{DEBUG_PORT}/json", timeout=2)
        tabs_resp.json()
        print("检测到已运行的Chrome调试实例，直接使用")
    except Exception:
        proc = start_chrome_debug()
        if not proc:
            print("\n自动启动失败，请手动执行：")
            print(f'  chrome.exe --remote-debugging-port={DEBUG_PORT} --remote-allow-origins=* --user-data-dir=chrome_profile https://cloud.tencent.com/login')
            print(f"  然后重新运行本脚本")
            return False

    input("\n登录完成后，按回车键获取Cookie...")

    try:
        tabs = requests.get(f"http://localhost:{DEBUG_PORT}/json", timeout=5).json()
    except Exception as e:
        print(f"无法连接Chrome调试端口: {e}")
        return False

    print(f"检测到 {len(tabs)} 个标签页")

    target_tab = None
    for tab in tabs:
        url = tab.get("url", "")
        if "cloud.tencent.com" in url:
            target_tab = tab
            print(f"找到腾讯云标签页: {url[:80]}")
            break

    if not target_tab:
        target_tab = tabs[0]
        print(f"未找到腾讯云标签页，使用第一个: {target_tab.get('url', '')[:80]}")

    ws_url = target_tab.get("webSocketDebuggerUrl")
    if not ws_url:
        print("无法获取WebSocket调试地址")
        return False

    try:
        import websocket
    except ImportError:
        print("缺少依赖，正在安装 websocket-client...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "websocket-client", "-q"])
        import websocket

    ws = websocket.create_connection(ws_url)

    try:
        nav_cmd = {"id": 0, "method": "Page.navigate", "params": {"url": ACTIVITY_URL}}
        ws.send(json.dumps(nav_cmd))
        ws.recv()
        time.sleep(3)
    except Exception:
        pass

    cmd = {"id": 1, "method": "Network.getAllCookies", "params": {}}
    ws.send(json.dumps(cmd))

    result = json.loads(ws.recv())
    cookies_raw = result.get("result", {}).get("cookies", [])

    cookies = []
    for c in cookies_raw:
        cookies.append({
            "name": c.get("name", ""),
            "value": c.get("value", ""),
            "domain": c.get("domain", ""),
            "path": c.get("path", "/"),
            "expires": c.get("expires", -1),
            "httpOnly": c.get("httpOnly", False),
            "secure": c.get("secure", False),
            "sameSite": c.get("sameSite", "None")
        })

    valid, missing = check_login_status(cookies)
    if not valid:
        print_login_error(missing)
        ws.close()
        return False

    display_key_cookies(cookies)
    save_cookies_to_file(cookies)
    ws.close()

    if proc:
        print("\n提示: Chrome调试实例仍在运行，可手动关闭")

    return True


# ===== 主入口 =====

def main():
    parser = argparse.ArgumentParser(description="腾讯云 Cookie 获取工具")
    parser.add_argument("--mode", "-m", choices=["pw", "cdp"], default=None,
                        help="获取方式: pw=Playwright, cdp=Chrome CDP (不指定则交互选择)")
    args = parser.parse_args()

    mode = args.mode
    if not mode:
        print("=" * 50)
        print("  腾讯云 Cookie 获取工具")
        print("=" * 50)
        print()
        print("请选择Cookie获取方式：")
        print("  1. Playwright (需安装playwright库，通过自动化框架控制浏览器)")
        print("  2. Chrome CDP  (需本机Chrome，通过调试协议直连浏览器)")
        print()
        choice = input("请输入选项 (1/2): ").strip()
        mode = "pw" if choice == "1" else "cdp"

    if mode == "pw":
        print("\n使用 Playwright 方式获取Cookie...")
        success = get_cookies_playwright()
    else:
        print("\n使用 Chrome CDP 方式获取Cookie...")
        success = get_cookies_cdp()

    if success:
        print("\n登录态验证通过，可以运行 snap_up_server.py")
        print("提示: 建议秒杀前10-30分钟重新运行本脚本获取最新Cookie")
    else:
        print("\nCookie获取失败")


if __name__ == "__main__":
    main()
