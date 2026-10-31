# 腾讯云秒杀抢购脚本 v7.9

自动化抢购腾讯云轻量应用服务器 38 元秒杀商品。httpx 异步 + HTTP/2 消除 GIL 争用，T-2.0s 起 10 波密集施压，T0 后全地域开火。

## 功能特点

- **httpx 异步 + HTTP/2**：秒杀阶段无 GIL 争用，比 requests+线程池快 5-20ms/请求；未装 httpx 自动降级
- **全自动参数获取**：从活动页 `__NEXT_DATA__` 提取全部参数，换活动只改关键词；入口失效自动切候选地址
- **x-csrf-token 自动计算**：从 skey Cookie 按 DJB Hash 计算，无需抓包
- **密集波次开火**：T-2.0s 起 10 波共 28 发提前施压，T0 后每 0.5 秒全地域开火；408 退避封顶 2 秒，密度优先
- **地域可筛选**：`BUY_REGIONS` 只抢指定地域（书写顺序即出手顺序），留空全抢；写错名字启动即报错
- **资格预检双通道**：`check-available` 的 `user_available`/`idcard_available` 字段 + `prepare-do` 错误码；不满足提前退出，等待期每 60 秒复检
- **门槛明示**：从 `$goods.user_condition` 解析商品硬性要求，启动横幅直接告知，不用等失败才知道
- **等待期零刷屏**：倒计时单行原地刷新（35 分钟只占 1 行），资格复检静默；重定向到文件时降级为每 5 分钟一行
- **波次时序精确**：T-3s 起停一切网络同步，改用缓存偏移，10 波全部准点（偏差 ≤20ms）
- **演习模式**：`--simulate[=N]` 完整走一遍真实时序，强制干跑绝不下单
- **零准备开销**：请求体预序列化、DNS 预解析、连接池预热，秒杀时直接发
- **时间同步**：HEAD 测纯网络 RTT + 最近 10 个样本取中位数（不取最低 RTT）
- **异步日志**：QueueHandler + QueueListener，退出前 flush，尾部日志不丢
- **Windows 定时器 1ms**：`timeBeginPeriod(1)` 把 `time.sleep()` 精度从 15.6ms 提到 1ms
- **错误码闭环**：致命错误（1100106/1101003/1101002）立即停；1101411 视为成功；连续 2 轮售罄退出

## 资格校验的能力边界（必读）

| 手段 | 售罄时可用 | 能否判定"未买过/新用户" | 说明 |
|------|-----------|--------------------------|------|
| `check-available` → `user_available`/`idcard_available` | ✅ | ❌ 仅限购维度 | 前端用它置灰按钮；实测字段=1 但下单仍 1100106 |
| `prepare-do` | ❌ 返回 1100136 售罄 | 有货时可 | 售罄/未开始会短路，拿不到结论 |
| `do-goods` preview=1 | ❌ 返回 1101001 未开始 | T0 后才可以 | 权威判定，但只能在开场后 |

**结论**：`1100106`（相同实名主体已产生付费）是**重型条件，只能由 T0 后首个 do-goods 响应给出**。已穷举站点接口确认无公开能力可提前判定：登录态与匿名态 `__NEXT_DATA__` 仅差 `isLogined`/`nowTime`；`prepare-do` 对 24 个商品全返回 `code=0`；`query-price` 价格层不反映资格；`check-act-available` 恒 400、`check-act-access` 恒 0，其余 `action` 全部 404。

脚本的做法是：启动时尽量排除（字段=0 直接退出），等待期每 60 秒复检，T0 后用密集火力最快拿真实响应，命中 `FATAL_CODES` 立刻停机。

站点唯一愿意给的是 `$goods.user_condition` —— **告诉你"要求什么"，绝不告诉你"你满不满足"**。值为 `2` 的是硬性门槛：

```json
// 轻量 4核4G3M
{"activated":1, "real_name":1, "registered":1, "local_user":1,
 "qcloud_new_user_by_idcard":2, "restricted_by":2}
```

