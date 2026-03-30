from fastapi import FastAPI, HTTPException, Depends, WebSocket, status, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
import jwt
import bcrypt
import random
import secrets
import json
import asyncio
from supabase import create_client, Client
import os
import uuid
import logging
import stripe
import requests
from enum import Enum
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize FastAPI
app = FastAPI(title="XBET Casino API", version="1.0.0")

# CORS - Allow all origins
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

# Admin Credentials
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
ADMIN_REFERRAL_CODE = os.getenv("ADMIN_REFERRAL_CODE")

# Stripe Configuration
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY
    logger.info("Stripe configured successfully")
else:
    logger.warning("Stripe secret key not configured")

# Roblox Configuration
ROBLOX_API_KEY = os.getenv("ROBLOX_API_KEY", "")
ROBLOX_GROUP_ID = os.getenv("ROBLOX_GROUP_ID", "")
ROBLOX_PASS_IDS = {
    100: os.getenv("ROBLOX_PASS_100"),
    500: os.getenv("ROBLOX_PASS_500"),
    1000: os.getenv("ROBLOX_PASS_1000"),
    5000: os.getenv("ROBLOX_PASS_5000"),
    10000: os.getenv("ROBLOX_PASS_10000"),
    50000: os.getenv("ROBLOX_PASS_50000")
}

# SendGrid Configuration
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY", "")
SENDGRID_FROM_EMAIL = os.getenv("SENDGRID_FROM_EMAIL")
SENDGRID_FROM_NAME = os.getenv("SENDGRID_FROM_NAME", "XBET Casino")
SENDGRID_REPLY_TO = os.getenv("SENDGRID_REPLY_TO")

if SENDGRID_API_KEY:
    logger.info("SendGrid configured successfully")
else:
    logger.warning("SendGrid API key not configured")

# Database Configuration
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

if not SUPABASE_URL or not SUPABASE_KEY:
    logger.error("Supabase credentials not configured!")

try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    logger.info("Supabase connected successfully")
except Exception as e:
    logger.error(f"Supabase connection failed: {e}")
    supabase = None

# ============================================
# MODELS
# ============================================

class UserRole(str, Enum):
    USER = "user"
    ADMIN = "admin"

class GameType(str, Enum):
    SLOTS = "slots"
    DICE = "dice"
    CRASH = "crash"
    MINES = "mines"
    PLINKO = "plinko"
    BLACKJACK = "blackjack"

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

class EmailRequest(BaseModel):
    to_email: str
    subject: str
    content: str

# ============================================
# HELPER FUNCTIONS
# ============================================

def hash_password(password: str) -> str:
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')

def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))

def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(seconds=JWT_EXPIRY)
    to_encode.update({"exp": expire, "iat": datetime.utcnow()})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    token = credentials.credentials
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        user = supabase.table("users").select("*").eq("id", user_id).execute()
        if not user.data:
            raise HTTPException(status_code=401, detail="User not found")
        
        if user.data[0].get("banned", False):
            raise HTTPException(status_code=403, detail="Account banned")
        
        return user.data[0]
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

async def send_email(to_email: str, subject: str, content: str):
    """Send email using SendGrid"""
    if not SENDGRID_API_KEY:
        logger.warning("SendGrid not configured, skipping email")
        return False
    
    try:
        data = {
            "personalizations": [{"to": [{"email": to_email}]}],
            "from": {"email": SENDGRID_FROM_EMAIL, "name": SENDGRID_FROM_NAME},
            "reply_to": {"email": SENDGRID_REPLY_TO},
            "subject": subject,
            "content": [{"type": "text/html", "value": content}]
        }
        
        headers = {
            "Authorization": f"Bearer {SENDGRID_API_KEY}",
            "Content-Type": "application/json"
        }
        
        response = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            json=data,
            headers=headers
        )
        
        if response.status_code == 202:
            logger.info(f"Email sent to {to_email}")
            return True
        else:
            logger.error(f"SendGrid error: {response.text}")
            return False
    except Exception as e:
        logger.error(f"Email send error: {e}")
        return False

async def verify_roblox_purchase(user_id: str, product_id: str, amount_robux: int) -> bool:
    """Verify Roblox purchase (simplified - in production, use Roblox API)"""
    if not ROBLOX_API_KEY:
        logger.warning("Roblox API not configured")
        return True  # Allow for testing
    
    try:
        # This is a simplified verification. In production, you should:
        # 1. Call Roblox API to verify the purchase
        # 2. Check if the user actually owns the game pass
        # 3. Verify the purchase hasn't been used before
        
        # For now, we'll simulate verification
        return True
    except Exception as e:
        logger.error(f"Roblox verification error: {e}")
        return False

