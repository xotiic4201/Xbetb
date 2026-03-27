from fastapi import FastAPI, HTTPException, Depends, WebSocket, WebSocketDisconnect, status, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
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

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.getenv('LOG_FILE', 'logs/xbet.log')),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Initialize FastAPI
app = FastAPI(title="XBet Casino - Premium Edition", version="3.0.0")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv('ALLOWED_ORIGINS', '*').split(','),
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
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

# Roblox Configuration
ROBLOX_API_KEY = os.getenv("ROBLOX_API_KEY", "")
ROBLOX_GROUP_ID = os.getenv("ROBLOX_GROUP_ID", "0")
ROBLOX_PASS_IDS = {
    100: os.getenv("ROBLOX_PASS_100"),
    500: os.getenv("ROBLOX_PASS_500"),
    1000: os.getenv("ROBLOX_PASS_1000"),
    5000: os.getenv("ROBLOX_PASS_5000"),
    10000: os.getenv("ROBLOX_PASS_10000"),
    50000: os.getenv("ROBLOX_PASS_50000")
}

# Database Configuration
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Redis for real-time data
redis_client = redis.Redis(
    host=os.getenv("REDIS_HOST", "localhost"),
    port=int(os.getenv("REDIS_PORT", 6379)),
    password=os.getenv("REDIS_PASSWORD", None),
    db=int(os.getenv("REDIS_DB", 0)),
    decode_responses=True
)

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

# Database initialization with admin user
def init_database():
    """Initialize database tables and default data with admin account"""
    try:
        # Check if admin exists
        admin_check = supabase.table("users").select("*").eq("email", "xotiicglizzy@gmail.com").execute()
        
        if not admin_check.data:
            # Create admin user with provided credentials
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
                "last_login": datetime.utcnow().isoformat()
            }
            
            supabase.table("users").insert(admin_data).execute()
            logger.info("Admin user created successfully: xotiic")
        
        # Create default system settings if not exists
        settings_check = supabase.table("system_settings").select("*").limit(1).execute()
        if not settings_check.data:
            default_settings = [
                {"key": "house_edge", "value": json.dumps({"slots": 0.025, "blackjack": 0.005, "dice": 0.01, "crash": 0.01, "mines": 0.02, "plinko": 0.03})},
                {"key": "min_bet", "value": json.dumps({"slots": 1, "blackjack": 5, "dice": 1, "crash": 1, "mines": 1, "plinko": 1})},
                {"key": "max_bet", "value": json.dumps({"slots": 10000, "blackjack": 5000, "dice": 5000, "crash": 5000, "mines": 5000, "plinko": 5000})},
                {"key": "withdrawal_limits", "value": json.dumps({"daily": 10000, "weekly": 50000, "monthly": 200000})},
                {"key": "referral_bonus", "value": json.dumps({"percentage": 10, "max_bonus": 1000})},
                {"key": "welcome_bonus", "value": json.dumps({"amount": 100, "wagering": 10})},
                {"key": "daily_bonus", "value": json.dumps({"amount": 100, "streak_multiplier": 1.5})},
                {"key": "maintenance", "value": json.dumps({"is_active": False, "message": "Site under maintenance"})}
            ]
            
            for setting in default_settings:
                supabase.table("system_settings").insert(setting).execute()
            logger.info("Default system settings created")
        
        # Create VIP benefits if not exists
        vip_check = supabase.table("vip_benefits").select("*").limit(1).execute()
        if not vip_check.data:
            vip_benefits = [
                {"vip_level": 1, "required_wagered": 0, "cashback_percentage": 0, "rakeback_percentage": 0, "withdrawal_limit": 1000, "daily_bonus": 0},
                {"vip_level": 2, "required_wagered": 1000, "cashback_percentage": 1, "rakeback_percentage": 1, "withdrawal_limit": 2000, "daily_bonus": 10},
                {"vip_level": 3, "required_wagered": 5000, "cashback_percentage": 2, "rakeback_percentage": 2, "withdrawal_limit": 5000, "daily_bonus": 25},
                {"vip_level": 4, "required_wagered": 25000, "cashback_percentage": 3, "rakeback_percentage": 3, "withdrawal_limit": 10000, "daily_bonus": 50},
                {"vip_level": 5, "required_wagered": 100000, "cashback_percentage": 4, "rakeback_percentage": 4, "withdrawal_limit": 25000, "daily_bonus": 100},
                {"vip_level": 6, "required_wagered": 500000, "cashback_percentage": 5, "rakeback_percentage": 5, "withdrawal_limit": 50000, "daily_bonus": 250},
                {"vip_level": 7, "required_wagered": 1000000, "cashback_percentage": 7, "rakeback_percentage": 7, "withdrawal_limit": 100000, "daily_bonus": 500},
                {"vip_level": 8, "required_wagered": 5000000, "cashback_percentage": 10, "rakeback_percentage": 10, "withdrawal_limit": 250000, "daily_bonus": 1000},
                {"vip_level": 9, "required_wagered": 10000000, "cashback_percentage": 12, "rakeback_percentage": 12, "withdrawal_limit": 500000, "daily_bonus": 2500},
                {"vip_level": 10, "required_wagered": 50000000, "cashback_percentage": 15, "rakeback_percentage": 15, "withdrawal_limit": 1000000, "daily_bonus": 5000}
            ]
            
            for vip in vip_benefits:
                supabase.table("vip_benefits").insert(vip).execute()
            logger.info("VIP benefits created")
            
    except Exception as e:
        logger.error(f"Database initialization error: {e}")

