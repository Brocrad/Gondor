#!/usr/bin/env python3
"""
Audio Processing Service (Background Service)
This service handles ALL YouTube processing - completely separate from Discord
Discord never sees yt-dlp, YouTube APIs, or audio processing
"""

import yt_dlp
import os
import json
import time
import hashlib
import asyncio
import signal
import sys
import threading
import atexit
from pathlib import Path

# Windows: allow emoji in print() (same issue as streaming bot)
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _youtube_ytdlp_opts():
    """Options that reduce YouTube 403 / SABR / missing-URL issues vs default web client."""
    return {
        'extractor_args': {
            'youtube': {
                # android/ios often avoid web-only SABR + JS player requirements
                'player_client': ['android', 'web', 'ios'],
            },
        },
        'retries': 5,
        'fragment_retries': 5,
        'noplaylist': True,
    }


class SmartMediaCleaner:
    """Air-gapped media cleanup - Discord never sees this"""
    def __init__(self, media_dir="media_library", max_cache_size_mb=100, max_age_days=3):
        self.media_dir = Path(media_dir)
        self.max_cache_size = max_cache_size_mb * 1024 * 1024  # Convert to bytes
        self.max_age_seconds = max_age_days * 24 * 60 * 60
        
    def cleanup_by_age(self):
        """Remove files older than max_age_days (protects playlist files)"""
        cleaned_files = []
        current_time = time.time()
        
        for webm_file in self.media_dir.glob("*.webm"):
            file_age = current_time - webm_file.stat().st_mtime
            
            if file_age > self.max_age_seconds:
                # Skip if file is referenced in any playlist
                if self._is_file_in_playlist(webm_file):
                    continue
                
                json_file = webm_file.with_suffix('.json')
                
                try:
                    webm_size = webm_file.stat().st_size
                    webm_file.unlink()
                    if json_file.exists():
                        json_file.unlink()
                    
                    cleaned_files.append({
                        'file': webm_file.name,
                        'size_mb': webm_size / (1024 * 1024),
                        'age_days': file_age / (24 * 60 * 60)
                    })
                except Exception as e:
                    print(f"⚠️ Cleanup failed {webm_file.name}: {e}")
        
        return cleaned_files
    
    def _is_file_in_playlist(self, file_path):
        """Check if a file is referenced in any playlist (protects both original and copied files)"""
        try:
            playlist_dir = Path("playlists")
            if not playlist_dir.exists():
                return False
            
            file_str = str(file_path)
            file_name = Path(file_path).name
            
            for playlist_file in playlist_dir.glob("*.json"):
                try:
                    with open(playlist_file, 'r') as f:
                        playlist_data = json.load(f)
                    
                    for song in playlist_data.get("files", []):
                        # Check if this is the exact file path
                        if song.get("audio_file") == file_str:
                            return True
                        
                        # Check if this is the original file that was copied to playlist
                        # (compare filenames to catch the original before it gets deleted)
                        playlist_file_name = Path(song.get("audio_file", "")).name
                        if playlist_file_name == file_name:
                            return True
                            
                except Exception:
                    continue
            
            return False
        except Exception:
            return False
    
    def cleanup_by_size(self):
        """Remove oldest files to stay under max_cache_size"""
        webm_files = []
        for webm_file in self.media_dir.glob("*.webm"):
            stat = webm_file.stat()
            webm_files.append((webm_file, stat.st_mtime, stat.st_size))
        
        webm_files.sort(key=lambda x: x[1])  # Sort by modification time
        current_size = sum(size for _, _, size in webm_files)
        cleaned_files = []
        
        while current_size > self.max_cache_size and webm_files:
            file_path, mod_time, file_size = webm_files.pop(0)
            
            # Skip if file is referenced in any playlist
            if self._is_file_in_playlist(file_path):
                continue
            
            json_file = file_path.with_suffix('.json')
            
            try:
                file_path.unlink()
                if json_file.exists():
                    json_file.unlink()
                
                current_size -= file_size
                cleaned_files.append({
                    'file': file_path.name,
                    'size_mb': file_size / (1024 * 1024)
                })
            except Exception as e:
                print(f"⚠️ Size cleanup failed {file_path.name}: {e}")
        
        return cleaned_files
    
    def cleanup_duplicates(self):
        """Remove duplicate downloads (same video_id, keep newest)"""
        song_groups = {}
        
        for json_file in self.media_dir.glob("*.json"):
            try:
                with open(json_file) as f:
                    metadata = json.load(f)
                
                video_id = metadata.get('video_id', '')
                if video_id:
                    if video_id not in song_groups:
                        song_groups[video_id] = []
                    
                    webm_file = json_file.with_suffix('.webm')
                    if webm_file.exists():
                        song_groups[video_id].append({
                            'json_file': json_file,
                            'webm_file': webm_file,
                            'timestamp': metadata.get('processed_time', 0)
                        })
            except Exception:
                continue
        
        cleaned_files = []
        for video_id, files in song_groups.items():
            if len(files) > 1:
                files.sort(key=lambda x: x['timestamp'], reverse=True)
                
                for old_file in files[1:]:  # Remove all but newest
                    try:
                        size = old_file['webm_file'].stat().st_size
                        old_file['webm_file'].unlink()
                        old_file['json_file'].unlink()
                        
                        cleaned_files.append({
                            'file': old_file['webm_file'].name,
                            'size_mb': size / (1024 * 1024)
                        })
                    except Exception:
                        continue
        
        return cleaned_files
    
    def run_smart_cleanup(self):
        """Run comprehensive cleanup - completely hidden from Discord"""
        try:
            duplicates = self.cleanup_duplicates()
            aged = self.cleanup_by_age()
            sized = self.cleanup_by_size()
            
            total_cleaned = len(duplicates) + len(aged) + len(sized)
            
            if total_cleaned > 0:
                print(f"🧹 Background cleanup: {total_cleaned} files removed")
                if duplicates:
                    print(f"  🔄 {len(duplicates)} duplicates")
                if aged:
                    print(f"  ⏰ {len(aged)} old files")
                if sized:
                    print(f"  💾 {len(sized)} for size limit")
            
        except Exception as e:
            print(f"⚠️ Background cleanup error: {e}")

