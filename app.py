import os
import secrets
import hashlib
import asyncio
import json
import stripe
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Depends, WebSocket, WebSocketDisconnect, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field, validator
from supabase import create_client, Client

# ==================== Configuration ====================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_hex(32))
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))
HOUSE_EDGE = float(os.getenv("HOUSE_EDGE", "0.01"))
FRONTEND_URL = os.getenv("FRONTEND_URL")
PORT = int(os.getenv("PORT", "5000"))

# Stripe Configuration
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
stripe.api_key = STRIPE_SECRET_KEY

# XCoin to USD conversion (1 XCoin = $0.01 USD)
XCOIN_TO_USD = 0.01
MIN_DEPOSIT_XCOIN = 100
MIN_WITHDRAWAL_XCOIN = 5000
MAX_DEPOSIT_XCOIN = 100000
MAX_WITHDRAWAL_XCOIN = 500000

# Admin credentials
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")

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
    xcoin_amount: float = Field(..., ge=MIN_DEPOSIT_XCOIN, le=MAX_DEPOSIT_XCOIN)
    
    @validator('xcoin_amount')
    def validate_amount(cls, v):
        if v < MIN_DEPOSIT_XCOIN:
            raise ValueError(f"Minimum deposit is {MIN_DEPOSIT_XCOIN} XCoin")
        return v

class WithdrawRequest(BaseModel):
    xcoin_amount: float = Field(..., ge=MIN_WITHDRAWAL_XCOIN, le=MAX_WITHDRAWAL_XCOIN)
    address: str = Field(..., min_length=10, max_length=200)
    
    @validator('xcoin_amount')
    def validate_amount(cls, v):
        if v < MIN_WITHDRAWAL_XCOIN:
            raise ValueError(f"Minimum withdrawal is {MIN_WITHDRAWAL_XCOIN} XCoin")
        return v

class BetRequest(BaseModel):
    game: str
    xcoin_amount: float = Field(..., gt=0, le=10000)
    params: Optional[Dict] = {}

class GameSettingsUpdate(BaseModel):
    game: str
    settings: Dict

class BanUserRequest(BaseModel):
    banned: bool

# ==================== Security Functions ====================
import jwt
import bcrypt

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
    return user

