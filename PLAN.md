# KidTube — Future Plan

KidTube is a kid-focused YouTube frontend: curated channels, no Shorts, simple UI, ad-free playback via Invidious (with yt-dlp fallback). This document outlines a phased roadmap for new features, with an emphasis on **safe social sharing** that keeps parents in control.

---

## Vision

A home-first video experience where kids can discover and enjoy trusted content, while parents stay confident about what is available, how long kids watch, and who they can share with.

**Principles for all future work:**

- **Parent-controlled** — new capabilities are opt-in via `config.yaml`, not on by default
- **Kid-safe by design** — no open social networks, no comments, no algorithmic rabbit holes
- **Simple UI** — large touch targets, minimal choices, readable typography
- **Config over env vars** — behavior lives in configuration files
- **Self-hosted first** — runs on a home server or LAN; no required cloud account

---

## Current State (v0)

| Area | Status |
|------|--------|
| Curated channels & playlists | ✅ |
| Shorts filter (`min_duration_seconds`) | ✅ |
| Live animal cams | ✅ |
| Personalized branding (`kid_name`) | ✅ |
| Invidious playback + yt-dlp fallback | ✅ |
| Season/playlist picker | ✅ |
| Mobile-friendly responsive UI | ✅ |

---

## Phase 1 — Personalization & Retention

Low-risk features that improve daily use without changing the trust model.

### 1.1 Favorites & Continue Watching

- **Heart / star button** on video cards and watch page
- **Continue watching** row on home page (resume where they left off)
- Persist locally (SQLite or JSON file under a data volume) — no account required
- Config: `favorites_enabled: true`, `max_continue_watching: 10`

### 1.2 Watch History (Parent View)

- Simple `/parent/history` page (PIN-protected) showing recently watched videos and durations
- Optional daily/weekly summary
- Config: `parent_pin`, `history_retention_days`

### 1.3 Search (Curated Only)

- Search box that only queries videos from configured channels/playlists
- No YouTube-wide search — prevents wandering into unapproved content
- Config: `search_enabled: true`

---

## Phase 2 — Social Sharing (Family-Safe)

Social features scoped to **trusted people**, not public social media.

### 2.1 Share a Video with Family

- **Share button** on the watch page opens a kid-friendly sheet:
  - Copy link (KidTube `/watch/…` URL, not raw YouTube)
  - Show QR code for another device on the same network
  - Optional: “Send to…” if a parent has configured share targets
- Shared links respect the same curated playback path (still ad-free via Invidious)
- Config:

```yaml
sharing:
  enabled: true
  allow_copy_link: true
  allow_qr_code: true
  # Pre-approved recipients (display name + optional contact)
  family_contacts:
    - name: "Grandma"
      method: email   # future: email, sms, or in-app
      address: "grandma@example.com"
```

### 2.2 “Watch Together” (Co-Viewing)

- Parent starts a **watch party** from their phone; kid’s TV/tablet syncs to the same timestamp
- Session scoped to LAN or a short-lived share code (no permanent rooms)
- Pause/play controlled by parent when `parent_controls_session: true`
- Useful for grandparents calling in: “Press this link and we’ll watch the same panda cam”

### 2.3 Recommend to a Sibling (Same Household)

- If multiple kid profiles exist (Phase 3), a **“Tell [sibling] about this”** button
- Delivers an in-app notification on the other profile’s home screen
- No external messaging — stays inside KidTube

### 2.4 Share Postcards (Optional, Parent-Gated)

- Generate a fun image card: thumbnail + “[Kid] loved this on KidTube!” + QR/link
- Parent must approve before anything leaves the app (export/share sheet)
- Keeps sharing playful without exposing kid data

**Explicit non-goals for social sharing:**

- No public profiles, followers, or feeds
- No integration with TikTok, Instagram, Discord, etc.
- No comments or reactions visible to strangers

---

## Phase 3 — Profiles & Parent Dashboard

### 3.1 Multiple Kid Profiles

