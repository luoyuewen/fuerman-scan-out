#!/usr/bin/env python3
"""富尔曼仓库扫码出库 — FastAPI 后端。

DEMO=1 时用 /workspace/qr/sample.csv 内存演示，不连飞书。
正式模式通过飞书开放平台读写「商品SKU库存」「出库记录」。
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import os
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEMO = os.getenv("DEMO", "1").strip() in ("1", "true", "True", "yes", "YES")
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "").strip()
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "").strip()
FEISHU_APP_TOKEN = os.getenv(
    "FEISHU_APP_TOKEN", "JWzPburUbalqVXs4ugkclgzhnce"
).strip()
FEISHU_TABLE_SKU = os.getenv("FEISHU_TABLE_SKU", "").strip()
FEISHU_TABLE_OUT = os.getenv("FEISHU_TABLE_OUT", "").strip()
SAMPLE_CSV = Path(os.getenv("SAMPLE_CSV", "/workspace/qr/sample.csv"))
TZ_SH = timezone(timedelta(hours=8))

ROOT = Path(__file__).resolve().parent

# One pooled client so scan → Feishu reuses TLS instead of handshaking per call.
_http: Optional[httpx.AsyncClient] = None


def _aclient() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(
            timeout=httpx.Timeout(12.0, connect=4.0),
            limits=httpx.Limits(
                max_keepalive_connections=8,
                max_connections=16,
                keepalive_expiry=120.0,
            ),
        )
    return _http


async def _warm_feishu() -> None:
    """Open the Feishu connection and cache the tenant token before the first scan."""
    try:
        await _tenant_token()
    except Exception:
        return


@asynccontextmanager
async def _app_lifespan(_app: FastAPI):
    global _http
    _http = _aclient()
    warm: Optional[asyncio.Task[Any]] = None
    if not DEMO and FEISHU_APP_ID and FEISHU_APP_SECRET:
        warm = asyncio.create_task(_warm_feishu())
    try:
        yield
    finally:
        if warm is not None and not warm.done():
            warm.cancel()
        if _http is not None and not _http.is_closed:
            await _http.aclose()
        _http = None


app = FastAPI(title="富尔曼扫码出库", version="1.2.0", lifespan=_app_lifespan)

# ---------------------------------------------------------------------------
# Demo store
# ---------------------------------------------------------------------------

_demo_skus: dict[str, dict[str, Any]] = {}
_demo_outbounds: list[dict[str, Any]] = []


def _stock_status(current: int, safety: int) -> str:
    if current <= 0:
        return "缺货"
    if current <= safety:
        return "预警"
    return "正常"


def _load_demo() -> None:
    global _demo_skus, _demo_outbounds
    _demo_skus = {}
    _demo_outbounds = []
    if not SAMPLE_CSV.is_file():
        return
    with SAMPLE_CSV.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            code = (row.get("SKU编码") or "").strip()
            if not code:
                continue
            inbound = int(row.get("入库") or 0)
            outbound = int(row.get("出库") or 0)
            safety = int(row.get("安全库存") or 0)
            current = inbound - outbound
            _demo_skus[code] = {
                "record_id": f"demo-{code}",
                "sku": code,
                "name": (row.get("商品名称") or "").strip(),
                "spec": (row.get("规格/颜色") or "").strip(),
                "size": (row.get("尺寸") or "").strip(),
                "material": (row.get("材质") or "").strip(),
                "unit": (row.get("单位") or "条").strip(),
                "current": current,
                "safety": safety,
                "inbound_total": inbound,
                "outbound_total": outbound,
                "status": _stock_status(current, safety),
            }


if DEMO:
    _load_demo()

# ---------------------------------------------------------------------------
# Feishu helpers
# ---------------------------------------------------------------------------

_token_cache: dict[str, Any] = {"token": None, "expires_at": 0.0}


async def _tenant_token() -> str:
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]
    if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
        raise HTTPException(status_code=500, detail="未配置 FEISHU_APP_ID / FEISHU_APP_SECRET")
    r = await _aclient().post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
    )
    data = r.json()
    if data.get("code") != 0:
        raise HTTPException(
            status_code=502,
            detail=f"获取飞书 token 失败: {data.get('msg') or data}",
        )
    _token_cache["token"] = data["tenant_access_token"]
    _token_cache["expires_at"] = now + int(data.get("expire", 7200))
    return _token_cache["token"]


# jsapi_ticket 全局限流，必须缓存（有效期约 7200 秒）
_ticket_cache: dict[str, Any] = {"ticket": None, "expires_at": 0.0}


async def _jsapi_ticket() -> str:
    now = time.time()
    if _ticket_cache["ticket"] and now < _ticket_cache["expires_at"] - 60:
        return _ticket_cache["ticket"]
    token = await _tenant_token()
    r = await _aclient().post(
        "https://open.feishu.cn/open-apis/jssdk/ticket/get",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )
    data = r.json()
    if data.get("code") != 0:
        raise HTTPException(
            status_code=502,
            detail=f"获取 jsapi_ticket 失败: {data.get('msg') or data}",
        )
    ticket = ((data.get("data") or {}).get("ticket") or "").strip()
    if not ticket:
        raise HTTPException(status_code=502, detail="jsapi_ticket 为空")
    expire_in = int((data.get("data") or {}).get("expire_in") or 7200)
    _ticket_cache["ticket"] = ticket
    _ticket_cache["expires_at"] = now + expire_in
    return ticket


def _jsapi_signature(ticket: str, nonce_str: str, timestamp: int, url: str) -> str:
    # 官方约定：按 jsapi_ticket、noncestr、timestamp、url 顺序拼接后做 SHA1
    verify = (
        f"jsapi_ticket={ticket}"
        f"&noncestr={nonce_str}"
        f"&timestamp={timestamp}"
        f"&url={url}"
    )
    return hashlib.sha1(verify.encode("utf-8")).hexdigest()


def _normalize_jsapi_url(raw: str) -> str:
    """前端传 location.href.split('#')[0]；去掉 hash，保留 query。"""
    url = unquote((raw or "").strip())
    if not url:
        raise HTTPException(status_code=400, detail="url 不能为空")
    if "#" in url:
        url = url.split("#", 1)[0]
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="url 必须以 http(s):// 开头")
    return url


def _text_field(value: Any) -> str:
    """Normalize Feishu text / formula / dual-link display values to str."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("name") or ""))
            else:
                parts.append(str(item))
        return "".join(parts).strip()
    if isinstance(value, dict):
        return str(value.get("text") or value.get("name") or "").strip()
    return str(value).strip()


