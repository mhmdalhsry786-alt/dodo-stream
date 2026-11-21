(function () {
  'use strict';

  // Platform Detection
  const ua = navigator.userAgent || '';
  const isIOS =
    /iPad|iPhone|iPod/.test(ua) ||
    (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const isAndroid = /Android/i.test(ua);

  // Storage Keys (Local per-friend device for privacy)
  const KEY_INVITE = 'dodo_invite_code';
  const KEY_LIBRARY = 'dodo_library_v1';
  const KEY_CONTINUE = 'dodo_continue_v1';
  const KEY_WATCHED = 'dodo_watched_eps_v1';
  const KEY_USER_ADDONS = 'dodo_user_addons_v1';

  // App State
  const state = {
    appName: 'دودو ستريم',
    mediaSig: '',
    addons: [], // Combined default + user addons with loaded manifests
    heroItem: null,
    currentMeta: null,
    currentEpisodes: [],
    currentEpisodeObj: null,
    currentStream: null,
    hlsInstance: null,
    discoverSkip: 0,
    deferredInstallPrompt: null,
    adminPin: '',
  };

  // Helpers
  const $ = (id) => document.getElementById(id);

  function showToast(msg) {
    const t = $('dodo-toast');
    if (!t) return;
    t.textContent = msg;
    t.classList.remove('hidden');
    clearTimeout(showToast._tid);
    showToast._tid = setTimeout(() => t.classList.add('hidden'), 3000);
  }

  function readJson(key, fallback) {
    try {
      const raw = localStorage.getItem(key);
      return raw ? JSON.parse(raw) : fallback;
    } catch (e) {
      return fallback;
    }
  }

  function writeJson(key, val) {
    try {
      localStorage.setItem(key, JSON.stringify(val));
    } catch (e) {}
  }

  function getInviteHeader() {
    const code = localStorage.getItem(KEY_INVITE) || '';
    return code ? { 'X-Dodo-Invite': code } : {};
  }

  async function apiFetch(url, opts = {}) {
    const headers = Object.assign({}, getInviteHeader(), opts.headers || {});
    const res = await fetch(url, Object.assign({}, opts, { headers }));
    if (res.status === 401) {
      showAuthGate();
      throw new Error('يتطلب رمز الدعوة');
    }
    return res;
  }

  function isInternalAddonUrl(u) {
    if (!u) return false;
    return (
      u.startsWith('/addons/') ||
      u.startsWith('/api/legacy-v1/') ||
      u.startsWith('/api/m3u-addon/') ||
      u.startsWith('/api/feed-addon/') ||
      u.startsWith(window.location.origin + '/addons/') ||
      u.startsWith(window.location.origin + '/api/legacy-v1/') ||
      u.startsWith(window.location.origin + '/api/m3u-addon/') ||
      u.startsWith(window.location.origin + '/api/feed-addon/')
    );
  }

  // Normalize & Call Any Stremio Addon Resource
  function buildAddonResourceUrl(transportUrl, resourcePath) {
    const base = transportUrl.replace(/\/manifest\.json(\?.*)?$/i, '').replace(/\/+$/, '');
    const full = base + '/' + resourcePath.replace(/^\/+/, '');
    if (isInternalAddonUrl(base)) {
      const joiner = full.includes('?') ? '&' : '?';
      return state.mediaSig ? `${full}${joiner}sig=${encodeURIComponent(state.mediaSig)}` : full;
    }
    return '/api/addon-proxy?url=' + encodeURIComponent(full);
  }

  async function fetchAddonJson(transportUrl, resourcePath) {
    const url = buildAddonResourceUrl(transportUrl, resourcePath);
    const res = await apiFetch(url);
    if (!res.ok) throw new Error('Addon HTTP ' + res.status);
    return res.json();
  }

  // Register Service Worker & Capture Android Install Prompt
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('/sw.js').catch(() => {});
    });
  }

  window.addEventListener('beforeinstallprompt', (e) => {
    e.preventDefault();
    state.deferredInstallPrompt = e;
  });

  // Auth Gate
  function showAuthGate() {
    $('app-shell').classList.add('hidden');
    $('auth-gate').classList.remove('hidden');
  }

  $('auth-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const code = $('invite-input').value.trim();
    const errEl = $('auth-error');
    errEl.classList.add('hidden');
    try {
      const res = await fetch('/api/auth/verify', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ invite_code: code }),
      });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        errEl.textContent = data.error || 'رمز الدعوة غير صحيح';
        errEl.classList.remove('hidden');
        return;
      }
      localStorage.setItem(KEY_INVITE, code);
      const mLink = $('pwa-manifest-link');
      if (mLink) mLink.href = '/manifest.webmanifest?invite=' + encodeURIComponent(code);
      $('auth-gate').classList.add('hidden');
      await bootstrapApp();
    } catch (err) {
      errEl.textContent = 'تعذر الاتصال بالخادم';
      errEl.classList.remove('hidden');
    }
  });

  // Navigation
  function switchView(viewName) {
    document.querySelectorAll('.view-page').forEach((el) => el.classList.remove('active'));
    const target = $('view-' + viewName);
    if (target) target.classList.add('active');

    document.querySelectorAll('.nav-btn, .bnav-btn').forEach((btn) => {
      btn.classList.toggle('active', btn.dataset.nav === viewName);
    });

    if (viewName === 'library') renderLibrary();
    if (viewName === 'addons') renderAddonsView();
    if (viewName === 'discover' && !$('discover-grid').children.length) {
      populateDiscoverCatalogs();
      loadDiscoverItems(true);
    }
    if (viewName === 'share') updateShareUrlBox();
    $('views-root').scrollTop = 0;
  }

  document.addEventListener('click', (e) => {
    const navEl = e.target.closest('[data-nav]');
    if (navEl) {
      e.preventDefault();
      switchView(navEl.dataset.nav);
    }
  });

  // Branding Update
  function applyBranding(name) {
    state.appName = name || 'دودو ستريم';
    document.title = state.appName;
    $('auth-app-title').textContent = state.appName;
    $('sidebar-brand-name').textContent = state.appName;
    $('mobile-brand-name').textContent = state.appName;
  }

  // Bootstrap App & Load Addon Manifests
  async function bootstrapApp() {
    try {
      const res = await fetch('/api/bootstrap', { headers: getInviteHeader() });
      const data = await res.json();
      applyBranding(data.app_name);
      if (!data.authorized) {
        showAuthGate();
        return;
      }
      state.mediaSig = data.media_sig || '';
      $('auth-gate').classList.add('hidden');
      $('app-shell').classList.remove('hidden');

      await initializeAddons(data.default_addons || []);
      renderContinueWatching();
      await loadBoardShelves();
      populateDiscoverCatalogs();
      updateShareUrlBox();
    } catch (err) {
      console.error(err);
    }
  }

  async function initializeAddons(serverDefaults) {
    const userSaved = readJson(KEY_USER_ADDONS, []);
    const merged = [...serverDefaults];
    for (const uaItem of userSaved) {
      if (uaItem && uaItem.transportUrl && !merged.some((x) => x.transportUrl === uaItem.transportUrl)) {
        merged.push(uaItem);
      }
    }
    const resolved = await Promise.all(
      merged.map(async (item) => {
        if (item.manifest) return item;
        try {
          const mfUrl = isInternalAddonUrl(item.transportUrl)
            ? buildAddonResourceUrl(item.transportUrl, 'manifest.json')
            : '/api/addon-proxy?url=' + encodeURIComponent(item.transportUrl);
          const r = await apiFetch(mfUrl);
          if (!r.ok) return null;
          const mf = await r.json();
          return Object.assign({}, item, {
            id: mf.id || item.id,
            name: item.name || mf.name,
            description: item.description || mf.description,
            manifest: mf,
          });
        } catch (e) {
          return null;
        }
      })
    );
    state.addons = resolved.filter((x) => x && x.manifest);
  }

  // Poster Card Builder
  function createPosterCard(item, addonTransport, extraBadge, progressPct) {
    const card = document.createElement('div');
    card.className = 'poster-card';
    const posterUrl = item.poster || '';
    const typeLabel = item.type === 'movie' ? 'فيلم' : 'مسلسل';

    const wrap = document.createElement('div');
    wrap.className = 'poster-img-wrap';

    if (posterUrl) {
      const img = document.createElement('img');
      img.loading = 'lazy';
      img.alt = item.name || '';
      img.src = posterUrl;
      img.onerror = () => {
        img.remove();
        const fb = document.createElement('div');
        fb.className = 'poster-fallback';
        fb.textContent = item.name || '';
        wrap.prepend(fb);
      };
      wrap.appendChild(img);
    } else {
      const fb = document.createElement('div');
      fb.className = 'poster-fallback';
      fb.textContent = item.name || '';
      wrap.appendChild(fb);
    }

    const b = document.createElement('span');
    b.className = 'card-badge';
    b.textContent = extraBadge || (item.imdbRating ? `⭐ ${item.imdbRating}` : typeLabel);
    wrap.appendChild(b);

    // 1-Click Instant Play button right on every poster card!
    const quickPlay = document.createElement('button');
    quickPlay.type = 'button';
    quickPlay.title = 'تشغيل تلقائي فوري';
    quickPlay.textContent = '▶';
    quickPlay.style.cssText =
      'position:absolute;bottom:10px;left:8px;width:34px;height:34px;border-radius:50%;border:0;background:linear-gradient(135deg,#9333ea,#06b6d4);color:#fff;font-size:0.9rem;cursor:pointer;display:flex;align-items:center;justify-content:center;box-shadow:0 4px 14px rgba(0,0,0,0.65);z-index:4;';
    quickPlay.addEventListener('click', (ev) => {
      ev.stopPropagation();
      quickPlayItemNow(item, addonTransport);
    });
    wrap.appendChild(quickPlay);

    if (typeof progressPct === 'number' && progressPct > 0) {
      const tr = document.createElement('div');
      tr.className = 'progress-track';
      const fl = document.createElement('div');
      fl.className = 'progress-fill';
      fl.style.width = Math.min(100, Math.max(4, progressPct)) + '%';
      tr.appendChild(fl);
      wrap.appendChild(tr);
    }

    const titleEl = document.createElement('div');
    titleEl.className = 'poster-title';
    titleEl.textContent = item.name || 'بدون عنوان';

    card.appendChild(wrap);
    card.appendChild(titleEl);

    if (item.subtitleText) {
      const sub = document.createElement('div');
      sub.className = 'poster-sub';
      sub.textContent = item.subtitleText;
      card.appendChild(sub);
    }

    card.addEventListener('click', () => {
      if (item.resumeEntry) {
        quickPlayItemNow(item, addonTransport);
      } else {
        openItemDetail(item, addonTransport);
      }
    });
    return card;
  }

  // Board (Home) Shelves
  async function loadBoardShelves() {
    const container = $('board-shelves');
    container.innerHTML = '';

    const cartoonAddon = state.addons.find((a) => a.id === 'dodo.cartoonmemories.catalog');
    const bidoooAddon = state.addons.find((a) => a.id === 'org.bidooo.arabic.addon');
    const cinemetaAddon = state.addons.find((a) => a.id === 'com.linvo.cinemeta');
    const streamCatsAddon = state.addons.find((a) => a.id === 'pw.ers.netflix-catalog');
    const kitsuAddon = state.addons.find((a) => a.id === 'anime.kitsu.strem.fun');
    const pubDomAddon = state.addons.find((a) => a.id === 'org.stremio.publicdomainmovies');

    const shelvesPlan = [];
    if (cartoonAddon) {
      shelvesPlan.push(
        { title: '📺 ذكريات MBC3', addon: cartoonAddon, type: 'series', catId: 'mbc3', badge: 'مدبلج' },
        { title: '🧽 ذكريات نيكلوديون', addon: cartoonAddon, type: 'series', catId: 'nick', badge: 'مدبلج' },
        { title: '⚡ ذكريات كرتون نتورك بالعربية', addon: cartoonAddon, type: 'series', catId: 'cn', badge: 'مدبلج' },
        { title: '🚀 ذكريات سبيستون', addon: cartoonAddon, type: 'series', catId: 'spacetoon', badge: 'مدبلج' },
        { title: '🏰 أفلام ديزني المدبلجة', addon: cartoonAddon, type: 'movie', catId: 'disney_films', badge: 'ديزني' },
        { title: '🎬 أفلام الكرتون المدبلجة', addon: cartoonAddon, type: 'movie', catId: 'cartoony_films', badge: 'فيلم مدبلج' }
      );
    }
    if (bidoooAddon) {
      shelvesPlan.push(
        { title: '🎙️ أفلام مدبلجة وعربية (Bidooo)', addon: bidoooAddon, type: 'movie', catId: 'bidooo_movies', badge: 'عربي' },
        { title: '🎭 مسلسلات مدبلجة مختارة', addon: bidoooAddon, type: 'series', catId: 'bidooo_series', badge: 'مدبلج' }
      );
    }
    if (streamCatsAddon) {
      shelvesPlan.push(
        { title: '🔴 رائج الآن على Netflix — أفلام', addon: streamCatsAddon, type: 'movie', catId: 'nfx', badge: 'Netflix' },
        { title: '🔴 رائج الآن على Netflix — مسلسلات', addon: streamCatsAddon, type: 'series', catId: 'nfx', badge: 'Netflix' },
        { title: '🟣 روائع HBO Max — مسلسلات', addon: streamCatsAddon, type: 'series', catId: 'hbm', badge: 'HBO' },
        { title: '🍎 أعمال Apple TV+ المختارة', addon: streamCatsAddon, type: 'series', catId: 'atp', badge: 'Apple TV+' },
        { title: '🔵 مختارات Prime Video', addon: streamCatsAddon, type: 'movie', catId: 'amp', badge: 'Prime' }
      );
    }
    if (cinemetaAddon) {
      shelvesPlan.push(
        { title: '🔥 الأفلام العالمية الرائجة (IMDb)', addon: cinemetaAddon, type: 'movie', catId: 'top' },
        { title: '🌟 المسلسلات العالمية الرائجة (IMDb)', addon: cinemetaAddon, type: 'series', catId: 'top' },
        { title: '🏆 الأعلى تقييمًا في السينما', addon: cinemetaAddon, type: 'movie', catId: 'imdbRating' }
      );
    }
    if (kitsuAddon && kitsuAddon.manifest.catalogs && kitsuAddon.manifest.catalogs.length) {
      const kc = kitsuAddon.manifest.catalogs[0];
      shelvesPlan.push({ title: '🗾 الأنمي الرائج', addon: kitsuAddon, type: kc.type, catId: kc.id, badge: 'أنمي' });
    }
    if (pubDomAddon) {
      shelvesPlan.push({ title: '🎞️ السينما الكلاسيكية المفتوحة', addon: pubDomAddon, type: 'movie', catId: 'publicdomainmovies', badge: 'مجاني' });
    }

    // Automatically render catalogs from ANY custom or community Stremio addon installed!
    for (const addon of state.addons) {
      if (addon.builtIn) continue;
      const cats = (addon.manifest && addon.manifest.catalogs) || [];
      for (const c of cats.slice(0, 6)) {
        const reqExtra = Array.isArray(c.extra) && c.extra.some((ex) => ex.isRequired);
        if (reqExtra) continue;
        shelvesPlan.push({
          title: `🧩 ${c.name || addon.name}`,
          addon,
          type: c.type || 'movie',
          catId: c.id,
          badge: addon.name,
        });
      }
    }

    // Create placeholder DOM sections so order stays consistent
    const domSlots = shelvesPlan.map((sp) => {
      const sec = document.createElement('div');
      sec.className = 'shelf-section hidden';
      sec.innerHTML = `<div class="shelf-head"><h2>${sp.title}</h2></div><div class="poster-row"></div>`;
      container.appendChild(sec);
      return { sp, sec, row: sec.querySelector('.poster-row') };
    });

    await Promise.all(
      domSlots.map(async ({ sp, sec, row }, idx) => {
        try {
          const data = await fetchAddonJson(sp.addon.transportUrl, `catalog/${sp.type}/${sp.catId}.json`);
          const metas = (data && data.metas) || [];
          if (!metas.length) return;
          if (idx === 0 && !state.heroItem) {
            setHeroBanner(metas[0], sp.addon.transportUrl, 'مختارات ذكريات الكرتون');
          }
          metas.slice(0, 28).forEach((m) => {
            m.type = m.type || sp.type;
            row.appendChild(createPosterCard(m, sp.addon.transportUrl, sp.badge));
          });
          sec.classList.remove('hidden');
        } catch (e) {}
      })
    );
  }

  function setHeroBanner(item, addonTransport, badgeText) {
    state.heroItem = { item, addonTransport };
    $('hero-title').textContent = item.name || 'دودو ستريم';
    if (item.description) $('hero-desc').textContent = item.description;
    $('hero-type-badge').textContent = badgeText || 'مميز';
    const bg = item.background || item.poster;
    if (bg) {
      $('hero-backdrop').style.backgroundImage = `url("${bg}")`;
    }
  }

  $('hero-play-btn').addEventListener('click', () => {
    if (state.heroItem) {
      quickPlayItemNow(state.heroItem.item, state.heroItem.addonTransport);
    }
  });

  // Continue Watching
  function renderContinueWatching() {
    const list = readJson(KEY_CONTINUE, []);
    const sec = $('continue-section');
    const row = $('continue-row');
    row.innerHTML = '';
    if (!list.length) {
      sec.classList.add('hidden');
      return;
    }
    sec.classList.remove('hidden');
    list.slice(0, 20).forEach((entry) => {
      const pct = entry.duration > 0 ? Math.round((entry.time / entry.duration) * 100) : 15;
      const itemObj = {
        id: entry.metaId,
        type: entry.type,
        name: entry.name,
        poster: entry.poster,
        subtitleText: entry.epTitle || 'متابعة من حيث توقفت',
        resumeEntry: entry,
      };
      row.appendChild(createPosterCard(itemObj, entry.addonTransport, `${pct}%`, pct));
    });
  }

  $('clear-history-btn').addEventListener('click', () => {
    localStorage.removeItem(KEY_CONTINUE);
    renderContinueWatching();
    showToast('تم مسح سجل المتابعة على جهازك');
  });

  // Universal Search
  let searchTimer = null;
  $('global-search').addEventListener('input', (e) => {
    const q = e.target.value.trim();
    $('clear-search').classList.toggle('hidden', !q);
    clearTimeout(searchTimer);
    if (!q) {
      switchView('board');
      return;
    }
    searchTimer = setTimeout(() => runUniversalSearch(q), 380);
  });

  $('clear-search').addEventListener('click', () => {
    $('global-search').value = '';
    $('clear-search').classList.add('hidden');
    switchView('board');
  });

  $('back-from-search').addEventListener('click', () => {
    $('global-search').value = '';
    $('clear-search').classList.add('hidden');
    switchView('board');
  });

  async function runUniversalSearch(query) {
    switchView('search');
    $('search-heading').textContent = `نتائج البحث عن: «${query}»`;
    const status = $('search-status');
    const root = $('search-results-container');
    root.innerHTML = '';

    // Stremio-style URL detection inside Search Bar!
    if (/^(https?:|stremios?:|magnet:|rtmps?:|rtsp:)/i.test(query) || /^[0-9a-f]{40}$/i.test(query) || query.includes('/stremio/v1') || query.includes('manifest.json')) {
      const linkBanner = document.createElement('div');
      linkBanner.className = 'card-panel';
      linkBanner.style.marginBottom = '16px';
      linkBanner.innerHTML = `
        <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;">
          <div>
            <strong>🔗 تم اكتشاف رابط أو صيغة مباشرة!</strong>
            <p class="muted-note" style="margin:4px 0 0;">يمكنك تثبيته كإضافة أو تشغيله فورًا بضغطة واحدة:</p>
          </div>
          <button type="button" class="btn-primary instant-open-search-url">⚡ فتح / تشغيل الرابط الآن</button>
        </div>
      `;
      linkBanner.querySelector('.instant-open-search-url').addEventListener('click', () => {
        unlockVideoOnUserGesture('جارٍ فتح الرابط تلقائيًا...');
        resolveAndOpenAnyInput(query, false).catch((err) => {
          closePlayer();
          showToast(err.message || 'تعذر فتح الرابط');
        });
      });
      root.appendChild(linkBanner);
    }

    status.textContent = 'جارٍ البحث في جميع القوائم والإضافات...';
    status.classList.remove('hidden');

    const tasks = [];
    for (const addon of state.addons) {
      const cats = (addon.manifest && addon.manifest.catalogs) || [];
      for (const cat of cats) {
        const supportsSearch =
          !cat.extra || cat.extra.some((ex) => ex.name === 'search') || addon.id === 'com.linvo.cinemeta';
        if (!supportsSearch) continue;
        // Avoid duplicate search across 6 cartoon channel catalogs; search main cartoon catalogs cleanly
        if (addon.id === 'dodo.cartoonmemories.catalog' && !['mbc3', 'nick', 'cn', 'spacetoon', 'auto_dubbed', 'disney_films', 'cartoony_films'].includes(cat.id)) {
          continue;
        }
        tasks.push({ addon, cat });
      }
    }

    const seenIds = new Set();
    let totalFound = 0;

    await Promise.all(
      tasks.map(async ({ addon, cat }) => {
        try {
          const encQ = encodeURIComponent(query);
          const data = await fetchAddonJson(
            addon.transportUrl,
            `catalog/${cat.type}/${cat.id}/search=${encQ}.json`
          );
          const metas = ((data && data.metas) || []).filter((m) => {
            if (!m || !m.id) return false;
            const key = `${cat.type}:${m.id}`;
            if (seenIds.has(key)) return false;
            seenIds.add(key);
            return true;
          });
          if (!metas.length) return;
          totalFound += metas.length;

          const sec = document.createElement('div');
          sec.className = 'shelf-section';
          sec.innerHTML = `<div class="shelf-head"><h2>${cat.name || addon.name}</h2></div><div class="poster-row"></div>`;
          const row = sec.querySelector('.poster-row');
          metas.slice(0, 30).forEach((m) => {
            m.type = m.type || cat.type;
            row.appendChild(createPosterCard(m, addon.transportUrl));
          });
          root.appendChild(sec);
        } catch (e) {}
      })
    );

    if (totalFound === 0) {
      status.textContent = 'لم يتم العثور على نتائج مطابقة. جرّب كتابة الاسم بالعربية أو الإنجليزية.';
    } else {
      status.classList.add('hidden');
    }
  }

  // Discover View (Dynamically supports all Stremio content types: movie, series, tv, channel, anime, custom)
  function populateDiscoverCatalogs() {
    const typeSelect = $('disc-type');
    const existingTypes = new Set(Array.from(typeSelect.options).map((o) => o.value));
    for (const addon of state.addons) {
      for (const c of (addon.manifest && addon.manifest.catalogs) || []) {
        if (c.type && !existingTypes.has(c.type)) {
          existingTypes.add(c.type);
          const opt = document.createElement('option');
          opt.value = c.type;
          opt.textContent = c.type;
          typeSelect.appendChild(opt);
        }
      }
    }
    const chosenType = typeSelect.value;
    const catSelect = $('disc-catalog');
    catSelect.innerHTML = '';
    for (const addon of state.addons) {
      const cats = (addon.manifest && addon.manifest.catalogs) || [];
      for (const c of cats) {
        if (c.type !== chosenType) continue;
        const reqExtra = Array.isArray(c.extra) && c.extra.some((ex) => ex.isRequired);
        if (reqExtra) continue;
        const opt = document.createElement('option');
        opt.value = JSON.stringify({ transportUrl: addon.transportUrl, catId: c.id, addonId: addon.id });
        opt.textContent = `${c.name || c.id} — (${addon.name})`;
        catSelect.appendChild(opt);
      }
    }
  }

  $('disc-type').addEventListener('change', () => {
    populateDiscoverCatalogs();
    loadDiscoverItems(true);
  });
  $('disc-catalog').addEventListener('change', () => loadDiscoverItems(true));
  $('disc-genre').addEventListener('change', () => loadDiscoverItems(true));
  $('discover-more-btn').addEventListener('click', () => loadDiscoverItems(false));

  async function loadDiscoverItems(reset) {
    const grid = $('discover-grid');
    const moreBtn = $('discover-more-btn');
    if (reset) {
      state.discoverSkip = 0;
      grid.innerHTML = '';
    }
    const rawVal = $('disc-catalog').value;
    if (!rawVal) return;
    const sel = JSON.parse(rawVal);
    const type = $('disc-type').value;
    const genre = $('disc-genre').value;

    const extras = [];
    if (genre && sel.addonId === 'com.linvo.cinemeta') extras.push(`genre=${encodeURIComponent(genre)}`);
    if (state.discoverSkip > 0) extras.push(`skip=${state.discoverSkip}`);
    const extraPath = extras.length ? '/' + extras.join('&') : '';

    try {
      const data = await fetchAddonJson(sel.transportUrl, `catalog/${type}/${sel.catId}${extraPath}.json`);
      const metas = (data && data.metas) || [];
      metas.forEach((m) => {
        m.type = m.type || type;
        grid.appendChild(createPosterCard(m, sel.transportUrl));
      });
      state.discoverSkip += metas.length;
      moreBtn.classList.toggle('hidden', metas.length < 20);
    } catch (e) {
      moreBtn.classList.add('hidden');
    }
  }

  // Detail & Episodes Modal
  async function openItemDetail(item, preferredTransport) {
    const modal = $('detail-modal');
    modal.classList.remove('hidden');

    $('detail-title').textContent = item.name || '';
    $('detail-desc').textContent = item.description || 'جارٍ تحميل تفاصيل العمل...';
    $('detail-poster').src = item.poster || '/static/icons/icon-192.png';
    $('detail-backdrop').style.backgroundImage = item.background || item.poster ? `url("${item.background || item.poster}")` : 'none';
    $('detail-chips').innerHTML = '';
    $('episodes-panel').classList.add('hidden');
    $('streams-list').innerHTML = '';
    $('streams-spinner').classList.remove('hidden');
    $('streams-spinner').textContent = 'جارٍ جلب التفاصيل...';

    const type = item.type || 'series';
    const id = item.id;

    // Determine best metadata addon
    let metaTransport = preferredTransport;
    if (id.startsWith('tt') && (!metaTransport || metaTransport.includes('/addons/cartoons/'))) {
      const cm = state.addons.find((a) => a.id === 'com.linvo.cinemeta');
      if (cm && !(type === 'movie' && id in {})) metaTransport = cm.transportUrl;
    }

    let meta = Object.assign({}, item);
    try {
      const res = await fetchAddonJson(metaTransport, `meta/${type}/${encodeURIComponent(id)}.json`);
      if (res && res.meta) {
        meta = Object.assign({}, item, res.meta);
      } else if (id.startsWith('tt')) {
        const cm = state.addons.find((a) => a.id === 'com.linvo.cinemeta');
        if (cm) {
          const r2 = await fetchAddonJson(cm.transportUrl, `meta/${type}/${encodeURIComponent(id)}.json`);
          if (r2 && r2.meta) meta = Object.assign({}, item, r2.meta);
        }
      }
    } catch (e) {}

    state.currentMeta = Object.assign({}, meta, { type, addonTransport: preferredTransport || metaTransport });
    updateFavoriteButton();

    $('detail-title').textContent = meta.name || item.name || '';
    $('detail-desc').textContent = meta.description || 'لا يوجد وصف إضافي متاح.';
    if (meta.poster) $('detail-poster').src = meta.poster;
    if (meta.background || meta.poster) {
      $('detail-backdrop').style.backgroundImage = `url("${meta.background || meta.poster}")`;
    }

    const chips = [];
    if (meta.year || meta.releaseInfo) chips.push(`📅 ${meta.releaseInfo || meta.year}`);
    if (meta.imdbRating) chips.push(`⭐ IMDb ${meta.imdbRating}`);
    if (meta.runtime) chips.push(`⏱ ${meta.runtime}`);
    if (Array.isArray(meta.genres)) chips.push(...meta.genres.slice(0, 3));
    $('detail-chips').innerHTML = chips.map((c) => `<span class="badge">${c}</span>`).join('');

    if (type === 'series' && Array.isArray(meta.videos) && meta.videos.length) {
      setupSeriesEpisodes(meta.videos, item.resumeEntry);
    } else {
      state.currentEpisodes = [];
      state.currentEpisodeObj = null;
      $('streams-heading').textContent = '🎬 سيرفرات المشاهدة المتاحة';
      await loadStreamsForTarget(type, id, meta.name);
    }
  }

  $('close-detail-btn').addEventListener('click', () => {
    $('detail-modal').classList.add('hidden');
  });

  function setupSeriesEpisodes(videos, resumeEntry) {
    const epPanel = $('episodes-panel');
    epPanel.classList.remove('hidden');
    state.currentEpisodes = videos.slice().sort((a, b) => {
      const sa = a.season ?? 1;
      const sb = b.season ?? 1;
      if (sa !== sb) return sa - sb;
      return (a.episode ?? 0) - (b.episode ?? 0);
    });

    const seasons = [...new Set(state.currentEpisodes.map((v) => v.season ?? 1))].filter((s) => s > 0 || state.currentEpisodes.length < 50);
    if (!seasons.length) seasons.push(1);

    const sSelect = $('season-select');
    sSelect.innerHTML = seasons
      .map((s) => `<option value="${s}">الموسم ${s}</option>`)
      .join('');

    // Pick initial episode (resume entry or first episode)
    let startEp = state.currentEpisodes[0];
    if (resumeEntry && resumeEntry.streamTargetId) {
      const found = state.currentEpisodes.find((v) => v.id === resumeEntry.streamTargetId);
      if (found) startEp = found;
    }
    if (startEp && startEp.season != null) {
      sSelect.value = String(startEp.season);
    }

    const renderSeasonList = () => {
      const chosenSeason = Number(sSelect.value);
      const q = $('ep-filter-input').value.trim().toLowerCase();
      const watchedMap = readJson(KEY_WATCHED, {});
      const listEl = $('episodes-list');
      listEl.innerHTML = '';

      const filtered = state.currentEpisodes.filter((v) => {
        const sMatch = seasons.length <= 1 || (v.season ?? 1) === chosenSeason;
        if (!sMatch) return false;
        if (!q) return true;
        const txt = `${v.episode || ''} ${v.title || v.name || ''}`.toLowerCase();
        return txt.includes(q);
      });

      filtered.forEach((ep) => {
        const row = document.createElement('div');
        const isAct = state.currentEpisodeObj && state.currentEpisodeObj.id === ep.id;
        row.className = 'ep-item' + (isAct ? ' active' : '');
        const watched = watchedMap[ep.id] ? ' ✓' : '';
        const epNum = ep.episode != null ? `حلقة ${ep.episode}` : 'حلقة';
        const epTitle = ep.title || ep.name || epNum;
        row.innerHTML = `
          <span class="ep-num">${epNum}${watched}</span>
          <span class="ep-title">${epTitle}</span>
          <span class="mini-badge">▶ تشغيل</span>
        `;
        row.addEventListener('click', () => {
          unlockVideoOnUserGesture(`جارٍ تشغيل ${epTitle}...`);
          selectEpisode(ep, 0, renderSeasonList, true);
        });
        listEl.appendChild(row);
      });
    };

    sSelect.onchange = renderSeasonList;
    $('ep-filter-input').oninput = renderSeasonList;

    if (startEp) {
      selectEpisode(startEp, resumeEntry ? resumeEntry.time || 0 : 0, renderSeasonList, false);
    } else {
      renderSeasonList();
    }
  }

  if ($('detail-autoplay-btn')) {
    $('detail-autoplay-btn').addEventListener('click', async () => {
      if (!state.currentMeta) return;
      unlockVideoOnUserGesture(`جارٍ تشغيل ${state.currentMeta.name || ''} تلقائيًا...`);
      if (state.currentMeta.type === 'series' && state.currentEpisodeObj) {
        const ep = state.currentEpisodeObj;
        const epLabel = ep.title || ep.name || `الحلقة ${ep.episode || ''}`;
        const streams = await loadStreamsForTarget('series', ep.id, `${state.currentMeta.name} — ${epLabel}`, true);
        if (!streams || !streams.length) closePlayer();
      } else {
        const streams = await loadStreamsForTarget(
          state.currentMeta.type || 'movie',
          state.currentMeta.id,
          state.currentMeta.name || '',
          true
        );
        if (!streams || !streams.length) closePlayer();
      }
    });
  }

  async function quickPlayItemNow(item, preferredTransport) {
    unlockVideoOnUserGesture(`جارٍ تشغيل «${item.name || 'الفيديو'}» تلقائيًا...`);
    try {
      const type = item.type || 'series';
      const id = item.id;
      let metaTransport = preferredTransport;
      if (id && id.startsWith('tt') && (!metaTransport || metaTransport.includes('/addons/cartoons/'))) {
        const cm = state.addons.find((a) => a.id === 'com.linvo.cinemeta');
        if (cm) metaTransport = cm.transportUrl;
      }
      let meta = Object.assign({}, item);
      if (metaTransport) {
        try {
          const res = await fetchAddonJson(metaTransport, `meta/${type}/${encodeURIComponent(id)}.json`);
          if (res && res.meta) meta = Object.assign({}, item, res.meta);
        } catch (e) {}
      }
      state.currentMeta = Object.assign({}, meta, { type, addonTransport: preferredTransport || metaTransport });
      if (type === 'series' && Array.isArray(meta.videos) && meta.videos.length) {
        const sorted = meta.videos.slice().sort((a, b) => ((a.season ?? 1) - (b.season ?? 1)) || ((a.episode ?? 0) - (b.episode ?? 0)));
        state.currentEpisodes = sorted;
        let targetEp = sorted[0];
        let resumeSec = 0;
        if (item.resumeEntry && item.resumeEntry.streamTargetId) {
          const found = sorted.find((v) => v.id === item.resumeEntry.streamTargetId);
          if (found) {
            targetEp = found;
            resumeSec = item.resumeEntry.time || 0;
          }
        }
        state.currentEpisodeObj = Object.assign({}, targetEp, { resumeTime: resumeSec });
        const epLabel = targetEp.title || targetEp.name || `الحلقة ${targetEp.episode || ''}`;
        const streams = await loadStreamsForTarget('series', targetEp.id, `${meta.name} — ${epLabel}`, true);
        if (!streams || !streams.length) {
          closePlayer();
          openItemDetail(item, preferredTransport);
        }
      } else {
        state.currentEpisodes = [];
        state.currentEpisodeObj = item.resumeEntry ? { id, resumeTime: item.resumeEntry.time || 0 } : null;
        const streams = await loadStreamsForTarget(type, id, meta.name || item.name || '', true);
        if (!streams || !streams.length) {
          closePlayer();
          openItemDetail(item, preferredTransport);
        }
      }
    } catch (e) {
      closePlayer();
      openItemDetail(item, preferredTransport);
    }
  }

  async function selectEpisode(ep, resumeTime = 0, refreshListCb = null, autoPlay = false) {
    state.currentEpisodeObj = Object.assign({}, ep, { resumeTime });
    if (refreshListCb) refreshListCb();
    else {
      document.querySelectorAll('.ep-item').forEach((el) => el.classList.remove('active'));
    }
    const epLabel = ep.title || ep.name || `الحلقة ${ep.episode || ''}`;
    $('streams-heading').textContent = `🎬 سيرفرات: ${epLabel}`;
    const streams = await loadStreamsForTarget('series', ep.id, `${state.currentMeta.name} — ${epLabel}`, autoPlay);
    if (autoPlay && (!streams || !streams.length)) {
      closePlayer();
      showToast('لم يتوفر سيرفر مباشر لهذه الحلقة بعد');
    }
  }

  // Full Stremio Addon SDK v3 Resource Matcher (supports string & object resource declarations)
  function addonSupportsResource(addon, resName, type, targetId) {
    const mf = addon && addon.manifest;
    if (!mf) return false;
    const resources = mf.resources || [];
    for (const r of resources) {
      if (typeof r === 'string') {
        if (r !== resName) continue;
        const types = mf.types || [];
        if (types.length && type && !types.includes(type)) continue;
        const prefixes = mf.idPrefixes;
        if (Array.isArray(prefixes) && prefixes.length && targetId) {
          if (!prefixes.some((p) => targetId.startsWith(p))) continue;
        }
        return true;
      } else if (r && typeof r === 'object' && r.name === resName) {
        const types = r.types || mf.types || [];
        if (types.length && type && !types.includes(type)) continue;
        const prefixes = r.idPrefixes || mf.idPrefixes;
        if (Array.isArray(prefixes) && prefixes.length && targetId) {
          if (!prefixes.some((p) => targetId.startsWith(p))) continue;
        }
        return true;
      }
    }
    return false;
  }

  // Query All Stream Addons in Parallel
  async function loadStreamsForTarget(type, targetId, displayTitle, autoPlay = false) {
    const listEl = $('streams-list');
    const spin = $('streams-spinner');
    if (listEl) listEl.innerHTML = '';
    if (spin) {
      spin.classList.remove('hidden');
      spin.textContent = 'جارٍ البحث عن أفضل السيرفرات...';
    }

    const streamAddons = state.addons.filter((a) => addonSupportsResource(a, 'stream', type, targetId));

    const collected = [];
    await Promise.all(
      streamAddons.map(async (addon) => {
        try {
          const data = await fetchAddonJson(
            addon.transportUrl,
            `stream/${type}/${encodeURIComponent(targetId)}.json`
          );
          const arr = (data && data.streams) || [];
          arr.forEach((s) => {
            if (s && (s.url || s.ytId || s.infoHash || s.externalUrl)) {
              collected.push(Object.assign({}, s, { _addonName: addon.name }));
            }
          });
        } catch (e) {}
      })
    );

    if (spin) spin.classList.add('hidden');

    if (!collected.length) {
      if (listEl) {
        listEl.innerHTML = `
          <div class="card-panel">
            <p class="muted-note" style="margin:0;">
              لم تُرجع الإضافات الحالية رابط بث مباشر لهذا العمل بعد.
              يمكنك إضافة أي إضافة أو رابط بث من زر <strong>«🔗 أي رابط / ملف»</strong> أو تبويب <strong>«🧩 الإضافات»</strong>.
            </p>
          </div>
        `;
      }
      state.currentStreamsPool = [];
      return [];
    }

    // Prioritize direct browser-compatible HLS / MP4 / Universal-HLS streams first across ALL devices!
    const scoreStream = (s) => {
      if (s.url && (s.url.includes('.m3u8') || s.url.includes('/universal-hls/') || s.url.includes('/cartoony-hls/'))) return 4;
      if (s.behaviorHints && s.behaviorHints.iosReady && s.url) return 3;
      if (s.ytId) return 2;
      if (s.url) return 1;
      return 0;
    };
    collected.sort((a, b) => scoreStream(b) - scoreStream(a));
    state.currentStreamsPool = collected;
    state.currentPlayContext = { type, targetId, displayTitle };

    if (listEl) {
      collected.forEach((st, idx) => {
        const iosReady = Boolean(st.behaviorHints && st.behaviorHints.iosReady);
        const isTorrent = Boolean((st.behaviorHints && st.behaviorHints.isTorrent) || st.infoHash);
        const row = document.createElement('div');
        row.className = 'stream-item' + (iosReady ? ' ios-best' : '');

        const streamName = st.name || st._addonName || `سيرفر #${idx + 1}`;
        const streamDesc = st.title || st.description || '';
        const compatTag = iosReady
          ? '📱 يعمل تلقائيًا على كل الأجهزة'
          : isTorrent
          ? '🧲 تورنت'
          : '🎬 رابط خارجي';

        row.innerHTML = `
          <div class="stream-info">
            <strong>${streamName}</strong>
            <small>${streamDesc}</small>
          </div>
          <span class="mini-badge">${compatTag}</span>
        `;
        row.addEventListener('click', () => {
          unlockVideoOnUserGesture(`جارٍ تشغيل ${streamName}...`);
          launchPlayer(st, type, targetId, displayTitle, idx);
        });
        listEl.appendChild(row);
      });
    }

    if (autoPlay && collected.length) {
      launchPlayer(collected[0], type, targetId, displayTitle, 0);
    }
    return collected;
  }

  // Smart Universal Cross-Device Video Player (iPhone Safari + Android Chrome + Desktop + TV)
  const SILENT_WAV_URI = 'data:audio/wav;base64,UklGRigAAABXQVZFZm10IBIAAAABAAEARKwAAIhYAQACABAAAABkYXRhAgAAAAEA';

  function setPlayerLoading(visible, msg) {
    const ov = $('player-loading-overlay');
    const txt = $('player-loading-text');
    if (!ov) return;
    if (msg && txt) txt.textContent = msg;
    ov.classList.toggle('hidden', !visible);
  }

  // Synchronously unlocks HTMLMediaElement inside user click event so iOS Safari & Android Chrome allow unmuted autoplay after async fetch!
  function unlockVideoOnUserGesture(loadingMsg) {
    const modal = $('player-modal');
    const video = $('dodo-video');
    if (modal) modal.classList.remove('hidden');
    setPlayerLoading(true, loadingMsg || 'جارٍ التشغيل التلقائي...');
    if (video) {
      try {
        video.playsInline = true;
        video.autoplay = true;
        if (!video.src) {
          video.src = SILENT_WAV_URI;
        }
        const p = video.play();
        if (p && typeof p.catch === 'function') p.catch(() => {});
      } catch (e) {}
    }
  }

  function destroyHls() {
    if (state.hlsInstance) {
      try {
        state.hlsInstance.destroy();
      } catch (e) {}
      state.hlsInstance = null;
    }
  }

  function buildAbsoluteUrl(u) {
    if (!u) return '';
    if (/^(https?:|magnet:|blob:|data:)/i.test(u)) return u;
    return window.location.origin + (u.startsWith('/') ? u : '/' + u);
  }

  function updateExternalPlayerLinks(streamObj) {
    const rawDirect = buildAbsoluteUrl(streamObj.externalUrl || streamObj.url || '');
    const playDirect = buildAbsoluteUrl(streamObj.url || '');
    const target = rawDirect || playDirect;

    $('ext-outplayer').href = target ? `outplayer://${target.replace(/^https?:\/\//i, '')}` : '#';
    $('ext-vlc-ios').href = target
      ? `vlc-x-callback://x-callback-url/stream?url=${encodeURIComponent(target)}`
      : '#';

    const cleanNoScheme = target.replace(/^https?:\/\//i, '');
    const scheme = target.startsWith('https://') ? 'https' : 'http';
    $('ext-android').href = target
      ? `intent://${cleanNoScheme}#Intent;scheme=${scheme};type=video/*;end`
      : '#';

    $('copy-stream-btn').onclick = () => {
      navigator.clipboard.writeText(target).then(() => showToast('تم نسخ رابط البث المباشر!'));
    };
  }

  function populateInPlayerServerSelect(activeIdx) {
    const sel = $('player-server-select');
    if (!sel) return;
    const pool = state.currentStreamsPool || [];
    if (!pool.length) {
      sel.innerHTML = '<option value="0">سيرفر مباشر</option>';
      return;
    }
    sel.innerHTML = pool
      .map((s, idx) => {
        const lbl = (s.name || s._addonName || `سيرفر #${idx + 1}`).replace(/</g, '&lt;');
        return `<option value="${idx}" ${idx === activeIdx ? 'selected' : ''}>${lbl}</option>`;
      })
      .join('');
    sel.onchange = () => {
      const nextIdx = Number(sel.value) || 0;
      const chosen = pool[nextIdx];
      const ctx = state.currentPlayContext || {};
      if (chosen) {
        unlockVideoOnUserGesture(`جارٍ التبديل إلى ${chosen.name || 'السيرفر'}...`);
        launchPlayer(chosen, ctx.type || 'movie', ctx.targetId || '', ctx.displayTitle || '', nextIdx);
      }
    };
  }

  async function startGuaranteedPlayback(video, startAt) {
    const unmuteBtn = $('unmute-floating-btn');
    if (unmuteBtn) unmuteBtn.classList.add('hidden');

    if (startAt > 5 && (!video.duration || startAt < video.duration - 15)) {
      try {
        video.currentTime = startAt;
      } catch (e) {}
    }

    try {
      video.muted = false;
      await video.play();
      setPlayerLoading(false);
    } catch (err) {
      // Strict browser blocked unmuted autoplay -> start muted immediately so video ALWAYS moves, with 1-tap unmute!
      try {
        video.muted = true;
        await video.play();
        setPlayerLoading(false);
        if (unmuteBtn) {
          unmuteBtn.classList.remove('hidden');
          unmuteBtn.onclick = () => {
            video.muted = false;
            video.volume = 1.0;
            unmuteBtn.classList.add('hidden');
          };
        }
      } catch (err2) {
        setPlayerLoading(false);
      }
    }
  }

  async function launchPlayer(streamObj, type, targetId, displayTitle, poolIndex = 0, fallbackStage = 0) {
    // If stream is purely an external web link (like WatchHub), try next automatic stream first if available!
    if (!streamObj.url && !streamObj.ytId && !streamObj.infoHash && streamObj.externalUrl) {
      const pool = state.currentStreamsPool || [];
      if (poolIndex + 1 < pool.length) {
        return launchPlayer(pool[poolIndex + 1], type, targetId, displayTitle, poolIndex + 1, 0);
      }
      setPlayerLoading(false);
      closePlayer();
      window.open(streamObj.externalUrl, '_blank', 'noopener,noreferrer');
      return;
    }

    const video = $('dodo-video');
    const ytFrame = $('dodo-yt-frame');
    const alertBox = $('player-alert');
    const unmuteBtn = $('unmute-floating-btn');
    alertBox.classList.add('hidden');
    if (unmuteBtn) unmuteBtn.classList.add('hidden');
    destroyHls();
    video.onerror = null;
    video.pause();
    video.innerHTML = '';
    video.classList.remove('hidden');
    if (ytFrame) {
      ytFrame.src = 'about:blank';
      ytFrame.classList.add('hidden');
    }

    state.currentStream = streamObj;
    state.currentPlayContext = { type, targetId, displayTitle };
    populateInPlayerServerSelect(poolIndex);

    $('player-main-title').textContent = state.currentMeta ? state.currentMeta.name : displayTitle;
    $('player-sub-title').textContent = state.currentEpisodeObj && state.currentEpisodeObj.title
      ? state.currentEpisodeObj.title
      : streamObj.name || '';

    // Next Episode button visibility
    const nextBtn = $('next-ep-btn');
    const nextEp = getNextEpisode();
    nextBtn.classList.toggle('hidden', !nextEp);
    nextBtn.onclick = () => {
      if (nextEp) {
        unlockVideoOnUserGesture('جارٍ تشغيل الحلقة التالية...');
        selectEpisode(nextEp, 0, null, true);
      }
    };

    updateExternalPlayerLinks(streamObj);
    $('player-modal').classList.remove('hidden');

    // Support YouTube (ytId) streams from any Stremio addon or direct link
    if (streamObj.ytId && ytFrame) {
      setPlayerLoading(false);
      video.removeAttribute('src');
      video.classList.add('hidden');
      ytFrame.classList.remove('hidden');
      ytFrame.src = `https://www.youtube.com/embed/${encodeURIComponent(streamObj.ytId)}?autoplay=1&playsinline=1&rel=0`;
      return;
    }

    // Choose URL according to automatic recovery stage:
    // Stage 0: primary url -> Stage 1: proxyUrl (CORS bypass) -> Stage 2: universalHlsUrl (FFmpeg H.264/AAC HLS) -> Stage 3: next server!
    let chosenRawUrl = streamObj.url || '';
    if (fallbackStage === 1 && streamObj.proxyUrl) {
      chosenRawUrl = streamObj.proxyUrl;
    } else if (fallbackStage === 2 && streamObj.universalHlsUrl) {
      chosenRawUrl = streamObj.universalHlsUrl;
    }
    const streamUrl = buildAbsoluteUrl(chosenRawUrl);

    if (!streamUrl && streamObj.infoHash) {
      const pool = state.currentStreamsPool || [];
      if (poolIndex + 1 < pool.length) {
        return launchPlayer(pool[poolIndex + 1], type, targetId, displayTitle, poolIndex + 1, 0);
      }
      setPlayerLoading(false);
      const mag = `magnet:?xt=urn:btih:${streamObj.infoHash}`;
      updateExternalPlayerLinks({ url: mag });
      alertBox.innerHTML = isIOS
        ? 'هذا الرابط تورنت خام والمحرك المحلي غير متصل حاليًا؛ شغّل التطبيق عبر لوحة التحكم على الكمبيوتر ليتم تحويله تلقائيًا للآيفون.'
        : 'هذا رابط تورنت خام؛ اضغط <strong>🤖 مشغل أندرويد</strong> بالأسفل أو شغّل المحرك المحلي للتحويل التلقائي.';
      alertBox.classList.remove('hidden');
      return;
    }

    setPlayerLoading(
      true,
      fallbackStage === 2
        ? '⚡ جارٍ التحويل التلقائي الفوري لصيغة متوافقة مع جهازك...'
        : '⚡ جارٍ بدء البث التلقائي...'
    );

    let failedAlready = false;
    const triggerAutomaticRecovery = () => {
      if (failedAlready) return;
      failedAlready = true;
      if (fallbackStage < 1 && streamObj.proxyUrl && streamObj.proxyUrl !== chosenRawUrl) {
        return launchPlayer(streamObj, type, targetId, displayTitle, poolIndex, 1);
      }
      if (fallbackStage < 2 && streamObj.universalHlsUrl && streamObj.universalHlsUrl !== chosenRawUrl) {
        return launchPlayer(streamObj, type, targetId, displayTitle, poolIndex, 2);
      }
      const pool = state.currentStreamsPool || [];
      if (poolIndex + 1 < pool.length) {
        showToast('جارٍ التبديل تلقائيًا للسيرفر البديل...');
        return launchPlayer(pool[poolIndex + 1], type, targetId, displayTitle, poolIndex + 1, 0);
      }
      setPlayerLoading(false);
      alertBox.innerHTML =
        'تعذر تشغيل هذا المصدر مباشرة داخل المتصفح. يمكنك الضغط على <strong>Outplayer</strong> أو <strong>VLC</strong> أو <strong>مشغل أندرويد</strong> بالأسفل لفتحه فورًا.';
      alertBox.classList.remove('hidden');
    };

    const isM3u8 = streamUrl.includes('.m3u8');
    if (targetId) {
      loadPlayerSubtitles(type, targetId, video);
    } else {
      $('subtitle-select').innerHTML = '<option value="">بدون ترجمة</option>';
    }

    const startAt = (state.currentEpisodeObj && state.currentEpisodeObj.resumeTime) || 0;
    video.addEventListener('playing', () => setPlayerLoading(false), { once: true });
    video.addEventListener('canplay', () => setPlayerLoading(false), { once: true });

    // Playback Strategy across ALL devices:
    // 1. iPhone / iPad / Mac Safari plays HLS (.m3u8) NATIVELY via hardware decoder!
    // 2. Android / Windows / Linux / Smart TV plays HLS via bundled hls.min.js!
    if (isM3u8) {
      if (isIOS || (!window.Hls || !window.Hls.isSupported()) && video.canPlayType('application/vnd.apple.mpegurl')) {
        video.src = streamUrl;
        video.load();
        video.addEventListener(
          'loadedmetadata',
          () => startGuaranteedPlayback(video, startAt),
          { once: true }
        );
        startGuaranteedPlayback(video, startAt);
      } else if (window.Hls && window.Hls.isSupported()) {
        const hls = new window.Hls({
          maxBufferLength: 30,
          enableWorker: true,
          manifestLoadingTimeOut: 15000,
        });
        state.hlsInstance = hls;
        hls.loadSource(streamUrl);
        hls.attachMedia(video);
        hls.on(window.Hls.Events.MANIFEST_PARSED, () => {
          startGuaranteedPlayback(video, startAt);
        });
        hls.on(window.Hls.Events.ERROR, (_ev, data) => {
          if (data && data.fatal) {
            triggerAutomaticRecovery();
          }
        });
      } else {
        video.src = streamUrl;
        startGuaranteedPlayback(video, startAt);
      }
    } else {
      video.src = streamUrl;
      video.load();
      video.addEventListener(
        'loadedmetadata',
        () => startGuaranteedPlayback(video, startAt),
        { once: true }
      );
      startGuaranteedPlayback(video, startAt);
    }

    video.onerror = () => {
      triggerAutomaticRecovery();
    };
  }

  function getNextEpisode() {
    if (!state.currentEpisodeObj || !state.currentEpisodes.length) return null;
    const idx = state.currentEpisodes.findIndex((e) => e.id === state.currentEpisodeObj.id);
    return idx >= 0 && idx + 1 < state.currentEpisodes.length ? state.currentEpisodes[idx + 1] : null;
  }

  // Native WebVTT Subtitles Loader (Works in iPhone Fullscreen + Supports Embedded Stream Subtitles!)
  async function loadPlayerSubtitles(type, targetId, video) {
    const subSelect = $('subtitle-select');
    subSelect.innerHTML = '<option value="">بدون ترجمة (أو صوت مدبلج)</option>';

    const allSubs = [];
    if (state.currentStream && Array.isArray(state.currentStream.subtitles)) {
      state.currentStream.subtitles.forEach((s) => {
        if (s && s.url) allSubs.push(s);
      });
    }

    if (!targetId.startsWith('cartoonymem:') && !targetId.startsWith('cartoonymovie:')) {
      const subAddons = state.addons.filter((a) => addonSupportsResource(a, 'subtitles', type, targetId));
      await Promise.all(
        subAddons.map(async (addon) => {
          try {
            const data = await fetchAddonJson(
              addon.transportUrl,
              `subtitles/${type}/${encodeURIComponent(targetId)}.json`
            );
            ((data && data.subtitles) || []).forEach((s) => {
              if (s && s.url) allSubs.push(s);
            });
          } catch (e) {}
        })
      );
    }

    if (!allSubs.length) return;

    // Attach <track> elements so iPhone Safari native fullscreen displays them!
    allSubs.slice(0, 10).forEach((s, idx) => {
      const track = document.createElement('track');
      track.kind = 'subtitles';
      track.srclang = s.lang || 'ar';
      track.label = s.label || `العربية #${idx + 1}`;
      track.src = s.url;
      if (idx === 0) track.default = true;
      video.appendChild(track);

      const opt = document.createElement('option');
      opt.value = String(idx);
      opt.textContent = track.label;
      if (idx === 0) opt.selected = true;
      subSelect.appendChild(opt);
    });

    const syncTracks = () => {
      const chosen = subSelect.value;
      const tracks = video.textTracks;
      for (let i = 0; i < tracks.length; i++) {
        tracks[i].mode = chosen !== '' && Number(chosen) === i ? 'showing' : 'disabled';
      }
    };
    subSelect.onchange = syncTracks;
    setTimeout(syncTracks, 500);
  }

  // Save Playback Progress & Watched Status
  const videoEl = $('dodo-video');
  let lastSavedSec = 0;
  videoEl.addEventListener('timeupdate', () => {
    const now = Math.floor(videoEl.currentTime || 0);
    if (now < 5 || Math.abs(now - lastSavedSec) < 5 || !state.currentMeta) return;
    lastSavedSec = now;

    const dur = Math.floor(videoEl.duration || 0);
    const meta = state.currentMeta;
    const ep = state.currentEpisodeObj;
    const targetId = ep ? ep.id : meta.id;

    if (ep && dur > 60 && now / dur > 0.85) {
      const w = readJson(KEY_WATCHED, {});
      w[ep.id] = true;
      writeJson(KEY_WATCHED, w);
    }

    const list = readJson(KEY_CONTINUE, []).filter((x) => x.metaId !== meta.id);
    list.unshift({
      metaId: meta.id,
      type: meta.type,
      name: meta.name,
      poster: meta.poster,
      addonTransport: meta.addonTransport,
      streamTargetId: targetId,
      epTitle: ep ? ep.title || `الحلقة ${ep.episode || ''}` : 'فيلم',
      time: now,
      duration: dur,
      updatedAt: Date.now(),
    });
    writeJson(KEY_CONTINUE, list.slice(0, 30));
  });

  $('pip-btn').addEventListener('click', async () => {
    try {
      if (videoEl.webkitSupportsPresentationMode) {
        videoEl.webkitSetPresentationMode(
          videoEl.webkitPresentationMode === 'picture-in-picture' ? 'inline' : 'picture-in-picture'
        );
      } else if (document.pictureInPictureElement) {
        await document.exitPictureInPicture();
      } else if (videoEl.requestPictureInPicture) {
        await videoEl.requestPictureInPicture();
      }
    } catch (e) {
      showToast('النافذة العائمة متاحة أثناء تشغيل الفيديو');
    }
  });

  // Automatically advance to Next Episode when current episode finishes!
  videoEl.addEventListener('ended', () => {
    const nextEp = getNextEpisode();
    if (nextEp) {
      showToast('⏭ جارٍ تشغيل الحلقة التالية تلقائيًا...');
      setPlayerLoading(true, '⏭ جارٍ تشغيل الحلقة التالية تلقائيًا...');
      selectEpisode(nextEp, 0, null, true);
    }
  });

  function closePlayer() {
    setPlayerLoading(false);
    destroyHls();
    videoEl.onerror = null;
    videoEl.pause();
    videoEl.removeAttribute('src');
    const unmuteBtn = $('unmute-floating-btn');
    if (unmuteBtn) unmuteBtn.classList.add('hidden');
    const ytFrame = $('dodo-yt-frame');
    if (ytFrame) {
      ytFrame.src = 'about:blank';
      ytFrame.classList.add('hidden');
    }
    $('player-modal').classList.add('hidden');
    renderContinueWatching();
  }

  $('close-player-btn').addEventListener('click', closePlayer);

  // Favorites / Library
  function updateFavoriteButton() {
    if (!state.currentMeta) return;
    const lib = readJson(KEY_LIBRARY, []);
    const exists = lib.some((x) => x.id === state.currentMeta.id);
    $('fav-toggle-btn').textContent = exists ? '❤️ محفوظ في مكتبتي' : '🤍 أضف لمكتبتي';
  }

  $('fav-toggle-btn').addEventListener('click', () => {
    if (!state.currentMeta) return;
    let lib = readJson(KEY_LIBRARY, []);
    const exists = lib.some((x) => x.id === state.currentMeta.id);
    if (exists) {
      lib = lib.filter((x) => x.id !== state.currentMeta.id);
      showToast('أُزيل من مكتبتك');
    } else {
      lib.unshift({
        id: state.currentMeta.id,
        type: state.currentMeta.type,
        name: state.currentMeta.name,
        poster: state.currentMeta.poster,
        addonTransport: state.currentMeta.addonTransport,
      });
      showToast('أُضيف إلى مكتبتك ❤️');
    }
    writeJson(KEY_LIBRARY, lib);
    updateFavoriteButton();
  });

  function renderLibrary() {
    const grid = $('library-grid');
    grid.innerHTML = '';
    const lib = readJson(KEY_LIBRARY, []);
    if (!lib.length) {
      grid.innerHTML = '<p class="muted-note">مكتبتك فارغة حاليًا. افتح أي مسلسل أو فيلم واضغط «🤍 أضف لمكتبتي».</p>';
      return;
    }
    lib.forEach((item) => {
      grid.appendChild(createPosterCard(item, item.addonTransport));
    });
  }

  $('export-lib-btn').addEventListener('click', () => {
    const backup = {
      app: 'dodo_stream',
      exportedAt: new Date().toISOString(),
      library: readJson(KEY_LIBRARY, []),
      continueWatching: readJson(KEY_CONTINUE, []),
      watched: readJson(KEY_WATCHED, {}),
      userAddons: readJson(KEY_USER_ADDONS, []),
    };
    const blob = new Blob([JSON.stringify(backup, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'dodo-stream-backup.json';
    a.click();
  });

  $('import-lib-input').addEventListener('change', (e) => {
    const file = e.target.files && e.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      try {
        const data = JSON.parse(reader.result);
        if (Array.isArray(data.library)) writeJson(KEY_LIBRARY, data.library);
        if (Array.isArray(data.continueWatching)) writeJson(KEY_CONTINUE, data.continueWatching);
        if (data.watched) writeJson(KEY_WATCHED, data.watched);
        if (Array.isArray(data.userAddons)) writeJson(KEY_USER_ADDONS, data.userAddons);
        renderLibrary();
        renderContinueWatching();
        showToast('تم استيراد بياناتك بنجاح!');
      } catch (err) {
        showToast('ملف النسخة غير صالح');
      }
    };
    reader.readAsText(file);
  });

  // Addons Manager + Built-in Stremio Community Addon Store
  let communityStoreCache = null;

  async function saveResolvedAddon(url, mf, makeGlobal = false) {
    const entry = { id: mf.id, name: mf.name, description: mf.description || '', transportUrl: url };
    if (makeGlobal) {
      let pin = state.adminPin || window.prompt('أدخل رمز المالك لتعميم الإضافة على جميع الأصدقاء :') || '';
      if (!pin.trim()) throw new Error('تم إلغاء إدخال رمز المالك');
      const inspectRes = await apiFetch('/api/admin/inspect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ admin_pin: pin.trim() }),
      });
      const inspectData = await inspectRes.json();
      if (!inspectRes.ok || !inspectData.ok) throw new Error('رمز المالك غير صحيح');
      state.adminPin = pin.trim();
      const globals = (inspectData.global_extra_addons || []).filter((x) => x.transportUrl !== url);
      entry.globalShared = true;
      globals.push(entry);
      await apiFetch('/api/admin/update', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ admin_pin: state.adminPin, global_extra_addons: globals }),
      });
    } else {
      const saved = readJson(KEY_USER_ADDONS, []).filter((x) => x.transportUrl !== url);
      saved.push(entry);
      writeJson(KEY_USER_ADDONS, saved);
    }

    state.addons = state.addons.filter((x) => x.transportUrl !== url);
    state.addons.push(Object.assign({}, entry, { manifest: mf }));
    renderAddonsView();
    populateDiscoverCatalogs();
    loadBoardShelves();
    return mf;
  }

  // Accepts ANY link or format: Stremio v3, Legacy /stremio/v1, M3U IPTV, JSON/RSS, or Direct Media/Magnet/YouTube!
  async function resolveAndOpenAnyInput(rawUrl, makeGlobal = false) {
    const res = await apiFetch('/api/resolve-link', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: rawUrl }),
    });
    const data = await res.json();
    if (!res.ok || !data || !data.ok) {
      throw new Error((data && data.error) || 'تعذر قراءة الرابط أو الصيغة');
    }

    if (data.kind === 'addon') {
      closePlayer();
      const mf = await saveResolvedAddon(data.transportUrl, data.manifest, makeGlobal);
      showToast(`✅ تم تثبيت «${mf.name}» بنجاح!`);
      return { kind: 'addon', manifest: mf };
    }

    // Direct playable media / HLS / MKV / MP4 / Magnet / Torrent / YouTube!
    const streams = data.streams || [];
    if (!streams.length) throw new Error('لم يتم العثور على مسار تشغيل داخل هذا الرابط');
    state.currentMeta = null;
    state.currentEpisodes = [];
    state.currentEpisodeObj = null;
    state.currentStreamsPool = streams;
    const title = data.title || 'بث مباشر';
    await launchPlayer(streams[0], 'movie', '', title, 0);
    return { kind: 'media', title };
  }

  async function installAddonByUrl(rawUrl, makeGlobal = false) {
    const res = await resolveAndOpenAnyInput(rawUrl, makeGlobal);
    return res.manifest || { name: res.title || 'تم الفتح' };
  }

  function renderAddonsView() {
    const listEl = $('addons-list');
    listEl.innerHTML = '';
    state.addons.forEach((addon) => {
      const card = document.createElement('div');
      card.className = 'addon-card';
      const isBuiltIn = Boolean(addon.builtIn);
      const mf = addon.manifest || {};
      const isConfigurable = Boolean(mf.behaviorHints && mf.behaviorHints.configurable);
      const configUrl = addon.transportUrl.replace(/\/manifest\.json(\?.*)?$/i, '/configure');
      const badgeLabel = isBuiltIn ? 'مدمجة رسميًا' : addon.globalShared ? '👑 معممة للأصدقاء' : 'إضافة مخصصة';
      card.innerHTML = `
        <div>
          <h4>🧩 ${addon.name || addon.id}</h4>
          <p>${addon.description || ''}</p>
        </div>
        <div class="row-gap">
          <span class="badge">${badgeLabel}</span>
          ${isConfigurable ? `<a href="${configUrl}" target="_blank" rel="noopener" class="btn-glass small-btn" style="text-decoration:none;">⚙️ إعداد</a>` : ''}
          <button class="btn-glass small-btn copy-mf-btn">📋 نسخ الرابط</button>
          ${!isBuiltIn ? '<button class="btn-glass small-btn rm-addon-btn">حذف</button>' : ''}
        </div>
      `;
      card.querySelector('.copy-mf-btn').addEventListener('click', () => {
        navigator.clipboard.writeText(addon.transportUrl).then(() => showToast('تم نسخ رابط manifest.json للإضافة!'));
      });
      const rmBtn = card.querySelector('.rm-addon-btn');
      if (rmBtn) {
        rmBtn.addEventListener('click', async () => {
          if (addon.globalShared) {
            const pin = state.adminPin || window.prompt('أدخل رمز المالك لإزالة الإضافة المعممة:') || '';
            if (!pin.trim()) return;
            const inspectRes = await apiFetch('/api/admin/inspect', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ admin_pin: pin.trim() }),
            });
            const inspectData = await inspectRes.json();
            if (!inspectRes.ok || !inspectData.ok) {
              showToast('رمز المالك غير صحيح');
              return;
            }
            state.adminPin = pin.trim();
            const globals = (inspectData.global_extra_addons || []).filter((x) => x.transportUrl !== addon.transportUrl);
            await apiFetch('/api/admin/update', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ admin_pin: state.adminPin, global_extra_addons: globals }),
            });
          } else {
            const saved = readJson(KEY_USER_ADDONS, []).filter((x) => x.transportUrl !== addon.transportUrl);
            writeJson(KEY_USER_ADDONS, saved);
          }
          state.addons = state.addons.filter((x) => x.transportUrl !== addon.transportUrl);
          renderAddonsView();
          populateDiscoverCatalogs();
          loadBoardShelves();
          showToast('تمت إزالة الإضافة');
        });
      }
      listEl.appendChild(card);
    });

    loadCommunityStore();
  }

  async function loadCommunityStore() {
    const storeGrid = $('community-store-grid');
    if (!storeGrid) return;
    if (!communityStoreCache) {
      storeGrid.innerHTML = '<p class="muted-note">جارٍ جلب متجر إضافات Stremio الرسمي والمجتمعي...</p>';
      try {
        const [offRes, comRes] = await Promise.all([
          apiFetch('/api/addon-proxy?url=' + encodeURIComponent('https://v3-cinemeta.strem.io/addon_catalog/all/official.json')),
          apiFetch('/api/addon-proxy?url=' + encodeURIComponent('https://v3-cinemeta.strem.io/addon_catalog/all/community.json')),
        ]);
        const offData = await offRes.json();
        const comData = await comRes.json();
        const merged = [...((offData && offData.addons) || []), ...((comData && comData.addons) || [])];
        const seen = new Set();
        communityStoreCache = merged.filter((x) => {
          if (!x || !x.transportUrl || seen.has(x.transportUrl)) return false;
          seen.add(x.transportUrl);
          return true;
        });
      } catch (e) {
        storeGrid.innerHTML = '<p class="muted-note">تعذر تحميل المتجر حاليًا.</p>';
        return;
      }
    }
    renderCommunityStoreGrid();
  }

  function renderCommunityStoreGrid() {
    const storeGrid = $('community-store-grid');
    if (!storeGrid || !communityStoreCache) return;
    const q = (($('store-search-input') && $('store-search-input').value) || '').trim().toLowerCase();
    storeGrid.innerHTML = '';

    const items = communityStoreCache.filter((entry) => {
      const mf = entry.manifest || {};
      if (!entry.transportUrl || !mf.name) return false;
      if (!q) return true;
      return `${mf.name} ${mf.description || ''}`.toLowerCase().includes(q);
    });

    items.slice(0, 48).forEach((entry) => {
      const mf = entry.manifest || {};
      const already = state.addons.some((a) => a.transportUrl === entry.transportUrl || a.id === mf.id);
      const card = document.createElement('div');
      card.className = 'addon-card';
      card.innerHTML = `
        <div>
          <h4>📦 ${mf.name}</h4>
          <p>${(mf.description || '').slice(0, 140)}</p>
        </div>
        <div class="row-gap">
          <button class="btn-glass small-btn store-inst-btn" ${already ? 'disabled' : ''}>
            ${already ? '✓ مثبتة' : '+ تثبيت'}
          </button>
        </div>
      `;
      const btn = card.querySelector('.store-inst-btn');
      if (!already) {
        btn.addEventListener('click', async () => {
          btn.disabled = true;
          btn.textContent = 'جارٍ التثبيت...';
          try {
            const makeGlobal = Boolean($('addon-global-chk') && $('addon-global-chk').checked);
            await installAddonByUrl(entry.transportUrl, makeGlobal);
            showToast(`تم تثبيت «${mf.name}» بنجاح!`);
          } catch (err) {
            btn.disabled = false;
            btn.textContent = '+ تثبيت';
            showToast(err.message || 'تعذر تثبيت الإضافة');
          }
        });
      }
      storeGrid.appendChild(card);
    });
  }

  if ($('store-search-input')) {
    $('store-search-input').addEventListener('input', renderCommunityStoreGrid);
  }

  $('add-addon-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const url = $('addon-url-input').value.trim();
    const makeGlobal = Boolean($('addon-global-chk') && $('addon-global-chk').checked);
    const msgEl = $('addon-msg');
    msgEl.classList.remove('hidden');
    msgEl.textContent = '⚡ جارٍ فحص الرابط والتعرف على صيغته تلقائيًا...';
    unlockVideoOnUserGesture('⚡ جارٍ فحص الرابط والتشغيل التلقائي...');

    try {
      const out = await resolveAndOpenAnyInput(url, makeGlobal);
      $('addon-url-input').value = '';
      if (out.kind === 'addon') {
        msgEl.textContent = makeGlobal
          ? `✅ تم تثبيت وتعميم «${out.manifest.name}» لجميع أصدقائك!`
          : `✅ تم تثبيت «${out.manifest.name}» بنجاح!`;
      } else {
        msgEl.textContent = `▶️ تم بدء تشغيل «${out.title}» تلقائيًا!`;
      }
    } catch (err) {
      closePlayer();
      msgEl.textContent = '❌ ' + (err.message || 'تعذر فتح الرابط');
    }
  });

  // Universal Link / Local File Modal Handlers
  if ($('open-universal-modal-btn')) {
    $('open-universal-modal-btn').addEventListener('click', () => {
      $('universal-link-modal').classList.remove('hidden');
      $('universal-link-status').classList.add('hidden');
      setTimeout(() => $('universal-link-input').focus(), 80);
    });
  }
  if ($('close-universal-modal-btn')) {
    $('close-universal-modal-btn').addEventListener('click', () => {
      $('universal-link-modal').classList.add('hidden');
    });
  }
  if ($('universal-link-form')) {
    $('universal-link-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const raw = $('universal-link-input').value.trim();
      const stEl = $('universal-link-status');
      if (!raw) return;
      stEl.classList.remove('hidden');
      stEl.textContent = '⚡ جارٍ الفتح التلقائي...';
      $('universal-link-modal').classList.add('hidden');
      unlockVideoOnUserGesture('⚡ جارٍ فتح الرابط والتشغيل التلقائي...');
      try {
        await resolveAndOpenAnyInput(raw, false);
        $('universal-link-input').value = '';
      } catch (err) {
        closePlayer();
        $('universal-link-modal').classList.remove('hidden');
        stEl.textContent = '❌ ' + (err.message || 'تعذر فتح الرابط');
      }
    });
  }
  if ($('local-media-file-input')) {
    $('local-media-file-input').addEventListener('change', (e) => {
      const file = e.target.files && e.target.files[0];
      if (!file) return;
      $('universal-link-modal').classList.add('hidden');
      const fname = file.name || 'ملف محلي';
      if (fname.toLowerCase().endsWith('.m3u') || fname.toLowerCase().endsWith('.m3u8')) {
        const reader = new FileReader();
        reader.onload = () => {
          const txt = String(reader.result || '');
          const lines = txt.split(/\r?\n/);
          const localStreams = [];
          let pendingTitle = '';
          for (const rawLn of lines) {
            const ln = rawLn.trim();
            if (!ln) continue;
            if (ln.startsWith('#EXTINF:')) {
              pendingTitle = ln.includes(',') ? ln.split(',').slice(1).join(',').trim() : 'قناة';
            } else if (!ln.startsWith('#') && /^https?:\/\//i.test(ln)) {
              localStreams.push({
                name: pendingTitle || `قناة #${localStreams.length + 1}`,
                title: fname,
                url: ln,
                behaviorHints: { iosReady: true },
              });
              pendingTitle = '';
            }
          }
          if (localStreams.length) {
            state.currentMeta = null;
            state.currentEpisodes = [];
            state.currentEpisodeObj = null;
            state.currentStreamsPool = localStreams;
            unlockVideoOnUserGesture(`جارٍ تشغيل ${localStreams[0].name}...`);
            launchPlayer(localStreams[0], 'tv', '', fname, 0);
          } else {
            showToast('لم يتم العثور على روابط قنوات داخل ملف M3U');
          }
        };
        reader.readAsText(file);
      } else {
        const blobUrl = URL.createObjectURL(file);
        const stObj = { name: fname, title: 'ملف من جهازك', url: blobUrl, behaviorHints: { iosReady: true } };
        state.currentMeta = null;
        state.currentEpisodes = [];
        state.currentEpisodeObj = null;
        state.currentStreamsPool = [stObj];
        unlockVideoOnUserGesture(`جارٍ تشغيل ${fname}...`);
        launchPlayer(stObj, 'movie', '', fname, 0);
      }
      e.target.value = '';
    });
  }

  // Share & Install Helpers
  function updateShareUrlBox() {
    const inv = localStorage.getItem(KEY_INVITE) || '';
    const u = new URL(window.location.origin + '/');
    if (inv) u.searchParams.set('invite', inv);
    $('share-current-url').value = u.toString();
  }

  $('copy-share-url-btn').addEventListener('click', () => {
    navigator.clipboard.writeText($('share-current-url').value).then(() => {
      showToast('تم نسخ رابط التطبيق للمشاركة!');
    });
  });

  $('android-pwa-btn').addEventListener('click', async () => {
    if (state.deferredInstallPrompt) {
      state.deferredInstallPrompt.prompt();
      await state.deferredInstallPrompt.userChoice;
      state.deferredInstallPrompt = null;
    } else if (isIOS) {
      showToast('على الآيفون: اضغط زر المشاركة ⬆️ بالأسفل ثم «إضافة إلى الشاشة الرئيسية»');
    } else {
      showToast('من قائمة المتصفح (⋮) اختر «تثبيت التطبيق» أو «الإضافة إلى الشاشة الرئيسية»');
    }
  });

  // Owner Privacy & Invite Panel
  $('owner-unlock-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const pin = $('owner-pin-input').value.trim();
    const errEl = $('owner-unlock-err');
    errEl.classList.add('hidden');
    try {
      const res = await apiFetch('/api/admin/inspect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ admin_pin: pin }),
      });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        errEl.textContent = data.error || 'رمز المالك غير صحيح';
        errEl.classList.remove('hidden');
        return;
      }
      state.adminPin = pin;
      $('owner-unlock-box').classList.add('hidden');
      $('owner-settings-box').classList.remove('hidden');
      $('cfg-app-name').value = data.app_name || 'دودو ستريم';
      $('cfg-invite-code').value = data.invite_code || '';
      $('cfg-privacy-mode').value = data.privacy_mode || 'internet_only';
    } catch (err) {
      errEl.textContent = 'تعذر التحقق من رمز المالك';
      errEl.classList.remove('hidden');
    }
  });

  $('owner-save-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const payload = {
      admin_pin: state.adminPin,
      app_name: $('cfg-app-name').value.trim(),
      invite_code: $('cfg-invite-code').value.trim(),
      privacy_mode: $('cfg-privacy-mode').value,
      new_admin_pin: $('cfg-new-pin').value.trim(),
    };
    const res = await apiFetch('/api/admin/update', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (res.ok && data.ok) {
      if (payload.new_admin_pin) state.adminPin = payload.new_admin_pin;
      $('cfg-new-pin').value = '';
      localStorage.setItem(KEY_INVITE, data.invite_code);
      applyBranding(data.app_name);
      updateShareUrlBox();
      const msg = $('owner-save-msg');
      msg.textContent = '✅ تم حفظ إعدادات الخصوصية ورمز الدعوة فورًا!';
      msg.classList.remove('hidden');
      showToast('تم حفظ الإعدادات بنجاح');
    }
  });

  $('copy-friend-invite-btn').addEventListener('click', () => {
    const inv = $('cfg-invite-code').value.trim();
    const link = `${window.location.origin}/?invite=${encodeURIComponent(inv)}`;
    const text =
      `🎬 دعوة خاصة لتطبيق «${state.appName}»\n` +
      `الأفلام والمسلسلات ومكتبة الكرتون المدبلج والترجمة العربية:\n` +
      `🔗 الرابط المباشر: ${link}\n` +
      `🔑 رمز الدعوة: ${inv}\n\n` +
      `🍎 للآيفون: افتح الرابط في Safari ثم اضغط زر المشاركة ⬆️ واختر «إضافة إلى الشاشة الرئيسية».\n` +
      `🤖 للأندرويد: افتح الرابط في Chrome واضغط «تثبيت التطبيق».`;
    navigator.clipboard.writeText(text).then(() => showToast('تم نسخ رسالة الدعوة الكاملة للأصدقاء!'));
  });

  // Start!
  bootstrapApp();
})();
