#!/usr/bin/env python3
"""
Gondor Bot Auto-Restart Manager
Automatically restarts the bot if it crashes or is rebooted via !reboot command
"""

import subprocess
import sys
import time
import os
from pathlib import Path

# Configuration
BOT_SCRIPT = "streaming.py"
RESTART_DELAY = 5  # seconds to wait before restarting
MAX_RESTART_ATTEMPTS = 10  # maximum consecutive restart attempts
RESTART_COOLDOWN = 300  # 5 minutes cooldown after max attempts

class BotManager:
    def __init__(self):
        self.restart_count = 0
        self.last_restart_time = 0
        self.bot_process = None
        
    def log(self, message):
        """Log message with timestamp"""
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] [BOT-MANAGER] {message}")
    
    def reset_restart_counter(self):
        """Reset restart counter if enough time has passed"""
        current_time = time.time()
        if current_time - self.last_restart_time > RESTART_COOLDOWN:
            if self.restart_count > 0:
                self.log(f"Cooldown period passed, resetting restart counter (was {self.restart_count})")
            self.restart_count = 0
    
    def can_restart(self):
        """Check if bot can be restarted"""
        self.reset_restart_counter()
        return self.restart_count < MAX_RESTART_ATTEMPTS
    
    def start_bot(self):
        """Start the bot process"""
        try:
            self.log(f"Starting bot: {BOT_SCRIPT}")
            
            # Check if bot script exists
            if not Path(BOT_SCRIPT).exists():
                self.log(f"❌ ERROR: Bot script '{BOT_SCRIPT}' not found!")
                return None
            
            # Start bot process
            self.bot_process = subprocess.Popen(
                [sys.executable, BOT_SCRIPT],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                bufsize=1
            )
            
            self.log(f"✅ Bot started with PID: {self.bot_process.pid}")
            return self.bot_process
            
        except Exception as e:
            self.log(f"❌ ERROR starting bot: {e}")
            return None
    
    def monitor_bot(self):
        """Monitor bot output and handle restart"""
        if not self.bot_process:
            return False
        
        try:
            # Read bot output line by line
            for line in iter(self.bot_process.stdout.readline, ''):
                if line:
                    # Forward bot output to console (remove newline to avoid double spacing)
                    print(line.rstrip())
                    
                    # Check for specific shutdown messages
                    if "🔄 Bot shutting down for restart..." in line:
                        self.log("🔄 Detected restart command, preparing for restart...")
                        break
                
                # Check if process has ended
                if self.bot_process.poll() is not None:
                    break
            
            # Wait for process to fully terminate
            exit_code = self.bot_process.wait()
            self.log(f"Bot process ended with exit code: {exit_code}")
            
            return True
            
        except Exception as e:
            self.log(f"❌ ERROR monitoring bot: {e}")
            return False
    
    def cleanup_process(self):
        """Clean up bot process"""
        if self.bot_process:
            try:
                if self.bot_process.poll() is None:
                    self.log("Terminating bot process...")
                    self.bot_process.terminate()
                    time.sleep(2)
                    
                    if self.bot_process.poll() is None:
                        self.log("Force killing bot process...")
                        self.bot_process.kill()
                
                self.bot_process = None
                
            except Exception as e:
                self.log(f"Error cleaning up process: {e}")
    
    def run(self):
        """Main bot management loop"""
        self.log("🤖 Gondor Bot Auto-Restart Manager Started")
        self.log(f"📝 Bot script: {BOT_SCRIPT}")
        self.log(f"⏱️ Restart delay: {RESTART_DELAY} seconds")
        self.log(f"🔄 Max restart attempts: {MAX_RESTART_ATTEMPTS}")
        self.log(f"❄️ Restart cooldown: {RESTART_COOLDOWN} seconds")
        self.log("=" * 60)
        
        try:
            while True:
                # Check if we can restart
                if not self.can_restart():
                    self.log(f"❌ Maximum restart attempts ({MAX_RESTART_ATTEMPTS}) reached!")
                    self.log(f"⏳ Waiting {RESTART_COOLDOWN} seconds before allowing restarts again...")
                    time.sleep(RESTART_COOLDOWN)
                    continue
                
                # Start the bot
                if not self.start_bot():
                    self.log("❌ Failed to start bot, retrying in 30 seconds...")
                    time.sleep(30)
                    continue
                
                # Monitor the bot
                bot_ended = self.monitor_bot()
                
                # Clean up
                self.cleanup_process()
                
                if bot_ended:
                    # Increment restart counter
                    self.restart_count += 1
                    self.last_restart_time = time.time()
                    
                    self.log(f"🔄 Bot restart #{self.restart_count}")
                    
                    if self.restart_count < MAX_RESTART_ATTEMPTS:
                        self.log(f"⏳ Waiting {RESTART_DELAY} seconds before restart...")
                        time.sleep(RESTART_DELAY)
                    else:
                        self.log("⚠️ Reached maximum restart attempts, entering cooldown...")
                else:
                    self.log("❌ Bot monitoring failed, retrying...")
                    time.sleep(10)
        
        except KeyboardInterrupt:
            self.log("🛑 Shutdown requested by user")
            self.cleanup_process()
            
        except Exception as e:
            self.log(f"💥 CRITICAL ERROR: {e}")
            self.cleanup_process()
            raise

def main():
    """Main entry point"""
    # Change to script directory
    script_dir = Path(__file__).parent
    os.chdir(script_dir)
    
    # Create and run bot manager
    manager = BotManager()
    manager.run()

if __name__ == "__main__":
    main()