def _num_field(value: Any) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, list) and value:
        return _num_field(value[0] if not isinstance(value[0], dict) else value[0].get("text"))
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0


def _escape_filter(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _normalize_lookup_code(code: str) -> str:
    """Keep existing normalize/upper behavior for ASCII-letter codes."""
    code = (code or "").strip()
    if code.isascii() and any(c.isalpha() for c in code):
        code = code.upper()
    return code


def _current_from_fields(fields: dict[str, Any]) -> tuple[int, int, int]:
    """Stock is inbound SUM − outbound SUM.

    「当前库存」is a formula and lags the row we just wrote, so it is only a
    fallback when the SUM columns are empty.
    """
    inbound = _num_field(fields.get("入库数量"))
    outbound = _num_field(fields.get("出库数量"))
    formula = _num_field(fields.get("当前库存"))
    has_parts = ("入库数量" in fields) or ("出库数量" in fields)
    if has_parts and (inbound + outbound) > 0:
        return inbound - outbound, inbound, outbound
    return formula, inbound, outbound


async def _feishu_query_sku_by_filter(filt: str) -> Optional[dict[str, Any]]:
    token = await _tenant_token()
    url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}"
        f"/tables/{FEISHU_TABLE_SKU}/records"
    )
    r = await _aclient().get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        params={
            "filter": filt,
            "page_size": 20,
            "display_formula_ref": "true",
        },
    )
    data = r.json()
    if data.get("code") != 0:
        raise HTTPException(
            status_code=502,
            detail=f"查询 SKU 失败: {data.get('msg') or data}",
        )
    items = (data.get("data") or {}).get("items") or []
    if not items:
        return None
    rec = items[0]
    fields = rec.get("fields") or {}
    current, inbound, outbound = _current_from_fields(fields)
    safety = _num_field(fields.get("安全库存"))
    status = _text_field(fields.get("库存状态")) or _stock_status(current, safety)
    return {
        "record_id": rec.get("record_id"),
        "sku": _text_field(fields.get("SKU编码")) or "",
        "name": _text_field(fields.get("商品名称")),
        "spec": _text_field(fields.get("规格/颜色")),
        "size": _text_field(fields.get("尺寸")),
        "material": _text_field(fields.get("材质")),
        "unit": _text_field(fields.get("单位")) or "条",
        "barcode": _text_field(fields.get("条码")),
        "current": current,
        "safety": safety,
        "inbound_total": inbound,
        "outbound_total": outbound,
        "status": status,
    }


# Raw Feishu rows, keyed by scanned code / SKU / barcode. Short TTL so a repeat
# scan skips the read; stock still moves via the overlay below.
_SKU_CACHE_TTL = 20.0
_sku_cache: dict[str, tuple[float, dict[str, Any]]] = {}

