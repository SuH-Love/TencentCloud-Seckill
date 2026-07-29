"""
腾讯云秒杀抢购脚本 v7.0
自动抢购腾讯云轻量应用服务器秒杀商品
核心改进：httpx异步客户端消除GIL争用 + 毫秒级时间同步 + 11波提前发射
用法: 先运行 get_cookies.py 登录，再运行本脚本
"""

import requests
import httpx
import json
import time
import asyncio
import logging
import os
import re
import signal
import sys
import ctypes
import socket
import threading
import logging.handlers
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

# Windows定时器精度：默认15.6ms，提升到1ms使time.sleep()更精确
_timer_resolved = False
if sys.platform == "win32":
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)
        _timer_resolved = True
    except Exception:
        pass

# =================== 日志配置 =================== #
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
log_filename = f"seckill_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
log_path = os.path.join(LOG_DIR, log_filename)

log = logging.getLogger("seckill")
log.setLevel(logging.DEBUG)
log.propagate = False

_log_queue = logging.handlers.QueueHandler(queue := __import__("queue").Queue(-1))
_log_queue.setLevel(logging.DEBUG)
log.addHandler(_log_queue)

_queue_listener = logging.handlers.QueueListener(
    _log_queue.queue,
    logging.FileHandler(log_path, encoding="utf-8"),
    logging.StreamHandler(),
    respect_handler_level=True
)
_queue_listener.handlers[0].setLevel(logging.DEBUG)
_queue_listener.handlers[0].setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
_queue_listener.handlers[1].setLevel(logging.DEBUG)
_queue_listener.handlers[1].setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
_queue_listener.start()

logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# =================== 用户配置区 =================== #
ACTIVITY_URL = f"https://cloud.tencent.com/act/pro/featured-{__import__('time').strftime('%Y%m')}"  # 活动页地址（自动取当月）
TARGET_GOODS_KEYWORD = "4核4G"           # 商品关键词，用于自动匹配
PREFERRED_REGIONS = ["广州", "上海", "北京"]  # 地域优先顺序
SECKILL_SLOT = "next"                    # "next"=最近一场, "10:00"=指定场次
# 提前发射波次：(秒杀前N秒, 打几个地域, 每地域请求数)
# 每波最多1请求/地域，间隔500ms，避免408排队
# 覆盖时间同步偏移±1.5s + 网络延迟500-1000ms的不确定性
PRE_FIRE_WAVES = [
    (2.5, 1, 1),   # T-2.5s: 广州 1请求
    (2.0, 1, 1),   # T-2.0s: 广州 1请求
    (1.5, 2, 1),   # T-1.5s: 广州+上海 2请求
    (1.0, 2, 1),   # T-1.0s: 广州+上海 2请求
    (0.5, 3, 1),   # T-0.5s: 全地域 3请求
    (0.3, 3, 1),   # T-0.3s: 全地域 3请求
    (0.1, 3, 1),   # T-0.1s: 全地域 3请求
]
WORKERS_PER_REGION = 2                   # 每地域并发数（3触发408，降为2）
FIRE_DURATION = 10                      # 持续抢购秒数（几ms售罄，10s足够）
CHECK_INTERVAL = 0.5                     # 库存检测间隔(秒)
BLIND_FIRE_INTERVAL = 0.5                # 盲打轮间隔(秒)，防限频
MAX_BLIND_ROUNDS = 10                    # 盲打最大轮数（售罄快无需多轮）
SOLD_OUT_EXIT_ROUNDS = 2                 # 连续N轮全部售罄则提前退出
RATE_LIMIT_BACKOFF = 2.0                 # 触发408限频后的退避秒数
SECKILL_TIMEOUT = 3                      # 秒杀期间请求超时(秒)，短超时快速重试
DRY_RUN = "--dry-run" in sys.argv        # 干跑模式，只验证不下单

# =================== 加载Cookie =================== #
session = requests.Session()
skey_value = ""
COOKIE_JAR = {}
with open(os.path.join(SCRIPT_DIR, "cookies.json"), "r", encoding="utf-8") as f:
    cookies = json.load(f)
    for cookie in cookies:
        name = cookie.get('name', '')
        if name == 'nick':
            continue
        if name == 'skey' and 'cloud.tencent.com' in cookie.get('domain', ''):
            skey_value = cookie.get('value', '')
        try:
            session.cookies.set(name, cookie.get('value', ''),
                                domain=cookie.get('domain', ''), path=cookie.get('path', '/'))
        except Exception:
            pass
        domain = cookie.get('domain', '')
        if 'tencent.com' in domain:
            COOKIE_JAR[name] = cookie.get('value', '')


