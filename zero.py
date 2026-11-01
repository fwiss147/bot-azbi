import os
import asyncio
import base64
import ctypes.util
import html
import json
import logging
import random
import re
import shutil
import time
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from itertools import islice

import discord
import yt_dlp
from discord import app_commands
from dotenv import load_dotenv
load_dotenv()

from spotipy.oauth2 import SpotifyClientCredentials
import spotipy

from flask import Flask
from threading import Thread

log = logging.getLogger("zero")

# إعداد خفيف بـ Flask باش يبقى السيرفر شغال 24/7 في Render كـ Web Service مجاني
app = Flask('')

@app.route('/')
def home():
    return "Bot is running 24/7!"

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        return default


TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = _env_int("GUILD_ID", 0) or None
IDLE_LEAVE_SECONDS = _env_int("IDLE_LEAVE_SECONDS", 60)
MAX_QUEUE = _env_int("MAX_QUEUE", 100)
COOKIEFILE = os.getenv("YTDL_COOKIEFILE") or None
PREFIX = (os.getenv("BOT_PREFIX") or "z!").strip() or "z!"

PANEL_BANNER_URL = os.getenv(
    "PANEL_BANNER_URL",
    "https://cdn.discordapp.com/attachments/1565743685713535106/1566127799679586366/download_1.jpg?ex=6ae769ac&is=6ae6182c&hm=f68a6e06b512fe0a4e8761ac8aa9db1d7b4a8db1854e4af4ea90f5717ff99a5d&",
)
PANEL_THUMB_URL = os.getenv(
    "PANEL_THUMB_URL",
    "https://cdn.discordapp.com/emojis/1561398290367512728.webp?size=160&animated=true",
)
PANEL_COLOR = int(os.getenv("PANEL_COLOR", "0x00A8FF"), 16)

STREAM_TTL_SECONDS = 1200

SPOTIFY_URL_RE = re.compile(
    r"https?://open\.spotify\.com/(?:intl-[a-zA-Z-]+/)?(track|album|playlist)/([a-zA-Z0-9]+)"
)

sp_client_id = os.getenv("SPOTIPY_CLIENT_ID")
spotify_client_secret = os.getenv("SPOTIPY_CLIENT_SECRET")

spotify_enabled = bool(sp_client_id and spotify_client_secret)
if spotify_enabled:
    sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
        client_id=sp_client_id,
        client_secret=spotify_client_secret
    ))

async def resolve_spotify_query(query: str) -> str:
    if spotify_enabled and ("spotify.com" in query):
        try:
            if "track/" in query:
                track_id = query.split("track/")[1].split("?")[0]
                track = sp.track(track_id)
                name = track['name']
                artists = ", ".join([artist['name'] for artist in track['artists']])
                return f"ytsearch:{name} {artists} official audio"
            elif "playlist/" in query:
                playlist_id = query.split("playlist/")[1].split("?")[0]
                playlist = sp.playlist(playlist_id)
                first_track = playlist['tracks']['items'][0]['track']
                name = first_track['name']
                artists = ", ".join([artist['name'] for artist in first_track['artists']])
                return f"ytsearch:{name} {artists} official audio"
        except Exception as e:
            log.error(f"Spotify API Error: {e}")
    return query

YTDL_BASE_OPTIONS = {
    "format": "bestaudio[acodec=opus]/bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "noprogress": True,
    "default_search": "ytsearch",
    "socket_timeout": 20,
    "source_address": "0.0.0.0",
}

FFMPEG_OPTIONS = {
    "before_options": (
        "-nostdin -reconnect 1 -reconnect_streamed 1 "
        "-reconnect_on_network_error 1 -reconnect_delay_max 5 "
        "-thread_queue_size 1024 -rw_timeout 15000000"
    ),
    "options": (
        "-vn -ar 48000 -ac 2 -af aresample=async=1:first_pts=0 -loglevel error"
    ),
}

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def ytdl_options(*, allow_playlist: bool = False, flat: bool = False) -> dict:
    options = dict(YTDL_BASE_OPTIONS)
    options["noplaylist"] = not allow_playlist
    if flat:
        options["extract_flat"] = "in_playlist"
        options["playlistend"] = MAX_QUEUE
    else:
        options["extract_flat"] = False
    if COOKIEFILE and os.path.exists(COOKIEFILE):
        options["cookiefile"] = COOKIEFILE
    return options


@dataclass
class Song:
    query: str
    title: str
    stream_url: str | None
    webpage_url: str
    duration: int
    thumbnail: str | None
    requester: discord.abc.User
    fetched_at: float = 0.0

    @property
    def length(self) -> str:
        total = int(self.duration or 0)
        if total <= 0:
            return "LIVE"
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"

    @property
    def needs_resolution(self) -> bool:
        return (
            not self.stream_url
            or self.fetched_at <= 0
            or (time.monotonic() - self.fetched_at) > STREAM_TTL_SECONDS
        )


