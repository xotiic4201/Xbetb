import os
import secrets
import hashlib
import json
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Depends, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr
from supabase import create_client, Client

# ==================== Configuration ====================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
SECRET_KEY = os.getenv("SECRET_KEY", "xbet-super-secret-key-2024")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))
HOUSE_EDGE = float(os.getenv("HOUSE_EDGE", "0.01"))
FRONTEND_URL = os.getenv("FRONTEND_URL")

# Admin credentials from Render env
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# ==================== Models ====================
class UserCreate(BaseModel):
    email: EmailStr
    password: str
    username: Optional[str] = None

class UserLogin(BaseModel):
    email: EmailStr
    password: str

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

# ==================== Security ====================
security = HTTPBearer()

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
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

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

# ==================== Provably Fair ====================
def generate_server_seed() -> str:
    return secrets.token_hex(32)

def hash_server_seed(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()

def get_random_number(server_seed: str, client_seed: str, nonce: int) -> float:
    combined = f"{server_seed}:{client_seed}:{nonce}"
    hash_value = hashlib.sha256(combined.encode()).hexdigest()
    return int(hash_value[:8], 16) / 0xffffffff

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
        r = get_random_number(server_seed, client_seed, nonce + pos) * 100
        cumulative = 0
        for sym in symbols:
            cumulative += sym["frequency"]
            if r < cumulative:
                return sym
        return symbols[0]
    
    reels = [get_symbol(i) for i in range(9)]
    total_win = 0
    winning_lines = []
    
    for line in paylines:
        line_symbols = [reels[idx] for idx in line]
        if all(s["id"] == line_symbols[0]["id"] for s in line_symbols):
            win = bet_amount * line_symbols[0]["payout"]
            total_win += win
            winning_lines.append(line)
    
    return {
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
    return {"roll": round(roll, 2), "is_win": is_win, "win_amount": win_amount, "multiplier": multiplier}

# ==================== WebSocket Manager ====================
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.crash_state = {"active": False, "multiplier": 1.0, "crash_point": 0, "players": {}, "server_seed": None, "client_seed": None, "nonce": 0}
        self.crash_task = None
    
    async def connect(self, websocket: WebSocket, user_id: str):
        await websocket.accept()
        self.active_connections[user_id] = websocket
    
    def disconnect(self, user_id: str):
        self.active_connections.pop(user_id, None)
    
    async def broadcast(self, message: dict):
        for ws in self.active_connections.values():
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
            "players": {}
        })
        
        r = get_random_number(self.crash_state["server_seed"], self.crash_state["client_seed"], self.crash_state["nonce"])
        self.crash_state["crash_point"] = max(1.00, 1.00 / (1.00 - r + HOUSE_EDGE))
        
        await self.broadcast({"type": "crash_start", "crash_point": self.crash_state["crash_point"]})
        
        async def run():
            while self.crash_state["active"] and self.crash_state["multiplier"] < self.crash_state["crash_point"]:
                await asyncio.sleep(0.1)
                self.crash_state["multiplier"] *= 1.03
                await self.broadcast({"type": "crash_multiplier", "multiplier": round(self.crash_state["multiplier"], 2)})
                
                for uid, player in list(self.crash_state["players"].items()):
                    if player.get("auto") and player["auto"] <= self.crash_state["multiplier"]:
                        win = player["bet"] * self.crash_state["multiplier"]
                        supabase.table("profiles").update({"xcoin_balance": supabase.rpc("increment", {"x": win})}).eq("id", uid).execute()
                        del self.crash_state["players"][uid]
                        await self.broadcast({"type": "crash_cashout", "user_id": uid, "win": round(win, 2), "auto": True})
            
            self.crash_state["active"] = False
            await self.broadcast({"type": "crash_crashed", "multiplier": round(self.crash_state["multiplier"], 2), "crash_point": self.crash_state["crash_point"]})
            self.crash_state["players"] = {}
            await asyncio.sleep(5)
            asyncio.create_task(self.start_crash_game())
        
        self.crash_task = asyncio.create_task(run())
    
    async def place_bet(self, user_id: str, amount: float, auto: Optional[float] = None):
        if not self.crash_state["active"]:
            return False
        user = supabase.table("profiles").select("xcoin_balance").eq("id", user_id).execute()
        if user.data and user.data[0]["xcoin_balance"] >= amount:
            supabase.table("profiles").update({"xcoin_balance": user.data[0]["xcoin_balance"] - amount}).eq("id", user_id).execute()
            self.crash_state["players"][user_id] = {"bet": amount, "auto": auto}
            return True
        return False
    
    async def cashout(self, user_id: str) -> Optional[float]:
        if user_id not in self.crash_state["players"]:
            return None
        player = self.crash_state["players"][user_id]
        win = player["bet"] * self.crash_state["multiplier"]
        supabase.table("profiles").update({"xcoin_balance": supabase.rpc("increment", {"x": win})}).eq("id", user_id).execute()
        del self.crash_state["players"][user_id]
        return win

manager = ConnectionManager()

# ==================== FastAPI App ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(manager.start_crash_game())
    yield

