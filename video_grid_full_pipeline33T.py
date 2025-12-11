#!/usr/bin/env python
# video_grid_full_pipeline33T.py
#
# 33T = 33S but with:
#   - REAL collage embedded inside the (decrypted) HTML as a data-URI image
#   - PUBLIC OG image is a blurred "cover" PNG only
#   - Only HTML_B + cover PNG are pushed to git
#
# Usage examples:
#   python video_grid_full_pipeline33T.py urls.csv 3
#   python video_grid_full_pipeline33T.py urls.csv 3 screenshot
#   python video_grid_full_pipeline33T.py urls.csv 3 screenshot ogmax=60 aes
#   python video_grid_full_pipeline33T.py urls.csv 3 aes
#
# Requires: cryptography, selenium, pillow, requests
#   pip install cryptography selenium pillow requests

import sys
import csv
import re
import base64
import textwrap
import subprocess
import time
import os
import json
import math
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime
from io import BytesIO

import requests

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import WebDriverException

from PIL import Image, ImageFilter  # for collages

import getpass  # for interactive password entry

# AES / PBKDF2 (pip install cryptography)
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


# --------------------------------------------------------------------
# CONFIG – Selenium profile (EXACTLY like Minimal_Selenium_example_08/09)
# --------------------------------------------------------------------
SELENIUM_USER_DATA_DIR = r"C:\Users\karma\AppData\Local\Google\Chrome\User Data Selenium"
SELENIUM_PROFILE_DIR   = "Default"
CHROMEDRIVER_EXE       = r"C:\Tools\chromedriver\chromedriver.exe"

# public base for GitHub Pages  (VD3)
PUBLIC_BASE_URL = "https://agatasiwekjendobrezakopane4eve.github.io/VD3"

# How long to wait after loading each page before screenshot (seconds)
WAIT_BEFORE_SCREENSHOT = 8

# vd3 paranoia: we DO publish a cover OG image by default
ENABLE_PUBLIC_OG_COLLAGE = True


# ------------------ platform detection helpers ------------------ #

def detect_platform(url: str) -> str:
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    if "rumble.com" in u:
        return "rumble"
    if "vimeo.com" in u:
        return "vimeo"
    if "tiktok.com" in u:
        return "tiktok"
    if "instagram.com" in u or "instagr.am" in u:
        return "instagram"
    if "twitter.com" in u or "x.com" in u:
        return "twitter"
    if "facebook.com" in u:
        return "facebook"
    return "other"


def extract_youtube_id(url: str):
    m = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_\-]{6,})", url)
    return m.group(1) if m else None


def extract_vimeo_id(url: str):
    m = re.search(r"vimeo\.com/(\d+)", url)
    return m.group(1) if m else None


def extract_rumble_id(url: str):
    path = url.split("?")[0]
    slug = path.rstrip("/").split("/")[-1]
    if "-" in slug:
        slug = slug.split("-")[0]
    return slug or None


# ------------------ Selenium driver ------------------ #

def get_driver():
    """
    EXACT same driver/profile wiring as Minimal_Selenium_example_08/09.
    Uses:
      SELENIUM_USER_DATA_DIR = C:\\Users\\karma\\AppData\\Local\\Google\\Chrome\\User Data Selenium
      SELENIUM_PROFILE_DIR   = Default
      CHROMEDRIVER_EXE       = C:\\Tools\\chromedriver\\chromedriver.exe
    """

    options = webdriver.ChromeOptions()
    options.add_argument(fr"--user-data-dir={SELENIUM_USER_DATA_DIR}")
    options.add_argument(f"--profile-directory={SELENIUM_PROFILE_DIR}")
    options.add_argument("--start-maximized")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")

    service = Service(CHROMEDRIVER_EXE)

    try:
        driver = webdriver.Chrome(service=service, options=options)
        print("✅ Selenium Chrome started with Selenium profile.")
    except WebDriverException as e:
        print("Error starting Chrome:", e)
        raise

    return driver


def safe_mkdir(path: str):
    os.makedirs(path, exist_ok=True)


# ------------------ thumbnails (lightweight HTTP oembed) ------------------ #

def safe_get_json(url: str, params: dict | None = None, timeout: int = 8):
    try:
        r = requests.get(url, params=params, timeout=timeout)
        if r.ok:
            return r.json()
    except Exception:
        return None
    return None


