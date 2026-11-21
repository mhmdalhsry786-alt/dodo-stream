#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Universal Link, Legacy Addon (Stremio v1/v2 JSON-RPC), M3U/IPTV, JSON/RSS Feed,
and Cross-Device Live FFmpeg HLS Transcoder for Dodo Stream (دودو ستريم).
"""
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


MEDIA_EXTENSIONS = (
    '.mp4', '.mkv', '.webm', '.avi', '.mov', '.flv', '.wmv', '.m4v',
    '.ts', '.m2ts', '.mpg', '.mpeg', '.3gp', '.ogv',
    '.mp3', '.m4a', '.aac', '.ogg', '.opus', '.flac', '.wav',
)


def b64u_encode(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode('utf-8')).decode('ascii').rstrip('=')


def b64u_decode(token: str) -> str:
    pad = '=' * ((4 - len(token) % 4) % 4)
    return base64.urlsafe_b64decode((token + pad).encode('ascii')).decode('utf-8')


def clean_any_input_url(raw: str) -> str:
    """Sanitize any user-pasted link: Markdown, stremio://, https:/// typos, bare domains, magnets."""
    s = (raw or '').strip()
    if not s:
        return ''
    # Extract URL inside Markdown [label](url) if present
    md_match = re.search(r'\]\(\s*([a-zA-Z][a-zA-Z0-9+.-]*://[^\s)]+)\s*\)', s)
    if md_match:
        s = md_match.group(1).strip()
    else:
        # Or extract first URL-like token if surrounded by text
        tok_match = re.search(r'((?:https?|stremios?|magnet|rtmps?|rtsp|mms):[^\s"\'<>]+)', s, re.I)
        if tok_match:
            s = tok_match.group(1).strip()

    s = s.strip('"\'<> ')
    low = s.lower()
    if low.startswith('magnet:'):
        return s
    if re.fullmatch(r'[0-9a-fA-F]{40}', s):
        return f'magnet:?xt=urn:btih:{s.lower()}'
    if low.startswith('stremios://'):
        s = 'https://' + s[len('stremios://'):]
    elif low.startswith('stremio://'):
        s = 'https://' + s[len('stremio://'):]

    # Fix triple/multiple slashes after http(s): e.g. https:///example.com/v1 -> https://example.com/v1
    s = re.sub(r'^(https?):/{3,}', r'\1://', s, flags=re.I)

    # Bare domain without scheme: e.g. opensubtitles.strem.io/stremio/v1
    if not re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*://', s):
        if re.match(r'^[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(/.*)?$', s):
            s = 'https://' + s

    return s


def extract_youtube_id(url: str) -> str:
    try:
        p = urllib.parse.urlparse(url)
        host = (p.hostname or '').lower()
        if host in ('youtu.be', 'www.youtu.be'):
            vid = p.path.strip('/').split('/')[0]
            return vid if re.fullmatch(r'[A-Za-z0-9_-]{11}', vid) else ''
        if 'youtube.com' in host or 'youtube-nocookie.com' in host:
            qs = urllib.parse.parse_qs(p.query)
            if 'v' in qs and re.fullmatch(r'[A-Za-z0-9_-]{11}', qs['v'][0]):
                return qs['v'][0]
            parts = [x for x in p.path.strip('/').split('/') if x]
            if len(parts) >= 2 and parts[0] in ('embed', 'shorts', 'live', 'v'):
                if re.fullmatch(r'[A-Za-z0-9_-]{11}', parts[1]):
                    return parts[1]
    except Exception:
        pass
    return ''


def extract_infohash_from_magnet(url: str):
    m = re.search(r'urn:btih:([0-9a-fA-F]{40}|[A-Za-z2-7]{32})', url)
    if not m:
        return '', ''
    raw_h = m.group(1)
    if len(raw_h) == 32 and not re.fullmatch(r'[0-9a-fA-F]{32}', raw_h):
        try:
            ih = base64.b32decode(raw_h.upper()).hex().lower()
        except Exception:
            return '', ''
    else:
        ih = raw_h.lower()
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    dn = (qs.get('dn') or [''])[0]
    return ih, dn


def extract_infohash_from_torrent_bytes(data: bytes) -> str:
    """Minimal Bencode parser to slice exact raw bytes of top-level 'info' dictionary and SHA1 it."""
    def parse_any(idx: int):
        ch = data[idx:idx + 1]
        if ch == b'i':
            end = data.index(b'e', idx + 1)
            return end + 1
        if ch == b'l':
            cur = idx + 1
            while data[cur:cur + 1] != b'e':
                cur = parse_any(cur)
            return cur + 1
        if ch == b'd':
            cur = idx + 1
            while data[cur:cur + 1] != b'e':
                k_start = cur
                cur = parse_any(cur)
                k_raw = data[k_start:cur]
                v_start = cur
                cur = parse_any(cur)
                if k_raw == b'4:info' and idx == 0:
                    raise StopIteration((v_start, cur))
            return cur + 1
        if b'0' <= ch <= b'9':
            colon = data.index(b':', idx)
            length = int(data[idx:colon])
            return colon + 1 + length
        raise ValueError('invalid bencode')

    try:
        parse_any(0)
    except StopIteration as found:
        start, end = found.value
        return hashlib.sha1(data[start:end]).hexdigest().lower()
    except Exception:
        pass
    return ''


# ---------------------------------------------------------------------------
# Stremio Legacy v1 / v2 JSON-RPC Protocol Bridge (/stremio/v1 & /q.json)
# ---------------------------------------------------------------------------
def _legacy_rpc_call(endpoint: str, method: str, params_list: list, validate_url_fn, timeout=10):
    base = endpoint.rstrip('/')
    if base.endswith('/q.json'):
        base = base[:-len('/q.json')]
    validate_url_fn(base, allow_local_addons=True)
    rpc_body = json.dumps({'id': 1, 'jsonrpc': '2.0', 'method': method, 'params': params_list})
    b_param = urllib.parse.quote(base64.b64encode(rpc_body.encode('utf-8')).decode('ascii'), safe='')
    q_url = f'{base}/q.json?b={b_param}'
    req = urllib.request.Request(
        q_url,
        headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) DodoStream/3.0',
            'Accept': 'application/json',
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.loads(r.read(5_000_000).decode('utf-8', 'replace'))
    if isinstance(resp, dict) and 'result' in resp:
        return resp['result']
    return None


