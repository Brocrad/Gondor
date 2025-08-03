#!/usr/bin/env python3
"""
Discord Music Bot (Python)
A reliable Discord music bot using yt-dlp for YouTube audio extraction.
"""

import discord
from discord.ext import commands
import yt_dlp
import asyncio
import os
import tempfile
import threading
import time
import functools
import subprocess
import socket
import requests
from dotenv import load_dotenv

# Load environment variables (skip if .env has issues)
try:
    load_dotenv()
except:
    print("⚠️ Skipping .env file due to encoding issues")

# Enhanced YouTube downloader configuration for better quality and buffering
ytdl_format_options = {
    # Prioritize high-quality audio formats
    'format': 'bestaudio[acodec^=opus]/bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    # Enhanced buffering and connection options
    'socket_timeout': 60,
    'retries': 3,
    'fragment_retries': 3,
    'retry_sleep_functions': {'http': lambda n: 2**n, 'fragment': lambda n: 2**n},
    # Pre-download options for better quality
    'prefer_ffmpeg': True,
    'keepvideo': False,
    'extractaudio': True,
    'audioformat': 'best',
    'audioquality': '0',  # Best quality
}

# FFmpeg options for URL streaming
ffmpeg_options_stream = {
    'before_options': (
        '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 '
        '-probesize 10M -analyzeduration 10M -fflags +discardcorrupt'
    ),
    'options': (
        '-vn '  # No video streams
        '-bufsize 1024k '  # Larger buffer for smoother playback
        '-filter:a "volume=0.8" '  # Consistent volume
    ),
}

# FFmpeg options for local file playback
ffmpeg_options_file = {
    'before_options': (
        '-probesize 10M -analyzeduration 10M'
    ),
    'options': (
        '-vn '  # No video streams
        '-filter:a "volume=0.8" '  # Consistent volume
    ),
}

# Default to streaming options for backward compatibility
ffmpeg_options = ffmpeg_options_stream

# Pre-buffering downloader for smoother playback
ytdl_download_options = {
    **ytdl_format_options,
    'outtmpl': 'temp_audio/%(title)s.%(ext)s',
    'keepvideo': False,
}

ytdl = yt_dlp.YoutubeDL(ytdl_format_options)
ytdl_download = yt_dlp.YoutubeDL(ytdl_download_options)

