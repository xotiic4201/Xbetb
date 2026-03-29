# ============================================
# XBET CASINO - COMPLETE BACKEND (FIXED)
# Single File FastAPI Application
# ============================================

from fastapi import FastAPI, HTTPException, Depends, WebSocket, WebSocketDisconnect, status, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List, Dict, Any, Union
from datetime import datetime, timedelta
import jwt
import bcrypt
import random
import secrets
import json
import asyncio
from supabase import create_client, Client
import os
from decimal import Decimal, ROUND_DOWN
import hashlib
import hmac
import time
from enum import Enum
import uuid
import redis
import logging
import stripe
import requests
from functools import lru_cache
from dotenv import load_dotenv
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize FastAPI
app = FastAPI(title="XBet Casino - Premium Edition", version="3.0.0")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://xbet-inky.vercel.app"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Security
security = HTTPBearer()
SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM = "HS256"
JWT_EXPIRY = int(os.getenv("JWT_EXPIRY", 86400))

# Stripe Configuration
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

# Roblox Configuration
ROBLOX_API_KEY = os.getenv("ROBLOX_API_KEY")
ROBLOX_GROUP_ID = os.getenv("ROBLOX_GROUP_ID")
ROBLOX_PASS_IDS = {
    100: os.getenv("ROBLOX_PASS_100"),
    500: os.getenv("ROBLOX_PASS_500"),
    1000: os.getenv("ROBLOX_PASS_1000"),
    5000: os.getenv("ROBLOX_PASS_5000"),
    10000: os.getenv("ROBLOX_PASS_10000"),
    50000: os.getenv("ROBLOX_PASS_50000")
}

# SendGrid Configuration
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")
SENDGRID_FROM_EMAIL = os.getenv("SENDGRID_FROM_EMAIL")
SENDGRID_FROM_NAME = os.getenv("SENDGRID_FROM_NAME")
SENDGRID_REPLY_TO = os.getenv("SENDGRID_REPLY_TO")

# Database Configuration
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Redis for real-time data
try:
    redis_client = redis.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", 6379)),
        password=os.getenv("REDIS_PASSWORD", None),
        db=int(os.getenv("REDIS_DB", 0)),
        decode_responses=True,
        socket_connect_timeout=5
    )
    redis_client.ping()
    logger.info("Redis connected successfully")
except Exception as e:
    logger.warning(f"Redis connection failed: {e}")
    redis_client = None

# Models
class UserRole(str, Enum):
    USER = "user"
    VIP = "vip"
    PREMIUM = "premium"
    ADMIN = "admin"
    MODERATOR = "moderator"

class GameType(str, Enum):
    SLOTS = "slots"
    BLACKJACK = "blackjack"
    CRASH = "crash"
    MINES = "mines"
    PLINKO = "plinko"
    DICE = "dice"
    POKER = "poker"
    ROULETTE = "roulette"
    WHEEL = "wheel"

class UserRegister(BaseModel):
    username: str = Field(..., min_length=3, max_length=20)
    email: Optional[EmailStr] = None
    password: str = Field(..., min_length=6)
    roblox_id: Optional[str] = None
    referral_code: Optional[str] = None

class UserLogin(BaseModel):
    username: Optional[str] = None
    email: Optional[EmailStr] = None
    password: str
    roblox_id: Optional[str] = None

class GameBet(BaseModel):
    game: GameType
    xcoin_amount: float = Field(..., gt=0)
    params: Dict[str, Any] = {}

class RobloxPurchase(BaseModel):
    roblox_id: str
    product_id: str
    amount_robux: int

class StripePayment(BaseModel):
    amount_xcoin: float
    payment_method: str = "card"

# ============================================
# HELPER FUNCTIONS
# ============================================

def hash_password(password: str) -> str:
    """Hash password with bcrypt"""
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')

def verify_password(password: str, hashed: str) -> bool:
    """Verify password"""
    return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))

def create_access_token(data: dict) -> str:
    """Create JWT token"""
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(seconds=JWT_EXPIRY)
    to_encode.update({"exp": expire, "iat": datetime.utcnow()})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    """Get current user from token"""
    token = credentials.credentials
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        user = supabase.table("users").select("*").eq("id", user_id).execute()
        if not user.data or user.data[0].get("banned", False):
            raise HTTPException(status_code=401, detail="User not found or banned")
        
        return user.data[0]
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

