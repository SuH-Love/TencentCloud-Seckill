"""
腾讯云秒杀抢购脚本 v7.9
httpx 异步 + HTTP/2 消除 GIL 争用；毫秒级时间同步；T-2s 起 10 波密集施压，T0 后全地域火力。

地域: 只抢哪些见「用户配置区」的 BUY_REGIONS（留空=全抢）
用法: 先运行 get_cookies.py 登录，再运行 python seckill.py
      --dry-run      只验证配置与参数，不下单
      --simulate=N   N 秒后模拟开抢，演练波次时序（强制干跑）
"""

import requests
try:
    import httpx
    HTTPX_OK = True
except ImportError:          # 缺包时整体降级为 requests + 线程池，不影响抢购
    httpx = None
    HTTPX_OK = False
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
import unicodedata
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

# 状态行（倒计时）用 \r 原地刷新，写日志前由 _ConsoleHandler 擦掉，否则会粘连成一行
_status_active = False
_status_isatty = sys.stdout.isatty() if hasattr(sys.stdout, "isatty") else False
_status_lock = threading.Lock()   # 边界：状态行刷新（主线程）与日志 emit（日志线程）互斥


class _ConsoleHandler(logging.StreamHandler):
    """控制台处理器：emit 前先擦除原地刷新的状态行，保证日志独占整行"""

    def emit(self, record):
        global _status_active
        with _status_lock:
            if _status_active:
                try:
                    self.stream.write("\r" + " " * 66 + "\r")
                    self.stream.flush()
                except Exception:
                    pass
                _status_active = False
            super().emit(record)

_queue_listener = logging.handlers.QueueListener(
    _log_queue.queue,
    logging.FileHandler(log_path, encoding="utf-8"),
    _ConsoleHandler(),
    respect_handler_level=True
)
_queue_listener.handlers[0].setLevel(logging.DEBUG)
_queue_listener.handlers[0].setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
# 控制台只输出INFO+，DEBUG细节（408逐条、时间同步、心跳）仅写入日志文件
_queue_listener.handlers[1].setLevel(logging.INFO)
_queue_listener.handlers[1].setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
_queue_listener.start()


def stop_logging():
    """停止异步日志监听（幂等）：把队列里剩余的日志全部落盘后再返回"""
    global _queue_listener
    if _queue_listener is None:
        return
    listener, _queue_listener = _queue_listener, None
    try:
        listener.stop()
    except Exception:
        pass

# 任何退出路径（含 exit(1) / 未捕获异常）都先 flush 日志队列，避免尾部日志丢失
import atexit as _atexit
_atexit.register(stop_logging)

logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# =================== 用户配置区 =================== #
# 旧入口 featured-YYYYMM 已下线（会报"未找到 activityData"），活动更替时把新地址插到最前面即可。
ACTIVITY_URLS = [
    "https://cloud.tencent.com/act/pro/double11-202611",   # 双11秒杀（当前有效，#MS 为秒杀模块锚点）
    f"https://cloud.tencent.com/act/pro/featured-{__import__('time').strftime('%Y%m')}",  # 旧月度精选（已下线，兜底保留）
]
ACTIVITY_URL = ACTIVITY_URLS[0]           # 实际生效的入口（启动时自动确定）
TARGET_GOODS_KEYWORD = "4核4G"            # 商品关键词
SECKILL_SLOT = "next"                     # "next"=最近一场, "10:00"=指定场次

BUY_REGIONS = []                          # 只抢这些地域；留空 = 全抢。填中文名，"广州" 也能命中 "广州三区"
PREFERRED_REGIONS = ["广州", "上海", "北京"]   # 出手顺序，仅 BUY_REGIONS 留空时生效
# 提前发射波次：(秒杀前N秒, 打几个地域, 每地域请求数)
# 表面 408 不代表作废，请求仍可能进服务端队列 —— 密度才是命中的前提。
PRE_FIRE_WAVES = [
    (2.0, 1, 1),
    (1.8, 1, 1),
    (1.6, 1, 1),
    (1.4, 2, 1),
    (1.2, 2, 1),
    (1.0, 3, 2),
    (0.8, 3, 1),
    (0.6, 3, 1),
    (0.4, 3, 2),
    (0.2, 3, 1),
]
PROBE_FIRST = False       # T0 首发探测：密集波次已覆盖 T0，无需先试探
# 408 退避：密度优先，退避仅兜底，上限 2 秒（窗口只有 10 秒，长退避等于弃权）
RATE_LIMIT_BACKOFF = 1.0
RATE_LIMIT_BACKOFF_MAX = 2.0
WORKERS_PER_REGION = 2                    # 每地域并发数（3 会触发 408）
FIRE_DURATION = 10                        # 持续抢购秒数（通常几 ms 售罄）
CHECK_INTERVAL = 0.5                      # 库存检测间隔(秒)
BLIND_FIRE_INTERVAL = 0.5                 # 盲打轮间隔(秒)
MAX_BLIND_ROUNDS = 20                     # 盲打最大轮数（0.5s/轮覆盖 10 秒窗口）
SOLD_OUT_EXIT_ROUNDS = 2                  # 连续 N 轮全售罄则提前退出
SECKILL_TIMEOUT = 3                       # 秒杀期间请求超时(秒)
ELIGIBILITY_RECHECK_INTERVAL = 60         # 等待期资格复检间隔(秒)
DRY_RUN = "--dry-run" in sys.argv         # 干跑：只验证配置，不下单

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