- Switch profile from a simple avatar picker (parent PIN to add/remove profiles)
- Each profile gets its own favorites, continue watching, and optional theme color
- Config:

```yaml
profiles:
  - name: "Aurora"
    avatar: "🦄"
    theme: "purple"
  - name: "Leo"
    avatar: "🚀"
    theme: "blue"
```

### 3.2 Parent Dashboard

- Single page: channels, screen time, history, sharing activity, approve/deny share requests
- Approve new channel suggestions from kids (wishlist queue)
- Export or delete all data for a profile (privacy)

### 3.3 Screen Time & Schedules

- Daily watch limits per profile
- “Time’s up” screen with a friendly message (configurable allowed hours, e.g. no videos after 7pm)
- Config: `screen_time_minutes_per_day`, `allowed_hours`

---

## Phase 4 — Discovery & Comfort

### 4.1 “Because You Watched…” (Bounded Recommendations)

- Recommend next video **only from the same channel or playlist**, not YouTube-wide
- Optional “similar channel” picks limited to `channels` already in config

### 4.2 Channel Wishlist

- Kid taps “Ask to add a channel”; parent sees request in dashboard
- Parent adds `@handle` to `config.yaml` or approves via UI that writes config

### 4.3 Themes & Accessibility

- High-contrast mode, dyslexia-friendly font option, reduced motion
- Per-profile accent colors and optional dark mode

---

## Phase 5 — Platform & Living Room

### 5.1 Progressive Web App (PWA)

- Install to tablet/home screen, offline shell, full-screen player
- Service worker caches static assets and last-viewed thumbnails

### 5.2 TV & Casting

- Chromecast / AirPlay support from watch page
- Optional Kodi or Apple TV shortcut bookmark
- Big remote-friendly focus navigation on TV browsers

### 5.3 Docker & Ops

- Health check endpoint for reverse proxy
- Optional backup volume for favorites/history
- Documented Caddy snippet for TLS + basic auth on `/parent/*` routes

---

## Phase 6 — Advanced (Optional)

Only if there is clear demand and parent oversight remains central.

| Feature | Notes |
|---------|--------|
| Offline downloads | Parent-approved videos only; storage limits in config |
| Multi-household sync | Encrypted sync between two self-hosted instances (e.g. grandparent’s KidTube) |
| Audio-only mode | Podcast-style playback for bedtime |
| Live event reminders | “Mark Rober dropped a new video” — parent notification, not kid push spam |
| Plugin channels | Custom RSS/non-YouTube sources in config |

---

## Suggested Implementation Order

```
Phase 1.1 Favorites & Continue Watching
    ↓
Phase 2.1 Share link + QR (social sharing MVP)
    ↓
Phase 1.2 Parent history + PIN
    ↓
Phase 3.1 Multi-profile
    ↓
Phase 2.2 Watch Together
    ↓
Phase 3.3 Screen time
    ↓
Phase 5.1 PWA
    ↓
Remaining items by user feedback
```

---

## Technical Notes

- **Storage:** Start with SQLite (`/data/kidtube.db`) mounted in Docker Compose; keeps favorites, history, and profiles in one place
- **Parent auth:** PIN stored as hash in config or separate `secrets.yaml` (gitignored); session cookie for dashboard
- **Sharing URLs:** Always use KidTube routes (`/watch/{id}?channel=…`) so playback stays within the curated stack
- **API shape:** Small JSON endpoints under `/api/` (e.g. `POST /api/favorites`, `GET /api/continue-watching`) consumed by minimal vanilla JS — no heavy frontend framework required
- **Testing:** Focus on config parsing, persistence, and parent gate flows; mock Invidious in tests

---

## Open Questions

1. **Email/SMS sharing** — worth adding (via SMTP in config) or keep QR + copy-link only?
2. **Sync across devices** — is LAN-only enough, or do families need Tailscale/cloud relay?
3. **Grandparent mode** — read-only instance with a subset of channels and one-tap “send me what they’re watching”?

Feedback and priorities welcome — adjust phases based on what your household uses most.