脚本因此把这一层拆成两行各司其职：「🔐 门槛」只陈述商品要求（`实名主体须为云新客`）；「🔍 资格」报告本账号确认到哪一步（`✅ 账号/实名通过 · ❓ 新用户资格待T0确认`）。后半句判据只在商品确实带 `qcloud_new_user_by_idcard`/`no_paid_deal` 硬门槛时出现，换商品不会误报。

## 环境要求

- **Python 3.10+**（实测 3.12 / 3.13）。`requests`、`h2`、`playwright` 的新版本均已要求 3.10+，低于这个版本只能装到旧版本，不建议。
- 抢购主程序：Windows / macOS / Linux 均可。
- Cookie 获取的 **CDP 模式目前只自动探测 Windows 的 Chrome 路径**；macOS / Linux 需手动以调试模式启动 Chrome（脚本会打印命令）。

## 安装步骤

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

`requirements.txt` 只含抢购主程序的两个包（`requests`、`httpx[http2]`），Cookie 获取的依赖按下面选用的方式单独装：

```bash
# 抢购主程序（requirements.txt 已包含）
pip install requests              # 必需：时间同步、降级通道都用它
pip install "httpx[http2]"        # 强烈推荐：异步 + HTTP/2 通道

# 获取 Cookie（按使用的方式二选一）
pip install playwright            # --mode pw
pip install websocket-client      # --mode cdp（缺失时脚本会自动安装）
```

四个容易踩的坑：

- **`httpx` 必须带 `[http2]`**。代码里 `httpx.AsyncClient(http2=True)` 是写死的，只执行 `pip install httpx` 会在启动时就抛 `ImportError: Using http2=True, but the 'h2' package is not installed`。
- **`httpx` 不装也能跑**，会自动降级为 `requests` + 线程池（启动横幅会标注「未装 httpx，已降级」），只是单请求慢 5–20ms。
- **`playwright install chromium` 对本项目无效**。代码用 `channel="chrome"` 直接调本机 Google Chrome（`playwright install --dry-run chrome` 显示的安装位置是 `<system>`，即不下载、只认系统已有的 Chrome）。所以要装的是**系统 Chrome**，`playwright install chromium` 下的是 Chromium，`channel="chrome"` 不会用它。
- **Playwright 模式以 `headless=False` 启动**，必须有图形界面；纯命令行服务器上只能走 CDP 模式或手动导出 Cookie。

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

> **活动入口固定为 `https://cloud.tencent.com/act`，不用跟着活动改**。它只用于让 act 域下发 `qcact_fingerprint`（act-api 风控指纹）——实测任何 `/act/*` 路径（连已下线的 404 页面）都会下发该指纹，所以入口更替不影响取 Cookie；万一拿不到就打印告警，但 Cookie 照常保存。登录必需的 `skey` / `uin` / `tinyid` 来自登录页，与此无关。

## 使用方法

### 配置参数

编辑 `seckill.py` 顶部的用户配置区（可按需修改以下几项，正常情况下保持默认即可）：

```python
TARGET_GOODS_KEYWORD = "4核4G"          # 商品关键词，用于自动匹配
SECKILL_SLOT = "next"                   # "next"=最近一场, "10:00"=指定场次
BUY_REGIONS = []                        # 只抢这些地域，留空=全抢
```

`ACTIVITY_URLS` 已配置当前有效的双11活动入口，无需手动修改；如入口再次失效，在 `seckill.py` 顶部的 `ACTIVITY_URLS` 列表最前面加上新地址即可。

### 地域：只抢哪些

只有一个开关 `BUY_REGIONS`，**留空 = 全抢**（活动里出现的所有地域都打，默认行为）。不想抢哪个就不填哪个，不需要单独写「不抢」列表。

```python
BUY_REGIONS = []                     # 默认：全抢
BUY_REGIONS = ["广州"]               # 只抢广州，波次集中一处
BUY_REGIONS = ["上海", "广州"]       # 只抢这两个，且上海先出手（书写顺序即出手顺序）
```

填中文地域名（与活动页下拉框一致），不是 regionId 数字；写 `"广州"` 也能命中 `"广州三区"` 这类带后缀的地域。名字全写错会直接报错退出并列出活动可选地域，不会白等一场。

