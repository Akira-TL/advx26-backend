"""Public NFC preview page — served when /c/{content_id} is opened in a browser."""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request, Response

from .database import Database
from .object_store import FileSystemObjectStore

CONTENT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")

_PREVIEW_ASSETS = {"audio": "AUDIO", "video": "VIDEO"}


def serve_preview_page(database: Database, content_id: str, request: Request) -> Response:
    """Render the HTML preview page for browser access to /c/{content_id}."""
    with database.connect() as conn:
        row = conn.execute(
            "SELECT display_label, duration_ms, state, created_at FROM contents "
            "WHERE id=? AND deleted_at IS NULL",
            (content_id,),
        ).fetchone()
        if row is None:
            return _html_page(_PageData(error="内容不存在"))
        if row["state"] != "READY":
            return _html_page(_PageData(error="内容处理中，请稍后再试"))

        chain_row = conn.execute(
            "SELECT chain_state, token_id, tx_hash, contract_address FROM content_chain "
            "WHERE content_id=?",
            (content_id,),
        ).fetchone()
        editions_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM content_editions WHERE content_id=?",
            (content_id,),
        ).fetchone()["cnt"]

    base = str(request.base_url).rstrip("/")
    on_chain = chain_row is not None and chain_row["chain_state"] == "MINTED"
    data = _PageData(
        content_id=content_id,
        title=row["display_label"],
        duration_ms=row["duration_ms"],
        created_at=row["created_at"] or "",
        audio_url=f"{base}/preview/{content_id}/audio",
        video_url=f"{base}/preview/{content_id}/video",
        on_chain=on_chain,
        token_id=chain_row["token_id"] if chain_row else None,
        tx_hash=chain_row["tx_hash"] if chain_row else None,
        contract_address=chain_row["contract_address"] if chain_row else None,
        editions_count=editions_count,
    )
    return _html_page(data)


class _PageData:
    __slots__ = (
        "content_id", "title", "duration_ms", "created_at",
        "audio_url", "video_url", "on_chain",
        "token_id", "tx_hash", "contract_address", "editions_count", "error",
    )

    def __init__(self, *, error: str | None = None, **kw):
        self.error = error
        self.content_id = kw.get("content_id", "")
        self.title = kw.get("title", "")
        self.duration_ms = kw.get("duration_ms", 0)
        self.created_at = kw.get("created_at", "")
        self.audio_url = kw.get("audio_url")
        self.video_url = kw.get("video_url")
        self.on_chain = kw.get("on_chain", False)
        self.token_id = kw.get("token_id")
        self.tx_hash = kw.get("tx_hash")
        self.contract_address = kw.get("contract_address")
        self.editions_count = kw.get("editions_count", 0)


def create_preview_router(
    *,
    database: Database,
    object_store: FileSystemObjectStore,
) -> APIRouter:
    router = APIRouter(tags=["preview"])

    @router.get("/preview/{content_id}/{asset_kind}", include_in_schema=False)
    async def preview_asset(content_id: str, asset_kind: str) -> Response:
        if not CONTENT_ID_PATTERN.fullmatch(content_id):
            raise HTTPException(status_code=404, detail="内容不存在")
        kind = _PREVIEW_ASSETS.get(asset_kind)
        if kind is None:
            raise HTTPException(status_code=404, detail="资源不存在")
        with database.connect() as conn:
            row = conn.execute(
                "SELECT mo.object_key, mo.content_type, mo.byte_length, mo.sha256, mo.etag "
                "FROM media_objects mo JOIN contents c ON mo.content_id = c.id "
                "WHERE mo.content_id=? AND mo.kind=? AND c.state='READY' AND c.deleted_at IS NULL",
                (content_id, kind),
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="资源不存在")
        try:
            with object_store.open(row["object_key"]) as f:
                payload = f.read()
        except OSError:
            raise HTTPException(status_code=503, detail="资源不可用")
        return Response(
            content=payload,
            media_type=row["content_type"],
            headers={
                "Cache-Control": "public, max-age=86400",
                "ETag": row["etag"],
                "Accept-Ranges": "bytes",
            },
        )

    return router


def _short_addr(addr: str | None) -> str:
    if not addr or len(addr) < 12:
        return ""
    return f"{addr[:6]}…{addr[-4:]}"


def _format_date(iso: str) -> str:
    if not iso:
        return ""
    return iso[:10].replace("-", ".")


def _html_page(d: _PageData) -> Response:
    if d.error:
        html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SoundPola</title>