class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5, temp_file=None):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('url')
        self.temp_file = temp_file
        self.duration = data.get('duration', 0)

    def cleanup(self, guild_id=None):
        """Clean up temporary files using event-driven approach"""
        if self.temp_file and os.path.exists(self.temp_file):
            # Try immediate cleanup first (sometimes works)
            try:
                os.remove(self.temp_file)
                print(f"🧹 Cleaned up temporary file: {os.path.basename(self.temp_file)}")
                return
            except PermissionError:
                # File is locked by FFmpeg, use event-driven cleanup
                if guild_id:
                    print(f"🕐 File locked by FFmpeg, monitoring process for cleanup: {os.path.basename(self.temp_file)}")
                    schedule_ffmpeg_cleanup(guild_id, self)
                else:
                    # Fallback to delayed cleanup if no guild_id
                    print(f"🕐 File locked, using delayed cleanup: {os.path.basename(self.temp_file)}")
                    self._schedule_delayed_cleanup()
            except Exception as e:
                print(f"⚠️ Failed to clean up temp file: {e}")
                if guild_id:
                    schedule_ffmpeg_cleanup(guild_id, self)
                else:
                    self._schedule_delayed_cleanup()

    def _schedule_delayed_cleanup(self):
        """Schedule delayed cleanup for locked files"""
        if self.temp_file:
            temp_file_path = self.temp_file  # Capture the path
            filename = os.path.basename(temp_file_path)
            
            def delayed_cleanup():
                import time
                
                # Wait progressively longer periods
                wait_times = [10, 20, 30, 60]  # 10s, 20s, 30s, 60s
                
                for i, wait_time in enumerate(wait_times):
                    time.sleep(wait_time)
                    
                    try:
                        if os.path.exists(temp_file_path):
                            os.remove(temp_file_path)  
                            print(f"🧹 Delayed cleanup successful after {sum(wait_times[:i+1])}s: {filename}")
                            return
                        else:
                            # File already gone, someone else cleaned it up
                            return
                    except:
                        if i < len(wait_times) - 1:
                            print(f"🕐 Still locked, will retry in {wait_times[i+1]}s: {filename}")
                        continue
                
                # Final attempt - if this fails, give up gracefully
                print(f"📁 File will be cleaned up on next bot restart: {filename}")
            
            cleanup_thread = threading.Thread(target=delayed_cleanup, daemon=True)
            cleanup_thread.start()

    @classmethod
    async def create_source(cls, search: str, *, loop=None, use_prebuffer=True):
        """Extract audio from YouTube with enhanced quality and pre-buffering"""
        loop = loop or asyncio.get_event_loop()
        
        print(f"🔍 Searching YouTube for: {search}")
        
        try:
            # First, get video info without downloading
            data = await loop.run_in_executor(None, lambda: ytdl.extract_info(f"ytsearch:{search}", download=False))
            
            if 'entries' in data:
                data = data['entries'][0]
            
            print(f"✅ Found: {data['title']}")
            duration_str = f" ({data.get('duration', 0)//60}:{data.get('duration', 0)%60:02d})" if data.get('duration') else ""
            print(f"🎵 Quality: {data.get('acodec', 'unknown')} • {data.get('abr', 'unknown')}kbps{duration_str}")
            
            # Try pre-buffering for better quality (download to temp file)
            if use_prebuffer and data.get('duration') and data.get('duration') < 1800:  # Only for songs < 30 min
                try:
                    print(f"⬇️ Pre-buffering for smoother playback...")
                    
                    # Create temp directory if it doesn't exist
                    temp_dir = os.path.join(os.getcwd(), 'temp_audio')
                    os.makedirs(temp_dir, exist_ok=True)
                    
                    # Download to temporary file
                    temp_file = await loop.run_in_executor(None, cls._download_audio, data['webpage_url'])
                    
                    if temp_file and os.path.exists(temp_file):
                        print(f"✅ Pre-buffered: {os.path.basename(temp_file)}")
                        source = discord.FFmpegPCMAudio(temp_file, **ffmpeg_options_file)
                        return cls(source, data=data, temp_file=temp_file)
                        
                except Exception as prebuffer_error:
                    print(f"⚠️ Pre-buffering failed, using direct stream: {str(prebuffer_error)}")
            
            # Fallback to direct streaming with enhanced quality
            print(f"🌐 Using direct stream with enhanced quality...")
            
            # Get the best audio URL with retries
            audio_url = await cls._get_best_audio_url(data, loop)
            source = discord.FFmpegPCMAudio(audio_url, **ffmpeg_options_stream)
            return cls(source, data=data)
            
        except Exception as e:
            print(f"❌ Error creating audio source: {str(e)}")
            raise e

    @staticmethod
    def _download_audio(url):
        """Download audio to temporary file"""
        try:
            temp_dir = os.path.join(os.getcwd(), 'temp_audio')
            os.makedirs(temp_dir, exist_ok=True)
            
            # Create a simpler filename using tempfile
            temp_fd, temp_filename = tempfile.mkstemp(suffix='.%(ext)s', dir=temp_dir)
            os.close(temp_fd)  # Close the file descriptor
            os.remove(temp_filename)  # Remove the placeholder file
            
            print(f"⬇️ Downloading to: {os.path.basename(temp_filename)}")
            
            # Configure downloader with cleaner options
            download_opts = {
                'format': 'bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best',
                'outtmpl': temp_filename,
                'restrictfilenames': True,
                'noplaylist': True,
                'nocheckcertificate': True,
                'ignoreerrors': False,
                'quiet': False,  # Show yt-dlp output for debugging
                'no_warnings': False,
                'extractaudio': False,  # Don't convert, keep original format
                'keepvideo': False,
                'writeinfojson': False,
                'writedescription': False,
                'writesubtitles': False,
                'writeautomaticsub': False,
                'writethumbnail': False,
            }
            
            print(f"🎵 yt-dlp downloading from: {url}")
            with yt_dlp.YoutubeDL(download_opts) as ydl:
                ydl.download([url])
            
            # Find the actual downloaded file (yt-dlp might change the extension)
            actual_files = []
            base_name = temp_filename.replace('.%(ext)s', '')
            
            for ext in ['.webm', '.m4a', '.mp4', '.opus']:
                potential_file = base_name + ext
                if os.path.exists(potential_file):
                    actual_files.append(potential_file)
            
            if actual_files:
                actual_file = actual_files[0]  # Use the first found file
                file_size = os.path.getsize(actual_file)
                print(f"✅ Download complete: {os.path.basename(actual_file)} ({file_size/1024:.1f}KB)")
                
                if file_size > 1000:  # At least 1KB
                    return actual_file
                else:
                    print(f"⚠️ Downloaded file too small ({file_size} bytes), removing")
                    os.remove(actual_file)
                    return None
            else:
                print("❌ No downloaded file found")
                return None
                
        except Exception as e:
            print(f"❌ Download failed: {str(e)}")
            print(f"❌ Exception type: {type(e).__name__}")
            # Clean up any partial files
            try:
                if 'temp_filename' in locals():
                    base_name = temp_filename.replace('.%(ext)s', '')
                    for ext in ['.webm', '.m4a', '.mp4', '.opus']:
                        potential_file = base_name + ext
                        if os.path.exists(potential_file):
                            os.remove(potential_file)
            except:
                pass
            return None

    @staticmethod
    async def _get_best_audio_url(data, loop):
        """Get the best audio URL with enhanced format selection"""
        try:
            # Try to get the highest quality audio format
            formats = data.get('formats', [])
            
            # Filter and sort audio formats by quality
            audio_formats = [f for f in formats if f.get('acodec') and f.get('acodec') != 'none']
            
            if audio_formats:
                # Prefer opus, then webm, then m4a, prioritizing higher bitrates
                def format_score(fmt):
                    codec_score = {'opus': 3, 'aac': 2, 'mp3': 1}.get(fmt.get('acodec', '').split('.')[0], 0)
                    bitrate_score = fmt.get('abr', 0) or 0
                    return codec_score * 1000 + bitrate_score
                
                best_format = max(audio_formats, key=format_score)
                if best_format.get('url'):
                    return best_format['url']
            
            # Fallback to original URL
            return data.get('url')
            
        except Exception as e:
            print(f"⚠️ Format selection failed, using fallback: {str(e)}")
            return data.get('url')

