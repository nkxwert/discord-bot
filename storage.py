"""File storage for !올리기 on any S3-compatible service (Backblaze B2, Cloudflare R2, ...),
plus optional video conversion with ffmpeg.

With STORAGE_PUBLIC_URL (a public bucket) links are plain URLs. Without it the bucket stays private and
links are presigned for up to 7 days, which matches how long files are kept. That works on accounts that
can't make public buckets, e.g. Backblaze B2 without a payment method.

The S3 calls are signed here (AWS Signature V4) and sent with the bot's shared aiohttp session instead
of boto3, which alone needs about 17MB of memory. Files are streamed through in small chunks and are
never held in memory as a whole."""
import asyncio
import hashlib
import hmac
import mimetypes
import os
import re
import secrets
import shutil
import tempfile
import time
import xml.etree.ElementTree as ElementTree
from datetime import datetime, timezone
from urllib.parse import quote, unquote, urlsplit

import aiohttp
from dotenv import load_dotenv
from yarl import URL

import net

load_dotenv()  # bot.py imports this module before it loads .env itself


def _env(name, legacy=""):
    """STORAGE_* setting, falling back to the older R2_* name."""
    return (os.getenv(name, "") or (os.getenv(legacy, "") if legacy else "")).strip()


_r2_account = _env("R2_ACCOUNT_ID")
ENDPOINT = _env("STORAGE_ENDPOINT").rstrip("/") or (
    f"https://{_r2_account}.r2.cloudflarestorage.com" if _r2_account else ""
)
ACCESS_KEY_ID = _env("STORAGE_ACCESS_KEY_ID", "R2_ACCESS_KEY_ID")
SECRET_ACCESS_KEY = _env("STORAGE_SECRET_ACCESS_KEY", "R2_SECRET_ACCESS_KEY")
BUCKET = _env("STORAGE_BUCKET", "R2_BUCKET")
PUBLIC_URL = _env("STORAGE_PUBLIC_URL", "R2_PUBLIC_URL").rstrip("/")
_b2_region = re.search(r"s3\.([a-z0-9-]+)\.backblazeb2\.com", ENDPOINT)
REGION = _env("STORAGE_REGION") or (_b2_region.group(1) if _b2_region else "auto")
HOST = urlsplit(ENDPOINT).netloc
MAX_LINK_SECONDS = 7 * 24 * 60 * 60  # the longest a presigned (SigV4) link can last
PAGE_LINK_SECONDS = 60 * 60
CHUNK_SIZE = 64 * 1024

UPLOAD_PAGE_KEY = "upload.html"  # the big-file upload page, served from the bucket itself
UPLOAD_PAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "upload.html")
PRESIGN_SECONDS = 600

# Shown in the browser / played in Discord. Everything else is stored as a plain download, so an uploaded
# HTML or SVG file can't be opened as a web page on our domain (phishing, scripts).
INLINE_TYPES = {
    "image/png", "image/jpeg", "image/gif", "image/webp",
    "video/mp4", "video/webm", "video/quicktime",
    "audio/mpeg", "audio/ogg", "audio/wav", "audio/x-wav", "audio/mp4", "audio/aac", "audio/flac",
}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
# ffmpeg needs far more memory than a small bot host has, so conversion is opt-in.
CONVERT_VIDEO = os.getenv("FILE_CONVERT_VIDEO", "false").strip().lower() in ("1", "true", "yes", "on")
CONVERT_MAX_BYTES = 300 * 1024 * 1024
CONVERT_TIMEOUT_SECONDS = 600


class StorageError(Exception):
    """Message is safe to show to the user."""


class S3Error(Exception):
    def __init__(self, status, body=b""):
        code = re.search(rb"<Code>([^<]+)</Code>", body or b"")
        message = re.search(rb"<Message>([^<]*)</Message>", body or b"")
        self.status = status
        self.code = code.group(1).decode() if code else ""
        self.message = message.group(1).decode(errors="replace") if message else ""
        super().__init__(f"HTTP {status} {self.code} {self.message}".strip())


