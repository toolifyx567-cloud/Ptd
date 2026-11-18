from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import requests, re, json, secrets, string
from bs4 import BeautifulSoup
from urllib.parse import quote

app = Flask(__name__)
CORS(app, expose_headers=["Content-Range", "Accept-Ranges",
                          "Content-Length", "Content-Disposition"])

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

DIRECT_MEDIA_RE = re.compile(r"\.(jpe?g|png|gif|webp|mp4)(\?|$)", re.I)
VIDEO_EXT_RE    = re.compile(r"\.mp4(\?|$)", re.I)
CHUNK = 64 * 1024


# ============================================================
#  HELPERS
# ============================================================
def _clean(u):
    if not u: return u
    return u.replace("\\u002F", "/").replace("\\/", "/").replace("&amp;", "&")


def _upgrade_image(url):
    url = _clean(url)
    if not url: return url
    return re.sub(r"(https?://i\.pinimg\.com/)(?:\d+x\d*|originals)/",
                  r"\1originals/", url)


def _thumb_from_image(url, size="236x"):
    url = _clean(url)
    if not url: return url
    return re.sub(r"(https?://i\.pinimg\.com/)(?:\d+x\d*|originals)/",
                  rf"\g<1>{size}/", url)


def _area(d):
    try: return int(d.get("width") or 0) * int(d.get("height") or 0)
    except Exception: return 0


def _random_suffix(length=6):
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _normalize_key(url):
    if not url: return ""
    url = re.sub(r"(i\.pinimg\.com/)(?:\d+x\d*|originals)/", r"\1_/", url)
    return url.split("?")[0]


# ============================================================
#  STORY PIN / IDEA PIN EXTRACTION  (NEW — this is the fix)
# ============================================================
def _extract_story_pin(node, out, depth=0):
    """
    Story Pins (multi-slide pins) store each slide in a `pages` array.
    Each page can contain its own `videos.video_list` and/or `images.orig`.
    We walk the JSON tree, and whenever we find a `pages` list whose items
    look like story pages, we pull every video and image out of them.
    """
    if depth > 30:
        return

    if isinstance(node, dict):
        # Look for story page arrays under several known keys
        for key in ("pages", "story_pin_data", "story_pin_pages"):
            pages = node.get(key)
            if isinstance(pages, list) and pages:
                for page in pages:
                    if not isinstance(page, dict):
                        continue
                    _extract_one_story_page(page, out)

        for val in node.values():
            _extract_story_pin(val, out, depth + 1)

    elif isinstance(node, list):
        for item in node:
            _extract_story_pin(item, out, depth + 1)


def _extract_one_story_page(page, out):
    """Extract videos and images from a single story slide."""
    # ---- videos ----
    v = page.get("videos")
    if isinstance(v, dict):
        vlist = v.get("video_list")
        if isinstance(vlist, dict):
            best = None
            for item in vlist.values():
                if not isinstance(item, dict):
                    continue
                u = _clean(item.get("url") or "")
                if not u.lower().endswith(".mp4"):
                    continue
                if best is None or _area(item) > _area(best):
                    best = {"url": u,
                            "width": item.get("width"),
                            "height": item.get("height")}
            if best:
                out.append({"type": "video", **best})

    # Some story pins put videos directly at page.video_list
    vlist2 = page.get("video_list")
    if isinstance(vlist2, dict):
        best = None
        for item in vlist2.values():
            if not isinstance(item, dict):
                continue
            u = _clean(item.get("url") or "")
            if not u.lower().endswith(".mp4"):
                continue
            if best is None or _area(item) > _area(best):
                best = {"url": u,
                        "width": item.get("width"),
                        "height": item.get("height")}
        if best:
            out.append({"type": "video", **best})

    # ---- images (cover/poster or static slide) ----
    imgs = page.get("images")
    if isinstance(imgs, dict):
        for key in ("orig", "originals"):
            img = imgs.get(key)
            if isinstance(img, dict) and img.get("url"):
                out.append({"type": "image",
                            "url": _upgrade_image(img["url"]),
                            "width": img.get("width"),
                            "height": img.get("height")})
                break


# ============================================================
#  FALLBACK COLLECTOR  (for normal pins)
# ============================================================
def _collect_media(node, out, depth=0):
    if depth > 30:
        return
    if isinstance(node, dict):
        v = node.get("videos")
        if isinstance(v, dict) and isinstance(v.get("video_list"), dict):
            best = None
            for item in v["video_list"].values():
                if not isinstance(item, dict):
                    continue
                u = _clean(item.get("url") or "")
                if not u.lower().endswith(".mp4"):
                    continue
                if best is None or _area(item) > _area(best):
                    best = {"url": u,
                            "width": item.get("width"),
                            "height": item.get("height")}
            if best:
                out.append({"type": "video", **best})

        imgs = node.get("images")
        if isinstance(imgs, dict):
            for key in ("orig", "originals"):
                img = imgs.get(key)
                if isinstance(img, dict) and img.get("url"):
                    out.append({"type": "image",
                                "url": _upgrade_image(img["url"]),
                                "width": img.get("width"),
                                "height": img.get("height")})
                    break

        for val in node.values():
            _collect_media(val, out, depth + 1)

    elif isinstance(node, list):
        for item in node:
            _collect_media(item, out, depth + 1)


def _dedupe(items):
    seen, result = set(), []
    for it in items:
        key = _normalize_key(it["url"])
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(it)
    return result