def get_lightweight_thumb(platform: str, url: str) -> str | None:
    """
    Try to get a thumbnail URL without a browser.
    Returns a URL string or None.
    """
    try:
        if platform == "youtube":
            vid = extract_youtube_id(url)
            if vid:
                return f"https://img.youtube.com/vi/{vid}/hqdefault.jpg"

        elif platform == "vimeo":
            data = safe_get_json("https://vimeo.com/api/oembed.json", params={"url": url})
            if data and "thumbnail_url" in data:
                return data["thumbnail_url"]

        elif platform == "tiktok":
            data = safe_get_json("https://www.tiktok.com/oembed", params={"url": url})
            if data and "thumbnail_url" in data:
                return data["thumbnail_url"]

        elif platform == "instagram":
            data = safe_get_json("https://www.instagram.com/oembed", params={"url": url})
            if data and "thumbnail_url" in data:
                return data["thumbnail_url"]

        elif platform == "twitter":
            data = safe_get_json("https://publish.twitter.com/oembed", params={"url": url})
            thumb = data.get("thumbnail_url") if data else None
            return thumb

        elif platform == "rumble":
            data = safe_get_json("https://rumble.com/oembed", params={"url": url, "format": "json"})
            if data and "thumbnail_url" in data:
                return data["thumbnail_url"]

    except Exception:
        return None

    return None


# ------------------ Selenium screenshot engine ------------------ #

def capture_screenshots_selenium(urls: list[str], out_dir: Path) -> dict[str, str]:
    """
    Use Selenium + Chrome (Minimal_Selenium_example_09 profile) to capture viewport screenshots.
    Returns {url: 'thumbs/shot_001.png'} for URLs that succeeded.
    """
    mapping: dict[str, str] = {}
    if not urls:
        return mapping

    safe_mkdir(str(out_dir))

    driver = get_driver()

    try:
        try:
            driver.set_window_size(1365, 768)
        except Exception:
            pass

        total = len(urls)
        for idx, url in enumerate(urls, start=1):
            print(f"[{idx}/{total}] 📸 (Selenium) {url}")
            try:
                driver.get(url)
                time.sleep(WAIT_BEFORE_SCREENSHOT)

                shot_name = f"shot_{idx:03d}.png"
                full_path = out_dir / shot_name
                rel_src = f"thumbs/{shot_name}"

                ok = driver.save_screenshot(str(full_path))
                if ok:
                    mapping[url] = rel_src.replace("\\", "/")
                    print(f"   📸 Saved Selenium screenshot → {full_path}")
                else:
                    print("   ⚠️ save_screenshot() returned False")
            except WebDriverException as e:
                print(f"   ❌ WebDriver error with {url}: {e}")
            except Exception as e:
                print(f"   ❌ Unexpected error with {url}: {e}")
    finally:
        print("   Closing Selenium browser…")
        try:
            driver.quit()
        except Exception:
            pass

    return mapping


def cleanup_screenshot_files(screenshot_rel_map: dict[str, str], repo_root: Path) -> None:
    """
    Delete local screenshot PNGs (thumbs) used for data-URIs.
    """
    paths: set[Path] = set()
    for rel in screenshot_rel_map.values():
        p = Path(rel)
        if not p.is_absolute():
            p = repo_root / p
        paths.add(p)

    for p in paths:
        try:
            p.unlink()
            print(f"🧹 Deleted screenshot: {p}")
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"⚠️ Failed to delete screenshot {p}: {e}")

    # try removing thumbs/ directory if empty
    thumbs_dir = repo_root / "thumbs"
    try:
        thumbs_dir.rmdir()
        print(f"🧹 Removed empty directory: {thumbs_dir}")
    except OSError:
        pass


# ------------------ data-URI helpers ------------------ #

def _guess_image_mime(src: str) -> str:
    s = src.lower()
    if s.endswith(".jpg") or s.endswith(".jpeg"):
        return "image/jpeg"
    if s.endswith(".gif"):
        return "image/gif"
    if s.endswith(".webp"):
        return "image/webp"
    return "image/png"


def src_to_data_uri(src: str, repo_root: Path) -> str | None:
    """
    Convert a local path or remote URL to a data:image/...;base64,... URI.
    Returns None if something fails.
    """
    try:
        if src.lower().startswith("http://") or src.lower().startswith("https://"):
            r = requests.get(src, timeout=10)
            if not r.ok:
                return None
            raw = r.content
            mime = _guess_image_mime(src)
        else:
            p = Path(src)
            if not p.is_absolute():
                p = repo_root / p
            if not p.exists():
                return None
            raw = p.read_bytes()
            mime = _guess_image_mime(p.name)

        b64 = base64.b64encode(raw).decode("ascii")
        return f"data:{mime};base64,{b64}"
    except Exception as e:
        print(f"⚠️ Failed to convert '{src}' to data URI: {e}")
        return None


# ------------------ tiles & fallback HTML ------------------ #

@dataclass
class VideoTile:
    url: str
    platform: str
    embed_html: str | None
    open_url: str
    thumb_src: str | None          # data-URI string for the grid
    thumb_src_collage: str | None  # original src (local path or URL) for collages


