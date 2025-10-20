from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
import asyncio
from typing import Dict, List
from datetime import datetime
import json
import os

# Room management
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
    async with room_lock:
        if room_id not in rooms:
            rooms[room_id] = []
        
        if len(rooms[room_id]) >= 2:
            log_event(f"Room {room_id} is full")
            return False
        
        rooms[room_id].append(websocket)
        log_event(f"✅ Client added to room {room_id} ({len(rooms[room_id])}/2)")
        return True

async def remove_client_from_room(room_id: str, websocket: WebSocket):
    async with room_lock:
        if room_id in rooms and websocket in rooms[room_id]:
            rooms[room_id].remove(websocket)
            log_event(f"❌ Client removed from room {room_id} ({len(rooms[room_id])}/2 remaining)")
            if len(rooms[room_id]) == 0:
                del rooms[room_id]
                log_event(f"🧹 Room {room_id} cleaned up")

async def relay_message(room_id: str, message: str, sender: WebSocket):
    async with room_lock:
        if room_id in rooms:
            for client in rooms[room_id]:
                if client != sender:
                    try:
                        await client.send_text(message)
                        msg_data = json.loads(message)
                        log_event(f"📤 Relayed {msg_data.get('type', 'msg')} in room {room_id}")
                    except Exception as e:
                        log_event(f"⚠️ Relay error: {e}")

# Create FastAPI app
app = FastAPI()

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def root():
    """Home page with server info."""
    uptime = datetime.now() - server_start_time
    hours = int(uptime.total_seconds() // 3600)
    minutes = int((uptime.total_seconds() % 3600) // 60)
    
    room_info = ""
    if rooms:
        room_info = "<h3>Active Rooms:</h3><ul>"
        for room_id, clients in rooms.items():
            room_info += f"<li>Room <code>{room_id}</code>: {len(clients)}/2 players</li>"
        room_info += "</ul>"
    else:
        room_info = "<p><em>No active rooms</em></p>"
    
    log_info = "<h3>Recent Events:</h3><ul>"
    for log in reversed(connection_log[-10:]):
        log_info += f"<li><code>{log}</code></li>"
    log_info += "</ul>"
    
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>WebRTC Signaling Server</title>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                max-width: 900px;
                margin: 0 auto;
                padding: 20px;
                background: #0f172a;
                color: #e2e8f0;
            }}
            h1 {{ color: #60a5fa; }}
            h2 {{ color: #34d399; margin-top: 30px; }}
            h3 {{ color: #a78bfa; }}
            code {{
                background: #1e293b;
                padding: 2px 6px;
                border-radius: 4px;
                color: #fbbf24;
            }}
            pre {{
                background: #1e293b;
                padding: 15px;
                border-radius: 8px;
                overflow-x: auto;
                border-left: 4px solid #60a5fa;
            }}
            .status {{
                display: inline-block;
                padding: 8px 16px;
                background: #065f46;
                border-radius: 20px;
                color: #d1fae5;
                margin: 10px 0;
            }}
            ul {{
                background: #1e293b;
                padding: 15px 30px;
                border-radius: 8px;
            }}
            li {{ margin: 8px 0; }}
            .endpoint {{
                background: #1e293b;
                padding: 15px;
                border-radius: 8px;
                border-left: 4px solid #34d399;
                margin: 15px 0;
            }}
        </style>
        <script>
            // Auto-refresh every 3 seconds
            setTimeout(() => location.reload(), 3000);
        </script>
    </head>
    <body>
        <h1>🎮 WebRTC Signaling Server</h1>
        <div class="status">🟢 Server Online | Uptime: {hours}h {minutes}m</div>
        
        <h2>📡 Connection Information</h2>
        <div class="endpoint">
            <strong>WebSocket Endpoint:</strong><br>
            <code>wss://[YOUR-RENDER-URL].onrender.com/ws/{{room_id}}/</code>
        </div>
        
        <h2>🔧 Godot Usage</h2>
        <pre><code>var signaling_url = "wss://[YOUR-RENDER-URL].onrender.com/ws/"
var room_id = "test123"
websocket_peer.connect_to_url(signaling_url + room_id + "/")</code></pre>
        
        <h2>📊 Server Status</h2>
        <p><strong>Active Rooms:</strong> {len(rooms)}</p>
        {room_info}
        
        {log_info}
        
        <p style="margin-top: 30px; color: #64748b; font-size: 0.9em;">
            <em>Page auto-refreshes every 3 seconds</em>
        </p>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

@app.get("/health")
async def health_check():
    """Health check endpoint for Render."""
    return {
        "status": "healthy",
        "uptime_seconds": (datetime.now() - server_start_time).total_seconds(),
        "active_rooms": len(rooms),
        "total_connections": sum(len(clients) for clients in rooms.values())
    }

@app.websocket("/ws/{room_id}/")
async def websocket_endpoint(websocket: WebSocket, room_id: str):
    """WebSocket endpoint for signaling."""
    log_event(f"🔌 Connection attempt for room: {room_id}")
    
    try:
        await websocket.accept()
        log_event(f"✅ WebSocket accepted for room: {room_id}")
    except Exception as e:
        log_event(f"❌ Error accepting WebSocket: {e}")
        return
    
    if not await add_client_to_room(room_id, websocket):
        try:
            await websocket.send_text('{"error": "Room is full"}')
            await websocket.close()
        except:
            pass
        return
    
    try:
        while True:
            message = await websocket.receive_text()
            await relay_message(room_id, message, websocket)
    except WebSocketDisconnect:
        log_event(f"🔌 Client disconnected from room: {room_id}")
    except Exception as e:
        log_event(f"⚠️ WebSocket error in room {room_id}: {e}")
    finally:
        await remove_client_from_room(room_id, websocket)

log_event("🚀 Server initialized and ready")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)