# ============================================================
#  EXTRACTION
# ============================================================
def extract_media(url):
    # ---------- Direct media URL ----------
    if DIRECT_MEDIA_RE.search(url):
        if VIDEO_EXT_RE.search(url):
            item = {"type": "video", "url": _clean(url), "index": 0,
                    "thumbnail_small": None}
        else:
            full = _upgrade_image(url)
            item = {"type": "image", "url": full, "index": 0,
                    "thumbnail": _thumb_from_image(url, "564x"),
                    "thumbnail_small": _thumb_from_image(url, "236x")}
        return {
            "success": True,
            "type": item["type"],
            "title": "Pinterest Media",
            "media": item["url"],
            "media_items": [item],
            "is_multi": False,
            "count": 1,
        }

    # ---------- Normal Pinterest page ----------
    r = SESSION.get(url, timeout=30, allow_redirects=True)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    html = r.text

    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.string.strip() if (soup.title and soup.title.string)
             else "Pinterest Download")

    story_items = []
    normal_items = []

    # 1) Preferred: parse __PWS_DATA__
    script = soup.find("script", id="__PWS_DATA__")
    if script and script.string:
        try:
            parsed = json.loads(script.string)
            # Try Story Pin extraction FIRST
            _extract_story_pin(parsed, story_items)
            # Also collect normal media (used as fallback)
            _collect_media(parsed, normal_items)
        except Exception:
            pass

    # Decide which set to use:
    #   - If the story walker found 2+ items, it's definitely a multi-slide pin
    #   - Otherwise use normal collector
    if len(story_items) >= 2:
        items = story_items
    else:
        items = normal_items if normal_items else story_items

    # 2) Fallback: every mp4 in raw HTML
    if not any(i["type"] == "video" for i in items):
        for m in re.finditer(
            r'"(?:contentUrl|url)"\s*:\s*"(https:[^"]+?\.mp4[^"]*)"', html
        ):
            items.append({"type": "video", "url": _clean(m.group(1))})

    # 3) Fallback: og:image
    if not any(i["type"] == "image" for i in items):
        m = (re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', html)
             or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image', html))
        if m:
            items.append({"type": "image", "url": _upgrade_image(m.group(1))})

    # 4) Last resort: any pinimg image
    if not items:
        for m in re.finditer(
            r'https://i\.pinimg\.com/(?:originals|\d+x\d*)/[^"\\\s]+?\.(?:jpe?g|png|webp)',
            html, re.I
        ):
            items.append({"type": "image", "url": _upgrade_image(m.group(0))})

    items = _dedupe(items)

    if not items:
        return {"success": False, "message": "Media not found"}

    # ---------- Enrich ----------
    for idx, it in enumerate(items):
        it["index"] = idx
        if it["type"] == "video":
            thumb = it.get("thumbnail") or it["url"]
            it.setdefault("thumbnail", _thumb_from_image(thumb, "564x"))
            it.setdefault("thumbnail_small", _thumb_from_image(thumb, "236x"))
        else:
            it.setdefault("thumbnail", _thumb_from_image(it["url"], "564x"))
            it.setdefault("thumbnail_small", _thumb_from_image(it["url"], "236x"))

    primary = items[0]
    is_multi = len(items) > 1

    return {
        "success": True,
        "type": primary["type"],
        "title": title,
        "media": primary["url"],
        "thumbnail": primary.get("thumbnail"),
        "thumbnail_small": primary.get("thumbnail_small"),
        "width": primary.get("width"),
        "height": primary.get("height"),

        "is_multi": is_multi,
        "media_items": items,
        "count": len(items),
    }


# ============================================================
#  ROUTES
# ============================================================
@app.route("/")
def home():
    return {"status": "ok"}


@app.route("/api/download", methods=["POST"])
def download():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"success": False, "message": "No URL"}), 400

    try:
        result = extract_media(url)
        if not result.get("success"):
            return jsonify(result), 404

        for it in result["media_items"]:
            it["stream_url"] = f"/api/stream?url={quote(it['url'], safe='')}"

        result["stream_url"] = result["media_items"][0]["stream_url"]
        return jsonify(result)

    except requests.exceptions.RequestException as e:
        return jsonify({"success": False, "message": f"Request failed: {e}"}), 502
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/stream", methods=["GET", "HEAD"])
def stream():
    target = request.args.get("url", "").strip()
    if not target:
        return jsonify({"success": False, "message": "No URL"}), 400

    if re.search(r"(pinterest\.[a-z.]+/pin/|pin\.it/)", target, re.I):
        r = extract_media(target)
        if not r.get("success"):
            return jsonify({"success": False, "message": r.get("message")}), 400
        target = r["media"]

    fwd = {"Accept-Encoding": "identity"}
    if request.headers.get("Range"):
        fwd["Range"] = request.headers["Range"]

    try:
        upstream = SESSION.get(target, headers=fwd, stream=True,
                               timeout=(10, 60), allow_redirects=True)
    except requests.exceptions.RequestException as e:
        return jsonify({"success": False, "message": str(e)}), 502

    headers = {
        "Content-Type": upstream.headers.get("Content-Type", "application/octet-stream"),
        "Accept-Ranges": "bytes",
        "Cache-Control": "public, max-age=86400",
    }
    for h in ("Content-Length", "Content-Range"):
        if h in upstream.headers:
            headers[h] = upstream.headers[h]

    is_video = bool(VIDEO_EXT_RE.search(target)) or \
               upstream.headers.get("Content-Type", "").startswith("video/")
    ext = "mp4" if is_video else "jpg"

    unique_name = f"toolifyx.netlify_{_random_suffix(6)}.{ext}"
    headers["Content-Disposition"] = f'attachment; filename="{unique_name}"'

    if request.method == "HEAD":
        upstream.close()
        return Response(status=upstream.status_code, headers=headers)

    def gen():
        try:
            for chunk in upstream.iter_content(chunk_size=CHUNK):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return Response(stream_with_context(gen()),
                    status=upstream.status_code, headers=headers)


if __name__ == "__main__":
    app.run(threaded=True)
