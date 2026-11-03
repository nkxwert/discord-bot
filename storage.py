"""File storage for !올리기 on any S3-compatible service (Backblaze B2, Cloudflare R2, ...),
plus optional video conversion with ffmpeg.

With STORAGE_PUBLIC_URL (a public bucket) links are plain URLs. Without it the bucket stays private and
links are presigned for up to 7 days, which matches how long files are kept. That works on accounts that
can't make public buckets, e.g. Backblaze B2 without a payment method."""
import asyncio
import mimetypes
import os
import re
import secrets
import shutil
import tempfile
from urllib.parse import quote, unquote

from dotenv import load_dotenv

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
MAX_LINK_SECONDS = 7 * 24 * 60 * 60  # the longest a presigned (SigV4) link can last
PAGE_LINK_SECONDS = 60 * 60

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
CONVERT_MAX_BYTES = 300 * 1024 * 1024
CONVERT_TIMEOUT_SECONDS = 600

_client = None


class StorageError(Exception):
    """Message is safe to show to the user."""


def configured():
    return all((ENDPOINT, ACCESS_KEY_ID, SECRET_ACCESS_KEY, BUCKET))


def client():
    global _client
    if _client is None:
        import boto3  # imported lazily so the bot runs without boto3 when storage isn't set up
        from botocore.config import Config
        _client = boto3.client(
            "s3",
            endpoint_url=ENDPOINT,
            aws_access_key_id=ACCESS_KEY_ID,
            aws_secret_access_key=SECRET_ACCESS_KEY,
            region_name=REGION,
            # Path-style keeps the upload page and the upload on one host, so no CORS rule is needed.
            config=Config(signature_version="s3v4", retries={"max_attempts": 3}, s3={"addressing_style": "path"}),
        )
    return _client


def clean_filename(name):
    """Keep the original name (and its extension, which Discord needs to play a video) minus path tricks."""
    name = os.path.basename(name.replace("\\", "/")).strip()
    name = re.sub(r"[\x00-\x1f\x7f/\\?#%\"<>|*:]", "_", name)
    name = name.lstrip(".") or "file"
    if len(name) > 100:
        stem, ext = os.path.splitext(name)
        name = stem[:100 - len(ext)] + ext
    return name


def new_key(guild_id, filename):
    """Random part so links can't be guessed; the filename stays last so the link ends in .mp4 etc."""
    return f"files/{guild_id or 'dm'}/{secrets.token_urlsafe(9)}/{clean_filename(filename)}"


def link(key, seconds=MAX_LINK_SECONDS):
    """Link that opens the file: a plain URL for a public bucket, else presigned (at most 7 days)."""
    if PUBLIC_URL:
        return f"{PUBLIC_URL}/{quote(key)}"
    return client().generate_presigned_url(
        "get_object", Params={"Bucket": BUCKET, "Key": key},
        ExpiresIn=max(60, min(int(seconds), MAX_LINK_SECONDS)),
    )


def guess_type(filename):
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def stored_headers(filename, content_type=None):
    """Content-Type / Content-Disposition to store: inline for safe media, a forced download otherwise."""
    content_type = (content_type or guess_type(filename)).split(";")[0].strip().lower()
    if content_type in INLINE_TYPES:
        return {"ContentType": content_type}
    return {
        "ContentType": "application/octet-stream",
        "ContentDisposition": f"attachment; filename*=UTF-8''{quote(clean_filename(filename))}",
    }


def is_inline(content_type):
    return content_type in INLINE_TYPES


async def put_file(key, data, filename, content_type=None):
    headers = stored_headers(filename, content_type)
    try:
        await asyncio.to_thread(client().put_object, Bucket=BUCKET, Key=key, Body=data, **headers)
    except Exception as exc:
        raise StorageError("파일 저장소에 올리지 못했습니다. 잠시 후 다시 시도해 주세요.") from exc
    return headers["ContentType"]


async def head(key):
    """(size, content_type, original filename or None) of an uploaded object, or None if it isn't there."""
    try:
        info = await asyncio.to_thread(client().head_object, Bucket=BUCKET, Key=key)
    except Exception as exc:
        if getattr(exc, "response", {}).get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return None
        raise StorageError("파일 저장소를 확인하지 못했습니다. 잠시 후 다시 시도해 주세요.") from exc
    match = re.search(r"filename\*=UTF-8''([^;]+)", info.get("ContentDisposition") or "")
    filename = clean_filename(unquote(match.group(1))) if match else None
    return info["ContentLength"], (info.get("ContentType") or "").split(";")[0].strip().lower(), filename


def name_for_type(content_type, stem="file"):
    """A filename for a browser upload that didn't say its name: the extension comes from the type."""
    extension = mimetypes.guess_extension(content_type or "") or ".bin"
    return f"{stem}{extension}"


