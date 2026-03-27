import os
import secrets
import hashlib
import json
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Depends, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr
from supabase import create_client, Client

# ==================== Configuration from Environment ====================

# Supabase Configuration
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

# JWT Configuration
SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))

# Game Configuration
HOUSE_EDGE = float(os.getenv("HOUSE_EDGE", "0.01"))

# Admin Credentials (from Render environment variables)
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")

# Frontend URL
FRONTEND_URL = os.getenv("FRONTEND_URL")

# Initialize Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# ==================== Models ====================

class UserCreate(BaseModel):
    email: EmailStr
    password: str
    username: Optional[str] = None
    referral_code: Optional[str] = None

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class UserResponse(BaseModel):
    id: str
    email: str
    username: str
    role: str
    xcoin_balance: float
    xbet_points: int
    referral_code: str

class BetRequest(BaseModel):
    game: str
    xcoin_amount: float
    params: Optional[Dict] = {}

class BetResponse(BaseModel):
    bet_id: str
    outcome: str
    win_amount: float
    result: Dict
    new_balance: float
    multiplier: float

class WithdrawRequest(BaseModel):
    xcoin_amount: float
    address: str

class ChatMessage(BaseModel):
    message: str
    room: str = "global"

# ==================== Security Functions ====================

def hash_password(password: str) -> str:
    import bcrypt
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')

def verify_password(password: str, hashed: str) -> bool:
    import bcrypt
    return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))

def create_access_token(data: dict) -> str:
    import jwt
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def verify_token(token: str) -> dict:
    import jwt
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