def probe_legacy_v1_endpoint(candidate_url: str, validate_url_fn):
    """Test if candidate_url (or its /stremio/v1 path) is a Stremio Legacy JSON-RPC addon."""
    bases = []
    u = candidate_url.split('?')[0].rstrip('/')
    if u.endswith('/q.json'):
        u = u[:-len('/q.json')]
    if u.endswith('/manifest.json'):
        u = u[:-len('/manifest.json')]
    bases.append(u)
    if not u.endswith('/stremio/v1'):
        bases.append(u + '/stremio/v1')

    for b in bases:
        try:
            res = _legacy_rpc_call(b, 'meta', [], validate_url_fn, timeout=8)
            if isinstance(res, dict) and isinstance(res.get('manifest'), dict):
                mf_raw = res['manifest']
                methods = res.get('methods') or mf_raw.get('methods') or []
                real_ep = str(mf_raw.get('endpoint') or b).rstrip('/')
                # Keep https if original host used https
                if b.startswith('https://') and real_ep.startswith('http://'):
                    b_host = urllib.parse.urlparse(b).hostname
                    r_host = urllib.parse.urlparse(real_ep).hostname
                    if b_host and b_host == r_host:
                        real_ep = 'https://' + real_ep[len('http://'):]
                return real_ep, mf_raw, methods
        except Exception:
            continue
    return None, None, None


def build_v3_manifest_from_legacy(endpoint: str, mf_raw: dict, methods: list) -> dict:
    types = mf_raw.get('types') or ['movie', 'series']
    resources = []
    catalogs = []
    if 'meta.find' in methods:
        resources.append('catalog')
        for t in types:
            catalogs.append({
                'type': t,
                'id': f'legacy_{t}',
                'name': mf_raw.get('name') or 'كتالوج',
                'extra': [{'name': 'search', 'isRequired': False}, {'name': 'skip', 'isRequired': False}],
            })
    if 'meta.get' in methods:
        resources.append('meta')
    if 'stream.find' in methods:
        resources.append('stream')
    if 'subtitles.find' in methods or 'subtitles.get' in methods:
        resources.append('subtitles')
    if not resources:
        resources = ['subtitles']
    return {
        'id': mf_raw.get('id') or ('legacy.' + hashlib.sha1(endpoint.encode('utf-8')).hexdigest()[:10]),
        'version': str(mf_raw.get('version') or '1.0.0'),
        'name': (mf_raw.get('name') or 'Stremio v1 Addon') + ' (v1)',
        'description': mf_raw.get('description') or f'إضافة Stremio v1 محولة تلقائيًا ({endpoint})',
        'logo': str(mf_raw.get('logo') or '').replace('http://www.strem.io/', 'https://www.strem.io/'),
        'types': types,
        'resources': resources,
        'catalogs': catalogs,
        'idPrefixes': mf_raw.get('idProperty') if isinstance(mf_raw.get('idProperty'), list) else ['tt'],
    }


def handle_legacy_v1_request(b64_ep: str, rel_path: str, origin: str, media_sig: str, validate_url_fn, fetch_json_fn):
    endpoint = b64u_decode(b64_ep)
    if rel_path == 'manifest.json':
        ep, mf_raw, methods = probe_legacy_v1_endpoint(endpoint, validate_url_fn)
        if not mf_raw:
            return 502, {'error': 'تعذر الاتصال بإضافة Stremio v1'}
        return 200, build_v3_manifest_from_legacy(ep, mf_raw, methods)

    parts = [x for x in rel_path.strip('/').split('/') if x]
    if len(parts) < 3:
        return 404, {'error': 'مسار غير مدعوم'}

    res_type = parts[0]
    media_type = parts[1]
    item_id = urllib.parse.unquote(parts[2]).removesuffix('.json')
    extra_str = urllib.parse.unquote(parts[3]).removesuffix('.json') if len(parts) > 3 else ''
    extra_qs = urllib.parse.parse_qs(extra_str)

    if res_type == 'catalog':
        q_obj = {'type': media_type}
        if extra_qs.get('search'):
            q_obj['search'] = extra_qs['search'][0]
        skip = int((extra_qs.get('skip') or ['0'])[0] or 0)
        rpc_res = _legacy_rpc_call(endpoint, 'meta.find', [None, {'query': q_obj, 'limit': 60, 'skip': skip}], validate_url_fn)
        metas = rpc_res if isinstance(rpc_res, list) else []
        for m in metas:
            if isinstance(m, dict) and 'id' not in m and m.get('imdb_id'):
                m['id'] = m['imdb_id']
        return 200, {'metas': metas}

    if res_type == 'meta':
        imdb_id = item_id.split(':')[0]
        rpc_res = _legacy_rpc_call(endpoint, 'meta.get', [None, {'query': {'imdb_id': imdb_id, 'type': media_type}}], validate_url_fn)
        if isinstance(rpc_res, dict):
            rpc_res.setdefault('id', imdb_id)
        return 200, {'meta': rpc_res}

    if res_type == 'stream':
        id_parts = item_id.split(':')
        q_obj = {'imdb_id': id_parts[0], 'type': media_type}
        if len(id_parts) >= 3 and id_parts[1].isdigit() and id_parts[2].isdigit():
            q_obj['season'] = int(id_parts[1])
            q_obj['episode'] = int(id_parts[2])
        rpc_res = _legacy_rpc_call(endpoint, 'stream.find', [None, {'query': q_obj}], validate_url_fn)
        streams = rpc_res if isinstance(rpc_res, list) else []
        return 200, {'streams': streams}

    if res_type == 'subtitles':
        id_parts = item_id.split(':')
        item_hash = ' '.join(id_parts)
        subs_out = []
        try:
            rpc_res = _legacy_rpc_call(endpoint, 'subtitles.find', [None, {'query': {'itemHash': item_hash}}], validate_url_fn)
            all_list = (rpc_res or {}).get('all') if isinstance(rpc_res, dict) else []
            for idx, sb in enumerate(all_list or []):
                if isinstance(sb, dict) and str(sb.get('url', '')).startswith(('http://', 'https://')):
                    q_u = urllib.parse.quote(sb['url'], safe='')
                    lang = sb.get('lang') or sb.get('langName') or 'sub'
                    subs_out.append({
                        'id': sb.get('id') or f'v1-sub-{idx}',
                        'lang': lang,
                        'label': f"{lang} (OpenSubtitles v1 #{idx + 1})",
                        'url': f'{origin}/api/sub.vtt?url={q_u}&sig={media_sig}',
                    })
        except Exception:
            pass

        # Official Stremio migrated opensubtitles.strem.io/stremio/v1 data to opensubtitles-v3.strem.io
        if not subs_out and 'opensubtitles.strem.io' in endpoint.lower():
            try:
                v3_data = fetch_json_fn(
                    f'https://opensubtitles-v3.strem.io/subtitles/{media_type}/{urllib.parse.quote(item_id)}.json',
                    timeout=9,
                    cache_ttl=300,
                )
                raw_subs = (v3_data or {}).get('subtitles') or []
                # Put Arabic subtitles first, then English & others
                raw_subs.sort(key=lambda x: 0 if str(x.get('lang', '')).lower().startswith('ara') else (1 if str(x.get('lang', '')).lower().startswith('eng') else 2))
                for idx, sb in enumerate(raw_subs[:25]):
                    u = str(sb.get('url', ''))
                    if u.startswith(('http://', 'https://')):
                        q_u = urllib.parse.quote(u, safe='')
                        lang = str(sb.get('lang', 'sub'))
                        lbl = f'العربية #{idx + 1}' if lang.lower().startswith('ara') else f'{lang.upper()} #{idx + 1}'
                        subs_out.append({
                            'id': sb.get('id') or f'osv3-{idx}',
                            'lang': lang,
                            'label': lbl,
                            'url': f'{origin}/api/sub.vtt?url={q_u}&sig={media_sig}',
                        })
            except Exception:
                pass
        return 200, {'subtitles': subs_out}

    return 404, {'error': 'مورد غير معروف'}


