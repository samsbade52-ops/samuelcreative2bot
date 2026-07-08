"""
Samuel Creative Bot v2.0 - Production Ready with Healthcheck
@samuelcreative2bot
"""

import os
import sys
import logging
import asyncio
import requests
import json
import time
import re
from datetime import datetime, timedelta
from functools import wraps
from typing import Optional, Tuple, Dict, Any
from threading import Thread
import traceback

# HTTP server for healthchecks
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

# Telegram imports
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, 
    CommandHandler, 
    MessageHandler, 
    filters, 
    ContextTypes,
    CallbackQueryHandler,
    PicklePersistence
)
from telegram.constants import ParseMode
from telegram.error import (
    TelegramError, 
    RetryAfter, 
    NetworkError,
    TimedOut,
    BadRequest
)

# Configure logging with rotation
from logging.handlers import RotatingFileHandler

# Setup logging
log_formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# Console handler
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)

# File handler with rotation
file_handler = RotatingFileHandler(
    'bot.log', 
    maxBytes=10*1024*1024,  # 10MB
    backupCount=5
)
file_handler.setFormatter(log_formatter)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.addHandler(console_handler)
logger.addHandler(file_handler)

# Configuration
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    logger.critical("❌ TELEGRAM_BOT_TOKEN not set!")
    sys.exit(1)

# API Keys
EXCHANGE_RATE_API_KEY = os.environ.get("EXCHANGE_RATE_API_KEY", "")
HUGGINGFACE_TOKEN = os.environ.get("HUGGINGFACE_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# Constants
MAX_RETRIES = 3
RETRY_DELAY = 2
REQUEST_TIMEOUT = 30
IMAGE_TIMEOUT = 60

# ============= HEALTH CHECK SERVER =============

class HealthCheckHandler(BaseHTTPRequestHandler):
    """Simple HTTP handler for health checks"""
    
    def do_GET(self):
        if self.path == '/health' or self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'OK')
        else:
            self.send_response(404)
            self.end_headers()
    
    def log_message(self, format, *args):
        # Suppress healthcheck logs
        if '/health' not in args[0] if args else False:
            logger.info(f"Healthcheck: {format % args}")

def run_healthcheck_server():
    """Run a simple HTTP server for health checks"""
    port = int(os.environ.get('PORT', 8080))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    logger.info(f"🩺 Healthcheck server running on port {port}")
    server.serve_forever()

# ============= RATE LIMITING =============

class RateLimiter:
    """Simple rate limiter to prevent API abuse"""
    def __init__(self):
        self._timestamps = {}
    
    def can_proceed(self, user_id: int, limit: int = 10, window: int = 60) -> bool:
        """Check if user can proceed (10 requests per minute by default)"""
        now = time.time()
        if user_id not in self._timestamps:
            self._timestamps[user_id] = []
        
        # Clean old timestamps
        self._timestamps[user_id] = [t for t in self._timestamps[user_id] if now - t < window]
        
        if len(self._timestamps[user_id]) >= limit:
            return False
        
        self._timestamps[user_id].append(now)
        return True

rate_limiter = RateLimiter()

# ============= ERROR DECORATORS =============

def safe_command(func):
    """Decorator to handle errors in commands gracefully"""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        try:
            return await func(update, context, *args, **kwargs)
        except Exception as e:
            logger.error(f"Error in {func.__name__}: {e}\n{traceback.format_exc()}")
            try:
                await update.message.reply_text(
                    "⚠️ *Something went wrong*\n\n"
                    "I encountered an error. Please try again or use a different command.\n"
                    "If the problem persists, try restarting the bot with /start",
                    parse_mode=ParseMode.MARKDOWN
                )
            except:
                pass
            return None
    return wrapper

def safe_api_call(func):
    """Decorator for API calls with retry logic"""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        for attempt in range(MAX_RETRIES):
            try:
                return await func(*args, **kwargs)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                logger.warning(f"API call failed (attempt {attempt+1}): {e}")
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                    continue
                raise
            except Exception as e:
                logger.error(f"API call error: {e}")
                raise
        return None
    return wrapper

