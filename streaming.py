#!/usr/bin/env python3
"""
Discord Audio Streaming Bot
A lightweight bot for streaming audio files to Discord voice channels
"""

import discord
from discord.ext import commands
import asyncio
import os
import sys
import json
import subprocess
import atexit
import time
import uuid
import requests
import shutil
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
try:
    load_dotenv()
except:
    print("WARNING: Skipping .env file due to encoding issues")

# Windows default console encoding (e.g. cp1252) cannot encode emoji in print(); fix before any emoji output.
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# Audio streaming utilities

def get_ffmpeg_path():
    """Get the full path to FFmpeg executable"""
    return r"C:\Users\Jasper\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg.Essentials_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.0-essentials_build\bin\ffmpeg.exe"

# FFmpeg options for audio playback - Optimized for Discord and AAC files
ffmpeg_options = {
    'before_options': '',
    'options': '-vn -f s16le -ar 48000 -ac 2 -acodec pcm_s16le -loglevel warning'
}

# Bot setup - completely generic
# Enable ALL necessary intents explicitly
intents = discord.Intents.default()
intents.message_content = True  # Required to read message content
intents.voice_states = True      # Required for voice channel operations
intents.guild_messages = True    # Required to receive messages in servers
intents.guilds = True            # Required to see guilds/servers
intents.members = False          # Not needed, but can enable if needed

print(f"🔧 Intents configured:")
print(f"   message_content: {intents.message_content}")
print(f"   voice_states: {intents.voice_states}")
print(f"   guild_messages: {intents.guild_messages}")
print(f"   guilds: {intents.guilds}")

bot = commands.Bot(command_prefix='!', intents=intents, description="File streaming utility")

# Directories for communication with background service
MEDIA_DIR = Path("media_library")
QUEUE_DIR = Path("request_queue")
MEDIA_DIR.mkdir(exist_ok=True)
QUEUE_DIR.mkdir(exist_ok=True)

# Background audio processor (audio_processor_service.py) — spawned automatically unless already running
_audio_processor_child: subprocess.Popen | None = None


def _project_root() -> Path:
    return Path(__file__).resolve().parent


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            return str(pid) in (r.stdout or "")
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _audio_processor_already_running() -> bool:
    pid_file = _project_root() / "request_queue" / ".audio_processor.pid"
    if not pid_file.exists():
        return False
    try:
        return _pid_exists(int(pid_file.read_text().strip()))
    except (ValueError, OSError):
        return False


def _ensure_audio_processor_subprocess() -> None:
    """Start queue consumer unless GONDOR_SKIP_AUDIO_PROCESSOR=1 or another instance is alive."""
    if os.environ.get("GONDOR_SKIP_AUDIO_PROCESSOR", "").strip() in ("1", "true", "yes"):
        print("⚠️ Skipping audio processor spawn (GONDOR_SKIP_AUDIO_PROCESSOR)")
        return
    if _audio_processor_already_running():
        print("🎵 Audio processor already running (shared queue)")
        return
    global _audio_processor_child
    script = _project_root() / "audio_processor_service.py"
    print("🎵 Starting background audio processor...")
    _audio_processor_child = subprocess.Popen(
        [sys.executable, str(script)],
        cwd=str(_project_root()),
    )
    time.sleep(1.5)
    if _audio_processor_child.poll() is not None:
        if _audio_processor_child.returncode == 0:
            print("🎵 Audio processor singleton: another worker is active")
        else:
            print(f"❌ Audio processor exited immediately (code {_audio_processor_child.returncode})")
    else:
        print("✅ Audio processor running")


def _terminate_audio_processor_child() -> None:
    global _audio_processor_child
    if _audio_processor_child is None or _audio_processor_child.poll() is not None:
        return
    try:
        _audio_processor_child.terminate()
        _audio_processor_child.wait(timeout=8)
    except Exception:
        try:
            _audio_processor_child.kill()
        except Exception:
            pass


atexit.register(_terminate_audio_processor_child)

# Track current playback
current_sources = {}

# Playlist management directory
PLAYLIST_DIR = Path("playlists")
PLAYLIST_DIR.mkdir(exist_ok=True)

# Playlist queue management with smart features
playlist_queues = {}  # {guild_id: {"playlist_name": str, "songs": list, "original_songs": list, "current_index": int, "shuffle": bool, "loop_mode": str, "total_songs": int}}

# Smart queue system for individual !play songs
song_queues = {}  # {guild_id: [{"title": str, "file_path": str, "duration": int, "requested_by": User, "cached": bool}, ...]}
# Next after_playing() after voice_client.stop() must continue the playlist (not !play song_queues)
playlist_force_after_stop = set()
notification_channels = {}  # {guild_id: channel} - tracks where to send music notifications

# Voice channel state management
VOICE_STATE_FILE = Path("voice_state.json")
PLAYBACK_STATE_FILE = Path("playback_state.json")

def set_notification_channel(guild_id, channel):
    """Set the notification channel for a guild"""
    notification_channels[guild_id] = channel

def get_notification_channel(guild_id):
    """Get the notification channel for a guild"""
    return notification_channels.get(guild_id)

def save_voice_state():
    """Save current voice channel connections to file"""
    try:
        voice_state = {}
        
        for guild in bot.guilds:
            if guild.voice_client and guild.voice_client.channel:
                voice_state[str(guild.id)] = {
                    'channel_id': guild.voice_client.channel.id,
                    'channel_name': guild.voice_client.channel.name,
                    'guild_name': guild.name,
                    'saved_time': time.time()
                }
        
        with open(VOICE_STATE_FILE, 'w') as f:
            json.dump(voice_state, f, indent=2)
        
        print(f"💾 Saved voice state for {len(voice_state)} guilds")
        return True
        
    except Exception as e:
        print(f"⚠️ Error saving voice state: {e}")
        return False

def load_voice_state():
    """Load saved voice channel connections"""
    try:
        if VOICE_STATE_FILE.exists():
            with open(VOICE_STATE_FILE, 'r') as f:
                voice_state = json.load(f)
            
            # Clean up old state files (older than 1 hour)
            current_time = time.time()
            cleaned_state = {}
            
            for guild_id, state in voice_state.items():
                if current_time - state.get('saved_time', 0) < 3600:  # 1 hour
                    cleaned_state[guild_id] = state
            
            print(f"📂 Loaded voice state for {len(cleaned_state)} guilds")
            return cleaned_state
        
        return {}
        
    except Exception as e:
        print(f"⚠️ Error loading voice state: {e}")
        return {}

def clear_voice_state():
    """Clear saved voice state file"""
    try:
        if VOICE_STATE_FILE.exists():
            VOICE_STATE_FILE.unlink()
            print("🧹 Cleared voice state file")
    except Exception as e:
        print(f"⚠️ Error clearing voice state: {e}")

async def rejoin_voice_channels():
    """Rejoin voice channels after restart"""
    voice_state = load_voice_state()
    
    if not voice_state:
        print("📢 No voice channels to rejoin")
        return
    
    rejoined_count = 0
    
    for guild_id_str, state in voice_state.items():
        try:
            guild_id = int(guild_id_str)
            guild = bot.get_guild(guild_id)
            
            if not guild:
                print(f"⚠️ Guild {state['guild_name']} not found, skipping")
                continue
            
            # Check if already connected
            if guild.voice_client:
                print(f"✅ Already connected to voice in {guild.name}")
                continue
            
            # Find the voice channel
            channel = guild.get_channel(state['channel_id'])
            
            if not channel:
                print(f"⚠️ Voice channel {state['channel_name']} not found in {guild.name}")
                continue
            
            # Check permissions
            if not channel.permissions_for(guild.me).connect:
                print(f"❌ No permission to connect to {channel.name} in {guild.name}")
                continue
            
            # Connect to voice channel
            await channel.connect()
            print(f"🔊 Rejoined voice channel: {channel.name} in {guild.name}")
            rejoined_count += 1
            
            # Small delay between connections
            await asyncio.sleep(1)
            
        except Exception as e:
            print(f"❌ Error rejoining {state.get('channel_name', 'unknown')} in {state.get('guild_name', 'unknown')}: {e}")
    
    if rejoined_count > 0:
        print(f"✅ Successfully rejoined {rejoined_count} voice channels")
        # Clear the state file after successful rejoin
        clear_voice_state()
    else:
        print("📢 No voice channels were rejoined")

def save_playback_state():
    """Save current playback state and queues"""
    try:
        playback_state = {}
        
        for guild in bot.guilds:
            guild_id = guild.id
            
            # Check if bot is in voice channel and something is playing
            if guild.voice_client and (guild.voice_client.is_playing() or guild.voice_client.is_paused()):
                current_source = current_sources.get(guild_id)
                
                if current_source and hasattr(current_source, 'title'):
                    guild_state = {
                        'voice_channel_id': guild.voice_client.channel.id,
                        'voice_channel_name': guild.voice_client.channel.name,
                        'guild_name': guild.name,
                        'current_song': {
                            'title': current_source.title,
                            'file_path': current_source.file_path,
                            'duration': current_source.duration
                        },
                        'was_playing': guild.voice_client.is_playing(),
                        'was_paused': guild.voice_client.is_paused(),
                        'saved_time': time.time()
                    }
                    
                    # Save song queue if it exists
                    if guild_id in song_queues and song_queues[guild_id]:
                        guild_state['song_queue'] = []
                        for song in song_queues[guild_id]:
                            # Convert User object to ID for JSON serialization
                            song_data = song.copy()
                            if hasattr(song_data.get('requested_by'), 'id'):
                                song_data['requested_by_id'] = song_data['requested_by'].id
                                song_data['requested_by_name'] = song_data['requested_by'].name
                                del song_data['requested_by']
                            guild_state['song_queue'].append(song_data)
                    
                    # Save playlist state if active
                    if guild_id in playlist_queues:
                        playlist_state = playlist_queues[guild_id].copy()
                        # Convert any non-serializable objects
                        guild_state['playlist_state'] = playlist_state
                    
                    # Save notification channel
                    if guild_id in notification_channels:
                        guild_state['notification_channel_id'] = notification_channels[guild_id].id
                    
                    playback_state[str(guild_id)] = guild_state
        
        if playback_state:
            with open(PLAYBACK_STATE_FILE, 'w') as f:
                json.dump(playback_state, f, indent=2)
            
            print(f"💾 Saved playback state for {len(playback_state)} guilds")
            return True
        else:
            print("📢 No active playback to save")
            return False
        
    except Exception as e:
        print(f"⚠️ Error saving playback state: {e}")
        return False

def load_playback_state():
    """Load saved playback state"""
    try:
        if PLAYBACK_STATE_FILE.exists():
            with open(PLAYBACK_STATE_FILE, 'r') as f:
                playback_state = json.load(f)
            
            # Clean up old state files (older than 1 hour)
            current_time = time.time()
            cleaned_state = {}
            
            for guild_id, state in playback_state.items():
                if current_time - state.get('saved_time', 0) < 3600:  # 1 hour
                    cleaned_state[guild_id] = state
            
            print(f"📂 Loaded playback state for {len(cleaned_state)} guilds")
            return cleaned_state
        
        return {}
        
    except Exception as e:
        print(f"⚠️ Error loading playback state: {e}")
        return {}

def clear_playback_state():
    """Clear saved playback state file"""
    try:
        if PLAYBACK_STATE_FILE.exists():
            PLAYBACK_STATE_FILE.unlink()
            print("🧹 Cleared playback state file")
    except Exception as e:
        print(f"⚠️ Error clearing playback state: {e}")

async def restore_playback_state():
    """Restore playback state after restart"""
    playback_state = load_playback_state()
    
    if not playback_state:
        print("📢 No playback state to restore")
        return
    
    restored_count = 0
    
    for guild_id_str, state in playback_state.items():
        try:
            guild_id = int(guild_id_str)
            guild = bot.get_guild(guild_id)
            
            if not guild:
                print(f"⚠️ Guild {state['guild_name']} not found, skipping playback restore")
                continue
            
            # Check if bot is in voice channel (should be from auto-rejoin)
            if not guild.voice_client:
                print(f"⚠️ Not in voice channel for {guild.name}, skipping playback restore")
                continue
            
            voice_client = guild.voice_client
            current_song = state.get('current_song')
            
            if not current_song or not current_song.get('file_path'):
                print(f"⚠️ No valid current song for {guild.name}")
                continue
            
            # Check if the file still exists
            if not Path(current_song['file_path']).exists():
                print(f"⚠️ Song file no longer exists: {current_song['title']}")
                continue
            
            print(f"🎵 Restoring playback in {guild.name}: {current_song['title']}")
            
            # Restore notification channel
            if state.get('notification_channel_id'):
                channel = guild.get_channel(state['notification_channel_id'])
                if channel:
                    set_notification_channel(guild_id, channel)
            
            # Restore song queue
            if state.get('song_queue'):
                restored_queue = []
                for song_data in state['song_queue']:
                    # Restore User object from ID
                    if 'requested_by_id' in song_data:
                        try:
                            user = await bot.fetch_user(song_data['requested_by_id'])
                            song_data['requested_by'] = user
                            del song_data['requested_by_id']
                            del song_data['requested_by_name']
                        except:
                            # If user can't be fetched, create a dummy user reference
                            song_data['requested_by'] = type('User', (), {
                                'id': song_data.get('requested_by_id', 0),
                                'name': song_data.get('requested_by_name', 'Unknown'),
                                'mention': f"<@{song_data.get('requested_by_id', 0)}>"
                            })()
                    
                    restored_queue.append(song_data)
                
                song_queues[guild_id] = restored_queue
                print(f"📋 Restored {len(restored_queue)} songs in queue")
            
            # Restore playlist state
            if state.get('playlist_state'):
                playlist_queues[guild_id] = state['playlist_state']
                print(f"🎵 Restored playlist state")
            
            # Create audio source and resume playback
            try:
                file_path = current_song['file_path']
                source = discord.FFmpegPCMAudio(file_path, executable=get_ffmpeg_path(), **ffmpeg_options)
                
                audio_source = SimpleAudioSource(
                    source,
                    title=current_song['title'],
                    duration=current_song.get('duration', 0),
                    file_path=file_path
                )
                
                current_sources[guild_id] = audio_source
                
                # Start playing
                voice_client.play(audio_source, after=lambda e: after_playing(e, guild_id))
                
                # Pause if it was paused before reboot
                if state.get('was_paused', False):
                    voice_client.pause()
                    print(f"⏸️ Resumed in paused state")
                
                print(f"✅ Restored playback: {current_song['title']}")
                restored_count += 1
                
                # Send notification about restoration
                notification_channel = get_notification_channel(guild_id)
                if notification_channel:
                    embed = discord.Embed(
                        title="🔄 Playback Restored",
                        description=f"**{current_song['title']}**",
                        color=0x00ff00
                    )
                    
                    embed.add_field(
                        name="Status",
                        value="▶️ Playing" if state.get('was_playing') else "⏸️ Paused",
                        inline=True
                    )
                    
                    if state.get('song_queue'):
                        embed.add_field(
                            name="Queue",
                            value=f"📋 {len(state['song_queue'])} songs restored",
                            inline=True
                        )
                    
                    embed.set_footer(text="Restored after bot restart")
                    
                    try:
                        await notification_channel.send(embed=embed)
                    except:
                        pass  # Don't fail if notification can't be sent
                
                # Small delay between restorations
                await asyncio.sleep(1)
                
            except Exception as e:
                print(f"❌ Error restoring playback for {current_song['title']}: {e}")
                continue
                
        except Exception as e:
            print(f"❌ Error restoring playback for guild {state.get('guild_name', 'unknown')}: {e}")
    
    if restored_count > 0:
        print(f"✅ Successfully restored playback for {restored_count} guilds")
        # Clear the state file after successful restoration
        clear_playback_state()
    else:
        print("📢 No playback was restored")