def compute_csrf_token(skey):
    """DJB Hash算法从skey计算x-csrf-token，未登录返回"0" """
    if not skey:
        return "0"
    hash_val = 5381
    for ch in skey:
        hash_val = ((hash_val << 5) + hash_val) + ord(ch)
        hash_val = hash_val & 0xFFFFFFFF
    return str(hash_val & 0x7FFFFFFF)


csrf_token = compute_csrf_token(skey_value)

if not skey_value:
    print("\n" + "=" * 60)
    print("  错误：未检测到有效登录态！")
    print("  请先运行 get_cookies.py 登录腾讯云后重试")
    print("=" * 60)
    sys.exit(1)

# 连接池：50连接，避免秒杀时新建TCP+TLS连接
adapter = requests.adapters.HTTPAdapter(pool_connections=50, pool_maxsize=50)
session.mount("https://", adapter)

# DNS预解析：秒杀前缓存act-api域名，避免秒杀时DNS查询延迟
_DNS_CACHE = {}
_ORIG_GETADDRINFO = socket.getaddrinfo

def _cached_getaddrinfo(host, port, *args, **kwargs):
    if host in _DNS_CACHE:
        return _DNS_CACHE[host]
    result = _ORIG_GETADDRINFO(host, port, *args, **kwargs)
    _DNS_CACHE[host] = result
    return result

socket.getaddrinfo = _cached_getaddrinfo

def pre_resolve_dns():
    """预解析关键域名，填充DNS缓存"""
    for domain in ["act-api.cloud.tencent.com", "cloud.tencent.com"]:
        try:
            socket.getaddrinfo(domain, 443, socket.AF_INET, socket.SOCK_STREAM)
            log.debug(f"DNS预解析: {domain} OK")
        except Exception as e:
            log.debug(f"DNS预解析: {domain} 失败: {e}")

# =================== 自动获取活动参数 =================== #

