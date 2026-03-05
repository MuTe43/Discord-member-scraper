<p align="center">
  <img src="assets/banner.png" alt="Server Lens" width="100%">
</p>

<p align="center">
  <strong>Self-hosted Discord member tracker with real-time gateway scraping.</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.11+-blue?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white" alt="Docker">
  <img src="https://img.shields.io/badge/self--hosted-local%20data-8B5CF6" alt="Self-Hosted">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
</p>

---

## Why Server Lens?

Discord's built-in member list only shows online users, hides behind slow scrolling, and offers zero annotation tools. **Server Lens** scrapes the *full* member list through Discord's Gateway API — including offline members — and gives you a beautiful interface to search, tag, annotate, and export your data.

| Feature | Discord Built-in | Server Lens |
|---------|:---:|:---:|
| See **all** members (including offline) | ❌ | ✅ |
| Export to CSV / JSON | ❌ | ✅ |
| Add custom tags per member | ❌ | ✅ |
| Personal notes per member | ❌ | ✅ |
| Search & filter by name | Slow scroll | ⚡ Instant |
| Sort by join date | ❌ | ✅ |
| Self-hosted, your data stays local | N/A | ✅ |

## Features

- 🔑 **Token-based auth** — paste your Discord user token to connect
- 🔭 **Full member scraping** — fetches all members via Gateway (op 14 range sweep + op 8 search sweep)
- 🤖 **Bot filtering** — automatically excludes bot accounts
- 🏷️ **Quirk tags** — add custom labels to any member (e.g. "lurker", "mod", "funny")
- 📝 **Notes** — write freeform notes per member, auto-saved
- 📦 **Export** — download member lists as CSV or JSON
- 🔄 **Re-scrape** — refresh anytime, existing tags & notes are preserved
- 🔍 **Search & filter** — instant search, sort by name/join date, filter by tags
- 🎨 **Beautiful UI** — dark theme with Discord-inspired aesthetics

## Quick Start

### 🐳 Docker (Recommended)

```bash
git clone https://github.com/YOUR_USERNAME/server-lens.git
cd server-lens
docker compose up
```

Open [http://localhost:8000](http://localhost:8000) and you're done.

### 🐍 Manual

```bash
git clone https://github.com/YOUR_USERNAME/server-lens.git
cd server-lens
pip install -r requirements.txt
cd backend
python main.py
```

Open [http://localhost:8000](http://localhost:8000) in your browser.

## How to Get Your Discord Token

1. Open Discord in your **browser** → [discord.com/app](https://discord.com/app)
2. Open DevTools → `F12`
3. Go to the **Network** tab
4. Click anything or send a message
5. Find a request to `discord.com/api`
6. In the request headers, copy the **Authorization** value

> **⚠️ Warning:** Your token grants full access to your account. Never share it. Server Lens runs 100% locally — your token is never sent to any third party.

## Architecture

```
┌──────────────────────────────────────────────┐
│                   Browser                    │
│  ┌──────────┐  ┌──────────┐  ┌───────────┐  │
│  │ index.html│  │ style.css│  │  app.js   │  │
│  └──────────┘  └──────────┘  └───────────┘  │
└──────────────────────┬───────────────────────┘
                       │ HTTP / SSE
┌──────────────────────┴───────────────────────┐
│              FastAPI Backend                 │
│  ┌──────────┐  ┌──────────┐  ┌───────────┐  │
│  │ main.py  │  │scraper.py│  │gateway.py │  │
│  │ (routes) │  │ (scrape) │  │ (session) │  │
│  └──────────┘  └──────────┘  └───────────┘  │
│  ┌──────────┐  ┌──────────────────────────┐  │
│  │models.py │  │     data.json (local)    │  │
│  └──────────┘  └──────────────────────────┘  │
└──────────────────────┬───────────────────────┘
                       │ WebSocket + REST
               ┌───────┴────────┐
               │ Discord API v9 │
               └────────────────┘
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/validate-token` | Validate token + get user's guilds |
| `POST` | `/api/scrape` | Scrape a server's member list (SSE stream) |
| `GET` | `/api/servers` | Get all scraped servers |
| `GET` | `/api/servers/{id}/members` | Get members of a server |
| `PATCH` | `/api/servers/{id}/members/{mid}` | Update quirks/notes |
| `DELETE` | `/api/servers/{id}` | Delete a server |

## Project Structure

```
server-lens/
├── backend/
│   ├── main.py          # FastAPI app + routes
│   ├── gateway.py       # Discord Gateway session manager
│   ├── scraper.py       # Member scraping logic + REST helpers
│   └── models.py        # Pydantic request models
├── frontend/
│   ├── index.html       # Page structure
│   ├── style.css        # All styles
│   └── app.js           # Application logic
├── assets/
│   └── banner.png       # Project banner
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── CONTRIBUTING.md
└── LICENSE
```

## Contributing

Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

[MIT](LICENSE) — use it, fork it, build on it.