启动即打印实际生效范围，如 `地域限定 2 个: 上海 > 广州（排除 北京）`；横幅「地域」行同步显示 `上海 > 广州 · 仅此2个`。

> 筛选会同步缩短波次：地域数少于波次设定时，超出的部分自动截断，计划发数随之下降（3 地域 28 发 → 2 地域 21 发 → 1 地域 12 发），不会空转。

### 地域优先级

`BUY_REGIONS` 留空（全抢）时，由 `PREFERRED_REGIONS` 决定出手顺序，越靠前越先被波次选中：

```python
PREFERRED_REGIONS = ["广州", "上海", "北京"]   # 默认
PREFERRED_REGIONS = ["上海", "广州", "北京"]   # 上海优先
```

`BUY_REGIONS` 非空时以它的书写顺序为准，此列表退为兜底排序。

其余所有参数均从活动页面自动获取。

### 运行秒杀

```bash
python seckill.py
```

**干跑模式**（只验证配置不实际下单）：

```bash
python seckill.py --dry-run
```

**演习模式**（验证波次时序，强制干跑绝不下单；N=提前多少秒开抢，默认 60）：

```bash
python seckill.py --simulate=20
```

> `--simulate` 会完整走一遍「倒计时 → 10 波 pre-fire → T0 火力」的真实时序，并打印每波实际距秒杀时间，用来确认机器上跑出的偏差。**必须留出 ≥15 秒**，否则启动开销（抓活动页 + 连接池预热约 4 秒）会把提前量吃掉。

脚本启动流程：
1. 读取Cookie + 计算csrf_token + 初始化httpx AsyncClient
2. 获取活动页面配置 + 匹配目标商品 + 解析秒杀时间
3. 预构建各地域请求体（预序列化为bytes）
4. 验证登录态 + 资格预检 + 初始化异步事件循环 + DNS预解析 + 连接池预热
5. 等待秒杀（心跳保活 + 状态行倒计时 + 静默资格复检）
6. T-2.0s~T0 密集波次（10波28发）→ T0 后每 0.5s 全地域火力（短退避保密度）

### 建议运行时机

秒杀前 **10-30 分钟**运行 `get_cookies.py` 获取最新 Cookie，然后立即启动 `seckill.py`。

## 抢购策略

| 阶段 | 时间 | 策略 | 说明 |
|------|------|------|------|
| 等待期 | T-∞ ~ T-30s | 心跳保活 + 状态行倒计时 | 每30秒保活requests+httpx并检测skey变化；资格每60秒静默复检（仅状态变化时提示）；倒计时单行原地刷新 |
| 预热期 | T-30s ~ T-10s | 预热连接池 | 启动时已预热过则跳过，避免重复打日志；状态行显示「连接池就绪」 |
| 就绪期 | T-10s ~ T-2.0s | 停网络同步 | T-3s 起改用缓存偏移，避免 200ms 同步阻塞把波次推后 |
| 提前发射 | T-2.0s ~ T+0.0s | 10波共28发，异步发射即忘 | 首选地域→多地域递进，后期波次每地域2发；表面408不代表作废，请求可能被服务端排队 |
| 盲打期 | T+0.0s ~ T+10s | 每0.5s全地域6发 | 密度优先；408退避1→2s封顶；连续3轮全408才降并发为1；售罄过半2轮即退出 |
| 检测期 | T+10s+ | 库存检测定向攻击 | 每0.5秒查库存，有货地域才发请求 |

三条原则支撑上面的配置：

- **408 是常态，秒拒≠作废**：请求可能已被服务端排队，数秒后才返回业务码
- **密度决定命中**：10 秒窗口内塞进队列的请求越多命中率越高，长退避等于把窗口让给对手
- **是否真打进看业务码**：出现 1100110/1100205 说明已进业务处理层；全程只有 408 说明连队列都没进去

### 时间同步策略

