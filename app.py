import os
import secrets
import hashlib
import asyncio
import json
import stripe
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Depends, WebSocket, WebSocketDisconnect, Request, BackgroundTasks, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field, validator
from supabase import create_client, Client
import jwt
import bcrypt

# ==================== Configuration ====================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_hex(32))
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))
HOUSE_EDGE = float(os.getenv("HOUSE_EDGE", "0.01"))
FRONTEND_URL = os.getenv("FRONTEND_URL")
PORT = int(os.getenv("PORT", "5000"))

# Admin credentials
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")

# Stripe Configuration
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
stripe.api_key = STRIPE_SECRET_KEY

# XCoin to USD conversion
XCOIN_TO_USD = 0.01
MIN_DEPOSIT_XCOIN = 100
MIN_WITHDRAWAL_XCOIN = 5000

# Initialize Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# ==================== Pydantic Models ====================
class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=6)
    username: Optional[str] = None
    referral_code: Optional[str] = None

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class DepositRequest(BaseModel):
    xcoin_amount: float = Field(..., ge=MIN_DEPOSIT_XCOIN)

class WithdrawRequest(BaseModel):
    xcoin_amount: float = Field(..., ge=MIN_WITHDRAWAL_XCOIN)
    address: str = Field(..., min_length=10, max_length=200)

class BetRequest(BaseModel):
    game: str
    xcoin_amount: float = Field(..., gt=0, le=10000)
    params: Optional[Dict] = {}

class BanUserRequest(BaseModel):
    banned: bool

# ==================== Security Functions ====================
def hash_password(password: str) -> str:
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')

def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))

def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def verify_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

security = HTTPBearer()

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    payload = verify_token(token)
    user_id = payload.get("sub")
    
    response = supabase.table("profiles").select("*").eq("id", user_id).execute()
    if not response.data:
        raise HTTPException(status_code=401, detail="User not found")
    
    user = response.data[0]
    if user.get("banned"):
        raise HTTPException(status_code=403, detail="Account banned")
    
    # Add role from token for admin check
    user["token_role"] = payload.get("role", "user")
    return user

async def get_admin_user(user: dict = Depends(get_current_user)):
    # Check both database role and token role
    if user.get("role") != "admin" and user.get("token_role") != "admin":
        # Also check if this is the specific admin email
        if user.get("email") != ADMIN_EMAIL:
            raise HTTPException(status_code=403, detail="Admin access required")
    return user

# ==================== Game Logic ====================
def get_random_number(server_seed: str, client_seed: str, nonce: int) -> float:
    combined = f"{server_seed}:{client_seed}:{nonce}"
    hash_value = hashlib.sha256(combined.encode()).hexdigest()
    return int(hash_value[:8], 16) / 0xffffffff

def play_slots(server_seed: str, client_seed: str, nonce: int, bet_amount: float):
    symbols = ["cherry", "lemon", "orange", "plum", "bell", "xbet"]
    payouts = [5, 10, 15, 20, 50, 200]
    
    # Generate 15 symbols for 5x3 grid
    reels = []
    for i in range(15):
        r = get_random_number(server_seed, client_seed, nonce + i)
        idx = int(r * len(symbols))
        reels.append(symbols[idx])
    
    # Create 5x3 grid
    grid = [reels[i:i+5] for i in range(0, 15, 5)]
    
    # Check for wins (horizontal lines)
    win_amount = 0
    winning_lines = []
    
    # Check each row
    for row in range(3):
        row_symbols = grid[row]
        if all(s == row_symbols[0] for s in row_symbols):
            idx = symbols.index(row_symbols[0])
            win = bet_amount * payouts[idx]
            win_amount += win
            winning_lines.append({"row": row, "symbol": row_symbols[0], "win": win})
    
    # Check for XBET symbol scatter
    xbet_count = sum(1 for reel in reels if reel == "xbet")
    if xbet_count >= 3:
        win_amount += bet_amount * 50 * xbet_count
        winning_lines.append({"type": "scatter", "count": xbet_count, "win": bet_amount * 50 * xbet_count})
    
    return {
        "reels_data": reels,
        "reel_grid": grid,
        "winning_lines": winning_lines,
        "win_amount": win_amount,
        "is_win": win_amount > 0,
        "multiplier": win_amount / bet_amount if win_amount > 0 else 0
    }