# Bot setup
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix='!', intents=intents)

# Global dictionary to track current sources for cleanup
current_sources = {}

# Event-driven cleanup system
cleanup_queue = {}  # Maps guild_id -> source awaiting cleanup
ffmpeg_monitor = {}  # Maps guild_id -> FFmpeg process monitoring info

def require_voice_connection():
    """Decorator to require bot to be in voice channel before executing command"""
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            # First argument should be ctx (context)
            ctx = args[0] if args else kwargs.get('ctx')
            if not ctx.guild.voice_client:
                await ctx.send("❌ I need to be summoned first! Use `!summon` to bring me to your voice channel.")
                return
            return await func(*args, **kwargs)
        return wrapper
    return decorator

def schedule_ffmpeg_cleanup(guild_id, source):
    """Schedule cleanup after FFmpeg process terminates"""
    cleanup_queue[guild_id] = source
    start_time = time.time()  # Track timing
    
    # Start monitoring thread for this guild's FFmpeg process
    def monitor_ffmpeg():
        # Wait for the voice client to exist and have a process
        attempts = 0
        monitor_start = time.time()
        
        while attempts < 30:  # Wait up to 30 seconds
            try:
                # Access the bot's voice client for this guild
                voice_client = None
                for vc in bot.voice_clients:
                    if vc.guild.id == guild_id:
                        voice_client = vc
                        break
                
                if voice_client and hasattr(voice_client, '_player') and voice_client._player:
                    # Monitor the FFmpeg process directly
                    player = voice_client._player
                    if hasattr(player, '_process') and player._process:
                        process = player._process
                        detection_time = time.time() - monitor_start
                        print(f"🔍 Monitoring FFmpeg process {process.pid} for cleanup (detected in {detection_time:.1f}s)")
                        
                        # Wait for process to terminate
                        wait_start = time.time()
                        process.wait()  # This blocks until process terminates
                        termination_time = time.time() - wait_start
                        
                        # Process terminated, now safe to cleanup
                        time.sleep(1)  # Small delay to ensure file handles are released
                        
                        if guild_id in cleanup_queue:
                            source_to_cleanup = cleanup_queue.pop(guild_id)
                            total_time = time.time() - start_time
                            print(f"🎯 FFmpeg process terminated after {termination_time:.1f}s, triggering cleanup (total: {total_time:.1f}s)")
                            
                            # Try cleanup now that process is dead
                            if source_to_cleanup.temp_file and os.path.exists(source_to_cleanup.temp_file):
                                cleanup_start = time.time()
                                try:
                                    os.remove(source_to_cleanup.temp_file)
                                    cleanup_time = time.time() - cleanup_start
                                    print(f"🧹 Event-driven cleanup successful in {cleanup_time:.3f}s: {os.path.basename(source_to_cleanup.temp_file)}")
                                except Exception as e:
                                    print(f"⚠️ Event-driven cleanup failed: {e}")
                        return
                        
            except Exception as e:
                print(f"⚠️ FFmpeg monitoring error: {e}")
            
            time.sleep(1)
            attempts += 1
        
        # Fallback if monitoring failed
        print(f"⚠️ FFmpeg monitoring timeout for guild {guild_id}, using delayed cleanup")
        if guild_id in cleanup_queue:
            source_to_cleanup = cleanup_queue.pop(guild_id)
            source_to_cleanup._schedule_delayed_cleanup()
    
    # Start monitoring in background thread
    monitor_thread = threading.Thread(target=monitor_ffmpeg, daemon=True)
    monitor_thread.start()