def configured():
    return all((ENDPOINT, ACCESS_KEY_ID, SECRET_ACCESS_KEY, BUCKET))


# ---------------------------------------------------------------- AWS Signature V4

def _hmac(key, text):
    return hmac.new(key, text.encode(), hashlib.sha256).digest()


def _signing_key(date):
    key = _hmac(("AWS4" + SECRET_ACCESS_KEY).encode(), date)
    key = _hmac(key, REGION)
    key = _hmac(key, "s3")
    return _hmac(key, "aws4_request")


def _path(key=""):
    """Path-style URI (bucket in the path), so the upload page and uploads share one host: no CORS needed."""
    return f"/{quote(BUCKET, safe='')}" + (f"/{quote(key, safe='/')}" if key else "")


def _query(params):
    return "&".join(
        f"{quote(str(k), safe='-_.~')}={quote(str(v), safe='-_.~')}" for k, v in sorted(params.items())
    )


def _signature(date, string_to_sign):
    return hmac.new(_signing_key(date), string_to_sign.encode(), hashlib.sha256).hexdigest()


def _now():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return stamp, stamp[:8]


def _signed_headers(method, path, params, headers, payload_hash):
    stamp, date = _now()
    headers = {k.lower(): str(v) for k, v in headers.items()}
    headers.update({"host": HOST, "x-amz-date": stamp, "x-amz-content-sha256": payload_hash})
    names = sorted(k for k in headers if k == "host" or k.startswith("x-amz-"))
    canonical = "\n".join([
        method, path, _query(params),
        "".join(f"{k}:{' '.join(headers[k].split())}\n" for k in names),
        ";".join(names), payload_hash,
    ])
    scope = f"{date}/{REGION}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    headers["authorization"] = (f"AWS4-HMAC-SHA256 Credential={ACCESS_KEY_ID}/{scope}, "
                                f"SignedHeaders={';'.join(names)}, Signature={_signature(date, to_sign)}")
    del headers["host"]  # aiohttp sends it from the URL
    return headers


def presign(method, key, seconds):
    stamp, date = _now()
    scope = f"{date}/{REGION}/s3/aws4_request"
    params = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256", "X-Amz-Credential": f"{ACCESS_KEY_ID}/{scope}",
        "X-Amz-Date": stamp, "X-Amz-Expires": str(int(seconds)), "X-Amz-SignedHeaders": "host",
    }
    path = _path(key)
    canonical = "\n".join([method, path, _query(params), f"host:{HOST}\n", "host", "UNSIGNED-PAYLOAD"])
    to_sign = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    return f"{ENDPOINT}{path}?{_query(params)}&X-Amz-Signature={_signature(date, to_sign)}"


async def _s3(method, key="", params=None, headers=None, data=b"", unsigned_payload=False,
              ok=(200, 204), timeout=60):
    """One signed request. Returns (status, headers, body); raises S3Error for other statuses."""
    params = params or {}
    payload_hash = "UNSIGNED-PAYLOAD" if unsigned_payload else hashlib.sha256(data or b"").hexdigest()
    path = _path(key)
    signed = _signed_headers(method, path, params, headers or {}, payload_hash)
    url = ENDPOINT + path + (f"?{_query(params)}" if params else "")
    async with net.session().request(
        method, URL(url, encoded=True), headers=signed, data=data,
        timeout=aiohttp.ClientTimeout(total=timeout),
    ) as response:
        body = await response.read()
        if response.status not in ok:
            raise S3Error(response.status, body)
        # CopyObject can answer 200 with an error in the body.
        if method == "PUT" and b"<Error>" in body[:200]:
            raise S3Error(500, body)
        return response.status, response.headers, body


def _xml_items(body, tag):
    root = ElementTree.fromstring(body)
    return [el for el in root.iter() if el.tag.rsplit("}", 1)[-1] == tag]


def _child(element, tag):
    for child in element:
        if child.tag.rsplit("}", 1)[-1] == tag:
            return child.text or ""
    return ""


# ---------------------------------------------------------------- names, links, types

