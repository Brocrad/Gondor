#!/usr/bin/env python3
"""
Audio System Launcher
Manages audio processing and Discord streaming services
"""

import subprocess
import sys
import os
import time
import signal
from pathlib import Path

class AudioSystemManager:
    def __init__(self):
        self.processor_process = None
        self.bot_process = None
        
    def start_background_processor(self):
        """Start the background audio processing service"""
        print("🎵 Starting audio processing service...")
        print("📁 Loading audio library management...")
        
        try:
            # Don't capture output so we can see what's happening
            self.processor_process = subprocess.Popen([
                sys.executable, "audio_processor_service.py"
            ])
            
            # Give it time to start and check for immediate failures
            print("⏳ Waiting for background processor to initialize...")
            time.sleep(3)
            
            if self.processor_process.poll() is None:
                print("✅ Background processor started successfully")
                return True
            else:
                print(f"❌ Background processor exited with code: {self.processor_process.returncode}")
                return False
                
        except Exception as e:
            print(f"❌ Error starting background processor: {e}")
            return False
    
    def start_discord_bot(self):
        """Start the streaming Discord bot"""
        print("\n🤖 Starting Discord streaming bot...")
        print("📁 Initializing file streaming service...")
        
        try:
            self.bot_process = subprocess.Popen([
                sys.executable, "streaming.py"
            ])
            
            # Give it time to start
            time.sleep(3)
            
            if self.bot_process.poll() is None:
                print("✅ Discord bot started successfully")
                return True
            else:
                print(f"❌ Discord bot exited with code: {self.bot_process.returncode}")
                return False
                
        except Exception as e:
            print(f"❌ Error starting Discord bot: {e}")
            return False
    
    def start_system(self):
        """Start the complete stealth system"""
        print("🚀 Starting Audio System")
        print("=" * 50)
        
        # Start background processor first
        if not self.start_background_processor():
            print("❌ Failed to start background processor")
            return False
        
        # Start Discord bot
        if not self.start_discord_bot():
            print("❌ Failed to start Discord bot")
            self.stop_system()
            return False
        
        print("\n✅ SYSTEM READY!")
        print("=" * 50)
        print("🎵 Audio streaming service: Online")
        print("📁 File management: Active")
        print("🔗 Discord integration: Connected")
        print("\n💡 Commands: !summon then !play <song name>")
        print("🛑 Press Ctrl+C to shutdown")
        
        return True
    
    def queue_shutdown_cleanup(self):
        """Queue final cleanup before system shutdown"""
        try:
            import uuid
            import json
            from pathlib import Path
            
            queue_dir = Path("request_queue")
            queue_dir.mkdir(exist_ok=True)
            
            cleanup_id = str(uuid.uuid4())[:8]
            cleanup_request = {
                'type': 'cleanup_all',
                'cleanup_id': cleanup_id,
                'timestamp': time.time(),
                'reason': 'system_shutdown'
            }
            
            cleanup_file = queue_dir / f"cleanup_all_{cleanup_id}.json"
            with open(cleanup_file, 'w') as f:
                json.dump(cleanup_request, f, indent=2)
            
            print("🧹 Queued final system cleanup")
            
            # Give background service time to process
            time.sleep(2)
            
        except Exception as e:
            print(f"⚠️ Failed to queue shutdown cleanup: {e}")

    def stop_system(self):
        """Stop all services"""
        print("\n🛑 Shutting down audio system...")
        
        # Queue final cleanup before stopping everything
        self.queue_shutdown_cleanup()
        
        if self.bot_process:
            try:
                self.bot_process.terminate()
                self.bot_process.wait(timeout=5)
                print("✅ Discord bot stopped")
            except:
                try:
                    self.bot_process.kill()
                    print("🔪 Discord bot force killed")
                except:
                    pass
        
        if self.processor_process:
            try:
                self.processor_process.terminate()
                self.processor_process.wait(timeout=5)
                print("✅ Background processor stopped")
            except:
                try:
                    self.processor_process.kill()
                    print("🔪 Background processor force killed")
                except:
                    pass
    
    def monitor_system(self):
        """Monitor both services"""
        try:
            while True:
                # Check if processes are still running
                if self.processor_process and self.processor_process.poll() is not None:
                    print("❌ Background processor died")
                    break
                
                if self.bot_process and self.bot_process.poll() is not None:
                    print("❌ Discord bot died")
                    break
                
                time.sleep(5)
                
        except KeyboardInterrupt:
            print("\n🛑 Shutdown requested")
        finally:
            self.stop_system()

def signal_handler(signum, frame):
    print("\n🛑 Received shutdown signal")
    sys.exit(0)

def main():
    # Setup signal handling
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    manager = AudioSystemManager()
    
    if len(sys.argv) > 1:
        if sys.argv[1] == "test":
            # Test mode - just run background processor with a test query
            print("🧪 Test Mode: Testing background processor only")
            test_query = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else "never gonna give you up"
            
            os.system(f'python audio_processor_service.py "{test_query}"')
            return
    
    # Start complete system
    if manager.start_system():
        manager.monitor_system()
    else:
        print("❌ Failed to start audio system")
        manager.stop_system()

if __name__ == "__main__":
    main()