def after_playing(error, guild_id, source):
    """Callback function after audio finishes playing"""
    if error:
        print(f"💥 Playback error in guild {guild_id}: {error}")
        print(f"💥 Error type: {type(error).__name__}")
    else:
        print(f"✅ Finished playing: {source.title}")
    
    # Clean up temporary files using event-driven approach
    if hasattr(source, 'cleanup'):
        source.cleanup(guild_id)
    
    # Remove from tracking (use pop to avoid KeyError)
    current_sources.pop(guild_id, None)

@bot.event
async def on_ready():
    print(f"✅ {bot.user.name} (Python) is online and ready!")
    print(f"🎵 Music bot connected to {len(bot.guilds)} servers")
    
    # Clean up any leftover temp files from previous runs
    cleanup_temp_files()
    
    print("🎮 Commands ready! Use !summon first, then !commands to see all available commands")

@bot.event
async def on_voice_state_update(member, before, after):
    """Track voice state changes for debugging"""
    # Only track our bot's voice state changes
    if member == bot.user:
        if before.channel != after.channel:
            if after.channel:
                print(f"🔊 Bot joined voice channel: {after.channel.name}")
            elif before.channel:
                print(f"👋 Bot left voice channel: {before.channel.name}")
            else:
                print(f"🔄 Bot voice state changed")
        
        # Additional debugging info
        if after.channel:
            print(f"🔍 Voice state - Self deaf: {after.self_deaf}, Self mute: {after.self_mute}")
            print(f"🔍 Voice state - Server deaf: {after.deaf}, Server mute: {after.mute}")

@bot.event  
async def on_disconnect():
    """Track when bot disconnects from Discord"""
    print("🔌 Bot disconnected from Discord")

@bot.event
async def on_resumed():
    """Track when bot reconnects to Discord"""
    print("🔌 Bot reconnected to Discord")

def cleanup_temp_files():
    """Clean up any leftover temporary files with persistence"""
    try:
        temp_dir = os.path.join(os.getcwd(), 'temp_audio')
        if os.path.exists(temp_dir):
            files = os.listdir(temp_dir)
            if files:
                print(f"🧹 Cleaning up {len(files)} leftover temp files...")
                cleaned = 0
                stubborn = 0
                
                for file in files:
                    file_path = os.path.join(temp_dir, file)
                    
                    # Try multiple times to clean each file
                    for attempt in range(3):
                        try:
                            os.remove(file_path)
                            cleaned += 1
                            break
                        except:
                            if attempt < 2:
                                import time
                                time.sleep(1)  # Wait 1 second between attempts
                            else:
                                stubborn += 1
                
                if cleaned > 0:
                    print(f"✅ Cleaned up {cleaned} temp files")
                if stubborn > 0:
                    print(f"📁 {stubborn} files still locked (will cleanup later)")
                    
    except Exception as e:
        print(f"⚠️ Error during temp file cleanup: {e}")

