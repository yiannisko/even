import re
import base64
from urllib.parse import urlparse, quote
from flask import Flask, request, Response
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
SESSION = requests.Session()
SESSION.verify = False

def get_self_url():
    return f"{request.scheme}://{request.host}"

def encode_url(url):
    return base64.urlsafe_b64encode(url.encode('utf-8')).decode('utf-8').rstrip('=')

def decode_url(encoded_url):
    padding = 4 - (len(encoded_url) % 4)
    return base64.urlsafe_b64decode(encoded_url + "=" * padding).decode('utf-8')

def process_m3u8(cdn_url, req_referer=None):
    ref = req_referer or request.args.get('ref', 'https://dlhd.st/')
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": ref,
        "Origin": ref.rstrip("/"),
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site"
    }
    
    try:
        resp = requests.get(cdn_url, headers=headers, allow_redirects=True, stream=True, timeout=15, verify=False)
        resp.raise_for_status()
    except Exception:
        return Response("Stream offline", status=502)

    content_type = resp.headers.get("Content-Type", "")
    is_m3u8 = (
        "application/vnd.apple.mpegurl" in content_type
        or "application/x-mpegURL" in content_type
        or re.search(r"\.m3u8", cdn_url, re.I)
    )

    if is_m3u8:
        self_url = get_self_url()
        base_url = cdn_url[: cdn_url.rfind("/") + 1]
        text_content = resp.content.decode('utf-8', errors='ignore')
        lines = text_content.split("\n")
        rewritten = []
        safe_ref = quote(ref)

        for line in lines:
            line = line.strip()
            if not line:
                continue

            if line.startswith("#"):
                rewritten.append(line)
                continue

            if not re.match(r"^https?://", line):
                if line.startswith("/"):
                    parsed = urlparse(cdn_url)
                    base_url = f"{parsed.scheme}://{parsed.netloc}"
                line = base_url + line

            encoded = encode_url(line)
            rewritten.append(f"{self_url}/cdn/{encoded}?ref={safe_ref}")
            
        resp.close()

        return Response(
            "\n".join(rewritten),
            content_type="application/vnd.apple.mpegurl",
            headers={"Access-Control-Allow-Origin": "*"}
        )

    ct = content_type if content_type else "video/mp2t"
    resp_headers = {"Access-Control-Allow-Origin": "*"}
    if "Content-Length" in resp.headers:
        resp_headers["Content-Length"] = resp.headers["Content-Length"]

    def generate():
        try:
            for chunk in resp.iter_content(chunk_size=65536):
                yield chunk
        finally:
            resp.close()

    return Response(
        generate(),
        content_type=ct,
        headers=resp_headers
    )

def resolve_and_play(live_id):
    stream_url = f"https://dlhd.st/stream/stream-{live_id}.php"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": f"https://dlhd.st/watch.php?id={live_id}"
    }
    
    try:
        r1 = SESSION.get(stream_url, headers=headers, timeout=10)
        r1.raise_for_status()
        site = r1.text

        iframe = re.search(r'iframe[^>]+src=["\']([^"\']+)["\']', site, re.I)
        if not iframe:
            return Response("Not found", status=404)

        data_url = iframe.group(1)
        parsed_iframe = urlparse(data_url)
        iframe_origin = f"{parsed_iframe.scheme}://{parsed_iframe.netloc}/"

        r2 = SESSION.get(data_url, headers=headers, timeout=10)
        r2.raise_for_status()
        site2 = r2.text

        patterns = [
            r"source:\s*window\.atob\('([^']+)'\)",
            r"atob\(['\"]([^'\"]+)['\"]\)",
            r'source:\s*["\']([^"\']+)["\']',
            r'file:\s*["\']([^"\']+)["\']',
        ]

        link = None
        for pat in patterns:
            m = re.search(pat, site2)
            if m:
                try:
                    link = base64.b64decode(m.group(1)).decode("utf-8") if "atob" in pat else m.group(1)
                    break
                except Exception:
                    link = m.group(1)
                    break
        
        if link:
            return process_m3u8(link, req_referer=iframe_origin)
        
        return Response("Not found", status=404)

    except Exception:
        return Response("Proxy err", status=502)

@app.route("/health", methods=["GET"])
def health():
    return Response("OK", status=200, content_type="text/plain")

@app.route("/favicon.ico")
def favicon():
    return "", 204

@app.route("/cdn/<encoded_url>", methods=["GET"])
def cdn_route(encoded_url):
    try:
        url = decode_url(encoded_url)
        return process_m3u8(url)
    except Exception:
        return Response("Decode err", status=400)

@app.route("/", methods=["GET"])
def index():
    live_id = request.args.get("ID")
    if live_id:
        return resolve_and_play(live_id)
    return Response("Usage: /<id>", status=400)

@app.route("/<live_id>", methods=["GET"])
def play_id_raw(live_id):
    live_id = live_id.replace(".m3u8", "")
    return resolve_and_play(live_id)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000, debug=False)