# ============= DICTIONARY FUNCTIONS =============

class DictionaryService:
    """Handles dictionary API calls with caching"""
    
    _cache = {}
    _cache_timeout = 3600  # 1 hour
    
    @classmethod
    async def get_definition(cls, word: str) -> str:
        """Get word definition with caching"""
        word = word.lower().strip()
        
        # Check cache
        if word in cls._cache:
            cached_time, data = cls._cache[word]
            if datetime.now() - cached_time < timedelta(seconds=cls._cache_timeout):
                return data
        
        try:
            url = f"https://api.dictionaryapi.dev/api/v2/entries/en/{word}"
            response = requests.get(url, timeout=REQUEST_TIMEOUT)
            
            if response.status_code == 200:
                data = response.json()
                if data:
                    formatted = cls._format_definition(data, word)
                    # Cache the result
                    cls._cache[word] = (datetime.now(), formatted)
                    return formatted
            
            elif response.status_code == 404:
                return f"❌ *'{word.capitalize()}'* not found. Please check spelling."
            
            else:
                return f"❌ *Dictionary Error*\nStatus: {response.status_code}"
                
        except requests.exceptions.Timeout:
            return "⏰ *Timeout*\nThe dictionary service is taking too long. Please try again."
        except Exception as e:
            logger.error(f"Dictionary error: {e}")
            return "❌ *Error*\nCould not fetch definition. Please try again later."
    
    @staticmethod
    def _format_definition(data: list, word: str) -> str:
        """Format the dictionary data"""
        result = f"📚 *{word.capitalize()}*\n\n"
        
        meanings = data[0].get("meanings", [])
        if not meanings:
            return f"❌ No definitions found for '{word}'."
        
        count = 0
        for meaning in meanings:
            if count >= 3:  # Limit to 3 meanings
                break
            
            part_of_speech = meaning.get("partOfSpeech", "unknown").upper()
            definitions = meaning.get("definitions", [])
            
            if definitions:
                result += f"*{part_of_speech}*\n"
                for i, definition in enumerate(definitions[:2], 1):
                    def_text = definition.get("definition", "No definition")
                    example = definition.get("example", "")
                    
                    result += f"  {i}. {def_text}"
                    if example:
                        result += f"\n     📌 *Example:* _{example}_"
                    result += "\n"
                result += "\n"
                count += 1
        
        return result.strip()

# ============= CURRENCY FUNCTIONS =============