def clean_filename(name):
    """Keep the original name (and its extension, which Discord needs to play a video) minus path tricks."""
    name = os.path.basename(name.replace("\\", "/")).strip()
    name = re.sub(r"[\x00-\x1f\x7f/\\?#%\"<>|*:]", "_", name)
    name = name.lstrip(".") or "file"
    if len(name) > 100:
        stem, ext = os.path.splitext(name)
        name = stem[:100 - len(ext)] + ext
    return name


def key_filename(filename):
    """ASCII form of the filename for the storage key. Korean letters would be percent-encoded and make
    links far longer (Discord link buttons allow 512 characters); the original name is kept in the list
    and in Content-Disposition, so downloads still get it. The extension stays so videos play."""
    stem, extension = os.path.splitext(clean_filename(filename))
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-_")[:40] or "file"
    extension = re.sub(r"[^A-Za-z0-9.]", "", extension)[:10].lower()
    return stem + extension


def new_key(guild_id, filename):
    """Random part so links can't be guessed; the filename stays last so the link ends in .mp4 etc."""
    return f"files/{guild_id or 'dm'}/{secrets.token_urlsafe(9)}/{key_filename(filename)}"


def link(key, seconds=MAX_LINK_SECONDS):
    """Link that opens the file: a plain URL for a public bucket, else presigned (at most 7 days)."""
    if PUBLIC_URL:
        return f"{PUBLIC_URL}/{quote(key)}"
    return presign("GET", key, max(60, min(int(seconds), MAX_LINK_SECONDS)))


def presigned_put(key):
    return presign("PUT", key, PRESIGN_SECONDS)


def guess_type(filename):
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def stored_headers(filename, content_type=None):
    """Content-Type / Content-Disposition to store: inline for safe media, a forced download otherwise."""
    content_type = (content_type or guess_type(filename)).split(";")[0].strip().lower()
    name = quote(clean_filename(filename))
    if content_type in INLINE_TYPES:
        return {"Content-Type": content_type, "Content-Disposition": f"inline; filename*=UTF-8''{name}"}
    return {"Content-Type": "application/octet-stream", "Content-Disposition": f"attachment; filename*=UTF-8''{name}"}


def is_inline(content_type):
    return content_type in INLINE_TYPES


def name_for_type(content_type, stem="file"):
    """A filename for a browser upload that didn't say its name: the extension comes from the type."""
    extension = mimetypes.guess_extension(content_type or "") or ".bin"
    return f"{stem}{extension}"


# ---------------------------------------------------------------- uploads

async def _file_chunks(path):
    with open(path, "rb") as f:
        while chunk := await asyncio.to_thread(f.read, CHUNK_SIZE):
            yield chunk


async def put_bytes(key, data, filename, content_type=None, extra_headers=None):
    """Small uploads (the upload page). Returns the stored Content-Type."""
    headers = {**stored_headers(filename, content_type), **(extra_headers or {})}
    try:
        await _s3("PUT", key, headers=headers, data=data)
    except Exception as exc:
        raise StorageError("파일 저장소에 올리지 못했습니다. 잠시 후 다시 시도해 주세요.") from exc
    return headers["Content-Type"]


async def put_path(key, path, filename, content_type=None):
    """Stream a local file up. Returns the stored Content-Type."""
    headers = stored_headers(filename, content_type)
    headers["Content-Length"] = str(os.path.getsize(path))
    try:
        await _s3("PUT", key, headers=headers, data=_file_chunks(path), unsigned_payload=True, timeout=3600)
    except Exception as exc:
        raise StorageError("파일 저장소에 올리지 못했습니다. 잠시 후 다시 시도해 주세요.") from exc
    return headers["Content-Type"]


