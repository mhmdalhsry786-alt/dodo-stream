#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Dodo Stream (دودو ستريم) — Unified Private Cross-Platform Streaming Server & PWA
Supports iPhone (iOS Safari PWA + Native HLS + WebVTT + Outplayer/VLC),
Android (WebAPK PWA + Intent External Players), and Desktop.
"""
import base64
import glob
import gzip
import hashlib
import hmac
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, wait
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from auto_discovery import Discovery
from universal_media import (
    UniversalHlsManager,
    b64u_encode,
    build_universal_media_streams,
    build_v3_manifest_from_legacy,
    handle_feed_addon_request,
    handle_legacy_v1_request,
    handle_m3u_addon_request,
    probe_legacy_v1_endpoint,
    resolve_any_link,
)

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, 'data')
STATIC_DIR = os.path.join(HERE, 'static')
CONFIG_PATH = os.path.join(HERE, 'config.json')
RUNTIME_CACHE_DIR = (
    os.path.join(HERE, '.cache')
    if (os.name == 'nt' or HERE == '/app')
    else '/tmp/dodo_stream_cache'
)
STREMIO_WEB_DIR = (
    os.path.join(HERE, 'stremio_web')
    if os.path.isdir(os.path.join(HERE, 'stremio_web'))
    else os.path.join(RUNTIME_CACHE_DIR, 'stremio_web')
)
STREMIO_WEB_RELEASE_ZIP = 'https://github.com/Stremio/stremio-web/releases/download/v5.2.0/stremio-web.zip'


def ensure_stremio_web_ui():
    """Ensure official open-source Stremio Web v5.2.0 (GPL-2.0) assets are present locally."""
    idx_file = os.path.join(STREMIO_WEB_DIR, 'index.html')
    if os.path.isfile(idx_file):
        return True
    try:
        os.makedirs(STREMIO_WEB_DIR, exist_ok=True)
        tmp_zip = os.path.join(STREMIO_WEB_DIR, '_stremio_web_v5.zip')
        print('[Dodo Stream] Downloading official Stremio Web v5.2.0 open-source bundle...', flush=True)
        req = urllib.request.Request(STREMIO_WEB_RELEASE_ZIP, headers={'User-Agent': 'Mozilla/5.0 DodoStream/5.0'})
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp_zip, 'wb') as out_f:
            shutil.copyfileobj(r, out_f)
        with zipfile.ZipFile(tmp_zip) as zf:
            for member in zf.infolist():
                if member.is_dir() or member.filename.endswith('.map'):
                    continue
                rel = member.filename[len('build/'):] if member.filename.startswith('build/') else member.filename
                if not rel or '..' in rel:
                    continue
                dest_p = os.path.join(STREMIO_WEB_DIR, rel.replace('/', os.sep))
                os.makedirs(os.path.dirname(dest_p), exist_ok=True)
                with zf.open(member) as src_f, open(dest_p, 'wb') as dst_f:
                    shutil.copyfileobj(src_f, dst_f)
        os.remove(tmp_zip)
        return os.path.isfile(idx_file)
    except Exception as exc:
        print('[Dodo Stream] Warning: could not bootstrap Stremio Web bundle:', exc, flush=True)
        return False

FFMPEG = shutil.which('ffmpeg') or r'D:\LocalAI\Apps\MediaTools\ffmpeg\bin\ffmpeg.exe'
FFPROBE = shutil.which('ffprobe') or r'D:\LocalAI\Apps\MediaTools\ffmpeg\bin\ffprobe.exe'
HAS_FFMPEG = bool(shutil.which(FFMPEG) or os.path.isfile(FFMPEG))
HAS_FFPROBE = bool(shutil.which(FFPROBE) or os.path.isfile(FFPROBE))
UHLS = UniversalHlsManager(FFMPEG, FFPROBE, HAS_FFMPEG, HAS_FFPROBE)
STREMIO_ENGINE_URL = os.environ.get('STREMIO_ENGINE_URL', 'http://127.0.0.1:11470').rstrip('/')
STREMIO_SERVER_JS_URL = 'https://dl.strem.io/server/v4.22.3/desktop/server.js'
_EMBEDDED_ENGINE_PROC = None
_ENGINE_START_LOCK = threading.Lock()
_ENGINE_CACHE = {'ts': 0.0, 'ok': False}


def is_stremio_engine_alive(force=False):
    now = time.time()
    if not force and now - _ENGINE_CACHE['ts'] < 8.0:
        return _ENGINE_CACHE['ok']
    ok = False
    try:
        with urllib.request.urlopen(f'{STREMIO_ENGINE_URL}/settings', timeout=1.2) as r:
            ok = r.status == 200
    except Exception:
        ok = False
    _ENGINE_CACHE['ts'] = now
    _ENGINE_CACHE['ok'] = ok
    return ok


def ensure_embedded_stremio_engine():
    """Auto-download & start official Stremio Streaming Server v4.22.3 (11470) in Cloud/Docker or headless hosts!"""
    if os.environ.get('DODO_DISABLE_EMBEDDED_ENGINE') == '1':
        return False
    if is_stremio_engine_alive(force=True):
        return True
    with _ENGINE_START_LOCK:
        if is_stremio_engine_alive(force=True):
            return True
        return _start_embedded_stremio_engine_unlocked()


def _start_embedded_stremio_engine_unlocked():
    global _EMBEDDED_ENGINE_PROC

    # 1. Check Windows local Stremio installation first if on Windows
    if os.name == 'nt':
        local_stremio = os.path.expandvars(r'%LOCALAPPDATA%\Programs\Stremio')
        rt_exe = os.path.join(local_stremio, 'stremio-runtime.exe')
        srv_js = os.path.join(local_stremio, 'server.js')
        if os.path.isfile(rt_exe) and os.path.isfile(srv_js):
            try:
                _EMBEDDED_ENGINE_PROC = subprocess.Popen(
                    [rt_exe, srv_js],
                    cwd=local_stremio,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                )
                for _ in range(15):
                    time.sleep(0.4)
                    if is_stremio_engine_alive(force=True):
                        return True
            except Exception:
                pass

    # 2. Cloud / Linux / Docker / Cross-Platform Node.js Engine (stremio-server v4.22.3)
    node_bin = shutil.which('node') or shutil.which('nodejs')
    if not node_bin:
        return False
    srv_dir = os.path.join(RUNTIME_CACHE_DIR, 'stremio_server')
    app_data_dir = os.path.join('/tmp/dodo_stream_cache' if os.name != 'nt' else RUNTIME_CACHE_DIR, 'stremio_server_data')
    os.makedirs(srv_dir, exist_ok=True)
    os.makedirs(app_data_dir, exist_ok=True)
    srv_js_path = os.path.join(srv_dir, 'server.js')
    if not os.path.isfile(srv_js_path) or os.path.getsize(srv_js_path) < 100_000:
        try:
            print('[Dodo Stream] Downloading official Stremio Server v4.22.3 for Cloud Torrent Engine...', flush=True)
            req = urllib.request.Request(STREMIO_SERVER_JS_URL, headers={'User-Agent': 'Mozilla/5.0 DodoStream/5.0'})
            tmp_js = srv_js_path + '.tmp'
            with urllib.request.urlopen(req, timeout=45) as r, open(tmp_js, 'wb') as out_f:
                shutil.copyfileobj(r, out_f)
            os.replace(tmp_js, srv_js_path)
        except Exception as exc:
            print('[Dodo Stream] Warning: could not download stremio-server:', exc, flush=True)
            return False

    env = os.environ.copy()
    env['NO_CORS'] = '1'
    env['CASTING_DISABLED'] = '1'
    env['APP_PATH'] = app_data_dir
    if HAS_FFMPEG:
        env['FFMPEG_BIN'] = shutil.which(FFMPEG) or FFMPEG
    if HAS_FFPROBE:
        env['FFPROBE_BIN'] = shutil.which(FFPROBE) or FFPROBE
    try:
        _EMBEDDED_ENGINE_PROC = subprocess.Popen(
            [node_bin, '--max-old-space-size=160', srv_js_path],
            cwd=srv_dir,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        for _ in range(20):
            time.sleep(0.35)
            if is_stremio_engine_alive(force=True):
                # Bound torrent disk cache (512 MB in Cloud containers, 2 GB on Desktop)
                max_cache = 536870912 if HERE == '/app' else 2147483648
                try:
                    req_cfg = urllib.request.Request(
                        f'{STREMIO_ENGINE_URL}/settings',
                        data=json.dumps({'cacheSize': max_cache}).encode('utf-8'),
                        headers={'Content-Type': 'application/json'},
                        method='POST',
                    )
                    urllib.request.urlopen(req_cfg, timeout=3).read()
                except Exception:
                    pass
                print('[Dodo Stream] Embedded Cloud Torrent Engine (stremio-server v4.22.3) active on 127.0.0.1:11470!', flush=True)
                return True
    except Exception as exc:
        print('[Dodo Stream] Warning: failed to launch embedded stremio-server:', exc, flush=True)
    return False

CONFIG_LOCK = threading.RLock()


def load_or_init_config():
    defaults = {
        'app_name': 'دودو ستريم',
        'app_subtitle': 'منصتك الخاصة للأفلام والمسلسلات والكرتون المدبلج',
        'invite_code': 'DODO-' + secrets.token_hex(3).upper(),
        'admin_pin': str(1000 + secrets.randbelow(9000)),
        'privacy_mode': 'internet_only',  # 'always' | 'internet_only' | 'off'
        'secret_key': secrets.token_hex(24),
        'global_extra_addons': [],
    }
    env_b64 = os.environ.get('DODO_INIT_CONFIG_B64', '').strip()
    if env_b64:
        try:
            env_cfg = json.loads(base64.b64decode(env_b64).decode('utf-8'))
            if isinstance(env_cfg, dict):
                defaults.update({k: v for k, v in env_cfg.items() if k in defaults})
        except Exception:
            pass
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding='utf-8') as f:
                saved = json.load(f)
            defaults.update({k: v for k, v in saved.items() if k in defaults})
        except Exception:
            pass
    try:
        save_config(defaults)
    except Exception:
        pass
    return defaults


def save_config(cfg):
    with CONFIG_LOCK:
        tmp = CONFIG_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)


CFG = load_or_init_config()


def make_media_sig(cfg=None):
    c = cfg or CFG
    msg = ('media:' + str(c.get('invite_code', ''))).encode('utf-8')
    return hmac.new(c['secret_key'].encode('utf-8'), msg, hashlib.sha256).hexdigest()[:20]


def make_session_token(cfg=None):
    c = cfg or CFG
    msg = ('session:' + str(c.get('invite_code', ''))).encode('utf-8')
    return hmac.new(c['secret_key'].encode('utf-8'), msg, hashlib.sha256).hexdigest()[:28]


def verify_admin(pin):
    with CONFIG_LOCK:
        return hmac.compare_digest(str(pin or '').strip(), str(CFG.get('admin_pin', '')))


# ---------------------------------------------------------------------------
# Load Cartoon & Disney Catalogs
# ---------------------------------------------------------------------------
CATALOG_DATA = json.load(open(os.path.join(DATA_DIR, 'catalog.json'), encoding='utf-8'))
CURATED = json.load(open(os.path.join(DATA_DIR, 'curated_dubbed.json'), encoding='utf-8'))
CARTOONY = json.load(open(os.path.join(DATA_DIR, 'cartoony_data.json'), encoding='utf-8'))
for _pack in sorted(glob.glob(os.path.join(DATA_DIR, 'cartoony_pack*.json'))):
    CARTOONY.update(json.load(open(_pack, encoding='utf-8')))
MOVIES = json.load(open(os.path.join(DATA_DIR, 'cartoony_movies.json'), encoding='utf-8'))
DISNEY_EXTERNAL = json.load(open(os.path.join(DATA_DIR, 'disney_external.json'), encoding='utf-8'))
CARTOONY_VIDEOS = {
    ep['video_id']
    for show in list(CARTOONY.values()) + list(MOVIES.values())
    for ep in show.get('episodes', [])
}
AUTO = Discovery(DATA_DIR, CARTOONY, MOVIES, CARTOONY_VIDEOS)
CHANNEL_EXTRA = json.load(open(os.path.join(DATA_DIR, 'auto_channels.json'), encoding='utf-8'))

CHANNEL_NAMES = {
    'mbc3': 'ذكريات MBC3',
    'nick': 'ذكريات نيكلوديون',
    'cn': 'ذكريات كرتون نتورك بالعربية',
    'spacetoon': 'ذكريات سبيستون',
}

CARTOON_MANIFEST = {
    'id': 'dodo.cartoonmemories.catalog',
    'version': '2.0.0',
    'name': 'ذكريات كرتون العرب وديزني',
    'description': 'مكتبة مسلسلات وأفلام الكرتون وديزني المدبلجة مع دعم كامل للآيفون والأندرويد (دون ضمان الدبلجة الأصلية).',
    'resources': ['catalog', 'meta', 'stream'],
    'types': ['series', 'movie'],
    'idPrefixes': ['tt', 'cartoonymem:', 'cartoonymovie:'],
    'catalogs': (
        [
            {
                'type': 'series',
                'id': k,
                'name': v,
                'extra': [{'name': 'search', 'isRequired': False}, {'name': 'skip', 'isRequired': False}],
            }
            for k, v in CHANNEL_NAMES.items()
        ]
        + [
            {
                'type': 'series',
                'id': 'auto_popular',
                'name': 'الرائج في الكرتون',
                'extra': [{'name': 'search', 'isRequired': False}, {'name': 'skip', 'isRequired': False}],
            },
            {
                'type': 'series',
                'id': 'auto_dubbed',
                'name': 'أعمال كرتون أخرى',
                'extra': [{'name': 'search', 'isRequired': False}, {'name': 'skip', 'isRequired': False}],
            },
            {
                'type': 'movie',
                'id': 'disney_films',
                'name': 'أفلام ديزني المدبلجة',
                'extra': [{'name': 'search', 'isRequired': False}, {'name': 'skip', 'isRequired': False}],
            },
            {
                'type': 'movie',
                'id': 'cartoony_films',
                'name': 'أفلام الكرتون المدبلجة',
                'extra': [{'name': 'search', 'isRequired': False}, {'name': 'skip', 'isRequired': False}],
            },
        ]
    ),
}

SUBTITLES_MANIFEST = {
    'id': 'dodo.arabic.subtitles',
    'version': '2.0.0',
    'name': 'ترجمة دودو العربية الشاملة',
    'description': 'ترجمة عربية فورية بصيغة WebVTT متوافقة مع شاشة الآيفون الكاملة والأندرويد (OpenSubtitles + SubDL + محلي).',
    'resources': ['subtitles'],
    'types': ['movie', 'series'],
    'idPrefixes': ['tt', 'kitsu'],
    'catalogs': [],
}

BIDOOO_BASE = 'https://ozpwgjjkvrpxbeuhmmdr.supabase.co/functions/v1/bidooo-addon'
OPENSUBS_BASE = 'https://opensubtitles-v3.strem.io'

POOL = ThreadPoolExecutor(max_workers=10)
STREAM_CACHE = {}
STREAM_LOCK = threading.Lock()
QUALITY_CACHE = {}
QUALITY_LOCK = threading.Lock()
PROXY_CACHE = {}
PROXY_LOCK = threading.Lock()


def name_key(name):
    name = str(name).split('|')[0]
    name = re.sub(r'\([^)]*\)', '', name)
    name = re.sub(r'[^\w\u0600-\u06ff ]', ' ', name)
    return (
        re.sub(r'\s+', ' ', name)
        .strip()
        .replace('أ', 'ا')
        .replace('إ', 'ا')
        .replace('آ', 'ا')
        .replace('ى', 'ي')
        .casefold()
    )


# ---------------------------------------------------------------------------
# SSRF-Safe Outbound HTTP Helper
# ---------------------------------------------------------------------------
ALLOWED_LOCAL_PORTS = {8788, 8789, 8790, 18761}


def validate_outbound_url(url, allow_local_addons=False):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('invalid url scheme')
    host = parsed.hostname.lower()
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    if allow_local_addons and host in ('127.0.0.1', 'localhost') and port in ALLOWED_LOCAL_PORTS:
        return url
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise ValueError('dns resolution failed') from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError('blocked private address')
    return url


def fetch_json(url, timeout=10, allow_local=False, cache_ttl=0):
    if cache_ttl > 0:
        with PROXY_LOCK:
            hit = PROXY_CACHE.get(url)
            if hit and time.time() - hit[0] < cache_ttl:
                return hit[1]
    validate_outbound_url(url, allow_local_addons=allow_local)
    req = urllib.request.Request(
        url,
        headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
            'Accept': 'application/json',
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read(6_000_000))
    if cache_ttl > 0:
        with PROXY_LOCK:
            if len(PROXY_CACHE) > 500:
                PROXY_CACHE.clear()
            PROXY_CACHE[url] = (time.time(), data)
    return data


# ---------------------------------------------------------------------------
# Cartoon Streams & Quality Discovery
# ---------------------------------------------------------------------------
def get_cartoony_qualities(video_id):
    with QUALITY_LOCK:
        cached = QUALITY_CACHE.get(video_id)
        if cached and time.time() - cached[0] < 900:
            return cached[1], True
    try:
        req = urllib.request.Request(
            f'https://pegasus.5387692.xyz/api/hls/{video_id}/playlist.m3u8',
            headers={
                'User-Agent': 'Mozilla/5.0',
                'Referer': 'https://cartoony.net/',
                'Origin': 'https://cartoony.net',
            },
        )
        with urllib.request.urlopen(req, timeout=6) as r:
            master = r.read(1_000_000).decode('utf-8')
        if not master.lstrip().startswith('#EXTM3U'):
            raise ValueError('invalid m3u8')
        found = []
        prev = ''
        for rawline in master.splitlines():
            line = rawline.strip()
            if line.startswith('#EXT-X-STREAM-INF:'):
                prev = line
                continue
            if not line or line.startswith('#'):
                continue
            if prev:
                m = re.fullmatch(r'([1-9][0-9]{1,3}p)/playlist\.m3u8', line)
                if m and m.group(1) not in found:
                    found.append(m.group(1))
                prev = ''
        if not found:
            raise ValueError('no variants')
        found.sort(key=lambda q: (q != '480p', int(q[:-1])))
        with QUALITY_LOCK:
            QUALITY_CACHE[video_id] = (time.time(), found)
        return found, True
    except Exception:
        return ['480p', '720p', '360p'], False


def build_cartoony_streams(base_origin, media_sig, video_id, title, status_note):
    prefix = f'{base_origin}/cartoony-hls/{video_id}'
    qs = f'?sig={media_sig}' if media_sig else ''
    available, confirmed = get_cartoony_qualities(video_id)
    hls_hints = {'notWebReady': False}
    out = []
    if confirmed and len(available) > 1:
        out.append(
            {
                'name': 'دودو • تلقائي 📱',
                'description': f'{title}\nجودة تلقائية ذكية (HLS فوري لكل الأجهزة) • {status_note}',
                'url': f'{prefix}/playlist.m3u8{qs}',
                'behaviorHints': hls_hints,
            }
        )
    for q in available:
        note = 'جودة مؤكدة' if confirmed else 'جودة احتياطية'
        if q == '480p':
            note += ' • سريعة ومستقرة'
        elif q == '360p':
            note += ' • قد تتعطل بعض المقاطع من المصدر'
        out.append(
            {
                'name': f'دودو • {q} 📱',
                'description': f'{title}\n{note} • {status_note}',
                'url': f'{prefix}/{q}/playlist.m3u8{qs}',
                'behaviorHints': hls_hints,
            }
        )
    return out


def query_stardima(episode_id, kind='series'):
    try:
        url = f'https://krtoonzaman.albranmaze7.workers.dev/stream/{kind}/{episode_id}.json'
        data = fetch_json(url, timeout=8)
        out = []
        for item in data.get('streams', [])[:10]:
            u = str(item.get('url', ''))
            if not u.startswith(('https://', 'http://')):
                continue
            title = str(item.get('title', ''))[:200]
            if 'مدبلج' not in title:
                continue
            s = dict(item)
            s['name'] = 'كرتون زمان • مدبلج'
            s['title'] = f"{item.get('name', '')}\n{title}"
            out.append(s)
        return out
    except Exception:
        return []


def inspect_or_decorate_bidooo_stream(item, base_origin, media_sig):
    url = str(item.get('url', ''))
    if not url.startswith(('https://', 'http://')):
        return []
    raw_title = str(item.get('title', '') or item.get('description', '') or 'نسخة مدبلجة / عربية')[:220]
    is_mkv = '.mkv' in raw_title.lower() or '.mkv' in url.lower() or 'remux' in raw_title.lower() or 'bluray' in raw_title.lower() or 'h265' in raw_title.lower()
    enc_u = urllib.parse.quote(url, safe='')
    sid = hashlib.sha1(url.encode('utf-8')).hexdigest()[:16]
    uhls_url = f'{base_origin}/api/universal-hls/{sid}/index.m3u8?url={enc_u}&sig={media_sig}' if HAS_FFMPEG else ''
    proxy_url = f'{base_origin}/api/stream-proxy?url={enc_u}&sig={media_sig}'
    results = []
    if HAS_FFMPEG:
        results.append(
            {
                'name': 'دودو الشامل • صوت عربي 📱',
                'description': f'{raw_title}\nتحويل HLS فوري يعمل تلقائيًا داخل مشغل Stremio على كل الأجهزة',
                'url': uhls_url,
                'behaviorHints': {'notWebReady': False},
            }
        )
    if not is_mkv:
        results.append(
            {
                'name': 'Bidooo • مباشر 📱',
                'description': raw_title,
                'url': proxy_url,
                'behaviorHints': {'notWebReady': False},
            }
        )
    else:
        results.append(
            {
                'name': 'Bidooo • ملف MKV خام',
                'description': raw_title,
                'url': url,
                'behaviorHints': {'notWebReady': False},
            }
        )
    return results


def find_external_dubbed_streams(item_id, kind, base_origin, media_sig):
    cache_key = f'{kind}:{item_id}:{base_origin}'
    with STREAM_LOCK:
        hit = STREAM_CACHE.get(cache_key)
        if hit and time.time() - hit[0] < 120:
            return hit[1]
    out = []
    try:
        bdata = fetch_json(f'{BIDOOO_BASE}/stream/{kind}/{item_id}.json', timeout=8)
        for s in bdata.get('streams', [])[:8]:
            if isinstance(s, dict):
                out.extend(inspect_or_decorate_bidooo_stream(s, base_origin, media_sig))
    except Exception:
        pass
    out.extend(query_stardima(item_id, kind))
    if out:
        with STREAM_LOCK:
            STREAM_CACHE[cache_key] = (time.time(), out)
    return out


# ---------------------------------------------------------------------------
# Subtitles: SRT / ASS -> WebVTT UTF-8 Converter for Native iPhone Safari
# ---------------------------------------------------------------------------
def decode_arabic_bytes(raw_bytes):
    for enc in ('utf-8-sig', 'utf-8', 'cp1256', 'windows-1256', 'iso-8859-6', 'utf-16'):
        try:
            txt = raw_bytes.decode(enc)
            if '\x00' not in txt:
                return txt
        except UnicodeDecodeError:
            continue
    return raw_bytes.decode('utf-8', errors='replace')


def srt_or_ass_to_vtt(raw_text):
    text = raw_text.replace('\r\n', '\n').replace('\r', '\n').lstrip('\ufeff').strip()
    if text.startswith('WEBVTT'):
        return text + '\n'
    # Handle ASS / SSA format
    if '[Events]' in text and 'Dialogue:' in text:
        cues = ['WEBVTT', '']
        for line in text.splitlines():
            if not line.startswith('Dialogue:'):
                continue
            parts = line[9:].split(',', 9)
            if len(parts) < 10:
                continue
            start_raw, end_raw, dialogue = parts[1].strip(), parts[2].strip(), parts[9].strip()

            def fmt_ass_ts(ts):
                m = re.match(r'^(\d+):(\d{2}):(\d{2})\.(\d{2})$', ts)
                if not m:
                    return None
                h, mn, sc, cs = int(m.group(1)), m.group(2), m.group(3), int(m.group(4)) * 10
                return f'{h:02d}:{mn}:{sc}.{cs:03d}'

            s_ts, e_ts = fmt_ass_ts(start_raw), fmt_ass_ts(end_raw)
            if not s_ts or not e_ts:
                continue
            clean = re.sub(r'\{[^}]*\}', '', dialogue).replace('\\N', '\n').replace('\\n', '\n').strip()
            if clean:
                cues.append(f'{s_ts} --> {e_ts}')
                cues.append(clean)
                cues.append('')
        return '\n'.join(cues) + '\n'

    # Standard SRT -> WebVTT
    lines = text.splitlines()
    out = ['WEBVTT', '']
    ts_re = re.compile(
        r'(\d{1,2}:\d{2}:\d{2})[,.](\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2})[,.](\d{3})(.*)'
    )
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m = ts_re.match(line)
        if m:
            s1 = m.group(1) if len(m.group(1)) == 8 else '0' + m.group(1)
            s2 = m.group(3) if len(m.group(3)) == 8 else '0' + m.group(3)
            out.append(f'{s1}.{m.group(2)} --> {s2}.{m.group(4)}')
            i += 1
            while i < len(lines) and lines[i].strip():
                cue_line = re.sub(r'\{\\[^}]*\}', '', lines[i])
                out.append(cue_line)
                i += 1
            out.append('')
        else:
            i += 1
    return '\n'.join(out) + '\n'


def find_arabic_subtitles(kind, item_id, base_origin, media_sig):
    subs = []
    sig_q = f'&sig={media_sig}' if media_sig else ''
    # 1. Bundled Attack on Titan S3 Arabic SRTs
    m_aot = re.fullmatch(r'tt2560140:3:(\d+)', item_id)
    if m_aot:
        ep = int(m_aot.group(1))
        srt_name = f'Shahid_WEB__Attack.On.Titan.S03E{ep:02d}.1080p.WEBRIP-Shahid.srt'
        local_p = os.path.join(DATA_DIR, 'aot_s3_ar', srt_name)
        if os.path.isfile(local_p):
            subs.append(
                {
                    'id': f'local-aot-s3e{ep:02d}',
                    'lang': 'ara',
                    'label': f'العربية • نسخة متزامنة جاهزة (الحلقة {ep})',
                    'url': f'{base_origin}/api/sub.vtt?aot={ep}{sig_q}',
                }
            )
    # 2. Optional bridge to local Windows SubDL/Universal Arabic addon on 127.0.0.1:8789
    try:
        enc_id = urllib.parse.quote(item_id, safe=':')
        ldata = fetch_json(f'http://127.0.0.1:8789/subtitles/{kind}/{enc_id}.json', timeout=3, allow_local=True)
        for idx, s in enumerate(ldata.get('subtitles', [])[:6]):
            u = str(s.get('url', ''))
            if not u.startswith(('http://', 'https://')):
                continue
            lang = str(s.get('lang', 'ara')).lower()
            if lang not in ('ara', 'ar', 'arabic'):
                continue
            q_u = urllib.parse.quote(u, safe='')
            subs.append(
                {
                    'id': f'subdl-{idx}',
                    'lang': 'ara',
                    'label': s.get('name') or f'العربية • SubDL #{idx + 1}',
                    'url': f'{base_origin}/api/sub.vtt?url={q_u}{sig_q}',
                }
            )
    except Exception:
        pass
    # 3. Official OpenSubtitles v3 (Arabic)
    try:
        enc_id = urllib.parse.quote(item_id, safe=':')
        odata = fetch_json(f'{OPENSUBS_BASE}/subtitles/{kind}/{enc_id}.json', timeout=6, cache_ttl=300)
        ar_list = [
            s
            for s in odata.get('subtitles', [])
            if str(s.get('lang', '')).lower() in ('ara', 'ar', 'arabic') and str(s.get('url', '')).startswith('http')
        ]
        for idx, s in enumerate(ar_list[:8]):
            q_u = urllib.parse.quote(s['url'], safe='')
            subs.append(
                {
                    'id': s.get('id') or f'os-ar-{idx}',
                    'lang': 'ara',
                    'label': f'العربية • OpenSubtitles #{idx + 1}',
                    'url': f'{base_origin}/api/sub.vtt?url={q_u}{sig_q}',
                }
            )
    except Exception:
        pass
    return subs


# ---------------------------------------------------------------------------
# Default Addon Registry
# ---------------------------------------------------------------------------
def get_default_addons(base_origin):
    addons = [
        {
            'id': CARTOON_MANIFEST['id'],
            'name': CARTOON_MANIFEST['name'],
            'description': CARTOON_MANIFEST['description'],
            'transportUrl': f'{base_origin}/addons/cartoons/manifest.json',
            'builtIn': True,
            'manifest': CARTOON_MANIFEST,
        },
        {
            'id': 'com.linvo.cinemeta',
            'name': 'Cinemeta • الأفلام والمسلسلات العالمية',
            'description': 'الفهرس الرسمي الشامل للأفلام والمسلسلات العالمية وتقييمات IMDb والحلقات.',
            'transportUrl': 'https://v3-cinemeta.strem.io/manifest.json',
            'builtIn': True,
        },
        {
            'id': 'pw.ers.netflix-catalog',
            'name': 'كتالوجات المنصات العالمية • Netflix & HBO & Apple TV+ & Prime',
            'description': 'قوائم الأعمال الرائجة على نتفليكس وHBO Max وديزني+ وبرايم فيديو وآبل تي في.',
            'transportUrl': 'https://7a82163c306e-stremio-netflix-catalog-addon.baby-beamup.club/manifest.json',
            'builtIn': True,
        },
        {
            'id': 'org.bidooo.arabic.addon',
            'name': 'Bidooo • أفلام ومسلسلات مدبلجة وعربية',
            'description': 'مكتبة تضم مئات الأفلام والمسلسلات المدبلجة للعربية متوافقة مع معرفات IMDb.',
            'transportUrl': f'{BIDOOO_BASE}/manifest.json',
            'builtIn': True,
        },
        {
            'id': 'anime.kitsu.strem.fun',
            'name': 'Anime Kitsu • مكتبة الأنمي',
            'description': 'فهرس مسلسلات وأفلام الأنمي الياباني مع المواسم والحلقات.',
            'transportUrl': 'https://anime-kitsu.strem.fun/manifest.json',
            'builtIn': True,
        },
        {
            'id': 'org.stremio.publicdomainmovies',
            'name': 'السينما الكلاسيكية المفتوحة • Public Domain',
            'description': 'أفلام عالمية كلاسيكية مفتوحة الحقوق جاهزة للمشاهدة المباشرة.',
            'transportUrl': 'https://caching.stremio.net/publicdomainmovies.now.sh/manifest.json',
            'builtIn': True,
        },
        {
            'id': 'org.stremio.watchhub',
            'name': 'WatchHub • منصات العرض الرسمية',
            'description': 'دليل توفر الأفلام والمسلسلات على المنصات الرسمية.',
            'transportUrl': 'https://watchhub.strem.io/manifest.json',
            'builtIn': True,
        },
        {
            'id': SUBTITLES_MANIFEST['id'],
            'name': SUBTITLES_MANIFEST['name'],
            'description': SUBTITLES_MANIFEST['description'],
            'transportUrl': f'{base_origin}/addons/subtitles/manifest.json',
            'builtIn': True,
            'manifest': SUBTITLES_MANIFEST,
        },
    ]
    # Auto-detect local YouTube addon on 127.0.0.1:8790 if running on owner's PC
    try:
        ym = fetch_json('http://127.0.0.1:8790/manifest.json', timeout=1.2, allow_local=True, cache_ttl=60)
        addons.append(
            {
                'id': ym.get('id', 'org.local.youtube.ar'),
                'name': ym.get('name', 'يوتيوب — بحث وتشغيل فوري'),
                'description': ym.get('description', 'إضافة يوتيوب المحلية من جهازك.'),
                'transportUrl': 'http://127.0.0.1:8790/manifest.json',
                'builtIn': True,
                'manifest': ym,
            }
        )
    except Exception:
        pass
    with CONFIG_LOCK:
        for extra in CFG.get('global_extra_addons', []):
            if isinstance(extra, dict) and extra.get('transportUrl'):
                addons.append(extra)
    return addons


# ---------------------------------------------------------------------------
# HTTP Request Handler
# ---------------------------------------------------------------------------
class DodoHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        pass

    def base_origin(self):
        proto = self.headers.get('X-Forwarded-Proto', '').split(',')[0].strip()
        host = (self.headers.get('X-Forwarded-Host') or self.headers.get('Host') or '127.0.0.1:18880').split(',')[0].strip()
        host_only = host.split(':')[0].lower()
        is_raw_ip = False
        try:
            ipaddress.ip_address(host_only)
            is_raw_ip = True
        except ValueError:
            pass
        if host_only not in ('127.0.0.1', 'localhost') and not is_raw_ip and '.' in host_only:
            proto = 'https'
        elif not proto:
            proto = 'http'
        return f'{proto}://{host}'

    def _session_cookie_header(self, token):
        is_https = self.base_origin().startswith('https://')
        samesite = 'SameSite=None; Secure' if is_https else 'SameSite=Lax'
        return f'dodo_session={token}; Path=/; Max-Age=7776000; {samesite}'

    def is_local_or_preview_request(self):
        host = (self.headers.get('X-Forwarded-Host') or self.headers.get('Host') or '').split(':')[0].lower()
        client_ip = self.client_address[0]
        if host in ('127.0.0.1', 'localhost') and not self.headers.get('CF-Connecting-IP'):
            return True
        if host.endswith('.e2b.app'):
            return True
        try:
            host_ip = ipaddress.ip_address(host)
            ip = ipaddress.ip_address(client_ip)
            if (host_ip.is_private or host_ip.is_loopback) and (ip.is_private or ip.is_loopback) and not self.headers.get('CF-Connecting-IP'):
                return True
        except ValueError:
            pass
        return False

    def is_authorized(self, query_params):
        with CONFIG_LOCK:
            mode = CFG.get('privacy_mode', 'internet_only')
            expected_code = str(CFG.get('invite_code', ''))
            expected_session = make_session_token(CFG)
            expected_sig = make_media_sig(CFG)
        if mode == 'off':
            return True
        if mode == 'internet_only' and self.is_local_or_preview_request():
            return True
        # Check media signature (?sig=...)
        sig = query_params.get('sig', [''])[0]
        if sig and hmac.compare_digest(sig, expected_sig):
            return True
        # Check invite code in query (?invite=...)
        inv = query_params.get('invite', [''])[0].strip()
        if inv and hmac.compare_digest(inv, expected_code):
            return True
        # Check header X-Dodo-Invite
        hdr = self.headers.get('X-Dodo-Invite', '').strip()
        if hdr and hmac.compare_digest(hdr, expected_code):
            return True
        # Check session cookie
        raw_cookie = self.headers.get('Cookie', '')
        if raw_cookie:
            c = SimpleCookie()
            try:
                c.load(raw_cookie)
                if 'dodo_session' in c and hmac.compare_digest(c['dodo_session'].value, expected_session):
                    return True
            except Exception:
                pass
        return False

    def add_privacy_headers(self):
        self.send_header('X-Robots-Tag', 'noindex, nofollow, noarchive')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, HEAD, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Dodo-Invite, X-Dodo-Admin, Range')
        self.send_header('Access-Control-Expose-Headers', 'Content-Type, Content-Length, Content-Range, Accept-Ranges')

    def _accepts_gzip(self):
        return 'gzip' in (self.headers.get('Accept-Encoding', '') or '').lower()

    def _is_compressible_type(self, content_type):
        ct = (content_type or '').lower()
        return any(x in ct for x in ('json', 'javascript', 'text/', 'xml', 'svg', 'wasm', 'ttf'))

    def send_json(self, status, obj, extra_headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        use_gz = len(body) >= 1024 and self._accepts_gzip()
        if use_gz:
            body = gzip.compress(body, compresslevel=5)
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        if use_gz:
            self.send_header('Content-Encoding', 'gzip')
        self.send_header('Vary', 'Accept-Encoding')
        self.send_header('Content-Length', str(len(body)))
        self.add_privacy_headers()
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        if getattr(self, 'command', 'GET') != 'HEAD':
            self.wfile.write(body)

    def send_bytes(self, status, body, content_type, cache='public, max-age=3600', extra_headers=None, pre_gzipped=False):
        use_gz = pre_gzipped
        if not pre_gzipped and len(body) >= 1024 and self._is_compressible_type(content_type) and self._accepts_gzip():
            body = gzip.compress(body, compresslevel=5)
            use_gz = True
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', cache)
        if use_gz:
            self.send_header('Content-Encoding', 'gzip')
        if self._is_compressible_type(content_type):
            self.send_header('Vary', 'Accept-Encoding')
        self.send_header('Content-Length', str(len(body)))
        self.add_privacy_headers()
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        if getattr(self, 'command', 'GET') != 'HEAD':
            self.wfile.write(body)

    def send_media_range(self, body, content_type):
        size = len(body)
        start, end = 0, size - 1
        rh = self.headers.get('Range', '')
        code = 200
        if rh:
            m = re.fullmatch(r'bytes=(\d*)-(\d*)', rh.strip())
            if not m or (not m.group(1) and not m.group(2)):
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{size}')
                self.send_header('Content-Length', '0')
                self.add_privacy_headers()
                self.end_headers()
                return
            if m.group(1):
                start = int(m.group(1))
                end = min(size - 1, int(m.group(2))) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            if start >= size or end < start:
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{size}')
                self.send_header('Content-Length', '0')
                self.add_privacy_headers()
                self.end_headers()
                return
            code = 206
        part = body[start : end + 1]
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(part)))
        self.send_header('Accept-Ranges', 'bytes')
        self.add_privacy_headers()
        if code == 206:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.end_headers()
        if getattr(self, 'command', 'GET') != 'HEAD':
            self.wfile.write(part)

    def do_HEAD(self):
        return self.do_GET()

    def do_OPTIONS(self):
        self.send_response(204)
        self.add_privacy_headers()
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip('/') or '/'
        length = min(int(self.headers.get('Content-Length', '0') or '0'), 2_000_000)
        raw = self.rfile.read(length) if length > 0 else b'{}'

        # Forward POST calls destined for local Stremio Streaming Server (/stremio-engine/... or root /<infoHash>/create)
        is_root_engine_post = bool(re.match(r'^/([0-9a-fA-F]{40}/create|settings|hlsv2/)', parsed.path))
        if parsed.path.startswith('/stremio-engine/') or is_root_engine_post:
            pq = urllib.parse.parse_qs(parsed.query)
            if not self.is_authorized(pq):
                return self.send_json(401, {'error': 'يتطلب رمز الدعوة'})
            sub = parsed.path[len('/stremio-engine/') :] if parsed.path.startswith('/stremio-engine/') else parsed.path.lstrip('/')
            if '..' in sub:
                return self.send_json(400, {'error': 'مسار غير صالح'})
            if not is_stremio_engine_alive() and not ensure_embedded_stremio_engine():
                return self.send_json(503, {'error': 'محرك البث المحلي غير متصل'})
            fwd_pairs = [(k, v) for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True) if k != 'sig']
            fwd_qs = urllib.parse.urlencode(fwd_pairs)
            upstream = f'{STREMIO_ENGINE_URL}/{sub}' + (f'?{fwd_qs}' if fwd_qs else '')
            try:
                req = urllib.request.Request(
                    upstream,
                    data=raw,
                    headers={'Content-Type': self.headers.get('Content-Type', 'application/json')},
                    method='POST',
                )
                with urllib.request.urlopen(req, timeout=20) as r:
                    return self.send_bytes(r.status, r.read(4_000_000), r.headers.get('Content-Type', 'application/json'), cache='no-store')
            except Exception as exc:
                return self.send_json(502, {'error': str(exc)[:120]})

        try:
            payload = json.loads(raw.decode('utf-8'))
        except Exception:
            return self.send_json(400, {'error': 'طلب غير صالح'})

        if path == '/api/auth/verify':
            code = str(payload.get('invite_code', '')).strip()
            with CONFIG_LOCK:
                ok = hmac.compare_digest(code, str(CFG.get('invite_code', '')))
                token = make_session_token(CFG)
            if not ok:
                return self.send_json(401, {'ok': False, 'error': 'رمز الدعوة غير صحيح'})
            cookie = self._session_cookie_header(token)
            return self.send_json(200, {'ok': True, 'session': token}, extra_headers={'Set-Cookie': cookie})

        if path == '/api/admin/update':
            pin = payload.get('admin_pin') or self.headers.get('X-Dodo-Admin', '')
            if not verify_admin(pin):
                return self.send_json(403, {'ok': False, 'error': 'رمز المالك غير صحيح'})
            with CONFIG_LOCK:
                if 'app_name' in payload and str(payload['app_name']).strip():
                    CFG['app_name'] = str(payload['app_name']).strip()[:40]
                if 'invite_code' in payload and str(payload['invite_code']).strip():
                    CFG['invite_code'] = str(payload['invite_code']).strip()[:40]
                if payload.get('privacy_mode') in ('always', 'internet_only', 'off'):
                    CFG['privacy_mode'] = payload['privacy_mode']
                if 'new_admin_pin' in payload and str(payload['new_admin_pin']).strip():
                    CFG['admin_pin'] = str(payload['new_admin_pin']).strip()[:30]
                if isinstance(payload.get('global_extra_addons'), list):
                    CFG['global_extra_addons'] = payload['global_extra_addons'][:25]
                save_config(CFG)
                token = make_session_token(CFG)
            cookie = self._session_cookie_header(token)
            return self.send_json(
                200,
                {
                    'ok': True,
                    'app_name': CFG['app_name'],
                    'invite_code': CFG['invite_code'],
                    'privacy_mode': CFG['privacy_mode'],
                    'global_extra_addons': CFG['global_extra_addons'],
                },
                extra_headers={'Set-Cookie': cookie},
            )

        if path == '/api/resolve-link':
            parsed_q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if not self.is_authorized(parsed_q):
                return self.send_json(401, {'ok': False, 'error': 'يتطلب رمز دعوة'})
            raw_url = str(payload.get('url', '')).strip()
            origin = self.base_origin()
            with CONFIG_LOCK:
                media_sig = make_media_sig(CFG)
            code, resp = resolve_any_link(
                raw_url,
                origin,
                media_sig,
                HAS_FFMPEG,
                STREMIO_ENGINE_URL,
                is_stremio_engine_alive,
                validate_outbound_url,
            )
            # Store internal adapter transportUrls as origin-agnostic relative paths so they work on Wi-Fi, Tunnel & Localhost!
            if resp.get('ok') and resp.get('kind') == 'addon':
                tu = str(resp.get('transportUrl', ''))
                if tu.startswith(origin + '/api/'):
                    resp['transportUrl'] = tu[len(origin):]
            return self.send_json(code, resp)

        if path == '/api/admin/inspect':
            pin = payload.get('admin_pin') or self.headers.get('X-Dodo-Admin', '')
            if not verify_admin(pin):
                return self.send_json(403, {'ok': False, 'error': 'رمز المالك غير صحيح'})
            with CONFIG_LOCK:
                return self.send_json(
                    200,
                    {
                        'ok': True,
                        'app_name': CFG['app_name'],
                        'invite_code': CFG['invite_code'],
                        'privacy_mode': CFG['privacy_mode'],
                        'global_extra_addons': CFG['global_extra_addons'],
                        'ffmpeg_available': HAS_FFMPEG,
                    },
                )

        return self.send_json(404, {'error': 'غير موجود'})

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        raw_path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        # Always block search engines
        if raw_path == '/robots.txt':
            return self.send_bytes(200, b'User-agent: *\nDisallow: /\n', 'text/plain; charset=utf-8')

        # Public assets required for PWA install & Official Stremio Web v5.2.0 Shell
        if raw_path == '/apple-touch-icon.png':
            return self.serve_static_file('icons/apple-touch-icon.png')
        if raw_path == '/favicon.ico' or raw_path == '/favicon.png':
            return self.serve_static_file('icons/favicon-64.png')
        if raw_path in ('/manifest.webmanifest', '/manifest.json'):
            inv = params.get('invite', [''])[0].strip()
            start_url = f'/?invite={urllib.parse.quote(inv)}' if inv else '/'
            with CONFIG_LOCK:
                app_name = CFG.get('app_name', 'دودو ستريم')
            manifest = {
                'name': app_name,
                'short_name': app_name,
                'description': 'منصة دودو ستريم المفتوحة المصدر — متوافقة 100% مع Stremio v5',
                'lang': 'ar',
                'dir': 'rtl',
                'start_url': start_url,
                'scope': '/',
                'display': 'standalone',
                'orientation': 'any',
                'background_color': '#0b0a15',
                'theme_color': '#0b0a15',
                'icons': [
                    {'src': '/static/icons/icon-192.png', 'sizes': '192x192', 'type': 'image/png', 'purpose': 'any maskable'},
                    {'src': '/static/icons/icon-512.png', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'any maskable'},
                ],
            }
            return self.send_json(200, manifest)
        if raw_path in ('/sw.js', '/service-worker.js'):
            sw_code = (
                "const CACHE='dodo-stremio-v8';\n"
                "self.addEventListener('install',()=>self.skipWaiting());\n"
                "self.addEventListener('activate',e=>e.waitUntil((async()=>{\n"
                "  for(const k of await caches.keys()){if(k!==CACHE)await caches.delete(k);}\n"
                "  await self.clients.claim();\n"
                "})()));\n"
                "self.addEventListener('fetch',e=>{\n"
                "  const u=new URL(e.request.url);\n"
                "  if(e.request.method!=='GET'||u.origin!==self.location.origin)return;\n"
                "  if(u.pathname.endsWith('.wasm')||u.pathname.includes('/fonts/')||(/\\/(main|790|513|186)\\.(js|css)$/.test(u.pathname))){\n"
                "    e.respondWith(caches.open(CACHE).then(async c=>{const hit=await c.match(e.request);if(hit)return hit;const r=await fetch(e.request);if(r.ok)c.put(e.request,r.clone());return r;}));\n"
                "  }\n"
                "});\n"
            ).encode('utf-8')
            return self.send_bytes(200, sw_code, 'application/javascript; charset=utf-8', cache='no-cache')
        if raw_path.startswith('/static/'):
            rel = raw_path[len('/static/') :]
            c_hdr = 'no-cache' if rel.startswith('dodo_') else 'public, max-age=86400'
            return self.serve_static_file(rel, cache=c_hdr)
        if raw_path == '/classic':
            return self.serve_static_file('index.html', cache='no-store')
        if raw_path in ('/', '/index.html'):
            extra = {}
            inv = params.get('invite', [''])[0].strip()
            with CONFIG_LOCK:
                app_name = CFG.get('app_name', 'دودو ستريم')
                if inv and hmac.compare_digest(inv, str(CFG.get('invite_code', ''))):
                    extra['Set-Cookie'] = self._session_cookie_header(make_session_token(CFG))
            stremio_idx = os.path.join(STREMIO_WEB_DIR, 'index.html')
            if os.path.isfile(stremio_idx):
                html = open(stremio_idx, encoding='utf-8').read()
                html = html.replace('<meta name="apple-mobile-web-app-title" content="Stremio">', f'<meta name="apple-mobile-web-app-title" content="{app_name}">')
                html = html.replace('<title>Stremio - Freedom to Stream</title>', f'<title>{app_name}</title>')
                if inv:
                    html = html.replace('href="manifest.json"', f'href="/manifest.json?invite={urllib.parse.quote(inv)}"')
                html = html.replace('href="images/icon_196x196.png"', 'href="/apple-touch-icon.png"')
                html = html.replace('href="favicons/favicon.ico"', 'href="/favicon.png"')
                html = html.replace('</head>', '<link rel="preconnect" href="https://v3-cinemeta.strem.io" crossorigin><link rel="preconnect" href="https://torrentio.strem.fun" crossorigin><link href="/static/dodo_stremio_overlay.css?v=8" rel="stylesheet"></head>')
                html = html.replace(
                    '<script src="296e65dd5a00319c808b6e270fcccab44b8e7a79/scripts/main.js"></script>',
                    '<script src="/static/dodo_stremio_bridge.js?v=12"></script><script src="296e65dd5a00319c808b6e270fcccab44b8e7a79/scripts/main.js"></script>',
                )
                return self.send_bytes(200, html.encode('utf-8'), 'text/html; charset=utf-8', cache='no-store', extra_headers=extra)
            return self.serve_static_file('index.html', cache='no-store', extra_headers=extra)

        # Serve Official Stremio Web v5.2.0 Assets (WASM, Bundles, Fonts, Images, Worker with Dodo Hook)
        if raw_path.startswith((
            '/296e65dd5a00319c808b6e270fcccab44b8e7a79/',
            '/fonts/',
            '/images/',
            '/favicons/',
            '/screenshots/',
            '/.well-known/',
        )) or raw_path == '/workbox-ee13fbdc.js':
            rel_s = raw_path.lstrip('/')
            if rel_s.endswith('/scripts/worker.js'):
                w_file = os.path.join(STREMIO_WEB_DIR, rel_s.replace('/', os.sep))
                if os.path.isfile(w_file):
                    orig_w = open(w_file, 'rb').read()
                    patched_w = (
                        b"if(typeof importScripts==='function'){try{importScripts('/static/dodo_worker_hook.js?v=6');}catch(e){}}\n"
                        + orig_w
                        + b"\nif(typeof self!=='undefined'&&typeof self.__dodoWrapWorkerInit==='function'){self.__dodoWrapWorkerInit();}\n"
                    )
                    return self.send_bytes(200, patched_w, 'application/javascript; charset=utf-8', cache='no-cache')
            imm_cache = (
                'public, max-age=31536000, immutable'
                if raw_path.startswith(('/296e65dd5a00319c808b6e270fcccab44b8e7a79/', '/fonts/'))
                else 'public, max-age=604800'
            )
            return self.serve_stremio_web_file(rel_s, cache=imm_cache)

        # Session & App Bootstrap Status
        if raw_path == '/api/bootstrap':
            authed = self.is_authorized(params)
            origin = self.base_origin()
            with CONFIG_LOCK:
                app_name = CFG.get('app_name', 'دودو ستريم')
                app_sub = CFG.get('app_subtitle', '')
                pmode = CFG.get('privacy_mode', 'internet_only')
                sig = make_media_sig(CFG) if authed else ''
            if not authed:
                return self.send_json(
                    200,
                    {
                        'authorized': False,
                        'app_name': app_name,
                        'app_subtitle': app_sub,
                        'privacy_mode': pmode,
                    },
                )
            return self.send_json(
                200,
                {
                    'authorized': True,
                    'app_name': app_name,
                    'app_subtitle': app_sub,
                    'privacy_mode': pmode,
                    'media_sig': sig,
                    'capabilities': {
                        'ffmpeg_remux': HAS_FFMPEG,
                        'cartoon_series': len(CARTOONY),
                        'cartoon_movies': len(MOVIES) + len(DISNEY_EXTERNAL),
                    },
                    'default_addons': get_default_addons(origin),
                },
            )

        # Enforce Privacy Gate on all protected API & media endpoints
        if not self.is_authorized(params):
            return self.send_json(401, {'error': 'يتطلب رمز دعوة خاص للدخول إلى دودو ستريم'})

        origin = self.base_origin()
        with CONFIG_LOCK:
            media_sig = make_media_sig(CFG)

        # -------------------------------------------------------------------
        # 1. HLS Proxy for Cartoony (with iPhone Range 206 & Dynamic Segment URLs)
        # -------------------------------------------------------------------
        if raw_path.startswith('/cartoony-hls/'):
            m = re.fullmatch(
                r'/cartoony-hls/([0-9a-fA-F-]{1,64})/(playlist\.m3u8|([1-9][0-9]{1,3}p)/(playlist\.m3u8|segment_[0-9]+\.ts))',
                raw_path,
            )
            if not m or m.group(1) not in CARTOONY_VIDEOS:
                return self.send_json(404, {'error': 'الفيديو غير موجود'})
            ident, part = m.group(1), m.group(2)
            if getattr(self, 'command', 'GET') == 'HEAD' and part.endswith('.m3u8'):
                return self.send_bytes(200, b'', 'application/vnd.apple.mpegurl', cache='no-store')
            upstream_part = part[:-3] + '.jpg' if part.endswith('.ts') else part
            upstream = f'https://pegasus.5387692.xyz/api/hls/{ident}/{upstream_part}'
            try:
                req = urllib.request.Request(
                    upstream,
                    headers={
                        'User-Agent': 'Mozilla/5.0',
                        'Referer': 'https://cartoony.net/',
                        'Origin': 'https://cartoony.net',
                    },
                )
                with urllib.request.urlopen(req, timeout=16) as r:
                    if part.endswith('.m3u8'):
                        txt = r.read(1_000_000).decode('utf-8').replace('.jpg', '.ts')
                        # Attach ?sig=... to child playlists & segments so iOS Safari & VLC/Outplayer authenticate seamlessly!
                        rewritten = []
                        for ln in txt.splitlines():
                            s_ln = ln.strip()
                            if s_ln and not s_ln.startswith('#'):
                                joiner = '&' if '?' in s_ln else '?'
                                rewritten.append(f'{s_ln}{joiner}sig={media_sig}')
                            else:
                                rewritten.append(ln)
                        body = ('\n'.join(rewritten) + '\n').encode('utf-8')
                        return self.send_media_range(body, 'application/vnd.apple.mpegurl')
                    else:
                        body = r.read(25 * 1024 * 1024 + 1)
                        if len(body) > 25 * 1024 * 1024:
                            return self.send_json(502, {'error': 'المقطع كبير جدًا'})
                        return self.send_media_range(body, 'video/mp2t')
            except Exception:
                return self.send_json(502, {'error': 'تعذر جلب المقطع من المصدر'})

        # -------------------------------------------------------------------
        # 2. WebVTT Subtitle Converter Endpoint (/api/sub.vtt & /subtitles.vtt)
        # -------------------------------------------------------------------
        if raw_path in ('/api/sub.vtt', '/subtitles.vtt'):
            aot_ep = params.get('aot', [''])[0]
            sub_url = params.get('url', [''])[0] or params.get('from', [''])[0]
            try:
                if aot_ep and aot_ep.isdigit():
                    ep = int(aot_ep)
                    p_srt = os.path.join(
                        DATA_DIR,
                        'aot_s3_ar',
                        f'Shahid_WEB__Attack.On.Titan.S03E{ep:02d}.1080p.WEBRIP-Shahid.srt',
                    )
                    raw_bytes = open(p_srt, 'rb').read()
                elif sub_url:
                    validate_outbound_url(sub_url, allow_local_addons=True)
                    req = urllib.request.Request(sub_url, headers={'User-Agent': 'Mozilla/5.0 DodoStream/2.0'})
                    with urllib.request.urlopen(req, timeout=12) as r:
                        raw_bytes = r.read(4_000_000)
                else:
                    return self.send_json(400, {'error': 'محدد الترجمة مفقود'})
                vtt = srt_or_ass_to_vtt(decode_arabic_bytes(raw_bytes)).encode('utf-8')
                return self.send_bytes(200, vtt, 'text/vtt; charset=utf-8', cache='public, max-age=600')
            except Exception:
                return self.send_bytes(200, b'WEBVTT\n\n', 'text/vtt; charset=utf-8', cache='no-store')

        # -------------------------------------------------------------------
        # 3. Smart iPhone Live Remuxer (MKV -> Fragmented MP4 with Arabic AAC)
        # -------------------------------------------------------------------
        if raw_path == '/api/remux':
            if not HAS_FFMPEG:
                return self.send_json(501, {'error': 'FFmpeg غير متوفر على هذا الخادم؛ استخدم زر VLC أو Outplayer'})
            src_url = params.get('url', [''])[0]
            try:
                validate_outbound_url(src_url, allow_local_addons=False)
            except ValueError:
                return self.send_json(400, {'error': 'رابط وسائط غير مسموح'})
            # Probe video codec & Arabic audio stream index quickly if ffprobe exists
            audio_map = '0:a:0?'
            vtag_args = []
            if HAS_FFPROBE:
                try:
                    pr = subprocess.run(
                        [
                            FFPROBE,
                            '-v',
                            'error',
                            '-show_entries',
                            'stream=index,codec_type,codec_name:stream_tags=language',
                            '-of',
                            'json',
                            '-analyzeduration',
                            '1500000',
                            '-probesize',
                            '1500000',
                            src_url,
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        timeout=10,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                    )
                    if pr.returncode == 0:
                        streams = json.loads(pr.stdout).get('streams', [])
                        for st in streams:
                            if st.get('codec_type') == 'video' and st.get('codec_name') in ('hevc', 'h265'):
                                vtag_args = ['-tag:v', 'hvc1']
                                break
                        for st in streams:
                            if st.get('codec_type') == 'audio':
                                lang = str(st.get('tags', {}).get('language', '')).lower()
                                if lang in ('ara', 'ar', 'arabic'):
                                    audio_map = f"0:{st['index']}"
                                    break
                except Exception:
                    pass

            cmd = (
                [
                    FFMPEG,
                    '-v',
                    'error',
                    '-reconnect',
                    '1',
                    '-reconnect_streamed',
                    '1',
                    '-i',
                    src_url,
                    '-map',
                    '0:v:0',
                    '-map',
                    audio_map,
                    '-c:v',
                    'copy',
                ]
                + vtag_args
                + [
                    '-c:a',
                    'aac',
                    '-ac',
                    '2',
                    '-b:a',
                    '160k',
                    '-movflags',
                    'frag_keyframe+empty_moov+default_base_moof',
                    '-f',
                    'mp4',
                    'pipe:1',
                ]
            )
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
            try:
                first_chunk = proc.stdout.read(65536)
                if not first_chunk:
                    proc.kill()
                    return self.send_json(502, {'error': 'تعذر بدء التحويل المباشر؛ استخدم Outplayer أو VLC'})
                self.send_response(200)
                self.send_header('Content-Type', 'video/mp4')
                self.send_header('Cache-Control', 'no-store')
                self.add_privacy_headers()
                self.end_headers()
                self.wfile.write(first_chunk)
                while True:
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except Exception:
                    pass
            return

        # -------------------------------------------------------------------
        # 4. Built-in Subtitles Addon Routes (/addons/subtitles/...)
        # -------------------------------------------------------------------
        if raw_path.startswith('/addons/subtitles/'):
            sub_rel = raw_path[len('/addons/subtitles/') :]
            if sub_rel == 'manifest.json':
                return self.send_json(200, SUBTITLES_MANIFEST)
            parts = sub_rel.split('/')
            if len(parts) == 3 and parts[0] == 'subtitles':
                kind = parts[1]
                item_id = urllib.parse.unquote(parts[2]).removesuffix('.json')
                return self.send_json(200, {'subtitles': find_arabic_subtitles(kind, item_id, origin, media_sig)})
            return self.send_json(404, {'error': 'مسار ترجمة غير معروف'})

        # -------------------------------------------------------------------
        # 5. Built-in Cartoon & Disney Addon Routes (/addons/cartoons/...)
        # -------------------------------------------------------------------
        if raw_path.startswith('/addons/cartoons/'):
            rel = raw_path[len('/addons/cartoons/') :]
            return self.handle_cartoon_addon(rel, origin, media_sig)

        # -------------------------------------------------------------------
        # 5a. Universal Live FFmpeg HLS Transcoder (/api/universal-hls/<sid>/<part>)
        # -------------------------------------------------------------------
        if raw_path.startswith('/api/universal-hls/'):
            if not HAS_FFMPEG:
                return self.send_json(501, {'error': 'FFmpeg غير متوفر'})
            m_uhls = re.fullmatch(r'/api/universal-hls/([0-9a-f]{8,32})/(index\.m3u8|seg[0-9]{4}\.ts)', raw_path)
            if not m_uhls:
                return self.send_json(400, {'error': 'مسار HLS غير صالح'})
            sid, part_name = m_uhls.group(1), m_uhls.group(2)
            if part_name == 'index.m3u8':
                src_u = params.get('url', [''])[0].strip()
                raw_hdrs = params.get('hdrs', ['{}'])[0]
                if not src_u:
                    sess = UHLS.get_session(sid)
                else:
                    try:
                        if not src_u.lower().startswith(('rtmp://', 'rtsp://', 'mms://')):
                            validate_outbound_url(src_u, allow_local_addons=False)
                        parsed_hdrs = json.loads(raw_hdrs) if raw_hdrs else {}
                    except Exception:
                        return self.send_json(400, {'error': 'رابط وسائط غير صالح'})
                    sess = UHLS.ensure_session(sid, src_u, parsed_hdrs)
                if not sess:
                    return self.send_json(404, {'error': 'انتهت جلسة البث'})
                m3u8_file = os.path.join(sess['dir'], 'index.m3u8')
                if not os.path.isfile(m3u8_file):
                    return self.send_json(502, {'error': 'تعذر بدء التحويل الفوري من المصدر'})
                txt = open(m3u8_file, encoding='utf-8', errors='replace').read()
                signed_lines = []
                for ln in txt.splitlines():
                    s_ln = ln.strip()
                    if s_ln and not s_ln.startswith('#'):
                        sep = '&' if '?' in s_ln else '?'
                        signed_lines.append(f'{s_ln}{sep}sig={media_sig}')
                    else:
                        signed_lines.append(ln)
                return self.send_bytes(
                    200,
                    ('\n'.join(signed_lines) + '\n').encode('utf-8'),
                    'application/vnd.apple.mpegurl',
                    cache='no-store',
                )
            else:
                sess = UHLS.get_session(sid)
                if not sess:
                    return self.send_json(404, {'error': 'الجلسة غير موجودة'})
                seg_file = os.path.join(sess['dir'], part_name)
                deadline = time.time() + 5.0
                while not os.path.isfile(seg_file) and time.time() < deadline:
                    time.sleep(0.12)
                if not os.path.isfile(seg_file):
                    return self.send_json(404, {'error': 'المقطع غير جاهز'})
                with open(seg_file, 'rb') as f:
                    seg_bytes = f.read()
                return self.send_media_range(seg_bytes, 'video/mp2t')

        # -------------------------------------------------------------------
        # 5a2. Universal Addon Adapters: Legacy Stremio v1, M3U IPTV, JSON/XML Feeds
        # -------------------------------------------------------------------
        if raw_path.startswith('/api/legacy-v1/'):
            rest = raw_path[len('/api/legacy-v1/') :]
            if '/' not in rest:
                return self.send_json(400, {'error': 'مسار v1 ناقص'})
            b64_ep, rel_sub = rest.split('/', 1)
            try:
                st_code, obj = handle_legacy_v1_request(b64_ep, rel_sub, origin, media_sig, validate_outbound_url, fetch_json)
                return self.send_json(st_code, obj)
            except Exception as exc:
                return self.send_json(502, {'error': f'خطأ في إضافة v1: {str(exc)[:100]}'})

        if raw_path.startswith('/api/m3u-addon/'):
            rest = raw_path[len('/api/m3u-addon/') :]
            if '/' not in rest:
                return self.send_json(400, {'error': 'مسار M3U ناقص'})
            b64_u, rel_sub = rest.split('/', 1)
            try:
                st_code, obj = handle_m3u_addon_request(b64_u, rel_sub, origin, media_sig, HAS_FFMPEG, validate_outbound_url)
                return self.send_json(st_code, obj)
            except Exception as exc:
                return self.send_json(502, {'error': f'خطأ في قائمة M3U: {str(exc)[:100]}'})

        if raw_path.startswith('/api/feed-addon/'):
            rest = raw_path[len('/api/feed-addon/') :]
            if '/' not in rest:
                return self.send_json(400, {'error': 'مسار الخلاصة ناقص'})
            b64_u, rel_sub = rest.split('/', 1)
            try:
                st_code, obj = handle_feed_addon_request(b64_u, rel_sub, origin, media_sig, HAS_FFMPEG, validate_outbound_url)
                return self.send_json(st_code, obj)
            except Exception as exc:
                return self.send_json(502, {'error': f'خطأ في الخلاصة: {str(exc)[:100]}'})

        # -------------------------------------------------------------------
        # 5b. Official Stremio Streaming Engine Bridge (/stremio-engine/... + root /hlsv2/...)
        #     Supports root-relative url.resolve paths from @stremio/stremio-video!
        # -------------------------------------------------------------------
        is_root_engine_get = bool(
            raw_path in ('/settings', '/device-info', '/network-info', '/playback-devices', '/stats.json', '/opensubHash', '/subtitleSignature')
            or raw_path.startswith(('/hlsv2/', '/proxy/', '/embedded-ass'))
            or re.match(r'^/[0-9a-fA-F]{40}/', raw_path)
        )
        if raw_path.startswith('/stremio-engine/') or is_root_engine_get:
            sub = (
                raw_path[len('/stremio-engine/') :].strip('/')
                if raw_path.startswith('/stremio-engine/')
                else raw_path.lstrip('/')
            )
            if '..' in sub:
                return self.send_json(400, {'error': 'مسار محرك البث غير صالح'})
            if getattr(self, 'command', 'GET') == 'HEAD' and sub.endswith('.m3u8'):
                return self.send_bytes(200, b'', 'application/vnd.apple.mpegurl', cache='no-store')

            # Fast-bypass hlsv2/probe for streams that are ALREADY HLS or Web-Ready MP4 so HTMLVideo/HLS.js plays them in 0ms!
            if sub == 'hlsv2/probe':
                media_u_probe = params.get('mediaURL', [''])[0].lower()
                if (
                    '.m3u8' in media_u_probe
                    or '/cartoony-hls/' in media_u_probe
                    or '/api/universal-hls/' in media_u_probe
                    or '/api/stream-proxy' in media_u_probe
                    or media_u_probe.split('?')[0].endswith(('.mp4', '.m4v', '.webm'))
                ):
                    return self.send_json(415, {'error': 'direct_web_ready_stream'})

            if not is_stremio_engine_alive() and not ensure_embedded_stremio_engine():
                # Return 503 when local 11470 is absent so Stremio Web cleanly uses direct HTMLVideo + HLS.js!
                return self.send_json(503, {'error': 'محرك البث المحلي غير متصل حاليًا'})

            fwd_pairs = []
            is_hlsv2_master = sub.startswith('hlsv2/') and sub.endswith('/master.m3u8')
            for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
                if k == 'sig':
                    continue
                if is_hlsv2_master and k == 'videoCodecs' and v.lower() in ('h265', 'hevc'):
                    continue
                if is_hlsv2_master and k == 'audioCodecs' and v.lower() in ('ac3', 'eac3', 'dts', 'truehd'):
                    continue
                if k in ('mediaURL', 'videoUrl') and v.startswith(origin + '/'):
                    rel_v = v[len(origin) :]
                    if re.match(r'^/(?:stremio-engine/)?[0-9a-fA-F]{40}/', rel_v):
                        clean_rel = re.sub(r'^/stremio-engine', '', rel_v)
                        clean_rel = re.sub(r'([?&])sig=[^&]*', '', clean_rel).rstrip('?&')
                        v = f'{STREMIO_ENGINE_URL}{clean_rel}'
                fwd_pairs.append((k, v))
            if is_hlsv2_master and not any(k == 'videoCodecs' for k, _ in fwd_pairs):
                fwd_pairs.append(('videoCodecs', 'h264'))
            if is_hlsv2_master and not any(k == 'audioCodecs' for k, _ in fwd_pairs):
                fwd_pairs.append(('audioCodecs', 'aac'))
            fwd_qs = urllib.parse.urlencode(fwd_pairs)
            upstream = f'{STREMIO_ENGINE_URL}/{sub}' + (f'?{fwd_qs}' if fwd_qs else '')
            req_headers = {'User-Agent': 'Mozilla/5.0'}
            if self.headers.get('Range') and not sub.endswith('.m3u8'):
                req_headers['Range'] = self.headers['Range']
            try:
                req = urllib.request.Request(upstream, headers=req_headers)
                with urllib.request.urlopen(req, timeout=45) as r:
                    ct = r.headers.get('Content-Type', 'application/octet-stream')
                    if sub in ('', 'settings'):
                        st_obj = json.loads(r.read(500_000).decode('utf-8', 'replace'))
                        if isinstance(st_obj, dict):
                            st_obj['baseUrl'] = origin
                        return self.send_json(200, st_obj)
                    if sub == 'hlsv2/probe':
                        pr_obj = json.loads(r.read(1_000_000).decode('utf-8', 'replace'))
                        if isinstance(pr_obj, dict):
                            fmt = pr_obj.get('format') or {}
                            fmt_name = str(fmt.get('name') or '').lower()
                            streams_l = pr_obj.get('streams') or []
                            # Chromium/Edge/Safari cannot demux HEVC/AC3/EAC3/DTS inside raw MKV; force seamless hlsv2 HLS!
                            needs_hls = 'matroska' in fmt_name and any(
                                str(st_i.get('codec') or '').lower() in ('hevc', 'h265', 'ac3', 'eac3', 'dts', 'truehd', 'mlp', 'pcm_s16le', 'pcm_s24le')
                                for st_i in streams_l
                                if isinstance(st_i, dict)
                            )
                            if needs_hls and isinstance(pr_obj.get('format'), dict):
                                pr_obj['format']['name'] = 'matroska_hls_required'
                        return self.send_json(200, pr_obj)
                    if sub.endswith('.m3u8') or 'mpegurl' in ct.lower():
                        m3u8_txt = r.read(2_000_000).decode('utf-8', 'replace')

                        def _sign_ref(ref_u):
                            if 'sig=' in ref_u:
                                return ref_u
                            sep = '&' if '?' in ref_u else '?'
                            return f'{ref_u}{sep}sig={media_sig}'

                        rewritten = []
                        for ln in m3u8_txt.splitlines():
                            s_ln = ln.strip()
                            if not s_ln:
                                rewritten.append(ln)
                            elif s_ln.startswith('#'):
                                rewritten.append(
                                    re.sub(r'URI="([^"]+)"', lambda m_u: f'URI="{_sign_ref(m_u.group(1))}"', ln)
                                )
                            else:
                                rewritten.append(_sign_ref(s_ln))
                        body = ('\n'.join(rewritten) + '\n').encode('utf-8')
                        return self.send_media_range(body, 'application/vnd.apple.mpegurl')
                    else:
                        if getattr(self, 'command', 'GET') == 'HEAD':
                            self.send_response(r.status)
                            self.send_header('Content-Type', ct)
                            if r.headers.get('Content-Length'):
                                self.send_header('Content-Length', r.headers['Content-Length'])
                            self.add_privacy_headers()
                            self.end_headers()
                            return
                        self.send_response(r.status)
                        self.send_header('Content-Type', ct)
                        if r.headers.get('Content-Length'):
                            self.send_header('Content-Length', r.headers['Content-Length'])
                        else:
                            self.close_connection = True
                        if r.headers.get('Content-Range'):
                            self.send_header('Content-Range', r.headers['Content-Range'])
                        self.send_header('Accept-Ranges', r.headers.get('Accept-Ranges', 'bytes'))
                        self.add_privacy_headers()
                        self.end_headers()
                        while True:
                            chunk = r.read(65536)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                        return
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception as exc:
                return self.send_json(502, {'error': f'تعذر الاتصال بمحرك البث المحلي (11470): {str(exc)[:100]}'})

        # -------------------------------------------------------------------
        # 5c. Fast Content-Type Probe Helper (/api/stream-head?url=...)
        # -------------------------------------------------------------------
        if raw_path == '/api/stream-head':
            probe_u = params.get('url', [''])[0].strip()
            ctype_guess = 'video/mp4'
            if '.m3u8' in probe_u.lower():
                ctype_guess = 'application/vnd.apple.mpegurl'
            else:
                try:
                    validate_outbound_url(probe_u, allow_local_addons=False)
                    req = urllib.request.Request(probe_u, headers={'User-Agent': 'Mozilla/5.0', 'Range': 'bytes=0-1'})
                    with urllib.request.urlopen(req, timeout=6) as r:
                        rc = (r.headers.get('Content-Type') or '').split(';')[0].strip().lower()
                        if rc and rc != 'application/octet-stream':
                            ctype_guess = rc
                except Exception:
                    pass
            return self.send_bytes(200, b'', ctype_guess, cache='no-store')

        # -------------------------------------------------------------------
        # 6. CORS-Free Universal Stremio Addon Proxy (/api/addon-proxy?url=...)
        #    Supports Stremio v3, Legacy /stremio/v1, M3U, and Stream Enrichment!
        # -------------------------------------------------------------------
        if raw_path == '/api/addon-proxy':
            target = params.get('url', [''])[0].strip()
            if target.startswith('stremio://'):
                target = 'https://' + target[len('stremio://') :]
            target = re.sub(r'^(https?):/{3,}', r'\1://', target, flags=re.I)

            # Native support if user pastes a Legacy Stremio v1 URL (e.g. https://opensubtitles.strem.io/stremio/v1) right into Stremio Addons!
            if '/stremio/v1' in target.lower():
                m_v1_res = re.match(r'^(https?://.*?/stremio/v1)(?:/(manifest\.json|(?:catalog|meta|stream|subtitles)/.+))?/?$', target, re.I)
                if m_v1_res:
                    v1_base = m_v1_res.group(1)
                    v1_rel = m_v1_res.group(2) or 'manifest.json'
                    st_c, v1_obj = handle_legacy_v1_request(b64u_encode(v1_base), v1_rel, origin, media_sig, validate_outbound_url, fetch_json)
                    return self.send_json(st_c, v1_obj)

            try:
                is_manifest = target.endswith('/manifest.json')
                is_catalog_or_meta = '/catalog/' in target or '/meta/' in target
                ttl = 600 if is_manifest else (180 if is_catalog_or_meta else 0)
                data = fetch_json(target, timeout=12, allow_local=True, cache_ttl=ttl)
                # Enrich external streams cleanly for Stremio Core (valid single StreamSource + instant content-type)
                if '/stream/' in target and isinstance(data, dict) and isinstance(data.get('streams'), list):
                    engine_up = is_stremio_engine_alive()
                    enriched = []
                    for s in data['streams'][:30]:
                        if not isinstance(s, dict):
                            continue
                        u = str(s.get('url', ''))
                        ih = str(s.get('infoHash', '')).strip().lower()
                        if u.startswith(('http://', 'https://')) and 'ozpwgjjkvrpxbeuhmmdr.supabase.co' in target:
                            enriched.extend(inspect_or_decorate_bidooo_stream(s, origin, media_sig))
                        elif re.fullmatch(r'[0-9a-f]{40}', ih):
                            # Pass infoHash + sources untouched so Official Stremio v5 withStreamingServer
                            # runs POST /<infoHash>/create (with trackers + guessFileIdx) & /hlsv2/ natively on Laptop, iPhone & Android!
                            enriched.append(s)
                        else:
                            s_copy = dict(s)
                            hints = dict(s_copy.get('behaviorHints') or {})
                            proxy_req_hdrs = (hints.get('proxyHeaders') or {}).get('request') if isinstance(hints.get('proxyHeaders'), dict) else None
                            hints.pop('proxyHeaders', None)
                            if u.startswith(('http://', 'https://')):
                                # Ensure strict single StreamSource for Rust stremio-core
                                s_copy.pop('externalUrl', None)
                                s_copy.pop('infoHash', None)
                                s_copy.pop('ytId', None)
                                hdr_dict = proxy_req_hdrs if isinstance(proxy_req_hdrs, dict) else {}
                                hdr_json = urllib.parse.quote(json.dumps(hdr_dict), safe='')
                                enc_u = urllib.parse.quote(u, safe='')
                                u_low = u.lower()
                                u_path = urllib.parse.urlparse(u_low).path
                                is_hls = u_path.endswith('.m3u8') or '.m3u8?' in u_low
                                is_non_native = u_path.endswith(('.mkv', '.avi', '.flv', '.wmv', '.ts', '.m2ts', '.mpg', '.mpeg'))
                                ext_hint = '.m3u8' if is_hls else ''
                                prox_u = f'{origin}/api/stream-proxy{ext_hint}?url={enc_u}&hdrs={hdr_json}&sig={media_sig}'
                                sid_u = hashlib.sha1((u + '|' + json.dumps(hdr_dict, sort_keys=True)).encode('utf-8')).hexdigest()[:16]
                                uhls_u = f'{origin}/api/universal-hls/{sid_u}/index.m3u8?url={enc_u}&hdrs={hdr_json}&sig={media_sig}' if HAS_FFMPEG else ''

                                if is_non_native and uhls_u:
                                    s_copy['url'] = uhls_u
                                    hints['notWebReady'] = False
                                elif hdr_dict or is_hls:
                                    s_copy['url'] = prox_u
                                    hints['notWebReady'] = False
                                else:
                                    hints['notWebReady'] = False
                            if isinstance(s_copy.get('subtitles'), list):
                                conv_subs = []
                                for idx_sb, sb in enumerate(s_copy['subtitles'][:12]):
                                    if isinstance(sb, dict) and str(sb.get('url', '')).startswith(('http://', 'https://')):
                                        q_sb = urllib.parse.quote(sb['url'], safe='')
                                        conv_subs.append({
                                            'id': sb.get('id') or f'st-sub-{idx_sb}',
                                            'lang': sb.get('lang', 'ara'),
                                            'url': f'{origin}/api/sub.vtt?url={q_sb}&sig={media_sig}',
                                        })
                                s_copy['subtitles'] = conv_subs
                            s_copy['behaviorHints'] = hints
                            enriched.append(s_copy)
                    data = dict(data, streams=enriched)
                # Rewrite external subtitles to UTF-8 WebVTT so iOS Safari plays them natively!
                if '/subtitles/' in target and isinstance(data, dict) and isinstance(data.get('subtitles'), list):
                    vtt_subs = []
                    for idx, s in enumerate(data['subtitles'][:20]):
                        u = str(s.get('url', ''))
                        if not u.startswith(('http://', 'https://')):
                            continue
                        q_u = urllib.parse.quote(u, safe='')
                        vtt_subs.append(
                            {
                                'id': s.get('id') or f'ext-sub-{idx}',
                                'lang': s.get('lang', 'ara'),
                                'label': s.get('label') or s.get('name') or s.get('lang', 'ترجمة'),
                                'url': f'{origin}/api/sub.vtt?url={q_u}&sig={media_sig}',
                            }
                        )
                    data = dict(data, subtitles=vtt_subs)
                return self.send_json(200, data)
            except Exception as exc:
                return self.send_json(502, {'error': f'تعذر الاتصال بالإضافة الخارجية: {str(exc)[:120]}'})

        # -------------------------------------------------------------------
        # 7. Stremio SDK proxyHeaders Stream Proxy (/api/stream-proxy[.m3u8])
        #    Allows community Live TV / HTTP addons with custom Referer/Origin
        #    headers to play natively inside iPhone Safari & Android!
        # -------------------------------------------------------------------
        if raw_path in ('/api/stream-proxy', '/api/stream-proxy.m3u8'):
            target_u = params.get('url', [''])[0].strip()
            raw_hdrs = params.get('hdrs', ['{}'])[0]
            try:
                validate_outbound_url(target_u, allow_local_addons=False)
                parsed_hdrs = json.loads(raw_hdrs) if raw_hdrs else {}
            except Exception:
                return self.send_json(400, {'error': 'رابط البث غير صالح'})
            safe_hdrs = {'User-Agent': 'Mozilla/5.0'}
            if isinstance(parsed_hdrs, dict):
                for hk, hv in parsed_hdrs.items():
                    if hk.lower() in ('referer', 'origin', 'user-agent', 'accept', 'accept-language') and isinstance(hv, str):
                        safe_hdrs[hk] = hv[:400]
            if self.headers.get('Range'):
                safe_hdrs['Range'] = self.headers['Range']
            try:
                req = urllib.request.Request(target_u, headers=safe_hdrs)
                with urllib.request.urlopen(req, timeout=20) as r:
                    ct = r.headers.get('Content-Type', 'application/octet-stream')
                    if raw_path.endswith('.m3u8') or 'mpegurl' in ct.lower() or target_u.split('?')[0].endswith('.m3u8'):
                        m3u8_txt = r.read(2_000_000).decode('utf-8', 'replace')
                        enc_hdrs = urllib.parse.quote(json.dumps(parsed_hdrs), safe='')

                        def _proxify(rel_or_abs):
                            abs_u = urllib.parse.urljoin(target_u, rel_or_abs.strip())
                            ext_h = '.m3u8' if '.m3u8' in abs_u.lower() else ''
                            return f'{origin}/api/stream-proxy{ext_h}?url={urllib.parse.quote(abs_u, safe="")}&hdrs={enc_hdrs}&sig={media_sig}'

                        out_lines = []
                        for ln in m3u8_txt.splitlines():
                            s_ln = ln.strip()
                            if not s_ln:
                                out_lines.append(ln)
                            elif s_ln.startswith('#'):
                                out_lines.append(re.sub(r'URI="([^"]+)"', lambda m_u: f'URI="{_proxify(m_u.group(1))}"', ln))
                            else:
                                out_lines.append(_proxify(s_ln))
                        return self.send_media_range(('\n'.join(out_lines) + '\n').encode('utf-8'), 'application/vnd.apple.mpegurl')
                    else:
                        if getattr(self, 'command', 'GET') == 'HEAD':
                            self.send_response(r.status)
                            self.send_header('Content-Type', ct)
                            if r.headers.get('Content-Length'):
                                self.send_header('Content-Length', r.headers['Content-Length'])
                            else:
                                self.send_header('Content-Length', '0')
                            self.add_privacy_headers()
                            self.end_headers()
                            return
                        self.send_response(r.status)
                        self.send_header('Content-Type', ct)
                        if r.headers.get('Content-Length'):
                            self.send_header('Content-Length', r.headers['Content-Length'])
                        else:
                            self.close_connection = True
                        if r.headers.get('Content-Range'):
                            self.send_header('Content-Range', r.headers['Content-Range'])
                        self.send_header('Accept-Ranges', r.headers.get('Accept-Ranges', 'bytes'))
                        self.add_privacy_headers()
                        self.end_headers()
                        while True:
                            chunk = r.read(65536)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                        return
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception as exc:
                return self.send_json(502, {'error': f'تعذر جلب البث عبر الوكيل: {str(exc)[:100]}'})

        return self.send_json(404, {'error': 'المسار غير موجود'})

    def handle_cartoon_addon(self, rel, origin, media_sig):
        if rel == 'manifest.json':
            return self.send_json(200, CARTOON_MANIFEST)
        p = [x for x in rel.strip('/').split('/') if x]

        # Catalog: Movies
        if len(p) >= 3 and p[0] == 'catalog' and p[1] == 'movie':
            cat_id = urllib.parse.unquote(p[2]).removesuffix('.json')
            if cat_id in ('cartoony_films', 'disney_films'):
                extra = urllib.parse.unquote(p[3]).removesuffix('.json') if len(p) > 3 else ''
                q_params = urllib.parse.parse_qs(extra)
                q = q_params.get('search', [''])[0].casefold().strip()
                try:
                    skip = max(0, int(q_params.get('skip', ['0'])[0]))
                except ValueError:
                    skip = 0
                movie_ids = AUTO.disney_ids() if cat_id == 'disney_films' else list(MOVIES)
                movie_ids.sort(key=lambda k: -AUTO.views.get(k, 0))
                metas = [
                    {
                        'id': 'cartoonymovie:' + k,
                        'type': 'movie',
                        'name': MOVIES[k]['title'],
                        'poster': MOVIES[k]['poster'],
                    }
                    for k in movie_ids
                    if k in MOVIES and q in MOVIES[k]['title'].casefold()
                ]
                if cat_id == 'disney_films':
                    metas += [m for m in DISNEY_EXTERNAL.values() if q in m['name'].casefold()]
                return self.send_json(200, {'metas': metas[skip : skip + 100]})

        # Meta: Movies
        if len(p) == 3 and p[0] == 'meta' and p[1] == 'movie':
            item = urllib.parse.unquote(p[2]).removesuffix('.json')
            if item in DISNEY_EXTERNAL:
                return self.send_json(200, {'meta': DISNEY_EXTERNAL[item]})
            m = re.fullmatch(r'cartoonymovie:([0-9]+)', item)
            if not m or m.group(1) not in MOVIES:
                return self.send_json(200, {'meta': None})
            movie = MOVIES[m.group(1)]
            return self.send_json(
                200,
                {
                    'meta': {
                        'id': item,
                        'type': 'movie',
                        'name': movie['title'],
                        'poster': movie['poster'],
                        'description': movie.get('audio_status', 'نسخة مدبلجة بحسب المصدر') + '\n' + movie.get('description', ''),
                    }
                },
            )

        # Stream: Movies
        if len(p) == 3 and p[0] == 'stream' and p[1] == 'movie':
            item = urllib.parse.unquote(p[2]).removesuffix('.json')
            if item in DISNEY_EXTERNAL or item.startswith('tt'):
                return self.send_json(200, {'streams': find_external_dubbed_streams(item, 'movie', origin, media_sig)})
            m = re.fullmatch(r'cartoonymovie:([0-9]+)', item)
            if not m or m.group(1) not in MOVIES:
                return self.send_json(200, {'streams': []})
            movie = MOVIES[m.group(1)]
            video = movie['episodes'][0]
            return self.send_json(
                200,
                {
                    'streams': build_cartoony_streams(
                        origin, media_sig, video['video_id'], movie['title'], movie.get('audio_status', 'مدبلج بحسب المصدر')
                    )
                },
            )

        # Meta: Series
        if len(p) == 3 and p[0] == 'meta' and p[1] == 'series':
            item = urllib.parse.unquote(p[2]).removesuffix('.json')
            m = re.fullmatch(r'cartoonymem:([0-9]+)', item)
            if not m or m.group(1) not in CARTOONY:
                return self.send_json(200, {'meta': None})
            show = CARTOONY[m.group(1)]
            videos = []
            for n, ep in enumerate(show.get('episodes', []), 1):
                if any(x in ep['title'] for x in ('مترجم', 'مترجمة')):
                    continue
                videos.append({'id': f"{item}:{ep['id']}", 'title': ep['title'], 'season': 1, 'episode': n})
            return self.send_json(
                200,
                {
                    'meta': {
                        'id': item,
                        'type': 'series',
                        'name': show['title'],
                        'poster': show['poster'],
                        'description': show.get('description', 'مسلسل كرتون مدبلج.'),
                        'videos': videos,
                    }
                },
            )

        # Stream: Series
        if len(p) == 3 and p[0] == 'stream' and p[1] == 'series':
            item = urllib.parse.unquote(p[2]).removesuffix('.json')
            if item.startswith('cartoonymem:'):
                m = re.fullmatch(r'cartoonymem:([0-9]+):([0-9]+)', item)
                if not m or m.group(1) not in CARTOONY:
                    return self.send_json(200, {'streams': []})
                ep = next((x for x in CARTOONY[m.group(1)]['episodes'] if str(x['id']) == m.group(2)), None)
                if not ep or any(x in ep['title'] for x in ('مترجم', 'مترجمة')):
                    return self.send_json(200, {'streams': []})
                return self.send_json(
                    200,
                    {'streams': build_cartoony_streams(origin, media_sig, ep['video_id'], ep['title'], 'مدبلج بحسب المصدر')},
                )
            if re.fullmatch(r'tt[0-9]+:[0-9]+:[0-9]+', item):
                return self.send_json(200, {'streams': find_external_dubbed_streams(item, 'series', origin, media_sig)})
            return self.send_json(200, {'streams': []})

        # Catalog: Series
        if len(p) >= 3 and p[0] == 'catalog' and p[1] == 'series':
            cat = urllib.parse.unquote(p[2]).removesuffix('.json')
            extra = urllib.parse.unquote(p[3]).removesuffix('.json') if len(p) > 3 else ''
            q_params = urllib.parse.parse_qs(extra)
            q = q_params.get('search', [''])[0].casefold().strip()
            try:
                skip = max(0, int(q_params.get('skip', ['0'])[0]))
            except ValueError:
                skip = 0

            if cat in ('auto_dubbed', 'auto_popular'):
                excluded = (
                    {i for v in CURATED.values() for i in v if not i.startswith('tt')}
                    | {i for v in CHANNEL_EXTRA.values() for i in v}
                    | {i for c in CHANNEL_NAMES for i in AUTO.channel_ids(c)}
                )
                ids = AUTO.snapshot()
                if cat == 'auto_dubbed':
                    ids = [i for i in ids if i not in excluded]
                else:
                    ids = [i for i in ids if i in excluded]
                    unique, groups = [], set()
                    for i in ids:
                        if i not in CARTOONY:
                            continue
                        grp = re.sub(r'\s+الجزء\s+.*$', '', name_key(CARTOONY[i]['title']))
                        if grp in groups:
                            continue
                        groups.add(grp)
                        unique.append(i)
                        if len(unique) >= 60:
                            break
                    ids = unique
                metas = [
                    {'id': 'cartoonymem:' + k, 'type': 'series', 'name': CARTOONY[k]['title'], 'poster': CARTOONY[k]['poster']}
                    for k in ids
                    if k in CARTOONY and q in CARTOONY[k]['title'].casefold()
                ]
                return self.send_json(200, {'metas': metas[skip : skip + 100]})

            if cat in CURATED:
                metas, seen = [], set()
                ids = CURATED[cat] + [i for i in CHANNEL_EXTRA.get(cat, []) if i in AUTO.snapshot()] + AUTO.channel_ids(cat)
                rank_by_name = {
                    name_key(CARTOONY[i]['title']): AUTO.views.get(i, 0)
                    for i in CHANNEL_EXTRA.get(cat, [])
                    if i in CARTOONY
                }
                for ident in ids:
                    if ident.startswith('tt'):
                        m = next((x for x in CATALOG_DATA.get(cat, []) if x['id'] == ident), None)
                    else:
                        v = CARTOONY.get(ident)
                        m = {'id': 'cartoonymem:' + ident, 'type': 'series', 'name': v['title'], 'poster': v['poster']} if v else None
                    if m and q in m['name'].casefold():
                        k = name_key(m['name'])
                        if k in seen:
                            continue
                        seen.add(k)
                        metas.append(m)
                metas.sort(
                    key=lambda m: -max(
                        AUTO.views.get(m['id'].removeprefix('cartoonymem:'), 0),
                        rank_by_name.get(name_key(m['name']), 0),
                    )
                )
                return self.send_json(200, {'metas': metas[skip : skip + 100]})

        return self.send_json(404, {'error': 'فهرس غير معروف'})

    def _guess_mime(self, norm):
        if norm.endswith('.wasm'):
            return 'application/wasm'
        if norm.endswith('.js'):
            return 'application/javascript; charset=utf-8'
        if norm.endswith('.css'):
            return 'text/css; charset=utf-8'
        if norm.endswith('.html'):
            return 'text/html; charset=utf-8'
        if norm.endswith('.svg'):
            return 'image/svg+xml'
        if norm.endswith('.woff2'):
            return 'font/woff2'
        if norm.endswith('.ttf'):
            return 'font/ttf'
        if norm.endswith('.webp'):
            return 'image/webp'
        ctype, _ = mimetypes.guess_type(norm)
        return ctype or 'application/octet-stream'

    def _serve_cached_asset(self, norm, cache, extra_headers=None):
        st = os.stat(norm)
        etag = f'"{int(st.st_mtime):x}-{st.st_size:x}"'
        inm = (self.headers.get('If-None-Match', '') or '').strip()
        if inm and etag in inm:
            self.send_response(304)
            self.send_header('ETag', etag)
            self.send_header('Cache-Control', cache)
            self.add_privacy_headers()
            self.end_headers()
            return
        mime = self._guess_mime(norm)
        can_gz = st.st_size >= 1024 and self._is_compressible_type(mime)
        entry = _STATIC_ASSET_CACHE.get(norm)
        if not entry or entry[0] != etag:
            with open(norm, 'rb') as f:
                raw_b = f.read()
            gz_b = None
            if can_gz:
                # 1) Fast path: serve pre-gzipped file from disk (instant, no CPU burn)
                gz_path = norm + '.gz'
                if os.path.isfile(gz_path):
                    try:
                        with open(gz_path, 'rb') as f:
                            gz_b = f.read() or None
                    except Exception:
                        gz_b = None
                # 2) Fallback: compress once in memory (only if no disk .gz exists)
                if gz_b is None:
                    gz_b = gzip.compress(raw_b, compresslevel=6)
            # Keep memory cache bounded; do not cache raw bytes of huge files
            if len(_STATIC_ASSET_CACHE) < 200:
                _STATIC_ASSET_CACHE[norm] = (etag, raw_b if len(raw_b) <= 3_000_000 else None, gz_b)
        else:
            _, raw_b, gz_b = entry
            if raw_b is None and not (gz_b is not None and self._accepts_gzip()):
                with open(norm, 'rb') as f:
                    raw_b = f.read()
        hdrs = {'ETag': etag}
        if extra_headers:
            hdrs.update(extra_headers)
        if gz_b is not None and self._accepts_gzip():
            return self.send_bytes(200, gz_b, mime, cache=cache, extra_headers=hdrs, pre_gzipped=True)
        return self.send_bytes(200, raw_b, mime, cache=cache, extra_headers=hdrs)

    def serve_stremio_web_file(self, rel_path, cache='public, max-age=86400', extra_headers=None):
        norm = os.path.normpath(os.path.join(STREMIO_WEB_DIR, rel_path))
        if not norm.startswith(os.path.normpath(STREMIO_WEB_DIR) + os.sep) or not os.path.isfile(norm):
            return self.send_json(404, {'error': 'الملف غير موجود'})
        return self._serve_cached_asset(norm, cache, extra_headers=extra_headers)

    def serve_static_file(self, rel_path, cache='public, max-age=3600', extra_headers=None):
        norm = os.path.normpath(os.path.join(STATIC_DIR, rel_path))
        if not norm.startswith(STATIC_DIR + os.sep) or not os.path.isfile(norm):
            return self.send_json(404, {'error': 'الملف غير موجود'})
        return self._serve_cached_asset(norm, cache, extra_headers=extra_headers)


_STATIC_ASSET_CACHE = {}


def _prewarm_heavy_bundles():
    """Pre-compress heavy Stremio bundles to .gz on disk so RAM stays minimal (<160 MB total) even on 256MB Free Cloud!"""
    for rel in (
        '296e65dd5a00319c808b6e270fcccab44b8e7a79/scripts/main.js',
        '296e65dd5a00319c808b6e270fcccab44b8e7a79/scripts/790.js',
        '296e65dd5a00319c808b6e270fcccab44b8e7a79/styles/main.css',
    ):
        norm = os.path.normpath(os.path.join(STREMIO_WEB_DIR, rel))
        gz_disk = norm + '.gz'
        if os.path.isfile(norm) and not os.path.isfile(gz_disk):
            try:
                with open(norm, 'rb') as f, open(gz_disk, 'wb') as gf:
                    gf.write(gzip.compress(f.read(), compresslevel=6))
            except Exception:
                pass


def main():
    ensure_stremio_web_ui()
    threading.Thread(target=ensure_embedded_stremio_engine, daemon=True).start()
    threading.Thread(target=_prewarm_heavy_bundles, daemon=True).start()
    host = os.environ.get('DODO_HOST', '0.0.0.0')
    port = int(os.environ.get('DODO_PORT', os.environ.get('PORT', '18880')))
    if os.environ.get('DODO_PRIVACY_MODE') in ('always', 'internet_only', 'off'):
        with CONFIG_LOCK:
            CFG['privacy_mode'] = os.environ['DODO_PRIVACY_MODE']
    if os.environ.get('DODO_INVITE_CODE'):
        with CONFIG_LOCK:
            CFG['invite_code'] = os.environ['DODO_INVITE_CODE']
    if os.environ.get('DODO_ADMIN_PIN'):
        with CONFIG_LOCK:
            CFG['admin_pin'] = os.environ['DODO_ADMIN_PIN']
    AUTO.start()
    srv = ThreadingHTTPServer((host, port), DodoHandler)
    print(f'[Dodo Stream] listening on http://{host}:{port} (FFmpeg={HAS_FFMPEG})', flush=True)
    srv.serve_forever()


if __name__ == '__main__':
    main()
