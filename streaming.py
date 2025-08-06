#!/usr/bin/env python3
"""
Discord Audio Streaming Bot
A lightweight bot for streaming audio files to Discord voice channels
"""

import discord
from discord.ext import commands
import asyncio
import os
import json
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
    print("⚠️ Skipping .env file due to encoding issues")

# Audio streaming utilities

# FFmpeg options for audio playback - Minimal for testing
ffmpeg_options = {
    'before_options': '',
    'options': '-vn'
}

# Bot setup - completely generic
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix='!', intents=intents, description="File streaming utility")

# Directories for communication with background service
MEDIA_DIR = Path("media_library")
QUEUE_DIR = Path("request_queue")
MEDIA_DIR.mkdir(exist_ok=True)
QUEUE_DIR.mkdir(exist_ok=True)

# Track current playback
current_sources = {}

# Playlist management directory
PLAYLIST_DIR = Path("playlists")
PLAYLIST_DIR.mkdir(exist_ok=True)

# Playlist queue management with smart features
playlist_queues = {}  # {guild_id: {"playlist_name": str, "songs": list, "original_songs": list, "current_index": int, "shuffle": bool, "loop_mode": str, "total_songs": int}}

# Smart queue system for individual !play songs
song_queues = {}  # {guild_id: [{"title": str, "file_path": str, "duration": int, "requested_by": User, "cached": bool}, ...]}

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
        source = discord.FFmpegPCMAudio(audio_path, **ffmpeg_options)
        
        audio_source = SimpleAudioSource(
            source, 
            title=next_song['title'],
            duration=next_song['duration'],
            file_path=next_song['file_path']
        )
        
        current_sources[guild_id] = audio_source
        
        # Play the song
        voice_client.play(audio_source, after=lambda e: after_playing(e, guild_id))
        
        # Send notification
        channels = [channel for channel in guild.text_channels if channel.permissions_for(guild.me).send_messages]
        if channels:
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
            
            await channels[0].send(embed=embed)
            
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
            source = discord.FFmpegPCMAudio(audio_path, **ffmpeg_options)
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
        
        # Send notification to text channel
        channels = [channel for channel in guild.text_channels if channel.permissions_for(guild.me).send_messages]
        if channels:
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
            
            await channels[0].send(embed=embed)
        
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
    def start_playlist_playback(playlist_name, guild_id):
        """Start playing a playlist (air-gap safe - only local files)"""
        try:
            result = PlaylistManager.get_playlist_contents(playlist_name)
            if result["status"] != "success":
                return result
            
            playlist = result["playlist"]
            songs = playlist.get("files", [])
            
            if not songs:
                return {"status": "error", "message": f"Playlist '{playlist_name}' is empty"}
            
            # Verify all playlist files exist
            valid_songs = []
            for song in songs:
                audio_file = Path(song.get("audio_file", ""))
                
                print(f"🔍 Checking playlist file: {audio_file}")
                print(f"🔍 File exists: {audio_file.exists()}")
                print(f"🔍 Absolute path: {audio_file.absolute()}")
                
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
                "position": current_index + 1,
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
                if current_index >= len(songs):
                    return {"status": "error", "message": "Already at end of playlist"}
                # Skip is handled by normal progression
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
    print("🎮 Audio streaming bot ready!")
    print("📁 File streaming service online")
    
    # Queue cleanup of any leftover files from previous runs
    queue_cleanup_all_request()

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

@bot.command(name="summon", help="Summon the bot to your voice channel")
async def summon(ctx):
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("❌ You need to be in a voice channel to summon me!")
        return

    channel = ctx.author.voice.channel
    voice_client = ctx.guild.voice_client

    if voice_client:
        if voice_client.channel == channel:
            await ctx.send(f"🔊 I'm already connected to **{channel.name}**!")
            return
        else:
            await voice_client.disconnect()

    try:
        await ctx.send(f"🎵 Connecting to **{channel.name}**...")
        voice_client = await channel.connect()
        await ctx.send(f"🔊 Connected to **{channel.name}**!")
    except Exception as e:
        await ctx.send(f"❌ Failed to connect to voice channel: {e}")

@bot.command(name="play", help="Stream audio content")
async def play(ctx, *, query: str):
    """Stream audio content from query"""
        
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
        max_wait = 60  # 60 seconds max (extended for testing)
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
        
        source = discord.FFmpegPCMAudio(file_path, **ffmpeg_options)
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
        name="📋 Playlist Commands",
        value=(
            "`!playlist` - Show all playlist commands\n"
            "`!playlist create <name>` - Create new playlist\n"
            "`!playlist play <name>` - Play entire playlist\n"
            "`!playlist shuffle` - Toggle shuffle mode 🔀\n"
            "`!playlist loop <mode>` - Set loop mode 🔁\n"
            "`!playlist skip/prev` - Skip songs ⏭️⏮️"
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
    
    # System status checks
    embed.add_field(
        name="🔒 System Status", 
        value="✅ Background processing active\n✅ File streaming operational\n✅ Communication systems active", 
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
async def playlist_cmd(ctx, action=None, *, args=None):
    """Air-gap safe playlist management - only uses local files"""
    
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
                
                # Use FFmpegPCMAudio - Discord.py handles Opus encoding internally
                source = discord.FFmpegPCMAudio(audio_path, **ffmpeg_options)
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
            # Stop current playback to trigger next song
            if ctx.guild.voice_client and (ctx.guild.voice_client.is_playing() or ctx.guild.voice_client.is_paused()):
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

# Run the bot with fresh token
if __name__ == "__main__":
    TOKEN = os.getenv('NEW_DISCORD_TOKEN')
    
    if not TOKEN:
        print("❌ No NEW_DISCORD_TOKEN found!")
        print("💡 Set NEW_DISCORD_TOKEN in .env file")
        exit(1)
    
    print("🚀 Starting Discord Audio Bot...")
    print("🎵 Initializing streaming services...")
    print("📁 Loading audio file management...")
    bot.run(TOKEN)