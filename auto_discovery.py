# -*- coding: utf-8 -*-
"""Cross-platform discovery helper based on provider descriptions (Linux & Windows)."""
import json, os, re, shutil, subprocess, threading, time, urllib.request

HEADERS = {
    'User-Agent': 'Mozilla/5.0',
    'Referer': 'https://cartoony.net/',
    'Origin': 'https://cartoony.net',
}
KEY = b'7annaba3l_loves_crypto_safe_key!'.hex()
OPENSSL = shutil.which('openssl') or r'C:\Program Files\Git\usr\bin\openssl.exe'


def decrypt(obj):
    iv = obj.get('iv', '')
    encrypted = obj.get('encryptedData', '')
    if (
        not re.fullmatch(r'[0-9a-fA-F]{32}', iv)
        or not re.fullmatch(r'[0-9a-fA-F]+', encrypted)
        or len(encrypted) > 20_000_000
    ):
        raise ValueError('bad encrypted response')
    p = subprocess.run(
        [OPENSSL, 'enc', '-d', '-aes-256-cbc', '-K', KEY, '-iv', iv],
        input=bytes.fromhex(encrypted),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=20,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        check=True,
    )
    return json.loads(p.stdout)


def request(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=20) as r:
        return json.loads(r.read(10_000_000))


def eligible(s, kind):
    title = str(s.get('title', ''))
    cat = str(s.get('category', ''))
    tech = str(s.get('description_technical', ''))
    if re.search(r'مدبلج\s*[/،+و-]\s*مترجم|دبلج منها|حلقات مترجمة', cat + ' ' + tech):
        return False
    return (
        bool(re.fullmatch(r'\d{1,12}', str(s.get('id', ''))))
        and kind + ' مدبلج' in cat
        and 'مترجم' not in cat
        and 'مترجم' not in title
        and s.get('enabled', True) is not False
    )


def identify_channels(show):
    text = ' '.join(str(show.get(k, '') or '') for k in ('title', 'description', 'description_technical', 'category'))
    patterns = {
        'cn': r'كرتون\s+نتورك|cartoon\s+network',
        'nick': r'نيكلوديون|نيكولوديون|nickelodeon',
        'mbc3': r'\bmbc\s*3\b|إم\s*بي\s*سي\s*(?:3|٣|ثلاثة)|ام\s*بي\s*سي\s*(?:3|٣|ثلاثة)',
        'spacetoon': r'سبيستون|spacetoon',
    }
    return [name for name, pattern in patterns.items() if re.search(pattern, text, re.I)]


def is_disney(show):
    text = ' '.join(str(show.get(k, '') or '') for k in ('title', 'category'))
    return bool(re.search(r'ديزني|disney', text, re.I))