app = FastAPI(title="XBet Casino API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=[FRONTEND_URL, "http://localhost:3000"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# ==================== Auth Routes ====================
@app.post("/api/auth/register")
async def register(user_data: UserCreate):
    existing = supabase.table("profiles").select("*").eq("email", user_data.email).execute()
    if existing.data:
        raise HTTPException(400, "Email already registered")
    
    auth_resp = supabase.auth.sign_up({"email": user_data.email, "password": user_data.password})
    if not auth_resp.user:
        raise HTTPException(400, "Registration failed")
    
    profile_data = {
        "id": auth_resp.user.id, "email": user_data.email, "username": user_data.username or user_data.email.split("@")[0],
        "role": "user", "xcoin_balance": 100.00, "client_seed": secrets.token_hex(16)
    }
    supabase.table("profiles").insert(profile_data).execute()
    token = create_access_token({"sub": auth_resp.user.id})
    return {"token": token, "user": {**profile_data, "xbet_points": 0}}

@app.post("/api/auth/login")
async def login(user_data: UserLogin):
    auth_resp = supabase.auth.sign_in_with_password({"email": user_data.email, "password": user_data.password})
    if not auth_resp.user:
        raise HTTPException(401, "Invalid credentials")
    
    profile = supabase.table("profiles").select("*").eq("id", auth_resp.user.id).execute()
    if not profile.data or profile.data[0].get("banned"):
        raise HTTPException(403, "Account banned")
    
    token = create_access_token({"sub": auth_resp.user.id})
    return {"token": token, "user": profile.data[0]}

@app.post("/api/auth/logout")
async def logout():
    return {"message": "Logged out"}

# ==================== Game Routes ====================
@app.post("/api/games/slots/play")
async def play_slots(bet: BetRequest, user: dict = Depends(get_current_user)):
    if bet.xcoin_amount <= 0 or bet.xcoin_amount > user["xcoin_balance"]:
        raise HTTPException(400, "Invalid bet amount")
    
    server_seed, client_seed, nonce = generate_server_seed(), user["client_seed"], user["nonce"] + 1
    result = play_slots(server_seed, client_seed, nonce, bet.xcoin_amount)
    
    new_balance = user["xcoin_balance"] - bet.xcoin_amount + result["win_amount"]
    supabase.table("profiles").update({"xcoin_balance": new_balance, "nonce": nonce}).eq("id", user["id"]).execute()
    supabase.table("bets").insert({
        "user_id": user["id"], "game_slug": "slots", "xcoin_amount": bet.xcoin_amount,
        "multiplier": result["multiplier"], "outcome": "win" if result["is_win"] else "loss",
        "xcoin_payout": result["win_amount"], "server_seed": server_seed, "client_seed": client_seed, "nonce": nonce, "result": result
    }).execute()
    
    return BetResponse(bet_id="", outcome="win" if result["is_win"] else "loss", win_amount=result["win_amount"], result=result, new_balance=new_balance, multiplier=result["multiplier"])

@app.post("/api/games/dice/play")
async def play_dice(bet: BetRequest, user: dict = Depends(get_current_user)):
    if bet.xcoin_amount <= 0 or bet.xcoin_amount > user["xcoin_balance"]:
        raise HTTPException(400, "Invalid bet amount")
    
    target, condition = bet.params.get("target", 50), bet.params.get("condition", "under")
    if target < 1 or target > 99:
        raise HTTPException(400, "Target must be between 1 and 99")
    
    server_seed, client_seed, nonce = generate_server_seed(), user["client_seed"], user["nonce"] + 1
    result = play_dice(server_seed, client_seed, nonce, bet.xcoin_amount, target, condition)
    
    new_balance = user["xcoin_balance"] - bet.xcoin_amount + result["win_amount"]
    supabase.table("profiles").update({"xcoin_balance": new_balance, "nonce": nonce}).eq("id", user["id"]).execute()
    supabase.table("bets").insert({
        "user_id": user["id"], "game_slug": "dice", "xcoin_amount": bet.xcoin_amount,
        "multiplier": result["multiplier"], "outcome": "win" if result["is_win"] else "loss",
        "xcoin_payout": result["win_amount"], "server_seed": server_seed, "client_seed": client_seed, "nonce": nonce, "result": result
    }).execute()
    
    return BetResponse(bet_id="", outcome="win" if result["is_win"] else "loss", win_amount=result["win_amount"], result=result, new_balance=new_balance, multiplier=result["multiplier"])

@app.get("/api/user/balance")
async def get_balance(user: dict = Depends(get_current_user)):
    return {"xcoin_balance": user["xcoin_balance"]}

@app.post("/api/games/crash/start")
async def start_crash():
    asyncio.create_task(manager.start_crash_game())
    return {"message": "Game started"}

# ==================== WebSocket ====================
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
                supabase.table("chat_messages").insert({"user_id": user_id, "username": username, "message": data["message"]}).execute()
                await manager.broadcast({"type": "chat", "username": username, "message": data["message"]})
            elif data.get("type") == "crash_bet":
                if await manager.place_bet(user_id, data["amount"], data.get("auto_cashout")):
                    await manager.broadcast({"type": "crash_bet_placed", "username": username, "bet": data["amount"]})
            elif data.get("type") == "crash_cashout":
                win = await manager.cashout(user_id)
                if win:
                    await manager.broadcast({"type": "crash_cashout", "username": username, "win": round(win, 2)})
    except WebSocketDisconnect:
        manager.disconnect(user_id)

# ==================== Run ====================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