async def get_admin_user(user: dict = Depends(get_current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user

# ==================== Provably Fair Functions ====================
def generate_server_seed() -> str:
    return secrets.token_hex(32)

def hash_server_seed(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()

def get_random_number(server_seed: str, client_seed: str, nonce: int) -> float:
    combined = f"{server_seed}:{client_seed}:{nonce}"
    hash_value = hashlib.sha256(combined.encode()).hexdigest()
    return int(hash_value[:8], 16) / 0xffffffff

def get_random_int(min_val: int, max_val: int, server_seed: str, client_seed: str, nonce: int) -> int:
    r = get_random_number(server_seed, client_seed, nonce)
    return min_val + int(r * (max_val - min_val + 1))

# ==================== Game Logic ====================
def play_slots(server_seed: str, client_seed: str, nonce: int, bet_amount: float):
    symbols = [
        {"id": "cherry", "payout": 5, "frequency": 30},
        {"id": "lemon", "payout": 10, "frequency": 25},
        {"id": "orange", "payout": 15, "frequency": 20},
        {"id": "plum", "payout": 20, "frequency": 15},
        {"id": "bell", "payout": 50, "frequency": 8},
        {"id": "xbet", "payout": 200, "frequency": 2}
    ]
    
    paylines = [[0,1,2], [3,4,5], [6,7,8], [0,4,8], [2,4,6]]
    
    def get_symbol(pos):
        r = get_random_int(0, 99, server_seed, client_seed, nonce + pos)
        cumulative = 0
        for sym in symbols:
            cumulative += sym["frequency"]
            if r < cumulative:
                return sym["id"]
        return symbols[0]["id"]
    
    reels = [get_symbol(i) for i in range(9)]
    total_win = 0
    winning_lines = []
    
    for line in paylines:
        line_symbols = [reels[idx] for idx in line]
        if all(s == line_symbols[0] for s in line_symbols):
            for sym in symbols:
                if sym["id"] == line_symbols[0]:
                    win = bet_amount * sym["payout"]
                    total_win += win
                    winning_lines.append(line)
                    break
    
    return {
        "reels_data": reels,
        "winning_lines": winning_lines,
        "win_amount": total_win,
        "is_win": total_win > 0,
        "multiplier": total_win / bet_amount if total_win > 0 else 0
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

def generate_crash_point(server_seed: str, client_seed: str, nonce: int) -> float:
    r = get_random_number(server_seed, client_seed, nonce)
    return max(1.00, 1.00 / (1.00 - r + HOUSE_EDGE))

# ==================== WebSocket Manager ====================
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.crash_state = {
            "active": False,
            "multiplier": 1.0,
            "crash_point": 0,
            "players": {},
            "server_seed": None,
            "client_seed": None,
            "nonce": 0,
            "round_id": 0
        }
        self.crash_task = None
        self.poker_games = {}
    
    async def connect(self, websocket: WebSocket, user_id: str):
        await websocket.accept()
        self.active_connections[user_id] = websocket
    
    def disconnect(self, user_id: str):
        if user_id in self.active_connections:
            del self.active_connections[user_id]
    
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
    
    async def start_crash_game(self):
        if self.crash_state["active"]:
            return
        
        self.crash_state.update({
            "server_seed": generate_server_seed(),
            "client_seed": "xbet_crash_seed",
            "nonce": self.crash_state["nonce"] + 1,
            "active": True,
            "multiplier": 1.0,
            "players": {},
            "round_id": self.crash_state["round_id"] + 1
        })
        
        r = get_random_number(
            self.crash_state["server_seed"],
            self.crash_state["client_seed"],
            self.crash_state["nonce"]
        )
        self.crash_state["crash_point"] = max(1.00, 1.00 / (1.00 - r + HOUSE_EDGE))
        
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
                
                # Check auto cashouts
                for uid, player in list(self.crash_state["players"].items()):
                    if player.get("auto") and player["auto"] <= self.crash_state["multiplier"]:
                        win = player["bet"] * self.crash_state["multiplier"]
                        user = supabase.table("profiles").select("xcoin_balance").eq("id", uid).execute()
                        if user.data:
                            supabase.table("profiles").update({
                                "xcoin_balance": user.data[0]["xcoin_balance"] + win
                            }).eq("id", uid).execute()
                        del self.crash_state["players"][uid]
                        await self.broadcast({
                            "type": "crash_cashout",
                            "user_id": uid,
                            "win": round(win, 2),
                            "multiplier": round(self.crash_state["multiplier"], 2),
                            "auto": True
                        })
            
            self.crash_state["active"] = False
            await self.broadcast({
                "type": "crash_crashed",
                "multiplier": round(self.crash_state["multiplier"], 2),
                "crash_point": self.crash_state["crash_point"],
                "round_id": self.crash_state["round_id"]
            })
            
            # Process remaining players (they lose)
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

# ==================== FastAPI App ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Starting XBet Casino API...")
    await create_admin_user()
    asyncio.create_task(manager.start_crash_game())
    yield
    print("Shutting down...")

app = FastAPI(title="XBet Casino API", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL, "http://localhost:3000", "http://localhost:5000", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==================== Admin Creation ====================
async def create_admin_user():
    try:
        existing = supabase.table("profiles").select("*").eq("email", ADMIN_EMAIL).execute()
        if not existing.data:
            print(f"Creating admin user: {ADMIN_EMAIL}")
            try:
                auth_response = supabase.auth.admin.create_user({
                    "email": ADMIN_EMAIL,
                    "password": ADMIN_PASSWORD,
                    "email_confirm": True,
                    "user_metadata": {"username": ADMIN_USERNAME, "role": "admin"}
                })
                if hasattr(auth_response, 'user') and auth_response.user:
                    user_id = auth_response.user.id
                    referral_code = secrets.token_hex(4).upper()
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
                    print(f"Admin user created: {ADMIN_EMAIL}")
            except Exception as e:
                print(f"Admin creation error: {e}")
        else:
            print(f"Admin user exists: {ADMIN_EMAIL}")
    except Exception as e:
        print(f"Error checking admin: {e}")

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
            "options": {"data": {"username": user_data.username or user_data.email.split("@")[0]}}
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
        
        # Handle referral if provided
        if user_data.referral_code:
            referrer = supabase.table("profiles").select("id").eq("referral_code", user_data.referral_code.upper()).execute()
            if referrer.data:
                profile_data["referred_by"] = referrer.data[0]["id"]
        
        supabase.table("profiles").insert(profile_data).execute()
        token = create_access_token({"sub": auth_resp.user.id})
        
        return {
            "token": token,
            "user": {
                "id": profile_data["id"],
                "email": profile_data["email"],
                "username": profile_data["username"],
                "role": profile_data["role"],
                "xcoin_balance": profile_data["xcoin_balance"],
                "xbet_points": profile_data["xbet_points"],
                "referral_code": referral_code
            }
        }
    except Exception as e:
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
        
        supabase.table("profiles").update({
            "last_login": datetime.utcnow().isoformat()
        }).eq("id", user["id"]).execute()
        
        token = create_access_token({"sub": user["id"]})
        
        return {
            "token": token,
            "user": {
                "id": user["id"],
                "email": user["email"],
                "username": user["username"],
                "role": user["role"],
                "xcoin_balance": user["xcoin_balance"],
                "xbet_points": user["xbet_points"],
                "referral_code": user.get("referral_code", "")
            }
        }
    except Exception as e:
        raise HTTPException(401, "Invalid credentials")

@app.post("/api/auth/logout")
async def logout():
    return {"message": "Logged out"}

# ==================== Stripe Payment Routes ====================
@app.post("/api/payments/create-deposit")
async def create_deposit(deposit: DepositRequest, user: dict = Depends(get_current_user)):
    try:
        if deposit.xcoin_amount < MIN_DEPOSIT_XCOIN:
            raise HTTPException(400, f"Minimum deposit is {MIN_DEPOSIT_XCOIN} XCoin")
        
        usd_amount = deposit.xcoin_amount * XCOIN_TO_USD
        
        # Create Stripe Checkout Session
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'usd',
                    'product_data': {
                        'name': f'{deposit.xcoin_amount:,.0f} XCoin',
                        'description': f'Deposit to XBet Casino - {deposit.xcoin_amount:,.0f} XCoin',
                        'images': [f'{FRONTEND_URL}/assets/coins/xcoin-gold.png']
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
        
        # Create pending transaction
        supabase.table("transactions").insert({
            "user_id": user["id"],
            "type": "deposit",
            "xcoin_amount": deposit.xcoin_amount,
            "usd_amount": usd_amount,
            "status": "pending",
            "stripe_session_id": session.id,
            "stripe_payment_intent": session.payment_intent
        }).execute()
        
        return {"session_id": session.id, "url": session.url}
    except Exception as e:
        raise HTTPException(500, f"Payment creation failed: {str(e)}")

@app.post("/api/payments/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(500, "Webhook secret not configured")
    
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except ValueError:
        raise HTTPException(400, "Invalid payload")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(400, "Invalid signature")
    
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        await handle_successful_deposit(session)
    elif event["type"] == "checkout.session.async_payment_failed":
        session = event["data"]["object"]
        await handle_failed_deposit(session)
    
    return {"received": True}

async def handle_successful_deposit(session):
    user_id = session["metadata"]["user_id"]
    xcoin_amount = float(session["metadata"]["xcoin_amount"])
    
    # Update transaction status
    supabase.table("transactions").update({
        "status": "completed",
        "completed_at": datetime.utcnow().isoformat()
    }).eq("stripe_session_id", session["id"]).execute()
    
    # Update user balance
    user = supabase.table("profiles").select("xcoin_balance, referred_by").eq("id", user_id).execute()
    if user.data:
        new_balance = user.data[0]["xcoin_balance"] + xcoin_amount
        supabase.table("profiles").update({
            "xcoin_balance": new_balance
        }).eq("id", user_id).execute()
        
        # Handle referral bonus if first deposit
        referred_by = user.data[0].get("referred_by")
        if referred_by:
            referral_bonus = xcoin_amount * 0.10  # 10% referral bonus
            referrer = supabase.table("profiles").select("xcoin_balance").eq("id", referred_by).execute()
            if referrer.data:
                supabase.table("profiles").update({
                    "xcoin_balance": referrer.data[0]["xcoin_balance"] + referral_bonus
                }).eq("id", referred_by).execute()
                
                # Record referral bonus transaction
                supabase.table("transactions").insert({
                    "user_id": referred_by,
                    "type": "referral",
                    "xcoin_amount": referral_bonus,
                    "usd_amount": referral_bonus * XCOIN_TO_USD,
                    "status": "completed",
                    "metadata": {"referred_user": user_id, "deposit_amount": xcoin_amount}
                }).execute()

async def handle_failed_deposit(session):
    supabase.table("transactions").update({
        "status": "failed"
    }).eq("stripe_session_id", session["id"]).execute()

@app.get("/api/payments/rate")
async def get_xcoin_rate():
    return {
        "xcoin_to_usd": XCOIN_TO_USD,
        "usd_to_xcoin": 1 / XCOIN_TO_USD,
        "min_deposit_xcoin": MIN_DEPOSIT_XCOIN,
        "min_deposit_usd": MIN_DEPOSIT_XCOIN * XCOIN_TO_USD,
        "max_deposit_xcoin": MAX_DEPOSIT_XCOIN,
        "max_deposit_usd": MAX_DEPOSIT_XCOIN * XCOIN_TO_USD
    }

# ==================== Game Routes ====================
@app.post("/api/games/slots/play")
async def play_slots(bet: BetRequest, user: dict = Depends(get_current_user)):
    if bet.xcoin_amount <= 0 or bet.xcoin_amount > user["xcoin_balance"]:
        raise HTTPException(400, "Invalid bet amount")
    
    server_seed = generate_server_seed()
    client_seed = user["client_seed"]
    nonce = user["nonce"] + 1
    
    result = play_slots(server_seed, client_seed, nonce, bet.xcoin_amount)
    new_balance = user["xcoin_balance"] - bet.xcoin_amount + result["win_amount"]
    
    supabase.table("profiles").update({
        "xcoin_balance": new_balance,
        "nonce": nonce,
        "server_seed": hash_server_seed(server_seed)
    }).eq("id", user["id"]).execute()
    
    bet_data = {
        "user_id": user["id"],
        "game_slug": "slots",
        "xcoin_amount": bet.xcoin_amount,
        "multiplier": result["multiplier"],
        "outcome": "win" if result["is_win"] else "loss",
        "xcoin_payout": result["win_amount"],
        "server_seed": server_seed,
        "client_seed": client_seed,
        "nonce": nonce,
        "result": result
    }
    supabase.table("bets").insert(bet_data).execute()
    
    return {
        "bet_id": "",
        "outcome": "win" if result["is_win"] else "loss",
        "win_amount": result["win_amount"],
        "result": result,
        "new_balance": new_balance,
        "multiplier": result["multiplier"],
        "server_seed": server_seed,
        "client_seed": client_seed,
        "nonce": nonce
    }

@app.post("/api/games/dice/play")
async def play_dice(bet: BetRequest, user: dict = Depends(get_current_user)):
    if bet.xcoin_amount <= 0 or bet.xcoin_amount > user["xcoin_balance"]:
        raise HTTPException(400, "Invalid bet amount")
    
    target = bet.params.get("target", 50)
    condition = bet.params.get("condition", "under")
    
    if target < 1 or target > 99:
        raise HTTPException(400, "Target must be between 1 and 99")
    
    server_seed = generate_server_seed()
    client_seed = user["client_seed"]
    nonce = user["nonce"] + 1
    
    result = play_dice(server_seed, client_seed, nonce, bet.xcoin_amount, target, condition)
    new_balance = user["xcoin_balance"] - bet.xcoin_amount + result["win_amount"]
    
    supabase.table("profiles").update({
        "xcoin_balance": new_balance,
        "nonce": nonce,
        "server_seed": hash_server_seed(server_seed)
    }).eq("id", user["id"]).execute()
    
    supabase.table("bets").insert({
        "user_id": user["id"],
        "game_slug": "dice",
        "xcoin_amount": bet.xcoin_amount,
        "multiplier": result["multiplier"],
        "outcome": "win" if result["is_win"] else "loss",
        "xcoin_payout": result["win_amount"],
        "server_seed": server_seed,
        "client_seed": client_seed,
        "nonce": nonce,
        "result": result
    }).execute()
    
    return {
        "bet_id": "",
        "outcome": "win" if result["is_win"] else "loss",
        "win_amount": result["win_amount"],
        "result": result,
        "new_balance": new_balance,
        "multiplier": result["multiplier"],
        "server_seed": server_seed,
        "client_seed": client_seed,
        "nonce": nonce
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

@app.post("/api/games/crash/start")
async def start_crash_game(admin: dict = Depends(get_admin_user)):
    asyncio.create_task(manager.start_crash_game())
    return {"message": "Game started"}

# ==================== User Routes ====================
@app.get("/api/user/balance")
async def get_balance(user: dict = Depends(get_current_user)):
    return {
        "xcoin_balance": user["xcoin_balance"],
        "role": user["role"],
        "username": user["username"],
        "xbet_points": user.get("xbet_points", 0),
        "referral_code": user.get("referral_code", "")
    }

@app.post("/api/user/withdraw")
async def withdraw(withdraw: WithdrawRequest, user: dict = Depends(get_current_user)):
    if withdraw.xcoin_amount <= 0:
        raise HTTPException(400, "Invalid amount")
    
    if withdraw.xcoin_amount > user["xcoin_balance"]:
        raise HTTPException(400, "Insufficient balance")
    
    if withdraw.xcoin_amount < MIN_WITHDRAWAL_XCOIN:
        raise HTTPException(400, f"Minimum withdrawal is {MIN_WITHDRAWAL_XCOIN} XCoin")
    
    # Create withdrawal request
    supabase.table("withdrawal_requests").insert({
        "user_id": user["id"],
        "xcoin_amount": withdraw.xcoin_amount,
        "address": withdraw.address,
        "status": "pending"
    }).execute()
    
    # Record transaction
    supabase.table("transactions").insert({
        "user_id": user["id"],
        "type": "withdrawal",
        "xcoin_amount": withdraw.xcoin_amount,
        "usd_amount": withdraw.xcoin_amount * XCOIN_TO_USD,
        "status": "pending"
    }).execute()
    
    # Deduct balance
    new_balance = user["xcoin_balance"] - withdraw.xcoin_amount
    supabase.table("profiles").update({
        "xcoin_balance": new_balance
    }).eq("id", user["id"]).execute()
    
    return {"message": "Withdrawal request submitted", "new_balance": new_balance}

@app.get("/api/user/history")
async def get_history(user: dict = Depends(get_current_user)):
    bets = supabase.table("bets").select("*").eq("user_id", user["id"]).order("created_at", desc=True).limit(50).execute()
    transactions = supabase.table("transactions").select("*").eq("user_id", user["id"]).order("created_at", desc=True).limit(20).execute()
    return {"bets": bets.data, "transactions": transactions.data}

@app.get("/api/user/transactions")
async def get_transactions(user: dict = Depends(get_current_user)):
    txs = supabase.table("transactions").select("*").eq("user_id", user["id"]).order("created_at", desc=True).limit(50).execute()
    return {"transactions": txs.data}

# ==================== Reward Routes ====================
@app.post("/api/rewards/daily")
async def claim_daily_bonus(user: dict = Depends(get_current_user)):
    last_claim = supabase.table("daily_bonuses").select("*").eq("user_id", user["id"]).execute()
    today = datetime.utcnow().date().isoformat()
    
    if last_claim.data and last_claim.data[0].get("last_claimed") == today:
        raise HTTPException(400, "Already claimed today")
    
    bonus_amount = 100
    streak = 1
    
    if last_claim.data:
        last_date = datetime.fromisoformat(last_claim.data[0]["last_claimed"]).date()
        yesterday = datetime.utcnow().date() - timedelta(days=1)
        if last_date == yesterday:
            streak = last_claim.data[0].get("streak", 0) + 1
            bonus_amount += (streak - 1) * 10
    
    supabase.table("daily_bonuses").upsert({
        "user_id": user["id"],
        "last_claimed": today,
        "streak": streak
    }).execute()
    
    new_balance = user["xcoin_balance"] + bonus_amount
    supabase.table("profiles").update({
        "xcoin_balance": new_balance
    }).eq("id", user["id"]).execute()
    
    # Record bonus transaction
    supabase.table("transactions").insert({
        "user_id": user["id"],
        "type": "bonus",
        "xcoin_amount": bonus_amount,
        "usd_amount": bonus_amount * XCOIN_TO_USD,
        "status": "completed"
    }).execute()
    
    return {"bonus": bonus_amount, "streak": streak, "new_balance": new_balance}

# ==================== Leaderboard Routes ====================
@app.get("/api/leaderboard")
async def get_leaderboard():
    try:
        # Biggest win
        biggest_win = supabase.table("bets").select("user_id, xcoin_payout, profiles(username)").eq("outcome", "win").order("xcoin_payout", desc=True).limit(1).execute()
        biggest_win_data = biggest_win.data[0] if biggest_win.data else None
        if biggest_win_data:
            biggest_win_data["username"] = biggest_win_data.get("profiles", {}).get("username", "Unknown")
        
        # Most games
        most_games = supabase.rpc("get_most_games").execute() if hasattr(supabase.rpc, "__call__") else None
        
        # Total wagered
        total_wagered = supabase.rpc("get_total_wagered").execute() if hasattr(supabase.rpc, "__call__") else None
        
        return {
            "biggest_win": biggest_win_data,
            "most_games": most_games.data[0] if most_games and most_games.data else None,
            "total_wagered": total_wagered.data[0] if total_wagered and total_wagered.data else None
        }
    except Exception as e:
        return {
            "biggest_win": None,
            "most_games": None,
            "total_wagered": None
        }

# ==================== Admin Routes ====================
@app.get("/api/admin/users")
async def get_all_users(admin: dict = Depends(get_admin_user)):
    users = supabase.table("profiles").select("*").order("created_at", desc=True).execute()
    return {"users": users.data}

@app.put("/api/admin/users/{user_id}/ban")
async def ban_user(user_id: str, ban_data: BanUserRequest, admin: dict = Depends(get_admin_user)):
    user = supabase.table("profiles").select("banned").eq("id", user_id).execute()
    if not user.data:
        raise HTTPException(404, "User not found")
    
    new_status = ban_data.banned
    supabase.table("profiles").update({"banned": new_status}).eq("id", user_id).execute()
    return {"message": f"User {'banned' if new_status else 'unbanned'}"}

@app.get("/api/admin/analytics")
async def get_analytics(admin: dict = Depends(get_admin_user)):
    total_users = supabase.table("profiles").select("*", count="exact").execute()
    total_bets = supabase.table("bets").select("*", count="exact").execute()
    total_deposits = supabase.table("transactions").select("xcoin_amount").eq("type", "deposit").eq("status", "completed").execute()
    total_withdrawals = supabase.table("transactions").select("xcoin_amount").eq("type", "withdrawal").eq("status", "completed").execute()
    
    volume = supabase.table("bets").select("xcoin_amount").execute()
    payout = supabase.table("bets").select("xcoin_payout").execute()
    
    volume_sum = sum(b.get("xcoin_amount", 0) for b in volume.data)
    payout_sum = sum(b.get("xcoin_payout", 0) for b in payout.data)
    deposit_sum = sum(d.get("xcoin_amount", 0) for d in total_deposits.data)
    withdrawal_sum = sum(w.get("xcoin_amount", 0) for w in total_withdrawals.data)
    
    return {
        "total_users": total_users.count,
        "total_bets": total_bets.count,
        "total_volume": volume_sum,
        "total_payout": payout_sum,
        "total_deposits": deposit_sum,
        "total_withdrawals": withdrawal_sum,
        "house_edge": ((volume_sum - payout_sum) / volume_sum * 100) if volume_sum > 0 else 0
    }

@app.get("/api/admin/settings/{game}")
async def get_game_settings(game: str):
    settings = supabase.table("game_settings").select("*").eq("game_id", game).execute()
    if not settings.data:
        default_settings = {
            "slots": {"rtp": 96.5, "house_edge": 3.5},
            "dice": {"house_edge": 1, "min_target": 1, "max_target": 99},
            "crash": {"max_multiplier": 10000, "curve_speed": 1.03, "house_edge": 1}
        }
        return {"game": game, "settings": default_settings.get(game, {})}
    return settings.data[0]

@app.put("/api/admin/settings/{game}")
async def update_game_settings(game: str, settings_update: GameSettingsUpdate, admin: dict = Depends(get_admin_user)):
    supabase.table("game_settings").upsert({
        "game_id": game,
        "settings": settings_update.settings,
        "updated_at": datetime.utcnow().isoformat()
    }).execute()
    return {"message": f"{game} settings updated"}

# ==================== WebSocket Routes ====================
@app.websocket("/ws/{token}")
async def websocket_endpoint(websocket: WebSocket, token: str):
    try:
        payload = verify_token(token)
        user_id = payload.get("sub")
        
        await manager.connect(websocket, user_id)
        
        user = supabase.table("profiles").select("username").eq("id", user_id).execute()
        username = user.data[0]["username"] if user.data else "User"
        
        while True:
            data = await websocket.receive_json()
            
            if data.get("type") == "chat":
                message = data.get("message", "")[:500]
                room = data.get("room", "global")
                
                supabase.table("chat_messages").insert({
                    "user_id": user_id,
                    "username": username,
                    "room": room,
                    "message": message
                }).execute()
                
                await manager.broadcast({
                    "type": "chat",
                    "username": username,
                    "message": message,
                    "room": room,
                    "timestamp": datetime.utcnow().isoformat()
                })
            
            elif data.get("type") == "crash_bet":
                amount = data.get("amount", 0)
                auto = data.get("auto_cashout")
                
                if await manager.place_crash_bet(user_id, amount, auto):
                    await manager.broadcast({
                        "type": "crash_bet_placed",
                        "username": username,
                        "bet": amount,
                        "auto_cashout": auto
                    })
                else:
                    await manager.send_message(user_id, {"type": "error", "message": "Cannot place bet"})
            
            elif data.get("type") == "crash_cashout":
                win = await manager.cashout_crash(user_id)
                if win:
                    await manager.broadcast({
                        "type": "crash_cashout",
                        "username": username,
                        "win": round(win, 2),
                        "multiplier": round(manager.crash_state["multiplier"], 2)
                    })
    
    except WebSocketDisconnect:
        manager.disconnect(user_id)
    except Exception as e:
        print(f"WebSocket error: {e}")

# ==================== Health Check ====================
@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "version": "2.0.0",
        "environment": "production" if STRIPE_SECRET_KEY else "development"
    }

# ==================== Run Server ====================
if __name__ == "__main__":
    import uvicorn
    print(f"Starting XBet Casino API on port {PORT}")
    print(f"Admin credentials: {ADMIN_EMAIL} / {ADMIN_PASSWORD}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
