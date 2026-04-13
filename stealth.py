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
        
        # Auto-restart configuration
        self.restart_count = 0
        self.max_restarts = 10
        self.restart_delay = 5
        self.restart_cooldown = 300  # 5 minutes
        self.last_restart_time = 0
        self.auto_restart_enabled = True
        
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
        
        # Discord bot spawns audio_processor_service.py itself (shared request_queue cwd)
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
    
    def log_with_timestamp(self, message):
        """Log message with timestamp"""
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] [SYSTEM] {message}")
    
    def reset_restart_counter(self):
        """Reset restart counter if cooldown period has passed"""
        current_time = time.time()
        if current_time - self.last_restart_time > self.restart_cooldown:
            if self.restart_count > 0:
                self.log_with_timestamp(f"Cooldown period passed, resetting restart counter (was {self.restart_count})")
            self.restart_count = 0
    
    def can_restart(self):
        """Check if system can be restarted"""
        self.reset_restart_counter()
        return self.auto_restart_enabled and self.restart_count < self.max_restarts
    
    def restart_discord_bot(self):
        """Restart the Discord bot component"""
        if not self.can_restart():
            self.log_with_timestamp(f"❌ Maximum restart attempts ({self.max_restarts}) reached!")
            self.log_with_timestamp(f"⏳ Waiting {self.restart_cooldown} seconds before allowing restarts...")
            return False
        
        self.log_with_timestamp("🔄 Restarting Discord streaming service...")
        
        # Stop current bot process
        if self.bot_process:
            try:
                self.bot_process.terminate()
                self.bot_process.wait(timeout=5)
            except:
                try:
                    self.bot_process.kill()
                except:
                    pass
        
        # Increment restart counter
        self.restart_count += 1
        self.last_restart_time = time.time()
        
        self.log_with_timestamp(f"🔄 Bot restart #{self.restart_count}")
        
        if self.restart_count < self.max_restarts:
            self.log_with_timestamp(f"⏳ Waiting {self.restart_delay} seconds before restart...")
            time.sleep(self.restart_delay)
        
        # Start new bot process
        return self.start_discord_bot()
    
    def monitor_system(self):
        """Monitor both services with auto-restart capability"""
        self.log_with_timestamp("🔍 System monitoring started")
        self.log_with_timestamp(f"🔄 Auto-restart: {'Enabled' if self.auto_restart_enabled else 'Disabled'}")
        self.log_with_timestamp(f"📊 Max restarts: {self.max_restarts}, Restart delay: {self.restart_delay}s")
        
        try:
            while True:
                # Check background processor
                if self.processor_process and self.processor_process.poll() is not None:
                    self.log_with_timestamp("❌ Background processor died")
                    if self.auto_restart_enabled:
                        self.log_with_timestamp("🔄 Restarting background processor...")
                        if not self.start_background_processor():
                            self.log_with_timestamp("❌ Failed to restart background processor")
                            break
                    else:
                        break
                
                # Check Discord bot
                if self.bot_process and self.bot_process.poll() is not None:
                    exit_code = self.bot_process.returncode
                    self.log_with_timestamp(f"❌ Discord bot died (exit code: {exit_code})")
                    
                    if self.auto_restart_enabled:
                        if not self.restart_discord_bot():
                            self.log_with_timestamp("❌ Failed to restart Discord bot")
                            break
                    else:
                        break
                
                time.sleep(5)
                
        except KeyboardInterrupt:
            self.log_with_timestamp("🛑 Shutdown requested by user")
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
    
    # Parse command line arguments
    if len(sys.argv) > 1:
        for arg in sys.argv[1:]:
            if arg == "test":
                # Test mode - just run background processor with a test query
                print("🧪 Test Mode: Testing background processor only")
                test_query = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else "never gonna give you up"
                os.system(f'python audio_processor_service.py "{test_query}"')
                return
            elif arg == "--no-restart":
                manager.auto_restart_enabled = False
                print("🔄 Auto-restart disabled")
            elif arg == "--max-restarts":
                try:
                    manager.max_restarts = int(sys.argv[sys.argv.index(arg) + 1])
                    print(f"📊 Max restarts set to: {manager.max_restarts}")
                except (IndexError, ValueError):
                    print("⚠️ Invalid max-restarts value, using default (10)")
            elif arg == "--restart-delay":
                try:
                    manager.restart_delay = int(sys.argv[sys.argv.index(arg) + 1])
                    print(f"⏱️ Restart delay set to: {manager.restart_delay}s")
                except (IndexError, ValueError):
                    print("⚠️ Invalid restart-delay value, using default (5s)")
    
    # Show startup configuration
    print("🚀 Starting Audio System")
    print("=" * 50)
    print(f"🔄 Auto-restart: {'Enabled' if manager.auto_restart_enabled else 'Disabled'}")
    if manager.auto_restart_enabled:
        print(f"📊 Max restart attempts: {manager.max_restarts}")
        print(f"⏱️ Restart delay: {manager.restart_delay} seconds")
        print(f"❄️ Restart cooldown: {manager.restart_cooldown} seconds")
    print("=" * 50)
    
    # Start complete system
    if manager.start_system():
        manager.monitor_system()
    else:
        print("❌ Failed to start audio system")
        manager.stop_system()

if __name__ == "__main__":
    main()