# ============================================
# DATABASE INITIALIZATION
# ============================================

def init_database():
    if not supabase:
        return
    
    try:
        # Create users table if not exists (run this in Supabase SQL editor)
        # Check if admin exists
        admin_check = supabase.table("users").select("*").eq("email", ADMIN_EMAIL).execute()
        
        if not admin_check.data:
            admin_id = str(uuid.uuid4())
            admin_data = {
                "id": admin_id,
                "username": ADMIN_USERNAME,
                "email": ADMIN_EMAIL,
                "password_hash": hash_password(ADMIN_PASSWORD),
                "xcoin_balance": 10000000.0,
                "role": "admin",
                "vip_level": 10,
                "referral_code": ADMIN_REFERRAL_CODE,
                "total_bets": 0,
                "total_wagered": 0,
                "total_won": 0,
                "total_purchases": 0,
                "total_deposits": 10000000.0,
                "created_at": datetime.utcnow().isoformat(),
                "last_login": datetime.utcnow().isoformat(),
                "banned": False,
                "email_verified": True
            }
            
            supabase.table("users").insert(admin_data).execute()
            logger.info(f"Admin user created: {ADMIN_USERNAME}")
    except Exception as e:
        logger.error(f"Database init error: {e}")

# ============================================
# AUTH ROUTES
# ============================================

@app.get("/")
async def root():
    return {
        "message": "XBET Casino API",
        "status": "online",
        "version": "4.0.0",
        "features": {
            "stripe": bool(STRIPE_SECRET_KEY),
            "roblox": bool(ROBLOX_API_KEY),
            "sendgrid": bool(SENDGRID_API_KEY)
        }
    }

@app.get("/api/health")
async def health_check():
    return {
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "services": {
            "supabase": supabase is not None,
            "stripe": bool(STRIPE_SECRET_KEY),
            "roblox": bool(ROBLOX_API_KEY),
            "sendgrid": bool(SENDGRID_API_KEY)
        }
    }

@app.post("/api/auth/register")
async def register(user: UserRegister, background_tasks: BackgroundTasks):
    try:
        existing = supabase.table("users").select("*").eq("username", user.username).execute()
        if existing.data:
            return JSONResponse(status_code=400, content={"detail": "Username already taken"})
        
        if user.email:
            existing_email = supabase.table("users").select("*").eq("email", user.email).execute()
            if existing_email.data:
                return JSONResponse(status_code=400, content={"detail": "Email already registered"})
        
        if user.roblox_id:
            existing_roblox = supabase.table("users").select("*").eq("roblox_id", user.roblox_id).execute()
            if existing_roblox.data:
                return JSONResponse(status_code=400, content={"detail": "Roblox ID already linked"})
        
        referral_code = secrets.token_hex(4).upper()
        user_id = str(uuid.uuid4())
        
        user_data = {
            "id": user_id,
            "username": user.username,
            "email": user.email,
            "roblox_id": user.roblox_id or "",
            "password_hash": hash_password(user.password),
            "xcoin_balance": 1000.0,
            "role": "user",
            "vip_level": 1,
            "referral_code": referral_code,
            "total_bets": 0,
            "total_wagered": 0,
            "total_won": 0,
            "total_purchases": 0,
            "total_deposits": 0,
            "created_at": datetime.utcnow().isoformat(),
            "last_login": datetime.utcnow().isoformat(),
            "banned": False,
            "email_verified": False
        }
        
        result = supabase.table("users").insert(user_data).execute()
        
        if not result.data:
            return JSONResponse(status_code=500, content={"detail": "Failed to create user"})
        
        # Handle referral
        if user.referral_code:
            try:
                referrer = supabase.table("users").select("*").eq("referral_code", user.referral_code).execute()
                if referrer.data:
                    new_balance = referrer.data[0]["xcoin_balance"] + 50
                    supabase.table("users").update({"xcoin_balance": new_balance}).eq("id", referrer.data[0]["id"]).execute()
                    
                    # Send referral notification email
                    if referrer.data[0].get("email") and SENDGRID_API_KEY:
                        background_tasks.add_task(
                            send_email,
                            referrer.data[0]["email"],
                            "Referral Bonus Earned!",
                            f"<h2>You earned 50 XCoin!</h2><p>Your friend {user.username} signed up using your referral code.</p>"
                        )
            except Exception as e:
                logger.error(f"Referral error: {e}")
        
        # Send welcome email
        if user.email and SENDGRID_API_KEY:
            background_tasks.add_task(
                send_email,
                user.email,
                "Welcome to XBET Casino!",
                f"""
                <h1>Welcome to XBET, {user.username}!</h1>
                <p>You've received 1000 XCoin as a welcome bonus!</p>
                <p>Start playing now and win big!</p>
                <a href="https://xbet-inky.vercel.app">Play Now</a>
                """
            )
        
        token = create_access_token({"sub": user_id, "role": "user"})
        
        return {
            "token": token,
            "user": {
                "id": user_id,
                "username": user.username,
                "email": user.email,
                "roblox_id": user.roblox_id or "",
                "xcoin_balance": 1000.0,
                "role": "user",
                "vip_level": 1,
                "referral_code": referral_code
            }
        }
        
    except Exception as e:
        logger.error(f"Registration error: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": f"Registration failed: {str(e)}"})

