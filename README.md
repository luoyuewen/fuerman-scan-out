# 富尔曼仓库 · 扫码出库（手机网页）

手机浏览器扫 SKU 二维码 → 看库存 → 出库 1 或手输数量 → 写入飞书「出库记录」，主表「当前库存」公式自动更新。

不依赖微信小程序。v1 用手机 Safari / Chrome 打开即可。

## 文件

| 路径 | 说明 |
|------|------|
| `index.html` | 手机端页面（相机扫码 + 库存卡 + 出库） |
| `server.py` | FastAPI 后端（演示模式 / 飞书 Open API） |
| `requirements.txt` | Python 依赖 |
| `.env.example` | 环境变量模板 |
| `.venv/` | 本地虚拟环境（已装好依赖） |

二维码样例图与演示数据：`/workspace/qr/*.png`、`/workspace/qr/sample.csv`。

## 快速演示（不连飞书）

```bash
cd /workspace/scan-out
export DEMO=1
.venv/bin/python server.py
```

浏览器打开：http://127.0.0.1:8765/

- 点演示区的 SKU 芯片即可查询（无需相机）。
- 「出库 1」/「按数量出库」会扣减内存库存，并生成出库单号 `CK-YYYYMMDD-HHMMSS-xxxx`。
- `POST /api/demo/reset` 可恢复 sample.csv 初始库存。

本机自测：

```bash
curl -s http://127.0.0.1:8765/api/health
curl -s http://127.0.0.1:8765/api/sku/FM-NYL-20-BK
curl -s -X POST http://127.0.0.1:8765/api/outbound \
  -H 'Content-Type: application/json' \
  -d '{"sku":"FM-NYL-20-BK","qty":2,"note":"演示出库"}'
```

## 正式接飞书