def fetch_activity_config():
    """从活动页面__NEXT_DATA__中提取活动ID和秒杀商品数据"""
    log.info("正在从活动页面获取配置...")
    try:
        resp = session.get(ACTIVITY_URL, timeout=15,
                           headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"})
        html = resp.text
        match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if not match:
            log.error("未找到 __NEXT_DATA__，页面结构可能已变更")
            return None
        next_data = json.loads(match.group(1))
        activity_data = next_data.get("props", {}).get("pageProps", {}).get("activityData", {})
        if not activity_data:
            log.error("未找到 activityData")
            return None
        activity_id = activity_data.get("activityId")
        flash_result = activity_data.get("twoDaySeckillState", {}).get("flashResult", {})
        return {"activity_id": activity_id, "flash_result": flash_result}
    except Exception as e:
        log.error(f"获取活动配置失败: {e}")
        return None


def find_target_goods(config):
    """用关键词匹配目标商品，收集候选后按时间排序选最近的"""
    flash_result = config["flash_result"]
    activity_id = config["activity_id"]
    now_ts = int(time.time() * 1000)
    candidates = []
    for date_key, slots in flash_result.items():
        for slot in slots:
            start_time_str = slot.get("start_time", "")
            start_ts = slot.get("startTime", 0)
            dynamic_status = slot.get("dynamic_status", -1)
            if SECKILL_SLOT == "next":
                if dynamic_status == 2 or start_ts < now_ts:
                    continue
            elif not start_time_str.startswith(SECKILL_SLOT):
                continue
            for sale in slot.get("sales", []):
                goods = sale.get("$goods", {})
                name = goods.get("name", "")
                descp_list = goods.get("descp", [])
                descp_str = " ".join(d.get("value", "") for d in descp_list)
                if TARGET_GOODS_KEYWORD not in name and TARGET_GOODS_KEYWORD not in descp_str:
                    continue
                act_id = sale.get("act_id") or goods.get("act_id")
                pb_id = goods.get("pb_id")
                default_params = sale.get("defaultGoodsParam", sale.get("defaultOrderParam", {}))
                goods_type = default_params.get("type", "")
                limits = goods.get("limits", {})
                region_ids = []
                region_map = {}
                region_id_limit = limits.get("regionId", {})
                if "constraint" in region_id_limit:
                    for area_id, rids in region_id_limit["constraint"].get("value", {}).items():
                        region_ids = rids
                elif "option" in region_id_limit:
                    region_ids = region_id_limit["option"]
                region_map_data = sale.get("regionMap", {}).get("data", {})
                for area_id, regions_list in region_map_data.items():
                    if isinstance(regions_list, list):
                        for r in regions_list:
                            region_map[r.get("value")] = r.get("label")
                region_options = sale.get("regionOptions", [])
                if isinstance(region_options, list) and not region_map:
                    for r in region_options:
                        region_map[r.get("value")] = r.get("label")
                if not region_map and region_ids:
                    fallback = {1: "广州", 4: "上海", 8: "北京", 5: "香港"}
                    for rid in region_ids:
                        if rid in fallback:
                            region_map[rid] = fallback[rid]
                price_data = sale.get("priceData", {})
                real_cost = price_data.get("real_cost", 0)
                candidates.append({
                    "start_ts": start_ts, "date": date_key,
                    "start_time": start_time_str,
                    "end_time": slot.get("end_time", ""),
                    "dynamic_status": dynamic_status,
                    "dynamic_status_cn": slot.get("dynamic_status_cn", ""),
                    "act_id": act_id, "pb_id": pb_id,
                    "goods_type": goods_type, "goods_name": name,
                    "descp": descp_str, "default_params": default_params,
                    "region_map": region_map, "real_cost": real_cost,
                })
    if not candidates:
        return None, {}
    candidates.sort(key=lambda x: x["start_ts"])
    best = candidates[0]
    log.info(f"匹配到 {len(candidates)} 个候选，选择最近的: {best['date']} {best['start_time']}")
    return best, best["region_map"]


def resolve_seckill_time(slot_info):
    """秒杀时间字符串 → 毫秒时间戳（显式东八区，不受系统时区影响）"""
    date = slot_info.get("date", "")
    start_time = slot_info.get("start_time", "")
    seckill_str = f"{date} {start_time}"
    try:
        dt = datetime.strptime(seckill_str, "%Y-%m-%d %H:%M:%S")
        dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
        ts = int(dt.timestamp()) * 1000
        return seckill_str, ts
    except Exception:
        return None, 0


def resolve_priority_regions(region_map):
    """按用户配置的地域优先顺序排序"""
    ordered = []
    remaining = []
    for rid, name in region_map.items():
        if name in PREFERRED_REGIONS:
            ordered.append((rid, name, PREFERRED_REGIONS.index(name)))
        else:
            remaining.append((rid, name, 999))
    ordered.sort(key=lambda x: x[2])
    remaining.sort(key=lambda x: x[0])
    return [r[0] for r in ordered + remaining], {r[0]: r[1] for r in ordered + remaining}


# =================== 执行自动配置 =================== #
config = fetch_activity_config()
if not config:
    log.error("无法获取活动配置，退出"); exit(1)

slot_info, region_map = find_target_goods(config)
if not slot_info:
    log.error(f"未找到包含 '{TARGET_GOODS_KEYWORD}' 的秒杀商品"); exit(1)

SECKILL_TIME_STR, SECKILL_TIMESTAMP = resolve_seckill_time(slot_info)
if not SECKILL_TIME_STR:
    log.error("无法解析秒杀时间"); exit(1)

PRIORITY_REGIONS, REGION_MAP = resolve_priority_regions(region_map)
ACTIVITY_ID = config["activity_id"]
ACT_ID = slot_info["act_id"]
BUSINESS_ID = slot_info["pb_id"]
GOODS_TYPE = slot_info["goods_type"]
DEFAULT_PARAMS = slot_info.get("default_params", {})

# =================== 预构建请求体 =================== #
BUY_URL = "https://act-api.cloud.tencent.com/dianshi/do-goods"
PREBUILT_BODIES = {}
for _rid in PRIORITY_REGIONS:
    _param = dict(DEFAULT_PARAMS)
    _param["regionId"] = _rid
    _param["goodsNum"] = 1
    _param["area"] = _param.get("area", 1)
    _param["ddocUnionConnect"] = _param.get("ddocUnionConnect", 0)
    _param["scenario"] = _param.get("scenario", "0")
    _param["zone"] = _param.get("zone", "")
    _do_data = {
        "activity_id": ACTIVITY_ID,
        "agent_channel": {"fromChannel": "", "fromSales": "", "isAgentClient": False, "fromUrl": ACTIVITY_URL},
        "business": {"id": BUSINESS_ID, "from": "lightningDeals"},
        "goods": [{"act_id": ACT_ID, "type": GOODS_TYPE, "goods_param": _param}],
        "preview": 0
    }
    PREBUILT_BODIES[_rid] = json.dumps(_do_data, ensure_ascii=False).encode("utf-8")

# =================== 请求头 =================== #
headers = {
    "x-csrf-token": csrf_token,
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    "Referer": ACTIVITY_URL,
    "Origin": "https://cloud.tencent.com",
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest"
}

# =================== 全局状态 =================== #
_lock = threading.Lock()
_stats = {"total": 0, "success": 0, "no_stock": 0, "rate_limited": 0, "fatal": 0, "timeout": 0, "exception": 0}
_stop_event = threading.Event()
_fatal_error = None

FATAL_CODES = {
    1100106: "不满足新用户条件",
    1101003: "csrf_token无效",
    1101002: "未登录",
    1100201: "活动未开始或已结束",
    1100301: "账号异常",
}
_server_time_offset = 0
_last_sync_time = 0
_recent_offsets = []  # 最近的有效偏移量样本，取中位数
_async_loop = None  # 异步事件循环，主程序启动时初始化

def _init_async_loop():
    """初始化异步事件循环（在后台线程中运行）"""
    global _async_loop
    _async_loop = asyncio.new_event_loop()
    t = threading.Thread(target=_async_loop.run_forever, daemon=True)
    t.start()
    return _async_loop

# =================== httpx 异步客户端 =================== #

async_client = httpx.AsyncClient(
    http2=True,
    limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
    timeout=httpx.Timeout(SECKILL_TIMEOUT, connect=2.0),
    headers=headers,
    cookies=COOKIE_JAR,
)

# =================== 工具函数 =================== #

def _sync_once():
    """单次时间同步，HEAD请求测纯网络RTT"""
    try:
        for url in ["https://act-api.cloud.tencent.com/dianshi/check-available", ACTIVITY_URL]:
            now_local = int(time.time() * 1000)
            try:
                resp = session.head(url, timeout=3, headers=headers, allow_redirects=True)
                server_time = resp.headers.get("Date")
                if server_time:
                    dt = datetime.strptime(server_time, "%a, %d %b %Y %H:%M:%S GMT")
                    beijing_time = dt + timedelta(hours=8)
                    server_ts = int(beijing_time.timestamp() * 1000)
                    rtt_ms = resp.elapsed.total_seconds() * 1000
                    local_mid = now_local + int(rtt_ms / 2)
                    raw_offset = server_ts - local_mid
                    return raw_offset, rtt_ms
            except Exception:
                continue
    except Exception:
        pass
    return None


def get_server_time(force_sync=False):
    """获取服务器时间，多样本取中位数偏移，秒杀前加速同步"""
    global _server_time_offset, _last_sync_time, _recent_offsets
    now_local = int(time.time() * 1000)
    with _lock:
        cached_offset = _server_time_offset
        cached_last_sync = _last_sync_time
    remaining = (SECKILL_TIMESTAMP - (now_local + cached_offset)) / 1000
    if remaining > 60:
        sync_interval = 30000
    elif remaining > 10:
        sync_interval = 5000
    else:
        sync_interval = 2000
    if not force_sync and (now_local - cached_last_sync) < sync_interval:
        return now_local + cached_offset
    if remaining <= 10:
        r = _sync_once()
        samples = [r] if r else []
    elif remaining <= 30:
        samples = []
        for _ in range(3):
            r = _sync_once()
            if r:
                samples.append(r)
            time.sleep(0.1)
    else:
        r = _sync_once()
        samples = [r] if r else []
    with _lock:
        for raw_offset, rtt_ms in samples:
            adjusted = raw_offset - 500
            _recent_offsets.append(adjusted)
        _recent_offsets = _recent_offsets[-10:]
        if _recent_offsets:
            sorted_offsets = sorted(_recent_offsets)
            mid = len(sorted_offsets) // 2
            _server_time_offset = sorted_offsets[mid]
        _last_sync_time = now_local
        final_offset = _server_time_offset
    if samples:
        log.debug(f"时间同步: {len(samples)}样本 offset={final_offset}ms recent={_recent_offsets[-3:]}")
    else:
        log.debug("服务器时间获取失败，使用缓存偏移")
    return now_local + final_offset


def verify_login():
    """验证Cookie和csrf_token有效性，返回True/False"""
    log.info("验证登录态...")
    try:
        check_data = {"activity_id": ACTIVITY_ID, "goods": [{"act_id": ACT_ID}], "preview": 0}
        resp = session.post("https://act-api.cloud.tencent.com/dianshi/check-available",
                            json=check_data, headers=headers, timeout=8)
        result = resp.json()
        code = result.get("code")
        code_str = str(code).upper() if isinstance(code, str) else None
        if code == 0:
            log.info("登录态验证通过")
            return True
        if code == 1101003 or code_str == "CSRF-ERROR":
            log.error("csrf_token无效！请重新运行 get_cookies.py 获取Cookie")
            return False
        if code in (1101002, -1) or code_str in ("NOT-LOGINED", "UNAUTHORIZED"):
            log.error(f"登录态已失效！(code={code}) 请重新运行 get_cookies.py 登录")
            return False
        log.info(f"登录态验证通过 (API code={code})")
        return True
    except Exception as e:
        log.warning(f"登录态验证异常: {e}（不阻断运行）")
        return True


def pre_warm():
    """预热连接池：同步预热act-api + 异步预热httpx（在_async_loop中）"""
    log.info("预热连接池...")
    try:
        for _ in range(3):
            session.post("https://act-api.cloud.tencent.com/dianshi/check-available",
                         json={"activity_id": ACTIVITY_ID, "goods": [{"act_id": ACT_ID}], "preview": 0},
                         headers=headers, timeout=5)
        log.info("连接池预热完成 (requests)")
    except Exception as e:
        log.warning(f"预热失败（不影响抢购）: {e}")
    if _async_loop is not None:
        try:
            afut = asyncio.run_coroutine_threadsafe(_async_warm(), _async_loop)
            afut.result(timeout=10)
            log.info("连接池预热完成 (httpx)")
        except Exception as e:
            log.warning(f"httpx预热失败: {e}")


async def _async_warm():
    """异步预热httpx连接"""
    for _ in range(3):
        try:
            await async_client.post(
                "https://act-api.cloud.tencent.com/dianshi/check-available",
                content=json.dumps({"activity_id": ACTIVITY_ID, "goods": [{"act_id": ACT_ID}], "preview": 0}).encode("utf-8"),
            )
        except Exception:
            pass


# =================== 异步秒杀核心 =================== #

async def _async_buy(region_id, attempt):
    """异步单次购买请求，httpx无GIL争用"""
    if _stop_event.is_set():
        return True
    if DRY_RUN:
        log.info(f"[干跑] 模拟下单: region={REGION_MAP.get(region_id, region_id)} attempt={attempt}")
        return False
    req_id = f"R{region_id}-{attempt}"
    body = PREBUILT_BODIES.get(region_id)
    if not body:
        return False
    try:
        t0 = time.perf_counter()
        resp = await async_client.post(BUY_URL, content=body)
        latency_ms = (time.perf_counter() - t0) * 1000
        result = resp.json()
        code = result.get("code")
        msg = result.get("msg", "")
        with _lock:
            _stats["total"] += 1
        log.debug(f"[{req_id}] {latency_ms:.0f}ms code={code} msg={msg}")
        if code == 0:
            with _lock:
                _stats["success"] += 1
            log.info(f"[{req_id}] 抢购成功！地域: {REGION_MAP.get(region_id, region_id)}")
            log.info(f"   完整响应: {json.dumps(result, ensure_ascii=False)}")
            _stop_event.set()
            return True
        if code == 1100151 or code == 1100136:
            with _lock:
                _stats["no_stock"] += 1
        elif code == 1101001:
            pass
        elif code == 408:
            with _lock:
                _stats["rate_limited"] += 1
        elif code in (1100205, 1100110, 1101408):
            pass
        elif code == 1101411:
            with _lock:
                _stats["success"] += 1
            log.info(f"[{req_id}] 已有未支付订单，抢购成功！")
            _stop_event.set()
            return True
        elif code in FATAL_CODES:
            with _lock:
                _stats["fatal"] += 1
                _fatal_error = f"code={code} {FATAL_CODES[code]}"
            log.error(f"[{req_id}] 致命错误 code={code}: {FATAL_CODES[code]}")
            _stop_event.set()
            return True
        else:
            with _lock:
                _stats["other_error"] = _stats.get("other_error", 0) + 1
            log.warning(f"[{req_id}] code={code} msg={msg}")
        return False
    except httpx.TimeoutException:
        with _lock:
            _stats["timeout"] += 1
        log.debug(f"[{req_id}] 超时")
        return False
    except Exception as e:
        with _lock:
            _stats["exception"] += 1
        log.debug(f"[{req_id}] 异常: {e}")
        return False


async def _async_fire_barrage(regions=None, prefix="b"):
    """异步并发购买，任一成功即返回"""
    if regions is None:
        regions = PRIORITY_REGIONS
    tasks = []
    for rid in regions:
        for w in range(WORKERS_PER_REGION):
            tasks.append(asyncio.ensure_future(_async_buy(rid, f"{prefix}{w}")))
    for f in asyncio.as_completed(tasks, timeout=SECKILL_TIMEOUT + 2):
        try:
            if f.result():
                for t in tasks:
                    t.cancel()
                return True
        except Exception:
            pass
    return False


def fire_barrage(regions=None, prefix="b"):
    """同步包装：提交异步任务到后台loop，等待结果"""
    if _async_loop is None:
        return _sync_fire_barrage_fallback(regions, prefix)
    try:
        afut = asyncio.run_coroutine_threadsafe(
            _async_fire_barrage(regions, prefix), _async_loop
        )
        return afut.result(timeout=SECKILL_TIMEOUT + 5)
    except Exception as e:
        log.debug(f"异步盲打异常，降级同步: {e}")
        return _sync_fire_barrage_fallback(regions, prefix)


def _sync_fire_barrage_fallback(regions=None, prefix="b"):
    """同步降级：异步不可用时用requests+线程"""
    if regions is None:
        regions = PRIORITY_REGIONS
    total_workers = len(regions) * WORKERS_PER_REGION
    with ThreadPoolExecutor(max_workers=total_workers) as pool:
        futures = []
        for i, rid in enumerate(regions):
            for w in range(WORKERS_PER_REGION):
                futures.append(pool.submit(_sync_buy_fallback, rid, f"{prefix}{w}"))
                if i * WORKERS_PER_REGION + w < total_workers - 1:
                    time.sleep(0.005)
        for f in as_completed(futures, timeout=SECKILL_TIMEOUT + 2):
            try:
                if f.result():
                    return True
            except Exception:
                pass
    return False


def _sync_buy_fallback(region_id, attempt):
    """同步降级购买请求"""
    if _stop_event.is_set():
        return True
    req_id = f"R{region_id}-{attempt}"
    body = PREBUILT_BODIES.get(region_id)
    if not body:
        return False
    try:
        resp = session.post(BUY_URL, data=body, headers=headers, timeout=SECKILL_TIMEOUT)
        result = resp.json()
        code = result.get("code")
        with _lock:
            _stats["total"] += 1
        if code == 0:
            with _lock:
                _stats["success"] += 1
            log.info(f"[{req_id}] 抢购成功！")
            _stop_event.set()
            return True
        if code == 1100151 or code == 1100136:
            with _lock:
                _stats["no_stock"] += 1
        elif code == 408:
            with _lock:
                _stats["rate_limited"] += 1
        elif code in FATAL_CODES:
            with _lock:
                _stats["fatal"] += 1
                _fatal_error = f"code={code} {FATAL_CODES[code]}"
            _stop_event.set()
            return True
        return False
    except Exception:
        with _lock:
            _stats["exception"] += 1
        return False


def check_inventory():
    """检查各地域库存，返回有库存的地域ID列表"""
    check_data = {"activity_id": ACTIVITY_ID, "goods": [{"act_id": ACT_ID}], "preview": 0}
    try:
        resp = session.post("https://act-api.cloud.tencent.com/dianshi/check-available",
                            json=check_data, headers=headers, timeout=5)
        result = resp.json()
        if result.get("code") != 0:
            log.debug(f"库存检查返回异常: code={result.get('code')}")
            return []
        goods_data = result.get("data", [{}])[0]
        if goods_data.get("available") != 1:
            log.debug("商品整体无货")
            return []
        quota = goods_data.get("quota", {})
        available_regions = []
        for rid in PRIORITY_REGIONS:
            name = REGION_MAP.get(rid, str(rid))
            avail = quota.get(str(rid), {}).get(GOODS_TYPE, {}).get("available", 0)
            if avail > 0:
                log.info(f"{name}有库存！(available={avail})")
                available_regions.append(rid)
        if not available_regions:
            log.debug("所有地域均无库存")
        return available_regions
    except Exception as e:
        log.debug(f"库存检查异常: {e}")
        return []


def keep_alive():
    """心跳保活，防止skey服务端过期+保持连接热"""
    try:
        resp = session.get(ACTIVITY_URL, timeout=10, headers={"User-Agent": headers["User-Agent"]})
        log.debug(f"心跳保活: status={resp.status_code}")
    except Exception:
        pass
    try:
        session.post("https://act-api.cloud.tencent.com/dianshi/check-available",
                     json={"activity_id": ACTIVITY_ID, "goods": [{"act_id": ACT_ID}], "preview": 0},
                     headers=headers, timeout=5)
    except Exception:
        pass
    if _async_loop is not None:
        try:
            asyncio.run_coroutine_threadsafe(_async_warm_once(), _async_loop).result(timeout=5)
        except Exception:
            pass


async def _async_warm_once():
    """单次异步保活"""
    try:
        await async_client.post(
            "https://act-api.cloud.tencent.com/dianshi/check-available",
            content=json.dumps({"activity_id": ACTIVITY_ID, "goods": [{"act_id": ACT_ID}], "preview": 0}).encode("utf-8"),
        )
    except Exception:
        pass


def recompute_csrf():
    """检测skey变化时重新计算csrf_token"""
    global csrf_token, headers, skey_value
    new_skey = session.cookies.get("skey", domain=".cloud.tencent.com")
    if new_skey and new_skey != skey_value:
        log.info("检测到skey变化，重新计算csrf_token")
        skey_value = new_skey
        csrf_token = compute_csrf_token(new_skey)
        headers["x-csrf-token"] = csrf_token
        async_client.headers["x-csrf-token"] = csrf_token


def get_stats():
    """返回统计摘要"""
    return dict(_stats)


# =================== 主程序 =================== #
if __name__ == "__main__":
    def signal_handler(sig, frame):
        log.info("收到中断信号，停止抢购...")
        _stop_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    PRE_FIRE_EARLIEST = SECKILL_TIMESTAMP - int(PRE_FIRE_WAVES[0][0] * 1000)
    run_start = datetime.now()

    print()
    print("=" * 60)
    print(f"  腾讯云秒杀脚本 v7.0{' [干跑模式]' if DRY_RUN else ''}")
    print("=" * 60)
    print(f"  商品: {slot_info.get('goods_name', 'N/A')}")
    print(f"  规格: {slot_info.get('descp', 'N/A')}")
    print(f"  价格: {slot_info.get('real_cost', 0)/100:.0f}元")
    print(f"  秒杀时间: {SECKILL_TIME_STR} ({slot_info.get('dynamic_status_cn', '')})")
    print(f"  优先地域: {' > '.join(REGION_MAP.get(r, str(r)) for r in PRIORITY_REGIONS)}")
    print(f"  并发数: {WORKERS_PER_REGION}/地域")
    print(f"  提前波次: {len(PRE_FIRE_WAVES)}波 {sum(r*n for _,r,n in PRE_FIRE_WAVES)}请求")
    print(f"  HTTP引擎: httpx/async+HTTP/2 (降级: requests+线程)")
    print(f"  CSRF Token: {csrf_token} (skey DJB hash)")
    print(f"  日志文件: {log_path}")
    print("=" * 60)
    print()

    if not verify_login():
        log.error("登录态无效，退出"); exit(1)

    _init_async_loop()
    pre_resolve_dns()
    pre_warm()

    log.info("等待秒杀...")
    phase = "waiting"
    blind_fire_count = 0
    sold_out_streak = 0
    fired_waves = set()


    while not _stop_event.is_set():
        now = get_server_time()

        # 阶段3：秒杀进行中
        if now >= SECKILL_TIMESTAMP:
            if phase != "firing":
                phase = "firing"
                fire_start = time.time()
                print()
                log.info("=" * 40)
                log.info("  秒杀开始！")
                log.info("=" * 40)
                log.info("等待1秒让pre-fire排队请求消化...")
                time.sleep(1.0)

            elapsed = time.time() - fire_start
            if FIRE_DURATION > 0 and elapsed >= FIRE_DURATION:
                log.info(f"持续时间到（{FIRE_DURATION}秒），停止")
                _stop_event.set()
                break

            if elapsed < 10:
                blind_fire_count += 1
                if blind_fire_count > MAX_BLIND_ROUNDS:
                    log.info(f"盲打已达{MAX_BLIND_ROUNDS}轮上限，切换库存检测")
                else:
                    log.info(f"盲打第{blind_fire_count}轮")
                    prev_no_stock = _stats["no_stock"]
                    if fire_barrage(PRIORITY_REGIONS):
                        break
                    if DRY_RUN:
                        log.info("[干跑模式] 验证完成，退出")
                        _stop_event.set()
                        break
                    if _stats["no_stock"] > prev_no_stock and _stats["total"] > 0:
                        no_stock_ratio = (_stats["no_stock"] - prev_no_stock) / max(len(PRIORITY_REGIONS) * WORKERS_PER_REGION, 1)
                        if no_stock_ratio >= 0.5:
                            sold_out_streak += 1
                        else:
                            sold_out_streak = 0
                    else:
                        sold_out_streak = 0
                    if sold_out_streak >= SOLD_OUT_EXIT_ROUNDS:
                        log.info(f"连续{SOLD_OUT_EXIT_ROUNDS}轮全部售罄，提前退出")
                        _stop_event.set()
                        break
                    rate_count = _stats.get("rate_limited", 0)
                    sleep_time = RATE_LIMIT_BACKOFF if rate_count > len(PRIORITY_REGIONS) * WORKERS_PER_REGION * 0.5 else BLIND_FIRE_INTERVAL
                    time.sleep(sleep_time)
            else:
                available = check_inventory()
                if available:
                    log.info(f"对有库存的地域发起攻击: {[REGION_MAP.get(r, r) for r in available]}")
                    if fire_barrage(available):
                        break
                time.sleep(CHECK_INTERVAL)

        # 阶段2：多波次提前发射（异步发射即忘，用缓存偏移不触发网络同步）
        elif now >= PRE_FIRE_EARLIEST:
            if phase != "pre_fire":
                phase = "pre_fire"
                log.info("进入提前发射阶段...")
            now = int(time.time() * 1000) + _server_time_offset
            for i, (seconds_before, region_count, num_per_region) in enumerate(PRE_FIRE_WAVES):
                wave_ts = SECKILL_TIMESTAMP - int(seconds_before * 1000)
                if now >= wave_ts and i not in fired_waves:
                    fired_waves.add(i)
                    remaining_sec = (SECKILL_TIMESTAMP - now) / 1000
                    target_regions = PRIORITY_REGIONS[:region_count]
                    total_req = region_count * num_per_region
                    log.info(f"波次{i+1} T-{seconds_before}s {total_req}请求→{[REGION_MAP.get(r,r) for r in target_regions]} 距秒杀{remaining_sec:.1f}s")
                    for rid in target_regions:
                        for w in range(num_per_region):
                            if _stop_event.is_set():
                                break
                            fut = asyncio.run_coroutine_threadsafe(
                                _async_buy(rid, f"w{i}-{rid}-{w}"), _async_loop
                            )
                            fut.add_done_callback(lambda f: log.debug(f"pre-fire异常: {f.exception()}") if f.exception() else None)
                            if num_per_region > 1 or region_count > 1:
                                time.sleep(0.05)
                        if _stop_event.is_set():
                            break

            time.sleep(0.05)

        # 阶段1：等待秒杀
        else:
            remaining_sec = (SECKILL_TIMESTAMP - now) / 1000
            if remaining_sec > 60:
                log.info(f"距离秒杀还有 {int(remaining_sec // 60)}分{int(remaining_sec % 60)}秒")
                keep_alive()
                recompute_csrf()
                time.sleep(30)
            elif remaining_sec > 30:
                log.info(f"距离秒杀还有 {int(remaining_sec)}秒")
                keep_alive()
                recompute_csrf()
                time.sleep(5)
            elif remaining_sec > 10:
                if phase != "pre_warm":
                    phase = "pre_warm"
                    log.info(f"距离秒杀还有 {int(remaining_sec)}秒，预热连接池...")
                    pre_warm()
                keep_alive()
                recompute_csrf()
                time.sleep(3)
            else:
                recompute_csrf()
                if remaining_sec < PRE_FIRE_WAVES[0][0] + 0.3:
                    time.sleep(0.05)
                else:
                    log.info(f"倒计时 {remaining_sec:.1f}秒...")
                    time.sleep(0.3)

    # 清理
    try:
        if _async_loop and _async_loop.is_running():
            asyncio.run_coroutine_threadsafe(async_client.aclose(), _async_loop).result(timeout=5)
    except Exception:
        pass
    try:
        if _async_loop:
            _async_loop.call_soon_threadsafe(_async_loop.stop)
    except Exception:
        pass

    # 统计报告
    run_end = datetime.now()
    elapsed = (run_end - run_start).total_seconds()
    stats = get_stats()

    print()
    print("=" * 60)
    print("  抢购统计报告")
    print("=" * 60)
    print(f"  运行时长: {elapsed:.1f}秒")
    print(f"  总请求数: {stats['total']}")
    print(f"  成功:     {stats['success']}")
    print(f"  余量不足: {stats['no_stock']}")
    print(f"  限频(408): {stats.get('rate_limited', 0)}")
    print(f"  致命错误: {stats.get('fatal', 0)}")
    print(f"  其他错误: {stats.get('other_error', 0)}")
    print(f"  超时:     {stats['timeout']}")
    print(f"  异常:     {stats['exception']}")
    if _fatal_error:
        print(f"  致命原因: {_fatal_error}")
    print(f"  成功率:   {stats['success']/max(stats['total'],1)*100:.1f}%")
    print(f"  日志文件: {log_path}")
    print("=" * 60)

    if _timer_resolved:
        try:
            ctypes.windll.winmm.timeEndPeriod(1)
        except Exception:
            pass
