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
    
    # Sync slash commands
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"❌ Failed to sync commands: {e}")

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

@bot.tree.command(name="play", description="Play music from YouTube")
async def play(interaction: discord.Interaction, search: str):
    """Play music from YouTube with enhanced quality and pre-buffering"""
    
    # Check if user is in voice channel
    if not interaction.user.voice:
        await interaction.response.send_message("❌ You need to be in a voice channel!", ephemeral=True)
        return

    # Defer immediately to prevent timeout
    try:
        await interaction.response.defer()
    except discord.errors.NotFound:
        # Interaction expired, can't respond
        print("⚠️ Interaction expired before we could defer")
        return
    except Exception as e:
        print(f"⚠️ Failed to defer interaction: {e}")
        return

    try:
        # Get voice channel and connect
        voice_channel = interaction.user.voice.channel
        
        if interaction.guild.voice_client is None:
            voice_client = await voice_channel.connect()
            print(f"🔊 Connected to voice channel: {voice_channel.name}")
        else:
            voice_client = interaction.guild.voice_client

        # Stop current playback and cleanup if playing
        if voice_client.is_playing() or voice_client.is_paused():
            voice_client.stop()
            await asyncio.sleep(0.5)  # Give time for cleanup callback

        # Create enhanced audio source with pre-buffering
        print(f"🎵 Processing request with enhanced streaming...")
        source = await YTDLSource.create_source(search, loop=bot.loop, use_prebuffer=True)
        
        # Track current source for cleanup
        current_sources[interaction.guild.id] = source

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
                after=lambda e: after_playing(e, interaction.guild.id, source)
            )
            
            # Verify playback started
            await asyncio.sleep(0.5)
            if voice_client.is_playing():
                duration_str = f" • {source.duration//60}:{source.duration%60:02d}" if source.duration else ""
                try:
                    await interaction.followup.send(f"🎵 **Now playing:** {source.title}{duration_str}")
                except discord.errors.NotFound:
                    print("⚠️ Interaction expired, but playback started successfully")
                print(f"✅ Successfully started: {source.title}")
            else:
                try:
                    await interaction.followup.send(f"❌ Failed to start playback. Trying direct stream...")
                except discord.errors.NotFound:
                    print("⚠️ Interaction expired during retry attempt")
                print("⚠️ Playback failed to start, retrying with direct stream")
                
                # Retry with direct streaming
                if hasattr(source, 'cleanup'):
                    source.cleanup()
                
                source = await YTDLSource.create_source(search, loop=bot.loop, use_prebuffer=False)
                current_sources[interaction.guild.id] = source
                voice_client.play(
                    source, 
                    after=lambda e: after_playing(e, interaction.guild.id, source)
                )
                
                await asyncio.sleep(0.5)
                if voice_client.is_playing():
                    try:
                        await interaction.followup.send(f"🎵 **Now playing:** {source.title} (direct stream)")
                    except discord.errors.NotFound:
                        print("⚠️ Interaction expired, but direct stream started successfully")
                    print(f"✅ Direct stream success: {source.title}")
                else:
                    try:
                        await interaction.followup.send(f"❌ Unable to play this track")
                    except discord.errors.NotFound:
                        print("⚠️ Interaction expired during error response")
                    print("💥 Both pre-buffer and direct stream failed")
                    
        except Exception as play_error:
            print(f"💥 Play error: {play_error}")
            try:
                await interaction.followup.send(f"❌ Playback error: {str(play_error)}")
            except discord.errors.NotFound:
                print("⚠️ Interaction expired during play error response")

    except Exception as e:
        try:
            await interaction.followup.send(f"❌ Error playing music: {str(e)}")
        except discord.errors.NotFound:
            print("⚠️ Interaction expired during error response")
        print(f"💥 Error: {str(e)}")
        
        # Cleanup on error
        guild_id = interaction.guild.id
        if guild_id in current_sources:
            source = current_sources[guild_id]
            if hasattr(source, 'cleanup'):
                source.cleanup()
            current_sources.pop(guild_id, None)

@bot.tree.command(name="stop", description="Stop music and disconnect")
async def stop(interaction: discord.Interaction):
    """Stop music and disconnect with cleanup"""
    try:
        guild_id = interaction.guild.id
        
        if interaction.guild.voice_client:
            # Stop playback and cleanup
            if interaction.guild.voice_client.is_playing() or interaction.guild.voice_client.is_paused():
                interaction.guild.voice_client.stop()
            
            # Manual cleanup if needed
            if guild_id in current_sources:
                source = current_sources[guild_id]
                if hasattr(source, 'cleanup'):
                    source.cleanup(guild_id)
                # Use pop() instead of del to avoid KeyError if already removed
                current_sources.pop(guild_id, None)
            
            # Clean up any pending FFmpeg monitoring
            cleanup_queue.pop(guild_id, None)
            
            await interaction.guild.voice_client.disconnect()
            await interaction.response.send_message("⏹️ Stopped and disconnected!")
        else:
            await interaction.response.send_message("❌ Not connected to voice channel.")
            
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
            await interaction.response.send_message(f"❌ Error stopping: {str(e)}")
        except:
            print("⚠️ Could not send error response for stop command")

@bot.tree.command(name="pause", description="Pause the music")
async def pause(interaction: discord.Interaction):
    """Pause the music"""
    if interaction.guild.voice_client and interaction.guild.voice_client.is_playing():
        interaction.guild.voice_client.pause()
        await interaction.response.send_message("⏸️ Music paused!")
    else:
        await interaction.response.send_message("❌ No music playing.")

@bot.tree.command(name="resume", description="Resume the music")
async def resume(interaction: discord.Interaction):
    """Resume the music"""
    if interaction.guild.voice_client and interaction.guild.voice_client.is_paused():
        interaction.guild.voice_client.resume()
        await interaction.response.send_message("▶️ Music resumed!")
    else:
        await interaction.response.send_message("❌ Music is not paused.")

@bot.tree.command(name="summon", description="Summon the bot to your voice channel")
async def summon(interaction: discord.Interaction):
    """Summon the bot to your voice channel"""
    
    # Check if user is in voice channel
    if not interaction.user.voice:
        await interaction.response.send_message("❌ You need to be in a voice channel to summon me!", ephemeral=True)
        return
    
    user_voice_channel = interaction.user.voice.channel
    
    try:
        # If bot is not connected to any voice channel
        if interaction.guild.voice_client is None:
            voice_client = await user_voice_channel.connect()
            await interaction.response.send_message(f"🔊 **Summoned!** Joined `{user_voice_channel.name}`")
            print(f"🔊 Summoned to voice channel: {user_voice_channel.name}")
        
        # If bot is connected but to a different channel
        elif interaction.guild.voice_client.channel != user_voice_channel:
            await interaction.guild.voice_client.move_to(user_voice_channel)
            await interaction.response.send_message(f"🔊 **Moved!** Now in `{user_voice_channel.name}`")
            print(f"🔊 Moved to voice channel: {user_voice_channel.name}")
        
        # If bot is already in the same channel
        else:
            await interaction.response.send_message(f"✅ I'm already in `{user_voice_channel.name}`!")
            
    except Exception as e:
        await interaction.response.send_message(f"❌ Failed to join voice channel: {str(e)}")
        print(f"💥 Summon error: {str(e)}")

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