1. 打开 [飞书开放平台](https://open.feishu.cn/app) → 创建企业自建应用。
2. 开通权限（至少）：
   - `bitable:app`（查看、评论、编辑和管理多维表格）  
     或细粒度：`base:record:retrieve`、`base:record:create`、`base:record:update`（以控制台实际名称为准）
   - 若用旧版权限名：`bitable:app` / 记录读写相关 scope
3. 发布应用版本，并把应用添加为该多维表格的协作者（可编辑），否则 API 读不到表。
4. 在多维表格 URL 里确认：
   - `app_token` = `JWzPburUbalqVXs4ugkclgzhnce`（`/base/` 后一段）
   - 打开「商品SKU库存」「出库记录」两张表，从浏览器地址或开发者工具拿到 `table_id`（`tbl...`）
5. 配置环境变量后启动：

```bash
cd /workspace/scan-out
export DEMO=0
export FEISHU_APP_ID=cli_xxx
export FEISHU_APP_SECRET=xxx
export FEISHU_APP_TOKEN=JWzPburUbalqVXs4ugkclgzhnce
export FEISHU_TABLE_SKU=tblxxxxxxxx   # 商品SKU库存
export FEISHU_TABLE_OUT=tblxxxxxxxx   # 出库记录
.venv/bin/python server.py
```

### 出库写入字段约定

写入「出库记录」时字段为：

| 字段 | 内容 |
|------|------|
| 出库单号 | `CK-20261022-163045-A1B2` 形式 |
| 出库时间 | 毫秒时间戳（亚洲/上海） |
| 出库时刻 | `YYYY/MM/DD HH:MM:SS`，精确到秒 |
| SKU | 关联到「商品SKU库存」对应行的 `record_id` |
| 数量 | 正整数 |
| 备注 | 可选 |

主表「入库数量 / 出库数量 / 当前库存」需已配置为关联汇总公式，写入出库记录后才会自动变。

## API

- `GET /api/health` — 健康检查、是否演示模式
- `GET /api/sku/{code}` — 按 SKU 编码查询；正式模式先精确匹配「SKU编码」，没有再精确匹配「条码」
- `POST /api/outbound` — body: `{"sku":"FM-NYL-20-BK","qty":10,"note":"..."}`  
  创建出库记录并返回更新后的库存卡
- `GET /api/demo/skus` / `POST /api/demo/reset` — 仅演示模式

## 手机怎么访问

服务默认绑定 `0.0.0.0:8765`（仅本机/内网测试，**不要**为此付费买公网服务器）。

手机扫码需要：

1. **同一局域网**：手机浏览器打开 `http://<电脑局域网IP>:8765/`  
   - iOS 相机扫码通常要求 **HTTPS**；若相机打不开，用页面上的「手动输入」或演示芯片。
2. **外网临时调试**：可用 Cloudflare Tunnel / ngrok 等把 `8765` 暴露成 HTTPS（自行配置，本项目不内置付费隧道）。
3. **长期使用**：把本目录部署到你已有的 HTTPS 主机（飞书云文档同租户机器、自有 VPS 等），并保管好 `APP_SECRET`。

## 二维码内容

打印标签上的二维码内容 = **SKU 编码本身**（例如 `FM-NYL-20-BK`），下面再印同一编号方便肉眼分辨。扫码后本页按编码查飞书记录。货架条码枪扫到的内容若是表里的「条码」，正式模式同样能查到对应 SKU。含英文字母的编码会按现有规则转成大写后再查。

## 规模提示（约 2 万 SKU）

- 本页按「扫一个查一个」工作，不一次拉全表，适合大批量 SKU。
- 飞书专业版单表约 2 万行顶格；二维码图片不必全部上传为附件（附件上限也约 2 万），打印标签 + 表内存编码即可。

## 故障排查

| 现象 | 处理 |
|------|------|
| 未找到 SKU | 确认编码与表里「SKU编码」或「条码」完全一致；正式模式确认 `FEISHU_TABLE_SKU` |
| 写出库失败 | 应用是否已加入该 Base 协作者；出库表字段名是否为「出库单号/出库时间/出库时刻/SKU/数量/备注」；SKU 是否为关联字段 |
| 库存不减 | 检查主表「出库数量」「当前库存」公式是否指向「出库记录」 |
| 相机黑屏 | 用 HTTPS、允许摄像头权限，或改用手动输入 |


## 飞书工作台 H5（扫一扫）

手机浏览器相机经常打不开；正式用法是把本服务部署到 HTTPS 固定域名，再挂到飞书自建应用「网页应用」。

### 部署环境变量（Render / 任意 PaaS）

| 变量 | 说明 |
|------|------|
| `DEMO` | 必须 `0` |
| `FEISHU_APP_ID` | 如 `cli_aa5377c7d2789c0f` |
| `FEISHU_APP_SECRET` | 应用密钥（只放托管面板，勿进 git） |
| `FEISHU_APP_TOKEN` | 多维表格 token |
| `FEISHU_TABLE_SKU` | 商品SKU库存表 ID |
| `FEISHU_TABLE_OUT` | 出库记录表 ID |
| `PORT` | 平台注入即可 |

启动：`uvicorn server:app --host 0.0.0.0 --port $PORT`（见 `Procfile` / `render.yaml`）。

### 飞书控制台还要做

1. **应用能力** → 开启「网页应用」，桌面端/移动端主页填 `https://你的域名/`。
2. **安全设置** → **H5 可信域名** 填你的域名（不含路径），否则 `h5sdk.config` 报 333447/333448。
3. **权限**：已有 bitable 读写；网页应用本身不需额外扫码 scope，但 `jsApiList` 需含 `scanCode`。
4. **版本管理与发布**：可用性包含仓库人员；发布后在工作台打开「grok仓库管理」。
5. 多维表格协作者里已添加该应用且「可编辑」。

### JSAPI 签名

前端请求 `GET /api/feishu/jsapi-config?url=` + `encodeURIComponent(location.href.split('#')[0])`。

服务端：`tenant_access_token` → `POST /open-apis/jssdk/ticket/get` 拿 `jsapi_ticket`（缓存）→  
`sha1("jsapi_ticket={t}&noncestr={n}&timestamp={ts}&url={url}")` → 返回 `{appId,timestamp,nonceStr,signature}`。

飞书内点「扫码出库」→ `tt.scanCode`；非飞书环境仍可用相机或手输 SKU。
