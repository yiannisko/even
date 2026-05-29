import re
import base64
from urllib.parse import urlparse
from flask import Flask, request, Response, stream_with_context
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
SESSION = requests.Session()
SESSION.verify = False

HEADERS_TV = {
    "user-agent": "Mozilla/5.0 (WebOS; SmartTV)",
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
}

HEADERS_PROXY = {
    "User-Agent": "Mozilla/5.0 (compatible; Proxy/1.0)",
}

def get_self_url():
    return f"{request.scheme}://{request.host}"

def encode_url(url):
    return base64.urlsafe_b64encode(url.encode('utf-8')).decode('utf-8').rstrip('=')

def decode_url(encoded_url):
    padding = len(encoded_url) % 4
    if padding:
        encoded_url += "=" * (4 - padding)
    return base64.urlsafe_b64decode(encoded_url).decode('utf-8')

def process_m3u8(cdn_url):
    try:
        resp = SESSION.get(cdn_url, headers=HEADERS_PROXY, allow_redirects=True, stream=True, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"Fetch err: {e}")
        return Response("Stream offline", status=502)

    content_type = resp.headers.get("Content-Type", "").lower()
    is_m3u8 = "mpegurl" in content_type or urlparse(cdn_url).path.endswith(".m3u8")

    if is_m3u8:
        text = resp.content.decode('utf-8', errors='ignore')
        self_url = get_self_url()
        base_url = cdn_url[: cdn_url.rfind("/") + 1]
        lines = text.splitlines()
        rewritten = []
        has_version = False

        for line in lines:
            line = line.strip()
            if not line:
                continue
            if line.startswith("#EXT-X-VERSION"):
                has_version = True
                rewritten.append(line)
                continue
            if line.startswith("#"):
                rewritten.append(line)
                if line == "#EXTM3U" and not has_version:
                    rewritten.append("#EXT-X-VERSION:3")
                    has_version = True
                continue

            if not re.match(r"^https?://", line):
                if line.startswith("/"):
                    parsed = urlparse(cdn_url)
                    base_url = f"{parsed.scheme}://{parsed.netloc}"
                line = base_url + line

            encoded = encode_url(line)
            rewritten.append(f"{self_url}/cdn/{encoded}.m3u8")

        return Response(
            "\r\n".join(rewritten),
            content_type="application/vnd.apple.mpegurl",
            headers={"Access-Control-Allow-Origin": "*"}
        )

    def generate():
        for chunk in resp.iter_content(chunk_size=16384):
            if chunk:
                yield chunk

    ct = content_type if content_type else "video/mp2t"
    return Response(
        stream_with_context(generate()),
        content_type=ct,
        headers={"Access-Control-Allow-Origin": "*"}
    )

def resolve_and_play(live_id):
    stream_url = f"https://dlhd.pk/stream/stream-{live_id}.php"
    headers = {**HEADERS_TV, "referer": f"https://dlhd.pk/watch.php?id={live_id}"}
    
    try:
        r1 = SESSION.get(stream_url, headers=headers, timeout=10)
        r1.raise_for_status()

        iframe = re.search(r'iframe[^>]+src=["\']([^"\']+)["\']', r1.text, re.I)
        if not iframe:
            print("No iframe")
            return Response("Not found", status=404)

        data_url = iframe.group(1)
        r2 = SESSION.get(data_url, headers=headers, timeout=10)
        r2.raise_for_status()

        patterns = [
            r"source:\s*window\.atob\('([^']+)'\)",
            r"atob\('([^']+)'\)",
            r'file:\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
            r'source:\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
        ]

        link = None
        for pat in patterns:
            m = re.search(pat, r2.text)
            if m:
                try:
                    link = base64.b64decode(m.group(1)).decode("utf-8") if "atob" in pat else m.group(1)
                    break
                except Exception:
                    link = m.group(1)
                    break
        
        if link:
            return process_m3u8(link)
            
        print("No link")
        return Response("Not found", status=404)

    except Exception as e:
        print(f"Resolve err: {e}")
        return Response("Proxy err", status=502)

@app.route("/health", methods=["GET"])
def health():
    return Response("OK", status=200, content_type="text/plain")

@app.route("/cdn/<path:encoded_url>", methods=["GET"])
def cdn_route(encoded_url):
    try:
        if encoded_url.endswith(".m3u8"):
            encoded_url = encoded_url[:-5]
        url = decode_url(encoded_url)
        return process_m3u8(url)
    except Exception as e:
        print(f"Decode err: {e}")
        return Response("Decode err", status=400)

@app.route("/", methods=["GET"])
def index():
    live_id = request.args.get("ID")
    if live_id:
        return resolve_and_play(live_id)
    return Response("Usage: /<id>", status=400)

@app.route("/<live_id>", methods=["GET"])
def play_id_raw(live_id):
    if live_id.endswith(".m3u8"):
        live_id = live_id[:-5]
    return resolve_and_play(live_id)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000, debug=False)