def build_embed(platform: str, url: str) -> str | None:
    """
    Return an <iframe> HTML snippet for platforms that actually allow it.
    For blocked platforms (Twitter/X, TikTok, Instagram, Rumble) we return None
    and rely on the screenshot/thumbnail card instead.
    """
    if platform == "youtube":
        vid = extract_youtube_id(url)
        if not vid:
            return None
        embed_url = f"https://www.youtube.com/embed/{vid}"
        return f"""
<div class="embed-wrapper">
  <iframe
    src="{embed_url}"
    title="YouTube video"
    frameborder="0"
    allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
    allowfullscreen>
  </iframe>
</div>
""".strip()

    if platform == "vimeo":
        vid = extract_vimeo_id(url)
        if not vid:
            return None
        embed_url = f"https://player.vimeo.com/video/{vid}"
        return f"""
<div class="embed-wrapper">
  <iframe
    src="{embed_url}"
    title="Vimeo video"
    frameborder="0"
    allow="autoplay; fullscreen; picture-in-picture"
    allowfullscreen>
  </iframe>
</div>
""".strip()

    if platform == "rumble":
        return None  # screenshot + fallback card only

    if platform == "facebook":
        from urllib.parse import quote_plus
        href = quote_plus(url)
        embed_url = f"https://www.facebook.com/plugins/video.php?href={href}&show_text=false"
        return f"""
<div class="embed-wrapper">
  <iframe
    src="{embed_url}"
    title="Facebook video"
    frameborder="0"
    allow="autoplay; clipboard-write; encrypted-media; picture-in-picture"
    allowfullscreen>
  </iframe>
</div>
""".strip()

    # Twitter / TikTok / Instagram / other -> fallback card only
    return None


PLATFORM_LABELS = {
    "youtube": "YouTube",
    "rumble": "Rumble",
    "vimeo": "Vimeo",
    "twitter": "Twitter / X",
    "tiktok": "TikTok",
    "instagram": "Instagram",
    "facebook": "Facebook",
    "other": "Video",
}


def build_fallback_card(tile: VideoTile) -> str:
    label = PLATFORM_LABELS.get(tile.platform, "Video")
    sub = f"Open on {label}"
    thumb_img = ""
    if tile.thumb_src:
        thumb_img = f"""
  <div class="fallback-thumb-wrapper">
    <img src="{tile.thumb_src}" alt="{label} thumbnail" loading="lazy">
  </div>
""".rstrip()

    return f"""
<div class="fallback-card" onclick="window.open('{tile.open_url}', '_blank')">
  {thumb_img}
  <div class="fallback-text">
    <div class="fallback-title">{label}</div>
    <div class="fallback-sub">{sub}</div>
    <div class="fallback-url">{tile.open_url}</div>
  </div>
</div>
""".strip()


# ------------------ collage builder for 33T ------------------ #

def download_image_to_pil(src: str, root: Path) -> Image.Image | None:
    """
    src may be:
      - relative path like 'thumbs/shot_001.png'
      - absolute 'C:\\...\\thumbs\\shot_001.png'
      - remote URL (http/https)
    """
    try:
        if src.lower().startswith("http://") or src.lower().startswith("https://"):
            r = requests.get(src, timeout=8)
            if not r.ok:
                return None
            return Image.open(BytesIO(r.content)).convert("RGB")
        # local path
        p = Path(src)
        if not p.is_absolute():
            p = root / p
        if not p.exists():
            return None
        return Image.open(p).convert("RGB")
    except Exception:
        return None