class AudioProcessor:
    def __init__(self, quality_mode='optimized'):
        # ALL the YouTube/music processing happens HERE - not in Discord bot
        
        # Quality presets
        if quality_mode == 'optimized':
            # Smallest files for streaming (128kbps MP3)
            self.audio_format_options = {
                **_youtube_ytdlp_opts(),
                'format': 'worstaudio/worst',
                'quiet': True,
                'no_warnings': True,
                'extractaudio': True,
                'audioformat': 'mp3',
                'audioquality': '5',  # Medium quality
                'socket_timeout': 45,
                'prefer_ffmpeg': True,
                'keepvideo': False,
                'postprocessor_args': ['-ar', '44100', '-ac', '2', '-b:a', '128k'],
                'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'referer': 'https://www.youtube.com/',
                'nocheckcertificate': True,
                'ignoreerrors': False,
            }
            print("🎯 Audio quality: Optimized for streaming (128kbps MP3)")
            
        elif quality_mode == 'balanced':
            # Balanced quality/size (192kbps)
            self.audio_format_options = {
                **_youtube_ytdlp_opts(),
                'format': 'bestaudio[abr<=192]/bestaudio/best',
                'quiet': True,
                'no_warnings': True,
                'extractaudio': True,
                'audioformat': 'mp3',
                'audioquality': '3',
                'socket_timeout': 45,
                'prefer_ffmpeg': True,
                'keepvideo': False,
                'postprocessor_args': ['-ar', '44100', '-ac', '2', '-b:a', '192k'],
                'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'referer': 'https://www.youtube.com/',
                'nocheckcertificate': True,
                'ignoreerrors': False,
            }
            print("⚖️ Audio quality: Balanced (192kbps MP3)")
            
        else:  # high quality
            # Best quality available
            self.audio_format_options = {
                **_youtube_ytdlp_opts(),
                'format': 'bestaudio/best',
                'quiet': True,
                'no_warnings': True,
                'extractaudio': True,
                'audioformat': 'best',
                'audioquality': '0',
                'socket_timeout': 45,
                'prefer_ffmpeg': True,
                'keepvideo': False,
                'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'referer': 'https://www.youtube.com/',
                'nocheckcertificate': True,
                'ignoreerrors': False,
            }
            print("🔊 Audio quality: High quality (best available)")
        
        # Setup directories
        self.media_dir = Path("media_library")
        self.queue_dir = Path("request_queue")  
        self.media_dir.mkdir(exist_ok=True)
        self.queue_dir.mkdir(exist_ok=True)
        
        # Initialize processor with memory management
        self.processor = yt_dlp.YoutubeDL(self.audio_format_options)
        self.request_count = 0  # Track processed requests for memory management
    
    def check_network_connectivity(self):
        """Check if we can reach YouTube and other essential services"""
        import socket
        import urllib.request
        
        # Test DNS resolution first
        try:
            socket.gethostbyname('www.google.com')
            print("✅ DNS resolution working")
        except socket.gaierror as e:
            print(f"❌ DNS resolution failed: {e}")
            return False
        
        # Test basic HTTP connectivity
        test_urls = [
            'https://www.google.com',
            'https://www.youtube.com',
            'https://httpbin.org/get'
        ]
        
        for url in test_urls:
            try:
                req = urllib.request.Request(url, headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                })
                with urllib.request.urlopen(req, timeout=10) as response:
                    if response.status == 200:
                        print(f"✅ Can reach {url}")
                        return True
            except Exception as e:
                print(f"⚠️ Cannot reach {url}: {e}")
                continue
        
        print("❌ No network connectivity detected")
        
        # Try to flush DNS cache as last resort
        try:
            print("🔄 Attempting DNS cache flush...")
            import subprocess
            result = subprocess.run(['ipconfig', '/flushdns'], 
                                  capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                print("✅ DNS cache flushed, retrying connectivity...")
                # Try one more time after DNS flush
                try:
                    socket.gethostbyname('www.google.com')
                    print("✅ DNS resolution working after flush")
                    return True
                except:
                    pass
            else:
                print("⚠️ DNS flush failed")
        except Exception as e:
            print(f"⚠️ Could not flush DNS: {e}")
        
        return False
        
        # Initialize air-gapped cleanup system
        self.cleaner = SmartMediaCleaner(
            media_dir=self.media_dir,
            max_cache_size_mb=100,  # 100MB cache limit
            max_age_days=3          # Keep files for 3 days max
        )
        
        # Start background cleanup (completely hidden from Discord)
        self._start_cleanup_scheduler()
        
        # Run initial cleanup
        print("🧹 Running initial cleanup...")
        self.cleaner.run_smart_cleanup()
        
        print("🎵 Audio Processing Service Started")
        print("📁 Audio library management: Active")
        print("🎧 File processing: Online")
        print("🧹 Smart cleanup enabled (100MB limit, 3-day retention)")
        
    def generate_safe_filename(self, query, video_id=None):
        """Generate completely generic filename that reveals nothing"""
        # Create hash of query + video_id for consistent naming
        content = f"{query}_{video_id}" if video_id else query
        hash_object = hashlib.md5(content.encode())
        file_hash = hash_object.hexdigest()[:12]
        
        # Generic filename with timestamp
        timestamp = int(time.time())
        return f"media_{timestamp}_{file_hash}"
    
    def process_audio_request(self, query):
        """Process audio request and save to media library"""
        try:
            print(f"🔍 Processing request: {query}")
            
            # Search and get video info (YouTube processing hidden from Discord)
            search_query = f"ytsearch:{query}"
            info = self.processor.extract_info(search_query, download=False)
            
            if 'entries' in info and len(info['entries']) > 0:
                video_info = info['entries'][0]
                video_id = video_info.get('id')
                title = video_info.get('title', 'Unknown')
                duration = video_info.get('duration', 0)
                
                print(f"✅ Found: {title}")
                
                # Generate safe filename
                safe_filename = self.generate_safe_filename(query, video_id)
                
                # Check if already processed
                existing_file = None
                for ext in ['.webm', '.m4a', '.mp4', '.opus']:
                    potential_path = self.media_dir / f"{safe_filename}{ext}"
                    if potential_path.exists():
                        existing_file = potential_path
                        break
                
                if existing_file:
                    print(f"📁 Already cached: {existing_file.name}")
                    return {
                        'status': 'success',
                        'file_path': str(existing_file),
                        'title': title,
                        'duration': duration,
                        'cached': True
                    }
                
                # Simple network test
                print("🌐 Testing basic connectivity...")
                try:
                    import socket
                    socket.gethostbyname('www.google.com')
                    print("✅ Network connectivity OK")
                except Exception as e:
                    print(f"⚠️ Network issue detected: {e}")
                    # Continue anyway - let yt-dlp handle it
                
                # Simple download approach - use what works
                print(f"⬇️ Downloading: {title}")
                print(f"⬇️ File: {safe_filename}.xxx")
                
                try:
                    download_opts = {
                        **self.audio_format_options,
                        'outtmpl': str(self.media_dir / f"{safe_filename}.%(ext)s")
                    }
                    
                    with yt_dlp.YoutubeDL(download_opts) as downloader:
                        downloader.download([video_info['webpage_url']])
                    
                    print("✅ Download completed successfully")
                    
                except Exception as e:
                    print(f"❌ Download failed: {e}")
                    try:
                        print("🔄 Retrying with tv_embedded client...")
                        download_opts_retry = {
                            **self.audio_format_options,
                            'outtmpl': str(self.media_dir / f"{safe_filename}.%(ext)s"),
                            'extractor_args': {
                                'youtube': {
                                    'player_client': ['tv_embedded', 'android', 'ios'],
                                },
                            },
                        }
                        with yt_dlp.YoutubeDL(download_opts_retry) as downloader:
                            downloader.download([video_info['webpage_url']])
                        print("✅ Download completed successfully (retry)")
                    except Exception as e_retry:
                        print(f"❌ Retry failed: {e_retry}")
                        try:
                            print("🔄 Trying alternative search approach...")
                            search_opts = {
                                **_youtube_ytdlp_opts(),
                                'format': 'worstaudio/worst',  # Smallest file
                                'quiet': True,
                                'extractaudio': True,
                                'audioformat': 'mp3',
                                'audioquality': '5',  # Medium quality
                                'outtmpl': str(self.media_dir / f"{safe_filename}.%(ext)s"),
                                'postprocessor_args': ['-ar', '44100', '-ac', '2', '-b:a', '128k'],
                                'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                                'referer': 'https://www.youtube.com/',
                                'nocheckcertificate': True,
                                'ignoreerrors': False,
                            }
                            search_query = f"ytsearch1:{query}"
                            with yt_dlp.YoutubeDL(search_opts) as downloader:
                                downloader.download([search_query])
                            print("✅ Alternative search succeeded")
                        except Exception as e2:
                            print(f"❌ Alternative search also failed: {e2}")
                            return {
                                'status': 'error',
                                'message': f'Download failed: {str(e)} / Retry: {str(e_retry)} / Alt: {str(e2)}'
                            }
                
                # Find the downloaded file (check for optimized formats first)
                downloaded_file = None
                for ext in ['.mp3', '.webm', '.m4a', '.mp4', '.opus']:
                    potential_path = self.media_dir / f"{safe_filename}{ext}"
                    if potential_path.exists():
                        downloaded_file = potential_path
                        break
                
                if downloaded_file and downloaded_file.exists():
                    file_size = downloaded_file.stat().st_size
                    size_mb = file_size/1024/1024
                    
                    # Show optimized size
                    if size_mb < 3:
                        print(f"✅ Downloaded (optimized): {downloaded_file.name} ({size_mb:.2f}MB) 🎯")
                    elif size_mb < 5:
                        print(f"✅ Downloaded: {downloaded_file.name} ({size_mb:.2f}MB)")
                    else:
                        print(f"✅ Downloaded (large): {downloaded_file.name} ({size_mb:.2f}MB) ⚠️")
                    
                    # Save metadata separately
                    metadata = {
                        'original_query': query,
                        'title': title,
                        'duration': duration,
                        'video_id': video_id,
                        'file_path': str(downloaded_file),
                        'processed_time': time.time()
                    }
                    
                    metadata_file = self.media_dir / f"{safe_filename}.json"
                    with open(metadata_file, 'w') as f:
                        json.dump(metadata, f, indent=2)
                    
                    # Quick cleanup after download (remove any immediate duplicates)
                    # duplicate_count = len(self.cleaner.cleanup_duplicates())
                    # if duplicate_count > 0:
                    #     print(f"🧹 Removed {duplicate_count} duplicate(s) after download")
                    
                    return {
                        'status': 'success',
                        'file_path': str(downloaded_file),
                        'title': title,
                        'duration': duration,
                        'cached': False
                    }
                else:
                    return {'status': 'error', 'message': 'Download failed - no file found'}
                    
            else:
                return {'status': 'error', 'message': 'No results found'}
                
        except Exception as e:
            print(f"❌ Processing error: {e}")
            return {'status': 'error', 'message': str(e)}
    
    def _start_cleanup_scheduler(self):
        """Start background cleanup thread - completely hidden from Discord"""
        def cleanup_worker():
            while True:
                try:
                    # Run cleanup every 6 hours
                    time.sleep(6 * 3600)  # 6 hours
                    print("🕐 Running scheduled cleanup...")
                    # self.cleaner.run_smart_cleanup()
                except Exception as e:
                    print(f"⚠️ Scheduled cleanup error: {e}")
        
        cleanup_thread = threading.Thread(target=cleanup_worker, daemon=True)
        cleanup_thread.start()
        print("⏰ Background cleanup scheduled every 6 hours")
    
    def process_queue(self):
        """Process requests from queue directory"""
        while True:
            try:
                # Look for request files and cleanup files
                request_files = list(self.queue_dir.glob("request_*.json"))
                cleanup_files = list(self.queue_dir.glob("cleanup_*.json"))
                
                # Process cleanup requests first (immediate)
                for cleanup_file in cleanup_files:
                    try:
                        with open(cleanup_file, 'r') as f:
                            cleanup_data = json.load(f)
                        
                        cleanup_type = cleanup_data.get('type')
                        
                        if cleanup_type == 'cleanup':
                            # Single file cleanup
                            file_path = cleanup_data.get('file_path', '')
                            if file_path and os.path.exists(file_path):
                                try:
                                    # Clean up both webm and json files
                                    webm_path = Path(file_path)
                                    json_path = webm_path.with_suffix('.json')
                                    
                                    if webm_path.exists():
                                        webm_path.unlink()
                                        print(f"🧹 Immediately cleaned: {webm_path.name}")
                                    
                                    if json_path.exists():
                                        json_path.unlink()
                                        print(f"🧹 Cleaned metadata: {json_path.name}")
                                        
                                except Exception as cleanup_err:
                                    print(f"⚠️ Cleanup failed for {os.path.basename(file_path)}: {cleanup_err}")
                        
                        elif cleanup_type == 'cleanup_all':
                            # Full cleanup of all media files
                            reason = cleanup_data.get('reason', 'unknown')
                            print(f"🧹 Running full cleanup ({reason})...")
                            
                            try:
                                cleaned_count = 0
                                failed_count = 0
                                
                                # Clean all webm files and their json metadata
                                for webm_file in self.media_dir.glob("*.webm"):
                                    try:
                                        json_file = webm_file.with_suffix('.json')
                                        
                                        if webm_file.exists():
                                            webm_file.unlink()
                                            cleaned_count += 1
                                        
                                        if json_file.exists():
                                            json_file.unlink()
                                            
                                    except Exception as file_err:
                                        failed_count += 1
                                        print(f"⚠️ Failed to clean {webm_file.name}: {file_err}")
                                
                                if cleaned_count > 0:
                                    print(f"🧹 Full cleanup complete: {cleaned_count} files removed")
                                    if failed_count > 0:
                                        print(f"⚠️ {failed_count} files failed to clean")
                                else:
                                    print("✨ No files to clean")
                                    
                            except Exception as full_cleanup_err:
                                print(f"❌ Full cleanup error: {full_cleanup_err}")
                        
                        # Remove cleanup request
                        cleanup_file.unlink()
                        
                    except Exception as e:
                        print(f"❌ Error processing cleanup {cleanup_file}: {e}")
                        try:
                            cleanup_file.unlink()
                        except:
                            pass
                
                # Process audio requests
                for request_file in request_files:
                    try:
                        with open(request_file, 'r') as f:
                            request_data = json.load(f)
                        
                        query = request_data.get('query', '')
                        request_id = request_data.get('request_id', '')
                        
                        if query:
                            print(f"📝 Processing queued request: {query}")
                            result = self.process_audio_request(query)
                            
                            # Memory management: periodically recreate yt-dlp instance
                            self.request_count += 1
                            if self.request_count >= 50:  # Every 50 requests
                                print("🧠 Refreshing processor for memory management...")
                                self.processor = yt_dlp.YoutubeDL(self.audio_format_options)
                                self.request_count = 0
                            
                            # Save result
                            result_file = self.queue_dir / f"result_{request_id}.json"
                            with open(result_file, 'w') as f:
                                json.dump(result, f, indent=2)
                            
                            # Remove request file
                            request_file.unlink()
                            
                    except Exception as e:
                        print(f"❌ Error processing {request_file}: {e}")
                        # Remove problematic request
                        try:
                            request_file.unlink()
                        except:
                            pass
                
                # Wait before checking again
                time.sleep(2)
                
            except KeyboardInterrupt:
                print("\n🛑 Shutting down Audio Processing Service")
                break
            except Exception as e:
                print(f"❌ Queue processing error: {e}")
                time.sleep(5)

def signal_handler(signum, frame):
    print("\n🛑 Received shutdown signal")
    sys.exit(0)

def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import subprocess as sp
            r = sp.run(
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


def _exit_if_duplicate_processor():
    """Only one queue consumer; if another is alive, exit quietly so a second bot can share it."""
    queue_dir = Path("request_queue")
    queue_dir.mkdir(exist_ok=True)
    pid_file = queue_dir / ".audio_processor.pid"
    if pid_file.exists():
        try:
            old = int(pid_file.read_text().strip())
            if _pid_exists(old):
                print(f"Audio processor already running (PID {old}) — exiting")
                sys.exit(0)
        except (ValueError, OSError):
            pass
        try:
            pid_file.unlink()
        except OSError:
            pass
    pid_file.write_text(str(os.getpid()))

    def _unlink_pid():
        try:
            if pid_file.exists() and pid_file.read_text().strip() == str(os.getpid()):
                pid_file.unlink()
        except OSError:
            pass

    atexit.register(_unlink_pid)


if __name__ == "__main__":
    _exit_if_duplicate_processor()

    # Setup signal handling
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Start processor
    processor = AudioProcessor()
    
    # Test mode - process single request
    if len(sys.argv) > 1:
        test_query = " ".join(sys.argv[1:])
        print(f"🧪 Test mode: Processing '{test_query}'")
        result = processor.process_audio_request(test_query)
        print(f"📋 Result: {result}")
    else:
        # Queue processing mode
        print("🔄 Starting queue processing mode...")
        print("💡 Use 'python audio_processor_service.py \"song name\"' for testing")
        processor.process_queue()