from fastapi import FastAPI, HTTPException, Depends, status, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
import jwt
import bcrypt
import secrets
import hashlib
import hmac
import random
import json
import asyncio
from contextlib import asynccontextmanager

# ==================== Configuration ====================

SECRET_KEY = "xbet-super-secret-key-2024-change-this-in-production"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 24 hours
HOUSE_EDGE = 0.01  # 1%

# In-memory storage (replace with database in production)
users_db = {}
bets_db = {}
transactions_db = {}
chat_messages = {}
active_connections = {}
crash_game_state = {
    "active": False,
    "multiplier": 1.0,
    "crash_point": 0,
    "players": {},
    "bets": {}
}

# ==================== Models ====================

class UserCreate(BaseModel):
    email: EmailStr
    password: str

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class UserResponse(BaseModel):
    id: str
    email: str
    role: str
    xcoin_balance: float

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

class WithdrawRequest(BaseModel):
    xcoin_amount: float
    address: str

# ==================== Security ====================

security = HTTPBearer()

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
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    payload = verify_token(token)
    user = users_db.get(payload.get("sub"))
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
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
    
    paylines = [
        [0, 1, 2], [3, 4, 5], [6, 7, 8],  # rows
        [0, 4, 8], [2, 4, 6]  # diagonals
    ]
    
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
        "reels": reels,
        "winning_lines": winning_lines,
        "win_amount": total_win,
        "is_win": total_win > 0
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

# ==================== FastAPI App ====================