# ============================================================================
# 🛡️ WHITELIST SECURITY SYSTEM
# ============================================================================

WHITELIST_FILE = Path("user_whitelist.json")

def load_whitelist():
    """Load authorized users from whitelist file"""
    try:
        if WHITELIST_FILE.exists():
            with open(WHITELIST_FILE, 'r') as f:
                data = json.load(f)
                return set(data.get('authorized_users', []))
        return set()
    except Exception as e:
        print(f"⚠️ Error loading whitelist: {e}")
        return set()

def save_whitelist(authorized_users):
    """Save authorized users to whitelist file"""
    try:
        data = {
            'authorized_users': list(authorized_users),
            'last_updated': time.time()
        }
        with open(WHITELIST_FILE, 'w') as f:
            json.dump(data, f, indent=2)
        return True
    except Exception as e:
        print(f"⚠️ Error saving whitelist: {e}")
        return False

def is_authorized(user_id):
    """Check if user is authorized to use administrative functions"""
    authorized_users = load_whitelist()
    return user_id in authorized_users

def require_authorization():
    """Decorator to require whitelist authorization for commands"""
    def decorator(func):
        import functools
        
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            # First argument should be ctx (context)
            ctx = args[0] if args else None
            
            if not ctx or not hasattr(ctx, 'author'):
                # Fallback if ctx is not properly passed
                return await func(*args, **kwargs)
            
            if not is_authorized(ctx.author.id):
                # Log unauthorized access attempt
                command_name = ctx.command.name if hasattr(ctx, 'command') and ctx.command else 'unknown'
                print(f"🚫 Unauthorized access attempt: {ctx.author} ({ctx.author.id}) tried to use {command_name}")
                
                # Send discrete denial message
                await ctx.send("❌ **Access denied.** This command requires authorization.")
                return
            
            # User is authorized, proceed with command
            return await func(*args, **kwargs)
        
        return wrapper
    return decorator

# Load whitelist on startup
authorized_users = load_whitelist()
print(f"🛡️ Whitelist loaded: {len(authorized_users)} authorized users")

class SimpleAudioSource(discord.PCMVolumeTransformer):
    """Simple audio source that just streams local files"""
    def __init__(self, source, *, title="Unknown", duration=0, file_path=None):
        super().__init__(source, volume=0.8)
        self.title = title
        self.duration = duration
        self.file_path = file_path  # Track the file path for cleanup

def queue_cleanup_request(file_path):
    """Signal background service to clean up a specific file"""
    try:
        cleanup_id = str(uuid.uuid4())[:8]
        
        cleanup_request = {
            'type': 'cleanup',
            'file_path': file_path,
            'cleanup_id': cleanup_id,
            'timestamp': time.time()
        }
        
        cleanup_file = QUEUE_DIR / f"cleanup_{cleanup_id}.json"
        with open(cleanup_file, 'w') as f:
            json.dump(cleanup_request, f, indent=2)
        
        print(f"🧹 Queued cleanup for: {os.path.basename(file_path)}")
        
    except Exception as e:
        print(f"⚠️ Failed to queue cleanup: {e}")

# Song queue management functions
def add_to_song_queue(guild_id, song_info):
    """Add a song to the guild's queue"""
    if guild_id not in song_queues:
        song_queues[guild_id] = []
    song_queues[guild_id].append(song_info)
    print(f"📋 Added '{song_info['title']}' to queue. Position: {len(song_queues[guild_id])}")

def get_next_queued_song(guild_id):
    """Get the next song from the queue"""
    if guild_id in song_queues and song_queues[guild_id]:
        return song_queues[guild_id].pop(0)
    return None

def clear_song_queue(guild_id):
    """Clear all songs from the queue"""
    if guild_id in song_queues:
        count = len(song_queues[guild_id])
        song_queues[guild_id].clear()
        return count
    return 0

def get_queue_info(guild_id):
    """Get information about the current queue"""
    if guild_id not in song_queues:
        return []
    return song_queues[guild_id]

async def play_next_queued_song(guild_id):
    """Play the next song in the queue"""
    guild = bot.get_guild(guild_id)
    if not guild or not guild.voice_client:
        return
    
    voice_client = guild.voice_client
    next_song = get_next_queued_song(guild_id)
    
    if not next_song:
        print(f"📋 Queue empty for guild {guild_id}")
        return
    
    print(f"🎵 Playing next queued song: {next_song['title']}")
    
    try:
        # Create audio source
        audio_path = str(next_song['file_path']).replace('\\', '/')
        source = discord.FFmpegPCMAudio(audio_path, executable=get_ffmpeg_path(), **ffmpeg_options)
        
        audio_source = SimpleAudioSource(
            source, 
            title=next_song['title'],
            duration=next_song['duration'],
            file_path=next_song['file_path']
        )
        
        current_sources[guild_id] = audio_source
        
        # Play the song
        voice_client.play(audio_source, after=lambda e: after_playing(e, guild_id))
        
        # Send notification to the channel where music commands are being used
        # Get the channel from the last command context for this guild
        notification_channel = get_notification_channel(guild_id)
        if notification_channel:
            embed = discord.Embed(
                title="🎵 Now Playing (from queue)",
                description=f"**{next_song['title']}**",
                color=0x9c27b0
            )
            embed.add_field(
                name="Requested by", 
                value=next_song['requested_by'].mention, 
                inline=True
            )
            embed.add_field(
                name="Queue Position", 
                value=f"📊 Was #{1} in queue", 
                inline=True
            )
            if next_song['duration']:
                duration_str = f"{next_song['duration']//60}:{next_song['duration']%60:02d}"
                embed.add_field(name="Duration", value=f"⏱️ {duration_str}", inline=True)
            
            remaining = len(song_queues.get(guild_id, []))
            if remaining > 0:
                embed.set_footer(text=f"{remaining} songs remaining in queue")
            
            try:
                await notification_channel.send(embed=embed)
            except:
                print(f"⚠️ Could not send notification to channel {notification_channel.name}")
            
    except Exception as e:
        print(f"❌ Error playing queued song: {e}")
        # Try to play the next song in queue
        await play_next_queued_song(guild_id)

def after_playing(error, guild_id):
    """Callback after audio finishes - handles cleanup and playlist continuation"""
    if error:
        print(f"💥 Playback error: {error}")
    else:
        print(f"✅ Finished streaming")
    
    # Get the source and signal cleanup
    source = current_sources.get(guild_id)
    if source and hasattr(source, 'file_path') and source.file_path:
        # For playlist files, don't clean up (they're permanent)
        # For temp files, signal cleanup
        if not str(source.file_path).startswith('playlists'):
            queue_cleanup_request(source.file_path)
    
    current_sources.pop(guild_id, None)
    
    if guild_id in playlist_force_after_stop:
        playlist_force_after_stop.discard(guild_id)
        if PlaylistManager.get_playlist_status(guild_id)["status"] == "active":
            asyncio.run_coroutine_threadsafe(
                play_next_playlist_song(guild_id),
                bot.loop,
            )
        return
    
    # Check for queued songs first (from !play commands)
    if guild_id in song_queues and song_queues[guild_id]:
        print(f"📋 Found {len(song_queues[guild_id])} songs in queue")
        # Schedule the next queued song
        asyncio.run_coroutine_threadsafe(
            play_next_queued_song(guild_id), 
            bot.loop
        )
        return
    
    # If no queued songs, check if there's a playlist active and play next song
    playlist_status = PlaylistManager.get_playlist_status(guild_id)
    if playlist_status["status"] == "active":
        # Schedule the next song in the bot's event loop
        asyncio.run_coroutine_threadsafe(
            play_next_playlist_song(guild_id), 
            bot.loop
        )

async def play_next_playlist_song(guild_id):
    """Play the next song in the active playlist"""
    try:
        print(f"🎵 Attempting to play next playlist song for guild {guild_id}")
        
        # Get next song from playlist
        result = PlaylistManager.get_next_playlist_song(guild_id)
        print(f"🔍 Next song result: {result.get('status')}")
        
        if result["status"] == "finished":
            # Playlist completed
            guild = bot.get_guild(guild_id)
            if guild and guild.voice_client:
                channels = [channel for channel in guild.text_channels if channel.permissions_for(guild.me).send_messages]
                if channels:
                    embed = discord.Embed(
                        title="🎵 Playlist Completed!",
                        description=f"Finished playing playlist: **{result.get('playlist_name', 'Unknown')}**",
                        color=0x4caf50
                    )
                    await channels[0].send(embed=embed)
            return
        
        if result["status"] != "success":
            print(f"❌ Failed to get next playlist song: {result['message']}")
            return
        
        # Get song info
        song = result["song"]
        audio_file = Path(song["audio_file"])
        
        print(f"🔍 Playing song: {song.get('title', 'Unknown')}")
        print(f"🔍 Audio file path: {audio_file}")
        print(f"🔍 File exists: {audio_file.exists()}")
        
        if not audio_file.exists():
            print(f"⚠️ Playlist file missing: {audio_file}")
            print(f"❌ File not found, skipping to next song")
            # Try to continue with next song
            await play_next_playlist_song(guild_id)
            return
        
        # Get guild and voice client
        guild = bot.get_guild(guild_id)
        if not guild or not guild.voice_client:
            print(f"⚠️ No voice client for guild {guild_id}")
            return
        
        voice_client = guild.voice_client
        
        # Stop current playback if any
        if voice_client.is_playing() or voice_client.is_paused():
            voice_client.stop()
            await asyncio.sleep(0.5)
        
        # Start playing the next song using global ffmpeg_options
        
        print(f"🔍 Creating FFmpeg source for next song: {str(audio_file)}")
        
        try:
            # Ensure consistent path format for FFmpeg
            audio_path = str(audio_file).replace('\\', '/')
            print(f"🔍 FFmpeg path (normalized): {audio_path}")
            
            # Simple path normalization for FFmpeg
            
            # Use FFmpegPCMAudio - Discord.py handles Opus encoding internally
            source = discord.FFmpegPCMAudio(audio_path, executable=get_ffmpeg_path(), **ffmpeg_options)
            print(f"✅ Created FFmpegPCMAudio source for next song")
            
            print(f"✅ Next song FFmpeg source created successfully")
            
            audio_source = SimpleAudioSource(
                source, 
                title=song.get("title", "Unknown"),
                duration=song.get("duration", 0),
                file_path=str(audio_file)
            )
            
            current_sources[guild_id] = audio_source
            
            print(f"🔍 Voice client status before next song: connected={voice_client.is_connected()}, playing={voice_client.is_playing()}")
            voice_client.play(audio_source, after=lambda e: after_playing(e, guild_id))
            print(f"✅ Started next song playback!")
            
            # Check if playback actually started
            await asyncio.sleep(1)
            print(f"🔍 Next song after 1 second - Voice client playing: {voice_client.is_playing()}")
            
        except Exception as e:
            print(f"❌ Error creating next song audio source: {e}")
            return
        
        # Send notification to the channel where music commands are being used
        notification_channel = get_notification_channel(guild_id)
        if notification_channel:
            embed = discord.Embed(
                title="🎵 Now Playing from Playlist",
                description=f"**{song.get('title', 'Unknown')}**",
                color=0x9c27b0
            )
            embed.add_field(
                name="Playlist", 
                value=f"🎵 {result['playlist_name']}", 
                inline=True
            )
            embed.add_field(
                name="Progress", 
                value=f"📊 {result['position']}/{result['total']}", 
                inline=True
            )
            if song.get('duration'):
                duration_str = f"{song['duration']//60}:{song['duration']%60:02d}"
                embed.add_field(name="Duration", value=f"⏱️ {duration_str}", inline=True)
            
            try:
                await notification_channel.send(embed=embed)
            except:
                print(f"⚠️ Could not send playlist notification to channel {notification_channel.name}")
        
    except Exception as e:
        print(f"❌ Error playing next playlist song: {e}")

