# 腾讯云秒杀抢购脚本 v7.0

自动化抢购腾讯云轻量应用服务器38元秒杀商品，httpx异步+HTTP/2消除GIL争用，7波提前发射覆盖时间偏移不确定性。

## 功能特点

- **httpx 异步 + HTTP/2**：秒杀阶段用 httpx AsyncClient 并发请求，无GIL争用，比 requests+线程快5-20ms/请求
- **全自动参数获取**：从活动页面 `__NEXT_DATA__` 自动提取所有参数，换活动只改URL和关键词
- **x-csrf-token 自动计算**：从 skey Cookie 通过 DJB Hash 算法计算，无需手动抓包
- **7波提前发射**：T-2.5s~T-0.1s 每500ms一波，共14请求，覆盖时间偏移±1.5s+网络延迟不确定性
- **预构建请求体**：启动时预序列化为bytes，秒杀时零序列化开销
- **中位数时间同步**：最近10个偏移样本取中位数，比最低RTT更稳定
- **DNS预解析+缓存**：monkey-patch socket.getaddrinfo，秒杀时零DNS查询
- **异步日志**：QueueHandler+QueueListener，消除文件I/O锁竞争
- **Windows定时器1ms**：timeBeginPeriod(1)使time.sleep()精度从15.6ms提升到1ms
- **致命错误即时停止**：1100106/1101003/1101002等不可重试错误立即停止
- **1101411视为成功**：已有未支付订单=抢购成功，立即停止
- **售罄提前退出**：连续2轮售罄过半则停止
- **限频智能退避**：408过半数时自动延长间隔至2秒
- **登录态预检**：启动时验证Cookie和csrf_token，支持数字和字符串类型错误码
- **心跳保活**：等待期间定期保活requests+httpx双连接，检测skey变化重新计算csrf_token
- **干跑模式**：`--dry-run`只验证配置不实际下单

## 环境要求

- Python 3.8+
- Windows / macOS / Linux

## 安装步骤

### 1. 安装依赖

```bash
pip install playwright requests httpx[http2] websocket-client
playwright install chromium
```

### 2. 获取登录 Cookie

```bash
python get_cookies.py
```

运行后交互式选择获取方式：

- **Playwright**：需安装 playwright 库，通过自动化框架控制浏览器
- **Chrome CDP**：需本机 Chrome，通过调试协议直连浏览器

也可直接指定方式：

```bash
python get_cookies.py --mode pw    # Playwright
python get_cookies.py --mode cdp   # Chrome CDP
```

扫码登录腾讯云后按回车，Cookie 自动保存到 `cookies.json`。

## 使用方法

### 配置参数

编辑 `seckill.py` 顶部的用户配置区（可按需修改以下3项，正常情况下保持默认即可）：

```python
ACTIVITY_URL = f"https://cloud.tencent.com/act/pro/featured-{__import__('time').strftime('%Y%m')}"  # 活动页地址（自动取当月）
TARGET_GOODS_KEYWORD = "4核4G"          # 商品关键词，用于自动匹配
SECKILL_SLOT = "next"                   # "next"=最近一场, "10:00"=指定场次
```

### 地域优先级配置

`seckill.py` 中的 `PREFERRED_REGIONS` 控制抢购地域的优先顺序，**越靠前优先级越高**，提前发射和盲打均优先使用排在前面的地域。

```python
PREFERRED_REGIONS = ["广州", "上海", "北京"]  # 默认：3地域
```

可用地域仅为：广州、上海、北京。常见修改示例：

```python
PREFERRED_REGIONS = ["广州"]                    # 只抢1个地域
PREFERRED_REGIONS = ["上海", "广州", "北京"]     # 上海优先
PREFERRED_REGIONS = ["北京", "广州"]             # 2地域，北京优先
```

其余所有参数均从活动页面自动获取。

### 运行秒杀

```bash
python seckill.py
```

**干跑模式**（只验证配置不实际下单）：

```bash
python seckill.py --dry-run
```

脚本启动流程：
1. 读取Cookie + 计算csrf_token + 初始化httpx AsyncClient
2. 获取活动页面配置 + 匹配目标商品 + 解析秒杀时间
3. 预构建各地域请求体（预序列化为bytes）
4. 验证登录态 + 初始化异步事件循环 + DNS预解析 + 连接池预热
5. 等待秒杀（心跳保活 + 渐进式时间同步）
6. 7波提前发射 + 秒杀开始后并发抢购

