# Discord Music Bot (Python)

A reliable Discord music bot that plays music from YouTube using the power of Python, `yt-dlp`, and `discord.py`.

## 🎵 Features

- 🔍 **Smart YouTube Search** - Search by song name or use direct URLs
- 🎵 **High-Quality Audio** - Reliable audio extraction using yt-dlp
- ⏸️ **Playback Controls** - Pause, resume, and stop functionality
- 🤖 **Slash Commands** - Modern Discord slash command interface
- 🔊 **Voice Channel Support** - Join and play music in voice channels
- 🛡️ **Error Handling** - Graceful error recovery and user feedback

## 🚀 Quick Start

### Prerequisites

1. **Python 3.8+** installed on your system
2. **FFmpeg** installed and in your system PATH
3. **Discord Bot Token** from Discord Developer Portal

### Installation

1. **Install Python dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Install FFmpeg:**
   - **Windows**: Download from https://ffmpeg.org and add to PATH
   - **macOS**: `brew install ffmpeg`
   - **Linux**: `sudo apt install ffmpeg`

3. **Set up your Discord bot:**
   - Go to https://discord.com/developers/applications
   - Create a new application and bot
   - Copy the bot token
   - Add your token to the bot file or set as environment variable

4. **Run the bot:**
   ```bash
   python music_bot.py
   ```

## 🎮 Commands

All commands are slash commands:

- `/play <search or url>` - Play music from YouTube
- `/summon` - Summon the bot to your voice channel
- `/pause` - Pause the current song
- `/resume` - Resume playback
- `/stop` - Stop music and disconnect

## 💡 Usage Examples

```
/play Never Gonna Give You Up
/play The Boys Are Back In Town
/play https://www.youtube.com/watch?v=dQw4w9WgXcQ
/pause
/resume
/stop
```

## 🔧 Why Python?

This bot uses Python instead of JavaScript because:

- ✅ **yt-dlp** - The gold standard for YouTube downloading
- ✅ **Better reliability** - Handles YouTube changes automatically
- ✅ **Fewer breakages** - More stable when YouTube updates
- ✅ **Superior audio extraction** - Professional-grade audio processing
- ✅ **discord.py** - Mature Discord library with excellent voice support

## 🛠️ Troubleshooting

### Bot doesn't respond:
- Check bot has proper permissions (Send Messages, Use Slash Commands, Connect, Speak)
- Verify bot token is correct
- Make sure you're in a voice channel when using `/play`

### Audio doesn't play:
- Ensure FFmpeg is installed and in PATH
- Some videos may be region-restricted
- Try different search terms or direct URLs

### yt-dlp errors:
- Update yt-dlp: `pip install --upgrade yt-dlp`
- YouTube occasionally blocks scrapers - this is normal

## 🎵 Perfect for Discord Servers

This Python-based music bot is designed to be:
- **Reliable** - Won't break when YouTube updates
- **Fast** - Quick audio extraction and playback
- **User-friendly** - Simple slash commands
- **Stable** - Robust error handling

Enjoy your working Discord music bot! 🎤🤖
🚀 To Run Your Bot in the Future:
1. Navigate to your project directory: cd <your directory path>
2. Activate virtual environment: & .venv/Scripts/Activate.ps1
3. Run the bot: python music_bot.py
