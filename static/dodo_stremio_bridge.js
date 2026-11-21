/* Dodo Stream — Official Stremio v5.2.0 Main-Thread Supercharger (GPL-2.0) */
(function () {
  var ORIGIN = window.location.origin;
  var KEY_INVITE = 'dodo_invite_code';
  var KEY_FIT = 'dodo_video_fit_mode';
  var dodoSig = '';
  var appName = 'دودو ستريم';
  var reqCounter = 1;
  var pendingWorkerReplies = {};

  // Preserve ?invite=... from URL into localStorage immediately
  try {
    var qs = new URLSearchParams(window.location.search);
    var urlInv = qs.get('invite');
    if (urlInv) {
      localStorage.setItem(KEY_INVITE, urlInv.trim());
    }
  } catch (e) {}

  // ⬆ ترتيب الأقسام المحفوظ: يُطبَّق على ملف profile قبل إنشاء محرك Stremio (ضمان صفر سباقات)
  function dodoApplySavedOrderToProfile() {
    try {
      var raw = localStorage.getItem('dodo_addon_order_v1');
      if (!raw) return;
      var urls = JSON.parse(raw);
      if (!Array.isArray(urls) || urls.length < 2) return;
      var prof = JSON.parse(localStorage.getItem('profile') || 'null');
      if (!prof || !Array.isArray(prof.addons) || prof.addons.length < 2) return;
      var seen = {};
      var ordered = [];
      urls.forEach(function (u) {
        if (seen[u]) return;
        for (var i = 0; i < prof.addons.length; i++) {
          if (prof.addons[i].transportUrl === u) { seen[u] = true; ordered.push(prof.addons[i]); break; }
        }
      });
      prof.addons.forEach(function (a) {
        if (!seen[a.transportUrl]) { seen[a.transportUrl] = true; ordered.push(a); }
      });
      var changed = false;
      for (var c = 0; c < ordered.length; c++) {
        if (ordered[c] !== prof.addons[c]) { changed = true; break; }
      }
      if (changed && ordered.length === prof.addons.length) {
        prof.addons = ordered;
        localStorage.setItem('profile', JSON.stringify(prof));
      }
    } catch (e) {}
  }
  dodoApplySavedOrderToProfile();

  function getInvite() {
    try {
      return localStorage.getItem(KEY_INVITE) || '';
    } catch (e) {
      return '';
    }
  }

  // 1. Capture Stremio's Core WASM Web Worker instance so we can dispatch InstallAddon & encodeStream live!
  var OrigWorker = window.Worker;
  if (typeof OrigWorker === 'function') {
    window.Worker = function (scriptURL, options) {
      var w = new OrigWorker(scriptURL, options);
      if (String(scriptURL).indexOf('worker.js') !== -1) {
        window.__dodoCoreWorker = w;
        w.addEventListener('message', function (ev) {
          var d = ev && ev.data;
          if (d && d.__dodoReply && pendingWorkerReplies[d.__dodoReply]) {
            var cb = pendingWorkerReplies[d.__dodoReply];
            delete pendingWorkerReplies[d.__dodoReply];
            cb(d);
          }
        });
      }
      return w;
    };
    window.Worker.prototype = OrigWorker.prototype;
  }

  function callCoreWorker(cmd, payload) {
    return new Promise(function (resolve, reject) {
      var w = window.__dodoCoreWorker;
      if (!w) return reject(new Error('محرك Stremio قيد التحميل، حاول بعد لحظة'));
      var id = 'dodo_' + (reqCounter++);
      pendingWorkerReplies[id] = function (res) {
        if (res.ok) resolve(res);
        else reject(new Error(res.error || 'خطأ في المحرك'));
      };
      w.postMessage(Object.assign({ __dodoCmd: cmd, reqId: id }, payload));
      setTimeout(function () {
        if (pendingWorkerReplies[id]) {
          delete pendingWorkerReplies[id];
          reject(new Error('انتهت مهلة الاستجابة'));
        }
      }, 8000);
    });
  }

  // 2. Main-Thread Fetch Enhancer: Instant 0ms Content-Type for HTMLVideo + /stremio-engine/ routing!
  var origFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    try {
      var rawUrl = typeof input === 'string' ? input : input && input.url ? input.url : String(input);
      var method = ((init && init.method) || (typeof Request !== 'undefined' && input instanceof Request && input.method) || 'GET').toUpperCase();

      // 1. Fast-path ALL HEAD Content-Type probes FIRST (0ms in memory) so HTMLVideo/HLS.js NEVER falls back to raw <video src=".m3u8">!
      if (method === 'HEAD') {
        if (rawUrl.indexOf('blob:') === 0) {
          return Promise.resolve(new Response('', { status: 200, headers: { 'content-type': 'video/mp4' } }));
        }
        if (/^https?:\/\//i.test(rawUrl)) {
          if (/\/[0-9a-f]{40}\/\d+/i.test(rawUrl)) {
            return Promise.resolve(
              new Response('', { status: 200, headers: { 'content-type': 'application/octet-stream' } })
            );
          }
          var cleanPath = rawUrl.split('?')[0].toLowerCase();
          if (
            cleanPath.endsWith('.m3u8') ||
            rawUrl.indexOf('/cartoony-hls/') !== -1 ||
            rawUrl.indexOf('/universal-hls/') !== -1 ||
            rawUrl.indexOf('/hlsv2/') !== -1 ||
            rawUrl.indexOf('/stream-proxy.m3u8') !== -1
          ) {
            return Promise.resolve(
              new Response('', { status: 200, headers: { 'content-type': 'application/vnd.apple.mpegurl' } })
            );
          }
          if (cleanPath.endsWith('.mp4') || cleanPath.endsWith('.m4v')) {
            return Promise.resolve(
              new Response('', { status: 200, headers: { 'content-type': 'video/mp4' } })
            );
          }
          if (cleanPath.endsWith('.webm')) {
            return Promise.resolve(
              new Response('', { status: 200, headers: { 'content-type': 'video/webm' } })
            );
          }
        }
      }

      // 2. Route local 127.0.0.1:11470 streaming server calls through Dodo /stremio-engine/
      if (/^https?:\/\/(127\.0\.0\.1|localhost):11470(\/|$)/i.test(rawUrl)) {
        var sub = rawUrl.replace(/^https?:\/\/(127\.0\.0\.1|localhost):11470\/?/i, '');
        var engUrl = ORIGIN + '/stremio-engine/' + sub;
        if (dodoSig && engUrl.indexOf('sig=') === -1) {
          engUrl += (engUrl.indexOf('?') === -1 ? '?' : '&') + 'sig=' + encodeURIComponent(dodoSig);
        }
        return origFetch(engUrl, init);
      }

      // 3. Instant 0ms bypass of withStreamingServer hlsv2/probe for streams ALREADY in HLS / MP4 / WebM!
      if (rawUrl.indexOf('/hlsv2/probe') !== -1) {
        try {
          var probeU = new URL(rawUrl, ORIGIN);
          var mediaU = (probeU.searchParams.get('mediaURL') || '').toLowerCase();
          if (
            mediaU.indexOf('.m3u8') !== -1 ||
            mediaU.indexOf('/cartoony-hls/') !== -1 ||
            mediaU.indexOf('/api/universal-hls/') !== -1 ||
            mediaU.indexOf('/api/stream-proxy') !== -1 ||
            /\.(mp4|m4v|webm)(\?|$)/.test(mediaU)
          ) {
            return Promise.reject(new Error('direct_web_ready_stream'));
          }
        } catch (e) {}
      }

      // 4. Attach media sig to same-origin Stremio engine / subtitle endpoints for Phone Privacy Gate
      if (
        dodoSig &&
        rawUrl.indexOf(ORIGIN) === 0 &&
        rawUrl.indexOf('sig=') === -1 &&
        (/\/(hlsv2|stremio-engine|subtitles\.vtt|opensubHash|subtitleSignature|embedded-ass|proxy|[0-9a-f]{40})(\/|\?|$)/i.test(rawUrl))
      ) {
        var signedUrl = rawUrl + (rawUrl.indexOf('?') === -1 ? '?' : '&') + 'sig=' + encodeURIComponent(dodoSig);
        return origFetch(signedUrl, init);
      }

      if (method === 'HEAD' && /^https?:\/\//i.test(rawUrl)) {
        return origFetch(input, init).catch(function () {
          var headProxy = ORIGIN + '/api/stream-head?url=' + encodeURIComponent(rawUrl);
          if (dodoSig) headProxy += '&sig=' + encodeURIComponent(dodoSig);
          return origFetch(headProxy);
        });
      }
    } catch (e) {}
    return origFetch(input, init);
  };

  // 3. Toast Helper
  var toastTimer = null;
  function showDodoToast(msg) {
    var el = document.getElementById('dodo-toast-pill');
    if (!el) {
      el = document.createElement('div');
      el.id = 'dodo-toast-pill';
      el.className = 'dodo-toast-pill';
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.style.opacity = '1';
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      el.style.opacity = '0';
    }, 2400);
  }

  // 4. Video Screen-Fill / Fullscreen Manager ("ملء كامل الشاشة 100%")
  var FIT_MODES = ['contain', 'cover', 'fill'];
  var FIT_LABELS = {
    contain: '↔ أبعاد أصلية',
    cover: '🔍 ملء الشاشة بدون حواف',
    fill: '⛶ تمديد كامل الشاشة 100%',
  };

  function getFitMode() {
    try {
      var saved = localStorage.getItem(KEY_FIT);
      if (FIT_MODES.indexOf(saved) !== -1) return saved;
    } catch (e) {}
    return 'cover'; // Default to full edge-to-edge screen coverage as requested!
  }

  function applyFitMode(mode) {
    var html = document.documentElement;
    var targetCls = 'dodo-fit-' + mode;
    if (!html.classList.contains(targetCls)) {
      html.classList.remove('dodo-fit-contain', 'dodo-fit-cover', 'dodo-fit-fill');
      html.classList.add(targetCls);
    }
    try {
      if (localStorage.getItem(KEY_FIT) !== mode) {
        localStorage.setItem(KEY_FIT, mode);
      }
    } catch (e) {}
    var vid = document.querySelector('[class*="video-container-"] video');
    if (vid && vid.style.getPropertyValue('object-fit') !== mode) {
      vid.style.setProperty('object-fit', mode, 'important');
    }
    var btn = document.getElementById('dodo-aspect-btn');
    var label = FIT_LABELS[mode] || '⛶ ملء الشاشة';
    if (btn && btn.textContent !== label) {
      btn.textContent = label;
    }
  }

  function cycleFitMode() {
    var cur = getFitMode();
    var next = FIT_MODES[(FIT_MODES.indexOf(cur) + 1) % FIT_MODES.length];
    applyFitMode(next);
    showDodoToast('وضع العرض: ' + FIT_LABELS[next]);
  }

  function toggleHardwareFullscreen() {
    var vid = document.querySelector('[class*="video-container-"] video');
    var doc = document;
    var isFs = Boolean(doc.fullscreenElement || doc.webkitFullscreenElement || (vid && vid.webkitDisplayingFullscreen));
    if (!isFs) {
      var root = doc.documentElement;
      if (root.requestFullscreen) {
        root.requestFullscreen().catch(function () {
          if (vid && vid.webkitEnterFullscreen) vid.webkitEnterFullscreen();
        });
      } else if (root.webkitRequestFullscreen) {
        root.webkitRequestFullscreen();
      } else if (vid && vid.webkitEnterFullscreen) {
        vid.webkitEnterFullscreen();
      }
    } else {
      if (doc.exitFullscreen) doc.exitFullscreen().catch(function () {});
      else if (doc.webkitExitFullscreen) doc.webkitExitFullscreen();
      else if (vid && vid.webkitExitFullscreen) vid.webkitExitFullscreen();
    }
  }

  // Keyboard shortcut Z for Aspect Fill, Double-click on video container for Fullscreen!
  window.addEventListener('keydown', function (e) {
    if (window.location.hash.indexOf('#/player/') !== 0) return;
    if (e.target && /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
    if (e.key === 'z' || e.key === 'Z' || e.key === 'ئ') {
      e.preventDefault();
      cycleFitMode();
    }
  });

  document.addEventListener('dblclick', function (e) {
    if (window.location.hash.indexOf('#/player/') !== 0) return;
    if (e.target && e.target.closest && (e.target.closest('[class*="control-bar-"]') || e.target.closest('[class*="menu-layer-"]') || e.target.closest('[class*="side-drawer-"]'))) {
      return;
    }
    toggleHardwareFullscreen();
  });

  // Unlock Mobile Audio/Video Context on first user interaction so Autoplay NEVER stalls on iOS/Android!
  var audioUnlocked = false;
  function unlockMediaOnce() {
    if (audioUnlocked) return;
    audioUnlocked = true;
    try {
      var Ctx = window.AudioContext || window.webkitAudioContext;
      if (Ctx) {
        var ctx = new Ctx();
        if (ctx.state === 'suspended') ctx.resume();
      }
    } catch (e) {}
  }
  document.addEventListener('pointerdown', unlockMediaOnce, { passive: true });
  document.addEventListener('click', unlockMediaOnce, { passive: true });

  // 5. Observe Stremio DOM for Player Controls, Guaranteed Autoplay & Instant Stream Play Button
  function syncRouteClassAndEnhancements() {
    var inPlayer = window.location.hash.indexOf('#/player/') === 0;
    document.documentElement.classList.toggle('dodo-in-player', inPlayer);

    if (inPlayer) {
      applyFitMode(getFitMode());
      // Ensure <video> starts playing automatically & handles FFmpeg fallback if needed
      var vid = document.querySelector('[class*="video-container-"] video');
      if (vid && !vid.__dodoHooked) {
        vid.__dodoHooked = true;
        vid.autoplay = true;
        vid.playsInline = true;
        vid.setAttribute('playsinline', '');
        vid.setAttribute('webkit-playsinline', '');
        vid.style.setProperty('object-fit', getFitMode(), 'important');

        var ensurePlay = function () {
          vid.style.setProperty('object-fit', getFitMode(), 'important');
          if (vid.paused && vid.readyState >= 2 && vid.currentTime < 0.8) {
            var p = vid.play();
            if (p && typeof p.catch === 'function') {
              p.catch(function () {
                vid.muted = true;
                vid.play().catch(function () {});
                showDodoToast('🔊 اضغط على أيقونة الصوت لتفعيله');
              });
            }
          }
        };
        vid.addEventListener('loadedmetadata', ensurePlay);
        vid.addEventListener('canplay', ensurePlay);

        // Automatic Universal FFmpeg HLS fallback if a raw external MKV/AVI/AC3 HTTP URL fails natively
        vid.addEventListener('error', function () {
          var badSrc = vid.currentSrc || vid.src || '';
          if (
            badSrc &&
            badSrc.indexOf('/api/universal-hls/') === -1 &&
            badSrc.indexOf('/cartoony-hls/') === -1 &&
            badSrc.indexOf('/stremio-engine/') === -1 &&
            badSrc.indexOf('/hlsv2/') === -1 &&
            !/\/[0-9a-f]{40}\//i.test(badSrc) &&
            /^https?:\/\//i.test(badSrc)
          ) {
            var sid = Array.from(badSrc).reduce(function (h, c) {
              return ((h << 5) - h + c.charCodeAt(0)) | 0;
            }, 0);
            var hexSid = Math.abs(sid).toString(16).padStart(10, '0') + 'dodo';
            var fallbackHls = ORIGIN + '/api/universal-hls/' + hexSid + '/index.m3u8?url=' + encodeURIComponent(badSrc);
            if (dodoSig) fallbackHls += '&sig=' + encodeURIComponent(dodoSig);
            showDodoToast('⚡ جارٍ التحويل التلقائي الفوري للصيغة...');
            if (vid.canPlayType('application/vnd.apple.mpegurl')) {
              vid.src = fallbackHls;
              vid.load();
              vid.play().catch(function () {});
            } else {
              callCoreWorker('encodeStream', {
                stream: {
                  name: 'دودو الشامل • HLS',
                  description: 'تحويل تلقائي فوري',
                  url: fallbackHls,
                  behaviorHints: { notWebReady: false },
                },
              })
                .then(function (enc) {
                  if (enc && enc.encoded) window.location.hash = '#/player/' + enc.encoded;
                })
                .catch(function () {});
            }
          }
        });
      }

      // Inject Screen-Fill button into Stremio's Player Control Bar
      var ctrlBar = document.querySelector('[class*="control-bar-buttons-container-"]') || document.querySelector('[class*="control-bar-layer-"]');
      if (ctrlBar && !document.getElementById('dodo-aspect-btn')) {
        var btn = document.createElement('button');
        btn.id = 'dodo-aspect-btn';
        btn.type = 'button';
        btn.title = 'تغيير ملء الشاشة (اختصار Z)';
        btn.textContent = FIT_LABELS[getFitMode()];
        btn.addEventListener('click', function (ev) {
          ev.stopPropagation();
          cycleFitMode();
        });
        ctrlBar.appendChild(btn);
      }
    }

    // In MetaDetails: Inject 1-Click "▶ تشغيل فوري تلقائي" at top of Streams list!
    if (window.location.hash.indexOf('#/detail/') === 0 && !document.getElementById('dodo-instant-play-btn')) {
      var realStreams = Array.from(document.querySelectorAll('a[href*="#/player/"]')).filter(function (a) {
        var t = a.innerText || '';
        return t.indexOf('المقطع الدعائي') === -1 && t.indexOf('Trailer') === -1;
      });
      var firstStream = realStreams[0];
      if (firstStream && firstStream.parentElement) {
        var playNowBtn = document.createElement('button');
        playNowBtn.id = 'dodo-instant-play-btn';
        playNowBtn.type = 'button';
        playNowBtn.innerHTML = '▶ تشغيل فوري تلقائي (أفضل سيرفر)';
        playNowBtn.addEventListener('click', function () {
          var avail = Array.from(document.querySelectorAll('a[href*="#/player/"]')).filter(function (a) {
            var t = a.innerText || '';
            return t.indexOf('المقطع الدعائي') === -1 && t.indexOf('Trailer') === -1;
          });
          // Smart Multi-Device Picker: prefer direct HLS/MP4 or 1080p/720p H.264 (0% Video CPU stream-copy) over heavy 4K HEVC!
          var best = avail[0];
          var bestScore = -999;
          for (var i = 0; i < avail.length; i++) {
            var txt = (avail[i].innerText || '').toLowerCase();
            var score = 0;
            if (txt.indexOf('دودو') !== -1 || txt.indexOf('480p') !== -1 || txt.indexOf('مباشر') !== -1) score += 50;
            if (txt.indexOf('1080p') !== -1 || txt.indexOf('720p') !== -1) score += 30;
            if (/\b(x264|h264|avc|mp4)\b/.test(txt)) score += 20;
            if (/\b(4k|2160p|remux|dv|dolby vision)\b/.test(txt)) score -= 40;
            if (/\b(hevc|x265|h265|10bit)\b/.test(txt)) score -= 18;
            score -= i * 2; // Preserve seed-sorted order among equals
            if (score > bestScore) {
              bestScore = score;
              best = avail[i];
            }
          }
          if (best) best.click();
        });
        firstStream.parentElement.insertBefore(playNowBtn, firstStream);
      }
    }
  }

  window.addEventListener('hashchange', syncRouteClassAndEnhancements);

  // 6. Universal Link / File / Privacy Quick Modal ("⚡ دودو")
  async function resolveAndOpenUniversal(rawUrl, makeGlobal) {
    var headers = { 'Content-Type': 'application/json' };
    if (getInvite()) headers['X-Dodo-Invite'] = getInvite();
    var res = await origFetch(ORIGIN + '/api/resolve-link', {
      method: 'POST',
      headers: headers,
      body: JSON.stringify({ url: rawUrl }),
    });
    var data = await res.json();
    if (!res.ok || !data || !data.ok) {
      throw new Error((data && data.error) || 'تعذر التعرف على الرابط أو الصيغة');
    }

    if (data.kind === 'addon') {
      await callCoreWorker('installAddon', {
        addon: { transportUrl: data.transportUrl, manifest: data.manifest },
      });
      if (makeGlobal) {
        var pin = window.prompt('أدخل رمز المالك لتعميم الإضافة لجميع الأصدقاء :') || '';
        if (pin.trim()) {
          var ir = await origFetch(ORIGIN + '/api/admin/inspect', {
            method: 'POST',
            headers: headers,
            body: JSON.stringify({ admin_pin: pin.trim() }),
          });
          var idata = await ir.json();
          if (ir.ok && idata.ok) {
            var gl = (idata.global_extra_addons || []).filter(function (x) {
              return x.transportUrl !== data.transportUrl;
            });
            gl.push({
              id: data.manifest.id,
              name: data.manifest.name,
              description: data.manifest.description || '',
              transportUrl: data.transportUrl,
              globalShared: true,
            });
            await origFetch(ORIGIN + '/api/admin/update', {
              method: 'POST',
              headers: headers,
              body: JSON.stringify({ admin_pin: pin.trim(), global_extra_addons: gl }),
            });
          }
        }
      }
      showDodoToast('✅ تم تثبيت الإضافة في Stremio: ' + data.manifest.name);
      return data;
    }

    // Direct Media / HLS / MKV / MP4 / Magnet / Torrent / YouTube -> Play right inside Stremio's native Player!
    var st = data.streams && data.streams[0];
    if (!st) throw new Error('لم يتم العثور على بث صالح');
    var encRes = await callCoreWorker('encodeStream', { stream: st });
    if (encRes && encRes.encoded) {
      window.location.hash = '#/player/' + encRes.encoded;
      showDodoToast('▶ جاري التشغيل داخل مشغل Stremio...');
    }
    return data;
  }

  function mountDodoQuickTools() {
    if (document.getElementById('dodo-tools-modal')) return;
    var modal = document.createElement('div');
    modal.id = 'dodo-tools-modal';
    modal.className = 'dodo-modal-backdrop dodo-hidden';
    var shareLink = ORIGIN + (getInvite() ? '/?invite=' + encodeURIComponent(getInvite()) : '/');

    modal.innerHTML =
      '<div class="dodo-modal-card">' +
      '  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">' +
      '    <h3 style="margin:0;">⚡ أدوات ' + appName + ' الشاملة</h3>' +
      '    <button type="button" id="dodo-close-tools" class="dodo-btn-ghost">✕ إغلاق</button>' +
      '  </div>' +
      '  <p style="margin:0 0 10px;color:#94a3b8;font-size:13px;line-height:1.55;">' +
      '    الصق <strong>أي رابط أو صيغة</strong> لتشغيلها أو تثبيتها فورًا داخل Stremio:<br/>' +
      '    • إضافات Stremio الحديثة (<code>manifest.json</code>) والقديمة (مثل <code>.../stremio/v1</code>)<br/>' +
      '    • قوائم القنوات (<code>.m3u</code>) أو روابط الفيديو المباشرة (<code>MP4, MKV, m3u8, TS, AVI</code>) أو <code>magnet:</code> أو يوتيوب!' +
      '  </p>' +
      '  <form id="dodo-any-link-form">' +
      '    <input type="text" id="dodo-any-link-input" class="dodo-field" dir="ltr" placeholder="https://... أو stremio://... أو magnet:?xt=..." required />' +
      '    <label style="display:flex;align-items:center;gap:8px;margin-top:9px;font-size:12.5px;color:#cbd5e1;cursor:pointer;">' +
      '      <input type="checkbox" id="dodo-any-global-chk" /> 👑 تعميم الإضافة تلقائيًا على أجهزة جميع الأصدقاء' +
      '    </label>' +
      '    <div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap;">' +
      '      <button type="submit" class="dodo-btn-primary" style="flex:1;">⚡ فتح / تثبيت / تشغيل فوري</button>' +
      '      <label class="dodo-btn-ghost" style="display:inline-flex;align-items:center;gap:6px;">' +
      '        📂 ملف من جهازك' +
      '        <input type="file" id="dodo-local-file" accept="video/*,audio/*,.mkv,.avi,.ts,.m3u,.m3u8" style="display:none;" />' +
      '      </label>' +
      '    </div>' +
      '  </form>' +
      '  <p id="dodo-any-status" style="margin:10px 0 0;font-size:13px;color:#38bdf8;display:none;"></p>' +
      '  <hr style="border:0;border-top:1px solid rgba(255,255,255,0.1);margin:16px 0;" />' +
      '  <div style="font-size:12.5px;color:#cbd5e1;">' +
      '    <strong>📲 رابط المشاركة وتثبيت التطبيق للأصدقاء (آيفون / أندرويد / لابتوب):</strong>' +
      '    <div style="display:flex;gap:8px;margin-top:8px;">' +
      '      <input type="text" id="dodo-share-input" class="dodo-field" dir="ltr" readonly value="' + shareLink.replace(/"/g, '&quot;') + '" />' +
      '      <button type="button" id="dodo-copy-share" class="dodo-btn-ghost">📋 نسخ</button>' +
      '    </div>' +
      '  </div>' +
      '</div>';
    document.body.appendChild(modal);

    document.getElementById('dodo-close-tools').addEventListener('click', function () {
      modal.classList.add('dodo-hidden');
    });
    modal.addEventListener('click', function (ev) {
      if (ev.target === modal) modal.classList.add('dodo-hidden');
    });

    document.getElementById('dodo-copy-share').addEventListener('click', function () {
      navigator.clipboard.writeText(document.getElementById('dodo-share-input').value).then(function () {
        showDodoToast('📋 تم نسخ رابط المشاركة!');
      });
    });

    document.getElementById('dodo-any-link-form').addEventListener('submit', async function (ev) {
      ev.preventDefault();
      var inp = document.getElementById('dodo-any-link-input');
      var st = document.getElementById('dodo-any-status');
      var makeG = document.getElementById('dodo-any-global-chk').checked;
      st.style.display = 'block';
      st.style.color = '#38bdf8';
      st.textContent = '⏳ جارٍ فحص الرابط والتنفيذ التلقائي...';
      try {
        await resolveAndOpenUniversal(inp.value.trim(), makeG);
        inp.value = '';
        st.style.display = 'none';
        modal.classList.add('dodo-hidden');
      } catch (err) {
        st.style.color = '#fb7185';
        st.textContent = '❌ ' + (err.message || 'تعذر فتح الرابط');
      }
    });

    document.getElementById('dodo-local-file').addEventListener('change', async function (ev) {
      var file = ev.target.files && ev.target.files[0];
      if (!file) return;
      var blobUrl = URL.createObjectURL(file);
      try {
        var enc = await callCoreWorker('encodeStream', {
          stream: {
            name: file.name,
            description: 'ملف محلي من جهازك',
            url: blobUrl,
            behaviorHints: {
              notWebReady: false,
            },
          },
        });
        if (enc && enc.encoded) {
          modal.classList.add('dodo-hidden');
          window.location.hash = '#/player/' + enc.encoded;
        }
      } catch (err) {
        showDodoToast('تعذر فتح الملف المحلي');
      }
      ev.target.value = '';
    });
  }

  // 7b. Section Arranger — سحب أقسام الصفحة الرئيسية في مكانها مباشرة
  // بنية صلبة: ترتيب منطقي في مصفوفة + مراقب طفريات يعيد فرضه كلما أعاد React الرسم
  function dodoIsHomeRoute() {
    var h = window.location.hash || '';
    return h === '' || h === '#' || h === '#/' || h.indexOf('#/?') === 0;
  }

  var DODO_ARRANGER_CSS =
    '[class*="board-row"].dodo-drag-section{outline:2px dashed rgba(124,58,237,0.75);outline-offset:-2px;border-radius:12px;cursor:grab;position:relative;}' +
    '[class*="board-row"].dodo-drag-section:hover{outline-color:#a78bfa;background:rgba(124,58,237,0.06);}' +
    '[class*="board-row"].dodo-dragging{opacity:0.35;}' +
    '[class*="board-row"].dodo-fixed-section{opacity:0.45;position:relative;}' +
    '.dodo-sec-nav{position:absolute;top:8px;inset-inline-end:10px;z-index:50;display:flex;gap:6px;}' +
    '.dodo-sec-nav button{width:38px;height:38px;border-radius:10px;border:1px solid rgba(255,255,255,0.2);background:rgba(30,27,45,0.92);color:#fff;font-size:16px;cursor:pointer;box-shadow:0 3px 10px rgba(0,0,0,0.35);}' +
    '.dodo-sec-nav button:active{background:#7c3aed;}' +
    '.dodo-sec-tag{position:absolute;top:10px;inset-inline-start:12px;z-index:50;font:600 11px inherit;color:#c4b5fd;background:rgba(30,27,45,0.9);border:1px solid rgba(124,58,237,0.5);border-radius:999px;padding:4px 12px;pointer-events:none;}' +
    '.dodo-save-bar{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);z-index:2147483600;display:flex;align-items:center;gap:10px;background:rgba(15,12,28,0.96);border:1px solid rgba(124,58,237,0.55);border-radius:999px;padding:10px 12px 10px 20px;box-shadow:0 8px 30px rgba(0,0,0,0.55);font:500 13px inherit;color:#e2e8f0;}' +
    '.dodo-save-bar button{border:0;border-radius:999px;padding:9px 18px;font:600 13px inherit;cursor:pointer;}' +
    '.dodo-save-bar .dodo-save-btn{background:linear-gradient(135deg,#7c3aed,#2563eb);color:#fff;}' +
    '.dodo-save-bar .dodo-cancel-btn{background:rgba(255,255,255,0.09);color:#cbd5e1;}';

  function dodoArrangerStyle() {
    if (document.getElementById('dodo-arranger-style')) return;
    var st = document.createElement('style');
    st.id = 'dodo-arranger-style';
    st.textContent = DODO_ARRANGER_CSS;
    document.head.appendChild(st);
  }

  function dodoGetBoardRows() {
    var tEl = document.querySelector('[class*="title-container"]');
    if (!tEl) return [];
    var row = tEl;
    while (row && String(row.className).indexOf('board-row') === -1) row = row.parentElement;
    if (!row || !row.parentElement) return [];
    var cont = row.parentElement;
    return Array.prototype.filter.call(cont.children, function (ch) {
      return String(ch.className).indexOf('board-row') !== -1;
    });
  }

  function dodoRowTitle(row) {
    var tEl = row.querySelector('[class*="title-container"]');
    return tEl ? String(tEl.textContent || '').trim() : '';
  }

  // مطابقة قسم (بعنوانه الظاهر) إلى إضافته — من ملف profile المخزن مباشرة
  function dodoBuildAddonKeysFromProfile() {
    var CAT_AR = { popular: 'رائج', top: 'رائج', featured: 'مميز', imdbrating: 'مميز', new: 'جديد', year: 'جديد' };
    var ADDON_AR = {
      'org.stremio.pubdomainmovies': 'افلام عامة',
      'com.linvo.pubdomainmovies': 'افلام عامة',
      'com.linvo.cinemeta': 'رائج'
    };
    var norm = function (s) {
      return String(s || '').toLowerCase().replace(/[\s_]+/g, ' ').replace(/^ال/, '').trim();
    };
    var prof = null;
    try { prof = JSON.parse(localStorage.getItem('profile') || 'null'); } catch (e) {}
    var addons = prof && Array.isArray(prof.addons) ? prof.addons : [];
    return addons.map(function (a) {
      var mf = a.manifest || {};
      var set = {};
      (mf.catalogs || []).forEach(function (c) {
        if (c.name) set[norm(c.name)] = true;
        if (c.id) {
          set[norm(c.id)] = true;
          var low = String(c.id).toLowerCase();
          if (CAT_AR[low]) set[norm(CAT_AR[low])] = true;
        }
        if (c.name && CAT_AR[norm(c.name)]) set[norm(CAT_AR[norm(c.name)])] = true;
      });
      if (mf.name) set[norm(mf.name)] = true;
      if (mf.id && ADDON_AR[mf.id]) set[norm(ADDON_AR[mf.id])] = true;
      return { url: a.transportUrl, name: mf.name || a.transportUrl, set: set };
    });
  }

  function dodoMatchTitleToAddon(title, keys) {
    if (!title) return null;
    var norm = function (s) {
      return String(s || '').toLowerCase().replace(/[\s_]+/g, ' ').replace(/^ال/, '').trim();
    };
    var np = norm(String(title).split(' - ')[0]);
    for (var i = 0; i < keys.length; i++) {
      if (keys[i].set[np]) return keys[i];
    }
    return null;
  }

  function mountDodoSectionArranger() {
    if (document.getElementById('dodo-arrange-btn')) return;
    dodoArrangerStyle();

    var editing = false;
    var arrangement = [];        // العناوين بالترتيب المنطقي الحالي
    var originalArrangement = [];
    var titleInfo = {};          // عنوان → {url, name}
    var arrObserver = null;
    var obsTimer = null;
    var saveBar = null;

    var btn = document.createElement('button');
    btn.type = 'button';
    btn.id = 'dodo-arrange-btn';
    btn.title = 'ترتيب أقسام الصفحة الرئيسية — اسحب كل قسم إلى موضعه';
    btn.textContent = '↕';
    btn.style.cssText =
      'position:fixed;bottom:18px;inset-inline-end:18px;z-index:2147483000;display:none;' +
      'width:46px;height:46px;border:0;border-radius:50%;cursor:pointer;' +
      'background:rgba(124,58,237,0.8);color:#fff;font-size:20px;line-height:1;' +
      'box-shadow:0 4px 16px rgba(0,0,0,0.4);opacity:0.5;transition:opacity .15s;';
    btn.addEventListener('mouseenter', function () { btn.style.opacity = '1'; });
    btn.addEventListener('mouseleave', function () { btn.style.opacity = '0.5'; });
    document.body.appendChild(btn);

    function syncVisibility() {
      btn.style.display = !editing && dodoIsHomeRoute() ? 'block' : 'none';
    }
    window.addEventListener('hashchange', syncVisibility);
    syncVisibility();

    function unmarkRow(row) {
      Array.prototype.slice.call(row.querySelectorAll('.dodo-sec-nav, .dodo-sec-tag')).forEach(function (el) { el.remove(); });
      row.classList.remove('dodo-drag-section', 'dodo-fixed-section', 'dodo-dragging');
      row.removeAttribute('data-dodo-addon');
      row.removeAttribute('data-dodo-title');
      row.removeAttribute('draggable');
    }

    function markRow(row) {
      var title = dodoRowTitle(row);
      if (!title) {
        row.classList.add('dodo-fixed-section');
        return null;
      }
      var info = titleInfo[title] || null;
      if (!info || !info.url) {
        row.classList.add('dodo-fixed-section');
        row.setAttribute('data-dodo-title', title);
        return null;
      }
      row.setAttribute('data-dodo-addon', info.url);
      row.setAttribute('data-dodo-title', title);
      row.classList.add('dodo-drag-section');
      row.setAttribute('draggable', 'true');
      if (!row.querySelector('.dodo-sec-tag')) {
        var tag = document.createElement('div');
        tag.className = 'dodo-sec-tag';
        tag.textContent = '↕ ' + (info.name || '');
        row.appendChild(tag);
      }
      if (!row.querySelector('.dodo-sec-nav')) {
        var nav = document.createElement('div');
        nav.className = 'dodo-sec-nav';
        function mkArrow(dir) {
          var b = document.createElement('button');
          b.type = 'button';
          b.textContent = dir === 'up' ? '⬆' : '⬇';
          b.addEventListener('click', function () { moveTitle(title, dir); });
          return b;
        }
        nav.appendChild(mkArrow('up'));
        nav.appendChild(mkArrow('down'));
        row.appendChild(nav);
      }
      if (!row.getAttribute('data-dodo-dnd')) {
        row.setAttribute('data-dodo-dnd', '1');
        row.addEventListener('dragstart', function () { row.classList.add('dodo-dragging'); });
        row.addEventListener('dragend', function () { row.classList.remove('dodo-dragging'); });
        row.addEventListener('dragover', function (ev) { ev.preventDefault(); });
        row.addEventListener('drop', function (ev) {
          ev.preventDefault();
          var cont = row.parentElement;
          if (!cont) return;
          var dragged = cont.querySelector('.dodo-dragging');
          if (!dragged || dragged === row) return;
          var fromTitle = dragged.getAttribute('data-dodo-title');
          var toTitle = row.getAttribute('data-dodo-title');
          if (fromTitle && toTitle && fromTitle !== toTitle) moveTitleBefore(fromTitle, toTitle);
        });
      }
      return title;
    }

    function moveTitle(title, dir) {
      var i = arrangement.indexOf(title);
      var j = dir === 'up' ? i - 1 : i + 1;
      if (i === -1 || j < 0 || j >= arrangement.length) return;
      arrangement.splice(j, 0, arrangement.splice(i, 1)[0]);
      applyArrangement();
      scrollToTitle(title);
    }

    function moveTitleBefore(fromTitle, toTitle) {
      var i = arrangement.indexOf(fromTitle);
      var j = arrangement.indexOf(toTitle);
      if (i === -1 || j === -1 || i === j) return;
      arrangement.splice(i, 1);
      j = arrangement.indexOf(toTitle);
      arrangement.splice(j, 0, fromTitle);
      applyArrangement();
    }

    function scrollToTitle(title) {
      var rows = dodoGetBoardRows();
      for (var i = 0; i < rows.length; i++) {
        if (rows[i].getAttribute('data-dodo-title') === title) {
          try { rows[i].scrollIntoView({ behavior: 'smooth', block: 'nearest' }); } catch (e) {}
          return;
        }
      }
    }

    // يعيد فرض الترتيب والأدوات على DOM الحالي (يُستدعى عند كل طفرة أثناء التحرير)
    function applyArrangement() {
      var rows = dodoGetBoardRows();
      if (!rows.length) return;
      var cont = rows[0].parentElement;
      // 1) تأكد أن كل صف معلَّم وهويته صحيحة
      rows.forEach(function (row) {
        var title = dodoRowTitle(row);
        var marked = row.getAttribute('data-dodo-title');
        if (!marked || marked !== title || (titleInfo[title] && !row.querySelector('.dodo-sec-nav'))) {
          unmarkRow(row);
          markRow(row);
        }
        if (title && arrangement.indexOf(title) === -1) arrangement.push(title);
      });
      // 2) أزل من الترتيب ما لم يعد موجودًا
      arrangement = arrangement.filter(function (title) {
        return rows.some(function (row) { return row.getAttribute('data-dodo-title') === title || dodoRowTitle(row) === title; });
      });
      // 3) رتب عقد DOM حسب الترتيب المنطقي — فقط إذا اختلف الترتيب الفعلي (منع دورات طفريات بلا نهاية)
      var currentOrder = rows.map(function (row) { return row.getAttribute('data-dodo-title') || dodoRowTitle(row); });
      var needsMove = false;
      for (var d = 0; d < arrangement.length; d++) {
        if (currentOrder[d] !== arrangement[d]) { needsMove = true; break; }
      }
      if (needsMove) {
        arrangement.forEach(function (title) {
          for (var i = 0; i < rows.length; i++) {
            if (rows[i].getAttribute('data-dodo-title') === title || dodoRowTitle(rows[i]) === title) {
              cont.appendChild(rows[i]);
              return;
            }
          }
        });
      }
    }

    function cleanup() {
      if (arrObserver) { arrObserver.disconnect(); arrObserver = null; }
      clearTimeout(obsTimer);
      dodoGetBoardRows().forEach(unmarkRow);
      if (saveBar) { saveBar.remove(); saveBar = null; }
    }

    function exitEditMode(restoreOriginal) {
      editing = false;
      if (restoreOriginal && originalArrangement.length) {
        arrangement = originalArrangement.slice();
        applyArrangement();
      }
      cleanup();
      syncVisibility();
    }

    async function enterEditMode() {
      editing = true;
      syncVisibility();
      var keys = dodoBuildAddonKeysFromProfile();
      if (!keys.length) {
        showDodoToast('التطبيق قيد التحميل — انتظر ثوانٍ ثم أعد المحاولة');
        editing = false;
        syncVisibility();
        return;
      }
      // خريطة العنوان → الإضافة لكل الأقسام الحالية
      titleInfo = {};
      dodoGetBoardRows().forEach(function (row) {
        var title = dodoRowTitle(row);
        if (!title || titleInfo[title]) return;
        var m = dodoMatchTitleToAddon(title, keys);
        titleInfo[title] = m ? { url: m.url, name: m.name } : { url: null, name: '' };
      });
      arrangement = dodoGetBoardRows().map(dodoRowTitle).filter(Boolean);
      originalArrangement = arrangement.slice();
      applyArrangement();

      // مراقب: كلما أعاد React الرسم أعدنا فرض الترتيب والأدوات
      arrObserver = new MutationObserver(function () {
        if (!editing) return;
        clearTimeout(obsTimer);
        obsTimer = setTimeout(applyArrangement, 120);
      });
      arrObserver.observe(document.body, { childList: true, subtree: true });

      saveBar = document.createElement('div');
      saveBar.className = 'dodo-save-bar';
      saveBar.innerHTML =
        '<span>اسحب كل قسم إلى موضعه (أو ⬆⬇)</span>' +
        '<button type="button" class="dodo-save-btn">💾 حفظ الترتيب</button>' +
        '<button type="button" class="dodo-cancel-btn">✕ إلغاء</button>';
      document.body.appendChild(saveBar);
      saveBar.querySelector('.dodo-save-btn').addEventListener('click', function () {
        var sb = saveBar.querySelector('.dodo-save-btn');
        sb.disabled = true;
        sb.textContent = '⏳ جارٍ الحفظ...';
        try {
          // رتبب الإضافات بحسب أول ظهور لأقسامها
          var urls = [];
          arrangement.forEach(function (title) {
            var info = titleInfo[title];
            if (info && info.url && urls.indexOf(info.url) === -1) urls.push(info.url);
          });
          if (urls.length < 2) throw new Error('لم أتعرف على أقسام كافية');
          var prof = JSON.parse(localStorage.getItem('profile') || 'null');
          if (!prof || !Array.isArray(prof.addons) || prof.addons.length < 2) throw new Error('ملف الإعداد غير جاهز');
          var seen = {};
          var ordered = [];
          urls.forEach(function (u) {
            if (seen[u]) return;
            for (var i = 0; i < prof.addons.length; i++) {
              if (prof.addons[i].transportUrl === u) { seen[u] = true; ordered.push(prof.addons[i]); break; }
            }
          });
          prof.addons.forEach(function (a) {
            if (!seen[a.transportUrl]) { seen[a.transportUrl] = true; ordered.push(a); }
          });
          var changed = false;
          for (var c = 0; c < ordered.length; c++) {
            if (ordered[c] !== prof.addons[c]) { changed = true; break; }
          }
          if (!changed) {
            showDodoToast('لم يتغير ترتيب الإضافات');
            exitEditMode(false);
            return;
          }
          prof.addons = ordered;
          localStorage.setItem('profile', JSON.stringify(prof));
          try { localStorage.setItem('dodo_addon_order_v1', JSON.stringify(urls)); } catch (e2) {}
          showDodoToast('✅ تم حفظ ترتيبك — جارٍ إعادة التحميل...');
          setTimeout(function () { window.location.reload(); }, 250);
        } catch (err) {
          sb.disabled = false;
          sb.textContent = '💾 حفظ الترتيب';
          showDodoToast('تعذر الحفظ: ' + (err && err.message ? err.message : 'حاول مجددًا'));
        }
      });
      saveBar.querySelector('.dodo-cancel-btn').addEventListener('click', function () {
        exitEditMode(true);
      });
    }

    btn.addEventListener('click', function () {
      if (editing) exitEditMode(true);
      else enterEditMode();
    });
  }

  // 7. Bootstrap Privacy Check & Start DOM Observer
  function initDodoBridge() {
    applyFitMode(getFitMode());
    var headers = {};
    if (getInvite()) headers['X-Dodo-Invite'] = getInvite();

    origFetch(ORIGIN + '/api/bootstrap', { headers: headers })
      .then(function (r) { return r.json(); })
      .then(function (boot) {
        if (boot && boot.app_name) {
          appName = boot.app_name;
          document.title = boot.app_name;
        }
        if (boot && boot.authorized === false) {
          var gate = document.createElement('div');
          gate.className = 'dodo-modal-backdrop';
          gate.innerHTML =
            '<div class="dodo-modal-card" style="text-align:center;max-width:400px;">' +
            '  <img src="/static/icons/icon-192.png" style="width:72px;height:72px;border-radius:18px;margin-bottom:10px;" alt="" />' +
            '  <h2>' + appName + '</h2>' +
            '  <p style="color:#94a3b8;font-size:13.5px;">منصة خاصة — أدخل رمز الدعوة للمتابعة</p>' +
            '  <form id="dodo-gate-form">' +
            '    <input type="password" id="dodo-gate-code" class="dodo-field" style="text-align:center;margin-bottom:12px;" placeholder="رمز الدعوة..." required />' +
            '    <button type="submit" class="dodo-btn-primary" style="width:100%;">دخول</button>' +
            '  </form>' +
            '  <p id="dodo-gate-err" style="color:#fb7185;font-size:13px;display:none;margin-top:10px;"></p>' +
            '</div>';
          document.body.appendChild(gate);
          document.getElementById('dodo-gate-form').addEventListener('submit', async function (e) {
            e.preventDefault();
            var code = document.getElementById('dodo-gate-code').value.trim();
            var r = await origFetch(ORIGIN + '/api/auth/verify', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ invite_code: code }),
            });
            var d = await r.json();
            if (r.ok && d.ok) {
              localStorage.setItem(KEY_INVITE, code);
              window.location.reload();
            } else {
              var err = document.getElementById('dodo-gate-err');
              err.style.display = 'block';
              err.textContent = (d && d.error) || 'رمز الدعوة غير صحيح';
            }
          });
          return;
        }
        dodoSig = (boot && boot.media_sig) || '';
        mountDodoQuickTools();
        mountDodoSectionArranger();
      })
      .catch(function () {
        mountDodoQuickTools();
        mountDodoSectionArranger();
      });

    var syncQueued = false;
    var obs = new MutationObserver(function () {
      var h = window.location.hash || '';
      if (h.indexOf('#/player') !== 0 && h.indexOf('#/detail/') !== 0) return;
      if (syncQueued) return;
      syncQueued = true;
      window.requestAnimationFrame(function () {
        syncQueued = false;
        syncRouteClassAndEnhancements();
      });
    });
    obs.observe(document.documentElement, { childList: true, subtree: true });
    syncRouteClassAndEnhancements();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initDodoBridge);
  } else {
    initDodoBridge();
  }
})();