async def finish_browser_upload(temp_key, filename, content_type, size):
    """Move a browser upload from its temporary key to its final name with the safe stored type,
    converting a video Discord can't play when possible. Returns (key, content_type, size, filename)."""
    folder = temp_key.rsplit("/", 1)[0]
    try:
        if is_video(filename) and can_convert() and size <= CONVERT_MAX_BYTES:
            converted = await to_playable_mp4(await download(temp_key), filename)
            if converted:
                data, filename = converted
                key = f"{folder}/{clean_filename(filename)}"
                stored_type = await put_file(key, data, filename)
                await delete([temp_key])
                return key, stored_type, len(data), filename
        key = f"{folder}/{clean_filename(filename)}"
        headers = stored_headers(filename, content_type)
        await asyncio.to_thread(
            client().copy_object, Bucket=BUCKET, Key=key, CopySource={"Bucket": BUCKET, "Key": temp_key},
            MetadataDirective="REPLACE", **headers,
        )
        await delete([temp_key])
        return key, headers["ContentType"], size, filename
    except StorageError:
        raise
    except Exception as exc:
        raise StorageError("파일을 정리하지 못했습니다. 잠시 후 다시 [업로드 완료]를 눌러 주세요.") from exc


async def download(key):
    result = await asyncio.to_thread(client().get_object, Bucket=BUCKET, Key=key)
    return await asyncio.to_thread(result["Body"].read)


async def delete(keys):
    """Delete objects; returns True if storage accepted the request (missing objects count as deleted).
    Backblaze B2 keeps a deleted file as a hidden older version that still uses space, so every version
    of each key is removed. Services without versions (e.g. R2) get a plain delete."""
    keys = [k for k in keys if k]
    if not keys:
        return True
    try:
        objects = await asyncio.to_thread(_all_versions, keys)
        for i in range(0, len(objects), 1000):
            await asyncio.to_thread(
                client().delete_objects, Bucket=BUCKET, Delete={"Objects": objects[i:i + 1000], "Quiet": True},
            )
    except Exception as exc:
        print(f"저장소 삭제 실패: {type(exc).__name__}: {exc}")
        return False
    return True


def _all_versions(keys):
    objects = []
    try:
        for key in keys:
            listing = client().list_object_versions(Bucket=BUCKET, Prefix=key)
            for entry in listing.get("Versions", []) + listing.get("DeleteMarkers", []):
                if entry["Key"] == key:
                    objects.append({"Key": key, "VersionId": entry["VersionId"]})
    except Exception:
        objects = []  # no version listing on this service
    found = {o["Key"] for o in objects}
    return objects + [{"Key": k} for k in keys if k not in found]


def presigned_put(key):
    return client().generate_presigned_url(
        "put_object", Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=PRESIGN_SECONDS,
    )


async def setup_bucket():
    """Upload the big-file page, and for a public bucket allow the browser to PUT from it (CORS).
    Failures are only logged: the CORS rule can also be set by hand (see README)."""
    try:
        with open(UPLOAD_PAGE_PATH, "rb") as f:
            page = f.read()
        await asyncio.to_thread(
            client().put_object, Bucket=BUCKET, Key=UPLOAD_PAGE_KEY, Body=page,
            ContentType="text/html; charset=utf-8", CacheControl="no-cache",
        )
    except Exception as exc:
        print(f"업로드 페이지 올리기 실패: {type(exc).__name__}: {exc}")
    if not PUBLIC_URL:
        return  # page and upload share the storage host: same origin, no CORS needed
    try:
        await asyncio.to_thread(client().put_bucket_cors, Bucket=BUCKET, CORSConfiguration={"CORSRules": [{
            "AllowedOrigins": [PUBLIC_URL], "AllowedMethods": ["PUT"], "AllowedHeaders": ["content-type", "content-disposition"],
            "MaxAgeSeconds": 3600,
        }]})
    except Exception as exc:
        print(f"저장소 CORS 설정 실패 (대시보드에서 직접 설정해 주세요): {type(exc).__name__}: {exc}")


def upload_page_link(key):
    # The PUT link rides in the #fragment, which browsers never send to the server.
    return f"{link(UPLOAD_PAGE_KEY, PAGE_LINK_SECONDS)}#{quote(presigned_put(key), safe='')}"


# ---------------------------------------------------------------- video conversion (optional)

def can_convert():
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def is_video(filename):
    return os.path.splitext(filename)[1].lower() in VIDEO_EXTENSIONS


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


async def to_playable_mp4(data, filename):
    """(data, filename) as an H.264/AAC MP4 that Discord plays, or None to keep the original.
    Videos that already play (H.264 in MP4) are left alone."""
    if not can_convert() or not is_video(filename) or len(data) > CONVERT_MAX_BYTES:
        return None
    with tempfile.TemporaryDirectory() as folder:
        source = os.path.join(folder, "in" + os.path.splitext(filename)[1].lower())
        target = os.path.join(folder, "out.mp4")
        with open(source, "wb") as f:
            f.write(data)
        codec = await _run("ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name",
                           "-of", "default=nw=1:nk=1", source, timeout=30)
        if codec is None:
            return None  # not a video ffprobe understands
        codec = codec.strip()
        if (codec == "h264" and source.endswith((".mp4", ".m4v"))) or (codec in ("vp8", "vp9") and source.endswith(".webm")):
            return None  # Discord already plays it
        done = await _run("ffmpeg", "-y", "-v", "error", "-i", source, "-map", "0:v:0", "-map", "0:a:0?",
                          "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p",
                          "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", target,
                          timeout=CONVERT_TIMEOUT_SECONDS)
        if done is None or not os.path.exists(target):
            return None
        with open(target, "rb") as f:
            converted = f.read()
    return converted, os.path.splitext(clean_filename(filename))[0] + ".mp4"