async def put_from_url(key, url, size, filename, content_type=None):
    """Stream a file (a Discord attachment) straight from its URL into storage, a chunk at a time,
    so even a big file only uses a few hundred KB of memory. Returns the stored Content-Type."""
    headers = stored_headers(filename, content_type)
    headers["Content-Length"] = str(size)
    try:
        async with net.session().get(url, timeout=aiohttp.ClientTimeout(total=3600)) as source:
            if source.status != 200:
                raise StorageError("디스코드에서 파일을 받지 못했습니다. 다시 시도해 주세요.")
            await _s3("PUT", key, headers=headers, data=source.content.iter_chunked(CHUNK_SIZE),
                      unsigned_payload=True, timeout=3600)
    except StorageError:
        raise
    except Exception as exc:
        raise StorageError("파일 저장소에 올리지 못했습니다. 잠시 후 다시 시도해 주세요.") from exc
    return headers["Content-Type"]


async def download_to(key_or_url, path, *, is_url=False):
    """Stream a stored object (or any URL) into a local file."""
    url = key_or_url if is_url else link(key_or_url, 3600)
    async with net.session().get(URL(url, encoded=True), timeout=aiohttp.ClientTimeout(total=3600)) as response:
        if response.status != 200:
            raise StorageError("파일을 받지 못했습니다.")
        with open(path, "wb") as f:
            async for chunk in response.content.iter_chunked(CHUNK_SIZE):
                await asyncio.to_thread(f.write, chunk)


async def head(key):
    """(size, content_type, original filename or None) of an uploaded object, or None if it isn't there."""
    try:
        _, headers, _ = await _s3("HEAD", key)
    except S3Error as exc:
        if exc.status == 404:
            return None
        raise StorageError("파일 저장소를 확인하지 못했습니다. 잠시 후 다시 시도해 주세요.") from exc
    except Exception as exc:
        raise StorageError("파일 저장소를 확인하지 못했습니다. 잠시 후 다시 시도해 주세요.") from exc
    match = re.search(r"filename\*=UTF-8''([^;]+)", headers.get("Content-Disposition") or "")
    filename = clean_filename(unquote(match.group(1))) if match else None
    content_type = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
    return int(headers.get("Content-Length") or 0), content_type, filename


async def copy(source_key, key, filename, content_type):
    """Server-side copy with the safe stored type (nothing passes through the bot)."""
    headers = {
        **stored_headers(filename, content_type),
        "x-amz-copy-source": _path(source_key),
        "x-amz-metadata-directive": "REPLACE",
    }
    await _s3("PUT", key, headers=headers)
    return headers["Content-Type"]


async def finish_browser_upload(temp_key, filename, content_type, size):
    """Move a browser upload from its temporary key to its final name with the safe stored type,
    converting a video Discord can't play when enabled. Returns (key, content_type, size, filename)."""
    folder = temp_key.rsplit("/", 1)[0]
    try:
        if should_convert(filename, size):
            converted = await convert_stored(temp_key, filename)
            if converted:
                path, filename, size, cleanup = converted
                try:
                    key = f"{folder}/{key_filename(filename)}"
                    stored_type = await put_path(key, path, filename)
                finally:
                    cleanup()
                await delete([temp_key])
                return key, stored_type, size, filename
        key = f"{folder}/{key_filename(filename)}"
        stored_type = await copy(temp_key, key, filename, content_type)
        await delete([temp_key])
        return key, stored_type, size, filename
    except StorageError:
        raise
    except Exception as exc:
        raise StorageError("파일을 정리하지 못했습니다. 잠시 후 다시 [업로드 완료]를 눌러 주세요.") from exc


# ---------------------------------------------------------------- deleting and listing

async def _versions(key):
    """Every version id of key (B2 keeps deleted files as hidden versions); None if not supported."""
    try:
        _, _, body = await _s3("GET", params={"versions": "", "prefix": key})
    except Exception:
        return None
    found = []
    for tag in ("Version", "DeleteMarker"):
        for item in _xml_items(body, tag):
            if _child(item, "Key") == key:
                found.append(_child(item, "VersionId"))
    return found


async def delete(keys):
    """Delete objects; returns True if storage accepted it (missing objects count as deleted).
    Backblaze B2 keeps a deleted file as a hidden older version that still uses space, so every version
    of each key is removed. Services without versions (e.g. R2) get a plain delete."""
    try:
        for key in [k for k in keys if k]:
            versions = await _versions(key)
            for version in versions or [None]:
                params = {"versionId": version} if version else {}
                await _s3("DELETE", key, params=params, ok=(200, 204, 404))
    except Exception as exc:
        print(f"저장소 삭제 실패: {type(exc).__name__}: {exc}")
        return False
    return True