app = FastAPI(title="XBet Casino API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==================== WebSocket Manager ====================

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
    
    async def connect(self, websocket: WebSocket, user_id: str):
        await websocket.accept()
        self.active_connections[user_id] = websocket
    
    def disconnect(self, user_id: str):
        if user_id in self.active_connections:
            del self.active_connections[user_id]
    
    async def send_message(self, user_id: str, message: dict):
        if user_id in self.active_connections:
            await self.active_connections[user_id].send_json(message)
    
    async def broadcast(self, message: dict, room: str = "global"):
        # In production, implement room-based broadcasting
        for user_id, ws in self.active_connections.items():
            try:
                await ws.send_json(message)
            except:
                pass

manager = ConnectionManager()

# ==================== WebSocket Routes ====================

@app.websocket("/ws/{token}")
async def websocket_endpoint(websocket: WebSocket, token: str):
    try:
        payload = verify_token(token)
        user_id = payload.get("sub")
        await manager.connect(websocket, user_id)
        
        while True:
            data = await websocket.receive_json()
            
            if data.get("type") == "chat":
                message = data.get("message", "")[:500]
                chat_message = {
                    "type": "chat",
                    "user_id": user_id,
                    "user_email": users_db[user_id]["email"],
                    "message": message,
                    "room": data.get("room", "global"),
                    "timestamp": datetime.utcnow().isoformat()
                }
                await manager.broadcast(chat_message)
            
            elif data.get("type") == "crash_bet":
                if not crash_game_state["active"]:
                    await manager.send_message(user_id, {"type": "error", "message": "Game not active"})
                    continue
                
                user = users_db[user_id]
                bet_amount = data.get("amount", 0)
                
                if user["xcoin_balance"] < bet_amount:
                    await manager.send_message(user_id, {"type": "error", "message": "Insufficient balance"})
                    continue
                
                user["xcoin_balance"] -= bet_amount
                crash_game_state["players"][user_id] = {
                    "bet": bet_amount,
                    "auto_cashout": data.get("auto_cashout")
                }
                crash_game_state["bets"][user_id] = bet_amount
                
                await manager.broadcast({
                    "type": "crash_bet_placed",
                    "user_id": user_id,
                    "bet": bet_amount,
                    "auto_cashout": data.get("auto_cashout")
                })
            
            elif data.get("type") == "crash_cashout":
                if user_id in crash_game_state["players"]:
                    player = crash_game_state["players"][user_id]
                    win = player["bet"] * crash_game_state["multiplier"]
                    users_db[user_id]["xcoin_balance"] += win
                    
                    del crash_game_state["players"][user_id]
                    
                    await manager.broadcast({
                        "type": "crash_cashout",
                        "user_id": user_id,
                        "win": win
                    })
    
    except WebSocketDisconnect:
        manager.disconnect(user_id)

# ==================== Auth Routes ====================

@app.post("/api/auth/register", response_model=UserResponse)
async def register(user_data: UserCreate):
    if user_data.email in users_db:
        raise HTTPException(status_code=400, detail="Email already registered")
    
    user_id = secrets.token_urlsafe(16)
    server_seed = generate_server_seed()
    
    users_db[user_data.email] = {
        "id": user_id,
        "email": user_data.email,
        "password_hash": hash_password(user_data.password),
        "role": "user",
        "banned": False,
        "xcoin_balance": 100.0,  # Welcome bonus
        "server_seed": hash_server_seed(server_seed),
        "client_seed": "xbet_default_seed",
        "nonce": 0,
        "created_at": datetime.utcnow().isoformat()
    }
    
    # Store server seed separately for revealing
    users_db[f"{user_data.email}_seed"] = server_seed
    
    token = create_access_token({"sub": user_data.email})
    
    return UserResponse(
        id=user_id,
        email=user_data.email,
        role="user",
        xcoin_balance=100.0
    )

@app.post("/api/auth/login")
async def login(user_data: UserLogin):
    user = users_db.get(user_data.email)
    if not user or not verify_password(user_data.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    if user.get("banned"):
        raise HTTPException(status_code=403, detail="Account banned")
    
    token = create_access_token({"sub": user_data.email})
    
    return {
        "token": token,
        "user": UserResponse(
            id=user["id"],
            email=user["email"],
            role=user["role"],
            xcoin_balance=user["xcoin_balance"]
        )
    }

@app.post("/api/auth/logout")
async def logout(user: dict = Depends(get_current_user)):
    return {"message": "Logged out"}

# ==================== Game Routes ====================

@app.post("/api/games/slots/play")
async def play_slots_game(bet: BetRequest, user: dict = Depends(get_current_user)):
    if bet.xcoin_amount <= 0 or bet.xcoin_amount > user["xcoin_balance"]:
        raise HTTPException(status_code=400, detail="Invalid bet amount")
    
    user_data = users_db[user["email"]]
    server_seed = users_db.get(f"{user['email']}_seed", generate_server_seed())
    client_seed = user_data["client_seed"]
    nonce = user_data["nonce"] + 1
    
    result = play_slots(server_seed, client_seed, nonce, bet.xcoin_amount)
    
    # Update balance
    if result["is_win"]:
        user_data["xcoin_balance"] += result["win_amount"]
    else:
        user_data["xcoin_balance"] -= bet.xcoin_amount
    
    user_data["nonce"] = nonce
    users_db[user["email"]] = user_data
    
    # Store bet
    bet_id = f"bet_{betIdCounter}"
    bets_db[bet_id] = {
        "id": bet_id,
        "user_id": user["id"],
        "game": "slots",
        "bet": bet.xcoin_amount,
        "win": result["win_amount"],
        "result": result,
        "timestamp": datetime.utcnow().isoformat()
    }
    global betIdCounter
    betIdCounter += 1
    
    return BetResponse(
        bet_id=bet_id,
        outcome="win" if result["is_win"] else "loss",
        win_amount=result["win_amount"],
        result=result,
        new_balance=user_data["xcoin_balance"]
    )

@app.post("/api/games/dice/play")
async def play_dice_game(bet: BetRequest, user: dict = Depends(get_current_user)):
    if bet.xcoin_amount <= 0 or bet.xcoin_amount > user["xcoin_balance"]:
        raise HTTPException(status_code=400, detail="Invalid bet amount")
    
    target = bet.params.get("target", 50)
    condition = bet.params.get("condition", "under")
    
    if target < 1 or target > 99:
        raise HTTPException(status_code=400, detail="Target must be between 1 and 99")
    
    user_data = users_db[user["email"]]
    server_seed = users_db.get(f"{user['email']}_seed", generate_server_seed())
    client_seed = user_data["client_seed"]
    nonce = user_data["nonce"] + 1
    
    result = play_dice(server_seed, client_seed, nonce, bet.xcoin_amount, target, condition)
    
    if result["is_win"]:
        user_data["xcoin_balance"] += result["win_amount"]
    else:
        user_data["xcoin_balance"] -= bet.xcoin_amount
    
    user_data["nonce"] = nonce
    users_db[user["email"]] = user_data
    
    bet_id = f"bet_{betIdCounter}"
    bets_db[bet_id] = {
        "id": bet_id,
        "user_id": user["id"],
        "game": "dice",
        "bet": bet.xcoin_amount,
        "win": result["win_amount"],
        "result": result,
        "timestamp": datetime.utcnow().isoformat()
    }
    betIdCounter += 1
    
    return BetResponse(
        bet_id=bet_id,
        outcome="win" if result["is_win"] else "loss",
        win_amount=result["win_amount"],
        result=result,
        new_balance=user_data["xcoin_balance"]
    )

@app.post("/api/games/crash/start")
async def start_crash_game(admin: dict = Depends(get_admin_user)):
    if crash_game_state["active"]:
        raise HTTPException(status_code=400, detail="Game already active")
    
    server_seed = generate_server_seed()
    client_seed = "xbet_crash_seed"
    nonce = 1
    
    crash_point = generate_crash_point(server_seed, client_seed, nonce)
    
    crash_game_state["active"] = True
    crash_game_state["multiplier"] = 1.0
    crash_game_state["crash_point"] = crash_point
    crash_game_state["players"] = {}
    crash_game_state["bets"] = {}
    crash_game_state["server_seed"] = server_seed
    crash_game_state["client_seed"] = client_seed
    crash_game_state["nonce"] = nonce
    
    # Start multiplier increasing
    async def increase_multiplier():
        while crash_game_state["active"] and crash_game_state["multiplier"] < crash_point:
            await asyncio.sleep(0.1)
            crash_game_state["multiplier"] *= 1.03
            
            await manager.broadcast({
                "type": "crash_multiplier",
                "multiplier": round(crash_game_state["multiplier"], 2)
            })
            
            # Check auto cashouts
            for user_id, player in list(crash_game_state["players"].items()):
                if player.get("auto_cashout") and player["auto_cashout"] <= crash_game_state["multiplier"]:
                    win = player["bet"] * crash_game_state["multiplier"]
                    users_db[user_id]["xcoin_balance"] += win
                    del crash_game_state["players"][user_id]
                    
                    await manager.broadcast({
                        "type": "crash_cashout",
                        "user_id": user_id,
                        "win": win,
                        "auto": True
                    })
        
        # Game crashed
        crash_game_state["active"] = False
        
        # Process remaining players (they lose)
        for user_id in crash_game_state["players"]:
            await manager.send_message(user_id, {
                "type": "crash_crashed",
                "multiplier": round(crash_game_state["multiplier"], 2)
            })
        
        await manager.broadcast({
            "type": "crash_crashed",
            "multiplier": round(crash_game_state["multiplier"], 2),
            "crash_point": crash_point
        })
        
        crash_game_state["players"] = {}
    
    asyncio.create_task(increase_multiplier())
    
    return {"message": "Game started", "crash_point": crash_point}

@app.get("/api/games/crash/state")
async def get_crash_state():
    return {
        "active": crash_game_state["active"],
        "multiplier": round(crash_game_state["multiplier"], 2),
        "players": len(crash_game_state["players"])
    }

# ==================== User Routes ====================

@app.get("/api/user/balance")
async def get_balance(user: dict = Depends(get_current_user)):
    return {"xcoin_balance": user["xcoin_balance"]}

@app.post("/api/user/withdraw")
async def withdraw(withdraw: WithdrawRequest, user: dict = Depends(get_current_user)):
    if withdraw.xcoin_amount <= 0:
        raise HTTPException(status_code=400, detail="Invalid amount")
    
    if withdraw.xcoin_amount > user["xcoin_balance"]:
        raise HTTPException(status_code=400, detail="Insufficient balance")
    
    if withdraw.xcoin_amount < 5000:  # Minimum 5000 XCoin ($50)
        raise HTTPException(status_code=400, detail="Minimum withdrawal is 5000 XCoin")
    
    user["xcoin_balance"] -= withdraw.xcoin_amount
    
    transaction_id = secrets.token_urlsafe(16)
    transactions_db[transaction_id] = {
        "id": transaction_id,
        "user_id": user["id"],
        "type": "withdrawal",
        "amount": withdraw.xcoin_amount,
        "address": withdraw.address,
        "status": "pending",
        "timestamp": datetime.utcnow().isoformat()
    }
    
    return {
        "message": "Withdrawal request submitted",
        "transaction_id": transaction_id,
        "new_balance": user["xcoin_balance"]
    }

@app.get("/api/user/history")
async def get_history(user: dict = Depends(get_current_user)):
    user_bets = [bet for bet in bets_db.values() if bet["user_id"] == user["id"]]
    return {"bets": user_bets[-50:]}

# ==================== Admin Routes ====================

@app.get("/api/admin/users")
async def get_all_users(admin: dict = Depends(get_admin_user)):
    users_list = []
    for email, user in users_db.items():
        if email not in ["_seed"]:
            users_list.append({
                "id": user["id"],
                "email": user["email"],
                "role": user["role"],
                "banned": user["banned"],
                "xcoin_balance": user["xcoin_balance"],
                "created_at": user.get("created_at")
            })
    return {"users": users_list}

@app.put("/api/admin/users/{user_id}/ban")
async def ban_user(user_id: str, admin: dict = Depends(get_admin_user)):
    for email, user in users_db.items():
        if user["id"] == user_id:
            user["banned"] = not user.get("banned", False)
            return {"message": f"User banned: {user['banned']}"}
    
    raise HTTPException(status_code=404, detail="User not found")

@app.get("/api/admin/analytics")
async def get_analytics(admin: dict = Depends(get_admin_user)):
    total_users = len([u for u in users_db.values() if isinstance(u, dict) and "id" in u])
    total_bets = len(bets_db)
    total_volume = sum(bet["bet"] for bet in bets_db.values())
    total_payout = sum(bet["win"] for bet in bets_db.values())
    
    return {
        "total_users": total_users,
        "total_bets": total_bets,
        "total_volume": total_volume,
        "total_payout": total_payout,
        "house_edge": ((total_volume - total_payout) / total_volume * 100) if total_volume > 0 else 0
    }

# ==================== Health Check ====================

@app.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}

# ==================== Run Server ====================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)
