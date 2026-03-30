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
from enum import Enum  # <-- THIS WAS MISSING
import uuid
import logging
import stripe
import requests
from functools import lru_cache
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
app = FastAPI(title="XBet Casino - Premium Edition", version="3.0.0")

# CORS - Allow all origins for development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://xbet-inky.vercel.app"],
    allow_credentials=True,
    allow_methods=["https://xbet-inky.vercel.app"],
    allow_headers=["https://xbet-inky.vercel.app"],
)

# Security
security = HTTPBearer()
SECRET_KEY = os.getenv("SECRET_KEY", "xbet_super_secret_key_2024_master_ultra_secure")
ALGORITHM = "HS256"
JWT_EXPIRY = int(os.getenv("JWT_EXPIRY", 86400))

# Stripe Configuration
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

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

# Database Configuration
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

if not SUPABASE_URL or not SUPABASE_KEY:
    logger.error("Supabase credentials not configured!")
    print("ERROR: SUPABASE_URL and SUPABASE_KEY must be set in .env file")
    print("Please create a .env file with your Supabase credentials")

try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    logger.info("Supabase connected successfully")
except Exception as e:
    logger.error(f"Supabase connection failed: {e}")
    print(f"ERROR: Failed to connect to Supabase: {e}")

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
        if not user.data:
            raise HTTPException(status_code=401, detail="User not found")
        
        if user.data[0].get("banned", False):
            raise HTTPException(status_code=403, detail="Account banned")
        
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
# DATABASE INITIALIZATION
# ============================================

def init_database():
    """Initialize database tables and default data"""
    try:
        # Check if users table exists by trying to query it
        test_query = supabase.table("users").select("*").limit(1).execute()
        logger.info("Users table exists")
        
        # Check if admin exists
        admin_check = supabase.table("users").select("*").eq("email", "xotiicglizzy@gmail.com").execute()
        
        if not admin_check.data:
            admin_id = str(uuid.uuid4())
            admin_data = {
                "id": admin_id,
                "username": "xotiic",
                "email": "xotiicglizzy@gmail.com",
                "password_hash": hash_password("40671Mps19*"),
                "xcoin_balance": 10000000.0,
                "role": "admin",
                "vip_level": 10,
                "referral_code": "XOTIICVIP",
                "total_bets": 0,
                "total_wagered": 0,
                "total_won": 0,
                "total_purchases": 0,
                "total_deposits": 10000000.0,
                "created_at": datetime.utcnow().isoformat(),
                "last_login": datetime.utcnow().isoformat(),
                "banned": False
            }
            
            supabase.table("users").insert(admin_data).execute()
            logger.info("Admin user created successfully: xotiic")
        else:
            logger.info("Admin user already exists")
            
    except Exception as e:
        logger.error(f"Database initialization error: {e}")
        print(f"ERROR: Database initialization failed: {e}")
        print("Please make sure your Supabase tables are created with the SQL schema provided")

# ============================================
# AUTH ROUTES
# ============================================

@app.get("/")
async def root():
    """Root endpoint"""
    return {"message": "XBET Casino API is running", "status": "online", "version": "3.0.0"}

@app.get("/api/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "version": "3.0.0",
        "supabase": "connected" if SUPABASE_URL and SUPABASE_KEY else "not configured"
    }