### 建议运行时机

秒杀前 **10-30 分钟**运行 `get_cookies.py` 获取最新 Cookie，然后立即启动 `seckill.py`。

## 抢购策略

| 阶段 | 时间 | 策略 | 说明 |
|------|------|------|------|
| 等待期 | T-∞ ~ T-60s | 心跳保活 | 每30秒保活requests+httpx，检测skey变化 |
| 加速期 | T-60s ~ T-30s | 加速保活+同步 | 每5秒保活+时间同步 |
| 预热期 | T-30s ~ T-10s | 预热+保活 | 预热连接池(仅一次)，每3秒保活+3样本时间同步 |
| 倒计时 | T-10s ~ T-2.5s | 高频检测+同步 | 每0.05-0.3秒检测，每2秒1样本时间同步 |
| 提前发射 | T-2.5s ~ T+0.0s | 7波14请求，异步发射即忘 | 纯缓存偏移，零网络请求，每请求后检查成功 |
| 盲打期 | T+1.0s ~ T+10s | 全地域盲打，as_completed流水线 | 等1秒让408排队消化，2并发/地域，成功即返回+取消剩余 |
| 检测期 | T+10s+ | 库存检测定向攻击 | 每0.5秒查库存，有货地域才发请求 |

### 7波提前发射详解

| 波次 | 发射时间 | 地域覆盖 | 请求数 | 目的 |
|------|----------|----------|--------|------|
| 1 | T-2.5s | 广州 | 1 | 时间偏早+网络延迟高时兜底 |
| 2 | T-2.0s | 广州 | 1 | 同上 |
| 3 | T-1.5s | 广州+上海 | 2 | 扩大覆盖 |
| 4 | T-1.0s | 广州+上海 | 2 | 关键波次，延迟1000ms时≈T+0.0s |
| 5 | T-0.5s | 全地域 | 3 | 延迟500ms时≈T+0.0s |
| 6 | T-0.3s | 全地域 | 3 | 兜底 |
| 7 | T-0.1s | 全地域 | 3 | 最后兜底 |

- 每波最多1请求/地域，错峰50ms，避免408限频
- 使用 `asyncio.run_coroutine_threadsafe` 异步发射，不阻塞主线程
- 每个请求后检查 `_stop_event`，成功即停止后续发射
- 此阶段用纯缓存偏移计算时间，零HEAD请求

### 时间同步策略

| 距秒杀 | 同步间隔 | 每次样本数 | 说明 |
|--------|----------|-----------|------|
| >60s | 30秒 | 1 | 远距离，低频 |
| 30-60s | 5秒 | 1 | 中距离 |
| 10-30s | 5秒 | 3* | 近距离，3样本取中位数 |
| <10s | 2秒 | 1 | 倒计时，1样本避免消耗限频配额 |
| 提前发射期间 | 不同步 | - | 纯缓存偏移，零网络请求 |

偏移量取最近10个样本的**中位数**（非最低RTT），更稳定。

## 配置参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `ACTIVITY_URL` | featured-当月 | 活动页地址，年月自动取当前月份 |
| `TARGET_GOODS_KEYWORD` | 4核4G | 商品关键词，匹配商品名或规格描述 |
| `PREFERRED_REGIONS` | 广州>上海>北京 | 地域优先顺序 |
| `SECKILL_SLOT` | next | 场次选择，"next"=最近一场，"10:00"=指定10点场 |
| `PRE_FIRE_WAVES` | 7波14请求 | 提前发射波次配置 |
| `WORKERS_PER_REGION` | 2 | 每地域并发数（3触发408，降为2） |
| `FIRE_DURATION` | 10 | 持续抢购秒数 |
| `CHECK_INTERVAL` | 0.5 | 库存检测间隔(秒) |
| `BLIND_FIRE_INTERVAL` | 0.5 | 盲打轮间隔(秒) |
| `MAX_BLIND_ROUNDS` | 10 | 盲打最大轮数 |
| `SOLD_OUT_EXIT_ROUNDS` | 2 | 连续N轮全部售罄则提前退出 |
| `RATE_LIMIT_BACKOFF` | 2.0 | 触发408限频后的退避秒数 |
| `SECKILL_TIMEOUT` | 3 | 秒杀期间请求超时(秒) |
| `--dry-run` | - | 干跑模式，只验证配置不实际下单 |

## 核心机制

### httpx 异步 + HTTP/2