def hash_password(password: str) -> str:
    """Hash password with bcrypt"""
    salt = bcrypt.gensalt(rounds=int(os.getenv("BCRYPT_ROUNDS", 12)))
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

# Auth Routes
@app.post("/api/auth/register")
async def register(user: UserRegister):
    """Register new user with optional Roblox ID"""
    try:
        # Check if user exists
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
        
        # Generate referral code
        referral_code = secrets.token_hex(4).upper()
        
        # Create user
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
        
        result = supabase.table("users").insert(user_data).execute()
        
        # Handle referral if provided
        if user.referral_code:
            referrer = supabase.table("users").select("*").eq("referral_code", user.referral_code).execute()
            if referrer.data:
                bonus = float(os.getenv("REFERRAL_BONUS_PERCENTAGE", 10)) / 100 * 100
                new_balance = referrer.data[0]["xcoin_balance"] + bonus
                supabase.table("users").update({"xcoin_balance": new_balance}).eq("id", referrer.data[0]["id"]).execute()
                
                supabase.table("referrals").insert({
                    "referrer_id": referrer.data[0]["id"],
                    "referred_id": user_id,
                    "bonus_amount": bonus,
                    "created_at": datetime.utcnow().isoformat()
                }).execute()
        
        token = create_access_token({"sub": user_id, "role": "user"})
        
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
    """Login user with email/username or Roblox ID"""
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

# Payment Routes
@app.post("/api/payments/create-stripe-session")
async def create_stripe_session(payment: StripePayment, current_user: dict = Depends(get_current_user)):
    """Create Stripe checkout session"""
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
    """Process Roblox purchase (like Bloxflip)"""
    try:
        # Convert Robux to XCoin (100 Robux = 100 XCoin)
        xcoin_amount = purchase.amount_robux
        
        # Add XCoin to user balance
        new_balance = current_user["xcoin_balance"] + xcoin_amount
        
        supabase.table("users").update({
            "xcoin_balance": new_balance,
            "total_purchases": current_user.get("total_purchases", 0) + 1,
            "total_deposits": current_user.get("total_deposits", 0) + xcoin_amount
        }).eq("id", current_user["id"]).execute()
        
        # Record purchase
        supabase.table("purchases").insert({
            "id": str(uuid.uuid4()),
            "user_id": current_user["id"],
            "amount": xcoin_amount,
            "currency": "xcoin",
            "payment_method": "roblox",
            "roblox_amount": purchase.amount_robux,
            "roblox_product_id": purchase.product_id,
            "status": "completed",
            "completed_at": datetime.utcnow().isoformat(),
            "created_at": datetime.utcnow().isoformat()
        }).execute()
        
        return {
            "success": True,
            "xcoin_added": xcoin_amount,
            "new_balance": new_balance,
            "message": f"Added {xcoin_amount} XCoin to your account!"
        }
    except HTTPException:
        raise
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