# Writes Feishu's SUM has not counted yet, keyed by canonical SKU.
# base_out is the outbound_total we last observed; extra is qty we added since.
_overlay: dict[str, dict[str, int]] = {}
_sku_locks: dict[str, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()


def _cache_get(code: str) -> Optional[dict[str, Any]]:
    hit = _sku_cache.get(code)
    if not hit:
        return None
    ts, raw = hit
    if time.monotonic() - ts > _SKU_CACHE_TTL:
        _sku_cache.pop(code, None)
        return None
    return dict(raw)


def _cache_put(query: str, item: dict[str, Any]) -> None:
    raw = dict(item)
    packed = (time.monotonic(), raw)
    _sku_cache[query] = packed
    sku = str(raw.get("sku") or "")
    if sku:
        _sku_cache[sku] = packed
    barcode = str(raw.get("barcode") or "")
    if barcode:
        _sku_cache[barcode] = packed


def _overlay_left(raw: dict[str, Any]) -> int:
    """Qty we wrote that this Feishu snapshot does not show yet.

    Either signal can reveal that the write landed: outbound SUM rose, or
    reported current fell. When the formula later catches up we must not
    subtract the same qty again.
    """
    sku = str(raw.get("sku") or "")
    ov = _overlay.get(sku)
    if not ov:
        return 0
    reported = int(raw.get("current") or 0)
    base_current = int(ov.get("base_current", reported))
    drop = base_current - reported
    if drop < 0:
        # Inbound (or a correction) raised stock. Don't treat that as a
        # negative "seen", or the extra qty would be stuck forever.
        ov["base_current"] = reported
        drop = 0
    seen_by_out = int(raw.get("outbound_total") or 0) - int(ov.get("base_out") or 0)
    if seen_by_out < 0:
        seen_by_out = 0
    seen = drop if drop > seen_by_out else seen_by_out
    left = int(ov.get("extra") or 0) - seen
    if left <= 0:
        _overlay.pop(sku, None)
        return 0
    ov["extra"] = left
    ov["base_out"] = int(raw.get("outbound_total") or 0)
    ov["base_current"] = reported
    return left


def _apply_overlay(raw: dict[str, Any]) -> dict[str, Any]:
    left = _overlay_left(raw)
    if left <= 0:
        return raw
    shown = dict(raw)
    shown["current"] = int(raw.get("current") or 0) - left
    shown["outbound_total"] = int(raw.get("outbound_total") or 0) + left
    shown["status"] = _stock_status(shown["current"], int(shown.get("safety") or 0))
    return shown


def _commit_overlay(raw: dict[str, Any], qty: int) -> dict[str, Any]:
    sku = str(raw.get("sku") or "")
    left = _overlay_left(raw)
    ov = _overlay.get(sku)
    if ov is None:
        ov = {
            "base_out": int(raw.get("outbound_total") or 0),
            "base_current": int(raw.get("current") or 0),
            "extra": 0,
        }
        _overlay[sku] = ov
    ov["extra"] = left + qty
    shown = dict(raw)
    shown["current"] = int(raw.get("current") or 0) - ov["extra"]
    shown["outbound_total"] = int(raw.get("outbound_total") or 0) + ov["extra"]
    shown["status"] = _stock_status(shown["current"], int(shown.get("safety") or 0))
    return shown


async def _lock_for(key: str) -> asyncio.Lock:
    async with _locks_guard:
        lock = _sku_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _sku_locks[key] = lock
        return lock


def _silence_task(task: asyncio.Task[Any]) -> None:
    task.cancel()

    def _done(done: asyncio.Task[Any]) -> None:
        if done.cancelled():
            return
        try:
            done.exception()
        except Exception:
            return

    task.add_done_callback(_done)


async def _feishu_list_sku(code: str) -> Optional[dict[str, Any]]:
    if not FEISHU_TABLE_SKU:
        raise HTTPException(status_code=500, detail="未配置 FEISHU_TABLE_SKU")
    code = _normalize_lookup_code(code)
    if not code:
        return None
    cached = _cache_get(code)
    if cached:
        return cached
    # SKU编码 and 条码 in parallel. A barcode used to wait for a SKU miss first.
    # Prefer the SKU编码 hit when both match (same order as the old sequential lookup).
    sku_task = asyncio.create_task(
        _feishu_query_sku_by_filter(
            f'CurrentValue.[SKU编码]="{_escape_filter(code)}"'
        )
    )
    bar_task = asyncio.create_task(
        _feishu_query_sku_by_filter(
            f'CurrentValue.[条码]="{_escape_filter(code)}"'
        )
    )
    try:
        item = await sku_task
    except Exception:
        _silence_task(bar_task)
        raise
    if item:
        _silence_task(bar_task)
        if not item.get("sku"):
            item["sku"] = code
        _cache_put(code, item)
        return item
    item = await bar_task
    if item:
        if not item.get("sku"):
            item["sku"] = code
        _cache_put(code, item)
        return item
    return None


async def _feishu_create_outbound(
    sku_record_id: str, sku_code: str, qty: int, note: str, order_no: str, date_ms: int
) -> dict[str, Any]:
    if not FEISHU_TABLE_OUT:
        raise HTTPException(status_code=500, detail="未配置 FEISHU_TABLE_OUT")
    token = await _tenant_token()
    # 关联字段写入 record_id 列表；出库时间为毫秒时间戳；出库时刻精确到秒
    time_text = datetime.fromtimestamp(
        date_ms / 1000.0, TZ_SH
    ).strftime("%Y/%m/%d %H:%M:%S")
    fields: dict[str, Any] = {
        "出库单号": order_no,
        "出库时间": date_ms,
        "出库时刻": time_text,
        "SKU": [sku_record_id],
        "数量": qty,
    }
    if note:
        fields["备注"] = note
    url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}"
        f"/tables/{FEISHU_TABLE_OUT}/records"
    )
    r = await _aclient().post(
        url,
        headers={"Authorization": f"Bearer {token}"},
        json={"fields": fields},
    )
    data = r.json()
    if data.get("code") != 0:
        # 兼容字段名差异：有的表可能把关联列叫「SKU编码」或文本字段
        msg = str(data.get("msg") or data)
        if "SKU" in msg or data.get("code") in (1254045, 1254060, 1254013):
            fields_alt = {
                "出库单号": order_no,
                "出库时间": date_ms,
                "出库时刻": time_text,
                "数量": qty,
                "备注": note or f"扫码出库 {sku_code}",
            }
            # 再试：文本 SKU
            fields_alt["SKU"] = sku_code
            r2 = await _aclient().post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json={"fields": fields_alt},
            )
            data = r2.json()
        if data.get("code") != 0:
            raise HTTPException(
                status_code=502,
                detail=f"写出库记录失败: {data.get('msg') or data}",
            )
    return data.get("data") or {}