<style>body{{font-family:"Noto Sans SC",-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#000;color:#9a9a9a;display:flex;align-items:center;justify-content:center;min-height:100dvh;font-size:15px}}</style>
</head><body><p>{d.error}</p></body></html>"""
        return Response(content=html, media_type="text/html; charset=utf-8")

    duration_s = f"{d.duration_ms / 1000:.1f}" if d.duration_ms else "—"
    date_str = _format_date(d.created_at)
    deep_link = f"soundpola://c/{d.content_id}"

    if d.on_chain:
        chain_badge = '<span class="badge on-chain">&#9670; 已上链</span>'
        chain_detail = f'''
    <div class="chain-info">
      <div class="chain-row"><span class="label">Token</span><span class="value">#{d.token_id}</span></div>
      <div class="chain-row"><span class="label">合约</span><span class="value mono">{_short_addr(d.contract_address)}</span></div>
      <div class="chain-row"><span class="label">收藏</span><span class="value">{d.editions_count} 人已领取</span></div>
    </div>
    <div class="btns">
      <a class="btn-primary" id="claimBtn" href="{deep_link}">在 SoundPola 中领取</a>
      <button class="btn-ghost" id="copyBtn" type="button">复制声片链接</button>
    </div>
    <p class="hint">打开 App 将此声音添加到你的链上钱包</p>'''
    else:
        chain_badge = '<span class="badge preview-only">&#9671; 仅预览</span>'
        chain_detail = '''
    <div class="chain-info">
      <p class="not-chained">此声音尚未上链，仅供试听预览</p>
    </div>
    <div class="btns">
      <button class="btn-ghost" id="copyBtn" type="button">复制声片链接</button>
    </div>'''

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>SoundPola · {d.title}</title>
<meta property="og:title" content="SoundPola · {d.title}">
<meta property="og:description" content="{duration_s}s 声音记忆{'· 已上链存证' if d.on_chain else ''}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@700&family=Noto+Sans+SC:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
::selection{{background:rgba(99,224,203,.28);color:#fff}}
body{{font-family:"Noto Sans SC",-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#000;color:#fff;min-height:100dvh;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:24px}}
.card{{width:100%;max-width:380px;background:#000;border:1px solid rgba(255,255,255,.06);border-radius:16px;overflow:hidden}}
.visual{{position:relative;width:100%;aspect-ratio:1;background:#000;display:flex;align-items:center;justify-content:center;overflow:hidden;cursor:pointer;-webkit-tap-highlight-color:transparent}}
.visual video{{width:100%;height:100%;object-fit:cover}}
.tap-hint{{position:absolute;width:48px;height:48px;border-radius:50%;background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center;backdrop-filter:blur(4px);transition:opacity .35s;pointer-events:none}}
.tap-hint svg{{width:19px;height:19px;fill:#fff;margin-left:2px}}
.tap-hint.hidden{{opacity:0}}
.info{{padding:20px 24px 24px}}
.title{{font-size:18px;font-weight:600;line-height:1.4;margin-bottom:8px}}
.meta{{font-size:13px;color:#9a9a9a;display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:4px}}
.badge{{display:inline-flex;align-items:center;gap:4px;font-size:11px;border-radius:999px;padding:4px 10px;font-weight:500}}
.badge.on-chain{{color:#f0c060;background:rgba(240,192,96,.1);border:1px solid rgba(240,192,96,.3)}}
.badge.preview-only{{color:#9a9a9a;background:rgba(154,154,154,.1);border:1px solid rgba(154,154,154,.25)}}
.progress{{margin-top:14px;height:3px;background:rgba(255,255,255,.08);border-radius:999px;overflow:hidden;cursor:pointer}}
.progress-bar{{height:100%;width:0%;background:#63e0cb;border-radius:999px;transition:width .1s linear}}
.time{{margin-top:6px;font-size:11px;color:#666;display:flex;justify-content:space-between}}
.detail{{margin-top:16px;padding-top:16px;border-top:1px solid rgba(255,255,255,.06)}}
.detail-row{{display:flex;justify-content:space-between;align-items:center;gap:16px;font-size:12px;color:#9a9a9a;margin-bottom:6px}}
.detail-row .val{{color:#fff;font-weight:500}}
.mono{{font-family:"IBM Plex Mono","SF Mono",Menlo,monospace;font-size:11px}}
.chain-info{{margin-top:16px;padding:14px;background:#000;border-radius:12px}}
.chain-row{{display:flex;justify-content:space-between;font-size:12px;margin-bottom:6px}}
.chain-row:last-child{{margin-bottom:0}}
.chain-row .label{{color:#666}}
.chain-row .value{{color:#fff;font-weight:500}}
.not-chained{{font-size:12px;color:#666;text-align:center;padding:4px 0}}
.btns{{display:grid;gap:10px;margin-top:16px}}
.btn-primary,.btn-ghost{{display:flex;align-items:center;justify-content:center;gap:8px;width:100%;min-height:48px;padding:0 20px;border-radius:8px;font-family:"Syne","Noto Sans SC",sans-serif;font-size:14px;font-weight:700;letter-spacing:.02em;text-align:center;text-decoration:none;cursor:pointer;-webkit-tap-highlight-color:transparent;touch-action:manipulation}}
.btn-primary{{border:1px solid #63e0cb;background:#63e0cb;color:#000;transition:background .15s ease,transform .15s ease}}
.btn-primary:hover{{background:#74e7d3}}
.btn-primary:active{{transform:translateY(1px)}}
.btn-ghost{{border:1px solid rgba(99,224,203,.28);background:transparent;color:#fff;transition:border-color .15s ease,color .15s ease,transform .15s ease}}
.btn-ghost:hover{{border-color:#63e0cb;color:#63e0cb}}
.btn-ghost:active{{transform:translateY(1px)}}
.btn-primary:focus-visible,.btn-ghost:focus-visible{{outline:2px solid #63e0cb;outline-offset:2px}}
.hint{{margin-top:8px;font-size:11px;color:#666;text-align:center}}
.footer{{margin-top:24px;text-align:center;font-size:12px;color:#666}}
.footer a{{color:#63e0cb;text-decoration:none}}
@media(prefers-reduced-motion:reduce){{*,*::before,*::after{{animation-duration:.01ms!important;animation-iteration-count:1!important;transition-duration:.01ms!important}}}}
</style>
</head>
<body>
<div class="card">
  <div class="visual" id="visual" onclick="togglePlay()">
    <video id="vid" loop playsinline preload="metadata" src="{d.video_url}"></video>
    <div class="tap-hint" id="hint"><svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg></div>
  </div>
  <div class="info">
    <div class="title">{d.title}</div>
    <div class="meta">
      {chain_badge}
      <span>{duration_s}s</span>
      <span>{date_str}</span>
    </div>
    <div class="progress" id="progress" onclick="seek(event)">
      <div class="progress-bar" id="bar"></div>
    </div>
    <div class="time"><span id="cur">0:00</span><span>{duration_s}s</span></div>
    <div class="detail">
      <div class="detail-row"><span>内容 ID</span><span class="val mono">{d.content_id[:12]}…</span></div>
      <div class="detail-row"><span>时长</span><span class="val">{duration_s} 秒</span></div>
      <div class="detail-row"><span>创建</span><span class="val">{date_str}</span></div>
    </div>
    {chain_detail}
  </div>
  <audio id="aud" preload="auto" src="{d.audio_url}"></audio>
</div>
<div class="footer">由 <a href="#">SoundPola</a> 铸造 · 声音记忆上链存证</div>
<script>
const aud=document.getElementById("aud"),vid=document.getElementById("vid"),
bar=document.getElementById("bar"),cur=document.getElementById("cur"),
hint=document.getElementById("hint");
let playing=false;
function togglePlay(){{playing?pause():play()}}
function play(){{aud.play();vid.play();playing=true;hint.classList.add("hidden")}}
function pause(){{aud.pause();vid.pause();playing=false;hint.classList.remove("hidden")}}
aud.addEventListener("timeupdate",()=>{{if(!aud.duration)return;bar.style.width=(aud.currentTime/aud.duration*100)+"%";cur.textContent=fmt(aud.currentTime)}});
aud.addEventListener("ended",()=>{{pause();bar.style.width="0%";cur.textContent="0:00";aud.currentTime=0;vid.currentTime=0}});
function seek(e){{if(!aud.duration)return;e.stopPropagation();const r=e.currentTarget.getBoundingClientRect();const p=(e.clientX-r.left)/r.width;aud.currentTime=p*aud.duration;vid.currentTime=p*vid.duration}}
function fmt(s){{const m=Math.floor(s/60),ss=Math.floor(s%60);return m+":"+(ss<10?"0":"")+ss}}
(function(){{
  var cid="{d.content_id}";
  var ua=navigator.userAgent;
  var isAndroid=/Android/i.test(ua);
  var isIOS=/iPhone|iPad|iPod/i.test(ua);
  var scheme="soundpola://c/"+cid;
  var intent="intent://c/"+cid+"#Intent;scheme=soundpola;package=com.soundpola.soundpola;S.browser_fallback_url="+encodeURIComponent(location.href)+";end";
  var btn=document.getElementById("claimBtn");
  if(btn){{btn.href=isAndroid?intent:scheme;}}
  var key="soundpola_jump_"+cid,jumped=false;
  try{{jumped=sessionStorage.getItem(key)==="1";if(!jumped)sessionStorage.setItem(key,"1");}}catch(e){{}}
  if(jumped)return;
  if(isAndroid){{
    location.href=intent;
  }}else if(isIOS){{
    var f=document.createElement("iframe");
    f.style.display="none";
    f.src=scheme;
    document.body.appendChild(f);
    setTimeout(function(){{f.remove()}},2500);
  }}
}})();
var copyBtn=document.getElementById("copyBtn");
if(copyBtn){{copyBtn.addEventListener("click",function(){{
  var link=location.href;
  if(navigator.clipboard&&navigator.clipboard.writeText){{
    navigator.clipboard.writeText(link).then(function(){{copyBtn.textContent="已复制";}});
  }}else{{window.prompt("请手动复制链接",link);}}
  setTimeout(function(){{copyBtn.textContent="复制声片链接";}},1600);
}});}}
</script>
</body>
</html>"""
    return Response(content=html, media_type="text/html; charset=utf-8")