def build_collages_for_33T(
    tiles: list[VideoTile],
    repo_root: Path,
    max_cells: int,
    base_w: int,
    base_h: int,
    timestamp: str,
) -> tuple[str | None, Path | None]:
    """
    Build:
      - REAL collage in memory -> return as data-URI string
      - COVER collage (blurred) -> saved to PNG -> returned Path
    Only uses up to max_cells thumbs.
    """
    if max_cells <= 0:
        print("🧩 Collage disabled (max_cells <= 0).")
        return None, None

    # Use the original sources (local PNGs or remote URLs)
    thumb_srcs: list[str] = []
    for t in tiles:
        if t.thumb_src_collage:
            thumb_srcs.append(t.thumb_src_collage)
            if len(thumb_srcs) >= max_cells:
                break

    if not thumb_srcs:
        print("🧩 No thumbnails available for collages.")
        return None, None

    # load PIL images
    imgs: list[Image.Image] = []
    for src in thumb_srcs:
        im = download_image_to_pil(src, repo_root)
        if im is not None:
            imgs.append(im)
    if not imgs:
        print("🧩 Failed to load any thumbs for collages.")
        return None, None

    n = len(imgs)

    cols = max(3, min(12, int(math.ceil(math.sqrt(n * base_w / base_h)))))
    rows = int(math.ceil(n / cols))

    cell_w = base_w // cols
    cell_h = base_h // rows

    print(
        f"🧩 Building REAL collage with {n} thumbs "
        f"({cols} cols × {rows} rows, cells {cell_w}×{cell_h}px, "
        f"image {base_w}×{base_h}px)"
    )

    collage = Image.new("RGB", (base_w, base_h), (0, 0, 0))

    for idx, im in enumerate(imgs):
        r = idx // cols
        c = idx % cols
        if r >= rows:
            break

        im_ratio = im.width / im.height
        cell_ratio = cell_w / cell_h
        if im_ratio > cell_ratio:
            new_w = cell_w
            new_h = int(cell_w / im_ratio)
        else:
            new_h = cell_h
            new_w = int(cell_h * im_ratio)
        resized = im.resize((new_w, new_h), Image.LANCZOS)

        x = c * cell_w + (cell_w - new_w) // 2
        y = r * cell_h + (cell_h - new_h) // 2
        collage.paste(resized, (x, y))

    # 1) REAL collage -> data URI (for inside decrypted HTML only)
    buf = BytesIO()
    collage.save(buf, format="PNG")
    raw_bytes = buf.getvalue()
    real_b64 = base64.b64encode(raw_bytes).decode("ascii")
    real_data_uri = f"data:image/png;base64,{real_b64}"
    print("🧬 Real collage encoded as data URI for internal banner.")

    # 2) COVER collage -> blur the real one and save as PNG
    cover = collage.filter(ImageFilter.GaussianBlur(radius=9))
    cover_name = f"grid-cover-{timestamp}.png"
    cover_path = repo_root / cover_name
    cover.save(cover_path, format="PNG")
    print(f"🖼  Cover OG image saved to: {cover_path}")

    return real_data_uri, cover_path


# ------------------ HTML page builder ------------------ #

def build_page_html(
    tiles: list[VideoTile],
    cols: int | None,
    screenshot_mode: bool,
    og_image_rel: str | None,
    real_collage_data_uri: str | None,
    timestamp: str,
) -> str:
    layout_label = (
        f"fixed {cols} columns on wide screens"
        if cols and cols > 0 else
        "auto-fill, many tiles per row"
    )
    if screenshot_mode:
        layout_label += " • with screenshots"

    open_all_js_array = ",\n    ".join(f"'{t.open_url}'" for t in tiles)

    tiles_html_parts: list[str] = []
    for t in tiles:
        inner_parts = []
        if t.embed_html:
            inner_parts.append(t.embed_html)
        if not t.embed_html or t.platform in ("twitter", "tiktok", "instagram", "rumble"):
            inner_parts.append(build_fallback_card(t))
        tile_html = f"""
<div class="tile">
  {''.join(inner_parts)}
</div>
""".strip()
        tiles_html_parts.append(tile_html)

    tiles_html = "\n".join(tiles_html_parts)

    if cols and cols > 0:
        cols_css = f"grid-template-columns: repeat({cols}, minmax(260px, 1fr));"
    else:
        cols_css = "grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));"

    og_meta = ""
    if og_image_rel:
        og_url = f"{PUBLIC_BASE_URL}/{og_image_rel}"
        og_meta = f"""
  <meta property="og:title" content="Watch These Videos Together">
  <meta property="og:description" content="Multi-platform video grid – open to watch all clips together.">
  <meta property="og:type" content="website">
  <meta property="og:image" content="{og_url}">
  <meta property="og:image:width" content="1200">
  <meta property="og:image:height" content="630">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="Watch These Videos Together">
  <meta name="twitter:description" content="Multi-platform video grid – open to watch all clips together.">
  <meta name="twitter:image" content="{og_url}">
""".rstrip()

    collage_banner_html = ""
    if real_collage_data_uri:
        collage_banner_html = f"""
    <div class="collage-banner">
      <img src="{real_collage_data_uri}" alt="Video collage" loading="lazy">
    </div>
""".rstrip()

    css = f"""
body {{
  margin: 0;
  padding: 0;
  background: #000;
  color: #fff;
  font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}}
.page-wrapper {{
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}}
.header {{
  text-align: center;
  padding: 16px 8px 8px;
}}
.header h1 {{
  margin: 0;
  font-size: 24px;
}}
.header .subtitle {{
  margin-top: 4px;
  font-size: 13px;
  opacity: 0.8;
}}
.collage-banner {{
  margin-top: 12px;
}}
.collage-banner img {{
  max-width: 100%;
  height: auto;
  border-radius: 10px;
  box-shadow: 0 0 10px rgba(0,0,0,0.8);
}}
#openAllBtn {{
  margin-top: 12px;
  padding: 8px 16px;
  border-radius: 999px;
  border: none;
  background: #f90;
  color: #000;
  font-weight: 600;
  cursor: pointer;
}}
#openAllBtn:hover {{
  filter: brightness(1.1);
}}
.layout-note {{
  margin-top: 6px;
  font-size: 12px;
  opacity: 0.7;
}}
.grid {{
  flex: 1;
  display: grid;
  {cols_css}
  gap: 16px;
  padding: 16px;
  box-sizing: border-box;
}}
.tile {{
  background: #111;
  border-radius: 10px;
  overflow: hidden;
  box-shadow: 0 0 6px rgba(0,0,0,0.6);
  display: flex;
  flex-direction: column;
  justify-content: flex-start;
}}
.embed-wrapper {{
  position: relative;
  width: 100%;
  padding-top: 56.25%; /* 16:9 */
  background: #000;
}}
.embed-wrapper iframe {{
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
}}
.fallback-card {{
  flex: 1;
  display: flex;
  flex-direction: row;
  align-items: center;
  padding: 12px 14px;
  cursor: pointer;
  border-top: 1px solid #222;
}}
.fallback-card:hover {{
  background: #181818;
}}
.fallback-thumb-wrapper {{
  flex: 0 0 auto;
  margin-right: 14px;
}}
.fallback-thumb-wrapper img {{
  display: block;
  max-width: 220px;
  max-height: 140px;
  object-fit: cover;
  border-radius: 8px;
}}
.fallback-text {{
  flex: 1;
  min-width: 0;
}}
.fallback-title {{
  font-size: 14px;
  font-weight: 600;
}}
.fallback-sub {{
  font-size: 12px;
  opacity: 0.8;
  margin-top: 2px;
}}
.fallback-url {{
  font-size: 11px;
  opacity: 0.7;
  margin-top: 4px;
  word-break: break-all;
}}
.footer {{
  text-align: center;
  font-size: 11px;
  padding: 8px;
  opacity: 0.7;
}}
@media (max-width: 768px) {{
  .grid {{
    grid-template-columns: 1fr;
  }}
}}
"""

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Watch These Videos Together</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
{og_meta}
  <style>
  {css}
  </style>