# ---------------------------------------------------------------------------
# API models
# ---------------------------------------------------------------------------


class OutboundBody(BaseModel):
    sku: str = Field(..., min_length=1, description="SKU 编码")
    qty: int = Field(..., ge=1, description="出库数量")
    note: Optional[str] = Field(None, description="备注")


def _make_order_no() -> str:
    now = datetime.now(TZ_SH)
    suffix = uuid.uuid4().hex[:4].upper()
    return f"CK-{now.strftime('%Y%m%d-%H%M%S')}-{suffix}"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/api/feishu/jsapi-config")
async def feishu_jsapi_config(url: str = Query(..., description="当前页完整 URL，不含 #hash")):
    """给飞书 H5 SDK config 用的签名参数。

    前端应传 encodeURIComponent(location.href.split('#')[0])。
    签名有效约 10 分钟，每次进入页面重新拉取即可，不要长期缓存 signature。
    """
    if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
        raise HTTPException(status_code=500, detail="未配置 FEISHU_APP_ID / FEISHU_APP_SECRET")
    page_url = _normalize_jsapi_url(url)
    ticket = await _jsapi_ticket()
    timestamp = int(time.time())
    nonce_str = secrets.token_hex(8)
    signature = _jsapi_signature(ticket, nonce_str, timestamp, page_url)
    return {
        "ok": True,
        "appId": FEISHU_APP_ID,
        "timestamp": timestamp,
        "nonceStr": nonce_str,
        "signature": signature,
        "url": page_url,
    }


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "demo": DEMO,
        "sku_count": len(_demo_skus) if DEMO else None,
        "app_token": FEISHU_APP_TOKEN,
        "table_sku_set": bool(FEISHU_TABLE_SKU),
        "table_out_set": bool(FEISHU_TABLE_OUT),
    }


@app.get("/api/sku/{code}")
async def get_sku(code: str):
    code = code.strip()
    if not code:
        raise HTTPException(status_code=400, detail="SKU 编码不能为空")

    if DEMO:
        item = _demo_skus.get(code)
        if not item:
            raise HTTPException(status_code=404, detail=f"未找到 SKU：{code}")
        return {"ok": True, "demo": True, "sku": item}

    item = await _feishu_list_sku(code)
    if not item:
        raise HTTPException(status_code=404, detail=f"未找到 SKU：{code}")
    return {"ok": True, "demo": False, "sku": _apply_overlay(item)}