class PlaylistManager:
    """Air-gapped playlist management - only references local files"""
    
    @staticmethod
    def create_playlist(name):
        """Create a new empty playlist"""
        try:
            # Sanitize playlist name
            safe_name = "".join(c for c in name if c.isalnum() or c in (' ', '-', '_')).strip()
            if not safe_name:
                return {"status": "error", "message": "Invalid playlist name"}
            
            playlist_file = PLAYLIST_DIR / f"{safe_name}.json"
            
            if playlist_file.exists():
                return {"status": "error", "message": f"Playlist '{safe_name}' already exists"}
            
            playlist_data = {
                "name": safe_name,
                "created": time.time(),
                "files": [],  # List of local file paths only
                "total_duration": 0
            }
            
            with open(playlist_file, 'w') as f:
                json.dump(playlist_data, f, indent=2)
            
            return {"status": "success", "message": f"Created playlist '{safe_name}'"}
            
        except Exception as e:
            return {"status": "error", "message": f"Failed to create playlist: {str(e)}"}
    
    @staticmethod
    def list_playlists():
        """List all available playlists"""
        try:
            playlists = []
            for playlist_file in PLAYLIST_DIR.glob("*.json"):
                try:
                    with open(playlist_file, 'r') as f:
                        data = json.load(f)
                    
                    playlists.append({
                        "name": data.get("name", playlist_file.stem),
                        "files": len(data.get("files", [])),
                        "duration": data.get("total_duration", 0)
                    })
                except Exception:
                    continue
            
            return {"status": "success", "playlists": playlists}
            
        except Exception as e:
            return {"status": "error", "message": f"Failed to list playlists: {str(e)}"}
    
    @staticmethod
    def get_available_media():
        """Get list of available local media files (air-gap safe)"""
        try:
            media_files = []
            
            for webm_file in MEDIA_DIR.glob("*.webm"):
                try:
                    # Get metadata if available
                    json_file = webm_file.with_suffix('.json')
                    title = "Unknown"
                    duration = 0
                    
                    if json_file.exists():
                        with open(json_file, 'r') as f:
                            metadata = json.load(f)
                        title = metadata.get('title', 'Unknown')
                        duration = metadata.get('duration', 0)
                    
                    media_files.append({
                        "file_path": str(webm_file),
                        "filename": webm_file.name,
                        "title": title,
                        "duration": duration
                    })
                except Exception:
                    continue
            
            return {"status": "success", "files": media_files}
            
        except Exception as e:
            return {"status": "error", "message": f"Failed to get media files: {str(e)}"}
    
    @staticmethod
    def add_song_to_playlist(playlist_name, current_file_path, song_title, duration=0):
        """Add currently playing song to playlist (air-gap safe)"""
        try:
            # Validate playlist exists
            playlist_file = PLAYLIST_DIR / f"{playlist_name}.json"
            if not playlist_file.exists():
                return {"status": "error", "message": f"Playlist '{playlist_name}' not found"}
            
            # Validate current file exists
            current_path = Path(current_file_path)
            if not current_path.exists():
                return {"status": "error", "message": "Current audio file not found"}
            
            # Create playlist-specific directory for permanent storage
            playlist_media_dir = PLAYLIST_DIR / playlist_name
            playlist_media_dir.mkdir(exist_ok=True)
            
            # Generate unique filename for playlist storage
            timestamp = int(time.time())
            file_id = str(uuid.uuid4())[:8]
            safe_title = "".join(c for c in song_title if c.isalnum() or c in (' ', '-', '_')).strip()[:30]
            if not safe_title:
                safe_title = "Unknown"
            
            # Create permanent filenames
            audio_filename = f"{safe_title}_{timestamp}_{file_id}.webm"
            metadata_filename = f"{safe_title}_{timestamp}_{file_id}.json"
            
            permanent_audio_path = playlist_media_dir / audio_filename
            permanent_metadata_path = playlist_media_dir / metadata_filename
            
            # Copy audio file to permanent location
            shutil.copy2(current_path, permanent_audio_path)
            
            # Copy metadata file if it exists
            metadata_source = current_path.with_suffix('.json')
            if metadata_source.exists():
                shutil.copy2(metadata_source, permanent_metadata_path)
            else:
                # Create basic metadata
                metadata = {
                    "title": song_title,
                    "duration": duration,
                    "added_to_playlist": time.time(),
                    "original_file": str(current_path.name)
                }
                with open(permanent_metadata_path, 'w') as f:
                    json.dump(metadata, f, indent=2)
            
            # Update playlist JSON
            with open(playlist_file, 'r') as f:
                playlist_data = json.load(f)
            
            # Add song entry to playlist
            song_entry = {
                "title": song_title,
                "audio_file": str(permanent_audio_path),
                "metadata_file": str(permanent_metadata_path),
                "duration": duration,
                "added": time.time()
            }
            
            playlist_data["files"].append(song_entry)
            playlist_data["total_duration"] += duration
            
            # Save updated playlist
            with open(playlist_file, 'w') as f:
                json.dump(playlist_data, f, indent=2)
            
            return {
                "status": "success", 
                "message": f"Added '{song_title}' to playlist '{playlist_name}'",
                "permanent_file": str(permanent_audio_path)
            }
            
        except Exception as e:
            return {"status": "error", "message": f"Failed to add song to playlist: {str(e)}"}
    
    @staticmethod
    def get_playlist_contents(playlist_name):
        """Get contents of a specific playlist"""
        try:
            playlist_file = PLAYLIST_DIR / f"{playlist_name}.json"
            if not playlist_file.exists():
                return {"status": "error", "message": f"Playlist '{playlist_name}' not found"}
            
            with open(playlist_file, 'r') as f:
                playlist_data = json.load(f)
            
            return {"status": "success", "playlist": playlist_data}
            
        except Exception as e:
            return {"status": "error", "message": f"Failed to get playlist contents: {str(e)}"}
    
    @staticmethod
    def clean_playlist(playlist_name):
        """Remove missing files from playlist JSON"""
        try:
            result = PlaylistManager.get_playlist_contents(playlist_name)
            if result["status"] != "success":
                return result
            
            playlist = result["playlist"]
            songs = playlist.get("files", [])
            original_count = len(songs)
            
            # Filter out missing files
            valid_songs = []
            removed_songs = []
            for song in songs:
                audio_file = Path(song.get("audio_file", ""))
                if audio_file.exists():
                    valid_songs.append(song)
                else:
                    removed_songs.append(song.get("title", "Unknown"))
                    print(f"⚠️ Removing missing file from playlist: {audio_file}")
            
            if len(valid_songs) != original_count:
                # Update the playlist JSON
                playlist["files"] = valid_songs
                playlist["total_duration"] = sum(song.get("duration", 0) for song in valid_songs)
                
                # Save updated playlist
                playlist_file = Path(f"playlists/{playlist_name}.json")
                with open(playlist_file, 'w', encoding='utf-8') as f:
                    json.dump(playlist, f, indent=2, ensure_ascii=False)
                
                return {
                    "status": "success",
                    "message": f"Cleaned playlist '{playlist_name}': removed {original_count - len(valid_songs)} missing files",
                    "removed_songs": removed_songs,
                    "remaining_songs": len(valid_songs)
                }
            else:
                return {"status": "success", "message": f"Playlist '{playlist_name}' is already clean"}
                
        except Exception as e:
            return {"status": "error", "message": f"Failed to clean playlist: {str(e)}"}

    @staticmethod
    def start_playlist_playback(playlist_name, guild_id):
        """Start playing a playlist (air-gap safe - only local files)"""
        try:
            # First clean the playlist to remove any missing files
            clean_result = PlaylistManager.clean_playlist(playlist_name)
            if clean_result["status"] == "success" and "removed" in clean_result["message"]:
                print(f"🧹 {clean_result['message']}")
            
            result = PlaylistManager.get_playlist_contents(playlist_name)
            if result["status"] != "success":
                return result
            
            playlist = result["playlist"]
            songs = playlist.get("files", [])
            
            if not songs:
                return {"status": "error", "message": f"Playlist '{playlist_name}' is empty"}
            
            # Verify all playlist files exist (should be clean now)
            valid_songs = []
            for song in songs:
                audio_file = Path(song.get("audio_file", ""))
                
                print(f"🔍 Checking playlist file: {audio_file}")
                print(f"🔍 File exists: {audio_file.exists()}")
                
                if audio_file.exists():
                    valid_songs.append(song)
                else:
                    print(f"⚠️ Playlist file missing: {audio_file}")
            
            if not valid_songs:
                return {"status": "error", "message": f"No valid files found in playlist '{playlist_name}'"}
            
            # Set up playlist queue with smart features
            playlist_queues[guild_id] = {
                "playlist_name": playlist_name,
                "songs": valid_songs.copy(),  # Current playing order
                "original_songs": valid_songs.copy(),  # Original order for un-shuffle
                "current_index": 0,
                "total_songs": len(valid_songs),
                "shuffle": False,  # Shuffle mode off by default
                "loop_mode": "off"  # Loop modes: "off", "single", "all"
            }
            
            return {
                "status": "success", 
                "message": f"Started playlist '{playlist_name}' with {len(valid_songs)} songs",
                "first_song": valid_songs[0]
            }
            
        except Exception as e:
            return {"status": "error", "message": f"Failed to start playlist: {str(e)}"}
    
    @staticmethod
    def get_next_playlist_song(guild_id):
        """Get the next song in the current playlist with loop support"""
        try:
            if guild_id not in playlist_queues:
                return {"status": "error", "message": "No playlist is currently active"}
            
            queue = playlist_queues[guild_id]
            current_index = queue["current_index"]
            songs = queue["songs"]
            loop_mode = queue.get("loop_mode", "off")
            
            print(f"🔍 Playlist state: index={current_index}, songs={len(songs)}, loop={loop_mode}")
            
            # Handle single song loop
            if loop_mode == "single":
                # Stay on current song (don't increment index)
                if current_index > 0:
                    current_index = current_index - 1  # Go back to current song
                    queue["current_index"] = current_index
                current_song = songs[current_index] if current_index < len(songs) else songs[0]
                queue["current_index"] += 1
            elif current_index >= len(songs):
                # End of playlist reached
                if loop_mode == "all":
                    # Loop back to beginning
                    queue["current_index"] = 0
                    current_song = songs[0]
                    queue["current_index"] += 1
                elif len(songs) == 1:
                    # Special case: single song playlist, loop it by default
                    queue["current_index"] = 0
                    current_song = songs[0]
                    queue["current_index"] += 1
                    print("🔄 Single song playlist - looping by default")
                else:
                    # Playlist finished
                    playlist_queues.pop(guild_id, None)
                    return {"status": "finished", "message": "Playlist completed"}
            else:
                # Normal progression
                current_song = songs[current_index]
                queue["current_index"] += 1
            
            return {
                "status": "success",
                "song": current_song,
                "position": queue["current_index"],  # Use the updated index for position
                "total": queue["total_songs"],
                "playlist_name": queue["playlist_name"]
            }
            
        except Exception as e:
            return {"status": "error", "message": f"Failed to get next song: {str(e)}"}
    
    @staticmethod
    def stop_playlist(guild_id):
        """Stop the current playlist"""
        if guild_id in playlist_queues:
            playlist_name = playlist_queues[guild_id]["playlist_name"]
            playlist_queues.pop(guild_id, None)
            return {"status": "success", "message": f"Stopped playlist '{playlist_name}'"}
        return {"status": "error", "message": "No playlist is currently active"}
    
    @staticmethod
    def get_playlist_status(guild_id):
        """Get current playlist status"""
        if guild_id not in playlist_queues:
            return {"status": "inactive"}
        
        queue = playlist_queues[guild_id]
        return {
            "status": "active",
            "playlist_name": queue["playlist_name"],
            "current_position": queue["current_index"],
            "total_songs": queue["total_songs"],
            "remaining": queue["total_songs"] - queue["current_index"],
            "shuffle": queue.get("shuffle", False),
            "loop_mode": queue.get("loop_mode", "off")
        }
    
    @staticmethod
    def shuffle_playlist(guild_id):
        """Toggle shuffle mode for current playlist"""
        try:
            if guild_id not in playlist_queues:
                return {"status": "error", "message": "No playlist is currently active"}
            
            queue = playlist_queues[guild_id]
            current_shuffle = queue.get("shuffle", False)
            
            if not current_shuffle:
                # Enable shuffle
                import random
                current_song_index = queue["current_index"] - 1  # Current song (already played)
                remaining_songs = queue["songs"][queue["current_index"]:]  # Unplayed songs
                
                # Shuffle only the remaining songs
                random.shuffle(remaining_songs)
                
                # Reconstruct the playlist: played songs + shuffled remaining
                queue["songs"] = queue["songs"][:queue["current_index"]] + remaining_songs
                queue["shuffle"] = True
                
                return {"status": "success", "message": "🔀 Shuffle enabled! Remaining songs randomized"}
            else:
                # Disable shuffle - restore original order
                current_song_index = queue["current_index"] - 1
                original_songs = queue["original_songs"]
                
                # Find where we are in the original playlist
                if current_song_index >= 0:
                    current_song = queue["songs"][current_song_index]
                    # Find this song in original order
                    try:
                        original_index = next(i for i, song in enumerate(original_songs) 
                                            if song.get("audio_file") == current_song.get("audio_file"))
                        queue["current_index"] = original_index + 1
                    except StopIteration:
                        # Fallback if not found
                        queue["current_index"] = 0
                
                queue["songs"] = original_songs.copy()
                queue["shuffle"] = False
                
                return {"status": "success", "message": "🔄 Shuffle disabled! Restored original order"}
                
        except Exception as e:
            return {"status": "error", "message": f"Failed to toggle shuffle: {str(e)}"}
    
    @staticmethod
    def set_loop_mode(guild_id, mode):
        """Set loop mode for current playlist"""
        try:
            if guild_id not in playlist_queues:
                return {"status": "error", "message": "No playlist is currently active"}
            
            valid_modes = ["off", "single", "all"]
            if mode not in valid_modes:
                return {"status": "error", "message": f"Invalid loop mode. Use: {', '.join(valid_modes)}"}
            
            queue = playlist_queues[guild_id]
            queue["loop_mode"] = mode
            
            mode_emojis = {"off": "⏹️", "single": "🔂", "all": "🔁"}
            mode_names = {"off": "Off", "single": "Single Song", "all": "All Songs"}
            
            return {
                "status": "success", 
                "message": f"{mode_emojis[mode]} Loop mode set to: {mode_names[mode]}"
            }
            
        except Exception as e:
            return {"status": "error", "message": f"Failed to set loop mode: {str(e)}"}
    
    @staticmethod
    def skip_song(guild_id, direction="next"):
        """Skip to next or previous song in playlist"""
        try:
            if guild_id not in playlist_queues:
                return {"status": "error", "message": "No playlist is currently active"}
            
            queue = playlist_queues[guild_id]
            current_index = queue["current_index"]
            songs = queue["songs"]
            
            if direction == "next":
                if len(songs) == 1:
                    # Single song playlist - restart the same song
                    queue["current_index"] = 0
                    return {"status": "success", "message": "⏭️ Restarting single song in playlist"}
                elif current_index >= len(songs):
                    # Multi-song playlist at end - loop back to start
                    queue["current_index"] = 0
                    return {"status": "success", "message": "⏭️ Looping back to first song"}
                else:
                    # Normal skip is handled by normal progression
                    return {"status": "success", "message": "⏭️ Skipping to next song"}
            
            elif direction == "previous":
                if current_index <= 1:
                    # Go to first song
                    queue["current_index"] = 0
                    return {
                        "status": "success", 
                        "message": "⏮️ Going to first song",
                        "song": songs[0]
                    }
                else:
                    # Go back 2 positions (since current_index is already +1 from current song)
                    queue["current_index"] = max(0, current_index - 2)
                    return {
                        "status": "success", 
                        "message": "⏮️ Going to previous song",
                        "song": songs[queue["current_index"]]
                    }
            
        except Exception as e:
            return {"status": "error", "message": f"Failed to skip song: {str(e)}"}