# ---------------------------------------------------------------------------
# Universal M3U / M3U8 IPTV & VOD Playlist Adapter (/api/m3u-addon/<b64>/...)
# ---------------------------------------------------------------------------
_M3U_CACHE = {}
_M3U_LOCK = threading.Lock()


def parse_m3u_playlist(text: str, base_url: str = '') -> list:
    items = []
    cur_meta = {}
    cur_headers = {}
    for raw_ln in text.splitlines():
        ln = raw_ln.strip()
        if not ln:
            continue
        if ln.startswith('#EXTINF:'):
            name = ln.split(',', 1)[1].strip() if ',' in ln else 'قناة / فيديو'
            logo_m = re.search(r'tvg-logo="([^"]*)"', ln, re.I)
            grp_m = re.search(r'group-title="([^"]*)"', ln, re.I)
            cur_meta = {
                'name': name or 'بث مباشر',
                'poster': logo_m.group(1).strip() if logo_m else '',
                'group': grp_m.group(1).strip() if grp_m else 'عام',
            }
        elif ln.startswith('#EXTVLCOPT:'):
            opt = ln[len('#EXTVLCOPT:'):].strip()
            if '=' in opt:
                k, v = opt.split('=', 1)
                kl = k.strip().lower()
                if kl == 'http-referrer':
                    cur_headers['Referer'] = v.strip()
                elif kl == 'http-user-agent':
                    cur_headers['User-Agent'] = v.strip()
                elif kl == 'http-origin':
                    cur_headers['Origin'] = v.strip()
        elif not ln.startswith('#'):
            full_u = urllib.parse.urljoin(base_url, ln) if base_url else ln
            vid_id = 'dodom3u:' + hashlib.sha1(full_u.encode('utf-8')).hexdigest()[:16]
            items.append({
                'id': vid_id,
                'name': cur_meta.get('name') or os.path.basename(urllib.parse.urlparse(full_u).path) or 'بث مباشر',
                'poster': cur_meta.get('poster') or '',
                'group': cur_meta.get('group') or 'قنوات وبثوث',
                'url': full_u,
                'headers': dict(cur_headers),
            })
            cur_meta = {}
            cur_headers = {}
            if len(items) >= 3000:
                break
    return items


def _load_m3u_items(playlist_url: str, validate_url_fn) -> list:
    with _M3U_LOCK:
        hit = _M3U_CACHE.get(playlist_url)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
    validate_url_fn(playlist_url, allow_local_addons=True)
    req = urllib.request.Request(playlist_url, headers={'User-Agent': 'Mozilla/5.0 DodoStream/3.0'})
    with urllib.request.urlopen(req, timeout=15) as r:
        txt = r.read(8_000_000).decode('utf-8', 'replace')
    items = parse_m3u_playlist(txt, playlist_url)
    with _M3U_LOCK:
        _M3U_CACHE[playlist_url] = (time.time(), items)
    return items