def play_dice(server_seed: str, client_seed: str, nonce: int, bet_amount: float, target: int, condition: str):
    roll = get_random_number(server_seed, client_seed, nonce) * 100
    
    if condition == "under":
        is_win = roll < target
        multiplier = (100 / target) * (1 - HOUSE_EDGE)
    else:
        is_win = roll > target
        multiplier = (100 / (100 - target)) * (1 - HOUSE_EDGE)
    
    win_amount = bet_amount * multiplier if is_win else 0
    return {
        "roll": round(roll, 2),
        "is_win": is_win,
        "win_amount": win_amount,
        "multiplier": multiplier if is_win else 0
    }

# ==================== WebSocket Manager ====================
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.users: Dict[str, dict] = {}
        self.crash_state = {
            "active": False,
            "multiplier": 1.0,
            "crash_point": 0,
            "players": {},
            "round_id": 0
        }
        self.crash_task = None
    
    async def connect(self, websocket: WebSocket, user_id: str, user_data: dict):
        await websocket.accept()
        self.active_connections[user_id] = websocket
        self.users[user_id] = user_data
    
    def disconnect(self, user_id: str):
        if user_id in self.active_connections:
            del self.active_connections[user_id]
        if user_id in self.users:
            del self.users[user_id]
    
    async def send_message(self, user_id: str, message: dict):
        if user_id in self.active_connections:
            try:
                await self.active_connections[user_id].send_json(message)
            except:
                pass
    
    async def broadcast(self, message: dict):
        for user_id, ws in self.active_connections.items():
            try:
                await ws.send_json(message)
            except:
                pass
    
    async def broadcast_chat(self, username: str, message: str):
        await self.broadcast({
            "type": "chat",
            "username": username,
            "message": message,
            "timestamp": datetime.utcnow().isoformat()
        })
    
    async def start_crash_game(self):
        if self.crash_state["active"]:
            return
        
        self.crash_state.update({
            "active": True,
            "multiplier": 1.0,
            "players": {},
            "round_id": self.crash_state["round_id"] + 1
        })
        
        # Random crash point between 1.2x and 10x
        self.crash_state["crash_point"] = 1.2 + (random.random() * 8.8)
        
        await self.broadcast({
            "type": "crash_start",
            "round_id": self.crash_state["round_id"],
            "crash_point": self.crash_state["crash_point"]
        })
        
        async def run_crash():
            while self.crash_state["active"] and self.crash_state["multiplier"] < self.crash_state["crash_point"]:
                await asyncio.sleep(0.1)
                self.crash_state["multiplier"] *= 1.03
                await self.broadcast({
                    "type": "crash_multiplier",
                    "multiplier": round(self.crash_state["multiplier"], 2),
                    "round_id": self.crash_state["round_id"]
                })
            
            self.crash_state["active"] = False
            await self.broadcast({
                "type": "crash_crashed",
                "multiplier": round(self.crash_state["multiplier"], 2),
                "crash_point": self.crash_state["crash_point"],
                "round_id": self.crash_state["round_id"]
            })
            
            for uid in self.crash_state["players"]:
                await self.send_message(uid, {
                    "type": "crash_lost",
                    "multiplier": round(self.crash_state["multiplier"], 2)
                })
            
            self.crash_state["players"] = {}
            await asyncio.sleep(5)
            asyncio.create_task(self.start_crash_game())
        
        self.crash_task = asyncio.create_task(run_crash())
    
    async def place_crash_bet(self, user_id: str, amount: float, auto: Optional[float] = None) -> bool:
        if not self.crash_state["active"]:
            return False
        
        user = supabase.table("profiles").select("xcoin_balance").eq("id", user_id).execute()
        if user.data and user.data[0]["xcoin_balance"] >= amount:
            supabase.table("profiles").update({
                "xcoin_balance": user.data[0]["xcoin_balance"] - amount
            }).eq("id", user_id).execute()
            self.crash_state["players"][user_id] = {"bet": amount, "auto": auto}
            return True
        return False
    
    async def cashout_crash(self, user_id: str) -> Optional[float]:
        if user_id not in self.crash_state["players"]:
            return None
        
        player = self.crash_state["players"][user_id]
        win = player["bet"] * self.crash_state["multiplier"]
        
        user = supabase.table("profiles").select("xcoin_balance").eq("id", user_id).execute()
        if user.data:
            supabase.table("profiles").update({
                "xcoin_balance": user.data[0]["xcoin_balance"] + win
            }).eq("id", user_id).execute()
        
        del self.crash_state["players"][user_id]
        return win