@app.post("/api/auth/login")
async def login(user: UserLogin):
    try:
        result = None
        if user.email:
            result = supabase.table("users").select("*").eq("email", user.email).execute()
        elif user.username:
            result = supabase.table("users").select("*").eq("username", user.username).execute()
        else:
            return JSONResponse(status_code=400, content={"detail": "Email or username required"})
        
        if not result.data:
            return JSONResponse(status_code=401, content={"detail": "Invalid credentials"})
        
        user_data = result.data[0]
        
        if not verify_password(user.password, user_data["password_hash"]):
            return JSONResponse(status_code=401, content={"detail": "Invalid credentials"})
        
        if user_data.get("banned", False):
            return JSONResponse(status_code=403, content={"detail": "Account banned"})
        
        supabase.table("users").update({
            "last_login": datetime.utcnow().isoformat()
        }).eq("id", user_data["id"]).execute()
        
        token = create_access_token({"sub": user_data["id"], "role": user_data["role"]})
        
        return {
            "token": token,
            "user": {
                "id": user_data["id"],
                "username": user_data["username"],
                "email": user_data.get("email", ""),
                "roblox_id": user_data.get("roblox_id", ""),
                "xcoin_balance": user_data["xcoin_balance"],
                "role": user_data["role"],
                "vip_level": user_data.get("vip_level", 1),
                "referral_code": user_data.get("referral_code", "")
            }
        }
        
    except Exception as e:
        logger.error(f"Login error: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": f"Login failed: {str(e)}"})

@app.get("/api/user/balance")
async def get_balance(current_user: dict = Depends(get_current_user)):
    return {
        "id": current_user["id"],
        "username": current_user["username"],
        "xcoin_balance": current_user["xcoin_balance"],
        "role": current_user["role"],
        "vip_level": current_user.get("vip_level", 1)
    }

# ============================================
# GAME: SLOTS (4% House Edge)
# ============================================

SLOTS_SYMBOLS = ["🍒", "🍋", "🍊", "🍇", "🔔", "⭐", "💎", "👑"]
SLOTS_WEIGHTS = [30, 25, 20, 12, 6, 4, 2, 1]
SLOTS_PAYOUTS = {
    "👑": {3: 100, 4: 500, 5: 5000},
    "💎": {3: 75, 4: 250, 5: 2500},
    "⭐": {3: 50, 4: 100, 5: 1000},
    "🔔": {3: 25, 4: 75, 5: 500},
    "🍇": {3: 15, 4: 40, 5: 250},
    "🍊": {3: 10, 4: 25, 5: 150},
    "🍋": {3: 5, 4: 15, 5: 100},
    "🍒": {3: 2, 4: 8, 5: 50}
}

def get_weighted_symbol():
    roll = random.randint(1, 100)
    cumulative = 0
    for i, weight in enumerate(SLOTS_WEIGHTS):
        cumulative += weight
        if roll <= cumulative:
            return SLOTS_SYMBOLS[i]
    return "🍒"

