import os
import secrets
import hashlib
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Depends, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr
from supabase import create_client, Client

# ==================== Configuration ====================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))
HOUSE_EDGE = float(os.getenv("HOUSE_EDGE", "0.01"))
FRONTEND_URL = os.getenv("FRONTEND_URL")

# Admin credentials
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
    return response.data[0]

# ==================== Game Logic ====================
def generate_server_seed() -> str:
    return secrets.token_hex(32)

def get_random_number(server_seed: str, client_seed: str, nonce: int) -> float:
    combined = f"{server_seed}:{client_seed}:{nonce}"
    hash_value = hashlib.sha256(combined.encode()).hexdigest()
    return int(hash_value[:8], 16) / 0xffffffff

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
    return {"roll": round(roll, 2), "is_win": is_win, "win_amount": win_amount, "multiplier": multiplier}

# ==================== FastAPI App ====================
app = FastAPI(title="XBet Casino API")
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
        "role": "user", "xcoin_balance": 100.00, "client_seed": secrets.token_hex(16), "nonce": 0
    }
    supabase.table("profiles").insert(profile_data).execute()
    token = create_access_token({"sub": auth_resp.user.id})
    return {"token": token, "user": profile_data}

@app.post("/api/auth/login")
async def login(user_data: UserLogin):
    auth_resp = supabase.auth.sign_in_with_password({"email": user_data.email, "password": user_data.password})
    if not auth_resp.user:
        raise HTTPException(401, "Invalid credentials")
    
    profile = supabase.table("profiles").select("*").eq("id", auth_resp.user.id).execute()
    if not profile.data:
        raise HTTPException(401, "Profile not found")
    
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
    
    server_seed = generate_server_seed()
    client_seed = user["client_seed"]
    nonce = user["nonce"] + 1
    
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
    
    target = bet.params.get("target", 50)
    condition = bet.params.get("condition", "under")
    
    server_seed = generate_server_seed()
    client_seed = user["client_seed"]
    nonce = user["nonce"] + 1
    
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
    return {"xcoin_balance": user["xcoin_balance"], "role": user["role"], "username": user["username"]}

# ==================== Admin Routes ====================
@app.get("/api/admin/users")
async def get_all_users(admin: dict = Depends(get_current_user)):
    if admin["role"] != "admin":
        raise HTTPException(403, "Admin access required")
    users = supabase.table("profiles").select("*").execute()
    return {"users": users.data}

@app.put("/api/admin/users/{user_id}/ban")
async def ban_user(user_id: str, admin: dict = Depends(get_current_user)):
    if admin["role"] != "admin":
        raise HTTPException(403, "Admin access required")
    user = supabase.table("profiles").select("banned").eq("id", user_id).execute()
    if not user.data:
        raise HTTPException(404, "User not found")
    new_status = not user.data[0]["banned"]
    supabase.table("profiles").update({"banned": new_status}).eq("id", user_id).execute()
    return {"message": f"User {'banned' if new_status else 'unbanned'}"}

@app.get("/api/admin/analytics")
async def get_analytics(admin: dict = Depends(get_current_user)):
    if admin["role"] != "admin":
        raise HTTPException(403, "Admin access required")
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

# ==================== WebSocket ====================
class ConnectionManager:
    def __init__(self):
        self.active: Dict[str, WebSocket] = {}
        self.crash_state = {"active": False, "multiplier": 1.0, "crash_point": 0, "players": {}}
    
    async def connect(self, ws: WebSocket, user_id: str):
        await ws.accept()
        self.active[user_id] = ws
    
    def disconnect(self, user_id: str):
        self.active.pop(user_id, None)
    
    async def broadcast(self, message: dict):
        for ws in self.active.values():
            try:
                await ws.send_json(message)
            except:
                pass

manager = ConnectionManager()

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
    except WebSocketDisconnect:
        manager.disconnect(user_id)

@app.get("/api/leaderboard")
async def get_leaderboard():
    return {
        "biggest_win": {"username": "Player1", "value": 5000},
        "most_games": {"username": "Player2", "value": 150},
        "total_wagered": {"username": "Player3", "value": 25000}
    }

@app.get("/api/games/crash/start")
async def start_crash():
    return {"message": "Game started"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