| 距秒杀 | 同步间隔 | 每次样本数 | 说明 |
|--------|----------|-----------|------|
| >60s | 30秒 | 1 | 远距离，低频 |
| 30-60s | 5秒 | 1 | 中距离 |
| 10-30s | 5秒 | 3 | 近距离，3样本取中位数 |
| <10s | 2秒 | 1 | 倒计时，1样本避免消耗限频配额 |
| 抢购阶段(T0后) | 不同步 | - | 纯缓存偏移，零网络请求，把连接全留给 do-goods |

偏移量取最近10个样本的**中位数**（非最低RTT），更稳定。

## 配置参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `ACTIVITY_URLS` | double11-202611 | 活动页候选入口列表，按顺序尝试取第一个有效的；活动更替时在列表最前面加新地址 |
| `TARGET_GOODS_KEYWORD` | 4核4G | 商品关键词，匹配商品名或规格描述 |
| `PREFERRED_REGIONS` | 广州>上海>北京 | 地域出手顺序（不筛选，排序用） |
| `SECKILL_SLOT` | next | 场次选择，"next"=最近一场，"10:00"=指定10点场 |
| `BUY_REGIONS` | `[]` 全抢 | 只抢这些地域，留空=全抢（中文名，如 `["广州"]`；书写顺序即出手顺序） |
| `PRE_FIRE_WAVES` | 10波28发 | 提前发射波次（T-2.0s起），密度决定命中率，勿随意减半 |
| `PROBE_FIRST` | False | 密度模式下关闭；设True则改为保守的T0单发探测 |
| `BLIND_FIRE_INTERVAL` | 0.5 | T0后盲打轮间隔(秒)，越小越密 |
| `RATE_LIMIT_BACKOFF` | 1.0 | 408退避起点(秒) |
| `RATE_LIMIT_BACKOFF_MAX` | 2.0 | 408退避上限(秒)，抢购窗口只有10秒，不宜过长 |
| `WORKERS_PER_REGION` | 2 | 每地域并发数（3触发408，降为2） |
| `FIRE_DURATION` | 10 | 持续抢购秒数 |
| `CHECK_INTERVAL` | 0.5 | 库存检测间隔(秒) |
| `ELIGIBILITY_RECHECK_INTERVAL` | 60 | 等待期资格复检间隔(秒) |
| `MAX_BLIND_ROUNDS` | 20 | 盲打最大轮数 |
| `SOLD_OUT_EXIT_ROUNDS` | 2 | 连续N轮全部售罄则提前退出 |
| `SECKILL_TIMEOUT` | 3 | 秒杀期间请求超时(秒) |
| `--dry-run` | - | 干跑模式，只验证配置不实际下单 |
| `--simulate[=N]` | - | 演习模式，开抢时刻设为 N 秒后（默认60），强制干跑 |

## 核心机制

### httpx 异步 + HTTP/2

秒杀阶段使用 `httpx.AsyncClient(http2=True)` 替代 `requests.Session`：
- **无 GIL 争用**：asyncio 单线程并发，不需要线程锁
- **HTTP/2 多路复用**：同一 TCP 连接并发多个请求，减少连接建立开销
- **命中即收手**：`asyncio.wait(FIRST_COMPLETED)` 轮询，任一成功立即取消剩余请求
- 降级路径：httpx 不可用时自动降级为 requests + ThreadPoolExecutor

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
    hash = ((hash << 5) + hash) + ord(ch)
x-csrf-token = str(hash & 0x7FFFFFFF)
```

### 服务器时间同步

HEAD 请求测纯网络 RTT（不含服务端处理时间）；Date 头只有秒级精度，offset 减 500ms 保守偏早；偏移取最近 10 个样本的中位数；东八区显式指定，不受系统时区影响。

### DNS 预解析

monkey-patch `socket.getaddrinfo`，首次解析后永久缓存，秒杀时零 DNS 查询。

### Windows 定时器精度

`timeBeginPeriod(1)` 把 sleep 精度从 15.6ms 提到 1ms，退出时 `timeEndPeriod(1)` 恢复。

## 文件结构

```
TencentCloud-Seckill/
├── seckill.py             # 秒杀主程序
├── get_cookies.py         # Cookie 获取脚本（Playwright / Chrome CDP）
├── requirements.txt       # 依赖清单（pip install -r requirements.txt）
├── .gitattributes         # 统一行尾为 LF，避免 Windows 反复提示 CRLF
├── cookies.json           # 登录 Cookie 存储（已 gitignore，不入库）
├── logs/                  # 日志文件目录（已 gitignore）
│   └── seckill_*.log      # 每次运行的详细日志（毫秒时间戳，异步写入）
├── chrome_profile/        # Chrome调试实例的用户数据（已 gitignore）
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