@bot.command(name="play", help="Play music from YouTube")
@require_voice_connection()
async def play(ctx: commands.Context, *, search: str):
    """Play music from YouTube with enhanced quality and pre-buffering"""
    
    # Check if user is in voice channel
    if not ctx.author.voice:
        await ctx.send("❌ You need to be in a voice channel!")
        return

    try:
        # Get voice channel and connect
        voice_channel = ctx.author.voice.channel
        
        if ctx.guild.voice_client is None:
            voice_client = await voice_channel.connect()
            print(f"🔊 Connected to voice channel: {voice_channel.name}")
        else:
            voice_client = ctx.guild.voice_client

        # Stop current playback and cleanup if playing
        if voice_client.is_playing() or voice_client.is_paused():
            voice_client.stop()
            await asyncio.sleep(0.5)  # Give time for cleanup callback

        # Create enhanced audio source with pre-buffering
        print(f"🎵 Processing request with enhanced streaming...")
        source = await YTDLSource.create_source(search, loop=bot.loop, use_prebuffer=True)
        
        # Track current source for cleanup
        current_sources[ctx.guild.id] = source

        # Validate source before playing (with more lenient file size check)
        if hasattr(source, 'temp_file') and source.temp_file:
            if os.path.exists(source.temp_file):
                file_size = os.path.getsize(source.temp_file)
                print(f"📊 Pre-buffered file: {os.path.basename(source.temp_file)} ({file_size/1024/1024:.2f}MB)")
                
                # Only reject if file is completely empty (0 bytes) or suspiciously tiny
                if file_size < 100:  # Less than 100 bytes suggests a real problem
                    print("⚠️ Pre-buffered file appears corrupted, switching to direct stream")
                    # Recreate source with direct streaming
                    if hasattr(source, 'cleanup'):
                        source.cleanup()
                    source = await YTDLSource.create_source(search, loop=bot.loop, use_prebuffer=False)
                else:
                    print(f"✅ Using pre-buffered file ({file_size/1024:.1f}KB)")
            else:
                print("⚠️ Pre-buffered file missing, switching to direct stream")
                source = await YTDLSource.create_source(search, loop=bot.loop, use_prebuffer=False)
        
        # Play audio with cleanup callback
        try:
            print(f"🎵 Starting playback...")
            voice_client.play(
                source, 
                after=lambda e: after_playing(e, ctx.guild.id, source)
            )
            
            # Verify playback started
            await asyncio.sleep(0.5)
            if voice_client.is_playing():
                duration_str = f" • {source.duration//60}:{source.duration%60:02d}" if source.duration else ""
                try:
                    await ctx.send(f"🎵 **Now playing:** {source.title}{duration_str}")
                except discord.errors.NotFound:
                    print("⚠️ Interaction expired, but playback started successfully")
                print(f"✅ Successfully started: {source.title}")
            else:
                try:
                    await ctx.send(f"❌ Failed to start playback. Trying direct stream...")
                except discord.errors.NotFound:
                    print("⚠️ Interaction expired during retry attempt")
                print("⚠️ Playback failed to start, retrying with direct stream")
                
                # Retry with direct streaming
                if hasattr(source, 'cleanup'):
                    source.cleanup()
                
                source = await YTDLSource.create_source(search, loop=bot.loop, use_prebuffer=False)
                current_sources[ctx.guild.id] = source
                voice_client.play(
                    source, 
                    after=lambda e: after_playing(e, ctx.guild.id, source)
                )
                
                await asyncio.sleep(0.5)
                if voice_client.is_playing():
                    try:
                        await ctx.send(f"🎵 **Now playing:** {source.title} (direct stream)")
                    except discord.errors.NotFound:
                        print("⚠️ Interaction expired, but direct stream started successfully")
                    print(f"✅ Direct stream success: {source.title}")
                else:
                    try:
                        await ctx.send(f"❌ Unable to play this track")
                    except discord.errors.NotFound:
                        print("⚠️ Interaction expired during error response")
                    print("💥 Both pre-buffer and direct stream failed")
                    
        except Exception as play_error:
            print(f"💥 Play error: {play_error}")
            try:
                await ctx.send(f"❌ Playback error: {str(play_error)}")
            except discord.errors.NotFound:
                print("⚠️ Interaction expired during play error response")

    except Exception as e:
        try:
            await ctx.send(f"❌ Error playing music: {str(e)}")
        except discord.errors.NotFound:
            print("⚠️ Interaction expired during error response")
        print(f"💥 Error: {str(e)}")
        
        # Cleanup on error
        guild_id = ctx.guild.id
        if guild_id in current_sources:
            source = current_sources[guild_id]
            if hasattr(source, 'cleanup'):
                source.cleanup()
            current_sources.pop(guild_id, None)

