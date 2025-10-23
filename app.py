from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
import asyncio
from typing import Dict, List
from datetime import datetime
import json
import os

# Simple signaling server - star topology with host relay
rooms: Dict[str, List[WebSocket]] = {}
room_peer_ids: Dict[str, Dict[int, WebSocket]] = {}
room_host: Dict[str, int] = {}
next_peer_id: Dict[str, int] = {}
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

async def add_client_to_room(room_id: str, websocket: WebSocket) -> (int, bool):
    """Add client to room and return peer ID and is_host status."""
    async with room_lock:
        log_event(f"ADD_CLIENT: Entering for room {room_id}. Current rooms: {list(rooms.keys())}")
        is_host = False
        if room_id not in rooms:
            log_event(f"ADD_CLIENT: Room {room_id} is new. Creating it.")
            is_host = True
            rooms[room_id] = []
            room_peer_ids[room_id] = {}
            next_peer_id[room_id] = 2
        else:
            log_event(f"ADD_CLIENT: Room {room_id} already exists.")
        
        if len(rooms[room_id]) >= 4:
            log_event(f"Room {room_id} is full ({len(rooms[room_id])}/4)")
            return None, False
        
        peer_id = 1 if is_host else next_peer_id[room_id]
        log_event(f"ADD_CLIENT: is_host={is_host}, assigned peer_id={peer_id}")
        if not is_host:
            next_peer_id[room_id] += 1
        
        if is_host:
            room_host[room_id] = peer_id

        # Notify existing peers
        if not is_host:
            msg = json.dumps({"type": "peer_joined", "peer_id": peer_id})
            for client in rooms[room_id]:
                try:
                    await client.send_text(msg)
                except:
                    pass

        rooms[room_id].append(websocket)
        room_peer_ids[room_id][peer_id] = websocket
        
        log_event(f"✅ Peer {peer_id} joined room {room_id} ({len(rooms[room_id])}/4)")
        return peer_id, is_host

async def remove_client_from_room(room_id: str, websocket: WebSocket):
    """Remove client from room."""
    async with room_lock:
        if room_id in rooms and websocket in rooms[room_id]:
            peer_id_to_remove = None
            for peer_id, ws in room_peer_ids[room_id].items():
                if ws == websocket:
                    peer_id_to_remove = peer_id
                    break
            
            if peer_id_to_remove:
                is_host_disconnecting = room_host.get(room_id) == peer_id_to_remove

                rooms[room_id].remove(websocket)
                del room_peer_ids[room_id][peer_id_to_remove]
                log_event(f"❌ Peer {peer_id_to_remove} left room {room_id} ({len(rooms[room_id])}/4)")
                
                if is_host_disconnecting:
                    log_event(f"Host {peer_id_to_remove} disconnected from room {room_id}. Closing room.")
                    for client in rooms[room_id]:
                        await client.send_text(json.dumps({"type": "error", "message": "Host disconnected"}))
                        await client.close()
                    del rooms[room_id]
                    del room_peer_ids[room_id]
                    del next_peer_id[room_id]
                    del room_host[room_id]
                    log_event(f"🧹 Room {room_id} deleted")
                else:
                    # Notify others
                    if rooms[room_id]:
                        msg = json.dumps({"type": "peer_disconnected", "peer_id": peer_id_to_remove})
                        for client in rooms[room_id]:
                            try:
                                await client.send_text(msg)
                            except:
                                pass
            
            # Cleanup empty room
            if room_id in rooms and not rooms[room_id]:
                del rooms[room_id]
                del room_peer_ids[room_id]
                del next_peer_id[room_id]
                if room_id in room_host:
                    del room_host[room_id]
                log_event(f"🧹 Room {room_id} deleted")

async def relay_message(room_id: str, message: str, sender: WebSocket):
    """Relay signaling messages between peers."""
    async with room_lock:
        if room_id in rooms:
            data = json.loads(message)
            from_peer_id = data.get("from_peer_id")
            host_id = room_host.get(room_id)
            
            if not host_id:
                log_event(f"Warning: No host found for room {room_id} during relay.")
                return

            if from_peer_id != host_id: # Message from a client
                if host_id in room_peer_ids[room_id]:
                    try:
                        await room_peer_ids[room_id][host_id].send_text(message)
                    except:
                        pass
            else: # Message from the host
                for peer_id, client in room_peer_ids[room_id].items():
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
        
        peer_id, is_host = await add_client_to_room(room_id, websocket)
        
        if peer_id is None:
            await websocket.send_text(json.dumps({"type": "error", "message": "Room full"}))
            await websocket.close()
            return
        
        # Send welcome with peer ID
        await websocket.send_text(json.dumps({
            "type": "welcome",
            "peer_id": peer_id,
            "room_id": room_id,
            "is_host": is_host
        }))
        log_event(f"🎉 Peer {peer_id} welcomed (host={is_host})")
        
        # Message loop - relay signaling only
        while True:
            message = await websocket.receive_text()
            data = json.loads(message)
            if data.get("type") == "join":
                continue
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