async def _list(max_pages):
    """(key, size, last_modified timestamp) under files/, and whether more pages were left."""
    items, token = [], None
    for _ in range(max_pages):
        params = {"list-type": "2", "prefix": "files/", "max-keys": "1000"}
        if token:
            params["continuation-token"] = token
        _, _, body = await _s3("GET", params=params)
        for item in _xml_items(body, "Contents"):
            modified = datetime.fromisoformat(_child(item, "LastModified").replace("Z", "+00:00")).timestamp()
            items.append((_child(item, "Key"), int(_child(item, "Size") or 0), modified))
        root = ElementTree.fromstring(body)
        truncated = _child(root, "IsTruncated") == "true"
        if not truncated:
            return items, False
        token = _child(root, "NextContinuationToken")
    return items, True


async def bucket_usage(max_pages=10):
    """(object count, bytes, more_left) actually in the bucket under files/, or None on failure.
    Each page is one list request (B2 counts it against the daily free transactions), so it is capped."""
    try:
        items, more = await _list(max_pages)
    except Exception as exc:
        print(f"저장소 사용량 확인 실패: {type(exc).__name__}: {exc}")
        return None
    return len(items), sum(size for _, size, _ in items), more


async def orphan_keys(known, max_pages=20, min_age_seconds=3600):
    """Keys under files/ that the bot's list doesn't know (older than an hour, so uploads in progress are
    left alone), or None if the bucket couldn't be listed."""
    try:
        items, _ = await _list(max_pages)
    except Exception as exc:
        print(f"저장소 목록 확인 실패: {type(exc).__name__}: {exc}")
        return None
    known, cutoff = set(known), time.time() - min_age_seconds
    return [key for key, _, modified in items if key not in known and modified < cutoff]


def describe():
    """Where files go, for £DB: (host, bucket, link mode)."""
    return HOST, BUCKET, "공개 주소" if PUBLIC_URL else "비공개 + 서명 링크(최대 7일)"


async def ping():
    """(milliseconds, None) if the bucket answers, else (None, error text)."""
    start = time.perf_counter()
    try:
        try:
            await _s3("HEAD", timeout=15)
        except S3Error:
            # Some keys may not allow HeadBucket; listing one object proves the connection as well.
            await _s3("GET", params={"list-type": "2", "max-keys": "1"}, timeout=15)
    except Exception as exc:
        return None, f"{type(exc).__name__}: {str(exc)[:200]}"
    return (time.perf_counter() - start) * 1000, None


def upload_origins():
    """Web origins the upload page runs on: the storage host itself (private bucket), and the public URL."""
    origins = [f"{urlsplit(ENDPOINT).scheme}://{HOST}"]
    if PUBLIC_URL:
        origins.append(f"{urlsplit(PUBLIC_URL).scheme}://{urlsplit(PUBLIC_URL).netloc}")
    return list(dict.fromkeys(origins))


async def set_cors():
    """Allow the upload page to PUT. Browsers send an Origin header even to the same host, and B2 then
    checks CORS rules, so this is needed for private buckets too. Returns None or the error text."""
    rule = "<CORSConfiguration><CORSRule>" + "".join(f"<AllowedOrigin>{o}</AllowedOrigin>" for o in upload_origins()) + (
        "<AllowedMethod>PUT</AllowedMethod><AllowedMethod>GET</AllowedMethod><AllowedMethod>HEAD</AllowedMethod>"
        "<AllowedHeader>*</AllowedHeader><MaxAgeSeconds>3600</MaxAgeSeconds></CORSRule></CORSConfiguration>")
    rule = rule.encode()
    try:
        import base64
        md5 = base64.b64encode(hashlib.md5(rule).digest()).decode()
        await _s3("PUT", params={"cors": ""}, data=rule, headers={"Content-MD5": md5, "Content-Type": "application/xml"})
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