@bot.command(name="stop", help="Stop the current song")
@require_voice_connection()
async def stop(ctx: commands.Context):
    """Stop current song but stay in voice channel"""
    try:
        guild_id = ctx.guild.id
        
        if ctx.guild.voice_client:
            # Stop playback and cleanup
            if ctx.guild.voice_client.is_playing() or ctx.guild.voice_client.is_paused():
                ctx.guild.voice_client.stop()
                
                # Manual cleanup if needed
                if guild_id in current_sources:
                    source = current_sources[guild_id]
                    if hasattr(source, 'cleanup'):
                        source.cleanup(guild_id)
                    # Use pop() instead of del to avoid KeyError if already removed
                    current_sources.pop(guild_id, None)
                
                # Clean up any pending FFmpeg monitoring
                cleanup_queue.pop(guild_id, None)
                
                await ctx.send("⏹️ Stopped the current song!")
            else:
                await ctx.send("❌ No music is currently playing.")
        else:
            await ctx.send("❌ Not connected to voice channel.")
            
    except discord.errors.HTTPException as e:
        if e.code == 40060:  # Interaction already acknowledged
            print("⚠️ Stop command interaction already acknowledged, but command executed")
        else:
            print(f"⚠️ Discord HTTP error in stop command: {e}")
    except discord.errors.NotFound:
        print("⚠️ Stop command interaction expired")
    except Exception as e:
        print(f"💥 Error in stop command: {e}")
        try:
            await ctx.send(f"❌ Error stopping: {str(e)}")
        except:
            print("⚠️ Could not send error response for stop command")

@bot.command(name="kill", help="Disconnect the bot from voice channel")
async def kill(ctx: commands.Context):
    """Disconnect the bot from voice channel"""
    try:
        guild_id = ctx.guild.id
        
        if ctx.guild.voice_client:
            # Stop any current playback first
            if ctx.guild.voice_client.is_playing() or ctx.guild.voice_client.is_paused():
                ctx.guild.voice_client.stop()
            
            # Manual cleanup if needed
            if guild_id in current_sources:
                source = current_sources[guild_id]
                if hasattr(source, 'cleanup'):
                    source.cleanup(guild_id)
                current_sources.pop(guild_id, None)
            
            # Clean up any pending FFmpeg monitoring
            cleanup_queue.pop(guild_id, None)
            
            # Disconnect from voice channel
            await ctx.guild.voice_client.disconnect()
            await ctx.send("👋 Disconnected from voice channel!")
        else:
            await ctx.send("❌ Not connected to voice channel.")
            
    except Exception as e:
        print(f"💥 Error in kill command: {e}")
        try:
            await ctx.send(f"❌ Error disconnecting: {str(e)}")
        except:
            print("⚠️ Could not send error response for kill command")

@bot.command(name="commands", help="Show all available commands")
async def commands_list(ctx: commands.Context):
    """Show all available commands"""
    help_embed = discord.Embed(
        title="🎵 Gondor Music Bot Commands",
        description="⚠️ **Important:** Use `!summon` first to bring me to your voice channel!\n\nYour advanced Discord music bot with event-driven cleanup!",
        color=0x00ff00
    )
    
    help_embed.add_field(
        name="🎶 Music Commands (Require !summon first)",
        value=(
            "`!play <search or url>` - Play music from YouTube\n"
            "`!pause` - Pause the current song\n"
            "`!resume` - Resume playback\n"
            "`!stop` - Stop current song (bot stays in channel)"
        ),
        inline=False
    )
    
    help_embed.add_field(
        name="🔧 Control Commands", 
        value=(
            "`!summon` - Summon bot to your voice channel\n"
            "`!kill` - Disconnect bot from voice channel 💀\n"
            "`!commands` - Show this commands list"
        ),
        inline=False
    )
    
    help_embed.add_field(
        name="✨ Features",
        value=(
            "• **Pre-buffering** for smooth playback\n"
            "• **Event-driven cleanup** system\n"
            "• **High-quality audio** (Opus preferred)\n"
            "• **Smart file management** on Windows"
        ),
        inline=False
    )
    
    help_embed.set_footer(text="Use !commands to see this message anytime • Made with ❤️")
    
    try:
        await ctx.send(embed=help_embed)
    except Exception as e:
        # Fallback to plain text if embeds don't work
        help_text = """
🎵 **Gondor Music Bot Commands**

⚠️ **Important:** Use `!summon` first to bring me to your voice channel!

**🎶 Music Commands (Require !summon first):**
`!play <search or url>` - Play music from YouTube
`!pause` - Pause the current song
`!resume` - Resume playback
`!stop` - Stop current song (bot stays in channel)

**🔧 Control Commands:**
`!summon` - Summon bot to your voice channel
`!kill` - Disconnect bot from voice channel 💀
`!commands` - Show this commands list

**✨ Features:** Pre-buffering, Event-driven cleanup, High-quality audio
        """
        await ctx.send(help_text)