</head>
<body>
<div class="page-wrapper">
  <div class="header">
    <h1>Watch These Videos Together</h1>
    <div class="subtitle">Grid · all visible at once · click any to play.</div>
{collage_banner_html}
    <button id="openAllBtn">Open all videos in new tabs</button>
    <div class="layout-note">Layout: {layout_label}</div>
  </div>
  <div class="grid">
    {tiles_html}
  </div>
  <div class="footer">
    Built by me 🧠 — all videos from YouTube or other embeds. [{timestamp}]
  </div>
</div>
<script>
  const allVideoUrls = [
    {open_all_js_array}
  ];
  document.getElementById('openAllBtn').addEventListener('click', () => {{
    const delay = 500;
    allVideoUrls.forEach((u, idx) => {{
      setTimeout(() => {{
        try {{
          window.open(u, '_blank');
        }} catch (e) {{}}
      }}, idx * delay);
    }});
  }});
</script>
</body>
</html>
"""
    return html


# ------------------ obfuscation (base64 wrapper) ------------------ #

def make_obfuscated_wrapper(html: str) -> str:
    b64 = base64.b64encode(html.encode("utf-8")).decode("ascii")
    chunks = textwrap.wrap(b64, 4000)
    chunks_js = ",\n    ".join(f'"{c}"' for c in chunks)

    wrapper = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>loading...</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body style="background:#000;color:#fff;font-family:system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <noscript>This page requires JavaScript to display its content.</noscript>
  <script>
  (function() {{
    var chunks = [
      {chunks_js}
    ];
    function b64ToUtf8(str) {{
      try {{
        return decodeURIComponent(escape(window.atob(str)));
      }} catch (e) {{
        return atob(str);
      }}
    }}
    var joined = chunks.join("");
    var html = b64ToUtf8(joined);
    document.open();
    document.write(html);
    document.close();
  }})();
  </script>
</body>
</html>
"""
    return wrapper


# ------------------ AES-GCM encryption helpers ------------------ #

def encrypt_html_aes_gcm(html: str, password: str, iterations: int = 100_000):
    """
    Encrypt HTML using AES-GCM (256-bit) with a key derived from the password
    via PBKDF2-HMAC-SHA256.
    Returns (salt_b64, iv_b64, ciphertext_b64, iterations).
    """
    password_bytes = password.encode("utf-8")

    salt = os.urandom(16)  # PBKDF2 salt
    iv = os.urandom(12)    # AES-GCM IV

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=iterations,
        backend=default_backend()
    )
    key = kdf.derive(password_bytes)
    aesgcm = AESGCM(key)

    ciphertext = aesgcm.encrypt(iv, html.encode("utf-8"), None)

    salt_b64 = base64.b64encode(salt).decode("ascii")
    iv_b64 = base64.b64encode(iv).decode("ascii")
    ct_b64 = base64.b64encode(ciphertext).decode("ascii")

    return salt_b64, iv_b64, ct_b64, iterations