def handle_m3u_addon_request(b64_url: str, rel_path: str, origin: str, media_sig: str, has_ffmpeg: bool, validate_url_fn):
    playlist_url = b64u_decode(b64_url)
    items = _load_m3u_items(playlist_url, validate_url_fn)
    host_label = urllib.parse.urlparse(playlist_url).hostname or 'M3U'
    groups = sorted({x['group'] for x in items if x.get('group')})[:40]

    if rel_path == 'manifest.json':
        extra = [{'name': 'search', 'isRequired': False}, {'name': 'skip', 'isRequired': False}]
        if groups:
            extra.append({'name': 'genre', 'options': groups, 'isRequired': False})
        return 200, {
            'id': 'dodo.m3u.' + hashlib.sha1(playlist_url.encode('utf-8')).hexdigest()[:10],
            'version': '1.0.0',
            'name': f'قائمة قنوات M3U ({host_label})',
            'description': f'قائمة تشغيل مفتوحة تضم {len(items)} قناة/فيديو تم تحويلها تلقائيًا لكل الأجهزة.',
            'types': ['tv'],
            'resources': ['catalog', 'meta', 'stream'],
            'idPrefixes': ['dodom3u:'],
            'catalogs': [{'type': 'tv', 'id': 'm3u_all', 'name': f'قنوات {host_label}', 'extra': extra}],
        }

    parts = [x for x in rel_path.strip('/').split('/') if x]
    if len(parts) >= 3 and parts[0] == 'catalog':
        extra_str = urllib.parse.unquote(parts[3]).removesuffix('.json') if len(parts) > 3 else ''
        qs = urllib.parse.parse_qs(extra_str)
        q = (qs.get('search') or [''])[0].strip().casefold()
        genre = (qs.get('genre') or [''])[0].strip()
        skip = int((qs.get('skip') or ['0'])[0] or 0)
        filtered = [
            {
                'id': x['id'],
                'type': 'tv',
                'name': x['name'],
                'poster': x['poster'] or f'{origin}/static/icons/icon-192.png',
                'posterShape': 'square',
                'description': f"التصنيف: {x['group']}",
            }
            for x in items
            if (not q or q in x['name'].casefold() or q in x['group'].casefold())
            and (not genre or x['group'] == genre)
        ]
        return 200, {'metas': filtered[skip:skip + 100]}

    if len(parts) == 3 and parts[0] == 'meta':
        item_id = urllib.parse.unquote(parts[2]).removesuffix('.json')
        found = next((x for x in items if x['id'] == item_id), None)
        if not found:
            return 200, {'meta': None}
        return 200, {
            'meta': {
                'id': found['id'],
                'type': 'tv',
                'name': found['name'],
                'poster': found['poster'] or f'{origin}/static/icons/icon-192.png',
                'description': f"بث مباشر — {found['group']}",
            }
        }

    if len(parts) == 3 and parts[0] == 'stream':
        item_id = urllib.parse.unquote(parts[2]).removesuffix('.json')
        found = next((x for x in items if x['id'] == item_id), None)
        if not found:
            return 200, {'streams': []}
        return 200, {
            'streams': build_universal_media_streams(
                found['url'], found['name'], origin, media_sig, has_ffmpeg, headers=found.get('headers')
            )
        }

    return 404, {'error': 'مسار قائمة M3U غير معروف'}


def parse_json_or_xml_feed(text: str, base_url: str = '') -> list:
    items = []
    t = text.strip()
    if t.startswith(('{', '[')):
        try:
            data = json.loads(t)
            arr = data if isinstance(data, list) else (
                data.get('items') or data.get('videos') or data.get('channels') or data.get('list') or []
                if isinstance(data, dict) else []
            )
            for idx, entry in enumerate(arr[:1500]):
                if not isinstance(entry, dict):
                    continue
                u = str(entry.get('url') or entry.get('stream') or entry.get('src') or entry.get('file') or '').strip()
                if not u:
                    continue
                full_u = urllib.parse.urljoin(base_url, u) if base_url else u
                name = str(entry.get('name') or entry.get('title') or f'عنصر #{idx + 1}')
                items.append({
                    'id': 'dodofeed:' + hashlib.sha1(full_u.encode('utf-8')).hexdigest()[:16],
                    'name': name,
                    'poster': str(entry.get('poster') or entry.get('logo') or entry.get('thumbnail') or ''),
                    'description': str(entry.get('description') or ''),
                    'url': full_u,
                })
        except Exception:
            pass
    elif '<' in t[:200]:
        try:
            root = ET.fromstring(t)
            for idx, elem in enumerate(root.iter()):
                tag = elem.tag.split('}')[-1].lower()
                if tag in ('item', 'entry'):
                    title = ''
                    media_u = ''
                    desc = ''
                    thumb = ''
                    for ch in elem:
                        ctag = ch.tag.split('}')[-1].lower()
                        if ctag == 'title' and ch.text:
                            title = ch.text.strip()
                        elif ctag in ('description', 'summary') and ch.text:
                            desc = ch.text.strip()[:250]
                        elif ctag in ('enclosure', 'content') and ch.attrib.get('url'):
                            media_u = ch.attrib['url'].strip()
                        elif ctag in ('thumbnail', 'image') and ch.attrib.get('url'):
                            thumb = ch.attrib['url'].strip()
                    if media_u:
                        items.append({
                            'id': 'dodofeed:' + hashlib.sha1(media_u.encode('utf-8')).hexdigest()[:16],
                            'name': title or f'حلقة #{idx + 1}',
                            'poster': thumb,
                            'description': desc,
                            'url': media_u,
                        })
        except Exception:
            pass
    return items