@bot.command(name="pause", help="Pause the music")
@require_voice_connection()
async def pause(ctx: commands.Context):
    """Pause the music"""
    if ctx.guild.voice_client and ctx.guild.voice_client.is_playing():
        ctx.guild.voice_client.pause()
        await ctx.send("⏸️ Music paused!")
    else:
        await ctx.send("❌ No music playing.")

@bot.command(name="resume", help="Resume the music")
@require_voice_connection()
async def resume(ctx: commands.Context):
    """Resume the music"""
    if ctx.guild.voice_client and ctx.guild.voice_client.is_paused():
        ctx.guild.voice_client.resume()
        await ctx.send("▶️ Music resumed!")
    else:
        await ctx.send("❌ Music is not paused.")

@bot.command(name="summon", help="Summon the bot to your voice channel")
async def summon(ctx: commands.Context):
    """Summon the bot to your voice channel with Windows-optimized connection"""
    
    # Check if user is in voice channel
    if not ctx.author.voice:
        await ctx.send("❌ You need to be in a voice channel to summon me!")
        return
    
    user_voice_channel = ctx.author.voice.channel
    
    # Clean up any existing broken connections first
    if ctx.guild.voice_client:
        try:
            if not ctx.guild.voice_client.is_connected():
                print("🧹 Cleaning up broken voice connection...")
                await ctx.guild.voice_client.disconnect(force=True)
                await asyncio.sleep(1)  # Wait for cleanup
        except Exception as cleanup_e:
            print(f"🧹 Cleanup error (ignoring): {cleanup_e}")
    
    try:
        # If bot is not connected to any voice channel
        if ctx.guild.voice_client is None:
            connecting_msg = await ctx.send(f"🔄 Connecting to `{user_voice_channel.name}`...")
            
            # Windows-specific connection with timeout and retry logic
            connection_timeout = 10  # seconds
            max_attempts = 3
            
            for attempt in range(max_attempts):
                try:
                    print(f"🔄 Connection attempt {attempt + 1}/{max_attempts}")
                    
                    # Create connection with Windows-specific options
                    voice_client = await asyncio.wait_for(
                        user_voice_channel.connect(
                            timeout=connection_timeout,
                            reconnect=True,  # Enable automatic reconnection
                            self_deaf=False,  # Ensure not deafened
                            self_mute=False   # Ensure not muted
                        ),
                        timeout=connection_timeout + 5
                    )
                    
                    print(f"🔊 Voice client created: {voice_client}")
                    print(f"🔊 Connected to: {voice_client.channel.name if voice_client.channel else 'Unknown'}")
                    print(f"🔊 Is connected: {voice_client.is_connected()}")
                    
                    # Extended wait and stability check
                    print("⏳ Checking connection stability...")
                    await asyncio.sleep(3)
                    
                    # Multiple stability checks
                    stable = True
                    for check in range(3):
                        await asyncio.sleep(1)
                        if not (ctx.guild.voice_client and ctx.guild.voice_client.is_connected()):
                            stable = False
                            print(f"❌ Stability check {check + 1} failed")
                            break
                        else:
                            print(f"✅ Stability check {check + 1} passed")
                    
                    if stable:
                        await connecting_msg.edit(content=f"🔊 **Successfully joined** `{user_voice_channel.name}`! Connection stable.")
                        print(f"✅ Stable connection established to: {user_voice_channel.name}")
                        return
                    else:
                        print(f"⚠️ Connection unstable on attempt {attempt + 1}")
                        if ctx.guild.voice_client:
                            await ctx.guild.voice_client.disconnect(force=True)
                        await asyncio.sleep(2)  # Wait before retry
                        
                except asyncio.TimeoutError:
                    print(f"⏰ Connection timeout on attempt {attempt + 1}")
                    if ctx.guild.voice_client:
                        try:
                            await ctx.guild.voice_client.disconnect(force=True)
                        except:
                            pass
                    await asyncio.sleep(2)
                    
                except Exception as attempt_e:
                    print(f"💥 Connection error on attempt {attempt + 1}: {attempt_e}")
                    if ctx.guild.voice_client:
                        try:
                            await ctx.guild.voice_client.disconnect(force=True)
                        except:
                            pass
                    await asyncio.sleep(2)
            
            # If we get here, all attempts failed - try alternative method
            print("🔄 Trying alternative connection method...")
            try:
                # Alternative connection method for problematic networks
                voice_client = await user_voice_channel.connect(timeout=30)
                
                # Don't check stability, just accept the connection
                await connecting_msg.edit(content=f"🔊 **Connected** to `{user_voice_channel.name}` (alternative method)")
                print(f"✅ Alternative connection successful to: {user_voice_channel.name}")
                return
                
            except Exception as alt_e:
                print(f"💥 Alternative connection failed: {alt_e}")
                await connecting_msg.edit(content=f"❌ **All connection methods failed** for `{user_voice_channel.name}`. This may be a network/firewall issue.")
                print(f"❌ All connection methods failed for: {user_voice_channel.name}")
        
        # If bot is connected but to a different channel
        elif ctx.guild.voice_client.channel != user_voice_channel:
            await ctx.guild.voice_client.move_to(user_voice_channel)
            await ctx.send(f"🔊 **Moved!** Now in `{user_voice_channel.name}`")
            print(f"🔊 Moved to voice channel: {user_voice_channel.name}")
        
        # If bot is already in the same channel
        else:
            # Double-check connection status
            if ctx.guild.voice_client.is_connected():
                await ctx.send(f"✅ I'm already in `{user_voice_channel.name}`!")
            else:
                await ctx.send(f"⚠️ I think I'm in `{user_voice_channel.name}` but connection seems broken. Try `!kill` then `!summon` again.")
                print(f"⚠️ Voice client exists but is not connected in: {user_voice_channel.name}")
            
    except Exception as e:
        await ctx.send(f"❌ Failed to join voice channel: {str(e)}")
        print(f"💥 Summon error: {str(e)}")
        print(f"💥 Error type: {type(e).__name__}")
        
        # Additional debugging info
        try:
            print(f"🔍 Guild voice client: {ctx.guild.voice_client}")
            if ctx.guild.voice_client:
                print(f"🔍 Voice client channel: {ctx.guild.voice_client.channel}")
                print(f"🔍 Voice client connected: {ctx.guild.voice_client.is_connected()}")
        except Exception as debug_e:
            print(f"🔍 Debug error: {debug_e}")