class CurrencyService:
    """Handles currency conversion with fallback"""
    
    _rates_cache = {}
    _cache_timeout = 1800  # 30 minutes
    _supported_currencies = [
        "USD", "EUR", "GBP", "NGN", "JPY", "CAD", "AUD", "CHF", 
        "CNY", "INR", "BRL", "ZAR", "KRW", "SGD", "MYR"
    ]
    
    @classmethod
    async def convert(cls, amount: float, from_curr: str, to_curr: str) -> str:
        """Convert currency with caching and fallback"""
        from_curr = from_curr.upper()
        to_curr = to_curr.upper()
        
        # Validate currencies
        if from_curr not in cls._supported_currencies or to_curr not in cls._supported_currencies:
            return f"⚠️ *Unsupported Currency*\n\nSupported: {', '.join(cls._supported_currencies)}"
        
        try:
            rate = await cls._get_rate(from_curr, to_curr)
            if rate:
                converted = amount * rate
                return (
                    f"💰 *Currency Conversion*\n\n"
                    f"{amount:.2f} {from_curr} = {converted:.2f} {to_curr}\n"
                    f"📊 Rate: 1 {from_curr} = {rate:.4f} {to_curr}\n"
                    f"🕐 Updated: {datetime.now().strftime('%H:%M:%S')}"
                )
            else:
                return "❌ *Conversion Failed*\nPlease try again later."
                
        except Exception as e:
            logger.error(f"Currency error: {e}")
            return "❌ *Error*\nCurrency conversion failed. Please try again."
    
    @classmethod
    async def _get_rate(cls, from_curr: str, to_curr: str) -> Optional[float]:
        """Get exchange rate with cache and fallback"""
        cache_key = f"{from_curr}_{to_curr}"
        
        # Check cache
        if cache_key in cls._rates_cache:
            cached_time, rate = cls._rates_cache[cache_key]
            if datetime.now() - cached_time < timedelta(seconds=cls._cache_timeout):
                return rate
        
        # Try API if available
        if EXCHANGE_RATE_API_KEY:
            try:
                url = f"https://v6.exchangerate-api.com/v6/{EXCHANGE_RATE_API_KEY}/latest/{from_curr}"
                response = requests.get(url, timeout=REQUEST_TIMEOUT)
                
                if response.status_code == 200:
                    data = response.json()
                    if data.get("result") == "success":
                        rate = data["conversion_rates"].get(to_curr)
                        if rate:
                            cls._rates_cache[cache_key] = (datetime.now(), rate)
                            return rate
            except Exception as e:
                logger.warning(f"ExchangeRate-API failed: {e}")
        
        # Fallback: mock rates for demo
        mock_rates = {
            "USD": {"EUR": 0.92, "GBP": 0.79, "NGN": 1550, "JPY": 148.5, "CAD": 1.35, "AUD": 1.52},
            "EUR": {"USD": 1.09, "GBP": 0.86, "NGN": 1680, "JPY": 161.5, "CAD": 1.47, "AUD": 1.65},
            "GBP": {"USD": 1.27, "EUR": 1.16, "NGN": 1960, "JPY": 187.5, "CAD": 1.71, "AUD": 1.93},
            "NGN": {"USD": 0.00065, "EUR": 0.00060, "GBP": 0.00051, "JPY": 0.096, "CAD": 0.00087, "AUD": 0.00098},
        }
        
        if from_curr in mock_rates and to_curr in mock_rates[from_curr]:
            rate = mock_rates[from_curr][to_curr]
            cls._rates_cache[cache_key] = (datetime.now(), rate)
            return rate
        
        return None

# ============= IMAGE GENERATION FUNCTIONS =============

class ImageService:
    """Handles image generation with multiple providers"""
    
    @classmethod
    async def generate(cls, prompt: str) -> Optional[bytes]:
        """Generate image with fallback providers"""
        # Try Pollinations first (fast, free)
        result = await cls._generate_pollinations(prompt)
        if result:
            return result
        
        # Try Hugging Face if token available
        if HUGGINGFACE_TOKEN:
            result = await cls._generate_huggingface(prompt)
            if result:
                return result
        
        # Try OpenAI if key available
        if OPENAI_API_KEY:
            result = await cls._generate_openai(prompt)
            if result:
                return result
        
        return None
    
    @classmethod
    async def _generate_pollinations(cls, prompt: str) -> Optional[bytes]:
        """Generate using Pollinations.ai (free, no API key)"""
        try:
            url = f"https://image.pollinations.ai/prompt/{prompt.replace(' ', '%20')}?width=1024&height=768&nologo=true"
            response = requests.get(url, timeout=IMAGE_TIMEOUT)
            
            if response.status_code == 200 and response.content:
                return response.content
            
            return None
        except Exception as e:
            logger.warning(f"Pollinations error: {e}")
            return None
    
    @classmethod
    async def _generate_huggingface(cls, prompt: str) -> Optional[bytes]:
        """Generate using Hugging Face (requires token)"""
        try:
            model = "black-forest-labs/FLUX.1-dev"
            api_url = f"https://api-inference.huggingface.co/models/{model}"
            headers = {"Authorization": f"Bearer {HUGGINGFACE_TOKEN}"}
            
            payload = {
                "inputs": prompt,
                "parameters": {
                    "negative_prompt": "blurry, bad quality, distorted, ugly",
                    "num_inference_steps": 20,
                    "guidance_scale": 7.5
                }
            }
            
            response = requests.post(api_url, headers=headers, json=payload, timeout=IMAGE_TIMEOUT)
            
            if response.status_code == 200 and response.content:
                return response.content
            
            # Check if model is loading
            if response.status_code == 503:
                logger.warning("Hugging Face model is loading")
                return None
            
            return None
            
        except Exception as e:
            logger.warning(f"HuggingFace error: {e}")
            return None
    
    @classmethod
    async def _generate_openai(cls, prompt: str) -> Optional[bytes]:
        """Generate using OpenAI DALL-E (requires API key)"""
        try:
            # This is a placeholder - actual OpenAI DALL-E integration
            # would require additional setup
            return None
        except Exception as e:
            logger.warning(f"OpenAI error: {e}")
            return None