@app.post("/api/outbound")
async def create_outbound(body: OutboundBody):
    code = body.sku.strip()
    qty = body.qty
    note = (body.note or "").strip()
    order_no = _make_order_no()
    now = datetime.now(TZ_SH)
    date_ms = int(now.timestamp() * 1000)

    if DEMO:
        item = _demo_skus.get(code)
        if not item:
            raise HTTPException(status_code=404, detail=f"未找到 SKU：{code}")
        if item["current"] < qty:
            raise HTTPException(
                status_code=400,
                detail=f"库存不足：当前 {item['current']}，要出 {qty}",
            )
        item["outbound_total"] += qty
        item["current"] -= qty
        item["status"] = _stock_status(item["current"], item["safety"])
        rec = {
            "order_no": order_no,
            "sku": code,
            "qty": qty,
            "note": note,
            "date": now.isoformat(),
        }
        _demo_outbounds.append(rec)
        return {
            "ok": True,
            "demo": True,
            "order_no": order_no,
            "qty": qty,
            "sku": item,
            "message": f"出库成功 {qty} {item.get('unit') or '条'}",
        }

    raw = await _feishu_list_sku(code)
    if not raw:
        raise HTTPException(status_code=404, detail=f"未找到 SKU：{code}")
    sku_key = str(raw.get("sku") or _normalize_lookup_code(code))
    async with await _lock_for(sku_key):
        # Re-read the short cache under the lock so a burst of the same SKU
        # counts writes the SUM has not caught up to yet.
        fresh = _cache_get(sku_key) or raw
        shown = _apply_overlay(fresh)
        if shown["current"] < qty:
            raise HTTPException(
                status_code=400,
                detail=f"库存不足：当前 {shown['current']}，要出 {qty}",
            )
        rid = fresh.get("record_id")
        if not rid:
            raise HTTPException(status_code=500, detail="SKU 记录缺少 record_id，无法关联出库")
        await _feishu_create_outbound(rid, code, qty, note, order_no, date_ms)
        # Do not re-query Feishu. The SUM fields lag; the overlay is the new stock.
        refreshed = _commit_overlay(fresh, qty)
    return {
        "ok": True,
        "demo": False,
        "order_no": order_no,
        "qty": qty,
        "sku": refreshed,
        "message": f"出库成功 {qty} {refreshed.get('unit') or '条'}，单号 {order_no}",
    }


@app.get("/api/demo/skus")
async def list_demo_skus():
    if not DEMO:
        raise HTTPException(status_code=400, detail="仅演示模式可用")
    return {"ok": True, "items": list(_demo_skus.values())}


@app.post("/api/demo/reset")
async def reset_demo():
    if not DEMO:
        raise HTTPException(status_code=400, detail="仅演示模式可用")
    _load_demo()
    return {"ok": True, "sku_count": len(_demo_skus)}


@app.exception_handler(HTTPException)
async def http_exc_handler(request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"ok": False, "detail": exc.detail},
    )


@app.get("/manifest.webmanifest")
async def web_manifest():
    return FileResponse(
        ROOT / "manifest.webmanifest",
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/sw.js")
async def service_worker():
    return FileResponse(
        ROOT / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


# no-cache so an installed shortcut refetches the F mark instead of a stale circle.
_ICON_HEADERS = {"Cache-Control": "no-cache"}


@app.get("/favicon.ico")
async def favicon():
    return FileResponse(
        ROOT / "favicon.ico",
        media_type="image/x-icon",
        headers=_ICON_HEADERS,
    )


@app.get("/icon-192.png")
async def icon_192():
    return FileResponse(
        ROOT / "icon-192.png",
        media_type="image/png",
        headers=_ICON_HEADERS,
    )


@app.get("/icon-512.png")
async def icon_512():
    return FileResponse(
        ROOT / "icon-512.png",
        media_type="image/png",
        headers=_ICON_HEADERS,
    )


@app.get("/apple-touch-icon.png")
async def apple_touch_icon():
    return FileResponse(
        ROOT / "apple-touch-icon.png",
        media_type="image/png",
        headers=_ICON_HEADERS,
    )


@app.get("/")
async def index():
    return FileResponse(ROOT / "index.html")


# Static QR preview images (optional)
qr_dir = Path("/workspace/qr")
if qr_dir.is_dir():
    app.mount("/qr", StaticFiles(directory=str(qr_dir)), name="qr")


def main():
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8765"))
    uvicorn.run("server:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