@bot.command(name="diagnose", help="Run diagnostics to check bot functionality")
async def diagnose(ctx):
    """Run diagnostic tests"""
    
    embed = discord.Embed(title="🔧 Bot Diagnostics", color=0x00ff00)
    
    # Check FFmpeg
    try:
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
    
    await ctx.send(embed=embed)

@bot.command(name="nettest", help="Test network connectivity to Discord voice servers")
async def network_test(ctx):
    """Test network connectivity to Discord voice servers"""
    
    embed = discord.Embed(title="🌐 Network Connectivity Test", color=0xff9900)
    
    # Test DNS resolution for Discord voice servers
    try:
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
    
    # Voice connection troubleshooting tips
    tips = [
        "🔥 **Try temporarily disabling Windows Firewall**",
        "🌐 **Check if other Discord bots work on your network**", 
        "📡 **Try using a VPN to test if it's ISP-related**",
        "🔄 **Restart your router/modem**",
        "⚙️ **Check Windows Defender settings**"
    ]
    
    embed.add_field(name="Troubleshooting Tips", value="\n".join(tips), inline=False)
    
    await ctx.send(embed=embed)

# Run the bot
if __name__ == "__main__":
    TOKEN = os.getenv('DISCORD_TOKEN')
    
    # If no token from env, show error
    if not TOKEN:
        print("❌ No Discord token found! Please set DISCORD_TOKEN in .env file")
        print("💡 Create a .env file with: DISCORD_TOKEN=your_bot_token_here")
        exit(1)
    
    if TOKEN:
        print("🚀 Starting Python Discord Music Bot...")
        bot.run(TOKEN)
    else:
        print("❌ No Discord token found")