def handle_feed_addon_request(b64_url: str, rel_path: str, origin: str, media_sig: str, has_ffmpeg: bool, validate_url_fn):
    feed_url = b64u_decode(b64_url)
    cache_key = 'feed:' + feed_url
    with _M3U_LOCK:
        hit = _M3U_CACHE.get(cache_key)
    if hit and time.time() - hit[0] < 600:
        items = hit[1]
    else:
        validate_url_fn(feed_url, allow_local_addons=True)
        req = urllib.request.Request(feed_url, headers={'User-Agent': 'Mozilla/5.0 DodoStream/3.0'})
        with urllib.request.urlopen(req, timeout=15) as r:
            txt = r.read(6_000_000).decode('utf-8', 'replace')
        items = parse_json_or_xml_feed(txt, feed_url)
        with _M3U_LOCK:
            _M3U_CACHE[cache_key] = (time.time(), items)

    host_label = urllib.parse.urlparse(feed_url).hostname or 'Feed'
    if rel_path == 'manifest.json':
        return 200, {
            'id': 'dodo.feed.' + hashlib.sha1(feed_url.encode('utf-8')).hexdigest()[:10],
            'version': '1.0.0',
            'name': f'خلاصة وسائط ({host_label})',
            'description': f'خلاصة مفتوحة تضم {len(items)} عنصر فيديو/صوت.',
            'types': ['tv'],
            'resources': ['catalog', 'meta', 'stream'],
            'idPrefixes': ['dodofeed:'],
            'catalogs': [{'type': 'tv', 'id': 'feed_all', 'name': f'وسائط {host_label}', 'extra': [{'name': 'search', 'isRequired': False}]}],
        }

    parts = [x for x in rel_path.strip('/').split('/') if x]
    if len(parts) >= 3 and parts[0] == 'catalog':
        extra_str = urllib.parse.unquote(parts[3]).removesuffix('.json') if len(parts) > 3 else ''
        q = (urllib.parse.parse_qs(extra_str).get('search') or [''])[0].strip().casefold()
        metas = [
            {
                'id': x['id'],
                'type': 'tv',
                'name': x['name'],
                'poster': x['poster'] or f'{origin}/static/icons/icon-192.png',
                'description': x.get('description') or '',
            }
            for x in items if not q or q in x['name'].casefold()
        ]
        return 200, {'metas': metas[:150]}

    if len(parts) == 3 and parts[0] == 'meta':
        item_id = urllib.parse.unquote(parts[2]).removesuffix('.json')
        found = next((x for x in items if x['id'] == item_id), None)
        return 200, {'meta': {
            'id': found['id'], 'type': 'tv', 'name': found['name'],
            'poster': found['poster'] or f'{origin}/static/icons/icon-192.png',
            'description': found.get('description') or '',
        } if found else None}

    if len(parts) == 3 and parts[0] == 'stream':
        item_id = urllib.parse.unquote(parts[2]).removesuffix('.json')
        found = next((x for x in items if x['id'] == item_id), None)
        return 200, {'streams': build_universal_media_streams(found['url'], found['name'], origin, media_sig, has_ffmpeg) if found else []}

    return 404, {'error': 'مسار غير معروف'}


# ---------------------------------------------------------------------------
# Universal Live FFmpeg HLS Transcoder/Remuxer (/api/universal-hls/<sid>/...)
# Ensures ANY container/codec (.mkv, .avi, .ts, .flv, HEVC, AC3, RTSP, etc.)
# plays IMMEDIATELY inside the browser on iPhone, iPad, Android, and PC!
# ---------------------------------------------------------------------------
class UniversalHlsManager:
    def __init__(self, ffmpeg_bin: str, ffprobe_bin: str, has_ffmpeg: bool, has_ffprobe: bool):
        self.ffmpeg = ffmpeg_bin
        self.ffprobe = ffprobe_bin
        self.has_ffmpeg = has_ffmpeg
        self.has_ffprobe = has_ffprobe
        self.root_dir = os.path.join(tempfile.gettempdir(), 'dodo_universal_hls')
        os.makedirs(self.root_dir, exist_ok=True)
        self.sessions = {}
        self.lock = threading.Lock()
        t = threading.Thread(target=self._reaper_loop, daemon=True)
        t.start()

    def _reaper_loop(self):
        while True:
            time.sleep(30)
            now = time.time()
            stale = []
            with self.lock:
                for sid, sess in list(self.sessions.items()):
                    if now - sess['last_access'] > 180:
                        stale.append((sid, sess))
                        self.sessions.pop(sid, None)
            for _, sess in stale:
                self._cleanup_session(sess)

    def _cleanup_session(self, sess):
        proc = sess.get('proc')
        if proc and proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass
        shutil.rmtree(sess.get('dir', ''), ignore_errors=True)

    def _probe_stream(self, src_url: str, hdr_arg: str):
        vcodec = ''
        pix_fmt = ''
        audio_map = '0:a:0?'
        if not self.has_ffprobe:
            return vcodec, pix_fmt, audio_map
        try:
            cmd = [self.ffprobe, '-v', 'error']
            if hdr_arg:
                cmd += ['-headers', hdr_arg]
            cmd += [
                '-analyzeduration', '1500000',
                '-probesize', '1500000',
                '-show_entries', 'stream=index,codec_type,codec_name,pix_fmt:stream_tags=language',
                '-of', 'json',
                src_url,
            ]
            pr = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=3.5,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
            if pr.returncode == 0:
                streams = json.loads(pr.stdout).get('streams', [])
                for st in streams:
                    if st.get('codec_type') == 'video' and not vcodec:
                        vcodec = str(st.get('codec_name') or '').lower()
                        pix_fmt = str(st.get('pix_fmt') or '').lower()
                for st in streams:
                    if st.get('codec_type') == 'audio':
                        lang = str((st.get('tags') or {}).get('language') or '').lower()
                        if lang in ('ara', 'ar', 'arabic'):
                            audio_map = f"0:{st['index']}"
                            break
        except Exception:
            pass
        return vcodec, pix_fmt, audio_map

    def ensure_session(self, sid: str, src_url: str, headers: dict = None):
        with self.lock:
            existing = self.sessions.get(sid)
            if existing:
                proc = existing.get('proc')
                m3u8_p = os.path.join(existing['dir'], 'index.m3u8')
                if (proc and proc.poll() is None) or os.path.isfile(m3u8_p):
                    existing['last_access'] = time.time()
                    return existing
                self.sessions.pop(sid, None)
                self._cleanup_session(existing)

        sdir = os.path.join(self.root_dir, sid)
        shutil.rmtree(sdir, ignore_errors=True)
        os.makedirs(sdir, exist_ok=True)

        hdr_lines = ['User-Agent: Mozilla/5.0']
        if isinstance(headers, dict):
            for hk, hv in headers.items():
                if hk.lower() in ('referer', 'origin', 'user-agent') and isinstance(hv, str):
                    if hk.lower() == 'user-agent':
                        hdr_lines[0] = f'User-Agent: {hv[:300]}'
                    else:
                        hdr_lines.append(f'{hk}: {hv[:300]}')
        hdr_arg = '\r\n'.join(hdr_lines) + '\r\n' if src_url.startswith(('http://', 'https://')) else ''

        vcodec, pix_fmt, audio_map = self._probe_stream(src_url, hdr_arg)
        can_copy_video = vcodec == 'h264' and pix_fmt in ('', 'yuv420p', 'yuvj420p')

        cmd = [self.ffmpeg, '-y', '-v', 'error']
        if hdr_arg:
            cmd += ['-headers', hdr_arg, '-reconnect', '1', '-reconnect_streamed', '1', '-reconnect_delay_max', '4']
        cmd += ['-fflags', '+genpts+discardcorrupt', '-i', src_url, '-map', '0:v:0?', '-map', audio_map, '-sn']

        if can_copy_video:
            cmd += ['-c:v', 'copy']
        else:
            cmd += [
                '-c:v', 'libx264',
                '-preset', 'ultrafast',
                '-pix_fmt', 'yuv420p',
                '-crf', '23',
                '-maxrate', '4500k',
                '-bufsize', '9000k',
                '-vf', 'scale=trunc(min(1920\\,iw)/2)*2:trunc(ow/a/2)*2',
                '-g', '48',
                '-sc_threshold', '0',
            ]

        cmd += [
            '-c:a', 'aac',
            '-ac', '2',
            '-ar', '44100',
            '-b:a', '160k',
            '-f', 'hls',
            '-hls_time', '4',
            '-hls_list_size', '0',
            '-hls_playlist_type', 'event',
            '-hls_flags', 'independent_segments+temp_file',
            '-hls_segment_filename', os.path.join(sdir, 'seg%04d.ts'),
            os.path.join(sdir, 'index.m3u8'),
        ]

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        sess = {'sid': sid, 'dir': sdir, 'proc': proc, 'url': src_url, 'last_access': time.time()}
        with self.lock:
            self.sessions[sid] = sess

        # Wait up to 9s for first segment + playlist so the browser starts playing immediately!
        m3u8_path = os.path.join(sdir, 'index.m3u8')
        deadline = time.time() + 9.0
        while time.time() < deadline:
            if os.path.isfile(m3u8_path) and os.path.getsize(m3u8_path) > 25:
                break
            if proc.poll() is not None:
                break
            time.sleep(0.18)

        return sess

    def get_session(self, sid: str):
        with self.lock:
            sess = self.sessions.get(sid)
            if sess:
                sess['last_access'] = time.time()
            return sess