class Discovery:
    def __init__(self, here, series, movies, videos):
        self.here = here
        self.series = series
        self.movies = movies
        self.videos = videos
        self.blocked = set(json.load(open(os.path.join(here, 'auto_blocked.json'), encoding='utf8')))
        self.lock = threading.RLock()
        self.status = {'state': 'بانتظار التحديث', 'last_update': None, 'error': None}
        self.path = os.path.join(here, 'auto_saved.json')
        self.cursor = 0
        with open(os.path.join(here, 'auto_seed.json'), encoding='utf8') as f:
            seed = json.load(f)
            self.ids = seed['series_ids']
            self.views = seed.get('views', {})
            self.channels = seed.get('channels', {})
            self.disney = seed.get('disney', [])
        try:
            with open(self.path, encoding='utf8') as f:
                saved = json.load(f)
            self.ids = list(dict.fromkeys(self.ids + saved.get('series_ids', [])))
            self.cursor = int(saved.get('cursor', 0))
            self.views.update(saved.get('views', {}))
            self.channels.update(saved.get('channels', {}))
            self.disney = list(dict.fromkeys(self.disney + saved.get('disney', [])))
            savedseries = {k: v for k, v in saved.get('series', {}).items() if k not in self.blocked}
            savedmovies = {k: v for k, v in saved.get('movies', {}).items() if k not in self.blocked}
            self.series.update(savedseries)
            self.movies.update(savedmovies)
            for obj in list(savedseries.values()) + list(savedmovies.values()):
                self.videos.update(ep['video_id'] for ep in obj['episodes'])
        except (OSError, ValueError, KeyError, TypeError):
            pass
        self.ids = [str(i) for i in self.ids if str(i) in self.series and str(i) not in self.blocked]

    def snapshot(self):
        with self.lock:
            return sorted(self.ids, key=lambda i: (-self.views.get(i, 0), i))

    def channel_ids(self, channel):
        with self.lock:
            return [i for i in self.snapshot() if channel in self.channels.get(i, [])]

    def disney_ids(self):
        with self.lock:
            return [i for i in self.disney if i not in self.blocked and i in self.movies]

    def get_status(self):
        with self.lock:
            return dict(self.status, series_count=len(self.ids), movie_count=len(self.movies))

    def save(self, newseries, newmovies):
        old = {}
        try:
            with open(self.path, encoding='utf8') as f:
                old = json.load(f)
        except (OSError, ValueError):
            pass
        oldseries = old.get('series', {})
        oldseries.update(newseries)
        oldmovies = old.get('movies', {})
        oldmovies.update(newmovies)
        body = {
            'series_ids': self.ids,
            'series': oldseries,
            'movies': oldmovies,
            'cursor': self.cursor,
            'views': self.views,
            'channels': self.channels,
            'disney': self.disney,
        }
        temp = self.path + '.tmp'
        with open(temp, 'w', encoding='utf8') as f:
            json.dump(body, f, ensure_ascii=False, separators=(',', ':'))
        os.replace(temp, self.path)

    def make_record(self, s, kind):
        ident = str(s['id'])
        eps = decrypt(request('https://cartoony.net/api/episodes?id=' + ident))
        out = []
        if not isinstance(eps, list):
            raise ValueError('bad episodes')
        for e in eps:
            title = str(e.get('title', ''))
            vid = str(e.get('video_id', ''))
            if 'مترجم' in title or not e.get('hls_supported') or not re.fullmatch(r'[0-9a-fA-F-]{1,64}', vid):
                continue
            if kind == 'series' and not re.match(r'^\s*[0-9]+[.\- ]', title):
                continue
            out.append({'id': int(e['id']), 'title': title, 'video_id': vid})
        if not out or (kind == 'movie' and len(out) != 1):
            return None
        poster = str(s.get('poster_cover_alt') or s.get('poster_cover') or '')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', poster):
            poster = ''
        result = {
            'id': int(ident),
            'title': str(s.get('title', ''))[:300],
            'description': str(s.get('description', ''))[:10000],
            'poster': 'https://cartoony.net/assets/img/posters/' + poster,
            'episodes': out,
        }
        if kind == 'movie':
            result['audio_status'] = 'المصدر يصفه بالمدبلج؛ اللغة والدبلجة غير مؤكدتين بالاستماع'
        return result

    def refresh(self):
        with self.lock:
            self.status.update(state='جارٍ فحص المصدر', error=None)
        try:
            shows = decrypt(request('https://cartoony.net/api/tvshows'))
            if not isinstance(shows, list) or not shows:
                raise ValueError('empty or invalid catalog')
            desired = [
                s
                for s in shows
                if isinstance(s, dict)
                and str(s.get('id', '')) not in self.blocked
                and (eligible(s, 'مسلسل') or eligible(s, 'فيلم'))
            ]
            if not desired:
                raise ValueError('no eligible titles; preserving previous catalog')
            newseries = {}
            newmovies = {}
            processed = 0
            for item in shows:
                if isinstance(item, dict) and re.fullmatch(r'\d{1,12}', str(item.get('id', ''))):
                    try:
                        self.views[str(item['id'])] = max(0, int(item.get('views') or 0))
                    except (ValueError, TypeError):
                        pass
            valid_ids = {str(s['id']) for s in desired if eligible(s, 'مسلسل')}
            with self.lock:
                self.ids = [i for i in self.ids if i in valid_ids]
                self.channels = {str(s['id']): identify_channels(s) for s in desired if eligible(s, 'مسلسل')}
                self.disney = [str(s['id']) for s in desired if eligible(s, 'فيلم') and is_disney(s)]
            if desired:
                offset = self.cursor % len(desired)
                desired = desired[offset:] + desired[:offset]
            visited = 0
            for s in desired:
                visited += 1
                ident = str(s['id'])
                kind = 'series' if eligible(s, 'مسلسل') else 'movie'
                if (kind == 'series' and ident in self.series) or (kind == 'movie' and ident in self.movies):
                    if kind == 'series' and ident in self.series and ident not in self.ids:
                        self.ids.append(ident)
                    continue
                if processed >= 12:
                    visited -= 1
                    break
                processed += 1
                try:
                    obj = self.make_record(s, kind)
                    if not obj:
                        continue
                    if kind == 'series':
                        newseries[ident] = obj
                    else:
                        newmovies[ident] = obj
                except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
                    pass
                time.sleep(1.0)
            self.cursor = (self.cursor + visited) % len(desired) if desired else 0
            with self.lock:
                self.series.update(newseries)
                self.movies.update(newmovies)
                for ident, obj in newseries.items():
                    if ident not in self.ids:
                        self.ids.append(ident)
                    self.videos.update(e['video_id'] for e in obj['episodes'])
                for obj in newmovies.values():
                    self.videos.update(e['video_id'] for e in obj['episodes'])
                self.save(newseries, newmovies)
                self.status.update(
                    state='اكتمل فحص الفهرس',
                    last_update=time.strftime('%Y-%m-%d %H:%M:%S'),
                    error=None,
                    added_series=len(newseries),
                    added_movies=len(newmovies),
                    attempted=processed,
                )
        except Exception as exc:
            with self.lock:
                self.status.update(state='تعذر التحديث؛ القائمة السابقة محفوظة', error=str(exc)[:200])

    def start(self):
        def loop():
            time.sleep(60)
            while True:
                self.refresh()
                time.sleep(24 * 60 * 60)

        threading.Thread(target=loop, name='dodo-cartoon-discovery', daemon=True).start()