def generate_server_seed() -> str:
    """Generate cryptographically secure server seed"""
    return secrets.token_hex(32)

def generate_client_seed() -> str:
    """Generate client seed"""
    return secrets.token_hex(16)

def provably_fair_hash(server_seed: str, client_seed: str, nonce: int) -> str:
    """Generate provably fair hash"""
    return hashlib.sha256(f"{server_seed}:{client_seed}:{nonce}".encode()).hexdigest()

# ============================================
# EMAIL SERVICE (SendGrid)
# ============================================

async def send_email(to_email: str, subject: str, html_content: str, text_content: str = None) -> bool:
    """Send email using SendGrid"""
    if not SENDGRID_API_KEY:
        logger.warning("SendGrid API key not configured")
        return False
    
    try:
        email_data = {
            "personalizations": [{
                "to": [{"email": to_email}],
                "reply_to": {"email": SENDGRID_REPLY_TO}
            }],
            "from": {
                "email": SENDGRID_FROM_EMAIL,
                "name": SENDGRID_FROM_NAME
            },
            "subject": subject,
            "content": [{
                "type": "text/html",
                "value": html_content
            }]
        }
        
        if text_content:
            email_data["content"].append({
                "type": "text/plain",
                "value": text_content
            })
        
        response = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={
                "Authorization": f"Bearer {SENDGRID_API_KEY}",
                "Content-Type": "application/json"
            },
            json=email_data,
            timeout=10
        )
        
        if response.status_code == 202:
            logger.info(f"Email sent to {to_email}: {subject}")
            return True
        else:
            logger.error(f"SendGrid error {response.status_code}: {response.text}")
            return False
            
    except Exception as e:
        logger.error(f"Email send error: {e}")
        return False