def build_universal_media_streams(
    media_url: str,
    title: str,
    origin: str,
    media_sig: str,
    has_ffmpeg: bool,
    headers: dict = None,
    stremio_engine_url: str = 'http://127.0.0.1:11470',
    engine_alive: bool = False,
) -> list:
    """Create universal cross-device stream objects (with automatic proxy & FFmpeg HLS fallback) for ANY media URL."""
    yt_id = extract_youtube_id(media_url)
    if yt_id:
        return [{
            'name': 'YouTube • 📱 مباشر لكل الأجهزة',
            'title': title or 'فيديو يوتيوب',
            'ytId': yt_id,
            'behaviorHints': {'iosReady': True, 'streamType': 'youtube'},
        }]

    if media_url.lower().startswith('magnet:'):
        ih, dn = extract_infohash_from_magnet(media_url)
        if ih and engine_alive:
            sid = hashlib.sha1(f'{ih}:0'.encode('utf-8')).hexdigest()[:14]
            media_u = urllib.parse.quote(f'{stremio_engine_url}/{ih}/0', safe='')
            return [{
                'name': 'محرك البث المباشر • 📱 آيفون وأندرويد',
                'description': dn or title or f'InfoHash: {ih[:12]}...',
                'url': f'{origin}/stremio-engine/hlsv2/{sid}/master.m3u8?mediaURL={media_u}&videoCodecs=h264&audioCodecs=aac&maxAudioChannels=2&sig={media_sig}',
                'behaviorHints': {
                    'notWebReady': False,
                    'proxyHeaders': {'response': {'content-type': 'application/vnd.apple.mpegurl'}},
                },
            }]
        return [{
            'name': 'رابط Magnet',
            'description': dn or title or 'رابط تورنت',
            'infoHash': ih,
            'behaviorHints': {'notWebReady': False},
        }]

    enc_u = urllib.parse.quote(media_url, safe='')
    hdr_json = urllib.parse.quote(json.dumps(headers or {}), safe='')
    sid = hashlib.sha1((media_url + '|' + json.dumps(headers or {}, sort_keys=True)).encode('utf-8')).hexdigest()[:16]
    uhls_url = f'{origin}/api/universal-hls/{sid}/index.m3u8?url={enc_u}&hdrs={hdr_json}&sig={media_sig}' if has_ffmpeg else ''

    clean_path = urllib.parse.urlparse(media_url).path.lower()
    is_hls = clean_path.endswith('.m3u8') or '.m3u8?' in media_url.lower()
    is_mp4 = clean_path.endswith(('.mp4', '.m4v'))
    is_non_browser_container = clean_path.endswith(('.mkv', '.avi', '.flv', '.wmv', '.ts', '.m2ts', '.mpg', '.mpeg')) or media_url.lower().startswith(('rtmp://', 'rtsp://', 'mms://'))
    proxy_ext = '.m3u8' if is_hls else ''
    proxy_url = f'{origin}/api/stream-proxy{proxy_ext}?url={enc_u}&hdrs={hdr_json}&sig={media_sig}' if media_url.startswith(('http://', 'https://')) else ''

    hls_hints = {
        'notWebReady': False,
    }
    direct_hints = {
        'notWebReady': False,
    }

    streams = []
    if is_non_browser_container and uhls_url:
        streams.append({
            'name': 'تحويل فوري شامل (HLS) • كل الأجهزة',
            'description': f'{title} — تحويل حي لصيغة H.264/AAC',
            'url': uhls_url,
            'behaviorHints': hls_hints,
        })
    else:
        primary_url = proxy_url if (headers or is_hls) and proxy_url else media_url
        streams.append({
            'name': 'تشغيل فوري مباشر • كل الأجهزة',
            'description': title,
            'url': primary_url,
            'behaviorHints': direct_hints,
        })
        if uhls_url:
            streams.append({
                'name': 'وضع التوافق الشامل (FFmpeg HLS)',
                'description': f'{title} — متوافق مع جميع المتصفحات',
                'url': uhls_url,
                'behaviorHints': hls_hints,
            })
    return streams