# ==================== Auth Dependencies ====================

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
        {"id": "cherry", "payout": 5, "frequency": 30, "emoji": "🍒"},
        {"id": "lemon", "payout": 10, "frequency": 25, "emoji": "🍋"},
        {"id": "orange", "payout": 15, "frequency": 20, "emoji": "🍊"},
        {"id": "plum", "payout": 20, "frequency": 15, "emoji": "🍑"},
        {"id": "bell", "payout": 50, "frequency": 8, "emoji": "🔔"},
        {"id": "xbet", "payout": 200, "frequency": 2, "emoji": "⭐"}
    ]
    
    paylines = [[0,1,2], [3,4,5], [6,7,8], [0,4,8], [2,4,6]]
    
    def get_symbol(pos):
        r = get_random_int(0, 99, server_seed, client_seed, nonce + pos)
        cumulative = 0
        for sym in symbols:
            cumulative += sym["frequency"]
            if r < cumulative:
                return sym
        return symbols[0]
    
    reels = [get_symbol(i) for i in range(9)]
    reels_emoji = [r["emoji"] for r in reels]
    
    total_win = 0
    winning_lines = []
    
    for line in paylines:
        line_symbols = [reels[idx] for idx in line]
        if all(s["id"] == line_symbols[0]["id"] for s in line_symbols):
            win = bet_amount * line_symbols[0]["payout"]
            total_win += win
            winning_lines.append(line)
    
    return {
        "reels": reels_emoji,
        "reels_data": [r["id"] for r in reels],
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

# ==================== WebSocket Manager ====================

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.crash_game_state = {
            "active": False,
            "multiplier": 1.0,
            "crash_point": 0,
            "players": {},
            "server_seed": None,
            "client_seed": None,
            "nonce": 0
        }
        self.crash_task = None
    
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
        if self.crash_game_state["active"]:
            return
        
        self.crash_game_state["server_seed"] = generate_server_seed()
        self.crash_game_state["client_seed"] = "xbet_crash_seed"
        self.crash_game_state["nonce"] += 1
        self.crash_game_state["active"] = True
        self.crash_game_state["multiplier"] = 1.0
        self.crash_game_state["players"] = {}
        
        r = get_random_number(
            self.crash_game_state["server_seed"],
            self.crash_game_state["client_seed"],
            self.crash_game_state["nonce"]
        )
        self.crash_game_state["crash_point"] = max(1.00, 1.00 / (1.00 - r + HOUSE_EDGE))
        
        await self.broadcast({
            "type": "crash_start",
            "crash_point": self.crash_game_state["crash_point"]
        })
        
        async def run_crash():
            while self.crash_game_state["active"] and self.crash_game_state["multiplier"] < self.crash_game_state["crash_point"]:
                await asyncio.sleep(0.1)
                self.crash_game_state["multiplier"] *= 1.03
                
                await self.broadcast({
                    "type": "crash_multiplier",
                    "multiplier": round(self.crash_game_state["multiplier"], 2)
                })
                
                # Check auto cashouts
                for user_id, player in list(self.crash_game_state["players"].items()):
                    if player.get("auto_cashout") and player["auto_cashout"] <= self.crash_game_state["multiplier"]:
                        win = player["bet"] * self.crash_game_state["multiplier"]
                        
                        # Update user balance
                        user_response = supabase.table("profiles").select("xcoin_balance").eq("id", user_id).execute()
                        if user_response.data:
                            new_balance = user_response.data[0]["xcoin_balance"] + win
                            supabase.table("profiles").update({
                                "xcoin_balance": new_balance
                            }).eq("id", user_id).execute()
                        
                        del self.crash_game_state["players"][user_id]
                        
                        await self.broadcast({
                            "type": "crash_cashout",
                            "user_id": user_id,
                            "win": round(win, 2),
                            "auto": True
                        })
            
            # Game crashed
            self.crash_game_state["active"] = False
            await self.broadcast({
                "type": "crash_crashed",
                "multiplier": round(self.crash_game_state["multiplier"], 2),
                "crash_point": self.crash_game_state["crash_point"]
            })
            
            self.crash_game_state["players"] = {}
            
            # Auto restart after 5 seconds
            await asyncio.sleep(5)
            asyncio.create_task(self.start_crash_game())
        
        self.crash_task = asyncio.create_task(run_crash())
    
    async def place_crash_bet(self, user_id: str, amount: float, auto_cashout: Optional[float] = None):
        if not self.crash_game_state["active"]:
            return False
        
        # Deduct balance immediately
        user_response = supabase.table("profiles").select("xcoin_balance").eq("id", user_id).execute()
        if user_response.data and user_response.data[0]["xcoin_balance"] >= amount:
            new_balance = user_response.data[0]["xcoin_balance"] - amount
            supabase.table("profiles").update({
                "xcoin_balance": new_balance
            }).eq("id", user_id).execute()
            
            self.crash_game_state["players"][user_id] = {
                "bet": amount,
                "auto_cashout": auto_cashout
            }
            return True
        return False
    
    async def cashout_crash(self, user_id: str) -> Optional[float]:
        if user_id not in self.crash_game_state["players"]:
            return None
        
        player = self.crash_game_state["players"][user_id]
        win = player["bet"] * self.crash_game_state["multiplier"]
        del self.crash_game_state["players"][user_id]
        
        # Add winnings to balance
        user_response = supabase.table("profiles").select("xcoin_balance").eq("id", user_id).execute()
        if user_response.data:
            new_balance = user_response.data[0]["xcoin_balance"] + win
            supabase.table("profiles").update({
                "xcoin_balance": new_balance
            }).eq("id", user_id).execute()
        
        return win

manager = ConnectionManager()

# ==================== FastAPI App ====================

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Create admin user if not exists
    await create_admin_user()
    # Start crash game
    asyncio.create_task(manager.start_crash_game())
    yield
    # Shutdown
    pass

app = FastAPI(title="XBet Casino API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL, "http://localhost:3000", "http://localhost:5000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==================== Admin Creation Function ====================

async def create_admin_user():
    """Create admin user from environment variables"""
    try:
        # Check if admin already exists
        response = supabase.table("profiles").select("*").eq("email", ADMIN_EMAIL).execute()
        
        if not response.data:
            print(f"Creating admin user: {ADMIN_EMAIL}")
            
            # Create user in Supabase Auth
            try:
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
                    
                    # Create admin profile
                    supabase.table("profiles").insert({
                        "id": user_id,
                        "email": ADMIN_EMAIL,
                        "username": ADMIN_USERNAME,
                        "role": "admin",
                        "xcoin_balance": 1000000.00,
                        "xbet_points": 100000,
                        "client_seed": secrets.token_hex(16)
                    }).execute()
                    
                    print(f"Admin user created successfully: {ADMIN_EMAIL}")
                else:
                    print("Failed to create admin user in auth")
            except Exception as e:
                print(f"Admin user already exists or error: {e}")
        else:
            print(f"Admin user already exists: {ADMIN_EMAIL}")
    except Exception as e:
        print(f"Error creating admin user: {e}")

# ==================== Auth Routes ====================

@app.post("/api/auth/register")
async def register(user_data: UserCreate):
    try:
        # Check if user exists
        existing = supabase.table("profiles").select("*").eq("email", user_data.email).execute()
        if existing.data:
            raise HTTPException(status_code=400, detail="Email already registered")
        
        # Create user in Supabase Auth
        response = supabase.auth.sign_up({
            "email": user_data.email,
            "password": user_data.password,
            "options": {
                "data": {
                    "username": user_data.username or user_data.email.split("@")[0],
                    "role": "user"
                }
            }
        })
        
        if not response.user:
            raise HTTPException(status_code=400, detail="Registration failed")
        
        # Create profile
        profile_data = {
            "id": response.user.id,
            "email": user_data.email,
            "username": user_data.username or user_data.email.split("@")[0],
            "role": "user",
            "xcoin_balance": 100.00,
            "client_seed": secrets.token_hex(16)
        }
        
        # Handle referral
        if user_data.referral_code:
            referrer = supabase.table("profiles").select("id").eq("referral_code", user_data.referral_code.upper()).execute()
            if referrer.data:
                profile_data["referred_by"] = referrer.data[0]["id"]
        
        supabase.table("profiles").insert(profile_data).execute()
        
        token = create_access_token({"sub": response.user.id})
        
        return {
            "token": token,
            "user": {
                "id": response.user.id,
                "email": user_data.email,
                "username": profile_data["username"],
                "role": "user",
                "xcoin_balance": 100.00,
                "xbet_points": 0,
                "referral_code": profile_data.get("referral_code", "")
            }
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/auth/login")
async def login(user_data: UserLogin):
    try:
        response = supabase.auth.sign_in_with_password({
            "email": user_data.email,
            "password": user_data.password
        })
        
        if not response.user:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        
        profile = supabase.table("profiles").select("*").eq("id", response.user.id).execute()
        if not profile.data:
            raise HTTPException(status_code=401, detail="Profile not found")
        
        user = profile.data[0]
        
        if user.get("banned"):
            raise HTTPException(status_code=403, detail="Account banned")
        
        # Update last login
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
                "referral_code": user["referral_code"]
            }
        }
    except Exception as e:
        raise HTTPException(status_code=401, detail="Invalid credentials")

# ==================== Game Routes ====================

@app.post("/api/games/slots/play")
async def play_slots_game(bet: BetRequest, user: dict = Depends(get_current_user)):
    if bet.xcoin_amount <= 0 or bet.xcoin_amount > user["xcoin_balance"]:
        raise HTTPException(status_code=400, detail="Invalid bet amount")
    
    server_seed = generate_server_seed()
    client_seed = user["client_seed"]
    nonce = user["nonce"] + 1
    
    result = play_slots(server_seed, client_seed, nonce, bet.xcoin_amount)
    
    # Record bet
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
    
    # Update user balance
    new_balance = user["xcoin_balance"] - bet.xcoin_amount + result["win_amount"]
    supabase.table("profiles").update({
        "xcoin_balance": new_balance,
        "nonce": nonce,
        "server_seed": hash_server_seed(server_seed)
    }).eq("id", user["id"]).execute()
    
    return BetResponse(
        bet_id="",
        outcome="win" if result["is_win"] else "loss",
        win_amount=result["win_amount"],
        result=result,
        new_balance=new_balance,
        multiplier=result["multiplier"]
    )

@app.post("/api/games/dice/play")
async def play_dice_game(bet: BetRequest, user: dict = Depends(get_current_user)):
    if bet.xcoin_amount <= 0 or bet.xcoin_amount > user["xcoin_balance"]:
        raise HTTPException(status_code=400, detail="Invalid bet amount")
    
    target = bet.params.get("target", 50)
    condition = bet.params.get("condition", "under")
    
    if target < 1 or target > 99:
        raise HTTPException(status_code=400, detail="Target must be between 1 and 99")
    
    server_seed = generate_server_seed()
    client_seed = user["client_seed"]
    nonce = user["nonce"] + 1
    
    result = play_dice(server_seed, client_seed, nonce, bet.xcoin_amount, target, condition)
    
    bet_data = {
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
    }
    
    supabase.table("bets").insert(bet_data).execute()
    
    new_balance = user["xcoin_balance"] - bet.xcoin_amount + result["win_amount"]
    supabase.table("profiles").update({
        "xcoin_balance": new_balance,
        "nonce": nonce
    }).eq("id", user["id"]).execute()
    
    return BetResponse(
        bet_id="",
        outcome="win" if result["is_win"] else "loss",
        win_amount=result["win_amount"],
        result=result,
        new_balance=new_balance,
        multiplier=result["multiplier"]
    )

@app.get("/api/games/crash/state")
async def get_crash_state():
    return {
        "active": manager.crash_game_state["active"],
        "multiplier": round(manager.crash_game_state["multiplier"], 2),
        "players": len(manager.crash_game_state["players"])
    }

# ==================== User Routes ====================

@app.get("/api/user/balance")
async def get_balance(user: dict = Depends(get_current_user)):
    return {"xcoin_balance": user["xcoin_balance"], "xbet_points": user["xbet_points"]}

@app.post("/api/user/withdraw")
async def withdraw(withdraw: WithdrawRequest, user: dict = Depends(get_current_user)):
    if withdraw.xcoin_amount <= 0:
        raise HTTPException(status_code=400, detail="Invalid amount")
    
    if withdraw.xcoin_amount > user["xcoin_balance"]:
        raise HTTPException(status_code=400, detail="Insufficient balance")
    
    if withdraw.xcoin_amount < 5000:
        raise HTTPException(status_code=400, detail="Minimum withdrawal is 5000 XCoin")
    
    supabase.table("withdrawal_requests").insert({
        "user_id": user["id"],
        "xcoin_amount": withdraw.xcoin_amount,
        "address": withdraw.address,
        "status": "pending"
    }).execute()
    
    new_balance = user["xcoin_balance"] - withdraw.xcoin_amount
    supabase.table("profiles").update({
        "xcoin_balance": new_balance
    }).eq("id", user["id"]).execute()
    
    return {"message": "Withdrawal request submitted", "new_balance": new_balance}

@app.get("/api/user/history")
async def get_history(user: dict = Depends(get_current_user)):
    bets = supabase.table("bets").select("*").eq("user_id", user["id"]).order("created_at", desc=True).limit(50).execute()
    return {"bets": bets.data}

# ==================== Admin Routes ====================

@app.get("/api/admin/users")
async def get_all_users(admin: dict = Depends(get_admin_user)):
    users = supabase.table("profiles").select("*").order("created_at", desc=True).execute()
    return {"users": users.data}

@app.put("/api/admin/users/{user_id}/ban")
async def ban_user(user_id: str, admin: dict = Depends(get_admin_user)):
    user = supabase.table("profiles").select("banned").eq("id", user_id).execute()
    if not user.data:
        raise HTTPException(status_code=404, detail="User not found")
    
    new_status = not user.data[0]["banned"]
    supabase.table("profiles").update({"banned": new_status}).eq("id", user_id).execute()
    
    return {"message": f"User {'banned' if new_status else 'unbanned'}"}

@app.get("/api/admin/analytics")
async def get_analytics(admin: dict = Depends(get_admin_user)):
    total_users = supabase.table("profiles").select("*", count="exact").execute()
    total_bets = supabase.table("bets").select("*", count="exact").execute()
    total_volume = supabase.table("bets").select("xcoin_amount").execute()
    total_payout = supabase.table("bets").select("xcoin_payout").execute()
    
    volume_sum = sum(b.get("xcoin_amount", 0) for b in total_volume.data)
    payout_sum = sum(b.get("xcoin_payout", 0) for b in total_payout.data)
    
    return {
        "total_users": total_users.count,
        "total_bets": total_bets.count,
        "total_volume": volume_sum,
        "total_payout": payout_sum,
        "house_edge": ((volume_sum - payout_sum) / volume_sum * 100) if volume_sum > 0 else 0
    }

# ==================== WebSocket Routes ====================

@app.websocket("/ws/{token}")
async def websocket_endpoint(websocket: WebSocket, token: str):
    try:
        payload = verify_token(token)
        user_id = payload.get("sub")
        
        await manager.connect(websocket, user_id)
        
        # Get user info for chat
        user_response = supabase.table("profiles").select("username").eq("id", user_id).execute()
        username = user_response.data[0]["username"] if user_response.data else "User"
        
        while True:
            data = await websocket.receive_json()
            
            if data.get("type") == "chat":
                message = data.get("message", "")[:500]
                
                # Save to database
                supabase.table("chat_messages").insert({
                    "user_id": user_id,
                    "username": username,
                    "room": data.get("room", "global"),
                    "message": message
                }).execute()
                
                # Broadcast to all
                await manager.broadcast({
                    "type": "chat",
                    "user_id": user_id,
                    "username": username,
                    "message": message,
                    "room": data.get("room", "global"),
                    "timestamp": datetime.utcnow().isoformat()
                })
            
            elif data.get("type") == "crash_bet":
                bet_amount = data.get("amount", 0)
                auto_cashout = data.get("auto_cashout")
                
                success = await manager.place_crash_bet(user_id, bet_amount, auto_cashout)
                if success:
                    await manager.broadcast({
                        "type": "crash_bet_placed",
                        "user_id": user_id,
                        "username": username,
                        "bet": bet_amount
                    })
                else:
                    await manager.send_message(user_id, {"type": "error", "message": "Cannot place bet"})
            
            elif data.get("type") == "crash_cashout":
                win = await manager.cashout_crash(user_id)
                if win:
                    await manager.broadcast({
                        "type": "crash_cashout",
                        "user_id": user_id,
                        "username": username,
                        "win": round(win, 2)
                    })
    
    except WebSocketDisconnect:
        manager.disconnect(user_id)
    except Exception as e:
        print(f"WebSocket error: {e}")

# ==================== Health Check ====================

@app.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}

# ==================== Run Server ====================

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 5000))
    uvicorn.run(app, host="0.0.0.0", port=port)