async def _try(label, method, url, headers=None, data=b""):
    try:
        async with net.session().request(method, URL(url, encoded=True), headers=headers or {}, data=data,
                                         timeout=aiohttp.ClientTimeout(total=20)) as response:
            body = await response.read()
            if response.status in (200, 204):
                return label, True, f"HTTP {response.status}"
            error = S3Error(response.status, body)
            return label, False, str(error)
    except Exception as exc:
        return label, False, f"{type(exc).__name__}: {exc}"


async def self_test():
    """Try what the bot and the upload page do, against the real storage (for £DB 테스트).
    Returns [(label, ok, detail)]; test objects are removed afterwards."""
    results = []
    key = f"files/selftest/{secrets.token_urlsafe(6)}/test.txt"
    try:
        await _s3("PUT", key, data=b"maco self-test", headers={"Content-Type": "text/plain"})
        results.append(("봇 업로드 (서명 헤더)", True, "OK"))
    except Exception as exc:
        results.append(("봇 업로드 (서명 헤더)", False, str(exc)))
    results.append(await _try("서명 링크로 열기 (GET)", "GET", link(key, 300)))
    origin = upload_origins()[0]
    browser = {"Content-Type": "text/plain", "Origin": origin}
    results.append(await _try("웹 업로드 (Origin 없음)", "PUT", presigned_put(key + ".a"),
                              {"Content-Type": "text/plain"}, b"a"))
    results.append(await _try("웹 업로드 (브라우저처럼 Origin 포함)", "PUT", presigned_put(key + ".b"), browser, b"b"))
    results.append(await _try("웹 업로드 + 파일 이름 헤더", "PUT", presigned_put(key + ".c"),
                              {**browser, "Content-Disposition": "attachment; filename*=UTF-8''test.txt"}, b"c"))
    cors_error = await set_cors()
    results.append(("CORS 규칙 설정", cors_error is None, cors_error or f"허용: {', '.join(upload_origins())}"))
    if cors_error is None:
        results.append(await _try("CORS 설정 후 웹 업로드 (Origin 포함)", "PUT", presigned_put(key + ".d"), browser, b"d"))
    native_key = f"files/selftest/{secrets.token_urlsafe(6)}/native.txt"
    if is_b2():
        try:
            await _b2_auth()
            results.append(("B2 고유 API 로그인", True, f"버킷 ID {_b2['bucketId'][:8]}…"))
            native_cors = await set_b2_cors()
            results.append(("B2 고유 CORS 규칙 설정", native_cors is None, native_cors or "b2_upload_file 허용"))
            results.append(await _b2_try("B2 고유 업로드 (Origin 포함, 페이지 방식)", origin, name=native_key))
        except Exception as exc:
            results.append(("B2 고유 API 로그인", False, f"{type(exc).__name__}: {exc}"))
    await delete([key] + [key + s for s in (".a", ".b", ".c", ".d")] + [native_key])
    return results


# ---------------------------------------------------------------- Backblaze B2 native API (browser uploads)
# Browsers got "400 InvalidRequest" from B2's S3 endpoint on presigned PUTs that work from the bot.
# Backblaze's documented browser upload is the native b2_upload_file call (see backblaze-b2-samples/
# b2-browser-upload), so on B2 the upload page uses that, with B2's own CORS rule for it.

B2_AUTH_URL = "https://api.backblazeb2.com/b2api/v3/b2_authorize_account"
B2_AUTH_SECONDS = 12 * 60 * 60  # tokens last 24 hours; renew well before
_b2 = {}  # cached authorization: apiUrl, token, accountId, bucketId, at


def is_b2():
    return "backblazeb2.com" in HOST


class B2Error(Exception):
    pass


