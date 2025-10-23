from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
import asyncio
from typing import Dict, List
from datetime import datetime
import json
import os

import asyncio
from typing import Dict, List
from datetime import datetime
import json
import os

# Simple signaling server - star topology with host relay
rooms: Dict[str, List[WebSocket]] = {}
room_lock = asyncio.Lock()
server_start_time = datetime.now()
connection_log: List[str] = []

def log_event(message: str):
    timestamp = datetime.now().strftime("%H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    print(log_entry)
    connection_log.append(log_entry)
    if len(connection_log) > 50:
        connection_log.pop(0)

async def add_client_to_room(room_id: str, websocket: WebSocket) -> bool:
    """Add client to room."""
    async with room_lock:
        if room_id not in rooms:
            rooms[room_id] = []
        
        if len(rooms[room_id]) >= 4:
            log_event(f"Room {room_id} is full ({len(rooms[room_id])}/4)")
            return False
        
        rooms[room_id].append(websocket)
        log_event(f"✅ Client joined room {room_id} ({len(rooms[room_id])}/4)")
        return True

async def remove_client_from_room(room_id: str, websocket: WebSocket):
    """Remove client from room."""
    async with room_lock:
        if room_id in rooms and websocket in rooms[room_id]:
            rooms[room_id].remove(websocket)
            log_event(f"❌ Client left room {room_id} ({len(rooms[room_id])}/4)")
            
            # Notify others
            if rooms[room_id]:
                msg = json.dumps({"type": "peer_disconnected"})
                for client in rooms[room_id]:
                    try:
                        await client.send_text(msg)
                    except:
                        pass
            
            # Cleanup empty room
            if not rooms[room_id]:
                del rooms[room_id]
                log_event(f"🧹 Room {room_id} deleted")

async def relay_message(room_id: str, message: str, sender: WebSocket):
    """Relay signaling messages between peers."""
    async with room_lock:
        if room_id in rooms:
            for client in rooms[room_id]:
                if client != sender:
                    try:
                        await client.send_text(message)
                    except:
                        pass


# FastAPI app
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def root():
    uptime = datetime.now() - server_start_time
    hours = int(uptime.total_seconds() // 3600)
    minutes = int((uptime.total_seconds() % 3600) // 60)
    
    room_info = ""
    if rooms:
        room_info = "<h3>Active Rooms:</h3><ul>"
        for room_id, clients in rooms.items():
            room_info += f"<li>Room <code>{room_id}</code>: {len(clients)}/4 players</li>"
        room_info += "</ul>"
    else:
        room_info = "<p><em>No active rooms</em></p>"
    
    log_info = "<h3>Recent Events:</h3><ul>"
    for log in reversed(connection_log[-15:]):
        log_info += f"<li><code>{log}</code></li>"
    log_info += "</ul>"
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>WebRTC Signaling Server (Star Topology)</title>
        <style>
            body {{ font-family: Arial, sans-serif; max-width: 900px; margin: 0 auto; padding: 20px; background: #0f172a; color: #e2e8f0; }}
            h1 {{ color: #60a5fa; }}
            code {{ background: #1e293b; padding: 2px 6px; border-radius: 4px; color: #fbbf24; }}
            .status {{ display: inline-block; padding: 8px 16px; background: #065f46; border-radius: 20px; color: #d1fae5; margin: 10px 0; }}
            ul {{ background: #1e293b; padding: 15px 30px; border-radius: 8px; }}
        </style>
        <script>setTimeout(() => location.reload(), 3000);</script>
    </head>
    <body>
        <h1>🎮 WebRTC Signaling Server</h1>
        <div class="status">🟢 Server Online | Uptime: {hours}h {minutes}m</div>
        
        <h2>📊 Server Status</h2>
        <p><strong>Active Rooms:</strong> {len(rooms)}</p>
        <p><strong>Total Connections:</strong> {sum(len(c) for c in rooms.values())}</p>
        {room_info}
        {log_info}
        
        <h2>ℹ️ Architecture</h2>
        <p><strong>Star Topology:</strong> Host (peer 1) relays all game data. Server only handles WebRTC signaling.</p>
        <p><strong>WebSocket URL:</strong> <code>wss://web-services-nheh.onrender.com/ws/{{room_id}}/</code></p>
    </body>
    </html>
    """
    return HTMLResponse(content=html)

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "uptime_seconds": (datetime.now() - server_start_time).total_seconds(),
        "active_rooms": len(rooms),
        "total_connections": sum(len(c) for c in rooms.values())
    }

@app.websocket("/ws/{room_id}/")
async def websocket_endpoint(websocket: WebSocket, room_id: str):
    """WebSocket endpoint for WebRTC signaling only."""
    peer_id = None
    try:
        await websocket.accept()
        log_event(f"🔌 Connection to room {room_id}")
        
        peer_id = await add_client_to_room(room_id, websocket)
        if peer_id is None:
            await websocket.send_text(json.dumps({"type": "error", "message": "Room full"}))
            await websocket.close()
            return
        
        # Send welcome with peer ID
        await websocket.send_text(json.dumps({
            "type": "welcome",
            "peer_id": peer_id,
            "room_id": room_id,
            "is_host": peer_id == 1
        }))
        log_event(f"🎉 Peer {peer_id} welcomed (host={peer_id == 1})")
        
        # Message loop - relay signaling only
        while True:
            message = await websocket.receive_text()
            await relay_message(room_id, message, websocket)
    
    except WebSocketDisconnect:
        log_event(f"🔌 Peer {peer_id} disconnected")
    except Exception as e:
        log_event(f"❌ Error for peer {peer_id}: {e}")
    finally:
        if peer_id:
            await remove_client_from_room(room_id, websocket)

log_event("🚀 Star topology signaling server ready")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