def fetch_activity_config():
    """遍历候选活动页，从__NEXT_DATA__中提取活动ID和秒杀商品数据"""
    global ACTIVITY_URL
    ua_headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"}
    for url in ACTIVITY_URLS:
        log.info(f"正在从活动页面获取配置: {url}")
        try:
            resp = session.get(url, timeout=15, headers=ua_headers)
            html = resp.text
            match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
            if not match:
                log.warning(f"未找到 __NEXT_DATA__，跳过该入口: {url}")
                continue
            next_data = json.loads(match.group(1))
            activity_data = next_data.get("props", {}).get("pageProps", {}).get("activityData", {})
            if not activity_data:
                log.warning(f"未找到 activityData，跳过该入口: {url}")
                continue
            activity_id = activity_data.get("activityId")
            flash_result = activity_data.get("twoDaySeckillState", {}).get("flashResult", {})
            if not activity_id or not flash_result:
                log.warning(f"入口无有效秒杀数据(activityId/flashResult为空)，跳过: {url}")
                continue
            ACTIVITY_URL = url  # 记录生效入口，后续Referer/fromUrl使用
            log.info(f"活动入口生效: {url} (activityId={activity_id})")
            return {"activity_id": activity_id, "flash_result": flash_result}
        except Exception as e:
            log.warning(f"获取活动配置失败({url}): {e}")
            continue
    log.error("所有候选活动入口均无法获取秒杀数据，请更新 ACTIVITY_URLS")
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
                    "user_condition": goods.get("user_condition", {}),
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


def _region_pick(names, keys):
    """把用户写的地域词映射到站点实际地域名：该词能精确命中就只取精确项，
    否则退化为双向包含（站点偶有 "广州三区" 这类带后缀 label，用户只写 "广州" 也能对上）"""
    hit = []
    for k in keys:
        if not k:
            continue
        matched = [k] if k in names else [n for n in names if k in n or n in k]
        hit.extend(m for m in matched if m not in hit)
    return hit


def resolve_priority_regions(region_map):
    """按 BUY_REGIONS 筛选（留空=全抢）并按优先级排序；筛空则退出，避免白等一场"""
    buy = [str(x).strip() for x in BUY_REGIONS if x and str(x).strip()]
    all_names = list(region_map.values())
    hit = _region_pick(all_names, buy) if buy else all_names

    picked, dropped = [], []
    for rid, name in region_map.items():
        if name in hit:
            picked.append((rid, name))
        else:
            dropped.append(name)
    if not picked:
        log.error(f"BUY_REGIONS 没有匹配到可抢地域。活动可选: {'、'.join(all_names)}")
        log.error(f"当前 BUY_REGIONS={buy}，请检查名字是否写错")
        exit(1)
    miss = [k for k in buy if not _region_pick(all_names, [k])]
    if miss:
        log.warning(f"BUY_REGIONS 中这些名字没匹配到地域: {'、'.join(miss)}（活动可选: {'、'.join(all_names)}）")

    order = buy or [str(x).strip() for x in PREFERRED_REGIONS if x and str(x).strip()]
    ranked = [(rid, name, order.index(name) if name in order else 999) for rid, name in picked]
    ranked.sort(key=lambda x: (x[2], x[0]))
    if dropped:
        seq = " > ".join(n for _, n, _ in ranked)
        log.info(f"地域限定 {len(ranked)} 个: {seq}（排除 {'、'.join(dropped)}）")
    return [r[0] for r in ranked], {r[0]: r[1] for r in ranked}

config = fetch_activity_config()
if not config:
    log.error("无法获取活动配置，退出"); exit(1)

slot_info, region_map = find_target_goods(config)
if not slot_info:
    log.error(f"未找到包含 '{TARGET_GOODS_KEYWORD}' 的秒杀商品"); exit(1)

SECKILL_TIME_STR, SECKILL_TIMESTAMP = resolve_seckill_time(slot_info)
if not SECKILL_TIME_STR:
    log.error("无法解析秒杀时间"); exit(1)

_sim_arg = next((a for a in sys.argv if a.startswith("--simulate")), None)
if _sim_arg:
    DRY_RUN = True
    try:
        _lead = float(_sim_arg.split("=", 1)[1]) if "=" in _sim_arg else float(sys.argv[sys.argv.index(_sim_arg) + 1])
    except Exception:
        _lead = 60.0
    SECKILL_TIMESTAMP = int(time.time() * 1000) + int(_lead * 1000)
    SECKILL_TIME_STR = datetime.fromtimestamp(SECKILL_TIMESTAMP / 1000).strftime("%m-%d %H:%M:%S")

PRIORITY_REGIONS, REGION_MAP = resolve_priority_regions(region_map)
ACTIVITY_ID = config["activity_id"]
ACT_ID = slot_info["act_id"]
BUSINESS_ID = slot_info["pb_id"]
GOODS_TYPE = slot_info["goods_type"]
DEFAULT_PARAMS = slot_info.get("default_params", {})

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


def planned_pre_fire():
    """波次计划发射数：地域被筛少后 target_regions 自动截断，统计要跟着截"""
    return sum(min(rc, len(PRIORITY_REGIONS)) * n for _, rc, n in PRE_FIRE_WAVES)

headers = {
    "x-csrf-token": csrf_token,
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    "Referer": ACTIVITY_URL,
    "Origin": "https://cloud.tencent.com",
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest"
}