def _fetch_text(url: str) -> str:
    req = urllib.request.Request(url, headers=HTTP_HEADERS)
    with urllib.request.urlopen(req, timeout=12) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def _resolve_spotify_single(url: str) -> str:
    clean_url = url.split("?")[0]
    try:
        page = _fetch_text(clean_url)
        title_match = re.search(r'<meta property="og:title" content="([^"]+)"', page)
        desc_match = re.search(
            r'<meta property="og:description" content="([^"]+)"', page
        )

        title = html.unescape(title_match.group(1)).strip() if title_match else ""
        artist = ""
        if desc_match:
            desc = html.unescape(desc_match.group(1))
            parts = [p.strip() for p in desc.split("·")]
            if parts:
                artist = parts[0].split("Spotify.")[-1].strip()

        if title and artist and artist.lower() not in title.lower():
            return f"ytsearch:{artist} - {title} audio"
        if title:
            return f"ytsearch:{title} audio"
    except Exception:
        pass

    oembed_url = (
        f"https://open.spotify.com/oembed?url={urllib.parse.quote(clean_url, safe='')}"
    )
    data = json.loads(_fetch_text(oembed_url))
    if data.get("title"):
        return f"ytsearch:{data['title']} audio"
    raise RuntimeError("Could not resolve Spotify track.")


def _find_track_list_in_dict(obj) -> list[dict] | None:
    if isinstance(obj, dict):
        if isinstance(obj.get("trackList"), list) and obj["trackList"]:
            return obj["trackList"]
        for val in obj.values():
            found = _find_track_list_in_dict(val)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_track_list_in_dict(item)
            if found:
                return found
    return None


def _resolve_spotify_collection(
    kind: str, item_id: str, requester: discord.abc.User, limit: int
) -> list[Song]:
    embed_url = f"https://open.spotify.com/embed/{kind}/{item_id}"
    songs: list[Song] = []
    try:
        page = _fetch_text(embed_url)
        track_list = None
        next_match = re.search(
            r'<script id="__NEXT_DATA__"[^>]*>([^<]+)</script>', page
        )
        if next_match:
            track_list = _find_track_list_in_dict(json.loads(next_match.group(1)))

        if not track_list:
            init_match = re.search(
                r'<script id="initial-state"[^>]*>([^<]+)</script>', page
            )
            if init_match:
                raw_state = init_match.group(1).strip()
                try:
                    decoded = base64.b64decode(raw_state).decode(
                        "utf-8", errors="ignore"
                    )
                    payload = json.loads(decoded)
                except Exception:
                    payload = json.loads(urllib.parse.unquote(raw_state))
                track_list = _find_track_list_in_dict(payload)

        if track_list:
            for item in track_list[:limit]:
                title = str(item.get("title") or item.get("name") or "").strip()
                subtitle = str(item.get("subtitle") or "").strip().replace("\xa0", " ")
                duration_ms = int(item.get("duration") or item.get("duration_ms") or 0)
                if not title:
                    continue
                full_title = f"{subtitle} - {title}" if subtitle else title
                songs.append(
                    Song(
                        query=f"ytsearch:{full_title} audio",
                        title=full_title,
                        stream_url=None,
                        webpage_url=f"https://open.spotify.com/{kind}/{item_id}",
                        duration=duration_ms // 1000,
                        thumbnail=None,
                        requester=requester,
                        fetched_at=0.0,
                    )
                )
    except Exception:
        log.warning("Embed scrape failed for Spotify %s/%s", kind, item_id)

    if songs:
        return songs
    raise RuntimeError("Could not load Spotify playlist.")


def _make_song(data: dict, query: str, requester: discord.abc.User) -> Song:
    stream_url = data.get("url")
    webpage = str(data.get("webpage_url") or data.get("original_url") or query)
    return Song(
        query=query,
        title=str(data.get("title") or "Unknown title"),
        stream_url=stream_url,
        webpage_url=webpage,
        duration=int(data.get("duration") or 0),
        thumbnail=data.get("thumbnail"),
        requester=requester,
        fetched_at=time.monotonic() if stream_url else 0.0,
    )


def _extract_single_song(query: str, requester: discord.abc.User) -> Song:
    search_query = query.strip()
    if SPOTIFY_URL_RE.match(search_query):
        search_query = _resolve_spotify_single(search_query)
    elif not search_query.startswith(("http://", "https://", "ytsearch:", "scsearch:")):
        search_query = f"ytsearch:{search_query}"

    with yt_dlp.YoutubeDL(ytdl_options(allow_playlist=False, flat=False)) as ydl:
        data = ydl.extract_info(search_query, download=False)

    if isinstance(data, dict) and "entries" in data:
        entries = data.get("entries") or []
        data = next(
            (entry for entry in entries if isinstance(entry, dict) and entry.get("url")),
            None,
        )
    if not isinstance(data, dict) or not data.get("url"):
        raise RuntimeError("No playable result found.")
    return _make_song(data, query, requester)