def queue_cleanup_all_request():
    """Signal background service to clean up all media files on startup/shutdown"""
    try:
        cleanup_id = str(uuid.uuid4())[:8]
        
        cleanup_request = {
            'type': 'cleanup_all',
            'cleanup_id': cleanup_id,
            'timestamp': time.time(),
            'reason': 'startup_cleanup'
        }
        
        cleanup_file = QUEUE_DIR / f"cleanup_all_{cleanup_id}.json"
        with open(cleanup_file, 'w') as f:
            json.dump(cleanup_request, f, indent=2)
        
        print(f"🧹 Queued full cleanup on startup")
        
    except Exception as e:
        print(f"⚠️ Failed to queue startup cleanup: {e}")

@bot.event
async def on_ready():
    print(f"✅ {bot.user.name} is online!")
    print(f"📁 Connected to {len(bot.guilds)} servers")
    
    # Check intents status
    print("\n🔍 INTENT STATUS CHECK:")
    print(f"   Message Content Intent: {bot.intents.message_content}")
    print(f"   Voice States Intent: {bot.intents.voice_states}")
    print(f"   Guild Messages Intent: {bot.intents.guild_messages}")
    
    if not bot.intents.message_content:
        print("\n⚠️ WARNING: MESSAGE CONTENT INTENT IS DISABLED!")
        print("⚠️ The bot cannot read message content!")
        print("⚠️ Go to Discord Developer Portal → Your Bot → Privileged Gateway Intents")
        print("⚠️ Enable 'MESSAGE CONTENT INTENT' and restart the bot!")
    else:
        print("✅ Message Content Intent is enabled")
    
    # List connected servers
    if bot.guilds:
        print(f"\n📋 Connected to {len(bot.guilds)} server(s):")
        for guild in bot.guilds:
            print(f"   • {guild.name} (ID: {guild.id})")
            # Check bot permissions in each server
            perms = guild.me.guild_permissions
            print(f"     Permissions: Read Messages={perms.read_messages}, Send Messages={perms.send_messages}")
    
    print("\n🎮 Audio streaming bot ready!")
    print("📁 File streaming service online")
    print("💡 Try typing '!commands' to test if the bot can see messages")
    
    # Auto-rejoin voice channels after restart
    print("\n🔊 Checking for voice channels to rejoin...")
    await rejoin_voice_channels()
    
    # Restore playback state after rejoining channels
    print("🎵 Checking for playback state to restore...")
    await restore_playback_state()
    
    # Queue cleanup of any leftover files from previous runs (only if no playback was restored)
    playback_state = load_playback_state()
    if not playback_state:
        queue_cleanup_all_request()
    else:
        print("🎵 Skipping cleanup - playback state exists")

@bot.event
async def on_disconnect():
    """Handle Discord disconnection"""
    print("🔌 Bot disconnected from Discord")
    
    # Queue cleanup of any remaining files on disconnect
    try:
        cleanup_id = str(uuid.uuid4())[:8]
        cleanup_request = {
            'type': 'cleanup_all',
            'cleanup_id': cleanup_id,
            'timestamp': time.time(),
            'reason': 'shutdown_cleanup'
        }
        
        cleanup_file = QUEUE_DIR / f"cleanup_all_{cleanup_id}.json"
        with open(cleanup_file, 'w') as f:
            json.dump(cleanup_request, f, indent=2)
        
        print(f"🧹 Queued shutdown cleanup")
    except Exception as e:
        print(f"⚠️ Failed to queue shutdown cleanup: {e}")

@bot.event
async def on_voice_state_update(member, before, after):
    """Track voice state changes"""
    if member == bot.user:
        if before.channel != after.channel:
            if after.channel:
                print(f"🔊 Bot joined voice channel: {after.channel.name}")
            elif before.channel:
                print(f"👋 Bot left voice channel: {before.channel.name}")

@bot.event
async def on_command_error(ctx, error):
    """Handle command errors"""
    if isinstance(error, commands.CommandNotFound):
        # Ignore unknown commands silently
        return
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing required argument: {error.param.name}")
    elif isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"⏳ Command on cooldown. Try again in {error.retry_after:.1f} seconds.")
    else:
        print(f"❌ Error in command {ctx.command.name if ctx.command else 'unknown'}: {type(error).__name__}: {error}")
        import traceback
        traceback.print_exc()
        await ctx.send(f"❌ An error occurred: {str(error)}")