# Game Routes - Slots (Enhanced)
class SlotsGame:
    SYMBOLS = ["cherry", "lemon", "orange", "plum", "bell", "xbet", "diamond", "crown"]
    PAYOUTS = {
        "crown": {3: 200, 4: 1000, 5: 5000},
        "diamond": {3: 150, 4: 750, 5: 2500},
        "xbet": {3: 100, 4: 500, 5: 1000},
        "bell": {3: 50, 4: 200, 5: 500},
        "plum": {3: 25, 4: 100, 5: 250},
        "orange": {3: 15, 4: 50, 5: 150},
        "lemon": {3: 10, 4: 30, 5: 100},
        "cherry": {3: 5, 4: 20, 5: 50}
    }
    
    @staticmethod
    def spin(server_seed: str, client_seed: str, nonce: int) -> Dict:
        hash_result = provably_fair_hash(server_seed, client_seed, nonce)
        
        grid = []
        for i in range(3):
            row = []
            for j in range(5):
                pos = int(hash_result[(i * 5 + j) * 2:(i * 5 + j) * 2 + 2], 16) % 8
                row.append(SlotsGame.SYMBOLS[pos])
            grid.append(row)
        
        winning_lines = []
        total_payout = 0
        
        for col in range(5):
            symbol = grid[1][col]
            count = 1
            if grid[0][col] == symbol:
                count += 1
            if grid[2][col] == symbol:
                count += 1
            
            if count >= 3 and symbol in SlotsGame.PAYOUTS:
                payout = SlotsGame.PAYOUTS[symbol].get(count, 0)
                if payout > 0:
                    winning_lines.append({
                        "reel": col,
                        "symbol": symbol,
                        "count": count,
                        "payout": payout
                    })
                    total_payout += payout
        
        multiplier = total_payout / 100 if total_payout > 0 else 0
        
        return {
            "reel_grid": grid,
            "winning_lines": winning_lines,
            "total_payout": total_payout,
            "multiplier": multiplier
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
        
        result = SlotsGame.spin(server_seed, client_seed, nonce)
        
        win_amount = bet.xcoin_amount * (result["total_payout"] / 100)
        
        new_balance = current_user["xcoin_balance"] - bet.xcoin_amount + win_amount
        
        supabase.table("users").update({
            "xcoin_balance": new_balance,
            "total_bets": current_user.get("total_bets", 0) + 1,
            "total_wagered": current_user.get("total_wagered", 0) + bet.xcoin_amount,
            "total_won": current_user.get("total_won", 0) + win_amount
        }).eq("id", current_user["id"]).execute()
        
        # Record game history
        supabase.table("game_history").insert({
            "id": str(uuid.uuid4()),
            "user_id": current_user["id"],
            "game_type": "slots",
            "bet_amount": bet.xcoin_amount,
            "win_amount": win_amount,
            "multiplier": result["multiplier"],
            "outcome": "win" if win_amount > 0 else "lose",
            "balance_before": current_user["xcoin_balance"],
            "balance_after": new_balance,
            "game_data": result,
            "server_seed": server_seed,
            "client_seed": client_seed,
            "nonce": nonce,
            "created_at": datetime.utcnow().isoformat()
        }).execute()
        
        redis_client.incr("total_bets")
        redis_client.incrbyfloat("total_wagered", bet.xcoin_amount)
        
        return {
            "result": result,
            "outcome": "win" if win_amount > 0 else "lose",
            "win_amount": win_amount,
            "new_balance": new_balance,
            "multiplier": result["multiplier"],
            "server_seed": server_seed,
            "client_seed": client_seed,
            "nonce": nonce
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Slots error: {e}")
        raise HTTPException(status_code=500, detail="Game error")

# Dice Game
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
        
        # Record game history
        supabase.table("game_history").insert({
            "id": str(uuid.uuid4()),
            "user_id": current_user["id"],
            "game_type": "dice",
            "bet_amount": bet.xcoin_amount,
            "win_amount": win_amount,
            "multiplier": multiplier if win else 0,
            "outcome": "win" if win else "lose",
            "balance_before": current_user["xcoin_balance"],
            "balance_after": new_balance,
            "game_data": {"roll": roll, "target": target, "condition": condition},
            "server_seed": server_seed,
            "client_seed": client_seed,
            "nonce": nonce,
            "created_at": datetime.utcnow().isoformat()
        }).execute()
        
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

# Admin Routes
@app.get("/api/admin/users")
async def get_users(current_user: dict = Depends(get_current_user)):
    """Get all users (admin only)"""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    
    users = supabase.table("users").select("id,username,email,roblox_id,xcoin_balance,role,vip_level,banned,created_at,total_purchases,total_deposits").execute()
    return {"users": users.data}

@app.put("/api/admin/users/{user_id}/ban")
async def ban_user(user_id: str, ban_data: Dict, current_user: dict = Depends(get_current_user)):
    """Ban or unban user (admin only)"""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    
    supabase.table("users").update({"banned": ban_data.get("banned", True)}).eq("id", user_id).execute()
    return {"message": "User updated"}

@app.put("/api/admin/users/{user_id}/balance")
async def update_balance(user_id: str, balance_data: Dict, current_user: dict = Depends(get_current_user)):
    """Update user balance (admin only)"""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    
    supabase.table("users").update({"xcoin_balance": balance_data.get("balance", 0)}).eq("id", user_id).execute()
    return {"message": "Balance updated"}

@app.put("/api/admin/users/{user_id}/role")
async def update_role(user_id: str, role_data: Dict, current_user: dict = Depends(get_current_user)):
    """Update user role (admin only)"""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    
    supabase.table("users").update({"role": role_data.get("role", "user")}).eq("id", user_id).execute()
    return {"message": "Role updated"}

# Stats Routes
@app.get("/api/leaderboard")
async def get_leaderboard():
    """Get top players leaderboard"""
    users = supabase.table("users").select("username,xcoin_balance,role,vip_level").order("xcoin_balance", desc=True).limit(10).execute()
    return {"players": users.data}

@app.get("/api/online-players")
async def get_online_players():
    """Get online players count"""
    online_count = redis_client.scard("online_users")
    return {"count": online_count}

@app.get("/api/stats")
async def get_stats():
    """Get global stats"""
    total_bets = redis_client.get("total_bets") or 0
    total_wagered = redis_client.get("total_wagered") or 0
    
    # Get total users
    total_users = supabase.table("users").select("count", count="exact").execute()
    
    return {
        "total_bets": int(total_bets),
        "total_wagered": float(total_wagered),
        "online_players": redis_client.scard("online_users"),
        "total_users": total_users.count if total_users.count else 0
    }

# Reward Routes
@app.post("/api/rewards/daily")
async def claim_daily_bonus(current_user: dict = Depends(get_current_user)):
    """Claim daily bonus"""
    last_claim = current_user.get("last_daily_claim")
    
    if last_claim:
        last_claim_date = datetime.fromisoformat(last_claim)
        if (datetime.utcnow() - last_claim_date).days < 1:
            raise HTTPException(status_code=400, detail="Already claimed today")
    
    bonus = float(os.getenv("DAILY_BONUS_AMOUNT", 100))
    new_balance = current_user["xcoin_balance"] + bonus
    
    supabase.table("users").update({
        "xcoin_balance": new_balance,
        "last_daily_claim": datetime.utcnow().isoformat()
    }).eq("id", current_user["id"]).execute()
    
    # Record transaction
    supabase.table("transactions").insert({
        "id": str(uuid.uuid4()),
        "user_id": current_user["id"],
        "type": "bonus",
        "amount": bonus,
        "balance_before": current_user["xcoin_balance"],
        "balance_after": new_balance,
        "metadata": {"bonus_type": "daily"},
        "created_at": datetime.utcnow().isoformat()
    }).execute()
    
    return {
        "bonus": bonus,
        "new_balance": new_balance,
        "message": "Daily bonus claimed!"
    }

# WebSocket Manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
    
    async def connect(self, websocket: WebSocket, user_id: str):
        await websocket.accept()
        self.active_connections[user_id] = websocket
        redis_client.sadd("online_users", user_id)
    
    def disconnect(self, user_id: str):
        if user_id in self.active_connections:
            del self.active_connections[user_id]
        redis_client.srem("online_users", user_id)
    
    async def send_personal_message(self, message: str, user_id: str):
        if user_id in self.active_connections:
            await self.active_connections[user_id].send_text(message)
    
    async def broadcast(self, message: str):
        for connection in self.active_connections.values():
            await connection.send_text(message)

manager = ConnectionManager()

@app.websocket("/ws/{token}")
async def websocket_endpoint(websocket: WebSocket, token: str):
    """WebSocket endpoint for real-time features"""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        
        if not user_id:
            await websocket.close(code=1008)
            return
        
        await manager.connect(websocket, user_id)
        
        await manager.send_personal_message(json.dumps({
            "type": "connected",
            "message": "Connected to XBet Casino"
        }), user_id)
        
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

# Initialize database on startup
@app.on_event("startup")
async def startup_event():
    """Initialize database and connections on startup"""
    try:
        # Test Redis connection
        redis_client.ping()
        logger.info("Redis connected successfully")
    except Exception as e:
        logger.warning(f"Redis connection failed: {e}")
    
    # Initialize database
    init_database()
    logger.info("Database initialization completed")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", 5000)),
        reload=os.getenv("ENVIRONMENT", "production") == "development"
    )
