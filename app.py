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
    encoded_url = encoded_url.replace('-', '+').replace('_', '/')
    padding = 4 - (len(encoded_url) % 4)
    if padding != 4:
        encoded_url += "=" * padding
    return base64.b64decode(encoded_url).decode('utf-8')

def make_proxy_url(base_url, link, ext):
    if not re.match(r"^https?://", link):
        parsed = urlparse(base_url)
        if link.startswith("/"):
            link = f"{parsed.scheme}://{parsed.netloc}{link}"
        else:
            base_dir = base_url[:base_url.rfind("/") + 1]
            link = base_dir + link
            
    encoded = encode_url(link)
    self_url = get_self_url()
    return f"{self_url}/cdn/{encoded}{ext}"

def process_m3u8(cdn_url):
    try:
        resp = SESSION.get(cdn_url, headers=HEADERS_PROXY, allow_redirects=True, stream=True, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"Fetch err: {e}")
        return Response("Stream offline", status=502)

    content_type = resp.headers.get("Content-Type", "").lower()
    is_m3u8 = "mpegurl" in content_type or urlparse(cdn_url).path.endswith(".m3u8")

    out_headers = {
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "no-store, no-cache, must-revalidate"
    }

    if is_m3u8:
        text = resp.content.decode('utf-8', errors='ignore')
        lines = text.splitlines()
        
        out = ["#EXTM3U", "#EXT-X-VERSION:3"]
        is_next_playlist = False

        for line in lines:
            line = line.strip()
            if not line or line == "#EXTM3U" or line.startswith("#EXT-X-VERSION"):
                continue
                
            if line.startswith("#EXT-X-MEDIA:TYPE=AUDIO"):
                continue

            if line.startswith("#EXT-X-STREAM-INF"):
                line = re.sub(r',AUDIO="[^"]+"', '', line)
                out.append(line)
                is_next_playlist = True
            elif line.startswith(("#EXT-X-TARGETDURATION", "#EXT-X-MEDIA-SEQUENCE", "#EXTINF", "#EXT-X-ENDLIST")):
                out.append(line)
                is_next_playlist = False
            elif line.startswith(("#EXT-X-KEY:", "#EXT-X-MAP:", "#EXT-X-MEDIA:", "#EXT-X-I-FRAME-STREAM-INF:")):
                m = re.search(r'URI="([^"]+)"', line)
                if m:
                    uri = m.group(1)
                    ext = ".key"
                    if line.startswith("#EXT-X-MAP:"):
                        ext = ".mp4"
                    else:
                        match = re.search(r'\.([a-z0-9]+)$', urlparse(uri).path, re.I)
                        if match:
                            ext = "." + match.group(1).lower()
                    proxied_uri = make_proxy_url(cdn_url, uri, ext)
                    line = line.replace(f'URI="{uri}"', f'URI="{proxied_uri}"')
                out.append(line)
            elif not line.startswith("#"):
                ext = ".ts"
                if is_next_playlist:
                    ext = ".m3u8"
                else:
                    match = re.search(r'\.([a-z0-9]+)$', urlparse(line).path, re.I)
                    if match:
                        real_ext = match.group(1).lower()
                        if real_ext in ["ts", "m4s", "mp4", "m4v", "m4a", "aac", "vtt", "jpg", "png"]:
                            ext = "." + real_ext
                
                proxied_line = make_proxy_url(cdn_url, line, ext)
                out.append(proxied_line)
                is_next_playlist = False
            else:
                out.append(line)

        out_headers["Content-Type"] = "application/vnd.apple.mpegurl"
        return Response("\r\n".join(out), headers=out_headers)

    def generate():
        for chunk in resp.iter_content(chunk_size=16384):
            if chunk:
                yield chunk

    if "video" in content_type or "audio" in content_type:
        out_headers["Content-Type"] = content_type
    elif urlparse(cdn_url).path.endswith((".m4s", ".mp4")):
        out_headers["Content-Type"] = "video/mp4"
    elif urlparse(cdn_url).path.endswith(".aac"):
        out_headers["Content-Type"] = "audio/aac"
    else:
        out_headers["Content-Type"] = "video/mp2t"

    if "Content-Length" in resp.headers:
        out_headers["Content-Length"] = resp.headers["Content-Length"]

    return Response(
        stream_with_context(generate()),
        status=resp.status_code,
        headers=out_headers
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
        ext_match = re.search(r'\.[a-z0-9]+$', encoded_url, re.I)
        if ext_match:
            encoded_url = encoded_url[:ext_match.start()]
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