def make_aes_protected_wrapper(html: str,
                               password: str,
                               og_image_rel: str | None = None) -> str:
    """
    Wrap the HTML in an AES-GCM-encrypted loader.
    Final page is a password prompt; decryption happens client-side
    using Web Crypto API.

    og_image_rel (if provided) will be exposed via OG meta tags so
    Facebook/LinkedIn/etc can show the COVER collage even though
    the body is just the password box.
    """
    salt_b64, iv_b64, ct_b64, iterations = encrypt_html_aes_gcm(html, password)

    og_meta = ""
    if og_image_rel:
        og_url = f"{PUBLIC_BASE_URL}/{og_image_rel}"
        og_meta = f"""
  <meta property="og:title" content="Watch These Videos Together">
  <meta property="og:description" content="Multi-platform video grid – click to unlock and watch.">
  <meta property="og:type" content="website">
  <meta property="og:image" content="{og_url}">
  <meta property="og:image:width" content="1200">
  <meta property="og:image:height" content="630">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="Watch These Videos Together">
  <meta name="twitter:description" content="Multi-platform video grid – click to unlock and watch.">
  <meta name="twitter:image" content="{og_url}">
""".rstrip()

    wrapper = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Protected page</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
{og_meta}
  <style>
    body {{
      margin: 0;
      padding: 0;
      background: #000;
      color: #fff;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
    }}
    .box {{
      background: #111;
      padding: 24px 28px;
      border-radius: 10px;
      box-shadow: 0 0 12px rgba(0,0,0,0.8);
      max-width: 360px;
      width: 100%;
    }}
    h1 {{
      margin: 0 0 12px;
      font-size: 18px;
      text-align: center;
    }}
    p {{
      margin: 0 0 16px;
      font-size: 13px;
      opacity: 0.8;
      text-align: center;
    }}
    label {{
      display: block;
      font-size: 12px;
      margin-bottom: 4px;
    }}
    input[type="password"] {{
      width: 100%;
      box-sizing: border-box;
      padding: 8px 10px;
      border-radius: 6px;
      border: 1px solid #333;
      background: #000;
      color: #fff;
      font-size: 14px;
      margin-bottom: 12px;
    }}
    button {{
      width: 100%;
      padding: 8px 10px;
      border-radius: 999px;
      border: none;
      background: #f90;
      color: #000;
      font-weight: 600;
      cursor: pointer;
      font-size: 14px;
    }}
    button:hover {{
      filter: brightness(1.1);
    }}
    .error {{
      margin-top: 10px;
      font-size: 12px;
      color: #f77;
      text-align: center;
      min-height: 1em;
    }}
  </style>
</head>
<body>
  <div class="box">
    <h1>Protected page</h1>
    <p>Enter the password to unlock the content.</p>
    <form id="unlockForm">
      <label for="pw">Password</label>
      <input id="pw" type="password" autocomplete="current-password" autofocus>
      <button type="submit">Unlock</button>
      <div id="error" class="error"></div>
    </form>
  </div>
  <script>
    const SALT_B64 = "{salt_b64}";
    const IV_B64 = "{iv_b64}";
    const CT_B64 = "{ct_b64}";
    const PBKDF2_ITERATIONS = {iterations};

    function base64ToArrayBuffer(b64) {{
      const binary = atob(b64);
      const len = binary.length;
      const bytes = new Uint8Array(len);
      for (let i = 0; i < len; i++) {{
        bytes[i] = binary.charCodeAt(i);
      }}
      return bytes.buffer;
    }}

    function stringToArrayBuffer(str) {{
      return new TextEncoder().encode(str);
    }}

    function arrayBufferToString(buf) {{
      return new TextDecoder().decode(buf);
    }}

    async function unlockWithPassword(pw) {{
      const errorEl = document.getElementById('error');
      errorEl.textContent = "";
      try {{
        const pwBuf = stringToArrayBuffer(pw);
        const salt = base64ToArrayBuffer(SALT_B64);
        const iv = base64ToArrayBuffer(IV_B64);
        const ciphertext = base64ToArrayBuffer(CT_B64);

        const baseKey = await crypto.subtle.importKey(
          "raw",
          pwBuf,
          "PBKDF2",
          false,
          ["deriveKey"]
        );

        const aesKey = await crypto.subtle.deriveKey(
          {{
            name: "PBKDF2",
            salt: salt,
            iterations: PBKDF2_ITERATIONS,
            hash: "SHA-256"
          }},
          baseKey,
          {{
            name: "AES-GCM",
            length: 256
          }},
          false,
          ["decrypt"]
        );

        const plaintextBuf = await crypto.subtle.decrypt(
          {{
            name: "AES-GCM",
            iv: new Uint8Array(base64ToArrayBuffer(IV_B64))
          }},
          aesKey,
          ciphertext
        );

        const html = arrayBufferToString(plaintextBuf);
        document.open();
        document.write(html);
        document.close();
      }} catch (e) {{
        console.error(e);
        errorEl.textContent = "Decryption failed. Wrong password.";
      }}
    }}

    document.getElementById('unlockForm').addEventListener('submit', function(e) {{
      e.preventDefault();
      const pw = (document.getElementById('pw').value || "").trim();
      if (!pw) {{
        document.getElementById('error').textContent = "Please enter a password.";
        return;
      }}
      unlockWithPassword(pw);
    }});
  </script>