@app.post("/api/games/slots/play")
async def play_slots(bet: GameBet, current_user: dict = Depends(get_current_user)):
    try:
        if bet.xcoin_amount > current_user["xcoin_balance"]:
            return JSONResponse(status_code=400, content={"detail": "Insufficient balance"})
        
        grid = [[get_weighted_symbol() for _ in range(5)] for _ in range(3)]
        
        total_multiplier = 0
        for col in range(5):
            symbol = grid[1][col]
            count = 1
            if grid[0][col] == symbol:
                count += 1
            if grid[2][col] == symbol:
                count += 1
            
            if count >= 3 and symbol in SLOTS_PAYOUTS:
                total_multiplier += SLOTS_PAYOUTS[symbol].get(count, 0)
        
        win_amount = bet.xcoin_amount * (total_multiplier / 100)
        new_balance = current_user["xcoin_balance"] - bet.xcoin_amount + win_amount
        
        supabase.table("users").update({
            "xcoin_balance": new_balance,
            "total_bets": current_user.get("total_bets", 0) + 1,
            "total_wagered": current_user.get("total_wagered", 0) + bet.xcoin_amount,
            "total_won": current_user.get("total_won", 0) + win_amount
        }).eq("id", current_user["id"]).execute()
        
        return {
            "result": {"reel_grid": grid, "total_multiplier": total_multiplier},
            "outcome": "win" if win_amount > bet.xcoin_amount else "lose",
            "win_amount": round(win_amount, 2),
            "new_balance": round(new_balance, 2),
            "multiplier": total_multiplier / 100
        }
    except Exception as e:
        logger.error(f"Slots error: {e}")
        return JSONResponse(status_code=500, content={"detail": "Game error"})

# ============================================
# GAME: DICE (2% House Edge)
# ============================================

HOUSE_EDGE_DICE = 0.98

@app.post("/api/games/dice/play")
async def play_dice(bet: GameBet, current_user: dict = Depends(get_current_user)):
    try:
        if bet.xcoin_amount > current_user["xcoin_balance"]:
            return JSONResponse(status_code=400, content={"detail": "Insufficient balance"})
        
        target = bet.params.get("target", 50)
        condition = bet.params.get("condition", "under")
        
        if target < 2 or target > 98:
            return JSONResponse(status_code=400, content={"detail": "Target must be between 2 and 98"})
        
        roll = random.uniform(0, 100)
        
        win = False
        if condition == "under" and roll < target:
            win = True
        elif condition == "over" and roll > target:
            win = True
        
        if condition == "under":
            fair_multiplier = 100 / target
        else:
            fair_multiplier = 100 / (100 - target)
        
        actual_multiplier = fair_multiplier * HOUSE_EDGE_DICE
        win_amount = bet.xcoin_amount * actual_multiplier if win else 0
        new_balance = current_user["xcoin_balance"] - bet.xcoin_amount + win_amount
        
        supabase.table("users").update({
            "xcoin_balance": new_balance,
            "total_bets": current_user.get("total_bets", 0) + 1,
            "total_wagered": current_user.get("total_wagered", 0) + bet.xcoin_amount,
            "total_won": current_user.get("total_won", 0) + win_amount
        }).eq("id", current_user["id"]).execute()
        
        return {
            "result": {"roll": round(roll, 2), "condition": condition, "target": target},
            "outcome": "win" if win else "lose",
            "win_amount": round(win_amount, 2),
            "multiplier": round(actual_multiplier, 2) if win else 0,
            "new_balance": round(new_balance, 2)
        }
    except Exception as e:
        logger.error(f"Dice error: {e}")
        return JSONResponse(status_code=500, content={"detail": "Game error"})

# ============================================
# GAME: CRASH (3% House Edge)
# ============================================

CRASH_HOUSE_EDGE = 0.03

@app.post("/api/games/crash/play")
async def play_crash(bet: GameBet, current_user: dict = Depends(get_current_user)):
    try:
        if bet.xcoin_amount > current_user["xcoin_balance"]:
            return JSONResponse(status_code=400, content={"detail": "Insufficient balance"})
        
        random_value = random.random()
        crash_point = 1 / (1 - random_value) * (1 - CRASH_HOUSE_EDGE)
        crash_point = min(crash_point, 10000)
        
        cashout_at = bet.params.get("cashout_at", 1.0)
        
        win = cashout_at < crash_point
        multiplier = cashout_at if win else 0
        win_amount = bet.xcoin_amount * multiplier if win else 0
        new_balance = current_user["xcoin_balance"] - bet.xcoin_amount + win_amount
        
        supabase.table("users").update({
            "xcoin_balance": new_balance,
            "total_bets": current_user.get("total_bets", 0) + 1,
            "total_wagered": current_user.get("total_wagered", 0) + bet.xcoin_amount,
            "total_won": current_user.get("total_won", 0) + win_amount
        }).eq("id", current_user["id"]).execute()
        
        return {
            "result": {"crash_point": round(crash_point, 2), "cashout_at": cashout_at},
            "outcome": "win" if win else "lose",
            "win_amount": round(win_amount, 2),
            "multiplier": multiplier,
            "new_balance": round(new_balance, 2)
        }
    except Exception as e:
        logger.error(f"Crash error: {e}")
        return JSONResponse(status_code=500, content={"detail": "Game error"})