@bot.event
async def on_message(message):
    """Log all messages that start with command prefix for debugging"""
    # Ignore messages from bots (including ourselves)
    if message.author.bot:
        await bot.process_commands(message)  # Still process in case we want bot-to-bot commands
        return
    
    # Log ALL messages for debugging (not just commands)
    guild_name = message.guild.name if message.guild else 'DM'
    channel_name = message.channel.name if hasattr(message.channel, 'name') else str(message.channel.type)
    channel_type = message.channel.type.name if hasattr(message.channel, 'type') else 'unknown'
    
    print(f"📨 Message received: '{message.content[:100] if message.content else '(empty)'}'")
    print(f"   From: {message.author} ({message.author.id})")
    print(f"   In: {guild_name} → {channel_name} (type: {channel_type})")
    print(f"   Content type: {type(message.content)}, Length: {len(message.content) if message.content else 0}")
    
    # Log messages with command prefix
    if message.content and message.content.startswith('!'):
        print(f"🎯 Processing command: '{message.content}'")
    
    # Process commands normally - THIS IS CRITICAL
    try:
        await bot.process_commands(message)
        if message.content and message.content.startswith('!'):
            print(f"✅ Command processed: '{message.content}'")
    except Exception as e:
        print(f"❌ Error processing command: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        # Try to send error to user
        try:
            await message.channel.send(f"❌ Error processing command: {str(e)}")
        except:
            pass

def queue_audio_request(query):
    """Queue request for background processing service"""
    request_id = str(uuid.uuid4())[:8]
    request_data = {
        'query': query,
        'request_id': request_id,
        'timestamp': time.time()
    }
    
    request_file = QUEUE_DIR / f"request_{request_id}.json"
    with open(request_file, 'w') as f:
        json.dump(request_data, f, indent=2)
    
    return request_id

def check_processing_result(request_id):
    """Check if background service has processed the request"""
    result_file = QUEUE_DIR / f"result_{request_id}.json"
    
    if result_file.exists():
        try:
            with open(result_file, 'r') as f:
                result = json.load(f)
            
            # Clean up result file
            result_file.unlink()
            return result
        except Exception as e:
            print(f"❌ Error reading result: {e}")
            return None
    
    return None

@bot.command(name="test", help="Test if bot is responding to commands")
async def test(ctx):
    """Simple test command to verify bot is working"""
    print(f"🧪 TEST COMMAND RECEIVED from {ctx.author} ({ctx.author.id}) in {ctx.guild.name}")
    await ctx.send("✅ **Bot is working!** I can see your commands. Try `!summon` now.")

@bot.command(name="summon", help="Summon the bot to your voice channel")
async def summon(ctx):
    print(f"📥 Received !summon command from {ctx.author} ({ctx.author.id}) in {ctx.guild.name}")
    
    if not ctx.author.voice or not ctx.author.voice.channel:
        print(f"❌ User {ctx.author} is not in a voice channel")
        await ctx.send("❌ You need to be in a voice channel to summon me!")
        return

    channel = ctx.author.voice.channel
    voice_client = ctx.guild.voice_client
    print(f"🎯 Target channel: {channel.name} (ID: {channel.id})")

    # Check permissions first
    permissions = channel.permissions_for(ctx.guild.me)
    print(f"🔐 Permissions check - Connect: {permissions.connect}, Speak: {permissions.speak}")
    
    if not permissions.connect:
        await ctx.send(f"❌ I don't have permission to connect to **{channel.name}**!\nPlease check my permissions in the server settings.")
        print(f"❌ Missing 'Connect' permission for {channel.name} in {ctx.guild.name}")
        return
    
    if not permissions.speak:
        await ctx.send(f"⚠️ I can connect but won't be able to play audio (missing 'Speak' permission)")
        print(f"⚠️ Missing 'Speak' permission for {channel.name} in {ctx.guild.name}")

    if voice_client:
        if voice_client.channel == channel:
            await ctx.send(f"🔊 I'm already connected to **{channel.name}**!")
            return
        else:
            try:
                await voice_client.disconnect()
                await asyncio.sleep(0.5)  # Small delay after disconnect
            except Exception as disconnect_error:
                print(f"⚠️ Error disconnecting from previous channel: {disconnect_error}")

    try:
        connecting_msg = await ctx.send(f"🎵 Connecting to **{channel.name}**...")
        print(f"🔄 Attempting to connect to voice channel: {channel.name} (ID: {channel.id}) in {ctx.guild.name}")
        print(f"   Channel type: {type(channel).__name__}")
        print(f"   Guild ID: {ctx.guild.id}")
        print(f"   Bot user: {ctx.guild.me.name} (ID: {ctx.guild.me.id})")
        
        # Check if bot is already connected somewhere
        if ctx.guild.voice_client:
            print(f"   Existing voice client found: {ctx.guild.voice_client}")
            print(f"   Existing connection status: {ctx.guild.voice_client.is_connected()}")
        
        # Try connecting with timeout
        print(f"   Starting connection attempt...")
        try:
            voice_client = await asyncio.wait_for(
                channel.connect(timeout=10.0, reconnect=True, self_deaf=False, self_mute=False),
                timeout=20.0  # Increased timeout
            )
            print(f"   Connection call completed, got voice_client: {voice_client}")
        except asyncio.TimeoutError as timeout_err:
            print(f"❌ Connection timeout during asyncio.wait_for: {timeout_err}")
            raise
        except Exception as connect_err:
            print(f"❌ Error during channel.connect(): {type(connect_err).__name__}: {connect_err}")
            raise
        
        # Verify connection immediately
        print(f"   Verifying connection...")
        print(f"   voice_client object: {voice_client}")
        print(f"   voice_client.is_connected(): {voice_client.is_connected() if voice_client else 'N/A'}")
        print(f"   voice_client.channel: {voice_client.channel if voice_client else 'N/A'}")
        
        await asyncio.sleep(1.0)  # Give it more time to establish
        
        # Check again after delay
        is_connected = voice_client.is_connected() if voice_client else False
        print(f"   After delay - is_connected: {is_connected}")
        
        if voice_client and is_connected:
            await connecting_msg.edit(content=f"🔊 **Connected** to **{channel.name}**!")
            print(f"✅ Successfully connected to {channel.name} in {ctx.guild.name}")
            print(f"   Voice client channel: {voice_client.channel.name if voice_client.channel else 'None'}")
        else:
            error_details = f"Voice client exists: {voice_client is not None}, Connected: {is_connected}"
            await connecting_msg.edit(content=f"⚠️ Connection issue: {error_details}\n💡 Try `!diagnose` to check status.")
            print(f"⚠️ Connection to {channel.name} failed verification: {error_details}")
            # Try to get more info
            if voice_client:
                print(f"   Voice client state: {voice_client}")
                print(f"   Voice client endpoint: {getattr(voice_client, 'endpoint', 'N/A')}")
            
    except asyncio.TimeoutError:
        error_msg = "❌ Connection timeout! This might be a network/firewall issue.\n💡 Try:\n• Temporarily disabling Windows Firewall\n• Checking your internet connection\n• Using `!nettest` to diagnose"
        try:
            await ctx.send(error_msg)
        except:
            pass
        print(f"❌ Connection timeout to {channel.name} in {ctx.guild.name}")
        import traceback
        traceback.print_exc()
        
    except discord.errors.ClientException as e:
        error_msg = f"❌ Discord client error: {str(e)}\n💡 The bot might already be connected elsewhere or there's a connection issue."
        try:
            await ctx.send(error_msg)
        except:
            pass
        print(f"❌ Discord client error connecting to {channel.name}: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        
    except discord.errors.PrivilegedIntentsRequired as e:
        error_msg = "❌ Missing required intents! Please enable 'MESSAGE CONTENT INTENT' in the Discord Developer Portal."
        try:
            await ctx.send(error_msg)
        except:
            pass
        print(f"❌ Missing privileged intents: {e}")
        import traceback
        traceback.print_exc()
        
    except Exception as e:
        error_msg = f"❌ Failed to connect: {str(e)}\n💡 Error type: {type(e).__name__}\n💡 Try `!diagnose` for more info"
        try:
            await ctx.send(error_msg)
        except:
            pass
        print(f"❌ Unexpected error connecting to {channel.name} in {ctx.guild.name}: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()

@bot.command(name="play", help="Stream audio content")
@require_authorization()
async def play(ctx, *, query: str):
    """Stream audio content from query"""
    
    # Set notification channel to where the command was issued
    set_notification_channel(ctx.guild.id, ctx.channel)
        
    voice_client = ctx.guild.voice_client
    if not voice_client or not voice_client.is_connected():
        await ctx.send("❌ I'm not connected to a voice channel! Use `!summon` first.")
        return

    
    try:
        # Process audio request
        processing_msg = await ctx.send(f"🔄 **Processing:** `{query}`...")
        
        print(f"📝 Queuing request: {query}")
        request_id = queue_audio_request(query)
        
        # Wait for background service to process (with timeout)
        max_wait = 180  # allow slow yt-dlp / first-time downloads
        wait_time = 0
        result = None
        
        while wait_time < max_wait:
            result = check_processing_result(request_id)
            if result:
                break
            
            await asyncio.sleep(1)
            wait_time += 1
            
            # Update progress every 5 seconds
            if wait_time % 5 == 0:
                await processing_msg.edit(content=f"🔄 **Processing:** `{query}` ({wait_time}s)")
        
        if not result:
            await processing_msg.edit(content=f"⏰ **Timeout:** Processing took too long")
            return
        
        if result['status'] != 'success':
            await processing_msg.edit(content=f"❌ **Error:** {result.get('message', 'Unknown error')}")
            return
        
        # Get file path from result
        file_path = result['file_path']
        title = result.get('title', 'Unknown')
        duration = result.get('duration', 0)
        cached = result.get('cached', False)
        
        if not os.path.exists(file_path):
            await processing_msg.edit(content=f"❌ **Error:** File not found")
            return
        
        # Check if something is already playing - if so, queue this song
        voice_client = ctx.guild.voice_client
        if voice_client.is_playing() or voice_client.is_paused():
            # Add to queue instead of interrupting current playback
            song_info = {
                'title': title,
                'file_path': file_path,
                'duration': duration,
                'requested_by': ctx.author,
                'cached': cached
            }
            add_to_song_queue(ctx.guild.id, song_info)
            
            # Show queue position
            queue_position = len(song_queues[ctx.guild.id])
            duration_str = f" • {duration//60}:{duration%60:02d}" if duration else ""
            cached_str = " (cached)" if cached else ""
            
            embed = discord.Embed(
                title="📋 Added to Queue",
                description=f"**{title}**{duration_str}{cached_str}",
                color=0x3498db
            )
            embed.add_field(name="Position", value=f"#{queue_position}", inline=True)
            embed.add_field(name="Requested by", value=ctx.author.mention, inline=True)
            
            queue_info = get_queue_info(ctx.guild.id)
            if len(queue_info) > 1:
                embed.set_footer(text=f"{len(queue_info)} songs in queue")
            
            await processing_msg.edit(content=None, embed=embed)
            return
        
        # Stream the audio file (nothing is currently playing)
        print(f"🎵 Streaming file: {os.path.basename(file_path)}")
        
        # Use FFmpegPCMAudio - Discord.py handles Opus encoding internally
        print(f"🔍 FFmpeg options being used: {ffmpeg_options}")
        print(f"🔍 File path for FFmpeg: {file_path}")
        print(f"🔍 File exists: {os.path.exists(file_path)}")
        print(f"🔍 File size: {os.path.getsize(file_path) if os.path.exists(file_path) else 'N/A'} bytes")
        
        # Test the file with FFmpeg manually first
        try:
            import subprocess
            test_cmd = ['ffmpeg', '-i', file_path, '-t', '5', '-f', 'null', '-']
            print(f"🔍 Testing file with command: {' '.join(test_cmd)}")
            result = subprocess.run(test_cmd, capture_output=True, text=True, timeout=15)
            print(f"🔍 FFmpeg test return code: {result.returncode}")
            if result.returncode != 0:
                print(f"⚠️ FFmpeg test stderr: {result.stderr[:200]}")
            else:
                print(f"✅ FFmpeg test passed!")
        except Exception as test_error:
            print(f"⚠️ FFmpeg test failed: {test_error}")
        
        source = discord.FFmpegPCMAudio(file_path, executable=get_ffmpeg_path(), **ffmpeg_options)
        print(f"✅ Created FFmpegPCMAudio source")
        
        audio_source = SimpleAudioSource(source, title=title, duration=duration, file_path=file_path)
        
        # Track for cleanup
        current_sources[ctx.guild.id] = audio_source
        
        # Start streaming
        voice_client.play(audio_source, after=lambda e: after_playing(e, ctx.guild.id))
        
        # Verify playback started
        await asyncio.sleep(0.5)
        if voice_client.is_playing():
            duration_str = f" • {duration//60}:{duration%60:02d}" if duration else ""
            cached_str = " (cached)" if cached else ""
            
            await processing_msg.edit(content=f"🎵 **Now streaming:** {title}{duration_str}{cached_str}")
            print(f"✅ Successfully streaming: {title}")
        else:
            await processing_msg.edit(content=f"❌ **Failed to start streaming**")
            print(f"❌ Streaming failed for: {title}")
            
    except Exception as e:
        print(f"💥 Error in stream command: {e}")
        try:
            await ctx.send(f"❌ **Streaming error:** {str(e)}")
        except:
            pass

@bot.command(name="pause", help="Pause audio stream")
async def pause(ctx):
    """Pause current stream"""
    if ctx.guild.voice_client and ctx.guild.voice_client.is_playing():
        ctx.guild.voice_client.pause()
        await ctx.send("⏸️ **Paused**")
    else:
        await ctx.send("❌ Nothing playing")

@bot.command(name="resume", help="Resume audio stream")
async def resume(ctx):
    """Resume stream"""
    if ctx.guild.voice_client and ctx.guild.voice_client.is_paused():
        ctx.guild.voice_client.resume()
        await ctx.send("▶️ **Resumed**")
    else:
        await ctx.send("❌ Nothing paused")

@bot.command(name="queue", help="Show current song queue")
async def show_queue(ctx):
    """Show the current song queue"""
    queue_info = get_queue_info(ctx.guild.id)
    
    if not queue_info:
        await ctx.send("📋 The queue is empty!")
        return
    
    embed = discord.Embed(
        title="📋 Current Song Queue",
        description=f"{len(queue_info)} songs in queue",
        color=0x3498db
    )
    
    # Show up to 10 songs in queue
    for i, song in enumerate(queue_info[:10], 1):
        duration_str = f" • {song['duration']//60}:{song['duration']%60:02d}" if song['duration'] else ""
        cached_str = " (cached)" if song['cached'] else ""
        
        embed.add_field(
            name=f"{i}. {song['title']}{duration_str}{cached_str}",
            value=f"Requested by {song['requested_by'].mention}",
            inline=False
        )
    
    if len(queue_info) > 10:
        embed.set_footer(text=f"... and {len(queue_info) - 10} more songs")
    
    await ctx.send(embed=embed)

@bot.command(name="clearqueue", help="Clear the song queue")
@require_authorization()
async def clear_queue(ctx):
    """Clear all songs from the queue"""
    count = clear_song_queue(ctx.guild.id)
    
    if count == 0:
        await ctx.send("📋 The queue is already empty!")
    else:
        embed = discord.Embed(
            title="🗑️ Queue Cleared",
            description=f"Removed {count} songs from the queue",
            color=0xff9900
        )
        await ctx.send(embed=embed)

@bot.command(name="skip", help="Skip to next track (playlist or !play queue)")
@require_authorization()
async def skip_track(ctx):
    """Skip current track; continues active playlist or advances !play queue."""
    set_notification_channel(ctx.guild.id, ctx.channel)
    vc = ctx.guild.voice_client
    if not vc or not vc.is_connected():
        await ctx.send("❌ I'm not connected to a voice channel.")
        return
    if not (vc.is_playing() or vc.is_paused()):
        await ctx.send("❌ Nothing playing")
        return
    
    if PlaylistManager.get_playlist_status(ctx.guild.id)["status"] == "active":
        result = PlaylistManager.skip_song(ctx.guild.id, "next")
        if result["status"] != "success":
            await ctx.send(f"❌ {result['message']}")
            return
        playlist_force_after_stop.add(ctx.guild.id)
        vc.stop()
        await ctx.send(f"⏭️ {result['message']}")
        return
    
    if song_queues.get(ctx.guild.id):
        vc.stop()
        await ctx.send("⏭️ Skipped — playing next in queue")
        return
    
    vc.stop()
    await ctx.send("⏭️ Stopped")

@bot.command(name="stop", help="Stop audio stream")
async def stop(ctx):
    """Stop current stream"""
    if ctx.guild.voice_client:
        if ctx.guild.voice_client.is_playing() or ctx.guild.voice_client.is_paused():
            # Queue cleanup of any active files before stopping
            source = current_sources.get(ctx.guild.id)
            if source and hasattr(source, 'file_path') and source.file_path:
                queue_cleanup_request(source.file_path)
            
            ctx.guild.voice_client.stop()
            current_sources.pop(ctx.guild.id, None)
            
            # Also clear the song queue when stopping
            queue_count = clear_song_queue(ctx.guild.id)
            if queue_count > 0:
                await ctx.send(f"⏹️ **Stopped** (cleared {queue_count} queued songs)")
            else:
                await ctx.send("⏹️ **Stopped**")
        else:
            await ctx.send("❌ Nothing playing")
    else:
        await ctx.send("❌ Not connected")

@bot.command(name="kill", help="Disconnect the bot from voice channel")
async def kill(ctx):
    """Disconnect from voice channel"""
    if ctx.guild.voice_client:
        # Queue cleanup of any active files before disconnecting
        source = current_sources.get(ctx.guild.id)
        if source and hasattr(source, 'file_path') and source.file_path:
            queue_cleanup_request(source.file_path)
        
        # Stop any playback
        if ctx.guild.voice_client.is_playing() or ctx.guild.voice_client.is_paused():
            ctx.guild.voice_client.stop()
        
        current_sources.pop(ctx.guild.id, None)
        await ctx.guild.voice_client.disconnect()
        await ctx.send("👋 **Disconnected**")
    else:
        await ctx.send("❌ Not connected")

@bot.command(name="commands", help="Show available commands")
async def show_commands(ctx):
    """Show help"""
    embed = discord.Embed(
        title="🎵 Gondor Music Bot Commands",
        description="⚠️ **Important:** Use `!summon` first to bring me to your voice channel!\n\nYour advanced Discord music bot with media management!",
        color=0x00ff00
    )
    
    embed.add_field(
        name="🎶 Music Commands (Require !summon first)",
        value=(
            "`!play <search or url>` - Stream audio content (auto-queues if busy)\n"
            "`!pause` - Pause the current song\n"
            "`!resume` - Resume playback\n"
            "`!stop` - Stop current song (bot stays in channel)\n"
            "`!queue` - Show current song queue 📋\n"
            "`!clearqueue` - Clear all queued songs 🗑️"
        ),
        inline=False
    )
    
    embed.add_field(
        name="🔧 Control Commands", 
        value=(
            "`!summon` - Summon bot to your voice channel\n"
            "`!kill` - Disconnect bot from voice channel 💀\n"
            "`!commands` - Show this commands list"
        ),
        inline=False
    )
    
    embed.add_field(
        name="🔍 Diagnostic Commands",
        value=(
            "`!diagnose` - Run bot diagnostics\n"
            "`!nettest` - Test network connectivity"
        ),
        inline=False
    )
    
    embed.add_field(
        name="🌍 Regional Settings",
        value=(
            "`!region` - Show current audio settings\n"
            "`!region us` - US/English audio (48kHz, stereo)\n"
            "`!region eu` - European audio (44.1kHz, stereo)\n"
            "`!region hq` - High Quality audio (192k)\n"
            "`!region voice` - Voice optimized (mono, low latency)\n"
            "`!voiceregion` - Show voice channel region info"
        ),
        inline=False
    )
    
    # Only show admin commands to authorized users
    if is_authorized(ctx.author.id):
        embed.add_field(
            name="🛡️ Admin Commands",
            value=(
                "`!whitelist` - Show authorized users\n"
                "`!whitelist add <user|id>` - Add user to whitelist\n"
                "`!whitelist remove <user|id>` - Remove user from whitelist\n"
                "`!whitelist check <user|id>` - Check user authorization\n"
                "`!reboot` - Restart the bot\n"
                "`!rejoin` - Rejoin saved voice channels\n"
                "`!deps update` - Upgrade pip packages from requirements.txt"
            ),
            inline=False
        )
        embed.set_footer(text="🛡️ You have administrative access")
    else:
        embed.set_footer(text="Some commands require authorization")
    
    embed.add_field(
        name="📋 Playlist Commands",
        value=(
            "`!playlist` - Show all playlist commands\n"
            "`!playlist create <name>` - Create new playlist\n"
            "`!playlist play <name>` - Play entire playlist\n"
            "`!playlist shuffle` - Toggle shuffle mode 🔀\n"
            "`!playlist loop <mode>` - Set loop mode 🔁\n"
            "`!skip` / `!playlist skip` - Skip to next track ⏭️\n"
            "`!playlist prev` - Previous track ⏮️"
        ),
        inline=False
    )
    
    embed.set_footer(text="Use !commands to see this message anytime • Made with ❤️")
    await ctx.send(embed=embed)

@bot.command(name="diagnose", help="Run diagnostics to check bot functionality")
async def diagnose(ctx):
    """Run diagnostic tests for the bot system"""
    
    embed = discord.Embed(title="🔧 Bot Diagnostics", color=0x00ff00)
    
    # Check FFmpeg
    try:
        import subprocess
        result = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            ffmpeg_version = result.stdout.split('\n')[0]
            embed.add_field(name="FFmpeg", value=f"✅ {ffmpeg_version[:50]}...", inline=False)
        else:
            embed.add_field(name="FFmpeg", value="❌ FFmpeg not working properly", inline=False)
    except FileNotFoundError:
        embed.add_field(name="FFmpeg", value="❌ FFmpeg not installed or not in PATH", inline=False)
    except subprocess.TimeoutExpired:
        embed.add_field(name="FFmpeg", value="❌ FFmpeg timeout", inline=False)
    except Exception as e:
        embed.add_field(name="FFmpeg", value=f"❌ FFmpeg error: {str(e)}", inline=False)
    
    # Check ytdlp/yt-dlp
    try:
        result = subprocess.run(['yt-dlp', '--version'], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            ytdlp_version = result.stdout.strip()
            embed.add_field(name="yt-dlp", value=f"✅ Version {ytdlp_version}", inline=False)
        else:
            embed.add_field(name="yt-dlp", value="❌ yt-dlp not working properly", inline=False)
    except FileNotFoundError:
        embed.add_field(name="yt-dlp", value="❌ yt-dlp not installed or not in PATH", inline=False)
    except subprocess.TimeoutExpired:
        embed.add_field(name="yt-dlp", value="❌ yt-dlp timeout", inline=False)
    except Exception as e:
        embed.add_field(name="yt-dlp", value=f"❌ yt-dlp error: {str(e)}", inline=False)    

    # Check voice client status
    if ctx.guild.voice_client:
        if ctx.guild.voice_client.is_connected():
            embed.add_field(name="Voice Connection", value=f"✅ Connected to {ctx.guild.voice_client.channel.name}", inline=False)
        else:
            embed.add_field(name="Voice Connection", value="⚠️ Voice client exists but not connected", inline=False)
    else:
        embed.add_field(name="Voice Connection", value="❌ Not connected to voice", inline=False)
    
    # Check permissions
    permissions = ctx.guild.me.guild_permissions
    voice_perms = []
    if permissions.connect:
        voice_perms.append("✅ Connect")
    else:
        voice_perms.append("❌ Connect")
        
    if permissions.speak:
        voice_perms.append("✅ Speak")
    else:
        voice_perms.append("❌ Speak")
        
    if permissions.use_voice_activation:
        voice_perms.append("✅ Voice Activity")
    else:
        voice_perms.append("❌ Voice Activity")
    
    embed.add_field(name="Voice Permissions", value="\n".join(voice_perms), inline=False)
    
    # Check if user is in voice
    if ctx.author.voice:
        embed.add_field(name="User Voice Status", value=f"✅ In {ctx.author.voice.channel.name}", inline=False)
    else:
        embed.add_field(name="User Voice Status", value="❌ Not in voice channel", inline=False)
    
    # Check file system components
    queue_status = "✅ Ready" if QUEUE_DIR.exists() else "❌ Missing"
    embed.add_field(name="Queue System", value=queue_status, inline=False)
    
    await ctx.send(embed=embed)

@bot.command(name="nettest", help="Test network connectivity to Discord voice servers")
async def nettest(ctx):
    """Test network connectivity for the bot system"""
    
    embed = discord.Embed(title="🌐 Network Connectivity Test", color=0xff9900)
    
    # Test DNS resolution for Discord voice servers
    try:
        import socket
        import requests
        # Test DNS resolution for common Discord voice endpoints
        test_hosts = [
            "discord.gg",
            "gateway.discord.gg", 
            "media.discordapp.net"
        ]
        
        dns_results = []
        for host in test_hosts:
            try:
                ip = socket.gethostbyname(host)
                dns_results.append(f"✅ {host} → {ip}")
            except socket.gaierror:
                dns_results.append(f"❌ {host} → DNS Failed")
        
        embed.add_field(name="DNS Resolution", value="\n".join(dns_results), inline=False)
        
    except Exception as e:
        embed.add_field(name="DNS Resolution", value=f"❌ DNS test failed: {str(e)}", inline=False)
    
    # Test if we can reach Discord's API
    try:
        response = requests.get("https://discord.com/api/v9/gateway", timeout=5)
        if response.status_code == 200:
            embed.add_field(name="Discord API", value="✅ Reachable", inline=False)
        else:
            embed.add_field(name="Discord API", value=f"⚠️ Status: {response.status_code}", inline=False)
    except Exception as api_e:
        embed.add_field(name="Discord API", value=f"❌ Unreachable: {str(api_e)}", inline=False)
    
    # Check Windows Firewall status (if possible)
    try:
        import subprocess
        result = subprocess.run(['netsh', 'advfirewall', 'show', 'allprofiles', 'state'], 
                              capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            if "ON" in result.stdout:
                embed.add_field(name="Windows Firewall", value="🟡 Enabled (may block UDP)", inline=False)
            else:
                embed.add_field(name="Windows Firewall", value="✅ Disabled", inline=False)
        else:
            embed.add_field(name="Windows Firewall", value="❓ Cannot determine status", inline=False)
    except Exception:
        embed.add_field(name="Windows Firewall", value="❓ Cannot check", inline=False)
    
    # System status checks - perform real checks for services and tools
    try:
        status_lines = []

        # 1) Background processing worker: send a lightweight "ping" request and wait briefly for a result file
        try:
            ping_id = str(uuid.uuid4())[:8]
            ping_request = {
                "type": "status_ping",
                "request_id": ping_id,
                "timestamp": time.time()
            }
            ping_file = QUEUE_DIR / f"request_{ping_id}.json"
            with open(ping_file, "w", encoding="utf-8") as f:
                json.dump(ping_request, f)
            background_status = "❌ No response"
            # Wait up to 5 seconds for a result_{id}.json to be created by the background processor
            waited = 0
            while waited < 5:
                await asyncio.sleep(1)
                waited += 1
                res = check_processing_result(ping_id)
                if res:
                    background_status = "✅ Background worker responded"
                    # If worker returns diagnostic info include short message
                    if isinstance(res, dict) and res.get("status"):
                        background_status += f" ({res.get('status')})"
                    break
        except Exception as be:
            background_status = f"❌ Ping error: {str(be)}"

        status_lines.append(f"🔁 Background worker: {background_status}")

        # 2) FFmpeg: check configured path and run -version
        try:
            ffmpeg_path = get_ffmpeg_path()
            ffmpeg_ok = False
            ffmpeg_msg = ""
            try:
                proc = subprocess.run([ffmpeg_path, "-version"], capture_output=True, text=True, timeout=5)
                if proc.returncode == 0 and proc.stdout:
                    ffmpeg_ok = True
                    ffmpeg_msg = proc.stdout.splitlines()[0]
                else:
                    # Try generic ffmpeg in PATH as fallback
                    proc2 = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=5)
                    if proc2.returncode == 0 and proc2.stdout:
                        ffmpeg_ok = True
                        ffmpeg_msg = proc2.stdout.splitlines()[0]
            except FileNotFoundError:
                # try ffmpeg in PATH
                proc2 = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=5)
                if proc2.returncode == 0 and proc2.stdout:
                    ffmpeg_ok = True
                    ffmpeg_msg = proc2.stdout.splitlines()[0]
            except Exception as e:
                ffmpeg_msg = f"Error: {e}"

            status_lines.append(f"🎛️ FFmpeg: {'✅ ' + ffmpeg_msg if ffmpeg_ok else '❌ Not available (' + str(ffmpeg_msg) + ')'}")
        except Exception as fe:
            status_lines.append(f"🎛️ FFmpeg: ❌ Check error ({fe})")

        # 3) yt-dlp availability
        try:
            proc = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True, timeout=5)
            if proc.returncode == 0:
                status_lines.append(f"📥 yt-dlp: ✅ Version {proc.stdout.strip()}")
            else:
                status_lines.append("📥 yt-dlp: ❌ Not responding properly")
        except FileNotFoundError:
            status_lines.append("📥 yt-dlp: ❌ Not installed or not in PATH")
        except Exception as ye:
            status_lines.append(f"📥 yt-dlp: ❌ Error ({ye})")

        # 4) Queue directory writable/readable
        try:
            test_file = QUEUE_DIR / f"health_{str(uuid.uuid4())[:8]}.tmp"
            with open(test_file, "w", encoding="utf-8") as f:
                f.write("ok")
            test_file.unlink()
            status_lines.append(f"📂 Queue dir ({QUEUE_DIR}): ✅ Read/Write")
        except Exception as qe:
            status_lines.append(f"📂 Queue dir ({QUEUE_DIR}): ❌ Not writable ({qe})")

        # 5) Media directory writable/readable
        try:
            test_file = MEDIA_DIR / f"health_{str(uuid.uuid4())[:8]}.tmp"
            with open(test_file, "w", encoding="utf-8") as f:
                f.write("ok")
            test_file.unlink()
            status_lines.append(f"📁 Media dir ({MEDIA_DIR}): ✅ Read/Write")
        except Exception as me:
            status_lines.append(f"📁 Media dir ({MEDIA_DIR}): ❌ Not writable ({me})")

        # 6) Disk space (free)
        try:
            usage = shutil.disk_usage(str(Path.cwd()))
            free_mb = usage.free // (1024 * 1024)
            status_lines.append(f"💾 Disk free: {free_mb} MB")
        except Exception as de:
            status_lines.append(f"💾 Disk: ❌ Error ({de})")

        # 7) Bot event loop
        try:
            loop_running = getattr(bot.loop, "is_running", lambda: False)()
            status_lines.append(f"🔌 Bot loop: {'✅ Running' if loop_running else '❌ Not running'}")
        except Exception as le:
            status_lines.append(f"🔌 Bot loop: ❌ Error ({le})")

        # Aggregate into embed
        embed.add_field(
            name="🔒 System Status Checks",
            value="\n".join(status_lines),
            inline=False
        )
    except Exception as e:
        embed.add_field(
            name="🔒 System Status",
            value=f"❌ Failed to run system checks: {e}",
            inline=False
        )
    
    # Voice connection troubleshooting tips
    tips = [
        "🔧 **Try `!summon` to reconnect to voice**",
        "🔄 **Use `!kill` then `!summon` if connection issues**", 
        "📡 **Check your internet connection**",
        "🛡️ **Temporarily disable Windows Firewall if blocked**"
    ]
    
    embed.add_field(name="Troubleshooting Tips", value="\n".join(tips), inline=False)
    
    await ctx.send(embed=embed)

@bot.command(name="playlist", help="Manage playlists")
@require_authorization()
async def playlist_cmd(ctx, action=None, *, args=None):
    """Air-gap safe playlist management - only uses local files"""
    
    # Set notification channel for playlist notifications
    if action in ["play", "next", "prev", "shuffle", "loop"]:
        set_notification_channel(ctx.guild.id, ctx.channel)
    
    if not action:
        embed = discord.Embed(
            title="🎵 Playlist Commands",
            description="Manage your local file playlists",
            color=0x9c27b0
        )
        
        embed.add_field(
            name="📋 Basic Commands",
            value=(
                "`!playlist create <name>` - Create new playlist\n"
                "`!playlist list` - Show all playlists\n"
                "`!playlist add <name>` - Add currently playing song\n"
                "`!playlist show <name>` - Show playlist contents\n"
                "`!playlist clean <name>` - Remove missing files from playlist\n"
                "`!playlist media` - Show available local media files"
            ),
            inline=False
        )
        
        embed.add_field(
            name="🎮 Playback Commands",
            value=(
                "`!playlist play <name>` - Play entire playlist\n"
                "`!playlist stop` - Stop current playlist\n"
                "`!playlist skip` - Skip to next song\n"
                "`!playlist prev` - Go to previous song\n"
                "`!playlist status` - Show current playlist status"
            ),
            inline=False
        )
        
        embed.add_field(
            name="🎛️ Smart Controls",
            value=(
                "`!playlist shuffle` - Toggle shuffle mode 🔀\n"
                "`!playlist loop <off/single/all>` - Set loop mode\n"
                "• `off` - No looping ⏹️\n"
                "• `single` - Loop current song 🔂\n"
                "• `all` - Loop entire playlist 🔁"
            ),
            inline=False
        )
        
        embed.add_field(
            name="🔒 Local Storage",
            value="Playlists only reference local media files\nAll playlist management is done locally for fast access",
            inline=False
        )
        
        embed.set_footer(text="Step 1: Basic playlist creation • More features coming")
        await ctx.send(embed=embed)
        return
    
    if action.lower() == "create":
        if not args:
            await ctx.send("❌ Usage: `!playlist create <playlist name>`")
            return
        
        result = PlaylistManager.create_playlist(args)
        
        if result["status"] == "success":
            await ctx.send(f"✅ {result['message']}")
        else:
            await ctx.send(f"❌ {result['message']}")
    
    elif action.lower() == "list":
        result = PlaylistManager.list_playlists()
        
        if result["status"] == "success":
            playlists = result["playlists"]
            
            if not playlists:
                await ctx.send("📋 No playlists found. Create one with `!playlist create <name>`")
                return
            
            embed = discord.Embed(
                title="📋 Your Playlists",
                color=0x9c27b0
            )
            
            for playlist in playlists:
                duration_str = f"{playlist['duration']//60}:{playlist['duration']%60:02d}" if playlist['duration'] else "0:00"
                embed.add_field(
                    name=f"🎵 {playlist['name']}",
                    value=f"{playlist['files']} files • {duration_str}",
                    inline=True
                )
            
            embed.set_footer(text="Use !playlist show <name> to view playlist contents")
            await ctx.send(embed=embed)
        else:
            await ctx.send(f"❌ {result['message']}")
    
    elif action.lower() == "add":
        if not args:
            await ctx.send("❌ Usage: `!playlist add <playlist name>`")
            return
        
        # Check if something is currently playing
        voice_client = ctx.guild.voice_client
        if not voice_client or not voice_client.is_playing():
            await ctx.send("❌ Nothing is currently playing! Use `!play <song>` first.")
            return
        
        # Get current playing source
        current_source = current_sources.get(ctx.guild.id)
        if not current_source or not hasattr(current_source, 'file_path') or not current_source.file_path:
            await ctx.send("❌ No file information available for current song.")
            return
        
        # Add song to playlist
        playlist_name = args.strip()
        song_title = getattr(current_source, 'title', 'Unknown')
        duration = getattr(current_source, 'duration', 0)
        
        # Add a small delay to ensure file copying completes before any cleanup
        await asyncio.sleep(0.5)
        
        result = PlaylistManager.add_song_to_playlist(
            playlist_name, 
            current_source.file_path, 
            song_title, 
            duration
        )
        print(f"🔍 Add song result: {result}")
        
        if result["status"] == "success":
            embed = discord.Embed(
                title="✅ Song Added to Playlist!",
                description=f"**{song_title}** has been permanently saved",
                color=0x4caf50
            )
            embed.add_field(name="Playlist", value=f"🎵 {playlist_name}", inline=True)
            embed.add_field(name="Duration", value=f"⏱️ {duration//60}:{duration%60:02d}" if duration else "Unknown", inline=True)
            embed.add_field(name="Status", value="🔒 Protected from cleanup", inline=True)
            embed.set_footer(text="Use !playlist show to view playlist contents")
            await ctx.send(embed=embed)
        else:
            await ctx.send(f"❌ {result['message']}")
    
    elif action.lower() == "play":
        if not args:
            await ctx.send("❌ Usage: `!playlist play <playlist name>`")
            return
        
        # Check if bot is in voice channel
        if not ctx.guild.voice_client:
            await ctx.send("❌ I need to be summoned first! Use `!summon`")
            return
        
        playlist_name = args.strip()
        
        # Start playlist playback
        result = PlaylistManager.start_playlist_playback(playlist_name, ctx.guild.id)
        print(f"🔍 Playlist start result: {result}")
        
        if result["status"] == "success":
            first_song = result["first_song"]
            
            # Play the first song
            audio_file = Path(first_song["audio_file"])
            
            print(f"🔍 First song: {first_song.get('title', 'Unknown')}")
            print(f"🔍 First song path: {audio_file}")
            print(f"🔍 First song exists: {audio_file.exists()}")
            
            if not audio_file.exists():
                await ctx.send(f"❌ First playlist file not found: {audio_file.name}")
                return
            
            voice_client = ctx.guild.voice_client
            
            # Stop current playback
            if voice_client.is_playing() or voice_client.is_paused():
                voice_client.stop()
                await asyncio.sleep(0.5)
            
            # Start playing first song using global ffmpeg_options
            
            print(f"🔍 About to create FFmpeg source with path: {str(audio_file)}")
            print(f"🔍 FFmpeg options: {ffmpeg_options}")
            
            try:
                # Ensure consistent path format for FFmpeg
                audio_path = str(audio_file).replace('\\', '/')
                print(f"🔍 FFmpeg path (normalized): {audio_path}")
                
                # Simple path normalization for FFmpeg
                
                # Use FFmpegPCMAudio with explicit FFmpeg path - Discord.py handles Opus encoding internally
                source = discord.FFmpegPCMAudio(audio_path, executable=get_ffmpeg_path(), **ffmpeg_options)
                print(f"✅ Created FFmpegPCMAudio source for playlist")
                
                print(f"✅ FFmpeg source created successfully")
                
                audio_source = SimpleAudioSource(
                    source,
                    title=first_song.get("title", "Unknown"),
                    duration=first_song.get("duration", 0),
                    file_path=str(audio_file)
                )
                print(f"✅ Audio source created successfully")
                
                current_sources[ctx.guild.id] = audio_source
                
                print(f"🔍 Voice client connected: {voice_client.is_connected()}")
                print(f"🔍 Voice client playing: {voice_client.is_playing()}")
                print(f"🔍 Voice client paused: {voice_client.is_paused()}")
                
                voice_client.play(audio_source, after=lambda e: after_playing(e, ctx.guild.id))
                print(f"✅ Started playback!")
                
                # First song is songs[0]; next call to get_next_playlist_song must return songs[1]
                if ctx.guild.id in playlist_queues:
                    playlist_queues[ctx.guild.id]["current_index"] = 1
                
                # Check if playback actually started
                await asyncio.sleep(1)
                print(f"🔍 After 1 second - Voice client playing: {voice_client.is_playing()}")
                
            except Exception as e:
                print(f"❌ Error creating audio source: {e}")
                await ctx.send(f"❌ Failed to create audio source: {str(e)}")
                return
            
            # Send confirmation
            embed = discord.Embed(
                title="🎵 Started Playlist Playback!",
                description=f"**{first_song.get('title', 'Unknown')}**",
                color=0x9c27b0
            )
            embed.add_field(name="Playlist", value=f"🎵 {playlist_name}", inline=True)
            embed.add_field(name="Total Songs", value=f"📊 {result['message'].split()[-2]}", inline=True)
            if first_song.get('duration'):
                duration_str = f"{first_song['duration']//60}:{first_song['duration']%60:02d}"
                embed.add_field(name="Duration", value=f"⏱️ {duration_str}", inline=True)
            
            embed.set_footer(text="Songs will play automatically • Use !playlist stop to end")
            await ctx.send(embed=embed)
        else:
            await ctx.send(f"❌ {result['message']}")
    
    elif action.lower() == "stop":
        result = PlaylistManager.stop_playlist(ctx.guild.id)
        
        if result["status"] == "success":
            # Stop current playback
            if ctx.guild.voice_client and (ctx.guild.voice_client.is_playing() or ctx.guild.voice_client.is_paused()):
                ctx.guild.voice_client.stop()
            
            embed = discord.Embed(
                title="⏹️ Playlist Stopped",
                description=result["message"],
                color=0xff5722
            )
            await ctx.send(embed=embed)
        else:
            await ctx.send(f"❌ {result['message']}")
    
    elif action.lower() == "clean":
        if not args:
            await ctx.send("❌ Usage: `!playlist clean <playlist name>`")
            return
        
        playlist_name = args.strip()
        result = PlaylistManager.clean_playlist(playlist_name)
        
        if result["status"] == "success":
            if "removed" in result["message"]:
                # Files were removed
                embed = discord.Embed(
                    title="🧹 Playlist Cleaned",
                    description=result["message"],
                    color=0xff9900
                )
                if result.get("removed_songs"):
                    removed_list = "\n".join([f"• {song}" for song in result["removed_songs"][:10]])
                    if len(result["removed_songs"]) > 10:
                        removed_list += f"\n... and {len(result['removed_songs']) - 10} more"
                    embed.add_field(
                        name="Removed Songs",
                        value=removed_list,
                        inline=False
                    )
                embed.add_field(
                    name="Remaining Songs",
                    value=f"{result['remaining_songs']} songs",
                    inline=True
                )
                await ctx.send(embed=embed)
            else:
                # No files needed to be removed
                await ctx.send(f"✅ {result['message']}")
        else:
            await ctx.send(f"❌ {result['message']}")
    
    elif action.lower() == "show":
        if not args:
            await ctx.send("❌ Usage: `!playlist show <playlist name>`")
            return
        
        result = PlaylistManager.get_playlist_contents(args.strip())
        
        if result["status"] == "success":
            playlist = result["playlist"]
            files = playlist.get("files", [])
            
            if not files:
                await ctx.send(f"📋 Playlist '{args}' is empty. Use `!playlist add {args}` while playing a song to add it!")
                return
            
            embed = discord.Embed(
                title=f"📋 Playlist: {playlist['name']}",
                description=f"{len(files)} songs • {playlist['total_duration']//60}:{playlist['total_duration']%60:02d}",
                color=0x9c27b0
            )
            
            for i, song in enumerate(files[:10]):  # Show first 10 songs
                duration_str = f"{song['duration']//60}:{song['duration']%60:02d}" if song['duration'] else "Unknown"
                embed.add_field(
                    name=f"{i+1}. {song['title'][:25]}{'...' if len(song['title']) > 25 else ''}",
                    value=f"⏱️ {duration_str}",
                    inline=True
                )
            
            if len(files) > 10:
                embed.set_footer(text=f"Showing 10 of {len(files)} songs")
            
            await ctx.send(embed=embed)
        else:
            await ctx.send(f"❌ {result['message']}")
    
    elif action.lower() == "media":
        result = PlaylistManager.get_available_media()
        
        if result["status"] == "success":
            files = result["files"]
            
            if not files:
                await ctx.send("📁 No local media files found. Play some music first to build your library!")
                return
            
            embed = discord.Embed(
                title="📁 Available Local Media Files",
                description="These files can be added to playlists",
                color=0x2196f3
            )
            
            for i, file in enumerate(files[:10]):  # Show first 10 files
                duration_str = f"{file['duration']//60}:{file['duration']%60:02d}" if file['duration'] else "Unknown"
                embed.add_field(
                    name=f"🎵 {file['title'][:30]}{'...' if len(file['title']) > 30 else ''}",
                    value=f"`{file['filename'][:20]}...` • {duration_str}",
                    inline=True
                )
            
            if len(files) > 10:
                embed.set_footer(text=f"Showing 10 of {len(files)} files")
            
            await ctx.send(embed=embed)
        else:
            await ctx.send(f"❌ {result['message']}")
    
    elif action.lower() == "shuffle":
        result = PlaylistManager.shuffle_playlist(ctx.guild.id)
        
        if result["status"] == "success":
            embed = discord.Embed(
                title="🔀 Shuffle Toggle",
                description=result["message"],
                color=0xff9800
            )
            await ctx.send(embed=embed)
        else:
            await ctx.send(f"❌ {result['message']}")
    
    elif action.lower() == "loop":
        if not args:
            await ctx.send("❌ Usage: `!playlist loop <off/single/all>`")
            return
        
        result = PlaylistManager.set_loop_mode(ctx.guild.id, args.strip().lower())
        
        if result["status"] == "success":
            embed = discord.Embed(
                title="🔁 Loop Mode",
                description=result["message"],
                color=0x4caf50
            )
            await ctx.send(embed=embed)
        else:
            await ctx.send(f"❌ {result['message']}")
    
    elif action.lower() == "skip":
        result = PlaylistManager.skip_song(ctx.guild.id, "next")
        
        if result["status"] == "success":
            # Stop current playback to trigger next song (prefer playlist over !play queue)
            if ctx.guild.voice_client and (ctx.guild.voice_client.is_playing() or ctx.guild.voice_client.is_paused()):
                playlist_force_after_stop.add(ctx.guild.id)
                ctx.guild.voice_client.stop()
            
            embed = discord.Embed(
                title="⏭️ Skip Song",
                description=result["message"],
                color=0x2196f3
            )
            await ctx.send(embed=embed)
        else:
            await ctx.send(f"❌ {result['message']}")
    
    elif action.lower() == "prev" or action.lower() == "previous":
        result = PlaylistManager.skip_song(ctx.guild.id, "previous")
        
        if result["status"] == "success":
            # Stop current playback and play the previous song
            if ctx.guild.voice_client and (ctx.guild.voice_client.is_playing() or ctx.guild.voice_client.is_paused()):
                playlist_force_after_stop.add(ctx.guild.id)
                ctx.guild.voice_client.stop()
            
            embed = discord.Embed(
                title="⏮️ Previous Song",
                description=result["message"],
                color=0x2196f3
            )
            await ctx.send(embed=embed)
        else:
            await ctx.send(f"❌ {result['message']}")
    
    elif action.lower() == "status":
        status = PlaylistManager.get_playlist_status(ctx.guild.id)
        
        if status["status"] == "active":
            shuffle_icon = "🔀" if status["shuffle"] else "📄"
            loop_icons = {"off": "⏹️", "single": "🔂", "all": "🔁"}
            loop_icon = loop_icons.get(status["loop_mode"], "⏹️")
            
            embed = discord.Embed(
                title="🎵 Current Playlist Status",
                color=0x9c27b0
            )
            
            embed.add_field(
                name="📋 Playlist",
                value=f"**{status['playlist_name']}**",
                inline=True
            )
            
            embed.add_field(
                name="📊 Progress",
                value=f"{status['current_position']}/{status['total_songs']} songs",
                inline=True
            )
            
            embed.add_field(
                name="⏳ Remaining",
                value=f"{status['remaining']} songs",
                inline=True
            )
            
            embed.add_field(
                name="🔀 Shuffle",
                value=f"{shuffle_icon} {'On' if status['shuffle'] else 'Off'}",
                inline=True
            )
            
            embed.add_field(
                name="🔁 Loop Mode",
                value=f"{loop_icon} {status['loop_mode'].title()}",
                inline=True
            )
            
            await ctx.send(embed=embed)
        else:
            await ctx.send("❌ No playlist is currently active. Use `!playlist play <name>` to start one!")
    
    else:
        await ctx.send(f"❌ Unknown action: `{action}`. Use `!playlist` to see available commands.")

@bot.command(name="region", help="Set regional audio settings")
@require_authorization()
async def set_region(ctx, region: str = None):
    """Set regional audio processing settings"""
    global ffmpeg_options
    
    if not region:
        embed = discord.Embed(
            title="🌍 Regional Audio Settings",
            description="Available regional configurations:",
            color=0x00ff00
        )
        
        embed.add_field(
            name="Current Settings",
            value=f"```{ffmpeg_options['options']}```",
            inline=False
        )
        
        embed.add_field(
            name="Available Regions",
            value=(
                "`!region us` - US/English (48kHz, stereo, 128k)\n"
                "`!region eu` - European (44.1kHz, stereo, 128k)\n"
                "`!region hq` - High Quality (48kHz, stereo, 192k)\n"
                "`!region voice` - Voice optimized (48kHz, mono, 64k)"
            ),
            inline=False
        )
        
        await ctx.send(embed=embed)
        return
    
    region = region.lower()
    
    if region == "us":
        ffmpeg_options = {
            'before_options': '',
            'options': '-vn -f s16le -ar 48000 -ac 2 -acodec pcm_s16le -loglevel warning'
        }
        await ctx.send("🇺🇸 **US/English audio settings applied:** 48kHz, stereo, PCM")
        
    elif region == "eu":
        ffmpeg_options = {
            'before_options': '',
            'options': '-vn -f s16le -ar 44100 -ac 2 -acodec pcm_s16le -loglevel warning'
        }
        await ctx.send("🇪🇺 **European audio settings applied:** 44.1kHz, stereo, PCM")
        
    elif region == "hq":
        ffmpeg_options = {
            'before_options': '',
            'options': '-vn -f s16le -ar 48000 -ac 2 -acodec pcm_s16le -loglevel warning -af volume=1.2'
        }
        await ctx.send("🎵 **High Quality audio settings applied:** 48kHz, stereo, PCM, +20% volume")
        
    elif region == "voice":
        ffmpeg_options = {
            'before_options': '',
            'options': '-vn -f s16le -ar 48000 -ac 1 -acodec pcm_s16le -loglevel warning'
        }
        await ctx.send("🎤 **Voice optimized settings applied:** 48kHz, mono, PCM")
        
    else:
        await ctx.send("❌ **Invalid region!** Use: `us`, `eu`, `hq`, or `voice`")

# ============================================================================
# 🛡️ WHITELIST MANAGEMENT COMMANDS
# ============================================================================

async def resolve_user_id(ctx, user_input):
    """Resolve user input to a Discord user ID (supports both ID and username)"""
    if not user_input:
        return None, None
    
    # Try to parse as user ID first
    try:
        user_id = int(user_input)
        # Try to fetch user to validate ID exists
        try:
            user = await bot.fetch_user(user_id)
            return user_id, user
        except:
            # ID doesn't exist, but still return it for error handling
            return user_id, None
    except ValueError:
        pass
    
    # Try to resolve as username (with or without discriminator)
    user_input = user_input.lower()
    
    # Search through guild members first (faster and more accurate)
    if ctx.guild:
        for member in ctx.guild.members:
            # Check username match
            if member.name.lower() == user_input:
                return member.id, member
            # Check display name match
            if member.display_name.lower() == user_input:
                return member.id, member
            # Check old format with discriminator
            if f"{member.name.lower()}#{member.discriminator}" == user_input:
                return member.id, member
    
    # If not found in guild, try global username search (limited)
    # This is less reliable but covers users not in the current guild
    try:
        # Try to find user by global username (this is limited by Discord API)
        for guild in bot.guilds:
            for member in guild.members:
                if member.name.lower() == user_input:
                    return member.id, member
                if member.display_name.lower() == user_input:
                    return member.id, member
    except:
        pass
    
    return None, None

@bot.command(name="whitelist", help="Manage authorized users")
@require_authorization()
async def whitelist_cmd(ctx, action=None, *, user_input=None):
    """Manage the whitelist of authorized users - supports both user IDs and usernames"""
    
    if not action:
        # Show current whitelist
        authorized_users = load_whitelist()
        
        embed = discord.Embed(
            title="🛡️ Authorized Users",
            description=f"Users with administrative access ({len(authorized_users)} total)",
            color=0x00ff00
        )
        
        if authorized_users:
            user_list = []
            for user_id in authorized_users:
                try:
                    user = await bot.fetch_user(user_id)
                    user_list.append(f"• **{user.name}** (`{user_id}`)")
                except:
                    user_list.append(f"• Unknown User (`{user_id}`)")
            
            embed.add_field(
                name="Authorized Users",
                value="\n".join(user_list[:10]),  # Limit to 10 for display
                inline=False
            )
            
            if len(user_list) > 10:
                embed.set_footer(text=f"... and {len(user_list) - 10} more users")
        else:
            embed.add_field(
                name="No Users",
                value="No users are currently authorized.",
                inline=False
            )
        
        embed.add_field(
            name="Commands",
            value=(
                "`!whitelist add <user_id|username>` - Add user to whitelist\n"
                "`!whitelist remove <user_id|username>` - Remove user from whitelist\n"
                "`!whitelist check <user_id|username>` - Check if user is authorized"
            ),
            inline=False
        )
        
        await ctx.send(embed=embed)
        return
    
    action = action.lower()
    
    if action == "add":
        if not user_input:
            await ctx.send("❌ **Missing user.** Usage: `!whitelist add <user_id|username>`")
            return
        
        # Resolve user input to user ID
        user_id, user = await resolve_user_id(ctx, user_input)
        
        if user_id is None:
            await ctx.send(f"❌ **User not found:** `{user_input}`\nTry using their exact username or Discord user ID.")
            return
        
        # Load current whitelist
        authorized_users = load_whitelist()
        
        if user_id in authorized_users:
            user_display = user.name if user else f"User {user_id}"
            await ctx.send(f"⚠️ **{user_display} is already authorized.**")
            return
        
        # Add user to whitelist
        authorized_users.add(user_id)
        
        if save_whitelist(authorized_users):
            if user:
                await ctx.send(f"✅ **Added {user.name} (`{user_id}`) to whitelist.**")
                print(f"🛡️ {ctx.author} added {user.name} ({user_id}) to whitelist")
            else:
                await ctx.send(f"✅ **Added user `{user_id}` to whitelist.**")
                print(f"🛡️ {ctx.author} added user {user_id} to whitelist")
        else:
            await ctx.send("❌ **Failed to save whitelist.** Please try again.")
    
    elif action == "remove":
        if not user_input:
            await ctx.send("❌ **Missing user.** Usage: `!whitelist remove <user_id|username>`")
            return
        
        # Resolve user input to user ID
        user_id, user = await resolve_user_id(ctx, user_input)
        
        if user_id is None:
            await ctx.send(f"❌ **User not found:** `{user_input}`\nTry using their exact username or Discord user ID.")
            return
        
        # Prevent removing yourself if you're the last admin
        authorized_users = load_whitelist()
        
        if user_id == ctx.author.id and len(authorized_users) == 1:
            await ctx.send("❌ **Cannot remove yourself as the last authorized user.**")
            return
        
        if user_id not in authorized_users:
            user_display = user.name if user else f"User {user_id}"
            await ctx.send(f"⚠️ **{user_display} is not in the whitelist.**")
            return
        
        # Remove user from whitelist
        authorized_users.discard(user_id)
        
        if save_whitelist(authorized_users):
            if user:
                await ctx.send(f"✅ **Removed {user.name} (`{user_id}`) from whitelist.**")
                print(f"🛡️ {ctx.author} removed {user.name} ({user_id}) from whitelist")
            else:
                await ctx.send(f"✅ **Removed user `{user_id}` from whitelist.**")
                print(f"🛡️ {ctx.author} removed user {user_id} from whitelist")
        else:
            await ctx.send("❌ **Failed to save whitelist.** Please try again.")
    
    elif action == "check":
        if not user_input:
            await ctx.send("❌ **Missing user.** Usage: `!whitelist check <user_id|username>`")
            return
        
        # Resolve user input to user ID
        user_id, user = await resolve_user_id(ctx, user_input)
        
        if user_id is None:
            await ctx.send(f"❌ **User not found:** `{user_input}`\nTry using their exact username or Discord user ID.")
            return
        
        authorized = is_authorized(user_id)
        status = "✅ **Authorized**" if authorized else "❌ **Not Authorized**"
        
        if user:
            await ctx.send(f"{status} - **{user.name}** (`{user_id}`)")
        else:
            await ctx.send(f"{status} - User `{user_id}`")
    
    else:
        await ctx.send("❌ **Invalid action.** Use: `add`, `remove`, or `check`")

@bot.command(name="deps", help="Upgrade Python dependencies (authorized)")
@require_authorization()
async def deps_cmd(ctx, action: str = None):
    """Run pip install -U -r requirements.txt on the host (same Python as the bot)."""
    if not action or action.lower() not in ("update", "upgrade", "sync"):
        await ctx.send(
            "📦 **Usage:** `!deps update` — runs `pip install --upgrade -r requirements.txt` "
            "for this bot’s Python.\n"
            "💡 Enable automatic upgrades on startup with env `GONDOR_AUTO_UPDATE_DEPS=1` "
            "(optional: `GONDOR_AUTO_UPDATE_YTDLP_ONLY=1` for yt-dlp only; "
            "`GONDOR_SKIP_DEP_UPDATE=1` to skip when auto-update is on)."
        )
        return
    await ctx.send("📦 Upgrading dependencies (may take a minute)...")
    from gondor_deps import upgrade_from_requirements

    ok = await asyncio.to_thread(upgrade_from_requirements)
    if ok:
        await ctx.send(
            "✅ **Dependencies updated.** Use `!reboot` so all processes pick up new versions."
        )
    else:
        await ctx.send("❌ **Dependency update failed.** Check the machine console for pip output.")

@bot.command(name="reboot", help="Restart the bot")
@require_authorization()
async def reboot_bot(ctx):
    """Restart the bot - authorized users only"""
    
    embed = discord.Embed(
        title="🔄 Bot Restart",
        description="Restarting the bot...",
        color=0xff9900
    )
    
    embed.add_field(
        name="Status",
        value="✅ Shutdown initiated by authorized user",
        inline=False
    )
    
    embed.add_field(
        name="Restart Process",
        value=(
            "1. Disconnecting from voice channels\n"
            "2. Saving current state\n"
            "3. Shutting down bot process\n"
            "4. Automatic restart (if configured)"
        ),
        inline=False
    )
    
    embed.set_footer(text=f"Requested by {ctx.author.name}")
    
    await ctx.send(embed=embed)
    
    # Log the reboot request
    print(f"🔄 Bot reboot requested by {ctx.author} ({ctx.author.id})")
    
    try:
        # Check if anything is currently playing
        has_active_playback = False
        for guild in bot.guilds:
            if guild.voice_client and (guild.voice_client.is_playing() or guild.voice_client.is_paused()):
                has_active_playback = True
                break
        
        # Save voice channel state before disconnecting
        save_voice_state()
        
        # Save playback state if there's active playback
        if has_active_playback:
            playback_saved = save_playback_state()
            if playback_saved:
                print("🎵 Active playback detected - cleanup will be bypassed to preserve files")
                print("🔄 Playback and queue will be restored after restart")
            else:
                print("⚠️ Failed to save playback state, proceeding with normal cleanup")
                has_active_playback = False
        
        # Disconnect from all voice channels gracefully
        for guild in bot.guilds:
            if guild.voice_client:
                print(f"🔌 Disconnecting from voice channel in {guild.name}")
                await guild.voice_client.disconnect()
        
        # Conditionally clean up based on playback state
        if has_active_playback:
            # Only clear in-memory state, preserve files for restoration
            print("🎵 Preserving playback state - skipping file cleanup")
            current_sources.clear()
            # Don't clear song_queues and playlist_queues - they're saved in playback state
            # Don't clear notification_channels - they're saved in playback state
        else:
            # Normal cleanup when nothing is playing
            current_sources.clear()
            song_queues.clear()
            playlist_queues.clear()
            notification_channels.clear()
            print("🧹 Cleaned up bot state")
        
        print("🔄 Bot shutting down for restart...")
        
        # Close the bot connection
        await bot.close()
        
    except Exception as e:
        print(f"⚠️ Error during graceful shutdown: {e}")
        # Force shutdown if graceful fails
        await bot.close()

@bot.command(name="rejoin", help="Rejoin saved voice channels")
@require_authorization()
async def rejoin_cmd(ctx):
    """Manually rejoin saved voice channels"""
    
    embed = discord.Embed(
        title="🔊 Voice Channel Rejoin",
        description="Attempting to rejoin saved voice channels...",
        color=0x00ff00
    )
    
    await ctx.send(embed=embed)
    
    # Log the rejoin request
    print(f"🔊 Manual rejoin requested by {ctx.author} ({ctx.author.id})")
    
    # Attempt to rejoin channels
    await rejoin_voice_channels()
    
    # Send completion message
    embed = discord.Embed(
        title="✅ Rejoin Complete",
        description="Voice channel rejoin process completed.",
        color=0x00ff00
    )
    
    embed.set_footer(text=f"Requested by {ctx.author.name}")
    await ctx.send(embed=embed)

@bot.command(name="voiceregion", help="Show voice channel region info")
async def voice_region(ctx):
    """Show voice channel region information and explain region changes"""
    
    embed = discord.Embed(
        title="🌍 Voice Channel Region",
        description="Voice channel region information",
        color=0x00ff00
    )
    
    # Get current voice channel info
    if ctx.author.voice and ctx.author.voice.channel:
        voice_channel = ctx.author.voice.channel
        
        # Try to get region info (may not be available in newer Discord API)
        try:
            # This is the old way - may not work in current Discord API
            region = voice_channel.rtc_region if hasattr(voice_channel, 'rtc_region') else None
            if region:
                embed.add_field(
                    name="Current Region",
                    value=f"📍 **{region}**",
                    inline=True
                )
            else:
                embed.add_field(
                    name="Current Region",
                    value="📍 **Auto** (Server default)",
                    inline=True
                )
        except:
            embed.add_field(
                name="Current Region",
                value="📍 **Auto** (Server default)",
                inline=True
            )
        
        embed.add_field(
            name="Voice Channel",
            value=f"🔊 **{voice_channel.name}**",
            inline=True
        )
        
        embed.add_field(
            name="Server",
            value=f"🏠 **{ctx.guild.name}**",
            inline=True
        )
        
    else:
        embed.add_field(
            name="Voice Channel",
            value="❌ **Not in a voice channel**",
            inline=False
        )
    
    # Explain region change situation
    embed.add_field(
        name="⚠️ Region Changes",
        value=(
            "**Voice channel region changes are no longer supported by Discord bots.**\n\n"
            "**To change voice channel region:**\n"
            "1. Right-click the voice channel\n"
            "2. Select 'Edit Channel'\n"
            "3. Go to 'Region Override' section\n"
            "4. Choose your preferred region\n\n"
            "**Available regions:** US East, US West, US South, US Central, Europe (Amsterdam/London/Frankfurt), Asia (Singapore/Hong Kong/Japan), Australia, Brazil"
        ),
        inline=False
    )
    
    embed.add_field(
        name="📋 Available Regions",
        value=(
            "🇺🇸 **US East** (New York)\n"
            "🇺🇸 **US West** (San Francisco)\n"
            "🇺🇸 **US South** (Atlanta)\n"
            "🇺🇸 **US Central** (Chicago)\n"
            "🇪🇺 **Europe** (Amsterdam/London/Frankfurt)\n"
            "🌏 **Asia** (Singapore/Hong Kong/Japan)\n"
            "🇦🇺 **Australia** (Sydney)\n"
            "🇧🇷 **Brazil** (São Paulo)"
        ),
        inline=False
    )
    
    embed.set_footer(text="Region changes require server administrator permissions")
    await ctx.send(embed=embed)

# Run the bot with fresh token
if __name__ == "__main__":
    TOKEN = os.getenv('NEW_DISCORD_TOKEN')
    
    if not TOKEN:
        print("❌ No NEW_DISCORD_TOKEN found!")
        print("💡 Set NEW_DISCORD_TOKEN in .env file")
        exit(1)
    
    from gondor_deps import maybe_auto_update_dependencies

    maybe_auto_update_dependencies()

    print("🚀 Starting Discord Audio Bot...")
    print("🎵 Initializing streaming services...")
    print("📁 Loading audio file management...")
    _ensure_audio_processor_subprocess()
    bot.run(TOKEN)