_lock = threading.Lock()
# total 在"请求发出时"即 +1（含超时/异常），保证 total == 各分项之和
_stats = {"total": 0, "success": 0, "no_stock": 0, "rate_limited": 0,
          "fatal": 0, "other_error": 0, "no_info": 0, "timeout": 0, "exception": 0}
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
_recent_offsets = []
_async_loop = None
_firing_mode = False
_elig_status = "unknown"  # field_ok / ineligible / sold_out / not_started / unknown

# 站点只下发「要求什么」（2=硬性要求，1/0=基础或不需要），不下发「本账号是否满足」，故只能提前告知不能判定
USER_CONDITION_LABELS = {
    "qcloud_new_user_by_idcard": "实名主体须为云新客",
    "no_paid_deal": "实名主体须无付费订单",
    "real_name": "需完成实名认证",
    "activated": "账号需已激活",
    "registered": "需已注册",
    "local_user": "需为大陆用户",
    "company": "需企业认证",
    "cooperator": "需渠道合作伙伴",
}


def describe_user_condition(cond):
    """把 user_condition 拆成 (硬性门槛列表, 是否有新客类硬门槛)"""
    if not isinstance(cond, dict) or not cond:
        return [], False
    hard = [USER_CONDITION_LABELS.get(k, k) for k, v in cond.items()
            if v == 2 and k in USER_CONDITION_LABELS]
    newbie = any(k in cond and cond[k] == 2
                 for k in ("qcloud_new_user_by_idcard", "no_paid_deal"))
    return hard, newbie


def _init_async_loop():
    """初始化异步事件循环（在后台线程中运行）"""
    global _async_loop
    if not HTTPX_OK:
        _async_loop = None
        log.warning("未安装 httpx，已降级为 requests + 线程池（并发能力下降）")
        return None
    _async_loop = asyncio.new_event_loop()
    t = threading.Thread(target=_async_loop.run_forever, daemon=True)
    t.start()
    return _async_loop

async_client = httpx.AsyncClient(
    http2=True,
    limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
    timeout=httpx.Timeout(SECKILL_TIMEOUT, connect=2.0),
    headers=headers,
    cookies=COOKIE_JAR,
) if HTTPX_OK else None


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
    # 抢购阶段：零网络请求，直接用缓存偏移（秒级窗口内偏移变化可忽略）
    if _firing_mode:
        return now_local + cached_offset
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


def check_eligibility(verbose=True):
    """预检抢购资格（只读，不产生订单）

    公开接口只能做"字段级"校验，无法在开抢前判定"实名主体是否已产生付费"：
      check-available 的 user/idcard_available 只管限购维度（10-31 实测：字段=1 但下单仍 1100106），
      prepare-do 在售罄/未开始时会短路，do-goods preview=1 开场前也只返回"未开始"。
    所以权威结论只能由 T0 后首个 do-goods 响应给出，本函数仅做提前排除。
    返回: 'ok' | 'ineligible' | 'sold_out' | 'not_started' | 'unknown'
    """
    global _elig_status
    # 通道1：check-available（售后仍可用，给出账号/实名维度 + 地域库存）
    try:
        resp = session.post(
            "https://act-api.cloud.tencent.com/dianshi/check-available",
            json={"activity_id": ACTIVITY_ID, "goods": [{"act_id": ACT_ID}], "preview": 0},
            headers=headers, timeout=8)
        result = resp.json()
        code = result.get("code")
        if code == 0:
            goods = (result.get("data") or [{}])[0]
            user_av = goods.get("user_available", -1)
            idcard_av = goods.get("idcard_available", -1)
            quota = goods.get("quota", {})
            stock = " ".join(f"{REGION_MAP.get(int(rid), rid)}:{q.get(GOODS_TYPE, {}).get('available', 0)}"
                             for rid, q in quota.items())
            if user_av == 0 or idcard_av == 0:
                log.error(f"资格预检：账号或实名维度不满足 (user={user_av}, idcard={idcard_av})")
                _elig_status = "ineligible"
                return "ineligible"
            if user_av > 0 and idcard_av > 0:
                # 字段通过 ≠ 新客资格通过（重条件需 T0 才知道）
                if verbose:
                    log.info(f"✅ 资格 账号={user_av} 实名={idcard_av} · 库存 {stock} · ❓ 新客资格待T0响应")
                _elig_status = "field_ok"
                return "ok"
        elif code in (1101002, "NOT-LOGINED", -1):
            log.warning(f"资格预检：登录态异常 code={code}")
            _elig_status = "unknown"
            return "unknown"
        else:
            log.debug(f"check-available 返回 code={code} msg={result.get('msg', '')}")
    except Exception as e:
        log.debug(f"check-available 资格查询异常: {e}")

    # 通道2：prepare-do（有货时可直接暴露 1100106；售罄时被 1100136 短路）
    body = {
        "activity_id": ACTIVITY_ID,
        "act_id": ACT_ID,
        "preview": 0,
        "channel_tag": "",
        "from_channel": "",
        "agent_key": "",
    }
    try:
        resp = session.post("https://act-api.cloud.tencent.com/dianshi/prepare-do",
                            json=body, headers=headers, timeout=8)
        result = resp.json()
        code = result.get("code")
        if code == 0:
            if verbose:
                log.info("资格预检通过（prepare-do）")
            _elig_status = "field_ok"
            return "ok"
        if code in (1100136, 1100151):
            if verbose:
                log.info("资格预检：商品当前售罄（上一场次状态），资格将在 T0 首个响应确认")
            if _elig_status != "field_ok":
                _elig_status = "sold_out"
            return "sold_out"
        if code == 1100106:
            log.error(f"资格预检：不满足新用户条件 (1100106): {result.get('msg', '')}")
            _elig_status = "ineligible"
            return "ineligible"
        if code in (1101001, 1100201):
            if _elig_status == "unknown":
                _elig_status = "not_started"
            return "not_started"
        log.debug(f"资格预检返回 code={code} msg={result.get('msg', '')}")
        return "unknown"
    except Exception as e:
        log.debug(f"资格预检异常: {e}")
        return "unknown"