</body>
</html>
"""
    return wrapper


# ------------------ git + GitHub Pages helpers ------------------ #

def run_git_commands(files: list[Path], commit_msg: str) -> None:
    paths_str = [str(f) for f in files]

    try:
        print("💻 Running git: add", " ".join(paths_str))
        subprocess.run(["git", "add", *paths_str], check=True)
    except Exception as e:
        print(f"⚠️ git add failed: {e}")
        return

    try:
        print("💻 Running git: commit -m", commit_msg)
        subprocess.run(["git", "commit", "-m", commit_msg], check=True)
    except Exception as e:
        print(f"⚠️ git commit failed (maybe nothing to commit?): {e}")
        return

    try:
        print("💻 Running git: push (attempt 1)")
        subprocess.run(["git", "push"], check=True)
    except Exception as e:
        print(f"⚠️ git push failed: {e}")


def wait_for_github(url: str, max_checks: int = 10, delay: int = 3) -> None:
    for i in range(1, max_checks + 1):
        try:
            r = requests.get(url, timeout=6)
            if r.ok:
                print(f"✅ GitHub is now serving: {url}")
                return
        except Exception:
            pass
        print(f"🌐 GitHub check {i}: {url}")
        time.sleep(delay)
    print("⚠️ Gave up waiting for GitHub (it may still appear shortly).")


# ------------------ main pipeline ------------------ #

def read_urls(input_path: Path) -> list[str]:
    if not input_path.exists():
        raise FileNotFoundError(input_path)

    urls: list[str] = []
    if input_path.suffix.lower() in (".csv", ".tsv"):
        with input_path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            for row in reader:
                if not row:
                    continue
                raw = row[0].strip()
                if raw:
                    urls.append(raw)
    else:
        with input_path.open("r", encoding="utf-8-sig") as f:
            for line in f:
                raw = line.strip()
                if raw:
                    urls.append(raw)
    return urls


def parse_args(argv: list[str]):
    if len(argv) < 2:
        print(
            "Usage: video_grid_full_pipeline33T.py "
            "input.csv [cols] [obf] [aes] [screenshot] [selenium] [ogmax=N]"
        )
    input_path = Path(argv[1])
    cols: int | None = None
    obfuscate = False
    aes_lock = False
    screenshot_mode = False
    engine = "selenium"  # only engine we support here
    og_max = 63  # default max tiles for collages

    for arg in argv[2:]:
        al = arg.lower()
        if al in ("obf", "obs", "obfs", "obfuscate"):
            obfuscate = True
        elif al in ("aes", "aeslock", "lock"):
            aes_lock = True
        elif al in ("screenshot", "screens", "shots"):
            screenshot_mode = True
        elif al in ("selenium", "sel", "s"):
            engine = "selenium"
        elif al.startswith("ogmax="):
            try:
                og_max = max(1, int(al.split("=", 1)[1]))
            except ValueError:
                pass
        else:
            try:
                cols = int(arg)
            except ValueError:
                pass

    if engine != "selenium":
        print("❌ Only 'selenium' engine is supported in 33T.")
        sys.exit(1)

    return input_path, cols, obfuscate, screenshot_mode, engine, og_max, aes_lock


def main():
    input_path, cols, obfuscate, screenshot_mode, engine, og_max, aes_lock = parse_args(sys.argv)
    print(
        "🎛  Layout:",
        "fixed %d columns on wide screens." % cols
        if cols
        else "auto-fill, min tile width, many per row.",
    )
    print("🕵️  Obfuscation:", "ON (base64 wrapper)" if obfuscate else "OFF")
    print("🔐  AES lock:", "ON (password required)" if aes_lock else "OFF")
    print("📸  Screenshots:", "ON" if screenshot_mode else "OFF")
    print("🔧  Screenshot engine:", engine)
    print("🧩  Collage tiles max:", og_max,
          "(PUBLIC DISABLED)" if not ENABLE_PUBLIC_OG_COLLAGE else "")

    # One timestamp for this run (used for filenames & cover collage)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 1) Read URLs
    urls_raw = read_urls(input_path)
    print("🔍 DEBUG: URLs read from CSV/text:")
    for i, u in enumerate(urls_raw, start=1):
        print(f"  {i}: '{u}'")
    print(f"Total lines with URLs: {len(urls_raw)}")

    # 2) De-duplicate while preserving order
    seen = set()
    urls: list[str] = []
    for u in urls_raw:
        if u not in seen:
            seen.add(u)
            urls.append(u)

    print(f"✅ Unique URLs used: {len(urls)}")

    # 3) Optional screenshots (Selenium, logged-in profile)
    screenshot_rel_map: dict[str, str] = {}
    if screenshot_mode:
        urls_needing_shots = [
            u for u in urls
            if detect_platform(u) in {"rumble", "twitter", "tiktok", "instagram"}
        ]
        platforms_for_shots = sorted({detect_platform(u) for u in urls_needing_shots})
        print(
            f"📸 Will take screenshots for {len(urls_needing_shots)} URLs "
            f"({', '.join(platforms_for_shots)})"
        )

        screenshot_rel_map = capture_screenshots_selenium(
            urls_needing_shots, Path("thumbs")
        )

    repo_root = Path(".")

    # 4) Build tiles (convert thumbs to data URIs, preserve original src for collages)
    tiles: list[VideoTile] = []
    for u in urls:
        platform = detect_platform(u)
        embed_html = build_embed(platform, u)

        # original thumbnail source (local Selenium shot or remote URL)
        raw_thumb = screenshot_rel_map.get(u) or get_lightweight_thumb(platform, u)
        if raw_thumb and "\\" in raw_thumb:
            raw_thumb = raw_thumb.replace("\\", "/")

        # data-URI used in the grid HTML
        data_uri = src_to_data_uri(raw_thumb, repo_root) if raw_thumb else None

        tiles.append(
            VideoTile(
                url=u,
                platform=platform,
                embed_html=embed_html,
                open_url=u,
                thumb_src=data_uri,
                thumb_src_collage=raw_thumb,
            )
        )

    # 5) Build collages (real data-URI + blurred cover PNG)
    real_collage_data_uri = None
    cover_path = None
    og_image_rel = None
    if ENABLE_PUBLIC_OG_COLLAGE and og_max > 0:
        real_collage_data_uri, cover_path = build_collages_for_33T(
            tiles,
            repo_root,
            max_cells=og_max,
            base_w=1200,
            base_h=630,
            timestamp=ts,
        )
        if cover_path:
            og_image_rel = cover_path.name

    # 6) We no longer need local screenshot files once data URIs are built
    if screenshot_mode and screenshot_rel_map:
        cleanup_screenshot_files(screenshot_rel_map, repo_root)

    # 7) Build full page HTML
    html = build_page_html(
        tiles,
        cols,
        screenshot_mode,
        og_image_rel,
        real_collage_data_uri,
        timestamp=ts,
    )

    # 8) Write A/B HTML files (A is local plain; B is pushed)
    out_dir = Path(".")
    out_name_a = f"grid_{ts}_A.html"
    out_name_b = f"grid_{ts}_B.html"

    html_a_path = out_dir / out_name_a
    html_b_path = out_dir / out_name_b

    html_a_path.write_text(html, encoding="utf-8")
    print(f"📝 HTML A written (local only): {html_a_path.name}")

    if aes_lock:
        print("🔐 AES lock enabled – prompting for password…")
        pw = getpass.getpass("Enter password to lock page (AES): ")
        if not pw:
            print("❌ Empty password not allowed for AES lock.")
            sys.exit(1)
        wrapper_html = make_aes_protected_wrapper(html, pw, og_image_rel)
        html_b_path.write_text(wrapper_html, encoding="utf-8")
    elif obfuscate:
        obf_html = make_obfuscated_wrapper(html)
        html_b_path.write_text(obf_html, encoding="utf-8")
    else:
        html_b_path.write_text(html, encoding="utf-8")

    print(f"📝 HTML B written (to be pushed): {html_b_path.name}")

    # 9) Git commit & push
    files_for_git: list[Path] = [html_b_path]
    if cover_path:
        files_for_git.append(cover_path)

    commit_msg = f"Add new video grid page (multi-platform v33T vd3, data-URI thumbs, AES/obf, cover collage) ({ts})"
    run_git_commands(files_for_git, commit_msg)

    public_url = f"{PUBLIC_BASE_URL}/{html_b_path.name}"

    print("\n⏳ Probing GitHub Pages for the new grid page…")
    wait_for_github(public_url)

    if cover_path:
        cover_url = f"{PUBLIC_BASE_URL}/{cover_path.name}"
        print("\n⏳ Probing GitHub Pages for the cover OG collage image…")
        wait_for_github(cover_url)

    print("\n✅ FULL PIPELINE COMPLETE (v33T selenium-only, internal real collage, public blurred cover).")
    print("Files pushed to GitHub:")
    print(f"  • {html_b_path.name}")
    if cover_path:
        print(f"  • {cover_path.name}")
    print("\nUse this URL in your Facebook / LinkedIn / secret drop (depending on obf/aes):")
    print(f"  {public_url}")


if __name__ == "__main__":
    main()