**Q: 运行后一直 408 限频，正常吗？**
A: 正常。开抢窗口内 408 是常态，不代表请求作废——它可能已被服务端排队。脚本会保持密度优先，退避封顶 2 秒，连续 3 轮全 408 才降并发。

**Q: 提前几小时运行可以吗？**
A: 可以，脚本会每 30 秒心跳保活。但建议秒杀前 10-30 分钟重新获取 Cookie。

**Q: 服务器时间 offset 不稳定？**
A: HTTP Date 头只有秒级精度，脚本取最近 10 个样本的中位数，再用 10 波提前发射覆盖剩余不确定性。

**Q: get_cookies.py Chrome CDP 模式报 WebSocket 403 错误？**
A: 先关闭所有 Chrome 窗口后重试。

## 版本历史

| 版本 | 变更 |
|---|---|
| v7.1 | 活动入口改为多候选自动切换（旧 `featured-YYYYMM` 下线导致「未找到 activityData」）；修 `_fatal_error` 变量遮蔽、`recompute_csrf` 多域兼容、`get_cookies.py` 死链与 CDP 消息竞态 |
| v7.2 | 曾取消 T0 前提前发射，改单发探测 + 指数退避（后被真实成功日志推翻） |
| v7.3 | 控制台输出分层（INFO 进控制台 / DEBUG 只进文件）、轮次单行摘要、结果报告重设计、启动横幅分组、按东亚字符宽度对齐 |
| v7.4 | 恢复并加大 T0 前波次密度（T-2.0s~T-0.2s 共 10 波 28 发）；退避上限压到 2s；连续 3 轮全 408 才降并发；新增资格预检双通道 |
| v7.5 | 穷举站点接口确认无「提前判定新用户资格」的公开能力；横幅明示 `$goods.user_condition`；新增轻量库存接口（2KB 替代 1.2MB 活动页）；httpx 改可选依赖 |
| v7.6 | 修复 T0 时序与统计缺陷：`as_completed` 在 3.12 返回 coroutine 致返回值恒 False、pre-fire 期网络同步使波次后移 200ms 并跳过末波、`total` 不含超时致分项之和大于总数；新增 `--simulate` 演习模式 |
| v7.7 | 等待期倒计时改单行原地刷新（33 分钟从 130+ 行降到 1 行），`_ConsoleHandler` 统一擦除状态行杜绝粘连；资格复检静默化；预热去重 |
| v7.8 | 横幅「资格」行改为状态式表达（✅ 已确认 / ❓ 待确认）并附判据提示；「门槛」行精简为只陈述条件，消除重复 |
| v7.9 | 新增 `BUY_REGIONS` 地域限定（留空=全抢，书写顺序即出手顺序）。此前 `PREFERRED_REGIONS` 只排序不过滤，设单地域仍会打全部地域；名字写错启动即报错并列出可选地域；波次计划数与日志请求数改按筛选后地域数计算 |
| v7.9.1 | 仅文档与依赖清单，脚本无改动：修正安装说明（Python 3.10+、`httpx[http2]` 必须带 extra、`playwright install chromium` 无效应装 Chrome、CDP 模式仅自动探测 Windows）；新增 `requirements.txt` 与 `.gitattributes`（统一 LF） |
| v7.9.2 | `get_cookies.py` 活动入口固定为聚合页 `https://cloud.tencent.com/act`，不再要求与 `seckill.py` 的入口手动同步（该页只用于触发 `qcact_fingerprint`，且实测任何 `/act/*` 路径都会下发）；补指纹缺失告警；修正原先把超时误报为「页面跳转被重定向」的提示 |

## 免责声明

本项目仅供学习交流使用，请勿用于商业用途。使用本工具产生的任何后果由使用者自行承担。