def get_welcome_email(username: str) -> str:
    """Welcome email HTML template"""
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Welcome to XBET Casino</title>
        <style>
            body {{
                font-family: Arial, sans-serif;
                line-height: 1.6;
                color: #333;
                background-color: #f4f4f4;
                margin: 0;
                padding: 0;
            }}
            .container {{
                max-width: 600px;
                margin: 20px auto;
                background: #ffffff;
                border-radius: 10px;
                overflow: hidden;
                box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            }}
            .header {{
                background: linear-gradient(135deg, #FFD966, #D4AF37);
                padding: 30px;
                text-align: center;
            }}
            .logo {{
                font-size: 32px;
                font-weight: bold;
                color: #0A0A0F;
                font-family: 'Orbitron', monospace;
            }}
            .content {{
                padding: 30px;
            }}
            .button {{
                display: inline-block;
                padding: 12px 24px;
                background: linear-gradient(135deg, #FFD966, #D4AF37);
                color: #0A0A0F;
                text-decoration: none;
                border-radius: 5px;
                margin: 20px 0;
            }}
            .footer {{
                background: #f4f4f4;
                padding: 20px;
                text-align: center;
                font-size: 12px;
                color: #666;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <div class="logo">XBET CASINO</div>
            </div>
            <div class="content">
                <h1>Welcome to XBET, {username}! 🎰</h1>
                <p>Thank you for joining the ultimate crypto casino experience!</p>
                <p>Your account has been successfully created with <strong>100 XCoin</strong> welcome bonus.</p>
                
                <h3>What you get:</h3>
                <ul>
                    <li>🎲 Provably Fair Games (Slots, Dice, Blackjack)</li>
                    <li>💰 Instant deposits via Stripe & Roblox</li>
                    <li>👑 VIP Program with exclusive benefits</li>
                    <li>🎁 Daily bonuses and promotions</li>
                </ul>
                
                <a href="https://xbet.com" class="button">Start Playing Now →</a>
                
                <p>Good luck and win big!</p>
            </div>
            <div class="footer">
                <p>© 2024 XBET Casino. All rights reserved.</p>
                <p>This is an automated message, please do not reply.</p>
            </div>
        </div>
    </body>
    </html>
    """

# ============================================
# AUTH ROUTES
# ============================================

@app.post("/api/auth/register")
async def register(user: UserRegister):
    """Register new user"""
    try:
        if user.email:
            existing = supabase.table("users").select("*").eq("email", user.email).execute()
            if existing.data:
                raise HTTPException(status_code=400, detail="Email already registered")
        
        existing_username = supabase.table("users").select("*").eq("username", user.username).execute()
        if existing_username.data:
            raise HTTPException(status_code=400, detail="Username already taken")
        
        if user.roblox_id:
            existing_roblox = supabase.table("users").select("*").eq("roblox_id", user.roblox_id).execute()
            if existing_roblox.data:
                raise HTTPException(status_code=400, detail="Roblox ID already linked")
        
        referral_code = secrets.token_hex(4).upper()
        user_id = str(uuid.uuid4())
        
        user_data = {
            "id": user_id,
            "username": user.username,
            "email": user.email,
            "roblox_id": user.roblox_id or "",
            "password_hash": hash_password(user.password),
            "xcoin_balance": 100.0,
            "role": "user",
            "vip_level": 1,
            "referral_code": referral_code,
            "total_bets": 0,
            "total_wagered": 0,
            "total_won": 0,
            "total_purchases": 0,
            "total_deposits": 0,
            "created_at": datetime.utcnow().isoformat(),
            "last_login": datetime.utcnow().isoformat()
        }
        
        supabase.table("users").insert(user_data).execute()
        
        if user.referral_code:
            referrer = supabase.table("users").select("*").eq("referral_code", user.referral_code).execute()
            if referrer.data:
                bonus = 50
                new_balance = referrer.data[0]["xcoin_balance"] + bonus
                supabase.table("users").update({"xcoin_balance": new_balance}).eq("id", referrer.data[0]["id"]).execute()
        
        token = create_access_token({"sub": user_id, "role": "user"})
        
        # Send welcome email if email provided
        if user.email and SENDGRID_API_KEY:
            asyncio.create_task(send_email(
                to_email=user.email,
                subject="Welcome to XBET Casino! 🎰",
                html_content=get_welcome_email(user.username)
            ))
        
        return {
            "token": token,
            "user": {
                "id": user_id,
                "username": user.username,
                "email": user.email,
                "roblox_id": user.roblox_id,
                "xcoin_balance": 100.0,
                "role": "user",
                "vip_level": 1,
                "referral_code": referral_code
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Registration error: {e}")
        raise HTTPException(status_code=500, detail="Registration failed")

@app.post("/api/auth/login")
async def login(user: UserLogin):
    """Login user"""
    try:
        query = None
        if user.email:
            query = supabase.table("users").select("*").eq("email", user.email)
        elif user.username:
            query = supabase.table("users").select("*").eq("username", user.username)
        elif user.roblox_id:
            query = supabase.table("users").select("*").eq("roblox_id", user.roblox_id)
        else:
            raise HTTPException(status_code=400, detail="Login method required")
        
        result = query.execute()
        
        if not result.data:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        
        user_data = result.data[0]
        
        if not verify_password(user.password, user_data["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        
        if user_data.get("banned", False):
            raise HTTPException(status_code=403, detail="Account banned")
        
        supabase.table("users").update({"last_login": datetime.utcnow().isoformat()}).eq("id", user_data["id"]).execute()
        
        token = create_access_token({"sub": user_data["id"], "role": user_data["role"]})
        
        if redis_client:
            redis_client.sadd("online_users", user_data["id"])
        
        return {
            "token": token,
            "user": {
                "id": user_data["id"],
                "username": user_data["username"],
                "email": user_data["email"],
                "roblox_id": user_data.get("roblox_id", ""),
                "xcoin_balance": user_data["xcoin_balance"],
                "role": user_data["role"],
                "vip_level": user_data.get("vip_level", 1),
                "referral_code": user_data.get("referral_code", "")
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Login error: {e}")
        raise HTTPException(status_code=500, detail="Login failed")

@app.get("/api/user/balance")
async def get_balance(current_user: dict = Depends(get_current_user)):
    """Get user balance"""
    return {
        "id": current_user["id"],
        "username": current_user["username"],
        "xcoin_balance": current_user["xcoin_balance"],
        "role": current_user["role"],
        "vip_level": current_user.get("vip_level", 1)
    }

# ============================================
# PAYMENT ROUTES
# ============================================

@app.post("/api/payments/create-stripe-session")
async def create_stripe_session(payment: StripePayment, current_user: dict = Depends(get_current_user)):
    """Create Stripe checkout session"""
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=400, detail="Stripe not configured")
    
    try:
        usd_amount = payment.amount_xcoin * 0.01
        
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'usd',
                    'product_data': {
                        'name': f'{payment.amount_xcoin} XCoin',
                        'description': 'Premium casino credits for XBet',
                    },
                    'unit_amount': int(usd_amount * 100),
                },
                'quantity': 1,
            }],
            mode='payment',
            success_url=f"{os.getenv('FRONTEND_URL', 'http://localhost:3000')}/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{os.getenv('FRONTEND_URL', 'http://localhost:3000')}/cancel",
            metadata={
                'user_id': current_user['id'],
                'xcoin_amount': str(payment.amount_xcoin)
            }
        )
        
        return {"session_id": session.id, "url": session.url}
    except Exception as e:
        logger.error(f"Stripe session error: {e}")
        raise HTTPException(status_code=500, detail="Payment creation failed")

@app.post("/api/payments/roblox-purchase")
async def roblox_purchase(purchase: RobloxPurchase, current_user: dict = Depends(get_current_user)):
    """Process Roblox purchase"""
    try:
        xcoin_amount = purchase.amount_robux
        new_balance = current_user["xcoin_balance"] + xcoin_amount
        
        supabase.table("users").update({
            "xcoin_balance": new_balance,
            "total_purchases": current_user.get("total_purchases", 0) + 1,
            "total_deposits": current_user.get("total_deposits", 0) + xcoin_amount
        }).eq("id", current_user["id"]).execute()
        
        return {
            "success": True,
            "xcoin_added": xcoin_amount,
            "new_balance": new_balance,
            "message": f"Added {xcoin_amount} XCoin to your account!"
        }
    except Exception as e:
        logger.error(f"Roblox purchase error: {e}")
        raise HTTPException(status_code=500, detail="Purchase processing failed")

@app.get("/api/payments/roblox-products")
async def get_roblox_products():
    """Get available Roblox products"""
    return {
        "products": [
            {"robux": 100, "xcoin": 100, "price_usd": 1.00, "product_id": ROBLOX_PASS_IDS.get(100, "xbet_100_robux")},
            {"robux": 500, "xcoin": 500, "price_usd": 5.00, "product_id": ROBLOX_PASS_IDS.get(500, "xbet_500_robux")},
            {"robux": 1000, "xcoin": 1000, "price_usd": 10.00, "product_id": ROBLOX_PASS_IDS.get(1000, "xbet_1000_robux")},
            {"robux": 5000, "xcoin": 5000, "price_usd": 50.00, "product_id": ROBLOX_PASS_IDS.get(5000, "xbet_5000_robux")},
            {"robux": 10000, "xcoin": 10000, "price_usd": 100.00, "product_id": ROBLOX_PASS_IDS.get(10000, "xbet_10000_robux")},
            {"robux": 50000, "xcoin": 50000, "price_usd": 500.00, "product_id": ROBLOX_PASS_IDS.get(50000, "xbet_50000_robux"), "vip_bonus": True}
        ]
    }

# ============================================
# GAME: SLOTS
# ============================================

SLOTS_SYMBOLS = ["cherry", "lemon", "orange", "plum", "bell", "xbet", "diamond", "crown"]
SLOTS_PAYOUTS = {
    "crown": {3: 200, 4: 1000, 5: 5000},
    "diamond": {3: 150, 4: 750, 5: 2500},
    "xbet": {3: 100, 4: 500, 5: 1000},
    "bell": {3: 50, 4: 200, 5: 500},
    "plum": {3: 25, 4: 100, 5: 250},
    "orange": {3: 15, 4: 50, 5: 150},
    "lemon": {3: 10, 4: 30, 5: 100},
    "cherry": {3: 5, 4: 20, 5: 50}
}

@app.post("/api/games/slots/play")
async def play_slots(bet: GameBet, current_user: dict = Depends(get_current_user)):
    """Play slot machine"""
    try:
        if bet.xcoin_amount > current_user["xcoin_balance"]:
            raise HTTPException(status_code=400, detail="Insufficient balance")
        
        server_seed = generate_server_seed()
        client_seed = bet.params.get("client_seed", generate_client_seed())
        nonce = random.randint(1, 1000000)
        
        hash_result = provably_fair_hash(server_seed, client_seed, nonce)
        
        grid = []
        for i in range(3):
            row = []
            for j in range(5):
                pos = int(hash_result[(i * 5 + j) * 2:(i * 5 + j) * 2 + 2], 16) % 8
                row.append(SLOTS_SYMBOLS[pos])
            grid.append(row)
        
        total_payout = 0
        for col in range(5):
            symbol = grid[1][col]
            count = 1
            if grid[0][col] == symbol:
                count += 1
            if grid[2][col] == symbol:
                count += 1
            
            if count >= 3 and symbol in SLOTS_PAYOUTS:
                total_payout += SLOTS_PAYOUTS[symbol].get(count, 0)
        
        win_amount = bet.xcoin_amount * (total_payout / 100)
        new_balance = current_user["xcoin_balance"] - bet.xcoin_amount + win_amount
        
        supabase.table("users").update({
            "xcoin_balance": new_balance,
            "total_bets": current_user.get("total_bets", 0) + 1,
            "total_wagered": current_user.get("total_wagered", 0) + bet.xcoin_amount,
            "total_won": current_user.get("total_won", 0) + win_amount
        }).eq("id", current_user["id"]).execute()
        
        if redis_client:
            redis_client.incr("total_bets")
            redis_client.incrbyfloat("total_wagered", bet.xcoin_amount)
        
        return {
            "result": {"reel_grid": grid, "total_payout": total_payout},
            "outcome": "win" if win_amount > 0 else "lose",
            "win_amount": win_amount,
            "new_balance": new_balance,
            "multiplier": total_payout / 100,
            "server_seed": server_seed,
            "client_seed": client_seed,
            "nonce": nonce
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Slots error: {e}")
        raise HTTPException(status_code=500, detail="Game error")

# ============================================
# GAME: DICE
# ============================================

@app.post("/api/games/dice/play")
async def play_dice(bet: GameBet, current_user: dict = Depends(get_current_user)):
    """Play dice game"""
    try:
        if bet.xcoin_amount > current_user["xcoin_balance"]:
            raise HTTPException(status_code=400, detail="Insufficient balance")
        
        target = bet.params.get("target", 50)
        condition = bet.params.get("condition", "under")
        
        server_seed = generate_server_seed()
        client_seed = bet.params.get("client_seed", generate_client_seed())
        nonce = random.randint(1, 1000000)
        
        hash_result = provably_fair_hash(server_seed, client_seed, nonce)
        roll = (int(hash_result[:8], 16) % 10001) / 100
        
        win = False
        if condition == "under" and roll < target:
            win = True
        elif condition == "over" and roll > target:
            win = True
        
        multiplier = 99 / target if condition == "under" else 99 / (100 - target)
        win_amount = bet.xcoin_amount * multiplier if win else 0
        new_balance = current_user["xcoin_balance"] - bet.xcoin_amount + win_amount
        
        supabase.table("users").update({
            "xcoin_balance": new_balance,
            "total_bets": current_user.get("total_bets", 0) + 1,
            "total_wagered": current_user.get("total_wagered", 0) + bet.xcoin_amount,
            "total_won": current_user.get("total_won", 0) + win_amount
        }).eq("id", current_user["id"]).execute()
        
        return {
            "result": {"roll": roll, "condition": condition, "target": target},
            "outcome": "win" if win else "lose",
            "win_amount": win_amount,
            "multiplier": multiplier if win else 0,
            "new_balance": new_balance,
            "server_seed": server_seed,
            "client_seed": client_seed,
            "nonce": nonce
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Dice error: {e}")
        raise HTTPException(status_code=500, detail="Game error")

# ============================================
# ADMIN ROUTES
# ============================================

@app.get("/api/admin/users")
async def get_users(current_user: dict = Depends(get_current_user)):
    """Get all users (admin only)"""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    
    users = supabase.table("users").select("*").execute()
    return {"users": users.data}

@app.put("/api/admin/users/{user_id}/ban")
async def ban_user(user_id: str, ban_data: Dict, current_user: dict = Depends(get_current_user)):
    """Ban or unban user"""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    
    supabase.table("users").update({"banned": ban_data.get("banned", True)}).eq("id", user_id).execute()
    return {"message": "User updated"}

@app.put("/api/admin/users/{user_id}/balance")
async def update_balance(user_id: str, balance_data: Dict, current_user: dict = Depends(get_current_user)):
    """Update user balance"""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    
    supabase.table("users").update({"xcoin_balance": balance_data.get("balance", 0)}).eq("id", user_id).execute()
    return {"message": "Balance updated"}

# ============================================
# STATS ROUTES
# ============================================

@app.get("/api/leaderboard")
async def get_leaderboard():
    """Get top players leaderboard"""
    users = supabase.table("users").select("username,xcoin_balance,role").order("xcoin_balance", desc=True).limit(10).execute()
    return {"players": users.data}

@app.get("/api/online-players")
async def get_online_players():
    """Get online players count"""
    count = redis_client.scard("online_users") if redis_client else 0
    return {"count": count}

@app.get("/api/stats")
async def get_stats():
    """Get global stats"""
    total_bets = redis_client.get("total_bets") if redis_client else 0
    total_wagered = redis_client.get("total_wagered") if redis_client else 0
    online = redis_client.scard("online_users") if redis_client else 0
    
    return {
        "total_bets": int(total_bets or 0),
        "total_wagered": float(total_wagered or 0),
        "online_players": online
    }

# ============================================
# REWARD ROUTES
# ============================================

@app.post("/api/rewards/daily")
async def claim_daily_bonus(current_user: dict = Depends(get_current_user)):
    """Claim daily bonus"""
    last_claim = current_user.get("last_daily_claim")
    
    if last_claim:
        last_date = datetime.fromisoformat(last_claim)
        if (datetime.utcnow() - last_date).days < 1:
            raise HTTPException(status_code=400, detail="Already claimed today")
    
    bonus = 100.0
    new_balance = current_user["xcoin_balance"] + bonus
    
    supabase.table("users").update({
        "xcoin_balance": new_balance,
        "last_daily_claim": datetime.utcnow().isoformat()
    }).eq("id", current_user["id"]).execute()
    
    return {"bonus": bonus, "new_balance": new_balance}

# ============================================
# WEBSOCKET
# ============================================

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
    
    async def connect(self, websocket: WebSocket, user_id: str):
        await websocket.accept()
        self.active_connections[user_id] = websocket
        if redis_client:
            redis_client.sadd("online_users", user_id)
    
    def disconnect(self, user_id: str):
        if user_id in self.active_connections:
            del self.active_connections[user_id]
        if redis_client:
            redis_client.srem("online_users", user_id)
    
    async def broadcast(self, message: str):
        for connection in self.active_connections.values():
            try:
                await connection.send_text(message)
            except:
                pass

manager = ConnectionManager()

@app.websocket("/ws/{token}")
async def websocket_endpoint(websocket: WebSocket, token: str):
    """WebSocket endpoint"""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        
        if not user_id:
            await websocket.close(code=1008)
            return
        
        await manager.connect(websocket, user_id)
        
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            
            if message.get("type") == "chat":
                user = supabase.table("users").select("username").eq("id", user_id).execute()
                if user.data:
                    await manager.broadcast(json.dumps({
                        "type": "chat",
                        "username": user.data[0]["username"],
                        "message": message.get("message", ""),
                        "timestamp": datetime.utcnow().isoformat()
                    }))
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        manager.disconnect(user_id)

# ============================================
# HEALTH CHECK
# ============================================

@app.get("/api/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "version": "3.0.0"
    }

# ============================================
# INITIALIZATION
# ============================================

@app.on_event("startup")
async def startup_event():
    """Initialize on startup"""
    init_database()
    logger.info("XBET Casino backend started successfully")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", 5000)),
        reload=os.getenv("ENVIRONMENT", "development") == "development"
    )