manager = ConnectionManager()
import random

# ==================== FastAPI App ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🎰 Starting XBet Casino API...")
    await create_admin_user()
    asyncio.create_task(manager.start_crash_game())
    yield
    print("Shutting down...")

app = FastAPI(title="XBet Casino API", version="3.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL, "http://localhost:3000", "http://localhost:5000", "https://xbet-casino.onrender.com", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve static files for graphics with proper MIME types
app.mount("/assets", StaticFiles(directory="public/assets", html=True), name="assets")

# ==================== Admin Creation ====================
async def create_admin_user():
    try:
        # Check if admin exists in profiles
        existing = supabase.table("profiles").select("*").eq("email", ADMIN_EMAIL).execute()
        
        if not existing.data:
            print(f"👑 Creating admin user: {ADMIN_EMAIL}")
            
            # Create auth user
            try:
                # First check if user exists in auth
                auth_response = supabase.auth.admin.create_user({
                    "email": ADMIN_EMAIL,
                    "password": ADMIN_PASSWORD,
                    "email_confirm": True,
                    "user_metadata": {
                        "username": ADMIN_USERNAME,
                        "role": "admin"
                    }
                })
                
                if hasattr(auth_response, 'user') and auth_response.user:
                    user_id = auth_response.user.id
                    referral_code = "ADMIN001"
                    
                    # Insert profile
                    supabase.table("profiles").insert({
                        "id": user_id,
                        "email": ADMIN_EMAIL,
                        "username": ADMIN_USERNAME,
                        "role": "admin",
                        "xcoin_balance": 1000000.00,
                        "xbet_points": 100000,
                        "client_seed": secrets.token_hex(16),
                        "nonce": 0,
                        "referral_code": referral_code
                    }).execute()
                    
                    print(f"✅ Admin user created: {ADMIN_EMAIL}")
                else:
                    print("⚠️ Admin user creation failed - no user returned")
            except Exception as e:
                print(f"⚠️ Admin creation error: {e}")
                # Try to update existing user to admin
                try:
                    existing_user = supabase.table("profiles").select("*").eq("email", ADMIN_EMAIL).execute()
                    if existing_user.data:
                        supabase.table("profiles").update({
                            "role": "admin",
                            "xcoin_balance": 1000000.00
                        }).eq("email", ADMIN_EMAIL).execute()
                        print(f"✅ Updated existing user to admin: {ADMIN_EMAIL}")
                except:
                    pass
        else:
            print(f"✅ Admin user exists: {ADMIN_EMAIL}")
            
            # Ensure admin has correct role
            if existing.data[0].get("role") != "admin":
                supabase.table("profiles").update({
                    "role": "admin"
                }).eq("email", ADMIN_EMAIL).execute()
                print("✅ Admin role updated")
                
    except Exception as e:
        print(f"❌ Error checking admin: {e}")

# ==================== Auth Routes ====================
@app.post("/api/auth/register")
async def register(user_data: UserCreate):
    try:
        existing = supabase.table("profiles").select("*").eq("email", user_data.email).execute()
        if existing.data:
            raise HTTPException(400, "Email already registered")
        
        auth_resp = supabase.auth.sign_up({
            "email": user_data.email,
            "password": user_data.password,
            "options": {
                "data": {
                    "username": user_data.username or user_data.email.split("@")[0],
                    "role": "user"
                }
            }
        })
        
        if not auth_resp.user:
            raise HTTPException(400, "Registration failed")
        
        referral_code = secrets.token_hex(4).upper()
        profile_data = {
            "id": auth_resp.user.id,
            "email": user_data.email,
            "username": user_data.username or user_data.email.split("@")[0],
            "role": "user",
            "xcoin_balance": 100.00,
            "xbet_points": 0,
            "client_seed": secrets.token_hex(16),
            "nonce": 0,
            "referral_code": referral_code
        }
        
        supabase.table("profiles").insert(profile_data).execute()
        
        token = create_access_token({
            "sub": auth_resp.user.id,
            "role": "user",
            "email": user_data.email
        })
        
        return {
            "token": token,
            "user": {
                "id": profile_data["id"],
                "email": profile_data["email"],
                "username": profile_data["username"],
                "role": profile_data["role"],
                "xcoin_balance": profile_data["xcoin_balance"],
                "referral_code": referral_code
            }
        }
    except Exception as e:
        print(f"Registration error: {e}")
        raise HTTPException(400, str(e))

@app.post("/api/auth/login")
async def login(user_data: UserLogin):
    try:
        auth_resp = supabase.auth.sign_in_with_password({
            "email": user_data.email,
            "password": user_data.password
        })
        
        if not auth_resp.user:
            raise HTTPException(401, "Invalid credentials")
        
        profile = supabase.table("profiles").select("*").eq("id", auth_resp.user.id).execute()
        if not profile.data:
            raise HTTPException(401, "Profile not found")
        
        user = profile.data[0]
        if user.get("banned"):
            raise HTTPException(403, "Account banned")
        
        # Update last login
        supabase.table("profiles").update({
            "last_login": datetime.utcnow().isoformat()
        }).eq("id", user["id"]).execute()
        
        token = create_access_token({
            "sub": user["id"],
            "role": user["role"],
            "email": user["email"]
        })
        
        return {
            "token": token,
            "user": {
                "id": user["id"],
                "email": user["email"],
                "username": user["username"],
                "role": user["role"],
                "xcoin_balance": user["xcoin_balance"],
                "referral_code": user.get("referral_code", "")
            }
        }
    except Exception as e:
        print(f"Login error: {e}")
        raise HTTPException(401, "Invalid credentials")

# ==================== Game Routes ====================
@app.post("/api/games/slots/play")
async def play_slots_endpoint(bet: BetRequest, user: dict = Depends(get_current_user)):
    if bet.xcoin_amount <= 0 or bet.xcoin_amount > user["xcoin_balance"]:
        raise HTTPException(400, "Invalid bet amount")
    
    server_seed = secrets.token_hex(32)
    client_seed = user["client_seed"]
    nonce = user["nonce"] + 1
    
    result = play_slots(server_seed, client_seed, nonce, bet.xcoin_amount)
    new_balance = user["xcoin_balance"] - bet.xcoin_amount + result["win_amount"]
    
    supabase.table("profiles").update({
        "xcoin_balance": new_balance,
        "nonce": nonce
    }).eq("id", user["id"]).execute()
    
    bet_data = {
        "user_id": user["id"],
        "game_slug": "slots",
        "xcoin_amount": bet.xcoin_amount,
        "multiplier": result["multiplier"],
        "outcome": "win" if result["is_win"] else "loss",
        "xcoin_payout": result["win_amount"],
        "result": result
    }
    supabase.table("bets").insert(bet_data).execute()
    
    return {
        "outcome": "win" if result["is_win"] else "loss",
        "win_amount": result["win_amount"],
        "result": result,
        "new_balance": new_balance,
        "multiplier": result["multiplier"]
    }

@app.post("/api/games/dice/play")
async def play_dice_endpoint(bet: BetRequest, user: dict = Depends(get_current_user)):
    if bet.xcoin_amount <= 0 or bet.xcoin_amount > user["xcoin_balance"]:
        raise HTTPException(400, "Invalid bet amount")
    
    target = bet.params.get("target", 50)
    condition = bet.params.get("condition", "under")
    
    if target < 1 or target > 99:
        raise HTTPException(400, "Target must be between 1 and 99")
    
    server_seed = secrets.token_hex(32)
    client_seed = user["client_seed"]
    nonce = user["nonce"] + 1
    
    result = play_dice(server_seed, client_seed, nonce, bet.xcoin_amount, target, condition)
    new_balance = user["xcoin_balance"] - bet.xcoin_amount + result["win_amount"]
    
    supabase.table("profiles").update({
        "xcoin_balance": new_balance,
        "nonce": nonce
    }).eq("id", user["id"]).execute()
    
    supabase.table("bets").insert({
        "user_id": user["id"],
        "game_slug": "dice",
        "xcoin_amount": bet.xcoin_amount,
        "multiplier": result["multiplier"],
        "outcome": "win" if result["is_win"] else "loss",
        "xcoin_payout": result["win_amount"],
        "result": result
    }).execute()
    
    return {
        "outcome": "win" if result["is_win"] else "loss",
        "win_amount": result["win_amount"],
        "result": result,
        "new_balance": new_balance,
        "multiplier": result["multiplier"]
    }

@app.post("/api/games/crash/bet")
async def place_crash_bet(bet: BetRequest, user: dict = Depends(get_current_user)):
    if bet.xcoin_amount <= 0 or bet.xcoin_amount > user["xcoin_balance"]:
        raise HTTPException(400, "Invalid bet amount")
    
    auto_cashout = bet.params.get("auto_cashout")
    success = await manager.place_crash_bet(user["id"], bet.xcoin_amount, auto_cashout)
    
    if not success:
        raise HTTPException(400, "Cannot place bet - game may not be active")
    
    new_balance = user["xcoin_balance"] - bet.xcoin_amount
    supabase.table("profiles").update({
        "xcoin_balance": new_balance
    }).eq("id", user["id"]).execute()
    
    return {
        "message": "Bet placed",
        "new_balance": new_balance
    }

@app.post("/api/games/crash/cashout")
async def cashout_crash(user: dict = Depends(get_current_user)):
    win = await manager.cashout_crash(user["id"])
    if win is None:
        raise HTTPException(400, "No active bet or game not active")
    
    return {
        "message": "Cashed out",
        "win_amount": win
    }

@app.get("/api/games/crash/state")
async def get_crash_state():
    return {
        "active": manager.crash_state["active"],
        "multiplier": round(manager.crash_state["multiplier"], 2),
        "players": len(manager.crash_state["players"]),
        "round_id": manager.crash_state["round_id"]
    }

# ==================== Payment Routes ====================
@app.post("/api/payments/create-deposit")
async def create_deposit(deposit: DepositRequest, user: dict = Depends(get_current_user)):
    try:
        if deposit.xcoin_amount < MIN_DEPOSIT_XCOIN:
            raise HTTPException(400, f"Minimum deposit is {MIN_DEPOSIT_XCOIN} XCoin")
        
        usd_amount = deposit.xcoin_amount * XCOIN_TO_USD
        
        if STRIPE_SECRET_KEY:
            session = stripe.checkout.Session.create(
                payment_method_types=['card'],
                line_items=[{
                    'price_data': {
                        'currency': 'usd',
                        'product_data': {
                            'name': f'{deposit.xcoin_amount:,.0f} XCoin',
                            'description': f'Deposit to XBet Casino - {deposit.xcoin_amount:,.0f} XCoin',
                        },
                        'unit_amount': int(usd_amount * 100),
                    },
                    'quantity': 1,
                }],
                mode='payment',
                success_url=f'{FRONTEND_URL}/deposit/success?session_id={{CHECKOUT_SESSION_ID}}',
                cancel_url=f'{FRONTEND_URL}/deposit/cancel',
                metadata={
                    'user_id': user['id'],
                    'xcoin_amount': str(deposit.xcoin_amount),
                    'email': user['email'],
                    'username': user['username']
                }
            )
            
            supabase.table("transactions").insert({
                "user_id": user["id"],
                "type": "deposit",
                "xcoin_amount": deposit.xcoin_amount,
                "usd_amount": usd_amount,
                "status": "pending",
                "stripe_session_id": session.id
            }).execute()
            
            return {"session_id": session.id, "url": session.url}
        else:
            # Demo mode
            new_balance = user["xcoin_balance"] + deposit.xcoin_amount
            supabase.table("profiles").update({
                "xcoin_balance": new_balance
            }).eq("id", user["id"]).execute()
            
            supabase.table("transactions").insert({
                "user_id": user["id"],
                "type": "deposit",
                "xcoin_amount": deposit.xcoin_amount,
                "usd_amount": usd_amount,
                "status": "completed"
            }).execute()
            
            return {"message": "Deposit successful", "new_balance": new_balance}
    except Exception as e:
        raise HTTPException(500, f"Payment creation failed: {str(e)}")

# ==================== User Routes ====================
@app.get("/api/user/balance")
async def get_balance(user: dict = Depends(get_current_user)):
    return {
        "xcoin_balance": user["xcoin_balance"],
        "role": user["role"],
        "username": user["username"],
        "referral_code": user.get("referral_code", "")
    }

@app.post("/api/user/withdraw")
async def withdraw(withdraw: WithdrawRequest, user: dict = Depends(get_current_user)):
    if withdraw.xcoin_amount > user["xcoin_balance"]:
        raise HTTPException(400, "Insufficient balance")
    
    new_balance = user["xcoin_balance"] - withdraw.xcoin_amount
    supabase.table("profiles").update({
        "xcoin_balance": new_balance
    }).eq("id", user["id"]).execute()
    
    supabase.table("withdrawal_requests").insert({
        "user_id": user["id"],
        "xcoin_amount": withdraw.xcoin_amount,
        "address": withdraw.address,
        "status": "pending"
    }).execute()
    
    return {"message": "Withdrawal request submitted", "new_balance": new_balance}

@app.post("/api/rewards/daily")
async def claim_daily_bonus(user: dict = Depends(get_current_user)):
    last_claim = supabase.table("daily_bonuses").select("*").eq("user_id", user["id"]).execute()
    today = datetime.utcnow().date().isoformat()
    
    if last_claim.data and last_claim.data[0].get("last_claimed") == today:
        raise HTTPException(400, "Already claimed today")
    
    bonus_amount = 100
    supabase.table("daily_bonuses").upsert({
        "user_id": user["id"],
        "last_claimed": today
    }).execute()
    
    new_balance = user["xcoin_balance"] + bonus_amount
    supabase.table("profiles").update({
        "xcoin_balance": new_balance
    }).eq("id", user["id"]).execute()
    
    return {"bonus": bonus_amount, "new_balance": new_balance}

# ==================== Admin Routes ====================
@app.get("/api/admin/users")
async def get_all_users(admin: dict = Depends(get_admin_user)):
    users = supabase.table("profiles").select("*").order("created_at", desc=True).execute()
    return {"users": users.data}

@app.put("/api/admin/users/{user_id}/ban")
async def ban_user(user_id: str, ban_data: BanUserRequest, admin: dict = Depends(get_admin_user)):
    supabase.table("profiles").update({"banned": ban_data.banned}).eq("id", user_id).execute()
    return {"message": f"User {'banned' if ban_data.banned else 'unbanned'}"}

@app.get("/api/admin/analytics")
async def get_analytics(admin: dict = Depends(get_admin_user)):
    total_users = supabase.table("profiles").select("*", count="exact").execute()
    total_bets = supabase.table("bets").select("*", count="exact").execute()
    volume = supabase.table("bets").select("xcoin_amount").execute()
    payout = supabase.table("bets").select("xcoin_payout").execute()
    
    volume_sum = sum(b.get("xcoin_amount", 0) for b in volume.data)
    payout_sum = sum(b.get("xcoin_payout", 0) for b in payout.data)
    
    return {
        "total_users": total_users.count,
        "total_bets": total_bets.count,
        "total_volume": volume_sum,
        "total_payout": payout_sum,
        "house_edge": ((volume_sum - payout_sum) / volume_sum * 100) if volume_sum > 0 else 0
    }

@app.get("/api/leaderboard")
async def get_leaderboard():
    try:
        top_players = supabase.table("profiles")\
            .select("username, xcoin_balance")\
            .eq("banned", False)\
            .order("xcoin_balance", desc=True)\
            .limit(10)\
            .execute()
        
        return {"players": top_players.data}
    except Exception as e:
        return {"players": []}

@app.get("/api/online-players")
async def get_online_players():
    return {"count": len(manager.active_connections)}

# ==================== WebSocket Routes ====================
@app.websocket("/ws/{token}")
async def websocket_endpoint(websocket: WebSocket, token: str):
    try:
        payload = verify_token(token)
        user_id = payload.get("sub")
        
        user = supabase.table("profiles").select("username, role").eq("id", user_id).execute()
        if not user.data:
            await websocket.close()
            return
        
        user_data = user.data[0]
        await manager.connect(websocket, user_id, user_data)
        
        await manager.broadcast_chat("System", f"{user_data['username']} joined the chat")
        
        while True:
            data = await websocket.receive_json()
            
            if data.get("type") == "chat":
                message = data.get("message", "")[:500]
                await manager.broadcast_chat(user_data["username"], message)
                
                supabase.table("chat_messages").insert({
                    "user_id": user_id,
                    "username": user_data["username"],
                    "message": message
                }).execute()
            
            elif data.get("type") == "crash_bet":
                amount = data.get("amount", 0)
                auto = data.get("auto_cashout")
                await manager.place_crash_bet(user_id, amount, auto)
            
            elif data.get("type") == "crash_cashout":
                await manager.cashout_crash(user_id)
            
            elif data.get("type") == "ping":
                await manager.send_message(user_id, {"type": "pong"})
    
    except WebSocketDisconnect:
        if user_id in manager.users:
            username = manager.users[user_id].get("username", "User")
            await manager.broadcast_chat("System", f"{username} left the chat")
        manager.disconnect(user_id)
    except Exception as e:
        print(f"WebSocket error: {e}")
        manager.disconnect(user_id)

# ==================== Health Check ====================
@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "version": "3.0.0",
        "games": ["slots", "dice", "crash"],
        "online_players": len(manager.active_connections)
    }

# ==================== Root Endpoint ====================
@app.get("/")
async def root():
    return {
        "message": "XBet Casino API is running",
        "version": "3.0.0",
        "docs": "/docs",
        "health": "/health",
        "games": ["slots", "dice", "crash"]
    }

# ==================== Run Server ====================
if __name__ == "__main__":
    import uvicorn
    print(f"🎰 XBet Casino API Starting...")
    print(f"👑 Admin: {ADMIN_EMAIL}")
    print(f"🔑 Admin Password: {ADMIN_PASSWORD}")
    print(f"🚀 Server running on http://localhost:{PORT}")
    print(f"📚 API Docs: http://localhost:{PORT}/docs")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
