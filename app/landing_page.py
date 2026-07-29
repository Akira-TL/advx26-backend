"""SoundPola landing page and APK download."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import FileResponse

_RELEASES_DIR = Path(__file__).resolve().parent.parent / "releases"
_APK_NAME = "soundpola-android.apk"


def create_landing_router() -> APIRouter:
    router = APIRouter(tags=["landing"])

    @router.get("/", include_in_schema=False)
    async def landing_page() -> Response:
        return Response(content=_landing_html(), media_type="text/html; charset=utf-8")

    @router.get("/download/soundpola.apk", include_in_schema=False)
    async def download_apk() -> Response:
        apk = _RELEASES_DIR / _APK_NAME
        if not apk.exists():
            raise HTTPException(
                status_code=404,
                detail="安装包尚未发布，请将 APK 放置到 releases/soundpola-android.apk",
            )
        return FileResponse(
            path=apk,
            filename="SoundPola.apk",
            media_type="application/vnd.android.package-archive",
        )

    return router


def _landing_html() -> str:
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SoundPola · 把声音铸造成可触摸的记忆</title>
<meta name="description" content="SoundPola — 录制声音、写入声片、上链存证。下载 App 开始收藏你的声音记忆。">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@700;800&family=Noto+Sans+SC:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#000;--mint:#63e0cb;--jade:#3db8a5;--gold:#f0c060;--text:#fff;--text2:#9a9a9a;--text3:#666}
html{scroll-behavior:smooth}
body{font-family:"Noto Sans SC",-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--bg);color:var(--text);line-height:1.6}
::selection{background:rgba(99,224,203,.28);color:#fff}
.wrap{max-width:1120px;margin:0 auto;padding:0 24px}
@media(min-width:640px){.wrap{padding:0 32px}}
@media(min-width:768px){.wrap{padding:0 48px}}
@keyframes glitchIn{0%{opacity:0;transform:translate(2px,-1px)}40%{opacity:1;transform:translate(-1px,1px)}100%{opacity:1;transform:translate(0,0)}}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.35}}
nav{position:sticky;top:0;z-index:40;background:#000;border-bottom:1px solid transparent;transition:background .2s,border-color .2s}
nav.scrolled{background:rgba(0,0,0,.95);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);border-bottom-color:rgba(255,255,255,.09)}
.nav-inner{display:flex;align-items:center;justify-content:space-between;height:56px}
@media(min-width:640px){.nav-inner{height:64px}}
.logo{display:flex;align-items:center;gap:10px;font-family:"Syne","Noto Sans SC",sans-serif;font-size:18px;font-weight:700;text-decoration:none;color:var(--text)}
.logo-mark{width:32px;height:32px;border-radius:8px;background:var(--mint);display:flex;align-items:center;justify-content:center;font-family:"Syne",sans-serif;font-size:13px;font-weight:800;line-height:1;color:#000}
.logo-tag{display:none;font-family:"IBM Plex Mono",monospace;font-size:10px;letter-spacing:.14em;color:var(--mint)}
@media(min-width:640px){.logo-tag{display:inline}}
.nav-link{color:var(--text2);text-decoration:none;font-family:"IBM Plex Mono",monospace;font-size:12px;letter-spacing:.12em;transition:color .2s}
.nav-link:hover{color:var(--mint)}
.hero{text-align:center;padding:80px 0 60px}
.badge{display:inline-flex;align-items:center;gap:8px;font-family:"IBM Plex Mono",monospace;font-size:11px;letter-spacing:.14em;color:var(--mint);margin-bottom:20px;animation:glitchIn .55s ease-out both}
.badge::before{content:"";width:6px;height:6px;border-radius:50%;background:var(--mint);animation:blink 2.4s ease-in-out infinite}
.hero h1{font-family:"Syne","Noto Sans SC",sans-serif;font-size:clamp(2rem,6.5vw,4.25rem);font-weight:800;letter-spacing:-.02em;line-height:1.06;margin-bottom:16px;animation:glitchIn .55s ease-out both;animation-delay:60ms}
.hero p{font-size:clamp(15px,2.5vw,17px);color:var(--text2);max-width:520px;margin:0 auto 36px;line-height:1.75}
.btn-row{display:flex;gap:14px;justify-content:center;flex-wrap:wrap}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:48px;padding:0 20px;border-radius:8px;font-family:"Syne","Noto Sans SC",sans-serif;font-weight:700;font-size:14px;letter-spacing:.02em;text-decoration:none;-webkit-tap-highlight-color:transparent;touch-action:manipulation;transition:background .15s ease,border-color .15s ease,color .15s ease,transform .15s ease}
.btn:focus-visible{outline:2px solid var(--mint);outline-offset:2px}
.btn:active{transform:translateY(1px)}
.btn-primary{border:1px solid var(--mint);background:var(--mint);color:#000}
.btn-primary:hover{background:#74e7d3}
.btn-secondary{border:1px solid rgba(99,224,203,.28);background:transparent;color:var(--text)}
.btn-secondary:hover{border-color:var(--mint);color:var(--mint)}
.btn-secondary.disabled{opacity:.45;pointer-events:none}
.btn svg{width:16px;height:16px;fill:currentColor}
.btn .reveal{display:none;font-family:"IBM Plex Mono",monospace;font-size:10px;letter-spacing:.08em;color:rgba(0,0,0,.7)}
.btn:hover .reveal{display:inline}
.hint{margin-top:14px;font-family:"IBM Plex Mono",monospace;font-size:11px;letter-spacing:.08em;color:var(--text3)}
.kicker{font-family:"IBM Plex Mono",monospace;font-size:11px;letter-spacing:.16em;color:var(--mint);margin-bottom:12px}
.section{padding:64px 0;border-top:1px solid rgba(255,255,255,.09)}
@media(min-width:768px){.section{padding:96px 0}}
.section h2{font-family:"Syne","Noto Sans SC",sans-serif;font-size:clamp(1.5rem,4.5vw,2.25rem);font-weight:700;margin:12px 0}
.section .sub{color:var(--text2);font-size:15px;line-height:1.75;max-width:672px;margin-bottom:40px}
.steps{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:24px;counter-reset:step}
.step{position:relative;padding-top:20px;border-top:1px solid rgba(255,255,255,.09)}
.step::before{counter-increment:step;content:"0" counter(step);font-family:"IBM Plex Mono",monospace;font-size:clamp(24px,3vw,36px);font-weight:500;letter-spacing:-.02em;color:rgba(99,224,203,.25);display:block;margin-bottom:10px}
.step h3{font-size:15px;font-weight:600;margin-bottom:6px}
.step p{font-size:14px;line-height:1.7;color:var(--text2)}
.grid{display:grid;grid-template-columns:1fr;border-top:1px solid rgba(255,255,255,.09)}
@media(min-width:768px){.grid{grid-template-columns:1fr 1fr}}
.card{padding:24px;border-bottom:1px solid rgba(255,255,255,.09);transition:background .2s}
@media(min-width:768px){.card{padding:32px}.grid .card:nth-child(odd){border-right:1px solid rgba(255,255,255,.09)}}
.card:hover{background:rgba(255,255,255,.02)}
.card-top{display:flex;align-items:flex-start;justify-content:space-between;gap:16px}
.card-num{font-family:"IBM Plex Mono",monospace;font-size:clamp(36px,5vw,52px);line-height:1;letter-spacing:-.02em;color:rgba(99,224,203,.2)}
.card-top svg{width:40px;height:40px;flex:none}
@media(min-width:640px){.card-top svg{width:48px;height:48px}}
.card h3{margin-top:20px;font-size:18px;font-weight:600}
.card p{margin-top:8px;font-size:14px;line-height:1.7;color:var(--text2);max-width:448px}
.card-line{margin-top:20px;height:1px;width:48px;background:rgba(99,224,203,.3);transition:width .25s ease,background .25s ease}
.card:hover .card-line{width:80px;background:rgba(99,224,203,.7)}
.download-box{position:relative;background:#000;border:1px solid rgba(99,224,203,.16);border-radius:10px;padding:24px;margin-top:32px}
@media(min-width:768px){.download-box{padding:40px}}
.corner{position:absolute;width:12px;height:12px;pointer-events:none}
.corner.tl{top:8px;left:8px;border-left:1px solid rgba(99,224,203,.5);border-top:1px solid rgba(99,224,203,.5)}
.corner.tr{top:8px;right:8px;border-right:1px solid rgba(99,224,203,.5);border-top:1px solid rgba(99,224,203,.5)}
.corner.bl{bottom:8px;left:8px;border-left:1px solid rgba(99,224,203,.5);border-bottom:1px solid rgba(99,224,203,.5)}
.corner.br{bottom:8px;right:8px;border-right:1px solid rgba(99,224,203,.5);border-bottom:1px solid rgba(99,224,203,.5)}
@media(min-width:640px){.corner.tl{top:12px;left:12px}.corner.tr{top:12px;right:12px}.corner.bl{bottom:12px;left:12px}.corner.br{bottom:12px;right:12px}}
.term-label{font-family:"IBM Plex Mono",monospace;font-size:11px;letter-spacing:.16em;color:var(--mint);margin-bottom:20px}
.download-box .btn-row{justify-content:flex-start}
.dl-status{display:inline-flex;align-items:center;gap:8px;margin-top:16px;font-family:"IBM Plex Mono",monospace;font-size:11px;letter-spacing:.12em;color:var(--mint)}
.dl-status .dot{width:6px;height:6px;border-radius:50%;background:var(--mint);animation:blink 2.4s ease-in-out infinite}
.qr-note{margin-top:12px;font-size:12px;line-height:1.6;color:var(--text3)}
footer{border-top:1px solid rgba(255,255,255,.09);padding:28px 0;padding-bottom:calc(28px + env(safe-area-inset-bottom));text-align:center;font-size:12px;color:var(--text3)}
footer .mono{font-family:"IBM Plex Mono",monospace;letter-spacing:.08em}
footer a{color:var(--text2);text-decoration:none}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:.01ms!important;animation-iteration-count:1!important;transition-duration:.01ms!important;scroll-behavior:auto!important}}
</style>
</head>
<body>
<nav id="nav">
  <div class="wrap nav-inner">
    <a class="logo" href="/"><span class="logo-mark">SP</span>SoundPola<span class="logo-tag">BETA / 1.0.0</span></a>
    <a class="nav-link" href="#download">下载</a>
  </div>
</nav>
<div class="wrap">
  <section class="hero">
    <p class="badge">声音记忆 / 链上存证</p>
    <h1>把声音铸造成<br><span>可触摸的记忆</span></h1>
    <p>录制一段声音，写入实体声片， mint 上链永久存证。声音从此有了形状、有了归属。</p>
    <div class="btn-row">
      <a class="btn btn-primary" href="https://babelbeast.com/app-release.apk">
        <svg viewBox="0 0 24 24"><path d="M17.5 12.5l-4-7a1 1 0 00-1.7 0l-4 7A5.5 5.5 0 003 18v.5A1.5 1.5 0 004.5 20h15a1.5 1.5 0 001.5-1.5V18a5.5 5.5 0 00-3.5-5.5zM7 9l-2.5-4M17 9l2.5-4"/></svg>
        Android APK 下载
        <span class="reveal">READY TO DOWNLOAD</span>
      </a>
      <a class="btn btn-secondary disabled" href="#">
        <svg viewBox="0 0 24 24"><path d="M16.4 12.8c0-2.3 1.9-3.4 2-3.5-1.1-1.6-2.8-1.8-3.4-1.8-1.4-.1-2.8.8-3.5.8-.7 0-1.8-.8-3-.8-1.5 0-2.9.9-3.7 2.3-1.6 2.7-.4 6.8 1.1 9 .8 1.1 1.7 2.3 2.9 2.3 1.1 0 1.6-.7 3-.7s1.8.7 3 .7c1.2 0 2-1.1 2.8-2.2.9-1.3 1.2-2.5 1.2-2.6-.1 0-2.4-.9-2.4-3.5zM14.2 5.9c.6-.8 1.1-1.9 1-3-.9 0-2 .6-2.7 1.4-.6.7-1.1 1.8-1 2.9 1.1.1 2.1-.5 2.7-1.3z"/></svg>
        iOS 即将上线
      </a>
    </div>
    <p class="hint">Android 需允许"安装未知来源应用" · 版本 1.0.0</p>
  </section>

  <section class="section">
    <p class="kicker">01 / FLOW</p>
    <h2>四步，把声音变成资产</h2>
    <p class="sub">从录制到上链，全程在你的设备上完成</p>
    <div class="steps">
      <div class="step"><h3>录制声音</h3><p>录下此刻的声音，生成专属可视化图形。</p></div>
      <div class="step"><h3>写入声片</h3><p>靠近 NFC 卡片，把声音写入实体声片。</p></div>
      <div class="step"><h3>上链存证</h3><p>一键 mint 到 Injective 链，获得唯一 Token。</p></div>
      <div class="step"><h3>收藏传播</h3><p>声片可被他人触碰领取，传播链永久可查。</p></div>
    </div>
  </section>

  <section class="section">
    <p class="kicker">02 / WHY</p>
    <h2>为什么是 SoundPola</h2>
    <p class="sub">声音是最容易被遗忘的记忆载体</p>
    <div class="grid">
      <div class="card">
        <div class="card-top"><p class="card-num">01</p><svg viewBox="0 0 64 64" aria-hidden="true"><circle cx="32" cy="32" r="22" fill="none" stroke="rgba(99,224,203,0.35)" stroke-width="1"/><circle cx="32" cy="32" r="10" fill="none" stroke="#63E0CB" stroke-width="1"/><circle cx="32" cy="10" r="2" fill="#63E0CB"/></svg></div>
        <h3>所见即所声</h3><p>每段声音生成独一无二的万花筒可视化，声纹即指纹。</p><div class="card-line"></div>
      </div>
      <div class="card">
        <div class="card-top"><p class="card-num">02</p><svg viewBox="0 0 64 64" aria-hidden="true"><rect x="12" y="18" width="40" height="28" fill="none" stroke="rgba(99,224,203,0.35)" stroke-width="1"/><text x="20" y="36" fill="#63E0CB" font-size="10" font-family="monospace">#0F2A</text></svg></div>
        <h3>链上确权</h3><p>ERC-721 标准铸造，所有权归你的钱包，平台无法篡改。</p><div class="card-line"></div>
      </div>
      <div class="card">
        <div class="card-top"><p class="card-num">03</p><svg viewBox="0 0 64 64" aria-hidden="true"><circle cx="32" cy="32" r="18" fill="none" stroke="rgba(99,224,203,0.35)" stroke-width="1"/><circle cx="32" cy="32" r="5" fill="#63E0CB"/><path d="M32 8v8M32 48v8M8 32h8M48 32h8" stroke="#63E0CB" stroke-width="1"/></svg></div>
        <h3>一触即达</h3><p>NFC 声片无需扫码，手机靠近即可试听与领取。</p><div class="card-line"></div>
      </div>
      <div class="card">
        <div class="card-top"><p class="card-num">04</p><svg viewBox="0 0 64 64" aria-hidden="true"><path d="M10 46 L24 28 L34 38 L54 14" fill="none" stroke="#63E0CB" stroke-width="1.25"/><circle cx="54" cy="14" r="2.5" fill="#63E0CB"/><circle cx="24" cy="28" r="2" fill="rgba(244,246,245,0.7)"/></svg></div>
        <h3>传播可溯</h3><p>每一次领取都生成新版本，传播路径链上永久存证。</p><div class="card-line"></div>
      </div>
    </div>
  </section>

  <section class="section" id="download">
    <p class="kicker">03 / DOWNLOAD</p>
    <h2>现在开始收藏声音</h2>
    <p class="sub">下载 SoundPola，录下第一段声音记忆</p>
    <div class="download-box">
      <span class="corner tl"></span><span class="corner tr"></span><span class="corner bl"></span><span class="corner br"></span>
      <p class="term-label">DOWNLOAD TERMINAL</p>
      <div class="btn-row">
        <a class="btn btn-primary" href="https://babelbeast.com/app-release.apk">
          <svg viewBox="0 0 24 24"><path d="M5 20h14v-2H5v2zM19 9h-4V3H9v6H5l7 7 7-7z"/></svg>
          下载 Android APK
          <span class="reveal">READY TO DOWNLOAD</span>
        </a>
      </div>
      <p class="dl-status"><span class="dot"></span>DOWNLOAD READY</p>
      <p class="qr-note">iOS 版本正在审核中，敬请期待</p>
    </div>
  </section>

  <footer>
    <p>SoundPola &copy; 2026 · <span class="mono">声音记忆 / 实体声片 / 链上存证</span> · <span class="mono">VERSION 1.0.0 &middot; ANDROID AVAILABLE</span></p>
  </footer>
</div>
<script>(function(){var n=document.getElementById("nav");addEventListener("scroll",function(){n.classList.toggle("scrolled",scrollY>12)},{passive:true});})();</script>
</body>
</html>"""