async def _b2_call(name, payload, retry=True):
    auth = await _b2_auth()
    url = f"{auth['apiUrl']}/b2api/v3/{name}"
    async with net.session().post(url, json=payload, headers={"Authorization": auth["token"]},
                                  timeout=aiohttp.ClientTimeout(total=20)) as response:
        data = await response.json(content_type=None)
    if response.status == 401 and retry:
        _b2.clear()
        return await _b2_call(name, payload, retry=False)
    if response.status != 200:
        raise B2Error(f"{name} HTTP {response.status} {data.get('code', '')} {data.get('message', '')}".strip())
    return data


async def _b2_auth():
    if _b2 and time.time() - _b2["at"] < B2_AUTH_SECONDS:
        return _b2
    import base64
    basic = base64.b64encode(f"{ACCESS_KEY_ID}:{SECRET_ACCESS_KEY}".encode()).decode()
    async with net.session().get(B2_AUTH_URL, headers={"Authorization": f"Basic {basic}"},
                                 timeout=aiohttp.ClientTimeout(total=20)) as response:
        data = await response.json(content_type=None)
    if response.status != 200:
        raise B2Error(f"b2_authorize_account HTTP {response.status} {data.get('code', '')} {data.get('message', '')}".strip())
    storage_api = data["apiInfo"]["storageApi"]
    _b2.clear()
    _b2.update(apiUrl=storage_api["apiUrl"], token=data["authorizationToken"], accountId=data["accountId"], at=time.time())
    allowed = [b for b in storage_api.get("allowed", {}).get("buckets") or [] if b.get("name") == BUCKET]
    if allowed and allowed[0].get("id"):
        _b2["bucketId"] = allowed[0]["id"]
    else:
        listing = await _b2_call("b2_list_buckets", {"accountId": data["accountId"], "bucketName": BUCKET})
        if not listing.get("buckets"):
            raise B2Error(f"버킷 {BUCKET}을(를) 찾지 못했습니다")
        _b2["bucketId"] = listing["buckets"][0]["bucketId"]
    return _b2


async def b2_upload_target():
    """(uploadUrl, upload token) for one browser upload. A fresh URL per link, since B2 wants one upload
    at a time per URL."""
    auth = await _b2_auth()
    data = await _b2_call("b2_get_upload_url", {"bucketId": auth["bucketId"]})
    return data["uploadUrl"], data["authorizationToken"]


async def set_b2_cors():
    """B2's own CORS rule: lets the page (served from the S3 host) call b2_upload_file, and keeps the S3
    operations. Replaces the bucket's CORS rules. Returns None or the error text."""
    try:
        auth = await _b2_auth()
        await _b2_call("b2_update_bucket", {
            "accountId": auth["accountId"], "bucketId": auth["bucketId"],
            "corsRules": [{
                "corsRuleName": "maco-upload",
                "allowedOrigins": upload_origins(),
                "allowedOperations": ["b2_upload_file", "b2_download_file_by_name", "b2_download_file_by_id",
                                      "s3_put", "s3_get", "s3_head"],
                "allowedHeaders": ["authorization", "content-type", "content-disposition", "x-bz-file-name",
                                   "x-bz-content-sha1", "x-bz-info-b2-content-disposition", "x-bz-info-*"],
                "exposeHeaders": ["x-bz-content-sha1", "x-bz-file-id"],
                "maxAgeSeconds": 3600,
            }],
        })
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


async def _b2_try(label, origin, data=b"self-test", name="files/selftest/native.txt"):
    try:
        upload_url, token = await b2_upload_target()
        headers = {"Authorization": token, "X-Bz-File-Name": quote(name, safe="/"), "Content-Type": "text/plain",
                   "X-Bz-Content-Sha1": "do_not_verify",
                   "X-Bz-Info-b2-content-disposition": quote("attachment; filename*=UTF-8''test.txt", safe="")}
        if origin:
            headers["Origin"] = origin
        async with net.session().post(upload_url, headers=headers, data=data,
                                      timeout=aiohttp.ClientTimeout(total=20)) as response:
            body = await response.json(content_type=None)
        if response.status == 200:
            allow = response.headers.get("Access-Control-Allow-Origin")
            return label, True, "HTTP 200" + (f" · CORS 허용 {allow}" if origin else "")
        return label, False, f"HTTP {response.status} {body.get('code', '')} {body.get('message', '')}".strip()
    except Exception as exc:
        return label, False, f"{type(exc).__name__}: {exc}"