@app.post("/api/auth/register")
async def register(user: UserRegister):
    """Register new user"""
    try:
        logger.info(f"Registration attempt for username: {user.username}")
        
        # Check if email already exists
        if user.email:
            existing = supabase.table("users").select("*").eq("email", user.email).execute()
            if existing.data:
                return JSONResponse(
                    status_code=400,
                    content={"detail": "Email already registered"}
                )
        
        # Check if username already exists
        existing_username = supabase.table("users").select("*").eq("username", user.username).execute()
        if existing_username.data:
            return JSONResponse(
                status_code=400,
                content={"detail": "Username already taken"}
            )
        
        # Check if Roblox ID already linked
        if user.roblox_id:
            existing_roblox = supabase.table("users").select("*").eq("roblox_id", user.roblox_id).execute()
            if existing_roblox.data:
                return JSONResponse(
                    status_code=400,
                    content={"detail": "Roblox ID already linked"}
                )
        
        # Generate referral code and user ID
        referral_code = secrets.token_hex(4).upper()
        user_id = str(uuid.uuid4())
        
        # Hash password
        hashed_password = hash_password(user.password)
        
        user_data = {
            "id": user_id,
            "username": user.username,
            "email": user.email,
            "roblox_id": user.roblox_id or "",
            "password_hash": hashed_password,
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
            "last_login": datetime.utcnow().isoformat(),
            "banned": False
        }
        
        # Insert user into database
        result = supabase.table("users").insert(user_data).execute()
        
        if not result.data:
            return JSONResponse(
                status_code=500,
                content={"detail": "Failed to create user"}
            )
        
        logger.info(f"User created successfully: {user.username} (ID: {user_id})")
        
        # Handle referral if provided
        if user.referral_code:
            try:
                referrer = supabase.table("users").select("*").eq("referral_code", user.referral_code).execute()
                if referrer.data:
                    bonus = 50
                    new_balance = referrer.data[0]["xcoin_balance"] + bonus
                    supabase.table("users").update({"xcoin_balance": new_balance}).eq("id", referrer.data[0]["id"]).execute()
                    logger.info(f"Referral bonus applied: {user.referral_code}")
            except Exception as e:
                logger.error(f"Referral processing error: {e}")
        
        # Create JWT token
        token = create_access_token({"sub": user_id, "role": "user"})
        
        # Return success response
        return {
            "token": token,
            "user": {
                "id": user_id,
                "username": user.username,
                "email": user.email,
                "roblox_id": user.roblox_id or "",
                "xcoin_balance": 100.0,
                "role": "user",
                "vip_level": 1,
                "referral_code": referral_code
            }
        }
        
    except Exception as e:
        logger.error(f"Registration error: {str(e)}")
        return JSONResponse(
            status_code=500,
            content={"detail": f"Registration failed: {str(e)}"}
        )

@app.post("/api/auth/login")
async def login(user: UserLogin):
    """Login user"""
    try:
        logger.info(f"Login attempt")
        
        # Build query based on login method
        result = None
        if user.email:
            result = supabase.table("users").select("*").eq("email", user.email).execute()
        elif user.username:
            result = supabase.table("users").select("*").eq("username", user.username).execute()
        elif user.roblox_id:
            result = supabase.table("users").select("*").eq("roblox_id", user.roblox_id).execute()
        else:
            return JSONResponse(
                status_code=400,
                content={"detail": "Email, username, or Roblox ID required"}
            )
        
        if not result.data:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid credentials"}
            )
        
        user_data = result.data[0]
        
        # Verify password
        if not verify_password(user.password, user_data["password_hash"]):
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid credentials"}
            )
        
        # Check if banned
        if user_data.get("banned", False):
            return JSONResponse(
                status_code=403,
                content={"detail": "Account banned"}
            )
        
        # Update last login
        supabase.table("users").update({
            "last_login": datetime.utcnow().isoformat()
        }).eq("id", user_data["id"]).execute()
        
        # Create JWT token
        token = create_access_token({"sub": user_data["id"], "role": user_data["role"]})
        
        logger.info(f"User logged in successfully: {user_data['username']}")
        
        # Return user data
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
        return JSONResponse(
            status_code=500,
            content={"detail": f"Login failed: {str(e)}"}
        )

@app.get("/api/user/balance")
async def get_balance(current_user: dict = Depends(get_current_user)):
    """Get user balance"""
    try:
        return {
            "id": current_user["id"],
            "username": current_user["username"],
            "xcoin_balance": current_user["xcoin_balance"],
            "role": current_user["role"],
            "vip_level": current_user.get("vip_level", 1)
        }
    except Exception as e:
        logger.error(f"Balance error: {str(e)}")
        return JSONResponse(
            status_code=500,
            content={"detail": "Failed to get balance"}
        )

# ============================================
# PAYMENT ROUTES
# ============================================

@app.post("/api/payments/create-stripe-session")
async def create_stripe_session(payment: StripePayment, current_user: dict = Depends(get_current_user)):
    """Create Stripe checkout session"""
    if not STRIPE_SECRET_KEY:
        return JSONResponse(
            status_code=400,
            content={"detail": "Stripe not configured"}
        )
    
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
            success_url=f"https://xbet-inky.vercel.app/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"https://xbet-inky.vercel.app/cancel",
            metadata={
                'user_id': current_user['id'],
                'xcoin_amount': str(payment.amount_xcoin)
            }
        )
        
        return {"session_id": session.id, "url": session.url}
    except Exception as e:
        logger.error(f"Stripe session error: {e}")
        return JSONResponse(
            status_code=500,
            content={"detail": "Payment creation failed"}
        )

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
        return JSONResponse(
            status_code=500,
            content={"detail": "Purchase processing failed"}
        )

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
            return JSONResponse(
                status_code=400,
                content={"detail": "Insufficient balance"}
            )
        
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
    except Exception as e:
        logger.error(f"Slots error: {e}")
        return JSONResponse(
            status_code=500,
            content={"detail": "Game error"}
        )

