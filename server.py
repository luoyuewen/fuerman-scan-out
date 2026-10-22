#!/usr/bin/env python3
"""富尔曼仓库扫码出库 — FastAPI 后端。

DEMO=1 时用 /workspace/qr/sample.csv 内存演示，不连飞书。
正式模式通过飞书开放平台读写「商品SKU库存」「出库记录」。
"""

from __future__ import annotations

import csv
import hashlib
import os
import re
import secrets
import time
import uuid
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

app = FastAPI(title="富尔曼扫码出库", version="1.1.0")

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
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(
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
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(
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


async def _feishu_list_sku(code: str) -> Optional[dict[str, Any]]:
    if not FEISHU_TABLE_SKU:
        raise HTTPException(status_code=500, detail="未配置 FEISHU_TABLE_SKU")
    token = await _tenant_token()
    filt = f'CurrentValue.[SKU编码]="{_escape_filter(code)}"'
    url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}"
        f"/tables/{FEISHU_TABLE_SKU}/records"
    )
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(
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
    current = _num_field(fields.get("当前库存"))
    safety = _num_field(fields.get("安全库存"))
    status = _text_field(fields.get("库存状态")) or _stock_status(current, safety)
    return {
        "record_id": rec.get("record_id"),
        "sku": _text_field(fields.get("SKU编码")) or code,
        "name": _text_field(fields.get("商品名称")),
        "spec": _text_field(fields.get("规格/颜色")),
        "size": _text_field(fields.get("尺寸")),
        "material": _text_field(fields.get("材质")),
        "unit": _text_field(fields.get("单位")) or "条",
        "current": current,
        "safety": safety,
        "inbound_total": _num_field(fields.get("入库数量")),
        "outbound_total": _num_field(fields.get("出库数量")),
        "status": status,
    }


async def _feishu_create_outbound(
    sku_record_id: str, sku_code: str, qty: int, note: str, order_no: str, date_ms: int
) -> dict[str, Any]:
    if not FEISHU_TABLE_OUT:
        raise HTTPException(status_code=500, detail="未配置 FEISHU_TABLE_OUT")
    token = await _tenant_token()
    # 关联字段写入 record_id 列表；日期为毫秒时间戳
    fields: dict[str, Any] = {
        "出库单号": order_no,
        "日期": date_ms,
        "SKU": [sku_record_id],
        "数量": qty,
    }
    if note:
        fields["备注"] = note
    url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}"
        f"/tables/{FEISHU_TABLE_OUT}/records"
    )
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(
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
                "日期": date_ms,
                "数量": qty,
                "备注": note or f"扫码出库 {sku_code}",
            }
            # 再试：文本 SKU
            fields_alt["SKU"] = sku_code
            async with httpx.AsyncClient(timeout=20.0) as client:
                r2 = await client.post(
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
    return {"ok": True, "demo": False, "sku": item}


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

    item = await _feishu_list_sku(code)
    if not item:
        raise HTTPException(status_code=404, detail=f"未找到 SKU：{code}")
    if item["current"] < qty:
        raise HTTPException(
            status_code=400,
            detail=f"库存不足：当前 {item['current']}，要出 {qty}",
        )
    rid = item.get("record_id")
    if not rid:
        raise HTTPException(status_code=500, detail="SKU 记录缺少 record_id，无法关联出库")
    await _feishu_create_outbound(rid, code, qty, note, order_no, date_ms)
    # 重新读一遍，拿公式更新后的库存（可能有短暂延迟）
    refreshed = await _feishu_list_sku(code) or item
    # 乐观扣减：若公式尚未刷新，本地展示减后数量
    if refreshed["current"] == item["current"]:
        refreshed = dict(refreshed)
        refreshed["current"] = max(0, item["current"] - qty)
        refreshed["outbound_total"] = item.get("outbound_total", 0) + qty
        refreshed["status"] = _stock_status(refreshed["current"], refreshed["safety"])
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