def _extract_songs_sync(
    query: str, requester: discord.abc.User, limit: int
) -> list[Song]:
    raw = query.strip()
    sp_match = SPOTIFY_URL_RE.match(raw)
    if sp_match:
        kind, item_id = sp_match.group(1), sp_match.group(2)
        if kind in {"playlist", "album"}:
            return _resolve_spotify_collection(kind, item_id, requester, limit)
        return [_extract_single_song(raw, requester)]

    is_url = raw.startswith(("http://", "https://"))
    if is_url and ("list=" in raw or "/playlist" in raw or "/sets/" in raw):
        with yt_dlp.YoutubeDL(ytdl_options(allow_playlist=True, flat=True)) as ydl:
            info = ydl.extract_info(raw, download=False)

        if isinstance(info, dict) and info.get("entries"):
            songs: list[Song] = []
            for entry in info["entries"]:
                if not isinstance(entry, dict):
                    continue
                entry_url = entry.get("url") or entry.get("webpage_url")
                entry_id = entry.get("id")
                if not entry_url and entry_id:
                    entry_url = f"https://www.youtube.com/watch?v={entry_id}"
                if not entry_url:
                    continue
                if not entry_url.startswith(("http://", "https://")):
                    entry_url = f"https://www.youtube.com/watch?v={entry_url}"

                title = str(entry.get("title") or "Queued track")
                if title in {"[Deleted video]", "[Private video]"}:
                    continue
                songs.append(
                    Song(
                        query=entry_url,
                        title=title,
                        stream_url=None,
                        webpage_url=entry_url,
                        duration=int(entry.get("duration") or 0),
                        thumbnail=entry.get("thumbnail"),
                        requester=requester,
                        fetched_at=0.0,
                    )
                )
                if len(songs) >= limit:
                    break
            if songs:
                return songs

    return [_extract_single_song(raw, requester)]


async def fetch_songs(
    query: str,
    requester: discord.abc.User,
    limit: int = MAX_QUEUE,
    timeout: float = 45,
) -> list[Song]:
    return await asyncio.wait_for(
        asyncio.to_thread(_extract_songs_sync, query, requester, limit),
        timeout=timeout,
    )


async def ensure_song_stream(song: Song, timeout: float = 45) -> None:
    target = (
        song.query
        if song.query.startswith("ytsearch:")
        else (song.webpage_url or song.query)
    )
    fresh = await asyncio.wait_for(
        asyncio.to_thread(_extract_single_song, target, song.requester),
        timeout=timeout,
    )
    song.stream_url = fresh.stream_url
    song.fetched_at = fresh.fetched_at
    if fresh.title and fresh.title != "Unknown title":
        song.title = fresh.title
    if fresh.webpage_url:
        song.webpage_url = fresh.webpage_url
    if fresh.duration:
        song.duration = fresh.duration
    if fresh.thumbnail:
        song.thumbnail = fresh.thumbnail


@dataclass
class GuildPlayer:
    queue: deque[Song] = field(default_factory=deque)
    current: Song | None = None
    loop: bool = False
    skip_loop_once: bool = False
    volume: float = 0.5
    channel_id: int | None = None
    panel_message: discord.Message | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    idle_task: asyncio.Task | None = None


class ZeroBot(discord.Client):
    def __init__(self, *, intents: discord.Intents):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        self.add_view(MusicInterfaceView())
        try:
            await self.tree.sync()
            if GUILD_ID:
                guild = discord.Object(id=GUILD_ID)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            log.info("Global and guild slash commands synced")
        except Exception:
            log.exception("Slash command sync failed")

    async def close(self) -> None:
        for guild_id in list(players.keys()):
            drop_player(guild_id)
        for task in list(background_tasks):
            task.cancel()
        await super().close()


_intents = discord.Intents.default()
_intents.message_content = True
_intents.voice_states = True
bot = ZeroBot(intents=_intents)
players: dict[int, GuildPlayer] = {}
background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return task


def get_player(guild_id: int) -> GuildPlayer:
    return players.setdefault(guild_id, GuildPlayer())


def cancel_idle_timer(player: GuildPlayer) -> None:
    if player.idle_task is not None:
        player.idle_task.cancel()
        player.idle_task = None


def start_idle_timer(guild_id: int, player: GuildPlayer) -> None:
    cancel_idle_timer(player)
    player.idle_task = _spawn(_idle_leave(guild_id))


def drop_player(guild_id: int) -> GuildPlayer | None:
    player = players.pop(guild_id, None)
    if player is not None:
        cancel_idle_timer(player)
    return player


def _clean_title(title: str, limit: int = 65) -> str:
    text = " ".join(title.split())
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text))


def _safe_url(url: str | None) -> str | None:
    if url and url.startswith(("http://", "https://")):
        return url
    return None


def build_music_interface_embed(
    guild: discord.Guild, user: discord.abc.User | None = None
) -> discord.Embed:
    player = players.get(guild.id)
    vc = guild.voice_client
    song = player.current if player else None
    mention = user.mention if user else (song.requester.mention if song else "listener")

    vol_pct = int((player.volume if player else 0.5) * 100)
    loop_str = "ON 🔁" if (player and player.loop) else "OFF"
    q_count = len(player.queue) if player else 0
    status_str = "Paused ⏸️" if (vc and vc.is_paused()) else ("Playing ▶️" if song else "Idle 💤")

    if song:
        track_line = f"**[{_clean_title(song.title)}]({_safe_url(song.webpage_url) or 'https://youtube.com'})**"
        meta_line = (
            f"⏱️ `{song.length}`  •  🔊 `{vol_pct}%`  •  🔁 `{loop_str}`  •  📜 `{q_count} in queue`"
        )
    else:
        track_line = "*No track is currently playing. Click **➕** or use `/play` to start!*"
        meta_line = f"🔊 `{vol_pct}%`  •  🔁 `{loop_str}`  •  📜 `{q_count} in queue`"

    desc = (
        "Manage your music playback and queue using the custom buttons below[cite: 7].\n"
        "──────────────────────────────────────────\n"
        f"🎧 ヾ Glad to see you, {mention}..\n\n"
        f"**Status:** `{status_str}`\n"
        f"🎵 **Track:** {track_line}\n"
        f"{meta_line}\n"
        "──────────────────────────────────────────\n"
        "Welcome to **CoolDown Music**! Custom interface active."
    )

    embed = discord.Embed(
        title="💜 CoolDown Custom Interface",
        description=desc,
        color=discord.Color(PANEL_COLOR),
    )

    thumb = _safe_url(song.thumbnail if song else None) or _safe_url(PANEL_THUMB_URL)
    if thumb:
        embed.set_thumbnail(url=thumb)

    banner = _safe_url(PANEL_BANNER_URL)
    if banner:
        embed.set_image(url=banner)

    return embed


