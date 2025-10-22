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
# Track assigned peer IDs for each room
room_peer_ids: Dict[str, List[int]] = {}
next_peer_id: Dict[str, int] = {}  # Track the next available peer ID for each room
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
            room_peer_ids[room_id] = []
            next_peer_id[room_id] = 1  # Start with ID 1 (host) and increment
        
        if len(rooms[room_id]) >= 4:
            log_event(f"Room {room_id} is full")
            return False
        
        # Assign a unique peer ID
        assigned_id = next_peer_id[room_id]
        room_peer_ids[room_id].append(assigned_id)
        next_peer_id[room_id] += 1
        
        rooms[room_id].append(websocket)
        log_event(f"✅ Client added to room {room_id} as peer ID {assigned_id} ({len(rooms[room_id])}/4)")
        return True

async def remove_client_from_room(room_id: str, websocket: WebSocket):
    async with room_lock:
        if room_id in rooms and websocket in rooms[room_id]:
            # Find the index of the websocket to remove the corresponding peer ID
            client_index = rooms[room_id].index(websocket)
            removed_websocket = rooms[room_id].pop(client_index)
            
            # Remove the corresponding peer ID at the same index
            removed_peer_id = None
            if room_id in room_peer_ids and client_index < len(room_peer_ids[room_id]):
                removed_peer_id = room_peer_ids[room_id].pop(client_index)
            
            if removed_peer_id is not None:
                log_event(f"❌ Client (Peer ID {removed_peer_id}) removed from room {room_id} ({len(rooms[room_id])}/4 remaining)")
            else:
                log_event(f"❌ Client removed from room {room_id} ({len(rooms[room_id])}/4 remaining)")
            
            # Check if room is now empty and cleanup if needed
            if room_id in rooms and len(rooms[room_id]) == 0:
                if room_id in rooms:
                    del rooms[room_id]
                if room_id in room_peer_ids:
                    del room_peer_ids[room_id]
                if room_id in next_peer_id:
                    del next_peer_id[room_id]
                log_event(f"🧹 Room {room_id} cleaned up")

async def relay_message(room_id: str, message: str, sender: WebSocket):
    async with room_lock:
        if room_id in rooms:
            # Parse the message to check if it's a special request
            try:
                msg_data = json.loads(message)
                msg_type = msg_data.get("type")
                
                # Handle special message types
                if msg_type == "get_peers":
                    # Send back the list of connected peer IDs
                    if room_id in room_peer_ids:
                        peer_list = room_peer_ids[room_id][:]
                        peer_msg = {
                            "type": "peer_list",
                            "peers": peer_list
                        }
                        try:
                            await sender.send_text(json.dumps(peer_msg))
                            log_event(f"📤 Sent peer list {peer_list} to client in room {room_id}")
                        except Exception as e:
                            log_event(f"⚠️ Error sending peer list: {e}")
                    return
                elif msg_type == "new_peer_joined":
                    # Broadcast new peer announcement to all in room except sender
                    await broadcast_to_room(room_id, message, sender)
                    # Also send updated peer list to all clients
                    if room_id in room_peer_ids:
                        peer_list = room_peer_ids[room_id][:]
                        peer_msg = {
                            "type": "peer_list",
                            "peers": peer_list
                        }
                        await broadcast_to_room(room_id, json.dumps(peer_msg), sender)
                        log_event(f"📢 Sent updated peer list to all peers in room {room_id}")
                    return
                else:
                    # For regular messages (including WebRTC SDP offers/answers and ICE candidates), broadcast to all other clients
                    await broadcast_to_room(room_id, message, sender)
                    # Only log if it's not an ICE candidate (too verbose)
                    if "candidate" not in msg_data and "name" not in msg_data:
                        log_event(f"📤 Relayed {msg_data.get('type', 'msg')} in room {room_id}")
            except json.JSONDecodeError:
                # If not JSON, treat as regular message and broadcast to all others
                await broadcast_to_room(room_id, message, sender)
                log_event(f"📤 Relayed message in room {room_id}")


async def broadcast_to_room(room_id: str, message: str, exclude_client: WebSocket = None):
    """Broadcast a message to all clients in the room."""
    async with room_lock:
        if room_id in rooms:
            for client in rooms[room_id]:
                if exclude_client is None or client != exclude_client:
                    try:
                        await client.send_text(message)
                    except Exception as e:
                        log_event(f"⚠️ Broadcast error: {e}")

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
    
    # Clean up any empty rooms that might have been left behind
    try:
        async with room_lock:
            # Create a copy of room keys to avoid modification during iteration
            room_ids = list(rooms.keys())
            for room_id in room_ids:
                if room_id in rooms and len(rooms[room_id]) == 0:
                    if room_id in rooms:
                        del rooms[room_id]
                    if room_id in room_peer_ids:
                        del room_peer_ids[room_id]
                    if room_id in next_peer_id:
                        del next_peer_id[room_id]
                    log_event(f"🧹 Cleanup: Empty room {room_id} removed")
    except Exception as e:
        log_event(f"⚠️ Error during room cleanup: {e}")
    
    room_info = ""
    if rooms:
        room_info = "<h3>Active Rooms:</h3><ul>"
        for room_id, clients in rooms.items():
            room_info += f"<li>Room <code>{room_id}</code>: {len(clients)}/4 players</li>"
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