_pre_warm_ts = 0.0   # 上次预热时间：启动已预热过就不必在 T-30s 再重复一遍


def pre_warm():
    """预热连接池：同步预热act-api + 异步预热httpx（在_async_loop中）"""
    global _pre_warm_ts
    _pre_warm_ts = time.time()
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
    with _lock:
        _stats["total"] += 1   # 发出即计数，超时/异常也归属总数，避免分项之和 > 总数
    try:
        t0 = time.perf_counter()
        resp = await async_client.post(BUY_URL, content=body)
        latency_ms = (time.perf_counter() - t0) * 1000
        result = resp.json()
        code = result.get("code")
        msg = result.get("msg", "")
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
        elif code == 408:
            with _lock:
                _stats["rate_limited"] += 1
        elif code in (1101001, 1100205, 1100110, 1101408):
            # 未开始/系统繁忙/处理中：无明确结论，但必须归类，否则分项之和与总数不符
            with _lock:
                _stats["no_info"] += 1
        elif code == 1101411:
            with _lock:
                _stats["success"] += 1
            log.info(f"[{req_id}] 已有未支付订单，抢购成功！")
            _stop_event.set()
            return True
        elif code in FATAL_CODES:
            global _fatal_error
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


async def _async_fire_barrage(regions=None, prefix="b", workers=None):
    """异步并发购买，任一成功即返回"""
    if regions is None:
        regions = PRIORITY_REGIONS
    if workers is None:
        workers = WORKERS_PER_REGION
    tasks = []
    for rid in regions:
        for w in range(workers):
            tasks.append(asyncio.ensure_future(_async_buy(rid, f"{prefix}{w}")))
    # 不能用 asyncio.as_completed：3.12 里它产出的不是 Future（.result() 会 AttributeError）
    pending = set(tasks)
    deadline = time.monotonic() + SECKILL_TIMEOUT + 2
    try:
        while pending:
            remain = deadline - time.monotonic()
            if remain <= 0:
                break
            done, pending = await asyncio.wait(
                pending, timeout=remain, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                break
            for f in done:
                if f.cancelled():
                    continue
                try:
                    if f.result():
                        for p in pending:
                            p.cancel()
                        return True
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    log.debug(f"盲打任务异常: {e}")
    finally:
        for t in pending:
            t.cancel()
    return False


def fire_barrage(regions=None, prefix="b", workers=None):
    """同步包装：提交异步任务到后台loop，等待结果"""
    if _async_loop is None:
        return _sync_fire_barrage_fallback(regions, prefix, workers)
    try:
        afut = asyncio.run_coroutine_threadsafe(
            _async_fire_barrage(regions, prefix, workers), _async_loop
        )
        return afut.result(timeout=SECKILL_TIMEOUT + 5)
    except Exception as e:
        log.debug(f"异步盲打异常，降级同步: {e}")
        return _sync_fire_barrage_fallback(regions, prefix, workers)


def _sync_fire_barrage_fallback(regions=None, prefix="b", workers=None):
    """同步降级：异步不可用时用requests+线程"""
    if regions is None:
        regions = PRIORITY_REGIONS
    if workers is None:
        workers = WORKERS_PER_REGION
    total_workers = len(regions) * workers
    with ThreadPoolExecutor(max_workers=total_workers) as pool:
        futures = []
        for i, rid in enumerate(regions):
            for w in range(workers):
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
    if DRY_RUN:
        # 安全护栏：干跑/演习绝不真实下单（降级路径曾漏掉此判断）
        log.info(f"[干跑] 模拟下单: region={REGION_MAP.get(region_id, region_id)} attempt={attempt}")
        return False
    req_id = f"R{region_id}-{attempt}"
    body = PREBUILT_BODIES.get(region_id)
    if not body:
        return False
    with _lock:
        _stats["total"] += 1   # 与异步路径口径一致：发出即计数
    try:
        resp = session.post(BUY_URL, data=body, headers=headers, timeout=SECKILL_TIMEOUT)
        result = resp.json()
        code = result.get("code")
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
        elif code in (1101001, 1100205, 1100110, 1101408):
            with _lock:
                _stats["no_info"] += 1
        elif code in FATAL_CODES:
            global _fatal_error
            with _lock:
                _stats["fatal"] += 1
                _fatal_error = f"code={code} {FATAL_CODES[code]}"
            _stop_event.set()
            return True
        else:
            with _lock:
                _stats["other_error"] += 1
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
    """检测skey变化时重新计算csrf_token（兼容多种cookie域写法）"""
    global csrf_token, headers, skey_value
    new_skey = None
    for d in (".cloud.tencent.com", "cloud.tencent.com", ".tencent.com"):
        try:
            v = session.cookies.get("skey", domain=d)
        except Exception:
            v = None
        if v:
            new_skey = v
            break
    if not new_skey:
        for c in session.cookies:
            if c.name == "skey" and "tencent.com" in (c.domain or ""):
                new_skey = c.value
                break
    if new_skey and new_skey != skey_value:
        log.info("检测到skey变化，重新计算csrf_token")
        skey_value = new_skey
        csrf_token = compute_csrf_token(new_skey)
        headers["x-csrf-token"] = csrf_token
        if async_client is not None:
            async_client.headers["x-csrf-token"] = csrf_token


def get_stats():
    """返回统计摘要"""
    return dict(_stats)


def disp_width(s):
    """终端显示宽度：CJK/emoji占2格，组合符与变体选择符占0格"""
    w = 0
    for ch in s:
        if ch in ("\ufe0f", "\u200d", "\u200b"):
            continue
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def pad_disp(s, width):
    """按显示宽度右补空格，保证含emoji/CJK的行能对齐"""
    return s + " " * max(0, width - disp_width(s))


def fmt_countdown(seconds):
    """倒计时文本：≥1小时 H:MM:SS，≥10秒 MM:SS，最后10秒保留一位小数"""
    seconds = max(0.0, seconds)
    if seconds >= 3600:
        return f"{int(seconds // 3600)}:{int(seconds % 3600 // 60):02d}:{int(seconds % 60):02d}"
    if seconds >= 10:
        return f"{int(seconds // 60):02d}:{int(seconds % 60):02d}"
    return f"{seconds:.1f} 秒"


def show_countdown(remaining_sec, hint=""):
    """单行原地刷新倒计时。返回 False 表示非终端环境，调用方应降级为低频日志"""
    global _status_active
    if not _status_isatty:
        return False
    text = f"⏳ 距离开抢 {fmt_countdown(remaining_sec)}"
    if hint:
        text += f"  ●  {hint}"
    text += f"  ●  {SECKILL_TIME_STR[-8:]} 开抢"
    with _status_lock:
        print(f"\r  {text}{' ' * max(0, 66 - disp_width(text))}", end="", flush=True)
        _status_active = True
    return True


def clear_status():
    """擦掉原地刷新的状态行（退出前调用，避免留下半行残影）"""
    global _status_active
    with _status_lock:
        if _status_active:
            print("\r" + " " * 66 + "\r", end="", flush=True)
            _status_active = False


def fetch_slot_live_state(slot_info):
    """用轻量库存接口刷新本场次实时状态（约 2KB，替代抓 1.2MB 活动页）

    实测接口：POST /act/common/ajax/dianshi  action=fetchSeckillGoodsStateByDate
      data = {"activityId": <int>, "dateList": ["YYYY-MM-DD"]}   ← 驼峰参数，缺一则报错
    返回 flashResult[date][] 含 dynamic_status / progress（库存进度 0~100）等，与活动页同构。
    仅用于启动时确认场次仍有效、并展示库存；不参与抢购判定。
    """
    date = slot_info.get("date")
    if not date or not ACTIVITY_ID:
        return None
    try:
        resp = session.post(
            "https://cloud.tencent.com/act/common/ajax/dianshi",
            data={"action": "fetchSeckillGoodsStateByDate",
                  "data": json.dumps({"activityId": int(ACTIVITY_ID),
                                      "dateList": [date]})},
            headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
            timeout=8)
        result = resp.json()
        if result.get("code") != 0:
            log.debug(f"轻量库存接口返回 code={result.get('code')} msg={result.get('msg')}")
            return None
        slots = ((result.get("data") or {}).get("flashResult") or {}).get(date) or []
        for slot in slots:
            if slot.get("start_time") != slot_info.get("start_time"):
                continue
            for sale in slot.get("sales", []):
                if sale.get("act_id") != slot_info.get("act_id"):
                    continue
                return {
                    "dynamic_status": slot.get("dynamic_status"),
                    "dynamic_status_cn": slot.get("dynamic_status_cn", ""),
                    "progress": sale.get("progress", 0),
                }
        return None
    except Exception as e:
        log.debug(f"轻量库存接口异常: {e}")
        return None


def print_banner(slot_info):
    """启动横幅：统一「单码点emoji + 双字标签」保证列对齐"""
    mins = (SECKILL_TIMESTAMP - int(time.time() * 1000)) / 60000
    if mins <= 0:
        eta = "已到点或已过点"
    elif mins < 1:
        eta = f"约 {mins * 60:.0f} 秒后"
    else:
        eta = f"约 {mins:.0f} 分钟后"
    goods = slot_info.get("goods_name", "N/A").replace("$", " · ")
    descp = slot_info.get("descp", "N/A")
    price = slot_info.get("real_cost", 0) / 100
    status = slot_info.get("dynamic_status_cn", "")
    live = slot_info.get("live_state") or {}
    if live.get("dynamic_status_cn"):
        status = live["dynamic_status_cn"]
    progress = live.get("progress")
    slot_line = f"{SECKILL_TIME_STR}（{status}，{eta}）"
    if progress is not None:
        slot_line += f" · 本场已售 {progress}%"
    regions = " > ".join(REGION_MAP.get(r, str(r)) for r in PRIORITY_REGIONS)
    region_tag = f"仅此{len(PRIORITY_REGIONS)}个 · " if BUY_REGIONS else ""
    if PRE_FIRE_WAVES:
        waves = planned_pre_fire()
        strategy = f"T-{PRE_FIRE_WAVES[0][0]:.1f}s起{len(PRE_FIRE_WAVES)}波共{waves}发 → T0后每{BLIND_FIRE_INTERVAL}s全地域火力"
    else:
        strategy = f"T0后单发探测→全地域火力 · 408退避{int(RATE_LIMIT_BACKOFF)}→{int(RATE_LIMIT_BACKOFF_MAX)}s"

    # 门槛行只说「要求什么」，资格行说「确认到哪一步」，两者不重复
    hard_conds, needs_newbie = describe_user_condition(slot_info.get("user_condition"))
    if hard_conds:
        gate_text = " + ".join(hard_conds)
    else:
        gate_text = "无硬性门槛"

    newbie_hint = "（实名有历史付费订单即不符）" if needs_newbie else ""
    elig_text = {
        "field_ok": f"✅ 账号/实名通过 · ❓ 新用户资格待T0确认{newbie_hint}",
        "ineligible": "❌ 字段不满足条件",
        "sold_out": f"◻ 售罄窗口 · ✅ 账号/实名通过 · ❓ 资格待T0确认{newbie_hint}",
        "not_started": f"◻ 场次未开始 · ❓ 资格待T0确认{newbie_hint}",
        "unknown": "◻ 未获取到资格信息 · 以T0首个响应为准",
    }.get(_elig_status, "◻ 待确认")

    rows = [
        ("📦", "商品", goods),
        ("📋", "规格", descp),
        ("💰", "价格", f"¥{price:.0f}"),
        ("⏰", "场次", slot_line),
        ("🔐", "门槛", gate_text),
        ("🔍", "资格", elig_text),
        ("🌏", "地域", f"{regions}  ·  {region_tag}{WORKERS_PER_REGION}并发/地域"),
        ("🎯", "策略", strategy),
        ("📝", "日志", os.path.join("logs", os.path.basename(log_path))),
        ("🔑", "会话", f"CSRF {csrf_token} · " +
                          ("httpx/HTTP2" if HTTPX_OK else "requests/线程池（未装 httpx，已降级）")),
    ]
    label_w = max(disp_width(f"{icon} {label}") for icon, label, _ in rows) + 2

    print()
    print("=" * 60)
    _tag = "  [演习·不下单]" if globals().get("_sim_arg") else ("  [干跑模式]" if DRY_RUN else "")
    print(f"  🔥 腾讯云秒杀脚本 v7.9{_tag}")
    print("=" * 60)
    for icon, label, value in rows:
        print("  " + pad_disp(f"{icon} {label}", label_w) + value)
    print("=" * 60)
    print()


def print_report(run_start, run_end, stats):
    """打印美观、无重复的抢购结果报告"""
    elapsed = (run_end - run_start).total_seconds()
    total = max(stats["total"], 1)
    rate_limited = stats.get("rate_limited", 0)
    fatal_cnt = stats.get("fatal", 0)
    other_err = stats.get("other_error", 0)
    no_info = stats.get("no_info", 0)
    pct = lambda n: f"{n / total * 100:4.1f}%"

    if stats["success"] > 0:
        outcome = "🎉 抢购成功 · 请尽快前往控制台支付"
        advice = "订单已生成，未支付会被释放，立即去控制台完成支付"
    elif "1100106" in (_fatal_error or ""):
        outcome = "❌ 未抢到 · 账号不满足新用户条件"
        advice = "该实名主体已产生过付费订单，需更换符合条件的新账号"
    elif _fatal_error:
        outcome = f"❌ 未抢到 · {_fatal_error}"
        advice = "按致命错误提示处理后重试（登录态/活动状态/账号状态）"
    elif stats["no_stock"] >= total * 0.5:
        outcome = "💤 未抢到 · 商品已售罄"
        advice = "抢购窗口内库存已被抢完，可提前准备或更换地域/场次"
    elif rate_limited >= total * 0.8:
        outcome = "🚫 未抢到 · 全程被限频(408)"
        advice = "触发账号级限频，可降低并发或延长退避后重试"
    elif stats["timeout"] >= total * 0.5:
        outcome = "📡 未抢到 · 网络超时为主"
        advice = "检查网络质量，或适当放宽 SECKILL_TIMEOUT"
    else:
        outcome = "❌ 未抢到 · 未获得有效结果"
        advice = "查看日志中的逐条响应码定位原因"

    dur = f"{int(elapsed // 60)}分{int(elapsed % 60)}秒" if elapsed >= 60 else f"{elapsed:.0f}秒"
    label_w = 12
    rows = [
        ("🏁", "最终结果", outcome),
        ("🕐", "运行时长", f"{dur}  ({run_start.strftime('%H:%M:%S')} → {run_end.strftime('%H:%M:%S')})"),
    ]
    if _fatal_error:
        rows.append(("🚨", "致命原因", _fatal_error))
    rows.append(("💡", "建议", advice))
    rows.append(("📝", "日志", log_path))

    print()
    print("=" * 60)
    print("  📊 抢购结果")
    print("=" * 60)
    for icon, label, value in rows:
        print("  " + pad_disp(f"{icon} {label}", label_w) + value)
    print()
    print("  ── 请求统计 " + "─" * 34)
    print(f"  总请求 {stats['total']:>4}    限频 {pct(rate_limited)}    成功 {pct(stats['success'])}")
    tree = [
        ("├", "成功", stats["success"]),
        ("├", "限频(408)", rate_limited),
        ("├", "售罄", stats["no_stock"]),
        ("├", "其他业务响应", fatal_cnt + other_err + no_info),
        ("└", "超时/异常", stats["timeout"] + stats["exception"]),
    ]
    for branch, name, n in tree:
        print(f"   {branch} " + pad_disp(name, 16) + f"{n:>4}  {pct(n)}")
    print("=" * 60)

if __name__ == "__main__":
    def signal_handler(sig, frame):
        log.info("收到中断信号，停止抢购...")
        _stop_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    PRE_FIRE_EARLIEST = SECKILL_TIMESTAMP - int(PRE_FIRE_WAVES[0][0] * 1000) if PRE_FIRE_WAVES else SECKILL_TIMESTAMP
    FIRST_WAVE_LEAD = PRE_FIRE_WAVES[0][0] if PRE_FIRE_WAVES else 1.0
    run_start = datetime.now()

    if not verify_login():
        log.error("登录态无效，退出"); exit(1)

    _elig = check_eligibility()
    if _elig == "ineligible":
        print()
        print("  ❌ 当前账号不满足该商品的新用户条件，已提前退出")
        print("     （相同实名主体已产生过付费订单，请更换符合条件的新账号）")
        print()
        log.error("资格预检不通过，退出")
        exit(1)

    # 轻量接口刷新本场次实时状态（约 2KB）：确认场次未结束、顺带拿到库存进度
    live = fetch_slot_live_state(slot_info)
    if live:
        slot_info["live_state"] = live
        if live.get("dynamic_status") == 2:
            log.warning(f"该场次已结束（{live.get('dynamic_status_cn')}），请调整 SECKILL_SLOT 或等待下一场")

    print_banner(slot_info)

    _init_async_loop()
    pre_resolve_dns()
    pre_warm()

    log.info("等待秒杀...")
    phase = "waiting"
    blind_fire_count = 0
    sold_out_streak = 0
    fired_waves = set()
    backoff = RATE_LIMIT_BACKOFF
    adapt_workers = WORKERS_PER_REGION
    consecutive_408 = 0
    pre_fire_sent = 0   # pre-fire 实发数（不是计划数），停机/异常时会小于计划值
    _last_elig_check = time.time()
    _last_heartbeat = 0.0
    _last_progress = 0.0

    while not _stop_event.is_set():
        now = get_server_time()

        # 阶段3：秒杀进行中
        if now >= SECKILL_TIMESTAMP:
            if phase != "firing":
                phase = "firing"
                _firing_mode = True  # 抢购期间停止时间同步，全部用缓存偏移
                fire_start = time.time()
                log.info("=" * 40)
                log.info("  秒杀开始！")
                log.info("=" * 40)
                if PRE_FIRE_WAVES:
                    planned = planned_pre_fire()
                    extra = "" if pre_fire_sent >= planned else f"（计划{planned}发，{planned - pre_fire_sent}发因T0到达未发出）"
                    log.info(f"pre-fire已发出{pre_fire_sent}发{extra}，持续施压中")
                else:
                    log.info("T0已到，开始火力覆盖")

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
                    prev_no_stock = _stats["no_stock"]
                    prev_rate = _stats.get("rate_limited", 0)
                    prev_total = _stats["total"]
                    if blind_fire_count == 1 and PROBE_FIRST:
                        round_label = "探测"
                        # 探测轮：仅首选地域1请求，避免瞬间多连发直接触发账号级限频
                        if fire_barrage(PRIORITY_REGIONS[:1], workers=1):
                            break
                    else:
                        round_label = f"第{blind_fire_count}轮"
                        # 上一轮全408时限频中，本轮降为1并发/地域，避免继续加码惩罚窗口
                        if fire_barrage(PRIORITY_REGIONS, workers=adapt_workers):
                            break
                    if DRY_RUN:
                        log.info("[干跑模式] 验证完成，退出")
                        _stop_event.set()
                        break
                    if _stop_event.is_set():
                        # 已停机（成功/致命错误/中断）：不再输出"发0请求 → 全速"这类空转摘要
                        break
                    rate_this = _stats.get("rate_limited", 0) - prev_rate
                    total_this = _stats["total"] - prev_total
                    ns_this = _stats["no_stock"] - prev_no_stock
                    if total_this > 0 and rate_this >= total_this:
                        consecutive_408 += 1
                        sleep_time = backoff
                        backoff = min(backoff * 2, RATE_LIMIT_BACKOFF_MAX)
                        # 7/28 成功场靠密集施压穿透限频，故前两轮全 408 仍保持火力，连续 3 轮才降并发
                        if consecutive_408 >= 3:
                            adapt_workers = 1
                            pace = f"持续限频 退避{sleep_time:.1f}s · 并发1"
                        else:
                            pace = f"限频 保持火力 · 间隔{sleep_time:.1f}s"
                    else:
                        consecutive_408 = 0
                        sleep_time = BLIND_FIRE_INTERVAL
                        backoff = RATE_LIMIT_BACKOFF
                        adapt_workers = WORKERS_PER_REGION   # 限频解除：恢复火力
                        pace = "全速"
                    log.info(f"[{round_label}] 发{total_this}请求 → 408×{rate_this} 售罄×{ns_this} → {pace}")
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
                _firing_mode = True   # 进入预发射即停时间同步：1 次同步会白拖 ~200ms，波次整体后移
                log.info("进入提前发射阶段...")   # 控制台 handler 会先擦掉倒计时状态行
            for i, (seconds_before, region_count, num_per_region) in enumerate(PRE_FIRE_WAVES):
                # 每轮重新取时间：缓存偏移零网络，波次间隔最短 0.2s，用首轮 now 会误判为"还早"
                now = int(time.time() * 1000) + _server_time_offset
                wave_ts = SECKILL_TIMESTAMP - int(seconds_before * 1000)
                if now >= wave_ts and i not in fired_waves:
                    fired_waves.add(i)
                    remaining_sec = (SECKILL_TIMESTAMP - now) / 1000
                    target_regions = PRIORITY_REGIONS[:region_count]
                    total_req = len(target_regions) * num_per_region   # 地域被筛少时按实际数报，不按计划数
                    log.info(f"波次{i+1} T-{seconds_before}s {total_req}请求→{[REGION_MAP.get(r,r) for r in target_regions]} 距秒杀{remaining_sec:.1f}s")
                    for rid in target_regions:
                        for w in range(num_per_region):
                            if _stop_event.is_set():
                                break
                            if _async_loop is not None:
                                try:
                                    asyncio.run_coroutine_threadsafe(
                                        _async_buy(rid, f"w{i}-{rid}-{w}"), _async_loop
                                    )
                                    pre_fire_sent += 1
                                except Exception as e:
                                    log.debug(f"pre-fire 发射异常: {e}")
                            else:
                                # 无 httpx 时没有事件循环，扔给事件循环会静默丢包，改起线程跑同步请求
                                threading.Thread(
                                    target=_sync_buy_fallback,
                                    args=(rid, f"w{i}-{rid}-{w}"), daemon=True
                                ).start()
                                pre_fire_sent += 1
                            if num_per_region > 1 or region_count > 1:
                                time.sleep(0.03)
                        if _stop_event.is_set():
                            break

            # 步长自适应：越接近 T0 轮询越密，避免跨过 T-0.2s 那一波（波次间隔仅 0.2s）
            remain_to_t0 = (SECKILL_TIMESTAMP - (int(time.time() * 1000) + _server_time_offset)) / 1000
            time.sleep(0.01 if remain_to_t0 < 1.0 else 0.05)

        # 阶段1：等待秒杀（倒计时走单行状态行，不刷屏）
        else:
            remaining_sec = (SECKILL_TIMESTAMP - now) / 1000
            if remaining_sec > 10:
                # 心跳/保活含网络请求，固定 30 秒一次；与每秒的状态行刷新解耦
                if time.time() - _last_heartbeat >= 30:
                    _last_heartbeat = time.time()
                    keep_alive()
                    recompute_csrf()
                    if time.time() - _last_elig_check > ELIGIBILITY_RECHECK_INTERVAL:
                        _last_elig_check = time.time()
                        prev_elig = _elig_status
                        if check_eligibility(verbose=False) == "ineligible":
                            log.error("资格复检不通过（不满足新用户条件），提前退出")
                            _stop_event.set()
                            break
                        if _elig_status != prev_elig:
                            log.info(f"资格状态变化: {prev_elig} → {_elig_status}")
                hint = ""
                if remaining_sec <= 30:
                    if phase != "pre_warm":
                        phase = "pre_warm"
                        # 启动时已预热过就别重复（否则会连打两遍"预热连接池/完成"）
                        if time.time() - _pre_warm_ts > 60:
                            pre_warm()
                    hint = "连接池就绪"
                # 终端：单行原地刷新；重定向到文件：退化为每 5 分钟一行，避免塞满 \r 碎片
                if not show_countdown(remaining_sec, hint):
                    if time.time() - _last_progress >= 300:
                        _last_progress = time.time()
                        log.info(f"距离开抢 {fmt_countdown(remaining_sec)} · {SECKILL_TIME_STR[-8:]} 开抢")
                time.sleep(1 if _status_isatty else 3)
            else:
                # T-3s 起停一切网络同步：一次同步白拖 ~200ms，会把波次1 从 T-2.0s 推后到 T-1.8s
                if not _firing_mode and remaining_sec <= FIRST_WAVE_LEAD + 1.0:
                    _firing_mode = True
                if _firing_mode:
                    now = int(time.time() * 1000) + _server_time_offset
                    remaining_sec = (SECKILL_TIMESTAMP - now) / 1000
                recompute_csrf()
                if remaining_sec < FIRST_WAVE_LEAD + 0.3:
                    time.sleep(0.05)
                else:
                    show_countdown(remaining_sec, "就绪")
                    time.sleep(0.1 if remaining_sec < FIRST_WAVE_LEAD + 1.0 else 0.3)

    clear_status()
    try:
        if _async_loop and _async_loop.is_running():
            if async_client is not None:
                asyncio.run_coroutine_threadsafe(async_client.aclose(), _async_loop).result(timeout=5)
    except Exception:
        pass
    try:
        if _async_loop:
            _async_loop.call_soon_threadsafe(_async_loop.stop)
    except Exception:
        pass

    # 先停日志监听把队列落盘，再打报告，否则日志线程会插进报告中间行
    stop_logging()

    run_end = datetime.now()
    stats = get_stats()
    print_report(run_start, run_end, stats)

    if _timer_resolved:
        try:
            ctypes.windll.winmm.timeEndPeriod(1)
        except Exception:
            pass