# ============================================
# GAME: MINES (5% House Edge)
# ============================================

MINES_MULTIPLIERS = {
    0: 1.00, 1: 1.20, 2: 1.45, 3: 1.80, 4: 2.20, 5: 2.70,
    6: 3.30, 7: 4.10, 8: 5.00, 9: 6.20, 10: 7.80, 11: 9.80,
    12: 12.50, 13: 16.00, 14: 20.50, 15: 26.50, 16: 34.00,
    17: 44.00, 18: 57.00, 19: 74.00, 20: 96.00
}

@app.post("/api/games/mines/play")
async def play_mines(bet: GameBet, current_user: dict = Depends(get_current_user)):
    try:
        if bet.xcoin_amount > current_user["xcoin_balance"]:
            return JSONResponse(status_code=400, content={"detail": "Insufficient balance"})
        
        revealed_count = bet.params.get("revealed_count", 0)
        position = bet.params.get("position", None)
        
        if position is not None:
            # Check if hit mine (5 mines in 25 tiles = 20% chance)
            is_mine = random.random() < 0.2
            
            if is_mine:
                new_balance = current_user["xcoin_balance"] - bet.xcoin_amount
                supabase.table("users").update({
                    "xcoin_balance": new_balance,
                    "total_bets": current_user.get("total_bets", 0) + 1,
                    "total_wagered": current_user.get("total_wagered", 0) + bet.xcoin_amount
                }).eq("id", current_user["id"]).execute()
                
                return {
                    "outcome": "lose",
                    "win_amount": 0,
                    "new_balance": round(new_balance, 2),
                    "hit_mine": True
                }
            
            multiplier = MINES_MULTIPLIERS.get(revealed_count + 1, 100) * 0.95  # 5% house edge
            win_amount = bet.xcoin_amount * multiplier
            new_balance = current_user["xcoin_balance"] - bet.xcoin_amount + win_amount
            
            supabase.table("users").update({
                "xcoin_balance": new_balance,
                "total_bets": current_user.get("total_bets", 0) + 1,
                "total_wagered": current_user.get("total_wagered", 0) + bet.xcoin_amount,
                "total_won": current_user.get("total_won", 0) + win_amount
            }).eq("id", current_user["id"]).execute()
            
            return {
                "outcome": "win" if win_amount > bet.xcoin_amount else "continue",
                "win_amount": round(win_amount, 2),
                "new_balance": round(new_balance, 2),
                "multiplier": round(multiplier, 2),
                "hit_mine": False
            }
        
        return {"game_started": True, "bet_amount": bet.xcoin_amount}
        
    except Exception as e:
        logger.error(f"Mines error: {e}")
        return JSONResponse(status_code=500, content={"detail": "Game error"})

# ============================================
# GAME: PLINKO
# ============================================

PLINKO_PAYOUTS = {
    "low": [0.5, 1, 1.5, 2, 1.5, 1, 0.5],
    "medium": [0.2, 0.5, 1, 2, 5, 2, 1, 0.5, 0.2],
    "high": [0.1, 0.2, 0.5, 1, 2, 5, 10, 5, 2, 1, 0.5, 0.2, 0.1]
}
RISK_HOUSE_EDGE = {"low": 0.01, "medium": 0.03, "high": 0.05}

@app.post("/api/games/plinko/play")
async def play_plinko(bet: GameBet, current_user: dict = Depends(get_current_user)):
    try:
        if bet.xcoin_amount > current_user["xcoin_balance"]:
            return JSONResponse(status_code=400, content={"detail": "Insufficient balance"})
        
        risk = bet.params.get("risk", "medium")
        if risk not in PLINKO_PAYOUTS:
            risk = "medium"
        
        payouts = PLINKO_PAYOUTS[risk]
        bucket = random.randint(0, len(payouts) - 1)
        multiplier = payouts[bucket] * (1 - RISK_HOUSE_EDGE.get(risk, 0.03))
        
        win_amount = bet.xcoin_amount * multiplier
        new_balance = current_user["xcoin_balance"] - bet.xcoin_amount + win_amount
        
        supabase.table("users").update({
            "xcoin_balance": new_balance,
            "total_bets": current_user.get("total_bets", 0) + 1,
            "total_wagered": current_user.get("total_wagered", 0) + bet.xcoin_amount,
            "total_won": current_user.get("total_won", 0) + win_amount
        }).eq("id", current_user["id"]).execute()
        
        return {
            "result": {"bucket": bucket, "risk": risk},
            "outcome": "win" if multiplier > 1 else "lose",
            "win_amount": round(win_amount, 2),
            "multiplier": round(multiplier, 2),
            "new_balance": round(new_balance, 2)
        }
    except Exception as e:
        logger.error(f"Plinko error: {e}")
        return JSONResponse(status_code=500, content={"detail": "Game error"})