# ---------------------------------------------------------------------------
# Master Link Classifier (/api/resolve-link) — Accepts ANY URL or Format!
# ---------------------------------------------------------------------------
def resolve_any_link(
    raw_input: str,
    origin: str,
    media_sig: str,
    has_ffmpeg: bool,
    stremio_engine_url: str,
    engine_alive_fn,
    validate_url_fn,
):
    url = clean_any_input_url(raw_input)
    if not url:
        return 400, {'ok': False, 'error': 'يرجى إدخال رابط صالح'}

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme in ('http', 'https') and not parsed.hostname:
        return 400, {
            'ok': False,
            'error': 'الرابط ناقص اسم النطاق (مثل https://opensubtitles.strem.io/stremio/v1) — تأكد من نسخ الرابط كاملًا.',
        }

    # 1. Magnet or raw 40-char infoHash
    if url.lower().startswith('magnet:'):
        streams = build_universal_media_streams(
            url, 'بث تورنت مباشر', origin, media_sig, has_ffmpeg,
            stremio_engine_url=stremio_engine_url, engine_alive=engine_alive_fn()
        )
        return 200, {'ok': True, 'kind': 'media', 'title': streams[0].get('title') or 'بث مباشر', 'streams': streams}

    # 2. YouTube link
    if extract_youtube_id(url):
        streams = build_universal_media_streams(url, 'فيديو يوتيوب', origin, media_sig, has_ffmpeg)
        return 200, {'ok': True, 'kind': 'media', 'title': 'فيديو يوتيوب', 'streams': streams}

    # 3. RTMP / RTSP / MMS live streams
    if url.lower().startswith(('rtmp://', 'rtsp://', 'mms://')):
        streams = build_universal_media_streams(url, 'بث مباشر حي', origin, media_sig, has_ffmpeg)
        return 200, {'ok': True, 'kind': 'media', 'title': 'بث مباشر حي', 'streams': streams}

    path_low = parsed.path.lower()

    # 4. Known direct video/audio file extensions -> play immediately!
    if path_low.endswith(MEDIA_EXTENSIONS):
        fname = urllib.parse.unquote(os.path.basename(parsed.path)) or 'ملف وسائط مباشر'
        streams = build_universal_media_streams(url, fname, origin, media_sig, has_ffmpeg)
        return 200, {'ok': True, 'kind': 'media', 'title': fname, 'streams': streams}

    # Validate HTTP/HTTPS outbound URL
    try:
        validate_url_fn(url, allow_local_addons=True)
    except Exception as exc:
        return 400, {'ok': False, 'error': f'رابط غير مسموح أو تعذر الوصول لنطاقه: {exc}'}

    # 5. Stremio Legacy v1/v2 quick check if URL clearly has /stremio/v1 or /q.json
    if '/stremio/v1' in path_low or path_low.endswith('/q.json'):
        ep, mf_raw, methods = probe_legacy_v1_endpoint(url, validate_url_fn)
        if mf_raw:
            b64_ep = b64u_encode(ep)
            transport_url = f'{origin}/api/legacy-v1/{b64_ep}/manifest.json'
            return 200, {
                'ok': True,
                'kind': 'addon',
                'transportUrl': transport_url,
                'manifest': build_v3_manifest_from_legacy(ep, mf_raw, methods),
            }

    # 6. Probe URL content (headers + first up to 512 KB) to auto-detect ANY format!
    headers_probe = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
    }
    ctype = ''
    sample = b''
    final_url = url
    try:
        req = urllib.request.Request(url, headers=headers_probe)
        with urllib.request.urlopen(req, timeout=12) as r:
            final_url = r.geturl() or url
            ctype = (r.headers.get('Content-Type') or '').lower()
            # If binary video/audio stream, don't download whole file! Read tiny header only.
            if any(ctype.startswith(p) for p in ('video/', 'audio/')):
                fname = urllib.parse.unquote(os.path.basename(urllib.parse.urlparse(final_url).path)) or 'بث مباشر'
                streams = build_universal_media_streams(final_url, fname, origin, media_sig, has_ffmpeg)
                return 200, {'ok': True, 'kind': 'media', 'title': fname, 'streams': streams}
            sample = r.read(1_500_000)
    except Exception:
        sample = b''

    # 6a. Is it a .torrent file?
    if path_low.endswith('.torrent') or 'bittorrent' in ctype or sample.startswith(b'd8:announce') or sample.startswith(b'd13:announce-list') or sample.startswith(b'd4:info'):
        ih = extract_infohash_from_torrent_bytes(sample)
        if ih:
            mag = f'magnet:?xt=urn:btih:{ih}'
            streams = build_universal_media_streams(
                mag, 'ملف تورنت مباشر', origin, media_sig, has_ffmpeg,
                stremio_engine_url=stremio_engine_url, engine_alive=engine_alive_fn()
            )
            return 200, {'ok': True, 'kind': 'media', 'title': 'ملف تورنت مباشر', 'streams': streams}

    text_body = sample.decode('utf-8', 'replace').strip() if sample else ''

    # 6b. Is it #EXTM3U? Distinguish HLS Video Stream vs IPTV Channel Playlist!
    if text_body.startswith('#EXTM3U') or 'mpegurl' in ctype or path_low.endswith(('.m3u', '.m3u8')):
        if '#EXT-X-TARGETDURATION' in text_body or '#EXT-X-STREAM-INF' in text_body or '#EXT-X-MEDIA-SEQUENCE' in text_body:
            fname = urllib.parse.unquote(os.path.basename(urllib.parse.urlparse(final_url).path)) or 'بث HLS مباشر'
            streams = build_universal_media_streams(final_url, fname, origin, media_sig, has_ffmpeg)
            return 200, {'ok': True, 'kind': 'media', 'title': fname, 'streams': streams}
        if '#EXTINF:' in text_body:
            items = parse_m3u_playlist(text_body, final_url)
            if items:
                with _M3U_LOCK:
                    _M3U_CACHE[final_url] = (time.time(), items)
                b64_u = b64u_encode(final_url)
                transport_url = f'{origin}/api/m3u-addon/{b64_u}/manifest.json'
                _, mf = handle_m3u_addon_request(b64_u, 'manifest.json', origin, media_sig, has_ffmpeg, validate_url_fn)
                return 200, {'ok': True, 'kind': 'addon', 'transportUrl': transport_url, 'manifest': mf}

    # 6c. Is the body directly a valid Stremio v3 manifest JSON, or a custom JSON video list?
    if text_body.startswith(('{', '[')):
        try:
            obj = json.loads(text_body)
            if isinstance(obj, dict) and obj.get('id') and obj.get('name') and ('resources' in obj or 'catalogs' in obj or 'types' in obj):
                return 200, {'ok': True, 'kind': 'addon', 'transportUrl': final_url, 'manifest': obj}
            feed_items = parse_json_or_xml_feed(text_body, final_url)
            if feed_items:
                with _M3U_LOCK:
                    _M3U_CACHE['feed:' + final_url] = (time.time(), feed_items)
                b64_u = b64u_encode(final_url)
                transport_url = f'{origin}/api/feed-addon/{b64_u}/manifest.json'
                _, mf = handle_feed_addon_request(b64_u, 'manifest.json', origin, media_sig, has_ffmpeg, validate_url_fn)
                return 200, {'ok': True, 'kind': 'addon', 'transportUrl': transport_url, 'manifest': mf}
        except Exception:
            pass

    # 6c2. Is it an RSS / Atom XML media feed?
    if text_body.startswith('<?xml') or '<rss' in text_body[:400].lower() or '<feed' in text_body[:400].lower():
        feed_items = parse_json_or_xml_feed(text_body, final_url)
        if feed_items:
            with _M3U_LOCK:
                _M3U_CACHE['feed:' + final_url] = (time.time(), feed_items)
            b64_u = b64u_encode(final_url)
            transport_url = f'{origin}/api/feed-addon/{b64_u}/manifest.json'
            _, mf = handle_feed_addon_request(b64_u, 'manifest.json', origin, media_sig, has_ffmpeg, validate_url_fn)
            return 200, {'ok': True, 'kind': 'addon', 'transportUrl': transport_url, 'manifest': mf}

    # 6d. Try finding a Stremio v3 /manifest.json relative to URL or extracted from HTML!
    v3_candidates = []
    # Scan HTML for stremio://.../manifest.json or https://.../manifest.json
    if text_body:
        for m_link in re.findall(r'(?:stremios?|https?)://[^\s"\'<>]+?manifest\.json(?:\?[^\s"\'<>]*)?', text_body, re.I):
            cleaned = clean_any_input_url(m_link)
            if cleaned not in v3_candidates:
                v3_candidates.append(cleaned)

    base_no_cfg = re.sub(r'/configure(?:/.*)?$', '', final_url.split('?')[0]).rstrip('/')
    if not base_no_cfg.endswith('/manifest.json'):
        v3_candidates.append(base_no_cfg + '/manifest.json')
    root_origin = f'{parsed.scheme}://{parsed.netloc}'
    if root_origin + '/manifest.json' not in v3_candidates:
        v3_candidates.append(root_origin + '/manifest.json')

    for cand in v3_candidates[:5]:
        try:
            validate_url_fn(cand, allow_local_addons=True)
            req = urllib.request.Request(cand, headers=headers_probe)
            with urllib.request.urlopen(req, timeout=8) as r:
                mf = json.loads(r.read(2_000_000).decode('utf-8', 'replace'))
            if isinstance(mf, dict) and mf.get('id') and mf.get('name'):
                return 200, {'ok': True, 'kind': 'addon', 'transportUrl': cand, 'manifest': mf}
        except Exception:
            continue

    # 6e. Try Stremio Legacy v1/v2 JSON-RPC probe on any URL!
    ep, mf_raw, methods = probe_legacy_v1_endpoint(final_url, validate_url_fn)
    if mf_raw:
        b64_ep = b64u_encode(ep)
        transport_url = f'{origin}/api/legacy-v1/{b64_ep}/manifest.json'
        return 200, {
            'ok': True,
            'kind': 'addon',
            'transportUrl': transport_url,
            'manifest': build_v3_manifest_from_legacy(ep, mf_raw, methods),
        }

    # 6f. Fallback: if it's an octet-stream or unknown HTTP URL, treat as direct playable stream!
    if ctype and 'html' not in ctype and 'json' not in ctype:
        fname = urllib.parse.unquote(os.path.basename(parsed.path)) or 'رابط مباشر'
        streams = build_universal_media_streams(final_url, fname, origin, media_sig, has_ffmpeg)
        return 200, {'ok': True, 'kind': 'media', 'title': fname, 'streams': streams}

    return 422, {
        'ok': False,
        'error': 'لم يتم العثور على إضافة أو قائمة تشغيل أو ملف فيديو مباشر في هذه الصفحة.',
    }