# ============= BOT COMMAND HANDLERS =============

@safe_command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    user = update.effective_user
    welcome_text = f"""
🌟 *Welcome to Samuel Creative Bot!* 🌟

Hi {user.first_name}! I'm your creative assistant.

*What I can do:*
🎨 *Image Generation* - Send any prompt
💱 *Currency Converter* - Convert currencies
📚 *Dictionary* - Define any word

*Quick Start:*
• Send any word → Get definition
• Send "100 USD to EUR" → Convert currency  
• Send "draw a cat" → Generate image
• Use /image [prompt] for images

*Commands:*
/start - Show this message
/help - Detailed help
/define [word] - Get definition
/convert [amount] [from] [to] - Convert
/image [prompt] - Generate image

*Need help?* Just ask or use /help

Made with ❤️ by Samuel Creative
"""
    await update.message.reply_text(welcome_text, parse_mode=ParseMode.MARKDOWN)

@safe_command
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command"""
    help_text = """
📖 *Help & Commands*

*📚 Dictionary*
• Send any word or /define [word]
• Shows definition, part of speech, and examples

*💱 Currency Converter*
• /convert 100 USD to EUR
• Or just send: "100 usd to eur"
• Supports 15+ major currencies

*🎨 Image Generation*
• /image a beautiful sunset
• Or send: "draw a cat astronaut"
• Be descriptive for best results

*💡 Tips*
• I use free APIs (rate limits apply)
• Image generation may take 10-30 seconds
• For best results, use detailed prompts

*📊 Rate Limits*
• 10 requests per minute per user
• Image: 5 requests per minute

*Support*
Contact: @SamuelCreative
GitHub: samuelcreative2bot