async def setup_bucket():
    """Upload the big-file page and allow the browser to upload from it (CORS).
    Failures are only logged: the CORS rule can also be set by hand (see README)."""
    try:
        with open(UPLOAD_PAGE_PATH, "rb") as f:
            page = f.read()
        await _s3("PUT", UPLOAD_PAGE_KEY, data=page,
                  headers={"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-cache"})
    except Exception as exc:
        print(f"업로드 페이지 올리기 실패: {type(exc).__name__}: {exc}")
    error = await set_b2_cors() if is_b2() else await set_cors()
    if error:
        print(f"저장소 CORS 설정 실패 (대시보드에서 직접 설정해 주세요): {error}")


async def upload_page_link(key, retention="7일"):
    """Link to the upload page. Where to upload (and the retention shown) ride in the #fragment, which
    browsers never send to a server. On B2 that is a native upload URL and token ("b2:" + base64 JSON),
    elsewhere (or if B2's API fails) a presigned PUT URL."""
    target = presigned_put(key)
    if is_b2():
        try:
            upload_url, token = await b2_upload_target()
            import base64
            import json
            target = "b2:" + base64.urlsafe_b64encode(json.dumps({"u": upload_url, "t": token, "k": key}).encode()).decode()
        except Exception as exc:
            print(f"B2 업로드 주소 받기 실패, 서명 링크로 대신합니다: {type(exc).__name__}: {exc}")
    return f"{link(UPLOAD_PAGE_KEY, PAGE_LINK_SECONDS)}#{quote(target, safe='')}|{quote(retention, safe='')}"


# ---------------------------------------------------------------- video conversion (optional, off by default)

def can_convert():
    return CONVERT_VIDEO and bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def is_video(filename):
    return os.path.splitext(filename)[1].lower() in VIDEO_EXTENSIONS


def should_convert(filename, size):
    return can_convert() and is_video(filename) and size <= CONVERT_MAX_BYTES


async def _run(*args, timeout):
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, _ = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return None
    return out.decode(errors="replace") if process.returncode == 0 else None


async def _convert_file(source, filename, folder):
    """Path of an H.264/AAC MP4 made from source, or None to keep the original (already plays, or failed)."""
    codec = await _run("ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name",
                       "-of", "default=nw=1:nk=1", source, timeout=30)
    if codec is None:
        return None  # not a video ffprobe understands
    codec = codec.strip()
    extension = os.path.splitext(filename)[1].lower()
    if (codec == "h264" and extension in (".mp4", ".m4v")) or (codec in ("vp8", "vp9") and extension == ".webm"):
        return None  # Discord already plays it
    target = os.path.join(folder, "out.mp4")
    done = await _run("ffmpeg", "-y", "-v", "error", "-i", source, "-map", "0:v:0", "-map", "0:a:0?",
                      "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p",
                      "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", target,
                      timeout=CONVERT_TIMEOUT_SECONDS)
    return target if done is not None and os.path.exists(target) else None


async def _convert(fetch, filename):
    """fetch(path) saves the original to path. Returns (mp4 path, new filename, size, cleanup) or None.
    Everything goes through temporary files, never memory."""
    folder = tempfile.mkdtemp(prefix="maco-")

    def cleanup():
        shutil.rmtree(folder, ignore_errors=True)

    try:
        source = os.path.join(folder, "in" + os.path.splitext(filename)[1].lower())
        await fetch(source)
        target = await _convert_file(source, filename, folder)
        if not target:
            cleanup()
            return None
        os.remove(source)
        return target, os.path.splitext(clean_filename(filename))[0] + ".mp4", os.path.getsize(target), cleanup
    except Exception:
        cleanup()
        raise


async def convert_url(url, filename):
    return await _convert(lambda path: download_to(url, path, is_url=True), filename)


async def convert_stored(key, filename):
    return await _convert(lambda path: download_to(key, path), filename)
