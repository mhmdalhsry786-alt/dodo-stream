/* Dodo Stream — Official Stremio v5 WASM Worker Bridge (GPL-2.0) — Turbo Startup v4 */
(function () {
  if (typeof WorkerGlobalScope === 'undefined' || !(self instanceof WorkerGlobalScope)) {
    return;
  }

  var ORIGIN = self.location.origin;
  var dodoInvite = '';
  var dodoSig = '';
  var origFetch = self.fetch.bind(self);

  // Grab sig/invite passed via worker URL query if available so 0ms startup requests are already signed!
  try {
    var wSearch = new URL(self.location.href).searchParams;
    dodoSig = wSearch.get('sig') || '';
    dodoInvite = wSearch.get('invite') || '';
  } catch (e) {}

  function addSigAndInvite(urlStr, initObj) {
    var u = urlStr;
    if (dodoSig && u.indexOf('sig=') === -1) {
      u += (u.indexOf('?') === -1 ? '?' : '&') + 'sig=' + encodeURIComponent(dodoSig);
    }
    var nextInit = initObj ? Object.assign({}, initObj) : {};
    if (dodoInvite) {
      var hdrs = new Headers(nextInit.headers || {});
      hdrs.set('X-Dodo-Invite', dodoInvite);
      nextInit.headers = hdrs;
    }
    return { url: u, init: nextInit };
  }

  function fetchWithTimeout(url, init, ms) {
    if (typeof AbortController === 'undefined') return self.fetch(url, init);
    var ctrl = new AbortController();
    var timer = setTimeout(function () {
      try { ctrl.abort(); } catch (e) {}
    }, ms || 3500);
    var merged = Object.assign({}, init || {}, { signal: ctrl.signal });
    return self.fetch(url, merged).finally(function () {
      clearTimeout(timer);
    });
  }

  self.fetch = function (input, init) {
    try {
      var rawUrl = typeof input === 'string' ? input : input && input.url ? input.url : String(input);
      var method = ((init && init.method) || (typeof Request !== 'undefined' && input instanceof Request && input.method) || 'GET').toUpperCase();

      // 1. Route local Stremio Streaming Server (127.0.0.1:11470) through Dodo's /stremio-engine/ bridge
      if (/^https?:\/\/(127\.0\.0\.1|localhost):11470(\/|$)/i.test(rawUrl)) {
        var subPath = rawUrl.replace(/^https?:\/\/(127\.0\.0\.1|localhost):11470\/?/i, '');
        var engineTarget = ORIGIN + '/stremio-engine/' + subPath;
        var wrappedEng = addSigAndInvite(engineTarget, init);
        return origFetch(wrappedEng.url, wrappedEng.init);
      }

      // 2. Same-origin Dodo routes: attach invite/sig automatically
      if (rawUrl.indexOf(ORIGIN + '/') === 0 || rawUrl.charAt(0) === '/') {
        var absLocal = rawUrl.charAt(0) === '/' ? ORIGIN + rawUrl : rawUrl;
        if (
          absLocal.indexOf(ORIGIN + '/addons/') === 0 ||
          absLocal.indexOf(ORIGIN + '/api/') === 0 ||
          absLocal.indexOf(ORIGIN + '/stremio-engine/') === 0
        ) {
          var wrappedLocal = addSigAndInvite(absLocal, init);
          return origFetch(wrappedLocal.url, wrappedLocal.init);
        }
        return origFetch(input, init);
      }

      // 3. External Addon Requests:
      //    - Direct global CDN fetch FIRST for standard HTTPS v3 addons (10x faster on phone, zero home-upload bottleneck!)
      //    - Automatic fallback to Dodo's Universal /api/addon-proxy ONLY for plain HTTP, legacy /stremio/v1, M3U, or CORS failures!
      if (/^https?:\/\//i.test(rawUrl) && method === 'GET') {
        var parsedHost = '';
        try {
          parsedHost = new URL(rawUrl).hostname.toLowerCase();
        } catch (e) {}
        if (
          parsedHost === 'api.strem.io' ||
          parsedHost === 'link.stremio.com' ||
          parsedHost === 'www.strem.io' ||
          parsedHost === 'www.stremio.com'
        ) {
          return origFetch(input, init);
        }
        var needsProxyFirst =
          rawUrl.indexOf('http://') === 0 ||
          rawUrl.indexOf('/stremio/v1') !== -1 ||
          /\.m3u8?(\?|$)/i.test(rawUrl);
        if (needsProxyFirst) {
          var proxImmediate = addSigAndInvite(ORIGIN + '/api/addon-proxy?url=' + encodeURIComponent(rawUrl), init);
          return origFetch(proxImmediate.url, proxImmediate.init);
        }
        return origFetch(input, init).catch(function () {
          var proxFallback = addSigAndInvite(ORIGIN + '/api/addon-proxy?url=' + encodeURIComponent(rawUrl), init);
          return origFetch(proxFallback.url, proxFallback.init);
        });
      }
    } catch (err) {}
    return origFetch(input, init);
  };

  async function seedDodoIntoStremioCore() {
    if (typeof self.getState !== 'function' || typeof self.dispatch !== 'function') return;
    try {
      if (!dodoInvite && typeof self.local_storage_get_item === 'function') {
        dodoInvite = (await self.local_storage_get_item('dodo_invite_code')) || '';
      }
    } catch (e) {}

    var bootRes = await origFetch(ORIGIN + '/api/bootstrap', {
      headers: dodoInvite ? { 'X-Dodo-Invite': dodoInvite } : {},
    });
    if (!bootRes.ok) return;
    var boot = await bootRes.json();
    if (!boot || !boot.authorized) return;
    if (boot.media_sig) dodoSig = boot.media_sig;

    var ctx = await self.getState('ctx');
    var profile = (ctx && ctx.profile) || {};
    var curSettings = profile.settings || {};
    var engineBaseUrl = ORIGIN + '/';

    var alreadySeeded = '';
    try {
      alreadySeeded = (await self.local_storage_get_item('dodo_stremio_v5_seeded_v2')) || '';
    } catch (e) {}

    try {
      if (curSettings.streamingServerUrl !== engineBaseUrl || !alreadySeeded) {
        self.dispatch({ action: 'Ctx', args: { action: 'AddServerUrl', args: engineBaseUrl } }, 'ctx', '#/');
        var nextSettings = Object.assign({}, curSettings, {
          streamingServerUrl: engineBaseUrl,
          streamingServerWarningDismissed: '2099-01-01T00:00:00.000Z',
        });
        if (!alreadySeeded) {
          nextSettings.interfaceLanguage = 'ar-AR';
          nextSettings.subtitlesLanguage = 'ara';
          nextSettings.audioLanguage = 'ara';
          nextSettings.bingeWatching = true;
        }
        self.dispatch({ action: 'Ctx', args: { action: 'UpdateSettings', args: nextSettings } }, 'ctx', '#/');
      }
    } catch (e) {}

    var installedAddons = Array.isArray(profile.addons) ? profile.addons : [];
    var installedIds = {};
    var installedUrls = {};
    for (var i = 0; i < installedAddons.length; i++) {
      var ad = installedAddons[i];
      if (ad && ad.manifest && ad.manifest.id) installedIds[ad.manifest.id] = true;
      if (ad && ad.transportUrl) installedUrls[ad.transportUrl] = true;
    }

    var defaults = Array.isArray(boot.default_addons) ? boot.default_addons : [];
    var missing = [];
    for (var j = 0; j < defaults.length; j++) {
      var item = defaults[j];
      if (!item || !item.transportUrl) continue;
      var tUrl = item.transportUrl.charAt(0) === '/' ? ORIGIN + item.transportUrl : item.transportUrl;
      if ((item.id && installedIds[item.id]) || installedUrls[tUrl]) continue;
      // Skip dead localhost port 8790 when opened remotely unless already embedded
      if (/^http:\/\/127\.0\.0\.1:8790\b/i.test(tUrl)) continue;
      missing.push({ item: item, tUrl: tUrl });
    }

    // Install missing addons in PARALLEL (0ms if item.manifest is already bundled, max 3.5s timeout otherwise!)
    if (missing.length > 0) {
      await Promise.allSettled(
        missing.map(async function (entry) {
          var mf = entry.item.manifest;
          if (!mf || !mf.id || !mf.name) {
            var mfResp = await fetchWithTimeout(entry.tUrl, {}, 3500);
            if (!mfResp.ok) return;
            mf = await mfResp.json();
          }
          if (!mf || !mf.id || !mf.name || installedIds[mf.id]) return;
          installedIds[mf.id] = true;
          self.dispatch(
            {
              action: 'Ctx',
              args: {
                action: 'InstallAddon',
                args: {
                  transportUrl: entry.tUrl,
                  manifest: mf,
                  flags: { official: false, protected: false },
                },
              },
            },
            'ctx',
            '#/'
          );
        })
      );
    }

    if (!alreadySeeded && typeof self.local_storage_set_item === 'function') {
      try {
        await self.local_storage_set_item('dodo_stremio_v5_seeded_v2', '1');
      } catch (e) {}
    }
  }

  self.addEventListener('message', function (ev) {
    var d = ev && ev.data;
    if (!d || !d.__dodoCmd) return;
    if (d.__dodoCmd === 'installAddon' && d.addon && typeof self.dispatch === 'function') {
      try {
        var tUrl = d.addon.transportUrl;
        if (tUrl.charAt(0) === '/') tUrl = ORIGIN + tUrl;
        self.dispatch(
          {
            action: 'Ctx',
            args: {
              action: 'InstallAddon',
              args: {
                transportUrl: tUrl,
                manifest: d.addon.manifest,
                flags: { official: false, protected: false },
              },
            },
          },
          'ctx',
          '#/'
        );
        self.postMessage({ __dodoReply: d.reqId, ok: true });
      } catch (err) {
        self.postMessage({ __dodoReply: d.reqId, ok: false, error: String(err) });
      }
    } else if (d.__dodoCmd === 'encodeStream' && d.stream && typeof self.encodeStream === 'function') {
      try {
        var encoded = self.encodeStream(d.stream);
        self.postMessage({ __dodoReply: d.reqId, ok: true, encoded: encoded });
      } catch (err) {
        self.postMessage({ __dodoReply: d.reqId, ok: false, error: String(err) });
      }
    }
  });

  self.__dodoWrapWorkerInit = function () {
    if (typeof self.init !== 'function' || self.init.__dodoWrapped) return;
    var origInit = self.init;
    var wrapped = async function () {
      var out = await origInit.apply(this, arguments);
      // Do NOT block self.init()! Render the UI immediately in 0ms and sync Dodo addons in background!
      setTimeout(function () {
        seedDodoIntoStremioCore().catch(function () {});
      }, 0);
      return out;
    };
    wrapped.__dodoWrapped = true;
    self.init = wrapped;
  };
})();