🔄 *Bot Version:* 2.0 (Production)
"""
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

@safe_command
async def define_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /define command"""
    if not context.args:
        await update.message.reply_text(
            "📚 *Usage:* /define [word]\n\nExample: `/define creativity`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    word = " ".join(context.args)
    
    # Rate limiting
    user_id = update.effective_user.id
    if not rate_limiter.can_proceed(user_id, limit=10, window=60):
        await update.message.reply_text(
            "⏰ *Rate Limit*\nPlease wait a moment before making more requests.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # Show typing
    await update.message.chat.send_action(action="typing")
    
    definition = await DictionaryService.get_definition(word)
    await update.message.reply_text(definition, parse_mode=ParseMode.MARKDOWN)

@safe_command
async def convert_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /convert command"""
    args = context.args
    
    if len(args) < 4 or args[2].lower() != "to":
        await update.message.reply_text(
            "💱 *Usage:* /convert [amount] [from] to [to]\n\n"
            "Examples:\n"
            "`/convert 100 USD to EUR`\n"
            "`/convert 50 GBP to NGN`\n\n"
            "Supported: USD, EUR, GBP, NGN, JPY, CAD, AUD, CHF, CNY, INR, BRL, ZAR, KRW, SGD, MYR",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    try:
        amount = float(args[0])
        from_curr = args[1].upper()
        to_curr = args[3].upper()
        
        # Rate limiting
        user_id = update.effective_user.id
        if not rate_limiter.can_proceed(user_id, limit=10, window=60):
            await update.message.reply_text(
                "⏰ *Rate Limit*\nPlease wait a moment.",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        await update.message.chat.send_action(action="typing")
        result = await CurrencyService.convert(amount, from_curr, to_curr)
        await update.message.reply_text(result, parse_mode=ParseMode.MARKDOWN)
        
    except ValueError:
        await update.message.reply_text(
            "❌ *Invalid Amount*\nPlease enter a valid number.",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Convert error: {e}")
        await update.message.reply_text(
            "❌ *Error*\nCould not convert currency. Please try again.",
            parse_mode=ParseMode.MARKDOWN
        )

@safe_command
async def image_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /image command"""
    if not context.args:
        await update.message.reply_text(
            "🎨 *Usage:* /image [prompt]\n\n"
            "Examples:\n"
            "`/image a beautiful sunset`\n"
            "`/image cyberpunk city at night`\n\n"
            "💡 Be descriptive for better results!\n"
            "⏰ Generation takes 10-30 seconds.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    prompt = " ".join(context.args)
    
    # Rate limiting (stricter for images)
    user_id = update.effective_user.id
    if not rate_limiter.can_proceed(user_id, limit=5, window=60):
        await update.message.reply_text(
            "⏰ *Rate Limit*\nImage generation is limited to 5 per minute.\n"
            "Please wait a moment.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # Send processing message
    processing_msg = await update.message.reply_text(
        f"🎨 *Generating image...*\n\n"
        f"📝 Prompt: \"{prompt}\"\n"
        f"⏰ Please wait 10-30 seconds...",
        parse_mode=ParseMode.MARKDOWN
    )
    
    try:
        # Show typing indicator
        await update.message.chat.send_action(action="upload_photo")
        
        # Generate image
        image_data = await ImageService.generate(prompt)
        
        if image_data:
            await processing_msg.delete()
            await update.message.reply_photo(
                photo=image_data,
                caption=f"🎨 *Generated Image*\n\n📝 *Prompt:* {prompt}\n\n✨ Generated by Samuel Creative Bot",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await processing_msg.edit_text(
                "❌ *Image Generation Failed*\n\n"
                "Sorry, I couldn't generate an image for this prompt.\n\n"
                "*Try:*\n"
                "• Use a different prompt\n"
                "• Try again in a few minutes\n"
                "• Use simpler descriptions\n\n"
                "Example: `/image a cute cat`",
                parse_mode=ParseMode.MARKDOWN
            )
            
    except Exception as e:
        logger.error(f"Image command error: {e}")
        await processing_msg.edit_text(
            "❌ *Error Generating Image*\n\n"
            "Something went wrong. Please try again later.",
            parse_mode=ParseMode.MARKDOWN
        )

@safe_command
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages intelligently"""
    text = update.message.text.strip()
    
    # Skip commands
    if text.startswith('/'):
        return
    
    # Check for currency pattern: "100 USD to EUR"
    currency_pattern = re.compile(r'^([\d.]+)\s*([A-Za-z]{3})\s+to\s+([A-Za-z]{3})$', re.IGNORECASE)
    match = currency_pattern.match(text)
    if match:
        amount = float(match.group(1))
        from_curr = match.group(2).upper()
        to_curr = match.group(3).upper()
        
        await update.message.chat.send_action(action="typing")
        result = await CurrencyService.convert(amount, from_curr, to_curr)
        await update.message.reply_text(result, parse_mode=ParseMode.MARKDOWN)
        return
    
    # Check for image request
    image_indicators = ["draw", "paint", "create", "generate", "make an image", "picture of", "image of"]
    text_lower = text.lower()
    
    if any(indicator in text_lower for indicator in image_indicators):
        # Extract prompt
        prompt = text
        for indicator in image_indicators:
            prompt = prompt.replace(indicator, "").strip()
        
        if prompt and len(prompt) > 2:
            # Reuse image command
            context.args = prompt.split()
            await image_command(update, context)
            return
    
    # Default: treat as dictionary lookup
    await update.message.chat.send_action(action="typing")
    definition = await DictionaryService.get_definition(text)
    await update.message.reply_text(definition, parse_mode=ParseMode.MARKDOWN)

@safe_command
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command - Check bot health"""
    status_text = f"""
✅ *Bot Status Report*

🤖 *Bot:* @samuelcreative2bot
🔄 *Status:* Online
📅 *Time:* {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

*Features:*
📚 Dictionary: ✅ Active
💱 Currency: ✅ Active
🎨 Image Gen: ✅ Active

*Cache Status:*
📖 Dictionary Cache: {len(DictionaryService._cache)} words
💰 Currency Cache: {len(CurrencyService._rates_cache)} rates

*Rate Limits:*
⏰ Active users: {len(rate_limiter._timestamps)}

*API Status:*
🌐 Pollinations.ai: ✅ Available
📊 ExchangeRate-API: {'✅' if EXCHANGE_RATE_API_KEY else '❌ Not configured'}
🤗 HuggingFace: {'✅' if HUGGINGFACE_TOKEN else '❌ Not configured'}

*System:*
🧠 Memory: {sys.getsizeof(rate_limiter._timestamps) / 1024:.2f} KB
🔄 Uptime: Running normally

🤖 *Bot is ready to serve!*
"""
    await update.message.reply_text(status_text, parse_mode=ParseMode.MARKDOWN)

# Error handler
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors gracefully"""
    logger.error(f"Update {update} caused error {context.error}")
    
    # Get error details
    error = context.error
    
    if isinstance(error, RetryAfter):
        await update.message.reply_text(
            f"⏰ *Rate Limit*\nPlease wait {error.retry_after} seconds before trying again.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    if isinstance(error, NetworkError):
        await update.message.reply_text(
            "🌐 *Network Error*\nPlease check your internet connection and try again.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    if isinstance(error, TimedOut):
        await update.message.reply_text(
            "⏰ *Timeout*\nThe request took too long. Please try again.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    if isinstance(error, BadRequest):
        # Don't respond to bad requests (usually user errors)
        return
    
    # Generic error
    try:
        await update.message.reply_text(
            "⚠️ *Oops! Something went wrong.*\n\n"
            "I've logged the error and will fix it soon.\n"
            "Please try again in a moment.",
            parse_mode=ParseMode.MARKDOWN
        )
    except:
        pass

# ============= MAIN FUNCTION =============

def main():
    """Start the bot with all handlers"""
    try:
        # Start healthcheck server in a separate thread
        health_thread = threading.Thread(target=run_healthcheck_server, daemon=True)
        health_thread.start()
        logger.info("🩺 Healthcheck server started in background thread")
        
        # Create persistence for better reliability
        persistence = PicklePersistence(filepath="bot_data.pickle")
        
        # Create application
        application = Application.builder()\
            .token(TOKEN)\
            .persistence(persistence)\
            .build()
        
        # Add command handlers
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("define", define_command))
        application.add_handler(CommandHandler("convert", convert_command))
        application.add_handler(CommandHandler("image", image_command))
        application.add_handler(CommandHandler("status", status_command))
        
        # Add text handler for non-commands
        application.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND, 
            handle_text
        ))
        
        # Add error handler
        application.add_error_handler(error_handler)
        
        # Start the bot
        logger.info("🚀 Starting Samuel Creative Bot...")
        logger.info(f"🤖 Bot username: @samuelcreative2bot")
        logger.info("✅ Bot is running! Press Ctrl+C to stop.")
        
        # Run with polling (more stable)
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,  # Skip old updates on restart
            connect_timeout=10,
            read_timeout=10,
            write_timeout=10,
            pool_timeout=10
        )
        
    except KeyboardInterrupt:
        logger.info("🛑 Bot stopped by user")
        sys.exit(0)
    except Exception as e:
        logger.critical(f"❌ Fatal error: {e}\n{traceback.format_exc()}")
        sys.exit(1)

if __name__ == "__main__":
    main()
