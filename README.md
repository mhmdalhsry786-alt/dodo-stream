---
title: Dodo Stream
emoji: 🎬
colorFrom: purple
colorTo: indigo
sdk: docker
app_port: 7860
pinned: true
---

# 🎬 Dodo Stream (دودو ستريم)

> **Copyright © 2026 [mhmdalhsry786-alt](https://github.com/mhmdalhsry786-alt)** — released under the **GNU GPL v3** license (see `LICENSE`).

A self-hosted, invite-only streaming platform with your **own brand**, built on the Stremio protocol and tuned for **iPhone / iPad, Android and desktop** browsers — no app-store install needed.

## ✨ Highlights
- **Arabic-dubbed cartoons & Disney memories:** 250+ series, 14,000+ dubbed episodes (MBC3, Nickelodeon Arabic, Cartoon Network Arabic, Spacetoon) + Disney movies as **HLS** that plays **natively on iPhone**.
- **Movies, series & anime:** via Cinemeta, Anime Kitsu and dubbed/Arabic catalogs.
- **Instant Arabic subtitles for iPhone:** automatic `SRT`/`ASS` → **WebVTT UTF-8** converter (OpenSubtitles, SubDL, local files) so Arabic subs render inside iOS fullscreen playback.
- **Live FFmpeg remux:** multi-audio `MKV` → `MP4/AAC` with the Arabic track, plus one-tap **Outplayer** / **VLC** buttons.
- **Stremio addon support:** paste any `manifest.json` URL to install addons for all your friends.

## 🚀 Run it

```bash
# Any machine with Python 3.10+ (works on 3.14)
python dodo_server.py          # → http://127.0.0.1:18880
```

Docker / cloud (24⁄7, laptop off):
```bash
docker build -t dodo-stream . && docker run -p 7860:7860 dodo-stream
```

### Optional environment variables
- `DODO_INVITE_CODE` — invite code for friends (auto-generated on first run).
- `DODO_ADMIN_PIN` — owner PIN for managing global addons (auto-generated on first run).
- `DODO_PRIVACY_MODE` — `always` or `internet_only`.

## 📄 License & credits
- **Copyright © 2026 mhmdalhsry786-alt — GNU GPL v3.** Keep the notice, share modifications under the same license.
- Stremio Web (GPL-2.0) is downloaded at runtime from its official source and remains under its own license; its code is not redistributed here.

**Disclaimer:** this is a technical tool you run yourself; it hosts no content. You are solely responsible for how you use it and for the external sources you add.