# ============================================
# GAME: DICE
# ============================================

@app.post("/api/games/dice/play")
async def play_dice(bet: GameBet, current_user: dict = Depends(get_current_user)):
    """Play dice game"""
    try:
        if bet.xcoin_amount > current_user["xcoin_balance"]:
            return JSONResponse(
                status_code=400,
                content={"detail": "Insufficient balance"}
            )
        
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
    except Exception as e:
        logger.error(f"Dice error: {e}")
        return JSONResponse(
            status_code=500,
            content={"detail": "Game error"}
        )

# ============================================
# ADMIN ROUTES
# ============================================

@app.get("/api/admin/users")
async def get_users(current_user: dict = Depends(get_current_user)):
    """Get all users (admin only)"""
    if current_user["role"] != "admin":
        return JSONResponse(
            status_code=403,
            content={"detail": "Admin access required"}
        )
    
    users = supabase.table("users").select("*").execute()
    return {"users": users.data}

@app.put("/api/admin/users/{user_id}/ban")
async def ban_user(user_id: str, ban_data: Dict, current_user: dict = Depends(get_current_user)):
    """Ban or unban user"""
    if current_user["role"] != "admin":
        return JSONResponse(
            status_code=403,
            content={"detail": "Admin access required"}
        )
    
    supabase.table("users").update({"banned": ban_data.get("banned", True)}).eq("id", user_id).execute()
    return {"message": "User updated"}

@app.put("/api/admin/users/{user_id}/balance")
async def update_balance(user_id: str, balance_data: Dict, current_user: dict = Depends(get_current_user)):
    """Update user balance"""
    if current_user["role"] != "admin":
        return JSONResponse(
            status_code=403,
            content={"detail": "Admin access required"}
        )
    
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
    return {"count": 0}

@app.get("/api/stats")
async def get_stats():
    """Get global stats"""
    try:
        total_users = supabase.table("users").select("count", count="exact").execute()
        return {
            "total_bets": 0,
            "total_wagered": 0,
            "online_players": 0,
            "total_users": total_users.count if total_users.count else 0
        }
    except:
        return {
            "total_bets": 0,
            "total_wagered": 0,
            "online_players": 0,
            "total_users": 0
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
            return JSONResponse(
                status_code=400,
                content={"detail": "Already claimed today"}
            )
    
    bonus = 100.0
    new_balance = current_user["xcoin_balance"] + bonus
    
    supabase.table("users").update({
        "xcoin_balance": new_balance,
        "last_daily_claim": datetime.utcnow().isoformat()
    }).eq("id", current_user["id"]).execute()
    
    return {"bonus": bonus, "new_balance": new_balance}

# ============================================
# WEBSOCKET (Basic)
# ============================================

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
    
    async def connect(self, websocket: WebSocket, user_id: str):
        await websocket.accept()
        self.active_connections[user_id] = websocket
    
    def disconnect(self, user_id: str):
        if user_id in self.active_connections:
            del self.active_connections[user_id]
    
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
# INITIALIZATION
# ============================================

@app.on_event("startup")
async def startup_event():
    """Initialize on startup"""
    logger.info("Starting XBET Casino backend...")
    if SUPABASE_URL and SUPABASE_KEY:
        init_database()
    logger.info("XBET Casino backend started successfully")

if __name__ == "__main__":
    import uvicorn
    import sys
    
    port = int(os.getenv("PORT", 5000))
    host = os.getenv("HOST", "0.0.0.0")
    
    print(f"\n{'='*50}")
    print(f"XBET Casino Backend Starting...")
    print(f"Host: {host}")
    print(f"Port: {port}")
    print(f"Supabase URL: {SUPABASE_URL[:50] if SUPABASE_URL else 'NOT SET'}...")
    print(f"{'='*50}\n")
    
    try:
        uvicorn.run(
            app,
            host=host,
            port=port,
            reload=False,
            log_level="info"
        )
    except KeyboardInterrupt:
        print("\nShutting down...")
        sys.exit(0)
    except Exception as e:
        print(f"\nERROR: Failed to start server: {e}")
        sys.exit(1)