# ============================================
# GAME: BLACKJACK
# ============================================

@app.post("/api/games/blackjack/play")
async def play_blackjack(bet: GameBet, current_user: dict = Depends(get_current_user)):
    try:
        if bet.xcoin_amount > current_user["xcoin_balance"]:
            return JSONResponse(status_code=400, content={"detail": "Insufficient balance"})
        
        action = bet.params.get("action", "deal")
        
        if action == "deal":
            return {
                "game_started": True,
                "bet_amount": bet.xcoin_amount,
                "message": "Game started! Use action 'hit' or 'stand'"
            }
        elif action == "hit":
            player_score = bet.params.get("player_score", 0)
            new_card = random.randint(1, 11)
            new_score = player_score + new_card
            
            if new_score > 21:
                new_balance = current_user["xcoin_balance"] - bet.xcoin_amount
                supabase.table("users").update({
                    "xcoin_balance": new_balance,
                    "total_bets": current_user.get("total_bets", 0) + 1,
                    "total_wagered": current_user.get("total_wagered", 0) + bet.xcoin_amount
                }).eq("id", current_user["id"]).execute()
                
                return {
                    "outcome": "lose",
                    "win_amount": 0,
                    "new_balance": round(new_balance, 2),
                    "bust": True,
                    "new_score": new_score
                }
            
            return {
                "outcome": "continue",
                "new_score": new_score,
                "new_card": new_card
            }
        elif action == "stand":
            dealer_score = bet.params.get("dealer_score", 15)
            player_score = bet.params.get("player_score", 0)
            
            if dealer_score < 17:
                dealer_score += random.randint(1, 11)
            
            win = player_score > dealer_score or dealer_score > 21
            win_amount = bet.xcoin_amount * 2 if win else 0
            new_balance = current_user["xcoin_balance"] - bet.xcoin_amount + win_amount
            
            supabase.table("users").update({
                "xcoin_balance": new_balance,
                "total_bets": current_user.get("total_bets", 0) + 1,
                "total_wagered": current_user.get("total_wagered", 0) + bet.xcoin_amount,
                "total_won": current_user.get("total_won", 0) + win_amount
            }).eq("id", current_user["id"]).execute()
            
            return {
                "outcome": "win" if win else "lose",
                "win_amount": round(win_amount, 2),
                "new_balance": round(new_balance, 2),
                "dealer_score": dealer_score
            }
        
    except Exception as e:
        logger.error(f"Blackjack error: {e}")
        return JSONResponse(status_code=500, content={"detail": "Game error"})

# ============================================
# PAYMENT ROUTES
# ============================================

@app.post("/api/payments/create-stripe-session")
async def create_stripe_session(payment: StripePayment, current_user: dict = Depends(get_current_user)):
    """Create Stripe checkout session"""
    if not STRIPE_SECRET_KEY:
        return JSONResponse(
            status_code=400,
            content={"detail": "Stripe not configured. Please contact support."}
        )
    
    try:
        usd_amount = payment.amount_xcoin * 0.01  # 100 XCoin = $1 USD
        
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'usd',
                    'product_data': {
                        'name': f'{int(payment.amount_xcoin)} XCoin',
                        'description': f'Premium casino credits for XBET - Get {int(payment.amount_xcoin)} XCoin to play!',
                    },
                    'unit_amount': int(usd_amount * 100),
                },
                'quantity': 1,
            }],
            mode='payment',
            success_url="https://xbet-inky.vercel.app/success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url="https://xbet-inky.vercel.app/cancel",
            metadata={
                'user_id': current_user['id'],
                'xcoin_amount': str(payment.amount_xcoin),
                'username': current_user['username']
            }
        )
        
        return {"session_id": session.id, "url": session.url}
    except Exception as e:
        logger.error(f"Stripe error: {e}")
        return JSONResponse(
            status_code=500,
            content={"detail": f"Payment creation failed: {str(e)}"}
        )

