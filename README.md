# Server Lens

Discord member tracker with a real backend scraper.

## Setup

```bash
pip install -r requirements.txt
cd backend
python main.py
```

Then open http://localhost:8000 in your browser.

## How to get your Discord user token

1. Open Discord in your **browser** (discord.com/app)
2. Open DevTools → `F12`
3. Go to the **Network** tab
4. Send any message or click anything
5. Find a request to `discord.com/api`
6. In the request headers look for `Authorization`
7. Copy that value — that's your token

> **Warning:** Keep your token private. Anyone with it has full access to your account.

## Features

- Paste token → validates and shows your servers
- Pick any server from a dropdown → scrapes full member list via `/guilds/{id}/members`
- Bots are filtered out automatically
- Full pagination — fetches all members regardless of server size
- Real Discord avatars and display names
- Add quirk tags + notes per member
- Re-scrape anytime to refresh — existing quirks/notes are preserved
- All data stored locally in `backend/data.json`

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/validate-token` | Validate token + get user's guilds |
| POST | `/api/scrape` | Scrape a server's member list |
| GET | `/api/servers` | Get all scraped servers |
| GET | `/api/servers/{id}/members` | Get members of a server |
| PATCH | `/api/servers/{id}/members/{mid}` | Update quirks/notes |
| DELETE | `/api/servers/{id}` | Delete a server |