async def refresh_panel(guild: discord.Guild, user: discord.abc.User | None = None) -> None:
    player = players.get(guild.id)
    if not player or not player.panel_message:
        return
    try:
        embed = build_music_interface_embed(guild, user)
        await player.panel_message.edit(embed=embed, view=MusicInterfaceView())
    except discord.HTTPException:
        player.panel_message = None


class AddTrackModal(discord.ui.Modal, title="Add Song or Playlist"):
    query = discord.ui.TextInput(
        label="Song Name, YouTube or Spotify Link",
        placeholder="e.g. Dancin Krono Remix or https://open.spotify.com/...",
        required=True,
        max_length=300,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await play.callback(interaction, str(self.query.value))


class MusicInterfaceView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        emoji=discord.PartialEmoji(name="shuffle", id=1566137079216210040),
        style=discord.ButtonStyle.secondary, custom_id="zm:shuffle", row=0
    )
    async def btn_shuffle(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await require_playback_control(interaction):
            return
        player = get_player(interaction.guild.id)
        if len(player.queue) < 2:
            await interaction.response.send_message(
                "Need at least 2 tracks in queue to shuffle.", ephemeral=True
            )
            return
        items = list(player.queue)
        random.shuffle(items)
        player.queue = deque(items)
        await interaction.response.edit_message(
            embed=build_music_interface_embed(interaction.guild, interaction.user), view=self
        )
        await interaction.followup.send("🔀 Queue shuffled!", ephemeral=True)

    @discord.ui.button(
        emoji=discord.PartialEmoji(name="skip", id=1566137195658616993),
        style=discord.ButtonStyle.secondary, custom_id="zm:skip", row=0
    )
    async def btn_skip(self, interaction: discord.Interaction, _: discord.ui.Button):
        await skip.callback(interaction)

    @discord.ui.button(
        emoji=discord.PartialEmoji(name="loop", id=1566137175287013417),
        style=discord.ButtonStyle.secondary, custom_id="zm:loop", row=0
    )
    async def btn_loop(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await require_playback_control(interaction):
            return
        player = get_player(interaction.guild.id)
        player.loop = not player.loop
        await interaction.response.edit_message(
            embed=build_music_interface_embed(interaction.guild, interaction.user), view=self
        )

    @discord.ui.button(
        emoji=discord.PartialEmoji(name="pause", id=1566137264235483316),
        style=discord.ButtonStyle.secondary, custom_id="zm:pause_resume", row=0
    )
    async def btn_pause_resume(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await require_playback_control(interaction):
            return
        vc = interaction.guild.voice_client
        if vc.is_paused():
            vc.resume()
            msg = "▶️ Resumed playback."
        elif vc.is_playing():
            vc.pause()
            msg = "⏸️ Paused playback."
        else:
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
            return
        await interaction.response.edit_message(
            embed=build_music_interface_embed(interaction.guild, interaction.user), view=self
        )
        await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(
        emoji=discord.PartialEmoji(name="stop", id=1566137264235483316),
        style=discord.ButtonStyle.secondary, custom_id="zm:stop", row=0
    )
    async def btn_stop(self, interaction: discord.Interaction, _: discord.ui.Button):
        await stop.callback(interaction)

    @discord.ui.button(
        emoji=discord.PartialEmoji(name="voldown", id=1566137157083472002),
        style=discord.ButtonStyle.secondary, custom_id="zm:vol_down", row=1
    )
    async def btn_vol_down(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await require_playback_control(interaction):
            return
        player = get_player(interaction.guild.id)
        player.volume = max(0.0, round(player.volume - 0.1, 2))
        vc = interaction.guild.voice_client
        if vc and getattr(vc, "source", None) and hasattr(vc.source, "volume"):
            vc.source.volume = player.volume
        await interaction.response.edit_message(
            embed=build_music_interface_embed(interaction.guild, interaction.user), view=self
        )

    @discord.ui.button(
        emoji=discord.PartialEmoji(name="volup", id=1566137139203276900),
        style=discord.ButtonStyle.secondary, custom_id="zm:vol_up", row=1
    )
    async def btn_vol_up(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await require_playback_control(interaction):
            return
        player = get_player(interaction.guild.id)
        player.volume = min(1.0, round(player.volume + 0.1, 2))
        vc = interaction.guild.voice_client
        if vc and getattr(vc, "source", None) and hasattr(vc.source, "volume"):
            vc.source.volume = player.volume
        await interaction.response.edit_message(
            embed=build_music_interface_embed(interaction.guild, interaction.user), view=self
        )

    @discord.ui.button(
        emoji=discord.PartialEmoji(name="add", id=1566137282287763566),
        style=discord.ButtonStyle.secondary, custom_id="zm:add_song", row=1
    )
    async def btn_add_song(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(AddTrackModal())

    @discord.ui.button(
        emoji=discord.PartialEmoji(name="queue", id=1566137299496865853),
        style=discord.ButtonStyle.secondary, custom_id="zm:queue", row=1
    )
    async def btn_queue(self, interaction: discord.Interaction, _: discord.ui.Button):
        await show_queue.callback(interaction)

    @discord.ui.button(
        emoji=discord.PartialEmoji(name="trash", id=1566137317452943481),
        style=discord.ButtonStyle.secondary, custom_id="zm:clear", row=1
    )
    async def btn_clear(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await require_playback_control(interaction):
            return
        player = get_player(interaction.guild.id)
        player.queue.clear()
        await interaction.response.edit_message(
            embed=build_music_interface_embed(interaction.guild, interaction.user), view=self
        )
        await interaction.followup.send("🗑️ Queue cleared.", ephemeral=True)


async def _handle_track_end(
    guild_id: int, finished_song: Song, error: Exception | None
) -> None:
    player = players.get(guild_id)
    if player is None:
        return
    if error is not None:
        player.skip_loop_once = True
    guild = bot.get_guild(guild_id)
    if guild is None:
        drop_player(guild_id)
        return
    await play_next(guild, previous_song=finished_song)


async def play_next(
    guild: discord.Guild, *, previous_song: Song | None = None
) -> None:
    player = players.get(guild.id)
    if player is None:
        return
    async with player.lock:
        if players.get(guild.id) is not player:
            return
        vc = guild.voice_client
        if vc is None or not vc.is_connected():
            return

        if previous_song is not None and player.current is not previous_song:
            return

        if vc.is_playing() or vc.is_paused():
            return

        while True:
            song: Song | None = None

            if player.loop and player.current is not None and not player.skip_loop_once:
                song = player.current
            elif player.queue:
                song = player.queue.popleft()

            player.skip_loop_once = False
            player.current = song

            if song is None:
                start_idle_timer(guild.id, player)
                await refresh_panel(guild)
                return

            if song.needs_resolution or player.loop:
                try:
                    await ensure_song_stream(song)
                except Exception:
                    log.exception("Could not resolve stream for %s", song.title)
                    player.loop = False
                    continue

            if players.get(guild.id) is not player or not vc.is_connected():
                return
            break

        cancel_idle_timer(player)
        loop = asyncio.get_running_loop()

        def after(error: Exception | None) -> None:
            if error is not None:
                log.error("Playback error in guild %s: %s", guild.id, error)
            if not loop.is_closed():
                loop.call_soon_threadsafe(
                    lambda: _spawn(_handle_track_end(guild.id, song, error))
                )

        try:
            source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(song.stream_url, **FFMPEG_OPTIONS),
                volume=player.volume,
            )
            vc.play(source, after=after)
            await refresh_panel(guild, song.requester)
        except Exception:
            log.exception("Could not start playback in guild %s", guild.id)
            player.current = None
            _spawn(play_next(guild))


async def _idle_leave(guild_id: int) -> None:
    try:
        await asyncio.sleep(IDLE_LEAVE_SECONDS)
        guild = bot.get_guild(guild_id)
        player = players.get(guild_id)
        if guild is None or player is None:
            return
        vc = guild.voice_client
        if vc is None or not vc.is_connected() or vc.channel is None:
            return

        alone = not any(not member.bot for member in vc.channel.members)
        inactive = (
            not vc.is_playing() and not vc.is_paused() and player.current is None
        )

        if not (alone or inactive):
            return

        drop_player(guild_id)
        vc.stop()
        await vc.disconnect()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Idle disconnect failed in guild %s", guild_id)


async def require_user_voice(
    interaction: discord.Interaction,
) -> discord.VoiceChannel | discord.StageChannel | None:
    state = getattr(interaction.user, "voice", None)
    channel = getattr(state, "channel", None)
    if channel is None:
        await interaction.response.send_message(
            "Join a voice channel first, then try again.", ephemeral=True
        )
        return None
    return channel


async def require_playback_control(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return False
    vc = interaction.guild.voice_client
    if vc is None or not vc.is_connected():
        await interaction.response.send_message(
            "I'm not in a voice channel.", ephemeral=True
        )
        return False
    user_channel = getattr(getattr(interaction.user, "voice", None), "channel", None)
    is_admin = getattr(interaction.user.guild_permissions, "manage_guild", False)
    if user_channel != vc.channel and not is_admin:
        await interaction.response.send_message(
            "Join my voice channel to control playback.", ephemeral=True
        )
        return False
    return True


@bot.event
async def on_ready():
    log.info("Logged in as %s (%d guilds)", bot.user, len(bot.guilds))
    activity = discord.Activity(
        type=discord.ActivityType.listening, 
        name="z!help"
    )
    await bot.change_presence(activity=activity)

@bot.event
async def on_guild_remove(guild: discord.Guild):
    drop_player(guild.id)


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
):
    guild = member.guild
    if bot.user is not None and member.id == bot.user.id and after.channel is None:
        drop_player(guild.id)
        return

    vc = guild.voice_client
    player = players.get(guild.id)
    if vc is None or player is None or not vc.is_connected() or vc.channel is None:
        return

    has_humans = any(not m.bot for m in vc.channel.members)
    is_active = vc.is_playing() or vc.is_paused() or player.current is not None

    if has_humans and is_active:
        cancel_idle_timer(player)
    elif not has_humans:
        if player.idle_task is None or player.idle_task.done():
            start_idle_timer(guild.id, player)


@bot.tree.command(
    name="panel",
    description="Send the interactive Music Interface panel",
)
@app_commands.guild_only()
async def panel_cmd(interaction: discord.Interaction):
    player = get_player(interaction.guild.id)
    player.channel_id = interaction.channel_id
    embed = build_music_interface_embed(interaction.guild, interaction.user)
    await interaction.response.send_message(embed=embed, view=MusicInterfaceView())
    try:
        player.panel_message = await interaction.original_response()
    except Exception:
        pass


@bot.tree.command(
    name="play",
    description="Play a song or playlist from YouTube or Spotify",
)
@app_commands.describe(
    query="Song name, YouTube link/playlist, or Spotify link/playlist"
)
@app_commands.guild_only()
async def play(interaction: discord.Interaction, query: str):
    channel = await require_user_voice(interaction)
    if channel is None:
        return
    await interaction.response.defer()
    
    query = await resolve_spotify_query(query)

    vc = interaction.guild.voice_client
    try:
        if vc is not None and not vc.is_connected():
            await vc.disconnect(force=True)
            vc = None
        if vc is None:
            vc = await channel.connect(timeout=30, reconnect=True, self_deaf=True)
        elif vc.channel != channel:
            if vc.is_playing() or vc.is_paused():
                has_humans = any(not m.bot for m in vc.channel.members)
                is_admin = getattr(
                    interaction.user.guild_permissions, "manage_guild", False
                )
                if has_humans and not is_admin:
                    await interaction.followup.send(
                        f"I'm already playing music in {vc.channel.mention}.",
                        ephemeral=True,
                    )
                    return
            await vc.move_to(channel)
    except Exception:
        await interaction.followup.send(
            "I couldn't join your voice channel. Check my permissions.",
            ephemeral=True,
        )
        return

    player = get_player(interaction.guild.id)
    player.channel_id = interaction.channel_id

    remaining_slots = MAX_QUEUE - len(player.queue)
    if remaining_slots <= 0:
        await interaction.followup.send(
            f"The queue is full ({MAX_QUEUE} songs).", ephemeral=True
        )
        return

    try:
        songs = await fetch_songs(query, interaction.user, limit=remaining_slots)
    except Exception:
        log.exception("Could not load songs for %r", query)
        await interaction.followup.send(
            "I couldn't find or load that. Make sure the link/playlist is public.",
            ephemeral=True,
        )
        return

    if not songs:
        await interaction.followup.send("No playable tracks found.", ephemeral=True)
        return

    for s in songs:
        player.queue.append(s)
    cancel_idle_timer(player)

    first_song = songs[0]
    await play_next(interaction.guild)

    embed = build_music_interface_embed(interaction.guild, interaction.user)
    sent = await interaction.followup.send(
        content=f"✅ Added **{len(songs)}** tracks to queue!" if len(songs) > 1 else None,
        embed=embed,
        view=MusicInterfaceView(),
        wait=True,
    )
    if isinstance(sent, discord.Message):
        player.panel_message = sent
  
@bot.tree.command(name="help", description="Show the music panel")
@app_commands.guild_only()
async def help_cmd(interaction: discord.Interaction):
    await panel_cmd.callback(interaction)

@bot.tree.command(name="pause", description="Pause playback")
@app_commands.guild_only()
async def pause(interaction: discord.Interaction):
    if not await require_playback_control(interaction):
        return
    vc = interaction.guild.voice_client
    if vc.is_playing():
        vc.pause()
        await refresh_panel(interaction.guild, interaction.user)
        await interaction.response.send_message("⏸️ Paused.", ephemeral=True)
    else:
        await interaction.response.send_message(
            "Nothing is playing right now.", ephemeral=True
        )


@bot.tree.command(name="resume", description="Resume playback")
@app_commands.guild_only()
async def resume(interaction: discord.Interaction):
    if not await require_playback_control(interaction):
        return
    vc = interaction.guild.voice_client
    if vc.is_paused():
        vc.resume()
        await refresh_panel(interaction.guild, interaction.user)
        await interaction.response.send_message("▶️ Resumed.", ephemeral=True)
    else:
        await interaction.response.send_message(
            "Playback is not paused.", ephemeral=True
        )


@bot.tree.command(name="skip", description="Skip the current song")
@app_commands.guild_only()
async def skip(interaction: discord.Interaction):
    if not await require_playback_control(interaction):
        return
    vc = interaction.guild.voice_client
    player = get_player(interaction.guild.id)

    if not (vc.is_playing() or vc.is_paused() or player.current is not None):
        await interaction.response.send_message(
            "Nothing is playing right now.", ephemeral=True
        )
        return

    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)

    player.skip_loop_once = True
    skipped_song = player.current
    vc.stop()

    await asyncio.sleep(0.15)
    if player.current is skipped_song and not vc.is_playing():
        await play_next(interaction.guild, previous_song=skipped_song)

    await refresh_panel(interaction.guild, interaction.user)

    if player.current is not None and player.current is not skipped_song:
        try:
            await interaction.followup.send(
                f"⏭️ Skipped! Now playing: **{_clean_title(player.current.title)}**",
                ephemeral=True,
            )
        except Exception:
            pass
    else:
        try:
            await interaction.followup.send("⏭️ Skipped. Queue is now empty.", ephemeral=True)
        except Exception:
            pass


@bot.tree.command(name="queue", description="Show the queue")
@app_commands.guild_only()
async def show_queue(interaction: discord.Interaction):
    player = players.get(interaction.guild.id)
    if player is None or (player.current is None and not player.queue):
        await interaction.response.send_message("The queue is empty.", ephemeral=True)
        return

    lines = []
    if player.current is not None:
        loop_badge = " 🔁" if player.loop else ""
        lines.append(
            f"**Now:** {_clean_title(player.current.title)} `[{player.current.length}]`{loop_badge}"
        )
    for index, song in enumerate(islice(player.queue, 10), 1):
        lines.append(f"`{index}.` {_clean_title(song.title)} `[{song.length}]`")
    if len(player.queue) > 10:
        lines.append(f"...and {len(player.queue) - 10} more")

    q_embed = discord.Embed(
        title="📜 Current Queue",
        description="\n".join(lines),
        color=discord.Color(PANEL_COLOR),
    )
    await interaction.response.send_message(embed=q_embed, ephemeral=True)

@bot.tree.command(
    name="join",
    description="Join your voice channel without playing music immediately",
)
@app_commands.guild_only()
async def join(interaction: discord.Interaction):
    channel = await require_user_voice(interaction)
    if channel is None:
        return
    await interaction.response.defer(ephemeral=True)

    vc = interaction.guild.voice_client
    try:
        if vc is not None and not vc.is_connected():
            await vc.disconnect(force=True)
            vc = None
        if vc is None:
            vc = await channel.connect(timeout=30, reconnect=True, self_deaf=True)
        elif vc.channel != channel:
            await vc.move_to(channel)
        
        player = get_player(interaction.guild.id)
        player.channel_id = interaction.channel_id
        await interaction.followup.send(f"🔊 Joined **{channel.name}**! Ready when you are.", ephemeral=True)
    except Exception:
        await interaction.followup.send(
            "I couldn't join your voice channel. Check my permissions.",
            ephemeral=True,
        )

@bot.tree.command(name="nowplaying", description="Show the Music Interface")
@app_commands.guild_only()
async def nowplaying(interaction: discord.Interaction):
    await panel_cmd.callback(interaction)


@bot.tree.command(name="loop", description="Toggle looping the current song")
@app_commands.guild_only()
async def loop(interaction: discord.Interaction):
    if not await require_playback_control(interaction):
        return
    player = get_player(interaction.guild.id)
    player.loop = not player.loop
    state = "ON" if player.loop else "OFF"
    await refresh_panel(interaction.guild, interaction.user)
    await interaction.response.send_message(f"🔁 Loop is now **{state}**.", ephemeral=True)


@bot.tree.command(name="shuffle", description="Shuffle the queue")
@app_commands.guild_only()
async def shuffle(interaction: discord.Interaction):
    if not await require_playback_control(interaction):
        return
    player = get_player(interaction.guild.id)
    if player is None or len(player.queue) < 2:
        await interaction.response.send_message(
            "I need at least two queued songs to shuffle.", ephemeral=True
        )
        return
    items = list(player.queue)
    random.shuffle(items)
    player.queue = deque(items)
    await refresh_panel(interaction.guild, interaction.user)
    await interaction.response.send_message("🔀 Queue shuffled.", ephemeral=True)


@bot.tree.command(name="volume", description="Set the volume (0-100)")
@app_commands.describe(level="Volume from 0 to 100")
@app_commands.guild_only()
async def volume(
    interaction: discord.Interaction, level: app_commands.Range[int, 0, 100]
):
    if not await require_playback_control(interaction):
        return
    player = get_player(interaction.guild.id)
    player.volume = level / 100
    vc = interaction.guild.voice_client
    source = getattr(vc, "source", None) if vc is not None else None
    if source is not None and hasattr(source, "volume"):
        source.volume = player.volume
    await refresh_panel(interaction.guild, interaction.user)
    await interaction.response.send_message(
        f"🔊 Volume set to **{level}%**.", ephemeral=True
    )


@bot.tree.command(
    name="stop", description="Stop playback, clear the queue and leave"
)
@app_commands.guild_only()
async def stop(interaction: discord.Interaction):
    if not await require_playback_control(interaction):
        return
    vc = interaction.guild.voice_client
    player = players.get(interaction.guild.id)
    panel_msg = player.panel_message if player else None
    drop_player(interaction.guild.id)
    vc.stop()
    await interaction.response.send_message(
        "⏹️ Stopped and left the voice channel.", ephemeral=True
    )
    try:
        await vc.disconnect()
    except Exception:
        pass
    if panel_msg:
        try:
            await panel_msg.edit(
                embed=build_music_interface_embed(interaction.guild, interaction.user),
                view=MusicInterfaceView(),
            )
        except Exception:
            pass


class _PrefixResponse:
    def __init__(self, message: discord.Message):
        self._message = message
        self._done = False
        self.last_sent: discord.Message | None = None

    def is_done(self) -> bool:
        return self._done

    async def defer(
        self, *, ephemeral: bool = False, thinking: bool = False
    ) -> None:
        self._done = True

    async def send_message(
        self,
        content: str | None = None,
        *,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
        ephemeral: bool = False,
        **kwargs,
    ) -> discord.Message | None:
        self._done = True
        self.last_sent = await _reply(
            self._message, content, embed=embed, view=view, **kwargs
        )
        return self.last_sent


class _PrefixFollowup:
    def __init__(self, message: discord.Message):
        self._message = message

    async def send(
        self,
        content: str | None = None,
        *,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
        ephemeral: bool = False,
        wait: bool = False,
        **kwargs,
    ) -> discord.Message | None:
        return await _reply(
            self._message, content, embed=embed, view=view, **kwargs
        )


async def _reply(
    message: discord.Message,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
    **kwargs,
) -> discord.Message | None:
    send_kwargs = dict(kwargs)
    if embed is not None:
        send_kwargs["embed"] = embed
    if view is not None:
        send_kwargs["view"] = view
    try:
        return await message.reply(content, mention_author=False, **send_kwargs)
    except discord.HTTPException:
        pass
    try:
        return await message.channel.send(content, **send_kwargs)
    except discord.HTTPException:
        return None


class _PrefixInteraction:
    def __init__(self, message: discord.Message):
        self.message = message
        self.user = message.author
        self.guild = message.guild
        self.guild_id = message.guild.id
        self.channel = message.channel
        self.channel_id = message.channel.id
        self.command = None
        self.response = _PrefixResponse(message)
        self.followup = _PrefixFollowup(message)

    async def original_response(self) -> discord.Message | None:
        return self.response.last_sent


PREFIX_COMMANDS = {
    "panel": panel_cmd.callback,
    "play": play.callback,
    "join": join.callback,
    "pause": pause.callback,
    "resume": resume.callback,
    "skip": skip.callback,
    "queue": show_queue.callback,
    "nowplaying": nowplaying.callback,
    "loop": loop.callback,
    "shuffle": shuffle.callback,
    "volume": volume.callback,
    "stop": stop.callback,
}
PREFIX_ALIASES = {
    "menu": "panel",
    "ui": "panel",
    "interface": "panel",
    "p": "play",
    "next": "skip",
    "s": "skip",
    "q": "queue",
    "np": "nowplaying",
    "now": "nowplaying",
    "vol": "volume",
    "v": "volume",
    "leave": "stop",
    "dc": "stop",
    "h": "help",
    "hel": "help",
    "commands": "help",
    "prefix": "help",
}


def _parse_prefix(content: str) -> tuple[str, str] | None:
    text = content.lstrip()
    if not text.lower().startswith(PREFIX.lower()):
        return None
    body = text[len(PREFIX) :].strip()
    if not body:
        return None
    name, _, args = body.partition(" ")
    return name.lower(), args.strip()


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return
    content = message.content.strip()

    if bot.user and content in {f"<@{bot.user.id}>", f"<@!{bot.user.id}>"}:
        ctx = _PrefixInteraction(message)
        await panel_cmd.callback(ctx)
        return

    parsed = _parse_prefix(content)
    if parsed is None:
        return
    name, args = parsed
    command_name = PREFIX_ALIASES.get(name, name)

    if command_name == "help":
        ctx = _PrefixInteraction(message)
        await panel_cmd.callback(ctx)
        return

    handler = PREFIX_COMMANDS.get(command_name)
    if handler is None:
        return

    context = _PrefixInteraction(message)
    try:
        if command_name == "play":
            if not args:
                await panel_cmd.callback(context)
                return
            await handler(context, args)
        elif command_name == "volume":
            try:
                level = int(args.split()[0])
            except (IndexError, ValueError):
                await _reply(message, f"Usage: `{PREFIX}volume <0-100>`")
                return
            if not 0 <= level <= 100:
                await _reply(message, "Volume must be between 0 and 100.")
                return
            await handler(context, level)
        else:
            await handler(context)
    except Exception:
        log.exception("Prefix command %s failed in guild %s", name, message.guild.id)


def _load_opus() -> None:
    if discord.opus.is_loaded():
        return
    try:
        discord.opus._load_default()
        if discord.opus.is_loaded():
            return
    except Exception:
        pass
    for lib_name in ("opus", "libopus.so.0", "libopus.0.dylib", "libopus-0.dll"):
        found = ctypes.util.find_library(lib_name) or lib_name
        try:
            discord.opus.load_opus(found)
            return
        except Exception:
            continue


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is missing in .env.")
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is not installed or not on PATH.")
    _load_opus()
    
    # تشغيل نظام البقاء نشطاً لـ Flask
    keep_alive()
    
    bot.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