秒杀阶段使用 `httpx.AsyncClient(http2=True)` 替代 `requests.Session`：
- **无GIL争用**：asyncio单线程并发，不需要线程锁
- **HTTP/2多路复用**：同一TCP连接并发多个请求，减少连接建立开销
- **as_completed流水线**：盲打时任一成功立即返回+取消剩余请求
- 降级路径：httpx不可用时自动降级为 requests + ThreadPoolExecutor

### 预构建请求体

```python
PREBUILT_BODIES[region_id] = json.dumps(request_data).encode("utf-8")
```

秒杀时直接 `async_client.post(url, content=PREBUILT_BODIES[rid])`，零序列化开销。

### 登录态预检

启动时调用 `check-available` API 验证：
- code=0：通过
- code=1101003 / CSRF-ERROR：csrf_token无效
- code=1101002 / NOT-LOGINED / UNAUTHORIZED：登录态失效
- 网络异常：不阻断运行

### 错误码处理

**致命错误（立即停止）**：

| code | 含义 |
|------|------|
| 1100106 | 不满足新用户条件 |
| 1101003 | csrf_token无效 |
| 1101002 | 未登录 |
| 1100201 | 活动未开始或已结束 |
| 1100301 | 账号异常 |

**成功类（立即停止）**：

| code | 含义 |
|------|------|
| 0 | 抢购成功 |
| 1101411 | 已有未支付订单（=抢购成功） |

**可重试**：1100205（系统繁忙）、1100110（正在执行其他操作）、1101408
**售罄**：1100151、1100136
**限频**：408

### x-csrf-token 计算

从 skey Cookie 通过 DJB Hash 算法计算：

```
hash = 5381
for ch in skey:
    hash = (hash << 5 + hash) + ord(ch)
x-csrf-token = str(hash & 0x7FFFFFFF)
```

### 服务器时间同步

- HEAD 请求测纯网络RTT（不含服务端处理时间）
- Date 头秒级精度，offset 减500ms保守偏早
- 中位数偏移：最近10个样本取中位数，比最低RTT更稳定
- 东八区显式指定，不受系统时区影响

### DNS预解析

monkey-patch `socket.getaddrinfo`，首次解析后永久缓存。秒杀时零DNS查询。

### Windows定时器精度

`timeBeginPeriod(1)` 将 sleep 精度从15.6ms提升到1ms，退出时 `timeEndPeriod(1)` 恢复。

## 文件结构

```
TencentCloud-Seckill/
├── seckill.py             # 秒杀主程序 v7.0
├── get_cookies.py         # Cookie 获取脚本（Playwright / Chrome CDP）
├── cookies.json           # 登录 Cookie 存储
├── logs/                  # 日志文件目录
│   └── seckill_*.log      # 每次运行的详细日志（毫秒时间戳，异步写入）
├── chrome_profile/        # Chrome调试实例的用户数据
└── README.md
```

## 常见问题

**Q: 启动时提示"未检测到有效登录态"**
A: 重新运行 `get_cookies.py` 登录后重试。

**Q: 提示"csrf_token无效"或"登录态已失效"**
A: Cookie 已过期，重新运行 `get_cookies.py`。也可先 `--dry-run` 快速验证。

**Q: 怎么确认秒杀前环境没问题？**
A: 运行 `python seckill.py --dry-run`，确认登录态验证通过、商品匹配正确即可。

**Q: 提示"不满足新用户条件"（code=1100106）**
A: 该账号已购买过同类型商品，需使用符合条件的账号。

**Q: 提示"秒杀尚未开始"（code=1101001）**
A: 正常现象，提前发射的请求会被服务端拒绝。

**Q: 提示"已有未支付订单"（code=1101411）**
A: 抢购成功！请前往控制台订单管理页支付。

**Q: 运行后一直408限频**
A: 并发已降至2/地域+错峰50ms，正常不会408。如仍限频，脚本自动退避2秒。

**Q: 提前几小时运行可以吗？**
A: 可以，脚本会每30秒心跳保活。但建议秒杀前10-30分钟重新获取Cookie。

**Q: 服务器时间offset不稳定？**
A: HTTP Date头只有秒级精度，脚本用中位数偏移+7波提前发射覆盖不确定性。

**Q: get_cookies.py Chrome CDP 模式报 WebSocket 403 错误？**
A: 先关闭所有 Chrome 窗口后重试。

## 免责声明

本项目仅供学习交流使用，请勿用于商业用途。使用本工具产生的任何后果由使用者自行承担。