@app.post("/api/payments/stripe-webhook")
async def stripe_webhook(request: Request):
    """Handle Stripe webhook for successful payments"""
    if not STRIPE_WEBHOOK_SECRET:
        return JSONResponse(status_code=400, content={"detail": "Webhook not configured"})
    
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
        
        if event.type == 'checkout.session.completed':
            session = event.data.object
            user_id = session.metadata.get('user_id')
            xcoin_amount = float(session.metadata.get('xcoin_amount', 0))
            
            if user_id and xcoin_amount > 0:
                user = supabase.table("users").select("*").eq("id", user_id).execute()
                if user.data:
                    new_balance = user.data[0]["xcoin_balance"] + xcoin_amount
                    supabase.table("users").update({
                        "xcoin_balance": new_balance,
                        "total_purchases": user.data[0].get("total_purchases", 0) + 1,
                        "total_deposits": user.data[0].get("total_deposits", 0) + xcoin_amount
                    }).eq("id", user_id).execute()
                    
                    logger.info(f"Added {xcoin_amount} XCoin to user {user_id}")
        
        return {"status": "success"}
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return JSONResponse(status_code=400, content={"detail": str(e)})

@app.post("/api/payments/roblox-purchase")
async def roblox_purchase(purchase: RobloxPurchase, current_user: dict = Depends(get_current_user)):
    """Process Roblox purchase"""
    try:
        # Verify the Roblox purchase
        verified = await verify_roblox_purchase(
            current_user["id"],
            purchase.product_id,
            purchase.amount_robux
        )
        
        if not verified and ROBLOX_API_KEY:
            return JSONResponse(
                status_code=400,
                content={"detail": "Roblox purchase verification failed. Please try again."}
            )
        
        xcoin_amount = purchase.amount_robux
        new_balance = current_user["xcoin_balance"] + xcoin_amount
        
        supabase.table("users").update({
            "xcoin_balance": new_balance,
            "total_purchases": current_user.get("total_purchases", 0) + 1,
            "total_deposits": current_user.get("total_deposits", 0) + xcoin_amount
        }).eq("id", current_user["id"]).execute()
        
        # Update VIP level based on total deposits
        total_deposits = current_user.get("total_deposits", 0) + xcoin_amount
        vip_level = min(10, int(total_deposits / 5000) + 1)
        supabase.table("users").update({"vip_level": vip_level}).eq("id", current_user["id"]).execute()
        
        return {
            "success": True,
            "xcoin_added": xcoin_amount,
            "new_balance": round(new_balance, 2),
            "vip_level": vip_level,
            "message": f"Added {xcoin_amount} XCoin to your account! You are now VIP Level {vip_level}."
        }
    except Exception as e:
        logger.error(f"Roblox purchase error: {e}")
        return JSONResponse(
            status_code=500,
            content={"detail": "Purchase processing failed. Please contact support."}
        )

@app.get("/api/payments/roblox-products")
async def get_roblox_products():
    """Get available Roblox products"""
    return {
        "products": [
            {"robux": 100, "xcoin": 100, "price_usd": 1.00, "product_id": ROBLOX_PASS_IDS.get(100, "xbet_100")},
            {"robux": 500, "xcoin": 500, "price_usd": 5.00, "product_id": ROBLOX_PASS_IDS.get(500, "xbet_500")},
            {"robux": 1000, "xcoin": 1000, "price_usd": 10.00, "product_id": ROBLOX_PASS_IDS.get(1000, "xbet_1000")},
            {"robux": 5000, "xcoin": 5000, "price_usd": 50.00, "product_id": ROBLOX_PASS_IDS.get(5000, "xbet_5000")},
            {"robux": 10000, "xcoin": 10000, "price_usd": 100.00, "product_id": ROBLOX_PASS_IDS.get(10000, "xbet_10000")},
            {"robux": 50000, "xcoin": 50000, "price_usd": 500.00, "product_id": ROBLOX_PASS_IDS.get(50000, "xbet_50000"), "vip_bonus": True}
        ]
    }

# ============================================
# EMAIL ROUTES
# ============================================

@app.post("/api/email/send")
async def send_email_endpoint(email: EmailRequest, current_user: dict = Depends(get_current_user)):
    """Send email (admin only)"""
    if current_user["role"] != "admin":
        return JSONResponse(status_code=403, content={"detail": "Admin access required"})
    
    success = await send_email(email.to_email, email.subject, email.content)
    if success:
        return {"message": "Email sent successfully"}
    else:
        return JSONResponse(status_code=500, content={"detail": "Failed to send email"})

# ============================================
# STATS ROUTES
# ============================================

@app.get("/api/leaderboard")
async def get_leaderboard():
    users = supabase.table("users").select("username,xcoin_balance,role,vip_level").order("xcoin_balance", desc=True).limit(10).execute()
    return {"players": users.data}

@app.get("/api/stats")
async def get_stats():
    try:
        total_users = supabase.table("users").select("count", count="exact").execute()
        total_bets = supabase.table("users").select("total_bets").execute()
        total_wagered = supabase.table("users").select("total_wagered").execute()
        
        sum_bets = sum(u.get("total_bets", 0) for u in total_bets.data) if total_bets.data else 0
        sum_wagered = sum(u.get("total_wagered", 0) for u in total_wagered.data) if total_wagered.data else 0
        
        return {
            "total_bets": sum_bets,
            "total_wagered": round(sum_wagered, 2),
            "online_players": 0,
            "total_users": total_users.count if total_users.count else 0
        }
    except:
        return {"total_bets": 0, "total_wagered": 0, "online_players": 0, "total_users": 0}

@app.get("/api/online-players")
async def get_online_players():
    return {"count": 0}

# ============================================
# REWARD ROUTES
# ============================================

@app.post("/api/rewards/daily")
async def claim_daily_bonus(current_user: dict = Depends(get_current_user)):
    last_claim = current_user.get("last_daily_claim")
    
    if last_claim:
        last_date = datetime.fromisoformat(last_claim)
        if (datetime.utcnow() - last_date).days < 1:
            return JSONResponse(status_code=400, content={"detail": "Already claimed today"})
    
    bonus = 100.0
    new_balance = current_user["xcoin_balance"] + bonus
    
    supabase.table("users").update({
        "xcoin_balance": new_balance,
        "last_daily_claim": datetime.utcnow().isoformat()
    }).eq("id", current_user["id"]).execute()
    
    return {"bonus": bonus, "new_balance": round(new_balance, 2)}

# ============================================
# ADMIN ROUTES
# ============================================

@app.get("/api/admin/users")
async def get_users(current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "admin":
        return JSONResponse(status_code=403, content={"detail": "Admin access required"})
    
    users = supabase.table("users").select("*").execute()
    return {"users": users.data}

@app.put("/api/admin/users/{user_id}/ban")
async def ban_user(user_id: str, ban_data: Dict, current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "admin":
        return JSONResponse(status_code=403, content={"detail": "Admin access required"})
    
    supabase.table("users").update({"banned": ban_data.get("banned", True)}).eq("id", user_id).execute()
    return {"message": "User updated"}

@app.put("/api/admin/users/{user_id}/balance")
async def update_balance(user_id: str, balance_data: Dict, current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "admin":
        return JSONResponse(status_code=403, content={"detail": "Admin access required"})
    
    supabase.table("users").update({"xcoin_balance": balance_data.get("balance", 0)}).eq("id", user_id).execute()
    return {"message": "Balance updated"}

# ============================================
# WEBSOCKET
# ============================================

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
    
    async def connect(self, websocket: WebSocket, user_id: str):
        await websocket.accept()
        self.active_connections[user_id] = websocket
        logger.info(f"User {user_id} connected")
    
    def disconnect(self, user_id: str):
        if user_id in self.active_connections:
            del self.active_connections[user_id]
            logger.info(f"User {user_id} disconnected")
    
    async def broadcast(self, message: str):
        for connection in self.active_connections.values():
            try:
                await connection.send_text(message)
            except:
                pass

manager = ConnectionManager()

@app.websocket("/ws/{token}")
async def websocket_endpoint(websocket: WebSocket, token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        
        if not user_id:
            await websocket.close(code=1008)
            return
        
        await manager.connect(websocket, user_id)
        
        # Send welcome message
        await websocket.send_text(json.dumps({
            "type": "connected",
            "message": "Connected to XBET WebSocket",
            "timestamp": datetime.utcnow().isoformat()
        }))
        
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
            elif message.get("type") == "ping":
                await websocket.send_text(json.dumps({
                    "type": "pong",
                    "timestamp": datetime.utcnow().isoformat()
                }))
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        manager.disconnect(user_id)

# ============================================
# INITIALIZATION
# ============================================

@app.on_event("startup")
async def startup_event():
    logger.info("Starting XBET Casino backend...")
    if supabase:
        init_database()
    
    # Log configuration status
    logger.info(f"Stripe: {'✅' if STRIPE_SECRET_KEY else '❌'}")
    logger.info(f"Roblox: {'✅' if ROBLOX_API_KEY else '❌'}")
    logger.info(f"SendGrid: {'✅' if SENDGRID_API_KEY else '❌'}")
    logger.info(f"Supabase: {'✅' if supabase else '❌'}")
    
    logger.info("XBET Casino backend started successfully")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 5000))
    uvicorn.run(app, host="0.0.0.0", port=port)
